"""BUG-POP-001：population-only remap 的回归测试。

背景
----
后端早已具备 ``population_nodata.py`` 契约、``GET/POST /api/population-nodata-policy``、
fail-closed 与 invalidation，但 Step02 没有正常用户闭环，也**没有**"只重算人口映射"的路径：
用户改完 NoData 语义后只能整块重跑工作区映射。

本文件锁定本轮新增的闭环：

1. ``MapData.population_grid_attribute(grid)`` 是**唯一**的人口映射入口，
   ``grid_attributes()`` 必须复用它（绝不出现第二份人口算法）；
2. ``POST /api/workspace/grid/population/remap`` 在 QGIS 线程上读取**当前** grid、
   **当前**人口源与**当前** policy，只执行 ``PopulationGridService.map``；
3. application 层 targeted apply 只替换 ``grid_attributes.population``：
   不重新生成 grid、不重算 terrain / buildings / airspace、不动 nodes / scenario_routes；
4. 人口变化继续按**既有** invalidation 使依赖的 risk / layered candidate 变 stale，
   ``_population_shelter_cache`` 被丢弃并由既有逻辑重建；
5. 一次用户动作 = **一次**提交（deferred_save 折叠，磁盘上不存在中间态）。

禁止事项（本文件不触碰）：Theta* V2、风险数学、Validation/Adoption、NoData 既有语义。
"""

from __future__ import annotations

import importlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

from cns_planner.application.workflow_service import WorkflowService

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"
POPULATION_SOURCE = "fixture/population.tif"

CONFIRMATION = {
    "mode": "nodata_is_zero_population",
    "role": "population",
    "source": "user_confirmation",
    "source_id": "worldpop-r2025a-population-count",
    "evidence": {"product": "WorldPop Population Counts R2025A"},
    "confirmed": True,
}


# --------------------------------------------------------------------------- QGIS stub


class _Dummy:
    """QGIS import stub：只让 import 链通过，不提供任何真实能力。"""

    WriteOnly = 1

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Dummy

    def __call__(self, *args, **kwargs):
        return _Dummy


def _install_qgis_stubs(monkeypatch):
    qgis = ModuleType("qgis")
    qgis.__path__ = []
    core = ModuleType("qgis.core")
    for name in (
        "QgsApplication", "QgsProject", "QgsCoordinateReferenceSystem",
        "QgsCoordinateTransform", "QgsRectangle", "QgsMapSettings",
        "QgsMapRendererParallelJob", "QgsRasterLayer", "QgsRasterShader",
        "QgsColorRampShader", "QgsSingleBandPseudoColorRenderer",
        "QgsVectorLayer", "QgsRasterBandStats", "QgsSpatialIndex",
        "QgsFeature", "QgsGeometry", "QgsPointXY", "QgsField", "QgsFields",
        "QgsWkbTypes", "QgsVectorFileWriter", "QgsFeatureRequest",
        "QgsCoordinateTransformContext", "QgsUnitTypes", "QgsDistanceArea",
        "QgsRasterBlock", "QgsRasterInterface", "QgsProjectDirtyBlocker",
    ):
        setattr(core, name, _Dummy)
    pyqt = ModuleType("qgis.PyQt")
    pyqt.__path__ = []
    qtcore = ModuleType("qgis.PyQt.QtCore")
    for name in ("QSize", "QBuffer", "QIODevice", "Qt", "QVariant", "QCoreApplication"):
        setattr(qtcore, name, _Dummy)
    qtgui = ModuleType("qgis.PyQt.QtGui")
    qtgui.QColor = _Dummy
    qgis.core, qgis.PyQt = core, pyqt
    pyqt.QtCore, pyqt.QtGui = qtcore, qtgui

    osgeo = ModuleType("osgeo")
    osgeo.__path__ = []
    gdal = ModuleType("osgeo.gdal")
    gdal.UseExceptions = lambda: None
    gdal.Open = lambda *args, **kwargs: None
    osgeo.gdal, osgeo.ogr, osgeo.osr = gdal, ModuleType("osgeo.ogr"), ModuleType("osgeo.osr")

    for name, module in {
        "qgis": qgis, "qgis.core": core, "qgis.PyQt": pyqt,
        "qgis.PyQt.QtCore": qtcore, "qgis.PyQt.QtGui": qtgui,
        "osgeo": osgeo, "osgeo.gdal": gdal, "osgeo.ogr": osgeo.ogr, "osgeo.osr": osgeo.osr,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def map_data_module(monkeypatch):
    _install_qgis_stubs(monkeypatch)
    return importlib.import_module("cns_planner.gis.map_data")


class _StubGridService:
    """只回报固定结果的网格映射替身（不读盘）。"""

    def __init__(self, kind):
        self.kind = kind

    def map(self, grid, *args, **kwargs):
        return {"status": "passed", "kind": self.kind, "grid_level": grid.get("level"), "cells": {}}


def _bare_map_data(module, policy, monkeypatch):
    """不做 ``__init__``（那会打开真实数据集）的最小 ``MapData``。"""

    data = object.__new__(module.MapData)
    data.paths = {"population": POPULATION_SOURCE, "building_grid": None, "buildings": None,
                  "terrain": None, "basemap": None}
    data.project, data.local_layers = None, []
    data._vector_role_cache = {}
    data.workflow_provider = lambda: {
        "population_nodata_policy": policy, "airspace_policies": {},
    }
    monkeypatch.setattr(module, "resolve_vector_layer_source",
                        lambda *args, **kwargs: {"ok": False, "status": "not_configured"})
    monkeypatch.setattr(module, "BuildingGridService", lambda: _StubGridService("buildings"))
    monkeypatch.setattr(module, "TerrainGridService", lambda: _StubGridService("terrain"))
    monkeypatch.setattr(module, "AirspaceGridService", lambda: _StubGridService("airspace"))
    monkeypatch.setattr(module, "aggregate_footprint_facts", lambda *args, **kwargs: {})
    return data


def _grid(service=None):
    return {"level": 6, "cells": [{"grid_id": "A", "bbox": [120, 30, 120.01, 30.01]},
                                  {"grid_id": "B", "bbox": [120.01, 30, 120.02, 30.01]}]}


# ------------------------------------------------------------------ 1) 唯一人口映射入口


def test_population_grid_attribute_reads_current_source_and_policy(map_data_module, monkeypatch):
    calls = []

    class Recorder:
        def map(self, grid, source_path, nodata_semantics=None):
            calls.append((grid, source_path, nodata_semantics))
            return {"status": "passed", "grid_level": grid.get("level"), "cells": {}}

    monkeypatch.setattr(map_data_module, "PopulationGridService", Recorder)
    policy = CONFIRMATION
    data = _bare_map_data(map_data_module, policy, monkeypatch)
    grid = _grid()

    result = data.population_grid_attribute(grid)

    assert result["status"] == "passed"
    assert len(calls) == 1
    assert calls[0][0] is grid
    assert calls[0][1] == POPULATION_SOURCE, "必须读当前人口源路径"
    assert calls[0][2] == policy, "必须读当前 workflow 快照里的 NoData 语义确认"


def test_grid_attributes_reuses_the_single_population_entry_point(map_data_module, monkeypatch):
    seen = []

    def fake_population(self, grid, nodata_semantics=None):
        seen.append((grid, nodata_semantics))
        return {"status": "passed", "marker": "population_entry_point", "cells": {}}

    monkeypatch.setattr(map_data_module.MapData, "population_grid_attribute", fake_population)
    policy = CONFIRMATION
    data = _bare_map_data(map_data_module, policy, monkeypatch)
    grid = _grid()

    result = data.grid_attributes(grid)

    assert len(seen) == 1, "grid_attributes 必须通过 population_grid_attribute 取人口映射"
    assert seen[0][0] is grid
    assert seen[0][1] == policy, "同一份当前 policy 必须原样传入，且不重复读取快照"
    assert result["population"]["marker"] == "population_entry_point"
    assert set(result) == {"population", "terrain", "buildings", "airspace"}


def test_grid_attributes_holds_no_second_population_algorithm():
    source = (ROOT / "cns_planner" / "gis" / "map_data.py").read_text(encoding="utf-8")
    body = source[
        source.index("def grid_attributes(self, grid):"):
        source.index("    @staticmethod\n    def _vector_role_record")
    ]
    assert "self.population_grid_attribute(" in body
    assert "PopulationGridService()" not in body, "不得在 grid_attributes 里复制人口算法"
    # remap 端点与全量映射共用同一入口。
    assert source.count("PopulationGridService().map(") == 1


# ------------------------------------------------------- 2) application 层 targeted apply


def _health():
    return {
        "status": "passed", "population": {"status": "passed"},
        "airspace": {"status": "passed"}, "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"}, "property": {"status": "missing_data"},
        "loaded_layer_count": 3, "covered_layer_count": 3,
    }


def _service(tmp_path, workspace=(120.001, 30.001, 120.02, 30.02)):
    defaults = tmp_path / "config" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(DEFAULTS.read_text(encoding="utf-8"), encoding="utf-8")
    store = tmp_path / "project_state.json"
    service = WorkflowService(store, defaults)
    service.set_workspace(list(workspace), _health(), 6)
    return service, store


def _population_result(grid, *, status="passed"):
    cells = {
        cell["grid_id"]: {
            "status": status, "value_status": status, "coverage_status": "full",
            "population_count_people": 12.0, "population_density_people_km2": 40.0,
        }
        for cell in grid["cells"]
    }
    return {
        "status": status, "value_status": status, "coverage_status": "full",
        "grid_level": grid["level"], "count": len(cells), "cells": cells,
        "source_profile": {"quantity": "population_count_per_source_pixel"},
        "population_mapping": {
            "status": status, "total_cells": len(cells), "covered_cells": len(cells),
            "unresolved_cells": 0, "full_cells": len(cells), "partial_cells": 0,
            "missing_cells": 0, "outside_cells": 0, "nodata_only_cells": 0,
            "confirmed_zero_cells": 0, "coverage_ratio": 1.0,
        },
    }


def _seed_state(service):
    """把除 population 之外的一切都放上可检测的哨兵。"""

    state = service.state
    state["nodes"] = [{"node_id": "N-KEEP"}]
    state["scenario_routes"] = [{"route_id": "R-KEEP"}]
    state["grid_attributes"].setdefault("terrain", {})["keep_marker"] = "terrain"
    state["grid_attributes"].setdefault("buildings", {})["keep_marker"] = "buildings"
    state["grid_attributes"].setdefault("airspace", {})["keep_marker"] = "airspace"
    state["grid_attributes"]["population"] = {"status": "stale", "grid_level": None, "cells": {}}
    state["grid_risk"] = {"status": "passed", "cells": {"A": {"score": 1}}}
    state["result_statuses"]["environment_risk"] = "passed"
    state["_population_shelter_cache"] = {
        "cells": {"A": {"shelter_coefficient": 1}}, "derived_from_fingerprint": "stale-marker",
    }
    service.session.save()
    return {
        "grid": state["grid"],
        "nodes": state["nodes"],
        "scenario_routes": state["scenario_routes"],
        "terrain": state["grid_attributes"]["terrain"],
        "buildings": state["grid_attributes"]["buildings"],
        "airspace": state["grid_attributes"]["airspace"],
        "grid_risk": state["grid_risk"],
        "attribute_keys": set(state["grid_attributes"]),
    }


def test_population_only_remap_replaces_population_and_nothing_else(tmp_path):
    service, _store = _service(tmp_path)
    before = _seed_state(service)
    grid = service.state["grid"]

    service.apply_population_grid_attribute(_population_result(grid))

    state = service.state
    assert state["grid"] is before["grid"], "不得重新生成标准网格"
    assert state["nodes"] is before["nodes"] and state["nodes"] == [{"node_id": "N-KEEP"}]
    assert state["scenario_routes"] is before["scenario_routes"]
    assert state["grid_attributes"]["terrain"] is before["terrain"]
    assert state["grid_attributes"]["buildings"] is before["buildings"]
    assert state["grid_attributes"]["airspace"] is before["airspace"]
    attributes = state["grid_attributes"]
    grid_ids = {cell["grid_id"] for cell in grid["cells"]}
    assert attributes["population"]["status"] == "passed"
    first_id = grid["cells"][0]["grid_id"]
    assert attributes["population"]["cells"][first_id]["population_count_people"] == 12.0
    assert set(attributes["population"]["cells"]) == grid_ids
    assert set(attributes) == before["attribute_keys"], "命名空间集合不得被增删"
    # 风险结果既没有被重算，也没有被删除：只按既有 invalidation 标 stale。
    assert state["grid_risk"] is before["grid_risk"]
    assert state["grid_risk"]["status"] == "stale"
    assert state["result_statuses"]["environment_risk"] == "stale"
    assert state["grid_risk"]["cells"] == {"A": {"score": 1}}
    # population_shelter 派生缓存必须失效：它由既有逻辑按**新**人口属性重建（指纹随之改变），
    # 绝不允许继续携带用旧人口算出的系数。
    assert state.get("_population_shelter_cache", {}).get("derived_from_fingerprint") != "stale-marker"
    assert state["data_source_profiles"]["population"]["quantity"] == "population_count_per_source_pixel"


def test_population_only_remap_is_one_single_commit(tmp_path):
    service, store = _service(tmp_path)
    _seed_state(service)
    grid = service.state["grid"]
    baseline = service.state["revision"]
    assert json.loads(store.read_text(encoding="utf-8"))["revision"] == baseline

    with service.deferred_save():
        service.apply_population_grid_attribute(_population_result(grid))
        assert service.session.pending_save is True
        assert json.loads(store.read_text(encoding="utf-8"))["revision"] == baseline, (
            "deferred 期间磁盘上不得出现中间态"
        )

    document = json.loads(store.read_text(encoding="utf-8"))
    assert document["revision"] == baseline + 1, "一次用户动作只能产生一次提交"
    assert document["grid_attributes"]["population"]["status"] == "passed"


def test_population_only_remap_rejects_a_foreign_grid(tmp_path):
    service, _store = _service(tmp_path)
    _seed_state(service)
    grid = service.state["grid"]

    wrong_level = _population_result(grid)
    wrong_level["grid_level"] = grid["level"] + 1
    with pytest.raises(ValueError):
        service.apply_population_grid_attribute(wrong_level)

    wrong_ids = _population_result(grid)
    wrong_ids["cells"] = {"NOT-A-GRID": {"status": "passed"}}
    with pytest.raises(ValueError):
        service.apply_population_grid_attribute(wrong_ids)

    # 两次失败都不得留下半成品。
    assert service.state["grid_attributes"]["population"]["status"] == "stale"


# ----------------------------------------------------------------- 3) HTTP 端点契约


class _FakeQgis:
    def __init__(self):
        self.calls = 0

    def call(self, work):
        self.calls += 1
        return work()


class _FakeWorkflow:
    """记录 router 调用顺序与 deferred 折叠的替身。"""

    #: ``router.post`` 的 resource_actions 表在**构造时**就会读取这两个属性（没有 lambda 包装）。
    candidate_sites_from_existing = None
    analyze_cns_gaps = None

    def __init__(self, grid):
        self.grid = grid
        self.applied = None
        self.deferred_open = 0
        self.deferred_commits = 0
        self.events = []

    def grid_snapshot(self):
        self.events.append("grid_snapshot")
        return self.grid

    @contextmanager
    def deferred_save(self):
        self.deferred_open += 1
        try:
            yield
        finally:
            self.deferred_open -= 1
            self.deferred_commits += 1

    def apply_population_grid_attribute(self, result):
        assert self.deferred_open == 1, "targeted apply 必须在同一个 deferred 提交块内"
        self.applied = result
        self.events.append("apply")
        return {"status": "applied"}


class _FakeData:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def population_grid_attribute(self, grid):
        self.calls.append(grid)
        return self.result


class _FakeRouterContext:
    def __init__(self, workflow, data):
        self.workflow = workflow
        self.data = data
        self.qgis = _FakeQgis()
        self.static = ROOT / "cns_planner" / "web"


def test_remap_route_runs_only_the_population_mapping_on_the_qgis_thread(monkeypatch, tmp_path):
    _install_qgis_stubs(monkeypatch)
    router_module = importlib.import_module("cns_planner.api.router")

    grid = _grid()
    workflow = _FakeWorkflow(grid)
    data = _FakeData(_population_result(grid))
    context = _FakeRouterContext(workflow, data)
    router = router_module.ApiRouter(context)

    response = router.post("/api/workspace/grid/population/remap", {})

    assert response.status == 200 and response.data == {"status": "applied"}
    assert context.qgis.calls == 1, "网格读取必须走 QGIS 线程"
    assert data.calls == [grid], "只允许调用一次人口映射，且使用当前 grid"
    assert workflow.applied is not None
    assert workflow.deferred_commits == 1, "读取 + 写入必须折叠为一次提交"


def test_remap_route_exists_in_the_get_free_post_table():
    source = (ROOT / "cns_planner" / "api" / "router.py").read_text(encoding="utf-8")
    assert '"/api/workspace/grid/population/remap"' in source
    assert "data.population_grid_attribute(workflow.grid_snapshot())" in source
    assert "workflow.apply_population_grid_attribute(result)" in source
    # 只读入口保持在 GET 表：remap 是唯一的新写入口。
    assert 'if path == "/api/population-nodata-policy": return Response(workflow.population_nodata_policy())' in source
    assert '"/api/population-nodata-policy": lambda: workflow.set_population_nodata_policy(payload)' in source
