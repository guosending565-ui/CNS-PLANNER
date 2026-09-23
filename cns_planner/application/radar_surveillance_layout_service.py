"""Radar Surveillance Layout V1 — Application 编排（唯一写入者）。

职责
----

1. 显式用户动作（``POST /api/radar-surveillance-layout/evaluate``）时，把真实 GIS 事实
   投影为算法的**标准输入**（米制航路、逐点 EGM2008 正高、逐点 surface_class、
   塔站址与雷达原点高度）；
2. 调用纯算法链 :mod:`cns_planner.algorithms.radar_layout`（两阶段 MILP + 5 m 复核）；
3. 保存 ``radar_surveillance_policy`` / ``radar_surveillance_layout`` 两个**独立**状态。

绝不自动写
----------

``existing_cns_facilities`` / ``coverage_3d`` / ``cns_service_capability`` /
``cns_corridor_assessment`` / ``cns_corridor_gap_assessment`` / ``cns_corridor_site_plan``
一律不被本服务触碰（``proposal_only = true``），设备目录中的其它 C/N/S 数据也不被修改。
"""

from __future__ import annotations

from copy import deepcopy

from ..algorithms.coverage.geometric_3d import path_length_m
from ..algorithms.radar_layout.v1 import (
    SOFTWARE_BASELINE, build_route_samples, parameters_block, solve_layout,
)
from ..domain.radar_surveillance_layout import (
    ALGORITHM_ID, ALGORITHM_NAME, ALGORITHM_SEMANTICS, ALGORITHM_VERSION,
    FIXED_ALTITUDE_LAYER_ID, FIXED_ALTITUDE_M, METRIC_CRS, MODEL_SCOPE, NOT_EVALUATED,
    SCHEMA_VERSION, VERTICAL_REFERENCE, default_radar_mount_assumption, device_provenance,
    device_summary, normalize_radar_mount_assumption, radar_geometry_parameters,
)
from ..domain.route_safety_evidence_v2 import stable_fingerprint, utc_now
from ..gis.radar_layout_adapter import (
    LAND_MASK_SEMANTICS, radar_layout_source_status, radar_mount_assumption_status,
    resolve_radar_origins,
)

POLICY_KEY = "radar_surveillance_policy"
LAYOUT_KEY = "radar_surveillance_layout"
STATUS_KEY = "radar_surveillance_layout"

PROPOSAL_ONLY = True

#: 本服务**绝不**写入的状态键（写入即违规）。
FORBIDDEN_WRITE_KEYS = (
    "existing_cns_facilities", "coverage_3d", "cns_service_capability",
    "cns_corridor_assessment", "cns_corridor_gap_assessment", "cns_corridor_site_plan",
    "cns_site_plan", "device_catalog", "operational_routes",
)

#: 权威结果容器摘要形状（与 ``domain.result_statuses`` 对齐）。
LAYOUT_STATUSES = (
    "not_calculated", "stale", "proposal_ready", "infeasible",
    "refinement_incomplete", "search_incomplete", "solver_error", "solver_unavailable",
    "not_ready", "unresolved",
)

BOUNDARIES = {
    "proposal_only": PROPOSAL_ONLY,
    "modifies_operational_routes": False,
    "creates_or_modifies_existing_cns_facilities": False,
    "writes_coverage_3d_or_cns_service_capability": False,
    "writes_cns_corridor_assessment_or_site_plan": False,
    "modifies_geometric_coverage_3d_sphere_hemisphere_semantics": False,
    "automatic_apply_of_proposal": False,
    "automatic_generation": False,
    "arbitrary_new_tower_creation": False,
    "terrain_los_or_radar_equation_evaluated": False,
    "elevation_angle_optimization": False,
    "route_replanning": False,
    "joint_cns_optimization": False,
    "solver_greedy_fallback_used": False,
}

READINESS_SEMANTICS = {
    "passed_operational_route_required": True,
    "real_tower_sites_required": True,
    "tower_radar_origin_egm2008_required": True,
    "explicit_radar_mount_height_required_and_never_hardcoded": True,
    "verified_fabdem_dtm_egm2008_required_for_route_sampling": True,
    "explicit_land_mask_source_required_for_land_sea_classification": True,
    "dem_nodata_is_never_used_to_infer_sea": True,
    "unknown_surface_class_is_fail_closed": True,
    "explicit_user_evaluation_required": True,
    "missing_item_is_not_ready_and_not_zero": True,
}


# ------------------------------------------------------------------------------ policy


def default_radar_surveillance_policy():
    """策略默认值：25 m / 5 m / 3 轮 + 未配置挂高（绝不填 0）。"""

    return {
        "status": "pending_confirmation",
        "optimization_sample_spacing_m": SOFTWARE_BASELINE["optimization_sample_spacing_m"],
        "validation_sample_spacing_m": SOFTWARE_BASELINE["validation_sample_spacing_m"],
        "max_refinement_rounds": SOFTWARE_BASELINE["max_refinement_rounds"],
        "allow_mixed_radar_types": True,
        "solver_time_limit_s": None,
        "radar_mount_height": default_radar_mount_assumption(),
        "software_baseline": deepcopy(SOFTWARE_BASELINE),
        "fixed_altitude_layer_id": FIXED_ALTITUDE_LAYER_ID,
        "fixed_altitude_m": FIXED_ALTITUDE_M,
        "vertical_reference": VERTICAL_REFERENCE,
        "metric_crs": METRIC_CRS,
        "source": "cns_planner_software_algorithm_baseline",
        "confirmed": False,
    }


def _positive_number(value, field, *, default):
    if value in (None, ""):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数值") from exc
    if number != number or abs(number) == float("inf"):
        raise ValueError(f"{field} 必须是有限数值")
    if number <= 0:
        raise ValueError(f"{field} 必须为正")
    return number


def normalize_radar_surveillance_policy(value):
    payload = value if isinstance(value, dict) else {}
    result = default_radar_surveillance_policy()
    result["optimization_sample_spacing_m"] = _positive_number(
        payload.get("optimization_sample_spacing_m"),
        "optimization_sample_spacing_m",
        default=SOFTWARE_BASELINE["optimization_sample_spacing_m"],
    )
    result["validation_sample_spacing_m"] = _positive_number(
        payload.get("validation_sample_spacing_m"),
        "validation_sample_spacing_m",
        default=SOFTWARE_BASELINE["validation_sample_spacing_m"],
    )
    rounds = payload.get("max_refinement_rounds")
    if rounds in (None, ""):
        result["max_refinement_rounds"] = int(SOFTWARE_BASELINE["max_refinement_rounds"])
    else:
        try:
            rounds = int(rounds)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_refinement_rounds 必须是整数") from exc
        if rounds < 0:
            raise ValueError("max_refinement_rounds 不能为负")
        result["max_refinement_rounds"] = rounds
    result["allow_mixed_radar_types"] = payload.get("allow_mixed_radar_types") is not False
    limit = payload.get("solver_time_limit_s")
    if limit in (None, ""):
        result["solver_time_limit_s"] = None
    else:
        try:
            limit = float(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("solver_time_limit_s 必须是数值") from exc
        result["solver_time_limit_s"] = limit if limit > 0 else None
    mount_payload = payload.get("radar_mount_height")
    if mount_payload is None and "radar_mount_height_m" in payload:
        mount_payload = {
            "radar_mount_height_m": payload.get("radar_mount_height_m"),
            "mount_height_basis": payload.get("mount_height_basis"),
            "source": payload.get("mount_height_source") or payload.get("source"),
            "confirmed": payload.get("mount_height_confirmed"),
            "parameter_origin": payload.get("mount_height_parameter_origin"),
        }
    result["radar_mount_height"] = normalize_radar_mount_assumption(mount_payload)
    mount_status = str(result["radar_mount_height"].get("status") or "not_configured")
    result["status"] = (
        "confirmed" if mount_status == "confirmed" else "pending_confirmation"
    )
    result["source"] = str(payload.get("source") or result["source"])
    result["confirmed"] = payload.get("confirmed") is True
    return result


# ------------------------------------------------------------------------------ helpers


def _source_fingerprint_component(record):
    """来源 -> 指纹分量：只保留**稳定**事实（配置路径 + 可用性 + 格式声明）。"""

    record = record if isinstance(record, dict) else {}
    configured = record.get("configured_path")
    if configured in (None, ""):
        configured = record.get("path")
    return {
        "configured_path": str(configured) if configured else None,
        "available": bool(record.get("available") or record.get("ok")),
        "semantics": record.get("semantics"),
        "role": record.get("role"),
    }


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _state_route(state, route_id):
    for route in state.get("operational_routes") or []:
        if isinstance(route, dict) and str(route.get("route_id")) == str(route_id):
            return route
    return None


def _route_ids(state, payload):
    requested = (payload or {}).get("route_id")
    ids = [
        str(item.get("route_id"))
        for item in state.get("operational_routes") or []
        if isinstance(item, dict)
    ]
    if requested in (None, "", "all"):
        return ids
    wanted = str(requested)
    if wanted not in ids:
        raise ValueError(f"运行航路不存在：{wanted}")
    return [wanted]


def _tower_items(state):
    collection = state.get("towers") or {}
    items = collection.get("items") if isinstance(collection, dict) else None
    return {
        str(item.get("tower_id")): item
        for item in items or []
        if isinstance(item, dict) and item.get("tower_id")
    }


# ------------------------------------------------------------------------------ service


class RadarSurveillanceLayoutService:
    """``radar_surveillance_policy`` / ``radar_surveillance_layout`` 的唯一写入者。"""

    def __init__(self, session, invalidation, snapshot):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: 由 composition root 注入：``(metric_point) -> [lon, lat]``，使用与
        #: 塔/地形查询相同的显式米制 CRS（舟山 ⇒ EPSG:32651）。
        self.to_geographic = None
        #: 由 composition root 注入的只读事实提供者。
        self.facts_provider = None

    # ------------------------------------------------------------------ containers
    def _policy(self):
        return normalize_radar_surveillance_policy(self.session.state.get(POLICY_KEY))

    def _stored(self):
        value = self.session.state.get(LAYOUT_KEY)
        if not isinstance(value, dict):
            return empty_radar_surveillance_layout()
        result = deepcopy(value)
        result.setdefault("items", [])
        result.setdefault("collection_id", LAYOUT_KEY)
        result.setdefault("schema_version", SCHEMA_VERSION)
        return result

    def policy_snapshot(self):
        return self._policy()

    def set_policy(self, payload=None):
        state = self.session.state
        payload = payload if isinstance(payload, dict) else {}
        raw = payload.get(POLICY_KEY) if isinstance(payload.get(POLICY_KEY), dict) else payload
        candidate = normalize_radar_surveillance_policy(raw)
        previous = normalize_radar_surveillance_policy(state.get(POLICY_KEY))
        if candidate != previous:
            state[POLICY_KEY] = candidate
            # 策略变化只 stale 本产物（proposal-only，unidirectional）。
            self.stale_for_reason("radar_surveillance_policy_changed")
        else:
            state[POLICY_KEY] = candidate
        self.session.save()
        return self.snapshot()

    def source_status(self):
        provider = self.facts_provider or {}
        paths = provider.get("paths") if isinstance(provider, dict) else {}
        return radar_layout_source_status(self.session.state, paths or {})

    def readiness_snapshot(self):
        """只读 readiness：不打开任何大数据集。"""

        state = self.session.state
        routes = [
            item for item in state.get("operational_routes") or []
            if isinstance(item, dict)
        ]
        passed_routes = [item for item in routes if str(item.get("status")) == "passed"]
        towers = _tower_items(state)
        profiles = (state.get("tower_obstacle_profiles") or {})
        profiles = profiles.get("items") if isinstance(profiles, dict) else {}
        profiles = profiles if isinstance(profiles, dict) else {}
        resolved = sum(
            1 for item in profiles.values()
            if isinstance(item, dict) and item.get("tower_top_orthometric_m") is not None
        )
        mount = radar_mount_assumption_status(state)
        return {
            "status": "passed" if (passed_routes and towers and resolved and mount["status"] != "not_configured") else "not_ready",
            "algorithm_id": ALGORITHM_ID,
            "algorithm_version": ALGORITHM_VERSION,
            "algorithm_name": ALGORITHM_NAME,
            "algorithm_semantics": ALGORITHM_SEMANTICS,
            "model_scope": MODEL_SCOPE,
            "proposal_only": PROPOSAL_ONLY,
            "passed_operational_route_count": len(passed_routes),
            "operational_route_count": len(routes),
            "tower_count": len(towers),
            "tower_with_resolved_radar_base_count": resolved,
            "radar_mount_height": mount,
            "fixed_altitude": {
                "altitude_layer_id": FIXED_ALTITUDE_LAYER_ID,
                "altitude_m": FIXED_ALTITUDE_M,
                "vertical_reference": VERTICAL_REFERENCE,
                "semantics": "fixed_route_height_not_optimised",
            },
            "parameters": parameters_block(),
            "device_summary": device_summary(),
            "sources": self.source_status(),
            "semantics": deepcopy(READINESS_SEMANTICS),
            "boundaries": deepcopy(BOUNDARIES),
            "not_evaluated": deepcopy(NOT_EVALUATED),
        }

    # ------------------------------------------------------------------ projection
    def result_snapshot(self, route_id=None):
        """只读投影：按当前上游输入重算 stale + 业务摘要。

        ``route_id`` 给定时只返回该航路（逐 sample 明细同样被裁剪）。
        """

        stored = self._stored()
        wanted = str(route_id) if route_id else None
        items = []
        for item in stored.get("items") or []:
            if not isinstance(item, dict):
                continue
            if wanted and str(item.get("route_id")) != wanted:
                continue
            entry = deepcopy(item)
            fingerprint = entry.get("input_fingerprint")
            if entry.get("status") != "stale":
                current = self.input_fingerprint(entry.get("route_id"))
                if fingerprint and current and fingerprint != current:
                    entry["status"] = "stale"
                    entry["stale_reason"] = "radar_surveillance_inputs_changed"
            items.append(entry)
        status = "not_calculated"
        if items:
            statuses = {str(item.get("status")) for item in items}
            if statuses == {"stale"}:
                status = "stale"
            elif "stale" in statuses:
                status = "pending_confirmation"
            else:
                status = "passed"
        return {
            **{key: value for key, value in stored.items() if key != "items"},
            "status": status,
            "count": len(items),
            "items": items,
            "by_route": {
                str(item.get("route_id")): item for item in items
            },
            "proposal_only": PROPOSAL_ONLY,
            "boundaries": deepcopy(BOUNDARIES),
            "semantics": {
                "read_only_projection": True,
                "stale_is_recomputed_from_current_inputs": True,
                "explicit_evaluation_required": True,
                "automatic_generation": False,
            },
        }

    # ------------------------------------------------------------------ summary
    def summary_snapshot(self):
        """有界摘要（供通用 workflow 快照使用，不含逐 sample 明细）。"""

        full = self.result_snapshot()
        items = []
        for item in full.get("items") or []:
            validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
            items.append({
                "route_id": item.get("route_id"),
                "status": item.get("status"),
                "stage": item.get("stage"),
                "stage_label": item.get("stage_label"),
                "altitude_layer_id": item.get("altitude_layer_id"),
                "altitude_m": item.get("altitude_m"),
                "vertical_reference": item.get("vertical_reference"),
                "selected_panel_count": item.get("selected_panel_count"),
                "selected_tower_count": item.get("selected_tower_count"),
                "radar_i_panel_count": item.get("radar_i_panel_count"),
                "radar_ii_panel_count": item.get("radar_ii_panel_count"),
                "candidate_tower_count": item.get("candidate_tower_count"),
                "candidate_panel_count": item.get("candidate_panel_count"),
                # 雷达原点与挂高假设（含 pending_confirmation 语义）必须在摘要里可见。
                "radar_origin": {
                    "mount_assumption": (
                        (item.get("radar_origin") or {}).get("mount_assumption")
                    ),
                    "mount_assumption_status": (
                        (item.get("radar_origin") or {}).get("mount_assumption_status")
                    ),
                    "resolve_status": (item.get("radar_origin") or {}).get("status"),
                    "resolved_count": (item.get("radar_origin") or {}).get("resolved_count"),
                    "unresolved_count": (item.get("radar_origin") or {}).get("unresolved_count"),
                    "backend_hardcoded_mount_height": False,
                },
                # 地图 overlay 需要**已选方案**的几何；未选 panel 的 coverage polygon 一律不下发
                # （否则会一次产生数百个多边形，造成地图性能问题）。
                "selected_panels": item.get("selected_panels") or [],
                "selected_tower_ids": item.get("selected_tower_ids") or [],
                "coverage_profile": validation.get("coverage_profile") or {
                    "entries": [], "count": 0, "truncated": False,
                },
                "solver": (
                    {
                        "name": (item.get("solver") or {}).get("name"),
                        "library": (item.get("solver") or {}).get("library"),
                        "stage": (item.get("solver") or {}).get("stage"),
                        "status": (item.get("solver") or {}).get("status"),
                        "optimality_proven": (item.get("solver") or {}).get("optimality_proven"),
                        "infeasibility_proven": (item.get("solver") or {}).get("infeasibility_proven"),
                        "mip_gap": (item.get("solver") or {}).get("mip_gap"),
                        "objective": (item.get("solver") or {}).get("objective"),
                        "message": (item.get("solver") or {}).get("message"),
                        "greedy_fallback_used": (item.get("solver") or {}).get("greedy_fallback_used"),
                    }
                    if isinstance(item.get("solver"), dict) else None
                ),
                "land": validation.get("land"),
                "sea": validation.get("sea"),
                "unknown": validation.get("unknown"),
                "coverage_summary": item.get("coverage_summary"),
                "parameters": item.get("parameters"),
                "refinement_rounds": [
                    {
                        "round_index": entry.get("round_index"),
                        "violation_count": entry.get("violation_count"),
                        "panel_count": entry.get("panel_count"),
                        "spacing_m": entry.get("spacing_m"),
                    }
                    for entry in item.get("refinement_rounds") or []
                ],
                "infeasibility_reasons": (item.get("infeasibility_reasons") or [])[:20],
                "unknown_evidence": (item.get("unknown_evidence") or [])[:20],
                "input_fingerprint": item.get("input_fingerprint"),
                "stale_reason": item.get("stale_reason"),
                "proposal_only": True,
            })
        return {
            "status": full.get("status"),
            "count": len(items),
            "items": items,
            "by_route": {str(item.get("route_id")): item for item in items},
            "detail_endpoint": "/api/radar-surveillance-layout",
            "semantics": {
                "bounded_summary_projection": True,
                "per_sample_detail_omitted": True,
                "unselected_panel_coverage_polygons_not_emitted": True,
                "coverage_profile_is_contiguous_same_status": True,
                "explicit_evaluation_required": True,
            },
            "proposal_only": PROPOSAL_ONLY,
        }

    # ------------------------------------------------------------------ fingerprints
    def input_fingerprint(self, route_id):
        """当前上游输入的确定性指纹（route / towers / 设备 / 陆域 / 挂高 / 策略）。"""

        return stable_fingerprint(
            self._fingerprint_components(route_id), prefix="radarlayout-",
        )

    def _fingerprint_components(self, route_id):
        """指纹输入分量（同时供诊断使用：逐分量 diff 定位变化来源）。"""

        state = self.session.state
        policy = self._policy()
        land_mask_status = self.source_status()
        route = _state_route(state, route_id) if route_id else None
        profiles = (state.get("tower_obstacle_profiles") or {})
        profiles = profiles.get("items") if isinstance(profiles, dict) else {}
        profiles = profiles if isinstance(profiles, dict) else {}
        spatial = state.get("spatial_3d") or {}
        layers = {
            str(item.get("altitude_layer_id")): item
            for item in spatial.get("altitude_layers") or []
            if isinstance(item, dict)
        }
        layer = layers.get(FIXED_ALTITUDE_LAYER_ID)
        towers = _tower_items(state)
        components = {
            "algorithm_id": ALGORITHM_ID,
            "algorithm_version": ALGORITHM_VERSION,
            "model_scope": MODEL_SCOPE,
            "route": (
                {
                    "route_id": str(route.get("route_id")),
                    "status": route.get("status"),
                    "distance_m": route.get("distance_m"),
                    "path_length_m": path_length_m(route.get("path") or []) if len(
                        route.get("path") or []
                    ) >= 2 else None,
                    "path_fingerprint": stable_fingerprint(
                        route.get("path") or [], prefix="radarroutepath-",
                    ),
                }
                if route else None
            ),
            "fixed_altitude_layer": {
                "altitude_layer_id": FIXED_ALTITUDE_LAYER_ID,
                # 只绑定**语义相关**的稳定字段：ALT-080 的目录条目在项目恢复时会被幂等
                # 补建（`ensure_default_altitude_layers`），若把整条记录（含 created_at /
                # evidence 等）纳入指纹，重新打开项目就会把刚求出的方案误判为 stale。
                "definition": (
                    {
                        "altitude_layer_id": layer.get("altitude_layer_id"),
                        "nominal_altitude_m": layer.get("nominal_altitude_m"),
                        "lower_altitude_m": layer.get("lower_altitude_m"),
                        "upper_altitude_m": layer.get("upper_altitude_m"),
                        "vertical_reference": layer.get("vertical_reference"),
                        "status": layer.get("status"),
                        "confirmed": layer.get("confirmed"),
                    }
                    if isinstance(layer, dict) else None
                ),
                "altitude_m": FIXED_ALTITUDE_M,
                "vertical_reference": VERTICAL_REFERENCE,
            },
            "towers": {
                "count": len(towers),
                "fingerprint": stable_fingerprint(
                    [
                        {
                            "tower_id": key,
                            "longitude": value.get("longitude"),
                            "latitude": value.get("latitude"),
                            "site_type": value.get("site_type"),
                            "height_m": value.get("height_m"),
                        }
                        for key, value in sorted(towers.items())
                    ],
                    prefix="radartowers-",
                ),
                "derived": {
                    "resolved_count": sum(
                        1 for item in profiles.values()
                        if isinstance(item, dict) and item.get("tower_top_orthometric_m") is not None
                    ),
                    "fingerprint": stable_fingerprint(
                        {
                            key: {
                                "tower_top_orthometric_m": value.get("tower_top_orthometric_m"),
                                "status": value.get("status"),
                                "vertical_status": value.get("vertical_status"),
                                "building_height_m": value.get("building_height_m"),
                            }
                            for key, value in sorted(profiles.items())
                            if isinstance(value, dict)
                        },
                        prefix="radarderived-",
                    ),
                },
            },
            "device": {
                "provenance": device_provenance()["source"]["sha256"],
                "radar_i": radar_geometry_parameters("radar_i"),
                "radar_ii": radar_geometry_parameters("radar_ii"),
            },
            # 来源分量只绑定**稳定事实**：配置路径（若有）与其可用性。
            # 运行期 readiness 的 reason/detail 会随「未配置」与「已配置但文件不存在」
            # 之间切换（项目重新打开时 provider 可能尚未注入），若纳入指纹会把刚求出的
            # 方案误判为 stale。reason/detail 仍完整保存在结果的 `sources` 里供审计。
            "land_mask": _source_fingerprint_component(land_mask_status["land_mask"]),
            "terrain_dtm": _source_fingerprint_component(land_mask_status["terrain"]),
            "radar_mount_height": policy.get("radar_mount_height"),
            "policy": {
                "optimization_sample_spacing_m": policy.get("optimization_sample_spacing_m"),
                "validation_sample_spacing_m": policy.get("validation_sample_spacing_m"),
                "max_refinement_rounds": policy.get("max_refinement_rounds"),
                "allow_mixed_radar_types": policy.get("allow_mixed_radar_types"),
                "solver_time_limit_s": policy.get("solver_time_limit_s"),
            },
        }
        return components

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, payload=None, *, facts_provider=None):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state

        if any(key in payload for key in (
            "optimization_sample_spacing_m", "validation_sample_spacing_m",
            "max_refinement_rounds", "radar_mount_height", "radar_mount_height_m",
            "allow_mixed_radar_types", "solver_time_limit_s",
        )):
            policy_payload = payload.get(POLICY_KEY) if isinstance(
                payload.get(POLICY_KEY), dict
            ) else payload
            state[POLICY_KEY] = normalize_radar_surveillance_policy(policy_payload)
        else:
            state.setdefault(POLICY_KEY, normalize_radar_surveillance_policy(None))

        policy = self._policy()
        provider = facts_provider if facts_provider is not None else self.facts_provider
        route_ids = _route_ids(state, payload)
        if not route_ids:
            raise ValueError("当前没有运行航路：雷达初步划设需要已发布运行航路")

        records = self._stored()
        items_by_id = {
            str(item.get("route_id")): item
            for item in records.get("items") or []
            if isinstance(item, dict)
        }
        evaluated = []
        for route_id in route_ids:
            record = self._evaluate_route(
                route_id=route_id, policy=policy, provider=provider,
            )
            items_by_id[route_id] = record
            evaluated.append(deepcopy(record))
        records["items"] = [
            items_by_id[key] for key in sorted(items_by_id)
        ]
        records["count"] = len(records["items"])
        records["algorithm_id"] = ALGORITHM_ID
        records["algorithm_version"] = ALGORITHM_VERSION
        records["schema_version"] = SCHEMA_VERSION
        records["model_scope"] = MODEL_SCOPE
        records["proposal_only"] = PROPOSAL_ONLY
        records["evaluated_at"] = utc_now()
        state[LAYOUT_KEY] = records
        state.setdefault("result_statuses", {})[STATUS_KEY] = (
            "passed" if all(
                str(item.get("status")) == "proposal_ready" for item in evaluated
            ) else "pending_confirmation"
        )
        self.session.save()
        return self.snapshot()

    def _evaluate_route(self, *, route_id, policy, provider):
        state = self.session.state
        provider = provider if isinstance(provider, dict) else {}
        projector = provider.get("to_metric")
        to_geographic = provider.get("to_geographic") or self.to_geographic
        route = _state_route(state, route_id)
        blockers, unknown_evidence = [], []

        base = {
            "route_id": str(route_id),
            "altitude_layer_id": FIXED_ALTITUDE_LAYER_ID,
            "altitude_m": FIXED_ALTITUDE_M,
            "vertical_reference": VERTICAL_REFERENCE,
            "algorithm_id": ALGORITHM_ID,
            "algorithm_version": ALGORITHM_VERSION,
            "algorithm_name": ALGORITHM_NAME,
            "algorithm_semantics": ALGORITHM_SEMANTICS,
            "model_scope": MODEL_SCOPE,
            "proposal_only": PROPOSAL_ONLY,
            "proposal_title": "80m固定高度航路方向性雷达几何初步划设方案",
            "evaluated_at": utc_now(),
            "parameters": parameters_block(),
            "device_provenance": device_provenance(),
            "device_summary": device_summary(),
            "not_evaluated": deepcopy(NOT_EVALUATED),
            "boundaries": deepcopy(BOUNDARIES),
        }
        base["input_fingerprint"] = self.input_fingerprint(route_id)

        if route is None:
            base.update({
                "status": "not_ready",
                "infeasibility_reasons": ["运行航路不存在"],
                "unknown_evidence": [],
            })
            return base
        base["route_status"] = route.get("status")
        path = route.get("path") or []
        if str(route.get("status")) != "passed":
            blockers.append(
                f"operational route 状态不是 passed（{route.get('status')}）"
            )
        if len(path) < 2:
            blockers.append("operational route 顶点不足（需要 >= 2 个顶点）")

        if projector is None:
            blockers.append(
                "缺少显式米制投影（EPSG:32651）；禁止 degree-as-meter"
            )

        towers = _tower_items(state)
        if not towers:
            blockers.append("项目中没有真实铁塔站址（唯一候选站址来源）")

        origins = resolve_radar_origins(
            towers=list(towers.values()),
            obstacle_profiles=state.get("tower_obstacle_profiles") or {},
            mount_assumption=policy.get("radar_mount_height"),
        )
        base["radar_origin"] = {
            "status": origins["status"],
            "mount_assumption": origins["mount_assumption"],
            "mount_assumption_status": origins["mount_assumption_status"],
            "tower_count": len(origins["records"]),
            "resolved_count": sum(
                1 for item in origins["records"] if item["origin_egm2008_m"] is not None
            ),
            "unresolved_count": origins["unresolved_count"],
            "unresolved": deepcopy(origins["unresolved"][:200]),
            "semantics": origins["semantics"],
            "backend_hardcoded_mount_height": False,
        }
        usable_towers = [
            item for item in origins["records"] if item["origin_egm2008_m"] is not None
        ]
        if not usable_towers:
            blockers.append(
                "没有任何铁塔具备已解析的雷达原点 EGM2008 正高"
                "（缺 FABDEM 地形正高或显式挂高）；缺关键垂向证据不得填 0"
            )
        else:
            # 塔站址 -> 显式米制平面坐标：与航路采样共用同一投影，算法因此只处理米。
            for item in usable_towers:
                longitude, latitude = item.get("longitude"), item.get("latitude")
                if longitude is None or latitude is None:
                    item["metric"] = None
                    item["origin_reason"] = "tower_coordinate_missing"
                    continue
                try:
                    metric = projector([float(longitude), float(latitude)])
                except Exception as exc:
                    item["metric"] = None
                    item["origin_reason"] = f"tower_metric_projection_failed:{exc}"
                    continue
                if not (isinstance(metric, (list, tuple)) and len(metric) >= 2):
                    item["metric"] = None
                    item["origin_reason"] = "tower_metric_projection_invalid"
                    continue
                item["metric"] = [float(metric[0]), float(metric[1])]
            usable_towers = [item for item in usable_towers if item.get("metric") is not None]

        if blockers:
            base.update({
                "status": "not_ready",
                "blockers": blockers,
                "infeasibility_reasons": list(blockers),
                "unknown_evidence": unknown_evidence,
                "candidate_tower_count": len(usable_towers),
                "candidate_panel_count": 0,
                "selected_panels": [],
                "selected_panel_count": None,
                "selected_tower_count": None,
                "radar_i_panel_count": None,
                "radar_ii_panel_count": None,
                "solver": None,
                "validation": None,
                "refinement_rounds": [],
            })
            return base

        # ---- 米制航路 -------------------------------------------------------------
        try:
            metric_path = [projector([float(point[0]), float(point[1])]) for point in path]
        except Exception as exc:
            base.update({
                "status": "not_ready",
                "blockers": [f"米制投影失败：{exc}"],
                "infeasibility_reasons": [f"米制投影失败：{exc}"],
                "unknown_evidence": [],
                "candidate_tower_count": len(usable_towers),
                "candidate_panel_count": 0,
                "selected_panels": [],
                "selected_panel_count": None,
                "selected_tower_count": None,
                "radar_i_panel_count": None,
                "radar_ii_panel_count": None,
                "solver": None,
                "validation": None,
                "refinement_rounds": [],
            })
            return base
        if any(
            not (isinstance(point, (list, tuple)) and len(point) >= 2) for point in metric_path
        ):
            blockers.append("米制投影返回了非法坐标")

        # ---- 逐点地形正高（禁用 0） ------------------------------------------------
        spacing = float(policy["optimization_sample_spacing_m"])
        from ..algorithms.radar_layout.geometry import (
            interpolate_metric_path, sample_offsets_m,
        )

        metric_samples = interpolate_metric_path(metric_path, spacing)
        metric_offsets = sample_offsets_m(metric_path, spacing)
        # 采样点 -> 地理坐标（用于地形采样与陆域判定）。
        projected = []
        for index, (point, offset) in enumerate(zip(metric_samples, metric_offsets)):
            longitude = latitude = None
            if callable(to_geographic):
                try:
                    geographic = to_geographic(point)
                    longitude, latitude = float(geographic[0]), float(geographic[1])
                except Exception:
                    longitude = latitude = None
            projected.append({
                "key": f"O{index:06d}",
                "metric": list(point),
                "offset_m": float(offset),
                "longitude": longitude,
                "latitude": latitude,
            })

        terrain_facts = {}
        terrain_sampler = provider.get("sample_terrain") if isinstance(provider, dict) else None
        if callable(terrain_sampler):
            terrain_facts = terrain_sampler([
                {"key": item["key"], "longitude": item["longitude"], "latitude": item["latitude"]}
                for item in projected
            ]) or {}
        ordered_terrain = [
            terrain_facts.get(item["key"]) or {} for item in projected
        ]

        land_classifier = provider.get("classify_surface") if isinstance(provider, dict) else None
        land_ready = (
            provider.get("land_mask") if isinstance(provider, dict) else None
        ) or {}
        surface_classes = ["unknown"] * len(metric_samples)
        surface_evidence = {"status": "unknown", "reason": "land_mask_not_configured"}
        if callable(land_classifier):
            geographic_points = []
            for point in metric_samples:
                if callable(to_geographic):
                    try:
                        geographic = to_geographic(point)
                        geographic_points.append(
                            (float(geographic[0]), float(geographic[1]))
                        )
                        continue
                    except Exception:
                        pass
                geographic_points.append((None, None))
            surface_classes = list(land_classifier(geographic_points) or [])
            surface_evidence = {
                "status": "passed" if "unknown" not in surface_classes else "partial",
                "reason": None,
                "mask": deepcopy(land_ready.get("readiness") or {}),
                "semantics": LAND_MASK_SEMANTICS,
            }
        elif not land_ready.get("ok"):
            unknown_evidence.append({
                "reason_code": "land_mask_not_configured_fail_closed",
                "detail": (
                    "没有可用的陆域 Polygon 来源：surface_class 全部保持 unknown，"
                    "required_distinct_site_count 未知，绝不自动按 sea（1 站）处理，"
                    "也绝不用 DEM NoData 推断海洋。"
                ),
                "affected_sample_count": len(metric_samples),
            })
            surface_evidence = {
                "status": "missing_data",
                "reason": "land_mask_not_configured",
                "mask": deepcopy(land_ready.get("readiness") or {}),
                "semantics": LAND_MASK_SEMANTICS,
            }

        class _OffsetLookup:
            """``offset_m -> value``：采样点与偏移一一对应，按最近偏移取。"""

            def __init__(self, offsets, values):
                self.offsets = [float(item) for item in offsets]
                self.values = list(values)

            def __call__(self, offset):
                if not self.offsets:
                    return None
                target = float(offset)
                best, best_delta = None, None
                for index, candidate in enumerate(self.offsets):
                    delta = abs(candidate - target)
                    if best_delta is None or delta < best_delta:
                        best, best_delta = index, delta
                if best is None:
                    return None
                return self.values[best] if best < len(self.values) else None

        def _altitude_at(offset):
            fact = _OffsetLookup(
                metric_offsets,
                [
                    (
                        item.get("elevation_m")
                        if str(item.get("status")) == "passed" else None
                    )
                    for item in ordered_terrain
                ],
            )(offset)
            return fact if isinstance(fact, (int, float)) and not isinstance(fact, bool) else None

        sampled = build_route_samples(
            metric_path=metric_path,
            egm2008_by_offset=_altitude_at,
            surface_by_offset=_OffsetLookup(metric_offsets, surface_classes),
            spacing_m=spacing,
            route_id=str(route_id),
            coordinate_resolver=(to_geographic if callable(to_geographic) else None),
        )
        # ---- 5 m 独立连续覆盖复核采样（与 25 m 优化采样共用同一套事实回调） --------
        validation_samples = None
        validation_spacing = float(policy["validation_sample_spacing_m"])
        if validation_spacing > 0 and validation_spacing != spacing:
            validation_samples = build_route_samples(
                metric_path=metric_path,
                egm2008_by_offset=_altitude_at,
                surface_by_offset=_OffsetLookup(metric_offsets, surface_classes),
                spacing_m=validation_spacing,
                route_id=str(route_id),
                sample_id_prefix="V",
                coordinate_resolver=(to_geographic if callable(to_geographic) else None),
            )["samples"]

        terrain_missing = len(sampled["unresolved_samples"])
        if terrain_missing:
            unknown_evidence.append({
                "reason_code": "route_sample_egm2008_altitude_unresolved",
                "detail": "部分航路采样点缺少 FABDEM EGM2008 正高，已从优化输入中排除（绝不填 0）",
                "affected_sample_count": terrain_missing,
            })
        if not sampled["samples"]:
            base.update({
                "status": "not_ready",
                "blockers": blockers + ["没有任何航路采样点具备 EGM2008 正高"],
                "infeasibility_reasons": ["航路采样点全部缺少 EGM2008 正高"],
                "unknown_evidence": unknown_evidence,
                "surface_classification": surface_evidence,
                "candidate_tower_count": len(usable_towers),
                "candidate_panel_count": 0,
                "selected_panels": [],
                "selected_panel_count": None,
                "selected_tower_count": None,
                "radar_i_panel_count": None,
                "radar_ii_panel_count": None,
                "solver": None,
                "validation": None,
                "refinement_rounds": [],
            })
            return base

        land_mask_status = radar_layout_source_status(state, provider.get("paths") or {})
        base["sources"] = {
            "terrain_dtm": land_mask_status["terrain"],
            "land_mask": land_mask_status["land_mask"],
            "radar_mount_height": land_mask_status["radar_mount_height"],
        }
        base["surface_classification"] = surface_evidence
        base["route_sampling"] = {
            "optimization_sample_spacing_m": spacing,
            "validation_sample_spacing_m": policy["validation_sample_spacing_m"],
            "sample_count": len(sampled["samples"]),
            "validation_review_sample_count": (
                len(validation_samples) if validation_samples else len(sampled["samples"])
            ),
            "unresolved_sample_count": terrain_missing,
            "route_metric_length_m": sampled["route_length_m"],
            "metric_crs": METRIC_CRS,
            "mht_grid_used_as_coverage_discretisation": False,
            "semantics": "sampled_along_actual_operational_route_distance",
        }

        options = {}
        if policy.get("solver_time_limit_s"):
            options["time_limit_s"] = policy["solver_time_limit_s"]

        # ---- 5 m 独立连续覆盖复核采样（与 25 m 优化采样共用同一套事实回调） --------
        solved = solve_layout(
            towers=usable_towers, samples=sampled["samples"],
            validation_samples=validation_samples,
            options=options,
            allow_mixed=bool(policy.get("allow_mixed_radar_types", True)),
        )

        solved_validation = deepcopy(solved.get("validation"))
        base.update({
            "status": _map_status(str(solved.get("status"))),
            "stage": solved.get("stage"),
            "stage_label": solved.get("stage_label"),
            "solver": deepcopy(solved.get("solver")),
            "selected_panels": deepcopy(solved.get("selected_panels") or []),
            "selected_panel_count": solved.get("selected_panel_count"),
            "selected_tower_count": solved.get("selected_tower_count"),
            "selected_tower_ids": deepcopy(solved.get("selected_tower_ids") or []),
            "radar_i_panel_count": solved.get("radar_i_panel_count"),
            "radar_ii_panel_count": solved.get("radar_ii_panel_count"),
            "candidate_tower_count": solved.get("candidate_tower_count"),
            "candidate_panel_count": solved.get("candidate_panel_count"),
            "candidate_statistics": deepcopy(solved.get("candidate_statistics") or {}),
            "unusable_towers": deepcopy(solved.get("unusable_towers") or []),
            "presolve": deepcopy(solved.get("presolve")),
            "stage_a": deepcopy(solved.get("stage_a")),
            "stage_b": deepcopy(solved.get("stage_b")),
            "refinement_rounds": deepcopy(solved.get("refinement_rounds") or []),
            "validation": solved_validation,
            "land_validation": (
                deepcopy((solved_validation or {}).get("land")) if solved_validation else None
            ),
            "sea_validation": (
                deepcopy((solved_validation or {}).get("sea")) if solved_validation else None
            ),
            "unknown_validation": (
                deepcopy((solved_validation or {}).get("unknown")) if solved_validation else None
            ),
            "uncovered_segments": deepcopy(
                (solved_validation or {}).get("uncovered_segments") or []
            ),
            "under_redundant_segments": deepcopy(
                (solved_validation or {}).get("under_redundant_segments") or []
            ),
            "unknown_segments": deepcopy(
                (solved_validation or {}).get("unknown_segments") or []
            ),
            "coverage_summary": {
                "validation_sample_spacing_m": policy["validation_sample_spacing_m"],
                "satisfied_sample_count": sum(
                    1 for item in (solved_validation or {}).get("samples") or []
                    if item.get("status") == "satisfied"
                ),
                "under_redundant_sample_count": sum(
                    1 for item in (solved_validation or {}).get("samples") or []
                    if item.get("status") == "under_redundant"
                ),
                "uncovered_sample_count": sum(
                    1 for item in (solved_validation or {}).get("samples") or []
                    if item.get("status") == "uncovered"
                ),
                "unknown_sample_count": sum(
                    1 for item in (solved_validation or {}).get("samples") or []
                    if item.get("status") == "unknown"
                ),
                "validated": bool((solved_validation or {}).get("validated")),
                "validation_fingerprint": stable_fingerprint(
                    {
                        "route_id": str(route_id),
                        "selected_panel_ids": sorted(
                            item["panel_id"] for item in solved.get("selected_panels") or []
                        ),
                        "spacing_m": policy["validation_sample_spacing_m"],
                    },
                    prefix="radarvalidation-",
                ),
            },
            "sample_count": solved.get("sample_count"),
            "message": solved.get("message"),
            "unknown_evidence": unknown_evidence + (
                deepcopy((solved_validation or {}).get("unknown_evidence") or [])
            ),
            "infeasibility_reasons": _infeasibility_reasons(solved),
            "parameters": parameters_block(
                fixed_altitude_m=FIXED_ALTITUDE_M,
                extra={
                    "optimization_sample_spacing_m": spacing,
                    "validation_sample_spacing_m": policy["validation_sample_spacing_m"],
                    "max_refinement_rounds": policy["max_refinement_rounds"],
                },
            ),
        })
        return base

    # ------------------------------------------------------------------ invalidation
    def stale_for_reason(self, reason="radar_surveillance_input_changed"):
        """只 stale 本产物；绝不触碰 routes / CNS / coverage_3d / site plan。"""

        state = self.session.state
        records = self._stored()
        changed = []
        for item in records.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("status") == "stale":
                continue
            item["status"] = "stale"
            item["stale_reason"] = str(reason)
            changed.append(str(item.get("route_id")))
        if changed:
            state[LAYOUT_KEY] = records
            state.setdefault("result_statuses", {})[STATUS_KEY] = "stale"
        return {
            "stale_route_ids": changed,
            "downstream": ["radar_surveillance_layout", "report"],
        }


def _map_status(value):
    mapping = {
        "optimal_coverage": "proposal_ready",
        "infeasible": "infeasible",
        "refinement_incomplete": "refinement_incomplete",
        "search_incomplete": "search_incomplete",
        "solver_error": "solver_error",
        "solver_unavailable": "solver_unavailable",
        "unresolved": "unresolved",
        "not_ready": "not_ready",
    }
    return mapping.get(str(value), "unresolved")


def _infeasibility_reasons(solved):
    reasons = []
    presolve = solved.get("presolve") or {}
    for item in presolve.get("insufficient_samples") or []:
        if item.get("reason") == "required_distinct_site_count_unknown":
            continue
        reasons.append(
            f"{item.get('sample_id')}: 候选独立站址数 "
            f"{item.get('candidate_distinct_site_count')} < 要求 "
            f"{item.get('required_distinct_site_count')}"
            f"（surface_class={item.get('surface_class')}）"
        )
    solver = solved.get("solver") or {}
    if solver.get("status") == "infeasible" and not reasons:
        reasons.append(solver.get("message") or "solver 明确证明不可行")
    if solver.get("status") in ("time_limit", "iteration_limit", "node_limit"):
        reasons.append(
            f"solver 搜索未完成（{solver.get('status')}）：可达性与最优性均未证明，"
            "不等同于不可行"
        )
    if solver.get("status") == "solver_unavailable":
        reasons.append(solver.get("message") or "solver 不可用")
    validation = solved.get("validation") or {}
    for segment in (validation.get("uncovered_segments") or [])[:20]:
        reasons.append(
            f"未覆盖连续段 {segment['route_offset_start_m']:.1f}–"
            f"{segment['route_offset_end_m']:.1f} m"
        )
    for segment in (validation.get("under_redundant_segments") or [])[:20]:
        reasons.append(
            f"冗余不足连续段 {segment['route_offset_start_m']:.1f}–"
            f"{segment['route_offset_end_m']:.1f} m"
        )
    return reasons


def empty_radar_surveillance_layout():
    return {
        "status": "not_calculated",
        "collection_id": LAYOUT_KEY,
        "schema_version": SCHEMA_VERSION,
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "model_scope": MODEL_SCOPE,
        "proposal_only": PROPOSAL_ONLY,
        "count": 0,
        "items": [],
        "boundaries": deepcopy(BOUNDARIES),
    }


def normalize_radar_surveillance_layout(value):
    """持久化 round-trip：绝不重算、绝不修复结论，只保证形状可读。"""

    if not isinstance(value, dict):
        return empty_radar_surveillance_layout()
    empty = empty_radar_surveillance_layout()
    result = deepcopy(value)
    result["collection_id"] = LAYOUT_KEY
    result["schema_version"] = str(result.get("schema_version") or SCHEMA_VERSION)
    result["algorithm_id"] = str(result.get("algorithm_id") or ALGORITHM_ID)
    result["algorithm_version"] = str(result.get("algorithm_version") or ALGORITHM_VERSION)
    result["model_scope"] = str(result.get("model_scope") or MODEL_SCOPE)
    result["proposal_only"] = True
    items = [
        item for item in result.get("items") or [] if isinstance(item, dict)
    ]
    result["items"] = items
    result["count"] = len(items)
    result["status"] = str(result.get("status") or empty["status"])
    result["boundaries"] = deepcopy(BOUNDARIES)
    return result


__all__ = [
    "BOUNDARIES", "FORBIDDEN_WRITE_KEYS", "LAYOUT_KEY", "LAYOUT_STATUSES", "POLICY_KEY",
    "PROPOSAL_ONLY", "READINESS_SEMANTICS", "STATUS_KEY",
    "RadarSurveillanceLayoutService", "default_radar_surveillance_policy",
    "empty_radar_surveillance_layout", "normalize_radar_surveillance_layout",
    "normalize_radar_surveillance_policy",
]