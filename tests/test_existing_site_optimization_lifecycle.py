"""M5 controlled proposal store, cumulative budgets and safe restore."""
from copy import deepcopy
import importlib
import importlib.util
import json
import os
import pytest
from test_existing_site_lexicographic_search import fixture, Runtime, policy, run, ids, api
from cns_planner.domain.joint_feasibility import seal


@pytest.fixture
def tmp_path():
    # Windows content-addressed filenames + long integration root exceed MAX_PATH
    # with the repository fixture's full test names. Keep only this module's
    # controlled temporary directories short; never touch conftest or M4.
    from pathlib import Path
    import uuid
    root=Path(os.environ["TEMP"])/("c4_"+uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    return root


def modules():
    name="cns_planner.application.existing_site_optimization_store"
    assert importlib.util.find_spec(name) is not None, "M5-C lifecycle behavior not implemented"
    return importlib.import_module(name),importlib.import_module("cns_planner.application.existing_site_optimization_service")


def runner(tmp_path,rt,d,c,checkpoint_id=None,**kw):
    store,service=modules();s=store.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    return service.run_existing_optimization(frozen_domain=d,optimization_context=c,search_policy=policy(**kw),runtime=rt,proposal_store=s,checkpoint_id=checkpoint_id)


def test_cache_once_and_revalidation_budget():
    _,service=modules();d,c=fixture();rt=Runtime(d,c,[("a0",)])
    cache=service.EvaluationCache(frozen_domain=d,optimization_context=c,runtime=rt)
    first=cache.evaluate_action_set(actions=d["installable_actions"][:1]);second=cache.evaluate_action_set(actions=d["installable_actions"][:1])
    assert first==second and len(rt.calls)==1 and cache.actual_replays==1 and cache.cache_hits==1
    second["actions"]=[]
    assert cache.evaluate_action_set(actions=d["installable_actions"][:1])==first
    c2=deepcopy(c);c2["objective_policy"]["version"]="new";c2=seal(c2,"optimization_context_fp")
    assert cache.identity!=service.EvaluationCache(frozen_domain=d,optimization_context=c2,runtime=rt).identity


def test_resume_preserves_equal_frontier_and_unknown(tmp_path):
    d,c=fixture();feasible=[("a0",),("a1",),("a0","a1")];rt=Runtime(d,c,feasible,unknown=[("a2",)])
    rt.after_call=lambda:setattr(rt,"cancel",True) if len(rt.calls)==3 else None
    interrupted=runner(tmp_path,rt,d,c)
    assert interrupted["checkpoint"]["frontier"] and interrupted["checkpoint_id"]
    resumed=runner(tmp_path,Runtime(d,c,feasible,unknown=[("a2",)]),d,c,interrupted["checkpoint_id"])
    whole=run(Runtime(d,c,feasible,unknown=[("a2",)]),d,c)
    assert ids(resumed["incumbent"])==ids(whole["incumbent"])
    assert resumed["unknown_leaves"]==whole["unknown_leaves"]
    assert resumed["metrics"]["resume_revalidation_replays"]==3
    assert resumed["metrics"]["actual_replays"]>whole["metrics"]["actual_replays"]
    assert {k:(v["status"],v["lower_bound"],v["upper_bound"]) for k,v in resumed["layers"].items()}=={k:(v["status"],v["lower_bound"],v["upper_bound"]) for k,v in whole["layers"].items()}


@pytest.mark.parametrize("mutation",["objective","scope","cost","evidence","frontier","incumbent","counters","cache","certificate","model"])
def test_resume_rejects_fingerprint_or_resealed_content(tmp_path,mutation):
    d,c=fixture();rt=Runtime(d,c,[("a0",)]);rt.after_call=lambda:setattr(rt,"cancel",True) if len(rt.calls)==3 else None
    first=runner(tmp_path,rt,d,c);cp=deepcopy(first["checkpoint"])
    if mutation in ("objective","scope","cost","evidence"):
        if mutation=="objective":c["objective_policy"]["version"]="2"
        if mutation=="scope":c["proof_scope"]["domain_scope"]["fixture"]="changed"
        if mutation=="cost":c["objective_policy"]["cost_basis"]["work_items"][0]["unit_price"]=99
        if mutation=="evidence":c["dependencies"]["M5"]="changed"
        c=seal(c,"optimization_context_fp")
    elif mutation=="frontier":cp["frontier"].pop()
    elif mutation=="incumbent":cp["incumbent"]={"fake":True}
    elif mutation=="counters":cp["metrics"]["actual_replays"]=0
    elif mutation=="cache":cp["cache_identity"]="forged"
    elif mutation=="certificate":cp["completed_layers"]={"L1":{"status":"proven_optimal"}}
    else:cp["regions"][0]["evaluation"]["model_evaluation_fp"]="forged"
    cp=seal(cp,"checkpoint_digest");store,service=modules();s=store.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    with pytest.raises(ValueError):
        cid=s.save_checkpoint(payload=cp)
        service.run_existing_optimization(frozen_domain=d,optimization_context=c,search_policy=policy(),runtime=Runtime(d,c,[("a0",)]),proposal_store=s,checkpoint_id=cid)


@pytest.mark.parametrize("stop",["cancel","memory","time","replay","retention","node"])
def test_storage_cancel_and_retention_keep_incumbent(tmp_path,stop):
    d,c=fixture();rt=Runtime(d,c,[()]);kw={}
    if stop in ("cancel","memory","time"):
        def after():
            if stop=="cancel":rt.cancel=True
            elif stop=="memory":rt.memory=257
            else:rt.now=61
        rt.after_call=after
    elif stop=="replay":kw["max_joint_replays"]=1
    elif stop=="retention":rt.feasible=lambda s:True;kw["retain_feasible_limit"]=1
    else:kw["max_visited_nodes"]=5
    r=runner(tmp_path,rt,d,c,**kw)
    assert r["incumbent"] is not None and ids(r["incumbent"])==set()
    assert all(x["status"]!="proven_optimal" for x in r["layers"].values())
    assert r["checkpoint"]["frontier"] and not r["new_site_stage_unlocked"]


def test_resume_revalidation_charges_existing_budget(tmp_path):
    d,c=fixture();rt=Runtime(d,c,[()]);rt.after_call=lambda:setattr(rt,"cancel",True)
    r=runner(tmp_path,rt,d,c,max_joint_replays=1)
    resumed=runner(tmp_path,Runtime(d,c,[()]),d,c,r["checkpoint_id"],max_joint_replays=1)
    assert resumed["metrics"]["actual_replays"]==1 and resumed["termination_reason"]=="joint_replay_budget"
    assert resumed["incumbent"]==r["incumbent"] and resumed["checkpoint"]["frontier"]


@pytest.mark.parametrize("where",["checkpoint","result","replace"])
def test_save_failure_preserves_incumbent(monkeypatch,tmp_path,where):
    mod,service=modules();d,c=fixture();s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    def fail(*a,**kw):raise OSError("synthetic disk full")
    if where=="replace":monkeypatch.setattr(mod.os,"replace",fail)
    else:monkeypatch.setattr(s,"save_"+where,fail)
    r=service.run_existing_optimization(frozen_domain=d,optimization_context=c,search_policy=policy(),runtime=Runtime(d,c,[()]),proposal_store=s)
    assert r["incumbent"] and ids(r["incumbent"])==set() and r["save_status"]=="failed"
    assert not r["new_site_stage_unlocked"] and not r["persisted_to_project_state"]
    if where!="result":assert r["optimization_status"]!="lexicographic_optimal" and r["checkpoint"]["frontier"]
    assert not list(s.root.glob(".staging-*"))


def test_store_total_budget_and_invalid_roots(tmp_path):
    mod,_=modules()
    with pytest.raises(ValueError):mod.M5ProposalStore(root=tmp_path/"proposals"/"m5",max_total_bytes=67108865)
    s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5",max_total_bytes=2000)
    a=s.save_result(payload={"value":"a"*1100})
    with pytest.raises(ValueError,match="storage budget"):s.save_result(payload={"value":"b"*1100})
    assert (s.root/(a+".json")).exists() and len(list(s.root.iterdir()))==1
    for root in (tmp_path/"other",tmp_path/"projects"/"x"/"proposals"/"m5",tmp_path/"artifacts"/"proposals"/"m5"):
        with pytest.raises(ValueError):mod.M5ProposalStore(root=root)
    formal=tmp_path/"formal";formal.mkdir();(formal/"current_project.json").write_text("{}")
    with pytest.raises(ValueError):mod.M5ProposalStore(root=formal/"proposals"/"m5")


@pytest.mark.parametrize("bad",["../escape","C:/formal/current_project.json","checkpoint-"+"0"*64+"/x"])
def test_store_rejects_path_traversal(tmp_path,bad):
    mod,_=modules();s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    with pytest.raises(ValueError):s.load_checkpoint(checkpoint_id=bad,expected_identity={})


def test_store_rejects_executable_json_and_tampering(tmp_path):
    mod,_=modules();s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    for x in ({"module_path":"evil"},{"runtime":"evil"},{"x":float("nan")},{"x":lambda:None}):
        with pytest.raises(ValueError):s.save_result(payload=x)
    d,c=fixture();r=run(Runtime(d,c),d,c);cid=s.save_checkpoint(payload=r["checkpoint"])
    path=s.root/(cid+".json");path.write_text('{"schema_version":"bad","schema_version":"duplicate"}',encoding="utf-8")
    with pytest.raises(ValueError):s.load_checkpoint(checkpoint_id=cid,expected_identity=r["identity"])


def test_reparse_rejection_without_creating_real_links(monkeypatch,tmp_path):
    import stat
    from types import SimpleNamespace
    from pathlib import Path
    mod,_=modules();root=tmp_path/"proposals"/"m5";root.mkdir(parents=True)
    original=Path.lstat
    def lstat(path,*a,**kw):
        if path==root:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,"lstat",lstat)
    with pytest.raises(ValueError,match="reparse"):mod.M5ProposalStore(root=root)


def test_controlled_checkpoint_period(tmp_path,monkeypatch):
    mod,service=modules();s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5");calls=[];original=s.save_checkpoint
    def save(**kw):calls.append(kw["payload"]["metrics"]["visited_nodes"]);return original(**kw)
    monkeypatch.setattr(s,"save_checkpoint",save);d,c=fixture(8)
    r=service.run_existing_optimization(frozen_domain=d,optimization_context=c,search_policy=policy(),runtime=Runtime(d,c,[()]),proposal_store=s)
    assert len(calls)<=7 and r["metrics"]["visited_nodes"]==511
    assert r["metrics"]["search_actual_replays"]==256 and r["metrics"]["verification_replays"]==256


def test_retention_does_not_misclassify_discarded_work_as_verification():
    d,c=fixture();rt=Runtime(d,c,lambda s:True);r=run(rt,d,c,retain_feasible_limit=1)
    assert r["metrics"]["verification_replays"]==0
    assert r["metrics"]["search_actual_replays"]==len(rt.calls)==1


def test_completed_certificates_survive_stopped_resume_as_prior_evidence():
    d,c=fixture();whole=run(Runtime(d,c,[("a0",)]),d,c)
    rt=Runtime(d,c,[("a0",)]);rt.cancel=True
    events=list(api().iter_existing_lexicographic(frozen_domain=d,optimization_context=c,search_policy=policy(),runtime=rt,checkpoint=whole["checkpoint"]))
    r=events[-1]["result"]
    assert r["prior_completed_layers"]==whole["checkpoint"]["completed_layers"]
    assert r["prior_certificate_revalidation_status"]=="not_verified"
    assert r["optimization_status"]!="lexicographic_optimal"


def test_runner_storage_policy_stops_without_discarding_incumbent(tmp_path):
    d,c=fixture();r=runner(tmp_path,Runtime(d,c,[()]),d,c,max_storage_bytes=100)
    assert r["save_status"]=="failed" and r["incumbent"] and r["checkpoint"]["frontier"]


def test_store_high_water_rejects_resealed_budget_rollback(tmp_path):
    mod,_=modules();d,c=fixture();r=run(Runtime(d,c,[()]),d,c);s=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    original=s.save_checkpoint(payload=r["checkpoint"])
    forged=deepcopy(r["checkpoint"]);m=forged["metrics"]
    m["actual_replays"]-=m["verification_replays"];m["verification_replays"]=0;m["elapsed_seconds"]=0
    forged=seal(forged,"checkpoint_digest")
    with pytest.raises(ValueError,match="budget.*rollback"):s.save_checkpoint(payload=forged)
    assert s.load_checkpoint(checkpoint_id=original,expected_identity=r["identity"])


def test_default_64mib_boundary_preserves_existing_file(monkeypatch,tmp_path):
    from pathlib import Path
    import stat
    mod,_=modules();store=mod.M5ProposalStore(root=tmp_path/"proposals"/"m5")
    identity=store.save_result(payload={"sentinel":"retained"});path=store.root/(identity+".json");before=path.read_bytes()
    original=Path.stat
    def reported_size(p,*a,**kw):
        value=original(p,*a,**kw)
        if p==path:
            from types import SimpleNamespace
            fields={k:getattr(value,k) for k in dir(value) if k.startswith("st_")}
            fields["st_size"]=64*1024*1024
            return SimpleNamespace(**fields)
        return value
    monkeypatch.setattr(Path,"stat",reported_size)
    with pytest.raises(ValueError,match="storage budget"):store.save_result(payload={"next":"blocked"})
    assert path.read_bytes()==before and len(list(store.root.iterdir()))==1
