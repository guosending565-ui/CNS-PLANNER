"""Read-only GeoJSON geometry health; never repairs or rewrites geometry."""

from __future__ import annotations


def inspect_geojson_geometries(features, allowed_types):
    counts = {"null": 0, "empty": 0, "invalid": 0, "unsupported": 0}
    bounds = []
    allowed = set(allowed_types)
    items = list(features or [])
    for feature in items:
        geometry = (feature or {}).get("geometry") if isinstance(feature, dict) else None
        if geometry is None:
            counts["null"] += 1
            continue
        kind = geometry.get("type") if isinstance(geometry, dict) else None
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not coordinates:
            counts["empty"] += 1
            continue
        if kind not in allowed:
            counts["unsupported"] += 1
            continue
        points = list(_points(coordinates))
        if not points or not _structurally_valid(kind, coordinates):
            counts["invalid"] += 1
            continue
        try:
            from shapely.geometry import shape

            if not shape(geometry).is_valid:
                counts["invalid"] += 1
                continue
        except ImportError:
            pass
        except Exception:
            counts["invalid"] += 1
            continue
        bounds.extend(points)
    bad = sum(counts.values())
    return {
        "status": "passed" if items and bad == 0 else "blocked" if bad else "not_calculated",
        "feature_count": len(items), **counts,
        "extent": ([min(p[0] for p in bounds), min(p[1] for p in bounds),
                    max(p[0] for p in bounds), max(p[1] for p in bounds)] if bounds else None),
        "repair_applied": False,
        "reason": "invalid_geometry_fail_closed" if bad else None,
    }


def _points(value):
    if (isinstance(value, (list, tuple)) and len(value) >= 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in value[:2])):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _points(item)


def _structurally_valid(kind, coordinates):
    if kind == "Point":
        return len(coordinates) >= 2
    if kind == "LineString":
        return len(coordinates) >= 2
    polygons = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
    for polygon in polygons:
        if not polygon:
            return False
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                return False
    return bool(polygons)
