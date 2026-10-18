"""M4 D: preserve native M3 gates and separately disclose research scope."""
from copy import deepcopy
import importlib
import importlib.util
import pytest
from test_joint_feasibility_evaluation import model_fixture,evaluator_api


def assessment_api():
    name="cns_planner.application.joint_candidate_assessment"
    assert importlib.util.find_spec(name) is not None, "M4 D binding not implemented"
    return importlib.import_module(name)


def candidate():
    d,c=model_fixture();r=evaluator_api().JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    return d,c,r


def bind(c,r,d,registry=None):
    return assessment_api().bind_joint_candidate_assessment(model_evaluation=r,frozen_context=c,
        actions=d["installable_actions"],evidence_registry=registry or {})


def test_model_feasible_but_source_and_final_not_verified():
    d,c,r=candidate();out=bind(c,r,d)
    assert out["scoped_model_status"]=="model_feasible"
    assert out["evidence_status"]=="evidence_incomplete" and out["independent_verification_status"]=="not_verified"
    assert not out["m3_assessment"]["final_research_admission"]["eligible"]
    assert out["input_status"]=="consistent",out["m3_assessment"]["identity_checks"]["errors"]


def test_subset_feasible_does_not_satisfy_complete_required_cns():
    d,c,r=candidate();out=bind(c,r,d)
    assert out["complete_required_cns_status"] in ("unknown","unacceptable")
    assert out["constraint_scope"]["kind"]=="declared_engineering_subset"
    assert out["full_scope_blockers"]
    assert out["formal_ue_status"]=="unresolved"


@pytest.mark.parametrize("change",["payload","parent","action","scope"])
def test_bad_parent_source_action_or_scope_binding_is_inconsistent(change):
    d,c,r=candidate()
    if change=="payload":r["native_results"]["post_plan"]["p14"]["input_fingerprint"]="bad"
    if change=="parent":r["native_materials"]["p15"]["fingerprint_input"]["corridor_assessment"]={}
    if change=="action":r["action_set_fingerprint"]="wrong-action-set"
    if change=="scope":r["context_fingerprint"]="other-context"
    out=bind(c,r,d)
    assert out["input_status"]=="input_inconsistent"
    assert out["scoped_model_status"]!="model_feasible"
    assert out["new_site_stage_unlocked"] is False


def test_fake_trusted_flag_or_new_run_id_cannot_unlock_final():
    d,c,r=candidate();out=bind(c,r,d,{"trusted":True,"run_id":"new-independent-run"})
    assert out["evidence_status"]=="evidence_incomplete"
    assert out["independent_verification_status"]=="not_verified"
    assert not out["final_research_admission"]["eligible"] and not out["new_site_stage_unlocked"]


def test_m3_prefilter_unknown_does_not_prune_search():
    d,c,r=candidate();out=bind(c,r,d)
    assert out["m3_assessment"]["prefilter_status"]=="fixture_only"
    assert out["scoped_model_status"]=="model_feasible"
    result=assessment_api().assemble_joint_search_result(search_result={"model_status":"model_feasible","search_status":"exhausted",
        "coverage_complete":True,"new_site_stage_unlocked":True},candidate_assessments=[out],frozen_context=c)
    assert result["new_site_stage_unlocked"] is False
    assert result["independent_verification_status"]=="not_verified"


def test_excluded_hard_violation_and_unknown_guardrails_remain_visible():
    d,c,r=candidate()
    r["constraint_vector"].extend([{"constraint_id":"Radar-unassessed","status":"not_evaluated"},
        {"constraint_id":"RTK-unknown","status":"unknown"},
        {"constraint_id":"communication-1000m","status":"unconsumed"},
        {"constraint_id":"hard-violation","status":"unacceptable"}])
    out=bind(c,r,d)
    assert out["complete_required_cns_status"]=="unacceptable"
    assert {"Radar-unassessed","RTK-unknown","communication-1000m","hard-violation"} <= set(out["full_scope_blockers"])


def test_invalid_candidate_cannot_remain_top_level_model_feasible():
    d,c,r=candidate();r["action_set_fingerprint"]="wrong"
    assessment=bind(c,r,d)
    result=assessment_api().assemble_joint_search_result(search_result={"model_status":"model_feasible","coverage_complete":True,
        "feasible_combinations":[{"action_set_fingerprint":assessment["action_set_fingerprint"],"actions":d["installable_actions"]}]},
        candidate_assessments=[assessment],frozen_context=c)
    assert result["model_status"]!="model_feasible"
    assert result["input_status"]=="input_inconsistent" and result["feasible_combinations"]==[]
