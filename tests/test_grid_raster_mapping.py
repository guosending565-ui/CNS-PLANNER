from copy import deepcopy
from pathlib import Path

from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.services.airspace_grid_service import AirspaceGridService
from cns_planner.services.conflict_grid_service import ConflictGridService
from cns_planner.services.grid_service import WorkspaceGridService
from cns_planner.services.population_grid_service import PopulationGridService
from cns_planner.services.raster_grid_adapter import GdalRasterAdapter
from cns_planner.services.terrain_grid_service import TerrainGridService
from cns_planner.services.traffic_grid_service import TrafficGridService
from cns_planner.services.workflow import WorkflowService


class StubRasterAdapter:
    def __init__(self, values_by_bbox, unit=None, crs="EPSG:4326"):
        self.values_by_bbox = values_by_bbox
        self.unit = unit
        self.crs = crs

    def describe(self):
        return {
            "path": "fixture.tif",
            "crs": self.crs,
            "band": 1,
            "nodata": -9999,
            "unit_metadata": self.unit,
            "scale": 1.0,
            "offset": 0.0,
        }

    def read_values(self, bbox):
        key = tuple(round(value, 8) for value in bbox)
        return list(self.values_by_bbox.get(key, []))


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
        "loaded_layer_count": 3,
        "covered_layer_count": 3,
    }


def _mapped_results(grid):
    values = {
        tuple(round(value, 8) for value in cell["bbox"]): [1.0, 2.0]
        for cell in grid["cells"]
    }
    population = PopulationGridService(lambda _: StubRasterAdapter(values)).map(grid, "population.tif")
    terrain = TerrainGridService(lambda _: StubRasterAdapter(values, "m")).map(grid, "terrain.tif")
    airspace = AirspaceGridService.empty("passed", {"path": "airspace.qgz", "layers": []})
    airspace.update({
        "grid_level": grid["level"],
        "count": grid["count"],
        "cells": {
            cell["grid_id"]: {
                "status": "no_coverage",
                "intersected_layer_count": 0,
                "coverage_ratio": 0.0,
                "airspaces": [],
            }
            for cell in grid["cells"]
        },
    })
    extensions = {
        name: {
            "status": "not_calculated",
            "source": None,
            "algorithm_id": None,
            "algorithm_version": None,
            "namespace": name,
            "grid_level": None,
            "count": 0,
            "cells": {},
        }
        for name in ("buildings", "property_exposure", "infrastructure", "towers")
    }
    return {
        "population": population, "terrain": terrain, "airspace": airspace,
        "traffic": TrafficGridService.empty(),
        "conflict": ConflictGridService.empty(),
        **extensions,
    }


def test_population_maps_raw_source_statistics_by_grid_id_without_geometry_copy():
    grid = _grid()
    original = deepcopy(grid)
    first_bbox = tuple(round(value, 8) for value in grid["cells"][0]["bbox"])
    adapter = StubRasterAdapter({first_bbox: [2.0, 3.5]}, unit=None)

    result = PopulationGridService(lambda _: adapter).map(grid, "population.tif")

    first_id = grid["cells"][0]["grid_id"]
    assert result["status"] == "missing_data"
    assert result["count"] == 4
    assert result["covered_count"] == 1
    assert set(result["cells"]) == {cell["grid_id"] for cell in grid["cells"]}
    assert result["cells"][first_id] == {
        "status": "passed",
        "valid_sample_count": 2,
        "value_sum": 5.5,
        "value_mean": 2.75,
        "value_min": 2.0,
        "value_max": 3.5,
    }
    assert result["value_unit"] is None
    assert result["unit_status"] == "unverified"
    assert result["interpretation"] == "source_values_only"
    assert grid == original
    assert all("geometry" not in attributes for attributes in result["cells"].values())


def test_terrain_maps_elevation_statistics_and_records_no_coverage():
    grid = _grid()
    first_bbox = tuple(round(value, 8) for value in grid["cells"][0]["bbox"])
    adapter = StubRasterAdapter({first_bbox: [10.0, 20.0, 30.0]}, unit="m")

    result = TerrainGridService(lambda _: adapter).map(grid, "terrain.tif")

    first_id, missing_id = grid["cells"][0]["grid_id"], grid["cells"][1]["grid_id"]
    assert result["cells"][first_id] == {
        "status": "passed",
        "valid_sample_count": 3,
        "mean_elevation": 20.0,
        "min_elevation": 10.0,
        "max_elevation": 30.0,
    }
    assert result["cells"][missing_id]["status"] == "missing_data"
    assert result["cells"][missing_id]["valid_sample_count"] == 0
    assert result["cells"][missing_id]["mean_elevation"] is None
    assert result["elevation_unit"] == "m"


class FakeBand:
    def __init__(self):
        self.values = [[1.0, -9999.0, 3.0], [4.0, 5.0, float("nan")]]

    def GetNoDataValue(self): return -9999.0
    def GetScale(self): return 2.0
    def GetOffset(self): return 10.0
    def GetUnitType(self): return "source-unit"
    def ReadAsArray(self, x, y, width, height):
        return [row[x:x + width] for row in self.values[y:y + height]]


class FakeDataset:
    RasterCount = 1
    RasterXSize = 3
    RasterYSize = 2

    def __init__(self): self.band = FakeBand()
    def GetRasterBand(self, _): return self.band
    def GetGeoTransform(self): return (0.0, 1000.0, 0.0, 2000.0, 0.0, -1000.0)
    def GetProjection(self): return "FAKE:SHIFTED"


class FakeGdal:
    GA_ReadOnly = 0

    @staticmethod
    def Open(path, mode): return FakeDataset()
    @staticmethod
    def InvGeoTransform(_): return (0.0, 0.001, 0.0, 2.0, 0.0, -0.001)


class FakeSpatialReference:
    def __init__(self): self.name = None
    def ImportFromWkt(self, value): self.name = value; return 0
    def ImportFromEPSG(self, value): self.name = f"EPSG:{value}"; return 0
    def SetAxisMappingStrategy(self, value): pass
    def GetAuthorityName(self, _): return "FAKE"
    def GetAuthorityCode(self, _): return "1000"


class FakeCoordinateTransformation:
    def __init__(self, source, target):
        self.forward = source.name == "EPSG:4326"

    def TransformPoints(self, points):
        factor = 1000.0 if self.forward else 0.001
        return [(point[0] * factor, point[1] * factor, 0.0) for point in points]


class FakeOsr:
    OAMS_TRADITIONAL_GIS_ORDER = 0
    SpatialReference = FakeSpatialReference
    CoordinateTransformation = FakeCoordinateTransformation


def test_gdal_adapter_transforms_crs_filters_nodata_and_uses_half_open_boundaries():
    adapter = GdalRasterAdapter("shifted.tif", FakeGdal, FakeOsr)

    assert adapter.read_values([0.0, 0.0, 3.0, 2.0]) == [12.0, 16.0, 18.0, 20.0]
    assert adapter.read_values([0.0, 1.0, 1.0, 2.0]) == [12.0]
    assert adapter.read_values([5.0, 5.0, 6.0, 6.0]) == []
    assert adapter.describe()["crs"] == "FAKE:1000"


def test_grid_attributes_save_restore_workspace_reset_and_legacy_compatibility(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    defaults = _defaults_path(tmp_path)
    service = WorkflowService(store, defaults)
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    mapped = _mapped_results(state["grid"])
    service.apply_grid_attributes(mapped)

    restored = WorkflowService(store, defaults)
    assert restored.grid_attributes_snapshot() == mapped

    changed = restored.set_workspace([120.021, 30.021, 120.03, 30.03], _health())
    assert changed["grid_attributes"]["population"]["status"] == "not_calculated"
    assert changed["grid_attributes"]["terrain"]["status"] == "not_calculated"
    assert changed["grid_attributes"]["airspace"]["status"] == "not_calculated"
    assert changed["grid_attributes"]["population"]["cells"] == {}

    document = ProjectRepository(store).load()
    document.pop("grid_attributes")
    ProjectRepository(store).save(document)
    legacy = WorkflowService(store, defaults)
    assert legacy.state["workspace"] == document["workspace"]
    assert legacy.grid_attributes_snapshot()["population"]["status"] == "not_calculated"
    assert legacy.grid_attributes_snapshot()["airspace"]["status"] == "not_calculated"


def test_population_and_terrain_source_changes_invalidate_only_matching_attributes(tmp_path):
    service = WorkflowService(tmp_path / "project.json", _defaults_path(tmp_path))
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    mapped = _mapped_results(state["grid"])
    service.apply_grid_attributes(mapped)

    service.invalidate_grid_attributes({"population"})
    service.save()
    assert service.state["grid_attributes"]["population"]["status"] == "stale"
    assert service.state["grid_attributes"]["terrain"]["status"] == "passed"
    assert WorkflowService(service.store_path, service.defaults_path).state["grid_attributes"]["population"]["status"] == "stale"

    service.state["grid_attributes"] = deepcopy(mapped)
    service.invalidate_grid_attributes({"terrain"})
    service.save()
    assert service.state["grid_attributes"]["population"]["status"] == "passed"
    assert service.state["grid_attributes"]["terrain"]["status"] == "stale"
