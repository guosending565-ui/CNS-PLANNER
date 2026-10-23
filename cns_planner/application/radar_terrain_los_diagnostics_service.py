"""Radar 地形 LOS 只读诊断服务（Round34-T07-B）— Application 编排。

本服务是 T07-A 的 LOS 计算与正式 Radar 规划流程之间**唯一**的接线点，
并且是**严格的只读**接线：

* **不写入** ``ProjectState``（没有 writer、没有 artifact、没有 schema 变更）；
* **不注册**任何失效入口（:mod:`cns_planner.application.invalidation_service` 未改动）；
* LOS 结论**不进入** panel_selection / 方位优化 / MILP / 独立站址数 / Required CNS /
  P14 / P15 / P16 / P16 排序与 cap。

默认路径**不打开 DEM**
----------------------

通用 workflow 快照与只读状态查询只返回轻量状态（身份、算法版本、能力边界、
可用性前提），**绝不**触发任何地形采样或 LOS 计算。只有显式请求
（``POST /api/radar-terrain-los/diagnose``）才会：

1. 按既有 T05 链解析真实铁塔的雷达原点（复用 ``resolve_radar_origins``）；
2. 用既有 FABDEM 访问器**每个路径只打开一次**，逐塔采样地表并给出物理输入诊断；
3. 在**有界**的（铁塔 × 航路 probe）组合上跑 T07-A 的逐 probe 地形 LOS；
4. 返回聚合统计 + 有限样本 + 输入指纹 + 独立 current/stale 判断。

有界与分页
----------

单次诊断的铁塔数、每塔 probe 数与全部评价记录数都有硬上限；逐 probe 明细查询只读
**本进程最近一次诊断**的内存证据块（不重新打开 DEM、不重算 LOS），并受分页与
每页上限约束。完整地形剖面**永不**进入 API 响应。
"""

from __future__ import annotations

import inspect
import math
import os
import threading
import time
from copy import deepcopy

from ..algorithms.radar_layout import terrain_los_lookup as lookup_domain
from ..domain import radar_building_los as building_los_domain
from ..domain import radar_combined_los_evidence as combined_evidence
from ..domain import radar_terrain_los as los_domain
from ..domain import radar_terrain_los_evidence as evidence_domain
from ..domain.radar_surveillance_layout import (
    ALTITUDE_LAYER_SOURCE_DEFAULT, METRIC_CRS, RADAR_GEOMETRY_PARAMETERS,
    RADAR_TYPE_I, VERTICAL_REFERENCE,
    resolve_radar_altitude_layer_id,
)
from ..gis import radar_building_los_adapter as building_los
from ..gis import radar_terrain_los_adapter as terrain_los

# ---------------------------------------------------------------- 工程边界（有界输出）

#: 单次诊断**最多**评价的铁塔数（默认值；调用方可显式调整，不可超过硬上限）。
DEFAULT_MAX_TOWER_COUNT = 6
MAX_TOWER_COUNT_LIMIT = 64

#: 单次诊断**最多**评价的 probe 总数（所有塔合计）。
DEFAULT_PROBE_BUDGET = 72
PROBE_BUDGET_LIMIT = 768

#: 摘要里逐 probe 样本条数上限。
DEFAULT_RECORD_SAMPLE_LIMIT = 24
RECORD_SAMPLE_LIMIT_LIMIT = 100

#: 明细分页上限。
DEFAULT_DETAIL_PAGE_LIMIT = 50
MAX_DETAIL_PAGE_LIMIT = 200

#: 单次诊断全部评价记录数硬上限（防止一次请求产生数十 MB JSON）。
MAX_EVALUATED_PROBE_TOTAL = 768

#: 低净空**提示**阈值（米）。它只是展示提示，**不是** LOS 三态的业务阈值，
#: 也绝不改变 :mod:`cns_planner.domain.radar_terrain_los` 的判定。
LOW_CLEARANCE_HINT_M = 5.0
LOW_CLEARANCE_HINT_LIMIT = 5

# ------------------------------------------------------- 联合 LOS 工程边界（T07-I2）

#: 单次联合诊断**最多**评价的铁塔数（默认 1：单站址优先，绝不一次扫全量）。
DEFAULT_COMBINED_TOWER_COUNT = 1
COMBINED_TOWER_COUNT_LIMIT = 4

#: 单次联合诊断**最多**评价的 probe 数（全部塔合计）。
DEFAULT_COMBINED_PROBE_BUDGET = 6
COMBINED_PROBE_BUDGET_LIMIT = 48

#: 单次联合诊断全部 ray 记录硬上限（防止一次请求产生数十 MB JSON）。
MAX_COMBINED_RAY_TOTAL = 48

#: 摘要里逐 ray 样本条数上限。
DEFAULT_COMBINED_SAMPLE_LIMIT = 12
COMBINED_SAMPLE_LIMIT_LIMIT = 48

#: 联合明细分页上限。
DEFAULT_COMBINED_DETAIL_PAGE_LIMIT = 20
MAX_COMBINED_DETAIL_PAGE_LIMIT = 50

#: 每条建筑射线的候选建筑上限（有界 RTree 查询）。
MAX_COMBINED_BUILDING_FEATURES_PER_RAY = 64
#: 建筑域单次执行的射线 / 查询 / 墙钟预算。
MAX_COMBINED_BUILDING_RAYS = 64
MAX_COMBINED_BUILDING_QUERIES = 64
MAX_COMBINED_BUILDING_ELAPSED_SECONDS = 20.0


def _positive_int(value, *, default, hard_limit):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, hard_limit)


def _text_list(value):
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "")]
    return []


class RadarTerrainLosDiagnosticsService:
    """地形 LOS 只读诊断（**无 ProjectState writer**）。"""

    def __init__(self, session, radar_layout_service=None, layered_route_planner_service=None,
                 terrain_source_factory=None, building_source_factory=None):
        self.session = session
        #: 只读依赖：复用既有 Radar 只读接口解析高度层与数据源路径，绝不复制其逻辑。
        self.radar_layout_service = radar_layout_service
        self.layered_route_planner_service = layered_route_planner_service
        self.facts_provider = None
        #: 由 composition root 注入的显式米制变换（``[lon, lat] -> [x, y]``）。
        self.to_metric = None
        #: 测试注入点：给定 ``(path, variant) -> source`` 时用它替代真实 FABDEM 访问器。
        #: 生产代码**不设置**它，因此生产语义永远是既有 ``NativeTerrainWindowSource`` /
        #: ``FabdemWindowTerrainSource``。
        self.terrain_source_factory = terrain_source_factory
        #: 测试注入点：给定 ``(path, metric_crs=...) -> source`` 时用它替代真实
        #: buildings v3 只读源。生产代码**不设置**它。
        self.building_source_factory = building_source_factory
        #: **进程内**内存缓存（不是持久化）：只保存最近一次显式诊断的证据块与明细记录。
        #: 它们不被任何 ProjectState 字段引用，进程结束即消失。
        self._last_evidence = None
        self._last_readable_records = []
        #: 联合 LOS 诊断的**进程内**内存证据与明细（同样不持久化、不写任何容器）。
        self._last_combined_evidence = None
        self._last_combined_detail_records = []
        self._last_combined_request_id = None
        #: 最近一次联合诊断的**数据源访问收据**（进程内、非持久化）。用于在"数据源已
        #: 打开但随后异常"时仍能如实回报打开次数；不写任何 ProjectState / artifact。
        self._last_source_receipt = None
        self._combined_generation = 0
        self._combined_cache_lock = threading.Lock()

    # ------------------------------------------------------------------ 依赖装配

    def _source_paths(self):
        service = self.radar_layout_service
        if service is not None and hasattr(service, "_source_paths"):
            return service._source_paths(facts_provider=self.facts_provider)
        state = self.session.state if isinstance(self.session.state, dict) else {}
        paths = dict(state.get("data_source_paths") or {})
        provider = self.facts_provider if isinstance(self.facts_provider, dict) else {}
        for key, value in (provider.get("paths") or {}).items():
            paths.setdefault(key, value)
        return paths

    def _metric_projector(self):
        projector = self.to_metric
        if callable(projector):
            return projector
        provider = self.facts_provider if isinstance(self.facts_provider, dict) else {}
        projector = provider.get("to_metric")
        return projector if callable(projector) else None

    def _to_geographic(self):
        """逆投影：**复用**既有 Radar 只读接口注入的 ``to_geographic``，绝不另建一套。

        与 :meth:`_metric_projector` 同源：composition root 注入的只读事实 provider
        同时携带 ``to_metric`` 与 ``to_geographic``（同一份 ``EPSG:32651`` 变换）。
        此前这里**只**读 ``radar_layout_service``，于是 provider 里明明有逆变换，
        ``_metric_transform()`` 仍判成"未注入"，联合诊断被永久 fail-closed 在
        ``metric_projector_not_injected`` —— 那是接线缺口，不是真实缺失。
        """

        service = self.radar_layout_service
        projector = getattr(service, "to_geographic", None) if service is not None else None
        if callable(projector):
            return projector
        transform = getattr(service, "metric_transform", None) if service is not None else None
        inverse = getattr(transform, "to_geographic", None)
        if callable(inverse):
            return inverse
        provider = self.facts_provider if isinstance(self.facts_provider, dict) else {}
        injection = provider.get("to_geographic")
        return injection if callable(injection) else None

    def _metric_transform(self):
        """显式米制变换（**必须**同时具备正/逆两个方向）。

        ``sample_terrain_profile`` 需要 ``to_geographic`` 才能把米制射线段映射回
        native 像素（见 T07-A 适配层），因此本服务只接受**双向**变换：缺逆变换时
        fail-closed，绝不用 degree-as-meter 或静默降级。
        """

        service = self.radar_layout_service
        candidate = getattr(service, "metric_transform", None) if service is not None else None
        if (candidate is not None and hasattr(candidate, "to_metric")
                and hasattr(candidate, "to_geographic")):
            return candidate
        forward = self._metric_projector()
        inverse = self._to_geographic()
        if forward is None or inverse is None:
            return None

        class _InjectedTransform:
            method = "injected_explicit_metric_transform"
            metric_crs = "explicit_metric_crs"

            @staticmethod
            def to_metric(point):
                converted = forward([float(point[0]), float(point[1])])
                if converted is None:
                    return None
                return [float(converted[0]), float(converted[1])]

            @staticmethod
            def to_geographic(point):
                converted = inverse([float(point[0]), float(point[1])])
                if converted is None:
                    return None
                return [float(converted[0]), float(converted[1])]

        return _InjectedTransform()

    def _altitude_layers(self):
        state = self.session.state if isinstance(self.session.state, dict) else {}
        return {
            str(item.get("altitude_layer_id")): item
            for item in ((state.get("spatial_3d") or {}).get("altitude_layers") or [])
            if isinstance(item, dict) and item.get("altitude_layer_id")
        }

    def _combined_altitude_layer(self, state, *, requested_layer_id=None):
        """Resolve only a layer that exists in the current authoritative catalog."""

        layers = self._altitude_layers()
        requested = str(requested_layer_id or "").strip()
        if not layers:
            return None, "altitude_layer_not_configured", (
                "当前项目没有权威高度层目录；联合 LOS 不使用 ALT-080 或 80 m 工程默认值。"
            )
        if requested:
            entry = layers.get(requested)
            if not isinstance(entry, dict):
                return None, "altitude_layer_not_configured", (
                    f"请求的高度层 {requested!r} 不在当前项目的权威高度层目录中；"
                    "不猜默认高度层。"
                )
            resolved = (requested, entry.get("nominal_altitude_m"),
                        "explicit_requested_altitude_layer", entry, False)
        else:
            resolved = resolve_radar_altitude_layer_id(state, layers=layers)
        layer_id, altitude_m, source, entry, ambiguous = resolved
        if source == ALTITUDE_LAYER_SOURCE_DEFAULT and not requested:
            return None, "altitude_layer_not_configured", (
                "当前项目没有已确认策略、运行航路或 current candidate 指定的权威高度层；"
                "联合 LOS 不采用 engineering_default_alt_080。请显式选择目录中的高度层。"
            )
        if ambiguous and not requested:
            return None, "altitude_layer_ambiguous", (
                "当前权威运行航路引用多个高度层；必须显式选择本次联合诊断高度层。"
            )
        if not isinstance(entry, dict) or str(layer_id or "") not in layers:
            return None, "altitude_layer_not_configured", (
                f"解析出的高度层 {str(layer_id or '')!r} 不在当前项目的权威高度层目录中。"
            )
        try:
            altitude_value = float(altitude_m)
        except (TypeError, ValueError):
            altitude_value = None
        if altitude_value is None or not math.isfinite(altitude_value):
            return None, "altitude_layer_altitude_invalid", (
                f"权威高度层 {layer_id!r} 缺少有效 nominal_altitude_m。"
            )
        return {
            "layer_id": str(layer_id),
            "altitude_m": altitude_value,
            "source": str(source),
            "entry": entry,
            "ambiguous": bool(ambiguous),
        }, None, None

    @staticmethod
    def _combined_routes(state, *, engineering_demo_mode=False):
        """Select strict routes by default; demo projections require an explicit scope."""

        if not engineering_demo_mode:
            routes = [
                deepcopy(item) for item in state.get("operational_routes") or []
                if isinstance(item, dict) and str(item.get("status")) == "passed"
            ]
            return routes, {
                "mode": "strict_operational", "engineering_demo_mode": False,
                "strict_safety_validation_passed": True,
                "engineering_conditional_acceptance": False,
                "use_scope": "strict_operational_route",
                "operational_authorization": False,
                "published_to_operational_routes": True,
                "route_ids": [str(item.get("route_id") or "") for item in routes],
                "limitations": [],
            }
        adoptions = [
            item for item in ((state.get("layered_operational_adoptions") or {}).get("items") or [])
            if isinstance(item, dict)
            and item.get("status") == "published_engineering_demo"
            and item.get("current_applicability") == "current"
            and item.get("strict_safety_validation_passed") is False
            and item.get("engineering_conditional_acceptance") is True
            and item.get("use_scope") == "engineering_demo_only"
            and item.get("operational_authorization") is False
            and item.get("published_to_operational_routes") is False
            and isinstance((item.get("after") or {}).get("route"), dict)
        ]
        routes = [deepcopy((item.get("after") or {}).get("route")) for item in adoptions]
        limitations = []
        for item in adoptions:
            for limitation in item.get("limitations") or []:
                if limitation not in limitations:
                    limitations.append(limitation)
        return routes, {
            "mode": "engineering_demo_read_only", "engineering_demo_mode": True,
            "strict_safety_validation_passed": False,
            "engineering_conditional_acceptance": bool(routes),
            "use_scope": "engineering_demo_only",
            "operational_authorization": False,
            "published_to_operational_routes": False,
            "route_ids": [str(item.get("route_id") or "") for item in routes],
            "adoption_ids": [str(item.get("adoption_id") or "") for item in adoptions],
            "limitations": limitations,
        }

    # ------------------------------------------------------------------ 只读状态

    def status(self):
        """轻量可用性状态：**不打开 DEM、不做 LOS 计算、不写状态**。"""

        state = self.session.state if isinstance(self.session.state, dict) else {}
        paths = self._source_paths()
        terrain_path = paths.get("terrain_dtm")
        tower_count = len(self._tower_state_items(state))
        profile_collection = state.get("tower_obstacle_profiles")
        profile_collection = (
            profile_collection if isinstance(profile_collection, dict) else {}
        )
        profiles = (
            profile_collection.get("items")
            if isinstance(profile_collection.get("items"), dict) else {}
        )
        resolved = sum(
            1 for item in profiles.values()
            if isinstance(item, dict) and item.get("tower_top_orthometric_m") is not None
        )
        routes = [
            item for item in state.get("operational_routes") or []
            if isinstance(item, dict) and str(item.get("status")) == "passed"
        ]
        layers = self._altitude_layers()
        layer_id, altitude_m, source, _layer, ambiguous = resolve_radar_altitude_layer_id(
            state, layers=layers or None,
        )
        metric_available = self._metric_transform() is not None
        availability = {
            "terrain_dtm": {
                "configured": bool(terrain_path),
                "available": bool(terrain_path),
                "reason": None if terrain_path else "terrain_dtm_not_configured",
                "path_identity": (
                    None if not terrain_path
                    else terrain_los.terrain_source_identity_for_path(str(terrain_path))
                ),
                "opened": False,
            },
            "metric_transform": {
                "available": metric_available,
                "reason": None if metric_available else "metric_projector_not_injected",
                "opened": False,
            },
            "tower_inventory": {
                "tower_count": tower_count,
                "resolved_tower_top_count": resolved,
            },
            "operational_routes": {
                "passed_route_count": len(routes),
                "route_ids": [str(item.get("route_id") or "") for item in routes],
            },
            "altitude_layer": {
                "altitude_layer_id": layer_id,
                "nominal_altitude_m": altitude_m,
                "resolution_source": source,
                "ambiguous_operational_routes": bool(ambiguous),
                "vertical_reference": VERTICAL_REFERENCE,
            },
        }
        status = evidence_domain.not_requested_status(adapter_availability=availability)
        status["available_to_compute"] = bool(
            terrain_path and metric_available and tower_count
        )
        status["detail_available"] = isinstance(self._last_evidence, dict) and bool(
            self._last_readable_records
        )
        return status

    # ------------------------------------------------------------------ 显式诊断

    def diagnose(self, payload=None):
        """**显式请求**时执行一次有界的地形 LOS 只读诊断。

        返回只读证据块。任何失败都如实给出 ``evidence_status = failed`` 与原因，
        **绝不**用空结论冒充"无遮挡"。
        """

        payload = payload if isinstance(payload, dict) else {}
        started = time.perf_counter()
        try:
            return self._diagnose(payload, started=started)
        except Exception as exc:  # noqa: BLE001 - 诊断失败必须如实降级，绝不静默成功
            return evidence_domain.failure_status(
                "terrain_los_diagnosis_failed", detail=f"{type(exc).__name__}:{exc}",
                adapter_availability=None,
            )

    def _diagnose(self, payload, *, started):
        state = self.session.state if isinstance(self.session.state, dict) else {}
        paths = self._source_paths()
        terrain_path = paths.get("terrain_dtm")
        transform = self._metric_transform()
        availability = {
            "terrain_dtm_configured": bool(terrain_path),
            "metric_transform_available": transform is not None,
        }
        if not terrain_path:
            return evidence_domain.failure_status(
                "terrain_dtm_not_configured",
                detail="项目未配置 terrain_dtm 数据源路径；不猜默认数据源。",
                adapter_availability=availability,
            )
        if transform is None:
            return evidence_domain.failure_status(
                "metric_projector_not_injected",
                detail="尚未注入显式米制变换（EPSG:32651）；禁止 degree-as-meter。",
                adapter_availability=availability,
            )
        max_tower_count = _positive_int(
            payload.get("max_tower_count"), default=DEFAULT_MAX_TOWER_COUNT,
            hard_limit=MAX_TOWER_COUNT_LIMIT,
        )
        probe_budget = _positive_int(
            payload.get("probe_budget"), default=DEFAULT_PROBE_BUDGET,
            hard_limit=min(PROBE_BUDGET_LIMIT, MAX_EVALUATED_PROBE_TOTAL),
        )
        record_sample_limit = _positive_int(
            payload.get("record_sample_limit"), default=DEFAULT_RECORD_SAMPLE_LIMIT,
            hard_limit=RECORD_SAMPLE_LIMIT_LIMIT,
        )
        route_filter = _text_list(payload.get("route_ids") or payload.get("route_id"))
        tower_filter = _text_list(payload.get("tower_ids") or payload.get("tower_id"))

        towers = self._tower_state_items(state)
        policy = self._policy()
        origin_result = self._resolve_origins(state, towers, policy)
        all_records = list(origin_result.get("records") or [])
        if not all_records:
            return evidence_domain.failure_status(
                "radar_origin_inventory_empty",
                detail="当前项目没有可解析雷达原点的真实铁塔站址。",
                adapter_availability=availability,
            )
        #: 显式 ``tower_ids`` 只限制"本次评价哪些塔"；它**不**删除、不改写任何站址，
        #: 也不影响下面全量库存的原点物理输入诊断。
        records = (
            [item for item in all_records if str(item.get("tower_id") or "") in set(tower_filter)]
            if tower_filter else all_records
        )

        cache = terrain_los.TerrainSourceCache(self.terrain_source_factory)
        try:
            window_source = cache.get(terrain_path, terrain_los.SOURCE_VARIANT_WINDOW)
            native_source = cache.get(terrain_path, terrain_los.SOURCE_VARIANT_NATIVE)
            if window_source is None or native_source is None:
                return evidence_domain.failure_status(
                    "terrain_source_unavailable",
                    detail=str(cache.describe().get("unavailable") or ""),
                    adapter_availability=availability,
                )
            # ---- 1. 原点物理输入诊断（DEM 只打开一次；285/88 口径的来源） ----------
            # 对**全部**候选站址做诊断，因此 origin_availability_counts 覆盖完整库存，
            # 而不是只覆盖被抽中的那几座塔（否则会掩盖 88 座不可评价原点）。
            physical = terrain_los.radar_origin_terrain_physical_inputs(
                all_records, terrain_source=window_source,
            )
            availability_counts = {
                name: 0 for name in los_domain.ORIGIN_AVAILABILITY_STATES
            }
            for record in all_records:
                key = str((physical.get(str(record.get("tower_id"))) or {}).get(
                    "origin_availability"
                ))
                if key in availability_counts:
                    availability_counts[key] += 1

            # ---- 2. 有界（塔 × 航路 probe）抽样 ---------------------------------
            layers = self._altitude_layers()
            layer_id, altitude_m, layer_source, layer_entry, ambiguous = (
                resolve_radar_altitude_layer_id(state, layers=layers or None)
            )
            layer_entry = layer_entry if isinstance(layer_entry, dict) else {}
            probes, probe_stats = self._route_probes(
                state, layer_id=layer_id, altitude_m=altitude_m, transform=transform,
                probe_budget=probe_budget, route_filter=route_filter,
            )
            selected_towers = self._select_towers(
                records, physical, max_tower_count=max_tower_count,
            )
            parameters = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]
            evaluations, truncation = self._evaluate(
                selected_towers, physical=physical, probes=probes,
                native_source=native_source, transform=transform, layer_id=layer_id,
                parameters=parameters,
            )
            elapsed = time.perf_counter() - started
            bounds = {
                "max_tower_count": max_tower_count,
                "probe_budget": probe_budget,
                "probe_stride": probe_stats.get("probe_stride"),
                "detail_page_limit": DEFAULT_DETAIL_PAGE_LIMIT,
            }
            evidence = evidence_domain.build_terrain_los_evidence(
                records=evaluations,
                candidate_tower_count=len(all_records),
                candidate_probe_count=probe_stats["candidate_probe_count"],
                origin_availability_counts=availability_counts,
                dem_source_identity=terrain_los.terrain_source_identity(native_source),
                dem_vertical_reference=getattr(native_source, "vertical_reference", None),
                altitude_layer={
                    **deepcopy(layer_entry),
                    "altitude_layer_id": layer_id,
                    "nominal_altitude_m": altitude_m,
                    "resolution_source": layer_source,
                    "vertical_reference": VERTICAL_REFERENCE,
                },
                sampling_interval_m=terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                envelope_radius_m=terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                earth_radius_m=terrain_los.EARTH_MEAN_RADIUS_M,
                k_factor=terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                bounds=bounds,
                dem_read_receipt=cache.describe(),
                elapsed_seconds=elapsed,
                prior_evidence=payload.get("prior_evidence"),
                low_clearance_threshold_m=LOW_CLEARANCE_HINT_M,
                low_clearance_limit=LOW_CLEARANCE_HINT_LIMIT,
                record_sample_limit=record_sample_limit,
                source_provenance={
                    "terrain_dtm_path": str(terrain_path),
                    "terrain_source_variant": terrain_los.SOURCE_VARIANT_NATIVE,
                    "point_sampler_variant": terrain_los.SOURCE_VARIANT_WINDOW,
                    "origin_resolution": "resolve_radar_origins",
                    "probe_source": "build_route_samples",
                    "route_filter": route_filter,
                    "tower_filter": tower_filter,
                    "candidate_probe_source_detail": probe_stats,
                    "altitude_layer_ambiguous": bool(ambiguous),
                },
                adapter_availability=availability,
            )
            evidence["evaluation_bounds_applied"] = deepcopy(truncation)
            evidence["evaluated_tower_source"] = "bounded_deterministic_tower_sample"
            evidence["dem_open_count"] = cache.describe().get("open_count")
            evidence["physical_input_diagnosed_tower_count"] = len(all_records)
            #: 明细级记录由服务**单独持有**（进程内），不重复塞进摘要响应。
            self._last_readable_records = [
                evidence_domain.readable_record_for_evaluation(item)
                for item in evaluations
            ]
            evidence["detail_available"] = bool(self._last_readable_records)
            self._last_evidence = evidence
            return evidence
        finally:
            cache.close()

    # ------------------------------------------------------------------ details

    def details(self, payload=None):
        """受限、可分页的逐 probe 明细查询（**不重新打开 DEM、不重算 LOS**）。

        数据来自本进程最近一次显式诊断的内存证据块。若没有可比对的证据，
        明确返回 ``failed`` + ``detail_not_available``，**绝不**隐式触发一次全量计算。
        """

        payload = payload if isinstance(payload, dict) else {}
        evidence = self._last_evidence
        if not isinstance(evidence, dict) or not self._last_readable_records:
            return {
                **evidence_domain.failure_status(
                    "detail_not_available",
                    detail=(
                        "本进程还没有可查询的地形 LOS 证据；明细查询**不会**隐式触发"
                        "全量计算。请先显式执行一次地形 LOS 检查。"
                    ),
                ),
                "detail_page": None,
            }
        page = evidence_domain.flatten_detail_records(
            evidence,
            records=self._last_readable_records,
            tower_id=payload.get("tower_id"),
            route_id=payload.get("route_id"),
            altitude_layer_id=payload.get("altitude_layer_id"),
            los_status=payload.get("los_status"),
            offset=payload.get("offset") or 0,
            limit=payload.get("limit") or DEFAULT_DETAIL_PAGE_LIMIT,
            max_limit=MAX_DETAIL_PAGE_LIMIT,
        )
        return {
            "adapter_id": evidence_domain.TERRAIN_LOS_EVIDENCE_ADAPTER_ID,
            "evidence_status": evidence.get("evidence_status"),
            "evidence_status_label": evidence.get("evidence_status_label"),
            "algorithm_version": evidence.get("algorithm_version"),
            "evaluation_scope": evidence.get("evaluation_scope"),
            "input_fingerprint": evidence.get("input_fingerprint"),
            "currentness": deepcopy(evidence.get("currentness")),
            "detail_page": page,
            "full_terrain_profile_included": False,
            "recomputed": False,
            "user_disclaimer_zh": evidence_domain.TERRAIN_LOS_USER_DISCLAIMER_ZH,
            **evidence_domain.READ_ONLY_DECLARATION,
        }

    # --------------------------------------------------------- 联合 LOS 只读诊断

    def combined_status(self, *, engineering_demo_mode=False):
        """联合 Terrain + Building LOS 的轻量状态。

        **不打开 DEM，也不打开 GPKG**：只读取项目配置里的数据源角色与路径名、
        站址数量、高度层解析结果与米制变换可用性。任何缺失一律 fail-closed 披露，
        绝不猜默认数据源，也绝不在打开面板时触发一次昂贵计算。
        """

        state = self.session.state if isinstance(self.session.state, dict) else {}
        paths = self._source_paths()
        terrain_path = paths.get("terrain_dtm")
        buildings_path, buildings_role = self._buildings_path()
        transform = self._metric_transform()
        metric_crs, metric_crs_source = self._metric_crs(transform)
        towers = self._tower_state_items(state)
        layers = self._altitude_layers()
        routes, route_consumption = self._combined_routes(
            state, engineering_demo_mode=engineering_demo_mode,
        )
        demo_layer_ids = {
            str(item.get("altitude_layer_id") or "") for item in routes
            if str(item.get("altitude_layer_id") or "")
        }
        requested_layer_id = next(iter(demo_layer_ids)) if (
            engineering_demo_mode and len(demo_layer_ids) == 1
        ) else None
        layer, layer_reason, _layer_detail = self._combined_altitude_layer(
            state, requested_layer_id=requested_layer_id,
        )
        layer_id = layer.get("layer_id") if layer else None
        altitude_m = layer.get("altitude_m") if layer else None
        layer_source = layer.get("source") if layer else None
        layer_entry = layer.get("entry") if layer else {}
        ambiguous = layer.get("ambiguous") if layer else False
        availability = {
            "terrain_dtm": {
                "configured": bool(terrain_path),
                "role": "terrain_dtm",
                "file_name": None if not terrain_path else os.path.basename(str(terrain_path)),
                "reason": None if terrain_path else "terrain_dtm_not_configured",
                "opened": False,
            },
            "buildings": {
                "configured": bool(buildings_path),
                "role": buildings_role,
                "file_name": (
                    None if not buildings_path else os.path.basename(str(buildings_path))
                ),
                "reason": None if buildings_path else "buildings_not_configured",
                "opened": False,
                "spatial_index_checked": False,
            },
            "metric_transform": {
                "available": transform is not None,
                "metric_crs": metric_crs,
                "metric_crs_source": metric_crs_source,
                "reason": None if transform is not None else "metric_projector_not_injected",
                "opened": False,
            },
            "tower_inventory": {"tower_count": len(towers)},
            "altitude_layer": {
                "configured": layer is not None,
                "altitude_layer_id": layer_id,
                "nominal_altitude_m": altitude_m,
                "resolution_source": layer_source,
                "ambiguous_operational_routes": bool(ambiguous),
                "vertical_reference": VERTICAL_REFERENCE,
                "reason": layer_reason,
            },
        }
        available_to_compute = bool(
            terrain_path and buildings_path and transform is not None and towers and layer and routes
        )
        evidence = self._last_combined_evidence
        status = combined_evidence.not_requested_status(
            adapter_availability=availability,
            altitude_layer={
                **deepcopy(layer_entry),
                "altitude_layer_id": layer_id,
                "nominal_altitude_m": altitude_m,
                "resolution_source": layer_source,
                "vertical_reference": VERTICAL_REFERENCE,
            },
            terrain_source_identity=(
                None if not terrain_path
                else terrain_los.terrain_source_identity_for_path(str(terrain_path))
            ),
            building_source_identity=(
                None if not buildings_path else {
                    "role": buildings_role,
                    "file_name": os.path.basename(str(buildings_path)),
                    "opened": False,
                }
            ),
        )
        if isinstance(evidence, dict):
            status["evidence_status"] = evidence.get("evidence_status")
            status["evidence_status_label"] = evidence.get("evidence_status_label")
            status["input_fingerprint"] = evidence.get("input_fingerprint")
            status["fingerprint_schema_version"] = evidence.get(
                "fingerprint_schema_version"
            )
            status["request_id"] = evidence.get("request_id")
            status["generation"] = evidence.get("generation")
            status["failure_reason"] = evidence.get("failure_reason")
            status["failure_detail"] = evidence.get("failure_detail")
            status["currentness"] = deepcopy(evidence.get("currentness"))
            status["stale"] = evidence.get("stale") is True
            status["aggregate"] = deepcopy(evidence.get("aggregate"))
            status["bounds"] = deepcopy(evidence.get("bounds"))
            status["detail_available"] = bool(self._last_combined_detail_records)
        status["available_to_compute"] = available_to_compute
        status["available_with_explicit_altitude_layer"] = bool(
            terrain_path and buildings_path and transform is not None and towers and layers
        )
        status["detail_available"] = bool(self._last_combined_detail_records) and bool(
            isinstance(evidence, dict)
        )
        #: 站址与高度层**选项**：只转印项目状态里的既有身份，绝不打开数据源、
        #: 也绝不改动任何站址容器；列表有界，避免把全量站址塞进轻量状态。
        status["tower_options"] = [
            {"tower_id": str(item.get("tower_id") or "")}
            for item in towers[:64] if str(item.get("tower_id") or "")
        ]
        status["tower_option_limit"] = 64
        status["altitude_layer_options"] = [
            {
                "altitude_layer_id": str(item.get("altitude_layer_id") or ""),
                "nominal_altitude_m": item.get("nominal_altitude_m"),
                "vertical_reference": item.get("vertical_reference") or VERTICAL_REFERENCE,
            }
            for item in sorted(
                layers.values(), key=lambda entry: str(entry.get("altitude_layer_id") or ""),
            )[:32]
            if str(item.get("altitude_layer_id") or "")
        ]
        status["open_panel_never_computes_los"] = True
        status["path_parameters_never_accepted"] = True
        status["route_consumption"] = route_consumption
        return status

    def diagnose_combined(self, payload=None):
        """**显式请求**时执行一次有界的 Terrain + Building 联合 LOS 只读诊断。

        这是本服务里**唯一**会同时打开 FABDEM 与 buildings v3 的入口。数据源路径
        **只**来自项目配置：调用方传入的任何路径类参数都被忽略并如实回执。
        任何失败都返回 ``evidence_status = failed`` 与原因，绝不返回任何 ``clear``。
        """

        payload = payload if isinstance(payload, dict) else {}
        engineering_demo_mode = payload.get("engineering_demo_mode") is True
        started = time.perf_counter()
        with self._combined_cache_lock:
            self._combined_generation += 1
            generation = self._combined_generation
            request_id = str(payload.get("request_id") or f"combined-los-{generation}")
            self._last_combined_request_id = request_id
            self._last_combined_evidence = None
            self._last_combined_detail_records = []
        try:
            self._last_source_receipt = None
            evidence = self._diagnose_combined(payload, started=started)
        except Exception as exc:  # noqa: BLE001 - 诊断失败必须如实降级，绝不静默成功
            evidence = combined_evidence.failure_status(
                "combined_los_diagnosis_failed",
                detail=f"{type(exc).__name__}:{exc}",
                adapter_availability=None,
            )
            # 数据源访问已经开始过：把真实收据合入失败响应（缺失的键保持缺失）。
            receipt = self._last_source_receipt or {}
            for key in ("dem_open_count", "building_open_count"):
                if receipt.get(key) is not None:
                    evidence[key] = receipt[key]
            if receipt.get("source_access_attempted"):
                evidence["source_access_attempted"] = True
        details = evidence.pop("_detail_records", [])
        _routes, route_consumption = self._combined_routes(
            self.session.state if isinstance(self.session.state, dict) else {},
            engineering_demo_mode=engineering_demo_mode,
        )
        evidence["route_consumption"] = route_consumption
        evidence["request_id"] = request_id
        evidence["generation"] = generation
        if evidence.get("evidence_status") == combined_evidence.EVIDENCE_STATUS_FAILED:
            #: 0 兜底**只**对"还没尝试访问数据源"就失败的路径成立（此时"一次都没打开"
            #: 是执行事实）。一旦数据源访问已经开始，缺计数就是"未知"：既不填 0，也不
            #: 编造更多信息 —— 未知绝不被静默转换成一个看起来确定的 0。
            if not evidence.get("source_access_attempted"):
                evidence.setdefault("dem_open_count", 0)
                evidence.setdefault("building_open_count", 0)
            evidence.pop("source_access_attempted", None)
        with self._combined_cache_lock:
            if generation == self._combined_generation:
                self._last_combined_evidence = evidence
                self._last_combined_detail_records = (
                    details if evidence.get("evidence_status") in {
                        combined_evidence.EVIDENCE_STATUS_COMPUTED,
                        combined_evidence.EVIDENCE_STATUS_STALE,
                    } and evidence.get("input_fingerprint") else []
                )
            else:
                evidence["superseded_by_newer_request"] = True
        return evidence

    def _diagnose_combined(self, payload, *, started):
        state = self.session.state if isinstance(self.session.state, dict) else {}
        paths = self._source_paths()
        terrain_path = paths.get("terrain_dtm")
        buildings_path, buildings_role = self._buildings_path()
        transform = self._metric_transform()
        #: 调用方**绝不能**指定任意本地文件路径。任何路径类参数都被忽略并如实回执。
        ignored_path_parameters = sorted(
            key for key in (
                "path", "terrain_path", "buildings_path", "building_path",
                "gpkg", "gpkg_path", "dem", "dem_path", "terrain_dtm", "buildings",
            )
            if key in payload
        )
        availability = {
            "terrain_dtm_configured": bool(terrain_path),
            "buildings_configured": bool(buildings_path),
            "buildings_role": buildings_role,
            "metric_transform_available": transform is not None,
            "ignored_path_parameters": ignored_path_parameters,
        }
        if not terrain_path:
            return combined_evidence.failure_status(
                "terrain_dtm_not_configured",
                detail="项目未配置 terrain_dtm 数据源路径；不猜默认数据源。",
                adapter_availability=availability,
            )
        if not buildings_path:
            return combined_evidence.failure_status(
                "buildings_not_configured",
                detail=(
                    "项目未配置 buildings 数据源路径；联合诊断必须同时具备地形与建筑"
                    "两个真实来源，缺少任何一个都不做单源冒充。"
                ),
                adapter_availability=availability,
            )
        if transform is None:
            return combined_evidence.failure_status(
                "metric_projector_not_injected",
                detail="尚未注入显式米制变换（EPSG:32651）；禁止 degree-as-meter。",
                adapter_availability=availability,
            )

        layer, layer_reason, layer_detail = self._combined_altitude_layer(
            state, requested_layer_id=payload.get("altitude_layer_id"),
        )
        if layer is None:
            return combined_evidence.failure_status(
                layer_reason, detail=layer_detail, adapter_availability=availability,
            )
        layer_id = layer["layer_id"]
        altitude_m = layer["altitude_m"]
        layer_source = layer["source"]
        layer_entry = layer["entry"]
        ambiguous = layer["ambiguous"]

        max_tower_count = _positive_int(
            payload.get("max_tower_count"), default=DEFAULT_COMBINED_TOWER_COUNT,
            hard_limit=COMBINED_TOWER_COUNT_LIMIT,
        )
        probe_budget = _positive_int(
            payload.get("probe_budget"), default=DEFAULT_COMBINED_PROBE_BUDGET,
            hard_limit=min(COMBINED_PROBE_BUDGET_LIMIT, MAX_COMBINED_RAY_TOTAL),
        )
        record_sample_limit = _positive_int(
            payload.get("record_sample_limit"), default=DEFAULT_COMBINED_SAMPLE_LIMIT,
            hard_limit=COMBINED_SAMPLE_LIMIT_LIMIT,
        )
        route_filter = _text_list(payload.get("route_ids") or payload.get("route_id"))
        tower_filter = _text_list(payload.get("tower_ids") or payload.get("tower_id"))

        towers = self._tower_state_items(state)
        policy = self._policy()
        origin_result = self._resolve_origins(state, towers, policy)
        all_records = list(origin_result.get("records") or [])
        if not all_records:
            return combined_evidence.failure_status(
                "radar_origin_inventory_empty",
                detail="当前项目没有可解析雷达原点的真实铁塔站址。",
                adapter_availability=availability,
            )
        #: 显式 ``tower_ids`` **只**限制本次评价哪些站址；它不删除、不改写任何站址，
        #: 也不影响全量库存的原点物理输入诊断。
        records = (
            [
                item for item in all_records
                if str(item.get("tower_id") or "") in set(tower_filter)
            ]
            if tower_filter else all_records
        )
        if not records:
            return combined_evidence.failure_status(
                "tower_filter_matched_nothing",
                detail="请求的 tower_ids 与当前项目真实站址库存没有交集。",
                adapter_availability=availability,
            )

        metric_crs, metric_crs_source = self._metric_crs(transform)
        cache = terrain_los.TerrainSourceCache(self.terrain_source_factory)
        building_source = None

        def _building_opened():
            """建筑数据源是否**真的**被打开过（不接受"对象构造成功"当证据）。

            生产实现 :class:`QgisBuildingLosSource` 在 ``__init__`` 里打开 GPKG 并校验
            图层/字段，因此"访问器存在"确实等价于"打开过"。但这条等价关系必须由
            **收据**证明而不是由类型假设：这里要求 ``describe()`` 至少能给出图层身份，
            否则一律如实返回 ``None``（未知），绝不把它写成 0，也绝不写成 1。
            """

            if building_source is None:
                return 0
            try:
                describe = building_source.describe() or {}
            except Exception:  # noqa: BLE001 - 无法取证就是"未知"，不是 0
                return None
            identity = describe.get("file_name") or describe.get("path") or describe.get("layer")
            return 1 if identity else None

        def _source_failure(reason, *, detail=None):
            """失败也如实回执**已经发生**的数据源打开行为，绝不伪报 0。

            这些失败点在 ``cache.get`` / 建筑访问器构造之后，DEM 或 GPKG 可能已经真的
            被打开；若在此处缺省填 0，前端"数据源打开次数"就会虚假陈述执行行为
            （违反"缺失不等于 0"）。打开前的失败由调用方兜底为 0。

            **只在真的取到证据时写入计数**：取证不到（``None``）就不写这个键，让收尾的
            ``setdefault(..., 0)`` 解释为"从未尝试打开" —— 而不是把"未知"写成 0。
            """

            receipt = {
                **combined_evidence.failure_status(
                    reason, detail=detail, adapter_availability=availability,
                ),
                #: 明确声明"数据源访问已经尝试过"：收尾的 0 兜底只适用于**访问之前**的
                #: 失败（此时"一次都没打开"是事实）。访问之后的失败绝不用 0 兜底。
                "source_access_attempted": True,
            }
            dem_open_count = cache.describe().get("open_count")
            if dem_open_count is not None:
                receipt["dem_open_count"] = dem_open_count
            building_open_count = _building_opened()
            if building_open_count is not None:
                receipt["building_open_count"] = building_open_count
            return receipt

        try:
            window_source = cache.get(terrain_path, terrain_los.SOURCE_VARIANT_WINDOW)
            native_source = cache.get(terrain_path, terrain_los.SOURCE_VARIANT_NATIVE)
            if window_source is None or native_source is None:
                return _source_failure(
                    "terrain_source_unavailable",
                    detail=str(cache.describe().get("unavailable") or ""),
                )
            building_source = self._building_source(buildings_path, metric_crs=metric_crs)
            if building_source is None:
                return _source_failure(
                    "building_source_unavailable",
                    detail="buildings 数据源无法构造只读访问器。",
                )
            describe = building_source.describe() or {}
            if not bool(describe.get("spatial_index_available")):
                return _source_failure(
                    "building_source_spatial_index_missing",
                    detail="buildings v3 未暴露 provider RTree；不做全表扫描冒充有界查询。",
                )
            physical = terrain_los.radar_origin_terrain_physical_inputs(
                all_records, terrain_source=window_source,
            )
            routes, _route_consumption = self._combined_routes(
                state, engineering_demo_mode=payload.get("engineering_demo_mode") is True,
            )
            probes, probe_stats = self._route_probes(
                state, layer_id=layer_id, altitude_m=altitude_m, transform=transform,
                probe_budget=probe_budget, route_filter=route_filter, routes=routes,
            )
            if not probes:
                return _source_failure(
                    "no_route_probe_available",
                    detail=(
                        "当前没有可用的已发布航路采样点（operational_routes.status=passed），"
                        "因此无法构造联合射线的目标端点。"
                    ),
                )
            selected_towers = [
                {**item, "metric": self._tower_metric_for(item, transform)}
                for item in self._select_towers(
                    records, physical, max_tower_count=max_tower_count,
                )
            ]
            parameters = RADAR_GEOMETRY_PARAMETERS[RADAR_TYPE_I]
            evaluations, truncation, lookup, building_context = self._evaluate_combined(
                selected_towers, physical=physical, probes=probes,
                native_source=native_source, building_source=building_source,
                transform=transform, layer_id=layer_id, altitude_m=altitude_m,
                parameters=parameters,
            )
            elapsed = time.perf_counter() - started
            ray_fingerprints = [item.get("cache_key") for item in evaluations]
            tower_identities = [
                {
                    "tower_id": str(item.get("tower_id") or ""),
                    "origin_egm2008_m": item.get("origin_egm2008_m"),
                    "origin_source": item.get("origin_source"),
                    "longitude": item.get("longitude"), "latitude": item.get("latitude"),
                }
                for item in selected_towers
            ]
            probe_identities = [
                {
                    "probe_id": item.get("voxel_id"), "route_id": item.get("route_id"),
                    "metric": list(item.get("metric") or []),
                    "altitude_layer_id": item.get("altitude_layer_id"),
                }
                for item in probes
            ]
            input_fingerprint = combined_evidence.combined_los_input_fingerprint(
                radar_type=RADAR_TYPE_I,
                altitude_layer={
                    "altitude_layer_id": layer_id, "nominal_altitude_m": altitude_m,
                    "resolution_source": layer_source, "vertical_reference": VERTICAL_REFERENCE,
                },
                terrain_source_identity=terrain_los.terrain_source_identity(native_source),
                building_source_identity=describe,
                tower_identities=tower_identities, probe_identities=probe_identities,
                sampling={
                    "sampling_interval_m": terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                    "envelope_radius_m": terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                },
                ray_input_fingerprints=ray_fingerprints,
                horizontal_crs=metric_crs, vertical_crs=VERTICAL_REFERENCE,
                ray_model=building_los_domain.RAY_MODEL_ID,
                tower_filter=tower_filter, route_filter=route_filter,
            )
            bounds = {
                "max_tower_count": max_tower_count,
                "probe_budget": probe_budget,
                "probe_stride": probe_stats.get("probe_stride"),
                "max_ray_total": MAX_COMBINED_RAY_TOTAL,
                "record_sample_limit": record_sample_limit,
                "detail_page_limit": DEFAULT_COMBINED_DETAIL_PAGE_LIMIT,
            }
            execution_receipt = combined_evidence.combined_los_execution_receipt(
                input_fingerprint=input_fingerprint, bounds=bounds,
                lookup_snapshot=lookup.snapshot(),
                evaluated_ray_count=len(evaluations), elapsed_seconds=elapsed,
                truncation=truncation, terrain_read_receipt=cache.describe(),
                building_open_count=1, building_source_opened=True,
            )
            summaries, details = [], []
            for item in evaluations:
                summary = combined_evidence.readable_ray_record(
                    evaluation=item["evaluation"], tower=item["tower"], probe=item["probe"],
                    radar_type=RADAR_TYPE_I, horizontal_crs=metric_crs, detailed=False,
                )
                detail = combined_evidence.readable_ray_record(
                    evaluation=item["evaluation"], tower=item["tower"], probe=item["probe"],
                    radar_type=RADAR_TYPE_I, horizontal_crs=metric_crs, detailed=True,
                )
                summaries.append(summary)
                details.append(detail)
            evidence = combined_evidence.build_combined_los_evidence(
                records=summaries, detailed_records=details, bounds=bounds,
                source_provenance={
                    "terrain_dtm_role": "terrain_dtm",
                    "terrain_dtm_file_name": os.path.basename(str(terrain_path)),
                    "buildings_role": buildings_role,
                    "buildings_file_name": os.path.basename(str(buildings_path)),
                    "origin_resolution": "resolve_radar_origins",
                    "probe_source": "build_route_samples",
                    "metric_crs": metric_crs, "metric_crs_source": metric_crs_source,
                    "route_filter": route_filter, "tower_filter": tower_filter,
                    "ignored_path_parameters": ignored_path_parameters,
                    "candidate_probe_source_detail": probe_stats,
                    "altitude_layer_ambiguous": bool(ambiguous),
                    "single_source_diagnostic": False,
                },
                terrain_source_identity=terrain_los.terrain_source_identity(native_source),
                building_source_identity=describe,
                altitude_layer={
                    **deepcopy(layer_entry),
                    "altitude_layer_id": layer_id, "nominal_altitude_m": altitude_m,
                    "resolution_source": layer_source, "vertical_reference": VERTICAL_REFERENCE,
                },
                sampling={
                    "sampling_interval_m": terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                    "envelope_radius_m": terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                    "earth_radius_m": terrain_los.EARTH_MEAN_RADIUS_M,
                    "k_factor": terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                },
                input_fingerprint=input_fingerprint,
                execution_receipt=execution_receipt,
                prior_evidence=payload.get("prior_evidence"),
                lookup_snapshot=lookup.snapshot(),
                ray_sample_limit=record_sample_limit,
                detail_page_limit=DEFAULT_COMBINED_DETAIL_PAGE_LIMIT,
                truncation=truncation, building_source_opened=True, building_open_count=1,
                horizontal_crs=metric_crs, vertical_crs=VERTICAL_REFERENCE,
            )
            evidence["building_execution_receipt"] = building_context.receipt()
            evidence["evaluated_tower_count"] = len(selected_towers)
            evidence["candidate_tower_count"] = len(all_records)
            evidence["evaluated_ray_count"] = len(evaluations)
            evidence["physical_input_diagnosed_tower_count"] = len(all_records)
            evidence["dem_open_count"] = cache.describe().get("open_count")
            evidence["probe_selection"] = deepcopy(probe_stats)
            #: 明细级记录由服务**单独持有**（进程内），不重复塞进摘要响应。
            evidence["_detail_records"] = details
            return evidence
        finally:
            # 数据源访问**已经开始**（前面那些"未配置/未注入"的失败都在打开之前提前
            # return）。这里把当下的收据固化到实例槽位：即使随后抛异常，外层组装失败
            # 响应时也能如实回报已发生的打开次数，而不是把"打开过"伪报成"0 次"。
            # ``cache.describe()`` 必须在 ``close()`` 之前读取。
            self._last_source_receipt = {
                "source_access_attempted": True,
                "dem_open_count": cache.describe().get("open_count"),
                "building_open_count": _building_opened(),
            }
            cache.close()
            building_source = None

    def combined_details(self, payload=None):
        """受限、可分页的逐射线联合明细（**不重开数据源、不重算 LOS**）。"""

        payload = payload if isinstance(payload, dict) else {}
        with self._combined_cache_lock:
            evidence = deepcopy(self._last_combined_evidence)
            records = deepcopy(self._last_combined_detail_records)
        requested_id = str(payload.get("request_id") or "")
        requested_fingerprint = str(payload.get("input_fingerprint") or "")
        requested_generation = payload.get("generation")
        try:
            requested_generation = int(requested_generation)
        except (TypeError, ValueError):
            requested_generation = None
        identity_matches = bool(
            isinstance(evidence, dict)
            and requested_id
            and requested_fingerprint
            and requested_generation is not None
            and requested_id == str(evidence.get("request_id") or "")
            and requested_fingerprint == str(
                evidence.get("input_fingerprint") or ""
            )
            and requested_generation == evidence.get("generation")
        )
        if not identity_matches or not records or evidence.get("evidence_status") not in {
            combined_evidence.EVIDENCE_STATUS_COMPUTED,
            combined_evidence.EVIDENCE_STATUS_STALE,
        }:
            return {
                **combined_evidence.failure_status(
                    "detail_not_available",
                    detail=(
                        "本进程还没有可查询的联合 LOS 证据；明细查询**不会**隐式触发"
                        "全量计算。请先显式执行一次联合视线诊断。"
                    ),
                ),
                "detail_page": None,
                "full_terrain_profile_included": False,
                "recomputed": False,
                "request_id": evidence.get("request_id") if isinstance(evidence, dict) else None,
                "generation": evidence.get("generation") if isinstance(evidence, dict) else None,
                "user_disclaimer_zh": combined_evidence.COMBINED_LOS_USER_DISCLAIMER_ZH,
                **deepcopy(evidence_domain.READ_ONLY_DECLARATION),
            }
        page = combined_evidence.flatten_combined_detail_records(
            records,
            tower_id=payload.get("tower_id"),
            route_id=payload.get("route_id"),
            combined_status=payload.get("combined_los_status")
            or payload.get("los_status"),
            building_domain_state=payload.get("building_domain_state"),
            offset=payload.get("offset") or 0,
            limit=payload.get("limit") or DEFAULT_COMBINED_DETAIL_PAGE_LIMIT,
            max_limit=MAX_COMBINED_DETAIL_PAGE_LIMIT,
        )
        return {
            "adapter_id": combined_evidence.COMBINED_LOS_EVIDENCE_ADAPTER_ID,
            "evidence_status": evidence.get("evidence_status"),
            "evidence_status_label": evidence.get("evidence_status_label"),
            "algorithm_version": evidence.get("algorithm_version"),
            "evaluation_scope": evidence.get("evaluation_scope"),
            "input_fingerprint": evidence.get("input_fingerprint"),
            "request_id": evidence.get("request_id"),
            "generation": evidence.get("generation"),
            "currentness": deepcopy(evidence.get("currentness")),
            "detail_page": page,
            "full_terrain_profile_included": False,
            "recomputed": False,
            "user_disclaimer_zh": combined_evidence.COMBINED_LOS_USER_DISCLAIMER_ZH,
            **deepcopy(evidence_domain.READ_ONLY_DECLARATION),
        }

    # --------------------------------------------------- 联合 LOS 内部装配

    def _buildings_path(self):
        """建筑数据源路径**只**来自项目配置；绝不接受调用方传入的任意本地路径。"""

        paths = self._source_paths()
        for role in ("buildings", "building_grid"):
            value = paths.get(role)
            if value:
                return str(value), role
        return None, None

    @staticmethod
    def _metric_crs(transform):
        """米制 CRS：优先取显式变换自报的 ``EPSG:xxxx``，否则用项目固定常量。

        项目固定的 :data:`~cns_planner.domain.radar_surveillance_layout.METRIC_CRS`
        是既有 canonical 常量（EPSG:32651），不是"猜默认值"；两者都会在来源里如实披露。
        """

        value = getattr(transform, "metric_crs", None) if transform is not None else None
        text = str(value or "").strip()
        if text.upper().startswith("EPSG:"):
            return text.upper(), "transform_metric_crs"
        return METRIC_CRS, "project_metric_crs_constant"

    def _building_source(self, path, *, metric_crs):
        """构造只读建筑源；测试可注入 ``building_source_factory`` 替代真实 GPKG。"""

        factory = self.building_source_factory
        if callable(factory):
            return factory(path, metric_crs=metric_crs)
        return building_los.QgisBuildingLosSource(path, metric_crs=metric_crs)

    @staticmethod
    def _tower_metric_for(tower, transform):
        """站址米制坐标：优先既有 ``metric``，否则用**已注入**的显式变换换算。

        逆投影缺失时如实返回 ``None``：该组合在联合评价里保持 ``unknown``
        （fail-closed），绝不用经纬度冒充米制距离。
        """

        metric = tower.get("metric")
        if isinstance(metric, (list, tuple)) and len(metric) >= 2:
            return [float(metric[0]), float(metric[1])]
        longitude, latitude = tower.get("longitude"), tower.get("latitude")
        if longitude is None or latitude is None or transform is None:
            return None
        converter = getattr(transform, "to_metric", None)
        if not callable(converter):
            return None
        try:
            converted = converter([float(longitude), float(latitude)])
        except Exception:  # noqa: BLE001 - 变换失败必须如实降级为 unknown
            return None
        if not (isinstance(converted, (list, tuple)) and len(converted) >= 2):
            return None
        return [float(converted[0]), float(converted[1])]

    def _evaluate_combined(self, towers, *, physical, probes, native_source,
                           building_source, transform, layer_id, altitude_m, parameters):
        """在有界预算内逐 (塔 × probe) 评价**同一条射线**上的地形与建筑两个域。

        ``probe_budget`` 是**全部塔合计**的上限，因此公平分给每座塔；被省下的 probe
        数与建筑域预算耗尽都如实记入 ``truncation`` / 收据，绝不用 ``clear`` 冒充。
        """

        context = building_los.BuildingLosExecutionContext(
            building_source=building_source, terrain_source=native_source, transform=transform,
            max_rays=MAX_COMBINED_BUILDING_RAYS, max_queries=MAX_COMBINED_BUILDING_QUERIES,
            max_features_per_ray=MAX_COMBINED_BUILDING_FEATURES_PER_RAY,
            max_elapsed_seconds=MAX_COMBINED_BUILDING_ELAPSED_SECONDS,
        )

        def terrain_evaluator(*, tower, sample, radar_type, geometry):
            del geometry
            record = physical.get(str(tower.get("tower_id") or "")) or {}
            return terrain_los.evaluate_probe_terrain_los(
                tower, sample, terrain_source=native_source, transform=transform,
                altitude_layer_id=layer_id,
                nominal_altitude_m=sample.get("nominal_altitude_m"),
                sampling_interval_m=terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                envelope_radius_m=terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                earth_radius_m=terrain_los.EARTH_MEAN_RADIUS_M,
                k_factor=terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                origin_physical_input=record,
                origin_availability=record.get("origin_availability"),
                max_slant_range_m=parameters.get("max_slant_range_m"),
                profile_detail=terrain_los.PROFILE_DETAIL_SUMMARY,
            )

        def building_evaluator(*, tower, sample, radar_type, geometry):
            del geometry
            return context.evaluate(tower, sample, radar_type=str(radar_type))

        lookup = lookup_domain.TerrainLosLookup(
            mode=lookup_domain.TERRAIN_LOS_MODE_ENFORCED,
            evaluator=terrain_evaluator, building_evaluator=building_evaluator,
            building_mode=lookup_domain.BUILDING_MODE_COMBINED,
            source_identity=terrain_los.terrain_source_identity(native_source),
            building_source_identity=building_source.describe(),
            route_id=None,
            sampling={
                "sampling_interval_m": terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                "envelope_radius_m": terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
            },
            max_ray_count=min(
                max(1, len(probes) * max(1, len(towers))), MAX_COMBINED_RAY_TOTAL,
            ),
        )
        per_tower = max(1, int(-(-len(probes) // max(1, len(towers))))) if probes else 0
        budget = MAX_COMBINED_RAY_TOTAL
        evaluations = []
        truncation = {
            "selected_tower_count": len(towers),
            "evaluated_tower_count": 0,
            "evaluated_probe_count": 0,
            "probe_budget_per_tower": per_tower,
            "max_ray_total": MAX_COMBINED_RAY_TOTAL,
            "truncated_tower_count": 0,
            "unevaluated_probe_count": 0,
            "ray_budget_exhausted": False,
        }
        for tower in towers:
            if budget <= 0 or per_tower <= 0:
                truncation["truncated_tower_count"] += 1
                truncation["unevaluated_probe_count"] += len(probes)
                truncation["ray_budget_exhausted"] = True
                continue
            tower_probes = list(probes)[: min(per_tower, budget)]
            budget -= len(tower_probes)
            truncation["evaluated_tower_count"] += 1
            truncation["unevaluated_probe_count"] += max(0, len(probes) - len(tower_probes))
            for probe in tower_probes:
                #: 建筑域需要显式 EGM2008 目标高度；高度**只**来自权威高度层，
                #: 绝不用代表高度兜底，也绝不当 0。
                probe_record = {
                    **probe,
                    "egm2008_m": altitude_m,
                    "altitude_egm2008_m": altitude_m,
                }
                evaluation = lookup.evaluate(tower, probe_record, RADAR_TYPE_I)
                evaluations.append({
                    "evaluation": evaluation, "tower": tower, "probe": probe_record,
                })
        truncation["evaluated_probe_count"] = len(evaluations)
        if context.receipt().get("budget_exhausted"):
            truncation["building_budget_exhausted"] = True
        return evaluations, truncation, lookup, context

    # ------------------------------------------------------------------ 内部装配

    def _policy(self):
        state = self.session.state if isinstance(self.session.state, dict) else {}
        return deepcopy(state.get("radar_surveillance_policy") or {})

    @staticmethod
    def _tower_state_items(state):
        """真实铁塔站址列表。

        兼容既有三种真实形态：``{"items": [...]}`` 集合包装（真实项目、
        ``radar_surveillance_layout_service._tower_items`` 的既有口径）、裸
        ``[...]`` 列表，以及按 ``tower_id`` 索引的 ``{id: record}`` 字典。
        绝不新增第二个站址容器，也绝不改写站址身份。
        """

        towers = state.get("towers")
        if isinstance(towers, dict):
            items = towers.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [item for item in towers.values() if isinstance(item, dict)]
        if isinstance(towers, list):
            return [item for item in towers if isinstance(item, dict)]
        return []

    def _resolve_origins(self, state, towers, policy):
        from ..gis.radar_layout_adapter import resolve_radar_origins

        collection = state.get("tower_obstacle_profiles")
        collection = collection if isinstance(collection, dict) else {}
        kwargs = {
            "towers": list(towers),
            "obstacle_profiles": collection,
            "mount_assumption": (policy or {}).get("radar_mount_height"),
        }
        # Round34-T07-R 正式集成适配：正式仓库的 ``resolve_radar_origins`` 仍是 V1.1
        # 单层塔顶语义，没有 Round34-T05 的 ``planning_policy_id`` / ``device_origins``
        # 分层复用。诊断入口必须与**生产布局同口径**，因此按运行时可用签名传参：
        # 只在参数存在时才传，绝不在此发展第二套原点解析、绝不猜默认原点。
        parameters = inspect.signature(resolve_radar_origins).parameters
        if "planning_policy_id" in parameters:
            kwargs["planning_policy_id"] = (policy or {}).get(
                "radar_planning_origin_policy_id"
            )
        if "device_origins" in parameters:
            kwargs["device_origins"] = list(towers)
        return resolve_radar_origins(**kwargs)

    def _route_probes(self, state, *, layer_id, altitude_m, transform,
                      probe_budget, route_filter, routes=None):
        """沿**当前权威运行航路**按既有采样链构造候选 probe（复用 ``build_route_samples``）。

        probe 高度**只**取自权威高度层的 ``nominal_altitude_m``，绝不使用走廊体素的
        代表高度。返回的 probe 列表按"沿全部航路依次、按全局 stride 抽稀、再截断到
        ``probe_budget``"构造，因此**确定**且**有界**。
        """

        from ..algorithms.radar_layout.v1 import build_route_samples

        spacing = float(((self._policy() or {}).get("optimization_sample_spacing_m")) or 25.0)
        routes = list(routes) if routes is not None else [
            item for item in state.get("operational_routes") or []
            if isinstance(item, dict) and str(item.get("status")) == "passed"
        ]
        routes.sort(key=lambda item: str(item.get("route_id") or ""))
        if route_filter:
            wanted = set(route_filter)
            routes = [item for item in routes if str(item.get("route_id") or "") in wanted]
        candidates, per_route = [], []
        for route in routes:
            route_id = str(route.get("route_id") or "")
            metric_path = self._route_metric_path(route, transform=transform)
            if not metric_path:
                continue
            built = build_route_samples(
                metric_path=metric_path, spacing_m=spacing, route_id=route_id,
                fixed_altitude_m=float(altitude_m if altitude_m is not None else 0.0),
                altitude_layer_id=layer_id,
                coordinate_resolver=None,
            )
            samples = list(built.get("samples") or [])
            per_route.append((route_id, samples))
            for sample in samples:
                metric = list(sample.get("metric") or [])
                longitude, latitude = self._geographic_of(transform, metric)
                candidates.append({
                    "voxel_id": f"{route_id}:{sample.get('sample_id')}",
                    "grid_id": None,
                    "route_id": route_id,
                    "longitude": longitude,
                    "latitude": latitude,
                    "altitude_layer_id": layer_id,
                    "nominal_altitude_m": altitude_m,
                    "metric": metric,
                    "distance_along_route_m": sample.get("distance_along_route_m"),
                    "sample_index": sample.get("sample_index"),
                })
        candidate_total = len(candidates)
        stride = self._stride_for(candidate_total, budget=probe_budget)
        selected = candidates[::stride][:probe_budget] if stride > 0 else []
        return selected, {
            "route_count": len(per_route),
            "candidate_probe_count": candidate_total,
            "selected_probe_count": len(selected),
            "truncated_probe_count": max(0, candidate_total - len(selected)),
            "probe_stride": stride,
            "sampling_spacing_m": spacing,
            "probe_height_source": "authoritative_altitude_layer_nominal_altitude_m",
        }

    @staticmethod
    def _stride_for(count, *, budget):
        if budget <= 0 or count <= budget:
            return 1
        return max(1, int(-(-count // budget)))

    @staticmethod
    def _geographic_of(transform, metric):
        """米制 probe 位置 → 经纬度（probe 只读身份字段）。

        逆投影**复用**已注入的显式变换；不可用时如实返回 ``(None, None)`` —— 该 probe
        的 LOS 会因为缺少可解析位置而保持 ``unknown``，**绝不**用米制数值冒充经纬度。
        """

        if not metric:
            return None, None
        inverse = getattr(transform, "to_geographic", None)
        if not callable(inverse):
            return None, None
        try:
            geographic = inverse([float(metric[0]), float(metric[1])])
        except Exception:  # noqa: BLE001 - 逆投影失败必须如实降级为 unknown
            return None, None
        if not (isinstance(geographic, (list, tuple)) and len(geographic) >= 2):
            return None, None
        return float(geographic[0]), float(geographic[1])

    def _route_metric_path(self, route, *, transform):
        """取航路米制几何：优先已保存的米制路径，否则用注入的显式投影换算。"""

        for key in ("metric_path", "path_metric"):
            value = route.get(key)
            if isinstance(value, list) and len(value) >= 2:
                points = [
                    [float(point[0]), float(point[1])] for point in value
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                if len(points) >= 2:
                    return points
        path = route.get("path")
        if not isinstance(path, list) or len(path) < 2:
            return None
        converted = []
        for point in path:
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                return None
            metric = transform.to_metric([point[0], point[1]])
            if metric is None:
                return None
            converted.append([float(metric[0]), float(metric[1])])
        return converted

    @staticmethod
    def _select_towers(records, physical, *, max_tower_count):
        """确定性的有界铁塔抽样：先可用原点、后不可评价；同组按 ``tower_id`` 排序。

        抽样**只**决定"本次评价哪些塔"，**绝不**删除、改写或降级任何站址；
        被抽中的不可评价塔在 LOS 主评价里仍然返回 ``unknown``（scenario_only 不计入）。
        """

        def _availability(record):
            return str((physical.get(str(record.get("tower_id"))) or {}).get(
                "origin_availability"
            ) or "")

        def _key(item):
            return str(item.get("tower_id") or "")

        usable = sorted(
            [item for item in records if _availability(item) == los_domain.ORIGIN_AVAILABILITY_USABLE],
            key=_key,
        )
        rest = sorted(
            [item for item in records if _availability(item) != los_domain.ORIGIN_AVAILABILITY_USABLE],
            key=_key,
        )
        selected = usable[:max_tower_count]
        if len(selected) < max_tower_count:
            selected = selected + rest[: max_tower_count - len(selected)]
        return selected

    def _evaluate(self, towers, *, physical, probes, native_source, transform, layer_id,
                  parameters):
        """在有界预算内逐塔评价。

        ``probe_budget`` 是**全部塔合计**的上限，因此必须**公平分给每座塔**：否则第一座
        塔会把预算全部吃掉，其余塔一座也评不了（"多塔"在响应里会退化成"单塔"）。
        分不完的余数给前面的塔，被省下的 probe 数如实记入 ``truncation``。
        """

        evaluations = []
        budget = MAX_EVALUATED_PROBE_TOTAL
        per_tower = max(1, int(-(-len(probes) // max(1, len(towers))))) if probes else 0

        truncation = {
            "selected_tower_count": len(towers),
            "evaluated_tower_count": 0,
            "evaluated_probe_count": 0,
            "probe_budget_per_tower": per_tower,
            "max_evaluated_probe_total": MAX_EVALUATED_PROBE_TOTAL,
            "truncated_tower_count": 0,
            "unevaluated_probe_count": 0,
        }
        for tower in towers:
            tower_id = str(tower.get("tower_id") or "")
            physical_record = physical.get(tower_id) or {}
            availability = physical_record.get("origin_availability")
            if budget <= 0 or per_tower <= 0:
                truncation["truncated_tower_count"] += 1
                truncation["unevaluated_probe_count"] += len(probes)
                continue
            tower_probes = list(probes)[: min(per_tower, budget)]
            budget -= len(tower_probes)
            truncation["evaluated_tower_count"] += 1
            truncation["unevaluated_probe_count"] += max(0, len(probes) - len(tower_probes))
            for probe in tower_probes:
                evaluation = terrain_los.evaluate_probe_terrain_los(
                    tower, probe, terrain_source=native_source, transform=transform,
                    altitude_layer_id=layer_id,
                    nominal_altitude_m=probe.get("nominal_altitude_m"),
                    sampling_interval_m=terrain_los.DEFAULT_PROFILE_SAMPLING_INTERVAL_M,
                    envelope_radius_m=terrain_los.DEFAULT_PROFILE_ENVELOPE_RADIUS_M,
                    earth_radius_m=terrain_los.EARTH_MEAN_RADIUS_M,
                    k_factor=terrain_los.DEFAULT_REFRACTION_K_FACTOR,
                    origin_physical_input=physical_record,
                    origin_availability=availability,
                    max_slant_range_m=parameters.get("max_slant_range_m"),
                    #: 只读诊断**绝不**下发完整地形剖面：一个 3 km 射线 × 5 m 采样
                    #: 就是 600+ 格，批量会让响应膨胀到几十 MB。
                    profile_detail=terrain_los.PROFILE_DETAIL_SUMMARY,
                )
                evaluation["route_id"] = probe.get("route_id")
                evaluations.append(evaluation)
        truncation["evaluated_probe_count"] = len(evaluations)
        return evaluations, truncation


__all__ = [
    "COMBINED_PROBE_BUDGET_LIMIT", "COMBINED_SAMPLE_LIMIT_LIMIT",
    "COMBINED_TOWER_COUNT_LIMIT", "DEFAULT_COMBINED_DETAIL_PAGE_LIMIT",
    "DEFAULT_COMBINED_PROBE_BUDGET", "DEFAULT_COMBINED_SAMPLE_LIMIT",
    "DEFAULT_COMBINED_TOWER_COUNT", "DEFAULT_DETAIL_PAGE_LIMIT",
    "DEFAULT_MAX_TOWER_COUNT", "DEFAULT_PROBE_BUDGET", "DEFAULT_RECORD_SAMPLE_LIMIT",
    "LOW_CLEARANCE_HINT_M", "MAX_COMBINED_DETAIL_PAGE_LIMIT", "MAX_COMBINED_RAY_TOTAL",
    "MAX_DETAIL_PAGE_LIMIT", "MAX_EVALUATED_PROBE_TOTAL", "MAX_TOWER_COUNT_LIMIT",
    "PROBE_BUDGET_LIMIT", "RECORD_SAMPLE_LIMIT_LIMIT",
    "RadarTerrainLosDiagnosticsService",
]
