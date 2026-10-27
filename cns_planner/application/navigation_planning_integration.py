"""Caller-owned NM3 planning supplement; no canonical publisher or state writer.

P14/P15/P16 here are supplements, not replacement canonical result containers.
The legacy N v2 path is returned unchanged when no policy is selected.
"""
from copy import deepcopy
import json
from pathlib import Path

from ..algorithms.registry import build_default_algorithm_registry
from ..domain.navigation_middle_reuse import POLICY_ID, POLICY_CONTRACT
from ..domain.route_safety_evidence_v2 import stable_fingerprint
from .navigation_integrity_research_service import evaluate_navigation_integrity_research


def read_navigation_planning_config(planning_config):
    """Read an explicit mapping or JSON path, separate from generic P16 selection.

    No persistence is performed. The caller owns authoritative configuration.
    """
    if isinstance(planning_config, (str, Path)):
        planning_config = json.loads(Path(planning_config).read_text(encoding='utf-8'))
    if not isinstance(planning_config, dict):
        raise ValueError('规划配置必须为对象')
    value = planning_config.get('navigation_integrity_planning')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value)-{'selection', 'middle_bindings'}:
        raise ValueError('未知导航规划配置')
    selection = value.get('selection')
    if (not isinstance(selection, dict) or set(selection)-{'algorithm_type','algorithm_id','version','parameters'} or
        (selection.get('algorithm_type'), selection.get('algorithm_id'), selection.get('version')) != ('site_planner', POLICY_ID, '1.0') or
        not isinstance(selection.get('parameters', {}), dict)):
        raise ValueError('要求显式版本化导航规划策略')
    build_default_algorithm_registry(None).create('site_planner', POLICY_ID, '1.0', selection.get('parameters'))
    bindings = value.get('middle_bindings')
    if bindings is not None and (not isinstance(bindings, list) or len(bindings)!=2 or
        any(not isinstance(b,dict) or set(b)!={'site_id','segment_index'} or not isinstance(b['site_id'],str) or not b['site_id'] or
            type(b['segment_index']) is not int or b['segment_index']<1 for b in bindings) or
        len({b['segment_index'] for b in bindings})!=2 or len({b['site_id'] for b in bindings})!=2):
        raise ValueError('要求两个独立中段职责绑定')
    return dict(selection=dict(selection, parameters=deepcopy(selection.get('parameters',{}))), middle_bindings=deepcopy(bindings))


def _hypothetical(value):
    """Remove the primitive's installed labels from caller-owned assumptions."""
    if isinstance(value, list):
        return [_hypothetical(v) for v in value]
    if isinstance(value, dict):
        result = {('selected_hypothetical_providers' if k=='selected_installed_providers' else k):_hypothetical(v) for k,v in value.items()}
        if result.get('planning_status')=='installed':
            result['planning_status']='hypothetical'
        if result.get('provider_id'):
            result['provider_id']='hypothetical:'+str(result['provider_id'])
        result['proposal_only']=True
        return result
    return value


def _residual(rows):
    return [dict(responsibility_id=r['responsibility_id'], role=r['role'],
                 segment_index=r.get('segment_index'), planning_status=r['planning_status'])
            for r in rows if r['planning_status']!='satisfied']


def dispatch_middle_actions(responsibilities, actions, candidate_actions):
    """Typed cumulative what-if; only exact evaluated candidates may be applied.

    Does not append facilities, create installed identities or invoke C/RID MILP.
    """
    rows=deepcopy(responsibilities)
    candidates={a['action_id']:a for a in candidate_actions}
    groups={p['distinct_site_id'] for r in rows for p in r['installed_providers']+r['hypothetical_providers']}
    segments={r['segment_index'] for r in rows if r['role']=='middle' and r['planning_status']=='satisfied'}
    seen=set()
    for action in actions:
        aid=action.get('action_id')
        if (aid in seen or aid not in candidates or action!=candidates[aid] or
            action.get('action_type')!='add_navigation_integrity_middle_monitor' or action.get('planner_family')!=POLICY_ID):
            raise ValueError('中段分发只接受本次评价的类型化候选动作')
        seen.add(aid)
        targets=[r for r in rows if r['role']=='middle' and r.get('segment_index')==action['segment_index'] and r['route_id']==action['route_id']]
        if not targets:
            targets=[r for r in rows if r['role']=='middle' and r.get('segment_index') is None and r['route_id']==action['route_id'] and r['planning_status']!='satisfied'][:1]
        if len(targets)!=1 or action['distinct_site_id'] in groups or action['segment_index'] in segments or targets[0]['planning_status']=='satisfied':
            raise ValueError('中段职责或四物理组互异约束不满足')
        row=targets[0]
        row['segment_index']=action['segment_index']
        row['hypothetical_providers']=[dict(provider_id='hypothetical:'+aid, distinct_site_id=action['distinct_site_id'],
            site_id=action['site_id'],coordinate=deepcopy(action['coordinate']),planning_status='hypothetical',proposal_only=True)]
        row['planning_status']=action['native_evidence']['planning_status']
        row['native_evidence']=deepcopy(action['native_evidence'])
        groups.add(action['distinct_site_id'])
        segments.add(action['segment_index'])
    return dict(responsibilities=rows,residual_gaps=_residual(rows),actual_delivery='unknown',proposal_only=True)


def _navigation_planning_fingerprint(*, config, required_cns, route, facilities,
        installed_facilities, facility_authority, selected_sites, site_pool,
        communication_delivery):
    """The single ``nav-planning-`` identity of one supplement evaluation.

    Both the successful evaluation and the fail-closed degraded supplement use
    this exact expression, so a configuration/input change can never leave the
    canonical P14 fingerprint bound to a stale supplement identity.
    """
    return stable_fingerprint(dict(config=config,contract=POLICY_CONTRACT,route=route,
        required_cns=required_cns,facilities=facilities,installed_facilities=installed_facilities,facility_authority=facility_authority,
        selected_sites=selected_sites,site_pool=site_pool,communication_delivery=communication_delivery),prefix='nav-planning-')


def evaluate_navigation_planning(*, required_cns, route, facilities, selected_sites,
        site_pool, planning_config=None, facility_authority='research_assumption',
        installed_facilities=None, communication_delivery=None):
    """Evaluate explicit policy configuration without a session or save operation.

    Research fixtures must use the default authority. Only a caller with real
    authoritative installed facilities may explicitly pass authority='installed'
    and supply installed_facilities separately; facilities remains research input.
    Authority is bound to the fingerprint, never inferred from device/site names.
    """
    if facility_authority not in ('research_assumption','installed'):
        raise ValueError('未知设施身份来源')
    config=read_navigation_planning_config({'navigation_integrity_planning':planning_config})
    args=dict(required_cns=required_cns,route=route,facilities=facilities,selected_sites=selected_sites,
              site_pool=site_pool,communication_delivery=communication_delivery)
    if config is None:
        return evaluate_navigation_integrity_research(**args)
    if facility_authority=='installed':
        if not isinstance(installed_facilities,dict) or not isinstance(installed_facilities.get('items'),list):
            raise ValueError('真实安装评价必须单独提供 installed_facilities 容器')
        args['facilities']=installed_facilities
    elif installed_facilities is not None:
        raise ValueError('研究假设与真实安装容器不得混合')
    raw=evaluate_navigation_integrity_research(**args,selection=config['selection'],middle_bindings=config['middle_bindings'])
    if not raw.get('endpoint_evidence'):
        raise ValueError('显式中段策略要求有效的导航需求合同')
    endpoints=next((r['endpoints'] for r in raw['endpoint_evidence']['routes'] if r['route_id']==route['route_id']),{})
    endpoint_groups=[p.get('distinct_site_id') for e in endpoints.values() for p in e.get('selected_installed_providers',[])]
    endpoint_identity_valid=len(endpoint_groups)==2 and None not in endpoint_groups and len(set(endpoint_groups))==2
    rows=[]
    for role in ('origin','destination'):
        e=endpoints.get(role,{})
        providers=deepcopy(e.get('selected_installed_providers') or [])
        # Preserve NM2's endpoint station/device identity gate in the supplement.
        coherent=endpoint_identity_valid and len(providers)==1
        for provider in providers:
            matching=[s for s in selected_sites if (s.get('physical_group') or s.get('redundancy_group'))==provider.get('distinct_site_id') and s.get('coordinate')==provider.get('coordinate')]
            coherent &= len(matching)==1 and {'C','N'}.issubset(matching[0].get('devices',[]))
        installed=providers if facility_authority=='installed' else []
        hypothetical=_hypothetical(providers) if facility_authority=='research_assumption' else []
        rows.append(dict(responsibility_id=route['route_id']+':'+role,route_id=route['route_id'],role=role,
            installed_providers=installed,hypothetical_providers=hypothetical,required_count=1,
            planning_status=e.get('planning_status','unknown') if coherent else 'unknown',
            identity_coherent=bool(coherent),actual_delivery='unknown'))
    indices=config['selection']['parameters'].get('middle_segment_indices')
    if config['middle_bindings'] is not None:
        bound=[b['segment_index'] for b in config['middle_bindings']]
        if indices is not None and set(indices)!=set(bound):
            raise ValueError('策略索引与场景职责绑定冲突')
        indices=bound
    if indices is None:
        indices=[a['segment_index'] for a in raw['selected_actions']] or [None,None]
    for n,index in enumerate(indices):
        rows.append(dict(responsibility_id=f"{route['route_id']}:middle:{n}",route_id=route['route_id'],
            role='middle',segment_index=index,required_count=1,installed_providers=[],hypothetical_providers=[],
            planning_status='unknown',actual_delivery='unknown'))
    candidates=_hypothetical(raw['candidate_actions'])
    chosen=_hypothetical(raw['selected_actions'])
    projected=dispatch_middle_actions(rows,chosen,candidates)
    projected.update(candidate_actions=candidates,selected_actions=chosen)
    return dict(contract=deepcopy(POLICY_CONTRACT),effective_config=config,
        input_fingerprint=_navigation_planning_fingerprint(config=config,required_cns=required_cns,
            route=route,facilities=facilities,installed_facilities=installed_facilities,
            facility_authority=facility_authority,selected_sites=selected_sites,
            site_pool=site_pool,communication_delivery=communication_delivery),
        facility_authority=facility_authority,P14=dict(responsibilities=rows,proposal_only=True),
        P15=dict(residual_gaps=_residual(rows),proposal_only=True),P16=projected,
        planning_status=raw['planning_status'],actual_delivery='unknown',counts=deepcopy(raw['counts']),
        new_contract_lexicographic_optimality='not_proved',canonical_published=False)


#: B2-1：P14 正式追加的版本化 supplement 合同。只追加，不替换 canonical P14 字段。
SUPPLEMENT_CONTRACT_VERSION = 'navigation_integrity_supplement_v1'


def _supplement_payload(evaluated):
    """Project a successful research evaluation into the versioned supplement."""

    return {
        'contract_version': SUPPLEMENT_CONTRACT_VERSION,
        'policy_id': POLICY_ID,
        'policy_version': str(POLICY_CONTRACT['version']),
        'input_fingerprint': evaluated['input_fingerprint'],
        'facility_authority': evaluated['facility_authority'],
        'responsibilities': deepcopy(evaluated['P14']['responsibilities']),
        'residual_gaps': deepcopy(evaluated['P15']['residual_gaps']),
        'proposal_only': True,
        'actual_delivery': 'unknown',
        'canonical_published': False,
    }


def build_navigation_integrity_supplement(*, required_cns, route, facilities,
        selected_sites, site_pool, planning_config=None,
        facility_authority='research_assumption', installed_facilities=None,
        communication_delivery=None):
    """Build the versioned P14/P15 supplement with the existing research algorithm.

    This is a **read-only adapter**: it calls :func:`evaluate_navigation_planning`
    (the untouched NM3 research entry point) and projects only the P14
    responsibilities and P15 residual gaps into a versioned supplement.  It does
    not rewrite the algorithm, does not create a canonical publisher and never
    writes ``ProjectState``.

    ``planning_config is None`` returns ``None`` so the legacy N v2 path keeps
    its exact result shape and fingerprint.
    """
    if planning_config is None:
        return None
    evaluated = evaluate_navigation_planning(
        required_cns=required_cns, route=route, facilities=facilities,
        selected_sites=selected_sites, site_pool=site_pool,
        planning_config=planning_config, facility_authority=facility_authority,
        installed_facilities=installed_facilities,
        communication_delivery=communication_delivery,
    )
    return _supplement_payload(evaluated)


#: Existing-facility / candidate-site station adaptation.  The research planner
#: consumes the frozen C/RID station shape; the canonical ProjectState stores
#: normalized CandidateSite / ExistingCNSFacility records.  This adapter only
#: maps already-declared facts (identity, coordinate, subsystem evidence,
#: planning cost/eligibility) and never invents a new site, device or cost.
_SUPPLEMENT_STATION_UNKNOWNS = [
    'source_CRS_and_installation_survey', 'permission', 'bearing_capacity',
    'power_backhaul', 'GNSS_environment', 'RF_LOS_interference', 'certification',
]


def _supplement_station_from_facility(facility):
    """Map one normalized existing CNS facility to the frozen station shape.

    B2-2 预检修复：``eligibility`` **不得**仅凭"设施记录存在"推断为 ``eligible``。
    只有同时具备显式 ``planning_profile.confirmed is True`` 与
    ``planning_profile.add_device_allowed is True`` 的**真实世界**设施才是 eligible；
    其余真实设施保持 ``conditional``，并把"确认/加装许可/安装调查"等未知事实写入
    ``unknowns``，绝不把 ``unknown`` 升格成通过。合成数据集记录不是现实设施，
    P16 的假想提案记录也只是工作副本，均不得冒充已安装资质。

    本函数只搬运已声明事实，不杜撰坐标、设备、成本或资格。
    """

    if not isinstance(facility, dict):
        return None
    site_id = str(facility.get('site_id') or facility.get('facility_id') or '')
    if not site_id:
        return None
    coordinate = facility.get('coordinate')
    if not (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2):
        return None
    from .cns_facility_assembler import SYNTHETIC_DATASET_CLASSES

    metadata = facility.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    if (
        str(metadata.get('dataset_class') or '') in SYNTHETIC_DATASET_CLASSES
        or metadata.get('real_world_facility') is False
    ):
        #: 合成 / 工程验证记录不是现实中的既有设施，绝不进入研究站点池冒充安装资质。
        return None
    profile = facility.get('planning_profile')
    profile = profile if isinstance(profile, dict) else {}
    confirmed = profile.get('confirmed') is True
    add_device_allowed = profile.get('add_device_allowed')
    if add_device_allowed is False:
        #: 显式不允许加装设备：不能作为中段 N 的候选宿主。
        return None
    proposal_only = metadata.get('proposal_only') is True
    eligibility = (
        'eligible'
        if confirmed and add_device_allowed is True and not proposal_only
        else 'conditional'
    )
    unknowns = list(_SUPPLEMENT_STATION_UNKNOWNS)
    if proposal_only:
        unknowns.insert(0, 'p16_hypothetical_proposal_host')
    if not confirmed:
        unknowns.insert(0, 'planning_profile_confirmation')
    if add_device_allowed is not True:
        unknowns.insert(0, 'add_device_permission')
    devices = sorted({
        str(device.get('subsystem') or '').upper()
        for device in facility.get('devices') or []
        if str(device.get('subsystem') or '').upper() in ('C', 'N', 'S')
    })
    return {
        'site_id': site_id,
        'physical_group': str(
            facility.get('distinct_site_id')
            or f"takeoff_landing_site:{site_id}"
        ),
        'coordinate': deepcopy(coordinate),
        'new': proposal_only,
        'reuse_class': profile.get('reuse_class') or 'existing_cns_facility',
        'devices': devices,
        'host_kind': 'existing_facility',
        'identity_refs': [str(facility.get('facility_id') or site_id)],
        'eligibility': eligibility,
        'conditional_cost': profile.get('planning_cost'),
        'qualification': {
            'status': 'conditional_engineering_not_installation_authorization',
            'source': 'existing_cns_facility_inventory',
            'planning_profile_confirmed': confirmed,
            'add_device_allowed': add_device_allowed,
            'installation_authorization': False,
            'proposal_only': proposal_only,
        },
        'unknowns': unknowns,
        'proposal_only': proposal_only,
    }


def _supplement_site_from_candidate(site):
    """Map one normalized candidate site to the frozen research site-pool shape."""

    if not isinstance(site, dict):
        return None
    site_id = str(site.get('site_id') or '')
    coordinate = site.get('coordinate')
    if not site_id or not (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2):
        return None
    subsystems = sorted({
        str(value).upper() for value in site.get('available_subsystems') or []
        if str(value).upper() in ('C', 'N', 'S')
    })
    if 'C' not in subsystems or site.get('usable') is False or site.get('locked') is True:
        return None
    profile = site.get('planning_profile') or {}
    return {
        'site_id': site_id,
        'physical_group': site_id,
        'coordinate': deepcopy(coordinate),
        'new': True,
        'reuse_class': profile.get('reuse_class') or 'candidate_site',
        'devices': subsystems,
        'host_kind': 'candidate_site',
        'identity_refs': [site_id],
        'eligibility': 'conditional',
        'conditional_cost': profile.get('planning_cost'),
        'unknowns': deepcopy(_SUPPLEMENT_STATION_UNKNOWNS),
    }


def _supplement_site_from_tower(site):
    """Map one tower-colocation host; only explicit ``available_subsystems`` count.

    Tower records do not declare installable subsystems (empty means *no
    evidence*, never "all subsystems"), so a tower without an explicit ``C``
    declaration is not admitted into the local-C candidate pool.
    """

    if not isinstance(site, dict):
        return None
    site_id = str(site.get('site_id') or '')
    coordinate = site.get('coordinate')
    if not site_id or not (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2):
        return None
    subsystems = sorted({
        str(value).upper() for value in site.get('available_subsystems') or []
        if str(value).upper() in ('C', 'N', 'S')
    })
    if 'C' not in subsystems:
        return None
    profile = site.get('planning_profile') or {}
    return {
        'site_id': site_id,
        'physical_group': site_id,
        'coordinate': deepcopy(coordinate),
        'new': False,
        'reuse_class': profile.get('reuse_class') or 'tower_colocation_host',
        'devices': subsystems,
        'host_kind': 'tower',
        'identity_refs': [site_id],
        'eligibility': 'conditional',
        'conditional_cost': profile.get('planning_cost'),
        'unknowns': deepcopy(_SUPPLEMENT_STATION_UNKNOWNS),
    }


def _supplement_sources(candidate_sites, tower_colocation, selected_stations):
    """Freeze the complete site pool in one deterministic order (deduplicated)."""

    pool, seen = [], set()

    def add(entry):
        if entry is None:
            return
        site_id = entry.get('site_id')
        if not site_id or site_id in seen:
            return
        seen.add(site_id)
        pool.append(entry)

    for entry in selected_stations:
        add(entry)
    for site in (candidate_sites or {}).get('items') or []:
        add(_supplement_site_from_candidate(site))
    for site in (tower_colocation or {}).get('items') or []:
        add(_supplement_site_from_tower(site))
    pool.sort(key=lambda item: str(item.get('site_id') or ''))
    return pool


def _supplement_context(facilities, candidate_sites=None, tower_colocation=None):
    """Build the frozen selected-station / site-pool context exactly once."""

    facilities = facilities if isinstance(facilities, dict) else {}
    selected_stations = [
        station for station in (
            _supplement_station_from_facility(item)
            for item in facilities.get('items') or []
        ) if station is not None
    ]
    site_pool = _supplement_sources(candidate_sites, tower_colocation, selected_stations)
    return selected_stations, site_pool


def navigation_integrity_middle_evaluation(*, planning_config, routes, required_cns,
        facilities, candidate_sites=None, tower_colocation=None,
        facility_authority='research_assumption', installed_facilities=None,
        communication_delivery=None):
    """Evaluate the frozen research plan once and expose its typed middle actions.

    P16 needs the **same** research evaluation that produced the P14 supplement,
    including the typed ``candidate_actions`` / ``selected_actions`` that the
    supplement intentionally does not duplicate.  This helper therefore shares
    the exact station/pool adaptation with
    :func:`navigation_integrity_supplement_inputs` and calls the untouched
    research entry point once.  It never writes state and never publishes.

    Returns ``None`` when no explicit policy exists.  A missing navigation
    requirement contract yields a versioned ``missing_data`` supplement with
    ``evaluated=None`` (the same fail-closed semantics as P14), while every
    other configuration/geometry error keeps propagating.
    """

    if planning_config is None:
        return None
    if not isinstance(routes, list) or len(routes) != 1:
        raise ValueError('NM3 中段补充当前只支持单航路项目')
    route = routes[0]
    facilities = facilities if isinstance(facilities, dict) else {}
    selected_stations, site_pool = _supplement_context(
        facilities, candidate_sites, tower_colocation,
    )
    try:
        evaluated = evaluate_navigation_planning(
            required_cns=required_cns, route=route, facilities=facilities,
            selected_sites=selected_stations, site_pool=site_pool,
            planning_config=planning_config, facility_authority=facility_authority,
            installed_facilities=installed_facilities,
            communication_delivery=communication_delivery,
        )
    except ValueError as exc:
        if '导航需求合同' not in str(exc):
            raise
        supplement = _degraded_navigation_integrity_supplement(
            reason=exc, config=planning_config, required_cns=required_cns,
            route=route, facilities=facilities, selected_sites=selected_stations,
            site_pool=site_pool, facility_authority=facility_authority,
            installed_facilities=installed_facilities,
            communication_delivery=communication_delivery,
        )
        return {
            'evaluated': None,
            'supplement': supplement,
            'selected_sites': selected_stations,
            'site_pool': site_pool,
        }
    return {
        'evaluated': evaluated,
        'supplement': _supplement_payload(evaluated),
        'selected_sites': selected_stations,
        'site_pool': site_pool,
    }


def _degraded_navigation_integrity_supplement(*, reason, config, required_cns, route,
        facilities, selected_sites, site_pool, facility_authority,
        installed_facilities, communication_delivery):
    """Fail-closed supplement when the explicit policy cannot be evaluated.

    The supplement is still versioned and carries the **same** ``nav-planning-``
    identity expression as a successful evaluation (so config changes always
    change the P14 fingerprint).  It never marks unknown evidence as satisfied.
    """

    return {
        'contract_version': SUPPLEMENT_CONTRACT_VERSION,
        'policy_id': POLICY_ID,
        'policy_version': str(POLICY_CONTRACT['version']),
        'input_fingerprint': _navigation_planning_fingerprint(
            config=config, required_cns=required_cns, route=route,
            facilities=facilities, installed_facilities=installed_facilities,
            facility_authority=facility_authority, selected_sites=selected_sites,
            site_pool=site_pool, communication_delivery=communication_delivery,
        ),
        'facility_authority': facility_authority,
        'responsibilities': [],
        'residual_gaps': [],
        'proposal_only': True,
        'actual_delivery': 'unknown',
        'canonical_published': False,
        'planning_status': 'missing_data',
        'missing_reason': str(reason),
    }


def navigation_integrity_supplement_inputs(*, planning_config, routes, required_cns,
        facilities, candidate_sites=None, tower_colocation=None,
        facility_authority='research_assumption', installed_facilities=None,
        communication_delivery=None):
    """Freeze the complete NM3 supplement input identity for P14.

    Both the synchronous ``CorridorService._assemble_inputs`` path and the
    worker ``task_specs._corridor_inputs`` snapshot call this one function, so
    the ``nav-planning-`` identity can never split into two values.  With no
    explicit config it returns an **empty** dict: the legacy P14 input shape and
    fingerprint stay byte-identical.

    The canonical R0005 contract is single-route; more than one route is
    rejected explicitly instead of silently picking one.
    """

    if planning_config is None:
        return {}
    if not isinstance(routes, list) or len(routes) != 1:
        raise ValueError('NM3 中段补充当前只支持单航路项目')
    route = routes[0]
    facilities = facilities if isinstance(facilities, dict) else {}
    selected_stations, site_pool = _supplement_context(
        facilities, candidate_sites, tower_colocation,
    )
    try:
        supplement = build_navigation_integrity_supplement(
            required_cns=required_cns, route=route, facilities=facilities,
            selected_sites=selected_stations, site_pool=site_pool,
            planning_config=planning_config, facility_authority=facility_authority,
            installed_facilities=installed_facilities,
            communication_delivery=communication_delivery,
        )
    except ValueError as exc:
        #: 显式策略要求有效的导航需求合同；配置已存在时不能静默回到旧路径
        #: （那会让"配置已生效"与"P14 仍无 supplement"同时成立）。这里保留
        #: 版本化、可 stale 的 missing_data supplement，如实披露原因。
        #: 其他配置/几何错误继续抛出：绝不把编程错误伪装成 missing_data。
        if '导航需求合同' not in str(exc):
            raise
        supplement = _degraded_navigation_integrity_supplement(
            reason=exc, config=planning_config, required_cns=required_cns,
            route=route, facilities=facilities, selected_sites=selected_stations,
            site_pool=site_pool, facility_authority=facility_authority,
            installed_facilities=installed_facilities,
            communication_delivery=communication_delivery,
        )
    return {'navigation_integrity_supplement': supplement}
