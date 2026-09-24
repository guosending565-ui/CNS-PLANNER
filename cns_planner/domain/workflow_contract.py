"""Phase4 canonical workflow vocabulary, separate from legacy result states.

``ResultStatus`` in :mod:`cns_planner.domain.status` remains the contract for
existing modules.  The enums and helpers here are additive and are used only
by the canonical workflow foundation until later migration batches opt in.
"""

from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from typing import Iterable


class InputRequirementLevel(StrEnum):
    REQUIRED = "required"
    ASSUMABLE = "assumable"
    OPTIONAL = "optional"
    ENHANCEMENT = "enhancement"


class WorkflowStatus(StrEnum):
    NOT_STARTED = "not_started"
    BLOCKED = "blocked"
    READY = "ready"
    READY_WITH_ASSUMPTIONS = "ready_with_assumptions"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    STALE = "stale"


class ReadinessState(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    READY_WITH_ASSUMPTIONS = "ready_with_assumptions"


class OutputMaturity(StrEnum):
    PROVISIONAL = "provisional"
    AUTHORITATIVE = "authoritative"


class AssessmentOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


def normalize_readiness(
    value: dict | None,
    *,
    active_assumption_ids: Iterable[str] | None = None,
) -> dict:
    """Return a strict, JSON-safe canonical readiness object.

    ``ready_with_assumptions`` is invalid without at least one referenced
    active assumption.  Supplying ``active_assumption_ids`` also rejects stale,
    superseded, withdrawn, expired, or unknown references.
    """

    if value is None:
        value = {
            "state": ReadinessState.BLOCKED,
            "blockers": ["not_evaluated"],
            "assumption_ids": [],
            "warnings": [],
        }
    if not isinstance(value, dict):
        raise ValueError("readiness 必须是对象")

    state = _enum_value(ReadinessState, value.get("state"), "readiness.state")
    blockers = _strings(value.get("blockers"), "readiness.blockers")
    assumption_ids = _strings(
        value.get("assumption_ids"), "readiness.assumption_ids"
    )
    warnings = _strings(value.get("warnings"), "readiness.warnings")

    if state == ReadinessState.BLOCKED.value and not blockers:
        raise ValueError("blocked readiness 必须声明 blocker")
    if state != ReadinessState.BLOCKED.value and blockers:
        raise ValueError("非 blocked readiness 不得包含 blocker")
    if state == ReadinessState.READY_WITH_ASSUMPTIONS.value and not assumption_ids:
        raise ValueError("ready_with_assumptions 必须引用 assumption_id")
    if state == ReadinessState.READY.value and assumption_ids:
        raise ValueError("ready readiness 不得引用 assumption_id")

    if active_assumption_ids is not None:
        active = {str(item) for item in active_assumption_ids}
        invalid = [item for item in assumption_ids if item not in active]
        if invalid:
            raise ValueError(f"readiness 引用了非 active assumption：{invalid[0]}")

    return {
        "state": state,
        "blockers": blockers,
        "assumption_ids": assumption_ids,
        "warnings": warnings,
    }


def normalize_node_summary(value: dict) -> dict:
    """Normalize the common canonical node summary without legacy projection."""

    if not isinstance(value, dict):
        raise ValueError("canonical node summary 必须是对象")
    node_id = str(value.get("node_id") or "").strip()
    if not node_id:
        raise ValueError("canonical node summary 缺少 node_id")
    result = {
        "node_id": node_id,
        "status": _enum_value(WorkflowStatus, value.get("status"), "status"),
        "output_maturity": _enum_value(
            OutputMaturity, value.get("output_maturity"), "output_maturity"
        ),
        "readiness": normalize_readiness(value.get("readiness")),
        "warnings": _strings(value.get("warnings"), "warnings"),
    }
    if value.get("assessment") is not None:
        assessment = value["assessment"]
        if not isinstance(assessment, dict):
            raise ValueError("assessment 必须是对象")
        result["assessment"] = {
            **deepcopy(assessment),
            "outcome": _enum_value(
                AssessmentOutcome, assessment.get("outcome"), "assessment.outcome"
            ),
        }
    return result


def _enum_value(enum_type, value, field):
    try:
        return enum_type(value).value
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field} 必须是：{allowed}") from exc


def _strings(value, field):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} 必须是数组")
    result = []
    for raw in value:
        item = str(raw or "").strip()
        if not item:
            raise ValueError(f"{field} 不得包含空值")
        if item not in result:
            result.append(item)
    return result

