"""BUG-MAP-ROUTE-END-001 取证与回归：Theta* V2 candidate.path 的精确端点语义。

用户现象：地图上在节点/航路端点附近出现一小截越过端点的深色航路线，端点 marker
（例如 N004）看起来落在一条连续线中间。

本模块**先把事实固定下来**（不改任何规划语义），再给出结论所需的最小证据：

1. ``candidate.path[0]`` / ``candidate.path[-1]`` **精确等于** scenario start / end
   （端点距离误差 0 m）—— 所以"线越过 marker"不可能是端点不精确造成的；
2. ``candidate.path[1]`` / ``candidate.path[-2]`` 是 **source / target cell centre**
   —— 这是 Theta* V2 明确的设计（搜索跑在格心，端点 connector 属于同一条 OD 账本）；
3. 当精确端点**不在**格心上时，几何上会出现"先回到格心、再出发"的可见跳；
   本测试用一个可复现的算例把这种"折回"量化出来（首段与次段方向相反）。

结论（见交付报告）：多余的一截来自 **candidate（Theta* V2 authoritative path）的
cell-centre connector**，不是 operational / scenario 图层叠加，也不是端点不精确。
因此按冻结规则**不改** Theta* V2 语义；本文件是端点不变量的回归证据。

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


def test_start_connector_vertex_is_the_source_cell_centre():
    """起点 connector 的顶点就是 source cell centre。

    当起点**已经**落在格心上时，去重后看不到重复点；这里用"起点偏离格心"的算例，
    才能真正看到 connector 顶点（这正是地图上"多出一截"的几何来源）。终点侧的同一条
    connector 由 ``grid_path`` 的最后一格（真正包含终点的 target cell）体现。
    """

    cells = T.grid_cells()
    exact_start = [122.0099, 30.005]
    exact_end = [122.0599, 30.035]
    candidate = _plan(cells, exact_start, exact_end)
    path = candidate["path"]
    assert path[1] == cells[0]["center"], "第二点必须是 source cell centre"
    assert candidate["grid_path"][0] == cells[0]["grid_id"]
    # target cell 必须真正包含精确终点，且它就是 grid_path 的最后一格。
    target_cell = next(
        cell for cell in cells
        if cell["bbox"][0] <= exact_end[0] < cell["bbox"][2]
        and cell["bbox"][1] <= exact_end[1] < cell["bbox"][3]
    )
    assert candidate["grid_path"][-1] == target_cell["grid_id"]
    assert path[-1] == exact_end

    # 起点 connector 长度 = 精确起点到 source cell centre 的距离（> 0 时才会出现可见跳）。
    assert metres(path[0], path[1]) > 100.0

    # 端点恰好落在格心时：path 退化为"格心 → … → 格心"，connector 长度为 0。
    centred = _plan(cells, cells[0]["center"], cells[-1]["center"])
    assert centred["path"][0] == cells[0]["center"]
    assert centred["path"][-1] == cells[-1]["center"]


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


def test_off_centre_start_produces_a_visible_fold_back_that_is_authoritative_geometry():
    """取证核心：起点在 source cell 前进侧边缘时，path 出现"先折回格心再出发"的跳。

    这是"线越过 marker 继续"的几何来源；它是**权威 geometry**（与账本一致），
    因此不允许在前端做显示期裁剪来"看起来正常"。
    """

    cells = T.grid_cells()
    exact_start = [122.0 + 0.0099, 30.0 + 0.005]   # source cell 东边缘
    exact_end = [122.05 + 0.005, 30.03 + 0.005]
    candidate = _plan(cells, exact_start, exact_end)
    path = candidate["path"]

    assert path[0] == exact_start
    assert path[1] == cells[0]["center"], "第二点必须是 source cell centre"
    # 第一段（exact start → centre）与第二段（centre → 下一个顶点）方向相反 ⇒ 折回。
    first = (path[1][0] - path[0][0], path[1][1] - path[0][1])
    second = (path[2][0] - path[1][0], path[2][1] - path[1][1])
    dot = first[0] * second[0] + first[1] * second[1]
    assert dot < 0, "起点 connector 与首段搜索段方向相反 ⇒ 可见折回"
    # 折回长度 = 精确起点到格心的距离（本例约 470 m，与 L8 格尺度一致）。
    fold_back_m = metres(path[0], path[1])
    assert fold_back_m > 100.0
    assert fold_back_m == pytest.approx(metres(exact_start, cells[0]["center"]), abs=1e-6)


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
    """锁定文档化语义：exact start → source centre → chain → target centre → exact end。"""

    source = Path("cns_planner/layered_route_planner/theta_star_v2.py").read_text(
        encoding="utf-8"
    )
    assert "start_connector_is_a_ledger_segment" in source
    assert "end_connector_is_a_ledger_segment" in source
    assert "exact start -> source centre" in source
    assert "target centre -> exact end" in source
