"""Round 31-D：CNS 能力缺口评估（P15）接入后台计算任务体系 —— 定向契约测试。

本轮只解决"逐体元判定后台化 + 用户可见真实进度"，因此测试**刻意**走可控小项目
（``test_corridor_site_planner_v2.configured``）+ 真 worker 子进程 + 真持久 task
store，不跑真实大项目，也不重复运行 18 分钟级的 P16 全量试算。

覆盖验收面：

1. 业务端点带 ``async: true`` 提交：立刻返回 ``202 + task_id``，不等完整 P15；
2. 同一个 task_id 可轮询（``GET /api/tasks/<id>`` 契约面）；
3. ``progress`` 单调不下降，且只由**真实执行节点**决定（无 sleep、无假百分比）；
4. ``heartbeat`` 由 worker 持续更新，读取侧给出 ``heartbeat_age_seconds``；
5. 计算段**绝不写 canonical state**（worker 侧的 session 一旦落盘立即失败）；
6. 页面刷新后可按 task_id 恢复（持久 task store，另一个进程实例也能读到）；
7. 成功后发布正式结果并给出业务结果名（前端据此自动刷新，无需 F5）；
8. 上游 P14 变化时以中文业务原因作废，绝不覆盖正式结果、绝不伪造成功；
9. 同步 API 原有调用者不受影响（不带 ``async`` 仍是同步语义）；
10. 后台化前后 P15 结论**逐字段一致**，携带规划目标更新时事务与失效语义不变；
11. P15 结论未变化时**不**错误使 P16 stale；
12. 发布失败 / 取消都不污染正式结果。
"""

from __future__ import annotations

from copy import deepcopy
import threading
import time

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.tasks import handlers
from cns_planner.tasks.handlers import RESULT_SCOPE_TEXT, TaskPublishError
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import (
    P15_TASK_TYPE, TASK_ENDPOINTS, _WorkerReadOnlySession, _p15_scope, task_spec,
)
from cns_planner.tasks.task_store import TaskStore

from test_corridor_site_planner_v2 import configured


ENDPOINT = "/api/cns-corridor-gap/evaluate"


def _without_timing(value):
    """剔除运行时耗时画像（键名以 ``_seconds`` 结尾或就叫 ``seconds``）。"""

    if isinstance(value, dict):
        return {
            str(key): _without_timing(item) for key, item in value.items()
            if str(key) != "seconds" and not str(key).endswith("_seconds")
        }
    if isinstance(value, list):
        return [_without_timing(item) for item in value]
    return value


def _objective_payload():
    """一份显式确认的规划目标（结构与 Step05「保存目标」提交的一致）。"""

    return {"routes": {"R1": {"route_id": "R1", "subsystems": {"C": {"objectives": {
        "max_confirmed_deficit_volume_fraction": {
            "value": 0.5, "operator": "<=", "source": "test", "confirmed": True,
        },
    }}}}}}


class _P15ApiContext:
    """路由上下文替身：``workflow`` / ``heavy_tasks`` 都是真实对象。"""

    def __init__(self, workflow, heavy_tasks):
        self.workflow, self.heavy_tasks, self.data = workflow, heavy_tasks, object()


class P15Runtime:
    """真 worker 子进程 + 真持久 store 的 P15 后台运行时。"""

    def __init__(self, tmp_path, workflow):
        self.workflow = workflow
        self.workflow.mutation_lock = threading.RLock()
        self.service = HeavyTaskService(
            self.workflow, tmp_path / "project.json", max_workers=1,
            heartbeat_interval=0.2, heartbeat_timeout=60.0, poll_interval=0.05,
        )
        self.workflow.heavy_tasks = self.service

    def start(self):
        self.service.start()
        return self

    def stop(self):
        self.service.stop()
        return self

    @property
    def store(self):
        return self.service.store

    def api(self):
        return ApiRouter(_P15ApiContext(self.workflow, self.service))


# ---- 登记契约（端点 / 业务名 / scope） ------------------------------------------


def test_p15_endpoint_is_registered_as_heavy_task():
    assert TASK_ENDPOINTS[ENDPOINT] == P15_TASK_TYPE
    assert RESULT_SCOPE_TEXT[P15_TASK_TYPE] == "CNS 能力缺口评估"
    assert task_spec(P15_TASK_TYPE).task_name == "CNS 能力缺口评估"


def test_p15_scope_is_bound_to_operational_routes():
    state = {"operational_routes": [{"route_id": "R2"}, {"route_id": "R1"}]}
    assert _p15_scope(state, {}) == "cns_corridor_gap:R1|R2"
    assert _p15_scope({}, {}) == "cns_corridor_gap:no-route"


def test_new_project_default_algorithm_configuration_resolves_for_p15(tmp_path):
    """新项目的默认算法配置必须能解析出 P15 的分析器（无需用户手改选择）。"""

    from cns_planner.tasks.task_input import algorithm_manifests

    workflow = configured(tmp_path)
    manifests = algorithm_manifests(
        workflow.algorithm_registry, workflow.state.get("algorithm_selection"),
        ("corridor_gap_analyzer",),
    )
    entry = manifests["corridor_gap_analyzer"]
    assert entry["algorithm_id"] and entry["version"]
    instance = workflow.algorithm_registry.create(
        "corridor_gap_analyzer", entry["algorithm_id"], entry["version"],
        entry["parameters"],
    )
    assert instance.algorithm_id == entry["algorithm_id"]


def test_server_async_detection_accepts_p15_endpoint():
    """真实 HTTP 服务层的异步判定必须接受 P15 端点（不带 async 仍走同步）。"""

    from cns_planner.api.server import wants_async_submission

    assert wants_async_submission(ENDPOINT, {"async": True}) is True
    assert wants_async_submission(ENDPOINT, {}) is False


# ---- 3 / 5：真实阶段进度与"计算段不写 canonical state" ---------------------------


def test_p15_plan_reports_real_stages_and_never_writes_state(tmp_path):
    workflow = configured(tmp_path)
    service = workflow.corridor_gap_service
    before = deepcopy(workflow.state)
    events = []
    outcome = service.plan(
        {}, on_progress=lambda value, message=None: events.append((value, message)),
    )
    values = [value for value, _ in events]
    messages = [str(message) for _, message in events]
    # 阶段来自真实执行节点，不是固定 sleep，也不是假百分比。
    assert messages[0] == "正在准备 CNS 能力缺口输入"
    assert "正在校验 CNS 服务走廊结论是否仍然有效" in messages
    assert "正在按规划目标判定能力缺口" in messages
    assert values == sorted(values) and values[-1] <= 1.0
    # 纯计算段：state 逐字段不变（目标 / 结论 / result_statuses 都不动），也不落盘。
    assert workflow.state == before
    # 只读钩子绝不改变计算结果：与不传回调逐字段一致。
    assert _without_timing(service.plan({})["result"]) == _without_timing(outcome["result"])


def test_worker_side_session_refuses_to_save_canonical_state():
    session = _WorkerReadOnlySession({"cns_corridor_gap_assessment": {}})
    with pytest.raises(RuntimeError):
        session.save()


# ---- 1 / 2 / 4 / 6 / 7 / 10：async 端到端 ---------------------------------------


def test_p15_async_submit_returns_task_id_and_publishes_the_same_result(tmp_path):
    workflow = configured(tmp_path)
    # 同步路径（既有调用者的语义）先跑一次，作为逐字段对照。
    workflow.evaluate_cns_corridor_gap()
    sync_result = deepcopy(workflow.state["cns_corridor_gap_assessment"])
    sync_status = workflow.state["result_statuses"]["cns_corridor_gap_assessment"]

    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        api = runtime.api()
        started = time.perf_counter()
        response = api.post(ENDPOINT, {"async": True})
        submit_seconds = time.perf_counter() - started
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        assert task_id
        # 1. 提交立刻返回（远小于完整 P15）。
        assert submit_seconds < 30.0

        # 2. 同一个 task_id 可轮询。
        view = api.get(f"/api/tasks/{task_id}", {}, {}).data
        assert view["task_id"] == task_id
        assert view["task_name"] == "CNS 能力缺口评估"
        assert view["advanced"]["status"] in ("queued", "running", "succeeded")
        assert view["message"]

        final = runtime.service.wait_for_terminal(task_id, timeout=300)
        assert final is not None, "任务记录丢失"
        assert final["advanced"]["status"] == "succeeded", final

        # 4. heartbeat 契约。
        assert final["heartbeat_at"]
        assert isinstance(final["heartbeat_age_seconds"], (int, float))

        # 7. 成功后给出业务结果名（前端据此自动刷新正式结果，无需 F5）。
        assert final["result_scope"] == "CNS 能力缺口评估"
        assert final["result_summary"].get("route_count") == 1

        # 6. 刷新页面后仍可恢复：另一个 store 实例（等价于新进程）能按 task_id 读到它。
        assert TaskStore(runtime.store.directory).find(task_id) is not None

        # 10. 后台化前后最终结论逐字段一致，且 result_statuses 同源。
        assert _without_timing(deepcopy(workflow.state["cns_corridor_gap_assessment"])) == (
            _without_timing(sync_result)
        )
        assert workflow.state["result_statuses"]["cns_corridor_gap_assessment"] == sync_status
        # 3. 进度必须走到收口段。
        assert float(final["progress"]) >= 0.9
    finally:
        runtime.stop()


def test_p15_async_result_matches_sync_with_objectives_update(tmp_path):
    """携带规划目标更新时：异步与同步的落库形态（目标 + 结论 + 失效）逐字段一致。"""

    objectives = _objective_payload()
    (tmp_path / "sync").mkdir(parents=True, exist_ok=True)
    (tmp_path / "async").mkdir(parents=True, exist_ok=True)
    sync = configured(tmp_path / "sync")
    sync.evaluate_cns_corridor_site_plan({})
    sync.evaluate_cns_corridor_gap({"cns_planning_objectives": objectives})

    workflow = configured(tmp_path / "async")
    workflow.evaluate_cns_corridor_site_plan({})
    runtime = P15Runtime(tmp_path / "async", workflow).start()
    try:
        response = runtime.api().post(
            ENDPOINT, {"async": True, "cns_planning_objectives": objectives},
        )
        assert response.status == 202, response.data
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=300)
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()

    assert workflow.state["cns_planning_objectives"] == sync.state["cns_planning_objectives"]
    assert _without_timing(workflow.state["cns_corridor_gap_assessment"]) == (
        _without_timing(sync.state["cns_corridor_gap_assessment"])
    )
    assert workflow.state["result_statuses"]["cns_corridor_gap_assessment"] == (
        sync.state["result_statuses"]["cns_corridor_gap_assessment"]
    )
    # 规划目标更新仍然按原语义使 P16 失效（不是"无条件失效"，而是与同步路径同源）。
    assert sync.state["result_statuses"]["cns_corridor_site_plan"] == "stale"
    assert workflow.state["result_statuses"]["cns_corridor_site_plan"] == "stale"


# ---- 9：同步调用者不受影响 ------------------------------------------------------


def test_sync_endpoint_stays_synchronous_and_registers_no_task(tmp_path):
    workflow = configured(tmp_path)
    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {})  # 不带 async
        assert response.status == 200
        assert response.data["cns_corridor_gap_assessment"]["input_fingerprint"]
        assert workflow.state["result_statuses"]["cns_corridor_gap_assessment"] in (
            "passed", "failed", "pending_confirmation", "missing_data", "not_applicable",
        )
        assert workflow.state["result_statuses"]["cns_corridor_gap_assessment"] != "stale"
        assert runtime.service.list() == [], "同步调用不得登记任何后台任务"
    finally:
        runtime.stop()


# ---- 8：上游变化 → 拒绝发布，绝不覆盖正式结果 -----------------------------------


def test_p14_change_after_submit_is_refused_without_overwriting(tmp_path):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_gap()
    published_before = deepcopy(workflow.state["cns_corridor_gap_assessment"])

    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        task_id = response.data["task_id"]
        # 提交之后上游 P14 结论被改写：任务必须作废，绝不据此发布过期 P15。
        workflow.state["cns_corridor_assessment"]["input_fingerprint"] = "changed-after-submit"
        workflow.state.setdefault("result_statuses", {})["cns_corridor_assessment"] = "passed"
        workflow.save()

        final = runtime.service.wait_for_terminal(task_id, timeout=300)
        assert final is not None, "任务记录丢失"
        assert final["advanced"]["status"] == "stale", final
        text = (final.get("business_error") or {}).get("text") or ""
        assert text, final
        assert "变化" in text or "上游" in text, text
        # 提交时已经存在的正式结果绝不被覆盖。
        assert workflow.state["cns_corridor_gap_assessment"] == published_before
    finally:
        runtime.stop()


# ---- 11：结论未变化时绝不错误使 P16 失效 ----------------------------------------


def test_unchanged_p15_conclusion_does_not_stale_p16(tmp_path):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_gap()
    p15_before = deepcopy(workflow.state["cns_corridor_gap_assessment"])
    workflow.evaluate_cns_corridor_site_plan({})
    p16_before = deepcopy(workflow.state["cns_corridor_site_plan"])
    assert workflow.state["result_statuses"]["cns_corridor_site_plan"] != "stale"

    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=300)
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()

    # 同一输入重算得到同一 fingerprint：P16 的基线仍成立，不得被无条件失效。
    assert workflow.state["cns_corridor_gap_assessment"]["input_fingerprint"] == (
        p15_before["input_fingerprint"]
    )
    assert workflow.state["cns_corridor_site_plan"] == p16_before
    assert workflow.state["result_statuses"]["cns_corridor_site_plan"] != "stale"


# ---- 12：发布失败 / 取消都不污染正式结果 ----------------------------------------


def test_invalid_staged_payload_fails_publish_and_keeps_canonical_state(
    tmp_path, monkeypatch,
):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_gap()
    published_before = deepcopy(workflow.state["cns_corridor_gap_assessment"])

    spec = task_spec(P15_TASK_TYPE)
    registry = workflow.algorithm_registry
    worker_payload = spec.plan_for(workflow.state, {}, registry)["worker_payload"]
    record = {
        "task_type": P15_TASK_TYPE,
        "input_fingerprint": spec.fingerprint_for(workflow.state, worker_payload, registry),
        "worker_payload": worker_payload,
        "result_summary": {
            "staged": {"artifact_id": "staged-id", "relative_path": "staged.json.gz",
                       "sha256": "staged-id", "artifact_type": "corridor.gap.detail"},
            "summary": {},
        },
    }
    monkeypatch.setattr(handlers, "publish_staged_artifact", lambda workdir, staged: dict(staged))
    monkeypatch.setattr(
        handlers, "_load_staged_payload",
        lambda *args, **kwargs: {"logical_key": "cns_corridor_gap_assessment",
                                 "value": {"result": {}}},
    )
    with pytest.raises(TaskPublishError):
        handlers.publish_task_result(workflow, record, workdir=tmp_path)
    assert workflow.state["cns_corridor_gap_assessment"] == published_before


def test_cancel_never_publishes_a_partial_result(tmp_path):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_gap()
    published_before = deepcopy(workflow.state["cns_corridor_gap_assessment"])

    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        task_id = response.data["task_id"]
        runtime.service.cancel(task_id)
        final = runtime.service.wait_for_terminal(task_id, timeout=300)
        assert final is not None
        assert final["advanced"]["status"] in ("cancelled", "succeeded"), final
        if final["advanced"]["status"] == "cancelled":
            assert workflow.state["cns_corridor_gap_assessment"] == published_before
        else:
            # 取消没抢在计算之前：正式结果只能是等价重算结论（同一输入指纹）。
            assert workflow.state["cns_corridor_gap_assessment"]["input_fingerprint"] == (
                published_before["input_fingerprint"]
            )
    finally:
        runtime.stop()


# ---- 反向顺序：先后台、再同步，同样一致 ------------------------------------------

def test_legacy_algorithm_selection_falls_back_like_the_sync_entry(tmp_path):
    """旧项目保存的算法版本无 factory 时：后台任务与同步入口用**同一份**有效身份。

    这是真实旧项目上的阻断点：``algorithm_selection.corridor_gap_analyzer.version``
    仍是升级前的 ``1.0``，同步入口按 ``_algorithm_compatibility`` 只读回落到当前的
    唯一注册版本，而后台任务快照若记录原始 ``1.0`` 就会在 worker 里必然失败。
    """

    workflow = configured(tmp_path)
    workflow.state["algorithm_selection"]["corridor_gap_analyzer"]["version"] = "1.0"
    workflow.save()
    workflow.evaluate_cns_corridor_gap()
    sync_result = deepcopy(workflow.state["cns_corridor_gap_assessment"])
    assert sync_result.get("algorithm_version") not in (None, "1.0")

    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        final = runtime.service.wait_for_terminal(task_id, timeout=300)
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()

    assert _without_timing(deepcopy(workflow.state["cns_corridor_gap_assessment"])) == (
        _without_timing(sync_result)
    )


def test_async_runs_first_and_sync_matches(tmp_path):
    workflow = configured(tmp_path)
    runtime = P15Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=300)
        assert final["advanced"]["status"] == "succeeded", final
        async_result = deepcopy(workflow.state["cns_corridor_gap_assessment"])
        workflow.evaluate_cns_corridor_gap()
        sync_result = deepcopy(workflow.state["cns_corridor_gap_assessment"])
        assert _without_timing(async_result) == _without_timing(sync_result)
    finally:
        runtime.stop()
