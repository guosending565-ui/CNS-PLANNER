"""BUG-SHOT-010 回归：快照结构指纹必须**与状态规模无关**（否则首屏不可用）。

**问题（本轮实测）**：`_snapshot_structure_key()` 旧实现对**每个顶层键**先做一次完整
`json.dumps(sort_keys=True)` 再比较长度。生产状态里有 `grid`（8008 个格）、整条服务走廊
的逐体元结果与 373 条塔事实，于是每次快照都把上百 MB 状态序列化一遍：

| 端点 | 实测（修复前） |
|---|---|
| `GET /api/workflow` | 100.8 s |
| `GET /api/data-sources` | 346.6 s |
| `GET /api/state` | **960.3 s** |

**修复**：先用 :func:`_serialized_size_exceeds` 在不序列化的前提下判定"是否小对象"，
只有确认足够小才 `json.dumps`。大对象仍退化为 `(类型, 长度, 对象身份)` —— 指纹语义
与旧实现**逐项一致**，只是把 O(状态字节数) 变成 O(顶层键数)。

本文件同时锁定"提速不得牺牲缓存正确性"：小对象原地修改、大对象整体替换、顶层容器
独立性都必须继续被指纹捕捉或隔离。
"""

from copy import deepcopy
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.workflow_service import (  # noqa: E402
    WorkflowService, _SNAPSHOT_STRUCTURE_DETAIL_LIMIT, _serialized_size_exceeds,
    _snapshot_structure_key,
)

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"


def _big_grid(cells=8008):
    return {"status": "passed", "count": cells, "cells": [
        {"grid_id": f"MHT4063-L08-C{index:08d}-RP{index:08d}",
         "bbox": [122.1 + index * 1e-5, 29.8, 122.1 + index * 1e-5 + 1e-5, 29.9],
         "center": [122.1 + index * 1e-5, 29.85],
         "geometry": {"type": "Polygon", "coordinates": [[[122.1, 29.8], [122.2, 29.8]]]}}
        for index in range(cells)
    ]}


def _big_corridor(voxels=2745):
    return {"status": "failed", "routes": [{
        "route_id": "R0005", "voxels": [{
            "voxel_id": f"VOXEL-{index}@ALT-100", "surface_class": "sea",
            "subsystems": [{
                "subsystem": code, "combined_status": "unknown",
                "provider_evaluations": [{"device_id": "D1", "status": "unknown"}],
                "evidence": [{"kind": "p14_service_evidence", "index": index}],
            } for code in ("C", "N", "S")],
        } for index in range(voxels)],
    }]}


def test_size_estimation_is_a_safe_upper_bound():
    #: 估算只能偏大：判定"不超过 limit"必须真的不超过。
    assert _serialized_size_exceeds("x" * 10, 8192) is False
    assert _serialized_size_exceeds("x" * 9000, 8192) is True
    assert _serialized_size_exceeds({"a": 1}, 8192) is False
    #: 元素个数本身超限 ⇒ 立即判定超限（不展开容器）。
    assert _serialized_size_exceeds(list(range(9000)), 8192) is True
    assert _serialized_size_exceeds({"k": list(range(9000))}, 8192) is True
    assert _serialized_size_exceeds({"k": {"nested": [{"deep": "v" * 9000}]}}, 8192) is True


def test_structure_key_is_independent_of_state_size():
    state = {
        "revision": 12,
        "grid": _big_grid(),
        "cns_corridor_assessment": _big_corridor(),
        "cns_corridor_gap_assessment": _big_corridor(),
        "coverage_3d": _big_corridor(500),
        "towers": {"status": "passed", "count": 373, "items": [
            {"tower_id": f"T{index}", "longitude": 122.2, "latitude": 29.9} for index in range(373)
        ]},
        "required_cns": {"status": "passed", "source": "test"},
    }
    started = time.perf_counter()
    key = _snapshot_structure_key(state)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"结构指纹必须与状态规模无关，实测 {elapsed:.3f}s"
    keys = [item[0] for item in key]
    assert "grid" in keys and "cns_corridor_assessment" in keys
    #: 大对象退化为结构摘要（类型 + 长度 + 身份），绝不保留文本。
    grid_entry = next(item for item in key if item[0] == "grid")
    assert len(grid_entry) == 4 and grid_entry[1] == "dict"


def test_small_in_place_edits_are_still_detected():
    state = {"revision": 1, "coverage": {"status": "passed", "count": 3}}
    before = _snapshot_structure_key(state)
    state["coverage"]["count"] = 4          # 原地修改同一个对象
    after = _snapshot_structure_key(state)
    assert before != after, "小对象的原地修改必须被结构指纹捕捉"


def test_large_object_replacement_is_still_detected():
    state = {"revision": 1, "grid": _big_grid(200)}
    before = _snapshot_structure_key(state)
    state["grid"] = _big_grid(200)          # copy-on-write：新对象、同长度
    after = _snapshot_structure_key(state)
    assert before != after, "大对象整体替换必须改变指纹（对象身份变化）"


def test_snapshot_cache_still_serves_consistent_and_fresh_projections(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    first = workflow.snapshot()
    assert workflow.snapshot() == first, "同 revision 的快照必须一致"
    assert workflow.snapshot() is not first, "返回的顶层容器必须彼此独立"

    workflow.state["coverage"] = {"status": "passed", "count": 7}
    workflow.invalidate_snapshot_cache()
    refreshed = workflow.snapshot()
    assert refreshed["coverage"]["count"] == 7

    #: 不递增 revision 的原地修改也必须让缓存失效（结构指纹的作用）。
    workflow.state["coverage"]["count"] = 9
    assert workflow.snapshot()["coverage"]["count"] == 9


def test_deepcopy_isolation_of_snapshot_containers(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    snapshot = workflow.snapshot()
    snapshot["coverage"] = {"tampered": True}
    assert workflow.snapshot().get("coverage") != {"tampered": True}
    assert deepcopy(workflow.state.get("coverage")) != {"tampered": True}


# ---------------------------------------------------------------------------
# BUG-SHOT-011：快照本身不得改变状态对象的身份（否则缓存永不命中）
# ---------------------------------------------------------------------------

def test_snapshot_does_not_change_large_object_identities(tmp_path):
    """决定性不变量：**走结构指纹的大对象**身份不得被快照改写。

    （小对象走"序列化文本"指纹，内容不变即指纹不变，因此它们被逐帧规范化重建
    不影响缓存；真正会让缓存键漂移的是用对象身份识别的大对象。）
    """

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    structural = {
        item[0] for item in _snapshot_structure_key(workflow.state) if len(item) == 4
    }
    before = {key: id(workflow.state[key]) for key in structural if key in workflow.state}
    workflow.snapshot()
    after = {key: id(workflow.state[key]) for key in before}
    changed = sorted(key for key in before if before[key] != after.get(key))
    assert changed == [], f"快照改写了大对象身份（缓存键会漂移）：{changed}"


def test_repeated_snapshots_keep_the_cache_key_stable(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.snapshot()
    first = _snapshot_structure_key(workflow.state)
    workflow.snapshot()
    second = _snapshot_structure_key(workflow.state)
    assert first == second, "快照后结构指纹必须稳定，否则快照缓存永不命中"


def test_normalization_backfill_still_repairs_broken_state(tmp_path):
    """反向保护：真正需要修正的状态仍必须被规范化并写回（不得为了缓存而放弃 backfill）。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["layered_route_validations"] = {"items": "not-a-list"}
    workflow.layered_route_validation_service.ensure_state()
    repaired = workflow.state["layered_route_validations"]
    assert isinstance(repaired, dict) and isinstance(repaired.get("items"), list)

    workflow.state["route_risk_profiles"] = {"items": [{"profile_id": "X"}]}
    workflow.route_risk_profile_service.ensure_state()
    assert workflow.state["route_risk_profiles"]["items"][0]["profile_id"] == "X"

    workflow.state["grid_risk_v2"] = {"cells": "broken"}
    workflow.risk_v2_service.ensure_state()
    assert isinstance(workflow.state["grid_risk_v2"], dict)
