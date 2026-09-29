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

import os

from ..reporting.map_templates import LAYER_DISPLAY_NAMES
from .figure_legend import legend_geometry
from .figure_spec import (
    GEOMETRY_FOOTPRINT, GEOMETRY_GRID_CELLS, GEOMETRY_LINE, GEOMETRY_POINT,
    GEOMETRY_POLYGON,
)
from .figure_style import (
    FONT_CANDIDATES, FONT_FILE_CANDIDATES, LAYOUT, fill_symbol, label_style,
    line_symbol, marker_symbol, style, symbol_preview_image,
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
            _build_map_decorations(layout, spec, font_family)
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
               font_size, color, bold=False, halo=None, align=None):
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
    """标题带：只放**图名**（主标题）与简短航路名（副标题）。

    project revision / FigureSpec / 图面范围 / source 状态一律不再出现在显著位置 ——
    它们继续写在 metadata 与 figure_spec 里。
    """

    from qgis.PyQt.QtCore import Qt

    plan = spec.layout or {}
    margin = float(plan.get("margin_mm") or LAYOUT["page_margin_mm"])
    title_top = float(plan.get("title_top_mm") or margin)
    band = float(LAYOUT["title_band_mm"])
    width = page[0] - 2 * margin
    _add_label(
        layout, spec.title, x_mm=margin, y_mm=title_top, width_mm=width,
        height_mm=band * 0.56, font_family=font_family,
        font_size=float(LAYOUT["map_title_font_size"]), color=LAYOUT["map_title_color"],
        bold=True, align=Qt.AlignHCenter,
    )
    subtitle = spec_subtitle(spec)
    if subtitle:
        _add_label(
            layout, subtitle, x_mm=margin, y_mm=title_top + band * 0.60,
            width_mm=width, height_mm=band * 0.38,
            font_family=font_family, font_size=float(LAYOUT["subtitle_font_size"]),
            color=LAYOUT["subtitle_color"], align=Qt.AlignHCenter,
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

def _build_map_decorations(layout, spec, font_family):
    """地图内装饰：左下角**黑白分段比例尺** + 右下角**简洁北箭头** + 页脚审计小字。"""

    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsLayoutItemPicture, QgsLayoutItemScaleBar, QgsUnitTypes

    plan = spec.layout or {}
    margin = float(plan.get("scale_bar_margin_mm") or 3.0)
    segment_km = _scale_bar_segment_km(spec)
    scale_bar = QgsLayoutItemScaleBar(layout)
    scale_bar.setStyle("Single Box")
    scale_bar.setUnits(QgsUnitTypes.DistanceKilometers)
    scale_bar.setUnitLabel("km")
    scale_bar.setNumberOfSegments(2)
    scale_bar.setNumberOfSegmentsLeft(0)
    scale_bar.setUnitsPerSegment(segment_km)
    font = QFont(font_family)
    font.setPointSizeF(float(LAYOUT["scalebar_font_size"]))
    font.setBold(True)
    scale_bar.setFont(font)
    scale_bar.setFontColor(QColor("#111111"))
    scale_bar.setLineColor(QColor("#111111"))
    # 黑白分段：第一段白底黑框、第二段黑底白框，边界清晰。
    scale_bar.setFillColor(QColor(255, 255, 255, 255))
    scale_bar.setFillColor2(QColor(17, 17, 17, 255))
    scale_bar.setHeight(2.4)
    scale_bar.setLineWidth(0.3)
    scale_bar.setMinimumBarWidth(20.0)
    scale_bar.setMaximumBarWidth(34.0)
    scale_bar.attemptMove(_position(
        float(plan["map_left_mm"]) + margin,
        float(plan["map_top_mm"]) + float(plan["map_height_mm"]) - margin - 9.5,
    ))
    layout.addLayoutItem(scale_bar)

    size = float(plan.get("north_arrow_size_mm") or 10.0)
    arrow = QgsLayoutItemPicture(layout)
    path = north_arrow_path()
    if path:
        arrow.setPicturePath(path)
        arrow.setResizeMode(QgsLayoutItemPicture.ZoomResizeFrame)
    arrow.attemptMove(_position(
        float(plan["map_left_mm"]) + float(plan["map_width_mm"]) - margin - size,
        float(plan["map_top_mm"]) + float(plan["map_height_mm"]) - margin - size,
    ))
    arrow.attemptResize(_size(size, size))
    layout.addLayoutItem(arrow)

    # 审计脚注：位于地图框与图例框之间的间隙，字号极小，不干扰图面。
    footer_y = float(plan["map_top_mm"]) + float(plan["map_height_mm"]) + 0.6
    _add_label(
        layout, _footer_note(spec),
        x_mm=float(plan["map_left_mm"]),
        y_mm=footer_y,
        width_mm=float(plan["map_width_mm"]), height_mm=3.6,
        font_family=font_family, font_size=float(LAYOUT["map_credit_font_size"]),
        color="#6b7683",
    )


def _footer_note(spec):
    """页脚审计脚注（极小字号）：只放最短的事实，不干扰图面。"""

    omitted = len(spec.omitted_layers or [])
    return f"WGS84 · revision {spec.generated_from_revision} · 未显示图层 {omitted} 项"


def _scale_bar_segment_km(spec):
    """比例尺分段长度：按图面宽度取 1/2/5 十进制档，使总长约 30~45 mm。"""

    width_km = float(spec.extent.width_km or 0.0)
    if width_km <= 0:
        return 5.0
    target = width_km / 6.0
    for step in (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0):
        if step >= target:
            return step
    return 100.0


# --------------------------------------------------------------------------- 标注

def _build_labels(layout, spec, map_extent, font_family):
    """在地图上叠加中文标注（白色 halo + 深色文字 + 可解释偏移）。

    实现方式：每个标注一个 ``QgsLayoutItemLabel``（外加同文本的浅色偏移拷贝作为 halo）。
    不使用覆盖整页的自绘版面项——那会遮住地图与图例。
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
        offset_x, offset_y = _label_offset(label, style_item)
        items.append(_label_item(
            layout, label.text,
            x_mm=frame["x"] + fx * frame["width"] + offset_x,
            y_mm=frame["y"] + fy * frame["height"] + offset_y,
            font_family=font_family, style_item=style_item,
        ))
    return items


def _label_item(layout, text, *, x_mm, y_mm, font_family, style_item):
    """一个标注文本（含白色 halo 与定位点）。"""

    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsLayoutItemLabel

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
        item.setHAlign(Qt.AlignLeft)
        item.setVAlign(Qt.AlignTop)
        item.attemptMove(_position(x_mm + dx, y_mm + dy))
        item.attemptResize(_size(30.0, 5.0))
        layout.addLayoutItem(item)
        return item

    for dx, dy in ((-0.22, 0.0), (0.22, 0.0), (0.0, -0.22), (0.0, 0.22)):
        make(text, style_item["halo_color"], dx, dy, float(style_item["font_size"]))
    item = make(text, style_item["color"], 0.0, 0.0, float(style_item["font_size"]))
    # 定位点：锚点处的小圆点（不画引线，避免遮挡主航路）。
    make("●", "#1c2733", -1.0, -1.4, 3.4)
    return item


def _label_style_safe(style_key):
    try:
        return label_style(style_key)
    except KeyError:
        return None


def _label_offset(label, style_item):
    """简单、可解释的偏移：起终点向外，转弯点向上，地名向左上。"""

    base = float(style_item["offset_mm"])
    if label.kind == "start":
        return (base, base)
    if label.kind == "end":
        return (base, -base)
    if label.kind == "turn":
        return (base, -base)
    return (-7.0, -base)


# --------------------------------------------------------------------------- 图例

def _build_legend_block(layout, spec, page, font_family):
    """地图下方的独立图例区：白底 + 边框 + **横向多列紧凑排列**。

    版式要点（按产品要求）：

    * 标题「图 例」在地图框下方**居中**；
    * 条目按语义分组**横向展开**为 2~3 列（不再是纵向长列表）；
    * 符号与文字同行，行距紧凑；
    * 不存在的图层不占位，因此图例高度随实际条目数自动收缩。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemPicture, QgsLayoutItemShape

    plan = spec.layout or {}
    top = float(plan["legend_top_mm"])
    left = float(plan["legend_left_mm"])
    width = float(plan["legend_width_mm"])
    height = float(plan["legend_height_mm"])
    columns = max(1, int(plan.get("legend_columns") or 2))

    background = QgsLayoutItemShape(layout)
    background.setShapeType(QgsLayoutItemShape.Rectangle)
    background.setSymbol(_rectangle_symbol(
        LAYOUT["legend_box_fill"], LAYOUT["legend_box_border"],
        LAYOUT["legend_box_width_mm"],
    ))
    background.attemptMove(_position(left, top))
    background.attemptResize(_size(width, height))
    layout.addLayoutItem(background)

    title_height = float(LAYOUT["legend_header_mm"])
    _add_label(
        layout, "图　例", x_mm=left + 4.0, y_mm=top - 0.4,
        width_mm=float(LAYOUT["legend_symbol_box_mm"]) + 12.0, height_mm=title_height,
        font_family=font_family,
        font_size=float(LAYOUT["legend_title_font_size"]), color="#12233a",
        bold=True, align=Qt.AlignLeft,
    )

    entries = _legend_entries(spec)
    if not entries:
        _add_label(
            layout, "本图没有可独立成项的图层。", x_mm=left + 3.0,
            y_mm=top + title_height + 1.0, width_mm=width - 6.0, height_mm=5.0,
            font_family=font_family, font_size=float(LAYOUT["legend_item_font_size"]),
            color="#4a5560",
        )
        return background

    row_height = float(plan.get("legend_row_mm") or LAYOUT["legend_row_mm"])
    symbol_box = float(plan.get("legend_symbol_box_mm") or LAYOUT["legend_symbol_box_mm"])
    column_gap = float(LAYOUT["legend_column_gap_mm"])
    # 分列与版面规划**共用** gis/figure_legend 的同一套算法（含同样的行距），
    # 保证"框高"与"实际画出来的行"完全一致。
    geometry = legend_geometry(
        entries, row_height=row_height,
        group_row=float(plan.get("legend_group_row_mm") or LAYOUT["legend_group_row_mm"]),
        columns=max(1, int(plan.get("legend_columns") or LAYOUT["legend_default_columns"])),
        header_height=title_height,
    )
    columns = max(1, int(geometry["columns"]))
    column_width = (width - 2 * 4.0 - (columns - 1) * column_gap) / columns
    start_y = top
    for column, y_offset, kind, text, style_key in geometry["rows"]:
        x = left + 4.0 + column * (column_width + column_gap)
        y = start_y + y_offset
        if kind == "group":
            _add_label(
                layout, text, x_mm=x, y_mm=y, width_mm=column_width,
                height_mm=row_height * 1.05,
                font_family=font_family,
                font_size=float(LAYOUT["legend_group_font_size"]),
                color=LAYOUT["legend_group_text_color"], bold=True,
            )
            continue
        if style_key:
            picture_path = _symbol_picture_path(style_key)
            if picture_path:
                picture = QgsLayoutItemPicture(layout)
                picture.setPicturePath(picture_path)
                picture.setResizeMode(QgsLayoutItemPicture.Zoom)
                picture.attemptMove(_position(x, y + 0.25))
                picture.attemptResize(_size(symbol_box, symbol_box * 0.8))
                layout.addLayoutItem(picture)
        _add_label(
            layout, text, x_mm=x + symbol_box + 1.4, y_mm=y + 0.55,
            width_mm=column_width - symbol_box - 2.0, height_mm=row_height * 1.05,
            font_family=font_family, font_size=float(LAYOUT["legend_item_font_size"]),
            color=LAYOUT["legend_item_text_color"],
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
    "ensure_fonts_registered", "north_arrow_path", "spec_subtitle",
]
