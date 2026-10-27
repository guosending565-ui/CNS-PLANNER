"""Research-only, explicitly versioned N endpoint contract compatibility layer.

This module is **not** the canonical N contract.  The canonical contract stays
``navigation_integrity_monitoring_v1`` (10 000 m engineering local-monitoring
context, ``confirmed=true``) and is selected whenever an input declares no
explicit version.  Nothing here changes that default.

The layer is reached only when an input **explicitly** declares::

    planning.contract_version == "navigation_integrity_monitoring_v2"

(``planning.version`` is accepted as the equivalent alias).  It exists so the
NM3 middle-reuse research chain can consume the already validated 1 km endpoint
association semantics without re-admitting v2 as a global formal contract.
Every entry point is fail-closed: an incomplete/invalid v2 identity produces
``unknown`` endpoint evidence, never a silent fallback to v1 and never a pass.

Consumers: the research chain only (NM3 / R0005 research runs).  The formal
ProjectState writers, the default policy and the v1 output shapes are untouched.
"""
from __future__ import annotations

from copy import deepcopy
from math import asin, cos, isfinite, radians, sin, sqrt

from .cns_service_contract import (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
)
from .navigation_integrity_monitoring import (
    ENDPOINT_ROLES,
    REASON_DELIVERY_DEFICIT,
    REASON_EVIDENCE_UNKNOWN,
    REASON_MISSING,
    STATUS_CONFIRMED_DEFICIT,
    STATUS_SATISFIED,
    STATUS_UNKNOWN,
    _coordinate_usable,
)

#: Explicit marker: this layer is research-only and must not be treated as the
#: formal N contract or as an installation/delivery statement.
RESEARCH_ONLY = True
CONSUMER_SCOPE = "research_only"

CONTRACT_VERSION_V1 = "navigation_integrity_monitoring_v1"
CONTRACT_VERSION_V2 = "navigation_integrity_monitoring_v2"

#: v2 local monitoring association radius (engineering assumption, not an RTK
#: baseline and not a certified GBAS service volume).
RADIUS_M = 1000.0

POLICY_SOURCE_V2 = (
    "用户正式假设（2026-10-22）：导航完好性监视按每个起降点 1 km 局地监测关联范围规划；"
    "1 km 为 engineering_assumption，不代表 RTK baseline 或认证 GBAS service volume；"
    "每个起降点仍需 1 套独立物理设备，不沿航路中段强制布站。"
)

POLICY = {
    "contract_version": CONTRACT_VERSION_V2,
    "placement_scope": "route_endpoints",
    "required_monitor_per_endpoint": 1,
    "required_endpoint_roles": ["origin", "destination"],
    "require_distinct_physical_site_per_endpoint": True,
    "preferred_site_binding": "takeoff_landing_site",
    "local_monitoring_radius_m": RADIUS_M,
    "radius_basis": "engineering_assumption",
    "local_monitoring_radius_semantics": (
        "engineering_local_monitoring_context_not_rtk_baseline_not_certified_gbas_service_volume"
    ),
    "corridor_monitoring_required": False,
    "delivery_service_key": SERVICE_KEY_COMMUNICATION,
    "confirmed": False,
    "maturity": "engineering_assumption",
    "source": POLICY_SOURCE_V2,
}

VERSION_KEYS = ("version", "contract_version")

#: Sentinel for an incomplete/invalid explicit v2 identity.  Callers must never
#: fall back to v1 when they see it.
INVALID_V2_CONTRACT = "invalid_navigation_integrity_monitoring_v2_contract"

#: Mean spherical Earth radius (m); used only for endpoint-monitor association.
EARTH_MEAN_RADIUS_M = 6371008.8


def normalize_v2_planning(value, *, field):
    """Validate and normalize one explicit v2 ``planning`` object.

    The v1 fields are version-exclusive: a v2 declaration that carries the v1
    10 000 m radius (or any other v1 value) is rejected, never treated as an
    equivalent input.
    """

    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    raw = value if isinstance(value, dict) else {}

    supplied_radius = raw.get("local_monitoring_radius_m")
    if supplied_radius not in (None, ""):
        try:
            radius = float(supplied_radius)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{field}.local_monitoring_radius_m 必须是 {RADIUS_M}"
            ) from exc
        if radius != RADIUS_M:
            raise ValueError(
                f"{field}.local_monitoring_radius_m 与 navigation_integrity_monitoring_v2 "
                f"canonical policy 不一致（版本互斥：v2 只接受 {RADIUS_M} m）"
            )
    for name, expected in POLICY.items():
        if name in ("contract_version", "source"):
            continue
        supplied = raw.get(name)
        if supplied not in (None, "") and supplied != expected:
            raise ValueError(
                f"{field}.{name} 与 navigation_integrity_monitoring_v2 canonical policy 不一致"
            )
    return deepcopy(POLICY)


def _declared_versions(value, field):
    versions = {}
    for name in VERSION_KEYS:
        supplied = (value or {}).get(name)
        if supplied in (None, ""):
            continue
        text = str(supplied).strip()
        if text not in (CONTRACT_VERSION_V1, CONTRACT_VERSION_V2):
            raise ValueError(
                f"{field}.{name} 必须是 {CONTRACT_VERSION_V1} 或 {CONTRACT_VERSION_V2}"
            )
        versions[name] = text
    if len(set(versions.values())) > 1:
        raise ValueError(f"{field}.version 与 {field}.contract_version 必须一致")
    return versions


def explicit_version(value):
    """Return the explicit contract version declared by a ``planning`` mapping."""

    versions = _declared_versions(value, "planning")
    return next(iter(versions.values()), None)


def normalize_planning(value, *, field):
    """Version gate shared with the canonical registry.

    Returns the v2 research policy only when the input explicitly declares v2;
    returns ``None`` for "no version" or explicit v1 so the caller keeps the
    canonical v1 normalization byte-identical.  An unknown version or a
    ``version``/``contract_version`` mismatch is rejected (fail-closed), never
    silently downgraded to v1.
    """

    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    raw = value if isinstance(value, dict) else {}
    versions = _declared_versions(raw, field)
    selected = next(iter(versions.values()), None)
    if selected in (None, CONTRACT_VERSION_V1):
        return None
    return normalize_v2_planning(raw, field=field)


def horizontal_distance_m(a, b):

    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)
    h = sin(d_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2.0) ** 2
    return 2.0 * EARTH_MEAN_RADIUS_M * asin(min(1.0, sqrt(h)))


def contract_from_requirement(requirement):
    """Parse one N requirement into ``None`` (v1) / contract dict / sentinel."""

    planning = (requirement or {}).get("planning")
    if not isinstance(planning, dict):
        return None
    version = explicit_version(planning)
    if version in (None, ""):
        return None
    if version == CONTRACT_VERSION_V1:
        return None
    if version != CONTRACT_VERSION_V2:
        return INVALID_V2_CONTRACT
    radius = planning.get("local_monitoring_radius_m")
    if (
        planning.get("placement_scope") != "route_endpoints"
        or isinstance(radius, bool)
        or not isinstance(radius, (int, float))
        or not isfinite(float(radius))
        or float(radius) <= 0.0
    ):
        return INVALID_V2_CONTRACT
    return {
        "contract_version": CONTRACT_VERSION_V2,
        "local_monitoring_radius_m": float(radius),
        "radius_basis": planning.get("radius_basis") or "engineering_assumption",
    }


def endpoint_contract(endpoint):
    """Recover the v2 contract of one endpoint record (``None`` for v1)."""

    if (endpoint or {}).get("contract_status"):
        return INVALID_V2_CONTRACT
    version = (endpoint or {}).get("contract_version")
    if version in (None, ""):
        return None
    if str(version) != CONTRACT_VERSION_V2:
        return INVALID_V2_CONTRACT
    radius = (endpoint or {}).get("local_monitoring_radius_m")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)):
        return INVALID_V2_CONTRACT
    return {
        "contract_version": CONTRACT_VERSION_V2,
        "local_monitoring_radius_m": float(radius),
        "radius_basis": (endpoint or {}).get("radius_basis") or "engineering_assumption",
    }


def contract_view(endpoint):
    """v2 contract view for gap/action payloads (empty dict for v1 records)."""

    version = (endpoint or {}).get("contract_version")
    view = {}
    if version not in (None, ""):
        view["contract_version"] = version
        view["local_monitoring_radius_m"] = endpoint.get("local_monitoring_radius_m")
        if endpoint.get("radius_basis"):
            view["radius_basis"] = endpoint.get("radius_basis")
    elif (endpoint or {}).get("contract_status"):
        view["contract_version"] = None
        view["local_monitoring_radius_m"] = None
        view["contract_status"] = endpoint.get("contract_status")
    rejected = [
        str(item.get("provider_id"))
        for item in endpoint.get("rejected_out_of_radius_providers") or []
        if item.get("provider_id")
    ]
    if rejected and view:
        view["rejected_out_of_radius_provider_ids"] = rejected
    return view


def partition_providers(endpoint_coordinate, providers, radius_m):
    """Split bound monitors into accepted / rejected / unresolved by radius."""

    accepted, rejected, unresolved = [], [], []
    for item in providers or []:
        provider = deepcopy(item)
        provider_coordinate = provider.get("coordinate")
        if not _coordinate_usable(provider_coordinate):
            unresolved.append(provider)
            continue
        try:
            distance = horizontal_distance_m(endpoint_coordinate, provider_coordinate)
        except (TypeError, ValueError):
            unresolved.append(provider)
            continue
        provider["horizontal_distance_m"] = distance
        if distance <= radius_m:
            accepted.append(provider)
        else:
            rejected.append(provider)
    return accepted, rejected, unresolved


def endpoint_record(role, site_id, coordinate, providers, delivery, contract):
    """v2 endpoint evidence: only monitors inside the local radius count."""

    invalid = contract == INVALID_V2_CONTRACT
    radius = None if invalid else contract["local_monitoring_radius_m"]
    endpoint_ok = bool(site_id) and _coordinate_usable(coordinate)
    if invalid or not endpoint_ok:
        accepted, rejected, unresolved = [], [], [deepcopy(item) for item in providers]
    else:
        accepted, rejected, unresolved = partition_providers(coordinate, providers, radius)

    reasons = []
    if invalid or not endpoint_ok or (not accepted and unresolved):
        planning = status = STATUS_UNKNOWN
        reasons.append(REASON_EVIDENCE_UNKNOWN)
    elif not accepted:
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

    record = {
        "endpoint_role": role, "coordinate": deepcopy(coordinate),
        "takeoff_landing_site_id": site_id, "required_count": 1,
        "selected_installed_providers": accepted,
        "distinct_site_ids": sorted({
            str(item.get("distinct_site_id")) for item in accepted
            if item.get("distinct_site_id")
        }),
        "planning_status": planning,
        "delivery_service_key": SERVICE_KEY_COMMUNICATION,
        "delivery_status": str((delivery or {}).get("status") or STATUS_UNKNOWN),
        "status": status, "reasons": reasons,
        "integrity_measurement_status": "not_measured_not_certified",
        "rejected_out_of_radius_providers": rejected,
        "unresolved_provider_evidence": unresolved,
    }
    if invalid:
        record["contract_version"] = None
        record["local_monitoring_radius_m"] = None
        record["contract_status"] = "invalid_or_incomplete_v2_contract"
    else:
        record["contract_version"] = contract["contract_version"]
        record["local_monitoring_radius_m"] = radius
        record["radius_basis"] = contract["radius_basis"]
    return record


def recompute_endpoint(endpoint, role):
    """v2 what-if recomputation of one endpoint record (pure)."""

    contract = endpoint_contract(endpoint)
    invalid = contract == INVALID_V2_CONTRACT
    providers = list(endpoint.get("selected_installed_providers") or [])
    site_id = endpoint.get("takeoff_landing_site_id")
    coordinate = endpoint.get("coordinate")
    if invalid or not site_id or not _coordinate_usable(coordinate):
        accepted, rejected, unresolved = [], [], providers
    else:
        accepted, rejected, unresolved = partition_providers(
            coordinate, providers, contract["local_monitoring_radius_m"],
        )
    reasons = []
    if invalid or not site_id or not _coordinate_usable(coordinate) or (
            not accepted and unresolved):
        planning = status = STATUS_UNKNOWN
        reasons.append(REASON_EVIDENCE_UNKNOWN)
    elif not accepted:
        planning = status = STATUS_CONFIRMED_DEFICIT
        reasons.append(REASON_MISSING[role])
    else:
        planning = STATUS_SATISFIED
        delivery_status = str(endpoint.get("delivery_status") or STATUS_UNKNOWN)
        if delivery_status == STATUS_CONFIRMED_DEFICIT:
            #: A monitor only discharges monitor_missing: a confirmed
            #: Communication delivery deficit never becomes satisfied.
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
        str(item.get("distinct_site_id")) for item in accepted
        if item.get("distinct_site_id")
    })
    endpoint["rejected_out_of_radius_providers"] = rejected
    endpoint["unresolved_provider_evidence"] = unresolved
    endpoint.pop("shared_distinct_site_ids_rejected", None)
    return endpoint


def apply_evidence_policy(result, route_contracts):
    """Attach v2 policy identity to an endpoint-evidence result (in place)."""

    contracts = list(route_contracts.values())
    if not contracts:
        return result
    primary_contract, primary_requirement = contracts[0]
    result["policy"] = deepcopy((primary_requirement.get("planning") or {}))
    result["contract_version"] = CONTRACT_VERSION_V2
    result["local_monitoring_radius_m"] = primary_contract["local_monitoring_radius_m"]
    result["radius_basis"] = primary_contract["radius_basis"]
    result["route_policies"] = {
        route_id: deepcopy((requirement.get("planning") or {}))
        for route_id, (_contract, requirement) in route_contracts.items()
    }
    return result


__all__ = [
    "CONSUMER_SCOPE", "CONTRACT_VERSION_V1", "CONTRACT_VERSION_V2",
    "EARTH_MEAN_RADIUS_M", "INVALID_V2_CONTRACT", "POLICY", "POLICY_SOURCE_V2",
    "RADIUS_M", "RESEARCH_ONLY", "VERSION_KEYS", "apply_evidence_policy",
    "contract_from_requirement", "contract_view", "endpoint_contract",
    "endpoint_record", "explicit_version", "horizontal_distance_m",
    "normalize_planning", "normalize_v2_planning", "partition_providers",
    "recompute_endpoint",
]
