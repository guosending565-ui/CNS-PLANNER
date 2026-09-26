"""Small dependency-neutral geodesy primitives shared by production algorithms."""

from __future__ import annotations

import math


EARTH_RADIUS_M = 6_371_008.8


def distance_m(a, b):
    """Return the local equirectangular distance between two lon/lat points."""

    latitude = math.radians((float(a[1]) + float(b[1])) / 2.0)
    dx = math.radians(float(b[0]) - float(a[0])) * EARTH_RADIUS_M * math.cos(latitude)
    dy = math.radians(float(b[1]) - float(a[1])) * EARTH_RADIUS_M
    return math.hypot(dx, dy)
