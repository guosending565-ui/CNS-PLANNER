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

地图 CRS 与经纬网 CRS（Round30-B1）
==================================

* **地图画布使用本地米制 CRS**（``EPSG:32651``，见 :data:`DEFAULT_MAP_CRS`）：
  覆盖圈是在米制 CRS 里按真实距离构造的正圆，只有在米制画布上纸面投影才是正圆；
  在 ``EPSG:4326`` 画布上，同样的几何会被经纬比例拉成椭圆。
* **经纬网仍使用 ``EPSG:4326``**：图上看到的依旧是 ``122.xx°E`` / ``29.xx°N``。
* 因此 FigureSpec 里的 WGS84 几何（含覆盖圈、标注锚点、extent）在本模块内统一
  **投影一次**进米制画布，绝不出现"EPSG:4326 坐标 + EPSG:32651 CRS 标签"的错误组合。
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
    CNS_LABEL_CARD_COLORS, CNS_LABEL_CARD_ORDER, CNS_LABEL_SECONDARY_STYLE,
    CNS_LEGEND_GROUP_COLUMNS, FONT_CANDIDATES,
    FONT_FILE_CANDIDATES, LABEL_STYLES, LAYOUT, LEGEND_GROUP_COLUMNS, fill_symbol, label_style,
    line_symbol, marker_symbol, style, symbol_preview_image,
)


class FigureRenderError(RuntimeError):
    """渲染失败：携带中文原因，供上层转成业务错误。"""


#: 数据坐标（FigureSpec 内所有几何 / extent / 标注的 CRS）。
WGS84_AUTHID = "EPSG:4326"
#: **地图画布** CRS：本地米制（UTM 51N）。地图几何在纸面上因此保持真实米制比例，
#: "按米制 buffer 生成的覆盖圈"在图上就是正圆，而不是被经纬比例拉长的椭圆。
DEFAULT_MAP_CRS = "EPSG:32651"
#: **经纬网** CRS：始终是 WGS84，图上是 ``122.xx°E`` / ``29.xx°N``。
GRID_CRS_AUTHID = "EPSG:4326"
#: 经纬度标注后缀（QGIS 的 DecimalWithSuffix 在 EPSG:4326 下按 X/Y 自动给 E/N）。
GRID_LON_SUFFIX = "°E"
GRID_LAT_SUFFIX = "°N"
#: 经纬网默认间隔候选（度）：adaptive interval 的取值集合。
GRID_INTERVAL_CANDIDATES = (
    1.0, 0.5, 0.25, 0.2, 0.1, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005, 0.002, 0.001,
)
#: 起终点标签对应的样式键（终点用红色边框，起点用绿色边框）。
ENDPOINT_STYLE_KEYS = {"start": "label_endpoint", "end": "label_endpoint_end"}
#: 起终点 leader line 颜色（起点绿 / 终点红）。
ENDPOINT_LEADER_COLORS = {"start": "#12a150", "end": "#d81b1b"}


class QgisFigureRenderer:
    """把 FigureSpec 渲染成 PNG 字节（不落盘、不返回路径）。"""

    #: 北箭头实现标识：``simple`` = 程序化生成的「圆框 + 向上箭头 + N」；
    #: 旧版使用的 QGIS 自带复杂罗盘已不再使用。运行时可据此断言。
    north_arrow_style = "simple"

    def __init__(self, *, font_family=None):
        self.font_family = font_family or choose_font_family()
        self._font_files_registered = False
        #: 最近一次 ``render`` 真实画出的引线几何（毫米版面坐标，含折点 / 长度）。
        #: 它是**渲染结果的自述**，供真机量化测试与诊断直接读取，不参与任何绘制决策。
        self.leader_items = []

    # ---- 对外入口 -------------------------------------------------------------

    def render(self, spec, *, dpi=300.0):
        """渲染一张专题图，返回 PNG 字节。"""

        from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize
        from qgis.core import QgsApplication, QgsLayoutExporter, QgsPrintLayout, QgsProject

        ensure_fonts_registered(self)
        self.leader_items = []
        dpi = float(dpi or 300.0)
        page = _page_size(spec)
        project = QgsProject()
        try:
            context = render_context(spec)
            layers = build_layers(spec, project, context=context)
            if not layers:
                raise FigureRenderError("没有任何可绘制的图层，已拒绝生成空白图件")
            layout = QgsPrintLayout(project)
            layout.initializeDefaults()
            _resize_page(layout, page)
            map_item, map_extent = _build_map(layout, spec, layers, context)
            font_family = self.font_family
            _build_title_band(layout, spec, page, font_family)
            _build_legend_block(layout, spec, page, font_family)
            _build_map_decorations(layout, spec, map_extent, font_family, context)
            _build_annotations(layout, spec, map_extent, font_family)
            _build_labels(layout, spec, map_extent, font_family, context,
                          leader_log=self.leader_items)

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


# --------------------------------------------------------------------------- 坐标口径

def render_context(spec):
    """本次渲染的坐标口径（纯数据）：地图 CRS + 是否需要投影。

    ``map_crs`` 来自 ``spec.layout['map_crs']``（缺省 :data:`DEFAULT_MAP_CRS`）。
    只要它不等于 ``EPSG:4326``，FigureSpec 里的 WGS84 几何就**必须**在进入图层前
    投影一次 —— 这正是"圆在纸面上变椭圆"的根因修复点。
    """

    plan = spec.layout or {}
    map_crs = str(plan.get("map_crs") or DEFAULT_MAP_CRS).strip() or DEFAULT_MAP_CRS
    return {
        "map_crs": map_crs,
        "grid_crs": GRID_CRS_AUTHID,
        "requires_transform": map_crs.upper() != WGS84_AUTHID,
    }


def _coordinate_transform(source_authid, target_authid):
    """构建 ``source → target`` 的坐标变换器（QGIS 不可用时返回 ``None``）。"""

    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

    source = QgsCoordinateReferenceSystem(str(source_authid))
    target = QgsCoordinateReferenceSystem(str(target_authid))
    if not source.isValid() or not target.isValid():
        return None
    return QgsCoordinateTransform(source, target, QgsProject.instance())


def _transform_geometry(geometry, transformer):
    """把几何投影到目标 CRS；失败时如实返回 ``None``（绝不返回错 CRS 的几何）。"""

    if transformer is None or geometry is None or geometry.isEmpty():
        return geometry
    from qgis.core import QgsGeometry

    clone = QgsGeometry(geometry)
    try:
        clone.transform(transformer)
    except (RuntimeError, TypeError, ValueError):  # pragma: no cover - 异常 CRS 组合
        return None
    return None if clone.isEmpty() else clone


def projected_extent(spec, context, *, width_mm, height_mm):
    """FigureSpec 的 WGS84 extent → **地图画布** CRS 的矩形（含最小缓冲）。

    实现要点（顺序很重要）：

    1. 先把 WGS84 extent 的两条对角投影到米制，得到数据 bbox；
    2. 若 ``extent_aspect_locked``（CNS 五图）则按**地图框长宽比**在米制下对称加缓冲 ——
       这样 ``QgsLayoutItemMap.zoomToExtent`` 的马赛克式 fit 不会因为长宽比不一致而裁掉
       数据，纸面上的米制比例也严格 1:1（圆永远是圆）；缓冲量由 aspect 反解，最小 0。
    3. 非锁定档位保持历史行为：extent 中心 + 均匀半宽/半高，由地图项自己 fit。
    """

    from qgis.core import QgsRectangle

    plan = spec.layout or {}
    lock_aspect = bool((spec.parameters or {}).get("extent_aspect_locked"))
    frame_aspect = (float(width_mm) / float(height_mm)) if height_mm else 1.0
    rectangle = QgsRectangle(
        float(spec.extent.west), float(spec.extent.south),
        float(spec.extent.east), float(spec.extent.north),
    )
    if not context.get("requires_transform"):
        return _fit_extent(rectangle, frame_aspect, lock_aspect)
    transformer = _coordinate_transform(WGS84_AUTHID, context["map_crs"])
    if transformer is None:  # pragma: no cover - CRS 不可用时退回数据口径
        return _fit_extent(rectangle, frame_aspect, lock_aspect)
    lower_left = transformer.transform(
        float(spec.extent.west), float(spec.extent.south),
    )
    upper_right = transformer.transform(
        float(spec.extent.east), float(spec.extent.north),
    )
    projected = QgsRectangle(
        min(lower_left.x(), upper_right.x()), min(lower_left.y(), upper_right.y()),
        max(lower_left.x(), upper_right.x()), max(lower_left.y(), upper_right.y()),
    )
    # 投影会略微改变长宽比：按地图框比例对称补缓冲（只加不裁），保证 1:1 米制比例。
    return _fit_extent(projected, frame_aspect, True)


def _fit_extent(rectangle, frame_aspect, lock_aspect):
    """把矩形按地图框长宽比对称扩成一个 ratio 一致的矩形（只扩不裁）。"""

    from qgis.core import QgsRectangle

    width = float(rectangle.width())
    height = float(rectangle.height())
    if width <= 0 or height <= 0 or not math.isfinite(width) or not math.isfinite(height):
        return rectangle
    ratio = float(frame_aspect) if math.isfinite(frame_aspect) and frame_aspect > 0 else 1.0
    if not lock_aspect:
        # 历史行为：长边贴合，短边按比例补到中心两侧。
        if width / height >= ratio:
            half_width = width * 0.5
            half_height = half_width / ratio
        else:
            half_height = height * 0.5
            half_width = half_height * ratio
        centre = rectangle.center()
        return QgsRectangle(
            centre.x() - half_width, centre.y() - half_height,
            centre.x() + half_width, centre.y() + half_height,
        )
    centre = rectangle.center()
    if width / height < ratio:
        half_width = height * ratio * 0.5
        half_height = height * 0.5
    else:
        half_width = width * 0.5
        half_height = width / ratio * 0.5
    return QgsRectangle(
        centre.x() - half_width, centre.y() - half_height,
        centre.x() + half_width, centre.y() + half_height,
    )


def projected_point(context, longitude, latitude):
    """WGS84 点 → 地图画布坐标 ``(x, y)``；投影不可用时返回 ``None``。"""

    if not context.get("requires_transform"):
        return float(longitude), float(latitude)
    transformer = _coordinate_transform(WGS84_AUTHID, context["map_crs"])
    if transformer is None:  # pragma: no cover
        return None
    point = transformer.transform(float(longitude), float(latitude))
    return float(point.x()), float(point.y())


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
    """副标题：优先使用 FigureSpec 里由 canonical facts 组成的 ``subtitle``。

    字段为空时回退到"起点 → 终点"的简短航路名（历史行为不变）。
    """

    explicit = str(getattr(spec, "subtitle", "") or "").strip()
    if explicit:
        return explicit
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

def build_layers(spec, project, context=None):
    """按 FigureSpec 构建 QGIS 图层。

    **顺序语义（实测确认）**：``QgsMapSettings.setLayers`` / ``QgsLayoutItemMap.setLayers``
    的列表**第一个元素是最顶层**。因此这里按 z 值**降序**排列，让规划航路（z 最大）
    排在最前，永远压在障碍物与底图之上。

    **坐标语义（Round30-B1）**：FigureSpec 的几何是 WGS84；本函数按
    :func:`render_context` 给出的 ``map_crs`` **在写入内存图层前统一投影一次**，
    因此图层 CRS 与地图画布 CRS 严格一致，绝不出现"4326 坐标 + 32651 CRS 标签"。
    """

    from qgis.core import QgsCoordinateReferenceSystem

    context = context or render_context(spec)
    transformer = (
        _coordinate_transform(WGS84_AUTHID, context["map_crs"])
        if context.get("requires_transform") else None
    )
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
        layer = _vector_layer(entry, transformer, context["map_crs"])
        if layer is None or not layer.isValid():
            continue
        if layer.featureCount() <= 0:
            continue
        layer.setCrs(QgsCoordinateReferenceSystem(context["map_crs"]))
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


def _vector_layer(entry, transformer=None, map_crs=WGS84_AUTHID):
    from qgis.core import (
        QgsFeature, QgsGeometry, QgsPointXY, QgsSingleSymbolRenderer, QgsVectorLayer,
    )

    geometry_name = {"polygon": "Polygon", "line": "LineString", "point": "Point"}[entry["kind"]]
    layer = QgsVectorLayer(f"{geometry_name}?crs={map_crs}", entry["name"], "memory")
    features = []
    if entry["kind"] == "polygon":
        symbol = fill_symbol(entry["style_key"])
        holes = entry.get("holes") or {}
        for index, ring in enumerate(entry["polygons"]):
            feature = QgsFeature()
            # QgsGeometry.fromPolygonXY 的第一个环是外环，其余是内环（洞）。
            rings = [_ring(ring, QgsPointXY)]
            rings.extend(_ring(hole, QgsPointXY) for hole in (holes.get(index) or []))
            geometry = _transform_geometry(QgsGeometry.fromPolygonXY(rings), transformer)
            if geometry is None:
                continue
            feature.setGeometry(geometry)
            features.append(feature)
    elif entry["kind"] == "line":
        symbol = line_symbol(entry["style_key"])
        for line in entry["lines"]:
            geometry = _transform_geometry(
                QgsGeometry.fromPolylineXY(_ring(line, QgsPointXY)), transformer,
            )
            if geometry is None:
                continue
            feature = QgsFeature()
            feature.setGeometry(geometry)
            features.append(feature)
    else:
        symbol = marker_symbol(entry["style_key"])
        for longitude, latitude in entry["points"]:
            geometry = _transform_geometry(
                QgsGeometry.fromPointXY(QgsPointXY(longitude, latitude)), transformer,
            )
            if geometry is None:
                continue
            feature = QgsFeature()
            feature.setGeometry(geometry)
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

def _build_map(layout, spec, layers, context=None):
    from qgis.core import QgsCoordinateReferenceSystem, QgsLayoutItemMap

    context = context or render_context(spec)
    plan = spec.layout or {}
    item = QgsLayoutItemMap(layout)
    item.setBackgroundEnabled(True)
    item.setBackgroundColor(_color(LAYOUT["map_background"]))
    item.attemptMove(_position(plan["map_left_mm"], plan["map_top_mm"]))
    item.attemptResize(_size(plan["map_width_mm"], plan["map_height_mm"]))
    # 地图画布 CRS = 本地米制：图层几何已在 build_layers 里投影到同一 CRS。
    item.setCrs(QgsCoordinateReferenceSystem(context["map_crs"]))
    item.setLayers(layers)
    # extent 必须先转成画布 CRS 再交给地图项（WGS84 的度绝不能直接喂给米制画布）。
    rendered = projected_extent(
        spec, context,
        width_mm=float(plan["map_width_mm"]), height_mm=float(plan["map_height_mm"]),
    )
    item.zoomToExtent(rendered)
    _configure_grid(item, spec, context)
    layout.addLayoutItem(item)
    return item, rendered


def grid_interval_for(span_deg, *, target_ticks=None, minimum=None, maximum=None):
    """**adaptive grid interval**：让一个坐标轴大约显示 ``target_ticks`` 个主刻度。

    纯计算、可单测。返回 :data:`GRID_INTERVAL_CANDIDATES` 中**不小于**理想间隔
    （``span / target``）的最小候选：间隔越大刻度越少，因此这保证刻度数不超过上限，
    同时又不会因为"舍入到更细的档"而把刻度堆密。
    """

    span = _finite_number(span_deg, 0.0)
    ticks = int(_finite_number(
        target_ticks if target_ticks is not None else LAYOUT.get("grid_target_ticks"), 6,
    ))
    low = int(_finite_number(
        minimum if minimum is not None else LAYOUT.get("grid_min_ticks"), 3,
    ))
    high = int(_finite_number(
        maximum if maximum is not None else LAYOUT.get("grid_max_ticks"), 9,
    ))
    fallback = _finite_number(LAYOUT.get("grid_interval_deg"), 0.05)
    if span <= 0 or ticks <= 0:
        return fallback
    ideal = span / float(ticks)
    ascending = tuple(sorted(GRID_INTERVAL_CANDIDATES))
    coarser = [item for item in ascending if item >= ideal]
    chosen = coarser[0] if coarser else ascending[-1]
    actual = span / chosen
    if not (low <= actual <= high):
        # 极端范围（太窄或太宽）：退回"刻度数最接近目标"的那一档。
        chosen = min(ascending, key=lambda item: abs(span / item - ticks))
    return float(chosen)


def grid_intervals(spec):
    """当前图件的经度 / 纬度网格间隔（度）。"""

    span_lon = abs(float(spec.extent.east) - float(spec.extent.west))
    span_lat = abs(float(spec.extent.north) - float(spec.extent.south))
    return grid_interval_for(span_lon), grid_interval_for(span_lat)


def _configure_grid(map_item, spec, context=None):
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsCoordinateReferenceSystem, QgsLayoutItemMapGrid

    context = context or render_context(spec)
    grid = map_item.grid()
    grid.setEnabled(True)
    # **经纬网 CRS 与地图画布 CRS 是两件事**：画布是米制，网线仍按 WGS84 画，
    # 因此标注依旧是 122.xx°E / 29.xx°N。
    grid.setCrs(QgsCoordinateReferenceSystem(context.get("grid_crs") or GRID_CRS_AUTHID))
    interval_x, interval_y = grid_intervals(spec)
    grid.setIntervalX(interval_x)
    grid.setIntervalY(interval_y)
    grid.setStyle(QgsLayoutItemMapGrid.Solid)
    grid.setFrameStyle(QgsLayoutItemMapGrid.Zebra)
    grid.setFramePenSize(0.25)
    grid.setFramePenColor(QColor(LAYOUT["grid_frame_color"]))
    grid.setFrameFillColor1(QColor(255, 255, 255, 190))
    grid.setFrameFillColor2(QColor(236, 241, 245, 190))
    # 网格线：**细、浅、低视觉权重**，绝不抢航路与 CNS 符号。
    try:
        from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer

        grid_line = QgsSimpleLineSymbolLayer(
            QColor(LAYOUT["grid_line_color"]),
            float(LAYOUT.get("grid_line_width_mm", 0.14)),
        )
        symbol = QgsLineSymbol()
        symbol.changeSymbolLayer(0, grid_line)
        grid.setLineSymbol(symbol)
    except (ImportError, AttributeError):  # pragma: no cover - 老版本 PyQGIS
        pass
    grid.setAnnotationEnabled(True)
    # 正式样式：**只显示 bottom（经度）与 left（纬度）**，top / right 一律关闭，
    # 因此不会出现"同一刻度在左右两侧重复标注"的零散小字。
    for side in (QgsLayoutItemMapGrid.Left, QgsLayoutItemMapGrid.Right,
                 QgsLayoutItemMapGrid.Top, QgsLayoutItemMapGrid.Bottom):
        grid.setAnnotationDisplay(QgsLayoutItemMapGrid.HideAll, side)
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.ShowAll, QgsLayoutItemMapGrid.Bottom)
    grid.setAnnotationDisplay(QgsLayoutItemMapGrid.ShowAll, QgsLayoutItemMapGrid.Left)
    grid.setAnnotationPosition(
        QgsLayoutItemMapGrid.OutsideMapFrame, QgsLayoutItemMapGrid.Left,
    )
    # Round30-B1.2：经度标注也移到地图框**之外**的下侧（原先在框内底边）。
    # 框外的经度带不再占用任何地图内容空间，比例尺与经度标注因此彻底分离
    # （比例尺同时也向地图内部上移了一档，见 LAYOUT['scale_bar_margin_mm']）。
    grid.setAnnotationPosition(
        QgsLayoutItemMapGrid.OutsideMapFrame, QgsLayoutItemMapGrid.Bottom,
    )
    # **所有经纬度文字保持水平**（经度不再竖排）。
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, QgsLayoutItemMapGrid.Left)
    grid.setAnnotationDirection(QgsLayoutItemMapGrid.Horizontal, QgsLayoutItemMapGrid.Bottom)
    grid.setAnnotationFormat(QgsLayoutItemMapGrid.DecimalWithSuffix)
    grid.setAnnotationPrecision(2)
    font = QFont(choose_font_family())
    font.setPointSizeF(float(LAYOUT["grid_font_size"]))
    grid.setAnnotationFont(font)
    grid.setAnnotationFontColor(QColor(LAYOUT["grid_color"]))
    return grid


# --------------------------------------------------------------------------- 比例尺 / 北箭头

def _build_map_decorations(layout, spec, map_extent, font_family, context=None):
    """地图装饰：左下角**黑白分段比例尺** + 右下角**简洁北箭头** + 图外的披露条与自检条。

    比例尺为什么自己画：QGIS 原生 ``QgsLayoutItemScaleBar`` 在**地理 CRS**（EPSG:4326）
    的地图上把 "20 km" 换算成极小的地图单位，实测条宽接近 0，而所有数字与单位标签被挤在
    同一位置相互重叠（V4 的现场）。改成本地程序化绘制后，段宽由**实际渲染范围**精确换算，
    数字 / 单位 / 分段条对齐清楚，文本与条之间还有固定间隙。Round30-B1 起地图画布本身
    就是米制，比例尺因此直接读米制渲染宽度，比"按度换算"更精确。

    地图框外的两条薄带（都不进入地图绘图区）：

    * **图面披露条**：模板给的一句话限制说明（例如 Radar 能力限制），位于地图框正下方；
    * **自检条**：地图 CRS / 经纬网 CRS / 项目 revision / 未显示图层，四段各自对齐。

    两段都落在"地图框下沿 → 图例框上沿"之间，因此既不会压到地图内容，也不会与图例标题
    重叠 —— 这也是"地图内部不再放任何大说明框"的替代表达位置。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemPicture

    context = context or render_context(spec)
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

    _build_disclosure_strip(layout, spec, plan, font_family, Qt)
    if bool(plan.get("audit_footer", LAYOUT.get("audit_footer_default", False))):
        _build_audit_strip(layout, spec, plan, font_family, Qt, context)


def scale_bar_geometry(spec, map_extent, layout=None):
    """黑白分段比例尺的几何（毫米，纯计算、可单测）。

    段长取 1/2/5 十进制档，使总长度落在 :data:`LAYOUT` 的目标区间内；段宽按**实际渲染
    范围**换算：``段宽 = 地图框宽 × 段长_km / 渲染范围宽_km``。因此"20 km"在图上就是
    真实的 20 km。

    ``map_extent`` 与地图画布同 CRS：

    * 地图画布是**米制**（``spec.layout['map_crs']`` 非 WGS84，且渲染宽度明显大于
      1 度对应的量级）→ 渲染宽度本身就是米，直接取用；
    * 旧版把地图画布设为 WGS84（渲染宽度是"度"，例如 0.43 ≈ 42 km）→ 按
      ``spec.extent.width_km`` 与 渲染宽度 / 数据宽度 的比例换算，历史行为不变。
    """

    plan = layout if layout is not None else (spec.layout or {})
    map_width_mm = float(plan["map_width_mm"])
    map_height_mm = float(plan["map_height_mm"])
    map_left = float(plan["map_left_mm"])
    map_top = float(plan["map_top_mm"])
    extent = spec.extent
    data_width_deg = float(extent.east) - float(extent.west)
    base_km = float(extent.width_km or 0.0)
    rendered_width = 0.0
    if map_extent is not None and not map_extent.isEmpty():
        rendered_width = float(map_extent.width())
    map_crs = str(plan.get("map_crs") or "").upper()
    # 单位判定以**地图画布 CRS** 为准：Round30-B1 起 CNS 五图的画布是米制，渲染宽度本身
    # 就是米；只有历史/WGS84 画布（``map_crs`` 缺省或 EPSG:4326）才按度宽比例换算。
    metric_canvas = rendered_width > 0 and map_crs not in ("", WGS84_AUTHID)
    if metric_canvas:
        width_km = rendered_width / 1000.0
    else:
        width_km = base_km
        if rendered_width > 0 and data_width_deg > 0:
            width_km = base_km * (rendered_width / data_width_deg)
    if not math.isfinite(width_km) or width_km <= 0:
        width_km = base_km if math.isfinite(base_km) and base_km > 0 else 1.0
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
    """程序化黑白分段比例尺：**每段一个刻度 + 数字标签**（例如 ``0 | 2 | 4 km``）。

    Round30-B1.1：段数固定 3（模板参数可覆盖），每个分界点都画刻度线并标注数字，
    因此不会只显示 "0 ---- 4 km"；字号与刻度都放大到可读的正式等级。
    """

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

    # 刻度线：每个分界点一条竖线（段高 + 固定刻度高），视觉上明确"每 2 km 一格"。
    tick_height = float(LAYOUT.get("scalebar_tick_height_mm", 0.9))
    for index in range(int(geometry["segments"]) + 1):
        tick = QgsLayoutItemShape(layout)
        tick.setShapeType(QgsLayoutItemShape.Rectangle)
        tick.setSymbol(_rectangle_symbol(
            LAYOUT["scalebar_line_color"], LAYOUT["scalebar_line_color"],
            LAYOUT["scalebar_border_width_mm"],
        ))
        tick.attemptMove(_position(
            float(geometry["left_mm"]) + index * segment_width
            - float(LAYOUT["scalebar_border_width_mm"]) / 2.0,
            float(geometry["top_mm"]) - tick_height,
        ))
        tick.attemptResize(_size(float(LAYOUT["scalebar_border_width_mm"]),
                                 float(geometry["height_mm"]) + tick_height))
        layout.addLayoutItem(tick)

    label_height = float(geometry["label_height_mm"])
    label_top = float(geometry["top_mm"]) - float(geometry["label_gap_mm"]) - label_height
    label_width = max(14.0, segment_width * 1.25)
    labels = [
        (
            float(geometry["left_mm"]) + index * segment_width,
            f"{float(geometry['segment_km']) * index:g}"
            + (" km" if index == int(geometry["segments"]) else ""),
        )
        for index in range(int(geometry["segments"]) + 1)
    ]
    for anchor_x, text in labels:
        _add_label(
            layout, text, x_mm=anchor_x - label_width / 2.0, y_mm=label_top,
            width_mm=label_width, height_mm=label_height, font_family=font_family,
            font_size=float(LAYOUT["scalebar_font_size"]),
            color=LAYOUT["scalebar_line_color"], bold=True,
            align=qt.AlignHCenter, valign=qt.AlignVCenter,
        )


def _build_disclosure_strip(layout, spec, plan, font_family, qt):
    """地图框**正下方**的一句话披露条（模板给的 ``layout['map_disclosure']``）。

    Round30-B1：CNS 五图原先在地图主体左下角放大说明框。说明框会遮住地图内容，
    因此本轮把它移除，改为在**地图框之外**保留一句话的正式披露（例如
    "Radar-I：当前既有站址与模型约束下无可行布设"），完整解释进入 FigureSpec.metadata。
    没有披露文本时本函数不产生任何版面项。
    """

    from qgis.PyQt.QtGui import QColor

    text = str(plan.get("map_disclosure") or "").strip()
    if not text:
        return None
    left = float(plan["map_left_mm"])
    width = float(plan["map_width_mm"])
    top = float(plan.get("footer_disclosure_top_mm") or 0.0)
    height = float(plan.get("footer_disclosure_mm") or LAYOUT["footer_disclosure_mm"])
    return _add_label(
        layout, text, x_mm=left, y_mm=top, width_mm=width, height_mm=height,
        font_family=font_family, font_size=float(LAYOUT["footer_disclosure_font_size"]),
        color=QColor(LAYOUT["footer_disclosure_color"]).name(),
        align=qt.AlignLeft, valign=qt.AlignVCenter,
    )


def _build_audit_strip(layout, spec, plan, font_family, qt, context=None):
    """地图框与图例框之间的**薄自检条**：地图 CRS / 经纬网 CRS / revision / 未显示图层。

    字号明显小于图例条目（:data:`LAYOUT['footer_strip_font_size']`），颜色弱化但可读；
    四段各自对齐到同一行的左 / 中 / 右，不进入地图绘图区、不压边框与比例尺。
    """

    from qgis.PyQt.QtGui import QColor

    context = context or render_context(spec)
    left = float(plan["map_left_mm"])
    width = float(plan["map_width_mm"])
    top = float(plan.get("footer_strip_top_mm") or 0.0)
    strip_height = float(plan.get("footer_strip_mm") or LAYOUT["footer_strip_mm"])
    font_size = float(LAYOUT["footer_strip_font_size"])
    color = QColor(LAYOUT["footer_strip_color"]).name()
    # 四段：地图 CRS ｜ 经纬网 CRS ｜ revision ｜ 未显示图层。
    left_width = width * 0.31
    grid_width = width * 0.24
    revision_width = width * 0.22
    omitted_width = width - left_width - grid_width - revision_width
    segments = (
        (left, left_width, qt.AlignLeft, _coordinate_note(spec, context)),
        (left + left_width, grid_width, qt.AlignLeft, _grid_note(spec, context)),
        (left + left_width + grid_width, revision_width, qt.AlignHCenter,
         _revision_note(spec)),
        (left + left_width + grid_width + revision_width, omitted_width, qt.AlignRight,
         _omitted_note(spec)),
    )
    for x_mm, width_mm, align, text in segments:
        _add_label(
            layout, text, x_mm=x_mm, y_mm=top + 0.3, width_mm=width_mm,
            height_mm=strip_height, font_family=font_family,
            font_size=font_size, color=color, align=align, valign=qt.AlignVCenter,
        )


def _coordinate_note(spec, context=None):
    context = context or render_context(spec)
    return f"地图 CRS：{context['map_crs']}（本地米制，覆盖圈为正圆）"


def _grid_note(spec, context=None):
    context = context or render_context(spec)
    return f"经纬网：{context['grid_crs']}"


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

def label_text_width_mm(text, font_size_pt, *, bold=False):
    """标注文本的**估算**宽度（毫米，纯计算、可单测）。

    中日韩字符按 1 em、ASCII / 数字按约 0.54 em 计权；``bold`` 再加 4%。
    它只用于决定标签卡与避让框的尺寸 —— 不做像素级排版，也不改变任何业务值。
    """

    size = _finite_number(font_size_pt, 8.0)
    em = max(0.8, size / 72.0 * 25.4)
    cjk_ratio = _finite_number(LAYOUT.get("label_char_width_ratio"), 1.0)
    ascii_ratio = _finite_number(LAYOUT.get("label_ascii_width_ratio"), 0.54)
    weight = 0.0
    for char in str(text or ""):
        weight += cjk_ratio if ord(char) > 0x2000 else ascii_ratio
    weight *= 1.04 if bold else 1.0
    return weight * em


def wrap_label_text(text, max_width_mm, font_size_pt, *, bold=False, max_lines=2):
    """把标签文字折成最多 ``max_lines`` 行（**正式图禁止用省略号**）。

    折行规则（确定性、纯计算）：

    * **优先在自然边界断行**：空格、``/`` 之后，以及中日韩字符之间；
    * 断点由左到右**首次超宽**确定，然后回退到该位置之前最近的可用断点；
      若回退会剩下一段很短的尾巴（< :data:`MIN_TAIL_CHARS` 个字符），
      再往前找一个断点，避免出现 "…／通" + "信" 这种把服务后缀切碎的断法；
    * 若折到 ``max_lines`` 仍放不下，**最后手段**才在末行加省略号（返回值标注 ``truncated``）。

    返回 ``(lines, truncated)``。
    """

    size = _finite_number(font_size_pt, 9.0)
    limit = max(6.0, _finite_number(max_width_mm, 50.0))
    maximum = max(1, int(_finite_number(max_lines, 2)))
    chunks = [chunk for chunk in str(text or "").split("\n") if chunk != ""]
    if not chunks:
        return [], False
    lines, truncated = [], False
    for index, chunk in enumerate(chunks):
        if index and lines:
            # 显式换行：上一段已经收尾。
            pass
        pending = chunk
        while pending:
            if len(lines) >= maximum:
                truncated = True
                break
            taken = _fit_prefix(pending, limit, size, bold)
            if taken >= len(pending):
                lines.append(pending)
                pending = ""
                continue
            lines.append(pending[:taken].rstrip())
            pending = pending[taken:].lstrip()
        if truncated:
            break
    if not lines:
        lines = [""]
    if truncated:
        last = lines[-1]
        while last and label_text_width_mm(last + "…", size, bold=bold) > limit:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines, truncated


#: 断行后允许留在下一行的最短尾巴（字符数）：避免把服务后缀切碎。
MIN_TAIL_CHARS = 3


def _break_offsets(text):
    """所有可用断点（**切分位置**索引集合）：空格 / ``/`` 之后、中日韩字符之间。"""

    offsets = set()
    for index in range(1, len(text)):
        previous = text[index - 1]
        if previous in (" ", "/", "·", "-"):
            offsets.add(index)
        elif ord(previous) > 0x2000 or ord(text[index]) > 0x2000:
            offsets.add(index)
    return offsets


def _fit_prefix(text, limit, size, bold):
    """返回第一行最多能放下的字符数（在自然边界处断开）。"""

    total = len(text)
    # 整段放得下 → 全部取走。
    if label_text_width_mm(text, size, bold=bold) <= limit:
        return total
    # 逐字符累加找到首次超宽的位置。
    overflow = total
    for index in range(1, total + 1):
        if label_text_width_mm(text[:index], size, bold=bold) > limit:
            overflow = index - 1
            break
    overflow = max(1, overflow)
    offsets = _break_offsets(text)
    candidates = sorted(
        (offset for offset in offsets if 1 <= offset <= overflow), reverse=True,
    )
    for offset in candidates:
        tail = len(text) - offset
        if 0 < tail < MIN_TAIL_CHARS:
            # 尾巴太短：再往前找一个断点（把尾巴并回上一行）。
            continue
        return offset
    return max(1, min(overflow, total - 1)) if overflow < total else total


def _break_tokens(text):
    """把一段文字切成"可断点元组"：ASCII 单词整体成 token，其余逐字符成 token。"""

    tokens, buffer = [], ""
    for char in str(text or ""):
        if char == " ":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(" ")
        elif ord(char) > 0x2000:
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def label_card_size(text, style_item):
    """标签卡尺寸 ``(卡片宽, 卡片高)``（毫米，纯计算、可单测；兼容旧调用）。

    卡片宽 = 折行后最宽一行 + 2 ×（卡片内 padding + 文本留白）；
    卡片高 = 行数 × 行高 + 2 × 卡片内 padding。
    正式图**优先折行**（最多 ``max_lines`` 行），只有极端情况才截断。
    """

    geometry = label_card_geometry(text, style_item)
    return geometry["width_mm"], geometry["height_mm"]


def label_card_geometry(text, style_item):
    """标签卡的完整几何：折行结果 + 尺寸 + 是否截断（毫米，纯计算、可单测）。"""

    size = _finite_number(style_item.get("font_size"), 9.0)
    padding = _finite_number(style_item.get("card_padding_mm"), 1.0)
    text_padding = _finite_number(LAYOUT.get("label_card_text_padding_mm"), 1.4)
    max_width = _finite_number(style_item.get("max_width_mm"), 50.0)
    max_lines = int(_finite_number(style_item.get("max_lines"), 2))
    bold = bool(style_item.get("bold"))
    # 文字可用宽度 = 卡片最大宽度 − 两侧 padding 与留白。
    inner = max(8.0, max_width - 2.0 * (padding + text_padding))
    lines, truncated = wrap_label_text(
        text, inner, size, bold=bold, max_lines=max_lines,
    )
    if not lines:
        lines = [""]
    line_height = size / 72.0 * 25.4 * 1.32
    widest = max(label_text_width_mm(line, size, bold=bold) for line in lines)
    width = widest + 2.0 * (padding + text_padding)
    height = len(lines) * line_height + 2.0 * padding
    return {
        "lines": lines,
        "line_count": len(lines),
        "truncated": bool(truncated),
        "width_mm": max(10.0, width),
        "height_mm": max(4.0, height),
        "line_height_mm": line_height,
        "padding_mm": padding,
        "text_padding_mm": text_padding,
    }


def label_secondary_style(style_item=None):
    """主卡**第二行**（同址 Navigation service tag）的文字样式（纯计算、可单测）。

    颜色 / 字号来自 :data:`~cns_planner.gis.figure_style.CNS_LABEL_SECONDARY_STYLE`；
    底纹取 :data:`CNS_LABEL_CARD_COLORS` 的 ``navigation`` 填充色 —— 与通信 / RID / Radar
    的分色方案**同一张表**，因此不引入第二套配色定义。
    """

    secondary = CNS_LABEL_SECONDARY_STYLE
    family = str(secondary.get("fill_family") or "navigation")
    card = CNS_LABEL_CARD_COLORS.get(family) or {}
    return {
        "fill": str(card.get("fill") or "#fdf8dd"),
        "border": str(card.get("border") or "#a98a12"),
        "text_color": str(secondary.get("text_color") or "#4a3a00"),
        "font_size": float(secondary.get("font_size") or 9.5),
        "bold": bool(secondary.get("bold", True)),
    }


def label_card_geometry_with_secondary(text, style_item, secondary_text):
    """带**第二行**的标签卡几何（毫米，纯计算、可单测）。

    第一行仍是被标注对象的主名（起降点名，沿用 ``style_item`` 的字号 / 折行规则）；
    第二行是合并进来的服务标签行（例如"导航监测提案（未确认）"）。

    卡片高度账目（三段，互不重叠）：

    * ``primary_text_height_mm``：第一行折行后的**文字块**高度（不含 padding）；
    * ``secondary_line_height_mm``：第二行单行高度；
    * 上下各一个 ``padding``。

    因此``height = padding + primary_text + secondary_line + padding``，第二行底纹条的
    位置就是 ``padding + primary_text``。第二行**不折行**：它是一枚 service tag，
    宁可卡片略宽，也不把 tag 拆成两行。
    """

    primary = label_card_geometry(text, style_item)
    if not str(secondary_text or ""):
        return primary
    secondary = label_secondary_style(style_item)
    padding = _finite_number(style_item.get("card_padding_mm"), 1.0)
    text_padding = _finite_number(LAYOUT.get("label_card_text_padding_mm"), 1.4)
    secondary_line_height = (
        float(secondary["font_size"]) / 72.0 * 25.4 * 1.32
    )
    primary_text_height = (
        float(primary["height_mm"]) - 2.0 * float(primary["padding_mm"])
    )
    primary_width = max(10.0, float(primary["width_mm"]))
    secondary_width = (
        label_text_width_mm(
            str(secondary_text), float(secondary["font_size"]),
            bold=bool(secondary["bold"]),
        )
        + 2.0 * (padding + text_padding)
    )
    width = max(primary_width, secondary_width)
    height = 2.0 * padding + primary_text_height + secondary_line_height
    return {
        **primary,
        "width_mm": width,
        "height_mm": max(4.0, height),
        "secondary": secondary,
        "secondary_text": str(secondary_text),
        "primary_text_height_mm": primary_text_height,
        "secondary_line_height_mm": secondary_line_height,
        "primary_width_mm": primary_width,
        "secondary_width_mm": max(10.0, secondary_width),
    }


def card_secondary_row(placement, card_width, card_height, geometry):
    """第二行底纹条的矩形 ``(x, y, 宽, 高)``（毫米）；没有第二行时返回 ``None``。

    条带**横跨整张卡**（左边缘到卡片右边缘），因此"第二行是导航 service tag"在视觉上
    一目了然；主卡外框仍由起终点色（起点绿 / 终点红）绘制。
    """

    line_height = geometry.get("secondary_line_height_mm")
    if not line_height:
        return None
    padding = _finite_number(geometry.get("padding_mm"), 1.0)
    top = (
        float(placement["y"]) + padding
        + float(geometry.get("primary_text_height_mm") or 0.0)
    )
    return (
        float(placement["x"]), top, float(card_width), float(line_height),
    )


def label_card_segments(services):
    """标签卡的**分块配色**（纯计算、可单测）。

    单业务 → ``[(family, 0.0, 1.0)]``（整块一色）；
    多业务 → 按 :data:`CNS_LABEL_CARD_ORDER`（通信 → RID → Radar → 导航）**等分**，
    例如 ``C + RID`` 就是左半浅绿 + 右半浅橙，``C + RID + Radar`` 就是三等分。

    颜色顺序与分块比例都是确定性的：同一组服务在任何图上都得到同一套分块。
    """

    families = [
        str(family) for family in (
            services if isinstance(services, (list, tuple, set)) else []
        ) if str(family) in CNS_LABEL_CARD_COLORS
    ]
    ordered = [family for family in CNS_LABEL_CARD_ORDER if family in set(families)]
    if not ordered:
        return []
    count = len(ordered)
    return [
        (family, index / float(count), (index + 1) / float(count))
        for index, family in enumerate(ordered)
    ]


def label_card_box(placement, card_width, card_height, collision_gap):
    """标签卡的避让矩形（**背景几何参与 collision**，与文字框不是同一个矩形）。

    背景卡比文字框更大（含 padding），因此避让必须用卡片矩形；否则会出现
    "文字不重叠、卡片互相压住"的视觉重叠。

    矩形 = 卡片本身 + ``collision_gap`` + :data:`LAYOUT['label_card_safety_mm']`
    安全余量。安全余量吸收"文字估算宽度与实际渲染宽度"的少量差异 ——
    没有它时，两张卡片可能只差零点几毫米就贴在一起，肉眼看起来就是叠字。
    """

    safety = _finite_number(LAYOUT.get("label_card_safety_mm"), 0.6)
    pad = float(collision_gap) + safety
    return (
        float(placement["x"]) - pad,
        float(placement["y"]) - pad,
        float(placement["x"]) + float(card_width) + pad,
        float(placement["y"]) + float(card_height) + pad,
    )


def _finite_number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _build_labels(layout, spec, map_extent, font_family, context=None, leader_log=None):
    """在地图上叠加中文标注（**正式标签背景卡** + 深色文字 + 可解释偏移）。

    实现方式（全部是正式 QGIS 版面项，绝无 PNG 后处理）：

    1. ``QgsLayoutItemShape`` 画标签卡的浅色底（多业务时按服务数等分成多块）；
    2. ``QgsLayoutItemLabel`` 在其上叠深色文字（另带同文本的浅色偏移拷贝作为 halo）。

    版式收口：

    * 起点 / 终点沿航路**外法向**向外偏移，偏移量大于星标半径，标签不压星标；
    * 标签卡尺寸由文本长度 + 字号 + 背景 padding 计算（见 :func:`label_card_size`）；
    * 卡片在锚点外侧时改用右对齐，标签始终**向外**展开（既不压标记也不出图）；
    * 转弯点 / 地名保持次级视觉优先级。

    避让规则（用户明确要求）：

    1. **collision suppression**：按 ``priority`` 从高到低放置，与已放置的**标签卡矩形**
       相交的候选被抑制（不移动站点坐标、不改名字）；起终点先尝试顺移、找不到位置才抑制；
    2. **selected CNS proposal 优先于普通标注**：被低优先级标签压住时，先撤掉那个
       低优先级标签，而不是把提案标签丢掉；
    3. **边界避让**：任何标签都被夹进地图框内，绝不跑出图框；
    4. 地图内部**不再为说明框预留区域**（Round30-B1 已把说明框移出地图主体）；
    5. **引线预算（Round30-B1.2）**：卡片离锚点太远、会画出超过
       :func:`leader_length_budget` 的引线时，**先给标签换一个靠近锚点的位置**；
       换不到位置时宁可**不画引线**，绝不拉出几十毫米的大 L 形折线。
       ``leader_log``（可选）把真正画出的引线几何记进调用方的列表，供真机量化测试读取。
    """

    if not spec.labels or map_extent is None or map_extent.isEmpty():
        return []
    plan = spec.layout or {}
    frame = {
        "x": float(plan["map_left_mm"]), "y": float(plan["map_top_mm"]),
        "width": float(plan["map_width_mm"]), "height": float(plan["map_height_mm"]),
    }
    x_span = float(map_extent.width())
    y_span = float(map_extent.height())
    if x_span <= 0 or y_span <= 0:
        return []
    context = context or render_context(spec)
    collision_gap = _finite_number(LAYOUT.get("label_collision_gap_mm"), 0.9)
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
    plotted = []
    created = []
    for label in ordered:
        style_item = _label_style_for(label)
        if style_item is None:
            continue
        projected = projected_point(context, label.longitude, label.latitude)
        if projected is None:
            continue
        point_x, point_y = projected
        fx = (point_x - map_extent.xMinimum()) / x_span
        fy = (map_extent.yMaximum() - point_y) / y_span
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            continue
        anchor_x = frame["x"] + fx * frame["width"]
        anchor_y = frame["y"] + fy * frame["height"]
        secondary_text = str(getattr(label, "secondary_text", "") or "")
        geometry = label_card_geometry_with_secondary(
            label.text, style_item, secondary_text,
        )
        card_width = geometry["width_mm"]
        card_height = geometry["height_mm"]
        clearance = _finite_number(
            LAYOUT.get("label_point_clearance_mm"), 1.8,
        ) + _finite_number(style_item.get("card_padding_mm"), 1.4)
        if label.kind == "place":
            # 地名的点位由源数据字段给出，不能当作站点符号，因此不额外留净空。
            clearance = 1.0
        placement = _label_placement(
            label, style_item, directions.get(label.kind), anchor_x, anchor_y,
            frame, card_width, card_height, clearance,
            line_count=int(geometry["line_count"]),
        )
        placement = _clamp_placement(placement, frame, card_width, card_height)
        box = label_card_box(placement, card_width, card_height, collision_gap)
        if _collides(box, plotted) and label.kind not in ("start", "end"):
            # **不叠字**：宁可把标签挪一点，也不让两张卡片压在一起。
            # 顺移只动卡片本身，绝不改站点坐标，也绝不动起终点。
            shifted = _reposition_placement(
                placement, anchor_y, frame, card_width, card_height, collision_gap, plotted,
            )
            if shifted is not None:
                placement = shifted
                box = label_card_box(placement, card_width, card_height, collision_gap)
        overwrite = _overwrite_conflicts(label.kind, box, plotted)
        keep = label.kind in ("start", "end") or bool(overwrite) or not _collides(box, plotted)
        if not keep:
            continue
        for dropped in overwrite:
            plotted.remove(dropped)
            for item in dropped.get("created") or []:
                layout.removeLayoutItem(item)
        # Round30-B1.2：引线**不再允许超长**。若当前落点会画出超过
        # ``leader_line_max_length_mm`` 的引线（例如原来那种几十毫米的大 L 形折线），
        # 就为这张卡换一个**靠近锚点**的位置，而不是继续把引线拉长。
        placement = _placement_within_leader_budget(
            label, style_item, placement, frame, anchor_x, anchor_y,
            card_width, card_height, clearance, collision_gap, plotted,
        )
        box = label_card_box(placement, card_width, card_height, collision_gap)
        entry = {
            "box": box, "kind": label.kind, "created": [],
            "anchor": (anchor_x, anchor_y), "geometry": geometry, "style": style_item,
        }
        leader_item = _leader_line_item(
            layout, label, style_item, placement, card_width, card_height,
            anchor_x, anchor_y, obstacles=[entry["box"] for entry in plotted],
            leader_log=leader_log,
        )
        if leader_item is not None:
            entry["created"].append(leader_item)
        text_items, shapes = _label_card_items(
            layout, label, style_item, placement, card_width, card_height,
            font_family, x_mm=anchor_x, y_mm=anchor_y, geometry=geometry,
        )
        entry["created"].extend(shapes)
        entry["created"].extend(text_items)
        plotted.append(entry)
    created.extend(item for entry in plotted for item in entry["created"])
    return created


def _placement_within_leader_budget(label, style_item, placement, frame,
                                    anchor_x, anchor_y, card_width, card_height,
                                    clearance, collision_gap, plotted):
    """把标签卡换到**引线长度不超预算**的落点（Round30-B1.2 的核心约束）。

    预算 = :data:`LAYOUT['leader_line_max_length_mm']`（15 mm）− 引线起点的 marker 净空
    （:data:`LAYOUT['leader_line_endpoint_gap_mm']`）。因此"引线长度"就是卡片最近边到
    锚点的**直线距离**，纯几何、可判定。

    流程：

    1. 卡片样式不带引线（地名 / 转弯点 / 普通标注）→ 不参与预算搜索，原样返回；
    2. 当前落点本身就在预算内 → 原样返回（**绝不无谓地挪动已排好的卡片**）；
    3. 否则按"离锚点由近到远"枚举候选落点（先换到锚点的另一侧，再按固定步长向外
       逐档展开），跳过越界、与已放置卡片相撞、或**引线仍然超预算**的候选；
    4. 仍然找不到 → 回退到"引线最短"的候选（宁可卡片压在已放置卡片附近，也不画长引线）；
    5. 一个候选都没有 → 保留原落点，由 :func:`leader_line_geometry` 的预算判定决定
       是否画引线（超预算时它返回 ``None``，即**宁可不画，也不画长引线**）。

    候选只移动卡片、绝不改站点坐标；起终点卡也会被移动（"标签应优先靠近锚点重新布置"
    优先于"拉长引线"）。
    """

    if not style_item.get("leader"):
        return placement
    budget = leader_length_budget()
    if leader_path_length_mm(placement, card_width, card_height, anchor_x, anchor_y) \
            <= budget:
        return placement
    # **不压标记**：候选卡与锚点的净空不得小于全局最小净空（候选 x 可能被边界夹回锚点
    # 正上方，此时卡片会盖住星标 —— 这种候选必须直接淘汰）。
    minimum_clearance = _finite_number(LAYOUT.get("label_point_clearance_mm"), 1.8)
    candidates = _leader_budget_candidates(
        placement, frame, anchor_x, anchor_y, card_width, card_height, clearance,
    )
    best = None
    best_length = None
    for candidate in candidates:
        if label_anchor_distance_mm(
            candidate, card_width, card_height, anchor_x, anchor_y,
        ) < minimum_clearance:
            continue
        if _collides(
            label_card_box(candidate, card_width, card_height, collision_gap), plotted,
        ):
            continue
        length = leader_path_length_mm(
            candidate, card_width, card_height, anchor_x, anchor_y,
        )
        if best_length is None or length < best_length:
            best, best_length = candidate, length
        if length <= budget:
            return candidate
    return best if best is not None else placement


def _leader_budget_candidates(placement, frame, anchor_x, anchor_y, card_width,
                              card_height, clearance):
    """引线预算搜索的候选落点（**按离锚点由近到远**排序，毫米纯计算）。

    先枚举四个基本方位（左右优先于上下：水平方向的引线在人眼看来最短最干净），
    再按 :data:`LAYOUT['label_reposition_step_mm']` 的步长逐档向外展开。
    """

    from qgis.PyQt.QtCore import Qt

    step = max(1.0, _finite_number(LAYOUT.get("label_reposition_step_mm"), 2.4))
    limit = max(4, int(_finite_number(LAYOUT.get("label_reposition_limit"), 12)))
    offsets = [
        # 左右：卡片整体移到锚点侧面（垂直方向留出 marker 净空）。
        (anchor_x + clearance + 0.4, anchor_y + clearance + 0.4),
        (anchor_x - clearance - 0.4 - card_width, anchor_y + clearance + 0.4),
        # 上下。
        (anchor_x, anchor_y + clearance + 0.4),
        (anchor_x, anchor_y - clearance - 0.4 - card_height),
    ]
    for index in range(1, limit + 1):
        for sign in (-1.0, 1.0):
            offsets.append((anchor_x + sign * step * index, anchor_y + clearance + 0.4))
            offsets.append((anchor_x, anchor_y + sign * step * index))
    seen = set()
    candidates = []
    for offset_x, offset_y in offsets:
        align = Qt.AlignLeft if offset_x >= anchor_x else Qt.AlignRight
        candidate = {
            **placement,
            "x": float(offset_x), "y": float(offset_y), "align": align,
        }
        clamped = _clamp_placement(candidate, frame, card_width, card_height)
        key = (round(float(clamped["x"]), 3), round(float(clamped["y"]), 3))
        if key in seen:
            continue
        seen.add(key)
        clamped["_leader_length_mm"] = leader_path_length_mm(
            clamped, card_width, card_height, anchor_x, anchor_y,
        )
        candidates.append(clamped)
    candidates.sort(key=lambda item: item["_leader_length_mm"])
    for candidate in candidates:
        candidate.pop("_leader_length_mm", None)
    return candidates


def _entry_items(entry):
    return list(entry.get("created") or [])


def _label_style_for(label):
    """按标注种类取样式：终点用红色边框版本，其余用 ``style_key``。"""

    key = ENDPOINT_STYLE_KEYS.get(str(label.kind)) if label.kind in ENDPOINT_STYLE_KEYS \
        else label.style_key
    style_item = _label_style_safe(key)
    if style_item is None:
        style_item = _label_style_safe(label.style_key)
    return style_item


#: 允许"垂直顺移避让"的标注种类（起终点绝不移动，因此不在表内）。
_SHIFTABLE_LABEL_KINDS = ("place", "turn", "cns_proposal")


def reposition_label_kinds():
    """可以被垂直顺移的标注种类（供测试断言"起终点绝不移动"）。"""

    return tuple(_SHIFTABLE_LABEL_KINDS)


def _reposition_placement(placement, anchor_y, frame, card_width, card_height,
                          collision_gap, plotted):
    """把被挡住的标签沿**垂直方向**挪到最近的空位（上下交替试探）。

    为什么需要它：同址多业务（例如某站同时有通信与 RID 提案）的两张卡片会落在同一条
    水平带上。没有这一步时，高优先级标签会把低优先级标签整条压掉 —— 用户看到的是
    "RID 提案不见了"，而不是"标签挪开了"。

    规则：

    * 只动卡片本身，**绝不改动站点坐标**；
    * 上、下两个方向交替、按固定档长试探，优先顺序是"就近"（先 1 档、再 2 档……）；
    * 候选必须落在图框内，且与已放置卡片（含安全余量）不相交；
    * 找不到空位时返回 ``None``，由调用方按优先级决定抑制。
    """

    step = max(1.0, _finite_number(LAYOUT.get("label_reposition_step_mm"), 2.4))
    limit = int(_finite_number(LAYOUT.get("label_reposition_limit"), 10))
    for index in range(1, limit + 1):
        for sign in (-1.0, 1.0):
            candidate = {
                **placement,
                "y": float(placement["y"]) + sign * step * index,
            }
            clamped = _clamp_placement(candidate, frame, card_width, card_height)
            if abs(float(clamped["y"]) - float(candidate["y"])) > 1e-9:
                continue
            # 挪动幅度不得小于安全净空（否则只是把重叠换了个位置）。
            if abs(float(clamped["y"]) - float(placement["y"])) < step * 0.5:
                continue
            if not _collides(
                label_card_box(clamped, card_width, card_height, collision_gap), plotted,
            ):
                return clamped
    return None


def _overwrite_conflicts(kind, box, plotted):
    """返回允许被 ``kind`` 反向抑制的低优先级标签条目（起终点与选中提案专用）。"""

    if kind not in ("start", "end", "cns_proposal"):
        return []
    conflicts = [entry for entry in plotted if _collides(box, [entry])]
    if not conflicts:
        return []
    if kind in ("start", "end"):
        return conflicts
    # selected CNS proposal：只撤掉**比它低**的普通标注，绝不丢掉别的提案。
    return [entry for entry in conflicts if entry["kind"] not in ("start", "end", "cns_proposal")]


def _collides(box, plotted):
    """标签卡矩形相交判定（轴对齐，毫米），``collision_gap`` 已包含在卡片矩形里。"""

    for entry in plotted or []:
        if _boxes_intersect(box, [entry["box"]]):
            return True
    return False


def _boxes_intersect(box, boxes):
    left, top, right, bottom = box
    for other in boxes:
        other_left, other_top, other_right, other_bottom = other
        if right <= other_left or left >= other_right:
            continue
        if bottom <= other_top or top >= other_bottom:
            continue
        return True
    return False


def _label_card_items(layout, label, style_item, placement, card_width, card_height,
                      font_family, *, x_mm, y_mm, geometry=None):
    """画一张标签卡：先画底色（可多块），再画**逐行**的深色文字。

    返回 ``(文字项列表, 形状项列表)``。文字按 :func:`wrap_label_text` 的结果分行，
    每行一个 ``QgsLayoutItemLabel``（行内水平居中），因此正式图不会出现省略号
    （除非超过 ``max_lines`` 的极端情况）。

    背景配色：

    * 服务标签（``label.services`` 非空）→ 按服务数**等分**分块着色；
    * 起终点 → 白底 + 服务色（起点绿 / 终点红）强边框，整块一色；
    * 其它（地名 / 转弯点）→ 不画背景卡，只画 halo 文字。
    """

    from qgis.core import QgsLayoutItemShape

    geometry = geometry or label_card_geometry(label.text, style_item)
    segments = _label_card_segments(label, style_item)
    shapes = []
    for (fill, border, border_width), start, end in segments:
        width = float(card_width) * (end - start)
        if width <= 0.05:
            continue
        shape = QgsLayoutItemShape(layout)
        shape.setShapeType(QgsLayoutItemShape.Rectangle)
        shape.setSymbol(_rectangle_symbol(fill, border, border_width))
        shape.attemptMove(_position(
            float(placement["x"]) + float(card_width) * start, float(placement["y"]),
        ))
        shape.attemptResize(_size(width, float(card_height)))
        layout.addLayoutItem(shape)
        shapes.append(shape)
    # Round30-B1.2：主卡第二行（同址 Navigation service tag）的浅黄底纹带。
    # 它画在整块卡底之上、文字之下，条带颜色取自 CNS_LABEL_CARD_COLORS["navigation"]。
    row = card_secondary_row(placement, card_width, card_height, geometry)
    if row is not None:
        band = QgsLayoutItemShape(layout)
        band.setShapeType(QgsLayoutItemShape.Rectangle)
        band.setSymbol(_rectangle_symbol(
            str(geometry["secondary"]["fill"]), str(geometry["secondary"]["fill"]), 0.0,
        ))
        band.attemptMove(_position(row[0], row[1]))
        band.attemptResize(_size(row[2], row[3]))
        layout.addLayoutItem(band)
        shapes.append(band)
    text_padding = _finite_number(
        LAYOUT.get("label_card_text_padding_mm"), 1.4,
    ) + _finite_number(style_item.get("card_padding_mm"), 1.4)
    line_height = float(geometry.get("line_height_mm") or card_height)
    lines = list(geometry.get("lines") or [label.text])
    text_width = float(card_width) - 2.0 * text_padding
    text_items = []
    for index, line in enumerate(lines):
        text_items.append(_label_item(
            layout, line,
            x_mm=float(placement["x"]) + text_padding,
            y_mm=float(placement["y"]) + float(
                geometry.get("padding_mm") or 0.0
            ) + index * line_height,
            font_family=font_family, style_item=style_item,
            box_width=text_width, box_height=line_height, align=placement["align"],
            anchor_x=x_mm, anchor_y=y_mm,
            draw_dot=(index == 0 and label.kind not in ("cns_proposal", "cns_existing")),
        ))
    secondary_text = str(getattr(label, "secondary_text", "") or "")
    if secondary_text and row is not None:
        secondary = geometry["secondary"]
        # 第二行是一枚 service tag：**单行**、不画锚点圆点、字号小于主名。
        text_items.append(_label_item(
            layout, secondary_text,
            x_mm=float(placement["x"]) + text_padding,
            y_mm=float(row[1]),
            font_family=font_family,
            style_item={
                **style_item,
                "color": str(secondary["text_color"]),
                "font_size": float(secondary["font_size"]),
                "bold": bool(secondary["bold"]),
            },
            box_width=text_width, box_height=float(row[3]),
            align=placement["align"], draw_dot=False,
        ))
    return text_items, shapes


def _label_card_segments(label, style_item):
    """标签卡底色的分块定义（纯计算、可单测）。

    返回 ``[((fill, border, border_width), start_ratio, end_ratio), ...]``，覆盖整张卡：

    * **起终点**（含合并了同址 Navigation 的起降点）→ 整块白底 + 起终点色强边框
      （起点绿 / 终点红）。第二行的浅黄底纹由 :func:`card_secondary_row` 单独叠加，
      因此**主卡外框永远是起终点色**；
    * **服务标签**（``label.services`` 非空）→ 按服务家族数**等分**分块着色。
    """

    families = [
        str(family) for family in (label.services or [])
        if str(family) in CNS_LABEL_CARD_COLORS
    ]
    ordered = [family for family in CNS_LABEL_CARD_ORDER if family in set(families)]
    if label.kind in ENDPOINT_STYLE_KEYS:
        # 起终点优先：服务家族只用来决定第二行的底纹，绝不替换主卡外框颜色。
        fill = str(style_item.get("card_fill") or "#ffffff")
        border = str(style_item.get("card_border") or "#12a150")
        width = float(style_item.get("card_border_width_mm")
                      or LAYOUT.get("label_card_border_width_mm", 0.5))
        return [((fill, border, width), 0.0, 1.0)]
    if not ordered:
        return []
    count = len(ordered)
    return [
        (
            (
                CNS_LABEL_CARD_COLORS[family]["fill"],
                CNS_LABEL_CARD_COLORS[family]["border"],
                float(CNS_LABEL_CARD_COLORS[family].get("border_width_mm")
                      or LAYOUT.get("label_card_border_width_mm", 0.5)),
            ),
            index / float(count),
            (index + 1) / float(count),
        )
        for index, family in enumerate(ordered)
    ]


# --------------------------------------------------------------------------- leader line

def label_anchor_distance_mm(placement, card_width, card_height, anchor_x, anchor_y):
    """卡片与锚点之间的**有效距离**（毫米）：水平净空与垂直净空之和。

    卡片把锚点完全罩住时距离为 0；只在某一轴方向上分开时取该轴的净空。
    """

    left, top = float(placement["x"]), float(placement["y"])
    right, bottom = left + float(card_width), top + float(card_height)
    dx = 0.0
    if anchor_x < left:
        dx = left - float(anchor_x)
    elif anchor_x > right:
        dx = float(anchor_x) - right
    dy = 0.0
    if anchor_y < top:
        dy = top - float(anchor_y)
    elif anchor_y > bottom:
        dy = float(anchor_y) - bottom
    return dx + dy


def leader_length_budget():
    """引线**允许的最大走线长度**（毫米，含起点 marker 净空）。

    Round30-B1.2 的硬约束：``LAYOUT['leader_line_max_length_mm']`` = 15 mm。卡片到锚点
    最近边的距离超过 ``预算 − leader_line_endpoint_gap_mm`` 时，调用方必须**为标签换
    位置**，而不是把引线继续拉长；:func:`leader_line_geometry` 在超过这个长度时直接
    返回 ``None``（宁可不画引线，也不出现几十毫米的大 L 形折线）。
    """

    return max(1.0, _finite_number(LAYOUT.get("leader_line_max_length_mm"), 15.0))


def leader_path_length_mm(placement, card_width, card_height, anchor_x, anchor_y):
    """引线的实际走线长度（毫米，纯几何）。

    候选走线都从 marker 外缘（沿指向卡片方向退开
    :data:`LAYOUT['leader_line_endpoint_gap_mm']`）画起，因此长度恒等于
    "锚点到卡片最近边的直线距离 + 起点净空"。它**不依赖** QGIS，可单测。
    """

    distance = label_anchor_distance_mm(
        placement, card_width, card_height, anchor_x, anchor_y,
    )
    gap = _finite_number(LAYOUT.get("leader_line_endpoint_gap_mm"), 1.4)
    return float(distance) + gap


def leader_line_geometry(placement, card_width, card_height, anchor_x, anchor_y,
                         obstacles=()):
    """leader line 的折线（毫米）；不需要画或超预算时返回 ``None``。

    Round30-B1.2 的走线规则（**短直线优先，其次短折线**）：

    * 距离 ≤ :data:`LAYOUT['leader_line_threshold_mm']`（4 mm）→ ``None``（不画）；
    * 走线长度 > :func:`leader_length_budget`（15 mm）→ ``None``：
      调用方（:func:`_build_labels`）会先把卡片换到锚点附近，换不到位置时**宁可不画**，
      也绝不出现原来那种几十毫米的大 L 形折线；
    * 否则从 **marker 外缘**（锚点沿指向卡片方向退开
      :data:`LAYOUT['leader_line_endpoint_gap_mm']`）连到**卡片最近边**的引线；
    * ``obstacles`` 是**必须避开的矩形**（已放置的其它标签卡）：直线被挡住时才改用
      **贴边的短折线**，因此引线不会压过正文文字；所有候选都被挡住时才退回最短直线。
    """

    distance = label_anchor_distance_mm(
        placement, card_width, card_height, anchor_x, anchor_y,
    )
    threshold = _finite_number(LAYOUT.get("leader_line_threshold_mm"), 4.0)
    if distance <= threshold:
        return None
    left, top = float(placement["x"]), float(placement["y"])
    right, bottom = left + float(card_width), top + float(card_height)
    gap = _finite_number(LAYOUT.get("leader_line_endpoint_gap_mm"), 1.4)
    minimum = _finite_number(LAYOUT.get("leader_line_min_length_mm"), 4.0)
    budget = leader_length_budget()
    if distance + gap > budget:
        return None

    # 卡片最近边上的落点：锚点在卡片下方/上方时接到水平边，否则接到竖直边。
    if float(anchor_y) > bottom:
        direct_end = (min(max(float(anchor_x), left), right), bottom)
    elif float(anchor_y) < top:
        direct_end = (min(max(float(anchor_x), left), right), top)
    elif float(anchor_x) <= left:
        direct_end = (left, min(max(float(anchor_y), top), bottom))
    elif float(anchor_x) >= right:
        direct_end = (right, min(max(float(anchor_y), top), bottom))
    else:  # 锚点被卡片罩住（理论上不会到这里，distance 会是 0）
        direct_end = (left, min(max(float(anchor_y), top), bottom))

    def build(points):
        """把候选折线里连续重复的点去掉，然后把起点退开 marker 外缘 ``gap``。"""

        # 去重：候选折线里可能因为锚点正好落在卡片边上而出现重复点。
        cleaned = []
        for point in points:
            if cleaned and math.dist(cleaned[-1], point) <= 1e-9:
                continue
            cleaned.append(point)
        if len(cleaned) < 2:
            return None
        # 卡片最近边上真正要连的那个点（**不是**锚点本身）。
        target_x, target_y = float(cleaned[1][0]), float(cleaned[1][1])
        dx = target_x - float(anchor_x)
        dy = target_y - float(anchor_y)
        norm = math.hypot(dx, dy)
        if norm > 1e-6:
            start = (
                float(anchor_x) + dx / norm * gap,
                float(anchor_y) + dy / norm * gap,
            )
        else:
            start = (target_x, target_y)
        routed = [start] + list(cleaned[1:])
        length = sum(math.dist(routed[index], routed[index + 1])
                     for index in range(len(routed) - 1))
        if length < minimum or length > budget:
            # 超预算的候选**直接丢弃**：这就是"短直线优先、其次短折线"的硬闸门。
            return None
        return {
            "points": routed,
            "start": routed[0],
            "end": routed[-1],
            "distance_mm": distance,
            "length_mm": length,
        }

    margin = _finite_number(LAYOUT.get("leader_line_obstacle_margin_mm"), 1.0)
    padded = [
        (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
        for box in (obstacles or ())
    ]

    def crosses(geometry):
        if not padded:
            return False
        points = geometry["points"]
        for index in range(len(points) - 1):
            for box in padded:
                if _segment_hits_box(points[index], points[index + 1], box):
                    return True
        return False

    # 候选走线（按"越短越优先"的顺序试探）：
    #   1) **直线**（首选：最短最干净）；
    #   2/3) 先横移到卡片外侧再竖直（贴着卡片边缘的**短折线**）；
    #   4..7) 先竖移到锚点上/下方的一个转折高度、再横移、最后接到卡片
    #         （只在直线与短折线都被挡住时才使用，且仍受 15 mm 预算限制）。
    elbow = _finite_number(LAYOUT.get("leader_line_elbow_mm"), 2.6)
    candidates = [[(float(anchor_x), anchor_y), direct_end]]
    # 折线拐角必须落在卡片**贴着锚点的那条水平边之外**：锚点在卡片上方时，拐角应在卡片
    # 顶边之上（``top - elbow``）；锚点在卡片下方时应在底边之下（``bottom + elbow``）。
    # （旧版把这两个方向写反了：锚点在卡片上方时会算出卡片底边 + elbow 的拐角，
    #   直线明明可用却画出一个先向下、再向左、最后向上折回的长折线。）
    corner_y = (top - elbow) if float(anchor_y) < top else (bottom + elbow)
    for corner_x in (left - elbow, right + elbow):
        candidates.append([
            (float(anchor_x), anchor_y), (corner_x, anchor_y), (corner_x, corner_y),
        ])
    for corner_x in (left - elbow, right + elbow):
        for lift in (6.0, 12.0, 20.0, 28.0):
            bend_y = (float(anchor_y) + lift) if float(anchor_y) > bottom \
                else (float(anchor_y) - lift)
            candidates.append([
                (float(anchor_x), anchor_y), (float(anchor_x), bend_y),
                (corner_x, bend_y), (corner_x, corner_y),
            ])
    for corner_x in (left - elbow, right + elbow):
        for lift in (6.0, 12.0, 20.0):
            bend_y = (float(anchor_y) + lift) if float(anchor_y) > bottom \
                else (float(anchor_y) - lift)
            candidates.append([
                (float(anchor_x), anchor_y), (float(anchor_x), bend_y),
                (direct_end[0], bend_y), direct_end,
            ])

    geometry = None
    for points in candidates:
        candidate = build(points)
        if candidate is None:
            continue
        if not crosses(candidate):
            return candidate
        if geometry is None:
            geometry = candidate
    return geometry


def _offset_from_anchor(point, anchor_x, anchor_y, gap):
    """把折线起点从锚点沿其方向退开 ``gap``（从 marker 外缘开始画）。"""

    dx = float(point[0]) - float(anchor_x)
    dy = float(point[1]) - float(anchor_y)
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return float(point[0]), float(point[1])
    return (float(anchor_x) + dx / length * gap, float(anchor_y) + dy / length * gap)


def _segment_hits_box(start, end, box):
    """线段是否与轴对齐矩形相交（含穿过、含端点落在框内）。

    Round30-B1.2：旧实现在这里把包含关系写反了（用"框角是否落在线段的包围盒里"来近似
    包含判定），结果是一条**从框外到框外、完全穿过矩形**的对角线会被判成"不相交"，
    引线于是真的压过了别的标签卡。现在改成标准的 **Liang–Barsky 裁剪**：
    只保留"与矩形内部真正有交"的线段，且端点内的判定是精确的。
    """

    left, top, right, bottom = (float(value) for value in box)
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    delta_x = x1 - x0
    delta_y = y1 - y0
    low, high = 0.0, 1.0
    for numerator, denominator in (
        (left - x0, delta_x), (x0 - right, -delta_x),
        (top - y0, delta_y), (y0 - bottom, -delta_y),
    ):
        if abs(denominator) < 1e-12:
            # 该方向上平行：只要不在边界内就完全不可能相交。
            if numerator > 0:
                return False
            continue
        ratio = numerator / denominator
        if denominator > 0:
            if ratio > high:
                return False
            low = max(low, ratio)
        else:
            if ratio < low:
                return False
            high = min(high, ratio)
    return low <= high


def _segments_intersect(p1, p2, p3, p4):
    """标准线段相交判定（含共线重叠的保守处理）。

    Round30-B1.2 起引线的避障改走 :func:`_segment_hits_box` 的 Liang–Barsky 裁剪，
    因此本函数当前**不再被引线路径调用**；它作为通用几何工具保留（同族判定在
    ``gis/planning_constraint_field_adapter`` 与 ``domain/regulatory_constraints``
    里仍在用同名实现）。
    """

    def orientation(a, b, c):
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(value) < 1e-9:
            return 0
        return 1 if value > 0 else -1

    def on_segment(a, b, c):
        return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
                and min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)

    o1 = orientation(p1, p2, p3)
    o2 = orientation(p1, p2, p4)
    o3 = orientation(p3, p4, p1)
    o4 = orientation(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_segment(p1, p2, p3):
        return True
    if o2 == 0 and on_segment(p1, p2, p4):
        return True
    if o3 == 0 and on_segment(p3, p4, p1):
        return True
    if o4 == 0 and on_segment(p3, p4, p2):
        return True
    return False


def leader_color(label, style_item):
    """leader line 的颜色：服务标签用服务主色，起终点用绿 / 红。"""

    if label.kind in ENDPOINT_LEADER_COLORS:
        return ENDPOINT_LEADER_COLORS[label.kind]
    segments = label_card_segments(list(label.services or []))
    if segments:
        family = segments[0][0]
        return str(CNS_LABEL_CARD_COLORS[family].get("leader")
                   or CNS_LABEL_CARD_COLORS[family]["border"])
    return str(style_item.get("card_border") or LAYOUT.get("footer_strip_color"))


def leader_pen_color(color):
    """引线画笔颜色：**业务色不变**，只按 ``leader_line_alpha`` 降低视觉权重。

    Round30-B1.2：引线只作归属提示，因此保持 hue（起终点绿 / 红、服务主色）不变，
    只把它调淡到 :data:`LAYOUT['leader_line_alpha']`（默认 0.82），配合 0.45 mm 线宽
    一起把引线的视觉重量压到正文标注之下。
    """

    from qgis.PyQt.QtGui import QColor

    pen = QColor(str(color or "#1c2733"))
    alpha = _finite_number(LAYOUT.get("leader_line_alpha"), 0.82)
    alpha = min(1.0, max(0.1, alpha))
    pen.setAlpha(int(round(alpha * 255)))
    return pen


def _leader_line_item(layout, label, style_item, placement, card_width, card_height,
                      anchor_x, anchor_y, obstacles=(), leader_log=None):
    """按需创建 leader line 版面项；不需要时返回 ``None``。

    实现方式：``QgsLayoutItem`` 的**自定义子类**（正式 QGIS 版面项，参与版面导出与
    z 序），而不是把线段画到版面之外。``QgsLayoutItemShape`` 在 3.44 上没有折线类型，
    因此这里用标准做法：继承 ``QgsLayoutItem`` 并实现 ``paint`` / ``draw``。

    ``leader_log``（可选）会把**真正画出来的**引线记进调用方的列表，供真机量化测试
    读取"实际引线长度 / 折点数"，而不是只读布局意图。
    """

    if not style_item.get("leader"):
        return None
    geometry = leader_line_geometry(
        placement, card_width, card_height, anchor_x, anchor_y, obstacles=obstacles,
    )
    if geometry is None:
        return None
    if leader_log is not None:
        leader_log.append({
            "kind": str(label.kind),
            "points": [tuple(point) for point in geometry["points"]],
            "length_mm": float(geometry["length_mm"]),
            "distance_mm": float(geometry["distance_mm"]),
            "vertex_count": len(geometry["points"]),
        })
    item = _leader_line_item_class()(layout)
    item.configure(
        geometry["points"],
        # 业务色不变，只按 ``leader_line_alpha`` 降低视觉权重（见 leader_pen_color）。
        leader_pen_color(leader_color(label, style_item)),
        float(LAYOUT.get("leader_line_width_mm", 0.45)),
    )
    layout.addLayoutItem(item)
    # 注意：**绝不能**把 leader 的 z 值设成负数。地图项（``QgsLayoutItemMap``）的 z 值为 0，
    # 更低的 z 会让整条引线被地图绘制覆盖（实测导出图里引线完全不可见）。
    # 保持默认 z=0，后加入的卡片（同样 z=0）自然压在引线之上。
    return item


#: 自定义 leader line 版面项的进程内缓存（避免每次渲染都重新构造类型）。
_LEADER_LINE_ITEM_CLASS = []


def _leader_line_item_class():
    """构造一次 ``QgsLayoutItem`` 子类：只负责把一条折线画到版面坐标系里。"""

    if _LEADER_LINE_ITEM_CLASS:
        return _LEADER_LINE_ITEM_CLASS[0]
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QPen
    from qgis.core import QgsLayoutItem

    class LeaderLineItem(QgsLayoutItem):  # type: ignore[misc]
        """一条 leader line（用**毫米版面坐标**给出折点，绘制时换算到本项局部坐标）。"""

        def __init__(self, layout):
            super().__init__(layout)
            self._points = []
            self._color = QColor("#1c2733")
            self._width_mm = 0.45

        def configure(self, points, color, width_mm):
            self._points = [(float(x), float(y)) for x, y in points]
            # ``color`` 允许是带 alpha 的 QColor（引线降权后就是），也兼容颜色字符串。
            self._color = color if isinstance(color, QColor) else QColor(str(color or "#1c2733"))
            self._width_mm = float(width_mm or 0.45)
            if self._points:
                xs = [point[0] for point in self._points]
                ys = [point[1] for point in self._points]
                self._origin = (min(xs), min(ys))
                self._span = (max(0.1, max(xs) - min(xs)),
                              max(0.1, max(ys) - min(ys)))
                self.attemptMove(_position(self._origin[0], self._origin[1]))
                self.attemptResize(_size(self._span[0], self._span[1]))
            self.update()

        def boundingRect(self):
            """必须给出非空包围盒，否则 QGIS 会跳过这个自定义版面项。"""

            from qgis.PyQt.QtCore import QRectF

            span_x, span_y = getattr(self, "_span", (1.0, 1.0))
            pad = max(0.6, self._width_mm)
            return QRectF(-pad, -pad, span_x + 2 * pad, span_y + 2 * pad)

        def _draw(self, painter):
            from qgis.PyQt.QtCore import QPointF

            if not self._points:
                return
            origin_x, origin_y = getattr(self, "_origin", self._points[0])
            pen = QPen(self._color)
            pen.setWidthF(max(0.2, self._width_mm))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            points = [QPointF(x - origin_x, y - origin_y) for x, y in self._points]
            for index in range(len(points) - 1):
                painter.drawLine(points[index], points[index + 1])

        # QGIS 3.34+ 的新钩子；老版本走 draw()。
        def paint(self, painter, item_style, options, widget=None):
            self._draw(painter)

        def draw(self, painter, item_style, options, widget=None):
            self._draw(painter)

    _LEADER_LINE_ITEM_CLASS.append(LeaderLineItem)
    return LeaderLineItem


def _clamp_placement(placement, frame, box_width, box_height):
    """把标签卡夹进地图框（边界避让）：宁可贴边，也不让标签跑出图框。"""

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


def _label_style_safe(style_key):
    try:
        return label_style(style_key)
    except KeyError:
        return None


def _endpoint_direction(spec, kind):
    """端点处的**向外**单位方向（经纬度空间）：起终点各自背离航路内部。

    经纬度平面上的"向外方向"只用于决定标签落在锚点的哪一侧（上下左右），
    因此不需要投影；偏移量本身在屏幕毫米空间里量取，与 CRS 无关。
    """

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


def _label_placement(label, style_item, direction, anchor_x, anchor_y, frame,
                     card_width, card_height, clearance, line_count=1):
    """由锚点、偏移与卡片尺寸算出标签卡的落点与对齐方式（纯几何，不画任何东西）。

    **卡片不压标记**：卡片离锚点最近的那条边至少留出 ``clearance``（星标半径 + 字体
    半高 + 卡片 padding 之后的统一净空），因此卡片永远不会盖住站点符号本身。

    落点在锚点的哪一侧由标签种类决定（这是"不叠字"的关键）：

    * 起终点 / 单行 CNS 站名：卡片在锚点**外侧（上下左右）**，绝不压星标与站点符号；
    * 多行 CNS 站名：卡片较高，贴在锚点**下方**（第一行离站点最近，视觉归属最清楚）；
    * 普通地名 / 转弯点：卡片在锚点**下方**，向下展开。
    """

    from qgis.PyQt.QtCore import Qt

    def outward(offset_x, offset_y):
        if offset_x < 0:
            # 卡片向锚点左侧展开：贴到图框左内沿时改为 AlignLeft（而不是被夹到框外
            # 再被"边界避让"推到图框外的无效位置 —— 起终点标签绝不裁剪）。
            candidate = anchor_x + offset_x - card_width
            if candidate < frame["x"] + 0.6:
                return {
                    "x": frame["x"] + 0.6, "y": anchor_y + offset_y,
                    "align": Qt.AlignLeft,
                }
            return {
                "x": candidate, "y": anchor_y + offset_y, "align": Qt.AlignRight,
            }
        return {
            "x": anchor_x + offset_x, "y": anchor_y + offset_y, "align": Qt.AlignLeft,
        }

    if label.kind in ("start", "end"):
        base = _finite_number(style_item.get("offset_mm"), 9.0)
        vx, vy = direction if direction else (1.0, 1.0)
        screen_x, screen_y = vx, -vy
        # 偏移方向过于水平时补一点垂直分量，避免标签与星标在同一水平线上。
        if abs(screen_y) < 0.35:
            screen_y = 0.35 if screen_y >= 0 else -0.35
        offset_x = screen_x * base
        offset_y = screen_y * base
        if screen_y < 0:
            # 卡片在锚点上方：底边离锚点至少 clearance。
            offset_y = min(offset_y, -(clearance + card_height))
        else:
            # 卡片在锚点下方：顶边离锚点至少 clearance，绝不压星标。
            offset_y = max(offset_y, clearance + 0.4)
        return outward(offset_x, offset_y)
    if label.kind == "turn":
        base = _finite_number(style_item.get("offset_mm"), 4.2)
        return {
            "x": anchor_x + base, "y": anchor_y + clearance + 0.4,
            "align": Qt.AlignLeft,
        }
    if label.kind in ("cns_proposal", "cns_existing"):
        # CNS 站名：**单行**卡片贴在锚点上方（不压站点符号）；
        # **多行**卡片明显更高，贴在下方才能保证"第一行正好落在站点附近"，
        # 视觉上仍然是这张卡在标注这个站点。
        if int(line_count) <= 1:
            return {
                "x": anchor_x, "y": anchor_y - clearance - card_height,
                "align": Qt.AlignLeft,
            }
        return {
            "x": anchor_x, "y": anchor_y + clearance + 0.4, "align": Qt.AlignLeft,
        }
    # 普通地名：卡片贴在锚点**下方**（向下展开），同样留出净空。
    return {
        "x": anchor_x, "y": anchor_y + clearance + 0.4,
        "align": Qt.AlignLeft,
    }


def _label_item(layout, text, *, x_mm, y_mm, font_family, style_item,
                box_width=None, box_height=None, align=None,
                anchor_x=None, anchor_y=None, draw_dot=True):
    """一个标注文本（含白色 halo；可选锚点圆点）。

    ``box_width`` / ``box_height`` 由 :func:`label_card_size` 给出（不再是固定常量）。
    锚点圆点画在**真实锚点**上（不是文本框角上），因此卡片尺寸变化不会让圆点漂移；
    CNS 提案标签不画圆点，避免与"规划提案"符号本身重复标记。
    """

    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.core import QgsLayoutItemLabel

    width = float(box_width if box_width is not None else 24.0)
    height = float(box_height if box_height is not None else 5.0)
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
        item.setVAlign(Qt.AlignVCenter)
        item.attemptMove(_position(x_mm + dx, y_mm + dy))
        item.attemptResize(_size(width, height))
        layout.addLayoutItem(item)
        return item

    for dx, dy in ((-0.22, 0.0), (0.22, 0.0), (0.0, -0.22), (0.0, 0.22)):
        make(text, style_item["halo_color"], dx, dy, float(style_item["font_size"]))
    item = make(text, style_item["color"], 0.0, 0.0, float(style_item["font_size"]))
    if draw_dot and anchor_x is not None and anchor_y is not None:
        # 定位点：真实锚点处的小圆点（不画引线，避免遮挡主航路）。
        dot = make("●", "#1c2733", 0.0, 0.0, 3.4)
        dot.attemptResize(_size(3.2, 3.2))
        dot.attemptMove(_position(float(anchor_x) - 1.6, float(anchor_y) - 1.6))
        dot.setHAlign(Qt.AlignHCenter)
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
    # 标题独占一行，并**横跨整个图例框内容宽度、水平居中**：
    # 这才是"图　例"位于图例框顶部正中间，而不是某一个小文本框的左上角。
    # 因此宽度取 `width - 2 × side_padding`（左右内边距严格对称）。
    _add_label(
        layout, "图　例", x_mm=left + side_padding, y_mm=top + 0.2,
        width_mm=max(10.0, width - 2.0 * side_padding),
        height_mm=title_height - 0.4,
        font_family=font_family,
        font_size=float(LAYOUT["legend_title_font_size"]), color="#12233a",
        bold=True, align=Qt.AlignHCenter, valign=Qt.AlignVCenter,
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
    item_font_size = float(LAYOUT["legend_item_font_size"])
    # 逐条把**条目文本**收口到本列可用宽度内：正式图绝不允许文字越过列边界。
    layer_of = {entry["full_text"]: entry["layer_key"] for entry in entries}
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
        layer_key = layer_of.get(text, "")
        display_text, _shortened = legend_display_text(
            layer_key, text, text_width, item_font_size,
        )
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
            layout, display_text, x_mm=x + symbol_box + gutter, y_mm=y,
            width_mm=text_width, height_mm=row_height,
            font_family=font_family, font_size=item_font_size,
            color=LAYOUT["legend_item_text_color"], valign=Qt.AlignVCenter,
        )
    return background


def _legend_entries(spec):
    """图例条目（已按语义顺序排好），只包含真实存在的图层。

    每条同时给出 ``text``（最终显示文本）与 ``full_text``（完整名称，保留给 metadata 与
    审计）。显示文本只在**估算宽度超过该列可用宽度**时才按
    :data:`LEGEND_COMPACT_LABELS` 收口或逐字符截断 ——
    这样"文字跨越列"这种正式图不允许的缺陷不会因为字号放大而出现。
    """

    names = {layer.layer_key: layer.display_name for layer in spec.layers}
    entries = []
    for item in spec.legend_items:
        full = str(
            item.display_name or names.get(item.layer_key)
            or LAYER_DISPLAY_NAMES.get(item.layer_key, item.layer_key)
        )
        entries.append({
            "layer_key": item.layer_key,
            "style_key": item.style_key,
            "text": full,
            "full_text": full,
            "group": item.legend_group,
        })
    return entries


#: 图例条目的**收口候选**：当完整名称在当前字号下放不进一列时，按顺序尝试更短的写法。
#:
#: 完整名称不会丢失：它仍在 ``FigureSpec.metadata`` 的图层 ``display_name``、
#: ``legend_items`` 与导出报告里。这里只影响**图例那一格**的可读性（宁可短一点，
#: 也不允许文字越过列边界）。
LEGEND_COMPACT_LABELS = {
    "cns_comm_proposal": ("通信规划提案（未确认）", "通信提案（未确认）"),
    "cns_rid_proposal": ("RID 规划提案（未确认）", "RID 提案（未确认）"),
    "cns_nav_proposal": ("导航完整性监测点提案（未确认）", "导航监测点提案"),
    "cns_coverage_comm": ("通信规划服务半径 4 km", "通信半径 4 km"),
    "cns_coverage_rid_land": ("RID 陆上/沿海规划半径 2 km", "RID 陆上沿海 2 km"),
    "cns_coverage_rid_sea": ("RID 海上最大规划半径 5 km", "RID 海上最大 5 km"),
    "cns_radar_context": ("Radar-I 评估候选站址（未选中）", "Radar 候选站址（未选中）"),
    "cns_radar_limitation": (
        "非合作监视能力限制（无可行布设）", "非合作监视能力限制",
        "非合作监视限制",
    ),
    "terrain_obstacle": ("地形障碍（≥ 显示阈值）", "地形障碍"),
    "building_obstacle": ("建筑障碍（≥ 显示阈值）", "建筑障碍"),
}


def legend_display_text(layer_key, full_text, available_mm, font_size_pt):
    """把图例条目文本收口到 ``available_mm`` 之内（纯计算、可单测）。

    顺序：

    1. 完整名称本身就放得下 → 原样返回；
    2. 按 :data:`LEGEND_COMPACT_LABELS` 的候选顺序找第一个放得下的写法；
    3. 仍然放不下 → 在**词边界**（空格 / ``/`` / 全角括号前）逐段去掉尾部，
       最后手段才逐字符加省略号。

    返回 ``(text, shortened)``。
    """

    size = _finite_number(font_size_pt, 10.5)
    limit = max(6.0, _finite_number(available_mm, 40.0))
    full = str(full_text or "")
    if label_text_width_mm(full, size) <= limit:
        return full, False
    for candidate in LEGEND_COMPACT_LABELS.get(str(layer_key), ()):
        if label_text_width_mm(candidate, size) <= limit:
            return candidate, True
    trimmed = _trim_tail_segments(full, limit, size)
    if label_text_width_mm(trimmed, size) <= limit:
        return trimmed, True
    text = trimmed
    while text and label_text_width_mm(text + "…", size) > limit:
        text = text[:-1]
    return (text + "…") if text else full, True


def _trim_tail_segments(text, limit, size):
    """按词边界从尾部逐段去掉修饰语（``（…）`` / ``/…`` / 空格后的片段）。"""

    import re

    pieces = [piece for piece in re.split(r"(?=[（/])|(?<= )", str(text or "")) if piece]
    while len(pieces) > 1:
        pieces = pieces[:-1]
        candidate = "".join(pieces).strip()
        if candidate and label_text_width_mm(candidate, size) <= limit:
            return candidate
    return str(text or "").strip()


def _rectangle_symbol(fill, border, width_mm):
    from qgis.core import QgsFillSymbol, QgsSimpleFillSymbolLayer

    layer = QgsSimpleFillSymbolLayer()
    layer.setColor(_color(fill))
    layer.setStrokeColor(_color(border))
    layer.setStrokeWidth(float(width_mm))
    symbol = QgsFillSymbol()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def _polyline_symbol(color, width_mm):
    """leader line 的线符号（无填充、圆头、指定宽度）。"""

    from qgis.core import QgsLineSymbol, QgsSimpleLineSymbolLayer

    layer = QgsSimpleLineSymbolLayer(_color(color), float(width_mm))
    layer.setPenCapStyle(_cap_style("round"))
    symbol = QgsLineSymbol()
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
    "DEFAULT_MAP_CRS", "FigureRenderError", "GRID_CRS_AUTHID", "QgisFigureRenderer",
    "WGS84_AUTHID", "annotation_box_geometry", "build_layers", "card_secondary_row",
    "choose_font_family", "ensure_fonts_registered", "estimate_wrapped_lines",
    "label_card_box", "label_card_geometry_with_secondary", "label_card_segments",
    "label_card_size", "label_secondary_style", "label_text_width_mm",
    "leader_length_budget", "leader_path_length_mm", "north_arrow_path",
    "projected_extent", "projected_point", "render_context", "scale_bar_geometry",
    "spec_subtitle",
]
