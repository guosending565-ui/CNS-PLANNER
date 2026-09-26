"""C/N/S device configuration and coverage-planning use cases."""


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
