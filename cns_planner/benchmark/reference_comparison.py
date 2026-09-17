"""Read-only comparison between a *confirmed* reference route and a planned route.

Preconditions, all enforced rather than assumed:

1. the reference route's **source** CRS is confirmed by a human;
2. an explicit, user-confirmed :mod:`cns_planner.domain.reference_route_link`
   association exists for the pair.

Only then are any numbers produced.  Both polylines are projected into a recorded
local metric CRS (a Transverse Mercator / UTM-style projection centred on the data)
and resampled to a recorded spacing before Hausdorff / discrete Fréchet distances are
computed, because those two metrics are only meaningful in a metric plane.

Output describes differences.  It contains no similarity score, no ranking and no
"better" verdict.
"""

from __future__ import annotations

import math

from ..domain.reference_crs import is_resolved, unresolved_reason
from .geodesy import geodesic_distance_m, geodesic_length_m, is_geographic_crs

__all__ = [
    "compute_reference_comparison",
    "reference_comparison_readiness",
    "local_metric_crs",
    "hausdorff_distance_m",
    "discrete_frechet_distance_m",
    "resample_polyline",
    "DEFAULT_SAMPLE_SPACING_M",
]

#: Resampling spacing for the metric-plane shape metrics.
DEFAULT_SAMPLE_SPACING_M = 25.0
#: Local metric CRS is a custom TM projection; the string is recorded with results.
LOCAL_METRIC_PROJECTION = "+proj=tmerc +lat_0={lat_0} +lon_0={lon_0} +k=1 +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
LOCAL_METRIC_CRS_LABEL = "local_transverse_mercator_centred_on_reference_route_wgs84"

_WGS84_GEOGRAPHIC = ("EPSG:4326", "OGC:CRS84")


class _Projector:
    """Planar projection built with pyproj, or a documented local fallback."""

    def __init__(self, lon_0, lat_0):
        self.lon_0, self.lat_0 = float(lon_0), float(lat_0)
        self.crs = LOCAL_METRIC_PROJECTION.format(lat_0=self.lat_0, lon_0=self.lon_0)
        self.backend = "pyproj_transformer"
        self._transformer = None
        try:  # pragma: no cover - exercised only when pyproj is missing
            from pyproj import Transformer

            self._transformer = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
        except Exception:
            self._transformer = None
            self.backend = "local_equirectangular_about_centre"
        self._f, self._k = self._local_scales()

    def _local_scales(self):
        if self._transformer is not None:
            return None, None
        return math.cos(math.radians(self.lat_0)), 111319.49079327357  # pragma: no cover

    def __call__(self, point):
        lon, lat = float(point[0]), float(point[1])
        if self._transformer is not None:
            x, y = self._transformer.transform(lon, lat)
            return float(x), float(y)
        return (  # pragma: no cover - fallback path
            (lon - self.lon_0) * self._k * self._f,
            (lat - self.lat_0) * self._k,
        )


def local_metric_crs(reference_path, planned_path):
    """Build the local metric projector used for shape metrics, plus its record."""

    points = [point for point in list(reference_path or []) + list(planned_path or []) if _valid(point)]
    if not points:
        raise ValueError("构建局部米制投影需要至少一个有效坐标")
    lon_0 = sum(float(point[0]) for point in points) / len(points)
    lat_0 = sum(float(point[1]) for point in points) / len(points)
    projector = _Projector(lon_0, lat_0)
    return projector, {
        "label": LOCAL_METRIC_CRS_LABEL,
        "proj4": projector.crs,
        "centre_lon": lon_0,
        "centre_lat": lat_0,
        "units": "m",
        "backend": projector.backend,
        "rationale": (
            "Hausdorff/Fréchet 必须在米制平面计算；先投影到以数据为中心的局部 TM，"
            "再按记录间距重采样。"
        ),
    }


def _valid(point):
    return (
        isinstance(point, (list, tuple)) and len(point) >= 2
        and isinstance(point[0], (int, float)) and isinstance(point[1], (int, float))
        and not isinstance(point[0], bool) and not isinstance(point[1], bool)
        and math.isfinite(float(point[0])) and math.isfinite(float(point[1]))
    )


def _clean(points):
    result = []
    for point in points or []:
        if not _valid(point):
            continue
        candidate = [float(point[0]), float(point[1])]
        if result and geodesic_distance_m(result[-1], candidate) <= 1e-9:
            continue
        result.append(candidate)
    return result


def resample_polyline(points, spacing_m=DEFAULT_SAMPLE_SPACING_M):
    """Resample a lon/lat polyline at (approximately) equal geodesic spacing."""

    clean = _clean(points)
    if len(clean) < 2:
        return clean
    spacing = float(spacing_m)
    if spacing <= 0:
        raise ValueError("resample spacing_m 必须为正数")
    result = [clean[0]]
    carry = 0.0
    for left, right in zip(clean, clean[1:]):
        segment = geodesic_distance_m(left, right)
        if segment <= 0:
            continue
        travelled = spacing - carry
        while travelled <= segment:
            ratio = travelled / segment
            result.append([
                left[0] + (right[0] - left[0]) * ratio,
                left[1] + (right[1] - left[1]) * ratio,
            ])
            travelled += spacing
        carry = (segment - (travelled - spacing)) % spacing
    if result[-1] != clean[-1]:
        result.append(clean[-1])
    return result


def _project(projector, points):
    return [projector(point) for point in points]


def hausdorff_distance_m(left, right):
    """Symmetric vertex-set Hausdorff distance between two planar point sets."""

    if not left or not right:
        return None
    def directed(source, target):
        worst = 0.0
        for point in source:
            best = min(math.hypot(point[0] - other[0], point[1] - other[1]) for other in target)
            worst = max(worst, best)
        return worst
    return max(directed(left, right), directed(right, left))


def discrete_frechet_distance_m(left, right):
    """Discrete Fréchet distance between two planar polylines (coupling measure)."""

    if not left or not right:
        return None
    rows = len(left)
    columns = len(right)
    table = [[0.0] * columns for _ in range(rows)]
    for index in range(rows):
        for other in range(columns):
            distance = math.hypot(
                left[index][0] - right[other][0], left[index][1] - right[other][1],
            )
            if index == 0 and other == 0:
                table[index][other] = distance
            elif index == 0:
                table[index][other] = max(table[index][other - 1], distance)
            elif other == 0:
                table[index][other] = max(table[index - 1][other], distance)
            else:
                table[index][other] = max(
                    min(
                        table[index - 1][other],
                        table[index - 1][other - 1],
                        table[index][other - 1],
                    ),
                    distance,
                )
    return table[-1][-1]


def _endpoint_offsets(reference_path, planned_path, projector):
    start = projector(reference_path[0]), projector(planned_path[0])
    end = projector(reference_path[-1]), projector(planned_path[-1])
    return (
        math.hypot(start[0][0] - start[1][0], start[0][1] - start[1][1]),
        math.hypot(end[0][0] - end[1][0], end[0][1] - end[1][1]),
    )


def reference_comparison_readiness(reference_route, link, scenario_route, planned_path=None):
    """Explain whether a comparison may run; never guesses a missing prerequisite."""

    reasons = []
    if not reference_route:
        reasons.append("reference_route_missing")
    if not scenario_route:
        reasons.append("scenario_route_missing")
    if not link or link.get("confirmed") is not True:
        reasons.append("explicit_reference_route_link_missing_or_unconfirmed")
    crs = (reference_route or {}).get("crs")
    if not is_resolved(crs, role="source_crs"):
        reasons.append(unresolved_reason(crs, role="source_crs") or "source_crs_pending_confirmation")
    else:
        value = (crs or {}).get("source_crs", {}).get("value")
        if not is_geographic_crs(value):
            reasons.append("source_crs_not_supported_for_geodesic_measurement")
    if reference_route is not None and len(_clean(reference_route.get("path") or [])) < 2:
        reasons.append("reference_route_has_fewer_than_two_valid_points")
    if scenario_route is not None:
        scenario_path = [scenario_route.get("start"), scenario_route.get("end")]
        if len(_clean(scenario_path)) < 2:
            reasons.append("scenario_route_has_fewer_than_two_valid_points")
    if planned_path is not None and len(_clean(planned_path)) < 2:
        reasons.append("planned_path_has_fewer_than_two_valid_points")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "requires_confirmed_source_crs": True,
        "requires_confirmed_user_link": True,
        "automatic_association": False,
    }


def compute_reference_comparison(
    reference_route, scenario_route, link, quality=None, *, planned_path=None,
    spacing_m=DEFAULT_SAMPLE_SPACING_M,
):
    """Describe the difference between a confirmed reference route and a planned route.

    ``planned_path`` (the actual published operational/experiment path) is used when
    supplied; otherwise the comparison falls back to the scenario endpoints and says
    so in ``geodesic.planned_length_source``.

    Returns ``status="not_ready"`` with explicit reasons when any precondition is
    missing, so callers never receive a half-measured comparison.
    """

    readiness = reference_comparison_readiness(reference_route, link, scenario_route, planned_path)
    base = {
        "status": "not_ready" if not readiness["ready"] else "passed",
        "readiness": readiness,
        "reference_route_id": (reference_route or {}).get("reference_route_id"),
        "scenario_route_id": (scenario_route or {}).get("route_id"),
        "link_id": (link or {}).get("link_id"),
        "geodesic": {
            "reference_length_m": None,
            "planned_length_m": None,
            "length_delta_m": None,
            "length_ratio": None,
            "start_offset_m": None,
            "end_offset_m": None,
            "metric_semantics": "ellipsoidal_geodesic_on_wgs84",
        },
        "metric_plane": {
            "projection": None,
            "sample_spacing_m": float(spacing_m),
            "hausdorff_distance_m": None,
            "discrete_frechet_distance_m": None,
            "reference_sample_count": 0,
            "planned_sample_count": 0,
            "method": None,
        },
        "similarity_score": None,
        "ranking": None,
        "verdict": None,
        "note": (
            "仅描述差异（长度/端点偏移/形状距离），不产生 similarity score、排名或“更好”结论。"
        ),
    }
    if not readiness["ready"]:
        return base

    reference_path = _clean(reference_route.get("path") or [])
    if planned_path is not None:
        planned_path = _clean(planned_path)
        planned_source = "planned_path_supplied"
    elif quality and (quality.get("path") if isinstance(quality, dict) else None):
        planned_path = _clean(quality.get("path"))
        planned_source = "quality_measured_published_path"
    else:
        planned_path = _clean([scenario_route.get("start"), scenario_route.get("end")])
        planned_source = "scenario_endpoints_only_no_planner_output"
    if len(planned_path) < 2:
        base["status"] = "not_ready"
        base["readiness"] = {
            **readiness,
            "ready": False,
            "reasons": [*readiness["reasons"], "planned_path_has_fewer_than_two_valid_points"],
        }
        return base

    projector, projection = local_metric_crs(reference_path, planned_path)
    reference_sample = resample_polyline(reference_path, spacing_m)
    planned_sample = resample_polyline(planned_path, spacing_m)
    reference_plane = _project(projector, reference_sample)
    planned_plane = _project(projector, planned_sample)
    start_offset, end_offset = _endpoint_offsets(reference_path, planned_path, projector)

    reference_length = geodesic_length_m(reference_path)
    planned_length = geodesic_length_m(planned_path)
    ratio = (
        planned_length / reference_length
        if reference_length > 0 else None
    )
    return {
        **base,
        "geodesic": {
            "reference_length_m": reference_length,
            "planned_length_m": planned_length,
            "length_delta_m": planned_length - reference_length,
            "length_ratio": ratio,
            "start_offset_m": start_offset,
            "end_offset_m": end_offset,
            "metric_semantics": "ellipsoidal_geodesic_on_wgs84",
            "reference_length_source": "reference_route_confirmed_crs",
            "planned_length_source": planned_source,
        },
        "metric_plane": {
            "projection": projection,
            "sample_spacing_m": float(spacing_m),
            "hausdorff_distance_m": hausdorff_distance_m(reference_plane, planned_plane),
            "discrete_frechet_distance_m": discrete_frechet_distance_m(reference_plane, planned_plane),
            "reference_sample_count": len(reference_sample),
            "planned_sample_count": len(planned_sample),
            "method": "project_to_local_metric_then_resample_then_shape_distance",
            "densify": True,
        },
    }
