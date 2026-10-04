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


#: subsystem 的 RequiredCNS 服务**全部**落在该作用域时，该子系统在走廊体素口径上
#: 不适用（``not_applicable``）：它的正式状态只来自 endpoint 证据。
ENDPOINT_SERVICE_SCOPE = "route_endpoints"

#: 走廊体素口径下"该子系统不参与聚合"的唯一原因文本（P14 / P15 共用，措辞一致）。
ENDPOINT_SCOPE_NOT_APPLICABLE_REASON = (
    "该子系统的 RequiredCNS 服务全部为 route_endpoints 作用域："
    "正式状态只来自 endpoint_service_evidence / endpoint_service_gaps，"
    "绝不参与走廊体素聚合"
)


def endpoint_scope_only_subsystems(requirements):
    """哪些 subsystem 的 RequiredCNS 服务**全部**是 ``route_endpoints`` 作用域。

    P14（corridor 体素）与 P15（corridor 缺口聚合）共用**同一份**判定，绝不各写一套：

    * 该子系统存在显式 required 服务，**且**它们的 ``service_scope`` 集合完全落在
      ``route_endpoints`` 内 ⇒ 该子系统在走廊口径上是 ``not_applicable``；
    * dispatch **按 ``service_scope`` 判定**，绝不 hardcode 任何 subsystem 码；
    * 没有显式服务要求（legacy 项目）或存在 corridor 作用域服务时返回空集/不含该项
      —— 既有行为逐项不变；
    * 若未来某子系统**同时**存在 corridor 与 endpoint 服务，它**不**在此集合内：
      只有 corridor-scope 服务照常参与 voxel 判定，endpoint-scope 服务仍由 endpoint
      证据承载（两者绝不互相顶替）。
    """

    codes = set()
    for code, name in (("C", "communication"), ("N", "navigation"), ("S", "surveillance")):
        service_map = normalize_service_requirements(
            code, (requirements or {}).get(name) or {},
        )
        if not service_map:
            continue
        required = [item for item in service_map.values() if item.get("required") is True]
        if not required:
            continue
        scopes = {
            service_registry_entry(item.get("service_key")).get("service_scope")
            for item in required
        }
        if scopes and scopes <= {ENDPOINT_SERVICE_SCOPE}:
            codes.add(code)
    return frozenset(codes)


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


#: 拥有**冻结工程规划基线**、允许从"已确认的 subsystem 要求"确定性派生的 canonical
#: 服务集合（Round 29-H 扩展为完整四服务）。
#:
#: Round 2 只允许 Communication / RID 两项，于是 Navigation 永远派生不出
#: ``N:navigation_integrity_monitoring``、Surveillance 永远派生不出
#: ``S:radar_noncooperative`` —— 真实项目只能靠 direct-save fallback 绕过 recommendation。
#: 本轮按用户裁定把**已确认工程要求**对应的四服务全部纳入派生规则，规则仍然显式、
#: 可审计、fail-closed。
CANONICAL_SERVICE_DERIVATION_KEYS = (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
)

#: 派生来源标注：这些数值来自既有冻结的**工程规划基线 / canonical adapter**，
#: 既不是厂家实测规格，也不是从设备数据或机载能力反推出来的结论。
DERIVED_SERVICE_REQUIREMENT_SOURCE = "derived_from_confirmed_subsystem_service_key"

#: **显式的**"已确认 subsystem 要求 → canonical 服务"派生规则表。
#:
#: * ``conditions``：该服务被派生时**额外**必须成立的显式确认条件（OR 语义，空 = 无额外
#:   条件）。条件只读取 subsystem 要求里**显式声明**的字段，绝不读取设备数据、机载档案
#:   或任何未确认推断。
#: * 只有携带**冻结 surface 工程规划基线**（``SERVICE_SURFACE_POLICY``）或
#:   **canonical adapter 契约**（``SERVICE_REGISTRY`` 的 ``type`` / ``planning`` /
#:   ``planning_policy``）的服务才会被派生。
#: * 显式 ``service_key`` 落在 ``service_scope == "legacy"`` 时**绝不**派生。
CANONICAL_SERVICE_DERIVATION_RULES = {
    "C": (
        {"service_key": SERVICE_KEY_COMMUNICATION, "conditions": ()},
    ),
    "N": (
        {
            "service_key": SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
            #: 只有该 subsystem 显式要求"导航完整性"时才派生完整性监测服务 ——
            #: 只要求水平精度不构成完整性监测要求。
            "conditions": ("integrity", "performance.integrity_required"),
        },
    ),
    "S": (
        {"service_key": SERVICE_KEY_RID_COOPERATIVE, "conditions": ()},
        #: 合作监视（RID）与非合作监视（Radar）是同一个"监视"要求的两个互补侧面，
        #: 二者**绝不互相合并**为一条服务，也绝不互相顶替。
        {"service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE, "conditions": ()},
    ),
}


def derive_canonical_service_requirement(subsystem, requirement, *, field="required_cns"):
    """从**已确认**的 subsystem 要求确定性派生 canonical service requirements。

    fail-closed 规则：只有以下条件**全部**成立才派生，否则返回 ``None``，调用方必须
    保持 legacy 语义（绝不猜测、绝不把未确认要求升级成正式服务要求）：

    * ``required is True`` 且 ``confirmed is True``；
    * 显式 ``service_key``（若有）**不是** legacy 身份（``N:navigation`` /
      ``S:surveillance``）；
    * 显式 ``service_key``（若有）必须命中 :data:`CANONICAL_SERVICE_DERIVATION_RULES`
      里该 subsystem 的服务集合 —— 否则（例如 ``N:rtk_augmentation``）保持 fail-closed，
      由调用方显式声明 ``services``；
    * 每条规则的 ``conditions`` 全部满足。

    返回 ``{service_key: requirement}``（可含多条，例如 Surveillance → RID + Radar），
    内容**全部**取自既有 canonical 常量 / adapter 契约，不引入任何新数值，也不读取设备
    数据或机载能力。
    """

    code = str(subsystem or "").strip().upper()
    item = requirement if isinstance(requirement, dict) else {}
    if item.get("required") is not True or item.get("confirmed") is not True:
        return None
    rules = CANONICAL_SERVICE_DERIVATION_RULES.get(code) or ()
    if not rules:
        return None
    explicit_key = validated_service_key(
        item.get("service_key"), subsystem=code, field=f"{field}.service_key",
    )
    if explicit_key is not None:
        if SERVICE_REGISTRY[explicit_key]["service_scope"] == "legacy":
            return None
        if explicit_key not in {rule["service_key"] for rule in rules}:
            #: 显式声明了本规则表之外的 canonical 服务（例如 N:rtk_augmentation）：
            #: 绝不擅自扩大服务集，保持 fail-closed。
            return None
    derived = {}
    for rule in rules:
        key = rule["service_key"]
        if not _derivation_conditions_met(item, rule.get("conditions") or ()):
            continue
        entry = _derived_service_entry(code, key, item, field=field)
        if entry is None:
            continue
        derived[key] = entry
    return derived or None


def _derivation_conditions_met(requirement, conditions):
    """该 subsystem 要求是否**显式**满足这些确认条件（OR 语义；空 = 无额外条件）。"""

    if not conditions:
        return True
    for path in conditions:
        current = requirement
        for part in str(path).split("."):
            current = current.get(part) if isinstance(current, dict) else None
        if current is True:
            return True
    return False


def _derived_service_entry(code, key, requirement, *, field):
    """一条派生服务条目：surface policy 服务取冻结几何，adapter 服务取 canonical 契约。"""

    source = str(requirement.get("source") or DERIVED_SERVICE_REQUIREMENT_SOURCE)
    entry = {
        "service_key": key,
        "required": True,
        "confirmed": True,
        "status": "passed",
        "source": source,
        "service_requirement_source": DERIVED_SERVICE_REQUIREMENT_SOURCE,
    }
    policy = service_policy(key)
    if policy is not None:
        entry.update({
            "geometry": deepcopy(policy.get("geometry") or {}),
            "radius_by_surface": deepcopy(policy.get("radius_by_surface") or {}),
            "redundancy_by_surface": deepcopy(policy.get("redundancy_by_surface") or {}),
            "radius_basis": policy.get("radius_basis"),
            "maturity": policy.get("maturity"),
            "parameter_origin": policy.get("parameter_origin"),
            "parameter_semantics": policy.get("parameter_semantics"),
            "missing_device_evidence": list(policy.get("missing_device_evidence") or []),
            "not_evaluated": list(policy.get("not_evaluated") or []),
        })
        if requirement.get("coverage_requirement") is not None:
            entry["coverage_requirement"] = requirement.get("coverage_requirement")
        if policy.get("type"):
            entry["type"] = deepcopy(policy["type"])
        return entry

    registry = SERVICE_REGISTRY.get(key)
    if registry is None:
        return None
    if registry.get("type"):
        entry["type"] = deepcopy(registry["type"])
    if registry.get("planning_policy"):
        #: Radar-I orientation-first contract（含"绝不自动升级 Radar-II"）。
        entry["planning_policy"] = deepcopy(registry["planning_policy"])
    if key == SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING:
        #: Round29-E 已确认的 endpoint contract（placement_scope / 每端 1 套 /
        #: 独立物理站址 / 10 km 局地监测关联范围工程假设）。
        entry["planning"] = _normalize_navigation_integrity_planning(
            None, field=f"{field}.services[{key}].planning",
            fallback_confirmed=True, fallback_source=source,
        )
    return entry


def canonicalize_requirement_services(subsystem, requirement, *, field="required_cns"):
    """解析一个 subsystem 要求上的正式 ``services``（显式优先，其次确定性派生）。

    返回 ``(services, provenance)``：

    * 显式 ``services`` 存在 ⇒ 原样返回该映射，``provenance.derived = False``；
      形状/合法性的 canonical 校验仍由 ``normalize_required_cns`` 完成。
    * 否则尝试 :func:`derive_canonical_service_requirement`；成功则返回派生映射与
      可审计的 ``provenance``（含逐服务的派生依据）。
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
        "basis": (
            "confirmed_subsystem_requirement_plus_frozen_surface_policy_or_"
            "canonical_service_adapter_contract"
        ),
        "service_keys": sorted(derived),
        "reason": (
            "该 subsystem 的已确认要求命中显式派生规则表，且对应服务拥有冻结 surface "
            "工程规划基线或 canonical adapter 契约：按既有 canonical 常量确定性派生正式 "
            "service requirement（绝不由设备数据或机载能力推断）"
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
    #: Round 29-H：Radar 的 **Radar-I orientation-first** 规划契约必须随服务身份一起
    #: 进入 canonical ``services``（显式声明与 recommendation 派生两条路径**同一结果**）。
    #: 调用方显式给出的同名字段优先，但一律以 registry 的冻结值为基线 —— 这不放宽任何
    #: evidence gate，只是补齐既有 canonical 契约。
    registry_planning_policy = SERVICE_REGISTRY[key].get("planning_policy")
    if registry_planning_policy:
        supplied_policy = (
            source.get("planning_policy") if isinstance(source.get("planning_policy"), dict)
            else {}
        )
        normalized["planning_policy"] = {
            **deepcopy(registry_planning_policy), **deepcopy(supplied_policy),
        }
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
    "CANONICAL_SERVICE_DERIVATION_KEYS", "CANONICAL_SERVICE_DERIVATION_RULES",
    "DERIVED_SERVICE_REQUIREMENT_SOURCE", "ENDPOINT_SCOPE_NOT_APPLICABLE_REASON",
    "ENDPOINT_SERVICE_SCOPE", "NAVIGATION_INTEGRITY_POLICY_SOURCE",
    "SERVICE_REGISTRY", "canonicalize_requirement_services",
    "derive_canonical_service_requirement", "endpoint_scope_only_subsystems",
    "normalize_service_requirements",
    "planner_family_for", "required_services_for", "service_registry_entry",
    "service_requirement_for", "service_scope_for",
]
