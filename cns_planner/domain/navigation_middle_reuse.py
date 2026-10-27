"""Versioned, caller-owned endpoint plus internal-segment placement contract.

No state writes, GIS dependency, C/RID solving, or global optimality claim.
Segments follow the minor great-circle arc between successive path vertices.
"""
from copy import deepcopy
from itertools import combinations
from math import atan2, cos, degrees, hypot, isfinite, radians, sin, sqrt

from .navigation_integrity_contract_v2 import (
    CONTRACT_VERSION_V2 as NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
    endpoint_record as _v2_endpoint_record,
    horizontal_distance_m,
)
from .route_safety_evidence_v2 import stable_fingerprint

POLICY_ID = 'navigation_integrity_endpoint_middle_reuse_v1'
POLICY_CONTRACT = dict(
    policy_id=POLICY_ID, version='1.0', endpoint_monitor_count_per_role=1,
    middle_monitor_count=2, distinct_internal_segments_required=True,
    distinct_physical_groups_required=4, local_monitoring_radius_m=1000.0,
    association_semantics='exists_horizontal_witness_on_internal_segment',
    segment_geometry='minor_great_circle_arc_between_path_vertices',
    full_segment_coverage_required=False, continuous_route_coverage_required=False,
    local_C_colocation_required=True, prefer_selected_physical_sites=True,
    qualification='conditional_engineering_not_installation_authorization',
)

def _coordinate(p):
    return (isinstance(p,(tuple,list)) and len(p)>=2 and
            all(isinstance(x,(int,float)) and not isinstance(x,bool) and isfinite(x) for x in p[:2])
            and -180<=p[0]<=180 and -90<=p[1]<=90)

def _xyz(p):
    lon,lat=map(radians,p[:2]); return (cos(lat)*cos(lon),cos(lat)*sin(lon),sin(lat))

def _dot(a,b): return sum(x*y for x,y in zip(a,b))

def _cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])

def _angle(a,b): return atan2(sqrt(_dot(_cross(a,b),_cross(a,b))),_dot(a,b))

def segment_witness(point, start, end):
    """Return a legal on-arc witness; reject invalid/ambiguous geometry."""
    if not all(_coordinate(p) for p in (point,start,end)):
        raise ValueError('缺少合法经纬度')
    p,a,b=map(_xyz,(point,start,end)); length=_angle(a,b)
    if length>=3.141592653589793-1e-9:
        raise ValueError('对跖航段无唯一短弧')
    options=[list(start[:2]),list(end[:2])]
    n=_cross(a,b); norm=_dot(n,n)
    if norm>1e-24:
        q=tuple(p[i]-n[i]*_dot(p,n)/norm for i in range(3)); size=sqrt(_dot(q,q))
        if size>1e-12:
            for sign in (1,-1):
                unit=tuple(sign*x/size for x in q)
                if abs(_angle(a,unit)+_angle(unit,b)-length)<1e-10:
                    options.append([degrees(atan2(unit[1],unit[0])),degrees(atan2(unit[2],hypot(unit[0],unit[1])))])
    return min(options,key=lambda w:horizontal_distance_m(point,w))

def middle_candidate_actions(route, selected_sites, site_pool, excluded_groups):
    """Extend the caller's pool only; never synthesize coordinates or site identity."""
    selected={s['site_id']:s for s in selected_sites}
    path=route.get('path') or []
    actions=[]; rejected=[]
    for source in site_pool:
        sid=source.get('site_id'); s=selected.get(sid,source)
        group=s.get('physical_group') or s.get('redundancy_group')
        source_ok=bool(source.get('host_kind')) and (bool(source.get('identity_refs') or source.get('source_record')) or
                   (source.get('host_kind')=='generated_land_grid' and source.get('new') is True and source.get('xy')))
        if (not sid or not group or group in excluded_groups or
            source.get('eligibility') not in ('conditional','eligible') or not source_ok or
            'C' not in (s.get('devices') or []) or not _coordinate(s.get('coordinate'))):
            rejected.append(dict(site_id=sid,reason='identity_qualification_coordinate_or_local_C_unavailable'))
            continue
        for i in range(1,len(path)-2):
            try: witness=segment_witness(s['coordinate'],path[i],path[i+1])
            except ValueError: continue
            distance=horizontal_distance_m(s['coordinate'],witness)
            if distance>1000.0: continue
            provider=dict(provider_id='monitor:'+sid,distinct_site_id=group,coordinate=deepcopy(s['coordinate']),
                          planning_status='installed',integrity_measurement_status='not_measured_not_certified')
            raw=_v2_endpoint_record('origin',f"{route['route_id']}:SEG-{i+1:02d}",witness,[provider],None,
                dict(contract_version=NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2,
                     local_monitoring_radius_m=1000.0,radius_basis='engineering_assumption'))
            # A segment is never registered as a takeoff/landing site. Reuse only
            # the local primitive, expose the association under its own typed fields.
            raw.pop('takeoff_landing_site_id'); raw.pop('endpoint_role')
            actions.append(dict(action_id=f"{POLICY_ID}:{route['route_id']}:{sid}:{i}",
                action_type='add_navigation_integrity_middle_monitor',planner_family=POLICY_ID,
                service_key='N:navigation_integrity_monitoring',route_id=route['route_id'],
                site_id=sid,distinct_site_id=group,coordinate=deepcopy(s['coordinate']),
                segment_index=i,segment_id=f"{route['route_id']}:SEG-{i+1:02d}",
                segment_coordinates=deepcopy(path[i:i+2]),witness_coordinate=witness,
                horizontal_distance_m=distance,native_evidence=raw,
                incremental_physical_site=0 if sid in selected else 1,
                reuse_class=s.get('reuse_class'),eligibility=dict(status='conditional',source_eligibility=source['eligibility']),
                qualification=deepcopy(source.get('qualification')),unknowns=deepcopy(source.get('unknowns') or [])))
    return sorted(actions,key=lambda a:a['action_id']),rejected

def plan_middle_reuse(route, selected_sites, site_pool, endpoint_evidence, middle_segment_indices=None, middle_bindings=None):
    """Choose a feasible pair with colocation priority; no lexicographic proof."""
    if not route.get('route_id') or not all(_coordinate(p) for p in route.get('path') or []):
        raise ValueError('航路身份或坐标非法')
    endpoints=next((r.get('endpoints',{}) for r in (endpoint_evidence or {}).get('routes',[]) if r['route_id']==route['route_id']),{})
    endpoint_providers=[]; endpoint_ok=True
    for role in ('origin','destination'):
        e=endpoints.get(role,{})
        providers=e.get('selected_installed_providers') or []
        endpoint_ok &= (e.get('planning_status')=='satisfied' and
                        e.get('contract_version')==NAVIGATION_INTEGRITY_CONTRACT_VERSION_V2 and
                        e.get('local_monitoring_radius_m')==1000.0 and len(providers)==1)
        endpoint_providers.extend(providers)
    groups={p.get('distinct_site_id') for p in endpoint_providers}
    endpoint_ok &= len(groups)==2 and None not in groups
    for p in endpoint_providers:
        matching=[s for s in selected_sites if
                  (s.get('physical_group') or s.get('redundancy_group'))==p.get('distinct_site_id') and
                  s.get('coordinate')==p.get('coordinate')]
        endpoint_ok &= bool(len(matching)==1 and {'C','N'}.issubset(matching[0].get('devices',[])))
    actions,rejected=middle_candidate_actions(route,selected_sites,site_pool,groups)
    if middle_segment_indices is not None:
        actions=[a for a in actions if a['segment_index'] in middle_segment_indices]
    if middle_bindings is not None:
        if (not isinstance(middle_bindings,list) or len(middle_bindings)!=2 or
            any(not isinstance(b,dict) or not b.get('site_id') or
                not isinstance(b.get('segment_index'),int) or isinstance(b['segment_index'],bool) for b in middle_bindings)):
            raise ValueError('回归职责须为两个显式站址与航段绑定')
        allowed={(b['site_id'],b['segment_index']) for b in middle_bindings}
        actions=[a for a in actions if (a['site_id'],a['segment_index']) in allowed]
    pairs=[pair for pair in combinations(actions,2) if pair[0]['segment_index']!=pair[1]['segment_index'] and
           pair[0]['distinct_site_id']!=pair[1]['distinct_site_id']]
    tiers={'existing_cns_facility':0,'existing_shared_site':1,'tower_colocation_host':2,'candidate_site':3,'greenfield':4}
    def rank(pair):
        return (sum(a['incremental_physical_site'] for a in pair),
                sum(tiers.get(a['reuse_class'],5) for a in pair),
                sum(a['horizontal_distance_m'] for a in pair),tuple(a['action_id'] for a in pair))
    chosen=list(min(pairs,key=rank)) if pairs and endpoint_ok else []
    stations=deepcopy(selected_sites)
    for action in chosen:
        s=next((s for s in stations if s['site_id']==action['site_id']),None)
        if s is None:
            s=deepcopy(next(s for s in site_pool if s['site_id']==action['site_id'])); stations.append(s)
        if 'N' not in s['devices']: s['devices'].append('N')
        s.setdefault('N_roles',[]).append(action['segment_id'])
    counts=dict(physical_sites=len({s.get('physical_group') or s.get('redundancy_group') for s in stations}),
                new_physical_sites=len({s.get('physical_group') or s.get('redundancy_group') for s in stations if s.get('new')}),
                incremental_physical_sites=sum(a['incremental_physical_site'] for a in chosen),
                C=sum('C' in s['devices'] for s in stations),RID=sum('S' in s['devices'] for s in stations),
                N=sum('N' in s['devices'] for s in stations))
    counts['total_devices']=counts['C']+counts['RID']+counts['N']
    result=dict(algorithm_id=POLICY_ID,algorithm_version='1.0',contract=deepcopy(POLICY_CONTRACT),
                planning_status='satisfied' if len(chosen)==2 and endpoint_ok else 'unknown',
                actual_delivery='unknown',integrity_measurement_status='not_measured_not_certified',
                endpoint_evidence=deepcopy(endpoint_evidence),candidate_actions=actions,rejected_sites=rejected,
                selected_actions=chosen,stations=stations,counts=counts,
                new_contract_lexicographic_optimality='not_proved',persisted_as_upstream=False)
    result['input_fingerprint']=stable_fingerprint(dict(contract=POLICY_CONTRACT,route=route,
        selected_sites=selected_sites,site_pool=site_pool,endpoint_evidence=endpoint_evidence,
        middle_segment_indices=middle_segment_indices,middle_bindings=middle_bindings),prefix='nav-middle-reuse-')
    result['effective_parameters']={'middle_segment_indices':deepcopy(middle_segment_indices)}
    result['scenario_bindings']=deepcopy(middle_bindings)
    return result
