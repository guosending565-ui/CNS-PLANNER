"""R7-B：P16-V2 假设站址三维投影的只读边界。

覆盖：只读 staged 结果、按物理站址合并、非正式语义、无任务时不回退 V1。
"""

from __future__ import annotations

from cns_planner.api.router import ApiRouter
from cns_planner.application.assumption_sites_3d import assumption_sites_payload
from cns_planner.tasks.task_specs import P16_V2_TASK_TYPE


class _Tasks:
    def __init__(self, records):
        self._records = records
        self.list_calls = []

    def list(self, status=None, limit=50):
        self.list_calls.append((status, limit))
        return list(self._records)


def _context(records):
    return type("Context", (), {"heavy_tasks": _Tasks(records)})()


def _task(actions, *, task_id="task-v2", finished="2026-10-22T18:14:31+00:00"):
    return {
        "task_id": task_id,
        "task_type": P16_V2_TASK_TYPE,
        "finished_at": finished,
        "status_text": "已完成",
        "result_summary": {
            "status": "computed",
            "inputs": {"scenario_id": "ZH-CNS-EA-R0005-BC-V1"},
            "selected_actions": actions,
            "formal_write": False,
        },
    }


def test_projection_is_read_only_and_groups_by_physical_site():
    actions = [
        {
            "action_id": "tower_colocation_host:EA-TWR-1:COMM-BASELINE-4KM",
            "site_id": "EA-TWR-1", "distinct_site_id": "site:EA-TWR-1",
            "coordinate": [122.1, 30.1], "subsystem": "C",
            "device_id": "COMM-BASELINE-4KM", "reuse_class": "tower_colocation_host",
        },
        {
            "action_id": "tower_colocation_host:EA-TWR-1:RID-BASELINE-2-5KM",
            "site_id": "EA-TWR-1", "distinct_site_id": "site:EA-TWR-1",
            "coordinate": [122.1, 30.1], "subsystem": "S",
            "device_id": "RID-BASELINE-2-5KM", "reuse_class": "tower_colocation_host",
        },
        {
            "action_id": "new_build_candidate:EA-R0005-B:RID-BASELINE-2-5KM",
            "site_id": "EA-R0005-B", "distinct_site_id": "site:EA-R0005-B",
            "coordinate": [122.2, 30.2], "subsystem": "S",
            "device_id": "RID-BASELINE-2-5KM", "reuse_class": "new_build_candidate",
        },
    ]
    payload = assumption_sites_payload(_context([_task(actions)]))
    assert payload["status"] == "available"
    assert payload["count"] == 2
    assert payload["action_count"] == 3
    assert payload["assumption_only"] is True
    assert payload["formal_equipment"] is False
    assert payload["canonical_write"] is False
    assert payload["source_task_id"] == "task-v2"
    first = next(item for item in payload["sites"]
                 if item["distinct_site_id"] == "site:EA-TWR-1")
    assert first["subsystems"] == ["C", "S"]
    assert len(first["action_ids"]) == 2
    assert payload["mount_height_agl_m"] == 15.0


def test_no_succeeded_v2_task_is_unavailable_not_v1_fallback():
    payload = assumption_sites_payload(_context([]))
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "no_succeeded_v2_task"
    assert payload["sites"] == []
    assert payload["count"] == 0
    assert payload["formal_equipment"] is False


def test_latest_v2_task_wins():
    older = _task([{
        "action_id": "a", "site_id": "old", "distinct_site_id": "site:old",
        "coordinate": [122.0, 30.0], "subsystem": "C",
    }], task_id="task-old", finished="2026-10-22T17:00:00+00:00")
    newer = _task([{
        "action_id": "b", "site_id": "new", "distinct_site_id": "site:new",
        "coordinate": [122.3, 30.3], "subsystem": "S",
    }], task_id="task-new", finished="2026-10-22T18:00:00+00:00")
    payload = assumption_sites_payload(_context([older, newer]))
    assert payload["source_task_id"] == "task-new"
    assert payload["sites"][0]["distinct_site_id"] == "site:new"


def test_router_registers_assumption_sites_as_read_only_3d_endpoint():
    assert ApiRouter.VISUALIZATION_3D_ACTIONS[
        "/api/visualization-3d/assumption-sites"
    ] == "assumption-sites"
