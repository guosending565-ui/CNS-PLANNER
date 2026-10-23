"""Read-only RTree adapter for Radar building LOS diagnostics."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

from ..domain import radar_building_los as building_los
from ..domain.building_clearance import building_height_value_is_usable
from ..domain.radar_terrain_los import LOS_STATUS_UNKNOWN, number_or_none, probe_geometry

DEFAULT_MAX_FEATURES_PER_RAY = 256
DEFAULT_MAX_RAYS = 1024
DEFAULT_MAX_QUERIES = 1024
DEFAULT_MAX_ELAPSED_SECONDS = 30.0
BUILDING_QUERY_MODE = "provider_rtree_whole_tower_probe_ray_bbox_then_exact_intersection"


def _identity_key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class BuildingFactCache:
    """Process-local cache bound to building, height and DEM source identities."""

    def __init__(self):
        self._items = {}

    def get(self, identity):
        value = self._items.get(_identity_key(identity))
        return deepcopy(value) if value is not None else None

    def put(self, identity, value):
        self._items[_identity_key(identity)] = deepcopy(value)

    def __len__(self):
        return len(self._items)


def _metric_point(record, transform, *, longitude_key="longitude", latitude_key="latitude"):
    metric = record.get("metric")
    if isinstance(metric, (list, tuple)) and len(metric) >= 2:
        x, y = number_or_none(metric[0]), number_or_none(metric[1])
        if x is not None and y is not None:
            return [x, y]
    lon, lat = number_or_none(record.get(longitude_key)), number_or_none(record.get(latitude_key))
    if lon is None or lat is None or transform is None or not hasattr(transform, "to_metric"):
        return None
    converted = transform.to_metric([lon, lat])
    if converted is None:
        return None
    return [float(converted[0]), float(converted[1])]


def _unknown(reason, *, tower, probe, geometry=None, completion="incomplete",
             gate=None, status=LOS_STATUS_UNKNOWN):
    return {
        "tower_id": tower.get("tower_id"),
        "probe_id": probe.get("probe_id") or probe.get("voxel_id") or probe.get("grid_id"),
        "status": status,
        "engineering_estimate_status": LOS_STATUS_UNKNOWN,
        "reason": reason,
        "computation_completion": completion,
        "evidence_completion": "incomplete",
        "probe_geometry": geometry,
        "obstruction_hits": [],
        "candidate_building_count": 0,
        "exact_intersection_count": 0,
        "input_fingerprint": None,
        "ray_model_id": building_los.RAY_MODEL_ID,
        "geometry_gate": deepcopy(gate),
        # ``within_frozen_device_range`` may only ever be true after the frozen device
        # geometry was actually resolved and the ray passed it; everything else is false.
        "within_frozen_device_range": bool(
            gate is not None and gate.get("within_frozen_device_range")
        ),
        "device_geometry": None if gate is None else deepcopy(gate.get("device_geometry")),
        "building_los_physical_unknown": False,
        "is_detection_probability": False,
    }


def _resolve_probe_height(probe, *, height_key="egm2008_m"):
    """Resolve the probe's explicit EGM2008 height and disclose which field carried it.

    The authoritative height chain (``probe_height`` / altitude-layer catalog) stays
    outside this read-only adapter: the caller must hand over an already resolved
    EGM2008 orthometric height.  ``altitude_egm2008_m`` is the resolved form; a canonical
    route sample carries the same quantity as ``egm2008_m``.
    """

    altitude = number_or_none(probe.get("altitude_egm2008_m"))
    source_field = "altitude_egm2008_m" if altitude is not None else None
    if altitude is None:
        altitude = number_or_none(probe.get(height_key))
        source_field = height_key if altitude is not None else None
    if altitude is None:
        altitude = number_or_none(probe.get("egm2008_m"))
        source_field = "egm2008_m" if altitude is not None else None
    if altitude is None:
        return "probe_height_evidence_missing", probe, None
    return None, {**probe, "altitude_egm2008_m": altitude}, {
        "probe_height_field": source_field,
        "probe_height_vertical_reference": building_los.EGM2008_ORTHOMETRIC,
        "probe_height_resolved_outside_adapter": True,
        "representative_height_used": False,
    }


def evaluate_probe_building_los(
    tower, probe, *, radar_type, building_source, transform, terrain_source,
    max_features=DEFAULT_MAX_FEATURES_PER_RAY,
    earth_radius_m=None, k_factor=1.0, probe_height_key="egm2008_m",
):
    """Evaluate one ray after the frozen Radar device geometry gate.

    ``radar_type`` is mandatory: ``within_frozen_device_range`` may only be declared when
    the frozen device geometry of that type is actually available and the ray passes the
    inclusive slant range *and* the elevation interval.  A combination the device
    geometry already excludes returns its own filtered status without querying buildings
    or opening DEM.
    """

    tower = tower if isinstance(tower, dict) else {}
    probe = probe if isinstance(probe, dict) else {}
    origin_metric = _metric_point(tower, transform)
    target_metric = _metric_point(probe, transform)
    origin_z = number_or_none(tower.get("origin_egm2008_m"))
    height_reason, probe, height_evidence = _resolve_probe_height(
        probe, height_key=probe_height_key,
    )
    target_z = number_or_none(probe.get("altitude_egm2008_m"))
    geometry = probe_geometry(
        origin_egm2008_m=origin_z, target_egm2008_m=target_z,
        origin_metric=origin_metric, target_metric=target_metric,
        earth_radius_m=earth_radius_m, k_factor=k_factor,
    )
    if height_reason is not None:
        return _unknown(height_reason, tower=tower, probe=probe, geometry=geometry)
    gate = building_los.frozen_geometry_gate(
        radar_type=radar_type, origin_metric=origin_metric, target_metric=target_metric,
        origin_egm2008_m=origin_z, target_egm2008_m=target_z,
    )
    status = gate["status"]
    if status == building_los.BUILDING_LOS_GATE_GEOMETRY_UNRESOLVED:
        return _unknown(
            str(gate.get("reason") or "geometry_inputs_incomplete"),
            tower=tower, probe=probe, geometry=geometry, gate=gate,
        )
    if status == building_los.BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE:
        return _unknown(
            "outside_frozen_device_range", tower=tower, probe=probe, geometry=geometry,
            completion="not_evaluated", gate=gate,
            status=building_los.GATE_OUTSIDE_STATUS,
        )
    if status == building_los.BUILDING_LOS_GATE_OUTSIDE_ELEVATION_INTERVAL:
        return _unknown(
            "outside_frozen_device_elevation_interval", tower=tower, probe=probe,
            geometry=geometry, completion="not_evaluated", gate=gate,
            status=building_los.GATE_GEOMETRY_FILTERED_STATUS,
        )
    if building_source is None or not hasattr(building_source, "query_ray_bbox"):
        return _unknown(
            "building_source_unavailable", tower=tower, probe=probe, geometry=geometry,
            gate=gate,
        )
    bbox = [
        min(origin_metric[0], target_metric[0]), min(origin_metric[1], target_metric[1]),
        max(origin_metric[0], target_metric[0]), max(origin_metric[1], target_metric[1]),
    ]
    started = time.perf_counter()
    query = building_source.query_ray_bbox(
        bbox, transform=transform, terrain_source=terrain_source,
        max_features=max(1, int(max_features)),
    )
    elapsed = time.perf_counter() - started
    receipt = dict(query.get("query_receipt") or {})
    receipt.update({
        "ray_bbox_metric": bbox, "elapsed_seconds": elapsed,
        "max_features": max(1, int(max_features)), "query_mode": BUILDING_QUERY_MODE,
        "query_complete": bool(query.get("query_complete")),
        "query_reason": query.get("query_reason"),
    })
    result = building_los.evaluate_building_los(
        origin_metric=origin_metric, target_metric=target_metric,
        origin_egm2008_m=origin_z, target_egm2008_m=target_z,
        buildings=query.get("buildings") or [],
        query_complete=bool(query.get("query_complete")),
        query_reason=query.get("query_reason"), query_receipt=receipt,
        query_policy={
            "ray_bbox_metric": bbox, "max_features": max(1, int(max_features)),
            "query_mode": BUILDING_QUERY_MODE,
        },
        building_source_identity=building_source.describe(),
        dem_source_identity=(
            terrain_source.describe() if terrain_source is not None
            and hasattr(terrain_source, "describe") else None
        ),
        earth_radius_m=earth_radius_m, k_factor=k_factor,
        route_probe_identity={
            "tower_id": tower.get("tower_id"),
            "probe_id": probe.get("probe_id") or probe.get("voxel_id") or probe.get("grid_id"),
        },
        radar_geometry=deepcopy(gate.get("device_geometry")),
    )
    result.update({
        "tower_id": tower.get("tower_id"),
        "probe_id": probe.get("probe_id") or probe.get("voxel_id") or probe.get("grid_id"),
        "within_frozen_device_range": True,
        "geometry_gate": deepcopy(gate),
        "device_geometry": deepcopy(gate.get("device_geometry")),
        "probe_height_evidence": deepcopy(height_evidence),
        "building_los_physical_unknown": result.get("status") == LOS_STATUS_UNKNOWN,
    })
    return result


class BuildingLosExecutionContext:
    """Bounded per-request execution with building-only counters."""

    def __init__(
        self, *, building_source, terrain_source, transform,
        max_rays=DEFAULT_MAX_RAYS, max_queries=DEFAULT_MAX_QUERIES,
        max_features_per_ray=DEFAULT_MAX_FEATURES_PER_RAY,
        max_elapsed_seconds=DEFAULT_MAX_ELAPSED_SECONDS,
    ):
        self.building_source = building_source
        self.terrain_source = terrain_source
        self.transform = transform
        self.max_rays = max(1, int(max_rays))
        self.max_queries = max(1, int(max_queries))
        self.max_features_per_ray = max(1, int(max_features_per_ray))
        self.max_elapsed_seconds = max(0.0, float(max_elapsed_seconds))
        self.ray_count = 0
        self.query_count = 0
        self.candidate_count = 0
        self.exact_intersection_count = 0
        self.elapsed_seconds = 0.0
        self.budget_exhausted = False
        self.geometry_filtered_count = 0
        self.geometry_filtered_status_counts = {
            building_los.GATE_OUTSIDE_STATUS: 0,
            building_los.GATE_GEOMETRY_FILTERED_STATUS: 0,
        }
        self.geometry_unresolved_count = 0

    def evaluate(self, tower, probe, *, radar_type, earth_radius_m=None, k_factor=1.0):
        if (self.ray_count >= self.max_rays or self.query_count >= self.max_queries
                or self.elapsed_seconds >= self.max_elapsed_seconds):
            self.budget_exhausted = True
            return _unknown("building_los_budget_exhausted", tower=tower, probe=probe)
        started = time.perf_counter()
        result = evaluate_probe_building_los(
            tower, probe, radar_type=radar_type,
            building_source=self.building_source,
            transform=self.transform, terrain_source=self.terrain_source,
            max_features=self.max_features_per_ray,
            earth_radius_m=earth_radius_m, k_factor=k_factor,
        )
        status = str(result.get("status") or "")
        if status in self.geometry_filtered_status_counts:
            # Filtered combinations never consulted buildings and never opened DEM, so
            # they must not consume the building ray/query budget or count as physical
            # building-LOS unknown.
            self.geometry_filtered_count += 1
            self.geometry_filtered_status_counts[status] += 1
        elif status == LOS_STATUS_UNKNOWN and result.get("geometry_gate") is None:
            self.geometry_unresolved_count += 1
        else:
            self.ray_count += 1
            self.query_count += 1
            self.candidate_count += int(result.get("candidate_building_count") or 0)
            self.exact_intersection_count += int(result.get("exact_intersection_count") or 0)
        self.elapsed_seconds += time.perf_counter() - started
        return result

    def receipt(self):
        return {
            "query_mode": BUILDING_QUERY_MODE,
            "ray_count": self.ray_count, "query_count": self.query_count,
            "candidate_building_count": self.candidate_count,
            "exact_intersection_count": self.exact_intersection_count,
            "elapsed_seconds": self.elapsed_seconds,
            "max_rays": self.max_rays, "max_queries": self.max_queries,
            "max_features_per_ray": self.max_features_per_ray,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "budget_exhausted": self.budget_exhausted,
            "geometry_filtered_count": self.geometry_filtered_count,
            "geometry_filtered_status_counts": dict(self.geometry_filtered_status_counts),
            "geometry_unresolved_count": self.geometry_unresolved_count,
            "computation_completion": "incomplete" if self.budget_exhausted else "complete",
        }


def _polygon_exteriors(geometry):
    kind = str((geometry or {}).get("type") or "")
    coordinates = (geometry or {}).get("coordinates") or []
    if kind == "Polygon":
        return [coordinates[0]] if coordinates else []
    if kind == "MultiPolygon":
        return [polygon[0] for polygon in coordinates if polygon]
    return []


class QgisBuildingLosSource:
    """GeoPackage provider/RTree source; all repair and caching stay in memory."""

    role = "buildings"

    def __init__(
        self, path, *, layer_name="buildings", metric_crs="EPSG:32651",
        height_estimate_field="height_estimate_m",
        height_evidence_field="height_evidence_level", id_field="fid",
        cache=None,
    ):
        from qgis.core import QgsCoordinateReferenceSystem, QgsVectorLayer

        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ValueError("buildings v3 GeoPackage 不存在")
        self.layer_name = str(layer_name)
        self.layer = QgsVectorLayer(
            f"{self.path}|layername={self.layer_name}", "Radar buildings v3", "ogr",
        )
        if not self.layer.isValid():
            raise ValueError("buildings v3 图层不可读")
        fields = {field.name() for field in self.layer.fields()}
        for required in (height_estimate_field, height_evidence_field):
            if required not in fields:
                raise ValueError(f"buildings v3 缺少字段 {required}")
        self.height_estimate_field = height_estimate_field
        self.height_evidence_field = height_evidence_field
        self.id_field = id_field if id_field in fields else None
        self.derived_assumption_field = "derived_assumption" if "derived_assumption" in fields else None
        self.metric_crs = QgsCoordinateReferenceSystem(metric_crs)
        self.cache = cache or BuildingFactCache()
        self._to_metric = None
        self._from_wgs84 = None

    def describe(self):
        stat = self.path.stat()
        return {
            "role": self.role, "file_name": self.path.name,
            "path": str(self.path), "layer": self.layer_name,
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "feature_count": int(self.layer.featureCount()),
            "crs": self.layer.crs().authid(),
            "spatial_index_available": bool(self.layer.hasSpatialIndex()),
            "query_mode": BUILDING_QUERY_MODE,
            "height_estimate_field": self.height_estimate_field,
            "height_evidence_field": self.height_evidence_field,
            "source_version": "round35_buildings_v3",
        }

    def query_ray_bbox(self, metric_bbox, *, transform, terrain_source, max_features):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
            QgsGeometry, QgsProject, QgsRectangle,
        )

        if not bool(self.layer.hasSpatialIndex()):
            return {
                "buildings": [], "query_complete": False,
                "query_reason": "building_source_spatial_index_missing",
                "query_receipt": {
                    "mode": BUILDING_QUERY_MODE, "provider_filter_rect": False,
                    "spatial_index_available": False, "candidate_count": 0,
                    "returned_count": 0, "truncated": True,
                },
            }
        context = QgsProject.instance().transformContext()
        if self._to_metric is None:
            self._to_metric = QgsCoordinateTransform(self.layer.crs(), self.metric_crs, context)
        if self._from_wgs84 is None:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            self._from_wgs84 = QgsCoordinateTransform(wgs84, self.layer.crs(), context)
        west, south, east, north = (float(item) for item in metric_bbox)
        corners = [transform.to_geographic([west, south]), transform.to_geographic([east, north])]
        if any(item is None for item in corners):
            return {"buildings": [], "query_complete": False,
                    "query_reason": "metric_transform_unavailable", "query_receipt": {}}
        rectangle = QgsRectangle(
            min(corners[0][0], corners[1][0]), min(corners[0][1], corners[1][1]),
            max(corners[0][0], corners[1][0]), max(corners[0][1], corners[1][1]),
        )
        if self.layer.crs().authid() != "EPSG:4326":
            rectangle = self._from_wgs84.transformBoundingBox(rectangle)
        request = QgsFeatureRequest().setFilterRect(rectangle).setLimit(int(max_features) + 1)
        features = list(self.layer.getFeatures(request))
        complete = len(features) <= int(max_features)
        features = features[: int(max_features)]
        source_identity = self.describe()
        dem_identity = (
            terrain_source.describe() if terrain_source is not None
            and hasattr(terrain_source, "describe") else None
        )
        buildings = []
        cache_hits = 0
        for feature in features:
            identifier = str(feature[self.id_field]) if self.id_field else str(feature.id())
            estimate = number_or_none(feature[self.height_estimate_field])
            if not building_height_value_is_usable(estimate):
                estimate = None
            evidence_level = str(feature[self.height_evidence_field] or "")
            derived_assumption = (
                str(feature[self.derived_assumption_field] or "")
                if self.derived_assumption_field else None
            )
            cache_identity = {
                "building_source": source_identity,
                "building_id": identifier,
                "height_source": {
                    "field": self.height_estimate_field, "value": estimate,
                    "evidence_field": self.height_evidence_field, "level": evidence_level,
                    "derived_assumption": derived_assumption,
                },
                "dem_source": dem_identity,
            }
            cached = self.cache.get(cache_identity)
            if cached is not None:
                cache_hits += 1
                buildings.append(cached)
                continue
            source_geometry = feature.geometry()
            if source_geometry is None or source_geometry.isEmpty():
                buildings.append({
                    "building_id": identifier, "geometry_metric": None,
                    "height_estimate_m": estimate, "height_evidence_level": evidence_level,
                    "derived_assumption": derived_assumption,
                    "ground_elevation_egm2008_m": None,
                    "source_version": source_identity["source_version"],
                })
                continue
            metric_geometry = QgsGeometry(source_geometry)
            metric_geometry.transform(self._to_metric)
            geometry_mapping = json.loads(metric_geometry.asJson())
            exteriors = _polygon_exteriors(geometry_mapping)
            ground = None
            if exteriors and terrain_source is not None:
                sampler = getattr(terrain_source, "sample_footprint_ground_parts", None)
                if callable(sampler):
                    grounds = sampler(exteriors, transform=transform)
                else:
                    single = getattr(terrain_source, "sample_footprint_ground", None)
                    grounds = [single(ring, transform=transform) for ring in exteriors] if callable(single) else []
                if grounds and all(number_or_none(value) is not None for value in grounds):
                    ground = max(float(value) for value in grounds)
            record = {
                "building_id": identifier, "geometry_metric": geometry_mapping,
                "height_estimate_m": estimate, "height_evidence_level": evidence_level,
                "derived_assumption": derived_assumption,
                "ground_elevation_egm2008_m": ground,
                "source_version": source_identity["source_version"],
            }
            self.cache.put(cache_identity, record)
            buildings.append(record)
        return {
            "buildings": buildings, "query_complete": complete,
            "query_reason": None if complete else "max_features_reached",
            "query_receipt": {
                "mode": BUILDING_QUERY_MODE, "provider_filter_rect": True,
                "spatial_index_available": bool(self.layer.hasSpatialIndex()),
                "candidate_count": len(features), "returned_count": len(buildings),
                "cache_hit_count": cache_hits, "max_features": int(max_features),
                "truncated": not complete,
            },
        }


__all__ = [
    "BUILDING_QUERY_MODE", "DEFAULT_MAX_ELAPSED_SECONDS", "DEFAULT_MAX_FEATURES_PER_RAY", "DEFAULT_MAX_QUERIES",
    "DEFAULT_MAX_RAYS", "BuildingFactCache", "BuildingLosExecutionContext",
    "QgisBuildingLosSource", "evaluate_probe_building_los",
]
