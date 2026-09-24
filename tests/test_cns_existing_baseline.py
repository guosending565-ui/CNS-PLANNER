from cns_planner.domain.assumptions import add_assumption, supersede_assumption
from cns_planner.domain.cns_existing_baseline import (
    EMPTY_BASELINE_REPORT_DISCLOSURE,
    existing_cns_baseline_readiness,
    normalize_cns_existing_baseline,
)


def empty_planning_assumption():
    return {
        "assumption_id": "ASM-CNS-EMPTY",
        "scope": "project",
        "node_id": None,
        "field": "cns_existing_baseline",
        "value": "empty",
        "unit": None,
        "basis": "engineering_baseline",
        "reason": "既有设施事实尚未取得，先按空基线规划",
        "source_ref": None,
        "owner": "planner",
        "confirmed": True,
        "authority_effect": "allowed_with_disclosure",
        "report_disclosure": EMPTY_BASELINE_REPORT_DISCLOSURE,
        "created_at": "2026-09-24T00:00:00+00:00",
        "expires_at": None,
        "invalidates_on": ["existing-cns-source-change"],
        "status": "active",
    }


def test_not_declared_factual_is_blocked_and_empty_planning_needs_active_assumption():
    factual = normalize_cns_existing_baseline(None)
    assert factual == {
        "knowledge_status": "not_declared",
        "planning_mode": "factual",
        "source": None,
        "evidence_ref": None,
        "declared_at": None,
        "declared_by": None,
    }
    assert existing_cns_baseline_readiness(factual)["state"] == "blocked"

    assumed = {**factual, "planning_mode": "assume_empty_for_planning"}
    assert existing_cns_baseline_readiness(assumed)["state"] == "blocked"
    registry = add_assumption(None, empty_planning_assumption())
    readiness = existing_cns_baseline_readiness(
        assumed, assumption_registry=registry
    )
    assert readiness["state"] == "ready_with_assumptions"
    assert readiness["assumption_ids"] == ["ASM-CNS-EMPTY"]
    assert readiness["warnings"] == [EMPTY_BASELINE_REPORT_DISCLOSURE]

    inactive = supersede_assumption(registry, "ASM-CNS-EMPTY")
    assert existing_cns_baseline_readiness(
        assumed, assumption_registry=inactive
    )["state"] == "blocked"


def test_confirmed_none_is_ready_and_normalizes_to_factual():
    baseline = normalize_cns_existing_baseline({
        "knowledge_status": "confirmed_none",
        "planning_mode": "assume_empty_for_planning",
    })
    assert baseline["planning_mode"] == "factual"
    assert existing_cns_baseline_readiness(baseline)["state"] == "ready"


def test_confirmed_present_requires_resolvable_facilities_or_evidence():
    baseline = normalize_cns_existing_baseline({
        "knowledge_status": "confirmed_present",
        "planning_mode": "factual",
    })
    assert existing_cns_baseline_readiness(
        baseline, facilities={"items": []}
    )["state"] == "blocked"
    assert existing_cns_baseline_readiness(
        baseline, facilities={"items": [{"facility_id": "F-1"}]}
    )["state"] == "ready"
    assert existing_cns_baseline_readiness({
        **baseline, "planning_mode": "assume_empty_for_planning",
    }, facilities={"items": [{"facility_id": "F-1"}]})["state"] == "blocked"

