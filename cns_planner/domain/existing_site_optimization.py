"""M5 sidecar finite-JSON contracts; no GIS, state or I/O ownership."""
from fractions import Fraction
from .joint_feasibility import json_value, digest, seal, verify

SCHEMA_REVIEW = "existing_site_domain_review.v1"
PROOF_KINDS = ("finite_synthetic_domain", "frozen_model_action_domain",
               "reviewed_real_existing_domain", "complete_required_cns")
DIMENSIONS = ("discovery", "identity", "installed_inventory", "activation_baseline",
              "qualification", "action_enumeration", "family_coverage",
              "dependency_closure", "source_authentication")


def proposal_flags():
    return dict(maturity="proposal", persisted_to_project_state=False,
                new_site_stage_unlocked=False)


def _checked_actions(actions, frozen_domain):
    verify(frozen_domain, "domain_fingerprint")
    rows = json_value(actions)
    known = {r["action_id"]: r for r in frozen_domain["installable_actions"]}
    ids, targets, instances = set(), set(), set()
    for row in rows:
        aid, sid, device = (row.get(k) for k in ("action_id", "physical_site_id", "device_id"))
        if any(not isinstance(v, str) or not v for v in (aid, sid, device, row.get("action_type"))):
            raise ValueError("action identity missing")
        if row.get("distinct_site_id") != sid or row != known.get(aid):
            raise ValueError("action outside frozen domain")
        if aid in ids or (sid, device) in targets: raise ValueError("unsupported multiplicity or duplicate action")
        instance = row.get("device_instance_id")
        if instance and instance in instances: raise ValueError("duplicate device instance")
        ids.add(aid); targets.add((sid, device))
        if instance: instances.add(instance)
    return sorted(rows, key=lambda r: r["action_id"])


def measure_cost(*, actions, objective_policy):
    """Exact incremental work items in one explicit comparable basis; never TCO."""
    basis = json_value(objective_policy).get("cost_basis")
    required = ("unit", "pricing_date", "tax", "discount_rate", "horizon_years",
                "coverage", "quantity_basis", "source_ref", "uncertainty", "work_items")
    if basis is None:
        return None, "cost_unresolved"
    if not isinstance(basis, dict) or any(k not in basis for k in required):
        raise ValueError("incomplete comparable cost basis")
    if set(basis)-set(required): raise ValueError("unsupported cost basis field")
    if any(not basis[k] for k in ("unit", "pricing_date", "tax", "coverage", "quantity_basis", "source_ref")):
        raise ValueError("cost basis identity")
    if (type(basis["discount_rate"]) not in (int, float) or basis["discount_rate"] <= -1 or
            type(basis["horizon_years"]) not in (int, float) or basis["horizon_years"] <= 0 or
            basis["quantity_basis"] != "work_item" or not isinstance(basis["coverage"], list)):
        raise ValueError("cost basis numeric/quantity contract")
    if basis["uncertainty"] != "exact":
        return None, "cost_unresolved"
    selected = {a["action_id"] for a in actions}
    covered, seen, total = set(), set(), Fraction(0)
    if not isinstance(basis["work_items"], list): raise ValueError("cost work items")
    for item in basis["work_items"]:
        if not isinstance(item, dict) or set(item)-{"work_item_id", "action_ids", "quantity", "unit_price", "source_ref"}:
            raise ValueError("unsupported cost work item field")
        wid = item.get("work_item_id")
        if not isinstance(wid, str) or not wid or wid in seen: raise ValueError("cost work item identity")
        seen.add(wid)
        aids = item.get("action_ids")
        if not isinstance(aids, list) or not aids or any(not isinstance(a, str) for a in aids):
            raise ValueError("cost action binding")
        if not selected.intersection(aids): continue
        covered.update(selected.intersection(aids))
        quantity, price = item.get("quantity"), item.get("unit_price")
        if not item.get("source_ref") or quantity is None or price is None:
            return None, "cost_unresolved"
        if type(quantity) not in (int, float) or quantity < 0 or type(price) not in (int, float):
            raise ValueError("cost quantity/price")
        # A shared item is charged once, regardless of how many actions trigger it.
        total += Fraction(str(quantity)) * Fraction(str(price))
    if covered != selected: return None, "cost_unresolved"
    try:
        number = int(total) if total.denominator == 1 else float(total)
        json_value(number)
    except (OverflowError, ValueError):
        return None, "cost_unresolved"
    if Fraction(str(number)) != total: return None, "cost_unresolved"
    return number, "comparable_incremental_cost"


def measure_objectives(*, actions: list[dict], frozen_domain: dict, objective_policy: dict,
                       activation_predictions: dict, participation_evidence: dict) -> dict:
    policy, predictions, participation = json_value([objective_policy, activation_predictions, participation_evidence])
    if (policy.get("schema_version") != "existing_site_objective_policy.v1" or
            policy.get("decision_id") != "DEC-M5-ACTIVATION-001" or
            policy.get("model_version") != "1.0" or
            any(not policy.get(k) for k in ("version", "source_ref", "prediction_policy_ref"))):
        raise ValueError("objective policy identity/version")
    rows = _checked_actions(actions, frozen_domain)
    # Exact M3 canonical encoding. Application tests assert equivalence to the
    # existing candidate_action_set_fingerprint; Domain never imports Application.
    action_fp = digest(dict(schema_version="candidate_action_set.v1", actions=rows))
    sites = {s["physical_site_id"]: s for s in frozen_domain["physical_sites"]}
    involved = sorted({a["physical_site_id"] for a in rows})
    if set(involved) - sites.keys(): raise ValueError("unresolved site identity")
    installed = frozen_domain["installed_device_instances"]
    installed_ids = [r.get("device_instance_id") for r in installed]
    if None in installed_ids or len(set(installed_ids)) != len(installed_ids):
        raise ValueError("installed instance identity missing/duplicate")
    installed_by_id = {r["device_instance_id"]: r for r in installed}
    new, reconfigured, instance_unknowns = [], [], []
    for row in rows:
        iid = row.get("device_instance_id")
        if row["action_type"] in ("reconfigure_device", "modify_device"):
            old = installed_by_id.get(iid)
            if not old or (old.get("physical_site_id"),old.get("device_id")) != (row["physical_site_id"],row["device_id"]):
                instance_unknowns.append(row["action_id"])
            else: reconfigured.append(iid)
        elif not iid: instance_unknowns.append(row["action_id"])
        elif iid in installed_by_id: raise ValueError("new instance already installed")
        else: new.append(iid)
    conflicts = []
    rules = [dict(s["colocation_rule"], physical_site_id=sid) for sid, s in sites.items() if s.get("colocation_rule")]
    rules.extend(policy.get("combination_constraints", []))
    for rule in rules:
        if not rule.get("source_ref"): continue
        sid = rule.get("physical_site_id")
        planned = [a for a in rows if a["physical_site_id"] == sid and a["action_type"] not in ("reconfigure_device", "modify_device")]
        capacity = rule.get("max_devices")
        if type(capacity) is int and len(planned) + sum(r.get("physical_site_id") == sid for r in installed) > capacity:
            conflicts.append(dict(physical_site_id=sid, reason="colocation_capacity_exceeded", rule=rule))
        slots = [a.get("slot_id") for a in planned if a.get("slot_id") is not None]
        occupied = [a.get("slot_id") for a in installed if a.get("physical_site_id") == sid and a.get("slot_id") is not None]
        if len(set(slots)) != len(slots) or set(slots) & set(occupied):
            conflicts.append(dict(physical_site_id=sid, reason="slot_conflict", rule=rule))
    active, activated, inactive, unknown = [], [], [], []
    evidence = {}
    for sid in involved:
        site = sites[sid]; baseline = site.get("baseline_activation_status")
        if not site.get("activation_source_ref"): baseline = "unknown"
        pred = predictions.get(sid) or {}
        conditions = pred.get("conditions") or {}
        valid = (pred.get("action_set_fp") == action_fp and pred.get("source_ref") and
                 pred.get("model_version") == policy["model_version"] and
                 pred.get("prediction_policy_ref") == policy["prediction_policy_ref"] and
                 pred.get("domain_fingerprint") == frozen_domain["domain_fingerprint"] and
                 bool(policy.get("dependency_fingerprint")) and
                 pred.get("dependency_fingerprint") == policy["dependency_fingerprint"] and not conflicts and
                 all((conditions.get(k) or {}).get("status") == "satisfied" and
                     (conditions.get(k) or {}).get("source_ref") for k in ("power", "backhaul", "equipment", "qualification")))
        evidence[sid] = dict(baseline_activation_status=baseline, activation_source_ref=site.get("activation_source_ref"),
                            post_plan_activation_prediction=pred, prediction_binding_valid=bool(valid))
        if baseline == "active_verified": active.append(sid)
        elif baseline == "inactive_verified" and valid and pred.get("status") == "predicted_active_under_model": activated.append(sid)
        elif baseline == "inactive_verified" and valid and pred.get("status") == "predicted_inactive_under_model": inactive.append(sid)
        else: unknown.append(sid)
    lb, ub = len(activated), len(activated) + len(unknown)
    value = None if unknown or conflicts else lb
    participating = participation.get("physical_site_ids")
    complete = participation.get("status") == "complete" and bool(participation.get("source_ref"))
    if complete and (not isinstance(participating, list) or any(not isinstance(s,str) for s in participating) or set(participating) - sites.keys()):
        raise ValueError("participation identity")
    devices = None if instance_unknowns else len(new)
    cost, cost_status = measure_cost(actions=rows, objective_policy=policy)
    return seal(dict(schema_version="existing_site_objective_measurement.v1",
        decision_id=policy["decision_id"], objective_policy=policy, objective_policy_fp=digest(policy),
        domain_fingerprint=frozen_domain["domain_fingerprint"], action_set_fp=action_fp,
        values=dict(activation=value, devices=devices, cost=cost),
        bounds=dict(activation=dict(lower_bound=lb,upper_bound=ub),
                    devices=dict(lower_bound=len(new), upper_bound=None if instance_unknowns else len(new)),
                    cost=dict(lower_bound=cost,upper_bound=cost)),
        activation_count_bounds=[lb,ub], activation_count_status="unknown" if value is None else "known",
        activation_evidence_identity="real_baseline" if len(active) == len(involved) else "model_prediction",
        confirmed_sets=dict(baseline_active_sites=active,model_newly_activated_sites=activated,model_inactive_sites=inactive),
        unknown_sites=unknown, activation_evidence=evidence, instance_unknowns=instance_unknowns,
        new_device_instances=new, reconfigured_device_instances=reconfigured,
        N_plan_sites=len(involved), N_newly_activated_existing_sites=value,
        N_new_device_instances=devices, N_participating_existing_sites=len(set(participating)) if complete else None,
        cost_status=cost_status, input_status="combination_conflict" if conflicts else "valid",
        combination_conflicts=conflicts, **proposal_flags()), "measurement_fp")


def validate_proof_scope(proof_scope: dict) -> dict:
    scope = json_value(proof_scope)
    if scope.get("kind") not in PROOF_KINDS:
        raise ValueError("proof scope kind")
    if any(not isinstance(scope.get(k), dict) or not scope[k] for k in ("domain_scope", "constraint_scope")):
        raise ValueError("proof scope requires domain and constraint identity")
    return scope
