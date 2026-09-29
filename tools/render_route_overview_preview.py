"""生成一张 route_overview_v1 预览图与 300 DPI 正式图（真实 QGIS 版面渲染）。

用法（必须用 QGIS 自带解释器）：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tools/render_route_overview_preview.py

语义边界（务必注意）：

* 图层数据全部来自项目已配置的**真实**数据源（陆域掩膜 / FABDEM / 建筑网格 / 铁塔清单）；
* 图中的航路是**演示航路**：当项目没有 canonical ``operational_routes`` 时，本工具不会
  伪造项目状态，只在内存里构造一条明确标注 ``demo_route_for_cartography_preview_only``
  的折线，因此产出的图**不是**项目正式成果图；
* 本工具不写任何项目状态、不调用任何业务算法；正式成果图请走 Step06「专题成果图」入口
  或 ``POST /api/map-figures/export``。

产出（默认写入 ``outputs/``）：

* ``map_figure_route_overview_v1_preview.png``（预览 DPI）
* ``map_figure_route_overview_v1_300dpi.png``（300 DPI 正式输出）
* ``map_figure_route_overview_v1_spec.json``（本次 FigureSpec，便于人工审查）
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 舟山本岛—普陀山一带的演示航路（仅用于展示制图效果，不是项目航路）。
DEMO_ROUTE = [
    [122.1067, 30.0167],
    [122.1800, 30.0400],
    [122.2450, 30.0200],
    [122.3100, 29.9750],
    [122.3900, 29.9600],
]


#: 进程级持有 QgsApplication：局部变量被回收会让 Qt 在后续字体访问时崩溃。
_QGIS_APPLICATION = None


def _require_qgis():
    """创建并**持有** QgsApplication（headless），返回该实例。"""

    global _QGIS_APPLICATION
    try:
        from qgis.core import QgsApplication
    except ImportError as exc:  # pragma: no cover - 需要 QGIS 解释器
        raise SystemExit(
            "本工具需要 PyQGIS；请用 QGIS 自带解释器运行，例如：\n"
            "  \"C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat\" tools/render_route_overview_preview.py"
        ) from exc
    application = QgsApplication.instance()
    if application is None:
        application = QgsApplication([], False)
        application.initQgis()
    _QGIS_APPLICATION = application
    return application


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="生成 route_overview_v1 演示预览与 300 DPI 图件")
    parser.add_argument("--project", default="projects/current_project.json",
                        help="项目状态文件（只读）")
    parser.add_argument("--sources", default="projects/map_sources.json",
                        help="数据源配置文件（只读）")
    parser.add_argument("--output-dir", default="outputs", help="产物目录")
    parser.add_argument("--preview-dpi", type=float, default=130.0)
    parser.add_argument("--export-dpi", type=float, default=300.0)
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(argv)
    _require_qgis()
    sys.path.insert(0, os.getcwd())
    # 应用实例必须一直被持有，否则 Qt 在后续字体/版面访问时会崩溃。
    _ = _QGIS_APPLICATION

    from cns_planner.application.map_figure_service import (
        MaterializationContext, _expand_extent, _layout_plan, _legend_layout_entries,
        materialize,
    )
    from cns_planner.gis.figure_spec import FigureSpec
    from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer
    from cns_planner.reporting.map_templates import parameters as template_parameters

    state = json.loads(Path(arguments.project).read_text(encoding="utf-8"))
    sources = json.loads(Path(arguments.sources).read_text(encoding="utf-8"))
    paths = sources.get("paths", sources)
    parameters = template_parameters("route_overview_v1")
    route = {
        "route_id": "DEMO-舟山演示航路", "path": DEMO_ROUTE, "path_crs": "OGC:CRS84",
        "kind": "demo_route_for_cartography_preview_only", "status": "demo",
        "start": {"name": "舟山本岛（演示）"}, "end": {"name": "普陀山以东（演示）"},
        "provenance": {"source_type": "demo_route_in_memory", "not_project_state": True},
    }
    context = MaterializationContext(
        state=state, paths=paths, route=route, extent=None, parameters=parameters,
        project_revision=int(state.get("revision") or 0),
        source_audits=state.get("source_audits") or {},
    )

    def build():
        layout = _layout_plan(parameters)
        aspect = layout["map_width_mm"] / layout["map_height_mm"]
        extent, evidence = _expand_extent(
            DEMO_ROUTE, buffer_km=parameters["extent_buffer_km"],
            max_padding_km=parameters["extent_max_padding_km"], aspect=aspect,
            metric_crs=parameters["extent_source_crs"],
        )
        context.extent = extent
        probe = materialize(context)
        layout = _layout_plan(
            parameters, legend_items=_legend_layout_entries(probe.legend_items),
        )
        aspect = layout["map_width_mm"] / layout["map_height_mm"]
        extent, evidence = _expand_extent(
            DEMO_ROUTE, buffer_km=parameters["extent_buffer_km"],
            max_padding_km=parameters["extent_max_padding_km"], aspect=aspect,
            metric_crs=parameters["extent_source_crs"],
        )
        context.extent = extent
        result = materialize(context)
        return layout, extent, evidence, result

    print("装配专题图层（读取真实数据源）…", flush=True)
    layout, extent, evidence, result = build()
    for layer in result.layers:
        print(f"  {layer.layer_key:20s} {layer.source_status:12s} "
              f"n={layer.feature_count:6d} {layer.source_reason or ''}", flush=True)
    spec = FigureSpec(
        template_id="route_overview_v1", template_version=1,
        title="航路周边状况图",
        route_id=route["route_id"], route_source="operational_routes",
        route_geometry=DEMO_ROUTE,
        route_start={"name": route["start"]["name"], "coordinate": DEMO_ROUTE[0]},
        route_end={"name": route["end"]["name"], "coordinate": DEMO_ROUTE[-1]},
        turn_points=DEMO_ROUTE[1:-1], extent=extent,
        extent_mode="route_bbox_plus_configurable_buffer_metric",
        extent_evidence=evidence, layers=result.layers, labels=result.labels,
        legend_items=result.legend_items, source_status=result.source_status,
        omitted_layers=result.omitted_layers,
        generated_from_revision=int(state.get("revision") or 0),
        display_thresholds={
            "terrain_threshold_m": parameters["terrain_threshold_m"],
            "building_threshold_m": parameters["building_threshold_m"],
            "semantics": "figure_display_threshold_only",
            "affects_planning_constraint_field": False,
            "affects_route_safety_decision": False,
            "is_business_gate": False,
        },
        layout=layout,
        # 副标题只显示简短航路名（演示航路如实标注 DEMO）。
        parameters={**parameters, "route_display_name": "舟山本岛—普陀山（DEMO 演示航路）"},
        warnings=list(result.warnings),
    )
    renderer = QgisFigureRenderer()
    output_dir = Path(arguments.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for label, dpi, filename in (
        ("预览", arguments.preview_dpi, "map_figure_route_overview_v1_preview.png"),
        ("300 DPI 正式输出", arguments.export_dpi, "map_figure_route_overview_v1_300dpi.png"),
    ):
        print(f"渲染{label}（{dpi:g} DPI）…", flush=True)
        payload = renderer.render(spec, dpi=dpi)
        target = output_dir / filename
        target.write_bytes(payload)
        print(f"  {target} ({len(payload)} bytes)", flush=True)
    spec_path = output_dir / "map_figure_route_overview_v1_spec.json"
    spec_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"FigureSpec: {spec_path}", flush=True)
    print("RENDER_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
