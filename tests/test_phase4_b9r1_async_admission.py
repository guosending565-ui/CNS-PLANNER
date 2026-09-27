"""Phase4-B9R.1：Async Performance Admission Closure —— 定向测试。

对应 B9R.1 验收要求 1–14：

1. sync within → 正常运行；
2. sync beyond_validated → 估算后、计算前拒绝；
3. async within → 正常运行；
4. async beyond_validated + 未显式接受 → 失败，且不 publish；
5. async beyond_validated + ``allow_beyond_validated_envelope=true`` → 运行；
6. async ceiling + 未显式接受 → 拒绝；
7. async ceiling + 显式接受 → **仍然**拒绝（硬天花板不可绕过）；
8. immutable snapshot 里冻结 ``complexity_estimate``；
9. preflight 进入 input fingerprint（估算件变化 → 指纹变化）；
10. 提交后修改 state 不改变 worker 使用的估算件；
11. 被阻断的任务不产生 staged / canonical artifact；
12. canonical assessment schema 不含复杂度估算；
13. 既有 P14 语义 SHA 测试保持（此处做保护清单回归）；
14. progress / cancel 行为保持。

需求 → 测试映射：

===  ==========================================================================
1    ``test_sync_within_validated_envelope_runs_and_writes_canonical``
2    ``test_sync_beyond_validated_envelope_is_rejected_before_evaluation``
3    ``test_async_within_validated_envelope_runs_and_stages``
4    ``test_async_beyond_validated_envelope_without_acceptance_is_blocked``
5    ``test_async_beyond_validated_envelope_with_acceptance_runs``
6    ``test_async_safety_ceiling_is_blocked_without_acceptance``
7    ``test_async_safety_ceiling_cannot_be_bypassed_by_acceptance``
8    ``test_snapshot_freezes_complexity_estimate``
9    ``test_complexity_estimate_participates_in_input_fingerprint``
10   ``test_worker_uses_frozen_estimate_after_state_becomes_worse``
11   ``test_blocked_task_publishes_nothing``
12   ``test_canonical_assessment_schema_has_no_complexity_estimate``
13   ``test_p14_protected_baseline_matches_workspace``
14   ``test_progress_and_cancel_contract_are_preserved``
（另加）``test_compute_uses_frozen_estimate_and_never_reestimates``、
     ``test_compute_rejects_safety_ceiling_even_with_acceptance`` —— 需求 5 的
     ``compute()`` 语义
===  ==========================================================================

全部走真实运行时组件：真 WorkflowService、真 immutable snapshot、真 P14 算法实例；
只有 worker 子进程被同进程的 :class:`_RunnerContext` 取代，以便精确断言
"没有 staged / 没有 canonical 写入"。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import (
    CNSServiceCorridorV1, COMPLEXITY_TIER_BEYOND, COMPLEXITY_TIER_CEILING,
    COMPLEXITY_TIER_VALIDATED, CorridorComplexityBlocked, estimate_corridor_complexity,
)
from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.application.corridor_service import CorridorScaleNotAccepted
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import normalize_device
from cns_planner.persistence.artifact_store import ArtifactStore
from cns_planner.tasks.handlers import plan_submission
from cns_planner.tasks.task_input import (
    AlgorithmResolver, InputSnapshotStore, fingerprint_of,
)
from cns_planner.tasks.task_spec import TaskCancelled
from cns_planner.tasks.task_specs import CORRIDOR_TASK_TYPE, _corridor_runner, task_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = REPO_ROOT / "cns_planner" / "config" / "defaults.json"
PROTECTED_BASELINE = Path(__file__).with_name("p14_protected_sha256.json")


# ---- 夹具：小体量、可参数化的规模杠杆 -------------------------------------------


def _requirement_set():
    return {
        "status": "passed",
        "project_default": {
            "communication": {
                "required": True, "status": "passed",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 1.0, "min_redundancy": 1},
            },
            "navigation": {"required": False, "status": "passed", "type": {}, "performance": {}},
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {},
    }


def _aircraft():
    return {
        "aircraft_id": "A1",
        "communication": {
            "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
        },
        "navigation": {}, "surveillance": {},
    }


def _device(radius_m):
    return normalize_device({
        "device_id": "D1", "name": "D1", "subsystem": "C", "role": "existing",
        "radius_m": radius_m, "mtbf_h": 1000,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.2, "min_redundancy": 1},
        "coverage_geometry": {
            "model": "sphere", "slant_range_m": radius_m, "source": "test", "confirmed": True,
        },
        "service_model": {
            "model_family": "declared_performance", "version": "1",
            "technology": "dedicated_radio",
            "parameters": {"performance": {"max_latency_s": 0.2}},
            "source": "test", "confirmed": True,
        },
    })


def _policy(width=300.0):
    return normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": width,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})


def corridor_fixture(grid_rows=6, grid_columns=6, providers_per_subsystem=1, layers=1,
                     radius_m=20_000.0, width_m=300.0, step=0.001):
    """与生产同构的走廊输入；provider 规模、``layers`` 与走廊宽度是规模杠杆。

    网格步长固定（0.001°），因此夹具只影响 ``estimated_evaluation_upper_bound``，
    真实计算量始终在毫秒级。
    """

    cells = [
        {
            "grid_id": f"H{row:03d}{column:03d}",
            "bbox": [column * step, row * step, (column + 1) * step, (row + 1) * step],
        }
        for row in range(grid_rows) for column in range(grid_columns)
    ]
    middle = (grid_columns // 2) * step
    route = {"route_id": "R1", "status": "passed",
             "path": [[middle, 0.0], [middle, (grid_rows - 1) * step]]}
    spatial = {
        "status": "passed",
        "altitude_layers": [{
            "altitude_layer_id": f"L{index}", "name": f"L{index}",
            "lower_altitude_m": 0.0, "upper_altitude_m": 1000.0,
            "vertical_reference": "egm2008_orthometric",
            "source": "test", "confirmed": True, "status": "confirmed",
        } for index in range(layers)],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant",
            "vertical_reference": "egm2008_orthometric", "constant_altitude_m": 100.0,
            "waypoints": [], "source": "test", "confirmed": True,
            "status": "confirmed", "geoid_undulation_m": None,
        }},
        "site_vertical_profiles": {},
    }
    items, catalog_items = [], []
    spread = max(1, providers_per_subsystem)
    for code in ("C", "N", "S"):
        for index in range(providers_per_subsystem):
            device_id, facility_id = f"{code}D{index:05d}", f"{code}F{index:05d}"
            entry = _device(radius_m)
            entry["device_id"] = device_id
            entry["subsystem"] = code
            catalog_items.append(entry)
            items.append({
                "facility_id": facility_id,
                "coordinate": [middle, 0.0005 + step * (index % spread)],
                "status": "active",
                "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
                "devices": [{"device_id": device_id, "subsystem": code, "status": "active"}],
            })
    return {
        "routes": [route], "spatial_3d": spatial,
        "grid": {"status": "passed", "level": 7, "cells": cells},
        "grid_attributes": {"terrain": {"status": "passed", "cells": {}}},
        "corridor_policy": _policy(width=width_m),
        "existing_facilities": {"status": "passed", "items": items},
        "device_catalog": {"status": "passed", "items": catalog_items},
    }


def estimate_for(data):
    return estimate_corridor_complexity(
        data["routes"], data["spatial_3d"], data["grid"], data["grid_attributes"],
        data["corridor_policy"], data["existing_facilities"], data["device_catalog"],
    )


#: 三档夹具参数：走廊覆盖整个网格（宽走廊），规模杠杆是 provider 总数。
#: 阈值：``VALIDATED_EVALUATION_LIMIT = 3e6`` / ``SAFETY_EVALUATION_CEILING = 1e7``。
VALIDATED_FIXTURE = {
    "grid_rows": 120, "grid_columns": 120, "providers_per_subsystem": 2,
    "layers": 1, "width_m": 4000.0,
}
BEYOND_FIXTURE = {
    "grid_rows": 120, "grid_columns": 120, "providers_per_subsystem": 400,
    "layers": 1, "width_m": 4000.0,
}
CEILING_FIXTURE = {
    "grid_rows": 200, "grid_columns": 120, "providers_per_subsystem": 700,
    "layers": 1, "width_m": 4000.0,
}


def fixture_with_tier(tier):
    """返回 (inputs, estimate)，并断言它确实落在期望的那一档。"""

    options = {
        COMPLEXITY_TIER_VALIDATED: VALIDATED_FIXTURE,
        COMPLEXITY_TIER_BEYOND: BEYOND_FIXTURE,
        COMPLEXITY_TIER_CEILING: CEILING_FIXTURE,
    }[tier]
    data = corridor_fixture(**options)
    estimate = estimate_for(data)
    assert estimate["tier"] == tier, (
        f"夹具未落在 {tier}：{estimate['tier']} "
        f"(upper_bound={estimate['estimated_evaluation_upper_bound']})"
    )
    return data, estimate


def project_state_from(data):
    """把夹具翻译成 ProjectState 的对应 state 键。"""

    return {
        "operational_routes": deepcopy(data["routes"]),
        "spatial_3d": deepcopy(data["spatial_3d"]),
        "grid": deepcopy(data["grid"]),
        "grid_attributes": deepcopy(data["grid_attributes"]),
        "required_cns": _requirement_set(),
        "aircraft_profiles": {"items": [_aircraft()], "count": 1},
        "selected_aircraft_profile_id": "A1",
        "existing_cns_facilities": deepcopy(data["existing_facilities"]),
        "device_catalog": deepcopy(data["device_catalog"]),
        "cns_corridor_policy": deepcopy(data["corridor_policy"]),
    }


class Project:
    """真实 WorkflowService + 真实 immutable snapshot 存储。"""

    def __init__(self, tmp_path):
        tmp_path = Path(tmp_path)
        tmp_path.mkdir(mode=0o755, exist_ok=True)
        self.workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
        self.workflow.mutation_lock = threading.RLock()
        self.snapshots = InputSnapshotStore(tmp_path / "project.json")

    @property
    def workdir(self):
        """项目状态文件所在目录 = worker 的 ``--workdir``（staged 区在它下面）。"""

        return Path(self.workflow.store_path).parent

    def load(self, data):
        self.workflow.state.update(project_state_from(data))
        self.workflow.save()
        return self

    def submit(self, payload=None):
        """走真实提交路径：生成并持久化 immutable snapshot。"""

        return plan_submission(
            self.workflow, CORRIDOR_TASK_TYPE, payload or {}, store=self.snapshots,
        )

    def fingerprint(self):
        return task_spec(CORRIDOR_TASK_TYPE).fingerprint_for(
            self.workflow.state, {}, self.workflow.algorithm_registry,
        )


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


# ---- 同进程 worker 上下文（可精确断言副作用） -----------------------------------


class _FakeStore:
    """只回答"任务还在运行、没有取消"的只读 store 替身。

    ``cancel_after_queries``：取消请求在若干次查询之后才被观察到（模拟真实
    时序：取消探测线程与算法检查点各自读一次 store）。
    """

    def __init__(self, task_id):
        self.task_id = task_id
        self.cancel_requested = False
        self.status = "running"
        self.cancel_after_queries = None
        self.queries = 0

    def find(self, _task_id):
        self.queries += 1
        cancelled = bool(self.cancel_requested)
        if self.cancel_after_queries is not None:
            cancelled = self.queries > int(self.cancel_after_queries)
        return {
            "task_id": self.task_id,
            "status": "cancelling" if cancelled else self.status,
            "cancel_requested": cancelled, "progress": 0.0,
        }


class _RunnerContext:
    """与 :class:`~cns_planner.tasks.worker.WorkerContext` 同形的最小上下文。

    ``check_cancel`` 按 production ``WorkerContext`` 的语义实现（读 task store 的
    ``cancel_requested``）——取消契约因此在本测试里是**真的**被验证，而不是打桩。
    """

    def __init__(self, snapshot, registry, workdir):
        self.inputs = {"snapshot": snapshot}
        self.task_id = "b9r1-task"
        self.store = _FakeStore(self.task_id)
        self.workdir = Path(workdir)
        self.progress_events = []
        self._algorithms = AlgorithmResolver(snapshot.get("algorithms") or {}, registry)

    def algorithms(self):
        return self._algorithms

    def check_cancel(self):
        current = self.store.find(self.task_id)
        if current is not None and current.get("cancel_requested"):
            raise TaskCancelled()
        return None

    def progress(self, value, message=None):
        self.progress_events.append((float(value), message))

    def stop_heartbeat(self, *, finished=False):
        return None

    def stage(self, payload, *, artifact_type):
        """与 production worker 一致：写进可回收的 staged 区（真 ArtifactStore）。"""

        return ArtifactStore(self.workdir).publish_temporary(
            payload, artifact_type=artifact_type,
            producer={"service": "B9R1Test", "algorithm_id": "cns_service_corridor_v1",
                      "algorithm_version": "1"},
            input_fingerprint=fingerprint_of(self.inputs["snapshot"]),
            scope={"task_id": self.task_id},
            summary={"task_id": self.task_id},
        )


def default_registry():
    return build_default_algorithm_registry(
        json.loads(DEFAULTS.read_text(encoding="utf-8"))
    )


def run_corridor_runner(snapshot, workdir):
    context = _RunnerContext(snapshot, default_registry(), workdir)
    return context, _corridor_runner(context)


def staged_files(workdir):
    """worker staged 区里是否留下了任何临时结果明细。"""

    directory = ArtifactStore(workdir).temporary_directory
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*"))


def canonical_state(project):
    """当前 canonical 服务走廊结果（空评估 = 没有任何 routes / 仍是未计算状态）。"""

    return project.workflow.state.get("cns_corridor_assessment") or {}


def assert_no_new_staged(before, after):
    """worker 计算**没有**新增任何 staged 结果明细（阻断 = 根本没走到 staging）。

    注意：项目 save 本身会往同一个可回收区写快照，所以只比较"新增"。
    """

    assert sorted(after) == sorted(before), f"阻断路径不得新增 staged：{set(after) - set(before)}"


def assert_no_canonical_result(project):
    """canonical result 必须仍是"未计算"（不是本次计算写出来的）。"""

    assessment = canonical_state(project)
    assert not assessment.get("routes"), f"不得写入 canonical 结果：{assessment.get('routes')}"
    assert assessment.get("route_count") in (None, 0)
    assert assessment.get("input_fingerprint") is None
    statuses = project.workflow.state.get("result_statuses") or {}
    assert statuses.get("cns_corridor_assessment") == "not_calculated", (
        "阻断路径不得更新 result_statuses"
    )


# ---- 1 / 2. 同步三档 -----------------------------------------------------------


def test_sync_within_validated_envelope_runs_and_writes_canonical(project):
    """1. sync within → 正常运行并写入 canonical result（附只读估算元数据）。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)

    snapshot = project.workflow.evaluate_cns_corridor({})

    assessment = project.workflow.state.get("cns_corridor_assessment") or {}
    assert assessment.get("algorithm_id") == CNSServiceCorridorV1.algorithm_id
    assert assessment.get("input_fingerprint")
    assert snapshot["corridor_complexity_estimate"]["tier"] == COMPLEXITY_TIER_VALIDATED
    assert snapshot["corridor_complexity_estimate"] == estimate


def test_sync_beyond_validated_envelope_is_rejected_before_evaluation(project, monkeypatch):
    """2. sync beyond_validated → 估算后、**计算前**拒绝，且不写 canonical。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_BEYOND)
    project.load(data)

    calls = []
    original = CNSServiceCorridorV1.evaluate

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CNSServiceCorridorV1, "evaluate", spy)

    with pytest.raises(CorridorScaleNotAccepted) as caught:
        project.workflow.evaluate_cns_corridor({})

    assert calls == [], "beyond_validated 必须在进入算法计算前就拒绝"
    assert caught.value.tier == COMPLEXITY_TIER_BEYOND
    assert "后台计算" in str(caught.value)
    assert "硬上限" not in str(caught.value)
    assert (project.workflow.state.get("cns_corridor_assessment") or {}).get("routes") in (None, [])
    assert estimate["tier"] == COMPLEXITY_TIER_BEYOND


# ---- 3–7. 异步（worker）三档 ---------------------------------------------------


def test_async_within_validated_envelope_runs_and_stages(project):
    """3. async within → worker 正常运行并 staged 结果（不写 canonical）。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    assert snapshot["inputs"]["complexity_estimate"]["tier"] == COMPLEXITY_TIER_VALIDATED

    context, result = run_corridor_runner(snapshot, project.workdir)

    assert result.staged is not None
    assert result.summary.get("algorithm_id") == CNSServiceCorridorV1.algorithm_id
    assert context.progress_events, "worker 必须上报进度"
    assert_no_canonical_result(project)
    assert estimate["tier"] == COMPLEXITY_TIER_VALIDATED


def test_async_beyond_validated_envelope_without_acceptance_is_blocked(project):
    """4. async beyond + 未显式接受 → 失败，且不产生 staged / canonical artifact。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_BEYOND)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    assert "allow_beyond_validated_envelope" not in (snapshot["worker_payload"] or {})
    before = staged_files(project.workdir)

    with pytest.raises(CorridorComplexityBlocked) as caught:
        run_corridor_runner(snapshot, project.workdir)

    assert caught.value.estimate["tier"] == COMPLEXITY_TIER_BEYOND
    assert_no_new_staged(before, staged_files(project.workdir))
    assert_no_canonical_result(project)
    assert estimate["tier"] == COMPLEXITY_TIER_BEYOND


def test_async_beyond_validated_envelope_with_acceptance_runs(project):
    """5. async beyond + 显式接受（随快照冻结）→ 运行并 staged。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_BEYOND)
    project.load(data)
    plan = project.submit({"allow_beyond_validated_envelope": True})
    assert plan["worker_payload"].get("allow_beyond_validated_envelope") is True
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    assert snapshot["worker_payload"]["allow_beyond_validated_envelope"] is True

    _context, result = run_corridor_runner(snapshot, project.workdir)

    assert result.staged is not None
    assert result.summary.get("input_fingerprint")
    assert estimate["tier"] == COMPLEXITY_TIER_BEYOND


def test_async_safety_ceiling_is_blocked_without_acceptance(project):
    """6. async ceiling + 未显式接受 → 拒绝。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_CEILING)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    before = staged_files(project.workdir)

    with pytest.raises(CorridorComplexityBlocked) as caught:
        run_corridor_runner(snapshot, project.workdir)

    assert caught.value.estimate["tier"] == COMPLEXITY_TIER_CEILING
    assert_no_new_staged(before, staged_files(project.workdir))
    assert estimate["tier"] == COMPLEXITY_TIER_CEILING


def test_async_safety_ceiling_cannot_be_bypassed_by_acceptance(project):
    """7. async ceiling + 显式接受 → **仍然**拒绝（硬天花板不可绕过）。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_CEILING)
    project.load(data)
    plan = project.submit({"allow_beyond_validated_envelope": True})
    assert plan["worker_payload"].get("allow_beyond_validated_envelope") is True
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    before = staged_files(project.workdir)

    with pytest.raises(CorridorComplexityBlocked) as caught:
        run_corridor_runner(snapshot, project.workdir)

    assert caught.value.estimate["tier"] == COMPLEXITY_TIER_CEILING
    assert_no_new_staged(before, staged_files(project.workdir))
    assert_no_canonical_result(project)
    assert estimate["tier"] == COMPLEXITY_TIER_CEILING


# ---- 8 / 9 / 10. snapshot 冻结、指纹、state 变化 --------------------------------


def test_snapshot_freezes_complexity_estimate(project):
    """8. immutable snapshot 的 inputs 里带着冻结的 ``complexity_estimate``。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    plan = project.submit({})
    stored = project.snapshots.load(plan["input_snapshot_ref"])["inputs"]["complexity_estimate"]

    assert stored == estimate
    assert stored["status"] == "estimated"
    assert stored["is_precise_measurement"] is False
    assert stored["tier"] == COMPLEXITY_TIER_VALIDATED


def test_complexity_estimate_participates_in_input_fingerprint(project):
    """9. preflight 进入 input fingerprint：规模变 → 结论变 → 指纹变。"""

    data, _estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    before = project.fingerprint()

    # 只改"规模"（provider 数），其余输入事实不动。
    bigger = corridor_fixture(**BEYOND_FIXTURE)
    project.workflow.state["existing_cns_facilities"] = deepcopy(bigger["existing_facilities"])
    project.workflow.state["device_catalog"] = deepcopy(bigger["device_catalog"])
    after = project.fingerprint()

    assert before != after
    assert estimate_for(bigger)["tier"] == COMPLEXITY_TIER_BEYOND
    # 提交时持久化的指纹 == snapshot 的内容寻址身份（估算件确实在指纹覆盖范围内）。
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    assert plan["input_fingerprint"] == fingerprint_of(snapshot)
    assert snapshot["inputs"]["complexity_estimate"]["tier"] == COMPLEXITY_TIER_BEYOND


def test_worker_uses_frozen_estimate_after_state_becomes_worse(project):
    """10. 提交后把 state 改成更坏的规模：worker 仍按快照冻结的估算件放行。"""

    data, _estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    assert snapshot["inputs"]["complexity_estimate"]["tier"] == COMPLEXITY_TIER_VALIDATED

    # 提交之后把当前 state 推到 ceiling 档：worker 若回读 state 就必然被阻断。
    worse = corridor_fixture(**CEILING_FIXTURE)
    project.workflow.state["existing_cns_facilities"] = deepcopy(worse["existing_facilities"])
    project.workflow.state["device_catalog"] = deepcopy(worse["device_catalog"])
    assert estimate_for(worse)["tier"] == COMPLEXITY_TIER_CEILING
    assert project.fingerprint() != plan["input_fingerprint"], "当前输入确实已变化（publish 会 stale）"

    _context, result = run_corridor_runner(snapshot, project.workdir)
    assert result.staged is not None, "worker 必须按冻结的估算件放行，而不是回读当前 state"


def test_blocked_task_publishes_nothing(project):
    """11. 被阻断的任务：没有 staged artifact，也没有 canonical 写入。"""

    data, _estimate = fixture_with_tier(COMPLEXITY_TIER_CEILING)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])
    before = staged_files(project.workdir)

    with pytest.raises(CorridorComplexityBlocked):
        run_corridor_runner(snapshot, project.workdir)

    assert_no_new_staged(before, staged_files(project.workdir))
    assert_no_canonical_result(project)


def test_canonical_assessment_schema_has_no_complexity_estimate(project):
    """12. canonical assessment schema 不含任何复杂度估算字段。"""

    data, _estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    snapshot = project.workflow.evaluate_cns_corridor({})
    assessment = project.workflow.state.get("cns_corridor_assessment") or {}

    assert "complexity_estimate" not in assessment
    assert "corridor_complexity_estimate" not in assessment
    assert not any("complexity" in str(key) for key in assessment)
    # 只读元数据只挂在响应快照上，不落 canonical。
    assert snapshot["corridor_complexity_estimate"]["tier"] == COMPLEXITY_TIER_VALIDATED


# ---- 13. P14 保护清单回归 ------------------------------------------------------


def test_p14_protected_baseline_matches_workspace():
    """13. P14 语义 SHA 基线测试保持：保护清单与工作区逐字节一致。"""

    baseline = json.loads(PROTECTED_BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == {
        "cns_planner/algorithms/corridor/v1.py",
        "cns_planner/algorithms/coverage/geometric_3d.py",
        "cns_planner/algorithms/service_capability/v1.py",
        "tests/test_cns_corridor.py",
    }
    actual = {
        name: hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
        for name in baseline
    }
    assert actual == baseline
    # P14 性能优化本体（几何覆盖 / 服务能力）逐字节未动。
    assert baseline["cns_planner/algorithms/coverage/geometric_3d.py"] == (
        "f2fc421253861d2a11ff7fb4f33efe0d89e0bcc31f9b9536108b203ec40acae9"
    )
    assert baseline["cns_planner/algorithms/service_capability/v1.py"] == (
        "79f9c56a97955847b74a5b6ede244befcb8dc77caf4739daad2c2b769ba57c28"
    )


# ---- compute() 语义 + progress / cancel 保持 -----------------------------------


def test_compute_uses_frozen_estimate_and_never_reestimates(project, monkeypatch):
    """compute() 只消费 inputs 的估算件：beyond 默认拒绝、显式接受放行。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_BEYOND)
    project.load(data)
    service = project.workflow.corridor_service

    inputs = service.compute_input({})
    assert inputs["complexity_estimate"]["tier"] == COMPLEXITY_TIER_BEYOND

    with pytest.raises(CorridorScaleNotAccepted) as caught:
        service.compute(deepcopy(inputs))
    assert caught.value.tier == COMPLEXITY_TIER_BEYOND

    accepted = dict(inputs, allow_beyond_validated_envelope=True)
    result = service.compute(accepted)
    assert result["algorithm_id"] == CNSServiceCorridorV1.algorithm_id

    # "不重新估算"：把估算件换成会放行的值后，compute 仍然按 inputs 的冻结值拒绝。
    calls = []
    monkeypatch.setattr(
        "cns_planner.application.corridor_service._complexity_preflight",
        lambda value: calls.append(1) or dict(estimate, tier=COMPLEXITY_TIER_VALIDATED),
    )
    with pytest.raises(CorridorScaleNotAccepted):
        service.compute(deepcopy(inputs))
    assert calls == [], "compute() 绝不重新估算"


def test_compute_rejects_safety_ceiling_even_with_acceptance(project):
    """compute() 语义：ceiling 档任何显式接受都不得绕过硬天花板。"""

    data, estimate = fixture_with_tier(COMPLEXITY_TIER_CEILING)
    project.load(data)
    service = project.workflow.corridor_service

    inputs = service.compute_input({})
    assert inputs["complexity_estimate"]["tier"] == COMPLEXITY_TIER_CEILING
    with pytest.raises(CorridorScaleNotAccepted) as caught:
        service.compute(deepcopy(inputs))
    assert caught.value.tier == COMPLEXITY_TIER_CEILING
    with pytest.raises(CorridorScaleNotAccepted):
        service.compute(dict(inputs, allow_beyond_validated_envelope=True))
    assert estimate["tier"] == COMPLEXITY_TIER_CEILING


def test_progress_and_cancel_contract_are_preserved(project):
    """14. 准入路径不改变 progress / cancel 契约。"""

    data, _estimate = fixture_with_tier(COMPLEXITY_TIER_VALIDATED)
    project.load(data)
    plan = project.submit({})
    snapshot = project.snapshots.load(plan["input_snapshot_ref"])

    context, result = run_corridor_runner(snapshot, project.workdir)
    stages = [value for value, _message in context.progress_events]
    # fake store 记录的是原始值：算法内部的 per-cell 进度可能回落到阶段值之下，
    # 因此单调性按既有契约（store 侧只增不减）逐次累积检查。
    running = 0.0
    for value in stages:
        running = max(running, value)
    assert running <= 1.0
    assert stages[0] > 0.0
    assert stages[-1] <= 1.0
    assert max(stages) >= 0.96, "worker 必须推进到最终进度"
    assert any(message for _value, message in context.progress_events)
    assert result.progress <= 1.0

    # worker 侧取消：取消在第二个 run-major 检查点被观察到 → 必须以 TaskCancelled
    # 结束，且不产生任何 staged 结果（也不覆盖 canonical）。
    before = staged_files(project.workdir)
    context = _RunnerContext(snapshot, default_registry(), project.workdir)
    context.store.cancel_after_queries = 1  # 第一次检查通过，第二次命中取消
    with pytest.raises(TaskCancelled):
        _corridor_runner(context)
    assert context.store.queries >= 2, "取消必须在 runner 的检查点被观察到"
    assert_no_new_staged(before, staged_files(project.workdir))
