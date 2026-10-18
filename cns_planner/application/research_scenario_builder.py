"""Offline conditional sidecar builder. Never calls old qualification/model APIs."""
from ..domain.joint_feasibility import digest, seal
from ..domain.research_scenario import bounded_json, checked_scenario, has_synthetic, TRACK


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
    actions = sc["route_scenario"].get("requested_actions", [])
    if type(actions) is not list: raise ValueError("requested actions list required")
    seen = set()
    for action in actions:
        if type(action) is not dict: raise ValueError("action object required")
        key = (action.get("physical_site_id"), action.get("device_id"))
        if key in seen: errors.append("unsupported_multiplicity")
        seen.add(key)
    return seal(dict(schema_version="conditional_domain.v1", evidence_track=TRACK, execution_enabled=False,
        status="blocked" if errors else "static_contract_passed", blockers=sorted(set(errors)),
        parent_real_domain_fingerprint=digest(parent), scenario_fingerprint=sc["scenario_fingerprint"],
        source_bundle_fingerprint=sc["source_bundle_fingerprint"],
        computational_dependency_fingerprint=digest(dict(scenario=sc["computational_dependency_fingerprint"],
            real_site_ledger=ledger, qualification=conditional, actions=actions)),
        real_qualification_snapshot=parent, real_baseline_activation=None, objective_measurement=None,
        conditional_qualification=conditional, conditional_inventory=[], finite_actions=actions,
        assumption_registry=entries, evidence_register=sc["evidence_register"],
        completeness_axes=dict(qualification="conditional_only", installed_inventory="unknown",
            activation_baseline="unknown", action_enumeration="not_verified", source_authentication="not_verified"),
        legacy_model_binding_status="unsupported_conditional_binding",
        independent_verification_status="not_verified", real_domain_proof_status="not_verified"), "domain_fingerprint")
