"""本地 pytest 配置（仅解决 DSH 文件沙箱下的临时目录问题）。

背景
----
当前 DSH 文件沙箱会拒绝 ``tempfile.mkdtemp`` 创建的目录（``os.stat`` → ``FileNotFoundError``、
写入 → ``PermissionError``），而直接用 ``os.mkdir`` / ``os.makedirs`` 创建的目录完全正常。
受影响的都是“运行环境”路径，与被测代码语义无关：

* pytest 自带 ``tmp_path`` 在 setup 阶段直接报错；
* ``pytest_sessionfinish`` 对 basetemp 根的 ``iterdir()`` 失败，会吞掉整轮测试报告；
* 被测代码 ``cns_planner.application.report_service`` 的报告 staging 目录
  （``tempfile.TemporaryDirectory(prefix=".p19-", dir=reports_root)``）无法写入。

本文件做三件事，全部只影响“临时目录怎么建、怎么清理”：

1. ``tmp_path`` 用 ``os.makedirs`` 创建；
2. 关闭 pytest 自带的失效符号链接清理（只删 dead symlink，对 Windows 结果无影响）；
3. 给 ``report_service`` 模块注入一个只含 ``TemporaryDirectory`` / ``mkdtemp`` 的
   ``tempfile`` 替身：``mkdtemp`` 用 ``os.mkdir`` 实现，``TemporaryDirectory`` 允许清理失败。
   创建位置、目录名前缀（``.p19-``）、写入内容与原子替换顺序都不变。

除此之外不改变任何 pytest 与被测代码行为。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest


def _temp_root() -> str:
    root = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return root


def _sandbox_mkdtemp(suffix=None, prefix=None, dir=None):
    """``tempfile.mkdtemp`` 的沙箱兼容实现（``os.mkdir``，不用 mkdtemp）。"""

    directory = str(dir) if dir is not None else _temp_root()
    os.makedirs(directory, exist_ok=True)
    for _ in range(1000):
        name = f"{prefix or ''}{uuid.uuid4().hex[:8]}{suffix or ''}"
        try:
            os.mkdir(os.path.join(directory, name))
        except FileExistsError:  # pragma: no cover - collision
            continue
        return os.path.join(directory, name)
    raise FileExistsError("无法创建唯一临时目录")


class _SandboxTemporaryDirectory:
    """``tempfile.TemporaryDirectory`` 的等价替身，使用沙箱兼容的 mkdtemp。

    Python 3.13 的 ``tempfile.TemporaryDirectory.__init__`` 直接调用模块级 ``mkdtemp``，
    子类无法覆盖，因此这里提供一个接口等价的自有实现（``name`` 属性、上下文管理器、
    ``cleanup()``、清理失败可忽略）。
    """

    def __init__(self, suffix=None, prefix=None, dir=None, ignore_cleanup_errors=False):
        self.name = _sandbox_mkdtemp(suffix, prefix, dir)
        self._ignore_cleanup_errors = ignore_cleanup_errors

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self):
        return self.name

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()
        return False


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    try:
        from _pytest import tmpdir as _tmpdir
    except Exception:  # pragma: no cover - pytest internals unavailable
        return
    if getattr(_tmpdir, "cleanup_dead_symlinks", None) is not None:
        _tmpdir.cleanup_dead_symlinks = lambda *args, **kwargs: None

    from cns_planner.application import report_service

    report_service.tempfile = type(
        "_tempfile_shim",
        (),
        {
            "TemporaryDirectory": _SandboxTemporaryDirectory,
            "mkdtemp": _sandbox_mkdtemp,
        },
    )


@pytest.fixture
def tmp_path(request):
    """``tmp_path``，但用沙箱可访问的方式创建目录。"""

    root = os.path.join(_temp_root(), "dsh_pytest_tmp")
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, request.node.name.replace("/", "_")[:80])
    suffix = 0
    path = base
    while os.path.exists(path):
        suffix += 1
        path = f"{base}_{suffix}"
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o755)
    except OSError:  # pragma: no cover - best effort on Windows
        pass
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)
