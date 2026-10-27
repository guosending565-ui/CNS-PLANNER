"""C3-C：三维只读投影与 EGM2008 椭球高换算的定向测试。"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import struct

import pytest

from cns_planner.application.research_visualization_service import (
    Visualization3DUnavailable, build_visualization_3d, canonical_sha256,
    load_terrain_asset, sample_terrain,
)
from cns_planner.gis.egm2008_geoid import Egm2008Geoid, GeoidUnavailable, load_geoid


class FakeGeoid:
    def describe(self):
        return {
            "filename": "us_nga_egm08_25.tif",
            "sha256": "a" * 64,
            "canonical_backend": "proj_vgridshift_bilinear",
            "pixel_size_deg": [0.041666666, 0.041666666],
            "self_description": "EGM2008 height (EPSG:3855)",
        }

    def undulation(self, lon, lat):
        return {"status": "passed", "value": 14.5}

    def undulation_strict(self, lon, lat):
        return 14.5


def _source_state(vertical_reference="egm2008_orthometric"):
    return {
        "spatial_3d": {
            "altitude_layers": [{
                "altitude_layer_id": "ALT-100", "name": "100 m 巡航高度层",
                "nominal_altitude_m": 100.0, "lower_altitude_m": 90.0,
                "upper_altitude_m": 125.0, "vertical_reference": "egm2008_orthometric",
                "source": "engineering_default",
            }],
            "route_operating_layers": [{
                "route_id": "R0005", "active": True, "altitude_layer_id": "ALT-100",
                "vertical_reference": vertical_reference, "research_only": True,
                "source": "research_projection_frozen_candidate_alt100",
            }],
        },
        "operational_routes": [{
            "route_id": "R0005", "vertical_reference": "egm2008_orthometric",
            "algorithm_compatibility": {
                "altitude": "frozen_research_premise_ALT-100_egm2008_orthometric"},
        }],
    }


def _manifest(tmp_path, *, radar_status="not_calculated", radar_conclusion="unknown",
              vertical_reference="egm2008_orthometric", n_actions=4):
    source = tmp_path / "source"
    (source / "runtime").mkdir(parents=True)
    state_path = source / "runtime" / "project_state.json"
    state_path.write_text(json.dumps(
        _source_state(vertical_reference), ensure_ascii=False), encoding="utf-8")
    actions = [
        {"action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
         "site_id": "N005", "coordinate": [122.2672, 29.8666],
         "planner_family": "endpoint_integrity_monitor", "segment_index": None,
         "kind": "other", "planning_status": "proposal_only"},
        {"action_id": "add_navigation_integrity_monitor:R0005:destination:N006",
         "site_id": "N006", "coordinate": [122.1883, 29.8194],
         "planner_family": "endpoint_integrity_monitor", "segment_index": None,
         "kind": "other", "planning_status": "proposal_only"},
        {"action_id": "navigation_integrity_endpoint_middle_reuse_v1:R0005:NEW-422750:4",
         "site_id": "NEW-422750", "coordinate": [122.2004, 29.8326],
         "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
         "segment_index": 4, "kind": "middle", "planning_status": "proposal_only"},
        {"action_id": "navigation_integrity_endpoint_middle_reuse_v1:R0005:NEW-425000:3",
         "site_id": "NEW-425000", "coordinate": [122.2236, 29.8417],
         "planner_family": "navigation_integrity_endpoint_middle_reuse_v1",
         "segment_index": 3, "kind": "middle", "planning_status": "proposal_only"},
    ][:n_actions]
    sites = [
        {"site_id": "NEW-422750", "facility_id": "RES-NEW-422750",
         "label": "NEW-422750", "coordinate": [122.2004, 29.8326],
         "kind": "planned_new_site", "proposal_only": True, "reuse_class": "greenfield",
         "subsystems": ["C", "N", "S"], "device_count": 3,
         "devices": [
             {"device_id": "C-1", "service_key": "C:communication", "subsystem": "C",
              "radius_m": 2000.0, "radius_source": "radius_by_surface",
              "actual_delivery": "unknown"},
             {"device_id": "N-1", "service_key": "N:navigation_integrity_monitoring",
              "subsystem": "N", "radius_m": 1000.0, "radius_source": "radius_by_surface",
              "actual_delivery": "unknown"},
             {"device_id": "S-1", "service_key": "S:rid_cooperative", "subsystem": "S",
              "radius_m": 5000.0, "radius_source": "radius_by_surface",
              "actual_delivery": "unknown"},
         ]},
        {"site_id": "takeoff_landing_site:N006", "facility_id": "RES-N006",
         "label": "起降点 N006", "coordinate": [122.1883, 29.8194],
         "kind": "existing_host", "proposal_only": False, "reuse_class": "existing_shared_site",
         "subsystems": ["C", "S"], "device_count": 2,
         "devices": [
             {"device_id": "C-2", "service_key": "C:communication", "subsystem": "C",
              "radius_m": 2000.0, "radius_source": "radius_by_surface"},
             {"device_id": "S-2", "service_key": "S:rid_cooperative", "subsystem": "S",
              "radius_m": 5000.0, "radius_source": "radius_by_surface"},
         ]},
    ]
    layers = [
        {"id": "C", "label": "Communication C", "status": "satisfied",
         "conclusion": "satisfied", "evidence_role": "input_radius_projection",
         "features": [], "feature_count": 0},
        {"id": "RID", "label": "RID cooperative", "status": "satisfied",
         "conclusion": "satisfied", "evidence_role": "input_radius_projection",
         "features": [], "feature_count": 0},
        {"id": "Radar", "label": "Radar non-cooperative", "status": radar_status,
         "conclusion": radar_conclusion, "formal_status": radar_status,
         "reason": "radar_layout_missing", "features": [], "feature_count": 0,
         "evidence_role": "no_layout_no_geometry"},
        {"id": "S", "label": "Surveillance S", "status": "unknown",
         "conclusion": "unknown", "evidence_role": "input_radius_projection",
         "features": [], "feature_count": 0},
        {"id": "N", "label": "Navigation N actions", "status": "unknown",
         "conclusion": "unknown", "evidence_role": "p16_selected_actions_proposal_only",
         "features": actions, "feature_count": len(actions)},
    ]
    return {
        "run_id": "run-test", "task": "C3-C", "route_id": "R0005",
        "authority": "research_isolated", "contract_version": "contract-test",
        "created_at": "2026-10-27T00:00:00+0800",
        "input_version": {
            "id": "in-test", "input_fingerprint": "f" * 64,
            "parameter_fingerprint": "p" * 64,
        },
        "authoritative_result_list_sha256": "r" * 64,
        "stages": {"P16": {"result_sha256": "s" * 64}},
        "execution": {"mode": "fresh_compute"},
        "provenance": {
            "solver_executed": True, "source_root": str(source),
            "source_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        },
        "map_projection": {
            "run_id": "run-test", "route": {
                "route_id": "R0005", "label": "R0005", "name": "R0005 研究投影",
                "altitude_layer_id": "ALT-100", "nominal_altitude_m": 100.0,
                "path": [[122.2672, 29.8666], [122.2236, 29.8417],
                         [122.1883, 29.8194]],
            },
            "sites": sites, "n_actions": actions, "layers": layers,
            "counts": {"sites": len(sites), "devices": 5, "existing_hosts": 1,
                       "planned_new_sites": 1, "n_actions": len(actions)},
        },
    }


def _terrain_asset(root, route):
    path = root / "c3c" / "terrain" / "r0005_terrain_100m.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 3, 2
    heights = struct.pack("<6f", 10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    payload = {
        "schema_version": "c3c-terrain-grid-1",
        "route_geometry_sha256": canonical_sha256(route),
        "extent": [122.18, 29.80, 122.28, 29.88],
        "width": width, "height": height,
        "xres_deg": 0.05, "yres_deg": 0.08,
        "grid_resolution_m": 100.0, "row_order": "north_to_south",
        "crs": "EPSG:4326", "values_dtype": "float32_le",
        "heights_ellipsoidal_m_b64": base64.b64encode(heights).decode("ascii"),
        "height_min_m": 10.0, "height_max_m": 60.0,
        "undulation_min_m": 14.4, "undulation_max_m": 14.6,
        "nodata_count": 0,
        "nodata_policy": "display_zero_ellipsoidal_disclosed_not_business_sea_level",
        "fabdem": {"filename": "FABDEM.tif", "sha256": "b" * 64,
                   "vertical_reference": "EGM2008_orthometric"},
        "geoid": {"filename": "us_nga_egm08_25.tif", "sha256": "a" * 64},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_build_projection_uses_per_vertex_egm2008_conversion(tmp_path):
    manifest = _manifest(tmp_path)
    payload = build_visualization_3d(
        manifest, root=tmp_path, manifest_file_sha256="m" * 64,
        geoid_loader=lambda: (FakeGeoid(), None))
    assert payload["identity"]["run_id"] == "run-test"
    assert payload["identity"]["result_list_sha256"] == "r" * 64
    assert payload["counts"] == {"sites": 2, "devices": 5, "existing_hosts": 1,
                                 "planned_new_sites": 1, "n_actions": 4}
    for point in payload["route"]["points"]:
        assert point["altitude_orthometric_m"] == 100.0
        assert point["undulation_m"] == 14.5
        assert point["altitude_ellipsoidal_m"] == 114.5
    assert payload["vertical"]["formula"] == "h_ellipsoidal = H_orthometric + N_EGM2008"
    assert payload["vertical"]["source_state_sha256_verified"] is True
    assert payload["vertical"]["canonical_reference"] == "EGM2008_orthometric"
    radar = next(layer for layer in payload["layers"] if layer["id"] == "Radar")
    assert radar["status"] == "not_calculated" and radar["conclusion"] == "unknown"
    assert radar["features"] == [] and radar["feature_count"] == 0
    assert payload["terrain"]["status"] == "unavailable"


def test_payload_digest_is_stable_and_excludes_generated_at(tmp_path):
    manifest = _manifest(tmp_path)
    first = build_visualization_3d(
        manifest, root=tmp_path, manifest_file_sha256="m" * 64,
        geoid_loader=lambda: (FakeGeoid(), None))
    second = build_visualization_3d(
        manifest, root=tmp_path, manifest_file_sha256="m" * 64,
        geoid_loader=lambda: (FakeGeoid(), None))
    assert first["payload_sha256"] == second["payload_sha256"]


def test_radar_cannot_be_reported_as_satisfied(tmp_path):
    manifest = _manifest(tmp_path, radar_status="satisfied", radar_conclusion="satisfied")
    with pytest.raises(Visualization3DUnavailable) as excinfo:
        build_visualization_3d(manifest, root=tmp_path,
                               geoid_loader=lambda: (FakeGeoid(), None))
    assert excinfo.value.code == "radar_status_not_fail_closed"


def test_vertical_reference_must_be_egm2008(tmp_path):
    manifest = _manifest(tmp_path, vertical_reference="agl")
    with pytest.raises(Visualization3DUnavailable) as excinfo:
        build_visualization_3d(manifest, root=tmp_path,
                               geoid_loader=lambda: (FakeGeoid(), None))
    assert excinfo.value.code == "route_vertical_reference_not_egm2008"


def test_source_state_sha_mismatch_is_fail_closed(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["provenance"]["source_state_sha256"] = "0" * 64
    with pytest.raises(Visualization3DUnavailable) as excinfo:
        build_visualization_3d(manifest, root=tmp_path,
                               geoid_loader=lambda: (FakeGeoid(), None))
    assert excinfo.value.code == "route_vertical_reference_source_sha_mismatch"


def test_terrain_grid_is_sampled_and_identity_checked(tmp_path):
    manifest = _manifest(tmp_path)
    route = manifest["map_projection"]["route"]["path"]
    _terrain_asset(tmp_path, route)
    terrain = load_terrain_asset(tmp_path, route)
    assert terrain["status"] == "available"
    assert terrain["nodata_count"] == 0
    # 网格中心点：北→南行序，双线性插值。
    value = sample_terrain(terrain, 122.23, 29.84)
    assert value == pytest.approx((10 + 20 + 30 + 40 + 50 + 60) / 6, abs=2.0)
    # 超出范围不插值、不外推。
    assert sample_terrain(terrain, 123.0, 30.0) is None
    # 几何身份不符时拒绝使用。
    terrain_bad = load_terrain_asset(tmp_path, [[122.0, 29.0], [122.1, 29.1]])
    assert terrain_bad["status"] == "unavailable"
    assert terrain_bad["reason"] == "terrain_route_geometry_mismatch"


def test_real_egm2008_grid_converts_with_proj():
    geoid, error = load_geoid()
    if geoid is None:
        pytest.skip(f"EGM2008 geoid grid unavailable: {error}")
    assert isinstance(geoid, Egm2008Geoid)
    first = geoid.undulation(122.2672, 29.8666)
    last = geoid.undulation(122.1883, 29.8194)
    assert first["status"] == "passed" and last["status"] == "passed"
    assert 14.4 < first["value"] < 15.1
    assert 14.4 < last["value"] < 15.1
    assert geoid.orthometric_to_ellipsoidal(100.0, 122.2, 29.83) == pytest.approx(
        100.0 + geoid.undulation_strict(122.2, 29.83), abs=1e-9)
    with pytest.raises(GeoidUnavailable):
        geoid.undulation_strict(500.0, 29.0)
