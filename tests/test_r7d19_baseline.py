import json
from pathlib import Path
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.domain.cns_service_contract import SERVICE_SURFACE_POLICY, COMMUNICATION_RADIUS_BY_SURFACE, RID_RADIUS_BY_SURFACE

ROOT = Path(__file__).resolve().parents[1]

def test_new_device_has_explicit_geometry_and_unknown_reliability():
    catalog = json.loads((ROOT / 'cns_planner/config/device_catalog.json').read_text(encoding='utf-8'))
    device = normalize_device(next(x for x in catalog['items'] if x['device_id'] == 'COMM-BASELINE-2KM'))
    assert device['coverage_geometry']['radius_by_surface'] == COMMUNICATION_RADIUS_BY_SURFACE
    assert device['mtbf_h'] is None
    assert device['reliability']['availability'] is None
    assert device['reliability']['status'] == 'missing_data'

def test_policy_and_scenario_identity_is_distinct_from_history():
    base = ROOT / 'cns_planner/config/engineering_assumptions'
    old = json.loads((base / 'zhoushan_v1.json').read_text(encoding='utf-8'))
    new = json.loads((base / 'zhoushan_v2.json').read_text(encoding='utf-8'))
    assert old['assumptions']['equipment']['C'] == 'COMM-BASELINE-4KM'
    assert new['assumptions']['equipment']['C'] == 'COMM-BASELINE-2KM'
    assert old['scenario_id'] != new['scenario_id'] and old['version'] != new['version']
    assert SERVICE_SURFACE_POLICY['C:communication']['policy_version'] == 'r7d19-comm-2km-v1'
    assert set(COMMUNICATION_RADIUS_BY_SURFACE.values()) == {2000}
    assert RID_RADIUS_BY_SURFACE == dict(land=2000, coastal_uncertain=2000, sea=5000)
