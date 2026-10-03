"""Canonical CNS service dispatch registry and RequiredCNS service helpers.

The registry describes dispatch semantics only.  Communication/RID surface
policy remains authoritative in :mod:`cns_service_contract`; radar geometry
remains authoritative in ``radar_surveillance_layout``.  In particular this
module intentionally contains no planning radius, panel geometry, or baseline
distance.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite

from .cns_performance import normalize_subsystem_contract, sync_aliases
from .cns_service_contract import (
    LEGACY_SERVICE_KEYS,
    RADAR_TYPE_SEMANTICS,
    RID_TYPE_SEMANTICS,
    RTK_AUGMENTATION_TYPE_SEMANTICS,
    NAVIGATION_INTEGRITY_TYPE_SEMANTICS,
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_SURVEILLANCE,
    normalize_redundancy_by_surface,
    service_policy,
    validated_service_key,
    validated_service_subtype,
)


SERVICE_REGISTRY = {
    SERVICE_KEY_COMMUNICATION: {
        "service_key": SERVICE_KEY_COMMUNICATION,
        "subsystem": "C",
        "label": "通信（Communication）",
        "planner_family": "omnidirectional_site",
        "provider_model": "ground_site",
        "requirement_semantics": "independent_service_requirement",
        "planning_maturity": "engineering_planning_baseline",
        "dependencies": [],
        "surface_dependent": True,
        "supports_site_planning": True,
        "service_scope": "corridor",
    },
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION: {
        "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
        "subsystem": "N",
        "label": "导航 RTK 增强（Navigation RTK Augmentation）",
        "planner_family": "navigation_reference_station",
        "provider_model": "reference_station_network",
        "requirement_semantics": "explicit_ground_augmentation_requirement",
        "planning_maturity": "engineering_planning_baseline",
        "dependencies": [SERVICE_KEY_COMMUNICATION],
        "surface_dependent": False,
        "supports_site_planning": True,
        "service_scope": "corridor",
        "type": deepcopy(RTK_AUGMENTATION_TYPE_SEMANTICS),
    },
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING: {
        "service_key": SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
        "subsystem": "N",
        "label": "导航完整性监测",
        "planner_family": "endpoint_integrity_monitor",
        "provider_model": "ground_navigation_integrity_monitor",
        "requirement_semantics": "route_endpoint_navigation_integrity_monitoring",
        "planning_maturity": "engineering_planning_baseline",
        "dependencies": [SERVICE_KEY_COMMUNICATION],
        "surface_dependent": False,
        "supports_site_planning": True,
        "service_scope": "route_endpoints",
        "type": deepcopy(NAVIGATION_INTEGRITY_TYPE_SEMANTICS),
        "exclusions": [
            "not_gbas_certification", "not_rtk_correction_station",
            "not_airborne_raim_or_abas", "no_device_model_or_vendor_performance_claim",
        ],
    },
    SERVICE_KEY_RID_COOPERATIVE: {
        "service_key": SERVICE_KEY_RID_COOPERATIVE,
        "subsystem": "S",
        "label": "合作监视 / 网络远程识别（RID）",
        "planner_family": "omnidirectional_site",
        "provider_model": "cooperative_receiver",
        "requirement_semantics": "independent_cooperative_surveillance_requirement",
        "planning_maturity": "engineering_planning_baseline",
        "dependencies": [],
        "surface_dependent": True,
        "supports_site_planning": True,
        "service_scope": "corridor",
        "type": deepcopy(RID_TYPE_SEMANTICS),
    },
    SERVICE_KEY_RADAR_NONCOOPERATIVE: {
        "service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE,
        "subsystem": "S",
        "label": "雷达非合作监视（Radar Non-cooperative Surveillance）",
        "planner_family": "directional_radar",
        "provider_model": "directional_sensor",
        "requirement_semantics": "independent_noncooperative_surveillance_requirement",
        "planning_maturity": "existing_canonical_radar_policy",
        "dependencies": [],
        "surface_dependent": False,
        "supports_site_planning": True,
        "service_scope": "corridor",
        "type": deepcopy(RADAR_TYPE_SEMANTICS),
        "planning_policy": {
            "required": True,
            "allowed_radar_types": ["radar_i"],
            "orientation_optimization": True,
            "orientation_policy": "bearing_derived_critical_angles",
            "existing_tower_first": True,
            "max_panels_per_tower": 4,
            "allow_automatic_radar_ii_escalation": False,
            "allow_range_relaxation": False,
            "gap_after_proven_infeasibility": True,
            "validation_sample_spacing_m": 5.0,
        },
    },
    # Legacy identities remain addressable, but do not acquire a new planner.
    SERVICE_KEY_NAVIGATION: {
        "service_key": SERVICE_KEY_NAVIGATION, "subsystem": "N",
        "label": "导航（legacy）", "planner_family": "legacy_subsystem",
        "provider_model": "legacy_navigation_capability",
        "requirement_semantics": "legacy_single_service_requirement",
        "planning_maturity": "legacy_compatible", "dependencies": [],
        "surface_dependent": False, "supports_site_planning": False,
        "service_scope": "legacy",
    },
    SERVICE_KEY_SURVEILLANCE: {
        "service_key": SERVICE_KEY_SURVEILLANCE, "subsystem": "S",
        "label": "监视（legacy）", "planner_family": "legacy_subsystem",
        "provider_model": "legacy_surveillance_provider",
        "requirement_semantics": "legacy_single_service_requirement",
        "planning_maturity": "legacy_compatible", "dependencies": [],
        "surface_dependent": False, "supports_site_planning": False,
        "service_scope": "legacy",
    },
}


def service_registry_entry(service_key):
    """Return a defensive copy of one registered service; unknown keys fail closed."""

    key = validated_service_key(service_key, field="service_key")
    return deepcopy(SERVICE_REGISTRY[key])


def planner_family_for(service_key):
    return service_registry_entry(service_key)["planner_family"]


def service_scope_for(service_key):
    """Explicit P14/P15/P16 dispatch scope for a canonical service."""

    return service_registry_entry(service_key)["service_scope"]


def normalize_service_requirements(subsystem, requirement, *, field="required_cns"):
    """Normalize the optional ``services`` map on one subsystem requirement.

    Absence returns ``None`` so callers can preserve the byte/shape semantics of
    legacy projects.  Presence is explicit opt-in and each item is normalized in
    its own service bucket; provider counts or redundancy are never combined.
    """

    code = str(subsystem or "").strip().upper()
    if code not in LEGACY_SERVICE_KEYS:
        raise ValueError(f"{field} subsystem 必须是 C/N/S")
    if not isinstance(requirement, dict):
        raise ValueError(f"{field} 必须是对象")
    if "services" not in requirement:
        return None
    services = requirement.get("services")
    if not isinstance(services, dict):
        raise ValueError(f"{field}.services 必须是对象")
    result = {}
    for raw_key, raw_value in services.items():
        key = validated_service_key(raw_key, subsystem=code, field=f"{field}.services key")
        if not isinstance(raw_value, dict):
            raise ValueError(f"{field}.services[{key}] 必须是对象")
        result[key] = _normalize_service_requirement(
            code, key, raw_value, field=f"{field}.services[{key}]",
        )
    return result


def required_services_for(subsystem, requirement):
    """Return required service views without upgrading a legacy requirement.

    Explicit ``services`` returns its independently normalized required entries.
    A legacy requirement returns at most one view: its explicit ``service_key``
    or the legacy subsystem identity.  Thus legacy S never means RID + Radar and
    aircraft ``gnss_rtk`` never means a ground RTK augmentation requirement.
    """

    code = str(subsystem or "").strip().upper()
    item = requirement if isinstance(requirement, dict) else {}
    services = normalize_service_requirements(code, item)
    if services is not None:
        return [deepcopy(value) for value in services.values() if value.get("required") is True]
    if item.get("required") is not True:
        return []
    key = validated_service_key(
        item.get("service_key"), subsystem=code, field="required_cns.service_key",
    ) or LEGACY_SERVICE_KEYS[code]
    view = deepcopy(item)
    view["service_key"] = key
    view["legacy_compatible_view"] = True
    return [view]


def service_requirement_for(subsystem, requirement, service_key):
    """Return one required service view, or ``None`` if that service is not required."""

    key = validated_service_key(service_key, subsystem=subsystem, field="service_key")
    return next(
        (item for item in required_services_for(subsystem, requirement)
         if item.get("service_key") == key),
        None,
    )


#: 只有拥有**冻结 surface 工程规划基线**的服务才允许从"已确认的 subsystem
#: ``service_key``"确定性派生 canonical ``services`` 条目。
#:
#: Radar（非合作监视）与 Navigation RTK augmentation 各有独立的 canonical adapter 与
#: 证据来源，**不**在此列；legacy ``S:surveillance`` / ``N:navigation`` 更不允许被
#: 升级成正式服务要求。
CANONICAL_SERVICE_DERIVATION_KEYS = (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_RID_COOPERATIVE,
)

#: 派生来源标注：这些数值来自 Round 2 冻结的**工程规划基线**，
#: 既不是厂家实测规格，也不是从设备数据反推出来的结论。
DERIVED_SERVICE_REQUIREMENT_SOURCE = "derived_from_confirmed_subsystem_service_key"


def derive_canonical_service_requirement(subsystem, requirement, *, field="required_cns"):
    """从**已确认**的 subsystem 要求确定性派生一个 canonical service requirement。

    fail-closed 规则：只有以下条件**全部**成立才派生，否则返回 ``None``，调用方必须
    保持 legacy 语义（绝不猜测、绝不把未确认要求升级成正式服务要求）：

    * ``required is True``；
    * ``confirmed is True``；
    * 显式 ``service_key`` 属于 :data:`CANONICAL_SERVICE_DERIVATION_KEYS`；
    * 该 ``service_key`` 在 ``SERVICE_SURFACE_POLICY`` 里有冻结的 surface policy。

    派生内容**全部**取自既有 canonical 常量（surface policy 的全向半球几何、
    ``radius_by_surface``、``redundancy_by_surface``、RID 类型语义），不引入任何新
    数值，也不读取设备数据。返回 ``{service_key: requirement}``。
    """

    code = str(subsystem or "").strip().upper()
    item = requirement if isinstance(requirement, dict) else {}
    if item.get("required") is not True or item.get("confirmed") is not True:
        return None
    key = validated_service_key(
        item.get("service_key"), subsystem=code, field=f"{field}.service_key",
    )
    if key not in CANONICAL_SERVICE_DERIVATION_KEYS:
        return None
    policy = service_policy(key)
    if policy is None:
        return None
    derived = {
        "service_key": key,
        "required": True,
        "confirmed": True,
        "status": "passed",
        "source": str(item.get("source") or DERIVED_SERVICE_REQUIREMENT_SOURCE),
        "service_requirement_source": DERIVED_SERVICE_REQUIREMENT_SOURCE,
        "geometry": deepcopy(policy.get("geometry") or {}),
        "radius_by_surface": deepcopy(policy.get("radius_by_surface") or {}),
        "redundancy_by_surface": deepcopy(policy.get("redundancy_by_surface") or {}),
        "radius_basis": policy.get("radius_basis"),
        "maturity": policy.get("maturity"),
        "parameter_origin": policy.get("parameter_origin"),
        "parameter_semantics": policy.get("parameter_semantics"),
        "missing_device_evidence": list(policy.get("missing_device_evidence") or []),
        "not_evaluated": list(policy.get("not_evaluated") or []),
    }
    if item.get("coverage_requirement") is not None:
        derived["coverage_requirement"] = item.get("coverage_requirement")
    if policy.get("type"):
        derived["type"] = deepcopy(policy["type"])
    return {key: derived}


def canonicalize_requirement_services(subsystem, requirement, *, field="required_cns"):
    """解析一个 subsystem 要求上的正式 ``services``（显式优先，其次确定性派生）。

    返回 ``(services, provenance)``：

    * 显式 ``services`` 存在 ⇒ 原样返回该映射，``provenance.derived = False``；
      形状/合法性的 canonical 校验仍由 ``normalize_required_cns`` 完成。
    * 否则尝试 :func:`derive_canonical_service_requirement`；成功则返回派生映射与
      可审计的 ``provenance``。
    * 两者都不成立 ⇒ ``(None, None)``：调用方必须保持 legacy 形状，**不得**自行
      构造任何 service 要求。

    本函数是"Adopt 后 authoritative ``required_cns`` 必须携带正式 service
    requirements"这一裁定的唯一派生入口；它不修改任何既有数值。
    """

    item = requirement if isinstance(requirement, dict) else {}
    explicit = normalize_service_requirements(subsystem, item, field=field)
    if explicit is not None:
        #: 提前跑一遍 canonical 校验（fail-closed），但**原样**返回调用方声明的映射，
        #: 让 ``normalize_required_cns`` 成为唯一规范化写入点（避免双重规范化差异）。
        return deepcopy(item.get("services")), {
            "source": "explicit_required_cns_services",
            "derived": False,
            "reason": "requirement 已显式声明 services，原样采用",
        }
    derived = derive_canonical_service_requirement(subsystem, item, field=field)
    if derived is None:
        return None, None
    return derived, {
        "source": DERIVED_SERVICE_REQUIREMENT_SOURCE,
        "derived": True,
        "basis": "confirmed_subsystem_service_key_plus_frozen_surface_policy",
        "reason": (
            "该 subsystem 已确认显式 service_key 且存在冻结 surface 工程规划基线，"
            "按既有 canonical 常量确定性派生正式 service requirement"
        ),
    }


def _normalize_service_requirement(subsystem, key, value, *, field):
    explicit = validated_service_key(
        value.get("service_key"), subsystem=subsystem, field=f"{field}.service_key",
    )
    if explicit is not None and explicit != key:
        raise ValueError(f"{field}.service_key 必须与 services key 一致：{key}")
    required = value.get("required")
    if required not in (True, False, None):
        raise ValueError(f"{field}.required 必须为 true、false 或 null")

    source = deepcopy(value)
    registry_type = SERVICE_REGISTRY[key].get("type") or {}
    supplied_type = source.get("type") or {}
    if not isinstance(supplied_type, dict):
        raise ValueError(f"{field}.type 必须是对象")
    for name, expected in registry_type.items():
        supplied = supplied_type.get(name)
        if supplied not in (None, "") and str(supplied).strip().lower() != expected:
            raise ValueError(f"{field}.type.{name} 与 service_key {key} 不一致")
    if registry_type:
        source["type"] = {**supplied_type, **registry_type}

    normalized = {**source, **normalize_subsystem_contract(subsystem, source, field=field)}
    normalized = sync_aliases(subsystem, normalized, field=field)
    if registry_type:
        normalized["type"] = {**(normalized.get("type") or {}), **registry_type}
    normalized["required"] = required
    normalized["service_key"] = key
    subtype = validated_service_subtype(
        source.get("service_subtype")
        or normalized.get("type", {}).get("service_subtype")
        or registry_type.get("service_subtype")
    )
    if subtype:
        normalized["service_subtype"] = subtype
    if "redundancy_by_surface" in source:
        normalized["redundancy_by_surface"] = normalize_redundancy_by_surface(
            source.get("redundancy_by_surface"), field=f"{field}.redundancy_by_surface",
        )
    if key == SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION:
        normalized["planning"] = _normalize_rtk_planning(
            source.get("planning"), field=f"{field}.planning",
            fallback_confirmed=normalized.get("confirmed", False),
            fallback_source=normalized.get("source"),
        )
    if key == SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING:
        normalized["planning"] = _normalize_navigation_integrity_planning(
            source.get("planning"), field=f"{field}.planning",
            fallback_confirmed=normalized.get("confirmed", False),
            fallback_source=normalized.get("source"),
        )
    normalized["status"] = (
        "passed" if required is False or (required is True and normalized.get("confirmed") is True)
        else "pending_confirmation"
    )
    return normalized


def _normalize_rtk_planning(value, *, field, fallback_confirmed=False, fallback_source=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    model = str(value.get("model") or "reference_station_baseline").strip()
    if model != "reference_station_baseline":
        raise ValueError(f"{field}.model 必须是 reference_station_baseline")
    baseline = _optional_positive_number(value.get("max_reference_baseline_m"), f"{field}.max_reference_baseline_m")
    site_count = _optional_positive_integer(value.get("required_distinct_site_count"), f"{field}.required_distinct_site_count")
    delivery_raw = value.get("delivery_service_key", SERVICE_KEY_COMMUNICATION)
    delivery = validated_service_key(delivery_raw, field=f"{field}.delivery_service_key")
    if delivery not in (None, SERVICE_KEY_COMMUNICATION):
        raise ValueError(f"{field}.delivery_service_key 只能是 C:communication 或 null")
    confirmed = value.get("confirmed", fallback_confirmed)
    if confirmed not in (True, False):
        raise ValueError(f"{field}.confirmed 必须为 boolean")
    maturity = str(value.get("maturity") or "engineering_assumption").strip()
    if maturity not in ("engineering_assumption", "confirmed_source"):
        raise ValueError(f"{field}.maturity 无效")
    source = value.get("source", fallback_source)
    source = None if source in (None, "") else str(source)
    readiness = (
        "ready" if confirmed and baseline is not None and site_count is not None
        else "pending_confirmation"
    )
    return {
        "model": model,
        "max_reference_baseline_m": baseline,
        "required_distinct_site_count": site_count,
        "delivery_service_key": delivery,
        "confirmed": confirmed,
        "source": source,
        "maturity": maturity,
        "planning_readiness": readiness,
        "missing_evidence": readiness != "ready",
    }


NAVIGATION_INTEGRITY_POLICY_SOURCE = (
    "Round29-E 用户确认：导航完整性监测设备优先布设在航路起点和终点起降场，"
    "两端各至少1套独立设备；当前不沿航路中段强制布站；"
    "10 km 为 CNS-PLANNER 舟山案例局地监测关联范围工程假设，"
    "不代表 RTK baseline 或认证 GBAS service volume。"
)


def _normalize_navigation_integrity_planning(value, *, field, fallback_confirmed=False,
                                             fallback_source=None):
    raw = value if isinstance(value, dict) else {}
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    fixed = {
        "placement_scope": "route_endpoints",
        "required_monitor_per_endpoint": 1,
        "required_endpoint_roles": ["origin", "destination"],
        "require_distinct_physical_site_per_endpoint": True,
        "preferred_site_binding": "takeoff_landing_site",
        "local_monitoring_radius_m": 10000.0,
        "local_monitoring_radius_semantics": (
            "engineering_local_monitoring_context_not_rtk_baseline_not_certified_gbas_service_volume"
        ),
        "corridor_monitoring_required": False,
        "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        "confirmed": True,
        "maturity": "engineering_assumption",
        "source": NAVIGATION_INTEGRITY_POLICY_SOURCE,
    }
    for name, expected in fixed.items():
        supplied = raw.get(name)
        if supplied not in (None, "") and supplied != expected:
            raise ValueError(f"{field}.{name} 与 Round29-E canonical policy 不一致")
    return fixed


def _optional_positive_number(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是正有限数值或 null")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是正有限数值或 null") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{field} 必须是正有限数值或 null")
    return number


def _optional_positive_integer(value, field):
    number = _optional_positive_number(value, field)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError(f"{field} 必须是正整数或 null")
    return int(number)


__all__ = [
    "CANONICAL_SERVICE_DERIVATION_KEYS", "DERIVED_SERVICE_REQUIREMENT_SOURCE",
    "NAVIGATION_INTEGRITY_POLICY_SOURCE",
    "SERVICE_REGISTRY", "canonicalize_requirement_services",
    "derive_canonical_service_requirement", "normalize_service_requirements",
    "planner_family_for", "required_services_for", "service_registry_entry",
    "service_requirement_for", "service_scope_for",
]
