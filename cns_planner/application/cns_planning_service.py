"""C/N/S device configuration and coverage-planning use cases."""

from ..catalogs import DeviceCatalog
from .production_write_authority import (
    canonical_operational_routes, compatibility_route_status,
    operational_routes_for_legacy_consumers, write_runtime_compatibility_result,
)


class CNSPlanningService:
    def __init__(self, session, planner, invalidation, snapshot):
        self.session, self.planner = session, planner
        self.invalidation, self.snapshot = invalidation, snapshot

    def set_devices(self, devices):
        if not isinstance(devices, list):
            raise ValueError("设备清单格式无效")
        clean = []
        for item in devices:
            if item.get("subsystem") not in ("C", "N", "S") or item.get("role") not in ("primary", "gap"):
                raise ValueError("设备分系统或角色无效")
            radius, mtbf = float(item["radius_m"]), float(item["mtbf"])
            if radius <= 0 or mtbf <= 0:
                raise ValueError("设备覆盖半径和 MTBF 必须大于零")
            clean.append({**item, "radius_m": radius, "mtbf": mtbf, "mtbf_h": mtbf, "enabled": bool(item.get("enabled", True))})
        self.session.state["devices"] = clean
        catalog_items = {item["device_id"]: item for item in self.session.state.get("device_catalog", {}).get("items", [])}
        for device in clean:
            catalog = catalog_items.get(device.get("device_id"))
            if catalog:
                catalog.update({"radius_m": device["radius_m"], "mtbf_h": device["mtbf_h"], "enabled": device["enabled"]})
                geometry = catalog.get("coverage_geometry") or {}
                if geometry.get("source") == "legacy_engineering_assumption":
                    geometry["slant_range_m"] = device["radius_m"]
        self.invalidation.workflow("devices")
        self.session.save()
        return self.snapshot()

    def plan_coverage(self):
        state = self.session.state
        if not state["rules"] or state["rules"]["status"] != "passed":
            raise ValueError("请先保存并通过运行规则校验")
        # 旧二维 coverage 是 legacy/compatibility 链：canonical 正式航路优先，没有正式
        # 航路时才回退到当前会话的 runtime-only 旧算法兼容试算。canonical 结果永不被
        # 兼容试算改写。
        routes = operational_routes_for_legacy_consumers(state, self.session)
        if canonical_operational_routes(state):
            if state["result_statuses"].get("routes") != "passed":
                raise ValueError("运行航路已失效，请先重新生成运行航路")
        elif compatibility_route_status(self.session) != "passed":
            raise ValueError("运行航路已失效，请先重新生成运行航路")
        if not routes or any(item.get("status") != "passed" for item in routes):
            raise ValueError("没有可用于布站的有效运行航路")
        devices = DeviceCatalog.to_coverage_v1(state.get("device_catalog") or {}, state["devices"])
        result = self.planner.plan(routes, devices)
        record = write_runtime_compatibility_result(
            self.session, "coverage", result,
            source_algorithm={
                "algorithm_type": "coverage_planner",
                "algorithm_id": getattr(self.planner, "algorithm_id", None),
                "algorithm_version": getattr(self.planner, "algorithm_version", None),
                "class": type(self.planner).__name__,
            },
            note=(
                "旧版二维覆盖试算：不用于正式规划，不写入项目状态，不影响三维覆盖、"
                "服务能力、设施规划或报告。"
            ),
        )
        response = self.snapshot()
        response["compatibility_coverage"] = record
        # 兼容端点响应保留旧字段名，便于旧客户端显示；该值不在 ProjectState，
        # 下一次通用 workflow snapshot 也不会携带这次新试算。
        response["coverage"] = record
        response["compatibility_write"] = True
        response["canonical_coverage_3d_unchanged"] = True
        return response
