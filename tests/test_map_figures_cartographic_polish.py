"""Round30-B1（Cartographic Polish）制图收口测试（纯计算层，不需要 PyQGIS）。

覆盖本轮 5 条硬约束：

1. **地图主体内没有大说明框**：CNS 五图的 ``FigureSpec.annotations`` 必须为空；
   完整披露进入 ``FigureSpec.metadata['disclosures']``，短披露进入地图框**之外**的
   ``layout['map_disclosure']``；Radar 能力限制仍然作为正式 legend item 存在；
2. **视觉等级**：正式字号重建（起终点 > selected CNS proposal > 既有设施 > 地名 >
   普通背景），并且图例行高由字号推导、五图统一；
3. **标签背景卡**：单业务一色、同址多业务按服务数**稳定等分**分块，且背景几何参与
   label collision；
4. **图例标题真正居中**：标题 item 横跨整个图例框内容宽度并水平居中；
5. **五图统一 + 只读**：版面档位是 ``cns_five_figure_v2``，五图共享 extent 与地图框，
   导图不写 state、不推进 revision，Radar 在 selected_panel=0 时绝不伪造站址。

QQGIS 真机量化验收（正圆比例、canvas CRS）在
``tests/test_map_figures_qgis_render.py`` 里，由 QGIS 解释器执行。
"""

from __future__ import annotations

import json

import pytest

from cns_planner.application.map_figure_service import MapFigureService
from cns_planner.gis.figure_legend import CNS_LEGEND_BALANCE_TOLERANCE, legend_geometry
from cns_planner.gis.figure_style import (
    CNS_LABEL_CARD_COLORS, CNS_LABEL_CARD_ORDER, CNS_LEGEND_GROUP_COLUMNS, LAYOUT,
    LABEL_STYLES,
)
from cns_planner.reporting.map_templates import (
    CNS_LAYOUT_PROFILE, CNS_LAYOUT_PROFILE_V1, CNS_SHARED_PARAMETERS,
)
from test_map_figures_cns_templates import (  # noqa: F401 - 复用同一套 canonical 形态
    REAL_EXISTING, _FakeRenderer, _Session, _layers, _legend_keys, _service, _state,
)

#: 五张 CNS 图的（模板 id, 模板覆盖参数）。
CNS_TARGETS = (
    ("communication_layout_v1", None),
    ("surveillance_layout_v1", {"surveillance_service": "rid_cooperative"}),
    ("surveillance_layout_v1", {"surveillance_service": "radar_noncooperative"}),
    ("navigation_layout_v1", None),
    ("cns_combined_v1", None),
)


def _five_specs(tmp_path, state=None):
    service, session, renderer = _service(tmp_path, state=state)
    specs = [
        service.build_figure(template_id=template_id, route_id="R0005",
                             parameter_overrides=overrides)
        for template_id, overrides in CNS_TARGETS
    ]
    return service, session, renderer, specs


# ============================================================ B1-02 说明框出图

def test_cns_five_figures_have_no_map_internal_annotation(tmp_path):
    """五张 CNS 图的地图主体里**不得**再有任何说明框。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        assert spec.annotations == [], spec.template_id
        assert spec.metadata["map_internal_annotations"] is False
        # 渲染器不会为 CNS 五图产出任何说明框版面项（没有输入就没有输出）。
        from cns_planner.gis import qgis_figure_renderer as renderer_module

        assert renderer_module.annotation_box_geometry is not None  # 通用能力仍在
        assert spec.to_dict()["annotations"] == []


def test_annotation_capability_is_not_deleted_for_other_templates():
    """只停用 CNS 五图对地图内说明框的消费，不改动通用 AnnotationSpec 能力。"""

    from cns_planner.gis.figure_spec import AnnotationSpec

    annotation = AnnotationSpec(
        annotation_id="kept", title="能力保留", lines=["route_overview 仍可使用说明框"],
        anchor="map_bottom_left", width_mm=82.0,
    )
    payload = annotation.to_dict()
    assert AnnotationSpec.from_dict(payload).to_dict() == payload
    # 通用版面常量仍在（annotation_* 没有被删除）。
    assert LAYOUT["annotation_font_size"] > 0
    assert LAYOUT["annotation_line_mm"] > 0


def test_radar_limitation_legend_item_and_offmap_disclosure_survive(tmp_path):
    """Radar 能力限制不许因为删除说明框而消失：正式 legend item + 图外短披露都在。"""

    _, _, _, specs = _five_specs(tmp_path)
    radar_spec = specs[2]
    keys = _legend_keys(radar_spec)
    assert "cns_radar_limitation" in keys
    item = next(
        entry for entry in radar_spec.legend_items
        if entry.layer_key == "cns_radar_limitation"
    )
    assert item.display_name == "非合作监视能力限制（无可行布设）"
    # 地图框**之外**的一条短披露（在 layout 里，渲染器据此画在图框下方）。
    assert "无可行布设" in radar_spec.layout["map_disclosure"]
    assert radar_spec.layout["map_disclosure"] == radar_spec.metadata["map_disclosure"]
    # 综合图同样保留这条短披露（它也是"没有 Radar 站址"的那张图）。
    assert "无可行布设" in specs[4].layout["map_disclosure"]
    # 通信 / 导航图没有 Radar 限制，因此不产生噪声披露。
    assert specs[0].layout["map_disclosure"] == ""
    assert specs[3].layout["map_disclosure"] == ""


def test_full_disclosures_are_recorded_in_metadata(tmp_path):
    """完整披露（含 Step6 门禁、半径语义、Radar 约束）逐字进入 metadata。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        disclosures = spec.metadata["disclosures"]
        assert disclosures, spec.template_id
        body = json.dumps(disclosures, ensure_ascii=False)
        assert "未通过 Step6 正式确认" in body
        for entry in disclosures:
            assert entry["title"] and entry["lines"]
            assert entry["disclosure_id"]
    radar_body = json.dumps(specs[2].metadata["disclosures"], ensure_ascii=False)
    assert "3 km" in radar_body and "90°" in radar_body
    assert "未放宽 range" in radar_body


# ============================================================ B1-01 视觉等级

def test_font_sizes_are_enlarged_and_visually_ranked():
    """正式字号重建：起终点 > selected CNS proposal > 既有设施 > 地名。"""

    endpoint = LABEL_STYLES["label_endpoint"]["font_size"]
    proposal = LABEL_STYLES["label_cns_proposal"]["font_size"]
    facility = LABEL_STYLES["label_cns_facility"]["font_size"]
    place = LABEL_STYLES["label_place"]["font_size"]
    turn = LABEL_STYLES["label_turn"]["font_size"]
    assert endpoint > proposal > facility > place
    assert turn <= place
    assert turn > LAYOUT["grid_font_size"]
    # Round30-B1 的 9.5 / 8.5 pt 必须被 B1.1 再放大一档（不得缩回 B1 小字号）。
    assert endpoint >= 12.5
    assert proposal >= 11.5
    assert facility >= 10.0
    assert place >= 9.0
    assert LAYOUT["map_title_font_size"] >= 19.0
    assert LAYOUT["subtitle_font_size"] >= 10.5
    assert LAYOUT["legend_title_font_size"] >= 14.0
    assert LAYOUT["legend_group_font_size"] >= 11.0
    assert LAYOUT["legend_item_font_size"] >= 10.5
    assert LAYOUT["grid_font_size"] >= 8.5
    assert LAYOUT["scalebar_font_size"] >= 9.0
    assert 8.5 <= LAYOUT["footer_disclosure_font_size"] <= 9.0
    # 经纬网标注仍低于图例条目；审计条仍是次要等级。
    assert LAYOUT["grid_font_size"] < LAYOUT["legend_item_font_size"]
    assert LAYOUT["footer_strip_font_size"] < LAYOUT["legend_item_font_size"]
    # 起终点是 bold；普通地名不是。
    assert LABEL_STYLES["label_endpoint"]["bold"] is True
    assert LABEL_STYLES["label_cns_proposal"]["bold"] is True
    assert LABEL_STYLES["label_place"]["bold"] is False


def test_no_fixed_label_box_constants_are_left():
    """固定的 44 × 5 mm 标签框硬顶必须消失（尺寸改为按文字计算）。"""

    assert "label_box_width_mm" not in LAYOUT
    assert "label_box_height_mm" not in LAYOUT
    for key in ("label_char_width_ratio", "label_ascii_width_ratio",
                "label_card_text_padding_mm", "label_collision_gap_mm"):
        assert key in LAYOUT, key


def test_legend_row_geometry_is_derived_from_the_new_font_sizes():
    """图例行高必须跟着字号一起放大（否则放大字号就会上下覆盖）。"""

    assert LAYOUT["legend_row_mm"] >= LAYOUT["legend_item_font_size"] / 72.0 * 25.4 * 1.5
    assert LAYOUT["legend_group_row_mm"] >= LAYOUT["legend_title_font_size"] / 72.0 * 25.4 * 0.8
    assert LAYOUT["legend_header_mm"] >= LAYOUT["legend_title_font_size"] / 72.0 * 25.4 * 1.4
    assert LAYOUT["legend_symbol_box_mm"] > 5.0
    assert LAYOUT["legend_text_gutter_mm"] >= 1.6


# ============================================================ B1-05 五图统一 + 不溢出

def test_five_figures_share_one_layout_profile_and_map_frame(tmp_path):
    _, _, _, specs = _five_specs(tmp_path)
    frames = {
        (round(spec.layout["map_width_mm"], 6), round(spec.layout["map_height_mm"], 6))
        for spec in specs
    }
    assert len(frames) == 1, frames
    for spec in specs:
        assert spec.layout["layout_profile"] == CNS_LAYOUT_PROFILE
        assert spec.metadata["layout_profile"] == CNS_LAYOUT_PROFILE
        assert spec.metadata["layout_profile_version"] == 2
        # Round30-A 的 v1 与此必须可区分。
        assert CNS_LAYOUT_PROFILE != CNS_LAYOUT_PROFILE_V1
        assert spec.parameters["layout_profile"] == CNS_LAYOUT_PROFILE
        # Round30-B1.1：图例最多 4 列（字号放大后仍保证单条文字不越过列边界）；
        # 实际使用的列数由内容均衡算法决定（在**真实装配**里 R0005 五图都用 3 列，
        # 见 test_map_figures_formal_polish 的列宽断言）。
        assert spec.parameters["legend_columns"] == 4
        assert 1 <= int(spec.layout["legend_columns"]) <= 4


def test_legend_never_overflows_the_page_or_its_own_box(tmp_path):
    """放大后的图例不许溢出：框高、行高与实际画出来的行必须一致且都在页面内。"""

    _, _, _, specs = _five_specs(tmp_path)
    page_bottom = 297.0 - float(LAYOUT["footer_band_mm"])
    for spec in specs:
        plan = spec.layout
        geometry = legend_geometry(
            _legend_entries(spec), row_height=float(plan["legend_row_mm"]),
            group_row=float(plan["legend_group_row_mm"]),
            columns=int(plan["legend_columns"]),
            header_height=float(LAYOUT["legend_header_mm"]),
            group_gap=float(plan["legend_group_gap_mm"]),
            group_item_gap=float(plan["legend_group_item_gap_mm"]),
            group_columns=CNS_LEGEND_GROUP_COLUMNS,
            top_padding=float(plan["legend_top_padding_mm"]),
            balance_tolerance=CNS_LEGEND_BALANCE_TOLERANCE,
        )
        # 实际行高与版面声明一致（renderer 与 _layout_plan 共用同一套几何）。
        assert geometry["row_mm"] == pytest.approx(plan["legend_row_mm"], abs=1e-9)
        # 框高必须容得下实际行（允许 1e-6 的浮点误差）。
        assert float(plan["legend_height_mm"]) + 1e-6 >= float(geometry["box_height_mm"]), (
            spec.template_id, plan["legend_height_mm"], geometry["box_height_mm"],
        )
        assert plan["legend_top_mm"] + plan["legend_height_mm"] <= page_bottom + 1e-6
        # 图例可用高度（页面下沿 − 图例上沿）必须容得下实际几何。
        available = (297.0 - float(LAYOUT["footer_band_mm"])) - plan["legend_top_mm"]
        assert available + 1e-6 >= float(geometry["box_height_mm"])
        # **地图框不因图例放大而缩小**：五图统一固定高度。
        assert plan["map_height_mm"] == pytest.approx(
            float(LAYOUT["cns_fixed_map_height_mm"]), abs=1e-9,
        )
        assert plan["map_width_mm"] == pytest.approx(182.0, abs=1e-9)
        # 正式图默认**不显示**工程审计条；披露条只在真有披露句时出现。
        assert plan["audit_footer"] is False
        assert plan["footer_strip_top_mm"] is None
        assert plan["disclosure_visible"] is bool(plan.get("map_disclosure"))
        if plan["disclosure_visible"]:
            map_bottom = plan["map_top_mm"] + plan["map_height_mm"]
            assert plan["footer_disclosure_top_mm"] >= map_bottom
            assert plan["legend_top_mm"] > plan["footer_disclosure_top_mm"]
        else:
            assert plan["footer_disclosure_top_mm"] is None


def _legend_entries(spec):
    return [
        {"group": item.legend_group, "text": item.display_name, "style_key": item.style_key}
        for item in spec.legend_items
    ]


def test_worst_case_legend_fits_the_shared_budget():
    """图例预算必须容得下 R0005 实际会出现的最大图例（综合图 7 组 12 条）。"""

    entries = [
        {"group": group, "text": text, "style_key": text}
        for group, items in (
            ("environment", ["海域", "陆地区域"]),
            ("obstacle", ["地形障碍（≥ 显示阈值）", "建筑障碍（≥ 显示阈值）"]),
            ("facility", ["既有通信站址"]),
            ("proposal", ["通信规划提案（未确认）", "RID 规划提案（未确认）",
                          "导航完整性监测点提案（未确认）"]),
            ("coverage", ["通信规划服务半径 4 km", "RID 陆上/沿海规划半径 2 km",
                          "RID 海上最大规划半径 5 km"]),
            ("limitation", ["非合作监视能力限制（无可行布设）"]),
            ("route", ["规划航路", "航路转弯点", "起点", "终点"]),
        )
        for text in items
    ]
    geometry = legend_geometry(
        entries, row_height=float(LAYOUT["legend_row_mm"]),
        group_row=float(LAYOUT["legend_group_row_mm"]),
        columns=int(CNS_SHARED_PARAMETERS["legend_columns"]),
        header_height=float(LAYOUT["legend_header_mm"]),
        group_gap=float(LAYOUT["legend_group_gap_mm"]),
        group_item_gap=float(LAYOUT["legend_group_item_gap_mm"]),
        group_columns=CNS_LEGEND_GROUP_COLUMNS,
        top_padding=float(LAYOUT["legend_top_padding_mm"]),
        balance_tolerance=CNS_LEGEND_BALANCE_TOLERANCE,
    )
    assert int(geometry["columns"]) == 3
    assert float(geometry["box_height_mm"]) <= float(
        LAYOUT["cns_legend_budget_height_mm"]
    )
    # B1.1：图例整体明显放大（B1 的 52.4 mm → 62 mm 量级）。
    assert float(geometry["box_height_mm"]) >= 58.0


def test_legend_title_spans_the_legend_box_and_is_centered():
    """图例标题必须横跨整个图例框内容宽度、水平居中（不是某个小文本框的左上角）。"""

    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    match = re.search(r'_add_label\(\s*\n\s*layout, "图　例",(?P<body>.*?)\n    \)', source,
                      re.DOTALL)
    assert match is not None, "图例标题的 _add_label 调用不存在"
    body = match.group("body")
    assert "align=Qt.AlignHCenter" in body
    assert "align=Qt.AlignLeft" not in body
    # 宽度必须是"整个图例框内容宽度"（左右内边距对称），不是固定小宽度。
    assert "width - 2.0 * side_padding" in body


# ============================================================ B1-03 标签背景卡

def test_label_card_size_grows_with_text_length():
    """标签卡尺寸按文字长度 / 字号 / padding 计算，不再被固定框硬顶。"""

    from cns_planner.gis.qgis_figure_renderer import label_card_size, label_text_width_mm

    short_style = LABEL_STYLES["label_place"]
    long_style = LABEL_STYLES["label_cns_proposal"]
    short_width, short_height = label_card_size("桃花岛", short_style)
    long_width, long_height = label_card_size("普陀桃花沙岙村H杆站 /通信/RID", long_style)
    assert long_width > short_width + 10.0
    assert long_height > short_height
    # 文字越长估算宽度越大（单调）。
    assert label_text_width_mm("桃", 9.5) < label_text_width_mm("桃花岛", 9.5)
    assert label_text_width_mm("ABC", 9.5) < label_text_width_mm("ABCDE", 9.5)


def test_single_service_label_card_is_one_block_with_the_right_colour():
    from cns_planner.gis.qgis_figure_renderer import label_card_segments

    assert label_card_segments(["communication"]) == [("communication", 0.0, 1.0)]
    assert label_card_segments(["rid"]) == [("rid", 0.0, 1.0)]
    assert label_card_segments(["radar"]) == [("radar", 0.0, 1.0)]
    assert label_card_segments(["navigation"]) == [("navigation", 0.0, 1.0)]
    # 颜色表就是权威配色：通信浅绿 / RID 浅橙 / Radar 浅蓝 / 导航浅黄（B1.1 更浅）。
    assert CNS_LABEL_CARD_COLORS["communication"]["fill"] == "#e8f7ec"
    assert CNS_LABEL_CARD_COLORS["rid"]["fill"] == "#fff3e4"
    assert CNS_LABEL_CARD_COLORS["radar"]["fill"] == "#e9f2fd"
    assert CNS_LABEL_CARD_COLORS["navigation"]["fill"] == "#fdf8dd"


def test_label_card_fills_are_very_light_and_borders_are_stronger():
    """B1.1：底色更浅、边框更清楚（不能是纯色大色块）。"""

    def _rgb(text):
        value = str(text).lstrip("#")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

    for family, palette in CNS_LABEL_CARD_COLORS.items():
        fill = _rgb(palette["fill"])
        border = _rgb(palette["border"])
        # 底色平均亮度很高（>= 232），且与边框有明显对比。
        assert sum(fill) / 3.0 >= 232.0, (family, fill)
        assert sum(fill) / 3.0 - sum(border) / 3.0 >= 40.0, (family, fill, border)
        assert float(palette["border_width_mm"]) >= 0.5, family


def test_multi_service_label_card_splits_by_service_count_in_a_stable_order():
    """同址多业务：按服务数等分分块，顺序固定（通信 → RID → Radar → 导航）。"""

    from cns_planner.gis.qgis_figure_renderer import label_card_segments

    # C + RID → 左半浅绿 + 右半浅橙。
    assert label_card_segments(["rid", "communication"]) == [
        ("communication", 0.0, 0.5), ("rid", 0.5, 1.0),
    ]
    # C + RID + Radar → 三等分。
    assert label_card_segments(["radar", "communication", "rid"]) == [
        ("communication", 0.0, 1 / 3), ("rid", 1 / 3, 2 / 3), ("radar", 2 / 3, 1.0),
    ]
    # 四业务 → 四等分，且顺序与 CNS_LABEL_CARD_ORDER 一致。
    assert label_card_segments(["navigation", "radar", "rid", "communication"]) == [
        ("communication", 0.0, 0.25), ("rid", 0.25, 0.5),
        ("radar", 0.5, 0.75), ("navigation", 0.75, 1.0),
    ]
    # 未登记的服务名不产生任何色块（绝不猜颜色）。
    assert label_card_segments(["unknown_service"]) == []
    assert label_card_segments([]) == []


def test_label_card_geometry_participates_in_collision():
    """背景几何必须参与 label collision（卡片矩形比文字框大，且含安全余量）。"""

    from cns_planner.gis.qgis_figure_renderer import label_card_box

    placement = {"x": 50.0, "y": 80.0, "align": None}
    pad = float(LAYOUT["label_collision_gap_mm"]) + float(LAYOUT["label_card_safety_mm"])
    box = label_card_box(placement, 40.0, 6.0, float(LAYOUT["label_collision_gap_mm"]))
    assert box == pytest.approx((50.0 - pad, 80.0 - pad, 90.0 + pad, 86.0 + pad))
    # 卡片矩形严格包含文字区域，且比文字区域更大（安全余量 > 0）。
    assert box[0] < placement["x"] and box[1] < placement["y"]
    assert box[2] > placement["x"] + 40.0 and box[3] > placement["y"] + 6.0
    assert pad > float(LAYOUT["label_collision_gap_mm"])


def test_colocated_services_get_one_label_with_stable_service_families(tmp_path):
    """同址 C + RID 必须合并成一条标签，并带上按固定顺序排好的服务家族。"""

    _, _, _, specs = _five_specs(tmp_path)
    combined = specs[4]
    colocated = [
        label for label in combined.labels
        if label.longitude == pytest.approx(122.228521)
        and label.latitude == pytest.approx(29.831441)
    ]
    assert len(colocated) == 1
    label = colocated[0]
    assert label.kind == "cns_proposal"
    assert label.style_key == "label_cns_proposal"
    assert label.services == ["communication", "rid"]
    assert "通信" in label.text and "RID" in label.text
    # 起终点标签：Round30-B1.2 起，与**导航监测提案同址**的起降点会把 Navigation
    # 并进主卡第二行（此时 services 带 navigation、secondary_text 非空且**没有**独立
    # 的导航卡）；没有同址导航提案的图仍保持 services 为空。
    endpoints = [item for item in combined.labels if item.kind in ("start", "end")]
    assert len(endpoints) == 2
    for item in endpoints:
        if item.secondary_text:
            assert item.services == ["navigation"], item.services
            assert item.secondary_service == "navigation"
        else:
            assert item.services == []
    # 综合图确实合并了两端（而不是把导航卡单独留着）。
    assert all(item.secondary_text for item in endpoints)
    navigation_cards = [
        item for item in combined.labels
        if item.kind == "cns_proposal" and "navigation" in (item.services or [])
    ]
    assert navigation_cards == []


def test_single_service_figures_get_single_block_backgrounds(tmp_path):
    """单业务图（通信 / RID / 导航）的站名标签只带一个服务家族。

    Round30-B1.2：导航图的监测点与起降点完全同址，因此导航卡**并入起终点主卡**，
    不再产生独立的 ``cns_proposal`` 标签；它的服务家族改为挂在起终点标签的
    ``services`` / ``secondary_text`` 上（底纹配色仍是同一个 navigation 家族）。
    """

    _, _, _, specs = _five_specs(tmp_path)
    communication_proposals = [
        item for item in specs[0].labels if item.kind == "cns_proposal"
    ]
    assert communication_proposals
    for label in communication_proposals:
        assert label.services == ["communication"]
    rid_proposals = [item for item in specs[1].labels if item.kind == "cns_proposal"]
    assert rid_proposals and all(item.services == ["rid"] for item in rid_proposals)

    navigation_spec = specs[3]
    assert [
        item for item in navigation_spec.labels if item.kind == "cns_proposal"
    ] == []
    for label in navigation_spec.labels:
        if label.kind in ("start", "end"):
            assert label.services == ["navigation"]
            assert label.secondary_text == "导航监测提案（未确认）"


def test_existing_cns_facility_labels_use_the_facility_style(tmp_path):
    """既有 CNS 设施用 ``label_cns_facility``（视觉等级高于普通地名）。"""

    state = _state()
    state["existing_cns_facilities"] = {
        "status": "passed", "collection_id": "existing-cns-facilities", "count": 1,
        "items": [json.loads(json.dumps(REAL_EXISTING))],
    }
    _, _, _, specs = _five_specs(tmp_path, state=state)
    combined = specs[4]
    facility_labels = [
        item for item in combined.labels if item.kind == "cns_existing"
    ]
    assert facility_labels
    for label in facility_labels:
        assert label.style_key == "label_cns_facility"
        assert label.services == ["communication"]
        assert label.priority == 90


# ============================================================ B1-06/09 CRS 口径

def test_map_canvas_crs_is_metric_and_grid_stays_wgs84(tmp_path):
    """地图画布 = EPSG:32651（米制）；经纬网 = EPSG:4326。"""

    from cns_planner.gis.qgis_figure_renderer import (
        DEFAULT_MAP_CRS, GRID_CRS_AUTHID, render_context, WGS84_AUTHID,
    )

    assert DEFAULT_MAP_CRS == "EPSG:32651"
    assert GRID_CRS_AUTHID == WGS84_AUTHID == "EPSG:4326"
    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        assert spec.layout["map_crs"] == DEFAULT_MAP_CRS
        assert spec.layout["grid_crs"] == WGS84_AUTHID
        assert spec.metadata["map_crs"] == DEFAULT_MAP_CRS
        assert spec.metadata["grid_crs"] == WGS84_AUTHID
        context = render_context(spec)
        assert context["map_crs"] == DEFAULT_MAP_CRS
        assert context["grid_crs"] == WGS84_AUTHID
        # FigureSpec 内的几何仍是 WGS84，因此必须走投影路径。
        assert context["requires_transform"] is True


def test_coverage_radii_are_unchanged_by_the_crs_switch(tmp_path):
    """只改坐标系口径，**绝不**改业务半径：4000 / 2000 / 5000 m 逐字不变。"""

    _, _, _, specs = _five_specs(tmp_path)
    comm_coverage = _layers(specs[0])["cns_coverage_comm"]
    assert comm_coverage.data["radius_m"] == 4000.0
    assert comm_coverage.source_detail["radius_m"] == 4000.0
    rid_layers = _layers(specs[1])
    assert rid_layers["cns_coverage_rid_land"].data["radius_m"] == 2000.0
    assert rid_layers["cns_coverage_rid_sea"].data["radius_m"] == 5000.0
    for spec in specs:
        for layer in spec.layers:
            radius = layer.source_detail.get("radius_m")
            if radius is not None:
                assert float(radius) in (2000.0, 4000.0, 5000.0), (
                    spec.template_id, layer.layer_key, radius,
                )


def test_canvas_crs_metadata_is_not_a_label_only_change(tmp_path):
    """CRS 修改必须完整：canvas / grid / 几何路径 / extent 口径都要一致。"""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "cns_planner" / "gis" / "qgis_figure_renderer.py"
    ).read_text(encoding="utf-8")
    # 内存图层的 CRS 来自 render_context，而不是硬编码的 4326。
    assert 'QgsVectorLayer(f"{geometry_name}?crs={map_crs}"' in source
    assert "?crs=EPSG:4326" not in source
    # extent 在设置前必须投影到画布 CRS。
    assert "projected_extent(" in source
    # 标注锚点也要投影。
    assert "projected_point(" in source
    # 经纬网 CRS 单独设置，且是 WGS84。
    assert 'context.get("grid_crs") or GRID_CRS_AUTHID' in source


# ============================================================ 只读 / 不伪造

def test_export_does_not_touch_state_revision_or_radar(tmp_path):
    """导图仍只读：不写 state、不推进 revision、不伪造 Radar 站址。"""

    service, session, renderer, specs = _five_specs(tmp_path)
    radar_layer = _layers(specs[2])["cns_radar_limitation"]
    assert radar_layer.feature_count == 0
    assert radar_layer.source_detail["selected_panel_count"] == 0
    assert radar_layer.source_detail["manufactured_radar_sites"] == 0
    assert radar_layer.source_detail["drawn_radar_sectors"] == 0
    context_points = _layers(specs[2])["cns_radar_context"].data["points"]
    assert all(point["radar_selected"] is False for point in context_points)
    for spec in specs:
        service._render(spec, dpi=300.0)  # noqa: SLF001 - 断言渲染路径同样只读
    assert session.saved == 0
    assert session.state["revision"] == 410


def test_five_figures_extent_and_scale_stay_identical(tmp_path):
    """放大字号 / 改 CRS 之后，五图仍共享同一个 extent 与同一个地图框。"""

    _, _, _, specs = _five_specs(tmp_path)
    extents = {tuple(round(value, 9) for value in spec.extent.as_list()) for spec in specs}
    assert len(extents) == 1
    aspects = {round(spec.layout["extent_aspect"], 9) for spec in specs}
    assert len(aspects) == 1
    for spec in specs:
        assert spec.extent_evidence["layout_profile"] == CNS_LAYOUT_PROFILE
        assert spec.extent_evidence["cns_five_figure_shared_extent"] is True


def test_build_figure_does_not_recompute_business_results(tmp_path):
    """制图链路不得重算任何业务结论（boundaries 里逐条声明）。"""

    _, _, _, specs = _five_specs(tmp_path)
    for spec in specs:
        boundaries = spec.boundaries
        assert boundaries["read_only"] is True
        assert boundaries["writes_state"] is False
        assert boundaries["recomputes_business_results"] is False
        assert boundaries["manufactures_radar_sites"] is False
        assert boundaries["proposal_never_drawn_as_confirmed"] is True
        assert spec.metadata["layout_engine"] == (
            "cns_canonical_figurespec_to_qgis_printlayout"
        )
