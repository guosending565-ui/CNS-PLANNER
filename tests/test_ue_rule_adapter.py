"""M1 uses synthetic inputs only; no ProjectState or business evaluation."""
from copy import deepcopy
import json

import pytest

from cns_planner.domain.fc30_profile import fc30_aircraft_profile
from cns_planner.domain.safety_policy import default_safety_policy

C = 'C:communication'
RTK = 'N:rtk_augmentation'
MONITOR = 'N:navigation_integrity_monitoring'
RID = 'S:rid_cooperative'
RADAR = 'S:radar_noncooperative'


def inputs():
    return dict(
        route_id='R0005', safety_policy=default_safety_policy(),
        required_cns={'source': 'fixture', 'version': 'req1', 'project_default': {
            'communication': {'required': True, 'confirmed': True, 'max_gap_m': 1000,
                              'services': {C: {'required': True, 'confirmed': True}}},
            'navigation': {'required': True, 'confirmed': True,
                           'services': {MONITOR: {'required': True, 'confirmed': True}}},
            'surveillance': {'required': True, 'confirmed': True, 'services': {
                RID: {'required': True, 'confirmed': True},
                RADAR: {'required': True, 'confirmed': True}}}}},
        continuous_service_policy={'source': 'fixture-policy', 'version': 'policy1',
            'confirmed': False, 'service_acceptability_limits': {
                'C': {'service_outage': 3.0, 'redundancy_degradation': 10.0}}},
        operation_scenario={'scenario_id': 'R0005-ALT100', 'version': 'scenario1',
                            'operating_phase': 'cruise', 'cruise_altitude': 'ALT-100',
                            'space_scope': {'route_id': 'R0005'}},
        aircraft_profile=fc30_aircraft_profile(), planning_evidence={'items': []},
    )


def build(**kwargs):
    # Import here so RED is a failing test for the missing callable interface.
    from cns_planner.application.ue_rule_adapter import build_frozen_ue_rule_snapshot
    return build_frozen_ue_rule_snapshot(**kwargs)


def rule(snapshot, key, mode):
    return next(r for r in snapshot['rules'] if r['service_key'] == key and r['failure_mode'] == mode)


def service(snapshot, key):
    return next(r for r in snapshot['requirement_consumption'] if r['service_key'] == key and r['field'] == '$service')


def test_unconfirmed_events_and_readonly_links_are_not_confirmation():
    data = inputs()
    result = build(**data)
    assert result['safety_policy']['confirmed'] is False
    ids = {e['event_id'] for e in result['events']}
    assert ids == {'FC-C-01', 'FC-N-01', 'FC-S-01', 'UE-C-01', 'UE-N-01', 'UE-S-01',
                   'CUE-CS-01', 'CUE-CN-01', 'CUE-NS-01'}
    assert all(e['confirmed'] is False and e['link_status'] == 'ue_link_unresolved' for e in result['events'])
    r = rule(result, C, 'service_outage')
    assert r['scenario_rule_id'] and r['ue_id'] is None and r['fc_id'] is None
    assert {'FC-C-01', 'UE-C-01'} <= set(r['candidate_event_ids'])
    assert r['confirmed'] is False
    assert result['acceptability_status'] == 'not_evaluated'


def test_communication_times_and_unconsumed_distance_are_independent():
    result = build(**inputs())
    assert rule(result, C, 'service_outage')['threshold'] == {'value': 3.0, 'unit': 's', 'operator': '<='}
    assert rule(result, C, 'redundancy_degradation')['threshold']['value'] == 10
    entry = next(x for x in result['requirement_consumption'] if x['field'] == 'communication.max_gap_m')
    assert entry['value'] == 1000 and entry['status'] == 'unconsumed'
    assert entry['reason_code'] == 'requirement_unconsumed'
    assert entry['production_consumer'] is None
    assert result['unconsumed_requirements']


def test_rtk_not_adopted_does_not_inherit_one_monitor_or_any_baseline():
    data = inputs()
    data['required_cns']['project_default']['navigation']['redundancy'] = 1
    result = build(**data)
    assert service(result, RTK)['reason_code'] == 'required_service_not_adopted'
    for field in ('max_reference_baseline_m', 'required_distinct_site_count'):
        r = next(x for x in result['requirement_consumption'] if x['field'] == 'planning.' + field)
        assert r['value'] is None and r['status'] == 'missing'
    assert RTK not in data['required_cns']['project_default']['navigation']['services']


def test_rid_radar_keep_separate_identity_and_no_s_pass():
    data = inputs()
    data['required_cns']['project_default']['surveillance']['services'][RADAR]['required'] = False
    result = build(**data)
    assert service(result, RID)['status'] == 'consumed'
    assert service(result, RADAR)['status'] == 'not_applicable'
    assert rule(result, RID, 'service_outage')['per_service_consumer'] is False
    assert rule(result, RADAR, 'service_outage')['applicability'] == 'not_applicable'
    assert all(x.get('acceptability_status') != 'passed' for x in result['rules'])


def test_fc30_declaration_is_retained_but_error_integrity_and_switch_are_missing():
    result = build(**inputs())
    nav = result['navigation_evidence']
    assert any(x['parameter'] == 'rtk_loss_fallback' for x in nav['declared_facts'])
    assert nav['status'] == 'missing'
    assert {'navigation.performance.max_horizontal_error_m',
            'navigation.performance.max_time_to_alert_s',
            'navigation_switch_time_s'} <= set(nav['missing_fields'])
    assert nav['simulation_parameters_applied'] is False


def test_fingerprints_are_deterministic_and_dict_order_independent():
    data = inputs()
    a = build(**data)
    reordered = json.loads(json.dumps(data, sort_keys=True))
    assert a['input_fingerprint'] == build(**reordered)['input_fingerprint']
    assert a == build(**data)
    assert len(a['input_fingerprint']) == 64


@pytest.mark.parametrize('change', ['threshold', 'confirmation', 'source_version', 'speed', 'phase', 'evidence_version', 'requirement'])
def test_relevant_content_changes_fingerprint(change):
    data = inputs()
    original = build(**data)['input_fingerprint']
    if change == 'threshold':
        data['continuous_service_policy']['service_acceptability_limits']['C']['service_outage'] = 4
    elif change == 'confirmation':
        data['safety_policy']['unacceptable_events'][0]['confirmed'] = True
    elif change == 'source_version':
        data['continuous_service_policy']['version'] = 'policy2'
    elif change == 'speed':
        data['aircraft_profile']['cruise_speed_mps'] = 16
    elif change == 'phase':
        data['operation_scenario']['operating_phase'] = 'landing'
    elif change == 'evidence_version':
        data['planning_evidence']['version'] = 'new'
    else:
        data['required_cns']['project_default']['communication']['max_gap_m'] = 1001
    assert original != build(**data)['input_fingerprint']


def test_simulation_identity_isolated_and_never_applied_to_fc30():
    data = inputs()
    real = build(**data)
    data['research_scenario'] = {'scenario_id': 'BACKUP-LIT-01', 'version': 'v1',
        'evidence_track': 'literature_grounded_simulation', 'source_id': 'paper-fixture',
        'parameters': {'horizontal_error_m': 2}}
    simulation = build(**data)
    assert real['input_fingerprint'] != simulation['input_fingerprint']
    assert real['navigation_evidence'] == simulation['navigation_evidence']
    assert simulation['research_scenario']['scenario_id'] == 'BACKUP-LIT-01'
    assert simulation['evidence_track'] == 'literature_grounded_simulation'


def test_input_objects_and_returned_objects_do_not_alias():
    data = inputs()
    before = deepcopy(data)
    result = build(**data)
    assert data == before
    result['frozen_inputs']['aircraft_profile']['navigation']['performance']['max_horizontal_error_m'] = 0
    assert data == before


def test_legacy_missing_and_null_do_not_become_pass_or_default_rtk():
    result = build(route_id='OLD')
    assert result['scenario_id'] is None and result['operating_phase'] is None
    assert result['acceptability_status'] == 'not_evaluated'
    assert rule(result, C, 'service_outage')['threshold']['value'] is None
    assert rule(result, C, 'service_outage')['applicability'] == 'unknown'
    assert service(result, RTK)['reason_code'] == 'required_service_not_adopted'
    assert result['missing_parameters']
    a = inputs()
    b = deepcopy(a)
    b['aircraft_profile']['version'] = None
    assert build(**a)['input_fingerprint'] != build(**b)['input_fingerprint']


def test_route_override_replaces_default_and_other_routes_are_not_copied():
    data = inputs()
    data['required_cns']['route_overrides'] = {'R0005': {'communication': {'required': False}},
        'OTHER': {'huge_unused': 'not-needed'}}
    a = build(**data)
    assert service(a, C)['status'] == 'not_applicable'
    data['required_cns']['route_overrides']['OTHER']['huge_unused'] = 'changed'
    assert a['input_fingerprint'] == build(**data)['input_fingerprint']


def test_existing_p17_limit_source_priority_and_builtin_disclosure():
    from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1, _limit
    data = inputs()
    data['continuous_service_policy']['parameter_overrides'] = {'c_full_outage_max_s': 6}
    result = build(**data)
    parameters = ContinuousServiceAcceptabilityV1._parameters({}, data['continuous_service_policy'])
    for key in (C, RID, RADAR):
        for mode in ('service_outage', 'redundancy_degradation'):
            r = rule(result, key, mode)
            assert r['threshold']['value'] == _limit(parameters, key[0], mode, data['continuous_service_policy'])
    r = rule(result, C, 'service_outage')
    assert r['source_id'] == 'continuous_service_policy.service_acceptability_limits.C.service_outage'
    assert r['version'] == 'policy1'
    empty = build(route_id='OLD')
    assert rule(empty, RID, 'service_outage')['evidence_level'] == 'internal_baseline'
    assert 'builtin_engineering_assumption' in rule(empty, RID, 'service_outage')['reason_codes']


def test_unmapped_extra_requirement_is_unknown_and_null_values_stay_missing():
    data = inputs()
    data['required_cns']['project_default']['communication']['custom_constraint'] = 7
    result = build(**data)
    r = next(x for x in result['requirement_consumption'] if x['field'] == 'communication.custom_constraint')
    assert r['status'] == 'unknown' and r['reason_code'] == 'consumer_unknown'


def test_nan_fingerprint_is_rejected_instead_of_nonstandard_json():
    data = inputs()
    data['aircraft_profile']['cruise_speed_mps'] = float('nan')
    with pytest.raises(ValueError):
        build(**data)


def test_navigation_rules_cover_both_services_without_injecting_thresholds():
    result = build(**inputs())
    assert {r['service_key'] for r in result['rules']} == {C, RTK, MONITOR, RID, RADAR}
    r = rule(result, RTK, 'reference_station_deficit')
    assert r['threshold']['value'] is None and r['threshold']['operator'] == '>='
    assert 'required_service_not_adopted' in r['reason_codes']
    assert r['ue_id'] is None and r['per_service_consumer'] is True


def test_source_and_applicability_are_separate_from_event_confirmation_and_consumption():
    data = inputs()
    r = rule(build(**data), C, 'service_outage')
    assert r['applicability'] == 'applicable'
    assert r['engineering_rule_status'] == 'sourced_and_applicable'
    assert r['consumer_exists'] is True and r['confirmed'] is False
    del data['continuous_service_policy']['source']
    r = rule(build(**data), C, 'service_outage')
    assert r['engineering_rule_status'] == 'unknown'
    assert 'rule_source_missing' in r['reason_codes']


def test_explicit_evidence_priority_and_version_are_read_without_timestamps():
    data = inputs()
    data['continuous_service_policy'] = {}
    data['planning_evidence']['items'] = [{
        'evidence_id': 'PEV-TEST', 'field': 'c_full_outage_max_s', 'scope': 'project',
        'source_type': 'engineering_assumption', 'source': 'fixture', 'value': 3,
        'statement': 'fixture', 'reason': 'fixture', 'report_disclosure': 'fixture',
        'confirmed_by_user': True, 'declared_by': 'fixture', 'version': 'ev-v1'}]
    a = build(**data)
    b = build(**data)
    assert a == b
    r = rule(a, C, 'service_outage')
    assert r['threshold']['value'] == 3 and r['source_id'] == 'PEV-TEST'
    assert r['version'] == 'ev-v1'


def test_missing_c_threshold_does_not_change_other_rule_applicability():
    data = inputs()
    data['continuous_service_policy'] = {}
    result = build(**data)
    assert rule(result, C, 'service_outage')['threshold']['value'] is None
    assert rule(result, RID, 'service_outage')['applicability'] == 'applicable'


def test_service_local_max_gap_is_also_unconsumed():
    data = inputs()
    data['required_cns']['project_default']['communication']['services'][C]['max_gap_m'] = 1000
    result = build(**data)
    r = next(x for x in result['requirement_consumption'] if x['field'] == '$requirement.max_gap_m')
    assert r['status'] == 'unconsumed' and r['reason_code'] == 'requirement_unconsumed'


def test_unadopted_service_rule_has_consumer_but_is_not_consumed():
    data = inputs()
    data['required_cns']['project_default']['surveillance']['services'][RADAR]['required'] = False
    r = rule(build(**data), RADAR, 'service_outage')
    assert r['consumer_exists'] is True
    assert r['consumption_status'] == 'not_applicable'


def test_simulation_missing_model_and_source_are_disclosed():
    data = inputs()
    data['research_scenario'] = {'scenario_id': 'SIM', 'evidence_track': 'literature_grounded_simulation'}
    result = build(**data)
    assert {'research_scenario.simulation_scenario_id', 'research_scenario.simulated_backup_model_id',
            'research_scenario.source_id', 'research_scenario.version'} <= set(result['missing_parameters'])
    assert result['research_evidence_status'] == 'unknown'


def test_unconfirmed_rtk_planning_is_not_consumed_even_when_service_is_confirmed():
    data = inputs()
    data['required_cns']['project_default']['navigation']['services'][RTK] = {
        'required': True, 'confirmed': True, 'source': 'fixture', 'version': 'rtk-v1',
        'planning': {'confirmed': False, 'max_reference_baseline_m': 5000,
                     'required_distinct_site_count': 1}}
    result = build(**data)
    r = rule(result, RTK, 'reference_station_deficit')
    assert r['threshold']['value'] == 1
    assert r['engineering_rule_status'] == 'unknown'
    assert r['consumption_status'] == 'unknown'
    for item in result['requirement_consumption']:
        if item['field'].startswith('planning.'):
            assert item['status'] == 'unknown'


def test_explicit_navigation_timing_evidence_is_retained_in_snapshot():
    data = inputs()
    data['aircraft_profile']['navigation_detection_time_s'] = 2
    data['aircraft_profile']['navigation_switch_time_s'] = 1
    result = build(**data)
    assert 'navigation_detection_time_s' not in result['navigation_evidence']['missing_fields']
    assert 'navigation_switch_time_s' not in result['navigation_evidence']['missing_fields']
    original = result['input_fingerprint']
    data['aircraft_profile']['navigation_switch_time_s'] = 3
    assert original != build(**data)['input_fingerprint']


@pytest.mark.parametrize('field', ['space_scope', 'cruise_altitude'])
def test_missing_scope_is_unknown_instead_of_expanding_rule_applicability(field):
    data = inputs()
    del data['operation_scenario'][field]
    result = build(**data)
    r = rule(result, C, 'service_outage')
    assert r['applicability'] == 'unknown'
    assert r['engineering_rule_status'] == 'unknown'
    assert 'rule_scope_missing' in r['reason_codes']
    assert 'operation_scenario.' + field in result['missing_parameters']
