"""Round31-E 定向验证：Radar-I → Radar-II 分级规划接入后台任务体系。

只覆盖本轮验收面，复用既有小 fixture（``test_radar_surveillance_layout._service``）
与既有算法级证据（``test_round31c_radar_ii_escalation``），不复制大型测试工程、不跑
R0005 全流程、不重跑 P16 试算。

1. async 提交立刻返回 ``202 + task_id``（真 worker 子进程 + 真持久 store）；
2. Stage A 可行 ⇒ 绝不升级；
3. Stage A 被严格证明不可行 ⇒ 才升级（既有站址 Radar-I + Radar-II）；
4. Stage A 未完成 / 未证明 ⇒ 绝不升级，且进度不假造 Stage B；
5. 计算段绝不写 canonical state（worker 侧 session 一旦落盘立即失败）；
6. 失败 / 取消都不覆盖已发布的正式结果；
7. 同步与异步业务结果一致（两侧使用**同一份** provider 装配）；
8. 完成后前端自动刷新（含 Radar 明细 hydrate）；
9. Radar overlay 仍然同时涵盖两种型号；
10. P15 / P16 的既有后台任务机制未被本轮改动破坏（由各自定向测试覆盖）。
"""

from __future__ import annotations

from copy import deepcopy
import threading
import time

import pytest

from cns_planner.algorithms.radar_layout import milp as milp_module
from cns_planner.api.router import ApiRouter
from cns_planner.application.map_figure_service import radar_overlay_panels
from cns_planner.domain.radar_surveillance_layout import RADAR_TYPE_I, RADAR_TYPE_II
from cns_planner.gis.radar_layout_adapter import radar_layout_facts_provider
from cns_planner.tasks.handlers import RESULT_SCOPE_TEXT
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import (
    RADAR_TASK_TYPE, TASK_ENDPOINTS, _WorkerReadOnlySession, _radar_scope, task_spec,
)
from cns_planner.tasks.task_store import TaskStore

from test_radar_surveillance_layout import (
    ROUTE_ID, _configure_demo_preview, _service, _shift_towers_beyond_radar_i,
)
from test_round31c_radar_ii_escalation import _incomplete_solve


ENDPOINT = "/api/radar-surveillance-layout/evaluate"
VOLATILE_KEYS = ("evaluated_at", "started_at", "seconds", "elapsed_s")


def _stable(value):
    """剔除运行时时间戳（它们不属于业务契约面）。"""

    if isinstance(value, dict):
        return {
            str(key): _stable(item) for key, item in value.items()
            if str(key) not in VOLATILE_KEYS and not str(key).endswith("_seconds")
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def factory_provider(service):
    """后台 worker 使用的**同一份** provider 装配（无 QGIS runtime）。"""

    radar = service.radar_surveillance_layout_service
    return radar_layout_facts_provider(
        paths=service.state.get("data_source_paths") or {},
        state=service.state,
        land_mask_authority=radar.land_mask_authority(),
        prefer_qgis_transform=False,
    )


class _RadarApiContext:
    def __init__(self, workflow, heavy_tasks):
        self.workflow, self.heavy_tasks, self.data = workflow, heavy_tasks, object()


class RadarRuntime:
    """真 worker 子进程 + 真持久 store 的 Radar 后台运行时。"""

    def __init__(self, tmp_path, workflow):
        self.workflow = workflow
        self.workflow.mutation_lock = threading.RLock()
        self.service = HeavyTaskService(
            self.workflow, tmp_path / "project.json", max_workers=1,
            heartbeat_interval=0.2, heartbeat_timeout=120.0, poll_interval=0.05,
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
        return ApiRouter(_RadarApiContext(self.workflow, self.service))


# ---- 1：登记契约与 async 提交 ---------------------------------------------------


def test_radar_endpoint_is_registered_as_heavy_task():
    assert TASK_ENDPOINTS[ENDPOINT] == RADAR_TASK_TYPE
    assert RESULT_SCOPE_TEXT[RADAR_TASK_TYPE] == "雷达设施优化规划"
    assert task_spec(RADAR_TASK_TYPE).task_name == "雷达设施优化规划"


def test_radar_scope_is_bound_to_operational_routes():
    state = {"operational_routes": [{"route_id": "R2"}, {"route_id": "R1"}]}
    assert _radar_scope(state, {}) == "radar_surveillance_layout:R1|R2"
    assert _radar_scope({}, {}) == "radar_surveillance_layout:no-route"


def test_radar_async_submit_returns_task_id_and_publishes(tmp_path):
    workflow, _fake = _service(tmp_path)
    #: fixture 只在内存里搭好项目事实；后台 worker 读的是项目文件，因此必须先落盘
    #: （否则"提交即 stale"——那是正确行为，不是缺陷）。
    workflow.save()
    runtime = RadarRuntime(tmp_path, workflow).start()
    try:
        api = runtime.api()
        response = api.post(ENDPOINT, {"async": True, "route_id": ROUTE_ID})
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        view = api.get(f"/api/tasks/{task_id}", {}, {}).data
        assert view["task_name"] == "雷达设施优化规划"
        final = runtime.service.wait_for_terminal(task_id, timeout=600)
        assert final is not None, "任务记录丢失"
        assert final["advanced"]["status"] == "succeeded", final
        assert final["result_scope"] == "雷达设施优化规划"
        assert final["heartbeat_at"]
        assert TaskStore(runtime.store.directory).find(task_id) is not None
        # 正式结果已由 canonical owner 发布（无数据源 ⇒ fail-closed 的 not_ready 结论，
        # 这是真实业务语义，不是执行失败）。
        item = workflow.state["radar_surveillance_layout"]["items"][0]
        assert item["status"]
        assert item["route_id"] == ROUTE_ID
    finally:
        runtime.stop()


# ---- 2 / 3 / 4：分级语义与真实阶段 ----------------------------------------------


def test_stage_a_feasible_never_escalates_and_reports_radar_i_only(tmp_path):
    workflow, provider = _service(tmp_path)
    service = workflow.radar_surveillance_layout_service
    stages = []
    outcome = service.plan(
        {"route_id": ROUTE_ID}, facts_provider=provider,
        on_progress=lambda value, message=None: stages.append((value, message)),
    )
    item = outcome["layout"]["items"][0]
    assert item["status"] == "proposal_ready"
    assert item["stage"] == "radar_i_only"
    assert item["escalation"]["escalated"] is False
    messages = [str(message) for _, message in stages]
    assert messages[0] == "正在准备雷达与航路数据"
    assert any("正在评估 Radar-I 覆盖" in message for message in messages)
    assert not any("已证明不可行" in message for message in messages), (
        "Stage A 已满足要求时绝不假造 Stage B"
    )
    values = [value for value, _ in stages]
    assert values == sorted(values) and values[-1] <= 1.0


def test_proven_infeasible_escalates_on_existing_sites_only(tmp_path):
    workflow, provider = _service(tmp_path)
    service = workflow.radar_surveillance_layout_service
    _shift_towers_beyond_radar_i(workflow)
    stages = []
    outcome = service.plan(
        {"route_id": ROUTE_ID}, facts_provider=provider,
        on_progress=lambda value, message=None: stages.append((value, message)),
    )
    item = outcome["layout"]["items"][0]
    assert item["stage"] == "radar_i_plus_radar_ii"
    assert item["escalation"]["escalated"] is True
    assert item["escalation"]["new_sites_created"] is False
    assert item["escalation"]["existing_sites_only"] is True
    messages = [str(message) for _, message in stages]
    assert any("已证明不可行" in message for message in messages)
    assert any("正在验证雷达覆盖与独立站址" in message for message in messages)


@pytest.mark.parametrize("status", ["time_limit", "node_limit", "solver_error", "infeasible"])
def test_unfinished_stage_a_never_escalates_and_never_fakes_stage_b(tmp_path, monkeypatch, status):
    workflow, provider = _service(tmp_path)
    service = workflow.radar_surveillance_layout_service
    #: 结构上仍然可行（presolve 不会先报不可行），只把 MILP 求解替换成"未完成"。
    monkeypatch.setattr(milp_module, "solve", _incomplete_solve(status))
    monkeypatch.setattr(
        milp_module, "solve_with_total_panel_count", _incomplete_solve(status),
    )
    stages = []
    outcome = service.plan(
        {"route_id": ROUTE_ID}, facts_provider=provider,
        on_progress=lambda value, message=None: stages.append((value, message)),
    )
    item = outcome["layout"]["items"][0]
    assert item["stage"] == "radar_i_only", status
    assert item["escalation"]["escalated"] is False, status
    assert item["escalation"]["stage_a_infeasibility_proven"] is False, status
    messages = [str(message) for _, message in stages]
    assert not any("已证明不可行" in message for message in messages), status
    assert any("求解未完成" in message for message in messages), status


# ---- 5：计算段绝不写 canonical state --------------------------------------------


def test_plan_never_writes_canonical_state(tmp_path):
    workflow, provider = _service(tmp_path)
    service = workflow.radar_surveillance_layout_service
    before = deepcopy(workflow.state)

    class SpySession:
        def __init__(self, state):
            self.state = state
            self.saves = 0

        def save(self):
            self.saves += 1
            raise AssertionError("纯计算段绝不落盘")

    spy = SpySession(workflow.state)
    original_session = service.session
    service.session = spy
    try:
        service.plan({"route_id": ROUTE_ID}, facts_provider=provider)
    finally:
        service.session = original_session
    assert spy.saves == 0
    assert workflow.state == before, "计算段不得改动 policy / layout / result_statuses"


def test_worker_side_session_refuses_to_save_canonical_state():
    session = _WorkerReadOnlySession({"radar_surveillance_layout": {}})
    with pytest.raises(RuntimeError):
        session.save()


def test_demo_preview_is_refused_as_a_background_task(tmp_path):
    workflow = _service(tmp_path)[0]
    spec = task_spec(RADAR_TASK_TYPE)
    with pytest.raises(ValueError):
        spec.plan_for(
            workflow.state, {"demo_preview_only": True, "route_id": ROUTE_ID},
            workflow.algorithm_registry,
        )


# ---- 6 / 7：不覆盖正式结果 + 同步 / 异步一致 ------------------------------------


def test_sync_and_async_results_match_with_the_same_provider(tmp_path):
    workflow, _fake = _service(tmp_path)
    workflow.evaluate_radar_surveillance_layout(
        {"route_id": ROUTE_ID}, facts_provider=factory_provider(workflow),
    )
    #: 与后台发布写的是**同一份 canonical 容器**（workflow.snapshot() 走的是有界摘要
    #: 投影，不能拿来逐字段对拍业务结果）。
    sync_layout = deepcopy(workflow.state["radar_surveillance_layout"])
    assert sync_layout["items"], sync_layout
    assert sync_layout["items"][0]["status"]

    runtime = RadarRuntime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True, "route_id": ROUTE_ID})
        assert response.status == 202, response.data
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=600)
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()

    async_layout = deepcopy(workflow.state["radar_surveillance_layout"])
    async_items = async_layout["items"]
    sync_items = sync_layout["items"]
    assert len(async_items) == len(sync_items) == 1
    async_item, sync_item = async_items[0], sync_items[0]
    #: 业务结论逐字段一致：输入指纹（含 provider 变换与样本分类）、分级结论、
    #: 选中面阵、独立站址与 5 m 复核证据。
    assert async_item["input_fingerprint"] == sync_item["input_fingerprint"]
    assert async_item["status"] == sync_item["status"]
    assert async_item["stage"] == sync_item["stage"]
    assert _stable(async_item["escalation"]) == _stable(sync_item["escalation"])
    assert _stable(async_item["selected_panels"]) == _stable(sync_item["selected_panels"])
    assert _stable(async_item["validation"]) == _stable(sync_item["validation"])
    assert _stable(async_item.get("coverage_profile")) == _stable(sync_item.get("coverage_profile"))
    assert workflow.state["result_statuses"]["radar_surveillance_layout"] == (
        "passed" if async_layout["items"][0]["status"] == "proposal_ready"
        else "pending_confirmation"
    )


def test_failed_task_never_overwrites_published_result(tmp_path):
    workflow, _fake = _service(tmp_path)
    workflow.evaluate_radar_surveillance_layout(
        {"route_id": ROUTE_ID}, facts_provider=factory_provider(workflow),
    )
    published_before = deepcopy(workflow.state["radar_surveillance_layout"])

    runtime = RadarRuntime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True, "route_id": ROUTE_ID})
        assert response.status == 202
        task_id = response.data["task_id"]
        # 删除不可变输入快照：worker 无法在正确输入上计算，必须失败而绝不发布。
        record = runtime.service.store.find(task_id)
        runtime.service.snapshot_path(record).unlink()
        final = runtime.service.wait_for_terminal(task_id, timeout=600)
        assert final is not None
        assert final["advanced"]["status"] in ("failed", "stale"), final
        assert workflow.state["radar_surveillance_layout"] == published_before
    finally:
        runtime.stop()


def test_changed_input_after_submit_is_refused_without_overwriting(tmp_path):
    workflow, _fake = _service(tmp_path)
    workflow.evaluate_radar_surveillance_layout(
        {"route_id": ROUTE_ID}, facts_provider=factory_provider(workflow),
    )
    published_before = deepcopy(workflow.state["radar_surveillance_layout"])

    runtime = RadarRuntime(tmp_path, workflow).start()
    try:
        response = runtime.api().post(ENDPOINT, {"async": True, "route_id": ROUTE_ID})
        assert response.status == 202
        # 提交后输入变化（塔位改动）：任务必须作废，绝不覆盖正式结果。
        workflow.state["towers"]["items"][0]["height_m"] = 99.0
        workflow.save()
        final = runtime.service.wait_for_terminal(response.data["task_id"], timeout=600)
        assert final is not None
        assert final["advanced"]["status"] == "stale", final
        text = (final.get("business_error") or {}).get("text") or ""
        assert "变化" in text or "上游" in text, text
        assert workflow.state["radar_surveillance_layout"] == published_before
    finally:
        runtime.stop()


# ---- 9：overlay 两类型号（本轮不改变绘制语义） ----------------------------------


def test_radar_overlay_still_covers_both_types():
    radar = {
        "selected_panels": [
            {"panel_id": "P1", "tower_id": "T1", "radar_type": "radar_i", "azimuth_deg": 0.0},
            {"panel_id": "P2", "tower_id": "T2", "radar_type": "radar_ii", "azimuth_deg": 90.0},
        ],
        "stage": "radar_i_plus_radar_ii",
        "radar_ii_panel_count": 1,
    }
    panels, skipped = radar_overlay_panels(radar)
    assert {panel["radar_type"] for panel in panels} == {RADAR_TYPE_I, RADAR_TYPE_II}
    assert skipped == 0


def test_read_only_task_query_is_not_blocked_by_the_publish_lock(tmp_path):
    """进度查询绝不排到 publish 的锁后面（真实项目上 canonical save 可达数十秒）。

    Round31-E 复现与最小修复：``GET /api/tasks*`` 过去经 ``_task_service()`` 调
    ``maybe_drive()``，而 ``_drive_locked`` 是在 ``HeavyTaskService._lock`` 内执行
    publish 的（publish 又持 ``workflow.mutation_lock``，并在其中读 staged artifact +
    canonical ``session.save()``）。因此"看进度"的只读请求会被发布过程整段挡住。本
    测试直接复现该持锁区段，断言只读查询**不再**等待它。
    """

    workflow, _fake = _service(tmp_path)
    workflow.save()
    runtime = RadarRuntime(tmp_path, workflow).start()
    api = runtime.api()
    started, release = threading.Event(), threading.Event()

    def hold_publish_locks():
        #: 复现 publish 的持锁区段：编排线程在 ``service._lock`` 内进入 publish，
        #: publish 再持 ``workflow.mutation_lock`` 完成 canonical 写入。
        with runtime.service._lock:  # noqa: SLF001 - 复现用
            with workflow.mutation_lock:
                started.set()
                release.wait(10)

    holder = threading.Thread(target=hold_publish_locks, daemon=True)
    holder.start()
    assert started.wait(5)
    #: ``maybe_drive`` 在 0.5 s 内是快判返回；把驱动时间戳置零以确定性地走到驱动分支
    #: （真实场景里用户轮询间隔远大于 0.5 s，因此每个轮询请求都会真正驱动一次）。
    runtime.service._last_drive = 0.0  # noqa: SLF001 - 复现用
    try:
        begin = time.perf_counter()
        api.get("/api/tasks", {}, {})
        catalog = api.get("/api/tasks/catalog", {}, {}).data
        elapsed = time.perf_counter() - begin
    finally:
        release.set()
        holder.join(5)
        runtime.stop()
    assert catalog["items"]
    assert elapsed < 0.5, f"只读任务查询被 publish 锁挡住 {elapsed:.2f}s"


def test_demo_preview_sync_path_still_works(tmp_path):
    """演示预览保持既有同步入口（后台任务明确拒绝它，见前一条测试）。"""

    workflow, provider = _service(tmp_path)
    _configure_demo_preview(workflow)
    result = workflow.evaluate_radar_surveillance_layout(
        {"demo_preview_only": True, "route_source": "current_layered_candidate"},
        facts_provider=provider,
    )
    assert result["radar_surveillance_layout"]["items"][0]["demo_preview_only"] is True
