from copy import deepcopy
import pytest
from cns_planner.application import p16_joint_research as m

def fixture(monkeypatch):
    cat={'items':[{'device_id':'COMM-BASELINE-2KM','enabled':True,'coverage_geometry':{}}]}
    air={'aircraft_id':'one','communication':{'performance':{}}}
    monkeypatch.setattr(m,'device_catalog_with_evidence',lambda s:cat)
    monkeypatch.setattr(m,'aircraft_profile_with_evidence',lambda s:air)
    state={'aircraft_profiles':{'items':[deepcopy(air)]},'device_catalog':cat}
    ad={'validation':{'status':'passed'},'pool':{'sites':[{}]*41,'actions':[{}]*62,'research_scope':{'communication_assumptions':{'radius_m':2000,'interface':'ip','max_latency_s':1}}}}
    return state,ad

def test_private_projection_preserves_real_inputs(monkeypatch):
    state,ad=fixture(monkeypatch);before=deepcopy([state,ad])
    result=m.research_state(state,ad)
    assert [state,ad]==before
    assert result['device_catalog']['items'][0]['service_model']['model_family']=='declared_performance'
    assert result['aircraft_profiles']['items'][0]['communication']['performance']['max_latency_s']==1

@pytest.mark.parametrize('field',['status','count','assumptions'])
def test_rejects_unadmitted_run(monkeypatch,field):
    state,ad=fixture(monkeypatch)
    if field=='status':ad['validation']['status']='blocked'
    if field=='count':ad['pool']['actions'].pop()
    if field=='assumptions':ad['pool']['research_scope']['communication_assumptions']['radius_m']=5000
    with pytest.raises(ValueError):m.research_state(state,ad)


def test_selected_profile_must_exist(monkeypatch):
    state,ad=fixture(monkeypatch)
    state["aircraft_profiles"]["items"]=[]
    with pytest.raises(ValueError):m.research_state(state,ad)


def test_final_gap_is_selected_combination_not_last_probe(monkeypatch):
    from cns_planner.application import corridor_site_planning_service as chain
    state,ad=fixture(monkeypatch)
    monkeypatch.setattr(m,'run_scenario_p16',lambda *a,**kw: {'status':'computed','result':{'selected_actions':[]},'final_gap':{'wrong':'last_probe'}})
    monkeypatch.setattr(chain,'rerun_corridor_chain',lambda *a,**kw: ({},{'correct':'selected'}))
    result=m.run_joint_research(state,ad)
    assert result['final_gap']=={'correct':'selected'}
    assert result['final_gap_origin']=='selected_combination_replay'
