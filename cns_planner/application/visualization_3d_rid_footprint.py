"""RID（``S:rid_cooperative``）**陆海分区非正圆**二维 footprint 的只读构造。

与既有 :mod:`cns_planner.application.rid_surface_footprint` 的关系：那个模块服务于
专题成果图（``map_figure_service``），用"2 km 基圆 + 海侧 2–5 km 环带减去陆域多边形"
的方式出图。本模块服务于**三维只读端点**，并且按任务要求做得更强：

* **逐点调用权威分类器** :meth:`cns_planner.gis.radar_layout_adapter.LandMaskSource.classify_many`
  —— 本模块**不复制**任何多边形/缓冲区/海岸线算法，也不自行判定海陆；
* footprint 的边界半径是**空间分类结果**：沿每个方位角从站心向外走，遇到第一个
  "该点地表类别所允许半径之外"的点就停止。因此：

  - 陆域 / 海岸不确定带（land / coastal_uncertain）方向最多 2000 m；
  - 海域（sea）方向最多 5000 m；
  - ``unknown`` 方向**立即停止**且不产生几何（fail-closed，绝不按陆地或海上处理）。

* 由此得到的轮廓**必然是非正圆**：它沿海岸线变化。回归测试会断言
  "同一站点不同方位角的半径标准差 > 0"且"半径集合中同时存在 2000 与 5000 量级"，
  以此排除"两个同心圆冒充"的实现。

三维表达：RID **没有**已验证的垂直有效范围（冻结契约里 ``not_evaluated`` 含
``real_antenna_pattern`` 等，且状态中没有垂直有效范围字段），因此本模块只输出
**有依据的高度切片**（服务原点 EGM2008 正高处的水平 footprint），并显式声明
``vertical_effective_range = not_modelled``；**绝不**把二维轮廓拉伸成确定高度的
完整覆盖体。

RID **绝不允许**被表现成 Radar 方向性扇区：输出里显式携带
``forbidden_geometry`` 与 ``omnidirectional = True``，且不产生任何方位角权重。
"""

from __future__ import annotations

import json
import math

from ..domain.cns_service_contract import (
    ENGINEERING_PLANNING_BASELINE as RID_MATURITY,
    PARAMETER_ORIGIN as RID_PARAMETER_ORIGIN,
    RID_FORBIDDEN_GEOMETRY,
    RID_NOT_EVALUATED,
)
from .visualization_3d_communication_volume import metric_transformer

#: RID 参数的成熟度与来源标注（复用冻结契约常量，不新造字符串）。
RID_PARAMETER_SEMANTICS = (
    "engineering_planning_baseline_not_verified_device_specification"
)

#: 输出标注：合作监视工程几何，非实测接收能力。
FOOTPRINT_SEMANTICS = "engineering_geometric_cooperative_surveillance_not_measured"

#: 默认方位角数量与径向步长（米）。步长决定轮廓精度，不改变任何半径策略。
DEFAULT_AZIMUTH_SEGMENTS = 72
DEFAULT_RADIAL_STEP_M = 50.0

MAX_AZIMUTH_SEGMENTS = 180
MAX_RADIAL_SAMPLES = 400

#: 单次响应硬上限。
MAX_SITES = 64


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _classify_points(classifier, points):
    """逐点调用**权威**分类器；返回类别列表。

    ``classifier`` 必须是既有的 ``LandMaskSource`` 实例（或等价 provider）。
    **绝不**在本模块内实现第二套海陆判定。
    """

    return list(classifier.classify_many(points))


def _ring_area_is_degenerate(points):
    """环是否退化为点/线（面积可忽略）。保留给诊断与单测使用。"""

    if len(points) < 3:
        return True
    area = 0.0
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0 < 1e-12


def _class_runs(classes):
    """把逐点类别序列压缩成游程 ``[[surface_class, count], ...]``。"""

    runs = []
    for surface in classes:
        surface = str(surface or "unknown")
        if runs and runs[-1][0] == surface:
            runs[-1][1] += 1
        else:
            runs.append([surface, 1])
    return runs


def _accepts_at(land_radius_m, sea_radius_m):
    """某点在给定半径处是否仍被接受 —— 与主循环**同一判据**。

    ``unknown`` 一律不接受（fail-closed）；陆/海岸不确定带只在 ``land_radius_m``
    之内被接受，海只在 ``sea_radius_m`` 之内被接受。二分细化因此**不可能**越过
    任何策略半径。
    """

    def accept(surface, offset):
        surface = str(surface or "unknown")
        offset = float(offset)
        if surface in ("land", "coastal_uncertain"):
            return offset <= land_radius_m
        if surface == "sea":
            return offset <= sea_radius_m
        return False

    return accept


def _refine_radius(classifier, to_wgs84, center, cos_t, sin_t, *, low, high, accept,
                   iterations=12):
    """在 ``[low, high]`` 内二分求"最后一个被接受"的半径。

    这是**几何精度**步骤，不改变任何策略半径：``low`` 已被接受、``high`` 已被拒绝，
    因此收敛结果始终落在同一策略区间内。每一步都重新调用**权威分类器**，
    因此细分后的边界依然由真实地表分类决定，而不是由采样步长决定。
    """

    low_value, high_value = float(low), float(high)
    for _ in range(int(iterations)):
        middle = (low_value + high_value) / 2.0
        point = to_wgs84(center[0] + middle * cos_t, center[1] + middle * sin_t)
        if point is None:
            break
        surface = _classify_points(classifier, [point])
        if surface and accept(surface[0], middle):
            low_value = middle
        else:
            high_value = middle
    return low_value


def _blocked_reason(unit):
    """阻塞原因码：优先用解析器给出的具体原因，否则按 origin_status 归一。"""

    reason = unit.get("origin_reason")
    if reason:
        return str(reason)
    status = str(unit.get("origin_status") or "").strip()
    if status == "missing":
        return "service_origin_egm2008_m_not_recorded"
    return "service_origin_not_confirmed"


def build_rid_footprint(
        unit, classifier, *, metric_crs="EPSG:32651",
        land_radius_m=2000.0, sea_radius_m=5000.0,
        azimuth_segments=DEFAULT_AZIMUTH_SEGMENTS,
        radial_step_m=DEFAULT_RADIAL_STEP_M,
        surface_class_facts_fingerprint=None):
    """构造一个 RID 站点的陆海分区 footprint（只读、JSON-safe）。

    返回结构要点：

    * ``geometry.status``：``available`` / ``blocked``；
    * ``geometry.geojson``：合法 GeoJSON **MultiPolygon**（WGS84，``OGC:CRS84``）；
    * ``geometry.parts``：每个部分带 ``surface_class``；
    * ``geometry.radial_profile``：逐方位角的边界半径与类别序列 —— 这是
      "非正圆由空间分类决定"的**可回归证据**；
    * ``vertical``：只给出有依据的高度切片，并显式声明垂直有效范围未建模。
    """

    azimuth_segments = max(8, min(int(azimuth_segments or DEFAULT_AZIMUTH_SEGMENTS),
                                  MAX_AZIMUTH_SEGMENTS))
    step = _finite(radial_step_m) or DEFAULT_RADIAL_STEP_M
    step = max(10.0, min(step, 500.0))
    land_radius = _finite(land_radius_m)
    sea_radius = _finite(sea_radius_m)
    samples = int(math.ceil((sea_radius or 0.0) / step))
    samples = max(1, min(samples, MAX_RADIAL_SAMPLES))

    base = {
        "object_id": f"rid-footprint:{unit.get('device_id')}",
        "device_id": unit.get("device_id"),
        "facility_id": unit.get("facility_id"),
        "site_id": unit.get("site_id"),
        "distinct_site_id": unit.get("distinct_site_id"),
        "site_name": unit.get("site_name"),
        "subsystem": unit.get("subsystem"),
        "service_key": unit.get("service_key"),
        "declaration_basis": unit.get("declaration_basis"),
        "declaration_note": unit.get("declaration_note"),
        "rid_type_declared": bool(unit.get("rid_type_declared")),
        "longitude": unit.get("longitude"),
        "latitude": unit.get("latitude"),
        "service_origin_egm2008_m": unit.get("service_origin_egm2008_m"),
        "origin_status": unit.get("origin_status"),
        "origin_reason": unit.get("origin_reason"),
        "origin_confirmed": unit.get("origin_status") == "confirmed",
        "coverage_semantics": FOOTPRINT_SEMANTICS,
        "model_scope": "geometric_only",
        "radius_basis": "geometric_planning_radius",
        "vertical_reference": "egm2008_orthometric",
        "omnidirectional": True,
        "horizontal_coverage_deg": 360.0,
        "forbidden_geometry": ["radar_90_degree_panel", "sector",
                               "horizontal_semicircle", "panel_azimuth"],
        "not_a_radar_sector": True,
        "radius_by_surface_m": {
            "land": land_radius, "coastal_uncertain": land_radius, "sea": sea_radius,
        },
    }
    contract = unit.get("service_contract") or {}
    policy = contract.get("policy") or {}
    base["service_contract"] = {
        "service_key": contract.get("service_key"),
        "service_surface_dependent": contract.get("service_surface_dependent"),
        "radius_basis": policy.get("radius_basis") or "geometric_planning_radius",
        "maturity": policy.get("maturity") or RID_MATURITY,
        "parameter_semantics": (policy.get("parameter_semantics")
                                or RID_PARAMETER_SEMANTICS),
        "urban_enabled": policy.get("urban_enabled"),
        "urban_classification": policy.get("urban_classification"),
        "forbidden_geometry": list(policy.get("forbidden_geometry")
                                   or RID_FORBIDDEN_GEOMETRY),
        "missing_device_evidence": list(policy.get("missing_device_evidence") or []),
    }
    # **未评估项必须披露**。真实缺陷（本轮实测）：当设备没有显式声明 RID 类型时，
    # ``service_contract_for`` 的 service_key 会回落到 legacy ``S:surveillance``，
    # 而冻结策略表里**没有**这个 key ⇒ ``policy`` 为空 ⇒ ``not_evaluated`` 变成空数组，
    # 弹窗显示成"未评估 —"。那是**漏披露**，等于默认宣称"没有未评估因素"。
    # 本端点声明的服务就是 RID，因此回落到冻结的 :data:`RID_NOT_EVALUATED` 是正确
    # 且唯一的兜底（不是猜测，也不是新造指标）。
    declared_not_evaluated = list(policy.get("not_evaluated") or [])
    base["not_evaluated"] = declared_not_evaluated or list(RID_NOT_EVALUATED)
    base["not_evaluated_source"] = (
        "service_contract_policy" if declared_not_evaluated
        else "frozen_contract_fallback_device_service_key_is_legacy")
    base["not_evaluated_semantics"] = (
        "以下因素在几何规划模型中**未评估**；not_evaluated ≠ 通过，"
        "不得据此判定实际接收能力"
    )
    base["limitations"] = [
        "合作监视**工程几何**：不保证实测接收能力",
        "未评估地形遮挡、建筑遮挡、RF 传播、接收机灵敏度、报文冲突与干扰",
        "海侧 5 km 是**规划半径**，不是实际覆盖边界",
    ]
    base["surface_class_facts_fingerprint"] = surface_class_facts_fingerprint

    # 1) 未确认安装高度 ⇒ 不生成确定几何（连切片也不生成）。
    if unit.get("origin_status") != "confirmed":
        reason = _blocked_reason(unit)
        base["geometry"] = {
            "status": "blocked",
            "reason": reason,
            "required_evidence": [
                "site.vertical_profile.service_origin_egm2008_m（已确认）",
            ],
            "geojson": None,
            "surface_parts": [],
            "radial_profile": [],
            "unknown_is_fail_closed": True,
        }
        base["vertical"] = {
            "status": "blocked", "reason": reason,
            "slice_egm2008_m": None,
            "vertical_effective_range": "not_modelled",
            "extrusion_forbidden": True,
        }
        return base

    longitude, latitude = _finite(unit.get("longitude")), _finite(unit.get("latitude"))
    if longitude is None or latitude is None:
        base["geometry"] = {
            "status": "blocked", "reason": "site_coordinate_missing",
            "geojson": None, "surface_parts": [], "radial_profile": [],
        }
        base["vertical"] = {"status": "blocked", "reason": "site_coordinate_missing",
                            "slice_egm2008_m": None}
        return base
    if classifier is None:
        base["geometry"] = {
            "status": "blocked", "reason": "land_mask_classifier_unavailable",
            "note": "未配置陆域掩膜 ⇒ 无法做陆海分区，绝不猜测海陆",
            "geojson": None, "surface_parts": [], "radial_profile": [],
        }
        base["vertical"] = {"status": "blocked",
                            "reason": "land_mask_classifier_unavailable",
                            "slice_egm2008_m": None}
        return base

    to_metric, to_wgs84 = metric_transformer(metric_crs)
    if to_metric is None or to_wgs84 is None:
        base["geometry"] = {
            "status": "blocked", "reason": "metric_transform_unavailable",
            "detail": {"metric_crs": metric_crs},
            "geojson": None, "surface_parts": [], "radial_profile": [],
        }
        base["vertical"] = {"status": "blocked",
                            "reason": "metric_transform_unavailable",
                            "slice_egm2008_m": None}
        return base
    center = to_metric(longitude, latitude)
    if center is None:
        base["geometry"] = {
            "status": "blocked", "reason": "site_coordinate_transform_failed",
            "geojson": None, "surface_parts": [], "radial_profile": [],
        }
        base["vertical"] = {"status": "blocked",
                            "reason": "site_coordinate_transform_failed",
                            "slice_egm2008_m": None}
        return base

    # 2) 逐方位角径向行走：**边界半径完全由分类结果决定**。
    radial_profile = []
    land_ring = []
    outer_ring = []
    class_counts = {"land": 0, "coastal_uncertain": 0, "sea": 0, "unknown": 0}
    transitions_total = 0
    blocked_azimuths = 0
    unknown_azimuths = 0
    covered_radii = []
    land_radii = []

    for index in range(azimuth_segments):
        theta = 2.0 * math.pi * index / float(azimuth_segments)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # 先一次性构造该方位的全部候选点（米制），再**批量**交给权威分类器。
        offsets = [step * (k + 1) for k in range(samples)]
        metric_points = [
            (center[0] + offset * cos_t, center[1] + offset * sin_t)
            for offset in offsets
        ]
        geographic = [to_wgs84(x, y) for x, y in metric_points]
        if any(point is None for point in geographic):
            base["geometry"] = {
                "status": "blocked", "reason": "radial_sampling_projection_failed",
                "geojson": None, "surface_parts": [], "radial_profile": [],
            }
            base["vertical"] = {"status": "blocked",
                                "reason": "radial_sampling_projection_failed",
                                "slice_egm2008_m": None,
                                "vertical_effective_range": "not_modelled",
                                "extrusion_forbidden": True}
            return base
        classes = _classify_points(classifier, geographic)

        covered_radius = 0.0
        land_prefix = 0.0
        prefix_is_land = True
        transitions = 0
        previous = None
        stopped_reason = "sea_radius_reached"
        last_allowed = None
        first_blocked = None
        crossing_low = None
        crossing_high = None
        for offset, surface in zip(offsets, classes):
            surface = str(surface or "unknown")
            if surface not in class_counts:
                surface = "unknown"
            class_counts[surface] += 1
            if previous is not None and surface != previous:
                transitions += 1
                if (prefix_is_land and previous in ("land", "coastal_uncertain")
                        and surface == "sea"):
                    # 陆 → 海的海岸线穿越区间：二分细化后即为真实海岸线距离。
                    crossing_low, crossing_high = previous_offset, offset
            previous_offset = offset
            previous = surface
            if surface == "unknown":
                # fail-closed：unknown **不**按陆地或海上处理，且立即终止该方位的延伸。
                stopped_reason = "unknown_surface_class_fail_closed"
                unknown_azimuths += 1
                first_blocked = offset
                break
            allowed = (
                (surface in ("land", "coastal_uncertain") and offset <= land_radius)
                or (surface == "sea" and offset <= sea_radius)
            )
            if not allowed:
                stopped_reason = f"surface_{surface}_beyond_its_policy_radius"
                first_blocked = offset
                break
            covered_radius = offset
            last_allowed = offset
            if prefix_is_land:
                if surface in ("land", "coastal_uncertain"):
                    land_prefix = offset
                else:
                    prefix_is_land = False

        # 二分细化：把"最后一个允许的采样点 / 第一个被拒的采样点"之间的边界
        # 收敛到 step/2^k 精度。**没有这一步，轮廓半径会被采样步长量化成少数几个
        # 离散值**，肉眼与回归都无法区分"海岸线驱动的轮廓"与"两个同心圆"。
        if last_allowed is not None and first_blocked is not None:
            covered_radius = _refine_radius(
                classifier, to_wgs84, center, cos_t, sin_t,
                low=last_allowed, high=first_blocked,
                accept=_accepts_at(land_radius, sea_radius))
        if crossing_low is not None and crossing_high is not None:
            land_prefix = _refine_radius(
                classifier, to_wgs84, center, cos_t, sin_t,
                low=crossing_low, high=crossing_high,
                accept=lambda surface, offset: (
                    str(surface or "unknown") in ("land", "coastal_uncertain")))

        transitions_total += transitions
        if covered_radius <= 0.0:
            blocked_azimuths += 1
        covered_radii.append(covered_radius)
        land_radii.append(land_prefix)

        outer_point = to_wgs84(center[0] + covered_radius * cos_t,
                               center[1] + covered_radius * sin_t)
        inner_point = to_wgs84(center[0] + land_prefix * cos_t,
                               center[1] + land_prefix * sin_t)
        if outer_point is None or inner_point is None:
            base["geometry"] = {
                "status": "blocked", "reason": "radial_ring_projection_failed",
                "geojson": None, "surface_parts": [], "radial_profile": [],
            }
            base["vertical"] = {"status": "blocked",
                                "reason": "radial_ring_projection_failed",
                                "slice_egm2008_m": None}
            return base
        outer_ring.append([round(outer_point[0], 9), round(outer_point[1], 9)])
        land_ring.append([round(inner_point[0], 9), round(inner_point[1], 9)])
        radial_profile.append({
            "bearing_deg": round(math.degrees(theta) % 360.0, 3),
            "boundary_radius_m": round(covered_radius, 3),
            "land_prefix_radius_m": round(land_prefix, 3),
            "class_transitions": transitions,
            "stopped_reason": stopped_reason,
            #: **游程编码**的类别序列：``[[surface_class, count], ...]``。
            #: 逐点数组在 200 采样 × 72 方位角下会让响应体膨胀两个数量级，
            #: 而游程编码保留了"哪些地表、各延伸多远"的全部可回归信息。
            "class_runs": _class_runs(classes),
        })

    if not outer_ring or all(radius <= 0.0 for radius in covered_radii):
        base["geometry"] = {
            "status": "blocked",
            "reason": "no_classified_surface_within_policy_radius",
            "note": "所有方位角都被 unknown 或超半径终止 ⇒ 不产生任何几何",
            "geojson": None, "surface_parts": [],
            "radial_profile": radial_profile,
            "classification_counts": class_counts,
            "unknown_azimuth_count": unknown_azimuths,
            "blocked_azimuth_count": blocked_azimuths,
            "unknown_is_fail_closed": True,
        }
        base["vertical"] = {
            "status": "blocked",
            "reason": "no_classified_surface_within_policy_radius",
            "slice_egm2008_m": None,
            "vertical_effective_range": "not_modelled",
            "extrusion_forbidden": True,
        }
        base["classification_counts"] = class_counts
        return base

    # 3) 组装 GeoJSON。
    #
    # **OGC 语义要点**：陆侧区域与海侧区域**共享海岸线边**（它们在拓扑上相邻，
    # 交集是一条线而不是面）。把两个相邻多边形放进同一个 MultiPolygon 会构成
    # "内部相接"的非法几何（shapely 实测 Self-intersection），因此：
    #
    #   * ``geojson``        = **整体 footprint** 的 MultiPolygon（各连通部分的并集，
    #                          保证 ``is_valid``）—— 这是前端渲染用的几何；
    #   * ``surface_parts``  = **每个 surface_class 分区各自的合法几何**（各自独立
    #                          GeoJSON，可单独着色/统计），并显式声明它们与整体
    #                          的关系是"按海岸线切分"，不是两个同心圆。
    #
    # 几何在 **WGS84 坐标**上做并集/差集；面积数值只有相对意义（度²），
    # **不**用于任何距离判定 —— 所有半径判定都已在米制帧内完成。
    try:
        from shapely.geometry import MultiPolygon, Polygon, mapping
        from shapely.ops import unary_union

        outer_ring.append(list(outer_ring[0]))
        land_ring.append(list(land_ring[0]))
        outer_polygon = Polygon(outer_ring)
        land_polygon = Polygon(land_ring)
        # 陆侧多边形只在"每个方位角都有正的前缀半径"时才是一个合法的闭合区域；
        # 否则它包含站心重复点、会退化甚至自相交 —— 此时**不输出**该分区，
        # 并如实说明，绝不用一个伪造的圆去补位。
        land_valid = (
            all(radius > 0.0 for radius in land_radii)
            and land_polygon.is_valid
            and not land_polygon.is_empty
            and land_polygon.area > 0.0
        )
        surface_parts = []
        if land_valid:
            surface_parts.append({
                "surface_class": "land_or_coastal_uncertain",
                "policy_radius_m": land_radius,
                "part_role": "land_and_coastal_uncertain_policy_area",
                "geometry": land_polygon,
            })
        sea_geometry = outer_polygon.difference(land_polygon) if land_valid else outer_polygon
        if not sea_geometry.is_empty:
            surface_parts.append({
                "surface_class": "sea",
                "policy_radius_m": sea_radius,
                "part_role": (
                    "sea_policy_area_outside_land_policy_area" if land_valid
                    else "sea_policy_area_full_extent"
                ),
                "geometry": sea_geometry,
            })
        if not surface_parts:
            base["geometry"] = {
                "status": "blocked", "reason": "footprint_geometry_empty",
                "geojson": None, "surface_parts": [], "radial_profile": radial_profile,
                "classification_counts": class_counts,
                "unknown_azimuth_count": unknown_azimuths,
                "blocked_azimuth_count": blocked_azimuths,
                "unknown_is_fail_closed": True,
            }
            base["vertical"] = {"status": "blocked", "reason": "footprint_geometry_empty",
                                "slice_egm2008_m": None,
                                "vertical_effective_range": "not_modelled",
                                "extrusion_forbidden": True}
            base["classification_counts"] = class_counts
            return base

        # 整体 footprint：所有分区的并集，按连通分量拆成 MultiPolygon 的成员。
        merged = unary_union([part["geometry"] for part in surface_parts])
        polygons = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
        polygons = [item for item in polygons if not item.is_empty]
        if not polygons:
            raise ValueError("footprint_union_empty")
        overall = (polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
        geojson = json.loads(json.dumps(mapping(overall)))
        if geojson["type"] == "Polygon":
            geojson = {"type": "MultiPolygon", "coordinates": [geojson["coordinates"]]}
        validity = _validate_geojson(geojson)
        for part in surface_parts:
            geometry = part.pop("geometry")
            part["area_deg2"] = round(float(geometry.area), 12)
            part["is_valid"] = bool(geometry.is_valid)
            part["geojson"] = json.loads(json.dumps(mapping(geometry)))
            if part["geojson"]["type"] == "Polygon":
                part["geojson"] = {"type": "MultiPolygon",
                                   "coordinates": [part["geojson"]["coordinates"]]}
        surface_parts_note = (
            "各 surface_class 分区**共享海岸线边**（拓扑相邻）。OGC 语义下相邻多边形"
            "不能同处一个 MultiPolygon，因此整体 footprint 用并集（geojson），"
            "分区几何在 surface_parts[].geojson 中各自独立给出"
        )
    except Exception as exc:  # 几何构造失败必须如实上报，绝不回落成同心圆
        base["geometry"] = {
            "status": "blocked",
            "reason": f"footprint_geometry_failed:{type(exc).__name__}",
            "geojson": None, "surface_parts": [], "radial_profile": radial_profile,
            "classification_counts": class_counts,
            "unknown_azimuth_count": unknown_azimuths,
            "blocked_azimuth_count": blocked_azimuths,
            "unknown_is_fail_closed": True,
        }
        base["vertical"] = {
            "status": "blocked",
            "reason": f"footprint_geometry_failed:{type(exc).__name__}",
            "slice_egm2008_m": None,
            "vertical_effective_range": "not_modelled",
            "extrusion_forbidden": True,
        }
        base["classification_counts"] = class_counts
        return base

    positive = [radius for radius in covered_radii if radius > 0.0]
    land_positive = [radius for radius in land_radii if radius > 0.0]
    base["geometry"] = {
        "status": "available",
        "reason": None,
        "geometry_method": (
            "per_azimuth_radial_walk_with_authoritative_land_mask_classification"
        ),
        "azimuth_segments": azimuth_segments,
        "radial_step_m": step,
        "metric_crs": metric_crs,
        "crs": "OGC:CRS84",
        "geojson": geojson,
        "geojson_semantics": "整体 footprint（各 surface_class 分区按海岸线切分后的并集）",
        "surface_parts": surface_parts,
        "surface_parts_semantics": surface_parts_note,
        "validity": validity,
        "radial_profile": radial_profile,
        "boundary_radius_m": {
            "min": round(min(positive), 3) if positive else None,
            "max": round(max(positive), 3) if positive else None,
            "mean": round(sum(positive) / len(positive), 3) if positive else None,
            "stddev": round(_stddev(positive), 3) if len(positive) > 1 else None,
            "distinct_values": len({round(radius, 1) for radius in positive}),
        },
        "land_prefix_radius_m": {
            "min": round(min(land_positive), 3) if land_positive else None,
            "max": round(max(land_positive), 3) if land_positive else None,
        },
        #: 非正圆判据：半径随方位角变化，且同时出现陆侧与海侧量级。
        "non_circular_evidence": {
            "radius_varies_with_azimuth": len({round(r, 1) for r in positive}) > 1,
            "both_land_and_sea_radii_present": bool(
                land_positive and any(
                    radius > land_radius + 1.0 for radius in positive)),
            "class_transitions_total": transitions_total,
            "note": (
                "轮廓半径逐方位角由权威陆海分类结果决定；"
                "两个同心圆**不可能**产生随方位角变化的半径集合"
            ),
        },
        "classification_counts": class_counts,
        "blocked_azimuth_count": blocked_azimuths,
        "unknown_azimuth_count": unknown_azimuths,
        "unknown_is_fail_closed": True,
        "two_dimensional_only": True,
    }
    base["vertical"] = {
        "status": "slice_only",
        "slice_egm2008_m": unit.get("service_origin_egm2008_m"),
        "vertical_effective_range": "not_modelled",
        "reason": "vertical_effective_range_not_modelled",
        "note": (
            "RID 没有已验证的垂直有效范围 ⇒ 只展示**有依据的高度切片**"
            "（服务原点 EGM2008 正高处的水平 footprint）；"
            "**绝不**把二维轮廓拉伸成确定高度的完整覆盖体"
        ),
        "extrusion_forbidden": True,
    }
    return base


def _stddev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _validate_geojson(geojson):
    """用 shapely 校验 GeoJSON 合法性（只读；不修改几何）。"""

    try:
        from shapely.geometry import shape
        from shapely.validation import explain_validity

        geometry = shape(geojson)
        return {
            "checked_with": "shapely.geometry.shape",
            "is_valid": bool(geometry.is_valid),
            "geom_type": geometry.geom_type,
            "area_m2_degrees": float(geometry.area),
            "reason": None if geometry.is_valid else explain_validity(geometry),
            "part_count": len(geometry.geoms) if hasattr(geometry, "geoms") else 1,
        }
    except Exception as exc:  # 校验失败必须如实上报，绝不静默通过
        return {
            "checked_with": "shapely.geometry.shape",
            "is_valid": None,
            "reason": f"validation_unavailable:{type(exc).__name__}",
        }


def build_rid_footprints(
        units, classifier, *, metric_crs="EPSG:32651", land_radius_m=2000.0,
        sea_radius_m=5000.0, azimuth_segments=DEFAULT_AZIMUTH_SEGMENTS,
        radial_step_m=DEFAULT_RADIAL_STEP_M, surface_class_facts_fingerprint=None,
        route_intersection=None):
    """构造全部 RID 站点的 footprint（有界：最多 :data:`MAX_SITES` 个）。"""

    selected = list(units or [])[:MAX_SITES]
    footprints = [
        build_rid_footprint(
            unit, classifier, metric_crs=metric_crs, land_radius_m=land_radius_m,
            sea_radius_m=sea_radius_m, azimuth_segments=azimuth_segments,
            radial_step_m=radial_step_m,
            surface_class_facts_fingerprint=surface_class_facts_fingerprint)
        for unit in selected
    ]
    available = [item for item in footprints if item["geometry"]["status"] == "available"]
    if route_intersection is not None:
        for item in footprints:
            item["route_intersection"] = route_intersection
    return {
        "footprints": footprints,
        "footprint_count": len(footprints),
        "available_count": len(available),
        "blocked_count": len(footprints) - len(available),
        "truncated": len(list(units or [])) > MAX_SITES,
    }


__all__ = [
    "DEFAULT_AZIMUTH_SEGMENTS", "DEFAULT_RADIAL_STEP_M", "FOOTPRINT_SEMANTICS", "MAX_SITES",
    "build_rid_footprint", "build_rid_footprints",
]
