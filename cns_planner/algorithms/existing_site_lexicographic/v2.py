"""R2.3-A finite conditional research-domain lexicographic search.

Pure JSON/model kernel. It never reads files, never writes ProjectState or
artifacts, never imports GIS/Application, and never calls a real CNS model. The
``runtime`` object is injected by trusted code; this module never accepts a
module path, callback, function handle or command from JSON.

Mathematical optimality is claimed only for an explicitly enumerated, complete,
finite conditional action domain and only after the layer proof has been replayed
against the injected runtime. Source authenticity, real device qualification,
real activation, complete Required CNS and independent verification are never
upgraded by this proof: the result keeps them ``not_verified``/``unknown``.

The conditional baseline is a separate research projection. Real qualification
and real activation facts are carried through unchanged and are never replaced by
the conditional model. Unsupported actions/families remain in the frozen action
set as explicit exclusions rather than being silently dropped. Unknown
qualification, unknown device inventory and unknown cost remain unknown and block
the affected layers.
"""
from copy import deepcopy

from ...domain.joint_feasibility import json_value, digest, seal, verify
from ...domain.existing_site_optimization import measure_cost, validate_proof_scope, proposal_flags
from ...domain.research_scenario import TRACK, checked_scenario
from .v1 import BudgetStop, partition

VERSION = "conditional-lexicographic.2.0"
DOMAIN_SCHEMA = "conditional_research_domain.v2"
CONTEXT_SCHEMA = "conditional_optimization_context.v2"
CHECKPOINT_SCHEMA = "conditional_lexicographic_checkpoint.v1"
RESULT_SCHEMA = "conditional_lexicographic_result.v2"
LAYER_SCHEMA = "conditional_layer_proof.v2"
BINDING_SCHEMA = "conditional_proof_binding.v2"
MEASUREMENT_SCHEMA = "conditional_objective_measurement.v1"
FIXTURE_TRACK = "synthetic_conditional_fixture"
OBJECTIVES = ("activation", "devices", "cost")
NAV_FIELDS = (
    ("provider_inventory_status", "provider_inventory"),
    ("device_status", "device_status"),
    ("handover_status", "handover"),
    ("speed_profile_status", "speed_profile"),
    ("backup_integrity_status", "backup_integrity"),
)
SATISFIED_NAVIGATION = ("satisfied", "confirmed", "current", "complete")
POLICY_FIELDS = {"algorithm_version", "order_policy", "max_visited_nodes",
    "max_joint_replays", "retain_feasible_limit", "max_storage_bytes",
    "max_elapsed_seconds", "max_memory_mb"}
ACTION_FIELDS = ("action_id", "physical_site_id", "device_id", "device_instance_id",
    "slot_id", "service_key", "planner_family", "action_type", "device_effect",
    "dependency_action_ids", "source_class", "assumption_refs", "applicable_models")
DEVICE_EFFECTS = ("new_instance", "reconfigure_existing", "none", "unknown")
QUALIFICATION_STATES = ("qualified_under_policy", "unknown")
BASELINE_STATES = ("active", "inactive", "unknown")


# ---------------------------------------------------------------------------
# Strict contracts.
# ---------------------------------------------------------------------------


def checked_policy(policy):
    p = json_value(policy)
    if type(p) is not dict or set(p) != POLICY_FIELDS:
        raise ValueError("explicit search policy fields required; unknown field or cap")
    if p["algorithm_version"] != VERSION or p["order_policy"] != "action_id":
        raise ValueError("conditional search algorithm/order")
    for key in ("max_visited_nodes", "max_joint_replays", "retain_feasible_limit",
                "max_storage_bytes"):
        if type(p[key]) is not int or p[key] < 0:
            raise ValueError("explicit nonnegative integer budget")
    for key in ("max_elapsed_seconds", "max_memory_mb"):
        if type(p[key]) not in (int, float) or p[key] <= 0:
            raise ValueError("explicit positive budget")
    if p["max_storage_bytes"] > 67108864:
        raise ValueError("64MiB proposal limit")
    return p


def runtime_available(runtime):
    if runtime is None or isinstance(runtime, dict):
        return False
    return all(callable(getattr(runtime, name, None)) for name in
        ("evaluate_action_set", "monotonic_seconds", "cancel_requested",
         "resident_memory_mb"))


def _string(value):
    return isinstance(value, str) and bool(value)


def _approved(entry):
    return (entry.get("evidence_class") == "C" and _string(entry.get("approval_ref")) and
            entry.get("validity") == "approved" and entry.get("value") is not None)


def _conditional_qualification(scenario):
    rows = []
    route = scenario["route_scenario"]
    for policy in scenario["policies"]:
        for entry in policy["entries"]:
            if entry.get("parameter_kind") != "installation_qualification":
                continue
            qualified = (entry.get("evidence_class") == "C" and
                _string(entry.get("approval_ref")) and
                entry.get("validity") == "approved" and
                entry.get("value") == "assumed_eligible")
            rows.append(dict(physical_site_id=entry.get("site_id"),
                parameter_id=entry.get("parameter_id"),
                scenario_id=route.get("scenario_id"),
                policy_id=policy.get("policy_id"),
                policy_version=policy.get("policy_version"),
                evidence_class=entry.get("evidence_class"),
                source_ref=entry.get("source_ref"),
                status=("conditional_qualified_under_policy" if qualified
                        else "unknown"),
                value=entry.get("value")))
    return rows


def _computational_fingerprint(scenario, parent, qualification):
    ledger = parent.get("site_ledger", [])
    actions = scenario["route_scenario"].get("requested_actions", [])
    return digest(dict(scenario=scenario["computational_dependency_fingerprint"],
        real_site_ledger=ledger, qualification=qualification, actions=actions))


def _parameters(scenario):
    return {entry.get("parameter_id"): entry
            for policy in scenario["policies"] for entry in policy["entries"]}


def _parent_actions(parent):
    return {row.get("physical_site_id"): row for row in parent.get("site_ledger", [])
            if type(row) is dict}


def _validate_objective_policy(policy):
    if type(policy) is not dict:
        raise ValueError("objective policy object required")
    if (policy.get("schema_version") != "existing_site_objective_policy.v1" or
            policy.get("decision_id") != "DEC-M5-ACTIVATION-001" or
            policy.get("model_version") != "1.0" or
            policy.get("activation_policy") != "conditional_research_only"):
        raise ValueError("conditional objective policy identity")
    for key in ("version", "source_ref", "prediction_policy_ref", "dependency_fingerprint"):
        if not _string(policy.get(key)):
            raise ValueError("objective policy identity:" + key)
    if not isinstance(policy.get("combination_constraints"), list):
        raise ValueError("objective policy combination constraints")
    # Validate cost-basis structure once; empty actions do not consume any item.
    measure_cost(actions=[], objective_policy=policy)


def _validate_actions(actions, qualification, scenario, parent):
    if type(actions) is not list or not actions:
        raise ValueError("explicit non-empty finite action set required")
    ids = [row.get("action_id") for row in actions if type(row) is dict]
    if len(ids) != len(actions) or any(not _string(item) for item in ids):
        raise ValueError("action object/identity required")
    if ids != sorted(set(ids)):
        raise ValueError("stable complete action order required")
    parent_sites = _parent_actions(parent)
    qualities = {}
    for row in qualification:
        sid = row.get("physical_site_id")
        if sid in qualities:
            raise ValueError("one conditional qualification entry per site required")
        qualities[sid] = row
    parameters = _parameters(scenario)
    pairs, instances = set(), set()
    for row in actions:
        if any(field not in row for field in ACTION_FIELDS):
            raise ValueError("action field missing")
        aid, sid, device = row["action_id"], row["physical_site_id"], row["device_id"]
        if any(not _string(row[field]) for field in
                ("physical_site_id", "device_id", "service_key", "planner_family",
                 "action_type", "source_class")):
            raise ValueError("action identity missing:" + str(aid))
        if sid not in parent_sites:
            raise ValueError("action site not in real parent ledger:" + str(aid))
        if sid not in qualities:
            raise ValueError("action site without conditional qualification:" + str(aid))
        if row["source_class"] not in ("A", "B", "C", "D"):
            raise ValueError("action source class:" + str(aid))
        if row["device_effect"] not in DEVICE_EFFECTS:
            raise ValueError("action device effect:" + str(aid))
        if (sid, device) in pairs:
            raise ValueError("unsupported multiplicity or duplicate action")
        pairs.add((sid, device))
        instance = row.get("device_instance_id")
        if instance is not None and not _string(instance):
            raise ValueError("device instance identity:" + str(aid))
        if row["device_effect"] in ("new_instance", "reconfigure_existing") and not instance:
            raise ValueError("device instance required:" + str(aid))
        if instance:
            if instance in instances:
                raise ValueError("duplicate device instance:" + str(aid))
            instances.add(instance)
        slot = row.get("slot_id")
        if slot is not None and not _string(slot):
            raise ValueError("slot identity:" + str(aid))
        for field in ("dependency_action_ids", "assumption_refs", "applicable_models"):
            value = row.get(field)
            if type(value) is not list or any(not _string(item) for item in value):
                raise ValueError("action list field:" + field)
        for dependency in row["dependency_action_ids"]:
            if dependency not in ids:
                raise ValueError("action dependency missing:" + dependency)
        for parameter_id in row["assumption_refs"]:
            if parameter_id not in parameters:
                raise ValueError("action assumption missing:" + parameter_id)
    return qualities


def _validate_conditional_sites(domain, qualification, scenario, parent):
    sites = domain.get("conditional_sites")
    if type(sites) is not list:
        raise ValueError("conditional sites list required")
    parent_sites = _parent_actions(parent)
    parameters = _parameters(scenario)
    quality = {row["physical_site_id"]: row for row in qualification}
    seen = set()
    for row in sites:
        if type(row) is not dict or set(row) != {"physical_site_id",
                "conditional_qualification", "conditional_activation_baseline",
                "baseline_source_ref", "baseline_assumption_refs", "missing_reason"}:
            raise ValueError("conditional site fields")
        sid = row["physical_site_id"]
        if not _string(sid) or sid in seen:
            raise ValueError("conditional site identity")
        seen.add(sid)
        if sid not in parent_sites:
            raise ValueError("conditional site not in real parent:" + str(sid))
        if sid not in quality:
            raise ValueError("conditional site without qualification:" + str(sid))
        entry = quality[sid]
        if row["conditional_qualification"] not in QUALIFICATION_STATES:
            raise ValueError("conditional qualification state")
        qualified = entry.get("status") == "conditional_qualified_under_policy"
        if (row["conditional_qualification"] == "qualified_under_policy") != qualified:
            raise ValueError("conditional qualification does not match approved policy")
        if row["conditional_activation_baseline"] not in BASELINE_STATES:
            raise ValueError("conditional activation baseline state")
        refs = row["baseline_assumption_refs"]
        if type(refs) is not list or any(not _string(item) for item in refs):
            raise ValueError("baseline assumption references")
        if not qualified:
            if row["conditional_activation_baseline"] != "unknown":
                raise ValueError("unqualified site cannot carry a conditional baseline")
            if not _string(row["missing_reason"]):
                raise ValueError("unknown conditional baseline requires a reason")
            if row["baseline_source_ref"] is not None:
                raise ValueError("unqualified site cannot carry a baseline source")
            continue
        if entry.get("parameter_id") not in refs:
            raise ValueError("baseline must cite its approved installation assumption")
        for parameter_id in refs:
            parameter = parameters.get(parameter_id)
            if parameter is None or not (_approved(parameter) or
                    parameter.get("evidence_class") in ("B",)):
                raise ValueError("baseline assumption not approved:" + str(parameter_id))
        baseline = row["conditional_activation_baseline"]
        if baseline in ("active", "inactive"):
            if not _string(row["baseline_source_ref"]):
                raise ValueError("conditional baseline source required")
            if row["missing_reason"] is not None:
                raise ValueError("known conditional baseline cannot declare a missing reason")
        elif not _string(row["missing_reason"]):
            raise ValueError("unknown conditional baseline requires a reason")
    if seen != set(quality):
        raise ValueError("conditional site coverage mismatch")
    return {row["physical_site_id"]: row for row in sites}


def _validate_installed(domain, parent):
    status = domain.get("installed_inventory_status")
    if status not in ("known", "unknown"):
        raise ValueError("installed inventory status")
    installed = domain.get("installed_device_instances")
    if type(installed) is not list:
        raise ValueError("installed inventory list")
    if installed != parent.get("installed_device_instances", []):
        raise ValueError("installed inventory is not the frozen real parent fact")
    expected_status = parent.get("installed_inventory_status", "unknown")
    if status != expected_status:
        raise ValueError("installed inventory status mismatch")
    parent_sites = _parent_actions(parent)
    instances = set()
    for row in installed:
        if type(row) is not dict:
            raise ValueError("installed instance object")
        iid = row.get("device_instance_id")
        sid = row.get("physical_site_id")
        if not _string(iid) or iid in instances or sid not in parent_sites:
            raise ValueError("installed instance identity")
        if not _string(row.get("device_id")):
            raise ValueError("installed device identity")
        instances.add(iid)
    return status, installed


def _validate_consumer_binding(domain, context, scenario):
    binding = domain.get("consumer_binding")
    required = {"consumers", "required_inventory", "included_constraint_ids",
        "excluded_constraints", "unconsumed_requirements", "unsupported_action_families",
        "unsupported_planner_families", "navigation_evidence"}
    if type(binding) is not dict or set(binding) != required:
        raise ValueError("consumer binding fields")
    consumers = binding["consumers"]
    if type(consumers) is not list or not consumers:
        raise ValueError("explicit consumers required")
    seen = set()
    for row in consumers:
        if (type(row) is not dict or
                set(row) != {"consumer_id", "method_ref", "method_version"}):
            raise ValueError("consumer identity object")
        cid = row["consumer_id"]
        if not _string(cid) or cid in seen:
            raise ValueError("unique consumer identity required")
        if not _string(row["method_ref"]) or not _string(row["method_version"]):
            raise ValueError("consumer method identity required")
        seen.add(cid)
    dependencies = {row.get("consumer_id"): row for row in
        scenario["route_scenario"].get("consumer_dependencies", [])}
    if seen != set(dependencies):
        raise ValueError("consumer binding is not the frozen scenario consumer set")
    for row in consumers:
        if row["method_version"] != dependencies[row["consumer_id"]].get("model_version"):
            raise ValueError("consumer method version mismatch")
    scope = scenario["route_scenario"].get("required_scope")
    if type(scope) is not dict:
        raise ValueError("required scope required")
    for field, scope_field in (("required_inventory", "required_constraint_inventory"),
                               ("included_constraint_ids", "included_constraint_ids"),
                               ("excluded_constraints", "excluded_constraints"),
                               ("unconsumed_requirements", "unconsumed_requirements")):
        if binding[field] != scope.get(scope_field):
            raise ValueError("required scope binding mismatch:" + field)
    for field, label in (("unsupported_action_families", "family"),
                         ("unsupported_planner_families", "planner")):
        rows = binding[field]
        if type(rows) is not list:
            raise ValueError("unsupported consumer capability list")
        names = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {label, "reason", "source_ref"} or
                    not _string(row[label]) or not _string(row["reason"]) or
                    not _string(row["source_ref"])):
                raise ValueError("unsupported consumer capability identity")
            if row[label] in names:
                raise ValueError("duplicate unsupported consumer capability")
            names.add(row[label])
    navigation = binding["navigation_evidence"]
    if type(navigation) is not dict or set(navigation) != {key for key, _ in NAV_FIELDS} | {"source_ref"}:
        raise ValueError("navigation evidence fields")
    if not _string(navigation.get("source_ref")):
        raise ValueError("navigation evidence source required")
    return binding


def _validate_enumeration(domain):
    enumeration = domain.get("enumeration")
    required = {"kind", "complete", "action_count", "subset_count", "basis_ref"}
    if type(enumeration) is not dict or not required <= set(enumeration) or \
            set(enumeration) - required - {"budget_audit"}:
        raise ValueError("explicit complete finite enumeration required")
    if enumeration["kind"] != "explicit_complete_finite" or enumeration["complete"] is not True:
        raise ValueError("explicit complete finite enumeration required")
    actions = domain.get("actions")
    n = len(actions) if type(actions) is list else -1
    if enumeration["action_count"] != n or enumeration["subset_count"] != 2 ** n:
        raise ValueError("enumeration count mismatch")
    if not _string(enumeration["basis_ref"]):
        raise ValueError("enumeration basis reference required")
    audit = enumeration.get("budget_audit")
    if n > 12:
        if type(audit) is not dict or set(audit) != {"leaf_evaluations",
                "node_upper_bound", "basis_ref"}:
            raise ValueError("explicit complete finite domain budget audit required for more than 12 actions")
        if (audit["leaf_evaluations"] != 2 ** n or
                type(audit["node_upper_bound"]) is not int or
                audit["node_upper_bound"] < 2 * 2 ** n - 1 or
                not _string(audit["basis_ref"])):
            raise ValueError("budget audit scale mismatch")
    elif audit is not None:
        if type(audit) is not dict or audit.get("leaf_evaluations") != 2 ** n:
            raise ValueError("budget audit scale mismatch")
    return enumeration


def _validate_pruning(domain, actions):
    rules = domain.get("pruning_rules")
    required = {"incompatible_action_pairs", "structural_source_ref",
        "max_selected_actions", "max_selected_actions_source_ref"}
    if type(rules) is not dict or set(rules) != required:
        raise ValueError("explicit pruning rules required")
    ids = {row["action_id"] for row in actions}
    pairs, seen = rules["incompatible_action_pairs"], set()
    if type(pairs) is not list:
        raise ValueError("incompatible action pairs list")
    for pair in pairs:
        if (type(pair) is not list or len(pair) != 2 or len(set(pair)) != 2 or
                any(item not in ids for item in pair)):
            raise ValueError("incompatible action pair identity")
        key = tuple(sorted(pair))
        if key in seen:
            raise ValueError("duplicate incompatible action pair")
        seen.add(key)
    if pairs and not _string(rules["structural_source_ref"]):
        raise ValueError("pruning rules require an explicit source")
    limit = rules["max_selected_actions"]
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError("explicit action-count limit")
        if not _string(rules["max_selected_actions_source_ref"]):
            raise ValueError("action-count limit requires an explicit source")
    return rules


def _validate_fixture(domain, context, scenario, parent, actions):
    fixture = domain.get("fixture")
    if type(fixture) is not dict:
        raise ValueError("explicit synthetic conditional fixture required")
    if (fixture.get("evidence_track") != FIXTURE_TRACK or
            context.get("evidence_track") != FIXTURE_TRACK):
        raise ValueError("explicit synthetic conditional fixture required")
    if not _string(fixture.get("fixture_id")) or not _string(fixture.get("fixture_version")):
        raise ValueError("fixture identity")
    manifest = fixture.get("manifest")
    fields = {"fixture_id", "fixture_version", "evidence_track",
        "scenario_fingerprint", "source_bundle_fingerprint",
        "computational_dependency_fingerprint", "parent_real_domain_fp",
        "objective_policy_fp", "action_order_digest", "route_id", "path_sha256",
        "altitude_value", "altitude_units", "vertical_reference",
        "operating_layer_id", "proof_scope_fp"}
    if type(manifest) is not dict or set(manifest) != fields:
        raise ValueError("fixture manifest fields")
    if fixture.get("fixture_sha256") != digest(manifest):
        raise ValueError("fixture manifest fingerprint mismatch")
    for key in ("fixture_id", "fixture_version", "evidence_track"):
        if manifest.get(key) != fixture.get(key):
            raise ValueError("fixture manifest identity mismatch:" + key)
    route = scenario["route_scenario"]
    expected = dict(scenario_fingerprint=scenario["scenario_fingerprint"],
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=domain["conditional_domain"]
            ["computational_dependency_fingerprint"],
        parent_real_domain_fp=digest(parent),
        objective_policy_fp=digest(context["objective_policy"]),
        action_order_digest=digest(actions), route_id=route["route_id"],
        path_sha256=route["path_sha256"], altitude_value=route["altitude_value"],
        altitude_units=route["altitude_units"],
        vertical_reference=route["vertical_reference"],
        operating_layer_id=route["operating_layer_id"],
        proof_scope_fp=digest(context["proof_scope"]))
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError("fixture material mismatch:" + key)
    return fixture


def _binding(domain, context, scenario, conditional):
    route = scenario["route_scenario"]
    frozen = domain["consumer_binding"]
    payload = dict(schema_version=BINDING_SCHEMA,
        proof_scope=deepcopy(context["proof_scope"]),
        required_inventory=deepcopy(frozen["required_inventory"]),
        included_constraint_ids=deepcopy(frozen["included_constraint_ids"]),
        excluded_constraints=deepcopy(frozen["excluded_constraints"]),
        unconsumed_requirements=deepcopy(frozen["unconsumed_requirements"]),
        consumers=deepcopy(frozen["consumers"]),
        unsupported_action_families=deepcopy(frozen["unsupported_action_families"]),
        unsupported_planner_families=deepcopy(frozen["unsupported_planner_families"]),
        navigation_evidence=deepcopy(frozen["navigation_evidence"]),
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=conditional["computational_dependency_fingerprint"],
        real_parent_fingerprint=digest(domain["real_parent_domain"]),
        conditional_domain_fingerprint=conditional["domain_fingerprint"],
        route=dict(route_id=route["route_id"], path_sha256=route["path_sha256"],
            altitude_value=route["altitude_value"], altitude_units=route["altitude_units"],
            vertical_reference=route["vertical_reference"],
            operating_layer_id=route["operating_layer_id"]),
        action_order_digest=digest(domain["actions"]),
        source_authentication_status="not_authenticated",
        real_domain_proof_status="not_verified",
        independent_verification_status="not_verified",
        m6_status="closed")
    return seal(payload, "binding_fp")


def proof_binding(frozen_conditional_domain, optimization_context):
    domain = json_value(frozen_conditional_domain)
    context = json_value(optimization_context)
    verify(domain, "domain_fingerprint")
    verify(context, "optimization_context_fp")
    scenario = checked_scenario(domain["scenario"])
    conditional = json_value(domain["conditional_domain"])
    verify(conditional, "domain_fingerprint")
    return _binding(domain, context, scenario, conditional)


def identity(frozen_conditional_domain, optimization_context):
    domain = json_value(frozen_conditional_domain)
    context = json_value(optimization_context)
    verify(domain, "domain_fingerprint")
    verify(context, "optimization_context_fp")
    if domain.get("schema_version") != DOMAIN_SCHEMA:
        raise ValueError("conditional research domain schema")
    if domain.get("evidence_track") != TRACK:
        raise ValueError("conditional domain evidence track")
    if domain.get("execution_enabled") is not False:
        raise ValueError("conditional domain is not offline/read-only")
    if domain.get("status") != "static_contract_passed":
        raise ValueError("conditional domain has blockers")
    scenario = checked_scenario(domain["scenario"])
    if scenario.get("evidence_track") != TRACK or scenario.get("execution_enabled") is not False:
        raise ValueError("R1 conditional scenario identity required")
    if scenario.get("status") != "static_contract_passed" or scenario.get("blockers"):
        raise ValueError("R1 conditional scenario has blockers")
    conditional = json_value(domain["conditional_domain"])
    verify(conditional, "domain_fingerprint")
    if conditional.get("schema_version") != "conditional_domain.v1":
        raise ValueError("R1 conditional domain schema")
    if conditional.get("evidence_track") != TRACK:
        raise ValueError("R1 conditional domain evidence track")
    if conditional.get("real_baseline_activation") is not None:
        raise ValueError("conditional domain must not import a real activation baseline")
    if conditional.get("objective_measurement") is not None:
        raise ValueError("conditional domain must not import objective measurements")
    if (conditional.get("execution_enabled") is not False or
            conditional.get("status") != "static_contract_passed" or
            conditional.get("blockers")):
        raise ValueError("R1 conditional domain has blockers")
    if conditional.get("legacy_model_binding_status") != "unsupported_conditional_binding":
        raise ValueError("legacy model binding must stay unsupported")
    if (conditional.get("independent_verification_status") != "not_verified" or
            conditional.get("real_domain_proof_status") != "not_verified"):
        raise ValueError("R1 conditional domain cannot claim independent/real verification")
    parent = domain.get("real_parent_domain")
    if type(parent) is not dict or conditional.get("real_qualification_snapshot") != parent:
        raise ValueError("real parent domain is not the R1 frozen parent")
    if conditional.get("parent_real_domain_fingerprint") != digest(parent):
        raise ValueError("real parent fingerprint mismatch")
    if (conditional.get("scenario_fingerprint") != scenario["scenario_fingerprint"] or
            conditional.get("source_bundle_fingerprint") != scenario["source_bundle_fingerprint"]):
        raise ValueError("conditional domain scenario/source identity mismatch")
    entries = [entry for policy in scenario["policies"] for entry in policy["entries"]]
    if conditional.get("assumption_registry") != entries:
        raise ValueError("approved assumption registry mismatch")
    qualification = _conditional_qualification(scenario)
    if conditional.get("conditional_qualification") != qualification:
        raise ValueError("conditional qualification is not the frozen approved policy")
    if conditional.get("computational_dependency_fingerprint") != \
            _computational_fingerprint(scenario, parent, qualification):
        raise ValueError("computational dependency identity mismatch")
    actions = domain.get("actions")
    route = scenario["route_scenario"]
    if actions != route.get("requested_actions") or actions != conditional.get("finite_actions"):
        raise ValueError("frozen action set X identity mismatch")
    qualities = _validate_actions(actions, qualification, scenario, parent)
    sites = _validate_conditional_sites(domain, qualification, scenario, parent)
    _validate_installed(domain, parent)
    _validate_consumer_binding(domain, context, scenario)
    _validate_enumeration(domain)
    _validate_pruning(domain, actions)
    fixture = _validate_fixture(domain, context, scenario, parent, actions)
    if (domain.get("m3_independent_verification_status") != "not_verified" or
            domain.get("m6_status") != "closed"):
        raise ValueError("M3/M6 research gates must stay not_verified/closed")
    if context.get("schema_version") != CONTEXT_SCHEMA:
        raise ValueError("conditional optimization context schema")
    if context.get("evidence_track") != FIXTURE_TRACK:
        raise ValueError("explicit synthetic conditional fixture required")
    policy = context.get("objective_policy")
    if domain.get("objective_policy") != policy:
        raise ValueError("domain/context objective policy mismatch")
    _validate_objective_policy(policy)
    scope = validate_proof_scope(context.get("proof_scope"))
    if scope != context["proof_scope"]:
        raise ValueError("proof scope canonical identity mismatch")
    if scope.get("kind") not in ("frozen_model_action_domain", "finite_synthetic_domain"):
        raise ValueError("R2.3-A proof scope cannot claim a real or complete Required domain")
    binding = _binding(domain, context, scenario, conditional)
    return dict(domain_fingerprint=domain["domain_fingerprint"],
        optimization_context_fp=context["optimization_context_fp"],
        full_domain_digest=digest(domain), full_context_digest=digest(context),
        algorithm_version=VERSION, action_order_digest=digest(actions),
        objective_policy_fp=digest(policy),
        cost_basis_fp=digest(policy.get("cost_basis")),
        scenario_fingerprint=scenario["scenario_fingerprint"],
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=conditional["computational_dependency_fingerprint"],
        parent_real_domain_fp=digest(parent),
        policy_fingerprint=digest(scenario["policies"]),
        route_id=route["route_id"], path_sha256=route["path_sha256"],
        altitude_value=route["altitude_value"], altitude_units=route["altitude_units"],
        vertical_reference=route["vertical_reference"],
        operating_layer_id=route["operating_layer_id"],
        consumers_fp=digest(binding["consumers"]),
        proof_binding_fp=binding["binding_fp"],
        fixture_sha256=fixture["fixture_sha256"],
        action_count=len(actions), subset_count=2 ** len(actions))


# ---------------------------------------------------------------------------
# Conditional measurement and leaf classification.
# ---------------------------------------------------------------------------


def selected_actions(domain, prefix):
    return [deepcopy(row) for row, bit in zip(domain["actions"], prefix) if bit == "1"]


def action_set_fingerprint(actions):
    rows = sorted(deepcopy(actions), key=lambda row: row["action_id"])
    return digest(dict(schema_version="candidate_action_set.v1", actions=rows))


def _excluded_reasons(domain):
    binding = domain["consumer_binding"]
    families = {row["family"]: row for row in binding["unsupported_action_families"]}
    planners = {row["planner"]: row for row in binding["unsupported_planner_families"]}
    result = {}
    for row in domain["actions"]:
        family, planner = row["service_key"], row["planner_family"]
        if family in families:
            item = families[family]
            result[row["action_id"]] = dict(rule="unsupported_action_family",
                action_id=row["action_id"], family=family, reason=item["reason"],
                source_ref=item["source_ref"])
        elif planner in planners:
            item = planners[planner]
            result[row["action_id"]] = dict(rule="unsupported_action_family",
                action_id=row["action_id"], planner=planner, reason=item["reason"],
                source_ref=item["source_ref"])
    return result


def _site_map(domain):
    return {row["physical_site_id"]: row for row in domain["conditional_sites"]}


def contract_status(domain, actions):
    """Frozen contract classification before any runtime call."""
    excluded = _excluded_reasons(domain)
    for row in actions:
        if row["action_id"] in excluded:
            return dict(kind="infeasible",
                reasons=["unsupported_action_family:" + row["action_id"]])
    sites = _site_map(domain)
    for row in actions:
        site = sites.get(row["physical_site_id"])
        if site is None:
            raise ValueError("action outside conditional site domain")
        if site["conditional_qualification"] != "qualified_under_policy":
            return dict(kind="unknown",
                reasons=["conditional_qualification_unknown:" + row["physical_site_id"]])
    planned, instances = {}, set()
    for row in actions:
        slot = row.get("slot_id")
        if slot is not None:
            key = (row["physical_site_id"], slot)
            if key in planned:
                return dict(kind="infeasible", reasons=["planned_slot_conflict"])
            planned[key] = row["action_id"]
        instance = row.get("device_instance_id")
        if instance is not None:
            if instance in instances:
                return dict(kind="infeasible", reasons=["duplicate_device_instance"])
            instances.add(instance)
    if domain["installed_inventory_status"] == "known":
        occupied = {(row.get("physical_site_id"), row.get("slot_id"))
                    for row in domain["installed_device_instances"]
                    if row.get("slot_id") is not None}
        for key in planned:
            if key in occupied:
                return dict(kind="infeasible", reasons=["installed_slot_conflict"])
    return dict(kind="ok", reasons=[])


def _prediction_valid(prediction, action_fp, domain, policy):
    if type(prediction) is not dict:
        return False
    if prediction.get("action_set_fp") != action_fp:
        return False
    if prediction.get("domain_fingerprint") != domain["domain_fingerprint"]:
        return False
    if prediction.get("model_version") != policy["model_version"]:
        return False
    if prediction.get("prediction_policy_ref") != policy["prediction_policy_ref"]:
        return False
    if prediction.get("dependency_fingerprint") != policy["dependency_fingerprint"]:
        return False
    if not _string(prediction.get("source_ref")):
        return False
    conditions = prediction.get("conditions") or {}
    return all(_string((conditions.get(key) or {}).get("source_ref")) and
               (conditions.get(key) or {}).get("status") == "satisfied"
               for key in ("power", "backhaul", "equipment", "qualification"))


def measure_conditional_objectives(*, actions, frozen_conditional_domain,
                                   objective_policy, raw_evaluation):
    domain = frozen_conditional_domain
    policy = objective_policy
    rows = json_value(actions)
    action_fp = action_set_fingerprint(rows)
    sites = _site_map(domain)
    predictions = raw_evaluation.get("conditional_activation_predictions") or {}
    if type(predictions) is not dict:
        predictions = {}
    involved = sorted({row["physical_site_id"] for row in rows})
    active, activated, inactive, unknown = [], [], [], []
    for sid in involved:
        site = sites[sid]
        baseline = site["conditional_activation_baseline"]
        prediction = predictions.get(sid)
        valid = _prediction_valid(prediction, action_fp, domain, policy)
        status = (prediction or {}).get("status")
        if baseline == "active":
            active.append(sid)
        elif baseline == "inactive" and valid and status == "predicted_active_under_conditional_model":
            activated.append(sid)
        elif baseline == "inactive" and valid and status == "predicted_inactive_under_conditional_model":
            inactive.append(sid)
        else:
            unknown.append(sid)
    lb, ub = len(activated), len(activated) + len(unknown)
    activation_value = None if unknown else lb
    installed_status = domain["installed_inventory_status"]
    installed = domain["installed_device_instances"]
    installed_ids = {row["device_instance_id"] for row in installed}
    new, reconfigured, instance_unknowns = [], [], []
    for row in rows:
        effect = row["device_effect"]
        instance = row.get("device_instance_id")
        if effect == "new_instance":
            if installed_status == "known" and instance in installed_ids:
                raise ValueError("new instance already installed")
            new.append(instance)
        elif effect == "reconfigure_existing":
            if installed_status == "known" and instance not in installed_ids:
                instance_unknowns.append(row["action_id"])
            else:
                reconfigured.append(instance)
        elif effect == "none":
            continue
        else:
            instance_unknowns.append(row["action_id"])
    if installed_status != "known" or instance_unknowns:
        devices = None
        device_upper = None
    else:
        devices = len(new)
        device_upper = len(new)
    cost, cost_status = measure_cost(actions=rows, objective_policy=policy)
    return seal(dict(schema_version=MEASUREMENT_SCHEMA,
        objective_policy=deepcopy(policy), objective_policy_fp=digest(policy),
        domain_fingerprint=domain["domain_fingerprint"], action_set_fp=action_fp,
        measurement_basis="conditional_site_baseline_projection",
        values=dict(activation=activation_value, devices=devices, cost=cost),
        bounds=dict(activation=dict(lower_bound=lb, upper_bound=ub),
            devices=dict(lower_bound=len(new), upper_bound=device_upper),
            cost=dict(lower_bound=cost, upper_bound=cost)),
        activation_count_bounds=[lb, ub],
        activation_count_status="unknown" if activation_value is None else "known",
        conditional_baseline_sources={sid: dict(
            baseline=sites[sid]["conditional_activation_baseline"],
            source_ref=sites[sid]["baseline_source_ref"],
            assumption_refs=deepcopy(sites[sid]["baseline_assumption_refs"]))
            for sid in involved},
        confirmed_sets=dict(baseline_active_sites=active,
            conditional_newly_activated_sites=activated,
            conditional_inactive_sites=inactive),
        unknown_sites=unknown, new_device_instances=new,
        reconfigured_device_instances=reconfigured,
        instance_unknowns=instance_unknowns,
        N_newly_activated_conditional_sites=activation_value,
        N_new_device_instances=devices, cost_status=cost_status,
        input_status="valid", combination_conflicts=[],
        **proposal_flags()), "measurement_fp")


def _validate_raw(raw, actions, context):
    if type(raw) is not dict:
        raise ValueError("runtime evaluation object required")
    if raw.get("evidence_track") != FIXTURE_TRACK:
        raise ValueError("runtime evidence track")
    if raw.get("evaluator_version") != context["evaluator_version"]:
        raise ValueError("runtime evaluator version")
    if raw.get("actions") != actions:
        raise ValueError("runtime action identity mismatch")
    status = raw.get("scoped_model_status")
    if status not in ("model_feasible", "unknown", "unacceptable"):
        raise ValueError("runtime model status")
    if not isinstance(raw.get("conditional_activation_predictions", {}), dict):
        raise ValueError("runtime activation predictions")
    return status


def evaluate_leaf(domain, context, runtime, prefix, actions=None, contract=None):
    actions = actions if actions is not None else selected_actions(domain, prefix)
    contract = contract if contract is not None else contract_status(domain, actions)
    if contract["kind"] == "infeasible":
        return dict(scoped_model_status="unacceptable", runtime_invoked=False,
            contract_reason=contract["reasons"],
            model_evaluation_fp=digest(dict(kind="contract_infeasible",
                action_ids=[row["action_id"] for row in actions],
                reasons=contract["reasons"])),
            objective_measurement=None, model_evaluation=None)
    if contract["kind"] == "unknown":
        return dict(scoped_model_status="unknown", runtime_invoked=False,
            contract_reason=contract["reasons"],
            model_evaluation_fp=digest(dict(kind="contract_unknown",
                action_ids=[row["action_id"] for row in actions],
                reasons=contract["reasons"])),
            objective_measurement=None, model_evaluation=None)
    raw = json_value(runtime.evaluate_action_set(actions=deepcopy(actions)))
    status = _validate_raw(raw, actions, context)
    measurement = None
    if status == "model_feasible":
        measurement = measure_conditional_objectives(
            actions=actions, frozen_conditional_domain=domain,
            objective_policy=context["objective_policy"], raw_evaluation=raw)
        if "objective_measurement" in raw and raw["objective_measurement"] != measurement:
            raise ValueError("runtime objective measurement mismatch")
    return dict(scoped_model_status=status, runtime_invoked=True,
        model_evaluation_fp=digest(raw), objective_measurement=measurement,
        model_evaluation=raw if status == "model_feasible" else None)


# ---------------------------------------------------------------------------
# Auditable bounds and layer summaries.
# ---------------------------------------------------------------------------


def derive_node_bounds(*, node, frozen_conditional_domain):
    domain = frozen_conditional_domain
    prefix = node.get("prefix")
    n = len(domain["actions"])
    if (not isinstance(prefix, str) or len(prefix) > n or
            set(prefix) - {"0", "1"}):
        raise ValueError("node prefix")
    selected = {row["action_id"] for row, bit in zip(domain["actions"], prefix) if bit == "1"}
    excluded = _excluded_reasons(domain)
    for action_id in sorted(selected):
        if action_id in excluded:
            proof = deepcopy(excluded[action_id])
            return dict(lower_bounds=dict(activation=0, devices=0, cost=None),
                prune=proof, domain_scope="frozen_conditional_actions_only",
                unknown_not_excluded=True)
    rules = domain["pruning_rules"]
    for pair in rules["incompatible_action_pairs"]:
        if set(pair) <= selected:
            return dict(lower_bounds=dict(activation=0, devices=0, cost=None),
                prune=dict(rule="incompatible_action_pair",
                    action_ids=list(pair), source_ref=rules["structural_source_ref"]),
                domain_scope="frozen_conditional_actions_only",
                unknown_not_excluded=True)
    limit = rules["max_selected_actions"]
    if limit is not None and len(selected) > limit:
        return dict(lower_bounds=dict(activation=0, devices=0, cost=None),
            prune=dict(rule="explicit_action_count_limit",
                selected_count=len(selected), max_selected_actions=limit,
                source_ref=rules["max_selected_actions_source_ref"]),
            domain_scope="frozen_conditional_actions_only",
            unknown_not_excluded=True)
    # No lower-bound pruning: activation, devices and shared/negative costs are
    # not monotone under supersets. Unknown qualification is never excluded.
    return dict(lower_bounds=dict(activation=0, devices=0, cost=None), prune=None,
        domain_scope="frozen_conditional_actions_only", unknown_not_excluded=True)


def witness(domain, region):
    return dict(actions=selected_actions(domain, region["prefix"]),
        objective_measurement=region["evaluation"]["objective_measurement"],
        model_evaluation=region["evaluation"]["model_evaluation"])


def _collect(domain, regions):
    rows, unknown = [], []
    for region in regions:
        if region["kind"] == "unknown":
            unknown.append(region["prefix"])
        elif (region["kind"] == "evaluated" and
              region["evaluation"]["scoped_model_status"] == "model_feasible"):
            rows.append(witness(domain, region))
    ranked = [row for row in rows if all(
        row["objective_measurement"]["values"][key] is not None
        for key in OBJECTIVES[:2])]
    ranked.sort(key=lambda row: tuple(
        (row["objective_measurement"]["values"][key] is None,
         row["objective_measurement"]["values"][key] or 0) for key in OBJECTIVES))
    return rows, unknown, ranked[0] if ranked else None


def summaries(domain, context, regions, frontier, validated, ident=None, binding=None):
    ident = ident or identity(domain, context)
    binding = binding or proof_binding(domain, context)
    rows, unknown, incumbent = _collect(domain, regions)
    unranked = [row for row in rows if any(
        row["objective_measurement"]["values"][key] is None for key in OBJECTIVES)]
    coverage_complete = not frontier
    prefix, layers, previous_proven = {}, {}, True
    for index, key in enumerate(OBJECTIVES):
        eligible = [row for row in rows
                    if all(row["objective_measurement"]["values"][name] == value
                           for name, value in prefix.items())]
        known = [row for row in eligible
                 if row["objective_measurement"]["values"][key] is not None]
        upper = min((row["objective_measurement"]["values"][key] for row in known),
                    default=None)
        ties = [row for row in known
                if row["objective_measurement"]["values"][key] == upper]
        blockers = list(unknown) + list(frontier)
        for row in rows:
            values = row["objective_measurement"]["values"]
            if any(values[name] is not None and values[name] != value
                   for name, value in prefix.items()):
                continue
            if any(values[name] is None for name in prefix):
                blockers.append(digest(row))
                continue
            if values[key] is None:
                lower = row["objective_measurement"]["bounds"][key]["lower_bound"]
                if upper is None or lower is None or lower <= upper:
                    blockers.append(digest(row))
        proven = (previous_proven and coverage_complete and upper is not None and
                  not blockers and validated)
        if proven:
            status = "proven_optimal"
        elif key == "cost" and any(
                row["objective_measurement"]["values"][key] is None
                for row in eligible):
            status = "cost_unresolved"
        elif upper is not None:
            status = "best_found"
        else:
            status = "incomplete"
        lower = upper if proven else (0 if key != "cost" else None)
        layers["L" + str(index + 1)] = dict(schema_version=LAYER_SCHEMA,
            level="L" + str(index + 1), identity=deepcopy(ident),
            proof_binding=deepcopy(binding), binding_fp=binding["binding_fp"],
            prefix=deepcopy(prefix), status=status, lower_bound=lower,
            upper_bound=upper, witness=ties[0] if ties else None,
            ties=deepcopy(ties), unknown=blockers, regions=deepcopy(regions),
            frontier=deepcopy(frontier), coverage_complete=coverage_complete,
            verification_status="verified" if validated else "not_verified",
            proof_scope=deepcopy(context["proof_scope"]))
        previous_proven = proven
        if upper is not None:
            prefix[key] = upper
        else:
            previous_proven = False
    return layers, rows, unranked, unknown, incumbent


# ---------------------------------------------------------------------------
# Replay verification.
# ---------------------------------------------------------------------------


def verify_conditional_layer_proof(*, layer_proof, frozen_conditional_domain,
                                   optimization_context, runtime, verification_budget):
    used = 0
    try:
        proof, budget = json_value([layer_proof, verification_budget])
        verify(proof, "proof_fp")
        domain = json_value(frozen_conditional_domain)
        context = json_value(optimization_context)
        ident = identity(domain, context)
        if proof.get("identity") != ident:
            raise ValueError("proof identity mismatch")
        binding = proof_binding(domain, context)
        if (proof.get("proof_binding") != binding or
                proof.get("binding_fp") != binding["binding_fp"]):
            raise ValueError("proof binding mismatch")
        if proof.get("level") not in ("L1", "L2", "L3"):
            raise ValueError("proof level")
        required = {"max_joint_replays", "max_elapsed_seconds", "max_memory_mb"}
        if type(budget) is not dict or set(budget) != required:
            raise ValueError("verification budget fields")
        if type(budget["max_joint_replays"]) is not int or budget["max_joint_replays"] < 0:
            raise ValueError("verification replay budget")
        for key in ("max_elapsed_seconds", "max_memory_mb"):
            if type(budget[key]) not in (int, float) or budget[key] < 0:
                raise ValueError("verification budget")
        regions, frontier = proof["regions"], proof["frontier"]
        partition(frontier, regions, len(domain["actions"]))
        start = runtime.monotonic_seconds()

        def check(replay=False):
            if runtime.cancel_requested():
                raise BudgetStop("cancelled")
            if runtime.monotonic_seconds() - start >= budget["max_elapsed_seconds"]:
                raise BudgetStop("verification_time_budget")
            memory = runtime.resident_memory_mb()
            if type(memory) not in (int, float) or memory < 0 or memory > budget["max_memory_mb"]:
                raise BudgetStop("verification_memory_budget")
            if replay and used >= budget["max_joint_replays"]:
                raise BudgetStop("verification_replay_budget")

        for region in regions:
            check()
            prefix = region["prefix"]
            if region["kind"] == "pruned":
                expected = derive_node_bounds(node={"prefix": prefix},
                    frozen_conditional_domain=domain)["prune"]
                if not expected or expected != region.get("proof"):
                    raise ValueError("invalid built-in pruning proof")
            elif region["kind"] in ("evaluated", "unknown"):
                if len(prefix) != len(domain["actions"]):
                    raise ValueError("nonleaf evaluation")
                if region.get("evaluation", {}).get("runtime_invoked"):
                    check(True)
                    used += 1
                actual = evaluate_leaf(domain, context, runtime, prefix)
                if actual != region.get("evaluation") or \
                        (actual["scoped_model_status"] == "unknown") != (region["kind"] == "unknown"):
                    raise ValueError("leaf replay mismatch")
            else:
                raise ValueError("unknown coverage region")
        check()
        unvalidated = summaries(domain, context, regions, frontier, False, ident, binding)[0]
        validated = summaries(domain, context, regions, frontier, True, ident, binding)[0]
        supplied = {key: value for key, value in proof.items() if key != "proof_fp"}
        level = proof["level"]
        if supplied not in (unvalidated[level], validated[level]):
            raise ValueError("layer claim mismatch")
        return dict(verification_status="verified", scope="frozen_model_action_domain",
            layers=validated, actual_replays=used,
            source_authentication_status="not_verified",
            real_domain_proof_status="not_verified",
            independent_verification_status="not_verified")
    except BudgetStop as exc:
        return dict(verification_status="not_verified", reason=str(exc),
            actual_replays=used, source_authentication_status="not_verified",
            real_domain_proof_status="not_verified",
            independent_verification_status="not_verified")
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return dict(verification_status="rejected", reason=str(exc),
            actual_replays=used, source_authentication_status="not_verified",
            real_domain_proof_status="not_verified",
            independent_verification_status="not_verified")


# ---------------------------------------------------------------------------
# Checkpoint.
# ---------------------------------------------------------------------------


def validate_checkpoint(checkpoint, domain, context, policy):
    cp = json_value(checkpoint)
    verify(cp, "checkpoint_digest")
    if cp.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError("checkpoint schema")
    if cp.get("identity") != identity(domain, context):
        raise ValueError("checkpoint identity/policy mismatch")
    previous = checked_policy(cp.get("search_policy"))
    if (previous["algorithm_version"] != policy["algorithm_version"] or
            previous["order_policy"] != policy["order_policy"]):
        raise ValueError("checkpoint algorithm/order mismatch")
    for key in ("max_visited_nodes", "max_joint_replays", "retain_feasible_limit",
                "max_storage_bytes", "max_elapsed_seconds", "max_memory_mb"):
        if policy[key] < previous[key]:
            raise ValueError("checkpoint budget rollback")
    if cp.get("cache_identity") != digest(cp["identity"]):
        raise ValueError("checkpoint cache identity")
    partition(cp["frontier"], cp["regions"], len(domain["actions"]))
    metrics = cp.get("metrics")
    required = {"visited_nodes", "actual_replays", "search_actual_replays",
        "attempts", "cache_hits", "verification_replays",
        "resume_revalidation_replays", "elapsed_seconds"}
    if type(metrics) is not dict or not required <= set(metrics):
        raise ValueError("checkpoint metrics")
    for key, value in metrics.items():
        if type(value) not in (int, float) or value < 0:
            raise ValueError("checkpoint cumulative budget")
    replay_regions = 0
    for region in cp["regions"]:
        if region["kind"] == "pruned":
            expected = derive_node_bounds(node={"prefix": region["prefix"]},
                frozen_conditional_domain=domain)["prune"]
            if not expected or expected != region.get("proof"):
                raise ValueError("checkpoint pruning proof")
        elif region["kind"] in ("evaluated", "unknown"):
            if len(region["prefix"]) != len(domain["actions"]):
                raise ValueError("checkpoint leaf")
            if region["evaluation"].get("runtime_invoked"):
                replay_regions += 1
        else:
            raise ValueError("checkpoint coverage region")
    if metrics["search_actual_replays"] != replay_regions:
        raise ValueError("checkpoint replay count")
    if metrics["actual_replays"] != (metrics["search_actual_replays"] +
            metrics["verification_replays"] + metrics["resume_revalidation_replays"]):
        raise ValueError("checkpoint replay accounting")
    processed = set()
    for prefix in cp["frontier"]:
        processed.update(prefix[:index] for index in range(len(prefix)))
    for region in cp["regions"]:
        processed.update(region["prefix"][:index]
                         for index in range(len(region["prefix"]) + 1))
    if metrics["visited_nodes"] != len(processed):
        raise ValueError("checkpoint node budget mismatch")
    layers, rows, unranked, unknown, incumbent = summaries(
        domain, context, cp["regions"], cp["frontier"], False)
    if cp.get("incumbent") != incumbent or cp.get("unknown_leaves") != unknown:
        raise ValueError("checkpoint incumbent/unknown mismatch")
    if cp.get("completed_layers"):
        validated = summaries(domain, context, cp["regions"], cp["frontier"], True)[0]
        if cp["completed_layers"] != validated:
            raise ValueError("checkpoint completed certificate mismatch")
    return deepcopy(cp)


# ---------------------------------------------------------------------------
# Main generator.
# ---------------------------------------------------------------------------


def _blocked_result(reason, domain=None, context=None, ident=None, binding=None):
    fixture = (domain or {}).get("fixture") if isinstance(domain, dict) else None
    fixture_out = None
    if isinstance(fixture, dict):
        fixture_out = dict(fixture_id=fixture.get("fixture_id"),
            fixture_version=fixture.get("fixture_version"),
            evidence_track=fixture.get("evidence_track"),
            manifest_fp=fixture.get("fixture_sha256"))
    return dict(schema_version=RESULT_SCHEMA, evidence_track=TRACK,
        status="blocked", optimization_status="blocked", termination_reason=reason,
        blockers=[reason], identity=deepcopy(ident), proof_binding=deepcopy(binding),
        binding_fp=(binding or {}).get("binding_fp"), execution_enabled=False,
        coverage_complete=False, optimal_proven=False,
        conditional_infeasible_proven=False, layers={}, incumbent=None,
        feasible_combinations=[], unranked_model_feasible=[], unknown_leaves=[],
        pruned_regions=[], checkpoint=None, metrics={}, fixture=fixture_out,
        proof_scope=deepcopy((context or {}).get("proof_scope")),
        source_authentication_status="not_authenticated",
        real_domain_proof_status="not_verified",
        independent_verification_status="not_verified",
        m3_independent_verification_status="not_verified", m6_status="closed",
        native_model_calls=0, **proposal_flags())


def iter_conditional_lexicographic(*, frozen_conditional_domain,
                                   optimization_context, search_policy, runtime,
                                   checkpoint=None):
    """Yield progress/checkpoint/result events for the frozen conditional domain."""
    policy = checked_policy(search_policy)
    domain = json_value(frozen_conditional_domain)
    context = json_value(optimization_context)
    fixture = domain.get("fixture") if isinstance(domain, dict) else None
    if (type(domain) is not dict or type(context) is not dict or
            type(fixture) is not dict or
            fixture.get("evidence_track") != FIXTURE_TRACK):
        yield dict(kind="result", result=_blocked_result(
            "non_fixture_conditional_execution_not_authorized", domain, context))
        return
    ident = identity(domain, context)
    binding = proof_binding(domain, context)
    if not runtime_available(runtime):
        yield dict(kind="result", result=_blocked_result(
            "runtime_unavailable", domain, context, ident, binding))
        return
    n = len(domain["actions"])
    if checkpoint is None:
        cp = dict(schema_version=CHECKPOINT_SCHEMA, identity=deepcopy(ident),
            search_policy=deepcopy(policy), cache_identity=digest(ident),
            frontier=[""], regions=[],
            metrics=dict(visited_nodes=0, actual_replays=0,
                search_actual_replays=0, attempts=0, cache_hits=0,
                verification_replays=0, resume_revalidation_replays=0,
                elapsed_seconds=0, retained_feasible=0, unknown_leaves=0),
            completed_layers={}, incumbent=None, unknown_leaves=[])
    else:
        cp = validate_checkpoint(checkpoint, domain, context, policy)
    cp.pop("checkpoint_digest", None)
    metrics = cp["metrics"]
    start = runtime.monotonic_seconds()
    elapsed = metrics["elapsed_seconds"]
    prior_layers = deepcopy(cp.get("completed_layers") or {})
    cp["prior_completed_layers"] = prior_layers
    prior_revalidated = False
    reason = "space_exhausted"
    cache = {}
    last_checkpoint_nodes = metrics["visited_nodes"]

    def budget(replay=False):
        metrics["elapsed_seconds"] = elapsed + max(0, runtime.monotonic_seconds() - start)
        if runtime.cancel_requested():
            raise BudgetStop("cancelled")
        if metrics["elapsed_seconds"] >= policy["max_elapsed_seconds"]:
            raise BudgetStop("time_budget")
        memory = runtime.resident_memory_mb()
        if type(memory) not in (int, float) or memory < 0:
            raise BudgetStop("memory_budget_unavailable")
        if memory > policy["max_memory_mb"]:
            raise BudgetStop("memory_budget")
        if replay and metrics["actual_replays"] >= policy["max_joint_replays"]:
            raise BudgetStop("joint_replay_budget")

    def evaluated(prefix, phase):
        metrics["attempts"] += 1
        if prefix in cache:
            metrics["cache_hits"] += 1
            return deepcopy(cache[prefix])
        actions = selected_actions(domain, prefix)
        contract = contract_status(domain, actions)
        invokes = contract["kind"] == "ok"
        budget(invokes)
        if invokes:
            metrics["actual_replays"] += 1
            metrics[phase] += 1
        target = runtime
        if (phase == "resume_revalidation_replays" and
                callable(getattr(runtime, "evaluate_uncached_action_set", None))):
            class FreshRuntime:
                def evaluate_action_set(self, *, actions):
                    return runtime.evaluate_uncached_action_set(actions=actions)
                def monotonic_seconds(self):
                    return runtime.monotonic_seconds()
                def cancel_requested(self):
                    return runtime.cancel_requested()
                def resident_memory_mb(self):
                    return runtime.resident_memory_mb()
            target = FreshRuntime()
        out = evaluate_leaf(domain, context, target, prefix,
                            actions=actions, contract=contract)
        cache[prefix] = out
        return deepcopy(out)

    def snapshot(layers=None):
        rows, unknown, incumbent = _collect(domain, cp["regions"])
        cp["incumbent"] = incumbent
        cp["unknown_leaves"] = unknown
        cp["completed_layers"] = layers or {}
        metrics["retained_feasible"] = len(rows)
        metrics["unknown_leaves"] = len(unknown)
        metrics["elapsed_seconds"] = elapsed + max(0, runtime.monotonic_seconds() - start)
        return seal(cp, "checkpoint_digest")

    try:
        if checkpoint is not None:
            for region in cp["regions"]:
                if region["kind"] == "pruned":
                    continue
                actual = evaluated(region["prefix"], "resume_revalidation_replays")
                if actual != region["evaluation"]:
                    raise ValueError("resume model/cache mismatch")
            if prior_layers:
                expected = summaries(domain, context, cp["regions"],
                    cp["frontier"], True, ident, binding)[0]
                if prior_layers != expected:
                    raise ValueError("resume completed certificate mismatch")
            prior_revalidated = True
        while cp["frontier"]:
            budget()
            if metrics["visited_nodes"] >= policy["max_visited_nodes"]:
                raise BudgetStop("node_budget")
            prefix = cp["frontier"][-1]
            prune = derive_node_bounds(node={"prefix": prefix},
                frozen_conditional_domain=domain)["prune"]
            if prune:
                cp["frontier"].pop()
                cp["regions"].append(dict(kind="pruned", prefix=prefix, proof=prune))
                metrics["visited_nodes"] += 1
            elif len(prefix) < n:
                cp["frontier"].pop()
                cp["frontier"].extend([prefix + "1", prefix + "0"])
                metrics["visited_nodes"] += 1
            else:
                feasible = sum(1 for region in cp["regions"]
                    if region["kind"] == "evaluated" and
                    region["evaluation"]["scoped_model_status"] == "model_feasible")
                if feasible >= policy["retain_feasible_limit"]:
                    raise BudgetStop("retention_budget")
                value = evaluated(prefix, "search_actual_replays")
                cp["frontier"].pop()
                cp["regions"].append(dict(
                    kind=("unknown" if value["scoped_model_status"] == "unknown"
                          else "evaluated"),
                    prefix=prefix, evaluation=value))
                metrics["visited_nodes"] += 1
            if metrics["visited_nodes"] - last_checkpoint_nodes >= 128:
                last_checkpoint_nodes = metrics["visited_nodes"]
                yield dict(kind="checkpoint", checkpoint=snapshot())
            else:
                yield dict(kind="progress", visited_nodes=metrics["visited_nodes"],
                    actual_replays=metrics["actual_replays"])
    except BudgetStop as exc:
        reason = str(exc)

    layers = summaries(domain, context, cp["regions"], cp["frontier"],
                       False, ident, binding)[0]
    verified_all = False
    if reason == "space_exhausted":
        cache.clear()

        class VerificationRuntime:
            def evaluate_action_set(self, *, actions):
                metrics["attempts"] += 1
                budget(True)
                metrics["actual_replays"] += 1
                metrics["verification_replays"] += 1
                target = getattr(runtime, "evaluate_uncached_action_set",
                                 runtime.evaluate_action_set)
                return target(actions=actions)
            def monotonic_seconds(self):
                return runtime.monotonic_seconds()
            def cancel_requested(self):
                return runtime.cancel_requested()
            def resident_memory_mb(self):
                return runtime.resident_memory_mb()

        receipt = verify_conditional_layer_proof(
            layer_proof=seal(layers["L1"], "proof_fp"),
            frozen_conditional_domain=domain, optimization_context=context,
            runtime=VerificationRuntime(),
            verification_budget=dict(
                max_joint_replays=max(0, policy["max_joint_replays"] - metrics["actual_replays"]),
                max_elapsed_seconds=max(0, policy["max_elapsed_seconds"] - metrics["elapsed_seconds"]),
                max_memory_mb=policy["max_memory_mb"]))
        if receipt["verification_status"] == "verified":
            layers = receipt["layers"]
            verified_all = True
        else:
            reason = receipt.get("reason", "verification_incomplete")

    final_cp = snapshot(layers if verified_all else {})
    layers = {key: seal(value, "proof_fp") for key, value in layers.items()}
    top = layers["L1"]
    second = layers["L2"]
    coverage_complete = not cp["frontier"]
    _, rows, unranked, unknown, incumbent = summaries(
        domain, context, cp["regions"], cp["frontier"], verified_all, ident, binding)
    infeasible_proven = (coverage_complete and not rows and not unknown and
                         reason == "space_exhausted" and verified_all)
    if infeasible_proven:
        status = "conditional_infeasible"
    elif (all(layer["status"] == "proven_optimal" and
              layer["verification_status"] == "verified"
              for layer in layers.values())):
        status = "conditional_lexicographic_optimal"
    elif (top["status"] == "proven_optimal" and
            top["verification_status"] == "verified" and
            second["status"] == "proven_optimal" and
            second["verification_status"] == "verified"):
        status = "conditional_first_two_levels_optimal"
    elif incumbent is not None:
        status = "conditional_best_found"
    else:
        status = "conditional_incomplete"
    optimal_proven = status == "conditional_lexicographic_optimal"
    binding = proof_binding(domain, context)
    parent = domain["real_parent_domain"]
    scope = domain["consumer_binding"]
    sources = [dict(source_id=row.get("source_id"), sha256=row.get("sha256"),
        version=row.get("version"), role=row.get("role"),
        evidence_class=row.get("evidence_class"),
        license_status=row.get("license_status"),
        authentication_status="not_authenticated")
        for row in scenario_sources(domain)]
    assumptions, assumption_gaps = _assumption_classification(domain)
    navigation = scope["navigation_evidence"]
    navigation_gaps = [label for field, label in NAV_FIELDS
                       if navigation.get(field) not in SATISFIED_NAVIGATION]
    navigation_status = "confirmed" if not navigation_gaps else "unknown"
    unconsumed_ids = {row.get("constraint_id")
                      for row in scope["unconsumed_requirements"] if type(row) is dict}
    communication_1000 = any(
        row.get("service_key") == "C:communication" and row.get("max_gap_m") == 1000
        and row.get("constraint_id") in unconsumed_ids
        for row in scope["required_inventory"] if type(row) is dict)
    evidence_gaps = set(str(gap) for gap in
                        domain["scenario"].get("evidence_gaps", []))
    evidence_gaps.add("source_authentication_not_verified")
    evidence_gaps.add("independent_verification_not_verified")
    evidence_gaps.add("real_domain_proof_not_verified")
    if navigation_gaps:
        evidence_gaps.add("navigation_evidence_incomplete")
    if communication_1000:
        evidence_gaps.add("communication_1000m_unconsumed")
    evidence_gaps.update(assumption_gaps)
    real_facts = []
    for row in parent.get("site_ledger", []):
        qualification = row.get("qualification") or {}
        real_facts.append(dict(physical_site_id=row.get("physical_site_id"),
            real_qualification_status=qualification.get("status"),
            real_activation_status=row.get("baseline_activation_status"),
            real_source_ref=(row.get("source_ref") or
                             qualification.get("source_ref"))))
    excluded_actions = list(_excluded_reasons(domain).values())
    unknown_details = [dict(prefix=region["prefix"],
        reasons=deepcopy(region["evaluation"].get("contract_reason", [])))
        for region in cp["regions"] if region["kind"] == "unknown"]
    result = dict(schema_version=RESULT_SCHEMA, evidence_track=TRACK,
        status=("completed" if reason == "space_exhausted" else "partial"),
        algorithm_version=VERSION, identity=deepcopy(ident),
        proof_binding=binding, binding_fp=binding["binding_fp"],
        optimization_status=status, termination_reason=reason,
        coverage_complete=coverage_complete, optimal_proven=optimal_proven,
        conditional_infeasible_proven=infeasible_proven,
        layers=layers, incumbent=incumbent, feasible_combinations=rows,
        unranked_model_feasible=unranked, unknown_leaves=unknown,
        unknown_leaf_details=unknown_details,
        pruned_regions=[region for region in cp["regions"]
                        if region["kind"] == "pruned"],
        checkpoint=final_cp, metrics=deepcopy(metrics),
        prior_completed_layers=prior_layers,
        prior_certificate_revalidation_status=("verified" if prior_revalidated
                                               else "not_verified"),
        proof_scope=deepcopy(context["proof_scope"]),
        search_scope=dict(action_count=n, subset_count=2 ** n,
            action_ids=[row["action_id"] for row in domain["actions"]],
            action_order_digest=digest(domain["actions"]),
            enumeration=deepcopy(domain["enumeration"]),
            pruning_rules=deepcopy(domain["pruning_rules"]),
            search_policy=deepcopy(policy), no_implicit_action_truncation=True,
            complete_finite_domain=True),
        excluded_actions=excluded_actions,
        conditional_sites=deepcopy(domain["conditional_sites"]),
        real_activation_facts=real_facts,
        real_qualification_snapshot=deepcopy(parent),
        measurement_basis="conditional_site_baseline_projection",
        required_inventory=deepcopy(scope["required_inventory"]),
        included_constraint_ids=deepcopy(scope["included_constraint_ids"]),
        excluded_constraints=deepcopy(scope["excluded_constraints"]),
        unconsumed_requirements=deepcopy(scope["unconsumed_requirements"]),
        communication_1000m_unconsumed=communication_1000,
        consumer_status=[dict(consumer_id=row["consumer_id"],
            method_ref=row["method_ref"], method_version=row["method_version"],
            model_call_status="not_invoked_by_algorithm",
            source="frozen_consumer_binding") for row in scope["consumers"]],
        navigation_status=navigation_status,
        navigation_evidence_gaps=navigation_gaps,
        complete_required_cns_status="unknown",
        evidence_gaps=sorted(evidence_gaps),
        source_classification=sources,
        source_authentication_status="not_authenticated",
        assumption_classification=assumptions,
        assumption_gaps=assumption_gaps,
        real_domain_proof_status="not_verified",
        independent_verification_status="not_verified",
        m3_independent_verification_status="not_verified",
        m6_status="closed",
        fixture=dict(fixture_id=domain["fixture"]["fixture_id"],
            fixture_version=domain["fixture"]["fixture_version"],
            evidence_track=domain["fixture"]["evidence_track"],
            manifest_fp=domain["fixture"]["fixture_sha256"]),
        execution_enabled=False, final_eligible=False,
        native_model_calls=0,
        **proposal_flags())
    yield dict(kind="checkpoint", checkpoint=final_cp)
    yield dict(kind="result", result=result)


def scenario_sources(domain):
    return domain["scenario"]["regional_bundle"].get("sources", [])


def _assumption_classification(domain):
    entries = [entry for policy in domain["scenario"]["policies"]
               for entry in policy["entries"]]
    rows, gaps = [], []
    for entry in entries:
        kind = entry.get("evidence_class")
        if kind == "B":
            classification = "engineering_proxy"
        elif kind == "C" and entry.get("value") is not None:
            classification = ("approved_conditional_assumption"
                              if _approved(entry) else
                              "conditional_assumption_pending_trusted_basis")
        elif kind in ("C", "D"):
            classification = "unknown"
        else:
            classification = "registered_source"
        if classification in ("unknown",
                               "conditional_assumption_pending_trusted_basis"):
            gaps.append("assumption_gap:" + str(entry.get("parameter_id")))
        rows.append(dict(parameter_id=entry.get("parameter_id"),
            evidence_class=kind, classification=classification,
            approval_ref=entry.get("approval_ref"),
            validity=entry.get("validity"),
            source_ref=entry.get("source_ref"),
            value_present=entry.get("value") is not None,
            missing_reason=entry.get("missing_reason")))
    return rows, sorted(set(gaps))


__all__ = ["iter_conditional_lexicographic", "identity", "proof_binding",
    "verify_conditional_layer_proof", "measure_conditional_objectives",
    "derive_node_bounds", "VERSION", "DOMAIN_SCHEMA", "CONTEXT_SCHEMA",
    "FIXTURE_TRACK"]
