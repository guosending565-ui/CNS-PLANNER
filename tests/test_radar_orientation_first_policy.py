from cns_planner.algorithms.radar_layout import milp
from cns_planner.algorithms.radar_layout.candidates import candidates_for_tower_and_type
from cns_planner.algorithms.radar_layout.v1 import solve_layout
from cns_planner.application.radar_surveillance_layout_service import (
    normalize_radar_surveillance_policy,
)
from cns_planner.domain.radar_surveillance_layout import (
    RADAR_TYPE_I, RADAR_TYPE_II, radar_geometry_parameters,
)


def tower(tower_id="T1", xy=(0.0, 0.0)):
    return {"tower_id": tower_id, "metric": list(xy), "origin_egm2008_m": 50.0}


def sample(index, xy, required=1):
    return {"sample_id": f"S{index}", "metric": list(xy), "egm2008_m": 80.0,
            "surface_class": "sea", "required_distinct_site_count": required,
            "distance_along_route_m": float(index * 5)}


def azimuths(samples):
    panels, _ = candidates_for_tower_and_type(
        tower=tower(), radar_type=RADAR_TYPE_I, samples=samples,
        parameters=radar_geometry_parameters(RADAR_TYPE_I),
    )
    return {item["azimuth_deg"] for item in panels}


def test_route_direction_changes_bearing_derived_candidate_azimuths():
    assert azimuths([sample(0, (1000.0, 0.0))]) != azimuths([sample(0, (0.0, 1000.0))])


def test_milp_selects_the_direction_that_adds_coverage():
    panels = [
        {"panel_id": "east", "tower_id": "T1", "radar_type": RADAR_TYPE_I,
         "covered_sample_indices": [0]},
        {"panel_id": "north", "tower_id": "T1", "radar_type": RADAR_TYPE_I,
         "covered_sample_indices": [0, 1]},
    ]
    solved = milp.solve(panels=panels, samples=[sample(0, (1, 0)), sample(1, (0, 1))],
                        required_counts=[1, 1], radar_types=[RADAR_TYPE_I])
    assert solved["solver"]["status"] == "optimal"
    assert solved["selected_panel_ids"] == ["north"]


def test_reorientation_feasible_never_reports_gap():
    result = solve_layout(towers=[tower()], samples=[sample(0, (1000.0, 0.0))])
    assert result["status"] == "optimal_coverage"
    assert result["gap_classification"] == "none"


def test_only_proven_radar_i_infeasibility_is_confirmed_gap():
    result = solve_layout(towers=[tower()], samples=[sample(0, (4000.0, 0.0))])
    assert result["status"] == "infeasible"
    assert result["managed_physical_gap"] is True
    assert result["gap_reason"] in {"range_limited", "independent_site_count_limited"}


def test_time_limit_is_search_incomplete_not_infeasible(monkeypatch):
    monkeypatch.setattr(milp, "solve", lambda **kwargs: {
        "solver": milp.empty_solver_block(status="time_limit", stage="radar_i_only"),
        "selected_panel_ids": [], "panel_count": None, "radar_ii_panel_count": 0,
    })
    result = solve_layout(towers=[tower()], samples=[sample(0, (1000.0, 0.0))])
    assert result["status"] == "search_incomplete"
    assert result["managed_physical_gap"] is False


def test_new_project_defaults_to_gated_radar_ii_escalation():
    """Round31-C：新项目默认"优先 Radar-I"，仅在**严格证明不可行**后升级既有站址 Radar-II。"""

    policy = normalize_radar_surveillance_policy(None)
    assert policy["allowed_radar_types"] == [RADAR_TYPE_I, RADAR_TYPE_II]
    assert policy["allow_automatic_radar_ii_escalation"] is True
    assert policy["allow_mixed_radar_types"] is True
    assert policy["escalation_policy_id"] == (
        "radar_i_first_existing_site_radar_ii_escalation"
    )

    #: 算法层默认仍是保守的 I-only 引擎语义：**不传** allow_mixed 时绝不升级。
    result = solve_layout(towers=[tower()], samples=[sample(0, (4000.0, 0.0))])
    assert result["radar_ii_panel_count"] == 0
    assert result["stage"] == "radar_i_only"
    assert all(item["radar_type"] != RADAR_TYPE_II for item in result["selected_panels"])
    assert result["escalation"]["escalated"] is False
    assert result["escalation"]["stage_a_infeasibility_proven"] is True
    assert result["escalation"]["escalation_blocked_reason"] == (
        "escalation_not_allowed_by_policy"
    )

    #: 只有显式允许（正式 service policy 的调用方式）才进入 Stage B，
    #: 并且只使用既有物理站址、绝不新建站址。
    escalated = solve_layout(
        towers=[tower()], samples=[sample(0, (4000.0, 0.0))], allow_mixed=True,
    )
    assert escalated["stage"] == "radar_i_plus_radar_ii"
    assert escalated["escalation"]["escalated"] is True
    assert escalated["escalation"]["existing_sites_only"] is True
    assert escalated["escalation"]["new_sites_created"] is False
    assert escalated["radar_ii_panel_count"] >= 1
    assert escalated["radar_ii_site_count"] == 1
