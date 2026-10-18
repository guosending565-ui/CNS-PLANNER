from copy import deepcopy
from pathlib import Path

from cns_planner.application.workflow_service import WorkflowService

DEFAULTS = Path(__file__).parents[1] / 'cns_planner/config/defaults.json'


def test_snapshot_computes_lane_identity_once_and_rechecks_after_invalidation(tmp_path):
    workflow = WorkflowService(tmp_path / 'project.json', DEFAULTS)
    layered = workflow.layered_route_planner_service
    calls = []
    original = layered.planner.fingerprints

    def counted(**kwargs):
        calls.append(1)
        return original(**kwargs)

    layered.planner.fingerprints = counted
    before = deepcopy({k: v for k, v in workflow.state.items() if not k.startswith("_")})
    first = workflow.snapshot()
    assert len(calls) == 1
    assert {k: v for k, v in workflow.state.items() if not k.startswith("_")} == before
    workflow.invalidate_snapshot_cache()
    calls.clear()
    second = workflow.snapshot()
    assert len(calls) == 1
    assert second == first
    assert {k: v for k, v in workflow.state.items() if not k.startswith("_")} == before


def test_identity_outside_snapshot_is_fresh_and_returns_independent_details(tmp_path):
    workflow = WorkflowService(tmp_path / 'project.json', DEFAULTS)
    layered = workflow.layered_route_planner_service
    before = deepcopy(workflow.state)
    first = layered.current_identity()
    first['fingerprint_components']['test_only'] = True
    assert 'test_only' not in layered.current_identity()['fingerprint_components']
    workflow.state['layered_route_planning_request']['scenario_route_id'] = 'changed'
    assert layered.current_identity()['lane_key'] != first['lane_key']
    assert workflow.state['revision'] == before['revision']