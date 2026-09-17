"""Corridor-local metric fine grid construction and coarse->fine mapping (pure).

This module contains only deterministic, side-effect-free geometry.  It never
opens a file, never imports QGIS/GDAL and never invents a safety value:

* the fine grid is built **only** inside the metric bounding box of the corridor's
  support cells (``只在 support corridor 构造局部米制 fine grid``);
* the horizontal resolution is *given* (explicit configuration or the DTM's own
  effective resolution).  A literal ``"30 m"`` is never assumed -- it would
  silently claim a precision the data does not have;
* a geographic (lon/lat) position for a fine cell is either supplied by the
  adapter (a real projected-CRS inverse transform) or produced by an explicitly
  labelled *display-only* plane fit over the parent cell centers.  The method
  always travels with the data;
* mapping coarse values onto finer cells always sets
  ``upsampled_without_new_information=true``: the fine cell inherits the coarse
  value, it does not gain source accuracy;
* terrain and building facts are computed as **conservative envelopes**: terrain
  from the maximum of intersecting valid pixels, buildings from the buffered
  footprint intersecting the cell.  Neither is an exact polygon validation --
  that is V3-C.
"""

from __future__ import annotations

from math import ceil, hypot, isfinite
import math

from .cost import BUILDING_EXPOSURE_INDEX_METHOD  # noqa: F401  (documented pairing)
from .fine_contracts import (
    AIRSPACE_MAPPING_METHOD, BUILDING_MAPPING_METHOD, FINE_GRID_SPEC_SCHEMA_VERSION,
    SOFT_FIELD_MAPPING_METHOD, TERRAIN_SAMPLING_METHOD, contract_fingerprint,
)

#: Tolerance used only for "<=" comparisons of exact geometry.
TOLERANCE = 1e-9

#: The soft channels a fine cell can inherit from its parent cell.
UPSAMPLED_CHANNELS = ("population_risk", "traffic_risk")

SUPPORT_CORRIDOR_MAPPING = "corridor_support_parent_cells_to_local_metric_index_grid"
DISPLAY_GEOGRAPHIC_METHOD = "least_squares_plane_fit_from_parent_cell_centers_for_display_only"


def cell_id(level, row, column):
    return f"F{int(level or 0)}-{int(row):04d}-{int(column):04d}"


# --------------------------------------------------------------------------- frame


def build_local_frame(
    *, metric_bounds, horizontal_crs, horizontal_crs_source, resolution_m,
    vertical_reference="egm2008_orthometric", local_to_geographic=None, provenance=None,
):
    """A local metric frame from explicit values only."""

    west, south, east, north = (float(value) for value in metric_bounds)
    if east <= west or north <= south:
        raise ValueError("metric_bounds 必须满足 west<east 且 south<north")
    frame = {
        "schema_version": "3.1-local-metric-frame",
        "frame_id": "V3BFRAME-LOCAL",
        "horizontal_crs": None if horizontal_crs in (None, "") else str(horizontal_crs),
        "horizontal_crs_source": None if horizontal_crs_source in (None, "") else str(horizontal_crs_source),
        "vertical_reference": vertical_reference,
        "origin_metric": [round(west, 9), round(south, 9)],
        "axis": {"column_axis": "east", "row_axis": "north", "index_origin": "south_west_corner"},
        "resolution_m": float(resolution_m),
        "metric_bounds": [round(west, 9), round(south, 9), round(east, 9), round(north, 9)],
        "local_to_geographic": dict(local_to_geographic or {
            "method": None, "authority": None, "display_only": True,
            "interpolated_from_parent_cells": False,
            "note": "未提供局部 index → 经纬度 映射；仅使用米制坐标",
        }),
        "provenance": dict(provenance or {}),
    }
    frame["frame_id"] = contract_fingerprint(frame, prefix="V3BFRAME-LOCAL-")[:20]
    return frame


# --------------------------------------------------------------------------- grid


def build_fine_grid_spec(
    *, frame, resolution_m, resolution_source, parent_cells_metric,
    requested_resolution_m=None, effective_source_resolution_m=None,
    parent_grid_resolution_m=None, corridor_id=None, corridor_ring_n=0, provenance=None,
):
    """Index grid covering the corridor support cells' metric bounding box.

    ``parent_cells_metric`` are the corridor **support** parent cells already
    expressed in the local metric CRS:
    ``[{"grid_id", "center_metric": [x, y], "bbox_metric": [w, s, e, n]}]``.
    """

    if resolution_source not in ("explicit_configuration", "dtm_effective_resolution"):
        raise ValueError(
            "resolution_source 必须是 explicit_configuration 或 dtm_effective_resolution"
        )
    resolution = float(resolution_m)
    if not isfinite(resolution) or resolution <= 0:
        raise ValueError("resolution_m 必须是有限正数")
    parents = list(parent_cells_metric or [])
    if not parents:
        raise ValueError("corridor support cells 为空：不得构造 fine grid")
    bboxes = []
    for parent in parents:
        bbox = parent.get("bbox_metric")
        if not bbox or len(bbox) != 4:
            center = parent.get("center_metric") or []
            if len(center) != 2:
                raise ValueError(f"parent cell {parent.get('grid_id')} 缺少 metric 几何")
            half = resolution / 2.0
            bbox = [center[0] - half, center[1] - half, center[0] + half, center[1] + half]
        bboxes.append([float(value) for value in bbox])
    west = min(bbox[0] for bbox in bboxes)
    south = min(bbox[1] for bbox in bboxes)
    east = max(bbox[2] for bbox in bboxes)
    north = max(bbox[3] for bbox in bboxes)
    nx = max(1, int(ceil((east - west) / resolution - TOLERANCE)))
    ny = max(1, int(ceil((north - south) / resolution - TOLERANCE)))
    spec = {
        "schema_version": FINE_GRID_SPEC_SCHEMA_VERSION,
        "spec_id": f"V3BGRID-{nx}x{ny}@{resolution:g}m",
        "resolution_m": resolution,
        "requested_resolution_m": requested_resolution_m,
        "resolution_source": resolution_source,
        "effective_source_resolution_m": effective_source_resolution_m,
        "nx": nx,
        "ny": ny,
        "cell_count": nx * ny,
        "local_frame": frame,
        "parent_grid_ids": sorted(str(parent["grid_id"]) for parent in parents),
        "corridor_id": corridor_id,
        "corridor_ring_n": int(corridor_ring_n or 0),
        "mapping_method": SUPPORT_CORRIDOR_MAPPING,
        "grid_index": "row_major_from_south_west_origin",
        "cell_id_format": "F{level}-{row:04d}-{column:04d}",
        "parent_grid_resolution_m": parent_grid_resolution_m,
        "resolution_deviation_m": (
            None if requested_resolution_m is None
            else round(float(requested_resolution_m) - resolution, 9)
        ),
        "not_a_safety_clearance": True,
        "semantics": "corridor_local_fine_search_grid_not_a_safety_volume",
        "provenance": dict(provenance or {}),
    }
    return spec


def fine_cells_from_spec(spec):
    """Deterministic fine cell geometry, row-major from the south-west corner."""

    grid = spec.get("fine_grid") if isinstance(spec.get("fine_grid"), dict) else spec
    frame = grid.get("local_frame") or {}
    resolution = grid.get("resolution_m")
    nx, ny = int(grid.get("nx") or 0), int(grid.get("ny") or 0)
    if resolution is None or nx <= 0 or ny <= 0:
        return []
    bounds = frame.get("metric_bounds")
    if not bounds:
        origin = frame.get("origin_metric") or [0.0, 0.0]
        west, south = float(origin[0]), float(origin[1])
    else:
        west, south = float(bounds[0]), float(bounds[1])
    level = int(grid.get("corridor_ring_n") or 0) * 0 + int(frame.get("fine_level") or 0)
    cells = []
    for row in range(ny):
        for column in range(nx):
            x0 = west + column * resolution
            y0 = south + row * resolution
            cells.append({
                "fine_cell_id": cell_id(level, row, column),
                "row": row,
                "column": column,
                "center_metric": [round(x0 + resolution / 2.0, 9), round(y0 + resolution / 2.0, 9)],
                "bbox_metric": [
                    round(x0, 9), round(y0, 9), round(x0 + resolution, 9), round(y0 + resolution, 9),
                ],
                "cell_size_m": float(resolution),
            })
    return cells


def fine_index(cells):
    return {str(cell["fine_cell_id"]): (int(cell["row"]), int(cell["column"])) for cell in cells}


def fine_adjacency(cells):
    """Chebyshev adjacency over the fine grid index (deterministic, no geometry guess)."""

    index = fine_index(cells)
    reverse = {value: key for key, value in index.items()}
    deltas = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
    adjacency = {}
    for grid_id, (row, column) in index.items():
        adjacency[grid_id] = tuple(sorted(
            reverse[(row + drow, column + dcolumn)]
            for dcolumn, drow in deltas
            if (row + drow, column + dcolumn) in reverse
        ))
    return adjacency


def fine_metric_bounds(spec):
    frame = (spec.get("fine_grid") if isinstance(spec.get("fine_grid"), dict) else spec).get("local_frame") or {}
    bounds = frame.get("metric_bounds")
    return None if not bounds else [float(value) for value in bounds]


# --------------------------------------------------------------------------- parent binding


def bind_fine_cells_to_parents(cells, parent_cells_metric):
    """Nearest parent cell centre for each fine cell (deterministic tie-break)."""

    parents = [
        (str(parent["grid_id"]), [float(parent["center_metric"][0]), float(parent["center_metric"][1])])
        for parent in parent_cells_metric or [] if parent.get("center_metric")
    ]
    result = {}
    for cell in cells:
        center = cell["center_metric"]
        best, best_distance = None, None
        for grid_id, parent_center in parents:
            value = hypot(center[0] - parent_center[0], center[1] - parent_center[1])
            if best_distance is None or value < best_distance - TOLERANCE or (
                math.isclose(value, best_distance, abs_tol=TOLERANCE) and (best is None or grid_id < best)
            ):
                best, best_distance = grid_id, value
        result[str(cell["fine_cell_id"])] = best
    return result


def plane_fit_geographic_centers(cells, parent_cells_geographic):
    """Display-only lon/lat for fine cell centres.

    The parent cell centres are used to fit ``lon = a + b*x + c*y`` and
    ``lat = d + e*x + f*y`` by ordinary least squares over local metric
    coordinates.  This is **not** a geodetic transform: it is flagged
    ``display_only`` and ``interpolated_from_parent_cells`` so no downstream
    consumer can mistake it for measured coordinates.  When the adapter has a
    real projected-CRS inverse transform it supplies ``center`` itself and this
    helper is not used.
    """

    parents = [
        (float(parent["center_metric"][0]), float(parent["center_metric"][1]),
         float(parent["center"][0]), float(parent["center"][1]))
        for parent in parent_cells_geographic or []
        if parent.get("center_metric") and parent.get("center")
    ]
    if len(parents) < 3:
        return {}
    coefficients = _plane_coefficients(parents)
    if coefficients is None:
        return {}
    result = {}
    for cell in cells:
        x, y = cell["center_metric"]
        result[str(cell["fine_cell_id"])] = [
            round(coefficients[0][0] + coefficients[0][1] * x + coefficients[0][2] * y, 12),
            round(coefficients[1][0] + coefficients[1][1] * x + coefficients[1][2] * y, 12),
        ]
    return result


def _plane_coefficients(parents):
    """Least-squares plane for (lon, lat) over (x, y); ``None`` if degenerate."""

    xs = [item[0] for item in parents]
    ys = [item[1] for item in parents]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    determinant = sxx * syy - sxy * sxy
    if abs(determinant) <= 1e-12:
        return None
    result = []
    for index in (2, 3):
        values = [item[index] for item in parents]
        mean_v = sum(values) / len(values)
        sxv = sum((x - mean_x) * (v - mean_v) for x, v in zip(xs, values))
        syv = sum((y - mean_y) * (v - mean_v) for y, v in zip(ys, values))
        b = (sxv * syy - syv * sxy) / determinant
        c = (syv * sxx - sxv * sxy) / determinant
        a = mean_v - b * mean_x - c * mean_y
        result.append((a, b, c))
    return result


# --------------------------------------------------------------------------- airspace


def map_airspace_to_fine(parent_airspace_by_id, parent_binding):
    """Fine cell airspace = its parent cell's *confirmed* policy status.

    V3-B consumes the confirmed ``AirspacePolicy`` only.  A missing parent, an
    unconfirmed policy or an unknown/restricted status stays fail-closed; the
    classification is never inferred from a layer name or colour.
    """

    result = {}
    for fine_cell_id, parent_grid_id in parent_binding.items():
        entry = parent_airspace_by_id.get(parent_grid_id) if parent_grid_id else None
        if not isinstance(entry, dict):
            result[fine_cell_id] = {
                "status": "unknown",
                "feature_id": None,
                "mapping_method": AIRSPACE_MAPPING_METHOD,
                "parent_grid_id": parent_grid_id,
                "policy_confirmed": False,
                "reason": "parent_cell_airspace_policy_missing",
            }
            continue
        status = str(entry.get("status") or "unknown")
        confirmed = bool(entry.get("policy_confirmed", False))
        effective = status if confirmed and status in ("confirmed_allowed", "confirmed_restricted") else "unknown"
        result[fine_cell_id] = {
            "status": effective,
            "feature_id": entry.get("feature_id"),
            "mapping_method": AIRSPACE_MAPPING_METHOD,
            "parent_grid_id": parent_grid_id,
            "policy_confirmed": confirmed,
            "reason": None if confirmed else "unconfirmed_airspace_policy_is_never_allowed",
        }
    return result


# --------------------------------------------------------------------------- soft fields

#: Where each mapped soft channel is read from inside the existing risk result.
#: V3-B **reuses** the RiskModel contributor ``normalized`` index; it does not
#: re-derive population or traffic risk.
RISK_CONTRIBUTOR_PATHS = {
    "population_risk": ("ground", "population"),
    "traffic_risk": ("ground", "traffic"),
}


def parent_soft_fields_from_risk_contributors(grid_risk, grid_ids, *, source_resolution_m=None):
    """Coarse soft fields taken from the existing RiskModel contributor indices.

    The value is the contributor's own ``normalized`` index (already clipped to
    ``[0, 1]`` by ``risk.v1``); its provenance names the exact contributor path and
    the risk algorithm version, so the soft cost stays traceable to the existing
    risk model instead of a V3-specific re-derivation.  A contributor that is not
    ``passed`` stays ``unknown`` -- enabling that channel then blocks the run.
    """

    cells = (grid_risk or {}).get("cells") or {}
    algorithm_id = (grid_risk or {}).get("algorithm_id")
    algorithm_version = (grid_risk or {}).get("algorithm_version")
    risk_status = (grid_risk or {}).get("status")
    result = {}
    for grid_id in grid_ids or []:
        cell = cells.get(str(grid_id)) or {}
        soft = {}
        for channel, (section, name) in RISK_CONTRIBUTOR_PATHS.items():
            section_entry = cell.get(section) or {}
            contributor = (section_entry.get("contributors") or {}).get(name) or {}
            normalized = contributor.get("normalized")
            if isinstance(normalized, bool) or not isinstance(normalized, (int, float)):
                normalized = None
            usable = (
                risk_status == "passed"
                and contributor.get("status") == "passed"
                and normalized is not None
                and 0.0 <= float(normalized) <= 1.0
            )
            soft[channel] = {
                "normalized_index": float(normalized) if usable else None,
                "status": "passed" if usable else (
                    "not_provided" if not cells else "unknown"
                ),
                "source": f"grid_risk.{section}.contributors.{name}.normalized",
                "source_resolution_m": source_resolution_m,
                "mapping_method": "reused_existing_risk_model_contributor_normalized_index",
                "upsampled_without_new_information": False,
                "risk_model": {
                    "algorithm_id": algorithm_id,
                    "algorithm_version": algorithm_version,
                    "contributor": name,
                    "contributor_status": contributor.get("status"),
                },
                "semantics": (
                    "length_integrated_population_exposure_index_not_probability"
                    if channel == "population_risk"
                    else "length_integrated_traffic_exposure_index_not_conflict_probability"
                ),
                "reason": None if usable else "risk_contributor_normalized_unavailable",
            }
        result[str(grid_id)] = soft
    return result


def upsample_soft_fields(parent_cells_by_id, parent_binding):
    """Coarse soft indices copied onto fine cells, flagged as *not new information*."""

    result = {}
    for fine_cell_id, parent_grid_id in parent_binding.items():
        parent = parent_cells_by_id.get(parent_grid_id) if parent_grid_id else None
        entry = {}
        for channel in UPSAMPLED_CHANNELS:
            source = ((parent or {}).get("soft_fields") or {}).get(channel) or {}
            entry[channel] = {
                "normalized_index": source.get("normalized_index"),
                "status": source.get("status") or "not_provided",
                "source": source.get("source"),
                "source_resolution_m": source.get("source_resolution_m"),
                "mapping_method": SOFT_FIELD_MAPPING_METHOD,
                "upsampled_without_new_information": True,
                "coarse_source_cell_id": parent_grid_id,
                "coarse_mapping_method": source.get("mapping_method"),
                "semantics": source.get("semantics"),
            }
        result[fine_cell_id] = entry
    return result


# --------------------------------------------------------------------------- terrain


def terrain_fact_from_pixels(values, nodata_value=None):
    """Conservative terrain fact from the pixels intersecting one fine cell.

    ``values`` are raw pixel values; ``nodata_value`` (and non-finite values) are
    excluded.  The hard floor uses the **maximum** valid elevation, never the
    centre sample or the mean.  No valid pixel at all means ``unknown`` -- which
    is blocked downstream, never treated as flat ground.
    """

    valid = []
    nodata = 0
    for raw in values or []:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            nodata += 1
            continue
        if not isfinite(value) or (nodata_value is not None and value == float(nodata_value)):
            nodata += 1
            continue
        valid.append(value)
    if not valid:
        return {
            "data_status": "unknown",
            "surface_elevation_max_egm2008_m": None,
            "valid_pixel_count": 0,
            "nodata_pixel_count": nodata,
            "sampling": TERRAIN_SAMPLING_METHOD,
            "reason": "no_valid_dtm_pixel_intersects_this_fine_cell_nodata_is_not_flat_ground",
        }
    return {
        "data_status": "passed",
        "surface_elevation_max_egm2008_m": round(max(valid), 9),
        "valid_pixel_count": len(valid),
        "nodata_pixel_count": nodata,
        "sampling": TERRAIN_SAMPLING_METHOD,
        "reason": None,
    }


def terrain_floor(terrain_fact, terrain_clearance_m):
    if terrain_fact.get("data_status") != "passed" or terrain_clearance_m is None:
        return None
    elevation = terrain_fact.get("surface_elevation_max_egm2008_m")
    return None if elevation is None else round(float(elevation) + float(terrain_clearance_m), 9)


# --------------------------------------------------------------------------- buildings


def building_facts_for_cells(
    cells, footprints, *, horizontal_clearance_m, vertical_clearance_m, terrain_floor_by_cell,
):
    """Conservative building envelope per fine cell.

    A footprint affects a cell when the footprint's ring comes within
    ``horizontal_clearance_m`` of the cell rectangle.  The required floor is

        max over affecting footprints of ( ground_max + height + vertical_clearance )

    where ``ground_max`` is the DTM maximum over the footprint (supplied by the
    adapter; it is *not* re-derived here).  A footprint without a confirmed
    ``height_m`` -- or without a resolved ground elevation -- makes the cell
    ``unknown``, which is blocked.  The source geometry is never modified: this is
    a fine-grid conservative envelope, **not** the V3-C exact polygon clearance.
    """

    result = {}
    for cell in cells:
        rect = cell.get("bbox_metric")
        affecting = []
        unresolved = []
        for footprint in footprints or []:
            ring = footprint.get("ring_metric") or []
            if len(ring) < 3 or not rect:
                continue
            distance = ring_rect_distance(ring, rect)
            if distance is None or distance > float(horizontal_clearance_m) + TOLERANCE:
                continue
            height = footprint.get("height_m")
            ground = footprint.get("ground_elevation_max_egm2008_m")
            if height is None or ground is None:
                unresolved.append(str(footprint.get("building_id")))
                continue
            if str(footprint.get("height_status") or "").lower() in ("missing", "unknown", ""):
                unresolved.append(str(footprint.get("building_id")))
                continue
            affecting.append({
                "building_id": str(footprint.get("building_id")),
                "distance_m": round(distance, 6),
                "ground_elevation_max_egm2008_m": float(ground),
                "height_m": float(height),
                "required_clearance_egm2008_m": round(
                    float(ground) + float(height) + float(vertical_clearance_m), 9,
                ),
            })
        if unresolved:
            result[str(cell["fine_cell_id"])] = {
                "data_status": "unknown",
                "required_clearance_egm2008_m": None,
                "roof_elevation_max_egm2008_m": None,
                "ground_elevation_max_egm2008_m": None,
                "horizontal_clearance_m": float(horizontal_clearance_m),
                "building_ids": sorted(unresolved),
                "mapping_method": BUILDING_MAPPING_METHOD,
                "reason": "building_height_or_ground_unknown_fail_closed",
            }
            continue
        if not affecting:
            result[str(cell["fine_cell_id"])] = {
                "data_status": "passed",
                "required_clearance_egm2008_m": None,
                "roof_elevation_max_egm2008_m": None,
                "ground_elevation_max_egm2008_m": terrain_floor_by_cell.get(str(cell["fine_cell_id"])),
                "horizontal_clearance_m": float(horizontal_clearance_m),
                "building_ids": [],
                "mapping_method": BUILDING_MAPPING_METHOD,
                "reason": None,
            }
            continue
        required = max(item["required_clearance_egm2008_m"] for item in affecting)
        roof = max(
            item["ground_elevation_max_egm2008_m"] + item["height_m"] for item in affecting
        )
        result[str(cell["fine_cell_id"])] = {
            "data_status": "passed",
            "required_clearance_egm2008_m": round(required, 9),
            "roof_elevation_max_egm2008_m": round(roof, 9),
            "ground_elevation_max_egm2008_m": max(
                item["ground_elevation_max_egm2008_m"] for item in affecting
            ),
            "horizontal_clearance_m": float(horizontal_clearance_m),
            "building_ids": sorted(item["building_id"] for item in affecting),
            "mapping_method": BUILDING_MAPPING_METHOD,
            "envelope_semantics": "conservative_fine_grid_envelope_not_exact_polygon_clearance",
            "reason": None,
        }
    return result


def ring_rect_distance(ring, rect):
    """Distance between a polygon ring and an axis-aligned rectangle (0 if touching).

    Returns ``None`` when the ring is degenerate.
    """

    points = [[float(point[0]), float(point[1])] for point in ring if len(point) >= 2]
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    west, south, east, north = (float(value) for value in rect)
    corners = ([west, south], [east, south], [east, north], [west, north])
    # Rectangle entirely inside the ring (and vice-versa) counts as touching.
    if _point_in_ring([(west + east) / 2, (south + north) / 2], points):
        return 0.0
    if any(_point_in_ring(corner, points) for corner in corners):
        return 0.0
    best = None
    for left, right in zip(points, points[1:]):
        if _segment_intersects_rect(left, right, rect):
            return 0.0
        value = _segment_rect_distance(left, right, rect)
        if value is not None and (best is None or value < best):
            best = value
    return best


def _point_in_ring(point, ring):
    inside = False
    for left, right in zip(ring, ring[1:]):
        if (left[1] > point[1]) != (right[1] > point[1]):
            crossing = (right[0] - left[0]) * (point[1] - left[1]) / (right[1] - left[1]) + left[0]
            if crossing > point[0]:
                inside = not inside
    return inside


def _segment_intersects_rect(left, right, rect, tolerance=TOLERANCE):
    west, south, east, north = (float(value) for value in rect)
    dx, dy = right[0] - left[0], right[1] - left[1]
    p = (-dx, dx, -dy, dy)
    q = (left[0] - west, east - left[0], left[1] - south, north - left[1])
    low, high = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) <= 1e-15:
            if qi < -tolerance:
                return False
            continue
        ratio = qi / pi
        if pi < 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high + tolerance:
            return False
    return high >= low - tolerance


def _segment_rect_distance(left, right, rect):
    if _segment_intersects_rect(left, right, rect):
        return 0.0
    west, south, east, north = (float(value) for value in rect)
    candidates = [
        _point_segment_distance([west, south], left, right),
        _point_segment_distance([east, south], left, right),
        _point_segment_distance([east, north], left, right),
        _point_segment_distance([west, north], left, right),
        _point_rect_distance(left, rect),
        _point_rect_distance(right, rect),
    ]
    return min(candidates)


def _point_segment_distance(point, left, right):
    dx, dy = right[0] - left[0], right[1] - left[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1e-18:
        return hypot(point[0] - left[0], point[1] - left[1])
    ratio = ((point[0] - left[0]) * dx + (point[1] - left[1]) * dy) / denominator
    ratio = max(0.0, min(1.0, ratio))
    return hypot(point[0] - (left[0] + dx * ratio), point[1] - (left[1] + dy * ratio))


def _point_rect_distance(point, rect):
    west, south, east, north = (float(value) for value in rect)
    dx = max(west - point[0], 0.0, point[0] - east)
    dy = max(south - point[1], 0.0, point[1] - north)
    return hypot(dx, dy)


# --------------------------------------------------------------------------- assembly


def assemble_fine_environment(
    *, spec, cells, terrain_by_cell, buildings_by_cell, airspace_by_cell,
    soft_fields_by_cell, properties, source_audit, source_type,
    status="passed", reason=None, provenance=None,
):
    """Assemble the canonical ``FineCellEnvironment`` in a stable cell order."""

    result_cells = []
    for cell in cells:
        fine_cell_id = str(cell["fine_cell_id"])
        terrain = dict(terrain_by_cell.get(fine_cell_id) or {})
        terrain["surface_clearance_egm2008_m"] = terrain_floor(
            terrain, properties.get("terrain_clearance_m"),
        )
        result_cells.append({
            "fine_cell_id": fine_cell_id,
            "row": cell.get("row"),
            "column": cell.get("column"),
            "center_metric": cell.get("center_metric"),
            "center": cell.get("center"),
            "bbox_metric": cell.get("bbox_metric"),
            "cell_size_m": cell.get("cell_size_m"),
            "parent_grid_id": cell.get("parent_grid_id"),
            "parent_binding": cell.get("parent_binding") or "nearest_parent_cell_center",
            "terrain": terrain,
            "buildings": dict(buildings_by_cell.get(fine_cell_id) or {}),
            "airspace": dict(airspace_by_cell.get(fine_cell_id) or {}),
            "soft_fields": dict(soft_fields_by_cell.get(fine_cell_id) or {}),
        })
    return {
        "schema_version": "3.1-fine-environment",
        "status": status,
        "reason": reason,
        "canonical_vertical_reference": "egm2008_orthometric",
        "source_type": source_type,
        "spec": spec,
        "source_audit": dict(source_audit or {}),
        "properties": dict(properties or {}),
        "cells": result_cells,
        "provenance": dict(provenance or {}),
    }


def fine_grid_summary(spec):
    return {
        "schema_version": FINE_GRID_SPEC_SCHEMA_VERSION,
        "spec_id": spec.get("spec_id"),
        "resolution_m": spec.get("resolution_m"),
        "resolution_source": spec.get("resolution_source"),
        "requested_resolution_m": spec.get("requested_resolution_m"),
        "effective_source_resolution_m": spec.get("effective_source_resolution_m"),
        "resolution_deviation_m": spec.get("resolution_deviation_m"),
        "nx": spec.get("nx"),
        "ny": spec.get("ny"),
        "cell_count": spec.get("cell_count"),
        "corridor_id": spec.get("corridor_id"),
        "corridor_ring_n": spec.get("corridor_ring_n"),
        "parent_grid_ids": list(spec.get("parent_grid_ids") or []),
        "mapping_method": spec.get("mapping_method"),
        "support_corridor_mapping": SUPPORT_CORRIDOR_MAPPING,
        "not_a_safety_clearance": True,
    }


__all__ = [
    "DISPLAY_GEOGRAPHIC_METHOD", "RISK_CONTRIBUTOR_PATHS", "SUPPORT_CORRIDOR_MAPPING",
    "TOLERANCE", "UPSAMPLED_CHANNELS", "assemble_fine_environment",
    "bind_fine_cells_to_parents", "build_fine_grid_spec", "build_local_frame",
    "building_facts_for_cells", "cell_id", "fine_adjacency", "fine_cells_from_spec",
    "fine_grid_summary", "fine_index", "fine_metric_bounds", "map_airspace_to_fine",
    "parent_soft_fields_from_risk_contributors", "plane_fit_geographic_centers",
    "ring_rect_distance", "terrain_fact_from_pixels", "terrain_floor",
    "upsample_soft_fields",
]
