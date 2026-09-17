"""Geodesic measurement helpers for expert evidence.

The planners keep their own published metrics (V1's fixed-degree grid, V2's
``distance_m``), and this module never feeds anything back into them.  It exists so
the *review* layer can state, with an explicit semantic label, what was measured:

``geodesic``
    ``pyproj.Geod(ellps="WGS84")`` — ellipsoidal geodesic distance and azimuth.

``spherical_haversine``
    Documented fallback used only when pyproj is unavailable.  It is labelled as
    such so no reviewer mistakes it for an ellipsoidal result.

Heading is a compass azimuth in degrees clockwise from true north, and heading
change is normalized to ``[0, 180]`` with a stated tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

try:  # pragma: no cover - import guard, exercised by the fallback test
    from pyproj import Geod

    _GEOD = Geod(ellps="WGS84")
    GEODESIC_BACKEND = "pyproj.Geod(ellps=WGS84)"
except Exception:  # pragma: no cover - only on installations without pyproj
    _GEOD = None
    GEODESIC_BACKEND = "spherical_haversine_r6371008.8"

ELLIPSOID = "WGS84"
SPHERE_RADIUS_M = 6371008.8

#: Heading differences below this are treated as "no turn".
HEADING_CHANGE_TOLERANCE_DEG = 1e-9
#: Boundary tolerance used by the geodesic fallback's azimuth computation.
AZIMUTH_EPSILON_DEG = 1e-12

METRIC_SEMANTICS = {
    "distance": "ellipsoidal_geodesic_distance_m" if _GEOD else "spherical_haversine_distance_m",
    "bearing": "ellipsoidal_geodesic_azimuth_deg" if _GEOD else "spherical_azimuth_deg",
    "backend": GEODESIC_BACKEND,
    "ellipsoid": ELLIPSOID if _GEOD else None,
    "distance_unit": "m",
    "bearing_convention": "compass_degrees_clockwise_from_true_north",
    "heading_change_range_deg": [0.0, 180.0],
    "heading_change_tolerance_deg": HEADING_CHANGE_TOLERANCE_DEG,
}


@dataclass(frozen=True)
class GeodesicResult:
    distance_m: float
    forward_azimuth_deg: float
    back_azimuth_deg: float
    backend: str


def is_geographic_crs(value):
    """True when a CRS value is a WGS84/CRS84 geographic CRS we may geodetically measure."""

    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return False
    if text in ("ogc:crs84", "crs84", "crs:84", "urn:ogc:def:crs:ogc:1.3:crs84"):
        return True
    normalized = text.replace("epsg:", "").replace("+", "")
    return normalized in ("4326", "4979", "crs84")


def _finite_pair(point):
    return (
        isinstance(point, (list, tuple)) and len(point) >= 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in point[:2])
        and math.isfinite(float(point[0])) and math.isfinite(float(point[1]))
    )


def geodesic_inverse(left, right):
    """Distance and azimuths from ``left`` to ``right`` on WGS84."""

    if not _finite_pair(left) or not _finite_pair(right):
        raise ValueError("测地计算需要两个有限 [lon, lat] 坐标")
    lon1, lat1 = float(left[0]), float(left[1])
    lon2, lat2 = float(right[0]), float(right[1])
    if _GEOD is not None:
        azimuth, back_azimuth, distance = _GEOD.inv(lon1, lat1, lon2, lat2)
        return GeodesicResult(
            distance_m=float(distance) if math.isfinite(distance) else 0.0,
            forward_azimuth_deg=float(azimuth) % 360.0,
            back_azimuth_deg=float(back_azimuth) % 360.0,
            backend=GEODESIC_BACKEND,
        )
    return GeodesicResult(  # pragma: no cover - fallback path
        distance_m=_spherical_distance_m(lon1, lat1, lon2, lat2),
        forward_azimuth_deg=_spherical_azimuth_deg(lon1, lat1, lon2, lat2),
        back_azimuth_deg=(_spherical_azimuth_deg(lon2, lat2, lon1, lat1)) % 360.0,
        backend=GEODESIC_BACKEND,
    )


def geodesic_distance_m(left, right):
    return geodesic_inverse(left, right).distance_m


def geodesic_bearing_deg(left, right):
    """Compass azimuth of ``left -> right`` (0 = true north, 90 = east)."""

    return geodesic_inverse(left, right).forward_azimuth_deg


def geodesic_length_m(points):
    """Summed geodesic length of a polyline; ``0.0`` for fewer than two vertices."""

    total = 0.0
    for left, right in zip(points or [], (points or [])[1:]):
        total += geodesic_distance_m(left, right)
    return total


def heading_change_deg(left_heading, right_heading):
    """Absolute turn between two compass azimuths, normalized to [0, 180]."""

    delta = abs(float(right_heading) - float(left_heading)) % 360.0
    change = min(delta, 360.0 - delta)
    return 0.0 if change <= HEADING_CHANGE_TOLERANCE_DEG else change


def _spherical_distance_m(lon1, lat1, lon2, lat2):  # pragma: no cover - fallback path
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return SPHERE_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(h)))


def _spherical_azimuth_deg(lon1, lat1, lon2, lat2):  # pragma: no cover - fallback path
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.degrees(math.atan2(y, x)) % 360.0
