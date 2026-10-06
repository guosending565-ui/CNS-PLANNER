"""Round32-C 定向验证：Step03 的**航路候选规划**与**航路风险画像**接入后台任务体系。

只覆盖本轮验收面，复用既有小 fixture（``test_layered_theta_star_v2.service_for`` 等）与既有
后台任务 harness 形状（``test_round31d_p15_background_task`` / ``test_round31e_radar_background_task``），
不复制大型测试工程、不跑 R0005 全流程、不重跑 P14–P17。

harness 说明（为什么 worker 在**本进程内**跑）
--------------------------------------------
本轮验收解释器是仓库约定的 ``D:\\tools\\anaconda\\python.exe``（3.13），而本机 GDAL/``osgeo``
只存在于 QGIS 自带的 Python 3.12（cp312 ABI，无法在 3.13 下 import）。真实 FABDEM 采样因此
在验收解释器里必然 import 失败，而不是产品缺陷：

* 生产路径**不需要 QGIS runtime**：``build_layered_feasibility_adapter`` 只依赖
  ``osgeo.gdal`` / ``osgeo.osr``，worker 由同一个解释器（生产即 QGIS Python）启动；
  这一点由 :func:`test_real_fabdem_adapter_assembly_is_qgis_free` 在**有 GDAL 的解释器**上证明。
* 在验收解释器上，本文件用 ``HeavyTaskService(spawn=...)`` 既有注入点把真实的 worker
  代码路径（``cns_planner.tasks.worker.main``）跑在本进程内，并只把 **GIS 采样边界**
  （``build_layered_feasibility_adapter``）换成与真实适配器同协议的替身 —— 与既有
  ``test_layered_theta_star_v2.StubAdapter`` 同一手法。Theta* 搜索、输入指纹、immutable
  snapshot、staging、compare-and-publish、current 适用性与取消全部保持真实。

锁定的事实：

1. 两个业务 endpoint 登记为 heavy task；带 ``async: true`` 返回 ``202 + task_id``；
   **不带 async 时同步语义逐字不变**；
2. worker 只调用服务的 ``plan()``（纯计算），绝不写 canonical state；唯一写入者是
   publish 阶段调用的 ``apply_computed``；
3. compare-and-publish：提交后输入变化 ⇒ ``stale``，绝不覆盖当前正式结果；
4. cooperative cancel：搜索期只读钩子按固定低频间隔检查取消，取消不写 state；
5. 同步与异步结果**逐字段一致**（两侧同一份 adapter 装配实现）；
6. 发布后的 candidate / RouteRiskProfile 都是 ``current``；
7. 搜索期进度只上报**真实**已展开 label 数，且钩子不可能改变搜索结果。
"""

from __future__ import annotations

from copy import deepcopy
import threading

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.api.server import wants_async_submission
from cns_planner.application.layered_route_planner_service import LayeredRoutePlannerService
from cns_planner.gis import layered_feasibility_adapter
from cns_planner.gis.layered_feasibility_adapter import (
    LayeredFeasibilityAdapter, _no_tower_fact, build_layered_feasibility_adapter,
)
from cns_planner.layered_route_planner import theta_star_v2
from cns_planner.tasks import worker as task_worker
from cns_planner.tasks.handlers import RESULT_SCOPE_TEXT
from cns_planner.tasks.service import HeavyTaskService
from cns_planner.tasks.task_specs import (
    LAYERED_CANDIDATE_TASK_TYPE, ROUTE_RISK_PROFILE_TASK_TYPE, TASK_ENDPOINTS,
    _WorkerReadOnlySession, _layered_candidate_scope, _route_risk_profile_scope, task_spec,
)

from test_layered_theta_star_v2 import grid_cells, service_for  # noqa: E402


ENDPOINT = "/api/layered-route-candidates/evaluate-real"
RRP_ENDPOINT = "/api/route-risk-profiles/evaluate"
SAMPLE_ELEVATION_M = 10.0
#: 取消 / 输入变化需要 worker 还在跑：比默认 fixture 更大的网格给"提交 → 取消/改输入"
#: 留出真实时间窗口。
SLOW_COLUMNS, SLOW_ROWS = 9, 9


# ---- GIS 采样边界替身（只替换栅格采样，adapter 语义与生产同一份实现） --------------


class _StubTerrainSource:
    """``FabdemWindowTerrainSource`` 的同协议替身：不打开栅格，只给确定性高程。"""

    role = "terrain_dtm"
    vertical_reference = "egm2008_orthometric"
    vertical_status = "confirmed"

    def __init__(self, path):
        self.path = str(path)

    def usable(self):
        return True, None

    def describe(self):
        return {
            "dataset": "test-synthetic-fabdem", "path": self.path,
            "vertical_reference": self.vertical_reference,
            "vertical_status": self.vertical_status,
            "read_mode": "single_read_only_window_per_run_read_as_array_no_resample",
        }

    def sample_cells(self, cells, *, transform):
        return {
            str(cell["fine_cell_id"]): {
                "data_status": "passed",
                "surface_elevation_max_egm2008_m": SAMPLE_ELEVATION_M,
                "valid_pixel_count": 4, "nodata_pixel_count": 0,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            }
            for cell in cells
        }


def _stub_assembly(source_paths):
    """与 ``build_layered_feasibility_adapter`` 同契约（缺 terrain_dtm 时同样 fail-closed）。"""

    paths = source_paths if isinstance(source_paths, dict) else {}
    terrain_dtm = paths.get("terrain_dtm")
    if not terrain_dtm:
        raise ValueError("请先配置 verified FABDEM terrain_dtm")
    return LayeredFeasibilityAdapter(_StubTerrainSource(terrain_dtm))


def _write_fabdem(path, *, elevation=SAMPLE_ELEVATION_M, size=120):
    """真实可读的合成 FABDEM GeoTIFF（EPSG:4326 + EGM2008 正高声明）。"""

    gdal = pytest.importorskip("osgeo.gdal")
    osr = pytest.importorskip("osgeo.osr")
    west, south, east, north = 121.98, 29.98, 122.09, 30.07
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform([
        west, (east - west) / size, 0.0, north, 0.0, -(north - south) / size,
    ])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.SetMetadata({"vertical_reference": "egm2008_orthometric"})
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.Fill(float(elevation))
    band.FlushCache()
    dataset.FlushCache()
    del dataset
    return str(path)


# ---- 项目 fixture ---------------------------------------------------------------


def _project(tmp_path, *, columns=3, rows=3):
    """一个**可真实求解**的最小 Step03 项目（小网格 + 建筑事实 + 数据源路径）。"""

    service, grid = service_for(tmp_path, grid=grid_cells(columns=columns, rows=rows))
    state = service.state
    state["data_source_paths"] = {"terrain_dtm": str(tmp_path / "fabdem.tif")}
    #: 建筑事实：0 栋且 status passed —— coarse mask 只承认 passed 的既定事实，
    #: missing ≠ 0，因此这里显式给出"已确认没有建筑"。
    state["grid_attributes"]["buildings"] = {
        "status": "passed", "source": "test", "count": len(grid),
        "cells": {
            cell["grid_id"]: {
                "status": "passed", "building_count": 0,
                "height_max_m": None, "valid_height_fraction": None,
            }
            for cell in grid
        },
    }
    service.save()
    return service, grid


def _adapter(service):
    """与 worker **同一处**装配入口（monkeypatch 后两侧自动一致，与生产同形）。"""

    return layered_feasibility_adapter.build_layered_feasibility_adapter(
        service.state.get("data_source_paths") or {}
    )


def _candidates(service):
    return service.state["layered_route_candidates"]


class _ApiContext:
    def __init__(self, workflow, heavy_tasks):
        self.workflow, self.heavy_tasks, self.data = workflow, heavy_tasks, object()


class _InlineWorkerProcess:
    """进程外壳：在本进程内跑**真实**的 worker 代码路径（``tasks.worker.main``）。"""

    def __init__(self, argv):
        self._code = None
        #: argv = [python, "-m", "cns_planner.tasks.worker", <worker 参数...>]
        self._thread = threading.Thread(target=self._run, args=(list(argv)[3:],), daemon=True)
        self._thread.start()

    def _run(self, argv):
        try:
            self._code = int(task_worker.main(argv) or 0)
        except SystemExit as exc:  # pragma: no cover - worker 正常路径不会走到
            self._code = int(getattr(exc, "code", 0) or 0)
        except BaseException:  # noqa: BLE001 - worker 崩溃由 store 的 failed 终态体现
            self._code = 1

    def poll(self):
        return self._code

    def terminate(self):
        return None

    def kill(self):
        return None

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return self._code


class Step03Runtime:
    """真 worker 代码路径 + 真持久 store（与 Round31-D/E 同一 harness 形状）。"""

    def __init__(self, tmp_path, workflow, *, spawn=None):
        self.workflow = workflow
        self.workflow.mutation_lock = threading.RLock()
        self.service = HeavyTaskService(
            self.workflow, tmp_path / "project.json", max_workers=1,
            heartbeat_interval=0.2, heartbeat_timeout=120.0, poll_interval=0.05,
            spawn=spawn,
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
        return ApiRouter(_ApiContext(self.workflow, self.service))

    def wait(self, task_id, timeout=600):
        return self.service.wait_for_terminal(task_id, timeout=timeout)


def _runtime(tmp_path, workflow, monkeypatch, *, columns=3, rows=3):
    """把 GIS 采样边界换成同协议替身，其余（搜索 / 指纹 / 发布）全部真实。"""

    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    return Step03Runtime(tmp_path, workflow, spawn=_InlineWorkerProcess).start()


# ---- 1：登记契约（业务名 / endpoint / scope / 同步兼容） --------------------------


def test_step03_endpoints_are_registered_as_heavy_tasks():
    assert TASK_ENDPOINTS[ENDPOINT] == LAYERED_CANDIDATE_TASK_TYPE
    assert TASK_ENDPOINTS[RRP_ENDPOINT] == ROUTE_RISK_PROFILE_TASK_TYPE
    assert task_spec(LAYERED_CANDIDATE_TASK_TYPE).task_name == "航路候选规划"
    assert task_spec(ROUTE_RISK_PROFILE_TASK_TYPE).task_name == "航路风险画像"
    assert RESULT_SCOPE_TEXT[LAYERED_CANDIDATE_TASK_TYPE] == "航路候选规划"
    assert RESULT_SCOPE_TEXT[ROUTE_RISK_PROFILE_TASK_TYPE] == "航路风险画像"


def test_ordinary_task_text_has_no_algorithm_name_or_fingerprint():
    """普通视图只出现业务名：任务名 / 提交文案里没有算法名、fingerprint 或原始 status。"""

    for task_type, name in (
        (LAYERED_CANDIDATE_TASK_TYPE, "航路候选规划"),
        (ROUTE_RISK_PROFILE_TASK_TYPE, "航路风险画像"),
    ):
        spec = task_spec(task_type)
        text = "%s %s" % (spec.task_name, spec.message)
        assert spec.task_name == name
        for forbidden in ("Theta", "fingerprint", "layeredcandv1", "passed", "stale"):
            assert forbidden not in text, text


def test_async_is_opt_in_and_the_sync_path_stays_identical():
    """登记了 heavy task 的 endpoint **只有**显式 ``async: true`` 才异步。"""

    assert wants_async_submission(ENDPOINT, {}) is False
    assert wants_async_submission(RRP_ENDPOINT, {}) is False
    assert wants_async_submission(ENDPOINT, {"async": True}) is True
    assert wants_async_submission(RRP_ENDPOINT, {"async": True}) is True
    # Round32-C：canonical state **没有** data_source_paths 的写入者（实测为 None），因此
    # 候选任务必须由提交侧把主进程已解析的真实来源冻结进 payload → 快照 inputs，否则 worker
    # 装配不出 FABDEM 适配器（后台任务必然 task_execution_failed，而同步路径读 live paths 无感）。
    from cns_planner.api.router import ApiRouter
    from cns_planner.tasks.task_specs import _layered_candidate_inputs

    assert LAYERED_CANDIDATE_TASK_TYPE in ApiRouter.RUNTIME_SOURCE_TASK_TYPES
    assert _layered_candidate_inputs({"data_source_paths": None}, {}).get("data_source_paths") in (None, {})
    frozen = _layered_candidate_inputs(
        {"data_source_paths": None}, {"data_source_paths": {"terrain_dtm": "D:/dem.tif"}},
    )
    assert frozen["data_source_paths"] == {"terrain_dtm": "D:/dem.tif"}

    class _Data:
        paths = {"terrain_dtm": "D:/dem.tif", "basemap": "D:/b.qgz"}

    class _Context:
        data = _Data()

    class _Router(ApiRouter):
        def __init__(self):  # noqa: D107 - 只要 context，不构造完整路由
            self.context = _Context()

    router = _Router()
    merged = router._freeze_runtime_sources(LAYERED_CANDIDATE_TASK_TYPE, {})
    assert merged["data_source_paths"]["terrain_dtm"] == "D:/dem.tif"
    assert router._freeze_runtime_sources("route_risk_profile_evaluate", {}) == {}, \
        "只有候选任务需要冻结运行期空间来源"


def test_scopes_are_bound_to_the_lane_and_the_active_candidate():
    payload = {"request": {"scenario_route_id": "R0005", "altitude_layer_id": "ALT-100"}}
    assert _layered_candidate_scope({}, payload) == "layered_route_candidate:R0005@ALT-100"
    state = {"layered_route_planning_request": {
        "scenario_route_id": "R1", "altitude_layer_id": "ALT-060"}}
    assert _layered_candidate_scope(state, {}) == "layered_route_candidate:R1@ALT-060"
    assert _layered_candidate_scope({}, {}) == "layered_route_candidate:no-route@no-layer"
    assert _route_risk_profile_scope(
        {"layered_route_candidates": {"active_candidate_id": "LRC-1"}}, {},
    ) == "route_risk_profile:LRC-1"
    assert _route_risk_profile_scope({}, {"candidate_id": "LRC-2"}) == "route_risk_profile:LRC-2"
    assert _route_risk_profile_scope({}, {}) == "route_risk_profile:no-candidate"


# ---- 2：计算段绝不写 canonical state --------------------------------------------


def test_candidate_plan_is_a_pure_computation(tmp_path, monkeypatch):
    service, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    before = deepcopy(service.state)

    class SpySession:
        def __init__(self, state):
            self.state, self.saves = state, 0

        def save(self):
            self.saves += 1
            raise AssertionError("纯计算段绝不落盘")

    planner_service = service.layered_route_planner_service
    spy = SpySession(planner_service.session.state)
    original = planner_service.session
    planner_service.session = spy
    try:
        outcome = planner_service.plan({}, adapter=_adapter(service))
    finally:
        planner_service.session = original
    assert spy.saves == 0
    assert outcome["outcome"] == "candidate", outcome
    assert outcome["key"] == "R0001@L8-LOW"
    assert service.state == before, "计算段不得改动任何 canonical 容器"
    # worker 侧会话同样绝不落盘（纯计算 + 只读会话是同一个不变量）。
    with pytest.raises(RuntimeError):
        _WorkerReadOnlySession({"layered_route_candidates": {}}).save()


# ---- 3：async 提交 / 发布 / current #############################################


def test_candidate_async_submit_returns_202_and_publishes_a_current_candidate(
    tmp_path, monkeypatch,
):
    workflow, _grid = _project(tmp_path)
    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        view = runtime.api().get("/api/tasks/%s" % task_id, {}, {}).data
        assert view["task_name"] == "航路候选规划"
        final = runtime.wait(task_id)
        assert final is not None, "任务记录丢失"
        assert final["advanced"]["status"] == "succeeded", final
        assert final["result_scope"] == "航路候选规划"
        assert final["heartbeat_at"]
    finally:
        runtime.stop()
    collection = workflow.layered_route_planner_service.result_snapshot()
    items = collection["items"]
    assert items, collection
    assert items[-1]["status"] == "candidate"
    assert items[-1]["current_applicability"] == "current"
    assert workflow.state["result_statuses"]["layered_route_candidate"] == "passed"


def test_candidate_sync_and_async_results_match(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    workflow.evaluate_layered_route_candidate({}, adapter=_adapter(workflow))
    sync_item = deepcopy(_candidates(workflow)["items"][-1])
    assert sync_item["status"] == "candidate", sync_item

    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()
    items = _candidates(workflow)["items"]
    async_item = items[-1]
    assert len(items) == 1, "同一输入指纹必须就地替换，而不是新增记录"
    for field in ("candidate_fingerprint", "input_fingerprint", "feasibility_mask_fingerprint",
                  "status", "distance_m", "grid_path", "route_id", "altitude_layer_id"):
        assert async_item[field] == sync_item[field], field


def test_candidate_changed_input_after_submit_is_not_published(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path, columns=SLOW_COLUMNS, rows=SLOW_ROWS)
    published_before = deepcopy(_candidates(workflow))
    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        #: 提交后输入变化（planning request 的工程依据变了 ⇒ 输入指纹变化）：
        #: 任务必须作废，绝不覆盖当前正式结果。
        workflow.state["layered_route_planning_request"]["source"] = "工程确认-改动后"
        workflow.save()
        final = runtime.wait(response.data["task_id"])
        assert final is not None
        assert final["advanced"]["status"] == "stale", final
        text = (final.get("business_error") or {}).get("text") or ""
        assert "变化" in text, text
    finally:
        runtime.stop()
    assert _candidates(workflow) == published_before


def test_cancel_request_never_publishes_a_candidate(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path, columns=SLOW_COLUMNS, rows=SLOW_ROWS)
    published_before = deepcopy(_candidates(workflow))
    #: 搜索期钩子降到逐次展开：取消有机会在**搜索中**被真实检查到（生产间隔是 2048）。
    monkeypatch.setattr(theta_star_v2, "SEARCH_HOOK_EXPANSION_INTERVAL", 1)
    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        api = runtime.api()
        response = api.post(ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        task_id = response.data["task_id"]
        cancel = api.post("/api/tasks/cancel", {"task_id": task_id})
        assert cancel.status in (200, 202), cancel.data
        final = runtime.wait(task_id)
        assert final is not None
        assert final["advanced"]["status"] in ("cancelled", "stale", "failed"), final
        assert final["advanced"]["status"] != "succeeded"
    finally:
        runtime.stop()
    assert _candidates(workflow) == published_before, "取消绝不写 canonical state"


# ---- 4：RouteRiskProfile 的同一套契约 -------------------------------------------


def test_risk_profile_async_submit_returns_202_and_publishes_current(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    workflow.evaluate_layered_route_candidate({}, adapter=_adapter(workflow))
    current = workflow.layered_route_planner_service.result_snapshot()["items"][-1]
    assert current["current_applicability"] == "current"

    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(RRP_ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        view = runtime.api().get("/api/tasks/%s" % response.data["task_id"], {}, {}).data
        assert view["task_name"] == "航路风险画像"
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] == "succeeded", final
        assert final["result_scope"] == "航路风险画像"
    finally:
        runtime.stop()
    profiles = workflow.route_risk_profiles()
    current_profiles = [
        item for item in profiles["items"] if item["current_applicability"] == "current"
    ]
    assert current_profiles, profiles["items"]
    assert current_profiles[-1]["status"] == "passed"
    readiness = workflow.route_risk_profile_readiness()
    assert readiness["status"] == "ready", readiness
    assert readiness["candidate"]["current_applicability"] == "current"


def test_risk_profile_sync_and_async_results_match(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    workflow.evaluate_layered_route_candidate({}, adapter=_adapter(workflow))
    workflow.evaluate_route_risk_profile({})
    sync_profile = deepcopy([
        item for item in workflow.route_risk_profiles()["items"]
        if item["current_applicability"] == "current"
    ][-1])

    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(RRP_ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        final = runtime.wait(response.data["task_id"])
        assert final["advanced"]["status"] == "succeeded", final
    finally:
        runtime.stop()
    profiles = workflow.route_risk_profiles()["items"]
    assert len(profiles) == 1, "同一 profile 指纹必须就地替换，而不是新增记录"
    async_profile = profiles[-1]
    assert async_profile["profile_id"] == sync_profile["profile_id"]
    assert async_profile["status"] == sync_profile["status"] == "passed"
    assert async_profile["fingerprints"] == sync_profile["fingerprints"]
    assert async_profile["segments"] == sync_profile["segments"]


def test_risk_profile_candidate_changed_is_not_published(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    workflow.evaluate_layered_route_candidate({}, adapter=_adapter(workflow))
    published_before = deepcopy(workflow.state["route_risk_profiles"])

    runtime = _runtime(tmp_path, workflow, monkeypatch)
    try:
        response = runtime.api().post(RRP_ENDPOINT, {"async": True})
        assert response.status == 202, response.data
        #: 提交后 current candidate 变化（候选重算 ⇒ lane 的候选记录被替换）：
        #: 画像任务必须作废，绝不把画像挂到已经变化的候选上。
        workflow.evaluate_layered_route_candidate({}, adapter=_adapter(workflow))
        workflow.state["layered_route_planning_request"]["source"] = "工程确认-改动后"
        workflow.save()
        final = runtime.wait(response.data["task_id"])
        assert final is not None
        assert final["advanced"]["status"] == "stale", final
    finally:
        runtime.stop()
    assert workflow.state["route_risk_profiles"] == published_before


# ---- 5：真实进度 + cooperative cancel（搜索期只读钩子） ---------------------------


def test_search_hook_reports_real_expanded_counts_without_changing_the_result(
    tmp_path, monkeypatch,
):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    planner_service = workflow.layered_route_planner_service
    baseline = planner_service.plan({}, adapter=_adapter(workflow))
    assert baseline["outcome"] == "candidate", baseline

    #: 生产间隔是固定常量 2048；小网格上永远触发不到，因此测试里把间隔降到 1。
    assert theta_star_v2.SEARCH_HOOK_EXPANSION_INTERVAL == 2048
    monkeypatch.setattr(theta_star_v2, "SEARCH_HOOK_EXPANSION_INTERVAL", 1)
    reported = []
    hooked = planner_service.plan(
        {}, adapter=_adapter(workflow), on_progress=lambda expanded: reported.append(expanded),
    )
    assert reported, "搜索期必须上报真实的已展开 label 数"
    assert reported == sorted(reported) and len(set(reported)) == len(reported)
    assert all(isinstance(value, int) and value > 0 for value in reported)
    #: 钩子绝不改变任何决策：同一 state + 同一 adapter ⇒ 同一份候选指纹与航路。
    for field in ("candidate_fingerprint", "input_fingerprint", "grid_path", "distance_m",
                  "status"):
        assert hooked["candidate"][field] == baseline["candidate"][field], field
    assert LayeredRoutePlannerService.SEARCH_STAGE_PROGRESS == 0.35


def test_cancel_check_aborts_the_search_without_writing_state(tmp_path, monkeypatch):
    workflow, _grid = _project(tmp_path)
    monkeypatch.setattr(
        layered_feasibility_adapter, "build_layered_feasibility_adapter", _stub_assembly,
    )
    planner_service = workflow.layered_route_planner_service
    before = deepcopy(workflow.state)
    monkeypatch.setattr(theta_star_v2, "SEARCH_HOOK_EXPANSION_INTERVAL", 1)

    class _Cancelled(Exception):
        pass

    calls = {"count": 0}

    def cancel_check():
        calls["count"] += 1
        if calls["count"] > 2:
            raise _Cancelled("任务已按请求取消")

    with pytest.raises(_Cancelled):
        planner_service.plan({}, adapter=_adapter(workflow), cancel_check=cancel_check)
    assert calls["count"] >= 3, "取消必须在搜索期被真实检查到"
    assert workflow.state == before, "取消绝不写 canonical state"


def test_worker_runners_declare_business_stage_messages():
    """runner 的阶段文案：恒定阶段值 + 真实展开数；不含伪造百分比。"""

    import inspect

    from cns_planner.tasks import task_specs

    source = inspect.getsource(task_specs._layered_candidate_runner)
    assert "SEARCH_STAGE_PROGRESS" in source
    assert "正在执行 Theta* 搜索 · 已展开 %d 个状态" in source
    assert "正在准备航路候选规划输入" in source
    rrp_source = inspect.getsource(task_specs._route_risk_profile_runner)
    assert "正在准备航路风险画像输入" in rrp_source
    assert "正在计算航路风险画像" in rrp_source
    assert task_spec(LAYERED_CANDIDATE_TASK_TYPE).release == "layered_route_candidates"
    assert task_spec(ROUTE_RISK_PROFILE_TASK_TYPE).release == "route_risk_profiles"


# ---- 6：真实栅格装配（只在带 GDAL 的解释器上可跑） --------------------------------


def test_real_fabdem_adapter_assembly_is_qgis_free_and_produces_passed_cells(tmp_path):
    """生产装配不需要 QGIS runtime：只依赖 ``osgeo.gdal`` / ``osgeo.osr``。

    本机 GDAL 只在 QGIS 自带的 Python（3.12）里，验收解释器（anaconda 3.13）没有它 ——
    因此这条断言在验收解释器上 skip；在带 GDAL 的解释器上它证明 worker 能真实装配。
    """

    workflow, grid = _project(tmp_path)
    raster = _write_fabdem(tmp_path / "fabdem.tif")
    workflow.state["data_source_paths"] = {"terrain_dtm": raster}
    adapter = build_layered_feasibility_adapter({"terrain_dtm": raster})
    assert adapter.source_status()["terrain"]["available"] is True
    cells = adapter.build_cells(list(workflow.state["grid"]["cells"]), workflow.state)
    assert len(cells) == len(grid)
    assert all(cell["terrain"]["data_status"] == "passed" for cell in cells)
    assert all(cell["buildings"]["data_status"] == "passed" for cell in cells)
    assert all(cell["towers"]["data_status"] == _no_tower_fact()["data_status"] for cell in cells)
    assert isinstance(adapter.describe(), dict)
