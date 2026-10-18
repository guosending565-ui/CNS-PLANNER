"""Deterministic subset search. No project access, I/O or infeasibility certificates."""
from copy import deepcopy
from ...domain.joint_feasibility import digest, json_value, seal, verify

VERSION="joint-feasibility.v1"


def evaluate_structural_necessary_conditions(*, node, frozen_domain, frozen_context):
    actions=frozen_domain["installable_actions"]; prefix=node["prefix"]
    selected={actions[i]["action_id"] for i,b in enumerate(prefix) if b=="1"}
    policy=frozen_domain["qualification_policy"]
    if policy.get("structural_source_ref"):
        for pair in policy.get("incompatible_action_pairs",[]):
            if len(pair)==2 and set(pair)<=selected:
                return dict(status="necessary_condition_failed",reason_code="irreversible_action_conflict",
                    version=VERSION,source_ref=policy["structural_source_ref"],action_ids=pair)
    # This bound is deliberately optimistic: unknown candidates are possible providers.
    for rule in policy.get("independent_site_requirements",[]):
        if not rule.get("source_ref") or type(rule.get("required_count")) is not int: continue
        if not isinstance(rule.get("baseline_possible_site_ids"),list): continue
        service=rule.get("service_key")
        possible=set(rule["baseline_possible_site_ids"])
        for i,action in enumerate(actions):
            if (i>=len(prefix) or prefix[i]=="1") and action.get("service_key")==service:
                possible.add(action["physical_site_id"])
        possible|={a["physical_site_id"] for a in frozen_domain["unknown_candidates"] if a.get("service_key")==service}
        if len(possible)<rule["required_count"]:
            return dict(status="necessary_condition_failed",reason_code="distinct_site_upper_bound",
                version=VERSION,source_ref=rule["source_ref"],upper_bound=len(possible),required=rule["required_count"])
    return dict(status="maybe")


def _identity(domain,context):
    verify(domain,"domain_fingerprint");verify(context,"context_fingerprint")
    verify(context["assessment_scope"],"scope_fingerprint")
    if context["domain_fingerprint"] != domain["domain_fingerprint"]:raise ValueError("domain identity mismatch")
    return dict(domain_fingerprint=domain["domain_fingerprint"],context_fingerprint=context["context_fingerprint"],
        scope_fingerprint=context["scope_fingerprint"],algorithm_version=VERSION,
        action_order_digest=digest([a["action_id"] for a in domain["installable_actions"]]))


def validate_checkpoint(*, checkpoint, frozen_domain, frozen_context):
    verify(checkpoint,"checkpoint_digest")
    if checkpoint.get("identity")!=_identity(frozen_domain,frozen_context):raise ValueError("checkpoint identity mismatch")
    n=len(frozen_domain["installable_actions"]); ranges=[]
    for prefix in checkpoint["frontier"]+[r["prefix"] for r in checkpoint["regions"]]:
        if not isinstance(prefix,str) or set(prefix)-{"0","1"} or len(prefix)>n:raise ValueError("checkpoint frontier invalid")
        lo=int(prefix or "0",2)*2**(n-len(prefix));ranges.append((lo,lo+2**(n-len(prefix))))
    cursor=0
    for lo,hi in sorted(ranges):
        if lo!=cursor:raise ValueError("checkpoint frontier overlap or gap")
        cursor=hi
    if cursor!=2**n:raise ValueError("checkpoint frontier incomplete")
    if any(type(checkpoint.get(k)) is not int or checkpoint[k]<0 for k in ("visited_node_count","joint_replay_count")):
        raise ValueError("checkpoint counters invalid")
    for region in checkpoint["regions"]:
        if region.get("kind") not in ("evaluated","unknown","pruned"):raise ValueError("checkpoint region invalid")
        if region["kind"]!="pruned" and len(region["prefix"])!=n:raise ValueError("checkpoint leaf invalid")
        if region["kind"]=="pruned":
            proof=evaluate_structural_necessary_conditions(node={"prefix":region["prefix"]},frozen_domain=frozen_domain,frozen_context=frozen_context)
            if proof.get("status")!="necessary_condition_failed" or proof!=region.get("proof"):
                raise ValueError("checkpoint invalid pruning proof")
    expected=[]
    for region in checkpoint["regions"]:
        if region["kind"]=="pruned":continue
        actions=[deepcopy(a) for a,b in zip(frozen_domain["installable_actions"],region["prefix"]) if b=="1"]
        evaluation=region.get("evaluation",{})
        if evaluation.get("actions",actions)!=actions:raise ValueError("checkpoint action binding")
        if evaluation.get("scoped_model_status",evaluation.get("model_status"))=="model_feasible":
            expected.append(dict(actions=actions,action_set_fingerprint=digest(dict(schema_version="candidate_action_set.v1",actions=actions)),model_evaluation=evaluation))
    if expected!=checkpoint["feasible_combinations"]:raise ValueError("checkpoint feasible result mismatch")
    return deepcopy(checkpoint)


def iter_joint_feasibility(*, frozen_domain, frozen_context, search_policy, runtime, checkpoint=None):
    domain=json_value(frozen_domain);context=json_value(frozen_context);policy=json_value(search_policy)
    for method in ("evaluate_action_set","evaluate_necessary_conditions","monotonic_seconds","cancel_requested","resident_memory_mb"):
        if isinstance(runtime,dict) or not callable(getattr(runtime,method,None)):raise ValueError("runtime must be code-injected")
    allowed={"mode","algorithm_version","order_policy","seed","max_visited_nodes","max_joint_replays","max_elapsed_seconds","max_memory_mb","stop_on_first_model_feasible","retain_feasible_limit"}
    if set(policy)-allowed:raise ValueError("unknown search policy field")
    if type(policy.get("retain_feasible_limit")) is not int or policy["retain_feasible_limit"]<=0:raise ValueError("retention budget must be positive")
    if policy.get("mode") not in ("exact_reference","bounded_complete_frontier"):raise ValueError("search mode")
    if policy.get("algorithm_version")!=VERSION or policy.get("order_policy")!="action_id":raise ValueError("algorithm/order version")
    for key in ("max_visited_nodes","max_joint_replays"):
        if type(policy.get(key)) is not int or policy[key]<0:raise ValueError("explicit nonnegative budget required")
    for key in ("max_elapsed_seconds","max_memory_mb"):
        if type(policy.get(key)) not in (int,float) or policy[key]<=0:raise ValueError("explicit positive budget required")
    identity=_identity(domain,context);actions=domain["installable_actions"];n=len(actions)
    if [a["action_id"] for a in actions]!=sorted(a["action_id"] for a in actions):raise ValueError("unstable action order")
    if checkpoint:
        cp=validate_checkpoint(checkpoint=checkpoint,frozen_domain=domain,frozen_context=context)
    else:
        cp=dict(schema_version="joint_checkpoint.v1",identity=identity,frontier=[""],regions=[],
            visited_node_count=0,joint_replay_count=0,feasible_combinations=[])
    cp.pop("checkpoint_digest",None);start=runtime.monotonic_seconds();reason="space_exhausted"
    def budget():
        if runtime.cancel_requested():return "cancelled"
        if runtime.monotonic_seconds()-start>=policy["max_elapsed_seconds"]:return "time_budget"
        memory=runtime.resident_memory_mb()
        if memory is None:return "memory_budget_unavailable"
        if memory>policy["max_memory_mb"]:return "memory_budget"
        return None
    resume_pending=False
    if checkpoint:
        for region in cp["regions"]:
            if region["kind"]=="pruned":continue
            stop=budget()
            if not stop and cp["joint_replay_count"]>=policy["max_joint_replays"]:stop="joint_replay_budget"
            if stop:reason=stop;resume_pending=True;break
            chosen=[deepcopy(a) for a,b in zip(actions,region["prefix"]) if b=="1"]
            cp["joint_replay_count"]+=1
            reproduced=json_value(runtime.evaluate_action_set(actions=chosen))
            stop=budget()
            if stop:reason=stop;resume_pending=True;break
            def stable(value):return {k:v for k,v in value.items() if k!="shared_chain_replay_count"}
            if stable(reproduced)!=stable(region["evaluation"]):raise ValueError("checkpoint covered result not reproduced")
    while cp["frontier"] and not resume_pending:
        stop=budget()
        if stop: reason=stop;break
        if cp["visited_node_count"]>=policy["max_visited_nodes"]:reason="node_budget";break
        new_feasible=False
        prefix=cp["frontier"][-1]
        cp["visited_node_count"]+=1
        proof=evaluate_structural_necessary_conditions(node={"prefix":prefix},frozen_domain=domain,frozen_context=context)
        # External necessary-condition responses are hints, never unreviewed proof authority.
        runtime.evaluate_necessary_conditions(node={"prefix":prefix})
        if proof["status"]=="necessary_condition_failed":
            cp["frontier"].pop();cp["regions"].append(dict(prefix=prefix,kind="pruned",proof=proof))
        elif len(prefix)<n:
            cp["frontier"].pop();cp["frontier"].extend([prefix+"1",prefix+"0"])
        else:
            if cp["joint_replay_count"]>=policy["max_joint_replays"]:reason="joint_replay_budget";break
            chosen=[deepcopy(a) for a,b in zip(actions,prefix) if b=="1"]
            cp["joint_replay_count"]+=1
            try: evaluated=json_value(runtime.evaluate_action_set(actions=deepcopy(chosen)))
            except Exception as exc:
                cp["evaluation_error"]=dict(type=type(exc).__name__,message=str(exc));reason="evaluation_error";break
            stop=budget()
            if stop:reason=stop;break
            if "actions" in evaluated and evaluated["actions"]!=chosen:
                reason="input_inconsistent";break
            status=evaluated.get("scoped_model_status",evaluated.get("model_status","unknown"))
            cp["frontier"].pop()
            kind="evaluated" if status in ("model_feasible","unacceptable") else "unknown"
            cp["regions"].append(dict(prefix=prefix,kind=kind,evaluation=evaluated))
            if status=="model_feasible":
                # Same canonical JSON schema as M3; Application independently binds/verifies it.
                fp=digest(dict(schema_version="candidate_action_set.v1",actions=chosen))
                if evaluated.get("action_set_fingerprint",fp)!=fp:raise ValueError("action fingerprint mismatch")
                cp["feasible_combinations"].append(dict(actions=chosen,action_set_fingerprint=fp,model_evaluation=evaluated))
                new_feasible=True
                if policy.get("stop_on_first_model_feasible"):
                    reason="first_model_feasible";break
                if len(cp["feasible_combinations"])>=policy["retain_feasible_limit"] and cp["frontier"]:
                    reason="feasible_retention_budget";break
        if new_feasible or cp["visited_node_count"]%128==0:
            yield dict(kind="checkpoint",payload=seal(cp,"checkpoint_digest"))
        else:
            yield dict(kind="progress",payload=dict(visited_node_count=cp["visited_node_count"],joint_replay_count=cp["joint_replay_count"]))
    unknown=sum(r["kind"]=="unknown" for r in cp["regions"])
    complete=not cp["frontier"] and not unknown and not resume_pending
    result=dict(schema_version="joint_feasibility_search_result.v1",**identity,
        model_status="model_feasible" if cp["feasible_combinations"] and not resume_pending else "undetermined",
        evidence_status="evidence_incomplete",independent_verification_status="not_verified",
        input_status="input_inconsistent" if reason=="input_inconsistent" else "consistent",
        search_status="budget_exhausted" if reason in ("node_budget","joint_replay_budget","time_budget","memory_budget","feasible_retention_budget") else "exhausted" if complete and reason=="space_exhausted" else "search_incomplete",
        coverage_complete=complete and reason=="space_exhausted",termination_reason=reason,
        visited_node_count=cp["visited_node_count"],joint_replay_count=cp["joint_replay_count"],
        visited_complete_combination_count=sum(r["kind"] in ("evaluated","unknown") for r in cp["regions"]),
        unknown_combination_count=unknown,pruned_node_count=sum(r["kind"]=="pruned" for r in cp["regions"]),
        proof_ledger=deepcopy(cp["regions"]),feasible_combinations=deepcopy(cp["feasible_combinations"]),
        proof_level="finite_domain_coverage_unverified" if complete else "candidate_model_evaluated" if cp["feasible_combinations"] else "none",
        proof_blockers=["trusted_infeasibility_verifier_unavailable"],optimality_status="not_claimed",
        search_policy=policy,evidence_track=domain["evidence_track"],maturity="proposal",persisted_to_project_state=False,
        new_site_stage_unlocked=False,checkpoint=seal(cp,"checkpoint_digest"))
    yield dict(kind="result",payload=result)


def search_joint_feasibility(**kwargs):
    for event in iter_joint_feasibility(**kwargs):
        if event["kind"]=="result":return event["payload"]
    raise RuntimeError("missing search result")
