import copy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.application.navigation_integrity_research_service import evaluate_navigation_integrity_research
from cns_planner.domain.navigation_middle_reuse import POLICY_ID, POLICY_CONTRACT, segment_witness

ROOT = Path(__file__).resolve().parent / 'fixtures' / 'navigation_middle_reuse'
def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))

def inputs():
    n = read('endpoint_input.json')
    return dict(required_cns=read('endpoint_context.json')['requirements'], route=n['route'],
                facilities=n['facilities'], selected_sites=read('stations.json'), site_pool=read('sites.json'),
                middle_bindings=read('middle_bindings.json'))

SELECTION = dict(algorithm_type='site_planner', algorithm_id='navigation_integrity_endpoint_middle_reuse_v1', version='1.0', parameters={'middle_segment_indices':[3,4]})

def run(**changes):
    data=inputs(); data.update(changes)
    return evaluate_navigation_integrity_research(**data, selection=SELECTION)

def test_registry_and_contract():
    registry=build_default_algorithm_registry(None)
    p=registry.create('site_planner', POLICY_ID, '1.0')
    assert p.algorithm_id == POLICY_ID
    assert POLICY_CONTRACT['middle_monitor_count']==2
    assert POLICY_CONTRACT['local_monitoring_radius_m']==1000.0
    with pytest.raises(ValueError): registry.create('site_planner', POLICY_ID, '2.0')

def test_research_strategy_cannot_replace_canonical_P16():
    from cns_planner.algorithms.registry import normalize_algorithm_selection
    with pytest.raises(ValueError,match='研究入口'):
        normalize_algorithm_selection({'site_planner':SELECTION})

def test_default_is_exact_legacy_endpoint_evidence():
    from cns_planner.domain.navigation_integrity_monitoring import build_navigation_integrity_endpoint_evidence
    data=inputs()
    expected=build_navigation_integrity_endpoint_evidence(data['required_cns'],[data['route']], existing_facilities=data['facilities'])
    assert evaluate_navigation_integrity_research(**data)==expected

def test_nm1_reproduces_through_registry():
    data=inputs(); before=copy.deepcopy(data)
    result=evaluate_navigation_integrity_research(**data,selection=SELECTION)
    assert data==before
    assert result['planning_status']=='satisfied'
    assert result['actual_delivery']=='unknown'
    assert result['new_contract_lexicographic_optimality']=='not_proved'
    assert result['counts']==dict(physical_sites=7,new_physical_sites=2,incremental_physical_sites=0,C=7,RID=7,N=4,total_devices=18)
    endpoints=result['endpoint_evidence']['routes'][0]['endpoints']
    assert endpoints['origin']['selected_installed_providers'][0]['provider_id'].endswith('672')
    assert endpoints['destination']['selected_installed_providers'][0]['provider_id'].endswith('1628')
    assert {(a['site_id'],a['segment_index']) for a in result['selected_actions']}=={
        ('NEW-425000-3301500',3),('NEW-422750-3300500',4)}
    assert all(a['native_evidence']['planning_status']=='satisfied' and a['native_evidence']['delivery_status']=='unknown' for a in result['selected_actions'])
    assert result['contract']['continuous_route_coverage_required'] is False

def test_other_route_not_hardcoded_and_fingerprint_changes():
    data=inputs(); result=run()
    data['route']['route_id']='ANOTHER'
    data['required_cns']['route_overrides']['ANOTHER']=copy.deepcopy(data['required_cns']['route_overrides']['R0005'])
    renamed=evaluate_navigation_integrity_research(**data,selection=SELECTION)
    assert renamed['planning_status']=='satisfied'
    assert all(a['route_id']=='ANOTHER' for a in renamed['selected_actions'])
    assert result['input_fingerprint']!=renamed['input_fingerprint']

def test_device_names_are_not_site_identity():
    data=inputs()
    for i,f in enumerate(data['facilities']['items']):
        for j,d in enumerate(f.get('devices',[])): d['device_id']=f'NAVINT-real-{i}-{j}'
    assert evaluate_navigation_integrity_research(**data,selection=SELECTION)['planning_status']=='satisfied'

def test_endpoint_station_table_must_agree_with_facility_N_devices():
    data=inputs()
    for s in data['selected_sites']:
        if s['N_roles']: s['devices'].remove('N')
    assert evaluate_navigation_integrity_research(**data,selection=SELECTION)['planning_status']!='satisfied'

@pytest.mark.parametrize('mutation',['no_C','shared_identity','unqualified','no_source','missing_coordinate'])
def test_rejects_ineligible_middle_hosts(mutation):
    data=inputs()
    for s in data['selected_sites']:
        if not s['new']: continue
        if mutation=='no_C': s['devices'].remove('C')
        if mutation=='shared_identity': s['physical_group']=next(x['physical_group'] for x in data['selected_sites'] if x['N_roles'])
        if mutation=='missing_coordinate': s['coordinate']=None
    if mutation in ('unqualified','no_source'):
        for p in data['site_pool']:
            if p['new']:
                if mutation=='unqualified': p['eligibility']='rejected'
                else: p['identity_refs']=[]; p.pop('source_record',None); p.pop('host_kind',None)
    result=evaluate_navigation_integrity_research(**data,selection=SELECTION)
    assert result['planning_status']!='satisfied'

def test_no_reuse_of_one_internal_segment():
    route=inputs()['route']; route['path']=route['path'][:4]
    assert run(route=route)['planning_status']!='satisfied'

@pytest.mark.parametrize('parameters',[{'local_monitoring_radius_m':1001},{'typo':True}])
def test_parameters_cannot_silently_change_contract(parameters):
    selection=dict(SELECTION,parameters=parameters)
    with pytest.raises(ValueError): evaluate_navigation_integrity_research(**inputs(),selection=selection)

def test_witness_inside_segment_and_distance_native():
    from cns_planner.domain.navigation_integrity_contract_v2 import horizontal_distance_m
    w=segment_witness([0.5,0.001],[0,0],[1,0])
    assert abs(w[1])<1e-10 and abs(w[0]-0.5)<1e-10
    assert horizontal_distance_m(w,[0.5,0.001])<1000

def test_invalid_endpoint_contract_cannot_satisfy_new_policy():
    data=inputs()
    for block in [data['required_cns']['project_default'],*data['required_cns']['route_overrides'].values()]:
        for service in block.get('navigation',{}).get('services',{}).values():
            service['planning']['local_monitoring_radius_m']=10000
    with pytest.raises(ValueError): evaluate_navigation_integrity_research(**data,selection=SELECTION)

def test_unbound_search_consumes_other_hosts_without_scenario_constants():
    data=inputs(); data['middle_bindings']=None
    selection=dict(SELECTION,parameters={})
    result=evaluate_navigation_integrity_research(**data,selection=selection)
    assert result['planning_status']=='satisfied'
    assert result['counts']['incremental_physical_sites']==0
    assert len({a['distinct_site_id'] for a in result['selected_actions']})==2
    assert len({a['segment_index'] for a in result['selected_actions']})==2

@pytest.mark.parametrize('path',[[[0,0],[1,0]],[[0,0],[1,0],[2,0]]])
def test_insufficient_or_invalid_route_never_satisfied(path):
    route=inputs()['route']; route['path']=path
    assert run(route=route)['planning_status']!='satisfied'

def test_invalid_route_coordinate_rejected_explicitly():
    route=inputs()['route']; route['path'][2]=[float('nan'),0]
    with pytest.raises(ValueError,match='航路'):
        run(route=route)

@pytest.mark.parametrize('indices',[[1,1],[0,2],[True,2],[1],[-1,3]])
def test_invalid_segment_configuration(indices):
    with pytest.raises(ValueError):
        evaluate_navigation_integrity_research(**inputs(),selection=dict(SELECTION,parameters={'middle_segment_indices':indices}))
