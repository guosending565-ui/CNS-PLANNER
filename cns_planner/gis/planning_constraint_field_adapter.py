"""Pure spatial mapping helpers for Planning Constraint Field generation."""

from __future__ import annotations

from copy import deepcopy

from ..domain.restricted_area import (
    normalize_restricted_area, normalize_restricted_area_collection,
)


def restricted_areas_by_cell(grid_cells, restricted_areas, *, grid_crs="OGC:CRS84"):
    result = {str(cell.get("grid_id")): [] for cell in grid_cells or [] if cell.get("grid_id")}
    for raw in restricted_areas or []:
        try:
            area = normalize_restricted_area(raw)
        except ValueError as exc:
            area = {
                "feature_id": str((raw or {}).get("feature_id") or "unidentified"),
                "geometry_status": "unknown", "planning_geometry": None,
                "geometry": (raw or {}).get("geometry"), "geometry_crs": (raw or {}).get("geometry_crs"),
                "normalization_error": str(exc), "confirmed": False,
                "constraint_type": str((raw or {}).get("constraint_type") or "conditional"),
                "domain": str((raw or {}).get("domain") or "critical_site"),
            }
        for cell in grid_cells or []:
            grid_id, bbox = str(cell.get("grid_id") or ""), cell.get("bbox")
            if not grid_id or not _bbox(bbox):
                continue
            geometry = area.get("planning_geometry")
            source_point_only = area.get("geometry_status") == "source_point_only_no_protection_geometry"
            candidate = area.get("geometry") if source_point_only else geometry
            if str(area.get("geometry_crs") or "") != str(grid_crs or ""):
                # Preserve possible applicability without pretending two CRS are equal.
                if _geometry_bbox(candidate) is not None and _bbox_intersects(bbox, _geometry_bbox(candidate)):
                    result[grid_id].append({**area, "spatial_status": "unknown_crs_mismatch"})
                continue
            if geometry is not None and geometry_intersects_bbox(geometry, bbox):
                result[grid_id].append({**area, "spatial_status": "intersects"})
            elif source_point_only and geometry_intersects_bbox(candidate, bbox):
                result[grid_id].append({**area, "spatial_status": "source_point_only"})
    return result


def towers_by_cell(grid_cells, tower_profiles):
    profiles = (
        tower_profiles.get("items") if isinstance(tower_profiles, dict) else tower_profiles
    ) or {}
    values = profiles.values() if isinstance(profiles, dict) else profiles
    result = {str(cell.get("grid_id")): [] for cell in grid_cells or [] if cell.get("grid_id")}
    for profile in values:
        if not isinstance(profile, dict):
            continue
        point = [profile.get("longitude"), profile.get("latitude")]
        if not _point(point):
            continue
        for cell in grid_cells or []:
            grid_id, bbox = str(cell.get("grid_id") or ""), cell.get("bbox")
            if grid_id and _bbox(bbox) and _point_in_bbox(point, bbox):
                result[grid_id].append(profile)
    return result


def restricted_area_continuous_evidence(value, *, transform):
    """Project audited protection geometry for an independent route-level validation."""

    collection = normalize_restricted_area_collection(value)
    states = collection.get("domain_states") or {}
    resolved = all(
        (states.get(domain) or {}).get("status") in ("confirmed_none", "confirmed_present")
        for domain in ("airspace", "critical_site")
    )
    items = []
    for area in collection.get("items") or []:
        projected = deepcopy(area)
        geometry = area.get("planning_geometry")
        if geometry is not None:
            projected["planning_geometry_metric"] = _project_geometry(geometry, transform)
        elif area.get("geometry_status") == "source_point_only_no_protection_geometry":
            point = (area.get("geometry") or {}).get("coordinates")
            projected["point_metric"] = transform.to_metric(point) if point else None
        items.append(projected)
    return {
        "status": "passed" if resolved else "unresolved",
        "applicability": "applicable", "areas": items,
        "source": deepcopy(collection.get("source")),
        "domain_states": deepcopy(states),
    }


def _project_geometry(geometry, transform):
    kind = str((geometry or {}).get("type") or "")
    coordinates = (geometry or {}).get("coordinates")

    def ring(value):
        result = []
        for point in value or []:
            projected = transform.to_metric(point)
            if projected is None:
                return None
            result.append([float(projected[0]), float(projected[1])])
        return result

    if kind == "Polygon":
        rings = [ring(item) for item in coordinates or []]
        return None if any(item is None for item in rings) else {"type": kind, "coordinates": rings}
    if kind == "MultiPolygon":
        polygons = [[ring(item) for item in polygon or []] for polygon in coordinates or []]
        if any(item is None for polygon in polygons for item in polygon):
            return None
        return {"type": kind, "coordinates": polygons}
    return None


def geometry_intersects_bbox(geometry, bbox):
    if not isinstance(geometry, dict) or not _bbox(bbox):
        return False
    kind, coordinates = geometry.get("type"), geometry.get("coordinates")
    if kind == "Point":
        return _point(coordinates) and _point_in_bbox(coordinates, bbox)
    if kind == "Polygon":
        return _polygon_intersects_bbox(coordinates, bbox)
    if kind == "MultiPolygon":
        return any(_polygon_intersects_bbox(polygon, bbox) for polygon in coordinates or [])
    return False


def _polygon_intersects_bbox(polygon, bbox):
    outer = polygon[0] if isinstance(polygon, (list, tuple)) and polygon else []
    if not outer or not _bbox_intersects(_ring_bbox(outer), bbox):
        return False
    corners = [
        [bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]],
    ]
    if any(_point_in_ring(corner, outer) for corner in corners):
        return True
    if any(_point_in_bbox(point, bbox) for point in outer if _point(point)):
        return True
    edges = list(zip(outer, outer[1:] + outer[:1]))
    box_edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(_segments_intersect(a, b, c, d) for a, b in edges for c, d in box_edges)


def _geometry_bbox(geometry):
    if not isinstance(geometry, dict):
        return None
    points = []

    def collect(value):
        if _point(value):
            points.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(geometry.get("coordinates"))
    return _ring_bbox(points) if points else None


def _ring_bbox(ring):
    points = [point for point in ring or [] if _point(point)]
    if not points:
        return None
    return [
        min(float(point[0]) for point in points), min(float(point[1]) for point in points),
        max(float(point[0]) for point in points), max(float(point[1]) for point in points),
    ]


def _point_in_ring(point, ring):
    x, y = float(point[0]), float(point[1])
    inside = False
    points = [item for item in ring or [] if _point(item)]
    if len(points) < 3:
        return False
    previous = points[-1]
    for current in points:
        x1, y1, x2, y2 = float(previous[0]), float(previous[1]), float(current[0]), float(current[1])
        if ((y1 > y) != (y2 > y)) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _segments_intersect(a, b, c, d):
    def orientation(p, q, r):
        return (float(q[1]) - float(p[1])) * (float(r[0]) - float(q[0])) - (
            float(q[0]) - float(p[0])
        ) * (float(r[1]) - float(q[1]))

    return orientation(a, b, c) * orientation(a, b, d) <= 0 and orientation(c, d, a) * orientation(c, d, b) <= 0


def _point_in_bbox(point, bbox):
    return float(bbox[0]) <= float(point[0]) <= float(bbox[2]) and float(bbox[1]) <= float(point[1]) <= float(bbox[3])


def _point(value):
    try:
        return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
            float(item) == float(item) for item in value[:2]
        )
    except (TypeError, ValueError):
        return False


def _bbox(value):
    return isinstance(value, (list, tuple)) and len(value) == 4


def _bbox_intersects(left, right):
    return bool(left and right) and min(float(left[2]), float(right[2])) >= max(float(left[0]), float(right[0])) and min(float(left[3]), float(right[3])) >= max(float(left[1]), float(right[1]))


__all__ = [
    "geometry_intersects_bbox", "restricted_area_continuous_evidence",
    "restricted_areas_by_cell", "towers_by_cell",
]
