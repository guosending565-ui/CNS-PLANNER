from copy import deepcopy
import pytest
from cns_planner.application.operational_route_clearance import revalidate_existing_route, fingerprint


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
