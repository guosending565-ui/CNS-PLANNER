from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.closed_loop import compare_gap_results
from cns_planner.domain.cns_inputs import normalize_candidate_site, normalize_device


DEFAULTS = Path("cns_planner/config/defaults.json")


def _vertical():
    return {
        "surface_elevation_m": 0.0,
        "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 0.0,
        "service_origin_egm2008_m": 0.0,
        "source": "test", "confirmed": True,
    }


def _planning_profile(reuse_class="candidate_site"):
    return {
        "reuse_class": reuse_class, "add_device_allowed": True,
        "planning_cost": None, "cost_unit": None,
        "source": "test", "confirmed": True,
    }


def _device(radius=700.0):
    return normalize_device({
        "device_id": "C-TEST", "name": "C Test", "subsystem": "C",
        "role": "existing", "enabled": True, "radius_m": radius,
        "mtbf_h": 1000.0,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": radius,
            "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2, "min_redundancy": 1}},
            "source": "test", "confirmed": True,
        },
    })


def _aircraft():
    return {
        "aircraft_id": "A1", "name": "A1", "source": "test", "metadata": {},
        "communication": {
            "status": "confirmed", "confirmed": True,
            "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        },
        "navigation": {}, "surveillance": {},
    }


def _required():
    return {
        "status": "passed", "source": "test", "route_overrides": {},
        "project_default": {
            "communication": {
                "required": True, "status": "passed",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
            },
            "navigation": {"required": False, "status": "passed"},
            "surveillance": {"required": False, "status": "passed"},
        },
    }


def _candidate(site_id="S1", longitude=0.0045):
    return normalize_candidate_site({
        "site_id": site_id, "name": site_id,
        "coordinate": [longitude, 0.0], "vertical_profile": _vertical(),
        "site_type": "tower", "available_subsystems": ["C"],
        "usable": True, "locked": False, "source": "test",
        "planning_profile": _planning_profile(),
    })


def _proposal_gap():
    return {
        "status": "confirmed_gap", "algorithm_id": "cns_gap_analysis_v2",
        "algorithm_version": "2.0", "input_fingerprint": "pre-p11-gap",
        "routes": [{
            "route_id": "R1", "route_length_m": 1000.0,
            "subsystems": [{"subsystem": "C", "segments": [{
                "segment_id": "R1:C:001", "route_id": "R1", "subsystem": "C",
                "start_route_offset_m": 0.0, "end_route_offset_m": 1000.0,
                "length_m": 1000.0, "planning_status": "confirmed_gap",
                "runtime_status": "satisfied", "combined_status": "confirmed_gap",
                "gap_causes": ["geometry_gap"], "reasons": ["baseline"],
                "evidence": [], "contingency_exposure": {"active": False},
                "remediation_scope": "ground_service_candidate",
            }]}],
        }],
    }


def _workflow(tmp_path, *, runtime="available", candidates=None, radius=700.0):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed",
        "path": [[0.0, 0.0], [0.009, 0.0]],
    }]
    workflow.state["spatial_3d"]["route_altitude_profiles"]["R1"] = {
        "route_id": "R1", "mode": "constant",
        "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": 100.0, "source": "test",
        "confirmed": True, "status": "confirmed",
    }
    workflow.state["grid"] = {
        "status": "passed", "cells": [{"grid_id": "G1", "bbox": [-1, -1, 1, 1]}],
    }
    workflow.state["grid_attributes"]["terrain"] = {
        "status": "passed", "cells": {
            "G1": {"status": "passed", "surface_elevation_mean_m": 0.0},
        },
    }
    workflow.state["required_cns"] = _required()
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [_aircraft()]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": [_device(radius)]}
    workflow.state["existing_cns_facilities"] = {"status": "passed", "items": [], "count": 0}
    candidate_values = candidates or [_candidate()]
    workflow.state["candidate_sites"] = {
        "status": "passed", "items": candidate_values, "count": len(candidate_values),
    }
    workflow.state["operational_timing"] = {
        "status": "confirmed",
        "route_motion_profiles": {"R1": {
            "route_id": "R1", "mode": "constant_ground_speed_mps",
            "constant_ground_speed_mps": 10.0,
            "source": "test", "confirmed": True, "status": "confirmed",
        }},
        "service_scenarios": {"R1": {
            "scenario_id": "SC1", "route_id": "R1", "source": "test",
            "confirmed": True, "status": "confirmed", "events": [{
                "event_id": "E1", "subsystem": "C", "start_s": 0.0,
                "end_s": 200.0, "external_state": runtime,
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
                "redundancy": 1, "source": "test", "confirmed": True,
            }],
        }},
        "response_time_budgets": {}, "encounter_scenarios": {},
    }
    workflow.state["cns_gap_analysis_v2"] = _proposal_gap()
    workflow.evaluate_cns_site_plan()
    return workflow


def test_preview_reruns_p7_p10_and_has_zero_upstream_pollution(tmp_path):
    workflow = _workflow(tmp_path)
    protected = {name: deepcopy(workflow.state[name]) for name in (
        "existing_cns_facilities", "coverage_3d", "cns_service_capability",
        "service_timeline", "cns_gap_analysis_v2", "cns_site_plan",
    )}
    result = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    assert result["validation_status"] == "validated_improvement"
    assert result["commit_status"] == "preview"
    assert result["model_scope"] == "engineering_closed_loop_verification"
    assert result["real_cns_model_validation"] == "not_evaluated"
    assert result["algorithm_runs"]["baseline"]["coverage_3d"]["algorithm_version"] == "1.0"
    assert result["algorithm_runs"]["planned"]["cns_gap_analysis_v2"]["algorithm_version"] == "2.0"
    assert {name: workflow.state[name] for name in protected} == protected
    prediction = result["prediction_comparison"]
    assert prediction["predicted_planning_gap_reduction_m"] == pytest.approx(1000.0)
    assert prediction["actual_planning_gap_reduction_m"] > 1000.0
    assert prediction["prediction_error_m"] == pytest.approx(
        prediction["actual_planning_gap_reduction_m"] - 1000.0
    )


def test_apply_commits_once_is_idempotent_and_preserves_provenance(tmp_path):
    workflow = _workflow(tmp_path)
    workflow.state["cns_corridor_assessment"] = {"status": "passed", "input_fingerprint": "before-apply"}
    workflow.state["result_statuses"]["cns_corridor_assessment"] = "passed"
    workflow.state["coverage"] = {"status": "passed", "layers": {}}
    workflow.state["cns_gap_analysis"] = {"status": "passed"}
    workflow.state["result_statuses"]["coverage"] = "passed"
    workflow.state["result_statuses"]["cns_gap"] = "passed"
    upstream = {name: deepcopy(workflow.state[name]) for name in (
        "grid", "operational_routes", "grid_risk",
    )}
    preview = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    application_id = preview["application"]["application_id"]
    applied = workflow.apply_closed_loop({"application_id": application_id})
    assessment = applied["closed_loop_assessment"]
    assert assessment["commit_status"] == "committed"
    facilities = workflow.state["existing_cns_facilities"]
    assert facilities["count"] == 1
    facility = facilities["items"][0]
    assert facility["planning_origin"]["application_id"] == application_id
    assert facility["devices"][0]["planning_origin"]["action_id"].startswith("candidate_site:")
    assert {name: workflow.state[name] for name in upstream} == upstream
    assert workflow.state["cns_site_plan"]["status"] == "stale"
    assert workflow.state["result_statuses"]["coverage"] == "stale"
    assert workflow.state["result_statuses"]["cns_gap"] == "stale"
    assert workflow.state["result_statuses"]["technical_risk"] == "stale"
    assert workflow.state["result_statuses"]["report"] == "stale"
    assert workflow.state["result_statuses"]["cns_corridor_assessment"] == "stale"
    assert workflow.state["cns_corridor_assessment"]["status"] == "stale"
    fingerprints = {
        name: workflow.state[name]["input_fingerprint"]
        for name in ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_analysis_v2")
    }
    repeated = workflow.apply_closed_loop({"application_id": application_id})
    assert repeated["closed_loop_assessment"]["commit_status"] == "committed"
    assert workflow.state["existing_cns_facilities"]["count"] == 1
    assert {name: workflow.state[name]["input_fingerprint"] for name in fingerprints} == fingerprints

    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    reopened_facility = reopened.state["existing_cns_facilities"]["items"][0]
    assert reopened_facility["planning_origin"]["application_id"] == application_id
    assert reopened_facility["devices"][0]["planning_origin"]["application_id"] == application_id


def test_all_selected_actions_are_applied_together_before_real_rerun(tmp_path):
    workflow = _workflow(tmp_path, candidates=[_candidate("S1", 0.00225), _candidate("S2", 0.00675)], radius=400)
    plan = workflow.state["cns_site_plan"]
    assert len(plan["candidate_actions"]) == 2
    plan["selected_actions"] = deepcopy(plan["candidate_actions"])
    plan["resolved_planning_gap_length_m"] = 1000.0
    result = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    assert len(result["application"]["applied_refs"]) == 2
    assert len({item["facility_id"] for item in result["application"]["applied_refs"]}) == 2
    assert result["prediction_comparison"]["actual_planning_gap_reduction_m"] > 0


def _gap(planning, combined=None, *, runtime="satisfied", length=100.0):
    combined = combined or planning
    is_gap = combined == "confirmed_gap"
    return {"routes": [{
        "route_id": "R", "subsystems": [{
            "subsystem": "C", "required": True,
            "planning_assessment": {"length_by_status_m": {planning: length}},
            "segments": [{
                "segment_id": "R:C:001", "start_route_offset_m": 0.0,
                "end_route_offset_m": length, "length_m": length,
                "planning_status": planning, "runtime_status": runtime,
                "combined_status": combined,
            }],
            "gap_length_m": length if is_gap else 0.0,
            "unknown_length_m": length if combined == "unknown" else 0.0,
            "contingency_exposure_length_m": 0.0,
            "contingency_exposure_duration_s": 0.0,
            "runtime_lost_length_m": length if runtime == "confirmed_gap" else 0.0,
            "runtime_lost_duration_s": 10.0 if runtime == "confirmed_gap" else 0.0,
            "max_continuous_gap_length_m": length if is_gap else 0.0,
            "max_continuous_gap_duration_s": 10.0 if is_gap else 0.0,
            "gap_segment_count": 1 if is_gap else 0,
        }],
    }]}


def test_gap_to_unknown_is_not_improvement_and_regression_is_detected():
    unknown = compare_gap_results(_gap("confirmed_gap"), _gap("unknown"), 100, [{}])
    assert unknown["validation_status"] == "inconclusive"
    assert unknown["prediction_comparison"]["actual_planning_gap_reduction_m"] == 0
    assert unknown["prediction_comparison"]["gap_to_unknown_length_m"] == 100

    regression = compare_gap_results(_gap("satisfied"), _gap("confirmed_gap"), 0, [{}])
    assert regression["validation_status"] == "regression"
    assert regression["regression_segments"]

    no_effect = compare_gap_results(_gap("confirmed_gap"), _gap("confirmed_gap"), 100, [{}])
    assert no_effect["validation_status"] == "no_material_improvement"

    unresolved_unknown = compare_gap_results(_gap("unknown"), _gap("unknown"), 0, [{}])
    assert unresolved_unknown["validation_status"] == "inconclusive"


def test_explicit_runtime_loss_is_not_changed_by_new_facility(tmp_path):
    workflow = _workflow(tmp_path, runtime="unavailable")
    assessment = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    comparison = next(
        item for item in assessment["comparisons"]
        if item["route_id"] == "R1" and item["subsystem"] == "C"
    )
    assert comparison["before"]["runtime_lost_length_m"] > 0
    assert comparison["after"]["runtime_lost_length_m"] == pytest.approx(
        comparison["before"]["runtime_lost_length_m"]
    )


def test_changed_input_rejects_apply_without_mutation(tmp_path):
    workflow = _workflow(tmp_path)
    preview = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    workflow.state["required_cns"]["source"] = "changed-after-preview"
    before = deepcopy(workflow.state)
    result = workflow.apply_closed_loop({
        "application_id": preview["application"]["application_id"],
    })
    assert result["closed_loop_assessment"]["commit_status"] == "stale_assessment"
    assert workflow.state == before


def test_rerun_exception_rolls_back_preview_and_apply(tmp_path):
    workflow = _workflow(tmp_path)
    before_preview = deepcopy(workflow.state)

    class BrokenCoverage:
        def __init__(self, parameters=None):
            self.parameters = parameters or {}

        def evaluate(self, *args):
            raise RuntimeError("synthetic rerun failure")

    working_model = workflow.closed_loop_service.coverage_model
    workflow.closed_loop_service.coverage_model = BrokenCoverage()
    with pytest.raises(RuntimeError, match="synthetic rerun failure"):
        workflow.evaluate_closed_loop()
    assert workflow.state == before_preview

    workflow.closed_loop_service.coverage_model = working_model
    preview = workflow.evaluate_closed_loop()["closed_loop_assessment"]
    before_apply = deepcopy(workflow.state)
    workflow.closed_loop_service.coverage_model = BrokenCoverage()
    with pytest.raises(RuntimeError, match="synthetic rerun failure"):
        workflow.apply_closed_loop({"application_id": preview["application"]["application_id"]})
    assert workflow.state == before_apply


class _ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_backfill_persistence_api_and_invalidation(tmp_path):
    workflow = _workflow(tmp_path)
    legacy = deepcopy(workflow.state)
    legacy.pop("closed_loop_assessment")
    legacy["result_statuses"].pop("closed_loop_assessment")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["closed_loop_assessment"]["status"] == "not_calculated"

    router = ApiRouter(_ApiContext(workflow))
    preview = router.post("/api/cns-closed-loop/evaluate", {}).data
    assert preview["closed_loop_assessment"]["commit_status"] == "preview"
    assert router.get("/api/cns-closed-loop", {}, {}).data["assessment_fingerprint"]
    application_id = preview["closed_loop_assessment"]["application"]["application_id"]
    applied = router.post("/api/cns-closed-loop/apply", {"application_id": application_id}).data
    assert applied["closed_loop_assessment"]["commit_status"] == "committed"

    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert reopened.closed_loop_snapshot()["commit_status"] == "committed"
    reopened.state["closed_loop_assessment"]["status"] = "passed"
    reopened.invalidation_service.cns_site_plan()
    assert reopened.state["closed_loop_assessment"]["status"] == "stale"
    assert reopened.state["result_statuses"]["closed_loop_assessment"] == "stale"
