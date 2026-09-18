"""GIS-boundary adapter for a real-data V3-A canonical environment."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ..benchmark.geodesy import geodesic_distance_m


class _GeographicIdentityTransform:
    """Adapter protocol: L8 bboxes are already OGC:CRS84 coordinates."""

    authority = "OGC:CRS84"

    @staticmethod
    def to_geographic(point):
        return [float(point[0]), float(point[1])]


class V3RealEnvironmentAdapter:
    """Build V3CellEnvironment from audited project state and a DTM sampler.

    File access stays behind this injected GIS object. The planner receives only
    normalized JSON facts and never opens a raster, GeoPackage or QGIS project.
    """

    adapter_id = "v3a_configured_real_sources_adapter"
    adapter_version = "1.0"

    def __init__(self, terrain_source):
        self.terrain_source = terrain_source

    def build(self, *, grid, policy, state):
        cells = list((grid or {}).get("cells") or [])
        terrain_inputs = [
            {"fine_cell_id": str(cell["grid_id"]), "bbox_metric": list(cell["bbox"])}
            for cell in cells
        ]
        terrain = self.terrain_source.sample_cells(
            terrain_inputs, transform=_GeographicIdentityTransform(),
        )
        building_map = (((state.get("grid_attributes") or {}).get("buildings") or {}).get("cells") or {})
        risk = state.get("grid_risk") or {}
        risk_cells = risk.get("cells") or {}
        result_cells = []
        for cell in cells:
            grid_id = str(cell["grid_id"])
            terrain_fact = deepcopy(terrain.get(grid_id) or {})
            elevation = terrain_fact.get("surface_elevation_max_egm2008_m")
            if terrain_fact.get("data_status") == "passed" and elevation is not None:
                terrain_fact["surface_clearance_egm2008_m"] = (
                    float(elevation) + float(policy["terrain_clearance_m"])
                )
            else:
                terrain_fact.update({
                    "data_status": "unknown", "surface_clearance_egm2008_m": None,
                })
            building = _building_fact(
                building_map.get(grid_id), elevation,
                policy.get("building_horizontal_clearance_m"),
                policy.get("building_vertical_clearance_m"),
            )
            bbox = list(cell.get("bbox") or [])
            center = list(cell.get("center") or [])
            cell_size_m = None
            if len(bbox) == 4 and len(center) >= 2:
                cell_size_m = geodesic_distance_m(
                    [bbox[0], center[1]], [bbox[2], center[1]],
                )
            result_cells.append({
                **deepcopy(cell),
                "cell_size_m": cell_size_m,
                "terrain": terrain_fact,
                "buildings": building,
                "airspace": {
                    "status": "not_applicable", "applicability": "display_only",
                    "semantics": "display_only_reference_layer_not_used_for_planning",
                },
                "soft_fields": _soft_fields(risk_cells.get(grid_id), risk),
            })
        audits = _active_audits(state)
        source_detail = {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "terrain_dtm": self.terrain_source.describe(),
            "source_audits": audits,
            "grid_risk": {
                "algorithm_id": risk.get("algorithm_id"),
                "algorithm_version": risk.get("algorithm_version"),
                "input_fingerprint": risk.get("input_fingerprint"),
            },
            "airspace": {"status": "not_applicable", "applicability": "display_only"},
        }
        source_detail["fingerprint"] = _hash(source_detail)
        return {
            "schema_version": "3.0-environment",
            "status": "passed" if result_cells else "missing_data",
            "canonical_vertical_reference": "egm2008_orthometric",
            "source_type": "configured_real_sources",
            "source_detail": source_detail,
            "grid_level": (grid or {}).get("level"),
            "properties": {
                "terrain_clearance_m": policy.get("terrain_clearance_m"),
                "building_horizontal_clearance_m": policy.get("building_horizontal_clearance_m"),
                "building_vertical_clearance_m": policy.get("building_vertical_clearance_m"),
                "soft_field_sources": {
                    "population_risk": "grid_risk.cells[*].ground.score",
                    "traffic_risk": "grid_risk.cells[*].air.score",
                },
            },
            "cells": result_cells,
        }


def _building_fact(raw, terrain_elevation, horizontal, vertical):
    item = raw if isinstance(raw, dict) else {}
    count = item.get("building_count")
    if item.get("status") != "passed" or count is None:
        return _unknown_building("building_grid_missing_or_outside_coverage", horizontal)
    if int(count) == 0:
        return {
            "data_status": "passed", "roof_elevation_max_egm2008_m": None,
            "required_clearance_egm2008_m": None,
            "horizontal_clearance_m": horizontal,
            "semantics": "confirmed_absence_of_building_constraint",
        }
    fraction = item.get("valid_height_fraction")
    height = item.get("height_max_m")
    if fraction is None or float(fraction) < 1.0 or height is None or terrain_elevation is None:
        return _unknown_building("building_height_or_ground_elevation_unresolved", horizontal)
    roof = float(terrain_elevation) + float(height)
    return {
        "data_status": "passed", "roof_elevation_max_egm2008_m": roof,
        "required_clearance_egm2008_m": roof + float(vertical),
        "horizontal_clearance_m": horizontal,
        "semantics": "dtm_cell_max_plus_verified_building_height_max_plus_vertical_clearance",
    }


def _unknown_building(reason, horizontal):
    return {
        "data_status": "unknown", "roof_elevation_max_egm2008_m": None,
        "required_clearance_egm2008_m": None, "horizontal_clearance_m": horizontal,
        "reason": reason,
    }


def _soft_fields(raw, risk):
    item = raw if isinstance(raw, dict) else {}
    provenance = {
        "source": "grid_risk",
        "source_resolution_m": None,
        "mapping_method": "same_l8_grid_direct_mapping",
        "upsampled_without_new_information": False,
        "algorithm_id": risk.get("algorithm_id"),
        "algorithm_version": risk.get("algorithm_version"),
        "input_fingerprint": risk.get("input_fingerprint"),
    }
    return {
        "population_risk": _soft_fact((item.get("ground") or {}).get("score"), provenance),
        "traffic_risk": _soft_fact((item.get("air") or {}).get("score"), provenance),
    }


def _soft_fact(value, provenance):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return {"status": "not_provided", "normalized_index": None, **provenance}
    return {"status": "passed", "normalized_index": max(0.0, min(1.0, float(value))), **provenance}


def _active_audits(state):
    items = ((state.get("source_audits") or {}).get("items") or {})
    return {
        role: {
            key: (items.get(role) or {}).get(key)
            for key in ("status", "source_id", "version_fingerprint", "sha256", "size_bytes", "mtime_ns")
        }
        for role in ("terrain_dtm", "buildings", "building_grid")
    }


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


__all__ = ["V3RealEnvironmentAdapter"]
