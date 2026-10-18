"""M5-B isolated contracts, approved M4 tiny fixture only; no real replay."""
from copy import deepcopy
import importlib
import importlib.util
import pytest
from test_joint_feasibility_evaluation import model_fixture
from cns_planner.domain.joint_feasibility import seal, digest


def api(name="existing_site_action_binding"):
    module="cns_planner.application."+name
    assert importlib.util.find_spec(module) is not None, "M5-B interface not implemented"
    return importlib.import_module(module)


def binding_inputs(track="synthetic_fixture"):
    d,_=model_fixture();d=deepcopy(d);d["evidence_track"]=track
    for dev in d["device_catalog"]["items"]:
        dev.update(catalog_version="catalog-1",model_version="model-1",source_ref="registered-catalog",evidence_track=track)
    units=[];bindings=[]
    for i,a in enumerate(d["installable_actions"]):
        a.update(slot_id="slot-"+str(i))
        units.append(dict(planning_unit_id=a["action_id"],service_key=a["service_key"],device_id=a["device_id"],evidence_track=track))
        bindings.append(dict(planning_unit_id=a["action_id"],action_id=a["action_id"],device_id=a["device_id"],
            catalog_version="catalog-1",model_version="model-1",device_instance_id=a["device_instance_id"],
            physical_site_id=a["physical_site_id"],slot_id=a["slot_id"],evidence_track=track,
            catalog_source_ref="registered-catalog",capability_source_ref="capability",qualification_source_ref="qualification",
            installation_permission_source_ref="permission",capacity_source_ref="capacity",
            dependency_action_ids=["comm"] if a["action_id"]=="rtk" else []))
    d["physical_sites"][0]["colocation_rule"]={"max_devices":2,"source_ref":"capacity"}
    d=seal(d,"domain_fingerprint")
    return dict(planning_units=units,selection_bindings=bindings,domain_review=dict(schema_version="existing_site_domain_review.v1",
        frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"],source_manifest={"source_ref":"registered"}),
        model_capabilities={"model_version":"1.0"},scope_declaration={"evidence_track":track,"fixed_configurations":[]})


def bind(args):return api().bind_device_actions(**args)


def test_rtk_requires_real_catalog_selection():
    args=binding_inputs("engineering_research")
    next(u for u in args["planning_units"] if u["planning_unit_id"]=="rtk")["device_id"]=None
    r=bind(args)
    assert r["binding_status"]=="blocked"
    row=next(a for a in r["blocked_actions"] if a["planning_unit_id"]=="rtk")
    assert row["reason_code"]=="rtk_device_selection_missing" and "device_id" in row["missing_fields"]
    assert all(a["action_id"]!="rtk" for a in r["bound_actions"])


@pytest.mark.parametrize("field",["device_id","catalog_version","model_version","device_instance_id","physical_site_id","slot_id"])
def test_inconsistent_explicit_identity_is_blocked(field):
    args=binding_inputs();args["selection_bindings"][0][field]="unregistered"
    r=bind(args)
    assert r["binding_status"]=="blocked" and field in str(r["blocked_actions"])


@pytest.mark.parametrize("field",["catalog_source_ref","capability_source_ref","qualification_source_ref","installation_permission_source_ref","capacity_source_ref"])
def test_missing_qualification_is_needs_evidence(field):
    args=binding_inputs();args["selection_bindings"][0][field]=None
    r=bind(args)
    assert r["binding_status"]=="blocked"
    assert r["blocked_actions"][0]["qualification_status"]=="needs_evidence"
    assert field in r["blocked_actions"][0]["missing_fields"]


def test_real_track_rejects_synthetic_catalog_and_actions():
    args=binding_inputs("engineering_research")
    # Production identity must not be laundered by changing only a track label.
    r=bind(args)
    assert r["binding_status"]=="blocked"
    assert "fixture_cross_track_rejected" in str(r["blocked_actions"])


def test_dependency_devices_count_once_and_input_is_readonly():
    args=binding_inputs();before=deepcopy(args);r=bind(args)
    assert r["binding_status"]=="bound_within_declared_model"
    assert r["bound_actions"]==args["domain_review"]["frozen_domain"]["installable_actions"]
    assert r["N_new_device_instances"]==2 and r["N_plan_sites"]==1
    assert r["N_newly_activated_existing_sites"] is None
    assert r["dependency_closure"]["rtk"]==["comm"]
    assert args==before and not r["persisted_to_project_state"]
    assert r["family_support_matrix"]["S:rid_cooperative"]["action_support"]=="supported"
    assert r["family_support_matrix"]["S:radar_noncooperative"]["action_support"]=="unsupported"


def test_missing_dependency_blocks_consumer_and_retains_full_input():
    args=binding_inputs();args["selection_bindings"][1]["dependency_action_ids"]=["missing-C"]
    r=bind(args)
    assert r["binding_status"]=="blocked" and "dependency_action_ids" in str(r["blocked_actions"])
    assert r["selection_bindings"]==args["selection_bindings"]


@pytest.mark.parametrize("service",["S:radar_noncooperative","N:navigation_integrity_monitoring"])
def test_unsupported_family_never_silently_disappears(service):
    args=binding_inputs();args["planning_units"].append(dict(planning_unit_id="unsupported",service_key=service,device_id="x"))
    r=bind(args)
    assert r["binding_status"]=="blocked" and "unsupported_constraint_consumer" in str(r["blocked_actions"])
    assert r["family_support_matrix"][service]["evaluation_support"]=="fixed_configuration_requires_explicit_scope"
    args["scope_declaration"]["fixed_configurations"]=[dict(service_key=service,source_ref="explicit-user-scope",configuration_fingerprint="layout-1")]
    r=bind(args)
    assert r["binding_status"]=="blocked"  # fixed baseline never consumes a requested new action
    assert r["family_support_matrix"][service]["evaluation_support"]=="fixed_configuration_only"


def test_duplicate_instance_or_binding_is_rejected():
    args=binding_inputs();args["selection_bindings"].append(deepcopy(args["selection_bindings"][0]))
    with pytest.raises(ValueError,match="duplicate"):bind(args)


def test_missing_capacity_limit_cannot_bind():
    args=binding_inputs();d=args["domain_review"]["frozen_domain"]
    d["physical_sites"][0]["colocation_rule"].pop("max_devices")
    d=seal(d,"domain_fingerprint");args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    r=bind(args)
    assert r["binding_status"]=="blocked" and "capacity_limit" in str(r["blocked_actions"])


def evaluation_inputs():
    from test_existing_site_domain_discovery import objective_inputs
    d,c=model_fixture()
    return dict(frozen_context=c,frozen_domain=d,domain_review=dict(schema_version="existing_site_domain_review.v1",
        frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"]),
        objective_policy=objective_inputs()["objective_policy"],evidence_registry={})


def evaluator(args):return api("existing_site_evaluation_adapter").ExistingSiteEvaluator(**args)


def test_joint_rtk_c_dependency_and_unknown_segments():
    from test_navigation_rtk_augmentation_chain import navigation_entry
    args=evaluation_inputs();e=evaluator(args);actions=args["frozen_domain"]["installable_actions"]
    single=[e.evaluate_action_set(actions=[a]) for a in actions];joint=e.evaluate_action_set(actions=actions)
    assert all(r["scoped_model_status"]!="model_feasible" for r in single)
    assert joint["scoped_model_status"]=="model_feasible"
    nav=navigation_entry(joint["native_results"]["post_plan"]["p14"])
    assert nav["geometry_status"]=="satisfied" and nav["delivery_status"]=="satisfied"
    assert joint["rtk_joint_result"]["navigation_status"]=="unknown"
    assert joint["rtk_segment_evidence"]["inputs"]==joint["rtk_joint_inputs"]
    assert joint["rtk_segment_evidence"]["result"]==joint["rtk_joint_result"]
    assert joint["rtk_segment_evidence"]["missing_fields"]
    assert joint["objective_measurement"]["values"]==dict(activation=None,devices=2,cost=None)


def test_native_and_m3_statuses_preserved():
    from cns_planner.application.joint_evaluation_adapter import JointModelEvaluator
    from cns_planner.application.joint_candidate_assessment import bind_joint_candidate_assessment
    args=evaluation_inputs();actions=args["frozen_domain"]["installable_actions"];before=deepcopy(args)
    raw=JointModelEvaluator(frozen_context=args["frozen_context"],frozen_domain=args["frozen_domain"]).evaluate(actions=actions)
    m3=bind_joint_candidate_assessment(model_evaluation=raw,frozen_context=args["frozen_context"],actions=actions,evidence_registry={})
    r=evaluator(args).evaluate_action_set(actions=actions)
    assert r["native_results"]==raw["native_results"] and r["native_materials"]==raw["native_materials"]
    assert r["candidate_assessment"]==m3 and r["m3_assessment"]==m3["m3_assessment"]
    assert r["unconsumed_requirements"]==raw["unconsumed_requirements"]
    assert any(x["field"]=="communication.max_gap_m" for x in r["unconsumed_requirements"])
    assert r["independent_verification_status"]=="not_verified" and not r["final_research_admission"]["eligible"]
    assert r["complete_required_cns_status"]!="satisfied" and args==before
    identity=r["binding_identity"]
    assert identity["route_id"]=="R1" and identity["operating_layer_id"]=="ALT-080"
    assert all(identity[k] for k in ("context_fingerprint","domain_fingerprint","route_geometry_fingerprint","source_registry_fingerprint","native_parent_fingerprint"))


@pytest.mark.parametrize("change",["applied","p17_applied","parent","context","route","layer","source"])
def test_projection_applies_entire_action_set(monkeypatch,change):
    args=evaluation_inputs();e=evaluator(args);evaluate=e.model.evaluate
    def damaged(*,actions):
        r=evaluate(actions=actions)
        if change=="applied":r["applied_action_ids"]=r["applied_action_ids"][:-1]
        if change=="p17_applied":r["native_materials"]["p17"]["evaluation_inputs"]["post_plan_projection"]["applied_action_ids"]=[]
        if change=="parent":r["native_materials"]["p15"]["fingerprint_input"]["corridor_assessment"]={}
        if change=="context":r["context_fingerprint"]="wrong"
        if change=="route":r["native_results"]["post_plan"]["p14"]["routes"][0]["route_id"]="wrong"
        if change=="layer":r["native_results"]["post_plan"]["p14"]["routes"][0]["voxels"][0]["altitude_layer_id"]="wrong"
        if change=="source":r["native_materials"]["p14"]["fingerprint_input"]["device_catalog"]={}
        return r
    monkeypatch.setattr(e.model,"evaluate",damaged)
    r=e.evaluate_action_set(actions=args["frozen_domain"]["installable_actions"])
    assert r["input_status"]=="input_inconsistent" and r["scoped_model_status"]!="model_feasible"
    assert not r["final_research_admission"]["eligible"] and r["identity_errors"]


@pytest.mark.parametrize("service,family",[("S:radar_noncooperative","radar_panel"),("N:navigation_integrity_monitoring","endpoint_integrity_monitor")])
def test_radar_endpoint_actions_block_before_replay(monkeypatch,service,family):
    args=evaluation_inputs();d=args["frozen_domain"];d["installable_actions"][0].update(service_key=service,planner_family=family)
    d=seal(d,"domain_fingerprint");args["frozen_domain"]=d
    c=args["frozen_context"];c.update(frozen_domain=d,domain_fingerprint=d["domain_fingerprint"]);args["frozen_context"]=seal(c,"context_fingerprint")
    args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    e=evaluator(args)
    monkeypatch.setattr(e.model,"evaluate",lambda **kw:pytest.fail("unsupported action replayed"))
    r=e.evaluate_action_set(actions=d["installable_actions"])
    assert r["evaluation_status"]=="unsupported_constraint_consumer" and r["actions"]==d["installable_actions"]
    assert r["scoped_model_status"]=="unknown" and not r["new_site_stage_unlocked"]


def test_real_rtk_no_contract_never_replays_or_fills_synthetic(monkeypatch):
    args=evaluation_inputs();d=args["frozen_domain"];d["evidence_track"]="engineering_research";d=seal(d,"domain_fingerprint")
    args["frozen_domain"]=d;c=args["frozen_context"];c.update(frozen_domain=d,domain_fingerprint=d["domain_fingerprint"],evidence_track="engineering_research")
    args["frozen_context"]=seal(c,"context_fingerprint");args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    e=evaluator(args);monkeypatch.setattr(e.model,"evaluate",lambda **kw:pytest.fail("unauthorized real replay"))
    r=e.evaluate_action_set(actions=d["installable_actions"])
    assert r["evaluation_status"]=="readonly_evaluation_unavailable" and "real_track" in str(r["reason_codes"])
    assert r["applied_action_ids"]==[] and r["rtk_segment_evidence"]["missing_fields"]


def test_review_and_context_mismatch_are_rejected():
    args=evaluation_inputs();args["domain_review"]["original_domain_fingerprint"]="wrong"
    with pytest.raises(ValueError,match="identity"):evaluator(args)


@pytest.mark.parametrize("field",["provider_inventory_status","handover_status","delivery_status","providers"])
def test_original_m2_missing_segment_fields_remain_unknown(field):
    from test_rtk_joint_acceptance import evaluate,segment
    row=segment();row[field]=[] if field=="providers" else None
    r=evaluate([row])
    # The adapter must disclose missing evidence even if native M2 accepts a
    # sufficient positive provider with inventory completeness still unknown.
    mod=api("existing_site_evaluation_adapter")
    receipt=mod._segment_evidence(dict(rtk_joint_inputs=dict(segments=[row],speed_profile=[{"source_ref":"fixture"}]),rtk_joint_result=r))
    assert receipt["navigation_status"]=="unknown" and receipt["missing_fields"]
    assert receipt["result"]==r


def test_original_inventory_gap_is_disclosed_without_rewriting_m2():
    from test_rtk_joint_acceptance import evaluate,segment
    row=segment(provider_inventory_status=None);native=evaluate([row])
    assert native["navigation_status"]=="satisfied"  # observed existing M2 contract gap
    receipt=api("existing_site_evaluation_adapter")._segment_evidence(dict(
        rtk_joint_inputs=dict(segments=[row],speed_profile=[{"source_ref":"fixture"}],backup_scenario={}),rtk_joint_result=native))
    assert receipt["navigation_status"]=="unknown"
    assert receipt["result"]==native and "segments[0].provider_inventory_status" in receipt["missing_fields"]


def test_same_model_second_slot_returns_blocked_not_new_upstream_semantics():
    args=binding_inputs();d=args["domain_review"]["frozen_domain"]
    d["installable_actions"][1]["device_id"]=d["installable_actions"][0]["device_id"]
    d["installable_actions"][1]["service_key"]=d["installable_actions"][0]["service_key"]
    args["planning_units"][1].update(device_id=d["installable_actions"][0]["device_id"],service_key=d["installable_actions"][0]["service_key"])
    args["selection_bindings"][1]["device_id"]=d["installable_actions"][0]["device_id"]
    d=seal(d,"domain_fingerprint");args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    r=bind(args)
    assert r["binding_status"]=="blocked" and "unsupported_upstream_multiplicity" in str(r["blocked_actions"])


def test_1000m_remains_unconsumed_without_changing_requirement():
    from test_ue_rule_adapter import inputs,build
    snapshot=build(**inputs())
    assert any(r.get("field")=="communication.max_gap_m" and r.get("value")==1000 for r in snapshot["unconsumed_requirements"])


@pytest.mark.parametrize("inventory",["partial","unknown",""])
def test_declared_incomplete_inventory_never_closes_evidence_gate(inventory):
    from test_rtk_joint_acceptance import evaluate,segment
    row=segment(provider_inventory_status=inventory)
    receipt=api("existing_site_evaluation_adapter")._segment_evidence(dict(
        rtk_joint_inputs=dict(segments=[row],speed_profile=[{"source_ref":"fixture"}],backup_scenario={"source_ref":"fixture"}),
        rtk_joint_result=evaluate([row])))
    assert receipt["navigation_status"]=="unknown" and "segments[0].provider_inventory_status" in receipt["missing_fields"]


def test_installed_instance_is_separate_from_proposed_and_reconfiguration():
    args=binding_inputs();d=args["domain_review"]["frozen_domain"];action=d["installable_actions"][0]
    d["installed_device_instances"]=[{k:action[k] for k in ("device_id","device_instance_id","physical_site_id","slot_id")}]
    action["action_type"]="reconfigure_device"
    d=seal(d,"domain_fingerprint");args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    r=bind(args)
    assert r["binding_status"]=="bound_within_declared_model" and r["N_new_device_instances"]==1
    assert r["installed_device_instances"][0]["device_instance_id"] not in r["proposed_device_instances"]


def test_borrowed_synthetic_track_is_rejected_at_binding_boundary():
    args=binding_inputs();args["selection_bindings"][0]["evidence_track"]="engineering_research"
    r=bind(args)
    assert r["binding_status"]=="blocked" and "evidence_track" in str(r["blocked_actions"])


def test_combination_capacity_conflict_closes_b_feasibility_without_rewriting_native():
    args=evaluation_inputs();d=args["frozen_domain"]
    d["physical_sites"][0]["colocation_rule"]={"max_devices":1,"source_ref":"synthetic-capacity"}
    d=seal(d,"domain_fingerprint");args["frozen_domain"]=d;c=args["frozen_context"]
    c.update(frozen_domain=d,domain_fingerprint=d["domain_fingerprint"]);args["frozen_context"]=seal(c,"context_fingerprint")
    args["domain_review"].update(frozen_domain=d,original_domain_fingerprint=d["domain_fingerprint"])
    r=evaluator(args).evaluate_action_set(actions=d["installable_actions"])
    assert r["objective_measurement"]["input_status"]=="combination_conflict"
    assert r["input_status"]=="combination_conflict" and r["scoped_model_status"]!="model_feasible"
    assert r["candidate_assessment"]["scoped_model_status"]=="model_feasible"
    assert r["binding_blockers"]==["combination_constraints_violated"]
