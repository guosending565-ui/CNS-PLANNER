"""专题制图的样式目录（**地图与图例共用的唯一符号定义**）。

本模块是 ``route_overview_v1`` 视觉语言的单一定义处：

* 地图上的图层通过 :func:`fill_symbol` / :func:`line_symbol` / :func:`marker_symbol`
  取到 QgsSymbol；
* 图例中的条目通过 :func:`symbol_preview_image` 用**同一个** QgsSymbol 渲染成图片。

因此"图例符号必须与地图完全一致"不是靠人眼对齐，而是同一份定义的两次渲染。

视觉优先级（本轮的固定约束）：
    海域 / 陆地 → 空域 → 障碍物 → 既有铁塔 / 铁塔障碍 → 机场 → **规划航路** → 起终点。
    规划航路与起终点永远在最上层，绝不被障碍物图层覆盖。
"""

from __future__ import annotations

from copy import deepcopy

#: 图层键 → 样式定义。``kind`` 决定渲染方式，``geometry`` 决定图层几何类型。
#: ``z`` 决定视觉层级（渲染器按 z **降序**传给 QGIS，即列表首个在最上层）：
#: 海域 → 陆地 → 地形障碍 → 建筑障碍 → 机场保护范围 → 既有铁塔 → 铁塔障碍
#: → 规划航路 → 航路转弯点 → 起点/终点。规划航路与起终点永远压在障碍面之上。
FIGURE_STYLES = {
    "sea": {
        "kind": "fill", "geometry": "polygon", "z": 1,
        "fill": "#cfe6f5", "outline": "#a9c8dd", "outline_width": 0.0,
    },
    "land": {
        "kind": "fill", "geometry": "polygon", "z": 2,
        "fill": "#ffffff", "outline": "#4c5a68", "outline_width": 0.35,
    },
    "airspace": {
        "kind": "fill", "geometry": "polygon", "z": 3,
        "fill": "#9fd9a3", "outline": "#4c9c58", "outline_width": 0.25, "opacity": 0.38,
    },
    "terrain_obstacle": {
        "kind": "fill", "geometry": "polygon", "z": 4,
        "fill": "#ff9412", "outline": "#b25f00", "outline_width": 0.25, "opacity": 0.85,
    },
    "building_obstacle": {
        "kind": "fill", "geometry": "polygon", "z": 5,
        "fill": "#e02020", "outline": "#8c1010", "outline_width": 0.25, "opacity": 0.9,
    },
    "airport_protection": {
        "kind": "fill", "geometry": "polygon", "z": 6,
        "fill": "transparent", "outline": "#ff7a00",
        "outline_width": 0.5, "outline_style": "dash",
    },
    "tower_existing": {
        "kind": "marker", "geometry": "point", "z": 8,
        "shape": "triangle", "size": 2.8, "fill": "#ffffff",
        "outline": "#7b2fbe", "outline_width": 0.5, "angle": 0.0,
    },
    "tower_obstacle": {
        "kind": "marker", "geometry": "point", "z": 9,
        "shape": "triangle", "size": 3.2, "fill": "#7b2fbe",
        "outline": "#4b1a75", "outline_width": 0.35, "angle": 0.0,
    },
    "airport": {
        "kind": "marker", "geometry": "point", "z": 10,
        "shape": "square", "size": 3.4, "fill": "#ff7a00",
        "outline": "#8f3f00", "outline_width": 0.4, "angle": 45.0,
    },
    "planned_route": {
        "kind": "line", "geometry": "line", "z": 20,
        "color": "#123a6b", "width": 1.5, "capstyle": "round", "joinstyle": "round",
    },
    "turn_point": {
        "kind": "marker", "geometry": "point", "z": 21,
        "shape": "circle", "size": 2.4, "fill": "#ffffff",
        "outline": "#123a6b", "outline_width": 0.45,
    },
    "start_point": {
        "kind": "marker", "geometry": "point", "z": 22,
        "shape": "star", "size": 6.4, "fill": "#12a150",
        "outline": "#084d26", "outline_width": 0.4,
    },
    "end_point": {
        "kind": "marker", "geometry": "point", "z": 23,
        "shape": "star", "size": 6.4, "fill": "#d81b1b",
        "outline": "#7a0d0d", "outline_width": 0.4,
    },
}

#: 标注样式（中文 + 白色 halo）。优先级数值越大越优先显示。
LABEL_STYLES = {
    "label_endpoint": {
        "font_size": 8.0, "color": "#12233a", "halo_color": "#ffffff",
        "halo_size": 1.4, "bold": True, "offset_mm": 2.2,
    },
    "label_turn": {
        "font_size": 6.5, "color": "#123a6b", "halo_color": "#ffffff",
        "halo_size": 1.1, "bold": False, "offset_mm": 1.8,
    },
    "label_place": {
        "font_size": 7.0, "color": "#55606b", "halo_color": "#ffffff",
        "halo_size": 1.0, "bold": False, "offset_mm": 1.4,
    },
}

#: 版面常量（毫米）。与模板参数互补：这里是"固定版式"，不是业务参数。
#: A4 竖版 210×297：标题带 → 地图 →（薄审计条）→ 图例框（目标占比见 _layout_plan）。
LAYOUT = {
    "page_margin_mm": 14.0,
    # 标题带 = 主标题 + 固定间距 + 副标题 + 固定间距（副标题绝不贴地图上边框）。
    "title_band_mm": 17.0,
    "title_gap_mm": 1.4,
    "title_map_gap_mm": 3.0,
    "subtitle_band_mm": 4.0,
    "footer_band_mm": 7.0,
    # 地图框下沿的三段式薄审计条（坐标系 / revision / 未显示图层）。
    "footer_strip_mm": 3.4,
    "footer_map_gap_mm": 1.6,
    "footer_legend_gap_mm": 2.6,
    # 兼容键：地图与图例之间的总间距 = 上面三段之和（_layout_plan 会显式给出三段）。
    "map_legend_gap_mm": 7.6,
    "footer_strip_font_size": 4.6,
    "footer_strip_color": "#8a94a0",
    "map_frame_color": "#2b3a4a",
    "map_frame_width_mm": 0.4,
    "map_background": "#cfe6f5",
    "map_title_font_size": 11.0,
    "map_title_color": "#12233a",
    "subtitle_font_size": 7.0,
    "subtitle_color": "#4a5560",
    "legend_box_fill": "#ffffff",
    "legend_box_border": "#2b3a4a",
    "legend_box_width_mm": 0.35,
    # 图例：标题独占一行（位于框内左上），标题与内容之间不留大块空白。
    "legend_title_font_size": 8.5,
    "legend_group_font_size": 7.0,
    "legend_item_font_size": 6.8,
    "legend_item_text_color": "#1c2733",
    "legend_group_text_color": "#38424d",
    # 统一二维网格：符号框固定宽度、文本固定起始 x、行高统一。
    "legend_symbol_box_mm": 5.0,
    "legend_text_gutter_mm": 1.6,
    "legend_header_mm": 5.6,
    "legend_top_padding_mm": 1.6,
    "legend_row_mm": 4.4,
    "legend_group_row_mm": 4.2,
    # 组标题 → 组内条目：小间距；组与组之间：更大但统一的间距。
    "legend_group_item_gap_mm": 0.5,
    "legend_group_gap_mm": 2.2,
    "legend_column_gap_mm": 6.0,
    "legend_side_padding_mm": 4.5,
    "legend_min_row_mm": 3.9,
    "legend_min_group_row_mm": 3.9,
    "legend_default_columns": 2,
    "max_legend_columns": 3,
    "grid_interval_deg": 0.1,
    "grid_font_size": 5.6,
    "grid_color": "#3b4652",
    "grid_line_color": "#c3ccd4",
    "grid_frame_color": "#8593a1",
    # 比例尺（程序化绘制的黑白分段条）：与左边框 / 下边框保持合理内距，
    # 数字与单位位于条上方且留有固定间隙，"20 km" 绝不贴条。
    "scale_bar_margin_mm": 5.4,
    "scalebar_font_size": 6.0,
    "scalebar_height_mm": 2.4,
    "scalebar_segments": 2,
    "scalebar_target_min_mm": 22.0,
    "scalebar_target_max_mm": 46.0,
    "scalebar_border_width_mm": 0.3,
    "scalebar_label_gap_mm": 0.9,
    "scalebar_label_height_mm": 3.6,
    "scalebar_fill_light": "#ffffff",
    "scalebar_fill_dark": "#111111",
    "scalebar_line_color": "#111111",
    "north_arrow_size_mm": 10.0,
    "north_arrow_margin_mm": 4.0,
    "map_credit_font_size": 4.6,
    # 标注：起终点统一留出"星标半径 + 间距"，长中文名有足够横向空间。
    "label_box_width_mm": 44.0,
    "label_box_height_mm": 5.0,
    "label_endpoint_offset_mm": 5.4,
    "label_turn_offset_mm": 3.0,
}

#: 图例分组标题与顺序（与 template_catalog.ROUTE_OVERVIEW_LEGEND_ORDER 同序）。
#: 当前项目没有机场数据，因此分组名就是"既有设施"；以后机场进入模板时由模板
#: 动态决定对应分组名，不在这里硬编码"与机场"。
LEGEND_GROUPS = (
    ("environment", "地理环境"),
    ("obstacle", "障碍物"),
    ("facility", "既有设施"),
    ("route", "规划航路"),
)

#: 图例**两列布局**的语义分列（组名 → 列号 0/1）。
#:
#: 分组本身已经决定了最自然的左右划分：左列放地理环境与障碍物，右列放既有设施与
#: 规划航路。这既是产品建议的分组，也让两列条目数接近（4 : 5），因此图例框不会出现
#: "一列很高、另一列一半空白"。某些组整体缺席时（例如当前没有机场 / 没有障碍物），
#: :func:`cns_planner.gis.figure_legend.legend_geometry` 会按**实际出现的组**分列；
#: 一旦出现未登记的组，则回退到自动均衡分列算法。
LEGEND_GROUP_COLUMNS = {
    "environment": 0,
    "obstacle": 0,
    "facility": 1,
    "route": 1,
}

#: 图层键 → 图例分组。
#: 注意：``airspace`` 仍然登记在册（供其它模板复用），但 route_overview_v1
#: **不再 materialize 空域图层**，因此它不会出现在这张图的图例里。
LEGEND_GROUP_OF = {
    "sea": "environment",
    "land": "environment",
    "airspace": "facility",
    "terrain_obstacle": "obstacle",
    "building_obstacle": "obstacle",
    "tower_existing": "facility",
    "tower_obstacle": "facility",
    "airport": "facility",
    "airport_protection": "facility",
    "planned_route": "route",
    "turn_point": "route",
    "start_point": "route",
    "end_point": "route",
}

#: 中文字体候选：Windows 优先系统自带中文字体，缺失时回退到 QGIS 自带字体。
FONT_CANDIDATES = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
    "SimSun",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "DejaVu Sans",
)

#: Windows 中文字体文件候选（只引用系统已安装字体，**绝不**把字体文件提交进仓库）。
FONT_FILE_CANDIDATES = (
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/msyh.ttf",
    r"C:/Windows/Fonts/simhei.ttf",
    r"C:/Windows/Fonts/simsun.ttc",
)


def style(style_key):
    """取样式定义（副本）；未知键抛 ``KeyError``，绝不静默用一个默认样式顶替。"""

    if style_key not in FIGURE_STYLES:
        raise KeyError(f"未登记的专题图样式：{style_key}")
    return deepcopy(FIGURE_STYLES[style_key])


def label_style(label_key):
    if label_key not in LABEL_STYLES:
        raise KeyError(f"未登记的标注样式：{label_key}")
    return deepcopy(LABEL_STYLES[label_key])


def legend_order_key(layer_key):
    """图例稳定排序键：按 :data:`LEGEND_GROUPS` 分组，再按登记顺序。"""

    from ..reporting.map_templates import ROUTE_OVERVIEW_LEGEND_ORDER

    group = LEGEND_GROUP_OF.get(layer_key, "zzz")
    group_index = next(
        (index for index, (key, _) in enumerate(LEGEND_GROUPS) if key == group), len(LEGEND_GROUPS),
    )
    order = ROUTE_OVERVIEW_LEGEND_ORDER
    layer_index = order.index(layer_key) if layer_key in order else len(order)
    return (group_index, layer_index)


# --------------------------------------------------------------------------- QGIS 符号

def _qgis():
    """按需导入 QGIS（保证纯数据路径在没有 QGIS 的解释器里也能被单元测试覆盖）。"""

    from qgis.core import (  # noqa: WPS433 - 延迟导入是本模块的契约
        QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
        QgsSimpleMarkerSymbolLayerBase,
    )

    return {
        "QgsFillSymbol": QgsFillSymbol, "QgsLineSymbol": QgsLineSymbol,
        "QgsMarkerSymbol": QgsMarkerSymbol, "QgsSimpleFillSymbolLayer": QgsSimpleFillSymbolLayer,
        "QgsSimpleLineSymbolLayer": QgsSimpleLineSymbolLayer,
        "QgsSimpleMarkerSymbolLayer": QgsSimpleMarkerSymbolLayer,
        "QgsSimpleMarkerSymbolLayerBase": QgsSimpleMarkerSymbolLayerBase,
    }


_MARKER_SHAPES = {
    "circle": "Circle", "square": "Square", "triangle": "Triangle",
    "star": "Star", "diamond": "Diamond", "pentagon": "Pentagon",
    "cross": "Cross",
}


def marker_shape_enum(shape):
    """把样式里的标记形状名映射到 QGIS 枚举值（未知形状抛错，不猜）。"""

    api = _qgis()
    if shape not in _MARKER_SHAPES:
        raise KeyError(f"未登记的标记形状：{shape}")
    return getattr(api["QgsSimpleMarkerSymbolLayerBase"], _MARKER_SHAPES[shape])


def marker_symbol(style_key):
    """标记符号（既有铁塔 / 铁塔障碍 / 机场 / 转弯点 / 起点 / 终点）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "marker":
        raise ValueError(f"{style_key} 不是标记样式")
    layer = api["QgsSimpleMarkerSymbolLayer"](
        marker_shape_enum(item["shape"]), float(item["size"]),
        float(item.get("angle") or 0.0),
    )
    layer.setColor(_color(item["fill"]))
    layer.setStrokeColor(_color(item["outline"]))
    layer.setStrokeWidth(0.0 if item.get("outline") == "transparent" else float(item["outline_width"]))
    symbol = api["QgsMarkerSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def line_symbol(style_key):
    """线符号（规划航路）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "line":
        raise ValueError(f"{style_key} 不是线样式")
    layer = api["QgsSimpleLineSymbolLayer"](_color(item["color"]), float(item["width"]))
    layer.setPenCapStyle(_cap_style(item.get("capstyle")))
    layer.setPenJoinStyle(_join_style(item.get("joinstyle")))
    symbol = api["QgsLineSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def fill_symbol(style_key):
    """面符号（海域 / 陆地 / 空域 / 障碍物 / 机场范围）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "fill":
        raise ValueError(f"{style_key} 不是面样式")
    layer = api["QgsSimpleFillSymbolLayer"]()
    layer.setColor(_color(item["fill"]))
    layer.setStrokeColor(_color(item["outline"]))
    layer.setStrokeWidth(0.0 if item.get("outline") == "transparent" else float(item["outline_width"]))
    layer.setStrokeStyle(_stroke_style(item.get("outline_style")))
    symbol = api["QgsFillSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    if item.get("opacity") is not None:
        symbol.setOpacity(float(item["opacity"]))
    return symbol


def symbol_for(style_key):
    """按样式键返回对应的 QgsSymbol（地图图层与图例预览共用）。"""

    item = style(style_key)
    if item["kind"] == "fill":
        return fill_symbol(style_key)
    if item["kind"] == "line":
        return line_symbol(style_key)
    return marker_symbol(style_key)


def symbol_preview_image(style_key, width_mm=6.0, height_mm=6.0):
    """用**同一份样式定义**把图例符号渲染成 PNG 字节。

    为什么不用 QgsSymbol 离屏渲染：本环境下 QgsSymbol 的离屏 ``renderFeature`` 需要
    完整的 QgsRenderContext（painter + mapToPixel + extent + scale），实测在 headless
    ``QT_QPA_PLATFORM=offscreen`` 下无法稳定产出内容（结果全透明）。图例符号改为按
    :data:`FIGURE_STYLES` **同一份样式表**直接绘制：形状 / 颜色 / 线宽 / 尺寸都取自同一
    定义，因此"图例与地图符号一致"仍由单一定义保证。
    """

    import math

    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

    scale = 12.0  # 12 px/mm ≈ 305 DPI：图例符号清晰且体积可控。
    width = max(8, int(round(width_mm * scale)))
    height = max(8, int(round(height_mm * scale)))
    item = style(style_key)
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        if item["kind"] == "fill":
            _draw_fill(painter, item, width, height)
        elif item["kind"] == "line":
            _draw_line(painter, item, width, height)
        else:
            _draw_marker(painter, item, width, height, math)
    finally:
        painter.end()
    return _image_png_bytes(image)


def _draw_fill(painter, item, width, height):
    from qgis.PyQt.QtCore import QRectF, Qt
    from qgis.PyQt.QtGui import QColor, QPen

    reference = min(width, height)
    inset = reference * 0.13
    painter.setBrush(QColor(str(item["fill"])))
    if str(item.get("outline")) == "transparent" or float(item.get("outline_width") or 0) <= 0:
        painter.setPen(Qt.NoPen)
    else:
        pen = QPen(QColor(str(item["outline"])))
        pen.setWidthF(max(1.0, float(item["outline_width"]) * reference * 0.35))
        if str(item.get("outline_style") or "solid") == "dash":
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
    painter.drawRect(QRectF(inset, inset, width - 2 * inset, height - 2 * inset))


def _draw_line(painter, item, width, height):
    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QColor, QPen

    pen = QPen(QColor(str(item["color"])))
    pen.setWidthF(max(1.2, float(item["width"]) * 1.6))
    pen.setCapStyle(_cap_style(item.get("capstyle")))
    painter.setPen(pen)
    painter.drawLine(QPointF(width * 0.08, height * 0.5), QPointF(width * 0.92, height * 0.5))


def _draw_marker(painter, item, width, height, math_module):
    from qgis.PyQt.QtCore import QPointF, QRectF
    from qgis.PyQt.QtGui import QColor, QPen, QPolygonF

    reference = min(width, height)
    centre = QPointF(width * 0.5, height * 0.5)
    radius = max(2.4, float(item["size"]) * reference * 0.06)
    pen = QPen(QColor(str(item["outline"])))
    pen.setWidthF(max(0.9, float(item.get("outline_width") or 0) * 1.6))
    painter.setPen(pen)
    painter.setBrush(QColor(str(item["fill"])))
    shape = str(item["shape"])
    if shape == "circle":
        painter.drawEllipse(centre, radius, radius)
        return
    if shape == "square":
        painter.save()
        try:
            painter.translate(centre)
            painter.rotate(float(item.get("angle") or 0.0))
            painter.drawRect(QRectF(-radius, -radius, 2 * radius, 2 * radius))
        finally:
            painter.restore()
        return
    if shape == "diamond":
        points = [
            QPointF(centre.x(), centre.y() - radius), QPointF(centre.x() + radius, centre.y()),
            QPointF(centre.x(), centre.y() + radius), QPointF(centre.x() - radius, centre.y()),
        ]
    elif shape == "triangle":
        half = radius * 1.15
        points = [
            QPointF(centre.x(), centre.y() - half),
            QPointF(centre.x() + half, centre.y() + half * 0.8),
            QPointF(centre.x() - half, centre.y() + half * 0.8),
        ]
    elif shape == "star":
        points = []
        for index in range(10):
            angle = -math_module.pi / 2 + index * math_module.pi / 5
            distance = radius * 1.25 if index % 2 == 0 else radius * 0.5
            points.append(QPointF(
                centre.x() + distance * math_module.cos(angle),
                centre.y() + distance * math_module.sin(angle),
            ))
    else:  # pragma: no cover - 未登记形状由 style() 抛错，这里只兜底
        painter.drawEllipse(centre, radius, radius)
        return
    painter.drawPolygon(QPolygonF(points))


def _image_png_bytes(image):
    """QImage → PNG 字节（经系统临时文件，QGIS 环境下的稳定路径）。"""

    import os
    import tempfile

    handle, path = tempfile.mkstemp(prefix="cns-symbol-", suffix=".png")
    os.close(handle)
    try:
        if not image.save(path, "PNG"):
            return b""
        with open(path, "rb") as stream:
            return stream.read()
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - 临时文件已被清理
            pass


def _color(value):
    from qgis.PyQt.QtGui import QColor

    if value is None or str(value) == "transparent":
        return QColor(0, 0, 0, 0)
    return QColor(str(value))


def _stroke_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"solid": Qt.SolidLine, "dash": Qt.DashLine, "dot": Qt.DotLine}
    return mapping.get(str(value or "solid"), Qt.SolidLine)


def _cap_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"round": Qt.RoundCap, "flat": Qt.FlatCap, "square": Qt.SquareCap}
    return mapping.get(str(value or "round"), Qt.RoundCap)


def _join_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"round": Qt.RoundJoin, "miter": Qt.MiterJoin, "bevel": Qt.BevelJoin}
    return mapping.get(str(value or "round"), Qt.RoundJoin)


__all__ = [
    "FIGURE_STYLES", "FONT_CANDIDATES", "FONT_FILE_CANDIDATES", "LABEL_STYLES", "LAYOUT",
    "LEGEND_GROUP_COLUMNS", "LEGEND_GROUP_OF", "LEGEND_GROUPS", "fill_symbol",
    "label_style", "legend_order_key", "line_symbol", "marker_shape_enum", "marker_symbol",
    "style", "symbol_for", "symbol_preview_image",
]
