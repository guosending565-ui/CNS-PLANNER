"""D1 admission and uncached full replay; explicitly synthetic inputs only."""
from copy import deepcopy
import importlib
import importlib.util
import json
import builtins
import io
import pytest
from test_existing_site_action_binding import binding_inputs, evaluation_inputs
from cns_planner.domain.joint_feasibility import seal


def api():
    name = "cns_planner.application.existing_site_optimization_evidence"
    assert importlib.util.find_spec(name) is not None, "D1 admission/replay interface missing"
    return importlib.import_module(name)


def inputs(track="synthetic_fixture"):
    b = binding_inputs(); e = evaluation_inputs()
    d = b["domain_review"]["frozen_domain"]
    if track != "synthetic_fixture":
        d["evidence_track"] = track
        d = seal(d, "domain_fingerprint")
    c = e["frozen_context"]
    c.update(frozen_domain=d, domain_fingerprint=d["domain_fingerprint"], evidence_track=track)
    c = seal(c, "context_fingerprint")
    review = dict(b["domain_review"], frozen_domain=d, original_domain_fingerprint=d["domain_fingerprint"])
    scope = dict(assessment_scope=c["assessment_scope"], planning_units=b["planning_units"],
                 selection_bindings=b["selection_bindings"], model_capabilities=b["model_capabilities"],
                 evidence_track=track, fixed_configurations=[])
    return dict(domain_review=review, frozen_context=c, objective_policy=e["objective_policy"],
        scope_declaration=scope, approved_run_spec=dict(execution_mode="synthetic_test" if track == "synthetic_fixture" else "real",
        proof_scope={"kind":"finite_synthetic_domain"}, output_root="R:/proposals/m5/d1-test",
        budget=dict(max_joint_replays=10, max_elapsed_seconds=60, max_nodes=1000,
                    max_memory_mb=256, max_storage_bytes=1048576),
        consumed=dict(joint_replays=0, elapsed_seconds=0, nodes=0, storage_bytes=0)))


def run(args=None): return api().build_readonly_run(**(args or inputs()))


def proposal(args):
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    return ExistingSiteEvaluator(frozen_context=args["frozen_context"], frozen_domain=args["domain_review"]["frozen_domain"],
        domain_review=args["domain_review"], objective_policy=args["objective_policy"], evidence_registry={}).evaluate_action_set(
            actions=args["domain_review"]["frozen_domain"]["installable_actions"])


def replay(p, r, **kw):
    return api().independently_replay_proposal(proposal=p, run_spec=r, evidence_registry={}, **kw)


def test_explicit_synthetic_admission_preserves_original_scope_and_gaps():
    a=inputs(); before=deepcopy(a); r=run(a)
    assert r["status"] == "ready" and a == before
    assert r["executable_scope"]["kind"] == "finite_synthetic_domain"
    assert r["original_requirement_scope"]["assessment_scope"] == a["frozen_context"]["assessment_scope"]
    assert r["source_gaps"] and r["unconsumed_requirements"]
    assert r["independent_verification_status"] == "not_verified"
    assert not r["persisted_to_project_state"] and not r["new_site_stage_unlocked"]


@pytest.mark.parametrize("claims",[{}, {"approved":True,"authorized":True,"trusted":True},
    {"execution_mode":"synthetic_test","trusted":True,"interruptible":True}])
def test_real_run_requires_separate_approval(claims):
    a=inputs("engineering_research"); a["approved_run_spec"].update(claims)
    r=run(a)
    assert r["status"] == "blocked" and not r["executable_scope"]["enabled"]
    assert "separate_real_run_authorization_required" in r["blockers"]
    assert "controlled_interruptible_worker_unavailable" in r["blockers"]
    assert "optimizer_finite_synthetic_domain_only" in r["blockers"]
    assert "synthetic_inputs_on_real_track" in r["blockers"]


@pytest.mark.parametrize("family",["N:rtk_augmentation","S:radar_noncooperative","N:navigation_integrity_monitoring"])
def test_no_scope_shrink_for_rtk_endpoint_radar(family):
    a=inputs(); a["scope_declaration"]["planning_units"].append(dict(planning_unit_id="unbound",service_key=family))
    r=run(a)
    assert r["status"] == "blocked" and "action_binding_incomplete" in r["blockers"]
    assert r["original_requirement_scope"]["scope_declaration"] == a["scope_declaration"]
    assert any(family in str(g) for g in r["source_gaps"])


def test_fixed_layout_claim_does_not_approve_new_actions_or_real_scope():
    a=inputs("engineering_research")
    a["scope_declaration"]["fixed_configurations"]=[dict(service_key="S:radar_noncooperative",
        configuration_fingerprint="layout",source_ref="user-approved",approved=True)]
    r=run(a)
    assert "fixed_configuration_user_approval_not_authenticated" in r["blockers"]
    assert r["status"] == "blocked"


@pytest.mark.parametrize("field",["provider_inventory_status","handover_status","delivery_status","speed_profile","backup_scenario"])
def test_rtk_native_consumption_gaps_are_explicit(field):
    r=run(inputs("engineering_research"))
    assert field in str(r["source_gaps"])
    assert "communication.max_gap_m" in str(r["unconsumed_requirements"])
    assert "native_rtk_equipment_not_selected" in r["blockers"]


@pytest.mark.parametrize("inventory",[None,"partial","unknown"])
def test_native_m2_satisfied_cannot_close_d1_navigation(inventory):
    from test_rtk_joint_acceptance import evaluate, segment
    a=inputs(); row=segment(provider_inventory_status=inventory); native=evaluate([row])
    assert native["navigation_status"] == "satisfied"
    result=api().navigation_evidence_status(dict(rtk_joint_inputs=dict(segments=[row],
        speed_profile=[{"source_ref":"fixture"}],backup_scenario={"source_ref":"fixture"}),rtk_joint_result=native))
    assert result["navigation_status"] == "unknown" and result["result"] == native
    assert "segments[0].provider_inventory_status" in result["missing_fields"]


def test_readonly_and_m3_not_verified_survive_replay(monkeypatch,tmp_path):
    a=inputs(); p=proposal(a); r=run(a); before=deepcopy((a,p,r))
    state=tmp_path/"ProjectState.json"; state.write_bytes(b"state")
    artifact=tmp_path/"artifact"; artifact.write_bytes(b"artifact")
    original=builtins.open; original_io=io.open
    def guard(op):
        def checked(file,mode="r",*args,**kwargs):
            assert not any(c in mode for c in "wax+"), "D1 attempted a write"
            return op(file,mode,*args,**kwargs)
        return checked
    monkeypatch.setattr(builtins,"open",guard(original)); monkeypatch.setattr(io,"open",guard(original_io))
    v=replay(p,r,verification_receipt={"trusted":True,"verified":True,"run_id":"different"})
    assert v["replay_status"] == "replayed" and v["actual_full_replays"] == 1
    assert v["evaluation"]["applied_action_ids"] == ["comm","rtk"]
    assert v["evaluation"]["native_results"] == p["native_results"]
    assert v["scoped_model_status"] == "model_feasible"
    assert v["complete_required_cns_status"] == "unknown"
    assert v["navigation_status"] == "unknown" and v["independent_verification_status"] == "not_verified"
    assert v["evaluation"]["m3_assessment"] == p["m3_assessment"]
    assert not v["final_research_admission"]["eligible"]
    assert (a,p,r)==before and state.read_bytes()==b"state" and artifact.read_bytes()==b"artifact"
    assert sorted(x.name for x in tmp_path.iterdir())==["ProjectState.json","artifact"]


@pytest.mark.parametrize("field",["native_materials","native_results","binding_identity","actions","action_set_fingerprint","frozen_context","domain_review"])
def test_wrong_parent_source_action_or_scope_rejected_even_resealed(field):
    a=inputs(); p=proposal(a); r=run(a)
    if field=="actions":p[field]=p[field][:1]
    elif field=="action_set_fingerprint":p[field]="forged"
    elif field=="binding_identity":p[field]["source_registry_fingerprint"]="forged"
    elif field=="native_materials":p[field]["p17"]["evaluation_inputs"]["post_plan_projection"]["applied_action_ids"]=[]
    elif field=="native_results":p[field]["post_plan"]["p14"]["routes"][0]["route_id"]="wrong-route"
    elif field=="frozen_context":p[field]["evaluator_versions"]["p14"]="wrong-version"
    else:p[field]["source_manifest"]={"source_ref":"forged"}
    p=seal(p,"evaluation_fp"); v=replay(p,r)
    assert v["replay_status"] == "rejected" and v["scoped_model_status"] != "model_feasible"
    assert v["independent_verification_status"] == "not_verified"


def test_replay_ignores_optimization_cache_and_reconstructs_model(monkeypatch):
    from cns_planner.application.existing_site_optimization_service import EvaluationCache
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    a=inputs(); p=proposal(a); r=run(a)
    monkeypatch.setattr(EvaluationCache,"evaluate_action_set",lambda **kw:pytest.fail("optimization cache used"))
    original=ExistingSiteEvaluator.evaluate_action_set
    def changed(self,**kw):
        out=original(self,**kw); out["native_results"]["post_plan"]["p14"]["input_fingerprint"]="changed-model"
        return seal(out,"evaluation_fp")
    monkeypatch.setattr(ExistingSiteEvaluator,"evaluate_action_set",changed)
    v=replay(p,r)
    assert v["replay_status"] == "rejected" and v["actual_full_replays"] == 1


@pytest.mark.parametrize("mutate",["missing","used","over","path","scope","mode"])
def test_admission_rejects_invalid_budget_path_scope_or_execution_mode(mutate):
    a=inputs(); s=a["approved_run_spec"]
    if mutate=="missing":s["budget"].pop("max_memory_mb")
    elif mutate=="used":s["consumed"]["joint_replays"]=10
    elif mutate=="over":s["budget"]["max_joint_replays"]=101
    elif mutate=="path":s["output_root"]="R:/projects/ProjectState.json"
    elif mutate=="scope":a["scope_declaration"]["assessment_scope"]={}
    else:s["execution_mode"]="real"
    assert run(a)["status"] == "blocked"


def test_sealed_admission_cannot_enable_real_replay(monkeypatch):
    a=inputs("engineering_research"); r=run(a); r["status"]="ready"; r["executable_scope"]["enabled"]=True
    r=seal(r,"run_spec_fp")
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    monkeypatch.setattr(ExistingSiteEvaluator,"evaluate_action_set",lambda **kw:pytest.fail("real call"))
    v=replay({"actions":[]},r)
    assert v["replay_status"] == "blocked" and v["actual_full_replays"] == 0


def test_cli_displays_admission_without_starting_models(tmp_path,capsys,monkeypatch):
    from tools.run_m5_readonly_optimization import main
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    a=inputs("engineering_research"); path=tmp_path/"snapshot.json"; path.write_text(json.dumps(a),encoding="utf-8")
    monkeypatch.setattr(ExistingSiteEvaluator,"evaluate_action_set",lambda **kw:pytest.fail("CLI ran model"))
    assert main(["--snapshot",str(path)]) == 2
    out=json.loads(capsys.readouterr().out)
    assert out["status"] == "blocked" and out["budget"] and out["blockers"]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("change",["policy","consumer","synthetic_limit"])
def test_admission_validates_policy_consumer_and_synthetic_domain_size(change):
    a=inputs()
    if change=="policy": a["objective_policy"]["decision_id"]="wrong-objective"
    elif change=="consumer":
        c=a["frozen_context"]; scope=c["assessment_scope"]
        scope["required_constraint_inventory"][0]["consumer"]["kind"]="missing-consumer"
        scope=seal(scope,"scope_fingerprint")
        c.update(assessment_scope=scope,scope_fingerprint=scope["scope_fingerprint"])
        a["frozen_context"]=seal(c,"context_fingerprint")
        a["scope_declaration"]["assessment_scope"]=scope
    else:
        a["approved_run_spec"]["synthetic_action_limit"]=1
    r=run(a)
    assert r["status"] == "blocked"


def test_replay_budget_and_evidence_axes_never_accept_forged_summary():
    a=inputs(); a["approved_run_spec"]["consumed"]["joint_replays"]=9
    p=proposal(a); p.update(independent_verification_status="verified", evidence_status="trusted")
    p=seal(p,"evaluation_fp"); r=run(a); v=replay(p,r)
    assert v["replay_status"] == "rejected"
    assert v["independent_verification_status"] == "not_verified"
    assert v["cumulative_consumed"]["joint_replays"] == 10


def test_cli_synthetic_ready_is_display_only(tmp_path,capsys,monkeypatch):
    from tools.run_m5_readonly_optimization import main
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    path=tmp_path/"synthetic.json"; path.write_text(json.dumps(inputs()),encoding="utf-8")
    monkeypatch.setattr(ExistingSiteEvaluator,"evaluate_action_set",lambda **kw:pytest.fail("CLI computed"))
    assert main(["--snapshot",str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


@pytest.mark.parametrize("field",["budget","consumed","proof_scope"])
@pytest.mark.parametrize("bad",[None,[],"declared"])
def test_malformed_run_containers_return_blocked(field,bad,tmp_path,capsys):
    from tools.run_m5_readonly_optimization import main
    a=inputs(); a["approved_run_spec"][field]=bad
    path=tmp_path/"bad.json"; path.write_text(json.dumps(a),encoding="utf-8")
    assert main(["--snapshot",str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert run(a)["status"] == "blocked"


@pytest.mark.parametrize("field",["schema_version","decision_id","model_version"])
def test_objective_policy_version_is_checked_before_admission(field):
    a=inputs(); a["objective_policy"][field]="unsupported"
    assert run(a)["status"] == "blocked"


@pytest.mark.parametrize("inventory",["partial","unknown"])
def test_full_replay_never_promotes_native_m2_inventory_gap(monkeypatch,inventory):
    from test_rtk_joint_acceptance import evaluate, segment
    from cns_planner.application.existing_site_evaluation_adapter import ExistingSiteEvaluator
    original=ExistingSiteEvaluator.evaluate_action_set
    row=segment(provider_inventory_status=inventory); native=evaluate([row])
    assert native["navigation_status"] == "satisfied"
    def controlled_fixture(self,**kw):
        out=original(self,**kw)
        out.update(rtk_joint_inputs=dict(segments=[row], speed_profile=[{"source_ref":"fixture"}],
            backup_scenario={"source_ref":"fixture"}),rtk_joint_result=native)
        # A test-only native-M2 positive result exercises D1's consumer gate.
        # No production runtime/factory or upstream semantic patch is introduced.
        return seal(out,"evaluation_fp")
    monkeypatch.setattr(ExistingSiteEvaluator,"evaluate_action_set",controlled_fixture)
    a=inputs(); v=replay(proposal(a),run(a))
    assert v["replay_status"] == "replayed"
    assert v["evaluation"]["rtk_joint_result"]["navigation_status"] == "satisfied"
    assert v["navigation_status"] == "unknown"
    assert not v["final_research_admission"]["eligible"]
    assert v["independent_verification_status"] == "not_verified"
