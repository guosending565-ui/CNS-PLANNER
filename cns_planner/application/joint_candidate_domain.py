"""Freeze supplied existing-site records; never discover or modify a project."""
from copy import deepcopy
from ..domain.joint_feasibility import json_value, digest, seal, verify
from .candidate_ue_acceptability import candidate_action_set_fingerprint


def _qualification(row):
    q=row.get("qualification") or {}
    status=q.get("status")
    if status == "qualified_under_model" and q.get("source_ref"): return status
    if status == "disqualified_with_proof" and q.get("source_ref") and q.get("necessary_condition_evidence"):
        return status
    if status == "not_applicable_to_service" and q.get("source_ref"): return status
    return "needs_evidence"


def freeze_existing_domain(*, discovery_manifest, site_records, installed_inventory,
        device_catalog, family_action_records, qualification_policy, scenario_scope):
    inputs=json_value(dict(discovery_manifest=discovery_manifest, site_records=site_records,
        installed_inventory=installed_inventory, device_catalog=device_catalog,
        family_action_records=family_action_records,qualification_policy=qualification_policy,scenario_scope=scenario_scope))
    sites=inputs["site_records"]; aliases={}; by_site={}
    for site in sites:
        sid=site.get("physical_site_id")
        if not isinstance(sid,str) or not sid or sid in by_site: raise ValueError("identity missing or duplicated")
        by_site[sid]=site
        for alias in [sid]+(site.get("aliases",[]) if site.get("alias_source_ref") else []):
            if alias in aliases and aliases[alias] != sid: raise ValueError("identity alias conflict")
            aliases[alias]=sid
        activation=site.get("baseline_activation_status")
        if activation not in ("active_verified","inactive_verified") or not site.get("activation_source_ref"):
            site["baseline_activation_status"]="unknown"
    qualified=[]; unknown=[]; excluded=[]; ledger=[]; ids=set(); targets=set()
    catalog={r.get("device_id") for r in device_catalog.get("items",[])}
    installed=inputs["installed_inventory"]
    for row in installed:
        sid=aliases.get(row.get("physical_site_id") or row.get("distinct_site_id"))
        if not sid:raise ValueError("installed identity unresolved")
        row["physical_site_id"]=sid
    instance_ids=set();installed_ids={r.get("device_instance_id") for r in installed}
    installed_targets={(r.get("physical_site_id"),r.get("device_id")) for r in installed}
    for original in inputs["family_action_records"]:
        row=deepcopy(original); sid=aliases.get(row.get("physical_site_id") or row.get("distinct_site_id"))
        if not sid: raise ValueError("identity action site unresolved")
        if row.get("distinct_site_id") and aliases.get(row["distinct_site_id"]) != sid: raise ValueError("identity action mismatch")
        row.update(physical_site_id=sid,distinct_site_id=sid)
        aid=row.get("action_id"); target=(sid,row.get("device_id"))
        if not aid or aid in ids or target in targets: raise ValueError("identity duplicate action/target")
        instance=row.get("device_instance_id")
        if instance and instance in instance_ids:raise ValueError("duplicate new device instance")
        if row.get("action_type") not in ("reconfigure_device","modify_device") and ((bool(instance) and instance in installed_ids) or target in installed_targets):
            raise ValueError("device instance already installed")
        if instance:instance_ids.add(instance)
        ids.add(aid); targets.add(target)
        site=by_site[sid]; qs=_qualification(site); qa=_qualification(row)
        existence=site.get("existing_evidence") or {}
        status="qualified_under_model"
        if row.get("reuse_class") == "new_build_candidate" or row.get("new_build") is True:
            status="disqualified_with_proof"; row["exclusion_reason"]="new_build_out_of_scope"
        elif "disqualified_with_proof" in (qs,qa): status="disqualified_with_proof"
        elif "not_applicable_to_service" in (qs,qa): status="not_applicable_to_service"
        elif (existence.get("status") != "confirmed" or not existence.get("source_ref")
              or qs != "qualified_under_model" or qa != "qualified_under_model" or row.get("device_id") not in catalog):
            status="needs_evidence"
        row["domain_qualification_status"]=status
        ledger.append(dict(action_id=aid,physical_site_id=sid,status=status,
            site_qualification=site.get("qualification"),action_qualification=row.get("qualification")))
        (qualified if status=="qualified_under_model" else unknown if status=="needs_evidence" else excluded).append(row)
    qualified.sort(key=lambda r:r["action_id"])
    candidate_action_set_fingerprint(actions=qualified)
    return seal(dict(schema_version="existing_joint_domain.v1",domain_version="1",
        discovery_manifest=inputs["discovery_manifest"],physical_sites=sites,alias_map=aliases,
        installed_device_instances=inputs["installed_inventory"],device_catalog=inputs["device_catalog"],
        installable_actions=qualified,unknown_candidates=unknown,excluded_candidates=excluded,
        qualification_ledger=ledger,qualification_policy=inputs["qualification_policy"],
        scope=inputs["scenario_scope"],completeness=dict(discovery=discovery_manifest.get("completeness","unknown"),
            qualification="unknown" if unknown else "closed_within_supplied_records",proof_eligible=False),
        evidence_track=scenario_scope.get("evidence_track","engineering_research")),"domain_fingerprint")


def freeze_constraint_scope(*, frozen_rule_snapshot, required_constraint_inventory, scope_declaration):
    json_value([frozen_rule_snapshot,required_constraint_inventory,scope_declaration])
    dec=deepcopy(scope_declaration); inv=deepcopy(required_constraint_inventory)
    ids=[r.get("constraint_id") for r in inv]; inc=dec.get("included_constraint_ids",[])
    exc=dec.get("excluded_constraints",[]); eids=[r.get("constraint_id") for r in exc]
    if (not ids or None in ids or len(ids)!=len(set(ids)) or len(inc)!=len(set(inc))
        or len(eids)!=len(set(eids)) or set(inc)&set(eids) or set(inc)|set(eids)!=set(ids)):
        raise ValueError("constraint partition must cover inventory exactly once")
    for row in exc:
        if any(not row.get(k) for k in ("reason","source_ref")): raise ValueError("exclusion source required")
    if dec.get("kind") not in ("declared_engineering_subset","complete_required_cns"):
        raise ValueError("scope kind")
    if dec["kind"]=="complete_required_cns" and exc: raise ValueError("complete scope exclusion")
    if any(not dec.get(k) for k in ("scope_id","version","source_ref")): raise ValueError("scope identity")
    known={r.get("scenario_rule_id") for r in frozen_rule_snapshot.get("rules",[])}
    known|={r.get("service_key") for r in frozen_rule_snapshot.get("rules",[])}
    known|={r.get("field") for r in frozen_rule_snapshot.get("requirement_consumption",[])}
    dec.update(schema_version="constraint_scope.v1",required_constraint_inventory=inv,
        inventory_status="declared_not_authenticated",mapping_unknowns=sorted(str(x) for x in set(ids)-known))
    return seal(dec,"scope_fingerprint")


def build_joint_search_context(*, frozen_rule_snapshot, frozen_domain, baseline_inputs, evaluator_versions, assessment_scope):
    verify(frozen_domain,"domain_fingerprint"); verify(assessment_scope,"scope_fingerprint")
    return seal(dict(schema_version="joint_search_context.v1",frozen_rule_snapshot=json_value(frozen_rule_snapshot),
        frozen_domain=json_value(frozen_domain),baseline_inputs=json_value(baseline_inputs),
        evaluator_versions=json_value(evaluator_versions),assessment_scope=json_value(assessment_scope),
        domain_fingerprint=frozen_domain["domain_fingerprint"],scope_fingerprint=assessment_scope["scope_fingerprint"],
        evidence_track=frozen_domain["evidence_track"]),"context_fingerprint")


def validate_actions(*, actions, frozen_domain):
    verify(frozen_domain,"domain_fingerprint"); json_value(actions)
    known={r["action_id"]:r for r in frozen_domain["installable_actions"]}
    candidate_action_set_fingerprint(actions=actions)
    for row in actions:
        if row.get("action_id") not in known or row != known[row["action_id"]]: raise ValueError("action outside frozen domain")
    return sorted(deepcopy(actions),key=lambda r:r["action_id"])


def count_joint_plan_sites(*, actions, frozen_domain, post_activation_evidence, participation_evidence):
    actions=validate_actions(actions=actions,frozen_domain=frozen_domain)
    sites={r["physical_site_id"]:r for r in frozen_domain["physical_sites"]}
    involved=sorted({r["physical_site_id"] for r in actions}); activated=[]; unknown=[]
    for sid in involved:
        status=sites[sid]["baseline_activation_status"]; post=post_activation_evidence.get(sid,{})
        if status == "active_verified": continue
        if status=="inactive_verified" and post.get("status")=="active_verified" and post.get("source_ref"):
            activated.append(sid)
        else: unknown.append(sid)
    installed={r.get("device_instance_id") for r in frozen_domain["installed_device_instances"]}
    new=[r.get("device_instance_id") for r in actions if r.get("action_type") not in ("reconfigure_device","modify_device")]
    new_count=None if any(not x for x in new) else len(set(new)-installed)
    participating=participation_evidence.get("physical_site_ids")
    complete=participation_evidence.get("status")=="complete" and bool(participation_evidence.get("source_ref"))
    if complete and (not isinstance(participating,list) or set(participating)-set(sites)): raise ValueError("participating site identity")
    return dict(plan_action_sites=involved,N_plan_sites=len(involved),newly_activated_existing_sites=activated,
        N_newly_activated_existing_sites=None if unknown else len(activated),
        activation_count_status="unknown" if unknown else "known",activation_unknown_sites=unknown,
        activation_count_bounds=[len(activated),len(activated)+len(unknown)],N_new_device_instances=new_count,
        participating_existing_sites=sorted(set(participating)) if complete else None,
        N_participating_existing_sites=len(set(participating)) if complete else None)
