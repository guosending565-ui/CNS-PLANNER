"""BUG-SHOT-006 回归：通用 workflow 快照的缓存语义必须只加速、不改语义。

修复前：``GET /api/workflow`` 单次实测 74-83 s，``GET /api/state`` 517.6 s，
根因是 ``WorkflowService.snapshot()`` 每次请求都重建整棵只读投影。

修复：按**权威 revision** 缓存投影结果（revision 只在 ProjectState 成功提交时自增）。
本测试锁定三条语义：

1. 同一 revision 下重复取快照，内容与被缓存前逐字相同；
2. revision 变化后，快照必须反映新状态（缓存必须失效）；
3. 快照返回的是独立顶层容器（调用方改动不会污染下一次返回值）。
"""

import json
from copy import deepcopy
from pathlib import Path

from cns_planner.application.workflow_service import WorkflowService

DEFAULTS = Path(__file__).resolve().parents[1] / "cns_planner" / "config" / "defaults.json"


def _service(tmp_path):
    return WorkflowService(tmp_path / "project.json", DEFAULTS)


def test_snapshot_repeats_are_identical_within_one_revision(tmp_path):
    service = _service(tmp_path)
    first = service.snapshot()
    second = service.snapshot()
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_snapshot_cache_is_reused_only_for_same_revision(tmp_path):
    service = _service(tmp_path)
    first = service.snapshot()
    # 同一 revision + 同一结构指纹：必须命中缓存（同一个投影对象被复用）。
    assert service._snapshot_cache[0][0] == service.state.get("revision")
    assert service._snapshot_cache[1] is not None
    cached_object = service._snapshot_cache[1]
    service.snapshot()
    assert service._snapshot_cache[1] is cached_object, "同 revision 重复取快照必须命中缓存"

    # revision 自增（提交一次）后必须重建，而不是继续返回旧投影。
    service.state["revision"] = int(service.state.get("revision") or 0) + 1
    third = service.snapshot()
    assert service._snapshot_cache[0][0] == service.state["revision"]
    assert service._snapshot_cache[1] is not cached_object, "revision 变化后必须重建快照"
    assert third["revision"] == service.state["revision"]
    assert third["revision"] != first["revision"]


def test_snapshot_cache_invalidates_on_direct_state_edits_without_revision_bump(tmp_path):
    """BUG-SHOT-006 复核：**不递增 revision** 的直接 state 修改也必须反映到快照。

    生产代码里有 `workflow.state["coverage"] = ...` 这类命令式赋值，以及
    `register_source_paths()` 这类不落盘的写路径；只看 revision 的缓存会让它们
    拿到过期投影（`tests/test_production_write_authority.py`
    与 `tests/test_towers_real_data.py` 各有用例因此打回）。
    """

    service = _service(tmp_path)
    service.snapshot()
    cached_object = service._snapshot_cache[1]
    revision = service.state.get("revision")
    legacy = {"status": "passed", "layers": {"C": {"status": "passed", "stations": []}}}
    service.state["coverage"] = deepcopy(legacy)
    assert service.state.get("revision") == revision, "本用例刻意不递增 revision"
    after = service.snapshot()
    assert after["coverage"] == legacy, "直接改 state 后快照必须反映新内容"
    assert service._snapshot_cache[1] is not cached_object, "内容变化后必须重建"

    # 显式失效同样必须生效。
    service.invalidate_snapshot_cache()
    assert service._snapshot_cache is None


def test_snapshot_returns_independent_top_level_container(tmp_path):
    service = _service(tmp_path)
    first = service.snapshot()
    first["__probe__"] = True
    first.pop("steps", None)
    second = service.snapshot()
    assert "__probe__" not in second, "调用方不得污染缓存中的快照"
    assert "steps" in second


def test_snapshot_result_reflects_new_state_after_revision_bump(tmp_path):
    """revision 变化后，快照必须反映新的状态内容（不是只换 revision 字段）。"""

    service = _service(tmp_path)
    service.snapshot()
    marker = {"status": "passed", "count": 7, "items": [], "note": "bug-shot-006-probe"}
    service.state["cns_corridor_gap_assessment"] = marker
    service.state["revision"] = int(service.state.get("revision") or 0) + 1
    snapshot = service.snapshot()
    projected = snapshot.get("cns_corridor_gap_assessment") or {}
    assert projected.get("note") == "bug-shot-006-probe"
