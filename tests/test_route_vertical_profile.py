from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.route_vertical_profile import RouteVerticalProfileV1
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.api.router import ApiRouter
from cns_planner.gis.route_vertical_profile_adapter import FabdemRouteSampler


DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE = {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.01, 0.0]]}
PROFILE = {
    "route_id": "R1", "mode": "constant", "vertical_reference": "agl",
    "constant_altitude_m": 100.0, "source": "confirmed-test", "confirmed": True,
    "status": "confirmed", "waypoints": [], "geoid_undulation_m": None,
}


class Sampler:
    def __init__(self, ground=50.0, signature="a"):
        self.ground, self.signature = ground, signature

    def sample_wgs84(self, coordinate):
        if self.ground is None:
            return {"status": "missing_data", "ground_egm2008_m": None, "reason": "dtm_nodata"}
        return {"status": "passed", "ground_egm2008_m": self.ground, "reason": "test"}

    def describe(self):
        return {"dataset": "FABDEM", "vertical_reference": "egm2008_orthometric", "mtime_ns": self.signature}


def clearance(status="passed", breach=False):
    interval = {
        "route_id": "R1", "building_id": "B1", "start_distance_m": 100.0,
        "end_distance_m": 200.0, "roof_elevation_m": 130.0,
        "ground_elevation_m": 50.0, "status": "breach" if breach else "safe",
    }
    route = {
        "route_id": "R1", "status": "failed" if breach else "passed",
        "buildings": [{
            "building_id": "B1", "height_m": 80.0, "ground_elevation_m": 50.0,
            "roof_elevation_m": 130.0, "vertical_status": "resolved", "intervals": [interval],
        }],
    }
    return {
        "status": status, "algorithm_id": "building_clearance_v1",
        "input_fingerprint": "clearance-fingerprint", "routes": [route],
        "breach_segments": [interval] if breach else [],
        "critical_buildings": [{"route_id": "R1", "building_id": "B1"}],
        "policy": {"vertical_clearance_m": 30.0},
    }


def evaluate(profile=PROFILE, sampler=None, assessment=None, parameters=None):
    return RouteVerticalProfileV1(parameters).evaluate(
        [deepcopy(ROUTE)], {"route_altitude_profiles": {"R1": deepcopy(profile)}},
        assessment or clearance(), sampler or Sampler(),
    )


def test_fabdem_samples_are_egm2008_and_agl_with_endpoints_and_cap():
    result = evaluate(parameters={"target_spacing_m": 1, "max_samples": 8})
    profile = result["profiles"][0]
    assert result["status"] == profile["status"] == "passed"
    assert len(profile["samples"]) == 8
    assert profile["samples"][0]["distance_m"] == 0
    assert profile["samples"][-1]["distance_m"] == pytest.approx(profile["route_length_m"])
    assert profile["samples"][0]["ground_egm2008_m"] == 50
    assert profile["samples"][0]["flight_egm2008_m"] == 150
    assert profile["samples"][0]["flight_agl_m"] == 100
    assert profile["samples"][0]["ground_clearance_m"] == 100
    assert profile["sampling"]["safety_decision"] == "not_evaluated_from_samples"


def test_missing_dtm_and_unconfirmed_vertical_reference_remain_unknown():
    missing = evaluate(sampler=Sampler(None))["profiles"][0]
    assert missing["status"] == "unknown"
    assert all(item["ground_egm2008_m"] is None for item in missing["samples"])
    pending = {**PROFILE, "vertical_reference": "unknown", "status": "pending_confirmation"}
    unresolved = evaluate(profile=pending)["profiles"][0]
    assert unresolved["status"] == "unknown"
    assert unresolved["samples"] == []
    invalid_height = {**PROFILE, "constant_altitude_m": "not-a-height"}
    invalid = evaluate(profile=invalid_height)["profiles"][0]
    assert invalid["status"] == "unknown"
    assert invalid["samples"][0]["reason"] == "route_altitude_unparseable"


def test_building_breach_is_reused_without_sample_rejudgement():
    result = evaluate(assessment=clearance("failed", breach=True))
    profile = result["profiles"][0]
    assert result["status"] == profile["status"] == "breach"
    assert profile["breach_intervals"][0]["building_id"] == "B1"
    assert profile["samples"][0]["ground_clearance_m"] == 100
    assert "exact building breach comes from BuildingClearanceV1" in profile["semantics"]


def test_valid_geometry_is_distinct_from_missing_clearance_evidence():
    result = evaluate(assessment={"status": "not_calculated", "routes": []})
    profile = result["profiles"][0]
    assert profile["status"] == "unknown"
    assert profile["profile_geometry_status"] == "passed"
    assert profile["clearance_evidence_status"] == "unknown"
    assert result["profile_geometry_status"] == "passed"
    assert result["clearance_evidence_status"] == "unknown"


def test_dtm_and_building_inputs_enter_fingerprint():
    first = evaluate(sampler=Sampler(signature="a"))
    second = evaluate(sampler=Sampler(signature="b"))
    changed_building = clearance()
    changed_building["input_fingerprint"] = "different"
    third = evaluate(assessment=changed_building)
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert first["input_fingerprint"] != third["input_fingerprint"]


def test_route_profile_stale_chain_and_old_project_backfill(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    workflow.state["route_vertical_profiles"] = evaluate()
    workflow.state["result_statuses"]["route_vertical_profiles"] = "passed"
    workflow.set_route_altitude_profile(PROFILE)
    assert workflow.state["route_vertical_profiles"]["status"] == "stale"

    legacy = blank_project(workflow.defaults)
    legacy.pop("route_vertical_profiles")
    legacy["result_statuses"].pop("route_vertical_profiles")
    restored = normalize_project(legacy, workflow.grid_service)
    assert restored["route_vertical_profiles"]["status"] == "not_calculated"
    assert restored["result_statuses"]["route_vertical_profiles"] == "not_calculated"


class FakeBand:
    def GetNoDataValue(self): return -9999
    def GetScale(self): return None
    def GetOffset(self): return None
    def ReadAsArray(self, column, row, width, height): return [[42.5]]


class FakeDataset:
    RasterXSize = RasterYSize = 2
    RasterCount = 1
    def GetRasterBand(self, index): return FakeBand()
    def GetGeoTransform(self): return (0, 1, 0, 2, 0, -1)
    def GetProjection(self): return "EPSG:4326"
    def GetMetadata(self): return {"vertical_reference": "EGM2008_orthometric"}


class FakeGdal:
    GA_ReadOnly = 0
    @staticmethod
    def Open(path, mode): return FakeDataset()
    @staticmethod
    def InvGeoTransform(transform): return (0, 1, 0, 2, 0, -1)


class FakeReference:
    def ImportFromWkt(self, value): pass
    def ImportFromEPSG(self, value): pass
    def SetAxisMappingStrategy(self, value): pass


class FakeTransformation:
    def __init__(self, source, target): pass
    def TransformPoint(self, lon, lat): return lon, lat, 0


class FakeOsr:
    OAMS_TRADITIONAL_GIS_ORDER = 0
    SpatialReference = FakeReference
    CoordinateTransformation = FakeTransformation


def test_read_only_fabdem_adapter_samples_source_pixel(tmp_path):
    path = tmp_path / "fabdem.tif"
    path.write_bytes(b"fixture")
    sampler = FabdemRouteSampler(path, FakeGdal, FakeOsr)
    assert sampler.sample_wgs84([0.5, 1.5]) == {
        "status": "passed", "ground_egm2008_m": 42.5,
        "reason": "FABDEM nearest source pixel",
    }
    assert sampler.sample_wgs84([9, 9])["reason"] == "outside_dtm_extent"
    assert sampler.describe()["vertical_reference"] == "EGM2008_orthometric"


class DirectQgis:
    @staticmethod
    def call(callback): return callback()


class ApiContext:
    def __init__(self, workflow):
        self.workflow, self.qgis, self.data = workflow, DirectQgis(), object()

    def evaluate_route_vertical_profiles(self, payload):
        return self.workflow.evaluate_route_vertical_profiles(Sampler(), payload)


def test_route_vertical_profile_api_persists_collection(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    workflow.state["spatial_3d"]["route_altitude_profiles"]["R1"] = deepcopy(PROFILE)
    workflow.state["building_clearance_assessment"] = clearance()
    router = ApiRouter(ApiContext(workflow))
    response = router.post("/api/route-vertical-profiles/evaluate", {"route_id": "R1"}).data
    assert response["route_vertical_profiles"]["status"] == "passed"
    assert router.get("/api/route-vertical-profiles", {}, {}).data["sample_count"] > 1
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["route_vertical_profiles"]["input_fingerprint"]
