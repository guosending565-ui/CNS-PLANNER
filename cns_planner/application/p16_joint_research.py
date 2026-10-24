"""Bounded D25 research only; never publishes canonical state."""
from copy import deepcopy
from .communication_assumption_probe import communication_assumption_inputs
from .planning_evidence_service import device_catalog_with_evidence, aircraft_profile_with_evidence
from .engineering_assumption_p16_adapter import run_scenario_p16

def research_state(state, admission):
    if admission['validation']['status'] != 'passed':
        raise ValueError('admission blocked')
    pool = admission['pool']
    if pool['research_scope']['communication_assumptions'] != {'radius_m':2000,'interface':'ip','max_latency_s':1}:
        raise ValueError('D25 requires the frozen D24 assumptions')
    if len(pool['sites']) != 41 or len(pool['actions']) != 62:
        raise ValueError('D24 pool identity/count required')
    result = deepcopy(state)
    catalog, profile = communication_assumption_inputs(device_catalog_with_evidence(state), aircraft_profile_with_evidence(state), **pool['research_scope']['communication_assumptions'])
    result['device_catalog'] = catalog
    matches = [item for item in result['aircraft_profiles']['items'] if item.get('aircraft_id') == profile['aircraft_id']]
    if len(matches) != 1:
        raise ValueError('unique selected aircraft profile required')
    matches[0].update(profile)
    for device in catalog['items']:
        if device['device_id'] == 'COMM-BASELINE-2KM':
            device['coverage_geometry'].update(confirmed=True,status='confirmed',source='explicit_research_assumption',assumption_only=True)
    return result

def run_joint_research(state, admission, **kwargs):
    isolated = research_state(state, admission)
    outcome = run_scenario_p16(isolated, admission['pool'], **kwargs)
    if outcome.get('status') == 'computed':
        # The existence probe can be the adapter's last rerun. Reconstruct the
        # selected combination, never expose that last candidate as the final gap.
        from .corridor_site_planning_service import _apply_cumulative_action, rerun_corridor_chain
        from ..algorithms.corridor.v1 import CNSServiceCorridorV1
        from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
        facilities = deepcopy(isolated.get('existing_cns_facilities') or {})
        for action in outcome['result']['selected_actions']:
            facilities = _apply_cumulative_action(facilities, action)
        _, outcome['final_gap'] = rerun_corridor_chain(
            isolated, CNSServiceCorridorV1(), CNSCorridorGapAnalyzerV1(), facilities,
        )
        outcome['final_gap_origin'] = 'selected_combination_replay'
    outcome.update(assumption_only=True, real_world_confirmed=False, writes_project_state=False)
    return outcome
