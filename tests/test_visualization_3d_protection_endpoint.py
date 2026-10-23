from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

from cns_planner.api.router import ApiRouter
from cns_planner.application.visualization_3d_service import Visualization3DService


HISTORICAL_PATH = [[122.20, 29.90], [122.21, 29.905], [122.22, 29.91]]
DEMO_PATH = [[122.20, 29.90], [122.207, 29.902], [122.214, 29.907], [122.22, 29.91]]


def _state():
    return {
        "revision": 468,
        "spatial_3d": {
            "canonical_vertical_reference": "egm2008_orthometric",
            "altitude_layers": [{
                "altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0,
                "lower_altitude_m": 90.0, "upper_altitude_m": 125.0,
                "vertical_reference": "EGM2008", "status": "confirmed", "confirmed": True,
            }],
            "route_operating_layers": [{
                "route_id": "R0005", "altitude_layer_id": "ALT-100",
                "active": True, "confirmed": False,
                "source": "software project default, non measured/project source",
            }],
            "route_3d_profiles": {"items": []},
        },
        "operational_routes": {"items": [{
            "route_id": "R0005", "status": "stale", "stale_reason": "workspace_changed",
            "path": HISTORICAL_PATH,
            "provenance": {"candidate_id": "historical-11", "validation_id": "VAL-11"},
        }]},
        "layered_operational_adoptions": {"items": [{
            "status": "published_engineering_demo", "current_applicability": "current",
            "adoption_id": "ADOPT-DEMO", "route_id": "R0005",
            "candidate_id": "demo-14", "validation_id": "VAL-14",
            "projection_fingerprint": "demo-projection",
            "strict_safety_validation_passed": False,
            "operational_authorization": False,
            "after": {"route": {
                "route_id": "R0005", "status": "engineering_demo_only", "path": DEMO_PATH,
            }},
        }]},
        "continuous_service_acceptability": {
            "status": "stale", "stale_reason": "p16_or_upstream_changed",
            "algorithm_id": "continuous_service_acceptability_v1",
            "algorithm_version": "2.2", "input_fingerprint": "p17-input",
            "routes": [{
                "route_id": "R0005",
                "corridor": {
                    "status": "evaluated", "route_path": HISTORICAL_PATH,
                    "route_length_m": 9904.422448544,
                    "cns_requirement_corridor_half_width_m": 500.0,
                    "D_separation_m": 30.0, "D_separation_authority": "explicit_evidence",
                    "D_maneuver_m": 50.0,
                    "D_maneuver_authority": "builtin_engineering_assumption",
                    "D_uncertainty_m": 10.0,
                    "D_uncertainty_authority": "explicit_evidence",
                    "V_relative_mps": 40.0, "relative_speed_basis": "conservative",
                    "T_chain_s": 10.0,
                    "T_chain_components_s": {
                        "detect_track": 3.0, "sensor_to_platform": 1.0,
                        "platform_processing": 2.0, "platform_to_aircraft": 1.0,
                        "aircraft_response_manoeuvre": 3.0,
                    },
                    "D_protection_m": 490.0, "outer_half_width_m": 990.0,
                },
            }],
        },
        "encounter_3d_assessment": {"status": "not_calculated", "tracks": []},
    }


def _router(service, monkeypatch):
    from cns_planner.application import visualization_3d_service as module

    monkeypatch.setattr(module, "service_for", lambda context: service)
    router = ApiRouter.__new__(ApiRouter)
    router._visualization_3d = ApiRouter._visualization_3d.__get__(router, ApiRouter)
    return router, SimpleNamespace(_visualization_3d_service=service)


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def test_project_result_discloses_historical_match_and_current_demo_conflict():
    state = _state()
    before = _hash(state)
    service = Visualization3DService(state, data=SimpleNamespace(paths={}))

    payload = service.protection_volumes({})

    assert payload["revision"] == 468
    assert payload["status"] == "historical_only"
    assert payload["intrusion_contract"]["status"] == "blocked"
    by_authority = {row["route_authority"]: row for row in payload["volumes"]}
    assert by_authority["canonical_operational_routes"]["status"] == "historical_stale"
    assert by_authority["engineering_demo_projection"]["status"] == (
        "geometry_version_conflict")
    assert payload["read_only"] is True
    assert payload["writes_performed"] == 0
    assert _hash(state) == before


def test_preview_requires_explicit_route_width_and_both_heights():
    service = Visualization3DService(_state(), data=SimpleNamespace(paths={}))
    payload = service.protection_volumes({"mode": "engineering_scenario_preview"})

    assert payload["status"] == "not_calculated"
    assert payload["selection_required"] is True
    assert payload["volumes"] == []
    assert set(payload["required_inputs"]) == {
        "route_geometry_fingerprint", "half_width_m",
        "lower_altitude_m", "upper_altitude_m",
    }
    assert len(payload["route_options"]) == 2
    assert payload["default_values"] == {}


def test_preview_endpoint_is_read_only_etagged_and_isolated(monkeypatch):
    state = _state()
    before = _hash(state)
    service = Visualization3DService(state, data=SimpleNamespace(paths={}))
    demo = next(row for row in service.routes()["routes"]
                if row["authority"] == "engineering_demo_projection")
    query = {
        "mode": ["engineering_scenario_preview"],
        "route_geometry_fingerprint": [demo["identity"]["fingerprint"]],
        "half_width_m": ["250"],
        "lower_altitude_m": ["90"],
        "upper_altitude_m": ["125"],
    }
    router, context = _router(service, monkeypatch)

    response = router._visualization_3d(
        context, "/api/visualization-3d/protection-volumes", query, {})

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["ETag"].startswith('"vis3dprotectionvolumesv1-')
    payload = response.data
    assert payload["status"] == "engineering_preview"
    assert payload["volume_count"] == 1
    assert payload["volumes"][0]["route_geometry_identity"] == demo["identity"]
    assert payload["volumes"][0]["isolation"]["written_to_project_state"] is False
    assert _hash(state) == before

    cached = router._visualization_3d(
        context, "/api/visualization-3d/protection-volumes", query,
        {"If-None-Match": response.headers["ETag"]})
    assert cached.status == 304
    assert cached.headers["Cache-Control"] == "no-store"


def test_preview_invalid_vertical_order_returns_400(monkeypatch):
    service = Visualization3DService(_state(), data=SimpleNamespace(paths={}))
    demo = next(row for row in service.routes()["routes"]
                if row["authority"] == "engineering_demo_projection")
    router, context = _router(service, monkeypatch)
    response = router._visualization_3d(
        context, "/api/visualization-3d/protection-volumes", {
            "mode": ["engineering_scenario_preview"],
            "route_geometry_fingerprint": [demo["identity"]["fingerprint"]],
            "half_width_m": ["250"], "lower_altitude_m": ["125"],
            "upper_altitude_m": ["90"],
        }, {})

    assert response.status == 400
    assert response.data["code"] == "protection_volume_preview_invalid"


def test_manifest_marks_protection_as_available_with_read_only_endpoint():
    service = Visualization3DService(_state(), data=SimpleNamespace(paths={}))
    manifest = service.manifest()
    layer = next(row for row in manifest["layers"] if row["id"] == "protection_volumes")

    assert layer["status"] == "available"
    assert layer["reserved"] is False
    assert layer["default_on"] is False
    assert layer["detail_endpoint"] == "/api/visualization-3d/protection-volumes"
