"""Round30-B1.2（Final Cartographic Micro-adjustment）定向测试（纯计算层）。

覆盖本轮**只做**的 3 件事（其余全部冻结）：

1. **Leader line**：线宽 0.4~0.5 mm、最大长度 15 mm、短直线优先；
   超预算时"给标签换位置"而不是继续拉长引线，换不到就宁可不画；
2. **同址 endpoint + navigation 合并**：N005 / N006 不再产生第二张导航文字卡，
   改为起降点主卡的第二行（浅黄 service tag），主卡外框仍是起点绿 / 终点红；
   Navigation 的图层 / 符号 / 图例 / metadata 一条不少；
3. **经纬度与比例尺**：grid interval 仍 0.05°、left 在外侧、bottom 也移到**框外**下侧，
   仍只显示 left + bottom、水平、两位小数；比例尺仍是 0 | 2 | 4 km 且向图内上移，
   与框外经度标注完全不重叠；北箭头不变。

真机（PyQGIS）部分在 ``test_map_figures_final_tuning_qgis.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.gis.figure_spec import LabelSpec
from cns_planner.gis.figure_style import (
    CNS_LABEL_CARD_COLORS, CNS_LABEL_SECONDARY_STYLE, LAYOUT,
)
from cns_planner.gis.qgis_figure_renderer import (
    _label_card_segments, card_secondary_row, label_card_geometry_with_secondary,
    label_secondary_style, leader_length_budget, leader_line_geometry,
    leader_path_length_mm, scale_bar_geometry,
)
from test_map_figures_cartographic_polish import _five_specs  # noqa: F401 - 复用同一套形态

#: 主卡名（起降点）与第二行（导航监测提案）的测试文案。
START_NAME = "直升机场起降点"
END_NAME = "东白莲华泰油库生活区野外起降点"
NAV_LINE = "导航监测提案（未确认）"


def _endpoint_and_nav(spec):
    endpoints = [item for item in spec.labels if item.kind in ("start", "end")]
    navigation = [
        item for item in spec.labels
        if "navigation" in (item.services or []) and item.kind == "cns_proposal"
    ]
    return endpoints, navigation


# ============================================================ 1. leader line

def test_leader_width_is_in_the_required_band():
    """引线线宽 0.4~0.5 mm（原来是 1.1 mm，太粗）。"""

    width = float(LAYOUT["leader_line_width_mm"])
    assert 0.4 <= width <= 0.5, width


def test_leader_max_length_is_15mm_and_replaces_the_old_threshold():
    """引线建议最大长度 15 mm；旧的 26 mm "锚点搜索门槛"必须收下来。"""

    assert float(LAYOUT["leader_line_max_length_mm"]) == 15.0
    assert float(LAYOUT["label_anchor_search_threshold_mm"]) <= 15.0
    assert leader_length_budget() == 15.0


def test_leader_geometry_refuses_to_stretch_beyond_the_budget():
    """超过 15 mm 预算时**直接不画**（由调用方改选标签位置），绝不拉长。"""

    placement = {"x": 100.0, "y": 100.0, "align": None}
    card_width, card_height = 40.0, 8.0
    gap = float(LAYOUT["leader_line_endpoint_gap_mm"])
    budget = leader_length_budget()
    # 刚好在预算内：画，而且是**两点的短直线**。
    inside = budget - gap
    geometry = leader_line_geometry(placement, card_width, card_height, 100.0 - inside, 104.0)
    assert geometry is not None
    assert len(geometry["points"]) == 2, geometry
    assert geometry["length_mm"] <= budget + 1e-9
    assert leader_path_length_mm(placement, card_width, card_height, 100.0 - inside, 104.0) \
        == pytest.approx(budget)
    # 超预算 0.5 mm：不画。
    outside = budget - gap + 0.5
    assert leader_line_geometry(
        placement, card_width, card_height, 100.0 - outside, 104.0,
    ) is None


def test_leader_prefers_a_straight_line_over_a_folded_one():
    """直线可用时永远是两点直线（短直线优先于短折线）。"""

    placement = {"x": 100.0, "y": 100.0, "align": None}
    # 四例分别落在卡片的左 / 下 / 上 / 右，距离都在阈值与 15 mm 预算之间。
    for anchor in ((88.0, 104.0), (110.0, 114.0), (98.0, 94.0), (150.0, 104.0)):
        geometry = leader_line_geometry(placement, 40.0, 8.0, anchor[0], anchor[1])
        assert geometry is not None, anchor
        assert len(geometry["points"]) == 2, (anchor, geometry)
        assert float(geometry["length_mm"]) <= leader_length_budget() + 1e-9
    # 距离 ≤ 4 mm 的四个方向都不画无意义引线。
    for anchor in ((98.0, 103.0), (100.0, 110.0), (100.0, 98.0), (142.0, 104.0), (112.0, 96.0)):
        assert leader_line_geometry(placement, 40.0, 8.0, anchor[0], anchor[1]) is None, anchor


def test_leader_alpha_lowers_the_visual_weight_without_changing_hue():
    """颜色继续跟业务色，只按 ``leader_line_alpha`` 降低视觉权重。"""

    pytest.importorskip("qgis.PyQt.QtGui")
    from cns_planner.gis.qgis_figure_renderer import leader_pen_color

    alpha = float(LAYOUT["leader_line_alpha"])
    assert 0.5 <= alpha < 1.0
    for value in (CNS_LABEL_CARD_COLORS["navigation"]["leader"], "#12a150", "#d81b1b"):
        pen = leader_pen_color(value)
        assert pen.alpha() < 255
        assert pen.name().lower() == value.lower()


def test_leader_width_source_has_no_legacy_fallback():
    """引线宽度只从 LAYOUT 取一次，且默认值是 0.45（不再有 1.1 的兜底）。"""

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    body = source.split("def leader_pen_color", 1)[1].split("def _leader_line_item", 1)[0]
    assert "leader_line_alpha" in body
    assert 'LAYOUT.get("leader_line_width_mm", 0.45)' in source
    assert 'LAYOUT.get("leader_line_width_mm", 1.1)' not in source


def test_segment_box_intersection_detects_a_full_crossing():
    """穿框判定必须是精确的：从框外到框外、完全穿过矩形也要判成"相交"。

    旧实现用"框角是否落在线段的包围盒里"近似包含判定，一条完全穿过矩形的对角线会被
    误判成不相交，引线于是真的压过了别的标签卡。这条用例把正确语义钉死。
    """

    from cns_planner.gis.qgis_figure_renderer import _segment_hits_box

    box = (10.0, 10.0, 20.0, 20.0)
    # 从框外到框外，对角线完全穿过矩形。
    assert _segment_hits_box((0.0, 0.0), (30.0, 30.0), box) is True
    assert _segment_hits_box((0.0, 30.0), (30.0, 0.0), box) is True
    # 完全穿过但不经过任何框角（最能区分旧实现的反例）。
    assert _segment_hits_box((9.0, 19.0), (21.0, 11.0), box) is True
    # 端点落在框内。
    assert _segment_hits_box((15.0, 15.0), (40.0, 15.0), box) is True
    assert _segment_hits_box((15.0, 5.0), (15.0, 15.0), box) is True
    # 真正不相交：擦边（贴着上边外侧）与完全在外。
    assert _segment_hits_box((0.0, 9.0), (30.0, 9.0), box) is False
    assert _segment_hits_box((0.0, 21.0), (30.0, 21.0), box) is False
    assert _segment_hits_box((21.0, 0.0), (21.0, 30.0), box) is False
    assert _segment_hits_box((0.0, 0.0), (5.0, 5.0), box) is False


# ============================================================ 2. endpoint + navigation 合并

def test_navigation_proposal_is_merged_into_the_endpoint_card(tmp_path):
    """导航图 / 综合图：导航监测提案不再单独成卡，两端都并入起降点主卡。"""

    _, _, _, specs = _five_specs(tmp_path)
    navigation_spec, combined = specs[3], specs[4]
    for spec in (navigation_spec, combined):
        endpoints, navigation = _endpoint_and_nav(spec)
        assert len(endpoints) == 2, spec.template_id
        assert navigation == [], spec.template_id
        for item in endpoints:
            assert item.secondary_text == NAV_LINE, item.text
            assert item.secondary_service == "navigation"
            # 主卡仍是起终点样式（起点绿 / 终点红），services 只用于第二行底纹。
            assert item.style_key == "label_endpoint"
            assert item.services == ["navigation"]
        assert {item.kind for item in endpoints} == {"start", "end"}


def test_merged_endpoint_card_text_matches_the_requested_two_lines(tmp_path):
    """第二行文字逐字符合要求，且主名不再重复导航后缀。"""

    _, _, _, specs = _five_specs(tmp_path)
    endpoints, _ = _endpoint_and_nav(specs[3])
    for item in endpoints:
        assert "/导航" not in item.text
        assert item.secondary_text == "导航监测提案（未确认）"
    # 主名仍是起降点名本身（不因为合并而被改写成别的对象）。
    assert all(item.text for item in endpoints)


def test_other_figures_keep_the_existing_service_scheme(tmp_path):
    """通信 / RID 同址标签仍保持现有分色方案，不被合并影响。"""

    _, _, _, specs = _five_specs(tmp_path)
    communication, rid = specs[0], specs[1]
    for spec in (communication, rid):
        for item in spec.labels:
            if item.kind in ("start", "end"):
                assert item.secondary_text == ""
                assert item.services == []
    combined_colocated = [
        item for item in specs[4].labels
        if item.kind == "cns_proposal" and item.longitude == pytest.approx(122.228521)
    ]
    assert len(combined_colocated) == 1
    assert combined_colocated[0].services == ["communication", "rid"]


def test_navigation_layer_symbol_legend_and_metadata_survive(tmp_path):
    """合并只改文字表达：Navigation 的图层 / 符号 / 图例 / metadata 一条不少。"""

    service, _, _, specs = _five_specs(tmp_path)
    navigation_spec = specs[3]
    layer_keys = [layer.layer_key for layer in navigation_spec.layers]
    assert "cns_nav_proposal" in layer_keys
    legend_keys = [item.layer_key for item in navigation_spec.legend_items]
    assert "cns_nav_proposal" in legend_keys
    # 符号定义（同心空心圆环）与配色完全不变。
    from cns_planner.gis.figure_style import FIGURE_STYLES

    assert FIGURE_STYLES["cns_nav_proposal"]["outline"] == "#d19a00"
    assert FIGURE_STYLES["cns_nav_proposal"]["kind"] == "marker"
    body = json.dumps(navigation_spec.metadata, ensure_ascii=False)
    assert "navigation" in body.lower() or "导航" in body
    # FigureSpec 仍然可序列化（第二行字段进入 to_dict / from_dict）。
    payload = LabelSpec(
        kind="start", text=START_NAME, secondary_text=NAV_LINE,
        secondary_service="navigation",
    ).to_dict()
    assert payload["secondary_text"] == NAV_LINE
    assert LabelSpec.from_dict(payload).to_dict() == payload
    assert service is not None


def test_secondary_style_uses_the_navigation_card_colour():
    """第二行底纹取 CNS_LABEL_CARD_COLORS["navigation"]，不引入第二套配色。"""

    style = label_secondary_style()
    navigation = CNS_LABEL_CARD_COLORS["navigation"]
    assert style["fill"] == navigation["fill"]
    assert style["border"] == navigation["border"]
    assert float(style["font_size"]) == float(CNS_LABEL_SECONDARY_STYLE["font_size"])
    # 第二行字号明显小于主名（它是 service tag，不是第二个主标题）。
    assert float(style["font_size"]) < 11.5

def test_merged_card_is_taller_and_never_narrower():
    """带第二行的卡：高度增加、宽度不小于原卡，且第二行不折行。"""

    from cns_planner.gis.figure_style import LABEL_STYLES

    style = LABEL_STYLES["label_endpoint"]
    plain = label_card_geometry_with_secondary(END_NAME, style, "")
    merged = label_card_geometry_with_secondary(END_NAME, style, NAV_LINE)
    assert merged["width_mm"] >= plain["width_mm"]
    assert merged["height_mm"] > plain["height_mm"]
    assert merged["secondary_text"] == NAV_LINE
    assert merged["primary_text_height_mm"] + merged["secondary_line_height_mm"] \
        + 2.0 * merged["padding_mm"] == pytest.approx(merged["height_mm"])
    row = card_secondary_row({"x": 10.0, "y": 20.0}, merged["width_mm"], merged["height_mm"], merged)
    assert row is not None
    assert row[0] == pytest.approx(10.0)
    assert row[2] == pytest.approx(merged["width_mm"])
    assert row[1] + row[3] <= 20.0 + merged["height_mm"] + 1e-9


def test_merged_endpoint_keeps_the_endpoint_frame_colour():
    """主卡外框永远是起终点色：第二行只加底纹，不替换主卡边框。"""

    from cns_planner.gis.figure_style import LABEL_STYLES

    label = LabelSpec(
        kind="start", text=START_NAME, services=["navigation"],
        secondary_text=NAV_LINE, secondary_service="navigation",
    )
    segments = _label_card_segments(label, LABEL_STYLES["label_endpoint"])
    assert len(segments) == 1
    (fill, border, width), start, end = segments[0]
    assert border == LABEL_STYLES["label_endpoint"]["card_border"] == "#12a150"
    assert fill == "#ffffff"
    assert (start, end) == (0.0, 1.0)
    assert width == pytest.approx(float(LABEL_STYLES["label_endpoint"]["card_border_width_mm"]))
    end_label = LabelSpec(
        kind="end", text=END_NAME, services=["navigation"],
        secondary_text=NAV_LINE, secondary_service="navigation",
    )
    end_segments = _label_card_segments(end_label, LABEL_STYLES["label_endpoint_end"])
    assert end_segments[0][0][1] == "#d81b1b"
    # 普通服务标签仍按服务家族等分分块（既有方案不变）。
    proposal = LabelSpec(kind="cns_proposal", text="站 /通信/RID",
                         services=["communication", "rid"])
    proposal_segments = _label_card_segments(proposal, LABEL_STYLES["label_cns_proposal"])
    assert len(proposal_segments) == 2
    assert proposal_segments[0][0][1] == CNS_LABEL_CARD_COLORS["communication"]["border"]
    assert proposal_segments[0][1:] == (0.0, 0.5)
    assert proposal_segments[1][0][1] == CNS_LABEL_CARD_COLORS["rid"]["border"]
    assert proposal_segments[1][1:] == (0.5, 1.0)


# ============================================================ 3. 经纬度与比例尺

def test_grid_interval_is_still_005_and_only_left_bottom(tmp_path):
    """grid interval 仍 0.05°；仍只显示 left + bottom，仍水平、两位小数。"""

    from cns_planner.gis.qgis_figure_renderer import grid_intervals

    assert float(LAYOUT["grid_interval_deg"]) == 0.05
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        longitudes, latitudes = grid_intervals(spec)
        assert longitudes == pytest.approx(0.05)
        assert latitudes == pytest.approx(0.05)
    body = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8").split("def _configure_grid", 1)[1].split("def grid_interval_for", 1)[0]
    assert "ShowAll, QgsLayoutItemMapGrid.Top" not in body
    assert "ShowAll, QgsLayoutItemMapGrid.Right" not in body
    assert "OutsideMapFrame, QgsLayoutItemMapGrid.Left" in body
    assert "OutsideMapFrame, QgsLayoutItemMapGrid.Bottom" in body
    assert "InsideMapFrame, QgsLayoutItemMapGrid.Bottom" not in body
    assert "QgsLayoutItemMapGrid.Horizontal" in body
    assert "setAnnotationPrecision(2)" in body


def test_grid_label_band_fits_between_the_frame_and_the_disclosure():
    """框外经度标注带必须完全落在"地图框 → 披露条"的间距之内。"""

    band = float(LAYOUT["grid_font_size"]) / 72.0 * 25.4 + 0.9
    assert float(LAYOUT["footer_map_gap_mm"]) >= band


def test_scale_bar_moves_up_inside_the_frame_away_from_the_longitudes(tmp_path):
    """比例尺 0 | 2 | 4 km 不变，但整体向图内上移，且仍在地图框内。"""

    _, _, _, specs = _five_specs(tmp_path)
    spec = specs[0]
    assert float(LAYOUT["scale_bar_margin_mm"]) >= 12.0
    assert int(LAYOUT["scalebar_segments"]) == 3
    geometry = scale_bar_geometry(spec, _Rect(27_940.0, 27_080.0), spec.layout)
    assert float(geometry["segment_km"]) == pytest.approx(2.0)
    assert float(geometry["total_km"]) == pytest.approx(6.0)
    assert int(geometry["segments"]) == 3
    plan = spec.layout
    frame_bottom = plan["map_top_mm"] + plan["map_height_mm"]
    bar_bottom = float(geometry["top_mm"]) + float(geometry["height_mm"])
    assert bar_bottom <= frame_bottom
    # 比例尺最下沿（含数字标签）必须在地图框内，而且与"框外经度标注带"之间留足净空：
    # 框外经度带从框下沿开始约 font/72*25.4 + 0.9 mm，因此比例尺块下沿至少要再高
    # `scale_bar_margin_mm`（向上移动后的内距），远大于经度带的高度。
    label_top = float(geometry["top_mm"]) - float(geometry["label_gap_mm"]) \
        - float(geometry["label_height_mm"])
    label_bottom = float(geometry["top_mm"]) - float(geometry["label_gap_mm"])
    assert label_top < label_bottom <= frame_bottom
    assert frame_bottom - max(bar_bottom, label_bottom) == pytest.approx(
        float(LAYOUT["scale_bar_margin_mm"])
    )
    grid_band = float(LAYOUT["grid_font_size"]) / 72.0 * 25.4 + 0.9
    assert float(LAYOUT["scale_bar_margin_mm"]) > grid_band


def test_north_arrow_is_not_touched_by_this_round():
    """北箭头保持当前版本（尺寸 / 内距都不动）。"""

    assert float(LAYOUT["north_arrow_size_mm"]) == 12.0
    assert float(LAYOUT["north_arrow_margin_mm"]) == 4.0


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


# ============================================================ 4. 其它全部冻结

def test_everything_else_is_frozen(tmp_path):
    """地图框 / CRS / 半径 / 图例预算 / Radar selected_panel=0 全部不变。"""

    _, _, _, specs = _five_specs(tmp_path)
    frames = {
        (round(spec.layout["map_width_mm"], 6), round(spec.layout["map_height_mm"], 6))
        for spec in specs
    }
    assert frames == {(182.0, 176.2)}, frames
    for spec in specs:
        assert spec.layout["map_crs"] == "EPSG:32651"
        assert spec.layout["grid_crs"] == "EPSG:4326"
        assert spec.metadata["map_crs"] == "EPSG:32651"
        assert spec.metadata["grid_crs"] == "EPSG:4326"
        assert spec.annotations == []
    # 4 / 2 / 5 km 半径不变。
    radii = sorted({
        float(layer.source_detail["radius_m"]) for spec in specs for layer in spec.layers
        if layer.source_detail.get("radius_m") is not None
    })
    assert radii == [2000.0, 4000.0, 5000.0]
    # Radar selected_panel_count 仍是 0，且没有伪造站址。
    radar_spec = specs[2]
    radar_layer = next(
        layer for layer in radar_spec.layers if layer.layer_key == "cns_radar_limitation"
    )
    assert int(radar_layer.source_detail.get("selected_panel_count") or 0) == 0
    # 主标题 / 副标题 / 起终点 / 提案 / 图例字号全部冻结。
    assert float(LAYOUT["map_title_font_size"]) == 19.0
    assert float(LAYOUT["subtitle_font_size"]) == 10.5
    assert float(LAYOUT["legend_title_font_size"]) == 14.0
    assert float(LAYOUT["legend_item_font_size"]) == 10.5
    from cns_planner.gis.figure_style import LABEL_STYLES

    assert float(LABEL_STYLES["label_endpoint"]["font_size"]) == 12.5
    assert float(LABEL_STYLES["label_cns_proposal"]["font_size"]) == 11.5
    assert float(LABEL_STYLES["label_cns_facility"]["font_size"]) == 10.0
