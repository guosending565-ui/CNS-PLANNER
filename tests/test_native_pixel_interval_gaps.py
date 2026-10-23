import pytest
from cns_planner.route_planner_v3.continuous_raster_window import resolve_native_pixel_intervals


def route(points):
    return {'horizontal_geometry': {'linearized': {'linestring_metric': points}}}


def test_adjacent_pixel_intervals_have_no_unsampled_gap():
    pixels=[{'pixel':[i,0], 'bbox_metric':[i*3.13,-1,(i+1)*3.13,1],
             'center_metric':[(i+.5)*3.13,0], 'elevation_egm2008_m':i,
             'data_status':'passed'} for i in range(10)]
    items=resolve_native_pixel_intervals(route([[0,0],[31.3,0]]),pixels,curve_chord_error_m=0)
    cursor=0
    for p in items:
        assert p['interval']['start_distance_m']<=cursor+1e-9
        cursor=max(cursor,p['interval']['end_distance_m'])
    assert cursor==pytest.approx(31.3)


def test_subsample_pixel_and_tangent_are_preserved():
    p={'pixel':[0,0], 'bbox_metric':[4.2,.5,4.3,1], 'center_metric':[4.25,.75], 'data_status':'unknown'}
    items=resolve_native_pixel_intervals(route([[0,0],[10,0]]),[p],curve_chord_error_m=.5)
    assert items
    assert items[0]['data_status']=='unknown'
    assert items[0]['interval']['start_distance_m']==pytest.approx(4.2)
    assert items[0]['interval']['end_distance_m']==pytest.approx(4.3)


def test_bent_route_does_not_use_endpoint_chord():
    p={'pixel':[0,0],'bbox_metric':[4.9,4.9,5.1,5.1],'center_metric':[5,5]}
    assert resolve_native_pixel_intervals(route([[0,0],[5,5],[10,0]]),[p],curve_chord_error_m=0)


def test_protection_space_detects_terrain_outside_centerline():
    from cns_planner.validation.continuous_validators import MetricRoute, validate_terrain
    r=route([[0,0],[100,0]])
    r['primitives']=[{'distance_start_m':0,'distance_end_m':100,
                     'z_start_egm2008_m':80,'z_end_egm2008_m':80}]
    pixel={'pixel':[0,0],'bbox_metric':[45,18,55,19],'center_metric':[50,18.5],
           'elevation_egm2008_m':90,'data_status':'passed'}
    assert not resolve_native_pixel_intervals(r,[pixel],curve_chord_error_m=0)
    pixels=resolve_native_pixel_intervals(r,[pixel],curve_chord_error_m=20)
    result=validate_terrain(MetricRoute(r),evidence={'pixels':pixels},policy={'terrain_clearance_m':0})
    assert result['status']=='failed'
    assert result['minimum_margin']==pytest.approx(-10)


def test_multiple_visits_keep_disjoint_intervals():
    p={'pixel':[0,0],'bbox_metric':[4,-1,6,1],'center_metric':[5,0]}
    items=resolve_native_pixel_intervals(route([[0,0],[10,0],[10,10],[0,10],[0,0],[10,0]]),[p],curve_chord_error_m=0)
    assert len(items)==2
    assert items[1]['interval']['start_distance_m']>items[0]['interval']['end_distance_m']
