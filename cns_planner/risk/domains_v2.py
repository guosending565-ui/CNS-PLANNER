"""Risk Framework V2 domain aggregation layer.

Aggregation is deliberately dumb and explicit:

* no default production weight exists, so a policy that is not ``confirmed``
  yields ``index = null`` with ``status = pending_confirmation`` (never a fake 0);
* a missing factor that carries a declared weight makes the domain
  ``unresolved`` — available weights are **never** re-normalized;
* a required factor that is missing is reported in ``unresolved``;
* weights are never inferred, and the domain never consumes airspace.
"""

from __future__ import annotations

from copy import deepcopy
import math

from ..domain.risk_v2 import (
    DOMAIN_FACTOR_IDS, DOMAIN_IDS, DOMAIN_LABELS,
    domain_aggregation_policy_fingerprint,
)
from .normalization import clip, finite

STATUS_NOT_CALCULATED = "not_calculated"
STATUS_PENDING = "pending_confirmation"
STATUS_UNRESOLVED = "unresolved"
STATUS_PASSED = "passed"
STATUS_STALE = "stale"


def empty_domain_result(domain_id, status=STATUS_NOT_CALCULATED):
    return {
        "domain_id": domain_id,
        "status": status,
        "index": None,
        "level": None,
        "contributors": {},
        "aggregation_policy_fingerprint": None,
        "data_completeness": 0.0,
        "unresolved": [],
        "required_factors": [],
        "semantics": domain_semantics(domain_id),
        "reason": None,
    }


def domain_semantics(domain_id):
    semantics = {
        "domain_id": domain_id,
        "label": DOMAIN_LABELS.get(domain_id),
        "relative_engineering_index": True,
        "not_accident_probability": True,
        "not_sora_grc": True,
        "not_sora_arc": True,
        "not_absolute_safety_risk": True,
        "missing_is_not_zero": True,
        "no_automatic_renormalization": True,
        "required_factor_missing_is_unresolved": True,
        "clearance_breach_is_feasibility_not_risk": True,
    }
    if domain_id == "air_traffic":
        semantics["uav_traffic_is_air_traffic_not_ground_transport"] = True
    if domain_id == "environment_obstacle":
        semantics["engineering_internal_domain"] = True
        semantics["not_sora_arc"] = True
        semantics["building_coverage_is_not_sheltering"] = True
        semantics["hard_clearance_separate_from_environment_risk"] = True
    return semantics


def _level_for(index, levels):
    if not finite(index) or not isinstance(levels, (list, tuple)):
        return None
    value = float(index)
    for item in levels:
        if not isinstance(item, dict):
            continue
        lower, upper = item.get("min"), item.get("max")
        if not finite(lower) or not finite(upper):
            continue
        if float(lower) <= value < float(upper) or item.get("include_max") and value == float(upper):
            return item.get("level")
    return None


def aggregate_domain(domain_id, factor_records, domain_policy, *, levels=None):
    """Aggregate one cell's factors into a domain result.

    ``factor_records`` maps ``factor_id`` to the per-cell factor record produced
    by :mod:`cns_planner.risk.factors_v2`.
    """

    policy = domain_policy if isinstance(domain_policy, dict) else {}
    weights = dict(policy.get("weights") or {})
    required = list(policy.get("required_factors") or [])
    result = empty_domain_result(domain_id)
    result["aggregation_policy_fingerprint"] = domain_aggregation_policy_fingerprint(
        domain_id, policy,
    )
    result["required_factors"] = list(required)
    result["policy_status"] = policy.get("status") or STATUS_PENDING
    result["method"] = policy.get("method")

    contributors = {}
    records = factor_records or {}
    for factor_id in DOMAIN_FACTOR_IDS[domain_id]:
        record = records.get(factor_id) or {}
        weight = weights.get(factor_id)
        status = record.get("status") or "not_available"
        index = record.get("normalized_index")
        included = weight is not None and status == STATUS_PASSED and finite(index)
        contributors[factor_id] = {
            "factor_id": factor_id,
            "required": factor_id in required,
            "weight": float(weight) if finite(weight) else None,
            "status": status,
            "resolved": bool(record.get("resolved")),
            "normalized_index": float(index) if finite(index) else None,
            "contribution": (float(weight) * float(index)) if included else None,
            "included": bool(included),
            "reason": record.get("reason"),
        }
    result["contributors"] = contributors

    weighted = [item for item in contributors.values() if item["weight"] is not None]
    total_weight = math.fsum(item["weight"] for item in weighted)
    available_weight = math.fsum(item["weight"] for item in weighted if item["included"])
    result["data_completeness"] = (
        available_weight / total_weight if total_weight > 0 else 0.0
    )
    unresolved = [item["factor_id"] for item in weighted if not item["included"]]
    missing_required = [factor_id for factor_id in required if factor_id in unresolved]
    result["unresolved"] = unresolved
    result["missing_required_factors"] = missing_required

    if result["policy_status"] != "confirmed":
        result["status"] = STATUS_PENDING
        result["index"] = None
        result["reason"] = "aggregation_policy_pending_confirmation"
        return result
    if any(item["status"] == STATUS_STALE for item in weighted):
        result["status"] = STATUS_STALE
        result["index"] = None
        result["reason"] = "input_stale"
        return result
    if unresolved:
        result["status"] = STATUS_UNRESOLVED
        result["index"] = None
        result["reason"] = (
            "missing_required_factors" if missing_required
            else "missing_weighted_factor_not_renormalized"
        )
        return result
    index = clip(math.fsum(item["contribution"] for item in weighted))
    result.update({
        "status": STATUS_PASSED,
        "index": index,
        "level": _level_for(index, levels),
        "reason": None,
    })
    return result


def aggregate_domains(factor_records, policy, *, levels_by_domain=None):
    """Aggregate every domain for one cell.  ``factor_records`` is factor-keyed."""

    domains = {}
    for domain_id in DOMAIN_IDS:
        domains[domain_id] = aggregate_domain(
            domain_id, factor_records, (policy or {}).get("domains", {}).get(domain_id),
            levels=(levels_by_domain or {}).get(domain_id),
        )
    return domains


def domain_summary(domain_id, cell_domains, contributors_policy):
    """Cross-cell summary for one domain.

    The summary never invents a cross-cell average index: the relative index is
    a per-cell quantity, so ``index`` stays ``None`` and is scoped explicitly.
    """

    results = list(cell_domains.values())
    statuses = [item.get("status") for item in results]
    counts = {status: statuses.count(status) for status in sorted(set(statuses))}
    status = _summary_status(statuses)
    completeness = (
        math.fsum(float(item.get("data_completeness") or 0.0) for item in results) / len(results)
        if results else 0.0
    )
    unresolved = sorted({
        factor_id for item in results for factor_id in (item.get("unresolved") or [])
    })
    first = results[0] if results else empty_domain_result(domain_id)
    contributors = {}
    for factor_id in DOMAIN_FACTOR_IDS[domain_id]:
        sample = (first.get("contributors") or {}).get(factor_id) or {}
        contributors[factor_id] = {
            "factor_id": factor_id,
            "required": factor_id in ((contributors_policy or {}).get("required_factors") or []),
            "weight": sample.get("weight"),
            "cell_passed_count": sum(
                1 for item in results
                if ((item.get("contributors") or {}).get(factor_id) or {}).get("included")
            ),
            "cell_count": len(results),
        }
    return {
        "domain_id": domain_id,
        "status": status,
        "index": None,
        "index_scope": "per_cell_only",
        "level": None,
        "cell_status_counts": counts,
        "contributors": contributors,
        "aggregation_policy_fingerprint": first.get("aggregation_policy_fingerprint"),
        "data_completeness": completeness,
        "unresolved": unresolved,
        "required_factors": first.get("required_factors") or [],
        "policy_status": first.get("policy_status"),
        "method": first.get("method"),
        "semantics": domain_semantics(domain_id),
        "reason": first.get("reason"),
    }


def _summary_status(statuses):
    if not statuses:
        return STATUS_NOT_CALCULATED
    unique = set(statuses)
    for candidate in (STATUS_STALE, STATUS_PENDING, STATUS_UNRESOLVED, STATUS_PASSED):
        if candidate in unique:
            return candidate
    return sorted(unique)[0]


__all__ = [
    "STATUS_NOT_CALCULATED", "STATUS_PASSED", "STATUS_PENDING", "STATUS_STALE",
    "STATUS_UNRESOLVED", "aggregate_domain", "aggregate_domains",
    "domain_semantics", "domain_summary", "empty_domain_result",
]
