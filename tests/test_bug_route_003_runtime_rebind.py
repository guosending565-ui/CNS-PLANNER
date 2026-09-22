"""BUG-ROUTE-003：项目切换后必须把 runtime provider/adapter 重新绑定到新 workflow。

根因（修复前）
--------------
``ApplicationContext.__init__`` 把下面 5 类 runtime 绑定挂在**当时那个**
``WorkflowService`` 实例的 service 上：

  * ``route_planner_v3_service.source_readiness``（V3-A 真实来源 readiness）
  * ``v3_operational_adoption_service.transform_resolver``（V3-D metric → CRS84）
  * ``layered_route_planner_service.source_status``（Theta* V2 / layered readiness）
  * ``layered_route_validation_service.evidence_adapter``
  * ``vertical_transition_validation_service.evidence_adapter``

``open_project`` / ``save_project_as`` 会用 ``WorkflowService(target, ...)`` 造一个**新实例**
并替换 ``self.workflow``，但没有在新实例上重新绑定。旧实例上的绑定不会跟着走，新实例的
layered readiness 因此报告 ``terrain/population source unavailable · —``，即使 FABDEM 已
verified、population 已 passed。

本文件锁定
----------
1. 启动 / open / save-as 之后，当前 workflow 的上述 5 类绑定都存在；
2. ``_activate_workflow`` 先切换实例、再绑定（绑定发生在 ``self.workflow`` 已指向新实例之后）；
3. open 真实 fixture 项目后，layered readiness 读取的是**新 workflow**的真实状态
   （terrain 取 ``source_audits.terrain_dtm``，population 取 ``grid_attributes.population``）；
4. 旧 workflow 不再被 activate（它的绑定不被改写，也不参与读取）。

测试用 QGIS import stub 加载 ``app_context``：本机 pytest 环境（anaconda 3.13）没有 QGIS
绑定，而 ``app_context`` → ``gis.map_data`` 会在导入期 import ``qgis.core``。stub 只满足
import 链，不构造任何真实 QGIS 对象，也不打开任何数据集；被验证的是真实的
``_activate_workflow`` / ``_bind_runtime_services`` / ``WorkflowService`` 行为。

禁止事项（本文件不触碰）：路径搜索算法、Validation/Adoption 判据、风险数学、项目数据语义。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from cns_planner.application.project_directory_service import ProjectDirectoryService
from cns_planner.application.workflow_service import WorkflowService

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"

#: 挂在具体 ``WorkflowService`` 实例的 service 上的 runtime 绑定（service 名, 属性名）。
RUNTIME_BINDINGS = (
    ("route_planner_v3_service", "source_readiness"),
    ("v3_operational_adoption_service", "transform_resolver"),
    ("layered_route_planner_service", "source_status"),
    ("layered_route_validation_service", "evidence_adapter"),
    ("vertical_transition_validation_service", "evidence_adapter"),
)

#: ``_bind_runtime_services`` 必须统一调用的 5 个配置方法。
CONFIGURE_METHODS = (
    "configure_route_planner_v3_sources",
    "configure_route_planner_v3_adoption",
    "configure_layered_route_planner_sources",
    "configure_layered_route_validation_sources",
    "configure_vertical_transition_validation_sources",
)

#: 只用于字符串路径的假数据源：任何真实数据打开都会失败，因此测试不可能"偷偷"读盘。
FAKE_PATHS = {
    "basemap": "fixture/basemap.qgz",
    "population": "fixture/population.tif",
    "terrain": "fixture/terrain.tif",
    "terrain_dtm": "fixture/terrain_dtm.tif",
    "buildings": "fixture/buildings.gpkg",
    "building_grid": "fixture/building_grid.gpkg",
}


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
    """把 qgis / osgeo 的最小替身放进 ``sys.modules``（仅本测试进程内有效）。"""

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
    qgis.core = core
    qgis.PyQt = pyqt
    pyqt.QtCore = qtcore
    pyqt.QtGui = qtgui

    osgeo = ModuleType("osgeo")
    osgeo.__path__ = []
    gdal = ModuleType("osgeo.gdal")
    gdal.UseExceptions = lambda: None
    gdal.Open = lambda *args, **kwargs: None
    osgeo.gdal = gdal
    osgeo.ogr = ModuleType("osgeo.ogr")
    osgeo.osr = ModuleType("osgeo.osr")

    for name, module in {
        "qgis": qgis, "qgis.core": core, "qgis.PyQt": pyqt,
        "qgis.PyQt.QtCore": qtcore, "qgis.PyQt.QtGui": qtgui,
        "osgeo": osgeo, "osgeo.gdal": gdal,
        "osgeo.ogr": osgeo.ogr, "osgeo.osr": osgeo.osr,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def app_context_module(monkeypatch):
    """在 QGIS stub 下加载真实的 ``ApplicationContext`` 模块。"""

    _install_qgis_stubs(monkeypatch)
    return importlib.import_module("cns_planner.application.app_context")


class FakeMapData:
    """只实现项目切换真正用到的 ``MapData`` 表面（不打开任何数据集）。"""

    def __init__(self, paths=None):
        self.paths = dict(paths or FAKE_PATHS)
        self.load_calls = []

    def load(self, paths, persist=True):
        self.load_calls.append((dict(paths), persist))
        self.paths = dict(paths)

    def metadata(self):
        return {"paths": dict(self.paths)}


def _context(module, tmp_path, data):
    """组装一个只含"切换 workflow 所需"属性的 ApplicationContext。

    用 ``object.__new__`` 跳过 ``__init__``：真构造需要完整 QGIS 运行时（MapData 会读真实
    数据源），而本测试要验证的恰好是 ``__init__`` / open / save-as 共用的那一个入口
    ``_activate_workflow``。
    """

    from cns_planner.gis.map_data import DEFAULT_PATHS

    automatic = tmp_path / "automatic" / "current_project.json"
    context = object.__new__(module.ApplicationContext)
    context.root = tmp_path
    context.workflow = WorkflowService(automatic, DEFAULTS)
    context.active_project_file = automatic
    context.data = data
    context.project_directories = ProjectDirectoryService(
        automatic, DEFAULTS, DEFAULT_PATHS, WorkflowService
    )
    return context


def _write_fixture_project(folder, *, terrain_status="verified", population_status="passed"):
    """写一个含 verified FABDEM 与 passed population 的最小真实项目。"""

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "project_state.json"
    service = WorkflowService(target, DEFAULTS)
    service.state["source_audits"] = {
        "items": {"terrain_dtm": {"status": terrain_status, "source_id": "fabdem"}},
    }
    service.state["grid_attributes"]["population"] = {
        "status": population_status, "value_status": population_status,
    }
    service.save()
    return target


def _assert_runtime_bindings(workflow):
    for service_name, attribute in RUNTIME_BINDINGS:
        bound = getattr(getattr(workflow, service_name), attribute)
        assert callable(bound), f"{service_name}.{attribute} 未绑定到当前 workflow"


# --------------------------------------------------------------------------------------
# 1) 启动 / 切换后 5 类绑定都存在
# --------------------------------------------------------------------------------------


def test_startup_activation_binds_all_five_runtime_providers(app_context_module, tmp_path):
    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)

    context._activate_workflow(context.workflow, context.active_project_file)

    _assert_runtime_bindings(context.workflow)


def test_bind_runtime_services_rebinds_on_a_replaced_instance(app_context_module, tmp_path):
    """同一个 context 换成新 workflow 后，绑定必须落在新实例上、且是新对象。"""

    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)
    old = context.workflow
    context._activate_workflow(old, context.active_project_file)
    before = {
        service: getattr(getattr(old, service), attribute)
        for service, attribute in RUNTIME_BINDINGS
    }

    replacement = WorkflowService(tmp_path / "replacement.json", DEFAULTS)
    context._activate_workflow(replacement, tmp_path / "replacement.json")

    assert context.workflow is replacement
    _assert_runtime_bindings(replacement)
    for service, attribute in RUNTIME_BINDINGS:
        assert getattr(getattr(replacement, service), attribute) is not before[service], (
            f"{service}.{attribute} 必须重新绑定，而不是复用旧 workflow 上的 provider"
        )


# --------------------------------------------------------------------------------------
# 2) 先切换实例，再绑定
# --------------------------------------------------------------------------------------


def test_activate_workflow_switches_instance_before_binding(app_context_module, tmp_path):
    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)
    replacement = WorkflowService(tmp_path / "switched.json", DEFAULTS)
    target = tmp_path / "switched.json"
    observed = []

    def record(name):
        def recorder(*args, **kwargs):
            observed.append((name, context.workflow, context.active_project_file))
        return recorder

    # configure_reference_sources 由 _activate_workflow 直接作用在目标 workflow 上，
    # 因此 spy 必须装在 replacement 实例上。
    replacement.configure_reference_sources = record("configure_reference_sources")
    for name in CONFIGURE_METHODS:
        setattr(context, name, record(name))

    context._activate_workflow(replacement, target)

    assert [item[0] for item in observed] == [
        "configure_reference_sources", *CONFIGURE_METHODS,
    ], "先 configure_reference_sources，再由 _bind_runtime_services 统一绑定"
    assert all(item[1] is replacement for item in observed), (
        "每个配置步骤都必须已经看到 self.workflow 指向新实例"
    )
    assert all(item[2] == target for item in observed), "self.active_project_file 必须先切换"
    assert context.active_project_file == target


# --------------------------------------------------------------------------------------
# 3) open 真实 fixture 项目
# --------------------------------------------------------------------------------------


def test_open_project_rebinds_and_reads_real_terrain_and_population(app_context_module, tmp_path):
    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)
    old = context.workflow
    context._activate_workflow(old, context.active_project_file)

    # 旧实例上的绑定被替换成哨兵：重新绑定只允许作用在新实例上，旧实例保持原样。
    sentinel = lambda: {"role": "sentinel"}  # noqa: E731 - 测试哨兵
    old.layered_route_planner_service.source_status = sentinel

    target = _write_fixture_project(tmp_path / "project_a")
    context.open_project(str(tmp_path / "project_a"))

    assert context.workflow is not old, "打开项目必须替换为新的 WorkflowService"
    assert context.active_project_file == target
    _assert_runtime_bindings(context.workflow)

    readiness = context.workflow.layered_route_planner_readiness()
    terrain = readiness["sources"]["terrain"]
    population = readiness["sources"]["population"]
    # terrain 只取真实 terrain_dtm audit；population 只取真实 grid_attributes.population.status
    assert terrain["audit_status"] == "verified"
    assert terrain["available"] is True, f"verified FABDEM 仍被报成不可用：{terrain}"
    assert population["status"] == "passed"
    assert population["available"] is True, f"passed population 仍被报成不可用：{population}"

    # 旧 workflow 不再参与：它的绑定仍是哨兵，且新实例的绑定是另一对象。
    assert old.layered_route_planner_service.source_status is sentinel
    assert context.workflow.layered_route_planner_service.source_status is not sentinel


def test_open_project_keeps_source_status_fail_closed_without_real_sources(
    app_context_module, tmp_path
):
    """反向锁定：来源真的不可用时必须仍然报 unavailable（不得把 available 硬设为 true）。"""

    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)
    target = _write_fixture_project(
        tmp_path / "project_missing", terrain_status="missing", population_status="missing_data",
    )
    context.open_project(str(tmp_path / "project_missing"))

    assert context.active_project_file == target
    readiness = context.workflow.layered_route_planner_readiness()
    assert readiness["sources"]["terrain"]["available"] is False
    assert readiness["sources"]["population"]["available"] is False


# --------------------------------------------------------------------------------------
# 4) save_as
# --------------------------------------------------------------------------------------


def test_save_project_as_rebinds_new_workflow(app_context_module, tmp_path):
    data = FakeMapData()
    context = _context(app_context_module, tmp_path, data)
    old = context.workflow
    context._activate_workflow(old, context.active_project_file)
    old.state["source_audits"] = {
        "items": {"terrain_dtm": {"status": "verified", "source_id": "fabdem"}},
    }
    old.state["grid_attributes"]["population"] = {
        "status": "passed", "value_status": "passed",
    }
    sentinel = lambda: {"role": "sentinel"}  # noqa: E731 - 测试哨兵
    old.layered_route_planner_service.source_status = sentinel

    folder = tmp_path / "saved_project"
    context.save_project_as(str(folder))

    assert context.workflow is not old, "另存为必须替换为新的 WorkflowService"
    assert context.active_project_file == folder / "project_state.json"
    _assert_runtime_bindings(context.workflow)

    readiness = context.workflow.layered_route_planner_readiness()
    assert readiness["sources"]["terrain"]["available"] is True
    assert readiness["sources"]["population"]["available"] is True
    assert old.layered_route_planner_service.source_status is sentinel
    assert context.workflow.layered_route_planner_service.source_status is not sentinel


# --------------------------------------------------------------------------------------
# 5) 源码级锁定：三条路径都必须经过同一个 runtime binder
# --------------------------------------------------------------------------------------


def test_application_context_routes_every_switch_through_activate_workflow():
    source = (ROOT / "cns_planner" / "application" / "app_context.py").read_text(
        encoding="utf-8"
    )
    init_body = source[source.index("def __init__(self, root=None):"):source.index("@property")]
    save_body = source[
        source.index("def save_project_as(self, project_dir):"):source.index(
            "def open_project(self, project_dir):"
        )
    ]
    open_body = source[
        source.index("def open_project(self, project_dir):"):source.index(
            "def replace_sources(self, paths):"
        )
    ]
    bind_body = source[
        source.index("def _bind_runtime_services(self):"):source.index(
            "def save_project_as(self, project_dir):"
        )
    ]

    assert "_activate_workflow(self.workflow, self.active_project_file)" in init_body
    assert "_activate_workflow(workflow, target)" in save_body
    assert "_activate_workflow(workflow, target)" in open_body
    for body in (save_body, open_body):
        assert "self.workflow, self.active_project_file = workflow, target" not in body, (
            "不得绕开 _activate_workflow 直接替换实例（那样会丢绑定）"
        )
    for name in CONFIGURE_METHODS:
        assert f"self.{name}()" in bind_body, f"_bind_runtime_services 漏掉 {name}"
