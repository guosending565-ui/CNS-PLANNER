"""M4 B: exhaustive reference is independent of search/pruning code."""
from copy import deepcopy
from itertools import combinations
import importlib
import importlib.util
import random
import pytest
from test_joint_candidate_domain import fixture_domain, fixture_context, domain_args, api


def search_api():
    name="cns_planner.algorithms.joint_feasibility"
    assert importlib.util.find_spec(name) is not None, "M4 B search not implemented"
    return importlib.import_module(name+".v1")


class FixtureRuntime:
    def __init__(self, feasible=(), unknown=()):
        self.feasible={frozenset(x) for x in feasible}; self.unknown={frozenset(x) for x in unknown}
        self.calls=[]; self.now=0.; self.cancel=False; self.memory=1.; self.after_call=None
    def evaluate_action_set(self, *, actions):
        ids=frozenset(r["action_id"] for r in actions); self.calls.append(ids)
        if self.after_call:self.after_call()
        return {"scoped_model_status":"model_feasible" if ids in self.feasible else "unknown" if ids in self.unknown else "unacceptable",
                "evidence_track":"synthetic_fixture", "actions":deepcopy(actions)}
    def evaluate_necessary_conditions(self, *, node): return {"status":"maybe"}
    def monotonic_seconds(self):return self.now
    def cancel_requested(self):return self.cancel
    def resident_memory_mb(self):return self.memory


def policy(**kw):
    p=dict(mode="exact_reference",algorithm_version="joint-feasibility.v1",order_policy="action_id",
        max_visited_nodes=100000,max_joint_replays=10000,max_elapsed_seconds=100,
        max_memory_mb=256,stop_on_first_model_feasible=False,retain_feasible_limit=10000)
    p.update(kw);return p


def run(rt,d=None,**kwargs):
    d=d or fixture_domain();return search_api().search_joint_feasibility(frozen_domain=d,
        frozen_context=fixture_context(d),search_policy=policy(**kwargs),runtime=rt)


def solutions(r):return {frozenset(a["action_id"] for a in s["actions"]) for s in r["feasible_combinations"]}


def test_zero_gain_complement_found():
    rt=FixtureRuntime([("a0","a1")]);r=run(rt)
    assert solutions(r)=={frozenset(("a0","a1"))}
    assert len(rt.calls)==4 and r["coverage_complete"]
    assert r["new_site_stage_unlocked"] is False


def test_nonmonotone_combination_preserves_feasible_subset():
    r=run(FixtureRuntime([("a0",)]))
    assert solutions(r)=={frozenset(("a0",))}


@pytest.mark.parametrize("seed",range(8))
def test_search_matches_independent_exhaustive_sets(seed):
    rng=random.Random(seed); ids=[f"a{i}" for i in range(5)]
    all_sets=[frozenset(c) for k in range(6) for c in combinations(ids,k)]
    expected={c for c in all_sets if rng.random()<0.35}
    r=run(FixtureRuntime(expected),fixture_domain(n=5))
    assert solutions(r)==expected and r["visited_complete_combination_count"]==32


def test_each_safe_pruned_region_has_no_feasible_leaf():
    args=domain_args(n=3);args["qualification_policy"]["incompatible_action_pairs"]=[["a0","a1"]]
    args["qualification_policy"]["structural_source_ref"]="confirmed-capacity"
    d=api().freeze_existing_domain(**args)
    expected={frozenset(c) for k in range(4) for c in combinations(["a0","a1","a2"],k) if not {"a0","a1"}<=set(c)}
    r=run(FixtureRuntime(expected),d)
    assert solutions(r)==expected and r["pruned_node_count"]>0
    for region in r["proof_ledger"]:
        if region["kind"] != "pruned":continue
        prefix=region["prefix"]
        for tail in range(2**(3-len(prefix))):
            bits=prefix+format(tail,f"0{3-len(prefix)}b") if len(prefix)<3 else prefix
            chosen=frozenset(f"a{i}" for i,b in enumerate(bits) if b=="1")
            assert chosen not in expected


def test_unknown_leaves_never_prove_infeasible():
    r=run(FixtureRuntime(unknown=[("a0",)]))
    assert r["model_status"]=="undetermined"
    assert not r["coverage_complete"] and r["unknown_combination_count"]==1
    assert r["proof_level"]!="finite_domain_infeasibility_verified"


@pytest.mark.parametrize("budget",["max_visited_nodes","max_joint_replays","max_elapsed_seconds","max_memory_mb"])
def test_budget_interrupt_preserves_incumbent_and_frontier(budget):
    rt=FixtureRuntime([()]); kw={budget:3 if budget=="max_visited_nodes" else 1}
    if budget=="max_elapsed_seconds":rt.after_call=lambda:setattr(rt,"now",2.)
    if budget=="max_memory_mb":rt.memory=2.
    r=run(rt,**kw)
    assert r["search_status"]=="budget_exhausted" and not r["coverage_complete"]
    assert r["checkpoint"]["frontier"]
    assert r["new_site_stage_unlocked"] is False
    if budget=="max_joint_replays":assert solutions(r)=={frozenset()}


def test_empty_action_baseline_is_evaluated():
    r=run(FixtureRuntime([()]),stop_on_first_model_feasible=True)
    assert solutions(r)=={frozenset()} and r["search_status"]=="search_incomplete"


def test_json_cannot_supply_runtime():
    with pytest.raises(ValueError,match="runtime"):
        run({"evaluate_action_set":"evil.module"})


def test_synthetic_exhaustion_is_not_real_infeasibility():
    r=run(FixtureRuntime())
    assert r["coverage_complete"] and r["model_status"]=="undetermined"
    assert r["proof_level"]=="finite_domain_coverage_unverified"


def test_cancel_and_unfinished_oracle_preserves_pending_leaf():
    rt=FixtureRuntime();rt.after_call=lambda:setattr(rt,"cancel",True)
    r=run(rt)
    assert r["termination_reason"]=="cancelled" and r["checkpoint"]["frontier"]
    assert r["visited_complete_combination_count"]==0


def test_callback_unknown_cannot_prune_a_potential_complement():
    rt=FixtureRuntime([("a0","a1")]);rt.evaluate_necessary_conditions=lambda **kw:{"status":"unknown"}
    assert solutions(run(rt))=={frozenset(("a0","a1"))}


def test_checkpoint_validation_obeys_replay_budget_and_cancel():
    d=fixture_domain();c=fixture_context(d);s=search_api()
    first=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(max_joint_replays=1),runtime=FixtureRuntime())
    runtime=FixtureRuntime();runtime.cancel=True
    resumed=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(),runtime=runtime,checkpoint=first["checkpoint"])
    assert runtime.calls==[] and resumed["termination_reason"]=="cancelled"
    runtime=FixtureRuntime()
    resumed=s.search_joint_feasibility(frozen_domain=d,frozen_context=c,search_policy=policy(max_joint_replays=1),runtime=runtime,checkpoint=first["checkpoint"])
    assert runtime.calls==[] and resumed["search_status"]=="budget_exhausted"


def test_independent_site_upper_bound_deduplicates_colocation():
    args=domain_args(n=2);args["qualification_policy"]["independent_site_requirements"]=[dict(source_ref="fixture-rid-count",required_count=2,
        service_key="C:communication",baseline_possible_site_ids=[])]
    d=api().freeze_existing_domain(**args);rt=FixtureRuntime();r=run(rt,d)
    assert rt.calls==[] and r["pruned_node_count"]==1 and not r["new_site_stage_unlocked"]


def test_unknown_search_policy_and_invalid_retention_are_rejected():
    with pytest.raises(ValueError):run(FixtureRuntime(),cap=6)
    with pytest.raises(ValueError):run(FixtureRuntime(),retain_feasible_limit=0)


def test_twelve_action_domain_matches_4096_subset_reference():
    ids=[f"a{i}" for i in range(12)]
    all_sets=[frozenset(c) for k in range(13) for c in combinations(ids,k)]
    # Independent truth table: no monotonicity assumption and no search helper used.
    expected={s for s in all_sets if s==frozenset(("a0","a11")) or s==frozenset(("a5",))}
    r=run(FixtureRuntime(expected),fixture_domain(n=12),max_elapsed_seconds=600)
    assert solutions(r)==expected
    assert r["visited_complete_combination_count"]==4096 and r["visited_node_count"]==8191
    assert r["coverage_complete"] and r["proof_level"]=="finite_domain_coverage_unverified"


def test_retention_limit_stops_without_dropping_found_solutions():
    r=run(FixtureRuntime([(),("a0",),("a1",),("a0","a1")]),retain_feasible_limit=1)
    assert len(r["feasible_combinations"])==1 and r["termination_reason"]=="feasible_retention_budget"
    assert r["checkpoint"]["frontier"] and not r["coverage_complete"]
