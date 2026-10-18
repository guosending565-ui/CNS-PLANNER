"""M3 small synthetic contracts; never loads a production ProjectState."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest


def api():
    from cns_planner.application import candidate_ue_acceptability
    return candidate_ue_acceptability


def digest(value, native=False):
    options = {} if native else dict(separators=(',', ':'), allow_nan=False)
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, **options).encode()).hexdigest()


def actions():
    return [dict(action_id='a', action_type='add_device', site_id='site-a', device_id='radio-a'),
            dict(action_id='b', action_type='add_device', site_id='site-b', device_id='radio-b')]


def snapshot():
    from test_rtk_joint_acceptance import snapshot as build
    return build()


def context(snap=None):
    snap = snap or snapshot()
    scenario = snap['frozen_inputs']['operation_scenario']
    return dict(schema_version='candidate_ue_context.v1',
        **{k:snap[k] for k in ('route_id','scenario_id','operating_phase')},
        operating_layer_id='L100', cruise_altitude=scenario['cruise_altitude'],
        space_scope=scenario['space_scope'], route_geometry_fingerprint='route-geometry',
        m1_rule_fingerprint=snap['input_fingerprint'],
        expected_action_set_fingerprint=api().candidate_action_set_fingerprint(actions=actions()),
        frozen_dependencies={k:'fixture-'+k for k in ('device_catalog_fingerprint',
            'candidate_domain_fingerprint','applicable_equipment_fingerprint',
            'source_registry_fingerprint','evidence_policy_version')},
        expected_algorithms=dict(m1_schema_version='frozen_ue_rules.v1',
            p14=dict(algorithm_id='cns_service_corridor_v1',algorithm_version='1.4'),
            p15=dict(algorithm_id='cns_corridor_gap_v1',algorithm_version='1.3'),
            p17=dict(algorithm_id='continuous_service_acceptability_v1',algorithm_version='2.4'),
            m2=dict(algorithm_id='rtk_joint_acceptance',algorithm_version='rtk_joint_acceptance.v1')),
        evidence_track='synthetic_fixture')


def codes(checks):
    return {x['reason_code'] for x in checks['errors'] + checks['unknowns']}


def test_action_set_identity():
    fn=api().candidate_action_set_fingerprint
    rows=actions()
    assert fn(actions=rows)==fn(actions=list(reversed(rows)))
    assert fn(actions=rows)!=fn(actions=rows[:1])
    other=deepcopy(rows); other[0]['device_id']='different'
    assert fn(actions=rows)!=fn(actions=other)
    assert rows==actions()


@pytest.mark.parametrize('change', ['duplicate','missing','conflict'])
def test_action_set_invalid(change):
    rows=actions()
    if change=='duplicate': rows.append(deepcopy(rows[0]))
    if change=='missing': del rows[0]['device_id']
    if change=='conflict': rows[1].update(site_id='site-a',device_id='radio-a')
    with pytest.raises(ValueError): api().candidate_action_set_fingerprint(actions=rows)


@pytest.mark.parametrize('value', [float('nan'),float('inf'),{1:'non-string-key'}])
def test_action_set_non_json(value):
    rows=actions(); rows[0]['configuration']=value
    with pytest.raises(ValueError): api().candidate_action_set_fingerprint(actions=rows)


def test_action_set_internal_array_order_is_preserved():
    rows=actions(); rows[0]['configuration']=[1,2]
    other=deepcopy(rows); other[0]['configuration'].reverse()
    assert api().candidate_action_set_fingerprint(actions=rows)!=api().candidate_action_set_fingerprint(actions=other)


def test_m1_reconstruction_is_deterministic_and_readonly():
    snap=snapshot(); old=deepcopy(snap)
    assert api()._validate_m1(snap)==api()._validate_m1(snap)
    assert not api()._validate_m1(snap)['errors']
    assert snap==old


def test_m1_old_digest_rejected():
    snap=snapshot(); snap['rules'][0]['threshold']['value']=90
    assert 'm1_fingerprint_mismatch' in codes(api()._validate_m1(snap))


def test_resigned_derived_rule_is_rejected():
    snap=snapshot(); snap['rules'][0]['threshold']['value']=90
    snap['input_fingerprint']=digest({k:v for k,v in snap.items() if k!='input_fingerprint'})
    assert 'm1_reconstruction_mismatch' in codes(api()._validate_m1(snap))


def test_context_identity_and_missing_are_distinct():
    snap=snapshot(); ctx=context(snap)
    assert not api()._validate_context(snap,ctx,actions())['errors']
    ctx['operating_phase']='approach'
    assert 'scope_mismatch' in codes(api()._validate_context(snap,ctx,actions()))
    del ctx['operating_phase']
    assert 'scope_missing' in codes(api()._validate_context(snap,ctx,actions()))
# T04--T07: native evidence materials are small and synthetic, not trusted receipts.
def native_pair(ctx, stage='post_plan'):
    from cns_planner.domain.cns_corridor import fixed_corridor_width_contract
    material=dict(routes=[dict(route_id=ctx['route_id'],path=[[0,0],[0.001,0]])],
        spatial_3d={},grid={},terrain={},required_cns={},aircraft_profile={},
        existing_facilities={'stage':stage},device_catalog={},corridor_policy={},parameters={},
        coverage_parameters={},capability_parameters={},surface_facts_fingerprint=None,
        algorithm_semantics=ctx['expected_algorithms']['p14'],
        fixed_corridor_width_contract=fixed_corridor_width_contract())
    p14=dict(status='failed',algorithm_id='cns_service_corridor_v1',algorithm_version='1.4',
        input_fingerprint=digest(material,True), routes=[dict(route_id=ctx['route_id'],
        route_length_m=100,voxels=[],subsystems=[])],
        fixed_corridor_width_contract=fixed_corridor_width_contract())
    gmat=dict(corridor_assessment=p14,required_cns={},planning_objectives={},parameters={},
        algorithm_semantics=ctx['expected_algorithms']['p15'],
        fixed_corridor_width_contract=fixed_corridor_width_contract())
    p15=dict(status='failed',algorithm_id='cns_corridor_gap_v1',algorithm_version='1.3',
        input_fingerprint=digest(gmat,True),routes=[dict(route_id=ctx['route_id'],
            route_length_m=100,voxels=[],subsystems=[])], input_provenance=dict(
                corridor_algorithm_id=p14['algorithm_id'],corridor_algorithm_version='1.4',
                corridor_input_fingerprint=p14['input_fingerprint']))
    return {'p14':envelope(ctx,'p14',p14,dict(fingerprint_input=material),stage),
            'p15':envelope(ctx,'p15',p15,dict(fingerprint_input=gmat),stage)}


def envelope(ctx,model,payload,binding,stage='post_plan'):
    return dict(schema_version='candidate_evidence_envelope.v1',evidence_id=stage+'-'+model,
        role='legacy_p17' if model=='p17' else stage,execution_role='quick_projection',
        producer={**ctx['expected_algorithms'][model], 'adapter_version':'1'},
        scope={k:ctx[k] for k in api().SCOPE_KEYS},plan_stage=stage,
        action_set_fingerprint=None if stage=='baseline' else ctx['expected_action_set_fingerprint'],
        baseline_evidence_id=None if stage=='baseline' else 'baseline-'+model,
        m1_rule_fingerprint=ctx['m1_rule_fingerprint'],native_input_fingerprint=payload['input_fingerprint'],
        payload_digest=digest(payload),parent_evidence_ids=[],run_id=stage+'-run',
        provenance_record_id='declared',payload=payload,input_binding=binding)


@pytest.mark.parametrize('model', ['p14','p15'])
def test_native_fingerprint_recipe_not_m3_digest(model):
    ctx=context(); env=native_pair(ctx)[model]
    checks=api()._adapt_native_evidence(env)
    assert not checks['errors'] and not checks['unknowns']
    assert checks['reconstructed_fingerprint']==env['native_input_fingerprint']
    env['native_input_fingerprint']=digest(env['input_binding']['fingerprint_input'])
    assert 'native_fingerprint_mismatch' in codes(api()._adapt_native_evidence(env))


def test_joint_binding_rejects_single_action_evidence():
    ctx=context(); env=native_pair(ctx)['p14']
    env['action_set_fingerprint']=api().candidate_action_set_fingerprint(actions=actions()[:1])
    assert 'scope_mismatch' in codes(api()._validate_evidence(env,ctx,{}))


def test_baseline_label_cannot_prove_post_plan():
    ctx=context(); env=native_pair(ctx,'baseline')['p14']
    env['role']='post_plan'
    assert 'baseline_post_plan_conflict' in codes(api()._validate_evidence(env,ctx,{}))


def test_provenance_self_signed_json_not_trusted():
    ctx=context(); env=native_pair(ctx)['p14']; env['trusted']=True
    result=api()._validate_evidence(env,ctx,{'declared':dict(trusted=True,payload_digest=env['payload_digest'])})
    assert result['provenance_status']=='unverified_declared'
    assert 'source_authenticity_unverified' in codes(result)


@pytest.mark.parametrize('change', ['payload','layer','version','material','schema'])
def test_native_evidence_boundaries(change):
    ctx=context(); env=native_pair(ctx)['p14']
    if change=='payload': env['payload']['routes'][0]['route_length_m']=101
    if change=='layer': env['scope']['operating_layer_id']='other'
    if change=='version': env['producer']['algorithm_version']='999'
    if change=='material': env['input_binding']={}
    if change=='schema': del env['schema_version']
    result=api()._validate_evidence(env,ctx,{})
    assert result['errors'] or result['unknowns']
    if change in ('payload','layer'): assert result['errors']
# T08--T11: use the existing P17/M2 pure models, never production services.
def p17_envelope(ctx, snap, pair=None, seconds=2, cause='service_deficit'):
    from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
    from test_continuous_service_acceptability import _route
    pair=deepcopy(pair or native_pair(ctx))
    row=_route(length_m=100, gap_causes=(cause,))
    row['route_id']=ctx['route_id']
    speed=snap['frozen_inputs']['aircraft_profile']['cruise_speed_mps']
    for sub in row['subsystems']:
        for segment in sub.get('continuous_deficit_segments',[]):
            segment.update(route_id=ctx['route_id'],start_route_offset_m=0,
                end_route_offset_m=seconds*speed,length_m=seconds*speed)
    gap=pair['p15']['payload']; gap['routes']=[row]
    gmat=pair['p15']['input_binding']['fingerprint_input']
    gap['input_fingerprint']=digest(gmat,True)
    # P15 native input identity does not certify this synthetic manually prepared output.
    pair['p15']['native_input_fingerprint']=gap['input_fingerprint']
    pair['p15']['payload_digest']=digest(gap)
    base=native_pair(ctx,'baseline')
    frozen=snap['frozen_inputs']
    inputs=dict(corridor_assessment=base['p14']['payload'],corridor_gap_assessment=base['p15']['payload'],
        site_plan={},aircraft_profile=frozen['aircraft_profile'],planning_evidence=frozen['planning_evidence'],
        operation_scenario=frozen['operation_scenario'],continuous_service_policy=frozen['continuous_service_policy'],
        device_catalog={},radar_surveillance_layout={},post_plan_projection=dict(available=True,
            corridor_assessment=pair['p14']['payload'],corridor_gap_assessment=gap,site_plan={},
            applied_action_ids=[r['action_id'] for r in actions()]))
    payload=ContinuousServiceAcceptabilityV1().evaluate(**inputs)
    env=envelope(ctx,'p17',payload,dict(evaluation_inputs=inputs))
    return env,pair


def rtk_inputs(snap, pair, *, outage=False, managed=False):
    from test_rtk_joint_acceptance import segment, backup
    from cns_planner.application.rtk_joint_acceptance import adapt_p14_p15_rtk_segments
    row=segment(delivery_status='confirmed_deficit' if outage else 'satisfied')
    service={k:v for k,v in row.items() if k not in ('start_m','end_m','evidence_status','source_fingerprint')}
    service.update(service_key='N:rtk_augmentation',status='confirmed_deficit' if outage else 'satisfied')
    voxel=dict(voxel_id='v1',altitude_layer_id='L100',nearest_route_offset_m=50,
        cell_half_diagonal_m=50,subsystems=[dict(subsystem='N',service_redundancy=[service])])
    p14=pair['p14']; p14['payload']['routes'][0]['voxels']=[voxel]
    p14['payload_digest']=digest(p14['payload'])
    p15=pair['p15']; mat=p15['input_binding']['fingerprint_input']; mat['corridor_assessment']=p14['payload']
    p15['payload']['input_fingerprint']=digest(mat,True)
    p15['native_input_fingerprint']=p15['payload']['input_fingerprint']; p15['payload_digest']=digest(p15['payload'])
    fp=p14['payload_digest']
    segments=adapt_p14_p15_rtk_segments(route_id=snap['route_id'],route_length_m=100,
        assessed_voxels=[voxel],source_fingerprint=fp)
    return dict(schema_version='candidate_rtk_joint_inputs.v1',route_length_m=100,segments=segments,
        speed_profile=[dict(start_m=0,end_m=100,speed_mps=10,confirmed=True,source_id='synthetic_fixture',version='1')],
        backup_scenario=backup() if managed else None,synthetic_fixture=True,service_evidence_track='synthetic_fixture',
        segment_binding=dict(parent_p14_evidence_id=p14['evidence_id'],parent_p15_evidence_id=p15['evidence_id'],
            adapter_id='rtk_p14_voxels_v1',adapter_version='1',source_fingerprint=fp,segments_digest=digest(segments)))


@pytest.mark.parametrize('outage,managed,status', [(False,False,'satisfied'),(True,True,'managed'),(True,False,'unknown')])
def test_rtk_normal_and_managed_keep_actual_status(outage,managed,status):
    snap=snapshot(); ctx=context(snap); pair=native_pair(ctx)
    data=rtk_inputs(snap,pair,outage=outage,managed=managed)
    result=api()._evaluate_rtk(snap,data,pair)
    assert result['result']['navigation_status']==status
    assert result['result']['rtk_status']==('rtk_degraded_or_outage' if outage else 'rtk_normal')
    assert not result['errors']
    if managed: assert result['result']['gaps'][0]['navigation_evaluation']['management_basis']=='managed_under_simulation'


def test_rtk_arbitrary_source_fingerprint_rejected():
    snap=snapshot(); pair=native_pair(context(snap)); data=rtk_inputs(snap,pair)
    data['segments'][0]['source_fingerprint']='arbitrary'
    data['segment_binding']['segments_digest']=digest(data['segments'])
    assert 'rtk_segment_reconstruction_mismatch' in codes(api()._evaluate_rtk(snap,data,pair))


@pytest.mark.parametrize('seconds,cause,expected', [(3,'service_deficit','managed'),
    (3.1,'service_deficit','unacceptable'),(8,'redundancy_deficit','managed'),
    (10.1,'redundancy_deficit','unacceptable')])
def test_p17_c_outage_and_degradation_limits_preserved(seconds,cause,expected):
    snap=snapshot(); ctx=context(snap); env,_=p17_envelope(ctx,snap,seconds=seconds,cause=cause)
    assert not api()._adapt_native_evidence(env)['errors']
    view=api()._extract_service_events(env['payload'],snap)
    c=next(r for r in view['service_assessments'] if r['service_key']=='C:communication')
    assert c['status']==expected
    event=c['events'][0]
    assert event['native_event']['duration_s']==seconds
    assert event['threshold']['value']==(3 if cause=='service_deficit' else 10)
    assert event['threshold_source']['source']=='fixture-policy'


def test_service_aggregate_cannot_prove_rid_radar():
    snap=snapshot(); env,_=p17_envelope(context(snap),snap)
    view=api()._extract_service_events(env['payload'],snap)
    for key in ('S:rid_cooperative','S:radar_noncooperative'):
        assert next(r for r in view['service_assessments'] if r['service_key']==key)['status']=='unknown'
    assert view['legacy_route_details'][0]['limitations']==env['payload']['routes'][0]['limitations']

def request(*, outage=False, managed=False):
    snap=snapshot(); ctx=context(snap); pair=native_pair(ctx)
    rtk=rtk_inputs(snap,pair,outage=outage,managed=managed)
    p17,pair=p17_envelope(ctx,snap,pair=pair)
    p17['baseline_evidence_id']='baseline-p14'
    return dict(frozen_rule_snapshot=snap,candidate_context=ctx,actions=actions(),
        baseline_evidence=native_pair(ctx,'baseline'),post_plan_evidence=pair,
        legacy_p17_evidence=p17,rtk_joint_inputs=rtk,evidence_registry={},
        research_review_registry=None,assessment_scope=dict(schema_version='candidate_ue_scope.v1',
            scope_id='fixture-engineering',version='1',kind='complete_required_cns',
            included_constraint_ids=['C:communication'],excluded_constraints=[],source_ref='synthetic_fixture'))


def test_p17_native_projection_binding_is_reconstructable():
    data=request(); result=api()._validate_evidence(data['legacy_p17_evidence'],data['candidate_context'],{})
    assert not result['errors']
    assert {r['reason_code'] for r in result['unknowns']}=={'source_authenticity_unverified'}


def test_scope_keeps_unconsumed_max_gap():
    data=request(); result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['assessment_status'] in ('unknown','not_evaluated')
    assert any(x.get('field')=='communication.max_gap_m' and x.get('value')==1000
        for x in result['unconsumed_requirements'])
    assert not result['final_research_admission']['eligible']


def test_unconfirmed_ue_separate_from_engineering_scope():
    snap=snapshot()
    result=api()._aggregate_assessment(identity={'errors':[],'unknowns':[]},
        services={'statuses':['satisfied'],'violations':[],'unknowns':[],'has_evaluation':True},
        scope={'kind':'confirmed_engineering_model','unconfirmed_ue':True},
        provenance={'authenticated':True,'uses_simulation':False})
    assert result['status']=='engineering_model_acceptable'
    data=request(); assessment=api().evaluate_candidate_ue_acceptability(**data)
    assert assessment['formal_ue_status']=='unresolved'
    assert assessment['ue_event_confirmations']==snap['events']
    assert all(x['confirmed'] is False for x in assessment['ue_event_confirmations'])


@pytest.mark.parametrize('simulation,status', [(False,'engineering_model_acceptable'),
    (True,'conditionally_acceptable_under_simulation')])
def test_aggregate_model_branches_are_not_authentication(simulation,status):
    result=api()._aggregate_assessment(identity={'errors':[],'unknowns':[]},
        services={'statuses':['managed'],'violations':[],'unknowns':[],'has_evaluation':True},
        scope={'kind':'confirmed_engineering_model'},
        provenance={'authenticated':True,'uses_simulation':simulation})
    assert result['status']==status


def test_fixture_relabel_cannot_enter_research():
    data=request(); result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['validation_status']=='illustrative_not_validated'
    assert result['prefilter_status']=='fixture_only'
    assert not result['research_admission']['eligible']
    data['candidate_context']['evidence_track']='engineering_research'
    data['rtk_joint_inputs']['synthetic_fixture']=False
    data['rtk_joint_inputs']['service_evidence_track']='engineering_research'
    changed=api().evaluate_candidate_ue_acceptability(**data)
    assert changed['validation_status']=='illustrative_not_validated'
    assert not changed['final_research_admission']['eligible']


def test_literature_self_review_not_trusted():
    data=request(outage=True,managed=True)
    data['research_review_registry']={'review':dict(review_id='review',reviewer_ref='independent-person',
        decision='approved_for_scope',trusted=True)}
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert 'research_review_authenticity_unverified' in {x['reason_code'] for x in result['unknowns']}
    assert not result['final_research_admission']['eligible']


def test_fc30_integrity_missing_stays_unknown():
    result=api().evaluate_candidate_ue_acceptability(**request(outage=True,managed=True))
    assert result['rtk_joint_result']['real_aircraft_evidence']['status']=='missing'
    assert result['real_aircraft_evidence']['status']=='missing'


def test_rtk_unknown_survives_legacy_nominal():
    data=request(outage=True)
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['rtk_joint_result']['navigation_status']=='unknown'
    assert result['legacy_p17_result']==data['legacy_p17_evidence']['payload']
    assert result['engineering_research_result']['status'] not in (
        'engineering_model_acceptable','conditionally_acceptable_under_simulation')


def test_legacy_violation_survives_rtk_managed():
    data=request(outage=True,managed=True)
    p17,pair=p17_envelope(data['candidate_context'],data['frozen_rule_snapshot'],
        pair=data['post_plan_evidence'],seconds=4)
    p17['baseline_evidence_id']='baseline-p14'
    data.update(legacy_p17_evidence=p17,post_plan_evidence=pair)
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['rtk_joint_result']['navigation_status']=='managed'
    assert result['violations']
    assert not result['research_admission']['eligible']


def test_empty_action_set_still_needs_post_plan():
    data=request(); data['actions']=[]
    data['candidate_context']['expected_action_set_fingerprint']=api().candidate_action_set_fingerprint(actions=[])
    data.update(post_plan_evidence=None,legacy_p17_evidence=None,rtk_joint_inputs=None)
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['assessment_status']=='not_evaluated'
    assert 'post_plan_p17_missing' in result['reason_codes']
# T17--T19: equality never proves independent execution.
def resign(assessment):
    assessment['assessment_fingerprint']=digest({k:v for k,v in assessment.items() if k!='assessment_fingerprint'})
    return assessment


def test_equal_payload_without_execution_proof():
    first=request(); second=deepcopy(first)
    for group in (second['baseline_evidence'],second['post_plan_evidence']):
        for env in group.values(): env.update(run_id='independent-run',execution_role='independent_replay')
    second['legacy_p17_evidence'].update(run_id='independent-run',execution_role='independent_replay')
    quick=api().evaluate_candidate_ue_acceptability(**first)
    final=api().evaluate_candidate_ue_acceptability(**second)
    result=api().compare_candidate_final_verification(quick_assessment=quick,final_assessment=final)
    assert result['content_comparison_status']=='equal'
    assert result['verification_status']=='not_verified'
    assert not result['research_admission_allowed']


@pytest.mark.parametrize('change', ['event_reason','threshold_source','rtk_segment','version','unknown','fingerprint'])
def test_same_status_different_event_is_mismatch(change):
    quick=api().evaluate_candidate_ue_acceptability(**request()); final=deepcopy(quick)
    if change=='event_reason': final['event_assessments'][0]['native_event']['basis']='different cause'
    if change=='threshold_source': final['event_assessments'][0]['threshold_source']['source']='other'
    if change=='rtk_segment': final['rtk_joint_result']['segments'][0]['end_m']=99
    if change=='version': final['algorithm_version']='999'
    if change=='unknown': final['unknowns'].append(dict(path='changed',reason_code='extra'))
    if change=='fingerprint': final['rtk_joint_fingerprint']='changed'
    resign(final)
    result=api().compare_candidate_final_verification(quick_assessment=quick,final_assessment=final)
    assert result['verification_status']=='verification_mismatch'
    assert result['differences']
    assert not result['research_admission_allowed']


def test_equal_unknown_results_never_final_admit():
    quick=api().evaluate_candidate_ue_acceptability(**request())
    quick['assessment_status']='unknown'; resign(quick)
    final=deepcopy(quick); final['independence_verified']=True; resign(final)
    result=api().compare_candidate_final_verification(quick_assessment=quick,final_assessment=final)
    assert result['verification_status']!='verified_for_this_candidate'
    assert not result['research_admission_allowed']


def test_verification_checks_output_integrity_and_missing_content():
    quick=api().evaluate_candidate_ue_acceptability(**request()); final=deepcopy(quick)
    final['assessment_status']='engineering_model_acceptable'
    result=api().compare_candidate_final_verification(quick_assessment=quick,final_assessment=final)
    assert result['verification_status']=='verification_mismatch'
    assert 'assessment_fingerprint_mismatch' in result['blocking_reason_codes']
    result=api().compare_candidate_final_verification(quick_assessment={},final_assessment={})
    assert result['content_comparison_status']=='incomplete'
    assert result['verification_status']=='not_verified'


def test_verification_duplicate_event_key_is_not_overwritten():
    quick=api().evaluate_candidate_ue_acceptability(**request()); final=deepcopy(quick)
    final['event_assessments'].append(deepcopy(final['event_assessments'][0])); resign(final)
    result=api().compare_candidate_final_verification(quick_assessment=quick,final_assessment=final)
    assert result['verification_status']=='verification_mismatch'
    assert any(d['reason_code']=='duplicate_event_key' for d in result['differences'])
# T20 plus cross-cutting read-only and complete-action regressions.
def test_assessment_is_read_only_proposal(monkeypatch):
    import builtins
    import socket
    from cns_planner.application.continuous_service_service import ContinuousServiceService
    data=request(outage=True,managed=True); before=deepcopy(data)
    evaluate=api().evaluate_candidate_ue_acceptability
    def forbidden(*args,**kwargs): raise AssertionError('No I/O, writer or upstream compute allowed')
    with monkeypatch.context() as patch:
        patch.setattr(builtins,'open',forbidden)
        patch.setattr(socket,'socket',forbidden)
        patch.setattr(ContinuousServiceService,'evaluate',forbidden)
        patch.setattr(ContinuousServiceService,'evaluate_actions',forbidden)
        patch.setattr(ContinuousServiceService,'compute_inputs',forbidden)
        result=evaluate(**data)
    assert data==before
    assert result==evaluate(**data)
    assert result['maturity']=='proposal' and result['persisted_to_project_state'] is False
    assert not result['research_admission']['eligible']
    assert not result['final_research_admission']['eligible']


def test_precomputed_joint_evidence_not_sum_of_singles():
    joint=request(); bad=request()
    bad['post_plan_evidence']['p14']['action_set_fingerprint']=api().candidate_action_set_fingerprint(actions=actions()[:1])
    result=api().evaluate_candidate_ue_acceptability(**bad)
    assert result['assessment_status']=='input_inconsistent'
    good=api().evaluate_candidate_ue_acceptability(**joint)
    assert good['assessment_status']!='input_inconsistent'
    c=next(r for r in good['service_assessments'] if r['service_key']=='C:communication')
    assert c['status']=='managed'
    assert good['rtk_joint_result']['rtk_status']=='rtk_normal'
    assert good['validation_status']=='illustrative_not_validated'


def test_changed_m1_source_rejects_candidate_even_with_old_context():
    data=request(); data['frozen_rule_snapshot']['rules'][0]['source']='changed'
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert result['assessment_status']=='input_inconsistent'


def test_missing_upstream_and_wrong_phase_fail_closed():
    data=request(); data['post_plan_evidence']=None
    result=api().evaluate_candidate_ue_acceptability(**data)
    assert not result['research_admission']['eligible']
    data=request(); data['post_plan_evidence']['p15']['scope']['operating_phase']='approach'
    assert api().evaluate_candidate_ue_acceptability(**data)['assessment_status']=='input_inconsistent'


def test_p17_threshold_equal_but_rule_source_changed_is_inconsistent():
    data=request(); inputs=data['legacy_p17_evidence']['input_binding']['evaluation_inputs']
    inputs['continuous_service_policy']=deepcopy(inputs['continuous_service_policy'])
    inputs['continuous_service_policy']['source']='another-source'
    # P17's native fingerprint omits policy source. M3 must still reject it.
    assert 'm1_p17_source_mismatch' in api().evaluate_candidate_ue_acceptability(**data)['reason_codes']

def test_action_set_real_p16_physical_identity_fields():
    rows=actions()
    rows[0].update(action_type='add_device_to_explicit_site',site_id=None,
        facility_id='existing-a',distinct_site_id='facility:existing-a')
    assert api().candidate_action_set_fingerprint(actions=rows)
    other=deepcopy(rows); other[1]['distinct_site_id']='facility:existing-a'
    other[1]['device_id']=other[0]['device_id']
    with pytest.raises(ValueError): api().candidate_action_set_fingerprint(actions=other)


def test_scope_identity_does_not_equate_bool_and_numeric_zero():
    result=dict(errors=[],unknowns=[],checks=[])
    api()._expect(result,'scope.height',False,0)
    assert result['errors']

@pytest.mark.parametrize('native_status', ['fully_satisfied', 'unacceptable'])
def test_service_name_alone_does_not_prove_rid_consumer(native_status):
    snap=snapshot(); env,_=p17_envelope(context(snap),snap)
    sub=next(r for r in env['payload']['routes'][0]['subsystems'] if r['subsystem']=='S')
    sub.update(service='S:rid_cooperative',status=native_status)
    view=api()._extract_service_events(env['payload'],snap)
    rid=next(r for r in view['service_assessments'] if r['service_key']=='S:rid_cooperative')
    assert rid['status']=='unknown'
    assert rid['native_result']['status']==native_status
    if native_status=='unacceptable': assert view['violations']