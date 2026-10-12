from copy import deepcopy
import sys
from types import SimpleNamespace
from types import ModuleType

import pytest

from cns_planner.gis.planning_constraint_field_adapter import (
    restricted_area_continuous_evidence,
)
from cns_planner.gis.restricted_area_input_adapter import restricted_area_dataset_inputs
from cns_planner.validation.continuous_validators import MetricRoute, validate_restricted_areas


VREF = "egm2008_orthometric"


class IdentityTransform:
    def __init__(self, authority="EPSG:32651"):
        self.authority = authority

    def to_metric(self, point):
        return [float(point[0]), float(point[1])]

    def to_geographic(self, point):
        return [float(point[0]), float(point[1])]

    def describe(self):
        return {"horizontal_crs": self.authority}


def declarations(*, complete=True):
    return {
        domain: {
            "confirmed_none": True,
            "source": f"{domain}-engineering-declaration" if complete else None,
            "evidence": [{"record": f"{domain}-scope", "applicability": "current_project"}],
        }
        for domain in ("airspace", "critical_site")
    }


def polygon_area():
    geometry = {
        "type": "Polygon",
        "coordinates": [[[40, -5], [60, -5], [60, 5], [40, 5], [40, -5]]],
    }
    return {
        "feature_id": "RA-1",
        "name": "audited polygon",
        "category": "regulatory",
        "domain": "airspace",
        "geometry": geometry,
        "protection_geometry": geometry,
        "geometry_crs": "EPSG:32651",
        "constraint_type": "hard_exclusion",
        "lower_altitude_m": 0.0,
        "upper_altitude_m": 200.0,
        "vertical_reference": VREF,
        "confirmed": True,
        "source": "regulator-dataset",
        "evidence": [{"record": "RA-1"}],
    }


def resolved_collection(items=()):
    return {
        "source": "audited-restricted-area-register",
        "evidence": [{"record": "register-2026"}],
        "domain_states": {
            "airspace": {"status": "confirmed_present" if items else "confirmed_none"},
            "critical_site": {"status": "confirmed_none"},
        },
        "items": list(items),
    }


def metric_route():
    return MetricRoute({
        "horizontal_geometry": {"linearized": {
            "linestring_metric": [[0.0, 0.0], [100.0, 0.0]],
            "curve_chord_error_m": 0.0,
        }},
        "primitives": [{
            "primitive_id": "s0",
            "kind": "line",
            "distance_start_m": 0.0,
            "distance_end_m": 100.0,
            "horizontal_length_m": 100.0,
            "z_start_egm2008_m": 100.0,
            "z_end_egm2008_m": 100.0,
        }],
    })


def test_persisted_confirmed_none_becomes_resolved_empty_continuous_evidence():
    merged = restricted_area_dataset_inputs({
        "restricted_area_declarations": declarations(),
    })
    evidence = restricted_area_continuous_evidence(merged, transform=IdentityTransform())

    assert evidence["status"] == "passed"
    assert evidence["applicability"] == "applicable"
    assert evidence["areas"] == []
    assert evidence["scope"]["domains"] == ["airspace", "critical_site"]
    assert evidence["source"]["domain_states"]["airspace"]["source"] == (
        "airspace-engineering-declaration"
    )
    assert evidence["source"]["domain_states"]["critical_site"]["evidence"]


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"restricted_area_declarations": declarations(complete=False)},
        {"restricted_areas": {"domain_states": {
            "airspace": {"status": "not_configured"},
            "critical_site": {"status": "not_configured"},
        }}},
    ],
)
def test_missing_not_configured_or_incomplete_authority_stays_unresolved(state):
    merged = restricted_area_dataset_inputs(state)
    evidence = restricted_area_continuous_evidence(merged, transform=IdentityTransform())

    assert evidence["status"] == "unresolved"
    assert evidence["applicability"] == "applicable"


def test_minimum_engineering_declaration_keeps_non_regulatory_limitation_disclosed():
    declared = declarations()
    for entry in declared.values():
        entry["source"] = "screenshot_run_minimum_engineering_configuration"
    merged = restricted_area_dataset_inputs({"restricted_area_declarations": declared})
    evidence = restricted_area_continuous_evidence(merged, transform=IdentityTransform())

    assert evidence["status"] == "passed"
    assert "engineering_declaration_not_regulatory_confirmation" in evidence["limitations"]


def test_payload_polygon_conflicting_with_persisted_confirmed_none_fails_closed():
    merged = restricted_area_dataset_inputs(
        {"restricted_area_declarations": declarations()},
        payload={"restricted_areas": resolved_collection([polygon_area()])},
    )
    evidence = restricted_area_continuous_evidence(merged, transform=IdentityTransform())

    assert merged["conflicts"]
    assert merged["domain_states"]["airspace"]["status"] == "unresolved"
    assert evidence["status"] == "unresolved"


def test_confirmed_present_polygon_still_runs_real_geometry_validation():
    merged = restricted_area_dataset_inputs({
        "restricted_areas": resolved_collection([polygon_area()]),
    })
    evidence = restricted_area_continuous_evidence(merged, transform=IdentityTransform())
    result = validate_restricted_areas(
        metric_route(),
        evidence=evidence,
        policy={},
        vertical_reference=VREF,
    )

    assert evidence["status"] == "passed"
    assert result["status"] == "failed"
    assert result["violations"][0]["evidence"]["feature_id"] == "RA-1"


def test_production_runtime_consumes_persisted_declarations_when_payload_omits_areas(monkeypatch):
    class QgisPlaceholder:
        pass

    def placeholder_module(name):
        module = ModuleType(name)
        module.__getattr__ = lambda attribute: QgisPlaceholder
        return module

    qgis = placeholder_module("qgis")
    qgis.__path__ = []
    pyqt = placeholder_module("qgis.PyQt")
    pyqt.__path__ = []
    monkeypatch.setitem(sys.modules, "qgis", qgis)
    monkeypatch.setitem(sys.modules, "qgis.core", placeholder_module("qgis.core"))
    monkeypatch.setitem(sys.modules, "qgis.PyQt", pyqt)
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtCore", placeholder_module("qgis.PyQt.QtCore"))
    monkeypatch.setitem(sys.modules, "qgis.PyQt.QtGui", placeholder_module("qgis.PyQt.QtGui"))
    osgeo = placeholder_module("osgeo")
    osgeo.__path__ = []
    gdal = placeholder_module("osgeo.gdal")
    gdal.UseExceptions = lambda: None
    ogr = placeholder_module("osgeo.ogr")
    osr = placeholder_module("osgeo.osr")
    osgeo.gdal = gdal
    osgeo.ogr = ogr
    osgeo.osr = osr
    monkeypatch.setitem(sys.modules, "osgeo", osgeo)
    monkeypatch.setitem(sys.modules, "osgeo.gdal", gdal)
    monkeypatch.setitem(sys.modules, "osgeo.ogr", ogr)
    monkeypatch.setitem(sys.modules, "osgeo.osr", osr)

    from cns_planner.application.app_context import ApplicationContext
    import cns_planner.gis.fine_environment_adapter as fine
    import cns_planner.route_planner_v3.continuous_raster_window as raster_window

    class Terrain:
        def __init__(self, path):
            self.path = path

        def native_window(self, **kwargs):
            return {"available": True, "source": {"path": self.path}, "pixels": []}

        def describe(self):
            return {"path": self.path}

    class Buildings:
        def __init__(self, path, **kwargs):
            self.path = path

        def query_route(self, *args, **kwargs):
            return {"available": True, "source": {"path": self.path}, "buildings": []}

        def describe(self):
            return {"path": self.path}

    monkeypatch.setattr(fine, "QgisMetricTransform", IdentityTransform)
    monkeypatch.setattr(fine, "NativeTerrainWindowSource", Terrain)
    monkeypatch.setattr(fine, "RouteCorridorBuildingSource", Buildings)
    monkeypatch.setattr(raster_window, "resolve_native_pixel_intervals", lambda route, pixels, **kw: pixels)

    context = ApplicationContext.__new__(ApplicationContext)
    context.data = SimpleNamespace(paths={"terrain_dtm": "terrain.tif", "buildings": "buildings.gpkg"})
    context.workflow = SimpleNamespace(session=SimpleNamespace(state={
        "restricted_area_declarations": declarations(),
        "tower_obstacle_profiles": {"status": "passed", "items": {}},
    }))
    result = context._layered_route_validation_evidence(
        candidate={"candidate_id": "LC-1"},
        path=[[0.0, 0.0], [100.0, 0.0]],
        altitude_layer={"altitude_layer_id": "ALT-100"},
        nominal_altitude_m=100.0,
        policy={"building_horizontal_clearance_m": 5.0},
        payload={"horizontal_crs": "EPSG:32651"},
    )

    assert result["adapter_id"] == "layered_candidate_real_source_validation_adapter_v2"
    assert result["restricted_areas"]["status"] == "passed"
    assert result["restricted_areas"]["areas"] == []
    assert result["sources"]["restricted_areas"]["domain_states"]["airspace"]["source"]
