"""固定 1000 m CNS 保障宽带的**图件侧构造**（Round38-FIG）。

职责边界（与 Round38-W1A / N2 / FC30 契约严格一致）：

* 几何**只**由 :func:`cns_planner.gis.corridor_geometry.build_fixed_corridor_geometry`
  构造 —— 本模块绝不重写第二套 buffer，绝不对经纬度加减数值近似宽度；
* currentness 与身份判定**只**由 :func:`cns_planner.gis.figure_currentness.
  fixed_corridor_currentness` 给出（与 ``GET /api/fixed-corridor-geometry`` 同一份）；
* 只读：不写 ``ProjectState``、不 ``save()``、不推进 revision、不触发失效传播。

语义（必须在图面上被如实表达）：

* 这是**中心线左右各 500 m 的 CNS 服务保障目标带**；
* 它**不是**任何设备已实现的覆盖区域，**不是**实测无线传播等值线，
  **不是**认证导航覆盖区，也**不构成**法规批准的运行空间；
* 它与导航中心线监测关联范围（2000 m）是两件事，互不替代。

画不画、画在哪一层由模板与样式决定：
:data:`cns_planner.gis.figure_style.FIGURE_STYLES` 里 ``cns_fixed_corridor`` 用浅色
半透明填充 + 虚线边界、``z=3``（在障碍物与航路线 / 站址之下），因此不会遮挡航路线、
站址图标与覆盖结果。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.cns_corridor import (
    CORRIDOR_HALF_WIDTH_M,
    CORRIDOR_TOTAL_WIDTH_M,
    GEOMETRY_ALGORITHM_ID,
    GEOMETRY_ALGORITHM_VERSION,
    WIDTH_POLICY_VERSION,
    WIDTH_SEMANTICS,
)
from .corridor_geometry import build_fixed_corridor_geometry
from .figure_currentness import (
    CURRENT,
    STALE,
    UNKNOWN,
    geojson_outer_rings,
    layer_currentness_entry,
    normalize_status,
)

#: 图件侧图层键（与模板目录 :data:`~cns_planner.reporting.map_templates.
#: FIXED_CORRIDOR_LAYER_KEY` 同值；此处独立持有，避免 GIS 层 import reporting 层）。
FIXED_CORRIDOR_LAYER_KEY = "cns_fixed_corridor"

#: 该图层的来源角色标识（不是本机数据源文件角色，绝不参与文件存在性判据）。
FIXED_CORRIDOR_SOURCE_ROLE = "fixed_corridor_geometry"

#: 该几何的语义标识：只回答"服务需要保障哪条带"，不回答"是否已具备能力"。
FIXED_CORRIDOR_GEOMETRY_SEMANTICS = (
    "cns_service_target_band_engineering_reference_not_measured_coverage_"
    "not_certified_airspace"
)

#: 固定宽带与导航中心线关联范围的分离声明（图面披露必须逐字包含它）。
WIDTH_VS_ASSOCIATION_DISCLAIMER = (
    "固定 1000 m CNS 保障宽带（中心线左右各 500 m）是服务保障目标带；"
    "它与导航中心线监测关联范围（2000 m）是两件不同的事，互不替代。"
    "该几何不是认证导航覆盖区，也不构成任何法规批准空间。"
)


def _clean_path(path):
    cleaned = []
    for point in path or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                cleaned.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
    return cleaned


def build_fixed_corridor_band(route, *, projected=None, entry=None,
                              corridor_effective_status=None, route_authority=None,
                              path=None):
    """构造固定 1000 m 保障目标带的图件图层数据 + 身份 + currentness。

    返回一个**纯字典**（JSON-safe），调用方（Application 装配层）据此构造 ``LayerSpec``：

    ``{"layer_key", "status", "reason", "currentness", "currentness_entry",
       "data", "detail", "feature_count", "source_role"}``

    只有 ``currentness == "current"`` 且几何真的构造成功时 ``data`` 才有 polygons；
    其余情况一律 ``data`` 为空且 ``status="unavailable"``，由调用方如实登记为
    **未绘制**（绝不画近似带、绝不画空图冒充成功）。
    """

    route = route if isinstance(route, dict) else {}
    route_id = _text(route.get("route_id"))
    cleaned = _clean_path(path if path is not None else route.get("path"))
    authority = route_authority if route_authority is not None else _route_authority(route)
    built = build_fixed_corridor_geometry(
        cleaned,
        route_id=route_id,
        route_authority=authority,
        source_crs=str(route.get("path_crs") or "OGC:CRS84"),
        construction_crs=route.get("construction_crs") or route.get("metric_crs"),
    )
    fingerprint = built.get("geometry_fingerprint")
    currentness, currentness_reason, currentness_identity = _figure_currentness(
        built=built,
        projected=projected,
        entry=entry,
        fingerprint=fingerprint,
        route_status=route.get("status"),
        corridor_effective_status=corridor_effective_status,
    )
    rings, holes = geojson_outer_rings(built.get("geometry"))
    drawable = (
        currentness == CURRENT
        and built.get("status") == "available"
        and bool(rings)
    )
    detail = {
        "width_policy_version": WIDTH_POLICY_VERSION,
        "corridor_half_width_m": built.get("corridor_half_width_m", CORRIDOR_HALF_WIDTH_M),
        "corridor_total_width_m": built.get(
            "corridor_total_width_m", CORRIDOR_TOTAL_WIDTH_M,
        ),
        "width_semantics": built.get("width_semantics", WIDTH_SEMANTICS),
        "geometry_algorithm_id": built.get("geometry_algorithm_id", GEOMETRY_ALGORITHM_ID),
        "geometry_algorithm_version": built.get(
            "geometry_algorithm_version", GEOMETRY_ALGORITHM_VERSION,
        ),
        "geometry_fingerprint": fingerprint,
        "geometry_status": built.get("status"),
        "geometry_reason": built.get("reason"),
        "construction_crs": built.get("construction_crs"),
        "projected_area_m2": built.get("projected_area_m2"),
        "projected_to_geodesic_length_ratio": built.get(
            "projected_to_geodesic_length_ratio",
        ),
        "boundary_ring_count": built.get("boundary_ring_count"),
        "source_crs": built.get("source_crs"),
        "route_id": route_id,
        "route_authority": deepcopy(authority),
        "boundary_ring_drawn": len(rings),
        "boundary_hole_count": sum(len(value) for value in holes.values()),
        "boundary_rings_discarded": bool(
            built.get("status") == "available" and not rings
        ),
        "semantics": FIXED_CORRIDOR_GEOMETRY_SEMANTICS,
        "not_device_coverage_area": True,
        "not_measured_radio_propagation": True,
        "not_certified_navigation_coverage": True,
        "not_certified_airspace": True,
        "navigation_association_radius_m": 2000.0,
        "navigation_association_is_not_this_band": True,
        "disclaimer": WIDTH_VS_ASSOCIATION_DISCLAIMER,
        "currentness": currentness,
        "currentness_reason": currentness_reason,
        "currentness_identity": deepcopy(currentness_identity),
        "p14_projected_result_present": bool(projected),
        "p14_route_entry_present": entry is not None,
        "drawn_as_current_band": drawable,
        "read_only": True,
        "writes_state": False,
        "recomputes_business_results": False,
        "never_drawn_from_approximate_geometry": True,
    }
    layer = {
        "layer_key": FIXED_CORRIDOR_LAYER_KEY,
        "source_role": FIXED_CORRIDOR_SOURCE_ROLE,
        "status": "available" if drawable else "unavailable",
        "reason": "" if drawable else _not_drawable_reason(
            currentness, currentness_reason, built, rings,
        ),
        "currentness": currentness,
        "currentness_entry": layer_currentness_entry(
            currentness=currentness,
            reason=currentness_reason,
            identity=currentness_identity,
            evidence={
                "geometry_fingerprint": fingerprint,
                "geometry_status": built.get("status"),
                "route_operational_status": route.get("status"),
                "p14_route_entry_present": entry is not None,
            },
        ),
        "detail": detail,
        "feature_count": 1 if drawable else 0,
        "data": ({"polygons": rings, "polygon_holes": holes} if drawable else {}),
    }
    return layer


def layer_currentness_record(layer):
    """从一次 :func:`build_fixed_corridor_band` 结果取出 currentness 记录。"""

    return deepcopy((layer or {}).get("currentness_entry") or {})


def _figure_currentness(*, built, projected, entry, fingerprint, route_status,
                        corridor_effective_status):
    """图件侧 currentness：委托 GIS 层唯一判定；几何构造自身失败时 fail-closed。

    为什么在这里还要看一次几何构造结果：图件与接口的差别是**图件必须真的画出来**。
    接口可以在几何 ``blocked`` 时只回报状态；图件若拿不到合法 ring，就绝不能把这一层
    当成"已绘制的 current 保障带"。
    """

    from .figure_currentness import fixed_corridor_currentness

    currentness, reason, identity = fixed_corridor_currentness(
        route_operational_status=route_status,
        projected=projected,
        entry=entry,
        geometry_fingerprint=fingerprint,
        corridor_effective_status=corridor_effective_status,
    )
    geometry_status = _text(built.get("status"))
    if geometry_status and geometry_status != "available":
        geometry_state = normalize_status(geometry_status, default=UNKNOWN)
        return geometry_state, f"geometry_construction_status={geometry_status}", identity
    if currentness == CURRENT and not fingerprint:
        # 判据说 current，但没有任何几何指纹：身份缺失，不得当作 current。
        return UNKNOWN, "fixed_corridor_geometry_fingerprint_absent", identity
    return currentness, reason, identity


def _not_drawable_reason(currentness, reason, built, rings):
    if built.get("status") != "available":
        return (
            "固定 1000 m 保障宽带几何构造未通过："
            f"{built.get('reason') or '未给出原因'}"
        )
    if not rings:
        return "固定 1000 m 保障宽带几何没有可绘制的合法外环"
    if currentness == STALE:
        return (
            "固定 1000 m 保障宽带不是 current（原因："
            f"{reason or 'unknown'}）：不得作为当前有效的保障目标带绘制"
        )
    if currentness == UNKNOWN:
        return (
            "固定 1000 m 保障宽带结果尚未评估或缺少身份证据（原因："
            f"{reason or 'unknown'}）"
        )
    return f"固定 1000 m 保障宽带不满足绘制条件（currentness={currentness}）"


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _route_authority(route):
    """航路权威身份的**唯一口径**，与 ``FixedCorridorGeometryService._route_authority``
    逐字段一致。

    为什么必须一致：``route_authority`` 是固定宽带几何身份的输入之一，两处口径不同会
    直接导致"重建指纹 != P14 条目指纹"，从而把一条本来 current 的带误判为 stale。
    这里同步给出同一份实现，并在 :func:`build_fixed_corridor_band` 的注释里写明该约束。
    """

    authority = (route or {}).get("authority")
    if isinstance(authority, dict):
        return deepcopy(authority)
    provenance = (route or {}).get("provenance")
    if not isinstance(provenance, dict):
        return None
    summary = {
        key: provenance.get(key) for key in (
            "source_type", "candidate_id", "candidate_fingerprint",
            "validation_id", "validation_fingerprint", "adoption_fingerprint",
        )
    }
    return summary if any(value is not None for value in summary.values()) else None


__all__ = [
    "FIXED_CORRIDOR_GEOMETRY_SEMANTICS", "FIXED_CORRIDOR_LAYER_KEY",
    "FIXED_CORRIDOR_SOURCE_ROLE", "WIDTH_VS_ASSOCIATION_DISCLAIMER",
    "build_fixed_corridor_band", "layer_currentness_record",
]
