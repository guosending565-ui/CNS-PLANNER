"""Round 2.6.1 evidence-tiered real-tower colocation planning regressions."""

from copy import deepcopy
from pathlib import Path

import pytest
from openpyxl import Workbook

from cns_planner.application.site_candidate_actions import candidate_actions
from cns_planner.application.continuous_service_service import (
    _add_estimated_origin_disclosure,
)
from cns_planner.application.corridor_site_planning_service import (
    _origin_tier_statistics,
    _residual_gap_diagnostics,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import normalize_candidate_site, normalize_device
from cns_planner.domain.cns_planning_objectives import normalize_cns_planning_objectives
from cns_planner.domain.site_planning import REUSE_TIERS
from cns_planner.domain.tower_colocation import build_tower_colocation_candidates
from cns_planner.reference_data.towers import load_towers
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2


DEFAULTS = Path("cns_planner/config/defaults.json")


def tower(tower_id="T1", *, site_type="角钢塔"):
    return {
        "tower_id": tower_id, "name": tower_id, "longitude": 122.0, "latitude": 30.0,
        "coordinate": [122.0, 30.0], "height_m": 25.0, "site_type": site_type,
        "source_columns": {"height_m": "塔身高度(m)"},
        "source": {"file_name": "航路航线规划-铁塔数据.xlsx", "row": 2},
    }


def profile(*, resolved=False):
    return {
        "status": "resolved" if resolved else "unresolved",
        "tower_top_orthometric_m": 142.0 if resolved else None,
        "terrain_elevation_m": 17.0,
        "building_height_m": None,
        "tower_structure_height_m": 25.0,
        "base_type": "ground" if resolved else "unknown",
        "vertical_status": "resolved" if resolved else "missing_origin_type",
    }


def policy():
    return {
        "planning_host_use_confirmed": True,
        "service_origin_assumption": "tower_top_agl_0",
        "planning_service_origin_policy": "terrain_plus_source_tower_height",
        "source": "user_configuration",
    }


# ---------------------------------------------------------------------------
# reuse-first + 同层成本排序 fixture
# ---------------------------------------------------------------------------


def _rank_action(action_id, *, cost=None, unit="relative_cost_unit"):
    profile = {
        "reuse_class": "tower_colocation_host", "add_device_allowed": True,
        "source": "test", "confirmed": True,
    }
    if cost is not None:
        profile.update({"planning_cost": cost, "cost_unit": unit})
    return {
        "action_id": action_id, "eligibility": {"status": "eligible"},
        "planning_profile": profile,
    }


def _rank_impact(action_id, *, gain):
    return {
        "action_id": action_id, "status": "eligible", "regressions": [],
        "confirmed_requirement_unit_volume_gain": gain,
        "newly_met_confirmed_objectives": 0,
        "max_continuous_deficit_projection_reduction_m": 0.0,
    }


def _tower_candidates(*, enabled=True):
    collection = build_tower_colocation_candidates(
        [{
            "tower_id": "T1", "name": "T1", "longitude": 122.005, "latitude": 30.0005,
            "coordinate": [122.005, 30.0005], "height_m": 25.0, "site_type": "角钢塔",
            "source_columns": {"height_m": "塔身高度(m)"},
            "source": {"file_name": "towers.xlsx", "row": 2},
        }],
        obstacle_profiles={"items": {"T1": {
            "status": "resolved", "tower_top_orthometric_m": 142.0,
            "terrain_elevation_m": 117.0, "building_height_m": None,
            "tower_structure_height_m": 25.0, "base_type": "ground",
            "vertical_status": "resolved",
        }}},
        policy={
            "planning_host_use_confirmed": enabled,
            "service_origin_assumption": "tower_top_agl_0" if enabled else None,
            "planning_service_origin_policy": (
                "terrain_plus_source_tower_height" if enabled else None
            ),
            "source": "test",
        },
    )
    collection["items"] = [
        normalize_candidate_site(item, index) for index, item in enumerate(collection["items"])
    ]
    collection["count"] = len(collection["items"])
    collection["tower_count"] = collection["count"]
    return collection


def _new_build_candidate():
    return normalize_candidate_site({
        "site_id": "S1", "name": "S1", "coordinate": [122.005, 30.0005],
        "vertical_profile": {
            "surface_elevation_m": 0.0, "surface_vertical_reference": "egm2008_orthometric",
            "mount_height_agl_m": 100.0, "service_origin_egm2008_m": 100.0,
            "source": "test", "confirmed": True,
        },
        "site_type": "tower", "available_subsystems": ["C"],
        "usable": True, "locked": False, "source": "test",
        "planning_profile": {
            "reuse_class": "new_build_candidate", "add_device_allowed": True,
            "source": "test", "confirmed": True,
            "planning_cost": 10.0, "cost_unit": "relative_cost_unit",
        },
    })


def _communication_device():
    return normalize_device({
        "device_id": "C1", "name": "C1", "subsystem": "C", "role": "existing",
        "radius_m": 500.0, "mtbf_h": 1000,
        "type": {
            "technology": "dedicated_radio", "network_scope": "dedicated",
            "interfaces": ["ip"],
        },
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": 500.0,
            "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {
                "performance": {"max_latency_s": 0.2},
                "independence_confirmed": True, "independence_group": "group-a",
            },
            "source": "test", "confirmed": True,
        },
    })


def _reuse_first_workflow(tmp_path, *, tower_policy_enabled=True):
    """最小的真实走廊 fixture：共塔候选 + 更便宜的新建候选都能解决同一缺口。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[122.0, 30.0], [122.01, 30.0]],
    }]
    state["grid"] = {"status": "passed", "level": 1, "cells": [
        {"grid_id": "G1", "bbox": [122.0, 30.0, 122.01, 30.001]},
    ]}
    state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }},
        "site_vertical_profiles": {},
    }
    state["required_cns"] = {
        "status": "passed",
        "project_default": {
            "communication": {
                "required": True, "status": "passed",
                "type": {
                    "technology": "dedicated_radio", "network_scope": "dedicated",
                    "interfaces": ["ip"],
                },
                "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
            },
            "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {},
    }
    state["aircraft_profiles"] = {"status": "passed", "items": [{
        "aircraft_id": "A1", "name": "A1", "cruise_speed_mps": 20.0, "max_speed_mps": 25.0,
        "communication": {
            "confirmed": True, "status": "confirmed",
            "type": {
                "technology": "dedicated_radio", "network_scope": "dedicated",
                "interfaces": ["ip"],
            },
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        },
    }]}
    state["selected_aircraft_profile_id"] = "A1"
    state["device_catalog"] = {"status": "passed", "items": [_communication_device()]}
    state["existing_cns_facilities"] = {"status": "passed", "items": [], "count": 0}
    state["candidate_sites"] = {
        "status": "passed", "items": [_new_build_candidate()], "count": 1,
    }
    state["tower_colocation_candidates"] = _tower_candidates(enabled=tower_policy_enabled)
    state["tower_colocation_policy"] = state["tower_colocation_candidates"]["policy"]
    state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    state["cns_planning_objectives"] = normalize_cns_planning_objectives(None)
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    return workflow


def test_confirmed_and_estimated_origins_are_separate_and_estimate_does_not_overwrite_fact():
    result = build_tower_colocation_candidates(
        [tower("A"), tower("B", site_type="楼面角钢塔")],
        obstacle_profiles={"items": {"A": profile(resolved=True), "B": profile()}},
        policy=policy(),
    )
    assert result["confirmed_origin_count"] == 1
    assert result["estimated_planning_origin_count"] == 1
    assert result["unusable_count"] == 0
    confirmed, estimated = result["items"]
    assert confirmed["vertical_profile"]["service_origin_egm2008_m"] == 142.0
    assert confirmed["vertical_profile"]["planning_origin_status"] == "confirmed"
    assert estimated["vertical_profile"]["service_origin_egm2008_m"] is None
    assert estimated["vertical_profile"]["planning_service_origin_egm2008_m"] == 42.0
    assert estimated["vertical_profile"]["planning_origin_status"] == "estimated"
    assert estimated["vertical_profile"]["planning_origin_authority"] == "engineering_estimate"
    assert estimated["metadata"]["obstacle_profile"]["tower_top_orthometric_m"] is None
    assert "未计入潜在建筑屋面高度" in "".join(
        estimated["vertical_profile"]["planning_origin_limitations"]
    )


def test_tier_b_is_planning_eligible_but_never_physical_mount_confirmed():
    collection = build_tower_colocation_candidates(
        [tower()], obstacle_profiles={"items": {"T1": profile()}}, policy=policy(),
    )
    device = {
        "device_id": "C1", "subsystem": "C", "enabled": True,
        "service_key": "C:communication",
        "coverage_geometry": {
            "model": "sphere", "confirmed": True,
            "radius_by_surface": {"land": 4000.0, "sea": 4000.0},
        },
        "service_model": {"model_family": "geometric_only", "confirmed": True},
    }
    action = candidate_actions(
        [{"subsystem": "C", "service_key": "C:communication"}],
        {"items": []}, {"items": []}, {"items": [device]}, collection,
    )[0]
    assert action["eligibility"]["status"] == "eligible"
    assert action["origin_status"] == "estimated"
    assert action["service_origin_egm2008_m"] == 42.0
    assert action["physical_mount_confirmed"] is False
    assert action["requires_site_survey"] is True


def test_planning_policy_invalidation_preserves_route_safety_and_r0005(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["operational_routes"] = [{
        "route_id": "R0005", "status": "passed", "stale_reason": None,
        "altitude_layer_id": "ALT-100",
    }]
    state["layered_route_candidates"] = {"status": "passed", "items": [{"candidate_id": "C1"}]}
    state["layered_route_validations"] = {"status": "passed", "items": [{"validation_id": "V1"}]}
    state["v3_operational_adoptions"] = {"status": "passed", "items": [{"adoption_id": "A1"}]}
    for key in ("layered_route_candidate", "layered_route_validation", "v3_operational_adoption"):
        state["result_statuses"][key] = "passed"
    state["cns_corridor_site_plan"] = {"status": "proposal_ready"}
    state["continuous_service_acceptability"] = {"status": "fully_satisfied"}
    state["cns_plan_review"] = {"status": "initialized"}
    workflow.invalidation_service.tower_planning_policy_changed()
    assert state["operational_routes"][0]["status"] == "passed"
    assert state["operational_routes"][0]["stale_reason"] is None
    assert state["layered_route_candidates"]["status"] == "passed"
    assert state["layered_route_validations"]["status"] == "passed"
    assert state["v3_operational_adoptions"]["status"] == "passed"
    assert state["cns_corridor_site_plan"]["status"] == "stale"
    assert state["continuous_service_acceptability"]["status"] == "stale"
    assert state["cns_plan_review"]["status"] == "stale"


def _tower_facts(_towers, _state):
    return {"terrain": {"T1": {"status": "passed", "elevation_m": 17.0,
                               "vertical_reference": "egm2008_orthometric"}}}


def test_policy_only_change_stales_planning_chain_but_not_route_safety(tmp_path):
    """只改共塔规划策略 ⇒ 只 stale P16/P17/review；R0005 与验证/采纳必须保持 passed。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["towers"] = {"status": "passed", "count": 1, "items": [tower()]}
    state["operational_routes"] = [{
        "route_id": "R0005", "status": "passed", "stale_reason": None,
    }]
    state["layered_route_candidates"] = {"status": "passed", "items": [{"candidate_id": "C1"}]}
    state["layered_route_validations"] = {"status": "passed", "items": [{"validation_id": "V1"}]}
    state["v3_operational_adoptions"] = {"status": "passed", "items": [{"adoption_id": "A1"}]}
    state["cns_corridor_site_plan"] = {"status": "proposal_ready"}
    state["continuous_service_acceptability"] = {"status": "fully_satisfied"}
    state["cns_plan_review"] = {"status": "initialized"}

    workflow.evaluate_tower_obstacle_profiles(
        {"tower_colocation_policy": policy()}, facts_provider=_tower_facts,
    )
    assert state["operational_routes"][0]["status"] == "passed"
    assert state["operational_routes"][0]["stale_reason"] is None
    assert state["layered_route_candidates"]["status"] == "passed"
    assert state["layered_route_validations"]["status"] == "passed"
    assert state["v3_operational_adoptions"]["status"] == "passed"
    assert state["cns_corridor_site_plan"]["status"] == "stale"
    assert state["continuous_service_acceptability"]["status"] == "stale"
    assert state["cns_plan_review"]["status"] == "stale"
    #: 规划原点估计照常在该次评估中派生（策略变更绝不阻止估计层工作）。
    assert state["tower_colocation_candidates"]["estimated_planning_origin_count"] == 1


def test_physical_tower_refresh_still_uses_the_physical_invalidation_chain(tmp_path):
    """策略未变、只是重新解析塔障碍物事实 ⇒ 仍走物理链（航路候选与 P16 一起 stale）。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = workflow.state
    state["towers"] = {"status": "passed", "count": 1, "items": [tower()]}
    state["tower_colocation_policy"] = policy()
    state["layered_route_candidates"] = {"status": "passed", "items": [{"candidate_id": "C1"}]}
    state["cns_corridor_site_plan"] = {"status": "proposal_ready"}

    workflow.evaluate_tower_obstacle_profiles(facts_provider=_tower_facts)
    assert state["layered_route_candidates"]["status"] == "stale"
    assert state["cns_corridor_site_plan"]["status"] == "stale"


def test_importer_records_original_height_column_and_semantics(tmp_path):
    path = tmp_path / "towers.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "站点清单"
    sheet.append(["所属站址编码", "站址名称", "经度", "纬度", "海拔高度(m)", "塔身高度(m)"])
    sheet.append(["T1", "测试塔", 122.0, 30.0, 999.0, 25.0])
    workbook.save(path)
    loaded = load_towers(path)
    assert loaded["items"][0]["height_m"] == 25.0
    assert loaded["items"][0]["source_columns"]["height_m"] == "塔身高度(m)"
    semantics = loaded["metadata"]["height_field_semantics"]
    assert semantics["source_columns"] == ["塔身高度(m)"]
    assert semantics["unit"] == "m"
    assert semantics["authority"] == "engineering_assumption"
    assert "海拔高度" not in semantics["source_columns"]


def test_policy_and_planning_origin_survive_save_reopen(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    workflow.state["towers"] = {"status": "passed", "count": 1, "items": [tower()]}

    def facts(_towers, _state):
        return {"terrain": {"T1": {"status": "passed", "elevation_m": 17.0,
                                    "vertical_reference": "egm2008_orthometric"}}}

    workflow.evaluate_tower_obstacle_profiles(
        {"tower_colocation_policy": policy()}, facts_provider=facts,
    )
    reopened = WorkflowService(path, DEFAULTS)
    saved_policy = reopened.state["tower_colocation_policy"]
    assert saved_policy["planning_service_origin_policy"] == "terrain_plus_source_tower_height"
    saved = reopened.state["tower_colocation_candidates"]
    assert saved["estimated_planning_origin_count"] == 1
    assert saved["items"][0]["vertical_profile"]["planning_origin_status"] == "estimated"


def test_p16_tower_origin_statistics_exclude_non_tower_sites_and_keep_tiers_distinct():
    actions = [
        {"action_id": "TA", "service_key": "C:communication",
         "reuse_class": "tower_colocation_host", "origin_status": "confirmed",
         "eligibility": {"status": "eligible"}},
        {"action_id": "TB", "service_key": "C:communication",
         "reuse_class": "tower_colocation_host", "origin_status": "estimated",
         "eligibility": {"status": "eligible"}},
        {"action_id": "SYN", "service_key": "C:communication",
         "reuse_class": "candidate_site", "origin_status": "confirmed",
         "eligibility": {"status": "eligible"}},
    ]
    counts = _origin_tier_statistics(actions, [actions[1]])["C:communication"]
    assert counts == {
        "confirmed_origin": {"candidate": 1, "eligible": 1, "selected": 0},
        "estimated_origin": {"candidate": 1, "eligible": 1, "selected": 1},
    }


def test_p17_estimated_origin_disclosure_preserves_acceptable_status():
    result = {"status": "acceptable_with_managed_gap", "disclosure_lines": []}
    _add_estimated_origin_disclosure(result, [{
        "tower_id": "T-B", "origin_status": "estimated",
        "requires_site_survey": True,
    }])
    assert result["status"] == "acceptable_with_managed_gap"
    assert result["estimated_origin_dependency_count"] == 1
    assert result["estimated_origin_tower_ids"] == ["T-B"]
    assert result["site_survey_required"] is True
    assert "实施前需现场勘察确认" in "".join(result["disclosure_lines"])


def test_no_eligible_proposal_reports_nearby_tower_evidence_blockers():
    gap = {"routes": [{
        "route_id": "R1",
        "subsystems": [{
            "subsystem": "C",
            "continuous_deficit_segments": [{
                "segment_id": "G1", "start_route_offset_m": 0.0,
                "end_route_offset_m": 1000.0, "length_m": 1000.0,
            }],
            "service_redundancy": [{
                "service_key": "C:communication",
                "surface_class_counts": {"sea": 1},
                "required_distinct_site_count_by_surface": {"sea": 1},
            }],
        }],
    }]}
    action = {
        "action_id": "T1:C1", "reuse_class": "tower_colocation_host",
        "service_key": "C:communication", "tower_id": "T1",
        "device_id": "C1", "coordinate": [122.0001, 30.0],
        "origin_status": "estimated", "device_type": {"network_scope": "unknown"},
        "eligibility": {"status": "eligible"},
    }
    state = {
        "operational_routes": [{"route_id": "R1", "path": [[122.0, 30.0], [122.01, 30.0]]}],
        "required_cns": {"project_default": {"communication": {"type": {
            "technology": "dedicated_radio", "network_scope": "dedicated",
            "interfaces": ["ip"],
        }}}},
        "selected_aircraft_profile_id": "FC30",
        "aircraft_profiles": {"items": [{"aircraft_id": "FC30", "cruise_speed_mps": 15.0}]},
        "device_catalog": {"items": [{
            "device_id": "C1",
            "coverage_geometry": {"confirmed": True, "model": "sphere",
                                  "radius_by_surface": {"sea": 4000.0}},
        }]},
    }
    diagnostics = _residual_gap_diagnostics(gap, [action], state)
    assert len(diagnostics) == 1
    assert diagnostics[0]["duration_s"] == 1000.0 / 15.0
    nearby = diagnostics[0]["nearby_real_towers"][0]
    assert nearby["tower_id"] == "T1"
    assert nearby["origin_tier"] == "estimated"
    #: Round 2.6.1：残余原因必须来自**真实门禁**（P8 第一步 provider 类型资格），
    #: 而不是把某一侧写死；缺声明是证据不足，绝不冒充"已确认不兼容"。
    assert nearby["residual_reason_category"] == "provider_type_incompatibility"
    assert "network_scope" in nearby["why_cannot_eliminate_gap"]


def test_no_eligible_proposal_reports_aircraft_participation_gap_for_rid():
    """provider 类型资格满足、机载参与能力缺失时，原因必须落在机载侧。"""

    gap = {"routes": [{
        "route_id": "R1",
        "subsystems": [{
            "subsystem": "S",
            "continuous_deficit_segments": [{
                "segment_id": "G1", "start_route_offset_m": 0.0,
                "end_route_offset_m": 1000.0, "length_m": 1000.0,
            }],
            "service_redundancy": [{
                "service_key": "S:rid_cooperative",
                "surface_class_counts": {"sea": 1},
                "required_distinct_site_count_by_surface": {"sea": 1},
            }],
        }],
    }]}
    action = {
        "action_id": "T1:R1", "reuse_class": "tower_colocation_host",
        "service_key": "S:rid_cooperative", "tower_id": "T1",
        "device_id": "RID1", "coordinate": [122.0001, 30.0],
        "origin_status": "confirmed",
        "device_type": {
            "target_cooperation": "cooperative", "sensor_mode": "passive",
            "technology": "network_remote_id",
            "service_subtype": "cooperative_surveillance",
        },
        "eligibility": {"status": "eligible"},
    }
    state = {
        "operational_routes": [{"route_id": "R1", "path": [[122.0, 30.0], [122.01, 30.0]]}],
        "required_cns": {"project_default": {"surveillance": {"type": {
            "target_cooperation": "cooperative", "sensor_mode": "passive",
            "technology": "network_remote_id",
            "service_subtype": "cooperative_surveillance",
        }}}},
        "selected_aircraft_profile_id": "FC30",
        "aircraft_profiles": {"items": [{
            "aircraft_id": "FC30", "cruise_speed_mps": 15.0,
            "surveillance": {"type": {"technology": "adsb", "target_cooperation": "cooperative"}},
        }]},
        "device_catalog": {"items": [{
            "device_id": "RID1",
            "coverage_geometry": {"confirmed": True, "model": "sphere",
                                  "radius_by_surface": {"sea": 5000.0}},
        }]},
    }
    diagnostics = _residual_gap_diagnostics(gap, [action], state)
    nearby = diagnostics[0]["nearby_real_towers"][0]
    assert nearby["residual_reason_category"] == "aircraft_participation_evidence_required"
    assert "cooperative_surveillance_services" in nearby["why_cannot_eliminate_gap"]
    assert "ADS-B" not in nearby["why_cannot_eliminate_gap"]


def test_same_reuse_tier_ranks_by_gain_per_confirmed_explicit_cost():
    """同一 reuse tier 内：成本单位一致且均为已确认成本时，用 gain / cost 排序。"""

    planner = CorridorReuseFirstSitePlannerV2()
    actions = [_rank_action("A", cost=10.0), _rank_action("B", cost=20.0)]
    impacts = [_rank_impact("A", gain=100.0), _rank_impact("B", gain=100.0)]
    ranked = planner.rank(actions, impacts)
    assert [item["action"]["action_id"] for item in ranked] == ["A", "B"]
    assert ranked[0]["explicit_cost"] == 10.0
    assert ranked[0]["score_semantics"] == "confirmed_unit_volume_gain_per_explicit_cost"
    assert ranked[0]["selection_score"] == pytest.approx(10.0)
    assert ranked[1]["selection_score"] == pytest.approx(5.0)


def test_same_reuse_tier_uses_action_count_proxy_when_cost_is_missing():
    """成本资料缺失时绝不伪造价格：退回 coverage / action-count proxy 排序。"""

    planner = CorridorReuseFirstSitePlannerV2()
    actions = [
        {**_rank_action("A"), "planning_profile": {"reuse_class": "candidate_site",
                                                   "confirmed": True}},
        _rank_action("B", cost=20.0),
    ]
    impacts = [_rank_impact("A", gain=10.0), _rank_impact("B", gain=100.0)]
    ranked = planner.rank(actions, impacts)
    assert {item["score_semantics"] for item in ranked} == {
        "confirmed_unit_volume_gain_per_action_count_proxy"
    }
    assert [item["action"]["action_id"] for item in ranked] == ["B", "A"]


def test_reuse_tier_order_keeps_new_build_behind_every_reuse_tier():
    """正式业务规则：reuse-first 优先于全局成本，新建塔永远排在最后。"""

    assert REUSE_TIERS == (
        "existing_cns_facility", "existing_shared_site", "tower_colocation_host",
        "candidate_site", "new_build_candidate",
    )
    assert REUSE_TIERS.index("tower_colocation_host") < REUSE_TIERS.index("new_build_candidate")


def test_tower_colocation_is_selected_before_cheaper_new_build_candidate(tmp_path):
    """端到端：共塔（无显式成本）与更便宜的新建候选解决同一缺口时，先选共塔。"""

    workflow = _reuse_first_workflow(tmp_path)
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "proposal_ready"
    selected = result["selected_actions"]
    assert [item["reuse_class"] for item in selected][0] == "tower_colocation_host"
    assert result["reuse_counts"]["tower_colocation_host"] == 1
    assert result["reuse_counts"]["new_build_candidate"] == 0
    assert selected[0]["tower_id"] == "T1"
    assert selected[0]["physical_mount_confirmed"] is False
    assert selected[0]["requires_site_survey"] is True


def test_new_build_is_reachable_only_after_tower_tier_has_no_action(tmp_path):
    """只有共塔 tier 无可行动作（规划宿主未确认）时，才允许进入新建候选。"""

    workflow = _reuse_first_workflow(tmp_path, tower_policy_enabled=False)
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "proposal_ready"
    selected = result["selected_actions"]
    assert "tower_colocation_host" not in {item["reuse_class"] for item in selected}
    assert result["reuse_counts"]["new_build_candidate"] == 1
