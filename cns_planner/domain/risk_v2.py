"""JSON-safe contracts for the additive Risk Framework V2.

Risk Framework V2 decomposes the single legacy relative ``overall`` risk of
``RiskModelV1`` into three engineering domains:

``ground`` / ``air_traffic`` / ``environment_obstacle``

Every number produced here is a **relative engineering index**.  Nothing in
this module computes an accident probability, a SORA GRC/ARC value or an
absolute safety risk.  Missing / unknown inputs are never converted into zero,
and no default production weight is shipped: an aggregation policy stays
``pending_confirmation`` until a project engineering source records explicit,
traceable weights.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from numbers import Real

SCHEMA_VERSION = "risk-framework-v2"
POLICY_SCHEMA_VERSION = "risk-policy-v2"

DOMAIN_IDS = ("ground", "air_traffic", "environment_obstacle")

DOMAIN_LABELS = {
    "ground": "Ground（地面暴露）",
    "air_traffic": "Air / Traffic（空中交通暴露）",
    "environment_obstacle": "Environment / Obstacle（工程环境-障碍物）",
}

#: Canonical factor catalogue.  ``source_field`` documents which canonical cell
#: quantity a factor is allowed to consume; it is never re-derived from another
#: namespace and it is never guessed.
FACTOR_DEFINITIONS = {
    "population_exposure": {
        "domain": "ground",
        "raw_unit": "people/km²",
        "source_role": "population",
        "source_field": "population_density_people_km2",
        "label": "人口暴露",
    },
    "property_exposure": {
        "domain": "ground",
        "raw_unit": None,
        "source_role": "property_exposure",
        "source_field": None,
        "label": "财产暴露（无数据源）",
    },
    "critical_infrastructure_exposure": {
        "domain": "ground",
        "raw_unit": None,
        "source_role": "infrastructure",
        "source_field": None,
        "label": "关键基础设施暴露（无数据源）",
    },
    "uav_traffic_exposure": {
        "domain": "air_traffic",
        "raw_unit": "relative_index_0_1",
        "source_role": "traffic",
        "source_field": "traffic_density_norm",
        "label": "UAV 交通暴露（flight_count / flight_seconds）",
    },
    "conflict_exposure": {
        "domain": "air_traffic",
        "raw_unit": "relative_index_0_1",
        "source_role": "conflict",
        "source_field": "conflict_rate_norm",
        "label": "冲突暴露",
    },
    "terrain_relief": {
        "domain": "environment_obstacle",
        "raw_unit": "m",
        "source_role": "terrain",
        "source_field": "surface_elevation_max_m - surface_elevation_min_m",
        "label": "地形起伏",
    },
    "building_coverage": {
        "domain": "environment_obstacle",
        "raw_unit": "ratio_0_1",
        "source_role": "buildings",
        "source_field": "building_coverage_ratio",
        "label": "建筑覆盖率",
    },
    "building_height": {
        "domain": "environment_obstacle",
        "raw_unit": "m",
        "source_role": "buildings",
        "source_field": "height_p95_m / height_max_m + valid_height_fraction",
        "label": "建筑高度",
    },
}

#: Attribute namespaces Risk Framework V2 is allowed to read.  ``airspace`` is
#: deliberately absent: it stays a display-only reference layer and can never
#: enter a V2 factor, value or fingerprint.
FACTOR_INPUT_ATTRIBUTES = (
    "population", "terrain", "traffic", "conflict", "buildings",
)

FACTOR_SOURCE_ROLE = {
    factor_id: definition["source_role"] for factor_id, definition in FACTOR_DEFINITIONS.items()
}

FACTOR_IDS = tuple(FACTOR_DEFINITIONS)
DOMAIN_FACTOR_IDS = {
    domain: tuple(
        factor_id for factor_id, definition in FACTOR_DEFINITIONS.items()
        if definition["domain"] == domain
    )
    for domain in DOMAIN_IDS
}

#: Factors whose canonical source is known to be absent in this system.  They
#: stay ``unknown`` forever and must never be filled with 0 or with a guess.
NO_SOURCE_FACTOR_IDS = (
    "property_exposure", "critical_infrastructure_exposure",
)

AGGREGATION_METHODS = ("weighted_sum",)

POLICY_PENDING_SOURCE = "未配置；Risk Framework V2 聚合策略必须由项目工程依据显式确认"

RELATIVE_INDEX_SEMANTICS = {
    "risk_semantics": "relative_engineering_index",
    "not_accident_probability": True,
    "not_sora_grc": True,
    "not_sora_arc": True,
    "not_absolute_safety_risk": True,
    "relative_scaling_only_not_a_safety_threshold": True,
    "missing_or_unknown_is_never_zero": True,
    "no_automatic_weight_renormalization": True,
    "airspace_is_display_only_not_a_factor": True,
    "clearance_breach_is_feasibility_not_risk": True,
}


def stable_fingerprint(value, *, prefix=""):
    """Deterministic fingerprint used for every V2 source/policy/input record."""

    return prefix + sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def factor_ids_for_domain(domain_id):
    return DOMAIN_FACTOR_IDS[domain_id]


# --------------------------------------------------------------------------- policy


def _empty_domain_policy(domain_id):
    return {
        "domain_id": domain_id,
        "method": None,
        "weights": {},
        "required_factors": [],
        "source": POLICY_PENDING_SOURCE,
        "source_origin": "unrecorded",
        "evidence": None,
        "confirmed": False,
        "status": "pending_confirmation",
        "status_reason": "aggregation_method_not_configured",
        "parameter_status": "no_default_production_risk_weights",
    }


def default_risk_policy_v2():
    """No production risk weight exists by default.

    The default is explicitly *not configured*: domains aggregate to
    ``index = null`` with ``status = pending_confirmation`` until a confirmed,
    traceable policy is supplied.
    """

    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "status": "pending_confirmation",
        "parameter_status": "no_default_production_risk_weights",
        "domains": {domain_id: _empty_domain_policy(domain_id) for domain_id in DOMAIN_IDS},
        "overall": {
            "status": "not_configured",
            "index": None,
            "reason": "cross_domain_aggregation_is_not_part_of_risk_framework_v2",
            "note": "路径 cost 权重属于下一阶段 Layered Risk-Aware Route Planner policy，不属于 Risk Framework。",
        },
        "semantics": deepcopy(RELATIVE_INDEX_SEMANTICS),
    }


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是有限非负数")
    return number


def _normalize_weights(domain_id, raw):
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{domain_id} 权重必须是对象")
    allowed = set(DOMAIN_FACTOR_IDS[domain_id])
    weights = {}
    for factor_id, value in raw.items():
        key = str(factor_id)
        if key not in allowed:
            raise ValueError(f"{domain_id} 权重包含未知 factor：{key}")
        weight = _optional_number(value, f"{domain_id}.weights.{key}")
        if weight is None:
            raise ValueError(f"{domain_id}.weights.{key} 不能为空")
        weights[key] = weight
    return weights


def _normalize_required(domain_id, raw):
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{domain_id}.required_factors 必须是数组")
    allowed = set(DOMAIN_FACTOR_IDS[domain_id])
    required = []
    for value in raw:
        key = str(value)
        if key not in allowed:
            raise ValueError(f"{domain_id}.required_factors 包含未知 factor：{key}")
        if key not in required:
            required.append(key)
    return required


def normalize_domain_policy(domain_id, value):
    """Validate one domain aggregation policy; never normalizes user weights."""

    source = value if isinstance(value, dict) else {}
    result = _empty_domain_policy(domain_id)
    method = source.get("method")
    if method in ("", None):
        method = None
    elif str(method) not in AGGREGATION_METHODS:
        raise ValueError(f"{domain_id}.method 只支持 {AGGREGATION_METHODS}，收到 {method!r}")
    else:
        method = str(method)
    source_text = str(source.get("source") or "").strip()
    explicit_source = bool(source_text) and source_text != POLICY_PENDING_SOURCE
    evidence = source.get("evidence")
    weights = _normalize_weights(domain_id, source.get("weights"))
    required = _normalize_required(domain_id, source.get("required_factors"))
    confirmed = bool(source.get("confirmed", False))
    result.update({
        "method": method,
        "weights": weights,
        "required_factors": required,
        "source": source_text or POLICY_PENDING_SOURCE,
        "evidence": deepcopy(evidence) if evidence not in (None, "") else None,
        "confirmed": confirmed,
    })
    result["source_origin"] = "explicit" if explicit_source else "unrecorded"
    if confirmed and not explicit_source:
        raise ValueError(f"{domain_id} 声明 confirmed 时必须提供显式 source")

    if method is None:
        if weights:
            raise ValueError(f"{domain_id} 未声明 method 时不得提供 weights")
        result["status"] = "pending_confirmation"
        result["status_reason"] = "aggregation_method_not_configured"
        return result

    if not weights:
        raise ValueError(f"{domain_id} weighted_sum 必须提供显式 weights")
    total = math.fsum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"{domain_id} weighted_sum 的权重和必须约等于 1（当前 {total!r}）；禁止自动归一化用户权重"
        )
    if not required:
        raise ValueError(f"{domain_id} 必须显式声明 required_factors")
    missing_weight = [factor_id for factor_id in required if factor_id not in weights]
    if missing_weight:
        raise ValueError(f"{domain_id} required_factors 缺少权重：{missing_weight}")
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "policy_not_confirmed"
        return result
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def normalize_risk_policy_v2(value):
    """Idempotent normalizer.  Nothing is filled in from a code default weight."""

    source = value if isinstance(value, dict) else {}
    domains_source = source.get("domains") if isinstance(source.get("domains"), dict) else {}
    domains = {}
    for domain_id in DOMAIN_IDS:
        raw = domains_source.get(domain_id)
        if not isinstance(raw, dict) and raw not in (None,):
            raise ValueError(f"risk_policy_v2.domains.{domain_id} 必须是对象")
        domains[domain_id] = normalize_domain_policy(domain_id, raw)
    result = default_risk_policy_v2()
    result["domains"] = domains
    result["status"] = (
        "confirmed" if all(item["status"] == "confirmed" for item in domains.values())
        else "pending_confirmation"
    )
    result["overall"] = deepcopy(default_risk_policy_v2()["overall"])
    return result


def policy_fingerprint(policy):
    return stable_fingerprint(policy, prefix="riskpolicyv2-")


def domain_policy_fingerprint(domain_policy):
    return stable_fingerprint(domain_policy, prefix="riskdomainpolicyv2-")


def domain_aggregation_policy_fingerprint(domain_id, domain_policy):
    return stable_fingerprint(
        {"domain_id": domain_id, "policy": domain_policy}, prefix="riskaggv2-",
    )


# --------------------------------------------------------------------------- result contract


def not_computed_model(model_id, reason, required_models):
    return {
        "status": "not_computed",
        "value": None,
        "unit": None,
        "model_id": model_id,
        "reason": reason,
        "required_models": list(required_models),
    }


def empty_absolute_risk():
    return not_computed_model(
        "absolute_risk",
        "requires_verified_failure_impact_exposure_consequence_and_encounter_models",
        [
            "verified_failure_probability_or_rate_model",
            "verified_ground_impact_probability_model",
            "verified_exposure_model",
            "verified_consequence_model",
            "verified_encounter_model",
        ],
    )


def empty_sora_grc():
    return not_computed_model(
        "sora_grc",
        "requires_verified_sora_ground_risk_class_containment_and_mitigation_evidence",
        [
            "verified_aircraft_characteristics",
            "verified_containment_evidence",
            "verified_mitigation_evidence",
            "authoritative_grc_scale",
        ],
    )


def empty_sora_arc():
    return not_computed_model(
        "sora_arc",
        "requires_verified_air_risk_class_encounter_model_and_airspace_evidence",
        [
            "verified_encounter_model",
            "verified_airspace_characterisation",
            "authoritative_arc_scale",
        ],
    )


def display_only_airspace():
    return {
        "status": "not_applicable",
        "applicability": "display_only",
        "role": "display_only_reference_layer",
        "is_factor": False,
        "is_domain": False,
        "is_constraint": False,
        "used_in_value_or_fingerprint": False,
        "semantics": "display_only_airspace_not_used_as_factor_risk_or_constraint",
    }


def empty_grid_risk_v2(status="not_calculated"):
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "algorithm_id": "risk-framework-v2-domains",
        "algorithm_version": "2.0",
        "risk_semantics": "relative_engineering_index",
        "absolute_risk": empty_absolute_risk(),
        "sora_grc": empty_sora_grc(),
        "sora_arc": empty_sora_arc(),
        "airspace": display_only_airspace(),
        "policy": default_risk_policy_v2(),
        "policy_fingerprint": None,
        "input_fingerprint": None,
        "input_status": {},
        "source_versions": {},
        "references": {},
        "factor_status": {},
        "factor_definitions": deepcopy(FACTOR_DEFINITIONS),
        "domains": {},
        "overall": {
            "status": "not_configured",
            "index": None,
            "contributors": {},
            "reason": "cross_domain_aggregation_is_not_part_of_risk_framework_v2",
            "semantics": "optional_compatibility_extension_never_defaulted",
        },
        "grid_level": None,
        "count": 0,
        "data_completeness": 0.0,
        "cells": {},
        "semantics": deepcopy(RELATIVE_INDEX_SEMANTICS),
        "notes": [
            "V2 只产出可解释的 relative engineering index；不声称事故概率、SORA GRC/ARC 或绝对安全风险。",
            "UAV traffic exposure 来自 flight_count/flight_seconds，只属于 Air/Traffic domain，不是地面交通。",
            "建筑 coverage 不是 sheltering；terrain/building hard clearance 属于 feasibility，不转换为 risk。",
        ],
    }


def normalize_grid_risk_v2(value):
    """Idempotent backfill for legacy projects without a V2 result."""

    if not isinstance(value, dict):
        return empty_grid_risk_v2()
    result = empty_grid_risk_v2(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result.setdefault("risk_semantics", "relative_engineering_index")
    result.setdefault("absolute_risk", empty_absolute_risk())
    result.setdefault("sora_grc", empty_sora_grc())
    result.setdefault("sora_arc", empty_sora_arc())
    result.setdefault("airspace", display_only_airspace())
    result.setdefault("semantics", deepcopy(RELATIVE_INDEX_SEMANTICS))
    for key, default in (
        ("input_status", {}), ("source_versions", {}), ("references", {}),
        ("factor_status", {}), ("domains", {}), ("cells", {}),
    ):
        if not isinstance(result.get(key), dict):
            result[key] = deepcopy(default)
    result.setdefault("notes", empty_grid_risk_v2()["notes"])
    result["policy"] = normalize_risk_policy_v2(result.get("policy"))
    overall = result.get("overall")
    if not isinstance(overall, dict) or overall.get("status") not in ("not_configured",):
        result["overall"] = deepcopy(empty_grid_risk_v2()["overall"])
    result["overall"].setdefault("index", None)
    result["overall"].setdefault("contributors", {})
    result["overall"]["status"] = "not_configured"
    result["overall"]["reason"] = "cross_domain_aggregation_is_not_part_of_risk_framework_v2"
    return result


__all__ = [
    "AGGREGATION_METHODS", "DOMAIN_FACTOR_IDS", "DOMAIN_IDS", "DOMAIN_LABELS",
    "FACTOR_DEFINITIONS", "FACTOR_IDS", "FACTOR_INPUT_ATTRIBUTES", "FACTOR_SOURCE_ROLE",
    "NO_SOURCE_FACTOR_IDS",
    "POLICY_PENDING_SOURCE", "POLICY_SCHEMA_VERSION", "RELATIVE_INDEX_SEMANTICS",
    "SCHEMA_VERSION", "default_risk_policy_v2", "display_only_airspace",
    "domain_aggregation_policy_fingerprint", "domain_policy_fingerprint",
    "empty_absolute_risk", "empty_grid_risk_v2", "empty_sora_arc",
    "empty_sora_grc", "factor_ids_for_domain", "normalize_domain_policy",
    "normalize_grid_risk_v2", "normalize_risk_policy_v2", "not_computed_model",
    "policy_fingerprint", "stable_fingerprint",
]
