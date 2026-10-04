"""在 QGIS 解释器里运行 Round30-B1.2 的真机量化测试（QGIS 自带 Python 没有 pytest）。

本轮真机验收的三条（都必须是**导出像素 / 版面几何**上的证据，而不是代码阅读）：

1. 引线：真机画出的每条引线长度 ≤ 15 mm，且在 300 DPI 像素上确实是一根细线；
2. 同址 endpoint + navigation：浅黄 service tag 底纹出现在起终点主卡内，且全部落在图框内；
3. 经纬度与比例尺：经度标注在**地图框外**下侧，比例尺在框内且与它至少留 1 mm 净空。

运行：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' \\
        tests/run_qgis_final_tuning_tests.py

退出码 0 = 全通过；非 0 = 有失败（并打印中文原因）。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

MODULE = "test_map_figures_final_tuning_qgis"

#: 本模块全部用例（都无参）。
TARGETS = (
    "test_rendered_leaders_never_exceed_the_length_budget",
    "test_leader_is_a_thin_line_on_the_exported_pixels",
    "test_leader_geometry_is_recorded_for_every_drawn_leader",
    "test_merged_endpoint_card_shows_the_navigation_service_tag",
    "test_merged_card_is_drawn_inside_the_map_frame",
    "test_longitude_labels_are_outside_the_frame_and_separated_from_the_scale_bar",
)


def _install_pytest_stub():
    """QGIS 解释器没有 pytest：装一个**最小**替身，只为让测试模块可以 import。"""

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


def main():
    module = importlib.import_module(MODULE)
    passed, failed = [], []
    for function_name in TARGETS:
        function = getattr(module, function_name, None)
        if function is None:
            failed.append((function_name, "测试函数不存在"))
            continue
        try:
            function()
            passed.append(function_name)
            print(f"[PASS] {function_name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 如实报告所有失败
            failed.append((function_name, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] {function_name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
    print(
        f"\nRound30-B1.2 真机量化测试：{len(passed)} PASS / {len(failed)} FAIL", flush=True,
    )
    for name, reason in failed:
        print(f"  - {name}: {reason}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
