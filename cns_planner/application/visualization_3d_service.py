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
import re
import struct
import threading
from copy import deepcopy
from datetime import datetime, timezone

from ..domain.geodesy import distance_m
from ..domain.cns_service_contract import RID_FORBIDDEN_GEOMETRY, RID_RADIUS_BY_SURFACE
from ..domain.layered_route import stable_fingerprint
from ..domain.layered_route_validation import path_fingerprint
from ..domain.spatial_3d import (
    TERMINAL_TRANSITION_IDENTITY_VERSION, terminal_transition_identity_reasons,
)
from ..domain.radar_surveillance_layout import NOT_EVALUATED as RADAR_NOT_EVALUATED
from ..domain.radar_surveillance_layout import RADAR_TYPES

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
                 relief_tile_source=None, source_lock=None, data=None,
                 shared_sites=None, fixed_corridor_service=None):
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
        #: Round38-T3D4：共享候选站址只读快照
        #: （``CNSSharedSiteCandidateService.snapshot()``）。它是站址身份的**唯一**
        #: 来源；为 ``None`` 时一律如实披露"未接线"，绝不自行枚举站址。
        self.shared_sites = shared_sites if isinstance(shared_sites, dict) else {}
        #: Round38-T3D4：固定 1000 m 保障宽带的**二维权威投影服务**。三维层直接
        #: 委托它解析航路与 P14 口径的 ``route_authority``，绝不另写一套口径
        #: （两套口径会造成"重建指纹 ≠ P14 条目指纹"的假阳性 stale）。
        self.fixed_corridor_service = fixed_corridor_service
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
        p17 = self.state.get("continuous_service_acceptability") or {}
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
            "continuous_service_acceptability": {
                "status": p17.get("status"),
                "algorithm_version": p17.get("algorithm_version"),
                "input_fingerprint": p17.get("input_fingerprint"),
            },
            "canonical_route_fingerprints": [
                entry["identity"]["fingerprint"] for entry in self._canonical_route_entries()
            ],
            "tower_count": len(_items(self.state.get("towers"))),
            "tower_profile_count": len(_items(self.state.get("tower_obstacle_profiles"))),
            "device_count": len(_items(self.state.get("device_catalog"))),
            "facility_count": len(_items(self.state.get("existing_cns_facilities"))),
            # Round38-T3D4：站址身份与固定宽带身份都必须进来源种子，否则站址 / 当前性
            # 一变而 ETag 不变，浏览器会拿 304 复用旧图层。两者都只取**廉价**身份
            # 摘要：绝不在 ETag 路径上做几何构造或地形采样。
            "shared_site_identity": self.shared_site_identity(),
            "fixed_corridor_identity": self.fixed_corridor_identity(),
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

    # ------------------------------------------------- Round38-T3D4 身份摘要

    def shared_site_identity(self):
        """共享候选站址的**廉价**身份摘要（不做几何构造、不做地形采样）。

        它是 ETag 来源种子的一部分：站址集合或空间资格一变，三维站址图层的 ETag
        必须跟着变，否则浏览器会拿 304 复用旧图层（"数据变了图没变"）。
        未接线时如实给出 ``available=False`` 与原因，绝不静默当成"没有站址"。
        """

        snapshot = self.shared_sites or {}
        candidates = snapshot.get("candidates")
        if not isinstance(candidates, list):
            return {
                "available": False,
                "status": snapshot.get("status"),
                "reason": "shared_site_candidates_unavailable",
                "count": 0,
                "fingerprint": None,
            }
        from .radar_shared_site_adapter import candidate_site_fingerprint_component

        component = candidate_site_fingerprint_component(snapshot)
        counts = snapshot.get("counts") or {}
        return {
            "available": True,
            "status": snapshot.get("status"),
            "reason": None,
            "count": component.get("count"),
            "fingerprint": component.get("fingerprint"),
            "counts": {
                "total": counts.get("total"),
                "towers": counts.get("towers"),
                "nodes": counts.get("nodes"),
                "reference_landing_sites": counts.get("reference_landing_sites"),
                "existing_cns_facilities": counts.get("existing_cns_facilities"),
                "spatial_candidates": counts.get("spatial_candidates"),
                "spatial_blocked": counts.get("spatial_blocked"),
            },
        }

    def fixed_corridor_identity(self):
        """固定 1000 m 保障宽带的**宽度契约身份**（唯一权威：Domain 层常量）。"""

        from ..gis.figure_currentness import fixed_corridor_width_identity

        return fixed_corridor_width_identity()

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
                # L1：建筑高度证据等级不升级
                "建筑 4tCa7 高度证据为 ML 预测，采用工程条件接受，不构成严格安全验证通过。",
                # L2：固定巡航高度假设 + 起降过渡未验证
                "当前航路使用 EGM2008 正高 100 m 恒定巡航高度假设。真实离场爬升和进场下降"
                "程序尚未配置及验证；当前建筑净空结果不代表起降过渡阶段安全。",
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
            #: Round38-FC30：canonical **完整几何身份**。与 N1 导航监测仿真、
            #: 固定 1000 m 保障宽带、Route3DProfile 使用同一实现（``path_fingerprint``）
            #: 与同一取值；``fingerprint`` 是本模块含 candidate/validation 身份的
            #: ``routegeometryv1-`` 家族，两者语义不同、都保留，绝不互相替代。
            "route_geometry_fingerprint": path_fingerprint(path),
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

    def _terminal_transition(self, route_id):
        """该航路的进离场/巡航衔接状态（只读投影，与 Route3DProfile 共用同一判据）。

        Round38-FC30：FIG 与 T3D4 不得各自按 ``route_id`` 重新拼接一套进离场关联。
        这里给出唯一来源：该航路当前的巡航高度层、canonical 完整几何指纹，以及离场/
        进场航段各自的衔接判定与理由。
        """

        spatial = self.state.get("spatial_3d") or {}
        assignment = None
        for item in _items(spatial.get("route_operating_layers")):
            if (isinstance(item, dict) and str(item.get("route_id")) == str(route_id)
                    and item.get("active") is not False):
                assignment = item
                break
        route = next((
            item for item in _items(self.state.get("operational_routes"))
            if isinstance(item, dict) and str(item.get("route_id")) == str(route_id)
        ), None)
        path = [
            point for point in (route or {}).get("path") or []
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        expected = path_fingerprint(path) if len(path) >= 2 else None
        route_layer_id = (assignment or {}).get("altitude_layer_id")
        transitions = {}
        for procedure_type in ("departure", "arrival"):
            procedure = next((
                item for item in _items(spatial.get("departure_arrival_procedures"))
                if isinstance(item, dict)
                and str(item.get("route_id")) == str(route_id)
                and str(item.get("procedure_type")) == procedure_type
            ), None)
            if procedure is None:
                transitions[procedure_type] = {
                    "procedure_id": None,
                    "status": "not_configured",
                    "confirmed": False,
                    "altitude_layer_id": None,
                    "route_geometry_fingerprint": None,
                    "identity_reasons": [f"尚未配置 {procedure_type} 进离场航段"],
                    "is_current_part_of_route": False,
                }
                continue
            reasons = terminal_transition_identity_reasons(
                procedure,
                expected_route_geometry_fingerprint=expected,
                route_altitude_layer_id=route_layer_id,
            )
            confirmed = bool(procedure.get("confirmed")) and str(
                procedure.get("status")
            ) == "confirmed"
            transitions[procedure_type] = {
                "procedure_id": procedure.get("procedure_id"),
                "status": procedure.get("status"),
                "confirmed": bool(procedure.get("confirmed")),
                "altitude_layer_id": procedure.get("altitude_layer_id"),
                "route_geometry_fingerprint": procedure.get("route_geometry_fingerprint"),
                "identity_reasons": reasons,
                "is_current_part_of_route": bool(confirmed and not reasons),
            }
        return {
            "identity_version": TERMINAL_TRANSITION_IDENTITY_VERSION,
            "route_altitude_layer_id": route_layer_id,
            "route_geometry_fingerprint": expected,
            "departure": transitions["departure"],
            "arrival": transitions["arrival"],
            "current_part_of_route": bool(
                transitions["departure"]["is_current_part_of_route"]
                and transitions["arrival"]["is_current_part_of_route"]
            ),
            "semantics": (
                "进离场航段只有在绑定当前巡航航路的完整几何指纹、且其高度层与航路当前巡航层"
                "一致时，才属于 current 航路；仅 route_id 相同不构成同一版本。"
            ),
            "read_only": True,
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
            "terminal_transition": self._terminal_transition(route_id),
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
                "terminal_transition": self._terminal_transition(route_id),
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
            {
                "id": "protection_volumes",
                "label": "P17 航路保护走廊",
                "status": "available",
                "reserved": False,
                "default_on": False,
                "count": len(_items(
                    (self.state.get("continuous_service_acceptability") or {}).get("routes"))),
                "object_id_prefix": OBJECT_ID_PREFIXES["protection_corridor"],
                "geometry_semantics": "p17_horizontal_corridor_with_evidenced_vertical_policy",
                "source_identity": {
                    "state_key": "continuous_service_acceptability.routes[].corridor",
                    "provenance_level": "production_container_readonly_historical_allowed",
                    "radius_authority": "corridor.outer_half_width_m",
                },
                "detail_endpoint": "/api/visualization-3d/protection-volumes",
                "notes": [
                    "历史 P17 只有与路线几何身份精确匹配时才显示",
                    "缺少确认上下界时只显示水平边界和名义高度切片",
                    "工程示意必须由用户显式选择路线并输入宽度和上下高度",
                    "本条是**航路保护区**（业务/法规与响应距离语义），"
                    "与固定 1000 m CNS 保障目标带是两件事，互不替代",
                    "固定宽带不是禁飞区、也不是入侵预警边界；三者必须分别表达",
                ],
                "items": [],
            },
            # Round38-T3D4：站址 / 设备四类身份分层（既有铁塔、起降点、既有设施）。
            {
                "id": "cns_site_identity",
                "label": "CNS 站址与设备身份（既有 / 规划选中 / 候选 / blocked 分层）",
                "status": (
                    "available" if self.shared_sites.get("candidates")
                    else "not_calculated"
                ),
                "reserved": False,
                "default_on": False,
                "count": (self.shared_sites.get("counts") or {}).get("total") or 0,
                "object_id_prefix": "site:",
                "geometry_semantics": "station_point_with_optional_evidenced_structure_height",
                "source_identity": {
                    "state_key": (
                        "shared_site_candidates + tower_obstacle_profiles + towers "
                        "+ existing_cns_facilities + radar_surveillance_layout "
                        "+ cns_corridor_site_plan"
                    ),
                    "provenance_level": "production_container_readonly",
                    "shared_site_fingerprint": self.shared_site_identity().get("fingerprint"),
                },
                "detail_endpoint": "/api/visualization-3d/cns-sites",
                "notes": [
                    "站址存在**绝不**推断设备已安装；候选站址不画成已布设设备",
                    "塔顶安装与地面站安装语义分别表达，地面站绝不继承塔顶高度",
                    "规划安装高度 ≠ 真实已安装高度",
                    "缺失高度一律 unknown，绝不填 0",
                ],
                "items": [],
            },
            # Round38-T3D4：固定 1000 m CNS 保障目标带（服务目标范围，不是覆盖结论）。
            {
                "id": "fixed_corridor_band",
                "label": "固定 1000 m CNS 保障目标带",
                "status": (
                    "available" if self.state.get("operational_routes")
                    else "not_calculated"
                ),
                "reserved": False,
                "default_on": False,
                "count": len([row for row in (self.state.get("operational_routes") or [])
                              if isinstance(row, dict)]),
                "object_id_prefix": OBJECT_ID_PREFIXES["protection_corridor"],
                "geometry_semantics": (
                    "fixed_1000m_service_target_band_horizontal_with_evidenced_vertical_slice"
                ),
                "source_identity": {
                    "state_key": "operational_routes + cns_corridor_assessment(P14)",
                    "provenance_level": "production_container_readonly",
                    "radius_authority": "fixed-1000m-v1（左右各 500 m）",
                    "geometry_authority": (
                        "cns_planner/gis/corridor_geometry.build_fixed_corridor_geometry"),
                    "currentness_authority": (
                        "cns_planner/gis/figure_currentness.fixed_corridor_currentness"),
                },
                "detail_endpoint": "/api/visualization-3d/fixed-corridor-bands",
                "notes": [
                    "只是 CNS 服务目标范围，**不代表**设备已满足覆盖",
                    "与 FIG 使用同一航路几何指纹、宽度版本与来源身份",
                    "非 current 时不得显示为正式当前保障结果（只能作明确标注的历史预览）",
                    "不得使用 P17 旧 outer_half_width_m 作为现行保障宽度",
                    "无可靠垂直上下界时只展示水平目标带，不伪造认证三维保障体积",
                ],
                "items": [],
            },
            {
                "id": "simulated_intrusion",
                "label": "模拟入侵目标（工程示意，非真实探测）",
                "status": "available",
                "reserved": False,
                "default_on": False,
                "count": 0,
                "object_id_prefix": OBJECT_ID_PREFIXES["simulated_intruder"],
                "geometry_semantics": "simulated_track_three_column",
                "source_identity": {
                    "state_key": None,
                    "provenance_level": "user_configured_engineering_scenario_preview",
                },
                "detail_endpoint": "/api/visualization-3d/intrusion-assessment",
                "notes": [
                    "轨迹与保护体由用户主动配置；恒定 simulated=true",
                    "不写 ProjectState、不进 fingerprint/staleness/readiness",
                    "监视探测恒为 not_evaluated：空间相交绝不被推导为已探测",
                    "水平投影相交、三维体进入、真实探测三者语义严格分离",
                ],
                "items": [],
            },
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
            # Round38-T3D4：站址身份与固定目标带的稳定入口（二维 / 三维 / 图件同源）。
            "sites_endpoint": "/api/visualization-3d/cns-sites",
            "fixed_corridor_bands_endpoint": (
                "/api/visualization-3d/fixed-corridor-bands"),
            "fixed_corridor_identity": self.fixed_corridor_identity(),
            "shared_site_source": self.shared_site_identity(),
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

    # ------------------------------------------------- Radar 方向性三维覆盖

    #: 设备参数示意预览的**常驻**免责声明（界面与响应都必须带同一句）。
    RADAR_PREVIEW_DISCLAIMER = "设备参数示意，非已采纳布设，非已确认探测覆盖"

    #: 预览允许使用未确认塔顶派生正高的**显式**声明值（由用户主动选择）。
    RADAR_PREVIEW_ORIGIN_TOWER_TOP = "tower_top_orthometric_preview"

    def radar_volumes(self, query=None):
        """``GET /api/visualization-3d/radar-volumes`` 的只读投影。

        两种**严格隔离**的模式：

        * ``mode=project_layout``（默认）：只用真实
          ``radar_surveillance_layout.items[].selected_panels`` 生成已选面阵；
        * ``mode=device_spec_preview``：与真实规划结果**完全隔离**的手动示意预览，
          由用户主动选择站位/型号/方位角后触发，不写 ProjectState、不进入规划结果、
          不进入服务覆盖统计、不进入 LOS 验收。
        """

        query = query or {}
        mode = str(_first(query, "mode") or "project_layout").strip().lower()
        if mode == self.RADAR_PREVIEW_MODE:
            return self._radar_device_spec_preview(query)
        return self._radar_project_layout(query)

    #: 预览模式标识（响应里必须原样回显，且真实模式绝不会带上它）。
    RADAR_PREVIEW_MODE = "device_spec_preview"

    def _radar_common_block(self, *, mode, geometry_scope):
        from .visualization_3d_radar_volume import (
            COVERAGE_SEMANTICS, FORBIDDEN_GEOMETRY, GEOMETRY_MODEL,
            LOS_STATUS_UNKNOWN, LOS_UNKNOWN_REASON,
        )

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **self._scope_block(),
            "vertical_reference": self._vertical_reference_block(),
            "mode": mode,
            "geometry_scope": geometry_scope,
            "layer_id": "radar_volumes",
            "label": "Radar 方向性三维覆盖（非合作监视工程几何）",
            "coverage_semantics": COVERAGE_SEMANTICS,
            "coverage_semantics_text": (
                "方向性**工程几何**楔体：只表达方位、仰角与斜距几何，"
                "不保证实测探测能力；地形 LOS、绕射、建筑遮挡、杂波、多径、"
                "干扰、雷达方程与 Pd-Pfa 曲线一律未评估"
            ),
            "model_scope": "geometric_only",
            "geometry_model": GEOMETRY_MODEL,
            "omnidirectional": False,
            "not_a_rid_footprint": True,
            "forbidden_geometry": list(FORBIDDEN_GEOMETRY),
            "not_evaluated": dict(RADAR_NOT_EVALUATED),
            "not_evaluated_source": (
                "domain.radar_surveillance_layout.NOT_EVALUATED（冻结契约）"
            ),
            "not_evaluated_semantics": (
                "not_evaluated ≠ 通过；几何覆盖不等于已确认探测覆盖"
            ),
            "los": {
                "status": LOS_STATUS_UNKNOWN,
                "reason": LOS_UNKNOWN_REASON,
                "terrain_los_evaluated": False,
                "visible_region_available": False,
                "note": (
                    "没有权威持久化的 Radar 地形 LOS 结果 ⇒ 遮挡状态恒为 unknown；"
                    "本服务不把未持久化的 LOS 推断写成 clear"
                ),
            },
            "geometry_semantics_separation": {
                "physical_maximum_coverage_wedge": True,
                "route_local_display_clipping_applied": False,
                "route_local_display_geometry_used": False,
                "los_confirmed_visible_region_used": False,
                "note": (
                    "物理最大覆盖楔体 ≠ 既有 display_geometry 的航路局部显示裁剪 "
                    "≠ 已由 LOS 确认的可见区域"
                ),
            },
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    def _radar_project_layout(self, query):
        """真实规划面阵：只消费权威 ``selected_panels``，缺证时 fail-closed。"""

        from .visualization_3d_radar_volume import MAX_PANELS, build_radar_volumes
        from .visualization_3d_sites import radar_units

        metric_crs = str(_first(query, "metric_crs") or "EPSG:32651")
        azimuth_segments = _parse_int(query, "azimuth_segments", None)
        elevation_segments = _parse_int(query, "elevation_segments", None)

        layout = self.state.get("radar_surveillance_layout") or {}
        units = radar_units(self.state)
        result = build_radar_volumes(
            units, metric_crs=metric_crs, azimuth_segments=azimuth_segments,
            elevation_segments=elevation_segments, geometry_scope="project_layout",
        )
        route_intersection = self._route_intersection_block()

        required_evidence = sorted({
            requirement
            for volume in result["volumes"]
            for requirement in (volume["geometry"].get("required_evidence") or [])
        })
        if not units:
            required_evidence = sorted(set(required_evidence) | {(
                "radar_surveillance_layout.items[].selected_panels"
                "（真实已选面阵；当前为空或未计算）"
            )})
        if not units or result["geometry_count"] == 0:
            status = "blocked" if (units or layout) else "not_calculated"
        else:
            status = "passed"

        statuses = sorted({str(unit.get("layout_item_status")) for unit in units})
        versions = sorted({str(unit.get("layout_algorithm_version")) for unit in units})
        stale_reasons = sorted({
            str(unit.get("layout_stale_reason")) for unit in units
            if unit.get("layout_stale_reason")
        })
        route_ids = sorted({str(unit.get("layout_route_id")) for unit in units})

        return {
            **self._radar_common_block(
                mode="project_layout", geometry_scope="project_layout"),
            "device_spec_preview": False,
            "status": status,
            "required_evidence": required_evidence,
            "project_layout": {
                "container_status": layout.get("status"),
                "container_count": layout.get("count") if isinstance(layout, dict) else None,
                "model_scope": layout.get("model_scope") if isinstance(layout, dict) else None,
                "proposal_only": layout.get("proposal_only") if isinstance(layout, dict) else None,
                "evaluated_at": layout.get("evaluated_at") if isinstance(layout, dict) else None,
                "selected_panel_count": len(units),
                "route_ids": route_ids,
                "item_statuses": statuses,
                "item_algorithm_versions": versions,
                "item_stale_reasons": stale_reasons,
                "item_stale": bool(stale_reasons) or statuses == ["stale"],
                "demo_preview_only": any(
                    unit.get("layout_demo_preview_only") for unit in units),
                "operationally_adopted": any(
                    unit.get("layout_operationally_adopted") for unit in units),
                #: 权威状态里 ``operationally_adopted=true`` 与 ``proposal_only=true``
                #: 同时存在。本服务**只原样回显**，绝不据此把结果读成"当前有效采纳"：
                #: 当前性一律以 item ``status`` / ``stale_reason`` 为准。
                "operationally_adopted_field_conflicts_with_proposal_only": bool(
                    any(unit.get("layout_operationally_adopted") for unit in units)
                    and (layout.get("proposal_only") is True
                         or any(unit.get("layout_proposal_only") for unit in units))
                ),
                "operationally_adopted_note": (
                    "该字段为权威状态原样回显；它**不**表示 selected_panels 已被采纳为"
                    "当前有效布设。当前性以 item status / stale_reason 为准。"
                ),
                "authority": (
                    "radar_surveillance_layout.items[].selected_panels"
                    "（已登记外置 scope，运行态由 restore_compacted_results 水合）"
                ),
                "origin_authority": (
                    "tower_obstacle_profiles.items[].tower_top_orthometric_m"
                    "（EGM2008 正高；confirmed 语义逐塔披露）"
                ),
                "stale_is_recomputed_from_current_inputs": True,
                "must_not_be_read_as": [
                    "已采纳布设（selected_panels 是 proposal_only 结果）",
                    "已确认探测覆盖（几何 ≠ 实测）",
                    "已通过地形 LOS 验收（status = unknown）",
                ],
            },
            "panel_source": "radar_surveillance_layout.items[].selected_panels",
            "panels_total": len(units),
            "panels_truncated": len(units) > MAX_PANELS,
            **result,
            "route_intersection": route_intersection,
        }

    def _radar_device_spec_preview(self, query):
        """设备参数示意预览：与真实规划结果**完全隔离**的手动预览。"""

        from .visualization_3d_radar_volume import (
            MAX_PANELS, ORIGIN_UNCONFIRMED, build_radar_volumes,
        )
        from .visualization_3d_sites import radar_panel_origin, tower_profile_index

        metric_crs = str(_first(query, "metric_crs") or "EPSG:32651")
        tower_id = str(_first(query, "tower_id") or "").strip()
        radar_type = str(_first(query, "radar_type") or "").strip()
        origin_mode = str(_first(query, "preview_origin_mode") or "").strip()
        azimuth_raw = _first(query, "azimuth_deg")
        try:
            azimuth = float(azimuth_raw) if azimuth_raw not in (None, "") else None
        except (TypeError, ValueError):
            azimuth = None

        index = tower_profile_index(self.state)
        options = []
        for key, profile in sorted(index.items()):
            if profile.get("tower_top_orthometric_m") in (None, ""):
                continue
            options.append({
                "tower_id": key,
                "name": profile.get("name"),
                "longitude": profile.get("longitude"),
                "latitude": profile.get("latitude"),
                "tower_top_orthometric_m": profile.get("tower_top_orthometric_m"),
                "tower_top_status": profile.get("tower_top_status"),
                "tower_confirmed": profile.get("confirmed") is True,
                "origin_status": ("confirmed" if profile.get("confirmed") is True
                                  else ORIGIN_UNCONFIRMED),
            })
        options = options[:MAX_PANELS]

        base = {
            **self._radar_common_block(
                mode=self.RADAR_PREVIEW_MODE, geometry_scope="device_spec_preview"),
            "device_spec_preview": True,
            "disclaimer": self.RADAR_PREVIEW_DISCLAIMER,
            "disclaimer_always_visible": True,
            "isolation": {
                "isolated_from_project_results": True,
                "written_to_project_state": False,
                "enters_planning_results": False,
                "enters_service_coverage_statistics": False,
                "enters_los_acceptance": False,
                "selected_panels_derived": False,
                "appears_in_project_layout_response": False,
                "note": (
                    "预览数据**不写入** ProjectState，也绝不进入真实规划面阵的 "
                    "API 响应或 selected_panels"
                ),
            },
            "preview_tower_options": options,
            "preview_tower_options_truncated": len(options) >= MAX_PANELS,
            "panels_total": 0,
            "panel_count": 0,
            "geometry_count": 0,
            "available_count": 0,
            "blocked_count": 0,
            "volumes": [],
        }

        missing_selection = []
        if not tower_id:
            missing_selection.append("tower_id（可靠站位）")
        if radar_type not in RADAR_TYPES:
            missing_selection.append("radar_type ∈ {radar_i, radar_ii}（真实型号）")
        if azimuth is None:
            missing_selection.append("azimuth_deg（预览方位角，0–360）")
        if missing_selection:
            base.update({
                "status": "not_calculated",
                "selection_required": True,
                "required_evidence": missing_selection,
                "selection_note": (
                    "设备参数示意必须由用户**主动**选择站位、雷达型号与预览方位角后触发"
                ),
            })
            return base

        profile = index.get(tower_id)
        if not isinstance(profile, dict):
            base.update({
                "status": "blocked", "selection_required": False,
                "reason": "preview_tower_not_found",
                "required_evidence": ["tower_obstacle_profiles.items[<tower_id>]（现有塔位）"],
            })
            return base

        origin, origin_status, origin_reason, origin_source, evidence = radar_panel_origin(
            profile)
        origin_values = {
            "service_origin_egm2008_m": origin,
            "origin_status": origin_status,
            "origin_reason": origin_reason,
            "origin_source": origin_source,
            "origin_evidence_level": evidence,
        }
        if origin is None:
            base.update({
                "status": "blocked", "selection_required": False,
                "reason": origin_reason or "tower_top_orthometric_m_not_resolved",
                "required_evidence": [
                    "tower_obstacle_profiles.items[%s].tower_top_orthometric_m（EGM2008 正高）"
                    % tower_id,
                    "高度未知时不得以 0 m 代替",
                ],
                **origin_values,
            })
            return base

        acknowledged = origin_mode == self.RADAR_PREVIEW_ORIGIN_TOWER_TOP
        if origin_status != "confirmed" and not acknowledged:
            base.update({
                "status": "blocked", "selection_required": True,
                "reason": "preview_origin_not_confirmed_needs_explicit_acknowledgement",
                "required_evidence": [
                    "已确认的设备安装高度（当前项目没有）",
                    "或显式声明 preview_origin_mode=%s（仅示意，非已确认安装高度）"
                    % self.RADAR_PREVIEW_ORIGIN_TOWER_TOP,
                ],
                "preview_origin_mode_required": self.RADAR_PREVIEW_ORIGIN_TOWER_TOP,
                **origin_values,
            })
            return base

        panel = {
            "panel_id": "PREVIEW-%s-%s-%.3f" % (tower_id, radar_type, azimuth),
            "tower_id": tower_id,
            "tower_name": profile.get("name"),
            "radar_type": radar_type,
            "azimuth_deg": azimuth,
            "declared_azimuth_deg": azimuth,
            "panel_half_width_deg": None,
            "longitude": profile.get("longitude"),
            "latitude": profile.get("latitude"),
            "coordinate_available": (
                profile.get("longitude") is not None
                and profile.get("latitude") is not None
            ),
            "tower_profile_found": True,
            "tower_confirmed": profile.get("confirmed") is True,
            "tower_top_status": profile.get("tower_top_status"),
            "tower_top_orthometric_m": profile.get("tower_top_orthometric_m"),
            "parameter_source": "device_spec_preview.user_selected",
            **origin_values,
        }
        result = build_radar_volumes(
            [panel], metric_crs=metric_crs,
            azimuth_segments=_parse_int(query, "azimuth_segments", None),
            elevation_segments=_parse_int(query, "elevation_segments", None),
            geometry_scope="device_spec_preview",
            allow_unconfirmed_origin=(origin_status == ORIGIN_UNCONFIRMED),
            preview=True,
        )
        base.update({
            "status": "passed" if result["geometry_count"] else "blocked",
            "selection_required": False,
            "preview_selection": {
                "tower_id": tower_id,
                "radar_type": radar_type,
                "azimuth_deg": azimuth,
                "preview_origin_mode": origin_mode or None,
                "user_selected": True,
            },
            "preview_origin_status": origin_status,
            "preview_origin_confirmed": origin_status == "confirmed",
            "preview_origin_acknowledged_by_user": bool(acknowledged),
            **origin_values,
            **result,
        })
        if not result["geometry_count"]:
            base["required_evidence"] = sorted({
                requirement
                for volume in result["volumes"]
                for requirement in (volume["geometry"].get("required_evidence") or [])
            })
        return base

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

    # ------------------------------------------------------- protection volumes

    def protection_volumes(self, query=None):
        """P17 历史结果与隔离工程示意的只读投影。"""

        from ..domain.cns_continuous_service import CONTINUOUS_SERVICE_ALGORITHM_VERSION
        from .visualization_3d_intrusion_contract import formal_intrusion_contract
        from .visualization_3d_protection_volume import (
            ProtectionVolumeInputError,
            build_engineering_preview,
            project_p17_protection_volumes,
        )

        query = query or {}
        demo = self._demo_route_entry()
        entries = ([demo] if demo else []) + self._canonical_route_entries()
        mode = str(_first(query, "mode") or "project_result").strip()
        if mode == "project_result":
            payload = project_p17_protection_volumes(
                self.state, entries,
                current_algorithm_version=CONTINUOUS_SERVICE_ALGORITHM_VERSION,
            )
            payload.update({
                "generated_at": _now(),
                "revision": self._revision(),
                **self._scope_block(),
                "vertical_reference": self._vertical_reference_block(),
                "intrusion_contract": formal_intrusion_contract(
                    self.state, payload.get("volumes") or []),
            })
            return payload
        if mode != "engineering_scenario_preview":
            raise Visualization3DUnavailable(
                "protection_volume_mode_invalid",
                "保护走廊模式无效",
                allowed_modes=["project_result", "engineering_scenario_preview"],
            )

        required_fields = (
            "route_geometry_fingerprint", "half_width_m",
            "lower_altitude_m", "upper_altitude_m",
        )
        route_options = [{
            "route_id": row.get("route_id"),
            "authority": row.get("authority"),
            "status": row.get("status"),
            "route_geometry_identity": row.get("identity"),
            "point_count": (row.get("identity") or {}).get("point_count"),
        } for row in entries]
        missing = [field for field in required_fields
                   if _first(query, field) in (None, "")]
        if missing:
            return {
                "schema_version": "visualization-3d-protection-volumes/v1",
                "mode": mode,
                "status": "not_calculated",
                "selection_required": True,
                "required_inputs": list(required_fields),
                "missing_inputs": missing,
                "default_values": {},
                "route_options": route_options,
                "volumes": [],
                "volume_count": 0,
                "disclaimer": "自定义保护区示意，非已确认 P17 结果",
                "revision": self._revision(),
                "read_only": True,
                "writes_performed": 0,
                "algorithms_invoked": [],
            }
        fingerprint = str(_first(query, "route_geometry_fingerprint") or "")
        route = next((row for row in entries
                      if str((row.get("identity") or {}).get("fingerprint")) == fingerprint), None)
        if route is None:
            raise Visualization3DUnavailable(
                "protection_volume_route_not_found",
                "未找到用户选择的航路几何身份",
                route_geometry_fingerprint=fingerprint,
            )
        try:
            preview = build_engineering_preview(
                route,
                half_width_m=_first(query, "half_width_m"),
                lower_altitude_m=_first(query, "lower_altitude_m"),
                upper_altitude_m=_first(query, "upper_altitude_m"),
            )
        except ProtectionVolumeInputError as exc:
            raise Visualization3DUnavailable(
                "protection_volume_preview_invalid", str(exc)) from exc
        return {
            "schema_version": "visualization-3d-protection-volumes/v1",
            "mode": mode,
            "status": "engineering_preview",
            "selection_required": False,
            "required_inputs": list(required_fields),
            "default_values": {},
            "route_options": route_options,
            "volumes": [preview],
            "volume_count": 1,
            "disclaimer": preview["disclaimer"],
            "revision": self._revision(),
            "vertical_reference": self._vertical_reference_block(),
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    # --------------------------------------------------- simulated intrusion

    def intrusion_assessment(self, query=None):
        """用户配置模拟轨迹 × 已解析保护体实例的只读事件判定。

        几何判定**只有** ``assess_simulated_track`` 一套实现；前端不得复制第二套
        Shapely 判定。输入恒为有界：节点数 / 时长 / 地理跨度 / 载荷字节数都有硬上限。

        ``detection_evidence`` 永远由服务端置空：客户端**不能**提交探测证据，
        因此监视探测恒为 ``not_evaluated``，空间相交绝不被推导成"已发现目标"。
        """

        from ..domain.cns_continuous_service import CONTINUOUS_SERVICE_ALGORITHM_VERSION
        from .visualization_3d_intrusion_contract import (
            TRACK_INPUT_LIMITS,
            IntrusionContractInputError,
            assess_simulated_track,
            decode_track_payload,
        )
        from .visualization_3d_protection_volume import (
            ProtectionVolumeInputError,
            build_engineering_preview,
            project_p17_protection_volumes,
        )

        query = query or {}
        mode = str(_first(query, "mode") or "engineering_scenario_preview").strip()
        if mode not in ("project_result", "engineering_scenario_preview"):
            raise Visualization3DUnavailable(
                "intrusion_mode_invalid", "模拟入侵模式无效",
                allowed_modes=["project_result", "engineering_scenario_preview"])
        query_bytes = sum(len(str(key)) + len(str(value)) for key, value in query.items())
        if query_bytes > TRACK_INPUT_LIMITS["max_payload_bytes"]:
            raise Visualization3DUnavailable(
                "intrusion_request_too_large",
                "模拟入侵请求超过 "
                f"{TRACK_INPUT_LIMITS['max_payload_bytes']} 字节上限")
        instance_id = str(_first(query, "render_instance_id") or "").strip()
        if not instance_id:
            raise Visualization3DUnavailable(
                "intrusion_binding_required",
                "必须显式绑定保护体实例键（render_instance_id）后才允许判定模拟轨迹")
        try:
            nodes = decode_track_payload(_first(query, "track"))
        except IntrusionContractInputError as exc:
            raise Visualization3DUnavailable(
                "intrusion_track_payload_invalid", str(exc)) from exc

        demo = self._demo_route_entry()
        entries = ([demo] if demo else []) + self._canonical_route_entries()
        if mode == "project_result":
            projected = project_p17_protection_volumes(
                self.state, entries,
                current_algorithm_version=CONTINUOUS_SERVICE_ALGORITHM_VERSION,
            )
            volume = next(
                (row for row in projected.get("volumes") or []
                 if str(row.get("render_instance_id")) == instance_id), None)
        else:
            required = ("route_geometry_fingerprint", "half_width_m",
                        "lower_altitude_m", "upper_altitude_m")
            missing = [field for field in required
                       if _first(query, field) in (None, "")]
            if missing:
                raise Visualization3DUnavailable(
                    "intrusion_preview_inputs_required",
                    "工程示意模式必须先显式选择航路并给出半宽与上下界",
                    missing_inputs=missing)
            fingerprint = str(_first(query, "route_geometry_fingerprint") or "")
            route = next((row for row in entries
                          if str((row.get("identity") or {}).get("fingerprint")) == fingerprint),
                         None)
            if route is None:
                raise Visualization3DUnavailable(
                    "intrusion_route_not_found", "未找到用户选择的航路几何身份",
                    route_geometry_fingerprint=fingerprint)
            try:
                volume = build_engineering_preview(
                    route,
                    half_width_m=_first(query, "half_width_m"),
                    lower_altitude_m=_first(query, "lower_altitude_m"),
                    upper_altitude_m=_first(query, "upper_altitude_m"),
                )
            except ProtectionVolumeInputError as exc:
                raise Visualization3DUnavailable(
                    "intrusion_preview_invalid", str(exc)) from exc
        if volume is None:
            raise Visualization3DUnavailable(
                "intrusion_protection_instance_not_found",
                "未找到与该实例键匹配的保护体；请重新选择对象后再播放",
                render_instance_id=instance_id)

        try:
            # detection_evidence 恒为 None：本服务不接受客户端提交的探测证据。
            assessment = assess_simulated_track(
                track_id=str(_first(query, "track_id") or "SIM-1"),
                track_nodes=nodes,
                protection_volume=volume,
                detection_evidence=None,
                binding={"render_instance_id": instance_id},
            )
        except IntrusionContractInputError as exc:
            raise Visualization3DUnavailable(
                "intrusion_assessment_rejected", str(exc)) from exc
        assessment.update({
            "revision": self._revision(),
            "generated_at": _now(),
            "track_input_limits": dict(TRACK_INPUT_LIMITS),
            "request_bytes": query_bytes,
            "detection_evidence_accepted_from_client": False,
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        })
        return assessment

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

    # --------------------------------- Round38-T3D4 站址 / 设备身份 / 固定目标带

    def _radar_site_inputs(self):
        """Radar 站址输入适配层的**只读**结果（含 blocked 站址与原因）。

        这是"起降点也可以作为 Radar 布设站址"以及"塔顶安装 / 地面站安装语义分别表达"
        的唯一来源：地面站高度必须来自既有显式安装高度证据，缺证即 blocked，
        **绝不**用塔顶兜底、绝不填 0。
        """

        from .radar_shared_site_adapter import radar_site_inputs

        if not self.shared_sites:
            return {
                "status": "unavailable",
                "reason": "shared_site_candidates_unavailable",
                "sites": [], "usable": [], "blocked": [],
                "statistics": {
                    "status": "unavailable",
                    "reason": "shared_site_candidates_unavailable",
                    "site_count": 0, "usable_count": 0, "blocked_count": 0,
                },
            }
        return radar_site_inputs(
            shared_snapshot=self.shared_sites,
            obstacle_profiles=self.state.get("tower_obstacle_profiles"),
            terrain_sampler=None,
        )

    def cns_sites(self, query=None):
        """三维 CNS 站址与设备身份的**只读分层投影**（C/N/RID/Radar 四类）。

        严格区分四类身份，绝不把所有候选都画成已布设设备：

        * ``existing_facility``  —— 既有真实且已显式确认的设施；
        * ``planned_selected``   —— 规划算法选中的待确认站址（**不是**已安装）；
        * ``candidate_unselected`` —— 位置可用的候选站址（**不含**设备）；
        * ``blocked``            —— 空间证据或安装高度证据缺失，fail-closed。

        设备几何（通信包络 / RID footprint / Radar 楔体）由既有 T3D-2A/2B 端点负责，
        本端点**只**给身份与高度证据，因此它既不重算覆盖，也不会伪造覆盖。
        """

        query = query or {}
        scope_block = self._scope_block()
        from .cns_3d_site_projection import (
            SERVICE_CATEGORY_COLORS, SERVICE_CATEGORY_LABELS,
            SITE_STATE_DISCLOSURE, SITE_STATE_LABELS, site_records,
        )

        shared_identity = self.shared_site_identity()
        radar_inputs = self._radar_site_inputs()
        projection = site_records(
            state=self.state,
            shared_snapshot=self.shared_sites,
            radar_inputs=radar_inputs,
        )

        sites = projection["sites"]
        want_state = str(_first(query, "site_state") or "").strip()
        if want_state:
            wanted = {token.strip() for token in want_state.split(",") if token.strip()}
            sites = [row for row in sites if row["site_state"] in wanted]
        want_category = str(_first(query, "service_category") or "").strip()
        if want_category:
            sites = [
                row for row in sites
                if want_category in (row.get("device_categories") or [])
                or row["site_state"] == want_category
            ]
        route_id = str(_first(query, "route_id") or "").strip()
        if route_id:
            # 航路过滤只保留与该航路有明确关系的站址；没有可核验关系时**返回空**
            # 而不是"看起来相关的都给你"。
            route_site_ids = self._route_related_site_ids(route_id)
            sites = [row for row in sites if row["site_id"] in route_site_ids]

        truncated = len(sites) > MAX_TOWERS
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **scope_block,
            "vertical_reference": self._vertical_reference_block(),
            "status": (
                "available" if sites else (
                    "unavailable" if not shared_identity["available"]
                    else "not_calculated"
                )
            ),
            "shared_site_source": shared_identity,
            "radar_site_input_status": (radar_inputs.get("statistics") or {}).get("status"),
            "sites": sites[:MAX_TOWERS],
            "truncated": truncated,
            "count": len(sites),
            "planned_devices": projection["planned_devices"],
            "blocked_sites": projection["blocked_sites"],
            "counts": projection["counts"],
            "service_categories": [
                {
                    "key": key,
                    "label": SERVICE_CATEGORY_LABELS[key],
                    "color": SERVICE_CATEGORY_COLORS[key],
                }
                for key in ("C", "N", "RID", "Radar", "other")
            ],
            "site_state_labels": deepcopy(SITE_STATE_LABELS),
            "site_state_disclosure": deepcopy(SITE_STATE_DISCLOSURE),
            "identity_rules": {
                "site_presence_never_implies_device": True,
                "candidate_never_drawn_as_installed": True,
                "tower_top_never_used_for_ground_site": True,
                "planned_installation_height_is_not_installed_height": True,
                "unknown_is_not_zero": True,
                "ground_site_requires_confirmed_installation_height": True,
            },
            "semantics": "read_only_site_identity_projection_not_device_coverage",
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    def _route_related_site_ids(self, route_id):
        """与某条航路**有可核验关系**的站址身份集合（没有关系就是空集）。

        可核验关系按以下优先级判定，绝不用"离航路近"这类推断来补齐：

        A. 动作自身显式 ``route_ids[]``；
        B. 动作自身显式 ``route_id``；
        C. 唯一且可追溯的 P16 计划级航路身份（``targets[].route_id`` 与
           ``before/after.routes[].route_id`` 的唯一集合）；
        D. 旧 ``action_id`` 命名契约兼容（deprecated，按 token 边界匹配）。

        显式字段与计划级身份冲突时 fail-closed；显式空关联（``route_ids=[]``）
        与字段缺失严格区分。``radar_surveillance_layout`` 条目上的显式
        ``route_id`` 仍是独立的既有证据路径。
        """

        requested = _text(route_id)
        related = set()
        if not requested:
            return related
        layout = self.state.get("radar_surveillance_layout")
        rows = layout.get("items") if isinstance(layout, dict) else layout
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            if _text(item.get("route_id")) != requested:
                continue
            for panel in item.get("selected_panels") or []:
                if isinstance(panel, dict) and panel.get("tower_id"):
                    related.add(f"tower-colocation:{panel['tower_id']}")
        container = self.state.get("cns_corridor_site_plan")
        actions = container.get("selected_actions") if isinstance(container, dict) else None
        plan_route_ids = self._plan_level_route_ids(container)
        from .cns_3d_site_projection import canonical_site_key

        for action in actions or []:
            if not isinstance(action, dict):
                continue
            if not self._action_relates_to_route(action, requested, plan_route_ids):
                continue
            host = action.get("host") if isinstance(action.get("host"), dict) else {}
            planning_origin = (
                action.get("planning_origin")
                if isinstance(action.get("planning_origin"), dict) else {}
            )
            tower_id = (
                _text(planning_origin.get("host_tower_id"))
                if planning_origin else None
            )
            if tower_id is None and host:
                tower_id = _text(host.get("host_tower_id"))
            key = canonical_site_key(
                action.get("distinct_site_id"), tower_host=tower_id)
            if key:
                related.add(key)
        return related

    @staticmethod
    def _plan_level_route_ids(container):
        """P16 计划级航路身份的唯一集合（只读证据，不推断）。

        只接受计划对象自身的 ``targets[].route_id`` 与
        ``before/after.routes[].route_id``。一旦出现第二个不同航路就提前返回，
        调用方据此判为"计划级不唯一"，绝不把全部动作默认归给当前航线。
        """

        found = set()
        if not isinstance(container, dict):
            return found
        sources = [container.get("targets")]
        for key in ("before", "after"):
            block = container.get(key)
            sources.append(block.get("routes") if isinstance(block, dict) else None)
        for rows in sources:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                text = _text(row.get("route_id"))
                if text:
                    found.add(text)
                    if len(found) > 1:
                        return found
        return found

    @classmethod
    def _action_relates_to_route(cls, action, requested, plan_route_ids):
        """动作与请求航路的关系判定；只在证据一致时返回 ``True``。"""

        present, explicit_ids, conflict = _explicit_action_route_ids(action)
        if conflict:
            return False
        if present:
            if not explicit_ids:
                # 显式空关联：不属于任何航线，绝不回退到计划级或 action_id。
                return False
            if plan_route_ids and not (explicit_ids & plan_route_ids):
                # 显式声明与计划级身份不一致 → fail-closed。
                return False
            return requested in explicit_ids
        if plan_route_ids:
            if len(plan_route_ids) != 1:
                # 计划级身份不唯一 → 无法证明动作属于哪条航线。
                return False
            return requested == next(iter(plan_route_ids))
        # 最后才回退到历史 action_id 命名契约（deprecated）。
        return _legacy_action_id_matches_route(action.get("action_id"), requested)

    def fixed_corridor_bands(self, query=None):
        """固定 1000 m CNS 保障目标带的**只读三维投影**。

        几何与 currentness **一律**委托既有唯一实现
        （``cns_planner.gis.corridor_band.build_fixed_corridor_band``），三维层
        **不重算 buffer、不新增宽度口径**。航路的 P14 口径 ``route_authority``
        由二维权威服务 :class:`FixedCorridorGeometryService` 提供，与
        ``GET /api/fixed-corridor-geometry`` 逐字段同源。

        垂直语义（本轮重点，必须 fail-closed）：

        * 有**已确认**固定巡航高度层时，给出 ``vertical_extent_status="confirmed"``
          与巡航正高，前端据此把目标带放在该高度层上；
        * 没有已确认高度层时只给**水平投影**，
          ``vertical_extent_status="unconfirmed"``，并声明"不得标称三维保障体积"；
        * ``currentness != current`` 时 ``drawable_as_current_band=False``，
          调用方只能按**明确标注的非权威历史预览**使用。
        """

        query = query or {}
        scope_block = self._scope_block()
        from ..gis.corridor_band import build_fixed_corridor_band
        from ..gis.figure_currentness import route_geometry_fingerprint
        from .result_currentness import effective_result_status

        requested = _text(_first(query, "route_id"))
        routes = [row for row in (self.state.get("operational_routes") or [])
                  if isinstance(row, dict)]
        if requested:
            routes = [row for row in routes
                      if str(row.get("route_id") or "") == requested]

        projected = None
        entries = {}
        if self.fixed_corridor_service is not None:
            try:
                projected = self.fixed_corridor_service.corridor_service.result_snapshot()
            except Exception:  # noqa: BLE001 - 只读投影缺失时如实降级
                projected = None
            for row in routes:
                route_id = str(row.get("route_id") or "")
                try:
                    _snapshot, entry = self.fixed_corridor_service._corridor_route_entry(
                        route_id)
                except Exception:  # noqa: BLE001
                    entry = None
                entries[route_id] = entry
        corridor_effective = effective_result_status(
            self.state, "cns_corridor_assessment")

        bands = []
        for route in routes:
            route_id = str(route.get("route_id") or "")
            path = [[float(point[0]), float(point[1])]
                    for point in (route.get("path") or [])
                    if isinstance(point, (list, tuple)) and len(point) >= 2]
            authority = None
            if self.fixed_corridor_service is not None:
                try:
                    authority = self.fixed_corridor_service._route_authority(route)
                except Exception:  # noqa: BLE001
                    authority = None
            layer = build_fixed_corridor_band(
                route,
                projected=projected,
                entry=entries.get(route_id),
                corridor_effective_status=corridor_effective,
                route_authority=authority,
            )
            detail = layer.get("detail") or {}
            data = layer.get("data") or {}
            vertical = self._band_vertical_extent(route_id)
            geometry_available = (
                detail.get("geometry_status") == "available"
                and bool(detail.get("geometry_fingerprint"))
            )
            current_band = bool(
                layer.get("status") == "available"
                and layer.get("currentness") == "current")
            # 非 current 的历史预览带：**只有**几何本身构造成功（有指纹、
            # construction CRS 已解析）时才允许给几何，并且必须常驻
            # ``preview_authority``。几何构造被阻断时仍然 fail-closed ——
            # 绝不拿近似几何或空几何冒充"历史预览"。
            preview_allowed = geometry_available and not current_band
            preview_polygons = []
            if preview_allowed:
                from ..gis.corridor_geometry import build_fixed_corridor_geometry
                from ..gis.figure_currentness import geojson_outer_rings

                rebuilt = build_fixed_corridor_geometry(
                    path,
                    route_id=route_id,
                    route_authority=authority,
                    source_crs=str(route.get("path_crs") or "OGC:CRS84"),
                    construction_crs=(route.get("construction_crs")
                                      or route.get("metric_crs")),
                )
                if rebuilt.get("status") == "available":
                    preview_polygons, _holes = geojson_outer_rings(
                        rebuilt.get("geometry"))
            bands.append({
                "route_id": route_id,
                "object_id": f"{OBJECT_ID_PREFIXES['protection_corridor']}{route_id}",
                # 航路几何指纹**只**由 GIS 层的唯一实现给出
                # （``figure_currentness.route_geometry_fingerprint`` = ``path_fingerprint``），
                # 三维层绝不另造第二套航路几何身份。
                "route_geometry_fingerprint": route_geometry_fingerprint(path),
                "route_authority": deepcopy(detail.get("route_authority")),
                "route_operational_status": route.get("status"),
                "layer_key": layer.get("layer_key"),
                "status": layer.get("status"),
                "reason": layer.get("reason"),
                "currentness": layer.get("currentness"),
                "currentness_entry": deepcopy(layer.get("currentness_entry")),
                "drawable_as_current_band": current_band,
                # 非 current 时的历史预览带：只有调用方显式要求才给几何，且必须如实标注。
                "preview_only": not current_band,
                "preview_allowed": preview_allowed,
                "preview_authority": (
                    "non_authoritative_historical_preview_not_a_current_assurance_result"
                    if preview_allowed else None
                ),
                "preview_polygons": deepcopy(preview_polygons),
                "must_be_labelled_non_authoritative": True,
                "polygons": deepcopy(data.get("polygons") or []),
                "polygon_holes": deepcopy(data.get("polygon_holes") or {}),
                "width_policy_version": detail.get("width_policy_version"),
                "corridor_half_width_m": detail.get("corridor_half_width_m"),
                "corridor_total_width_m": detail.get("corridor_total_width_m"),
                "width_semantics": detail.get("width_semantics"),
                "geometry_algorithm_id": detail.get("geometry_algorithm_id"),
                "geometry_algorithm_version": detail.get("geometry_algorithm_version"),
                "geometry_fingerprint": detail.get("geometry_fingerprint"),
                "geometry_status": detail.get("geometry_status"),
                "geometry_reason": detail.get("geometry_reason"),
                "construction_crs": detail.get("construction_crs"),
                "projected_area_m2": detail.get("projected_area_m2"),
                "projected_to_geodesic_length_ratio": detail.get(
                    "projected_to_geodesic_length_ratio"),
                "boundary_ring_count": detail.get("boundary_ring_count"),
                "boundary_ring_drawn": detail.get("boundary_ring_drawn"),
                "semantics": detail.get("semantics"),
                "not_device_coverage_area": True,
                "not_certified_airspace": True,
                "navigation_association_radius_m": detail.get(
                    "navigation_association_radius_m"),
                "disclaimer": detail.get("disclaimer"),
                **vertical,
            })

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "revision": self._revision(),
            **scope_block,
            "vertical_reference": self._vertical_reference_block(),
            "status": "available" if bands else "unavailable",
            "reason": None if bands else (
                "no_operational_route" if not requested
                else "requested_route_not_found"
            ),
            "requires_route_id": False,
            "route_id_requested": requested,
            "bands": bands,
            "band_count": len(bands),
            "width_contract": self.fixed_corridor_identity(),
            "geometry_source": {
                "builder": "cns_planner/gis/corridor_geometry.build_fixed_corridor_geometry",
                "layer_builder": "cns_planner/gis/corridor_band.build_fixed_corridor_band",
                "currentness_authority": (
                    "cns_planner/gis/figure_currentness.fixed_corridor_currentness"),
                "route_authority_authority": (
                    "FixedCorridorGeometryService._route_authority（P14 口径，唯一）"),
                "consumes_existing_endpoint": "/api/fixed-corridor-geometry",
                "recomputes_buffer_in_3d": False,
                "uses_p17_outer_half_width_m": False,
            },
            "round38_corridor_effective_status": corridor_effective,
            "identity_rules": {
                "same_fingerprint_as_figure_and_2d": True,
                "stale_never_shown_as_current": True,
                "no_approximate_band_when_geometry_blocked": True,
                "p17_outer_half_width_m_forbidden": True,
                "horizontal_projection_is_not_a_3d_certified_volume": True,
            },
            "read_only": True,
            "writes_performed": 0,
            "algorithms_invoked": [],
        }

    def _band_vertical_extent(self, route_id):
        """该航路固定目标带的**垂直证据**（只读回显，绝不推断）。"""

        layer = self._altitude_layer(route_id)
        confirmed = bool(
            layer.get("nominal_altitude_m") is not None
            and layer.get("layer_confirmed") is True
            and layer.get("operating_layer_confirmed") is not False
        )
        return {
            "vertical_extent_status": "confirmed" if confirmed else "unconfirmed",
            "vertical_extent_reason": None if confirmed else (
                "no_confirmed_fixed_cruise_layer_for_this_route"
            ),
            "altitude_layer_id": layer.get("altitude_layer_id"),
            "nominal_altitude_m": layer.get("nominal_altitude_m"),
            "vertical_reference": layer.get("vertical_reference"),
            "altitude_layer_status": layer.get("layer_status"),
            "altitude_layer_confirmed": layer.get("layer_confirmed"),
            "operating_layer_confirmed": layer.get("operating_layer_confirmed"),
            "climb_cruise_descent_available": bool(
                layer.get("climb_cruise_descent_available")),
            "vertical_volume_semantics": (
                "nominal_cruise_layer_slice_not_a_certified_protection_volume"
                if confirmed else
                "horizontal_projection_only_no_vertical_guarantee"
            ),
            "may_claim_3d_certified_volume": False,
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


def _text(value):
    """规范化可选文本身份：空串与纯空白一律当作"缺失"（``None``）。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _explicit_action_route_ids(action):
    """读取动作自身的显式航路标识，区分"字段缺失"与"显式空关联"。

    返回 ``(present, ids, conflict)``：``present=False`` 表示 ``route_id`` 与
    ``route_ids`` 两个字段都不存在（允许继续使用计划级身份）；``present=True``
    且 ``ids`` 为空表示显式声明"不属于任何航线"；``conflict=True`` 表示两个
    显式字段互相矛盾，调用方必须 fail-closed。
    """

    has_ids = "route_ids" in action
    has_id = "route_id" in action
    if not has_ids and not has_id:
        return False, set(), False
    ids = set()
    if has_ids:
        raw = action.get("route_ids")
        if isinstance(raw, (list, tuple, set)):
            ids = {text for item in raw if (text := _text(item))}
        elif isinstance(raw, str):
            text = _text(raw)
            ids = {text} if text else set()
        # 其他类型无法解释 → 显式空（fail-closed）。
    if not has_id:
        return True, ids, False
    single = _text(action.get("route_id"))
    single_ids = {single} if single else set()
    if not has_ids:
        return True, single_ids, False
    if single_ids:
        # 两个字段同时存在时必须相互包含；不一致即冲突。
        if not single_ids <= ids:
            return True, set(), True
        return True, ids, False
    if ids:
        # ``route_id`` 显式为空，``route_ids`` 却列出航线 → 冲突。
        return True, set(), True
    return True, set(), False


def _legacy_action_id_matches_route(action_id, route_id):
    """历史 ``action_id`` 命名契约兼容（**deprecated**），按 token 边界匹配。

    旧实现用 ``route_id in action_id``，会把 ``R0005`` 命中 ``R00050``、
    把 ``R000`` 命中 ``R0005``。这里要求航路标识是完整的分隔 token。
    """

    text = _text(action_id)
    target = _text(route_id)
    if not text or not target:
        return False
    pattern = r"(?:^|[:|])" + re.escape(target) + r"(?=$|[:|])"
    return re.search(pattern, text) is not None


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
    # Round38-T3D4：站址身份的唯一来源是既有共享候选站址只读投影服务。
    # 未装配时如实留空（服务会披露"未接线"），绝不自行枚举站址。
    shared_sites = None
    shared_service = getattr(workflow, "cns_shared_site_candidate_service", None)
    if shared_service is not None and callable(getattr(shared_service, "snapshot", None)):
        try:
            shared_sites = shared_service.snapshot()
        except Exception:  # noqa: BLE001 - 只读投影失败不得让三维视图整体不可用
            shared_sites = None
    fixed_corridor_service = getattr(
        workflow, "fixed_corridor_geometry_service", None)
    return Visualization3DService(
        state, terrain_source=terrain, terrain_tile_source=terrain_tile_source,
        geoid=geoid, geoid_error=geoid_error,
        imagery_service=imagery_service_for(context),
        relief_tile_source=relief_tile_source,
        source_lock=source_lock,
        data=data,
        shared_sites=shared_sites,
        fixed_corridor_service=fixed_corridor_service,
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
