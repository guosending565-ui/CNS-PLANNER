"""MH/T 4063.1-2026 gridded airspace utilities.

This module implements the spatial subdivision rules defined by
MH/T 4063.1-2026.

At this stage we define planar grid sizes and regular non-polar
cell boundaries. Encoding and decoding will be added later.
"""

import math
from fractions import Fraction


MIN_PLANAR_LEVEL = 1
MAX_PLANAR_LEVEL = 16


# (longitude span, latitude span), in degrees.
#
# MH/T 4063.1-2026 planar subdivision:
#
# L1   6° × 4°
# L2   3° × 2°
# L3   30' × 30'
# L4   15' × 10'
# L5   5' × 5'
# L6   1' × 1'
# L7   12" × 12"
# L8   4" × 4"
# L9   2" × 2"
# L10  1" × 1"
# L11  1/2" × 1/2"
# L12  1/4" × 1/4"
# L13  1/8" × 1/8"
# L14  1/16" × 1/16"
# L15  1/32" × 1/32"
# L16  1/64" × 1/64"

LEVEL_SIZE_DEGREES = {
    1: (Fraction(6, 1), Fraction(4, 1)),
    2: (Fraction(3, 1), Fraction(2, 1)),
    3: (Fraction(1, 2), Fraction(1, 2)),
    4: (Fraction(1, 4), Fraction(1, 6)),
    5: (Fraction(1, 12), Fraction(1, 12)),
    6: (Fraction(1, 60), Fraction(1, 60)),
    7: (Fraction(1, 300), Fraction(1, 300)),
    8: (Fraction(1, 900), Fraction(1, 900)),
    9: (Fraction(1, 1800), Fraction(1, 1800)),
    10: (Fraction(1, 3600), Fraction(1, 3600)),
    11: (Fraction(1, 7200), Fraction(1, 7200)),
    12: (Fraction(1, 14400), Fraction(1, 14400)),
    13: (Fraction(1, 28800), Fraction(1, 28800)),
    14: (Fraction(1, 57600), Fraction(1, 57600)),
    15: (Fraction(1, 115200), Fraction(1, 115200)),
    16: (Fraction(1, 230400), Fraction(1, 230400)),
}
# Number of child cells created from the previous level:
#
# value = (longitude_columns, latitude_rows)
#
# Example:
# level 3 divides each level-2 cell into
# 6 columns × 4 rows.

LEVEL_SUBDIVISIONS = {
    2: (2, 2),
    3: (6, 4),
    4: (2, 3),
    5: (3, 2),
    6: (5, 5),
    7: (5, 5),
    8: (3, 3),
    9: (2, 2),
    10: (2, 2),
    11: (2, 2),
    12: (2, 2),
    13: (2, 2),
    14: (2, 2),
    15: (2, 2),
    16: (2, 2),
}

def validate_level(level: int) -> None:
    """Validate a planar grid level."""

    if not isinstance(level, int):
        raise TypeError("level 必须是整数")

    if not MIN_PLANAR_LEVEL <= level <= MAX_PLANAR_LEVEL:
        raise ValueError(
            f"平面网格层级必须位于 "
            f"{MIN_PLANAR_LEVEL}～{MAX_PLANAR_LEVEL}"
        )


def cell_size_degrees(level: int) -> tuple[float, float]:
    """
    Return grid size as
    (longitude_degrees, latitude_degrees).
    """

    validate_level(level)

    lon_size, lat_size = LEVEL_SIZE_DEGREES[level]

    return float(lon_size), float(lat_size)


def cell_size_arcseconds(level: int) -> tuple[float, float]:
    """
    Return grid size as
    (longitude_arcseconds, latitude_arcseconds).
    """

    lon_deg, lat_deg = cell_size_degrees(level)

    return (
        lon_deg * 3600.0,
        lat_deg * 3600.0,
    )


def validate_coordinate(
    lon: float,
    lat: float,
) -> None:
    """
    Validate a geographic coordinate.

    Current implementation supports the regular
    non-polar region only.
    """

    if not isinstance(lon, (int, float)):
        raise TypeError("lon 必须是数值")

    if not isinstance(lat, (int, float)):
        raise TypeError("lat 必须是数值")

    if not math.isfinite(lon) or not math.isfinite(lat):
        raise ValueError("经纬度必须是有限数值")

    if not -180.0 <= lon < 180.0:
        raise ValueError("经度必须位于 [-180, 180)")

    # Polar-region subdivision is different.
    # It will be implemented separately if needed.
    if not -88.0 < lat < 88.0:
        raise ValueError(
            "当前版本仅支持 88°S～88°N 非极地区"
        )


def cell_bounds(
    lon: float,
    lat: float,
    level: int,
) -> tuple[float, float, float, float]:
    """
    Return the planar grid cell containing a coordinate.

    Result order:
        (
            min_lon,
            min_lat,
            max_lon,
            max_lat,
        )
    """

    validate_level(level)
    validate_coordinate(lon, lat)

    lon_size, lat_size = LEVEL_SIZE_DEGREES[level]

    # Decimal-degree input is first converted through str()
    # so Fraction does not inherit binary float noise.
    lon_value = Fraction(str(lon))
    lat_value = Fraction(str(lat))

    # ---------------------------------------------------------
    # Longitude
    #
    # Regular grid origin:
    # 180 degrees west.
    # ---------------------------------------------------------

    lon_origin = Fraction(-180, 1)

    lon_index = (
        lon_value - lon_origin
    ) // lon_size

    min_lon = (
        lon_origin
        + lon_index * lon_size
    )

    max_lon = min_lon + lon_size

      # ---------------------------------------------------------
    # Latitude
    #
    # Use the equator as the origin and floor the signed
    # latitude directly.
    #
    # This gives every cell a consistent half-open interval:
    #
    #     [min_lat, max_lat)
    #
    # It also makes coordinates lying exactly on a grid line
    # deterministic in both hemispheres.
    # ---------------------------------------------------------

    lat_index = (
        lat_value // lat_size
    )

    min_lat = lat_index * lat_size
    max_lat = min_lat + lat_size

    return (
        float(min_lon),
        float(min_lat),
        float(max_lon),
        float(max_lat),
    )
def cell_indices(
    lon: float,
    lat: float,
    level: int,
) -> tuple[int, int]:
    """
    Return the geometric column and row of the coordinate
    inside its parent grid cell.

    Result:
        (column, row)

    Column numbering starts from west to east.
    Row numbering starts from south to north.

    This function returns geometric indices only.
    Conversion to MH/T encoding characters is handled separately.
    """

    validate_level(level)
    validate_coordinate(lon, lat)

    if level == 1:
        raise ValueError(
            "第一级网格没有父级网格，不能计算子网格行列号"
        )

    lon_value = Fraction(str(lon))
    lat_value = Fraction(str(lat))

    parent_lon_size, parent_lat_size = (
        LEVEL_SIZE_DEGREES[level - 1]
    )

    child_lon_size, child_lat_size = (
        LEVEL_SIZE_DEGREES[level]
    )

    # ---------------------------------------------------------
    # Locate the parent grid exactly.
    # ---------------------------------------------------------

    lon_origin = Fraction(-180, 1)

    parent_lon_index = (
        (lon_value - lon_origin)
        // parent_lon_size
    )

    parent_min_lon = (
        lon_origin
        + parent_lon_index * parent_lon_size
    )

    parent_lat_index = (
        lat_value // parent_lat_size
    )

    parent_min_lat = (
        parent_lat_index * parent_lat_size
    )

    # ---------------------------------------------------------
    # Locate the child cell inside the parent.
    # ---------------------------------------------------------

    column = int(
        (lon_value - parent_min_lon)
        // child_lon_size
    )

    row = int(
        (lat_value - parent_min_lat)
        // child_lat_size
    )

    expected_columns, expected_rows = (
        LEVEL_SUBDIVISIONS[level]
    )

    if not 0 <= column < expected_columns:
        raise RuntimeError(
            f"第{level}级列号计算异常：{column}"
        )

    if not 0 <= row < expected_rows:
        raise RuntimeError(
            f"第{level}级行号计算异常：{row}"
        )

    return column, row