"""Synthetic corridor-local fine environments for V3-B tests and experiments.

The **real** fine environment comes from ``gis.fine_environment_adapter`` (GDAL
windowed FABDEM reads + QGIS/GPKG RTree building queries; airspace is a
display-only reference layer and is never a source).  This module builds a
*synthetic* one from an explicit specification so the refinement kernel, the
conservative envelopes and the provenance rules can be exercised
deterministically without any real data.

Everything produced here is labelled ``source_type=synthetic``; real-data
readiness stays blocked until the adapter supplies audited evidence.  The module
deliberately reuses the same pure helpers the real adapter uses
(``terrain_fact_from_pixels``, ``building_facts_for_cells``,
``map_airspace_to_fine``, ``upsample_soft_fields``), so a synthetic test exercises
the real mapping code paths rather than a parallel implementation.
"""

from __future__ import annotations

from copy import deepcopy
from math import cos, radians

from .fine_grid import (
    DISPLAY_GEOGRAPHIC_METHOD, SUPPORT_CORRIDOR_MAPPING, assemble_fine_environment,
    bind_fine_cells_to_parents, build_fine_grid_spec, build_local_frame,
    building_facts_for_cells, fine_cells_from_spec, map_airspace_to_fine,
    terrain_fact_from_pixels, upsample_soft_fields,
)
from .fine_contracts import contract_fingerprint

#: Sub-pixels per fine cell used to exercise the "max of intersecting valid
#: pixels" rule: a single elevated sub-pixel must raise the hard floor even though
#: the cell mean stays low.
SUBPIXELS_PER_AXIS = 2

SYNTHETIC_FINE_SPEC_DEFAULT = {
    "profile_id": "synthetic_fine_open",
    "base_surface_elevation_m": 0.0,
    #: {fine_cell_id: elevation_m} -- one elevated sub-pixel per listed cell.
    "terrain_spikes": {},
    "unknown_terrain_cells": [],
    "unknown_building_cells": [],
    #: [{"row_min","row_max","column_min","column_max","height_m"|None,
    #:   "ground_elevation_max_egm2008_m"|None,"building_id"}]
    "building_blocks": [],
    "restricted_cells": [],
    "unknown_airspace_cells": [],
    "unconfirmed_airspace_cells": [],
    "resolution_m": 30.0,
    "max_stride_cells": 1,
    "soft_field_provenance": {},
}

SYNTHETIC_FRAME_AUTHORITY = "synthetic:local_equirectangular_m"
SYNTHETIC_FRAME_METHOD = "synthetic_equirectangular_metric_definition_explicit_not_a_geodetic_crs"


def _metres_per_degree(reference_latitude_deg):
    metres_lat = 111132.0
    metres_lon = 111320.0 * max(0.05, cos(radians(reference_latitude_deg)))
    return metres_lon, metres_lat


def geographic_to_metric(point, *, origin, metres_per_degree):
    metres_lon, metres_lat = metres_per_degree
    return [
        (float(point[0]) - origin[0]) * metres_lon,
        (float(point[1]) - origin[1]) * metres_lat,
    ]


def metric_to_geographic(point, *, origin, metres_per_degree):
    metres_lon, metres_lat = metres_per_degree
    return [
        origin[0] + float(point[0]) / metres_lon,
        origin[1] + float(point[1]) / metres_lat,
    ]


def normalize_synthetic_fine_spec(value=None):
    spec = deepcopy(SYNTHETIC_FINE_SPEC_DEFAULT)
    source = value if isinstance(value, dict) else {}
    for key in SYNTHETIC_FINE_SPEC_DEFAULT:
        if key in source and source[key] is not None:
            spec[key] = deepcopy(source[key])
    spec["terrain_spikes"] = {str(key): float(item) for key, item in (spec["terrain_spikes"] or {}).items()}
    spec["unknown_terrain_cells"] = [str(item) for item in spec["unknown_terrain_cells"] or []]
    spec["unknown_building_cells"] = [str(item) for item in spec["unknown_building_cells"] or []]
    spec["restricted_cells"] = [str(item) for item in spec["restricted_cells"] or []]
    spec["unknown_airspace_cells"] = [str(item) for item in spec["unknown_airspace_cells"] or []]
    spec["unconfirmed_airspace_cells"] = [str(item) for item in spec["unconfirmed_airspace_cells"] or []]
    blocks = []
    for raw in spec["building_blocks"] or []:
        blocks.append({
            "building_id": str(raw.get("building_id") or f"SYNB-{len(blocks) + 1:03d}"),
            "row_min": int(raw["row_min"]), "row_max": int(raw["row_max"]),
            "column_min": int(raw["column_min"]), "column_max": int(raw["column_max"]),
            "height_m": None if raw.get("height_m") in (None, "") else float(raw["height_m"]),
            "ground_elevation_max_egm2008_m": (
                None if raw.get("ground_elevation_max_egm2008_m") in (None, "")
                else float(raw["ground_elevation_max_egm2008_m"])
            ),
        })
    spec["building_blocks"] = blocks
    spec["resolution_m"] = float(spec["resolution_m"])
    spec["max_stride_cells"] = int(spec["max_stride_cells"])
    return spec


def parent_cells_metric(parent_cells, *, origin, metres_per_degree):
    """Corridor support cells expressed in the synthetic local metric frame."""

    resolution = None
    result = []
    for cell in parent_cells or []:
        center = cell.get("center")
        if not center:
            continue
        metric = geographic_to_metric(center, origin=origin, metres_per_degree=metres_per_degree)
        bbox = cell.get("bbox")
        if bbox:
            west_south = geographic_to_metric(
                [bbox[0], bbox[1]], origin=origin, metres_per_degree=metres_per_degree,
            )
            east_north = geographic_to_metric(
                [bbox[2], bbox[3]], origin=origin, metres_per_degree=metres_per_degree,
            )
            bbox_metric = [west_south[0], west_south[1], east_north[0], east_north[1]]
            resolution = resolution or max(
                bbox_metric[2] - bbox_metric[0], bbox_metric[3] - bbox_metric[1],
            )
        else:
            bbox_metric = None
        result.append({
            "grid_id": str(cell["grid_id"]),
            "center": [float(center[0]), float(center[1])],
            "center_metric": metric,
            "bbox_metric": bbox_metric,
            "soft_fields": deepcopy(cell.get("soft_fields") or {}),
            "airspace": deepcopy(cell.get("airspace") or {}),
        })
    return result, resolution


def build_synthetic_fine_environment(
    *, parent_environment, corridor, policy, spec=None, source_detail=None,
):
    """Build a corridor-local synthetic ``FineCellEnvironment`` + frame + spec.

    Returns ``{"frame", "fine_grid", "environment", "parent_cells_metric"}``.
    Resolution provenance is ``explicit_configuration`` because the synthetic spec
    states it; the real adapter uses ``dtm_effective_resolution`` when the DTM's
    own pixel size is the source of truth.
    """

    spec = normalize_synthetic_fine_spec(spec)
    parent_cells = list(parent_environment.get("cells") or [])
    corridor = corridor or {}
    support_ids = list(corridor.get("support_grid_ids") or corridor.get("center_grid_ids") or [])
    support_set = set(support_ids)
    selected = [cell for cell in parent_cells if str(cell["grid_id"]) in support_set]
    if not selected:
        raise ValueError("synthetic fine 环境需要非空 corridor support cells")
    support_metric, parent_resolution = parent_cells_metric(
        selected, origin=_origin(selected), metres_per_degree=_metres(selected),
    )
    metres_per_degree = _metres(selected)
    origin = _origin(selected)
    west = min(item["bbox_metric"][0] for item in support_metric if item["bbox_metric"])
    south = min(item["bbox_metric"][1] for item in support_metric if item["bbox_metric"])
    east = max(item["bbox_metric"][2] for item in support_metric if item["bbox_metric"])
    north = max(item["bbox_metric"][3] for item in support_metric if item["bbox_metric"])
    frame = build_local_frame(
        metric_bounds=[west, south, east, north],
        horizontal_crs=SYNTHETIC_FRAME_AUTHORITY,
        horizontal_crs_source="synthetic_explicit_definition_not_a_real_epsg_code",
        resolution_m=spec["resolution_m"],
        local_to_geographic={
            "method": SYNTHETIC_FRAME_METHOD,
            "authority": SYNTHETIC_FRAME_AUTHORITY,
            "display_only": True,
            "interpolated_from_parent_cells": False,
            "note": (
                "合成算例的米制帧由显式 equirectangular 定义给出，不是真实大地测量投影；"
                "真实数据必须由 fine environment adapter 提供真实投影与逆变换"
            ),
        },
        provenance={"builder": "route_planner_v3.fine_synthetic", "not_real_data": True},
    )
    grid = build_fine_grid_spec(
        frame=frame,
        resolution_m=spec["resolution_m"],
        resolution_source="explicit_configuration",
        requested_resolution_m=spec["resolution_m"],
        effective_source_resolution_m=parent_resolution,
        parent_grid_resolution_m=parent_resolution,
        parent_cells_metric=support_metric,
        corridor_id=corridor.get("corridor_id"),
        corridor_ring_n=corridor.get("ring_n") or 0,
        provenance={
            "builder": "route_planner_v3.fine_synthetic",
            "support_corridor_mapping": SUPPORT_CORRIDOR_MAPPING,
            "synthetic_spec": deepcopy(spec),
        },
    )
    cells = fine_cells_from_spec(grid)
    for cell in cells:
        cell["center"] = metric_to_geographic(
            cell["center_metric"], origin=origin, metres_per_degree=metres_per_degree,
        )
    binding = bind_fine_cells_to_parents(cells, support_metric)
    for cell in cells:
        cell["parent_grid_id"] = binding[str(cell["fine_cell_id"])]
        cell["parent_binding"] = "nearest_parent_cell_center"
    parent_by_id = {str(item["grid_id"]): item for item in parent_cells}
    airspace = {
        str(cell["fine_cell_id"]): {
            "status": "not_applicable", "applicability": "display_only",
            "mapping_method": "display_only_reference_layer_not_used_for_planning",
            "policy_confirmed": False,
        }
        for cell in cells
    }
    terrain = _terrain(cells, spec)
    footprints = _footprints(cells, spec)
    blocks_by_cell = _blocks_by_cell(cells, spec)
    for fine_cell_id in spec["unknown_building_cells"]:
        blocks_by_cell.setdefault(fine_cell_id, {"unresolved": ["synthetic_unknown_height"]})
    buildings = _buildings(cells, spec, blocks_by_cell, footprints, policy, terrain)
    soft_fields = upsample_soft_fields(parent_by_id, binding)
    _apply_soft_provenance(soft_fields, spec)
    source_audit = {
        "source_type": "synthetic",
        "terrain": {"source": "synthetic_spec", "sampling": "explicit_subpixel_values_not_real_fabdem"},
        "buildings": {"source": "synthetic_spec", "query": "explicit_metric_rectangles_not_gpkg"},
        "airspace": {"status": "not_applicable", "applicability": "display_only"},
        "not_real_data": True,
    }
    source_audit["fingerprint"] = contract_fingerprint(source_audit, prefix="V3BSRC-")
    environment = assemble_fine_environment(
        spec=grid,
        cells=cells,
        terrain_by_cell=terrain,
        buildings_by_cell=buildings,
        airspace_by_cell=airspace,
        soft_fields_by_cell=soft_fields,
        properties={
            "resolution_m": spec["resolution_m"],
            "cell_size_m": spec["resolution_m"],
            "terrain_clearance_m": policy.get("terrain_clearance_m"),
            "building_horizontal_clearance_m": policy.get("building_horizontal_clearance_m"),
            "building_vertical_clearance_m": policy.get("building_vertical_clearance_m"),
            "soft_field_sources": _soft_provenance(spec),
            "subpixels_per_axis": SUBPIXELS_PER_AXIS,
        },
        source_audit=source_audit,
        source_type="synthetic",
        provenance={
            "builder": "route_planner_v3.fine_synthetic.build_synthetic_fine_environment",
            "not_real_data": True,
            "source_detail": dict(source_detail or {}),
        },
    )
    environment["subpixel_terrain_values"] = _subpixel_values(cells, spec)
    return {
        "frame": frame,
        "fine_grid": grid,
        "environment": environment,
        "parent_cells_metric": support_metric,
        "synthetic_spec": spec,
    }


# --------------------------------------------------------------------------- internals


def _origin(cells):
    centers = [cell["center"] for cell in cells if cell.get("center")]
    return [centers[0][0], centers[0][1]]


def _metres(cells):
    centers = [cell["center"] for cell in cells if cell.get("center")]
    latitude = sum(point[1] for point in centers) / len(centers)
    return _metres_per_degree(latitude)


def _terrain(cells, spec):
    values = _subpixel_values(cells, spec)
    result = {}
    unknown = set(spec["unknown_terrain_cells"])
    for cell in cells:
        fine_cell_id = str(cell["fine_cell_id"])
        if fine_cell_id in unknown:
            result[fine_cell_id] = {
                "data_status": "unknown",
                "surface_elevation_max_egm2008_m": None,
                "valid_pixel_count": 0,
                "nodata_pixel_count": SUBPIXELS_PER_AXIS ** 2,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance",
                "reason": "synthetic_explicit_nodata_fail_closed",
            }
            continue
        result[fine_cell_id] = terrain_fact_from_pixels(values[fine_cell_id], nodata_value=None)
    return result


def _subpixel_values(cells, spec):
    """``SUBPIXELS_PER_AXIS^2`` sub-pixel elevations per fine cell.

    A spike raises exactly one sub-pixel, so the *maximum* differs from the mean --
    which is what makes the "hard floor = max intersecting pixel" rule testable.
    """

    spikes = spec["terrain_spikes"]
    base = float(spec["base_surface_elevation_m"])
    count = SUBPIXELS_PER_AXIS ** 2
    result = {}
    for cell in cells:
        fine_cell_id = str(cell["fine_cell_id"])
        values = [base] * count
        spike = spikes.get(fine_cell_id)
        if spike is not None:
            values[count - 1] = float(spike)
        result[fine_cell_id] = values
    return result


def _footprints(cells, spec):
    """Synthetic footprints as metric rectangles around the selected cells."""

    by_index = {(int(cell["row"]), int(cell["column"])): cell for cell in cells}
    footprints = []
    for block in spec["building_blocks"]:
        row_min, row_max = block["row_min"], block["row_max"]
        column_min, column_max = block["column_min"], block["column_max"]
        corners = []
        for row, column in (
            (row_min, column_min), (row_min, column_max),
            (row_max, column_max), (row_max, column_min),
        ):
            cell = by_index.get((row, column))
            if cell is None:
                corners = []
                break
            corners.append(cell["bbox_metric"])
        if not corners:
            continue
        west = min(bbox[0] for bbox in corners)
        south = min(bbox[1] for bbox in corners)
        east = max(bbox[2] for bbox in corners)
        north = max(bbox[3] for bbox in corners)
        footprints.append({
            "building_id": block["building_id"],
            "ring_metric": [
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ],
            "height_m": block["height_m"],
            "height_status": "predicted" if block["height_m"] is not None else "unknown",
            "ground_elevation_max_egm2008_m": (
                block["ground_elevation_max_egm2008_m"]
                if block["ground_elevation_max_egm2008_m"] is not None
                else float(spec["base_surface_elevation_m"])
            ),
        })
    return footprints


def _blocks_by_cell(cells, spec):
    by_index = {(int(cell["row"]), int(cell["column"])): cell for cell in cells}
    result = {}
    for block in spec["building_blocks"]:
        unresolved = block["height_m"] is None or block["ground_elevation_max_egm2008_m"] is None
        for row in range(block["row_min"], block["row_max"] + 1):
            for column in range(block["column_min"], block["column_max"] + 1):
                cell = by_index.get((row, column))
                if cell is None:
                    continue
                bucket = result.setdefault(str(cell["fine_cell_id"]), {"unresolved": []})
                if unresolved:
                    bucket["unresolved"].append(block["building_id"])
    return result


def _buildings(cells, spec, blocks_by_cell, footprints, policy, terrain):
    horizontal = policy.get("building_horizontal_clearance_m")
    vertical = policy.get("building_vertical_clearance_m")
    if horizontal is None or vertical is None:
        raise ValueError("构建 synthetic fine 环境需要显式 building clearance policy")
    facts = building_facts_for_cells(
        cells, footprints,
        horizontal_clearance_m=horizontal, vertical_clearance_m=vertical,
        terrain_floor_by_cell={
            cell["fine_cell_id"]: (terrain.get(cell["fine_cell_id"]) or {}).get(
                "surface_elevation_max_egm2008_m",
            )
            for cell in cells
        },
    )
    for fine_cell_id, bucket in blocks_by_cell.items():
        if not bucket.get("unresolved") or fine_cell_id not in facts:
            continue
        facts[fine_cell_id].update({
            "data_status": "unknown",
            "required_clearance_egm2008_m": None,
            "roof_elevation_max_egm2008_m": None,
            "building_ids": sorted(bucket["unresolved"]),
            "reason": "synthetic_building_height_or_ground_unknown_fail_closed",
        })
    return facts


def _soft_provenance(spec):
    return {
        "population_risk": {
            "source": "coarse_parent_environment_soft_fields",
            "source_resolution_m": None,
            "mapping_method": "coarse_cell_index_upsampled_to_fine_cells",
            "upsampled_without_new_information": True,
        },
        "traffic_risk": {
            "source": "coarse_parent_environment_soft_fields",
            "source_resolution_m": None,
            "mapping_method": "coarse_cell_index_upsampled_to_fine_cells",
            "upsampled_without_new_information": True,
        },
    }


def _apply_soft_provenance(soft_fields, spec):
    overrides = spec.get("soft_field_provenance") or {}
    for entry in soft_fields.values():
        for channel, values in entry.items():
            values["upsampled_without_new_information"] = True
            values["mapping_method"] = "coarse_cell_index_upsampled_to_fine_cells"
            override = overrides.get(channel)
            if isinstance(override, dict):
                values.update({
                    key: override[key] for key in
                    ("source_resolution_m", "coarse_mapping_method") if key in override
                })
    return soft_fields


__all__ = [
    "SUBPIXELS_PER_AXIS", "SYNTHETIC_FINE_SPEC_DEFAULT", "SYNTHETIC_FRAME_AUTHORITY",
    "SYNTHETIC_FRAME_METHOD", "build_synthetic_fine_environment",
    "geographic_to_metric", "metric_to_geographic", "normalize_synthetic_fine_spec",
    "parent_cells_metric",
]
