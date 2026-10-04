"""Round30-B1.1（Formal Figure Visual Polish）定向测试（纯计算层，不需要 PyQGIS）。

覆盖本轮 16 条验收要求中可在纯计算层验证的部分：

1. 字号达到 B1.1 正式值（且不得缩回 B1 小字号）；
2. 地图框未缩小（五图统一 182.0 × 176.2 mm，且与图例内容无关）；
3. map / grid CRS 未改变；
4. R0005 的 adaptive grid interval = 0.05°（或等价结果）；
5. top / right annotation 关闭；
6. bottom / left annotation 打开；
7. 经纬网文字保持水平；
8. endpoint 标签不使用省略号；
9. CNS proposal 优先自动换行（最多两~三行）；
10. 卡片顺移 > 4 mm 时产生 leader line；
11. 距离 ≤ 4 mm 时不出现无意义 leader；
12. 多业务分色卡仍然正确；
13. 普通既有铁塔只改样式（尺寸 / 描边 / 不透明度）；
14. 业务半径 / 几何未变化；
15. Radar selected_panel_count 仍为 0；
16. 导图前后 canonical state 不变（sha256 / revision）。

真机（PyQGIS）部分在 ``test_map_figures_cartographic_polish_qgis.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.application.map_figure_service import (
    _altitude_fact, _figure_subtitle, _figure_title, _layout_plan,
)
from cns_planner.gis.figure_legend import CNS_LEGEND_BALANCE_TOLERANCE, legend_geometry
from cns_planner.gis.figure_style import (
    CNS_LABEL_CARD_COLORS, CNS_LABEL_CARD_ORDER, FIGURE_STYLES, LABEL_STYLES, LAYOUT,
)
from cns_planner.gis.qgis_figure_renderer import (
    DEFAULT_MAP_CRS, GRID_CRS_AUTHID, WGS84_AUTHID, grid_interval_for, grid_intervals,
    label_anchor_distance_mm, label_card_geometry, leader_line_geometry,
    wrap_label_text,
)
from cns_planner.reporting.map_templates import (
    CNS_LAYOUT_PROFILE, CNS_SHARED_PARAMETERS, SURVEILLANCE_SERVICE_RADAR,
    SURVEILLANCE_SERVICE_RID,
)
from test_map_figures_cns_templates import (  # noqa: F401 - 复用同一套 canonical 形态
    _five_specs as _cns_five_specs, _layers, _service,
)

CNS_TARGETS = (
    ("communication_layout_v1", None),
    ("surveillance_layout_v1", {"surveillance_service": "rid_cooperative"}),
    ("surveillance_layout_v1", {"surveillance_service": "radar_noncooperative"}),
    ("navigation_layout_v1", None),
    ("cns_combined_v1", None),
)


def _five_specs(tmp_path):
    service, session, renderer = _service(tmp_path)
    specs = [
        service.build_figure(template_id=template_id, route_id="R0005",
                             parameter_overrides=overrides)
        for template_id, overrides in CNS_TARGETS
    ]
    return service, session, renderer, specs


# ============================================================ 1 ~ 3. 字号 / 地图框 / CRS

def test_formal_font_sizes_reach_b1_1_values():
    """B1.1 正式字号：主 19 / 副 10.5 / 图例 14-11-10.5 / 标签 12.5-11.5-10-9。"""

    assert LAYOUT["map_title_font_size"] == 19.0
    assert LAYOUT["subtitle_font_size"] == 10.5
    assert LAYOUT["legend_title_font_size"] == 14.0
    assert LAYOUT["legend_group_font_size"] == 11.0
    assert LAYOUT["legend_item_font_size"] == 10.5
    assert LABEL_STYLES["label_endpoint"]["font_size"] == 12.5
    assert LABEL_STYLES["label_cns_proposal"]["font_size"] == 11.5
    assert LABEL_STYLES["label_cns_facility"]["font_size"] == 10.0
    assert LABEL_STYLES["label_place"]["font_size"] == 9.0
    # 经纬网标注落在 8.5~9.5 目标区间的下沿（必须低于正文标注）。
    assert 8.5 <= LAYOUT["grid_font_size"] <= 9.5
    assert LAYOUT["grid_font_size"] < LABEL_STYLES["label_place"]["font_size"]
    # 比例尺 9~9.5；Radar 披露 8.5~9。
    assert 9.0 <= LAYOUT["scalebar_font_size"] <= 9.5
    assert 8.5 <= LAYOUT["footer_disclosure_font_size"] <= 9.0
    # 起终点 / 提案 / 既有设施都是 bold。
    for key in ("label_endpoint", "label_endpoint_end", "label_cns_proposal",
                "label_cns_facility"):
        assert LABEL_STYLES[key]["bold"] is True


def test_legend_columns_cap_is_four_and_no_entry_crosses_its_column(tmp_path):
    """图例最多 4 列；实际使用的列数下，任何条目的估算宽度都不得超过该列可用宽度。"""

    from cns_planner.gis.qgis_figure_renderer import (
        label_text_width_mm, legend_display_text,
    )

    assert int(CNS_SHARED_PARAMETERS["legend_columns"]) == 4
    assert int(LAYOUT["max_legend_columns"]) == 4
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        plan = spec.layout
        columns = max(1, int(plan["legend_columns"]))
        column_width = (
            float(plan["legend_width_mm"]) - 2 * float(plan["legend_side_padding_mm"])
            - (columns - 1) * float(plan["legend_column_gap_mm"])
        ) / columns
        text_width = column_width - float(plan["legend_symbol_box_mm"]) - float(
            plan["legend_text_gutter_mm"]
        )
        for item in spec.legend_items:
            display, _shortened = legend_display_text(
                item.layer_key, item.display_name, text_width,
                float(LAYOUT["legend_item_font_size"]),
            )
            assert label_text_width_mm(
                display, float(LAYOUT["legend_item_font_size"]),
            ) <= text_width + 1e-6, (spec.template_id, item.display_name, display)


def test_legend_full_names_are_preserved_in_metadata(tmp_path):
    """图例显示文本可以收口，但**完整名称**必须仍在 FigureSpec 里（审计不丢）。"""

    _, _, _, specs = _five_specs(tmp_path)
    combined = specs[4]
    names = {item.display_name for item in combined.legend_items}
    assert "非合作监视能力限制（无可行布设）" in names
    assert any("导航完整性监测点" in name for name in names)
    # 收口表自身必须覆盖这些会被收口的条目。
    from cns_planner.gis.qgis_figure_renderer import LEGEND_COMPACT_LABELS

    for key in ("cns_radar_limitation", "cns_nav_proposal", "cns_coverage_rid_land"):
        assert key in LEGEND_COMPACT_LABELS


def test_map_frame_is_not_shrunk_and_is_identical_across_five_figures(tmp_path):
    """地图框不因图例放大而缩小：五图统一 182.0 × 176.2 mm。"""

    _, _, _, specs = _five_specs(tmp_path)
    frames = {
        (round(spec.layout["map_width_mm"], 6), round(spec.layout["map_height_mm"], 6))
        for spec in specs
    }
    assert frames == {(182.0, float(LAYOUT["cns_fixed_map_height_mm"]))}, frames
    # 与某个"图例极大"的假设版面比较：地图框高度必须**完全不变**。
    heavy = _layout_plan(CNS_SHARED_PARAMETERS, [
        {"group": group, "text": text, "style_key": text}
        for group, items in (
            ("environment", ["海域", "陆地区域"]),
            ("obstacle", ["地形障碍（≥ 显示阈值）", "建筑障碍（≥ 显示阈值）"]),
            ("facility", ["既有通信站址"]),
            ("proposal", ["通信规划提案（未确认）", "RID 规划提案（未确认）",
                          "导航完整性监测点提案（未确认）"]),
            ("coverage", ["通信规划服务半径 4 km", "RID 陆地/沿海规划范围 2 km",
                          "RID 海上延伸规划范围 2–5 km"]),
            ("limitation", ["非合作监视能力限制（无可行布设）"]),
            ("route", ["规划航路", "航路转弯点", "起点", "终点"]),
        )
        for text in items
    ])
    assert heavy["map_height_mm"] == pytest.approx(
        float(LAYOUT["cns_fixed_map_height_mm"]), abs=1e-9,
    )
    # 图例确实被放大了（B1 的 52.4 mm → 62 mm 量级）。
    assert heavy["legend_height_mm"] >= 58.0


def test_map_and_grid_crs_are_unchanged(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        assert spec.layout["map_crs"] == DEFAULT_MAP_CRS == "EPSG:32651"
        assert spec.layout["grid_crs"] == GRID_CRS_AUTHID == WGS84_AUTHID == "EPSG:4326"
        assert spec.metadata["map_crs"] == DEFAULT_MAP_CRS
        assert spec.metadata["grid_crs"] == GRID_CRS_AUTHID


# ============================================================ 4 ~ 7. 经纬网

def test_grid_interval_is_005_degree_for_the_r0005_extent(tmp_path):
    """R0005 当前 extent 下 adaptive interval 给出 0.05°（每轴约 5~7 个主刻度）。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        interval_x, interval_y = grid_intervals(spec)
        assert interval_x == pytest.approx(0.05), (spec.template_id, interval_x)
        assert interval_y == pytest.approx(0.05), (spec.template_id, interval_y)
        span_lon = float(spec.extent.east) - float(spec.extent.west)
        span_lat = float(spec.extent.north) - float(spec.extent.south)
        ticks_x = span_lon / interval_x
        ticks_y = span_lat / interval_y
        assert 4.0 <= ticks_x <= 8.0, ticks_x
        assert 4.0 <= ticks_y <= 8.0, ticks_y


def test_adaptive_grid_interval_scales_with_the_extent():
    """adaptive：范围越大间隔越粗，且刻度数始终落在 5~7 附近。"""

    assert grid_interval_for(0.2893) == pytest.approx(0.05)
    assert grid_interval_for(0.2433) == pytest.approx(0.05)
    assert grid_interval_for(2.0) > grid_interval_for(0.29)
    assert grid_interval_for(0.02) < grid_interval_for(0.29)
    for span in (0.02, 0.1, 0.29, 0.5, 1.0, 2.0, 5.0):
        interval = grid_interval_for(span)
        assert 1 <= span / interval <= 12, (span, interval)


def test_grid_annotation_sides_and_direction_are_formal():
    """只启用 bottom（经度）与 left（纬度）；top / right 关闭；文字全部水平。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    body = source.split("def _configure_grid", 1)[1].split("def grid_interval_for", 1)[0]
    # 先全部 HideAll，再只打开 Bottom 与 Left。
    assert body.count("QgsLayoutItemMapGrid.HideAll") == 1
    assert "ShowAll, QgsLayoutItemMapGrid.Bottom" in body
    assert "ShowAll, QgsLayoutItemMapGrid.Left" in body
    assert "ShowAll, QgsLayoutItemMapGrid.Top" not in body
    assert "ShowAll, QgsLayoutItemMapGrid.Right" not in body
    # 不再有竖排（Vertical）标注。
    assert "QgsLayoutItemMapGrid.Vertical" not in body
    assert body.count("QgsLayoutItemMapGrid.Horizontal") >= 2
    # 经纬网 CRS 仍是 WGS84，且网格线是细线符号。
    assert 'context.get("grid_crs") or GRID_CRS_AUTHID' in body
    assert "grid_line_width_mm" in body


# ============================================================ 8 ~ 9. 折行 / 无省略号

def test_endpoint_and_proposal_labels_do_not_use_ellipsis(tmp_path):
    """正式站名（起终点 / 选中提案）不得出现省略号，也不允许被截断。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        for label in spec.labels:
            assert "…" not in label.text, (spec.template_id, label.text)
            style = LABEL_STYLES.get(label.style_key) or {}
            geometry = label_card_geometry(label.text, style)
            assert geometry["truncated"] is False, (label.text, geometry)
            assert geometry["line_count"] <= int(style.get("max_lines", 3))
            # 折行后必须是无省略号的完整文字。
            for line in geometry["lines"]:
                assert "…" not in line, (label.text, geometry["lines"])


def test_long_station_name_wraps_at_most_two_or_three_lines():
    """长站名优先自动换行（最多 max_lines 行），不截断、也不把服务后缀切碎。"""

    style = LABEL_STYLES["label_cns_proposal"]
    inner = float(style["max_width_mm"]) - 6.0
    long_name = "东白莲华泰油库生活区野外起降点 /导航"
    lines, truncated = wrap_label_text(
        long_name, inner, float(style["font_size"]),
        bold=True, max_lines=int(style["max_lines"]),
    )
    assert truncated is False
    assert 2 <= len(lines) <= int(style["max_lines"])
    assert "".join(line.replace(" ", "") for line in lines) == long_name.replace(" ", "")
    from cns_planner.gis.qgis_figure_renderer import label_text_width_mm

    for line in lines:
        assert label_text_width_mm(line, style["font_size"], bold=True) <= inner + 1e-6
    # 服务后缀不得被切成 "／通" + "信"：含"/"的行必须在斜杠之后整体换行。
    assert not any(line.rstrip().endswith("/") for line in lines), lines


def test_wrap_prefers_natural_boundaries_over_hard_splits():
    """优先在空格 / 斜杠 / 中文边界断行，且不产生过短的尾巴。"""

    style = LABEL_STYLES["label_cns_proposal"]
    inner = float(style["max_width_mm"]) - 6.0
    lines, _ = wrap_label_text(
        "普陀虾峙东白莲罐区 /通信", inner, float(style["font_size"]),
        bold=True, max_lines=3,
    )
    assert lines[-1].lstrip().startswith("/"), lines
    for line in lines[:-1]:
        assert len(line.strip()) >= 3, lines


def test_card_max_width_is_in_the_required_band():
    """推荐卡片最大宽度 50~55 mm（正式站名必须完整显示）。"""

    for key in ("label_endpoint", "label_cns_proposal", "label_cns_facility"):
        width = float(LABEL_STYLES[key]["max_width_mm"])
        assert 50.0 <= width <= 55.0, (key, width)


def test_card_height_grows_with_line_count():
    style = LABEL_STYLES["label_cns_proposal"]
    one = label_card_geometry("虾峙岛大岙站 /通信", style)
    two = label_card_geometry("东白莲华泰油库生活区野外起降点 /导航", style)
    assert two["line_count"] >= one["line_count"]
    assert two["height_mm"] > one["height_mm"]


# ============================================================ 10 ~ 11. leader line

def test_leader_line_only_appears_beyond_the_threshold():
    """有效距离 > 4 mm 才画 leader line；≤ 4 mm 时返回 None；> 15 mm 时直接不画。"""

    placement = {"x": 100.0, "y": 100.0, "align": None}
    card_width, card_height = 40.0, 8.0
    threshold = float(LAYOUT["leader_line_threshold_mm"])
    assert threshold == 4.0

    # 锚点离卡片左边缘 2 mm → 不画。
    near = leader_line_geometry(placement, card_width, card_height, 98.0, 104.0)
    assert near is None
    # 锚点离卡片 12 mm → 画（仍在 Round30-B1.2 的 15 mm 预算内）。
    far = leader_line_geometry(placement, card_width, card_height, 88.0, 104.0)
    assert far is not None
    assert far["distance_mm"] == pytest.approx(12.0)
    assert far["length_mm"] > 0
    # 卡片罩住锚点 → 距离 0 → 不画。
    assert label_anchor_distance_mm(placement, card_width, card_height, 110.0, 104.0) == 0.0
    assert leader_line_geometry(placement, card_width, card_height, 110.0, 104.0) is None
    # Round30-B1.2：有效距离 20 mm 会画出超预算引线，因此**返回 None**
    # （调用方改成给标签换位置，而不是把引线拉长）。
    assert float(LAYOUT["leader_line_max_length_mm"]) == 15.0
    assert leader_line_geometry(placement, card_width, card_height, 80.0, 104.0) is None


def test_leader_line_starts_outside_the_marker_and_ends_on_the_card_edge():
    placement = {"x": 100.0, "y": 100.0, "align": None}
    geometry = leader_line_geometry(placement, 40.0, 8.0, 88.0, 104.0)
    assert geometry is not None
    start_x, start_y = geometry["start"]
    end_x, end_y = geometry["end"]
    gap = float(LAYOUT["leader_line_endpoint_gap_mm"])
    # 锚点在卡片左侧：起点退开 marker 外缘固定净空，终点落在卡片最近边（x=100）。
    assert start_x == pytest.approx(88.0 + gap)
    assert start_y == pytest.approx(104.0)
    assert end_x == pytest.approx(100.0)
    assert end_y == pytest.approx(104.0)
    # 锚点正下方：优先竖直走线（最短、最干净）。
    vertical = leader_line_geometry(placement, 40.0, 8.0, 110.0, 120.0)
    assert vertical is not None
    assert len(vertical["points"]) == 2
    assert vertical["start"][0] == pytest.approx(110.0)
    assert vertical["end"][0] == pytest.approx(110.0)
    assert vertical["end"][1] == pytest.approx(108.0)


def test_leader_line_routes_around_already_placed_cards():
    """引线必须避开已放置的其它标签卡（不压过正文文字）。"""

    from cns_planner.gis.qgis_figure_renderer import _segment_hits_box

    # 锚点正下方先放了一张障碍卡：竖直走线会被挡住，引线必须**贴着卡片上边绕过去**
    # （3 个折点），且仍受 Round30-B1.2 的 15 mm 预算限制。
    placement = {"x": 100.0, "y": 130.0, "align": None}
    anchor = (104.0, 120.0)
    obstacle = (104.0, 126.0, 112.0, 130.0)
    blocked = leader_line_geometry(
        placement, 40.0, 8.0, anchor[0], anchor[1], obstacles=[obstacle],
    )
    assert blocked is not None
    assert len(blocked["points"]) >= 3, blocked
    for index in range(len(blocked["points"]) - 1):
        assert not _segment_hits_box(
            blocked["points"][index], blocked["points"][index + 1], obstacle,
        ), blocked
    # 没有障碍时仍优先最短竖直走线。
    clear = leader_line_geometry(placement, 40.0, 8.0, anchor[0], anchor[1])
    assert len(clear["points"]) == 2
    assert clear["length_mm"] <= float(LAYOUT["leader_line_max_length_mm"])


def test_far_shifted_proposal_cards_get_a_leader_line(tmp_path):
    """被顺移很远的提案卡必须带 leader line（否则用户找不到它在标注哪个站点）。"""

    _, _, _, specs = _five_specs(tmp_path)
    combined = specs[4]
    # 该图存在被顺移的提案 / 地名卡：任何被画出的卡片，只要离锚点 > 4 mm，
    # 其样式都必须允许 leader line。
    for label in combined.labels:
        style = LABEL_STYLES.get(label.style_key) or {}
        if label.kind in ("cns_proposal", "cns_existing", "start", "end"):
            assert style.get("leader") is True, (label.kind, label.style_key)


def test_leader_colors_follow_service_and_endpoint_kinds():
    from cns_planner.gis.qgis_figure_renderer import leader_color

    class _Label:
        def __init__(self, kind, services):
            self.kind = kind
            self.services = services

    assert leader_color(_Label("start", []), {}) == "#12a150"
    assert leader_color(_Label("end", []), {}) == "#d81b1b"
    assert leader_color(_Label("cns_proposal", ["communication"]), {}) == \
        CNS_LABEL_CARD_COLORS["communication"]["leader"]
    assert leader_color(_Label("cns_proposal", ["rid"]), {}) == \
        CNS_LABEL_CARD_COLORS["rid"]["leader"]
    assert leader_color(_Label("cns_existing", ["navigation"]), {}) == \
        CNS_LABEL_CARD_COLORS["navigation"]["leader"]


def test_leader_lines_are_formal_layout_items():
    """leader line 必须是正式 QGIS 版面项（不是 PNG 后处理）。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    assert "class LeaderLineItem(QgsLayoutItem)" in source
    assert "layout.addLayoutItem(item)" in source
    # 明确说明为什么不用 QgsLayoutItemShape 的折线（3.44 没有该类型）。
    assert "QgsLayoutItemShape" in source


# ============================================================ 12. 多业务分色

def test_multi_service_split_cards_still_work(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    combined = specs[4]
    colocated = [
        label for label in combined.labels
        if label.longitude == pytest.approx(122.228521)
        and label.latitude == pytest.approx(29.831441)
    ]
    assert len(colocated) == 1
    assert colocated[0].services == ["communication", "rid"]
    from cns_planner.gis.qgis_figure_renderer import label_card_segments

    assert label_card_segments(colocated[0].services) == [
        ("communication", 0.0, 0.5), ("rid", 0.5, 1.0),
    ]
    assert CNS_LABEL_CARD_ORDER == ("communication", "rid", "radar", "navigation")


# ============================================================ 13 ~ 15. 视觉降级 / 业务不变

def test_ordinary_towers_and_obstacles_only_changed_visually():
    """普通铁塔 / 障碍层只改样式：不删数据、不改 threshold、不改几何。"""

    tower = FIGURE_STYLES["tower_existing"]
    assert tower["size"] == 2.3
    assert tower["outline_width"] == 0.4
    assert tower["opacity"] == 0.78
    assert tower["shape"] == "triangle"
    # 提案符号仍明显更大、更实。
    assert FIGURE_STYLES["cns_comm_proposal"]["size"] >= 4.4
    assert FIGURE_STYLES["cns_comm_proposal"]["fill"] == "transparent"

    terrain = FIGURE_STYLES["terrain_obstacle"]
    assert terrain["opacity"] <= 0.6
    assert terrain["outline_width"] <= 0.2
    building = FIGURE_STYLES["building_obstacle"]
    assert building["opacity"] <= 0.6

    # 显示阈值仍是模板参数（图面口径），不是业务 gate。
    assert CNS_SHARED_PARAMETERS["terrain_threshold_m"] == 100.0
    assert CNS_SHARED_PARAMETERS["building_threshold_m"] == 100.0
    assert CNS_SHARED_PARAMETERS["terrain_threshold_basis"] == (
        "figure_display_threshold_from_spec"
    )


def test_business_radii_and_geometry_are_untouched(tmp_path):
    """业务半径 / 覆盖几何完全未变：4000 / 2000 / 5000 m。"""

    _, _, _, specs = _five_specs(tmp_path)
    assert _layers(specs[0])["cns_coverage_comm"].data["radius_m"] == 4000.0
    rid = _layers(specs[1])
    assert rid["cns_coverage_rid_land"].data["radius_m"] == 2000.0
    assert rid["cns_coverage_rid_sea"].data["radius_m"] == 5000.0
    for spec in specs:
        for layer in spec.layers:
            radius = layer.source_detail.get("radius_m")
            if radius is not None:
                assert float(radius) in (2000.0, 4000.0, 5000.0)


def test_radar_selected_panels_are_still_zero(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    radar = _layers(specs[2])["cns_radar_limitation"]
    assert radar.source_detail["selected_panel_count"] == 0
    assert radar.source_detail["manufactured_radar_sites"] == 0
    assert radar.source_detail["drawn_radar_sectors"] == 0
    assert radar.feature_count == 0
    assert specs[2].layout["map_disclosure"]
    assert any(item.layer_key == "cns_radar_limitation"
               for item in specs[2].legend_items)


# ============================================================ 16. 只读

def test_export_keeps_canonical_state_unchanged(tmp_path):
    """导图前后 canonical state 不变：不 save、不推进 revision、不伪造 Radar。"""

    service, session, renderer, specs = _five_specs(tmp_path)
    before = json.dumps(session.state, ensure_ascii=False, sort_keys=True)
    revision = session.state["revision"]
    for spec in specs:
        service._render(spec, dpi=300.0)  # noqa: SLF001 - 断言渲染路径同样只读
    assert session.saved == 0
    assert session.state["revision"] == revision
    assert json.dumps(session.state, ensure_ascii=False, sort_keys=True) == before


# ============================================================ 标题 / 副标题 / 审计开关

def test_formal_titles_distinguish_all_five_figures():
    base = dict(CNS_SHARED_PARAMETERS)
    assert _figure_title("x", {}, "communication_layout_v1", base) == "通信设施布设图"
    assert _figure_title(
        "x", {}, "surveillance_layout_v1",
        {**base, "surveillance_service": SURVEILLANCE_SERVICE_RID},
    ) == "RID 合作监视设施布设图"
    assert _figure_title(
        "x", {}, "surveillance_layout_v1",
        {**base, "surveillance_service": SURVEILLANCE_SERVICE_RADAR},
    ) == "Radar 非合作监视评估图"
    assert _figure_title("x", {}, "navigation_layout_v1", base) == (
        "导航完整性监测点布设图"
    )
    assert _figure_title("x", {}, "cns_combined_v1", base) == "CNS 综合设施布设图"


def test_subtitle_comes_from_canonical_altitude_facts():
    """副标题只由 canonical facts 组成；缺高度事实时**不硬编码**。"""

    state = {"radar_surveillance_layout": {"items": [
        {"route_id": "R0005", "altitude_layer_id": "ALT-100", "altitude_m": 100.0},
    ]}}
    assert _altitude_fact(state, "R0005") == ("ALT-100", 100.0)
    assert _figure_subtitle(state, "R0005") == "R0005 · ALT-100 · 固定巡航高度 100 m"
    # 没有该 route 的记录 → 只写 route_id。
    assert _figure_subtitle(state, "R9999") == "R9999"
    assert _figure_subtitle({}, "R0005") == "R0005"
    # 有高度层但没有 altitude_m → 不编造高度。
    partial = {"radar_surveillance_layout": {"items": [
        {"route_id": "R0005", "altitude_layer_id": "ALT-100"},
    ]}}
    assert _figure_subtitle(partial, "R0005") == "R0005 · ALT-100"


def test_five_figures_carry_title_and_subtitle(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    titles = {spec.title for spec in specs}
    assert len(titles) == 5, titles
    for spec in specs:
        assert spec.title and "R0005" not in spec.title
        # 副标题完全由 canonical facts 组成（夹具里有 R0005 的 ALT-100 记录）。
        assert spec.subtitle == "R0005 · ALT-100 · 固定巡航高度 100 m"
        assert spec.layout["map_disclosure"] == spec.metadata["map_disclosure"]


def test_audit_footer_switch_defaults_to_off_and_keeps_metadata(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        assert spec.parameters["audit_footer"] is False
        assert spec.layout["audit_footer"] is False
        assert spec.layout["footer_strip_top_mm"] is None
        # 审计信息仍在 metadata 与参数里（正式图隐藏、审计不丢）。
        assert spec.metadata["map_crs"] == "EPSG:32651"
        assert spec.metadata["grid_crs"] == "EPSG:4326"
        assert spec.generated_from_revision == 410
    # review 模式：显式打开审计条后，它严格位于地图框与图例框之间。
    review = _layout_plan({**CNS_SHARED_PARAMETERS, "audit_footer": True})
    assert review["audit_footer"] is True
    assert review["footer_strip_top_mm"] >= (
        review["map_top_mm"] + review["map_height_mm"]
    )
    assert review["legend_top_mm"] > review["footer_strip_top_mm"]
    # 打开审计条**不改变**地图框。
    assert review["map_height_mm"] == pytest.approx(
        float(LAYOUT["cns_fixed_map_height_mm"]), abs=1e-9,
    )


def test_scale_bar_shows_three_segments_for_the_r0005_extent(tmp_path):
    """R0005 下比例尺是 0 | 2 | 4 km 三段（不是只给两端）。"""

    from cns_planner.gis.figure_style import LAYOUT as STYLE_LAYOUT
    from cns_planner.gis.qgis_figure_renderer import scale_bar_geometry

    _, _, _, specs = _five_specs(tmp_path)
    spec = specs[0]
    rendered = 27_940.0  # R0005 米制渲染宽度
    geometry = scale_bar_geometry(spec, _Rect(rendered, 27_080.0), spec.layout)
    assert int(geometry["segments"]) == int(STYLE_LAYOUT["scalebar_segments"]) == 3
    assert float(geometry["segment_km"]) == pytest.approx(2.0)
    assert float(geometry["total_km"]) == pytest.approx(6.0)
    assert 20.0 <= float(geometry["width_mm"]) <= 56.0


class _Rect:
    """最小渲染范围替身（只需要 width/height/isEmpty）。"""

    def __init__(self, width, height):
        self._width, self._height = float(width), float(height)

    def isEmpty(self):
        return self._width <= 0 or self._height <= 0

    def width(self):
        return self._width

    def height(self):
        return self._height


def test_north_arrow_is_enlarged_but_stays_inside_the_frame(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        plan = spec.layout
        assert plan["north_arrow_size_mm"] == pytest.approx(
            float(LAYOUT["north_arrow_size_mm"])
        )
        assert plan["north_arrow_size_mm"] == pytest.approx(12.0)
        # 仍留在地图框内（右下角内距定位）。
        assert plan["north_arrow_margin_mm"] > 0
        assert plan["north_arrow_size_mm"] + plan["north_arrow_margin_mm"] < (
            plan["map_width_mm"] / 2.0
        )
