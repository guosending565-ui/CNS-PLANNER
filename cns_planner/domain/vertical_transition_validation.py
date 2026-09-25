"""Vertical Transition Continuous Validation V1 — source-native climb/descent geometry.

This module closes the *only* geometry gap left by the production Route3DProfile V1: the
existing ``LayeredRouteValidation`` validates one **fixed cruise altitude** ``H`` along the
candidate's original two-dimensional centreline, and it deliberately never endorses the
climb or the descent.  This artifact validates exactly those two terminal transitions:

    ``departure_climb``    ``[0, s_join]``
    ``arrival_descent``    ``[s_leave, L]``

against the **real** native FABDEM pixels and the **real** building footprints, using the
unchanged production geometry helpers:

* :class:`cns_planner.validation.continuous_validators.MetricRoute`;
* :func:`cns_planner.validation.continuous_validators.validate_terrain`;
* :func:`cns_planner.validation.continuous_validators.validate_buildings`;
* :func:`cns_planner.domain.building_clearance.building_roof_elevation`.

What this artifact **is**::

    source_native_terminal_transition_geometry_validation

What it explicitly is **not** (and never claims)::

    * not a flight-dynamics / aircraft-kinematic validation;
    * not a terminal procedure certification;
    * not a regulatory (SORA / ARC / GRC) assessment;
    * not a route-safety conclusion;
    * not a Dubins Airplane / minimum-snap trajectory.

Hard semantics that must not be weakened:

* the *original* operational route polyline is truncated by route distance — a straight
  ``start → end`` chord is **never** substituted for it;
* the metric projection of every phase goes through one explicit ``horizontal_crs``;
* ``curve_chord_error_m`` is **0**: the truncated polyline *is* the validation geometry,
  not an approximation of another curve;
* NoData / missing height / missing ground / invalid geometry is ``unresolved`` — never
  filled with a default, never read as ``0``, never read as "safe";
* the horizontal building clearance used for the footprint query is ``0`` and is recorded
  verbatim as ``geometry_intersection_threshold``.  It is a *pure geometry intersection
  test*, **not** an engineering separation minimum, and this module never introduces a new
  default terrain or building clearance;
* an exact contact (``margin == 0``, e.g. a terminal endpoint touching the surface) is
  **not** a penetration;
* the verdict vocabulary is ``penetration`` / ``unresolved`` / ``validated`` — never
  ``safe`` / ``unsafe``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import TypedDict

from .route_3d_profile import VERTICAL_REFERENCE

#: Contract version of this artifact.
SCHEMA_VERSION = "vertical-transition-validation-v1"
COLLECTION_SCHEMA_VERSION = "vertical-transition-validation-collection-v1"
VALIDATOR_VERSION = "vertical_transition_source_native_geometry_validation_v1"
ARTIFACT_TYPE = "source_native_terminal_transition_geometry_validation"

#: ``current_applicability`` vocabulary of one stored transition validation.
VALIDATION_STATUSES = ("validated", "failed", "unresolved", "not_ready", "stale")

#: The two transition phases.  There is deliberately no ``cruise`` phase here — the cruise
#: segment is owned by the existing ``LayeredRouteValidation``.
TRANSITION_PHASE_IDS = ("departure_climb", "arrival_descent")

PHASE_LABELS = {
    "departure_climb": "离场爬升（departure climb，[0, s_join]）",
    "arrival_descent": "进场下降（arrival descent，[s_leave, L]）",
}

#: The two independent source-native evidence domains of every phase.
TRANSITION_DOMAIN_IDS = ("terrain", "building")

DOMAIN_LABELS = {
    "terrain": "Terrain（源生 FABDEM 像素）",
    "building": "Building（真实 footprint 几何相交）",
}

#: Where the truncated distance range of one phase comes from.
RANGE_SOURCE = "route_3d_profile_join_leave_distance_along_route"

#: How the truncated polyline was derived.  A straight start→end chord is forbidden.
TRUNCATION_SEMANTICS = (
    "original_operational_route_polyline_truncated_by_route_distance_"
    "never_a_start_to_end_straight_chord"
)

#: Numeric tolerance (metres) for "is this station the same station?".  It only decides
#: whether a boundary anchor is inserted or an existing vertex is reused; it never moves a
#: user-declared ``s_join`` / ``s_leave``.
TOLERANCE_ROUTE_M = 1e-6

#: The literal label recorded next to every clearance the transition geometry uses.
CLEARANCE_SEMANTICS = "geometry_intersection_threshold_not_an_engineering_clearance"

#: The clearance values this validator is allowed to use.  Both are **0** because the
#: transition check is a contact/intersection geometry test; no new clearance is invented.
GEOMETRY_INTERSECTION_THRESHOLD_M = 0.0

#: The recorded failure reasons of one phase (documentation of the fixed vocabulary; the
#: per-domain ``reason_id`` values produced by the unchanged validators are ``
#: below_native_terrain_clearance`` and ``building_footprint_penetration``).
PHASE_FAILURE_REASONS = ("terrain_penetration", "building_penetration")

#: The full evidence boundary of this artifact, carried verbatim by every record.
SEMANTICS = {
    "artifact_type": ARTIFACT_TYPE,
    "thin_geometry_validation_not_a_planner": True,
    "source_native_terrain_pixels": True,
    "real_building_footprint_geometry_intersection": True,
    "no_new_default_terrain_or_building_clearance": True,
    "horizontal_clearance_is_a_geometry_intersection_threshold": True,
    "curve_chord_error_m_is_zero": True,
    "original_operational_polyline_never_replaced_by_a_straight_chord": True,
    "nodata_or_unknown_is_not_zero": True,
    "unknown_is_never_safe": True,
    "exact_contact_is_not_a_penetration": True,
    "never_rewrites_route_3d_profile": True,
    "never_rewrites_layered_route_validation": True,
    "never_replans_or_repairs": True,
    "produces_safe_or_unsafe_verdict": False,
    "full_3d_geometry_validated_is_not_aircraft_kinematic_validation": True,
    "full_3d_geometry_validated_is_not_terminal_procedure_certification": True,
    "full_3d_geometry_validated_is_not_route_safe": True,
    "airspace_display_only_never_used": True,
    "no_new_regulatory_algorithm": True,
    "stale_transition_never_stales_the_theta_chain": True,
}

#: The explicit "what this is not" block.  Kept as data so the UI and the API can print it.
BOUNDARIES = {
    "is_flight_dynamics_validation": False,
    "is_aircraft_kinematic_validation": False,
    "is_terminal_procedure_certification": False,
    "is_regulatory_assessment": False,
    "is_route_safety_conclusion": False,
    "is_ground_clearance_certification": False,
    "produces_safe_or_unsafe_verdict": False,
    "uses_display_only_airspace": False,
    "statement": (
        "本产物只做 climb/descent 的源生 3D 几何验证（地形像素 + 真实建筑 footprint 相交），"
        "不判断真实飞行动力学可行性，不做终端程序认证，不做监管符合性评估，"
        "也不构成 route_safe 结论。"
    ),
}


class VerticalTransitionValidation(TypedDict, total=False):
    """One continuous source-native geometry validation of a route's two transitions."""

    validation_id: str
    route_id: str
    profile_id: str
    status: str
    current_applicability: str
    phases: list[dict]
    minimum_margins: dict
    unresolved_evidence: list[dict]
    fingerprints: dict
    provenance: dict
    limitations: list[str]
    created_at: str


# ---------------------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------------------


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def transition_fingerprint(components):
    """Fingerprint the declared dependency set of one transition validation."""

    from .route_safety_evidence_v2 import stable_fingerprint

    return stable_fingerprint(components, prefix="verticaltransitionv1-")


def empty_vertical_transition_validation(status="not_ready"):
    if status not in VALIDATION_STATUSES:
        status = "not_ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "validator_version": VALIDATOR_VERSION,
        "validation_id": None,
        "route_id": None,
        "profile_id": None,
        "status": status,
        "status_reason": None,
        "blocking_reasons": [],
        "current_applicability": "not_ready",
        "stale_reason": None,
        "created_at": None,
        "vertical_reference": VERTICAL_REFERENCE,
        "route_length_m": None,
        "phases": [],
        "phase_statuses": {},
        "minimum_margins": {"departure_climb_terrain_m": None, "departure_climb_building_m": None,
                            "arrival_descent_terrain_m": None, "arrival_descent_building_m": None},
        "failed_intervals": [],
        "unresolved_evidence": [],
        "failed_interval_count": 0,
        "unresolved_interval_count": 0,
        "source_type": None,
        "source_audits": {},
        "horizontal_crs": None,
        "clearance_usage": {
            "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
            "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
            "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
            "curve_chord_error_m": 0.0,
            "semantics": CLEARANCE_SEMANTICS,
            "new_default_clearance_introduced": False,
        },
        "validator_versions": {
            "terrain": "source_native_terrain_validator_v1@vertical_transition_clearance_zero",
            "building": "real_footprint_geometry_intersection_validator_v1@vertical_transition",
            "native_pixel_intervals": "native_pixel_interval_v1",
            "transition_geometry": VALIDATOR_VERSION,
        },
        "fingerprints": {
            "transition_fingerprint": None, "profile_fingerprint": None,
            "validation_fingerprint": None, "components": {},
        },
        "provenance": {},
        "limitations": [],
        "operational_route": False,
        "cns_assessed": False,
        "full_3d_geometry_validated": False,
        "semantics": deepcopy(SEMANTICS),
        "boundaries": deepcopy(BOUNDARIES),
    }


def empty_vertical_transition_validation_collection():
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "not_calculated",
        "count": 0,
        "active_validation_id": None,
        "items": [],
        "notes": [
            "Vertical Transition Validation V1 只验证 current ProductionRoute3DProfile 的 "
            "climb/descent 源生 3D 几何。",
            "cruise 段仍由既有 LayeredRouteValidation 负责，两者共同构成完整 3D geometry evidence。",
            "verdict 只有 validated / failed / unresolved / not_ready / stale，绝不输出 safe/unsafe。",
        ],
    }


def normalize_vertical_transition_validation(value):
    """Idempotent JSON-safe backfill.  Never repairs a conclusion."""

    if not isinstance(value, dict):
        return empty_vertical_transition_validation()
    status = str(value.get("status") or "not_ready")
    result = empty_vertical_transition_validation(status)
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result["artifact_type"] = ARTIFACT_TYPE
    result["validator_version"] = VALIDATOR_VERSION
    if result.get("status") not in VALIDATION_STATUSES:
        result["status"] = "not_ready"
    if result.get("current_applicability") not in (
        "current", "stale", "unresolved", "not_ready", "stale_inputs_changed",
        "stale_profile_not_current",
    ):
        result["current_applicability"] = "not_ready"
    result.setdefault("phases", [])
    result.setdefault("unresolved_evidence", [])
    result.setdefault("failed_intervals", [])
    result["semantics"] = {**deepcopy(SEMANTICS), **(
        result.get("semantics") if isinstance(result.get("semantics"), dict) else {}
    )}
    result["boundaries"] = deepcopy(BOUNDARIES)
    # These two are derived contract invariants, never mutable conclusions.
    result["operational_route"] = False
    result["cns_assessed"] = False
    result["full_3d_geometry_validated"] = result.get("status") == "validated"
    return result


def normalize_vertical_transition_validation_collection(value=None):
    source = value if isinstance(value, dict) else {}
    items = [
        normalize_vertical_transition_validation(item)
        for item in source.get("items") or [] if isinstance(item, dict)
    ]
    result = empty_vertical_transition_validation_collection()
    result.update(deepcopy(source))
    result["schema_version"] = COLLECTION_SCHEMA_VERSION
    result["items"] = items
    result["count"] = len(items)
    if result.get("active_validation_id") not in {item.get("validation_id") for item in items}:
        result["active_validation_id"] = None
    if not items:
        result["status"] = "not_calculated"
    return result


# ---------------------------------------------------------------------------------------
# z(s) sampling — the same piecewise-linear contract the Route3DProfile declares
# ---------------------------------------------------------------------------------------


def route_profile_altitude(waypoints, distance_m, route_length_m):
    """EGM2008 altitude at ``distance_m`` of a piecewise-linear ``z(s)`` profile.

    Mirrors the *unchanged* sampling contract of the profile consumers: strictly linear
    between adjacent waypoints, clamped to the profile extent.  Returns ``None`` when the
    profile cannot be sampled (no waypoints / non-finite length / non-finite altitudes),
    which the callers must treat as ``unresolved``.
    """

    if not waypoints:
        return None
    try:
        length = float(route_length_m)
    except (TypeError, ValueError):
        return None
    if length <= 0:
        return None
    points = []
    for item in waypoints:
        if not isinstance(item, dict):
            continue
        try:
            distance = float(item.get("distance_along_route_m"))
            altitude = float(item.get("altitude_m"))
        except (TypeError, ValueError):
            return None
        if distance != distance or altitude != altitude:
            return None
        points.append((distance, altitude))
    if not points:
        return None
    points.sort(key=lambda pair: pair[0])
    value = max(0.0, min(length, float(distance_m)))
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for left, right in zip(points, points[1:]):
        if value < left[0] or value > right[0]:
            continue
        span = right[0] - left[0]
        if span <= 0:
            return right[1]
        ratio = (value - left[0]) / span
        return left[1] + (right[1] - left[1]) * ratio
    return points[-1][1]


# ---------------------------------------------------------------------------------------
# geometry truncation — the original polyline, never a straight chord
# ---------------------------------------------------------------------------------------


def _point_at_distance(points, cumulative, distance_m):
    """Linear interpolation inside segment ``index`` of a densified polyline."""

    if not points:
        return None
    value = max(0.0, min(cumulative[-1], float(distance_m)))
    for index in range(len(points) - 1):
        start, end = cumulative[index], cumulative[index + 1]
        if value < start - 1e-9 or value > end + 1e-9:
            continue
        span = end - start
        if span <= 0:
            return [float(points[index][0]), float(points[index][1])]
        ratio = (value - start) / span
        return [
            float(points[index][0]) + (float(points[index + 1][0]) - float(points[index][0])) * ratio,
            float(points[index][1]) + (float(points[index + 1][1]) - float(points[index][1])) * ratio,
        ]
    return [float(points[-1][0]), float(points[-1][1])]


def truncate_waypoints(points, start_m, end_m, *, tolerance_m=TOLERANCE_ROUTE_M):
    """Truncate a polyline to the route-distance window ``[start_m, end_m]``.

    Every vertex whose along-track distance lies strictly inside the window is **kept**:
    the result is the original polyline, not a straight ``start → end`` chord.  The two
    boundary stations are inserted as exact anchors (deduplicated when they coincide with a
    kept vertex).  Returns ``(points, meta)``.
    """

    from math import hypot

    tolerance = max(1e-9, float(tolerance_m))
    clean = [
        [float(point[0]), float(point[1])]
        for point in points or []
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    meta = {
        "input_vertex_count": len(clean),
        "kept_interior_vertex_count": 0,
        "boundary_anchors_inserted": 0,
        "truncation_semantics": TRUNCATION_SEMANTICS,
        "straight_start_to_end_chord_used": False,
    }
    if len(clean) < 2:
        return [], meta
    cumulative = [0.0]
    for left, right in zip(clean, clean[1:]):
        cumulative.append(cumulative[-1] + hypot(right[0] - left[0], right[1] - left[1]))
    total = cumulative[-1]
    if total <= 0:
        return [], meta
    start, end = float(start_m), float(end_m)
    if end < start:
        start, end = end, start
    start = max(0.0, start)
    end = min(total, end)
    if end < start:
        return [], meta
    truncated = []
    distances = []
    interior = 0
    for index, point in enumerate(clean):
        if cumulative[index] < start - tolerance or cumulative[index] > end + tolerance:
            continue
        if 0 < index < len(clean) - 1 and start + tolerance < cumulative[index] < end - tolerance:
            interior += 1
        if not truncated or truncated[-1] != point:
            truncated.append(point)
            distances.append(cumulative[index])
    if not truncated:
        # A degenerate window that contains no vertex at all (for example ``[L - ε, L]`` on the
        # final segment): keep the final segment and let the boundary anchors handle the rest.
        if start < total - tolerance:
            truncated = [list(clean[-2]), list(clean[-1])]
            distances = [cumulative[-2], cumulative[-1]]
    if not truncated:
        return [], meta
    # A window edge that falls strictly inside the segment the first (or last) retained vertex
    # belongs to must be represented by an exact anchor.  When the edge is *before* that vertex
    # the anchor becomes the new first vertex; when it is *after* it the anchor becomes the new
    # last vertex.  An edge that lands on an existing vertex (within the numeric tolerance) must
    # not get a second, almost coincident vertex: two vertices at nearly the same
    # route-distance with different altitudes would make the piecewise-linear ``z(s)``
    # ambiguous again — exactly what this validator must never produce.
    boundary = 0
    head = _point_at_distance(clean, cumulative, start)
    tail = _point_at_distance(clean, cumulative, end)
    if start < distances[0] - tolerance and start < end - tolerance and truncated[0] != head:
        truncated.insert(0, head)
        distances.insert(0, start)
        boundary += 1
    if end > distances[-1] + tolerance and end > start + tolerance and truncated[-1] != tail:
        truncated.append(tail)
        distances.append(end)
        boundary += 1
    kept_distances = [
        min(max(value, start), end) for value in distances
    ]
    meta["kept_interior_vertex_count"] = interior
    meta["boundary_anchors_inserted"] = boundary
    #: The **absolute** route distance of every returned vertex.  The caller needs it because
    #: the geometric length of the truncated polyline is *not* the profile's distance basis:
    #: the route-distance parameterisation is the operational ``path_length_m`` of the whole
    #: route, so a short window can still carry a large route distance.
    meta["vertex_distances_along_route_m"] = [float(value) for value in kept_distances]
    meta["window"] = {"start_distance_along_route_m": start, "end_distance_along_route_m": end}
    return truncated, meta


def metric_route_from_phase(points_metric, *, z_start_m, z_end_m, waypoints, route_length_m,
                            vertex_distances_along_route_m=None, distance_scale=1.0,
                            curve_chord_error_m=0.0):
    """A ``curve_chord_error_m == 0`` :class:`MetricRoute` for one truncated transition.

    The metric polyline is the truncated original polyline; ``z`` is the Route3DProfile's
    piecewise-linear ``z(s)`` resampled at every metric vertex.  ``curve_chord_error_m`` is
    deliberately ``0``: the truncated polyline *is* the validation geometry.

    ``vertex_distances_along_route_m`` (as returned by :func:`truncate_waypoints`) is the
    along-track distance of each vertex in the *truncation frame*; ``distance_scale`` converts
    it back into the profile's own route-distance basis (``metric_m / route_m``).  Both are
    required in general, because the *geometric* length of a short window is not the
    route-distance window of the profile.
    """

    from ..validation.continuous_validators import MetricRoute

    points = [
        [float(point[0]), float(point[1])]
        for point in points_metric or []
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if len(points) < 2:
        raise ValueError("transition phase 需要至少两个 metric 顶点")
    from math import hypot

    cumulative = [0.0]
    for left, right in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + hypot(right[0] - left[0], right[1] - left[1]))
    length = cumulative[-1]
    if length <= 0:
        raise ValueError("transition phase 的 metric 长度必须大于零")
    try:
        scale = float(distance_scale)
    except (TypeError, ValueError):
        raise ValueError("transition phase 的 distance_scale 必须是有限数值") from None
    if not scale > 0:
        raise ValueError("transition phase 的 distance_scale 必须大于零")
    route_distances = [
        float(value) / scale for value in (vertex_distances_along_route_m or [])
    ]
    if len(route_distances) != len(points):
        raise ValueError(
            "transition phase 缺少每个顶点的 route distance"
            "（几何长度不是 profile 的 distance basis）"
        )
    primitives = []
    for index in range(len(points) - 1):
        start, end = cumulative[index], cumulative[index + 1]
        if index == 0:
            z_from = float(z_start_m)
        else:
            z_from = route_profile_altitude(waypoints, route_distances[index], route_length_m)
        if index == len(points) - 2:
            z_to = float(z_end_m)
        else:
            z_to = route_profile_altitude(
                waypoints, route_distances[index + 1], route_length_m,
            )
        if z_from is None or z_to is None:
            raise ValueError("transition phase 的 z(s) 无法解析")
        primitives.append({
            "primitive_id": f"transition-{index}",
            "kind": "line",
            "distance_start_m": start,
            "distance_end_m": end,
            "horizontal_length_m": end - start,
            "z_start_egm2008_m": float(z_from),
            "z_end_egm2008_m": float(z_to),
            "gradient": None if end - start <= 0 else (float(z_to) - float(z_from)) / (end - start),
        })
    route = {
        "status": "realized",
        "horizontal_geometry": {
            "linearized": {
                "linestring_metric": points,
                "curve_chord_error_m": float(curve_chord_error_m or 0.0),
                "method": TRUNCATION_SEMANTICS,
                "point_count": len(points),
                "actual_max_chord_error_m": 0.0,
            },
        },
        "primitives": primitives,
        "vertical_reference": VERTICAL_REFERENCE,
        "total_distance_m": length,
        "semantics": {
            "strategic_route_centerline_not_aircraft_kinematic_trajectory": True,
            "truncated_original_polyline_not_a_straight_chord": True,
            "curve_chord_error_m_is_zero": True,
            "z_is_piecewise_linear_route_3d_profile": True,
        },
    }
    return MetricRoute(route), route, {
        "metric_length_m": length,
        "metric_vertex_count": len(points),
        "primitive_count": len(primitives),
    }


# ---------------------------------------------------------------------------------------
# per-phase verdicts
# ---------------------------------------------------------------------------------------


def empty_phase_result(phase_id):
    if phase_id not in TRANSITION_PHASE_IDS:
        raise ValueError(f"未知 transition phase：{phase_id!r}")
    return {
        "phase_id": phase_id,
        "label": PHASE_LABELS[phase_id],
        "status": "unresolved",
        "reason": None,
        "range": {},
        "geometry": {},
        "domains": {},
        "domain_statuses": {},
        "minimum_margins": {"terrain_vertical_m": None, "building_vertical_m": None},
        "failed_intervals": [],
        "unresolved_evidence": [],
        "penetration": False,
        "evidence": {},
        "semantics": {
            "original_polyline_truncated_not_a_straight_chord": True,
            "curve_chord_error_m_is_zero": True,
            "horizontal_building_clearance_is_a_geometry_intersection_threshold": True,
            "unknown_is_never_safe": True,
            "exact_contact_is_not_a_penetration": True,
        },
    }


def _phase_status(terrain, building):
    """Fixed per-phase mapping: penetration ⇒ failed, otherwise unknown ⇒ unresolved."""

    statuses = {str(terrain.get("status") or ""), str(building.get("status") or "")}
    if "failed" in statuses:
        return "failed", "penetration"
    if "unresolved" in statuses:
        return "unresolved", "unresolved_evidence"
    if statuses == {"passed"}:
        return "validated", None
    return "unresolved", "evidence_not_resolved"


def building_domain_result(metric_route, *, evidence, to_geographic=None):
    """Real-footprint building **geometry intersection** validation of one transition.

    The footprint geometry is intersected with the truncated route **exactly**
    (``horizontal_clearance_m = 0``, ``curve_chord_error_m = 0``) — this zero is recorded
    verbatim as ``geometry_intersection_threshold`` and is *not* an engineering separation
    minimum.  The roof elevation keeps the *shared* ``ground + height`` semantics
    (:func:`cns_planner.domain.building_clearance.building_roof_elevation`), and the margin is

        ``margin = minimum_z(intersection interval) - roof_elevation``

    so a route penetrating the building prism produces a negative margin.  Missing height /
    missing ground / invalid footprint geometry is ``unresolved``, never a pass and never a
    fabricated number.
    """

    from .building_clearance import building_roof_elevation
    from .building_geometry_quality import (
        empty_geometry_quality_report, merge_geometry_quality, prepare_footprint_polygons,
    )
    from ..validation.continuous_contracts import (
        TOLERANCE, empty_domain_result, violation_interval,
    )
    from ..validation.continuous_validators import _coordinates

    result = empty_domain_result("building", "passed")
    result["evaluated"] = True
    footprints = list((evidence or {}).get("buildings") or [])
    result["evidence"] = {
        "source": (evidence or {}).get("source") or {},
        "building_count": len(footprints),
        "horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "curve_error_m": 0.0,
        "clearance_semantics": CLEARANCE_SEMANTICS,
        "metric_buffer_distance_m": 0.0,
        "roof_semantics": "shared_building_clearance_roof_elevation_helper",
        "margin_semantics": "minimum_z_minus_roof_elevation_m",
        "source_geometry_modified": False,
        "make_valid_applied": False,
        "geometry_intersection_test": True,
        "query": "route_corridor_provider_spatial_index_exact_polygon_intersection",
    }
    result["semantics"] = {
        "real_footprint_geometry_intersection": True,
        "horizontal_clearance_zero_is_a_geometry_threshold_not_a_clearance_parameter": True,
        "exact_contact_margin_zero_is_not_a_penetration": True,
        "unknown_is_never_safe": True,
    }
    if not (evidence or {}).get("available", True):
        return _building_unresolved_only(result, "building_source_unavailable", {
            "required": "configured_building_source_with_spatial_index", "observed": None,
        })
    minimum_vertical = None
    unresolved = []
    geometry_quality = empty_geometry_quality_report()
    for footprint in footprints:
        identifier = footprint.get("building_id")
        # Same geometry quality gate as the cruise validator: never rewrite the source ring,
        # repair with shapely ``make_valid`` when that yields a valid polygon, and keep an
        # unrepairable ring ``unresolved`` instead of inventing a number.
        prepared = prepare_footprint_polygons(footprint)
        for record in prepared["records"]:
            merge_geometry_quality(geometry_quality, record)
        if prepared["quality"] == "invalid":
            unresolved.append({
                "building_id": identifier,
                "reason": "building_footprint_quality_unresolved",
                "internal_geometry_quality_status": prepared["quality"],
                "internal_geometry_quality": prepared["annotation"],
            })
            continue
        roof = building_roof_elevation(
            footprint.get("ground_elevation_max_egm2008_m"), footprint.get("height_m"),
        )
        if roof.get("status") != "resolved":
            unresolved.append({
                "building_id": identifier, "reason": roof.get("reason"),
                "ground_elevation_max_egm2008_m": footprint.get("ground_elevation_max_egm2008_m"),
                "height_m": footprint.get("height_m"),
            })
            continue
        roof_m = float(roof["roof_elevation_egm2008_m"])
        # Every part is validated: ignoring a smaller part could hide a penetration.
        for piece_index, polygon in enumerate(prepared["polygons"]):
            intersection = metric_route.line.intersection(polygon)
            if intersection.is_empty:
                continue
            distances = [metric_route.distance_of([x, y]) for x, y in _coordinates(intersection)]
            if not distances:
                continue
            start, end = min(distances), max(distances)
            observed = metric_route.minimum_z(start, end)
            if observed is None:
                unresolved.append({
                    "building_id": identifier,
                    "reason": "realized_altitude_profile_unresolved_for_building_interval",
                    "start_distance_m": start, "end_distance_m": end,
                    "footprint_part_index": piece_index,
                })
                continue
            margin = float(observed) - roof_m
            minimum_vertical = margin if minimum_vertical is None else min(minimum_vertical, margin)
            if margin < -TOLERANCE:
                result["violations"].append(violation_interval(
                    domain="building", reason_id="building_footprint_penetration",
                    start_distance_m=start, end_distance_m=end,
                    start_point=metric_route.to_geographic(
                        metric_route.sample_at(start), to_geographic=to_geographic,
                    ),
                    end_point=metric_route.to_geographic(
                        metric_route.sample_at(end), to_geographic=to_geographic,
                    ),
                    required=roof_m, observed=observed, margin=margin,
                    evidence={
                        "building_id": identifier,
                        "building_source": footprint.get("source"),
                        "height_m": footprint.get("height_m"),
                        "ground_elevation_max_egm2008_m": footprint.get(
                            "ground_elevation_max_egm2008_m"
                        ),
                        "roof_elevation_egm2008_m": roof_m,
                        "horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
                        "curve_error_m": 0.0,
                        "clearance_semantics": CLEARANCE_SEMANTICS,
                        "interval_length_m": float(end) - float(start),
                        "footprint_part_index": piece_index,
                        "footprint_part_count": len(prepared["polygons"]),
                    },
                ))
    result["evidence"]["geometry_quality"] = geometry_quality
    result["evidence"]["building_quality_report"] = geometry_quality
    result["evidence"]["source_modified"] = False
    # Reporting only: repairing geometry does not relax the penetration criterion.
    result["evidence"]["make_valid_applied"] = bool(geometry_quality["repair"]["applied_count"])
    result["evidence"]["prefer_shapely_make_valid"] = True
    result["evidence"]["unrepairable_geometry_stays_unknown"] = True
    result["minimum_margin"] = minimum_vertical
    result["item_count"] = len(footprints)
    result["failed_interval_count"] = len(result["violations"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = "building_footprint_penetration"
        return result
    if unresolved:
        result["status"] = "unresolved"
        result["reason"] = "building_evidence_unresolved"
        for item in unresolved:
            start, end = item.get("start_distance_m"), item.get("end_distance_m")
            result["unresolved"].append(violation_interval(
                domain="building", reason_id=item.get("reason") or "building_evidence_unresolved",
                start_distance_m=start, end_distance_m=end,
                start_point=(
                    None if start is None else metric_route.to_geographic(
                        metric_route.sample_at(start), to_geographic=to_geographic,
                    )
                ),
                end_point=(
                    None if end is None else metric_route.to_geographic(
                        metric_route.sample_at(end), to_geographic=to_geographic,
                    )
                ),
                required="confirmed_height_and_ground_and_valid_geometry",
                observed="unresolved", evidence=item,
            ))
        result["unresolved_interval_count"] = len(result["unresolved"])
        return result
    result["status"] = "passed"
    return result


def _building_unresolved_only(result, reason, evidence):
    from ..validation.continuous_contracts import violation_interval

    result["status"] = "unresolved"
    result["reason"] = reason
    result["unresolved"] = [violation_interval(
        domain="building", reason_id=reason,
        required=evidence.get("required"), observed=evidence.get("observed"),
        evidence=evidence,
    )]
    result["unresolved_interval_count"] = 1
    return result


def evaluate_phase(phase_id, *, metric_route, terrain_evidence, building_evidence,
                   to_geographic=None):
    """Run the two source-native domain validators on one transition phase.

    Terrain uses the *unchanged* production ``validate_terrain`` with an explicit
    ``clearance == 0``.  Buildings use :func:`building_domain_result`, which keeps the shared
    ``ground + height`` roof semantics and the real-footprint geometry intersection but
    computes ``margin = minimum_z - roof`` directly — the shared
    ``evaluate_vertical_clearance`` helper clamps an aircraft below the roof to a *zero*
    clearance (a prism-interior contact), which cannot express the negative margin a
    footprint penetration must produce.
    """

    from ..validation.continuous_validators import validate_terrain

    phase = empty_phase_result(phase_id)
    policy = {
        "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "use_curve_error_envelope": False,
    }
    terrain = validate_terrain(
        metric_route, evidence=terrain_evidence or {}, policy=policy,
        to_geographic=to_geographic,
    )
    building = building_domain_result(
        metric_route, evidence=building_evidence or {}, to_geographic=to_geographic,
    )
    phase["domains"] = {"terrain": deepcopy(terrain), "building": deepcopy(building)}
    phase["domain_statuses"] = {name: (phase["domains"][name] or {}).get("status")
                                for name in TRANSITION_DOMAIN_IDS}
    phase["minimum_margins"] = {
        "terrain_vertical_m": terrain.get("minimum_margin"),
        "building_vertical_m": building.get("minimum_margin"),
    }
    phase["failed_intervals"] = [
        deepcopy(item) for name in TRANSITION_DOMAIN_IDS
        for item in (phase["domains"][name] or {}).get("violations") or []
    ]
    phase["unresolved_evidence"] = [
        deepcopy(item) for name in TRANSITION_DOMAIN_IDS
        for item in (phase["domains"][name] or {}).get("unresolved") or []
    ]
    status, reason = _phase_status(terrain, building)
    phase["status"] = status
    phase["reason"] = reason
    phase["penetration"] = status == "failed"
    phase["evidence"] = {
        "terrain_semantics": (terrain.get("evidence") or {}).get("terrain_semantics"),
        "terrain_source": deepcopy((terrain.get("evidence") or {}).get("source") or {}),
        "terrain_pixel_count": (terrain.get("evidence") or {}).get("pixel_count"),
        "building_source": deepcopy((building.get("evidence") or {}).get("source") or {}),
        "building_count": (building.get("evidence") or {}).get("building_count"),
        "building_horizontal_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "building_vertical_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "terrain_clearance_m": GEOMETRY_INTERSECTION_THRESHOLD_M,
        "curve_chord_error_m": metric_route.curve_chord_error_m,
        "clearance_semantics": CLEARANCE_SEMANTICS,
        "margin_semantics": "minimum_z_minus_surface_elevation_m",
    }
    return phase


def evaluate_transition_verdict(phases):
    """The transition-level verdict.  Never ``safe`` / ``unsafe``.

    * any definite penetration ⇒ ``failed``;
    * no penetration but remaining unknown evidence ⇒ ``unresolved``;
    * both phases fully resolved without penetration ⇒ ``validated``.
    """

    if not phases:
        return "not_ready", "no_transition_phase_evaluated"
    statuses = [str(item.get("status") or "unresolved") for item in phases]
    if "failed" in statuses:
        return "failed", "confirmed_penetration_in_at_least_one_transition_phase"
    if any(status != "validated" for status in statuses):
        return "unresolved", "transition_evidence_not_fully_resolved"
    return "validated", None


# ---------------------------------------------------------------------------------------
# record assembly
# ---------------------------------------------------------------------------------------


def transition_geometry(profile, route_id=None):
    """The two explicit transition ranges ``[0, s_join]`` / ``[s_leave, L]``.

    A profile whose ``z(s)`` cannot unambiguously represent both endpoints
    (``z0 != H`` with ``s_join == 0``, or ``z1 != H`` with ``s_leave == L``) is reported as
    ``unresolved`` here instead of being repaired: a distance-parametric profile cannot hold
    two altitudes at one station.
    """

    result = {
        "route_id": route_id or (profile or {}).get("route_id"),
        "status": "resolved",
        "reasons": [],
        "route_length_m": None,
        "cruise_altitude_m": None,
        "phases": {},
    }
    if not isinstance(profile, dict):
        result["status"] = "unresolved"
        result["reasons"].append("缺少 ProductionRoute3DProfile")
        return result
    join_leave = profile.get("join_leave") or {}
    length = profile.get("route_length_m")
    cruise = profile.get("cruise_altitude_m")
    terminals = profile.get("terminal_altitude_m") or {}
    join = join_leave.get("join_distance_along_route_m")
    leave = join_leave.get("leave_distance_along_route_m")
    z0 = terminals.get("departure_egm2008_m")
    z1 = terminals.get("arrival_egm2008_m")
    for name, value in (
        ("route_length_m", length), ("cruise_altitude_m", cruise),
        ("s_join", join), ("s_leave", leave), ("z0", z0), ("z1", z1),
    ):
        if value in (None, ""):
            result["status"] = "unresolved"
            result["reasons"].append(f"profile 缺少显式 {name}：不推断")
    if result["status"] != "resolved":
        return result
    try:
        length = float(length)
        cruise = float(cruise)
        join = float(join)
        leave = float(leave)
        z0 = float(z0)
        z1 = float(z1)
    except (TypeError, ValueError):
        result["status"] = "unresolved"
        result["reasons"].append("profile 的几何数值不是有限数")
        return result
    if length <= 0:
        result["status"] = "unresolved"
        result["reasons"].append("route_length_m 必须大于零")
    if join < 0 or leave < 0 or join > length or leave > length or join > leave:
        result["status"] = "unresolved"
        result["reasons"].append("s_join/s_leave 越界或相互重叠：不修正用户输入")
    if result["status"] != "resolved":
        return result
    if z0 != cruise and join <= 0:
        result["status"] = "unresolved"
        result["reasons"].append(
            "zero_horizontal_vertical_jump_at_the_departure_endpoint："
            "z0 != H 但 s_join == 0，distance-parametric z(s) 无法无歧义表示同一 s 的两个高度"
        )
    if z1 != cruise and leave >= length:
        result["status"] = "unresolved"
        result["reasons"].append(
            "zero_horizontal_vertical_jump_at_the_arrival_endpoint："
            "z1 != H 但 s_leave == L，distance-parametric z(s) 无法无歧义表示同一 s 的两个高度"
        )
    if result["status"] != "resolved":
        return result
    result.update({
        "route_length_m": length,
        "cruise_altitude_m": cruise,
        "phases": {
            "departure_climb": {
                "phase_id": "departure_climb",
                "start_distance_along_route_m": 0.0,
                "end_distance_along_route_m": join,
                "length_m": join,
                "z_start_m": z0,
                "z_end_m": cruise,
                "range_source": RANGE_SOURCE,
            },
            "arrival_descent": {
                "phase_id": "arrival_descent",
                "start_distance_along_route_m": leave,
                "end_distance_along_route_m": length,
                "length_m": length - leave,
                "z_start_m": cruise,
                "z_end_m": z1,
                "range_source": RANGE_SOURCE,
            },
        },
    })
    return result


def assemble_record(*, route_id, profile_id, phase_results, geometry, components, fingerprint,
                    source_audits, horizontal_crs, source_type, provenance,
                    created_at=None, status=None, reason=None, blocking_reasons=None,
                    profile_applicability="current"):
    """Build the stored record from the evaluated phases (or from a readiness block)."""

    resolved_status = status or evaluate_transition_verdict(phase_results)[0]
    record = empty_vertical_transition_validation(resolved_status)
    phases = [deepcopy(item) for item in phase_results or []]
    margins = {}
    for item in phases:
        phase_margins = item.get("minimum_margins") or {}
        margins[f"{item['phase_id']}_terrain_m"] = phase_margins.get("terrain_vertical_m")
        margins[f"{item['phase_id']}_building_m"] = phase_margins.get("building_vertical_m")
    record.update({
        "validation_id": "VTV-" + str(fingerprint or "")[-12:].upper(),
        "route_id": route_id,
        "profile_id": profile_id,
        "status": resolved_status,
        "status_reason": reason if reason is not None else evaluate_transition_verdict(
            phase_results
        )[1],
        "blocking_reasons": deepcopy(blocking_reasons or []),
        "current_applicability": (
            "current" if resolved_status in ("validated", "failed", "unresolved")
            else "not_ready"
        ),
        "created_at": created_at or utc_now(),
        "route_length_m": geometry.get("route_length_m"),
        "phases": phases,
        "phase_statuses": {item["phase_id"]: item["status"] for item in phases},
        "minimum_margins": margins,
        "failed_intervals": [
            deepcopy(item) for phase in phases for item in phase.get("failed_intervals") or []
        ],
        "unresolved_evidence": [
            {"phase_id": phase["phase_id"], **deepcopy(item)}
            for phase in phases for item in phase.get("unresolved_evidence") or []
        ],
        "source_type": source_type,
        "source_audits": deepcopy(source_audits or {}),
        "horizontal_crs": horizontal_crs,
        "fingerprints": {
            "transition_fingerprint": fingerprint,
            "profile_fingerprint": components.get("route_3d_profile_fingerprint"),
            "validation_fingerprint": components.get("cruise_validation_fingerprint"),
            "components": deepcopy(components),
        },
        "provenance": deepcopy(provenance or {}),
        "profile_applicability": profile_applicability,
    })
    record["failed_interval_count"] = len(record["failed_intervals"])
    record["unresolved_interval_count"] = len(record["unresolved_evidence"])
    record["full_3d_geometry_validated"] = resolved_status == "validated"
    record["limitations"] = transition_limitations(record)
    return record


def transition_limitations(record):
    status = str((record or {}).get("status") or "")
    limitations = [
        "本验证只覆盖 climb/descent 的源生几何：地形按 FABDEM 原生像素、"
        "建筑按真实 footprint 几何相交判定。",
        "cruise 段由既有 LayeredRouteValidation 负责；两者共同构成完整 3D geometry evidence。",
        "full_3d_geometry_validated 只表示 3D 几何证据完整：它**不是** aircraft kinematic "
        "validation、不是 terminal procedure certification，也不是 route_safe。",
        "本产物不输出 safe/unsafe，不进入 Theta* cost，也不改写 Route3DProfile 或 "
        "LayeredRouteValidation。",
    ]
    if status == "failed":
        limitations.append(
            "存在明确 penetration（margin < 0）：该段几何确实与地表/屋顶相交。"
            "这不等于整条航路不安全，也不表示任何飞行可行性结论。"
        )
    elif status == "unresolved":
        limitations.append(
            "存在未解析证据（NoData / 缺 height / 缺 ground / 无效几何）："
            "unresolved 既不是 pass 也不是 penetration。"
        )
    elif status == "not_ready":
        limitations.append(
            "readiness 未满足：没有任何源生几何证据被评估。not_ready 既不是 pass 也不是 failure。"
        )
    return limitations


def current_transition_validation(state):
    """The current (``current_applicability == 'current'``) transition validation, if any.

    Shared read-only helper.  It never recomputes anything and never prefers a stale record
    over the absence of one.
    """

    collection = normalize_vertical_transition_validation_collection(
        (state or {}).get("vertical_transition_validations")
    )
    for item in reversed(collection["items"]):
        if item.get("current_applicability") == "current":
            return item
    return None


__all__ = [
    "ARTIFACT_TYPE", "BOUNDARIES", "CLEARANCE_SEMANTICS", "COLLECTION_SCHEMA_VERSION",
    "DOMAIN_LABELS", "GEOMETRY_INTERSECTION_THRESHOLD_M",
    "PHASE_FAILURE_REASONS", "PHASE_LABELS", "RANGE_SOURCE",
    "SCHEMA_VERSION", "SEMANTICS", "TOLERANCE_ROUTE_M",
    "TRANSITION_DOMAIN_IDS", "TRANSITION_PHASE_IDS",
    "TRUNCATION_SEMANTICS",
    "VALIDATION_STATUSES", "VALIDATOR_VERSION", "VerticalTransitionValidation",
    "assemble_record", "building_domain_result", "current_transition_validation",
    "empty_phase_result", "empty_vertical_transition_validation",
    "empty_vertical_transition_validation_collection", "evaluate_phase",
    "evaluate_transition_verdict",
    "metric_route_from_phase", "normalize_vertical_transition_validation",
    "normalize_vertical_transition_validation_collection", "route_profile_altitude",
    "transition_fingerprint", "transition_geometry", "transition_limitations",
    "truncate_waypoints", "utc_now",
]
