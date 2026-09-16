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
