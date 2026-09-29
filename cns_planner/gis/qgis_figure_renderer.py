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

from ..reporting.map_templates import LAYER_DISPLAY_NAMES
from .figure_legend import legend_geometry
from .figure_spec import (
    GEOMETRY_FOOTPRINT, GEOMETRY_GRID_CELLS, GEOMETRY_LINE, GEOMETRY_POINT,
    GEOMETRY_POLYGON,
)
from .figure_style import (
    FONT_CANDIDATES, FONT_FILE_CANDIDATES, LAYOUT, LEGEND_GROUP_COLUMNS, fill_symbol,
    label_style, line_symbol, marker_symbol, style, symbol_preview_image,
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
    # 标注位置：纬度在左右，经度只在底部（避免上下两侧出现重复且过密的经度标注）。
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
        QgsLayoutItemMapGrid.OutsideMapFrame, QgsLayoutItemMapGrid.Bottom,
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
    directions = {
        "start": _endpoint_direction(spec, "start"),
        "end": _endpoint_direction(spec, "end"),
    }
    items = []
    for label in spec.labels:
        if label.longitude is None or label.latitude is None:
            continue
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
        items.append(_label_item(
            layout, label.text, x_mm=placement["x"], y_mm=placement["y"],
            font_family=font_family, style_item=style_item,
            box_width=box_width, box_height=box_height, align=placement["align"],
        ))
    return items


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
    # 保证"框高"与"实际画出来的行"完全一致。
    geometry = legend_geometry(
        entries, row_height=row_height, group_row=group_row,
        columns=max(1, int(plan.get("legend_columns") or LAYOUT["legend_default_columns"])),
        header_height=title_height, group_gap=group_gap,
        group_item_gap=group_item_gap, group_columns=LEGEND_GROUP_COLUMNS,
        top_padding=top_padding,
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
    "FigureRenderError", "QgisFigureRenderer", "build_layers", "choose_font_family",
    "ensure_fonts_registered", "north_arrow_path", "scale_bar_geometry", "spec_subtitle",
]
