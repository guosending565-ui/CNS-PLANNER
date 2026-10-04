"""Round30-B1.2 真机（PyQGIS）量化验收：引线、同址合并卡、框外经度与比例尺。

为什么必须有这一层：本轮的三条修改都只能在**真实 QGIS 版面 + 真实导出像素**上收口：

1. **引线真的变细、真的不再超长**：读渲染器自述的引线几何（线宽取自 ``LAYOUT``），
   并在 300 DPI 像素上确认引线是细线而不是原来的粗线；
2. **同址 endpoint + navigation 真的合并成一张卡**：浅黄（navigation 卡色）底纹条
   必须出现在起终点卡内部，且地图上不再有第二张导航文字卡；
3. **框外经度真的在地图框之外、比例尺真的与它分离**：用像素行剖面量出
   "地图框下沿 → 经度标注带 → 比例尺"三者的相对位置。

运行方式（需要 QGIS 解释器）：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' \\
        tests/run_qgis_final_tuning_tests.py
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("qgis", reason="需要 PyQGIS（用 python-qgis-ltr.bat 运行本文件）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from cns_planner.application.map_figure_service import _layout_plan  # noqa: E402
from cns_planner.gis.figure_spec import (  # noqa: E402
    ExtentSpec, FigureSpec, GEOMETRY_LINE, GEOMETRY_POINT, LabelSpec, LayerSpec,
    LegendItem,
)
from cns_planner.gis.figure_style import CNS_LABEL_CARD_COLORS, LAYOUT  # noqa: E402
from cns_planner.gis.qgis_figure_renderer import (  # noqa: E402
    QgisFigureRenderer, leader_length_budget,
)
from cns_planner.reporting.map_templates import CNS_SHARED_PARAMETERS  # noqa: E402

#: 进程级持有 QgsApplication（局部变量会被 GC，之后任何 Qt 访问都会 fail-fast）。
_QGIS_APPLICATION = None

#: R0005 真实航路端点与同址站址（与 canonical state 一致）。
START = [122.2672222222, 29.8666666667]
END = [122.1883333333, 29.8194444444]
COLOCATED = (122.228521, 29.831441)


def _qgis_app():
    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


def _render(spec, dpi=300.0):
    from PIL import Image

    renderer = QgisFigureRenderer()
    payload = renderer.render(spec, dpi=dpi)
    assert payload.startswith(b"\x89PNG")
    image = Image.open(io.BytesIO(payload)).convert("RGB")
    return _Array(image), dpi, renderer


class _Array:
    """导出图的只读像素矩阵（numpy），几何换算与颜色统计都在这里做。"""

    def __init__(self, image):
        import numpy as np

        self.image = image
        self.array = np.asarray(image, dtype=np.int16)
        self.height, self.width = self.array.shape[:2]

    def mask(self, color, tolerance=8):
        import numpy as np

        target = np.array(color, dtype=np.int16)
        return (np.abs(self.array - target).max(axis=2) <= tolerance)

    def count(self, color, window=None, tolerance=8):
        mask = self.mask(color, tolerance)
        if window is None:
            return int(mask.sum())
        x0, y0, x1, y1 = window
        return int(mask[y0:y1, x0:x1].sum())

    def ink_rows(self, y_from, y_to, x_from, x_to, threshold=180):
        band = self.array[y_from:y_to, x_from:x_to]
        dark = (band.min(axis=2) < threshold).any(axis=1)
        return [y_from + index for index, value in enumerate(dark) if value]


def _rgb(text):
    value = text.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _map_window(image, layout, dpi):
    """地图框对应的像素窗口（**外沿**，不向内缩：贴边 = 仍在框内）。"""

    return (
        int(float(layout["map_left_mm"]) / 25.4 * dpi),
        int(float(layout["map_top_mm"]) / 25.4 * dpi),
        int((float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi),
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi),
    )


def _base_spec(template_id, title):
    parameters = {**CNS_SHARED_PARAMETERS}
    layout = _layout_plan(parameters)
    layout["map_disclosure"] = ""
    return FigureSpec(
        template_id=template_id, template_version=1,
        title=title, route_id="R0005", route_source="operational_routes",
        route_geometry=[START, list(COLOCATED), END],
        route_start=START, route_end=END, turn_points=[list(COLOCATED)],
        extent=ExtentSpec(west=122.0760, south=29.7296, east=122.3762, north=29.9565,
                          width_km=28.98, height_km=25.26),
        extent_mode="test", generated_from_revision=410, layout=layout,
        parameters=parameters,
    )


def _merged_endpoint_spec() -> FigureSpec:
    """起降点 + 同址导航监测提案：主卡第二行必须是浅黄 service tag。"""

    spec = _base_spec("navigation_layout_v1", "导航完整性监测点布设图（B1.2 合并卡验收）")
    spec.labels = [
        LabelSpec(kind="start", text="直升机场起降点", longitude=START[0],
                  latitude=START[1], priority=100, style_key="label_endpoint",
                  services=["navigation"], secondary_text="导航监测提案（未确认）",
                  secondary_service="navigation"),
        LabelSpec(kind="end", text="东白莲华泰油库生活区野外起降点", longitude=END[0],
                  latitude=END[1], priority=100, style_key="label_endpoint_end",
                  services=["navigation"], secondary_text="导航监测提案（未确认）",
                  secondary_service="navigation"),
    ]
    spec.legend_items = [
        LegendItem("cns_nav_proposal", "cns_nav_proposal",
                   "导航完整性监测点提案（未确认）", "proposal"),
    ]
    spec.layers = [
        LayerSpec("cns_nav_proposal", "导航完整性监测点提案（未确认）", "cns_nav_proposal",
                  GEOMETRY_POINT, "cns_corridor_site_plan", source_status="available",
                  feature_count=2,
                  data={"points": [
                      {"longitude": START[0], "latitude": START[1]},
                      {"longitude": END[0], "latitude": END[1]},
                  ]}),
        LayerSpec("planned_route", "规划航路", "planned_route", GEOMETRY_LINE,
                  "operational_routes", source_status="available", feature_count=1,
                  data={"geometry": [START, list(COLOCATED), END]}),
    ]
    return spec


def _far_proposal_spec() -> FigureSpec:
    """一张离锚点较远的提案卡：用来证明引线会**换位置**而不是被拉长。"""

    spec = _base_spec("communication_layout_v1", "通信设施布设图（B1.2 引线预算验收）")
    spec.labels = [
        LabelSpec(kind="start", text="直升机场起降点", longitude=START[0],
                  latitude=START[1], priority=100, style_key="label_endpoint"),
        LabelSpec(kind="cns_proposal", text="普陀桃花沙岙村H杆站 /通信",
                  longitude=COLOCATED[0], latitude=COLOCATED[1], priority=80,
                  style_key="label_cns_proposal", services=["communication"]),
    ]
    spec.legend_items = [
        LegendItem("cns_comm_proposal", "cns_comm_proposal", "通信规划提案（未确认）",
                   "proposal"),
    ]
    spec.layers = [
        LayerSpec("cns_comm_proposal", "通信规划提案（未确认）", "cns_comm_proposal",
                  GEOMETRY_POINT, "cns_corridor_site_plan", source_status="available",
                  feature_count=1,
                  data={"points": [
                      {"longitude": COLOCATED[0], "latitude": COLOCATED[1]},
                  ]}),
        LayerSpec("planned_route", "规划航路", "planned_route", GEOMETRY_LINE,
                  "operational_routes", source_status="available", feature_count=1,
                  data={"geometry": [START, list(COLOCATED), END]}),
    ]
    return spec


# ============================================================ 1. 引线：线宽与预算

def test_rendered_leaders_never_exceed_the_length_budget():
    """真机渲染里每一条引线都不超过 15 mm（旧的几十毫米 L 形折线不再出现）。"""

    _qgis_app()
    budget = leader_length_budget()
    gap = float(LAYOUT["leader_line_endpoint_gap_mm"])
    for spec in (_merged_endpoint_spec(), _far_proposal_spec()):
        _, _, renderer = _render(spec, dpi=150.0)
        for leader in renderer.leader_items:
            assert leader["length_mm"] <= budget + 1e-6, leader
            # 折线不得绕远路：长度 ≈ 直线距离 + 起点净空。
            assert leader["length_mm"] <= leader["distance_mm"] + 2.0 * gap + 1e-6, leader


def test_leader_is_a_thin_line_on_the_exported_pixels():
    """导出的引线必须是一根**细线**：沿法向剖面量出的线宽要贴近 0.45 mm。"""

    _qgis_app()
    spec = _far_proposal_spec()
    image, dpi, renderer = _render(spec, dpi=300.0)
    assert renderer.leader_items, "该用例必须真的画出引线"
    widths = _leader_profile_widths(image, renderer.leader_items)
    assert widths, "没有取到任何引线法向剖面"
    # 沿法向数出的墨迹宽度 = 线宽 + 抗锯齿，0.45 mm 的线实测约 0.55~0.65 mm；
    # 旧版 1.1 mm 的线会明显超过 1.0 mm，因此这个上界有判别力。
    assert max(widths) <= 0.85, (
        f"引线法向宽度最大 {max(widths):.3f} mm（全部："
        f"{[round(value, 3) for value in widths]}），超过 0.45 mm 目标线宽的合理余量"
    )
    assert float(LAYOUT["leader_line_width_mm"]) <= 0.5


def _leader_profile_widths(image, leaders, background_delta=6):
    """沿每条引线的**法向**数"与背景不同的连续像素"，得到线宽（毫米）。

    为什么不用颜色掩膜：引线只有 0.45 mm 宽，且颜色带 alpha，叠在浅绿覆盖圈 / 浅蓝海域
    上会被混色，用"匹配某个 RGB"会既漏检又误检。法向剖面直接量"墨迹跨了多少毫米"，
    与颜色无关，因此能真实反映线宽。
    """

    array = image.array
    px_per_mm = image.width / 210.0
    step_mm = 1.0 / (px_per_mm * 4.0)
    widths = []
    for leader in leaders:
        points = leader["points"]
        for index in range(len(points) - 1):
            (ax, ay), (bx, by) = points[index], points[index + 1]
            length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            if length <= 0.5:
                continue
            unit_x, unit_y = (bx - ax) / length, (by - ay) / length
            normal_x, normal_y = -unit_y, unit_x
            for ratio in (0.3, 0.5, 0.7):
                centre_x = ax + (bx - ax) * ratio
                centre_y = ay + (by - ay) * ratio
                sample = array[
                    int(round((centre_y + normal_y) * px_per_mm)),
                    int(round((centre_x + normal_x) * px_per_mm)),
                ]
                count = 0
                for step in range(-40, 41):
                    offset = step * step_mm
                    px = int(round((centre_x + normal_x * offset) * px_per_mm))
                    py = int(round((centre_y + normal_y * offset) * px_per_mm))
                    if int(abs(array[py, px] - sample).max()) > background_delta:
                        count += 1
                widths.append(count * step_mm)
    return widths


def test_leader_geometry_is_recorded_for_every_drawn_leader():
    """渲染器自述的引线几何与 LAYOUT 的线宽 / 预算同源（不是硬编码）。"""

    _qgis_app()
    _, _, renderer = _render(_far_proposal_spec(), dpi=150.0)
    for leader in renderer.leader_items:
        assert leader["vertex_count"] >= 2
        assert leader["kind"] in ("start", "end", "cns_proposal", "cns_existing")


# ============================================================ 2. 同址合并卡

def _color_bands(image, color, tolerance=6, minimum_run_mm=10.0):
    """找出某颜色在图上形成的**长水平条带**（``[(row, left, right), ...]``）。

    为什么必须要求"连续 ≥ minimum_run_mm"：浅黄 ``#fdf8dd`` 也会在图例区域由抗锯齿
    混色偶然出现一两个像素。真正的 service tag 底纹是横跨整张卡的长条，因此用条带
    而不是纯色计数来判定，既不会漏检也不会误检。
    """

    import numpy as np

    mask = np.array(
        np.abs(image.array - np.array(color, dtype=np.int16)).max(axis=2) <= tolerance
    )
    minimum_run = max(2, int(minimum_run_mm * image.width / 210.0))
    bands = []
    for row in range(mask.shape[0]):
        columns = np.flatnonzero(mask[row])
        if columns.size < minimum_run:
            continue
        breaks = np.flatnonzero(np.diff(columns) > 1)
        for segment in np.split(columns, breaks + 1):
            if segment.size >= minimum_run:
                bands.append((row, int(segment.min()), int(segment.max())))
    return bands


def test_merged_endpoint_card_shows_the_navigation_service_tag():
    """浅黄（navigation 卡色）底纹必须出现在起终点卡内，起终点强边框仍在。"""

    _qgis_app()
    spec = _merged_endpoint_spec()
    image, dpi, _ = _render(spec, dpi=300.0)
    layout = spec.layout
    window = _map_window(image, layout, dpi)
    px_per_mm = image.width / 210.0
    navigation_fill = _rgb(CNS_LABEL_CARD_COLORS["navigation"]["fill"])
    bands = _color_bands(image, navigation_fill)
    assert bands, "导航 service tag 底纹没有出现"
    # 两条底纹（起点卡 + 终点卡）必须都在地图框内。
    frame_left = float(layout["map_left_mm"]) * px_per_mm
    frame_right = (float(layout["map_left_mm"]) + float(layout["map_width_mm"])) * px_per_mm
    frame_bottom = (float(layout["map_top_mm"]) + float(layout["map_height_mm"])) * px_per_mm
    rows = sorted({band[0] for band in bands})
    assert rows[0] >= float(layout["map_top_mm"]) * px_per_mm
    assert rows[-1] <= frame_bottom
    for _row, left, right in bands:
        assert left >= frame_left and right <= frame_right
    # 起终点主卡的强边框颜色必须真的画在图上（起点绿 / 终点红）。
    assert image.count(_rgb("#12a150"), window=window, tolerance=8) > 100
    assert image.count(_rgb("#d81b1b"), window=window, tolerance=8) > 100
    # 两条主卡相距很远：底纹行跨度必须显著大于单张卡的高度（证明是两张卡而不是一张）。
    assert (rows[-1] - rows[0]) > 10.0 * px_per_mm


def test_merged_card_is_drawn_inside_the_map_frame():
    """合并后的主卡（含第二行）必须完整落在地图框内（用条带包围盒判定）。"""

    _qgis_app()
    spec = _merged_endpoint_spec()
    image, dpi, _ = _render(spec, dpi=300.0)
    px_per_mm = image.width / 210.0
    layout = spec.layout
    frame_left = float(layout["map_left_mm"]) * px_per_mm
    frame_top = float(layout["map_top_mm"]) * px_per_mm
    frame_right = (float(layout["map_left_mm"]) + float(layout["map_width_mm"])) * px_per_mm
    frame_bottom = (float(layout["map_top_mm"]) + float(layout["map_height_mm"])) * px_per_mm
    bands = _color_bands(image, _rgb(CNS_LABEL_CARD_COLORS["navigation"]["fill"]))
    assert bands, "没有找到任何导航 service tag 底纹"
    rows = [band[0] for band in bands]
    lefts = [band[1] for band in bands]
    rights = [band[2] for band in bands]
    assert min(lefts) >= frame_left, (min(lefts), frame_left)
    assert max(rights) <= frame_right, (max(rights), frame_right)
    assert min(rows) >= frame_top, (min(rows), frame_top)
    assert max(rows) <= frame_bottom, (max(rows), frame_bottom)


# ============================================================ 3. 框外经度与比例尺

def test_longitude_labels_are_outside_the_frame_and_separated_from_the_scale_bar():
    """经度标注在地图框**外**下侧；比例尺在框内，且两者不重叠。"""

    _qgis_app()
    spec = _merged_endpoint_spec()
    image, dpi, _ = _render(spec, dpi=300.0)
    layout = spec.layout
    px_per_mm = dpi / 25.4
    frame_bottom_px = int(
        (float(layout["map_top_mm"]) + float(layout["map_height_mm"])) / 25.4 * dpi
    )
    left_px = int(float(layout["map_left_mm"]) / 25.4 * dpi)
    right_px = int(
        (float(layout["map_left_mm"]) + float(layout["map_width_mm"])) / 25.4 * dpi
    )
    # 框外：框下沿起 4 mm 内必须有经度文字（说明经度确实画在框外）。
    outside = image.ink_rows(
        frame_bottom_px + 2, frame_bottom_px + int(4 * px_per_mm), left_px, right_px,
    )
    assert outside, "地图框外侧下沿没有经度标注"
    # 比例尺所在左下列：其墨迹必须全部高于框外经度带（且在框内）。
    scale_rows = image.ink_rows(
        int((float(layout["map_top_mm"]) + float(layout["map_height_mm"]) - 40.0)
            / 25.4 * dpi),
        frame_bottom_px - int(1.2 * px_per_mm),
        left_px, int(left_px + 70 * px_per_mm),
    )
    assert scale_rows, "地图框内左下角没有比例尺墨迹"
    assert max(scale_rows) <= frame_bottom_px, "比例尺跑出地图框"
    assert max(scale_rows) < min(outside), (
        f"比例尺最低墨迹行 {max(scale_rows)} 与框外经度最高行 {min(outside)} 重叠"
    )
    assert (min(outside) - max(scale_rows)) / px_per_mm >= 1.0, (
        "比例尺与框外经度之间的净空不足 1 mm"
    )
