from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import default_algorithm_selection
from cns_planner.algorithms.requirements.manual_v1 import ManualRequiredCNSV1
from cns_planner.algorithms.requirements.operational_context_v2 import OperationalContextRequiredCNSV2
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_inputs import pending_required_cns
from cns_planner.domain.requirement_policy import (
    normalize_operation_context, normalize_requirement_policies,
)


DEFAULTS = Path("cns_planner/config/defaults.json")


def context(**values):
    return {"project_default": {
        name: {"value": value, "source": "synthetic_fixture", "confirmed": True}
        for name, value in values.items()
    }}


def complete_requirements():
    return {
        "communication": {
            "required": True, "coverage_requirement": 0.95, "max_gap_m": 1000,
            "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
        },
        "navigation": {"required": False},
        "surveillance": {"required": False},
    }


def policy(policy_id="P-1", field="operation_mode", expected="bvlos", requirements=None, **updates):
    item = {
        "policy_id": policy_id, "version": "1.0", "name": policy_id,
        "source_type": "project_rule", "source": "synthetic_fixture",
        "reference": "TEST", "clause": "1", "confirmed": True,
        "applicability": {"all_of": [{"field": field, "operator": "eq", "value": expected}]},
        "requirements": requirements or complete_requirements(),
    }
    item.update(updates)
    return item


def evaluate(model=None, ctx=None, policies=None, routes=None, current=None):
    return (model or OperationalContextRequiredCNSV2()).evaluate(
        current or pending_required_cns(), ctx or context(operation_mode="bvlos"),
        {"items": policies or [policy()]}, routes or [],
    )


def select_v2(workflow):
    workflow.select_algorithm({
        "algorithm_type": "requirement_model",
        "algorithm_id": "operational_context_required_cns_v2",
        "version": "2.0", "parameters": {},
    })


def test_manual_v1_is_default_pass_through_and_old_project_backfills(tmp_path):
    assert default_algorithm_selection()["requirement_model"] == {
        "algorithm_type": "requirement_model", "algorithm_id": "manual_required_cns_v1",
        "version": "1.0", "parameters": {},
    }
    current = pending_required_cns()
    result = ManualRequiredCNSV1().evaluate(current)
    assert result["recommended_required_cns"] == current
    assert result["current_vs_recommended_diff"] == []

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    old = deepcopy(workflow.state)
    for name in ("cns_operation_context", "cns_requirement_policies", "required_cns_recommendation", "required_cns_adoption"):
        old.pop(name)
    old["algorithm_selection"].pop("requirement_model")
    normalized = normalize_project(old, workflow.grid_service)
    assert normalized["algorithm_selection"]["requirement_model"]["algorithm_id"] == "manual_required_cns_v1"
    assert normalized["cns_requirement_policies"]["status"] == "not_configured"


def test_confirmed_exact_match_and_alias_normalization_are_deterministic():
    requirements = complete_requirements()
    requirements["communication"]["latency_ms"] = 1500
    requirements["communication"]["performance"] = {"max_continuous_outage_s": 10, "min_redundancy": 2}
    result = evaluate(policies=[policy(requirements=requirements)])
    communication = result["recommended_required_cns"]["project_default"]["communication"]
    assert result["status"] == "recommendation_ready"
    assert communication["performance"]["max_latency_s"] == 1.5
    assert communication["latency_ms"] == 1500
    assert communication["performance"]["min_redundancy"] == 2
    assert result["input_fingerprint"] == evaluate(policies=[policy(requirements=requirements)])["input_fingerprint"]


def test_route_override_uses_effective_route_context():
    ctx = context(operation_mode="vlos")
    ctx["route_overrides"] = {"R-2": {
        "operation_mode": {"value": "bvlos", "source": "route", "confirmed": True}
    }}
    result = evaluate(ctx=ctx, routes=["R-1", "R-2"])
    matched = {(item["scope"], item["policy_id"]) for item in result["matched_policies"]}
    assert ("R-2", "P-1") in matched
    assert ("R-1", "P-1") not in matched
    assert result["recommended_required_cns"]["route_overrides"]["R-2"]["communication"]["required"] is True


@pytest.mark.parametrize("operation_mode,traffic,latency", [
    ("vlos", "single_uas", 5.0), ("bvlos", "multiple_uas", 1.0),
])
def test_context_names_have_no_builtin_semantics_without_synthetic_policy(operation_mode, traffic, latency):
    policies = [policy(
        policy_id=f"{operation_mode}-{traffic}", requirements={"communication": {"performance": {"max_latency_s": latency}}},
        applicability={"all_of": [
            {"field": "operation_mode", "operator": "eq", "value": operation_mode},
            {"field": "uas_traffic_context", "operator": "eq", "value": traffic},
        ]},
    )]
    result = evaluate(ctx=context(operation_mode=operation_mode, uas_traffic_context=traffic), policies=policies)
    assert result["recommended_required_cns"]["project_default"]["communication"]["performance"]["max_latency_s"] == latency


def test_no_policy_has_no_defaults_and_missing_or_unconfirmed_context_is_unknown():
    no_policy = OperationalContextRequiredCNSV2().evaluate(pending_required_cns(), context(operation_mode="bvlos"), {"items": []}, [])
    assert no_policy["status"] == "not_configured"
    assert no_policy["recommended_required_cns"] is None
    raw = context(operation_mode="bvlos")
    raw["project_default"]["operation_mode"]["confirmed"] = False
    unknown = evaluate(ctx=raw)
    assert unknown["status"] == "pending_confirmation"
    assert unknown["unknown_policies"][0]["reason"] == "applicability_unknown"


def test_unconfirmed_policy_does_not_apply():
    result = evaluate(policies=[policy(confirmed=False)])
    assert result["status"] == "pending_confirmation"
    assert result["matched_policies"] == []
    assert result["unknown_policies"][0]["reason"] == "policy_unconfirmed"


def test_incomplete_recommendation_stays_pending_and_cannot_be_adopted(tmp_path):
    result = evaluate(policies=[policy(requirements={"communication": {"required": True}})])
    assert result["status"] == "pending_confirmation"
    assert result["completeness_status"] == "pending_confirmation"
    assert any(item["reason"] == "recommended_required_cns_incomplete" for item in result["missing_evidence"])
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["required_cns_recommendation"] = result
    with pytest.raises(ValueError, match="尚未完整"):
        workflow.adopt_required_cns_recommendation()


def test_different_fields_merge_and_same_value_merges_provenance():
    policies = [
        policy("A", requirements={"communication": {"required": True}}),
        policy("B", requirements={"communication": {"performance": {"max_latency_s": 2.0}}}),
        policy("C", requirements={"communication": {"performance": {"max_latency_s": 2.0}}}),
    ]
    result = evaluate(policies=policies)
    assert result["status"] == "pending_confirmation"
    assert result["conflicts"] == []
    provenance = result["field_provenance"]["project.communication.performance.max_latency_s"]
    assert [item["policy_id"] for item in provenance] == ["B", "C"]
    reversed_result = evaluate(policies=list(reversed(policies)))
    assert reversed_result["input_fingerprint"] == result["input_fingerprint"]


def test_conflicting_values_are_not_ranked_and_cannot_be_adopted(tmp_path):
    result = evaluate(policies=[
        policy("A", requirements={"communication": {"performance": {"max_latency_s": 1.0}}}),
        policy("B", requirements={"communication": {"performance": {"max_latency_s": 2.0}}}),
    ])
    assert result["status"] == "conflict"
    assert result["recommended_required_cns"] is None
    assert result["conflicts"][0]["policy_ids"] == ["A", "B"]

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["required_cns_recommendation"] = result
    with pytest.raises(ValueError, match="conflict"):
        workflow.adopt_required_cns_recommendation()


def test_preview_is_zero_pollution_and_ignores_capability_facility_runtime(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    select_v2(workflow)
    workflow.set_cns_operation_context(context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [policy()]})
    before = deepcopy(workflow.state["required_cns"])
    first = workflow.evaluate_required_cns_recommendation()["required_cns_recommendation"]
    assert workflow.state["required_cns"] == before
    workflow.state["aircraft_profiles"] = {"items": [{"aircraft_id": "changed"}]}
    workflow.state["existing_cns_facilities"] = {"items": [{"facility_id": "changed"}]}
    workflow.state["service_timeline"] = {"status": "lost"}
    second = workflow.evaluate_required_cns_recommendation()["required_cns_recommendation"]
    assert second["input_fingerprint"] == first["input_fingerprint"]
    assert second["recommended_required_cns"] == first["recommended_required_cns"]


def test_adopt_updates_formal_required_once_and_uses_existing_invalidation(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    select_v2(workflow)
    workflow.set_cns_operation_context(context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [policy()]})
    workflow.state["result_statuses"].update({"cns_service_capability": "passed", "cns_corridor_assessment": "passed"})
    workflow.state["cns_service_capability"]["status"] = "passed"
    workflow.state["cns_corridor_assessment"]["status"] = "passed"
    workflow.evaluate_required_cns_recommendation()
    response = workflow.adopt_required_cns_recommendation({"source": "test_user"})
    assert response["required_cns"]["project_default"]["communication"]["required"] is True
    assert response["required_cns_adoption"]["status"] == "adopted"
    assert response["required_cns_adoption"]["provenance"]
    assert workflow.required_cns_recommendation_snapshot()["current_required_cns_diverged"] is False
    assert workflow.required_cns_recommendation_snapshot()["adoption_status"] == "adopted_current"
    assert response["result_statuses"]["cns_service_capability"] == "stale"
    assert response["result_statuses"]["cns_corridor_assessment"] == "stale"


def test_manual_required_edit_marks_diverged_but_does_not_stale_recommendation(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    select_v2(workflow)
    workflow.set_cns_operation_context(context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [policy()]})
    workflow.evaluate_required_cns_recommendation()
    recommendation_status = workflow.state["required_cns_recommendation"]["status"]
    workflow.set_required_cns({"scope": "project", "requirements": pending_required_cns()["project_default"]})
    assert workflow.state["required_cns_recommendation"]["status"] == recommendation_status
    assert workflow.required_cns_recommendation_snapshot()["current_required_cns_diverged"] is True
    with pytest.raises(ValueError, match="过期"):
        workflow.adopt_required_cns_recommendation()


def test_context_policy_model_and_route_changes_only_stale_recommendation(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["required_cns_recommendation"] = {"status": "recommendation_ready"}
    workflow.state["result_statuses"].update({"required_cns_recommendation": "passed", "routes": "passed", "coverage_3d": "passed"})
    workflow.invalidation_service.workflow("requirement_context")
    assert workflow.state["required_cns_recommendation"]["status"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage_3d"] == "passed"

    workflow.state["required_cns_recommendation"] = {"status": "recommendation_ready"}
    workflow.state["result_statuses"]["required_cns_recommendation"] = "passed"
    workflow.select_algorithm({
        "algorithm_type": "requirement_model", "algorithm_id": "operational_context_required_cns_v2",
        "version": "2.0", "parameters": {},
    })
    assert workflow.state["required_cns_recommendation"]["status"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "passed"


def test_contract_rejects_arbitrary_expression_and_persists_roundtrip(tmp_path):
    with pytest.raises(ValueError, match="白名单"):
        normalize_requirement_policies({"items": [policy(field="__import__('os').system") ]})
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    select_v2(workflow)
    workflow.set_cns_operation_context(context(operation_mode="bvlos"))
    workflow.set_cns_requirement_policies({"items": [policy()]})
    workflow.evaluate_required_cns_recommendation()
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["cns_operation_context"] == workflow.state["cns_operation_context"]
    assert restored.state["cns_requirement_policies"] == workflow.state["cns_requirement_policies"]
    assert restored.state["required_cns_recommendation"] == workflow.state["required_cns_recommendation"]


class ApiContext:
    data = object()
    def __init__(self, workflow): self.workflow = workflow


def test_api_resources_and_registry_manifests(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    router = ApiRouter(ApiContext(workflow))
    assert router.get("/api/cns-operation-context", {}, {}).status == 200
    assert router.get("/api/cns-requirement-policies", {}, {}).data["status"] == "not_configured"
    assert router.get("/api/cns-required-recommendation", {}, {}).data["status"] == "not_calculated"
    ids = {(item["algorithm_type"], item["algorithm_id"], item["version"]) for item in workflow.algorithms_snapshot()["items"]}
    assert ("requirement_model", "manual_required_cns_v1", "1.0") in ids
    assert ("requirement_model", "operational_context_required_cns_v2", "2.0") in ids
    response = router.post("/api/cns-operation-context", context(operation_mode="bvlos"))
    assert response.data["cns_operation_context"]["project_default"]["operation_mode"]["value"] == "bvlos"
    select_v2(workflow)
    assert router.post("/api/cns-requirement-policies", {"items": [policy()]}).status == 200
    evaluated = router.post("/api/cns-required-recommendation/evaluate", {}).data
    assert evaluated["required_cns_recommendation"]["status"] == "recommendation_ready"
    adopted = router.post("/api/cns-required-recommendation/adopt", {"source": "api_test"}).data
    assert adopted["required_cns_adoption"]["status"] == "adopted"
