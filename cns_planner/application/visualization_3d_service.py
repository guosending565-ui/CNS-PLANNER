"""只读三维可视化投影服务（``visualization_3d_service``）。

定位：**一个只读投影层**，不是第二套规划服务。硬边界（违反即返工）：

1. **零写入者**：不 import ``production_write_authority``、不注册失效入口、
   不写 artifact、不调用任何 ``*.save()``、不递增 ``revision``；
2. **不重跑算法**：不调用 Theta*/MILP/P14/P17/CNS 覆盖；几何只做**只读重采样**；
3. **不读** ``GET /api/workflow``（单次 147 MB）：只读顶层容器 + 专用只读适配器；
4. **不伪造权威**：``stale`` / ``not_calculated`` / ``unknown`` 一律显式标注，
   工程演示（``engineering_demo_only``）绝不伪装成严格通过；
5. **里程与几何身份绑定**：长度只与其来源 ``path`` 同容器成对出现；
   不同航路几何**禁止**共用里程段（见 ``docs`` 与 ``route_geometry_identity``）。

本模块不 import QGIS；地形与 geoid 一律经 ``gis/`` 适配器只读访问。
"""

from __future__ import annotations

import math
import struct
import threading
from datetime import datetime, timezone

from ..domain.geodesy import distance_m
from ..domain.cns_service_contract import RID_FORBIDDEN_GEOMETRY, RID_RADIUS_BY_SURFACE
from ..domain.layered_route import stable_fingerprint

SCHEMA_VERSION = "visualization-3d/v1"

#: 水平里程的唯一 canonical 口径（与 ``domain/geodesy.distance_m`` 一致）。
LENGTH_METRIC = "horizontal_equirectangular_2d_wgs84_6371008.8"
LENGTH_IMPLEMENTATION = "cns_planner/domain/geodesy.py:11 distance_m"

#: 三维端点为后续模块预留的对象 ID 前缀（二维/三维必须同源，禁止分叉命名）。
OBJECT_ID_PREFIXES = {
    "route": "route:",
    "route_geometry": "route-geometry:",
    "tower": "tower:",
    "device": "device:",
    "facility": "facility:",
    "communication_volume": "comm-volume:",
    "rid_footprint": "rid-footprint:",
    "radar_panel": "radar-panel:",
    "protection_corridor": "protection-corridor:",
    "simulated_intruder": "sim-intruder:",
    "terrain_window": "terrain-window:",
}

#: 单次响应硬上限（有界分块）。
MAX_ROUTE_POINTS = 4000
MAX_TOWERS = 2000
MAX_DEVICES = 500
MAX_TERRAIN_CELLS = 1024 * 1024


def _items(container):
    """统一取出容器条目（``{"items": [...]}`` / ``{"items": {...}}`` / 裸列表）。"""

    if isinstance(container, dict):
        raw = container.get("items")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return list(raw.values())
        return []
    if isinstance(container, list):
        return container
    return []


def _status_of(container, *, empty_status="not_calculated"):
    if not isinstance(container, dict):
        return "not_calculated" if container is None else "unknown"
    declared = container.get("status")
    if declared:
        return str(declared)
    return empty_status


def _haversine_m(a, b):
    """球面 haversine（仅作 alternate 对照口径，不是 canonical）。"""

    lon1, lat1 = math.radians(float(a[0])), math.radians(float(a[1]))
    lon2, lat2 = math.radians(float(b[0])), math.radians(float(b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


def _now():
    return datetime.now(timezone.utc).isoformat()


class Visualization3DUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    def as_dict(self):
        return {"error": str(self), "code": self.code, "detail": self.detail}


class Visualization3DService:
    """只读三维投影。

    :param state: ``WorkflowService.state``（**只读**引用，绝不修改）
    :param terrain_source: :class:`cns_planner.gis.terrain_window_adapter.TerrainWindowSource`
    :param geoid: :class:`cns_planner.gis.egm2008_geoid.Egm2008Geoid`（可为 ``None``）
    :param geoid_error: geoid 不可用时的原因字典
    """

    def __init__(self, state, *, terrain_source=None, terrain_tile_source=None,
                 geoid=None, geoid_error=None, imagery_service=None, state_identity=None,
                 relief_tile_source=None, source_lock=None, data=None):
        self.state = state if isinstance(state, dict) else {}
        self.terrain_source = terrain_source
        self.terrain_tile_source = terrain_tile_source
        self.geoid = geoid
        self.geoid_error = geoid_error
        self.imagery_service = imagery_service
        self.state_identity = state_identity or {}
        #: ``MapData``（**只读**引用）：仅用于读取已配置的真实数据源路径
        #: （陆域掩膜）。本服务绝不通过它写配置或加载数据集。
        self._data = data
        #: FABDEM 派生晕渲源；仅在合法影像不可用时作为**明确标注**的地形表达。
        self.relief_tile_source = relief_tile_source
        #: 保护共享 GDAL/PROJ 句柄的**唯一**锁。地形瓦片、晕渲瓦片与
        #: ``terrain-window`` 接口读的都是同一批 Dataset/transformer，必须互斥；
        #: 各组件自建锁等于没有互斥（实测过这个缺陷）。
        self._source_lock = source_lock if source_lock is not None else threading.RLock()

    # ------------------------------------------------------------------ 基础

    def identity_seed(self):
        """ETag 的**来源身份**种子（只依赖只读输入，与 ``generated_at`` 无关）。"""

        terrain = self.terrain_source.describe() if self.terrain_source else None
        geoid = self.geoid.describe() if self.geoid else None
        demo = self._current_demo_adoption() or {}
        imagery = self.imagery_service.configuration() if self.imagery_service else None
        terrain_tiles = self.terrain_tile_source.layer_json(
            "/api/visualization-3d/terrain") if self.terrain_tile_source else None
        return {
            "revision": self._revision(),
            "scope": self.scope(),
            "terrain_sha256": (terrain or {}).get("sha256"),
            "geoid_sha256": (geoid or {}).get("sha256"),
            "demo_projection_fingerprint": demo.get("projection_fingerprint"),
            "demo_adoption_id": demo.get("adoption_id"),
            "canonical_route_fingerprints": [
                entry["identity"]["fingerprint"] for entry in self._canonical_route_entries()
            ],
            "tower_count": len(_items(self.state.get("towers"))),
            "tower_profile_count": len(_items(self.state.get("tower_obstacle_profiles"))),
            "device_count": len(_items(self.state.get("device_catalog"))),
            "facility_count": len(_items(self.state.get("existing_cns_facilities"))),
            "imagery": {
                "default_source": (imagery or {}).get("default_source"),
                "status": (imagery or {}).get("status"),
                "reason_code": (imagery or {}).get("reason_code"),
            },
            "terrain_tiles": {
                "format": (terrain_tiles or {}).get("format"),
                "maxzoom": (terrain_tiles or {}).get("maxzoom"),
                "fabdem_sha256": ((terrain_tiles or {}).get("source") or {}).get("fabdem_sha256"),
                "egm2008_sha256": ((terrain_tiles or {}).get("source") or {}).get("egm2008_sha256"),
            },
        }

    def _revision(self):
        try:
            return int(self.state.get("revision") or 0)
        except (TypeError, ValueError):
            return 0

    def _current_demo_adoption(self):
        """当前**生效**的工程演示采纳记录（``current_applicability == current``）。"""

        for item in _items(self.state.get("layered_operational_adoptions")):
            if not isinstance(item, dict):
                continue
            if (str(item.get("status")) == "published_engineering_demo"
                    and str(item.get("current_applicability")) == "current"):
                return item
        return None

    def _current_strict_adoption(self):
        for item in _items(self.state.get("layered_operational_adoptions")):
            if not isinstance(item, dict):
                continue
            if (str(item.get("status")) == "published"
                    and str(item.get("current_applicability")) == "current"):
                return item
        return None

    def scope(self):
        """``strict`` 或 ``engineering_demo_only``（由当前生效采纳记录决定）。"""

        if self._current_demo_adoption() is not None:
            return "engineering_demo_only"
        return "strict"

    def _scope_block(self):
        demo = self._current_demo_adoption()
        strict = self._current_strict_adoption()
        active = demo or strict
        scope = "engineering_demo_only" if demo is not None else "strict"
        authorization = False
        strict_passed = False
        if active is not None:
            authorization = bool(active.get("operational_authorization")) and demo is None
            strict_passed = bool(active.get("strict_safety_validation_passed"))
        disclosure = []
        if scope == "engineering_demo_only":
            disclosure = [
                "严格安全验证未通过（strict_safety_validation_passed=false）",
                "无运行许可（operational_authorization=false）",
                "本视图为工程演示，不得作为运行依据",
            ]
        return {
            "scope": scope,
            "engineering_demo_only": demo is not None,
            "operational_authorization": authorization,
            "strict_safety_validation_passed": strict_passed,
            "adoption_id": (active or {}).get("adoption_id"),
            "adoption_status": (active or {}).get("status"),
            "current_applicability": (active or {}).get("current_applicability"),
            "publication_target": (
                "layered_operational_adoptions.engineering_demo_projection"
                if demo is not None else "operational_routes"
            ),
            "published_to_operational_routes": bool(
                (active or {}).get("published_to_operational_routes")
            ),
            "disclosure": disclosure,
        }

    # -------------------------------------------------------------- 垂直基准

    def _vertical_reference_block(self):
        spatial = self.state.get("spatial_3d") or {}
        canonical = str(spatial.get("canonical_vertical_reference") or "egm2008_orthometric")
        if self.geoid is not None:
            conversion = {
                "status": "available",
                "method": "egm2008_geoid_undulation",
                "formula_orthometric_to_ellipsoidal": "h_ellipsoidal = H_orthometric + N",
                "formula_ellipsoidal_to_orthometric": "H_orthometric = h_ellipsoidal - N",
                "grid_interpolation": "bilinear_2x2",
                "epsilon_tolerance_m": 1e-6,
                "geoid_grid": self.geoid.describe(),
                "substitutes_forbidden": ["EGM96", "EGM84"],
            }
        else:
            conversion = {
                "status": "blocked",
                "method": "egm2008_geoid_undulation",
                "reason": (self.geoid_error or {}).get("code", "egm2008_grid_missing"),
                "reason_text": (self.geoid_error or {}).get("message"),
                "geoid_grid": None,
                "substitutes_forbidden": ["EGM96", "EGM84"],
                "note": "正高与椭球高**不混用**；本视图保持 EGM2008 正高自洽渲染",
            }
        return {
            "canonical": canonical,
            "displayed": canonical,
            "canonical_is_ellipsoidal": False,
            "ellipsoidal_conversion": conversion,
        }

    # ------------------------------------------------------------------ 高度

    def _altitude_layer(self, route_id):
        """解析该航路的固定巡航高度层（只读回显，绝不推断）。"""

        spatial = self.state.get("spatial_3d") or {}
        layers = {str(item.get("altitude_layer_id")): item
                  for item in _items(spatial.get("altitude_layers")) if isinstance(item, dict)}
        chosen = None
        for item in _items(spatial.get("route_operating_layers")):
            if not isinstance(item, dict):
                continue
            if str(item.get("route_id")) != str(route_id):
                continue
            if item.get("active") is False:
                continue
            chosen = item
            break
        layer = layers.get(str((chosen or {}).get("altitude_layer_id"))) if chosen else None
        return {
            "mode": "constant_cruise_layer",
            "altitude_layer_id": (chosen or {}).get("altitude_layer_id"),
            "nominal_altitude_m": (layer or {}).get("nominal_altitude_m"),
            "vertical_reference": (layer or {}).get("vertical_reference"),
            "layer_status": (layer or {}).get("status"),
            "layer_confirmed": (layer or {}).get("confirmed"),
            "operating_layer_confirmed": (chosen or {}).get("confirmed"),
            "operating_layer_active": (chosen or {}).get("active"),
            "source": (chosen or {}).get("source"),
            "full_3d_profile_available": bool(_items(spatial.get("route_3d_profiles"))),
            "climb_cruise_descent_available": False,
            "limitation": (
                "权威状态无 Route3DProfile：只有固定巡航高度，无爬升/下降几何；"
                "第三坐标**不是**高度（two_dimensional_crs84_path=true）"
            ),
        }

    # ------------------------------------------------------- 几何身份与里程

    def _route_geometry_identity(self, *, route_id, path, candidate_id=None,
                                 validation_id=None, provenance=None, source_label=None):
        """**统一**的航路几何身份 + 长度方法 + 里程插值合同。"""

        provenance = provenance or {}
        offsets = []
        cumulative = 0.0
        for a, b in zip(path, path[1:]):
            cumulative += distance_m(a, b)
            offsets.append(cumulative)
        equirect_total = cumulative
        haversine_total = sum(_haversine_m(a, b) for a, b in zip(path, path[1:]))
        identity_payload = {
            "route_id": route_id,
            "candidate_id": candidate_id,
            "validation_id": validation_id,
            "point_count": len(path),
            "path": [[float(p[0]), float(p[1])] for p in path],
            "path_crs": "OGC:CRS84",
        }
        fingerprint = stable_fingerprint(identity_payload, prefix="routegeometryv1-")
        return {
            "identity_id": f"route-geometry:{route_id}:{candidate_id or 'unknown'}",
            "object_id_prefix": OBJECT_ID_PREFIXES["route_geometry"],
            "route_id": route_id,
            "candidate_id": candidate_id,
            "validation_id": validation_id,
            "candidate_fingerprint": provenance.get("candidate_fingerprint"),
            "validation_fingerprint": provenance.get("validation_fingerprint"),
            "projection_fingerprint": provenance.get("projection_fingerprint"),
            "adoption_fingerprint": provenance.get("adoption_fingerprint"),
            "source_label": source_label,
            "point_count": len(path),
            "path_crs": "OGC:CRS84",
            "third_coordinate_is_altitude": False,
            "fingerprint": fingerprint,
            "mileage": {
                "metric": LENGTH_METRIC,
                "implementation": LENGTH_IMPLEMENTATION,
                "horizontal_only": True,
                "vertical_component": "none",
                "total_m": equirect_total,
                "segment_cumulative_offsets_m": offsets,
                "interpolation_contract": {
                    "method": "equirectangular_segment_cumulative_linear",
                    "policy": "clamp_no_extrapolation",
                    "note": "按段累计里程线性插值；越过两端一律 clamp，不按比例外推",
                },
                "alternate_metrics_m": {
                    "spherical_haversine": haversine_total,
                    "note": "仅作对照；canonical 一律用 equirectangular（与后端同式）",
                },
                "geometry_binding": {
                    "bound_to_geometry_fingerprint": fingerprint,
                    "cross_geometry_reuse_forbidden": True,
                    "reason": (
                        "同一 R0005 在状态里同时存在 7/11/14 点三份不同几何，"
                        "长度只在同指纹内可比；跨几何套用里程段会造成 378–500 m 错位"
                    ),
                },
            },
        }

    # ------------------------------------------------------------ demo 航路

    def _demo_route_entry(self):
        """合法的工程演示航路（来自 ``engineering_demo_projection``，不伪造 canonical）。"""

        adoption = self._current_demo_adoption()
        if adoption is None:
            return None
        route = ((adoption.get("after") or {}).get("route")
                 or (adoption.get("before") or {}).get("route") or {})
        path = route.get("path")
        if not isinstance(path, list) or len(path) < 2:
            return None
        clean = [[float(p[0]), float(p[1])] for p in path
                 if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(clean) < 2:
            return None
        route_id = str(route.get("route_id") or adoption.get("route_id") or "")
        identity = self._route_geometry_identity(
            route_id=route_id,
            path=clean,
            candidate_id=adoption.get("candidate_id"),
            validation_id=adoption.get("validation_id"),
            provenance={
                "candidate_fingerprint": adoption.get("candidate_fingerprint"),
                "validation_fingerprint": adoption.get("validation_fingerprint"),
                "projection_fingerprint": adoption.get("projection_fingerprint"),
            },
            source_label="layered_operational_adoptions.items[current].after.route",
        )
        return {
            "authority": "engineering_demo_projection",
            "authority_label": "工程演示投影（非正式运行航路）",
            "status": str(route.get("status") or "engineering_demo_only"),
            "kind": route.get("kind"),
            "use_scope": "engineering_demo_only",
            "current_applicability": adoption.get("current_applicability"),
            "route_id": route_id,
            "identity": identity,
            "path": clean,
            "path_crs": "OGC:CRS84",
            "altitude": self._altitude_layer(route_id),
            "operational_authorization": False,
            "strict_safety_validation_passed": bool(
                ((adoption.get("after") or {}).get("route") or {}).get("provenance", {})
                .get("strict_safety_validation_passed")
            ),
            "source_container": "state.layered_operational_adoptions.engineering_demo_projection",
            "is_canonical_operational_route": False,
            "notes": [
                "该几何来自合法 demo projection，**未**写入 canonical operational_routes",
                "严格安全验证未通过；无运行许可",
            ],
        }

    def _canonical_route_entries(self):
        """正式 ``operational_routes`` 容器（原样回显状态，绝不改写成"已通过"）。"""

        entries = []
        for route in _items(self.state.get("operational_routes")):
            if not isinstance(route, dict):
                continue
            path = route.get("path")
            if not isinstance(path, list) or len(path) < 2:
                continue
            clean = [[float(p[0]), float(p[1])] for p in path
                     if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(clean) < 2:
                continue
            route_id = str(route.get("route_id") or "")
            provenance = route.get("provenance") or {}
            identity = self._route_geometry_identity(
                route_id=route_id,
                path=clean,
                candidate_id=provenance.get("candidate_id"),
                validation_id=provenance.get("validation_id"),
                provenance=provenance,
                source_label="operational_routes.items[].path",
            )
            entries.append({
                "authority": "canonical_operational_routes",
                "authority_label": "正式运行航路容器",
                "status": str(route.get("status") or "unknown"),
                "stale_reason": route.get("stale_reason"),
                "kind": route.get("kind"),
                "use_scope": "strict_operational_route" if route.get("status") == "passed"
                             else "unknown",
                "route_id": route_id,
                "identity": identity,
                "path": clean,
                "path_crs": route.get("path_crs") or "OGC:CRS84",
                "altitude": self._altitude_layer(route_id),
                "is_canonical_operational_route": True,
                "notes": (
                    [] if route.get("status") == "passed"
                    else [f"正式容器状态为 {route.get('status')}，不得按已确认展示"]
                ),
            })
        return entries

    # ------------------------------------------------------------ 里程冲突

    def _mileage_conflicts(self):
        """显式报告"容器里的长度属于另一份几何"的冲突（跨几何复用检测）。"""

        conflicts = []
        canonical = {entry["route_id"]: entry for entry in self._canonical_route_entries()}
        declared = []
        coverage = self.state.get("coverage_3d") or {}
        for route in _items(coverage.get("routes")):
            if isinstance(route, dict) and route.get("route_length_m") is not None:
                declared.append({
                    "container": "coverage_3d.routes[].route_length_m",
                    "container_status": str(coverage.get("status") or "unknown"),
                    "route_id": str(route.get("route_id")),
                    "declared_m": float(route.get("route_length_m")),
                    "has_geometry": False,
                })
        capability = self.state.get("cns_service_capability") or {}
        for route in _items(capability.get("routes")):
            if not isinstance(route, dict):
                continue
            for field in ("route_length_m", "fail_length_m"):
                if route.get(field) is not None:
                    declared.append({
                        "container": f"cns_service_capability.routes[].{field}",
                        "container_status": str(capability.get("status") or "unknown"),
                        "route_id": str(route.get("route_id")),
                        "declared_m": float(route.get(field)),
                        "has_geometry": False,
                    })
        corridor = self.state.get("cns_corridor_assessment") or {}
        for route in _items(corridor.get("routes")):
            if not isinstance(route, dict):
                continue
            geometry = route.get("corridor_geometry") or {}
            path = geometry.get("route_path")
            declared_length = route.get("route_length_m")
            if declared_length is None:
                continue
            recomputed = None
            if isinstance(path, list) and len(path) >= 2:
                clean = [[float(p[0]), float(p[1])] for p in path
                         if isinstance(p, (list, tuple)) and len(p) >= 2]
                recomputed = sum(distance_m(a, b) for a, b in zip(clean, clean[1:]))
            declared.append({
                "container": "cns_corridor_assessment.routes[].route_length_m",
                "container_status": str(corridor.get("status") or "unknown"),
                "route_id": str(route.get("route_id")),
                "declared_m": float(declared_length),
                "has_geometry": isinstance(path, list),
                "geometry_point_count": len(path) if isinstance(path, list) else None,
                "recomputed_from_own_geometry_m": recomputed,
            })

        for entry in declared:
            route_id = entry["route_id"]
            live = canonical.get(route_id)
            if live is None:
                continue
            live_total = live["identity"]["mileage"]["total_m"]
            live_points = live["identity"]["point_count"]
            delta = entry["declared_m"] - live_total
            own = entry.get("recomputed_from_own_geometry_m")
            if own is not None:
                own_delta = abs(entry["declared_m"] - own)
                if own_delta > 0.5:
                    conflicts.append({
                        "kind": "declared_length_disagrees_with_own_geometry",
                        "container": entry["container"],
                        "route_id": route_id,
                        "declared_m": entry["declared_m"],
                        "recomputed_from_own_geometry_m": own,
                        "delta_m": own_delta,
                        "container_status": entry["container_status"],
                        "severity": "high",
                    })
                continue
            if abs(delta) <= 0.5:
                continue
            conflicts.append({
                "kind": "declared_length_belongs_to_another_geometry",
                "container": entry["container"],
                "route_id": route_id,
                "declared_m": entry["declared_m"],
                "live_geometry_m": live_total,
                "live_geometry_point_count": live_points,
                "delta_m": delta,
                "container_status": entry["container_status"],
                "live_geometry_status": live["status"],
                "severity": "high" if abs(delta) > 1.0 else "low",
                "consequence": (
                    "该长度不得用于三维里程轴，也不得与该几何的保护走廊/缺口段混用"
                ),
            })
        return conflicts

    # ------------------------------------------------------------- 图层清单

    def _tower_layer(self):
        profiles = {str(item.get("tower_id")): item
                    for item in _items(self.state.get("tower_obstacle_profiles"))
                    if isinstance(item, dict)}
        towers = []
        resolved = 0
        unknown = 0
        for tower in _items(self.state.get("towers")):
            if not isinstance(tower, dict):
                continue
            lon, lat = tower.get("longitude"), tower.get("latitude")
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                continue
            profile = profiles.get(str(tower.get("tower_id"))) or {}
            top = profile.get("tower_top_orthometric_m")
            known = isinstance(top, (int, float))
            resolved += 1 if known else 0
            unknown += 0 if known else 1
            towers.append({
                "object_id": f"{OBJECT_ID_PREFIXES['tower']}{tower.get('tower_id')}",
                "tower_id": tower.get("tower_id"),
                "name": tower.get("name"),
                "longitude": float(lon),
                "latitude": float(lat),
                "site_type": tower.get("site_type"),
                "source_elevation_m": tower.get("elevation_m"),
                "source_height_m": tower.get("height_m"),
                "terrain_elevation_m": profile.get("terrain_elevation_m"),
                "terrain_vertical_reference": profile.get("terrain_vertical_reference"),
                "terrain_status": profile.get("terrain_status"),
                "tower_structure_height_m": profile.get("tower_structure_height_m"),
                "tower_top_orthometric_m": top if known else None,
                "tower_top_status": profile.get("tower_top_status") or "unknown",
                "vertical_status": profile.get("vertical_status") or "unknown",
                "confirmed": bool(profile.get("confirmed")),
                "status": "resolved" if known else "unknown",
                "unknown_reason": None if known else (
                    profile.get("reason") or "tower_top_orthometric_m_unresolved"
                ),
            })
        return {
            "id": "towers",
            "label": "铁塔点位与塔顶高程",
            "status": "available" if towers else "not_calculated",
            "count": len(towers),
            "known_top_count": resolved,
            "unknown_top_count": unknown,
            "object_id_prefix": OBJECT_ID_PREFIXES["tower"],
            "source_identity": {
                "state_key": "towers + tower_obstacle_profiles",
                "provenance_level": "production_container_readonly",
                "vertical_reference": "egm2008_orthometric",
            },
            "geometry_semantics": "point_with_optional_top_elevation",
            "notes": [
                f"{unknown} 座塔的塔顶正高未解析（unknown ≠ 0，绝不填 0）",
                "塔顶高程是派生事实，不进入人口风险或 Risk Framework V2",
            ],
            "items": towers[:MAX_TOWERS],
            "truncated": len(towers) > MAX_TOWERS,
        }

    def _device_layer(self):
        devices = []
        for device in _items(self.state.get("device_catalog")):
            if not isinstance(device, dict):
                continue
            coverage = device.get("coverage_geometry") or {}
            vertical = device.get("vertical_profile") or {}
            devices.append({
                "object_id": f"{OBJECT_ID_PREFIXES['device']}{device.get('device_id')}",
                "device_id": device.get("device_id"),
                "name": device.get("name"),
                "service_key": device.get("service_key"),
                "subsystem": device.get("subsystem"),
                "role": device.get("role"),
                "enabled": device.get("enabled"),
                "confirmed": device.get("confirmed"),
                "confirmation_status": device.get("confirmation_status"),
                "coverage_model": coverage.get("model"),
                "model_scope": coverage.get("model_scope"),
                "radius_by_surface": coverage.get("radius_by_surface"),
                "slant_range_m": coverage.get("slant_range_m"),
                "coverage_confirmed": coverage.get("confirmed"),
                "service_origin_egm2008_m": vertical.get("service_origin_egm2008_m"),
                "vertical_profile_status": vertical.get("status"),
                "vertical_profile_confirmed": vertical.get("confirmed"),
                "vertical_range_limits_available": False,
                "vertical_unknown_reason": "vertical_effective_range_not_modelled",
                "position_available": False,
                "position_note": "设备本体的安装位置在 existing_cns_facilities 上，见 facilities 图层",
            })
        return {
            "id": "cns_devices",
            "label": "CNS 设备与服务能力",
            "status": "available" if devices else "not_calculated",
            "count": len(devices),
            "object_id_prefix": OBJECT_ID_PREFIXES["device"],
            "source_identity": {"state_key": "device_catalog", "provenance_level": "declared_performance"},
            "geometry_semantics": "capability_record_point_attached_to_facility",
            "notes": [
                "垂直有效范围在数据模型中缺失 ⇒ 一律 unknown，不得用固定高度切片冒充",
                "model_scope=geometric_only：只有几何包络，没有场强/LOS/链路预算",
            ],
            "items": devices[:MAX_DEVICES],
            "truncated": len(devices) > MAX_DEVICES,
        }

    def _facility_layer(self):
        facilities = []
        for site in _items(self.state.get("existing_cns_facilities")):
            if not isinstance(site, dict):
                continue
            coordinate = site.get("coordinate")
            vertical = site.get("vertical_profile") or {}
            metadata = site.get("metadata") or {}
            facilities.append({
                "object_id": f"{OBJECT_ID_PREFIXES['facility']}{site.get('facility_id')}",
                "facility_id": site.get("facility_id"),
                "site_id": site.get("site_id"),
                "name": site.get("name"),
                "longitude": (float(coordinate[0]) if isinstance(coordinate, (list, tuple))
                              and len(coordinate) >= 2 else None),
                "latitude": (float(coordinate[1]) if isinstance(coordinate, (list, tuple))
                             and len(coordinate) >= 2 else None),
                "coordinate": coordinate,
                "service_origin_egm2008_m": vertical.get("service_origin_egm2008_m"),
                "vertical_profile_status": vertical.get("status"),
                "vertical_reference": vertical.get("surface_vertical_reference"),
                "dataset_class": metadata.get("dataset_class"),
                "real_world_facility": metadata.get("real_world_facility"),
                "synthetic": str(metadata.get("dataset_class") or "").startswith("synthetic"),
                "device_ids": [d.get("device_id") for d in (site.get("devices") or [])
                               if isinstance(d, dict)],
            })
        return {
            "id": "cns_facilities",
            "label": "CNS 站址（设备安装位置）",
            "status": "available" if facilities else "not_calculated",
            "count": len(facilities),
            "object_id_prefix": OBJECT_ID_PREFIXES["facility"],
            "source_identity": {"state_key": "existing_cns_facilities",
                                "provenance_level": "production_container_readonly"},
            "geometry_semantics": "station_point_with_service_origin_elevation",
            "notes": ["合成站址必须显式标注 [SYNTHETIC]，不得当作真实设施"],
            "items": facilities,
            "truncated": False,
        }

    def _reserved_layer(self, layer_id, label, semantics, notes, id_prefix, status="not_implemented"):
        return {
            "id": layer_id,
            "label": label,
            "status": status,
            "count": 0,
            "object_id_prefix": id_prefix,
            "geometry_semantics": semantics,
            "source_identity": {"state_key": None, "provenance_level": "reserved_contract"},
            "notes": notes,
            "items": [],
            "reserved": True,
        }

    # ---------------------------------------------------------------- manifest

    def manifest(self, query=None):
        """场景清单。

        默认**只给摘要**（图层状态 + 计数 + 身份），不带任何几何；
        ``?layers=towers,cns_devices`` 才把指定图层的 ``items`` 附上 ——
        这就是最小端点下的"按需分块"：首屏不下载全量点位。
        """

        query = query or {}
        spatial = self.state.get("spatial_3d") or {}
        scope_block = self._scope_block()
        demo = self._demo_route_entry()
        canonical = self._canonical_route_entries()
        towers = self._tower_layer()
        devices = self._device_layer()
        facilities = self._facility_layer()
        from .visualization_3d_sites import communication_units, rid_units
        comm_units = communication_units(self.state)
        rid_units_available = rid_units(
            self.state, rid_radius_by_surface=RID_RADIUS_BY_SURFACE)
        terrain = self.terrain_source.describe() if self.terrain_source else None
        imagery = self.imagery_service.configuration() if self.imagery_service else {
            "status": "blocked", "reason_code": "imagery_service_unavailable",
        }

        route_layer_status = "available" if demo else (
            "stale" if canonical and canonical[0]["status"] != "passed" else "not_calculated"
        )
        layers = [
            {
                "id": "terrain_fabdem",
                "label": "FABDEM 三维地形",
                "status": "available" if terrain else "not_calculated",
                "count": 1 if terrain else 0,
                "object_id_prefix": OBJECT_ID_PREFIXES["terrain_window"],
                "geometry_semantics": "heightmap_window",
                "source_identity": {
                    "state_key": "map_sources.terrain_dtm",
                    "provenance_level": "production_readonly_source",
                    "vertical_reference": (terrain or {}).get("vertical_reference"),
                    "sha256": (terrain or {}).get("sha256"),
                },
                "detail_endpoint": "/api/visualization-3d/terrain-window",
                "notes": [
                    "正高自洽场：地形与所有几何使用同一 EGM2008 正高基准",
                    "NoData 保留为 null，绝不填补为 0",
                ],
                "items": [],
            },
            {
                "id": "route_geometry_demo",
                "label": "R0005 工程演示航路（demo projection）",
                "status": "available" if demo else "not_calculated",
                "count": 1 if demo else 0,
                "object_id_prefix": OBJECT_ID_PREFIXES["route"],
                "geometry_semantics": "two_dimensional_crs84_polyline_at_fixed_cruise_layer",
                "source_identity": {
                    "state_key": "layered_operational_adoptions.engineering_demo_projection",
                    "provenance_level": "engineering_demo_only",
                    "candidate_id": (demo or {}).get("identity", {}).get("candidate_id"),
                    "fingerprint": (demo or {}).get("identity", {}).get("fingerprint"),
                },
                "detail_endpoint": "/api/visualization-3d/routes",
                "notes": (demo or {}).get("notes", []),
                "items": [],
            },
            {
                "id": "route_geometry_canonical",
                "label": "正式运行航路容器（operational_routes）",
                "status": route_layer_status if canonical else "not_calculated",
                "count": len(canonical),
                "object_id_prefix": OBJECT_ID_PREFIXES["route"],
                "geometry_semantics": "two_dimensional_crs84_polyline_at_fixed_cruise_layer",
                "source_identity": {
                    "state_key": "operational_routes",
                    "provenance_level": "production_container_readonly",
                    "status": canonical[0]["status"] if canonical else None,
                    "stale_reason": canonical[0].get("stale_reason") if canonical else None,
                },
                "detail_endpoint": "/api/visualization-3d/routes",
                "notes": [
                    "容器状态原样回显；stale 绝不伪装为已确认",
                    "该几何与 demo projection 的几何**不同版本**，里程不可互相套用",
                ],
                "items": [],
            },
            {**towers, "detail_endpoint": "/api/visualization-3d/manifest"},
            {**devices, "detail_endpoint": None},
            {**facilities, "detail_endpoint": None},
            {
                "id": "communication_volumes",
                "label": "通信三维工程几何包络",
                "status": "available" if comm_units else "not_calculated",
                "count": len(comm_units),
                "object_id_prefix": OBJECT_ID_PREFIXES["communication_volume"],
                "geometry_semantics": "omnidirectional_hemisphere_slant_range",
                "source_identity": {
                    "state_key": "existing_cns_facilities + device_catalog",
                    "provenance_level": "production_container_readonly",
                    "radius_authority": "coverage_geometry.radius_by_surface",
                },
                "detail_endpoint": "/api/visualization-3d/communication-volumes",
                "notes": [
                    "engineering_geometric_coverage_not_measured：工程几何包络，非实测覆盖",
                    "按**三维斜距**约束（半球壳）；二维 4 km 圆盘只是 delta=0 处的切片",
                    "未确认安装高度的设备**不生成**确定三维包络（如实列出所需证据）",
                    "遮挡 / 地形 LOS / 绕射 / 干扰 / 链路预算一律 not_evaluated",
                ],
                "items": [],
            },
            {
                "id": "rid_footprints",
                "label": "RID 陆海非正圆覆盖（合作监视工程几何）",
                "status": "available" if rid_units_available else "not_calculated",
                "count": len(rid_units_available),
                "object_id_prefix": OBJECT_ID_PREFIXES["rid_footprint"],
                "geometry_semantics": "surface_partitioned_omnidirectional_2d",
                "source_identity": {
                    "state_key": "existing_cns_facilities + device_catalog",
                    "provenance_level": "production_container_readonly",
                    "radius_authority": "RID_RADIUS_BY_SURFACE",
                },
                "detail_endpoint": "/api/visualization-3d/rid-footprints",
                "notes": [
                    "陆 2000 m / 海岸不确定 2000 m / 海 5000 m；unknown 不产生几何",
                    "轮廓逐方位角由**权威** LandMaskSource 分类结果决定（非两个同心圆）",
                    "只展示有依据的高度切片，绝不拉伸成确定高度的覆盖体",
                    "**绝不**表现成 Radar 方向性扇区；不保证实测接收能力",
                ],
                "items": [],
            },
            self._reserved_layer(
                "radar_volumes", "Radar 方向性三维体", "sector_wedge_slant_shell",
                ["本轮未实现；合同已预留 ID 与 scope",
                 "单面阵绝不渲染成全向；LOS 一律 unknown（terrain_los_not_persisted）"],
                OBJECT_ID_PREFIXES["radar_panel"],
            ),
            self._reserved_layer(
                "protection_volumes", "航路三维保护体与监视缺口", "corridor_halfwidth_volume",
                ["本轮未实现；合同已预留 ID 与 scope",
                 "权威边界只来自 operational_routes → P14 → P17，本服务不设置任何新半径",
                 "P17 corridor 与当前几何指纹不匹配时**不得**强行套用保护区"],
                OBJECT_ID_PREFIXES["protection_corridor"],
            ),
            self._reserved_layer(
                "simulated_intrusion", "入侵目标预警演示（模拟）", "simulated_track_three_column",
                ["本轮未实现；合同已预留 ID 与 scope",
                 "必须常驻 simulated=true；不写 ProjectState、不进 fingerprint/staleness/readiness",
                 "是否实际确认探测恒为 not_confirmed"],
                OBJECT_ID_PREFIXES["simulated_intruder"],
                status="reserved_simulated",
            ),
        ]

        raw_layers = _first(query, "layers")
        requested = [token.strip() for token in str(raw_layers or "").split(",") if token.strip()]
        if requested:
            known = {layer["id"] for layer in layers}
            unknown = [token for token in requested if token not in known]
            for layer in layers:
                if layer["id"] not in requested:
                    layer["items"] = []
                    layer["items_omitted"] = True
                    layer["detail_endpoint"] = (
                        f"/api/visualization-3d/manifest?layers={layer['id']}"
                    )
        else:
            unknown = []
            for layer in layers:
                if layer.get("items"):
                    layer["items_omitted"] = True
                    layer["items"] = []
                    layer["detail_endpoint"] = (
                        f"/api/visualization-3d/manifest?layers={layer['id']}"
                    )

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **scope_block,
            "vertical_reference": self._vertical_reference_block(),
            "terrain": terrain,
            "terrain_window_endpoint": "/api/visualization-3d/terrain-window",
            "terrain_tiles": {
                "status": "available" if self.terrain_tile_source else "blocked",
                "format": "heightmap-1.0" if self.terrain_tile_source else None,
                "maxzoom": 13 if self.terrain_tile_source else None,
                "layer_json_endpoint": "/api/visualization-3d/terrain/layer.json",
                "automatic_lod": bool(self.terrain_tile_source),
                # 水 mask 状态必须与 layer.json 的 extensions 同真：前端据此决定
                # 是否启用海面效果。对外只暴露粗粒度 available/blocked，不泄漏内部
                # 失败码细节。
                "water_mask_status": (
                    "available" if _terrain_water_mask_available(self.terrain_tile_source)
                    else "blocked"
                ),
            },
            "imagery": imagery,
            "imagery_config_endpoint": "/api/visualization-3d/imagery/config",
            # FABDEM 派生地形晕渲：**不是**卫星影像。当合法影像凭证不可用时，它是
            # 唯一能表达真实起伏的底图；对外必须始终带 is_satellite_imagery=false，
            # 绝不允许被当作影像验收证据。
            "terrain_relief": {
                "status": "available" if self.relief_tile_source else "blocked",
                "kind": "fabdem_hillshade",
                "is_satellite_imagery": False,
                "derived_from": ["fabdem_30m", "egm2008_geoid"],
                "vertical_exaggeration": 1.0,
                "url_template": "/api/visualization-3d/relief/{z}/{x}/{y}.png",
                "tile_size": 256,
                # 前端据此把图层限制在 FABDEM 覆盖范围内，避免请求全球低层级瓦片。
                "extent": (list(self.relief_tile_source.extent)
                           if self.relief_tile_source else None),
                "minzoom": 6,
                "maxzoom": 13,
                "label": "FABDEM 地形晕渲（非卫星影像）",
                "attribution": "地形晕渲由 FABDEM 30 m + EGM2008 生成；非卫星影像",
            },
            "routes_endpoint": "/api/visualization-3d/routes",
            "object_id_prefixes": OBJECT_ID_PREFIXES,
            "layers": layers,
            "layer_ids": [layer["id"] for layer in layers],
            "requested_layers": requested or None,
            "unknown_requested_layers": unknown,
            "layers_inline": bool(requested),
            "load_budget": {
                "manifest_target_bytes": 65536,
                "manifest_bytes": None,
                "first_screen_target_mb": 8,
                "max_terrain_cells_per_request": MAX_TERRAIN_CELLS,
                # 实测（不是硬编码）：本进程收到的 /api/workflow 全量快照请求数。
                "workflow_endpoint_used": workflow_snapshot_requests_observed() > 0,
                "workflow_endpoint_requests": workflow_snapshot_requests_observed(),
                "blocking_status": ["available", "partial", "not_calculated", "stale", "unknown"],
            },
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
            "state_identity": self.state_identity,
            "spatial_3d_status": spatial.get("status"),
            "warning": (
                "工程演示模式（engineering_demo_only）：严格安全验证未通过、无运行许可。"
                if scope_block["engineering_demo_only"] else None
            ),
        }

    def terrain_layer_json(self):
        if self.terrain_tile_source is None:
            raise Visualization3DUnavailable(
                "terrain_tiles_unavailable", "多级 FABDEM 地形尚不可用")
        return self.terrain_tile_source.layer_json("/api/visualization-3d/terrain")

    def terrain_tile(self, z, x, y):
        if self.terrain_tile_source is None:
            raise Visualization3DUnavailable(
                "terrain_tiles_unavailable", "多级 FABDEM 地形尚不可用")
        return self.terrain_tile_source.tile(z, x, y)

    def relief_tile(self, z, x, y):
        """FABDEM 派生晕渲瓦片（**不是**卫星影像，见模块 docstring）。"""

        if self.relief_tile_source is None:
            raise Visualization3DUnavailable(
                "relief_tiles_unavailable", "FABDEM 地形晕渲尚不可用")
        return self.relief_tile_source.tile(z, x, y)

    # ------------------------------------------------- 三维 CNS 覆盖（只读几何）

    def _surface_class_provider(self):
        """取**权威**陆域分类源（fail-closed：未配置时返回 ``None``）。

        复用与 Step5 surface facts / Radar layout **同一份**路径与策略：图层名与
        海岸不确定带只来自 canonical ``surface_classification_policy``，
        ``unknown`` 一律 fail-closed。本服务**不复制**任何分类算法。
        """

        data = self._data
        paths = getattr(data, "paths", None) or {}
        land_mask_path = paths.get("land_mask")
        if not land_mask_path:
            return None, {"code": "land_mask_not_configured",
                          "message": "未配置陆域掩膜，无法做陆海分区"}
        policy = self.state.get("surface_classification_policy") or {}
        try:
            from ..gis.radar_layout_adapter import LandMaskSource
        except Exception as exc:  # pragma: no cover - 几何库缺失
            return None, {"code": "land_mask_backend_unavailable",
                          "message": str(exc)}
        source = LandMaskSource(
            land_mask_path,
            layer_name=policy.get("land_mask_layer_name"),
            coastal_uncertainty_buffer_m=policy.get("coastal_uncertainty_buffer_m"),
        )
        return source, None

    def communication_volumes(self, query=None):
        """``GET /api/visualization-3d/communication-volumes`` 的只读投影。

        通信三维工程几何包络：使用**已有设备的真实坐标与安装高程**，复用现有全向
        几何规划模型（``coverage_geometry.radius_by_surface``，默认服务合同半径
        4000 m，但以设备与生效服务合同为准），并按**三维斜距**约束包络面。
        """

        query = query or {}
        from .visualization_3d_communication_volume import (
            build_communication_volumes,
        )
        from .visualization_3d_sites import communication_units

        metric_crs = str(_first(query, "metric_crs") or "EPSG:32651")
        azimuth_segments = _parse_int(query, "azimuth_segments", 72)
        vertical_segments = _parse_int(query, "vertical_segments", 12)
        surface_class = _first(query, "surface_class")

        units = communication_units(self.state)
        result = build_communication_volumes(
            units, metric_crs=metric_crs, azimuth_segments=azimuth_segments,
            vertical_segments=vertical_segments, surface_class=surface_class)
        route_intersection = self._route_intersection_block()
        for volume in result["volumes"]:
            volume["route_intersection"] = route_intersection

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **self._scope_block(),
            "vertical_reference": self._vertical_reference_block(),
            "layer_id": "communication_volumes",
            "label": "通信三维工程几何包络",
            "coverage_semantics": "engineering_geometric_coverage_not_measured",
            "coverage_semantics_text": (
                "工程**几何**包络：只表达距离几何，不保证实测接收能力；"
                "遮挡、地形 LOS、绕射、干扰、链路预算、容量、吞吐与时延一律未评估"
            ),
            "model_scope": "geometric_only",
            "geometry_model": "omnidirectional_hemisphere_slant_range",
            "slant_range_is_criterion": True,
            "two_dimensional_disk_is_not_the_volume": True,
            "metric_crs": metric_crs,
            "default_contract_radius_m": 4000.0,
            "radius_authority": (
                "device_catalog[].coverage_geometry.radius_by_surface "
                "（经 domain.cns_service_contract.effective_radius_m 解析）"
            ),
            "units_total": len(units),
            **result,
            "route_intersection": route_intersection,
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    def rid_footprints(self, query=None):
        """``GET /api/visualization-3d/rid-footprints`` 的只读投影。

        RID 陆海**非正圆**二维 footprint：逐方位角调用**权威**陆海分类器，
        边界半径完全由空间分类结果决定（陆/海岸 2000 m、海 5000 m、``unknown``
        立即终止且不产生几何）。三维中只展示有依据的高度切片，**绝不**把二维轮廓
        拉伸成确定高度的完整覆盖体，也**绝不**表现成 Radar 方向性扇区。
        """

        query = query or {}
        from .visualization_3d_rid_footprint import build_rid_footprints
        from .visualization_3d_sites import rid_units

        metric_crs = str(_first(query, "metric_crs") or "EPSG:32651")
        azimuth_segments = _parse_int(query, "azimuth_segments", 72)
        radial_step_m = _first(query, "radial_step_m")
        try:
            radial_step = float(radial_step_m) if radial_step_m else 25.0
        except (TypeError, ValueError):
            radial_step = 25.0

        policy = self.state.get("surface_classification_policy") or {}
        facts = self.state.get("surface_class_facts") or {}
        units = rid_units(self.state, rid_radius_by_surface=RID_RADIUS_BY_SURFACE)
        classifier, classifier_error = self._surface_class_provider()
        result = build_rid_footprints(
            units, classifier, metric_crs=metric_crs,
            land_radius_m=RID_RADIUS_BY_SURFACE["land"],
            sea_radius_m=RID_RADIUS_BY_SURFACE["sea"],
            azimuth_segments=azimuth_segments, radial_step_m=radial_step,
            surface_class_facts_fingerprint=facts.get("input_fingerprint"))
        route_intersection = self._route_intersection_block()
        for item in result["footprints"]:
            item["route_intersection"] = route_intersection

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **self._scope_block(),
            "vertical_reference": self._vertical_reference_block(),
            "layer_id": "rid_footprints",
            "label": "RID 陆海非正圆覆盖（合作监视工程几何）",
            "coverage_semantics": (
                "engineering_geometric_cooperative_surveillance_not_measured"
            ),
            "coverage_semantics_text": (
                "合作监视**工程几何**：不保证实测接收能力；"
                "地形/建筑遮挡、RF 传播、接收机灵敏度、报文冲突与干扰一律未评估"
            ),
            "model_scope": "geometric_only",
            "geometry_model": "surface_partitioned_omnidirectional_2d",
            "not_a_radar_sector": True,
            "forbidden_geometry": list(RID_FORBIDDEN_GEOMETRY),
            "omnidirectional": True,
            "horizontal_coverage_deg": 360.0,
            "metric_crs": metric_crs,
            "radius_by_surface_m": dict(RID_RADIUS_BY_SURFACE),
            "radius_authority": (
                "domain.cns_service_contract.RID_RADIUS_BY_SURFACE（冻结工程规划基线）"
            ),
            "land_mask": {
                "status": "available" if classifier is not None else "blocked",
                "error": classifier_error,
                "authority": (
                    "cns_planner.gis.radar_layout_adapter.LandMaskSource.classify_many"
                ),
                "classification_algorithm_copied": False,
                "policy": {
                    "land_mask_layer_name": policy.get("land_mask_layer_name"),
                    "coastal_uncertainty_buffer_m": policy.get(
                        "coastal_uncertainty_buffer_m"),
                    "semantics": policy.get("semantics"),
                    "unknown_is_fail_closed": policy.get("unknown_is_fail_closed"),
                },
                "surface_class_facts": {
                    "status": facts.get("status"),
                    "input_fingerprint": facts.get("input_fingerprint"),
                    "surface_class_counts": facts.get("surface_class_counts"),
                    "classified_grid_cell_count": facts.get("classified_grid_cell_count"),
                },
            },
            "vertical": {
                "status": "slice_only",
                "vertical_effective_range": "not_modelled",
                "extrusion_forbidden": True,
                "note": (
                    "RID 没有已验证的垂直有效范围 ⇒ 只展示有依据的高度切片，"
                    "**绝不**把二维轮廓拉伸成确定高度的完整覆盖体"
                ),
            },
            "units_total": len(units),
            **result,
            "route_intersection": route_intersection,
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    def _route_intersection_block(self):
        """与当前 R0005 航路的相交关系（**只有指纹匹配才允许绑定历史覆盖结果**）。

        这是任务硬要求：``coverage_3d`` 的历史结果只有在
        ``route_geometry_identity`` 匹配时才可绑定；不匹配时如实报告
        ``binding_allowed = false`` 与原因，**绝不**把旧结果套到新几何上。
        """

        demo = self._demo_route_entry()
        coverage = self.state.get("coverage_3d") or {}
        coverage_fingerprint = coverage.get("input_fingerprint")
        block = {
            "route_id": (demo or {}).get("route_id"),
            "authority": (demo or {}).get("authority"),
            "geometry_fingerprint": ((demo or {}).get("identity") or {}).get("fingerprint"),
            "coverage_3d_status": coverage.get("status"),
            "coverage_3d_input_fingerprint": coverage_fingerprint,
            "coverage_3d_algorithm_id": coverage.get("algorithm_id"),
            "historical_continuous_coverage_binding": "not_bound",
            "binding_allowed": False,
            "binding_rule": (
                "只有 coverage_3d 的输入指纹能证明与当前 route_geometry 身份一致时，"
                "才允许绑定历史连续覆盖结果"
            ),
            "reason": None,
            "note": (
                "本服务**不重跑**覆盖算法；这里只报告相交关系所需身份，"
                "并如实说明历史结果是否可绑定"
            ),
        }
        if demo is None:
            block["reason"] = "no_engineering_demo_route_available"
            return block
        if not coverage_fingerprint:
            block["reason"] = "coverage_3d_not_calculated"
            return block
        # coverage_3d 的输入指纹是"路由 + spatial_3d + terrain + providers + 参数"的
        # 组合哈希，**不是** route_geometry 身份；因此这里绝不把它当作几何身份使用。
        block["reason"] = "coverage_3d_input_fingerprint_is_not_a_route_geometry_identity"
        block["coverage_3d_routes"] = [
            {"route_id": item.get("route_id"), "status": item.get("status")}
            for item in (coverage.get("routes") or [])
        ]
        return block

    # ------------------------------------------------------------------ routes

    def routes(self, query=None):
        query = query or {}
        scope_block = self._scope_block()
        demo = self._demo_route_entry()
        canonical = self._canonical_route_entries()
        conflicts = self._mileage_conflicts()

        entries = []
        if demo:
            entries.append(demo)
        entries.extend(canonical)

        want = str(_first(query, "authority") or "").strip()
        if want:
            entries = [entry for entry in entries if entry["authority"] == want]
        route_id = str(_first(query, "route_id") or "").strip()
        if route_id:
            entries = [entry for entry in entries if str(entry["route_id"]) == route_id]

        for entry in entries:
            if entry["identity"]["point_count"] > MAX_ROUTE_POINTS:
                entry["truncated"] = True
                entry["path"] = entry["path"][:MAX_ROUTE_POINTS]
                entry["identity"]["point_count"] = len(entry["path"])
                entry["notes"] = list(entry.get("notes") or []) + [
                    f"折线超过 {MAX_ROUTE_POINTS} 点，已截断并显式标记（绝不静默截断）"
                ]

        demo_conflict = [c for c in conflicts if c["severity"] == "high"]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **scope_block,
            "vertical_reference": self._vertical_reference_block(),
            "routes": entries,
            "route_count": len(entries),
            "mileage_contract": {
                "canonical_metric": LENGTH_METRIC,
                "implementation": LENGTH_IMPLEMENTATION,
                "geometry_binding_required": True,
                "cross_geometry_reuse_forbidden": True,
                "interpolation": "equirectangular_segment_cumulative_linear_clamp",
                "reason": (
                    "同一 route_id 在状态中存在 7/11/14 点三份不同几何；"
                    "长度只在同一 route_geometry fingerprint 内可比"
                ),
            },
            "mileage_conflicts": conflicts,
            "conflict_count": len(conflicts),
            "high_severity_conflict_count": len(demo_conflict),
            "read_only": True,
            "writes_performed": 0,
        }

    # --------------------------------------------------------- terrain-window

    @staticmethod
    def _source_call(default_code, action):
        """把 ``gis/`` 适配器的失败统一翻译成 :class:`Visualization3DUnavailable`。

        ``gis/`` 层有自己的异常类型（``TerrainWindowUnavailable`` /
        ``GeoidUnavailable``）。服务层必须把它们翻成**同一族**错误，否则路由只能
        落到 500，而"参数非法/窗口超限"本应是 400。
        """

        try:
            return action()
        except Visualization3DUnavailable:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None) or default_code
            detail = getattr(exc, "detail", None)
            raise Visualization3DUnavailable(
                str(code), str(exc), **(detail if isinstance(detail, dict) else {})
            ) from exc

    def terrain_window(self, query=None):
        """有界地形窗口。返回 ``(payload, content_type, etag_payload)``。"""

        query = query or {}
        if self.terrain_source is None:
            raise Visualization3DUnavailable(
                "terrain_source_unavailable", "FABDEM 地形源不可用，无法提供地形窗口"
            )
        bbox = _parse_bbox(query)
        width = _parse_int(query, "width", 256)
        height = _parse_int(query, "height", 256)
        want_geoid = str(_first(query, "geoid", "1")).strip().lower() not in ("0", "false", "no")
        fmt = str(_first(query, "format", "json")).strip().lower()

        with self._source_lock:
            window = self._source_call(
                "terrain_window_failed",
                lambda: self.terrain_source.window(bbox, width, height),
            )
        geoid_block = None
        if want_geoid:
            if self.geoid is not None:
                with self._source_lock:
                    geoid_window = self._source_call(
                        "egm2008_window_failed",
                        lambda: self.geoid.window(bbox, width, height),
                    )
                geoid_identity = self.geoid.describe()
                geoid_block = {
                    "status": geoid_window["status"],
                    "grid": geoid_identity["filename"],
                    "sha256": geoid_identity["sha256"],
                    "backend": geoid_window.get("backend"),
                    "nodata_count": geoid_window["nodata_count"],
                    "min_m": _min_of(geoid_window["values"]),
                    "max_m": _max_of(geoid_window["values"]),
                    "mean_m": _mean_of(geoid_window["values"]),
                    "values": geoid_window["values"],
                }
            else:
                geoid_block = {
                    "status": "blocked",
                    "reason": (self.geoid_error or {}).get("code", "egm2008_grid_missing"),
                    "values": None,
                }

        meta = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            "scope": self.scope(),
            "window": {
                "bbox": window["bbox"],
                "width": window["width"],
                "height": window["height"],
                "row_order": window["row_order"],
                "status": window["status"],
                "nodata_count": window["nodata_count"],
                "nodata_value": window["nodata_value"],
                "nodata_policy": window["nodata_policy"],
                "valid_count": window["valid_count"],
                "min_m": window["min_m"],
                "max_m": window["max_m"],
                "mean_m": window["mean_m"],
            },
            "source_identity": {
                "state_key": "map_sources.terrain_dtm",
                "path": self.terrain_source.describe()["path"],
                "sha256": self.terrain_source.describe()["sha256"],
                "provenance_level": "production_readonly_source",
            },
            "vertical_reference": self._vertical_reference_block(),
            "geoid_offsets": geoid_block,
            "read_only": True,
            "writes_performed": 0,
        }
        etag = stable_fingerprint({
            "bbox": window["bbox"], "w": window["width"], "h": window["height"],
            "revision": self._revision(),
            "terrain_sha256": self.terrain_source.describe()["sha256"],
            "geoid_sha256": (self.geoid.describe()["sha256"] if self.geoid else None),
        }, prefix="terrainwindowv1-")

        if fmt in ("f32", "binary", "bin"):
            body = _encode_f32(window["values"])
            meta["window"]["encoding"] = "float32_little_endian"
            meta["window"]["values_omitted"] = True
            if geoid_block and geoid_block.get("values"):
                meta["geoid_offsets"]["encoding"] = "float32_little_endian"
                meta["geoid_offsets"]["values_omitted"] = True
            header = {
                "schema_version": SCHEMA_VERSION,
                "etag": etag,
                "window": meta["window"],
                "source_identity": meta["source_identity"],
                "vertical_reference": meta["vertical_reference"],
                "geoid_offsets_summary": (
                    {k: v for k, v in (geoid_block or {}).items() if k != "values"}
                    if geoid_block else None
                ),
                "revision": self._revision(),
                "scope": self.scope(),
                "read_only": True,
            }
            import json as _json
            head = _json.dumps(header, ensure_ascii=False).encode("utf-8")
            body = struct.pack("<I", len(head)) + head + body
            if geoid_block and geoid_block.get("values"):
                body += _encode_f32(geoid_block["values"])
            return body, "application/octet-stream", etag

        meta["etag"] = etag
        import json as _json
        return _json.dumps(meta, ensure_ascii=False).encode("utf-8"), \
            "application/json; charset=utf-8", etag


# ------------------------------------------------------------------ 工具函数


def _first(query, key, default=None):
    value = query.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return default if value is None else value


def _parse_int(query, key, default):
    raw = _first(query, key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        raise Visualization3DUnavailable(
            "terrain_window_parameter_invalid", f"参数 {key} 不是整数：{raw!r}"
        )


def _parse_bbox(query):
    raw = _first(query, "bbox")
    if raw:
        parts = [p for p in str(raw).replace(";", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise Visualization3DUnavailable(
                "terrain_window_bbox_invalid", f"bbox 必须是 west,south,east,north：{raw!r}"
            )
        try:
            return [float(p) for p in parts]
        except ValueError as exc:
            raise Visualization3DUnavailable(
                "terrain_window_bbox_invalid", f"bbox 不是数值：{raw!r}"
            ) from exc
    keys = ("west", "south", "east", "north")
    values = [_first(query, key) for key in keys]
    if all(v is not None and v != "" for v in values):
        try:
            return [float(v) for v in values]
        except ValueError as exc:
            raise Visualization3DUnavailable(
                "terrain_window_bbox_invalid", f"bbox 分量不是数值：{values!r}"
            ) from exc
    raise Visualization3DUnavailable(
        "terrain_window_bbox_missing",
        "缺少 bbox（用 bbox=west,south,east,north 或 west/south/east/north 提供）",
    )


def _encode_f32(values):
    flat = []
    for row in values:
        for value in row:
            flat.append(float("nan") if value is None else float(value))
    return struct.pack(f"<{len(flat)}f", *flat)


# ------------------------------------------------------- 进程级只读源装配


#: 进程级**实测**计数：本服务进程收到过多少次 ``/api/workflow`` 全量快照（147 MB）。
#: manifest 的 ``load_budget.workflow_endpoint_used`` 必须读它，而不是硬编码
#: ``False``——硬编码的"否"证明不了任何事；只有实测计数才能在真的被触碰时报出来。
_WORKFLOW_SNAPSHOT_REQUESTS = 0
_WORKFLOW_SNAPSHOT_LOCK = threading.Lock()


def record_workflow_snapshot_request():
    """由 API 层在真实分发 ``/api/workflow`` 时调用（纯计数，不写 ProjectState）。"""

    global _WORKFLOW_SNAPSHOT_REQUESTS
    with _WORKFLOW_SNAPSHOT_LOCK:
        _WORKFLOW_SNAPSHOT_REQUESTS += 1


def workflow_snapshot_requests_observed():
    """本进程已观测到的 ``/api/workflow`` 请求数（只读）。"""

    with _WORKFLOW_SNAPSHOT_LOCK:
        return _WORKFLOW_SNAPSHOT_REQUESTS


_SOURCE_CACHE: dict = {}
_SOURCE_LOCK = threading.RLock()


def terrain_path_of(data):
    """从 MapData 取 FABDEM 路径（只读，绝不改写配置）。"""

    paths = getattr(data, "paths", None) or {}
    value = paths.get("terrain_dtm")
    return str(value) if value else None


def sources(terrain_path):
    """惰性装配只读地形/geoid 源（进程级缓存；失败原因也缓存，不反复重试）。"""

    key = str(terrain_path or "")
    with _SOURCE_LOCK:
        if key in _SOURCE_CACHE:
            return _SOURCE_CACHE[key]
        terrain = terrain_error = None
        if terrain_path:
            try:
                from ..gis.terrain_window_adapter import TerrainWindowSource
                terrain = TerrainWindowSource(terrain_path)
            except Exception as exc:  # 只读适配失败不该让整个三维不可用
                terrain_error = {
                    "code": getattr(exc, "code", "terrain_source_unavailable"),
                    "message": str(exc),
                }
        else:
            terrain_error = {"code": "terrain_dtm_not_configured",
                             "message": "未配置 FABDEM terrain_dtm"}
        try:
            from ..gis.egm2008_geoid import load_geoid
            geoid, geoid_error = load_geoid()
        except Exception as exc:  # pragma: no cover - 环境级失败
            geoid, geoid_error = None, {"code": "egm2008_grid_open_failed", "message": str(exc)}
        _SOURCE_CACHE[key] = (terrain, terrain_error, geoid, geoid_error)
        return _SOURCE_CACHE[key]


def _terrain_water_mask_available(terrain_tile_source):
    """水 mask 是否真的可用（与 ``layer.json`` 的 ``extensions`` 同源判定）。"""

    if terrain_tile_source is None:
        return False
    probe = getattr(terrain_tile_source, "_water_mask_status", None)
    if not callable(probe):
        return False
    try:
        return probe() == "available_from_cartographic_mask"
    except Exception:  # pragma: no cover - 只读探测不该让 manifest 失败
        return False


def _water_mask_source(context, data):
    """惰性装配只读水掩膜源（进程级缓存；失败也缓存，不反复重试）。

    水掩膜只是**显示增强**：任何失败都必须降级为「无 water mask」，绝不能让它把
    地形瓦片整体拖垮。它只读 canonical ``land_mask`` 矢量源，不写任何状态、不参与
    规划判定（``land_mask`` 是 surface classification 的权威来源，这里只是把同一份
    事实用于水面渲染，不反向影响它）。
    """

    cached = getattr(context, "_visualization_3d_water_mask", "unset")
    if cached != "unset":
        return cached
    result = (None, {"code": "land_mask_not_configured", "message": "未配置陆域掩膜"})
    paths = getattr(data, "paths", None) or {}
    land_mask_path = paths.get("land_mask")
    if land_mask_path:
        try:
            from ..gis.water_mask_adapter import build_water_mask_source
            source, error = build_water_mask_source(land_mask_path)
            result = (source, error) if source is not None else (None, error)
        except Exception as exc:  # pragma: no cover - 环境级失败
            result = (None, {"code": "water_mask_source_unavailable", "message": str(exc)})
    try:
        setattr(context, "_visualization_3d_water_mask", result)
    except Exception:  # pragma: no cover - 只读上下文可能拒绝挂载
        pass
    return result


def service_for(context):
    """为一次只读请求装配 :class:`Visualization3DService`（零写入）。"""

    workflow = getattr(context, "workflow", None)
    if workflow is None:
        raise Visualization3DUnavailable("workflow_unavailable", "工作台状态尚未装配")
    state = workflow.state
    data = getattr(context, "data", None)
    terrain_path = terrain_path_of(data)
    terrain, terrain_error, geoid, geoid_error = sources(terrain_path)
    from .visualization_3d_imagery_service import service_for as imagery_service_for
    terrain_tile_source = getattr(context, "_visualization_3d_terrain_tile_source", None)
    relief_tile_source = getattr(context, "_visualization_3d_relief_tile_source", None)
    # 地形瓦片、晕渲瓦片与 terrain-window 接口读的是**同一批** GDAL/PROJ 句柄，
    # 必须共用**同一把**锁。锁与 provider 一起挂在 context 上，保证复用缓存时
    # 拿到的仍是同一对象（否则 service 的新锁与 provider 的旧锁又不互斥）。
    source_lock = getattr(context, "_visualization_3d_source_lock", None)
    if source_lock is None:
        source_lock = threading.RLock()
        try:
            setattr(context, "_visualization_3d_source_lock", source_lock)
        except Exception:  # pragma: no cover - 只读上下文可能拒绝挂载
            pass
    if terrain_tile_source is None and terrain is not None and geoid is not None:
        from ..gis.terrain_tile_adapter import TerrainTileSource
        water_mask, _water_mask_error = _water_mask_source(context, data)
        terrain_tile_source = TerrainTileSource(terrain, geoid,
                                                water_mask_source=water_mask,
                                                source_lock=source_lock)
        setattr(context, "_visualization_3d_terrain_tile_source", terrain_tile_source)
    if relief_tile_source is None and terrain is not None and geoid is not None:
        try:
            from ..gis.relief_tile_adapter import ReliefTileSource
            relief_tile_source = ReliefTileSource(terrain, geoid,
                                                  source_lock=source_lock)
            setattr(context, "_visualization_3d_relief_tile_source", relief_tile_source)
        except Exception:  # 晕渲只是显示增强，失败必须降级而不是让三维不可用
            relief_tile_source = None
    return Visualization3DService(
        state, terrain_source=terrain, terrain_tile_source=terrain_tile_source,
        geoid=geoid, geoid_error=geoid_error,
        imagery_service=imagery_service_for(context),
        relief_tile_source=relief_tile_source,
        source_lock=source_lock,
        data=data,
        state_identity={
            "state_key": "workflow.state",
            "terrain_path": terrain_path,
            "terrain_error": terrain_error,
            "accepts_writes": False,
        },
    )


def _flatten(values):
    return [value for row in values for value in row if value is not None]


def _min_of(values):
    flat = _flatten(values)
    return min(flat) if flat else None


def _max_of(values):
    flat = _flatten(values)
    return max(flat) if flat else None


def _mean_of(values):
    flat = _flatten(values)
    return (sum(flat) / len(flat)) if flat else None
