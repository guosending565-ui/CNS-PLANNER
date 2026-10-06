"""F-03 回归：**真正的新建空项目**（``create_blank``）。

Round32-A 的 M-03：当前 UI/API 只有 Save As / Open —— 新用户必须先手工建目录或先
「另存为」才能拿到一个项目目录，而 Save As 会**继承**当前项目的全部结果容器。
``create_blank`` 与它语义相反，本文件逐条锁定：

1. 只从 ``blank_project(defaults)`` 默认链生成 state（含 registry 默认 algorithm_selection）；
2. 不继承当前项目的 state（除项目名称/身份/时间戳外的每个键都必须与空白基线逐字相同）；
3. 数据源只写公共默认配置，不沿用当前项目已解析的来源；
4. 目录冲突一律 fail-closed，绝不覆盖用户文件（已有项目 / 非空非项目目录）；
5. 失败时回滚，不留半成品；
6. 创建成功的项目可以被正常重新打开（不是"写了一份读不回来的文件"）。
"""

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.project_directory_service import ProjectDirectoryService  # noqa: E402
from cns_planner.application.project_state import blank_project  # noqa: E402
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.persistence.project_repository import ProjectRepository  # noqa: E402

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"
DEFAULT_SOURCES = {
    "basemap": "D:/defaults/basemap.tif",
    "population": "D:/defaults/population.tif",
    "terrain": "D:/defaults/terrain.tif",
}
#: ``WorkflowSession._commit`` 会推进这几个字段，它们不属于"继承"判据。
MUTABLE_KEYS = ("revision", "last_saved_at", "project")


class _Data:
    """``MapData`` 替身：只记录 ``load()``，不开真实数据（与 BUG-SHOT-002 测试同一手法）。"""

    def __init__(self, paths=None):
        self.paths = dict(paths or {"basemap": "CURRENT-B", "population": "CURRENT-P", "terrain": "CURRENT-T"})
        self.loaded = None

    def load(self, paths, persist=True):
        self.loaded = dict(paths)
        self.paths = dict(paths)
        return self


def _service(tmp_path, factory=WorkflowService):
    return ProjectDirectoryService(
        tmp_path / "auto" / "current_project.json", DEFAULTS, DEFAULT_SOURCES, factory,
    )


def _blank_reference():
    return blank_project(json.loads(DEFAULTS.read_text(encoding="utf-8")))


def _load_state(folder):
    return ProjectRepository(folder / "project_state.json").load()


def _strip_mutable(state):
    return {key: value for key, value in state.items() if key not in MUTABLE_KEYS}


def _created_state(tmp_path, service=None, folder="probe", name="探针项目"):
    """用同一套初始化链造一个对照项目，返回它的落盘 state。"""

    target = tmp_path / folder
    (service or _service(tmp_path)).create_blank(target, name)
    return _load_state(target)


def _seed_current_project(tmp_path):
    """放一个"当前项目"：它在 automatic 位置上，并且带着真实业务结果。"""

    auto = tmp_path / "auto"
    auto.mkdir(parents=True, exist_ok=True)
    previous = blank_project({})
    previous["project"]["name"] = "上一个项目"
    previous["workspace"] = {"bbox": [1, 2, 3, 4], "revision": 7}
    previous["operational_routes"] = {
        "status": "passed", "collection_id": "operational_routes", "count": 2,
        "items": [{"route_id": "R-OLD"}], "source": None, "metadata": {},
    }
    ProjectRepository(auto / "current_project.json").save(previous)
    return previous


def test_create_blank_matches_blank_project_defaults(tmp_path):
    """新建项目的 state 就是 ``blank_project(defaults)`` 默认链，只额外带项目名称。

    对照口径：另一个**同链条**新建项目的 state（初始化默认链会补建 canonical 目录，
    例如 aircraft_profiles / planning_exposure_policy，那属于"默认链"而不是继承）。
    """

    folder = tmp_path / "new_project"
    service = _service(tmp_path)
    candidate, target = service.create_blank(folder, "  全新项目  ")
    assert target == folder / "project_state.json"
    assert isinstance(candidate, WorkflowService)
    state = _load_state(folder)
    reference = _blank_reference()
    assert state["project"]["name"] == "全新项目", "项目名称必须取用户输入（并去掉首尾空白）"
    # registry 默认 algorithm_selection：与空白基线逐字相同，即"当前 registry defaults"。
    assert state["algorithm_selection"] == reference["algorithm_selection"]
    assert state["algorithm_selection"], "默认算法选择不得为空"
    # 与同链条的另一个新建项目除项目身份外完全一致 → 不含任何"当前项目"痕迹。
    assert _strip_mutable(state) == _strip_mutable(_created_state(tmp_path, service))


def test_create_blank_does_not_inherit_current_project_state_or_sources(tmp_path):
    """当前项目带着 workspace / 运行航路 / 已解析来源时，新建项目必须**一处都不继承**。"""

    previous = _seed_current_project(tmp_path)
    folder = tmp_path / "fresh"
    service = _service(tmp_path)
    data = _Data()
    service.create_blank(folder, "空白项目", data)
    state = _load_state(folder)
    assert state["workspace"] is None
    assert state["grid"] is None
    assert state["project"]["project_id"] != previous["project"]["project_id"]
    assert (state.get("operational_routes") or {}).get("count", 0) == 0
    # 数据源只来自公共默认配置：既不是当前项目已解析的 data.paths，也不是它的落盘配置。
    written = json.loads((folder / "data_sources.json").read_text(encoding="utf-8"))
    assert written == DEFAULT_SOURCES, "项目 data_sources.json 必须来自公共默认配置"
    assert data.loaded == DEFAULT_SOURCES, "新项目激活后内存数据源必须切到该项目的配置"
    assert all("CURRENT" not in str(value) for value in written.values())
    assert _strip_mutable(state) == _strip_mutable(_created_state(tmp_path, service))


def test_create_blank_is_fail_closed_on_conflicting_directories(tmp_path):
    """目录冲突一律 fail-closed，且绝不覆盖用户文件 / 已存在的项目。"""

    service = _service(tmp_path)
    # 1) 已经是 CNS 项目 → 提示改用「打开项目」，原文件一字不改。
    existing = tmp_path / "existing"
    existing.mkdir(parents=True)
    ProjectRepository(existing / "project_state.json").save({"schema_version": "keep-me"})
    before = (existing / "project_state.json").read_bytes()
    with pytest.raises(ValueError, match="打开项目"):
        service.create_blank(existing, "不该被创建")
    assert (existing / "project_state.json").read_bytes() == before
    # 2) 非空且不是 CNS 项目 → 绝不覆盖用户文件，也不留下任何新文件。
    user_files = tmp_path / "user_files"
    user_files.mkdir(parents=True)
    user_file = user_files / "我的资料.txt"
    user_file.write_text("重要内容", encoding="utf-8")
    with pytest.raises(ValueError, match="不是空目录"):
        service.create_blank(user_files, "不该被创建")
    assert user_file.read_text(encoding="utf-8") == "重要内容"
    assert not (user_files / "project_state.json").exists()
    assert not (user_files / "data_sources.json").exists()
    # 3) 同一目录新建两次：第二次必须被当作"已有项目"拒绝（幂等安全）。
    twice = tmp_path / "twice"
    service.create_blank(twice, "第一次")
    with pytest.raises(ValueError, match="打开项目"):
        service.create_blank(twice, "第二次")
    assert _load_state(twice)["project"]["name"] == "第一次"


def test_create_blank_rejects_invalid_input(tmp_path):
    """名称 / 目录的非法输入必须在写任何文件之前就被拒绝。"""

    cases = [
        ("", "C:/somewhere", "项目名称"),
        ("x" * 121, "C:/somewhere", "项目名称"),
        ("正常名称", "", "项目目录"),
        ("正常名称", "relative/path", "完整路径"),
    ]
    for name, directory, match in cases:
        with pytest.raises(ValueError, match=match):
            _service(tmp_path).create_blank(directory, name)


def test_create_blank_creates_missing_directory_and_is_reopenable(tmp_path):
    """目录不存在时创建（含父目录），且创建出来的项目能被正常重新打开。"""

    folder = tmp_path / "deep" / "nested" / "project"
    service = _service(tmp_path)
    service.create_blank(folder, "可重开项目")
    assert folder.is_dir()
    candidate, target = service.open(folder, _Data())
    assert target == folder / "project_state.json"
    snapshot = candidate.snapshot()
    assert snapshot["project"]["name"] == "可重开项目"
    assert not snapshot.get("workspace"), "新项目不得带有工作区"


def test_create_blank_rolls_back_when_initialization_fails(tmp_path):
    """初始化默认链失败时，不留半成品文件（原子性）。"""

    class _Broken(WorkflowService):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("模拟默认链失败")

    folder = tmp_path / "broken"
    with pytest.raises(RuntimeError):
        _service(tmp_path, factory=_Broken).create_blank(folder, "失败项目")
    assert not (folder / "project_state.json").exists()
    assert not (folder / "data_sources.json").exists()
