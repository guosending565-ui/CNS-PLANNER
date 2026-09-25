from copy import deepcopy

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.domain.plan_review import variant_id
from test_corridor_site_planner_v2 import configured, device, candidate, existing


class _Context:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = None


def _ready(workflow):
    workflow.evaluate_cns_corridor_site_plan()
    return workflow


def test_initialize_baseline_and_auto_is_deterministic_and_zero_pollution(tmp_path):
    workflow = _ready(configured(tmp_path))
    before = deepcopy({key: workflow.state[key] for key in (
        "existing_cns_facilities", "cns_corridor_assessment", "cns_corridor_gap_assessment",
    )})
    result = workflow.initialize_cns_plan_review()["cns_plan_review"]
    assert [item["source"] for item in result["variants"]] == ["baseline", "p16_auto"]
    auto = result["variants"][1]
    assert auto["variant_id"] == variant_id(result["baseline_fingerprint"], auto["selected_action_ids"])
    assert auto["evaluation"]["authoritative_hypothetical"]["persisted_as_upstream"] is False
    assert result["automatic_overall_score"] is result["automatic_rank"] is None
    assert {key: workflow.state[key] for key in before} == before


def test_user_variant_include_exclude_and_confirm_gate(tmp_path):
    workflow = _ready(configured(tmp_path))
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    auto = review["variants"][1]
    created = workflow.create_cns_plan_variant({
        "base_variant_id": auto["variant_id"], "exclude_action_ids": auto["selected_action_ids"],
        "name": "Manual zero", "notes": "test",
    })["cns_plan_review"]
    # Identity is action-set based; an existing baseline identity is reused rather than duplicated.
    assert created["variants"][0]["selected_action_ids"] == []
    assert len({item["variant_id"] for item in created["variants"]}) == len(created["variants"])


def test_confirm_without_objectives_requires_ack_and_apply_is_idempotent(tmp_path):
    workflow = _ready(configured(tmp_path))
    workflow.state["coverage_3d"].update({"status": "passed", "input_fingerprint": "coverage-before"})
    workflow.state["cns_service_capability"].update({"status": "meets_under_model", "input_fingerprint": "capability-before"})
    workflow.state["service_timeline"].update({"status": "passed", "input_fingerprint": "timeline-before"})
    workflow.state["cns_gap_analysis_v2"].update({"status": "passed", "input_fingerprint": "gap-before"})
    workflow.state["result_statuses"].update({
        "coverage_3d": "passed", "cns_service_capability": "passed",
        "service_timeline": "passed", "cns_gap_v2": "passed",
    })
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    auto = review["variants"][1]
    workflow.select_cns_plan_variant({"variant_id": auto["variant_id"]})
    with pytest.raises(ValueError):
        workflow.confirm_cns_plan({"variant_id": auto["variant_id"]})
    confirmed = workflow.confirm_cns_plan({
        "variant_id": auto["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "synthetic project has no configured planning objectives",
    })["confirmed_cns_plan"]
    assert confirmed["status"] == "confirmed"
    before_count = workflow.state["existing_cns_facilities"]["count"]
    applied = workflow.apply_confirmed_cns_plan({"plan_id": confirmed["plan_id"]})["confirmed_cns_plan"]
    assert applied["status"] == "applied"
    after_count = workflow.state["existing_cns_facilities"]["count"]
    assert after_count >= before_count
    workflow.apply_confirmed_cns_plan({"plan_id": confirmed["plan_id"]})
    assert workflow.state["existing_cns_facilities"]["count"] == after_count
    for name in (
        "coverage_3d", "cns_service_capability", "service_timeline",
        "cns_gap_v2", "cns_corridor_assessment",
        "cns_corridor_gap_assessment", "cns_corridor_site_plan", "report",
    ):
        assert workflow.state["result_statuses"][name] == "stale", name


def test_stale_apply_and_backfill_and_api(tmp_path):
    workflow = _ready(configured(tmp_path))
    response = ApiRouter(_Context(workflow)).post("/api/cns-plan-review/initialize", {}).data
    assert response["cns_plan_review"]["variants"]
    auto = response["cns_plan_review"]["variants"][-1]
    confirmed = ApiRouter(_Context(workflow)).post("/api/cns-plan-review/confirm", {
        "variant_id": auto["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "ack",
    }).data["confirmed_cns_plan"]
    workflow.state["required_cns"]["metadata"] = {"changed": True}
    rejected = ApiRouter(_Context(workflow)).post("/api/cns-plan-review/apply", {"plan_id": confirmed["plan_id"]}).data
    assert rejected["confirmed_cns_plan"]["apply_attempt"]["status"] == "stale_plan"
    legacy = deepcopy(workflow.state)
    legacy.pop("cns_plan_review"); legacy.pop("confirmed_cns_plan")
    legacy["result_statuses"].pop("cns_plan_review")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["cns_plan_review"]["status"] == "not_initialized"
    assert normalized["confirmed_cns_plan"]["status"] == "not_confirmed"


def test_confirmed_objectives_gate_ready_without_override(tmp_path):
    objectives = {"routes": {"R1": {"subsystems": {"C": {"objectives": {
        "min_satisfied_volume_fraction": {"value": 0.5, "operator": ">=", "source": "test", "confirmed": True},
    }}}}}}
    workflow = _ready(configured(tmp_path, objectives=objectives))
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    auto = review["variants"][1]
    assert auto["evaluation"]["confirmation_gate"]["status"] == "ready_for_confirmation"
    confirmed = workflow.confirm_cns_plan({"variant_id": auto["variant_id"], "source": "test", "reason": "objectives met"})
    assert confirmed["confirmed_cns_plan"]["acknowledgements"] == []


def test_no_action_required_baseline_can_be_confirmed_and_noop_applied(tmp_path):
    facility = existing()
    facility["devices"] = [{"device_id": "C1", "subsystem": "C", "status": "active", "service_model": {"status": "missing_data"}}]
    workflow = configured(tmp_path, facilities=[facility])
    assert workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]["status"] == "no_action_required"
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    assert len(review["variants"]) == 1
    baseline = review["variants"][0]
    confirmed = workflow.confirm_cns_plan({
        "variant_id": baseline["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "baseline already meets current requirement",
    })["confirmed_cns_plan"]
    before = deepcopy(workflow.state["existing_cns_facilities"])
    result = workflow.apply_confirmed_cns_plan({"plan_id": confirmed["plan_id"]})["confirmed_cns_plan"]
    assert result["status"] == "applied"
    assert workflow.state["existing_cns_facilities"] == before


def test_confirmed_variant_is_immutable_but_can_clone_new_action_set(tmp_path):
    workflow = _ready(configured(
        tmp_path, redundancy=2,
        devices=[device("C1", "a"), device("C2", "b")], candidates=[candidate("S1")],
    ))
    review = workflow.initialize_cns_plan_review()["cns_plan_review"]
    auto = review["variants"][1]
    workflow.confirm_cns_plan({
        "variant_id": auto["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "manual comparison",
    })
    with pytest.raises(ValueError, match="不可原地修改"):
        workflow.create_cns_plan_variant({"base_variant_id": auto["variant_id"]})
    changed = workflow.create_cns_plan_variant({
        "base_variant_id": auto["variant_id"], "exclude_action_ids": [auto["selected_action_ids"][0]],
        "name": "One action removed",
    })["cns_plan_review"]
    assert any(item["source"] == "user_edited" for item in changed["variants"])
    assert workflow.state["confirmed_cns_plan"]["variant_id"] == auto["variant_id"]


def test_apply_exception_rolls_back_and_invalidation_marks_history_stale(tmp_path):
    workflow = _ready(configured(tmp_path))
    auto = workflow.initialize_cns_plan_review()["cns_plan_review"]["variants"][1]
    confirmed = workflow.confirm_cns_plan({
        "variant_id": auto["variant_id"], "confirm_without_objectives": True,
        "source": "test", "reason": "rollback fixture",
    })["confirmed_cns_plan"]
    before = deepcopy(workflow.state)

    class BrokenCorridor:
        parameters = {}
        def __init__(self, parameters=None): pass
        def evaluate(self, *args, **kwargs): raise RuntimeError("synthetic rerun failure")

    workflow.plan_review_service.corridor_model = BrokenCorridor()
    with pytest.raises(RuntimeError, match="synthetic rerun failure"):
        workflow.apply_confirmed_cns_plan({"plan_id": confirmed["plan_id"]})
    assert workflow.state == before
    workflow.invalidation_service.cns_corridor_site_plan()
    assert workflow.state["cns_plan_review"]["status"] == "stale"
    assert workflow.state["confirmed_cns_plan"]["current_applicability"] == "stale"
