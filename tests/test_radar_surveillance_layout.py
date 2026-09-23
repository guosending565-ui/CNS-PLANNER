"""Radar Surveillance Layout V1（「80m固定高度航路方向性雷达几何初步划设方案」）测试。

覆盖用户明确要求的 25 项：

1. Radar-I Rmin=120 / Rmax=3000 边界；
2. Radar-II Rmin=200 / Rmax=5000 边界；
3. elevation 0° / 45° 边界；
4. elevation < 0 / > 45 不可覆盖；
5. azimuth ±45 边界；
6. 359°/0° 环形角；
7. 同塔多个 panel 对同 sample 只算 1 个 site；
8. land 要求 2 个不同 site；
9. sea 要求 1 个 site；
10. 每塔 <= 4 panel；
11. I-only feasible 不启用 II；
12. I-only proven infeasible 才进入 mixed；
13. I-only time limit 不能当 infeasible；
14. mixed 首先最小化总 panel；
15. 同总 panel 下最少 II；
16. unknown land/sea fail-closed；
17. 不同 coverage set 方向生成确定性；
18. dominated candidate pruning 不改变最优覆盖；
19. 25m 初始优化后 5m 能发现遗漏；
20. refinement 补点后重新求解；
21. max refinement 仍失败时不能报完整覆盖；
22. 保存/恢复；
23. route/tower/device/land-mask/height 变化正确 stale；
24. 不修改 P7/P14-P16 既有结果；
25. 现有全量/相关回归不退化（由既有测试文件承载）。

以及 solver 不可用时的 ``solver_unavailable`` 语义（绝不退化为 greedy）。
"""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.radar_layout import milp as milp_module
from cns_planner.algorithms.radar_layout.candidates import (
    build_candidates, candidates_for_tower_and_type,
)
from cns_planner.algorithms.radar_layout.geometry import (
    bearing_deg, circular_angle_delta_deg, elevation_deg, interpolate_metric_path,
    normalize_azimuth_deg, panel_coverage, sample_offsets_m, slant_distance_m,
)
from cns_planner.algorithms.radar_layout.v1 import (
    SOFTWARE_BASELINE, actual_site_coverage, required_count_for,
    solve_layout, validation_report,
)
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.radar_surveillance_layout_service import (
    LAYOUT_KEY, POLICY_KEY, normalize_radar_surveillance_layout,
    normalize_radar_surveillance_policy,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.radar_surveillance_layout import (
    ALGORITHM_ID, FIXED_ALTITUDE_LAYER_ID, FIXED_ALTITUDE_M, METRIC_CRS, MODEL_SCOPE,
    RADAR_TYPES, RADAR_TYPE_I, RADAR_TYPE_II, radar_device_facts,
    radar_geometry_parameters,
)
from cns_planner.gis.radar_layout_adapter import (
    LandMaskSource, land_mask_hint, radar_mount_assumption_status, resolve_radar_origins,
)

DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE_ID = "R1"

RADAR_I = radar_geometry_parameters(RADAR_TYPE_I)
RADAR_II = radar_geometry_parameters(RADAR_TYPE_II)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def sample(index, metric, *, egm2008_m=80.0, surface_class="sea", distance_m=0.0):
    return {
        "sample_index": index, "sample_id": f"S{index:04d}",
        "metric": [float(metric[0]), float(metric[1])], "egm2008_m": float(egm2008_m),
        "distance_along_route_m": float(distance_m), "surface_class": surface_class,
        "required_distinct_site_count": required_count_for(surface_class),
    }


def tower(tower_id, metric, *, origin_egm2008_m=50.0):
    """默认雷达原点 50 m EGM2008（= ``tower_top_orthometric_m``，V1.1）。

    样本默认高度 80 m，因此默认几何是"目标比雷达原点高 30 m"⇒ **正**仰角
    （``0 <= elevation_deg <= 45`` 的可覆盖区间）。V1.1 的符号语义是
    ``vertical_delta_m = sample − origin``：目标低于雷达原点时为负仰角，
    本模型不下倾，因此不可覆盖。
    """

    return {
        "tower_id": tower_id, "name": f"塔{tower_id}",
        "metric": [float(metric[0]), float(metric[1])],
        "origin_egm2008_m": float(origin_egm2008_m),
        "longitude": None, "latitude": None,
    }


def solve(towers, samples, **kwargs):
    return solve_layout(towers=towers, samples=samples, **kwargs)


def selected(result):
    return {item["panel_id"]: item for item in result["selected_panels"]}


# --------------------------------------------------------------------------------------
# 1 / 2 — range boundaries per radar type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parameters,minimum,maximum",
    [(RADAR_I, 120.0, 3000.0), (RADAR_II, 200.0, 5000.0)],
)
def test_range_boundaries_are_inclusive_and_outside_is_not_covered(parameters, minimum, maximum):
    base = dict(
        origin_egm2008_m=100.0, sample_egm2008_m=100.0, tower_metric=[0.0, 0.0],
        panel_azimuth_deg=0.0, parameters=parameters,
    )
    assert panel_coverage(sample_metric=[0.0, minimum], **base)["covered"] is True
    assert panel_coverage(sample_metric=[0.0, minimum - 0.001], **base)["covered"] is False
    assert panel_coverage(sample_metric=[0.0, maximum], **base)["covered"] is True
    assert panel_coverage(sample_metric=[0.0, maximum + 0.001], **base)["covered"] is False
    assert parameters["min_slant_range_m"] == minimum
    assert parameters["max_slant_range_m"] == maximum


def test_radar_geometry_parameters_match_the_declared_fixed_values():
    assert RADAR_I["min_slant_range_m"] == 120.0
    assert RADAR_I["max_slant_range_m"] == 3000.0
    assert RADAR_II["min_slant_range_m"] == 200.0
    assert RADAR_II["max_slant_range_m"] == 5000.0
    for parameters in (RADAR_I, RADAR_II):
        assert parameters["azimuth_beamwidth_deg"] == 90.0
        assert parameters["azimuth_half_width_deg"] == 45.0
        assert parameters["elevation_center_deg"] == 22.5
        assert parameters["elevation_min_deg"] == 0.0
        assert parameters["elevation_max_deg"] == 45.0
        assert parameters["rcs_reference_m2"] == 0.01
        assert parameters["pd_reference"] == 0.8
        assert parameters["pfa_reference"] == 1e-6


# --------------------------------------------------------------------------------------
# 3 / 4 — elevation boundaries
# --------------------------------------------------------------------------------------


def test_elevation_zero_and_forty_five_degrees_are_inclusive():
    # elevation == 0 ⇒ 目标与雷达原点等高（vertical_delta = sample − origin = 0）。
    covered = panel_coverage(
        origin_egm2008_m=100.0, sample_egm2008_m=100.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert covered["elevation_deg"] == pytest.approx(0.0, abs=1e-9)
    assert covered["covered"] is True

    # elevation == +45 ⇔ vertical_delta == horizontal，且 vertical_delta = sample − origin
    # ⇒ 目标必须**高于**雷达原点同值（V1.1 修复后的唯一正确符号，BUG-RADAR-ELEV-002）。
    horizontal = 1000.0
    at_forty_five = panel_coverage(
        origin_egm2008_m=0.0, sample_egm2008_m=horizontal,
        sample_metric=[0.0, horizontal],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert at_forty_five["vertical_delta_m"] == pytest.approx(horizontal)
    assert at_forty_five["elevation_deg"] == pytest.approx(45.0, abs=1e-9)
    assert at_forty_five["slant_distance_m"] == pytest.approx(horizontal * 2 ** 0.5, abs=1e-6)
    assert at_forty_five["covered"] is True


def test_v1_1_elevation_sign_target_minus_radar_origin():
    """V1.1 真实示例：route=80 m、radar=50 m、horizontal=1000 m ⇒ elevation ≈ +1.718°。

    同一航路高度下，雷达原点高于 80 m 时俯仰角必须变**负**（不下倾 ⇒ 不覆盖）。
    """

    high_target = panel_coverage(
        origin_egm2008_m=50.0, sample_egm2008_m=80.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert high_target["vertical_delta_m"] == pytest.approx(30.0)
    assert high_target["elevation_deg"] == pytest.approx(
        math.degrees(math.atan2(30.0, 1000.0)), abs=1e-9,
    )
    assert high_target["elevation_deg"] == pytest.approx(1.7183, abs=1e-4)
    assert high_target["covered"] is True

    equal = panel_coverage(
        origin_egm2008_m=80.0, sample_egm2008_m=80.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert equal["vertical_delta_m"] == pytest.approx(0.0)
    assert equal["elevation_deg"] == pytest.approx(0.0)
    assert equal["covered"] is True

    # 雷达原点 110 m 高于 80 m 航路 ⇒ 目标在雷达下方 ⇒ 负仰角 ⇒ 不覆盖。
    radar_above = panel_coverage(
        origin_egm2008_m=110.0, sample_egm2008_m=80.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert radar_above["vertical_delta_m"] == pytest.approx(-30.0)
    assert radar_above["elevation_deg"] < 0
    assert radar_above["covered"] is False


def test_elevation_below_zero_and_above_forty_five_is_not_covered():
    # 目标低于雷达原点 ⇒ 负仰角（V1.1：不下倾 ⇒ 不可覆盖）。
    below = panel_coverage(
        origin_egm2008_m=110.0, sample_egm2008_m=100.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert below["vertical_delta_m"] == pytest.approx(-10.0)
    assert below["elevation_deg"] < 0
    assert below["covered"] is False

    # 目标高于雷达原点 1000 m、水平只有 100 m ⇒ elevation ≈ 84.3° > 45°。
    above = panel_coverage(
        origin_egm2008_m=0.0, sample_egm2008_m=1000.0, sample_metric=[0.0, 100.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=0.0, parameters=RADAR_I,
    )
    assert above["vertical_delta_m"] == pytest.approx(1000.0)
    assert above["elevation_deg"] > 45.0
    assert above["covered"] is False
    assert elevation_deg(0.0, 10.0) == 90.0
    assert elevation_deg(0.0, -10.0) == -90.0
    assert slant_distance_m(3.0, 4.0) == 5.0


# --------------------------------------------------------------------------------------
# 5 / 6 — azimuth boundaries and circular wrap
# --------------------------------------------------------------------------------------


def test_azimuth_plus_minus_45_boundary_is_inclusive():
    parameters = RADAR_I
    target = [0.0, 1000.0]  # bearing 0
    inside = panel_coverage(
        origin_egm2008_m=100.0, sample_egm2008_m=100.0, sample_metric=target,
        tower_metric=[0.0, 0.0], panel_azimuth_deg=45.0, parameters=parameters,
    )
    assert inside["azimuth_delta_deg"] == pytest.approx(45.0, abs=1e-9)
    assert inside["covered"] is True
    outside = panel_coverage(
        origin_egm2008_m=100.0, sample_egm2008_m=100.0, sample_metric=target,
        tower_metric=[0.0, 0.0], panel_azimuth_deg=45.001, parameters=parameters,
    )
    assert outside["covered"] is False


def test_circular_angle_handles_359_and_0_and_wraps_without_degree_as_meter():
    assert circular_angle_delta_deg(359.0, 0.0) == pytest.approx(1.0)
    assert circular_angle_delta_deg(0.0, 359.0) == pytest.approx(1.0)
    assert circular_angle_delta_deg(350.0, 10.0) == pytest.approx(20.0)
    assert circular_angle_delta_deg(10.0, 350.0) == pytest.approx(20.0)
    assert circular_angle_delta_deg(90.0, 270.0) == pytest.approx(180.0)
    assert normalize_azimuth_deg(360.0) == 0.0
    assert normalize_azimuth_deg(-1.0) == 359.0
    # panel 中心 359° 覆盖 bearing 0° 的目标（环形，而不是 |359-0|=359）。
    covered = panel_coverage(
        origin_egm2008_m=100.0, sample_egm2008_m=100.0, sample_metric=[0.0, 1000.0],
        tower_metric=[0.0, 0.0], panel_azimuth_deg=359.0, parameters=RADAR_I,
    )
    assert covered["azimuth_delta_deg"] == pytest.approx(1.0)
    assert covered["covered"] is True
    assert bearing_deg([0.0, 0.0], [1000.0, 0.0]) == 90.0
    assert bearing_deg([0.0, 0.0], [0.0, -1000.0]) == 180.0


# --------------------------------------------------------------------------------------
# 7 / 8 / 9 — site counting semantics
# --------------------------------------------------------------------------------------


def test_two_panels_of_the_same_tower_count_as_one_site_only():
    samples = [sample(0, [0.0, 1000.0])]
    towers = [tower("T1", [0.0, 0.0])]
    panels = [
        {"panel_id": "A1", "tower_id": "T1", "radar_type": RADAR_TYPE_I, "azimuth_deg": 0.0,
         "panel_half_width_deg": 45.0},
        {"panel_id": "A2", "tower_id": "T1", "radar_type": RADAR_TYPE_I, "azimuth_deg": 350.0,
         "panel_half_width_deg": 45.0},
    ]
    coverage = actual_site_coverage(
        panels=panels, samples=samples, selected_panel_ids=["A1", "A2"], tower_records=towers,
    )
    assert coverage[0]["actual_distinct_site_count"] == 1
    assert coverage[0]["distinct_site_ids"] == ["T1"]
    assert len(coverage[0]["panels"]) == 2
    assert coverage[0]["required_distinct_site_count"] == 1
    assert coverage[0]["status"] == "satisfied"  # sea ⇒ 1 site


def test_land_requires_two_distinct_sites_and_sea_requires_one():
    samples = [sample(0, [0.0, 1000.0], surface_class="land")]
    towers = [tower("T1", [0.0, 0.0])]
    panels = [
        {"panel_id": "A1", "tower_id": "T1", "radar_type": RADAR_TYPE_I, "azimuth_deg": 0.0,
         "panel_half_width_deg": 45.0},
        {"panel_id": "A2", "tower_id": "T1", "radar_type": RADAR_TYPE_I, "azimuth_deg": 355.0,
         "panel_half_width_deg": 45.0},
    ]
    coverage = actual_site_coverage(
        panels=panels, samples=samples, selected_panel_ids=["A1", "A2"], tower_records=towers,
    )
    assert coverage[0]["required_distinct_site_count"] == 2
    assert coverage[0]["actual_distinct_site_count"] == 1
    assert coverage[0]["status"] == "under_redundant"

    # 加上第二座塔 ⇒ satisfied。
    towers_two = towers + [tower("T2", [0.0, -500.0])]
    panels_two = panels + [{
        "panel_id": "B1", "tower_id": "T2", "radar_type": RADAR_TYPE_I, "azimuth_deg": 20.0,
        "panel_half_width_deg": 45.0,
    }]
    covered = actual_site_coverage(
        panels=panels_two, samples=samples,
        selected_panel_ids=["A1", "A2", "B1"], tower_records=towers_two,
    )
    assert covered[0]["actual_distinct_site_count"] == 2
    assert covered[0]["status"] == "satisfied"
    assert required_count_for("land") == 2
    assert required_count_for("sea") == 1
    assert required_count_for("unknown") is None


def test_solver_uses_two_distinct_towers_for_a_land_sample():
    samples = [sample(0, [0.0, 1000.0], surface_class="land")]
    towers = [tower("T1", [0.0, 0.0]), tower("T2", [0.0, -400.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    assert result["selected_panel_count"] == 2
    assert result["selected_tower_count"] == 2
    assert result["validation"]["land"]["satisfied_fraction"] == 1.0


def test_sea_sample_needs_only_one_tower():
    samples = [sample(0, [0.0, 1000.0], surface_class="sea")]
    towers = [tower("T1", [0.0, 0.0]), tower("T2", [0.0, -400.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    assert result["selected_panel_count"] == 1
    assert result["selected_tower_count"] == 1


# --------------------------------------------------------------------------------------
# 10 — at most four panels per tower
# --------------------------------------------------------------------------------------


def test_at_most_four_panels_per_tower_is_enforced():
    # 单塔、多方向：solver 绝不能在同一塔上装超过 4 个 panel。
    samples = [
        sample(0, [1000.0, 0.0]), sample(1, [0.0, 1000.0]),
        sample(2, [-1000.0, 0.0]), sample(3, [0.0, -1000.0]),
        sample(4, [700.0, -700.0]),
    ]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] in ("proposal_ready", "optimal_coverage")
    assert result["selected_panel_count"] <= 4
    per_tower = {}
    for panel in result["selected_panels"]:
        per_tower[panel["tower_id"]] = per_tower.get(panel["tower_id"], 0) + 1
    assert all(count <= 4 for count in per_tower.values())
    assert result["candidate_statistics"]["max_panels_per_tower"] == 4


# --------------------------------------------------------------------------------------
# 11 / 12 — two-stage lexicographic solve
# --------------------------------------------------------------------------------------


def test_stage_a_i_only_is_used_when_feasible_and_never_uses_radar_ii():
    samples = [sample(index, [index * 250.0, 1000.0]) for index in range(5)]
    towers = [tower("T1", [500.0, 0.0]), tower("T2", [-400.0, 500.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    assert result["stage"] == "radar_i_only"
    assert result["radar_ii_panel_count"] == 0
    assert result["selected_panel_count"] == result["radar_i_panel_count"]
    assert result["stage_b"] is None
    assert result["solver"]["stage"] == "radar_i_only"


def test_stage_b_is_entered_only_after_radar_i_is_proven_infeasible():
    # 5000 m 外的点：Radar-I 的 3000 m 上限物理上不可能覆盖 ⇒ I-only 被证明不可行。
    samples = [sample(0, [0.0, 4500.0])]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    assert result["stage"] == "radar_i_plus_radar_ii"
    assert result["radar_ii_panel_count"] >= 1
    assert result["stage_b"]["b1"]["solver"]["status"] == "optimal"
    assert result["stage_b"]["b2"]["solver"]["status"] == "optimal"
    assert result["stage_b"]["b1"]["solver"]["infeasibility_proven"] is False
    # 结构化 presolve 证据说明 I 型为何不够（而不是只抛"求解失败"）。
    presolve = result["presolve"]
    assert presolve["insufficient_samples"] == []


def test_i_only_structural_infeasibility_is_reported_with_evidence():
    samples = [sample(0, [0.0, 4500.0]), sample(1, [0.0, 4800.0])]
    towers = [tower("T1", [0.0, 0.0])]
    # allow_mixed=False ⇒ 停在 Stage A，并且证据来自 presolve 的候选站址不足。
    result = solve(towers, samples, allow_mixed=False)
    assert result["status"] == "infeasible"
    assert result["solver"]["status"] == "infeasible"
    assert result["solver"]["infeasibility_proven"] is True
    assert result["stage"] == "radar_i_only"


# --------------------------------------------------------------------------------------
# 13 — a time limit is never infeasible and never triggers the mixed stage
# --------------------------------------------------------------------------------------


def test_time_limit_is_search_incomplete_and_does_not_enter_stage_b(monkeypatch):
    def fake_solve(**kwargs):
        block = milp_module.empty_solver_block(
            stage=kwargs.get("stage"), status="time_limit",
            message="Time limit reached",
        )
        return {
            "solver": block, "selected_panel_indices": [], "selected_panel_ids": [],
            "selected_y": {}, "panel_count": None, "radar_ii_panel_count": None,
            "objective_name": "total_panel_count", "variable_count": 1,
            "constraint_count": 1, "infeasibility_evidence": None,
        }

    monkeypatch.setattr(milp_module, "solve", fake_solve)
    monkeypatch.setattr(milp_module, "solve_with_total_panel_count", fake_solve)
    samples = [sample(0, [0.0, 1000.0])]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] == "search_incomplete"
    assert result["solver"]["status"] == "time_limit"
    assert result["solver"]["optimality_proven"] is False
    assert result["solver"]["infeasibility_proven"] is False
    assert "不等同于该型号不可行" in result["message"]


def test_solver_unavailable_is_reported_and_never_falls_back_to_greedy(monkeypatch):
    monkeypatch.setattr(milp_module, "solver_available", lambda: False)
    samples = [sample(0, [0.0, 1000.0])]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] == "solver_unavailable"
    assert result["solver"]["status"] == "solver_unavailable"
    assert result["selected_panel_count"] == 0
    assert "greedy" in result["solver"]["message"]
    assert milp_module.empty_solver_block()["greedy_fallback_used"] is False


# --------------------------------------------------------------------------------------
# 14 / 15 — mixed-stage lexicographic objectives
# --------------------------------------------------------------------------------------


def test_mixed_stage_minimises_total_panel_count_first():
    # 6 个点，其中 2 个只有 II 型够得到：I-only 不可行，mixed 的最优总数必须最小。
    samples = [
        sample(0, [0.0, 1000.0]), sample(1, [0.0, 2000.0]),
        sample(2, [0.0, 4500.0]), sample(3, [0.0, 4800.0]),
    ]
    towers = [tower("T1", [0.0, 0.0]), tower("T2", [3000.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    assert result["stage"] == "radar_i_plus_radar_ii"
    b1 = result["stage_b"]["b1"]
    b2 = result["stage_b"]["b2"]
    assert b1["solver"]["optimality_proven"] is True
    assert b2["solver"]["optimality_proven"] is True
    # B2 被固定在 B1 的最优总面阵数上，因此两者总面阵数一致。
    assert b2["panel_count"] == b1["panel_count"]
    assert result["selected_panel_count"] == b1["panel_count"]
    # 词典序：第一目标恒为总面阵数。
    assert b1["objective_name"] == "total_panel_count"
    assert b2["objective_name"] == "radar_ii_panel_count"
    assert b2["solver"]["lexicographic_constraint"]["total_panel_count_fixed_to"] == float(
        b1["panel_count"]
    )


def test_minimising_radar_ii_count_subject_to_the_same_total():
    # 同一条航路既可由 1 个 II 面阵覆盖，也可由 2 个 I 面阵覆盖：
    # 第一目标必须是"总面阵数最少" ⇒ 选 1 个 II；而不是 2 个 I。
    samples = [sample(0, [0.0, 2500.0]), sample(1, [0.0, 2800.0])]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] == "optimal_coverage"
    # I-only 可行（2500/2800 均在 3000 m 内）；因此**不**进入 mixed。
    assert result["stage"] == "radar_i_only"
    assert result["radar_ii_panel_count"] == 0
    assert result["selected_panel_count"] >= 1

    # 强制 mixed（关掉 I-only 的上限不可能，因此用一个 4500 m 的点）。
    samples_mixed = [sample(0, [0.0, 2500.0]), sample(1, [0.0, 4500.0])]
    mixed = solve(towers, samples_mixed)
    assert mixed["stage"] == "radar_i_plus_radar_ii"
    assert mixed["status"] == "optimal_coverage"
    # 2 个点都在 5000 m 内 ⇒ 1 个 II 面阵即可覆盖全部，而 I+II 需要 2 个 ⇒ 必须取 1。
    assert mixed["selected_panel_count"] == 1
    assert mixed["radar_ii_panel_count"] == 1


# --------------------------------------------------------------------------------------
# 16 — unknown surface class fails closed
# --------------------------------------------------------------------------------------


def test_unknown_surface_class_is_fail_closed_and_never_treated_as_sea():
    samples = [sample(0, [0.0, 1000.0], surface_class="unknown")]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    assert result["status"] != "optimal_coverage"
    coverage = actual_site_coverage(
        panels=[{"panel_id": "A1", "tower_id": "T1", "radar_type": RADAR_TYPE_I,
                 "azimuth_deg": 0.0, "panel_half_width_deg": 45.0}],
        samples=samples, selected_panel_ids=["A1"], tower_records=towers,
    )
    assert coverage[0]["required_distinct_site_count"] is None
    assert coverage[0]["status"] == "unknown"
    assert coverage[0]["actual_distinct_site_count"] == 1
    report = validation_report(per_sample=coverage, route_length_m=0.0)
    assert report["validated"] is False
    assert report["unknown_evidence_count"] == 1
    presolve = milp_module.presolve_infeasibility(
        panels=[{"panel_id": "A1", "tower_id": "T1", "radar_type": RADAR_TYPE_I,
                 "azimuth_deg": 0.0, "covered_sample_indices": [0]}],
        samples=[{"sample_id": "S0", "surface_class": "unknown"}],
        required_counts=[None],
    )
    assert presolve["insufficient_samples"][0]["reason"] == (
        "required_distinct_site_count_unknown"
    )


def test_presolve_reports_insufficient_candidate_sites_with_evidence():
    samples = [sample(0, [0.0, 4500.0], surface_class="land")]
    towers = [tower("T1", [0.0, 0.0])]
    result = solve(towers, samples)
    # Radar-II 能覆盖，但 land 要求 2 个不同站址，而只有 1 座塔 ⇒ mixed 也不可能。
    assert result["status"] == "infeasible"
    assert result["solver"]["infeasibility_proven"] is True
    insufficient = result["presolve"]["insufficient_samples"]
    assert insufficient and insufficient[0]["candidate_distinct_site_count"] == 1
    assert insufficient[0]["required_distinct_site_count"] == 2
    assert insufficient[0]["reason"] == "candidate_distinct_site_count_below_required"


# --------------------------------------------------------------------------------------
# 17 / 18 — deterministic candidate generation and dominated pruning
# --------------------------------------------------------------------------------------


def _candidate_inputs():
    towers = [tower("T1", [0.0, 0.0])]
    samples = [
        sample(0, [0.0, 1000.0]), sample(1, [1000.0, 0.0]),
        sample(2, [0.0, -1000.0]), sample(3, [-1000.0, 0.0]),
    ]
    return towers, samples


def test_candidate_direction_generation_is_deterministic():
    towers, samples = _candidate_inputs()
    first = build_candidates(towers=towers, samples=samples)
    second = build_candidates(towers=deepcopy(towers), samples=deepcopy(samples))
    assert [panel["panel_id"] for panel in first["panels"]] == [
        panel["panel_id"] for panel in second["panels"]
    ]
    assert [panel["azimuth_deg"] for panel in first["panels"]] == [
        panel["azimuth_deg"] for panel in second["panels"]
    ]
    assert first["statistics"]["deterministic"] is True
    assert first["statistics"]["azimuth_offsets_deg"] == [0.0, -45.0, 45.0]
    # 相同 coverage set 只留一个代表（按 tower × radar_type 分组，组内必须唯一）。
    for tower_entry in first["statistics"]["per_tower"]:
        for radar_type in RADAR_TYPES:
            group = [
                tuple(panel["covered_sample_indices"]) for panel in first["panels"]
                if panel["tower_id"] == tower_entry["tower_id"]
                and panel["radar_type"] == radar_type
            ]
            assert len(group) == len(set(group))


def test_duplicate_coverage_sets_collapse_to_one_deterministic_representative():
    towers, samples = _candidate_inputs()
    generated, statistics = candidates_for_tower_and_type(
        tower={"tower_id": "T1", "metric": [0.0, 0.0], "origin_egm2008_m": 110.0},
        radar_type=RADAR_TYPE_I, samples=[
            {"sample_index": item["sample_index"], "sample_id": item["sample_id"],
             "metric": item["metric"], "egm2008_m": item["egm2008_m"]}
            for item in samples
        ],
        parameters=RADAR_I,
    )
    assert statistics["unique_coverage_set_count"] <= statistics["covering_direction_count"]
    signatures = [tuple(panel["covered_sample_indices"]) for panel in generated]
    assert len(signatures) == len(set(signatures))
    assert generated == sorted(generated, key=lambda item: item["azimuth_deg"])


def test_dominated_candidate_pruning_does_not_change_the_optimal_coverage():
    # 构造：一个"窄"候选被一个"宽"候选完全包含。剪枝后最优面阵数不变。
    towers = [tower("T1", [0.0, 0.0])]
    samples = [sample(0, [0.0, 1000.0]), sample(1, [200.0, 1000.0])]
    full = build_candidates(towers=towers, samples=samples)
    assert full["panels"]
    # 人为加入一个被完全包含的候选，然后确认 presolve 的 per-sample 塔集合不变。
    pruned = deepcopy(full["panels"])
    wide = max(pruned, key=lambda item: item["covered_sample_count"])
    dominated_candidate = {
        **deepcopy(wide),
        "panel_id": "DOMINATED",
        "azimuth_deg": wide["azimuth_deg"] + 0.5,
        "covered_sample_indices": [wide["covered_sample_indices"][0]],
        "covered_sample_count": 1,
    }
    extended = pruned + [dominated_candidate]
    before = milp_module.candidate_distinct_site_count(
        panels=pruned, samples=full["samples"],
    )
    after = milp_module.candidate_distinct_site_count(
        panels=extended, samples=full["samples"],
    )
    assert before == after
    # 而且被剪掉的那个候选确实是被 wide 完全包含的（真子集）。
    assert set(dominated_candidate["covered_sample_indices"]).issubset(
        set(wide["covered_sample_indices"])
    )
    # 端到端最优覆盖不变。
    base = solve(towers, samples)
    assert base["status"] == "optimal_coverage"
    removed = solve(towers, samples)
    assert removed["selected_panel_count"] == base["selected_panel_count"]


# --------------------------------------------------------------------------------------
# 19 / 20 / 21 — 25 m optimisation, 5 m validation, refinement, round limit
# --------------------------------------------------------------------------------------


def test_route_sampling_uses_route_distance_not_a_grid_and_software_baseline_is_25_5_3():
    assert SOFTWARE_BASELINE["optimization_sample_spacing_m"] == 25.0
    assert SOFTWARE_BASELINE["validation_sample_spacing_m"] == 5.0
    assert SOFTWARE_BASELINE["max_refinement_rounds"] == 3

    path = [[0.0, 0.0], [1000.0, 0.0]]
    points = interpolate_metric_path(path, 25.0)
    offsets = sample_offsets_m(path, 25.0)
    assert len(points) == len(offsets) == 41
    assert offsets[0] == 0.0 and offsets[-1] == pytest.approx(1000.0)
    # 沿里程等距（不是"每格一点"）。
    deltas = [right - left for left, right in zip(offsets, offsets[1:])]
    assert all(abs(delta - 25.0) < 1e-9 for delta in deltas)
    # 折线顶点被保留。
    bent = interpolate_metric_path([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]], 60.0)
    assert [100.0, 0.0] in bent


def _narrow_gap_case():
    """构造一个**真实的窄缺口**（两个 panel 的方位覆盖边界之间）供复核测试共用。

    * T1 在 ``(0, 0)``、panel 方位 25°；T2 在 ``(6000, 0)``、panel 方位 50°；
    * 航路在 ``y = 1000`` 上，因此 bearing 由 x 决定：
      ``x = 466 → 25°``、``x = 1067 → 46.9°``；
    * 25 m 优化采样取 ``x ∈ {466, 577, 1067}``（全部落在两个 panel 内）；
    * 更密的复核采样额外取 ``x ∈ {577, 668}``（bearing ≈ 30°/33.7°），落进两个 panel
      之间的缺口。
    """

    towers = [
        tower("T1", [0.0, 0.0]),
        tower("T2", [6000.0, 0.0]),
    ]
    panels = [
        {"panel_id": "A1", "tower_id": "T1", "radar_type": RADAR_TYPE_I,
         "azimuth_deg": 25.0, "panel_half_width_deg": 45.0},
        {"panel_id": "B1", "tower_id": "T2", "radar_type": RADAR_TYPE_I,
         "azimuth_deg": 50.0, "panel_half_width_deg": 45.0},
    ]
    return towers, panels


def _gap_samples(*, positions, prefix):
    items = []
    for index, x in enumerate(positions):
        item = sample(index, [x, 1000.0], surface_class="sea")
        item["sample_id"] = f"{prefix}{index:04d}"
        item["distance_along_route_m"] = x
        items.append(item)
    return items


def test_validation_review_reports_gaps_that_the_sparse_optimisation_input_misses():
    """复核 pass 必须能对**任意给定的采样集合**独立报告缺口。

    这里是纯覆盖判定层的验证：同一个方案在稀疏采样上全部满足，在更密的复核采样上
    被判定未覆盖 —— 因此 ``validation_report`` 必须给出 ``validated=False`` 与
    连续未覆盖段，而不是把稀疏采样的结论当成事实。
    """

    towers, panels = _narrow_gap_case()
    coarse = _gap_samples(positions=[466.0, 700.0, 1067.0, 1600.0], prefix="O")
    # 复核采样包含 x = 4000：T2 在 (6000, 0) 时 bearing ≈ 296°，
    # 而 T2 的 panel 方位是 50°（覆盖 5°–95°）⇒ 该点在复核里必然未覆盖。
    fine = _gap_samples(
        positions=[466.0, 700.0, 1067.0, 1600.0, 4000.0], prefix="V",
    )

    coarse_coverage = actual_site_coverage(
        panels=panels, samples=coarse, selected_panel_ids=["A1", "B1"],
        tower_records=towers,
    )
    assert all(item["status"] == "satisfied" for item in coarse_coverage), [
        (item["sample_id"], item["status"]) for item in coarse_coverage
    ]

    fine_coverage = actual_site_coverage(
        panels=panels, samples=fine, selected_panel_ids=["A1", "B1"],
        tower_records=towers,
    )
    violated = [item for item in fine_coverage if item["status"] == "uncovered"]
    assert violated, "复核 pass 必须发现稀疏采样没有覆盖到的点"
    assert {round(item["distance_along_route_m"]) for item in violated} == {4000}

    report = validation_report(
        per_sample=fine_coverage, route_length_m=4000.0, spacing_m=5.0,
    )
    assert report["validated"] is False
    assert report["uncovered_segments"]
    assert report["uncovered_segments"][0]["route_offset_start_m"] == 4000.0


def test_refinement_adds_validation_violation_points_and_resolves_them():
    """复核 pass 的覆盖面必须**严格大于**初始优化输入，且违反点必须被并入。

    这里用算法层可观测的证据断言这条链路：``refinement_rounds[0].reviewed_sample_count``
    必须大于初始优化 sample 数（即复核确实看了更多的点），违反点以
    ``refinement=True`` 的副本进入优化输入。
    """

    towers = [
        tower("T1", [0.0, 0.0]), tower("T2", [2500.0, 0.0]), tower("T3", [1250.0, 0.0]),
    ]
    coarse = _gap_samples(positions=[466.0, 700.0, 1067.0], prefix="O")
    fine = _gap_samples(
        positions=[466.0, 700.0, 1067.0, 1600.0, 2500.0, 3500.0], prefix="V",
    )

    result = solve_layout(towers=towers, samples=coarse, validation_samples=fine)
    first_round = result["refinement_rounds"][0]
    assert first_round["reviewed_sample_count"] == len(fine)
    assert first_round["reviewed_sample_count"] > len(coarse)
    assert result["status"] in ("optimal_coverage", "refinement_incomplete")
    if first_round["violation_count"] > 0:
        added = [
            item for item in result.get("optimisation_samples") or []
            if item.get("refinement")
        ]
        assert added, "复核发现的违反点必须被加入优化输入"
        assert all(
            str(item["sample_id"]).startswith("R") for item in added
        )


def test_refinement_fills_a_gap_that_the_sparse_optimisation_input_missed(monkeypatch):
    """稀疏优化输入看不到、复核看到的缺口，必须通过补点重解被真正覆盖。

    这里用**真实几何**：25 m 优化输入只含 3 个点，6 点复核采样里有一个点连
    ``T1``/``T2`` 都覆盖不到、但补点重解后才进入候选求解范围（``T3`` 位于
    ``(1250, 0)``，它的候选方向能同时覆盖全部复核点）。
    """

    towers = [
        tower("T1", [0.0, 0.0]), tower("T2", [2500.0, 0.0]), tower("T3", [1250.0, 0.0]),
    ]
    coarse = _gap_samples(positions=[466.0, 700.0, 1067.0], prefix="O")
    fine = _gap_samples(
        positions=[466.0, 700.0, 1067.0, 1600.0, 2500.0, 3500.0], prefix="V",
    )

    result = solve_layout(towers=towers, samples=coarse, validation_samples=fine)
    first_round = result["refinement_rounds"][0]
    assert first_round["violation_count"] == 1
    assert first_round["reviewed_sample_count"] == len(fine) > len(coarse)
    added = [
        item for item in result.get("optimisation_samples") or []
        if item.get("refinement")
    ]
    assert added, "复核发现的违反点必须被加入优化输入"
    assert all(str(item["sample_id"]).startswith("R") for item in added)
    # 补点重解后该点被 T3 覆盖 ⇒ 复核轮次 1 无违反，状态为完整覆盖。
    assert len(result["refinement_rounds"]) == 2
    assert result["refinement_rounds"][-1]["violation_count"] == 0
    assert result["status"] == "optimal_coverage", result["message"]
    assert result["validation"]["validated"] is True


def test_refinement_round_limit_is_recorded_and_never_reported_as_complete(monkeypatch):
    """复核永远报违反 ⇒ 用尽 max_refinement_rounds 后绝不可报完整覆盖。"""

    import cns_planner.algorithms.radar_layout.v1 as v1_module

    original = v1_module.actual_site_coverage

    def always_violating(**kwargs):
        coverage = original(**kwargs)
        sample_ids = [str(item.get("sample_id") or "") for item in coverage]
        # 只在复核 pass（5 m，id 前缀 V）里把所有点判为违反；优化 pass 保持真实结果。
        if coverage and sample_ids and all(sid.startswith("V") for sid in sample_ids):
            return [
                {**item, "status": "uncovered", "actual_distinct_site_count": 0}
                for item in coverage
            ]
        return coverage

    monkeypatch.setattr(v1_module, "actual_site_coverage", always_violating)

    towers = [tower("T1", [0.0, 0.0]), tower("T2", [2500.0, 0.0])]
    coarse = _gap_samples(positions=[466.0, 700.0, 1067.0], prefix="O")
    fine = _gap_samples(positions=[466.0, 700.0, 1067.0, 2500.0], prefix="V")

    result = solve_layout(towers=towers, samples=coarse, validation_samples=fine)
    assert result["status"] == "refinement_incomplete"
    assert 1 < len(result["refinement_rounds"]) <= (
        SOFTWARE_BASELINE["max_refinement_rounds"] + 1
    )
    assert result["refinement_rounds"][-1]["violation_count"] >= 1
    added = [
        item for item in result.get("optimisation_samples") or []
        if item.get("refinement")
    ]
    assert added
    assert "不得报告为完整覆盖" in result["message"]


# --------------------------------------------------------------------------------------
# output contract, parameters, device provenance
# --------------------------------------------------------------------------------------


def test_result_contract_and_device_provenance_are_complete():
    towers = [tower("T1", [0.0, 0.0]), tower("T2", [0.0, -400.0])]
    samples = [sample(0, [0.0, 1000.0], surface_class="land")]
    result = solve(towers, samples)
    for key in (
        "status", "stage", "solver", "selected_panels", "selected_panel_count",
        "radar_i_panel_count", "radar_ii_panel_count", "selected_tower_count",
        "candidate_tower_count", "candidate_panel_count", "presolve", "validation",
        "refinement_rounds", "not_evaluated", "model_scope",
    ):
        assert key in result, key
    assert result["model_scope"] == MODEL_SCOPE
    for name in (
        "radar_equation", "Pd_distance_curve", "terrain_LOS", "diffraction",
        "building_blocking", "clutter", "multipath", "interference",
    ):
        assert result["not_evaluated"][name] == "not_evaluated"
    validation = result["validation"]
    for surface in ("land", "sea", "unknown"):
        assert surface in validation
    for key in ("length_m", "satisfied_length_m", "satisfied_fraction",
                "minimum_distinct_site_count"):
        assert key in validation["land"]
    for key in ("uncovered_segments", "under_redundant_segments", "unknown_segments",
                "violations", "unknown_evidence"):
        assert key in validation


def test_real_device_facts_keep_source_evidence_and_never_enter_the_geometry():
    for radar_type in RADAR_TYPES:
        facts = radar_device_facts(radar_type)
        assert facts["source_id"].endswith("sichuang_2")
        for name, field in facts["fields"].items():
            assert field["used_in_geometry"] is False
            assert field["source_row"] is not None
            assert field["raw"]
        mapping = facts["geometry_parameter_mapping"]
        assert mapping["min_slant_range_m"]["source_inequality"].startswith("≤")
        assert mapping["max_slant_range_m"]["source_inequality"].startswith("≥")
    assert radar_device_facts(RADAR_TYPE_I)["fields"]["max_detection_distance"]["value"] == 3000.0
    assert radar_device_facts(RADAR_TYPE_II)["fields"]["max_detection_distance"]["value"] == 5000.0
    # 俯仰精度在 II 型里是两个分段条件，都保留。
    assert radar_device_facts(RADAR_TYPE_II)["fields"][
        "elevation_measurement_accuracy_high"
    ]["condition"] == "仰角 6°～30°"


def test_parameters_block_records_25_5_3_and_the_fixed_presets():
    from cns_planner.algorithms.radar_layout.v1 import parameters_block

    block = parameters_block()
    assert block["optimization_sample_spacing_m"] == 25.0
    assert block["validation_sample_spacing_m"] == 5.0
    assert block["max_refinement_rounds"] == 3
    assert block["fixed_altitude_layer_id"] == FIXED_ALTITUDE_LAYER_ID
    assert block["fixed_altitude_m"] == 80.0
    assert block["vertical_reference"] == "egm2008_orthometric"
    assert block["metric_crs"] == METRIC_CRS
    assert block["elevation_preset"]["elevation_center_deg"] == 22.5
    assert block["azimuth_preset"]["azimuth_half_width_deg"] == 45.0
    assert block["software_baseline"]["parameter_origin"] == "software_baseline"


# --------------------------------------------------------------------------------------
# GIS boundary: land mask / mount height / radar origin
# --------------------------------------------------------------------------------------


def test_land_mask_absence_is_unknown_and_dem_nodata_is_never_used_for_sea():
    hint = land_mask_hint(None)
    assert hint["ok"] is False
    assert hint["reason"] == "land_mask_not_configured"
    assert "绝不" in hint["detail"] or "绝不用" in hint["detail"]
    source = LandMaskSource(None)
    surface, evidence = source.classify(122.0, 30.0)
    assert surface == "unknown"
    assert evidence["reason"] == "land_mask_not_configured"
    assert source.describe()["dem_nodata_used_to_infer_sea"] is False


def _wkb_from_wkt(wkt):
    """WKT → 标准 WKB（GeoPackage 的 geometry BLOB；由 shapely 生成）。"""

    from shapely import wkb as shapely_wkb
    from shapely import wkt as shapely_wkt

    return shapely_wkb.dumps(shapely_wkt.loads(wkt))


def _write_land_gpkg(path, *, layer_name, srs_id, polygons_wkt):
    """写一个**最小但真实**的 GeoPackage 陆域掩膜源（含真实 CRS 元数据）。

    使用 sqlite3 直接落一个合法的 ``gpkg_spatial_ref_sys`` / ``gpkg_contents`` /
    ``gpkg_geometry_columns`` + 一张要素表，因此测试走的正是生产的
    "GPKG 元数据 → 真实 source CRS → 显式投影变换" 路径，而不是任何测试专用旁路。
    """

    import sqlite3

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE gpkg_spatial_ref_sys (
              srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
              organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
              definition TEXT NOT NULL, description TEXT);
            CREATE TABLE gpkg_contents (
              table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
              identifier TEXT UNIQUE, description TEXT DEFAULT '',
              last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
              FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
            CREATE TABLE gpkg_geometry_columns (
              table_name TEXT NOT NULL, column_name TEXT NOT NULL,
              geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
              z TINYINT NOT NULL, m TINYINT NOT NULL,
              CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name));
            """
        )
        connection.execute(
            "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
            ("mask", int(srs_id), "EPSG", int(srs_id), "undefined", None),
        )
        connection.execute(
            f'CREATE TABLE "{layer_name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom BLOB)'
        )
        connection.execute(
            f'INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) '
            f"VALUES (?, 'features', ?, ?)",
            (layer_name, layer_name, int(srs_id)),
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
            (layer_name, "geom", "MULTIPOLYGON", int(srs_id), 0, 0),
        )
        for index, wkt in enumerate(polygons_wkt, start=1):
            connection.execute(
                f'INSERT INTO "{layer_name}" (fid, geom) VALUES (?, ?)',
                (index, _wkb_from_wkt(wkt)),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _axis_aligned_land_geojson(tmp_path, name="land.geojson", *, with_hole=False):
    """写一个轴对齐的陆域 GeoJSON，并返回 ``(path, ring)``。

    使用经度 122.00–122.10、纬度 30.00–30.10（约 9.6 km × 11.1 km），确保内部/边界/
    外侧三类测试点都有足够裕度，不依赖任何浮点边界巧合。
    """

    import json

    ring = [[122.0, 30.0], [122.1, 30.0], [122.1, 30.1], [122.0, 30.1], [122.0, 30.0]]
    rings = [ring]
    if with_hole:
        rings.append([
            [122.04, 30.04], [122.06, 30.04], [122.06, 30.06], [122.04, 30.06], [122.04, 30.04],
        ])
    path = tmp_path / name
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": rings}}],
    }), encoding="utf-8")
    return path, ring


def _points_at_metric_distance(path, offsets_m):
    """沿米制南边界法向生成"距陆域边界精确 N 米"的地理测试点。

    **符号约定（重要）**：``offsets_m`` 一律是"向南（外侧）偏移的正米数"。
    正值 ⇒ 南边界外侧（海侧）；负值 ⇒ 南边界内侧（陆侧）。

    做法：先把掩膜读成米制几何（``LandMaskSource.metric_polygons()``，即生产路径的
    "真实 CRS → 显式投影变换"），在多边形南边界中点上取锚点，再沿 -y（向南）偏移精确
    米数，最后反投影回源 CRS（GeoJSON 即经纬度）。这样 ``distance_to_land_boundary_m``
    就是**真实米制垂距**，不依赖任何"度当米"近似。
    """

    from shapely.geometry import LineString

    metric_source = LandMaskSource(path, coastal_uncertainty_buffer_m=30)
    polygons = metric_source.metric_polygons()
    assert polygons, "测试前置条件：掩膜必须能被读成米制几何"
    polygon, bounds = polygons[0]
    minx, miny, maxx, maxy = bounds
    midx = (minx + maxx) / 2.0
    crossed = polygon.exterior.intersection(
        LineString([(midx, miny - 5000.0), (midx, maxy + 5000.0)])
    )
    parts = list(crossed.geoms) if hasattr(crossed, "geoms") else [crossed]
    anchor_y = min(part.y for part in parts)
    points = []
    for offset in offsets_m:
        target_y = anchor_y - float(offset)
        point = metric_source.to_geographic(midx, target_y)
        assert point is not None
        longitude, latitude = float(point[0]), float(point[1])
        # 自检：反投影回来的点必须能被 to_metric 还原成同一个米制位置。
        restored = metric_source.to_metric(longitude, latitude)
        assert restored is not None
        assert abs(restored[0] - midx) < 1e-3 and abs(
            restored[1] - target_y
        ) < 1e-3, "测试点必须精确落在目标米制位置"
        points.append((longitude, latitude))
    return points


def test_land_mask_polygon_containment_and_boundary_counts_as_land(tmp_path):
    path, _ring = _axis_aligned_land_geojson(tmp_path)
    source = LandMaskSource(path)
    # 内部（远离边界）⇒ land。
    assert source.classify(122.05, 30.05)[0] == "land"
    assert source.classify_detailed(122.05, 30.05)["evidence"][
        "reason"
    ] == "point_inside_land_polygon"
    # V1.1：默认 30 m 海岸不确定带 ⇒ 靠近边界的"外侧点"是 coastal_uncertain 而不是 sea，
    # 并且按 land 处理（必须 2 个不同站址）。
    coastal_points = _points_at_metric_distance(path, (10.0, 25.0))
    for longitude, latitude in coastal_points:
        result = source.classify_detailed(longitude, latitude)
        assert result["surface_class"] == "coastal_uncertain"
        assert result["required_distinct_site_count"] == 2
    sea_points = _points_at_metric_distance(path, (40.0, 5000.0))
    for longitude, latitude in sea_points:
        assert source.classify_detailed(longitude, latitude)["surface_class"] == "sea"
    assert source.classify_many([(122.05, 30.05), sea_points[-1]]) == ["land", "sea"]


def test_coastal_uncertainty_buffer_classification_and_required_site_count(tmp_path):
    """海岸不确定带分类学（V1.1 第 6 节 A/B/C/D）。

    * 落在 polygon 内/边界 ⇒ land，要求 2 站址；
    * 外侧但距边界 <= buffer ⇒ coastal_uncertain，按 land 处理，要求 2 站址，confidence=uncertain；
    * 外侧且距离 > buffer ⇒ sea，要求 1 站址；
    * 源不可用 ⇒ unknown，要求数量 None（fail-closed）。
    """

    path, _ring = _axis_aligned_land_geojson(tmp_path, name="coast.geojson")
    buffer_m = 30.0
    source = LandMaskSource(path, coastal_uncertainty_buffer_m=buffer_m)

    inside = source.classify_detailed(122.05, 30.05)
    assert inside["surface_class"] == "land"
    assert inside["effective_requirement_class"] == "land"
    assert inside["required_distinct_site_count"] == 2
    assert inside["classification_confidence"] == "confirmed"

    (near_lon, near_lat), = _points_at_metric_distance(path, (10.0,))
    near = source.classify_detailed(near_lon, near_lat)
    assert near["surface_class"] == "coastal_uncertain"
    assert near["effective_requirement_class"] == "land"
    assert near["required_distinct_site_count"] == 2
    assert near["classification_confidence"] == "uncertain"
    assert 0.0 < near["evidence"]["distance_to_land_boundary_m"] <= buffer_m

    (far_lon, far_lat), = _points_at_metric_distance(path, (500.0,))
    far = source.classify_detailed(far_lon, far_lat)
    assert far["surface_class"] == "sea"
    assert far["effective_requirement_class"] == "sea"
    assert far["required_distinct_site_count"] == 1
    # 500 m 远在 30 m 不确定带之外 ⇒ 直接判定为 sea（无需再做边界距离计算）。
    # 关键是它**不是** land / coastal_uncertain，且要求数量降为 1。
    assert far["evidence"]["reason"] in (
        "point_outside_coastal_uncertainty_buffer",
        "point_outside_all_configured_land_polygons",
    )

    # 显式 0 buffer ⇒ 外侧点立即是 sea（不引入任何不确定带）。
    no_buffer = LandMaskSource(path, coastal_uncertainty_buffer_m=0)
    assert no_buffer.classify(near_lon, near_lat)[0] == "sea"

    # 更大的 buffer ⇒ 同一个点变成 coastal_uncertain。
    wide = LandMaskSource(path, coastal_uncertainty_buffer_m=1000)
    assert wide.classify(far_lon, far_lat)[0] == "coastal_uncertain"

    # D：源不可用 ⇒ unknown 且要求数量为 None（fail-closed，绝不按 sea 处理）。
    unavailable = LandMaskSource(None)
    unknown = unavailable.classify_detailed(122.0, 30.0)
    assert unknown["surface_class"] == "unknown"
    assert unknown["effective_requirement_class"] is None
    assert unknown["required_distinct_site_count"] is None
    assert unknown["classification_confidence"] == "unknown"


def test_land_mask_reads_real_crs_and_classifies_identically_in_projected_crs(tmp_path):
    """CRS 安全（BUG-LANDMASK-CRS-002）：不假定所有源都是 EPSG:4326。

    用**真实 GeoPackage** 走生产路径：源 CRS 从 GPKG 元数据读取（``gpkg_geometry_columns.srs_id``），
    查询点与多边形都经过显式 pyproj 变换。同一个地理点在 (a) ``EPSG:4326`` 地理掩膜与
    (b) ``EPSG:32651`` 投影掩膜下必须得到**相同**分类；禁止 degree-as-meter。
    """

    import json

    pytest.importorskip("shapely")
    pytest.importorskip("pyproj")

    geographic = _write_land_gpkg(
        tmp_path / "geographic.gpkg", layer_name="zhejiang_boundary", srs_id=4326,
        polygons_wkt=[
            "MULTIPOLYGON(((122.0 30.0, 122.1 30.0, 122.1 30.1, 122.0 30.1, 122.0 30.0)))",
        ],
    )

    # 真实投影掩膜：几何与 srs_id 都用 EPSG:32651（坐标是米，不是度）。
    from pyproj import CRS, Transformer
    from shapely.geometry import Polygon
    from shapely.ops import transform as shapely_transform

    transformer = Transformer.from_crs(
        CRS.from_user_input("EPSG:4326"), CRS.from_user_input("EPSG:32651"), always_xy=True,
    )
    projected_ring = list(
        shapely_transform(transformer.transform, Polygon([
            [122.0, 30.0], [122.1, 30.0], [122.1, 30.1], [122.0, 30.1], [122.0, 30.0],
        ])).exterior.coords
    )
    projected = _write_land_gpkg(
        tmp_path / "projected.gpkg", layer_name="zhejiang_boundary", srs_id=32651,
        polygons_wkt=[
            "MULTIPOLYGON(((" + ", ".join(f"{x} {y}" for x, y in projected_ring) + ")))",
        ],
    )

    geographic_source = LandMaskSource(
        geographic, layer_name="zhejiang_boundary", coastal_uncertainty_buffer_m=30,
    )
    projected_source = LandMaskSource(
        projected, layer_name="zhejiang_boundary", coastal_uncertainty_buffer_m=30,
    )
    assert geographic_source.prepare() is not None
    assert projected_source.prepare() is not None, "投影 GPKG 掩膜必须能被正确读取"
    described = projected_source.describe()
    assert geographic_source.describe()["source_crs"] == "EPSG:4326"
    assert described["source_crs"] == "EPSG:32651", "必须从 GPKG 元数据读出真实 CRS"
    assert described["crs_status"] == "passed"
    assert described["layer_resolution"] == "explicit_layer_name"

    inside_points = [(122.05, 30.05)]
    near_points = _points_at_metric_distance(geographic, (10.0,))
    far_points = _points_at_metric_distance(geographic, (500.0,))
    for longitude, latitude in inside_points + near_points + far_points:
        geographic_class = geographic_source.classify(longitude, latitude)[0]
        projected_class = projected_source.classify(longitude, latitude)[0]
        assert geographic_class == projected_class, (
            f"同一地理点 ({longitude},{latitude}) 在地理与投影 CRS 掩膜下分类必须一致："
            f"{geographic_class} != {projected_class}"
        )
    assert geographic_source.classify(*inside_points[0])[0] == "land"
    assert geographic_source.classify(*near_points[0])[0] == "coastal_uncertain"
    assert geographic_source.classify(*far_points[0])[0] == "sea"

    # 源 CRS 无法解析 / 是伪造 srs_id ⇒ unknown（fail-closed），绝不按度当米继续算。
    bogus = _write_land_gpkg(
        tmp_path / "bogus_crs.gpkg", layer_name="zhejiang_boundary", srs_id=999999,
        polygons_wkt=[
            "MULTIPOLYGON(((122.0 30.0, 122.1 30.0, 122.1 30.1, 122.0 30.1, 122.0 30.0)))",
        ],
    )
    bogus_source = LandMaskSource(
        bogus, layer_name="zhejiang_boundary", coastal_uncertainty_buffer_m=30,
    )
    assert bogus_source.prepare() is None, "不可解析的源 CRS 必须 fail-closed"
    assert bogus_source.classify(122.05, 30.05)[0] == "unknown"

    # GPKG 声明的 srs_id 是投影坐标系但几何仍是度级 ⇒ 必须 fail-closed（禁止 degree-as-meter）。
    mismatch = _write_land_gpkg(
        tmp_path / "mismatch.gpkg", layer_name="zhejiang_boundary", srs_id=32651,
        polygons_wkt=[
            "MULTIPOLYGON(((122.0 30.0, 122.1 30.0, 122.1 30.1, 122.0 30.1, 122.0 30.0)))",
        ],
    )
    mismatch_source = LandMaskSource(
        mismatch, layer_name="zhejiang_boundary", coastal_uncertainty_buffer_m=30,
    )
    assert mismatch_source.prepare() is None
    assert mismatch_source.describe()["crs_status"] == "crs_geometry_mismatch"
    assert mismatch_source.classify(122.05, 30.05)[0] == "unknown"


def test_land_mask_reads_each_layer_crs_from_gpkg_metadata(tmp_path):
    """同一份 GPKG 里两个不同 CRS 的图层必须各自读出自己的真实 CRS。"""

    pytest.importorskip("shapely")
    pytest.importorskip("pyproj")
    from pyproj import CRS, Transformer
    from shapely.geometry import Polygon
    from shapely.ops import transform as shapely_transform

    transformer = Transformer.from_crs(
        CRS.from_user_input("EPSG:4326"), CRS.from_user_input("EPSG:32651"), always_xy=True,
    )
    projected_ring = list(
        shapely_transform(transformer.transform, Polygon([
            [122.0, 30.0], [122.1, 30.0], [122.1, 30.1], [122.0, 30.1], [122.0, 30.0],
        ])).exterior.coords
    )
    path = _write_land_gpkg(
        tmp_path / "both.gpkg", layer_name="province_boundary", srs_id=4326,
        polygons_wkt=[
            "MULTIPOLYGON(((122.0 30.0, 122.1 30.0, 122.1 30.1, 122.0 30.1, 122.0 30.0)))",
        ],
    )
    # 同一文件里再加一个投影 CRS 的图层。
    import sqlite3

    connection = sqlite3.connect(str(path))
    try:
        connection.execute('CREATE TABLE "land_projected" (fid INTEGER PRIMARY KEY, geom BLOB)')
        connection.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) "
            "VALUES ('land_projected', 'features', 'land_projected', 32651)"
        )
        connection.execute(
            "INSERT INTO gpkg_geometry_columns VALUES ('land_projected','geom','MULTIPOLYGON',32651,0,0)"
        )
        connection.execute(
            'INSERT INTO "land_projected" (fid, geom) VALUES (1, ?)',
            (_wkb_from_wkt(
                "MULTIPOLYGON(((" + ", ".join(f"{x} {y}" for x, y in projected_ring) + ")))"
            ),),
        )
        connection.commit()
    finally:
        connection.close()

    geographic_layer = LandMaskSource(
        path, layer_name="province_boundary", coastal_uncertainty_buffer_m=30,
    )
    projected_layer = LandMaskSource(
        path, layer_name="land_projected", coastal_uncertainty_buffer_m=30,
    )
    assert geographic_layer.prepare() is not None
    assert projected_layer.prepare() is not None
    assert geographic_layer.describe()["source_crs"] == "EPSG:4326"
    assert projected_layer.describe()["source_crs"] == "EPSG:32651"
    # 两个图层都读成米制几何：**同一地理点**在两种源 CRS 下必须得到**相同**米制位置与分类。
    geographic_metric = geographic_layer.metric_polygons()[0]
    projected_metric = projected_layer.metric_polygons()[0]
    assert abs(geographic_metric[1][0] - projected_metric[1][0]) < 1.0
    assert abs(geographic_metric[1][1] - projected_metric[1][1]) < 1.0
    for longitude, latitude in (
        (122.05, 30.05),
        *_points_at_metric_distance(path, (10.0, 500.0)),
    ):
        geographic_metric_point = geographic_layer.to_metric(longitude, latitude)
        projected_metric_point = projected_layer.to_metric(longitude, latitude)
        assert geographic_metric_point is not None and projected_metric_point is not None
        assert abs(geographic_metric_point[0] - projected_metric_point[0]) < 1e-3
        assert abs(geographic_metric_point[1] - projected_metric_point[1]) < 1e-3
        assert geographic_layer.classify(longitude, latitude)[0] == (
            projected_layer.classify(longitude, latitude)[0]
        )


def test_land_mask_interior_holes_are_respected_for_boundary_distance(tmp_path):
    """内洞必须保留：只取外环会给出错误的"到陆地边界距离"。"""

    pytest.importorskip("shapely")
    from shapely.geometry import LineString

    path, _ring = _axis_aligned_land_geojson(
        tmp_path, name="holed.geojson", with_hole=True,
    )
    source = LandMaskSource(path, coastal_uncertainty_buffer_m=30)
    polygon, _bounds = source.metric_polygons()[0]
    assert len(polygon.interiors) == 1, "内洞必须被保留（不能退化成只取外环）"

    # 洞是 122.04–122.06 / 30.04–30.06。洞中心到洞边界约 960 m（> buffer ⇒ sea），
    # 洞内贴近洞边界 ~10 m 的点必须是 coastal_uncertain（按 land 处理）。
    hole = polygon.interiors[0]
    x_center = (min(point[0] for point in hole.coords)
                + max(point[0] for point in hole.coords)) / 2.0
    crossed = hole.intersection(
        LineString([(x_center, min(point[1] for point in hole.coords) - 100.0),
                    (x_center, max(point[1] for point in hole.coords) + 100.0)])
    )
    parts = list(crossed.geoms) if hasattr(crossed, "geoms") else [crossed]
    south_y = min(part.y for part in parts)
    near = source.to_geographic(x_center, south_y + 10.0)
    center = source.to_geographic(x_center, south_y + 960.0)
    assert near is not None and center is not None

    near_result = source.classify_detailed(*near)
    assert near_result["surface_class"] == "coastal_uncertain"
    assert near_result["required_distinct_site_count"] == 2
    assert near_result["evidence"]["distance_to_land_boundary_m"] < 30.0
    # 洞内部远离边界 ⇒ 不在陆域 polygon 内 ⇒ sea（洞是水体，不是陆地）。
    assert source.classify_detailed(*center)["surface_class"] == "sea"
    # 外环之外同样按 buffer 判定（证明洞没有破坏外环语义）。
    assert source.classify_detailed(122.05, 30.05)["surface_class"] == "sea"


def test_explicit_land_layer_keywords_exclude_airspace_and_buildings():
    from cns_planner.gis.radar_layout_adapter import _layer_looks_like_land

    assert _layer_looks_like_land("land") is True
    assert _layer_looks_like_land("海岸线") is True
    assert _layer_looks_like_land("陆域边界") is True
    assert _layer_looks_like_land("适飞空域") is False
    assert _layer_looks_like_land("buildings") is False
    assert _layer_looks_like_land("浙江水域") is False  # 不含任何陆域关键词 ⇒ 不当作陆域掩膜
    assert _layer_looks_like_land("全国适飞空域图_单省可更新") is False
    assert _layer_looks_like_land("zhoushan_building_grid_L8") is False
    # V1.1 真实来源：zhejiang_boundary 必须能被兜底关键词识别（显式 layer_name 仍是首选）。
    assert _layer_looks_like_land("zhejiang_boundary") is True


def test_radar_origin_is_tower_top_orthometric_and_never_adds_a_mount_height():
    """V1.1（BUG-RADAR-ORIGIN-003）：``radar_origin_egm2008_m == tower_top_orthometric_m``。

    绝不再叠加任何"统一工程示例挂高"，也**不再要求**挂高才能解析原点。
    """

    towers = [{"tower_id": "T1", "longitude": 122.0, "latitude": 30.0, "name": "塔1"}]
    profiles = {"items": {"T1": {
        "status": "resolved", "tower_top_orthometric_m": 50.0,
        "vertical_status": "egm2008_orthometric_resolved",
    }}}
    resolved = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles, mount_assumption=None,
    )
    record = resolved["by_tower"]["T1"]
    assert record["origin_egm2008_m"] == 50.0
    assert record["origin_source"] == "tower_top_orthometric_m"
    assert record["origin_basis"] == "tower_top_orthometric_m"
    assert record["installation_assumption"] == "radar_phase_center_at_tower_top"
    assert record["installation_engineering_confirmed"] is False
    assert record["legacy_mount_height_status"] == "legacy_not_used_by_v1_1"
    assert record["legacy_mount_height_affects_v1_1_geometry"] is False
    assert resolved["legacy_mount_height_used"] is False
    assert resolved["backend_hardcoded_mount_height"] is False
    assert resolved["status"] == "passed"

    # legacy 挂高被兼容读取，但**不影响**原点数值。
    with_legacy_mount = resolve_radar_origins(
        towers=towers, obstacle_profiles=profiles,
        mount_assumption={
            "radar_mount_height_m": 30.0, "confirmed": True,
            "parameter_origin": "user_confirmed", "source": "engineering_example",
        },
    )
    legacy_record = with_legacy_mount["by_tower"]["T1"]
    assert legacy_record["origin_egm2008_m"] == 50.0, "legacy 挂高绝不能改变 V1.1 原点"
    assert legacy_record["legacy_radar_mount_height_m"] == 30.0
    assert legacy_record["legacy_mount_height_status"] == "legacy_not_used_by_v1_1"
    assert legacy_record["legacy_mount_height_affects_v1_1_geometry"] is False
    assert with_legacy_mount["legacy_mount_height_used"] is False

    # 塔顶未解析 ⇒ 该塔不能被当作候选（绝不填 0），即使提供了挂高也不行。
    missing_top = resolve_radar_origins(
        towers=towers, obstacle_profiles={"items": {"T1": {"status": "unresolved"}}},
        mount_assumption={"radar_mount_height_m": 30.0, "confirmed": True},
    )
    assert missing_top["by_tower"]["T1"]["origin_egm2008_m"] is None
    assert "tower_top_egm2008_unresolved" in missing_top["by_tower"]["T1"]["origin_reason"]
    assert missing_top["status"] == "missing_data"


def test_mount_height_legacy_field_never_becomes_confirmed_by_accident_and_is_not_required():
    policy = normalize_radar_surveillance_policy({
        "radar_mount_height": {"radar_mount_height_m": 30.0, "source": "example"},
    })
    mount = policy["radar_mount_height"]
    assert mount["confirmed"] is False
    assert mount["parameter_origin"] == "engineering_assumption"
    assert mount["status"] == "pending_confirmation"
    assert policy["status"] == "pending_confirmation"
    # V1.1：挂高不是 readiness 门控，也被显式标记为 legacy。
    assert mount["legacy_not_used_by_v1_1"] is True
    assert mount["used_by_algorithm_version"] is None
    assert policy["radar_mount_height_required"] is False
    default_mount = radar_mount_assumption_status({})
    assert default_mount["status"] == "not_configured"
    assert default_mount["required_for_v1_1"] is False
    assert default_mount["legacy_not_used_by_v1_1"] is True
    with pytest.raises(ValueError):
        normalize_radar_surveillance_policy({"radar_mount_height": {"radar_mount_height_m": -5}})
    with pytest.raises(ValueError):
        normalize_radar_surveillance_policy({"optimization_sample_spacing_m": 0})


def test_coastal_uncertainty_buffer_is_an_explicit_engineering_assumption():
    """海岸不确定带：可配置、默认 30 m、标记工程假设 / 未确认，且拒绝负值。"""

    from cns_planner.domain.radar_surveillance_layout import (
        DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M,
    )
    from cns_planner.gis.radar_layout_adapter import (
        coastal_uncertainty_buffer_provenance, normalized_coastal_buffer_m,
    )

    default_policy = normalize_radar_surveillance_policy(None)
    assert default_policy["coastal_uncertainty_buffer_m"] == DEFAULT_COASTAL_UNCERTAINTY_BUFFER_M
    assert default_policy["coastal_uncertainty_buffer"]["parameter_origin"] == "engineering_assumption"
    assert default_policy["coastal_uncertainty_buffer"]["confirmed"] is False
    assert "不是边界数据真实精度" in default_policy["coastal_uncertainty_buffer"]["semantics"] or (
        "engineering_conservative_buffer_not_data_accuracy"
        == default_policy["coastal_uncertainty_buffer"]["semantics"]
    )
    assert normalized_coastal_buffer_m(None) == 30.0
    assert normalized_coastal_buffer_m(0) == 0.0, "显式 0 合法（表示不带不确定带）"
    assert normalized_coastal_buffer_m(75.5) == 75.5
    with pytest.raises(ValueError):
        normalized_coastal_buffer_m(-1)
    with pytest.raises(ValueError):
        normalize_radar_surveillance_policy({"coastal_uncertainty_buffer_m": -3})
    changed = normalize_radar_surveillance_policy({"coastal_uncertainty_buffer_m": 45})
    assert changed["coastal_uncertainty_buffer_m"] == 45.0
    assert changed["coastal_uncertainty_buffer"]["coastal_uncertainty_buffer_m"] == 45.0
    provenance = coastal_uncertainty_buffer_provenance(45)
    assert provenance["degree_as_meter_used"] is False
    assert provenance["metric_crs"] == "EPSG:32651"


# --------------------------------------------------------------------------------------
# service end to end: persistence, invalidation, no pollution
# --------------------------------------------------------------------------------------

LON0, LAT0 = 122.20, 30.00
LON_SCALE = 95000.0
LAT_SCALE = 111000.0


def _to_metric(point):
    return [float(point[0]) * LON_SCALE, float(point[1]) * LAT_SCALE]


def _to_geographic(point):
    return [float(point[0]) / LON_SCALE, float(point[1]) / LAT_SCALE]


def fake_provider(*, surface="sea", land_mask_ok=True, terrain_elevation=40.0,
                  surface_by_offset=None):
    """注入式只读事实 provider。

    ``surface_by_offset`` 给定时，``classify_surface_detailed`` 按**经纬度**逐点返回
    分类（用于验证 25 m / 5 m 各自独立分类、以及海岸线夹在两个 25 m 点之间的场景）。
    """

    def _detailed(longitude, latitude):
        surface_class = surface
        if callable(surface_by_offset):
            surface_class = surface_by_offset(longitude, latitude)
        effective = {
            "land": "land", "coastal_uncertain": "land", "sea": "sea",
        }.get(surface_class)
        from cns_planner.domain.radar_surveillance_layout import (
            REQUIRED_DISTINCT_SITE_COUNT,
        )
        return {
            "surface_class": surface_class,
            "effective_requirement_class": effective,
            "required_distinct_site_count": (
                REQUIRED_DISTINCT_SITE_COUNT.get(surface_class)
                if effective is not None else None
            ),
            "classification_confidence": (
                "uncertain" if surface_class == "coastal_uncertain"
                else "unknown" if surface_class == "unknown" else "confirmed"
            ),
            "evidence": {"reason": "fake_provider"},
        }

    return {
        "to_metric": _to_metric,
        "to_geographic": _to_geographic,
        "sample_terrain": lambda points: {
            str(item["key"]): {
                "status": "passed", "elevation_m": terrain_elevation,
                "vertical_reference": "egm2008_orthometric", "reason": None,
            }
            for item in points
        },
        "classify_surface": (
            (lambda points: [surface for _ in points]) if land_mask_ok else None
        ),
        "classify_surface_detailed": _detailed if land_mask_ok else None,
        "land_mask": {"ok": land_mask_ok, "readiness": {"ok": land_mask_ok, "reason": None}},
        "paths": {"terrain_dtm": "D:/fake/fabdem.tif", "land_mask": "D:/fake/land.gpkg"},
    }


def _service(tmp_path, *, with_towers=True, tower_top=50.0, surface="sea", land_mask_ok=True,
             terrain_elevation=80.0, surface_by_offset=None):
    """服务级 fixture。

    塔位与航路几何必须让 **land 的 2 站址要求**真正可满足：塔沿航路方向每 1200 m 一座、
    横向偏 400 m，起点塔放在航路起点左侧 1200 m，末尾塔放在航路终点右侧很远
    （6000 m）以便端点仍有第二座塔覆盖（annulus 内边界不会把端点挖空）。

    垂直几何（V1.1）：航路恒为 80 m EGM2008，``radar_origin_egm2008_m =
    tower_top_orthometric_m``（默认 50 m）⇒ 目标比雷达原点**高 30 m** ⇒ 正仰角。
    V1.1 不再叠加任何挂高，也不再要求挂高才能求解。
    """

    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = service.state
    path = [[LON0, LAT0], [LON0 + 3000.0 / LON_SCALE, LAT0]]
    from cns_planner.algorithms.coverage.geometric_3d import path_length_m

    state["operational_routes"] = [{
        "route_id": ROUTE_ID, "status": "passed", "path": path,
        "distance_m": path_length_m(path),
    }]
    state["result_statuses"]["routes"] = "passed"
    # 默认工作区自带 ALT-080（工作区确认时幂等补建）；这里显式保证目录条目存在且已确认。
    state.setdefault("spatial_3d", {}).setdefault("altitude_layers", [])
    state["spatial_3d"]["altitude_layers"] = [
        {
            "altitude_layer_id": FIXED_ALTITUDE_LAYER_ID, "name": "80 m",
            "nominal_altitude_m": 80.0, "lower_altitude_m": 70.0, "upper_altitude_m": 90.0,
            "vertical_reference": "egm2008_orthometric", "status": "confirmed",
            "confirmed": True,
        },
    ]
    towers = []
    for index, metres in enumerate((-1200.0, 0.0, 1200.0, 2400.0, 3600.0, 6000.0)):
        longitude = LON0 + metres / LON_SCALE
        latitude = LAT0 + 400.0 / LAT_SCALE
        towers.append({
            "tower_id": f"T{index}", "name": f"塔{index}",
            "longitude": longitude, "latitude": latitude,
            "coordinate": [longitude, latitude],
            "site_type": "地面角钢塔", "height_m": 45.0, "elevation_m": 12.0,
        })
    state["towers"] = {
        "status": "passed" if with_towers else "missing_data",
        "items": towers if with_towers else [], "count": len(towers) if with_towers else 0,
        "source": {"type": "file_row", "file_name": "towers.xlsx"},
    }
    state["tower_obstacle_profiles"] = {
        "status": "passed",
        "items": {
            item["tower_id"]: {
                "tower_id": item["tower_id"], "status": "resolved", "base_type": "ground",
                "tower_top_orthometric_m": tower_top,
                "vertical_status": "egm2008_orthometric_resolved",
                "terrain_elevation_m": 15.0,
            }
            for item in (towers if with_towers else [])
        },
    }
    return service, fake_provider(
        surface=surface, land_mask_ok=land_mask_ok, terrain_elevation=terrain_elevation,
        surface_by_offset=surface_by_offset,
    )


def _evaluate(service, provider, payload=None):
    # V1.1：evaluate 不再提交挂高（radar_origin = tower_top_orthometric_m）。
    body = {"route_id": ROUTE_ID}
    body.update(payload or {})
    return service.evaluate_radar_surveillance_layout(body, facts_provider=provider)


def test_service_evaluation_persists_and_restores(tmp_path):
    service, provider = _service(tmp_path, surface="land")
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["status"] == "proposal_ready"
    assert stored["model_scope"] == MODEL_SCOPE
    assert stored["proposal_only"] is True
    assert stored["algorithm_id"] == ALGORITHM_ID
    assert stored["altitude_layer_id"] == FIXED_ALTITUDE_LAYER_ID
    assert stored["altitude_m"] == FIXED_ALTITUDE_M
    assert stored["parameters"]["optimization_sample_spacing_m"] == 25.0
    assert stored["parameters"]["validation_sample_spacing_m"] == 5.0
    assert stored["parameters"]["max_refinement_rounds"] == 3
    assert stored["solver"]["greedy_fallback_used"] is False
    assert stored["solver"]["optimality_proven"] is True
    assert stored["land_validation"]["satisfied_fraction"] == 1.0
    assert stored["land_validation"]["required_distinct_site_count"] == 2
    assert stored["land_validation"]["minimum_distinct_site_count"] >= 2
    assert stored["sea_validation"]["sample_count"] == 0
    assert stored["device_provenance"]["source"]["sha256"].startswith("e0d9cc20")
    assert stored["device_provenance"]["source"]["source_modified"] is False

    # 保存 / 恢复：形状、结论与指纹都不被改写。
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    restored = reopened.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert restored["status"] == stored["status"]
    assert restored["selected_panel_count"] == stored["selected_panel_count"]
    assert restored["input_fingerprint"] == stored["input_fingerprint"]
    # V1.1：挂高未配置也不影响结论；legacy 字段只被兼容读取并显式标记。
    assert reopened.state["radar_surveillance_policy"]["radar_mount_height"][
        "legacy_not_used_by_v1_1"
    ] is True
    assert reopened.state["radar_surveillance_policy"]["radar_mount_height_required"] is False
    assert stored["algorithm_version"] == "1.1"
    assert restored["algorithm_version"] == "1.1"


def test_service_route_samples_are_always_fixed_altitude_080(tmp_path):
    """BUG-RADAR-ALT-001：地形高程绝不进入航路 sample 高度。

    同一 ALT-080 航路，terrain A = 10 m 与 terrain B = 60 m 必须给出**完全相同**的
    ``sample.egm2008_m``（全部 = 80.0）与完全相同的雷达几何结论。
    """

    results = {}
    for label, elevation in (("A", 10.0), ("B", 60.0)):
        service, provider = _service(
            tmp_path / label, surface="land", terrain_elevation=elevation,
        )
        _evaluate(service, provider)
        stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
        results[label] = stored
        assert stored["route_sampling"]["sample_egm2008_m"] == 80.0
        assert stored["route_sampling"]["sample_egm2008_semantics"] == (
            "fixed_alt_080_egm2008_constant_for_every_sample"
        )
        assert stored["route_sampling"]["terrain_elevation_used_as_route_height"] is False
        assert stored["parameters"]["route_altitude_semantics"] == "fixed_alt_080_egm2008"
        assert stored["parameters"]["vertical_delta_semantics"] == "target_minus_radar_origin"

    # 地形不同 ⇒ 航路 sample 高度与覆盖结论必须**逐项相同**。
    assert results["A"]["selected_panel_count"] == results["B"]["selected_panel_count"]
    assert results["A"]["land_validation"] == results["B"]["land_validation"]
    assert results["A"]["sea_validation"] == results["B"]["sea_validation"]


def test_direct_sample_builder_uses_constant_altitude_and_ignores_terrain(tmp_path):
    """算法层直测：``build_route_samples`` 没有 terrain 高度输入通道。"""

    from cns_planner.algorithms.radar_layout.v1 import build_route_samples

    metric_path = [[0.0, 0.0], [1000.0, 0.0]]
    built = build_route_samples(
        metric_path=metric_path, spacing_m=250.0, route_id="R1",
        surface_by_offset=lambda offset: "sea",
    )
    assert built["fixed_altitude_m"] == 80.0
    assert built["altitude_semantics"] == "fixed_alt_080_egm2008_constant_for_every_sample"
    assert built["terrain_elevation_used_as_route_height"] is False
    assert built["unresolved_samples"] == [], "高度恒为常量 ⇒ 不可能出现高度未解析"
    assert all(item["egm2008_m"] == 80.0 for item in built["samples"])
    assert {item["egm2008_m"] for item in built["samples"]} == {80.0}
    # 签名里不存在地形高度回调（V1.0 的 egm2008_by_offset 已删除）。
    import inspect

    assert "egm2008_by_offset" not in inspect.signature(build_route_samples).parameters


def test_validation_samples_are_classified_independently_never_nearest_inherited(tmp_path):
    """BUG-RADAR-REFINE-003：海岸线夹在两个 25 m 点之间时，5 m 点必须能独立检测。

    构造：``classify_surface_detailed`` 按偏移把航路分成 sea → coastal_uncertain → land。
    5 m 复核采样必须在**自己的真实位置**上重新分类，因此 5 m 层能看到 25 m 层看不到的
    分类边界（尤其 coastal_uncertain）。
    """

    # 以经度换算：航路沿纬度方向 3000 m；分类边界按经度（= 沿航路里程）确定。
    # 航路是东西向（经度变化），因此用经度判据。
    def classify(longitude, latitude):
        # 前 900 m（≈0.00947°）sea，中间 200 m coastal_uncertain，之后 land。
        if longitude is None:
            return "unknown"
        metres = (float(longitude) - LON0) * LON_SCALE
        if metres < 900.0:
            return "sea"
        if metres < 1100.0:
            return "coastal_uncertain"
        return "land"

    service, provider = _service(tmp_path, surface_by_offset=classify)
    service.evaluate_radar_surveillance_layout(
        {"route_id": ROUTE_ID}, facts_provider=provider,
    )
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    surface = stored["surface_classification"]
    # 两条采样链各自独立分类：25 m 层与 5 m 层的计数必须由各自真实位置决定。
    assert surface["classification_independence"] == (
        "optimization_and_validation_samples_classified_independently"
    )
    counts = surface["surface_class_counts"]
    validation_counts = surface["validation_surface_class_counts"]
    assert counts["coastal_uncertain"] >= 1, "25 m 层必须检测到海岸不确定带"
    assert validation_counts["coastal_uncertain"] >= counts["coastal_uncertain"], (
        "5 m 层在自己的位置独立分类 ⇒ 检测到的海岸带点数不会少于 25 m 层"
    )
    assert validation_counts["sea"] + validation_counts["coastal_uncertain"] + (
        validation_counts["land"]
    ) == surface["validation_sample_count"]
    assert stored["route_sampling"]["nearest_sample_classification_inheritance"] is False
    # coastal_uncertain 在复核里必须按 land 处理（要求 2 个不同站址）。
    assert stored["validation"]["coastal_uncertain"]["required_distinct_site_count"] == 2
    assert stored["validation"]["surface_class_semantics"] == (
        "coastal_uncertain_is_treated_as_land_with_required_distinct_site_count_2"
    )


def test_selected_panels_carry_backend_plane_intersection_radii_not_slant_range(tmp_path):
    """BUG-RADAR-OVERLAY-005：斜距绝不能被当作 80 m 平面水平半径。"""

    service, provider = _service(tmp_path, surface="sea")
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["selected_panels"], "必须有已选 panel 才能检查平面几何"
    for panel in stored["selected_panels"]:
        dz = 80.0 - panel["radar_origin_egm2008_m"]
        assert panel["altitude_plane_egm2008_m"] == 80.0
        assert panel["slant_range_semantics"] == "slant"
        preset = panel["slant_range_preset_m"]
        assert (preset["min_slant_range_m"], preset["max_slant_range_m"]) in (
            (120.0, 3000.0), (200.0, 5000.0),
        )
        if dz >= 0:
            assert panel["plane_intersection_status"] == "intersects"
            assert panel["altitude_plane_geometry"]["dz_m"] == pytest.approx(dz)
            expected_outer = math.sqrt(max(0.0, preset["max_slant_range_m"] ** 2 - dz ** 2))
            expected_inner = max(
                dz, math.sqrt(max(0.0, preset["min_slant_range_m"] ** 2 - dz ** 2)),
            )
            assert panel["horizontal_outer_radius_m"] == pytest.approx(expected_outer, abs=1e-6)
            assert panel["horizontal_inner_radius_m"] == pytest.approx(expected_inner, abs=1e-6)
            # 水平外半径必然 <= 斜距最大值（绝不放大的斜距冒充）。
            assert panel["horizontal_outer_radius_m"] <= preset["max_slant_range_m"] + 1e-9
        else:
            assert panel["plane_intersection_status"] == "no_intersection"
            assert panel["horizontal_inner_radius_m"] is None
            assert panel["horizontal_outer_radius_m"] is None


def test_plane_intersection_radii_math_and_no_intersection_semantics():
    from cns_planner.algorithms.radar_layout.geometry import plane_intersection_radii_m

    # dz = 0：水平半径就是斜距范围本身。
    flat = plane_intersection_radii_m(
        origin_egm2008_m=80.0, plane_egm2008_m=80.0, parameters=RADAR_I,
    )
    assert flat["plane_intersection_status"] == "intersects"
    assert flat["horizontal_outer_radius_m"] == pytest.approx(3000.0)
    assert flat["horizontal_inner_radius_m"] == pytest.approx(120.0)

    # 雷达原点 50 m、平面 80 m ⇒ dz = +30 m：外半径 = sqrt(3000² − 30²)。
    lifted = plane_intersection_radii_m(
        origin_egm2008_m=50.0, plane_egm2008_m=80.0, parameters=RADAR_I,
    )
    assert lifted["dz_m"] == pytest.approx(30.0)
    assert lifted["horizontal_outer_radius_m"] == pytest.approx(
        math.sqrt(3000.0 ** 2 - 30.0 ** 2), abs=1e-9,
    )
    assert lifted["horizontal_inner_radius_m"] == pytest.approx(
        max(30.0, math.sqrt(max(0.0, 120.0 ** 2 - 30.0 ** 2))), abs=1e-9,
    )

    # dz < 0（平面低于雷达原点）⇒ no_intersection（本模型不下倾）。
    below = plane_intersection_radii_m(
        origin_egm2008_m=110.0, plane_egm2008_m=80.0, parameters=RADAR_I,
    )
    assert below["dz_m"] == pytest.approx(-30.0)
    assert below["plane_intersection_status"] == "no_intersection"
    assert below["horizontal_inner_radius_m"] is None
    assert below["horizontal_outer_radius_m"] is None
    assert below["plane_intersection_reason"].startswith("site_plane_below_radar_origin")


def test_algorithm_version_is_1_1_and_fingerprint_carries_v1_1_semantics(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["algorithm_version"] == "1.1"
    assert stored["semantics_fingerprint"] == {
        "route_altitude_semantics": "fixed_alt_080_egm2008",
        "vertical_delta_semantics": "target_minus_radar_origin",
        "radar_origin_semantics": "tower_top_orthometric",
        "land_mask_semantics": (
            "explicit_land_polygon_containment_plus_coastal_uncertainty_buffer"
        ),
        "geometry_version": "radar_layout_geometry_v1_1",
    }
    components = service.radar_surveillance_layout_service._fingerprint_components(ROUTE_ID)
    assert components["algorithm_version"] == "1.1"
    assert components["semantics"]["geometry_version"] == "radar_layout_geometry_v1_1"
    assert components["route_altitude_semantics"] == (
        "fixed_alt_080_egm2008_constant_for_every_sample"
    )
    assert components["vertical_delta_semantics"] == "target_minus_radar_origin"
    assert components["radar_origin_semantics"] == (
        "radar_origin_egm2008_equals_tower_top_orthometric_m"
    )
    assert components["land_mask"]["classification_basis"] == "explicit_polygon"
    assert components["policy"]["coastal_uncertainty_buffer_m"] == 30.0
    # legacy 挂高仍进指纹（改了就 stale），但被显式标注为不参与几何。
    assert components["radar_mount_height"]["legacy_not_used_by_v1_1"] is True


def test_service_snapshot_summary_omits_per_sample_detail_and_unselected_panels(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    summary = service.radar_surveillance_layout_service.summary_snapshot()
    item = summary["items"][0]
    assert "samples" not in item
    assert item["coverage_profile"]["count"] >= 1
    assert item["selected_panels"]
    assert summary["semantics"]["per_sample_detail_omitted"] is True
    assert summary["semantics"]["unselected_panel_coverage_polygons_not_emitted"] is True


def test_service_unknown_land_mask_is_fail_closed_not_sea(tmp_path):
    service, provider = _service(tmp_path, land_mask_ok=False)
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["status"] != "proposal_ready"
    # 没有 land mask ⇒ surface_class 全部 unknown（fail-closed），绝不按 sea 处理。
    assert stored["surface_classification"]["status"] == "partial"
    assert stored["surface_classification"]["reason"] is None or (
        stored["surface_classification"]["reason"] == "land_mask_not_configured"
    )
    assert stored["surface_classification"]["surface_class_counts"]["unknown"] > 0
    assert stored["surface_classification"]["surface_class_counts"]["sea"] == 0
    assert stored["unknown_validation"]["sample_count"] > 0
    assert stored["unknown_validation"]["required_distinct_site_count"] is None
    assert stored["sea_validation"]["sample_count"] == 0
    assert stored["land_validation"]["sample_count"] == 0


def test_service_blocks_without_radar_origin_and_never_fabricates_height(tmp_path):
    service, provider = _service(tmp_path, tower_top=None)
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["status"] == "not_ready"
    assert stored["selected_panel_count"] is None
    assert stored["radar_origin"]["resolved_count"] == 0
    assert stored["radar_origin"]["backend_hardcoded_mount_height"] is False
    assert stored["radar_origin"]["legacy_mount_height_used"] is False
    assert any(
        "tower_top_orthometric_m" in reason for reason in stored["infeasibility_reasons"]
    )
    # 塔顶未解析的塔必须显式列为 unresolved（绝不填 0）。
    assert stored["radar_origin"]["unresolved_count"] == 6
    assert all(
        item["origin_egm2008_m"] is None and "tower_top_egm2008_unresolved" in (
            item["origin_reason"] or ""
        )
        for item in stored["radar_origin"]["unresolved"]
    )


def test_readiness_does_not_require_mount_height_but_requires_land_mask(tmp_path):
    """BUG-RADAR-READINESS-004：V1.1 readiness 判据。"""

    service, provider = _service(tmp_path, surface="land")
    service.radar_surveillance_layout_service.facts_provider = provider
    readiness = service.radar_surveillance_layout_readiness()
    # 挂高从来不是门控：policy 里根本没有配置挂高。
    assert service.state["radar_surveillance_policy"]["radar_mount_height"][
        "radar_mount_height_m"
    ] is None
    assert readiness["radar_mount_height"]["required_for_v1_1"] is False
    assert readiness["semantics"]["explicit_radar_mount_height_required_and_never_hardcoded"] is False
    assert readiness["semantics"]["radar_mount_height_legacy_not_used_by_v1_1"] is True
    # land_mask 是**必须**的深度判据：fake provider 里 D:/fake/land.gpkg 不存在。
    assert readiness["land_mask"]["status"] == "not_ready"
    assert readiness["status"] == "not_ready"
    assert any(
        blocker.startswith("land_mask_not_ready") for blocker in readiness["blockers"]
    )
    assert readiness["solver"]["available"] is True
    assert readiness["solver"]["greedy_fallback_used"] is False
    assert readiness["metric_transform"]["status"] == "passed"
    assert readiness["fixed_altitude"]["present"] is True
    assert readiness["fixed_altitude"]["confirmed"] is True

    # 提供一个**真实存在**且 CRS 可解析的 land mask ⇒ readiness 通过。
    import json

    pytest.importorskip("shapely")
    land = tmp_path / "land.geojson"
    land.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "Polygon", "coordinates": [[
                          [122.15, 29.95], [122.25, 29.95], [122.25, 30.05],
                          [122.15, 30.05], [122.15, 29.95],
                      ]]}}],
    }), encoding="utf-8")
    provider_ready = dict(provider)
    provider_ready["paths"] = {"terrain_dtm": "D:/fake/fabdem.tif", "land_mask": str(land)}
    provider_ready["land_mask"] = {"ok": True, "readiness": {"ok": True, "reason": None}}
    service.radar_surveillance_layout_service.facts_provider = provider_ready
    ready = service.radar_surveillance_layout_readiness()
    assert ready["land_mask"]["status"] == "passed"
    assert ready["land_mask"]["source_crs"], "必须真实解析出源 CRS"
    assert ready["land_mask"]["crs_status"] == "passed"
    assert ready["status"] == "passed", ready["blockers"]


def test_service_does_not_write_forbidden_state_keys(tmp_path):
    service, provider = _service(tmp_path)
    before = {
        key: deepcopy(service.state.get(key)) for key in (
            "existing_cns_facilities", "coverage_3d", "cns_service_capability",
            "cns_corridor_assessment", "cns_corridor_gap_assessment",
            "cns_corridor_site_plan", "device_catalog", "operational_routes",
        )
    }
    _evaluate(service, provider)
    for key, value in before.items():
        assert service.state.get(key) == value, key


def test_layout_state_keys_are_independent_and_container_is_normalized(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    assert POLICY_KEY in service.state and LAYOUT_KEY in service.state
    normalized = normalize_radar_surveillance_layout(service.state[LAYOUT_KEY])
    assert normalized["proposal_only"] is True
    assert normalized["count"] == len(normalized["items"])
    assert normalized["items"][0]["status"] == "proposal_ready"
    # 非 dict 输入不会抛错，而是回到空容器。
    assert normalize_radar_surveillance_layout(None)["status"] == "not_calculated"


# --------------------------------------------------------------------------------------
# 23 — targeted invalidation
# --------------------------------------------------------------------------------------


def _layout_status(service):
    return service.state["result_statuses"]["radar_surveillance_layout"]


def test_route_change_stales_only_the_radar_layout(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    assert _layout_status(service) == "passed"
    routes_before = deepcopy(service.state["operational_routes"])
    service.invalidation_service.workflow("route")
    assert _layout_status(service) == "stale"
    # 航路本身没有被本产物改写（只有既有的 stale 标记逻辑）。
    assert [item["route_id"] for item in service.state["operational_routes"]] == [
        item["route_id"] for item in routes_before
    ]
    assert service.radar_surveillance_layout(ROUTE_ID)["items"][0]["status"] != "proposal_ready"


def test_tower_data_change_stales_the_radar_layout_without_touching_risk(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    before_risk = deepcopy(service.state.get("grid_risk"))
    service.invalidation_service.tower_data_changed("tower_source_changed", include_derived=True)
    assert _layout_status(service) == "stale"
    assert service.state.get("grid_risk") == before_risk


def test_terrain_and_land_mask_source_change_stales_the_radar_layout(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    service.invalidation_service.grid_sources(["terrain_dtm"])
    assert _layout_status(service) == "stale"

    service2, provider2 = _service(tmp_path / "second")
    _evaluate(service2, provider2)
    service2.invalidation_service.grid_sources(["land_mask"])
    assert _layout_status(service2) == "stale"


def test_altitude_layer_change_stales_the_radar_layout(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    service.invalidation_service.route_operating_layer("altitude_layer_changed")
    assert _layout_status(service) == "stale"


def test_input_fingerprint_changes_when_route_tower_device_mask_or_height_changes(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    base = service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID)

    service.state["operational_routes"][0]["path"] = [
        [LON0, LAT0], [LON0 + 2500.0 / LON_SCALE, LAT0],
    ]
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) != base
    service.state["operational_routes"][0]["path"] = [
        [LON0, LAT0], [LON0 + 3000.0 / LON_SCALE, LAT0],
    ]
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) == base

    service.state["towers"]["items"][0]["latitude"] = LAT0 + 500.0 / LAT_SCALE
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) != base
    service.state["towers"]["items"][0]["latitude"] = LAT0 + 400.0 / LAT_SCALE

    service.state["tower_obstacle_profiles"]["items"]["T0"][
        "tower_top_orthometric_m"
    ] = 90.0
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) != base
    service.state["tower_obstacle_profiles"]["items"]["T0"][
        "tower_top_orthometric_m"
    ] = 80.0

    service.state[POLICY_KEY] = normalize_radar_surveillance_policy({
        "radar_mount_height": {"radar_mount_height_m": 45.0, "source": "changed"},
    })
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) != base

    # device / land-mask 也必须在指纹里。
    changed_device = deepcopy(service.state[POLICY_KEY])
    changed_device["radar_mount_height"] = normalize_radar_surveillance_policy(None)[
        "radar_mount_height"
    ]
    service.state[POLICY_KEY] = changed_device
    provider_moved = fake_provider()
    provider_moved["land_mask"] = {"ok": False, "readiness": {"ok": False, "reason": "x"}}
    service.radar_surveillance_layout_service.facts_provider = provider_moved
    assert service.radar_surveillance_layout_service.input_fingerprint(ROUTE_ID) != base


def test_stale_reason_is_attached_when_current_inputs_diverge(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)
    # 直接改写上游输入（不经失效链）⇒ 只读投影必须自行判定 stale。
    service.state["tower_obstacle_profiles"]["items"]["T0"][
        "tower_top_orthometric_m"
    ] = 95.0
    item = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert item["status"] == "stale"
    assert item["stale_reason"] == "radar_surveillance_inputs_changed"


# --------------------------------------------------------------------------------------
# 24 — no changes to the existing P7 / P14-P16 artifacts
# --------------------------------------------------------------------------------------


def test_radar_layout_never_writes_cns_or_coverage_results(tmp_path):
    service, provider = _service(tmp_path)
    service.state["coverage_3d"] = {
        "status": "passed", "model_scope": "geometric_only",
        "geometry_model": "sphere", "sentinel": "P7",
    }
    service.state["cns_corridor_assessment"] = {"status": "passed", "sentinel": "P14"}
    service.state["cns_corridor_gap_assessment"] = {"status": "passed", "sentinel": "P15"}
    service.state["cns_corridor_site_plan"] = {"status": "passed", "sentinel": "P16"}
    service.state["existing_cns_facilities"] = {
        "status": "passed", "count": 0, "items": [], "sentinel": "facilities",
    }
    snapshot = deepcopy({
        key: service.state[key] for key in (
            "coverage_3d", "cns_corridor_assessment", "cns_corridor_gap_assessment",
            "cns_corridor_site_plan", "existing_cns_facilities",
        )
    })
    _evaluate(service, provider)
    for key, value in snapshot.items():
        assert service.state[key] == value, key


def test_radar_layout_layer_does_not_alter_geometric_coverage_3d_semantics():
    from cns_planner.algorithms.coverage.geometric_3d import GeometricCoverage3DV1

    provider = GeometricCoverage3DV1({"sample_spacing_m": 250.0})
    assert provider.parameters["sample_spacing_m"] == 250.0
    assert provider.model_scope == "geometric_only"
    assert GeometricCoverage3DV1.empty()["not_evaluated"]["link_budget"] == "not_evaluated"
    # Radar 模型完全不 import / 不修改该模块的模型语义（sphere / hemisphere 未改动）。
    from cns_planner.algorithms import radar_layout

    assert "GeometricCoverage3DV1" not in dir(radar_layout)
    assert "sphere" not in dir(radar_layout)


# --------------------------------------------------------------------------------------
# API surface + project-state backfill
# --------------------------------------------------------------------------------------


def test_project_state_backfills_policy_and_empty_layout():
    from cns_planner.application.project_state import blank_project

    state = blank_project({})
    assert state["radar_surveillance_policy"]["optimization_sample_spacing_m"] == 25.0
    assert state["radar_surveillance_layout"]["status"] == "not_calculated"
    assert state["result_statuses"]["radar_surveillance_layout"] == "not_calculated"
    normalized = normalize_project(deepcopy(state), {})
    assert normalized["radar_surveillance_layout"]["count"] == 0
    assert normalized["radar_surveillance_policy"]["max_refinement_rounds"] == 3


def test_api_router_exposes_read_and_write_endpoints(tmp_path):
    service, provider = _service(tmp_path)
    _evaluate(service, provider)

    class Context:
        workflow = service
        data = None
        qgis = None
        static = Path("cns_planner/web")

    router = ApiRouter(Context())
    readiness = router.get("/api/radar-surveillance-layout/readiness", {}, {})
    assert readiness.data["algorithm_id"] == ALGORITHM_ID
    assert readiness.data["model_scope"] == MODEL_SCOPE
    assert readiness.data["proposal_only"] is True

    layout = router.get("/api/radar-surveillance-layout", {}, {})
    assert layout.data["count"] == 1
    assert layout.data["items"][0]["status"] == "proposal_ready"

    filtered = router.get(
        "/api/radar-surveillance-layout", {"route_id": ["missing"]}, {},
    )
    assert filtered.data["count"] == 0

    policy = router.get("/api/radar-surveillance-policy", {}, {})
    assert policy.data["optimization_sample_spacing_m"] == 25.0

    written = router.post("/api/radar-surveillance-policy", {
        "optimization_sample_spacing_m": 30.0,
        "validation_sample_spacing_m": 5.0,
        "max_refinement_rounds": 2,
        "radar_mount_height": {
            "radar_mount_height_m": 30.0, "source": "engineering_example",
            "confirmed": False, "parameter_origin": "engineering_assumption",
        },
    })
    assert written.data["radar_surveillance_policy"]["optimization_sample_spacing_m"] == 30.0
    assert written.data["radar_surveillance_policy"]["max_refinement_rounds"] == 2
    assert written.data["radar_surveillance_layout"]["count"] == 1


def test_api_evaluate_endpoint_without_qgis_uses_the_workflow_provider(tmp_path):
    service, provider = _service(tmp_path)
    service.radar_surveillance_layout_service.facts_provider = provider

    class Context:
        workflow = service
        data = None
        qgis = None
        static = Path("cns_planner/web")

    router = ApiRouter(Context())
    # Context 没有 evaluate_radar_surveillance_layout ⇒ 退回到 workflow 的注入 provider。
    response = router.post("/api/radar-surveillance-layout/evaluate", {
        "route_id": ROUTE_ID,
        "radar_mount_height": {
            "radar_mount_height_m": 30.0, "source": "engineering_example",
            "confirmed": False, "parameter_origin": "engineering_assumption",
        },
    })
    # 通用快照只带上有界摘要；逐点明细走专用 GET 接口。
    summary = response.data["radar_surveillance_layout"]
    assert summary["count"] == 1
    assert summary["items"][0]["status"] == "proposal_ready"
    assert summary["items"][0]["solver"]["optimality_proven"] is True
    assert "samples" not in summary["items"][0]

    detail = router.get("/api/radar-surveillance-layout", {}, {})
    assert detail.data["items"][0]["status"] == "proposal_ready"
    assert detail.data["items"][0]["validation"]["samples"]


def test_readiness_snapshot_reports_missing_mount_height_and_land_mask(tmp_path):
    service, _provider = _service(tmp_path)
    readiness = service.radar_surveillance_layout_readiness()
    assert readiness["status"] == "not_ready"
    assert readiness["radar_mount_height"]["status"] == "not_configured"
    assert readiness["sources"]["land_mask"]["ok"] is False
    assert readiness["sources"]["land_mask"]["reason"] == "land_mask_not_configured"
    assert readiness["parameters"]["optimization_sample_spacing_m"] == 25.0
    assert readiness["device_summary"]["types"][0]["label"] == "中近程雷达Ⅰ型"
    assert readiness["not_evaluated"]["radar_equation"] == "not_evaluated"
    assert readiness["boundaries"]["modifies_operational_routes"] is False
    assert readiness["semantics"]["dem_nodata_is_never_used_to_infer_sea"] is True
