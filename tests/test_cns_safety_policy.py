from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.safety_policy import default_safety_policy, normalize_safety_policy
from cns_planner.safety.event_evaluator import (
    evaluate_failure_condition,
    evaluate_safety_events,
    evaluate_unacceptable_event,
)
from cns_planner.safety.fault_tree import evaluate_fault_tree


DEFAULTS = Path("cns_planner/config/defaults.json")


def templates():
    policy = default_safety_policy()
    return policy, policy["failure_conditions"][0], policy["unacceptable_events"][0]


def test_templates_are_unconfirmed_and_do_not_invent_severity_or_probability():
    policy = default_safety_policy()
    assert [item["failure_condition_id"] for item in policy["failure_conditions"]] == [
        "FC-C-01", "FC-N-01", "FC-S-01",
    ]
    assert [item["unacceptable_event_id"] for item in policy["unacceptable_events"]] == [
        "UE-C-01", "UE-N-01", "UE-S-01",
    ]
    for item in policy["failure_conditions"] + policy["unacceptable_events"]:
        assert item["severity"] == "unknown"
        assert item["confirmed"] is False
        assert item["source"] == "project_template"
    assert all(item["probability"] is None for item in policy["unacceptable_events"])


@pytest.mark.parametrize(
    "service_state,expected",
    [
        ("lost", "triggered"),
        ("available", "not_triggered"),
        ("available_degraded", "not_triggered"),
        ("contingency", "not_triggered"),
        ("unknown", "unknown"),
        ("not_applicable", "not_applicable"),
    ],
)
def test_loss_failure_condition_uses_service_state_without_changing_p4(service_state, expected):
    _, failure_condition, _ = templates()
    result = evaluate_failure_condition(
        failure_condition,
        {"service_state": service_state, "evidence": [{"kind": "p4"}]},
        {"subsystem": "C"},
    )
    assert result["status"] == expected
    assert result["evidence"][-1] == {"kind": "service_state", "value": service_state}


def test_unconfirmed_unacceptable_event_remains_unknown_after_loss():
    _, failure_condition, unacceptable_event = templates()
    fc_result = evaluate_failure_condition(failure_condition, "lost")
    result = evaluate_unacceptable_event(
        unacceptable_event,
        {failure_condition["failure_condition_id"]: fc_result},
    )
    assert fc_result["status"] == "triggered"
    assert result["status"] == "unknown"
    assert result["severity"] == "unknown"
    assert "不能形成安全结论" in result["reasons"][0]


def test_confirmed_unacceptable_event_can_be_evaluated_without_inferred_severity():
    _, failure_condition, unacceptable_event = templates()
    unacceptable_event = {
        **unacceptable_event,
        "confirmed": True,
        "status": "passed",
        "source": "approved project analysis",
    }
    result = evaluate_safety_events(
        failure_condition, "lost", {"subsystem": "C"}, unacceptable_event
    )
    assert result["failure_condition"]["status"] == "triggered"
    assert result["unacceptable_event"]["status"] == "triggered"
    assert result["unacceptable_event"]["severity"] == "unknown"


def gate(kind, independence=True):
    return {
        "node_type": "top_event",
        "event_ref": "TOP",
        "children": [{
            "node_type": kind,
            "independence_confirmed": independence,
            "children": [
                {
                    "node_type": "basic_event",
                    "event_ref": "B1",
                    "state": "triggered",
                    "probability": 0.1,
                },
                {"node_type": "reference", "event_ref": "B2"},
            ],
        }],
    }


def test_fault_tree_and_or_qualitative_and_probability():
    states = {"B2": {"status": "triggered", "probability": 0.2}}
    and_result = evaluate_fault_tree(gate("and"), states)
    or_result = evaluate_fault_tree(gate("or"), states)
    assert and_result["status"] == "triggered"
    assert and_result["probability"] == pytest.approx(0.02)
    assert or_result["status"] == "triggered"
    assert or_result["probability"] == pytest.approx(0.28)


def test_fault_tree_refuses_probability_without_confirmed_independence():
    result = evaluate_fault_tree(
        gate("or", independence=False),
        {"B2": {"status": "triggered", "probability": 0.2}},
    )
    assert result["probability"] is None
    assert result["status"] == "pending_dependency"
    assert result["qualitative_status"] == "triggered"


def test_fmea_traceability_references_are_validated_and_rpn_is_not_persisted():
    policy = default_safety_policy()
    policy["fmea_records"] = [{
        "failure_mode_id": "FM-C-01",
        "function": "communication",
        "component": "airborne radio",
        "subsystem": "C",
        "failure_mode": "loss",
        "local_effect": "link unavailable",
        "next_effect": "contingency requested",
        "end_effect": "operation effect pending",
        "detection": "service-state monitor",
        "mitigation": "confirmed fallback",
        "failure_condition_refs": ["FC-C-01"],
        "unacceptable_event_refs": ["UE-C-01"],
        "source": "project analysis",
        "confirmed": False,
        "rpn": 125,
    }]
    normalized = normalize_safety_policy(policy)
    assert normalized["fmea_records"][0]["failure_condition_refs"] == ["FC-C-01"]
    assert "rpn" not in normalized["fmea_records"][0]
    invalid = deepcopy(policy)
    invalid["fmea_records"][0]["failure_condition_refs"] = ["FC-UNKNOWN"]
    with pytest.raises(ValueError, match="FMEA 引用无效"):
        normalize_safety_policy(invalid)


def test_schema_v2_backfill_and_safety_policy_round_trip(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    legacy = deepcopy(workflow.state)
    legacy.pop("safety_policy")
    legacy.pop("safety_assessment")
    legacy["result_statuses"].pop("safety_assessment")
    workflow.repository.save(legacy)
    restored = WorkflowService(path, DEFAULTS)
    assert restored.state["schema_version"] == 2
    assert restored.state["safety_policy"]["failure_conditions"][0]["severity"] == "unknown"
    assert restored.state["safety_assessment"]["status"] == "not_calculated"
    policy = deepcopy(restored.state["safety_policy"])
    policy.update({"source": "project assessment", "confirmed": True, "status": "passed"})
    restored.set_safety_policy(policy)
    reopened = WorkflowService(path, DEFAULTS)
    assert reopened.state["safety_policy"]["source"] == "project assessment"
    assert reopened.state["safety_policy"]["confirmed"] is True


def test_safety_policy_directed_invalidation_does_not_touch_route_grid_coverage_or_gap(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    untouched = ("workspace", "grid", "environment_risk", "routes", "coverage", "cns_gap")
    for name in untouched:
        workflow.state["result_statuses"][name] = "passed"
    workflow.state["result_statuses"].update({
        "safety_assessment": "passed", "technical_risk": "passed", "report": "passed",
    })
    policy = deepcopy(workflow.state["safety_policy"])
    policy["source"] = "new project source"
    workflow.set_safety_policy(policy)
    assert {name: workflow.state["result_statuses"][name] for name in untouched} == {
        name: "passed" for name in untouched
    }
    assert workflow.state["result_statuses"]["safety_assessment"] == "stale"
    assert workflow.state["result_statuses"]["technical_risk"] == "stale"
    assert workflow.state["result_statuses"]["report"] == "stale"
    assert workflow.state["risks"]["technical"]["status"] == "stale"
    assert workflow.state["risks"]["technical"]["value"] is None


class ApiData:
    error = None


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = ApiData()


def test_safety_policy_and_pure_preview_api(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    router = ApiRouter(ApiContext(workflow))
    policy = router.get("/api/cns/safety-policy", {}, {}).data
    before_assessment = deepcopy(workflow.state["safety_assessment"])
    preview = router.post("/api/cns/events/evaluate", {
        "failure_condition": policy["failure_conditions"][0],
        "unacceptable_event": policy["unacceptable_events"][0],
        "service_state": {"service_state": "lost"},
        "operational_context": {"subsystem": "C"},
    }).data
    assert preview["failure_condition"]["status"] == "triggered"
    assert preview["unacceptable_event"]["status"] == "unknown"
    assert workflow.state["safety_assessment"] == before_assessment
    tree = router.post("/api/cns/fault-tree/evaluate", {
        "tree": gate("and"),
        "event_states": {"B2": {"status": "triggered", "probability": 0.2}},
    }).data
    assert tree["probability"] == pytest.approx(0.02)
    updated = deepcopy(policy)
    updated["source"] = "api project assessment"
    response = router.post(
        "/api/cns/safety-policy", {"safety_policy": updated}
    ).data
    assert response["safety_policy"]["source"] == "api project assessment"


def test_failure_mode_and_severity_enums_are_validated():
    policy = default_safety_policy()
    policy["failure_conditions"][0]["failure_mode"] = "random"
    with pytest.raises(ValueError, match="failure_mode"):
        normalize_safety_policy(policy)
    policy = default_safety_policy()
    policy["failure_conditions"][0]["severity"] = "certainly_catastrophic"
    with pytest.raises(ValueError, match="severity"):
        normalize_safety_policy(policy)
