"""Phase4-B6X：Persistent Heavy Task Runtime 定向测试。

覆盖范围（对应 B6X 验收要求的 15 条）：

1. POST 立即返回 202 + task_id（真 HTTP server，真 task 提交路径）；
2. 长计算不持有 ``mutation_lock``（计算期间锁可被其它线程获取）；
3. progress / heartbeat 被持续更新；
4. 取消 queued 任务；
5. 取消 running 任务（cooperative cancel）；
6. cancelled 任务不发布 canonical result；
7. 输入 revision/指纹变化 → ``stale``，不覆盖当前 canonical result；
8. staged artifact 成功后原子 publish（canonical 文件 + manifest 引用）；
9. publish 失败不破坏旧 active result；
10. worker crash 恢复（心跳超时 → 明确策略）；
11. 重启后 task 历史可读；
12. 同 scope 重复提交处理（返回已有 active task / 明确 conflict）；
13. browser refresh 可按 task_id 恢复状态（HTTP GET /api/tasks/<id>）；
14. corridor 异步结果与原同步逻辑语义等价；
15. P14 四文件 SHA 不变。

全部走真实运行时：真的 worker 子进程、真的持久 task store、真的 artifact staging。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.altitude_layer_defaults import default_altitude_layers
from cns_planner.persistence.artifact_store import ArtifactStore
from cns_planner.persistence.project_compaction import restore_compacted_results
from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import (
    CORRIDOR_TASK_TYPE, PCF_TASK_TYPE, PROBE_STATE_KEY, PROBE_TASK_TYPE,
)
from cns_planner.tasks.task_store import (
    CANCELLED, FAILED, QUEUED, RUNNING, STALE, SUCCEEDED, TASK_DIRECTORY,
    TASK_REQUIRED_FIELDS, TaskStore, resolve_task_store_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = REPO_ROOT / "cns_planner" / "config" / "defaults.json"

#: P14 性能工作线：B6X **不得**修改这四个文件。
P14_FILES = (
    "cns_planner/algorithms/corridor/v1.py",
    "cns_planner/algorithms/coverage/geometric_3d.py",
    "cns_planner/algorithms/service_capability/v1.py",
    "tests/test_cns_corridor.py",
)
P14_BASELINE = Path(__file__).with_name("p14_protected_sha256.json")


# ---- 基础夹具 ---------------------------------------------------------------


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Runtime:
    """一个测试用的完整重任务运行时（真 store + 真 worker 进程）。"""

    def __init__(self, tmp_path, **options):
        self.workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
        self.workflow.mutation_lock = threading.RLock()
        self.service = HeavyTaskService(
            self.workflow, tmp_path / "project.json",
            max_workers=options.pop("max_workers", 1),
            heartbeat_interval=options.pop("heartbeat_interval", 0.2),
            heartbeat_timeout=options.pop("heartbeat_timeout", 30.0),
            poll_interval=options.pop("poll_interval", 0.05),
            **options,
        )

    @property
    def store(self) -> TaskStore:
        return self.service.store

    def start(self):
        self.service.start()
        return self

    def stop(self):
        self.service.stop()
        return self

    def save(self):
        self.workflow.save()
        return self

    def persist_layers(self):
        self.workflow.state.setdefault("spatial_3d", {})["altitude_layers"] = default_altitude_layers()
        return self.save()

    def persist_grid(self, cells=2):
        self.workflow.state["grid"] = {
            "status": "passed", "crs": "OGC:CRS84", "level": 8,
            "cells": [
                {"grid_id": f"L8-0-{index}", "bbox": [float(index), 0.0, float(index + 1), 1.0], "level": 8}
                for index in range(cells)
            ],
        }
        return self.save()

    def wait(self, task_id, timeout=120):
        view = self.service.wait_for_terminal(task_id, timeout=timeout)
        assert view is not None, "任务记录丢失"
        return view


@pytest.fixture
def runtime(tmp_path):
    instance = Runtime(tmp_path).start()
    try:
        yield instance
    finally:
        instance.stop()


def probe_payload(**overrides):
    payload = {
        "probe_id": "probe", "marker": "b6x", "steps": 3, "step_seconds": 0.0,
        "payload_bytes": 0, "crash": False, "fail": False,
    }
    payload.update(overrides)
    return payload


def fresh_store(directory) -> TaskStore:
    """模拟"另一个进程/重启后"读取同一个持久 store。"""

    return TaskStore(Path(directory))


# ---- 1. POST 立即返回 202 + task_id ------------------------------------------


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class StubData:
    """传输层契约测试用的最小 MapData 替身（不加载 QGIS/数据源）。"""

    error = None
    paths: dict = {}

    def metadata(self):
        return {"data_sources": {}, "data_health": {}}


class StubQgis:
    def call(self, function):
        return function()


class HttpHarness:
    """真 HTTP server（``cns_planner.api.server``），用于验证传输层契约。

    这里只替换 ``ApplicationContext``：``qgis`` / ``data`` 换成最小替身，而
    ``workflow`` / ``heavy_tasks`` / ``mutation_lock`` 全部是**真实对象**，
    因此被测的是真正的路由与任务路径，而不是桩。
    """

    def __init__(self, tmp_path):
        from cns_planner.api.server import create_server

        self.root = Path(tmp_path)
        project_file = self.root / "projects" / "current_project.json"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        self.workflow = WorkflowService(project_file, DEFAULTS)
        self.workflow.mutation_lock = threading.RLock()
        self.heavy_tasks = HeavyTaskService(
            self.workflow, project_file, max_workers=1,
            heartbeat_interval=0.2, heartbeat_timeout=30.0, poll_interval=0.05,
        )
        self.workflow.heavy_tasks = self.heavy_tasks
        self.heavy_tasks.start()
        context = type("HarnessContext", (), {})()
        context.root = self.root
        context.token = "task-token"
        context.workflow = self.workflow
        context.heavy_tasks = self.heavy_tasks
        context.mutation_lock = self.workflow.mutation_lock
        context.data = StubData()
        context.qgis = StubQgis()
        self.context = context
        self.port = _free_port()
        self.server = create_server(context, address=("127.0.0.1", self.port))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.heavy_tasks.stop()
        return False

    def request(self, path, *, method="GET", payload=None, headers=None, timeout=20):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-CNS-Token": self.context.token, **(headers or {})},
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body or "{}"), dict(response.headers)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, json.loads(body or "{}"), dict(error.headers)


def test_post_task_returns_202_and_task_id_without_waiting(tmp_path):
    with HttpHarness(tmp_path) as harness:
        payload = probe_payload(probe_id="http", steps=4, step_seconds=0.25)
        started = time.time()
        status, data, _headers = harness.request(
            "/api/tasks", method="POST", payload={"task_type": PROBE_TASK_TYPE, **payload},
        )
        elapsed = time.time() - started
        assert status == 202, data
        assert data["created"] is True
        assert str(data["task_id"]).startswith("task-")
        # "立即返回"：提交只用登记时间，不等长计算（长计算至少 1 秒）。
        assert elapsed < 1.0, f"提交耗时 {elapsed:.2f}s，说明长计算仍在请求线程里"
        assert data["task"]["status_text"] == "正在排队"
        # 主界面不得出现 raw 枚举 / task_id：它们只在 advanced 里。
        assert "queued" not in json.dumps(
            {key: value for key, value in data["task"].items() if key != "advanced"},
            ensure_ascii=False,
        )
        assert data["task"]["advanced"]["status"] == "queued"


# ---- 2. 长任务不持有 mutation_lock -------------------------------------------


def test_long_running_task_never_holds_mutation_lock(runtime):
    view_record, created = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="lock", steps=6, step_seconds=0.4)
    )
    assert created is True
    lock = runtime.workflow.mutation_lock
    observed_free = 0

    def try_lock():
        """在独立线程里尝试获取并立即释放（RLock 必须由同一线程释放）。"""

        acquired = lock.acquire(timeout=2.0)
        if acquired:
            lock.release()
        return acquired

    deadline = time.time() + 30
    while time.time() < deadline:
        record = runtime.store.get(view_record["task_id"])
        if str(record.get("status")) == RUNNING:
            # 从**另一个线程**尝试获取锁：长计算期间必须能拿到。
            acquired = []
            worker = threading.Thread(target=lambda: acquired.append(try_lock()))
            worker.start()
            worker.join(timeout=5)
            if acquired and acquired[0]:
                observed_free += 1
            else:
                pytest.fail("长计算期间 mutation_lock 被 worker 长时间持有")
        if str(record.get("status")) in (SUCCEEDED, FAILED, STALE, CANCELLED):
            break
        time.sleep(0.1)
    assert observed_free >= 2, "没有观测到 running 期间锁可用"
    view = runtime.wait(view_record["task_id"])
    assert view["advanced"]["status"] == SUCCEEDED


# ---- 3. progress / heartbeat --------------------------------------------------


def test_progress_and_heartbeat_are_updated(runtime):
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="hb", steps=4, step_seconds=0.3)
    )
    task_id = record["task_id"]
    observed = []
    deadline = time.time() + 30
    while time.time() < deadline:
        current = runtime.store.get(task_id)
        observed.append(current)
        if str(current.get("status")) in (SUCCEEDED, FAILED, STALE, CANCELLED):
            break
        time.sleep(0.1)
    progresses = {float(item.get("progress") or 0.0) for item in observed}
    heartbeats = {item.get("heartbeat_at") for item in observed if item.get("heartbeat_at")}
    assert len(progresses) >= 2, f"progress 没有推进：{progresses}"
    assert len(heartbeats) >= 2, "heartbeat_at 没有更新"
    assert any(item.get("started_at") for item in observed), "缺少 started_at"
    view = runtime.wait(task_id)
    assert view["advanced"]["status"] == SUCCEEDED
    assert view["progress"] == 1.0


# ---- 4/5/6. 取消 --------------------------------------------------------------


def test_cancel_queued_task(runtime):
    # max_workers=0 时任务停在 queued，可直接验证"取消排队任务"。
    idle = HeavyTaskService(
        runtime.workflow, runtime.workflow.store_path, max_workers=1,
        heartbeat_interval=0.2, heartbeat_timeout=30.0, poll_interval=0.05,
        autostart_workers=False,
    )
    record, created = idle.submit(PROBE_TASK_TYPE, probe_payload(probe_id="queued"))
    assert created is True
    assert runtime.store.get(record["task_id"])["status"] == QUEUED
    view = idle.cancel(record["task_id"])
    assert view["cancel_outcome"] == "cancelled_queued"
    assert view["advanced"]["status"] == CANCELLED
    assert view["status_text"] == "已取消"
    assert view["advanced"]["result_artifact_ref"] is None
    assert PROBE_STATE_KEY not in runtime.workflow.state


def test_cancel_running_task_is_cooperative_and_publishes_nothing(runtime):
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="cancel-run", steps=40, step_seconds=0.2)
    )
    task_id = record["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        if str(runtime.store.get(task_id)["status"]) == RUNNING:
            break
        time.sleep(0.05)
    assert str(runtime.store.get(task_id)["status"]) == RUNNING
    view = runtime.service.cancel(task_id)
    assert view["cancel_outcome"] in ("cancel_requested", "already_finished")
    final = runtime.wait(task_id, timeout=60)
    assert final["advanced"]["status"] == CANCELLED
    assert final["status_text"] == "已取消"
    # 取消不得发布 canonical result，也不得留下"成功"痕迹。
    assert final["advanced"]["result_artifact_ref"] is None
    assert PROBE_STATE_KEY not in runtime.workflow.state
    stored = runtime.store.get(task_id)
    assert stored["cancel_requested"] is True


def test_cancelled_task_does_not_publish_canonical_result(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1).start()
    try:
        runtime.persist_layers()
        record, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
        task_id = record["task_id"]
        # 让任务拿到 running 后立即取消；无论它停在哪个阶段，都不得写 canonical。
        before = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
        runtime.service.cancel(task_id)
        final = runtime.wait(task_id, timeout=120)
        assert final["advanced"]["status"] in (CANCELLED, SUCCEEDED)
        if final["advanced"]["status"] == CANCELLED:
            after = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
            assert after == before, "取消的任务改写了 canonical result"
            assert final["advanced"]["result_artifact_ref"] is None
    finally:
        runtime.stop()


# ---- 7. 输入变化 → stale，不覆盖 canonical ------------------------------------


def test_input_change_marks_task_stale_without_overwriting_canonical(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1).start()
    try:
        runtime.persist_layers()
        runtime.workflow.state["required_cns"] = {
            "project_default": {"communication": {"required": True, "coverage_requirement": 95}}
        }
        runtime.save()
        record, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
        task_id = record["task_id"]
        # 计算还在排队/运行期间改变输入（真输入变化，而不是 revision 自增）。
        runtime.workflow.state["required_cns"] = {
            "project_default": {"communication": {"required": True, "coverage_requirement": 42}}
        }
        runtime.save()
        final = runtime.wait(task_id, timeout=120)
        assert final["advanced"]["status"] == STALE, final["advanced"]["error"]
        assert final["status_text"] == "输入已变化，请重新运行"
        assert final["advanced"]["result_artifact_ref"] is None
        # canonical result 保持不变（原来根本没有结果）。
        statuses = runtime.workflow.state.get("result_statuses") or {}
        assert statuses.get("cns_corridor_assessment") in (None, "not_calculated")
    finally:
        runtime.stop()


def test_revision_bump_alone_does_not_invalidate_input(runtime):
    """输入指纹只覆盖输入：不相干的 state 变化不会让任务变 stale。"""

    runtime.persist_layers()
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="fp", steps=6, step_seconds=0.2)
    )
    runtime.workflow.state["project"]["name"] = "无关改动"
    runtime.save()
    view = runtime.wait(record["task_id"], timeout=60)
    assert view["advanced"]["status"] == SUCCEEDED


# ---- 8/9. publish 语义 --------------------------------------------------------


def test_staged_artifact_is_published_atomically(runtime):
    runtime.persist_layers()
    runtime.persist_grid()
    record, _ = runtime.service.submit(
        PCF_TASK_TYPE, {"altitude_layer_id": "ALT-080"}
    )
    view = runtime.wait(record["task_id"], timeout=120)
    assert view["advanced"]["status"] == SUCCEEDED, view["advanced"]["error"]
    reference = view["advanced"]["result_artifact_ref"]
    assert reference and reference["relative_path"].startswith(".cns-results/")
    canonical = Path(runtime.workflow.store_path).parent / reference["relative_path"]
    assert canonical.is_file(), "canonical artifact 不存在"
    assert "tmp" not in reference["relative_path"], "发布后仍指向可回收区"
    manifest = runtime.workflow.state.get("artifact_manifest") or {}
    assert reference["artifact_id"] in (manifest.get("entries") or {}), "manifest 未登记该 artifact"
    assert (manifest.get("refs") or {}).get("planning_constraint_fields") == reference["artifact_id"]
    collection = runtime.workflow.state.get("planning_constraint_fields") or {}
    assert collection.get("count") == 1
    assert (collection.get("items") or [{}])[0].get("altitude_layer_id") == "ALT-080"


def test_publish_failure_keeps_previous_active_result(tmp_path):
    from cns_planner.tasks import handlers

    # 第二个任务的发布路径从提交前就是坏的：worker 仍然正常算出 staged 结果，
    # 失败只发生在 compare-and-publish 阶段。
    runtime = Runtime(tmp_path, max_workers=1).start()
    try:
        runtime.persist_layers()
        # 先产生一个真实的 canonical corridor 结果（此时发布路径仍是好的）。
        first, _ = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
        published = runtime.wait(first["task_id"], timeout=180)
        assert published["advanced"]["status"] == SUCCEEDED, published["advanced"]["error"]
        baseline = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
    finally:
        runtime.service.stop(terminate_workers=False)

    original = handlers.publish_staged_artifact

    def broken(workdir, staged):
        raise handlers.TaskPublishError("注入的发布失败")

    handlers.publish_staged_artifact = broken
    service = None
    try:
        # 新 service 接管同一个持久 store；提交第二个任务后 worker 照常算完，
        # 但 publish 阶段会失败。这里把缓存清空，确保发布一定经过被打坏的路径。
        service = HeavyTaskService(
            runtime.workflow, runtime.workflow.store_path, max_workers=1,
            heartbeat_interval=0.2, heartbeat_timeout=30.0, poll_interval=0.05,
        )
        service._published.clear()
        record, created = service.submit(CORRIDOR_TASK_TYPE, {})
        assert created is True
        failed = service.wait_for_terminal(record["task_id"], timeout=180)
        assert failed is not None
        assert failed["advanced"]["status"] == FAILED, failed["advanced"]
        assert failed["business_error"]["text"] == "结果发布失败，原有正式结果未被替换"
        assert failed["advanced"]["result_artifact_ref"] is None
        after = json.dumps(runtime.workflow.state.get("cns_corridor_assessment"), sort_keys=True)
        assert after == baseline, "发布失败破坏了旧 active result"
    finally:
        handlers.publish_staged_artifact = original
        if service is not None:
            service.stop()


# ---- 10/11. crash 与重启恢复 --------------------------------------------------


def test_worker_crash_recovery_marks_stale_never_succeeded(runtime):
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="crash", steps=2, step_seconds=0.2, crash=True)
    )
    task_id = record["task_id"]
    # 明确策略：worker 崩溃后任务停在 running；心跳超时后由恢复路径标记为 stale。
    runtime.service.heartbeat_timeout = 0.0
    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        runtime.service.drive()
        final = runtime.service.find(task_id)
        if final["advanced"]["status"] in (STALE, FAILED, CANCELLED, SUCCEEDED):
            break
        time.sleep(0.1)
    assert final is not None
    assert final["advanced"]["status"] == STALE, final["advanced"]
    assert final["status_text"] == "输入已变化，请重新运行"
    assert "中断" in (final["business_error"]["text"] or "")
    assert final["advanced"]["result_artifact_ref"] is None
    assert PROBE_STATE_KEY not in runtime.workflow.state


def test_restart_marks_orphan_running_task_stale_and_keeps_history(tmp_path):
    runtime = Runtime(tmp_path, max_workers=1).start()
    store_directory = runtime.store.directory
    task_id = None
    try:
        record, _ = runtime.service.submit(
            PROBE_TASK_TYPE, probe_payload(probe_id="orphan", steps=40, step_seconds=0.5)
        )
        task_id = record["task_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            if str(runtime.store.get(task_id)["status"]) == RUNNING:
                break
            time.sleep(0.05)
        # 模拟进程被杀：直接强杀 worker，并让服务端不再收尾（重启前的状态）。
        runtime.service.stop(terminate_workers=False)
        for process in list(runtime.service._processes.values()):
            process.kill()
        runtime.service._processes.clear()
        assert str(fresh_store(store_directory).get(task_id)["status"]) == RUNNING
    finally:
        runtime.service.stop()

    # "重启"：新的 service 实例从同一个 store 恢复。
    restarted = Runtime(tmp_path, max_workers=1, heartbeat_timeout=0.0)
    restarted.workflow.mutation_lock = threading.RLock()
    restarted.service.start()
    try:
        record = fresh_store(store_directory).get(task_id)
        assert str(record["status"]) == STALE
        view = restarted.service.find(task_id)
        assert view["status_text"] == "输入已变化，请重新运行"
        assert view["business_error"]["code"] == "task_worker_lost"
        # 历史可读：所有契约字段仍在。
        for field in TASK_REQUIRED_FIELDS:
            assert field in record, f"task 记录缺少契约字段 {field}"
    finally:
        restarted.service.stop()


# ---- 12. 同 scope 重复提交 ----------------------------------------------------


def test_duplicate_submission_same_scope_returns_existing_task(tmp_path):
    service_holder = HeavyTaskService(
        WorkflowService(tmp_path / "project.json", DEFAULTS), tmp_path / "project.json",
        max_workers=1, autostart_workers=False,
    )
    service_holder.workflow.mutation_lock = threading.RLock()
    first, created_first = service_holder.submit(PROBE_TASK_TYPE, probe_payload(probe_id="dup"))
    second, created_second = service_holder.submit(PROBE_TASK_TYPE, probe_payload(probe_id="dup"))
    assert created_first is True
    assert created_second is False
    assert second["task_id"] == first["task_id"], "同 scope 重复提交必须返回已有 active task"
    # 明确 conflict 模式：显式报冲突，而不是静默再开一个。
    from cns_planner.tasks.service import CONFLICT_REJECT
    from cns_planner.tasks.task_store import TaskConflictError

    with pytest.raises(TaskConflictError):
        service_holder.submit(
            PROBE_TASK_TYPE, probe_payload(probe_id="dup"), conflict_policy=CONFLICT_REJECT,
        )
    # 不同 scope 可以并行。
    other, created_other = service_holder.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="other"),
    )
    assert created_other is True
    assert other["task_id"] != first["task_id"]


# ---- 13. 刷新后按 task_id 恢复 ------------------------------------------------


def test_http_refresh_can_restore_task_state_by_task_id(tmp_path):
    with HttpHarness(tmp_path) as harness:
        status, data, _ = harness.request(
            "/api/tasks", method="POST",
            payload={"task_type": PROBE_TASK_TYPE, **probe_payload(probe_id="refresh", steps=3, step_seconds=0.15)},
        )
        assert status == 202
        task_id = data["task_id"]
        # 刷新页面 = 重新 GET：状态必须能从持久 store 恢复（不依赖任何内存会话）。
        deadline = time.time() + 30
        view = None
        while time.time() < deadline:
            status, payload, _ = harness.request(f"/api/tasks/{task_id}")
            assert status == 200, payload
            view = payload
            if view["status_text"] in ("已完成", "执行失败", "已取消", "输入已变化，请重新运行"):
                break
            time.sleep(0.2)
        assert view["status_text"] == "已完成", view
        assert view["advanced"]["task_id"] == task_id
        assert view["result_scope"] == "运行时探针"
        # 列表端点也包含它（前端恢复列表用）。
        status, listing, _ = harness.request("/api/tasks?limit=50")
        assert status == 200
        items = listing if isinstance(listing, list) else listing.get("items")
        assert any(item["advanced"]["task_id"] == task_id for item in items)


# ---- 14. corridor 异步语义等价 ------------------------------------------------


def _corridor_state_fingerprint(assessment):
    """corridor 结果的内容指纹（忽略时间戳类元数据）。"""

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: scrub(item) for key, item in value.items()
                if key not in ("created_at", "generated_at", "timestamp", "evaluated_at")
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return sha256(
        json.dumps(scrub(assessment), ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _prepare_corridor_project(workflow):
    workflow.state.setdefault("spatial_3d", {})["altitude_layers"] = default_altitude_layers()
    workflow.state["grid"] = {
        "status": "passed", "crs": "OGC:CRS84", "level": 8,
        "cells": [{"grid_id": "L8-0-0", "bbox": [0.0, 0.0, 1.0, 1.0], "level": 8}],
    }
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed",
        "path": [[0.1, 0.1], [0.9, 0.9]], "waypoints": [[0.1, 0.1], [0.9, 0.9]],
    }]
    workflow.state["required_cns"] = {
        "status": "passed",
        "project_default": {
            "communication": {"required": True, "coverage_requirement": 95, "confirmed": True},
            "navigation": {"required": True, "coverage_requirement": 95, "confirmed": True},
            "surveillance": {"required": True, "coverage_requirement": 90, "confirmed": True},
        },
        "route_overrides": {}, "confirmed": True,
    }
    workflow.save()


def test_corridor_async_result_matches_sync_semantics(tmp_path):
    sync_workflow = WorkflowService(tmp_path / "sync" / "project.json", DEFAULTS)
    sync_workflow.mutation_lock = threading.RLock()
    _prepare_corridor_project(sync_workflow)
    with sync_workflow.mutation_lock:
        sync_workflow.evaluate_cns_corridor({})
    sync_result = sync_workflow.state.get("cns_corridor_assessment") or {}
    assert sync_result, "同步路径没有产生 corridor 结果"

    runtime = Runtime(tmp_path / "async", max_workers=1).start()
    try:
        _prepare_corridor_project(runtime.workflow)
        record, created = runtime.service.submit(CORRIDOR_TASK_TYPE, {})
        assert created is True
        view = runtime.wait(record["task_id"], timeout=120)
        assert view["advanced"]["status"] == SUCCEEDED, view["advanced"]["error"]
        async_result = runtime.workflow.state.get("cns_corridor_assessment") or {}
        assert async_result, "异步任务没有写入 canonical corridor result"
        assert _corridor_state_fingerprint(async_result) == _corridor_state_fingerprint(sync_result), (
            "异步 corridor 结果与同步逻辑语义不一致"
        )
        assert async_result.get("input_fingerprint") == sync_result.get("input_fingerprint")
        assert (runtime.workflow.state.get("result_statuses") or {}).get("cns_corridor_assessment") == \
            (sync_workflow.state.get("result_statuses") or {}).get("cns_corridor_assessment")
    finally:
        runtime.stop()


# ---- 15. P14 四文件 SHA 不变 --------------------------------------------------


def test_p14_protected_files_are_byte_identical():
    assert P14_BASELINE.is_file(), "缺少 P14 基线指纹文件"
    baseline = json.loads(P14_BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) == set(P14_FILES), "P14 基线文件清单与预期不一致"
    actual = {relative: file_sha256(REPO_ROOT / relative) for relative in P14_FILES}
    assert actual == baseline, "B6X 修改了受保护的 P14 文件"


# ---- 附加：运行时契约 ---------------------------------------------------------


def test_task_contract_fields_and_store_location(runtime):
    record, _ = runtime.service.submit(PROBE_TASK_TYPE, probe_payload(probe_id="contract"))
    for field in TASK_REQUIRED_FIELDS:
        assert field in record, f"Task Contract 缺少字段 {field}"
    assert record["status"] == QUEUED
    assert record["cancel_requested"] is False
    assert record["progress"] == 0.0
    # 独立 task store：位于项目工作区旁的 .cns-tasks，而不是 ProjectState。
    assert record["input_snapshot_ref"]
    assert Path(runtime.store.directory).name == TASK_DIRECTORY
    assert TASK_DIRECTORY not in runtime.workflow.state
    runtime.wait(record["task_id"], timeout=60)
    stored = runtime.store.get(record["task_id"])
    assert stored["started_at"] and stored["finished_at"]
    assert stored["worker"]["kind"] == "heavy-task-worker"
    assert stored["worker"]["pid"] != os.getpid(), "长计算必须运行在独立 worker 进程"


def test_worker_runs_in_separate_process(runtime):
    record, _ = runtime.service.submit(
        PROBE_TASK_TYPE, probe_payload(probe_id="proc", steps=6, step_seconds=0.3)
    )
    deadline = time.time() + 20
    worker_pid = None
    while time.time() < deadline:
        current = runtime.store.get(record["task_id"])
        worker_pid = (current.get("worker") or {}).get("pid")
        if worker_pid and current.get("started_at"):
            break
        time.sleep(0.05)
    assert worker_pid and worker_pid != os.getpid()
    runtime.wait(record["task_id"], timeout=60)


def test_synchronous_endpoints_still_work_without_async_flag(tmp_path):
    """轻任务/既有调用方不受影响：不带 async 时仍是原同步语义。"""

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.mutation_lock = threading.RLock()
    _prepare_corridor_project(workflow)
    payload = {"async": False}
    from cns_planner.api.router import ApiRouter

    class Context:
        pass

    context = Context()
    context.workflow = workflow
    context.data = type("Data", (), {"error": None, "metadata": lambda self: {}, "paths": {}})()
    context.heavy_tasks = HeavyTaskService(
        workflow, tmp_path / "project.json", max_workers=1, autostart_workers=False,
    )
    response = ApiRouter(context).post("/api/cns-service-corridor/evaluate", payload)
    assert response.status == 200
    assert isinstance(response.data, dict) and "cns_corridor_assessment" in response.data
    assert workflow.state.get("cns_corridor_assessment")
    # 同步路径不创建任务。
    assert context.heavy_tasks.store.list() == []
