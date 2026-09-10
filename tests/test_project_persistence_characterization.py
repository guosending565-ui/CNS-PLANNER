"""Project persistence characterization and documented safety expectations.

All files live below pytest's temporary directory.  The map-server persistence
functions are loaded with QGIS import stubs so these tests require neither QGIS
nor machine-specific data sources.
"""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from cns_planner.services.workflow import WorkflowService


def _health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 3,
        "covered_layer_count": 3,
    }


def _populate(service):
    service.set_project({"name": "持久化回归项目"})
    service.set_workspace([120.0, 30.0, 120.01, 30.01], _health())
    service.add_node([120.002, 30.002], "A")
    service.add_node([120.008, 30.008], "B")
    service.generate_scenario("both")
    service.set_rules(
        {
            "manufacturer": "固定测试厂商",
            "model": "PERSIST-V1",
            "cruise_speed": 25,
            "max_speed": 40,
            "mtbf": 10000,
            "route_id": "R0001",
            "height_ab": 120,
            "height_ba": 150,
            "height_mode": "different",
            "horizontal_separation": 100,
            "direction_rule": "按航向分层",
            "delay_sensor": 500,
            "delay_command": 500,
        }
    )
    service.generate_operational([])
    devices = [{**item, "mtbf": item["mtbf_h"]} for item in service.snapshot()["devices"]]
    service.set_devices(devices)
    service.plan_coverage()
    return service


@pytest.fixture
def defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir()
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


@pytest.fixture
def map_server_module(monkeypatch):
    class Dummy:
        WriteOnly = 1

        def __init__(self, *args, **kwargs):
            pass

    qgis = ModuleType("qgis")
    qgis.__path__ = []
    core = ModuleType("qgis.core")
    for name in (
        "QgsApplication",
        "QgsProject",
        "QgsCoordinateReferenceSystem",
        "QgsCoordinateTransform",
        "QgsRectangle",
        "QgsMapSettings",
        "QgsMapRendererParallelJob",
        "QgsRasterLayer",
        "QgsRasterShader",
        "QgsColorRampShader",
        "QgsSingleBandPseudoColorRenderer",
    ):
        setattr(core, name, Dummy)
    pyqt = ModuleType("qgis.PyQt")
    pyqt.__path__ = []
    qtcore = ModuleType("qgis.PyQt.QtCore")
    qtcore.QSize = qtcore.QBuffer = qtcore.QIODevice = Dummy
    qtgui = ModuleType("qgis.PyQt.QtGui")
    qtgui.QColor = Dummy
    qgis.core = core
    qgis.PyQt = pyqt
    pyqt.QtCore = qtcore
    pyqt.QtGui = qtgui

    osgeo = ModuleType("osgeo")
    osgeo.__path__ = []
    gdal = ModuleType("osgeo.gdal")
    gdal.UseExceptions = lambda: None
    osgeo.gdal = gdal

    for name, module in {
        "qgis": qgis,
        "qgis.core": core,
        "qgis.PyQt": pyqt,
        "qgis.PyQt.QtCore": qtcore,
        "qgis.PyQt.QtGui": qtgui,
        "osgeo": osgeo,
        "osgeo.gdal": gdal,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(Path("cns_planner").resolve()))

    module_path = Path("cns_planner/map_server.py").resolve()
    spec = importlib.util.spec_from_file_location("_persistence_map_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMapData:
    def __init__(self, paths=None, load_error=None):
        self.paths = paths or {
            "basemap": "fixture/map.qgz",
            "population": "fixture/population.tif",
            "terrain": "fixture/terrain.tif",
        }
        self.load_error = load_error
        self.load_calls = []

    def load(self, paths, persist=True):
        self.load_calls.append((deepcopy(paths), persist))
        if self.load_error:
            raise self.load_error
        self.paths = deepcopy(paths)

    def metadata(self):
        return {"paths": deepcopy(self.paths)}


def _configure_map_server(module, tmp_path, defaults_path, data=None):
    auto_file = tmp_path / "automatic" / "current_project.json"
    module.DEFAULT_CONFIG = defaults_path
    module.AUTO_PROJECT_FILE = auto_file
    module.ACTIVE_PROJECT_FILE = auto_file
    module.DEFAULTS = {}
    module.WORKFLOW = module.WorkflowService(auto_file, defaults_path)
    module.DATA = data or FakeMapData()
    return auto_file


def test_current_project_auto_save_and_reload_preserves_full_state(tmp_path, defaults_path):
    store = tmp_path / "automatic" / "current_project.json"
    original = _populate(WorkflowService(store, defaults_path))

    saved_document = json.loads(store.read_text(encoding="utf-8"))
    restored = WorkflowService(store, defaults_path)

    assert saved_document == original.state
    assert restored.state == original.state
    for field in (
        "schema_version",
        "project",
        "workspace",
        "nodes",
        "node_seq",
        "route_seq",
        "retired_route_ids",
        "scenario_routes",
        "operational_routes",
        "aircraft",
        "rules",
        "devices",
        "coverage",
        "risks",
        "result_statuses",
        "last_saved_at",
    ):
        assert restored.state[field] == original.state[field]
    assert not list(tmp_path.rglob("*.tmp"))


def test_save_as_copies_state_and_sources_then_uses_new_project(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    _populate(module.WORKFLOW)
    before = deepcopy(module.WORKFLOW.state)
    destination = tmp_path / "saved-project"

    metadata = module.save_project_as(destination)

    target = destination / "project_state.json"
    assert auto_file.is_file()
    assert target.is_file()
    assert module.ACTIVE_PROJECT_FILE == target
    assert module.WORKFLOW.store_path == target
    assert module.WORKFLOW.state == json.loads(target.read_text(encoding="utf-8"))
    assert module.WORKFLOW.state["project"]["project_id"] == before["project"]["project_id"]
    assert module.WORKFLOW.state["project"]["name"] == "持久化回归项目"
    assert json.loads((destination / "data_sources.json").read_text(encoding="utf-8")) == module.DATA.paths
    assert metadata == {"paths": module.DATA.paths}
    assert not list(tmp_path.rglob("*.tmp"))


def test_open_existing_project_switches_state_and_restores_sources(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "当前项目"})
    current_id = module.WORKFLOW.state["project"]["project_id"]

    project_dir = tmp_path / "existing-project"
    target = project_dir / "project_state.json"
    existing = _populate(module.WorkflowService(target, defaults_path))
    expected_state = deepcopy(existing.state)
    expected_sources = {
        "basemap": "portable/map.qgz",
        "population": "portable/population.tif",
        "terrain": "portable/terrain.tif",
    }
    sources_file = project_dir / "data_sources.json"
    sources_file.write_text(json.dumps(expected_sources), encoding="utf-8")
    files_before = {path: path.read_bytes() for path in project_dir.iterdir()}

    module.open_project(project_dir)

    assert module.ACTIVE_PROJECT_FILE == target
    assert module.ACTIVE_PROJECT_FILE != auto_file
    assert module.WORKFLOW.state == expected_state
    assert module.WORKFLOW.state["project"]["project_id"] != current_id
    assert module.DATA.paths == expected_sources
    assert module.DATA.load_calls == [(expected_sources, False)]
    assert {path: path.read_bytes() for path in project_dir.iterdir()} == files_before
    assert not list(tmp_path.rglob("*.tmp"))


def test_open_missing_project_file_preserves_current_project(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "仍然有效"})
    current_workflow = module.WORKFLOW
    current_state = deepcopy(current_workflow.state)
    empty_project_dir = tmp_path / "empty-project"
    empty_project_dir.mkdir()

    with pytest.raises(ValueError, match="缺少 project_state.json"):
        module.open_project(empty_project_dir)

    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.WORKFLOW is current_workflow
    assert module.WORKFLOW.state == current_state
    assert list(empty_project_dir.iterdir()) == []


def test_source_load_failure_preserves_current_project_and_writes_nothing(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    failing_data = FakeMapData(load_error=ValueError("fixture source load failed"))
    auto_file = _configure_map_server(module, tmp_path, defaults_path, failing_data)
    module.WORKFLOW.set_project({"name": "当前有效项目"})
    current_workflow = module.WORKFLOW
    current_state = deepcopy(current_workflow.state)
    current_paths = deepcopy(module.DATA.paths)

    project_dir = tmp_path / "source-failure-project"
    target = project_dir / "project_state.json"
    _populate(module.WorkflowService(target, defaults_path))
    (project_dir / "data_sources.json").write_text(
        json.dumps(
            {
                "basemap": "bad/map.qgz",
                "population": "bad/population.tif",
                "terrain": "bad/terrain.tif",
            }
        ),
        encoding="utf-8",
    )
    files_before = {path: path.read_bytes() for path in project_dir.iterdir()}

    with pytest.raises(ValueError, match="fixture source load failed"):
        module.open_project(project_dir)

    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.WORKFLOW is current_workflow
    assert module.WORKFLOW.state == current_state
    assert module.DATA.paths == current_paths
    assert {path: path.read_bytes() for path in project_dir.iterdir()} == files_before
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.xfail(
    strict=True,
    reason="unsafe current behavior: an unsupported schema silently opens as a new blank project",
)
def test_open_invalid_schema_should_reject_and_preserve_current_project(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "当前有效项目"})
    current_workflow = module.WORKFLOW
    project_dir = tmp_path / "invalid-schema-project"
    project_dir.mkdir()
    (project_dir / "project_state.json").write_text(
        json.dumps({"schema_version": 999, "project": {"name": "不应打开"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema"):
        module.open_project(project_dir)

    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.WORKFLOW is current_workflow


@pytest.mark.xfail(
    strict=True,
    reason="unsafe current behavior: damaged JSON silently opens as a new blank project",
)
def test_open_damaged_json_should_reject_and_preserve_current_project(
    tmp_path, defaults_path, map_server_module
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "当前有效项目"})
    current_workflow = module.WORKFLOW
    project_dir = tmp_path / "damaged-project"
    project_dir.mkdir()
    (project_dir / "project_state.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON"):
        module.open_project(project_dir)

    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.WORKFLOW is current_workflow


@pytest.mark.xfail(
    strict=True,
    reason="unsafe current behavior: a failed atomic replace leaves an orphan .tmp file",
)
def test_auto_save_replace_failure_should_not_leave_temporary_file(
    tmp_path, defaults_path, monkeypatch
):
    store = tmp_path / "automatic" / "current_project.json"
    service = WorkflowService(store, defaults_path)
    service.set_project({"name": "已保存版本"})
    saved_bytes = store.read_bytes()
    service.state["project"]["name"] = "未完成版本"
    temporary = store.with_suffix(".tmp")
    original_replace = Path.replace

    def fail_target_replace(path, target):
        if path == temporary:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_target_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        service.save()

    assert store.read_bytes() == saved_bytes
    assert not temporary.exists()


@pytest.mark.xfail(
    strict=True,
    reason="unsafe current behavior: interrupted save-as copy can leave a partial project_state.json",
)
def test_save_as_copy_failure_should_not_leave_partial_target(
    tmp_path, defaults_path, map_server_module, monkeypatch
):
    module = map_server_module
    auto_file = _configure_map_server(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "当前有效项目"})
    current_workflow = module.WORKFLOW
    destination = tmp_path / "partial-save-project"
    partial_target = destination / "project_state.json"

    def interrupted_copy(repository, target):
        Path(target).write_text('{"schema_version":', encoding="utf-8")
        raise OSError("simulated interrupted copy")

    monkeypatch.setattr(module.ProjectRepository, "copy_to", interrupted_copy)

    with pytest.raises(OSError, match="simulated interrupted copy"):
        module.save_project_as(destination)

    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.WORKFLOW is current_workflow
    assert not partial_target.exists()
