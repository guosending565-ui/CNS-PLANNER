from copy import deepcopy
from types import SimpleNamespace

import pytest

from cns_planner.application.navigation_monitoring_simulation_service import (
    NavigationMonitoringSimulationService,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route_validation import path_fingerprint


DEMO_PATH = [[122.00, 30.00], [122.01, 30.00], [122.02, 30.00]]
CANONICAL_PATH = [[122.00, 30.00], [122.015, 30.006], [122.02, 30.00]]


class NoWriteSession:
    def __init__(self, state):
        self.state = state
        self.save_calls = 0

    def save(self):
        self.save_calls += 1
        raise AssertionError("只读仿真不得保存 ProjectState")


class CandidateProvider:
    def __init__(self, *, blocked_end=False):
        self.blocked_end = blocked_end
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        candidates = [
            {"candidate_id": "node:A", "source_id": "A", "candidate_kind": "project_node", "coordinate": [122.00, 30.00], "source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True}, "spatial_use_status": "eligible"},
            {"candidate_id": "node:B", "source_id": "B", "candidate_kind": "project_node", "coordinate": [122.02, 30.00], "source_crs": {"value": None if self.blocked_end else "EPSG:4326", "status": "pending_confirmation" if self.blocked_end else "confirmed", "confirmed": not self.blocked_end}, "spatial_use_status": "blocked" if self.blocked_end else "eligible"},
            {"candidate_id": "tower-colocation:T1", "source_id": "T1", "candidate_kind": "tower_colocation", "coordinate": [122.01, 30.001], "source_crs": {"value": "EPSG:4490", "status": "confirmed", "confirmed": True}, "spatial_use_status": "eligible"},
        ]
        return {"status": "passed", "candidates": candidates, "spatial_candidates": [item for item in candidates if item["spatial_use_status"] == "eligible"], "counts": {"total": 3, "towers": 1}, "corridor_width_policy_version": "fixed-1000m-v1", "corridor_half_width_m": 500, "full_width_service_assurance_status": "not_evaluated"}


class ArtifactReader:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def content(self, logical_key, **kwargs):
        self.calls.append((logical_key, kwargs))
        return {"status": "passed", "records": [{"part": "items", "index": i, "record": deepcopy(item)} for i, item in enumerate(self.records)]}


def _route(route_id, path, *, status="passed"):
    return {"route_id": route_id, "status": status, "path": deepcopy(path), "path_crs": "OGC:CRS84", "start_node_id": "A", "end_node_id": "B"}


def _candidate_record(candidate_id, candidate_fingerprint, path):
    return {"candidate_id": candidate_id, "route_id": "R0005", "status": "candidate", "current_applicability": "current", "candidate_fingerprint": candidate_fingerprint, "path": deepcopy(path)}


def _risk_profile(candidate_id, candidate_fingerprint, path):
    return {
        "profile_id": "RRP-CURRENT", "status": "passed", "current_applicability": "current",
        "candidate": {"candidate_id": candidate_id, "candidate_fingerprint": candidate_fingerprint},
        "route": {"route_id": "R0005", "path_fingerprint": path_fingerprint(path)},
        "domains": {"ground": {"high_risk": {"status": "available", "intervals": [{"start_distance_m": 200.0, "end_distance_m": 500.0}]}}},
        "fingerprints": {"profile_fingerprint": "risk-profile-current", "candidate_fingerprint": candidate_fingerprint},
    }


def _state():
    demo_route = _route("R0005", DEMO_PATH, status="engineering_demo_only")
    canonical_route = _route("R0005", CANONICAL_PATH)
    return {
        "revision": 468,
        "layered_operational_adoptions": {"items": [
            {"adoption_id": "LRA-DEMO", "route_id": "R0005", "status": "published_engineering_demo", "current_applicability": "current", "engineering_conditional_acceptance": True, "candidate_id": "LC-DEMO", "candidate_fingerprint": "candidate-demo", "validation_id": "LRV-DEMO", "validation_fingerprint": "validation-demo", "projection_fingerprint": "projection-demo", "after": {"route": demo_route}},
            {"adoption_id": "LRA-STRICT", "route_id": "R0005", "status": "published", "current_applicability": "current", "engineering_conditional_acceptance": False, "candidate_id": "LC-STRICT", "candidate_fingerprint": "candidate-strict", "validation_id": "LRV-STRICT", "validation_fingerprint": "validation-strict", "projection_fingerprint": "projection-strict", "after": {"route": canonical_route}},
        ]},
        "operational_routes": [canonical_route],
        "layered_route_candidates": {"status": "passed", "items": [], "detail_status": "externalized"},
        "artifact_manifest": {"refs": {"layered_route_candidates": {"artifact_id": "ART-CANDIDATES"}}},
        "route_risk_profiles": {"status": "passed", "items": [_risk_profile("LC-DEMO", "candidate-demo", DEMO_PATH)]},
        "existing_cns_facilities": {"items": [
            {"facility_id": "F-A", "site_id": "A", "distinct_site_id": "takeoff_landing_site:A", "name": "起点设施", "coordinate": DEMO_PATH[0], "status": "active", "devices": [{"device_id": "NAV-A", "service_key": "N:navigation_integrity_monitoring", "status": "active"}]},
            {"facility_id": "F-NEAR", "site_id": "OTHER", "distinct_site_id": "takeoff_landing_site:OTHER", "name": "终点同名", "coordinate": DEMO_PATH[-1], "status": "active", "devices": [{"device_id": "NAV-NEAR", "service_key": "N:navigation_integrity_monitoring", "status": "active"}]},
        ]},
        "spatial_3d": {"route_altitude_profiles": {}},
    }


def _service(*, blocked_end=False):
    state = _state()
    reader = ArtifactReader([
        _candidate_record("LC-DEMO", "candidate-demo", DEMO_PATH),
        _candidate_record("LC-STRICT", "candidate-strict", CANONICAL_PATH),
    ])
    session = NoWriteSession(state)
    service = NavigationMonitoringSimulationService(
        session,
        CandidateProvider(blocked_end=blocked_end),
        reader,
        candidate_applicability=lambda item: (
            "current" if item.get("candidate_id") == "LC-DEMO" else "stale_inputs_changed"
        ),
        risk_profile_snapshot=lambda: {
            "items": [{**_risk_profile("LC-DEMO", "candidate-demo", DEMO_PATH),
                       "current_applicability": "current"}],
        },
    )
    return service, session, reader


def test_read_time_currentness_projection_wins_over_persisted_fields():
    service, session, _ = _service()
    session.state["route_risk_profiles"]["items"][0]["current_applicability"] = "stale"

    result = service.simulate({"route_source": "engineering_demo", "route_id": "R0005"})

    assert result["risk_priority_status"] == "available"
    assert result["risk_unavailable_reasons"] == []


def test_projected_stale_risk_profile_remains_unavailable():
    service, _, _ = _service()
    service.risk_profile_snapshot = lambda: {
        "items": [{**_risk_profile("LC-DEMO", "candidate-demo", DEMO_PATH),
                   "current_applicability": "stale_request_or_inputs_changed"}],
    }

    result = service.simulate({"route_source": "engineering_demo", "route_id": "R0005"})

    assert result["risk_priority_status"] == "unavailable"
    assert "current_risk_profile_unavailable" in result["risk_unavailable_reasons"]


def test_demo_route_identity_uses_hydrated_candidate_and_current_risk_only():
    service, session, reader = _service()
    before = deepcopy(session.state)
    result = service.simulate({"route_source": "engineering_demo", "route_id": "R0005", "parameters": {"monitor_association_radius_m": 200}})

    assert result["route_identity"] == {
        "route_authority": "engineering_demo_projection",
        "route_id": "R0005",
        "adoption_id": "LRA-DEMO",
        "candidate_id": "LC-DEMO",
        "candidate_fingerprint": "candidate-demo",
        "validation_id": "LRV-DEMO",
        "source_crs": "OGC:CRS84",
        "construction_crs": "EPSG:32651",
        "point_count": 3,
        "route_geometry_fingerprint": path_fingerprint(DEMO_PATH),
        "currentness": "current",
    }
    assert result["risk_priority_status"] == "available"
    assert result["risk_identity"]["profile_id"] == "RRP-CURRENT"
    assert result["risk_identity"]["profile_fingerprint"] == "risk-profile-current"
    assert reader.calls and reader.calls[0][0] == "layered_route_candidates"
    assert session.state == before
    assert session.save_calls == 0


def test_canonical_route_never_reuses_demo_geometry_or_risk_and_checks_request_fingerprint():
    service, _, _ = _service()
    canonical_fp = path_fingerprint(CANONICAL_PATH)
    result = service.simulate({"route_source": "operational_route", "route_id": "R0005", "route_geometry_fingerprint": canonical_fp, "parameters": {"monitor_association_radius_m": 200}})
    assert result["route_identity"]["route_authority"] == "canonical_operational_route"
    assert result["route_identity"]["candidate_fingerprint"] == "candidate-strict"
    assert result["route_identity"]["route_geometry_fingerprint"] == canonical_fp
    assert result["route_identity"]["route_geometry_fingerprint"] != path_fingerprint(DEMO_PATH)
    assert result["risk_priority_status"] == "unavailable"
    assert "candidate_fingerprint_mismatch" in result["risk_unavailable_reasons"]

    with pytest.raises(ValueError, match="几何指纹"):
        service.simulate({"route_source": "operational_route", "route_id": "R0005", "route_geometry_fingerprint": path_fingerprint(DEMO_PATH)})


def test_endpoint_crs_blocks_whole_simulation_while_missing_elevation_only_disables_3d_claims():
    blocked, _, _ = _service(blocked_end=True)
    result = blocked.simulate({"route_source": "engineering_demo", "route_id": "R0005"})
    assert result["status"] == "blocked"
    assert result["reason"] == "mandatory_endpoint_spatial_evidence_unavailable"

    service, _, _ = _service()
    result = service.simulate({"route_source": "engineering_demo", "route_id": "R0005"})
    assert result["status"] != "blocked"
    assert result["three_dimensional_assurance_status"] == "not_evaluated"
    assert result["three_dimensional_assurance_reason"] == "route_elevation_evidence_unavailable"


def test_exact_existing_navigation_device_is_reused_once_and_proximity_match_is_ignored():
    service, _, _ = _service()
    result = service.simulate({"route_source": "engineering_demo", "route_id": "R0005", "parameters": {"max_optional_monitor_sites": 0}})
    devices = {item["site_id"]: item for item in result["devices"]}
    assert devices["node:A"]["device_id"] == "NAV-A"
    assert devices["node:A"]["provisioning"] == "reused_existing"
    assert devices["node:B"]["device_id"] != "NAV-NEAR"
    assert len({item["device_id"] for item in result["devices"]}) == len(result["devices"])


def test_parameters_recompute_result_and_workflow_facade_remains_read_only():
    service, session, _ = _service()
    before = deepcopy(session.state)
    first = service.simulate({"route_source": "engineering_demo", "route_id": "R0005", "parameters": {"monitor_association_radius_m": 100, "route_speed_mps": 15, "gap_tolerance_s": 10, "max_optional_monitor_sites": 0}})
    second = service.simulate({"route_source": "engineering_demo", "route_id": "R0005", "parameters": {"monitor_association_radius_m": 150, "route_speed_mps": 20, "gap_tolerance_s": 12, "max_optional_monitor_sites": 1}})
    assert first["parameter_fingerprint"] != second["parameter_fingerprint"]
    assert first["result_fingerprint"] != second["result_fingerprint"]
    assert first["candidate_snapshot_fingerprint"] == second["candidate_snapshot_fingerprint"]
    assert first["writes_performed"] == second["writes_performed"] == 0
    assert session.state == before

    workflow = object.__new__(WorkflowService)
    workflow.navigation_monitoring_simulation_service = service
    assert workflow.simulate_navigation_monitoring({"route_source": "engineering_demo", "route_id": "R0005"})["writes_performed"] == 0
