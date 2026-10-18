"""M1: detached, read-only UE/engineering-rule inputs for M2/M3.

Accept only small, already selected input containers, never a session or a
ProjectState. No evaluator, persistence, adoption or default safety approval is
invoked. A snapshot is JSON data detached from its inputs; consumers must treat
it as immutable and verify its fingerprint before reusing it.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1, _limit
from ..domain.cns_continuous_service import (
    CONTINUOUS_SERVICE_ALGORITHM_ID, CONTINUOUS_SERVICE_ALGORITHM_VERSION,
)
from ..domain.cns_service_registry import required_services_for, service_registry_entry
from ..domain.fc30_profile import fc30_planning_speeds
from ..domain.navigation_augmentation import planning_policy_status


SCHEMA_VERSION = 'frozen_ue_rules.v1'
SERVICES = {
    'C:communication': ('C', 'communication', 'cns_planner.algorithms.corridor.v1'),
    'N:rtk_augmentation': ('N', 'navigation', 'cns_planner.domain.navigation_augmentation'),
    'N:navigation_integrity_monitoring': (
        'N', 'navigation', 'cns_planner.domain.navigation_integrity_monitoring'),
    'S:rid_cooperative': ('S', 'surveillance', 'cns_planner.algorithms.corridor.v1'),
    'S:radar_noncooperative': ('S', 'surveillance', 'cns_planner.domain.radar_service_evidence'),
}
_EVENT_COLLECTIONS = {
    'failure_conditions': 'failure_condition_id',
    'unacceptable_events': 'unacceptable_event_id',
    'coupled_unacceptable_events': 'coupled_unacceptable_event_id',
}
_LIMIT_FIELDS = {
    'service_outage': 'c_full_outage_max_s',
    'redundancy_degradation': 'c_redundancy_degradation_max_s',
}
_NAV_EVIDENCE_FIELDS = (
    'navigation.performance.max_horizontal_error_m',
    'navigation.performance.max_vertical_error_m',
    'navigation.performance.max_time_to_alert_s',
    'navigation.performance.min_availability',
    'navigation_detection_time_s', 'navigation_switch_time_s',
)


def _object(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('M1输入容器必须是对象或null')
    return value


def _at(value, path):
    for name in path.split('.'):
        if not isinstance(value, dict) or name not in value:
            return None
        value = value[name]
    return value


def _leaves(value, prefix):
    if isinstance(value, dict) and value:
        for key, item in sorted(value.items()):
            yield from _leaves(item, f'{prefix}.{key}')
    else:
        yield prefix, value


def _events(policy):
    records = []
    for collection, id_field in _EVENT_COLLECTIONS.items():
        for definition in policy.get(collection) or []:
            records.append({
                'event_id': definition.get(id_field), 'kind': collection,
                'confirmed': definition.get('confirmed'),
                'status': definition.get('status'),
                'definition': deepcopy(definition),
                # No production FC/UE -> P17 compiler exists at this baseline.
                'link_status': 'ue_link_unresolved',
                'production_consumer': None,
            })
    return records


def _consumption(requirements):
    rows, declarations = [], {}
    for key, (code, section, consumer) in SERVICES.items():
        parent = _object(requirements.get(section))
        # Reuse canonical dispatch identity, but never copy normalized planning
        # defaults into the frozen declarations (in particular RTK station count).
        dispatched = {x['service_key'] for x in required_services_for(code, parent)}
        if 'services' in parent:
            raw = deepcopy(_object(parent['services'].get(key)))
        elif key in dispatched:
            raw = deepcopy(parent)
        else:
            raw = {}
        declarations[key] = raw
        required = raw.get('required')
        if required is False or (not raw and parent.get('required') is False):
            status, reason = 'not_applicable', 'service_explicitly_not_required'
        elif key == 'N:rtk_augmentation' and key not in dispatched:
            status, reason = 'unconsumed', 'required_service_not_adopted'
        elif not raw:
            status, reason = 'missing', 'service_requirement_missing'
        elif required is not True or raw.get('confirmed') is not True:
            status, reason = 'unknown', 'service_requirement_unconfirmed'
        else:
            status, reason = 'consumed', 'production_service_dispatch'
        rows.append({
            'service_key': key, 'field': '$service', 'value': deepcopy(raw),
            'required': required, 'confirmed': raw.get('confirmed'),
            'adopted': key in dispatched and raw.get('confirmed') is True,
            'status': status, 'reason_code': reason,
            'consumer_exists': True, 'production_consumer': consumer,
            'scope': service_registry_entry(key)['service_scope'],
            'semantics': 'input_dispatch_only_not_requirement_satisfaction',
        })
        for field, value in _leaves(raw, '$requirement'):
            if not raw:
                continue
            # Unknown fields must remain visible; no blanket "all consumed".
            unconsumed = key == 'C:communication' and field == '$requirement.max_gap_m' and value is not None
            rows.append({
                'service_key': key, 'field': field, 'value': deepcopy(value),
                'status': 'unconsumed' if unconsumed else 'missing' if value is None else 'unknown',
                'reason_code': 'requirement_unconsumed' if unconsumed else
                               'requirement_value_missing' if value is None else 'consumer_unknown',
                'production_consumer': None,
            })
    # Preserve subsystem-level fields too, including max_gap_m even when a
    # service map is present. Never equate this distance with a time threshold.
    for section, parent in sorted(requirements.items()):
        if section not in ('communication', 'navigation', 'surveillance'):
            continue
        key = {'communication': 'C:communication', 'navigation': 'N:navigation',
               'surveillance': 'S:surveillance'}[section]
        for name, value in sorted(_object(parent).items()):
            if name == 'services':
                continue
            for field, leaf in _leaves(value, f'{section}.{name}'):
                unconsumed = section == 'communication' and name == 'max_gap_m' and leaf is not None
                rows.append({
                    'service_key': key, 'field': field, 'value': deepcopy(leaf),
                    'status': 'unconsumed' if unconsumed else 'missing' if leaf is None else 'unknown',
                    'reason_code': 'requirement_unconsumed' if unconsumed else
                                   'requirement_value_missing' if leaf is None else 'consumer_unknown',
                    'production_consumer': None,
                })
    rtk = declarations['N:rtk_augmentation']
    planning_status = planning_policy_status(rtk.get('planning'))
    for name in ('max_reference_baseline_m', 'required_distinct_site_count'):
        value = _at(rtk, f'planning.{name}')
        rows.append({
            'service_key': 'N:rtk_augmentation', 'field': f'planning.{name}', 'value': value,
            'status': 'missing' if value is None else 'consumed' if rtk.get('required') is True
                      and rtk.get('confirmed') is True and planning_status['ready'] else 'unknown',
            'reason_code': 'threshold_missing' if value is None else 'production_planning_parameter'
                           if rtk.get('required') is True and rtk.get('confirmed') is True and planning_status['ready']
                           else planning_status['reason'] or 'service_requirement_unconfirmed',
            'production_consumer': SERVICES['N:rtk_augmentation'][2] if value is not None else None,
        })
    return rows, declarations


def _threshold_source(code, kind, policy, parameters, evidence):
    name = _LIMIT_FIELDS[kind]
    limit = _at(policy, f'service_acceptability_limits.{code}.{kind}')
    parameter_override = _at(policy, f'parameter_overrides.{name}')
    if limit is not None or parameter_override is not None:
        path = f'service_acceptability_limits.{code}.{kind}' if limit is not None else f'parameter_overrides.{name}'
        return dict(source_id=f'continuous_service_policy.{path}', source=policy.get('source'),
                    evidence_level='engineering_assumption', version=policy.get('version'),
                    authority='policy_override')
    parameter = parameters[name]
    if code == 'C' and parameter.get('value') is not None:
        evidence_id = parameter.get('evidence_id')
        raw = next((x for x in evidence.get('items', []) if x.get('evidence_id') == evidence_id), {})
        return dict(source_id=evidence_id or f'p17.parameter_baseline.{name}',
                    source=parameter.get('source'), evidence_level=parameter.get('source_type'),
                    version=raw.get('version') if evidence_id else CONTINUOUS_SERVICE_ALGORITHM_VERSION,
                    authority=parameter.get('authority'))
    # This mirrors the actual _limit result, including S's existing baseline;
    # it does not promote C evidence into S-specific source evidence.
    return dict(source_id=f'p17.DEFAULT_SERVICE_ACCEPTABILITY_LIMITS.{code}.{kind}',
                source='cns_planner.domain.cns_continuous_service.DEFAULT_SERVICE_ACCEPTABILITY_LIMITS',
                evidence_level='internal_baseline' if code == 'S' else 'unknown',
                version=CONTINUOUS_SERVICE_ALGORITHM_VERSION,
                authority='builtin_engineering_assumption' if code == 'S' else 'evidence_required')


def build_frozen_ue_rule_snapshot(
    *, route_id, safety_policy=None, required_cns=None, continuous_service_policy=None,
    operation_scenario=None, aircraft_profile=None, planning_evidence=None, research_scenario=None,
) -> dict:
    """Build detached rule/provenance JSON and a deterministic SHA256.

    RequiredCNS route override replaces the project requirement set, following
    existing navigation/Radar resolvers. Missing fields remain absent/null; only
    the existing P17 resolver may expose its *labelled* engineering baselines.
    Research data is frozen separately and is never applied to aircraft facts.
    This callable is an explicit M2/M3 entry, not a new API or runtime hook.
    """
    required = _object(required_cns)
    override = _object(required.get('route_overrides')).get(str(route_id))
    effective = override or required.get('project_default') or {}
    # No copy/traversal of other routes, artifacts, P14/P15 or complete state.
    frozen = deepcopy({
        'route_id': route_id,
        'required_cns': {
            **{k: required[k] for k in ('status', 'confirmed', 'source', 'version', 'schema_version') if k in required},
            'requirements': effective, 'authority': 'route_override' if override else 'project_default',
        },
        'safety_policy': {k: v for k, v in _object(safety_policy).items()
                          if k in (*_EVENT_COLLECTIONS, 'confirmed', 'status', 'source', 'version',
                                   'functional_dependencies', 'coupled_conditions')},
        'continuous_service_policy': _object(continuous_service_policy),
        'operation_scenario': _object(operation_scenario),
        'aircraft_profile': {k: v for k, v in _object(aircraft_profile).items()
                             if k in ('aircraft_id', 'model', 'version', 'source', 'metadata',
                                      'cruise_speed_mps', 'max_speed_mps', 'max_horizontal_speed_mps',
                                      'navigation_detection_time_s', 'navigation_switch_time_s',
                                      'communication', 'navigation', 'surveillance', 'airborne_evidence')},
        'planning_evidence': _object(planning_evidence),
        'research_scenario': research_scenario,
    })
    # Enforce JSON-only, finite input and canonical string-key ordering before
    # parsing. No default=str, time stamps, repr() or object addresses.
    json.dumps(frozen, sort_keys=True, ensure_ascii=False, allow_nan=False)
    policy = frozen['continuous_service_policy']
    scenario = frozen['operation_scenario']
    aircraft = frozen['aircraft_profile']
    evidence = frozen['planning_evidence']
    research = _object(frozen['research_scenario'])
    track = research.get('evidence_track') if research else 'real_aircraft_evidence'
    if track not in ('real_aircraft_evidence', 'engineering_research', 'literature_grounded_simulation'):
        raise ValueError('research_scenario.evidence_track必须显式区分真实证据与研究仿真')
    events = _events(frozen['safety_policy'])
    consumption, declarations = _consumption(effective)
    parameters = ContinuousServiceAcceptabilityV1._parameters(evidence, policy)
    identity = dict(route_id=route_id, scenario_id=scenario.get('scenario_id'),
                    operating_phase=scenario.get('operating_phase', scenario.get('flight_phase')))
    missing = [name for name, value in identity.items() if value is None or value == '']
    scope_missing = [f'operation_scenario.{name}' for name in ('space_scope', 'cruise_altitude')
                     if not scenario.get(name)]
    missing.extend(scope_missing)
    context_complete = not missing
    if research:
        missing.extend(f'research_scenario.{name}' for name in ('scenario_id', 'source_id', 'version')
                       if research.get(name) in (None, ''))
    if track == 'literature_grounded_simulation':
        missing.extend(f'research_scenario.{name}' for name in ('simulation_scenario_id', 'simulated_backup_model_id')
                       if research.get(name) in (None, ''))
    rules = []
    for key in ('C:communication', 'S:rid_cooperative', 'S:radar_noncooperative'):
        code = key[0]
        adoption = next(x for x in consumption if x['field'] == '$service' and x['service_key'] == key)
        for kind in _LIMIT_FIELDS:
            value = _limit(parameters, code, kind, policy)
            source = _threshold_source(code, kind, policy, parameters, evidence)
            reasons = ['ue_link_unresolved']
            if scope_missing:
                reasons.append('rule_scope_missing')
            if value is None:
                reasons.append('threshold_missing')
                missing.append(f'{key}.{kind}.threshold')
            if source['authority'] == 'builtin_engineering_assumption':
                reasons.append('builtin_engineering_assumption')
            if not source['source']:
                reasons.append('rule_source_missing')
            if source['version'] is None:
                reasons.append('rule_version_missing')
            applicability = ('not_applicable' if adoption['status'] == 'not_applicable' else
                             'applicable' if adoption['adopted'] and context_complete else 'unknown')
            rules.append({
                **identity, **source, 'service_key': key, 'source_service_key': key,
                'failure_mode': kind,
                'scenario_rule_id': f"{identity['scenario_id']}:{key}:{kind}" if identity['scenario_id'] else None,
                'ue_id': None, 'fc_id': None,
                'candidate_event_ids': [x['event_id'] for x in events if x['definition'].get('subsystem') == code],
                'link_status': 'ue_link_unresolved', 'confirmed': policy.get('confirmed'),
                'research_scenario_id': research.get('scenario_id'), 'evidence_track': track,
                'threshold': {'value': value, 'unit': 's', 'operator': '<='},
                'scope': {'service_scope': service_registry_entry(key)['service_scope'],
                          'space': scenario.get('space_scope'), 'altitude': scenario.get('cruise_altitude'),
                          'operating_phase': identity['operating_phase']},
                'required': declarations[key].get('required'),
                'applicability': applicability,
                'engineering_rule_status': 'sourced_and_applicable' if applicability == 'applicable'
                    and value is not None and source['source'] and source['version'] else 'unknown',
                'production_consumer': 'ContinuousServiceAcceptabilityV1._service_subsystem',
                'consumer_exists': True, 'consumer_scope': f'subsystem:{code}',
                'per_service_consumer': code == 'C',
                'consumption_status': 'not_applicable' if applicability == 'not_applicable' else
                                      'missing' if value is None else
                                      'consumed' if adoption['adopted'] else 'unknown',
                'acceptability_status': 'not_evaluated', 'reason_codes': reasons,
            })
    for key, name, mode, unit, operator in (
        ('N:rtk_augmentation', 'max_reference_baseline_m', 'reference_baseline_exceeded', 'm', '<='),
        ('N:rtk_augmentation', 'required_distinct_site_count', 'reference_station_deficit', 'count', '>='),
        ('N:navigation_integrity_monitoring', 'required_monitor_per_endpoint', 'endpoint_monitor_deficit', 'count', '>='),
    ):
        declaration = declarations[key]
        value = _at(declaration, f'planning.{name}')
        adoption = next(x for x in consumption if x['field'] == '$service' and x['service_key'] == key)
        source = _at(declaration, 'planning.source') or declaration.get('source')
        version = _at(declaration, 'planning.version') or declaration.get('version')
        reasons = ['ue_link_unresolved']
        planning_status = (planning_policy_status(declaration.get('planning'))
                           if key == 'N:rtk_augmentation' else {'ready': True, 'reason': None})
        if not planning_status['ready']:
            reasons.append(planning_status['reason'])
        if scope_missing:
            reasons.append('rule_scope_missing')
        if value is None:
            reasons.append('threshold_missing')
            missing.append(f'{key}.planning.{name}')
        if adoption['reason_code'] == 'required_service_not_adopted':
            reasons.append('required_service_not_adopted')
        if not source:
            reasons.append('rule_source_missing')
        if not version:
            reasons.append('rule_version_missing')
        applicability = ('not_applicable' if adoption['status'] == 'not_applicable' else
                         'applicable' if adoption['adopted'] and context_complete else 'unknown')
        rules.append({
            **identity, 'service_key': key, 'source_service_key': key, 'failure_mode': mode,
            'scenario_rule_id': f"{identity['scenario_id']}:{key}:{mode}" if identity['scenario_id'] else None,
            'ue_id': None, 'fc_id': None,
            'candidate_event_ids': [x['event_id'] for x in events if x['definition'].get('subsystem') == 'N'],
            'link_status': 'ue_link_unresolved',
            'confirmed': _at(declaration, 'planning.confirmed') if key == 'N:rtk_augmentation'
                         else declaration.get('confirmed'),
            'research_scenario_id': research.get('scenario_id'), 'evidence_track': track,
            'threshold': {'value': value, 'unit': unit, 'operator': operator},
            'source_id': f'required_cns.{key}.planning.{name}', 'source': source,
            'version': version, 'evidence_level': declaration.get('evidence_level', 'unknown'),
            'scope': {'service_scope': service_registry_entry(key)['service_scope'],
                      'space': scenario.get('space_scope'), 'altitude': scenario.get('cruise_altitude'),
                      'operating_phase': identity['operating_phase']},
            'required': declaration.get('required'), 'applicability': applicability,
            'engineering_rule_status': 'sourced_and_applicable' if applicability == 'applicable'
                and value is not None and source and version and planning_status['ready'] else 'unknown',
            'production_consumer': SERVICES[key][2], 'consumer_exists': True,
            'consumer_scope': service_registry_entry(key)['service_scope'],
            'per_service_consumer': True,
            'consumption_status': 'consumed' if value is not None and adoption['adopted'] and planning_status['ready'] else
                                  'missing' if value is None else 'unknown',
            'acceptability_status': 'not_evaluated', 'reason_codes': reasons,
        })
    nav_missing = [name for name in _NAV_EVIDENCE_FIELDS if _at(aircraft, name) is None]
    speed = fc30_planning_speeds(aircraft)
    if speed['route_speed_mps'] is None:
        missing.append('aircraft_profile.cruise_speed_mps')
    missing.extend(f"{row['service_key']}.{row['field']}" for row in consumption if row['status'] == 'missing')
    result = {
        'schema_version': SCHEMA_VERSION, **identity, 'evidence_track': track,
        'research_evidence_status': 'not_evaluated' if not research else 'unknown',
        'safety_policy': frozen['safety_policy'], 'research_scenario': deepcopy(frozen['research_scenario']),
        'p17_parser': {'algorithm_id': CONTINUOUS_SERVICE_ALGORITHM_ID,
                       'algorithm_version': CONTINUOUS_SERVICE_ALGORITHM_VERSION},
        'frozen_inputs': frozen, 'events': events, 'rules': rules,
        'parameters': parameters, 'requirement_consumption': consumption,
        'unresolved_events': [x['event_id'] for x in events],
        'unconsumed_requirements': [deepcopy(x) for x in consumption if x['status'] == 'unconsumed'],
        'missing_parameters': sorted(set(missing)),
        'navigation_evidence': {
            'status': 'missing' if nav_missing else 'unknown',
            'reason_code': 'capability_evidence_missing' if nav_missing else 'capability_evidence_not_verified',
            'missing_fields': nav_missing,
            'declared_facts': deepcopy((aircraft.get('metadata') or {}).get('fc30_facts') or []),
            'simulation_parameters_applied': False,
        },
        'speed': {**speed, 'source_id': 'selected_aircraft_profile.cruise_speed_mps',
                  'source': aircraft.get('source'), 'version': aircraft.get('version')},
        'acceptability_status': 'not_evaluated', 'persisted_to_project_state': False,
    }
    result['input_fingerprint'] = hashlib.sha256(json.dumps(
        result, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False,
    ).encode('utf-8')).hexdigest()
    return result


__all__ = ['build_frozen_ue_rule_snapshot']
