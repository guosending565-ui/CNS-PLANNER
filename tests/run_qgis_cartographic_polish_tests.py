"""在 QGIS 解释器里运行 Round30-B1 的真机量化测试（QGIS 自带 Python 没有 pytest）。

为什么需要它：B1 的两条核心修复只能在真实 QGIS 版面上验证 ——

1. **覆盖圈在纸面上是正圆**（通信 4 km / RID 2 km / RID 5 km 的像素宽高比 ≈ 1，
   且实测直径仍对应 4000 / 2000 / 5000 m）；
2. **地图画布 = EPSG:32651、经纬网 = EPSG:4326**，WGS84 几何确实被投影到米制画布。

运行：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' \\
        tests/run_qgis_cartographic_polish_tests.py

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

MODULE = "test_map_figures_cartographic_polish_qgis"

#: 需要参数化的用例（``(参数...)`` 由 parametrize 给出）。
PARAMETERIZED = (
    ("test_coverage_circle_is_visually_round_and_keeps_its_true_radius",
     (("communication", "#1a9c4a", 4000.0),
      ("rid_land", "#e8720c", 2000.0),
      ("rid_sea", "#e8720c", 5000.0))),
)

#: 无参用例。
PLAIN = (
    "test_ellipse_regression_would_be_detected",
    "test_map_canvas_is_metric_and_grid_stays_wgs84",
    "test_wgs84_geometry_is_really_projected_onto_the_metric_canvas",
    "test_leader_line_is_really_rendered_when_the_card_is_far_from_the_anchor",
    "test_label_background_cards_are_really_drawn_with_the_official_colours",
    "test_label_background_cards_are_inside_the_map_frame",
    "test_legend_title_item_spans_the_box_and_is_horizontally_centred",
    "test_legend_title_ink_is_horizontally_centred_on_the_page",
    "test_five_figure_shared_layout_renders_without_annotation_boxes",
)


def _install_pytest_stub():
    """QGIS 解释器没有 pytest：装一个**最小**替身，只为让测试模块可以 import。

    只实现本文件真正用到的 API（``importorskip`` / ``skip`` / ``approx`` /
    ``mark.parametrize``）；一旦被测函数调用到其它 pytest 能力，这里会显式抛错。
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

    class _Approx:
        def __init__(self, value, abs=1e-12, rel=1e-6):  # noqa: A002 - 与 pytest 同名
            self.value = value
            self.abs = abs
            self.rel = rel

        def __eq__(self, other):
            return abs(float(other) - float(self.value)) <= max(
                float(self.abs), abs(float(self.value)) * float(self.rel),
            )

        def __repr__(self):  # pragma: no cover
            return f"approx({self.value})"

    class _Mark:
        @staticmethod
        def parametrize(*_args, **_kwargs):
            def decorator(function):
                function._parametrize = (_args, _kwargs)  # noqa: SLF001
                return function

            return decorator

    stub = types.ModuleType("pytest")
    stub.importorskip = importorskip
    stub.skip = skip
    stub.mark = _Mark()
    stub.approx = lambda value, abs=1e-12, rel=1e-6: _Approx(value, abs=abs, rel=rel)
    stub._Skipped = _Skipped
    sys.modules["pytest"] = stub


_install_pytest_stub()


def main():
    module = importlib.import_module(MODULE)
    passed, failed = [], []
    for function_name, cases in PARAMETERIZED:
        function = getattr(module, function_name, None)
        if function is None:
            failed.append((function_name, "测试函数不存在"))
            continue
        for case in cases:
            label = f"{function_name}[{case[0]}]"
            try:
                function(*case)
                passed.append(label)
                print(f"[PASS] {label}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 如实报告所有失败
                failed.append((label, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}", flush=True)
                traceback.print_exc()
    for function_name in PLAIN:
        function = getattr(module, function_name, None)
        if function is None:
            failed.append((function_name, "测试函数不存在"))
            continue
        try:
            function()
            passed.append(function_name)
            print(f"[PASS] {function_name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed.append((function_name, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] {function_name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
    print(
        f"\nRound30-B1 真机量化测试：{len(passed)} PASS / {len(failed)} FAIL", flush=True,
    )
    for name, reason in failed:
        print(f"  - {name}: {reason}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
