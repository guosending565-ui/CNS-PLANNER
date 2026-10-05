"""专题制图的样式目录（**地图与图例共用的唯一符号定义**）。

本模块是 ``route_overview_v1`` 视觉语言的单一定义处：

* 地图上的图层通过 :func:`fill_symbol` / :func:`line_symbol` / :func:`marker_symbol`
  取到 QgsSymbol；
* 图例中的条目通过 :func:`symbol_preview_image` 用**同一个** QgsSymbol 渲染成图片。

因此"图例符号必须与地图完全一致"不是靠人眼对齐，而是同一份定义的两次渲染。

视觉优先级（本轮的固定约束）：
    海域 / 陆地 → 空域 → 障碍物 → 既有铁塔 / 铁塔障碍 → 机场 → **规划航路** → 起终点。
    规划航路与起终点永远在最上层，绝不被障碍物图层覆盖。
"""

from __future__ import annotations

from copy import deepcopy

#: 图层键 → 样式定义。``kind`` 决定渲染方式，``geometry`` 决定图层几何类型。
#: ``z`` 决定视觉层级（渲染器按 z **降序**传给 QGIS，即列表首个在最上层）：
#: 海域 → 陆地 → 地形障碍 → 建筑障碍 → 机场保护范围 → 既有铁塔 → 铁塔障碍
#: → 规划航路 → 航路转弯点 → 起点/终点。规划航路与起终点永远压在障碍面之上。
FIGURE_STYLES = {
    "sea": {
        "kind": "fill", "geometry": "polygon", "z": 1,
        "fill": "#cfe6f5", "outline": "#a9c8dd", "outline_width": 0.0,
    },
    "land": {
        "kind": "fill", "geometry": "polygon", "z": 2,
        "fill": "#ffffff", "outline": "#4c5a68", "outline_width": 0.35,
    },
    "airspace": {
        "kind": "fill", "geometry": "polygon", "z": 3,
        "fill": "#9fd9a3", "outline": "#4c9c58", "outline_width": 0.25, "opacity": 0.38,
    },
    "terrain_obstacle": {
        # Round30-B1.1：只降低 fill 的视觉重量（opacity / saturation 下调、轮廓略细），
        # 不改变 threshold、不改变几何、不改变任何业务数据。
        "kind": "fill", "geometry": "polygon", "z": 4,
        "fill": "#ffa64d", "outline": "#c4761f", "outline_width": 0.18, "opacity": 0.55,
    },
    "building_obstacle": {
        "kind": "fill", "geometry": "polygon", "z": 5,
        "fill": "#e85c5c", "outline": "#a83a3a", "outline_width": 0.18, "opacity": 0.55,
    },
    "airport_protection": {
        "kind": "fill", "geometry": "polygon", "z": 6,
        "fill": "transparent", "outline": "#ff7a00",
        "outline_width": 0.5, "outline_style": "dash",
    },
    "tower_existing": {
        # 普通既有铁塔**退到背景层**：尺寸缩小约 18%、描边略细、整体 ~78% 不透明。
        # 只改样式，不删数据、不动坐标。
        "kind": "marker", "geometry": "point", "z": 8,
        "shape": "triangle", "size": 2.3, "fill": "#ffffff",
        "outline": "#8a5ec0", "outline_width": 0.4, "angle": 0.0, "opacity": 0.78,
    },
    "tower_obstacle": {
        "kind": "marker", "geometry": "point", "z": 9,
        "shape": "triangle", "size": 3.2, "fill": "#7b2fbe",
        "outline": "#4b1a75", "outline_width": 0.35, "angle": 0.0,
    },
    "airport": {
        "kind": "marker", "geometry": "point", "z": 10,
        "shape": "square", "size": 3.4, "fill": "#ff7a00",
        "outline": "#8f3f00", "outline_width": 0.4, "angle": 45.0,
    },
    "planned_route": {
        "kind": "line", "geometry": "line", "z": 20,
        "color": "#123a6b", "width": 1.5, "capstyle": "round", "joinstyle": "round",
    },
    "turn_point": {
        "kind": "marker", "geometry": "point", "z": 21,
        "shape": "circle", "size": 2.4, "fill": "#ffffff",
        "outline": "#123a6b", "outline_width": 0.45,
    },
    "start_point": {
        "kind": "marker", "geometry": "point", "z": 22,
        "shape": "star", "size": 6.4, "fill": "#12a150",
        "outline": "#084d26", "outline_width": 0.4,
    },
    "end_point": {
        "kind": "marker", "geometry": "point", "z": 23,
        "shape": "star", "size": 6.4, "fill": "#d81b1b",
        "outline": "#7a0d0d", "outline_width": 0.4,
    },
    # ---- CNS 专题图（服务主色：通信绿 / RID 橙 / Radar 蓝 / 导航完整性黄）--------
    #
    # 既有设施 = **实心 + 实线**；P16 selected_action = **空心 + 虚线描边**，
    # 视觉上永远与"已建成"区分开。覆盖圈只画规划服务半径（planning service radius），
    # 不是实测无线传播等值线。Radar 在 R0005 无选中站址，因此只有"候选上下文"与
    # "能力限制"两种蓝色表达，绝不出现扇区或新建站。
    "cns_coverage_comm": {
        "kind": "fill", "geometry": "polygon", "z": 7,
        "fill": "transparent", "outline": "#1a9c4a",
        "outline_width": 0.45, "outline_style": "dash",
        # 覆盖圈在地图上是**真实的米制圆**；图例符号也用圆环表达，
        # 否则用户会以为地图上画的是矩形区域。
        "shape": "circle",
    },
    "cns_coverage_rid_land": {
        "kind": "fill", "geometry": "polygon", "z": 7,
        "fill": "transparent", "outline": "#e8720c",
        "outline_width": 0.55, "outline_style": "solid", "shape": "circle",
    },
    "cns_coverage_rid_sea": {
        "kind": "line", "geometry": "line", "z": 7,
        "color": "#e8720c", "width": 0.55, "line_style": "dash",
    },
    "cns_radar_context": {
        "kind": "marker", "geometry": "point", "z": 11,
        "shape": "triangle", "size": 2.6, "fill": "transparent",
        "outline": "#1f6fd0", "outline_width": 0.45, "angle": 0.0,
    },
    "cns_radar_sector": {
        "kind": "fill", "geometry": "polygon", "z": 8,
        "fill": "#7fb7ff", "opacity": 0.28, "outline": "#1f6fd0",
        "outline_width": 0.55, "outline_style": "dash",
    },
    "cns_radar_proposal": {
        "kind": "marker", "geometry": "point", "z": 16,
        "shape": "triangle", "size": 5.2, "fill": "#ffffff",
        "outline": "#1f6fd0", "outline_width": 0.75, "outline_style": "dash",
    },
    "cns_existing": {
        "kind": "marker", "geometry": "point", "z": 12,
        "shape": "circle", "size": 4.0, "fill": "#1a9c4a",
        "outline": "#0d5c2b", "outline_width": 0.45,
    },
    "cns_comm_proposal": {
        "kind": "marker", "geometry": "point", "z": 13,
        "shape": "circle", "size": 4.6, "fill": "transparent",
        "outline": "#1a9c4a", "outline_width": 0.6, "outline_style": "dash",
    },
    "cns_rid_proposal": {
        "kind": "marker", "geometry": "point", "z": 14,
        "shape": "square", "size": 4.4, "fill": "transparent",
        "outline": "#e8720c", "outline_width": 0.6, "outline_style": "dash", "angle": 45.0,
    },
    "cns_nav_proposal": {
        # 导航完整性监测点与**起降点星标完全同址**（坐标逐字节一致，绝不偏移）。
        # 因此它用**更大的空心圆环**表达"同心环"，既不会被星标盖住，也不遮住星标本身。
        # Round30-B1.2：符号本身不动；同址的文字卡不再单独出图（并入起降点主卡第二行）。
        "kind": "marker", "geometry": "point", "z": 24,
        "shape": "circle", "size": 11.6, "fill": "transparent",
        "outline": "#d19a00", "outline_width": 0.75, "outline_style": "dash",
    },
    "cns_radar_limitation": {
        "kind": "marker", "geometry": "point", "z": 16,
        "shape": "cross", "size": 4.4, "fill": "transparent",
        "outline": "#1f6fd0", "outline_width": 0.6, "outline_style": "dash",
    },
}

#: 标注样式（中文 + 背景卡）。优先级数值越大越优先显示。
#:
#: Round30-B1.1（Formal Figure Visual Polish）在 B1 基础上再放大一档，并冻结为**正式字号**：
#:
#:     起终点（12.5 pt，bold，白底 + 服务色强边框）
#:       > selected CNS proposal（11.5 pt，bold）
#:       > 既有 CNS 设施（10 pt，bold）
#:       > 普通地名 / 转弯点（9 pt）
#:       > ordinary tower context（不标名）
#:
#: ``max_width_mm`` / ``max_lines`` 控制**自动换行**（正式图禁止用省略号）：文字先按
#: 自然断点折行，只有超过最大行数时才退回截断（``ellipsis``）。
LABEL_STYLES = {
    "label_endpoint": {
        "font_size": 12.5, "color": "#12233a", "halo_color": "#ffffff",
        "halo_size": 1.6, "bold": True, "offset_mm": 9.0, "card_padding_mm": 1.8,
        "card_fill": "#ffffff", "card_border": "#12a150", "card_border_width_mm": 0.9,
        "max_width_mm": 52.0, "max_lines": 3, "leader": True,
    },
    "label_endpoint_end": {
        # 终点：同一套几何，但边框与 leader 用终点红（由渲染器按 kind 选用）。
        "font_size": 12.5, "color": "#12233a", "halo_color": "#ffffff",
        "halo_size": 1.6, "bold": True, "offset_mm": 9.0, "card_padding_mm": 1.8,
        "card_fill": "#ffffff", "card_border": "#d81b1b", "card_border_width_mm": 0.9,
        "max_width_mm": 52.0, "max_lines": 3, "leader": True,
    },
    "label_turn": {
        "font_size": 9.0, "color": "#123a6b", "halo_color": "#ffffff",
        "halo_size": 1.2, "bold": False, "offset_mm": 4.2, "card_padding_mm": 1.1,
        "max_width_mm": 50.0, "max_lines": 2, "leader": False,
    },
    "label_place": {
        "font_size": 9.0, "color": "#4a5560", "halo_color": "#ffffff",
        "halo_size": 1.2, "bold": False, "offset_mm": 3.6, "card_padding_mm": 1.1,
        "max_width_mm": 50.0, "max_lines": 2, "leader": False,
    },
    # CNS 站点标签：浅色背景卡（多业务等分分块），文字深色、加粗。
    "label_cns_facility": {
        "font_size": 10.0, "color": "#16212b", "halo_color": "#ffffff",
        "halo_size": 1.2, "bold": True, "offset_mm": 4.6, "card_padding_mm": 1.5,
        "max_width_mm": 54.0, "max_lines": 3, "leader": True,
    },
    "label_cns_proposal": {
        "font_size": 11.5, "color": "#111b24", "halo_color": "#ffffff",
        "halo_size": 1.2, "bold": True, "offset_mm": 5.4, "card_padding_mm": 1.8,
        "max_width_mm": 54.0, "max_lines": 3, "leader": True,
    },
}

#: CNS 标签背景卡的**官方配色**（Round30-B1.1 起为"很浅的底 + 稍清楚的边"）。
#:
#: 顺序固定为 communication → RID → radar → navigation，同址多业务按**该顺序**等分
#: 分块着色（左侧第一块永远是通信绿），因此同一组服务在五张图上永远是同一套分块颜色。
CNS_LABEL_CARD_COLORS = {
    "communication": {
        "fill": "#e8f7ec", "border": "#2f9d5b", "border_width_mm": 0.5,
        "leader": "#1a9c4a", "service_key": "C:communication",
    },
    "rid": {
        "fill": "#fff3e4", "border": "#d97b1e", "border_width_mm": 0.5,
        "leader": "#e8720c", "service_key": "S:rid_cooperative",
    },
    "radar": {
        "fill": "#e9f2fd", "border": "#3a7fc4", "border_width_mm": 0.5,
        "leader": "#1f6fd0", "service_key": "S:radar_noncooperative",
    },
    "navigation": {
        "fill": "#fdf8dd", "border": "#a98a12", "border_width_mm": 0.5,
        "leader": "#d19a00", "service_key": "N:navigation_integrity_monitoring",
    },
}

#: 多业务标签卡的分块顺序（**唯一**顺序来源；分块宽度 = 卡宽 / 服务数）。
CNS_LABEL_CARD_ORDER = ("communication", "rid", "radar", "navigation")

#: Round30-B1.2：同址「起降点 + 导航监测提案」合并后，主卡**第二行**（Navigation
#: service tag）的样式。它只描述那一行的文字与浅黄色底纹：
#:
#: * 主卡外框仍是起终点色（起点绿 / 终点红），第二行只加一条浅黄底带；
#: * 因此"起降点"与"导航监测提案（未确认）"各占一行、同一张卡、同一条引线；
#: * 配色取自 :data:`CNS_LABEL_CARD_COLORS` 的 ``navigation``，与雷达 / 通信 / RID
#:   的分色方案**同源**，不引入第二套颜色定义。
CNS_LABEL_SECONDARY_STYLE = {
    "fill_family": "navigation",
    "text_color": "#4a3a00",
    "font_size": 9.5,
    "bold": True,
}

#: 版面常量（毫米）。与模板参数互补：这里是"固定版式"，不是业务参数。
#: A4 竖版 210×297：标题带 → 地图 →（可选披露条 / 审计条）→ 图例框 → 页脚。
#:
#: Round30-B1.1：整体文字再放大一档（主标题 19 pt / 图例条目 10.5 pt），
#: 但**地图框尺寸不缩小**（182.0 × 176.2 mm，与 B1 的 176.4 mm 基本一致），
#: 放大的图例利用 A4 页面底部剩余空间展开。
LAYOUT = {
    "page_margin_mm": 14.0,
    # 标题带 = 主标题（19 pt）+ 固定间距 + 副标题（10.5 pt）+ 固定间距。
    "title_band_mm": 25.0,
    "title_gap_mm": 1.8,
    "title_map_gap_mm": 3.0,
    "subtitle_band_mm": 5.2,
    "footer_band_mm": 4.0,
    # CNS 五图的**地图框高度**（毫米）：五图严格一致，且不因图例内容变化。
    # 182 × 176.2 mm；B1 为 176.4 mm，差值 0.2 mm（正文尺寸不变，仅消除版面舍入差）。
    "cns_fixed_map_height_mm": 176.2,
    # 地图框下沿之外的两条薄带（都可开关）：
    #   1) 图面披露短句（Radar 能力限制等，属**业务披露**，正式图也保留）；
    #   2) 工程审计条（地图 CRS / 经纬网 CRS / revision / 未显示图层，**正式图默认关闭**）。
    "footer_disclosure_mm": 4.6,
    "footer_strip_mm": 3.8,
    # Round30-B1.2：经度标注移到**地图框之外**的下侧，因此"地图框 → 披露条"的间距从
    # 1.8 mm 放宽到 5.0 mm，保证框外经度标注带**完全不与披露条重叠**。
    "footer_map_gap_mm": 5.0,
    "footer_legend_gap_mm": 3.0,
    # 兼容键：地图与图例之间的总间距 = 上面三段之和（_layout_plan 会显式给出三段）。
    "map_legend_gap_mm": 13.2,
    "footer_disclosure_font_size": 9.0,
    "footer_disclosure_color": "#33414f",
    "footer_strip_font_size": 6.8,
    "footer_strip_color": "#687581",
    "map_frame_color": "#2b3a4a",
    "map_frame_width_mm": 0.4,
    "map_background": "#cfe6f5",
    "map_title_font_size": 19.0,
    "map_title_color": "#12233a",
    "subtitle_font_size": 10.5,
    "subtitle_color": "#41505e",
    "legend_box_fill": "#ffffff",
    "legend_box_border": "#2b3a4a",
    "legend_box_width_mm": 0.35,
    # 图例：标题独占一行、**横跨整个图例框内容宽度并水平居中**（见 renderer）。
    "legend_title_font_size": 14.0,
    "legend_group_font_size": 11.0,
    "legend_item_font_size": 10.5,
    "legend_item_text_color": "#1c2733",
    "legend_group_text_color": "#2f3a45",
    # 统一二维网格：符号框固定宽度、文本固定起始 x、行高统一。
    "legend_symbol_box_mm": 5.8,
    "legend_text_gutter_mm": 1.6,
    # 行高 / 组标题行高：与 10.5 / 11 pt 字号配套（约 1.55 倍 / 1.45 倍行距）。
    "legend_header_mm": 8.0,
    "legend_top_padding_mm": 1.5,
    "legend_row_mm": 5.8,
    "legend_group_row_mm": 5.8,
    # 组标题 → 组内条目：小间距；组与组之间：更大但统一的间距。
    "legend_group_item_gap_mm": 0.9,
    "legend_group_gap_mm": 1.8,
    "legend_column_gap_mm": 6.0,
    "legend_side_padding_mm": 4.5,
    "legend_min_row_mm": 4.6,
    "legend_min_group_row_mm": 4.6,
    "legend_default_columns": 2,
    # B1.1：字号放大后单条图例文字更宽，列数上限放宽到 4（实际列数仍由均衡算法决定）。
    "max_legend_columns": 4,
    "cns_legend_columns": 4,
    # 经纬网（adaptive interval）：目标每轴 5~7 个主刻度。
    "grid_interval_deg": 0.05,
    "grid_target_ticks": 6,
    "grid_min_ticks": 3,
    "grid_max_ticks": 9,
    # 经纬网标注：次要视觉等级（必须低于普通地名等正文标注），B1.1 取目标区间的下沿，
    # 因为同一轴上的刻度比地名更密，字号一致反而会更抢眼。
    "grid_font_size": 8.5,
    "grid_color": "#33414f",
    "grid_line_color": "#ccd5dd",
    "grid_line_width_mm": 0.14,
    "grid_frame_color": "#93a1ae",
    # 比例尺（程序化绘制的黑白分段条）：0 | 2 | 4 km 三段，刻度与数字放大。
    #
    # Round30-B1.2：经度标注移到**地图框之外**的下侧，因此比例尺要在地图**内部**再上移
    # 一档：``margin`` = 12.0 mm（原 6.4 mm）使比例尺（含数字标签与刻度）与框外经度标注
    # 完全不重叠，同时仍留在图框内、不压北箭头。
    "scale_bar_margin_mm": 12.0,
    "scalebar_font_size": 9.5,
    "scalebar_height_mm": 2.6,
    "scalebar_segments": 3,
    "scalebar_target_min_mm": 20.0,
    "scalebar_target_max_mm": 56.0,
    "scalebar_border_width_mm": 0.3,
    "scalebar_label_gap_mm": 1.1,
    "scalebar_label_height_mm": 4.2,
    "scalebar_tick_height_mm": 0.9,
    "scalebar_fill_light": "#ffffff",
    "scalebar_fill_dark": "#111111",
    "scalebar_line_color": "#111111",
    # 北箭头：比 B1 放大 20%（10 → 12 mm），但仍在图框内、不压地图内容。
    "north_arrow_size_mm": 12.0,
    "north_arrow_margin_mm": 4.0,
    "map_credit_font_size": 4.6,
    # ---- 标签卡 --------------------------------------------------------------
    # 卡片尺寸按"折行后的文字宽度 + 字号 + padding"计算（不再用固定框硬顶）。
    "label_char_width_ratio": 1.0,
    "label_ascii_width_ratio": 0.54,
    "label_card_text_padding_mm": 1.4,
    "label_card_border_width_mm": 0.5,
    "label_point_clearance_mm": 1.8,
    # 标注避让：已放置标签卡之间保留的最小净空（毫米）。
    "label_collision_gap_mm": 1.0,
    # 标签卡避让矩形的额外安全余量（吸收"估算文字宽度 vs 实际渲染宽度"的差异）。
    "label_card_safety_mm": 0.6,
    # 被别的卡片挡住时，允许沿垂直方向按该步长试探避让的档数。
    "label_reposition_step_mm": 3.0,
    "label_reposition_limit": 12,
    # ---- leader line ---------------------------------------------------------
    # Round30-B1.2：引线只做"归属提示"，不再做"穿越大半张图的连线"。
    #
    # * 线宽 1.1 → 0.45 mm（约 0.4~0.5 mm 目标区间）；
    # * **硬上限** :data:`LAYOUT['leader_line_max_length_mm']` = 15 mm：
    #   超过它就不再拉长引线，而是**为标签换一个靠近锚点的位置**；
    #   换不到位置时该卡按既有优先级规则被抑制（宁可少一张卡，也不画超大 L 形折线）；
    # * 走线优先级：**短直线 > 短折线**，折线只在直线被标注卡挡住时才使用。
    "leader_line_threshold_mm": 4.0,
    "leader_line_width_mm": 0.45,
    "leader_line_endpoint_gap_mm": 1.4,
    "leader_line_elbow_mm": 2.6,
    "leader_line_min_length_mm": 4.0,
    #: 引线**建议最大长度**（毫米）：超限时重新选标签位置，而不是继续拉长引线。
    "leader_line_max_length_mm": 15.0,
    #: 引线颜色的视觉权重（alpha）：跟业务色不变，只降低不透明度。
    "leader_line_alpha": 0.82,
    # 引线走线避让已放置标签卡时，障碍矩形四周额外留出的净空。
    "leader_line_obstacle_margin_mm": 1.0,
    "leader_line_start_color": "#12a150",
    "leader_line_end_color": "#d81b1b",
    # 顺移后 card 与 anchor 距离超过该值时发起"锚点净空搜索"（避免 leader 过长）。
    # B1.2：门槛收到引线上限附近，使"搜索"成为常规路径而不是兜底路径。
    "label_anchor_search_threshold_mm": 12.0,
    # ---- CNS 五图统一版式 ------------------------------------------------------
    # 图例预算：按综合图在三列下的真实需求取值（B1.1 实测 62.2 mm）+ 少量余量。
    "cns_legend_budget_height_mm": 64.0,
    "cns_min_map_height_mm": 140.0,
    # 工程审计条（地图 CRS / 经纬网 CRS / revision / 未显示图层）的默认开关：
    # **正式图默认关闭**，review 模式可由模板参数打开。
    "audit_footer_default": False,
    # 图面说明框（通用 AnnotationSpec，供 route_overview / route_detail 等模板使用）。
    # CNS 五图不消费地图内部大说明框，但这项能力本身保留。
    "annotation_font_size": 6.1,
    "annotation_title_font_size": 6.9,
    "annotation_line_mm": 4.6,
    "annotation_title_line_mm": 5.2,
    "annotation_padding_mm": 2.2,
    "annotation_border_color": "#1f6fd0",
    "annotation_border_width_mm": 0.35,
    "annotation_fill": "#ffffff",
    "annotation_text_color": "#1c2733",
    "annotation_title_color": "#10233a",
}

#: 图例分组标题与顺序。
#:
#: ``route_overview_v1`` 只使用 environment / obstacle / facility / route 四组，
#: 顺序与以前完全一致（历史版式不变）；CNS 专题图使用其中的 environment / obstacle
#: 加上既有 CNS、规划提案、覆盖能力、能力限制四组。当前项目没有机场数据，因此
#: facility 组名就是"既有设施"；以后机场进入模板时由模板动态决定对应分组名，
#: 不在这里硬编码"与机场"。
LEGEND_GROUPS = (
    ("environment", "地理环境"),
    ("obstacle", "障碍物"),
    ("facility", "既有设施"),
    ("cns_existing", "既有 CNS 设施"),
    ("proposal", "规划提案（未确认）"),
    ("coverage", "覆盖 / 能力"),
    ("limitation", "能力限制"),
    ("route", "规划航路"),
)

#: 图例**两列布局**的语义分列（组名 → 列号 0/1）——``route_overview_v1`` 专用。
#:
#: 分组本身已经决定了最自然的左右划分：左列放地理环境与障碍物，右列放既有设施与
#: 规划航路。这既是产品建议的分组，也让两列条目数接近（4 : 5），因此图例框不会出现
#: "一列很高、另一列一半空白"。某些组整体缺席时（例如当前没有机场 / 没有障碍物），
#: :func:`cns_planner.gis.figure_legend.legend_geometry` 会按**实际出现的组**分列；
#: 一旦出现未登记的组，则回退到自动均衡分列算法。
LEGEND_GROUP_COLUMNS = {
    "environment": 0,
    "obstacle": 0,
    "facility": 1,
    "route": 1,
}

#: CNS 专题图的语义分列：按"环境/障碍/站址背景/提案"与"覆盖/限制/航路"左右铺开，
#: 使条目最多的 CNS 综合图两列高度接近。任何未登记的组都会让整体回退到自动均衡分列。
CNS_LEGEND_GROUP_COLUMNS = {
    "environment": 0,
    "obstacle": 0,
    "facility": 0,
    "cns_existing": 0,
    "proposal": 0,
    "coverage": 1,
    "limitation": 1,
    "route": 1,
}

#: 图层键 → 图例分组。
#: 注意：``airspace`` 仍然登记在册（供其它模板复用），但 route_overview_v1
#: **不再 materialize 空域图层**，因此它不会出现在这张图的图例里。
LEGEND_GROUP_OF = {
    "sea": "environment",
    "land": "environment",
    "airspace": "facility",
    "terrain_obstacle": "obstacle",
    "building_obstacle": "obstacle",
    "tower_existing": "facility",
    "tower_obstacle": "facility",
    "airport": "facility",
    "airport_protection": "facility",
    "planned_route": "route",
    "turn_point": "route",
    "start_point": "route",
    "end_point": "route",
    # ---- CNS 专题图 ----
    "cns_existing": "cns_existing",
    "cns_comm_proposal": "proposal",
    "cns_rid_proposal": "proposal",
    "cns_nav_proposal": "proposal",
    "cns_coverage_comm": "coverage",
    "cns_coverage_rid_land": "coverage",
    "cns_coverage_rid_sea": "coverage",
    "cns_radar_context": "limitation",
    "cns_radar_sector": "coverage",
    "cns_radar_proposal": "proposal",
    "cns_radar_limitation": "limitation",
}

#: 中文字体候选：Windows 优先系统自带中文字体，缺失时回退到 QGIS 自带字体。
FONT_CANDIDATES = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
    "SimSun",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "DejaVu Sans",
)

#: Windows 中文字体文件候选（只引用系统已安装字体，**绝不**把字体文件提交进仓库）。
FONT_FILE_CANDIDATES = (
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/msyh.ttf",
    r"C:/Windows/Fonts/simhei.ttf",
    r"C:/Windows/Fonts/simsun.ttc",
)


def style(style_key):
    """取样式定义（副本）；未知键抛 ``KeyError``，绝不静默用一个默认样式顶替。"""

    if style_key not in FIGURE_STYLES:
        raise KeyError(f"未登记的专题图样式：{style_key}")
    return deepcopy(FIGURE_STYLES[style_key])


def label_style(label_key):
    if label_key not in LABEL_STYLES:
        raise KeyError(f"未登记的标注样式：{label_key}")
    return deepcopy(LABEL_STYLES[label_key])


def legend_order_key(layer_key, order=None):
    """图例稳定排序键：按 :data:`LEGEND_GROUPS` 分组，再按模板声明的图层顺序。

    ``order`` 缺省沿用 ``route_overview_v1`` 的图层顺序（历史行为不变）；
    CNS 专题图传入 :data:`cns_planner.reporting.map_templates.CNS_LEGEND_ORDER`。
    """

    from ..reporting.map_templates import ROUTE_OVERVIEW_LEGEND_ORDER

    sequence = tuple(order) if order else ROUTE_OVERVIEW_LEGEND_ORDER
    group = LEGEND_GROUP_OF.get(layer_key, "zzz")
    group_index = next(
        (index for index, (key, _) in enumerate(LEGEND_GROUPS) if key == group), len(LEGEND_GROUPS),
    )
    layer_index = sequence.index(layer_key) if layer_key in sequence else len(sequence)
    return (group_index, layer_index)


# --------------------------------------------------------------------------- QGIS 符号

def _qgis():
    """按需导入 QGIS（保证纯数据路径在没有 QGIS 的解释器里也能被单元测试覆盖）。"""

    from qgis.core import (  # noqa: WPS433 - 延迟导入是本模块的契约
        QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSimpleFillSymbolLayer,
        QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
        QgsSimpleMarkerSymbolLayerBase,
    )

    return {
        "QgsFillSymbol": QgsFillSymbol, "QgsLineSymbol": QgsLineSymbol,
        "QgsMarkerSymbol": QgsMarkerSymbol, "QgsSimpleFillSymbolLayer": QgsSimpleFillSymbolLayer,
        "QgsSimpleLineSymbolLayer": QgsSimpleLineSymbolLayer,
        "QgsSimpleMarkerSymbolLayer": QgsSimpleMarkerSymbolLayer,
        "QgsSimpleMarkerSymbolLayerBase": QgsSimpleMarkerSymbolLayerBase,
    }


_MARKER_SHAPES = {
    "circle": "Circle", "square": "Square", "triangle": "Triangle",
    "star": "Star", "diamond": "Diamond", "pentagon": "Pentagon",
    "cross": "Cross",
}


def marker_shape_enum(shape):
    """把样式里的标记形状名映射到 QGIS 枚举值（未知形状抛错，不猜）。"""

    api = _qgis()
    if shape not in _MARKER_SHAPES:
        raise KeyError(f"未登记的标记形状：{shape}")
    return getattr(api["QgsSimpleMarkerSymbolLayerBase"], _MARKER_SHAPES[shape])


def marker_symbol(style_key):
    """标记符号（既有铁塔 / 铁塔障碍 / 机场 / 转弯点 / 起点 / 终点）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "marker":
        raise ValueError(f"{style_key} 不是标记样式")
    layer = api["QgsSimpleMarkerSymbolLayer"](
        marker_shape_enum(item["shape"]), float(item["size"]),
        float(item.get("angle") or 0.0),
    )
    layer.setColor(_color(item["fill"]))
    layer.setStrokeColor(_color(item["outline"]))
    layer.setStrokeWidth(0.0 if item.get("outline") == "transparent" else float(item["outline_width"]))
    # P16 规划提案用虚线描边 + 空心填充，与"既建设施（实线 / 实心）"视觉可分。
    layer.setStrokeStyle(_stroke_style(item.get("outline_style")))
    symbol = api["QgsMarkerSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    if item.get("opacity") is not None:
        # 普通既有铁塔"退到背景层"靠的就是这里：只降透明度，不删数据、不动坐标。
        symbol.setOpacity(float(item["opacity"]))
    return symbol


def line_symbol(style_key):
    """线符号（规划航路）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "line":
        raise ValueError(f"{style_key} 不是线样式")
    layer = api["QgsSimpleLineSymbolLayer"](_color(item["color"]), float(item["width"]))
    layer.setPenStyle(_stroke_style(item.get("line_style")))
    layer.setPenCapStyle(_cap_style(item.get("capstyle")))
    layer.setPenJoinStyle(_join_style(item.get("joinstyle")))
    symbol = api["QgsLineSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def fill_symbol(style_key):
    """面符号（海域 / 陆地 / 空域 / 障碍物 / 机场范围）。"""

    api = _qgis()
    item = style(style_key)
    if item["kind"] != "fill":
        raise ValueError(f"{style_key} 不是面样式")
    layer = api["QgsSimpleFillSymbolLayer"]()
    layer.setColor(_color(item["fill"]))
    layer.setStrokeColor(_color(item["outline"]))
    layer.setStrokeWidth(0.0 if item.get("outline") == "transparent" else float(item["outline_width"]))
    layer.setStrokeStyle(_stroke_style(item.get("outline_style")))
    symbol = api["QgsFillSymbol"]()
    symbol.changeSymbolLayer(0, layer)
    if item.get("opacity") is not None:
        symbol.setOpacity(float(item["opacity"]))
    return symbol


def symbol_for(style_key):
    """按样式键返回对应的 QgsSymbol（地图图层与图例预览共用）。"""

    item = style(style_key)
    if item["kind"] == "fill":
        return fill_symbol(style_key)
    if item["kind"] == "line":
        return line_symbol(style_key)
    return marker_symbol(style_key)


def symbol_preview_image(style_key, width_mm=6.0, height_mm=6.0):
    """用**同一份样式定义**把图例符号渲染成 PNG 字节。

    为什么不用 QgsSymbol 离屏渲染：本环境下 QgsSymbol 的离屏 ``renderFeature`` 需要
    完整的 QgsRenderContext（painter + mapToPixel + extent + scale），实测在 headless
    ``QT_QPA_PLATFORM=offscreen`` 下无法稳定产出内容（结果全透明）。图例符号改为按
    :data:`FIGURE_STYLES` **同一份样式表**直接绘制：形状 / 颜色 / 线宽 / 尺寸都取自同一
    定义，因此"图例与地图符号一致"仍由单一定义保证。
    """

    import math

    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

    scale = 12.0  # 12 px/mm ≈ 305 DPI：图例符号清晰且体积可控。
    width = max(8, int(round(width_mm * scale)))
    height = max(8, int(round(height_mm * scale)))
    item = style(style_key)
    # 地图 marker 保持原尺寸；仅把图例里的紫色空心三角提升到与星标/转弯点相近的
    # 阅读等级（普通铁塔已缩小并降低不透明度，图例里若照搬会看不清）。
    if style_key in ("tower_existing", "tower_obstacle"):
        item["size"] = 4.4
        item["opacity"] = 1.0
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        if item.get("opacity") is not None:
            painter.setOpacity(float(item["opacity"]))
        if item["kind"] == "fill":
            _draw_fill(painter, item, width, height)
        elif item["kind"] == "line":
            _draw_line(painter, item, width, height)
        else:
            _draw_marker(painter, item, width, height, math)
    finally:
        painter.end()
    return _image_png_bytes(image)


def _draw_fill(painter, item, width, height):
    from qgis.PyQt.QtCore import QRectF, Qt
    from qgis.PyQt.QtGui import QColor, QPen

    reference = min(width, height)
    inset = reference * 0.13
    painter.setBrush(QColor(str(item["fill"])))
    if str(item.get("outline")) == "transparent" or float(item.get("outline_width") or 0) <= 0:
        painter.setPen(Qt.NoPen)
    else:
        pen = QPen(QColor(str(item["outline"])))
        pen.setWidthF(max(1.0, float(item["outline_width"]) * reference * 0.35))
        if str(item.get("outline_style") or "solid") == "dash":
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
    if str(item.get("shape") or "") == "circle":
        # 真实米制圆（CNS 覆盖圈）在图例里也用圆环表达。
        painter.drawEllipse(QRectF(inset, inset, width - 2 * inset, height - 2 * inset))
        return
    painter.drawRect(QRectF(inset, inset, width - 2 * inset, height - 2 * inset))


def _draw_line(painter, item, width, height):
    from qgis.PyQt.QtCore import QPointF
    from qgis.PyQt.QtGui import QColor, QPen

    pen = QPen(QColor(str(item["color"])))
    pen.setWidthF(max(1.2, float(item["width"]) * 1.6))
    pen.setStyle(_stroke_style(item.get("line_style")))
    pen.setCapStyle(_cap_style(item.get("capstyle")))
    painter.setPen(pen)
    painter.drawLine(QPointF(width * 0.08, height * 0.5), QPointF(width * 0.92, height * 0.5))


def _draw_marker(painter, item, width, height, math_module):
    from qgis.PyQt.QtCore import QPointF, QRectF
    from qgis.PyQt.QtGui import QColor, QPen, QPolygonF

    reference = min(width, height)
    centre = QPointF(width * 0.5, height * 0.5)
    radius = max(2.4, float(item["size"]) * reference * 0.06)
    pen = QPen(QColor(str(item["outline"])))
    pen.setWidthF(max(0.9, float(item.get("outline_width") or 0) * 1.6))
    pen.setStyle(_stroke_style(item.get("outline_style")))
    painter.setPen(pen)
    painter.setBrush(QColor(str(item["fill"])))
    shape = str(item["shape"])
    if shape == "circle":
        painter.drawEllipse(centre, radius, radius)
        return
    if shape == "cross":
        painter.drawLine(
            QPointF(centre.x() - radius, centre.y()),
            QPointF(centre.x() + radius, centre.y()),
        )
        painter.drawLine(
            QPointF(centre.x(), centre.y() - radius),
            QPointF(centre.x(), centre.y() + radius),
        )
        return
    if shape == "square":
        painter.save()
        try:
            painter.translate(centre)
            painter.rotate(float(item.get("angle") or 0.0))
            painter.drawRect(QRectF(-radius, -radius, 2 * radius, 2 * radius))
        finally:
            painter.restore()
        return
    if shape == "diamond":
        points = [
            QPointF(centre.x(), centre.y() - radius), QPointF(centre.x() + radius, centre.y()),
            QPointF(centre.x(), centre.y() + radius), QPointF(centre.x() - radius, centre.y()),
        ]
    elif shape == "triangle":
        half = radius * 1.15
        points = [
            QPointF(centre.x(), centre.y() - half),
            QPointF(centre.x() + half, centre.y() + half * 0.8),
            QPointF(centre.x() - half, centre.y() + half * 0.8),
        ]
    elif shape == "star":
        points = []
        for index in range(10):
            angle = -math_module.pi / 2 + index * math_module.pi / 5
            distance = radius * 1.25 if index % 2 == 0 else radius * 0.5
            points.append(QPointF(
                centre.x() + distance * math_module.cos(angle),
                centre.y() + distance * math_module.sin(angle),
            ))
    else:  # pragma: no cover - 未登记形状由 style() 抛错，这里只兜底
        painter.drawEllipse(centre, radius, radius)
        return
    painter.drawPolygon(QPolygonF(points))


def _image_png_bytes(image):
    """QImage → PNG 字节（经系统临时文件，QGIS 环境下的稳定路径）。"""

    import os
    import tempfile

    handle, path = tempfile.mkstemp(prefix="cns-symbol-", suffix=".png")
    os.close(handle)
    try:
        if not image.save(path, "PNG"):
            return b""
        with open(path, "rb") as stream:
            return stream.read()
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - 临时文件已被清理
            pass


def _color(value):
    from qgis.PyQt.QtGui import QColor

    if value is None or str(value) == "transparent":
        return QColor(0, 0, 0, 0)
    return QColor(str(value))


def _stroke_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"solid": Qt.SolidLine, "dash": Qt.DashLine, "dot": Qt.DotLine}
    return mapping.get(str(value or "solid"), Qt.SolidLine)


def _cap_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"round": Qt.RoundCap, "flat": Qt.FlatCap, "square": Qt.SquareCap}
    return mapping.get(str(value or "round"), Qt.RoundCap)


def _join_style(value):
    from qgis.PyQt.QtCore import Qt

    mapping = {"round": Qt.RoundJoin, "miter": Qt.MiterJoin, "bevel": Qt.BevelJoin}
    return mapping.get(str(value or "round"), Qt.RoundJoin)


__all__ = [
    "CNS_LABEL_CARD_COLORS", "CNS_LABEL_CARD_ORDER", "CNS_LABEL_SECONDARY_STYLE",
    "CNS_LEGEND_GROUP_COLUMNS",
    "FIGURE_STYLES", "FONT_CANDIDATES", "FONT_FILE_CANDIDATES",
    "LABEL_STYLES", "LAYOUT", "LEGEND_GROUP_COLUMNS", "LEGEND_GROUP_OF", "LEGEND_GROUPS",
    "fill_symbol", "label_style", "legend_order_key", "line_symbol", "marker_shape_enum",
    "marker_symbol", "style", "symbol_for", "symbol_preview_image",
]
