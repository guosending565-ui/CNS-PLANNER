"""通信（``C:communication``）**三维工程几何包络**的只读构造。

语义边界（必须逐条遵守，违反即返工）：

1. **不是实测覆盖**：输出的是 ``engineering_geometric_coverage_not_measured`` ——
   工程规划几何包络，**不保证**实测接收能力、场强或链路可用性。
2. **三维斜距**：包络由 ``slant = hypot(horizontal, delta)`` 约束，因此它是
   **球/半球壳**而不是"二维相应半径 圆柱"。二维圆盘只是它在某一高度层上的切片。
3. **半球有据可依**：几何模型取自设备 ``coverage_geometry.model``（冻结契约为
   ``hemisphere``），并由 :data:`...OMNIDIRECTIONAL_HEMISPHERE` 显式声明
   ``omnidirectional=True`` / ``horizontal_coverage_deg=360``。本模块**绝不**引入
   任何方位角、扇区或面板参数（那是 Radar 的几何）。
4. **半径权威**：半径一律经 :func:`...effective_radius_m` 按 surface 解析
   （``radius_by_surface`` 优先，legacy ``slant_range_m`` 次之），
   **绝不**自行取数或为了让包络好看而放宽。
5. **未确认安装高度 ⇒ 不生成确定包络**：``origin_status != confirmed`` 时不输出
   ``geometry``，只输出明确原因与所需证据。
6. **未评估项必须披露**：遮挡、地形 LOS、绕射、干扰、链路预算、容量、吞吐、
   时延一律 ``not_evaluated``（取自冻结契约的 ``not_evaluated`` 元组），
   绝不冒充"已评估且通过"。

几何实现全部在**显式米制 CRS**（默认 ``EPSG:32651``，与项目既有陆域/地形查询
同一帧）中完成，再转回 WGS84，因此不是"按经纬度差近似"的椭圆。
"""

from __future__ import annotations

import math

from ..domain.cns_service_contract import (
    COMMUNICATION_NOT_EVALUATED,
    ENGINEERING_PLANNING_BASELINE,
    OMNIDIRECTIONAL_HEMISPHERE,
)

#: 通信参数的成熟度与来源标注（复用冻结契约常量，不新造字符串）。
COMMUNICATION_MATURITY = ENGINEERING_PLANNING_BASELINE
COMMUNICATION_PARAMETER_SEMANTICS = (
    "engineering_planning_baseline_not_verified_device_specification"
)

#: 输出标注：工程几何包络，非实测。
COVERAGE_SEMANTICS = "engineering_geometric_coverage_not_measured"

#: 半球包络的默认垂直采样层数（只影响显示平滑度，不改变任何半径）。
DEFAULT_VERTICAL_SEGMENTS = 12

#: 单次响应的硬上限（有界分块，避免超大 payload）。
MAX_UNITS = 64
MAX_AZIMUTH_SEGMENTS = 180


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def metric_transformer(metric_crs):
    """构造 ``(to_metric, to_wgs84)`` 一对显式变换；不可用时返回 ``(None, None)``。"""

    try:
        from pyproj import CRS, Transformer

        to_metric = Transformer.from_crs(
            CRS.from_epsg(4326), CRS.from_user_input(metric_crs), always_xy=True)
        to_wgs84 = Transformer.from_crs(
            CRS.from_user_input(metric_crs), CRS.from_epsg(4326), always_xy=True)
    except Exception:
        return None, None

    def forward(longitude, latitude):
        try:
            x, y = to_metric.transform(float(longitude), float(latitude))
        except Exception:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return float(x), float(y)

    def backward(x, y):
        try:
            first, second = to_wgs84.transform(float(x), float(y))
        except Exception:
            return None
        if not (math.isfinite(first) and math.isfinite(second)):
            return None
        return float(first), float(second)

    return forward, backward


def _hemisphere_vertices(center_x, center_y, origin_z, radius_m, *, azimuth_segments,
                         vertical_segments, to_wgs84):
    """半球壳顶点（``[lon, lat, egm2008_orthometric_m]``）。

    半球定义：``delta = z - origin_z >= 0``（**向上**半球，与 P7 的
    ``vertical_ok = model == "sphere" or delta >= 0`` 一致），且
    ``hypot(horizontal, delta) <= radius``。

    参数化（上半球，天顶角 ``phi`` 从 0=天顶 到 pi/2=水平面）：

    * 水平半径 ``rho(phi) = radius * sin(phi)``；
    * 相对高度 ``dz(phi) = radius * cos(phi)``。

    每个 ``phi`` 上取一圈 ``azimuth_segments`` 个点（**全向 360°**，无任何方位偏好）。
    顶点先按 ``phi`` 分层、每层闭合（首点重复），得到三角带可直接渲染的顶点序。
    """

    rings = []
    for index in range(int(vertical_segments) + 1):
        phi = (math.pi / 2.0) * index / float(vertical_segments)
        rho = radius_m * math.sin(phi)
        dz = radius_m * math.cos(phi)
        ring = []
        for step in range(int(azimuth_segments) + 1):
            theta = 2.0 * math.pi * step / float(azimuth_segments)
            x = center_x + rho * math.cos(theta)
            y = center_y + rho * math.sin(theta)
            point = to_wgs84(x, y)
            if point is None:
                return []
            ring.append([
                round(point[0], 9), round(point[1], 9),
                round(origin_z + dz, 3),
            ])
        rings.append(ring)
    return rings


def _horizontal_section(center_x, center_y, origin_z, radius_m, *, azimuth_segments,
                        to_wgs84):
    """``delta = 0`` 处的水平切片（二维圆盘边界）—— 这是**唯一**与二维圆重合的截面。"""

    ring = []
    for step in range(int(azimuth_segments) + 1):
        theta = 2.0 * math.pi * step / float(azimuth_segments)
        point = to_wgs84(center_x + radius_m * math.cos(theta),
                         center_y + radius_m * math.sin(theta))
        if point is None:
            return []
        ring.append([round(point[0], 9), round(point[1], 9), round(origin_z, 3)])
    return ring


def build_communication_volume(
        unit, *, metric_crs="EPSG:32651", azimuth_segments=72, vertical_segments=None,
        surface_class=None, max_radius_m=None):
    """构造**一个**通信服务单元的三维几何包络。

    返回结构（全部 JSON-safe）：

    * ``geometry.status``：``available`` / ``blocked``；
    * ``geometry.shell_rings``：半球壳顶点环（每环 ``[lon, lat, H_egm2008]``）；
    * ``geometry.horizontal_section``：``delta = 0`` 的水平切片；
    * ``geometry.slant_range_m`` / ``geometry.radius_source``：半径与其权威来源；
    * ``not_evaluated``：冻结契约里未评估的因素（遮挡 / 干扰 / 链路预算 …）。

    ``origin_status != confirmed`` 时**不生成任何确定包络**，只返回 blocked 与原因。
    """

    azimuth_segments = max(12, min(int(azimuth_segments or 72), MAX_AZIMUTH_SEGMENTS))
    vertical_segments = int(vertical_segments or DEFAULT_VERTICAL_SEGMENTS)
    vertical_segments = max(2, min(vertical_segments, 36))

    base = {
        "object_id": f"comm-volume:{unit.get('device_id')}",
        "device_id": unit.get("device_id"),
        "facility_id": unit.get("facility_id"),
        "site_id": unit.get("site_id"),
        "distinct_site_id": unit.get("distinct_site_id"),
        "site_name": unit.get("site_name"),
        "subsystem": unit.get("subsystem"),
        "service_key": unit.get("service_key"),
        "longitude": unit.get("longitude"),
        "latitude": unit.get("latitude"),
        "service_origin_egm2008_m": unit.get("service_origin_egm2008_m"),
        "origin_status": unit.get("origin_status"),
        "origin_reason": unit.get("origin_reason"),
        "origin_source": unit.get("origin_source"),
        "origin_confirmed": unit.get("origin_status") == "confirmed",
        "coverage_semantics": COVERAGE_SEMANTICS,
        "model_scope": "geometric_only",
        "radius_basis": "geometric_planning_radius",
        "vertical_reference": "egm2008_orthometric",
        "surface_class_used": surface_class,
    }
    contract = unit.get("service_contract") or {}
    policy = contract.get("policy") or {}
    base["service_contract"] = {
        "service_key": contract.get("service_key"),
        "service_surface_dependent": contract.get("service_surface_dependent"),
        "radius_basis": policy.get("radius_basis") or "geometric_planning_radius",
        "maturity": policy.get("maturity") or COMMUNICATION_MATURITY,
        "parameter_semantics": (policy.get("parameter_semantics")
                                or COMMUNICATION_PARAMETER_SEMANTICS),
        "geometry": policy.get("geometry") or dict(OMNIDIRECTIONAL_HEMISPHERE),
        "missing_device_evidence": list(policy.get("missing_device_evidence") or []),
    }
    # **未评估项必须披露**（与 RID 同一缺陷同一修法）：若服务契约回落到某个
    # 冻结策略表里不存在的 legacy key，``policy`` 会为空，``not_evaluated`` 就会
    # 变成空数组 —— 那是漏披露。本端点声明的是全向通信服务，因此回落到冻结的
    # :data:`COMMUNICATION_NOT_EVALUATED`。
    declared_not_evaluated = list(policy.get("not_evaluated") or [])
    base["not_evaluated"] = declared_not_evaluated or list(COMMUNICATION_NOT_EVALUATED)
    base["not_evaluated_source"] = (
        "service_contract_policy" if declared_not_evaluated
        else "frozen_contract_fallback_device_service_key_is_legacy")
    base["not_evaluated_semantics"] = (
        "以下因素在几何规划模型中**未评估**；not_evaluated ≠ 通过，"
        "不得据此判定通信可用"
    )
    base["limitations"] = [
        "工程几何包络（geometric_only）：只有距离几何，没有场强/LOS/链路预算",
        "不保证实测接收能力；实际可用性取决于地形遮挡、干扰与设备性能",
    ]

    # 1) 安装高度必须已确认：否则**绝不**生成确定的三维空间包络。
    if unit.get("origin_status") != "confirmed":
        base["geometry"] = {
            "status": "blocked",
            "reason": unit.get("origin_reason") or "service_origin_not_confirmed",
            "required_evidence": [
                "site.vertical_profile.service_origin_egm2008_m（已确认）",
                "或 device_catalog[].vertical_profile.service_origin_egm2008_m（已确认）",
            ],
            "note": (
                "未确认设备安装高度时不得生成确定的三维空间包络："
                "否则包络的垂直位置是虚构的"
            ),
            "shell_rings": [],
            "horizontal_section": [],
        }
        return base

    # 2) 站址坐标必须存在。
    longitude, latitude = _finite(unit.get("longitude")), _finite(unit.get("latitude"))
    if longitude is None or latitude is None:
        base["geometry"] = {
            "status": "blocked", "reason": "site_coordinate_missing",
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    # 3) 半径必须由权威函数解析。
    from ..domain.cns_service_contract import (
        effective_radius_m, geometry_usable, index_max_range_m, radius_by_surface_of,
    )

    geometry = unit.get("geometry") or {}
    if not geometry_usable(geometry):
        base["geometry"] = {
            "status": "blocked", "reason": "coverage_geometry_not_usable",
            "detail": {
                "model": geometry.get("model"),
                "radius_by_surface": geometry.get("radius_by_surface"),
                "slant_range_m": geometry.get("slant_range_m"),
            },
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    per_surface = radius_by_surface_of(geometry) or {}
    base["radius_by_surface_m"] = dict(per_surface) or None

    # 半径口径**必须显式区分**，绝不混用：
    #  * 给定可判定的 surface_class ⇒ 用 effective_radius_m 取该地表的规划半径
    #    （与 P7 判定逐项一致）；
    #  * 未给定 / unknown ⇒ 只显示**地表包络**（各档半径的最大值 = 并集的外边界），
    #    并显式标注 envelope_over_surfaces=true。它**不是**某个地表的覆盖判定，
    #    因此绝不允许被读成"unknown 地表也已覆盖"。
    normalized = str(surface_class or "").strip().lower()
    if normalized in ("land", "coastal_uncertain", "sea"):
        resolution = effective_radius_m(geometry, normalized)
        radius = _finite(resolution.get("radius_m"))
        radius_basis = "per_surface_resolution"
        envelope_over_surfaces = False
    else:
        resolution = {
            "surface_class": normalized or None,
            "radius_m": index_max_range_m(geometry),
            "source": ("radius_by_surface" if per_surface else "legacy_slant_range_m"),
            "status": "envelope",
            "reason": "surface_class_not_provided_envelope_displayed",
        }
        radius = _finite(resolution.get("radius_m"))
        radius_basis = "surface_envelope_max"
        envelope_over_surfaces = True

    base["geometry_resolution"] = {
        "surface_class": resolution.get("surface_class"),
        "radius_m": radius,
        "radius_source": resolution.get("source"),
        "status": resolution.get("status"),
        "reason": resolution.get("reason"),
        "radius_basis": radius_basis,
        "envelope_over_surfaces": envelope_over_surfaces,
    }
    if radius is None:
        base["geometry"] = {
            "status": "blocked", "reason": "coverage_radius_unresolved",
            "detail": base["geometry_resolution"],
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    model = str(geometry.get("model") or "")
    if model not in ("hemisphere", "sphere"):
        base["geometry"] = {
            "status": "blocked", "reason": "geometry_model_not_omnidirectional",
            "detail": {"model": model},
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    to_metric, to_wgs84 = metric_transformer(metric_crs)
    if to_metric is None:
        base["geometry"] = {
            "status": "blocked", "reason": "metric_transform_unavailable",
            "detail": {"metric_crs": metric_crs},
            "shell_rings": [], "horizontal_section": [],
        }
        return base
    center = to_metric(longitude, latitude)
    if center is None:
        base["geometry"] = {
            "status": "blocked", "reason": "site_coordinate_transform_failed",
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    origin_z = _finite(unit.get("service_origin_egm2008_m"))
    if origin_z is None:
        base["geometry"] = {
            "status": "blocked", "reason": "service_origin_egm2008_m_not_recorded",
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    rings = _hemisphere_vertices(
        center[0], center[1], origin_z, radius,
        azimuth_segments=azimuth_segments, vertical_segments=vertical_segments,
        to_wgs84=to_wgs84)
    section = _horizontal_section(
        center[0], center[1], origin_z, radius,
        azimuth_segments=azimuth_segments, to_wgs84=to_wgs84)
    if not rings or not section:
        base["geometry"] = {
            "status": "blocked", "reason": "geometry_projection_failed",
            "shell_rings": [], "horizontal_section": [],
        }
        return base

    base["geometry"] = {
        "status": "available",
        "reason": None,
        "model": model,
        "model_scope": geometry.get("model_scope") or "geometric_only",
        "omnidirectional": True,
        "horizontal_coverage_deg": 360.0,
        "azimuth_segments": azimuth_segments,
        "vertical_segments": vertical_segments,
        "slant_range_m": radius,
        "radius_source": resolution.get("source"),
        "radius_basis": radius_basis,
        "envelope_over_surfaces": envelope_over_surfaces,
        "metric_crs": metric_crs,
        #: 三维斜距是判据：包络面上的点到服务原点的**斜距**恒等于 radius。
        "slant_distance_is_criterion": True,
        "two_dimensional_disk_is_not_the_volume": True,
        "horizontal_radius_at_origin_height_m": radius,
        "apex_egm2008_m": round(origin_z + radius, 3),
        "zenith_note": (
            "顶点为天顶方向 radius 处；水平面处水平半径 = radius。"
            "**二维相应半径 圆盘只是 delta=0 处的切片**，不是完整三维包络"
        ),
        "envelope_note": (
            "未指定可判定地表类别 ⇒ 显示的是**地表包络**（各档规划半径的最大值，"
            "即并集外边界）。它是显示用外边界，**不是**任何单一地表的覆盖判定；"
            "实际是否覆盖必须按目标点的真实 surface_class 重新解析半径"
            if envelope_over_surfaces else None
        ),
        #: 顶点环：``[[lon, lat, H_egm2008_m], ...]``，每环首点重复以闭合。
        "shell_rings": rings,
        "horizontal_section": section,
        "shell_ring_semantics": (
            "上半球壳；第 0 环为天顶（半径 0），末环为水平面（水平半径 = slant_range_m）"
        ),
    }
    return base


def build_communication_volumes(
        units, *, metric_crs="EPSG:32651", azimuth_segments=72, vertical_segments=None,
        surface_class=None, route_intersection=None):
    """构造全部通信单元的三维包络（有界：最多 :data:`MAX_UNITS` 个）。"""

    selected = list(units or [])[:MAX_UNITS]
    volumes = [
        build_communication_volume(
            unit, metric_crs=metric_crs, azimuth_segments=azimuth_segments,
            vertical_segments=vertical_segments, surface_class=surface_class)
        for unit in selected
    ]
    available = [item for item in volumes if item["geometry"]["status"] == "available"]
    blocked = [item for item in volumes if item["geometry"]["status"] != "available"]
    if route_intersection is not None:
        for item in volumes:
            item["route_intersection"] = route_intersection
    return {
        "volumes": volumes,
        "volume_count": len(volumes),
        "available_count": len(available),
        "blocked_count": len(blocked),
        "truncated": len(list(units or [])) > MAX_UNITS,
    }


__all__ = [
    "COVERAGE_SEMANTICS", "DEFAULT_VERTICAL_SEGMENTS", "MAX_AZIMUTH_SEGMENTS", "MAX_UNITS",
    "build_communication_volume", "build_communication_volumes", "metric_transformer",
]
