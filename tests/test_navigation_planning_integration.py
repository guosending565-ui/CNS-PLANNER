import copy
from types import SimpleNamespace
from pathlib import Path
import pytest
from test_navigation_middle_reuse_v1 import inputs, SELECTION
from cns_planner.application.navigation_planning_integration import (
    read_navigation_planning_config, evaluate_navigation_planning,
    dispatch_middle_actions,
)

def config(**kw):
    return dict(selection=copy.deepcopy(SELECTION), middle_bindings=inputs()['middle_bindings'], **kw)

def evaluate(**kw):
    data=inputs(); data.pop('middle_bindings')
    return evaluate_navigation_planning(**data, planning_config=config(), **kw)

def test_default_exact_v2():
    from cns_planner.application.navigation_integrity_research_service import evaluate_navigation_integrity_research
    data=inputs(); data.pop('middle_bindings')
    assert evaluate_navigation_planning(**data)==evaluate_navigation_integrity_research(**data)

def test_config_validates_version_and_unknown_fields():
    assert read_navigation_planning_config({'navigation_integrity_planning':config()})==config()
    bad=config(); bad['selection']['version']='2.0'
    with pytest.raises(ValueError): read_navigation_planning_config({'navigation_integrity_planning':bad})
    with pytest.raises(ValueError): read_navigation_planning_config({'navigation_integrity_planning':dict(config(), typo=True)})

def test_portable_config_file_and_inputs_are_read_only():
    path=Path(__file__).resolve().parents[1]/'config/navigation_integrity/planning.example.json'
    cfg=read_navigation_planning_config(path)
    data=inputs(); data.pop('middle_bindings'); original=copy.deepcopy(data)
    r=evaluate_navigation_planning(**data,planning_config=cfg)
    assert data==original
    assert r['planning_status']=='satisfied'
    assert r['canonical_published'] is False

def test_four_roles_residual_and_whatif():
    result=evaluate()
    assert result['counts']['N']==4 and result['counts']['total_devices']==18
    assert len(result['P14']['responsibilities'])==4
    assert len(result['P15']['residual_gaps'])==2
    assert len(result['P16']['residual_gaps'])==0
    assert result['P16']['actual_delivery']=='unknown'
    assert result['new_contract_lexicographic_optimality']=='not_proved'
    assert all(not r['installed_providers'] for r in result['P14']['responsibilities'])
    assert all(r['hypothetical_providers'] for r in result['P16']['responsibilities'])
    assert 'selected_installed_providers' not in str(result)

def test_installed_requires_explicit_authority():
    with pytest.raises(ValueError): evaluate(facility_authority='installed')
    result=evaluate(facility_authority='installed',installed_facilities=inputs()['facilities'])
    endpoints=result['P14']['responsibilities'][:2]
    assert all(r['installed_providers'] and not r['hypothetical_providers'] for r in endpoints)

def test_fingerprint_binds_config_and_real_authority():
    a=evaluate(); b=evaluate(facility_authority='installed',installed_facilities=inputs()['facilities'])
    assert a['input_fingerprint']!=b['input_fingerprint']
    data=inputs(); data.pop('middle_bindings'); cfg=config(); cfg['middle_bindings']=None
    assert a['input_fingerprint']!=evaluate_navigation_planning(**data, planning_config=cfg)['input_fingerprint']

def test_dispatch_accumulates_and_rejects_forged_action():
    r=evaluate(); candidates=r['P16']['candidate_actions']; chosen=r['P16']['selected_actions']
    base=r['P14']['responsibilities']; before=copy.deepcopy(base)
    one=dispatch_middle_actions(base,chosen[:1],candidates)
    assert len(one['residual_gaps'])==1
    both=dispatch_middle_actions(base,chosen,candidates)
    assert both['residual_gaps']==[] and base==before
    bad=copy.deepcopy(chosen[0]); bad['distinct_site_id']='forged'
    with pytest.raises(ValueError): dispatch_middle_actions(base,[bad],candidates)
    with pytest.raises(ValueError): dispatch_middle_actions(base,chosen+chosen,candidates)

def test_missing_endpoint_never_satisfied():
    data=inputs(); data.pop('middle_bindings'); data['facilities']={'items':[]}
    r=evaluate_navigation_planning(**data, planning_config=config())
    assert r['planning_status']!='satisfied' and r['P16']['residual_gaps']

def test_invalidation_in_single_authority_only():
    from cns_planner.application.invalidation_service import InvalidationService
    state={k:{'status':'passed'} for k in ('cns_corridor_assessment','cns_corridor_gap_assessment','cns_corridor_site_plan','continuous_service_acceptability')}
    state.update(facilities={'items':[{'id':'real'}]},routes=[{'id':'route'}],radar_surveillance_layout={'status':'passed'})
    original=copy.deepcopy(state)
    service=InvalidationService(SimpleNamespace(state=state))
    service.navigation_integrity_planning_changed()
    assert all(state[k]['status']=='stale' for k in original if k.startswith('cns_corridor'))
    for k in ('facilities','routes','radar_surveillance_layout'): assert state[k]==original[k]

def test_research_and_installed_containers_must_not_be_mixed():
    with pytest.raises(ValueError): evaluate(installed_facilities=inputs()['facilities'])
    result=evaluate(facility_authority='installed',installed_facilities={'items':[]})
    assert all(not r['installed_providers'] for r in result['P14']['responsibilities'])
    assert result['P16']['residual_gaps']

@pytest.mark.parametrize('field',['route','required_cns','facilities','selected_sites','site_pool'])
def test_identity_fingerprint_covers_inputs(field):
    data=inputs(); data.pop('middle_bindings'); before=copy.deepcopy(data)
    original=evaluate_navigation_planning(**data,planning_config=config())
    if field in ('selected_sites','site_pool'): data[field][0]['audit_revision']='changed'
    else: data[field]['audit_revision']='changed'
    changed=evaluate_navigation_planning(**data,planning_config=config())
    assert changed['input_fingerprint']!=original['input_fingerprint']
    assert before!=data

def test_action_types_and_physical_groups_are_enforced():
    r=evaluate(); selected=r['P16']['selected_actions']
    bad=copy.deepcopy(selected); bad[1]['distinct_site_id']=bad[0]['distinct_site_id']
    with pytest.raises(ValueError): dispatch_middle_actions(r['P14']['responsibilities'],bad,bad)
    bad=copy.deepcopy(selected); bad[0]['action_type']='generic_install'
    with pytest.raises(ValueError): dispatch_middle_actions(r['P14']['responsibilities'],bad,bad)

def test_invalidation_downstream_and_frozen_history():
    from cns_planner.application.invalidation_service import InvalidationService
    state={k:{'status':'passed'} for k in ('cns_corridor_assessment','cns_corridor_gap_assessment','cns_corridor_site_plan','continuous_service_acceptability','cns_plan_review')}
    state.update(confirmed_cns_plan={'status':'confirmed','history':[{'id':'freeze'}]},
        cns_planning_reports={'active_report_id':'active','records':[{'report_id':'active'},{'report_id':'frozen'}]})
    calls=[]; service=InvalidationService(SimpleNamespace(state=state))
    service.route_safety_evidence_invalidator=lambda reason:calls.append(reason)
    service.navigation_integrity_planning_changed()
    assert state['continuous_service_acceptability']['status']=='stale'
    assert state['cns_plan_review']['status']=='stale'
    assert state['confirmed_cns_plan']['current_applicability']=='stale'
    assert state['confirmed_cns_plan']['history']==[{'id':'freeze'}]
    assert state['cns_planning_reports']['records'][0]['current_applicability']=='stale_current_project'
    assert state['cns_planning_reports']['records'][1]=={'report_id':'frozen'}
    assert calls==['cns_corridor_changed']

def test_endpoint_station_mismatch_stays_in_residual_after_middle_whatif():
    data=inputs(); data.pop('middle_bindings')
    for site in data['selected_sites']:
        if site['N_roles']: site['devices'].remove('N')
    r=evaluate_navigation_planning(**data,planning_config=config())
    assert r['planning_status']!='satisfied'
    projected=dispatch_middle_actions(r['P14']['responsibilities'],r['P16']['candidate_actions'],r['P16']['candidate_actions'])
    assert len(projected['residual_gaps'])==2
    assert {g['role'] for g in projected['residual_gaps']}=={'origin','destination'}

def test_unbound_partial_pool_can_evaluate_single_middle_action():
    data=inputs(); data.pop('middle_bindings')
    data['site_pool']=[s for s in data['site_pool'] if s['site_id']=='NEW-425000-3301500']
    selection=copy.deepcopy(SELECTION); selection['parameters']={}
    r=evaluate_navigation_planning(**data,planning_config={'selection':selection})
    assert r['planning_status']!='satisfied'
    one=dispatch_middle_actions(r['P14']['responsibilities'],r['P16']['candidate_actions'][:1],r['P16']['candidate_actions'])
    assert len(one['residual_gaps'])==1

@pytest.mark.parametrize('requirements',[{},None,{'navigation':{}},[]])
def test_missing_navigation_contract_has_explicit_error(requirements):
    data=inputs(); data.pop('middle_bindings'); data['required_cns']=requirements
    with pytest.raises(ValueError,match='导航需求'):
        evaluate_navigation_planning(**data,planning_config=config())
