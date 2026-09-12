from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.safety_policy import (
    default_safety_policy,
    normalize_safety_policy,
)
from cns_planner.safety.coupling import (
    evaluate_coupled_condition,
    evaluate_coupled_events,
    evaluate_coupled_unacceptable_event,
    normalize_event_observation,
)


DEFAULTS = Path("cns_planner/config/defaults.json")


def bundle(index=0, logic=None, temporal=None):
    policy = default_safety_policy()
    dependency = deepcopy(policy["functional_dependencies"][index])
    condition = deepcopy(policy["coupled_conditions"][index])
    coupled_ue = deepcopy(policy["coupled_unacceptable_events"][index])
    dependency.update({"confirmed": True, "status": "passed", "source": "test"})
    condition.update({"confirmed": True, "status": "passed", "source": "test"})
    coupled_ue.update({"confirmed": True, "status": "passed", "source": "test"})
    if logic:
        condition["logic"] = logic
    if temporal is not None:
        condition["temporal"] = temporal
    return dependency, condition, coupled_ue


def observations(dependency, statuses=None, times=None):
    statuses = statuses or ["triggered"] * len(dependency["stages"])
    times = times or [(None, None)] * len(dependency["stages"])
    return [
        {
            "ref": stage["event_ref"],
            "subsystem": stage["subsystem"],
            "status": status,
            "start_s": interval[0],
            "end_s": interval[1],
            "source": "fixture",
        }
        for stage, status, interval in zip(dependency["stages"], statuses, times)
    ]


def test_three_research_templates_are_unconfirmed_and_have_no_safety_claim():
    policy = default_safety_policy()
    assert [item["dependency_id"] for item in policy["functional_dependencies"]] == [
        "FD-CS-01", "FD-CN-01", "FD-NS-01",
    ]
    assert [item["coupled_condition_id"] for item in policy["coupled_conditions"]] == [
        "CC-CS-01", "CC-CN-01", "CC-NS-01",
    ]
    assert [
        item["coupled_unacceptable_event_id"]
        for item in policy["coupled_unacceptable_events"]
    ] == ["CUE-CS-01", "CUE-CN-01", "CUE-NS-01"]
    for key in (
        "functional_dependencies", "coupled_conditions",
        "coupled_unacceptable_events",
    ):
        for item in policy[key]:
            assert item["severity"] == "unknown"
            assert item["confirmed"] is False
            assert item["source"] == "project_template"
            assert item["status"] == "pending_confirmation"
    assert policy["functional_dependencies"][0]["high_level_function"] == (
        "tactical_conflict_mitigation"
    )
    assert policy["functional_dependencies"][1]["dependency_type"] == "recovery"
    assert policy["functional_dependencies"][2]["subsystems"] == ["N", "S"]


def test_p5_policy_backfills_p6_contract_and_validates_references():
    p5 = default_safety_policy()
    for key in (
        "functional_dependencies", "coupled_conditions",
        "coupled_unacceptable_events",
    ):
        p5.pop(key)
    restored = normalize_safety_policy(p5)
    assert len(restored["functional_dependencies"]) == 3
    invalid = deepcopy(restored)
    invalid["coupled_conditions"][0]["functional_dependency_ref"] = "FD-UNKNOWN"
    with pytest.raises(ValueError, match="未知 functional dependency"):
        normalize_safety_policy(invalid)
    invalid = deepcopy(restored)
    invalid["coupled_conditions"][0]["participants"][0] = "OBS-UNKNOWN"
    with pytest.raises(ValueError, match="participants"):
        normalize_safety_policy(invalid)
    invalid = deepcopy(restored)
    invalid["coupled_unacceptable_events"][0]["coupled_condition_refs"] = [
        "CC-UNKNOWN"
    ]
    with pytest.raises(ValueError, match="未知 condition"):
        normalize_safety_policy(invalid)


def test_event_observation_is_json_safe_validated_and_drops_probability():
    result = normalize_event_observation({
        "event_ref": "FC-C-01",
        "subsystem": "c",
        "status": "lost",
        "start_s": 1,
        "end_s": 2,
        "source": "P4",
        "probability": 0.9,
    })
    assert result == {
        "ref": "FC-C-01",
        "subsystem": "C",
        "status": "lost",
        "start_s": 1.0,
        "end_s": 2.0,
        "source": "P4",
    }
    with pytest.raises(ValueError, match="end_s"):
        normalize_event_observation({
            "ref": "X", "subsystem": "C", "status": "triggered",
            "start_s": 2, "end_s": 1,
        })


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["triggered", "triggered"], "triggered"),
        (["triggered", "not_triggered"], "not_triggered"),
        (["triggered", "unknown"], "unknown"),
        (["triggered", "not_applicable"], "not_applicable"),
    ],
)
def test_all_of_logic(statuses, expected):
    dependency, condition, _ = bundle(1, "all_of")
    result = evaluate_coupled_condition(
        condition, observations(dependency, statuses), {}, dependency
    )
    assert result["status"] == expected
    assert result["probability"] is None


def test_sequence_requires_time_and_checks_order_and_timeout():
    dependency, condition, _ = bundle(
        0, "sequence", {"max_separation_s": 5, "min_overlap_s": None}
    )
    correct = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(1, 2), (4, 5)]),
        {},
        dependency,
    )
    assert correct["status"] == "triggered"
    wrong_order = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(4, 5), (1, 2)]),
        {},
        dependency,
    )
    assert wrong_order["status"] == "not_triggered"
    timeout = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(1, 2), (7, 8)]),
        {},
        dependency,
    )
    assert timeout["status"] == "not_triggered"
    assert any(
        item.get("rule") == "max_separation_s"
        for item in timeout["temporal_evidence"]
    )
    missing = evaluate_coupled_condition(
        condition, observations(dependency), {}, dependency
    )
    assert missing["status"] == "unknown"


def test_sequence_uses_dependency_stage_order_and_stage_delay():
    dependency, condition, _ = bundle(0, "sequence")
    dependency["stages"][1]["max_delay_s"] = 2
    condition["participants"] = list(reversed(condition["participants"]))
    result = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(1, 2), (4, 5)]),
        {},
        dependency,
    )
    assert result["status"] == "not_triggered"
    assert any(
        item.get("rule") == "stage_max_delay_s"
        for item in result["temporal_evidence"]
    )


def test_overlap_requires_intervals_and_minimum_overlap():
    dependency, condition, _ = bundle(
        2, "overlap", {"max_separation_s": None, "min_overlap_s": 2}
    )
    triggered = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(0, 5), (2, 6)]),
        {},
        dependency,
    )
    assert triggered["status"] == "triggered"
    no_overlap = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(0, 1), (2, 3)]),
        {},
        dependency,
    )
    assert no_overlap["status"] == "not_triggered"
    too_short = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(0, 2.5), (2, 4)]),
        {},
        dependency,
    )
    assert too_short["status"] == "not_triggered"
    missing = evaluate_coupled_condition(
        condition,
        observations(dependency, times=[(0, None), (1, 2)]),
        {},
        dependency,
    )
    assert missing["status"] == "unknown"


def test_operational_context_controls_applicability():
    dependency, condition, _ = bundle(1, "all_of")
    condition["operational_context"] = {"flight_phase": "enroute"}
    current = observations(dependency)
    assert evaluate_coupled_condition(
        condition, current, {}, dependency
    )["status"] == "unknown"
    assert evaluate_coupled_condition(
        condition, current, {"flight_phase": "landing"}, dependency
    )["status"] == "not_applicable"
    assert evaluate_coupled_condition(
        condition, current, {"flight_phase": "enroute"}, dependency
    )["status"] == "triggered"


def test_unconfirmed_dependency_and_coupled_ue_remain_unknown():
    policy = default_safety_policy()
    dependency = policy["functional_dependencies"][0]
    condition = deepcopy(policy["coupled_conditions"][0])
    condition.update({"confirmed": True, "status": "passed"})
    current = observations(dependency, times=[(0, 1), (2, 3)])
    assert evaluate_coupled_condition(
        condition, current, {}, dependency
    )["status"] == "unknown"
    dependency = deepcopy(dependency)
    dependency.update({"confirmed": True, "status": "passed"})
    condition_result = evaluate_coupled_condition(
        condition, current, {}, dependency
    )
    coupled_ue = policy["coupled_unacceptable_events"][0]
    result = evaluate_coupled_unacceptable_event(
        coupled_ue,
        {condition_result["event_id"]: condition_result},
        {},
    )
    assert condition_result["status"] == "triggered"
    assert result["status"] == "unknown"
    assert result["severity"] == "unknown"


def test_coupling_never_multiplies_or_reports_probability():
    dependency, condition, coupled_ue = bundle(1, "all_of")
    current = observations(dependency)
    for item, probability in zip(current, (0.2, 0.3)):
        item["probability"] = probability
    result = evaluate_coupled_events(
        dependency, condition, current, {}, coupled_ue
    )
    assert result["coupled_condition"]["status"] == "triggered"
    assert result["coupled_unacceptable_event"]["status"] == "triggered"
    assert result["probability"] is None
    assert result["coupled_condition"]["probability"] is None
    assert result["coupled_unacceptable_event"]["probability"] is None
    candidate = default_safety_policy()
    candidate["coupled_unacceptable_events"][0]["probability"] = 0.06
    with pytest.raises(ValueError, match="不接受 coupled probability"):
        normalize_safety_policy(candidate)


def test_p6_backfill_round_trip_and_directed_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    legacy = deepcopy(workflow.state)
    for key in (
        "functional_dependencies", "coupled_conditions",
        "coupled_unacceptable_events",
    ):
        legacy["safety_policy"].pop(key)
    workflow.repository.save(legacy)
    restored = WorkflowService(path, DEFAULTS)
    assert len(restored.state["safety_policy"]["functional_dependencies"]) == 3
    untouched = ("workspace", "grid", "routes", "coverage", "cns_gap")
    for name in untouched:
        restored.state["result_statuses"][name] = "passed"
    restored.state["result_statuses"].update({
        "safety_assessment": "passed",
        "technical_risk": "passed",
        "report": "passed",
    })
    policy = deepcopy(restored.state["safety_policy"])
    policy["functional_dependencies"][0]["source"] = "project study"
    restored.set_safety_policy(policy)
    reopened = WorkflowService(path, DEFAULTS)
    assert (
        reopened.state["safety_policy"]["functional_dependencies"][0]["source"]
        == "project study"
    )
    assert all(reopened.state["result_statuses"][name] == "passed" for name in untouched)
    assert all(
        reopened.state["result_statuses"][name] == "stale"
        for name in ("safety_assessment", "technical_risk", "report")
    )


class ApiData:
    error = None


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = ApiData()


def test_coupled_preview_api_is_pure(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    policy = workflow.state["safety_policy"]
    dependency = deepcopy(policy["functional_dependencies"][0])
    condition = deepcopy(policy["coupled_conditions"][0])
    coupled_ue = deepcopy(policy["coupled_unacceptable_events"][0])
    for item in (dependency, condition, coupled_ue):
        item.update({"confirmed": True, "status": "passed", "source": "test"})
    before = deepcopy(workflow.state["safety_assessment"])
    response = ApiRouter(ApiContext(workflow)).post(
        "/api/cns/coupled-events/evaluate",
        {
            "functional_dependency": dependency,
            "coupled_condition": condition,
            "coupled_unacceptable_event": coupled_ue,
            "observations": observations(
                dependency, times=[(0, 1), (2, 3)]
            ),
            "operational_context": {},
        },
    ).data
    assert response["coupled_condition"]["status"] == "triggered"
    assert response["coupled_unacceptable_event"]["status"] == "triggered"
    assert response["probability"] is None
    assert workflow.state["safety_assessment"] == before
