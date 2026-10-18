"""Real R0005 conditional-domain solver envelope (P2-A2).

This module is a pure JSON transform between the P2-A1 research scenario /
conditional domain and the nested envelope shape consumed by the existing V2
conditional lexicographic search.  It never reads ProjectState or artifacts,
never calls a model, never starts a search and never authorizes execution.

The transform is deliberately lossless:

* every frozen candidate action (42 in R0005) is carried through with its
  original ``action_id``, ``route_ids[]``, site and conditional instance;
* the 375-site real parent domain and the 22 action sites stay separate
  statistical scopes;
* unknown activation, qualification, installed inventory, capacity, cost and
  action-dependency evidence stay unknown.  Empty lists are only emitted with an
  explicit ``unknown`` status next to them, never as evidence of zero/absence;
* the source identity (state SHA256, scenario/domain/action-set fingerprints)
  is frozen inside the envelope, and an envelope built for another source is
  rejected by :func:`verify_real_solver_envelope`.
"""
from copy import deepcopy

from ..domain.existing_site_optimization import validate_proof_scope
from ..domain.joint_feasibility import digest, json_value, seal, verify
from ..domain.research_scenario import TRACK, bounded_json, checked_scenario
from .research_scenario_builder import build_conditional_domain

ENVELOPE_SCHEMA = "real_conditional_solver_envelope.v1"
ACTIVATION_POLICY_SCHEMA = "real_activation_inventory_policy.v1"
PREFLIGHT_SCHEMA = "real_domain_preflight.v1"

STALE_LAYERS = ("operational", "validation", "adoption", "operating_layer")
UNSUPPORTED_ACTION_FAMILIES = (
    ("S:rid_cooperative", "rid_figure_only_not_a_service_consumer"),
    ("S:radar_noncooperative", "radar_disabled_unsupported"),
    ("N:navigation_integrity_monitoring", "endpoint_new_action_unsupported"),
)
UNSUPPORTED_PLANNER_FAMILIES = (
    ("endpoint_integrity_monitor", "endpoint_new_action_unsupported"),
    ("directional_radar", "radar_disabled_unsupported"),
)
ACTIVATION_MISSING_REASON = (
    "rev503 has no real activation baseline or installed-device inventory for "
    "the 22 conditional action sites"
)


def _source_rows(scenario):
    """Return the frozen source rows keyed by source_id."""
    return {row.get("source_id"): row
            for row in scenario["regional_bundle"].get("sources", [])
            if type(row) is dict}


def _frozen_source_identity(scenario, domain):
    """Derive the immutable source identity used by the envelope."""
    route = scenario["route_scenario"]
    parent = domain["real_qualification_snapshot"]
    rows = _source_rows(scenario)
    state = rows.get("rev503_project_state") or {}
    baseline = route.get("baseline_ref") or {}
    source_sha = baseline.get("sha256")
    if not (type(source_sha) is str and len(source_sha) == 64):
        raise ValueError("frozen baseline SHA256 required")
    if state.get("sha256") != source_sha:
        raise ValueError("scenario source row does not match baseline_ref")
    if parent.get("source_state_sha256") != source_sha:
        raise ValueError("parent domain source SHA mismatch")
    route_parent = route.get("parent_real_domain")
    if route_parent is not None and route_parent != parent:
        raise ValueError("scenario parent domain is not the conditional domain parent")
    if route.get("parent_real_domain_fingerprint") not in (None, digest(parent)):
        raise ValueError("scenario parent domain fingerprint mismatch")
    return dict(
        source_state_sha256=source_sha,
        source_state_revision=baseline.get("revision"),
        source_state_locator=baseline.get("source_ref"),
        source_state_bytes=state.get("bytes"),
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        scenario_fingerprint=scenario["scenario_fingerprint"],
        conditional_domain_fingerprint=domain["domain_fingerprint"],
        parent_real_domain_fingerprint=digest(parent),
        action_set_fingerprint=domain["action_set_fingerprint"],
        computational_dependency_fingerprint=domain["computational_dependency_fingerprint"],
        model_versions=deepcopy(route.get("model_versions") or {}),
        source_rows=dict((key, dict(source_id=row.get("source_id"),
            sha256=row.get("sha256"), bytes=row.get("bytes"),
            version=row.get("version"), locator=row.get("locator")))
            for key, row in sorted(rows.items())),
    )


def _conditional_sites(domain):
    """Build the strict per-site conditional baseline projection."""
    sites = []
    for row in domain.get("conditional_qualification", []):
        qualified = row.get("status") == "conditional_qualified_under_policy"
        cites = [row["parameter_id"]] if qualified else []
        sites.append(dict(
            physical_site_id=row.get("physical_site_id"),
            conditional_qualification=("qualified_under_policy" if qualified
                                       else "unknown"),
            conditional_activation_baseline="unknown",
            baseline_source_ref=None,
            baseline_assumption_refs=cites,
            missing_reason=ACTIVATION_MISSING_REASON,
        ))
    return sites


def _consumer_binding(scenario):
    """Freeze the scenario consumer set as the V2 consumer-binding contract."""
    route = scenario["route_scenario"]
    scope = route.get("required_scope") or {}
    consumers = []
    for dependency in route.get("consumer_dependencies", []):
        consumers.append(dict(
            consumer_id=dependency.get("consumer_id"),
            method_ref="scenario.consumer_dependencies[" + str(
                dependency.get("consumer_id")) + "]",
            method_version=dependency.get("model_version"),
        ))
    unsupported_actions = [dict(family=family, reason=reason,
        source_ref="scenario.route_scenario.consumer_registry")
        for family, reason in UNSUPPORTED_ACTION_FAMILIES]
    unsupported_planners = [dict(planner=planner, reason=reason,
        source_ref="scenario.route_scenario.consumer_registry")
        for planner, reason in UNSUPPORTED_PLANNER_FAMILIES]
    navigation = {key: "unknown" for key in
        ("provider_inventory_status", "device_status", "handover_status",
         "speed_profile_status", "backup_integrity_status")}
    navigation["source_ref"] = ("scenario.route_scenario.snapshot_declarations"
                                "#navigation_unknown")
    return dict(
        consumers=consumers,
        required_inventory=deepcopy(scope.get("required_constraint_inventory")),
        included_constraint_ids=deepcopy(scope.get("included_constraint_ids")),
        excluded_constraints=deepcopy(scope.get("excluded_constraints")),
        unconsumed_requirements=deepcopy(scope.get("unconsumed_requirements")),
        unsupported_action_families=unsupported_actions,
        unsupported_planner_families=unsupported_planners,
        navigation_evidence=navigation,
    )


def _enumeration(actions):
    count = len(actions)
    return dict(
        kind="explicit_complete_finite",
        complete=True,
        action_count=count,
        subset_count=2 ** count,
        basis_ref="rev503.cns_corridor_site_plan.candidate_actions",
        budget_audit=dict(
            leaf_evaluations=2 ** count,
            node_upper_bound=2 * 2 ** count - 1,
            basis_ref=("mathematical complete binary tree over the frozen "
                       "candidate snapshot; not an approved execution budget"),
        ),
    )


def _pruning_rules():
    return dict(incompatible_action_pairs=[], structural_source_ref=None,
        max_selected_actions=None, max_selected_actions_source_ref=None)


def _proof_scope(scenario, domain, actions):
    route = scenario["route_scenario"]
    scope = route.get("required_scope") or {}
    return dict(
        kind="frozen_model_action_domain",
        domain_scope=dict(
            scope_id=route.get("scenario_id"),
            action_count=len(actions),
            action_set_fingerprint=domain["action_set_fingerprint"],
            conditional_site_count=len(domain.get("conditional_qualification", [])),
            parent_site_count=len(
                (domain["real_qualification_snapshot"] or {}).get("site_ledger", [])),
        ),
        constraint_scope=dict(
            required_inventory_count=len(scope.get("required_constraint_inventory") or []),
            included_constraint_count=len(scope.get("included_constraint_ids") or []),
            excluded_constraint_count=len(scope.get("excluded_constraints") or []),
            unconsumed_requirement_count=len(scope.get("unconsumed_requirements") or []),
            complete_required_cns_status="unknown",
        ),
    )


def _activation_policy(domain):
    """Keep activation/inventory unknown; never encode them as zero."""
    return seal(dict(
        schema_version=ACTIVATION_POLICY_SCHEMA,
        status="unknown",
        activation_baseline_source="not_available_in_rev503_state",
        installed_inventory_status="unknown",
        installed_device_instances_status="unknown_not_enumerated",
        installed_device_instances=[],
        l1_newly_activated_site_count=dict(status="unknown", value=None,
            bounds=None, reason=ACTIVATION_MISSING_REASON),
        l2_new_device_instance_count=dict(status="unknown", value=None,
            bounds=None, reason=ACTIVATION_MISSING_REASON),
        conditional_instance_ids_are_not_installed_devices=True,
        applied_policy_assumptions=[],
        forbidden_defaults=["all_candidate_sites_inactive",
                            "installed_inventory_is_empty",
                            "conditional_instance_is_installed_device"],
    ), "policy_fingerprint")


def _execution_blockers(scenario, domain, actions):
    """Reproduce the P2-A1 execution blockers from the frozen inputs.

    This is a derivation, not an authorization: every item remains a blocker.
    """
    route = scenario["route_scenario"]
    blockers = []
    stale = route.get("snapshot_mode") == "stale_declared"
    if stale:
        for layer in STALE_LAYERS:
            blockers.append("route_evidence_stale_declared:" + layer)
        blockers.append("p16_native_action_route_ids_absent")
    if any(row.get("dependency_evidence_status") not in (None, "declared")
           for row in actions):
        blockers.append("action_dependency_evidence_unknown")
    if stale:
        blockers.extend(("device_qualification_unknown",
                         "installed_inventory_unknown",
                         "device_capacity_unknown",
                         "cost_unknown"))
    families = {row.get("service_key") for row in actions}
    if "S:rid_cooperative" in families:
        blockers.append("consumer_unsupported:S:rid_cooperative")
    if "N:navigation_integrity_monitoring" in families:
        blockers.append("consumer_unsupported:N:navigation_integrity_monitoring")
    scope = route.get("required_scope") or {}
    for row in scope.get("unconsumed_requirements") or []:
        if type(row) is dict and row.get("constraint_id"):
            blockers.append("required_unconsumed:" + str(row["constraint_id"]))
    blockers.append("research_execution_not_authorized")
    return sorted(set(blockers))


def _search_budget(search_budget):
    if search_budget is None:
        return dict(status="not_approved", policy=None, approved_by=None,
            approved_at=None, max_actions=None, max_visited_nodes=None,
            max_joint_replays=None, max_elapsed_seconds=None,
            max_memory_mb=None)
    if type(search_budget) is not dict:
        raise ValueError("search budget object required")
    budget = deepcopy(search_budget)
    # The envelope builder can freeze a future approval, but it can never
    # fabricate one: an approved budget must carry the explicit approval fields.
    if budget.get("status") != "approved":
        raise ValueError("only an explicit approved budget may be frozen")
    for key in ("approved_by", "approved_at", "max_actions", "max_visited_nodes",
                "max_joint_replays", "max_elapsed_seconds", "max_memory_mb"):
        if budget.get(key) in (None, ""):
            raise ValueError("approved budget identity missing:" + key)
    return budget


def build_real_solver_envelope(*, scenario, conditional_domain,
                               expected_source_sha256=None, search_budget=None):
    """Transform the frozen P2-A1 real conditional domain into a V2 envelope."""
    sc = checked_scenario(scenario)
    domain = bounded_json(conditional_domain)
    if type(domain) is not dict:
        raise ValueError("conditional domain object required")
    verify(domain, "domain_fingerprint")
    if domain.get("schema_version") != "conditional_domain.v1":
        raise ValueError("conditional domain schema")
    if domain.get("scenario_fingerprint") != sc["scenario_fingerprint"]:
        raise ValueError("conditional domain scenario identity mismatch")
    rebuilt = build_conditional_domain(
        parent_domain_review=domain["real_qualification_snapshot"],
        scenario=sc, assumption_registry=domain["assumption_registry"])
    if rebuilt != domain:
        raise ValueError("conditional domain canonical identity mismatch")
    parent = domain["real_qualification_snapshot"]
    if domain.get("parent_real_domain_fingerprint") != digest(parent):
        raise ValueError("real parent domain fingerprint mismatch")
    identity = _frozen_source_identity(sc, domain)
    if (expected_source_sha256 is not None and
            identity["source_state_sha256"] != expected_source_sha256):
        raise ValueError("real envelope source SHA mismatch")
    actions = deepcopy(domain["finite_actions"])
    ids = [row.get("action_id") for row in actions]
    if ids != sorted(set(ids)):
        raise ValueError("stable complete action order required")
    if len(actions) != sc["route_scenario"].get("candidate_action_count"):
        raise ValueError("action count differs from frozen candidate count")
    sites = _conditional_sites(domain)
    if len({row["physical_site_id"] for row in sites}) != len(sites):
        raise ValueError("duplicate conditional site")
    if len(sites) != len(domain.get("conditional_qualification", [])):
        raise ValueError("conditional site coverage mismatch")
    policy = _activation_policy(domain)
    proof_scope = _proof_scope(sc, domain, actions)
    if validate_proof_scope(proof_scope) != proof_scope:
        raise ValueError("proof scope canonical identity mismatch")
    blockers = _execution_blockers(sc, domain, actions)
    budget = _search_budget(search_budget)
    payload = dict(
        schema_version=ENVELOPE_SCHEMA,
        evidence_track=TRACK,
        execution_enabled=False,
        execution_authorization="not_authorized",
        contract_status="real_domain_contract_ready",
        status="real_domain_contract_ready",
        blockers=[],
        execution_blockers=blockers,
        scenario=sc,
        conditional_domain=domain,
        real_parent_domain=parent,
        actions=actions,
        conditional_sites=sites,
        objective_policy=None,
        objective_policy_status="unavailable_no_approved_cost_basis",
        consumer_binding=_consumer_binding(sc),
        enumeration=_enumeration(actions),
        pruning_rules=_pruning_rules(),
        pruning_evidence_status=("unknown_no_pairs_declared_not_proven_absent"),
        dependency_evidence_status="unknown",
        dependency_closure_proven=False,
        proof_scope=proof_scope,
        proof_scope_status="conditional_lexicographic_only_not_complete_required",
        installed_inventory_status="unknown",
        installed_device_instances=[],
        installed_device_instances_status="unknown_not_enumerated",
        activation_policy=policy,
        evidence_track_contract="research_real_domain_envelope_never_executes",
        search_budget=budget,
        search_budget_status=budget["status"],
        fixture=None,
        m3_independent_verification_status="not_verified",
        m6_status="closed",
        source_identity=identity,
        admission_execution_blockers=deepcopy(blockers),
        final_eligible=False,
    )
    return seal(payload, "envelope_fingerprint")


def verify_real_solver_envelope(envelope, *, scenario=None,
                                conditional_domain=None,
                                expected_source_sha256=None):
    """Strictly verify a real-domain envelope against its frozen identity.

    Any source, scenario, domain or action-set identity change rejects the old
    envelope instead of silently adapting it to new inputs.
    """
    env = json_value(envelope)
    if type(env) is not dict:
        raise ValueError("real solver envelope object required")
    verify(env, "envelope_fingerprint")
    if env.get("schema_version") != ENVELOPE_SCHEMA:
        raise ValueError("real solver envelope schema")
    if env.get("evidence_track") != TRACK:
        raise ValueError("real solver envelope evidence track")
    if env.get("execution_enabled") is not False:
        raise ValueError("real solver envelope must stay execution-disabled")
    if env.get("execution_authorization") != "not_authorized":
        raise ValueError("real solver envelope cannot authorize execution")
    if env.get("fixture") is not None:
        raise ValueError("real solver envelope cannot carry a synthetic fixture")
    identity = env.get("source_identity") or {}
    embedded = checked_scenario(env.get("scenario"))
    if embedded["scenario_fingerprint"] != identity.get("scenario_fingerprint"):
        raise ValueError("envelope scenario fingerprint mismatch")
    if embedded["source_bundle_fingerprint"] != identity.get("source_bundle_fingerprint"):
        raise ValueError("envelope source bundle fingerprint mismatch")
    if embedded["route_scenario"]["baseline_ref"]["sha256"] != \
            identity.get("source_state_sha256"):
        raise ValueError("envelope source SHA mismatch")
    conditional = json_value(env.get("conditional_domain"))
    verify(conditional, "domain_fingerprint")
    if conditional["domain_fingerprint"] != identity.get("conditional_domain_fingerprint"):
        raise ValueError("envelope conditional domain fingerprint mismatch")
    if conditional["scenario_fingerprint"] != embedded["scenario_fingerprint"]:
        raise ValueError("envelope conditional domain scenario mismatch")
    parent = env.get("real_parent_domain")
    if digest(parent) != identity.get("parent_real_domain_fingerprint"):
        raise ValueError("envelope parent domain fingerprint mismatch")
    if conditional["parent_real_domain_fingerprint"] != digest(parent):
        raise ValueError("envelope parent/conditional domain mismatch")
    actions = env.get("actions")
    if actions != conditional.get("finite_actions"):
        raise ValueError("envelope action set is not the frozen conditional action set")
    if digest(actions) != identity.get("action_set_fingerprint"):
        raise ValueError("envelope action set fingerprint mismatch")
    if expected_source_sha256 is not None and \
            identity.get("source_state_sha256") != expected_source_sha256:
        raise ValueError("real envelope source SHA mismatch")
    if scenario is not None:
        fresh = checked_scenario(scenario)
        if fresh["scenario_fingerprint"] != identity.get("scenario_fingerprint"):
            raise ValueError("real envelope belongs to another scenario")
        if fresh["route_scenario"]["baseline_ref"]["sha256"] != \
                identity.get("source_state_sha256"):
            raise ValueError("real envelope belongs to another source SHA")
    if conditional_domain is not None:
        fresh_domain = json_value(conditional_domain)
        verify(fresh_domain, "domain_fingerprint")
        if fresh_domain["domain_fingerprint"] != identity.get(
                "conditional_domain_fingerprint"):
            raise ValueError("real envelope belongs to another conditional domain")
    return env


__all__ = ["ENVELOPE_SCHEMA", "ACTIVATION_POLICY_SCHEMA", "PREFLIGHT_SCHEMA",
    "build_real_solver_envelope", "verify_real_solver_envelope"]
