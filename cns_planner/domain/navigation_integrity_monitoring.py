"""Route-endpoint GNSS navigation-integrity monitoring contract and planner.

This is deliberately separate from RTK augmentation and from corridor voxels.  It
records installation and delivery readiness only; without real measurements it
never certifies or passes GNSS integrity, GBAS, RAIM, or ABAS performance.
"""

from __future__ import annotations

from copy import deepcopy

from .cns_service_contract import (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
)
from .cns_service_registry import service_requirement_for
from .route_safety_evidence_v2 import stable_fingerprint


SERVICE_KEY = SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
ENDPOINT_ROLES = ("origin", "destination")
STATUS_SATISFIED = "satisfied"
STATUS_CONFIRMED_DEFICIT = "confirmed_deficit"
STATUS_UNKNOWN = "unknown"
ACTION_TYPE = "add_navigation_integrity_monitor"
#: P16 dispatch 家族身份：endpoint 动作**必须**走专用 what-if（只重算 endpoint 证据），
#: 绝不能被当作普通走廊 radio device 塞进通用 coverage radius planner。
PLANNER_FAMILY = "endpoint_integrity_monitor"
#: 该动作的规划单元身份（**不是**具体厂家/型号，也不伪造任何设备性能）。
PLANNING_UNIT = "navigation_integrity_monitor_engineering_planning_unit"
#: 确定性、非厂家的 planning device identity 前缀。
DEVICE_ID_PREFIX = "NAVINT"

REASON_MISSING = {
    "origin": "origin_integrity_monitor_missing",
    "destination": "destination_integrity_monitor_missing",
}
REASON_DELIVERY_DEFICIT = "integrity_monitor_delivery_deficit"
REASON_EVIDENCE_UNKNOWN = "integrity_monitor_evidence_unknown"


def navigation_integrity_requirement_for(required_cns, route_id=None):
    requirements = (
        ((required_cns or {}).get("route_overrides") or {}).get(str(route_id))
        if route_id not in (None, "") else None
    ) or (required_cns or {}).get("project_default") or {}
    return service_requirement_for(
        "N", requirements.get("navigation") or {}, SERVICE_KEY,
    )


def build_navigation_integrity_endpoint_evidence(
    required_cns, routes, *, existing_facilities=None, communication_delivery=None,
):
    """Build canonical endpoint evidence for routes explicitly requiring the service."""

    route_results = []
    providers = _installed_providers(existing_facilities)
    for route in routes or []:
        route_id = str(route.get("route_id") or "")
        requirement = navigation_integrity_requirement_for(required_cns, route_id)
        if requirement is None:
            continue
        endpoints = {}
        for role in ENDPOINT_ROLES:
            site_id, coordinate = _route_endpoint(route, role)
            matching = [
                deepcopy(item) for item in providers
                if site_id and str(item.get("takeoff_landing_site_id") or "") == site_id
            ]
            delivery = _delivery_fact(communication_delivery, route_id, role)
            endpoints[role] = _endpoint_record(
                role, site_id, coordinate, matching, delivery,
            )

        # A physical monitor/site is never allowed to discharge both endpoint duties.
        origin_ids = set(endpoints["origin"].get("distinct_site_ids") or [])
        destination_ids = set(endpoints["destination"].get("distinct_site_ids") or [])
        shared = sorted(origin_ids & destination_ids)
        if shared:
            for role in ENDPOINT_ROLES:
                endpoint = endpoints[role]
                endpoint["planning_status"] = STATUS_CONFIRMED_DEFICIT
                endpoint["status"] = STATUS_CONFIRMED_DEFICIT
                endpoint["reasons"] = sorted(set([
                    *(endpoint.get("reasons") or []), REASON_MISSING[role],
                ]))
                endpoint["shared_distinct_site_ids_rejected"] = shared

        statuses = [endpoints[role]["status"] for role in ENDPOINT_ROLES]
        route_status = _aggregate(statuses)
        route_results.append({
            "route_id": route_id,
            "service_key": SERVICE_KEY,
            "service_scope": "route_endpoints",
            "endpoints": endpoints,
            "status": route_status,
            "integrity_measurement_status": "not_measured_not_certified",
            "continuous_gap_m": None,
        })

    if not route_results:
        return None
    result = {
        "service_key": SERVICE_KEY,
        "subsystem": "N",
        "service_subtype": "navigation_integrity_monitoring",
        "technology": "gnss_integrity_monitoring",
        "target": "gnss_navigation_performance",
        "service_scope": "route_endpoints",
        "planner_family": "endpoint_integrity_monitor",
        "provider_model": "ground_navigation_integrity_monitor",
        "routes": route_results,
        "status": _aggregate([item["status"] for item in route_results]),
        "policy": deepcopy((navigation_integrity_requirement_for(required_cns) or {}).get("planning") or {}),
        "not_claimed": [
            "gbas_certification", "rtk_correction_station", "airborne_raim_or_abas",
            "device_model_or_vendor_performance", "gnss_integrity_certified_or_passed",
        ],
    }
    result["input_fingerprint"] = stable_fingerprint(result, prefix="nav-integrity-")
    return result


def endpoint_gap_evidence(endpoint_evidence):
    """P15 view: endpoint reasons only, with no corridor-length deficit."""

    routes = []
    for route in (endpoint_evidence or {}).get("routes") or []:
        gaps, unknown, states = [], [], []
        for role in ENDPOINT_ROLES:
            endpoint = deepcopy((route.get("endpoints") or {}).get(role) or {})
            states.append(_endpoint_state(role, endpoint))
            if endpoint.get("status") == STATUS_CONFIRMED_DEFICIT:
                gaps.append({
                    "endpoint_role": role,
                    "takeoff_landing_site_id": endpoint.get("takeoff_landing_site_id"),
                    "coordinate": endpoint.get("coordinate"),
                    "reasons": endpoint.get("reasons") or [REASON_MISSING[role]],
                    "status": STATUS_CONFIRMED_DEFICIT,
                })
            elif endpoint.get("status") == STATUS_UNKNOWN:
                unknown.append({
                    "endpoint_role": role,
                    "takeoff_landing_site_id": endpoint.get("takeoff_landing_site_id"),
                    "coordinate": endpoint.get("coordinate"),
                    "reasons": endpoint.get("reasons") or [REASON_EVIDENCE_UNKNOWN],
                    "status": STATUS_UNKNOWN,
                })
        routes.append({
            "route_id": route.get("route_id"), "service_key": SERVICE_KEY,
            "service_scope": "route_endpoints", "confirmed_endpoint_gaps": gaps,
            "unknown_endpoint_evidence": unknown, "continuous_gap_m": None,
            #: additive：本 route 每个 endpoint 的**完整**状态（含 satisfied）。
            #: P16 的 endpoint what-if 需要"装上 monitor 后该 endpoint 变成什么"，
            #: 而缺口列表只在未满足时出现，因此必须另有完整状态视图，绝不靠"缺席"猜测。
            "endpoint_states": states,
            "status": _aggregate([
                *(STATUS_CONFIRMED_DEFICIT for _ in gaps),
                *(STATUS_UNKNOWN for _ in unknown),
            ]) if gaps or unknown else STATUS_SATISFIED,
        })
    return {"service_key": SERVICE_KEY, "service_scope": "route_endpoints", "routes": routes,
            "status": _aggregate([item["status"] for item in routes])}


def _endpoint_state(role, endpoint):
    """endpoint → P16 可索引的稳定状态视图（只做改名与状态归一，不引入新口径）。"""

    status = str(endpoint.get("status") or STATUS_UNKNOWN)
    return {
        "endpoint_role": role,
        "takeoff_landing_site_id": endpoint.get("takeoff_landing_site_id"),
        "coordinate": deepcopy(endpoint.get("coordinate")),
        "planning_status": endpoint.get("planning_status"),
        "status": status,
        "reasons": deepcopy(endpoint.get("reasons") or []),
        "delivery_status": endpoint.get("delivery_status"),
        "distinct_site_ids": deepcopy(endpoint.get("distinct_site_ids") or []),
        "integrity_measurement_status": endpoint.get("integrity_measurement_status"),
    }


def navigation_integrity_monitor_actions(endpoint_gap):
    """P16 actions, bound only to the missing endpoint's real takeoff/landing site.

    每个动作都带 **显式 eligibility**：只有 endpoint 缺口已被确认为
    ``monitor_missing``、且起降点站址身份与坐标证据完整时才生成 ``eligible``；
    任何一项证据不足一律**不生成** eligible 动作（保持 unknown / evidence_required），
    绝不因为"缺 eligibility 字段"而在下游被静默当作 ineligible。
    """

    actions = []
    for route in (endpoint_gap or {}).get("routes") or []:
        for gap in route.get("confirmed_endpoint_gaps") or []:
            role = str(gap.get("endpoint_role") or "")
            reasons = set(gap.get("reasons") or [])
            if REASON_MISSING.get(role) not in reasons:
                continue  # delivery-only deficits belong to Communication planning.
            site_id = str(gap.get("takeoff_landing_site_id") or "")
            coordinate = gap.get("coordinate")
            if not site_id or not _coordinate_usable(coordinate):
                continue
            route_id = str(route.get("route_id") or "")
            actions.append({
                "action_id": f"{ACTION_TYPE}:{route_id}:{role}:{site_id}",
                "action": ACTION_TYPE,
                "planner_family": PLANNER_FAMILY,
                "service_key": SERVICE_KEY,
                "subsystem": "N",
                "route_id": route.get("route_id"),
                "endpoint_role": role,
                "site_id": site_id,
                "takeoff_landing_site_id": site_id,
                "distinct_site_id": f"takeoff_landing_site:{site_id}",
                "coordinate": deepcopy(coordinate),
                "site_binding": "takeoff_landing_site",
                "reuse_class": "existing_shared_site",
                "free_site_optimization": False,
                #: 确定性、非厂家的 planning device identity：同一
                #: ``(route, endpoint_role, site)`` 永远得到同一个 id，既不是厂家型号，
                #: 也不携带任何设备性能声明。
                "device_id": f"{DEVICE_ID_PREFIX}-{route_id}-{role}-{site_id}",
                "device_service_key": SERVICE_KEY,
                "planning_unit": PLANNING_UNIT,
                "equipment_selection_status": "not_selected",
                "proposal_only": True,
                "eligibility": {"status": "eligible", "reasons": []},
                "eligibility_basis": {
                    "endpoint_gap_status": STATUS_CONFIRMED_DEFICIT,
                    "endpoint_reason": REASON_MISSING[role],
                    "takeoff_landing_site_id": site_id,
                    "coordinate_available": True,
                    "site_binding": "takeoff_landing_site",
                    "semantics": "endpoint_monitor_missing_with_complete_site_identity",
                },
            })
    return sorted(actions, key=lambda item: item["action_id"])


def hypothetical_endpoint_monitor_evidence(endpoint_evidence, actions):
    """把 endpoint monitor 动作作为 **caller-owned 假想 provider** 应用。

    这是 endpoint 动作的**专用 what-if**：它只重算 endpoint 证据
    （``planning_status`` / ``status`` / 缺口列表），**不**重跑 P14/P15 走廊链 ——
    安装一台 endpoint 完整性监测站不改变任何走廊几何、C/RID 覆盖或 Radar 布局。

    语义铁律（与正式 endpoint 证据同一口径）：

    * 装上 monitor 只解决 ``*_integrity_monitor_missing``；
    * Communication delivery 依赖保持**当前基线事实**：若 delivery 仍为
      ``confirmed_deficit``，该 endpoint 最终仍是
      ``confirmed_deficit(integrity_monitor_delivery_deficit)``；
    * 同一物理站址**绝不**同时承担 origin 与 destination 两项职责。
    """

    if not endpoint_evidence:
        return endpoint_evidence
    by_route_role = {}
    for action in actions or []:
        if str(action.get("planner_family") or "") != PLANNER_FAMILY:
            continue
        route_id = str(action.get("route_id") or "")
        role = str(action.get("endpoint_role") or "")
        if not route_id or role not in ENDPOINT_ROLES:
            continue
        by_route_role.setdefault((route_id, role), []).append(action)
    if not by_route_role:
        return deepcopy(endpoint_evidence)

    result = deepcopy(endpoint_evidence)
    routes = []
    for route in result.get("routes") or []:
        route_id = str(route.get("route_id") or "")
        endpoints = {}
        for role in ENDPOINT_ROLES:
            endpoint = deepcopy((route.get("endpoints") or {}).get(role) or {})
            for action in by_route_role.get((route_id, role)) or []:
                provider = _hypothetical_monitor_provider(role, action)
                if provider is None:
                    continue
                installed = endpoint.setdefault("selected_installed_providers", [])
                if any(str(item.get("provider_id")) == str(provider["provider_id"])
                       for item in installed):
                    continue
                installed.append(provider)
            endpoints[role] = _recompute_endpoint(endpoint, role)

        # A physical monitor/site is never allowed to discharge both endpoint duties.
        shared = sorted(
            set(endpoints["origin"].get("distinct_site_ids") or [])
            & set(endpoints["destination"].get("distinct_site_ids") or [])
        )
        if shared:
            for role in ENDPOINT_ROLES:
                endpoint = endpoints[role]
                endpoint["planning_status"] = STATUS_CONFIRMED_DEFICIT
                endpoint["status"] = STATUS_CONFIRMED_DEFICIT
                endpoint["reasons"] = sorted(set([
                    *(endpoint.get("reasons") or []), REASON_MISSING[role],
                ]))
                endpoint["shared_distinct_site_ids_rejected"] = shared

        routes.append({
            **deepcopy(route),
            "endpoints": endpoints,
            "status": _aggregate([endpoints[role]["status"] for role in ENDPOINT_ROLES]),
        })
    result["routes"] = routes
    result["status"] = _aggregate([item["status"] for item in routes])
    result["input_fingerprint"] = stable_fingerprint(result, prefix="nav-integrity-")
    return result


def _hypothetical_monitor_provider(role, action):
    """一条 endpoint monitor 动作 → 假想 provider 记录（站址身份完整保留）。"""

    site_id = str(action.get("takeoff_landing_site_id") or action.get("site_id") or "")
    coordinate = action.get("coordinate")
    if not site_id or not _coordinate_usable(coordinate):
        return None
    device_id = str(action.get("device_id") or f"{DEVICE_ID_PREFIX}-{role}-{site_id}")
    return {
        "provider_id": device_id,
        "facility_id": f"p16-proposal:{site_id}",
        "takeoff_landing_site_id": site_id,
        "distinct_site_id": str(
            action.get("distinct_site_id") or f"takeoff_landing_site:{site_id}"
        ),
        "coordinate": deepcopy(coordinate),
        "planning_status": "installed",
        "integrity_measurement_status": "not_measured_not_certified",
        "hypothetical_action_id": action.get("action_id"),
        "proposal_only": True,
        "equipment_selection_status": "not_selected",
        "planning_unit": action.get("planning_unit") or PLANNING_UNIT,
    }


def _recompute_endpoint(endpoint, role):
    """按"当前 provider 集合 + 基线 delivery 事实"重算 endpoint 状态（纯函数）。"""

    providers = list(endpoint.get("selected_installed_providers") or [])
    site_id = endpoint.get("takeoff_landing_site_id")
    coordinate = endpoint.get("coordinate")
    reasons = []
    if not site_id or not _coordinate_usable(coordinate):
        planning = status = STATUS_UNKNOWN
        reasons.append(REASON_EVIDENCE_UNKNOWN)
    elif not providers:
        planning = status = STATUS_CONFIRMED_DEFICIT
        reasons.append(REASON_MISSING[role])
    else:
        planning = STATUS_SATISFIED
        delivery_status = str(endpoint.get("delivery_status") or STATUS_UNKNOWN)
        if delivery_status == STATUS_CONFIRMED_DEFICIT:
            #: 装上 monitor 只解决 monitor_missing：Communication delivery 若是已确认
            #: 缺口，endpoint 最终仍是 confirmed_deficit，绝不升级为 satisfied。
            status = STATUS_CONFIRMED_DEFICIT
            reasons.append(REASON_DELIVERY_DEFICIT)
        elif delivery_status != STATUS_SATISFIED:
            status = STATUS_UNKNOWN
            reasons.append(REASON_EVIDENCE_UNKNOWN)
        else:
            status = STATUS_SATISFIED
    endpoint["endpoint_role"] = role
    endpoint["planning_status"] = planning
    endpoint["status"] = status
    endpoint["reasons"] = reasons
    endpoint["distinct_site_ids"] = sorted({
        str(item.get("distinct_site_id")) for item in providers
        if item.get("distinct_site_id")
    })
    endpoint.pop("shared_distinct_site_ids_rejected", None)
    return endpoint


def _installed_providers(collection):
    providers = []
    for facility in (collection or {}).get("items") or []:
        site_id = facility.get("takeoff_landing_site_id") or facility.get("site_id")
        distinct = facility.get("distinct_site_id") or (f"takeoff_landing_site:{site_id}" if site_id else None)
        for device in facility.get("devices") or []:
            if str(device.get("service_key") or "") != SERVICE_KEY:
                continue
            if str(device.get("status") or "active") not in ("active", "installed", "passed"):
                continue
            providers.append({
                "provider_id": device.get("device_id") or facility.get("facility_id"),
                "facility_id": facility.get("facility_id"),
                "takeoff_landing_site_id": None if site_id is None else str(site_id),
                "distinct_site_id": None if distinct is None else str(distinct),
                "coordinate": deepcopy(facility.get("coordinate")),
                "planning_status": "installed",
                "integrity_measurement_status": "not_measured_not_certified",
            })
    return providers


def _route_endpoint(route, role):
    is_origin = role == "origin"
    site_id = route.get("start_node_id" if is_origin else "end_node_id")
    path = route.get("path") or []
    coordinate = route.get("start" if is_origin else "end")
    if not _coordinate_usable(coordinate) and path:
        coordinate = path[0] if is_origin else path[-1]
    return (None if site_id in (None, "") else str(site_id), deepcopy(coordinate))


def _endpoint_record(role, site_id, coordinate, providers, delivery):
    reasons = []
    if not site_id or not _coordinate_usable(coordinate):
        planning = status = STATUS_UNKNOWN
        reasons.append(REASON_EVIDENCE_UNKNOWN)
    elif not providers:
        planning = status = STATUS_CONFIRMED_DEFICIT
        reasons.append(REASON_MISSING[role])
    else:
        planning = STATUS_SATISFIED
        delivery_status = str((delivery or {}).get("status") or STATUS_UNKNOWN)
        if delivery_status == STATUS_CONFIRMED_DEFICIT:
            status = STATUS_CONFIRMED_DEFICIT
            reasons.append(REASON_DELIVERY_DEFICIT)
        elif delivery_status != STATUS_SATISFIED:
            status = STATUS_UNKNOWN
            reasons.append(REASON_EVIDENCE_UNKNOWN)
        else:
            status = STATUS_SATISFIED
    return {
        "endpoint_role": role, "coordinate": deepcopy(coordinate),
        "takeoff_landing_site_id": site_id, "required_count": 1,
        "selected_installed_providers": providers,
        "distinct_site_ids": sorted({str(item.get("distinct_site_id")) for item in providers
                                     if item.get("distinct_site_id")}),
        "planning_status": planning,
        "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        "delivery_status": str((delivery or {}).get("status") or STATUS_UNKNOWN),
        "status": status, "reasons": reasons,
        "integrity_measurement_status": "not_measured_not_certified",
    }


def _delivery_fact(delivery, route_id, role):
    if callable(delivery):
        value = delivery(route_id, role)
    else:
        route = (delivery or {}).get(str(route_id)) if isinstance(delivery, dict) else None
        value = (route or {}).get(role) if isinstance(route, dict) else None
    if isinstance(value, str):
        value = {"status": value}
    return value if isinstance(value, dict) else None


def _aggregate(statuses):
    statuses = list(statuses or [])
    if STATUS_CONFIRMED_DEFICIT in statuses:
        return STATUS_CONFIRMED_DEFICIT
    if STATUS_UNKNOWN in statuses or not statuses:
        return STATUS_UNKNOWN
    return STATUS_SATISFIED


def _coordinate_usable(value):
    return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
        item is not None for item in value[:2]
    )


__all__ = [
    "ACTION_TYPE", "DEVICE_ID_PREFIX", "ENDPOINT_ROLES", "PLANNER_FAMILY", "PLANNING_UNIT",
    "REASON_DELIVERY_DEFICIT", "REASON_EVIDENCE_UNKNOWN", "REASON_MISSING", "SERVICE_KEY",
    "build_navigation_integrity_endpoint_evidence", "endpoint_gap_evidence",
    "hypothetical_endpoint_monitor_evidence", "navigation_integrity_monitor_actions",
    "navigation_integrity_requirement_for",
]
