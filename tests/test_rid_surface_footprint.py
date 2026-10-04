"""Round30-B2: surface-aware RID map footprint (presentation only)."""

from __future__ import annotations

import math

import pytest

pytest.importorskip("shapely")
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from cns_planner.application.rid_surface_footprint import (
    build_surface_aware_rid_footprint,
)
from cns_planner.application.cns_facility_assembler import rid_radius_pair


METRES_PER_DEGREE = 111_320.0


class _LandMask:
    def __init__(self, polygons, *, coastal_buffer_m=0.0):
        self._items = [(polygon, polygon.bounds) for polygon in polygons]
        self.coastal_uncertainty_buffer_m = coastal_buffer_m

    def metric_polygons(self):
        return self._items

    @staticmethod
    def to_metric(longitude, latitude):
        return float(longitude) * METRES_PER_DEGREE, float(latitude) * METRES_PER_DEGREE

    @staticmethod
    def to_geographic(x, y):
        return float(x) / METRES_PER_DEGREE, float(y) / METRES_PER_DEGREE

    def describe(self):
        return {
            "source_role": "land_mask", "source_type": "real",
            "metric_crs": "LOCAL:METRES", "polygon_count": len(self._items),
            "crs_status": "passed",
        }


def _facts(**overrides):
    value = {
        "status": "passed",
        "source": {"land_mask_configured": True},
        "land_mask": {"configured_path": "authoritative-land.gpkg"},
        "classified_grid_cell_count": 3,
        "surface_class_counts": {
            "land": 1, "coastal_uncertain": 1, "sea": 1, "unknown": 0,
        },
        "input_fingerprint": "surface-facts-sha256",
    }
    value.update(overrides)
    return value


def _build(source, facts=None):
    land_radius, sea_radius = rid_radius_pair()
    return build_surface_aware_rid_footprint(
        0.0, 0.0, land_radius_m=land_radius, sea_radius_m=sea_radius,
        land_mask_source=source, surface_class_facts=_facts() if facts is None else facts,
        metric_crs="EPSG:32651",
    )


def _metric_geometry(records):
    polygons = []
    for item in records:
        shell = [(x * METRES_PER_DEGREE, y * METRES_PER_DEGREE) for x, y in item["exterior"]]
        holes = [
            [(x * METRES_PER_DEGREE, y * METRES_PER_DEGREE) for x, y in ring]
            for ring in item.get("holes") or []
        ]
        polygons.append(Polygon(shell, holes))
    return unary_union(polygons)


def _metric_lines(records):
    from shapely.geometry import LineString

    return unary_union([
        LineString([(x * METRES_PER_DEGREE, y * METRES_PER_DEGREE) for x, y in line])
        for line in records
    ])


def test_all_land_has_only_2km_base():
    footprint = _build(_LandMask([box(-10_000, -10_000, 10_000, 10_000)]))
    assert len(footprint["base_polygons"]) == 1
    assert footprint["sea_extension_polygons"] == []
    assert footprint["metadata"]["extension_reason"] == "no_confirmed_sea_within_2_to_5km"


def test_all_sea_has_2km_base_and_full_2_to_5km_annulus():
    footprint = _build(_LandMask([box(100_000, 100_000, 101_000, 101_000)]))
    extension = _metric_geometry(footprint["sea_extension_polygons"])
    assert extension.covers(Point(3_000, 0))
    assert not extension.covers(Point(1_000, 0))
    assert math.isclose(extension.area, math.pi * (5_000 ** 2 - 2_000 ** 2), rel_tol=0.01)


def test_coast_only_extends_in_confirmed_sea_direction():
    # x >= 0 is land; the confirmed-sea side is x < 0 after the uncertainty buffer.
    footprint = _build(_LandMask([box(0, -10_000, 10_000, 10_000)], coastal_buffer_m=300.0))
    extension = _metric_geometry(footprint["sea_extension_polygons"])
    boundary = _metric_lines(footprint["sea_extension_boundary_lines"])
    assert extension.covers(Point(-3_000, 0))
    assert not extension.covers(Point(3_000, 0))
    assert boundary.distance(Point(-5_000, 0)) < 1.0
    assert boundary.distance(Point(5_000, 0)) > 1_000.0


def test_coastal_uncertain_never_extends_beyond_2km():
    footprint = _build(_LandMask([box(0, -10_000, 10_000, 10_000)], coastal_buffer_m=300.0))
    extension = _metric_geometry(footprint["sea_extension_polygons"])
    # Both probes are outside 2 km; only the point beyond the 300 m coastal band is confirmed sea.
    assert not extension.covers(Point(-100, 3_000))
    assert extension.covers(Point(-1_000, 3_000))


def test_unknown_or_missing_landmask_is_fail_closed_to_2km():
    unknown = _build(
        _LandMask([box(100_000, 100_000, 101_000, 101_000)]),
        facts=_facts(status="stale"),
    )
    missing = _build(None)
    assert unknown["sea_extension_polygons"] == []
    assert missing["sea_extension_polygons"] == []
    assert unknown["metadata"]["extension_reason"] == "surface_class_facts_not_passed"
    assert missing["metadata"]["extension_reason"] == "land_mask_source_missing"


def test_registered_radii_are_unchanged():
    assert rid_radius_pair() == (2000.0, 5000.0)
