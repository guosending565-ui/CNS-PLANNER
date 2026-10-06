"""专题成果图模板目录（Presentation / Cartographic Export）。

本模块只描述**制图模板**，不含任何业务算法：

* 每个模板声明 id / version / 状态（``available`` / ``planned``）、中文名称、用途说明、
  可用图层键、图例分组顺序与显示阈值；
* ``route_overview_v1`` 与本轮新增的四个 CNS 专题模板为 ``available``；
  ``route_detail_v1`` 仍是 ``planned``（目录会明确返回"尚未实现"，绝不生成假结果）；
* 数据源角色（哪一项 FigureSpec 图层读哪个真实数据源）由模板声明，装配与读取在
  Application / GIS 层完成，模板本身不打开任何数据集。

阈值语义（重要）：
    ``terrain_threshold_m`` / ``building_threshold_m`` 是 **figure display threshold**：
    只决定"这一张图上哪些格被显示为障碍物"，它们
    不进入 Planning Constraint Field、不改变航路安全判断、也不是新的业务 gate。
    阈值与依据会原样写进 FigureSpec，成为可审计事实。

CNS 服务视觉语言（本轮冻结）：
    通信 = 绿 · RID = 橙 · Radar = 蓝 · 导航完整性 = 黄；
    既有设施用**实线 / 实心**符号，P16 ``selected_actions`` 一律是
    **规划提案（未确认）**，用虚线外圈 / 空心符号，绝不上色成已建成设施。
    覆盖圈只是 ``planning service radius``，不是实测无线传播等值线。
"""

from __future__ import annotations

from copy import deepcopy
import math

TEMPLATE_SCHEMA_VERSION = 1

ROUTE_OVERVIEW_V1 = "route_overview_v1"
ROUTE_DETAIL_V1 = "route_detail_v1"
COMMUNICATION_LAYOUT_V1 = "communication_layout_v1"
NAVIGATION_LAYOUT_V1 = "navigation_layout_v1"
SURVEILLANCE_LAYOUT_V1 = "surveillance_layout_v1"
CNS_COMBINED_V1 = "cns_combined_v1"

#: 模板状态：``available`` 表示本实现已能真实出图；``planned`` 表示只登记类型与说明。
AVAILABLE = "available"
PLANNED = "planned"

#: 五张 CNS 专题图（不含 route_overview / route_detail）的统一 extent 口径标识。
CNS_EXTENT_POLICY_ID = "cns_route_context_v1"
#: 当前 CNS 五图共用的版式档位：地图框尺寸固定 ⇒ extent / 比例尺 / 可比性都固定。
#:
#: 版本沿革（Round30-B1 起）：
#:
#: * ``cns_five_figure_v1`` = Round30-A 首版（标题 11 pt / 图例条目 6.8 pt / 2 列图例 /
#:   地图框 182 × 193 mm / 地图内部带大说明框）；
#: * ``cns_five_figure_v2`` = Round30-B1 Cartographic Polish：整体文字放大并建立视觉等级、
#:   删除地图内部说明框、标签改为正式背景卡、图例标题真正居中、图例 3 列重排、
#:   map canvas 改 EPSG:32651（覆盖圈在纸面上是真正的正圆）。地图框统一为 182 × 171 mm。
#:
#: 模板参数与 FigureSpec.metadata 都会带上这个版本号，因此 Round30-A 与 Round30-B 的
#: 图件在 metadata 中**可区分**，不会出现"两张图看起来不一样但元数据完全一致"。
CNS_LAYOUT_PROFILE = "cns_five_figure_v2"
#: 历史档位标识，只用于识别 Round30-A 已导出的旧图件（不参与新图生成）。
CNS_LAYOUT_PROFILE_V1 = "cns_five_figure_v1"
#: 监视布设图的 variant 参数名与两个合法取值。
SURVEILLANCE_SERVICE_PARAMETER = "surveillance_service"
SURVEILLANCE_SERVICE_RID = "rid_cooperative"
SURVEILLANCE_SERVICE_RADAR = "radar_noncooperative"
SURVEILLANCE_SERVICE_VALUES = (SURVEILLANCE_SERVICE_RID, SURVEILLANCE_SERVICE_RADAR)

#: 服务主色（图例与前端共用同一份定义；制图符号在 gis/figure_style.py 中按此取值）。
CNS_SERVICE_COLORS = {
    "C:communication": {"family": "communication", "color": "#1a9c4a", "label": "通信"},
    "S:rid_cooperative": {"family": "rid", "color": "#e8720c", "label": "RID"},
    "S:radar_noncooperative": {"family": "radar", "color": "#1f6fd0", "label": "Radar"},
    "N:navigation_integrity_monitoring": {
        "family": "navigation", "color": "#d19a00", "label": "导航完整性",
    },
}

#: CNS 服务覆盖口径（**只用于图面表达**，不参与任何业务重算）。
CNS_COVERAGE_POLICY = {
    "C:communication": {
        "omnidirectional": True,
        "radius_by_surface_m": {"land": 4000.0, "coastal_uncertain": 4000.0, "sea": 4000.0},
        "radius_basis": "geometric_planning_radius",
        "radius_semantics": "planning_service_radius_not_measured_radio_propagation",
        "redundancy_by_surface": {"land": 2, "coastal_uncertain": 2, "sea": 1},
    },
    "S:rid_cooperative": {
        "omnidirectional": True,
        "radius_by_surface_m": {"land": 2000.0, "coastal_uncertain": 2000.0, "sea": 5000.0},
        "radius_basis": "geometric_planning_radius",
        "radius_semantics": "planning_service_radius_not_measured_radio_propagation",
    },
    "S:radar_noncooperative": {
        "panel_azimuth_deg": 90.0,
        "panel_range_km": 3.0,
        "radar_ii_enabled": False,
        "radius_semantics": "radar_i_90deg_panel_3km_radar_ii_not_enabled_range_not_relaxed",
    },
    "N:navigation_integrity_monitoring": {
        "local_monitoring_radius_m": 10000.0,
        "radius_semantics": (
            "engineering_local_monitoring_context_not_rtk_baseline_"
            "not_certified_gbas_service_volume"
        ),
    },
}

#: 本轮预留的后续模板（不生成任何结果，只声明类型与用途）。
_PLANNED_TEMPLATES = (
    {
        "template_id": ROUTE_DETAIL_V1,
        "display_name": "航路细节放大图",
        "purpose": "以更小的缓冲区放大航路局部，展示转弯、净空与周边障碍细节。",
        "status": PLANNED,
        "planned_inputs": ["operational_route", "terrain", "buildings", "towers"],
        "next_round_note": "复用同一 FigureSpec 边界与渲染器，只更换 extent buffer 与图层集合。",
    },
)

#: ``route_overview_v1`` 的稳定图例顺序（只显示实际存在且可用的项）。
#: 顺序即语义分组顺序：地理环境 → 障碍物 → 既有设施与机场 → 规划航路。
#: **不含适飞空域**：图1按产品要求不表达空域（空域能力与其它模板不受影响）。
ROUTE_OVERVIEW_LEGEND_ORDER = (
    "sea",
    "land",
    "terrain_obstacle",
    "building_obstacle",
    "tower_existing",
    "tower_obstacle",
    "airport",
    "airport_protection",
    "planned_route",
    "turn_point",
    "start_point",
    "end_point",
)

#: CNS 专题图的共同底图与设施图层顺序（各模板只取其中真实存在且可用的项）。
#: 语义分组顺序：地理环境 → 障碍物 → 站址背景 → 既有设施 → 规划提案 →
#: 覆盖/能力 → 能力限制 → 规划航路。
CNS_LEGEND_ORDER = (
    "sea",
    "land",
    "terrain_obstacle",
    "building_obstacle",
    "tower_existing",
    "cns_existing",
    "cns_comm_proposal",
    "cns_rid_proposal",
    "cns_nav_proposal",
    "cns_coverage_comm",
    "cns_coverage_rid_land",
    "cns_coverage_rid_sea",
    "cns_radar_context",
    "cns_radar_sector",
    "cns_radar_proposal",
    "cns_radar_limitation",
    "planned_route",
    "turn_point",
    "start_point",
    "end_point",
)

#: :data:`ROUTE_OVERVIEW_LEGEND_ORDER` 与 :data:`CNS_LEGEND_ORDER` 的中文名。
LAYER_DISPLAY_NAMES = {
    "sea": "海域",
    "land": "陆地区域",
    "airspace": "已确认适飞空域",
    "terrain_obstacle": "地形障碍（≥ 显示阈值）",
    "building_obstacle": "建筑障碍（≥ 显示阈值）",
    # 源 Excel 的"铁塔细分类型"含楼面抱杆、楼面拉线塔、美化外罩、落地塔、H 杆塔、
    # 单管塔等，373 个点实际是**通信站址集合**，不全部是独立铁塔。
    "tower_existing": "既有通信站址",
    "tower_obstacle": "铁塔障碍",
    "airport": "机场",
    "airport_protection": "机场净空/保护范围",
    "planned_route": "规划航路",
    "turn_point": "航路转弯点",
    "start_point": "起点",
    "end_point": "终点",
    # ---- CNS 专题图 ----
    "cns_existing": "既有 CNS 设施（已建）",
    "cns_comm_proposal": "通信规划提案（未确认）",
    "cns_rid_proposal": "RID 规划提案（未确认）",
    "cns_nav_proposal": "导航完整性监测点提案（未确认）",
    "cns_coverage_comm": "通信规划服务半径 4 km",
    "cns_coverage_rid_land": "RID 陆地/沿海规划范围 2 km",
    "cns_coverage_rid_sea": "RID 海上延伸规划范围 2–5 km",
    "cns_radar_context": "Radar 评估候选站址（未选中）",
    #: Round31-C：正式分级规划下 Stage B 会给出 I 型 + II 型混合方案，
    #: 图例必须让两类型号都可辨识（Ⅰ型蓝 / Ⅱ型紫），不得只暗示 "Radar-I"。
    "cns_radar_sector": "Radar 90° 规划扇区（未确认；Ⅰ型蓝 / Ⅱ型紫）",
    "cns_radar_proposal": "Radar 规划站址（未确认）",
    "cns_radar_limitation": "非合作监视能力限制（无可行布设）",
}

#: 图层键 → 默认读取的真实数据源角色（Application 层据此装配；未配置即为不可用）。
#: ``sea`` / ``land`` 指向**制图专用**的 ``cartographic_land``（只影响地图表达），
#: 业务 ``land_mask``（surface classification 的判据）取值与语义都不受此表影响。
#: ``cns_*`` 角色指向 ProjectState 里的 canonical 容器，**不是**文件路径。
LAYER_SOURCE_ROLES = {
    "sea": "cartographic_land",
    "land": "cartographic_land",
    "airspace": "basemap",
    "terrain_obstacle": "terrain_dtm",
    "building_obstacle": "building_grid",
    "tower_existing": "towers",
    "tower_obstacle": "tower_obstacle_profiles",
    "airport": "airports",
    "airport_protection": "airports",
    "cns_existing": "existing_cns_facilities",
    "cns_comm_proposal": "cns_corridor_site_plan",
    "cns_rid_proposal": "cns_corridor_site_plan",
    "cns_nav_proposal": "cns_corridor_site_plan",
    "cns_coverage_comm": "cns_corridor_site_plan",
    "cns_coverage_rid_land": "cns_corridor_site_plan",
    "cns_coverage_rid_sea": "cns_corridor_site_plan",
    "cns_radar_context": "radar_surveillance_layout",
    "cns_radar_sector": "radar_surveillance_layout",
    "cns_radar_proposal": "radar_surveillance_layout",
    "cns_radar_limitation": "radar_surveillance_layout",
}

#: 本轮模板参数默认值。全部可在调用时覆盖，且会被记录进 FigureSpec。
ROUTE_OVERVIEW_PARAMETERS = {
    # 图1 的定义是"**航路周边**状况图"，不是舟山市区域总览：范围 = 选定航路 bbox +
    # 制图缓冲（米制计算），默认 10 km。站址只在范围确定**之后**按范围筛选，
    # 绝不为了显示更多站址而扩大范围。
    "extent_buffer_km": 10.0,
    "extent_max_padding_km": 30.0,
    "extent_source_crs": "EPSG:32651",
    # 通信站址是图 1 的展示层：地图范围仍按 A4 版式正常扩展，但站址只显示航路
    # 10 km 邻域内的记录。该参数不参与任何 Communication/CNS 规划或 land-mask。
    "site_display_buffer_km": 10.0,
    # figure display threshold（只是显示口径，不是业务约束）。
    "terrain_threshold_m": 100.0,
    "building_threshold_m": 100.0,
    "terrain_threshold_basis": "figure_display_threshold_from_spec",
    "building_threshold_basis": "figure_display_threshold_from_spec",
    # 版面（毫米）：A4 竖版 210×297。标题带 / 地图 / 间距 / 图例 / 页脚由 _layout_plan
    # 按实际图例条目数分配：``map_fraction`` 是地图的**最大**占比（0.72）。
    "document_width_mm": 210.0,
    "document_height_mm": 297.0,
    "margin_mm": 14.0,
    "map_fraction": 0.72,
    # 图例：横向 **2 列**（按语义分组横向展开）；条目更多时由图例算法自动均衡列高。
    "legend_columns": 2,
    # 显示控制。
    "segments_per_degree": 5,
    "show_landmark_labels": True,
    "show_place_labels": True,
    "preview_dpi": 130,
    "export_dpi": 300,
    "preview_px_per_mm": 3.0,
    # 布尔开关（显式列出，避免客户端传入非布尔值悄悄生效）。
    "label_turn_points": True,
}

#: 五张 CNS 专题图共用的模板参数。与 route_overview 的差别只有：
#:
#: * ``legend_columns`` 取 :data:`~cns_planner.gis.figure_style.LAYOUT` 的
#:   ``cns_legend_columns``（3 列）：CNS 分组多（最多 8 组），2 列会把右列压到近百毫米，
#:   放大字号后地图被挤到 140 mm 以下；3 列仍保持"整组不拆、保序"；
#: * ``layout_profile`` 固定为 :data:`CNS_LAYOUT_PROFILE`（``cns_five_figure_v2``）：
#:   地图框尺寸在五图之间**完全一致**，因此 extent、比例尺、经纬网都逐图可比 ——
#:   这是产品明确要求的"五图统一"；
#: * ``extent_policy_id`` 记录统一 extent 口径，便于审计与测试断言；
#: * ``extent_aspect_locked`` 让 extent 的计算长宽比取固定档位值，不随图例条目数浮动。
CNS_SHARED_PARAMETERS = {
    **ROUTE_OVERVIEW_PARAMETERS,
    "extent_policy_id": CNS_EXTENT_POLICY_ID,
    "layout_profile": CNS_LAYOUT_PROFILE,
    "legend_columns": 4,
    "extent_aspect_locked": True,
    # 工程审计条（地图 CRS / 经纬网 CRS / revision / 未显示图层）**默认关闭**：
    # 正式图只保留业务披露，审计信息仍完整保存在 FigureSpec.metadata 与导出报告里。
    "audit_footer": False,
    # 覆盖圈全部位于航路 10 km 邻域内（通信 4 km / RID 5 km，站址距航路 ≤ 2 km），
    # 因此 10 km 缓冲足够容纳覆盖圈；该参数只是把这条判断写进 FigureSpec 供审计。
    "coverage_display_margin_km": 7.0,
    "show_synthetic_existing_facilities": False,
    "show_navigation_local_monitoring_radius": False,
}

#: 模板参数的**类型与范围契约**：``key -> (类型, 下界/枚举, 上界)``。
#:
#: 为什么需要它：``parameters()`` 之前只判断"键是否存在"，于是
#: ``extent_buffer_km="abc"`` / ``legend_columns=999`` / ``document_width_mm=-1``
#: 这类值会一路进到版面与几何计算里。这里给出每个参数的合法类型与闭区间范围，
#: 非法值一律**拒绝**（抛 :class:`MapFigureParameterInvalid`），绝不静默截断成别的值。
PARAMETER_CONSTRAINTS = {
    "extent_buffer_km": ("number", 0.1, 100.0),
    "extent_max_padding_km": ("number", 0.0, 500.0),
    "extent_source_crs": ("crs", None, None),
    "extent_policy_id": ("text", 1, 64),
    "layout_profile": ("text", 1, 64),
    "extent_aspect_locked": ("boolean", None, None),
    "site_display_buffer_km": ("number", 0.1, 100.0),
    "coverage_display_margin_km": ("number", 0.0, 50.0),
    "show_synthetic_existing_facilities": ("boolean", None, None),
    "show_navigation_local_monitoring_radius": ("boolean", None, None),
    # 工程审计条开关：正式图默认 false（review 模式可显式打开）。
    "audit_footer": ("boolean", None, None),
    "terrain_threshold_m": ("number", 0.0, 10000.0),
    "building_threshold_m": ("number", 0.0, 10000.0),
    "terrain_threshold_basis": ("text", 1, 200),
    "building_threshold_basis": ("text", 1, 200),
    "document_width_mm": ("number", 80.0, 1000.0),
    "document_height_mm": ("number", 80.0, 1000.0),
    "margin_mm": ("number", 0.0, 80.0),
    "map_fraction": ("number", 0.30, 0.95),
    "legend_columns": ("integer", 1, 4),
    "segments_per_degree": ("integer", 1, 60),
    "show_landmark_labels": ("boolean", None, None),
    "show_place_labels": ("boolean", None, None),
    "label_turn_points": ("boolean", None, None),
    "preview_dpi": ("number", 36.0, 600.0),
    "export_dpi": ("number", 36.0, 600.0),
    "preview_px_per_mm": ("number", 0.5, 12.0),
    # 监视布设图的 variant 参数：**只允许**两个登记值，非法值明确拒绝。
    SURVEILLANCE_SERVICE_PARAMETER: ("enum", None, SURVEILLANCE_SERVICE_VALUES),
}

#: 模板允许的最大画布总像素（宽 × 高）；超过即拒绝，避免 QGIS 版面渲染失控。
MAX_PARAMETER_PIXELS = 40_000_000


class MapFigureParameterInvalid(ValueError):
    """模板参数不满足类型 / 范围契约（中文原因 + 参数名）。"""

    code = "map_figure_parameter_invalid"

    def __init__(self, parameter, reason):
        self.parameter = str(parameter)
        self.reason = str(reason)
        super().__init__(f"模板参数「{self.parameter}」不可用：{self.reason}")


def _validate_parameter(key, value):
    """按 :data:`PARAMETER_CONSTRAINTS` 校验单个参数；返回规范化后的值。"""

    kind, low, high = PARAMETER_CONSTRAINTS[key]
    if kind == "boolean":
        if not isinstance(value, bool):
            raise MapFigureParameterInvalid(key, "必须是布尔值 true / false")
        return value
    if kind == "enum":
        allowed = tuple(high or ())
        if not allowed:
            raise MapFigureParameterInvalid(key, "该参数没有登记任何合法取值")
        text = str(value or "").strip()
        if text not in allowed:
            raise MapFigureParameterInvalid(
                key, "只允许 " + " / ".join(allowed) + f"（收到 {text or '空值'}）",
            )
        return text
    if kind == "integer":
        # 注意：bool 是 int 的子类，绝不能把 True 当成 1 接受。
        if isinstance(value, bool) or not isinstance(value, int):
            raise MapFigureParameterInvalid(key, "必须是整数")
        if low is not None and not (low <= value <= high):
            raise MapFigureParameterInvalid(key, f"必须在 {low} ~ {high} 之间（收到 {value}）")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MapFigureParameterInvalid(key, "必须是数字")
        number = float(value)
        if not math.isfinite(number):
            raise MapFigureParameterInvalid(key, "必须是有限数字")
        if low is not None and not (low <= number <= high):
            raise MapFigureParameterInvalid(key, f"必须在 {low} ~ {high} 之间（收到 {number}）")
        return number
    if kind == "crs":
        text = str(value or "").strip()
        if not text or len(text) > 64:
            raise MapFigureParameterInvalid(key, "必须是非空且不超过 64 字符的 CRS 标识")
        return text
    if kind == "text":
        text = str(value or "").strip()
        if low is not None and not (low <= len(text) <= high):
            raise MapFigureParameterInvalid(key, f"长度必须在 {low} ~ {high} 字符之间")
        return text
    raise MapFigureParameterInvalid(key, f"未登记的参数类型 {kind}")


def _validate_parameter_pixels(parameters):
    """**真正的总像素上限**：宽 × 高（毫米→英寸→像素）必须 <= MAX_PARAMETER_PIXELS。"""

    for dpi_key in ("export_dpi", "preview_dpi"):
        dpi = float(parameters.get(dpi_key) or 0.0)
        if dpi <= 0:
            continue
        width_px = float(parameters["document_width_mm"]) / 25.4 * dpi
        height_px = float(parameters["document_height_mm"]) / 25.4 * dpi
        total = width_px * height_px
        if total > MAX_PARAMETER_PIXELS:
            raise MapFigureParameterInvalid(
                dpi_key,
                f"该 DPI 下页面为 {int(width_px)}×{int(height_px)} = {int(total)} 像素，"
                f"超过总像素上限 {MAX_PARAMETER_PIXELS}",
            )
    return parameters


def _template(template_id, *, display_name, purpose, availability, parameters=None,
              legend_order=(), layers=(), notes="", variant_parameter=None,
              required_parameters=()):
    return {
        "template_id": template_id,
        "template_version": TEMPLATE_SCHEMA_VERSION,
        "display_name": display_name,
        "purpose": purpose,
        "status": availability,
        "parameters": deepcopy(parameters or {}),
        "legend_order": list(legend_order),
        "layer_keys": list(layers),
        "notes": notes,
        "variant_parameter": variant_parameter,
        "required_parameters": list(required_parameters),
    }


ROUTE_OVERVIEW_V1_TEMPLATE = _template(
    ROUTE_OVERVIEW_V1,
    display_name="航路周边状况图",
    purpose=(
        "以上方地图、下方独立图例的固定 A4 竖版，展示选定权威运行航路及其周边海陆、"
        "障碍物与既有通信站址的环境关系。"
    ),
    availability=AVAILABLE,
    parameters=ROUTE_OVERVIEW_PARAMETERS,
    legend_order=ROUTE_OVERVIEW_LEGEND_ORDER,
    layers=tuple(ROUTE_OVERVIEW_LEGEND_ORDER),
    notes=(
        "只读消费 canonical operational route 与已配置 GIS 数据源；不含适飞空域表达；"
        "缺数据的图层会被省略，并在 FigureSpec.source_status / omitted_layers 中记录原因，"
        "绝不补 0 或伪造。"
    ),
)

_COMMON_CNS_NOTES = (
    "只读消费 canonical operational route、既有 CNS 基线、P16 selected_actions、"
    "P17 连续服务结论与 Radar 划设结果；P16 动作一律按「规划提案（未确认）」表达，"
    "覆盖圆只是 planning service radius。缺数据的图层会被省略并在 FigureSpec 记录原因，"
    "绝不补 0、绝不新造站址、绝不生成 Radar 扇区。"
)

COMMUNICATION_LAYOUT_V1_TEMPLATE = _template(
    COMMUNICATION_LAYOUT_V1,
    display_name="通信设施布设图",
    purpose=(
        "展示 R0005 航路周边的既有 CNS 基线、P16 选中的通信（C:communication）规划提案"
        "及其规划服务半径，以及作为宿主背景的真实通信站址。"
    ),
    availability=AVAILABLE,
    parameters=CNS_SHARED_PARAMETERS,
    legend_order=CNS_LEGEND_ORDER,
    layers=tuple(CNS_LEGEND_ORDER),
    notes=_COMMON_CNS_NOTES + " 只消费 service_key=C:communication 的动作，绝不混入 RID/Radar。",
)

NAVIGATION_LAYOUT_V1_TEMPLATE = _template(
    NAVIGATION_LAYOUT_V1,
    display_name="导航完整性监测点布设图",
    purpose=(
        "展示 R0005 起降点（N005 / N006）上的导航完整性监测点（GNSS Navigation "
        "Integrity Monitor）规划提案与交付赤字披露。"
    ),
    availability=AVAILABLE,
    parameters=CNS_SHARED_PARAMETERS,
    legend_order=CNS_LEGEND_ORDER,
    layers=tuple(CNS_LEGEND_ORDER),
    notes=(
        _COMMON_CNS_NOTES + " 只消费 service_key=N:navigation_integrity_monitoring；"
        "监测点画在真实 endpoint site 上，绝不移动到铁塔、绝不称 RTK / GBAS，"
        "local_monitoring_radius 不是认证覆盖范围（默认不绘制）。"
    ),
)

SURVEILLANCE_LAYOUT_V1_TEMPLATE = _template(
    SURVEILLANCE_LAYOUT_V1,
    display_name="监视设施布设图",
    purpose=(
        "按 variant 分别展示两种监视能力：RID 合作监视（S:rid_cooperative）与 "
        "Radar 非合作监视（S:radar_noncooperative）。二者几何与结论完全不同，绝不混画。"
    ),
    availability=AVAILABLE,
    parameters={**CNS_SHARED_PARAMETERS, SURVEILLANCE_SERVICE_PARAMETER: None},
    legend_order=CNS_LEGEND_ORDER,
    layers=tuple(CNS_LEGEND_ORDER),
    notes=(
        _COMMON_CNS_NOTES + " 必须显式给出 surveillance_service（rid_cooperative / "
        "radar_noncooperative）；非法或缺失一律明确拒绝，绝不静默回退到任一 variant。"
    ),
    variant_parameter=SURVEILLANCE_SERVICE_PARAMETER,
    required_parameters=(SURVEILLANCE_SERVICE_PARAMETER,),
)

CNS_COMBINED_V1_TEMPLATE = _template(
    CNS_COMBINED_V1,
    display_name="CNS 综合布设图",
    purpose=(
        "在同一版面上叠加既有 CNS 基线、P16 选中的通信 / RID / 导航完整性提案与统一 extent；"
        "Radar 以能力限制说明表达（R0005 无可选站址）。"
    ),
    availability=AVAILABLE,
    parameters=CNS_SHARED_PARAMETERS,
    legend_order=CNS_LEGEND_ORDER,
    layers=tuple(CNS_LEGEND_ORDER),
    notes=(
        _COMMON_CNS_NOTES + " 同址多业务用多符号叠加表达，坐标逐字节一致，绝不靠偏移错位；"
        "R0005 的 Radar 新建设施数量恒为 0。"
    ),
)

#: ``route_detail_v1`` 等后续模板仍可以使用空域图层（能力未删除）。
PLANNED_LAYER_KEYS = ("airspace",)

_AVAILABLE_TEMPLATES = (
    ROUTE_OVERVIEW_V1_TEMPLATE,
    COMMUNICATION_LAYOUT_V1_TEMPLATE,
    NAVIGATION_LAYOUT_V1_TEMPLATE,
    SURVEILLANCE_LAYOUT_V1_TEMPLATE,
    CNS_COMBINED_V1_TEMPLATE,
)

#: 模板目录：id → 模板声明。顺序即接口返回顺序。
COMMERCIAL_TEMPLATES = {item["template_id"]: item for item in _AVAILABLE_TEMPLATES}
for _entry in _PLANNED_TEMPLATES:
    COMMERCIAL_TEMPLATES[_entry["template_id"]] = {
        "template_id": _entry["template_id"],
        "template_version": TEMPLATE_SCHEMA_VERSION,
        "display_name": _entry["display_name"],
        "purpose": _entry["purpose"],
        "status": _entry["status"],
        "parameters": {},
        "legend_order": [],
        "layer_keys": [],
        "planned_inputs": list(_entry["planned_inputs"]),
        "notes": _entry["next_round_note"],
        "variant_parameter": None,
        "required_parameters": [],
    }


def catalog():
    """模板目录（JSON-safe）：本轮哪些可生成、哪些只是预留，一目了然。"""

    return {
        "schema_version": TEMPLATE_SCHEMA_VERSION,
        "available_template_ids": [
            template_id for template_id, item in COMMERCIAL_TEMPLATES.items()
            if item["status"] == AVAILABLE
        ],
        "templates": [deepcopy(item) for item in COMMERCIAL_TEMPLATES.values()],
        "cns_service_colors": deepcopy(CNS_SERVICE_COLORS),
        "cns_coverage_policy": deepcopy(CNS_COVERAGE_POLICY),
        "cns_extent_policy_id": CNS_EXTENT_POLICY_ID,
        "cns_layout_profile": CNS_LAYOUT_PROFILE,
        "surveillance_service_values": list(SURVEILLANCE_SERVICE_VALUES),
        "notes": (
            "本目录只登记模板。status=planned 的模板尚未实现，调用生成接口会被明确拒绝，"
            "系统不会为它们生成任何占位结果。CNS 专题图中的 P16 设施一律是"
            "「规划提案（未确认）」，既有设施与规划提案必须视觉可分。"
        ),
    }


def template(template_id):
    """按 id 取模板声明；不存在时返回 ``None``。"""

    return COMMERCIAL_TEMPLATES.get(str(template_id or "").strip())


def is_available(template_id):
    item = template(template_id)
    return bool(item and item["status"] == AVAILABLE)


def parameters(template_id, overrides=None):
    """模板默认参数 + 调用方覆盖。

    覆盖值必须过 :data:`PARAMETER_CONSTRAINTS` 的类型 / 范围校验，并再过一次
    **总像素**上限校验；非法值一律抛 :class:`MapFigureParameterInvalid`。
    未知键仍被忽略（不是"静默生效"，而是根本不进入参数集）。
    """

    item = template(template_id)
    base = deepcopy(item["parameters"]) if item else {}
    for key, value in (overrides or {}).items():
        if key not in base or value is None:
            continue
        if key in PARAMETER_CONSTRAINTS:
            base[key] = _validate_parameter(key, value)
        else:
            base[key] = value
    return _validate_parameter_pixels(base)


def required_parameters(template_id):
    """模板**必须显式提供**的参数名（缺失即拒绝，绝不用默认值兜底）。"""

    item = template(template_id)
    return tuple(item.get("required_parameters") or ()) if item else ()


__all__ = [
    "AVAILABLE", "CNS_COMBINED_V1", "CNS_COMBINED_V1_TEMPLATE", "CNS_COVERAGE_POLICY",
    "CNS_EXTENT_POLICY_ID", "CNS_LAYOUT_PROFILE", "CNS_LAYOUT_PROFILE_V1", "CNS_LEGEND_ORDER",
    "CNS_SERVICE_COLORS",
    "CNS_SHARED_PARAMETERS", "COMMERCIAL_TEMPLATES", "COMMUNICATION_LAYOUT_V1",
    "COMMUNICATION_LAYOUT_V1_TEMPLATE", "LAYER_DISPLAY_NAMES", "LAYER_SOURCE_ROLES",
    "MAX_PARAMETER_PIXELS", "MapFigureParameterInvalid", "NAVIGATION_LAYOUT_V1",
    "NAVIGATION_LAYOUT_V1_TEMPLATE", "PARAMETER_CONSTRAINTS", "PLANNED",
    "PLANNED_LAYER_KEYS", "ROUTE_DETAIL_V1", "ROUTE_OVERVIEW_LEGEND_ORDER",
    "ROUTE_OVERVIEW_PARAMETERS", "ROUTE_OVERVIEW_V1", "ROUTE_OVERVIEW_V1_TEMPLATE",
    "SURVEILLANCE_LAYOUT_V1", "SURVEILLANCE_LAYOUT_V1_TEMPLATE",
    "SURVEILLANCE_SERVICE_PARAMETER", "SURVEILLANCE_SERVICE_RADAR",
    "SURVEILLANCE_SERVICE_RID", "SURVEILLANCE_SERVICE_VALUES", "TEMPLATE_SCHEMA_VERSION",
    "catalog", "is_available", "parameters", "required_parameters", "template",
]
