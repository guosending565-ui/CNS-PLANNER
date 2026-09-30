"""恢复上次明确项目（/api/project/open）在 HTTP 层的最小契约测试。

本文件只回答一件事：**恢复请求要怎样才能通过服务端的会话校验**。

BUG-PROJECT-RESTORE-001 的现场是：前端在服务器状态落地之前发起了
``POST /api/project/open``，请求头 ``X-CNS-Token`` 为空 → ``do_POST`` 的
``valid_token`` 判定为假 → 403「无效会话或来源」；而 revision 门过得去
（GET ``/api/state`` 的响应体带整数 ``revision``，客户端会记住它）。

因此这里断言三条：
1. 缺 token 的恢复请求必须被拒（403），且不改变任何项目状态；
2. 带当前会话 token + revision 的恢复请求必须被接受，并切换到该项目；
3. 服务器已经是同一个明确项目时重复恢复仍然成功（幂等，不破坏状态）。

全部用 QGIS import stub，不读机器专属数据源，不涉及真实空间计算。
"""

from copy import deepcopy
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from cns_planner.services.workflow import WorkflowService


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
    spec = importlib.util.spec_from_file_location("_restore_map_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMapData:
    """最小 MapData 替身：只记录 ``load()`` 被怎么调用。"""

    def __init__(self, paths=None):
        self.paths = paths or {
            "basemap": "fixture/map.qgz",
            "population": "fixture/population.tif",
            "terrain": "fixture/terrain.tif",
        }
        self.load_calls = []

    def load(self, paths, persist=True):
        self.load_calls.append((deepcopy(paths), persist))
        self.paths = deepcopy(paths)

    def metadata(self):
        return {"paths": deepcopy(self.paths)}


def _configure(module, tmp_path, defaults_path, data=None):
    auto_file = tmp_path / "automatic" / "current_project.json"
    module.DEFAULT_CONFIG = defaults_path
    module.AUTO_PROJECT_FILE = auto_file
    module.ACTIVE_PROJECT_FILE = auto_file
    module.DEFAULTS = {}
    module.WORKFLOW = WorkflowService(auto_file, defaults_path)
    module.DATA = data or FakeMapData()
    return auto_file


def _project_directory(tmp_path, defaults_path, name="explicit-project"):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    workflow = WorkflowService(folder / "project_state.json", defaults_path)
    workflow.set_project({"name": "明确项目"})
    workflow.save()
    (folder / "data_sources.json").write_text(
        json.dumps({
            "basemap": "portable/map.qgz",
            "population": "portable/population.tif",
            "terrain": "portable/terrain.tif",
        }),
        encoding="utf-8",
    )
    return folder


def _post_open(module, project_dir, *, token, revision, host="127.0.0.1:8765", monkeypatch=None):
    """直接驱动 ``Handler.do_POST``，返回 (响应体, 状态码)。"""

    if monkeypatch is not None:
        monkeypatch.setattr(module, "gis_call", lambda action: action())
    payload = json.dumps({"project_dir": str(project_dir)}).encode("utf-8")
    responses = []
    handler = object.__new__(module.Handler)
    handler.path = "/api/project/open"
    headers = {"Host": host, "Content-Length": str(len(payload))}
    if token is not None:
        headers["X-CNS-Token"] = token
    if revision is not None:
        headers["X-CNS-Revision"] = str(revision)
    handler.headers = headers
    handler.rfile = BytesIO(payload)
    handler.allowed = lambda: True
    handler.respond = lambda data, *args, **kwargs: responses.append(
        (data, kwargs.get("status", args[1] if len(args) > 1 else 200))
    )
    handler.do_POST()
    assert responses, "handler 必须给出响应"
    return responses[-1]


def test_restore_open_without_session_token_is_rejected_as_invalid_session(
    tmp_path, defaults_path, map_server_module, monkeypatch
):
    """恢复请求缺 token → 403「无效会话或来源」，且不切换项目。"""

    module = map_server_module
    auto_file = _configure(module, tmp_path, defaults_path)
    folder = _project_directory(tmp_path, defaults_path)
    current_workflow, current_target = module.WORKFLOW, module.ACTIVE_PROJECT_FILE

    body, status = _post_open(
        module, folder, token=None, revision=module.WORKFLOW.state["revision"],
        monkeypatch=monkeypatch,
    )

    assert status == 403
    assert body == {"error": "无效会话或来源"}
    assert module.ACTIVE_PROJECT_FILE == current_target == auto_file
    assert module.WORKFLOW is current_workflow, "被拒的恢复请求绝不能切换 workflow"
    assert module.DATA.load_calls == []


def test_restore_open_with_current_session_token_and_revision_switches_project(
    tmp_path, defaults_path, map_server_module, monkeypatch
):
    """带当前会话 token + revision 的恢复请求必须被接受并切换项目。"""

    module = map_server_module
    auto_file = _configure(module, tmp_path, defaults_path)
    module.WORKFLOW.set_project({"name": "自动恢复项目"})
    folder = _project_directory(tmp_path, defaults_path)
    expected_sources = {
        "basemap": "portable/map.qgz",
        "population": "portable/population.tif",
        "terrain": "portable/terrain.tif",
    }

    _body, status = _post_open(
        module, folder, token=module.TOKEN,
        revision=module.WORKFLOW.state["revision"], monkeypatch=monkeypatch,
    )

    assert status == 200
    assert module.ACTIVE_PROJECT_FILE == folder / "project_state.json"
    assert module.ACTIVE_PROJECT_FILE != auto_file
    assert module.WORKFLOW.store_path == folder / "project_state.json"
    assert module.WORKFLOW.state["project"]["name"] == "明确项目"
    assert module.DATA.paths == expected_sources
    assert module.DATA.load_calls == [(expected_sources, False)]


def test_restore_open_is_idempotent_when_server_already_holds_that_project(
    tmp_path, defaults_path, map_server_module, monkeypatch
):
    """服务器已是同一个明确项目时再次恢复仍然成功（服务器项目优先，不产生冲突）。"""

    module = map_server_module
    _configure(module, tmp_path, defaults_path)
    folder = _project_directory(tmp_path, defaults_path)

    _first, first_status = _post_open(
        module, folder, token=module.TOKEN,
        revision=module.WORKFLOW.state["revision"], monkeypatch=monkeypatch,
    )
    settled_state = deepcopy(module.WORKFLOW.state)
    settled_target = module.ACTIVE_PROJECT_FILE

    _second, second_status = _post_open(
        module, folder, token=module.TOKEN,
        revision=module.WORKFLOW.state["revision"], monkeypatch=monkeypatch,
    )

    assert first_status == 200 and second_status == 200
    assert module.ACTIVE_PROJECT_FILE == settled_target
    assert module.WORKFLOW.state["project"] == settled_state["project"]
    assert module.WORKFLOW.state["project"]["project_id"] == settled_state["project"]["project_id"]


def test_restore_open_with_stale_revision_is_rejected_before_touching_project(
    tmp_path, defaults_path, map_server_module, monkeypatch
):
    """revision 门与 token 门相互独立：过期的 revision 必须返回 409 且不切换项目。"""

    module = map_server_module
    auto_file = _configure(module, tmp_path, defaults_path)
    folder = _project_directory(tmp_path, defaults_path)
    stale = int(module.WORKFLOW.state["revision"]) - 5

    body, status = _post_open(
        module, folder, token=module.TOKEN, revision=stale, monkeypatch=monkeypatch,
    )

    assert status == 409
    assert "刷新" in body["error"]
    assert module.ACTIVE_PROJECT_FILE == auto_file
    assert module.DATA.load_calls == []
