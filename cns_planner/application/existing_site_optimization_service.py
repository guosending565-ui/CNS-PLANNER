"""Opt-in synthetic M5 runner. No real model import or production registration."""
from copy import deepcopy
from ..domain.joint_feasibility import digest, json_value
from ..domain.existing_site_optimization import proposal_flags
from ..algorithms.existing_site_lexicographic.v1 import (
    identity, checked_policy, iter_existing_lexicographic, summaries)
from .existing_site_optimization_store import M5ProposalStore


class EvaluationCache:
    """Context-complete cache; proof and resume replay deliberately bypass it."""
    def __init__(self, *, frozen_domain, optimization_context, runtime):
        self.identity=identity(frozen_domain,optimization_context)
        self.runtime=runtime;self.cache={};self.attempts=0;self.actual_replays=0;self.cache_hits=0

    def evaluate_action_set(self, *, actions):
        rows=sorted(json_value(actions),key=lambda a:a["action_id"])
        key=digest(dict(identity=self.identity,actions=rows));self.attempts+=1
        if key in self.cache:self.cache_hits+=1
        else:
            self.actual_replays+=1
            self.cache[key]=json_value(self.runtime.evaluate_action_set(actions=rows))
        return deepcopy(self.cache[key])

    def evaluate_uncached_action_set(self, *, actions):
        self.attempts+=1;self.actual_replays+=1
        return self.runtime.evaluate_action_set(actions=actions)

    def evaluate_necessary_conditions(self, *, node):
        return self.runtime.evaluate_necessary_conditions(node=node)

    def monotonic_seconds(self):return self.runtime.monotonic_seconds()
    def cancel_requested(self):return self.runtime.cancel_requested()
    def resident_memory_mb(self):return self.runtime.resident_memory_mb()


def _stopped_result(cp, domain, context):
    layers,rows,unranked,unknown,inc=summaries(domain,context,cp["regions"],cp["frontier"],False)
    # A save failure cannot erase already validated certificates.
    if cp.get("completed_layers"):layers=cp["completed_layers"]
    return dict(schema_version="existing_site_optimization_result.v1",identity=cp["identity"],
        optimization_status="first_two_levels_optimal" if layers["L2"]["status"]=="proven_optimal" else "best_found" if inc else "incomplete",
        termination_reason="save_failed",coverage_complete=not cp["frontier"],layers=layers,incumbent=inc,
        feasible_combinations=rows,unranked_model_feasible=unranked,unknown_leaves=unknown,checkpoint=cp,
        metrics=deepcopy(cp["metrics"]),proof_scope=context["proof_scope"],
        real_domain_proof_status="not_verified",independent_verification_status="not_verified",**proposal_flags())


def run_existing_optimization(*, frozen_domain, optimization_context, search_policy, runtime,
                              proposal_store: M5ProposalStore, checkpoint_id=None):
    if not isinstance(proposal_store,M5ProposalStore):raise ValueError("trusted M5 proposal store required")
    ident=identity(frozen_domain,optimization_context);p=checked_policy(search_policy)
    cp=proposal_store.load_checkpoint(checkpoint_id=checkpoint_id,expected_identity=ident) if checkpoint_id else None
    adapter=EvaluationCache(frozen_domain=frozen_domain,optimization_context=optimization_context,runtime=runtime)
    iterator=iter_existing_lexicographic(frozen_domain=frozen_domain,optimization_context=optimization_context,
        search_policy=p,runtime=adapter,checkpoint=cp)
    last_id=checkpoint_id;result=None;save_error=None;proposal_store.run_limit=p["max_storage_bytes"]
    try:
        for event in iterator:
            if event["kind"]=="checkpoint":
                cp=event["checkpoint"]
                try:last_id=proposal_store.save_checkpoint(payload=cp)
                except (OSError,ValueError) as exc:
                    save_error=str(exc);result=_stopped_result(cp,frozen_domain,optimization_context);break
            elif event["kind"]=="result":result=event["result"]
    finally:
        iterator.close()
    if result is None:raise ValueError("runner produced no result")
    result.update(checkpoint_id=last_id,save_status="failed" if save_error else "saved",
        application_cache_metrics=dict(attempts=adapter.attempts,actual_replays=adapter.actual_replays,cache_hits=adapter.cache_hits))
    if save_error:result["save_error"]=save_error
    else:
        try:result["result_id"]=proposal_store.save_result(payload=result)
        except (OSError,ValueError) as exc:result.update(save_status="failed",save_error=str(exc),termination_reason="save_failed")
    return result
