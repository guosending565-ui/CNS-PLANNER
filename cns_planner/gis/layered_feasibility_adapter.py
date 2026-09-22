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
#: 铁塔是点几何：以**原始塔坐标**做权威事实，网格只充当"哪些 cell 受该塔影响"的加速索引。
LAYERED_TOWER_MAPPING_METHOD = (
    "per_tower_source_point_geometry_against_the_current_grid_cells_no_coarsening"
)
TOWER_OBSTACLE_SEMANTICS = "obstacle_clearance_not_a_risk_factor"

TERRAIN_FACT_KEYS = (
    "data_status", "surface_elevation_max_egm2008_m", "valid_pixel_count",
    "nodata_pixel_count", "sampling", "reason",
)
BUILDING_FACT_KEYS = (
    "data_status", "building_count", "height_max_m", "height_p95_m",
    "valid_height_fraction", "building_coverage_ratio", "reason",
)
TOWER_FACT_KEYS = (
    "data_status", "tower_count", "resolved_count", "unresolved_count",
    "tower_top_max_egm2008_m", "reason",
)

#: 米→度的近似换算只用于"水平净空影响范围"，不是任何净空值本身。
_METRES_PER_DEGREE_LAT = 110540.0
_METRES_PER_DEGREE_LON_EQUATOR = 111320.0


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


def _no_tower_fact():
    return {
        "data_status": "no_towers",
        "tower_count": 0,
        "resolved_count": 0,
        "unresolved_count": 0,
        "tower_top_max_egm2008_m": None,
        "reason": None,
    }


def _tower_fact(counts):
    if not counts or not counts.get("tower_count"):
        return _no_tower_fact()
    resolved = counts["resolved_count"]
    unresolved = counts["unresolved_count"]
    if unresolved:
        # 有任何一塔高度未解析就不能声称该格无塔或塔顶已知：fail-closed。
        return {
            "data_status": "unknown",
            "tower_count": counts["tower_count"],
            "resolved_count": resolved,
            "unresolved_count": unresolved,
            "tower_top_max_egm2008_m": counts.get("tower_top_max_egm2008_m"),
            "reason": "tower_height_unresolved",
        }
    return {
        "data_status": "passed",
        "tower_count": counts["tower_count"],
        "resolved_count": resolved,
        "unresolved_count": 0,
        "tower_top_max_egm2008_m": counts.get("tower_top_max_egm2008_m"),
        "reason": None,
    }


def _horizontal_half_degrees(clearance_m, latitude):
    """把显式水平净空（米）换算成经纬度 bbox 半径；未配置即 0（不假设任何裕度）。"""

    if not isinstance(clearance_m, (int, float)) or isinstance(clearance_m, bool):
        return 0.0, 0.0
    metres = float(clearance_m)
    if metres <= 0:
        return 0.0, 0.0
    import math

    cos_lat = max(0.05, abs(math.cos(math.radians(float(latitude or 0.0)))))
    return metres / _METRES_PER_DEGREE_LAT, metres / (_METRES_PER_DEGREE_LON_EQUATOR * cos_lat)


def tower_facts_by_cell(grid_cells, state):
    """把真实铁塔归到它们影响到的 grid cell（网格只是加速索引，塔坐标是权威事实）。

    * 塔坐标**原样**使用，绝不因为工作区层级降低而被聚合/取整；
    * 水平影响范围来自显式配置的 ``tower_clearance_policy.tower_horizontal_clearance_m``；
      未配置时为 0（只影响塔点所在 cell），并在 mask 里显式报 ``not_configured``；
    * 塔顶高程取该 cell 内所有已解析塔的**最大值**（保守方向：塔顶更高 ⇒ 净空更严格）；
    * 只要该 cell 里有任何一塔高度未解析，该 cell 的 tower fact 就是 ``unknown``。
    """

    state = state or {}
    profiles = (state.get("tower_obstacle_profiles") or {}).get("items") or {}
    policy = state.get("tower_clearance_policy") or {}
    horizontal = policy.get("tower_horizontal_clearance_m")

    towers = []
    for tower in ((state.get("towers") or {}).get("items") or []):
        if not isinstance(tower, dict):
            continue
        longitude = tower.get("longitude")
        latitude = tower.get("latitude")
        if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
            continue
        profile = profiles.get(str(tower.get("tower_id") or "")) or {}
        top = profile.get("tower_top_orthometric_m")
        resolved = profile.get("status") == "resolved" and isinstance(top, (int, float))
        half_lat, half_lon = _horizontal_half_degrees(horizontal, latitude)
        towers.append({
            "tower_id": str(tower.get("tower_id") or ""),
            "longitude": float(longitude),
            "latitude": float(latitude),
            "resolved": resolved,
            "top": float(top) if resolved else None,
            "box": (
                float(longitude) - half_lon, float(latitude) - half_lat,
                float(longitude) + half_lon, float(latitude) + half_lat,
            ),
        })
    if not towers:
        return {}

    facts = {}
    for cell in grid_cells or []:
        grid_id = str(cell.get("grid_id") or "")
        bbox = cell.get("bbox")
        if not grid_id or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        counts = None
        for tower in towers:
            west, south, east, north = tower["box"]
            if east < float(bbox[0]) or west > float(bbox[2]):
                continue
            if north < float(bbox[1]) or south > float(bbox[3]):
                continue
            if counts is None:
                counts = {
                    "tower_count": 0, "resolved_count": 0, "unresolved_count": 0,
                    "tower_top_max_egm2008_m": None,
                }
            counts["tower_count"] += 1
            if tower["resolved"]:
                counts["resolved_count"] += 1
                current = counts["tower_top_max_egm2008_m"]
                counts["tower_top_max_egm2008_m"] = (
                    tower["top"] if current is None else max(current, tower["top"])
                )
            else:
                counts["unresolved_count"] += 1
        if counts is not None:
            facts[grid_id] = _tower_fact(counts)
    return facts


class LayeredFeasibilityAdapter:
    """Read-only terrain/building/tower facts for the layered feasibility mask."""
    adapter_id = "layered_feasibility_coarse_envelope_adapter"
    adapter_version = "1.1"

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
            "tower_mapping_method": LAYERED_TOWER_MAPPING_METHOD,
            "tower_obstacle_semantics": TOWER_OBSTACLE_SEMANTICS,
            "read_mode": "single_read_only_window_per_run_read_as_array_no_resample",
            "scope": "coarse_strategic_vertical_envelope",
            "not_exact_footprint": True,
            "not_horizontal_clearance": True,
        }

    def build_cells(self, grid_cells, state):
        """Canonical ``[{grid_id, terrain, buildings, towers}]`` facts for every L8 cell."""

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
        # 真实铁塔：**点几何权威**。这里只把每个塔归到它（按显式水平净空扩展后）相交的
        # grid cell 上，网格仅是加速索引；塔坐标本身从不被网格粗化。
        tower_cells = tower_facts_by_cell(cells, state)
        result = []
        for cell in cells:
            grid_id = str(cell.get("grid_id") or "")
            if not grid_id:
                continue
            result.append({
                "grid_id": grid_id,
                "terrain": _terrain_fact(terrain_facts.get(grid_id)),
                "buildings": _building_fact(building_cells.get(grid_id)),
                "towers": tower_cells.get(grid_id) or _no_tower_fact(),
            })
        return result


def layered_feasibility_source_status(state, terrain_path=None):
    """Read-only readiness summary (never opens a dataset).

    The keys must match what the readiness projection and the frontend actually read:
    ``terrain`` (not only ``terrain_dtm``) plus ``population``.  Reporting the terrain
    status under a key nobody reads made every project look as if the verified FABDEM
    source were missing.
    """

    state = state or {}
    audits = (state.get("source_audits") or {}).get("items") or {}
    buildings = audits.get("buildings") or {}
    building_grid = audits.get("building_grid") or {}
    terrain_audit = audits.get("terrain_dtm") or {}
    terrain_status = str(terrain_audit.get("status") or "")
    terrain_configured = bool(terrain_path)
    terrain_available = terrain_configured and terrain_status == "verified"
    if terrain_available:
        terrain_reason = None
    elif not terrain_configured:
        terrain_reason = "terrain_source_not_configured"
    else:
        terrain_reason = f"terrain_source_audit_{terrain_status or 'unknown'}"
    terrain = {
        "role": "terrain_dtm",
        "configured": terrain_configured,
        "audit_status": terrain_audit.get("status"),
        "available": terrain_available,
        "reason": terrain_reason,
    }

    attribute = (state.get("grid_attributes") or {}).get("population") or {}
    population_status = str(attribute.get("status") or "")
    population_available = population_status == "passed"
    population = {
        "role": "population",
        "configured": bool(attribute),
        "status": attribute.get("status"),
        "value_status": attribute.get("value_status"),
        "coverage_status": attribute.get("coverage_status"),
        "nodata_only_count": attribute.get("nodata_only_count"),
        "confirmed_zero_population_count": attribute.get("confirmed_zero_population_count"),
        "nodata_semantics_status": (attribute.get("nodata_semantics") or {}).get("mode"),
        "available": population_available,
        "reason": None if population_available else (
            "population_attribute_not_calculated" if not attribute
            else str(attribute.get("message") or population_status or "population_unavailable")
        ),
    }

    return {
        # ``terrain`` is the contract the readiness projection and the UI read; ``terrain_dtm``
        # is kept for already-deployed consumers of the older key.
        "terrain": terrain,
        "terrain_dtm": dict(terrain),
        "population": population,
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
    "LAYERED_TOWER_MAPPING_METHOD", "TOWER_FACT_KEYS", "TOWER_OBSTACLE_SEMANTICS",
    "LayeredFeasibilityAdapter", "layered_feasibility_source_status", "tower_facts_by_cell",
]
