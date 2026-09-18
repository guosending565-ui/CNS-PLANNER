"""GIS boundary for the Layered Route Planner V1 coarse feasibility facts.

The algorithm package (``cns_planner.layered_route_planner``) must never read QGIS/GDAL or
any file.  This adapter produces the canonical JSON facts it consumes:

* terrain: the **existing verified FABDEM window sampler**
  (``cns_planner.gis.fine_environment_adapter.FabdemWindowTerrainSource``) — a single
  read-only window per run, no resampling, NoData is never filled and never treated as
  flat ground;
* buildings: the **existing L8 building grid facts** already mapped into
  ``grid_attributes.buildings`` (``building_count`` / ``height_max_m`` /
  ``valid_height_fraction``) — the aggregation is not re-implemented here.

The result is deliberately a *coarse strategic vertical envelope* per L8 cell.  Exact
footprint / horizontal clearance is the later continuous-validation stage's job.
"""

from __future__ import annotations

#: The L8 bounding boxes are already OGC:CRS84 (same assumption as the existing V3-A
#: real-source adapter).
LAYERED_TERRAIN_MAPPING_METHOD = "same_l8_grid_cell_bbox_read_only_window"
LAYERED_BUILDING_MAPPING_METHOD = "same_l8_grid_direct_mapping_of_existing_building_grid_facts"

TERRAIN_FACT_KEYS = (
    "data_status", "surface_elevation_max_egm2008_m", "valid_pixel_count",
    "nodata_pixel_count", "sampling", "reason",
)
BUILDING_FACT_KEYS = (
    "data_status", "building_count", "height_max_m", "height_p95_m",
    "valid_height_fraction", "building_coverage_ratio", "reason",
)


class _GeographicIdentityTransform:
    """L8 cell bboxes are already geographic OGC:CRS84 coordinates."""

    authority = "OGC:CRS84"

    @staticmethod
    def to_geographic(point):
        return [float(point[0]), float(point[1])]


def _terrain_fact(raw):
    fact = raw if isinstance(raw, dict) else {}
    elevation = fact.get("surface_elevation_max_egm2008_m")
    passed = str(fact.get("data_status") or "") == "passed" and isinstance(
        elevation, (int, float),
    ) and not isinstance(elevation, bool)
    if passed:
        return {
            "data_status": "passed",
            "surface_elevation_max_egm2008_m": float(elevation),
            "valid_pixel_count": fact.get("valid_pixel_count"),
            "nodata_pixel_count": fact.get("nodata_pixel_count"),
            "sampling": fact.get("sampling"),
            "reason": None,
        }
    return {
        "data_status": "unknown",
        "surface_elevation_max_egm2008_m": None,
        "valid_pixel_count": fact.get("valid_pixel_count"),
        "nodata_pixel_count": fact.get("nodata_pixel_count"),
        "sampling": fact.get("sampling"),
        "reason": str(fact.get("reason") or "terrain_data_unavailable"),
    }


def _building_fact(raw):
    """Pass the existing L8 building facts through; never rebuild the aggregation."""

    fact = raw if isinstance(raw, dict) else {}
    if str(fact.get("status") or "") != "passed":
        return {
            "data_status": "unknown",
            "building_count": None,
            "height_max_m": None,
            "height_p95_m": None,
            "valid_height_fraction": None,
            "building_coverage_ratio": None,
            "reason": str(fact.get("reason") or "building_grid_missing_or_outside_coverage"),
        }
    count = fact.get("building_count")
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        return {
            "data_status": "unknown",
            "building_count": None,
            "height_max_m": None,
            "height_p95_m": None,
            "valid_height_fraction": None,
            "building_coverage_ratio": fact.get("building_coverage_ratio"),
            "reason": "building_count_missing",
        }
    return {
        "data_status": "passed",
        "building_count": int(count),
        "height_max_m": fact.get("height_max_m"),
        "height_p95_m": fact.get("height_p95_m"),
        "valid_height_fraction": fact.get("valid_height_fraction"),
        "building_coverage_ratio": fact.get("building_coverage_ratio"),
        "reason": None,
        "height_field": "height_max_m",
    }


class LayeredFeasibilityAdapter:
    """Read-only terrain/building facts for the layered feasibility mask."""

    adapter_id = "layered_feasibility_coarse_envelope_adapter"
    adapter_version = "1.0"

    def __init__(self, terrain_source):
        self.terrain_source = terrain_source

    def source_status(self):
        status = {"terrain": None, "mapping_method": LAYERED_TERRAIN_MAPPING_METHOD}
        if self.terrain_source is None:
            status["terrain"] = {"available": False, "reason": "terrain_source_not_configured"}
            return status
        usable = getattr(self.terrain_source, "usable", None)
        if callable(usable):
            ok, reason = usable()
            status["terrain"] = {
                "available": bool(ok),
                "reason": reason,
                "vertical_reference": getattr(self.terrain_source, "vertical_reference", None),
                "vertical_status": getattr(self.terrain_source, "vertical_status", None),
            }
        else:
            status["terrain"] = {"available": True, "reason": None}
        return status

    def describe(self):
        describe = getattr(self.terrain_source, "describe", None)
        detail = describe() if callable(describe) else None
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "terrain": detail,
            "terrain_mapping_method": LAYERED_TERRAIN_MAPPING_METHOD,
            "building_mapping_method": LAYERED_BUILDING_MAPPING_METHOD,
            "read_mode": "single_read_only_window_per_run_read_as_array_no_resample",
            "scope": "coarse_strategic_vertical_envelope",
            "not_exact_footprint": True,
            "not_horizontal_clearance": True,
        }

    def build_cells(self, grid_cells, state):
        """Canonical ``[{grid_id, terrain, buildings}]`` facts for every L8 cell."""

        cells = [cell for cell in (grid_cells or []) if isinstance(cell, dict)]
        status = self.source_status()
        terrain_available = bool((status.get("terrain") or {}).get("available"))
        terrain_facts = {}
        if cells and terrain_available:
            inputs = [
                {"fine_cell_id": str(cell["grid_id"]), "bbox_metric": list(cell["bbox"])}
                for cell in cells if cell.get("grid_id") and cell.get("bbox")
            ]
            sampled = self.terrain_source.sample_cells(
                inputs, transform=_GeographicIdentityTransform(),
            )
            terrain_facts = sampled if isinstance(sampled, dict) else {}
        elif cells:
            reason = str(
                (status.get("terrain") or {}).get("reason") or "terrain_source_unavailable"
            )
            terrain_facts = {
                str(cell["grid_id"]): {
                    "data_status": "unknown",
                    "surface_elevation_max_egm2008_m": None,
                    "reason": reason,
                }
                for cell in cells if cell.get("grid_id")
            }
        building_cells = (
            ((state or {}).get("grid_attributes") or {}).get("buildings") or {}
        ).get("cells") or {}
        result = []
        for cell in cells:
            grid_id = str(cell.get("grid_id") or "")
            if not grid_id:
                continue
            result.append({
                "grid_id": grid_id,
                "terrain": _terrain_fact(terrain_facts.get(grid_id)),
                "buildings": _building_fact(building_cells.get(grid_id)),
            })
        return result


def layered_feasibility_source_status(state, terrain_path=None):
    """Read-only readiness summary (never opens a dataset)."""

    audits = ((state or {}).get("source_audits") or {}).get("items") or {}
    buildings = audits.get("buildings") or {}
    building_grid = audits.get("building_grid") or {}
    return {
        "terrain_dtm": {
            "role": "terrain_dtm",
            "configured": bool(terrain_path),
            "audit_status": (audits.get("terrain_dtm") or {}).get("status"),
        },
        "buildings": {"role": "buildings", "audit_status": buildings.get("status")},
        "building_grid": {"role": "building_grid", "audit_status": building_grid.get("status")},
        "airspace": {
            "role": "airspace",
            "applicability": "display_only",
            "used_in_feasibility": False,
        },
    }


__all__ = [
    "LAYERED_BUILDING_MAPPING_METHOD", "LAYERED_TERRAIN_MAPPING_METHOD",
    "LayeredFeasibilityAdapter", "layered_feasibility_source_status",
]
