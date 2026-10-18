"""M5-B read-only composition of unchanged M4, M2 and M3 contracts.

Only the existing explicitly synthetic micro-model is executable in this unit.
Real runtime admission belongs to separately authorized D; missing upstream
segment evidence is reported, never patched into an evaluated result.
"""
from copy import deepcopy
from ..domain.joint_feasibility import json_value, digest, verify, seal
from ..domain.existing_site_optimization import measure_objectives, proposal_flags
from .joint_evaluation_adapter import JointModelEvaluator
from .joint_candidate_assessment import bind_joint_candidate_assessment
from .joint_candidate_domain import validate_actions
from .candidate_ue_acceptability import candidate_action_set_fingerprint
from .existing_site_action_binding import checked_review, action_family, FAMILIES, RADAR, ENDPOINT

REAL_RTK_GAPS = ["catalog_model_version", "device_instance_id", "physical_host_and_slot",
    "provider_inventory_status", "providers[].device_status", "providers[].eligibility_status",
    "providers[].baseline_distance_m", "max_reference_baseline_m", "delivery_status",
    "correction_delivery_interface_source", "handover_status", "speed_profile", "backup_scenario"]


def _segment_evidence(evaluation):
    inputs = deepcopy(evaluation.get("rtk_joint_inputs"))
    result = deepcopy(evaluation.get("rtk_joint_result"))
    missing = []
    if inputs is None:
        missing.append("rtk_joint_inputs")
    else:
        if not inputs.get("segments"): missing.append("segments")
        for i, row in enumerate(inputs.get("segments", [])):
            if row.get("provider_inventory_status") != "complete":
                missing.append("segments[%d].provider_inventory_status" % i)
            for key in ("handover_status", "delivery_status", "delivery_service_key", "source_fingerprint"):
                if row.get(key) in (None, "", "unknown", "not_verified", "not_evaluated"):
                    missing.append("segments[%d].%s" % (i, key))
            if not row.get("providers"): missing.append("segments[%d].providers" % i)
            for j, provider in enumerate(row.get("providers", [])):
                for key in ("device_status", "eligibility_status", "baseline_distance_m", "distinct_site_id"):
                    if provider.get(key) in (None, "", "unknown", "not_verified", "not_evaluated"):
                        missing.append("segments[%d].providers[%d].%s" % (i, j, key))
        if not inputs.get("speed_profile"): missing.append("speed_profile")
        if not inputs.get("backup_scenario"): missing.append("backup_scenario")
    return dict(inputs=inputs, result=result, missing_fields=missing,
        navigation_status="unknown" if missing else (result or {}).get("navigation_status", "unknown"),
        unknowns=deepcopy((result or {}).get("unknowns", [])),
        evidence_semantics="original_m4_m2_results_no_sidecar_promotion")


class ExistingSiteEvaluator:
    def __init__(self, *, frozen_context, frozen_domain, domain_review, objective_policy, evidence_registry):
        self.context, self.domain, self.policy, self.registry = json_value(
            [frozen_context, frozen_domain, objective_policy, evidence_registry])
        self.review, reviewed = checked_review(domain_review)
        verify(self.context, "context_fingerprint")
        verify(self.context["assessment_scope"], "scope_fingerprint")
        if reviewed != self.domain or self.context["frozen_domain"] != self.domain:
            raise ValueError("domain review/context identity mismatch")
        if (self.context["evidence_track"] != self.domain["evidence_track"] or
                self.context["scope_fingerprint"] != self.context["assessment_scope"]["scope_fingerprint"]):
            raise ValueError("context scope/track identity mismatch")
        self.model = JointModelEvaluator(frozen_context=self.context, frozen_domain=self.domain)

    def _identity(self, actions, evaluation):
        snapshot = self.context["frozen_rule_snapshot"]
        scenario = snapshot.get("frozen_inputs", {}).get("operation_scenario", {})
        state = self.context["baseline_inputs"].get("model_state", {})
        return dict(context_fingerprint=self.context["context_fingerprint"],
            domain_fingerprint=self.domain["domain_fingerprint"],scope_fingerprint=self.context["scope_fingerprint"],
            route_id=snapshot.get("route_id"),operating_layer_id=scenario.get("operating_layer_id"),
            route_geometry_fingerprint=digest(state.get("operational_routes", [])),
            source_registry_fingerprint=digest(self.registry),source_manifest_fingerprint=digest(self.review.get("source_manifest")),
            domain_review_fingerprint=digest(self.review),objective_policy_fingerprint=digest(self.policy),
            action_set_fingerprint=candidate_action_set_fingerprint(actions=actions),
            native_parent_fingerprint=digest(evaluation.get("native_materials", {})))

    def _unavailable(self, actions, status, reasons):
        return seal(dict(schema_version="existing_site_evaluation.v1",actions=actions,
            action_set_fingerprint=candidate_action_set_fingerprint(actions=actions),
            context_fingerprint=self.context["context_fingerprint"],evidence_track=self.context["evidence_track"],
            evaluation_status=status,scoped_model_status="unknown",complete_required_cns_status="unknown",
            reason_codes=reasons,applied_action_ids=[],binding_identity=self._identity(actions, {}),
            input_status="blocked",identity_errors=[],objective_measurement=None,candidate_assessment=None,m3_assessment=None,
            final_research_admission=dict(eligible=False,status="blocked",blockers=reasons),
            independent_verification_status="not_verified",real_domain_proof_status="not_verified",
            rtk_segment_evidence=dict(inputs=None,result=None,missing_fields=list(REAL_RTK_GAPS)),
            unconsumed_requirements=deepcopy(self.context["frozen_rule_snapshot"].get("unconsumed_requirements", [])),
            frozen_context=deepcopy(self.context),domain_review=deepcopy(self.review),**proposal_flags()),"evaluation_fp")

    def evaluate_action_set(self, *, actions):
        actions = validate_actions(actions=actions, frozen_domain=self.domain)
        unsupported = [a["action_id"] for a in actions if action_family(a) not in FAMILIES or action_family(a) in (RADAR, ENDPOINT)]
        if unsupported:
            return self._unavailable(actions, "unsupported_constraint_consumer",
                ["unsupported_action_family:" + aid for aid in unsupported])
        if self.context["evidence_track"] != "synthetic_fixture":
            return self._unavailable(actions, "readonly_evaluation_unavailable",
                ["real_track_requires_separate_run_authorization", "real_rtk_segment_contract_unavailable"])
        evaluation = json_value(self.model.evaluate(actions=actions))
        if evaluation.get("evaluation_status") != "evaluated":
            out = self._unavailable(actions, "readonly_evaluation_unavailable", evaluation.get("reason_codes", []))
            out["native_evaluation"] = evaluation
            return seal(out, "evaluation_fp")
        assessment = bind_joint_candidate_assessment(model_evaluation=evaluation, frozen_context=self.context,
            actions=actions,evidence_registry=self.registry)
        errors = list(assessment.get("identity_errors", []))
        expected = [a["action_id"] for a in actions]
        if evaluation.get("applied_action_ids") != expected: errors.append("applied_action_set_mismatch")
        p17 = evaluation.get("native_materials", {}).get("p17", {}).get("evaluation_inputs", {})
        if p17.get("post_plan_projection", {}).get("applied_action_ids") != expected:
            errors.append("p17_applied_action_set_mismatch")
        identity = self._identity(actions, evaluation)
        for route in evaluation.get("native_results", {}).get("post_plan", {}).get("p14", {}).get("routes", []):
            if route.get("route_id") == identity["route_id"]:
                if any(v.get("altitude_layer_id") != identity["operating_layer_id"] for v in route.get("voxels", [])):
                    errors.append("operating_layer_mismatch")
        route_ids = [r.get("route_id") for r in evaluation["native_results"]["post_plan"]["p14"].get("routes", [])]
        if identity["route_id"] not in route_ids: errors.append("route_identity_mismatch")
        measurement = measure_objectives(actions=actions,frozen_domain=self.domain,objective_policy=self.policy,
            activation_predictions={},participation_evidence={})
        out = deepcopy(evaluation)
        out.update(schema_version="existing_site_evaluation.v1",candidate_assessment=assessment,
            m3_assessment=assessment["m3_assessment"],final_research_admission=assessment["final_research_admission"],
            objective_measurement=measurement,binding_identity=identity,
            frozen_context=deepcopy(self.context),domain_review=deepcopy(self.review),
            rtk_segment_evidence=_segment_evidence(evaluation),
            input_status="input_inconsistent" if errors else assessment["input_status"],
            identity_errors=sorted(set(errors)),scoped_model_status="input_inconsistent" if errors else assessment["scoped_model_status"],
            real_domain_proof_status="not_verified",**proposal_flags())
        included = set(self.context["assessment_scope"]["included_constraint_ids"])
        needs_m2 = any(r["constraint_id"] in included and r.get("consumer", {}).get("kind") == "m2_navigation"
                       for r in self.context["assessment_scope"]["required_constraint_inventory"])
        if not errors and needs_m2 and out["rtk_segment_evidence"]["navigation_status"] == "unknown":
            out["scoped_model_status"] = "unknown"
        out["binding_blockers"] = []
        if measurement["input_status"] == "combination_conflict":
            out["binding_blockers"].append("combination_constraints_violated")
            if not errors:
                out.update(input_status="combination_conflict",scoped_model_status="unacceptable")
        return seal(out, "evaluation_fp")
