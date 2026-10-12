"""Round34-T07-I2-H3：integration 工作树与主仓库的**共享文件对象**隔离回归。

背景（H3 阶段B）：integration 的 ``projects/current_project.json``、
``projects/data_sources.json``、``projects/map_sources.json`` 曾经是主仓库同名文件的
NTFS **硬链接**，``projects/derived`` 曾是指向主仓库的**目录联接**。这意味着
integration 里的任何一次状态写入（``POST /api/state``、``session.save()``、后台任务
``.cns-tasks`` / ``.cns-results`` 落盘）都会直接改写**正式主仓库**的业务文件。

本测试用 Windows 文件身份（``GetFileInformationByHandle`` 的 VolumeSerial + FileIndex）
而不是路径字符串来判定"是不是同一个文件对象"，并断言：

1. 三个项目 JSON 与主仓库同名文件**不是**同一个文件对象（identity 不同）；
2. integration 工作树内**没有**任何 links>1 的文件／目录（无硬链接）；
3. integration 工作树内**没有**任何 reparse point（无 junction/symlink）；
4. ``projects/map_sources.json`` 不含任何指向主仓库的路径。

未安装 pywin32 也不需要：这里直接用 ``ctypes`` 调 ``kernel32``，与产品代码无耦合。
无主仓库（或非 NTFS）时整个模块 skip，绝不伪造通过。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
from pathlib import Path

import pytest

INTEGRATION = Path(__file__).resolve().parents[1]
MAIN = Path(r"D:\Projects\CNS-PLANNER")
SHARED_FILES = ("current_project.json", "data_sources.json", "map_sources.json")

GENERIC_READ = 0
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not MAIN.is_dir(),
    reason="需要 Windows + 主仓库用于身份比对",
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _INFO(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wt.DWORD), ("ftCreationTime", wt.FILETIME),
        ("ftLastAccessTime", wt.FILETIME), ("ftLastWriteTime", wt.FILETIME),
        ("dwVolumeSerialNumber", wt.DWORD), ("nFileSizeHigh", wt.DWORD),
        ("nFileSizeLow", wt.DWORD), ("nNumberOfLinks", wt.DWORD),
        ("nFileIndexHigh", wt.DWORD), ("nFileIndexLow", wt.DWORD),
    ]


kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CreateFileW.argtypes = [
    wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE,
]
kernel32.GetFileInformationByHandle.argtypes = [wt.HANDLE, ctypes.POINTER(_INFO)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
_INVALID = ctypes.c_void_p(-1).value


def _identity(path: Path):
    """返回 ``(identity, link_count)``；打不开时返回 ``(None, None)``。"""

    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS, None,
    )
    if handle == _INVALID:
        return None, None
    try:
        info = _INFO()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None, None
        index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return f"{int(info.dwVolumeSerialNumber)}:{index}", int(info.nNumberOfLinks)
    finally:
        kernel32.CloseHandle(handle)


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def test_project_files_are_not_shared_with_the_main_repository():
    for name in SHARED_FILES:
        local = INTEGRATION / "projects" / name
        remote = MAIN / "projects" / name
        assert local.is_file(), f"integration 缺少 {name}"
        assert remote.is_file(), f"主仓库缺少 {name}"
        local_identity, local_links = _identity(local)
        remote_identity, remote_links = _identity(remote)
        assert local_identity is not None, f"{name}: 无法取得 integration 文件身份"
        assert remote_identity is not None, f"{name}: 无法取得主仓库文件身份"
        assert local_identity != remote_identity, (
            f"{name} 与主仓库仍是同一个 NTFS 文件对象（{local_identity}）："
            "integration 的写入会直接改写正式主仓库"
        )
        assert local_links == 1 and remote_links == 1, (
            f"{name} 仍有多重链接：integration={local_links} main={remote_links}"
        )


def test_integration_worktree_has_no_hardlinks_or_reparse_points():
    offenders_links = []
    offenders_reparse = []
    for dirpath, dirnames, filenames in os.walk(INTEGRATION):
        if ".git" in Path(dirpath).parts:
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames + dirnames:
            path = Path(dirpath) / name
            if _is_reparse(path):
                offenders_reparse.append(str(path.relative_to(INTEGRATION)))
            _, links = _identity(path)
            if links is not None and links > 1:
                offenders_links.append((str(path.relative_to(INTEGRATION)), links))
    assert not offenders_links, f"integration 仍有硬链接：{offenders_links}"
    assert not offenders_reparse, f"integration 仍有目录联接/符号链接：{offenders_reparse}"


def test_map_sources_do_not_point_at_the_main_repository():
    document = json.loads(
        (INTEGRATION / "projects" / "map_sources.json").read_text(encoding="utf-8")
    )
    offenders = {
        key: value for key, value in document.items()
        if isinstance(value, str) and str(MAIN).lower() in value.lower()
    }
    assert not offenders, f"map_sources.json 仍指向主仓库：{offenders}"
    # 派生数据必须指向本工作树内的独立副本（不得再经主仓库 projects/derived）
    derived = {
        key: value for key, value in document.items()
        if isinstance(value, str) and "derived" in value.replace("\\", "/")
    }
    for key, value in derived.items():
        assert str(INTEGRATION).lower() in value.lower(), (
            f"{key} 的派生数据路径不在本工作树内：{value}"
        )


def test_task_store_is_a_worktree_local_independent_file():
    local = INTEGRATION / "projects" / ".cns-tasks" / "store.json"
    if not local.is_file():
        pytest.skip("integration 尚未创建 .cns-tasks/store.json")
    remote = MAIN / "projects" / ".cns-tasks" / "store.json"
    local_identity, local_links = _identity(local)
    assert local_links == 1, f"store.json 有多重链接：{local_links}"
    if remote.is_file():
        remote_identity, _ = _identity(remote)
        assert local_identity != remote_identity, (
            "integration 的 .cns-tasks/store.json 与主仓库共享文件对象"
        )
    assert str(INTEGRATION).lower() in str(local).lower()
