"""RouteRiskProfile V1 的 JSON-safe 契约（Layered Route Planner V1 candidate → 路径风险画像）。

本契约只描述**一个 current LayeredRouteCandidate** 在当前 ``grid_risk_v2`` 与当前
``RouteRiskProfilePolicy`` 下的路径暴露画像。它不重规划、不修改 candidate、不写
``operational_routes`` / CNS / ``RouteOperatingLayer``，也不做参考航线比较。

硬边界（全部由代码强制，而不是靠约定）：

* 每个 domain 的 ``{medium_min, high_min}`` **没有默认值**：未配置/未确认时
  ``classification`` 与 high-risk length/intervals 一律 ``not_configured`` / ``None``，
  绝不猜 0.6 / 0.8 之类的阈值；
* classification 只在 **per-domain** 范围成立，不生成 cross-domain overall 或
  cross-domain high-risk；
* domain / factor 缺失保留 unresolved 长度，绝不用 0 补齐；
* factor contributor 只是 **relative engineering contribution**，不是事故原因概率；
* absolute accident risk / SORA GRC / ARC 一律 ``not_computed``；
* 适飞空域仍是 ``display_only``，不进入任何数值或 fingerprint；
* ``artifact_type`` 明确是 ``layered_route_candidate``：未来 reference/operational adapter
  可以复用同一画像契约，而不会把 target 写死成 operational route。
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

from .risk_v2 import (
    DOMAIN_FACTOR_IDS, DOMAIN_IDS, DOMAIN_LABELS, FACTOR_DEFINITIONS, FACTOR_IDS,
    NO_SOURCE_FACTOR_IDS, display_only_airspace, empty_absolute_risk, empty_sora_arc,
    empty_sora_grc, stable_fingerprint,
)

SCHEMA_VERSION = "route-risk-profile-v1"

#: 画像的 target 类型。本轮只分析 Layered Route Planner candidate；未来 adapter 可扩展。
ARTIFACT_TYPE = "layered_route_candidate"

ALGORITHM_ID = "route_risk_profile_v1"
ALGORITHM_VERSION = "1.0"

#: 画像状态词表。``inconsistent_evidence`` 表示一致性 gate 失败：绝不静默保存。
PROFILE_STATUSES = (
    "not_calculated", "missing_data", "blocked", "stale", "not_ready",
    "inconsistent_evidence", "passed",
)

CLASSIFICATION_LEVELS = ("low", "medium", "high")

POLICY_PENDING_SOURCE = "未配置；RouteRiskProfile 阈值必须由项目工程依据显式确认"

#: 单个 domain 的阈值语义。
THRESHOLD_SEMANTICS = {
    "per_domain_only": True,
    "no_default_thresholds": True,
    "null_is_not_zero": True,
    "confirmed_requires_explicit_source": True,
    "cross_domain_overall_not_generated": True,
    "classification_requires_confirmed_thresholds": True,
    "bounds": [0.0, 1.0],
}

PROFILE_SEMANTICS = {
    "artifact_type": ARTIFACT_TYPE,
    "risk_semantics": "relative_engineering_index",
    "not_accident_probability": True,
    "not_sora_grc": True,
    "not_sora_arc": True,
    "not_absolute_safety_risk": True,
    "analysis_only_no_replanning": True,
    "does_not_modify_candidate": True,
    "never_writes_operational_routes_or_cns": True,
    "never_creates_route_operating_layer": True,
    "missing_or_unknown_is_never_zero": True,
    "unresolved_length_is_preserved": True,
    "classification_is_per_domain_only": True,
    "no_cross_domain_overall_or_high_risk": True,
    "factor_contribution_is_relative_engineering_contribution_not_accident_cause_probability": True,
    "risk_v2_overall_not_used": True,
    "profile_policy_thresholds_have_no_default": True,
    "airspace_is_display_only": True,
}


# --------------------------------------------------------------------------- helpers


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _text(value, default=""):
    text = str(value or "").strip()
    return text or default


def _optional_text(value):
    return str(value or "").strip() or None


# --------------------------------------------------------------------------- policy


def default_route_risk_profile_domain(domain_id):
    return {
        "domain_id": domain_id,
        "medium_min": None,
        "high_min": None,
        "source": POLICY_PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "status": "not_configured",
        "status_reason": "thresholds_not_configured",
        "parameter_status": "no_default_thresholds",
        "bounds": [0.0, 1.0],
        "label": DOMAIN_LABELS.get(domain_id),
        "semantics": deepcopy(THRESHOLD_SEMANTICS),
    }


def default_route_risk_profile_policy():
    """RouteRiskProfilePolicy：没有任何默认阈值。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_configured",
        "parameter_status": "no_default_thresholds",
        "domains": {
            domain_id: default_route_risk_profile_domain(domain_id) for domain_id in DOMAIN_IDS
        },
        "semantics": deepcopy(THRESHOLD_SEMANTICS),
        "notes": [
            "阈值只能按 domain 显式确认；未确认时 classification / high-risk 指标为 "
            "not_configured / null，而不是 0.6 / 0.8 之类的猜测值。",
            "profile 的 exposure / mean / max 不依赖阈值，未确认阈值时依然计算。",
        ],
    }


def normalize_route_risk_profile_domain(domain_id, value):
    """校验单个 domain 的阈值；非法阈值直接拒绝，不做任何自动修正。"""

    source = value if isinstance(value, dict) else {}
    result = default_route_risk_profile_domain(domain_id)
    medium = _optional_number(source.get("medium_min"), f"{domain_id}.medium_min")
    high = _optional_number(source.get("high_min"), f"{domain_id}.high_min")
    source_text = _text(source.get("source"), POLICY_PENDING_SOURCE)
    explicit_source = source_text != POLICY_PENDING_SOURCE
    evidence = source.get("evidence")
    result.update({
        "medium_min": medium,
        "high_min": high,
        "source": source_text,
        "evidence": deepcopy(evidence) if evidence not in (None, "") else None,
        "confirmed": bool(source.get("confirmed", False)),
    })
    if medium is None and high is None:
        result["status"] = "not_configured"
        result["status_reason"] = "thresholds_not_configured"
        return result
    if medium is None or high is None:
        raise ValueError(
            f"{domain_id} 的 medium_min 与 high_min 必须同时显式提供（"
            f"medium_min={medium!r}, high_min={high!r}）"
        )
    if not (0.0 <= medium <= high <= 1.0):
        raise ValueError(
            f"{domain_id} 阈值必须满足 0 <= medium_min <= high_min <= 1"
            f"（medium_min={medium!r}, high_min={high!r}）"
        )
    if not result["confirmed"]:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "thresholds_not_confirmed"
        return result
    if not explicit_source:
        raise ValueError(f"{domain_id} 声明 confirmed 时必须提供显式 source")
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def normalize_route_risk_profile_policy(value):
    """幂等归一化；没有 confirmed 阈值的 domain 保持 not_configured。"""

    source = value if isinstance(value, dict) else {}
    domains_source = source.get("domains") if isinstance(source.get("domains"), dict) else {}
    domains = {
        domain_id: normalize_route_risk_profile_domain(domain_id, domains_source.get(domain_id))
        for domain_id in DOMAIN_IDS
    }
    result = default_route_risk_profile_policy()
    result["domains"] = domains
    statuses = {item["status"] for item in domains.values()}
    result["status"] = (
        "confirmed" if statuses == {"confirmed"}
        else "not_configured" if statuses == {"not_configured"}
        else "pending_confirmation"
    )
    return result


def domain_threshold_fingerprint(domain_id, domain_policy):
    policy = domain_policy if isinstance(domain_policy, dict) else {}
    return stable_fingerprint(
        {
            "domain_id": domain_id,
            "medium_min": policy.get("medium_min"),
            "high_min": policy.get("high_min"),
            "confirmed": bool(policy.get("confirmed")),
            "status": policy.get("status"),
            "source": policy.get("source"),
            "evidence": policy.get("evidence"),
        },
        prefix="routeprofiledomainpolicyv1-",
    )


def route_risk_profile_policy_fingerprint(policy):
    """policy fingerprint 覆盖阈值、确认状态与来源（evidence 变化也视为配置变化）。"""

    normalized = normalize_route_risk_profile_policy(policy)
    return stable_fingerprint(
        {
            "status": normalized["status"],
            "domains": {
                domain_id: {
                    "medium_min": item.get("medium_min"),
                    "high_min": item.get("high_min"),
                    "confirmed": bool(item.get("confirmed")),
                    "status": item.get("status"),
                    "source": item.get("source"),
                    "evidence": item.get("evidence"),
                }
                for domain_id, item in normalized["domains"].items()
            },
        },
        prefix="routeprofilepolicyv1-",
    )


def classify_domain_index(index, domain_policy):
    """按 **单一 domain** 的已确认阈值分类 ``low`` / ``medium`` / ``high``。"""

    policy = domain_policy if isinstance(domain_policy, dict) else {}
    domain_id = policy.get("domain_id")
    medium, high = policy.get("medium_min"), policy.get("high_min")
    classification = {
        "domain_id": domain_id,
        "status": "not_configured",
        "level": None,
        "index": float(index) if _finite(index) else None,
        "thresholds": {"medium_min": medium, "high_min": high},
        "policy_fingerprint": domain_threshold_fingerprint(domain_id, policy),
        "semantics": "per_domain_classification_only",
    }
    if str(policy.get("status") or "") != "confirmed" or not _finite(medium) or not _finite(high):
        classification["reason"] = str(
            policy.get("status_reason") or "thresholds_not_configured"
        )
        classification["high_risk_metrics"] = "not_configured"
        return classification
    if not _finite(index):
        classification["status"] = "not_available"
        classification["reason"] = "index_not_resolved"
        classification["high_risk_metrics"] = "not_available"
        return classification
    value = float(index)
    classification["status"] = "passed"
    classification["level"] = (
        "low" if value < float(medium) else "medium" if value < float(high) else "high"
    )
    classification["reason"] = None
    classification["high_risk_metrics"] = "available"
    return classification


# --------------------------------------------------------------------------- result contract


def empty_domain_profile(domain_id, status="not_calculated"):
    return {
        "domain_id": domain_id,
        "label": DOMAIN_LABELS.get(domain_id),
        "status": status,
        "active_cost_domain": False,
        "exposure_index_m": None,
        "mean_index": None,
        "max_index": None,
        "max_location": None,
        "resolved_length_m": 0.0,
        "unresolved_length_m": 0.0,
        "coverage": None,
        "resolved_segment_count": 0,
        "unresolved_segment_count": 0,
        "unresolved_cells": [],
        "classification": {
            "domain_id": domain_id,
            "status": "not_configured",
            "level": None,
            "index": None,
            "reason": "thresholds_not_configured",
            "thresholds": {"medium_min": None, "high_min": None},
            "policy_fingerprint": None,
            "high_risk_metrics": "not_configured",
            "semantics": "per_domain_classification_only",
        },
        "high_risk": {
            "status": "not_configured",
            "length_m": None,
            "interval_count": None,
            "intervals": None,
            "reason": "thresholds_not_configured",
        },
        "contributors": {},
        "provenance": {},
        "semantics": {
            "domain_id": domain_id,
            "relative_engineering_index": True,
            "not_accident_probability": True,
            "not_sora_grc": True,
            "not_sora_arc": True,
            "missing_is_not_zero": True,
            "unresolved_length_preserved": True,
        },
    }


def empty_factor_profile(factor_id):
    definition = FACTOR_DEFINITIONS[factor_id]
    return {
        "factor_id": factor_id,
        "domain": definition["domain"],
        "raw_unit": definition.get("raw_unit"),
        "label": definition.get("label"),
        "canonical_source_available": factor_id not in NO_SOURCE_FACTOR_IDS,
        "status": "not_calculated",
        "resolved": False,
        "resolved_length_m": 0.0,
        "unresolved_length_m": 0.0,
        "coverage": None,
        "raw_exposure": None,
        "normalized_exposure_index_m": None,
        "weighted_contribution_index_m": None,
        "weight": None,
        "contribution_status": "not_available",
        "contributor_rank": None,
        "contributor_semantics": (
            "relative_engineering_contribution_not_accident_cause_probability"
        ),
        "source_ids": [],
        "source_fingerprints": [],
        "normalization_reference_fingerprints": [],
        "provenance": {},
    }


def empty_route_risk_profile(status="not_calculated", *, candidate_id=None, route_id=None,
                             altitude_layer_id=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": None,
        "artifact_type": ARTIFACT_TYPE,
        "status": status,
        "status_reason": None,
        "blocking_reasons": [],
        "candidate": {
            "candidate_id": candidate_id,
            "status": None,
            "route_id": route_id,
            "altitude_layer_id": altitude_layer_id,
            "lane_key": None,
            "grid_level": None,
            "candidate_fingerprint": None,
            "input_fingerprint": None,
            "risk_fingerprint": None,
            "feasibility_mask_fingerprint": None,
            "current_applicability": None,
        },
        "route": {
            "route_id": route_id,
            "altitude_layer_id": altitude_layer_id,
            "grid_level": None,
            "cell_count": 0,
            "grid_path": [],
            "path_fingerprint": None,
        },
        "layer": {"altitude_layer_id": altitude_layer_id, "grid_level": None},
        "policy": default_route_risk_profile_policy(),
        "route_length_m": None,
        "domains": {
            domain_id: empty_domain_profile(domain_id) for domain_id in DOMAIN_IDS
        },
        "factors": {factor_id: empty_factor_profile(factor_id) for factor_id in FACTOR_IDS},
        "contributors": {
            "status": "not_available",
            "ranking": [],
            "reason": "risk_v2_aggregation_policy_has_no_confirmed_contributor_weight",
            "semantics": (
                "relative_engineering_contribution_not_accident_cause_probability"
            ),
        },
        "classification": {
            "status": "not_configured",
            "index_scope": "per_domain_only",
            "cross_domain_overall": "not_computed",
            "domains": {
                domain_id: {"domain_id": domain_id, "status": "not_configured", "level": None}
                for domain_id in DOMAIN_IDS
            },
            "semantics": deepcopy(THRESHOLD_SEMANTICS),
        },
        "high_risk": {
            "status": "not_configured",
            "index_scope": "per_domain_only",
            "cross_domain_high_risk": "not_computed",
            "domains": {
                domain_id: {
                    "domain_id": domain_id, "status": "not_configured",
                    "length_m": None, "interval_count": None, "intervals": None,
                }
                for domain_id in DOMAIN_IDS
            },
        },
        "segments": [],
        "connector_semantics": None,
        "consistency": {"status": "not_evaluated", "tolerance": None, "checks": []},
        "fingerprints": {
            "profile_fingerprint": None,
            "policy_fingerprint": None,
            "candidate_fingerprint": None,
            "path_fingerprint": None,
            "grid_risk_v2_input_fingerprint": None,
            "grid_risk_v2_policy_fingerprint": None,
            "grid_risk_v2_cells_fingerprint": None,
            "components": {},
        },
        "provenance": {},
        "not_computed": {
            "absolute_risk": empty_absolute_risk(),
            "sora_grc": empty_sora_grc(),
            "sora_arc": empty_sora_arc(),
        },
        "airspace": display_only_airspace(),
        "semantics": deepcopy(PROFILE_SEMANTICS),
        "notes": [
            "只分析 current LayeredRouteCandidate：不重规划、不修改 candidate、不写 "
            "operational_routes / CNS / RouteOperatingLayer。",
            "exposure / mean / max 与 planner 的 cost_breakdown 使用同一个 backend-only 积分 helper，"
            "因此数值一致；缺失 domain/factor 只累加 unresolved 长度，绝不补 0。",
            "classification 只在 per-domain 成立，且必须由工程显式确认阈值；"
            "未确认时 high-risk 指标为 not_configured / null。",
            "factor contributor 只是 relative engineering contribution，"
            "不是事故原因概率；absolute risk / SORA GRC / ARC 一律 not_computed。",
            "适飞空域是 display_only，不进入 profile 数值或 fingerprint。",
        ],
    }


def normalize_route_risk_profile(value):
    """幂等 backfill：旧记录缺字段时补默认值，但不改写已有结论。"""

    if not isinstance(value, dict):
        return empty_route_risk_profile()
    result = empty_route_risk_profile(str(value.get("status") or "not_calculated"))
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result["artifact_type"] = ARTIFACT_TYPE
    candidate = result.get("candidate")
    result["candidate"] = candidate if isinstance(candidate, dict) else {}
    for key, default in empty_route_risk_profile()["candidate"].items():
        result["candidate"].setdefault(key, default)
    route = result.get("route")
    result["route"] = route if isinstance(route, dict) else {}
    for key, default in empty_route_risk_profile()["route"].items():
        result["route"].setdefault(key, default)
    layer = result.get("layer")
    result["layer"] = layer if isinstance(layer, dict) else {}
    for key, default in empty_route_risk_profile()["layer"].items():
        result["layer"].setdefault(key, default)
    result["policy"] = normalize_route_risk_profile_policy(result.get("policy"))
    domains = result.get("domains") if isinstance(result.get("domains"), dict) else {}
    result["domains"] = {
        domain_id: _normalize_domain_profile(domain_id, domains.get(domain_id))
        for domain_id in DOMAIN_IDS
    }
    factors = result.get("factors") if isinstance(result.get("factors"), dict) else {}
    result["factors"] = {
        factor_id: _normalize_factor_profile(factor_id, factors.get(factor_id))
        for factor_id in FACTOR_IDS
    }
    for key in ("contributors", "classification", "high_risk", "consistency", "fingerprints",
                "provenance", "not_computed"):
        if not isinstance(result.get(key), dict):
            result[key] = deepcopy(empty_route_risk_profile()[key])
    result["segments"] = [item for item in result.get("segments") or [] if isinstance(item, dict)]
    result.setdefault("blocking_reasons", [])
    result.setdefault("connector_semantics", None)
    result.setdefault("airspace", display_only_airspace())
    result.setdefault("semantics", deepcopy(PROFILE_SEMANTICS))
    result.setdefault("notes", empty_route_risk_profile()["notes"])
    return result


def _normalize_domain_profile(domain_id, value):
    result = empty_domain_profile(domain_id)
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    for key, default in empty_domain_profile(domain_id).items():
        result.setdefault(key, default)
    result["domain_id"] = domain_id
    result["unresolved_cells"] = sorted({
        str(item) for item in result.get("unresolved_cells") or [] if str(item)
    })
    return result


def _normalize_factor_profile(factor_id, value):
    result = empty_factor_profile(factor_id)
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    for key, default in empty_factor_profile(factor_id).items():
        result.setdefault(key, default)
    result["factor_id"] = factor_id
    return result


def default_route_risk_profile_collection():
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_calculated",
        "count": 0,
        "artifact_type": ARTIFACT_TYPE,
        "items": [],
        "last_evaluation": None,
        "notes": [
            "独立 route_risk_profiles 容器：只保存 RouteRiskProfile，不写 operational_routes、"
            "CNS、RouteOperatingLayer 或任何 legacy 结果。",
            "旧 profile 在输入变化后保留为 stale 审计证据，不删除、不覆盖。",
        ],
    }


def normalize_route_risk_profile_collection(value):
    if not isinstance(value, dict):
        return default_route_risk_profile_collection()
    result = default_route_risk_profile_collection()
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result["artifact_type"] = ARTIFACT_TYPE
    items = [item for item in result.get("items") or [] if isinstance(item, dict)]
    result["items"] = [normalize_route_risk_profile(item) for item in items]
    result["count"] = len(result["items"])
    last = result.get("last_evaluation")
    result["last_evaluation"] = last if isinstance(last, dict) else None
    result["status"] = str(
        result.get("status") or ("passed" if result["items"] else "not_calculated")
    )
    result.setdefault("notes", default_route_risk_profile_collection()["notes"])
    return result


# --------------------------------------------------------------------------- fingerprints

#: profile fingerprint 的封闭组件集合。绝不含 airspace。
ROUTE_RISK_PROFILE_FINGERPRINT_COMPONENTS = (
    "artifact_type", "candidate_id", "candidate_fingerprint", "candidate_input_fingerprint",
    "candidate_risk_fingerprint", "route_id", "altitude_layer_id", "grid_level",
    "path_fingerprint", "grid_path", "grid_risk_v2_input_fingerprint",
    "grid_risk_v2_policy_fingerprint", "grid_risk_v2_cells_fingerprint",
    "profile_policy_fingerprint", "domain_policy_fingerprints",
    "factor_source_fingerprints", "algorithm",
)


def path_fingerprint(path, grid_path):
    return stable_fingerprint(
        {"path": list(path or []), "grid_path": list(grid_path or [])},
        prefix="routeprofilepathv1-",
    )


def profile_fingerprint(components):
    """Deterministic fingerprint over the declared components (never airspace)."""

    return stable_fingerprint(components, prefix="routeprofilev1-")


def grid_risk_v2_cells_fingerprint(grid_risk_v2):
    """当前 ``grid_risk_v2`` cell 内容的指纹（domain index + factor normalized index）。"""

    cells = (grid_risk_v2 or {}).get("cells")
    cells = cells if isinstance(cells, dict) else {}
    payload = {}
    for grid_id in sorted(cells):
        record = cells[grid_id] if isinstance(cells[grid_id], dict) else {}
        payload[str(grid_id)] = {
            "status": record.get("status"),
            "domains": {
                domain_id: {
                    "index": (record.get(domain_id) or {}).get("index")
                    if isinstance(record.get(domain_id), dict) else None,
                    "container_status": (
                        (record.get(domain_id) or {}).get("status")
                        if isinstance(record.get(domain_id), dict) else None
                    ),
                }
                for domain_id in DOMAIN_IDS
            },
            "factors": {
                factor_id: {
                    "status": (record.get("factors") or {}).get(factor_id, {}).get("status")
                    if isinstance((record.get("factors") or {}).get(factor_id), dict) else None,
                    "normalized_index": (
                        (record.get("factors") or {}).get(factor_id, {}).get("normalized_index")
                        if isinstance((record.get("factors") or {}).get(factor_id), dict) else None
                    ),
                }
                for factor_id in FACTOR_IDS
            },
        }
    return stable_fingerprint(payload, prefix="routeprofilegridriskv1-")


def factor_source_fingerprints(grid_risk_v2):
    """每 factor 的 source / normalization reference 指纹（来自 ``grid_risk_v2`` 摘要）。"""

    factor_status = (grid_risk_v2 or {}).get("factor_status")
    factor_status = factor_status if isinstance(factor_status, dict) else {}
    result = {}
    for factor_id in FACTOR_IDS:
        summary = factor_status.get(factor_id) if isinstance(factor_status.get(factor_id), dict) else {}
        result[factor_id] = {
            "source_id": summary.get("source_id"),
            "source_fingerprint": summary.get("source_fingerprint"),
            "normalization_reference_fingerprint": (
                (summary.get("normalization") or {}).get("reference_fingerprint")
                if isinstance(summary.get("normalization"), dict) else None
            ),
        }
    return result


def domain_policy_fingerprints(policy):
    normalized = normalize_route_risk_profile_policy(policy)
    return {
        domain_id: domain_threshold_fingerprint(domain_id, item)
        for domain_id, item in normalized["domains"].items()
    }


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "ARTIFACT_TYPE", "CLASSIFICATION_LEVELS",
    "DOMAIN_FACTOR_IDS", "DOMAIN_IDS", "POLICY_PENDING_SOURCE", "PROFILE_SEMANTICS",
    "PROFILE_STATUSES", "ROUTE_RISK_PROFILE_FINGERPRINT_COMPONENTS", "SCHEMA_VERSION",
    "THRESHOLD_SEMANTICS",
    "classify_domain_index", "default_route_risk_profile_collection",
    "default_route_risk_profile_domain", "default_route_risk_profile_policy",
    "domain_policy_fingerprints", "domain_threshold_fingerprint", "empty_domain_profile",
    "empty_factor_profile", "empty_route_risk_profile", "factor_source_fingerprints",
    "grid_risk_v2_cells_fingerprint", "normalize_route_risk_profile",
    "normalize_route_risk_profile_collection", "normalize_route_risk_profile_domain",
    "normalize_route_risk_profile_policy", "path_fingerprint", "profile_fingerprint",
    "route_risk_profile_policy_fingerprint",
]
