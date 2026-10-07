"""Round32-I：MVP 保存可靠性稳定化 —— 正式 ProjectState 落盘定向测试。

覆盖本轮目标行为：

* 正常大 JSON 保存成功，且临时文件与正式文件同目录、替换发生在写完+fsync 之后；
* 瞬时 ``WinError 5``（目标文件正被读句柄占用）的有界重试：首败次成 / 耗尽后安全失败；
* 真实句柄语义：读者持句柄期间替换失败，读者一释放重试即成功；
* serialization 失败、非瞬时 ``OSError`` 都不破坏原文件，且非瞬时错误不重试；
* Windows ``winerror`` 是排他判据：非白名单 ``winerror`` + ``errno=EACCES`` 不判为瞬时；
* 非 replace 阶段（temp 写入）的 ``OSError`` 不会被伪装成 replace 占用；
* 重试预算有界且很小（不无限重试）；
* 并发保存不产生截断 JSON；
* ``WorkflowSession`` 保存失败时 revision 回滚、且不伪报保存成功。

实现方式：小 fixture + 注入 replace / 权限失败。**不创建 275 MB 测试数据**，
本文件也不是全量回归。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import threading
import time

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.persistence import atomic_io
from cns_planner.persistence.project_repository import (
    ProjectRepository, ProjectSaveError,
)


def _access_denied(target, winerror=5):
    """构造带 Windows ``winerror`` 的瞬时拒绝访问异常。"""

    return PermissionError(errno.EACCES, "Access is denied", str(target), winerror)


def _fast_backoff(monkeypatch):
    """把退避压到 0（只为测试提速；预算本身由预算不变量测试守护）。"""

    monkeypatch.setattr(atomic_io, "REPLACE_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0))


@pytest.fixture
def defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir()
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def _store(tmp_path):
    path = tmp_path / "project" / "project_state.json"
    return path, ProjectRepository(path)


# ---- 1. 正常大 JSON 保存 -------------------------------------------------------


def test_large_project_state_save_roundtrips_intact(tmp_path):
    path, repository = _store(tmp_path)
    document = {
        "schema_version": 2,
        "revision": 1,
        "cells": {f"G{i}": {"value": "x" * 200} for i in range(20000)},
    }

    repository.save(document)

    assert path.stat().st_size > 4_000_000
    assert repository.load()["cells"] == document["cells"]
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 1
    assert not list(path.parent.glob("*.tmp"))


# ---- 2. 同目录临时文件 + 原子替换（不得先破坏正式文件） -----------------------


def test_save_stages_in_same_directory_then_replaces_atomically(tmp_path, monkeypatch):
    path, repository = _store(tmp_path)
    repository.save({"revision": 1, "payload": "v1"})
    observed = {}
    real_replace = os.replace

    def spy_replace(source, target):
        source, target = Path(source), Path(target)
        if target == path:
            observed["source"] = source
            # 替换发生的这一刻：临时文件已完整，正式文件仍是旧内容。
            observed["staged"] = source.read_bytes()
            observed["target"] = target.read_bytes()
        return real_replace(source, target)

    monkeypatch.setattr(atomic_io.os, "replace", spy_replace)

    repository.save({"revision": 2, "payload": "v2"})

    assert observed["source"].parent == path.parent          # 同目录 → 同卷原子 rename
    assert observed["source"].suffix == ".tmp"
    assert json.loads(observed["staged"]) == {"revision": 2, "payload": "v2"}
    assert json.loads(observed["target"]) == {"revision": 1, "payload": "v1"}
    assert json.loads(repository.backup_path.read_text(encoding="utf-8"))["revision"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 2
    assert not list(path.parent.glob("*.tmp"))


# ---- 3. 第一次 WinError 5，第二次成功 ------------------------------------------


def test_first_transient_winerror5_is_retried_and_succeeds(tmp_path, monkeypatch):
    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    attempts = {"count": 0}
    real_replace = os.replace

    def flaky_replace(source, target):
        if Path(target) == path:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _access_denied(target)
        return real_replace(source, target)

    _fast_backoff(monkeypatch)
    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)

    repository.save({"revision": 2})

    assert attempts["count"] == 2
    assert repository.load()["revision"] == 2
    assert not list(path.parent.glob("*.tmp"))


# ---- 4. 持续 WinError 5：有界耗尽 + 安全失败 -----------------------------------


def test_persistent_winerror5_exhausts_bounded_retries_safely(tmp_path, monkeypatch):
    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    repository.save({"revision": 2})
    before = path.read_bytes()
    attempts = {"count": 0}
    real_replace = os.replace

    def always_busy(source, target):
        if Path(target) == path:
            attempts["count"] += 1
            raise _access_denied(target)
        return real_replace(source, target)

    _fast_backoff(monkeypatch)
    monkeypatch.setattr(atomic_io.os, "replace", always_busy)

    with pytest.raises(ProjectSaveError) as info:
        repository.save({"revision": 3})

    assert attempts["count"] == atomic_io.REPLACE_ATTEMPTS == 5   # 次数很少、绝不无限
    assert info.value.detail["winerror"] == 5
    assert "占用" in str(info.value)                              # 普通用户看得懂
    assert "WinError 5" in str(info.value)                        # 高级信息保留
    assert isinstance(info.value.__cause__, OSError)
    assert path.read_bytes() == before                            # 正式文件未被破坏
    # 备份轮换发生在正式替换之前（既有语义），因此 .bak 是完整的旧正式版本，不是半截。
    assert json.loads(repository.backup_path.read_text(encoding="utf-8"))["revision"] == 2
    assert not list(path.parent.glob("*.tmp"))


# ---- 5. serialization 失败：原文件与备份都不变 --------------------------------


def test_serialization_failure_keeps_original_and_backup(tmp_path):
    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    repository.save({"revision": 2})
    before = path.read_bytes()
    backup_before = repository.backup_path.read_bytes()

    with pytest.raises(ValueError):
        repository.save({"revision": 3, "bad": float("nan")})

    assert path.read_bytes() == before
    assert repository.backup_path.read_bytes() == backup_before
    assert not list(path.parent.glob("*.tmp"))


# ---- 6. 非瞬时 IOError：不重试、原样抛出 --------------------------------------


def test_non_transient_oserror_is_not_retried(tmp_path, monkeypatch):
    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    before = path.read_bytes()
    attempts = {"count": 0}
    real_replace = os.replace

    def disk_full(source, target):
        if Path(target) == path:
            attempts["count"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(source, target)

    monkeypatch.setattr(atomic_io.os, "replace", disk_full)

    with pytest.raises(OSError) as info:
        repository.save({"revision": 2})

    assert not isinstance(info.value, ProjectSaveError)   # 不伪装成"被占用"
    assert attempts["count"] == 1                          # 非瞬时错误立即失败
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


# ---- 7. 真实句柄语义：读者释放后重试成功（根因回归） --------------------------


def test_save_tolerates_real_reader_window(tmp_path):
    """用真实 Windows 句柄复现根因：读者持句柄时替换失败，释放后重试成功。"""

    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    opened = threading.Event()

    def hold_reader():
        with open(path, "rb") as stream:     # 普通读句柄（真实第三方/worker 语义）
            opened.set()
            stream.read()
            time.sleep(0.35)                 # 远短于有界重试预算（1.5 s）

    reader = threading.Thread(target=hold_reader, daemon=True)
    reader.start()
    assert opened.wait(5)
    try:
        repository.save({"revision": 2})
        assert repository.load()["revision"] == 2
        assert not list(path.parent.glob("*.tmp"))
    finally:
        reader.join(5)


# ---- 8. 并发保存不产生截断 JSON ------------------------------------------------


def test_concurrent_saves_never_produce_truncated_json(tmp_path):
    path = tmp_path / "project" / "project_state.json"

    def save(revision):
        ProjectRepository(path).save({"revision": revision, "payload": str(revision) * 4000})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(24)))

    current = json.loads(path.read_text(encoding="utf-8"))
    backup = json.loads(ProjectRepository(path).backup_path.read_text(encoding="utf-8"))
    assert current["payload"] == str(current["revision"]) * 4000
    assert backup["payload"] == str(backup["revision"]) * 4000
    assert not list(path.parent.glob("*.tmp"))


# ---- 9. WorkflowSession：失败回滚 revision，不伪报成功 ------------------------


def test_workflow_session_save_failure_rolls_back_revision(
    tmp_path, defaults_path, monkeypatch
):
    store = tmp_path / "automatic" / "current_project.json"
    service = WorkflowService(store, defaults_path)
    service.set_project({"name": "已保存版本"})
    before_revision = service.state["revision"]
    before_project_revision = service.state["project"]["revision"]
    saved_bytes = store.read_bytes()
    real_replace = os.replace

    def always_busy(source, target):
        if Path(target) == store:
            raise _access_denied(target)
        return real_replace(source, target)

    _fast_backoff(monkeypatch)
    monkeypatch.setattr(atomic_io.os, "replace", always_busy)
    service.state["project"]["name"] = "未完成版本"

    with pytest.raises(ProjectSaveError) as info:
        service.save()

    assert service.state["revision"] == before_revision
    assert service.state["project"]["revision"] == before_project_revision
    assert store.read_bytes() == saved_bytes              # 磁盘仍是旧版本，没有半截 JSON
    assert "占用" in str(info.value)
    assert not list(store.parent.glob("*.tmp"))


# ---- 10. 重试预算有界且很小 ----------------------------------------------------


def test_retry_budget_is_bounded_and_small():
    assert atomic_io.REPLACE_ATTEMPTS == 5
    delays = atomic_io.REPLACE_BACKOFF_SECONDS
    assert len(delays) == atomic_io.REPLACE_ATTEMPTS - 1
    assert list(delays) == sorted(delays)          # 递增退避
    assert 0 < min(delays)
    assert sum(delays) <= 2.0                      # 有界：总退避不超过 2 秒，非无限重试


# ---- 11. Windows winerror 是排他判据 ------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows winerror 语义")
def test_non_whitelisted_winerror_is_not_transient(tmp_path, monkeypatch):
    """winerror 存在时它是唯一判据：非 5/32/33 不得因 errno=EACCES 被误判为瞬时。"""

    non_whitelisted = PermissionError(errno.EACCES, "Access is denied", None, 2)
    assert non_whitelisted.winerror == 2
    assert atomic_io.is_transient_replace_error(non_whitelisted) is False

    # 只有在完全没有 winerror 时才回落到 errno 白名单（POSIX 语义）。
    fallback = PermissionError(errno.EACCES, "Access is denied")
    assert getattr(fallback, "winerror", None) is None
    assert atomic_io.is_transient_replace_error(fallback) is True

    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    before = path.read_bytes()
    attempts = {"count": 0}
    real_replace = os.replace

    def odd_replace(source, target):
        if Path(target) == path:
            attempts["count"] += 1
            raise PermissionError(errno.EACCES, "Access is denied", str(target), 2)
        return real_replace(source, target)

    monkeypatch.setattr(atomic_io.os, "replace", odd_replace)

    with pytest.raises(OSError) as info:
        repository.save({"revision": 2})

    assert not isinstance(info.value, ProjectSaveError)   # 不伪装成 replace 占用
    assert attempts["count"] == 1                          # 不重试
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


# ---- 12. 非 replace 阶段的失败不得伪装成 replace 占用 --------------------------


def test_temp_write_failure_is_not_reported_as_replace_contention(tmp_path, monkeypatch):
    """temp 写入本身被拒时必须原样抛出：不是 replace 冲突，也不得进入替换重试。"""

    path, repository = _store(tmp_path)
    repository.save({"revision": 1})
    repository.save({"revision": 2})
    before = path.read_bytes()
    backup_before = repository.backup_path.read_bytes()
    replace_calls = {"count": 0}
    real_replace = os.replace

    def deny_write(self, temporary, content):
        # 即使是 Windows 的瞬时码（winerror 5），只要发生在 temp 写入阶段，
        # 就不等于"replace 被占用"，不得翻译成 ProjectSaveError。
        raise PermissionError(errno.EACCES, "Access is denied", str(temporary), 5)

    def counting_replace(source, target):
        replace_calls["count"] += 1
        return real_replace(source, target)

    monkeypatch.setattr(ProjectRepository, "_write_durable", deny_write)
    monkeypatch.setattr(atomic_io.os, "replace", counting_replace)

    with pytest.raises(PermissionError) as info:
        repository.save({"revision": 3})

    assert not isinstance(info.value, ProjectSaveError)          # 不伪装成 replace 占用
    assert replace_calls["count"] == 0                           # 未进入 atomic replace
    assert path.read_bytes() == before                           # 正式文件未被破坏
    assert repository.backup_path.read_bytes() == backup_before  # 备份轮换尚未发生
    assert not list(path.parent.glob("*.tmp"))
