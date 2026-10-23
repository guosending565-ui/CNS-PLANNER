"""C-batch composition contracts; no planner execution or production state."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from cns_planner.tasks import task_specs as specs
from cns_planner.application.cns_shared_site_candidate_service import CNSSharedSiteCandidateService
from test_radar_surveillance_layout import _service, ROUTE_ID


def test_composition_shares_candidates_and_diagnostics(tmp_path):
    workflow, provider = _service(tmp_path)
    radar = workflow.radar_surveillance_layout_service
    assert radar.shared_sites is workflow.cns_shared_site_candidate_service
    assert workflow.corridor_site_planning_service.shared_site_candidates is radar.shared_sites
    assert workflow.radar_terrain_los_diagnostics_service.radar_layout_service is radar


def test_shared_towers_keep_main_explicit_planning_origin(tmp_path):
    workflow, provider = _service(tmp_path)
    radar = workflow.radar_surveillance_layout_service
    workflow.state['towers']['crs'] = {'source_crs': {'value': 'EPSG:4326', 'confirmed': True}}
    policy = deepcopy(radar.policy_snapshot())
    policy['radar_planning_origin_policy_id'] = 'radar_all_tower_rooftop_estimate_v1'
    # Inspect preparation only; no solve needed.
    result = radar._radar_site_inputs(policy)
    assert result['sites']
    assert all(item['installation_position'] == 'tower_top' for item in result['sites'])
    assert all(item['origin_egm2008_m'] == 57.0 for item in result['resolved']['records'])
    assert all(item['origin_confirmed'] is False for item in result['resolved']['records'])


@pytest.mark.parametrize('runner,module_name,class_name,keyword', [
    (specs._radar_runner, 'radar_surveillance_layout_service', 'RadarSurveillanceLayoutService', 'shared_sites'),
    (specs._p16_runner, 'corridor_site_planning_service', 'CorridorSitePlanningService', 'shared_site_candidates'),
])
def test_workers_receive_shared_projection_without_running_planner(monkeypatch, runner, module_name, class_name, keyword):
    import importlib
    module = importlib.import_module('cns_planner.application.' + module_name)
    state = {'nodes': [], 'towers': {'items': []}, 'reference_landing_sites': {}}
    class StopBeforeCompute(Exception): pass
    class Spy:
        def __init__(self, *args, **kwargs):
            shared = kwargs[keyword]
            assert isinstance(shared, CNSSharedSiteCandidateService)
            assert shared.snapshot()['status'] == 'passed'
            assert args[0].state == state
            raise StopBeforeCompute()
    monkeypatch.setattr(module, class_name, Spy)
    context = SimpleNamespace(inputs={'snapshot': {'inputs': {}}}, project_state=state,
        check_cancel=lambda: None, progress=lambda *args: None,
        algorithms=lambda: SimpleNamespace(resolve_all=lambda *args: None, create=lambda *args: object()))
    with pytest.raises(StopBeforeCompute): runner(context)


@pytest.mark.parametrize('key', ['nodes', 'reference_landing_sites', 'existing_cns_facilities', 'tower_colocation_candidates', 'tower_colocation_policy'])
def test_radar_freezes_shared_source_changes(key):
    before = specs._radar_inputs({}, {})
    after = specs._radar_inputs({key: {'changed': True}}, {})
    assert before != after


def test_pcf_submission_rejects_stale_attributes():
    with pytest.raises(ValueError, match='失效'):
        specs._pcf_plan({'grid_attributes': {'population': {'status': 'stale'}}}, {})


def test_pending_installation_policy_is_consumed_without_state_write(tmp_path):
    workflow, _ = _service(tmp_path)
    radar = workflow.radar_surveillance_layout_service
    before = deepcopy(workflow.state)
    evidence = {'installation_height_m': 5.0, 'source': 'user_confirmed', 'confirmed': True}
    assert radar._radar_installation_evidence({'ground_site_installation_heights': {'N1': evidence}})['N1'] == evidence
    assert workflow.state == before


@pytest.mark.parametrize('kind', ['nodes', 'existing_cns_facilities'])
@pytest.mark.parametrize('field,value', [('installation_height_m', 8.0), ('confirmed', False), ('source', 'engineering_assumption')])
def test_installation_edits_change_service_input_fingerprint(tmp_path, kind, field, value):
    workflow, _ = _service(tmp_path)
    block = {'installation_height_m': 5.0, 'source': 'user_confirmed', 'confirmed': True}
    if kind == 'nodes':
        workflow.state[kind] = [{'node_id': 'N1', 'coordinate': [122.0, 30.0], 'radar_installation': block}]
    else:
        workflow.state[kind] = {'items': [{'facility_id': 'F1', 'radar_installation': block}]}
    radar = workflow.radar_surveillance_layout_service
    before = radar.input_fingerprint(ROUTE_ID)
    block[field] = value
    assert radar.input_fingerprint(ROUTE_ID) != before
