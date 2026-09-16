"""Application boundary for reference-only landing sites and equipment facts."""

from copy import deepcopy

from ..reference_data import (
    load_equipment_reference_catalog, load_reference_landing_sites, load_reference_routes,
)


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

    def routes_snapshot(self):
        return deepcopy(self.session.state.get("reference_routes") or {})

    def import_landing_sites(self, path, save=True):
        previous = self.session.state.get("reference_landing_sites") or {}
        result = load_reference_landing_sites(path)
        self._migrate_landing_site_references(previous, result)
        self.session.state["reference_landing_sites"] = result
        if save:
            self.session.save()
        return self.snapshot()

    def _migrate_landing_site_references(self, previous, current):
        old_by_id = {
            item.get("reference_site_id"): item
            for item in previous.get("items") or [] if item.get("reference_site_id")
        }
        new_items = current.get("items") or []
        for node in self.session.state.get("nodes") or []:
            old_id = node.get("reference_site_id")
            old = old_by_id.get(old_id)
            if not old:
                continue
            candidates = [item for item in new_items if self._same_landing_site(old, item)]
            if len(candidates) != 1 or candidates[0].get("reference_site_id") == old_id:
                continue
            new_id = candidates[0]["reference_site_id"]
            node["reference_site_id"] = new_id
            provenance = node.setdefault("provenance", {})
            provenance.setdefault("legacy_reference_site_ids", []).append(old_id)
            provenance["reference_site_id"] = new_id

    @staticmethod
    def _same_landing_site(left, right):
        left_coordinate, right_coordinate = left.get("coordinate"), right.get("coordinate")
        return (
            str(left.get("name") or "").strip().casefold() == str(right.get("name") or "").strip().casefold()
            and left_coordinate == right_coordinate
        )

    def import_routes(self, path, save=True):
        result = load_reference_routes(path)
        self.session.state["reference_routes"] = result
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
