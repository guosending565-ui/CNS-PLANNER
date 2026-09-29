"""专题成果图真实 QGIS 出图 smoke test（需要 PyQGIS + QGIS Python 解释器）。

本文件只在 QGIS 解释器里真实执行（``python-qgis-ltr.bat``）；在普通解释器下整体跳过：

    & 'C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat' -m pytest tests/test_map_figures_qgis_render.py -q

断言的都是**可观测**事实：

* PNG 有效（文件头 + Pillow 可解码）；
* 像素尺寸与版面毫米 × DPI 一致（容差 5%）；
* 地图区域真的画出了海域底色（不是空白页）；
* 图例符号渲染结果非空；
* 没有任何可绘制图层时**明确失败**，而不是产出一张空白图。
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("qgis", reason="需要 PyQGIS（用 python-qgis-ltr.bat 运行本文件）")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cns_planner.gis.figure_spec import (  # noqa: E402
    ExtentSpec, FigureSpec, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON, LabelSpec,
    LayerSpec, LegendItem,
)
from cns_planner.gis import figure_style as figure_style_module  # noqa: E402


def _qgis_app():
    from qgis.core import QgsApplication

    app = QgsApplication.instance() or QgsApplication([], False)
    app.initQgis()
    return app


def _layout_plan():
    return {
        "document_width_mm": 210.0, "document_height_mm": 297.0, "margin_mm": 10.0,
        "title_band_mm": 12.0, "subtitle_band_mm": 6.0, "title_top_mm": 10.0,
        "map_top_mm": 28.0, "map_left_mm": 10.0, "map_width_mm": 190.0,
        "map_height_mm": 193.0, "legend_top_mm": 226.0, "legend_left_mm": 10.0,
        "legend_width_mm": 190.0, "legend_height_mm": 55.0, "legend_columns": 1,
        "legend_rows_per_column": 8, "legend_row_mm": 6.2, "legend_group_row_mm": 6.4,
        "legend_symbol_box_mm": 6.0, "scale_bar_margin_mm": 3.0,
        "north_arrow_size_mm": 16.0, "fixed_layout": "map_above_legend_below",
    }


def _spec(*, with_layers=True):
    spec = FigureSpec(
        template_id="route_overview_v1", template_version=1,
        title="航路周边总览图（SMOKE）", route_id="SMOKE-1",
        route_source="operational_routes",
        route_geometry=[[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]],
        route_start=[122.05, 29.95], route_end=[122.35, 29.98],
        turn_points=[[122.20, 30.01]],
        extent=ExtentSpec(west=121.90, south=29.85, east=122.50, north=30.12,
                          width_km=58.0, height_km=30.0),
        extent_mode="smoke", generated_from_revision=3, layout=_layout_plan(),
        labels=[
            LabelSpec(kind="start", text="起点甲", longitude=122.05, latitude=29.95,
                      priority=100, style_key="label_endpoint"),
            LabelSpec(kind="end", text="终点乙", longitude=122.35, latitude=29.98,
                      priority=100, style_key="label_endpoint"),
        ],
        legend_items=[
            LegendItem("land", "land", "陆地区域", "environment"),
            LegendItem("planned_route", "planned_route", "规划航路", "route"),
            LegendItem("start_point", "start_point", "起点", "route"),
        ],
    )
    if with_layers:
        spec.layers = [
            # 海域 = 地图画布矩形 − 制图陆地面：这里给一个只覆盖左上角的假海域补集，
            # 让"海域填充"与"陆地填充"两种颜色都能在同一张图上出现。
            LayerSpec("sea", "海域", "sea", GEOMETRY_POLYGON, "cartographic_land",
                      source_status="available", feature_count=1,
                      data={"polygons": [[[121.90, 30.00], [122.50, 30.00], [122.50, 30.12],
                                          [121.90, 30.12], [121.90, 30.00]]]}),
            LayerSpec("land", "陆地区域", "land", GEOMETRY_POLYGON, "cartographic_land",
                      source_status="available", feature_count=1,
                      data={"polygons": [[[121.95, 29.87], [122.42, 29.87], [122.42, 30.10],
                                          [121.95, 30.10], [121.95, 29.87]]]}),
            LayerSpec("planned_route", "规划航路", "planned_route", GEOMETRY_LINE,
                      "operational_routes", source_status="available", feature_count=1,
                      data={"geometry": [[122.05, 29.95], [122.20, 30.01], [122.35, 29.98]]}),
            LayerSpec("start_point", "起点", "start_point", GEOMETRY_POINT,
                      "operational_routes", source_status="available", feature_count=1,
                      data={"points": [{"longitude": 122.05, "latitude": 29.95}]}),
            LayerSpec("end_point", "终点", "end_point", GEOMETRY_POINT,
                      "operational_routes", source_status="available", feature_count=1,
                      data={"points": [{"longitude": 122.35, "latitude": 29.98}]}),
        ]
    return spec


def test_renderer_produces_a_real_png_with_expected_size():
    _qgis_app()
    from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

    renderer = QgisFigureRenderer()
    payload = renderer.render(_spec(), dpi=100.0)
    assert isinstance(payload, bytes) and payload.startswith(b"\x89PNG")

    from PIL import Image
    import io

    image = Image.open(io.BytesIO(payload))
    expected_width = 210.0 / 25.4 * 100.0
    expected_height = 297.0 / 25.4 * 100.0
    assert abs(image.width - expected_width) / expected_width < 0.05
    assert abs(image.height - expected_height) / expected_height < 0.05


def test_rendered_map_contains_the_sea_background_and_land_fill():
    _qgis_app()
    from PIL import Image
    import io

    from cns_planner.gis.qgis_figure_renderer import QgisFigureRenderer

    payload = QgisFigureRenderer().render(_spec(), dpi=72.0)
    image = Image.open(io.BytesIO(payload)).convert("RGB")
    colors = image.getcolors(maxcolors=1_000_000) or []
    palette = {color: count for count, color in colors}
    # 海域底色来自版式常量（地图框底色），陆地填充来自样式表：两者都必须真的出现。
    assert palette.get(_rgb("#cfe6f5"), 0) > 200
    assert palette.get(_rgb("#fbfbf8"), 0) > 200
    # 规划航路颜色也必须出现（航路在最高视觉层）。
    assert palette.get(_rgb("#123a6b"), 0) > 20


def test_no_drawable_layers_is_an_explicit_failure():
    _qgis_app()
    from cns_planner.gis.qgis_figure_renderer import FigureRenderError, QgisFigureRenderer

    with pytest.raises(FigureRenderError) as error:
        QgisFigureRenderer().render(_spec(with_layers=False), dpi=72.0)
    assert "没有任何可绘制的图层" in str(error.value)


def test_legend_symbol_previews_are_not_empty():
    _qgis_app()
    for style_key in figure_style_module.FIGURE_STYLES:
        payload = figure_style_module.symbol_preview_image(style_key)
        assert payload.startswith(b"\x89PNG"), style_key
        # 空图只有 ~120 字节；有内容的符号明显更大。
        assert len(payload) > 200, (style_key, len(payload))


def test_chinese_font_is_resolved_and_registered():
    _qgis_app()
    from cns_planner.gis.qgis_figure_renderer import (
        QgisFigureRenderer, choose_font_family, ensure_fonts_registered,
    )

    family = choose_font_family()
    assert family in figure_style_module.FONT_CANDIDATES
    renderer = QgisFigureRenderer()
    ensure_fonts_registered(renderer)
    assert renderer._font_files_registered is True


def _rgb(text):
    value = text.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
