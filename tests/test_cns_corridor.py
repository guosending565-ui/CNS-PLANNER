from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, cell_area_m2, nearest_route_position,
)
from cns_planner.algorithms.coverage.geometric_3d import (
    GeometricCoverage3DV1, build_geometric_providers, evaluate_geometry_point, route_profile_height,
)
from cns_planner.algorithms.service_capability.v1 import (
    CNSServiceCapabilityV1, build_provider_devices, evaluate_capability_point,
)
from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy


DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE = {"route_id": "R1", "status": "passed", "path": [[-0.001, 0.0], [0.001, 0.0]]}
GRID = {"status": "passed", "level": 1, "cells": [
    {"grid_id": "EDGE", "bbox": [-0.0005, 0.0, 0.0005, 0.001]},
    {"grid_id": "FAR", "bbox": [0.01, 0.01, 0.011, 0.011]},
]}
SPATIAL = {
    "altitude_layers": [{
        "altitude_layer_id": "L1", "name": "L1", "lower_altitude_m": 50.0,
        "upper_altitude_m": 150.0, "vertical_reference": "egm2008_orthometric",
        "source": "test", "confirmed": True, "status": "confirmed",
    }],
    "route_altitude_profiles": {"R1": {
        "route_id": "R1", "mode": "constant", "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": 100.0, "waypoints": [], "source": "test",
        "confirmed": True, "status": "confirmed", "geoid_undulation_m": None,
    }},
    "site_vertical_profiles": {},
}


def requirement_set(code="C"):
    values = {
        "communication": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }
    if code == "C":
        values["communication"] = {
            "required": True, "status": "passed",
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
        }
    elif code == "N":
        values["navigation"] = {
            "required": True, "status": "passed", "type": {"technology": "gnss"},
            "performance": {"max_horizontal_error_m": 5.0, "integrity_required": "required", "min_redundancy": 1},
        }
    return {"status": "passed", "project_default": values, "route_overrides": {}}


def aircraft(code="C"):
    result = {"aircraft_id": "A1", "communication": {}, "navigation": {}, "surveillance": {}}
    if code == "C":
        result["communication"] = {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
        }
    if code == "N":
        result["navigation"] = {
            "confirmed": True, "status": "confirmed", "capabilities": ["pnt"],
            "type": {"technology": "gnss"},
            "performance": {"max_horizontal_error_m": 2.0, "integrity_required": "required", "min_redundancy": 1},
        }
    return result


def device(radius=40.0, confirmed=True):
    return normalize_device({
        "device_id": "D1", "name": "D1", "subsystem": "C", "role": "existing",
        "radius_m": radius, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {"model": "sphere", "slant_range_m": radius, "source": "test", "confirmed": True},
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio", "parameters": {"performance": {"max_latency_s": 0.2}},
            "source": "test", "confirmed": confirmed,
        },
    })


def facilities():
    return {"status": "passed", "items": [{
        "facility_id": "F1", "coordinate": [0.0, 0.0], "status": "active",
        "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
        "devices": [{"device_id": "D1", "subsystem": "C", "status": "active"}],
    }]}


def policy(width=0.0):
    return normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": width,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})


def evaluate(*, spatial=None, terrain=None, catalog=None, requirements=None, aircraft_profile=None):
    return CNSServiceCorridorV1().evaluate(
        [ROUTE], spatial or SPATIAL, GRID,
        terrain if terrain is not None else {"terrain": {"status": "passed", "cells": {}}},
        requirements or requirement_set(), aircraft_profile or aircraft(), facilities(),
        catalog or {"status": "passed", "items": [device()]}, policy(),
    )


def test_centerline_can_meet_while_conservatively_included_edge_voxel_is_deficit():
    providers = build_geometric_providers(facilities(), {"items": [device()]})["C"]
    centerline = evaluate_geometry_point({
        "longitude": 0.0, "latitude": 0.0, "altitude_egm2008_m": 100.0,
        "vertical_status": "passed",
    }, providers)
    assert centerline["covered"] is True
    result = evaluate()
    route = result["routes"][0]
    assert route["horizontal_cell_count"] == 1
    voxel = route["voxels"][0]
    c = next(item for item in voxel["subsystems"] if item["subsystem"] == "C")
    assert c["planning_status"] == "confirmed_deficit"
    assert voxel["nearest_route_distance_m"] <= voxel["cell_half_diagonal_m"]
    assert route["horizontal_discretization"] == "conservative_grid_cell_inclusion_not_exact_buffer"


def test_vertical_overlap_waypoint_linear_and_volume_proxy_conservation():
    spatial = deepcopy(SPATIAL)
    total = nearest_route_position(ROUTE["path"], [0.001, 0])["route_offset_m"]
    spatial["route_altitude_profiles"]["R1"].update({
        "mode": "waypoint_linear", "constant_altitude_m": None,
        "waypoints": [{"distance_along_route_m": 0, "altitude_m": 80}, {"distance_along_route_m": total, "altitude_m": 120}],
    })
    result = evaluate(spatial=spatial)
    voxel = result["routes"][0]["voxels"][0]
    expected = route_profile_height(spatial["route_altitude_profiles"]["R1"], voxel["nearest_route_offset_m"], total)
    assert voxel["probe"]["altitude_egm2008_m"] == pytest.approx(expected)
    assert voxel["vertical_overlap_thickness_m"] == pytest.approx(20)
    assert voxel["discretized_volume_proxy_m3"] == pytest.approx(cell_area_m2(GRID["cells"][0]["bbox"]) * 20)
    summary = result["routes"][0]["subsystems"][0]
    assert summary["required_volume_proxy_m3"] == pytest.approx(
        summary["satisfied_volume_proxy_m3"] + summary["confirmed_deficit_volume_proxy_m3"] + summary["unknown_volume_proxy_m3"]
    )


@pytest.mark.parametrize("reference,terrain", [
    ("agl", {"terrain": {"cells": {"EDGE": {"status": "missing_data", "surface_elevation_mean_m": None}}}}),
    ("wgs84_ellipsoidal", {"terrain": {"cells": {}}}),
])
def test_unresolved_vertical_evidence_is_unknown_not_zero(reference, terrain):
    spatial = deepcopy(SPATIAL)
    spatial["route_altitude_profiles"]["R1"]["vertical_reference"] = reference
    spatial["altitude_layers"][0]["vertical_reference"] = reference
    result = evaluate(spatial=spatial, terrain=terrain)
    voxel = result["routes"][0]["voxels"][0]
    assert voxel["vertical_status"] in ("missing_data", "unresolved")
    assert voxel["discretized_volume_proxy_m3"] is None
    assert next(item for item in voxel["subsystems"] if item["subsystem"] == "C")["planning_status"] == "unknown"


def test_unconfirmed_service_unknown_confirmed_failure_deficit_and_gnss_non_site():
    pending = evaluate(catalog={"items": [device(200, confirmed=False)]})
    assert pending["routes"][0]["subsystems"][0]["unknown_voxel_count"] == 1
    failed = evaluate(catalog={"items": [device(40, confirmed=True)]})
    assert failed["routes"][0]["subsystems"][0]["confirmed_deficit_voxel_count"] == 1
    nav = evaluate(requirements=requirement_set("N"), aircraft_profile=aircraft("N"), catalog={"items": []})
    assert next(item for item in nav["routes"][0]["voxels"][0]["subsystems"] if item["subsystem"] == "N")["planning_status"] == "satisfied"


def test_candidate_sites_are_not_inputs_and_fingerprint_is_deterministic():
    first, second = evaluate(), evaluate()
    assert first == second
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert all("candidate" not in key for key in first)
    assert first["routes"][0]["voxels"][0]["voxel_id"] == "EDGE@L1"


def test_p7_and_p8_route_evaluators_use_the_same_public_point_contracts():
    catalog = {"items": [device(200)]}
    p7 = GeometricCoverage3DV1({"sample_spacing_m": 100}).evaluate(
        [ROUTE], SPATIAL, GRID, {"terrain": {"cells": {}}}, facilities(), catalog,
    )
    route, geometry = p7["routes"][0], p7["routes"][0]["subsystems"][0]
    base_sample = route["samples"][0]
    expected_geometry = evaluate_geometry_point(
        base_sample, build_geometric_providers(facilities(), catalog)["C"]
    )
    assert {key: geometry["samples"][0][key] for key in expected_geometry} == expected_geometry
    p8 = CNSServiceCapabilityV1().evaluate(p7, requirement_set(), aircraft(), facilities(), catalog)
    expected_capability = evaluate_capability_point(
        "C", geometry["samples"][0], requirement_set()["project_default"]["communication"],
        aircraft(), build_provider_devices(catalog, facilities()),
    )
    assert p8["routes"][0]["subsystems"][0]["samples"][0] == expected_capability


def test_corridor_spec_requires_confirmed_nonnegative_explicit_dimensions():
    pending = normalize_cns_corridor_policy({"routes": {"R1": {"route_id": "R1", "confirmed": True}}})
    assert pending["routes"]["R1"]["status"] == "pending_confirmation"
    with pytest.raises(ValueError, match="非负"):
        policy(-1)


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_api_persistence_backfill_and_directed_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    assert workflow.algorithm_registry.manifest("corridor_model", "cns_service_corridor_v1", "1.0")
    workflow.state.update({
        "operational_routes": [deepcopy(ROUTE)], "grid": deepcopy(GRID), "spatial_3d": deepcopy(SPATIAL),
        "required_cns": requirement_set(), "aircraft_profiles": {"status": "passed", "items": [aircraft()]},
        "selected_aircraft_profile_id": "A1", "device_catalog": {"status": "passed", "items": [device()]},
        "existing_cns_facilities": facilities(),
    })
    api = ApiRouter(ApiContext(workflow))
    response = api.post("/api/cns-service-corridor/evaluate", {"cns_corridor_policy": policy()}).data
    assert response["cns_corridor_assessment"]["algorithm_id"] == "cns_service_corridor_v1"
    assert api.get("/api/cns-service-corridor", {}, {}).data["input_fingerprint"]
    restored = WorkflowService(path, DEFAULTS)
    assert restored.cns_corridor_snapshot()["input_fingerprint"] == response["cns_corridor_assessment"]["input_fingerprint"]
    restored.state["cns_corridor_assessment"]["status"] = "passed"
    restored.state["result_statuses"].update({"cns_corridor_assessment": "passed", "routes": "passed", "coverage_3d": "passed", "cns_gap_v2": "passed"})
    restored.invalidation_service.workflow("corridor_policy")
    assert restored.state["result_statuses"]["cns_corridor_assessment"] == "stale"
    assert {restored.state["result_statuses"][key] for key in ("routes", "coverage_3d", "cns_gap_v2")} == {"passed"}
    legacy = deepcopy(restored.state)
    legacy.pop("cns_corridor_policy")
    legacy.pop("cns_corridor_assessment")
    legacy["result_statuses"].pop("cns_corridor_assessment")
    from cns_planner.application.project_state import normalize_project
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["cns_corridor_policy"]["status"] == "pending_confirmation"
    assert normalized["cns_corridor_assessment"]["status"] == "not_calculated"


def test_existing_input_change_stales_corridor_without_touching_centerline_products(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["cns_corridor_assessment"] = {"status": "passed"}
    workflow.state["result_statuses"].update({
        "cns_corridor_assessment": "passed", "routes": "passed", "coverage_3d": "passed",
        "cns_service_capability": "passed", "service_timeline": "passed", "cns_gap_v2": "passed",
    })
    workflow.invalidation_service.workflow("corridor_policy")
    assert workflow.state["result_statuses"]["cns_corridor_assessment"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage_3d"] == "passed"
