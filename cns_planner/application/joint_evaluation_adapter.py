"""M4 isolated, fixed-factory joint models. No session or artifact dependency."""
from copy import deepcopy
from ..domain.joint_feasibility import json_value, digest, verify
from ..domain.cns_corridor import fixed_corridor_width_contract
from ..domain.surface_classification import surface_class_provider_for, surface_facts_fingerprint_for
from ..domain.navigation_augmentation import build_navigation_service_evidence
from ..domain.radar_service_evidence import build_radar_service_evidence
from ..algorithms.corridor.v1 import CNSServiceCorridorV1
from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
from .planning_evidence_service import aircraft_profile_with_evidence, device_catalog_with_evidence
from .plan_projection import project_facilities, split_plan_actions
from .navigation_reference_station_planning_service import hypothetical_navigation_sites
from .corridor_service import _complexity_preflight, _admission_rejection
from .rtk_joint_acceptance import adapt_p14_p15_rtk_segments, evaluate_p17_rtk_joint
from .candidate_ue_acceptability import candidate_action_set_fingerprint, _extract_service_events, _service_status, _validate_m1
from .joint_candidate_domain import validate_actions, count_joint_plan_sites


class JointModelEvaluator:
    def __init__(self, *, frozen_context, frozen_domain):
        self.context=json_value(frozen_context);self.domain=json_value(frozen_domain)
        verify(self.context,"context_fingerprint");verify(self.domain,"domain_fingerprint")
        if self.context["domain_fingerprint"]!=self.domain["domain_fingerprint"]:raise ValueError("domain identity mismatch")
        expected={"p14":CNSServiceCorridorV1.algorithm_version,"p15":CNSCorridorGapAnalyzerV1.algorithm_version,
            "p17":ContinuousServiceAcceptabilityV1.algorithm_version,"m2":"rtk_joint_acceptance.v1"}
        if self.context["evaluator_versions"]!=expected:raise ValueError("unsupported evaluator version")
        self.state=json_value(self.context["baseline_inputs"].get("model_state",{}))
        snapshot=self.context["frozen_rule_snapshot"]
        if _validate_m1(snapshot)["errors"]:raise ValueError("frozen rule identity inconsistent")
        scenario=snapshot.get("frozen_inputs",{}).get("operation_scenario",{})
        declared_layer=scenario.get("operating_layer_id")
        layers={r.get("altitude_layer_id") for r in self.state.get("spatial_3d",{}).get("altitude_layers",[])}
        if declared_layer and declared_layer not in layers:raise ValueError("operating layer identity inconsistent")
        # Only value snapshots enter here. There is no session/store/prototype argument.
        self._baseline=None
        self.replay_count=0

    def _compute(self, actions):
        state=deepcopy(self.state); facilities=project_facilities(state.get("existing_cns_facilities",{}),actions)
        ordinary,radar,nav=split_plan_actions(actions)
        if radar or any(a.get("planner_family")=="endpoint_integrity_monitor" for a in actions):
            raise ValueError("readonly_evaluation_unavailable: unsupported upstream action family")
        catalog=device_catalog_with_evidence(state)
        evidence=build_navigation_service_evidence(state.get("required_cns",{}),
            route_ids=[r["route_id"] for r in state.get("operational_routes",[])],
            existing_facilities=state.get("existing_cns_facilities",{}),candidate_sites=state.get("candidate_sites",{}),
            tower_colocation=state.get("tower_colocation_candidates",{}))
        if nav:
            if evidence is None:raise ValueError("readonly_evaluation_unavailable: RTK input missing")
            _,evidence=hypothetical_navigation_sites(evidence,nav)
        applied=[]
        for action in actions:
            if action in nav:
                found=any(r.get("provider_id")==action["action_id"] for r in (evidence or {}).get("providers",[]))
            else:
                found=any(dev.get("metadata",{}).get("hypothetical_action_id")==action["action_id"]
                    for site in facilities.get("items",[]) for dev in site.get("devices",[]))
            if not found:raise ValueError("readonly_evaluation_unavailable: skipped action "+action["action_id"])
            applied.append(action["action_id"])
        surface=surface_class_provider_for(state); surface_fp=surface_facts_fingerprint_for(state)
        # Fixed constructors only; JSON cannot select classes, callables or external providers.
        p14=CNSServiceCorridorV1((state.get("cns_corridor_assessment") or {}).get("parameters"))
        p15=CNSCorridorGapAnalyzerV1((state.get("cns_corridor_gap_assessment") or {}).get("parameters"))
        selection=state.get("algorithm_selection",{})
        args=dict(routes=state.get("operational_routes",[]),spatial_3d=state.get("spatial_3d",{}),
            grid=state.get("grid",{}),grid_attributes=state.get("grid_attributes",{}),
            required_cns=state.get("required_cns",{}),aircraft_profile=aircraft_profile_with_evidence(state),
            existing_facilities=facilities,device_catalog=catalog,corridor_policy=state.get("cns_corridor_policy",{}),
            coverage_parameters=(selection.get("coverage_model") or {}).get("parameters",{}),
            capability_parameters=(selection.get("service_model") or {}).get("parameters",{}),
            surface_class_provider=surface,surface_facts_fingerprint=surface_fp)
        preflight=_complexity_preflight(args)
        if _admission_rejection(preflight):raise ValueError("readonly_evaluation_unavailable: complexity admission")
        radar_evidence=build_radar_service_evidence(state.get("required_cns",{}),state.get("radar_surveillance_layout",{}),
            route_ids=[r["route_id"] for r in state.get("operational_routes",[])])
        args.update(navigation_service_evidence=evidence,radar_service_evidence=radar_evidence,preflight=preflight)
        corridor=p14.evaluate(**args)
        gap=p15.evaluate(corridor,state.get("required_cns",{}),state.get("cns_planning_objectives",{}))
        self.replay_count+=1
        material=dict(routes=args["routes"],spatial_3d=args["spatial_3d"],grid=args["grid"],
            terrain=(args["grid_attributes"].get("terrain") or {}).get("cells",{}),required_cns=args["required_cns"],
            aircraft_profile=args["aircraft_profile"] or {},existing_facilities=facilities,device_catalog=catalog,
            corridor_policy=args["corridor_policy"],parameters=p14.parameters,
            coverage_parameters=args["coverage_parameters"],capability_parameters=args["capability_parameters"],
            surface_facts_fingerprint=surface_fp,algorithm_semantics=dict(algorithm_id=p14.algorithm_id,algorithm_version=p14.algorithm_version),
            fixed_corridor_width_contract=fixed_corridor_width_contract())
        for key,value in (("navigation_service_evidence",evidence),("radar_service_evidence",radar_evidence),
                          ("navigation_endpoint_evidence",corridor.get("endpoint_service_evidence"))):
            if value is not None:material[key]=value
        gmat=dict(corridor_assessment=corridor,required_cns=args["required_cns"],planning_objectives=state.get("cns_planning_objectives",{}),
            parameters=p15.parameters,algorithm_semantics=dict(algorithm_id=p15.algorithm_id,algorithm_version=p15.algorithm_version),
            fixed_corridor_width_contract=fixed_corridor_width_contract())
        return dict(p14=corridor,p15=gap),dict(p14={"fingerprint_input":material},p15={"fingerprint_input":gmat}),applied

    def evaluate(self, *, actions):
        actions=validate_actions(actions=actions,frozen_domain=self.domain)
        action_fp=candidate_action_set_fingerprint(actions=actions)
        base=dict(actions=actions,action_set_fingerprint=action_fp,context_fingerprint=self.context["context_fingerprint"],
            evidence_track=self.context["evidence_track"],evidence_status="evidence_incomplete",independent_verification_status="not_verified",
            maturity="proposal",persisted_to_project_state=False,new_site_stage_unlocked=False)
        try:
            if self._baseline is None:self._baseline=self._compute([])
            baseline,bmat,_=deepcopy(self._baseline)
            post,material,applied=self._compute(actions) if actions else deepcopy(self._baseline)
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            return dict(base,evaluation_status="readonly_evaluation_unavailable",scoped_model_status="unknown",
                complete_required_cns_status="unknown",reason_codes=[str(exc)],applied_action_ids=[])
        state=deepcopy(self.state);snapshot=self.context["frozen_rule_snapshot"]
        scenario=snapshot.get("frozen_inputs",{}).get("operation_scenario",{})
        frozen=snapshot.get("frozen_inputs",{})
        p17_inputs=dict(corridor_assessment=baseline["p14"],corridor_gap_assessment=baseline["p15"],site_plan={"selected_actions":actions},
            aircraft_profile=deepcopy(frozen.get("aircraft_profile")),planning_evidence=deepcopy(frozen.get("planning_evidence")),
            operation_scenario=deepcopy(frozen.get("operation_scenario",scenario)),continuous_service_policy=deepcopy(frozen.get("continuous_service_policy")),
            device_catalog=device_catalog_with_evidence(state),radar_surveillance_layout=state.get("radar_surveillance_layout",{}),
            post_plan_projection=dict(available=True,corridor_assessment=post["p14"],corridor_gap_assessment=post["p15"],
                applied_action_ids=sorted(applied),projection_semantics="post_plan_projection_hypothetical_state_never_written_into_existing_cns",persisted_as_upstream=False))
        p17=ContinuousServiceAcceptabilityV1().evaluate(**p17_inputs)
        material["p17"]={"evaluation_inputs":p17_inputs}
        rtk=None;rtk_inputs=None
        route=next((r for r in post["p15"].get("routes",[]) if r.get("route_id")==snapshot.get("route_id")),None)
        if route and any(r.get("service_key")=="N:rtk_augmentation" for r in snapshot.get("rules",[])):
            segments=adapt_p14_p15_rtk_segments(route_id=route["route_id"],route_length_m=route["route_length_m"],
                assessed_voxels=route.get("voxels",[]),source_fingerprint=digest(post["p15"]))
            rtk_inputs=dict(route_length_m=route["route_length_m"],segments=segments,
                speed_profile=self.context["baseline_inputs"].get("speed_profile"),
                service_evidence_track=self.context["evidence_track"],synthetic_fixture=self.context["evidence_track"]=="synthetic_fixture")
            rtk=evaluate_p17_rtk_joint(frozen_snapshot=snapshot,**rtk_inputs)
        view=_extract_service_events(p17,snapshot)
        vector=[]
        for constraint in self.context["assessment_scope"]["required_constraint_inventory"]:
            consumer=constraint.get("consumer",{});kind=consumer.get("kind");status="unknown";key=consumer.get("service_key")
            if kind=="p14_service" and constraint.get("kind")=="engineering_geometry" and self.context["assessment_scope"]["kind"]=="declared_engineering_subset":
                entries=[e for r in post["p14"].get("routes",[]) for v in r.get("voxels",[]) for sub in v.get("subsystems",[])
                    for e in sub.get("service_redundancy",sub.get("services",[])) if e.get("service_key")==key]
                if entries:
                    statuses=[_service_status(e.get("status")) for e in entries]
                    status=("unacceptable" if "unacceptable" in statuses else
                        ("managed" if "managed" in statuses else "satisfied") if all(s in ("satisfied","managed") for s in statuses) else "unknown")
            elif kind=="p17_service":
                matches=[s for s in view["service_assessments"] if s["service_key"]==key]
                if matches:
                    statuses=[s["status"] for s in matches]
                    status=("unacceptable" if "unacceptable" in statuses else
                        ("managed" if "managed" in statuses else "satisfied") if all(s in ("satisfied","managed") for s in statuses) else "unknown")
            elif kind=="m2_navigation" and rtk:status=rtk["navigation_status"]
            if (constraint["constraint_id"] in self.context["assessment_scope"].get("mapping_unknowns",[])
                    or not constraint.get("source_ref") or constraint.get("applicable") is not True):
                status=("not_applicable" if constraint.get("applicable") is False and constraint.get("applicability_source_ref")
                    and constraint.get("source_ref") and constraint["constraint_id"] not in self.context["assessment_scope"].get("mapping_unknowns",[]) else "unknown")
            vector.append(dict(constraint_id=constraint["constraint_id"],status=status,consumer=consumer,
                consumed=status!="unknown",source_ref=constraint.get("source_ref")))
        included=set(self.context["assessment_scope"]["included_constraint_ids"])
        chosen=[v for v in vector if v["constraint_id"] in included]
        statuses=[v["status"] for v in chosen]
        policies={r["constraint_id"]:r for r in self.context["assessment_scope"]["required_constraint_inventory"]}
        accepted=bool(chosen) and all(v["status"] in ("satisfied","not_applicable") or (v["status"]=="managed"
            and policies[v["constraint_id"]].get("allow_managed") is True
            and policies[v["constraint_id"]].get("managed_policy_source_ref")) for v in chosen)
        scoped="unacceptable" if "unacceptable" in statuses else "model_feasible" if accepted else "unknown"
        # Supplied scope inventories and sources are declarations, never proof of complete Required CNS.
        full="unacceptable" if any(v["status"]=="unacceptable" for v in vector) else "unknown"
        return dict(base,evaluation_status="evaluated",execution_track="shared_models_synthetic_inputs" if self.context["evidence_track"]=="synthetic_fixture" else "shared_models_unverified_inputs",
            scoped_model_status=scoped,complete_required_cns_status=full,constraint_vector=vector,
            applied_action_ids=sorted(applied),native_results=dict(baseline=baseline,post_plan=post,p17=p17),
            native_materials=dict(baseline=bmat,**material),rtk_joint_result=rtk,rtk_joint_inputs=rtk_inputs,
            unconsumed_requirements=deepcopy(snapshot.get("unconsumed_requirements",[])),
            site_counts=count_joint_plan_sites(actions=actions,frozen_domain=self.domain,post_activation_evidence={},participation_evidence={}),
            shared_chain_replay_count=self.replay_count)


def evaluate_joint_action_set(*, actions, frozen_context, evaluation_cache):
    verify(frozen_context,"context_fingerprint")
    fp=candidate_action_set_fingerprint(actions=actions);key=digest([frozen_context["context_fingerprint"],fp])
    if key not in evaluation_cache:
        evaluation_cache[key]=JointModelEvaluator(frozen_context=frozen_context,frozen_domain=frozen_context["frozen_domain"]).evaluate(actions=actions)
    return deepcopy(evaluation_cache[key])
