"""Independent route-quality measurement for expert review.

This evaluator exists *outside* the planners.  It never writes into a V1/V2 result
dict and it never re-derives risk: when a planner already published authoritative
risk metrics (V2 ``distance_m`` / ``risk_exposure_index_m`` / ``mean_risk_index`` /
``max_risk_index`` / ``detour_factor``) those values are read back and reported as
``planner_reported``.  Everything else is measured from the published polyline.

Feasibility is reported from the existing authoritative results
(:func:`cns_planner.application.constraint_validation.validate_hard_constraints` for
hard constraints and the planner's own ``status`` for allowed-airspace feasibility).
No verdict, ranking or recommendation is produced.
"""

from __future__ import annotations

import math

from ..algorithms.coverage.v1 import distance_m
from ..application.constraint_validation import validate_hard_constraints

__all__ = [
    "evaluate_route_quality",
    "evaluate_constraint_feasibility",
    "polyline_metrics",
    "heading_deg",
]

#: Shared, code-level tolerance for treating a published vertex as duplicated or a
#: published segment as degenerate.
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


def heading_deg(left, right):
    """Compass heading of ``left -> right`` in degrees (0 = north, 90 = east)."""

    return math.degrees(math.atan2(right[0] - left[0], right[1] - left[1])) % 360.0


def _clean_points(path):
    """Return the published vertices, dropping exact/near duplicates only."""

    points = []
    for item in path or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        lon, lat = item[0], item[1]
        if isinstance(lon, bool) or isinstance(lat, bool):
            continue
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        if not (math.isfinite(float(lon)) and math.isfinite(float(lat))):
            continue
        point = [float(lon), float(lat)]
        if points and distance_m(points[-1], point) <= GEOMETRY_TOLERANCE_M:
            continue
        points.append(point)
    return points


def polyline_metrics(path):
    """Measure the published polyline. Pure geometry, no planner involvement."""

    points = _clean_points(path)
    lengths = [distance_m(left, right) for left, right in zip(points, points[1:])]
    headings = [heading_deg(left, right) for left, right in zip(points, points[1:])]
    changes = []
    for left, right in zip(headings, headings[1:]):
        delta = abs(right - left) % 360.0
        changes.append(min(delta, 360.0 - delta))
    return {
        "path_length_m": sum(lengths),
        "segment_count": len(lengths),
        "turn_count": len(changes),
        "total_heading_change_deg": sum(changes),
        "max_heading_change_deg": max(changes) if changes else 0.0,
        "min_segment_m": min(lengths) if lengths else 0.0,
        "vertex_count": len(points),
        "straight_line_distance_m": (
            distance_m(points[0], points[-1]) if len(points) > 1 else 0.0
        ),
    }


def evaluate_constraint_feasibility(hard_constraints):
    """Fail-closed hard-constraint verdict from the authoritative validator."""

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


def evaluate_route_quality(route, result, hard_constraints=None, runtime_ms=None):
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
        },
        "hard_constraint_input": evaluate_constraint_feasibility(hard_constraints),
        "allowed_airspace_feasibility": {
            "status": _allowed_feasibility_status(status),
            "source": "planner_result_status_only_not_rejudged",
            "reason": result.get("reason"),
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
        "verdicts": {
            "automatically_ranked": False,
            "automatically_scored": False,
            "preferred_algorithm": None,
        },
    }


def _allowed_feasibility_status(status):
    if status == "passed":
        return "passed"
    if status == "failed":
        return "failed"
    if status == "not_applicable":
        return "not_applicable"
    return "unknown"
