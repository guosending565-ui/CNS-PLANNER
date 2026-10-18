"""Auditable built-in bounds and replay verifier for finite synthetic models.

This is model verification, never source authentication or independent research
acceptance. No necessary-condition callback is trusted as pruning evidence.
"""
from copy import deepcopy
from ...domain.joint_feasibility import json_value, seal, verify


def derive_node_bounds(*, node, frozen_domain, optimization_context):
    from .v1 import identity
    identity(frozen_domain, optimization_context)
    prefix = node.get("prefix")
    actions = frozen_domain["installable_actions"]
    if not isinstance(prefix,str) or len(prefix)>len(actions) or set(prefix)-{"0","1"}: raise ValueError("node prefix")
    selected = {a["action_id"] for a,b in zip(actions,prefix) if b=="1"}
    policy = frozen_domain["qualification_policy"]
    prune = None
    if policy.get("structural_source_ref"):
        for pair in policy.get("incompatible_action_pairs",[]):
            if isinstance(pair,list) and len(pair)==2 and len(set(pair))==2 and set(pair)<=selected:
                prune = dict(rule="irreversible_action_conflict",action_ids=pair,source_ref=policy["structural_source_ref"])
                break
    if prune is None:
        for rule in policy.get("independent_site_requirements",[]):
            if (not rule.get("source_ref") or type(rule.get("required_count")) is not int or rule["required_count"]<0 or
                    not isinstance(rule.get("baseline_possible_site_ids"),list)): continue
            possible = set(rule["baseline_possible_site_ids"])
            for i,a in enumerate(actions):
                if (i>=len(prefix) or prefix[i]=="1") and a.get("service_key")==rule.get("service_key"):
                    possible.add(a["physical_site_id"])
            # Unknown candidates remain optimistic providers, even though this search enumerates Q only.
            for a in frozen_domain["unknown_candidates"]:
                if a.get("service_key")==rule.get("service_key"):
                    possible.add(a["physical_site_id"])
            if len(possible)<rule["required_count"]:
                prune=dict(rule="distinct_site_upper_bound",service_key=rule["service_key"],possible_site_ids=sorted(possible),
                    upper_bound=len(possible),required_count=rule["required_count"],source_ref=rule["source_ref"])
                break
    # No irreversibility contract is assumed for predicted activation, instances,
    # subsidies or discounts. These mathematical bounds cannot remove equality.
    return dict(lower_bounds=dict(activation=0,devices=0,cost=None),prune=prune,
                domain_scope="frozen_installable_actions_only",unknown_not_excluded=True)


def verify_layer_proof(*, layer_proof, frozen_domain, optimization_context, runtime, verification_budget):
    from .v1 import identity, partition, evaluate_leaf, summaries, BudgetStop
    used=0
    try:
        proof, budget = json_value([layer_proof, verification_budget])
        verify(proof,"proof_fp")
        if proof.get("identity")!=identity(frozen_domain,optimization_context):raise ValueError("proof context identity")
        if proof.get("level") not in ("L1","L2","L3"):raise ValueError("proof level")
        required={"max_joint_replays","max_elapsed_seconds","max_memory_mb"}
        if set(budget)!=required:raise ValueError("verification budget fields")
        if type(budget["max_joint_replays"]) is not int or budget["max_joint_replays"]<0:raise ValueError("verification replay budget")
        for key in ("max_elapsed_seconds","max_memory_mb"):
            if type(budget[key]) not in (int,float) or budget[key]<0:raise ValueError("verification budget")
        regions, frontier=proof["regions"],proof["frontier"]
        partition(frontier,regions,len(frozen_domain["installable_actions"]))
        start=runtime.monotonic_seconds()
        def check(replay=False):
            if runtime.cancel_requested():raise BudgetStop("cancelled")
            if runtime.monotonic_seconds()-start>=budget["max_elapsed_seconds"]:raise BudgetStop("verification_time_budget")
            mem=runtime.resident_memory_mb()
            if type(mem) not in (int,float) or mem<0 or mem>budget["max_memory_mb"]:raise BudgetStop("verification_memory_budget")
            if replay and used>=budget["max_joint_replays"]:raise BudgetStop("verification_replay_budget")
        for region in regions:
            check()
            prefix=region["prefix"]
            if region["kind"]=="pruned":
                expected=derive_node_bounds(node={"prefix":prefix},frozen_domain=frozen_domain,optimization_context=optimization_context)["prune"]
                if not expected or expected!=region.get("proof"):raise ValueError("invalid built-in pruning proof")
            elif region["kind"] in ("evaluated","unknown"):
                if len(prefix)!=len(frozen_domain["installable_actions"]):raise ValueError("nonleaf evaluation")
                check(True);used+=1
                actual=evaluate_leaf(frozen_domain,optimization_context,runtime,prefix)
                if actual!=region.get("evaluation") or (actual["scoped_model_status"]=="unknown")!=(region["kind"]=="unknown"):
                    raise ValueError("leaf replay mismatch")
            else:raise ValueError("unknown coverage region")
        check()
        # Compare all claimed fields, not merely the digest. A resealed forged LB,
        # witness, prefix, completeness claim or unknown list is still rejected.
        unvalidated=summaries(frozen_domain,optimization_context,regions,frontier,False)[0]
        validated=summaries(frozen_domain,optimization_context,regions,frontier,True)[0]
        supplied={k:v for k,v in proof.items() if k!="proof_fp"}
        level=proof["level"]
        if supplied not in (unvalidated[level],validated[level]):raise ValueError("layer claim mismatch")
        return dict(verification_status="verified",scope="finite_synthetic_domain",layers=validated,
                    actual_replays=used,source_authentication_status="not_verified",independent_research_status="not_verified")
    except BudgetStop as exc:
        return dict(verification_status="not_verified",reason=str(exc),actual_replays=used)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError) as exc:
        return dict(verification_status="rejected",reason=str(exc),actual_replays=used)
