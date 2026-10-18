"""M3 read-only candidate contracts. JSON self-consistency is not provenance.

No session, file/HTTP access, upstream planning, persistence or trusted-JSON
escape hatch. Native model conclusions remain separate from research gates.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from .ue_rule_adapter import build_frozen_ue_rule_snapshot

VERSION = '1.0'
SCHEMA = 'candidate_ue_assessment.v1'
SCOPE_KEYS = ('route_id', 'scenario_id', 'operating_phase', 'operating_layer_id',
              'cruise_altitude', 'space_scope')
ALGORITHMS = {
    'p14': ('cns_service_corridor_v1', '1.4'),
    'p15': ('cns_corridor_gap_v1', '1.3'),
    'p17': ('continuous_service_acceptability_v1', '2.4'),
    'm2': ('rtk_joint_acceptance', 'rtk_joint_acceptance.v1'),
}


def _json(value):
    """Reject non-JSON objects/keys and nonfinite numbers before any coercion."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json(item)
        return
    raise ValueError('输入须为有限数值、字符串键的JSON容器')


def _digest(value):
    _json(value)
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _record(path, reason_code):
    return dict(path=path, reason_code=reason_code)


def _checks():
    return dict(errors=[], unknowns=[], checks=[])


def _expect(result, path, actual, expected):
    if actual is None or actual == '':
        result['unknowns'].append(_record(path, 'scope_missing'))
    elif _digest(actual) != _digest(expected):
        result['errors'].append(_record(path, 'scope_mismatch'))


def candidate_action_set_fingerprint(*, actions: list[dict]) -> str:
    _json(actions)
    if type(actions) is not list:
        raise ValueError('actions须为完整动作列表')
    ids, targets = set(), set()
    for row in actions:
        if type(row) is not dict or any(not isinstance(row.get(k), str) or not row[k]
                for k in ('action_id', 'action_type', 'device_id')):
            raise ValueError('动作须有action_id/action_type/device_id和物理站址身份')
        site = row.get('distinct_site_id')
        if site is None:
            if isinstance(row.get('site_id'), str) and row['site_id']:
                site = 'site:'+row['site_id']
            elif isinstance(row.get('facility_id'), str) and row['facility_id']:
                site = 'facility:'+row['facility_id']
        if not isinstance(site, str) or not site:
            raise ValueError('动作缺少明确的物理站址身份')
        target = (site, row['device_id'])
        if row['action_id'] in ids or target in targets:
            raise ValueError('重复或冲突动作')
        ids.add(row['action_id'])
        targets.add(target)
    return _digest(dict(schema_version='candidate_action_set.v1',
                        actions=sorted(actions, key=lambda row: row['action_id'])))


def _validate_m1(snapshot: dict) -> dict:
    _json(snapshot)
    if type(snapshot) is not dict:
        raise ValueError('M1快照须为JSON对象')
    result = _checks()
    if snapshot.get('schema_version') != 'frozen_ue_rules.v1':
        result['unknowns'].append(_record('m1.schema_version', 'unsupported_algorithm_version'))
        return result
    if snapshot.get('input_fingerprint') != _digest({k:v for k,v in snapshot.items()
                                                   if k != 'input_fingerprint'}):
        result['errors'].append(_record('m1.input_fingerprint', 'm1_fingerprint_mismatch'))
    frozen = snapshot.get('frozen_inputs')
    if not isinstance(frozen, dict) or not isinstance(frozen.get('required_cns'), dict):
        result['unknowns'].append(_record('m1.frozen_inputs', 'm1_reconstruction_missing'))
        return result
    required = dict(frozen['required_cns'])
    requirements = required.pop('requirements', None)
    authority = required.pop('authority', None)
    if authority == 'route_override':
        required['route_overrides'] = {snapshot.get('route_id'): requirements}
    elif authority == 'project_default':
        required['project_default'] = requirements
    else:
        result['unknowns'].append(_record('m1.required_cns.authority', 'm1_reconstruction_missing'))
        return result
    try:
        rebuilt = build_frozen_ue_rule_snapshot(route_id=snapshot.get('route_id'),
            required_cns=required, **{k:frozen.get(k) for k in (
                'safety_policy', 'continuous_service_policy', 'operation_scenario',
                'aircraft_profile', 'planning_evidence', 'research_scenario')})
    except (ValueError, TypeError, KeyError):
        result['unknowns'].append(_record('m1.frozen_inputs', 'm1_reconstruction_missing'))
    else:
        if rebuilt != snapshot:
            result['errors'].append(_record('m1', 'm1_reconstruction_mismatch'))
        else:
            result['checks'].append(_record('m1', 'content_reconstructed_not_source_authenticated'))
    return result


def _validate_context(snapshot: dict, context: dict, actions: list[dict]) -> dict:
    _json(context)
    result = _checks()
    if context.get('schema_version') != 'candidate_ue_context.v1':
        result['unknowns'].append(_record('context.schema_version', 'unsupported_schema'))
    scenario = snapshot.get('frozen_inputs', {}).get('operation_scenario', {})
    for key in SCOPE_KEYS:
        expected = snapshot.get(key) if key in SCOPE_KEYS[:3] else scenario.get(key)
        if key == 'operating_layer_id':
            if not context.get(key):
                result['unknowns'].append(_record('context.'+key, 'scope_missing'))
            elif expected is not None:
                _expect(result, 'context.'+key, context.get(key), expected)
        elif expected is None:
            result['unknowns'].append(_record('m1.'+key, 'scope_missing'))
        else:
            _expect(result, 'context.'+key, context.get(key), expected)
    for key, expected in (('m1_rule_fingerprint', snapshot.get('input_fingerprint')),
                          ('expected_action_set_fingerprint', candidate_action_set_fingerprint(actions=actions))):
        _expect(result, 'context.'+key, context.get(key), expected)
    for key in ('device_catalog_fingerprint', 'candidate_domain_fingerprint',
                'applicable_equipment_fingerprint', 'source_registry_fingerprint', 'evidence_policy_version'):
        if not context.get('frozen_dependencies', {}).get(key):
            result['unknowns'].append(_record('context.frozen_dependencies.'+key, 'dependency_missing'))
    if not context.get('route_geometry_fingerprint'):
        result['unknowns'].append(_record('context.route_geometry_fingerprint', 'dependency_missing'))
    versions = context.get('expected_algorithms', {})
    if versions.get('m1_schema_version') != 'frozen_ue_rules.v1':
        result['unknowns'].append(_record('context.expected_algorithms.m1', 'unsupported_algorithm_version'))
    for model, (identifier, version) in ALGORITHMS.items():
        if versions.get(model) != dict(algorithm_id=identifier, algorithm_version=version):
            result['unknowns'].append(_record('context.expected_algorithms.'+model, 'unsupported_algorithm_version'))
    return result

def _native_digest(value, *, p17=False):
    _json(value)
    options = dict(default=str) if p17 else {}
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, **options).encode()).hexdigest()


def _model(envelope):
    producer = envelope.get('producer', {})
    return next((key for key, (identifier, _) in ALGORITHMS.items()
                 if producer.get('algorithm_id') == identifier), None)


def _adapt_native_evidence(envelope: dict) -> dict:
    """Rebuild native input identity; never authenticate a caller's JSON."""
    result = _checks()
    model = _model(envelope)
    result.update(model=model, reconstructed_fingerprint=None)
    payload = envelope.get('payload', {})
    binding = envelope.get('input_binding', {})
    if model not in ('p14', 'p15', 'p17'):
        result['unknowns'].append(_record('producer', 'unsupported_algorithm_version'))
        return result
    identifier, version = ALGORITHMS[model]
    if (envelope.get('producer', {}).get('algorithm_version') != version or
            payload.get('algorithm_id') != identifier or payload.get('algorithm_version') != version):
        result['unknowns'].append(_record('producer', 'unsupported_algorithm_version'))
        return result
    if model in ('p14', 'p15'):
        material = binding.get('fingerprint_input')
        mandatory = (('routes', 'spatial_3d', 'grid', 'terrain', 'required_cns', 'aircraft_profile',
                      'existing_facilities', 'device_catalog', 'corridor_policy', 'parameters',
                      'coverage_parameters', 'capability_parameters', 'surface_facts_fingerprint')
                     if model == 'p14' else
                     ('corridor_assessment', 'required_cns', 'planning_objectives', 'parameters'))
        optional = ('radar_service_evidence', 'navigation_service_evidence', 'navigation_endpoint_evidence') if model == 'p14' else ()
        keys = set(mandatory) | {'algorithm_semantics', 'fixed_corridor_width_contract'}
        if not isinstance(material, dict) or not keys <= material.keys():
            result['unknowns'].append(_record('input_binding', 'native_material_missing'))
            return result
        if set(material) - keys - set(optional):
            result['unknowns'].append(_record('input_binding', 'unsupported_native_material'))
            return result
        if material['algorithm_semantics'] != dict(algorithm_id=identifier, algorithm_version=version):
            result['errors'].append(_record('input_binding.algorithm_semantics', 'native_version_mismatch'))
        from ..domain.cns_corridor import fixed_corridor_width_contract
        if material['fixed_corridor_width_contract'] != fixed_corridor_width_contract():
            result['unknowns'].append(_record('input_binding.fixed_corridor_width_contract', 'width_contract_mismatch'))
        rebuilt = _native_digest(material)
    else:
        inputs = binding.get('evaluation_inputs')
        required = {'corridor_assessment', 'corridor_gap_assessment', 'site_plan',
                    'aircraft_profile', 'planning_evidence', 'operation_scenario',
                    'continuous_service_policy', 'device_catalog', 'radar_surveillance_layout',
                    'post_plan_projection'}
        if not isinstance(inputs, dict) or not required <= inputs.keys():
            result['unknowns'].append(_record('input_binding.evaluation_inputs', 'native_material_missing'))
            return result
        from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
        try:
            reproduced = ContinuousServiceAcceptabilityV1().evaluate(**inputs)
        except (TypeError, ValueError, KeyError, AttributeError):
            result['unknowns'].append(_record('input_binding.evaluation_inputs', 'p17_reconstruction_unavailable'))
            return result
        rebuilt = reproduced.get('input_fingerprint')
        if reproduced != payload:
            result['errors'].append(_record('payload', 'p17_reconstruction_mismatch'))
    result['reconstructed_fingerprint'] = rebuilt
    if rebuilt != envelope.get('native_input_fingerprint') or rebuilt != payload.get('input_fingerprint'):
        result['errors'].append(_record('native_input_fingerprint', 'native_fingerprint_mismatch'))
    result['checks'].append(_record('input_binding', 'native_input_reconstructed_not_source_authenticated'))
    return result


def _validate_evidence(envelope: dict, context: dict, registry: dict) -> dict:
    _json(envelope)
    result = _checks()
    result['provenance_status'] = 'unverified_declared'
    result['unknowns'].append(_record('provenance_record_id', 'source_authenticity_unverified'))
    if envelope.get('schema_version') != 'candidate_evidence_envelope.v1':
        result['unknowns'].append(_record('schema_version', 'unsupported_schema'))
    for name in ('evidence_id', 'execution_role', 'run_id', 'provenance_record_id'):
        if not envelope.get(name):
            result['unknowns'].append(_record(name, 'evidence_identity_missing'))
    for name in SCOPE_KEYS:
        _expect(result, 'scope.'+name, envelope.get('scope', {}).get(name), context.get(name))
    _expect(result, 'm1_rule_fingerprint', envelope.get('m1_rule_fingerprint'), context.get('m1_rule_fingerprint'))
    stage = envelope.get('plan_stage')
    if stage == 'baseline':
        if envelope.get('action_set_fingerprint') is not None or envelope.get('role') != 'baseline':
            result['errors'].append(_record('plan_stage', 'baseline_post_plan_conflict'))
    elif stage == 'post_plan':
        if envelope.get('role') not in ('post_plan', 'legacy_p17'):
            result['errors'].append(_record('role', 'baseline_post_plan_conflict'))
        _expect(result, 'action_set_fingerprint', envelope.get('action_set_fingerprint'),
                context.get('expected_action_set_fingerprint'))
        if not envelope.get('baseline_evidence_id'):
            result['unknowns'].append(_record('baseline_evidence_id', 'baseline_binding_missing'))
    else:
        result['unknowns'].append(_record('plan_stage', 'plan_stage_missing'))
    payload = envelope.get('payload')
    if not isinstance(payload, dict):
        result['unknowns'].append(_record('payload', 'evidence_payload_missing'))
        return result
    if envelope.get('payload_digest') != _digest(payload):
        result['errors'].append(_record('payload_digest', 'payload_digest_mismatch'))
    routes = payload.get('routes')
    if not isinstance(routes, list) or not routes:
        result['unknowns'].append(_record('payload.routes', 'route_evidence_missing'))
    else:
        if len(routes) != 1 or routes[0].get('route_id') != context.get('route_id'):
            result['errors'].append(_record('payload.routes', 'route_scope_mismatch'))
        for route in routes:
            for voxel in route.get('voxels', []):
                _expect(result, 'voxel.altitude_layer_id', voxel.get('altitude_layer_id'),
                        context.get('operating_layer_id'))
    native = _adapt_native_evidence(envelope)
    for name in ('errors', 'unknowns', 'checks'):
        result[name].extend(native[name])
    model = native['model']
    if model == 'p15':
        parent = envelope.get('input_binding', {}).get('fingerprint_input', {}).get('corridor_assessment', {})
        provenance = payload.get('input_provenance', {})
        for field, value in (('corridor_input_fingerprint', parent.get('input_fingerprint')),
                             ('corridor_algorithm_id', parent.get('algorithm_id')),
                             ('corridor_algorithm_version', parent.get('algorithm_version'))):
            _expect(result, 'payload.input_provenance.'+field, provenance.get(field), value)
    if model == 'p17' and stage == 'post_plan':
        projection = payload.get('post_plan_projection')
        if not isinstance(projection, dict) or projection.get('available') is not True or not payload.get('baseline'):
            result['errors'].append(_record('payload.post_plan_projection', 'baseline_post_plan_conflict'))
        elif any(route.get('plan_stage') != 'post_plan' for route in payload.get('routes', [])):
            result['errors'].append(_record('payload.routes.plan_stage', 'baseline_post_plan_conflict'))
        else:
            comparison = projection.get('comparison', {}).get('projection', {})
            _expect(result, 'comparison.post_plan_fingerprint', comparison.get('post_plan_fingerprint'),
                    payload.get('input_fingerprint'))
            _expect(result, 'comparison.baseline_fingerprint', comparison.get('baseline_fingerprint'),
                    payload['baseline'].get('input_fingerprint'))
    # A registry can expose contradictions, but cannot authenticate itself.
    record = registry.get(envelope.get('provenance_record_id'))
    if isinstance(record, dict) and record.get('payload_digest') is not None:
        _expect(result, 'registry.payload_digest', record['payload_digest'], envelope.get('payload_digest'))
    return result

def _evaluate_rtk(snapshot: dict, rtk_inputs: dict | None, evidence: dict) -> dict:
    from .rtk_joint_acceptance import adapt_p14_p15_rtk_segments, evaluate_p17_rtk_joint
    result = _checks()
    result['result'] = None
    if not rtk_inputs:
        result['unknowns'].append(_record('rtk_joint_inputs', 'rtk_evidence_missing'))
        return result
    if rtk_inputs.get('schema_version') != 'candidate_rtk_joint_inputs.v1':
        result['unknowns'].append(_record('rtk_joint_inputs.schema_version', 'unsupported_schema'))
        return result
    binding = rtk_inputs.get('segment_binding', {})
    adapter = binding.get('adapter_id')
    model = {'rtk_p14_voxels_v1': 'p14', 'rtk_p15_voxels_v1': 'p15'}.get(adapter)
    if model is None or binding.get('adapter_version') != '1':
        result['unknowns'].append(_record('rtk.segment_binding', 'unsupported_adapter_version'))
        return result
    for key in ('p14', 'p15'):
        parent = evidence.get(key)
        if not isinstance(parent, dict):
            result['unknowns'].append(_record('rtk.'+key, 'rtk_parent_missing'))
        else:
            _expect(result, 'rtk.parent_'+key, binding.get('parent_'+key+'_evidence_id'), parent.get('evidence_id'))
    parent = evidence.get(model, {})
    payload = parent.get('payload', {})
    rows = [r for r in payload.get('routes', []) if r.get('route_id') == snapshot.get('route_id')]
    if len(rows) != 1:
        result['unknowns'].append(_record('rtk.parent.routes', 'rtk_parent_missing'))
        return result
    _expect(result, 'rtk.route_length_m', rtk_inputs.get('route_length_m'), rows[0].get('route_length_m'))
    _expect(result, 'rtk.source_fingerprint', binding.get('source_fingerprint'), _digest(payload))
    segments = rtk_inputs.get('segments')
    if not isinstance(segments, list):
        result['unknowns'].append(_record('rtk.segments', 'rtk_evidence_missing'))
        return result
    if binding.get('segments_digest') != _digest(segments):
        result['errors'].append(_record('rtk.segments_digest', 'payload_digest_mismatch'))
    try:
        rebuilt = adapt_p14_p15_rtk_segments(route_id=snapshot.get('route_id'),
            route_length_m=rtk_inputs.get('route_length_m'), assessed_voxels=rows[0].get('voxels', []),
            source_fingerprint=_digest(payload))
    except (TypeError, ValueError, KeyError):
        result['unknowns'].append(_record('rtk.segment_binding', 'rtk_reconstruction_unavailable'))
        return result
    if rebuilt != segments:
        result['errors'].append(_record('rtk.segments', 'rtk_segment_reconstruction_mismatch'))
    if result['errors'] or result['unknowns']:
        return result
    try:
        result['result'] = evaluate_p17_rtk_joint(frozen_snapshot=snapshot,
            **{key:rtk_inputs.get(key) for key in ('route_length_m', 'segments', 'speed_profile',
               'backup_scenario', 'synthetic_fixture', 'service_evidence_track')})
    except (ValueError, TypeError, KeyError):
        result['unknowns'].append(_record('rtk_joint_inputs', 'rtk_evaluation_unavailable'))
    return result


def _service_status(value):
    return {'fully_satisfied': 'satisfied', 'acceptable_with_managed_gap': 'managed',
            'managed_gap': 'managed', 'nominal': 'satisfied',
            'acceptable_degraded': 'managed', 'satisfied': 'satisfied', 'managed': 'managed',
            'unacceptable': 'unacceptable', 'not_applicable': 'not_applicable',
            'not_evaluated': 'not_evaluated'}.get(value, 'unknown')


def _extract_service_events(p17: dict, snapshot: dict) -> dict:
    """Project native events without implementing another acceptability formula."""
    result = dict(service_assessments=[], event_assessments=[], unknowns=[],
                  violations=[], managed_gaps=[], legacy_route_details=[])
    for route_index, route in enumerate(p17.get('routes', [])):
        details = {k:deepcopy(route[k]) for k in ('route_id', 'plan_stage', 'corridor',
            'first_detection_evidence', 'surveillance_acceptance', 'threat_layers',
            'limitations') if k in route}
        result['legacy_route_details'].append(details)
        for sub_index, subsystem in enumerate(route.get('subsystems', [])):
            key = subsystem.get('service') or {'C':'C:communication', 'N':'N:navigation',
                                               'S':'S:surveillance'}.get(subsystem.get('subsystem'), 'unknown')
            path = f'routes[{route_index}].subsystems[{sub_index}]'
            service = dict(service_key=key, source_model='legacy_p17',
                status=_service_status(subsystem.get('status')), native_result_path=path,
                scope={k:snapshot.get(k) for k in SCOPE_KEYS[:3]}, events=[],
                unknowns=deepcopy(subsystem.get('reason_codes') or []), violations=[],
                native_result=deepcopy(subsystem))
            independent = True
            if key in ('S:rid_cooperative', 'S:radar_noncooperative'):
                rules = [r for r in snapshot.get('rules', []) if r.get('service_key') == key]
                independent = bool(rules) and all(r.get('per_service_consumer') is True for r in rules)
                if not independent:
                    reason = _record(path, 'per_service_consumer_unproven')
                    service['status'] = 'unknown'
                    service['unknowns'].append(reason)
                    result['unknowns'].append(reason)
            for event in subsystem.get('events', []):
                rule = next((r for r in snapshot.get('rules', []) if
                    r.get('service_key') == key and r.get('failure_mode') == event.get('kind')), None)
                rule_ref = rule.get('scenario_rule_id') if rule else None
                identity = dict(route_id=route.get('route_id'), service_key=key,
                    rule_id=rule_ref, failure_mode=event.get('kind'), event_id=event.get('event_id'),
                    start_m=event.get('start_offset_m'), end_m=event.get('end_offset_m'))
                item = dict(identity, event_key=_digest(identity), source_model='legacy_p17',
                    native_event=deepcopy(event), native_result_path=path+'.events',
                    status='unknown' if event.get('exceeds_limit') is None else
                        'unacceptable' if event['exceeds_limit'] else 'managed',
                    threshold=deepcopy(rule.get('threshold')) if rule else
                        dict(value=event.get('limit_s'), unit='s', operator='<='),
                    threshold_source={k:deepcopy(rule.get(k)) for k in
                        ('source_id', 'source', 'authority', 'version', 'scope', 'confirmed')} if rule else None,
                    unknowns=[] if rule else ['rule_source_mapping_unknown'])
                if not independent:
                    item['native_model_status'] = item['status']
                    item['status'] = 'unknown'
                    item['unknowns'].append('per_service_consumer_unproven')
                service['events'].append(item)
                result['event_assessments'].append(item)
                if item['status'] == 'unacceptable':
                    violation = _record(item['event_key'], 'legacy_p17_event_unacceptable')
                    result['violations'].append(violation)
                    service['violations'].append(violation)
                elif item['status'] == 'managed':
                    result['managed_gaps'].append(item)
            if _service_status(subsystem.get('status')) == 'unacceptable':
                result['violations'].append(_record(path, 'legacy_p17_unacceptable'))
            result['service_assessments'].append(service)
    # An S aggregate or threat-layer verdict cannot prove each independent requirement.
    evaluated = {s['service_key'] for s in result['service_assessments']}
    for key in ('C:communication', 'N:rtk_augmentation', 'N:navigation_integrity_monitoring',
                'S:rid_cooperative', 'S:radar_noncooperative'):
        if key not in evaluated:
            reason = _record(key, 'per_service_evaluation_unproven')
            result['unknowns'].append(reason)
            result['service_assessments'].append(dict(service_key=key, source_model='legacy_p17',
                status='unknown', events=[], unknowns=[reason], violations=[], native_result_path=None))
    return result

def _aggregate_assessment(*, identity: dict, services: dict, scope: dict, provenance: dict) -> dict:
    statuses = services.get('statuses', [])
    if identity.get('errors'):
        status = 'input_inconsistent'
    elif services.get('violations') and provenance.get('authenticated'):
        status = 'unacceptable'
    elif not services.get('has_evaluation'):
        status = 'not_evaluated'
    elif (identity.get('unknowns') or services.get('unknowns') or
          not provenance.get('authenticated') or not statuses or
          any(s not in ('satisfied', 'managed', 'not_applicable') for s in statuses)):
        status = 'unknown'
    else:
        status = ('conditionally_acceptable_under_simulation' if provenance.get('uses_simulation')
                  else 'engineering_model_acceptable')
    return dict(status=status)


def _research_gates(assessment: dict) -> dict:
    fixture = assessment['validation_status'] == 'illustrative_not_validated'
    status = assessment['assessment_status']
    acceptable = status in ('engineering_model_acceptable', 'conditionally_acceptable_under_simulation')
    eligible = acceptable and not fixture and not assessment['violations']
    prefilter = ('fixture_only' if fixture else 'reject' if status in ('input_inconsistent', 'unacceptable')
                 else 'retain_for_verification' if eligible else 'needs_evidence')
    return dict(prefilter_status=prefilter, research_admission=dict(eligible=eligible,
        meaning='candidate_prefilter_only', scope_id=assessment['assessment_scope'].get('scope_id'),
        conditions=['须完成有效独立终验，预筛不代表最终研究方案可行'],
        blockers=assessment['reason_codes'] if not eligible else []),
        final_research_admission=dict(eligible=False,
            status='pending_independent_verification' if eligible else 'blocked',
            blockers=['independent_execution_proof_unavailable']))


def _merge_checks(target, source, prefix):
    for key in ('errors', 'unknowns', 'checks'):
        target[key].extend(dict(row, path=prefix+'.'+row['path']) for row in source.get(key, []))


def _validate_bundle(baseline, post, p17, context, actions, snapshot):
    result = _checks()
    for group, name, stage in ((baseline, 'baseline', 'baseline'), (post, 'post_plan', 'post_plan')):
        if not group:
            result['unknowns'].append(_record(name, 'upstream_evidence_missing'))
            continue
        for model in ('p14', 'p15'):
            env = group.get(model)
            if not isinstance(env, dict):
                result['unknowns'].append(_record(name+'.'+model, 'upstream_evidence_missing'))
                continue
            _expect(result, name+'.'+model+'.plan_stage', env.get('plan_stage'), stage)
            _expect(result, name+'.'+model+'.model', _model(env), model)
            if stage == 'post_plan' and isinstance((baseline or {}).get(model), dict):
                _expect(result, name+'.'+model+'.baseline_evidence_id', env.get('baseline_evidence_id'),
                        baseline[model].get('evidence_id'))
        if all(isinstance(group.get(m), dict) for m in ('p14', 'p15')):
            bound = group['p15'].get('input_binding', {}).get('fingerprint_input', {}).get('corridor_assessment')
            if bound is None:
                result['unknowns'].append(_record(name+'.p15.input_binding', 'native_material_missing'))
            elif bound != group['p14'].get('payload'):
                result['errors'].append(_record(name+'.p15.input_binding', 'parent_payload_mismatch'))
    if not p17:
        result['unknowns'].append(_record('legacy_p17', 'post_plan_p17_missing'))
        return result
    _expect(result, 'legacy_p17.model', _model(p17), 'p17')
    _expect(result, 'legacy_p17.plan_stage', p17.get('plan_stage'), 'post_plan')
    inputs = p17.get('input_binding', {}).get('evaluation_inputs', {})
    projection = inputs.get('post_plan_projection') or {}
    for group, data, name in ((baseline, inputs, 'baseline'), (post, projection, 'post_plan')):
        for key, model in (('corridor_assessment', 'p14'), ('corridor_gap_assessment', 'p15')):
            expected = (group or {}).get(model, {}).get('payload')
            if expected is None or key not in data:
                result['unknowns'].append(_record('legacy_p17.'+name+'.'+model, 'parent_binding_missing'))
            elif data[key] != expected:
                result['errors'].append(_record('legacy_p17.'+name+'.'+model, 'parent_payload_mismatch'))
    if baseline and isinstance(baseline.get('p14'), dict):
        _expect(result, 'legacy_p17.baseline_evidence_id', p17.get('baseline_evidence_id'),
                baseline['p14'].get('evidence_id'))
    action_ids = projection.get('applied_action_ids')
    if action_ids is None:
        result['unknowns'].append(_record('p17.applied_action_ids', 'joint_action_binding_missing'))
    elif sorted(action_ids) != sorted(row['action_id'] for row in actions):
        result['errors'].append(_record('p17.applied_action_ids', 'joint_action_binding_mismatch'))
    frozen = snapshot.get('frozen_inputs', {})
    for key in ('aircraft_profile', 'planning_evidence', 'operation_scenario', 'continuous_service_policy'):
        if key not in inputs:
            result['unknowns'].append(_record('p17.'+key, 'native_material_missing'))
        elif inputs[key] != frozen.get(key):
            result['errors'].append(_record('p17.'+key, 'm1_p17_source_mismatch'))
    result['unknowns'].append(_record('actions', 'joint_execution_binding_unverified'))
    return result


def _validate_scope(scope, snapshot):
    result = _checks()
    if scope.get('schema_version') != 'candidate_ue_scope.v1':
        result['unknowns'].append(_record('scope.schema_version', 'unsupported_schema'))
    for field in ('scope_id', 'version', 'source_ref'):
        if not scope.get(field):
            result['unknowns'].append(_record('scope.'+field, 'assessment_scope_missing'))
    if scope.get('kind') not in ('confirmed_engineering_model', 'complete_required_cns'):
        result['unknowns'].append(_record('scope.kind', 'assessment_scope_missing'))
    included = scope.get('included_constraint_ids')
    if not isinstance(included, list) or not included or any(not isinstance(x, str) for x in included):
        result['unknowns'].append(_record('scope.included_constraint_ids', 'constraint_scope_missing'))
    else:
        known = {r.get('scenario_rule_id') for r in snapshot.get('rules', [])}
        known.update(r.get('service_key') for r in snapshot.get('rules', []))
        known.update(r.get('field') for r in snapshot.get('requirement_consumption', []))
        if len(included) != len(set(included)) or any(x not in known for x in included):
            result['unknowns'].append(_record('scope.included_constraint_ids', 'constraint_mapping_unknown'))
    exclusions = scope.get('excluded_constraints')
    if not isinstance(exclusions, list):
        result['unknowns'].append(_record('scope.excluded_constraints', 'exclusion_disclosure_missing'))
    else:
        for item in exclusions:
            if not isinstance(item, dict) or any(not item.get(k) for k in ('constraint_id', 'reason', 'source_ref')):
                result['unknowns'].append(_record('scope.excluded_constraints', 'exclusion_disclosure_missing'))
        if exclusions and scope.get('kind') == 'complete_required_cns':
            result['errors'].append(_record('scope.excluded_constraints', 'complete_scope_exclusion'))
    return result


def evaluate_candidate_ue_acceptability(*, frozen_rule_snapshot: dict, candidate_context: dict,
        actions: list[dict], baseline_evidence: dict | None, post_plan_evidence: dict | None,
        legacy_p17_evidence: dict | None, rtk_joint_inputs: dict | None, evidence_registry: dict,
        research_review_registry: dict | None = None, assessment_scope: dict) -> dict:
    """Evaluate precomputed candidate evidence; trust/independence gates stay closed.

    Registry dictionaries are declarations, never independent authentication.
    Returned legacy/M2 model facts are retained even when research cannot admit.
    """
    arguments = dict(frozen_rule_snapshot=frozen_rule_snapshot, candidate_context=candidate_context,
        actions=actions, baseline_evidence=baseline_evidence, post_plan_evidence=post_plan_evidence,
        legacy_p17_evidence=legacy_p17_evidence, rtk_joint_inputs=rtk_joint_inputs,
        evidence_registry=evidence_registry, research_review_registry=research_review_registry,
        assessment_scope=assessment_scope)
    _json(arguments)
    for key, value in arguments.items():
        if key == 'actions':
            continue
        if value is None and key in ('baseline_evidence', 'post_plan_evidence', 'legacy_p17_evidence',
                                     'rtk_joint_inputs', 'research_review_registry'):
            continue
        if not isinstance(value, dict):
            raise ValueError(key+'须为JSON对象')
    snapshot, context = frozen_rule_snapshot, candidate_context
    action_fp = candidate_action_set_fingerprint(actions=actions)
    identity = _checks()
    _merge_checks(identity, _validate_m1(snapshot), 'identity')
    _merge_checks(identity, _validate_context(snapshot, context, actions), 'identity')
    _merge_checks(identity, _validate_scope(assessment_scope, snapshot), 'scope')
    envelope_checks = []
    envelopes = []
    for group in (baseline_evidence, post_plan_evidence):
        for model in ('p14', 'p15'):
            env = (group or {}).get(model)
            if isinstance(env, dict):
                envelopes.append(env)
    if legacy_p17_evidence:
        envelopes.append(legacy_p17_evidence)
    for env in envelopes:
        check = _validate_evidence(env, context, evidence_registry)
        _merge_checks(identity, check, 'evidence.'+str(env.get('evidence_id')))
        envelope_checks.append(dict(evidence_id=env.get('evidence_id'), **check))
    ids = [env.get('evidence_id') for env in envelopes]
    if len(ids) != len(set(ids)):
        identity['errors'].append(_record('evidence_ids', 'duplicate_evidence_id'))
    _merge_checks(identity, _validate_bundle(baseline_evidence, post_plan_evidence,
        legacy_p17_evidence, context, actions, snapshot), 'binding')
    rtk = _evaluate_rtk(snapshot, rtk_joint_inputs, post_plan_evidence or {}) if not identity['errors'] else _checks()
    _merge_checks(identity, rtk, 'rtk')
    legacy = (legacy_p17_evidence or {}).get('payload') or {}
    view = _extract_service_events(legacy, snapshot)
    model_rtk = rtk.get('result')
    if model_rtk:
        view['service_assessments'].append(dict(service_key='N:rtk_augmentation', source_model='m2',
            status=model_rtk['navigation_status'], actual_service_status=model_rtk['rtk_status'],
            events=deepcopy(model_rtk['gaps']), unknowns=deepcopy(model_rtk['unknowns']),
            violations=deepcopy(model_rtk['violations']), native_result_path='rtk_joint_result'))
        view['unknowns'].extend(_record('rtk', code) for code in model_rtk['unknowns'])
        view['violations'].extend(_record('rtk', code) for code in model_rtk['violations'])
        view['managed_gaps'].extend(deepcopy(gap) for gap in model_rtk['gaps']
            if gap['navigation_evaluation']['status'] == 'managed')
    unconsumed = deepcopy(snapshot.get('unconsumed_requirements', []))
    missing = deepcopy(snapshot.get('missing_parameters', []))
    view['unknowns'].extend(_record('requirements.'+str(r.get('field')), 'unconsumed_requirement')
                           for r in unconsumed)
    if research_review_registry is not None or (rtk_joint_inputs or {}).get('backup_scenario'):
        view['unknowns'].append(_record('research_review_registry', 'research_review_authenticity_unverified'))
    fixture = ('synthetic_fixture' in json.dumps(arguments, ensure_ascii=False) or
               bool((rtk_joint_inputs or {}).get('synthetic_fixture')))
    statuses = [s['status'] for s in view['service_assessments']]
    services = dict(statuses=statuses, violations=view['violations'], unknowns=view['unknowns'],
                    has_evaluation=bool(legacy_p17_evidence and post_plan_evidence))
    engineering = _aggregate_assessment(identity=identity, services=services,
        scope=assessment_scope, provenance=dict(authenticated=False,
            uses_simulation=bool((rtk_joint_inputs or {}).get('backup_scenario'))))
    status = engineering['status']
    if fixture and status != 'input_inconsistent':
        status = 'not_evaluated'
    unknowns = identity['unknowns'] + view['unknowns']
    if fixture:
        unknowns.append(_record('evidence_track', 'fixture_not_eligible_for_research'))
    result = dict(schema_version=SCHEMA, algorithm_id='candidate_ue_acceptability', algorithm_version=VERSION,
        **{k:deepcopy(context.get(k)) for k in SCOPE_KEYS},
        action_set_fingerprint=action_fp, m1_rule_fingerprint=snapshot.get('input_fingerprint'),
        p14_p15_evidence_fingerprint={name:{m:(group or {}).get(m, {}).get('payload_digest')
            for m in ('p14', 'p15')} for name, group in
            (('baseline', baseline_evidence), ('post_plan', post_plan_evidence))},
        legacy_p17_fingerprint=legacy.get('input_fingerprint'),
        rtk_joint_fingerprint=(model_rtk or {}).get('input_fingerprint'),
        candidate_input_fingerprint=_digest(arguments),
        frozen_candidate_fingerprint=_digest(dict(snapshot=snapshot, context=context, actions=sorted(actions, key=lambda r:r['action_id']),
            assessment_scope=assessment_scope, evidence_registry=evidence_registry,
            research_review_registry=research_review_registry)),
        evidence_track='synthetic_fixture' if fixture else context.get('evidence_track'),
        validation_status='illustrative_not_validated' if fixture else 'source_authentication_required',
        assessment_status=status, assessment_scope=deepcopy(assessment_scope),
        coverage_status='partial' if services['has_evaluation'] else 'missing',
        formal_ue_status='unresolved', ue_event_confirmations=deepcopy(snapshot.get('events', [])),
        legacy_p17_result=deepcopy(legacy), rtk_joint_result=deepcopy(model_rtk),
        engineering_research_result=engineering, real_aircraft_evidence=deepcopy(snapshot.get('navigation_evidence')),
        service_assessments=view['service_assessments'], event_assessments=view['event_assessments'],
        legacy_route_details=view['legacy_route_details'], missing_parameters=missing,
        unconsumed_requirements=unconsumed, unknowns=unknowns, violations=view['violations'],
        managed_gaps=view['managed_gaps'], identity_checks=identity,
        provenance_checks=envelope_checks, excluded_constraint_disclosures=deepcopy(
            assessment_scope.get('excluded_constraints', [])), verification_status='not_requested',
        evidence_bindings=[{k:deepcopy(env.get(k)) for k in ('evidence_id', 'role', 'execution_role',
            'run_id', 'scope', 'action_set_fingerprint', 'baseline_evidence_id', 'm1_rule_fingerprint',
            'native_input_fingerprint', 'payload_digest', 'parent_evidence_ids', 'provenance_record_id',
            'producer')} for env in envelopes],
        maturity='proposal', persisted_to_project_state=False)
    result['reason_codes'] = sorted({r['reason_code'] for r in identity['errors']+unknowns+view['violations']})
    result.update(_research_gates(result))
    result['assessment_fingerprint'] = _digest(result)
    return result

def _diff(quick, final, path, differences):
    if type(quick) is not type(final):
        differences.append(dict(path=path, quick_value=quick, final_value=final,
                                reason_code='content_value_mismatch'))
    elif isinstance(quick, dict):
        for key in sorted(quick.keys() | final.keys()):
            child = path+'.'+key
            if key not in quick or key not in final:
                differences.append(dict(path=child, quick_present=key in quick, final_present=key in final,
                    quick_value=quick.get(key), final_value=final.get(key), reason_code='content_field_mismatch'))
            else:
                _diff(quick[key], final[key], child, differences)
    elif isinstance(quick, list):
        for index in range(max(len(quick), len(final))):
            if index >= min(len(quick), len(final)):
                differences.append(dict(path=f'{path}[{index}]', reason_code='content_item_missing',
                    quick_value=quick[index] if index < len(quick) else None,
                    final_value=final[index] if index < len(final) else None))
            else:
                _diff(quick[index], final[index], f'{path}[{index}]', differences)
    elif quick != final:
        differences.append(dict(path=path, quick_value=quick, final_value=final,
                                reason_code='content_value_mismatch'))


def _comparison_content(assessment, differences, label):
    content = {key:deepcopy(value) for key, value in assessment.items()
               if key not in ('assessment_fingerprint', 'candidate_input_fingerprint')}
    for record in content.get('evidence_bindings', []):
        record.pop('run_id', None)
        record.pop('execution_role', None)
    for field in ('event_assessments', 'service_assessments', 'evidence_bindings'):
        rows = content.get(field)
        if not isinstance(rows, list):
            continue
        indexed = {}
        for row in rows:
            if field == 'event_assessments':
                key = row.get('event_key')
            elif field == 'service_assessments':
                key = _digest([row.get('service_key'), row.get('source_model'), row.get('scope'), row.get('native_result_path')])
            else:
                key = row.get('evidence_id')
            if not key or key in indexed:
                differences.append(dict(path=label+'.'+field, event_key=key,
                    reason_code='duplicate_event_key' if field == 'event_assessments' else 'duplicate_identity_key'))
            else:
                indexed[key] = row
        content[field] = indexed
    # Only these collections are specified as sets. Segment/evidence arrays retain order.
    for field in ('unknowns', 'violations', 'missing_parameters', 'unconsumed_requirements', 'reason_codes'):
        if isinstance(content.get(field), list):
            content[field] = sorted(content[field], key=_digest)
    return content


def _compare_assessment_content(quick: dict, final: dict) -> dict:
    differences = []
    required = {'schema_version', 'algorithm_id', 'algorithm_version', 'action_set_fingerprint',
                'm1_rule_fingerprint', 'assessment_fingerprint', 'frozen_candidate_fingerprint',
                'p14_p15_evidence_fingerprint', 'legacy_p17_fingerprint', 'rtk_joint_fingerprint',
                'legacy_p17_result', 'rtk_joint_result', 'service_assessments', 'event_assessments',
                'assessment_scope', 'unknowns', 'violations', 'evidence_bindings', 'identity_checks',
                'provenance_checks', 'maturity', 'persisted_to_project_state'}
    missing = sorted(required - quick.keys() | required - final.keys())
    for label, assessment in (('quick', quick), ('final', final)):
        if assessment.get('assessment_fingerprint') is not None:
            if assessment['assessment_fingerprint'] != _digest({k:v for k,v in assessment.items()
                                                               if k != 'assessment_fingerprint'}):
                differences.append(dict(path=label+'.assessment_fingerprint', reason_code='assessment_fingerprint_mismatch'))
    quick_content = _comparison_content(quick, differences, 'quick')
    final_content = _comparison_content(final, differences, 'final')
    _diff(quick_content, final_content, 'assessment', differences)
    return dict(content_comparison_status='different' if differences else 'incomplete' if missing else 'equal',
                differences=differences, missing_fields=missing,
                compared_paths=sorted(set(quick_content) & set(final_content)))


def _verify_independent_execution(quick: dict, final: dict) -> dict:
    # No trusted execution-chain verifier is installed in M3 v1. JSON flags and
    # distinct run IDs are never credentials. Keep this closed even for fixtures.
    return dict(status='not_verified', reason_code='independent_execution_proof_unavailable',
        quick_runs=[b.get('run_id') for b in quick.get('evidence_bindings', [])],
        final_runs=[b.get('run_id') for b in final.get('evidence_bindings', [])])


def compare_candidate_final_verification(*, quick_assessment: dict, final_assessment: dict) -> dict:
    """Compare every retained model fact, without inventing independent execution."""
    _json(quick_assessment)
    _json(final_assessment)
    if not isinstance(quick_assessment, dict) or not isinstance(final_assessment, dict):
        raise ValueError('比较输入须为M3评价JSON对象')
    comparison = _compare_assessment_content(quick_assessment, final_assessment)
    independence = _verify_independent_execution(quick_assessment, final_assessment)
    mismatch = comparison['content_comparison_status'] == 'different'
    blockers = {d['reason_code'] for d in comparison['differences']}
    blockers.add(independence['reason_code'])
    if comparison['missing_fields']:
        blockers.add('assessment_content_incomplete')
    result = dict(schema_version='candidate_ue_verification.v1',
        verification_status='verification_mismatch' if mismatch else 'not_verified',
        candidate_fingerprint=quick_assessment.get('frozen_candidate_fingerprint'),
        quick_assessment_fingerprint=quick_assessment.get('assessment_fingerprint'),
        final_assessment_fingerprint=final_assessment.get('assessment_fingerprint'),
        independence_checks=independence, **comparison, blocking_reason_codes=sorted(blockers),
        research_admission_allowed=False, maturity='proposal', persisted_to_project_state=False)
    result['verification_fingerprint'] = _digest(result)
    return result


__all__ = ['candidate_action_set_fingerprint', 'evaluate_candidate_ue_acceptability',
           'compare_candidate_final_verification']