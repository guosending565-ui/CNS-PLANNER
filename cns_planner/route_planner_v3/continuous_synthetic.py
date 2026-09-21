"""Deterministic **synthetic** V3-C domain evidence (never real data).

This module builds canonical V3-C domain evidence from an explicit specification so
the continuous realizer and every source-native validator can be exercised end to
end without QGIS/GDAL.  It fabricates *nothing silently*: the terrain pixels,
building footprints, airspace polygons and their clearance parameters all come from
the explicit spec, and the assembled evidence is labelled
``source_type=synthetic`` with the spec recorded.

It is deliberately **not** a validator and **not** a planner: it only turns an
explicit description into the canonical ``domain_evidence`` shape the pure
validators consume.  Real runs use the GIS adapter instead.
"""

from __future__ import annotations

from math import hypot

from .continuous_contracts import (
    default_v3_validation_policy, normalize_v3_validation_policy,
)
from .continuous_raster_window import resolve_native_pixel_intervals

SYNTHETIC_EVIDENCE_SOURCE_TYPE = "synthetic_v3c_domain_evidence"

#: Default specs.  Everything is metric and local; no projection is implied.
SYNTHETIC_CURVE_CHORD_ERROR_M = 0.5


def default_synthetic_continuous_spec():
    """An explicit, deterministic metric exercise specification."""

    return {
        "spec_id": "synthetic_v3c_open",
        "scene_bounds_metric": [-2000.0, -2000.0, 4000.0, 4000.0],
        "allowed_metric_polygons": [
            [[-2000.0, -2000.0], [4000.0, -2000.0], [4000.0, 4000.0], [-2000.0, 4000.0]],
        ],
        "blocked_metric_polygons": [],
        "unconfirmed_metric_polygons": [],
        "dtm": {
            "enabled": True,
            "resolution_m": 100.0,
            "base_elevation_m": 0.0,
            "scene_bounds_metric": [-2000.0, -2000.0, 4000.0, 4000.0],
            "spikes": [],
            "nodata_cells": [],
        },
        "buildings": [],
        "clearances": {
            "terrain_clearance_m": 30.0,
            "building_horizontal_clearance_m": 50.0,
            "building_vertical_clearance_m": 25.0,
        },
        "min_altitude_egm2008_m": 100.0,
        "max_altitude_egm2008_m": 400.0,
        "aircraft_min_turn_radius_m": 200.0,
        "max_climb_gradient": 0.08,
        "max_descent_gradient": 0.08,
        "curve_chord_error_m": SYNTHETIC_CURVE_CHORD_ERROR_M,
        "max_validation_samples": 200000,
        "source": "synthetic V3-C exercise spec（显式，不是真实数据）",
        "confirmed": True,
    }


def normalize_synthetic_continuous_spec(value=None):
    base = default_synthetic_continuous_spec()
    source = value if isinstance(value, dict) else {}
    result = {**base, **{key: source[key] for key in source if key in base}}
    for key in ("allowed_metric_polygons", "blocked_metric_polygons", "unconfirmed_metric_polygons",
                "buildings"):
        if key in source and isinstance(source[key], list):
            result[key] = source[key]
    dtm = dict(base["dtm"])
    if isinstance(source.get("dtm"), dict):
        dtm.update(source["dtm"])
    for key in ("spikes", "nodata_cells"):
        if isinstance(source.get("dtm"), dict) and isinstance(source["dtm"].get(key), list):
            dtm[key] = source["dtm"][key]
    result["dtm"] = dtm
    clearances = dict(base["clearances"])
    if isinstance(source.get("clearances"), dict):
        clearances.update(source["clearances"])
    result["clearances"] = clearances
    result["spec_id"] = str(source.get("spec_id") or base["spec_id"])
    return result


def build_synthetic_continuous_evidence(spec=None, *, continuous_route=None):
    """Assemble the canonical V3-C ``domain_evidence`` from an explicit spec.

    When ``continuous_route`` (the realized geometry) is supplied, the native DTM
    pixels are resolved into the along-track intervals over which each of them is the
    limiting terrain constraint -- the same pure helper the GIS adapter uses, so the
    synthetic and real paths cannot drift apart.
    """

    normalized = normalize_synthetic_continuous_spec(spec)
    dtm_spec = normalized["dtm"]
    buildings = normalized["buildings"] or []
    pixels, pixel_meta = _synthetic_dtm_pixels(dtm_spec)
    resolved_pixels = pixels
    if continuous_route is not None:
        resolved_pixels = resolve_native_pixel_intervals(
            continuous_route, pixels,
            curve_chord_error_m=normalized["curve_chord_error_m"],
        )
    evidence = {
        "source_type": SYNTHETIC_EVIDENCE_SOURCE_TYPE,
        "source_detail": {"spec_id": normalized["spec_id"], "spec": normalized},
        "sample_count": len(pixels),
        "to_geographic": None,
        "airspace": {
            "confirmed": bool(normalized.get("confirmed", True)),
            "allowed": [
                {
                    "feature_id": f"synthetic-allowed-{index}",
                    "outer_metric": [[float(point[0]), float(point[1])] for point in polygon],
                    "holes_metric": [],
                    "altitude_interval": None,
                    "source": normalized["source"],
                }
                for index, polygon in enumerate(normalized["allowed_metric_polygons"] or [])
            ],
            "blocked": [
                {
                    "feature_id": f"synthetic-blocked-{index}",
                    "outer_metric": [
                        [float(point[0]), float(point[1])]
                        for point in _entry_ring(item)
                    ],
                    "holes_metric": [],
                    "altitude_interval": _entry_altitude(item),
                    "source": normalized["source"],
                }
                for index, item in enumerate(normalized["blocked_metric_polygons"] or [])
            ],
            "unconfirmed": [
                {
                    "feature_id": f"synthetic-unconfirmed-{index}",
                    "outer_metric": [[float(point[0]), float(point[1])] for point in polygon],
                    "holes_metric": [],
                    "altitude_interval": None,
                    "source": "synthetic_unconfirmed_policy",
                }
                for index, polygon in enumerate(normalized["unconfirmed_metric_polygons"] or [])
            ],
        },
        "terrain": {
            "available": bool(dtm_spec.get("enabled", True)),
            "source": {
                "role": "terrain_dtm",
                "source_type": "synthetic",
                "resolution_m": dtm_spec.get("resolution_m"),
                "crs": "local_metric_synthetic",
                "nodata": -9999.0,
                "vertical_reference": "egm2008_orthometric",
                "read_mode": "synthetic_explicit_pixel_window_no_resample",
            },
            "pixels": resolved_pixels,
            "pixel_metadata": pixel_meta,
            "window": {
                "scene_pixel_count": len(pixels),
                "route_touched_pixel_count": len(resolved_pixels),
                "resolve_method": "pure_python_route_envelope_pixel_window",
                "resampled": False,
            },
        },
        "buildings": {
            "available": True,
            "source": {
                "role": "buildings",
                "source_type": "synthetic",
                "query": "synthetic_explicit_footprint_list",
                "roof_semantics": "shared_building_clearance_roof_elevation_helper",
            },
            "buildings": [
                {
                    "building_id": str(item.get("building_id") or f"synthetic-building-{index}"),
                    "source": "synthetic",
                    "ring_metric": [[float(point[0]), float(point[1])] for point in item["ring_metric"]],
                    # A multi-part footprint keeps every part so the clearance validator can
                    # evaluate all of them (never just the largest one).
                    **({
                        "ring_parts_metric": [
                            [[float(point[0]), float(point[1])] for point in ring]
                            for ring in item["ring_parts_metric"]
                        ],
                    } if item.get("ring_parts_metric") else {}),
                    "height_m": item.get("height_m"),
                    "height_status": item.get("height_status", "predicted"),
                    "ground_elevation_max_egm2008_m": item.get("ground_elevation_max_egm2008_m"),
                }
                for index, item in enumerate(buildings)
                if isinstance(item, dict) and item.get("ring_metric")
            ],
        },
    }
    return evidence


def _as_polygon_entry(item):
    if isinstance(item, dict) and item.get("ring_metric"):
        return {"ring_metric": item["ring_metric"], "altitude_interval": item.get("altitude_interval")}
    return {"ring_metric": item, "altitude_interval": None}


def _entry_ring(item):
    """The metric ring of either a bare polygon or an ``{"ring_metric", ...}`` entry."""

    if isinstance(item, dict) and item.get("ring_metric"):
        return item["ring_metric"]
    return item


def _entry_altitude(item):
    if isinstance(item, dict) and item.get("altitude_interval"):
        return {**item["altitude_interval"], "confirmed": True}
    return None


def _spike_center(spike, resolution):
    center = spike.get("center_metric")
    if isinstance(center, (list, tuple)) and len(center) >= 2:
        return [float(center[0]), float(center[1])]
    return [0.0, 0.0]


def _disc_hits_rect(center, radius, bbox):
    offset_x = max(bbox[0] - center[0], 0.0, center[0] - bbox[2])
    offset_y = max(bbox[1] - center[1], 0.0, center[1] - bbox[3])
    if offset_x <= 0.0 and offset_y <= 0.0:
        return True
    return hypot(offset_x, offset_y) <= radius


def _synthetic_dtm_pixels(dtm_spec):
    """Every synthetic DTM pixel: ``(elevation, NoData)`` on a regular metric grid.

    The pixels are enumerated exhaustively over the scene bounds -- this is exactly
    what the real adapter's window does over a raster -- so the route's pixel set and
    the evidence sample count are honest, not decorative.
    """

    resolution = float(dtm_spec.get("resolution_m") or 100.0)
    bounds = [float(value) for value in dtm_spec.get("scene_bounds_metric")
              or [-2000.0, -2000.0, 4000.0, 4000.0]]
    base = float(dtm_spec.get("base_elevation_m") or 0.0)
    nodata_value = -9999.0
    spikes = [item for item in dtm_spec.get("spikes") or [] if isinstance(item, dict)]
    nodata_cells = [item for item in dtm_spec.get("nodata_cells") or [] if isinstance(item, dict)]
    west, south, east, north = bounds
    columns = max(1, int((east - west) / resolution + 0.5))
    rows = max(1, int((north - south) / resolution + 0.5))
    pixels = []
    for row in range(rows):
        for column in range(columns):
            center = [west + (column + 0.5) * resolution, south + (row + 0.5) * resolution]
            bbox = [
                west + column * resolution, south + row * resolution,
                west + (column + 1) * resolution, south + (row + 1) * resolution,
            ]
            value = base
            status = "passed"
            reason = None
            # A spike/nodata disc affects the pixels it actually covers, so a spike
            # placed on a pixel *boundary* is not silently ignored.
            for spike in spikes:
                if _disc_hits_rect(_spike_center(spike, resolution), float(spike.get("radius_m") or resolution), bbox):
                    value = float(spike.get("elevation_m", value))
            for cell in nodata_cells:
                if _disc_hits_rect(_spike_center(cell, resolution), float(cell.get("radius_m") or resolution), bbox):
                    status, reason, value = "unknown", "synthetic_dtm_nodata_pixel", None
            pixels.append({
                "pixel": [column, row],
                "bbox_metric": [round(value_, 9) for value_ in bbox],
                "center_metric": [round(center[0], 9), round(center[1], 9)],
                "data_status": status,
                "elevation_egm2008_m": None if value is None else round(float(value), 9),
                "source_value": None if value is None else round(float(value), 9),
                "reason": reason,
            })
    metadata = {
        "resolution_m": resolution,
        "column_count": columns,
        "row_count": rows,
        "pixel_count": len(pixels),
        "nodata_value": nodata_value,
        "enumerated_exhaustively": True,
        "not_a_safety_clearance": True,
    }
    return pixels, metadata


def synthetic_clearances(spec=None):
    return normalize_synthetic_continuous_spec(spec)["clearances"]


def synthetic_validation_policy(spec=None, *, confirmed=None):
    """A **V3-C** validation policy from the synthetic spec (explicit, no defaults)."""

    normalized = normalize_synthetic_continuous_spec(spec)
    policy = default_v3_validation_policy()
    policy.update({
        "curve_chord_error_m": normalized["curve_chord_error_m"],
        "max_validation_samples": normalized["max_validation_samples"],
        "use_curve_error_envelope": True,
        "source": normalized["source"],
        "confirmed": bool(normalized["confirmed"] if confirmed is None else confirmed),
    })
    return normalize_v3_validation_policy(policy)


__all__ = [
    "SYNTHETIC_CURVE_CHORD_ERROR_M", "SYNTHETIC_EVIDENCE_SOURCE_TYPE",
    "build_synthetic_continuous_evidence", "default_synthetic_continuous_spec",
    "normalize_synthetic_continuous_spec", "synthetic_clearances",
    "synthetic_validation_policy",
]
