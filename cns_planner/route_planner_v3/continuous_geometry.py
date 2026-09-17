"""V3-C continuous geometry realization: polyline → C1 straight/arc geometry.

The realizer works **inside the V3-B local metric frame**: the V3-B
``refined_candidate`` already carries its metric trajectory
(``metric_projection``) and the per-state EGM2008 altitude, so no projection is
invented here.  This module is pure: no file access, no QGIS/GDAL, no safety value
is guessed.

What it guarantees, and what it deliberately does **not**:

* **C1 only** -- position and heading are continuous.  Curvature is allowed to jump
  where a straight meets an arc.  V3-C never claims continuous curvature / C2 and
  never invents a clothoid (future work);
* every internal turn becomes an explicit **circular-arc fillet** whose radius is
  ``>= aircraft_min_turn_radius_m``.  V3-C takes ``R = Rmin`` and never reduces it
  to make a turn fit: if the tangent length ``R·tan(|Δψ|/2)`` does not fit the
  usable neighbouring segments, or two adjacent fillets overlap, or the geometry
  degenerates, the realization reports ``turn_realization_failed`` with
  ``replan_required``;
* the arc is kept as **analytic geometry** (centre, radius, start/end angle,
  tangent points, arc length) *and* linearized with an **explicit**
  ``curve_chord_error_m``.  The linearized LineString is an approximation of known
  bound: ``actual_max_chord_error_m`` is recomputed from the realized geometry and
  must not exceed the requested tolerance.  There is no default tolerance and no
  claim that the sampled polyline *is* the mathematical curve;
* the vertical profile is the **EGM2008 orthometric altitude against the realized
  along-track distance**: tangent-point altitudes come from linear interpolation on
  the corresponding V3-B segment, arc interiors interpolate between the two tangent
  altitudes.
"""

from __future__ import annotations

from math import acos, atan2, ceil, cos, degrees, hypot, isfinite, pi, sin, tan

from .continuous_contracts import (
    ANALYTIC_VS_LINEARIZED, CONTINUITY_SEMANTICS, CURVE_ERROR_ENVELOPE_SEMANTICS,
    TOLERANCE, empty_circular_arc, empty_continuous_primitive, empty_continuous_route,
    empty_linearization, empty_turn_realization, finite_number,
)

#: How the fillet radius is chosen.  V3-C takes exactly the minimum radius.
TURN_RADIUS_POLICY = "radius_equals_explicit_minimum_never_reduced_to_fit"

#: How the arc is linearized.
CHORD_LINEARIZATION_METHOD = "equal_angle_chord_sagitta_bounded_by_explicit_curve_chord_error"

#: Failure reasons of the turn realization (there is no "reduced radius" outcome).
TURN_FAILURE_REASONS = (
    "turn_realization_failed_segment_too_short_for_minimum_turn_radius",
    "turn_realization_failed_adjacent_fillets_overlap",
    "turn_realization_failed_degenerate_geometry",
    "turn_realization_failed_invalid_minimum_turn_radius",
    "turn_realization_failed_chord_error_tolerance_unresolved",
    "turn_realization_failed_chord_error_bound_exceeded",
    "turn_realization_failed_non_finite_geometry",
)

#: Turn angles below this magnitude are treated as collinear (the vertex is dropped).
_COLLINEAR_TOLERANCE_RAD = 1e-12

#: Vertical-profile method string (canonical EGM2008 against realized distance).
VERTICAL_METHOD = "egm2008_orthometric_altitude_against_realized_along_track_distance"


def normalize_angle(value):
    """Angle in ``(-pi, pi]``."""

    result = (float(value) + pi) % (2.0 * pi) - pi
    return pi if result == -pi else result


def heading_deg(dx, dy):
    """Local metric heading in degrees, 0 = north (+y), clockwise."""

    return degrees(atan2(float(dx), float(dy))) % 360.0


def analytic_arc_sagitta_m(radius_m, sweep_rad):
    """Sagitta of the *single-chord* approximation of a whole arc (a diagnostic)."""

    return float(radius_m) * (1.0 - cos(abs(float(sweep_rad)) / 2.0))


def chord_sagitta_m(radius_m, sweep_rad, segments=1):
    """Max deviation of an ``n``-equal-segment polyline from the arc.

    Each sub-arc spans ``θ/n``, so its mid-chord sagitta is
    ``R·(1 − cos(θ/(2n)))`` -- the quantity that must stay inside the explicit
    ``curve_chord_error_m``.  ``segments=1`` reduces to the whole-arc sagitta.
    """

    count = max(1, int(segments))
    return float(radius_m) * (1.0 - cos(abs(float(sweep_rad)) / (2.0 * count)))


def measured_chord_error_m(center, radius_m, start_angle_rad, sweep_rad, sampled_points,
                           *, dense_samples=256):
    """Measured max deviation of the realized polyline from its analytic arc.

    The realized LineString is a chord approximation, so its true error is not a
    formula but a measurement: sample the analytic arc densely and take the maximum
    perpendicular distance to the polyline.  This is what ``actual_max_chord_error_m``
    reports, and it is what the validation compares against the explicit tolerance.
    """

    if len(sampled_points) < 2 or not finite_number(radius_m) or radius_m <= 0:
        return 0.0
    worst = 0.0
    for index in range(dense_samples + 1):
        theta = start_angle_rad + float(sweep_rad) * (index / float(dense_samples))
        point = [center[0] + radius_m * cos(theta), center[1] + radius_m * sin(theta)]
        best = None
        for left, right in zip(sampled_points, sampled_points[1:]):
            value = _point_segment_distance(point, left, right)
            if best is None or value < best:
                best = value
        if best is not None and best > worst:
            worst = best
    return worst


def _point_segment_distance(point, left, right):
    dx, dy = float(right[0]) - float(left[0]), float(right[1]) - float(left[1])
    denominator = dx * dx + dy * dy
    if denominator <= 1e-18:
        return _distance(point, left)
    ratio = ((float(point[0]) - float(left[0])) * dx + (float(point[1]) - float(left[1])) * dy) / denominator
    ratio = max(0.0, min(1.0, ratio))
    return hypot(float(point[0]) - (float(left[0]) + dx * ratio),
                 float(point[1]) - (float(left[1]) + dy * ratio))


def chord_segments_for(radius_m, sweep_rad, chord_error_m):
    """Smallest segment count whose equal-angle chord sagitta is within tolerance.

    The sagitta of one of ``n`` equal sub-arcs is ``R·(1 − cos(θ/(2n)))``, which
    *decreases* as ``n`` grows.  So ``n = ceil(θ / (2·acos(1 − e/R)))`` is the
    minimum count that keeps every sub-chord within ``e``.
    """

    radius = float(radius_m)
    error = float(chord_error_m)
    sweep = abs(float(sweep_rad))
    if radius <= 0 or sweep <= 0:
        return 1
    if error <= 0 or error >= radius:
        return 1
    ratio = max(-1.0, min(1.0, 1.0 - error / radius))
    step = 2.0 * acos(ratio)
    if step <= 0:
        return 1
    return max(1, int(ceil(sweep / step - 1e-12)))


def _distance(left, right):
    return hypot(float(right[0]) - float(left[0]), float(right[1]) - float(left[1]))


def _heading_gap(left_rad, right_rad):
    """Smallest absolute difference between two headings, in radians."""

    delta = (float(left_rad) - float(right_rad)) % (2.0 * pi)
    return min(delta, 2.0 * pi - delta)


def _unit(from_point, to_point):
    length = _distance(from_point, to_point)
    if length <= TOLERANCE:
        return None
    return [(float(to_point[0]) - float(from_point[0])) / length,
            (float(to_point[1]) - float(from_point[1])) / length]


def _solve_turn_angles(points):
    """Signed turn angle at each internal vertex (``None`` when collinear)."""

    angles = []
    for index in range(1, len(points) - 1):
        incoming_heading = atan2(points[index][1] - points[index - 1][1],
                                 points[index][0] - points[index - 1][0])
        outgoing_heading = atan2(points[index + 1][1] - points[index][1],
                                 points[index + 1][0] - points[index][0])
        if _distance(points[index - 1], points[index]) <= TOLERANCE:
            angles.append(None)
            continue
        if _distance(points[index], points[index + 1]) <= TOLERANCE:
            angles.append(None)
            continue
        value = normalize_angle(outgoing_heading - incoming_heading)
        angles.append(None if abs(value) <= _COLLINEAR_TOLERANCE_RAD else value)
    return angles


def _prepared_waypoints(metric_points, altitudes):
    points, point_alts = [], []
    for index, raw in enumerate(metric_points or []):
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            return None, None, "turn_realization_failed_degenerate_geometry"
        point = [float(raw[0]), float(raw[1])]
        if not (isfinite(point[0]) and isfinite(point[1])):
            return None, None, "turn_realization_failed_non_finite_geometry"
        altitude = None
        if altitudes is not None and index < len(altitudes) and finite_number(altitudes[index]):
            altitude = float(altitudes[index])
        if points and _distance(points[-1], point) <= TOLERANCE:
            if altitude is not None:
                point_alts[-1] = altitude
            continue
        points.append(point)
        point_alts.append(altitude)
    if len(points) < 2:
        return None, None, "turn_realization_failed_degenerate_geometry"
    return points, point_alts, None


def _fill_missing_altitudes(point_alts):
    known = [value for value in point_alts if value is not None]
    if not known:
        return [None for _ in point_alts], [], "no_source_altitude_available"
    filled, carried = [], []
    last = None
    for index, value in enumerate(point_alts):
        if value is None:
            carried.append(index)
            value = last if last is not None else known[0]
        last = value
        filled.append(value)
    return filled, carried, None


class _TopLevelProfile:
    """Cumulative top-level distance and interpolated EGM2008 altitude of a polyline."""

    def __init__(self, points, altitudes):
        self.points = points
        self.altitudes = altitudes
        self.cumulative = [0.0]
        for index in range(len(points) - 1):
            self.cumulative.append(self.cumulative[-1] + _distance(points[index], points[index + 1]))

    def _locate(self, point):
        """Index of the segment whose projection of ``point`` is closest."""

        best_index, best_distance = None, None
        for index in range(len(self.points) - 1):
            segment = _distance(self.points[index], self.points[index + 1])
            if segment <= TOLERANCE:
                continue
            unit = _unit(self.points[index], self.points[index + 1])
            projection = (
                (float(point[0]) - self.points[index][0]) * unit[0]
                + (float(point[1]) - self.points[index][1]) * unit[1]
            )
            clamped = max(0.0, min(segment, projection))
            offset = hypot(
                float(point[0]) - (self.points[index][0] + unit[0] * clamped),
                float(point[1]) - (self.points[index][1] + unit[1] * clamped),
            )
            if best_distance is None or offset < best_distance - 1e-9:
                best_index, best_distance = index, offset
            if offset <= 1e-9:
                # An exact hit on this segment wins; ties are already resolved by the
                # strict improvement test above, which keeps the earliest segment.
                break
        return best_index

    def distance_at(self, point):
        """Top-level distance of the projection of ``point`` onto the polyline."""

        index = self._locate(point)
        if index is None:
            return self.cumulative[-1]
        segment = _distance(self.points[index], self.points[index + 1])
        unit = _unit(self.points[index], self.points[index + 1])
        projection = (
            (float(point[0]) - self.points[index][0]) * unit[0]
            + (float(point[1]) - self.points[index][1]) * unit[1]
        )
        return self.cumulative[index] + max(0.0, min(segment, projection))

    def altitude_at(self, point):
        index = self._locate(point)
        if index is None:
            return self.altitudes[-1]
        segment = _distance(self.points[index], self.points[index + 1])
        unit = _unit(self.points[index], self.points[index + 1])
        projection = (
            (float(point[0]) - self.points[index][0]) * unit[0]
            + (float(point[1]) - self.points[index][1]) * unit[1]
        )
        left, right = self.altitudes[index], self.altitudes[index + 1]
        if left is None:
            return right
        if right is None:
            return left
        ratio = 0.0 if segment <= TOLERANCE else max(0.0, min(1.0, projection / segment))
        return left + (right - left) * ratio


def realize_continuous_route(
    *, metric_points, altitudes=None, min_turn_radius_m, curve_chord_error_m,
    route_id=None, frame=None, strict_chord_bound=True,
):
    """Realize a V3-B metric trajectory as C1 straight/arc geometry.

    Returns ``{"status", "route", "reasons", "turn_failures", "curve_error"}`` where
    ``status`` is ``realized`` or ``turn_realization_failed``.
    """

    route = empty_continuous_route()
    frame = frame or {}
    route["route_id"] = route_id
    route["frame_id"] = frame.get("frame_id")
    route["horizontal_crs"] = frame.get("horizontal_crs")
    route["curve_error_envelope"]["curve_chord_error_m"] = (
        None if curve_chord_error_m is None else float(curve_chord_error_m)
    )
    route["vertical"]["method"] = VERTICAL_METHOD
    failures = []

    if not finite_number(min_turn_radius_m) or float(min_turn_radius_m) <= 0:
        return _failed(route, "turn_realization_failed_invalid_minimum_turn_radius", failures)
    if not finite_number(curve_chord_error_m) or float(curve_chord_error_m) <= 0:
        return _failed(route, "turn_realization_failed_chord_error_tolerance_unresolved", failures)

    radius = float(min_turn_radius_m)
    tolerance = float(curve_chord_error_m)
    points, point_alts, problem = _prepared_waypoints(metric_points, altitudes)
    if problem is not None:
        return _failed(route, problem, failures)
    point_alts, carried_indices, altitude_problem = _fill_missing_altitudes(point_alts)
    profile = _TopLevelProfile(points, point_alts)
    angles = _solve_turn_angles(points)
    tangent_lengths = [
        None if angle is None else radius * tan(abs(angle) / 2.0) for angle in angles
    ]

    # 1. every usable segment must fit the fillets that consume it from both ends.
    for segment_index in range(len(points) - 1):
        length = _distance(points[segment_index], points[segment_index + 1])
        consumed = 0.0
        for corner_index, tangent in enumerate(tangent_lengths):
            if tangent is None:
                continue
            vertex = corner_index + 1
            if vertex - 1 == segment_index or vertex == segment_index:
                consumed += tangent
        if consumed > length + TOLERANCE:
            failures.append({
                "reason": "turn_realization_failed_segment_too_short_for_minimum_turn_radius",
                "segment_index": segment_index,
                "segment_length_m": round(length, 9),
                "required_tangent_length_m": round(consumed, 9),
                "shortfall_m": round(consumed - length, 9),
                "minimum_turn_radius_m": radius,
                "radius_reduced": False,
            })
    if failures:
        return _failed(
            route, "turn_realization_failed_segment_too_short_for_minimum_turn_radius", failures,
        )

    # 2. build straights and analytic arcs in order along the trajectory.
    primitives, turns = [], []
    cumulative = 0.0
    previous_end = list(points[0])
    for vertex in range(1, len(points) - 1):
        angle = angles[vertex - 1]
        if angle is None:
            continue
        tangent = tangent_lengths[vertex - 1]
        incoming_unit = _unit(points[vertex - 1], points[vertex])
        outgoing_unit = _unit(points[vertex], points[vertex + 1])
        if incoming_unit is None or outgoing_unit is None:
            return _failed(route, "turn_realization_failed_degenerate_geometry", failures)
        arc_start = [points[vertex][0] - tangent * incoming_unit[0],
                     points[vertex][1] - tangent * incoming_unit[1]]
        arc_end = [points[vertex][0] + tangent * outgoing_unit[0],
                   points[vertex][1] + tangent * outgoing_unit[1]]
        if _distance(previous_end, arc_start) > TOLERANCE:
            primitive, cumulative = _straight_primitive(
                primitives, previous_end, arc_start, profile, cumulative,
            )
            primitives.append(primitive)
        turn = _realize_turn(
            vertex_index=vertex, vertex_metric=points[vertex], angle=angle, radius=radius,
            tangent=tangent, arc_start=arc_start, arc_end=arc_end,
            incoming_unit=incoming_unit, outgoing_unit=outgoing_unit,
            min_turn_radius_m=radius, chord_error_m=tolerance,
        )
        if turn is None:
            failures.append({
                "reason": "turn_realization_failed_degenerate_geometry",
                "vertex_index": vertex,
                "vertex_metric": [round(points[vertex][0], 9), round(points[vertex][1], 9)],
                "turn_angle_rad": round(angle, 12),
                "note": "fillet tangent construction inconsistent with the analytic turn",
            })
            return _failed(route, "turn_realization_failed_degenerate_geometry", failures)
        arc_primitive = empty_continuous_primitive("circular_arc")
        arc_primitive.update({
            "primitive_id": f"p{len(primitives):03d}-arc",
            "index": len(primitives),
            "kind": "circular_arc",
            "distance_start_m": round(cumulative, 9),
            "horizontal_length_m": round(turn["arc"]["arc_length_m"], 9),
            "start_metric": [round(arc_start[0], 9), round(arc_start[1], 9)],
            "end_metric": [round(arc_end[0], 9), round(arc_end[1], 9)],
            "start_heading_deg": round(turn["start_heading_deg"], 9),
            "end_heading_deg": round(turn["end_heading_deg"], 9),
            "heading_change_deg": round(degrees(angle), 9),
            "z_start_egm2008_m": _round_or_none(profile.altitude_at(arc_start)),
            "z_end_egm2008_m": _round_or_none(profile.altitude_at(arc_end)),
            "arc": turn["arc"],
            "linearization": turn["linearization"],
            "sampled_points_metric": turn["sampled_points_metric"],
            "vertical_interpolation": "linear_in_arc_length_between_tangent_point_altitudes",
        })
        _finish_vertical(arc_primitive)
        cumulative += turn["arc"]["arc_length_m"]
        arc_primitive["distance_end_m"] = round(cumulative, 9)
        primitives.append(arc_primitive)
        turns.append(turn)
        previous_end = list(arc_end)
    if _distance(previous_end, points[-1]) > TOLERANCE:
        primitive, cumulative = _straight_primitive(
            primitives, previous_end, points[-1], profile, cumulative,
        )
        primitives.append(primitive)
    if not primitives:
        return _failed(route, "turn_realization_failed_degenerate_geometry", failures)

    linearized = [list(primitives[0]["start_metric"])]
    for primitive in primitives:
        linearized.extend(primitive["sampled_points_metric"][1:])
    actual_chord = max(
        [
            (primitive.get("linearization") or {}).get("chord_error_m") or 0.0
            for primitive in primitives
        ] or [0.0]
    )
    route["primitives"] = primitives
    route["turns"] = turns
    route["turn_realization_status"] = "realized"
    route["turn_realization_reasons"] = []
    route["horizontal_geometry"] = {
        "analytic": {
            "primitive_count": len(primitives),
            "straight_count": sum(1 for item in primitives if item["kind"] == "straight"),
            "arc_count": sum(1 for item in primitives if item["kind"] == "circular_arc"),
            "turn_count": len(turns),
            "total_horizontal_length_m": round(cumulative, 9),
            "curvature_continuity": "C1_position_and_heading_only",
            "continuous_curvature": False,
            "c2": False,
            "clothoid": "future_work_not_implemented",
            "turn_radius_policy": TURN_RADIUS_POLICY,
            "radius_reduced_anywhere": False,
            "semantics": CONTINUITY_SEMANTICS,
        },
        "linearized": {
            "linestring_metric": [[round(point[0], 9), round(point[1], 9)] for point in linearized],
            "point_count": len(linearized),
            "actual_max_chord_error_m": round(actual_chord, 12),
            "curve_chord_error_m": tolerance,
            "method": CHORD_LINEARIZATION_METHOD,
            "semantics": ANALYTIC_VS_LINEARIZED,
            "not_the_mathematical_curve": True,
        },
    }
    route["vertical"]["total_distance_m"] = round(cumulative, 9)
    route["waypoint_evidence"] = {
        "waypoint_count": len(points),
        "carried_altitude_indices": carried_indices,
        "altitude_evidence_problem": altitude_problem,
        "collinear_vertices_dropped": sum(1 for angle in angles if angle is None),
        "dropped_vertex_indices": [index + 1 for index, angle in enumerate(angles) if angle is None],
        "geometry_domain": "v3b_local_metric_frame",
    }
    _summarize_vertical(route, primitives)
    if strict_chord_bound and actual_chord > tolerance * (1.0 + 1e-9) + 1e-12:
        return _failed(route, "turn_realization_failed_chord_error_bound_exceeded", [{
            "reason": "turn_realization_failed_chord_error_bound_exceeded",
            "requested_curve_chord_error_m": tolerance,
            "actual_max_chord_error_m": round(actual_chord, 12),
        }])
    route["status"] = "realized"
    route["reason"] = None
    return {
        "status": "realized",
        "route": route,
        "reasons": [],
        "turn_failures": [],
        "curve_error": {
            "requested_max_chord_error_m": tolerance,
            "actual_max_chord_error_m": round(actual_chord, 12),
            "method": CHORD_LINEARIZATION_METHOD,
            "envelope_radius_m": tolerance,
            "semantics": CURVE_ERROR_ENVELOPE_SEMANTICS,
            "analytic_geometry_retained": True,
            "not_the_mathematical_curve": True,
        },
    }


def _round_or_none(value):
    return None if value is None else round(float(value), 9)


def _straight_primitive(primitives, start, end, profile, cumulative):
    heading = degrees(atan2(end[1] - start[1], end[0] - start[0])) % 360.0
    length = _distance(start, end)
    primitive = empty_continuous_primitive("straight")
    primitive.update({
        "primitive_id": f"p{len(primitives):03d}-straight",
        "index": len(primitives),
        "kind": "straight",
        "distance_start_m": round(cumulative, 9),
        "distance_end_m": round(cumulative + length, 9),
        "horizontal_length_m": round(length, 9),
        "start_metric": [round(start[0], 9), round(start[1], 9)],
        "end_metric": [round(end[0], 9), round(end[1], 9)],
        "start_heading_deg": round(heading, 9),
        "end_heading_deg": round(heading, 9),
        "heading_change_deg": 0.0,
        "z_start_egm2008_m": _round_or_none(profile.altitude_at(start)),
        "z_end_egm2008_m": _round_or_none(profile.altitude_at(end)),
        "sampled_points_metric": [
            [round(start[0], 9), round(start[1], 9)],
            [round(end[0], 9), round(end[1], 9)],
        ],
        "vertical_interpolation": "linear_on_the_source_v3b_segment",
    })
    _finish_vertical(primitive)
    return primitive, cumulative + length


def _failed(route, reason, failures):
    reasons = sorted({reason} | {
        item.get("reason") for item in failures if item.get("reason")
    })
    route["status"] = "turn_realization_failed"
    route["reason"] = reason
    route["turn_realization_status"] = "turn_realization_failed"
    route["turn_realization_reasons"] = reasons
    route["operational_route"] = False
    route["cns_assessed"] = False
    route["semantics"]["replan_required"] = True
    route["semantics"]["radius_never_reduced"] = True
    route["semantics"]["turn_radius_policy"] = TURN_RADIUS_POLICY
    route["semantics"]["turn_failures"] = failures
    return {
        "status": "turn_realization_failed",
        "route": route,
        "reasons": reasons,
        "turn_failures": failures,
        "curve_error": None,
    }


def _realize_turn(*, vertex_index, vertex_metric, angle, radius, tangent, arc_start, arc_end,
                  incoming_unit, outgoing_unit, min_turn_radius_m, chord_error_m):
    """Analytic fillet geometry + its bounded linearization for one internal vertex.

    The tangent headings come from the *known* incoming/outgoing directions of the
    trajectory -- not from re-deriving an angle out of the tangent-point offset, which
    is a different vector and would break the C1 continuity claim.
    """

    sweep = float(angle)
    incoming = atan2(incoming_unit[1], incoming_unit[0])
    outgoing = atan2(outgoing_unit[1], outgoing_unit[0])
    # The analytic sweep has the sign of the actual turn, so the centre is placed
    # perpendicular to the incoming heading on the inside of the turn.
    signed_sweep = normalize_angle(outgoing - incoming)
    if abs(abs(signed_sweep) - abs(sweep)) > 1e-9:
        # A turn whose tangent construction disagrees with the requested angle means
        # the offset direction was resolved the wrong way: refuse rather than bend.
        return None
    sweep = signed_sweep
    # Backwards along the incoming leg: the tangent point is *behind* the vertex.
    backwards = incoming + pi
    perpendicular = backwards - (pi / 2.0) * (1.0 if sweep > 0 else -1.0)
    center = [arc_start[0] + radius * cos(perpendicular), arc_start[1] + radius * sin(perpendicular)]
    # The centre must sit exactly ``radius`` from both tangent points; otherwise the
    # fillet is not tangent to the adjacent straights and the realization is refused.
    if abs(_distance(center, arc_start) - radius) > 1e-6 or abs(_distance(center, arc_end) - radius) > 1e-6:
        return None
    # The tangent headings are the leg directions by definition: on the arc the
    # heading is the tangent, which is ``angle ± 90°`` with the sign of the sweep.
    start_angle = atan2(arc_start[1] - center[1], arc_start[0] - center[0])
    tangent_sign = pi / 2.0 if sweep > 0 else -pi / 2.0
    if _heading_gap(incoming, start_angle + tangent_sign) > 1e-6:
        return None
    if _heading_gap(outgoing,
                    atan2(arc_end[1] - center[1], arc_end[0] - center[0]) + tangent_sign) > 1e-6:
        return None
    end_angle = atan2(arc_end[1] - center[1], arc_end[0] - center[0])
    arc_length = radius * abs(sweep)
    segments = chord_segments_for(radius, sweep, chord_error_m)
    sampled = []
    for index in range(segments + 1):
        theta = start_angle + sweep * (index / float(segments))
        sampled.append([
            round(center[0] + radius * cos(theta), 9),
            round(center[1] + radius * sin(theta), 9),
        ])
    # The realized geometry must reproduce the analytic tangent points exactly.
    sampled[0] = [round(arc_start[0], 9), round(arc_start[1], 9)]
    sampled[-1] = [round(arc_end[0], 9), round(arc_end[1], 9)]
    # Realized chord bound: measured against the analytic arc, not assumed.
    chord_error = measured_chord_error_m(center, radius, start_angle, sweep, sampled)
    analytic_sagitta = analytic_arc_sagitta_m(radius, sweep)
    arc = empty_circular_arc()
    arc.update({
        "center_metric": [round(center[0], 9), round(center[1], 9)],
        "radius_m": round(radius, 9),
        "start_angle_rad": round(start_angle, 12),
        "end_angle_rad": round(end_angle, 12),
        "sweep_angle_rad": round(abs(sweep), 12),
        "signed_sweep_rad": round(sweep, 12),
        "arc_length_m": round(arc_length, 9),
        "chord_length_m": round(_distance(arc_start, arc_end), 9),
        "tangent_points_metric": [
            [round(arc_start[0], 9), round(arc_start[1], 9)],
            [round(arc_end[0], 9), round(arc_end[1], 9)],
        ],
        "turn_direction": "left" if sweep > 0 else "right",
        "analytic_geometry_retained": True,
        "radius_verified_from_center": round(_distance(center, arc_start), 9),
    })
    linearization = empty_linearization()
    linearization.update({
        "method": CHORD_LINEARIZATION_METHOD,
        "curve_chord_error_m": float(chord_error_m),
        "chord_error_m": round(chord_error, 12),
        "chord_error_bound_m": float(chord_error_m),
        "requested_max_chord_error_m": float(chord_error_m),
        "segments": segments,
        "angular_step_rad": round(abs(sweep) / segments, 12),
        "sampled_point_count": len(sampled),
        "analytic_geometry_retained": True,
        "not_the_mathematical_curve": True,
        "whole_arc_single_chord_sagitta_m": round(analytic_sagitta, 12),
        "measured_chord_error_method": "dense_analytic_arc_to_realized_polyline_max_perpendicular_distance",
        "semantics": ANALYTIC_VS_LINEARIZED,
    })
    turn = empty_turn_realization()
    turn.update({
        "turn_id": f"turn{vertex_index:03d}",
        "vertex_index": vertex_index,
        "vertex_metric": [round(vertex_metric[0], 9), round(vertex_metric[1], 9)],
        "turn_angle_rad": round(angle, 12),
        "turn_angle_deg": round(degrees(angle), 9),
        "turn_direction": arc["turn_direction"],
        "radius_m": round(radius, 9),
        "min_turn_radius_m": round(float(min_turn_radius_m), 9),
        "radius_was_reduced": False,
        "radius_policy": TURN_RADIUS_POLICY,
        "tangent_length_m": round(tangent, 9),
        "tangent_points_metric": arc["tangent_points_metric"],
        "arc_center_metric": arc["center_metric"],
        "arc_start_angle_rad": arc["start_angle_rad"],
        "arc_end_angle_rad": arc["end_angle_rad"],
        "arc_length_m": arc["arc_length_m"],
        "status": "realized",
        "failure_reason": None,
        "curve_chord_error_m": float(chord_error_m),
        "chord_error_m": linearization["chord_error_m"],
        "linearization_segments": segments,
        "linearization": linearization,
        "arc": arc,
        "sampled_points_metric": sampled,
        "start_heading_deg": round(degrees(incoming) % 360.0, 9),
        "end_heading_deg": round(degrees(outgoing) % 360.0, 9),
        "heading_change_deg": round(degrees(angle), 9),
        "semantics": CONTINUITY_SEMANTICS,
    })
    return turn


def _finish_vertical(primitive):
    z_start, z_end = primitive.get("z_start_egm2008_m"), primitive.get("z_end_egm2008_m")
    length = primitive.get("horizontal_length_m")
    if z_start is None or z_end is None or not length:
        primitive["gradient"] = None
        primitive["length_3d_m"] = None
        return
    delta = float(z_end) - float(z_start)
    primitive["gradient"] = round(delta / float(length), 12)
    primitive["length_3d_m"] = round(hypot(float(length), delta), 9)


def _summarize_vertical(route, primitives):
    z_values = [
        value for primitive in primitives
        for value in (primitive.get("z_start_egm2008_m"), primitive.get("z_end_egm2008_m"))
        if value is not None
    ]
    route["vertical"].update({
        "z_start_egm2008_m": primitives[0]["z_start_egm2008_m"] if primitives else None,
        "z_end_egm2008_m": primitives[-1]["z_end_egm2008_m"] if primitives else None,
        "min_z_egm2008_m": min(z_values) if z_values else None,
        "max_z_egm2008_m": max(z_values) if z_values else None,
        "climb_distance_m": round(sum(
            primitive["horizontal_length_m"] for primitive in primitives
            if (primitive.get("gradient") or 0.0) > 0
        ), 9),
        "descent_distance_m": round(sum(
            primitive["horizontal_length_m"] for primitive in primitives
            if (primitive.get("gradient") or 0.0) < 0
        ), 9),
        "level_distance_m": round(sum(
            primitive["horizontal_length_m"] for primitive in primitives
            if primitive.get("gradient") == 0
        ), 9),
        "max_climb_gradient_observed": max(
            [primitive["gradient"] for primitive in primitives
             if (primitive.get("gradient") or 0.0) > 0] or [0.0]
        ),
        "max_descent_gradient_observed": min(
            [primitive["gradient"] for primitive in primitives
             if (primitive.get("gradient") or 0.0) < 0] or [0.0]
        ),
    })


__all__ = [
    "CHORD_LINEARIZATION_METHOD", "TURN_FAILURE_REASONS", "TURN_RADIUS_POLICY",
    "VERTICAL_METHOD", "analytic_arc_sagitta_m", "chord_sagitta_m", "chord_segments_for",
    "heading_deg", "measured_chord_error_m", "normalize_angle", "realize_continuous_route",
]
