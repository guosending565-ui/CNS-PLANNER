"""Round30-B1 真机（PyQGIS）量化验收：正圆、地图 CRS、标签背景卡、图例标题。

为什么必须有这一层：B1 的核心修复之一是"覆盖圈在纸面上不再是椭圆"。肉眼判断不构成
验收证据，因此这里在**真实 QGIS 版面**上量像素：

1. **正圆量化**：通信 4 km / RID 2 km / RID 5 km 三个圆，在 300 DPI 导出里
   ``宽度像素 / 高度像素`` 必须 ≈ 1.0（阈值 0.97 ~ 1.03）；并且实测像素直径必须与
   真实半径一致（4000 / 2000 / 5000 m × 每毫米多少像素 × 版面缩放），因此"为了好看
   改半径"会被直接测出来；
2. **地图画布 CRS = EPSG:32651，经纬网 CRS = EPSG:4326**；
3. WGS84 的 FigureSpec 坐标确实被投影到了米制画布（要素坐标量级就是证据）；
4. CNS 提案标签真的画出了**浅色背景卡**（像素颜色 = 官方配色），
   且单业务与多业务分块的颜色都能在图上看到；
5. 图例标题横跨图例框内容宽度、水平居中（用版面项几何 + 像素重心双重证据）。

运行方式（需要 QGIS 解释器）：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' -m pytest \\
        tests/test_map_figures_cartographic_polish_qgis.py -q
"""

from __future__ import annotations

import io
import math
import os

import pytest

pytest.importorskip("qgis", reason="需要 PyQGIS（用 python-qgis-ltr.bat 运行本文件）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cns_planner.application.map_figure_service import _layout_plan  # noqa: E402
from cns_planner.gis.figure_spec import (  # noqa: E402
    ExtentSpec, FigureSpec, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON,
    LabelSpec, LayerSpec, LegendItem,
)
from cns_planner.gis.figure_style import (  # noqa: E402
    CNS_LABEL_CARD_COLORS, LAYOUT,
)
from cns_planner.gis.qgis_figure_renderer import (  # noqa: E402
    DEFAULT_MAP_CRS, GRID_CRS_AUTHID, QgisFigureRenderer, build_layers,
    projected_extent, render_context,
)
from cns_planner.gis import qgis_figure_renderer as R  # noqa: E402
from cns_planner.reporting.map_templates import (  # noqa: E402
    CNS_SHARED_PARAMETERS, SURVEILLANCE_SERVICE_PARAMETER, SURVEILLANCE_SERVICE_RID,
)

#: 进程级持有 QgsApplication（局部变量会被 GC，之后任何 Qt 访问都会 fail-fast）。
_QGIS_APPLICATION = None

#: R0005 真实航路端点与同址站址（与 canonical state 一致）。
START = [122.2672222222, 29.8666666667]
END = [122.1883333333, 29.8194444444]
COLOCATED = (122.228521, 29.831441)

#: 圆形覆盖圈半径（米）——业务值，绝不允许为了好看而改变。
RADII_M = {"communication": 4000.0, "rid_land": 2000.0, "rid_sea": 5000.0}

#: 覆盖圈图层键 → 图上真实使用的样式键（样式表里的键名与图层键不同名）。
COVERAGE_STYLE_KEYS = {
    "communication": "cns_coverage_comm",
    "rid_land": "cns_coverage_rid_land",
    "rid_sea": "cns_coverage_rid_sea",
}


def _qgis_app():
    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


def _ring(longitude, latitude, radius_m, segments=72):
    """与装配层同源的米制正圆环（WGS84 输出），此处内联以保证测试自足。"""

    from pyproj import CRS, Transformer
    from shapely.geometry import Point
    from shapely.ops import transform as shapely_transform

    to_metric = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(32651),
                                     always_xy=True)
    to_wgs = Transformer.from_crs(CRS.from_epsg(32651), CRS.from_epsg(4326),
                                  always_xy=True)
    x, y = to_metric.transform(longitude, latitude)
    circle = Point(x, y).buffer(radius_m, quad_segs=segments // 4)
    projected = shapely_transform(lambda xs, ys: to_wgs.transform(xs, ys), circle)
    return [[round(float(px), 9), round(float(py), 9)] for px, py in projected.exterior.coords]


def _circle_spec(*, services=("communication",)) -> FigureSpec:
    """只画覆盖圈的 CNS spec（避免其它图层的同色像素干扰像素测量）。"""

    parameters = {**CNS_SHARED_PARAMETERS}
    layout = _layout_plan(parameters)
    spec = FigureSpec(
        template_id="cns_combined_v1", template_version=1,
        title="CNS 正圆验收图（测试）", route_id="R0005",
        route_source="operational_routes",
        route_geometry=[START, list(COLOCATED), END],
        route_start=START, route_end=END, turn_points=[list(COLOCATED)],
        extent=ExtentSpec(west=122.0760, south=29.7296, east=122.3762, north=29.9565,
                          width_km=28.98, height_km=25.26),
        extent_mode="test", generated_from_revision=410, layout=layout,
        parameters=parameters,
    )
    layers = []
    for key in services:
        radius = RADII_M[key]
        style_key = COVERAGE_STYLE_KEYS[key]
        ring = _ring(*COLOCATED, radius)
        geometry_type = GEOMETRY_LINE if key == "rid_sea" else GEOMETRY_POLYGON
        data = {"lines": [ring], "radius_m": radius} if key == "rid_sea" else {
            "polygons": [ring], "radius_m": radius,
        }
        layers.append(LayerSpec(
            style_key, f"覆盖圈 {radius:g} m", style_key,
            geometry_type, "cns_corridor_site_plan", source_status="available",
            feature_count=1, data=data,
        ))
    spec.layers = layers
    return spec


def _render(spec, dpi=300.0):
    from PIL import Image

    payload = QgisFigureRenderer().render(spec, dpi=dpi)
    assert payload.startswith(b"\x89PNG")
    return Image.open(io.BytesIO(payload)).convert("RGB"), dpi


def _palette(image):
    colors = image.getcolors(maxcolors=4_000_000) or []
    return {color: count for count, color in colors}


def _rgb(text):
    value = text.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _bbox_of_color(image, color, *, window=None):
    """某个纯色像素的包围盒（``window`` 可把搜索限制在地图框内）。"""

    pixels = image.load()
    x0, y0, x1, y1 = window or (0, 0, image.width, image.height)
    left = top = None
    right = bottom = -1
    for y in range(y0, y1):
        for x in range(x0, x1):
            if pixels[x, y] == color:
                if left is None or x < left:
                    left = x
                if right < x:
                    right = x
                if top is None or y < top:
                    top = y
                if bottom < y:
                    bottom = y
    if left is None:
        return None
    return (left, top, right, bottom)


def _expected_pixels(spec, radius_m, dpi):
    """由真实渲染范围换算出的**预期像素直径**（正圆 ⇒ 宽 = 高）。"""

    layout = spec.layout
    context = render_context(spec)
    rectangle = projected_extent(
        spec, context, width_mm=float(layout["map_width_mm"]),
        height_mm=float(layout["map_height_mm"]),
    )
    assert rectangle.width() > 1000.0, "渲染范围必须是米制"
    scale = float(layout["map_width_mm"]) / rectangle.width()   # mm / m
    diameter_mm = 2.0 * float(radius_m) * scale
    return diameter_mm / 25.4 * float(dpi)


# ============================================================ 1. 正圆量化

@pytest.mark.parametrize(
    ("service", "stroke", "radius"),
    (
        ("communication", "#1a9c4a", 4000.0),
        ("rid_land", "#e8720c", 2000.0),
        ("rid_sea", "#e8720c", 5000.0),
    ),
)
def test_coverage_circle_is_visually_round_and_keeps_its_true_radius(service, stroke, radius):
    """覆盖圈在纸面上必须是正圆，且实测直径对应真实米制半径。"""

    _qgis_app()
    spec = _circle_spec(services=(service,))
    image, dpi = _render(spec, dpi=300.0)
    layout = spec.layout
    # 把像素搜索限制在地图框内（图例符号也用过同一个颜色）。
    map_window = (
        int(float(layout["map_left_mm"]) / 25.4 * dpi) + 2,
        int(float(layout["map_top_mm"]) / 25.4 * dpi) + 2,
        int((float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi) - 2,
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi) - 2,
    )
    box = _bbox_of_color(image, _rgb(stroke), window=map_window)
    assert box is not None, f"{service} 的覆盖圈描边颜色没有出现在地图框内"

    width_px = box[2] - box[0] + 1
    height_px = box[3] - box[1] + 1
    ratio = width_px / height_px
    expected = _expected_pixels(spec, radius, dpi)

    # 1) 视觉宽高比 ≈ 1（允许抗锯齿 / 描边宽度的少量误差）。
    assert 0.97 <= ratio <= 1.03, (
        f"{service}: 宽高比 {ratio:.4f}（宽 {width_px}px / 高 {height_px}px）不是正圆"
    )
    # 2) 真实半径未被改动：实测直径 ≈ 2 × radius × 比例（容差含描边 ~0.6 mm）。
    tolerance = 0.6 / 25.4 * dpi
    assert abs(width_px - expected) <= tolerance, (
        f"{service}: 实测宽 {width_px}px，预期 {expected:.1f}px（半径 {radius:g} m）"
    )
    assert abs(height_px - expected) <= tolerance, (
        f"{service}: 实测高 {height_px}px，预期 {expected:.1f}px（半径 {radius:g} m）"
    )


def test_ellipse_regression_would_be_detected():
    """回归保护：把画布换回 WGS84 时宽高比会明显偏离 1（证明上面的断言有判别力）。"""

    _qgis_app()
    spec = _circle_spec(services=("communication",))
    # 保持几何不变，只把画布 CRS 换回 WGS84 —— 这正是 Round30-A 的旧行为。
    spec.layout = {**spec.layout, "map_crs": "EPSG:4326"}
    image, dpi = _render(spec, dpi=300.0)
    layout = spec.layout
    window = (
        int(float(layout["map_left_mm"]) / 25.4 * dpi) + 2,
        int(float(layout["map_top_mm"]) / 25.4 * dpi) + 2,
        int((float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi) - 2,
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi) - 2,
    )
    box = _bbox_of_color(image, _rgb("#1a9c4a"), window=window)
    assert box is not None
    ratio = (box[2] - box[0] + 1) / (box[3] - box[1] + 1)
    # 在经纬画布上 4 km 圆会被拉成明显的椭圆（约 1.1 倍以上）。
    assert abs(ratio - 1.0) > 0.05, f"WGS84 画布下宽高比只有 {ratio:.4f}，判别力不足"


# ============================================================ 2/3. CRS 口径

def test_map_canvas_is_metric_and_grid_stays_wgs84():
    _qgis_app()
    from qgis.core import QgsLayoutItemMap, QgsPrintLayout, QgsProject

    spec = _circle_spec(services=("communication",))
    project = QgsProject()
    try:
        layers = build_layers(spec, project, context=render_context(spec))
        assert layers
        # 内存图层 CRS 与画布一致（不是 4326）。
        for layer in layers:
            assert layer.crs().authid() == DEFAULT_MAP_CRS
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        from cns_planner.gis.qgis_figure_renderer import _build_map

        item, extent = _build_map(layout, spec, layers, render_context(spec))
        assert isinstance(item, QgsLayoutItemMap)
        assert item.crs().authid() == DEFAULT_MAP_CRS
        assert item.grid().crs().authid() == GRID_CRS_AUTHID == "EPSG:4326"
        # 渲染范围是米制（UTM 51N 的东坐标量级）。
        assert extent.width() > 10_000.0
        assert 200_000.0 < extent.center().x() < 600_000.0
        assert 3_000_000.0 < extent.center().y() < 4_000_000.0
    finally:
        project.clear()


def test_wgs84_geometry_is_really_projected_onto_the_metric_canvas():
    """要素坐标量级必须变成米制（只改 CRS 标签会立刻被这条测出来）。"""

    _qgis_app()
    from qgis.core import QgsProject

    spec = _circle_spec(services=("communication",))
    project = QgsProject()
    try:
        layers = build_layers(spec, project, context=render_context(spec))
        layer = layers[0]
        points = []
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            box = geometry.boundingBox()
            points.append((box.center().x(), box.center().y()))
        assert points
        for x, y in points:
            assert 100_000.0 < x < 900_000.0, f"东坐标 {x} 不是米制投影坐标"
            assert 3_000_000.0 < y < 4_000_000.0, f"北坐标 {y} 不是米制投影坐标"
    finally:
        project.clear()


# ============================================================ 4. 标签背景卡

def _label_spec() -> FigureSpec:
    """带浅绿（单业务）与浅绿+浅橙（多业务）背景卡的 CNS spec。"""

    parameters = {**CNS_SHARED_PARAMETERS}
    layout = _layout_plan(parameters)
    layout["map_disclosure"] = ""
    spec = FigureSpec(
        template_id="cns_combined_v1", template_version=1,
        title="CNS 标签背景卡验收图（测试）", route_id="R0005",
        route_source="operational_routes",
        route_geometry=[START, list(COLOCATED), END],
        route_start=START, route_end=END, turn_points=[list(COLOCATED)],
        extent=ExtentSpec(west=122.0760, south=29.7296, east=122.3762, north=29.9565,
                          width_km=28.98, height_km=25.26),
        extent_mode="test", generated_from_revision=410, layout=layout,
        parameters=parameters,
        labels=[
            LabelSpec(kind="start", text="直升机场起降点", longitude=START[0],
                      latitude=START[1], priority=100, style_key="label_endpoint"),
            LabelSpec(kind="cns_proposal", text="普陀桃花沙岙村H杆 /通信/RID",
                      longitude=COLOCATED[0], latitude=COLOCATED[1], priority=80,
                      style_key="label_cns_proposal",
                      services=["communication", "rid"]),
            LabelSpec(kind="cns_proposal", text="虾峙岛大岙站 /通信",
                      longitude=122.187705, latitude=29.822895, priority=80,
                      style_key="label_cns_proposal", services=["communication"]),
        ],
        legend_items=[
            LegendItem("cns_comm_proposal", "cns_comm_proposal", "通信规划提案（未确认）",
                       "proposal"),
        ],
    )
    spec.layers = [
        LayerSpec("cns_comm_proposal", "通信规划提案（未确认）", "cns_comm_proposal",
                  GEOMETRY_POINT, "cns_corridor_site_plan", source_status="available",
                  feature_count=2,
                  data={"points": [
                      {"longitude": COLOCATED[0], "latitude": COLOCATED[1]},
                      {"longitude": 122.187705, "latitude": 29.822895},
                  ]}),
        LayerSpec("planned_route", "规划航路", "planned_route", GEOMETRY_LINE,
                  "operational_routes", source_status="available", feature_count=1,
                  data={"geometry": [START, list(COLOCATED), END]}),
    ]
    return spec


def test_leader_line_is_really_rendered_when_the_card_is_far_from_the_anchor():
    """卡片离锚点 > 4 mm 时，leader line 必须真的出现在导出像素上。

    这里刻意把一个提案卡放在离锚点很远的位置（多业务 + 同址地名一起顺移），
    并在**引线中点**取像素：如果引线被地图层覆盖（z 值设错）该断言会立刻失败。
    """

    _qgis_app()
    spec = _label_spec()
    geometry = R.label_card_geometry(
        spec.labels[1].text, R._label_style_safe("label_cns_proposal"),
    )
    # 计算该卡的实际落点（与渲染器同一套纯几何），再取引线中点的像素。
    context = R.render_context(spec)
    extent = R.projected_extent(
        spec, context, width_mm=float(spec.layout["map_width_mm"]),
        height_mm=float(spec.layout["map_height_mm"]),
    )
    point_x, point_y = R.projected_point(
        context, spec.labels[1].longitude, spec.labels[1].latitude,
    )
    frame = {
        "x": float(spec.layout["map_left_mm"]), "y": float(spec.layout["map_top_mm"]),
        "width": float(spec.layout["map_width_mm"]),
        "height": float(spec.layout["map_height_mm"]),
    }
    anchor_x = frame["x"] + (point_x - extent.xMinimum()) / extent.width() * frame["width"]
    anchor_y = frame["y"] + (extent.yMaximum() - point_y) / extent.height() * frame["height"]
    placement = {"x": anchor_x, "y": anchor_y + 12.0, "align": None}
    leader = R.leader_line_geometry(
        placement, geometry["width_mm"], geometry["height_mm"], anchor_x, anchor_y,
    )
    assert leader is not None, "构造的用例必须产生 leader line"
    image, dpi = _render(spec, dpi=300.0)
    px_per_mm = dpi / 25.4
    mid_x = int((leader["start"][0] + leader["end"][0]) / 2.0 * px_per_mm)
    mid_y = int((leader["start"][1] + leader["end"][1]) / 2.0 * px_per_mm)
    # 引线宽度 1.1 mm ≈ 13 px，在 ±6 px 窗口内必须命中服务主色。
    expected = _rgb(CNS_LABEL_CARD_COLORS["communication"]["leader"])
    hits = 0
    pixels = image.load()
    for dy in range(-6, 7):
        for dx in range(-6, 7):
            color = pixels[mid_x + dx, mid_y + dy]
            if all(abs(color[index] - expected[index]) <= 26 for index in range(3)):
                hits += 1
    assert hits > 0, (
        f"引线中点 ({mid_x},{mid_y}) 附近没有服务主色像素（期望 {expected}）——"
        "leader line 可能被地图层覆盖"
    )


def test_label_background_cards_are_really_drawn_with_the_official_colours():
    _qgis_app()
    spec = _label_spec()
    image, _dpi = _render(spec, dpi=300.0)
    palette = _palette(image)
    # 单业务（浅绿）与多业务（浅绿 + 浅橙）两块底色都必须出现。
    for family in ("communication", "rid"):
        fill = _rgb(CNS_LABEL_CARD_COLORS[family]["fill"])
        assert palette.get(fill, 0) > 300, (
            f"{family} 的标签背景卡颜色 {CNS_LABEL_CARD_COLORS[family]['fill']} "
            f"只出现 {palette.get(fill, 0)} 像素"
        )
    # 背景是"浅色"：亮度必须明显高于文字色。
    light = _rgb(CNS_LABEL_CARD_COLORS["communication"]["fill"])
    assert sum(light) / 3.0 > 200.0


def test_label_background_cards_are_inside_the_map_frame():
    """背景卡必须落在地图框内（不许被裁切成半块）。"""

    _qgis_app()
    spec = _label_spec()
    image, dpi = _render(spec, dpi=300.0)
    layout = spec.layout
    window = (
        int(float(layout["map_left_mm"]) / 25.4 * dpi) + 2,
        int(float(layout["map_top_mm"]) / 25.4 * dpi) + 2,
        int((float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi) - 2,
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi) - 2,
    )
    for family in ("communication", "rid"):
        box = _bbox_of_color(
            image, _rgb(CNS_LABEL_CARD_COLORS[family]["fill"]), window=window,
        )
        assert box is not None, f"{family} 卡片不在图框内"
        assert box[0] > window[0] and box[1] > window[1]
        assert box[2] < window[2] and box[3] < window[3]


# ============================================================ 5. 图例标题居中

def test_legend_title_item_spans_the_box_and_is_horizontally_centred():
    _qgis_app()
    from qgis.PyQt.QtCore import Qt
    from qgis.core import QgsLayoutItemLabel, QgsPrintLayout, QgsProject

    from cns_planner.gis.qgis_figure_renderer import _build_legend_block

    spec = _label_spec()
    project = QgsProject()
    try:
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        _build_legend_block(layout, spec, (210.0, 297.0), "SimHei")
        plan = spec.layout
        side = float(LAYOUT["legend_side_padding_mm"])
        titles = [
            item for item in layout.items()
            if isinstance(item, QgsLayoutItemLabel) and item.text() == "图　例"
        ]
        assert len(titles) == 1
        title = titles[0]
        # 水平居中：Qt.AlignHCenter（不是 AlignLeft）。
        assert title.hAlign() == Qt.AlignHCenter
        expected_width = float(plan["legend_width_mm"]) - 2.0 * side
        assert title.sizeWithUnits().width() == pytest.approx(expected_width, abs=0.01)
        left = title.positionWithUnits().x()
        assert left == pytest.approx(float(plan["legend_left_mm"]) + side, abs=0.01)
        # 标题 item 的水平中心 = 整个图例框的水平中心。
        centre = left + expected_width / 2.0
        assert centre == pytest.approx(
            float(plan["legend_left_mm"]) + float(plan["legend_width_mm"]) / 2.0, abs=0.01,
        )
        # 标题位于图例框顶部（不与任何条目行重叠）。
        top = title.positionWithUnits().y()
        assert top >= float(plan["legend_top_mm"]) - 0.01
        assert top + title.sizeWithUnits().height() <= float(plan["legend_top_mm"]) + float(
            LAYOUT["legend_header_mm"]
        ) + 0.01
    finally:
        project.clear()


def test_legend_title_ink_is_horizontally_centred_on_the_page():
    """像素级复核：图例标题那一行文字的墨迹重心必须落在图例框水平中心附近。"""

    _qgis_app()
    spec = _label_spec()
    image, dpi = _render(spec, dpi=300.0)
    plan = spec.layout
    y0 = int((float(plan["legend_top_mm"]) + 0.2) / 25.4 * dpi)
    y1 = int((float(plan["legend_top_mm"]) + float(LAYOUT["legend_header_mm"]))
             / 25.4 * dpi)
    x0 = int(float(plan["legend_left_mm"]) / 25.4 * dpi)
    x1 = int((float(plan["legend_left_mm"]) + float(plan["legend_width_mm"]))
             / 25.4 * dpi)
    pixels = image.load()
    xs = []
    for y in range(y0, min(y1, image.height)):
        for x in range(x0, min(x1, image.width)):
            r, g, b = pixels[x, y]
            if r < 120 and g < 120 and b < 130:
                xs.append(x)
    assert xs, "图例标题区域没有找到深色文字像素"
    ink_centre = (min(xs) + max(xs)) / 2.0
    box_centre = (x0 + x1) / 2.0
    tolerance = 0.02 * (x1 - x0)
    assert abs(ink_centre - box_centre) <= tolerance, (
        f"标题墨迹重心 {ink_centre:.0f} 偏离图例框中心 {box_centre:.0f}"
        f"（容差 {tolerance:.0f}px）"
    )


# ============================================================ 6. 整图仍然成立

def test_five_figure_shared_layout_renders_without_annotation_boxes():
    """CNS 五图版面（无说明框）在真机下仍能出图，且地图框内没有整块白底说明框。"""

    _qgis_app()
    spec = _circle_spec(services=("communication", "rid_land", "rid_sea"))
    # 模拟 Radar 图的图外短披露：重新规划一次版面，披露带因此出现在地图框下方。
    disclosure = "Radar-I：当前既有站址与模型约束下无可行布设"
    spec.layout = _layout_plan({**spec.parameters, "map_disclosure": disclosure})
    spec.layout["map_disclosure"] = disclosure
    spec.metadata = {"map_disclosure": disclosure}
    image, dpi = _render(spec, dpi=200.0)
    palette = _palette(image)
    assert palette.get(_rgb("#1a9c4a"), 0) > 50
    assert palette.get(_rgb("#e8720c"), 0) > 50

    # 地图框内部的"大面积纯白"像素必须是少数（说明框会带来成千上万个白像素块）。
    layout = spec.layout
    map_window = (
        int(float(layout["map_left_mm"]) / 25.4 * dpi) + 3,
        int(float(layout["map_top_mm"]) / 25.4 * dpi) + 3,
        int((float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi) - 3,
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi) - 3,
    )
    white = _bbox_of_color(image, (255, 255, 255), window=map_window)
    if white is not None:
        area = (white[2] - white[0] + 1) * (white[3] - white[1] + 1)
        # 地图主体里只可能有零散白像素（背景是海色 / 陆色），不可能有 30×20 mm 的白框。
        assert area < (30.0 / 25.4 * dpi) * (20.0 / 25.4 * dpi)

    # 图外披露短句（若模板给了披露）确实被画在"地图框下沿 → 图例框上沿"之间的带里。
    if layout.get("map_disclosure"):
        disclosure_top = float(layout["footer_disclosure_top_mm"])
        assert disclosure_top >= float(layout["map_top_mm"]) + float(layout["map_height_mm"])
        assert disclosure_top + float(layout["footer_disclosure_mm"]) <= float(
            layout["legend_top_mm"]
        )
    # 正式图默认不显示工程审计条（信息仍在 metadata）。
    assert layout["audit_footer"] is False
    assert layout["footer_strip_top_mm"] is None
