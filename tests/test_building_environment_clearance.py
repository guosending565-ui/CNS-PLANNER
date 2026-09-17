from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from cns_planner.algorithms.building_clearance import BuildingClearanceV1
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.api.router import ApiRouter
from cns_planner.data.health import build_health
from cns_planner.data.mapping.buildings import BuildingGridService
from cns_planner.domain.building_clearance import (
    default_building_clearance_policy, normalize_building_clearance_policy,
)
from cns_planner.gis.building_clearance_adapter import DtmFootprintSampler
from cns_planner.gis.source_inspection import inspect_geopackage
from cns_planner.reporting.builder import ReportBuilder


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"


def _gpkg(path, kind, row=True):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE gpkg_contents (
          table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT,
          srs_id INTEGER, min_x REAL, min_y REAL, max_x REAL, max_y REAL
        );
        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT, column_name TEXT, geometry_type_name TEXT,
          srs_id INTEGER, z INTEGER, m INTEGER
        );
    """)
    if kind == "building_grid":
        table = "building_grid_L8"
        connection.executescript("""
          CREATE TABLE building_grid_L8 (
            fid INTEGER PRIMARY KEY, geom BLOB, grid_level INTEGER,
            building_count INTEGER, building_area_m2 REAL,
            building_coverage_ratio REAL, height_mean_m REAL,
            height_p95_m REAL, height_max_m REAL, valid_height_fraction REAL,
            west REAL, south REAL, east REAL, north REAL
          );
          CREATE VIRTUAL TABLE rtree_building_grid_L8_geom USING rtree(id,minx,maxx,miny,maxy);
        """)
        if row:
            values = (1, None, 8, 2, 30.0, 0.25, 12.0, 18.0, 20.0, 1.0, 121.0, 30.0, 121.001111111, 30.001111111)
            connection.execute("INSERT INTO building_grid_L8 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            connection.execute("INSERT INTO rtree_building_grid_L8_geom VALUES (1,121,121.001111111,30,30.001111111)")
        geometry = "POLYGON"
    else:
        table = "buildings"
        connection.executescript("""
          CREATE TABLE buildings (
            fid INTEGER PRIMARY KEY, geom BLOB, id TEXT, source TEXT,
            height_m REAL, height_var REAL, height_status TEXT
          );
          CREATE VIRTUAL TABLE rtree_buildings_geom USING rtree(id,minx,maxx,miny,maxy);
        """)
        if row:
            connection.execute("INSERT INTO buildings VALUES (1,NULL,'B1','GBA',12,2,'valid')")
            connection.execute("INSERT INTO rtree_buildings_geom VALUES (1,121,121.001,30,30.001)")
        geometry = "MULTIPOLYGON"
    connection.execute("INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?)", (table, "features", table, 4326, 121.0, 30.0, 121.001111111, 30.001111111))
    connection.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,0,0)", (table, "geom", geometry, 4326))
    connection.commit(); connection.close()


def _grid(level=8, outside=False):
    bbox = [122.0, 31.0, 122.001111111, 31.001111111] if outside else [121.0, 30.0, 121.001111111, 30.001111111]
    return {"level": level, "workspace_bbox": bbox, "cells": [{"grid_id": "G1", "bbox": bbox}]}


def test_building_geopackage_schema_and_rtree_validation(tmp_path):
    path = tmp_path / "buildings.gpkg"; _gpkg(path, "buildings")
    info = inspect_geopackage(path, "buildings")
    assert info["geometry_type"] == "MULTIPOLYGON"
    assert info["spatial_index"] is True
    assert info["valid_height_fraction"] == 1
    assert {"height_m", "height_var", "height_status"} <= set(info["fields"])
    assert info["geometry_health"] == {
        "status": "not_fully_checked", "feature_count": 1, "null": 1,
        "empty": None, "invalid": None, "unsupported": 0,
        "extent": [121.0, 30.0, 121.001111111, 30.001111111],
        "repair_applied": False, "method": "gpkg_schema_and_null_scan",
        "topology_checked": False,
    }


def test_building_grid_schema_and_direct_l8_bounds_mapping(tmp_path):
    path = tmp_path / "grid.gpkg"; _gpkg(path, "building_grid")
    result = BuildingGridService().map(_grid(), path)
    assert result["status"] == "passed"
    assert result["metadata"]["mapping"] == "exact_bounds_not_grid_key"
    assert result["cells"]["G1"] == {
        "status": "passed", "building_count": 2, "building_area_m2": 30.0,
        "building_coverage_ratio": .25, "height_mean_m": 12.0,
        "height_p95_m": 18.0, "height_max_m": 20.0,
        "valid_height_fraction": 1.0, "building_exposure": .25,
    }


def test_building_grid_non_l8_is_unsupported_and_not_resampled(tmp_path):
    path = tmp_path / "grid.gpkg"; _gpkg(path, "building_grid")
    result = BuildingGridService().map(_grid(7), path)
    assert result["status"] == "unsupported"
    assert result["cells"]["G1"]["reason"] == "unsupported_grid_level"
    assert "插值" in result["message"]


def test_building_grid_outside_extent_is_missing_not_zero(tmp_path):
    path = tmp_path / "grid.gpkg"; _gpkg(path, "building_grid")
    result = BuildingGridService().map(_grid(outside=True), path)
    assert result["status"] == "missing_data"
    assert result["cells"]["G1"]["building_count"] is None
    assert result["cells"]["G1"]["reason"] == "outside_coverage"


def test_dtm_mask_median_nodata_and_negative_elevation_are_distinct():
    values = [[-3.0, -9999.0, 5.0], [7.0, 9.0, 11.0]]
    mask = [[1, 1, 0], [1, 0, 0]]
    valid = DtmFootprintSampler._valid_masked_values(values, mask, -9999.0)
    assert valid == [-3.0, 7.0]
    assert sum(valid) / len(valid) == 2.0
    assert DtmFootprintSampler._valid_masked_values([[-9999.0]], [[1]], -9999.0) == []


class FakeAdapter:
    def __init__(self, *, height=15.0, dtm_status="passed", interval=(10.0, 30.0), horizontal=2.0):
        self.height, self.dtm_status = height, dtm_status
        self.interval, self.horizontal = interval, horizontal

    def provenance(self):
        return {
            "terrain_dtm": {"source": "FABDEM", "vertical_reference": "EGM2008_orthometric"},
            "buildings": {"source": "GBA", "query": "RTree"},
            "roof_formula": "FABDEM_DTM_footprint_median + GBA_height_m",
            "forbidden_source": "GLO30_DSM_not_used_for_roof",
        }

    def route_candidates(self, route, horizontal, policy):
        start, end = self.interval
        return {"status": "passed", "query": {"spatial_index_required": True}, "candidates": [{
            "building_id": "B1", "source": "GBA", "height_m": self.height,
            "height_var": 2.5, "horizontal_minimum_m": self.horizontal,
            "terrain": {
                "dtm_status": self.dtm_status,
                "ground_elevation_median_m": 0.0 if self.dtm_status == "passed" else None,
                "ground_elevation_min_m": -1.0, "ground_elevation_max_m": 1.0,
                "ground_relief_m": 2.0, "dtm_valid_pixel_count": 8,
            },
            "affected_intervals": [{
                "start_distance_m": start, "end_distance_m": end,
                "closest_distance_along_route_m": (start + end) / 2,
                "path": [[121.0, 30.0], [121.0002, 30.0]],
            }],
            "geometry": {"type": "Polygon", "coordinates": []},
            "evidence": {"geometry_method": "polygon_buffer_route_intersection"},
        }]}

    def route_surface_elevation(self, route, distance):
        return 0.0


def _route_inputs(reference="egm2008_orthometric", altitude=20.0):
    route = {"route_id": "R1", "status": "passed", "path": [[121.0, 30.0], [121.001, 30.0]]}
    spatial = {"route_altitude_profiles": {"R1": {
        "route_id": "R1", "mode": "constant", "constant_altitude_m": altitude,
        "vertical_reference": reference, "confirmed": True, "status": "confirmed",
    }}}
    return [route], spatial


def _policy(confirmed=True):
    return normalize_building_clearance_policy({
        "horizontal_clearance_m": 10, "vertical_clearance_m": 10,
        "source": "project engineering basis", "confirmed": confirmed,
    })


def test_resolved_vertical_breach_and_continuous_segment_with_polygon_evidence():
    routes, spatial = _route_inputs()
    result = BuildingClearanceV1().evaluate(routes, spatial, _policy(), FakeAdapter())
    assert result["status"] == "failed"
    segment = result["breach_segments"][0]
    assert segment["vertical_minimum_m"] == 5
    assert segment["start_distance_m"] == 10
    assert segment["end_distance_m"] == 30
    assert segment["breach_length_m"] == 20
    assert segment["evidence"]["geometry_method"] == "polygon_buffer_route_intersection"


def test_missing_height_and_dtm_nodata_are_unknown_never_safe():
    routes, spatial = _route_inputs()
    missing_height = BuildingClearanceV1().evaluate(routes, spatial, _policy(), FakeAdapter(height=None))
    missing_dtm = BuildingClearanceV1().evaluate(routes, spatial, _policy(), FakeAdapter(dtm_status="unresolved"))
    assert missing_height["status"] == "unresolved"
    assert missing_dtm["status"] == "unresolved"
    assert missing_height["routes"][0]["buildings"][0]["vertical_status"] == "unresolved"
    assert missing_dtm["statistics"]["safe_route_count"] == 0


def test_unconfirmed_policy_never_outputs_confirmed_safe():
    routes, spatial = _route_inputs(altitude=100.0)
    result = BuildingClearanceV1().evaluate(routes, spatial, _policy(False), FakeAdapter(horizontal=2))
    assert result["status"] == "pending_confirmation"
    assert result["routes"][0]["status"] == "unknown"
    incomplete = normalize_building_clearance_policy({
        "horizontal_clearance_m": 10, "vertical_clearance_m": 10,
        "confirmed": True, "source": "",
    })
    assert incomplete["status"] == "pending_confirmation"


def test_agl_uses_route_surface_reference_and_glo30_dsm_is_excluded():
    routes, spatial = _route_inputs("agl", 20.0)
    result = BuildingClearanceV1().evaluate(routes, spatial, _policy(), FakeAdapter())
    assert result["breach_segments"][0]["vertical_minimum_m"] == 5
    assert result["provenance"]["forbidden_source"] == "GLO30_DSM_not_used_for_roof"
    assert result["provenance"]["roof_formula"].startswith("FABDEM_DTM")


def test_source_health_exposes_dtm_schema_and_vector_content_checks():
    metadata = {
        "paths": {"terrain_dtm": "dtm.tif", "buildings": "b.gpkg", "building_grid": "g.gpkg"},
        "terrain_dtm": {"width": 10, "height": 10, "bands": 1, "crs": "EPSG:4326", "nodata": -9999, "dtype": "Float32", "extent": [0, 0, 1, 1], "vertical_status": "confirmed"},
        "vector_sources": {
            "buildings": {"status": "passed", "layer": "buildings", "feature_count": 1, "crs": "EPSG:4326", "geometry_type": "MULTIPOLYGON", "fields": {"height_m": "REAL"}, "spatial_index": True},
            "building_grid": {"status": "passed", "layer": "building_grid_L8", "feature_count": 1, "crs": "EPSG:4326", "geometry_type": "POLYGON", "fields": {"grid_level": "INTEGER"}, "spatial_index": True},
        },
    }
    items = {item["id"]: item for item in build_health(metadata)["items"]}
    assert items["terrain_dtm"]["status"] == "ready"
    assert items["buildings"]["status"] == "ready"
    assert items["building_grid"]["status"] == "ready"


def test_policy_invalidation_and_project_round_trip(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    workflow.set_building_clearance_policy(_policy())
    workflow.state["building_clearance_assessment"] = BuildingClearanceV1.empty("passed")
    workflow.state["result_statuses"]["building_clearance"] = "passed"
    workflow.invalidate_grid_attributes({"terrain_dtm"})
    assert workflow.state["building_clearance_assessment"]["status"] == "stale"
    workflow.save()
    restored = WorkflowService(path, DEFAULTS)
    assert restored.state["building_clearance_policy"]["confirmed"] is True
    assert restored.state["building_clearance_assessment"]["status"] == "stale"


@pytest.mark.parametrize("changed", ["workspace", "route", "route_algorithm", "spatial_3d"])
def test_workspace_route_and_spatial_changes_stale_clearance(tmp_path, changed):
    workflow = WorkflowService(tmp_path / f"{changed}.json", DEFAULTS)
    workflow.state["building_clearance_assessment"] = BuildingClearanceV1.empty("passed")
    workflow.state["result_statuses"]["building_clearance"] = "passed"
    workflow.invalidation_service.workflow(changed)
    assert workflow.state["building_clearance_assessment"]["status"] == "stale"


class _Qgis:
    @staticmethod
    def call(callback): return callback()


class _Context:
    def __init__(self, workflow, adapter):
        self.workflow, self.adapter = workflow, adapter
        self.data = type("Data", (), {"error": None})()
        self.qgis = _Qgis()

    def evaluate_building_clearance(self):
        return self.workflow.evaluate_building_clearance(self.adapter)


def test_building_clearance_api_and_end_to_end_state(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    routes, spatial = _route_inputs()
    workflow.state["operational_routes"] = routes
    workflow.state["spatial_3d"] = spatial
    router = ApiRouter(_Context(workflow, FakeAdapter()))
    saved = router.post("/api/building-clearance/policy", _policy()).data
    assert saved["building_clearance_policy"]["confirmed"] is True
    evaluated = router.post("/api/building-clearance/evaluate", {}).data
    assert evaluated["building_clearance_assessment"]["status"] == "failed"
    assert router.get("/api/building-clearance", {}, {}).data["breach_segments"]


def test_report_and_frontend_expose_independent_building_chain(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    model = ReportBuilder().build(workflow.state, [], "2026-09-16T00:00:00Z")
    section = model["sections"]["building_environment_and_clearance"]
    assert "FABDEM ≠ survey-grade DTM" in section["limitations"]
    web = (ROOT / "cns_planner" / "web" / "js" / "workflow" / "step02_workspace.js").read_text(encoding="utf-8")
    routes = (ROOT / "cns_planner" / "web" / "js" / "workflow" / "step03_routes.js").read_text(encoding="utf-8")
    main = (ROOT / "cns_planner" / "web" / "js" / "main.js").read_text(encoding="utf-8")
    assert all(value in web for value in ("building_density", "building_p95", "building_max"))
    assert "/api/building-clearance/evaluate" in routes
    assert "buildingClearanceLayer" in main


def test_runtime_adapter_contract_uses_polygon_not_centroid_and_rtree_query():
    source = (ROOT / "cns_planner" / "gis" / "building_clearance_adapter.py").read_text(encoding="utf-8")
    assert "setFilterRect" in source
    assert "route_metric.distance(footprint_metric)" in source
    assert "footprint_metric.buffer" in source
    assert "centroid" not in source.lower()
    assert "height_var_semantics" in source
