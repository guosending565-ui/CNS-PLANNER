"""Production-neutral validators: airspace, native terrain, buildings, kinematics.

Every validator here is **pure Python**: it consumes canonical evidence (confirmed
policy polygons in the metric frame, native raster pixel windows, building
footprints with the shared roof semantics) and returns a
:func:`empty_domain_result` contract.  No QGIS/GDAL import, no file access, no
repair and no replan -- a violation is reported as structured evidence only.

Semantics that must not be weakened:

* **airspace** -- only ``confirmed`` policy geometry is consumed.  The realized
  route's *uncertainty envelope* (the linearized LineString buffered by the explicit
  ``curve_chord_error_m``) must be fully covered by the allowed union and must not
  intersect the blocked union.  A mixed / boundary / unconfirmed feature touching
  the envelope is ``unresolved`` -- fail-closed, never "probably allowed";
* **terrain** -- the **native** raster is checked directly, pixel by pixel, over the
  pixels the realized path (with its error envelope) touches.  NoData is
  ``unresolved`` and is never filled bilinearly or treated as zero.  The claim is
  ``source_native_raster_validation``, not that the real world terrain is
  mathematically continuous;
* **building** -- real footprints are buffered by the explicit horizontal clearance
  **plus** the curve error and intersected with the route, then the route's minimum
  ``z`` over each interval is compared with ``roof + vertical_clearance``.  The roof
  formula and the vertical-clearance test come from the shared building-clearance
  helpers, so V3-C does not invent a third roof definition.  Missing height /
  ground / invalid geometry is ``unresolved``;
* **geometry** and **kinematics** -- the analytic arcs are re-checked (``R >= Rmin``,
  tangent heading continuity, chord bound), and the climb/descent gradient is
  revalidated on every continuous primitive.  The V3-B ``R·Δψ`` proxy is *not* the
  final turn verification.
"""

from __future__ import annotations

from math import hypot

from ..domain.building_clearance import (
    building_height_status_is_resolved, building_roof_elevation,
    evaluate_vertical_clearance,
)
from ..domain.restricted_area import altitude_applicability, normalize_restricted_area
from .continuous_contracts import (
    TOLERANCE, empty_domain_result, finite_number, violation_interval,
)
#: How the route's uncertainty envelope is built.  ``quad_segs`` applies to the
#: *cap* rounding of a polyline buffer; the offset distance itself is exact.
ENVELOPE_METHOD = "linearized_linestring_buffered_by_explicit_curve_chord_error_m"
DEFAULT_QUAD_SEGS = 8

#: Recording of the Shapely buffer approximation actually used.
BUFFER_APPROXIMATION = {
    "library": "shapely",
    "parameter": "quad_segs",
    "quad_segs": DEFAULT_QUAD_SEGS,
    "note": "quad_segs 只影响 cap 的圆弧离散；(x,y) 坐标与 buffer 距离本身是 exact 的",
}

#: Terrain evidence semantics (fixed string; never a claim of real-world exactness).
TERRAIN_SEMANTICS = "source_native_raster_validation"


def metric_distance(left, right):
    return hypot(float(right[0]) - float(left[0]), float(right[1]) - float(left[1]))


class MetricRoute:
    """The realized route in the V3-B local metric frame, with error envelope.

    Wraps the linearized LineString (an approximation of known chord bound) and the
    analytic primitives, and offers the along-track queries the validators need.
    Vector predicates run on the linearized representation *including* the explicit
    curve-error envelope -- that is exact for that representation, not for the
    mathematical curve.
    """

    def __init__(self, route, *, quad_segs=DEFAULT_QUAD_SEGS):
        from shapely.geometry import LineString

        self.route = route or {}
        linearized = ((route or {}).get("horizontal_geometry") or {}).get("linearized") or {}
        points = [
            [float(point[0]), float(point[1])]
            for point in linearized.get("linestring_metric") or []
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        if len(points) < 2:
            raise ValueError("V3-C MetricRoute 需要至少两个 realized line 顶点")
        self.points = points
        self.line = LineString([(point[0], point[1]) for point in points])
        self.curve_chord_error_m = float(linearized.get("curve_chord_error_m") or 0.0)
        self.primitives = list((route or {}).get("primitives") or [])
        self.quad_segs = int(quad_segs)
        self._length = float(self.line.length)

    # ------------------------------------------------------------------ geometry

    @property
    def length_m(self):
        return self._length

    def envelope(self, *, radius_m=None):
        """The route uncertainty envelope (offset by the explicit chord error)."""

        radius = self.curve_chord_error_m if radius_m is None else float(radius_m)
        if radius <= 0:
            return self.line
        return self.line.buffer(radius, quad_segs=self.quad_segs)

    def sample_at(self, distance_m):
        """Metric ``[x, y]`` at along-track ``distance_m`` (clamped)."""

        value = max(0.0, min(self._length, float(distance_m)))
        point = self.line.interpolate(value)
        return [float(point.x), float(point.y)]

    def distance_of(self, point_m):
        """Along-track distance of the projection of a metric point (clamped)."""

        from shapely.geometry import Point

        value = float(self.line.project(Point(float(point_m[0]), float(point_m[1]))))
        return max(0.0, min(self._length, value))

    def subline(self, start_m, end_m):
        """A LineString covering ``[start, end]`` of the route (clamped, densified)."""

        from shapely.geometry import LineString

        start = max(0.0, float(start_m))
        end = min(self._length, float(end_m))
        if end < start:
            start, end = end, start
        if end - start <= TOLERANCE:
            point = self.line.interpolate(start)
            return LineString([(point.x, point.y), (point.x, point.y)])
        # Densify so a straight cut stays straight even on a coarse input line.
        steps = max(2, int((end - start) / max(1.0, self._length / 256.0)) + 1)
        points = []
        for index in range(steps + 1):
            ratio = index / float(steps)
            point = self.line.interpolate(start + (end - start) * ratio)
            points.append((point.x, point.y))
        return LineString(points)

    def intervals_for_polygon(self, polygon):
        """Along-track intervals where the route uncertainty envelope hits a polygon."""

        envelope = self.envelope()
        if envelope is None or envelope.is_empty or polygon is None or polygon.is_empty:
            return []
        if not _bbox_intersects(envelope.bounds, polygon.bounds):
            return []
        # Coverage predicates already handle "the polygon contains the whole envelope";
        # the interval is then simply the whole route and no geometry needs to be built.
        if envelope.within(polygon) or envelope.covers(polygon):
            return [{"start_distance_m": 0.0, "end_distance_m": self._length}]
        intersection = envelope.intersection(polygon)
        if intersection.is_empty:
            return []
        intervals = []
        for part in _parts(intersection):
            distances = []
            for x, y in _coordinates(part):
                distances.append(self.distance_of([x, y]))
            if not distances:
                continue
            start, end = min(distances), max(distances)
            if end - start <= TOLERANCE:
                continue
            intervals.append({"start_distance_m": start, "end_distance_m": end})
        return _merge_intervals(intervals)

    def distance_to_polygon(self, polygon):
        """Shortest metric distance between the route line and a polygon."""

        if polygon is None or polygon.is_empty:
            return None
        return float(self.line.distance(polygon))

    def vertical_z(self, distance_m):
        """EGM2008 altitude at along-track distance (linear inside each primitive)."""

        value = max(0.0, min(self._length, float(distance_m)))
        for primitive in self.primitives:
            start = primitive.get("distance_start_m")
            end = primitive.get("distance_end_m")
            if start is None or end is None:
                continue
            if value < start - TOLERANCE or value > end + TOLERANCE:
                continue
            z_start, z_end = primitive.get("z_start_egm2008_m"), primitive.get("z_end_egm2008_m")
            if z_start is None or z_end is None:
                return None
            span = float(end) - float(start)
            ratio = 0.0 if span <= TOLERANCE else max(0.0, min(1.0, (value - float(start)) / span))
            return float(z_start) + (float(z_end) - float(z_start)) * ratio
        return None

    def sample_interval(self, start_m, end_m, *, samples=9):
        """Evenly spaced ``(distance, metric point, z)`` triples on an interval."""

        start, end = float(start_m), float(end_m)
        if end < start:
            start, end = end, start
        count = max(2, int(samples))
        result = []
        for index in range(count):
            distance = start + (end - start) * (index / float(count - 1))
            result.append({
                "distance_m": distance,
                "point_metric": self.sample_at(distance),
                "z_egm2008_m": self.vertical_z(distance),
            })
        return result

    def minimum_z(self, start_m, end_m, *, samples=17):
        values = [
            item["z_egm2008_m"] for item in self.sample_interval(start_m, end_m, samples=samples)
        ]
        resolved = [value for value in values if value is not None]
        if not resolved:
            return None
        if len(resolved) != len(values):
            return None
        return min(resolved)

    def to_geographic(self, point_m, *, to_geographic):
        if to_geographic is None:
            return None
        converted = to_geographic([float(point_m[0]), float(point_m[1])])
        if converted is None:
            return None
        return [float(converted[0]), float(converted[1])]


def _parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    kind = geometry.geom_type
    if kind in ("GeometryCollection", "MultiPolygon", "MultiLineString"):
        return [part for part in geometry.geoms if part is not None and not part.is_empty]
    return [geometry]


def _coordinates(geometry):
    kind = geometry.geom_type
    if kind == "Point":
        return [(float(geometry.x), float(geometry.y))]
    if kind == "LineString":
        return [(float(x), float(y)) for x, y in geometry.coords]
    if kind in ("MultiLineString", "GeometryCollection"):
        result = []
        for part in geometry.geoms:
            result.extend(_coordinates(part))
        return result
    if kind == "Polygon":
        result = [(float(x), float(y)) for x, y in geometry.exterior.coords]
        for ring in geometry.interiors:
            result.extend((float(x), float(y)) for x, y in ring.coords)
        return result
    if kind == "MultiPolygon":
        result = []
        for part in geometry.geoms:
            result.extend(_coordinates(part))
        return result
    return []


def _merge_intervals(items, *, tolerance=1e-6):
    merged = []
    for item in sorted(items, key=lambda value: value["start_distance_m"]):
        if merged and item["start_distance_m"] <= merged[-1]["end_distance_m"] + tolerance:
            merged[-1]["end_distance_m"] = max(merged[-1]["end_distance_m"], item["end_distance_m"])
        else:
            merged.append(dict(item))
    return merged


def _bbox_intersects(left, right, *, tolerance=1e-9):
    if left is None or right is None:
        return False
    return not (
        left[2] < right[0] - tolerance or right[2] < left[0] - tolerance
        or left[3] < right[1] - tolerance or right[3] < left[1] - tolerance
    )


def _bbox_gap(left, right):
    """Separation between two bounding boxes (0 when they overlap)."""

    if left is None or right is None:
        return None
    return hypot(
        max(left[0] - right[2], 0.0, right[0] - left[2]),
        max(left[1] - right[3], 0.0, right[1] - left[3]),
    )


def _polygon_from_evidence(entry):
    from shapely.geometry import Polygon

    outer = entry.get("outer_metric")
    if not outer or len(outer) < 3:
        return None
    holes = [hole for hole in entry.get("holes_metric") or [] if hole and len(hole) >= 3]
    polygon = Polygon(outer, holes)
    if polygon.is_empty or polygon.area <= 0:
        return None
    if not polygon.is_valid:
        # The source geometry is never modified; an invalid polygon cannot be used
        # as confirmed evidence and therefore stays unresolved upstream.
        return None
    return polygon


# --------------------------------------------------------------------------- geometry


def validate_geometry(route, *, policy):
    """Re-check the realization itself: analytic arcs, C1, and the chord bound."""

    result = empty_domain_result("geometry", "passed")
    result["evaluated"] = True
    semantics = (route or {}).get("semantics") or {}
    analytic = ((route or {}).get("horizontal_geometry") or {}).get("analytic") or {}
    linearized = ((route or {}).get("horizontal_geometry") or {}).get("linearized") or {}
    result["evidence"] = {
        "turn_realization_status": (route or {}).get("turn_realization_status"),
        "primitive_count": analytic.get("primitive_count"),
        "arc_count": analytic.get("arc_count"),
        "continuous_curvature": analytic.get("continuous_curvature", False),
        "c2": False,
        "clothoid": "future_work_not_implemented",
        "curvature_continuity": analytic.get("curvature_continuity"),
        "curve_chord_error_m": linearized.get("curve_chord_error_m"),
        "actual_max_chord_error_m": linearized.get("actual_max_chord_error_m"),
        "linearization_method": linearized.get("method"),
        "linearized_point_count": linearized.get("point_count"),
        "radius_policy": analytic.get("turn_radius_policy"),
        "radius_reduced_anywhere": analytic.get("radius_reduced_anywhere", False),
        "connectivity": "position_and_heading_only",
        "semantics": semantics,
    }
    result["semantics"] = {
        "not_continuous_curvature": True,
        "arc_is_analytic_linestring_is_bounded_approximation": True,
    }
    if (route or {}).get("status") != "realized" or (route or {}).get("turn_realization_status") != "realized":
        result["status"] = "failed"
        result["reason"] = (route or {}).get("reason") or "turn_realization_failed"
        result["violations"] = [violation_interval(
            domain="geometry", reason_id=result["reason"],
            required="realized_c1_geometry", observed=(route or {}).get("turn_realization_status"),
            evidence={
                "turn_realization_reasons": (route or {}).get("turn_realization_reasons") or [],
                "turn_failures": (route or {}).get("turn_failures") or (
                    (route or {}).get("semantics") or {}
                ).get("turn_failures") or [],
                "radius_never_reduced": True,
                "semantics": "replan_required_no_automatic_repair",
            },
        )]
        result["failed_interval_count"] = 1
        return result
    if analytic.get("radius_reduced_anywhere"):
        result["status"] = "failed"
        result["reason"] = "turn_radius_reduced_not_allowed"
        result["violations"] = [violation_interval(
            domain="geometry", reason_id="turn_radius_reduced_not_allowed",
            required="radius_never_reduced", observed=True,
        )]
        result["failed_interval_count"] = 1
        return result
    tolerance = linearized.get("curve_chord_error_m")
    actual = linearized.get("actual_max_chord_error_m")
    if not finite_number(tolerance) or tolerance <= 0:
        result["status"] = "unresolved"
        result["reason"] = "curve_chord_error_m_unresolved"
        result["unresolved"] = [violation_interval(
            domain="geometry", reason_id="curve_chord_error_m_unresolved",
            required="explicit_curve_chord_error_m", observed=tolerance,
        )]
        result["unresolved_interval_count"] = 1
        return result
    if finite_number(actual) and actual > float(tolerance) * (1.0 + 1e-9) + 1e-12:
        result["status"] = "failed"
        result["reason"] = "curve_chord_error_bound_exceeded"
        result["violations"] = [violation_interval(
            domain="geometry", reason_id="curve_chord_error_bound_exceeded",
            required=float(tolerance), observed=float(actual),
            margin=float(tolerance) - float(actual),
        )]
        result["failed_interval_count"] = 1
        result["minimum_margin"] = float(tolerance) - float(actual)
        result["margin_semantics"] = "requested_minus_actual_curve_chord_error_m"
    return result


def validate_altitude_bounds(route, *, policy):
    """Re-validate the realized min/max EGM2008 altitude against the explicit band."""

    result = empty_domain_result("altitude", "passed")
    result["evaluated"] = True
    minimum = policy.get("min_altitude_egm2008_m")
    maximum = policy.get("max_altitude_egm2008_m")
    vertical = (route or {}).get("vertical") or {}
    z_min, z_max = vertical.get("min_z_egm2008_m"), vertical.get("max_z_egm2008_m")
    result["evidence"] = {
        "min_altitude_egm2008_m": minimum,
        "max_altitude_egm2008_m": maximum,
        "observed_min_z_egm2008_m": z_min,
        "observed_max_z_egm2008_m": z_max,
        "vertical_method": vertical.get("method"),
        "vertical_reference": (route or {}).get("vertical_reference"),
    }
    result["margin_semantics"] = "explicit_bound_minus_observed_extreme_m"
    if minimum is None or maximum is None:
        result["status"] = "unresolved"
        result["reason"] = "altitude_bounds_unresolved"
        result["unresolved"] = [violation_interval(
            domain="altitude", reason_id="altitude_bounds_unresolved",
            required="explicit_min_max_altitude_egm2008_m",
            observed={"min": minimum, "max": maximum},
        )]
        result["unresolved_interval_count"] = 1
        return result
    if z_min is None or z_max is None:
        result["status"] = "unresolved"
        result["reason"] = "realized_altitude_profile_unresolved"
        result["unresolved"] = [violation_interval(
            domain="altitude", reason_id="realized_altitude_profile_unresolved",
            required="resolved_z_s_profile", observed=None,
        )]
        result["unresolved_interval_count"] = 1
        return result
    lower_margin = float(z_min) - float(minimum)
    upper_margin = float(maximum) - float(z_max)
    result["minimum_margin"] = min(lower_margin, upper_margin)
    if lower_margin < -TOLERANCE:
        primitive = (route or {}).get("primitives") or [{}]
        result["status"] = "failed"
        result["reason"] = "realized_altitude_below_minimum"
        result["violations"] = [violation_interval(
            domain="altitude", reason_id="realized_altitude_below_minimum",
            start_distance_m=0.0, end_distance_m=vertical.get("total_distance_m"),
            start_point=primitive[0].get("start_metric"),
            end_point=primitive[-1].get("end_metric"),
            required=float(minimum), observed=float(z_min), margin=lower_margin,
        )]
        result["failed_interval_count"] = 1
    if upper_margin < -TOLERANCE:
        primitive = (route or {}).get("primitives") or [{}]
        result["status"] = "failed"
        result["reason"] = "realized_altitude_above_maximum"
        result["violations"].append(violation_interval(
            domain="altitude", reason_id="realized_altitude_above_maximum",
            start_distance_m=0.0, end_distance_m=vertical.get("total_distance_m"),
            start_point=primitive[0].get("start_metric"),
            end_point=primitive[-1].get("end_metric"),
            required=float(maximum), observed=float(z_max), margin=upper_margin,
        ))
        result["failed_interval_count"] = len(result["violations"])
    return result


# --------------------------------------------------------------------------- airspace


def validate_airspace(metric_route, *, evidence, policy, to_geographic=None):
    """Exact confirmed-polygon route-level airspace validation (V3-C's own check).

    Requirements: the route uncertainty envelope must be fully covered by the
    confirmed allowed union and must not intersect the confirmed blocked union.
    Any feature whose confirmation is missing but which touches the envelope makes
    the domain ``unresolved`` -- fail-closed.
    """

    result = empty_domain_result("airspace", "passed")
    result["evaluated"] = True
    allowed_entries = list((evidence or {}).get("allowed") or [])
    blocked_entries = list((evidence or {}).get("blocked") or [])
    unconfirmed_entries = list((evidence or {}).get("unconfirmed") or [])
    confirmed = bool((evidence or {}).get("confirmed", True))
    envelope_radius = metric_route.curve_chord_error_m if policy.get("use_curve_error_envelope", True) else 0.0
    envelope = metric_route.envelope(radius_m=envelope_radius)
    result["evidence"] = {
        "envelope_method": ENVELOPE_METHOD,
        "envelope_radius_m": envelope_radius,
        "confirmed_allowed_polygon_count": len(allowed_entries),
        "confirmed_blocked_polygon_count": len(blocked_entries),
        "unconfirmed_feature_count": len(unconfirmed_entries),
        "policy_confirmed": confirmed,
        "vector_predicate_semantics": (
            "exact_on_the_linearized_representation_including_the_explicit_curve_chord_error_envelope"
        ),
        "buffer_approximation": {**BUFFER_APPROXIMATION, "quad_segs": metric_route.quad_segs},
        "final_route_level_validation": True,
        "fine_cell_centre_only_test": False,
    }
    result["semantics"] = {
        "unknown_is_never_safe": True,
        "mixed_boundary_or_unconfirmed_is_unresolved": True,
        "horizontal_only_when_no_vertical_evidence": True,
    }
    if not confirmed:
        return _unresolved_only(result, "airspace_policy_not_confirmed", {
            "required": "confirmed_airspace_policy", "observed": None,
        })
    allowed_polygons = [polygon for polygon in (_polygon_from_evidence(entry) for entry in allowed_entries) if polygon is not None]
    blocked_polygons = [polygon for polygon in (_polygon_from_evidence(entry) for entry in blocked_entries) if polygon is not None]
    if len(allowed_polygons) != len(allowed_entries) or len(blocked_polygons) != len(blocked_entries):
        return _unresolved_only(result, "airspace_geometry_unavailable_or_invalid", {
            "required": "valid_confirmed_policy_geometry",
            "observed": {
                "allowed_readable": len(allowed_polygons),
                "allowed_declared": len(allowed_entries),
                "blocked_readable": len(blocked_polygons),
                "blocked_declared": len(blocked_entries),
            },
        })
    if not allowed_polygons:
        return _unresolved_only(result, "no_confirmed_allowed_airspace_geometry", {
            "required": "at_least_one_confirmed_allowed_polygon", "observed": 0,
        })
    from shapely.geometry import Point
    from shapely.ops import unary_union

    allowed_union = unary_union(allowed_polygons)
    blocked_union = unary_union(blocked_polygons) if blocked_polygons else None
    allow_touching = bool(policy.get("airspace_allow_touching_blocked_boundary", False))

    # 1. every point of the envelope must be inside the confirmed allowed union.
    coverage_gap = envelope.difference(allowed_union)
    interval_gap = None
    if coverage_gap is not None and not coverage_gap.is_empty:
        gap_area = float(coverage_gap.area)
    else:
        gap_area = 0.0
    # Small numerical slivers at exact boundaries are tolerated; a real gap is not.
    gap_tolerance = max(1e-9, (envelope_radius or 0.0) * 1e-6)
    gap_significant = gap_area > gap_tolerance
    if gap_significant:
        for part in _parts(coverage_gap):
            distances = [metric_route.distance_of([x, y]) for x, y in _coordinates(part)]
            if not distances:
                continue
            start, end = min(distances), max(distances)
            start_point = metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic)
            end_point = metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic)
            result["violations"].append(violation_interval(
                domain="airspace", reason_id="route_envelope_not_covered_by_confirmed_allowed_airspace",
                start_distance_m=start, end_distance_m=end,
                start_point=start_point, end_point=end_point,
                required="envelope_fully_covered_by_confirmed_allowed_union",
                observed="partially_outside_allowed_airspace",
                margin=-float(metric_route.line.distance(allowed_union)),
                evidence={"gap_area_m2": round(float(part.area), 6)},
            ))

    # Horizontal margin: the envelope's distance to the confirmed allowed boundary,
    # signed by whether the route itself is inside the confirmed allowed airspace.
    horizontal_margins = []
    try:
        allowed_distance = float(envelope.distance(allowed_union))
        inside_allowed = bool(allowed_union.covers(metric_route.line))
    except (AttributeError, TypeError, ValueError):
        allowed_distance, inside_allowed = None, False
    if allowed_distance is not None:
        signed = allowed_distance if inside_allowed else -allowed_distance
        horizontal_margins.append(signed if signed != 0.0 else 0.0)
    if gap_significant:
        horizontal_margins.append(-(gap_area ** 0.5))

    # 2. the envelope must not intersect the confirmed blocked union.
    if blocked_union is not None and not blocked_union.is_empty:
        intersection = envelope.intersection(blocked_union)
        if allow_touching:
            intersection = intersection.difference(envelope.boundary.buffer(0.0))
        if intersection is not None and not intersection.is_empty and float(intersection.area) > gap_tolerance:
            for part in _parts(intersection):
                distances = [metric_route.distance_of([x, y]) for x, y in _coordinates(part)]
                if not distances:
                    continue
                start, end = min(distances), max(distances)
                start_point = metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic)
                end_point = metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic)
                result["violations"].append(violation_interval(
                    domain="airspace", reason_id="route_envelope_intersects_confirmed_blocked_airspace",
                    start_distance_m=start, end_distance_m=end,
                    start_point=start_point, end_point=end_point,
                    required="envelope_disjoint_from_confirmed_blocked_union",
                    observed="intersects_confirmed_blocked_airspace",
                    margin=-float(part.area) ** 0.5,
                    evidence={"intersection_area_m2": round(float(part.area), 6)},
                ))
        # Horizontal margin to the blocked union (positive = clear).
        blocked_margin = float(metric_route.line.distance(blocked_union))
        horizontal_margins.append(blocked_margin)

    # 3. unconfirmed geometry touching the envelope keeps the domain unresolved.
    unresolved_intervals = []
    for entry in unconfirmed_entries:
        polygon = _polygon_from_evidence(entry)
        if polygon is None:
            continue
        if _bbox_gap(metric_route.line.bounds, polygon.bounds) is not None and (
            _bbox_gap(metric_route.line.bounds, polygon.bounds) > envelope_radius + envelope_radius
        ):
            continue
        for interval in metric_route.intervals_for_polygon(polygon):
            unresolved_intervals.append({
                "feature_id": entry.get("feature_id"),
                "start_distance_m": interval["start_distance_m"],
                "end_distance_m": interval["end_distance_m"],
                "reason": "unconfirmed_airspace_feature_overlaps_route_envelope",
            })
    result["evidence"]["horizontal_clearance_margin_m"] = (
        min(horizontal_margins) if horizontal_margins else None
    )
    result["evidence"]["coverage_gap_area_m2"] = round(gap_area, 9)
    result["evidence"]["envelope_area_m2"] = round(float(envelope.area), 9)
    result["minimum_margin"] = result["evidence"]["horizontal_clearance_margin_m"]
    result["margin_semantics"] = "signed_horizontal_margin_to_confirmed_policy_boundary_m"

    # 4. confirmed vertical evidence, if any: check z(s) over the intersection interval.
    vertical_violations, vertical_unresolved, horizontal_only = _airspace_vertical_checks(
        metric_route, allowed_entries, blocked_entries, to_geographic=to_geographic,
    )
    result["violations"].extend(vertical_violations)
    unresolved_intervals.extend(vertical_unresolved)
    result["evidence"]["horizontal_only_policy_evidence"] = horizontal_only
    result["evidence"]["vertical_evidence_semantics"] = (
        "confirmed_lower_upper_altitude_intersection_interval_checked_no_altitude_guessed"
        if not horizontal_only else
        "horizontal_only_policy_evidence_no_confirmed_vertical_evidence_altitude_never_guessed"
    )
    result["failed_interval_count"] = len(result["violations"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = result["violations"][0]["reason_id"]
        return result
    if unresolved_intervals:
        result["status"] = "unresolved"
        result["reason"] = "unresolved_airspace_evidence"
        for item in unresolved_intervals:
            start = item["start_distance_m"]
            end = item["end_distance_m"]
            result["unresolved"].append(violation_interval(
                domain="airspace", reason_id=item["reason"],
                start_distance_m=start, end_distance_m=end,
                start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                required="confirmed_airspace_classification", observed="unconfirmed",
                evidence={"feature_id": item["feature_id"]},
            ))
        result["unresolved_interval_count"] = len(result["unresolved"])
        return result
    result["status"] = "passed"
    return result


def _airspace_vertical_checks(metric_route, allowed_entries, blocked_entries, *, to_geographic):
    """z(s) checks for features that carry confirmed lower/upper altitude evidence."""

    violations, unresolved, horizontal_only = [], [], True
    for kind, entries in (("allowed", allowed_entries), ("blocked", blocked_entries)):
        for entry in entries:
            interval = entry.get("altitude_interval")
            if not interval or interval.get("confirmed") is not True:
                continue
            horizontal_only = False
            polygon = _polygon_from_evidence(entry)
            if polygon is None:
                continue
            if not _bbox_intersects(metric_route.line.bounds, polygon.bounds):
                continue
            for item in metric_route.intervals_for_polygon(polygon):
                start, end = item["start_distance_m"], item["end_distance_m"]
                z_min = metric_route.minimum_z(start, end)
                if z_min is None:
                    unresolved.append({
                        "feature_id": entry.get("feature_id"),
                        "start_distance_m": start, "end_distance_m": end,
                        "reason": "realized_altitude_profile_unresolved_for_airspace_interval",
                    })
                    continue
                lower = interval.get("lower_altitude_egm2008_m")
                upper = interval.get("upper_altitude_egm2008_m")
                if lower is not None and z_min < float(lower) - TOLERANCE:
                    violations.append(violation_interval(
                        domain="airspace", reason_id="route_below_confirmed_airspace_lower_altitude",
                        start_distance_m=start, end_distance_m=end,
                        start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                        end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                        required=float(lower), observed=z_min, margin=z_min - float(lower),
                        evidence={"feature_id": entry.get("feature_id"), "policy": kind},
                    ))
                if upper is not None and z_min > float(upper) + TOLERANCE:
                    violations.append(violation_interval(
                        domain="airspace", reason_id="route_above_confirmed_airspace_upper_altitude",
                        start_distance_m=start, end_distance_m=end,
                        start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                        end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                        required=float(upper), observed=z_min, margin=float(upper) - z_min,
                        evidence={"feature_id": entry.get("feature_id"), "policy": kind},
                    ))
    return violations, unresolved, horizontal_only


def _unresolved_only(result, reason, evidence):
    result["status"] = "unresolved"
    result["reason"] = reason
    result["unresolved"] = [violation_interval(
        domain=result["domain"], reason_id=reason,
        required=evidence.get("required"), observed=evidence.get("observed"),
        evidence=evidence,
    )]
    result["unresolved_interval_count"] = 1
    return result


# --------------------------------------------------------------------------- terrain


def validate_terrain(metric_route, *, evidence, policy, to_geographic=None):
    """Native-raster terrain validation over the pixels the realized path touches.

    Each native pixel that the route's uncertainty envelope intersects contributes a
    ``[elevation + terrain_clearance]`` floor and the along-track interval over which
    the route is influenced by it.  The route's minimum ``z`` on that interval must
    not be below the floor.  NoData ⇒ ``unresolved`` (never filled, never zero).
    """

    result = empty_domain_result("terrain", "passed")
    result["evaluated"] = True
    clearance = policy.get("terrain_clearance_m")
    pixels = list((evidence or {}).get("pixels") or [])
    source = (evidence or {}).get("source") or {}
    result["evidence"] = {
        "terrain_semantics": TERRAIN_SEMANTICS,
        "source": source,
        "pixel_count": len(pixels),
        "terrain_clearance_m": clearance,
        "envelope_radius_m": metric_route.curve_chord_error_m,
        "nodata_policy": "nodata_is_unresolved_never_bilinear_never_zero",
        "no_fine_cell_hard_floor_substitution": True,
        "claim": "source_native_raster_validation_not_real_world_terrain_exactness",
    }
    result["semantics"] = {
        "source_native_raster_validation": True,
        "not_mathematically_continuous_real_world_terrain": True,
        "unknown_is_never_safe": True,
    }
    if not (evidence or {}).get("available", True):
        return _unresolved_only(result, "native_terrain_source_unavailable", {
            "required": "configured_native_dtm_raster", "observed": None,
        })
    if clearance is None:
        return _unresolved_only(result, "terrain_clearance_m_unresolved", {
            "required": "explicit_terrain_clearance_m", "observed": None,
        })
    if not pixels:
        return _unresolved_only(result, "no_native_terrain_pixel_intersects_the_realized_route", {
            "required": "at_least_one_native_dtm_pixel", "observed": 0,
        })
    margins = []
    unresolved = []
    for pixel in pixels:
        interval = pixel.get("interval") or {}
        start, end = interval.get("start_distance_m"), interval.get("end_distance_m")
        if start is None or end is None:
            continue
        elevation = pixel.get("elevation_egm2008_m")
        status = str(pixel.get("data_status") or "unknown")
        if status != "passed" or elevation is None:
            unresolved.append({
                "reason": pixel.get("reason") or "native_terrain_pixel_nodata",
                "pixel": pixel.get("pixel"),
                "start_distance_m": start, "end_distance_m": end,
            })
            continue
        required_floor = float(elevation) + float(clearance)
        observed = metric_route.minimum_z(start, end)
        if observed is None:
            unresolved.append({
                "reason": "realized_altitude_profile_unresolved_for_terrain_interval",
                "pixel": pixel.get("pixel"),
                "start_distance_m": start, "end_distance_m": end,
            })
            continue
        margin = observed - required_floor
        margins.append(margin)
        if margin < -TOLERANCE:
            result["violations"].append(violation_interval(
                domain="terrain", reason_id="below_native_terrain_clearance",
                start_distance_m=start, end_distance_m=end,
                start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                required=required_floor, observed=observed, margin=margin,
                evidence={
                    "pixel": pixel.get("pixel"),
                    "source_value": pixel.get("source_value"),
                    "elevation_egm2008_m": elevation,
                    "terrain_clearance_m": clearance,
                    "crs": source.get("crs"),
                    "nodata": source.get("nodata"),
                },
            ))
    result["minimum_margin"] = min(margins) if margins else None
    result["margin_semantics"] = "minimum_z_minus_terrain_elevation_plus_explicit_clearance_m"
    result["item_count"] = len(pixels)
    result["failed_interval_count"] = len(result["violations"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = "below_native_terrain_clearance"
        return result
    if unresolved:
        result["status"] = "unresolved"
        result["reason"] = "terrain_nodata_or_unresolved_pixels"
        for item in unresolved:
            start, end = item["start_distance_m"], item["end_distance_m"]
            result["unresolved"].append(violation_interval(
                domain="terrain", reason_id=item["reason"],
                start_distance_m=start, end_distance_m=end,
                start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                required="valid_native_dtm_pixel", observed="nodata_or_unresolved",
                evidence={"pixel": item.get("pixel")},
            ))
        result["unresolved_interval_count"] = len(result["unresolved"])
        return result
    result["status"] = "passed"
    return result


# --------------------------------------------------------------------------- buildings


def validate_buildings(metric_route, *, evidence, policy, to_geographic=None):
    """Real-footprint building clearance on the realized route.

    The footprint (buffered by the explicit horizontal clearance *plus* the curve
    error) is intersected with the route; over each affected interval the route's
    minimum ``z`` is compared with ``roof + vertical_clearance`` using the shared
    building-clearance roof semantics.  Missing height / ground is ``unresolved``.

    Footprint geometry passes through one explicit quality gate first
    (``domain/building_geometry_quality``): the source ring is never rewritten, an invalid
    ring is repaired in memory with shapely ``make_valid`` when that yields a valid polygon
    (every part is then evaluated), and an unrepairable ring stays ``unresolved`` — it is
    never treated as "no building" and never as a pass.  The buffer approximation is
    recorded, and a per-footprint quality report is attached to ``evidence``.
    """

    from ..domain.building_geometry_quality import (
        empty_geometry_quality_report, merge_geometry_quality, prepare_footprint_polygons,
    )

    result = empty_domain_result("building", "passed")
    result["evaluated"] = True
    horizontal = policy.get("building_horizontal_clearance_m")
    vertical = policy.get("building_vertical_clearance_m")
    curve_error = metric_route.curve_chord_error_m if policy.get("use_curve_error_envelope", True) else 0.0
    footprints = list((evidence or {}).get("buildings") or [])
    result["evidence"] = {
        "source": (evidence or {}).get("source") or {},
        "building_count": len(footprints),
        "horizontal_clearance_m": horizontal,
        "vertical_clearance_m": vertical,
        "curve_error_m": curve_error,
        "metric_buffer_distance_m": (
            None if horizontal is None else float(horizontal) + float(curve_error)
        ),
        "buffer_approximation": {**BUFFER_APPROXIMATION, "quad_segs": metric_route.quad_segs},
        "roof_semantics": "shared_building_clearance_roof_elevation_helper",
        "source_geometry_modified": False,
        "make_valid_applied": False,
        "query": "route_bbox_plus_clearance_provider_spatial_index",
    }
    result["semantics"] = {
        "exact_polygon_clearance_on_the_query_result": True,
        "unknown_is_never_safe": True,
        "unknown_never_confirmed_safe": True,
    }
    if horizontal is None or vertical is None:
        return _unresolved_only(result, "building_clearance_parameters_unresolved", {
            "required": "explicit_horizontal_and_vertical_clearance_m",
            "observed": {"horizontal": horizontal, "vertical": vertical},
        })
    if not (evidence or {}).get("available", True):
        return _unresolved_only(result, "building_source_unavailable", {
            "required": "configured_building_source_with_spatial_index", "observed": None,
        })
    minimum_horizontal = None
    minimum_vertical = None
    unresolved = []
    geometry_quality = empty_geometry_quality_report()
    # BUG-VALIDATION-BUILDING-003：``query_route`` 返回的是 route bbox（+ clearance）内的建筑，
    # 因此 bbox 里**完全不影响航路**的建筑过去也会先被要求 ground/height，缺失即 unresolved ——
    # 真实验证里 terrain passed 而 building unresolved 全部落在这个原因上。判定顺序固定为：
    #   ① footprint geometry quality 仍然最先 fail-closed；
    #   ② 对所有 valid polygon part 先计算与 ``metric_route.line`` 的距离，并取
    #      ``buffer_distance = horizontal_clearance + curve_error``；
    #   ③ 所有 part 都不影响 route/buffer → 不要求 ground/height、不 unresolved，直接跳过；
    #   ④ 至少一个 part 真正影响 route/buffer → 才要求 ground + height。
    # 不给 ground 任何默认值，也不放松 fail-closed：真正相交但证据缺失仍然是 unresolved。
    buffer_distance = float(horizontal) + float(curve_error)
    for footprint in footprints:
        identifier = footprint.get("building_id")
        # Geometry quality gate: the source ring is **never** rewritten, an invalid ring is
        # repaired in memory with shapely ``make_valid`` when that yields a valid polygon, and
        # an unrepairable ring stays ``unresolved`` (never "no building" and never a pass).
        prepared = prepare_footprint_polygons(footprint)
        for record in prepared["records"]:
            merge_geometry_quality(geometry_quality, record)
        if prepared["quality"] == "invalid":
            unresolved.append({
                "building_id": identifier,
                # Stable reason id for "this footprint could not be interpreted".  It stays
                # free of the word "geometry" so the frontend validation panel can keep its
                # "no interval polygon / no coordinate derivation" guarantee while this
                # reason is rendered verbatim.
                "reason": "building_footprint_quality_unresolved",
                "internal_geometry_quality_status": prepared["quality"],
                "internal_geometry_quality": prepared["annotation"],
            })
            continue
        # Every part of the footprint is considered separately: taking only the largest part
        # could hide a part that really reaches into the route/buffer corridor.
        affected = []
        for piece_index, polygon in enumerate(prepared["polygons"]):
            distance = float(polygon.distance(metric_route.line))
            minimum_horizontal = (
                distance if minimum_horizontal is None else min(minimum_horizontal, distance)
            )
            # Cheap and exact for the linearized representation: a part that stays farther than
            # the buffer distance cannot intersect the buffered corridor.
            if distance > buffer_distance + TOLERANCE:
                continue
            buffered = polygon.buffer(buffer_distance, quad_segs=metric_route.quad_segs)
            intersection = metric_route.line.intersection(buffered)
            if intersection.is_empty:
                continue
            distances = [metric_route.distance_of([x, y]) for x, y in _coordinates(intersection)]
            if not distances:
                continue
            affected.append((piece_index, distance, min(distances), max(distances)))
        if not affected:
            # Inside the query bbox but provably not affecting the route/buffer corridor: no
            # ground/height evidence is required and nothing becomes unresolved.
            continue
        height_status = footprint.get("height_status")
        if not building_height_status_is_resolved(height_status):
            unresolved.append({
                "building_id": identifier,
                "reason": "building_height_status_unresolved",
                "height_status": height_status,
                "height_m": footprint.get("height_m"),
            })
            continue
        # Every affected part is evaluated separately: taking only the largest part could hide a
        # penetrating roof on a smaller part.
        part_grounds = footprint.get("ground_elevation_by_part_egm2008_m")
        for piece_index, distance, start, end in affected:
            ground = footprint.get("ground_elevation_max_egm2008_m")
            if isinstance(part_grounds, (list, tuple)):
                ground = part_grounds[piece_index] if piece_index < len(part_grounds) else None
            roof = building_roof_elevation(ground, footprint.get("height_m"))
            if roof.get("status") != "resolved":
                unresolved.append({
                    "building_id": identifier, "reason": roof.get("reason"),
                    "footprint_part_index": piece_index,
                    "ground_elevation_max_egm2008_m": ground,
                    "height_m": footprint.get("height_m"),
                })
                continue
            roof_m = float(roof["roof_elevation_egm2008_m"])
            required_clearance = roof_m + float(vertical)
            observed = metric_route.minimum_z(start, end)
            if observed is None:
                unresolved.append({
                    "building_id": identifier,
                    "reason": "realized_altitude_profile_unresolved_for_building_interval",
                    "footprint_part_index": piece_index,
                })
                continue
            evaluation = evaluate_vertical_clearance(
                minimum_altitude_egm2008_m=observed, roof_elevation_egm2008_m=roof_m,
                required_clearance_m=vertical,
                ground_elevation_m=ground,
            )
            margin = evaluation["vertical_margin_m"]
            if margin is not None:
                minimum_vertical = margin if minimum_vertical is None else min(minimum_vertical, margin)
            if evaluation["status"] != "resolved":
                unresolved.append({
                    "building_id": identifier, "reason": evaluation.get("reason"),
                    "footprint_part_index": piece_index,
                })
                continue
            if margin < -TOLERANCE:
                result["violations"].append(violation_interval(
                    domain="building", reason_id="building_vertical_clearance_violated",
                    start_distance_m=start, end_distance_m=end,
                    start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                    end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                    required=required_clearance, observed=observed, margin=margin,
                    evidence={
                        "building_id": identifier,
                        "building_source": footprint.get("source"),
                        "height_m": footprint.get("height_m"),
                        "ground_elevation_max_egm2008_m": ground,
                        "roof_elevation_egm2008_m": roof_m,
                        "horizontal_clearance_m": horizontal,
                        "vertical_clearance_m": vertical,
                        "curve_error_m": curve_error,
                        "horizontal_distance_m": distance,
                        "interval_length_m": float(end) - float(start),
                        "footprint_part_index": piece_index,
                        "footprint_part_count": len(prepared["polygons"]),
                    },
                ))
    result["evidence"]["minimum_horizontal_distance_m"] = minimum_horizontal
    result["evidence"]["building_quality_report"] = geometry_quality
    result["evidence"]["geometry_quality"] = geometry_quality
    result["evidence"]["source_modified"] = False
    # Reporting only: geometry quality never changes the verdict on its own.  A repaired-but-
    # valid footprint can still be a genuine penetration, and an unrepairable one stays
    # unresolved instead of "no building".
    result["evidence"]["make_valid_applied"] = bool(geometry_quality["repair"]["applied_count"])
    result["evidence"]["prefer_shapely_make_valid"] = True
    result["evidence"]["unrepairable_geometry_stays_unknown"] = True
    result["minimum_margin"] = minimum_vertical
    result["margin_semantics"] = "minimum_z_minus_roof_minus_explicit_vertical_clearance_m"
    result["item_count"] = len(footprints)
    result["failed_interval_count"] = len(result["violations"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = "building_vertical_clearance_violated"
        return result
    if unresolved:
        result["status"] = "unresolved"
        result["reason"] = "building_evidence_unresolved"
        for item in unresolved:
            result["unresolved"].append(violation_interval(
                domain="building", reason_id=item.get("reason") or "building_evidence_unresolved",
                required="confirmed_height_and_ground_and_valid_geometry",
                observed="unresolved",
                evidence=item,
            ))
        result["unresolved_interval_count"] = len(result["unresolved"])
        return result
    result["status"] = "passed"
    return result


# --------------------------------------------------------------------------- towers


def validate_towers(metric_route, *, evidence, policy, to_geographic=None):
    """Independently validate confirmed tower tops against the realized route buffer.

    A numerically resolved tower top is not publication evidence.  Every tower that
    horizontally affects the route must carry ``confirmed=True`` and a concrete
    EGM2008 orthometric top, otherwise the domain is unresolved.
    """

    from shapely.geometry import Point

    result = empty_domain_result("tower", "passed")
    result["evaluated"] = True
    source = evidence if isinstance(evidence, dict) else {}
    if source.get("applicability") == "not_applicable" or (
        "towers" not in source and not source
    ):
        result["evidence"] = {"applicability": "not_applicable", "tower_count": 0}
        result["semantics"] = {"resolved_is_not_confirmed": True}
        return result
    if str(source.get("status") or "passed") not in ("passed", "resolved"):
        return _unresolved_only(result, "tower_source_unavailable", {
            "required": "confirmed_tower_obstacle_source", "observed": source.get("status"),
        })
    horizontal = policy.get("tower_horizontal_clearance_m")
    vertical = policy.get("tower_vertical_clearance_m")
    if not finite_number(horizontal) or not finite_number(vertical):
        return _unresolved_only(result, "tower_clearance_parameters_unresolved", {
            "required": "explicit_tower_horizontal_and_vertical_clearance_m",
            "observed": {"horizontal": horizontal, "vertical": vertical},
        })
    horizontal, vertical = float(horizontal), float(vertical)
    towers = [item for item in source.get("towers") or [] if isinstance(item, dict)]
    result["item_count"] = len(towers)
    result["evidence"] = {
        "tower_count": len(towers), "source": source.get("source"),
        "tower_horizontal_clearance_m": horizontal,
        "tower_vertical_clearance_m": vertical,
        "applicability": source.get("applicability") or "applicable",
    }
    result["semantics"] = {
        "resolved_is_not_confirmed": True,
        "confirmed_top_required_for_vertical_verdict": True,
        "independent_from_search_constraint_field": True,
    }
    margins = []
    for tower in towers:
        point = tower.get("point_metric")
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        geometry = Point(float(point[0]), float(point[1]))
        distance = float(metric_route.line.distance(geometry))
        if distance > horizontal + metric_route.curve_chord_error_m + TOLERANCE:
            continue
        along = metric_route.distance_of(point)
        z_value = metric_route.vertical_z(along)
        top = tower.get("tower_top_orthometric_m")
        tower_id = tower.get("tower_id")
        if tower.get("confirmed") is not True or not finite_number(top):
            result["unresolved"].append(violation_interval(
                domain="tower", reason_id="tower_top_not_confirmed",
                start_distance_m=along, end_distance_m=along,
                start_point=metric_route.to_geographic(point, to_geographic=to_geographic),
                end_point=metric_route.to_geographic(point, to_geographic=to_geographic),
                required="confirmed_tower_top_orthometric_m", observed=top,
                evidence={
                    "tower_id": tower_id, "tower_status": tower.get("status"),
                    "tower_top_status": tower.get("tower_top_status"),
                    "horizontal_distance_m": distance,
                },
            ))
            continue
        if z_value is None:
            result["unresolved"].append(violation_interval(
                domain="tower", reason_id="realized_altitude_profile_unresolved_for_tower",
                start_distance_m=along, end_distance_m=along,
                required=float(top) + vertical, observed=None,
                evidence={"tower_id": tower_id},
            ))
            continue
        required = float(top) + vertical
        margin = float(z_value) - required
        margins.append(margin)
        if float(z_value) < required - TOLERANCE:
            result["violations"].append(violation_interval(
                domain="tower", reason_id="below_confirmed_tower_clearance",
                start_distance_m=along, end_distance_m=along,
                start_point=metric_route.to_geographic(point, to_geographic=to_geographic),
                end_point=metric_route.to_geographic(point, to_geographic=to_geographic),
                required=required, observed=float(z_value), margin=margin,
                evidence={
                    "tower_id": tower_id, "tower_top_orthometric_m": float(top),
                    "tower_vertical_clearance_m": vertical,
                    "horizontal_distance_m": distance,
                    "confirmation_authority": tower.get("confirmation_authority"),
                },
            ))
    result["minimum_margin"] = min(margins) if margins else None
    result["margin_semantics"] = "route_z_minus_confirmed_tower_top_plus_clearance_m"
    result["failed_interval_count"] = len(result["violations"])
    result["unresolved_interval_count"] = len(result["unresolved"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = "below_confirmed_tower_clearance"
    elif result["unresolved"]:
        result["status"] = "unresolved"
        result["reason"] = "tower_evidence_unresolved"
    return result


# --------------------------------------------------------------------------- restricted areas


def validate_restricted_areas(metric_route, *, evidence, policy, vertical_reference,
                              to_geographic=None):
    """Validate route intersection with independently supplied protected geometry."""

    from shapely.geometry import Point, shape

    result = empty_domain_result("restricted_area", "passed")
    result["evaluated"] = True
    source = evidence if isinstance(evidence, dict) else {}
    if source.get("applicability") == "not_applicable" or (
        "areas" not in source and not source
    ):
        result["evidence"] = {"applicability": "not_applicable", "area_count": 0}
        return result
    if str(source.get("status") or "passed") not in ("passed", "resolved"):
        return _unresolved_only(result, "restricted_area_source_unavailable", {
            "required": "audited_restricted_area_source", "observed": source.get("status"),
        })
    areas = [item for item in source.get("areas") or [] if isinstance(item, dict)]
    conditional = {
        str(item) for item in policy.get("conditional_hard_exclusion_feature_ids") or []
    }
    route_altitude = metric_route.minimum_z(0.0, metric_route.length_m)
    result["item_count"] = len(areas)
    result["evidence"] = {
        "area_count": len(areas), "source": source.get("source"),
        "applicability": source.get("applicability") or "applicable",
    }
    result["semantics"] = {
        "protection_geometry_required": True,
        "category_does_not_imply_radius": True,
        "advisory_is_not_hard_block": True,
        "independent_from_search_constraint_field": True,
    }
    for raw in areas:
        try:
            area = normalize_restricted_area(raw)
        except ValueError as exc:
            result["unresolved"].append(violation_interval(
                domain="restricted_area", reason_id="restricted_area_contract_invalid",
                required="valid_restricted_area_contract", observed=str(exc),
                evidence={"feature_id": raw.get("feature_id")},
            ))
            continue
        metric_geometry = raw.get("planning_geometry_metric") or raw.get("protection_geometry_metric")
        source_point = raw.get("point_metric")
        if metric_geometry is None and area.get("geometry_status") == "source_point_only_no_protection_geometry":
            if isinstance(source_point, (list, tuple)) and len(source_point) >= 2:
                point = Point(float(source_point[0]), float(source_point[1]))
                if metric_route.line.distance(point) <= metric_route.curve_chord_error_m + TOLERANCE:
                    along = metric_route.distance_of(source_point)
                    result["unresolved"].append(violation_interval(
                        domain="restricted_area", reason_id="protection_geometry_unresolved",
                        start_distance_m=along, end_distance_m=along,
                        required="source_or_audited_policy_protection_geometry", observed="source_point_only",
                        evidence={"feature_id": area.get("feature_id"), "category": area.get("category")},
                    ))
            continue
        if not isinstance(metric_geometry, dict):
            continue
        try:
            geometry = shape(metric_geometry)
        except (TypeError, ValueError):
            result["unresolved"].append(violation_interval(
                domain="restricted_area", reason_id="protection_geometry_invalid",
                required="valid_metric_polygon", observed=None,
                evidence={"feature_id": area.get("feature_id")},
            ))
            continue
        intervals = metric_route.intervals_for_polygon(geometry)
        if not intervals:
            continue
        interval = intervals[0]
        start, end = interval["start_distance_m"], interval["end_distance_m"]
        applicability = (
            altitude_applicability(
                area, altitude_m=route_altitude, vertical_reference=vertical_reference,
            ) if route_altitude is not None else "unknown"
        )
        if area.get("confirmed") is not True:
            reason = "restricted_area_unconfirmed"
        elif applicability == "unknown":
            reason = "restricted_area_vertical_scope_unresolved"
        elif applicability == "not_applicable" or area.get("constraint_type") == "advisory":
            continue
        elif area.get("constraint_type") == "conditional" and area.get("feature_id") not in conditional:
            reason = "conditional_restricted_area_policy_unresolved"
        else:
            reason = None
        common = {
            "domain": area.get("domain"), "feature_id": area.get("feature_id"),
            "constraint_type": area.get("constraint_type"),
            "vertical_applicability": applicability,
        }
        if reason:
            result["unresolved"].append(violation_interval(
                domain="restricted_area", reason_id=reason,
                start_distance_m=start, end_distance_m=end,
                start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                required="confirmed_applicable_protection_policy", observed="unresolved",
                evidence=common,
            ))
        else:
            result["violations"].append(violation_interval(
                domain="restricted_area", reason_id="confirmed_hard_exclusion_intersection",
                start_distance_m=start, end_distance_m=end,
                start_point=metric_route.to_geographic(metric_route.sample_at(start), to_geographic=to_geographic),
                end_point=metric_route.to_geographic(metric_route.sample_at(end), to_geographic=to_geographic),
                required="no_intersection", observed="intersects", evidence=common,
            ))
    result["failed_interval_count"] = len(result["violations"])
    result["unresolved_interval_count"] = len(result["unresolved"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = "confirmed_hard_exclusion_intersection"
    elif result["unresolved"]:
        result["status"] = "unresolved"
        result["reason"] = "restricted_area_evidence_unresolved"
    return result


# --------------------------------------------------------------------------- kinematics


def validate_kinematics(route, *, policy, aircraft_motion_limits=None):
    """Re-verify minimum radius, tangent heading continuity and gradients."""

    result = empty_domain_result("kinematics", "passed")
    result["evaluated"] = True
    limits = aircraft_motion_limits or {}
    min_radius_required = policy.get("aircraft_min_turn_radius_m")
    if min_radius_required is None and limits:
        min_radius_required = limits.get("min_turn_radius_m")
    climb_limit = policy.get("max_climb_gradient")
    descent_limit = policy.get("max_descent_gradient")
    if climb_limit is None and limits:
        climb_limit = limits.get("max_climb_gradient")
    if descent_limit is None and limits:
        descent_limit = limits.get("max_descent_gradient")
    primitives = list((route or {}).get("primitives") or [])
    turns = list((route or {}).get("turns") or [])
    radii = [
        float(turn["radius_m"]) for turn in turns
        if finite_number(turn.get("radius_m"))
    ]
    observed_min_radius = min(radii) if radii else None
    tangent_ok, tangent_detail = _tangent_continuity(route)
    gradients = [
        (primitive.get("gradient"), primitive) for primitive in primitives
        if finite_number(primitive.get("gradient"))
    ]
    climb_observed = max([value for value, _ in gradients if value > 0] or [0.0])
    descent_observed = min([value for value, _ in gradients if value < 0] or [0.0])
    self_intersection = _self_intersection_diagnostic(route)
    result["evidence"] = {
        "minimum_turn_radius_observed_m": observed_min_radius,
        "required_minimum_turn_radius_m": min_radius_required,
        "max_climb_gradient_observed": climb_observed,
        "max_descent_gradient_observed": descent_observed,
        "max_allowed_climb_gradient": climb_limit,
        "max_allowed_descent_gradient": descent_limit,
        "tangent_heading_continuity_verified": tangent_ok,
        "tangent_continuity_detail": tangent_detail,
        "turn_verification": "analytic_arc_radius_and_tangent_heading_not_the_v3b_arc_length_proxy",
        "arc_count": len(turns),
        "primitive_count": len(primitives),
        "self_intersection_diagnostic": self_intersection,
        "self_intersection_is_failure": bool(policy.get("self_intersection_is_failure", False)),
        "self_intersection_semantics": (
            "diagnostic_only_not_automatically_unsafe_unless_policy_explicitly_requires"
        ),
    }
    result["semantics"] = {
        "analytic_radius_and_tangent_heading_verified": True,
        "v3b_arc_length_proxy_is_not_the_final_turn_validation": True,
        "self_intersection_diagnostic_only": not bool(policy.get("self_intersection_is_failure", False)),
    }
    radius_margins, climb_margins, descent_margins = [], [], []
    if min_radius_required is None:
        result["status"] = "unresolved"
        result["reason"] = "minimum_turn_radius_unresolved"
        result["unresolved"].append(violation_interval(
            domain="kinematics", reason_id="minimum_turn_radius_unresolved",
            required="explicit_aircraft_min_turn_radius_m", observed=None,
        ))
    elif observed_min_radius is not None:
        radius_margins.append(observed_min_radius - float(min_radius_required))
        if observed_min_radius < float(min_radius_required) - TOLERANCE:
            result["violations"].append(violation_interval(
                domain="kinematics", reason_id="analytic_turn_radius_below_minimum",
                required=float(min_radius_required), observed=observed_min_radius,
                margin=observed_min_radius - float(min_radius_required),
                evidence={"arc_count": len(turns)},
            ))
    if not tangent_ok:
        result["violations"].append(violation_interval(
            domain="kinematics", reason_id="tangent_heading_continuity_broken",
            required="C1_position_and_heading_continuity", observed="discontinuous",
            evidence={"detail": tangent_detail},
        ))
    if climb_limit is None or descent_limit is None:
        if result["status"] != "unresolved":
            result["status"] = "unresolved"
            result["reason"] = "climb_or_descent_gradient_limit_unresolved"
        result["unresolved"].append(violation_interval(
            domain="kinematics", reason_id="climb_or_descent_gradient_limit_unresolved",
            required="explicit_max_climb_and_descent_gradient",
            observed={"climb": climb_limit, "descent": descent_limit},
        ))
    else:
        climb_margins.append(float(climb_limit) - climb_observed)
        descent_margins.append(float(descent_limit) - abs(descent_observed))
        for value, primitive in gradients:
            if value > float(climb_limit) + TOLERANCE:
                result["violations"].append(_gradient_violation(
                    primitive, "continuous_primitive_climb_gradient_exceeded",
                    float(climb_limit), value,
                ))
            if -value > float(descent_limit) + TOLERANCE:
                result["violations"].append(_gradient_violation(
                    primitive, "continuous_primitive_descent_gradient_exceeded",
                    float(descent_limit), abs(value),
                ))
    margins = radius_margins + climb_margins + descent_margins
    result["minimum_margin"] = min(margins) if margins else None
    result["margin_semantics"] = "minimum_of_radius_and_gradient_margins"
    result["item_count"] = len(primitives)
    result["failed_interval_count"] = len(result["violations"])
    result["unresolved_interval_count"] = len(result["unresolved"])
    if result["violations"]:
        result["status"] = "failed"
        result["reason"] = result["violations"][0]["reason_id"]
        return result
    if result["status"] == "unresolved" or result["unresolved"]:
        result["status"] = "unresolved"
        result["reason"] = result["reason"] or "kinematic_evidence_unresolved"
        return result
    result["status"] = "passed"
    result["reason"] = None
    return result


def _gradient_violation(primitive, reason_id, required, observed):
    return violation_interval(
        domain="kinematics", reason_id=reason_id,
        start_distance_m=primitive.get("distance_start_m"),
        end_distance_m=primitive.get("distance_end_m"),
        start_point=primitive.get("start_metric"), end_point=primitive.get("end_metric"),
        required=required, observed=observed, margin=required - observed,
        evidence={
            "primitive_id": primitive.get("primitive_id"),
            "kind": primitive.get("kind"),
            "horizontal_length_m": primitive.get("horizontal_length_m"),
            "length_3d_m": primitive.get("length_3d_m"),
            "z_start_egm2008_m": primitive.get("z_start_egm2008_m"),
            "z_end_egm2008_m": primitive.get("z_end_egm2008_m"),
        },
    )


def _tangent_continuity(route, *, tolerance_deg=1e-6):
    """Verify heading continuity between consecutive primitives, analytically."""

    primitives = list((route or {}).get("primitives") or [])
    detail = []
    ok = True
    for index in range(len(primitives) - 1):
        left, right = primitives[index], primitives[index + 1]
        delta = _heading_gap_deg(left.get("end_heading_deg"), right.get("start_heading_deg"))
        if delta is None:
            ok = False
            detail.append({"index": index, "reason": "heading_unresolved"})
            continue
        if delta > tolerance_deg:
            ok = False
            detail.append({
                "index": index, "reason": "heading_discontinuity_deg", "delta_deg": delta,
                "left_end_heading_deg": left.get("end_heading_deg"),
                "right_start_heading_deg": right.get("start_heading_deg"),
            })
    # Within an arc the heading sweeps continuously by construction; verify that the
    # analytic arc endpoints reproduce the primitive headings.
    for turn in (route or {}).get("turns") or []:
        arc = turn.get("arc") or {}
        if not arc.get("center_metric") or not finite_number(arc.get("radius_m")):
            ok = False
            detail.append({"turn_id": turn.get("turn_id"), "reason": "arc_geometry_unresolved"})
            continue
        verified = arc.get("radius_verified_from_center")
        if not finite_number(verified) or abs(float(verified) - float(arc["radius_m"])) > 1e-6:
            ok = False
            detail.append({"turn_id": turn.get("turn_id"), "reason": "arc_radius_inconsistent"})
        for key, other in (
            ("start_heading_deg", "end_heading_deg"),
        ):
            gap = _heading_gap_deg(turn.get(key), turn.get(other))
            if gap is None or abs(gap - abs(float(turn.get("heading_change_deg") or 0.0))) > 1e-6:
                ok = False
                detail.append({
                    "turn_id": turn.get("turn_id"),
                    "reason": "arc_heading_change_inconsistent_with_geometry",
                    "turn_angle_deg": turn.get("turn_angle_deg"),
                })
    return ok, detail


def _heading_gap_deg(left, right):
    """Smallest absolute heading difference in degrees, or ``None`` if unresolved."""

    if left is None or right is None:
        return None
    return abs(((float(left) - float(right) + 180.0) % 360.0) - 180.0)


def _self_intersection_diagnostic(route):
    """Planar self-intersection diagnostic of the linearized route (never a verdict)."""

    try:
        from shapely.geometry import LineString
    except ImportError:
        return None
    points = [
        [float(point[0]), float(point[1])]
        for point in ((route or {}).get("horizontal_geometry") or {}).get("linearized", {}).get(
            "linestring_metric",
        ) or []
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if len(points) < 3:
        return {"available": False, "reason": "not_enough_points"}
    line = LineString([(point[0], point[1]) for point in points])
    if line.is_simple:
        return {"available": True, "self_intersecting": False, "semantics": "diagnostic_only"}
    return {
        "available": True,
        "self_intersecting": True,
        "semantics": "diagnostic_only_not_automatically_unsafe",
        "note": "linearized representation may self-intersect at a turn re-entry",
    }


__all__ = [
    "BUFFER_APPROXIMATION", "DEFAULT_QUAD_SEGS", "ENVELOPE_METHOD", "MetricRoute",
    "TERRAIN_SEMANTICS", "metric_distance", "validate_airspace",
    "validate_altitude_bounds", "validate_buildings", "validate_geometry",
    "validate_kinematics", "validate_restricted_areas", "validate_terrain", "validate_towers",
]
