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
    V3_REAL_DATA_ADAPTER_STATUS, RoutePlannerV3ExperimentService, record_summary,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.route_planner_v3.contracts import (
    empty_v3_experimental_session, normalize_v3_experiments, normalize_v3_planning_policy,
)

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


def test_v3_evaluation_never_writes_operational_routes_or_switches_the_planner(tmp_path):
    service = workflow(tmp_path)
    service.generate_scenario_od("N001", "N002", "ab")
    service.generate_operational([])
    before_routes = deepcopy(service.state["operational_routes"])
    before_selection = deepcopy(service.state["algorithm_selection"])
    before_status = service.state["result_statuses"]["routes"]
    before_spatial = deepcopy(service.state["spatial_3d"])
    before_experiments = deepcopy(service.state["route_planning_experiments"])

    snapshot = evaluate(service)

    assert service.state["operational_routes"] == before_routes
    assert service.state["algorithm_selection"] == before_selection
    assert service.state["result_statuses"]["routes"] == before_status
    assert service.state["spatial_3d"] == before_spatial
    assert service.state["route_planning_experiments"] == before_experiments
    collection = snapshot["route_planner_v3_experiments"]
    assert collection["count"] == 1
    assert collection["operational_routes_untouched"] is True
    assert collection["algorithm_selection_untouched"] is True
    record = latest_record(service)
    assert record["provenance"]["operational_routes_untouched"] is True
    assert record["provenance"]["algorithm_selection_untouched"] is True
    assert record["provenance"]["spatial_3d_untouched"] is True


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
        "operational route；未做 30 m 局部精化、未做 exact polygon/terrain 最终判定、未做 CNS 联合优化。"
    )
    assert record["verdicts"]["operational_route"] is False
    assert record["verdicts"]["final_validation_performed"] is False
    assert snapshot["route_planner_v3_experiments"]["allowed_result_statuses"] == [
        "strategic_candidate", "failed", "missing_data", "pending_confirmation", "not_ready",
    ]
    assert snapshot["route_planner_v3_experiments"]["architecture"]
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
    assert "30m_local_refinement" in scope["not_implemented"]
    assert "exact_polygon_terrain_final_validation" in scope["not_implemented"]
    assert "cns_joint_optimization" in scope["not_implemented"]
    assert "energy_model" in scope["not_implemented"]


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


def test_v3_api_endpoints_are_additive_and_forward_payloads():
    context = ApiContext()
    router = ApiRouter(context)
    assert router.get("/api/route-planner-v3-experiments", {}, {}).data == {"status": "passed", "count": 1}
    assert router.get("/api/route-planner-v3/readiness", {}, {}).data["stage"] == "V3-A"
    assert router.post("/api/route-planner-v3/policy", {"min_altitude_egm2008_m": 100.0}).data["status"] == "passed"
    assert router.post(
        "/api/route-planner-v3-experiments/evaluate", {"environment_source": "canonical_synthetic"},
    ).data["evaluated"] is True
    assert router.post(
        "/api/route-planner-v3-experiments/delete", {"experiment_id": "V3-AAAAAAAAAAAA"},
    ).data["deleted"] == "V3-AAAAAAAAAAAA"
    assert context.workflow.calls == [
        ("v3-snapshot",),
        ("v3-readiness",),
        ("v3-policy", {"min_altitude_egm2008_m": 100.0}),
        ("v3-evaluate", {"environment_source": "canonical_synthetic"}),
        ("v3-delete", "V3-AAAAAAAAAAAA"),
    ]


def test_v3_api_paths_are_distinct_from_the_existing_route_experiment_api():
    paths = ["/api/route-experiments", "/api/route-planner-v3-experiments"]
    assert len(set(paths)) == 2
    assert "/api/route-planner-v3-experiments/evaluate" != "/api/route-experiments/evaluate"


# --------------------------------------------------------------------------------------
# V1/V2/P7/P19 boundary
# --------------------------------------------------------------------------------------


def test_v3_does_not_change_the_algorithm_registry_catalog_or_default_selection():
    catalog = build_default_algorithm_registry({}).catalog()
    ids = {(item["algorithm_type"], item["algorithm_id"], item["version"]) for item in catalog}
    assert ("route_planner", "route_planner_v1", "1.0") in ids
    assert ("route_planner", "risk_aware_route_planner_v2", "2.0") in ids
    assert not any(item[1].startswith("route_planner_v3") for item in ids)
    selection = default_algorithm_selection()
    assert selection["route_planner"]["algorithm_id"] == "route_planner_v1"
    assert normalize_v3_planning_policy(None)["confirmed"] is False


def test_v3_service_is_wired_into_the_workflow_without_a_registry_entry(tmp_path):
    service = workflow(tmp_path)
    assert isinstance(service.route_planner_v3_service, RoutePlannerV3ExperimentService)
    assert service.route_planner_v3_service.planner.algorithm_id == "route_planner_v3_strategic"
    assert service.route_planner_v3_service.planner.uses_v3_native_3d is True
    assert service.route_planner_v3_service is not service.route_experiment_service
