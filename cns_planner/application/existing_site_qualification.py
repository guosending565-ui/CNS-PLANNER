"""Conservative qualifications and M4-compatible sidecar candidates."""
from ..domain.joint_feasibility import json_value, seal
from ..domain.existing_site_optimization import proposal_flags
from .joint_candidate_domain import freeze_existing_domain

REQUIRED = ("legal_host", "mount_height", "capacity", "power", "backhaul", "colocation", "origin")


def _qualification(evidence):
    status = evidence.get("status")
    if not evidence.get("source_ref"): return "needs_evidence"
    if status == "disqualified_with_proof" and evidence.get("necessary_condition_evidence"): return status
    if status in ("qualified_under_model", "not_applicable_to_service"): return status
    return "needs_evidence"


def qualify_existing_actions(*, discovered_domain: dict, installed_inventory: list[dict],
                             device_catalog: dict, qualification_policy: dict) -> dict:
    discovered, installed, catalog, policy = json_value([discovered_domain, installed_inventory, device_catalog, qualification_policy])
    if discovered.get("discovery_status") == "input_inconsistent":
        raise ValueError("discovery identity inconsistent")
    sites = discovered.get("physical_sites", [])
    aliases = discovered.get("raw_to_physical_aliases", {})
    by_site = {s["physical_site_id"]:s for s in sites}
    inventory_gaps, constraints, site_ledger = [], [], []
    for site in sites:
        sid = site["physical_site_id"]; evidence = site.get("qualification_evidence") or {}
        gaps = [k for k in REQUIRED if (evidence.get(k) or {}).get("status") != "satisfied" or not (evidence.get(k) or {}).get("source_ref")]
        explicit = _qualification(site.get("qualification") or {})
        status = explicit if explicit in ("disqualified_with_proof", "not_applicable_to_service") else "needs_evidence" if gaps else "qualified_under_model"
        site["qualification"] = dict(status=status, source_ref=(site.get("qualification") or {}).get("source_ref") or policy.get("source_ref"),
            necessary_condition_evidence=(site.get("qualification") or {}).get("necessary_condition_evidence"), missing_evidence=gaps)
        site_ledger.append(dict(physical_site_id=sid, qualification=site["qualification"], evidence=evidence))
        rule = site.get("colocation_rule") or {}
        if rule.get("source_ref") and type(rule.get("max_devices")) is int and rule["max_devices"] >= 0:
            constraints.append(dict(rule,physical_site_id=sid))
        else:
            site["qualification"]["status"] = "needs_evidence"
            site["qualification"]["missing_evidence"].append("colocation_rule")
        baseline = site.get("baseline_activation_evidence") or {}
        site["baseline_activation_status"] = baseline.get("status", "unknown")
        site["activation_source_ref"] = baseline.get("source_ref")
        if not baseline.get("valid_at") or baseline.get("identity") != "real_baseline":
            site["baseline_activation_status"] = "unknown"
            site["activation_source_ref"] = None
    seen_instances = set()
    valid_installed = []
    for row in installed:
        sid = aliases.get(row.get("physical_site_id"),row.get("physical_site_id")); iid=row.get("device_instance_id")
        if iid and iid in seen_instances: raise ValueError("duplicate installed instance")
        if iid: seen_instances.add(iid)
        row["physical_site_id"] = sid
        if sid not in by_site or not iid or not row.get("source_ref") or not row.get("installed_at") or not row.get("device_id"):
            inventory_gaps.append(dict(record=row,reason="installed_instance_evidence_missing"))
        else: valid_installed.append(row)
    items = catalog.get("items", [])
    device_ids = [r.get("device_id") for r in items]
    if len(device_ids) != len(set(device_ids)): raise ValueError("duplicate catalog identity")
    devices = {r.get("device_id"):r for r in items}
    accepted, unsupported = [], []
    targets, instances, action_ids = set(), set(), set()
    for row in policy.get("family_action_records", []):
        aid=row.get("action_id"); sid=aliases.get(row.get("physical_site_id"),row.get("physical_site_id")); device=row.get("device_id")
        if not aid or aid in action_ids: raise ValueError("duplicate/missing action identity")
        action_ids.add(aid)
        iid=row.get("device_instance_id")
        if iid and iid in instances: raise ValueError("duplicate new device instance")
        if iid: instances.add(iid)
        reason = None
        if sid not in by_site: reason="physical_identity_unresolved"
        elif (sid,device) in targets: reason="unsupported_multiplicity"
        elif any(r["physical_site_id"] == sid and r["device_id"] == device for r in valid_installed) and row.get("action_type") not in ("reconfigure_device","modify_device"):
            reason="installed_target_requires_explicit_replacement_binding"
        targets.add((sid,device))
        if reason:
            unsupported.append(dict(row,domain_qualification_status="needs_evidence",reason=reason)); continue
        row.update(physical_site_id=sid, distinct_site_id=sid)
        q=_qualification(row.get("qualification") or {})
        registered=devices.get(device,{})
        if q == "qualified_under_model" and (not catalog.get("version") or not catalog.get("source_ref") or not registered.get("model") or not registered.get("source_ref") or not iid or inventory_gaps):
            q="needs_evidence"
        old=row.get("qualification") or {}
        row["qualification"]=dict(old,status=q)
        accepted.append(row)
    frozen=freeze_existing_domain(discovery_manifest={"completeness":discovered.get("discovery_status"),"discovery_fingerprint":discovered.get("discovery_fingerprint"),"source_manifest":discovered.get("source_manifest",[])},
        site_records=sites,installed_inventory=valid_installed,device_catalog=catalog,family_action_records=accepted,
        qualification_policy=policy,scenario_scope=discovered.get("discovery_scope",{}))
    unknown=frozen["unknown_candidates"]+unsupported
    return seal(dict(schema_version="existing_site_qualification.v1",frozen_domain=frozen,
        Q=frozen["installable_actions"],U=unknown,E=frozen["excluded_candidates"],
        site_qualification_ledger=site_ledger, installed_inventory_evidence=installed,
        inventory_gaps=inventory_gaps,combination_constraints=constraints,
        qualification_status="evidence_incomplete" if unknown or inventory_gaps or any(s["qualification"]["status"] == "needs_evidence" for s in sites) else "closed_within_supplied_records",
        **proposal_flags()),"qualification_fingerprint")
