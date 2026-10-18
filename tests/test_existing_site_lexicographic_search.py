"""M5-C synthetic finite-domain tests; no real evaluator or project I/O."""
from copy import deepcopy
from itertools import combinations
import importlib
import importlib.util
import time
import pytest
from test_joint_candidate_domain import domain_args, fixture_context, api as domain_api
from test_existing_site_domain_discovery import objective_inputs, prediction
from cns_planner.domain.joint_feasibility import seal, digest


def api():
    name="cns_planner.algorithms.existing_site_lexicographic"
    assert importlib.util.find_spec(name) is not None, "M5-C search behavior not implemented"
    return importlib.import_module(name+".v1")


def fixture(n=3):
    args=domain_args(n=n)
    args["site_records"].append(dict(args["site_records"][0], physical_site_id="tower:T2",aliases=[],baseline_activation_status="active_verified"))
    # a0/a1 share an inactive tower; later actions use an already active tower.
    for i,a in enumerate(args["family_action_records"]):
        if i>=2: a.update(physical_site_id="tower:T2",distinct_site_id="tower:T2",site_id="S2")
    d=domain_api().freeze_existing_domain(**args)
    p=objective_inputs()["objective_policy"]
    p["cost_basis"]=dict(unit="synthetic_credit",pricing_date="2026-10-18",tax="included",discount_rate=0,
        horizon_years=1,coverage=["device","shared"],quantity_basis="work_item",source_ref="synthetic_fixture",uncertainty="exact",
        work_items=[dict(work_item_id="device-"+str(i),action_ids=["a"+str(i)],quantity=1,unit_price=10+i,source_ref="synthetic_fixture") for i in range(n)]+
        [dict(work_item_id="shared",action_ids=["a0","a1"],quantity=1,unit_price=7,source_ref="synthetic_fixture")])
    c=seal(dict(schema_version="existing_site_optimization_context.v1",frozen_context=fixture_context(d),objective_policy=p,
        proof_scope=dict(kind="finite_synthetic_domain",domain_scope={"fixture":"C"},constraint_scope={"model":"fixture-1"}),
        evaluator_version="synthetic.v1",evidence_track="synthetic_fixture",family_configuration={"C":"fixed"},dependencies={"M5":"fixture"}),"optimization_context_fp")
    return d,c


def policy(**kw):
    p=dict(algorithm_version="existing-site-lexicographic.1.0",order_policy="action_id",max_visited_nodes=8191,
        max_joint_replays=20000,max_elapsed_seconds=60,max_memory_mb=256,retain_feasible_limit=4096,max_storage_bytes=67108864)
    p.update(kw);return p


class Runtime:
    def __init__(self,d,c, feasible=None, unknown=()):
        self.d=d;self.c=c;self.feasible=feasible;self.unknown=set(map(frozenset,unknown));self.calls=[]
        self.cancel=False;self.now=0.;self.memory=1.;self.after_call=None;self.unknown_activation=set();self.unknown_devices=set()
    def evaluate_action_set(self, *, actions):
        ids=frozenset(a["action_id"] for a in actions);self.calls.append(ids)
        if self.after_call:self.after_call()
        possible=self.feasible(ids) if callable(self.feasible) else ids in set(map(frozenset,self.feasible or []))
        status="unknown" if ids in self.unknown else "model_feasible" if possible else "unacceptable"
        x=dict(actions=actions,frozen_domain=self.d,objective_policy=self.c["objective_policy"])
        preds=prediction(x)
        if ids in self.unknown_activation:preds={}
        return dict(scoped_model_status=status,evidence_track="synthetic_fixture",evaluator_version="synthetic.v1",
            activation_predictions=preds,participation_evidence={},actions=deepcopy(actions),m3_status="not_verified")
    def evaluate_necessary_conditions(self, *, node):return {"status":"failed","source_ref":"untrusted callback"}
    def monotonic_seconds(self):return self.now
    def cancel_requested(self):return self.cancel
    def resident_memory_mb(self):return self.memory


def run(rt,d,c,**kw):
    events=list(api().iter_existing_lexicographic(frozen_domain=d,optimization_context=c,search_policy=policy(**kw),runtime=rt))
    return events[-1]["result"]


def ids(row):return frozenset(a["action_id"] for a in row["actions"])


def test_l1_precedes_device_and_cost():
    d,c=fixture(4);rt=Runtime(d,c,[("a0",),("a2","a3")]);r=run(rt,d,c)
    assert ids(r["incumbent"])=={"a2","a3"}
    assert [r["layers"][x]["upper_bound"] for x in ("L1","L2","L3")]==[0,2,25]
    assert all(v["status"]=="proven_optimal" for v in r["layers"].values())
    assert r["proof_scope"]["kind"]=="finite_synthetic_domain" and not r["new_site_stage_unlocked"]


def test_l2_precedes_cost_and_l3_only_after_fixed_prefix():
    d,c=fixture(4);c["objective_policy"]["cost_basis"]["work_items"][2]["unit_price"]=1000;c=seal(c,"optimization_context_fp")
    r=run(Runtime(d,c,[("a2",),("a3",),("a2","a3")]),d,c)
    assert ids(r["incumbent"])=={"a3"}
    assert r["layers"]["L2"]["prefix"]=={"activation":0}
    assert r["layers"]["L3"]["prefix"]=={"activation":0,"devices":1}
    assert {ids(x) for x in r["layers"]["L2"]["ties"]}=={frozenset(["a2"]),frozenset(["a3"])}


def test_unknown_activation_is_unranked():
    d,c=fixture();rt=Runtime(d,c,[("a0",),("a2",)]);rt.unknown_activation={frozenset(["a0"])}
    r=run(rt,d,c)
    assert ids(r["incumbent"])=={"a2"} and len(r["unranked_model_feasible"])==1
    assert r["layers"]["L1"]["status"]=="proven_optimal" # optimistic 0 cannot improve 0
    assert r["layers"]["L2"]["status"]!="proven_optimal" # tie could improve later


def test_missing_cost_keeps_two_layer_proofs():
    d,c=fixture();c["objective_policy"]["cost_basis"]=None;c=seal(c,"optimization_context_fp")
    r=run(Runtime(d,c,[("a0",),("a1",)]),d,c)
    assert r["optimization_status"]=="first_two_levels_optimal"
    assert r["layers"]["L3"]["status"]=="cost_unresolved"
    assert r["layers"]["L3"]["upper_bound"] is None


def test_nonmonotone_and_zero_gain_complements():
    d,c=fixture();r=run(Runtime(d,c,[("a0","a1"),("a2",)]),d,c)
    assert {ids(x) for x in r["feasible_combinations"]}=={frozenset(["a0","a1"]),frozenset(["a2"])}
    assert r["coverage_complete"] and r["metrics"]["search_actual_replays"]==8


def test_unknown_leaf_blocks_only_relevant_layers():
    d,c=fixture();r=run(Runtime(d,c,[("a0",)],unknown=[("a2",)]),d,c)
    assert r["layers"]["L1"]["status"]!="proven_optimal" and r["unknown_leaves"]
    assert r["layers"]["L2"]["status"]!="proven_optimal"


@pytest.mark.parametrize("field,value",[("cap",6),("max_joint_replays",-1),("max_memory_mb",None)])
def test_strict_policy(field,value):
    d,c=fixture()
    with pytest.raises(ValueError):run(Runtime(d,c),d,c,**{field:value})


def test_shared_cost_and_measurement_identity():
    from cns_planner.domain.existing_site_optimization import measure_objectives
    d,c=fixture();actions=d["installable_actions"][:2];x=dict(actions=actions,frozen_domain=d,objective_policy=c["objective_policy"])
    r=measure_objectives(**x,activation_predictions=prediction(x),participation_evidence={})
    assert r["values"]=={"activation":1,"devices":2,"cost":28}
    assert r["activation_evidence_identity"]=="model_prediction"
    assert r["confirmed_sets"]["baseline_active_sites"]==[]


def test_negative_cost_does_not_prune_supersets():
    d,c=fixture();c["objective_policy"]["cost_basis"]["work_items"][2]["unit_price"]=-100;c=seal(c,"optimization_context_fp")
    r=run(Runtime(d,c,[("a2",)]),d,c)
    assert r["layers"]["L3"]["upper_bound"]==-100


@pytest.mark.parametrize("variant",["complete","missing_cost","unknown"])
def test_twelve_actions_match_independent_4096_subsets(variant):
    # Reference enumerator and formulas do not use M5 search, measurement or bounds.
    d,c=fixture(12)
    d["qualification_policy"].update(structural_source_ref="synthetic_fixture",incompatible_action_pairs=[["a6","a7"]])
    d=seal(d,"domain_fingerprint");c["frozen_context"]=fixture_context(d)
    if variant=="missing_cost":c["objective_policy"]["cost_basis"]=None
    c=seal(c,"optimization_context_fp")
    names=[f"a{i}" for i in range(12)]
    def feasible(s):
        return (len(s)<=3 and ({"a0","a1"}<=s or {"a2","a3"}<=s or "a4" in s or "a5" in s)
                and not {"a4","a5"}<=s and not {"a6","a7"}<=s)
    reference=[];all_sets=[]
    for k in range(13):
        for combo in combinations(names,k):
            selected=set(combo);all_sets.append(selected)
            if feasible(selected):
                a=int(bool(selected & {"a0","a1"}))
                devices=len(selected)
                cost=sum(10+int(x[1:]) for x in selected)+(7 if selected & {"a0","a1"} else 0)
                reference.append((frozenset(selected),(a,devices,None if variant=="missing_cost" else cost)))
    assert len(all_sets)==4096 and 2*len(all_sets)-1==8191
    rt=Runtime(d,c,feasible,unknown=[("a2",)] if variant=="unknown" else [])
    start=time.monotonic();r=run(rt,d,c);duration=time.monotonic()-start
    assert duration<=60, f"12-action fixture exceeded 60s: {duration}"
    assert {ids(x) for x in r["feasible_combinations"]}=={s for s,v in reference}
    current=reference
    for index,level in enumerate(("L1","L2","L3")):
        if variant=="missing_cost" and index==2:
            assert r["layers"][level]["status"]=="cost_unresolved";break
        best=min(v[index] for _,v in current)
        current=[(s,v) for s,v in current if v[index]==best]
        assert r["layers"][level]["upper_bound"]==best
        assert {ids(x) for x in r["layers"][level]["ties"]}=={s for s,v in current}
        if variant=="unknown" and index>0:assert r["layers"][level]["status"]!="proven_optimal"
        else:
            assert r["layers"][level]["lower_bound"]==best
            assert r["layers"][level]["status"]=="proven_optimal"
    # Every pruned region is independently expanded to its true complete leaves.
    ordered=[a["action_id"] for a in d["installable_actions"]]
    for region in r["pruned_regions"]:
        covered=[s for s in all_sets if "".join("1" if a in s else "0" for a in ordered).startswith(region["prefix"])]
        assert covered and all(not feasible(s) for s in covered)
    assert r["metrics"]["visited_nodes"]<=8191 and r["proof_scope"]["kind"]=="finite_synthetic_domain"
    print(f"C3 {variant}: reference_subsets=4096 reference_nodes=8191 search_nodes={r['metrics']['visited_nodes']} actual={r['metrics']['actual_replays']} seconds={duration:.3f}")


@pytest.mark.parametrize("change",["duplicate","missing","unresolved","bad_basis","fractional"])
def test_cost_contract_rejects_or_preserves_missing(change):
    from cns_planner.domain.existing_site_optimization import measure_cost
    d,c=fixture();p=c["objective_policy"];items=p["cost_basis"]["work_items"]
    if change=="duplicate":items.append(deepcopy(items[0]))
    if change=="missing":items[0]["unit_price"]=None
    if change=="unresolved":p["cost_basis"]["uncertainty"]="interval"
    if change=="bad_basis":p["cost_basis"]["discount_rate"]="unknown"
    if change=="fractional":items[0]["unit_price"]=0.1;items[1]["unit_price"]=0.2;items[-1]["unit_price"]=0
    if change in ("duplicate","bad_basis"):
        with pytest.raises(ValueError):measure_cost(actions=d["installable_actions"][:2],objective_policy=p)
    else:
        v,status=measure_cost(actions=d["installable_actions"][:2],objective_policy=p)
        assert v==(0.3 if change=="fractional" else None)


def test_large_integer_costs_remain_exact_and_distinct():
    from cns_planner.domain.existing_site_optimization import measure_cost
    d,c=fixture();p=c["objective_policy"];p["cost_basis"]["work_items"][2]["unit_price"]=10**30+1
    value,_=measure_cost(actions=d["installable_actions"][2:],objective_policy=p)
    assert value==10**30+1


def test_unknown_zero_bound_preserves_first_two_proofs_and_later_frontier():
    d,c=fixture();r=run(Runtime(d,c,[()],unknown=[("a2",)]),d,c)
    assert r["layers"]["L1"]["status"]=="proven_optimal"
    assert r["layers"]["L2"]["status"]=="proven_optimal"
    assert r["layers"]["L3"]["status"]!="proven_optimal"
    assert r["unknown_leaves"] and r["layers"]["L3"]["unknown"]


def test_unknown_device_count_is_not_action_count():
    d,c=fixture();d["installable_actions"][0].pop("device_instance_id");d=seal(d,"domain_fingerprint")
    c["frozen_context"]=fixture_context(d);c=seal(c,"optimization_context_fp")
    r=run(Runtime(d,c,[("a0",)]),d,c)
    assert r["unranked_model_feasible"][0]["objective_measurement"]["values"]["devices"] is None
    assert r["incumbent"] is None and r["layers"]["L1"]["status"]=="proven_optimal"
    assert r["layers"]["L2"]["status"]!="proven_optimal"


def test_real_or_changed_model_identity_never_enters_c_search():
    d,c=fixture();c["evidence_track"]="real";c=seal(c,"optimization_context_fp")
    rt=Runtime(d,c)
    with pytest.raises(ValueError,match="synthetic_fixture"):run(rt,d,c)
    assert not rt.calls


@pytest.mark.parametrize("level",["basis","item"])
def test_unsupported_cost_semantics_are_not_silently_ignored(level):
    from cns_planner.domain.existing_site_optimization import measure_cost
    d,c=fixture();basis=c["objective_policy"]["cost_basis"]
    if level=="basis":basis["volume_discounts"]=[{"amount":100,"min_devices":2}]
    else:basis["work_items"][0]["requires_all_actions"]=["a0","a1"]
    with pytest.raises(ValueError,match="cost.*field"):
        measure_cost(actions=d["installable_actions"][:1],objective_policy=c["objective_policy"])
