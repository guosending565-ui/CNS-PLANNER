"""固定 1000 m CNS 保障宽带的**只读几何投影**（Round38-N2）。

职责边界（与 W1A 契约严格一致）：

* 几何**只**由 :func:`cns_planner.gis.corridor_geometry.build_fixed_corridor_geometry`
  构造 —— 本模块绝不重写第二套 buffer，绝不对经纬度加减数值近似宽度；
* 只读 ``ProjectState``：不 save、不 invalidate、不写任何容器、不注册 writer；
* **身份门禁**：只有当 P14 结果有效（非 stale）**且**其路由条目的固定几何指纹与本次
  重建指纹一致时，``currentness`` 才是 ``current``；否则如实返回 ``stale`` /
  ``unavailable``，调用方**不得**把该几何当作有效保障边界，更不得把它画成
  认证导航覆盖区。
* 本接口表达的是**中心线左右各 500 m 的 CNS 服务保障目标带**，与 N1 的
  2000 m 监测关联范围是两件不同的事，二者在任何输出里都不得互相替代。
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
from ..domain.layered_route_validation import path_fingerprint
from ..gis.corridor_geometry import build_fixed_corridor_geometry
from ..gis.figure_currentness import fixed_corridor_currentness, layer_currentness_entry
from .result_currentness import effective_result_status


#: 固定宽带几何与导航中心线监测关联的**分离声明**：两者永不等价。
WIDTH_VS_ASSOCIATION_DISCLAIMER = (
    "固定 1000 m CNS 保障宽带（中心线左右各 500 m）是服务保障目标带；"
    "它与导航中心线监测关联范围（2000 m）是两件不同的事，互不替代。"
    "该几何不是认证导航覆盖区，也不构成任何法规批准空间。"
)

#: 该几何的语义标识：只回答"服务需要保障哪条带"，不回答"是否已具备能力"。
GEOMETRY_SEMANTICS = "cns_service_target_band_engineering_reference_not_certified_coverage"


class FixedCorridorGeometryService:
    """权威航路 → 固定宽带几何的只读投影（唯一消费者入口）。"""

    def __init__(self, session, corridor_service):
        self.session = session
        self.corridor_service = corridor_service

    # ---- 只读解析 -----------------------------------------------------------

    def _canonical_routes(self):
        return [
            item for item in (self.session.state.get("operational_routes") or [])
            if isinstance(item, dict)
        ]

    def _resolve_route(self, route_id):
        routes = self._canonical_routes()
        matches = [
            item for item in routes
            if not route_id or str(item.get("route_id") or "") == str(route_id)
        ]
        if len(matches) != 1:
            return None, (
                "未指定航路且存在多条正式运行航路" if not route_id
                else "未找到指定的正式运行航路"
            )
        return matches[0], None

    @staticmethod
    def _clean_path(path):
        clean = []
        for point in path or []:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                clean.append([float(point[0]), float(point[1])])
        return clean

    @staticmethod
    def _route_authority(route):
        provenance = route.get("provenance") or {}
        authority = route.get("authority")
        if isinstance(authority, dict):
            return deepcopy(authority)
        if not isinstance(provenance, dict):
            return None
        summary = {
            key: provenance.get(key) for key in (
                "source_type", "candidate_id", "candidate_fingerprint",
                "validation_id", "validation_fingerprint", "adoption_fingerprint",
            )
        }
        return summary if any(value is not None for value in summary.values()) else None

    def _corridor_route_entry(self, route_id):
        """P14 只读投影里该航路的条目（含固定几何身份）；缺失时为 ``None``。"""

        projected = self.corridor_service.result_snapshot() or {}
        for item in projected.get("routes") or []:
            if isinstance(item, dict) and str(item.get("route_id") or "") == str(route_id):
                return projected, item
        return projected, None

    def _currentness(self, route_id, geometry_fingerprint, projected, entry,
                     route_status=None):
        """固定宽带几何的**有效** currentness（Round38-FIG 起委托 GIS 层唯一实现）。

        判定口径完整定义在
        :func:`cns_planner.gis.figure_currentness.fixed_corridor_currentness`：
        图件（FigureSpec）与接口（``GET /api/fixed-corridor-geometry``）因此消费
        **同一份**判据，不可能出现"接口说 current、图上却按 stale 画"的分叉。

        这里只负责补齐 corridor 结果的**有效状态**（算法语义 stale 投影）。
        """

        effective = effective_result_status(self.session.state, "cns_corridor_assessment")
        currentness, reason, identity = fixed_corridor_currentness(
            route_operational_status=route_status,
            projected=projected,
            entry=entry,
            geometry_fingerprint=geometry_fingerprint,
            corridor_effective_status=effective,
        )
        return currentness, reason, identity

    # ---- 只读入口 -----------------------------------------------------------

    def geometry(self, route_id=None):
        """返回固定宽带几何 + 身份 + **有效** currentness（绝不写状态）。"""

        route, error = self._resolve_route(route_id)
        if route is None:
            return {
                "status": "unavailable",
                "reason": error,
                "route_id": None if not route_id else str(route_id),
                "width_policy_version": WIDTH_POLICY_VERSION,
                "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
                "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
                "width_semantics": WIDTH_SEMANTICS,
                "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
                "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
                "geometry": None,
                "geometry_fingerprint": None,
                "currentness": "unavailable",
                "currentness_reason": error,
                "semantics": GEOMETRY_SEMANTICS,
                "disclaimer": WIDTH_VS_ASSOCIATION_DISCLAIMER,
                "writes_performed": 0,
            }

        resolved_route_id = str(route.get("route_id") or "")
        path = self._clean_path(route.get("path"))
        built = build_fixed_corridor_geometry(
            path,
            route_id=resolved_route_id,
            route_authority=self._route_authority(route),
            source_crs=str(route.get("path_crs") or "OGC:CRS84"),
            construction_crs=route.get("construction_crs") or route.get("metric_crs"),
        )
        projected, entry = self._corridor_route_entry(resolved_route_id)
        fingerprint = built.get("geometry_fingerprint")
        currentness, reason, identity = self._currentness(
            resolved_route_id, fingerprint, projected, entry, route.get("status"),
        )
        common = {
            "route_id": resolved_route_id,
            "route_authority": self._route_authority(route),
            "route_geometry_fingerprint": path_fingerprint(path),
            "route_point_count": len(path),
            "route_operational_status": route.get("status"),
            "width_policy_version": WIDTH_POLICY_VERSION,
            "corridor_half_width_m": CORRIDOR_HALF_WIDTH_M,
            "corridor_total_width_m": CORRIDOR_TOTAL_WIDTH_M,
            "width_semantics": WIDTH_SEMANTICS,
            "geometry_algorithm_id": GEOMETRY_ALGORITHM_ID,
            "geometry_algorithm_version": GEOMETRY_ALGORITHM_VERSION,
            "geometry_fingerprint": fingerprint,
            "geometry_identity": identity,
            "currentness": currentness,
            "currentness_reason": reason,
            "currentness_entry": layer_currentness_entry(
                currentness=currentness, reason=reason, identity=identity,
                evidence={
                    "route_geometry_fingerprint": path_fingerprint(path),
                    "route_operational_status": route.get("status"),
                    "source": "fixed_corridor_geometry_service",
                },
            ),
            "semantics": GEOMETRY_SEMANTICS,
            "disclaimer": WIDTH_VS_ASSOCIATION_DISCLAIMER,
            "writes_performed": 0,
        }
        if built.get("status") != "available":
            # 构造被阻断时**不给出任何几何**：调用方必须 fail-closed，
            # 绝不用近似几何继续计算或绘制。
            return {
                **common,
                "status": "blocked",
                "reason": built.get("reason"),
                "construction_crs": None,
                "geometry": None,
                "projected_area_m2": None,
                "projected_to_geodesic_length_ratio": None,
            }
        return {
            **common,
            "status": "available",
            "reason": None,
            "construction_crs": built.get("construction_crs"),
            "metric_crs_source": built.get("metric_crs_source"),
            "route_length_geodesic_m": built.get("route_length_geodesic_m"),
            "projected_area_m2": built.get("projected_area_m2"),
            "projected_to_geodesic_length_ratio": built.get(
                "projected_to_geodesic_length_ratio"
            ),
            "geometry": deepcopy(built.get("geometry")),
        }


__all__ = [
    "GEOMETRY_SEMANTICS", "WIDTH_VS_ASSOCIATION_DISCLAIMER",
    "FixedCorridorGeometryService",
]
