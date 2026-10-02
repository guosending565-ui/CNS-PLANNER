"""BUG-SHOT-002 回归：打开**只读**项目目录时不得因为写 data_sources.tmp 而失败。

现象：``POST /api/project/open`` 打开外部只读基线项目时抛
``[Errno 13] Permission denied: ...\\data_sources.tmp``，项目完全打不开。

根因：``ProjectDirectoryService.open()`` 里有一条**确定性兼容迁移**——当项目状态里
登记了 verified 的 legacy tower 来源、而 ``data_sources.json`` 还没有它时，会把迁移结果
写回项目目录。迁移结果在本次会话内已经完整生效，写回只是让下次打开少推导一次，属于
可选的持久化优化；但它在只读目录下会直接抛 OSError，把"信息更完整"的迁移变成"打不开"。

本测试用真实只读目录（Windows 上收紧 ACL）覆盖这条路径：

1. 只读目录也能打开，且**不阻塞**（BUG-SHOT-002 的原始症状）；
2. 迁移写入失败被如实记录在 ``migration_write_error`` 上，可被 UX 展示；
3. 可写目录下迁移仍然会正常持久化（不得因为修复而丢掉这条兼容迁移）。
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.project_directory_service import ProjectDirectoryService  # noqa: E402
from cns_planner.application.project_state import blank_project  # noqa: E402
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.persistence.project_repository import ProjectRepository  # noqa: E402

DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"


class _Data:
    paths = {"basemap": "B", "population": "P", "terrain": "T"}

    def __init__(self):
        self.loaded = None

    def load(self, paths, persist=True):
        #: ``open()`` 最后会把合并后的来源装回 MapData；这里只记录，不开真实数据。
        self.loaded = dict(paths)
        self.paths = dict(paths)
        return self

    def register_source_paths(self, *args, **kwargs):
        return None


def _service(tmp_path):
    return ProjectDirectoryService(
        tmp_path / "auto" / "current_project.json", DEFAULTS, _Data.paths, WorkflowService,
    )


def _project_state(tower_file: Path):
    """可被 ``normalize_project`` 接受的完整项目状态 + 一条 verified legacy tower 来源。"""

    stat = tower_file.stat()
    state = blank_project({})
    state["towers"] = {
        "status": "passed",
        "count": 1,
        "items": [{"tower_id": "T1", "longitude": 122.2, "latitude": 29.9}],
        "source": {"path": str(tower_file)},
    }
    state["source_audits"] = {
        "items": {
            "towers": {
                "status": "verified",
                "verification": {
                    "sha256": "deadbeef", "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                },
                "evidence": [],
            }
        }
    }
    return state


def _write_project(folder: Path, state: dict):
    folder.mkdir(parents=True, exist_ok=True)
    ProjectRepository(folder / "project_state.json").save(state)


def _make_readonly(folder: Path):
    """用 icacls 拒绝写入（Windows）。返回是否成功收紧。"""

    user = os.environ.get("USERNAME") or ""
    result = subprocess.run(
        ["icacls", str(folder), "/deny", f"{user}:(WD,AD,DC,DE,WDAC)"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    # 确认真的写不进去（否则测试会在错误前提下"通过"）。
    try:
        (folder / ".write_probe").write_text("x", encoding="utf-8")
    except OSError:
        return True
    (folder / ".write_probe").unlink(missing_ok=True)
    return False


def _restore_writable(folder: Path):
    subprocess.run(["icacls", str(folder), "/reset"], capture_output=True, text=True)


@pytest.mark.skipif(os.name != "nt", reason="只读目录语义在 Windows 上用 ACL 验证")
def test_open_readonly_project_succeeds_and_records_write_failure(tmp_path):
    tower_file = tmp_path / "towers.xlsx"
    tower_file.write_text("tower-id,lon,lat\nT1,122.2,29.9\n", encoding="utf-8")
    folder = tmp_path / "ro_project"
    _write_project(folder, _project_state(tower_file))
    assert _make_readonly(folder), "无法把测试目录设为只读，跳过"
    try:
        service = _service(tmp_path)
        candidate, target = service.open(folder, _Data())
        # 关键断言：只读目录也必须能打开（修复前这里直接抛 PermissionError）。
        assert target == folder / "project_state.json"
        assert isinstance(candidate, WorkflowService)
        # 迁移写入失败必须被如实记录，供 UX 提示"项目为只读"。
        assert service.migration_write_error, "只读目录下的写入失败必须被如实记录"
        assert not (folder / "data_sources.json").exists(), "只读项目目录不得被改动"
    finally:
        _restore_writable(folder)


def test_open_writable_project_still_persists_the_migration(tmp_path):
    tower_file = tmp_path / "towers.xlsx"
    tower_file.write_text("tower-id,lon,lat\nT1,122.2,29.9\n", encoding="utf-8")
    folder = tmp_path / "rw_project"
    _write_project(folder, _project_state(tower_file))
    service = _service(tmp_path)
    service.open(folder, _Data())
    written = json.loads((folder / "data_sources.json").read_text(encoding="utf-8"))
    assert written.get("towers") == str(tower_file), "可写目录下兼容迁移必须照常持久化"
    assert service.migration_write_error is None
