"""在 QGIS 解释器里运行专题成果图 smoke 验证（无需 pytest）。

QGIS 自带 Python 环境通常没有 pytest，因此真正的出图验证在这里独立执行：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' tools/verify_map_figures_qgis.py

退出码 0 = 全部通过；非 0 = 有失败项（逐项打印中文结论）。
"""

from __future__ import annotations

import io
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.getcwd())

RESULT = {"passed": 0, "failed": []}


def check(name, condition, detail=""):
    if condition:
        RESULT["passed"] += 1
        print(f"[PASS] {name} {detail}", flush=True)
    else:
        RESULT["failed"].append(name)
        print(f"[FAIL] {name} {detail}", flush=True)


def main():
    from qgis.core import QgsApplication

    app = QgsApplication([], False)
    app.initQgis()

    from PIL import Image

    from cns_planner.gis import figure_style as style
    from cns_planner.gis.figure_spec import (
        ExtentSpec, FigureSpec, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON,
        LabelSpec, LayerSpec, LegendItem,
    )
    from cns_planner.gis.qgis_figure_renderer import (
        FigureRenderError, QgisFigureRenderer, choose_font_family, ensure_fonts_registered,
    )

    layout = {
        "document_width_mm": 210.0, "document_height_mm": 297.0, "margin_mm": 10.0,
        "title_band_mm": 12.0, "subtitle_band_mm": 6.0, "title_top_mm": 10.0,
        "map_top_mm": 28.0, "map_left_mm": 10.0, "map_width_mm": 190.0,
        "map_height_mm": 193.0, "legend_top_mm": 226.0, "legend_left_mm": 10.0,
        "legend_width_mm": 190.0, "legend_height_mm": 55.0, "legend_columns": 1,
        "legend_rows_per_column": 8, "legend_row_mm": 6.2, "legend_group_row_mm": 6.4,
        "legend_symbol_box_mm": 6.0, "scale_bar_margin_mm": 3.0,
        "north_arrow_size_mm": 16.0, "fixed_layout": "map_above_legend_below",
    }

    def make_spec(with_layers=True):
        spec = FigureSpec(
            template_id="route_overview_v1", template_version=1,
            title="航路周边总览图（QGIS VERIFY）", route_id="VERIFY-1",
            route_source="operational_routes",
            route_geometry=[[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]],
            route_start=[122.05, 29.95], route_end=[122.35, 29.98],
            extent=ExtentSpec(west=121.90, south=29.85, east=122.50, north=30.12,
                              width_km=58.0, height_km=30.0),
            extent_mode="verify", generated_from_revision=3, layout=layout,
            labels=[
                LabelSpec(kind="start", text="起点甲", longitude=122.05, latitude=29.95,
                          priority=100, style_key="label_endpoint"),
                LabelSpec(kind="end", text="终点乙", longitude=122.35, latitude=29.98,
                          priority=100, style_key="label_endpoint"),
            ],
            legend_items=[
                LegendItem("land", "land", "陆地区域", "environment"),
                LegendItem("planned_route", "planned_route", "规划航路", "route"),
                LegendItem("turn_point", "turn_point", "航路转弯点", "route"),
                LegendItem("start_point", "start_point", "起点", "route"),
                LegendItem("end_point", "end_point", "终点", "route"),
            ],
        )
        if with_layers:
            spec.layers = [
                LayerSpec("sea", "海域", "sea", GEOMETRY_POLYGON, "land_mask",
                          source_status="available", feature_count=1, data={}),
                LayerSpec("land", "陆地区域", "land", GEOMETRY_POLYGON, "land_mask",
                          source_status="available", feature_count=1,
                          data={"polygons": [[[121.95, 29.87], [122.42, 29.87],
                                              [122.42, 30.10], [121.95, 30.10],
                                              [121.95, 29.87]]]}),
                LayerSpec("planned_route", "规划航路", "planned_route", GEOMETRY_LINE,
                          "operational_routes", source_status="available", feature_count=1,
                          data={"geometry": [[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]]}),
                LayerSpec("turn_point", "航路转弯点", "turn_point", GEOMETRY_POINT,
                          "operational_routes", source_status="available", feature_count=1,
                          data={"points": [{"longitude": 122.20, "latitude": 30.01}]}),
                LayerSpec("start_point", "起点", "start_point", GEOMETRY_POINT,
                          "operational_routes", source_status="available", feature_count=1,
                          data={"points": [{"longitude": 122.05, "latitude": 29.95}]}),
                LayerSpec("end_point", "终点", "end_point", GEOMETRY_POINT,
                          "operational_routes", source_status="available", feature_count=1,
                          data={"points": [{"longitude": 122.35, "latitude": 29.98}]}),
            ]
        return spec

    renderer = QgisFigureRenderer()
    ensure_fonts_registered(renderer)
    check("中文字体已解析", choose_font_family() in style.FONT_CANDIDATES,
          f"family={choose_font_family()}")
    check("中文字体已注册", renderer._font_files_registered is True)

    payload = renderer.render(make_spec(), dpi=100.0)
    check("PNG 文件头有效", payload.startswith(b"\x89PNG"), f"{len(payload)} bytes")
    image = Image.open(io.BytesIO(payload))
    expected_w, expected_h = 210.0 / 25.4 * 100.0, 297.0 / 25.4 * 100.0
    check("像素尺寸与版面 × DPI 一致（容差 5%）",
          abs(image.width - expected_w) / expected_w < 0.05
          and abs(image.height - expected_h) / expected_h < 0.05,
          f"{image.width}x{image.height} 期望约 {expected_w:.0f}x{expected_h:.0f}")

    small = Image.open(io.BytesIO(renderer.render(make_spec(), dpi=72.0))).convert("RGB")
    palette = {color: count for count, color in (small.getcolors(maxcolors=1_000_000) or [])}

    def rgb(text):
        value = text.lstrip("#")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

    check("海域底色已绘制", palette.get(rgb("#cfe6f5"), 0) > 200,
          f"{palette.get(rgb('#cfe6f5'), 0)} px")
    # 陆地填充现在是白色（与海区分靠深灰蓝边界），因此断言**边界颜色**出现：
    # 这证明陆地图层真的被渲染，而不是被底色吞掉。
    check("陆地区域已绘制（边界颜色出现）", palette.get(rgb("#4c5a68"), 0) > 20,
          f"{palette.get(rgb('#4c5a68'), 0)} px")
    check("规划航路已绘制（最高视觉层）", palette.get(rgb("#123a6b"), 0) > 20,
          f"{palette.get(rgb('#123a6b'), 0)} px")

    try:
        renderer.render(make_spec(with_layers=False), dpi=72.0)
        check("无可绘制图层时明确失败", False, "未抛出 FigureRenderError")
    except FigureRenderError as exc:
        check("无可绘制图层时明确失败", "没有任何可绘制的图层" in str(exc), str(exc))

    for style_key in style.FIGURE_STYLES:
        preview = style.symbol_preview_image(style_key)
        check(f"图例符号非空：{style_key}",
              preview.startswith(b"\x89PNG") and len(preview) > 200, f"{len(preview)} bytes")

    print("", flush=True)
    if RESULT["failed"]:
        print(f"QGIS 验证失败项：{RESULT['failed']}", flush=True)
        print(f"通过 {RESULT['passed']} 项，失败 {len(RESULT['failed'])} 项", flush=True)
        sys.stdout.flush()
        os._exit(1)
    print(f"QGIS 验证全部通过（{RESULT['passed']} 项）", flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
