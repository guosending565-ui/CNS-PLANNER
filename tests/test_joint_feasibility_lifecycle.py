"""M4 E: bounded proposal storage and restart never acquire production authority."""
from copy import deepcopy
import importlib
import importlib.util
import json
import pytest
from test_joint_feasibility_evaluation import model_fixture,evaluator_api
from test_joint_feasibility_search import FixtureRuntime,policy,solutions,search_api
from test_joint_candidate_domain import fixture_context,fixture_domain


def store_api():
    name="cns_planner.application.joint_feasibility_store"
    assert importlib.util.find_spec(name) is not None, "M4 E proposal store not implemented"
    return importlib.import_module(name)


def runner_api():
    return importlib.import_module("cns_planner.application.joint_feasibility_service")


def test_cached_joint_set_replays_once(monkeypatch):
    d,c=model_fixture();cache={};mod=evaluator_api();a=d["installable_actions"]
    calls=[];original=mod.JointModelEvaluator._compute
    def compute(self,actions):calls.append(tuple(x["action_id"] for x in actions));return original(self,actions)
    monkeypatch.setattr(mod.JointModelEvaluator,"_compute",compute)
    first=mod.evaluate_joint_action_set(actions=a,frozen_context=c,evaluation_cache=cache)
    second=mod.evaluate_joint_action_set(actions=a,frozen_context=c,evaluation_cache=cache)
    assert first==second and len(cache)==1 and len(calls)==2  # one frozen baseline plus one joint set
    second["scoped_model_status"]="changed"
    assert cache[next(iter(cache))]["scoped_model_status"]=="model_feasible"


def test_interrupt_resume_matches_uninterrupted_node_budget(tmp_path):
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals")
    d,c=model_fixture();mod=runner_api()
    interrupted=mod.run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(max_joint_replays=1),proposal_store=store)
    assert interrupted["search_status"]=="budget_exhausted"
    resumed=mod.run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),proposal_store=store,checkpoint_id=interrupted["checkpoint_id"])
    whole=mod.run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),proposal_store=store)
    assert solutions(resumed)==solutions(whole) and resumed["coverage_complete"]==whole["coverage_complete"]
    assert not resumed["new_site_stage_unlocked"] and resumed["independent_verification_status"]=="not_verified"


def test_checkpoint_rejects_identity_frontier_or_digest_corruption(tmp_path):
    from cns_planner.domain.joint_feasibility import seal
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals");d=fixture_domain();c=fixture_context(d)
    r=search_api().search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(max_visited_nodes=2),runtime=FixtureRuntime())
    cp=r["checkpoint"];sid=store.save_checkpoint(payload=cp)
    assert store.load_checkpoint(checkpoint_id=sid,expected_identity=cp["identity"])==cp
    with pytest.raises(ValueError):store.load_checkpoint(checkpoint_id=sid,expected_identity={})
    broken=deepcopy(cp);broken["frontier"].append(broken["frontier"][0]);broken=seal(broken,"checkpoint_digest")
    with pytest.raises(ValueError,match="frontier"):
        search_api().validate_checkpoint(checkpoint=broken,frozen_domain=d,frozen_context=c)
    path=tmp_path/"proposals"/(sid+".json");path.write_text("{}",encoding="utf-8")
    with pytest.raises(ValueError):store.load_checkpoint(checkpoint_id=sid,expected_identity=cp["identity"])


@pytest.mark.parametrize("bad_id",["../escape","C:/formal/current_project.json","checkpoint-"+"0"*64+"/x"],ids=["traversal","absolute","separator"])
def test_checkpoint_json_cannot_execute_or_escape_root(tmp_path,bad_id):
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals")
    with pytest.raises(ValueError):store.load_checkpoint(checkpoint_id=bad_id,expected_identity={})
    with pytest.raises(ValueError):store.save_checkpoint(payload={"runtime":"evil.module"})


def test_formal_project_root_and_reparse_roots_are_rejected(tmp_path):
    with pytest.raises(ValueError):store_api().JointProposalStore(proposal_root=tmp_path/"projects"/"formal"/"proposals")
    with pytest.raises(ValueError):store_api().JointProposalStore(proposal_root=tmp_path/"artifacts"/"proposals")


def test_save_failure_preserves_incumbent_and_incomplete_status(monkeypatch,tmp_path):
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals");d,c=model_fixture()
    monkeypatch.setattr(store,"save_checkpoint",lambda **kw:(_ for _ in ()).throw(OSError("fixture disk full")))
    r=runner_api().run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),proposal_store=store)
    assert solutions(r) and r["checkpoint_save_status"]=="checkpoint_save_failed"
    assert r["new_site_stage_unlocked"] is False and r["checkpoint"]["feasible_combinations"]


def test_unknown_and_unfinished_oracle_survive_restore():
    d=fixture_domain();c=fixture_context(d);s=search_api();rt=FixtureRuntime(unknown=[("a0",)])
    r=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),runtime=rt)
    r2=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),runtime=FixtureRuntime(unknown=[("a0",)]),checkpoint=r["checkpoint"])
    assert r2["unknown_combination_count"]==1 and not r2["coverage_complete"]


def test_resume_revalidates_declared_covered_results():
    from cns_planner.domain.joint_feasibility import seal
    d=fixture_domain();c=fixture_context(d);s=search_api()
    r=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(max_joint_replays=1),runtime=FixtureRuntime())
    cp=deepcopy(r["checkpoint"]);cp["regions"][0]["evaluation"]["scoped_model_status"]="model_feasible"
    cp=seal(cp,"checkpoint_digest")
    with pytest.raises(ValueError):
        s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),runtime=FixtureRuntime(),checkpoint=cp)


def test_lifecycle_cannot_write_formal_project_or_artifacts(tmp_path):
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals");d,c=model_fixture();before=deepcopy((d,c))
    formal=tmp_path/"formal";formal.mkdir();(formal/"state").write_bytes(b"rev494");(formal/"artifact").write_bytes(b"rev503")
    r=runner_api().run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),proposal_store=store)
    assert (d,c)==before and (formal/"state").read_bytes()==b"rev494" and (formal/"artifact").read_bytes()==b"rev503"
    assert len(list(formal.iterdir()))==2 and r["persisted_to_project_state"] is False
    assert list((tmp_path/"proposals").glob("*.json"))


def test_runner_does_not_persist_every_search_node(monkeypatch,tmp_path):
    store=store_api().JointProposalStore(proposal_root=tmp_path/"proposals");d,c=model_fixture();calls=[]
    original=store.save_checkpoint
    def save(**kw):calls.append(kw["payload"]["visited_node_count"]);return original(**kw)
    monkeypatch.setattr(store,"save_checkpoint",save)
    result=runner_api().run_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),proposal_store=store)
    assert len(calls)<=2 and len(calls)<result["visited_node_count"]


def test_proposal_total_storage_budget_preserves_previous_files(monkeypatch,tmp_path):
    mod=store_api();monkeypatch.setattr(mod,"MAX_PROPOSAL_BYTES",2048)
    store=mod.JointProposalStore(proposal_root=tmp_path/"proposals")
    first=store.save_result(payload={"value":"x"*1100})
    with pytest.raises(ValueError,match="storage budget"):
        store.save_result(payload={"value":"y"*1100})
    assert (tmp_path/"proposals"/(first+".json")).exists()
    assert len(list((tmp_path/"proposals").iterdir()))==1


def test_scope_registry_and_fact_changes_invalidate_correct_layers():
    from cns_planner.domain.joint_feasibility import seal
    from cns_planner.application.joint_candidate_domain import build_joint_search_context
    from cns_planner.application.joint_candidate_assessment import bind_joint_candidate_assessment
    d,c=model_fixture();cache={};mod=evaluator_api();actions=d["installable_actions"]
    first=mod.evaluate_joint_action_set(actions=actions,frozen_context=c,evaluation_cache=cache)
    changed=deepcopy(c);changed["baseline_inputs"]["model_state"]["grid_attributes"]["terrain"]["cells"]["G1"]["surface_class"]="sea"
    changed=seal(changed,"context_fingerprint")
    second=mod.evaluate_joint_action_set(actions=actions,frozen_context=changed,evaluation_cache=cache)
    assert len(cache)==2
    assert first["native_results"]["post_plan"]["p14"]["input_fingerprint"]!=second["native_results"]["post_plan"]["p14"]["input_fingerprint"]
    scope=deepcopy(c["assessment_scope"]);scope["version"]="2";scope=seal(scope,"scope_fingerprint")
    c2=build_joint_search_context(frozen_rule_snapshot=c["frozen_rule_snapshot"],frozen_domain=d,
        baseline_inputs=c["baseline_inputs"],evaluator_versions=c["evaluator_versions"],assessment_scope=scope)
    mod.evaluate_joint_action_set(actions=actions,frozen_context=c2,evaluation_cache=cache)
    assert len(cache)==3
    a=bind_joint_candidate_assessment(model_evaluation=first,frozen_context=c,actions=actions,evidence_registry={})
    b=bind_joint_candidate_assessment(model_evaluation=first,frozen_context=c,actions=actions,evidence_registry={"source":"different"})
    assert a["m3_arguments"]["candidate_context"]["frozen_dependencies"]["source_registry_fingerprint"]!=b["m3_arguments"]["candidate_context"]["frozen_dependencies"]["source_registry_fingerprint"]
    assert a["independent_verification_status"]==b["independent_verification_status"]=="not_verified"
