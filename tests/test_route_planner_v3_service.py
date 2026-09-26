"""Route Planner V3-A integration: state container, service, readiness and API.

The integration tests assert the hard boundary of V3-A: it is an independent
experimental container that never writes ``operational_routes``, never switches
``algorithm_selection``, never touches ``spatial_3d``/route altitude profiles and
never changes any V1/V2 or P7-P19 result.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.algorithms.registry import build_default_algorithm_registry, default_algorithm_selection
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.route_planner_v3_service import (
    V3_REAL_DATA_ADAPTER_STATUS, V3B_REAL_DATA_ADAPTER_STATUS,
    RoutePlannerV3ExperimentService, record_summary,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.route_planner_v3.contracts import (
    empty_v3_experimental_session, normalize_v3_experiments, normalize_v3_planning_policy,
)
from cns_planner.route_planner_v3.fine_contracts import normalize_v3_fine_refinement_policy

DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [122.0, 29.9, 122.02, 29.92]


def v3_policy(**overrides):
    payload = {
        "min_altitude_egm2008_m": 100.0,
        "max_altitude_egm2008_m": 400.0,
        "vertical_step_m": 50.0,
        "terrain_clearance_m": 50.0,
        "building_horizontal_clearance_m": 15.0,
        "building_vertical_clearance_m": 30.0,
        "aircraft_min_turn_radius_m": 20.0,
        "max_climb_gradient": 0.5,
        "max_descent_gradient": 0.5,
        "planning_speed_mps": 25.0,
        "source": "integration_test",
        "confirmed": True,
    }
    payload.update(overrides)
    return payload


def workflow(tmp_path, name="project.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    service.state["workspace"] = {"status": "passed", "bbox": list(WORKSPACE)}
    service.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.0005, 29.9005]},
        {"node_id": "N002", "name": "B", "coordinate": [122.0195, 29.9195]},
    ]
    service.state["node_seq"] = 2
    return service


def evaluate(service, **overrides):
    payload = {"environment_source": "canonical_synthetic", "synthetic_spec": {"profile_id": "open_flat"}}
    payload.update(overrides)
    if "policy" not in payload:
        payload["policy"] = v3_policy()
    return service.evaluate_route_planner_v3(payload)


def latest_record(service):
    """The full frozen record from the detail endpoint (the snapshot only summarises)."""

    return service.route_planner_v3_snapshot()["records"][0]


def latest_summary(service):
    return service.snapshot()["route_planner_v3_experiments"]["active_experiment"]


# --------------------------------------------------------------------------------------
# project state container
# --------------------------------------------------------------------------------------


def test_blank_project_has_the_additive_v3_containers():
    project = blank_project({})
    assert project["v3_planning_policy"]["status"] == "pending_confirmation"
    assert project["v3_planning_policy"]["min_altitude_egm2008_m"] is None
    assert project["route_planner_v3_experiments"] == empty_v3_experimental_session()
    assert project["route_planner_v3_experiments"]["collection_id"] == "route-planner-v3-experiments"
    assert project["route_planner_v3_experiments"]["records"] == []
    assert "不写入 operational_routes" in project["route_planner_v3_experiments"]["note"]


def test_legacy_project_backfills_the_v3_containers_without_touching_other_fields():
    legacy = blank_project({})
    before_routes = deepcopy(legacy["operational_routes"])
    before_selection = deepcopy(legacy["algorithm_selection"])
    legacy.pop("v3_planning_policy")
    legacy.pop("route_planner_v3_experiments")
    normalized = normalize_project(legacy, WorkspaceGridService())
    assert normalized["v3_planning_policy"]["min_altitude_egm2008_m"] is None
    assert normalized["route_planner_v3_experiments"]["count"] == 0
    assert normalized["operational_routes"] == before_routes
    assert normalized["algorithm_selection"] == before_selection


def test_v3_experiment_container_survives_save_and_reload(tmp_path):
    service = workflow(tmp_path)
    evaluate(service)
    path = tmp_path / "project.json"
    assert path.exists()
    reloaded = WorkflowService(path, DEFAULTS)
    snapshot = reloaded.route_planner_v3_snapshot()
    assert snapshot["count"] == 1
    assert snapshot["records"][0]["result"]["status"] == "strategic_candidate"


def test_v3_experiment_normalization_rejects_a_non_object_and_keeps_records():
    with pytest.raises(ValueError):
        normalize_v3_experiments(["not-an-object"])
    collection = normalize_v3_experiments({
        "records": [{"experiment_id": "V3-AAAAAAAAAAAA", "result": {"status": "strategic_candidate"}}],
        "active_experiment_id": "V3-UNKNOWN",
    })
    assert collection["count"] == 1
    assert collection["active_experiment_id"] == "V3-AAAAAAAAAAAA"
    assert collection["records"][0]["result"]["operational_route"] is False


# --------------------------------------------------------------------------------------
# service behaviour and the isolation boundary
# --------------------------------------------------------------------------------------


def test_v3_result_is_never_the_operational_route_and_carries_the_disclaimer(tmp_path):
    service = workflow(tmp_path)
    snapshot = evaluate(service)
    record = latest_record(service)
    result = record["result"]
    assert result["status"] == "strategic_candidate"
    assert result["operational_route"] is False
    assert result["final_validation_performed"] is False
    assert result["disclaimer"] == (
        "V3-A strategic candidate：3D + heading 战略搜索结果，不是 final safe / validated "
        "operational route；未做 V3-B corridor-local 精化、未做 V3-C exact polygon/terrain/"
        "continuous clearance 验证、也未做 Route–CNS 联合优化。"
    )
    assert record["verdicts"]["operational_route"] is False
    assert record["verdicts"]["final_validation_performed"] is False
    assert snapshot["route_planner_v3_experiments"]["allowed_result_statuses"] == [
        "strategic_candidate", "failed", "missing_data", "pending_confirmation", "not_ready",
        "search_incomplete",
    ]
    assert snapshot["route_planner_v3_experiments"]["allowed_refinement_statuses"] == [
        "refined_candidate", "failed", "not_ready", "missing_data", "search_incomplete",
    ]
    assert snapshot["route_planner_v3_experiments"]["architecture"]
    assert snapshot["route_planner_v3_experiments"]["v3b_architecture"]
    assert snapshot["route_planner_v3_readiness"]["architecture"] == (
        snapshot["route_planner_v3_experiments"]["architecture"]
    )


def test_v3_experiment_uses_a_temporary_l8_grid_without_replacing_the_project_grid(tmp_path):
    service = workflow(tmp_path)
    service.state["grid"] = WorkspaceGridService().generate(list(WORKSPACE), 6)
    project_grid_before = deepcopy(service.state["grid"])
    evaluate(service)
    record = latest_record(service)
    assert service.state["grid"] == project_grid_before
    assert record["provenance"]["project_grid_level"] == 6
    assert record["result"]["trajectory_summary"]["state_count"] > 0
    assert record["result"]["search_statistics"]["state_space_shape"]["grid_cells"] == 324


def test_v3_experiment_identity_is_deterministic_and_reruns_are_idempotent(tmp_path):
    service = workflow(tmp_path)
    evaluate(service)
    first = latest_record(service)
    evaluate(service)
    records = service.route_planner_v3_snapshot()["records"]
    assert len(records) == 1
    assert records[0]["experiment_id"] == first["experiment_id"]
    assert records[0]["result"]["input_fingerprint"] == first["result"]["input_fingerprint"]
    assert service.route_planner_v3_snapshot()["count"] == 1

    evaluate(service, synthetic_spec={
        "profile_id": "wall", "terrain_profile": "ridge_longitude",
        "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 600.0,
    })
    detail = service.route_planner_v3_snapshot()
    assert detail["count"] == 2
    assert {item["result"]["status"] for item in detail["records"]} == {
        "strategic_candidate", "failed",
    }


def test_v3_experiment_delete_removes_only_the_requested_record(tmp_path):
    service = workflow(tmp_path)
    evaluate(service)
    evaluate(service, synthetic_spec={"profile_id": "second", "terrain_profile": "rough",
                                      "terrain_relative_amplitude_m": 20.0})
    records = service.route_planner_v3_snapshot()["records"]
    assert len(records) == 2
    remaining = service.delete_route_planner_v3_experiment(records[0]["experiment_id"])
    ids = [item["experiment_id"] for item in remaining["route_planner_v3_experiments"]["records"]]
    assert records[0]["experiment_id"] not in ids
    assert records[1]["experiment_id"] in ids
    with pytest.raises(ValueError):
        service.delete_route_planner_v3_experiment("V3-DOESNOTEXIST")


def test_v3_evaluation_refuses_an_unready_policy_instead_of_guessing(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="未确认"):
        evaluate(service, policy={"min_altitude_egm2008_m": 100.0})
    with pytest.raises(ValueError, match="confirmed"):
        evaluate(service, policy=v3_policy(confirmed=False))
    assert service.state["route_planner_v3_experiments"]["count"] == 0


def test_v3_evaluation_requires_a_saved_workspace(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    with pytest.raises(ValueError, match="工作区"):
        evaluate(service)


def test_v3_evaluation_rejects_a_non_synthetic_environment_source(tmp_path):
    service = workflow(tmp_path)
    with pytest.raises(ValueError, match="canonical_synthetic"):
        evaluate(service, environment_source="real_gdal_adapter")


def test_v3_policy_is_persisted_and_overridable_per_run(tmp_path):
    service = workflow(tmp_path)
    service.set_route_planner_v3_policy(v3_policy())
    stored = service.route_planner_v3_snapshot()
    assert stored is not None
    policy = service.state["v3_planning_policy"]
    assert policy["status"] == "confirmed"
    assert policy["terrain_clearance_m"] == 50.0
    assert policy["missing_parameters"] == []
    # A per-run payload policy is a *complete* parameter set for that run: the
    # stored policy is never merged in, so no unstated parameter can be silently
    # reused from a different operation.
    evaluate(service, policy=v3_policy(aircraft_min_turn_radius_m=35.0))
    record = latest_record(service)
    assert record["result"]["effective_policy"]["aircraft_min_turn_radius_m"] == 35.0
    assert service.state["v3_planning_policy"]["aircraft_min_turn_radius_m"] == 20.0
    with pytest.raises(ValueError, match="未确认"):
        evaluate(service, policy={"aircraft_min_turn_radius_m": 35.0})


def test_v3_corridor_parameters_are_explicit_and_default_to_no_support_ring(tmp_path):
    service = workflow(tmp_path)
    evaluate(service)
    corridor = latest_record(service)["result"]["candidate_refinement_corridor"]
    assert corridor["ring_n"] == 0
    assert corridor["support_grid_ids"] == corridor["center_grid_ids"]
    assert corridor["semantics"] == "refinement_search_window_not_safety_corridor"

    evaluate(
        service, corridor_ring_n=2, refinement_cell_size_m=30.0, corridor_altitude_margin_m=100.0,
    )
    ringed = latest_record(service)["result"]["candidate_refinement_corridor"]
    assert ringed["ring_n"] == 2
    assert set(ringed["center_grid_ids"]) <= set(ringed["support_grid_ids"])
    assert ringed["support_cell_count"] >= ringed["center_state_count"]
    assert ringed["refinement_cell_size_m"] == 30.0
    assert ringed["altitude_envelope"]["explicit_margin_m"] == 100.0
    assert ringed["altitude_envelope"]["semantics"] == "planned_altitude_extent_not_a_clearance_volume"


def test_v3_endpoints_follow_the_selected_scenario_route(tmp_path):
    service = workflow(tmp_path)
    service.generate_scenario_od("N001", "N002", "ab")
    route_id = service.state["scenario_routes"][0]["route_id"]
    evaluate(service, route_id=route_id)
    record = latest_record(service)
    assert record["route_id"] == route_id
    trajectory = record["result"]["trajectory_summary"]
    projection = record["result"]["horizontal_projection"]
    assert projection[0] == [122.0005, 29.9005]
    assert projection[-1] == [122.0195, 29.9195]
    assert trajectory["endpoint_grid_binding"] == "nearest_search_cell_center"


# --------------------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------------------


def test_v3_readiness_reports_each_domain_and_the_missing_real_adapter(tmp_path):
    service = workflow(tmp_path)
    readiness = service.route_planner_v3_readiness()
    assert readiness["status"] == "passed"
    assert readiness["stage"] == "V3-A"
    assert readiness["algorithm"]["registered_in_algorithm_registry"] is False
    assert readiness["algorithm"]["default_route_planner"] == "route_planner_v1"
    assert readiness["policy_readiness"]["status"] == "blocked"
    assert set(readiness["policy_readiness"]["missing_parameters"]) >= {"min_altitude_egm2008_m"}
    assert readiness["aircraft_readiness"]["status"] == "blocked"
    assert readiness["grid"]["status"] == "regenerable_at_l8"
    assert readiness["grid"]["l8_cell_count"] == 324
    assert readiness["grid"]["level"] is None or readiness["grid"]["level"] != 8

    real = readiness["real_data_readiness"]
    assert real["status"] == "blocked"
    assert real["adapter_status"] == V3_REAL_DATA_ADAPTER_STATUS
    assert real["required_before_real_run"]

    scope = readiness["stage_scope"]
    assert "l8_strategic_search" in scope["implemented"]
    assert "corridor_local_fine_refinement_in_this_stage" in scope["not_implemented"]
    assert "exact_polygon_terrain_continuous_clearance_validation" in scope["not_implemented"]
    assert "operational_adapter" in scope["not_implemented"]
    assert "cns_joint_optimization" in scope["not_implemented"]
    assert "energy_model" in scope["not_implemented"]
    assert scope["implemented_in_other_stages"] == {
        "corridor_local_fine_refinement": "V3-B",
        "exact_validation": "V3-C",
        "validated_route_operational_adapter_and_cns_assessment": "V3-D",
    }
    assert real["v3b"]["adapter_status"] == V3B_REAL_DATA_ADAPTER_STATUS
    assert real["v3b"]["blocking_reasons"]


def test_v3_readiness_becomes_ready_for_policy_and_aircraft_once_confirmed(tmp_path):
    service = workflow(tmp_path)
    service.set_route_planner_v3_policy(v3_policy())
    readiness = service.route_planner_v3_readiness()
    assert readiness["policy_readiness"]["status"] == "ready"
    assert readiness["aircraft_readiness"]["status"] == "ready"
    assert readiness["aircraft_readiness"]["climb_gradient"] == pytest.approx(0.5)
    assert readiness["aircraft_readiness"]["min_turn_radius_m"] == 20.0


def test_v3_readiness_blocks_a_rate_only_aircraft_without_a_planning_speed(tmp_path):
    service = workflow(tmp_path)
    service.set_route_planner_v3_policy(v3_policy(
        max_climb_gradient=None, max_descent_gradient=None,
        max_climb_rate_mps=5.0, max_descent_rate_mps=5.0, planning_speed_mps=None,
    ))
    readiness = service.route_planner_v3_readiness()
    assert readiness["aircraft_readiness"]["status"] == "blocked"
    assert readiness["aircraft_readiness"]["planning_speed_source"] == (
        "unresolved_no_planning_speed_is_ever_guessed"
    )


def test_v3_readiness_reports_the_workflow_snapshot_for_the_panel(tmp_path):
    service = workflow(tmp_path)
    snapshot = service.snapshot()
    assert "route_planner_v3_readiness" in snapshot
    projection = snapshot["route_planner_v3_experiments"]
    assert projection["count"] == 0
    assert projection["detail_endpoint"] == "/api/route-planner-v3-experiments"
    assert projection["architecture"]
    assert projection["operational_routes_untouched"] is True


def test_v3_snapshot_summaries_stay_bounded_and_keep_the_detail_endpoint(tmp_path):
    service = workflow(tmp_path)
    evaluate(service)
    snapshot = service.snapshot()
    projection = snapshot["route_planner_v3_experiments"]
    active = projection["active_experiment"]
    assert set(active) == set(record_summary(
        service.route_planner_v3_snapshot()["records"][0]
    ))
    assert "state_path" not in json.dumps(active)
    assert active["status"] == "strategic_candidate"
    assert active["state_count"] > 0
    assert active["corridor_center_count"] > 0
    assert active["runtime_ms"] >= 0
    assert json.dumps(projection, allow_nan=False)


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


class RecordingWorkflow:
    def __init__(self):
        self.calls = []

    # Present only so the shared /api dispatch table can be traversed without
    # touching unrelated routes; these are never invoked by the V3 paths.
    def analyze_cns_gaps_v2(self, payload):
        raise AssertionError("unrelated route must not run")

    def candidate_sites_from_existing(self):
        raise AssertionError("unrelated route must not run")

    def analyze_cns_gaps(self):
        raise AssertionError("unrelated route must not run")

    def route_planner_v3_snapshot(self):
        self.calls.append(("v3-snapshot",))
        return {"status": "passed", "count": 1}

    def route_planner_v3_readiness(self):
        self.calls.append(("v3-readiness",))
        return {"status": "passed", "stage": "V3-A"}

    def set_route_planner_v3_policy(self, payload):
        self.calls.append(("v3-policy", payload))
        return {"status": "passed", "policy": payload}

    def evaluate_route_planner_v3(self, payload):
        self.calls.append(("v3-evaluate", payload))
        return {"status": "passed", "evaluated": True}

    def delete_route_planner_v3_experiment(self, experiment_id):
        self.calls.append(("v3-delete", experiment_id))
        return {"status": "passed", "deleted": experiment_id}


class ApiContext:
    def __init__(self):
        self.workflow = RecordingWorkflow()
        self.data = object()


def test_v3_does_not_change_the_algorithm_registry_catalog_or_default_selection():
    catalog = build_default_algorithm_registry({}).catalog()
    ids = {(item["algorithm_type"], item["algorithm_id"], item["version"]) for item in catalog}
    assert ("route_planner", "route_planner_v1", "1.0") not in ids
    assert ("route_planner", "risk_aware_route_planner_v2", "2.0") not in ids
    assert not any(item[1].startswith("route_planner_v3") for item in ids)
    selection = default_algorithm_selection()
    assert "route_planner" not in selection
    assert normalize_v3_planning_policy(None)["confirmed"] is False


def test_v3_service_is_wired_into_the_workflow_without_a_registry_entry(tmp_path):
    service = workflow(tmp_path)
    assert isinstance(service.route_planner_v3_service, RoutePlannerV3ExperimentService)
    assert service.route_planner_v3_service.planner.algorithm_id == "route_planner_v3_strategic"
    assert service.route_planner_v3_service.planner.uses_v3_native_3d is True
    assert service.route_planner_v3_service.refinement_planner.algorithm_id == (
        "route_planner_v3_corridor_refinement"
    )


# --------------------------------------------------------------------------------------
# V3-B: corridor-local refinement through the service
# --------------------------------------------------------------------------------------


def set_fine_policy(service, **overrides):
    payload = {
        "horizontal_crs": "synthetic:local_equirectangular_m",
        "resolution_source": "explicit_configuration",
        "resolution_m": 60.0,
        "max_stride_cells": 3,
        "source": "integration_test_fine_policy",
        "confirmed": True,
    }
    payload.update(overrides)
    return service.set_route_planner_v3_fine_policy(payload)


def refine(service, **overrides):
    payload = {
        "environment_source": "canonical_synthetic",
        "refinement_cell_size_m": 60.0,
        "max_stride_cells": 3,
    }
    payload.update(overrides)
    return service.evaluate_route_planner_v3_refinement(payload)


def latest_refinement(service):
    return service.route_planner_v3_snapshot()["records"][0]["refinements"][0]


def test_v3b_blank_project_has_no_fine_policy_defaults(tmp_path):
    project = blank_project({})
    policy = project["v3_fine_refinement_policy"]
    assert policy["horizontal_crs"] is None
    assert policy["resolution_m"] is None
    assert policy["resolution_source"] is None
    # The stored default carries no safety value and is not confirmed; the missing
    # CRS / resolution source are reported as *blocked*, and the stored default is
    # exactly the normalization of an empty input so a save/load round-trip is stable.
    assert policy["status"] == "blocked"
    assert policy["confirmed"] is False
    assert "30 m" in policy["source"]
    assert set(policy["missing_parameters"]) == {"horizontal_crs", "resolution_source"}
    assert policy["reasons"]
    assert normalize_v3_fine_refinement_policy(policy) == policy
    legacy = blank_project({})
    legacy.pop("v3_fine_refinement_policy")
    normalized = normalize_project(legacy, WorkspaceGridService())
    assert normalized["v3_fine_refinement_policy"]["resolution_m"] is None
    assert normalized["v3_fine_refinement_policy"]["horizontal_crs"] is None


def test_v3b_refinement_readiness_reports_sources_and_blocking_reasons(tmp_path):
    service = workflow(tmp_path)
    readiness = service.route_planner_v3_refinement_readiness()
    assert readiness["stage"] == "V3-B"
    assert readiness["model_scope"] == "corridor_local_3d_refinement_v3b"
    assert readiness["status"] == "blocked"
    assert "no_current_v3a_strategic_candidate_with_corridor" in readiness["blocking_reasons"]
    assert readiness["allowed_refinement_statuses"] == [
        "refined_candidate", "failed", "not_ready", "missing_data", "search_incomplete",
    ]
    assert readiness["resolution_policy"] == (
        "explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant"
    )
    assert readiness["environment_sources"] == ["canonical_synthetic", "configured_real_sources"]
    assert "corridor_local_metric_fine_grid" in readiness["stage_scope"]["implemented"]
    assert "exact_polygon_membership" in readiness["stage_scope"]["not_implemented"]
    assert readiness["real_data_readiness"]["adapter_id"] == "v3b_fine_environment_adapter"

    evaluate(service)
    service.set_route_planner_v3_policy(v3_policy())
    set_fine_policy(service)
    ready = service.route_planner_v3_refinement_readiness()
    assert ready["selected_strategic_candidate"]["corridor_id"]
    assert ready["fine_policy"]["resolution_m"] == 60.0
    assert ready["v3_policy_readiness"]["status"] == "ready"
    client_only = [
        reason for reason in ready["blocking_reasons"]
        if not reason.endswith("_not_configured_or_missing")
        and reason != "no_confirmed_allowed_airspace_cells"
    ]
    assert client_only == []


def test_v3b_refinement_runs_only_on_a_selected_current_strategic_candidate(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    with pytest.raises(ValueError, match="strategic_candidate"):
        refine(service)
    evaluate(service, synthetic_spec={
        "profile_id": "wall", "terrain_profile": "ridge_longitude",
        "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 600.0,
    })
    with pytest.raises(ValueError, match="strategic_candidate"):
        refine(service)
    assert service.state["route_planner_v3_experiments"]["records"][0]["refinements"] == []
    assert service.state["route_planner_v3_experiments"]["records"][0]["result"]["status"] == "failed"

    evaluate(service)
    refine(service)
    assert latest_refinement(service)["result"]["status"] == "refined_candidate"


def test_v3b_synthetic_refinement_produces_a_non_final_refined_candidate(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    evaluate(service, corridor_ring_n=1)
    before_routes = deepcopy(service.state["operational_routes"])
    before_selection = deepcopy(service.state["algorithm_selection"])
    before_spatial = deepcopy(service.state["spatial_3d"])
    refine(service)
    refinement = latest_refinement(service)
    result = refinement["result"]
    assert result["status"] == "refined_candidate"
    assert result["operational_route"] is False
    assert result["final_validation_performed"] is False
    assert result["v3c_validation_pending"] is True
    assert "未执行 V3-C" in result["disclaimer"]
    assert result["algorithm_id"] == "route_planner_v3_corridor_refinement"
    assert len(result["state_path"]) > 1
    assert result["distance_m"] > 0
    evidence = result["fine_grid_evidence"]
    assert evidence["resolution_m"] == 60.0
    assert evidence["resolution_source"] == "explicit_configuration"
    assert evidence["cell_count"] > 0
    assert evidence["environment_cell_count"] == evidence["cell_count"]
    assert evidence["corridor_support_cell_count"] > 0
    assert evidence["terrain_sampling"].startswith("intersecting_valid_fabdem_pixels_max")
    assert evidence["v3c_pending"]
    assert result["motion_model"]["multi_cell_stride"] is True
    assert result["motion_model"]["traversed_cells_are_checked"] is True
    assert result["search_statistics"]["search_complete"] is True
    assert result["hard_constraint_summary"]["intermediate_obstacles_cannot_be_skipped_by_a_stride"] is True
    assert result["trajectory_summary"]["traversed_cell_check_count"] > 0
    assert refinement["verdicts"] == {
        "operational_route": False,
        "final_validation_performed": False,
        "exact_validation_performed": False,
        "requires_v3c_exact_validation": True,
        "automatic_ranking": False,
        "automatically_scored": False,
    }
    # The V3-B write stays inside its own container.
    assert service.state["operational_routes"] == before_routes
    assert service.state["algorithm_selection"] == before_selection
    assert service.state["spatial_3d"] == before_spatial
    json.dumps(service.route_planner_v3_snapshot(), allow_nan=False)


def test_v3b_refinement_requires_a_confirmed_fine_policy_with_a_resolution_source(tmp_path):
    service = workflow(tmp_path)
    evaluate(service, corridor_ring_n=1)
    # A DTM-derived resolution cannot be resolved for a synthetic environment.
    service.evaluate_route_planner_v3_refinement({
        "environment_source": "canonical_synthetic", "max_stride_cells": 3,
        "fine_policy": {
            "horizontal_crs": "EPSG:32651", "resolution_source": "dtm_effective_resolution",
            "resolution_m": 60.0, "max_stride_cells": 3, "source": "x", "confirmed": True,
        },
    })
    blocked = latest_refinement(service)
    assert blocked["result"]["status"] == "not_ready"
    assert blocked["result"]["reason"] == (
        "synthetic_source_requires_explicit_configuration_resolution"
    )
    assert blocked["result"]["state_path"] == []

    # No resolution source at all is a configuration error: never assume 30 m.
    service.evaluate_route_planner_v3_refinement({
        "environment_source": "canonical_synthetic", "max_stride_cells": 3,
        "fine_policy": {
            "horizontal_crs": "EPSG:32651", "resolution_source": None,
            "max_stride_cells": 3, "source": "x", "confirmed": True,
        },
    })
    unresolved = latest_refinement(service)
    assert unresolved["result"]["status"] == "not_ready"
    assert unresolved["result"]["reason"] == "fine_resolution_unresolved"


def test_v3b_configured_real_sources_are_blocked_without_an_injected_adapter(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    evaluate(service, corridor_ring_n=1)
    refine(service, environment_source="configured_real_sources")
    refinement = latest_refinement(service)
    result = refinement["result"]
    assert result["status"] == "not_ready"
    assert result["reason"] == "configured_real_sources_unavailable"
    assert refinement["environment_source"] == "configured_real_sources"
    readiness = service.route_planner_v3_refinement_readiness()["real_data_readiness"]
    assert readiness["status"] == "blocked"
    assert "terrain_dtm_not_configured_or_missing" in readiness["blocking_reasons"]
    assert readiness["semantics"] == (
        "readiness_report_only_no_data_read_no_fabricated_environment"
    )
    with pytest.raises(ValueError, match="canonical_synthetic"):
        refine(service, environment_source="guessed_real_adapter")


def test_v3b_refinement_becomes_stale_when_policy_or_sources_change(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    evaluate(service, corridor_ring_n=1)
    refine(service)
    snapshot = service.route_planner_v3_refinement_snapshot()
    assert snapshot["count"] == 1
    assert snapshot["stale_count"] == 0
    assert snapshot["items"][0]["current_applicability"] == "current"
    assert snapshot["items"][0]["changed_components"] == []
    assert set(snapshot["components"]) == {
        "strategic_fingerprint", "corridor_fingerprint", "policy_fingerprint",
        "source_fingerprint", "frame_fingerprint", "fine_grid_fingerprint",
    }

    # A changed fine policy changes the fingerprint -> stale.
    set_fine_policy(service, resolution_m=45.0)
    stale = service.route_planner_v3_refinement_snapshot()
    assert stale["stale_count"] == 1
    assert stale["items"][0]["current_applicability"] == "stale"
    assert "policy_fingerprint" in stale["items"][0]["changed_components"]
    assert any("已变化" in reason for reason in stale["items"][0]["reasons"])

    # Restoring it makes the stored refinement current again.
    set_fine_policy(service)
    assert service.route_planner_v3_refinement_snapshot()["stale_count"] == 0

    # A synthetic refinement tracks the workspace/grid it was rebuilt inside.
    service.state["grid"] = WorkspaceGridService().generate(list(WORKSPACE), 8)
    grid_stale = service.route_planner_v3_refinement_snapshot()
    assert grid_stale["stale_count"] == 1
    assert "source_fingerprint" in grid_stale["items"][0]["changed_components"]

    # A real-source refinement tracks its own source audits instead.
    refine(service, environment_source="configured_real_sources")
    mixed = service.route_planner_v3_refinement_snapshot()
    assert mixed["count"] == 2
    real_item = next(
        item for item in mixed["items"] if item["status"] == "not_ready"
    )
    assert real_item["current_applicability"] == "current"
    service.state["source_audits"] = {
        "status": "passed", "schema_version": 1, "count": 1,
        "items": {"terrain_dtm": {
            "role": "terrain_dtm", "status": "needs_revalidation",
            "version_fingerprint": "CHANGED", "size_bytes": 10, "mtime_ns": 20,
        }},
    }
    changed_source = service.route_planner_v3_refinement_snapshot()
    source_stale = [
        item for item in changed_source["items"]
        if item["current_applicability"] == "stale"
    ]
    assert source_stale
    assert any(
        "source_fingerprint" in item["changed_components"] for item in source_stale
    )


def test_v3b_refinements_are_normalized_and_bounded_per_experiment(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    evaluate(service, corridor_ring_n=1)
    for cell_size in (60.0, 90.0, 120.0):
        refine(service, refinement_cell_size_m=cell_size)
    refinements = service.route_planner_v3_snapshot()["records"][0]["refinements"]
    assert len(refinements) == 3
    assert len({item["refinement_id"] for item in refinements}) == 3
    assert all(item["result"]["final_validation_performed"] is False for item in refinements)
    collection = normalize_v3_experiments(service.state["route_planner_v3_experiments"])
    assert collection["records"][0]["refinements"][0]["result"]["operational_route"] is False


def test_v3b_record_summary_projects_the_refinement_read_only(tmp_path):
    service = workflow(tmp_path)
    set_fine_policy(service)
    evaluate(service, corridor_ring_n=1)
    refine(service)
    record = service.route_planner_v3_snapshot()["records"][0]
    summary = record_summary(record)
    assert summary["refinement_count"] == 1
    assert summary["refinement_status"] == "refined_candidate"
    assert summary["refinement_resolution_m"] == 60.0
    assert summary["refinement_resolution_source"] == "explicit_configuration"
    assert summary["refinement_cell_count"] > 0
    assert summary["refinement_environment_cell_count"] == summary["refinement_cell_count"]
    assert summary["refinement_expanded_states"] > 0
    assert summary["refinement_final_validation_performed"] is False
    assert summary["refinement_environment_source"] == "canonical_synthetic"
    assert "state_path" not in json.dumps(summary)
    snapshot = service.snapshot()
    active = snapshot["route_planner_v3_experiments"]["active_experiment"]
    assert set(active) == set(summary)
    assert snapshot["route_planner_v3_refinements"]["count"] == 1
    assert snapshot["route_planner_v3_refinement_readiness"]["stage"] == "V3-B"
    json.dumps(snapshot["route_planner_v3_refinements"], allow_nan=False)


def test_v3b_refinement_refuses_an_unconfirmed_fine_policy(tmp_path):
    """Like the V3-A policy, an unconfirmed fine configuration never runs."""

    service = workflow(tmp_path)
    evaluate(service, corridor_ring_n=1)
    with pytest.raises(ValueError, match="未确认"):
        service.evaluate_route_planner_v3_refinement({
            "environment_source": "canonical_synthetic", "refinement_cell_size_m": 60.0,
            "fine_policy": {
                "horizontal_crs": "EPSG:32651",
                "resolution_source": "explicit_configuration",
                "resolution_m": 60.0, "source": "x", "confirmed": False,
            },
        })
    assert service.route_planner_v3_snapshot()["records"][0]["refinements"] == []
    # An unconfigured (blocked) stored policy is refused the same way.
    with pytest.raises(ValueError, match="未确认"):
        refine(service, fine_policy=None, refinement_cell_size_m=None)
    assert service.route_planner_v3_snapshot()["records"][0]["refinements"] == []


class RecordingRefinementWorkflow(RecordingWorkflow):
    def route_planner_v3_refinement_readiness(self):
        self.calls.append(("v3-refinement-readiness",))
        return {"status": "blocked", "stage": "V3-B"}

    def route_planner_v3_refinement_snapshot(self):
        self.calls.append(("v3-refinements",))
        return {"status": "not_calculated", "count": 0}

    def set_route_planner_v3_fine_policy(self, payload):
        self.calls.append(("v3-fine-policy", payload))
        return {"status": "passed", "fine_policy": payload}

    def evaluate_route_planner_v3_refinement(self, payload):
        self.calls.append(("v3-refine", payload))
        return {"status": "passed", "refined": True}
