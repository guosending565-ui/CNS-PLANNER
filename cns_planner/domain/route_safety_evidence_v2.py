"""Route Safety Evidence Assessment V2 — additive post-planning evidence aggregation.

This module is **not** a new risk algorithm.  It does not enter any Theta* cost, it does not
recompute the terrain/building validator, the Risk Framework V2 domains or the CNS models,
and it never produces a single "Safety Score".

It aggregates the evidence that already exists along one published operational lineage::

    Theta* Candidate -> RouteRiskProfile -> Layered Continuous Validation
      -> Operational Adoption -> CNS assessment -> Route Safety Evidence V2

The four evidence domains are reported **separately**:

* ``geometry_obstacle``          — the existing ``LayeredRouteValidation`` verdict;
* ``ground_exposure``            — the Theta* population x shelter objective *and* the
  post-hoc ``RouteRiskProfile`` ground domain, deliberately never merged into one score;
* ``regulatory``                 — the formal ``regulatory_constraints`` dataset only (never
  the display-only airspace layer);
* ``cns_operational_support``    — the existing coverage / capability / gap (and, when
  current, corridor) results, reported per C/N/S.

Hard semantics, enforced here and in the service:

* ``unknown != safe``, ``missing_data != zero``;
* ``validated route != safe route``;
* ``CNS unmet != route unsafe``: a confirmed CNS gap is an *operational support deficit* and
  may never turn a passed geometry validation into a failure;
* ``low risk != safe``;
* ``regulatory not configured != passed``.

The overall status therefore only describes **evidence completeness and explicit hard
constraint results**.  It is never an accident probability, a fatality probability, a SORA
GRC/ARC, an automated safety level, or a safety score.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json


SCHEMA_VERSION = "route-safety-evidence-v2"
COLLECTION_SCHEMA_VERSION = "route-safety-evidence-v2-collection"
ALGORITHM_ID = "route_safety_evidence_v2"
ALGORITHM_VERSION = "2.0"

#: What this artifact is: evidence aggregation over a *published layered operational route*.
ARTIFACT_TYPE = "route_safety_evidence_for_published_layered_operational_adoption"

DOMAIN_IDS = (
    "geometry_obstacle", "ground_exposure", "regulatory", "cns_operational_support",
)

DOMAIN_LABELS = {
    "geometry_obstacle": "Geometry / Obstacle",
    "ground_exposure": "Ground Exposure",
    "regulatory": "Regulatory",
    "cns_operational_support": "CNS Operational Support",
}

#: Overall evidence status vocabulary.  There is deliberately no ``safe``/``unsafe`` here.
ASSESSMENT_STATUSES = (
    "evidence_complete", "evidence_incomplete", "evidence_unresolved",
    "hard_constraint_failed", "not_ready", "stale",
)

#: Per-domain status vocabularies.  ``unknown``/``unresolved`` are never collapsed into ok.
#: ``not_ready`` is valid for every domain: it means "this evidence domain was not reached
#: because the lineage itself is missing", which is neither a pass nor a failure.
DOMAIN_STATUSES = {
    "geometry_obstacle": (
        "validated", "failed", "unresolved", "validation_incomplete", "not_ready", "stale",
    ),
    "ground_exposure": ("assessed", "unresolved", "not_calculated", "not_ready", "stale"),
    "regulatory": (
        "evaluated_no_confirmed_intersection", "blocked_by_confirmed_constraint",
        "not_configured", "not_evaluated", "unresolved", "not_ready", "stale",
    ),
    "cns_operational_support": (
        "supported", "operational_support_deficit", "unknown", "not_run", "not_ready", "stale",
    ),
}

#: The status of a domain that was never reached.
DEFAULT_DOMAIN_STATUS = "not_ready"

CNS_SUBSYSTEMS = ("C", "N", "S")

#: The only two things that may set ``hard_constraint_failed``.
HARD_CONSTRAINT_DOMAINS = ("geometry_obstacle", "regulatory")

#: Statuses that mean "evaluation was attempted but evidence is unknown/unresolved".
UNRESOLVED_DOMAIN_STATUSES = {
    "geometry_obstacle": ("unresolved", "validation_incomplete"),
    "ground_exposure": ("unresolved",),
    "regulatory": ("unresolved",),
    "cns_operational_support": ("unknown",),
}

#: Statuses that mean "this evidence domain was not evaluated at all".
INCOMPLETE_DOMAIN_STATUSES = {
    "geometry_obstacle": ("not_ready",),
    "ground_exposure": ("not_calculated",),
    "regulatory": ("not_configured", "not_evaluated"),
    "cns_operational_support": ("not_run",),
}

SEMANTICS = {
    "post_planning_evidence_aggregation": True,
    "not_a_new_risk_algorithm": True,
    "not_used_in_theta_star_cost": True,
    "not_a_safety_score": True,
    "no_overall_safety_score_or_ranking": True,
    "no_accident_or_fatality_probability": True,
    "no_sora_grc_or_arc": True,
    "no_automatic_safety_level": True,
    "unknown_is_not_safe": True,
    "missing_data_is_not_zero": True,
    "validated_route_is_not_a_safe_route": True,
    "cns_unmet_is_not_route_unsafe": True,
    "low_risk_is_not_safe": True,
    "regulatory_not_configured_is_not_passed": True,
    "display_only_airspace_never_used": True,
    "never_modifies_upstream_results": True,
    "overall_status_is_evidence_completeness_only": True,
}

#: Executable statement of the "CNS gap != unsafe" boundary.
CNS_GAP_SEMANTICS = {
    "confirmed_cns_gap_is_operational_support_deficit": True,
    "confirmed_cns_gap_is_not_route_unsafe": True,
    "confirmed_cns_gap_never_fails_geometry_validation": True,
    "confirmed_cns_gap_never_sets_hard_constraint_failed": True,
    "capability_meets_is_not_runtime_availability": True,
    "coverage_geometry_is_not_propagation": True,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_fingerprint(value, *, prefix=""):
    return prefix + sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        default=str,
    ).encode()).hexdigest()


def assessment_fingerprint(components):
    """Fingerprint of the declared evidence dependency set of one assessment."""

    return stable_fingerprint(components, prefix="routesafetyevidencev2-")


def empty_domain(domain_id, status=None):
    if domain_id not in DOMAIN_IDS:
        raise ValueError(f"未知 route safety evidence domain：{domain_id!r}")
    resolved = status or DEFAULT_DOMAIN_STATUS
    if resolved not in DOMAIN_STATUSES[domain_id]:
        resolved = DEFAULT_DOMAIN_STATUS
    return {
        "domain_id": domain_id,
        "label": DOMAIN_LABELS[domain_id],
        "status": resolved,
        "status_reason": None,
        "evidence": {},
        "metrics": {},
        "sources": [],
        "limitations": [],
        "hard_constraint_failure": False,
        "used_in_overall_as": "evidence_state_only",
        "semantics": {},
    }


def empty_route_safety_evidence_v2(status="not_ready"):
    if status not in ASSESSMENT_STATUSES:
        status = "not_ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "algorithm_id": ALGORITHM_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "assessment_id": None,
        "route_id": None,
        "adoption_id": None,
        "status": status,
        "status_reason": None,
        "current_applicability": "not_evaluated",
        "stale_reason": None,
        "created_at": None,
        "lineage": {
            "status": "not_resolved",
            "operational_route": None,
            "adoption_id": None,
            "validation_id": None,
            "candidate_id": None,
            "route_risk_profile_id": None,
            "altitude_layer_id": None,
            "missing_links": [],
            "stale_links": [],
            "complete": False,
            "semantics": {
                "lineage_must_be_complete_to_be_current": True,
                "incomplete_lineage_is_never_reported_as_current": True,
            },
        },
        "domains": {domain_id: empty_domain(domain_id) for domain_id in DOMAIN_IDS},
        "evidence_summary": {
            "status": status,
            "hard_constraint_domains": [],
            "unresolved_domains": [],
            "incomplete_domains": [],
            "domains": {domain_id: None for domain_id in DOMAIN_IDS},
            "statement": (
                "该状态表示证据完整性/明确硬约束结果，不是自动安全认证或安全评分。"
            ),
            "is_safety_certification": False,
            "produces_safety_score": False,
            "produces_safety_ranking": False,
            "cns_gap_is_operational_support_deficit_not_route_unsafe": True,
        },
        "limitations": [],
        "fingerprints": {
            "assessment_fingerprint": None,
            "components": {},
            "operational_route_adoption_fingerprint": None,
            "validation_fingerprint": None,
            "candidate_fingerprint": None,
            "route_risk_profile_fingerprint": None,
            "regulatory_dataset_fingerprint": None,
            "coverage_3d_fingerprint": None,
            "cns_service_capability_fingerprint": None,
            "cns_gap_v2_fingerprint": None,
            "cns_corridor_fingerprint": None,
            "route_3d_profile_fingerprint": None,
            "route_3d_profile_id": None,
            "route_3d_profile_applicability": None,
            "terminal_transition_validation": None,
            "evaluator_version": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        },
        "provenance": {
            "evaluator": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
            "consumer_of": [
                "layered_operational_adoptions",
                "layered_route_validations",
                "layered_route_candidates",
                "route_risk_profiles",
                "regulatory_constraints",
                "coverage_3d",
                "cns_service_capability",
                "cns_gap_analysis_v2",
                "cns_corridor_assessment",
            ],
            "upstream_modified": False,
            "recomputed_upstream_algorithms": [],
            "display_only_airspace_used": False,
            "notes": [
                "V2 是 post-planning evidence aggregation：不进入 Theta* cost，不重算风险数学，"
                "不自动重新规划，不自动 adopt。",
                "四个 domain 分开报告，绝不合成 overall risk/safety score。",
            ],
        },
        "semantics": deepcopy(SEMANTICS),
        "cns_gap_semantics": deepcopy(CNS_GAP_SEMANTICS),
    }


def normalize_route_safety_evidence_v2(value):
    """Idempotent backfill: never rewrites an existing conclusion."""

    if not isinstance(value, dict):
        return empty_route_safety_evidence_v2()
    result = empty_route_safety_evidence_v2(str(value.get("status") or "not_ready"))
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result["artifact_type"] = ARTIFACT_TYPE
    result["algorithm_id"] = ALGORITHM_ID
    result["algorithm_version"] = ALGORITHM_VERSION
    if result.get("status") not in ASSESSMENT_STATUSES:
        result["status"] = "not_ready"
    domains = result.get("domains")
    if not isinstance(domains, dict):
        domains = {}
    for domain_id in DOMAIN_IDS:
        current = domains.get(domain_id)
        base = empty_domain(domain_id)
        if isinstance(current, dict):
            base.update(deepcopy(current))
            if base.get("status") not in DOMAIN_STATUSES[domain_id]:
                base["status"] = DEFAULT_DOMAIN_STATUS
        base["domain_id"] = domain_id
        base["label"] = DOMAIN_LABELS[domain_id]
        domains[domain_id] = base
    result["domains"] = domains
    result.setdefault("semantics", {}).update(SEMANTICS)
    result["cns_gap_semantics"] = deepcopy(CNS_GAP_SEMANTICS)
    result.setdefault("evidence_summary", empty_route_safety_evidence_v2()["evidence_summary"])
    result.setdefault("fingerprints", empty_route_safety_evidence_v2()["fingerprints"])
    result.setdefault("lineage", empty_route_safety_evidence_v2()["lineage"])
    result["provenance"] = {
        **empty_route_safety_evidence_v2()["provenance"],
        **(result.get("provenance") if isinstance(result.get("provenance"), dict) else {}),
    }
    return result


def empty_route_safety_evidence_v2_collection():
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "status": "not_calculated",
        "count": 0,
        "active_assessment_id": None,
        "items": [],
        "notes": [
            "Route Safety Evidence V2 只聚合已发布 layered operational adoption 的既有证据。",
            "不产生 safety score、排名、事故概率、SORA GRC/ARC 或自动安全等级。",
            "CNS confirmed gap 记为 operational support deficit，不代表 route unsafe。",
        ],
    }


def normalize_route_safety_evidence_v2_collection(value=None):
    source = value if isinstance(value, dict) else {}
    items = [
        normalize_route_safety_evidence_v2(item)
        for item in source.get("items") or [] if isinstance(item, dict)
    ]
    result = empty_route_safety_evidence_v2_collection()
    result.update(deepcopy(source))
    result["schema_version"] = COLLECTION_SCHEMA_VERSION
    result["items"] = items
    result["count"] = len(items)
    known = {item.get("assessment_id") for item in items}
    if result.get("active_assessment_id") not in known:
        result["active_assessment_id"] = None
    if not items:
        result["status"] = "not_calculated"
    return result


def overall_status_for(domain_statuses):
    """The overall **evidence** status of one assessment.

    ``domain_statuses`` maps each declared domain id to its status string.  The rules are
    intentionally conservative and never express "safe"/"unsafe":

    ``hard_constraint_failed``
        only for an explicit hard result — a failed terrain/building clearance validation,
        or a confirmed regulatory constraint violation;
    ``stale``
        any domain whose evidence is no longer current;
    ``evidence_unresolved``
        evaluation was attempted but unknown/unresolved evidence remains;
    ``evidence_incomplete``
        some declared domain was not evaluated at all (e.g. regulatory not configured, CNS
        never run) — this is *not* a pass;
    ``evidence_complete``
        every declared domain was evaluated.  A confirmed CNS gap still counts as complete
        evidence, because "evidence is complete" is not "everything is satisfied".
    """

    statuses = {
        domain_id: str((domain_statuses or {}).get(domain_id) or "")
        for domain_id in DOMAIN_IDS
    }
    if all(
        statuses[domain_id] == "not_ready" for domain_id in DOMAIN_IDS
    ):
        return "not_ready"
    if any(statuses[domain_id] == "stale" for domain_id in DOMAIN_IDS):
        return "stale"
    if statuses["geometry_obstacle"] == "failed":
        return "hard_constraint_failed"
    if statuses["regulatory"] == "blocked_by_confirmed_constraint":
        return "hard_constraint_failed"
    if any(
        statuses[domain_id] in UNRESOLVED_DOMAIN_STATUSES[domain_id]
        for domain_id in DOMAIN_IDS
    ):
        return "evidence_unresolved"
    if any(
        statuses[domain_id] in INCOMPLETE_DOMAIN_STATUSES[domain_id]
        for domain_id in DOMAIN_IDS
    ):
        return "evidence_incomplete"
    return "evidence_complete"


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "ARTIFACT_TYPE", "ASSESSMENT_STATUSES",
    "CNS_GAP_SEMANTICS", "CNS_SUBSYSTEMS", "COLLECTION_SCHEMA_VERSION",
    "DEFAULT_DOMAIN_STATUS", "DOMAIN_IDS",
    "DOMAIN_LABELS", "DOMAIN_STATUSES", "HARD_CONSTRAINT_DOMAINS",
    "INCOMPLETE_DOMAIN_STATUSES", "SCHEMA_VERSION", "SEMANTICS",
    "UNRESOLVED_DOMAIN_STATUSES",
    "assessment_fingerprint", "empty_domain", "empty_route_safety_evidence_v2",
    "empty_route_safety_evidence_v2_collection", "normalize_route_safety_evidence_v2",
    "normalize_route_safety_evidence_v2_collection", "overall_status_for",
    "stable_fingerprint", "utc_now",
]
