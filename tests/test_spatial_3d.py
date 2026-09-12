from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.geometric_3d import GeometricCoverage3DV1
from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.api.router import ApiRouter
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.spatial_3d import (
    normalize_route_altitude_profile, normalize_vertical_profile,
    resolve_egm2008_height, voxel_ref,
)


DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE = {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.01, 0.0]]}
GRID = {"status": "passed", "level": 1, "cells": [{"grid_id": "G1", "bbox": [-0.1, -0.1, 0.1, 0.1]}]}
TERRAIN = {"terrain": {"status": "passed", "cells": {"G1": {"status": "passed", "surface_elevation_mean_m": 50.0}}}}


def profile(reference="agl", altitude=100.0):
    return {"route_id": "R1", "mode": "constant", "vertical_reference": reference, "constant_altitude_m": altitude, "source": "test", "confirmed": True}


def catalog(model="sphere", radius=2000.0, technology="unknown"):
    device = normalize_device({
        "device_id": "C1", "name": "C1", "subsystem": "C", "role": "existing",
        "radius_m": radius, "mtbf_h": 1000, "type": {"technology": technology},
        "coverage_geometry": {"model": model, "slant_range_m": radius, "source": "test", "confirmed": True},
    })
    return {"status": "passed", "items": [device]}


def facilities(origin=50.0):
    return {"status": "passed", "items": [{
        "facility_id": "F1", "coordinate": [0.0, 0.0], "status": "active",
        "vertical_profile": {"service_origin_egm2008_m": origin, "confirmed": True},
        "devices": [{"device_id": "C1", "subsystem": "C", "status": "active"}],
    }]}


def evaluate(*, route_profile=None, geometry="sphere", radius=2000.0, terrain=TERRAIN, origin=50.0, spacing=250.0):
    model = GeometricCoverage3DV1({"sample_spacing_m": spacing})
    return model.evaluate(
        [ROUTE], {"route_altitude_profiles": {"R1": route_profile or profile()}},
        GRID, terrain, facilities(origin), catalog(geometry, radius),
    )


def test_vertical_reference_resolution_and_nodata_rules():
    assert resolve_egm2008_height(100, "agl", surface_elevation_m=50)["altitude_egm2008_m"] == 150
    assert resolve_egm2008_height(100, "agl")["status"] == "missing_data"
    assert resolve_egm2008_height(100, "wgs84_ellipsoidal")["status"] == "unresolved"
    resolved = resolve_egm2008_height(100, "wgs84_ellipsoidal", geoid_undulation_m=20)
    assert resolved["altitude_egm2008_m"] == 80
    assert normalize_vertical_profile(None, legacy_elevation_m=25)["service_origin_egm2008_m"] is None


def test_voxel_ref_is_lazy_stable_and_not_a_workspace_collection():
    assert voxel_ref("GRID-1", "ALT-1") == {"voxel_id": "GRID-1@ALT-1", "grid_id": "GRID-1", "altitude_layer_id": "ALT-1"}


def test_route_3d_samples_include_surface_agl_and_egm2008():
    result = evaluate()
    samples = result["routes"][0]["samples"]
    assert samples[0]["surface_elevation_m"] == 50
    assert samples[0]["altitude_agl_m"] == 100
    assert samples[0]["altitude_egm2008_m"] == 150
    assert [item["distance_along_route_m"] for item in samples] == sorted(item["distance_along_route_m"] for item in samples)


def test_waypoint_linear_profile_contract_and_sampling():
    total = 1111.9508
    linear = normalize_route_altitude_profile({
        "route_id": "R1", "mode": "waypoint_linear", "vertical_reference": "egm2008_orthometric",
        "waypoints": [{"distance_along_route_m": 0, "altitude_m": 100}, {"distance_along_route_m": total, "altitude_m": 200}],
        "source": "test", "confirmed": True,
    })
    samples = evaluate(route_profile=linear, spacing=total / 2)["routes"][0]["samples"]
    assert samples[1]["altitude_egm2008_m"] == pytest.approx(150, abs=0.1)


def test_route_3d_rejects_missing_dem_and_unresolved_ellipsoid():
    nodata = {"terrain": {"cells": {"G1": {"status": "missing_data", "surface_elevation_mean_m": None}}}}
    assert evaluate(terrain=nodata)["routes"][0]["status"] == "missing_data"
    assert evaluate(route_profile=profile("wgs84_ellipsoidal"))["routes"][0]["status"] == "missing_data"


def test_sphere_and_hemisphere_boundaries_use_slant_range():
    sphere = evaluate(route_profile=profile("egm2008_orthometric", 40), geometry="sphere", radius=100, origin=50, spacing=100)
    hemisphere = evaluate(route_profile=profile("egm2008_orthometric", 40), geometry="hemisphere", radius=100, origin=50, spacing=100)
    assert sphere["routes"][0]["subsystems"][0]["samples"][0]["covered"] is True
    assert hemisphere["routes"][0]["subsystems"][0]["samples"][0]["covered"] is False
    boundary = evaluate(route_profile=profile("egm2008_orthometric", 150), geometry="sphere", radius=100, origin=50, spacing=100)
    assert boundary["routes"][0]["subsystems"][0]["samples"][0]["covered"] is True


def test_legacy_radius_is_unconfirmed_geometric_assumption_and_gnss_is_not_auto_sphere():
    legacy = normalize_device({"device_id": "C1", "name": "C", "subsystem": "C", "role": "existing", "radius_m": 1000, "mtbf_h": 1000})
    assert legacy["coverage_geometry"] == {
        "model": "hemisphere", "slant_range_m": 1000.0, "model_scope": "geometric_only",
        "source": "legacy_engineering_assumption", "confirmed": False, "status": "pending_confirmation",
    }
    gnss = normalize_device({"device_id": "N1", "name": "N", "subsystem": "N", "role": "existing", "radius_m": 1000, "mtbf_h": 1000, "type": {"technology": "gnss"}})
    assert gnss["coverage_geometry"]["model"] == "none"


def test_coverage_statistics_are_length_weighted_and_explicitly_geometric_only():
    result = evaluate(radius=550, spacing=200)
    subsystem = result["routes"][0]["subsystems"][0]
    assert subsystem["covered_length_m"] + subsystem["uncovered_length_m"] == pytest.approx(subsystem["route_length_m"])
    assert subsystem["covered_fraction"] == pytest.approx(subsystem["covered_length_m"] / subsystem["route_length_m"])
    point_fraction = sum(item["covered"] is True for item in subsystem["samples"]) / len(subsystem["samples"])
    assert subsystem["covered_fraction"] != pytest.approx(point_fraction)
    assert result["model_scope"] == "geometric_only"
    assert set(result["not_evaluated"].values()) == {"not_evaluated"}


def test_registry_project_backfill_roundtrip_and_directed_invalidation(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    manifest = build_default_algorithm_registry(workflow.defaults).manifest("coverage_model", "geometric_coverage_3d_v1", "1.0")
    assert manifest.algorithm_id == "geometric_coverage_3d_v1"
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    workflow.state["result_statuses"]["routes"] = "passed"
    workflow.set_route_altitude_profile(profile())
    workflow.state["coverage_3d"] = {"status": "passed"}
    workflow.state["result_statuses"].update({"coverage_3d": "passed", "routes": "passed", "coverage": "passed", "cns_gap": "passed"})
    workflow.set_altitude_layers({"altitude_layers": [{"altitude_layer_id": "A1", "lower_altitude_m": 0, "upper_altitude_m": 120, "vertical_reference": "agl", "source": "test", "confirmed": True}]})
    assert workflow.state["coverage_3d"]["status"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage"] == "passed"
    assert workflow.state["result_statuses"]["cns_gap"] == "passed"
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["spatial_3d"]["route_altitude_profiles"]["R1"]["constant_altitude_m"] == 100
    assert "voxels" not in restored.state["spatial_3d"]


def test_terrain_and_existing_inputs_stale_3d_without_reverse_effects(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["coverage_3d"] = {"status": "passed"}
    workflow.state["result_statuses"].update({"coverage_3d": "passed", "routes": "passed", "coverage": "passed", "cns_gap": "passed"})
    workflow.invalidate_grid_attributes({"terrain"})
    assert workflow.state["result_statuses"]["coverage_3d"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage"] == "passed"
    assert workflow.state["result_statuses"]["cns_gap"] == "passed"


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_spatial_3d_api_is_additive_and_persists_configuration(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    router = ApiRouter(ApiContext(workflow))
    router.post("/api/spatial-3d/route-profile", profile())
    response = router.get("/api/spatial-3d", {}, {}).data
    assert response["route_altitude_profiles"]["R1"]["vertical_reference"] == "agl"
    assert router.get("/api/coverage-3d", {}, {}).data["model_scope"] == "geometric_only"


def test_workflow_evaluates_saves_and_restores_coverage_3d(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    workflow.state["result_statuses"]["routes"] = "passed"
    workflow.state["grid"] = deepcopy(GRID)
    workflow.state["grid_attributes"].update(deepcopy(TERRAIN))
    workflow.state["device_catalog"] = catalog()
    workflow.state["existing_cns_facilities"] = facilities()
    workflow.set_route_altitude_profile(profile())
    result = workflow.evaluate_coverage_3d({"parameters": {"sample_spacing_m": 200}})["coverage_3d"]
    assert result["algorithm_id"] == "geometric_coverage_3d_v1"
    assert result["route_count"] == 1
    restored = WorkflowService(path, DEFAULTS).coverage_3d_snapshot()
    assert restored["input_fingerprint"] == result["input_fingerprint"]
