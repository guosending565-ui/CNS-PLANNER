from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_planning_objectives import normalize_cns_planning_objectives


DEFAULTS = Path("cns_planner/config/defaults.json")


def provider(identifier, *, group=None, independent=False, status="meets_under_model", facility="F1"):
    return {
        "facility_id": facility, "device_id": identifier, "status": status,
        "independence_confirmed": independent,
        "independence_group": group,
    }


def voxel(identifier, offset, half, *, service="satisfied", providers=None, volume=100.0, code="C"):
    entries = []
    for subsystem in ("C", "N", "S"):
        current = service if subsystem == code else "not_applicable"
        entries.append({
            "subsystem": subsystem,
            "planning_status": current,
            "p8_status": {
                "satisfied": "meets_under_model", "confirmed_deficit": "does_not_meet_under_model",
                "unknown": "unknown", "not_applicable": "not_applicable",
            }[current],
            "provider_evaluations": deepcopy(providers or []) if subsystem == code else [],
            "reasons": [],
            "evidence": ([{"kind": "aircraft_navigation"}] if subsystem == "N" and code == "N" else []),
        })
    return {
        "voxel_id": identifier, "grid_id": identifier.split("@")[0],
        "altitude_layer_id": identifier.split("@")[1],
        "nearest_route_offset_m": offset, "cell_half_diagonal_m": half,
        "discretized_volume_proxy_m3": volume, "subsystems": entries,
    }


def corridor(voxels, status="passed", length=100.0):
    return {
        "status": status, "algorithm_id": "cns_service_corridor_v1", "algorithm_version": "1.0",
        "input_fingerprint": "p14-fixture", "routes": [{
            "route_id": "R1", "route_length_m": length, "status": "passed",
            "voxels": deepcopy(voxels),
        }],
    }


def required(code="C", redundancy=1):
    result = {
        "communication": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }
    names = {"C": "communication", "N": "navigation", "S": "surveillance"}
    result[names[code]] = {
        "required": True, "status": "passed",
        "type": {"technology": "gnss"} if code == "N" else {},
        "performance": {"min_redundancy": redundancy},
    }
    return {"status": "passed", "project_default": result, "route_overrides": {}}


def objectives(entries=None):
    return normalize_cns_planning_objectives({"routes": {"R1": {"subsystems": {"C": {"objectives": entries or {}}}}}})


def result(voxels, *, requirement=None, objective_values=None, status="passed"):
    return CNSCorridorGapAnalyzerV1().evaluate(
        corridor(voxels, status=status), requirement or required(),
        objectives(objective_values) if objective_values is not None else normalize_cns_planning_objectives(),
    )


def subsystem(assessment, code="C"):
    return next(item for item in assessment["routes"][0]["subsystems"] if item["subsystem"] == code)


def test_one_provider_satisfies_one_but_not_confirmed_two_provider_redundancy():
    one = provider("D1", group="power-a", independent=True)
    satisfied = subsystem(result([voxel("G1@L1", 50, 10, providers=[one])]))
    assert satisfied["redundancy"]["voxel_counts"]["satisfied"] == 1
    assessment = result([voxel("G1@L1", 50, 10, providers=[one])], requirement=required(redundancy=2))
    deficit = subsystem(assessment)
    entry = voxel_entry(assessment)
    assert entry["confirmed_independent_provider_count"] == 1
    assert entry["redundancy_status"] == "confirmed_deficit"
    assert deficit["confirmed_target_voxel_ids"] == ["G1@L1"]


def test_two_providers_without_independence_are_unknown_not_confirmed_gap():
    providers = [provider("D1"), provider("D2", facility="F2")]
    assessment = result([voxel("G1@L1", 50, 10, providers=providers)], requirement=required(redundancy=2))
    entry = voxel_entry(assessment)
    assert entry["qualified_provider_count"] == 2
    assert entry["confirmed_independent_provider_count"] is None
    assert entry["redundancy_status"] == "unknown"
    assert entry["combined_status"] == "unknown"
    assert assessment["confirmed_target_voxel_ids"] == []


def test_two_confirmed_independent_groups_satisfy_and_same_site_has_no_penalty():
    providers = [
        provider("D1", group="power-a", independent=True, facility="SAME"),
        provider("D2", group="power-b", independent=True, facility="SAME"),
    ]
    assessment = result([voxel("G1@L1", 50, 10, providers=providers)], requirement=required(redundancy=2))
    entry = voxel_entry(assessment)
    assert entry["confirmed_independent_provider_count"] == 2
    assert entry["combined_status"] == "satisfied"
    assert assessment["not_evaluated"]["common_cause"] == "not_evaluated"
    assert "common_cause" not in entry["causes"]


def test_non_site_gnss_keeps_service_and_does_not_create_ground_redundancy_gap():
    assessment = result(
        [voxel("G1@L1", 50, 10, service="satisfied", providers=[], code="N")],
        requirement=required("N", redundancy=2),
    )
    nav = subsystem(assessment, "N")
    entry = next(item for item in assessment["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "N")
    assert entry["ground_provider_redundancy_status"] == "not_applicable_to_site_provider_redundancy"
    assert entry["combined_status"] == "satisfied"
    assert nav["confirmed_target_voxel_ids"] == []
    assert nav["redundancy"]["volume_proxy_m3"]["not_applicable"] == 100
    assert sum(nav["redundancy"]["volume_proxy_m3"].values()) == nav["redundancy"]["required_volume_proxy_m3"]


def test_unknown_service_is_not_confirmed_and_volume_classification_is_conservative():
    assessment = result([voxel("G1@L1", 50, 10, service="unknown", providers=[])])
    c = subsystem(assessment)
    assert c["confirmed_target_voxel_ids"] == []
    assert c["unknown_voxel_ids"] == ["G1@L1"]
    assert c["combined"]["volume_proxy_m3"]["unknown"] == 100
    assert sum(c["combined"]["volume_proxy_m3"].values()) == c["required_volume_proxy_m3"]


def test_spatial_continuous_deficit_merges_clips_and_is_deterministic():
    failed = []
    for identifier, offset, half in (("G1@L1", 10, 20), ("G2@L1", 40, 20), ("G3@L1", 95, 20)):
        failed.append(voxel(identifier, offset, half, service="confirmed_deficit"))
    first, second = result(failed), result(failed)
    c = subsystem(first)
    assert [(item["start_route_offset_m"], item["end_route_offset_m"]) for item in c["continuous_deficit_segments"]] == [(0.0, 60.0), (75.0, 100.0)]
    assert c["total_confirmed_deficit_projection_m"] == pytest.approx(85)
    assert c["max_continuous_deficit_projection_m"] == pytest.approx(60)
    assert c["continuous_deficit_semantics"] == "conservative_longitudinal_projection_of_corridor_voxel_deficits"
    assert first == second


@pytest.mark.parametrize(("definition", "expected"), [
    ({"min_satisfied_volume_fraction": {"value": 1, "operator": ">=", "source": "test", "confirmed": True}}, "objectives_met"),
    ({"max_confirmed_deficit_volume_fraction": {"value": 0, "operator": "<=", "source": "test", "confirmed": True}}, "objectives_met"),
    ({"min_satisfied_volume_fraction": {"value": 1.1, "operator": ">=", "source": "test", "confirmed": True}}, "invalid"),
])
def test_objective_contract_and_met_states(definition, expected):
    if expected == "invalid":
        with pytest.raises(ValueError, match="0..1"):
            objectives(definition)
        return
    c = subsystem(result([voxel("G1@L1", 50, 10, providers=[provider("D1")])], objective_values=definition))
    assert c["objective_status"] == expected
    assert c["objective_results"][0]["status"] == "met"


def test_objectives_not_met_unknown_and_not_configured_without_defaults():
    not_met = {"min_satisfied_volume_fraction": {"value": 1, "operator": ">=", "source": "test", "confirmed": True}}
    c = subsystem(result([voxel("G1@L1", 50, 10, service="confirmed_deficit")], objective_values=not_met))
    assert c["objective_status"] == "objectives_not_met"
    assert c["objective_results"][0]["status"] == "not_met"
    unknown = subsystem(result([voxel("G1@L1", 50, 10, providers=[provider("D1")], volume=None)], objective_values=not_met))
    assert unknown["objective_status"] == "objectives_unknown"
    assert unknown["objective_results"][0]["status"] == "unknown"
    not_configured = subsystem(result([voxel("G1@L1", 50, 10, providers=[provider("D1")])]))
    assert not_configured["objective_status"] == "objectives_not_configured"
    assert normalize_cns_planning_objectives()["routes"] == {}
    pending = {"min_satisfied_volume_fraction": {"value": 1, "operator": ">=", "source": "draft", "confirmed": False}}
    pending_result = subsystem(result([voxel("G1@L1", 50, 10, providers=[provider("D1")])], objective_values=pending))
    assert pending_result["objective_status"] == "objectives_not_configured"
    assert pending_result["objective_results"][0]["status"] == "unknown"


@pytest.mark.parametrize("status", ["stale", "missing_data", "not_calculated"])
def test_stale_or_missing_p14_is_rejected(status):
    assessment = result([], status=status)
    assert assessment["status"] == "missing_data"
    assert assessment["routes"] == []


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_api_persistence_backfill_and_one_way_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    assert workflow.algorithm_registry.manifest("corridor_gap_analyzer", "cns_corridor_gap_v1", "1.0")
    workflow.state["cns_corridor_assessment"] = corridor([
        voxel("G1@L1", 50, 10, providers=[provider("D1")])
    ])
    api = ApiRouter(ApiContext(workflow))
    configured = objectives({
        "min_satisfied_volume_fraction": {"value": 0.5, "operator": ">=", "source": "test", "confirmed": True}
    })
    api.post("/api/cns-planning-objectives", {"cns_planning_objectives": configured})
    response = api.post("/api/cns-corridor-gap/evaluate", {}).data
    assert response["cns_corridor_gap_assessment"]["algorithm_id"] == "cns_corridor_gap_v1"
    assert api.get("/api/cns-planning-objectives", {}, {}).data["status"] == "confirmed"
    assert api.get("/api/cns-corridor-gap", {}, {}).data["input_fingerprint"]
    restored = WorkflowService(path, DEFAULTS)
    assert restored.cns_corridor_gap_snapshot()["input_fingerprint"] == response["cns_corridor_gap_assessment"]["input_fingerprint"]
    restored.state["result_statuses"].update({
        "cns_corridor_gap_assessment": "passed", "cns_corridor_assessment": "passed",
        "routes": "passed", "coverage_3d": "passed", "cns_gap_v2": "passed",
    })
    restored.state["cns_corridor_gap_assessment"]["status"] = "passed"
    restored.invalidation_service.workflow("planning_objectives")
    assert restored.state["result_statuses"]["cns_corridor_gap_assessment"] == "stale"
    assert {restored.state["result_statuses"][key] for key in ("cns_corridor_assessment", "routes", "coverage_3d", "cns_gap_v2")} == {"passed"}
    restored.state["cns_corridor_assessment"]["status"] = "passed"
    restored.state["cns_corridor_gap_assessment"]["status"] = "passed"
    restored.state["result_statuses"]["cns_corridor_gap_assessment"] = "passed"
    restored.invalidation_service.cns_corridor()
    assert restored.state["result_statuses"]["cns_corridor_gap_assessment"] == "stale"
    legacy = deepcopy(restored.state)
    for key in ("cns_planning_objectives", "cns_corridor_gap_assessment"):
        legacy.pop(key)
    legacy["result_statuses"].pop("cns_corridor_gap_assessment")
    from cns_planner.application.project_state import normalize_project
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["cns_planning_objectives"]["status"] == "objectives_not_configured"
    assert normalized["cns_corridor_gap_assessment"]["status"] == "not_calculated"


def voxel_entry(assessment, code="C"):
    return next(
        item for item in assessment["routes"][0]["voxels"][0]["subsystems"]
        if item["subsystem"] == code
    )
