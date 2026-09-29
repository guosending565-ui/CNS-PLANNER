"""真实 ``map_server`` QgsApplication 生命周期下的 300 DPI 出图 smoke test。

为什么需要它：其余制图测试要么用假渲染器，要么自己 new 一个 ``QgsApplication``。
真实服务（``cns_planner/api/server.py`` 由 ``map_app.py`` / ``app.py`` 启动）的生命周期是
**先初始化 QgsApplication 并 initQgis()，再在同一进程里创建渲染器并导出 300 DPI 版面**；
本轮就踩过一次"QgsApplication 只被局部变量持有 → 被 GC → 无 QGuiApplication 访问 Qt →
Qt fail-fast（0xC0000409）"的坑。因此这里必须按**同样的顺序与持有方式**跑一遍。

运行方式（需要 QGIS 解释器；只需标准库 + PyQGIS）：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tests/test_map_figures_qgis_smoke.py

退出码 0 = 通过；非 0 = 失败（并打印中文原因）。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 与真实服务同样的持有方式：**进程级全局**引用，绝不让它被 GC。
_QGIS_APPLICATION = None

#: 真实 canonical ``operational_routes`` 形态（``start`` / ``end`` 是坐标数组）。
DEMO_ROUTE = {
    "route_id": "SMOKE-R0001", "status": "passed", "path_crs": "OGC:CRS84",
    "kind": "layered_risk_aware_operational_route",
    "path": [[122.1067, 30.0167], [122.1800, 30.0400], [122.2450, 30.0200],
             [122.3100, 29.9750], [122.3900, 29.9600]],
    "start": [122.1067, 30.0167], "end": [122.3900, 29.9600],
    "start_node_id": "N0001", "end_node_id": "N0002",
    "provenance": {"source_type": "smoke_test_in_memory"},
}


def bootstrap_qgis():
    """按真实服务顺序初始化 QGIS：只 import QgsApplication → new → initQgis。"""

    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


class _Session:
    """最小 session：只读 state，绝不写盘（本 smoke 不碰项目状态）。"""

    def __init__(self, state, store_path):
        self.state = state
        self.store_path = store_path

    def save(self):  # pragma: no cover - 制图链路已不再调用 session.save()
        raise AssertionError("制图链路不得调用 session.save()")


def run_smoke(workspace_root=None, *, dpi=300.0):
    """返回 ``(ok, 中文结论, 事实字典)``；不抛异常（失败也如实返回）。"""

    root = Path(workspace_root or Path(__file__).resolve().parents[1]).resolve()
    sys.path.insert(0, str(root))
    application = bootstrap_qgis()
    facts = {"qgis_prefix": application.prefixPath(), "dpi": float(dpi)}

    from cns_planner.application.map_figure_service import MapFigureService
    from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

    # 组合根顺序：先建渲染器（会 choose_font_family → QFontDatabase），再装配服务。
    renderer = QgisFigureRenderer()
    facts["font_family"] = renderer.font_family
    state = {"revision": 11, "operational_routes": [copy.deepcopy(DEMO_ROUTE)],
             "source_audits": {"items": {}}, "towers": {"count": 0, "items": []}}
    service = MapFigureService(
        _Session(state, root / "projects" / "current_project.json"),
        lambda: {}, lambda action: action(), renderer_factory=lambda: renderer,
        project_directory=root / "_diag" / "smoke_project",
    )
    spec = service.build_figure(template_id="route_overview_v1", route_id="SMOKE-R0001")
    facts["extent"] = spec.extent.as_list()
    facts["layer_count"] = len(spec.layers)
    facts["tower_count"] = next(
        (layer.feature_count for layer in spec.layers if layer.layer_key == "tower_existing"), 0,
    )

    image = service._render(spec, dpi=dpi)  # noqa: SLF001 - smoke 刻意走真实渲染入口
    facts["bytes"] = len(image)
    if not bytes(image).startswith(b"\x89PNG"):
        return False, "300 DPI 渲染没有返回有效 PNG", facts
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(image)) as opened:
            facts["pixels"] = [opened.width, opened.height]
    except ImportError:  # pragma: no cover - QGIS 环境自带 Pillow
        facts["pixels"] = None

    width_mm = float(spec.layout["document_width_mm"])
    height_mm = float(spec.layout["document_height_mm"])
    expected = (width_mm / 25.4 * dpi, height_mm / 25.4 * dpi)
    facts["expected_pixels"] = [round(expected[0]), round(expected[1])]
    if facts["pixels"]:
        drift = max(abs(facts["pixels"][0] - expected[0]) / expected[0],
                    abs(facts["pixels"][1] - expected[1]) / expected[1])
        facts["pixel_drift"] = round(drift, 4)
        if drift > 0.05:
            return False, f"像素尺寸与版面不符（偏差 {drift:.1%}）", facts
    # 渲染完成后 QgsApplication 必须仍然存活：这正是上一轮 fail-fast 的触发点。
    if _QGIS_APPLICATION is None or _QGIS_APPLICATION.instance() is None:
        return False, "渲染后 QgsApplication 已丢失（会导致后续 Qt 访问崩溃）", facts
    return True, "300 DPI 在真实 QgsApplication 生命周期下出图成功", facts


def main():
    ok, message, facts = run_smoke()
    print(json.dumps({"ok": ok, "message": message, "facts": facts},
                     ensure_ascii=False, indent=2), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
