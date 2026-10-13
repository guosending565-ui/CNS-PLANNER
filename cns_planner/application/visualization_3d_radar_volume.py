"""Radar **方向性**三维覆盖楔体的只读构造（Round37-T3D-2B）。

语义边界（逐条必须遵守，违反即返工）
------------------------------------

1. **不是实测探测能力**：输出 ``engineering_geometric_directional_coverage_not_measured``
   —— 这是「方向性几何覆盖楔体」，不是雷达方程、不是 Pd-Pfa 曲线、不是实测探测距离。
   ``terrain_LOS`` / ``diffraction`` / ``building_blocking`` / ``clutter`` /
   ``multipath`` / ``interference`` / ``radar_equation`` / ``Pd_distance_curve``
   一律 ``not_evaluated``（直接取冻结契约 :data:`...NOT_EVALUATED`）。
2. **真三维楔体，不是二维圆**：斜距 ``R`` 是判据；对每个仰角 ``elev``
   ``水平距离 = R × cos(elev)``、``相对高度 = R × sin(elev)``。因此
   **``max_slant_range_m`` 绝不是所有高度截面的水平半径**，只有 ``elev = 0``
   那一个截面才与二维圆重合（``horizontal_radius_at_elevation_zero_m``）。
3. **方向性**：水平面阵范围 = 真实 ``azimuth_deg`` ± ``azimuth_half_width_deg``
   （冻结契约 45° ⇒ 90° 水平扇区）；仰角 ``0°–45°``（中心 22.5°）。本模块**绝不**
   生成全向 360° 覆盖，也绝不生成半球（那是通信/RID 的几何）。
4. **参数权威**：型号几何参数一律来自
   :func:`cns_planner.domain.radar_surveillance_layout.radar_geometry_parameters`
   （本模块**不复制**任何设备常量）。Radar-I 斜距 ``120–3000 m``；
   Radar-II ``200–5000 m``，且**只在显式声明**时使用。
5. **原点必须是 EGM2008 正高**：由 :func:`...visualization_3d_sites.radar_panel_origin`
   从 ``tower_obstacle_profiles`` 解析并携带确认语义。``origin_status != confirmed``
   时**不生成确定楔体**（``blocked`` + ``required_evidence``），绝不用 0 m 顶替。
6. **三种几何不可混用**（响应里逐项显式声明）：

   * 本模块输出的**物理最大覆盖楔体**；
   * 既有 ``display_geometry`` 的**航路局部显示裁剪**（本模块不做裁剪）；
   * 已由 LOS 确认的**可见区域**（当前**没有**权威持久化结果 ⇒ ``unknown``）。
7. **只读**：本模块零写入；不 import 任何写入权威，不注册失效入口。

米制几何全部在显式 CRS（默认 ``EPSG:32651``，与项目既有塔/地形查询同一帧）中完成，
再转回 WGS84；方位角按**投影平面**正北 0°、顺时针为正（与
``cns_planner.algorithms.radar_layout.geometry.bearing_deg`` 同一约定）。
"""

from __future__ import annotations

import math

from ..domain.cns_service_contract import (
    RADAR_SERVICE_SUBTYPE,
    RADAR_TARGET_COOPERATION,
    RADAR_TECHNOLOGY,
    RADAR_TYPE_SEMANTICS,
)
from ..domain.radar_surveillance_layout import (
    AZIMUTH_HALF_WIDTH_DEG,
    ELEVATION_CENTER_DEG,
    ELEVATION_MAX_DEG,
    ELEVATION_MIN_DEG,
    NOT_EVALUATED,
    RADAR_TYPE_II,
    RADAR_TYPE_LABELS,
    RADAR_TYPES,
    radar_geometry_parameters,
)
from .visualization_3d_communication_volume import metric_transformer

#: 覆盖语义：方向性工程几何，非实测探测能力。
COVERAGE_SEMANTICS = "engineering_geometric_directional_coverage_not_measured"

#: 几何模型标识（与通信 ``omnidirectional_hemisphere_slant_range`` 明确区分）。
GEOMETRY_MODEL = "directional_panel_wedge_slant_range_azimuth_elevation"

#: Radar 侧**禁止**的几何（对称于 RID 的 ``forbidden_geometry``）。
FORBIDDEN_GEOMETRY = (
    "omnidirectional_full_circle",
    "hemisphere_360",
    "rid_surface_partitioned_footprint",
)

#: LOS 未持久化时的状态与原因（**不得**被读成"已确认无遮挡"）。
LOS_STATUS_UNKNOWN = "unknown"
LOS_UNKNOWN_REASON = "terrain_los_not_persisted"

#: 有界分块与顶点分辨率（只影响显示平滑度，不改变任何半径/角度语义）。
MAX_PANELS = 64
MAX_AZIMUTH_SEGMENTS = 48
DEFAULT_AZIMUTH_SEGMENTS = 12
MAX_ELEVATION_SEGMENTS = 12
DEFAULT_ELEVATION_SEGMENTS = 4

#: 与 :data:`cns_planner.application.visualization_3d_sites.ORIGIN_UNCONFIRMED` 同值。
#: 这里刻意只比较字符串，避免几何模块反向依赖站址解析模块。
ORIGIN_UNCONFIRMED = "unconfirmed"

#: 截面验证用的仰角采样（°）：0 是唯一与二维圆重合的截面，22.5 是波束中心。
CROSS_SECTION_ELEVATIONS_DEG = (0.0, 11.25, 22.5, 33.75, 45.0)


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_azimuth_deg(value):
    """归一化到 ``[0, 360)``（360 与 0 是同一方向）。"""

    result = float(value) % 360.0
    return 0.0 if result == 360.0 else result


def azimuth_window_deg(azimuth_deg, half_width_deg):
    """面阵的水平覆盖窗口（可能跨越 0°/360°）。

    返回 ``(start, end, wrapped)``：``start`` 与 ``end`` 都归一化到 ``[0, 360)``；
    ``wrapped is True`` 表示窗口跨越正北（例如中心 350°、半宽 45° ⇒ 305° → 35°）。
    跨度恒为 ``2 × half_width``。
    """

    center = normalize_azimuth_deg(azimuth_deg)
    half = abs(float(half_width_deg))
    start = normalize_azimuth_deg(center - half)
    end = normalize_azimuth_deg(center + half)
    return start, end, bool(center - half < 0.0 or center + half >= 360.0)


def wedge_directions(azimuth_deg, *, azimuth_segments, elevation_segments):
    """楔体参数化：返回 ``[(elevation_deg, [azimuth_deg, ...]), ...]``。

    每一层仰角的方位角序列都从 ``-half`` 扫到 ``+half``（**含两端点**），因此
    端面正好落在真实波束边缘上，绝不"少画一条边"。
    """

    azimuths = int(max(2, min(int(azimuth_segments), MAX_AZIMUTH_SEGMENTS)))
    elevations = int(max(1, min(int(elevation_segments), MAX_ELEVATION_SEGMENTS)))
    layers = []
    for index in range(elevations + 1):
        elevation = ELEVATION_MIN_DEG + (
            (ELEVATION_MAX_DEG - ELEVATION_MIN_DEG) * index / float(elevations)
        )
        line = [
            normalize_azimuth_deg(
                float(azimuth_deg) - AZIMUTH_HALF_WIDTH_DEG
                + (2.0 * AZIMUTH_HALF_WIDTH_DEG) * step / float(azimuths)
            )
            for step in range(azimuths + 1)
        ]
        layers.append((elevation, line))
    return layers


def _point(x_m, y_m, z_m, to_wgs84):
    geographic = to_wgs84(x_m, y_m)
    if geographic is None:
        return None
    return [round(geographic[0], 9), round(geographic[1], 9), round(z_m, 3)]


def _slant_surface_rings(center_x, center_y, origin_z, slant_range_m, *, azimuth_deg,
                         layers, to_wgs84):
    """给定斜距的楔体曲面：每层仰角一圈点（``[lon, lat, H_egm2008]``）。

    ``水平距离 = slant_range × cos(elev)``、``相对高度 = slant_range × sin(elev)``。
    """

    rings = []
    for elevation, azimuths in layers:
        radians = math.radians(elevation)
        horizontal = slant_range_m * math.cos(radians)
        delta_z = slant_range_m * math.sin(radians)
        ring = []
        for azimuth in azimuths:
            bearing = math.radians(azimuth)
            # 投影平面：x 向东、y 向北；方位角自正北顺时针 ⇒ dx = sin, dy = cos。
            point = _point(
                center_x + horizontal * math.sin(bearing),
                center_y + horizontal * math.cos(bearing),
                origin_z + delta_z, to_wgs84,
            )
            if point is None:
                return []
            ring.append(point)
        rings.append(ring)
    return rings


def _side_fan(center_x, center_y, origin_z, *, azimuth_deg, inner_slant_m, outer_slant_m,
              layers, to_wgs84, edge="start"):
    """端面（方位角处于波束边缘）的竖直扇形：沿仰角从内斜距扫到外斜距。

    ``edge="start"`` 取 ``azimuth - half``，``edge="end"`` 取 ``azimuth + half``。
    """

    offset = -AZIMUTH_HALF_WIDTH_DEG if edge == "start" else AZIMUTH_HALF_WIDTH_DEG
    azimuth = normalize_azimuth_deg(float(azimuth_deg) + offset)
    bearing = math.radians(azimuth)
    ring = []
    for slant in (inner_slant_m, outer_slant_m):
        for elevation, _ in layers:
            radians = math.radians(elevation)
            horizontal = slant * math.cos(radians)
            point = _point(
                center_x + horizontal * math.sin(bearing),
                center_y + horizontal * math.cos(bearing),
                origin_z + slant * math.sin(radians), to_wgs84,
            )
            if point is None:
                return []
            ring.append(point)
    return ring


def cross_section_table(parameters):
    """截面数值表：仰角 → 水平距离与相对高度（**供逐项核对，不得省略**）。

    对每个采样仰角 ``elev``：``horizontal = R × cos(elev)``、``dz = R × sin(elev)``。
    ``elev = 0`` 那一行是唯一与二维圆重合的截面；把它与
    ``max_slant_range_m`` 并列展示，专门防止"把斜距当水平半径"的误读。
    """

    inner = float(parameters["min_slant_range_m"])
    outer = float(parameters["max_slant_range_m"])
    rows = []
    for elevation in CROSS_SECTION_ELEVATIONS_DEG:
        radians = math.radians(elevation)
        rows.append({
            "elevation_deg": elevation,
            "min_slant_range_m": inner,
            "max_slant_range_m": outer,
            "min_horizontal_distance_m": round(inner * math.cos(radians), 6),
            "max_horizontal_distance_m": round(outer * math.cos(radians), 6),
            "min_relative_height_m": round(inner * math.sin(radians), 6),
            "max_relative_height_m": round(outer * math.sin(radians), 6),
        })
    return rows


def build_radar_volume(panel, *, metric_crs="EPSG:32651", azimuth_segments=None,
                       elevation_segments=None, geometry_scope="project_layout",
                       allow_unconfirmed_origin=False, preview=False):
    """构造**一个** Radar 面阵的方向性三维楔体。

    返回结构（全部 JSON-safe）：``geometry.status`` 为 ``available`` / ``blocked``；
    ``blocked`` 时 ``geometry.required_evidence`` **必须非空**，且不输出任何顶点。

    ``allow_unconfirmed_origin`` 默认为 ``False``：**真实规划面阵**只有在安装高度
    已确认时才生成确定楔体。唯一允许传 ``True`` 的是明确标记的
    ``device_spec_preview``（用户主动选择的设备参数示意），且此时几何会带上
    ``origin_confirmed = False`` 与 ``preview_origin_not_confirmed = True``，
    绝不冒充已确认安装高度；``origin_status`` 为 ``missing``（高度未知）时
    **任何模式都 fail-closed**，不以 0 m 代替。

    ``panel`` 契约见 :func:`cns_planner.application.visualization_3d_sites.radar_units`。
    """

    azimuth_segments = int(azimuth_segments or DEFAULT_AZIMUTH_SEGMENTS)
    elevation_segments = int(elevation_segments or DEFAULT_ELEVATION_SEGMENTS)

    radar_type = str(panel.get("radar_type") or "")
    base = {
        "object_id": f"radar-panel:{panel.get('panel_id')}",
        "panel_id": panel.get("panel_id"),
        "tower_id": panel.get("tower_id"),
        "tower_name": panel.get("tower_name"),
        "radar_type": radar_type or None,
        "radar_type_label": RADAR_TYPE_LABELS.get(radar_type),
        "radar_type_declared": radar_type in RADAR_TYPES,
        "declared_azimuth_deg": _finite(panel.get("declared_azimuth_deg")),
        "azimuth_deg": _finite(panel.get("azimuth_deg")),
        "panel_half_width_deg": _finite(panel.get("panel_half_width_deg")),
        "longitude": panel.get("longitude"),
        "latitude": panel.get("latitude"),
        "coordinate_available": bool(panel.get("coordinate_available")),
        #: 站点解析（``radar_units``）用 ``origin_egm2008_m``，预览单元的等价字段名是
        #: ``service_origin_egm2008_m``；两者都表示**同一个** EGM2008 正高，绝不混入
        #: 源数据未确认垂直基准的 ``elevation_m``。
        "service_origin_egm2008_m": (
            panel.get("origin_egm2008_m")
            if panel.get("origin_egm2008_m") is not None
            else panel.get("service_origin_egm2008_m")
        ),
        "origin_status": panel.get("origin_status"),
        "origin_reason": panel.get("origin_reason"),
        "origin_source": panel.get("origin_source"),
        "origin_evidence_level": panel.get("origin_evidence_level"),
        "origin_confirmed": panel.get("origin_status") == "confirmed",
        "tower_top_status": panel.get("tower_top_status"),
        "tower_confirmed": panel.get("tower_confirmed") is True,
        "tower_profile_found": bool(panel.get("tower_profile_found")),
        "layout_route_id": panel.get("layout_route_id"),
        "layout_item_status": panel.get("layout_item_status"),
        "layout_stale_reason": panel.get("layout_stale_reason"),
        "layout_algorithm_version": panel.get("layout_algorithm_version"),
        "layout_proposal_only": bool(panel.get("layout_proposal_only")),
        "layout_operationally_adopted": bool(panel.get("layout_operationally_adopted")),
        "layout_altitude_layer_id": panel.get("layout_altitude_layer_id"),
        "layout_altitude_m": panel.get("layout_altitude_m"),
        "parameter_source": panel.get("parameter_source"),
        "geometry_scope": geometry_scope,
        "coverage_semantics": COVERAGE_SEMANTICS,
        "coverage_semantics_text": (
            "方向性工程**几何**覆盖楔体：只表达方向、仰角与斜距几何，"
            "不保证实测探测能力（RCS、Pd、地形遮挡、杂波、干扰一律未评估）"
        ),
        "model_scope": "geometric_only",
        "vertical_reference": "egm2008_orthometric",
        #: Radar 是**方向性非合作监视**；与 RID（全向合作监视）几何语义严格分离。
        "service_subtype": RADAR_TYPE_SEMANTICS.get("service_subtype", RADAR_SERVICE_SUBTYPE),
        "target_cooperation": RADAR_TYPE_SEMANTICS.get(
            "target_cooperation", RADAR_TARGET_COOPERATION),
        "technology": RADAR_TYPE_SEMANTICS.get("technology", RADAR_TECHNOLOGY),
        "not_a_rid_footprint": True,
        "omnidirectional": False,
        "forbidden_geometry": list(FORBIDDEN_GEOMETRY),
        "forbidden_geometry_note": (
            "本图层**不得**被渲染成全向圆或半球；单面阵恒为 90° 方向性扇区"
        ),
        "not_evaluated": dict(NOT_EVALUATED),
        "not_evaluated_semantics": (
            "以下因素在本几何模型中**未评估**；not_evaluated ≠ 通过，"
            "不得据此判定探测能力"
        ),
        "los": {
            "status": LOS_STATUS_UNKNOWN,
            "reason": LOS_UNKNOWN_REASON,
            "terrain_los_evaluated": False,
            "visible_region_available": False,
            "note": (
                "当前**没有**权威持久化的 Radar 地形 LOS 结果 ⇒ 遮挡状态恒为 unknown；"
                "本图层只负责几何展示与既有 LOS 状态披露，绝不把未持久化的推断写成 clear"
            ),
        },
        "geometry_semantics_separation": {
            "physical_maximum_coverage_wedge": True,
            "route_local_display_clipping_applied": False,
            "route_local_display_geometry_used": False,
            "los_confirmed_visible_region_used": False,
            "note": (
                "本响应是**物理最大覆盖楔体**，不含既有 display_geometry 的航路局部显示裁剪，"
                "也不代表已由 LOS 确认的可见区域"
            ),
        },
        "engineering_geometric_coverage_not_measured": True,
        "preview": bool(preview),
    }

    def _blocked(reason, required_evidence, detail=None):
        """统一的 blocked 几何块。

        即使几何被阻塞，**方向性语义也必须照常披露**（``omnidirectional = False``、
        90° 水平覆盖、0–45° 仰角），这样前端能显示"这是方向性设备、只是缺证"，
        而不是退化成"没有方向信息"。顶点数组恒为空：**绝不**用默认几何顶替，
        也绝不生成全向圆。
        """

        geometry = {
            "status": "blocked",
            "reason": reason,
            "required_evidence": list(required_evidence),
            "omnidirectional": False,
            "horizontal_coverage_deg": 2.0 * AZIMUTH_HALF_WIDTH_DEG,
            "azimuth_half_width_deg": AZIMUTH_HALF_WIDTH_DEG,
            "elevation_min_deg": ELEVATION_MIN_DEG,
            "elevation_center_deg": ELEVATION_CENTER_DEG,
            "elevation_max_deg": ELEVATION_MAX_DEG,
            "slant_range_semantics": "slant",
            "outer_surface_rings": [], "inner_surface_rings": [],
            "side_fans": [], "ground_arc": [], "near_arc": [],
            "azimuth_center_ray": [], "cross_section": [],
        }
        if detail is not None:
            geometry["detail"] = detail
        return geometry

    # ---- 1) 型号必须是被显式声明的已知型号（Radar-II 绝不自动升级） --------------
    if radar_type not in RADAR_TYPES:
        base["geometry"] = _blocked(
            "radar_type_not_declared_or_unknown",
            ["selected_panels[].radar_type ∈ {radar_i, radar_ii}（由权威布局显式声明）"],
            {"radar_type": radar_type or None, "supported": list(RADAR_TYPES)},
        )
        return base
    base["parameters"] = radar_geometry_parameters(radar_type)
    base["parameters_source"] = (
        "cns_planner.domain.radar_surveillance_layout.radar_geometry_parameters(%r)" % radar_type
    )
    parameters = base["parameters"]
    if radar_type == RADAR_TYPE_II:
        # Radar-II 只在**显式声明**时使用；正式规划策略禁止自动升级。
        base["radar_ii_explicitly_declared"] = True
        base["radar_ii_is_not_a_formal_plan_escalation"] = True

    # ---- 2) 安装高度（EGM2008 正高）必须已确认 ---------------------------------
    # 真实规划面阵（allow_unconfirmed_origin=False）：``unconfirmed`` 也必须 blocked。
    # device_spec_preview（用户**显式**承认这是示意）：允许使用有来源但未确认的塔顶
    # 派生正高，但仍逐项标注 origin_confirmed=False；``missing`` 任何模式都 blocked。
    origin_usable_for_preview = (
        bool(allow_unconfirmed_origin) and bool(preview)
        and base["origin_status"] == ORIGIN_UNCONFIRMED
        and _finite(base["service_origin_egm2008_m"]) is not None
    )
    if base["origin_status"] != "confirmed" and not origin_usable_for_preview:
        base["geometry"] = _blocked(
            panel.get("origin_reason") or "radar_origin_not_confirmed",
            [
                "tower_obstacle_profiles.items[<tower_id>].tower_top_orthometric_m（已确认）",
                "或设备安装记录 vertical_profile.service_origin_egm2008_m（已确认）",
                "缺少已确认的 EGM2008 正高时**不得**生成确定楔体，也不得以 0 m 代替",
            ],
            {
                "origin_status": base["origin_status"],
                "tower_top_status": base["tower_top_status"],
                "tower_top_orthometric_m": panel.get("tower_top_orthometric_m"),
                "tower_profile_found": base["tower_profile_found"],
            },
        )
        return base

    if origin_usable_for_preview:
        base["preview_origin_acknowledgement_required"] = True
        base["preview_origin_acknowledged_by_user"] = True
        base["preview_origin_note"] = (
            "示意原点取自 tower_obstacle_profiles 的塔顶**派生**正高，"
            "该值**未确认**（confirmed=false）；它只用于展示设备方向性，"
            "不代表已确认的设备安装高度，也不代表已采纳布设"
        )

    # ---- 3) 水平坐标必须存在且可用 --------------------------------------------
    longitude, latitude = _finite(base["longitude"]), _finite(base["latitude"])
    if longitude is None or latitude is None:
        base["geometry"] = _blocked(
            "radar_site_coordinate_missing",
            ["tower_obstacle_profiles.items[<tower_id>].longitude / latitude"],
        )
        return base

    azimuth = _finite(base["azimuth_deg"])
    if azimuth is None:
        base["geometry"] = _blocked(
            "panel_azimuth_missing",
            ["selected_panels[].azimuth_deg（真实面阵方位角）"],
        )
        return base
    azimuth = normalize_azimuth_deg(azimuth)
    base["azimuth_deg"] = azimuth
    start, end, wrapped = azimuth_window_deg(azimuth, AZIMUTH_HALF_WIDTH_DEG)
    base["azimuth_window"] = {
        "center_deg": azimuth,
        "start_deg": start,
        "end_deg": end,
        "wrapped_across_north": wrapped,
        "span_deg": 2.0 * AZIMUTH_HALF_WIDTH_DEG,
        "half_width_deg": AZIMUTH_HALF_WIDTH_DEG,
        "half_width_source": "domain.radar_surveillance_layout.AZIMUTH_HALF_WIDTH_DEG",
    }

    origin_z = _finite(base["service_origin_egm2008_m"])
    if origin_z is None:
        base["geometry"] = _blocked(
            "service_origin_egm2008_m_not_recorded",
            ["已确认的 Radar 相位中心 EGM2008 正高"],
        )
        return base

    to_metric, to_wgs84 = metric_transformer(metric_crs)
    if to_metric is None:
        base["geometry"] = _blocked(
            "metric_transform_unavailable",
            ["可用的 PROJ 米制变换（%s）" % metric_crs],
            {"metric_crs": metric_crs},
        )
        return base
    center = to_metric(longitude, latitude)
    if center is None:
        base["geometry"] = _blocked(
            "site_coordinate_transform_failed",
            ["站点坐标（%s）可投影" % metric_crs],
        )
        return base

    inner_slant = float(parameters["min_slant_range_m"])
    outer_slant = float(parameters["max_slant_range_m"])
    layers = wedge_directions(
        azimuth, azimuth_segments=azimuth_segments, elevation_segments=elevation_segments)
    outer_rings = _slant_surface_rings(
        center[0], center[1], origin_z, outer_slant,
        azimuth_deg=azimuth, layers=layers, to_wgs84=to_wgs84)
    inner_rings = _slant_surface_rings(
        center[0], center[1], origin_z, inner_slant,
        azimuth_deg=azimuth, layers=layers, to_wgs84=to_wgs84)
    fans = [
        _side_fan(center[0], center[1], origin_z, azimuth_deg=azimuth,
                  inner_slant_m=inner_slant, outer_slant_m=outer_slant,
                  layers=layers, to_wgs84=to_wgs84, edge=edge)
        for edge in ("start", "end")
    ]
    ground_arc = [
        _point(center[0] + slant * math.sin(math.radians(azimuth_value)),
               center[1] + slant * math.cos(math.radians(azimuth_value)),
               origin_z, to_wgs84)
        for slant, azimuth_value in (
            [(inner_slant, az) for az in layers[0][1]]
            + [(outer_slant, az) for az in reversed(layers[0][1])]
        )
    ]
    # 近端弧（斜距 = min，elevation = 0）与**中心方位射线**：把"中心方位、近远斜距"
    # 在三维里显式画出来，避免只靠文字说明（也是"斜距 ≠ 水平半径"的可视证据）。
    near_arc = [
        _point(center[0] + inner_slant * math.sin(math.radians(azimuth_value)),
               center[1] + inner_slant * math.cos(math.radians(azimuth_value)),
               origin_z, to_wgs84)
        for azimuth_value in layers[0][1]
    ]
    bearing = math.radians(azimuth)
    azimuth_center_ray = [
        _point(center[0], center[1], origin_z, to_wgs84),
        _point(center[0] + outer_slant * math.sin(bearing),
               center[1] + outer_slant * math.cos(bearing),
               origin_z, to_wgs84),
    ]
    if (not outer_rings or not all(outer_rings) or not all(fans)
            or None in ground_arc or None in near_arc or None in azimuth_center_ray):
        base["geometry"] = _blocked(
            "geometry_projection_failed",
            ["可用的 %s ↔ WGS84 双向投影" % metric_crs],
        )
        return base

    base["geometry"] = {
        "status": "available",
        "reason": None,
        "geometry_model": GEOMETRY_MODEL,
        "model_scope": "geometric_only",
        "metric_crs": metric_crs,
        "omnidirectional": False,
        "horizontal_coverage_deg": 2.0 * AZIMUTH_HALF_WIDTH_DEG,
        "azimuth_center_deg": azimuth,
        "azimuth_start_deg": start,
        "azimuth_end_deg": end,
        "azimuth_wrapped_across_north": wrapped,
        "elevation_min_deg": ELEVATION_MIN_DEG,
        "elevation_center_deg": ELEVATION_CENTER_DEG,
        "elevation_max_deg": ELEVATION_MAX_DEG,
        "min_slant_range_m": inner_slant,
        "max_slant_range_m": outer_slant,
        #: 斜距是判据：楔体面上的点到雷达原点的**斜距**恒等于对应层的 slant。
        "slant_range_semantics": "slant",
        "slant_range_is_criterion": True,
        #: **唯一**与二维圆重合的截面（elev = 0）；其余截面的水平半径都更小。
        "horizontal_radius_at_elevation_zero_m": outer_slant,
        "horizontal_radius_is_not_the_slant_range": True,
        "two_dimensional_disk_is_not_the_volume": True,
        "apex_egm2008_m": round(origin_z + outer_slant * math.sin(
            math.radians(ELEVATION_MAX_DEG)), 3),
        #: 正高的确认语义必须随几何一起披露：预览使用未确认塔顶正高时如实标注。
        "origin_confirmed": base["origin_status"] == "confirmed",
        "preview_origin_not_confirmed": base["origin_status"] != "confirmed",
        "origin_status": base["origin_status"],
        "zenith_note": (
            "仰角以雷达原点的**局部水平面**为基准，正仰角向上；"
            "仰角上限 %.1f° 处的相对高度恒为 斜距 × sin(%.1f°)"
            % (ELEVATION_MAX_DEG, ELEVATION_MAX_DEG)
        ),
        "azimuth_segments": int(max(2, min(azimuth_segments, MAX_AZIMUTH_SEGMENTS))),
        "elevation_segments": int(max(1, min(elevation_segments, MAX_ELEVATION_SEGMENTS))),
        "outer_surface_rings": outer_rings,
        "inner_surface_rings": inner_rings,
        "side_fans": [{"edge": edge, "ring": ring}
                      for edge, ring in zip(("start", "end"), fans)],
        "ground_arc": ground_arc,
        "near_arc": near_arc,
        "azimuth_center_ray": azimuth_center_ray,
        "cross_section": cross_section_table(parameters),
        "vertex_semantics": (
            "顶点为 [lon, lat, egm2008_orthometric_m]；"
            "外曲面每层仰角一圈（方位自 -45° 到 +45°，含两端），端面为竖直扇形；"
            "ground_arc / near_arc 为 elevation=0 处的外/内水平弧，"
            "azimuth_center_ray 为自雷达原点沿中心方位指向外沿的两点线"
        ),
    }
    return base


def build_radar_volumes(panels, *, metric_crs="EPSG:32651", azimuth_segments=None,
                        elevation_segments=None, geometry_scope="project_layout",
                        allow_unconfirmed_origin=False, preview=False):
    """构造全部 Radar 面阵的楔体（有界：最多 :data:`MAX_PANELS` 个）。"""

    all_panels = list(panels or [])
    selected = all_panels[:MAX_PANELS]
    volumes = [
        build_radar_volume(
            panel, metric_crs=metric_crs, azimuth_segments=azimuth_segments,
            elevation_segments=elevation_segments, geometry_scope=geometry_scope,
            allow_unconfirmed_origin=allow_unconfirmed_origin, preview=preview)
        for panel in selected
    ]
    available = [item for item in volumes if item["geometry"]["status"] == "available"]
    blocked = [item for item in volumes if item["geometry"]["status"] != "available"]
    return {
        "volumes": volumes,
        "panel_count": len(volumes),
        "geometry_count": len(available),
        "available_count": len(available),
        "blocked_count": len(blocked),
        "truncated": len(all_panels) > MAX_PANELS,
        "blocked_reasons": sorted({
            str((item["geometry"] or {}).get("reason"))
            for item in blocked if (item["geometry"] or {}).get("reason")
        }),
    }


__all__ = [
    "COVERAGE_SEMANTICS", "CROSS_SECTION_ELEVATIONS_DEG", "DEFAULT_AZIMUTH_SEGMENTS",
    "DEFAULT_ELEVATION_SEGMENTS", "FORBIDDEN_GEOMETRY", "GEOMETRY_MODEL",
    "LOS_STATUS_UNKNOWN", "LOS_UNKNOWN_REASON", "MAX_AZIMUTH_SEGMENTS",
    "MAX_ELEVATION_SEGMENTS", "MAX_PANELS", "ORIGIN_UNCONFIRMED", "azimuth_window_deg",
    "build_radar_volume", "build_radar_volumes", "cross_section_table",
    "normalize_azimuth_deg", "wedge_directions",
]
