"""Source-native evidence assembly for production layered-route validation.

Both the synchronous application path and the background worker construct this same
adapter explicitly.  The factory owns no application context and only reads the frozen
state/source paths supplied by its caller.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path


def runtime_source_identity(path):
    """Return the existing cheap file identity used by source manifests (path/stat)."""

    if not path:
        return {"path": None, "status": "missing"}
    source = Path(path)
    try:
        stat = source.stat()
    except OSError:
        return {"path": str(source), "status": "missing"}
    return {
        "path": str(source.resolve()),
        "status": "available",
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def runtime_source_identities(paths, roles=("terrain_dtm", "buildings")):
    paths = paths if isinstance(paths, dict) else {}
    return {role: runtime_source_identity(paths.get(role)) for role in roles}


def build_layered_validation_evidence_adapter(
    *, state, terrain_dtm_path, buildings_path, horizontal_crs,
):
    """Build the one production validation evidence adapter from explicit inputs."""

    frozen_state = deepcopy(state if isinstance(state, dict) else {})
    terrain_path = str(terrain_dtm_path or "").strip()
    building_path = str(buildings_path or "").strip()
    metric_crs = str(horizontal_crs or "").strip()

    def adapter(
        *, candidate, path, altitude_layer, nominal_altitude_m, policy, payload,
        on_progress=None, cancel_check=None,
    ):
        del candidate, altitude_layer, nominal_altitude_m
        from .fine_environment_adapter import (
            NativeTerrainWindowSource, QgisMetricTransform, RouteCorridorBuildingSource,
        )
        from .planning_constraint_field_adapter import restricted_area_continuous_evidence
        from ..route_planner_v3.continuous_raster_window import resolve_native_pixel_intervals

        if not terrain_path or not building_path:
            raise ValueError("请先配置 verified FABDEM terrain_dtm 与 buildings GeoPackage")
        if not metric_crs:
            raise ValueError("production validation 需要显式 horizontal_crs（米制 CRS）")
        progress = on_progress if callable(on_progress) else lambda *_args: None
        cancel = cancel_check if callable(cancel_check) else lambda: None
        transform = QgisMetricTransform(metric_crs)
        metric_path = []
        for point in path or []:
            converted = transform.to_metric([float(point[0]), float(point[1])])
            if converted is None:
                raise ValueError("candidate CRS84 path 无法投影到显式 metric CRS")
            metric_path.append([float(converted[0]), float(converted[1])])
        route = {"horizontal_geometry": {"linearized": {
            "linestring_metric": metric_path, "curve_chord_error_m": 0.0,
        }}}
        terrain = NativeTerrainWindowSource(terrain_path)
        buildings = RouteCorridorBuildingSource(building_path, crs_authority=metric_crs)
        progress(0.10, "正在加载地形与建筑证据")
        terrain_evidence = terrain.native_window(
            transform=transform, metric_line=metric_path,
            envelope_radius_m=0.0, spacing_m=None,
        )
        terrain_evidence["pixels"] = resolve_native_pixel_intervals(
            route, terrain_evidence.get("pixels") or [], curve_chord_error_m=0.0,
        )
        building_evidence = buildings.query_route(
            route, transform=transform,
            horizontal_clearance_m=policy["building_horizontal_clearance_m"],
            curve_error_m=0.0, terrain_source=terrain,
        )
        cancel()
        tower_collection = frozen_state.get("tower_obstacle_profiles") or {}
        tower_items = []
        for profile in (tower_collection.get("items") or {}).values():
            if not isinstance(profile, dict):
                continue
            point = transform.to_metric([profile.get("longitude"), profile.get("latitude")])
            if point is not None:
                tower_items.append({
                    **deepcopy(profile),
                    "point_metric": [float(point[0]), float(point[1])],
                })
        tower_evidence = {
            "status": "passed" if tower_collection.get("status") == "passed" else "unresolved",
            "applicability": "applicable", "towers": tower_items,
            "source": deepcopy(tower_collection.get("source")),
        }
        request = payload if isinstance(payload, dict) else {}
        raw_restricted = (
            request["restricted_areas"]
            if "restricted_areas" in request else frozen_state.get("restricted_areas")
        )
        restricted_evidence = (
            restricted_area_continuous_evidence(raw_restricted, transform=transform)
            if raw_restricted is not None else {
                "status": "unresolved", "applicability": "applicable", "areas": [],
                "source": None,
            }
        )
        return {
            "adapter_id": "layered_candidate_real_source_validation_adapter_v1",
            "source_type": "configured_real_sources",
            "metric_path": metric_path, "metric_crs": metric_crs,
            "to_geographic": transform.to_geographic,
            "terrain": terrain_evidence, "buildings": building_evidence,
            "towers": tower_evidence, "restricted_areas": restricted_evidence,
            "sample_count": len(terrain_evidence.get("pixels") or []) + len(
                building_evidence.get("buildings") or []
            ) + len(tower_items) + len(restricted_evidence.get("areas") or []),
            "sources": {
                "terrain_dtm": terrain.describe(), "buildings": buildings.describe(),
                "towers": {
                    "collection_id": tower_collection.get("collection_id"),
                    "status": tower_collection.get("status"),
                    "count": tower_collection.get("count"),
                    "confirmed_count": tower_collection.get("confirmed_count"),
                },
                "restricted_areas": deepcopy(restricted_evidence.get("source")),
                "metric_frame": transform.describe(),
            },
            "airspace": {
                "status": "not_applicable", "applicability": "display_only",
                "used_in_validation": False,
            },
        }

    return adapter


__all__ = [
    "build_layered_validation_evidence_adapter",
    "runtime_source_identities", "runtime_source_identity",
]
