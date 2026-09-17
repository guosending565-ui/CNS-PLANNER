from copy import deepcopy

import pytest

from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.data.mapping.population import PopulationGridService
from cns_planner.data.mapping.terrain import TerrainGridService
from cns_planner.data.registry import build_registry
from cns_planner.data.source_profiles import COPERNICUS_GLO30, WORLDPOP_R2025A
from cns_planner.domain.provenance import SOURCE_TYPES, source_profile
from cns_planner.domain.quantities import geographic_bbox_area_m2, quantity_value
from cns_planner.gis.raster_adapter import GdalRasterAdapter
from cns_planner.persistence.project_repository import ProjectRepository


class IdentityBand:
    def __init__(self, values, nodata=-9999.0):
        self.values, self.nodata = values, nodata

    def GetNoDataValue(self): return self.nodata
    def GetScale(self): return None
    def GetOffset(self): return None
    def GetUnitType(self): return "person/source_pixel"
    def ReadAsArray(self, x, y, width, height):
        return [row[x:x + width] for row in self.values[y:y + height]]


class IdentityDataset:
    RasterCount = 1
    RasterXSize = 2
    RasterYSize = 1

    def __init__(self, values): self.band = IdentityBand(values)
    def GetRasterBand(self, _): return self.band
    def GetGeoTransform(self): return (0.0, 1.0, 0.0, 1.0, 0.0, -1.0)
    def GetProjection(self): return "EPSG:4326"


class IdentityGdal:
    GA_ReadOnly = 0
    values = [[10.0, 20.0]]

    @classmethod
    def Open(cls, path, mode): return IdentityDataset(cls.values)
    @staticmethod
    def InvGeoTransform(_): return (0.0, 1.0, 0.0, 1.0, 0.0, -1.0)


class IdentitySpatialReference:
    def __init__(self): self.name = None
    def ImportFromWkt(self, value): self.name = value; return 0
    def ImportFromEPSG(self, value): self.name = f"EPSG:{value}"; return 0
    def SetAxisMappingStrategy(self, value): pass
    def GetAuthorityName(self, _): return "EPSG"
    def GetAuthorityCode(self, _): return "4326"


class IdentityTransformation:
    def __init__(self, source, target): pass
    def TransformPoints(self, points): return [(point[0], point[1], 0.0) for point in points]


class IdentityOsr:
    OAMS_TRADITIONAL_GIS_ORDER = 0
    SpatialReference = IdentitySpatialReference
    CoordinateTransformation = IdentityTransformation


def test_quantity_and_source_profile_contracts_are_json_safe_and_explicit():
    assert SOURCE_TYPES == ("real", "synthetic", "manual")
    assert quantity_value(None, "population_count", "person")["status"] == "missing_data"
    assert WORLDPOP_R2025A["quantity"] == "population_count_per_source_pixel"
    assert WORLDPOP_R2025A["unit"] == "person/source_pixel"
    assert WORLDPOP_R2025A["provenance"]["product_status"] == "alpha"
    assert COPERNICUS_GLO30["provenance"]["surface_model"] == "DSM"
    assert COPERNICUS_GLO30["crs"]["horizontal"] == "EPSG:4326"
    assert COPERNICUS_GLO30["crs"]["vertical"] == "EPSG:3855"
    with pytest.raises(ValueError):
        source_profile({**WORLDPOP_R2025A, "source_type": "unknown"})


def test_wgs84_grid_area_is_latitude_dependent():
    equator = geographic_bbox_area_m2([0, 0, 1, 1])
    high_latitude = geographic_bbox_area_m2([0, 60, 1, 61])
    assert equator > high_latitude > 0


def test_population_count_mapping_conserves_people_with_area_weights():
    IdentityGdal.values = [[10.0, 20.0]]
    adapter = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr)
    complete = adapter.read_population_count([0.0, 0.0, 2.0, 1.0])
    middle = adapter.read_population_count([0.5, 0.0, 1.5, 1.0])
    assert complete["status"] == "passed"
    assert complete["population_count_people"] == pytest.approx(30.0)
    assert middle["status"] == "passed"
    assert middle["population_count_people"] == pytest.approx(15.0)
    assert middle["source_coverage_fraction"] == pytest.approx(1.0)


def test_population_nodata_is_missing_not_zero_or_passed():
    IdentityGdal.values = [[10.0, -9999.0]]
    adapter = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr)
    result = adapter.read_population_count([0.0, 0.0, 2.0, 1.0])
    assert result["status"] == "missing_data"
    assert result["value_status"] == "passed"
    assert result["coverage_status"] == "partial"
    assert result["population_count_people"] == pytest.approx(10.0)
    assert result["source_coverage_fraction"] < 1.0
    assert result["quality_flags"] == ["partial_source_coverage", "not_extrapolated"]


def test_population_valid_zero_nodata_only_and_outside_extent_are_distinct():
    IdentityGdal.values = [[0.0, 0.0]]
    adapter = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr)
    zero = adapter.read_population_count([0.0, 0.0, 2.0, 1.0])
    assert zero["value_status"] == "passed"
    assert zero["coverage_status"] == "full"
    assert zero["population_count_people"] == 0.0

    IdentityGdal.values = [[-9999.0, -9999.0]]
    nodata = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr).read_population_count([0.0, 0.0, 2.0, 1.0])
    assert nodata["value_status"] == "missing_data"
    assert nodata["coverage_status"] == "nodata_only"
    assert nodata["population_count_people"] is None

    outside = adapter.read_population_count([5.0, 5.0, 6.0, 6.0])
    assert outside["value_status"] == "missing_data"
    assert outside["coverage_status"] == "outside_extent"
    assert outside["population_count_people"] is None


def test_population_service_emits_count_density_and_keeps_legacy_statistics():
    IdentityGdal.values = [[10.0, 20.0]]
    adapter = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr)
    grid = {"status": "passed", "level": 6, "cells": [{"grid_id": "G", "bbox": [0.0, 0.0, 2.0, 1.0]}]}
    result = PopulationGridService(lambda _: adapter).map(grid, "population.tif")
    cell = result["cells"]["G"]
    assert cell["population_count_people"] == pytest.approx(30.0)
    assert cell["population_density_people_km2"] == pytest.approx(30.0 / (cell["grid_area_m2"] / 1_000_000.0))
    assert cell["value_mean"] == pytest.approx(15.0)
    assert result["mapping"]["population_conservation"] is True
    assert result["mapping"]["interpolation"] == "none"


def test_population_service_keeps_partial_value_and_reports_coverage_counts():
    IdentityGdal.values = [[10.0, -9999.0]]
    adapter = GdalRasterAdapter("population.tif", IdentityGdal, IdentityOsr)
    grid = {"status": "passed", "level": 6, "cells": [{"grid_id": "G", "bbox": [0.0, 0.0, 2.0, 1.0]}]}
    result = PopulationGridService(lambda _: adapter).map(grid, "population.tif")
    cell = result["cells"]["G"]
    assert result["value_status"] == "passed"
    assert result["coverage_status"] == "partial"
    assert result["partial_count"] == 1
    assert result["full_count"] == result["missing_count"] == result["outside_count"] == 0
    assert cell["value_status"] == "passed"
    assert cell["population_count_people"] == pytest.approx(10.0)
    assert cell["population_density_people_km2"] == pytest.approx(
        10.0 / (cell["valid_covered_area_m2"] / 1_000_000.0)
    )
    assert cell["density_support_area_m2"] == cell["valid_covered_area_m2"]
    assert cell["density_semantics"] == "observed_covered_area_density"
    assert cell["quantities"]["population_density"]["conversion"]["not_extrapolated"] is True


class TerrainAdapter:
    def describe(self):
        return {"path": "glo30.tif", "crs": "EPSG:4326", "unit_metadata": "m", "band": 1, "nodata": None, "pixel_size": [1 / 3600, 1 / 3600]}
    def read_values(self, bbox): return [100.0, 120.0]


def test_terrain_contract_identifies_glo30_as_dsm_with_vertical_datum():
    grid = {"status": "passed", "level": 6, "cells": [{"grid_id": "G", "bbox": [120.0, 30.0, 120.01, 30.01]}]}
    result = TerrainGridService(lambda _: TerrainAdapter()).map(grid, "glo30.tif")
    assert result["surface_model"] == "DSM"
    assert result["source_profile"]["unit"] == "m"
    assert result["source_profile"]["crs"]["vertical"] == "EPSG:3855"
    assert result["cells"]["G"]["surface_elevation_mean_m"] == 110.0


class NoopGrid:
    def generate(self, bbox): return {"status": "passed", "level": 6, "cells": []}


def test_schema_v2_backfills_and_roundtrips_source_profiles(tmp_path):
    state = blank_project({})
    state["data_source_profiles"]["population"]["provenance"]["path"] = "fixture.tif"
    path = tmp_path / "project.json"
    repository = ProjectRepository(path)
    repository.save(state)
    restored = normalize_project(repository.load(), NoopGrid())
    assert restored["data_source_profiles"]["population"]["provenance"]["path"] == "fixture.tif"
    legacy = deepcopy(state)
    legacy.pop("data_source_profiles")
    restored_legacy = normalize_project(legacy, NoopGrid())
    assert restored_legacy["data_source_profiles"]["population"]["unit"] == "person/source_pixel"


def test_old_population_result_backfills_partial_value_without_extrapolation():
    state = blank_project({})
    state["grid_attributes"]["population"] = {
        "status": "missing_data", "quantity_status": "missing_data", "count": 1,
        "cells": {"G": {
            "status": "passed", "quantity_status": "missing_data",
            "population_count_people": 10.0, "population_density_people_km2": 10.0,
            "grid_area_m2": 1_000_000.0, "source_coverage_fraction": 0.5,
        }},
    }
    restored = normalize_project(state, NoopGrid())
    population = restored["grid_attributes"]["population"]
    cell = population["cells"]["G"]
    assert population["value_status"] == "passed"
    assert population["partial_count"] == 1
    assert cell["value_status"] == "passed"
    assert cell["coverage_status"] == "partial"
    assert cell["valid_covered_area_m2"] == 500_000.0
    assert cell["population_density_people_km2"] == 20.0


def test_registry_exposes_source_type_and_semantic_metadata():
    metadata = {
        "paths": {"population": "population.tif", "terrain": "terrain.tif"},
        "workflow": {"data_source_profiles": {"population": WORLDPOP_R2025A, "terrain": COPERNICUS_GLO30}},
        "population": {}, "terrain": {},
    }
    sources = {item["id"]: item for item in build_registry(metadata)}
    assert sources["population"]["source_type"] == "real"
    assert sources["population"]["unit"] == "person/source_pixel"
    assert sources["terrain"]["resolution"]["angular_value"] == 1
    assert sources["traffic"]["source_type"] == "synthetic"
    assert sources["candidate_sites"]["source_type"] == "manual"
