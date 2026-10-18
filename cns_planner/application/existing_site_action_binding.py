"""M5-B explicit selection sidecar. Finite JSON only; no source or state I/O.

A binding references an exact frozen M4 action; it never manufactures a model,
slot, instance, qualification, or changes a requested family into fixed scope.
Source references are declarations, not authentication receipts.
"""
from ..domain.joint_feasibility import json_value, seal, verify
from ..domain.existing_site_optimization import proposal_flags
from .joint_candidate_domain import validate_actions

C = "C:communication"
RTK = "N:rtk_augmentation"
RID = "S:rid_cooperative"
RADAR = "S:radar_noncooperative"
ENDPOINT = "N:navigation_integrity_monitoring"
FAMILIES = (C, RTK, RID, RADAR, ENDPOINT)
SOURCE_FIELDS = ("catalog_source_ref", "capability_source_ref", "qualification_source_ref",
                 "installation_permission_source_ref", "capacity_source_ref")
IDENTITY_FIELDS = ("device_id", "device_instance_id", "physical_site_id", "slot_id")


def action_family(row):
    # An explicit planner identity takes precedence over a conflicting service tag.
    if row.get("planner_family") == "endpoint_integrity_monitor": return ENDPOINT
    if row.get("planner_family") in ("radar_panel", "radar_surveillance"): return RADAR
    return row.get("service_key") or row.get("device_service_key")


def family_support_matrix(*, scope_declaration, model_capabilities):
    scope, capabilities = json_value([scope_declaration, model_capabilities])
    if capabilities.get("model_version") != "1.0": raise ValueError("unsupported model version")
    fixed = scope.get("fixed_configurations", [])
    if not isinstance(fixed, list): raise ValueError("fixed configurations must be explicit records")
    result = {}
    for family in FAMILIES:
        ordinary = family in (C, RTK, RID)
        approved = any(r.get("service_key") == family and r.get("source_ref") and
                       r.get("configuration_fingerprint") for r in fixed)
        result[family] = dict(action_support="supported" if ordinary else "unsupported",
            evaluation_support="shared_model" if ordinary else
                "fixed_configuration_only" if approved else "fixed_configuration_requires_explicit_scope",
            limitations=["communication.max_gap_m=1000_unconsumed"] if family == C else
                ["real_segment_contract_incomplete"] if family == RTK else
                ["does_not_replace_radar"] if family == RID else ["new_actions_unsupported"],
            fixed_scope_references=[r for r in fixed if r.get("service_key") == family])
    return result


def _synthetic(value):
    if isinstance(value, dict):
        if value.get("evidence_track") == "synthetic_fixture" or value.get("synthetic_fixture") is True:
            return True
        if value.get("equipment_selection_status") == "synthetic_fixture_selected": return True
        return any(_synthetic(v) for v in value.values())
    if isinstance(value, list): return any(_synthetic(v) for v in value)
    return False


def checked_review(domain_review):
    review = json_value(domain_review)
    if review.get("schema_version") != "existing_site_domain_review.v1": raise ValueError("domain review schema")
    domain = review.get("frozen_domain", {})
    verify(domain, "domain_fingerprint")
    if review.get("original_domain_fingerprint") != domain["domain_fingerprint"]:
        raise ValueError("domain review identity mismatch")
    return review, domain


def bind_device_actions(*, planning_units, selection_bindings, domain_review,
                        model_capabilities, scope_declaration):
    units, bindings, scope = json_value([planning_units, selection_bindings, scope_declaration])
    review, domain = checked_review(domain_review)
    matrix = family_support_matrix(scope_declaration=scope, model_capabilities=model_capabilities)
    track = scope.get("evidence_track")
    if track != domain["evidence_track"]: raise ValueError("evidence track mismatch")
    if not track: raise ValueError("evidence track required")
    for rows, field in ((units, "planning_unit_id"), (bindings, "planning_unit_id"), (bindings, "action_id")):
        ids = [r.get(field) for r in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("missing or duplicate binding identity: " + field)
    if {r["planning_unit_id"] for r in bindings} - {r["planning_unit_id"] for r in units}:
        raise ValueError("binding without planning unit")
    selected = {r["planning_unit_id"]: r for r in bindings}
    actions = {r["action_id"]: r for r in domain["installable_actions"]}
    catalog = {r["device_id"]: r for r in domain["device_catalog"]["items"]}
    installed = domain["installed_device_instances"]
    sites = {r["physical_site_id"]: r for r in domain["physical_sites"]}
    candidates, closure = {}, {}
    reasons = {}
    for unit in units:
        uid = unit["planning_unit_id"]; binding = selected.get(uid, {})
        action = actions.get(binding.get("action_id"), {})
        family = action_family(unit)
        missing = []
        if family not in matrix or matrix[family]["action_support"] != "supported":
            reason = "unsupported_constraint_consumer"; missing.append("supported_action_family")
        elif family == RTK and not unit.get("device_id"):
            reason = "rtk_device_selection_missing"; missing.append("device_id")
        elif track != "synthetic_fixture" and any(_synthetic(v) for v in (unit, binding, action, domain["device_catalog"])):
            reason = "fixture_cross_track_rejected"; missing.append("real_catalog_and_action_evidence")
        else:
            reason = "device_binding_incomplete"
            if not action: missing.append("frozen_action_id")
            device = catalog.get(binding.get("device_id"), {})
            if not device: missing.append("device_id")
            if unit.get("device_id") != binding.get("device_id"): missing.append("device_id")
            if binding.get("evidence_track") != track or unit.get("evidence_track") != track:
                missing.append("evidence_track")
            if action and action_family(action) != family: missing.append("service_key")
            for field in IDENTITY_FIELDS:
                if not binding.get(field) or binding.get(field) != action.get(field): missing.append(field)
            for field in ("catalog_version", "model_version"):
                if not binding.get(field) or binding.get(field) != device.get(field): missing.append(field)
            for field in SOURCE_FIELDS:
                if not isinstance(binding.get(field), str) or not binding[field].strip(): missing.append(field)
            if not device.get("source_ref") or binding.get("catalog_source_ref") != device.get("source_ref"):
                missing.append("catalog_source_ref")
            site = sites.get(binding.get("physical_site_id"), {})
            for owner, label in ((action, "action_qualification"), (site, "host_qualification")):
                q = owner.get("qualification", {})
                if q.get("status") != "qualified_under_model" or not q.get("source_ref"): missing.append(label)
            rule = site.get("colocation_rule", {})
            limit = rule.get("max_devices")
            if type(limit) is not int or limit < 0 or not rule.get("source_ref") or rule.get("source_ref") != binding.get("capacity_source_ref"):
                missing.append("capacity_limit")
            else:
                sid = binding.get("physical_site_id")
                proposed = sum(v.get("physical_site_id") == sid and actions.get(v.get("action_id"), {}).get("action_type")
                               not in ("reconfigure_device", "modify_device") for v in bindings)
                if proposed + sum(v.get("physical_site_id") == sid for v in installed) > limit:
                    missing.append("capacity_limit_exceeded")
            existence = site.get("existing_evidence", {})
            if existence.get("status") != "confirmed" or not existence.get("source_ref"): missing.append("physical_host_source")
            is_modify = action.get("action_type") in ("reconfigure_device", "modify_device")
            old = next((r for r in installed if r.get("device_instance_id") == binding.get("device_instance_id")), None)
            if is_modify:
                if not old or any(old.get(k) != binding.get(k) for k in IDENTITY_FIELDS): missing.append("installed_instance")
            elif old: missing.append("new_instance_already_installed")
            if not is_modify and any(r.get("physical_site_id") == binding.get("physical_site_id") and
                                    r.get("slot_id") == binding.get("slot_id") for r in installed):
                missing.append("slot_id_occupied")
            deps = binding.get("dependency_action_ids")
            if not isinstance(deps, list) or any(not isinstance(v, str) for v in deps) or len(set(deps or [])) != len(deps or []):
                missing.append("dependency_action_ids"); deps = []
            if family == RTK and not any(action_family(actions.get(v, {})) == C for v in deps):
                missing.append("dependency_action_ids")
            if any(v not in actions or v == action.get("action_id") for v in deps): missing.append("dependency_action_ids")
            closure[binding.get("action_id", uid)] = deps
        if missing:
            reasons[uid] = dict(planning_unit_id=uid, action_id=binding.get("action_id"), service_key=family,
                reason_code=reason, missing_fields=sorted(set(missing)), qualification_status="needs_evidence")
        else:
            candidates[action["action_id"]] = (uid, action, binding)
    # A dependency must itself be bound; propagate failures without dropping a consumer.
    while True:
        bad = [aid for aid, (_, _, _) in candidates.items() if any(d not in candidates for d in closure.get(aid, []))]
        if not bad: break
        for aid in bad:
            uid, _, _ = candidates.pop(aid)
            reasons[uid] = dict(planning_unit_id=uid, action_id=aid, reason_code="dependency_binding_incomplete",
                missing_fields=["dependency_action_ids"], qualification_status="needs_evidence")
    bound = [v[1] for _, v in sorted(candidates.items())]
    # Preserve the original action values and the frozen domain as the only membership authority.
    instances = [a.get("device_instance_id") for a in bound]
    targets = [(a["physical_site_id"], a["device_id"]) for a in bound]
    slots = [(a["physical_site_id"], a.get("slot_id")) for a in bound]
    if len(set(instances)) != len(instances): raise ValueError("duplicate device instance")
    if len(set(targets)) != len(targets):
        for aid, (uid, _, _) in candidates.items():
            reasons[uid] = dict(planning_unit_id=uid,action_id=aid,reason_code="unsupported_upstream_multiplicity",
                missing_fields=["same_model_multi_instance_consumer"],qualification_status="needs_evidence")
        candidates = {}; bound = []
    if len(set(slots)) != len(slots): raise ValueError("duplicate slot")
    bound = validate_actions(actions=bound, frozen_domain=domain)
    new = [a["device_instance_id"] for a in bound if a["action_type"] not in ("reconfigure_device", "modify_device")]
    return seal(dict(schema_version="existing_site_action_binding.v1",planning_units=units,selection_bindings=bindings,
        domain_review=review,model_capabilities=json_value(model_capabilities),scope_declaration=scope,
        bound_actions=bound,blocked_actions=list(reasons.values()),dependency_closure=closure,
        family_support_matrix=matrix,binding_records=[v[2] for _,v in sorted(candidates.items())],
        installed_device_instances=installed,proposed_device_instances=new,
        N_new_device_instances=len(new),N_plan_sites=len({a["physical_site_id"] for a in bound}),
        N_newly_activated_existing_sites=None,activation_count_status="not_evaluated",
        binding_status="blocked" if reasons else "bound_within_declared_model",
        real_domain_proof_status="not_verified",**proposal_flags()),"binding_fp")
