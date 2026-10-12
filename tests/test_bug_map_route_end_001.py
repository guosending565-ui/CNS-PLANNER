"""BUG-MAP-ROUTE-END-001 取证与回归：Theta* V2 candidate.path 的精确端点语义。

用户现象（修复前）：地图上在节点/航路端点附近出现一小截越过端点的深色航路线，
端点 marker（例如 N004）看起来落在一条连续线中间。

本模块固定端点几何的事实与不变量：

1. ``candidate.path[0]`` / ``candidate.path[-1]`` **精确等于** scenario start / end
   （端点距离误差 0 m）—— 所以"线越过 marker"不可能是端点不精确造成的；
2. ``candidate.path[1]`` / ``candidate.path[-2]`` 是 **被选中的 source / target anchor
   格心**（见下）；
3. 端点 connector 是**权威 geometry**：它进入 candidate 的 OD 账本（distance / risk /
   turn），因此前端**不得**做显示期裁剪。

**语义修订记录（必须有，否则本文件会被误读成"能力回退"）**

原取证结论（2026-09-23，BUG-MAP-ROUTE-END-001）是：connector 固定折回 *所在格格心*，
当精确端点不在格心上时几何上会出现"先回到格心、再出发"的可见跳，并注明"若后续要消除该
可见跳，必须按归档第 13 节做成显式版本化变更"。

该修订由 commit ``2e9e0f7``（2026-09-28）以 **BUG-ROUTE-006 端点虚拟锚定** 落地：
``candidate.path[1]`` / ``path[-2]`` 不再固定是"所在格格心"，而是
``{精确端点所在格} ∪ 其 1-ring`` 中由**既有 objective 选优**、且位于
``ENDPOINT_ANCHOR_APERTURE_DEG``（90°，相对真实飞行方向）内的**锚点格心**；
``candidate["grid_path"]`` 的首/尾就是这两个锚点。折回因此被显式消除。

本文件据此重新基线：仍然锁定"端点精确 + connector 属于 OD 账本 + 前端不得裁剪 + 不得
折回"，但把"connector 顶点 == 所在格格心"这一已被取代的断言，改为
"connector 顶点 == 被选中锚点的格心，且不构成反向折回"。

复用既有 Theta* V2 单测的 fixture 装配（``tests/test_layered_theta_star_v2.py``），
保证本文件只验证**几何端点语义**，不重复维护第二套 planner 装配。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import test_layered_theta_star_v2 as T


def metres(left, right):
    """经纬度近似米制距离：只用于取证量级，不参与任何算法判定。"""

    latitude = math.radians((left[1] + right[1]) / 2.0)
    return math.hypot(
        (right[0] - left[0]) * 111320.0 * math.cos(latitude),
        (right[1] - left[1]) * 110540.0,
    )


def _plan(cells, start, end, route_id="R0001"):
    return T.plan_v2(cells, route={
        "route_id": route_id, "start": list(start), "end": list(end),
    })


def _by_id(cells, grid_id):
    return next(cell for cell in cells if cell["grid_id"] == grid_id)


def _anchor_center(cells, grid_id):
    return list(_by_id(cells, grid_id)["center"])


def _one_ring(cells, grid_id):
    """``grid_id`` 自身加它的 8 邻域（= planner ``GridGraph.neighbors`` 的候选锚点定义）。"""

    anchor = _by_id(cells, grid_id)
    allowed = {grid_id}
    for cell in cells:
        if max(
            abs(cell["column"] - anchor["column"]), abs(cell["row"] - anchor["row"])
        ) == 1:
            allowed.add(cell["grid_id"])
    return allowed


def _bearing_deg(start_point, end_point):
    """0 = 正北、顺时针（与 planner 的 ``grid_bearing_deg`` 同口径，仅用于取证）。"""

    delta_lon = end_point[0] - start_point[0]
    delta_lat = end_point[1] - start_point[1]
    if delta_lon == 0.0 and delta_lat == 0.0:
        return None
    return math.degrees(math.atan2(delta_lon, delta_lat)) % 360.0


def _heading_delta_deg(from_deg, to_deg):
    return abs((float(to_deg) - float(from_deg) + 180.0) % 360.0 - 180.0)


# --------------------------------------------------------------------------------------
# 1. 端点不变量：candidate.path 的首尾**精确**等于 scenario 起终点
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,label",
    [
        (None, None, "两端都在 cell centre"),
        ([122.0099, 30.005], None, "起点在 source cell 东边缘"),
        (None, [122.0599, 30.035], "终点深入 target cell 东侧"),
        ([122.0099, 30.005], [122.0599, 30.035], "两端都偏离 cell centre"),
    ],
)
def test_candidate_path_endpoints_are_exactly_the_scenario_endpoints(start, end, label):
    """无论端点是否落在 cell centre 上，path 首尾必须**精确**等于 scenario 起终点。"""

    cells = T.grid_cells()
    real_start = list(start) if start else list(cells[0]["center"])
    real_end = list(end) if end else list(cells[-1]["center"])
    candidate = _plan(cells, real_start, real_end)
    assert candidate["status"] == "candidate", label
    path = candidate["path"]
    assert len(path) >= 2, label
    assert path[0] == real_start, f"{label}：candidate.path[0] 必须精确等于 scenario start"
    assert path[-1] == real_end, f"{label}：candidate.path[-1] 必须精确等于 scenario end"
    # 端点距离误差严格为 0 m（不是"很小"，是精确相等）。
    assert metres(path[0], real_start) == 0.0
    assert metres(path[-1], real_end) == 0.0


def test_start_connector_vertex_is_the_selected_source_anchor_centre():
    """起点 connector 的顶点是 objective 选出的 **source anchor 格心**（BUG-ROUTE-006）。

    BUG-ROUTE-006 之前该顶点被固定为"精确起点所在格的格心"；当前语义下候选锚点是
    ``{所在格} ∪ 1-ring``，由既有 objective 在全部组合里选优（``grid_path[0]`` 就是被选中的
    那一格）。因此本测试锁定：

    * ``path[1]`` / ``path[-2]`` 精确等于 ``grid_path[0]`` / ``grid_path[-1]`` 的格心；
    * 被选中的 target anchor 属于"包含精确终点的那一格"的 1-ring（含自身）；
    * 起点偏离格心时 connector 长度 > 0（这正是"多出一截"的几何来源）。
    """

    cells = T.grid_cells()
    exact_start = [122.0099, 30.005]
    exact_end = [122.0599, 30.035]
    candidate = _plan(cells, exact_start, exact_end)
    assert candidate["status"] == "candidate"
    path = candidate["path"]
    grid_path = candidate["grid_path"]

    assert path[1] == _anchor_center(cells, grid_path[0]), (
        "path[1] 必须是 grid_path[0]（被选中的 source anchor）的格心"
    )
    assert path[-2] == _anchor_center(cells, grid_path[-1]), (
        "path[-2] 必须是 grid_path[-1]（被选中的 target anchor）的格心"
    )

    # target anchor 必须是"真正包含精确终点的格"自身或其 1-ring 邻居。
    target_cell = next(
        cell for cell in cells
        if cell["bbox"][0] <= exact_end[0] < cell["bbox"][2]
        and cell["bbox"][1] <= exact_end[1] < cell["bbox"][3]
    )
    assert grid_path[-1] in _one_ring(cells, target_cell["grid_id"])
    assert path[-1] == exact_end

    # 起点 connector 长度 = 精确起点到被选中锚点格心的距离（> 0 时才会出现可见跳）。
    assert metres(path[0], path[1]) > 100.0

    # 端点恰好落在格心时：path 首尾仍然精确等于格心，且"所在格"必须是**已准入**的候选锚点
    # （最优锚点可以因同长平局而落在 1-ring 上，但所在格绝不能被 aperture 误排除）。
    centred = _plan(cells, cells[0]["center"], cells[-1]["center"])
    assert centred["path"][0] == cells[0]["center"]
    assert centred["path"][-1] == cells[-1]["center"]
    anchors = centred["search_statistics"]["endpoint_anchors"]
    assert cells[0]["grid_id"] in anchors["source_anchor_grid_ids"], (
        "精确起点正好落在所在格格心时，该格必须是已准入的 source anchor"
    )
    assert cells[-1]["grid_id"] in anchors["target_anchor_grid_ids"], (
        "精确终点正好落在所在格格心时，该格必须是已准入的 target anchor"
    )


def test_reported_distance_agrees_with_the_exact_od_polyline_including_connectors():
    """``distance_m`` 必须与整条折线全长（含两端 cell-centre connector）一致。"""

    cells = T.grid_cells()
    exact_start = [122.0099, 30.005]
    exact_end = [122.0599, 30.035]
    candidate = _plan(cells, exact_start, exact_end)
    path = candidate["path"]
    polyline_m = sum(metres(left, right) for left, right in zip(path, path[1:]))
    assert candidate["distance_m"] == pytest.approx(polyline_m, rel=5e-3)
    # 而且大于"只看起终点"的直线距离（说明 connector 确实被计价了）。
    assert candidate["distance_m"] > metres(exact_start, exact_end)


def test_off_centre_start_never_folds_back_through_the_containing_cell_centre():
    """取证核心（BUG-ROUTE-006 之后的当前语义）：起点在 source cell 前进侧边缘时，
    candidate **不再**"先折回所在格格心再出发"。

    BUG-MAP-ROUTE-END-001 记录的是修复前的几何：connector 固定折回 source cell centre，
    在地图上表现为"端点 marker 落在一条连续线中间"。BUG-ROUTE-006 把端点锚定改成
    "所在格 + 1-ring 的 objective 选优 + aperture 排除反向锚点"，该折回因此不再出现。

    本测试锁定的正是"折回被消除"这件事本身：如果 aperture 又把反向锚点放回候选集
    （例如把 target 的比较基准取成 ``exact end -> exact start``），这里就会重新出现
    反向折回、甚至直接 ``no_path``。
    """

    cells = T.grid_cells()
    exact_start = [122.0 + 0.0099, 30.0 + 0.005]   # source cell 东边缘
    exact_end = [122.05 + 0.005, 30.03 + 0.005]
    candidate = _plan(cells, exact_start, exact_end)
    assert candidate["status"] == "candidate"
    path = candidate["path"]

    assert path[0] == exact_start
    # connector 顶点仍然必须是某个锚点格心（权威几何，不得被前端裁剪/吸附）。
    assert path[1] == _anchor_center(cells, candidate["grid_path"][0])

    # 1) 方向不变量：起点 connector 与紧随其后的搜索段**不得**反向（这就是"折回"）。
    first = (path[1][0] - path[0][0], path[1][1] - path[0][1])
    second = (path[2][0] - path[1][0], path[2][1] - path[1][1])
    dot = first[0] * second[0] + first[1] * second[1]
    assert dot >= 0, "起点 connector 与首段搜索段方向相反 ⇒ 折回缺陷回归"

    # 2) aperture 不变量：被选中的 source anchor 相对真实飞行方向的夹角必须在 90° 内。
    departure = _bearing_deg(exact_start, path[1])
    forward = _bearing_deg(exact_start, exact_end)
    assert _heading_delta_deg(forward, departure) <= 90.0 + 1e-9

    # 3) 本例中"所在格格心"正好位于飞行方向的反侧，因此必须被 aperture 排除
    #    （这正是修复前那个约 470 m 折回的来源）。
    source_cell = _by_id(cells, candidate["grid_path"][0])
    containing = next(
        cell for cell in cells
        if cell["bbox"][0] <= exact_start[0] < cell["bbox"][2]
        and cell["bbox"][1] <= exact_start[1] < cell["bbox"][3]
    )
    assert containing["grid_id"] != source_cell["grid_id"], (
        "该算例的 source anchor 必须落在所在格的 1-ring 内，否则本测试不再覆盖折回场景"
    )
    assert _heading_delta_deg(
        forward, _bearing_deg(exact_start, containing["center"])
    ) > 90.0, "所在格锚点在该算例里必须处于飞行方向反侧（修复前正是它造成折回）"


def test_candidate_path_is_unchanged_by_display_layer_slicing_contract():
    """前端 overlay 的契约：candidate.path 原样绘制，绝不做显示期裁剪。"""

    source = Path("cns_planner/web/js/map/layered_candidate_overlay.js").read_text(
        encoding="utf-8"
    )
    for forbidden in ("slice(", "pop()", "clip", "trimEnd", "dropLast"):
        assert forbidden not in source, (
            f"candidate overlay 不得包含 {forbidden}：显示层绝不允许裁剪权威几何"
        )
    assert "drawablePoints(candidate.path)" in source


def test_endpoint_semantics_are_documented_in_the_planner():
    """锁定文档化语义：exact start → selected source anchor → chain → selected target
    anchor → exact end，两端 connector 都进入 OD 账本（BUG-ROUTE-006）。"""

    source = Path("cns_planner/layered_route_planner/theta_star_v2.py").read_text(
        encoding="utf-8"
    )
    assert "start_connector_is_a_ledger_segment" in source
    assert "end_connector_is_a_ledger_segment" in source
    assert "exact start -> selected source anchor" in source
    assert "selected target anchor -> exact end" in source
    # 锚点候选集与 aperture 也必须被显式文档化（否则本文件的语义无从复核）。
    assert "containing_cell_plus_one_ring" in source
    assert "fixed_cell_centre_connector_removed" in source
