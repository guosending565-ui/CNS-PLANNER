"""Precompute JSON-safe route eligibility outside the pure-Python planner."""

from __future__ import annotations

from hashlib import sha256
import json


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def empty_airspace_eligibility(status="missing_data", message="没有 confirmed allowed airspace"):
    return {
        "status": status,
        "message": message,
        "allowed_grid_ids": [],
        "connector_safe_grid_ids": [],
        "allowed_edges": [],
        "feature_counts": {"allowed": 0, "blocked": 0, "unknown": 0},
        "feature_fingerprint": None,
        "policy_fingerprint": None,
        "fingerprint": None,
    }


class AirspaceEligibilityService:
    algorithm_id = "confirmed-airspace-eligibility"
    algorithm_version = "1.0"

    def build(self, cells, features, policies):
        policy_items = (policies or {}).get("items") if isinstance(policies, dict) else policies
        policy_by_id = {
            str(item.get("feature_id")): item
            for item in (policy_items or []) if isinstance(item, dict) and item.get("feature_id")
        }
        enriched, allowed_geometries = [], []
        counts = {"allowed": 0, "blocked": 0, "unknown": 0}
        for raw in features or []:
            feature = dict(raw)
            policy = policy_by_id.get(str(feature.get("feature_id"))) or {}
            eligibility = str(policy.get("route_eligibility") or "unknown").lower()
            confirmed = policy.get("confirmed") is True
            effective = eligibility if confirmed and eligibility in ("allowed", "blocked") else "unknown"
            counts[effective] += 1
            feature["route_eligibility"] = effective
            feature["policy_confirmed"] = confirmed
            feature["policy_source"] = policy.get("source")
            enriched.append(feature)
            if effective == "allowed" and feature.get("geometry"):
                allowed_geometries.extend(_polygons(feature["geometry"]))

        feature_fingerprint = _hash(enriched)
        policy_fingerprint = _hash(sorted(policy_by_id.values(), key=lambda item: str(item.get("feature_id"))))
        if not allowed_geometries:
            result = empty_airspace_eligibility()
            result.update({
                "algorithm_id": self.algorithm_id,
                "algorithm_version": self.algorithm_version,
                "features": enriched,
                "feature_counts": counts,
                "feature_fingerprint": feature_fingerprint,
                "policy_fingerprint": policy_fingerprint,
            })
            result["fingerprint"] = _hash({"features": feature_fingerprint, "policies": policy_fingerprint})
            return result

        cell_by_id = {str(cell["grid_id"]): cell for cell in cells or []}
        allowed_ids = sorted(
            grid_id for grid_id, cell in cell_by_id.items()
            if any(_polygon_covers_rect(polygon, cell["bbox"]) for polygon in allowed_geometries)
        )
        allowed_set = set(allowed_ids)
        centers = {grid_id: _center(cell) for grid_id, cell in cell_by_id.items()}
        center_index = {(round(point[0], 12), round(point[1], 12)): grid_id for grid_id, point in centers.items()}
        edges = []
        ids = sorted(cell_by_id)
        for index, left in enumerate(ids):
            if left not in allowed_set:
                continue
            for right in ids[index + 1:]:
                if right not in allowed_set or not _bbox_touches(cell_by_id[left]["bbox"], cell_by_id[right]["bbox"]):
                    continue
                a, b = centers[left], centers[right]
                if a[0] != b[0] and a[1] != b[1]:
                    guards = (
                        center_index.get((round(a[0], 12), round(b[1], 12))),
                        center_index.get((round(b[0], 12), round(a[1], 12))),
                    )
                    if None in guards or any(item not in allowed_set for item in guards):
                        continue
                if _segment_covered_by_union(a, b, allowed_geometries):
                    edges.append([left, right])
        result = {
            "status": "passed",
            "message": "仅 confirmed allowed geometry 可用于运行航路",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "allowed_grid_ids": allowed_ids,
            "connector_safe_grid_ids": allowed_ids,
            "allowed_edges": edges,
            "feature_counts": counts,
            "features": enriched,
            "feature_fingerprint": feature_fingerprint,
            "policy_fingerprint": policy_fingerprint,
        }
        result["fingerprint"] = _hash({
            "features": feature_fingerprint,
            "policies": policy_fingerprint,
            "allowed_grid_ids": allowed_ids,
            "allowed_edges": edges,
        })
        return result


def _center(cell):
    center = cell.get("center")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        return [float(center[0]), float(center[1])]
    west, south, east, north = (float(value) for value in cell["bbox"])
    return [(west + east) / 2.0, (south + north) / 2.0]


def _bbox_touches(a, b):
    a = [float(value) for value in a]
    b = [float(value) for value in b]
    x_overlap = min(a[2], b[2]) - max(a[0], b[0])
    y_overlap = min(a[3], b[3]) - max(a[1], b[1])
    tolerance = 1e-11
    return x_overlap >= -tolerance and y_overlap >= -tolerance and (
        abs(x_overlap) <= tolerance or abs(y_overlap) <= tolerance
    )


def _polygons(geometry):
    if not isinstance(geometry, dict):
        return []
    kind, coordinates = geometry.get("type"), geometry.get("coordinates") or []
    groups = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
    result = []
    for rings in groups:
        clean = []
        for ring in rings or []:
            points = [[float(point[0]), float(point[1])] for point in ring or [] if len(point) >= 2]
            if len(points) >= 3:
                if points[0] != points[-1]:
                    points.append(points[0])
                clean.append(points)
        if clean:
            result.append((clean[0], clean[1:]))
    return result


def _point_on_segment(point, left, right, tolerance=1e-10):
    cross = (point[0] - left[0]) * (right[1] - left[1]) - (point[1] - left[1]) * (right[0] - left[0])
    if abs(cross) > tolerance:
        return False
    return (
        min(left[0], right[0]) - tolerance <= point[0] <= max(left[0], right[0]) + tolerance
        and min(left[1], right[1]) - tolerance <= point[1] <= max(left[1], right[1]) + tolerance
    )


def _point_in_ring(point, ring):
    inside = False
    for left, right in zip(ring, ring[1:]):
        if _point_on_segment(point, left, right):
            return True, True
        if (left[1] > point[1]) != (right[1] > point[1]):
            crossing_x = (right[0] - left[0]) * (point[1] - left[1]) / (right[1] - left[1]) + left[0]
            if crossing_x > point[0]:
                inside = not inside
    return inside, False


def _point_covered(point, polygon):
    outer, holes = polygon
    inside, boundary = _point_in_ring(point, outer)
    if not inside:
        return False
    if boundary:
        return True
    for hole in holes:
        in_hole, on_hole = _point_in_ring(point, hole)
        if in_hole and not on_hole:
            return False
    return True


#: Public aliases.  They expose exactly the confirmed-policy geometry helpers that
#: V3-B's fine environment adapter reuses; the V1/V2 behaviour above is unchanged.
def polygons_of(geometry):
    return _polygons(geometry)


def point_covered_by_polygon(point, polygon):
    return _point_covered(point, polygon)


def rect_covered_by_any(polygons, rect):
    """Full coverage of a rectangle by at least one polygon (no boundary crossing)."""

    return _rect_covered_by_any(polygons, rect)


def rect_intersects_any(polygons, rect):
    """True when the rectangle shares area with at least one polygon."""

    return _rect_intersects_any(polygons, rect)


def polygon_intersects_rect(polygon, rect):
    """True when a single polygon shares area with the rectangle."""

    return _polygon_intersects_rect(polygon, rect)


def _strictly_in_rect(point, rect, tolerance=1e-10):
    west, south, east, north = (float(value) for value in rect)
    return west + tolerance < point[0] < east - tolerance and south + tolerance < point[1] < north - tolerance


def _segment_has_rect_interior(left, right, rect):
    """Liang-Barsky clip; true only when a positive segment lies in rect interior."""
    west, south, east, north = (float(value) for value in rect)
    dx, dy = right[0] - left[0], right[1] - left[1]
    p = (-dx, dx, -dy, dy)
    q = (left[0] - west, east - left[0], left[1] - south, north - left[1])
    low, high = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) <= 1e-15:
            if qi < 0:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    if high - low <= 1e-12:
        return False
    midpoint = [left[0] + dx * ((low + high) / 2), left[1] + dy * ((low + high) / 2)]
    return _strictly_in_rect(midpoint, rect)


def _polygon_covers_rect(polygon, rect):
    west, south, east, north = (float(value) for value in rect)
    corners = ([west, south], [east, south], [east, north], [west, north])
    center = [(west + east) / 2.0, (south + north) / 2.0]
    if not all(_point_covered(point, polygon) for point in [*corners, center]):
        return False
    outer, holes = polygon
    # A polygon/hole boundary entering the cell means full coverage is unproven.
    for ring in [outer, *holes]:
        if any(_strictly_in_rect(point, rect) for point in ring[:-1]):
            return False
        if any(_segment_has_rect_interior(left, right, rect) for left, right in zip(ring, ring[1:])):
            return False
    return True


def _rect_covered_by_any(polygons, rect):
    """True when the rectangle is fully covered by at least one polygon."""

    rectangle = _as_ring(rect)
    if len(rectangle) < 4:
        return False
    return any(
        _polygon_covers_ring(polygon, rectangle) for polygon in polygons or []
    )


def _rect_intersects_any(polygons, rect):
    """True when the rectangle shares area with at least one polygon."""

    rectangle = _as_ring(rect)
    if len(rectangle) < 4:
        return False
    return any(
        _polygon_intersects_ring(polygon, rectangle) for polygon in polygons or []
    )


def _ring_covered_by_any(polygons, outer):
    """Full coverage of a polygon ring by at least one ``(outer, holes)`` polygon."""

    ring = _as_ring(outer)
    return any(_polygon_covers_ring(polygon, ring) for polygon in polygons or [])


def _ring_intersects_any(polygons, outer):
    """Area intersection between a polygon ring and any ``(outer, holes)`` polygon."""

    ring = _as_ring(outer)
    return any(_polygon_intersects_ring(polygon, ring) for polygon in polygons or [])


def _as_ring(value, *, tolerance=1e-12):
    """Normalize ``[w, s, e, n]`` or a point ring into a closed point ring.

    Reported cell/geometry shapes travel in both forms (a bbox from the grid, a
    densified ring from the adapter), so the coverage helpers accept either and never
    guess: a 4-number list is a bbox, anything else must already be a point ring.
    """

    values = list(value or [])
    if len(values) == 2 and isinstance(values[0], list) and values[0] and isinstance(values[0][0], list):
        # Already an ``(outer, holes)`` polygon pair: the outer ring is what matters.
        values = list(values[0])
    if len(values) == 4 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in values
    ):
        west, south, east, north = (float(item) for item in values)
        return [
            [west, south], [east, south], [east, north], [west, north], [west, south],
        ]
    ring = [[float(point[0]), float(point[1])] for point in values if len(point) >= 2]
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _ring_bbox(ring):
    xs = [float(point[0]) for point in ring]
    ys = [float(point[1]) for point in ring]
    return [min(xs), min(ys), max(xs), max(ys)]


def _ring_covers_rect(ring, rectangle):
    """A ring/polygon fully covering another ring, with no boundary crossing."""

    if not ring or len(ring) < 4 or not rectangle or len(rectangle) < 4:
        return False
    return _polygon_covers_ring(ring, _as_ring(rectangle))


def _edge_enters_ring(left, right, ring):
    """True when the *interior* of an edge passes through the ring's interior."""

    for ratio in (0.25, 0.5, 0.75):
        probe = [left[0] + (right[0] - left[0]) * ratio, left[1] + (right[1] - left[1]) * ratio]
        if _strictly_inside_ring(probe, ring):
            return True
    return False


def _ring_intersects_rect(ring, rectangle):
    """Positive-area intersection between two polygon rings."""

    return _ring_intersects_ring(ring, _as_ring(rectangle))


def _ring_intersects_ring(left, right):
    if not left or len(left) < 4 or not right or len(right) < 4:
        return False
    if _ring_covers_rect(left, right) or _ring_covers_rect(right, left):
        return True
    outer_left, holes_left = _as_polygon(left)
    outer_right, holes_right = _as_polygon(right)
    for candidate in [outer_left, *holes_left]:
        if any(_strictly_inside_ring(point, outer_right) for point in candidate[:-1]):
            return True
    for candidate in [outer_right, *holes_right]:
        if any(_strictly_inside_ring(point, outer_left) for point in candidate[:-1]):
            return True
    for a, b in zip(outer_left, outer_left[1:]):
        for c, d in zip(outer_right, outer_right[1:]):
            if _segments_cross_interior(a, b, c, d):
                return True
    return False


def _ring_corners(ring):
    xs = [point[0] for point in ring[:-1]]
    ys = [point[1] for point in ring[:-1]]
    west, south, east, north = min(xs), min(ys), max(xs), max(ys)
    return [[west, south], [east, south], [east, north], [west, north]]


def _strictly_inside_ring(point, ring, tolerance=1e-12):
    inside, boundary = _point_in_ring(point, ring)
    return bool(inside and not boundary)


def _inside_ring_no_boundary(point, ring):
    """Interior without treating a coincident boundary as "entering"."""

    inside, _boundary = _point_in_ring(point, ring)
    return bool(inside)


def _segment_enters_ring(left, right, ring, tolerance=1e-12):
    """True when a segment except its endpoints enters the ring's interior."""

    steps = 8
    for index in range(1, steps):
        ratio = index / float(steps)
        probe = [left[0] + (right[0] - left[0]) * ratio, left[1] + (right[1] - left[1]) * ratio]
        if _strictly_inside_ring(probe, ring):
            return True
    return False


def _polygon_covers_ring(polygon, ring):
    """Full containment of one polygon ring inside a polygon (boundary crossing fails)."""

    if not ring or len(ring) < 3:
        return False
    outer, holes = _as_polygon(polygon)
    if not outer:
        return False
    if not all(_point_covered(point, (outer, holes)) for point in ring[:-1]):
        return False
    for candidate in [outer, *holes]:
        for point in candidate[:-1]:
            if _point_inside_ring(point, ring):
                return False
        for left, right in zip(candidate, candidate[1:]):
            if any(_segments_cross_interior(left, right, a, b) for a, b in zip(ring, ring[1:])):
                return False
    return True


def _polygon_intersects_ring(polygon, ring):
    """Positive-area intersection between a polygon and a ring."""

    if not ring or len(ring) < 3:
        return False
    outer, holes = _as_polygon(polygon)
    if not outer:
        return False
    if _polygon_covers_ring((outer, holes), ring):
        return True
    if any(_point_covered(point, (outer, holes)) for point in ring[:-1]):
        return True
    if any(_point_inside_ring(point, ring) for point in outer[:-1]):
        return True
    for left, right in zip(outer, outer[1:]):
        if any(_segments_cross_interior(left, right, a, b) for a, b in zip(ring, ring[1:])):
            return True
    return False


def _as_polygon(value):
    """Normalize a bare ring or an ``(outer, holes)`` pair into ``(outer, holes)``."""

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], list):
        outer = _as_ring(value[0])
        holes = [_as_ring(hole) for hole in value[1] or []]
        return outer, [hole for hole in holes if len(hole) >= 4]
    return _as_ring(value), []


def _point_inside_ring(point, ring):
    return _point_in_ring(point, ring)[0]


def _segments_cross_interior(a, b, c, d, tolerance=1e-12):
    """True when two segments cross at a point interior to both."""

    parameters = _intersection_parameters(a, b, c, d)
    return bool(parameters) and tolerance < parameters[0] < 1.0 - tolerance and \
        tolerance < parameters[1] < 1.0 - tolerance


def _intersection_parameters(a, b, c, d):
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denominator = rx * sy - ry * sx
    if abs(denominator) <= 1e-15:
        return []
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = (qx * sy - qy * sx) / denominator
    u = (qx * ry - qy * rx) / denominator
    if -1e-12 <= t <= 1 + 1e-12 and -1e-12 <= u <= 1 + 1e-12:
        return [t, u]
    return []


def _polygon_intersects_rect(polygon, rect, tolerance=1e-12):
    """Public: does the ``(outer, holes)`` polygon share area with the rectangle?"""

    return _polygon_intersects_ring(polygon, _as_ring(rect))


def _polygon_contains_rect_interior(polygon, rect, tolerance=1e-12):
    west, south, east, north = (float(value) for value in rect)
    half_width = max((east - west) / 2.0, tolerance)
    half_height = max((north - south) / 2.0, tolerance)
    probes = (
        [(west + east) / 2.0, (south + north) / 2.0],
        [west + half_width * 0.9, south + half_height * 0.9],
        [east - half_width * 0.9, south + half_height * 0.9],
        [east - half_width * 0.9, north - half_height * 0.9],
        [west + half_width * 0.9, north - half_height * 0.9],
    )
    return all(_point_covered(point, polygon) for point in probes)


def _intersection_parameter(a, b, c, d):
    rx, ry, sx, sy = b[0] - a[0], b[1] - a[1], d[0] - c[0], d[1] - c[1]
    denominator = rx * sy - ry * sx
    qx, qy = c[0] - a[0], c[1] - a[1]
    if abs(denominator) <= 1e-15:
        return []
    t = (qx * sy - qy * sx) / denominator
    u = (qx * ry - qy * rx) / denominator
    return [max(0.0, min(1.0, t))] if -1e-12 <= t <= 1 + 1e-12 and -1e-12 <= u <= 1 + 1e-12 else []


def _segment_covered_by_union(left, right, polygons):
    breaks = [0.0, 1.0]
    dx, dy = right[0] - left[0], right[1] - left[1]
    for outer, holes in polygons:
        for ring in [outer, *holes]:
            for a, b in zip(ring, ring[1:]):
                breaks.extend(_intersection_parameter(left, right, a, b))
                for point in (a, b):
                    if _point_on_segment(point, left, right):
                        denominator = dx * dx + dy * dy
                        if denominator:
                            breaks.append(((point[0] - left[0]) * dx + (point[1] - left[1]) * dy) / denominator)
    breaks = sorted(set(round(value, 12) for value in breaks))
    samples = [breaks[0], breaks[-1], *[(a + b) / 2 for a, b in zip(breaks, breaks[1:])]]
    return all(any(_point_covered([left[0] + dx * t, left[1] + dy * t], polygon) for polygon in polygons) for t in samples)
