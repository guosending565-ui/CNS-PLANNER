import json

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.catalogs import AircraftCNSProfileCatalog, DeviceCatalog
from cns_planner.domain.cns_inputs import normalize_required_cns
from cns_planner.gis.cns_input_adapter import CNSInputAdapter


CONFIG = __import__("pathlib").Path("cns_planner/config")


def service(tmp_path):
    return WorkflowService(tmp_path / "project.json", CONFIG / "defaults.json")


def test_aircraft_and_device_catalogs_load_without_conflating_requirements():
    aircraft = AircraftCNSProfileCatalog.load(CONFIG)
    devices = DeviceCatalog.load(CONFIG)
    assert aircraft["status"] == "passed"
    assert aircraft["items"][0]["communication"]["status"] == "pending_confirmation"
    assert isinstance(aircraft["items"][0]["communication"]["capabilities"], list)
    assert devices["status"] == "passed"
    assert {item["subsystem"] for item in devices["items"]} == {"C", "N", "S"}
    required = normalize_required_cns(None)
    assert required["status"] == "pending_confirmation"
    assert required["metadata"]["semantics"] == "mission_requirement_not_aircraft_capability"
    assert required["project_default"]["communication"]["required"] is None


def test_device_catalog_adapter_preserves_active_v1_shape(tmp_path):
    workflow = service(tmp_path)
    active = workflow.snapshot()["devices"]
    assert DeviceCatalog.to_coverage_v1(workflow.state["device_catalog"], active) == active


def test_required_cns_project_and_route_override_round_trip(tmp_path):
    workflow = service(tmp_path)
    requirements = {
        "communication": {"required": True, "coverage_requirement": 95, "max_gap_m": 500, "latency_ms": 100, "redundancy": 2},
        "navigation": {"required": False},
        "surveillance": {"required": True, "coverage_requirement": 90, "update_interval_s": 2, "redundancy": 1},
    }
    result = workflow.set_required_cns({"scope": "project", "requirements": requirements})
    assert result["required_cns"]["status"] == "passed"
    restored = service(tmp_path).snapshot()
    assert restored["required_cns"]["project_default"]["communication"]["max_gap_m"] == 500


def test_existing_facilities_merge_devices_and_round_trip(tmp_path):
    workflow = service(tmp_path)
    items = [
        {"facility_id": "F1", "name": "站点一", "coordinate": [122.0, 30.0], "subsystem": "C", "device_id": "C1"},
        {"facility_id": "F1", "name": "站点一", "coordinate": [122.0, 30.0], "subsystem": "N", "device_id": "N1"},
    ]
    result = workflow.import_existing_cns({"items": items})
    facilities = result["existing_cns_facilities"]
    assert facilities["count"] == 1
    assert [item["subsystem"] for item in facilities["items"][0]["devices"]] == ["C", "N"]
    restored = service(tmp_path).snapshot()
    assert restored["existing_cns_facilities"] == facilities


def test_candidate_sites_import_geojson_derive_and_round_trip(tmp_path):
    workflow = service(tmp_path)
    result = workflow.import_candidate_sites({"items": [{
        "site_id": "S1", "name": "铁塔一", "coordinate": [122.01, 30.01],
        "site_type": "tower", "available_subsystems": ["C", "S"],
        "usable": True, "locked": False,
    }]})
    assert result["candidate_sites"]["items"][0]["site_type"] == "tower"
    restored = service(tmp_path).snapshot()
    assert restored["candidate_sites"]["items"][0]["site_id"] == "S1"
    workflow.import_existing_cns({"items": [{
        "facility_id": "F2", "coordinate": [122.02, 30.02],
        "devices": [{"device_id": "D", "subsystem": "S"}],
    }]})
    derived = workflow.candidate_sites_from_existing()["candidate_sites"]["items"][0]
    assert derived["site_id"] == "F2"
    assert derived["locked"] is True
    assert derived["available_subsystems"] == ["S"]


def test_adapter_reads_csv_and_geojson(tmp_path):
    csv_path = tmp_path / "facilities.csv"
    csv_path.write_text("facility_id,name,longitude,latitude,subsystem,device_id\nF1,A,122,30,C,D1\n", encoding="utf-8")
    facilities = CNSInputAdapter().load_existing({"path": str(csv_path)})
    assert facilities["items"][0]["coordinate"] == [122.0, 30.0]
    geojson_path = tmp_path / "sites.geojson"
    geojson_path.write_text(json.dumps({
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "g1", "geometry": {"type": "Point", "coordinates": [122.1, 30.1]},
            "properties": {"site_id": "S1", "available_subsystems": ["N"]},
        }]
    }), encoding="utf-8")
    candidates = CNSInputAdapter().load_candidates({"path": str(geojson_path)})
    assert candidates["items"][0]["metadata"]["feature_id"] == "g1"


def test_old_schema_v2_backfills_new_cns_fields(tmp_path):
    initial = service(tmp_path)
    document = initial.state.copy()
    for key in ("aircraft_profiles", "selected_aircraft_profile_id", "required_cns", "device_catalog", "existing_cns_facilities", "candidate_sites"):
        document.pop(key, None)
    initial.repository.save(document)
    restored = service(tmp_path).snapshot()
    assert restored["aircraft_profiles"]["status"] == "passed"
    assert restored["device_catalog"]["status"] == "passed"
    assert restored["required_cns"]["status"] == "pending_confirmation"
    assert restored["existing_cns_facilities"]["count"] == 0


def test_invalid_cns_input_is_rejected():
    with pytest.raises(ValueError):
        CNSInputAdapter().load_candidates({"items": [{"site_id": "bad", "coordinate": [999, 30]}]})


def test_cns_inputs_invalidate_only_downstream_results(tmp_path):
    workflow = service(tmp_path)
    workflow.state["result_statuses"].update({
        "workspace": "passed", "routes": "passed", "coverage": "passed",
        "technical_risk": "passed", "report": "passed",
    })
    workflow.state["coverage"] = {"status": "passed"}
    result = workflow.import_candidate_sites({"items": [{
        "site_id": "S1", "coordinate": [122, 30], "available_subsystems": ["C"],
    }]})
    assert result["result_statuses"]["workspace"] == "passed"
    assert result["result_statuses"]["routes"] == "passed"
    assert result["result_statuses"]["coverage"] == "stale"
    assert result["result_statuses"]["technical_risk"] == "stale"
