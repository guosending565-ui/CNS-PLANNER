from pathlib import Path

from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.services.airspace_grid_service import AirspaceGridService
from cns_planner.services.grid_service import WorkspaceGridService
from cns_planner.services.qgis_airspace_adapter import QgisAirspaceAdapter
from cns_planner.services.workflow import WorkflowService


class StubAirspaceAdapter:
    def __init__(self, mapped):
        self.mapped = mapped
        self.calls = []

    def describe(self):
        return {
            "path": "fixture.qgz",
            "layers": [{"layer_id": "airspace-1", "name": "测试空域", "crs": "EPSG:4326"}],
        }

    def intersections(self, cells, workspace_bbox):
        self.calls.append(([cell["grid_id"] for cell in cells], workspace_bbox))
        return self.mapped


class FailingAirspaceAdapter(StubAirspaceAdapter):
    def intersections(self, cells, workspace_bbox):
        raise ValueError("fixture intersection failure")


class FakeCrs:
    def __init__(self, name, scale=1.0):
        self.name = name
        self.scale = scale

    def authid(self): return self.name
    def description(self): return self.name


class FakeRectangle:
    def __init__(self, *bbox): self.bbox = tuple(bbox)


class FakeTransform:
    def __init__(self, source, target, project): self.scale = target.scale / source.scale

    def transformBoundingBox(self, rectangle):
        return FakeRectangle(*(value * self.scale for value in rectangle.bbox))


class FakeGeometry:
    def __init__(self, bbox=None): self.bbox = bbox

    @classmethod
    def fromRect(cls, rectangle): return cls(rectangle.bbox)

    def transform(self, transform): self.bbox = tuple(value * transform.scale for value in self.bbox)
    def boundingBox(self): return FakeRectangle(*self.bbox)
    def isNull(self): return self.bbox is None
    def isEmpty(self): return self.bbox is None or self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]
    def area(self): return 0.0 if self.isEmpty() else (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])
    def intersects(self, other):
        return self.bbox[0] < other.bbox[2] and self.bbox[2] > other.bbox[0] and self.bbox[1] < other.bbox[3] and self.bbox[3] > other.bbox[1]
    def intersection(self, other):
        bbox = (
            max(self.bbox[0], other.bbox[0]), max(self.bbox[1], other.bbox[1]),
            min(self.bbox[2], other.bbox[2]), min(self.bbox[3], other.bbox[3]),
        )
        return FakeGeometry(bbox)


class FakeFeature:
    def __init__(self, feature_id, bbox, attributes):
        self.feature_id = feature_id
        self.feature_geometry = FakeGeometry(bbox)
        self.attributes = attributes

    def id(self): return self.feature_id
    def geometry(self): return self.feature_geometry
    def __getitem__(self, name): return self.attributes.get(name)


class FakeField:
    def __init__(self, name): self.field_name = name
    def name(self): return self.field_name


class FakeFeatureRequest:
    def setFilterRect(self, rectangle): self.rectangle = rectangle; return self


class FakeSpatialIndex:
    query_count = 0

    def __init__(self, features): self.features = list(features)
    def intersects(self, rectangle):
        type(self).query_count += 1
        query = FakeGeometry(rectangle.bbox)
        return [feature.id() for feature in self.features if feature.geometry().intersects(query)]


class FakeLayer:
    def __init__(self, features):
        self.features = features
        self.request_bbox = None
        self.layer_crs = FakeCrs("EPSG:FAKE", 100.0)

    def id(self): return "airspace-1"
    def name(self): return "测试空域"
    def crs(self): return self.layer_crs
    def fields(self): return [FakeField("category"), FakeField("lower"), FakeField("ignored")]
    def getFeatures(self, request): self.request_bbox = request.rectangle.bbox; return iter(self.features)


def _grid():
    return WorkspaceGridService(preferred_level=6).generate(
        [120.001, 30.001, 120.02, 30.02]
    )


def _defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir()
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def _health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 1,
        "covered_layer_count": 1,
    }


def _airspace_result(grid):
    first_id, second_id = [cell["grid_id"] for cell in grid["cells"][:2]]
    mapped = {
        first_id: {
            "coverage_ratio": 1.0,
            "airspaces": [{
                "layer_id": "airspace-1",
                "name": "测试空域",
                "feature_id": 11,
                "category": "管制区",
                "type": "CTR",
                "intersection_ratio": 1.0,
                "intersection_area": 12.5,
                "source_attributes": {"lower": 300, "upper": 900},
            }],
        },
        second_id: {
            "coverage_ratio": 0.25,
            "airspaces": [{
                "layer_id": "airspace-1",
                "name": "测试空域",
                "feature_id": 12,
                "category": None,
                "type": "限制区",
                "intersection_ratio": 0.25,
                "intersection_area": 3.0,
                "source_attributes": {"高度下限": "GND"},
            }],
        },
    }
    return AirspaceGridService().map(grid, StubAirspaceAdapter(mapped))


def _workflow_attributes(service, grid):
    attributes = service.grid_attributes_snapshot()
    for kind in ("population", "terrain"):
        attributes[kind].update({
            "status": "passed",
            "grid_level": grid["level"],
            "count": grid["count"],
            "cells": {cell["grid_id"]: {} for cell in grid["cells"]},
        })
    attributes["airspace"] = _airspace_result(grid)
    return attributes


def test_airspace_maps_full_partial_and_no_coverage_by_grid_id_without_geometry():
    grid = _grid()
    first_id, second_id, third_id = [cell["grid_id"] for cell in grid["cells"][:3]]
    mapped = {
        first_id: {
            "coverage_ratio": 1.0,
            "airspaces": [{
                "layer_id": "airspace-1", "name": "测试空域", "feature_id": 11,
                "category": "管制区", "type": "CTR", "intersection_ratio": 1.0,
                "intersection_area": 12.5, "source_attributes": {"lower": 300},
            }],
        },
        second_id: {
            "coverage_ratio": 0.25,
            "airspaces": [{
                "layer_id": "airspace-1", "name": "测试空域", "feature_id": 12,
                "category": None, "type": "限制区", "intersection_ratio": 0.25,
                "intersection_area": 3.0, "source_attributes": {},
            }],
        },
    }
    adapter = StubAirspaceAdapter(mapped)

    result = AirspaceGridService().map(grid, adapter)

    assert result["status"] == "passed"
    assert result["algorithm_id"] == "airspace-grid-intersection"
    assert result["algorithm_version"] == "1.0"
    assert result["count"] == grid["count"]
    assert result["hit_count"] == 2
    assert result["cells"][first_id]["status"] == "full_coverage"
    assert result["cells"][second_id]["status"] == "partial_intersection"
    assert result["cells"][third_id] == {
        "status": "no_coverage",
        "intersected_layer_count": 0,
        "coverage_ratio": 0.0,
        "airspaces": [],
    }
    assert result["cells"][first_id]["airspaces"][0]["source_attributes"] == {"lower": 300}
    assert all("geometry" not in value for value in result["cells"].values())
    assert adapter.calls == [([cell["grid_id"] for cell in grid["cells"]], grid["workspace_bbox"])]


def test_qgis_adapter_transforms_crs_filters_workspace_and_uses_spatial_index():
    layer = FakeLayer([
        FakeFeature(11, (12000.0, 3000.0, 12001.0, 3001.0), {"category": "CTR", "lower": 300, "ignored": "drop"}),
        FakeFeature(99, (13000.0, 4000.0, 13001.0, 4001.0), {"category": "far"}),
    ])
    adapter = object.__new__(QgisAirspaceAdapter)
    adapter.layers = [layer]
    adapter.project = object()
    adapter.source_path = "fixture.qgz"
    adapter.QgsCoordinateReferenceSystem = lambda name: FakeCrs(name)
    adapter.QgsCoordinateTransform = FakeTransform
    adapter.QgsFeatureRequest = FakeFeatureRequest
    adapter.QgsGeometry = FakeGeometry
    adapter.QgsRectangle = FakeRectangle
    adapter.QgsSpatialIndex = FakeSpatialIndex
    cells = [
        {"grid_id": "cell-1", "bbox": [120.0, 30.0, 120.01, 30.01]},
        {"grid_id": "cell-2", "bbox": [120.01, 30.0, 120.02, 30.01]},
    ]
    FakeSpatialIndex.query_count = 0

    result = adapter.intersections(cells, [120.0, 30.0, 120.02, 30.01])

    assert layer.request_bbox == (12000.0, 3000.0, 12002.0, 3001.0)
    assert FakeSpatialIndex.query_count == len(cells)
    assert result["cell-1"]["coverage_ratio"] == 1.0
    assert result["cell-1"]["airspaces"][0]["feature_id"].startswith("ASF-")
    assert result["cell-1"]["airspaces"][0]["source_feature_id"] == 11
    assert result["cell-1"]["airspaces"][0]["category"] == "CTR"
    assert result["cell-1"]["airspaces"][0]["source_attributes"] == {"category": "CTR", "lower": 300}
    assert result["cell-2"] == {"airspaces": [], "coverage_ratio": 0.0}


def test_airspace_mapping_failure_is_not_reported_as_no_coverage():
    grid = _grid()

    result = AirspaceGridService().map(grid, FailingAirspaceAdapter({}))

    assert result["status"] == "failed"
    assert result["message"] == "fixture intersection failure"
    assert {cell["status"] for cell in result["cells"].values()} == {"failed"}
    assert all(cell["coverage_ratio"] is None for cell in result["cells"].values())


def test_airspace_save_restore_workspace_reset_and_old_project_compatibility(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    defaults = _defaults_path(tmp_path)
    service = WorkflowService(store, defaults)
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    attributes = _workflow_attributes(service, state["grid"])
    service.apply_grid_attributes(attributes)

    restored = WorkflowService(store, defaults)
    assert restored.grid_attributes_snapshot()["airspace"] == attributes["airspace"]

    changed = restored.set_workspace([120.021, 30.021, 120.03, 30.03], _health())
    assert changed["grid_attributes"]["airspace"]["status"] == "not_calculated"
    assert changed["grid_attributes"]["airspace"]["cells"] == {}

    document = ProjectRepository(store).load()
    document["grid_attributes"].pop("airspace")
    ProjectRepository(store).save(document)
    legacy = WorkflowService(store, defaults)
    assert legacy.state["workspace"] == document["workspace"]
    assert legacy.grid_attributes_snapshot()["airspace"]["status"] == "not_calculated"
    assert legacy.state["reference_routes"]["items"] == []
    assert legacy.state["airspace_policies"]["status"] == "pending_confirmation"
    assert legacy.grid_attributes_snapshot()["airspace"]["airspace_eligibility"]["status"] == "not_calculated"


def test_basemap_change_is_display_only_and_does_not_stale_grid_attributes(tmp_path):
    service = WorkflowService(tmp_path / "project.json", _defaults_path(tmp_path))
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    attributes = _workflow_attributes(service, state["grid"])
    service.apply_grid_attributes(attributes)

    service.invalidate_grid_attributes({"basemap"})
    service.save()

    assert service.state["grid_attributes"]["airspace"]["status"] == "passed"
    assert service.state["grid_attributes"]["population"]["status"] == "passed"
    assert service.state["grid_attributes"]["terrain"]["status"] == "passed"
    persisted = WorkflowService(service.store_path, service.defaults_path)
    assert persisted.state["grid_attributes"]["airspace"]["status"] == "passed"
