"""Surface-aware RID planning footprint geometry for map figures only.

This module deliberately has no renderer or canonical-state write path.  It turns one
RID site plus the authoritative :class:`LandMaskSource` into JSON-safe WGS84 polygons:

* an omnidirectional 2 km base; and
* only the part of the 2--5 km annulus that is confirmed sea.

``confirmed sea`` is the area outside the authoritative land polygons *and* their
configured coastal-uncertainty buffer.  Missing/unusable evidence is fail-closed and
therefore produces no extension beyond 2 km.
"""

from __future__ import annotations

from copy import deepcopy
import math

from .cns_facility_assembler import coverage_circle_ring


def _facts_are_usable(facts):
    facts = facts if isinstance(facts, dict) else {}
    if str(facts.get("status") or "") != "passed":
        return False, "surface_class_facts_not_passed"
    source = facts.get("source") if isinstance(facts.get("source"), dict) else {}
    land_mask = facts.get("land_mask") if isinstance(facts.get("land_mask"), dict) else {}
    configured = source.get("land_mask_configured") is True or bool(
        land_mask.get("configured_path") or land_mask.get("source_identity")
    )
    if not configured:
        return False, "surface_class_facts_land_mask_not_configured"
    counts = facts.get("surface_class_counts")
    counts = counts if isinstance(counts, dict) else {}
    classified = facts.get("classified_grid_cell_count")
    if classified is None:
        classified = sum(
            int(counts.get(name) or 0) for name in ("land", "coastal_uncertain", "sea")
        )
    if int(classified or 0) <= 0:
        return False, "surface_class_facts_have_no_confirmed_surface"
    return True, None


def _polygon_parts(geometry):
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind == "Polygon":
        return [geometry]
    if kind in ("MultiPolygon", "GeometryCollection"):
        result = []
        for part in geometry.geoms:
            result.extend(_polygon_parts(part))
        return result
    return []


def _to_wgs84_polygons(geometry, land_mask_source):
    polygons = []
    for part in sorted(_polygon_parts(geometry), key=lambda item: -item.area):
        exterior = []
        for x, y in part.exterior.coords:
            point = land_mask_source.to_geographic(x, y)
            if point is None:
                return []
            exterior.append([round(float(point[0]), 9), round(float(point[1]), 9)])
        holes = []
        for interior in part.interiors:
            ring = []
            for x, y in interior.coords:
                point = land_mask_source.to_geographic(x, y)
                if point is None:
                    return []
                ring.append([round(float(point[0]), 9), round(float(point[1]), 9)])
            if len(ring) >= 4:
                holes.append(ring)
        if len(exterior) >= 4:
            polygons.append({"exterior": exterior, "holes": holes})
    return polygons


def _line_parts(geometry):
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    kind = getattr(geometry, "geom_type", "")
    if kind in ("LineString", "LinearRing"):
        return [geometry]
    if kind in ("MultiLineString", "GeometryCollection"):
        result = []
        for part in geometry.geoms:
            result.extend(_line_parts(part))
        return result
    return []


def _to_wgs84_lines(geometry, land_mask_source):
    lines = []
    for part in _line_parts(geometry):
        line = []
        for x, y in part.coords:
            point = land_mask_source.to_geographic(x, y)
            if point is None:
                return []
            line.append([round(float(point[0]), 9), round(float(point[1]), 9)])
        if len(line) >= 2:
            lines.append(line)
    return lines


def build_surface_aware_rid_footprint(
        longitude, latitude, *, land_radius_m, sea_radius_m, land_mask_source,
        surface_class_facts, metric_crs="EPSG:32651", segments=72):
    """Build one map-only RID footprint without changing registered radii.

    The return value contains a 2 km ``base_polygons`` collection and a separately
    styled ``sea_extension_polygons`` collection.  No extension is returned unless
    both serialized surface facts and the continuous authoritative LandMask geometry
    are usable.  The function does not mutate project state or any business result.
    """

    base_radius = float(land_radius_m)
    outer_radius = float(sea_radius_m)
    if not all(math.isfinite(value) and value > 0 for value in (base_radius, outer_radius)):
        raise ValueError("RID footprint radii must be positive finite numbers")
    if outer_radius < base_radius:
        raise ValueError("RID sea radius must not be smaller than land radius")

    base_ring = coverage_circle_ring(
        longitude, latitude, base_radius, metric_crs=metric_crs, segments=segments,
    )
    result = {
        "base_polygons": ([{"exterior": base_ring, "holes": []}] if base_ring else []),
        "sea_extension_polygons": [],
        "sea_extension_boundary_lines": [],
        "metadata": {
            "surface_aware_visualization": True,
            "affects_planning": False,
            "land_radius_m": base_radius,
            "sea_radius_m": outer_radius,
            "surface_class_facts_status": (
                surface_class_facts.get("status")
                if isinstance(surface_class_facts, dict) else None
            ),
            "surface_class_facts_input_fingerprint": (
                surface_class_facts.get("input_fingerprint")
                if isinstance(surface_class_facts, dict) else None
            ),
            "fail_closed": True,
            "extension_status": "omitted",
            "extension_reason": None,
        },
    }
    facts_usable, reason = _facts_are_usable(surface_class_facts)
    if not facts_usable:
        result["metadata"]["extension_reason"] = reason
        return result
    if land_mask_source is None:
        result["metadata"]["extension_reason"] = "land_mask_source_missing"
        return result

    try:
        prepared = land_mask_source.metric_polygons()
        center = land_mask_source.to_metric(longitude, latitude)
    except Exception as exc:  # defensive: GIS evidence failure must stay fail-closed
        result["metadata"]["extension_reason"] = f"land_mask_unusable:{type(exc).__name__}"
        return result
    if not prepared or center is None:
        result["metadata"]["extension_reason"] = "land_mask_geometry_or_transform_unavailable"
        return result

    try:
        from shapely.geometry import Point
        from shapely.ops import unary_union

        quad_segs = max(8, int(segments) // 4)
        inner = Point(*center).buffer(base_radius, quad_segs=quad_segs)
        outer = Point(*center).buffer(outer_radius, quad_segs=quad_segs)
        annulus = outer.difference(inner)
        land = unary_union([polygon for polygon, _bounds in prepared])
        coastal_buffer_m = float(
            getattr(land_mask_source, "coastal_uncertainty_buffer_m", 0.0) or 0.0
        )
        excluded = land.buffer(max(0.0, coastal_buffer_m))
        confirmed_sea = annulus.difference(excluded)
        polygons = _to_wgs84_polygons(confirmed_sea, land_mask_source)
        # The visible 5 km boundary is clipped independently, so coastlines and the
        # inner 2 km boundary are not mistaken for additional RID range contours.
        boundary_lines = _to_wgs84_lines(outer.boundary.difference(excluded), land_mask_source)
    except Exception as exc:  # defensive: never turn geometry failure into a 5 km circle
        result["metadata"]["extension_reason"] = f"surface_clip_failed:{type(exc).__name__}"
        return result

    if not polygons:
        result["metadata"]["extension_reason"] = "no_confirmed_sea_within_2_to_5km"
        return result
    result["sea_extension_polygons"] = polygons
    result["sea_extension_boundary_lines"] = boundary_lines
    result["metadata"].update({
        "extension_status": "available",
        "extension_reason": None,
        "coastal_uncertainty_buffer_m": coastal_buffer_m,
        "geometry_method": (
            "metric_annulus_2_to_5km_intersection_confirmed_sea_from_authoritative_land_mask"
        ),
        "confirmed_sea_semantics": "outside_land_and_coastal_uncertainty_buffer",
        "land_mask": deepcopy(land_mask_source.describe()),
    })
    return result


__all__ = ["build_surface_aware_rid_footprint"]
