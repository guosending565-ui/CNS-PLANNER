"""在 QGIS 解释器里运行需要 PyQGIS 的那几条定向测试（无需 pytest）。

为什么需要它：QGIS 自带的 Python 没有装 pytest，但有一批断言必须在真实 PyQGIS 下才能
验证（MapData 启动、带洞几何进渲染器、300 DPI 生命周期 smoke）。本脚本用一段最小
"测试驱动"（收集 + 运行 + 统计）把它们跑起来，参数由 ``unittest.mock.patch`` 提供。

运行：
    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tests/run_qgis_map_figure_tests.py
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def _install_pytest_stub():
    """QGIS 解释器没有 pytest：装一个**最小**替身，只为让测试模块可以 import。

    只实现本文件真正用到的两个 API（``importorskip`` / ``skip``）；一旦被测函数调用到
    其它 pytest 能力，这里会显式抛错而不是静默通过。
    """

    if "pytest" in sys.modules:
        return
    import importlib.util
    import types

    if importlib.util.find_spec("pytest") is not None:
        return

    class _Skipped(Exception):
        pass

    def importorskip(name, reason=""):
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            raise _Skipped(f"importorskip({name}) 失败：{reason or exc}") from exc

    def skip(reason=""):
        raise _Skipped(reason or "skipped")

    stub = types.ModuleType("pytest")
    stub.importorskip = importorskip
    stub.skip = skip
    stub._Skipped = _Skipped
    sys.modules["pytest"] = stub


_install_pytest_stub()

#: 只跑这三条：其余制图测试在普通解释器里已经覆盖。
TARGETS = (
    "test_map_figures_review_fixes:test_mapdata_startup_survives_missing_cartographic_land",
    "test_map_figures_review_fixes:test_land_polygon_holes_reach_the_renderer_geometry",
    "test_map_figures_review_fixes:test_300dpi_smoke_in_real_qgis_lifecycle",
)


def main():
    import importlib
    import shutil

    module = importlib.import_module("test_map_figures_review_fixes")
    # 临时目录放在工作区内的 ``projects/`` 下：受限沙箱下 QGIS 解释器可能无法写系统
    # 临时目录或 ``_diag``，而项目目录是它一直在读写的区域。
    base = ROOT / "projects" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    passed, failed = [], []
    for target in TARGETS:
        _, function_name = target.split(":", 1)
        function = getattr(module, function_name, None)
        if function is None:
            failed.append((function_name, "测试函数不存在"))
            continue
        directory = tempfile.mkdtemp(prefix="mf-qgis-", dir=str(base))
        tmp_path = Path(directory)
        try:
            if function.__code__.co_argcount == 0:
                function()
            else:
                function(tmp_path)
            passed.append(function_name)
            print(f"[PASS] {function_name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 这里就是要如实报告所有失败
            failed.append((function_name, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] {function_name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    print(f"\nQGIS 定向测试：{len(passed)} PASS / {len(failed)} FAIL", flush=True)
    for name, reason in failed:
        print(f"  - {name}: {reason}", flush=True)
    return 0 if not failed else 1


class _Noop:
    """``monkeypatch`` 的最小替身（只需要 setattr）。"""

    def setattr(self, *args, **kwargs):
        from unittest.mock import patch

        patcher = patch.object(*args, **kwargs)
        patcher.start()
        return patcher


if __name__ == "__main__":
    sys.exit(main())
