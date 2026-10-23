from copy import deepcopy
import pytest
from cns_planner.application.operational_route_clearance import revalidate_existing_route, fingerprint
from cns_planner.risk.operational_route_profile import CONNECTOR_SEMANTICS


def inputs():
    path = [[0, 0], [1, 1], [2, 0]]
    state = {"operational_routes": [{"route_id": "R0005", "path": path,
        "provenance": {"operational_authorization": False}}], "spatial_3d": {
        "altitude_layers": [{"altitude_layer_id": "ALT-100", "nominal_altitude_m": 100,
                            "vertical_reference": "egm2008_orthometric", "confirmed": True, "status": "confirmed"}]}}
    kwargs = dict(route_id="R0005", altitude_layer_id="ALT-100", expected_geometry_fingerprint=fingerprint(path),
        evidence={"metric_path": path, "terrain": {"available": False}, "buildings": {"available": False},
                  "towers": {"status": "unresolved", "towers": []}}, policy={}, source_identity={})
    return state, kwargs


def test_readonly_no_candidate_and_missing_never_pass():
    state, kwargs = inputs(); before = deepcopy(state)
    result = revalidate_existing_route(state, **kwargs)
    assert state == before
    assert all(d['status'] == 'unresolved' for d in result['domains'].values())
    assert not result['operational_authorization']
    assert len(result['subject']['path']) == 3


def test_fingerprint_and_geometry_gate():
    state, kwargs = inputs()
    first = revalidate_existing_route(state, **kwargs)
    kwargs['source_identity'] = {'terrain': 'changed'}
    assert revalidate_existing_route(state, **kwargs)['fingerprint'] != first['fingerprint']
    state['operational_routes'][0]['path'][1] = [5, 5]
    with pytest.raises(ValueError): revalidate_existing_route(state, **kwargs)


def test_middle_vertex_obstacle_is_checked():
    state, kwargs = inputs()
    kwargs['policy'] = {'tower_horizontal_clearance_m': 0, 'tower_vertical_clearance_m': 0}
    kwargs['evidence']['towers'] = {'status': 'passed', 'towers': [{
        'tower_id': 'T', 'point_metric': [1, 1], 'tower_top_orthometric_m': 120, 'confirmed': True}]}
    result = revalidate_existing_route(state, **kwargs)
    assert len(result['domains']['towers']['violations']) == 1
    kwargs['evidence']['towers']['towers'][0]['confirmed'] = False
    result = revalidate_existing_route(state, **kwargs)
    assert len(result['domains']['towers']['unresolved']) == 1


def test_existing_route_risk_segments_use_operational_connector_semantics():
    """R7-D18 回归：D4 风险画像必须能把 segment connector 语义传给生产 profile。

    修复前 ``_route_risk_segment`` 不接受 ``connector_semantics``，只要传入
    ``grid`` + ``grid_risk_v2`` 就会 TypeError，既有航路复验根本跑不完。
    """

    state, kwargs = inputs()
    path = [[0.5, 0.5], [1.5, 0.5], [2.5, 0.5]]
    state['operational_routes'][0]['path'] = path
    kwargs['expected_geometry_fingerprint'] = fingerprint(path)
    kwargs['evidence']['metric_path'] = path
    kwargs['to_geographic'] = lambda point: list(point)
    kwargs['grid'] = {'cells': [
        {'grid_id': 'G0', 'bbox': [0.0, 0.0, 1.0, 1.0], 'center': [0.5, 0.5]},
        {'grid_id': 'G1', 'bbox': [1.0, 0.0, 2.0, 1.0], 'center': [1.5, 0.5]},
        {'grid_id': 'G2', 'bbox': [2.0, 0.0, 3.0, 1.0], 'center': [2.5, 0.5]},
    ]}
    domains = {
        domain_id: {'domain_id': domain_id, 'status': 'passed', 'index': 0.5}
        for domain_id in ('ground', 'air_traffic', 'environment_obstacle')
    }
    kwargs['grid_risk_v2'] = {
        'cells': {grid_id: {'domains': deepcopy(domains)} for grid_id in ('G0', 'G1', 'G2')},
        'policy': {'domains': {}},
    }
    result = revalidate_existing_route(state, **kwargs)
    risk = result['risk']
    assert risk['profile']['segments'], '既有航路风险画像必须产生 segments'
    for segment in risk['profile']['segments']:
        assert segment['provenance']['connector_semantics'] == CONNECTOR_SEMANTICS
    assert result['status'] == 'unknown', '障碍证据缺失时仍必须 fail-closed'
