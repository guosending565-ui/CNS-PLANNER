"""M4 M3 evidence adapter; declarations never authenticate execution."""
from copy import deepcopy
from ..domain.joint_feasibility import json_value, digest, verify
from .candidate_ue_acceptability import (candidate_action_set_fingerprint, evaluate_candidate_ue_acceptability,
    ALGORITHMS,SCOPE_KEYS)
from .joint_candidate_domain import validate_actions


def bind_joint_candidate_assessment(*,model_evaluation,frozen_context,actions,evidence_registry,research_review_registry=None):
    context=json_value(frozen_context);evaluation=json_value(model_evaluation);registry=json_value(evidence_registry)
    verify(context,"context_fingerprint");actions=validate_actions(actions=actions,frozen_domain=context["frozen_domain"])
    fp=candidate_action_set_fingerprint(actions=actions);snapshot=context["frozen_rule_snapshot"]
    mismatch=[]
    if evaluation.get("context_fingerprint")!=context["context_fingerprint"]:mismatch.append("context_fingerprint_mismatch")
    if evaluation.get("action_set_fingerprint")!=fp or evaluation.get("actions")!=actions:mismatch.append("action_set_mismatch")
    scenario=snapshot.get("frozen_inputs",{}).get("operation_scenario",{})
    native=evaluation.get("native_results",{});post=native.get("post_plan",{})
    layer=next((v.get("altitude_layer_id") for r in post.get("p14",{}).get("routes",[]) for v in r.get("voxels",[])),None)
    scope={k:deepcopy(snapshot.get(k) if k in SCOPE_KEYS[:3] else scenario.get(k)) for k in SCOPE_KEYS}
    scope["operating_layer_id"]=scenario.get("operating_layer_id") or layer
    deps=dict(device_catalog_fingerprint=digest(context["frozen_domain"]["device_catalog"]),
        candidate_domain_fingerprint=context["domain_fingerprint"],applicable_equipment_fingerprint=digest(actions),
        source_registry_fingerprint=digest(registry),evidence_policy_version=str(context["frozen_domain"]["qualification_policy"].get("version","unverified-policy")))
    mctx=dict(schema_version="candidate_ue_context.v1",**scope,m1_rule_fingerprint=snapshot.get("input_fingerprint"),
        expected_action_set_fingerprint=fp,frozen_dependencies=deps,
        route_geometry_fingerprint=digest(context["baseline_inputs"].get("model_state",{}).get("operational_routes",[])),
        evidence_track=context["evidence_track"],expected_algorithms=dict(m1_schema_version="frozen_ue_rules.v1",
            **{m:dict(algorithm_id=a,algorithm_version=v) for m,(a,v) in ALGORITHMS.items()}))
    token=digest([context["context_fingerprint"],fp])[:16]
    def eid(stage,model):return token+":"+stage+":"+model
    def envelope(stage,model,payload,binding):
        parents=[]
        if model=="p15":parents=[eid(stage,"p14")]
        if model=="p17":parents=[eid("baseline","p14"),eid("post_plan","p14"),eid("post_plan","p15")]
        return dict(schema_version="candidate_evidence_envelope.v1",evidence_id=eid(stage,model),
            role="legacy_p17" if model=="p17" else stage,execution_role="quick_projection",
            producer=dict(algorithm_id=ALGORITHMS[model][0],algorithm_version=ALGORITHMS[model][1],adapter_version="m4-v1"),
            scope=scope,plan_stage=stage,action_set_fingerprint=None if stage=="baseline" else fp,
            baseline_evidence_id=None if stage=="baseline" else eid("baseline","p14" if model=="p17" else model),
            m1_rule_fingerprint=snapshot.get("input_fingerprint"),native_input_fingerprint=payload.get("input_fingerprint"),
            payload_digest=digest(payload),parent_evidence_ids=parents,run_id=token+":quick",
            provenance_record_id="unverified-declaration:"+token,payload=deepcopy(payload),input_binding=deepcopy(binding))
    materials=evaluation.get("native_materials",{})
    baseline={m:envelope("baseline",m,p,materials.get("baseline",{}).get(m,{})) for m,p in native.get("baseline",{}).items() if m in ("p14","p15")}
    postenv={m:envelope("post_plan",m,p,materials.get(m,{})) for m,p in post.items() if m in ("p14","p15")}
    p17=envelope("post_plan","p17",native["p17"],materials.get("p17",{})) if native.get("p17") else None
    rtk=deepcopy(evaluation.get("rtk_joint_inputs"))
    if rtk:
        rtk.update(schema_version="candidate_rtk_joint_inputs.v1",segment_binding=dict(adapter_id="rtk_p15_voxels_v1",adapter_version="1",
            parent_p14_evidence_id=eid("post_plan","p14"),parent_p15_evidence_id=eid("post_plan","p15"),
            source_fingerprint=digest(post.get("p15",{})),segments_digest=digest(rtk["segments"])))
    declared=context["assessment_scope"]
    mscope=dict(schema_version="candidate_ue_scope.v1",scope_id=declared["scope_id"],version=declared["version"],source_ref=declared["source_ref"],
        kind="confirmed_engineering_model" if declared["kind"]=="declared_engineering_subset" else "complete_required_cns",
        included_constraint_ids=deepcopy(declared["included_constraint_ids"]),excluded_constraints=deepcopy(declared["excluded_constraints"]))
    m3=evaluate_candidate_ue_acceptability(frozen_rule_snapshot=snapshot,candidate_context=mctx,actions=actions,
        baseline_evidence=baseline or None,post_plan_evidence=postenv or None,legacy_p17_evidence=p17,
        rtk_joint_inputs=rtk,evidence_registry=registry,research_review_registry=research_review_registry,assessment_scope=mscope)
    mismatch.extend(r["reason_code"] for r in m3["identity_checks"]["errors"])
    vector=deepcopy(evaluation.get("constraint_vector",[]))
    blockers=[r["constraint_id"] for r in vector if r.get("status") not in ("satisfied","not_applicable")]
    blockers.extend(str(r.get("field",r)) for r in snapshot.get("unconsumed_requirements",[]))
    blockers.extend(r["constraint_id"] for r in declared["excluded_constraints"])
    blockers.extend(("required_inventory_not_authenticated","independent_execution_not_verified"))
    violation=any(r.get("status")=="unacceptable" for r in vector) or bool(m3.get("violations"))
    return dict(schema_version="joint_candidate_assessment.v1",actions=actions,action_set_fingerprint=fp,
        context_fingerprint=context["context_fingerprint"],constraint_scope=deepcopy(declared),constraint_vector=vector,
        scoped_model_status="input_inconsistent" if mismatch else evaluation.get("scoped_model_status","unknown"),
        complete_required_cns_status="unacceptable" if violation else "unknown",full_scope_blockers=sorted(set(blockers)),
        input_status="input_inconsistent" if mismatch else "consistent",identity_errors=sorted(set(mismatch)),
        evidence_status="evidence_incomplete",independent_verification_status="not_verified",formal_ue_status="unresolved",
        m3_assessment=m3,prefilter_status=m3["prefilter_status"],research_admission=deepcopy(m3["research_admission"]),
        final_research_admission=deepcopy(m3["final_research_admission"]),m3_arguments=dict(candidate_context=mctx,
            baseline_evidence=baseline,post_plan_evidence=postenv,legacy_p17_evidence=p17,rtk_joint_inputs=rtk,assessment_scope=mscope),
        evidence_track=context["evidence_track"],maturity="proposal",persisted_to_project_state=False,new_site_stage_unlocked=False)


def assemble_joint_search_result(*,search_result,candidate_assessments,frozen_context):
    result=json_value(search_result);assessments=json_value(candidate_assessments)
    verify(frozen_context,"context_fingerprint")
    if any(a["context_fingerprint"]!=frozen_context["context_fingerprint"] for a in assessments):raise ValueError("assessment context mismatch")
    invalid=[a for a in assessments if a.get("input_status")!="consistent"]
    accepted={a["action_set_fingerprint"] for a in assessments if a.get("input_status")=="consistent" and a.get("scoped_model_status")=="model_feasible"}
    raw=result.get("feasible_combinations",[])
    retained=[s for s in raw if s.get("action_set_fingerprint") in accepted]
    result.update(input_status="input_inconsistent" if invalid else "consistent",
        feasible_combinations=[] if invalid else retained,
        rejected_or_unverified_combinations=raw if invalid else [s for s in raw if s not in retained],
        model_status="undetermined" if invalid or not accepted else "model_feasible",
        complete_required_cns_status="unknown",formal_ue_status="unresolved")
    if invalid:result.update(coverage_complete=False,search_status="search_incomplete",termination_reason="input_inconsistent")
    result.update(candidate_assessments=assessments,constraint_scope=deepcopy(frozen_context["assessment_scope"]),
        context_fingerprint=frozen_context["context_fingerprint"],evidence_status="evidence_incomplete",
        independent_verification_status="not_verified",final_research_admission=dict(eligible=False,status="blocked",
        blockers=["independent_execution_proof_unavailable"]),maturity="proposal",persisted_to_project_state=False,new_site_stage_unlocked=False)
    return result
