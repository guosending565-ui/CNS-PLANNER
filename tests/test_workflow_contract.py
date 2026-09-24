from copy import deepcopy

import pytest

from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.domain.status import ResultStatus
from cns_planner.domain.workflow_contract import (
    AssessmentOutcome,
    InputRequirementLevel,
    OutputMaturity,
    ReadinessState,
    WorkflowStatus,
    normalize_readiness,
)


class NoopGrid:
    def generate(self, bbox):
        return {"status": "passed", "cells": [], "bbox": bbox}


def test_canonical_enums_are_exact_and_legacy_result_status_is_unchanged():
    assert [item.value for item in InputRequirementLevel] == [
        "required", "assumable", "optional", "enhancement",
    ]
    assert [item.value for item in WorkflowStatus] == [
        "not_started", "blocked", "ready", "ready_with_assumptions",
        "running", "completed", "completed_with_warnings", "failed", "stale",
    ]
    assert [item.value for item in ReadinessState] == [
        "blocked", "ready", "ready_with_assumptions",
    ]
    assert [item.value for item in OutputMaturity] == [
        "provisional", "authoritative",
    ]
    assert [item.value for item in AssessmentOutcome] == [
        "passed", "failed", "unknown",
    ]
    assert ResultStatus.NOT_CALCULATED.value == "not_calculated"
    assert ResultStatus.PENDING_CONFIRMATION.value == "pending_confirmation"


def test_ready_with_assumptions_requires_active_references():
    value = {
        "state": "ready_with_assumptions",
        "blockers": [],
        "assumption_ids": ["ASM-1"],
        "warnings": ["engineering baseline"],
    }
    assert normalize_readiness(value, active_assumption_ids=["ASM-1"]) == value
    with pytest.raises(ValueError, match="非 active assumption"):
        normalize_readiness(value, active_assumption_ids=[])
    with pytest.raises(ValueError, match="必须引用 assumption_id"):
        normalize_readiness({**value, "assumption_ids": []})


def test_project_state_additive_backfill_preserves_legacy_state():
    project = blank_project({})
    assert set(("assumptions", "cns_existing_baseline", "canonical_workflow")) <= set(project)
    legacy = deepcopy(project)
    legacy_selection = deepcopy(legacy["algorithm_selection"])
    legacy_statuses = deepcopy(legacy["result_statuses"])
    for key in ("assumptions", "cns_existing_baseline", "canonical_workflow"):
        legacy.pop(key)
    legacy["existing_cns_facilities"] = {
        "status": "not_calculated", "count": 0, "items": [],
    }

    normalized = normalize_project(legacy, NoopGrid())

    assert normalized["algorithm_selection"] == legacy_selection
    assert normalized["result_statuses"] == legacy_statuses
    assert normalized["assumptions"]["items"] == []
    assert normalized["cns_existing_baseline"]["knowledge_status"] == "not_declared"
    assert normalized["cns_existing_baseline"]["planning_mode"] == "factual"
    assert set(normalized["canonical_workflow"]["nodes"]) == {
        "environment", "risk_field", "route_candidate", "route_validation",
        "operational_route", "required_cns", "coverage", "service_capability",
        "service_corridor", "capability_gap", "facility_plan", "plan_review", "report",
    }

