"""Reject forged coverage/bounds; validate only the frozen synthetic model."""
from copy import deepcopy
import importlib
import pytest
from test_existing_site_lexicographic_search import fixture, Runtime, run, ids, api
from cns_planner.domain.joint_feasibility import seal


def proof_api():return importlib.import_module("cns_planner.algorithms.existing_site_lexicographic.proof")


def test_equal_bound_frontier_survives_next_layer():
    d,c=fixture();r=run(Runtime(d,c,[("a0","a1"),("a0",),("a1",)]),d,c)
    assert ids(r["incumbent"])=={"a0"}
    assert len(r["layers"]["L1"]["ties"])==3 and len(r["layers"]["L2"]["ties"])==2
    assert r["layers"]["L3"]["upper_bound"]==17


def test_unknown_and_nonmonotone_activation_lower_bounds():
    d,c=fixture();p=proof_api().derive_node_bounds(node={"prefix":"11"},frozen_domain=d,optimization_context=c)
    assert p["lower_bounds"]=={"activation":0,"devices":0,"cost":None}
    assert p["prune"] is None # predictions can change with completions


@pytest.mark.parametrize("mutation",["bound","gap","overlap","complete","witness","prefix","unknown"])
def test_invalid_prune_and_unknown_leaf_reject_proof(mutation):
    d,c=fixture();r=run(Runtime(d,c,[("a0",)]),d,c);p=deepcopy(r["layers"]["L1"])
    if mutation=="bound":p["lower_bound"]=99
    elif mutation=="gap":p["regions"].pop()
    elif mutation=="overlap":p["regions"].append(deepcopy(p["regions"][0]))
    elif mutation=="complete":p["regions"][0]={"kind":"pruned","prefix":"","proof":{"reason":"callback_failed"}}
    elif mutation=="witness":p["witness"]["objective_measurement"]["values"]["activation"]=0
    elif mutation=="prefix":p["prefix"]={"activation":42}
    else:p["unknown"]=[];p["regions"][0]["evaluation"]["scoped_model_status"]="model_feasible"
    p=seal(p,"proof_fp")
    v=proof_api().verify_layer_proof(layer_proof=p,frozen_domain=d,optimization_context=c,runtime=Runtime(d,c,[("a0",)]),verification_budget={"max_joint_replays":32,"max_elapsed_seconds":60,"max_memory_mb":256})
    assert v["verification_status"]!="verified"


def test_verification_budget_is_not_valid_proof():
    d,c=fixture();r=run(Runtime(d,c,[("a0",)]),d,c)
    v=proof_api().verify_layer_proof(layer_proof=r["layers"]["L1"],frozen_domain=d,optimization_context=c,runtime=Runtime(d,c,[("a0",)]),verification_budget={"max_joint_replays":0,"max_elapsed_seconds":60,"max_memory_mb":256})
    assert v["verification_status"]=="not_verified"


def test_each_safe_prune_covers_only_infeasible_real_leaves():
    from test_joint_candidate_domain import domain_args, api as domain_api
    from test_existing_site_lexicographic_search import fixture_context
    args=domain_args(n=3);args["qualification_policy"].update(structural_source_ref="synthetic_fixture",incompatible_action_pairs=[["a0","a1"]])
    d=domain_api().freeze_existing_domain(**args);_,c=fixture();c["frozen_context"]=fixture_context(d);c=seal(c,"optimization_context_fp")
    feasible=lambda s:not {"a0","a1"}<=s
    r=run(Runtime(d,c,feasible),d,c)
    assert r["pruned_regions"]
    from itertools import combinations
    for region in r["pruned_regions"]:
        prefix=region["prefix"]
        for k in range(4):
            for leaf in combinations(["a0","a1","a2"],k):
                bits="".join("1" if a in leaf else "0" for a in ["a0","a1","a2"])
                if bits.startswith(prefix):assert not feasible(set(leaf))


def test_provider_upper_bound_includes_unknown_and_deduplicates_towers():
    from test_joint_candidate_domain import fixture_context
    d,c=fixture();d["qualification_policy"]["independent_site_requirements"]=[dict(service_key="C:communication",required_count=2,baseline_possible_site_ids=[],source_ref="synthetic_fixture")]
    d=seal(d,"domain_fingerprint");c["frozen_context"]=fixture_context(d);c=seal(c,"optimization_context_fp")
    b=proof_api().derive_node_bounds(node={"prefix":"00"},frozen_domain=d,optimization_context=c)
    assert b["prune"]["upper_bound"]==1
    d["unknown_candidates"]=[dict(physical_site_id="unknown:tower",service_key="C:communication")];d=seal(d,"domain_fingerprint")
    c["frozen_context"]=fixture_context(d);c=seal(c,"optimization_context_fp")
    assert proof_api().derive_node_bounds(node={"prefix":"00"},frozen_domain=d,optimization_context=c)["prune"] is None
