"""Canonical knowledge/planning contract for the existing CNS baseline."""

from __future__ import annotations

from copy import deepcopy
from enum import StrEnum

from .assumptions import find_active_assumption_for_field
from .workflow_contract import ReadinessState


class CNSExistingKnowledgeStatus(StrEnum):
    NOT_DECLARED = "not_declared"
    CONFIRMED_NONE = "confirmed_none"
    CONFIRMED_PRESENT = "confirmed_present"


class CNSExistingPlanningMode(StrEnum):
    FACTUAL = "factual"
    ASSUME_EMPTY_FOR_PLANNING = "assume_empty_for_planning"


EMPTY_BASELINE_REPORT_DISCLOSURE = (
    "空既有设施工程规划基线；不表示现实中不存在 CNS"
)


def empty_cns_existing_baseline() -> dict:
    return {
        "knowledge_status": CNSExistingKnowledgeStatus.NOT_DECLARED.value,
        "planning_mode": CNSExistingPlanningMode.FACTUAL.value,
        "source": None,
        "evidence_ref": None,
        "declared_at": None,
        "declared_by": None,
    }


def normalize_cns_existing_baseline(value: dict | None) -> dict:
    """Normalize without inferring facts from an facilities collection.

    Missing legacy state always becomes ``not_declared + factual``.  In
    particular, callers must not pass or inspect an empty facility list to
    infer ``confirmed_none``.
    """

    if value is None:
        return empty_cns_existing_baseline()
    if not isinstance(value, dict):
        raise ValueError("cns_existing_baseline 必须是对象")
    try:
        knowledge_status = CNSExistingKnowledgeStatus(
            value.get("knowledge_status") or "not_declared"
        ).value
    except ValueError as exc:
        raise ValueError("cns_existing_baseline.knowledge_status 无效") from exc
    try:
        planning_mode = CNSExistingPlanningMode(
            value.get("planning_mode") or "factual"
        ).value
    except ValueError as exc:
        raise ValueError("cns_existing_baseline.planning_mode 无效") from exc

    if knowledge_status == CNSExistingKnowledgeStatus.CONFIRMED_NONE.value:
        planning_mode = CNSExistingPlanningMode.FACTUAL.value

    return {
        "knowledge_status": knowledge_status,
        "planning_mode": planning_mode,
        "source": deepcopy(value.get("source")),
        "evidence_ref": deepcopy(value.get("evidence_ref")),
        "declared_at": value.get("declared_at"),
        "declared_by": value.get("declared_by"),
    }


def existing_cns_baseline_readiness(
    baseline: dict | None,
    *,
    facilities: dict | list | None = None,
    assumption_registry: dict | None = None,
) -> dict:
    normalized = normalize_cns_existing_baseline(baseline)
    knowledge = normalized["knowledge_status"]
    mode = normalized["planning_mode"]

    if knowledge == CNSExistingKnowledgeStatus.NOT_DECLARED.value:
        if mode == CNSExistingPlanningMode.FACTUAL.value:
            return _blocked("existing_cns_baseline_not_declared")
        assumption = find_active_assumption_for_field(
            assumption_registry, "cns_existing_baseline"
        )
        if assumption is None or assumption.get("value") != "empty":
            return _blocked("existing_cns_empty_planning_assumption_required")
        return {
            "state": ReadinessState.READY_WITH_ASSUMPTIONS.value,
            "blockers": [],
            "assumption_ids": [assumption["assumption_id"]],
            "warnings": [EMPTY_BASELINE_REPORT_DISCLOSURE],
        }

    if knowledge == CNSExistingKnowledgeStatus.CONFIRMED_NONE.value:
        return _ready()

    if mode == CNSExistingPlanningMode.ASSUME_EMPTY_FOR_PLANNING.value:
        return _blocked("confirmed_present_conflicts_with_assume_empty")

    if _has_resolvable_evidence(normalized.get("evidence_ref"), facilities):
        return _ready()
    return _blocked("confirmed_present_facilities_or_evidence_required")


def _has_resolvable_evidence(evidence_ref, facilities):
    if isinstance(evidence_ref, str) and evidence_ref.strip():
        return True
    if isinstance(evidence_ref, dict) and evidence_ref:
        return True
    if isinstance(evidence_ref, (list, tuple)) and evidence_ref:
        return True
    if isinstance(facilities, dict):
        items = facilities.get("items")
    else:
        items = facilities
    return isinstance(items, (list, tuple)) and any(
        isinstance(item, dict) and bool(item) for item in items
    )


def _blocked(reason):
    return {
        "state": ReadinessState.BLOCKED.value,
        "blockers": [reason],
        "assumption_ids": [],
        "warnings": [],
    }


def _ready():
    return {
        "state": ReadinessState.READY.value,
        "blockers": [],
        "assumption_ids": [],
        "warnings": [],
    }

