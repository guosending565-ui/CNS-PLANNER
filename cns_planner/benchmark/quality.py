"""Independent route-quality measurement for expert review.

This evaluator exists *outside* the planners.  It never writes into a V1/V2 result
dict and it never re-derives risk: when a planner already published authoritative
risk metrics (V2 ``distance_m`` / ``risk_exposure_index_m`` / ``mean_risk_index`` /
``max_risk_index`` / ``detour_factor``) those values are read back and reported as
``planner_reported``.  Everything else is measured from the published polyline.

Measurement semantics (explicit, never implicit):

* distances and headings are **geodesic** on WGS84 (``pyproj.Geod``), with a
  labelled spherical fallback only when pyproj is unavailable;
* headings are compass azimuths (0 = true north, clockwise) and heading change is
  normalized to ``[0, 180]`` degrees with a stated tolerance;
* the published planner polyline is treated as WGS84 lon/lat because that is the
  project's canonical representation for planned routes — reference-route data,
  whose CRS is *not* confirmed, is handled by the separate reference comparator and
  never measured here.

Feasibility is reported from the existing authoritative results
(:func:`cns_planner.application.constraint_validation.validate_hard_constraints` for
hard constraints and the planner's own ``status`` for allowed-airspace feasibility).
No verdict, ranking or recommendation is produced.
"""

from __future__ import annotations

import re

from .geodesy import (
    HEADING_CHANGE_TOLERANCE_DEG,
    METRIC_SEMANTICS,
    geodesic_bearing_deg,
    geodesic_distance_m,
    geodesic_inverse,
    heading_change_deg,
)

__all__ = [
    "evaluate_route_quality",
    "RoutePlanningDiagnostics",
    "grid_path_metrics",
    "evaluate_constraint_feasibility",
    "polyline_metrics",
    "heading_deg",
    "HEADING_CHANGE_TOLERANCE_DEG",
    "METRIC_SEMANTICS",
]

#: Shared, code-level tolerance for treating a published vertex as duplicated or a
#: published segment as degenerate.  This is a *vertex dedup* tolerance, not a
#: measurement tolerance: it does not change any reported metric above its value.
GEOMETRY_TOLERANCE_M = 1e-6

_PLANNER_RISK_FIELDS = (
    "distance_m",
    "risk_exposure_index_m",
    "mean_risk_index",
    "max_risk_index",
    "optimization_cost",
    "straight_line_distance_m",
    "detour_factor",
    "risk_component",
    "risk_weight_lambda",
)

#: The subset that is genuinely risk evidence.  These are read from the existing
#: planner output and are never recomputed here.
_RISK_EVIDENCE_FIELDS = (
    "risk_exposure_index_m",
    "mean_risk_index",
    "max_risk_index",
    "optimization_cost",
    "risk_component",
    "risk_weight_lambda",
)

_GRID_ID = re.compile(
    r"^MHT4063-L(?P<level>\d+)-C(?P<column>\d+)-R(?P<sign>[PM])(?P<row>\d+)$"
)

ZIGZAG_INDEX_DEFINITION = (
    "sum_absolute_heading_change_deg / (180 * interior_vertex_count); "
    "0 when there is no interior vertex"
)


def heading_deg(left, right):
    """Geodesic compass heading of ``left -> right`` (0 = north, 90 = east)."""

    return geodesic_bearing_deg(left, right)


def _clean_points(path):
    """Return the published vertices, dropping near-duplicate points only."""

    points = []
    for item in path or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        lon, lat = item[0], item[1]
        if isinstance(lon, bool) or isinstance(lat, bool):
            continue
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        point = [float(lon), float(lat)]
        if points and geodesic_distance_m(points[-1], point) <= GEOMETRY_TOLERANCE_M:
            continue
        points.append(point)
    return points


def polyline_metrics(path):
    """Measure the published polyline geodetically. No planner involvement."""

    points = _clean_points(path)
    lengths = [geodesic_distance_m(left, right) for left, right in zip(points, points[1:])]
    headings = [geodesic_bearing_deg(left, right) for left, right in zip(points, points[1:])]
    changes = [
        heading_change_deg(left, right) for left, right in zip(headings, headings[1:])
    ]
    straight = geodesic_distance_m(points[0], points[-1]) if len(points) > 1 else 0.0
    interior_vertex_count = max(0, len(points) - 2)
    zigzag_index = (
        sum(changes) / (180.0 * interior_vertex_count)
        if interior_vertex_count else 0.0
    )
    return {
        "path_length_m": sum(lengths),
        "segment_count": len(lengths),
        "turn_count": sum(1 for change in changes if change > HEADING_CHANGE_TOLERANCE_DEG),
        "total_heading_change_deg": sum(changes),
        "max_heading_change_deg": max(changes) if changes else 0.0,
        "min_segment_m": min(lengths) if lengths else 0.0,
        "vertex_count": len(points),
        "zigzag_index": zigzag_index,
        "zigzag_index_definition": ZIGZAG_INDEX_DEFINITION,
        "straight_line_distance_m": straight,
        "metric_semantics": dict(METRIC_SEMANTICS),
        "heading_method": "geodesic_azimuth_difference_normalized_to_180",
        "heading_change_tolerance_deg": HEADING_CHANGE_TOLERANCE_DEG,
        "vertex_dedup_tolerance_m": GEOMETRY_TOLERANCE_M,
        "assumed_input_crs": "EPSG:4326",
        "assumed_input_crs_note": (
            "运行航路 path 为项目 canonical 表示（WGS84 经度/纬度）；"
            "CRS 未确认的 reference 数据不得使用本度量。"
        ),
    }


def _parse_grid_id(grid_id):
    match = _GRID_ID.match(str(grid_id or ""))
    if not match:
        return None
    row = int(match.group("row")) * (-1 if match.group("sign") == "M" else 1)
    return int(match.group("level")), int(match.group("column")), row


def grid_path_metrics(grid_path, grid=None):
    """Describe V2 grid traversal without judging or changing the route."""

    ids = [str(value) for value in grid_path or []]
    parsed = [_parse_grid_id(value) for value in ids]
    histogram = {name: 0 for name in ("E", "W", "N", "S", "NE", "NW", "SE", "SW")}
    horizontal = vertical = diagonal = invalid = 0
    direction_by_delta = {
        (1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S",
        (1, 1): "NE", (-1, 1): "NW", (1, -1): "SE", (-1, -1): "SW",
    }
    for left, right in zip(parsed, parsed[1:]):
        if left is None or right is None or left[0] != right[0]:
            invalid += 1
            continue
        dx, dy = right[1] - left[1], right[2] - left[2]
        direction = direction_by_delta.get((dx, dy))
        if direction is None:
            invalid += 1
            continue
        histogram[direction] += 1
        if dx and dy:
            diagonal += 1
        elif dx:
            horizontal += 1
        else:
            vertical += 1
    levels = sorted({item[0] for item in parsed if item is not None})
    cell_size = (grid or {}).get("cell_size_degrees")
    representative = None
    cells = (grid or {}).get("cells") or []
    if cells:
        bbox = cells[0].get("bbox") or []
        if len(bbox) == 4:
            center_lat = (float(bbox[1]) + float(bbox[3])) / 2.0
            representative = {
                "width_m": geodesic_distance_m([bbox[0], center_lat], [bbox[2], center_lat]),
                "height_m": geodesic_distance_m([bbox[0], bbox[1]], [bbox[0], bbox[3]]),
                "semantics": "representative_first_grid_cell_geodesic_dimensions",
            }
    return {
        "grid_path_vertex_count": len(ids),
        "grid_step_count": max(0, len(ids) - 1),
        "horizontal_step_count": horizontal,
        "vertical_step_count": vertical,
        "diagonal_step_count": diagonal,
        "invalid_or_non_adjacent_step_count": invalid,
        "direction_histogram": histogram,
        "grid_level": levels[0] if len(levels) == 1 else (levels or None),
        "cell_size_degrees": list(cell_size) if isinstance(cell_size, (list, tuple)) else cell_size,
        "representative_cell_size_m": representative,
        "source": "published_grid_path_and_supplied_grid_metadata",
    }


def evaluate_constraint_feasibility(hard_constraints):
    """Fail-closed hard-constraint verdict from the authoritative validator."""

    from ..application.constraint_validation import validate_hard_constraints

    try:
        validate_hard_constraints(hard_constraints)
    except ValueError as exc:
        return {
            "status": "rejected",
            "detail": str(exc),
            "validator": "cns_planner.application.constraint_validation.validate_hard_constraints",
            "count": None,
        }
    count = 0 if hard_constraints in (None, []) else len(hard_constraints)
    return {
        "status": "accepted" if count == 0 else "well_formed",
        "detail": "无硬约束" if count == 0 else f"{count} 项硬约束输入合法（未判断是否阻断路径）",
        "validator": "cns_planner.application.constraint_validation.validate_hard_constraints",
        "count": count,
    }


def _planner_reported(result):
    reported = {}
    for name in _PLANNER_RISK_FIELDS:
        if name in (result or {}):
            reported[name] = result.get(name)
    return reported


def evaluate_route_quality(
    route, result, hard_constraints=None, runtime_ms=None, *, grid=None,
    airspace_eligibility=None,
):
    """Measure one published ``result`` for one ``route``. Reports, never ranks.

    ``route``/``result`` are read-only; the returned mapping is a separate
    structure that no planner output refers to.
    """

    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or "missing_data")
    raw_path = result.get("path") or []
    metrics = polyline_metrics(raw_path)
    straight = metrics["straight_line_distance_m"]
    measured_detour = (
        metrics["path_length_m"] / straight
        if straight > GEOMETRY_TOLERANCE_M
        else (1.0 if metrics["path_length_m"] <= GEOMETRY_TOLERANCE_M else None)
    )
    reported = _planner_reported(result)
    planned_length = reported.get("distance_m")
    grid_behavior = grid_path_metrics(result.get("grid_path"), grid)
    return {
        "route_id": str(result.get("route_id") or (route or {}).get("route_id") or ""),
        "status": status,
        "algorithm_id": result.get("algorithm_id"),
        "algorithm_version": result.get("algorithm_version"),
        "input_fingerprint": result.get("input_fingerprint"),
        "reason": result.get("reason"),
        "quality": {
            **metrics,
            "detour_factor": measured_detour,
            "detour_factor_source": "measured_from_published_path",
            "measurement_scope": "published_polyline_only",
            "heading_convention": "compass_degrees_0_north_clockwise",
        },
        "planner_reported": reported,
        "planner_reported_vs_measured": {
            "path_length_m_delta": (
                None if planned_length is None else planned_length - metrics["path_length_m"]
            ),
            "detour_factor_delta": (
                None
                if reported.get("detour_factor") is None or measured_detour is None
                else reported["detour_factor"] - measured_detour
            ),
            "note": (
                "planner_reported 为 planner 自身契约下的数值（V1 固定度网格 / V2 复用项目米制距离），"
                "measured 为本次 geodesic 度量；差值只用于说明度量口径差异。"
            ),
        },
        "hard_constraint_input": evaluate_constraint_feasibility(hard_constraints),
        "constraint_input_summary": {
            "hard_constraint_count": len(hard_constraints or []) if isinstance(hard_constraints, list) else None,
            "allowed_airspace_status": "not_applicable",
            "confirmed_allowed_feature_count": None,
            "allowed_grid_id_count": None,
            "allowed_edge_count": None,
            "source": "display_only_airspace_not_used_for_route_constraints",
        },
        "grid_behavior": grid_behavior,
        "allowed_airspace_feasibility": {
            "status": "not_applicable",
            "source": "display_only_airspace_not_used_for_route_constraints",
            "reason": None,
        },
        "risk_metrics": {
            name: reported[name] for name in _RISK_EVIDENCE_FIELDS if name in reported
        },
        "risk_metrics_source": (
            "read_from_existing_planner_output"
            if any(name in reported for name in _RISK_EVIDENCE_FIELDS) else "not_published_by_planner"
        ),
        "risk_recomputed": False,
        "runtime_ms": runtime_ms,
        "runtime_note": "runtime_ms 仅用于报告，不参与任何质量判定。",
        "metric_semantics": dict(METRIC_SEMANTICS),
        "verdicts": {
            "automatically_ranked": False,
            "automatically_scored": False,
            "preferred_algorithm": None,
        },
    }


class RoutePlanningDiagnostics:
    """Read-only façade for descriptive route diagnostics.

    The class deliberately delegates to :func:`evaluate_route_quality` and never
    receives a planner instance, so it cannot alter search behavior or results.
    """

    @staticmethod
    def evaluate(route, result, hard_constraints=None, runtime_ms=None, *, grid=None,
                 airspace_eligibility=None):
        return evaluate_route_quality(
            route, result, hard_constraints, runtime_ms,
            grid=grid, airspace_eligibility=airspace_eligibility,
        )


def _allowed_feasibility_status(status):
    if status == "passed":
        return "passed"
    if status == "failed":
        return "failed"
    if status == "not_applicable":
        return "not_applicable"
    return "unknown"
