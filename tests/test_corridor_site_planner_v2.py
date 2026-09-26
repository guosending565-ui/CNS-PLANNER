from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.application.corridor_site_planning_service import _impact
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import normalize_candidate_site, normalize_device, normalize_existing_facility
from cns_planner.domain.cns_planning_objectives import normalize_cns_planning_objectives
from cns_planner.domain.corridor_site_planning import default_corridor_site_planning_policy
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2


DEFAULTS = Path("cns_planner/config/defaults.json")


def vertical():
    return {
        "surface_elevation_m": 0.0, "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 100.0, "service_origin_egm2008_m": 100.0,
        "source": "test", "confirmed": True,
    }


def planning_profile(reuse_class="candidate_site", cost=None, unit=None):
    result = {"reuse_class": reuse_class, "add_device_allowed": True, "source": "test", "confirmed": True}
    if cost is not None:
        result.update({"planning_cost": cost, "cost_unit": unit or "credits"})
    return result


def device(identifier="C1", group="group-a", radius=500.0):
    return normalize_device({
        "device_id": identifier, "name": identifier, "subsystem": "C", "role": "existing",
        "radius_m": radius, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {"model": "sphere", "slant_range_m": radius, "source": "test", "confirmed": True},
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {
                "performance": {"max_latency_s": 0.2},
                "independence_confirmed": True, "independence_group": group,
            },
            "source": "test", "confirmed": True,
        },
    })


def required(redundancy=1, *, navigation=False):
    values = {
        "communication": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }
    if navigation:
        values["navigation"] = {
            "required": True, "status": "passed", "type": {"technology": "gnss"},
            "performance": {"max_horizontal_error_m": 5.0, "min_redundancy": redundancy},
        }
    else:
        values["communication"] = {
            "required": True, "status": "passed",
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": redundancy},
        }
    return {"status": "passed", "project_default": values, "route_overrides": {}}


def aircraft(*, navigation=False):
    result = {"aircraft_id": "A1", "name": "A1", "communication": {}, "navigation": {}, "surveillance": {}}
    if navigation:
        result["navigation"] = {
            "confirmed": True, "status": "confirmed", "capabilities": ["pnt"],
            "type": {"technology": "gnss"},
            "performance": {"max_horizontal_error_m": 2.0, "min_redundancy": 1},
        }
    else:
        result["communication"] = {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        }
    return result


def candidate(identifier, reuse="candidate_site", cost=None, unit=None):
    return normalize_candidate_site({
        "site_id": identifier, "name": identifier, "coordinate": [0.0, 0.0005],
        "vertical_profile": vertical(), "site_type": "tower", "available_subsystems": ["C"],
        "usable": True, "locked": False, "source": "test",
        "planning_profile": planning_profile(reuse, cost, unit),
    })


def existing(identifier="F1", reuse="existing_shared_site"):
    return normalize_existing_facility({
        "facility_id": identifier, "site_id": identifier, "name": identifier,
        "coordinate": [0.0, 0.0005], "vertical_profile": vertical(),
        "devices": [], "status": "active", "source": "test",
        "planning_profile": planning_profile(reuse),
    })


def configured(tmp_path, *, redundancy=1, devices=None, candidates=None, facilities=None,
               navigation=False, objectives=None):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[-0.001, 0.0], [0.001, 0.0]],
    }]
    workflow.state["grid"] = {"status": "passed", "level": 1, "cells": [
        {"grid_id": "G1", "bbox": [-0.0005, 0.0, 0.0005, 0.001]},
    ]}
    workflow.state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    workflow.state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }}, "site_vertical_profiles": {},
    }
    workflow.state["required_cns"] = required(redundancy, navigation=navigation)
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [aircraft(navigation=navigation)]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    selected_devices = [device()] if devices is None else devices
    workflow.state["device_catalog"] = {"status": "passed", "items": selected_devices}
    workflow.state["existing_cns_facilities"] = {"status": "passed", "items": facilities or [], "count": len(facilities or [])}
    selected_candidates = [candidate("S1")] if candidates is None else candidates
    workflow.state["candidate_sites"] = {"status": "passed", "items": selected_candidates, "count": len(selected_candidates)}
    workflow.state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    workflow.state["cns_planning_objectives"] = normalize_cns_planning_objectives(objectives)
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    return workflow


def test_single_service_gap_and_proposal_zero_pollution(tmp_path):
    workflow = configured(tmp_path)
    before = deepcopy({key: workflow.state[key] for key in (
        "existing_cns_facilities", "cns_corridor_assessment", "cns_corridor_gap_assessment",
    )})
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "proposal_ready"
    assert result["target_voxel_count"] == 1
    assert result["confirmed_requirement_unit_volume_gain"] > 0
    assert result["selected_actions"][0]["action_id"] == "candidate_site:S1:C1"
    assert {key: workflow.state[key] for key in before} == before
    assert result["proposal_only"] is True
    assert result["requires_user_confirmation_and_apply"] is True
    assert result["authoritative_combined_what_if_consistent"] is True
    assert result["combined_what_if_gain_difference"] == pytest.approx(0)
    assert result["baseline_corridor_fingerprint"] == before["cns_corridor_assessment"]["input_fingerprint"]
    assert result["baseline_corridor_gap_fingerprint"] == before["cns_corridor_gap_assessment"]["input_fingerprint"]
    assert result["final_hypothetical_corridor_fingerprint"]
    assert result["final_hypothetical_corridor_gap_fingerprint"]
    assert result["not_evaluated"]["common_cause"] == "not_evaluated"


def test_two_actions_create_partial_then_joint_redundancy(tmp_path):
    workflow = configured(
        tmp_path, redundancy=2,
        devices=[device("C1", "group-a"), device("C2", "group-b")],
        candidates=[candidate("S1")],
    )
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert len(result["selected_actions"]) == 2
    gains = [item["marginal_confirmed_requirement_unit_volume_gain"] for item in result["selected_actions"]]
    assert gains[0] > 0 and gains[1] > 0
    # Phase4-B5X：完整 P14+P15 的假想证据已外置，专用接口仍然给全量。
    final = workflow.cns_corridor_site_plan_snapshot()["final_hypothetical_evidence"]["corridor_gap"]
    entry = next(item for item in final["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "C")
    assert entry["confirmed_independent_provider_count"] == 2
    assert entry["combined_status"] == "satisfied"


def test_same_independence_group_does_not_repeat_gain(tmp_path):
    workflow = configured(
        tmp_path, redundancy=2,
        devices=[device("C1", "same"), device("C2", "same")],
        candidates=[candidate("S1")],
    )
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert len(result["selected_actions"]) == 1
    assert result["residual_confirmed_targets"]


def test_non_site_navigation_and_unknown_never_become_targets(tmp_path):
    nav = configured(tmp_path / "nav", navigation=True, devices=[], candidates=[candidate("S1")])
    nav_result = nav.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert nav_result["target_voxel_count"] == 0
    assert nav_result["selected_actions"] == []
    assert nav_result["status"] == "no_action_required"
    assert nav.state["result_statuses"]["cns_corridor_site_plan"] == "passed"

    unknown = configured(tmp_path / "unknown")
    c = next(item for item in unknown.state["cns_corridor_gap_assessment"]["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "C")
    c["combined_status"] = "unknown"
    unknown.state["cns_corridor_gap_assessment"]["routes"][0]["subsystems"][0]["confirmed_target_voxel_ids"] = []
    unknown.state["cns_corridor_gap_assessment"]["routes"][0]["subsystems"][0]["unknown_voxel_ids"] = ["G1@L1"]
    result = unknown.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["target_voxel_count"] == 0
    assert result["unknown_evidence_required"]
    assert result["status"] == "evidence_required"
    assert unknown.state["result_statuses"]["cns_corridor_site_plan"] == "pending_confirmation"


def test_confirmed_objectives_already_met_requires_no_action_even_with_target(tmp_path):
    objectives = {"routes": {"R1": {"subsystems": {"C": {"objectives": {
        "max_confirmed_deficit_volume_fraction": {"value": 1.0, "operator": "<=", "source": "test", "confirmed": True},
    }}}}}}
    workflow = configured(tmp_path, objectives=objectives)
    before = deepcopy(workflow.state["cns_corridor_gap_assessment"])
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["target_voxel_count"] == 1
    assert result["status"] == "no_action_required"
    assert result["selected_actions"] == []
    assert result["before"] == result["after"]
    # Phase4-B5X：假想证据（完整 P15 结果副本）已外置，专用接口仍然给全量。
    assert (
        workflow.cns_corridor_site_plan_snapshot()["final_hypothetical_evidence"]["corridor_gap"]
        == before
    )


def test_confirmed_target_without_positive_candidate_is_no_eligible_proposal(tmp_path):
    workflow = configured(tmp_path, candidates=[])
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["target_voxel_count"] == 1
    assert result["status"] == "no_eligible_proposal"
    assert workflow.state["result_statuses"]["cns_corridor_site_plan"] == "failed"


def test_strict_reuse_tier_precedes_later_tier(tmp_path):
    workflow = configured(
        tmp_path, facilities=[existing()], candidates=[candidate("S1")],
    )
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["selected_actions"][0]["reuse_class"] == "existing_shared_site"
    assert len(result["selected_actions"]) == 1


def test_objective_met_stops_and_unconfigured_runs_until_no_gain(tmp_path):
    objectives = {"routes": {"R1": {"subsystems": {"C": {"objectives": {
        "min_satisfied_volume_fraction": {"value": 0.5, "operator": ">=", "source": "test", "confirmed": True},
    }}}}}}
    configured_objective = configured(
        tmp_path / "objective", redundancy=2,
        devices=[device("C1", "a"), device("C2", "b")], candidates=[candidate("S1")],
        objectives=objectives,
    )
    stopped = configured_objective.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert stopped["stop_reason"] == "all_evaluable_confirmed_objectives_met"
    assert len(stopped["selected_actions"]) == 2

    no_objectives = configured(
        tmp_path / "none", redundancy=2,
        devices=[device("C1", "a"), device("C2", "b")], candidates=[candidate("S1")],
    ).evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert no_objectives["stop_reason"] == "no_positive_confirmed_marginal_gain"
    assert len(no_objectives["selected_actions"]) == 2


def test_rank_does_not_compare_different_cost_units_and_is_deterministic():
    planner = CorridorReuseFirstSitePlannerV2()
    actions = []
    impacts = []
    for identifier, cost, unit, gain in (("B", 1, "USD", 20), ("A", 1, "CNY", 20)):
        actions.append({
            "action_id": identifier, "reuse_class": "candidate_site",
            "eligibility": {"status": "eligible"},
            "planning_profile": planning_profile("candidate_site", cost, unit),
        })
        impacts.append({
            "action_id": identifier, "status": "eligible", "confirmed_requirement_unit_volume_gain": gain,
            "newly_met_confirmed_objectives": 0, "max_continuous_deficit_projection_reduction_m": 0,
            "regressions": [],
        })
    ranked = planner.rank(actions, impacts)
    assert [item["action"]["action_id"] for item in ranked] == ["A", "B"]
    assert {item["score_semantics"] for item in ranked} == {"confirmed_unit_volume_gain_per_action_count_proxy"}


def test_regression_and_satisfied_to_unknown_gate_all_positive_benefit(tmp_path):
    workflow = configured(tmp_path)
    baseline = deepcopy(workflow.state["cns_corridor_gap_assessment"])
    target = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]["targets"][0]
    satisfied = deepcopy(baseline)
    entry = next(item for item in satisfied["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "C")
    entry.update({"service_status": "satisfied", "redundancy_status": "satisfied", "combined_status": "satisfied", "qualified_provider_count": 1, "confirmed_independent_provider_count": 1})
    regressed = deepcopy(satisfied)
    right = next(item for item in regressed["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "C")
    right.update({"service_status": "confirmed_deficit", "redundancy_status": "confirmed_deficit", "combined_status": "confirmed_gap", "qualified_provider_count": 0, "confirmed_independent_provider_count": 0})
    impact = _impact({"action_id": "X"}, satisfied, regressed, [target])
    assert impact["status"] == "ineligible"
    assert impact["regressions"]
    assert impact["confirmed_requirement_unit_volume_gain"] == 0

    unknown = deepcopy(satisfied)
    right = next(item for item in unknown["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "C")
    right.update({"service_status": "unknown", "redundancy_status": "unknown", "combined_status": "unknown", "confirmed_independent_provider_count": None})
    impact = _impact({"action_id": "Y"}, satisfied, unknown, [target])
    assert impact["status"] == "unknown"
    assert impact["unknown_regressions"]
    assert impact["confirmed_requirement_unit_volume_gain"] == 0


@pytest.mark.parametrize("upstream", ["cns_corridor_assessment", "cns_corridor_gap_assessment"])
def test_stale_or_missing_upstream_refuses_planning(tmp_path, upstream):
    workflow = configured(tmp_path)
    workflow.state[upstream]["status"] = "stale"
    before = deepcopy(workflow.state["existing_cns_facilities"])
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "missing_data"
    assert result["selected_actions"] == []
    assert workflow.state["existing_cns_facilities"] == before


class ApiContext:
    def __init__(self, workflow):
        self.workflow, self.data = workflow, object()


def test_registry_api_persistence_backfill_and_invalidation(tmp_path):
    workflow = configured(tmp_path)
    assert workflow.algorithm_registry.manifest("site_planner", "corridor_reuse_first_site_planner_v2", "2.0")
    # B7X：新项目的 ``site_planner`` 默认只保存 production 的 corridor 实现；
    # 归档的 ReuseFirstSitePlannerV1 只由 CompatibilitySelectionAdapter 解析。
    assert workflow.state["algorithm_selection"]["site_planner"]["algorithm_id"] == (
        "corridor_reuse_first_site_planner_v2"
    )
    api = ApiRouter(ApiContext(workflow))
    response = api.post("/api/cns-corridor-site-plan/evaluate", {}).data
    assert response["cns_corridor_site_plan"]["input_fingerprint"]
    assert api.get("/api/cns-corridor-site-plan", {}, {}).data["proposal_only"] is True
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.cns_corridor_site_plan_snapshot()["input_fingerprint"] == response["cns_corridor_site_plan"]["input_fingerprint"]
    restored.state["result_statuses"].update({"routes": "passed", "cns_corridor_assessment": "passed", "cns_corridor_gap_assessment": "passed", "cns_corridor_site_plan": "passed"})
    restored.state["cns_corridor_site_plan"]["status"] = "proposal_ready"
    restored.invalidation_service.workflow("corridor_site_planning_policy")
    assert restored.state["result_statuses"]["cns_corridor_site_plan"] == "stale"
    assert restored.state["result_statuses"]["cns_corridor_gap_assessment"] == "passed"
    assert restored.state["result_statuses"]["routes"] == "passed"
    legacy = deepcopy(restored.state)
    legacy.pop("corridor_site_planning_policy")
    legacy.pop("cns_corridor_site_plan")
    legacy["result_statuses"].pop("cns_corridor_site_plan")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["corridor_site_planning_policy"]["status"] == "pending_confirmation"
    assert normalized["cns_corridor_site_plan"]["status"] == "not_calculated"


def test_site_planner_default_stays_v1_and_v2_selection_binds_only_v2_service(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert workflow.site_planner.algorithm_id == "reuse_first_site_planner_v1"
    workflow.select_algorithm({
        "algorithm_type": "site_planner",
        "algorithm_id": "corridor_reuse_first_site_planner_v2",
        "version": "2.0", "parameters": {"audit_tag": "test"},
    })
    assert workflow.site_planner.algorithm_id == "reuse_first_site_planner_v1"
    assert workflow.corridor_site_planner.algorithm_id == "corridor_reuse_first_site_planner_v2"
    assert workflow.corridor_site_planner.parameters == {"audit_tag": "test"}
