from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import (
    normalize_candidate_site, normalize_device, normalize_existing_facility,
    normalize_planning_profile,
)
from cns_planner.domain.site_planning import default_site_planning_policy
from cns_planner.site_planner.reuse_first_v1 import ReuseFirstSitePlannerV1


DEFAULTS = Path("cns_planner/config/defaults.json")


def planning_profile(reuse_class="candidate_site", cost=None):
    value = {
        "reuse_class": reuse_class, "add_device_allowed": True,
        "source": "test", "confirmed": True,
    }
    if cost is not None:
        value.update({"planning_cost": cost, "cost_unit": "cost-unit"})
    return value


def vertical():
    return {
        "surface_elevation_m": 0.0,
        "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 0.0,
        "service_origin_egm2008_m": 0.0,
        "source": "test", "confirmed": True,
    }


def device(*, unsupported=False, independence=None):
    parameters = {"performance": {"max_latency_s": 0.2, "min_redundancy": 1}}
    if independence:
        parameters.update({"independence_confirmed": True, "independence_group": independence})
    return normalize_device({
        "device_id": "C-TEST", "name": "C Test", "subsystem": "C",
        "role": "existing", "radius_m": 700.0, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": 700.0,
            "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "unsupported" if unsupported else "declared_performance",
            "version": "1", "technology": "dedicated_radio",
            "parameters": parameters, "source": "test", "confirmed": True,
        },
    })


def required(redundancy=1):
    return {"status": "passed", "project_default": {
        "communication": {
            "required": True, "status": "passed",
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": redundancy},
        },
        "navigation": {"required": False, "status": "passed"},
        "surveillance": {"required": False, "status": "passed"},
    }, "route_overrides": {}}


def aircraft():
    return {
        "aircraft_id": "A1", "name": "A1",
        "communication": {
            "status": "confirmed", "confirmed": True, "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        },
        "navigation": {}, "surveillance": {}, "source": "test", "metadata": {},
    }


def gap_v2(segments=None):
    segments = segments or [{
        "segment_id": "R1:C:001", "route_id": "R1", "subsystem": "C",
        "start_route_offset_m": 0.0, "end_route_offset_m": 1000.0,
        "length_m": 1000.0, "planning_status": "confirmed_gap",
        "runtime_status": "unknown", "combined_status": "confirmed_gap",
        "gap_causes": ["geometry_gap"], "reasons": ["baseline geometry gap"],
        "evidence": [], "contingency_exposure": {"active": False},
        "remediation_scope": "ground_service_candidate",
    }]
    return {"status": "confirmed_gap", "algorithm_id": "cns_gap_analysis_v2", "algorithm_version": "2.0", "input_fingerprint": "gap-before", "routes": [{
        "route_id": "R1", "route_length_m": 1000.0,
        "subsystems": [{"subsystem": "C", "segments": segments}],
    }]}


def configure_workflow(tmp_path, *, candidate=None, existing=None, catalog_device=None, redundancy=1, segments=None):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.009, 0.0]],
    }]
    workflow.state["spatial_3d"]["route_altitude_profiles"]["R1"] = {
        "route_id": "R1", "mode": "constant",
        "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": 100.0, "source": "test",
        "confirmed": True, "status": "confirmed",
    }
    workflow.state["grid"] = {"status": "passed", "cells": [{"grid_id": "G1", "bbox": [-1, -1, 1, 1]}]}
    workflow.state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {
        "G1": {"status": "passed", "surface_elevation_mean_m": 0.0},
    }}
    workflow.state["required_cns"] = required(redundancy)
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [aircraft()]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": [catalog_device or device()]}
    workflow.state["existing_cns_facilities"] = {"status": "passed", "items": existing or [], "count": len(existing or [])}
    candidate = candidate or normalize_candidate_site({
        "site_id": "S1", "name": "S1", "coordinate": [0.0045, 0.0],
        "vertical_profile": vertical(), "site_type": "tower",
        "available_subsystems": ["C"], "usable": True, "locked": False,
        "source": "test", "planning_profile": planning_profile(),
    })
    workflow.state["candidate_sites"] = {"status": "passed", "items": [candidate], "count": 1}
    workflow.state["cns_gap_analysis_v2"] = gap_v2(segments)
    return workflow


def action(action_id, tier, *, cost=None):
    profile = {"confirmed": True, "planning_cost": cost, "cost_unit": "cost-unit" if cost is not None else None}
    return {
        "action_id": action_id, "reuse_class": tier, "subsystem": "C",
        "eligibility": {"status": "eligible", "reasons": []},
        "planning_profile": profile,
    }


def impact(action_id, intervals):
    length = sum(end - start for start, end in intervals)
    return {
        "action_id": action_id, "status": "eligible",
        "planning_gap_reduction_m": length,
        "resolved_segment_ids": ["T"] if length == 100 else [],
        "affected_segment_ids": ["T"],
        "segment_resolutions": [{
            "segment_id": "T", "resolved_intervals": intervals,
            "planning_gap_reduction_m": length,
        }],
        "evidence": [],
    }


def test_planning_profile_is_explicit_and_backfills_without_guessing():
    missing = normalize_planning_profile(None)
    assert missing == {
        "reuse_class": None, "add_device_allowed": None,
        "planning_cost": None, "cost_unit": None,
        "source": "not_defined", "confirmed": False, "status": "missing_data",
    }
    profile = normalize_planning_profile(planning_profile("existing_shared_site", 10))
    assert profile["reuse_class"] == "existing_shared_site"
    assert profile["planning_cost"] == 10
    with pytest.raises(ValueError, match="cost_unit"):
        normalize_planning_profile({**planning_profile(), "planning_cost": 10})


def test_only_confirmed_ground_improvable_planning_gaps_become_targets(tmp_path):
    segments = [
        gap_v2()["routes"][0]["subsystems"][0]["segments"][0],
        {"segment_id": "UNKNOWN", "length_m": 100, "planning_status": "unknown", "runtime_status": "unknown", "gap_causes": ["unknown_evidence"], "remediation_scope": "evidence_collection"},
        {"segment_id": "RUNTIME", "length_m": 100, "planning_status": "satisfied", "runtime_status": "confirmed_gap", "gap_causes": ["runtime_service_loss"], "remediation_scope": "operational_scenario"},
        {"segment_id": "CONT", "length_m": 100, "planning_status": "satisfied", "runtime_status": "satisfied_by_contingency", "gap_causes": [], "remediation_scope": "unknown"},
        {"segment_id": "AIRCRAFT", "length_m": 100, "planning_status": "confirmed_gap", "runtime_status": "unknown", "gap_causes": ["static_service_mismatch"], "remediation_scope": "aircraft_or_requirement"},
    ]
    workflow = configure_workflow(tmp_path, segments=segments)
    result = workflow.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["target_segment_count"] == 1
    assert result["target_planning_gap_length_m"] == 1000


def test_application_what_if_uses_p7_p8_and_does_not_mutate_upstream(tmp_path):
    workflow = configure_workflow(tmp_path)
    existing_before = deepcopy(workflow.state["existing_cns_facilities"])
    fingerprints_before = {
        name: deepcopy(workflow.state.get(name))
        for name in ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_analysis_v2")
    }
    result = workflow.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["status"] == "proposal_ready"
    assert result["selected_actions"][0]["action_id"] == "candidate_site:S1:C-TEST"
    assert result["resolved_planning_gap_length_m"] == pytest.approx(1000)
    impact_value = result["candidate_impacts"][0]
    assert impact_value["status"] == "eligible"
    assert [item["algorithm_version"] for item in impact_value["evidence"][:2]] == ["1.0", "1.0"]
    assert all(item.get("persisted") is False for item in impact_value["evidence"][:2])
    assert workflow.state["existing_cns_facilities"] == existing_before
    assert {name: workflow.state.get(name) for name in fingerprints_before} == fingerprints_before
    assert result["proposal_only"] is True
    assert result["requires_closed_loop_validation"] is True


def test_planning_profile_is_excluded_from_established_p7_p8_fingerprints(tmp_path):
    workflow = configure_workflow(tmp_path)
    facility = normalize_existing_facility({
        "facility_id": "F1", "site_id": "F1", "name": "F1",
        "coordinate": [0.0045, 0.0], "vertical_profile": vertical(),
        "devices": [{"device_id": "C-TEST", "subsystem": "C", "status": "active"}],
        "status": "active", "source": "test",
        "planning_profile": planning_profile("existing_cns_facility"),
    })
    workflow.state["existing_cns_facilities"] = {
        "status": "passed", "items": [facility], "count": 1,
    }
    legacy_facilities = deepcopy(workflow.state["existing_cns_facilities"])
    legacy_facilities["items"][0].pop("planning_profile")
    legacy_facilities["items"][0].pop("planning_origin")
    for installed in legacy_facilities["items"][0]["devices"]:
        installed.pop("planning_origin")
    expected_coverage = workflow.coverage_model_3d.evaluate(
        workflow.state["operational_routes"], workflow.state["spatial_3d"],
        workflow.state["grid"], workflow.state["grid_attributes"],
        legacy_facilities, workflow.state["device_catalog"],
    )
    actual_coverage = workflow.evaluate_coverage_3d()["coverage_3d"]
    assert actual_coverage["input_fingerprint"] == expected_coverage["input_fingerprint"]

    expected_capability = workflow.cns_service_model.evaluate(
        actual_coverage, workflow.state["required_cns"], aircraft(),
        legacy_facilities, workflow.state["device_catalog"],
    )
    actual_capability = workflow.evaluate_cns_service_capability()["cns_service_capability"]
    assert actual_capability["input_fingerprint"] == expected_capability["input_fingerprint"]


@pytest.mark.parametrize(("updates", "expected"), [
    ({"usable": False}, "ineligible"),
    ({"locked": True}, "ineligible"),
])
def test_locked_or_unusable_candidate_is_ineligible(tmp_path, updates, expected):
    candidate = normalize_candidate_site({
        "site_id": "S1", "coordinate": [0.0045, 0], "site_type": "tower",
        "available_subsystems": ["C"], "usable": updates.get("usable", True),
        "locked": updates.get("locked", False), "vertical_profile": vertical(),
        "planning_profile": planning_profile(),
    })
    result = configure_workflow(tmp_path, candidate=candidate).evaluate_cns_site_plan()["cns_site_plan"]
    assert result["candidate_actions"][0]["eligibility"]["status"] == expected
    assert result["selected_actions"] == []


def test_unsupported_or_unconfirmed_what_if_evidence_cannot_gain(tmp_path):
    unsupported = configure_workflow(tmp_path / "unsupported", catalog_device=device(unsupported=True)).evaluate_cns_site_plan()["cns_site_plan"]
    assert unsupported["candidate_actions"][0]["eligibility"]["status"] == "ineligible"
    assert unsupported["resolved_planning_gap_length_m"] == 0

    candidate = normalize_candidate_site({
        "site_id": "S1", "coordinate": [0.0045, 0], "site_type": "tower",
        "available_subsystems": ["C"], "usable": True, "locked": False,
        "planning_profile": planning_profile(),
    })
    missing_vertical = configure_workflow(tmp_path / "vertical", candidate=candidate)
    missing_vertical.state["candidate_sites"]["items"][0]["vertical_profile"] = {"confirmed": False, "service_origin_egm2008_m": None}
    result = missing_vertical.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["candidate_actions"][0]["eligibility"]["status"] == "unknown"


def test_reuse_tiers_overlap_cost_proxy_and_tie_are_deterministic():
    planner = ReuseFirstSitePlannerV1()
    targets = [{"segment_id": "T", "route_id": "R", "subsystem": "C", "length_m": 100, "requires_joint_optimization": False}]
    actions = [
        action("candidate", "candidate_site"),
        action("existing", "existing_cns_facility"),
        action("tie-b", "existing_shared_site"),
        action("tie-a", "existing_shared_site"),
    ]
    impacts = [
        impact("candidate", [[0, 100]]),
        impact("existing", [[0, 50]]),
        impact("tie-a", [[50, 75]]),
        impact("tie-b", [[50, 75]]),
    ]
    result = planner.plan(targets, actions, impacts, default_site_planning_policy())
    assert [item["action_id"] for item in result["selected_actions"]] == ["existing", "tie-a", "candidate"]
    assert result["resolved_planning_gap_length_m"] == 100
    assert sum(item["marginal_planning_gap_reduction_m"] for item in result["selected_actions"]) == 100
    assert result["cost_summary"]["cost_semantics"] == "action_count_proxy_no_currency"
    assert result["cost_summary"]["currency_total"] is None
    repeated = planner.plan(targets, actions, impacts, default_site_planning_policy())
    assert repeated["input_fingerprint"] == result["input_fingerprint"]
    assert repeated["selected_actions"] == result["selected_actions"]


def test_redundancy_is_not_inferred_and_joint_need_remains_residual(tmp_path):
    workflow = configure_workflow(tmp_path, redundancy=2)
    result = workflow.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["selected_actions"] == []
    assert result["remaining_planning_gap_length_m"] == 1000
    assert result["requires_joint_optimization"] is True
    assert result["unresolved_segments"][0]["requires_joint_optimization"] is True


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_backfill_persistence_api_and_directed_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = configure_workflow(tmp_path)
    registry = build_default_algorithm_registry(workflow.defaults)
    assert registry.manifest("site_planner", "reuse_first_site_planner_v1", "1.0")
    legacy = deepcopy(workflow.state)
    legacy.pop("site_planning_policy")
    legacy.pop("cns_site_plan")
    legacy["result_statuses"].pop("cns_site_plan")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["site_planning_policy"]["strategy"] == "reuse_first_weighted_greedy_set_cover"
    assert normalized["cns_site_plan"]["status"] == "not_calculated"
    assert normalized["candidate_sites"]["items"][0]["planning_profile"]["reuse_class"] == "candidate_site"

    router = ApiRouter(ApiContext(workflow))
    response = router.post("/api/cns-site-plan", {"site_planning_policy": {**default_site_planning_policy(), "confirmed": True, "source": "test"}}).data
    assert response["cns_site_plan"]["proposal_only"] is True
    assert router.get("/api/cns-site-plan", {}, {}).data["algorithm_id"] == "reuse_first_site_planner_v1"
    reopened = WorkflowService(path, DEFAULTS)
    assert reopened.cns_site_plan_snapshot()["input_fingerprint"] == response["cns_site_plan"]["input_fingerprint"]

    reopened.state["cns_site_plan"]["status"] = "proposal_ready"
    reopened.state["result_statuses"].update({
        "grid": "passed", "routes": "passed", "coverage": "passed", "cns_gap": "passed",
        "coverage_3d": "passed", "cns_service_capability": "passed",
        "service_timeline": "passed", "cns_gap_v2": "passed", "cns_site_plan": "passed",
    })
    reopened.invalidation_service.cns_site_plan()
    assert reopened.state["result_statuses"]["cns_site_plan"] == "stale"
    assert {reopened.state["result_statuses"][name] for name in ("grid", "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2")} == {"passed"}
