"""Tests for MH/T 4063.1 planar grid utilities."""

import pytest

from cns_planner.algorithms.grid.mht4063 import (
    cell_bounds,
    cell_indices,
    cell_size_arcseconds,
    cell_size_degrees,
    validate_level,
)

@pytest.mark.parametrize(
    ("level", "expected_lon", "expected_lat"),
    [
        (1, 21600.0, 14400.0),
        (2, 10800.0, 7200.0),
        (3, 1800.0, 1800.0),
        (4, 900.0, 600.0),
        (5, 300.0, 300.0),
        (6, 60.0, 60.0),
        (7, 12.0, 12.0),
        (8, 4.0, 4.0),
        (9, 2.0, 2.0),
        (10, 1.0, 1.0),
        (11, 0.5, 0.5),
        (12, 0.25, 0.25),
        (13, 0.125, 0.125),
        (14, 0.0625, 0.0625),
        (15, 0.03125, 0.03125),
        (16, 0.015625, 0.015625),
    ],
)
def test_cell_size_arcseconds(
    level,
    expected_lon,
    expected_lat,
):
    """Each level should return the expected angular cell size."""

    lon_size, lat_size = cell_size_arcseconds(level)

    assert lon_size == pytest.approx(expected_lon)
    assert lat_size == pytest.approx(expected_lat)


def test_level_7_size_in_degrees():
    """Level 7 should equal 12 arcseconds in both directions."""

    lon_size, lat_size = cell_size_degrees(7)

    expected = 12.0 / 3600.0

    assert lon_size == pytest.approx(expected)
    assert lat_size == pytest.approx(expected)


@pytest.mark.parametrize("level", [0, 17, -1, 100])
def test_invalid_level_range(level):
    """Levels outside the supported range must be rejected."""

    with pytest.raises(ValueError):
        validate_level(level)


def test_non_integer_level():
    """Grid level must be an integer."""

    with pytest.raises(TypeError):
        validate_level(7.5)
def dms(degrees, minutes, seconds):
    """Convert degrees/minutes/seconds to decimal degrees."""
    return degrees + minutes / 60.0 + seconds / 3600.0


def test_level_7_bounds():
    """Level 7 grid should have 12 arcsecond boundaries."""

    lon = dms(104, 19, 19.304)
    lat = dms(30, 18, 33.404)

    bounds = cell_bounds(lon, lat, 7)

    expected = (
        dms(104, 19, 12),
        dms(30, 18, 24),
        dms(104, 19, 24),
        dms(30, 18, 36),
    )

    assert bounds == pytest.approx(expected)


def test_level_8_bounds():
    """Level 8 grid should have 4 arcsecond boundaries."""

    lon = dms(104, 19, 19.304)
    lat = dms(30, 18, 33.404)

    bounds = cell_bounds(lon, lat, 8)

    expected = (
        dms(104, 19, 16),
        dms(30, 18, 32),
        dms(104, 19, 20),
        dms(30, 18, 36),
    )

    assert bounds == pytest.approx(expected)


def test_cell_bounds_contains_point():
    """The original point must lie inside the returned grid."""

    lon = 122.3
    lat = 30.1

    min_lon, min_lat, max_lon, max_lat = cell_bounds(
        lon,
        lat,
        7,
    )

    assert min_lon <= lon < max_lon
    assert min_lat <= lat < max_lat


def test_negative_latitude_bounds():
    """Southern hemisphere grids should also contain the point."""

    lon = 120.0
    lat = -30.1

    min_lon, min_lat, max_lon, max_lat = cell_bounds(
        lon,
        lat,
        7,
    )

    assert min_lon <= lon < max_lon
    assert min_lat <= lat < max_lat


def test_polar_region_not_supported_yet():
    """Polar subdivision is deliberately deferred."""

    with pytest.raises(ValueError):
        cell_bounds(120.0, 89.0, 7)
@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (2, (0, 1)),
        (3, (4, 0)),
        (4, (1, 1)),
        (5, (0, 1)),
        (6, (4, 3)),
        (7, (1, 2)),
        (8, (1, 2)),
    ],
)
def test_indices_against_standard_example(
    level,
    expected,
):
    """
    Verify child-grid geometric indices
    using the MH/T example coordinate.
    """

    lon = dms(104, 19, 19.304)
    lat = dms(30, 18, 33.404)

    assert cell_indices(
        lon,
        lat,
        level,
    ) == expected


def test_level_1_has_no_child_indices():
    """Level 1 has no parent grid."""

    with pytest.raises(ValueError):
        cell_indices(
            104.0,
            30.0,
            1,
        )