"""Synthetic canonical environments for V3-A tests and experiments.

V3-A deliberately ships **no** 30 m refinement and **no** real terrain/building
adapter.  The algorithm therefore only ever runs against a canonical
:class:`V3CellEnvironment`; this module builds such an environment from an
*explicit* synthetic specification so nothing is inferred from a file or a layer
name.

Everything produced here is labelled ``source_type=synthetic``.  Real-data
readiness stays ``blocked`` until an adapter supplies audited evidence.
"""

from __future__ import annotations

from copy import deepcopy

from ..benchmark.geodesy import geodesic_distance_m
from .motion import derive_grid_index

#: Deterministic pseudo-noise constants (a fixed LCG, not a random module call).
_LCG_MODULUS = 2147483647
_LCG_MULTIPLIER = 1103515245
_LCG_INCREMENT = 12345

TERRAIN_PROFILES = ("flat", "ridge_longitude", "ridge_latitude", "longitude_bands", "rough")
BUILDING_PROFILES = ("none", "single_block", "cluster")

SYNTHETIC_SPEC_DEFAULT = {
    "profile_id": "synthetic_open_flat",
    "terrain_profile": "flat",
    "base_surface_elevation_m": 0.0,
    "terrain_relative_amplitude_m": 0.0,
    "ridge_longitude_band": None,
    "ridge_latitude_band": None,
    "ridge_height_m": 0.0,
    "longitude_bands": [],
    "buildings_profile": "none",
    "building_height_m": 0.0,
    "building_count": 0,
    "building_cells": [],
    "restricted_cells": [],
    "unknown_terrain_cells": [],
    "unknown_building_cells": [],
    "population_per_cell": {},
    "traffic_per_cell": {},
    "seed": 1,
}


def normalize_synthetic_spec(value=None):
    spec = deepcopy(SYNTHETIC_SPEC_DEFAULT)
    source = value if isinstance(value, dict) else {}
    for key in SYNTHETIC_SPEC_DEFAULT:
        if key in source and source[key] is not None:
            spec[key] = deepcopy(source[key])
    if spec["terrain_profile"] not in TERRAIN_PROFILES:
        raise ValueError(f"未知 terrain_profile：{spec['terrain_profile']}")
    if spec["buildings_profile"] not in BUILDING_PROFILES:
        raise ValueError(f"未知 buildings_profile：{spec['buildings_profile']}")
    if spec["ridge_longitude_band"] is not None:
        band = [float(item) for item in spec["ridge_longitude_band"]]
        if len(band) != 2 or band[0] > band[1]:
            raise ValueError("ridge_longitude_band 必须是 [min, max]")
        spec["ridge_longitude_band"] = band
    if spec["ridge_latitude_band"] is not None:
        band = [float(item) for item in spec["ridge_latitude_band"]]
        if len(band) != 2 or band[0] > band[1]:
            raise ValueError("ridge_latitude_band 必须是 [min, max]")
        spec["ridge_latitude_band"] = band
    spec["buildings_profile"] = str(spec["buildings_profile"])
    bands = []
    for raw in spec["longitude_bands"] or []:
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError("longitude_bands 每项必须是 [min_lon, max_lon, surface_elevation_m]")
        low, high, elevation = (float(raw[0]), float(raw[1]), float(raw[2]))
        if low > high:
            raise ValueError("longitude_bands 的 min_lon 不得大于 max_lon")
        bands.append([low, high, elevation])
    spec["longitude_bands"] = sorted(bands, key=lambda item: item[0])
    spec["building_cells"] = [str(item) for item in spec["building_cells"] or []]
    spec["restricted_cells"] = [str(item) for item in spec["restricted_cells"] or []]
    spec["unknown_terrain_cells"] = [str(item) for item in spec["unknown_terrain_cells"] or []]
    spec["unknown_building_cells"] = [str(item) for item in spec["unknown_building_cells"] or []]
    spec["population_per_cell"] = {
        str(key): float(value) for key, value in (spec["population_per_cell"] or {}).items()
    }
    spec["traffic_per_cell"] = {
        str(key): float(value) for key, value in (spec["traffic_per_cell"] or {}).items()
    }
    # Backwards-compatible alias: an unspecified channel is treated as unknown
    # terrain evidence, which is the conservative reading.
    legacy_unknown = source.get("unknown_cells")
    if legacy_unknown and not spec["unknown_terrain_cells"]:
        spec["unknown_terrain_cells"] = [str(item) for item in legacy_unknown]
    spec["seed"] = int(spec["seed"])
    return spec


def build_synthetic_environment(grid_cells, policy, spec=None, *, source_detail=None):
    """Build a canonical environment from explicit canonical grid cells.

    ``grid_cells`` are the project's MH/T cells (``grid_id``/``bbox``/``center``
    plus optional ``column``/``row``/``level``).  The spec decides every terrain
    and building value; no value is derived from real data.
    """

    spec = normalize_synthetic_spec(spec)
    cells = list(grid_cells or [])
    if not cells:
        raise ValueError("构建 synthetic V3 环境需要非空 MH/T grid cells")
    terrain_clearance = _required(policy, "terrain_clearance_m")
    vertical_clearance = _required(policy, "building_vertical_clearance_m")
    horizontal_clearance = _required(policy, "building_horizontal_clearance_m")
    cells = _apply_cell_bounds_index(cells)
    unknown_terrain = {str(item) for item in spec["unknown_terrain_cells"]}
    unknown_building = {str(item) for item in spec["unknown_building_cells"]}
    restricted = {str(item) for item in spec["restricted_cells"]}
    building_targets = _building_targets(cells, spec)
    result_cells = []
    for index, cell in enumerate(cells):
        grid_id = str(cell["grid_id"])
        center = [float(cell["center"][0]), float(cell["center"][1])]
        surface = _surface_elevation(spec, center, index, grid_id)
        # The three evidence channels are independent: an unknown terrain cell is
        # still a confirmed airspace decision, and vice versa.
        terrain_status = "unknown" if grid_id in unknown_terrain else "passed"
        airspace_status = (
            "confirmed_restricted" if grid_id in restricted else "confirmed_allowed"
        )
        building_status = "unknown" if grid_id in unknown_building else "passed"
        roof = None
        if building_status == "passed" and grid_id in building_targets:
            roof = surface + float(spec["building_height_m"])
        result_cells.append({
            "grid_id": grid_id,
            "center": center,
            "cell_size_m": _cell_size_m(cell),
            "column": cell.get("column"),
            "row": cell.get("row"),
            "level": cell.get("level"),
            "bbox": deepcopy(cell.get("bbox")),
            "terrain": {
                "data_status": terrain_status,
                "surface_elevation_max_egm2008_m": surface,
                "surface_clearance_egm2008_m": surface + terrain_clearance,
            },
            "buildings": {
                "data_status": building_status,
                "roof_elevation_max_egm2008_m": roof,
                "required_clearance_egm2008_m": (
                    None if roof is None else roof + vertical_clearance
                ),
                "horizontal_clearance_m": horizontal_clearance,
            },
            "airspace": {"status": airspace_status, "feature_id": f"synthetic:{grid_id}"},
        })
    return {
        "schema_version": "3.0-environment",
        "status": "passed",
        "reason": None,
        "canonical_vertical_reference": "egm2008_orthometric",
        "source_type": "synthetic",
        "source_detail": {
            "builder": "route_planner_v3.synthetic.build_synthetic_environment",
            "spec": deepcopy(spec),
            "not_real_data": True,
            "no_30m_refinement_implemented": True,
            **(source_detail or {}),
        },
        "grid_level": _grid_level(cells),
        "properties": {
            "cell_size_m": _median_cell_size(cells),
            "terrain_clearance_m": terrain_clearance,
            "building_horizontal_clearance_m": horizontal_clearance,
            "building_vertical_clearance_m": vertical_clearance,
            "population_per_cell": {
                str(cell["grid_id"]): float((spec.get("population_per_cell") or {}).get(str(cell["grid_id"]), 0.0))
                for cell in cells
            },
            "traffic_per_cell": {
                str(cell["grid_id"]): float((spec.get("traffic_per_cell") or {}).get(str(cell["grid_id"]), 0.0))
                for cell in cells
            },
            "unknown_terrain_cell_count": len(unknown_terrain),
            "unknown_building_cell_count": len(unknown_building),
            "restricted_cell_count": len(restricted),
        },
        "cells": result_cells,
    }


def open_cell_ids(cells):
    return [str(cell["grid_id"]) for cell in cells or []]


# --------------------------------------------------------------------------- internals


def _required(policy, name):
    value = (policy or {}).get(name)
    if value is None:
        raise ValueError(f"构建 canonical 环境需要显式 policy.{name}")
    return float(value)


def _apply_cell_bounds_index(cells):
    index = derive_grid_index({str(cell["grid_id"]): cell for cell in cells})
    result = []
    for cell in cells:
        entry = dict(cell)
        derived = index.get(str(cell["grid_id"]))
        if derived is not None:
            entry["level"], entry["column"], entry["row"] = derived
        result.append(entry)
    return result


def _median_cell_size(cells):
    sizes = sorted(_cell_size_m(cell) for cell in cells if _cell_size_m(cell) is not None)
    if not sizes:
        return None
    return sizes[len(sizes) // 2]


def _cell_size_m(cell):
    explicit = cell.get("cell_size_m")
    if isinstance(explicit, (int, float)) and float(explicit) > 0:
        return float(explicit)
    bbox = cell.get("bbox")
    if not bbox:
        return None
    center = cell.get("center") or [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
    return round(geodesic_distance_m([bbox[0], center[1]], [bbox[2], center[1]]), 6)


def _grid_level(cells):
    levels = {int(cell.get("level") or 0) for cell in cells if cell.get("level") is not None}
    return next(iter(levels)) if len(levels) == 1 else None


def _surface_elevation(spec, center, index, grid_id):
    base = float(spec["base_surface_elevation_m"])
    profile = spec["terrain_profile"]
    if profile == "flat":
        return round(base, 6)
    if profile == "ridge_longitude":
        band = spec["ridge_longitude_band"] or [center[0], center[0]]
        if band[0] <= center[0] <= band[1]:
            return round(base + float(spec["ridge_height_m"]), 6)
        return round(base, 6)
    if profile == "ridge_latitude":
        band = spec["ridge_latitude_band"] or [center[1], center[1]]
        if band[0] <= center[1] <= band[1]:
            return round(base + float(spec["ridge_height_m"]), 6)
        return round(base, 6)
    if profile == "longitude_bands":
        for low, high, elevation in spec["longitude_bands"]:
            if low <= center[0] <= high:
                return round(elevation, 6)
        return round(base, 6)
    amplitude = float(spec["terrain_relative_amplitude_m"])
    if amplitude == 0:
        return round(base, 6)
    noise = _lcg_unit(index, spec["seed"], grid_id)
    return round(base + amplitude * noise, 6)


def _lcg_unit(index, seed, grid_id):
    value = (int(seed) * 7919 + index * 104729 + sum(ord(char) for char in str(grid_id))) % _LCG_MODULUS
    value = (value * _LCG_MULTIPLIER + _LCG_INCREMENT) % _LCG_MODULUS
    return value / float(_LCG_MODULUS)


def _building_targets(cells, spec):
    profile = spec["buildings_profile"]
    explicit = {str(item) for item in spec["building_cells"] or []}
    if profile == "none":
        return explicit
    if profile == "single_block":
        return explicit | set(_first_cells(cells, 1))
    count = max(int(spec["building_count"] or 0), 1)
    return explicit | set(_first_cells(cells, count))


def _first_cells(cells, count):
    """Deterministic lowest-index cells (sorted by column then row then grid_id)."""

    def key(cell):
        return (
            cell.get("column") if cell.get("column") is not None else 0,
            cell.get("row") if cell.get("row") is not None else 0,
            str(cell["grid_id"]),
        )

    return [str(cell["grid_id"]) for cell in sorted(cells, key=key)[:max(0, int(count))]]


__all__ = [
    "TERRAIN_PROFILES", "BUILDING_PROFILES", "SYNTHETIC_SPEC_DEFAULT",
    "normalize_synthetic_spec", "build_synthetic_environment", "open_cell_ids",
]
