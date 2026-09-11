"""C/N/S device configuration and coverage-planning use cases."""

from ..catalogs import DeviceCatalog


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
        self.invalidation.workflow("devices")
        self.session.save()
        return self.snapshot()

    def plan_coverage(self):
        state = self.session.state
        if not state["rules"] or state["rules"]["status"] != "passed":
            raise ValueError("请先保存并通过运行规则校验")
        if state["result_statuses"].get("routes") != "passed":
            raise ValueError("运行航路已失效，请先重新生成运行航路")
        if not state["operational_routes"] or any(item.get("status") != "passed" for item in state["operational_routes"]):
            raise ValueError("没有可用于布站的有效运行航路")
        devices = DeviceCatalog.to_coverage_v1(state.get("device_catalog") or {}, state["devices"])
        state["coverage"] = self.planner.plan(state["operational_routes"], devices)
        state["result_statuses"]["coverage"] = state["coverage"]["status"]
        state["result_statuses"]["report"] = "not_calculated"
        self.session.save()
        return self.snapshot()
