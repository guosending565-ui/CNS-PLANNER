"""Deterministic complete-frontier M5 search. Pure JSON/model kernel, no I/O."""
from copy import deepcopy
from ...domain.joint_feasibility import json_value, digest, seal, verify
from ...domain.existing_site_optimization import measure_objectives, validate_proof_scope, proposal_flags

VERSION = "existing-site-lexicographic.1.0"
OBJECTIVES = ("activation", "devices", "cost")


class BudgetStop(Exception):
    pass


def identity(domain, context):
    verify(domain, "domain_fingerprint")
    verify(context, "optimization_context_fp")
    m4 = context["frozen_context"]
    verify(m4, "context_fingerprint")
    verify(m4["assessment_scope"], "scope_fingerprint")
    if m4["domain_fingerprint"] != domain["domain_fingerprint"]:
        raise ValueError("M4 domain identity mismatch")
    scope = validate_proof_scope(context["proof_scope"])
    if (context.get("schema_version") != "existing_site_optimization_context.v1" or
            scope["kind"] != "finite_synthetic_domain" or context.get("evidence_track") != "synthetic_fixture" or
            domain["discovery_manifest"].get("evidence_track") != "synthetic_fixture" or
            not context.get("evaluator_version")):
        raise ValueError("C runner requires explicit synthetic_fixture identity")
    actions = domain["installable_actions"]
    if [a["action_id"] for a in actions] != sorted(set(a["action_id"] for a in actions)):
        raise ValueError("stable complete action order required")
    if len(actions) > 12: raise ValueError("finite synthetic budget: at most 12 actions")
    return dict(domain_fingerprint=domain["domain_fingerprint"], optimization_context_fp=context["optimization_context_fp"],
                full_domain_digest=digest(domain), full_context_digest=digest(context), algorithm_version=VERSION,
                action_order_digest=digest(actions), objective_policy_fp=digest(context["objective_policy"]),
                cost_basis_fp=digest(context["objective_policy"].get("cost_basis")))


def check_runtime(runtime):
    for method in ("evaluate_action_set", "evaluate_necessary_conditions", "monotonic_seconds", "cancel_requested", "resident_memory_mb"):
        if isinstance(runtime, dict) or not callable(getattr(runtime, method, None)):
            raise ValueError("runtime must be code-injected")


def checked_policy(policy):
    p = json_value(policy)
    integers = ("max_visited_nodes", "max_joint_replays", "retain_feasible_limit", "max_storage_bytes")
    fields = {*integers, "max_elapsed_seconds", "max_memory_mb", "algorithm_version", "order_policy"}
    if set(p) != fields: raise ValueError("explicit search policy fields required; unknown field or cap")
    if p["algorithm_version"] != VERSION or p["order_policy"] != "action_id": raise ValueError("search algorithm/order")
    for k in integers:
        if type(p[k]) is not int or p[k] < 0: raise ValueError("explicit nonnegative integer budget")
    for k in ("max_elapsed_seconds", "max_memory_mb"):
        if type(p[k]) not in (int, float) or p[k] <= 0: raise ValueError("explicit positive budget")
    if p["max_storage_bytes"] > 67108864: raise ValueError("64MiB proposal limit")
    return p


def selected_actions(domain, prefix):
    return [deepcopy(a) for a, bit in zip(domain["installable_actions"], prefix) if bit == "1"]


def evaluate_leaf(domain, context, runtime, prefix):
    actions = selected_actions(domain, prefix)
    raw = json_value(runtime.evaluate_action_set(actions=deepcopy(actions)))
    if (raw.get("evidence_track") != "synthetic_fixture" or raw.get("evaluator_version") != context["evaluator_version"] or
            raw.get("actions") != actions): raise ValueError("model/source/action identity mismatch")
    status = raw.get("scoped_model_status")
    if status not in ("model_feasible", "unknown", "unacceptable"): raise ValueError("model status")
    measurement = None
    if status == "model_feasible":
        measurement = measure_objectives(actions=actions, frozen_domain=domain, objective_policy=context["objective_policy"],
            activation_predictions=raw.get("activation_predictions", {}), participation_evidence=raw.get("participation_evidence", {}))
        if measurement["combination_conflicts"]: raise ValueError("model feasible contradicts frozen constraints")
        if "objective_measurement" in raw and raw["objective_measurement"] != measurement:
            raise ValueError("model objective measurement mismatch")
    # Keep the M3/model outcome on the witness. Digest rejected outcomes to keep the ledger bounded.
    return dict(scoped_model_status=status, model_evaluation_fp=digest(raw), objective_measurement=measurement,
                model_evaluation=raw if status == "model_feasible" else None)


def witness(domain, region):
    return dict(actions=selected_actions(domain, region["prefix"]),
                objective_measurement=region["evaluation"]["objective_measurement"],
                model_evaluation=region["evaluation"]["model_evaluation"])


def partition(frontier, regions, n):
    intervals = []
    for prefix in list(frontier) + [r["prefix"] for r in regions]:
        if not isinstance(prefix, str) or set(prefix)-{"0", "1"} or len(prefix) > n: raise ValueError("invalid frontier prefix")
        lo = int(prefix or "0", 2) * 2**(n-len(prefix))
        intervals.append((lo, lo+2**(n-len(prefix))))
    cursor = 0
    for lo, hi in sorted(intervals):
        if lo != cursor: raise ValueError("frontier gap or overlap")
        cursor = hi
    if cursor != 2**n: raise ValueError("incomplete frontier")


def summaries(domain, context, regions, frontier, validated=False):
    rows = [witness(domain, r) for r in regions if r["kind"] == "evaluated" and r["evaluation"]["scoped_model_status"] == "model_feasible"]
    unknown = [r["prefix"] for r in regions if r["kind"] == "unknown"]
    unranked = [r for r in rows if any(r["objective_measurement"]["values"][k] is None for k in OBJECTIVES)]
    prefix, layers, previous_proven = {}, {}, True
    for index, key in enumerate(OBJECTIVES):
        eligible = [r for r in rows if all(r["objective_measurement"]["values"][k] == v for k, v in prefix.items())]
        known = [r for r in eligible if r["objective_measurement"]["values"][key] is not None]
        upper = min((r["objective_measurement"]["values"][key] for r in known), default=None)
        ties = [r for r in known if r["objective_measurement"]["values"][key] == upper]
        # Null prior values may enter the fixed prefix. Null current values can improve using their optimistic bound.
        # Unknown feasibility cannot beat the mathematical zero bound of A/D;
        # equality remains in the ledger and may still affect later objectives.
        blockers = (list(unknown) + list(frontier)) if key == "cost" or upper != 0 else []
        for r in rows:
            values = r["objective_measurement"]["values"]
            if any(values[k] is not None and values[k] != v for k, v in prefix.items()): continue
            if any(values[k] is None for k in prefix):
                if key == "cost" or upper != 0: blockers.append(digest(r))
                continue
            if values[key] is None:
                lower = r["objective_measurement"]["bounds"][key]["lower_bound"]
                if upper is None or lower is None or lower < upper: blockers.append(digest(r))
        proven = previous_proven and upper is not None and not blockers and validated
        status = "proven_optimal" if proven else "cost_unresolved" if key == "cost" and any(r["objective_measurement"]["values"][key] is None for r in eligible) else "best_found" if upper is not None else "incomplete"
        lower = upper if proven else 0 if key != "cost" else None
        layers["L"+str(index+1)] = dict(schema_version="existing_site_layer_proof.v1", level="L"+str(index+1),
            identity=identity(domain, context), prefix=deepcopy(prefix), status=status, lower_bound=lower, upper_bound=upper,
            witness=ties[0] if ties else None, ties=ties, unknown=blockers, regions=regions, frontier=frontier,
            verification_status="verified" if validated else "not_verified", proof_scope=context["proof_scope"])
        previous_proven = proven
        if upper is not None: prefix[key] = upper
        else: previous_proven = False
    ranked = [r for r in rows if all(r["objective_measurement"]["values"][k] is not None for k in OBJECTIVES[:2])]
    ranked.sort(key=lambda r: tuple((r["objective_measurement"]["values"][k] is None, r["objective_measurement"]["values"][k] or 0) for k in OBJECTIVES))
    return layers, rows, unranked, unknown, ranked[0] if ranked else None


def validate_checkpoint(cp, domain, context, policy):
    from .proof import derive_node_bounds
    verify(cp, "checkpoint_digest")
    if cp.get("identity") != identity(domain, context) or cp.get("search_policy") != policy: raise ValueError("checkpoint identity/policy mismatch")
    partition(cp["frontier"], cp["regions"], len(domain["installable_actions"]))
    for key, value in cp["metrics"].items():
        if type(value) not in (int, float) or value < 0: raise ValueError("checkpoint cumulative budget")
    m = cp["metrics"]
    leaves = sum(r["kind"] != "pruned" for r in cp["regions"])
    if (m["search_actual_replays"] != leaves or m["actual_replays"] != m["search_actual_replays"]+m["verification_replays"]+m["resume_revalidation_replays"] or
            m["visited_nodes"] < leaves or cp["cache_identity"] != digest(cp["identity"])):
        raise ValueError("checkpoint counters/cache identity")
    processed = set()
    for prefix in cp["frontier"]:
        processed.update(prefix[:i] for i in range(len(prefix)))
    for region in cp["regions"]:
        processed.update(region["prefix"][:i] for i in range(len(region["prefix"])+1))
    if m["visited_nodes"] != len(processed): raise ValueError("checkpoint node budget mismatch")
    for r in cp["regions"]:
        if r["kind"] == "pruned":
            if derive_node_bounds(node={"prefix":r["prefix"]},frozen_domain=domain,optimization_context=context)["prune"] != r.get("proof"):
                raise ValueError("checkpoint pruning proof")
        elif r["kind"] not in ("unknown", "evaluated") or len(r["prefix"]) != len(domain["installable_actions"]): raise ValueError("checkpoint leaf")
    # Derived data cannot be used to smuggle an incumbent or a completed certificate.
    layers, rows, unranked, unknown, incumbent = summaries(domain, context, cp["regions"], cp["frontier"], validated=False)
    if cp["incumbent"] != incumbent or cp["unknown_leaves"] != unknown: raise ValueError("checkpoint incumbent/unknown mismatch")
    return deepcopy(cp)


def iter_existing_lexicographic(*, frozen_domain, optimization_context, search_policy, runtime, checkpoint=None):
    from .proof import derive_node_bounds, verify_layer_proof
    d, c = json_value([frozen_domain, optimization_context]); p = checked_policy(search_policy)
    ident = identity(d, c); check_runtime(runtime); n = len(d["installable_actions"])
    if checkpoint is None:
        cp = dict(schema_version="existing_site_checkpoint.v1", identity=ident, search_policy=p, cache_identity=digest(ident),
            frontier=[""], regions=[], metrics=dict(visited_nodes=0,actual_replays=0,search_actual_replays=0,
                attempts=0,cache_hits=0,verification_replays=0,resume_revalidation_replays=0,elapsed_seconds=0), completed_layers={})
    else: cp = validate_checkpoint(json_value(checkpoint), d, c, p)
    cp.pop("checkpoint_digest", None); metrics = cp["metrics"]; start = runtime.monotonic_seconds(); elapsed = metrics["elapsed_seconds"]
    prior_layers = deepcopy(cp.get("completed_layers") or cp.get("prior_completed_layers", {}))
    cp["prior_completed_layers"] = prior_layers
    prior_revalidated = False
    reason = "space_exhausted"; cache = {}; last_checkpoint_nodes = metrics["visited_nodes"]

    def budget(replay=False):
        metrics["elapsed_seconds"] = elapsed + max(0, runtime.monotonic_seconds()-start)
        if runtime.cancel_requested(): raise BudgetStop("cancelled")
        if metrics["elapsed_seconds"] >= p["max_elapsed_seconds"]: raise BudgetStop("time_budget")
        mem = runtime.resident_memory_mb()
        if type(mem) not in (int,float) or mem < 0: raise BudgetStop("memory_budget_unavailable")
        if mem > p["max_memory_mb"]: raise BudgetStop("memory_budget")
        if replay and metrics["actual_replays"] >= p["max_joint_replays"]: raise BudgetStop("joint_replay_budget")

    def evaluated(prefix, phase):
        metrics["attempts"] += 1
        if prefix in cache:
            metrics["cache_hits"] += 1
            return deepcopy(cache[prefix])
        budget(True); metrics["actual_replays"] += 1; metrics[phase] += 1
        target = runtime
        if phase == "resume_revalidation_replays" and callable(getattr(runtime,"evaluate_uncached_action_set",None)):
            class FreshRuntime:
                def evaluate_action_set(self, *, actions):return runtime.evaluate_uncached_action_set(actions=actions)
            target = FreshRuntime()
        out = evaluate_leaf(d, c, target, prefix)
        # Complete returned outcomes survive cancellation; never silently drop a valid incumbent.
        cache[prefix] = out
        return deepcopy(out)

    def snapshot(layers=None):
        ls, rows, unranked, unknown, inc = summaries(d, c, cp["regions"], cp["frontier"], False)
        cp["incumbent"] = inc; cp["unknown_leaves"] = unknown
        cp["completed_layers"] = layers or {}
        metrics["elapsed_seconds"] = elapsed + max(0, runtime.monotonic_seconds()-start)
        return seal(cp, "checkpoint_digest")

    try:
        if checkpoint is not None:
            # Full replay, never trust a self-sealed cache or completed certificate on disk.
            for region in cp["regions"]:
                if region["kind"] == "pruned": continue
                actual = evaluated(region["prefix"], "resume_revalidation_replays")
                if actual != region["evaluation"]: raise ValueError("resume model/cache mismatch")
            if prior_layers:
                expected = summaries(d,c,cp["regions"],cp["frontier"],True)[0]
                if prior_layers != expected: raise ValueError("resume completed certificate mismatch")
            prior_revalidated = True
        while cp["frontier"]:
            budget()
            if metrics["visited_nodes"] >= p["max_visited_nodes"]: raise BudgetStop("node_budget")
            prefix = cp["frontier"][-1]
            prune = derive_node_bounds(node={"prefix":prefix}, frozen_domain=d, optimization_context=c)["prune"]
            if prune:
                cp["frontier"].pop(); cp["regions"].append(dict(kind="pruned",prefix=prefix,proof=prune)); metrics["visited_nodes"] += 1
            elif len(prefix) < n:
                cp["frontier"].pop();cp["frontier"].extend([prefix+"1",prefix+"0"]); metrics["visited_nodes"] += 1
            else:
                if sum(r["kind"] == "evaluated" and r["evaluation"]["scoped_model_status"] == "model_feasible" for r in cp["regions"]) >= p["retain_feasible_limit"]:
                    raise BudgetStop("retention_budget")
                value = evaluated(prefix, "search_actual_replays")
                cp["frontier"].pop();cp["regions"].append(dict(kind="unknown" if value["scoped_model_status"] == "unknown" else "evaluated",prefix=prefix,evaluation=value)); metrics["visited_nodes"] += 1
                if value["scoped_model_status"] == "model_feasible":
                    yield dict(kind="checkpoint",checkpoint=snapshot())
            if metrics["visited_nodes"]-last_checkpoint_nodes >= 128:
                last_checkpoint_nodes=metrics["visited_nodes"];yield dict(kind="checkpoint",checkpoint=snapshot())
            else: yield dict(kind="progress",visited_nodes=metrics["visited_nodes"],actual_replays=metrics["actual_replays"])
    except BudgetStop as exc: reason = str(exc)
    layers, rows, unranked, unknown, inc = summaries(d,c,cp["regions"],cp["frontier"],False)
    if reason == "space_exhausted":
        # One fresh model pass validates all layers. The verifier does not authenticate real sources.
        cache.clear()
        class VerificationRuntime:
            def evaluate_action_set(self, *, actions):
                metrics["attempts"] += 1
                budget(True)
                metrics["actual_replays"] += 1
                metrics["verification_replays"] += 1
                return getattr(runtime,"evaluate_uncached_action_set",runtime.evaluate_action_set)(actions=actions)
            def monotonic_seconds(self): return runtime.monotonic_seconds()
            def cancel_requested(self): return runtime.cancel_requested()
            def resident_memory_mb(self): return runtime.resident_memory_mb()
        receipt=verify_layer_proof(layer_proof=seal(layers["L1"],"proof_fp"),frozen_domain=d,optimization_context=c,
            runtime=VerificationRuntime(),verification_budget=dict(max_joint_replays=p["max_joint_replays"]-metrics["actual_replays"],
                max_elapsed_seconds=max(0,p["max_elapsed_seconds"]-metrics["elapsed_seconds"]),max_memory_mb=p["max_memory_mb"]))
        if receipt["verification_status"] == "verified": layers=receipt["layers"]
        else: reason=receipt.get("reason","verification_incomplete")
    final_cp=snapshot(layers if all(x["verification_status"]=="verified" for x in layers.values()) else {})
    layers={k:seal(v,"proof_fp") for k,v in layers.items()}
    status="lexicographic_optimal" if layers["L3"]["status"]=="proven_optimal" else "first_two_levels_optimal" if layers["L2"]["status"]=="proven_optimal" else "best_found" if inc else "incomplete"
    result=dict(schema_version="existing_site_optimization_result.v1",identity=ident,optimization_status=status,termination_reason=reason,
        coverage_complete=not cp["frontier"],layers=layers,incumbent=inc,feasible_combinations=rows,unranked_model_feasible=unranked,
        unknown_leaves=unknown,pruned_regions=[r for r in cp["regions"] if r["kind"]=="pruned"],checkpoint=final_cp,metrics=deepcopy(metrics),
        prior_completed_layers=prior_layers,prior_certificate_revalidation_status="verified" if prior_revalidated else "not_verified",
        proof_scope=c["proof_scope"],real_domain_proof_status="not_verified",independent_verification_status="not_verified",**proposal_flags())
    yield dict(kind="checkpoint",checkpoint=final_cp)
    yield dict(kind="result",result=result)
