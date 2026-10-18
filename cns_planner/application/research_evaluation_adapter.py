"""R2.1 conditional research evaluation adapter.

Projects an R1 conditional domain into the existing pure M4/M2/M3 consumers only
when trusted code supplies an explicit consumer identity, input applicability,
assumption approval basis and an approved synthetic fixture. This module never
writes ProjectState or artifacts, never reads production data, never promotes
conditional evidence into native results, and never enables execution.
"""
from copy import deepcopy

from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..algorithms.existing_site_lexicographic.v2 import preflight_real_domain
from ..domain.joint_feasibility import digest, json_value, seal, verify
from ..domain.research_scenario import TRACK, bounded_json, checked_scenario
from .candidate_ue_acceptability import candidate_action_set_fingerprint
from .joint_candidate_assessment import bind_joint_candidate_assessment
from .joint_evaluation_adapter import JointModelEvaluator
from .research_scenario_admission import inspect_research_admission
from .research_scenario_builder import build_conditional_domain
from .research_solver_envelope import build_real_solver_envelope, verify_real_solver_envelope
from .rtk_joint_acceptance import VERSION as RTK_VERSION

INPUTS_SCHEMA = "research_evaluation_inputs.v1"
OUTPUT_SCHEMA = "research_evaluation.v1"
REAL_OUTPUT_SCHEMA = "research_real_evaluation.v1"
REGISTRY_SCHEMA = "research_consumer_registry.v1"
FIXTURE_TRACK = "synthetic_fixture"
M4_CONSUMER = "m4_joint"
M4_METHOD_REF = "cns_planner.application.joint_evaluation_adapter.JointModelEvaluator"
M3_METHOD_REF = "cns_planner.application.joint_candidate_assessment.bind_joint_candidate_assessment"
M3_METHOD_VERSION = "joint_candidate_assessment.v1"
RTK_METHOD_REF = "cns_planner.application.rtk_joint_acceptance.evaluate_p17_rtk_joint"
M4_METHOD_VERSION = "p14:{};p15:{};p17:{};m2:{}".format(
    CNSServiceCorridorV1.algorithm_version, CNSCorridorGapAnalyzerV1.algorithm_version,
    ContinuousServiceAcceptabilityV1.algorithm_version, RTK_VERSION)
DEPENDENT_CONSUMERS = {"m2_rtk": RTK_METHOD_REF, "m3_binding": M3_METHOD_REF}
SUPPORTED_FAMILIES = ("C:communication", "N:rtk_augmentation")
UNSUPPORTED_FAMILY_CODES = {
    "S:radar_noncooperative": "unsupported_action_family:S:radar_noncooperative",
    "S:rid_cooperative": "rid_figure_only_not_a_service_consumer",
    "N:navigation_integrity_monitoring": "endpoint_new_action_unsupported",
}
BINDING_FIELDS = ("action_id", "service_key", "planner_family", "physical_site_id",
    "device_id", "device_instance_id")
NAVIGATION_EVIDENCE_FIELDS = (
    ("provider_inventory_status", "provider_inventory"),
    ("device_status", "device_status"),
    ("handover_status", "handover"),
    ("speed_profile_status", "speed_profile"),
    ("backup_integrity_status", "backup_integrity"),
)
SATISFIED_NAVIGATION_STATES = ("satisfied", "confirmed", "current", "complete")


def _model_input_description(route, fixture_context, sources, assumptions, results,
                             projections, included):
    manifest = fixture_context["manifest"]
    scope = fixture_context["context"].get("assessment_scope", {}) or {}
    description = dict(schema_version="research_model_input.v1",
        consumer_id=M4_CONSUMER, method_ref=M4_METHOD_REF,
        method_version=M4_METHOD_VERSION,
        research_input_origin=TRACK, model_input_origin=FIXTURE_TRACK,
        fixture=dict(fixture_id=manifest.get("fixture_id"),
            fixture_version=manifest.get("fixture_version"),
            fixture_sha256=fixture_context["fixture"].get("fixture_sha256")),
        route=dict(route_id=route.get("route_id"),
            path_sha256=route.get("path_sha256"),
            altitude_value=route.get("altitude_value"),
            altitude_units=route.get("altitude_units"),
            vertical_reference=route.get("vertical_reference"),
            operating_layer_id=route.get("operating_layer_id"),
            scope=route.get("scope")),
        model_scope=dict(scope_id=scope.get("scope_id"), kind=scope.get("kind"),
            included_constraint_ids=deepcopy(scope.get("included_constraint_ids", []))),
        required_constraint_ids=deepcopy(included),
        sources=deepcopy(sources), assumptions=deepcopy(assumptions),
        actions=deepcopy([projections[key] for key in sorted(projections)]),
        action_manifest=[dict(action_id=row["action_id"],
            physical_site_id=row["physical_site_id"],
            device_model_id=row["device_model_id"],
            device_instance_id=row["device_instance_id"], slot_id=row["slot_id"],
            family=row["family"],
            dependency_action_ids=deepcopy(row["dependency_action_ids"]),
            applicable_models=deepcopy(row["applicable_models"]),
            projection_digest=row["projection_digest"]) for row in results
            if row["status"] == "supported"])
    return seal(description, "model_input_fingerprint")


def _checked_frozen(frozen_inputs):
    frozen = bounded_json(frozen_inputs)
    if type(frozen) is not dict or frozen.get("schema_version") != INPUTS_SCHEMA:
        raise ValueError("research evaluation inputs schema")
    scenario = checked_scenario(frozen.get("scenario"))
    domain = bounded_json(frozen.get("conditional_domain"))
    if type(domain) is not dict:
        raise ValueError("conditional domain object required")
    verify(domain, "domain_fingerprint")
    if domain.get("scenario_fingerprint") != scenario["scenario_fingerprint"]:
        raise ValueError("conditional domain scenario identity mismatch")
    rebuilt = build_conditional_domain(
        parent_domain_review=domain["real_qualification_snapshot"], scenario=scenario,
        assumption_registry=domain["assumption_registry"])
    if rebuilt != domain:
        raise ValueError("conditional domain canonical identity mismatch")
    route = scenario["route_scenario"]
    if type(route.get("required_scope")) is not dict:
        raise ValueError("required scope object required")
    evidence = frozen.get("route_evidence")
    if type(evidence) is not dict:
        raise ValueError("route evidence object required")
    native_m2 = frozen.get("native_m2_result")
    if native_m2 is not None:
        json_value(native_m2)
    fixture = frozen.get("fixture")
    if fixture is not None:
        if type(fixture) is not dict or type(fixture.get("manifest")) is not dict:
            raise ValueError("fixture manifest object required")
        if fixture.get("evidence_track") != FIXTURE_TRACK:
            raise ValueError("fixture evidence track")
        if fixture.get("fixture_sha256") != digest(fixture["manifest"]):
            raise ValueError("fixture manifest fingerprint mismatch")
    return dict(frozen=frozen, scenario=scenario, domain=domain, route=route,
        evidence=evidence, fixture=fixture, native_m2=native_m2)


def _consumers(consumer_registry):
    registry = json_value(consumer_registry)
    if type(registry) is not dict or registry.get("schema_version") != REGISTRY_SCHEMA:
        raise ValueError("research consumer registry schema")
    rows = registry.get("consumers")
    if type(rows) is not list or not rows:
        raise ValueError("explicit consumers required")
    consumers = {}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("consumer objects required")
        consumer_id = row.get("consumer_id")
        if type(consumer_id) is not str or not consumer_id or consumer_id in consumers:
            raise ValueError("unique consumer identity required")
        if type(row.get("supported_action_families")) is not list:
            raise ValueError("consumer families list required")
        if type(row.get("input_applicability")) is not dict:
            raise ValueError("consumer applicability object required")
        if "assumption_approval_basis" in row and type(row["assumption_approval_basis"]) is not dict:
            raise ValueError("consumer approval basis object required")
        row.setdefault("assumption_approval_basis", {})
        consumers[consumer_id] = row
    if M4_CONSUMER not in consumers:
        raise ValueError("m4_joint consumer required")
    return consumers


def _consumer_errors(consumers, scenario, domain, route):
    errors = []
    if set(consumers) != {M4_CONSUMER}:
        errors.extend("consumer_method_unsupported:"+cid
            for cid in sorted(set(consumers)-{M4_CONSUMER}))
    m4 = consumers[M4_CONSUMER]
    if m4.get("method_ref") != M4_METHOD_REF:
        errors.append("consumer_method_unsupported:"+M4_CONSUMER)
    if m4.get("method_version") != M4_METHOD_VERSION:
        errors.append("consumer_method_version_mismatch:"+M4_CONSUMER)
    for family in m4["supported_action_families"]:
        if family not in SUPPORTED_FAMILIES:
            errors.append("consumer_claims_unsupported_capability:"+str(family))
    expected = dict(scenario_fingerprint=scenario["scenario_fingerprint"],
        domain_fingerprint=domain["domain_fingerprint"],
        source_bundle_fingerprint=scenario["source_bundle_fingerprint"],
        computational_dependency_fingerprint=domain["computational_dependency_fingerprint"],
        real_parent_fingerprint=digest(domain["real_qualification_snapshot"]),
        route_id=route["route_id"], path_sha256=route["path_sha256"],
        altitude_value=route["altitude_value"],
        vertical_reference=route["vertical_reference"],
        operating_layer_id=route["operating_layer_id"])
    for key, value in expected.items():
        if m4["input_applicability"].get(key) != value:
            errors.append("consumer_input_inapplicable:"+M4_CONSUMER+":"+key)
    dependencies = m4.get("dependent_consumers")
    if type(dependencies) is not list or set(dependencies) != set(DEPENDENT_CONSUMERS):
        errors.append("dependent_consumer_not_declared")
    return errors


def _fixture_context(fixture, consumers, scenario, route):
    if fixture is None:
        return None, []
    if M4_CONSUMER not in consumers:
        return None, ["consumer_unavailable:"+M4_CONSUMER]
    m4 = consumers[M4_CONSUMER]
    errors = []
    binding = m4.get("fixture")
    if type(binding) is not dict:
        return None, ["fixture_not_registered_for_consumer:"+M4_CONSUMER]
    for key in ("fixture_id", "fixture_version", "fixture_sha256"):
        if binding.get(key) != fixture.get(key):
            errors.append("fixture_identity_mismatch:"+key)
    material = m4.get("fixture_material")
    if type(material) is not dict:
        return None, errors + ["fixture_material_required"]
    context = material.get("frozen_context")
    domain = material.get("frozen_domain")
    if type(context) is not dict or type(domain) is not dict:
        return None, errors + ["fixture_material_object_required"]
    verify(context, "context_fingerprint")
    verify(domain, "domain_fingerprint")
    manifest = fixture["manifest"]
    observed = dict(context_fingerprint=context["context_fingerprint"],
        domain_fingerprint=domain["domain_fingerprint"],
        scope_fingerprint=context["scope_fingerprint"],
        scope_id=context["assessment_scope"]["scope_id"],
        action_set_fingerprint=candidate_action_set_fingerprint(
            actions=domain["installable_actions"]),
        route_id=context["frozen_rule_snapshot"]["route_id"],
        operating_layer_id=context["frozen_rule_snapshot"]["frozen_inputs"]
            ["operation_scenario"]["operating_layer_id"])
    for key, value in observed.items():
        if manifest.get(key) != value:
            errors.append("fixture_material_mismatch:"+key)
    for key in ("fixture_id", "fixture_version", "evidence_track"):
        if manifest.get(key) != fixture.get(key):
            errors.append("fixture_manifest_identity_mismatch:"+key)
    if (context.get("frozen_domain") or {}).get(
            "domain_fingerprint") != domain["domain_fingerprint"]:
        errors.append("fixture_context_domain_mismatch")
    if context.get("domain_fingerprint") != domain["domain_fingerprint"]:
        errors.append("fixture_context_domain_mismatch")
    if context.get("evidence_track") != FIXTURE_TRACK:
        errors.append("fixture_evidence_track_required")
    if observed["route_id"] != route["route_id"]:
        errors.append("fixture_research_route_mismatch")
    if observed["operating_layer_id"] != route["operating_layer_id"]:
        errors.append("fixture_research_layer_mismatch")
    declared_scope = m4["input_applicability"].get("scope_id")
    if declared_scope is not None and declared_scope != observed["scope_id"]:
        errors.append("consumer_input_inapplicable:"+M4_CONSUMER+":scope_id")
    return dict(fixture=fixture, context=context, domain=domain, manifest=manifest,
        native_map={row["action_id"]: row for row in domain["installable_actions"]},
        material=material), errors


def _action_results(rows, consumers, domain, fixture_context):
    parent = domain["real_qualification_snapshot"]
    parent_ids = {row.get("physical_site_id") for row in parent.get("site_ledger", [])
        if type(row) is dict}
    frozen = {row.get("action_id"): row for row in domain.get("finite_actions", [])
        if type(row) is dict}
    families = consumers[M4_CONSUMER]["supported_action_families"]
    groups = {}
    for row in rows:
        key = (row.get("physical_site_id"), row.get("device_id"))
        groups[key] = groups.get(key, 0) + 1
    results, projections, errors, seen = [], {}, [], set()
    frozen_ids = set(frozen)
    missing_x = sorted(str(item) for item in frozen_ids - {row.get("action_id") for row in rows})
    if missing_x:
        errors.append("frozen_action_set_incomplete:"+",".join(missing_x))
    for row in rows:
        action_id = row.get("action_id")
        family = row.get("service_key")
        planner = row.get("planner_family")
        reasons = []
        if type(action_id) is not str or not action_id:
            reasons.append("action_id_required")
        if type(family) is not str or not family:
            reasons.append("action_family_required")
        for field in ("planner_family", "physical_site_id", "device_id",
                "device_instance_id", "slot_id", "source_class"):
            if type(row.get(field)) is not str or not row.get(field):
                reasons.append("action_field_missing:"+field)
        for field in ("dependency_action_ids", "applicable_models", "assumption_refs"):
            value = row.get(field)
            if type(value) is not list or any(type(item) is not str for item in value):
                reasons.append("action_field_missing:"+field)
        if row.get("source_class") not in ("A", "B", "C", "D"):
            reasons.append("action_source_class_invalid")
        if action_id in seen:
            reasons.append("duplicate_action_id")
        seen.add(action_id)
        if action_id not in frozen or frozen.get(action_id) != row:
            reasons.append("action_outside_frozen_x:"+str(action_id))
        if row.get("physical_site_id") not in parent_ids:
            reasons.append("action_site_not_in_real_parent_ledger")
        if type(row.get("applicable_models")) is list:
            registered = set(consumers) | set(DEPENDENT_CONSUMERS)
            for model in row["applicable_models"]:
                if model not in registered:
                    reasons.append("applicable_model_unregistered:"+str(model))
        if groups.get((row.get("physical_site_id"), row.get("device_id")), 0) > 1:
            reasons.append("unsupported_multiplicity")
        if type(row.get("dependency_action_ids")) is list:
            for dependency in row["dependency_action_ids"]:
                if dependency not in frozen_ids:
                    reasons.append("action_dependency_missing:"+str(dependency))
        status = "supported"
        code = UNSUPPORTED_FAMILY_CODES.get(family)
        if code is None and planner == "directional_radar":
            code = "unsupported_action_family:S:radar_noncooperative"
        if code is None and planner == "endpoint_integrity_monitor":
            code = "endpoint_new_action_unsupported"
        if code is not None:
            status = "unsupported"
            reasons.append(code)
        elif family not in families:
            reasons.append("consumer_unavailable:"+str(family))
        if family == "N:rtk_augmentation" and row.get("equipment_selection_status") in (
                "not_selected", "real_selection_pending", "unsupported"):
            reasons.append("rtk_real_selection_unavailable")
        if fixture_context is not None and status == "supported" and action_id in frozen:
            native = fixture_context["native_map"].get(action_id)
            if native is None:
                reasons.append("native_action_not_in_fixture_domain")
            else:
                for field in BINDING_FIELDS:
                    if row.get(field) != native.get(field):
                        reasons.append("action_projection_mismatch:"+field)
                if (family == "N:rtk_augmentation" and
                        native.get("equipment_selection_status") != "synthetic_fixture_selected"):
                    reasons.append("rtk_real_selection_unavailable")
                projections[action_id] = deepcopy(native)
        if reasons and status == "supported":
            status = "blocked"
        errors.extend(reasons)
        results.append(dict(action_id=action_id, family=family, status=status,
            physical_site_id=row.get("physical_site_id"),
            device_model_id=row.get("device_id"),
            device_instance_id=row.get("device_instance_id"),
            slot_id=row.get("slot_id"),
            dependency_action_ids=deepcopy(row.get("dependency_action_ids", [])),
            applicable_models=deepcopy(row.get("applicable_models", [])),
            source_class=row.get("source_class"),
            assumption_refs=deepcopy(row.get("assumption_refs", [])),
            reason_codes=sorted(set(reasons)),
            projection_digest=digest(projections[action_id]) if action_id in projections else None,
            applied=False))
    return results, projections, errors


def _evidence_classification(scenario, consumers, fixture_context):
    bundle = scenario["regional_bundle"]
    basis = consumers[M4_CONSUMER].get("assumption_approval_basis") or {}
    sources, source_gaps = [], []
    for row in bundle.get("sources", []):
        sources.append(dict(source_id=row.get("source_id"), sha256=row.get("sha256"),
            version=row.get("version"), role=row.get("role"),
            evidence_class=row.get("evidence_class"),
            license_status=row.get("license_status"),
            authentication_status="not_authenticated"))
    source_gaps.extend(gap for gap in scenario.get("evidence_gaps", [])
        if str(gap).startswith("source_"))
    source_gaps.append("source_authentication_not_verified")
    entries = [entry for policy in scenario.get("policies", [])
        for entry in policy.get("entries", []) if type(entry) is dict]
    by_id = {entry.get("parameter_id"): entry for entry in entries}
    errors, assumptions, assumption_gaps = [], [], []
    if fixture_context is not None:
        for entry in entries:
            parameter_id = entry.get("parameter_id")
            if entry.get("evidence_class") == "C" and entry.get("value") is not None:
                reference = basis.get(parameter_id)
                if not reference:
                    errors.append("assumption_approval_basis_missing:"+str(parameter_id))
                elif reference != entry.get("approval_ref"):
                    errors.append("assumption_approval_basis_mismatch:"+str(parameter_id))
        for parameter_id in basis:
            entry = by_id.get(parameter_id)
            if (entry is None or entry.get("evidence_class") != "C" or
                    entry.get("value") is None):
                errors.append("assumption_basis_unknown:"+str(parameter_id))
    for entry in entries:
        parameter_id = entry.get("parameter_id")
        kind = entry.get("evidence_class")
        if kind == "B":
            classification = "engineering_proxy"
            status = "declared_model_proxy"
        elif kind == "C" and entry.get("value") is not None:
            bound = basis.get(parameter_id) == entry.get("approval_ref")
            classification = ("approved_conditional_assumption" if bound
                else "conditional_assumption_pending_trusted_basis")
            status = "trusted_basis_bound" if bound else "declared_only"
        elif kind in ("C", "D"):
            classification = "unknown"
            status = "unknown"
        else:
            classification = "registered_source"
            status = "declared"
        assumptions.append(dict(parameter_id=parameter_id,
            policy_id=next((policy.get("policy_id") for policy in scenario.get("policies", [])
                if entry in policy.get("entries", [])), None),
            policy_version=entry.get("policy_version"), evidence_class=kind,
            classification=classification, basis_status=status,
            json_approval_ref=entry.get("approval_ref"),
            source_ref=entry.get("source_ref"),
            value_present=entry.get("value") is not None,
            missing_reason=entry.get("missing_reason")))
        if classification in ("unknown", "conditional_assumption_pending_trusted_basis"):
            assumption_gaps.append("assumption_gap:"+str(parameter_id))
    return sources, sorted(set(source_gaps)), assumptions, sorted(set(assumption_gaps)), errors


def _admission(scenario, domain, evidence, consumers, native_m2):
    capabilities = dict(consumers={cid: row.get("method_version")
            for cid, row in consumers.items()},
        supported_action_families=sorted({family
            for row in consumers.values() for family in row["supported_action_families"]}))
    intent = dict(mode="inspect", native_m2_result=deepcopy(native_m2),
        output_root=None)
    return inspect_research_admission(scenario=scenario, conditional_domain=domain,
        route_evidence=evidence, consumer_capabilities=capabilities, run_intent=intent)


def _native_parent_chain(native, context, fixture_domain, route, manifest, projected, m3):
    materials = native.get("native_materials", {}) or {}
    baseline = (native.get("native_results", {}) or {}).get("baseline", {}) or {}
    post = (native.get("native_results", {}) or {}).get("post_plan", {}) or {}
    p14, p15 = post.get("p14", {}) or {}, post.get("p15", {}) or {}
    layers = {voxel.get("altitude_layer_id") for item in p14.get("routes", [])
        for voxel in item.get("voxels", [])}
    baseline_p15 = (materials.get("baseline", {}) or {}).get("p15", {})
    post_p15 = (materials.get("p15", {}) or {})
    p17 = ((materials.get("p17", {}) or {}).get("evaluation_inputs", {}) or {})
    chain = dict(
        context_fingerprint=native.get("context_fingerprint") == context.get("context_fingerprint"),
        domain_fingerprint=context.get("domain_fingerprint") == fixture_domain.get("domain_fingerprint"),
        scope_fingerprint=context.get("scope_fingerprint") ==
            (context.get("assessment_scope", {}) or {}).get("scope_fingerprint"),
        scope_id=(context.get("assessment_scope", {}) or {}).get("scope_id") == manifest.get("scope_id"),
        action_set_fingerprint=native.get("action_set_fingerprint") == manifest.get("action_set_fingerprint"),
        route_id=any(item.get("route_id") == route.get("route_id") for item in p14.get("routes", [])),
        operating_layer_id=route.get("operating_layer_id") in layers,
        baseline_p15_parent=(baseline_p15.get("fingerprint_input", {}) or {}).get(
            "corridor_assessment") == baseline.get("p14"),
        post_p15_parent=(post_p15.get("fingerprint_input", {}) or {}).get(
            "corridor_assessment") == p14,
        p17_baseline_parent=p17.get("corridor_assessment") == baseline.get("p14"),
        p17_post_p14_parent=(p17.get("post_plan_projection", {}) or {}).get(
            "corridor_assessment") == p14,
        p17_post_p15_parent=(p17.get("post_plan_projection", {}) or {}).get(
            "corridor_gap_assessment") == p15,
        p17_selected_actions=(p17.get("site_plan", {}) or {}).get(
            "selected_actions") == sorted(projected, key=lambda row: row["action_id"]))
    if m3 is not None:
        chain["m3_context"] = m3.get("context_fingerprint") == context.get("context_fingerprint")
        chain["m3_action_set"] = m3.get(
            "action_set_fingerprint") == manifest.get("action_set_fingerprint")
    return {key: bool(value) for key, value in chain.items()}


def _navigation(native, consumers):
    m2 = (native or {}).get("rtk_joint_result")
    evidence = consumers[M4_CONSUMER].get("navigation_evidence")
    evidence = evidence if type(evidence) is dict else {}
    gaps = []
    for field, label in NAVIGATION_EVIDENCE_FIELDS:
        if evidence.get(field) not in SATISFIED_NAVIGATION_STATES:
            gaps.append(label)
    if m2 is None:
        gaps.append("m2_result_missing")
    native_status = (m2 or {}).get("navigation_status", "unknown")
    status = native_status if native_status in ("unacceptable", "confirmed_deficit") else "unknown"
    return status, sorted(set(gaps)), m2


def evaluate_conditional_actions(*, frozen_inputs, actions, consumer_registry) -> dict:
    """Evaluate a frozen conditional action set with fixture-only pure models."""
    checked = _checked_frozen(frozen_inputs)
    scenario, domain, route = checked["scenario"], checked["domain"], checked["route"]
    fixture, native_m2 = checked["fixture"], checked["native_m2"]
    consumers = _consumers(consumer_registry)
    rows = bounded_json(actions)
    if type(rows) is not list or not rows or any(type(row) is not dict for row in rows):
        raise ValueError("non-empty action object list required")
    errors = list(scenario.get("blockers", [])) + list(domain.get("blockers", []))
    errors.extend(_consumer_errors(consumers, scenario, domain, route))
    admission = _admission(scenario, domain, checked["evidence"], consumers, native_m2)
    errors.extend(admission.get("blockers", []))
    fixture_context, fixture_errors = _fixture_context(fixture, consumers, scenario, route)
    errors.extend(fixture_errors)
    results, projections, action_errors = _action_results(rows, consumers, domain,
        fixture_context)
    errors.extend(action_errors)
    sources, source_gaps, assumptions, assumption_gaps, evidence_errors = \
        _evidence_classification(scenario, consumers, fixture_context)
    errors.extend(evidence_errors)
    required_scope = route["required_scope"]
    inventory = deepcopy(required_scope.get("required_constraint_inventory", []))
    included = deepcopy(required_scope.get("included_constraint_ids", []))
    excluded = deepcopy(required_scope.get("excluded_constraints", []))
    unconsumed = deepcopy(admission.get("unconsumed_requirements", []))
    consumer_mapping = {row.get("constraint_id"): deepcopy(row.get("consumer"))
        for row in inventory if type(row) is dict}
    requirement_1000 = any(row.get("service_key") == "C:communication" and
        row.get("max_gap_m") == 1000 for row in inventory)
    native, m3, model_input = None, None, None
    native_chain, native_fingerprints = {}, {}
    native_errors = []
    if fixture_context is not None and not errors:
        projected = [projections[key] for key in sorted(projections)]
        model_input = _model_input_description(route, fixture_context, sources,
            assumptions, results, projections, included)
        try:
            native = JointModelEvaluator(
                frozen_context=deepcopy(fixture_context["context"]),
                frozen_domain=deepcopy(fixture_context["domain"])).evaluate(actions=projected)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            native_errors.append("native_model_error:"+str(exc)[:160])
        if native is not None:
            if native.get("evaluation_status") != "evaluated":
                native_errors.append("native_evaluation_unavailable")
            if native.get("evidence_track") != FIXTURE_TRACK:
                native_errors.append("native_evidence_track_mismatch")
            expected_ids = sorted(row["action_id"] for row in projected)
            if native.get("applied_action_ids") != expected_ids:
                native_errors.append("native_applied_action_mismatch")
            native_chain = _native_parent_chain(native, fixture_context["context"],
                fixture_context["domain"], route, fixture_context["manifest"],
                projected, None)
            if not all(native_chain.values()):
                native_errors.append("native_parent_chain_mismatch")
            native_fingerprints = dict(
                context_fingerprint=fixture_context["context"].get("context_fingerprint"),
                domain_fingerprint=fixture_context["domain"].get("domain_fingerprint"),
                scope_fingerprint=fixture_context["context"].get("scope_fingerprint"),
                action_set_fingerprint=native.get("action_set_fingerprint"),
                p14_input_fingerprint=((native.get("native_results", {}).get(
                    "post_plan", {}) or {}).get("p14", {}) or {}).get("input_fingerprint"),
                p15_input_fingerprint=((native.get("native_results", {}).get(
                    "post_plan", {}) or {}).get("p15", {}) or {}).get("input_fingerprint"),
                p17_input_fingerprint=(native.get("native_results", {}).get(
                    "p17", {}) or {}).get("input_fingerprint"),
                m2_input_fingerprint=((native.get("rtk_joint_result") or {}) or {}).get(
                    "input_fingerprint"))
            if not native_errors:
                material = fixture_context["material"]
                try:
                    m3 = bind_joint_candidate_assessment(model_evaluation=native,
                        frozen_context=fixture_context["context"], actions=projected,
                        evidence_registry=material.get("m3_evidence_registry") or {})
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    native_errors.append("m3_consumer_unavailable:"+str(exc)[:160])
                if m3 is not None:
                    context_fp = fixture_context["context"].get("context_fingerprint")
                    action_fp = fixture_context["manifest"].get("action_set_fingerprint")
                    if (m3.get("input_status") != "consistent" or
                            m3.get("context_fingerprint") != context_fp or
                            m3.get("action_set_fingerprint") != action_fp):
                        native_errors.append("m3_input_inconsistent")
                    native_chain = _native_parent_chain(native, fixture_context["context"],
                        fixture_context["domain"], route, fixture_context["manifest"],
                        projected, m3)
                    if not all(native_chain.values()):
                        native_errors.append("native_parent_chain_mismatch")
    errors.extend(native_errors)
    applied_ids = list((native or {}).get("applied_action_ids", []))
    applied_set = set(applied_ids)
    for row in results:
        row["applied"] = row["action_id"] in applied_set
    if native is not None and any(row["status"] == "supported" and not row["applied"]
            for row in results):
        errors.append("native_applied_action_mismatch")
    navigation_status, navigation_gaps, m2_chain = _navigation(native, consumers)
    gaps = list(admission.get("evidence_gaps", [])) + source_gaps + assumption_gaps
    if requirement_1000 and "communication_1000m_unconsumed" not in gaps:
        gaps.append("communication_1000m_unconsumed")
    blockers = sorted(set(str(item) for item in errors))
    if blockers:
        status = "blocked"
    elif native is not None:
        status = "evaluated_fixture_only"
    elif fixture is None:
        status = "not_evaluated_real_inputs"
    else:
        status = "blocked"
    native_results = {}
    if native is not None:
        native_results["m4_joint"] = native
    if m3 is not None:
        native_results["m3_binding"] = m3
    if status == "evaluated_fixture_only" and native is not None:
        scoped_status = native.get("scoped_model_status", "unknown")
    elif fixture is None and not blockers:
        scoped_status = "not_evaluated"
    else:
        scoped_status = "unknown"
    native_complete = (native or {}).get("complete_required_cns_status")
    complete_status = "unacceptable" if native_complete == "unacceptable" else "unknown"
    effects = []
    for row in results:
        if row["applied"]:
            effects.append(dict(action_id=row["action_id"], consumer_id=M4_CONSUMER,
                effect="native_model_applied", applied=True))
        elif row["status"] != "supported":
            effects.append(dict(action_id=row["action_id"], effect="action_not_applied",
                reason=row["reason_codes"][0] if row["reason_codes"] else "unsupported"))
    if native is not None:
        effects.append(dict(effect="navigation_unknown",
            reasons=navigation_gaps or ["fixture_only_navigation_evidence"]))
        effects.append(dict(effect="complete_required_cns_not_claimed", status="unknown"))
    if requirement_1000:
        effects.append(dict(effect="communication_1000m_unconsumed"))
    if (native_m2 is not None and native_m2.get("navigation_status") == "satisfied"
            and navigation_status == "unknown"):
        effects.append(dict(effect="native_m2_positive_not_promoted",
            reason="navigation evidence incomplete"))
    consumer_identities = [dict(consumer_id=M4_CONSUMER,
        method_ref=M4_METHOD_REF, method_version=M4_METHOD_VERSION,
        source="trusted_registry",
        model_call_status=("invoked_fixture_only" if native is not None else "not_invoked"))]
    consumer_identities.append(dict(consumer_id="m2_rtk", method_ref=RTK_METHOD_REF,
        method_version=RTK_VERSION, source="m4_fixed_chain",
        model_call_status=("consumed" if m2_chain is not None else "not_invoked")))
    consumer_identities.append(dict(consumer_id="m3_binding", method_ref=M3_METHOD_REF,
        method_version=M3_METHOD_VERSION, source="m4_dependent_chain",
        model_call_status=("consumed" if m3 is not None else "not_invoked")))
    m3_status = dict(status="not_invoked")
    if m3 is not None:
        m3_status = dict(status="bound", input_status=m3.get("input_status"),
            scoped_model_status=m3.get("scoped_model_status"),
            complete_required_cns_status=m3.get("complete_required_cns_status"),
            formal_ue_status=m3.get("formal_ue_status"),
            prefilter_status=m3.get("prefilter_status"),
            independent_verification_status=m3.get("independent_verification_status"))
    radar_present = any(row.get("family") == "S:radar_noncooperative" for row in results)
    radar_diagnostic = "not_present"
    if radar_present:
        radar_diagnostic = "rejected_new_action_not_supported"
    elif native is not None:
        radar_evidence = ((native.get("native_materials", {}) or {}).get(
            "radar_service_evidence"))
        if radar_evidence not in (None, {}, []):
            radar_diagnostic = "diagnostic_only_not_consumed"
    unsupported = []
    for gap in navigation_gaps:
        if gap in ("provider_inventory", "handover", "speed_profile", "backup_integrity"):
            unsupported.append("per_segment_"+gap)
    for row in results:
        if row.get("family") == "S:rid_cooperative":
            unsupported.append("rid_figure_only_service_consumer")
        if row.get("family") == "S:radar_noncooperative":
            unsupported.append("radar_new_action_not_supported")
        if row.get("family") == "N:navigation_integrity_monitoring":
            unsupported.append("endpoint_new_action_not_supported")
    if radar_diagnostic == "diagnostic_only_not_consumed":
        unsupported.append("radar_los_diagnostic_only")
    unsupported = sorted(set(unsupported))
    if unsupported:
        effects.append(dict(effect="unsupported_dimensions", dimensions=unsupported))
    fixture_out = None
    if fixture is not None:
        fixture_out = dict(fixture_id=fixture.get("fixture_id"),
            fixture_version=fixture.get("fixture_version"),
            fixture_sha256=fixture.get("fixture_sha256"),
            evidence_track=fixture.get("evidence_track"),
            scope_id=fixture["manifest"].get("scope_id"))
    result = dict(schema_version=OUTPUT_SCHEMA, evidence_track=TRACK, status=status,
        execution_enabled=False, persisted_to_project_state=False,
        new_site_stage_unlocked=False, final_eligible=False,
        fixture_only=native is not None, fixture=fixture_out,
        model_call_status=("invoked_fixture_only" if native is not None
            else "refused_non_fixture_inputs" if fixture is None and not blockers
            else "not_invoked_blocked"),
        scenario=dict(scenario_id=route.get("scenario_id"), version=route.get("version"),
            scenario_fingerprint=scenario.get("scenario_fingerprint"),
            source_bundle_fingerprint=scenario.get("source_bundle_fingerprint"),
            computational_dependency_fingerprint=scenario.get(
                "computational_dependency_fingerprint"), evidence_track=TRACK),
        scenario_fingerprint=scenario.get("scenario_fingerprint"),
        domain_fingerprint=domain.get("domain_fingerprint"),
        source_bundle_fingerprint=scenario.get("source_bundle_fingerprint"),
        computational_dependency_fingerprint=scenario.get(
            "computational_dependency_fingerprint"),
        action_set_fingerprint=digest(sorted(rows, key=lambda row: str(
            row.get("action_id")))),
        native_action_set_fingerprint=(fixture_context or {}).get("manifest", {}).get(
            "action_set_fingerprint"),
        consumer_identities=consumer_identities,
        model_inputs=model_input,
        action_results=deepcopy(results),
        applied_action_ids=applied_ids,
        missing_action_ids=[row["action_id"] for row in results if not row["applied"]],
        native_model_results=native_results,
        native_parent_chain=native_chain,
        native_input_fingerprints=native_fingerprints,
        native_m2_result=deepcopy(native_m2),
        native_m2_result_fingerprint=digest(native_m2) if native_m2 is not None else None,
        m2_status=dict(status="consumed_via_m4_fixed_chain" if m2_chain is not None
                else "not_invoked",
            navigation_status=(m2_chain or {}).get("navigation_status"),
            rtk_status=(m2_chain or {}).get("rtk_status"),
            unknowns=deepcopy((m2_chain or {}).get("unknowns", [])),
            input_fingerprint=(m2_chain or {}).get("input_fingerprint")),
        m3_status=m3_status,
        conditional_effects=effects,
        scoped_model_status=scoped_status,
        complete_required_cns_status=complete_status,
        navigation_status=navigation_status,
        navigation_evidence_gaps=navigation_gaps,
        unsupported_dimensions=unsupported,
        evidence_status=("fixture_only" if status == "evaluated_fixture_only"
            else "evidence_incomplete"),
        independent_verification_status="not_verified",
        real_domain_proof_status="not_verified",
        source_classification=sources,
        assumption_classification=assumptions,
        source_authentication_status="not_authenticated",
        source_gaps=source_gaps,
        assumption_gaps=assumption_gaps,
        evidence_gaps=sorted(set(str(gap) for gap in gaps)),
        required_inventory=inventory,
        included_constraint_ids=included,
        excluded_constraints=excluded,
        unconsumed_requirements=unconsumed,
        consumer_mapping=consumer_mapping,
        real_qualification_snapshot=deepcopy(domain["real_qualification_snapshot"]),
        radar_diagnostic_status=radar_diagnostic,
        blockers=blockers)
    return seal(result, "evaluation_fingerprint")


def _real_admission(scenario, domain, evidence, native_m2):
    """Run the P2-A1 static admission with its own declared consumer snapshot.

    The evaluation registry is a *capability claim* for the evaluator branch;
    the admission partition is frozen against the scenario's declared consumer
    registry, so required constraints cannot disappear merely because the real
    evaluator binding is unavailable.
    """
    declared = scenario["route_scenario"].get("consumer_registry") or []
    if declared:
        capabilities = dict(
            consumers={row.get("consumer_id"): row.get("method_version")
                for row in declared if type(row) is dict},
            supported_action_families=sorted({family for row in declared
                if type(row) is dict for family in row.get("supported_action_families", [])}),
            note="frozen scenario consumer registry; static mapping only")
    else:
        capabilities = dict(consumers={M4_CONSUMER: M4_METHOD_VERSION},
            supported_action_families=["C:communication"],
            note="evaluation registry fallback; no scenario consumer registry")
    intent = dict(mode="inspect", native_m2_result=deepcopy(native_m2),
        output_root=None)
    return inspect_research_admission(scenario=scenario, conditional_domain=domain,
        route_evidence=evidence, consumer_capabilities=capabilities, run_intent=intent)


def _real_action_results(rows, domain):
    """Classify real actions without requiring a synthetic slot or fixture."""
    parent_ids = {row.get("physical_site_id") for row in
        domain["real_qualification_snapshot"].get("site_ledger", [])
        if type(row) is dict}
    frozen = {row.get("action_id"): row for row in domain.get("finite_actions", [])
        if type(row) is dict}
    results, errors, seen = [], [], set()
    frozen_ids = set(frozen)
    missing = sorted(str(item) for item in frozen_ids - {row.get("action_id") for row in rows})
    if missing:
        errors.append("frozen_action_set_incomplete:" + ",".join(missing))
    for row in rows:
        action_id = row.get("action_id")
        family = row.get("service_key")
        planner = row.get("planner_family")
        reasons = []
        if type(action_id) is not str or not action_id:
            reasons.append("action_id_required")
        if type(family) is not str or not family:
            reasons.append("action_family_required")
        for field in ("planner_family", "physical_site_id", "device_id",
                      "device_instance_id", "source_class"):
            if type(row.get(field)) is not str or not row.get(field):
                reasons.append("action_field_missing:" + field)
        if "slot_id" not in row:
            reasons.append("slot_identity_not_recorded")
        for field in ("dependency_action_ids", "applicable_models", "assumption_refs"):
            value = row.get(field)
            if type(value) is not list or any(type(item) is not str for item in value):
                reasons.append("action_field_missing:" + field)
        if row.get("source_class") not in ("A", "B", "C", "D"):
            reasons.append("action_source_class_invalid")
        if action_id in seen:
            reasons.append("duplicate_action_id")
        seen.add(action_id)
        if action_id not in frozen or frozen.get(action_id) != row:
            reasons.append("action_outside_frozen_x:" + str(action_id))
        if row.get("physical_site_id") not in parent_ids:
            reasons.append("action_site_not_in_real_parent_ledger")
        if type(row.get("dependency_action_ids")) is list:
            for dependency in row["dependency_action_ids"]:
                if dependency not in frozen_ids:
                    reasons.append("action_dependency_missing:" + str(dependency))
        code = UNSUPPORTED_FAMILY_CODES.get(family)
        if code is None and planner == "directional_radar":
            code = "unsupported_action_family:S:radar_noncooperative"
        if code is None and planner == "endpoint_integrity_monitor":
            code = "endpoint_new_action_unsupported"
        if code is not None:
            status = "unsupported"
            reasons.append(code)
        elif family == "C:communication":
            status = "blocked"
            reasons.append("real_evaluator_binding_unavailable")
        elif family in SUPPORTED_FAMILIES:
            status = "blocked"
            reasons.append("real_evaluator_binding_unavailable")
        else:
            status = "unsupported"
            reasons.append("consumer_unavailable:" + str(family))
        if row.get("dependency_evidence_status") not in (None, "declared"):
            reasons.append("action_dependency_evidence_unknown")
        if reasons:
            errors.extend(reason for reason in reasons
                if reason.startswith(("action_outside", "action_site_not",
                                      "duplicate_action_id", "frozen_action")))
        results.append(dict(
            action_id=action_id,
            family=family,
            planner_family=planner,
            status=status,
            physical_site_id=row.get("physical_site_id"),
            device_model_id=row.get("device_id"),
            device_instance_id=row.get("device_instance_id"),
            slot_id=row.get("slot_id"),
            route_ids=deepcopy(row.get("route_ids", [])),
            dependency_action_ids=deepcopy(row.get("dependency_action_ids", [])),
            dependency_evidence_status=row.get("dependency_evidence_status"),
            applicable_models=deepcopy(row.get("applicable_models", [])),
            source_class=row.get("source_class"),
            assumption_refs=deepcopy(row.get("assumption_refs", [])),
            reason_codes=sorted(set(reasons)),
            applied=False,
        ))
    return results, errors


def _real_consumer_matrix(scenario):
    route = scenario["route_scenario"]
    declarations = route.get("snapshot_declarations") or {}
    declared = route.get("consumer_registry") or []
    historical = {
        "p14_corridor": declarations.get("p14", {}),
        "p15_corridor_gap": declarations.get("p15", {}),
        "p16_site_plan": declarations.get("p16", {}),
        "p17_continuous": declarations.get("p17", {}),
    }
    model_consumers = []
    for row in declared:
        if type(row) is not dict:
            continue
        cid = row.get("consumer_id")
        if cid == M4_CONSUMER:
            status = "partial_real_binding_unavailable"
            reason = "real_evaluator_binding_unavailable"
        elif cid in historical:
            snapshot = historical[cid]
            status = {"failed": "historical_failed_not_reusable",
                      "proposal_ready": "proposal_ready_planning_only",
                      "stale": "stale_not_current"}.get(
                          snapshot.get("status"), "unknown_not_current")
            reason = snapshot.get("reason")
        elif cid == "figure_only_rid":
            status = "unsupported_figure_only"
            reason = "rid_figure_only_not_a_service_consumer"
        elif cid == "m2_rtk":
            status = "not_invoked"
            reason = "m2_used_only_via_m4_fixed_chain"
        else:
            status = "declared_not_evaluated"
            reason = row.get("input_applicability")
        model_consumers.append(dict(
            consumer_id=cid,
            method_ref=row.get("method_ref"),
            method_version=row.get("method_version"),
            supported_action_families=deepcopy(row.get("supported_action_families", [])),
            status=status,
            reason=reason,
            historical_snapshot_status=(historical.get(cid, {}) or {}).get("status"),
            historical_status_is_not_current_evaluation=True if cid in historical else False,
        ))
    service_consumers = {
        "C:communication": dict(status="partial_supported_real_binding_unavailable",
            consumers=["m4_joint", "p14_corridor", "p15_corridor_gap",
                       "p16_site_plan", "p17_continuous"],
            reason="real_evaluator_binding_unavailable"),
        "S:rid_cooperative": dict(status="unsupported_figure_only",
            consumers=["figure_only_rid"],
            reason="rid_figure_only_not_a_service_consumer"),
        "N:navigation_integrity_monitoring": dict(
            status="unsupported_endpoint_new_action",
            consumers=["p16_site_plan"],
            reason="endpoint_new_action_unsupported"),
        "S:radar_noncooperative": dict(status="disabled_unsupported",
            consumers=[], reason="radar_disabled_unsupported"),
    }
    return dict(model_consumers=model_consumers, service_consumers=service_consumers)


def _real_required_partition(scenario, admission):
    route = scenario["route_scenario"]
    inventory = deepcopy(admission.get("required_inventory", []))
    included = deepcopy(admission.get("included_constraint_ids", []))
    excluded = deepcopy(admission.get("excluded_constraints", []))
    unconsumed = deepcopy(admission.get("unconsumed_requirements", []))
    unconsumed_ids = [row.get("constraint_id") for row in unconsumed
                      if type(row) is dict]
    c1000 = [row.get("constraint_id") for row in inventory
             if type(row) is dict and row.get("service_key") == "C:communication"
             and row.get("max_gap_m") == 1000]
    c45 = [row.get("constraint_id") for row in inventory
           if type(row) is dict and row.get("service_key") == "C:communication"
           and row.get("max_gap_m") == 45]
    return dict(
        inventory_count=len(inventory),
        included_count=len(included),
        excluded_count=len(excluded),
        unconsumed_count=len(unconsumed),
        partition_complete=(len(inventory) == len(included) + len(excluded)
                            and not (set(included) & {row.get("constraint_id")
                                 for row in excluded if type(row) is dict})),
        inventory=inventory,
        included_constraint_ids=included,
        excluded_constraints=excluded,
        unconsumed_requirements=unconsumed,
        unconsumed_constraint_ids=unconsumed_ids,
        c1000m=dict(constraint_ids=c1000,
            status="excluded_and_unconsumed",
            independent_of_45m=True),
        c45m=dict(constraint_ids=c45,
            status=("included_but_consumer_partial"
                    if c45 else "not_present")),
        required_consumer_missing_ids=unconsumed_ids,
    )


def _real_branch_identification(*, scenario, domain, preflight, partition,
                                envelope, envelope_fingerprint=None):
    route = scenario["route_scenario"]
    declarations = route.get("snapshot_declarations") or {}
    qualification = domain.get("conditional_qualification") or []
    qualified = [row for row in qualification
                 if row.get("status") == "conditional_qualified_under_policy"]
    activation_policy = envelope.get("activation_policy") or {}
    installed_status = envelope.get("installed_inventory_status")
    installed_instances = envelope.get("installed_device_instances")
    model_versions = route.get("model_versions") or {}
    conflicts = []
    declared_versions = {
        row.get("consumer_id"): row.get("method_version")
        for row in route.get("consumer_registry", []) if type(row) is dict
    }
    key_alias = {"p14_corridor": "p14", "p15_corridor_gap": "p15",
                 "p16_site_plan": "p16", "p17_continuous": "p17",
                 "m2_rtk": "m2_rtk", "m4_joint": "m4_joint"}
    for cid, version in declared_versions.items():
        expected = model_versions.get(key_alias.get(cid, cid))
        if expected is not None and version != expected:
            conflicts.append(dict(consumer_id=cid, declared=version,
                                  frozen=expected))
    return dict(
        legal_source_not_executed=dict(
            status="true",
            detail="frozen source identity verified; execution_authorization=not_authorized",
            preflight_status=preflight.get("status"),
            scenario_fingerprint=scenario["scenario_fingerprint"]),
        supported_consumer_present=dict(
            status="partial",
            consumers=["m4_joint", "p14_corridor", "p15_corridor_gap",
                       "p16_site_plan", "p17_continuous"],
            reason="real_evaluator_binding_unavailable"),
        unsupported_service_present=dict(
            status="true",
            services=["S:rid_cooperative", "N:navigation_integrity_monitoring",
                      "S:radar_noncooperative"],
            reasons=["rid_figure_only_not_a_service_consumer",
                     "endpoint_new_action_unsupported",
                     "radar_disabled_unsupported"]),
        required_consumer_missing=dict(
            status="true" if partition["unconsumed_constraint_ids"] else "false",
            constraint_ids=deepcopy(partition["unconsumed_constraint_ids"])),
        activation_qualification_baseline_missing=dict(
            status="true",
            qualified_sites=len(qualified),
            unknown_sites=len(qualification) - len(qualified),
            activation_baseline=activation_policy.get("status"),
            installed_inventory_status=installed_status,
            installed_device_instances=installed_instances,
            reason="no_real_activation_or_installation_evidence"),
        model_input_version_or_fingerprint_conflict=dict(
            status="detected" if conflicts else "not_detected",
            conflicts=conflicts,
            scenario_fingerprint=scenario["scenario_fingerprint"],
            domain_fingerprint=domain["domain_fingerprint"],
            envelope_fingerprint=envelope_fingerprint),
        evaluated_real=dict(
            status="false",
            reason="real_evaluator_binding_unavailable",
            model_call_status="not_invoked"),
    )


def evaluate_real_conditional_actions(*, frozen_inputs, actions,
                                      consumer_registry) -> dict:
    """Evaluate the real R0005 conditional domain without inventing results.

    This is the explicit fixture-free branch.  It validates the solver envelope
    and the real evaluation preconditions, then returns an evidence-scoped
    refusal.  It never calls M4/M3/M2 for real inputs and never returns
    ``evaluated_real``; the existing fixture branch below is untouched.
    """
    checked = _checked_frozen(frozen_inputs)
    if checked["fixture"] is not None:
        raise ValueError("real evaluation branch requires fixture-free inputs")
    scenario, domain, route = checked["scenario"], checked["domain"], checked["route"]
    native_m2 = checked["native_m2"]
    consumers = _consumers(consumer_registry)
    rows = bounded_json(actions)
    if type(rows) is not list or not rows or any(type(row) is not dict for row in rows):
        raise ValueError("non-empty action object list required")
    errors = list(scenario.get("blockers", [])) + list(domain.get("blockers", []))
    errors.extend(_consumer_errors(consumers, scenario, domain, route))
    envelope = build_real_solver_envelope(scenario=scenario,
        conditional_domain=domain)
    verify_real_solver_envelope(envelope, scenario=scenario,
        conditional_domain=domain)
    preflight = preflight_real_domain(real_domain_envelope=envelope)
    errors.extend(preflight.get("contract_blockers", []))
    admission = _real_admission(scenario, domain, checked["evidence"], native_m2)
    errors.extend(admission.get("blockers", []))
    results, action_errors = _real_action_results(rows, domain)
    errors.extend(action_errors)
    sources, source_gaps, assumptions, assumption_gaps, evidence_errors = \
        _evidence_classification(scenario, consumers, None)
    errors.extend(evidence_errors)
    partition = _real_required_partition(scenario, admission)
    if not partition["partition_complete"]:
        errors.append("real_required_partition_incomplete")
    if partition["c1000m"]["constraint_ids"] and (
            "REQ-C-GAP-1000" not in partition["unconsumed_constraint_ids"]):
        errors.append("real_c1000m_cannot_be_cleared_by_45m")
    blockers = set(str(item) for item in preflight.get("execution_blockers", []))
    blockers.update(str(item) for item in admission.get("execution_blockers", []))
    blockers.add("real_evaluator_binding_unavailable")
    blockers.update(str(item) for item in errors)
    contract_blockers = sorted(set(str(item) for item in errors))
    status = "blocked" if contract_blockers else "evaluation_blocked"
    navigation_gaps = ["m2_result_missing"]
    unsupported = ["real_evaluator_binding_unavailable",
                   "rid_figure_only_service_consumer",
                   "endpoint_new_action_not_supported",
                   "radar_disabled_unsupported"]
    branch = _real_branch_identification(scenario=scenario, domain=domain,
        preflight=preflight, partition=partition, envelope=envelope,
        envelope_fingerprint=envelope.get("envelope_fingerprint"))
    effects = [
        dict(effect="real_domain_evaluation_blocked",
             reasons=sorted(set(blockers))),
        dict(effect="navigation_unknown", reasons=navigation_gaps),
        dict(effect="complete_required_cns_not_claimed", status="unknown"),
        dict(effect="real_evaluator_binding_unavailable"),
    ]
    consumer_matrix = _real_consumer_matrix(scenario)
    return seal(dict(
        schema_version=REAL_OUTPUT_SCHEMA,
        evidence_track=TRACK,
        evaluation_branch="real_domain",
        status=status,
        evaluation_status="not_evaluated",
        execution_enabled=False,
        execution_authorization="not_authorized",
        persisted_to_project_state=False,
        new_site_stage_unlocked=False,
        final_eligible=False,
        model_feasible=None,
        optimal_proven=False,
        fixture_only=False,
        fixture=None,
        model_call_status="not_invoked_real_domain_binding_unavailable",
        scenario=dict(
            scenario_id=route.get("scenario_id"),
            version=route.get("version"),
            scenario_fingerprint=scenario.get("scenario_fingerprint"),
            source_bundle_fingerprint=scenario.get("source_bundle_fingerprint"),
            computational_dependency_fingerprint=scenario.get(
                "computational_dependency_fingerprint"),
            evidence_track=TRACK),
        scenario_fingerprint=scenario.get("scenario_fingerprint"),
        domain_fingerprint=domain.get("domain_fingerprint"),
        source_bundle_fingerprint=scenario.get("source_bundle_fingerprint"),
        computational_dependency_fingerprint=domain.get(
            "computational_dependency_fingerprint"),
        source_state_sha256=(route.get("baseline_ref") or {}).get("sha256"),
        action_set_fingerprint=domain.get("action_set_fingerprint"),
        envelope_fingerprint=envelope.get("envelope_fingerprint"),
        real_solver_preflight=dict(
            status=preflight.get("status"),
            contract_blockers=deepcopy(preflight.get("contract_blockers", [])),
            execution_blockers=deepcopy(preflight.get("execution_blockers", [])),
            execution_status=preflight.get("execution_status"),
            search_started=preflight.get("search_started"),
            action_count=preflight.get("action_count"),
            conditional_site_count=preflight.get("conditional_site_count"),
            parent_site_count=preflight.get("parent_site_count"),
            action_site_count=preflight.get("action_site_count"),
            final_eligible=preflight.get("final_eligible"),
            preflight_fingerprint=preflight.get("preflight_fingerprint"),
        ),
        real_domain_status=preflight.get("status"),
        branch_identification=branch,
        consumer_capability_matrix=consumer_matrix,
        consumer_identities=[
            dict(consumer_id=M4_CONSUMER, method_ref=M4_METHOD_REF,
                 method_version=M4_METHOD_VERSION, source="trusted_registry",
                 model_call_status="not_invoked_real_domain_binding_unavailable"),
            dict(consumer_id="m2_rtk", method_ref=RTK_METHOD_REF,
                 method_version=RTK_VERSION, source="m4_fixed_chain",
                 model_call_status="not_invoked"),
            dict(consumer_id="m3_binding", method_ref=M3_METHOD_REF,
                 method_version=M3_METHOD_VERSION, source="m4_dependent_chain",
                 model_call_status="not_invoked"),
        ],
        required_partition=partition,
        required_inventory=deepcopy(partition["inventory"]),
        included_constraint_ids=deepcopy(partition["included_constraint_ids"]),
        excluded_constraints=deepcopy(partition["excluded_constraints"]),
        unconsumed_requirements=deepcopy(partition["unconsumed_requirements"]),
        consumer_mapping={row.get("constraint_id"): deepcopy(row.get("consumer"))
            for row in partition["inventory"] if type(row) is dict},
        action_results=deepcopy(results),
        applied_action_ids=[],
        missing_action_ids=[row["action_id"] for row in results if not row["applied"]],
        native_model_results={},
        native_parent_chain={},
        native_input_fingerprints={},
        native_m2_result=deepcopy(native_m2),
        native_m2_result_fingerprint=digest(native_m2) if native_m2 is not None else None,
        m2_status=dict(status="not_invoked", navigation_status="unknown",
            rtk_status=None, unknowns=["m2_result_missing"],
            input_fingerprint=None),
        m3_status=dict(status="not_invoked"),
        conditional_effects=effects,
        scoped_model_status="not_evaluated",
        complete_required_cns_status="unknown",
        complete_required_proof_status="not_proven",
        navigation_status="unknown",
        navigation_evidence_gaps=navigation_gaps,
        unsupported_dimensions=sorted(set(unsupported)),
        evidence_status="evidence_incomplete",
        independent_verification_status="not_verified",
        real_domain_proof_status="not_verified",
        source_classification=sources,
        assumption_classification=assumptions,
        source_authentication_status="not_authenticated",
        source_gaps=source_gaps,
        assumption_gaps=assumption_gaps,
        evidence_gaps=sorted(set(str(gap) for gap in
            list(admission.get("evidence_gaps", [])) + source_gaps + assumption_gaps)),
        real_qualification_snapshot=deepcopy(domain["real_qualification_snapshot"]),
        activation_policy=deepcopy(envelope.get("activation_policy")),
        installed_inventory_status=envelope.get("installed_inventory_status"),
        installed_device_instances=deepcopy(envelope.get("installed_device_instances")),
        installed_device_instances_status=envelope.get(
            "installed_device_instances_status"),
        radar_diagnostic_status="disabled_unsupported_not_present",
        contract_blockers=contract_blockers,
        blockers=sorted(blockers),
    ), "evaluation_fingerprint")


__all__ = ["evaluate_conditional_actions", "evaluate_real_conditional_actions",
    "M4_METHOD_VERSION", "M4_METHOD_REF", "M3_METHOD_REF", "M3_METHOD_VERSION",
    "RTK_METHOD_REF", "INPUTS_SCHEMA", "OUTPUT_SCHEMA", "REAL_OUTPUT_SCHEMA",
    "REGISTRY_SCHEMA"]
