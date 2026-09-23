"""Focused regression tests for shared FABDEM footprint-ground sampling."""

from pathlib import Path

import pytest

from cns_planner.gis.fine_environment_adapter import (
    FabdemWindowTerrainSource,
    NativeTerrainWindowSource,
    _FabdemRasterBase,
)


class _Transform:
    @staticmethod
    def to_geographic(point):
        return list(point)


class _Band:
    def __init__(self, values, *, nodata=-9999.0, scale=None, offset=None):
        self.values = values
        self.nodata = nodata
        self.scale = scale
        self.offset = offset

    def ReadAsArray(self, x0, y0, width, height):
        return [row[x0:x0 + width] for row in self.values[y0:y0 + height]]

    def GetNoDataValue(self):
        return self.nodata

    def GetScale(self):
        return self.scale

    def GetOffset(self):
        return self.offset


class _Dataset:
    def __init__(self, band):
        self.band = band
        self.RasterYSize = len(band.values)
        self.RasterXSize = len(band.values[0])


def _source(source_type, values, *, vertical_status="confirmed", nodata=-9999.0,
            scale=None, offset=None):
    source = source_type.__new__(source_type)
    source.band = _Band(values, nodata=nodata, scale=scale, offset=offset)
    source.dataset = _Dataset(source.band)
    source.inverse = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    source.nodata = nodata
    source.vertical_status = vertical_status
    source._transform_to_raster_crs = lambda coordinate: (
        float(coordinate[0]), float(coordinate[1]), 0.0,
    )
    return source


RING_2_BY_2 = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]


def test_native_and_window_sources_inherit_one_authoritative_sampling_implementation():
    assert NativeTerrainWindowSource.sample_footprint_ground is _FabdemRasterBase.sample_footprint_ground
    assert FabdemWindowTerrainSource.sample_footprint_ground is _FabdemRasterBase.sample_footprint_ground
    assert NativeTerrainWindowSource._pixel_window is _FabdemRasterBase._pixel_window
    assert FabdemWindowTerrainSource._pixel_window is _FabdemRasterBase._pixel_window
    assert NativeTerrainWindowSource._to_pixel is _FabdemRasterBase._to_pixel
    assert FabdemWindowTerrainSource._to_pixel is _FabdemRasterBase._to_pixel
    assert hasattr(_source(NativeTerrainWindowSource, [[1.0]]), "sample_footprint_ground")


def test_native_and_window_sources_return_same_scaled_max_for_valid_two_by_two_window():
    values = [[1.0, 4.0], [2.0, 3.0]]
    expected = 9.0  # max(raw)=4; scale=2, offset=1
    results = [
        _source(source_type, values, scale=2.0, offset=1.0).sample_footprint_ground(
            RING_2_BY_2, transform=_Transform(),
        )
        for source_type in (FabdemWindowTerrainSource, NativeTerrainWindowSource)
    ]
    assert results == pytest.approx([expected, expected])


@pytest.mark.parametrize("source_type", [FabdemWindowTerrainSource, NativeTerrainWindowSource])
def test_all_nodata_remains_none_never_zero(source_type):
    source = _source(source_type, [[-9999.0, -9999.0], [-9999.0, -9999.0]])
    assert source.sample_footprint_ground(RING_2_BY_2, transform=_Transform()) is None


@pytest.mark.parametrize("source_type", [FabdemWindowTerrainSource, NativeTerrainWindowSource])
def test_unconfirmed_vertical_reference_remains_none(source_type):
    source = _source(source_type, [[1.0, 2.0], [3.0, 4.0]], vertical_status="unresolved")
    assert source.sample_footprint_ground(RING_2_BY_2, transform=_Transform()) is None


def test_building_capability_guard_accepts_native_source_and_returns_ground():
    terrain = _source(NativeTerrainWindowSource, [[1.0, 3.15], [2.0, 3.0]])
    ground = None
    if hasattr(terrain, "sample_footprint_ground"):
        ground = terrain.sample_footprint_ground(RING_2_BY_2, transform=_Transform())
    assert ground == pytest.approx(3.15)
    assert ground != 0.0


def test_real_4tceh_native_footprint_ground_is_original_two_by_two_window():
    """Optional local-data integration: requires the QGIS Python runtime and source files."""

    qgis_core = pytest.importorskip("qgis.core")
    terrain_path = Path(
        "D:/aaa2026project/UOM/舟山/规划系统/FABDEM/processed/"
        "Zhoushan_FABDEM_DTM_30m.tif"
    )
    buildings_path = Path(
        "D:/aaa2026project/UOM/舟山/规划系统/building/processed/"
        "zhoushan_buildings.gpkg"
    )
    if not terrain_path.is_file() or not buildings_path.is_file():
        pytest.skip("real Zhoushan FABDEM/building sources are not present")

    from cns_planner.gis.fine_environment_adapter import QgisMetricTransform, _metric_rings_of

    layer = qgis_core.QgsVectorLayer(
        f"{buildings_path}|layername=buildings", "4tCeH", "ogr",
    )
    assert layer.isValid()
    request = qgis_core.QgsFeatureRequest().setFilterExpression('"id" = \'4tCeH\'')
    feature = next(layer.getFeatures(request))
    metric_crs = qgis_core.QgsCoordinateReferenceSystem("EPSG:32651")
    coordinate_transform = qgis_core.QgsCoordinateTransform(
        layer.crs(), metric_crs, qgis_core.QgsProject.instance().transformContext(),
    )
    geometry = qgis_core.QgsGeometry(feature.geometry())
    geometry.transform(coordinate_transform)
    rings = _metric_rings_of(geometry)
    assert len(rings) == 1
    assert len(rings[0]) == 6

    terrain = NativeTerrainWindowSource(terrain_path)
    ground = terrain.sample_footprint_ground(
        rings[0], transform=QgisMetricTransform("EPSG:32651"),
    )
    assert ground == pytest.approx(3.150000095, abs=1e-6)
    assert ground != pytest.approx(3.349999905, abs=1e-6)
