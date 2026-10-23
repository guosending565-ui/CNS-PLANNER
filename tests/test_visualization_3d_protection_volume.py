from __future__ import annotations

import math

import pytest
from pyproj import Transformer

from cns_planner.application.visualization_3d_protection_volume import (
    ProtectionVolumeInputError,
    build_engineering_preview,
    build_horizontal_corridor,
    project_p17_protection_volumes,
    route_path_fingerprint,
)


HISTORICAL_PATH = [
    [122.2000, 29.9000],
    [122.2100, 29.9040],
    [122.2200, 29.9090],
]
DEMO_PATH = [
    [122.2000, 29.9000],
    [122.2080, 29.9020],
    [122.2160, 29.9070],
    [122.2240, 29.9100],
]


def _route(path, *, authority, candidate_id, status="stale"):
    return {
        "authority": authority,
        "status": status,
        "route_id": "R0005",
        "path": path,
        "identity": {
            "identity_id": f"route-geometry:R0005:{candidate_id}",
            "fingerprint": route_path_fingerprint(path),
            "candidate_id": candidate_id,
            "point_count": len(path),
        },
        "altitude": {
            "altitude_layer_id": "ALT-100",
            "nominal_altitude_m": 100.0,
            "vertical_reference": "EGM2008",
            "layer_status": "confirmed",
            "layer_confirmed": True,
            "source": "software project default, non measured/project source",
            "full_3d_profile_available": False,
        },
    }


def _state(*, half_width=990.0, corridor_path=HISTORICAL_PATH):
    return {
        "revision": 468,
        "continuous_service_acceptability": {
            "status": "stale",
            "stale_reason": "p16_or_upstream_changed",
            "algorithm_id": "continuous_service_acceptability_v1",
            "algorithm_version": "2.2",
            "input_fingerprint": "p17-input-fingerprint",
            "routes": [{
                "route_id": "R0005",
                "route_length_m": 9904.422448544,
                "corridor": {
                    "status": "evaluated",
                    "route_path": corridor_path,
                    "route_length_m": 9904.422448544,
                    "cns_requirement_corridor_half_width_m": 500.0,
                    "D_separation_m": 30.0,
                    "D_separation_authority": "explicit_evidence",
                    "D_maneuver_m": 50.0,
                    "D_maneuver_authority": "builtin_engineering_assumption",
                    "D_uncertainty_m": 10.0,
                    "D_uncertainty_authority": "explicit_evidence",
                    "V_relative_mps": 40.0,
                    "relative_speed_basis": "conservative",
                    "T_chain_s": 10.0,
                    "T_chain_components_s": {
                        "detect_track": 3.0,
                        "sensor_to_platform": 1.0,
                        "platform_processing": 2.0,
                        "platform_to_aircraft": 1.0,
                        "aircraft_response_manoeuvre": 3.0,
                    },
                    "D_protection_m": 490.0,
                    "outer_half_width_m": half_width,
                    "formula": (
                        "D_protection = D_separation + V_relative * T_chain + "
                        "D_maneuver + D_uncertainty"
                    ),
                },
            }],
        },
    }


def test_buffer_uses_metric_crs_and_round_caps_for_a_straight_route():
    to_wgs84 = Transformer.from_crs("EPSG:32651", "EPSG:4326", always_xy=True)
    lon1, lat1 = to_wgs84.transform(423000.0, 3310000.0)
    lon2, lat2 = to_wgs84.transform(424000.0, 3310000.0)

    result = build_horizontal_corridor([[lon1, lat1], [lon2, lat2]], 100.0)

    expected_area = 2.0 * 100.0 * 1000.0 + math.pi * 100.0 ** 2
    assert result["status"] == "available"
    assert result["construction_crs"] == "EPSG:32651"
    assert result["geometry"]["type"] == "Polygon"
    assert result["route_length_projected_m"] == pytest.approx(1000.0, abs=0.02)
    assert result["projected_area_m2"] == pytest.approx(expected_area, rel=0.001)
    assert result["boundary_ring_count"] == 1
    assert result["is_valid"] is True


def test_buffer_repairs_crossing_route_and_returns_valid_wgs84_geometry():
    crossing = [
        [122.20, 29.90], [122.22, 29.92], [122.20, 29.92], [122.22, 29.90],
    ]
    result = build_horizontal_corridor(crossing, 80.0)
    coordinates = result["geometry"]["coordinates"]

    assert result["status"] == "available"
    assert result["is_valid"] is True
    assert result["projected_area_m2"] > 0
    assert result["boundary_ring_count"] >= 1
    assert all(121.0 < point[0] < 123.0 and 29.0 < point[1] < 31.0
               for polygon in coordinates for ring in polygon
               for point in ring) if result["geometry"]["type"] == "MultiPolygon" else all(
                   121.0 < point[0] < 123.0 and 29.0 < point[1] < 31.0
                   for ring in coordinates for point in ring
               )


def test_historical_p17_matches_only_the_exact_11_point_identity():
    historical = _route(
        HISTORICAL_PATH,
        authority="canonical_operational_routes",
        candidate_id="LRC2-R0005-ALT-100-layeredcandv1-05",
    )
    demo = _route(
        DEMO_PATH,
        authority="engineering_demo_projection",
        candidate_id="LRC2-R0005-ALT-100-layeredcandv1-91",
        status="engineering_demo_only",
    )

    payload = project_p17_protection_volumes(
        _state(), [historical, demo], current_algorithm_version="2.1")

    assert payload["status"] == "historical_only"
    assert payload["stored_algorithm_version"] == "2.2"
    assert payload["current_code_algorithm_version"] == "2.1"
    assert payload["algorithm_version_conflict"] is True
    historical_volume = next(row for row in payload["volumes"]
                             if row["route_authority"] == "canonical_operational_routes")
    assert historical_volume["object_id"] == "protection-corridor:R0005:ALT-100"
    assert historical_volume["status"] == "historical_stale"
    assert historical_volume["outer_half_width_m"] == 990.0
    assert historical_volume["route_geometry_identity"] == historical["identity"]
    assert historical_volume["geometry_dimension"] == "horizontal_boundary_with_altitude_slice"
    assert historical_volume["vertical_extent_status"] == "vertical_extent_unconfirmed"
    assert historical_volume["nominal_altitude_slice_egm2008_m"] == 100.0
    assert historical_volume["lower_altitude_m"] is None
    assert historical_volume["upper_altitude_m"] is None
    assert historical_volume["geometry"]["status"] == "available"
    assert "confirmed_vertical_lower_and_upper_interfaces" in historical_volume["required_evidence"]

    conflict = next(row for row in payload["volumes"]
                    if row["route_authority"] == "engineering_demo_projection")
    assert conflict["status"] == "geometry_version_conflict"
    assert conflict["geometry"]["status"] == "blocked"
    assert conflict["geometry"]["reason"] == "geometry_version_conflict"
    assert conflict["route_geometry_identity"] == demo["identity"]
    assert conflict["p17_route_geometry_identity"]["fingerprint"] == route_path_fingerprint(
        HISTORICAL_PATH)


def test_missing_p17_half_width_fails_closed_without_geometry():
    route = _route(
        HISTORICAL_PATH,
        authority="canonical_operational_routes",
        candidate_id="historical",
    )
    payload = project_p17_protection_volumes(
        _state(half_width=None), [route], current_algorithm_version="2.1")
    volume = payload["volumes"][0]

    assert payload["status"] == "blocked"
    assert volume["status"] == "evidence_required"
    assert volume["geometry"]["status"] == "blocked"
    assert volume["geometry"]["reason"] == "outer_half_width_m_missing"
    assert "continuous_service_acceptability.routes[].corridor.outer_half_width_m" in (
        volume["required_evidence"])


def test_corridor_outside_utm_construction_zone_fails_closed():
    """跨 180° 经线的航路在 UTM 51N 中畸变成数千公里，必须拒绝而不是画巨型走廊。"""

    with pytest.raises(ProtectionVolumeInputError, match="construction zone"):
        build_horizontal_corridor([[179.9, 29.9], [-179.9, 29.9]], 250.0)


def test_preview_rejects_route_outside_construction_zone():
    route = {
        "route_id": "R-FAR",
        "authority": "engineering_demo_projection",
        "path": [[179.9, 29.9], [-179.9, 29.9]],
    }
    with pytest.raises(ProtectionVolumeInputError, match="construction zone"):
        build_engineering_preview(
            route, half_width_m=250, lower_altitude_m=90, upper_altitude_m=125)


def test_preview_declares_region_and_construction_zone_validation():
    route = _route(
        DEMO_PATH,
        authority="engineering_demo_projection",
        candidate_id="demo-current",
        status="engineering_demo_only",
    )
    preview = build_engineering_preview(
        route, half_width_m=250, lower_altitude_m=90, upper_altitude_m=125)

    assert preview["input_validation"] == {
        "all_values_explicit": True,
        "numeric_and_finite": True,
        "route_geometry_checked": True,
        "region_checked": True,
        "construction_zone_checked": True,
        "bounds_order_checked": True,
    }


def test_engineering_preview_requires_explicit_valid_width_and_vertical_bounds():
    route = _route(
        DEMO_PATH,
        authority="engineering_demo_projection",
        candidate_id="demo-current",
        status="engineering_demo_only",
    )
    with pytest.raises(ProtectionVolumeInputError, match="half_width_m"):
        build_engineering_preview(route, half_width_m=None,
                                  lower_altitude_m=90, upper_altitude_m=125)
    with pytest.raises(ProtectionVolumeInputError, match="lower.*upper"):
        build_engineering_preview(route, half_width_m=250,
                                  lower_altitude_m=125, upper_altitude_m=90)

    preview = build_engineering_preview(
        route, half_width_m=250, lower_altitude_m=90, upper_altitude_m=125)
    assert preview["object_id"].startswith("protection-preview:")
    assert preview["mode"] == "engineering_scenario_preview"
    assert preview["geometry_dimension"] == "three_dimensional_volume"
    assert preview["vertical_extent_status"] == "user_supplied_engineering_preview"
    assert preview["lower_altitude_m"] == 90.0
    assert preview["upper_altitude_m"] == 125.0
    assert preview["disclaimer"] == "自定义保护区示意，非已确认 P17 结果"
    assert preview["isolation"] == {
        "written_to_project_state": False,
        "enters_formal_p17": False,
        "enters_cns_statistics": False,
        "enters_radar_los": False,
        "operational_authorization": False,
    }
