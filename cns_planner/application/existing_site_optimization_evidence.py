"""M5-D1 value-only admission and fresh full-action model replay.

Real execution is deliberately unavailable: no JSON approval, factory, receipt,
session, writer or runtime can open it. Synthetic replay validates the declared
model only; it never authenticates sources or independent execution.
"""
from copy import deepcopy
from pathlib import PureWindowsPath
from time import monotonic
from ..domain.joint_feasibility import json_value, digest, seal, verify
from ..domain.existing_site_optimization import proposal_flags, measure_objectives
from .existing_site_action_binding import bind_device_actions, checked_review, _synthetic, RTK, RADAR, ENDPOINT
from .existing_site_evaluation_adapter import ExistingSiteEvaluator, _segment_evidence, REAL_RTK_GAPS
from .candidate_ue_acceptability import candidate_action_set_fingerprint
from .joint_candidate_domain import validate_actions

LIMITS = dict(max_joint_replays=100, max_elapsed_seconds=900, max_nodes=100000,
              max_memory_mb=4096, max_storage_bytes=64*1024*1024)
USAGE = dict(joint_replays="max_joint_replays", elapsed_seconds="max_elapsed_seconds",
             nodes="max_nodes", storage_bytes="max_storage_bytes")


def navigation_evidence_status(evaluation):
    """Keep the unchanged native M2 result, with B's stricter evidence gate."""
    return _segment_evidence(json_value(evaluation))


def _budget_errors(spec):
    budget, used = spec.get("budget", {}), spec.get("consumed", {})
    errors = []
    if not isinstance(budget, dict) or not isinstance(used, dict):
        return ["invalid_budget_or_cumulative_usage_container"]
    for key, ceiling in LIMITS.items():
        value = budget.get(key)
        if type(value) not in (int, float) or value <= 0 or value > ceiling:
            errors.append("invalid_budget:" + key)
        elif key not in ("max_elapsed_seconds", "max_memory_mb") and type(value) is not int:
            errors.append("invalid_budget:" + key)
    for key, limit in USAGE.items():
        value = used.get(key)
        if type(value) not in (int, float) or value < 0:
            errors.append("invalid_cumulative_usage:" + key)
        elif key != "elapsed_seconds" and type(value) is not int:
            errors.append("invalid_cumulative_usage:" + key)
        elif type(budget.get(limit)) in (int, float) and value >= budget[limit]:
            errors.append("cumulative_budget_exhausted:" + key)
    return errors


def _output_errors(value):
    if not isinstance(value, str) or not value:
        return ["isolated_proposal_output_root_required"]
    path = PureWindowsPath(value)
    parts = [p.lower() for p in path.parts]
    if (not path.is_absolute() or ".." in parts or
            any(p in ("projects", "artifacts", "projectstate.json", ".git") for p in parts) or
            not any(parts[i:i+2] == ["proposals", "m5"] for i in range(len(parts)-2))):
        return ["isolated_proposal_output_root_required"]
    # D1 never opens or writes this locator. Future real workers must validate
    # filesystem containment/reparse points themselves before any output write.
    return []


def build_readonly_run(*, domain_review, frozen_context, objective_policy,
                       scope_declaration, approved_run_spec):
    review, context, policy, scope, spec = json_value(
        [domain_review, frozen_context, objective_policy, scope_declaration, approved_run_spec])
    blockers, gaps = [], []
    track = context.get("evidence_track")
    synthetic = track == "synthetic_fixture" and spec.get("execution_mode") == "synthetic_test"
    binding = None
    domain = review.get("frozen_domain", {})
    try:
        review, domain = checked_review(review)
        verify(context, "context_fingerprint")
        verify(context["assessment_scope"], "scope_fingerprint")
        if (context["frozen_domain"] != domain or context["domain_fingerprint"] != domain["domain_fingerprint"]
                or context["scope_fingerprint"] != context["assessment_scope"]["scope_fingerprint"]
                or track != domain["evidence_track"] or scope.get("evidence_track") != track):
            raise ValueError("frozen context/domain/track mismatch")
        if scope.get("assessment_scope") != context["assessment_scope"]:
            blockers.append("declared_scope_mismatch")
        selected = set(context["assessment_scope"]["included_constraint_ids"])
        inventory = context["assessment_scope"]["required_constraint_inventory"]
        for row in inventory:
            if row["constraint_id"] in selected:
                consumer = row.get("consumer", {})
                kind = consumer.get("kind")
                if kind not in ("p14_service", "p17_service", "m2_navigation") or not consumer.get("service_key"):
                    blockers.append("unsupported_constraint_consumer:" + row["constraint_id"])
        if selected - {row["constraint_id"] for row in inventory}:
            blockers.append("included_constraint_inventory_missing")
        measure_objectives(actions=[], frozen_domain=domain, objective_policy=policy,
                           activation_predictions={}, participation_evidence={})
        action_limit = spec.get("synthetic_action_limit", 12)
        if (type(action_limit) is not int or not 1 <= action_limit <= 12 or
                len(domain["installable_actions"]) > action_limit):
            blockers.append("synthetic_action_limit_exceeded")
        binding = bind_device_actions(planning_units=scope.get("planning_units", []),
            selection_bindings=scope.get("selection_bindings", []), domain_review=review,
            model_capabilities=scope.get("model_capabilities", {}), scope_declaration=scope)
        if not scope.get("planning_units") or binding["binding_status"] == "blocked":
            blockers.append("action_binding_incomplete")
        if {a["action_id"] for a in binding["bound_actions"]} != {a["action_id"] for a in domain["installable_actions"]}:
            blockers.append("full_action_domain_binding_incomplete")
        gaps.extend(binding["blocked_actions"])
        # Constructor validation only. No model is evaluated at admission.
        ExistingSiteEvaluator(frozen_context=context, frozen_domain=domain, domain_review=review,
                              objective_policy=policy, evidence_registry={})
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        blockers.append("input_inconsistent:" + str(exc))
    blockers.extend(_budget_errors(spec))
    blockers.extend(_output_errors(spec.get("output_root")))
    proof = spec.get("proof_scope")
    if not isinstance(proof, dict) or proof.get("kind") != "finite_synthetic_domain":
        blockers.append("optimizer_finite_synthetic_domain_only")
    snapshot = context.get("frozen_rule_snapshot", {})
    unconsumed = deepcopy(snapshot.get("unconsumed_requirements", []))
    declared = context.get("assessment_scope", {})
    gaps.extend([dict(reason_code="unconsumed_requirement", requirement=r) for r in unconsumed])
    gaps.extend([dict(reason_code="excluded_constraint", constraint=r) for r in declared.get("excluded_constraints", [])])
    gaps.extend([dict(reason_code="real_rtk_contract_missing", field=f) for f in REAL_RTK_GAPS])
    gaps.extend([dict(reason_code="native_rtk_equipment_not_selected"),
                 dict(reason_code="source_authentication_not_verified"),
                 dict(reason_code="independent_execution_not_verified")])
    if not synthetic:
        blockers.extend(["separate_real_run_authorization_required", "real_execution_disabled_in_d1",
            "controlled_interruptible_worker_unavailable", "optimizer_finite_synthetic_domain_only",
            "trusted_source_verifier_unavailable", "trusted_independent_executor_unavailable",
            "real_qualification_model_instance_slot_activation_evidence_incomplete",
            "real_rtk_segment_contract_unavailable", "native_rtk_equipment_not_selected"])
        if _synthetic([domain, context.get("baseline_inputs"), scope, policy]):
            blockers.append("synthetic_inputs_on_real_track")
        if unconsumed: blockers.append("required_constraints_unconsumed")
        if declared.get("excluded_constraints"): blockers.append("complete_required_scope_not_consumed")
        if scope.get("fixed_configurations"):
            blockers.append("fixed_configuration_user_approval_not_authenticated")
    for unit in scope.get("planning_units", []):
        family = unit.get("service_key")
        if family in (RADAR, ENDPOINT):
            blockers.append("unsupported_new_action_family:" + family)
    ready = not blockers
    frozen = dict(domain_review=review, frozen_context=context, objective_policy=policy,
                  scope_declaration=scope, approved_run_spec=spec)
    return seal(dict(schema_version="existing_site_readonly_run.v1", status="ready" if ready else "blocked",
        frozen_inputs=frozen, frozen_input_identity=dict(context_fingerprint=context.get("context_fingerprint"),
            domain_fingerprint=domain.get("domain_fingerprint"), scope_fingerprint=context.get("scope_fingerprint"),
            source_manifest_fingerprint=digest(review.get("source_manifest")), objective_policy_fingerprint=digest(policy),
            evaluator_versions=deepcopy(context.get("evaluator_versions")),
            route_id=snapshot.get("route_id"), operating_layer_id=snapshot.get("frozen_inputs", {}).get("operation_scenario", {}).get("operating_layer_id")),
        original_requirement_scope=dict(assessment_scope=deepcopy(declared),scope_declaration=scope,
            frozen_rule_snapshot=deepcopy(snapshot)),
        executable_scope=dict(enabled=ready, kind="finite_synthetic_domain", evidence_track=track,
            included_constraint_ids=deepcopy(declared.get("included_constraint_ids", [])),
            limitation="explicit_synthetic_model_only_no_real_execution"),
        binding=binding, source_gaps=gaps, unconsumed_requirements=unconsumed,
        native_consumption_contract=dict(m4="fixed P14/P15/P17 constructors; existing value snapshot fields only",
            m2="P15 segments plus speed_profile; no new real segment/backup propagation",
            rtk_equipment_selection="not_selected", communication_max_gap_m="unconsumed",
            radar_new_actions="unsupported", endpoint_new_actions="unsupported"),
        budget_enforcement="synthetic bounded fixture only; no hard interruptible real worker",
        cumulative_usage_authentication_status="not_verified",
        budget=deepcopy(spec.get("budget")), cumulative_consumed=deepcopy(spec.get("consumed")),
        output_root=spec.get("output_root"), output_policy="locator_only_no_filesystem_writes",
        blockers=sorted(set(blockers)), evidence_status="evidence_incomplete",
        independent_verification_status="not_verified", real_domain_proof_status="not_verified", **proposal_flags()), "run_spec_fp")


def independently_replay_proposal(*, proposal, run_spec, evidence_registry, verification_receipt=None):
    # No injected runtime, cache, session or writer. Receipts remain declarations.
    proposal, run, registry, receipt = json_value([proposal, run_spec, evidence_registry, verification_receipt])
    out = dict(schema_version="existing_site_independent_replay.v1", replay_status="blocked", actual_full_replays=0,
        cache_hits=0, scoped_model_status="unknown", complete_required_cns_status="unknown",
        evidence_status="evidence_incomplete", independent_verification_status="not_verified",
        real_domain_proof_status="not_verified", navigation_status="unknown", evaluation=None,
        verification_receipt=receipt, reason_codes=["trusted_independent_execution_and_source_verifiers_unavailable"],
        final_research_admission=dict(eligible=False,status="blocked",blockers=["independent_execution_not_verified"]),
        **proposal_flags())
    try:
        verify(run, "run_spec_fp")
        rebuilt = build_readonly_run(**run["frozen_inputs"])
        if rebuilt["status"] != "ready":
            out["reason_codes"].extend(rebuilt["blockers"])
            return out
        if rebuilt != run: raise ValueError("run specification identity mismatch")
        # Re-check hard code boundary, even if a caller re-seals a fake ready run.
        args = run["frozen_inputs"]
        if args["frozen_context"]["evidence_track"] != "synthetic_fixture":
            raise ValueError("real execution disabled")
        verify(proposal, "evaluation_fp")
        domain = args["domain_review"]["frozen_domain"]
        actions = validate_actions(actions=proposal["actions"], frozen_domain=domain)
        if proposal.get("action_set_fingerprint") != candidate_action_set_fingerprint(actions=actions):
            raise ValueError("action fingerprint mismatch")
        if proposal.get("frozen_context") != args["frozen_context"] or proposal.get("domain_review") != args["domain_review"]:
            raise ValueError("proposal frozen identity mismatch")
        start = monotonic()
        evaluator = ExistingSiteEvaluator(frozen_context=args["frozen_context"], frozen_domain=domain,
            domain_review=args["domain_review"], objective_policy=args["objective_policy"], evidence_registry=registry)
        evaluation = evaluator.evaluate_action_set(actions=actions)
        out["actual_full_replays"] = 1
        elapsed = monotonic()-start
        used = deepcopy(run["cumulative_consumed"])
        used["joint_replays"] += 1; used["elapsed_seconds"] += elapsed
        out["cumulative_consumed"] = used
        if used["elapsed_seconds"] > run["budget"]["max_elapsed_seconds"]:
            raise ValueError("cumulative elapsed budget exhausted during synthetic replay")
        # Equal summaries/run_ids cannot replace native full-X/parent/source checks.
        fields = ("actions", "action_set_fingerprint", "applied_action_ids", "context_fingerprint",
            "binding_identity", "native_materials", "native_results", "candidate_assessment", "m3_assessment",
            "rtk_joint_inputs", "rtk_joint_result", "scoped_model_status", "complete_required_cns_status",
            "objective_measurement", "unconsumed_requirements", "input_status", "identity_errors",
            "evidence_status", "independent_verification_status", "real_domain_proof_status",
            "final_research_admission", "rtk_segment_evidence")
        mismatch = [key for key in fields if proposal.get(key) != evaluation.get(key)]
        if mismatch: raise ValueError("full replay mismatch:" + ",".join(mismatch))
        if evaluation.get("evaluation_status") != "evaluated" or evaluation.get("identity_errors"):
            raise ValueError("native replay unavailable or inconsistent")
        nav = navigation_evidence_status(evaluation)
        out.update(replay_status="replayed", evaluation=evaluation,
            scoped_model_status=evaluation["scoped_model_status"],
            complete_required_cns_status=evaluation["complete_required_cns_status"],
            navigation_status=nav["navigation_status"],rtk_segment_evidence=nav,
            model_validation_scope=deepcopy(run["executable_scope"]),
            full_action_set_fingerprint=evaluation["action_set_fingerprint"],
            native_chain_replays=evaluator.model.replay_count)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        out.update(replay_status="rejected", scoped_model_status="unknown")
        out["reason_codes"].append(str(exc))
    return out
