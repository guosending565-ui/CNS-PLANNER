from copy import deepcopy
import json
import math
from pathlib import Path
import random

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, cell_area_m2, nearest_route_position,
)
from cns_planner.domain.geodesy import distance_m
from cns_planner.algorithms.coverage.geometric_3d import (
    GeometricCoverage3DV1, GeometricProviderIndex, build_geometric_providers,
    evaluate_geometry_point, route_profile_height,
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


def test_optimized_corridor_matches_reference_full_business_result(monkeypatch):
    """All acceleration gates preserve the complete legacy list-scan result."""
    import cns_planner.algorithms.corridor.v1 as corridor

    grid = deepcopy(GRID)
    grid["cells"].extend([
        {"grid_id": "NEAR", "bbox": [0.0005, -0.0005, 0.0015, 0.0005]},
        {"grid_id": "VERY-FAR", "bbox": [1.0, 1.0, 1.001, 1.001]},
    ])
    spatial = deepcopy(SPATIAL)
    spatial["altitude_layers"].extend([
        {
            "altitude_layer_id": "L2", "name": "L2", "lower_altitude_m": 90.0,
            "upper_altitude_m": 130.0, "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        },
        {
            "altitude_layer_id": "L-FAR", "name": "L-FAR", "lower_altitude_m": 500.0,
            "upper_altitude_m": 600.0, "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        },
    ])
    catalog_items = [device(200)]
    facility_items = facilities()["items"]
    for index in range(1, 40):
        item = deepcopy(device(200))
        item["device_id"] = f"D{index + 1}"
        catalog_items.append(item)
        facility_items.append({
            "facility_id": f"F{index + 1}",
            "coordinate": [index / 10000, (index % 3) / 10000],
            "status": "active",
            "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
            "devices": [{"device_id": item["device_id"], "subsystem": "C", "status": "active"}],
        })
    inputs = (
        [deepcopy(ROUTE)], spatial, grid, {"terrain": {"status": "passed", "cells": {}}},
        requirement_set(), aircraft(), {"status": "passed", "items": facility_items},
        {"status": "passed", "items": catalog_items}, policy(100),
    )
    optimized = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))
    with monkeypatch.context() as reference:
        reference.setattr(corridor, "index_geometric_providers", lambda providers: providers)
        reference.setattr(corridor, "_route_bbox_candidate", lambda route, point, limit: True)
        reference.setattr(corridor, "_candidate_layers", lambda profile, spec, layers: layers)
        legacy_list_scan = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))
    assert optimized == legacy_list_scan


def test_geometric_provider_index_is_field_for_field_equivalent_and_ordered():
    providers = []
    for index in range(40):
        providers.append({
            "facility_id": f"F{index}", "device_id": f"D{index}",
            "coordinate": [(index - 20) / 10000, (index % 4) / 10000],
            "service_origin_egm2008_m": 90.0 + index % 5,
            "coverage_geometry": {
                "model": "sphere" if index % 2 else "hemisphere",
                "slant_range_m": 50.0 + index * 10,
            },
        })
    sample = {
        "longitude": 0.0, "latitude": 0.0, "altitude_egm2008_m": 100.0,
        "vertical_status": "passed",
    }
    expected = evaluate_geometry_point(sample, providers)
    assert evaluate_geometry_point(sample, GeometricProviderIndex(providers)) == expected
    assert [item["device_id"] for item in expected["providers"]] == [
        item["device_id"] for item in providers
        if item["device_id"] in {match["device_id"] for match in expected["providers"]}
    ]


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
    assert workflow.algorithm_registry.manifest("corridor_model", "cns_service_corridor_v1", "1.1")
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


# --------------------------------------------------------------------------- performance
# 下面这组测试是 P14 性能优化的**语义等价性证据**：它们不测时间，只证明优化后的
# 计算路径与优化前逐字段一致，以及每个预筛/索引门控都是保守的（只会保留候选，
# 绝不丢弃真实结果）。参考实现直接来自优化前提交 82b5b09 的逐段实现。


def _reference_nearest_route_position(path, point):
    """优化前 ``nearest_route_position`` 的参考实现（逐段重算 length/投影）。"""

    cumulative, best = 0.0, None
    latitude = math.radians(float(point[1]))
    sx = 111_320.0 * max(math.cos(latitude), 1e-12)
    sy = 110_574.0
    for index, (left, right) in enumerate(zip(path, path[1:])):
        ax, ay = (float(left[0]) - point[0]) * sx, (float(left[1]) - point[1]) * sy
        bx, by = (float(right[0]) - point[0]) * sx, (float(right[1]) - point[1]) * sy
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        t = 0.0 if denominator == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denominator))
        coordinate = [float(left[0]) + (float(right[0]) - float(left[0])) * t,
                      float(left[1]) + (float(right[1]) - float(left[1])) * t]
        segment_length = distance_m(left, right)
        candidate = {
            "distance_m": distance_m(point, coordinate),
            "route_offset_m": cumulative + segment_length * t,
            "coordinate": coordinate, "segment_index": index,
        }
        if best is None or (candidate["distance_m"], candidate["route_offset_m"], index) < (
            best["distance_m"], best["route_offset_m"], best["segment_index"]
        ):
            best = candidate
        cumulative += segment_length
    return best or {
        "distance_m": 0.0, "route_offset_m": 0.0,
        "coordinate": list(point), "segment_index": 0,
    }


def _random_path(rng, origin_lon, origin_lat):
    return [
        [origin_lon + rng.uniform(-0.05, 0.05), origin_lat + rng.uniform(-0.05, 0.05)]
        for _ in range(rng.randint(2, 7))
    ]


def test_metric_route_nearest_matches_reference_segment_scan():
    """预计算 segment metric 不得改变任何最近航路点字段（含并列段索引）。"""

    rng = random.Random(20260924)
    for _ in range(300):
        origin_lon, origin_lat = rng.uniform(-179.0, 179.0), rng.uniform(-80.0, 80.0)
        path = _random_path(rng, origin_lon, origin_lat)
        if rng.random() < 0.3:  # 退化：重复点导致的零长度段
            path.insert(1, list(path[0]))
        point = [origin_lon + rng.uniform(-0.08, 0.08), origin_lat + rng.uniform(-0.08, 0.08)]
        assert nearest_route_position(path, point) == _reference_nearest_route_position(path, point)


def test_route_bounding_box_prescreen_never_discards_an_included_cell():
    """廉价 bbox 预筛只允许丢弃"真实最近距离必然超出阈值"的单元。"""

    import cns_planner.algorithms.corridor.v1 as corridor

    rng = random.Random(82)
    for _ in range(400):
        origin_lon, origin_lat = rng.uniform(-179.0, 179.0), rng.uniform(-70.0, 70.0)
        path = _random_path(rng, origin_lon, origin_lat)
        metric_route = corridor._MetricRoute(path)
        point = [origin_lon + rng.uniform(-0.09, 0.09), origin_lat + rng.uniform(-0.09, 0.09)]
        limit = rng.uniform(50.0, 4000.0)
        distance = corridor._MetricRoute(path).nearest(point)["distance_m"]
        if not corridor._route_bbox_candidate(metric_route, point, limit):
            assert distance > limit, (path, point, limit, distance)


def _reference_layer_bounds(prepared, layer):
    """不缓存的层上下界解析，用于对照 ``_resolved_layer_bounds`` 缓存。"""

    from cns_planner.domain.spatial_3d import resolve_egm2008_height

    surface = prepared["surface"]
    return (
        resolve_egm2008_height(
            layer.get("lower_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
        ),
        resolve_egm2008_height(
            layer.get("upper_altitude_m"), layer.get("vertical_reference", "unknown"),
            surface_elevation_m=surface, geoid_undulation_m=layer.get("geoid_undulation_m"),
        ),
    )


def _random_vertical_reference(rng):
    return rng.choice(["egm2008_orthometric", "agl", "wgs84_ellipsoidal", "unknown"])


def _random_fixture(rng, index):
    """随机但确定性的 P14 输入；覆盖三类垂直基准与缺失/未确认分支。"""

    from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy

    origin_lon, origin_lat = rng.uniform(121.9, 122.1), rng.uniform(29.9, 30.1)
    path = _random_path(rng, origin_lon, origin_lat)
    route = {"route_id": f"R{index}", "status": rng.choice(["passed", "passed", "stale"]),
             "path": path}

    cells = []
    for cell_index in range(rng.randint(3, 14)):
        left = origin_lon + rng.uniform(-0.03, 0.03)
        bottom = origin_lat + rng.uniform(-0.03, 0.03)
        cells.append({
            "grid_id": f"C{cell_index}",
            "bbox": [left, bottom, left + rng.uniform(0.0004, 0.004), bottom + rng.uniform(0.0004, 0.004)],
        })
    terrain = {}
    for cell in cells:
        terrain[cell["grid_id"]] = (
            {"status": "passed", "surface_elevation_mean_m": rng.uniform(-5.0, 120.0)}
            if rng.random() < 0.7 else {"status": "missing_data", "surface_elevation_mean_m": None}
        )

    layers = []
    for layer_index in range(rng.randint(1, 4)):
        lower = rng.uniform(20.0, 180.0)
        layers.append({
            "altitude_layer_id": f"L{layer_index}", "name": f"L{layer_index}",
            "lower_altitude_m": lower, "upper_altitude_m": lower + rng.uniform(10.0, 80.0),
            "vertical_reference": _random_vertical_reference(rng),
            "geoid_undulation_m": rng.choice([None, -28.5, 31.0]),
            "source": "fuzz", "confirmed": rng.random() < 0.85,
            "status": rng.choice(["confirmed", "confirmed", "pending_confirmation"]),
        })
    profile = {
        "route_id": route["route_id"],
        "mode": rng.choice(["constant", "waypoint_linear"]),
        "vertical_reference": _random_vertical_reference(rng),
        "constant_altitude_m": rng.uniform(40.0, 200.0),
        "waypoints": [], "geoid_undulation_m": rng.choice([None, -28.5, 31.0]),
        "source": "fuzz", "confirmed": True, "status": "confirmed",
    }
    if profile["mode"] == "waypoint_linear":
        profile["waypoints"] = [
            {"distance_along_route_m": offset, "altitude_m": rng.uniform(40.0, 200.0)}
            for offset in (0.0, rng.uniform(500.0, 4000.0))
        ]
    spatial = {
        "status": "passed", "altitude_layers": layers,
        "route_altitude_profiles": {route["route_id"]: profile},
        "route_operating_layers": [], "site_vertical_profiles": {},
    }

    catalog_items, facility_items = [], []
    for code in ("C", "N", "S"):
        for device_index in range(rng.randint(0, 6)):
            item = deepcopy(device(rng.choice([40.0, 200.0, 900.0]), confirmed=rng.random() < 0.8))
            item["device_id"] = f"{code}{device_index}"
            item["subsystem"] = code
            item["coverage_geometry"]["model"] = rng.choice(["sphere", "hemisphere"])
            item["coverage_geometry"]["slant_range_m"] = item["radius_m"]
            catalog_items.append(item)
            facility_items.append({
                "facility_id": f"{code}F{device_index}",
                "coordinate": [origin_lon + rng.uniform(-0.02, 0.02), origin_lat + rng.uniform(-0.02, 0.02)],
                "status": rng.choice(["active", "active", "inactive"]),
                "vertical_profile": {"service_origin_egm2008_m": rng.uniform(10.0, 160.0), "confirmed": True},
                "devices": [{"device_id": item["device_id"], "subsystem": code, "status": "active"}],
            })

    requirements = requirement_set(rng.choice(["C", "N", "S"]))
    for name in ("communication", "navigation", "surveillance"):
        entry = requirements["project_default"][name]
        if rng.random() < 0.25:
            entry["required"] = rng.choice([True, False])
        if rng.random() < 0.2:
            entry["status"] = "pending_confirmation"
    pol = normalize_cns_corridor_policy({"routes": {route["route_id"]: {
        "route_id": route["route_id"], "horizontal_half_width_m": rng.choice([0.0, 100.0, 600.0]),
        "vertical_lower_margin_m": rng.choice([0.0, 20.0, 60.0]),
        "vertical_upper_margin_m": rng.choice([0.0, 20.0, 60.0]),
        "source": "fuzz", "confirmed": True,
    }}})
    return (
        [route], spatial, {"status": "passed", "level": 7, "cells": cells},
        {"terrain": {"status": "passed", "cells": terrain}},
        requirements, aircraft(rng.choice(["C", "N"])),
        {"status": "passed", "items": facility_items},
        {"status": "passed", "items": catalog_items}, pol,
    )


def test_optimized_corridor_matches_reference_over_random_fixtures(monkeypatch):
    """随机夹具下，关闭全部加速门控的参考实现必须给出逐字段一致的结果。"""

    import cns_planner.algorithms.corridor.v1 as corridor

    rng = random.Random(4096)
    compared = 0
    for index in range(24):
        inputs = _random_fixture(rng, index)
        optimized = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))
        with monkeypatch.context() as reference:
            reference.setattr(corridor, "index_geometric_providers", lambda providers: providers)
            reference.setattr(corridor, "_route_bbox_candidate", lambda route, point, limit: True)
            reference.setattr(corridor, "_candidate_layers", lambda profile, spec, layers: layers)
            reference.setattr(corridor, "_resolved_layer_bounds", _reference_layer_bounds)
            reference.setattr(corridor, "prepare_capability_context",
                              lambda code, required, aircraft: None)
            legacy = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))
        assert optimized == legacy, f"fixture {index} 结果不一致"
        compared += 1
    assert compared == 24


def test_evidence_clone_is_deepcopy_equivalent_and_keeps_aliasing():
    """证据快速深拷贝必须与 ``copy.deepcopy`` 等价：值相同、别名结构相同。"""

    from copy import deepcopy as reference_clone

    from cns_planner.algorithms.corridor.v1 import _clone_value

    rng = random.Random(90210)

    def build(depth=0):
        roll = rng.random()
        if depth >= 3 or roll < 0.35:
            return rng.choice([None, True, False, 0, -3, 2.5, "文本", "", b"bytes"])
        if roll < 0.5:
            return tuple(build(depth + 1) for _ in range(rng.randint(0, 3)))
        if roll < 0.6:
            return {f"s{index}" for index in range(rng.randint(0, 3))}
        if roll < 0.8:
            return [build(depth + 1) for _ in range(rng.randint(0, 4))]
        return {f"k{index}": build(depth + 1) for index in range(rng.randint(0, 4))}

    for _ in range(200):
        shared = [build(), {"nested": build()}]
        value = {"a": shared, "b": shared, "c": build(), ("tuple-key", 1): shared[1]}
        clone = _clone_value(value)
        reference = reference_clone(value)
        assert clone == reference
        assert clone["a"] is clone["b"], "重复引用必须仍然共享同一个副本"
        assert clone["a"] is not value["a"] and clone["a"] is not shared
        assert clone[("tuple-key", 1)] is clone["a"][1]
        assert type(clone["c"]) is type(reference["c"])


def _structure(value):
    """把 JSON 结构归一成可比较形式，保留"键的原始类型"这一信息。"""

    if isinstance(value, dict):
        return ("dict", tuple(sorted((_structure(key), _structure(item))
                                    for key, item in value.items())))
    if isinstance(value, list):
        return ("list", tuple(_structure(item) for item in value))
    return (type(value).__name__, value)


def test_fresh_clone_matches_evidence_clone_for_shared_free_structures():
    """``_clone_fresh`` 用于"整棵树都是新建的"证据：必须与 ``_clone_value`` 逐字段等价。

    P14 只在 ``_cell_voxels`` 里对刚构造、无共享引用的证据字段使用它；这里用随机
    无共享引用的 JSON 结构穷举验证两者产出相等（含 tuple-key 与非字符串键）。
    该路径只对 dict/list/原子标量做快速处理，因此输入限定在 JSON 子集。
    """

    from cns_planner.algorithms.corridor.v1 import _clone_fresh, _clone_value

    rng = random.Random(4242)

    def build(depth=0):
        roll = rng.random()
        if depth >= 4 or roll < 0.4:
            return rng.choice([None, True, False, 0, -7, 1.5, "文本", ""])
        if roll < 0.6:
            return [build(depth + 1) for _ in range(rng.randint(0, 4))]
        if roll < 0.7:
            return {(1, 2): build(depth + 1), 7: build(depth + 1)}
        return {f"k{index}": build(depth + 1) for index in range(rng.randint(0, 4))}

    for _ in range(200):
        value = build()
        fresh = _clone_fresh(value)
        memo_clone = _clone_value(value)
        assert _structure(fresh) == _structure(memo_clone)
        if isinstance(value, (dict, list)):
            assert fresh is not value


def _progress_fixture():
    """确定性中型夹具：走廊内含足够多单元，使长循环检查点被多次触发。"""

    route = {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.0, 0.04]]}
    cells = []
    for row in range(12):
        for column in range(40):
            left = -0.006 + column * 0.0003
            bottom = row * 0.0035
            cells.append({
                "grid_id": f"G{row:02d}{column:02d}",
                "bbox": [left, bottom, left + 0.0003, bottom + 0.0035],
            })
    spatial = {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": "L1", "name": "L1",
            "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 100.0,
            "waypoints": [], "source": "test", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    items, catalog_items = [], []
    for code in ("C", "N", "S"):
        for index in range(3):
            device_id, facility_id = f"{code}D{index}", f"{code}F{index}"
            entry = device(radius=900.0)
            entry["device_id"] = device_id
            entry["subsystem"] = code
            catalog_items.append(entry)
            items.append({
                "facility_id": facility_id,
                "coordinate": [0.0005 * index, 0.012 + 0.008 * index],
                "status": "active",
                "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
                "devices": [{"device_id": device_id, "subsystem": code, "status": "active"}],
            })
    return (
        [route], spatial, {"status": "passed", "level": 7, "cells": cells},
        {"terrain": {"status": "passed", "cells": {}}},
        requirement_set("C"), aircraft("C"),
        {"status": "passed", "items": items},
        {"status": "passed", "items": catalog_items},
        policy(width=800.0),
    )


def test_optional_progress_callback_is_monotonic_and_does_not_change_result():
    """``progress_callback`` 是可选钩子：默认 None 时无行为变化，传入时单调递增。"""

    inputs = _progress_fixture()
    baseline = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))
    assert baseline["status"] not in (None, "")
    assert baseline["routes"][0]["horizontal_cell_count"] >= 16, "夹具必须触发多个检查点"

    seen = []
    hooked = CNSServiceCorridorV1().evaluate(
        *deepcopy(inputs),
        progress_callback=lambda value, message: seen.append((value, message)),
    )
    assert hooked == baseline, "进度钩子不得改变任何业务结果"
    values = [value for value, _message in seen]
    assert len(values) > 1, "长循环必须多次上报进度"
    assert values == sorted(values), "进度必须单调不减"
    assert values[0] <= 100.0 and values[-1] <= 100.0
    assert values[-1] > values[0], "进度必须真的推进"
    assert all(isinstance(message, str) for _value, message in seen)


def _build_preflight_inputs(grid_cells, providers_per_subsystem, radius_m=900.0):
    """构造 preflight 夹具：小体量网格 + 可参数化的 provider 规模。

    与生产输入同构（同一 policy/requirement/aircraft 形状），只把 grid 与 catalog
    缩小到测试可承受的规模，便于对"估算与准入"做纯函数断言。

    ``radius_m`` 是 provider 服务半径：它是**唯一**的密度杠杆——放大半径会让同一个
    探针格落入更多 provider 的外接方形，从而把评估上界推过包线阈值，而真实计算量
    仍由实际的几何覆盖决定，因此可以在毫秒级断言准入行为。
    """

    route = {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.0, 0.03]]}
    cells = []
    for row in range(10):
        for column in range(10):
            left = -0.004 + column * 0.0008
            bottom = row * 0.003
            cells.append({
                "grid_id": f"P{row:02d}{column:02d}",
                "bbox": [left, bottom, left + 0.0008, bottom + 0.003],
            })
    spatial = {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": "L1", "name": "L1",
            "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 100.0,
            "waypoints": [], "source": "test", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    items, catalog_items = [], []
    for code in ("C", "N", "S"):
        for index in range(providers_per_subsystem):
            device_id, facility_id = f"{code}D{index:05d}", f"{code}F{index:05d}"
            entry = device(radius=radius_m)
            entry["device_id"] = device_id
            entry["subsystem"] = code
            catalog_items.append(entry)
            items.append({
                "facility_id": facility_id,
                "coordinate": [-0.002 + 0.0002 * (index % 20), 0.001 * (index % 30)],
                "status": "active",
                "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
                "devices": [{"device_id": device_id, "subsystem": code, "status": "active"}],
            })
    return {
        "routes": [route], "spatial_3d": spatial,
        "grid": {"status": "passed", "level": 7, "cells": cells},
        "grid_attributes": {"terrain": {"status": "passed", "cells": {}}},
        "corridor_policy": policy(width=200.0),
        "existing_facilities": {"status": "passed", "items": items},
        "device_catalog": {"status": "passed", "items": catalog_items},
    }


def _preflight(**kwargs):
    from cns_planner.algorithms.corridor.v1 import estimate_corridor_complexity

    inputs = _build_preflight_inputs(**kwargs)
    return inputs, estimate_corridor_complexity(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"], inputs["grid_attributes"],
        inputs["corridor_policy"], inputs["existing_facilities"], inputs["device_catalog"],
    )


def _preflight_threshold_fixture(providers_per_subsystem=8, layers=1):
    """构造一个"评估上界可越过包线阈值"的夹具（几何紧凑，断言毫秒级）。

    网格覆盖一条长航路（corridor 单元多）、层带与航路高度必然相交、provider 全部
    聚在航路旁并通过**大服务半径**覆盖每个探针。于是
    ``estimated_evaluation_upper_bound`` ≈ ``corridor 单元 × provider 数 × 层数``；
    ``layers`` 与 ``providers_per_subsystem`` 是把这个上界推到任意档位的两个杠杆。

    杠杆优先用 ``layers``：它抬高上界的同时只让"每个探针格 × provider"的真实评估
    重复若干次，因此既能把上界推过阈值，又把测试自身耗时压在毫秒级。
    """

    step_lon, step_lat = 0.001, 0.001
    cells = []
    for row in range(24):
        for column in range(24):
            left, bottom = column * step_lon, row * step_lat
            cells.append({
                "grid_id": f"H{row:03d}{column:03d}",
                "bbox": [left, bottom, left + step_lon, bottom + step_lat],
            })
    route = {"route_id": "R1", "status": "passed",
             "path": [[0.011, 0.0], [0.013, 0.02]]}
    spatial = {
        "status": "passed",
        # 每层都是宽层带：保证航路高度带（100 ± 20 m）一定落在每一层内。
        "altitude_layers": [{
            "altitude_layer_id": f"L{index}", "name": f"L{index}",
            "lower_altitude_m": 0.0, "upper_altitude_m": 1000.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        } for index in range(layers)],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 100.0,
            "waypoints": [], "source": "test", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    items, catalog_items = [], []
    for code in ("C", "N", "S"):
        for index in range(providers_per_subsystem):
            device_id, facility_id = f"{code}D{index:05d}", f"{code}F{index:05d}"
            entry = device(radius=20_000.0)
            entry["device_id"] = device_id
            entry["subsystem"] = code
            catalog_items.append(entry)
            items.append({
                "facility_id": facility_id,
                "coordinate": [0.021 + 0.000005 * index, 0.02],
                "status": "active",
                "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
                "devices": [{"device_id": device_id, "subsystem": code, "status": "active"}],
            })
    return {
        "routes": [route], "spatial_3d": spatial,
        "grid": {"status": "passed", "level": 7, "cells": cells},
        "grid_attributes": {"terrain": {"status": "passed", "cells": {}}},
        "corridor_policy": policy(width=300.0),
        "existing_facilities": {"status": "passed", "items": items},
        "device_catalog": {"status": "passed", "items": catalog_items},
    }


def _estimate_threshold_fixture(providers_per_subsystem=200, layers=1):
    from cns_planner.algorithms.corridor.v1 import estimate_corridor_complexity

    data = _preflight_threshold_fixture(providers_per_subsystem, layers=layers)
    return data, estimate_corridor_complexity(
        data["routes"], data["spatial_3d"], data["grid"], data["grid_attributes"],
        data["corridor_policy"], data["existing_facilities"], data["device_catalog"],
    )


def test_complexity_preflight_estimates_are_estimates_not_measurements():
    """preflight 必须自我标注为估算，且不产生任何结果或副作用。"""

    from cns_planner.algorithms.corridor import v1 as corridor_module

    inputs, estimate = _preflight(grid_cells=100, providers_per_subsystem=5)

    assert estimate["status"] == "estimated"
    assert estimate["is_precise_measurement"] is False
    assert estimate["estimate_method"]
    assert estimate["estimate_basis"]
    assert estimate["estimated_corridor_cells"] >= 0
    assert estimate["estimated_voxels"] >= 0
    assert estimate["estimated_evaluation_upper_bound"] >= 0
    assert estimate["grid_cell_count"] == len(inputs["grid"]["cells"])
    assert estimate["provider_count_by_subsystem"] == {"C": 5, "N": 5, "S": 5}

    # 纯估算：不得触发任何体素探测或 P8 判定。
    calls = []
    original = {
        "capability": corridor_module.evaluate_capability_point,
        "geometry": corridor_module.evaluate_geometry_point,
        "cell_voxels": corridor_module.CNSServiceCorridorV1._cell_voxels,
    }

    def spy(name, target):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return target(*args, **kwargs)

        return wrapper

    corridor_module.evaluate_capability_point = spy(
        "capability", original["capability"])
    corridor_module.evaluate_geometry_point = spy("geometry", original["geometry"])
    corridor_module.CNSServiceCorridorV1._cell_voxels = spy(
        "cell_voxels", original["cell_voxels"])
    try:
        _preflight(grid_cells=100, providers_per_subsystem=5)
    finally:
        corridor_module.evaluate_capability_point = original["capability"]
        corridor_module.evaluate_geometry_point = original["geometry"]
        corridor_module.CNSServiceCorridorV1._cell_voxels = original["cell_voxels"]
    assert calls == [], f"preflight 不得调用评估路径，实际调用了 {calls}"


def test_complexity_preflight_is_monotonic_in_provider_scale():
    """同一航路下 provider 越多，评估上界必须单调不减（纯估算的一致性）。"""

    _small_inputs, small = _preflight(grid_cells=100, providers_per_subsystem=2)
    _large_inputs, large = _preflight(grid_cells=100, providers_per_subsystem=40)

    assert small["estimated_corridor_cells"] == large["estimated_corridor_cells"]
    assert large["provider_count_total"] > small["provider_count_total"]
    assert large["estimated_evaluation_upper_bound"] >= small["estimated_evaluation_upper_bound"]



def _preflight_above_threshold(providers_per_subsystem=8):
    """构造一个"评估上界可越过包线阈值"的夹具（几何紧凑，断言毫秒级）。

    网格覆盖一条长航路（corridor 单元多）、层带与航路高度必然相交、provider 全部
    聚在航路旁并通过**大服务半径**覆盖每个探针。于是
    ``estimated_evaluation_upper_bound`` ≈ ``corridor 单元 × provider 数 × 层数``，
    用 ``providers_per_subsystem`` 就能把上界推到任意档位，而真实计算量仍然很小。
    """

    step_lon, step_lat = 0.001, 0.001
    cells = []
    for row in range(40):
        for column in range(50):
            left, bottom = column * step_lon, row * step_lat
            cells.append({
                "grid_id": f"H{row:03d}{column:03d}",
                "bbox": [left, bottom, left + step_lon, bottom + step_lat],
            })
    route = {"route_id": "R1", "status": "passed",
             "path": [[0.021, 0.0], [0.023, 0.04]]}
    spatial = {
        "status": "passed",
        # 单一宽层带：保证航路高度带（100 ± 20 m）一定落在层内。
        "altitude_layers": [{
            "altitude_layer_id": "L1", "name": "L1",
            "lower_altitude_m": 0.0, "upper_altitude_m": 300.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 100.0,
            "waypoints": [], "source": "test", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    items, catalog_items = [], []
    for code in ("C", "N", "S"):
        for index in range(providers_per_subsystem):
            device_id, facility_id = f"{code}D{index:05d}", f"{code}F{index:05d}"
            entry = device(radius=20_000.0)
            entry["device_id"] = device_id
            entry["subsystem"] = code
            catalog_items.append(entry)
            items.append({
                "facility_id": facility_id,
                "coordinate": [0.021 + 0.00001 * index, 0.02],
                "status": "active",
                "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
                "devices": [{"device_id": device_id, "subsystem": code, "status": "active"}],
            })
    return {
        "routes": [route], "spatial_3d": spatial,
        "grid": {"status": "passed", "level": 7, "cells": cells},
        "grid_attributes": {"terrain": {"status": "passed", "cells": {}}},
        "corridor_policy": policy(width=300.0),
        "existing_facilities": {"status": "passed", "items": items},
        "device_catalog": {"status": "passed", "items": catalog_items},
    }


def _estimate_above_threshold(providers_per_subsystem=8):
    from cns_planner.algorithms.corridor.v1 import estimate_corridor_complexity

    data = _preflight_above_threshold(providers_per_subsystem)
    return data, estimate_corridor_complexity(
        data["routes"], data["spatial_3d"], data["grid"], data["grid_attributes"],
        data["corridor_policy"], data["existing_facilities"], data["device_catalog"],
    )


def test_complexity_tiers_are_ordered_and_ceiling_is_blocking():
    """三档必须有序：within < beyond（不阻断）< ceiling（阻断）。"""

    from cns_planner.algorithms.corridor.v1 import (
        COMPLEXITY_TIER_BEYOND, COMPLEXITY_TIER_CEILING, COMPLEXITY_TIER_VALIDATED,
        SAFETY_EVALUATION_CEILING, VALIDATED_EVALUATION_LIMIT,
    )

    assert 0 < VALIDATED_EVALUATION_LIMIT < SAFETY_EVALUATION_CEILING

    _inputs, within = _preflight(grid_cells=100, providers_per_subsystem=2)
    assert within["tier"] == COMPLEXITY_TIER_VALIDATED
    assert within["blocking"] is False
    assert within["beyond_validated_envelope"] is False
    assert within["message"] == ""

    _data, beyond = _estimate_threshold_fixture(400, layers=20)
    assert beyond["estimated_evaluation_upper_bound"] > VALIDATED_EVALUATION_LIMIT
    assert beyond["tier"] == COMPLEXITY_TIER_BEYOND
    assert beyond["beyond_validated_envelope"] is True
    assert beyond["blocking"] is False
    assert beyond["message"], "超界必须给出可读原因"

    _data, ceiling = _estimate_threshold_fixture(1200, layers=20)
    assert ceiling["estimated_evaluation_upper_bound"] > SAFETY_EVALUATION_CEILING
    assert ceiling["tier"] == COMPLEXITY_TIER_CEILING
    assert ceiling["blocking"] is True
    assert "硬上限" in ceiling["message"]

def test_corridor_complexity_blocked_raises_before_constructing_result():
    """硬安全天花板必须在计算前阻断，且不返回任何结果对象。"""

    from cns_planner.algorithms.corridor.v1 import (
        CNSServiceCorridorV1, CorridorComplexityBlocked, COMPLEXITY_TIER_CEILING,
    )

    data, estimate = _estimate_threshold_fixture(1200, layers=20)
    assert estimate["tier"] == COMPLEXITY_TIER_CEILING
    inputs = data

    with pytest.raises(CorridorComplexityBlocked) as caught:
        CNSServiceCorridorV1().evaluate(
            inputs["routes"], inputs["spatial_3d"], inputs["grid"],
            inputs["grid_attributes"], requirement_set("C"), aircraft("C"),
            inputs["existing_facilities"], inputs["device_catalog"],
            inputs["corridor_policy"], preflight=estimate,
        )
    assert caught.value.estimate["blocking"] is True
    # B9R.1：硬天花板不可绕过——显式风险接受同样被无条件阻断。
    with pytest.raises(CorridorComplexityBlocked):
        CNSServiceCorridorV1().evaluate(
            inputs["routes"], inputs["spatial_3d"], inputs["grid"],
            inputs["grid_attributes"], requirement_set("C"), aircraft("C"),
            inputs["existing_facilities"], inputs["device_catalog"],
            inputs["corridor_policy"], preflight=estimate,
            allow_beyond_validated_envelope=True,
        )


def test_beyond_validated_envelope_requires_explicit_acceptance_and_keeps_math():
    """B9R.1：beyond 档默认拒绝；显式接受后照常算，且结果与"不传 preflight"逐字一致。"""

    from cns_planner.algorithms.corridor.v1 import (
        CNSServiceCorridorV1, CorridorComplexityBlocked,
    )

    inputs, estimate = _estimate_threshold_fixture(400, layers=20)
    assert estimate["tier"] == "beyond_validated_envelope"
    assert estimate["blocking"] is False

    plain = CNSServiceCorridorV1().evaluate(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"], inputs["grid_attributes"],
        requirement_set("C"), aircraft("C"), inputs["existing_facilities"],
        inputs["device_catalog"], inputs["corridor_policy"],
    )
    # beyond 档：未显式接受 → 计算前阻断（不再静默继续）。
    with pytest.raises(CorridorComplexityBlocked) as caught:
        CNSServiceCorridorV1().evaluate(
            inputs["routes"], inputs["spatial_3d"], inputs["grid"], inputs["grid_attributes"],
            requirement_set("C"), aircraft("C"), inputs["existing_facilities"],
            inputs["device_catalog"], inputs["corridor_policy"], preflight=estimate,
        )
    assert caught.value.estimate["tier"] == "beyond_validated_envelope"
    # 显式接受后照常计算，且准入判定不得改变任何数学结果。
    with_preflight = CNSServiceCorridorV1().evaluate(
        inputs["routes"], inputs["spatial_3d"], inputs["grid"], inputs["grid_attributes"],
        requirement_set("C"), aircraft("C"), inputs["existing_facilities"],
        inputs["device_catalog"], inputs["corridor_policy"], preflight=estimate,
        allow_beyond_validated_envelope=True,
    )
    assert with_preflight == plain, "准入判定不得改变任何计算结果"


def test_sync_corridor_entry_rejects_beyond_safety_ceiling(tmp_path):
    """同步入口必须给出可读拒绝，而不是先构造数 GB 结果再失败。"""

    from cns_planner.application.corridor_service import CorridorScaleNotAccepted
    from cns_planner.application.workflow_service import WorkflowService

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    inputs, _estimate = _estimate_threshold_fixture(1200, layers=20)
    workflow.state.update({
        "operational_routes": inputs["routes"],
        "spatial_3d": inputs["spatial_3d"],
        "grid": inputs["grid"],
        "grid_attributes": inputs["grid_attributes"],
        "required_cns": requirement_set("C"),
        "aircraft_profiles": {"items": [aircraft("C")], "count": 1},
        "selected_aircraft_profile_id": "A1",
        "existing_cns_facilities": inputs["existing_facilities"],
        "device_catalog": inputs["device_catalog"],
        "cns_corridor_policy": inputs["corridor_policy"],
    })

    estimate = workflow.corridor_service.complexity_estimate({})
    assert estimate["blocking"] is True

    with pytest.raises(CorridorScaleNotAccepted) as caught:
        workflow.evaluate_cns_corridor({})
    assert "硬上限" in str(caught.value)
    assert caught.value.estimate["blocking"] is True
    # 拒绝时不得写入任何 canonical result。
    assert not (workflow.state.get("cns_corridor_assessment") or {}).get("routes")


def test_sync_corridor_entry_attaches_read_only_estimate(tmp_path):
    """包线内的同步入口照常计算，只附加只读复杂度元数据。"""

    from cns_planner.application.workflow_service import WorkflowService

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    inputs = _build_preflight_inputs(grid_cells=100, providers_per_subsystem=2)
    workflow.state.update({
        "operational_routes": inputs["routes"],
        "spatial_3d": inputs["spatial_3d"],
        "grid": inputs["grid"],
        "grid_attributes": inputs["grid_attributes"],
        "required_cns": requirement_set("C"),
        "aircraft_profiles": {"items": [aircraft("C")], "count": 1},
        "selected_aircraft_profile_id": "A1",
        "existing_cns_facilities": inputs["existing_facilities"],
        "device_catalog": inputs["device_catalog"],
        "cns_corridor_policy": inputs["corridor_policy"],
    })

    snapshot = workflow.evaluate_cns_corridor({})
    estimate = snapshot.get("corridor_complexity_estimate") or {}
    assert estimate.get("tier")
    assert estimate.get("is_precise_measurement") is False
    # canonical assessment 本身不得被塞入估算字段（result schema 不变）。
    assessment = workflow.state.get("cns_corridor_assessment") or {}
    assert "complexity_estimate" not in assessment
    assert "corridor_complexity_estimate" not in assessment


def test_optional_cancel_check_interrupts_long_loop_and_keeps_math_unchanged():
    """``cancel_check`` 抛出时立即中断长循环；不抛出时结果与默认路径逐字段一致。"""

    inputs = _progress_fixture()
    baseline = CNSServiceCorridorV1().evaluate(*deepcopy(inputs))

    calls = {"count": 0}

    def never_cancel():
        calls["count"] += 1

    unchanged = CNSServiceCorridorV1().evaluate(*deepcopy(inputs), cancel_check=never_cancel)
    assert unchanged == baseline, "永不取消的检查点不得改变结果"
    assert calls["count"] >= 1, "长循环必须真正调用 cancel_check"

    class Cancelled(Exception):
        pass

    def cancel_immediately():
        raise Cancelled("stop")

    with pytest.raises(Cancelled):
        CNSServiceCorridorV1().evaluate(
            *deepcopy(inputs), cancel_check=cancel_immediately,
            progress_callback=lambda value, message: None,
        )
