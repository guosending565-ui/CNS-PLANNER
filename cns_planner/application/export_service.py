"""Read-only legacy and confirmed-plan delivery exports."""

from copy import deepcopy
from hashlib import sha256
import json

from ..domain.reporting import sanitize_report_value


class ExportService:
    def __init__(self, session, snapshot):
        self.session, self.snapshot = session, snapshot

    def project(self):
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    def routes(self):
        features = [{
            "type": "Feature",
            "properties": {key: route.get(key) for key in ("route_id", "status", "reason", "algorithm_id", "algorithm_version")},
            "geometry": {"type": "LineString", "coordinates": route.get("path", [])},
        } for route in self.session.state["operational_routes"]]
        return self._collection(features)

    def sites(self):
        features = []
        for subsystem, layer in (self.session.state.get("coverage") or {}).get("layers", {}).items():
            for station in layer.get("stations", []):
                properties = {key: value for key, value in station.items() if key != "coordinate"}
                properties["subsystem"] = subsystem
                features.append({"type": "Feature", "properties": properties, "geometry": {"type": "Point", "coordinates": station["coordinate"]}})
        return self._collection(features)

    def confirmed_facilities(self):
        """Final/proposed P18 facilities; deliberately ignores legacy coverage stations."""
        state = self.session.state
        plan = state.get("confirmed_cns_plan") or {}
        facilities = deepcopy((state.get("existing_cns_facilities") or {}).get("items") or [])
        if plan.get("status") == "confirmed":
            for action in plan.get("confirmed_actions") or []:
                facility_id = action.get("facility_id")
                facility = next((item for item in facilities if str(item.get("facility_id")) == str(facility_id)), None) if facility_id else None
                if facility is None:
                    coordinate = deepcopy(action.get("coordinate"))
                    if not isinstance(coordinate, list) or len(coordinate) < 2:
                        continue
                    identity = sha256(f"{action.get('reuse_class')}|{action.get('site_id')}".encode()).hexdigest()[:16]
                    facility = {
                        "facility_id": f"confirmed-proposal:{identity}", "site_id": action.get("site_id"),
                        "name": f"已确认待应用 {action.get('site_id')}", "coordinate": coordinate,
                        "vertical_profile": deepcopy(action.get("vertical") or {}), "devices": [],
                        "status": "confirmed_not_applied", "source": "P18 confirmed plan",
                    }
                    facilities.append(facility)
                if not any(str(item.get("device_id")) == str(action.get("device_id")) for item in facility.get("devices") or []):
                    facility.setdefault("devices", []).append({
                        "device_id": action.get("device_id"), "subsystem": action.get("subsystem"),
                        "status": "confirmed_not_applied", "source_action_id": action.get("action_id"),
                    })
        features = []
        for facility in facilities:
            coordinate = facility.get("coordinate")
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                continue
            properties = sanitize_report_value({key: deepcopy(value) for key, value in facility.items() if key != "coordinate"})
            properties["plan_id"] = plan.get("plan_id")
            properties["plan_status"] = plan.get("status")
            features.append({"type": "Feature", "properties": properties,
                             "geometry": {"type": "Point", "coordinates": coordinate}})
        return self._collection(features)

    def algorithms(self, catalog):
        return json.dumps(sanitize_report_value({
            "selection": deepcopy(self.session.state.get("algorithm_selection") or {}),
            "manifests": deepcopy(catalog or []),
        }), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")

    @staticmethod
    def _collection(features):
        return json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2).encode("utf-8")
