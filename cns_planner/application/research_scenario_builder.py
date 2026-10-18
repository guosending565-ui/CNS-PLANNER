"""Offline conditional sidecar builder. Never calls old qualification/model APIs.

P2-A1 extends the original synthetic-only builder with a research-side real
domain adapter (rev503 P16 candidate actions) while keeping every legacy
``requested_actions`` fixture path intact.
"""
from collections import Counter
import hashlib

from ..domain.joint_feasibility import digest, seal
from ..domain.research_scenario import bounded_json, checked_scenario, has_synthetic, TRACK

CANONICAL_PREFIX = {
    "tower:": "tower-colocation:",
    "takeoff_landing_site:": "node:",
}
MODEL_BINDINGS = {
    "C:communication": (
        ["m4_joint", "p14_corridor", "p15_corridor_gap", "p17_continuous"],
        {"m4_joint": "supported_partial", "p14_corridor": "supported_partial_geometry_only",
         "p15_corridor_gap": "supported_partial_gap_only", "p17_continuous": "supported_gate_only"},
    ),
    "S:rid_cooperative": (
        ["figure_only"],
        {"figure_only": "unsupported_figure_only_not_a_service_consumer"},
    ),
    "N:navigation_integrity_monitoring": (
        ["m4_joint", "p16_site_plan"],
        {"m4_joint": "unsupported_endpoint_new_action", "p16_site_plan": "supported_planning_only"},
    ),
    "N:rtk_augmentation": (
        ["m4_joint", "m2_rtk"],
        {"m4_joint": "supported_partial", "m2_rtk": "supported_partial"},
    ),
}
BASE_ASSUMPTIONS = (
    "ASM-ROUTE-DERIVED",
    "ASM-DEVICE-INSTANCE-CONDITIONAL",
    "ASM-INVENTORY-UNKNOWN",
    "ASM-CAPACITY-UNKNOWN",
    "ASM-COST-UNKNOWN",
)


def canonical_physical_site_id(action: dict) -> tuple[str, str]:
    """Map a P16 action site to the canonical physical site key.

    ``tower:``/``takeoff_landing_site:`` are P16 identity strings; the canonical
    keys match the 3-D consumer (``tower-colocation:``/``node:``).  No distance,
    name or device-id inference is allowed.
    """
    distinct = action.get("distinct_site_id")
    if type(distinct) is str:
        for prefix, canonical in CANONICAL_PREFIX.items():
            if distinct.startswith(prefix):
                return canonical + distinct[len(prefix):], "distinct_site_id"
    host = (action.get("host") or {}).get("host_tower_id") if type(action.get("host")) is dict else None
    if type(host) is str and host:
        return "tower-colocation:" + host, "host_tower_id"
    endpoint = action.get("takeoff_landing_site_id")
    if type(endpoint) is str and endpoint:
        return "node:" + endpoint, "takeoff_landing_site_id"
    return "unknown", "unresolved"


def conditional_instance_id(scenario_id: str, action_id: str, physical_site_id: str) -> str:
    """Stable research instance id; never an installed-asset identifier."""
    seed = "|".join((scenario_id, action_id, physical_site_id)).encode("utf-8")
    return "condinst:" + hashlib.sha256(seed).hexdigest()[:20]


def derive_plan_route_scope(*, targets, before_routes, after_routes, source_ref: str) -> dict:
    """Freeze the P16 plan-level route identity from three independent fields."""
    def route_ids(rows):
        return sorted({row.get("route_id") for row in (rows or [])
                       if type(row) is dict and type(row.get("route_id")) is str and row.get("route_id")})

    target_ids, before_ids, after_ids = route_ids(targets), route_ids(before_routes), route_ids(after_routes)
    union = sorted(set(target_ids) | set(before_ids) | set(after_ids))
    return dict(
        route_ids=union,
        target_route_ids=target_ids,
        before_route_ids=before_ids,
        after_route_ids=after_ids,
        plan_scope_unique=len(union) == 1,
        plan_scope_conflict=not (target_ids == before_ids == after_ids),
        source_ref=source_ref,
        derivation="targets[].route_id + before.routes[].route_id + after.routes[].route_id",
    )


def _route_relation(action: dict, plan_scope: dict, state_ref: str) -> dict:
    plan_ids = plan_scope.get("route_ids") or []
    if "route_ids" in action:
        explicit = action.get("route_ids")
        if type(explicit) is not list or not explicit:
            return dict(route_ids=[], route_relation_source="unresolved",
                        route_relation_status="blocked", reason="explicit_route_ids_empty_or_invalid",
                        route_relation_evidence_ref=state_ref + "#candidate_actions[].route_ids")
        outside = sorted({x for x in explicit if x not in plan_ids})
        if outside:
            return dict(route_ids=[], route_relation_source="unresolved",
                        route_relation_status="blocked",
                        reason="explicit_route_not_in_verified_plan_scope:" + ",".join(outside),
                        route_relation_evidence_ref=state_ref + "#candidate_actions[].route_ids")
        return dict(route_ids=sorted(set(explicit)), route_relation_source="explicit_action_route_ids",
                    route_relation_status="confirmed", reason=None,
                    route_relation_evidence_ref=state_ref + "#candidate_actions[].route_ids")
    if "route_id" in action and action.get("route_id") not in (None, ""):
        route_id = action.get("route_id")
        if route_id not in plan_ids:
            return dict(route_ids=[], route_relation_source="unresolved",
                        route_relation_status="blocked",
                        reason="explicit_route_not_in_verified_plan_scope:" + str(route_id),
                        route_relation_evidence_ref=state_ref + "#candidate_actions[].route_id")
        return dict(route_ids=[route_id], route_relation_source="explicit_action_route_id",
                    route_relation_status="confirmed", reason=None,
                    route_relation_evidence_ref=state_ref + "#candidate_actions[].route_id")
    if not plan_ids:
        return dict(route_ids=[], route_relation_source="unresolved",
                    route_relation_status="blocked", reason="plan_route_scope_empty",
                    route_relation_evidence_ref=state_ref + "#plan_route_scope")
    if not plan_scope.get("plan_scope_unique") or plan_scope.get("plan_scope_conflict"):
        return dict(route_ids=[], route_relation_source="unresolved",
                    route_relation_status="blocked",
                    reason=("plan_route_scope_conflict" if plan_scope.get("plan_scope_conflict")
                            else "plan_route_scope_ambiguous"),
                    route_relation_evidence_ref=state_ref + "#plan_route_scope")
    return dict(route_ids=list(plan_ids), route_relation_source="derived_from_verified_plan_scope",
                route_relation_status="derived", reason=None,
                route_relation_evidence_ref=state_ref + "#plan_route_scope")


def _model_binding(service_key: str) -> tuple[list, dict]:
    return MODEL_BINDINGS.get(service_key, ([], {}))


def normalize_conditional_actions(*, candidate_actions, plan_scope: dict, scenario_id: str,
                                  state_sha256: str, state_locator: str,
                                  selected_action_ids=()) -> dict:
    """Build the research-side action contract without touching P16 production.

    Each action keeps its original ``action_id`` and source fields, and gains a
    normalized ``route_ids[]`` plus a stable conditional device instance.  A
    route relation that cannot be proven is preserved as ``blocked`` so the
    static admission fails closed instead of silently dropping the action.
    """
    if type(candidate_actions) is not list:
        raise ValueError("candidate action list required")
    state_ref = state_locator + "@sha256:" + state_sha256
    selected = set(selected_action_ids or ())
    normalized, relation_counts, service_counts = [], Counter(), Counter()
    for action in candidate_actions:
        if type(action) is not dict:
            raise ValueError("candidate action object required")
        action_id = action.get("action_id")
        if type(action_id) is not str or not action_id:
            raise ValueError("candidate action_id required")
        physical_site_id, site_source = canonical_physical_site_id(action)
        service_key = action.get("device_service_key") or action.get("service_key") or "unknown"
        device_id = action.get("device_id")
        device_id = device_id if type(device_id) is str and device_id else "unknown"
        relation = _route_relation(action, plan_scope, state_ref)
        models, support = _model_binding(service_key)
        dependencies = action.get("dependency_action_ids")
        declared_dependencies = type(dependencies) is list
        normalized.append(dict(
            action_id=action_id,
            source_action_id=action_id,
            action_type=action.get("action_type") or action.get("action") or "unknown",
            planner_family=(action.get("planner_family") or action.get("reuse_class") or
                            action.get("action_type") or "unknown"),
            service_key=service_key,
            device_service_key=action.get("device_service_key") or service_key,
            route_ids=relation["route_ids"],
            route_relation_source=relation["route_relation_source"],
            route_relation_status=relation["route_relation_status"],
            route_relation_evidence_ref=relation["route_relation_evidence_ref"],
            route_relation_reason=relation["reason"],
            physical_site_id=physical_site_id,
            physical_site_source=site_source,
            source_site_id=action.get("site_id"),
            source_distinct_site_id=action.get("distinct_site_id"),
            host_tower_id=((action.get("host") or {}).get("host_tower_id")
                           if type(action.get("host")) is dict else action.get("tower_id")),
            coordinate=action.get("coordinate"),
            device_id=device_id,
            device_model_id=device_id,
            device_effect="new_instance",
            slot_id=None,
            device_instance_id=conditional_instance_id(scenario_id, action_id, physical_site_id),
            device_instance_evidence="research_assigned_conditional_instance_not_installed_asset",
            device_instance_evidence_level="conditional_research",
            dependency_action_ids=list(dependencies) if declared_dependencies else [],
            dependency_evidence_status="declared" if declared_dependencies else "unknown_not_declared_in_source",
            source_class="C",
            source_class_detail="rev503_p16_candidate_action",
            source_class_semantics="conditional_action_from_real_p16_candidate_installation_not_confirmed",
            historical_selected_action=action_id in selected,
            assumption_refs=list(BASE_ASSUMPTIONS),
            applicable_models=list(models),
            consumer_support=dict(support),
            equipment_selection_status=action.get("equipment_selection_status") or "unknown",
            eligibility_status=(action.get("eligibility") or {}).get("status")
                if type(action.get("eligibility")) is dict else None,
            inventory_status="unknown",
            capacity_status="unknown",
            cost_status="unknown",
            cost_unit=None,
        ))
        relation_counts[relation["route_relation_source"]] += 1
        service_counts[service_key] += 1
    ids = [row["action_id"] for row in normalized]
    instances = [(row["physical_site_id"], row["device_instance_id"]) for row in normalized]
    blocked = [row["action_id"] for row in normalized if row["route_relation_status"] == "blocked"]
    return dict(
        schema_version="research_normalized_actions.v1",
        evidence_track=TRACK,
        scenario_id=scenario_id,
        source_state_sha256=state_sha256,
        source_state_locator=state_locator,
        plan_route_scope=plan_scope,
        actions=normalized,
        selected_action_ids=sorted(selected),
        stats=dict(
            candidate_count=len(normalized),
            unique_action_ids=len(set(ids)),
            duplicate_action_ids=sorted({x for x in ids if ids.count(x) > 1}),
            selected_action_count=len(selected),
            selected_actions_are_reference_only=True,
            route_relation_counts=dict(relation_counts),
            service_counts=dict(service_counts),
            physical_site_count=len({row["physical_site_id"] for row in normalized}),
            device_instance_count=len(set(instances)),
            blocked_action_ids=sorted(blocked),
            no_implicit_action_truncation=len(normalized) == len(candidate_actions),
        ),
    )


def build_real_parent_domain(*, towers, endpoints, state_sha256: str, state_locator: str,
                             source_ref: str) -> dict:
    """Build the real 373-tower + 2-endpoint parent domain as a static review.

    Qualification/inventory/capacity/cost stay ``unknown``; the ledger only
    records physical identity and source status.
    """
    ledger = []
    for item in towers:
        tower_id = str(item.get("tower_id"))
        ledger.append(dict(
            physical_site_id="tower-colocation:" + tower_id,
            source_site_id="tower:" + tower_id,
            site_type="tower",
            coordinate=item.get("coordinate"),
            qualification=dict(status="needs_evidence", value="unknown",
                               reason="no_site_qualification_or_equipment_installation_evidence"),
            baseline_activation_status="unknown",
            source_crs=item.get("source_crs"),
            crs_confirmed=bool(item.get("crs_confirmed")),
            operator=item.get("operator"),
            inventory_status="unknown", capacity_status="unknown", cost_status="unknown",
        ))
    for endpoint in endpoints:
        ledger.append(dict(
            physical_site_id="node:" + endpoint,
            source_site_id="takeoff_landing_site:" + endpoint,
            site_type="endpoint",
            coordinate=None,
            qualification=dict(status="needs_evidence", value="unknown",
                               reason="endpoint_site_identity_only_no_device_evidence"),
            baseline_activation_status="unknown",
            source_crs=None, crs_confirmed=False, operator=None,
            inventory_status="unknown", capacity_status="unknown", cost_status="unknown",
        ))
    return dict(
        evidence_track="engineering_research",
        parent_domain_kind="rev503_real_p16_action_site_domain",
        source_state_sha256=state_sha256,
        source_state_locator=state_locator,
        source_ref=source_ref,
        site_qualification_counts=dict(Q=0, U=len(ledger), E=0),
        tower_count=len(towers),
        endpoint_count=len(endpoints),
        site_ledger=ledger,
        inventory_status="unknown", capacity_status="unknown", cost_status="unknown",
        activation_baseline_status="unknown",
        qualification_evidence="no_real_site_or_device_qualification_evidence",
    )


def build_conditional_domain(*, parent_domain_review: dict, scenario: dict, assumption_registry: list[dict]) -> dict:
    parent, entries = bounded_json([parent_domain_review, assumption_registry])
    sc = checked_scenario(scenario)
    if type(parent) is not dict or type(entries) is not list: raise ValueError("parent/registry shape")
    errors = list(sc["blockers"])
    if has_synthetic(parent): errors.append("synthetic_parent_forbidden")
    expected = [e for p in sc["policies"] for e in p["entries"]]
    if entries != expected: errors.append("assumption_registry_policy_mismatch")
    ledger = parent.get("site_ledger", [])
    if type(ledger) is not list: raise ValueError("site ledger required")
    ids = [s.get("physical_site_id") for s in ledger if type(s) is dict]
    if len(ids) != len(ledger) or any(type(x) is not str for x in ids) or len(set(ids)) != len(ids):
        errors.append("invalid_physical_site_identity")
    conditional = []
    for p in sc["policies"]:
        for e in p["entries"]:
            if e.get("parameter_kind") != "installation_qualification": continue
            sid = e.get("site_id")
            if sid not in ids: errors.append("conditional_site_missing")
            # A/B cannot confer conditional installation permission; D cannot confer any value.
            qualified = (e.get("evidence_class") == "C" and e.get("approval_ref") and
                e.get("validity") == "approved" and e.get("value") == "assumed_eligible")
            conditional.append(dict(physical_site_id=sid, parameter_id=e["parameter_id"],
                scenario_id=sc["route_scenario"]["scenario_id"], policy_id=p["policy_id"], policy_version=p["policy_version"],
                evidence_class=e["evidence_class"], source_ref=e["source_ref"],
                status="conditional_qualified_under_policy" if qualified else "unknown", value=e.get("value")))
    route = sc["route_scenario"]
    conditional_actions = route.get("conditional_actions")
    actions = conditional_actions if conditional_actions is not None else route.get("requested_actions", [])
    if type(actions) is not list: raise ValueError("requested/conditional actions list required")
    normalized_contract = conditional_actions is not None
    inventory = []
    seen_instances, pair_counts = set(), Counter()
    for action in actions:
        if type(action) is not dict: raise ValueError("action object required")
        action_id = action.get("action_id")
        site = action.get("physical_site_id")
        device = action.get("device_id")
        instance = action.get("device_instance_id")
        if normalized_contract and (not action.get("route_ids") or action.get("route_relation_status") == "blocked"):
            errors.append("action_route_relation_blocked:" + str(action_id))
        if normalized_contract and action.get("route_relation_source") not in (None, "explicit_action_route_ids",
                "explicit_action_route_id", "derived_from_verified_plan_scope"):
            errors.append("unsupported_route_relation_source:" + str(action_id))
        if normalized_contract and (type(instance) is not str or not instance):
            errors.append("device_instance_identity_required:" + str(action_id))
        key = (site, instance) if normalized_contract else (site, device)
        if key in seen_instances: errors.append("unsupported_multiplicity")
        seen_instances.add(key)
        pair_counts[(site, device)] += 1
        inventory.append(dict(
            action_id=action_id,
            physical_site_id=site,
            device_id=device,
            device_instance_id=instance,
            inventory_status="unknown",
            capacity_status="unknown",
            cost_status="unknown",
            installed_status=("conditional_not_installed" if normalized_contract else "unknown"),
            qualification_status="unknown",
            inventory_source=("not_available_in_rev503_state" if normalized_contract
                              else "legacy_fixture_no_inventory_contract"),
            cost_unit=None,
            multi_instance=bool(pair_counts[(site, device)] > 1),
        ))
    if normalized_contract:
        for (site, device), count in pair_counts.items():
            if count > 1:
                instances = {row["device_instance_id"] for row in inventory
                             if row["physical_site_id"] == site and row["device_id"] == device}
                if len(instances) != count:
                    errors.append("unsupported_multiplicity")
    route_ids = [row.get("route_ids") for row in actions]
    enumeration_complete = (type(route.get("candidate_action_count")) is int and
                            route.get("candidate_action_count") == len(actions) and
                            len({row.get("action_id") for row in actions}) == len(actions))
    return seal(dict(schema_version="conditional_domain.v1", evidence_track=TRACK, execution_enabled=False,
        status="blocked" if errors else "static_contract_passed", blockers=sorted(set(errors)),
        parent_real_domain_fingerprint=digest(parent), scenario_fingerprint=sc["scenario_fingerprint"],
        source_bundle_fingerprint=sc["source_bundle_fingerprint"],
        computational_dependency_fingerprint=digest(dict(scenario=sc["computational_dependency_fingerprint"],
            real_site_ledger=ledger, qualification=conditional, actions=actions)),
        real_qualification_snapshot=parent, real_baseline_activation=None, objective_measurement=None,
        conditional_qualification=conditional, conditional_inventory=inventory, finite_actions=actions,
        action_set_fingerprint=digest(actions),
        action_route_scope=route.get("route_scope"),
        action_route_ids_present=all(bool(row) for row in route_ids),
        assumption_registry=entries, evidence_register=sc["evidence_register"],
        completeness_axes=dict(qualification="conditional_only", installed_inventory="unknown",
            activation_baseline="unknown",
            action_enumeration=("complete_from_frozen_candidate_snapshot" if enumeration_complete
                                else "not_verified"),
            source_authentication="state_and_required_matrix_verified_tower_inventory_declared"),
        legacy_model_binding_status="unsupported_conditional_binding",
        independent_verification_status="not_verified", real_domain_proof_status="not_verified"), "domain_fingerprint")
