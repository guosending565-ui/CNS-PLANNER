"""只读：验证 cartographic_land 缺失时的启动与出图降级行为（第 6 条 A–E）。

不改任何文件：通过构造一份"cartographic_land 指向不存在路径"的 paths 副本，
按生产组合根的方式组装 ApplicationContext 与 MapFigureService，检查：

A. 代码即使没有本地派生文件也能正常启动（不抛异常）；
B. cartographic_land 状态明确为 unavailable / not_configured；
C. 不因缺文件导致整体启动失败；
D. derive 工具可重新生成（由工具自身保证，这里只检查配置里没有硬依赖）；
E. 生成路径不依赖开发者绝对路径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_QGIS_APPLICATION = None


def qgis():
    global _QGIS_APPLICATION
    from qgis.core import QgsApplication

    _QGIS_APPLICATION = QgsApplication.instance() or QgsApplication([], False)
    _QGIS_APPLICATION.initQgis()
    return _QGIS_APPLICATION


DEMO_ROUTE = [
    [122.1067, 30.0167], [122.1800, 30.0400], [122.2450, 30.0200],
    [122.3100, 29.9750], [122.3900, 29.9600],
]


def main():
    qgis()
    sys.path.insert(0, os.getcwd())

    from cns_planner.application.map_figure_service import MapFigureService

    state = json.loads(Path("projects/current_project.json").read_text(encoding="utf-8"))
    sources = json.loads(Path("projects/map_sources.json").read_text(encoding="utf-8"))
    paths = dict(sources.get("paths", sources))

    checks = []

    # ---- 场景 1：配置指向不存在的文件（模拟 clean clone 没有派生数据） ----
    missing = dict(paths)
    missing["cartographic_land"] = "projects/derived/cartographic_land/does_not_exist.gpkg"
    service = MapFigureService(_Session(state), lambda: missing, lambda action: action())
    service.session.state = _state_with_demo_route(state)
    spec = service.build_figure(template_id="route_overview_v1")
    layers = {layer.layer_key: layer for layer in spec.layers}
    land, sea = layers["land"], layers["sea"]
    checks.append(("A 配置了不存在的陆地面时仍能构建 FigureSpec", True, "build_figure 未抛异常"))
    checks.append(("B 陆地层状态为 unavailable",
                   land.source_status == "unavailable", land.source_status))
    checks.append(("B 海域层状态为 unavailable",
                   sea.source_status == "unavailable", sea.source_status))
    checks.append(("B 原因写明文件缺失",
                   "不存在" in (land.source_reason or ""), land.source_reason))
    checks.append(("B 状态写进 omitted_layers",
                   {"land", "sea"} <= {item["layer_key"] for item in spec.omitted_layers},
                   [item["layer_key"] for item in spec.omitted_layers]))
    checks.append(("C 航路层仍然可用（未因缺底图整体失败）",
                   spec.source_status["planned_route"]["status"] == "available",
                   spec.source_status["planned_route"]["status"]))
    checks.append(("C 图例不含缺失的陆海图层",
                   not ({"land", "sea"} & {item.layer_key for item in spec.legend_items}),
                   [item.layer_key for item in spec.legend_items]))

    # ---- 场景 2：完全没有配置 cartographic_land（clean clone 默认） ----
    unconfigured = dict(paths)
    unconfigured.pop("cartographic_land", None)
    service2 = MapFigureService(_Session(state), lambda: unconfigured,
                                lambda action: action())
    service2.session.state = _state_with_demo_route(state)
    spec2 = service2.build_figure(template_id="route_overview_v1")
    layers2 = {layer.layer_key: layer for layer in spec2.layers}
    checks.append(("B 未配置时为 unknown",
                   layers2["land"].source_status == "unknown",
                   layers2["land"].source_status))
    checks.append(("B 未配置时原因写明未配置",
                   "未配置" in (layers2["land"].source_reason or ""),
                   layers2["land"].source_reason))
    checks.append(("C 未配置时依然能出图（航路可用）",
                   spec2.source_status["planned_route"]["status"] == "available",
                   spec2.source_status["planned_route"]["status"]))

    # ---- 场景 3：MapData 启动（组合根路径解析）不因缺文件失败 ----
    from cns_planner.gis.map_data import MapData

    data = MapData(settings_path=Path("projects/map_sources.json"))
    checks.append(("A MapData 启动未报错（error 为空或仅路径缺失提示）",
                   not data.error, repr(data.error)[:120]))
    role = data.vector_role_source("cartographic_land")
    checks.append(("B MapData 能解析 cartographic_land 角色",
                   isinstance(role, dict) and "ok" in role,
                   {key: role.get(key) for key in ("ok", "status", "resolved_path")}))

    # ---- 场景 4：代码里不得出现开发者绝对路径 ----
    hardcoded = []
    for target in ("cns_planner/application/map_figure_service.py",
                   "cns_planner/gis/qgis_figure_renderer.py",
                   "cns_planner/gis/figure_spec.py",
                   "cns_planner/gis/figure_style.py",
                   "cns_planner/reporting/map_templates/template_catalog.py"):
        text = Path(target).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if "aaa2026project" in line or "/Users/yiding" in line:
                hardcoded.append(f"{target}:{number}")
    checks.append(("E 制图代码中没有开发者绝对路径", not hardcoded, hardcoded or "无"))

    passed = 0
    print("=== cartographic_land 缺失时的降级行为 ===", flush=True)
    for label, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  ({detail})", flush=True)
        passed += 1 if ok else 0
    print(f"\n  {passed}/{len(checks)} PASS", flush=True)
    return 0 if passed == len(checks) else 4


class _Session:
    def __init__(self, state):
        self.state = state
        self.store_path = Path("projects/current_project.json")

    def save(self):  # pragma: no cover
        raise AssertionError("诊断脚本不得写项目状态")


def _state_with_demo_route(state):
    import copy

    result = copy.deepcopy(state)
    result["operational_routes"] = [{
        "route_id": "DEMO-降级诊断", "status": "demo", "path_crs": "OGC:CRS84",
        "kind": "diagnostic", "path": DEMO_ROUTE,
        "start": {"name": "起点"}, "end": {"name": "终点"},
        "provenance": {"source_type": "diagnostic_in_memory", "not_project_state": True},
    }]
    return result


if __name__ == "__main__":
    sys.exit(main())
