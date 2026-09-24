import pytest

from cns_planner.domain.assumptions import (
    add_assumption,
    empty_assumption_registry,
    read_assumption,
    read_assumptions,
    supersede_assumption,
    validate_active_assumption_ids,
    validate_assumption,
    validate_assumption_application,
    withdraw_assumption,
)
from cns_planner.domain.workflow_contract import AssessmentOutcome, InputRequirementLevel


def assumption(assumption_id="ASM-SHELTER", **overrides):
    value = {
        "assumption_id": assumption_id,
        "scope": "node",
        "node_id": "route_candidate",
        "field": "shelter_coefficient",
        "value": 1.0,
        "unit": None,
        "basis": "engineering_baseline",
        "reason": "真实遮蔽系数暂缺",
        "source_ref": None,
        "owner": "planner",
        "confirmed": True,
        "authority_effect": "allowed_with_disclosure",
        "report_disclosure": "采用遮蔽系数 1.0 工程基线；不是现场遮蔽事实",
        "created_at": "2026-09-24T00:00:00+00:00",
        "expires_at": None,
        "invalidates_on": ["shelter-source-change"],
        "status": "active",
    }
    value.update(overrides)
    return value


def test_add_read_supersede_and_withdraw_are_pure_and_status_aware():
    empty = empty_assumption_registry()
    added = add_assumption(empty, assumption())
    assert empty["items"] == []
    assert validate_assumption(assumption())["assumption_id"] == "ASM-SHELTER"
    assert read_assumption(added, "ASM-SHELTER")["value"] == 1.0
    assert read_assumption(added, "missing") is None
    assert read_assumptions(added, active_only=True)[0]["value"] == 1.0
    assert validate_active_assumption_ids(added, ["ASM-SHELTER"]) == ("ASM-SHELTER",)

    superseded = supersede_assumption(added, "ASM-SHELTER")
    assert read_assumptions(superseded, active_only=True) == []
    with pytest.raises(ValueError, match="不是 active"):
        validate_active_assumption_ids(superseded, ["ASM-SHELTER"])

    second = add_assumption(added, assumption(
        "ASM-TERRAIN", field="terrain_evidence", value="missing",
        authority_effect="provisional_only",
    ))
    withdrawn = withdraw_assumption(second, "ASM-TERRAIN")
    assert {item["status"] for item in withdrawn["items"]} == {"active", "withdrawn"}


def test_registry_rejects_duplicate_ids_and_invalid_authority_effect():
    registry = add_assumption(None, assumption())
    with pytest.raises(ValueError, match="已存在"):
        add_assumption(registry, assumption())
    with pytest.raises(ValueError, match="authority_effect"):
        add_assumption(None, assumption(authority_effect="automatic_pass"))


def test_assumption_cannot_downgrade_required_or_turn_unknown_into_passed():
    with pytest.raises(ValueError, match="REQUIRED"):
        validate_assumption_application(
            requirement_level=InputRequirementLevel.REQUIRED
        )
    with pytest.raises(ValueError, match="unknown assessment"):
        validate_assumption_application(
            requirement_level=InputRequirementLevel.ASSUMABLE,
            assessment_before=AssessmentOutcome.UNKNOWN,
            assessment_after=AssessmentOutcome.PASSED,
        )
    validate_assumption_application(
        requirement_level=InputRequirementLevel.ASSUMABLE,
        assessment_before=AssessmentOutcome.UNKNOWN,
        assessment_after=AssessmentOutcome.UNKNOWN,
    )
