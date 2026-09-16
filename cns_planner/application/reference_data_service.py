"""Application boundary for reference-only landing sites and equipment facts."""

from copy import deepcopy

from ..reference_data import load_equipment_reference_catalog, load_reference_landing_sites


class ReferenceDataService:
    def __init__(self, session, route_service, snapshot):
        self.session = session
        self.route_service = route_service
        self.snapshot = snapshot

    def ensure_equipment_catalog(self):
        current = self.session.state.get("equipment_reference_catalog") or {}
        if current.get("status") != "passed":
            self.session.state["equipment_reference_catalog"] = load_equipment_reference_catalog()

    def landing_sites_snapshot(self):
        return deepcopy(self.session.state.get("reference_landing_sites") or {})

    def equipment_catalog_snapshot(self):
        return deepcopy(self.session.state.get("equipment_reference_catalog") or {})

    def import_landing_sites(self, path, save=True):
        result = load_reference_landing_sites(path)
        self.session.state["reference_landing_sites"] = result
        if save:
            self.session.save()
        return self.snapshot()

    def add_landing_site_to_project(self, reference_site_id):
        collection = self.session.state.get("reference_landing_sites") or {}
        site = next(
            (item for item in collection.get("items") or [] if item.get("reference_site_id") == reference_site_id),
            None,
        )
        if not site:
            raise ValueError("参考起降点不存在")
        if site.get("quality") == "invalid" or not site.get("coordinate"):
            raise ValueError("参考起降点坐标无效，不能加入项目")
        return self.route_service.add_reference_site(site)
