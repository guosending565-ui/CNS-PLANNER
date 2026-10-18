"""M4 C: real shared pure models on tiny synthetic inputs; no production session."""
from copy import deepcopy
import builtins
import importlib
import importlib.util
import pytest
from test_joint_candidate_domain import api as domain_api


def evaluator_api():
    name="cns_planner.application.joint_evaluation_adapter"
    assert importlib.util.find_spec(name) is not None, "M4 C evaluator not implemented"
    return importlib.import_module(name)


def model_fixture():
    import test_navigation_rtk_augmentation_chain as f
    from cns_planner.application.navigation_reference_station_planning_service import navigation_reference_station_candidate_actions
    from cns_planner.application.ue_rule_adapter import build_frozen_ue_rule_snapshot
    from test_ue_rule_adapter import inputs
    tower=f.not_installed_tower("S1",122.,30.,tower_id="T1")
    req=f.required(planning=f.ready_planning(),communication_required=True)
    state=f.p16_state(required_cns=req,tower_sites={"items":[tower]},communication_count=1)
    state["aircraft_profiles"]["items"][0]["aircraft_id"]="TEST-AC"
    evidence=f.planning_evidence(sites=[tower])
    nav=navigation_reference_station_candidate_actions([f.navigation_action_target()],navigation_evidence=evidence)[0]
    # Explicit synthetic catalog selection, never promote unresolved production planning units.
    nav.update(action_id="rtk",device_id="RTK-fixture",device_instance_id="new-rtk",physical_site_id="tower:T1",
        equipment_selection_status="synthetic_fixture_selected",not_a_device_model=False)
    device=deepcopy(f.communication_facilities(count=2)["items"][1]["devices"][0]);device["device_id"]="C-fixture"
    state["device_catalog"]={"items":[device,{"device_id":"RTK-fixture","subsystem":"N","service_key":f.RTK}]}
    comm=dict(action_id="comm",action_type="add_device_to_explicit_site",device_id="C-fixture",device_instance_id="new-c",
        site_id="S1",distinct_site_id="tower:T1",physical_site_id="tower:T1",planner_family="omnidirectional_site",
        subsystem="C",service_key=f.COMMUNICATION,device_service_key=f.COMMUNICATION,device_type=device["type"],
        coordinate=[122.,30.],vertical={"confirmed":True,"service_origin_egm2008_m":10.},
        reuse_class="tower_colocation_host",host={"host_type":"tower","host_tower_id":"T1"})
    for a in (comm,nav):a["qualification"]={"status":"qualified_under_model","source_ref":"synthetic-fixture"}
    da=domain_api();domain=da.freeze_existing_domain(discovery_manifest={"completeness":"controlled_subset"},
        site_records=[dict(physical_site_id="tower:T1",existing_evidence={"status":"confirmed","source_ref":"fixture"},
            qualification={"status":"qualified_under_model","source_ref":"fixture"},baseline_activation_status="inactive_verified",activation_source_ref="fixture")],
        installed_inventory=[],device_catalog=state["device_catalog"],family_action_records=[comm,nav],
        qualification_policy={},scenario_scope={"evidence_track":"synthetic_fixture"})
    m1=inputs();m1["route_id"]="R1";m1["required_cns"]=req;m1["aircraft_profile"]=deepcopy(state["aircraft_profiles"]["items"][0]);m1["operation_scenario"]["space_scope"]={"route_id":"R1"};m1["operation_scenario"]["cruise_altitude"]="ALT-080";m1["operation_scenario"]["operating_layer_id"]="ALT-080"
    snap=build_frozen_ue_rule_snapshot(**m1)
    inventory=[dict(constraint_id=f.RTK,service_key=f.RTK,kind="engineering_geometry",field="navigation",hard=True,
        applicable=True,source_ref="synthetic-fixture",consumer={"kind":"p14_service","service_key":f.RTK})]
    scope=da.freeze_constraint_scope(frozen_rule_snapshot=snap,required_constraint_inventory=inventory,
        scope_declaration=dict(scope_id="geometry-subset",version="1",source_ref="fixture",kind="declared_engineering_subset",
            included_constraint_ids=[f.RTK],excluded_constraints=[]))
    ctx=da.build_joint_search_context(frozen_rule_snapshot=snap,frozen_domain=domain,baseline_inputs={"model_state":state},
        evaluator_versions={"p14":"1.4","p15":"1.3","p17":"2.4","m2":"rtk_joint_acceptance.v1"},assessment_scope=scope)
    return domain,ctx


def test_joint_projection_not_sum_of_single_impacts():
    mod=evaluator_api();d,c=model_fixture();e=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)
    actions=d["installable_actions"]
    single=[e.evaluate(actions=[a]) for a in actions];joint=e.evaluate(actions=actions)
    assert all(x["scoped_model_status"]!="model_feasible" for x in single)
    assert joint["scoped_model_status"]=="model_feasible"
    assert joint["execution_track"]=="shared_models_synthetic_inputs"
    assert joint["applied_action_ids"]==[a["action_id"] for a in actions]
    assert joint["complete_required_cns_status"]!="satisfied"


def test_rtk_and_communication_joint_delivery():
    eapi=evaluator_api();d,c=model_fixture();e=eapi.JointModelEvaluator(frozen_context=c,frozen_domain=d)
    joint=e.evaluate(actions=d["installable_actions"])
    from test_navigation_rtk_augmentation_chain import navigation_entry
    nav=navigation_entry(joint["native_results"]["post_plan"]["p14"])
    assert nav["geometry_status"]=="satisfied" and nav["delivery_status"]=="satisfied"
    assert joint["rtk_joint_result"]["navigation_status"]=="unknown"  # inventory/handover not invented


def test_readonly_state_artifact_and_source_objects_unchanged(monkeypatch,tmp_path):
    mod=evaluator_api();d,c=model_fixture();before=deepcopy((d,c));e=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)
    protected=tmp_path/"formal-artifact";protected.write_bytes(b"unchanged");old=list(tmp_path.iterdir())
    original=builtins.open
    def no_write(file,mode="r",*args,**kwargs):
        if any(x in mode for x in "wax+"):raise AssertionError("model attempted file write")
        return original(file,mode,*args,**kwargs)
    import io
    original_io=io.open
    def no_io_write(file,mode="r",*args,**kwargs):
        if any(x in mode for x in "wax+"):raise AssertionError("model attempted pathlib write")
        return original_io(file,mode,*args,**kwargs)
    monkeypatch.setattr(builtins,"open",no_write)
    monkeypatch.setattr(io,"open",no_io_write)
    e.evaluate(actions=d["installable_actions"])
    assert (d,c)==before and protected.read_bytes()==b"unchanged" and list(tmp_path.iterdir())==old


@pytest.mark.parametrize("value",[lambda:None,object()])
def test_runtime_provider_and_prototype_are_not_json_selectable(value):
    mod=evaluator_api();d,c=model_fixture();c["baseline_inputs"]["provider"]=value
    with pytest.raises(ValueError):mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)


def test_p17_receives_candidate_inputs_without_formal_plan():
    mod=evaluator_api();d,c=model_fixture();r=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    inputs=r["native_materials"]["p17"]["evaluation_inputs"]
    assert inputs["site_plan"]["selected_actions"]==d["installable_actions"]
    assert inputs["post_plan_projection"]["corridor_assessment"]==r["native_results"]["post_plan"]["p14"]


def test_skipped_action_blocks_full_evaluation():
    mod=evaluator_api();d,c=model_fixture();bad=deepcopy(d["installable_actions"]);bad[0]["device_id"]="missing"
    with pytest.raises(ValueError):mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=bad)


def test_radar_rtk_and_communication_unconsumed_remain_unknown():
    mod=evaluator_api();d,c=model_fixture();r=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    assert r["complete_required_cns_status"]=="unknown"
    assert r["rtk_joint_result"]["unknowns"]
    assert r["new_site_stage_unlocked"] is False


def test_complexity_admission_is_not_infeasibility(monkeypatch):
    mod=evaluator_api();d,c=model_fixture()
    monkeypatch.setattr(mod,"_complexity_preflight",lambda inputs:{"tier":"beyond_safety_ceiling"})
    r=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    assert r["evaluation_status"]=="readonly_evaluation_unavailable"
    assert r["scoped_model_status"]=="unknown"


def test_p17_joint_binding_contains_baseline_and_post_plan():
    mod=evaluator_api();d,c=model_fixture();r=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    inputs=r["native_materials"]["p17"]["evaluation_inputs"]
    assert inputs["corridor_assessment"]==r["native_results"]["baseline"]["p14"]
    assert inputs["post_plan_projection"]["corridor_assessment"]==r["native_results"]["post_plan"]["p14"]


def test_exception_path_is_readonly(monkeypatch,tmp_path):
    import io
    mod=evaluator_api();d,c=model_fixture();before=deepcopy((d,c));e=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)
    original=io.open
    def deny_write(file,mode="r",*args,**kw):
        if any(x in mode for x in "wax+"):raise AssertionError("unexpected model write")
        return original(file,mode,*args,**kw)
    monkeypatch.setattr(io,"open",deny_write)
    monkeypatch.setattr(mod.CNSServiceCorridorV1,"evaluate",lambda *a,**k:(_ for _ in ()).throw(ValueError("fixture interruption")))
    r=e.evaluate(actions=d["installable_actions"])
    assert r["evaluation_status"]=="readonly_evaluation_unavailable" and (d,c)==before
    assert not list(tmp_path.iterdir())


def test_requested_model_versions_must_match_fixed_factories():
    from cns_planner.domain.joint_feasibility import seal
    mod=evaluator_api();d,c=model_fixture();c["evaluator_versions"]["p14"]="9.9"
    c=seal(c,"context_fingerprint")
    with pytest.raises(ValueError,match="version"):
        mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)


@pytest.mark.parametrize("allow,want",[(False,"unknown"),(True,"model_feasible")])
def test_managed_requires_explicit_scoped_policy(monkeypatch,allow,want):
    from cns_planner.domain.joint_feasibility import seal
    mod=evaluator_api();d,c=model_fixture();scope=deepcopy(c["assessment_scope"])
    scope["required_constraint_inventory"]=[dict(constraint_id="C:communication",kind="service_acceptability",hard=True,applicable=True,
        field="communication",source_ref="fixture",consumer={"kind":"p17_service","service_key":"C:communication"},
        allow_managed=allow,managed_policy_source_ref="explicit-fixture-policy" if allow else None)]
    scope["included_constraint_ids"]=["C:communication"];c["assessment_scope"]=seal(scope,"scope_fingerprint")
    c["scope_fingerprint"]=c["assessment_scope"]["scope_fingerprint"];c=seal(c,"context_fingerprint")
    # This test verifies scoped aggregation of an already evaluated native status, not a safety formula.
    monkeypatch.setattr(mod,"_extract_service_events",lambda *a:{"service_assessments":[{"service_key":"C:communication","status":"managed"}]})
    result=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    assert result["constraint_vector"][0]["status"]=="managed"
    assert result["scoped_model_status"]==want and result["complete_required_cns_status"]!="satisfied"


@pytest.mark.parametrize("change",["unmapped","applicability","source"])
def test_unresolved_scope_mapping_cannot_become_local_feasible(change):
    from cns_planner.domain.joint_feasibility import seal
    from cns_planner.application.joint_candidate_domain import freeze_constraint_scope
    mod=evaluator_api();d,c=model_fixture();scope=deepcopy(c["assessment_scope"]);item=scope["required_constraint_inventory"][0]
    if change=="unmapped":
        item["constraint_id"]="unmapped-rule";scope["included_constraint_ids"]=["unmapped-rule"]
    if change=="applicability":item["applicable"]=None
    if change=="source":item["source_ref"]=None
    scope=freeze_constraint_scope(frozen_rule_snapshot=c["frozen_rule_snapshot"],
        required_constraint_inventory=scope["required_constraint_inventory"],scope_declaration=scope)
    c["assessment_scope"]=scope;c["scope_fingerprint"]=scope["scope_fingerprint"];c=seal(c,"context_fingerprint")
    result=mod.JointModelEvaluator(frozen_context=c,frozen_domain=d).evaluate(actions=d["installable_actions"])
    assert result["scoped_model_status"]=="unknown"
    assert result["constraint_vector"][0]["consumed"] is False


def test_declared_operating_layer_conflict_is_rejected():
    from cns_planner.domain.joint_feasibility import seal
    mod=evaluator_api();d,c=model_fixture()
    c["baseline_inputs"]["model_state"]["spatial_3d"]["altitude_layers"][0]["altitude_layer_id"]="OTHER-LAYER"
    c=seal(c,"context_fingerprint")
    with pytest.raises(ValueError,match="layer"):
        mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)


def test_corrupted_frozen_rules_cannot_enter_model_callback():
    from cns_planner.domain.joint_feasibility import seal
    mod=evaluator_api();d,c=model_fixture();c["frozen_rule_snapshot"]["input_fingerprint"]="bad"
    c=seal(c,"context_fingerprint")
    with pytest.raises(ValueError,match="rule"):
        mod.JointModelEvaluator(frozen_context=c,frozen_domain=d)
