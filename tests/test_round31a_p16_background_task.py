"""Round 31-A：CNS 设施规划（P16）接入后台计算任务体系 —— 定向契约测试。

本轮只解决"长任务后台化 + 用户可见进度"，因此测试**刻意**先走可控小项目
（``test_corridor_site_planner_v2.configured``）+ 真 worker 子进程 + 真持久 task
store，不跑真实 20 分钟项目。

覆盖验收面：

1. 业务端点带 ``async: true`` 提交：立刻返回 ``202 + task_id``，不等完整 P16；
2. 同一个 task_id 可轮询（``GET /api/tasks/<id>`` 契约面）；
3. ``progress`` 单调不下降，且只由**真实工作量**决定（无 sleep、无假进度）；
4. ``heartbeat`` 由 worker 持续更新，读取侧给出 ``heartbeat_age_seconds``；
5. ``message`` 随真实阶段变化（中文业务文案，不含 P14/P15/P16 与指纹）；
6. 页面刷新后可按 task_id 恢复（持久 task store，另一个进程实例也能读到）；
7. 成功后发布正式结果并给出业务结果名（前端据此自动刷新，无需 F5）；
8. 上游结论变化时以中文业务原因作废，绝不覆盖正式结果、绝不伪造成功；
9. 同步 API 原有调用者不受影响（不带 ``async`` 仍是同步语义）；
10. 后台化前后 P16 最终规划结果**逐字段一致**（只剔除运行时耗时画像）。
"""

from __future__ import annotations

from copy import deepcopy
import threading
import time

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application import corridor_site_planning_service as p16_service
from cns_planner.application.corridor_site_planning_service import (
    _p16_loop_progress, _p16_tier_message, _progress_reporter,
)
from cns_planner.domain.site_planning import REUSE_TIERS
from cns_planner.tasks.handlers import RESULT_SCOPE_TEXT
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import (
    P16_TASK_TYPE, TASK_ENDPOINTS, _p16_scope,
)
from cns_planner.tasks.task_store import TaskStore

from test_corridor_site_planner_v2 import candidate, configured


ENDPOINT = "/api/cns-corridor-site-plan/evaluate"


def _without_timing(value):
    """剔除运行时耗时画像：两次运行的秒数必然不同，它不属于业务契约面。

    剔除规则是显式的（键名以 ``_seconds`` 结尾或就叫 ``seconds``），不是"猜哪些字段
    可以不一样"——业务字段一个都不放过。
    """

    if isinstance(value, dict):
        return {
            str(key): _without_timing(item) for key, item in value.items()
            if str(key) != "seconds" and not str(key).endswith("_seconds")
        }
    if isinstance(value, list):
        return [_without_timing(item) for item in value]
    return value


class _P16ApiContext:
    """路由上下文替身：``workflow`` / ``heavy_tasks`` 都是真实对象。"""

    def __init__(self, workflow, heavy_tasks):
        self.workflow, self.heavy_tasks, self.data = workflow, heavy_tasks, object()


class P16Runtime:
    """真 worker 子进程 + 真持久 store 的 P16 后台运行时。"""

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
        return ApiRouter(_P16ApiContext(self.workflow, self.service))


# ---- 登记契约（端点 / 业务名 / scope） ------------------------------------------


def test_p16_endpoint_is_registered_as_heavy_task():
    assert TASK_ENDPOINTS[ENDPOINT] == P16_TASK_TYPE
    assert RESULT_SCOPE_TEXT[P16_TASK_TYPE] == "CNS 设施规划"


def test_p16_scope_is_bound_to_operational_routes():
    state = {"operational_routes": [{"route_id": "R2"}, {"route_id": "R1"}]}
    assert _p16_scope(state, {}) == "cns_corridor_site_plan:R1|R2"
    assert _p16_scope({}, {}) == "cns_corridor_site_plan:no-route"


# ---- 3 / 5：进度与文案（真实工作量，不是假进度） ---------------------------------


def test_loop_progress_uses_real_workload_and_never_goes_backwards():
    limit = 6
    previous = -1.0
    selected = 0
    for tier_index in range(len(REUSE_TIERS)):
        for _ in range(2):
            value = _p16_loop_progress(tier_index, selected, limit)
            assert 0.15 <= value <= 0.85
            assert value >= previous, "进度绝不回退"
            previous = value
            selected += 1
    # 同一个 tier 内"选中更多方案"必然推进进度（真实工作量驱动）。
    assert _p16_loop_progress(0, 3, limit) > _p16_loop_progress(0, 0, limit)
    # 选择上限缺失 / 为 0 时不得除零，也不越过区间。
    assert 0.15 <= _p16_loop_progress(0, 0, 0) <= 0.85


def test_progress_reporter_is_monotonic_throttled_and_delivers_completion():
    events = []
    report = _progress_reporter(lambda value, message=None: events.append((value, message)))
    report(0.10, "第一段")
    report(0.05, "回退必须被夹住")
    report(0.90, "节流窗口内的重复上报可以丢")
    report(1.0, "完成")
    values = [value for value, _ in events]
    assert values == sorted(values)
    assert values[0] == 0.10
    assert values[-1] == 1.0, "终值不受节流影响，任务窗口不会停在 99%"
    assert all(value >= 0.10 for value in values)
    # 没有回调时是安全的 no-op（同步路径语义不变）。
    assert _progress_reporter(None)(0.5, "x") is None


def test_tier_message_is_business_chinese():
    assert _p16_tier_message("tower_colocation_host", 0) == "正在评估已有铁塔共址方案"
    assert "已选择 4 个方案" in _p16_tier_message("tower_colocation_host", 4)
    assert _p16_tier_message("unregistered_tier", 0) == "正在评估剩余候选方案"


def test_p16_plan_reports_real_stages_without_changing_the_result(tmp_path, monkeypatch):
    #: 可控小项目会在毫秒级跑完，时间节流会把中间阶段吞掉；这里只关掉**节流**
    #: （节流语义另有专门的单元测试），从而如实观察 plan 上报的完整阶段序列。
    monkeypatch.setattr(p16_service, "P16_PROGRESS_INTERVAL_SECONDS", 0.0)
    workflow = configured(
        tmp_path, redundancy=2, candidates=[candidate("S1"), candidate("S2")],
    )
    service = workflow.corridor_site_planning_service
    events = []
    outcome = service.plan(
        {}, on_progress=lambda value, message=None: events.append((value, message)),
    )
    values = [value for value, _ in events]
    messages = [message for _, message in events]
    # 阶段来自真实执行节点，不是固定 sleep。
    assert messages[0] == "正在准备 CNS 设施规划输入"
    assert "正在生成现有设施候选" in messages
    assert "正在准备走廊候选单元" in messages
    assert any(str(item).startswith("正在评估") for item in messages)
    assert any("正在进行" in str(item) for item in messages)
    assert values == sorted(values)
    assert 0.0 <= min(values) and max(values) <= 1.0
    assert values[-1] >= 0.9
    # 只读钩子绝不改变计算结果：与不传回调逐字段一致。
    plain = service.plan({})
    assert _without_timing(outcome["result"]) == _without_timing(plain["result"])


# ---- 1 / 2 / 4 / 6 / 7 / 10：async 端到端 ---------------------------------------


def test_p16_async_submit_returns_task_id_and_publishes_the_same_result(tmp_path):
    workflow = configured(tmp_path)
    # 同步路径（既有调用者的语义）先跑一次，作为逐字段对照。
    workflow.evaluate_cns_corridor_site_plan({})
    sync_result = deepcopy(workflow.state["cns_corridor_site_plan"])

    runtime = P16Runtime(tmp_path, workflow).start()
    try:
        api = runtime.api()
        started = time.perf_counter()
        response = api.post(ENDPOINT, {"async": True})
        submit_seconds = time.perf_counter() - started
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        assert task_id
        # 1. 提交立刻返回（远小于完整 P16）。
        assert submit_seconds < 60.0

        # 2. 同一个 task_id 可轮询。
        view = api.get(f"/api/tasks/{task_id}", {}, {}).data
        assert view["task_id"] == task_id
        assert view["task_name"] == "CNS 设施规划"
        assert view["advanced"]["status"] in ("queued", "running", "succeeded")
        assert view["message"]

        final = runtime.service.wait_for_terminal(task_id, timeout=600)
        assert final is not None, "任务记录丢失"
        assert final["advanced"]["status"] == "succeeded", final

        # 4. heartbeat 契约。
        assert final["heartbeat_at"]
        assert isinstance(final["heartbeat_age_seconds"], (int, float))

        # 7. 成功后给出业务结果名（前端据此自动刷新正式结果，无需 F5）。
        assert final["result_scope"] == "CNS 设施规划"
        assert final["result_summary"].get("status") in (
            "proposal_ready", "no_action_required", "evidence_required",
            "no_eligible_proposal",
        )

        # 6. 刷新页面后仍可恢复：另一个 store 实例（等价于新进程）能按 task_id 读到它。
        assert TaskStore(runtime.store.directory).find(task_id) is not None

        # 10. 后台化前后最终规划结果逐字段一致。
        async_result = deepcopy(workflow.state["cns_corridor_site_plan"])
        assert _without_timing(async_result) == _without_timing(sync_result)

        # 3. 进度必须走到收口段。
        assert float(final["progress"]) >= 0.9
    finally:
        runtime.stop()


def test_p16_async_result_matches_sync_when_async_runs_first(tmp_path):
    """反向顺序同样一致：先算后台任务，再跑同步路径，两者必须逐字段相同。"""

    workflow = configured(tmp_path)
    runtime = P16Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=600)
        assert final["advanced"]["status"] == "succeeded", final
        async_result = deepcopy(workflow.state["cns_corridor_site_plan"])
        workflow.evaluate_cns_corridor_site_plan({})
        sync_result = deepcopy(workflow.state["cns_corridor_site_plan"])
        assert _without_timing(async_result) == _without_timing(sync_result)
    finally:
        runtime.stop()


# ---- 9：同步调用者不受影响 ------------------------------------------------------


def test_sync_endpoint_stays_synchronous_and_registers_no_task(tmp_path):
    workflow = configured(tmp_path)
    runtime = P16Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {})  # 不带 async
        assert response.status == 200
        assert response.data["cns_corridor_site_plan"]["input_fingerprint"]
        assert workflow.state["result_statuses"]["cns_corridor_site_plan"] == "passed"
        assert runtime.service.list() == [], "同步调用不得登记任何后台任务"
    finally:
        runtime.stop()


def test_async_request_without_task_not_supported_by_sync_path(tmp_path):
    """未登记为 heavy 的端点带 async 仍走原同步语义（不被误当成任务）。"""

    from cns_planner.tasks.task_specs import P15_TASK_TYPE, task_type_for_endpoint

    #: Round 31-D 起 P15 也已登记；这里换一个仍然只走同步语义的端点。
    assert task_type_for_endpoint("/api/coverage-3d/evaluate") is None
    assert task_type_for_endpoint("/api/cns-corridor-gap/evaluate") == P15_TASK_TYPE
    assert task_type_for_endpoint(ENDPOINT) == P16_TASK_TYPE


# ---- 8：上游变化 → 中文业务原因，绝不覆盖正式结果 -------------------------------


def test_upstream_change_after_submit_is_reported_in_chinese(tmp_path):
    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_site_plan({})
    published_before = deepcopy(workflow.state["cns_corridor_site_plan"])

    runtime = P16Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        # 提交之后上游结论被改写：worker 必须自证 baseline 身份并作废任务。
        workflow.state["cns_corridor_gap_assessment"]["input_fingerprint"] = "changed-after-submit"
        workflow.state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = "passed"
        workflow.save()

        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=600)
        assert final["advanced"]["status"] == "stale", final
        text = (final.get("business_error") or {}).get("text") or ""
        assert text, final
        # 提交后输入已变化：worker 在算之前就 fail-fast，用户看到的是中文业务结论。
        assert text == "输入已变化，请重新运行"
        assert final["status_text"] == "输入已变化，请重新运行"
        # 绝不覆盖提交时已经存在的正式结果。
        assert workflow.state["cns_corridor_site_plan"] == published_before
    finally:
        runtime.stop()


def test_upstream_not_current_refuses_background_planning_with_chinese_reason(tmp_path):
    """上游结论不是当前结果时：后台任务如实作废，并给出"先重算上游"的中文原因。"""

    workflow = configured(tmp_path)
    workflow.evaluate_cns_corridor_site_plan({})
    published_before = deepcopy(workflow.state["cns_corridor_site_plan"])
    # 上游能力缺口结论已经不是当前结果（用户尚未重算），且这发生在**提交之前**：
    # 提交时不设门禁是刻意的（与同步入口一致），拒绝由 worker 内的门禁如实给出。
    workflow.state["cns_corridor_gap_assessment"]["status"] = "stale"
    workflow.state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = "stale"
    workflow.save()

    runtime = P16Runtime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=600)
        assert final["advanced"]["status"] == "stale", final
        text = (final.get("business_error") or {}).get("text") or ""
        # 专门的中文业务原因：告诉用户"先重算上游"，而不是笼统的"输入已变化"。
        assert "上游" in text and "重算" in text, text
        assert workflow.state["cns_corridor_site_plan"] == published_before
    finally:
        runtime.stop()
