"""FigureSpec → QGIS 图层与 PrintLayout → PNG（GIS / 制图层）。

契约
====

* 本模块**只**负责制图：把 :class:`~cns_planner.gis.figure_spec.FigureSpec` 变成
  QGIS 图层、``QgsPrintLayout`` 版面项，最后导出 PNG 字节。
* 它**不**读项目文件、**不**改任何状态、**不**计算任何业务结论；所需数据全部已在
  FigureSpec 中（由 Application 层装配）。
* 所有真正的 QGIS/Qt 对象都在**调用方指定的线程**上创建（组合根通过
  ``context.qgis.call(...)`` 保证是 QGIS owner 线程）；本模块不自己起线程、不碰 UI。

固定版式（本轮最重要的视觉约束）
================================

    上方 = 地图；下方 = 独立图例区域（白底 + 边框，与地图分开）。
    地图内：左下比例尺、右下北箭头、沿边框经纬网；地图外：标题与副标题。

图例符号与地图符号**同源**：两者都由 :mod:`cns_planner.gis.figure_style` 的同一个
QgsSymbol 渲染（地图走图层渲染器，图例走 ``symbol_preview_image``）。
"""

from __future__ import annotations

import math
import os

from ..reporting.map_templates import CNS_LAYOUT_PROFILE, LAYER_DISPLAY_NAMES
from .figure_legend import CNS_LEGEND_BALANCE_TOLERANCE, legend_geometry
from .figure_spec import (
    GEOMETRY_FOOTPRINT, GEOMETRY_GRID_CELLS, GEOMETRY_LINE, GEOMETRY_POINT,
    GEOMETRY_POLYGON,
)
from .figure_style import (
    CNS_LEGEND_GROUP_COLUMNS, FONT_CANDIDATES, FONT_FILE_CANDIDATES, LAYOUT,
    LEGEND_GROUP_COLUMNS, fill_symbol, label_style, line_symbol, marker_symbol, style,
    symbol_preview_image,
)


class FigureRenderError(RuntimeError):
    """渲染失败：携带中文原因，供上层转成业务错误。"""


WGS84_AUTHID = "EPSG:4326"


class QgisFigureRenderer:
    """把 FigureSpec 渲染成 PNG 字节（不落盘、不返回路径）。"""

    #: 北箭头实现标识：``simple`` = 程序化生成的「圆框 + 向上箭头 + N」；
    #: 旧版使用的 QGIS 自带复杂罗盘已不再使用。运行时可据此断言。
    north_arrow_style = "simple"

    def __init__(self, *, font_family=None):
        self.font_family = font_family or choose_font_family()
        self._font_files_registered = False

    # ---- 对外入口 -------------------------------------------------------------

    def render(self, spec, *, dpi=300.0):
        """渲染一张专题图，返回 PNG 字节。"""

        from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize
        from qgis.core import QgsApplication, QgsLayoutExporter, QgsPrintLayout, QgsProject

        ensure_fonts_registered(self)
        dpi = float(dpi or 300.0)
        page = _page_size(spec)
        project = QgsProject()
        try:
            layers = build_layers(spec, project)
            if not layers:
                raise FigureRenderError("没有任何可绘制的图层，已拒绝生成空白图件")
            layout = QgsPrintLayout(project)
            layout.initializeDefaults()
            _resize_page(layout, page)
            map_item, map_extent = _build_map(layout, spec, layers)
            font_family = self.font_family
            _build_title_band(layout, spec, page, font_family)
            _build_legend_block(layout, spec, page, font_family)
            _build_map_decorations(layout, spec, map_extent, font_family)
            _build_annotations(layout, spec, map_extent, font_family)
            _build_labels(layout, spec, map_extent, font_family)

            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            exporter = QgsLayoutExporter(layout)
            image = exporter.renderPageToImage(0, QSize(), dpi)
            if image is None or image.isNull():
                raise FigureRenderError("QGIS 版面渲染返回空图像")
            if not image.save(buffer, "PNG"):
                raise FigureRenderError("PNG 编码失败")
            payload = bytes(buffer.data())
            if not payload.startswith(b"\x89PNG"):
                raise FigureRenderError("渲染结果不是有效 PNG")
            return payload
        finally:
            project.clear()
            QgsApplication.processEvents()


# --------------------------------------------------------------------------- 版面工具

def _page_size(spec):
    layout = spec.layout or {}
    return (
        float(layout.get("document_width_mm") or 210.0),
        float(layout.get("document_height_mm") or 297.0),
    )


def _resize_page(layout, page):
    from qgis.core import QgsLayoutSize, QgsUnitTypes

    layout.pageCollection().page(0).setPageSize(
        QgsLayoutSize(page[0], page[1], QgsUnitTypes.LayoutMillimeters)
    )


def _position(x_mm, y_mm):
    from qgis.core import QgsLayoutPoint, QgsUnitTypes

    return QgsLayoutPoint(float(x_mm), float(y_mm), QgsUnitTypes.LayoutMillimeters)


def _size(width_mm, height_mm):
    from qgis.core import QgsLayoutSize, QgsUnitTypes

    return QgsLayoutSize(float(width_mm), float(height_mm), QgsUnitTypes.LayoutMillimeters)


def _add_label(layout, text, *, x_mm, y_mm, width_mm, height_mm, font_family,
               font_size, color, bold=False, halo=None, align=None, valign=None):
    """加一个文本版面项；``halo`` 给出底色时会先叠一层轻微偏移的同文本描边。"""

    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsLayoutItemLabel

    def make(item_text, item_color, dx_mm, dy_mm):
        item = QgsLayoutItemLabel(layout)
        item.setText(item_text)
        font = QFont(font_family)
        font.setPointSizeF(float(font_size))
        font.setBold(bool(bold))
        item.setFont(font)
        item.setFontColor(QColor(item_color))
        item.setMarginX(0.0)
        item.setMarginY(0.0)
        if align is not None:
            item.setHAlign(align)
        if valign is not None:
            item.setVAlign(valign)
        item.attemptMove(_position(x_mm + dx_mm, y_mm + dy_mm))
        item.attemptResize(_size(width_mm, height_mm))
        layout.addLayoutItem(item)
        return item

    if halo:
        for dx, dy in ((-0.2, 0.0), (0.2, 0.0), (0.0, -0.2), (0.0, 0.2)):
            make(text, halo, dx, dy)
    return make(text, color, 0.0, 0.0)


# --------------------------------------------------------------------------- 标题

def _build_title_band(layout, spec, page, font_family):
    """标题带：主标题 → **固定间距** → 副标题 → **固定间距** → 地图框。

    project revision / FigureSpec / 图面范围 / source 状态一律不出现在显著位置 ——
    它们继续写在 metadata 与 figure_spec 里（坐标系与 revision 另有一条极小的审计条，
    见 :func:`_build_map_decorations`）。
    """

    from qgis.PyQt.QtCore import Qt

    plan = spec.layout or {}
    margin = float(plan.get("margin_mm") or LAYOUT["page_margin_mm"])
    title_top = float(plan.get("title_top_mm") or margin)
    band = float(plan.get("title_band_mm") or LAYOUT["title_band_mm"])
    gap = float(plan.get("title_gap_mm") or LAYOUT["title_gap_mm"])
    subtitle_height = float(
        plan.get("subtitle_height_mm") or LAYOUT["subtitle_band_mm"]
    )
    main_height = float(
        plan.get("title_main_height_mm") or max(4.0, band - gap - subtitle_height)
    )
    subtitle_top = float(plan.get("subtitle_top_mm") or (title_top + main_height + gap))
    width = page[0] - 2 * margin
    _add_label(
        layout, spec.title, x_mm=margin, y_mm=title_top, width_mm=width,
        height_mm=main_height, font_family=font_family,
        font_size=float(LAYOUT["map_title_font_size"]), color=LAYOUT["map_title_color"],
        bold=True, align=Qt.AlignHCenter, valign=Qt.AlignVCenter,
    )
    subtitle = spec_subtitle(spec)
    if subtitle:
        _add_label(
            layout, subtitle, x_mm=margin, y_mm=subtitle_top,
            width_mm=width, height_mm=subtitle_height,
            font_family=font_family, font_size=float(LAYOUT["subtitle_font_size"]),
            color=LAYOUT["subtitle_color"], align=Qt.AlignHCenter, valign=Qt.AlignTop,
        )


def spec_subtitle(spec):
    """副标题：只显示简短航路名（无航路名时返回空字符串）。"""

    name = _route_display_name(spec)
    return name or ""


def _route_display_name(spec):
    """从 FigureSpec 的 route_start/route_end 与 route_id 派生一个简短航路名。"""

    parameters = spec.parameters or {}
    explicit = str(parameters.get("route_display_name") or "").strip()
    if explicit:
        return explicit
    start = _endpoint_name(spec.route_start)
    end = _endpoint_name(spec.route_end)
    if start and end:
        return f"{start} → {end}"
    if start or end:
        return start or end
    return "" if str(spec.route_id or "").startswith("DEMO") else str(spec.route_id or "")


def _endpoint_name(value):
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name else ""
    return ""


# --------------------------------------------------------------------------- 图层

def build_layers(spec, project):
    """按 FigureSpec 构建 QGIS 图层。

    **顺序语义（实测确认）**：``QgsMapSettings.setLayers`` / ``QgsLayoutItemMap.setLayers``
    的列表**第一个元素是最顶层**。因此这里按 z 值**降序**排列，让规划航路（z 最大）
    排在最前，永远压在障碍物与底图之上。
    """

    from qgis.core import QgsCoordinateReferenceSystem

    entries = []
    for layer in spec.layers:
        if layer.source_status != "available" or layer.feature_count <= 0:
            continue
        entry = _layer_entry(spec, layer)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: (-item["z"], item["key"]))
    built = []
    for entry in entries:
        layer = _vector_layer(entry)
        if layer is None or not layer.isValid():
            continue
        if layer.featureCount() <= 0:
            continue
        layer.setCrs(QgsCoordinateReferenceSystem(WGS84_AUTHID))
        project.addMapLayer(layer, False)
        built.append(layer)
    return built


def _layer_entry(spec, layer):
    """把 FigureSpec 图层 + 样式转成"待构建图层"描述（纯数据，不碰 QGIS）。"""

    style_item = style(layer.style_key)
    data = layer.data or {}
    if layer.geometry_type in (GEOMETRY_POLYGON, GEOMETRY_FOOTPRINT, GEOMETRY_GRID_CELLS):
        polygons = _polygons_of(spec, layer, data)
        if not polygons:
            return None
        return {"key": layer.layer_key, "z": style_item["z"], "style_key": layer.style_key,
                "kind": "polygon", "polygons": polygons, "name": layer.display_name,
                "holes": _holes_of(polygons, data)}
    if layer.geometry_type == GEOMETRY_LINE:
        geometry = data.get("geometry") or []
        if len(geometry) < 2:
            return None
        return {"key": layer.layer_key, "z": style_item["z"], "style_key": layer.style_key,
                "kind": "line", "lines": [geometry], "name": layer.display_name}
    if layer.geometry_type == GEOMETRY_POINT:
        points = [
            (float(item["longitude"]), float(item["latitude"]))
            for item in (data.get("points") or [])
            if isinstance(item, dict) and item.get("longitude") is not None
            and item.get("latitude") is not None
        ]
        if not points:
            return None
        return {"key": layer.layer_key, "z": style_item["z"], "style_key": layer.style_key,
                "kind": "point", "points": points, "name": layer.display_name}
    return None


def _polygons_of(spec, layer, data):
    """图层多边形外环。

    海域（``sea``）来自 FigureSpec 的**真实多边形差集**
    （地图画布矩形 − ``cartographic_land``，由装配层用可靠的多边形库计算），因此这里与
    其它面图层一样只读 ``data["polygons"]``；不再用"整个画布矩形铺海色"，也不再用
    矩形条带近似。内环（洞）由 :func:`_holes_of` 单独取出，并在
    :func:`_vector_layer` 里重建为真正的 QGIS 多边形内环。
    """

    return [ring for ring in (data.get("polygons") or []) if len(ring) >= 4]


def _holes_of(polygons, data):
    """把装配层给出的内环映射成 ``{polygon_index: [hole, ...]}``（数据驱动，绝不发明洞）。"""

    raw = data.get("polygon_holes")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, holes in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(polygons):
            continue
        cleaned = [hole for hole in (holes or []) if len(hole) >= 4]
        if cleaned:
            result[index] = cleaned
    return result


def _vector_layer(entry):
    from qgis.core import (
        QgsFeature, QgsGeometry, QgsPointXY, QgsSingleSymbolRenderer, QgsVectorLayer,
    )

    geometry_name = {"polygon": "Polygon", "line": "LineString", "point": "Point"}[entry["kind"]]
    layer = QgsVectorLayer(f"{geometry_name}?crs=EPSG:4326", entry["name"], "memory")
    features = []
    if entry["kind"] == "polygon":
        symbol = fill_symbol(entry["style_key"])
        holes = entry.get("holes") or {}
        for index, ring in enumerate(entry["polygons"]):
            feature = QgsFeature()
            # QgsGeometry.fromPolygonXY 的第一个环是外环，其余是内环（洞）。
            rings = [_ring(ring, QgsPointXY)]
            rings.extend(_ring(hole, QgsPointXY) for hole in (holes.get(index) or []))
            feature.setGeometry(QgsGeometry.fromPolygonXY(rings))
            features.append(feature)
    elif entry["kind"] == "line":
        symbol = line_symbol(entry["style_key"])
        for line in entry["lines"]:
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromPolylineXY(_ring(line, QgsPointXY)))
            features.append(feature)
    else:
        symbol = marker_symbol(entry["style_key"])
        for longitude, latitude in entry["points"]:
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(longitude, latitude)))
            features.append(feature)
    valid = [feature for feature in features if not feature.geometry().isEmpty()]
    layer.dataProvider().addFeatures(valid)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.updateExtents()
    return layer


def _ring(points, point_class):
    return [
        point_class(float(point[0]), float(point[1]))
        for point in points
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]


# --------------------------------------------------------------------------- 地图项

def _build_map(layout, spec, layers):
    from qgis.core import QgsCoordinateReferenceSystem, QgsLayoutItemMap, QgsRectangle

    plan = spec.layout or {}
    item = QgsLayoutItemMap(layout)
    item.setBackgroundEnabled(True)
    item.setBackgroundColor(_color(LAYOUT["map_background"]))
    item.attemptMove(_position(plan["map_left_mm"], plan["map_top_mm"]))
    item.attemptResize(_size(plan["map_width_mm"], plan["map_height_mm"]))
    item.setCrs(QgsCoordinateReferenceSystem(WGS84_AUTHID))
    item.setLayers(layers)
    width_mm = float(plan["map_width_mm"])
    height_mm = float(plan["map_height_mm"])
    frame_aspect = width_mm / height_mm if height_mm else 1.0
    extent = QgsRectangle(
        spec.extent.west, spec.extent.south, spec.extent.east, spec.extent.north,
    )
    centre = extent.center()
    data_aspect = extent.width() / extent.height() if extent.height() else frame_aspect
    if data_aspect >= frame_aspect:
        half_width = extent.width() * 0.5
        half_height = half_width / frame_aspect
    else:
        half_height = extent.height() * 0.5
        half_width = half_height * frame_aspect
    half_width = max(half_width, 1e-6)
    half_height = max(half_height, 1e-6)
    rendered = QgsRectangle(
        centre.x() - half_width, centre.y() - half_height,
        centre.x() + half_width, centre.y() + half_height,
    )
    item.zoomToExtent(rendered)
    _configure_grid(item, spec)
    layout.addLayoutItem(item)
    return item, rendered


def _configure_grid(map_item, spec):
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsCoordinateReferenceSystem, QgsLayoutItemMapGrid

    grid = map_item.grid()
    grid.setEnabled(True)
    grid.setCrs(QgsCoordinateReferenceSystem(WGS84_AUTHID))
    interval = _grid_interval(spec)
    grid.setIntervalX(interval)
    grid.setIntervalY(interval)
    grid.setStyle(QgsLayoutItemMapGrid.Solid)
    grid.setFrameStyle(QgsLayoutItemMapGrid.Zebra)
    grid.setFramePenSize(0.25)
    grid.setFramePenColor(QColor(LAYOUT["grid_frame_color"]))
    grid.setFrameFillColor1(QColor(255, 255, 255, 190))
    grid.setFrameFillColor2(QColor(236, 241, 245, 190))
    # 网格线用独立的浅灰线符号（默认黑色太抢眼，会压过航路）。
    try:
        from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer

        grid_line = QgsSimpleLineSymbolLayer(QColor(LAYOUT["grid_line_color"]), 0.18)
        symbol = QgsLineSymbol()
        symbol.changeSymbolLayer(0, grid_line)
        grid.setLineSymbol(symbol)
    except (ImportError, AttributeError):  # pragma: no cover - 老版本 PyQGIS
        pass
    grid.setAnnotationEnabled(True)
    # 标注位置：纬度在**地图框外侧**左右，经度在**地图框内侧**底边。
    #
    # 为什么经度必须放进内侧：地图框下沿之外紧接着就是那条薄审计条
    # （坐标系 / revision / 未显示图层），外侧经度标注会与它逐字重叠 —— 这正是
    # 用户反复反馈的"WGS84 小字与其它元素重叠"。放进内侧后两类信息各占一条
    # 互不相交的水平带，且都留在图框范围内。
    for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right,
                 QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
        grid.setAnnotationDisplay(QgsLayoutItemMapGrid.ShowAll, side)
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.HideAll, QgsLayoutItemMapGrid.Top)
    grid.setAnnotationPosition(
        QgsLayoutItemMapGrid.OutsideMapFrame, QgsLayoutItemMapGrid.Left,
    )
    grid.setAnnotationPosition(
        QgsLayoutItemMapGrid.OutsideMapFrame, QgsLayoutItemMapGrid.Right,
    )
    grid.setAnnotationPosition(
        QgsLayoutItemMapGrid.InsideMapFrame, QgsLayoutItemMapGrid.Bottom,
    )
    # 标注走向：纬度水平、经度垂直。
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, QgsLayoutItemMapGrid.Left)
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, QgsLayoutItemMapGrid.Right)
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, QgsLayoutItemMapGrid.Bottom)
    grid.setAnnotationFormat(QgsLayoutItemMapGrid.DecimalWithSuffix)
    grid.setAnnotationPrecision(2)
    font = QFont(choose_font_family())
    font.setPointSizeF(float(LAYOUT["grid_font_size"]))
    grid.setAnnotationFont(font)
    grid.setAnnotationFontColor(QColor("#3b4652"))
    return grid


def _grid_interval(spec):
    """经纬网间隔：由 segments_per_degree 派生，避免标签过密。"""

    segments = int((spec.parameters or {}).get("segments_per_degree") or 5)
    segments = max(2, min(20, segments))
    target = 1.0 / segments
    for candidate in (0.5, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01):
        if candidate <= target:
            return candidate
    return 0.01


# --------------------------------------------------------------------------- 比例尺 / 北箭头

def _build_map_decorations(layout, spec, map_extent, font_family):
    """地图装饰：左下角**黑白分段比例尺**（程序化绘制）+ 右下角**简洁北箭头**；另加图外审计条。

    比例尺为什么自己画：QGIS 原生 ``QgsLayoutItemScaleBar`` 在**地理 CRS**（EPSG:4326）
    的地图上把 "20 km" 换算成极小的地图单位，实测条宽接近 0，而所有数字与单位标签被挤在
    同一位置相互重叠（V4 的现场）。改成本地程序化绘制后，段宽由**实际渲染范围**精确换算，
    数字 / 单位 / 分段条对齐清楚，文本与条之间还有固定间隙。

    审计条（坐标系 / 项目 revision / 未显示图层）位于**地图框下沿之外、图例框之上**的
    独立薄条里，三段各自对齐。它绝不进入地图绘图区、不压地图边框、不压比例尺，
    也不与图例标题重叠 —— 之前那一串小字挤在地图边线上的问题由此消除。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemPicture

    plan = spec.layout or {}
    _build_scale_bar(layout, spec, map_extent, plan, font_family, Qt)

    size = float(plan.get("north_arrow_size_mm") or LAYOUT["north_arrow_size_mm"])
    arrow_margin = float(
        plan.get("north_arrow_margin_mm") or LAYOUT["north_arrow_margin_mm"]
    )
    arrow = QgsLayoutItemPicture(layout)
    path = north_arrow_path()
    if path:
        arrow.setPicturePath(path)
        arrow.setResizeMode(QgsLayoutItemPicture.ZoomResizeFrame)
    arrow.attemptMove(_position(
        float(plan["map_left_mm"]) + float(plan["map_width_mm"]) - arrow_margin - size,
        float(plan["map_top_mm"]) + float(plan["map_height_mm"]) - arrow_margin - size,
    ))
    arrow.attemptResize(_size(size, size))
    layout.addLayoutItem(arrow)

    _build_audit_strip(layout, spec, plan, font_family, Qt)


def scale_bar_geometry(spec, map_extent, layout=None):
    """黑白分段比例尺的几何（毫米，纯计算、可单测）。

    段长取 1/2/5 十进制档，使总长度落在 :data:`LAYOUT` 的目标区间内；段宽按**实际渲染
    范围**换算：``段宽 = 地图框宽 × 段长_km / 渲染范围宽_km``。因此"20 km"在图上就是
    真实的 20 km。
    """

    plan = layout if layout is not None else (spec.layout or {})
    map_width_mm = float(plan["map_width_mm"])
    map_height_mm = float(plan["map_height_mm"])
    map_left = float(plan["map_left_mm"])
    map_top = float(plan["map_top_mm"])
    extent = spec.extent
    data_width_deg = float(extent.east) - float(extent.west)
    width_km = float(extent.width_km or 0.0)
    if (data_width_deg > 0 and map_extent is not None and not map_extent.isEmpty()
            and float(map_extent.width()) > 0):
        width_km = width_km * (float(map_extent.width()) / data_width_deg)
    if not math.isfinite(width_km) or width_km <= 0:
        width_km = 1.0
    margin = float(plan.get("scale_bar_margin_mm") or LAYOUT["scale_bar_margin_mm"])
    segments = max(1, int(LAYOUT["scalebar_segments"]))
    bar_height = float(LAYOUT["scalebar_height_mm"])
    target_min = float(LAYOUT["scalebar_target_min_mm"])
    target_max = float(LAYOUT["scalebar_target_max_mm"])
    target_mid = (target_min + target_max) / 2.0
    steps = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0,
             500.0, 1000.0, 2000.0)
    chosen = None
    for step in steps:
        width_mm = map_width_mm * (step * segments) / width_km
        if chosen is None or abs(width_mm - target_mid) < abs(chosen[1] - target_mid):
            chosen = (step, width_mm)
        if target_min <= width_mm <= target_max:
            chosen = (step, width_mm)
            break
    segment_km, bar_width_mm = chosen
    # 绝不超出地图框（左右各留同样的内距）。
    ceiling = max(6.0, map_width_mm - 2.0 * margin)
    bar_width_mm = min(bar_width_mm, ceiling)
    return {
        "segment_km": float(segment_km),
        "segments": segments,
        "total_km": float(segment_km) * segments,
        "width_mm": bar_width_mm,
        "segment_width_mm": bar_width_mm / segments,
        "height_mm": bar_height,
        "left_mm": map_left + margin,
        "top_mm": map_top + map_height_mm - margin - bar_height,
        "margin_mm": margin,
        "label_gap_mm": float(LAYOUT["scalebar_label_gap_mm"]),
        "label_height_mm": float(LAYOUT["scalebar_label_height_mm"]),
        "rendered_width_km": width_km,
    }


def _build_scale_bar(layout, spec, map_extent, plan, font_family, qt):
    """程序化黑白分段比例尺：分段矩形 + 条上方的数字 / 单位标签。"""

    from qgis.core import QgsLayoutItemShape

    geometry = scale_bar_geometry(spec, map_extent, plan)
    segment_width = float(geometry["segment_width_mm"])
    for index in range(int(geometry["segments"])):
        shape = QgsLayoutItemShape(layout)
        shape.setShapeType(QgsLayoutItemShape.Rectangle)
        fill = (LAYOUT["scalebar_fill_dark"] if index % 2 else LAYOUT["scalebar_fill_light"])
        shape.setSymbol(_rectangle_symbol(
            fill, LAYOUT["scalebar_line_color"], LAYOUT["scalebar_border_width_mm"],
        ))
        shape.attemptMove(_position(
            float(geometry["left_mm"]) + index * segment_width, float(geometry["top_mm"]),
        ))
        shape.attemptResize(_size(segment_width, float(geometry["height_mm"])))
        layout.addLayoutItem(shape)

    label_height = float(geometry["label_height_mm"])
    label_top = float(geometry["top_mm"]) - float(geometry["label_gap_mm"]) - label_height
    label_width = max(18.0, segment_width)
    labels = (
        (float(geometry["left_mm"]), "0"),
        (float(geometry["left_mm"]) + float(geometry["width_mm"]),
         f"{geometry['total_km']:g} km"),
    )
    for anchor_x, text in labels:
        _add_label(
            layout, text, x_mm=anchor_x - label_width / 2.0, y_mm=label_top,
            width_mm=label_width, height_mm=label_height, font_family=font_family,
            font_size=float(LAYOUT["scalebar_font_size"]),
            color=LAYOUT["scalebar_line_color"], bold=True,
            align=qt.AlignHCenter, valign=qt.AlignVCenter,
        )


def _build_audit_strip(layout, spec, plan, font_family, qt):
    """地图框与图例框之间的**薄审计条**：左坐标系 / 中 revision / 右未显示图层。

    字号明显小于图例条目（:data:`LAYOUT['footer_strip_font_size']`），颜色弱化但可读；
    三段各自对齐到同一行的左 / 中 / 右，不进入地图绘图区、不压边框与比例尺。
    """

    from qgis.PyQt.QtGui import QColor

    left = float(plan["map_left_mm"])
    width = float(plan["map_width_mm"])
    top = float(
        plan.get("footer_strip_top_mm")
        or (float(plan["map_top_mm"]) + float(plan["map_height_mm"]) + 1.6)
    )
    strip_height = float(plan.get("footer_strip_mm") or LAYOUT["footer_strip_mm"])
    font_size = float(LAYOUT["footer_strip_font_size"])
    color = QColor(LAYOUT["footer_strip_color"]).name()
    third = width / 3.0
    segments = (
        (left, third, qt.AlignLeft, _coordinate_note(spec)),
        (left + third, third, qt.AlignHCenter, _revision_note(spec)),
        (left + 2 * third, third, qt.AlignRight, _omitted_note(spec)),
    )
    for x_mm, width_mm, align, text in segments:
        _add_label(
            layout, text, x_mm=x_mm, y_mm=top + 0.3, width_mm=width_mm,
            height_mm=strip_height, font_family=font_family,
            font_size=font_size, color=color, align=align, valign=qt.AlignVCenter,
        )


def _coordinate_note(spec):
    return f"坐标系：{WGS84_AUTHID.replace('EPSG:', 'WGS84 / EPSG:')}"


def _revision_note(spec):
    return f"项目 revision：{spec.generated_from_revision}"


def _omitted_note(spec):
    return f"未显示图层：{len(spec.omitted_layers or [])} 项"


# --------------------------------------------------------------------------- 图面说明框

def estimate_wrapped_lines(lines, inner_width_mm, font_size_pt):
    """估算 ``QgsLayoutItemLabel`` 在给定宽度下会把每一行折成几行（纯计算）。

    为什么必须估算：版面项会**自动换行**并把自身高度撑到内容高度，而背景矩形的高度是
    我们在画之前就定好的。若不估算，长句（例如页脚那段确认语义披露）会溢出白底框，
    压到地图内容上 —— 这正是实测发现的问题。
    """

    if not lines:
        return 0
    # 中文字符宽 ≈ 字号（pt→mm）；ASCII 与数字按 0.55 个汉字宽计权。
    char_mm = max(0.8, float(font_size_pt) / 72.0 * 25.4)
    per_line = max(6.0, float(inner_width_mm) / char_mm)
    total = 0
    for line in lines:
        weight = sum(1.0 if ord(char) > 0x2000 else 0.55 for char in str(line))
        total += max(1, int(math.ceil(weight / per_line))) if weight else 1
    return total


def annotation_box_geometry(annotation, plan, spec=None, map_extent=None):
    """说明框的版面几何（毫米，纯计算、可单测）。

    说明框是 CNS 专题图表达**能力限制 / 服务半径语义**的唯一图面位置。它被放在地图框
    左下、**比例尺与经度标注之上**，因此既不压比例尺、也不压北箭头、更不会跑出图框。
    高度按**折行后的实际行数**估算，保证白底框始终包住全部文字。
    """

    width = max(30.0, float(annotation.width_mm))
    line = float(LAYOUT["annotation_line_mm"])
    title_line = float(LAYOUT["annotation_title_line_mm"])
    padding = float(LAYOUT["annotation_padding_mm"])
    lines = [str(item) for item in (annotation.lines or [])]
    wrapped = estimate_wrapped_lines(
        lines, width - 2.0 * padding, float(LAYOUT["annotation_font_size"]),
    )
    height = title_line + wrapped * line + 2.0 * padding
    map_left = float(plan["map_left_mm"])
    map_top = float(plan["map_top_mm"])
    map_width = float(plan["map_width_mm"])
    map_height = float(plan["map_height_mm"])
    margin = float(annotation.offset_mm)
    anchor = str(annotation.anchor or "map_bottom_left")
    if anchor in ("map_bottom_left", "map_lower_left_above_scalebar"):
        # 底部要同时避开三样东西：地图框内侧的经度标注带、左下比例尺的**条与标签**、
        # 以及地图框下沿本身。三者取最靠上的那条线，说明框永远贴在它上方。
        bottom_clearance = (
            float(LAYOUT["grid_font_size"]) * 0.353 * 3.4
            + float(LAYOUT["scalebar_label_height_mm"]) + 1.2
        )
        top_limit = map_top + map_height - bottom_clearance
        if spec is not None and map_extent is not None:
            try:
                geometry = scale_bar_geometry(spec, map_extent, plan)
                bar_top = float(geometry["top_mm"])
                label_top = (
                    bar_top - float(geometry["label_gap_mm"]) - float(geometry["label_height_mm"])
                )
                top_limit = min(top_limit, label_top - 1.2)
            except (KeyError, TypeError, ValueError):  # pragma: no cover - 比例尺几何缺失
                pass
        x = map_left + margin
        y = top_limit - height
    elif anchor == "map_top_left":
        x = map_left + margin
        y = map_top + margin
    elif anchor == "map_top_right":
        x = map_left + map_width - margin - width
        y = map_top + margin
    else:  # map_bottom_right
        x = map_left + map_width - margin - width
        y = map_top + map_height - margin - height
    # 边界避让：说明框永远留在图框内。
    x = min(max(x, map_left + 0.8), map_left + map_width - width - 0.8)
    y = min(max(y, map_top + 0.8), map_top + map_height - height - 0.8)
    return {
        "x_mm": x, "y_mm": y, "width_mm": width, "height_mm": height,
        "bottom_mm": y + height,
        "title_height_mm": title_line, "line_mm": line, "padding_mm": padding,
        "line_count": len(lines), "wrapped_line_count": wrapped,
        "inside_map_frame": True,
        "anchor": anchor,
    }


def _item_height_mm(item, fallback):
    """版面项**实际**占位高度（毫米）。取不到时回退到估算值，绝不返回 0。"""

    if item is None:
        return float(fallback)
    for reader in ("rect", "sizeWithUnits"):
        try:
            value = getattr(item, reader)()
        except (AttributeError, RuntimeError, TypeError):  # pragma: no cover - 老版本 PyQGIS
            continue
        height = getattr(value, "height", None)
        if callable(height):
            height = height()
        if height is None:  # QgsLayoutMeasurement
            height = getattr(value, "length", None)
            if callable(height):
                height = height()
        try:
            number = float(height)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return float(fallback)


def _build_annotations(layout, spec, map_extent, font_family):
    """图面说明框：白底 + 细边框 + 标题 + 多行正文（纯版面项，不含任何几何）。

    高度**以文本实际渲染尺寸为准**：``QgsLayoutItemLabel`` 会按内容自动换行并撑高自身，
    估算出来的行高只能用来决定初始位置。因此这里先按估算放置，再读回标题与正文的
    真实高度，把白底框精确贴合到内容上并把整块内容底部对齐到预留基线 ——
    这样既不会出现"文字溢出白底框"，也不会出现"框很高、内容只占上半部分"。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemShape

    plan = spec.layout or {}
    items = []
    for annotation in spec.annotations or []:
        geometry = annotation_box_geometry(annotation, plan, spec, map_extent)
        if geometry["height_mm"] <= 0:
            continue
        padding = float(geometry["padding_mm"])
        inner_width = float(geometry["width_mm"]) - 2.0 * padding
        background = QgsLayoutItemShape(layout)
        background.setShapeType(QgsLayoutItemShape.Rectangle)
        background.setSymbol(_rectangle_symbol(
            LAYOUT["annotation_fill"], LAYOUT["annotation_border_color"],
            LAYOUT["annotation_border_width_mm"],
        ))
        background.attemptMove(_position(geometry["x_mm"], geometry["y_mm"]))
        background.attemptResize(_size(geometry["width_mm"], geometry["height_mm"]))
        layout.addLayoutItem(background)

        title_item = _add_label(
            layout, annotation.title,
            x_mm=geometry["x_mm"] + padding, y_mm=geometry["y_mm"] + padding * 0.6,
            width_mm=inner_width, height_mm=float(geometry["title_height_mm"]),
            font_family=font_family,
            font_size=float(LAYOUT["annotation_title_font_size"]),
            color=LAYOUT["annotation_title_color"], bold=True,
            align=Qt.AlignLeft, valign=Qt.AlignVCenter,
        )
        body = "\n".join(str(line) for line in (annotation.lines or []))
        body_item = None
        if body:
            body_item = _add_label(
                layout, body,
                x_mm=geometry["x_mm"] + padding,
                y_mm=geometry["y_mm"] + padding * 0.6 + float(geometry["title_height_mm"]),
                width_mm=inner_width,
                height_mm=float(geometry["wrapped_line_count"]) * float(geometry["line_mm"]),
                font_family=font_family,
                font_size=float(LAYOUT["annotation_font_size"]),
                color=LAYOUT["annotation_text_color"],
                align=Qt.AlignLeft, valign=Qt.AlignTop,
            )
        title_height = _item_height_mm(title_item, geometry["title_height_mm"])
        body_height = _item_height_mm(
            body_item, float(geometry["wrapped_line_count"]) * float(geometry["line_mm"]),
        )
        actual_height = padding * 0.6 + title_height + body_height + padding
        # 底部对齐到预留基线（比例尺/经度标注之上），整体上移，绝不改变底部位置。
        top = float(geometry["bottom_mm"]) - actual_height
        top = max(float(plan["map_top_mm"]) + 0.8, top)
        background.attemptMove(_position(geometry["x_mm"], top))
        background.attemptResize(_size(geometry["width_mm"], actual_height))
        title_item.attemptMove(_position(geometry["x_mm"] + padding, top + padding * 0.6))
        if body_item is not None:
            body_item.attemptMove(_position(
                geometry["x_mm"] + padding, top + padding * 0.6 + title_height,
            ))
        items.append(background)
    return items


# --------------------------------------------------------------------------- 标注

def _build_labels(layout, spec, map_extent, font_family):
    """在地图上叠加中文标注（白色 halo + 深色文字 + 可解释偏移）。

    实现方式：每个标注一个 ``QgsLayoutItemLabel``（外加同文本的浅色偏移拷贝作为 halo）。
    不使用覆盖整页的自绘版面项——那会遮住地图与图例。

    版式收口：

    * 起点 / 终点使用**同一套偏移规则**（沿航路外法向向外偏移
      :data:`LAYOUT['label_endpoint_offset_mm']`），偏移量大于星标半径，因此标签不压星标；
    * 标注框宽度固定为 :data:`LAYOUT['label_box_width_mm']`，为长中文名称预留空间；
    * 外侧在锚点左边时改用右对齐，标签始终**向外**展开（既不压星标也不出图）；
    * 转弯点标签保持次级视觉优先级（更小字号、更小偏移）。

    本轮新增的两条硬约束（用户明确要求）：

    1. **简单 collision suppression**：按 ``priority`` 从高到低放置，与已放置标注矩形
       相交的候选被**抑制**（不是移动位置、更不是改名字）。起终点永不抑制；
    2. **边界避让**：任何标注都被夹进地图框内，绝不跑出图框。
    """

    if not spec.labels or map_extent is None or map_extent.isEmpty():
        return []
    plan = spec.layout or {}
    frame = {
        "x": float(plan["map_left_mm"]), "y": float(plan["map_top_mm"]),
        "width": float(plan["map_width_mm"]), "height": float(plan["map_height_mm"]),
    }
    lon_span = map_extent.width()
    lat_span = map_extent.height()
    if lon_span <= 0 or lat_span <= 0:
        return []
    box_width = float(LAYOUT["label_box_width_mm"])
    box_height = float(LAYOUT["label_box_height_mm"])
    collision_gap = float(LAYOUT.get("label_collision_gap_mm", 0.5))
    directions = {
        "start": _endpoint_direction(spec, "start"),
        "end": _endpoint_direction(spec, "end"),
    }
    ordered = sorted(
        (
            label for label in spec.labels
            if label.longitude is not None and label.latitude is not None
        ),
        key=lambda item: (-int(item.priority or 50), str(item.kind), str(item.text)),
    )
    items = []
    # 说明框先占位：站名标签绝不压在图面说明框上（说明框是必须可读的披露内容）。
    reserved = []
    for annotation in spec.annotations or []:
        geometry = annotation_box_geometry(annotation, plan, spec, map_extent)
        reserved.append((
            float(geometry["x_mm"]) - collision_gap,
            float(geometry["y_mm"]) - collision_gap,
            float(geometry["x_mm"]) + float(geometry["width_mm"]) + collision_gap,
            float(geometry["y_mm"]) + float(geometry["height_mm"]) + collision_gap,
        ))
    placed = list(reserved)
    for label in ordered:
        style_item = _label_style_safe(label.style_key)
        if style_item is None:
            continue
        fx = (float(label.longitude) - map_extent.xMinimum()) / lon_span
        fy = (map_extent.yMaximum() - float(label.latitude)) / lat_span
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            continue
        anchor_x = frame["x"] + fx * frame["width"]
        anchor_y = frame["y"] + fy * frame["height"]
        placement = _label_placement(
            label, style_item, directions.get(label.kind), anchor_x, anchor_y,
            frame, box_width,
        )
        placement = _clamp_placement(placement, frame, box_width, box_height)
        if label.kind in ("start", "end"):
            # 起终点标签**永不因别的标签被抑制**，但必须避开说明框。
            placement = _avoid_reserved(
                placement, reserved, frame, box_width, box_height, collision_gap,
            )
        box = _label_box(placement, box_width, box_height, collision_gap)
        if label.kind not in ("start", "end") and _collides(box, placed):
            continue
        placed.append(box)
        items.append(_label_item(
            layout, label.text, x_mm=placement["x"], y_mm=placement["y"],
            font_family=font_family, style_item=style_item,
            box_width=box_width, box_height=box_height, align=placement["align"],
        ))
    return items


def _label_box(placement, box_width, box_height, collision_gap):
    return (
        float(placement["x"]) - collision_gap,
        float(placement["y"]) - collision_gap,
        float(placement["x"]) + box_width + 2.0 + collision_gap,
        float(placement["y"]) + box_height + collision_gap,
    )


def _avoid_reserved(placement, reserved, frame, box_width, box_height, collision_gap):
    """把一个标签移到预留矩形之外（优先上方，其次右侧/下方），并留在图框内。"""

    if not reserved:
        return placement
    current = _label_box(placement, box_width, box_height, collision_gap)
    if not _collides(current, reserved):
        return placement
    step = collision_gap + 1.2
    for left, top, right, bottom in reserved:
        candidates = (
            {"x": float(placement["x"]), "y": top - box_height - step,
             "align": placement["align"]},
            {"x": right + step, "y": float(placement["y"]), "align": placement["align"]},
            {"x": float(placement["x"]), "y": bottom + step, "align": placement["align"]},
        )
        for candidate in candidates:
            candidate = _clamp_placement(candidate, frame, box_width, box_height)
            if not _collides(_label_box(candidate, box_width, box_height, collision_gap),
                             reserved):
                return candidate
    return placement


def _clamp_placement(placement, frame, box_width, box_height):
    """把标签夹进地图框（边界避让）：宁可贴边，也不让标签跑出图框。"""

    margin = 0.6
    left = frame["x"] + margin
    top = frame["y"] + margin
    right = frame["x"] + frame["width"] - box_width - margin
    bottom = frame["y"] + frame["height"] - box_height - margin
    if right < left:
        right = left
    if bottom < top:
        bottom = top
    return {
        **placement,
        "x": min(max(float(placement["x"]), left), right),
        "y": min(max(float(placement["y"]), top), bottom),
    }


def _collides(box, placed):
    """矩形相交判定（轴对齐，毫米）。标注框之间保留 ``collision_gap`` 的净空。"""

    left, top, right, bottom = box
    for other_left, other_top, other_right, other_bottom in placed:
        if right <= other_left or left >= other_right:
            continue
        if bottom <= other_top or top >= other_bottom:
            continue
        return True
    return False


def _label_style_safe(style_key):
    try:
        return label_style(style_key)
    except KeyError:
        return None


def _endpoint_direction(spec, kind):
    """端点处的**向外**单位方向（经纬度空间）：起终点各自背离航路内部。"""

    points = [
        point for point in (spec.route_geometry or [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if len(points) < 2:
        return None
    if kind == "start":
        anchor, neighbour = points[0], points[1]
    else:
        anchor, neighbour = points[-1], points[-2]
    dx = float(anchor[0]) - float(neighbour[0])
    dy = float(anchor[1]) - float(neighbour[1])
    norm = math.hypot(dx, dy)
    if not math.isfinite(norm) or norm <= 0:
        return None
    return dx / norm, dy / norm


def _label_placement(label, style_item, direction, anchor_x, anchor_y, frame, box_width):
    """由锚点、偏移与地图框算出标签的落点与对齐方式（纯几何，不画任何东西）。"""

    from qgis.PyQt.QtCore import Qt

    if label.kind in ("start", "end"):
        base = float(LAYOUT["label_endpoint_offset_mm"])
        vx, vy = direction if direction else (1.0, 1.0)
        screen_x, screen_y = vx, -vy
        # 偏移方向过于水平时补一点垂直分量，避免标签与星标在同一水平线上。
        if abs(screen_y) < 0.35:
            screen_y = 0.35 if screen_y >= 0 else -0.35
        offset_x = screen_x * base
        offset_y = screen_y * base
        if offset_x < 0:
            return {"x": anchor_x + offset_x - box_width, "y": anchor_y + offset_y,
                    "align": Qt.AlignRight}
        return {"x": anchor_x + offset_x, "y": anchor_y + offset_y, "align": Qt.AlignLeft}
    if label.kind == "turn":
        base = float(LAYOUT["label_turn_offset_mm"])
        return {"x": anchor_x + base, "y": anchor_y - base, "align": Qt.AlignLeft}
    base = float(style_item["offset_mm"])
    return {"x": anchor_x - 7.0, "y": anchor_y - base, "align": Qt.AlignLeft}


def _label_item(layout, text, *, x_mm, y_mm, font_family, style_item,
                box_width=None, box_height=None, align=None):
    """一个标注文本（含白色 halo 与定位点）。"""

    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsLayoutItemLabel

    width = float(box_width if box_width is not None else LAYOUT["label_box_width_mm"])
    height = float(box_height if box_height is not None else LAYOUT["label_box_height_mm"])
    horizontal = align if align is not None else Qt.AlignLeft

    def make(content, color, dx, dy, size):
        item = QgsLayoutItemLabel(layout)
        item.setText(content)
        font = QFont(font_family)
        font.setPointSizeF(size)
        font.setBold(bool(style_item.get("bold")))
        item.setFont(font)
        item.setFontColor(QColor(color))
        item.setMarginX(0.0)
        item.setMarginY(0.0)
        item.setHAlign(horizontal)
        item.setVAlign(Qt.AlignTop)
        item.attemptMove(_position(x_mm + dx, y_mm + dy))
        item.attemptResize(_size(width, height))
        layout.addLayoutItem(item)
        return item

    for dx, dy in ((-0.22, 0.0), (0.22, 0.0), (0.0, -0.22), (0.0, 0.22)):
        make(text, style_item["halo_color"], dx, dy, float(style_item["font_size"]))
    item = make(text, style_item["color"], 0.0, 0.0, float(style_item["font_size"]))
    # 定位点：锚点处的小圆点（不画引线，避免遮挡主航路）。
    make("●", "#1c2733", -1.0, -1.4, 3.4)
    return item


# --------------------------------------------------------------------------- 图例

def _build_legend_block(layout, spec, page, font_family):
    """地图下方的独立图例区：白底 + 边框 + **统一的二维网格**（默认 2 列）。

    版式要点（本轮收口）：

    * 「图例」标题**独占一行**，位于框内左上，与内容之间不留大块空白；
    * 2 列横向展开，左右两列按语义分组均衡（左 = 地理环境 + 障碍物，
      右 = 既有设施 + 规划航路，见 :data:`~cns_planner.gis.figure_style.LEGEND_GROUP_COLUMNS`）；
    * **符号框固定宽度、文本固定起始 x、行高统一**，因此面 / 线 / 点符号基线一致；
    * 组标题略粗；组标题 → 组内条目间距小，组与组之间间距更大但统一；
    * 不存在的图层不占位，因此图例框高度随实际条目数自适应，不出现大块空白。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemPicture, QgsLayoutItemShape

    plan = spec.layout or {}
    top = float(plan["legend_top_mm"])
    left = float(plan["legend_left_mm"])
    width = float(plan["legend_width_mm"])
    height = float(plan["legend_height_mm"])
    side_padding = float(
        plan.get("legend_side_padding_mm") or LAYOUT["legend_side_padding_mm"]
    )
    title_height = float(LAYOUT["legend_header_mm"])

    background = QgsLayoutItemShape(layout)
    background.setShapeType(QgsLayoutItemShape.Rectangle)
    background.setSymbol(_rectangle_symbol(
        LAYOUT["legend_box_fill"], LAYOUT["legend_box_border"],
        LAYOUT["legend_box_width_mm"],
    ))
    background.attemptMove(_position(left, top))
    background.attemptResize(_size(width, height))
    layout.addLayoutItem(background)

    # 标题独占一行：位于框内左上，垂直居中于标题行。
    _add_label(
        layout, "图　例", x_mm=left + side_padding, y_mm=top + 0.5,
        width_mm=side_padding + float(LAYOUT["legend_symbol_box_mm"]) + 14.0,
        height_mm=title_height - 1.2,
        font_family=font_family,
        font_size=float(LAYOUT["legend_title_font_size"]), color="#12233a",
        bold=True, align=Qt.AlignLeft, valign=Qt.AlignVCenter,
    )

    entries = _legend_entries(spec)
    if not entries:
        _add_label(
            layout, "本图没有可独立成项的图层。", x_mm=left + side_padding,
            y_mm=top + title_height + 1.0, width_mm=width - 2 * side_padding,
            height_mm=5.0,
            font_family=font_family, font_size=float(LAYOUT["legend_item_font_size"]),
            color="#4a5560",
        )
        return background

    row_height = float(plan.get("legend_row_mm") or LAYOUT["legend_row_mm"])
    group_row = float(plan.get("legend_group_row_mm") or LAYOUT["legend_group_row_mm"])
    group_gap = float(plan.get("legend_group_gap_mm") or LAYOUT["legend_group_gap_mm"])
    group_item_gap = float(
        plan.get("legend_group_item_gap_mm") or LAYOUT["legend_group_item_gap_mm"]
    )
    top_padding = float(
        plan.get("legend_top_padding_mm") or LAYOUT["legend_top_padding_mm"]
    )
    symbol_box = float(plan.get("legend_symbol_box_mm") or LAYOUT["legend_symbol_box_mm"])
    gutter = float(plan.get("legend_text_gutter_mm") or LAYOUT["legend_text_gutter_mm"])
    column_gap = float(
        plan.get("legend_column_gap_mm") or LAYOUT["legend_column_gap_mm"]
    )
    # 分列与版面规划**共用** gis/figure_legend 的同一套算法（含同样的行距与语义分列），
    # 保证"框高"与"实际画出来的行"完全一致。CNS 五图使用自己的语义分列表，
    # 保证同一套分组在五张图上落在同样的左右两列。
    profile = str(plan.get("layout_profile") or "")
    cns_profile = profile == CNS_LAYOUT_PROFILE
    group_columns = (
        CNS_LEGEND_GROUP_COLUMNS if cns_profile else LEGEND_GROUP_COLUMNS
    )
    geometry = legend_geometry(
        entries, row_height=row_height, group_row=group_row,
        columns=max(1, int(plan.get("legend_columns") or LAYOUT["legend_default_columns"])),
        header_height=title_height, group_gap=group_gap,
        group_item_gap=group_item_gap, group_columns=group_columns,
        top_padding=top_padding,
        # 与版面规划（Application 层）使用**同一套**均衡规则，否则框高与实际行数会不一致。
        balance_tolerance=(CNS_LEGEND_BALANCE_TOLERANCE if cns_profile else None),
    )
    columns = max(1, int(geometry["columns"]))
    column_width = (width - 2 * side_padding - (columns - 1) * column_gap) / columns
    text_width = max(6.0, column_width - symbol_box - gutter)
    for column, y_offset, kind, text, style_key in geometry["rows"]:
        x = left + side_padding + column * (column_width + column_gap)
        y = top + y_offset
        if kind == "group":
            _add_label(
                layout, text, x_mm=x, y_mm=y, width_mm=column_width,
                height_mm=group_row,
                font_family=font_family,
                font_size=float(LAYOUT["legend_group_font_size"]),
                color=LAYOUT["legend_group_text_color"], bold=True,
                valign=Qt.AlignVCenter,
            )
            continue
        if style_key:
            picture_path = _symbol_picture_path(style_key)
            if picture_path:
                picture = QgsLayoutItemPicture(layout)
                picture.setPicturePath(picture_path)
                picture.setResizeMode(QgsLayoutItemPicture.Zoom)
                # 符号框固定宽度、行内垂直居中：面 / 线 / 点三种符号基线一致。
                symbol_height = symbol_box * 0.8
                picture.attemptMove(_position(
                    x, y + max(0.0, (row_height - symbol_height) / 2.0),
                ))
                picture.attemptResize(_size(symbol_box, symbol_height))
                layout.addLayoutItem(picture)
        # 文本起始 x 固定 = 符号框右缘 + 固定间距（与符号形状无关）。
        _add_label(
            layout, text, x_mm=x + symbol_box + gutter, y_mm=y,
            width_mm=text_width, height_mm=row_height,
            font_family=font_family, font_size=float(LAYOUT["legend_item_font_size"]),
            color=LAYOUT["legend_item_text_color"], valign=Qt.AlignVCenter,
        )
    return background


def _legend_entries(spec):
    """图例条目（已按语义顺序排好），只包含真实存在的图层。"""

    names = {layer.layer_key: layer.display_name for layer in spec.layers}
    entries = []
    for item in spec.legend_items:
        entries.append({
            "layer_key": item.layer_key,
            "style_key": item.style_key,
            "text": item.display_name or names.get(item.layer_key)
            or LAYER_DISPLAY_NAMES.get(item.layer_key, item.layer_key),
            "group": item.legend_group,
        })
    return entries


def _rectangle_symbol(fill, border, width_mm):
    from qgis.core import QgsFillSymbol, QgsSimpleFillSymbolLayer

    layer = QgsSimpleFillSymbolLayer()
    layer.setColor(_color(fill))
    layer.setStrokeColor(_color(border))
    layer.setStrokeWidth(float(width_mm))
    symbol = QgsFillSymbol()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def _symbol_png(style_key):
    """图例符号 PNG 字节（与地图同一 QgsSymbol 渲染）。"""

    try:
        return symbol_preview_image(style_key)
    except KeyError:
        return b""


#: 图例符号 PNG 的内存缓存：同一进程内同一符号只渲染一次。
_SYMBOL_PICTURE_CACHE = {}


def _symbol_picture_path(style_key):
    """把图例符号写成受控临时图片并返回路径（供 QgsLayoutItemPicture 使用）。

    文件位于系统临时目录下的专用子目录，按样式键命名；内容由同一个 QgsSymbol 渲染，
    因此图例与地图符号一致。临时文件不进入仓库、也不进入项目目录。
    """

    import hashlib
    import tempfile

    cached = _SYMBOL_PICTURE_CACHE.get(style_key)
    if cached and os.path.isfile(cached):
        return cached
    payload = _symbol_png(style_key)
    if not payload:
        return None
    directory = os.path.join(tempfile.gettempdir(), "cns-map-figure-symbols")
    os.makedirs(directory, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()[:16]
    path = os.path.join(directory, f"{style_key}-{digest}.png")
    if not os.path.isfile(path):
        with open(path, "wb") as stream:
            stream.write(payload)
    _SYMBOL_PICTURE_CACHE[style_key] = path
    return path


# --------------------------------------------------------------------------- 字体 / 颜色 / 资源

def choose_font_family():
    """选一个可用的中文字体族（Windows 优先系统字体；字体文件绝不提交进仓库）。"""

    from qgis.PyQt.QtGui import QFontDatabase

    families = set()
    try:
        families = set(QFontDatabase().families())
    except (RuntimeError, TypeError):  # pragma: no cover - 无字体数据库时
        families = set()
    for candidate in FONT_CANDIDATES:
        if candidate in families:
            return candidate
    return FONT_CANDIDATES[0]


def ensure_fonts_registered(renderer):
    """把系统已安装的中文字体文件注册进 Qt（只读系统路径，不复制、不提交）。"""

    if getattr(renderer, "_font_files_registered", False):
        return
    from qgis.PyQt.QtGui import QFontDatabase

    for path in FONT_FILE_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            QFontDatabase.addApplicationFont(path)
        except (RuntimeError, TypeError):  # pragma: no cover - 字体文件损坏
            continue
    renderer._font_files_registered = True


def north_arrow_path():
    """**简洁北箭头**：白底圆形 + 向上箭头 + 字母 N（程序化生成 SVG，离线可复现）。

    不再使用 QGIS 自带的复杂罗盘符号：参考图要求的是"N + 圆形轮廓 + 简单向上箭头"。
    SVG 只在系统临时目录生成一次，不进入仓库、也不进入项目目录。
    """

    import hashlib
    import tempfile

    cached = _NORTH_ARROW_CACHE.get("path")
    if cached and os.path.isfile(cached):
        return cached
    svg = _north_arrow_svg()
    directory = os.path.join(tempfile.gettempdir(), "cns-map-figure-symbols")
    os.makedirs(directory, exist_ok=True)
    digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(directory, f"north-arrow-{digest}.svg")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(svg)
    _NORTH_ARROW_CACHE["path"] = path
    return path


#: 北箭头 SVG 的进程内缓存。
_NORTH_ARROW_CACHE = {}


def _north_arrow_svg():
    """简洁北箭头：圆形边框 + 向上实心箭头 + 顶部字母 N。"""

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        '<circle cx="32" cy="36" r="21" fill="#ffffff" fill-opacity="0.92" '
        'stroke="#12233a" stroke-width="1.6"/>'
        '<polygon points="32,18 41,45 32,38 23,45" fill="#12233a"/>'
        '<rect x="29.4" y="24" width="5.2" height="3.6" fill="#ffffff" fill-opacity="0.0"/>'
        '<text x="32" y="12.5" font-family="Microsoft YaHei, SimHei, sans-serif" '
        'font-size="13" font-weight="bold" text-anchor="middle" fill="#12233a">N</text>'
        '</svg>'
    )


def _color(value):
    from qgis.PyQt.QtGui import QColor

    return QColor(str(value))


__all__ = [
    "FigureRenderError", "QgisFigureRenderer", "annotation_box_geometry", "build_layers",
    "choose_font_family", "ensure_fonts_registered", "estimate_wrapped_lines",
    "north_arrow_path", "scale_bar_geometry", "spec_subtitle",
]
