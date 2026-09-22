"""BUG-ROUTE-004 表征测试：Theta* 搜索目标必须与 candidate 最终 exact-OD 目标一致。

修复前的缺陷（当前 HEAD）：

1. ``_theta_star()`` 的普通 relax 与 Theta* shortcut relax 都把
   ``distance=distance_m(start_point, neighbour_point)`` 交给 ``_extend_ledger()``，
   而该函数会把 distance **累加**到已有 ledger 上。于是搜索累计的不是真实 segment
   长度，而是反复累加的 ``origin -> neighbour`` 距离。
2. 搜索只走 ``source cell center -> ... -> target cell center``：
   ``exact start -> source center`` 与 ``target center -> exact end`` 两段 stub 既
   不进入 ledger，也不进入目标成本；而 ``_route_segment_audit()`` 却按
   ``exact start -> source center -> Theta* vertices -> target center -> exact end``
   重新计算 distance / risk / turn。两者可以不一致。
3. 搜索一旦 pop 到任意一个 target label 就 ``break``：不同 incoming heading 的
   terminal turn 不同，第一个被 pop 的 goal label 未必是完整 OD objective 最小者。
4. shortcut 的 ledger base 取自 ``parent_key``（current 的 parent），几何上的新
   parent 却是 grandparent：ledger 与它声称描述的那条链不一致。
5. ``_route_segment_audit()`` 用 360 分箱重算 heading，而搜索用
   ``heading_bin_count``（默认 8）分箱：turn 的定价基准与审计基准不同。

下面每个测试都固定一条不变量。它们中的绝大多数在当前 HEAD 上失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_layered_theta_star_v2 import (  # noqa: E402
    BUILDING_POLICY, LEVEL, build_mask, building_cell, cell_id, grid_cells,
    layer, plan_v2, request, route_for, user_defined_baseline_policy,
)
from cns_planner.algorithms.coverage.v1 import distance_m  # noqa: E402
from cns_planner.domain.layered_theta_v2 import (  # noqa: E402
    normalize_theta_v2_objective_policy,
)

HEADING_BIN_COUNT = 8
THETA_MIN_DEG = 5.0
SEARCH_PARAMETERS = {"heading_bin_count": HEADING_BIN_COUNT, "theta_min_deg": THETA_MIN_DEG}


def objective_policy(*, risk, turn, distance):
    return normalize_theta_v2_objective_policy({
        "risk_weight": risk, "turn_weight": turn, "distance_weight": distance,
        "source": "工程确认-BUG-ROUTE-004-测试", "provenance": "explicit_override",
        "confirmed": True,
    })


DISTANCE_ONLY = objective_policy(risk=0.0, turn=0.0, distance=1.0)
TURN_AND_DISTANCE = objective_policy(risk=0.0, turn=0.5, distance=0.5)


def centre(grid, column, row):
    wanted = cell_id(column, row)
    for cell in grid:
        if cell["grid_id"] == wanted:
            return list(cell["center"])
    raise AssertionError(f"网格中没有 {wanted}")


def blocked_checkerboard(grid, blocked):
    """把给定 ``(column, row)`` 建成超高建筑，得到确定性 blocked mask。"""

    buildings = {cell_id(column, row): building_cell(5000.0) for column, row in blocked}
    mask = build_mask(grid, buildings=buildings)
    for grid_id in buildings:
        assert mask["cells"][grid_id]["status"] == "blocked", grid_id
    return mask


def ledger_consistency(candidate):
    """读取（必须存在的）搜索目标账本 / 最终审计账本一致性证据。"""

    record = (candidate.get("search_statistics") or {}).get("goal_ledger_consistency")
    assert record is not None, (
        "BUG-ROUTE-004：candidate 必须报告搜索 winning goal ledger 与最终 exact-OD "
        "audit ledger 的一致性证据（search_statistics.goal_ledger_consistency）"
    )
    return record


def audit_ledger_from_segments(candidate):
    """只用 candidate 报告的 los_segments 复算 distance / risk / turn 账本。"""

    return {
        "distance_m": sum(float(item["length_m"] or 0.0) for item in candidate["los_segments"]),
        "risk_exposure_index_m": sum(
            float(item["risk_exposure_index_m"] or 0.0) for item in candidate["los_segments"]
        ),
    }


def geometry_length(points):
    return sum(distance_m(a, b) for a, b in zip(points, points[1:]))


def heading_delta_deg(from_deg, to_deg):
    delta = (float(to_deg) - float(from_deg)) % 360.0
    return abs(delta - 360.0 if delta > 180.0 else delta)


# ---------------------------------------------------------------------------------------
# a) distance-only 多段路径：搜索 distance == 最终真实路径长度
# ---------------------------------------------------------------------------------------


def test_distance_only_multi_segment_search_distance_equals_final_path_length():
    grid = grid_cells(columns=3, rows=3)
    mask = blocked_checkerboard(grid, [(1, 1)])
    route = {
        "route_id": "R0001", "start": centre(grid, 0, 0), "end": centre(grid, 2, 2),
    }
    candidate = plan_v2(
        grid, mask=mask, route=route, objective=DISTANCE_ONLY, parameters=SEARCH_PARAMETERS,
    )
    assert candidate["status"] == "candidate"
    assert len(candidate["grid_path"]) >= 2
    assert len(candidate["los_segments"]) >= 3, "该网格必须产生多段路径"

    objective = candidate["planning_objective"]
    audit = audit_ledger_from_segments(candidate)
    assert objective["distance_m"] == pytest.approx(audit["distance_m"], abs=1e-6)
    assert objective["distance_m"] == pytest.approx(
        geometry_length(candidate["path"]), abs=1e-6
    )

    record = ledger_consistency(candidate)
    assert record["consistent"] is True, record
    assert record["search_distance_m"] == pytest.approx(objective["distance_m"], abs=1e-6)
    assert record["search_total_cost"] == pytest.approx(record["audit_total_cost"], abs=1e-9)
    assert record["absolute_difference"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------------------
# b) exact OD 不在 cell center：两端 stub 必须计入
# ---------------------------------------------------------------------------------------


def test_off_center_od_stubs_enter_the_search_distance_ledger():
    grid = grid_cells(columns=3, rows=3)
    mask = blocked_checkerboard(grid, [(1, 1)])
    source_center = centre(grid, 0, 0)
    target_center = centre(grid, 2, 2)
    start = [source_center[0] + 0.003, source_center[1] + 0.002]
    end = [target_center[0] - 0.002, target_center[1] - 0.003]
    route = {"route_id": "R0001", "start": start, "end": end}

    candidate = plan_v2(
        grid, mask=mask, route=route, objective=DISTANCE_ONLY, parameters=SEARCH_PARAMETERS,
    )
    assert candidate["status"] == "candidate"
    first, last = candidate["los_segments"][0], candidate["los_segments"][-1]
    assert first["length_m"] == pytest.approx(distance_m(start, source_center), abs=1e-9)
    assert last["length_m"] == pytest.approx(distance_m(target_center, end), abs=1e-9)
    assert first["length_m"] > 0.0 and last["length_m"] > 0.0

    objective = candidate["planning_objective"]
    assert objective["distance_m"] == pytest.approx(
        sum(item["length_m"] for item in candidate["los_segments"]), abs=1e-6
    )
    # 两端 stub 必须真实存在于搜索账本里（而不是只在事后审计里出现）。
    record = ledger_consistency(candidate)
    assert record["search_distance_m"] == pytest.approx(objective["distance_m"], abs=1e-6)
    assert record["consistent"] is True, record
    assert candidate["path"][0] == start
    assert candidate["path"][-1] == end


# ---------------------------------------------------------------------------------------
# c) turn_weight > 0：两端 heading / turn 参与定价，且审计与搜索同基准
# ---------------------------------------------------------------------------------------


def test_turn_weight_prices_both_end_headings_on_the_search_bins():
    grid = grid_cells(columns=3, rows=3)
    mask = blocked_checkerboard(grid, [(1, 1)])
    source_center = centre(grid, 0, 0)
    target_center = centre(grid, 2, 2)
    # start 在 source center 正西、end 在 target center 正东：两端 stub 各有一个明确
    # heading，从而 terminal turn 也必须有明确定价。
    start = [source_center[0] - 0.004, source_center[1]]
    end = [target_center[0] + 0.004, target_center[1]]
    route = {"route_id": "R0001", "start": start, "end": end}

    candidate = plan_v2(
        grid, mask=mask, route=route, objective=TURN_AND_DISTANCE,
        parameters=SEARCH_PARAMETERS,
    )
    assert candidate["status"] == "candidate"
    turns = candidate["turn_statistics"]["turns"]
    assert turns, "两端 stub 的 heading 必须能产生可审计的转向定价"
    assert candidate["planning_objective"]["turn_count"] == len(turns)

    # 审计 heading 必须就是搜索定价用的分箱中心：8 分箱 => 22.5 / 67.5 / ...
    step = 360.0 / HEADING_BIN_COUNT
    for turn in turns:
        for key in ("from_heading_deg", "to_heading_deg"):
            value = turn[key]
            offset = (value - step / 2.0) % step
            assert min(offset, step - offset) < 1e-6, (
                f"{key}={value} 不是 heading_bin_count={HEADING_BIN_COUNT} 的分箱中心："
                "审计 heading 与搜索定价基准不一致"
            )

    # 审计 heading 链必须与 los_segments 报告的 outgoing heading 完全一致（并按
    # theta_min_deg 门限筛选：低于门限的转向不进入 turns）。
    headings = [item["outgoing_heading_deg"] for item in candidate["los_segments"]]
    expected = [
        (previous, following)
        for previous, following in zip(headings, headings[1:])
        if previous is not None and following is not None
        and heading_delta_deg(previous, following) > THETA_MIN_DEG
    ]
    assert [(item["from_heading_deg"], item["to_heading_deg"]) for item in turns] == expected

    record = ledger_consistency(candidate)
    assert record["consistent"] is True, record
    assert record["search_turn_cost_m"] == pytest.approx(
        candidate["planning_objective"]["turn_cost_m"], abs=1e-6
    )


# ---------------------------------------------------------------------------------------
# d) competing target headings：选择完整 OD objective 更小者
# ---------------------------------------------------------------------------------------


def test_competing_target_headings_pick_the_smaller_complete_od_objective():
    grid = grid_cells(columns=3, rows=3)
    mask = blocked_checkerboard(grid, [(1, 1)])
    source_center = centre(grid, 0, 0)
    target_center = centre(grid, 2, 2)
    start = [source_center[0] - 0.004, source_center[1] + 0.004]
    # end 放在 target cell 的西北角附近：从西边进入 target 与从南边进入 target 的
    # terminal turn 明显不同。
    end = [target_center[0] - 0.004, target_center[1] + 0.004]
    route = {"route_id": "R0001", "start": start, "end": end}

    candidate = plan_v2(
        grid, mask=mask, route=route, objective=TURN_AND_DISTANCE,
        parameters=SEARCH_PARAMETERS,
    )
    assert candidate["status"] == "candidate"
    statistics = candidate["search_statistics"]
    goal_candidates = statistics.get("goal_candidates")
    assert goal_candidates, (
        "BUG-ROUTE-004：搜索必须评估每一个到达 target 的 heading label（而不是在第一个 "
        "target label 处直接 break），并把完整 OD objective 记录为 goal_candidates"
    )
    assert len({item["incoming_heading_bin"] for item in goal_candidates}) >= 2, goal_candidates

    best = min(item["total_cost"] for item in goal_candidates)
    assert candidate["planning_objective"]["total_cost"] == pytest.approx(best, abs=1e-6)
    assert candidate["optimization_cost"] == pytest.approx(best, abs=1e-6)
    assert statistics.get("search_termination") == "queue_lower_bound_exceeds_best_goal"


# ---------------------------------------------------------------------------------------
# e) start / end 位于同一 cell
# ---------------------------------------------------------------------------------------


def test_start_and_end_inside_the_same_cell_still_build_the_exact_od_ledger():
    grid = grid_cells(columns=2, rows=2)
    source_center = centre(grid, 0, 0)
    start = [source_center[0] - 0.003, source_center[1] - 0.002]
    end = [source_center[0] + 0.003, source_center[1] + 0.002]
    route = {"route_id": "R0001", "start": start, "end": end}

    candidate = plan_v2(
        grid, route=route, objective=DISTANCE_ONLY, parameters=SEARCH_PARAMETERS,
    )
    assert candidate["status"] == "candidate"
    assert candidate["grid_path"] == [cell_id(0, 0)]
    assert candidate["path"][0] == start
    assert candidate["path"][-1] == end
    assert candidate["distance_m"] == pytest.approx(
        distance_m(start, source_center) + distance_m(source_center, end), abs=1e-6
    )
    assert candidate["planning_objective"]["distance_m"] == pytest.approx(
        candidate["distance_m"], abs=1e-9
    )
    # 搜索与审计必须描述同一条 exact-OD 链（含两段重合点之间的 stub）。
    record = ledger_consistency(candidate)
    assert record["consistent"] is True, record
    assert record["search_distance_m"] == pytest.approx(candidate["distance_m"], abs=1e-6)


# ---------------------------------------------------------------------------------------
# f) 成功 candidate 锁定：search objective == candidate final objective
# ---------------------------------------------------------------------------------------


def test_successful_candidate_locks_search_and_final_objective_together():
    grid = grid_cells(columns=5, rows=4)
    mask = blocked_checkerboard(grid, [(2, 1), (2, 2)])
    source_center = centre(grid, 0, 0)
    target_center = centre(grid, 4, 3)
    start = [source_center[0] - 0.002, source_center[1] + 0.001]
    end = [target_center[0] + 0.003, target_center[1] - 0.002]
    route = {"route_id": "R0001", "start": start, "end": end}

    candidate = plan_v2(
        grid, mask=mask, route=route, parameters=SEARCH_PARAMETERS,
        objective=objective_policy(risk=0.4, turn=0.3, distance=0.3),
    )
    assert candidate["status"] == "candidate"
    objective = candidate["planning_objective"]

    # 1) optimization_cost 就是 planning_objective.total_cost
    assert candidate["optimization_cost"] == pytest.approx(objective["total_cost"], abs=1e-9)
    # 2) distance_m 就是 los_segments 长度之和
    assert candidate["distance_m"] == pytest.approx(
        sum(item["length_m"] for item in candidate["los_segments"]), abs=1e-6
    )
    # 3) path 首尾就是 exact OD，内部顶点是 cell center
    assert candidate["path"][0] == start
    assert candidate["path"][-1] == end
    assert candidate["path"][1] == source_center
    assert candidate["path"][-2] == target_center
    # 4) 搜索 winning goal 的 total cost == candidate 最终 total cost
    record = ledger_consistency(candidate)
    assert record["consistent"] is True, record
    assert record["search_total_cost"] == pytest.approx(objective["total_cost"], abs=1e-9)
    assert record["search_risk_exposure_index_m"] == pytest.approx(
        objective["risk_exposure_index_m"], abs=1e-6
    )
    assert record["search_turn_cost_m"] == pytest.approx(objective["turn_cost_m"], abs=1e-6)
    assert record["search_distance_m"] == pytest.approx(objective["distance_m"], abs=1e-6)
    # 5) 目标权重语义未被改动
    assert objective["risk_weight"] == pytest.approx(0.4)
    assert objective["turn_weight"] == pytest.approx(0.3)
    assert objective["distance_weight"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------------------
# 旧 golden 语义：搜索仍然只按 population × shelter risk 定价
# ---------------------------------------------------------------------------------------


def test_default_objective_golden_case_still_holds_after_the_fix():
    grid = grid_cells()
    candidate = plan_v2(grid)
    objective = candidate["planning_objective"]
    assert candidate["status"] == "candidate"
    assert objective["risk_weight"] == pytest.approx(0.8)
    assert objective["turn_weight"] == pytest.approx(0.1)
    assert objective["distance_weight"] == pytest.approx(0.1)
    assert candidate["optimization_cost"] == pytest.approx(objective["total_cost"], abs=1e-9)
    assert candidate["route_risk_density"]["value"] == pytest.approx(0.5, abs=1e-9)
    assert candidate["search_statistics"]["search_completeness"] == "optimal_path_found"
    # 均匀 0.5 risk 场下，risk exposure 恰好是长度的一半。
    assert objective["risk_exposure_index_m"] == pytest.approx(
        0.5 * objective["distance_m"], abs=1e-6
    )
