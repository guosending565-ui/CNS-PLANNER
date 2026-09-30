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
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
    SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_KEY_RID_COOPERATIVE,
    SERVICE_KEY_SURVEILLANCE,
    normalize_redundancy_by_surface,
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
    },
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION: {
        "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
        "subsystem": "N",
        "label": "导航 RTK 增强（Navigation RTK Augmentation）",
        "planner_family": "navigation_reference_station",
        "provider_model": "reference_station_network",
        "requirement_semantics": "explicit_ground_augmentation_requirement",
        "planning_maturity": "contract_only_pending_confirmation",
        "dependencies": [SERVICE_KEY_COMMUNICATION],
        "surface_dependent": False,
        "supports_site_planning": True,
        "type": deepcopy(RTK_AUGMENTATION_TYPE_SEMANTICS),
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
        "type": deepcopy(RADAR_TYPE_SEMANTICS),
    },
    # Legacy identities remain addressable, but do not acquire a new planner.
    SERVICE_KEY_NAVIGATION: {
        "service_key": SERVICE_KEY_NAVIGATION, "subsystem": "N",
        "label": "导航（legacy）", "planner_family": "legacy_subsystem",
        "provider_model": "legacy_navigation_capability",
        "requirement_semantics": "legacy_single_service_requirement",
        "planning_maturity": "legacy_compatible", "dependencies": [],
        "surface_dependent": False, "supports_site_planning": False,
    },
    SERVICE_KEY_SURVEILLANCE: {
        "service_key": SERVICE_KEY_SURVEILLANCE, "subsystem": "S",
        "label": "监视（legacy）", "planner_family": "legacy_subsystem",
        "provider_model": "legacy_surveillance_provider",
        "requirement_semantics": "legacy_single_service_requirement",
        "planning_maturity": "legacy_compatible", "dependencies": [],
        "surface_dependent": False, "supports_site_planning": False,
    },
}


def service_registry_entry(service_key):
    """Return a defensive copy of one registered service; unknown keys fail closed."""

    key = validated_service_key(service_key, field="service_key")
    return deepcopy(SERVICE_REGISTRY[key])


def planner_family_for(service_key):
    return service_registry_entry(service_key)["planner_family"]


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
    "SERVICE_REGISTRY", "normalize_service_requirements", "planner_family_for",
    "required_services_for", "service_registry_entry", "service_requirement_for",
]
