"""CNS catalog, requirement, existing-facility and candidate-site use cases."""

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog, DeviceCatalog
from ..domain.cns_inputs import normalize_required_cns


def empty_collection(collection_id):
    return {
        "status": "not_calculated", "collection_id": collection_id,
        "source": None, "metadata": {}, "count": 0, "items": [],
    }


class CNSInputService:
    def __init__(self, session, invalidation, adapter, snapshot):
        self.session, self.invalidation = session, invalidation
        self.adapter, self.snapshot = adapter, snapshot

    def ensure_catalogs(self):
        state, config_dir = self.session.state, self.session.defaults_path.parent
        if state.get("aircraft_profiles", {}).get("status") != "passed":
            try:
                state["aircraft_profiles"] = AircraftCNSProfileCatalog.load(config_dir)
            except OSError:
                state["aircraft_profiles"] = AircraftCNSProfileCatalog.from_defaults(self.session.defaults.get("aircraft_library"))
        if state.get("device_catalog", {}).get("status") != "passed":
            try:
                state["device_catalog"] = DeviceCatalog.load(config_dir)
            except OSError:
                state["device_catalog"] = DeviceCatalog.from_defaults(self.session.defaults.get("device_library"))
        state.setdefault("selected_aircraft_profile_id", None)
        state["required_cns"] = normalize_required_cns(state.get("required_cns"))
        state.setdefault("existing_cns_facilities", empty_collection("existing-cns-facilities"))
        state.setdefault("candidate_sites", empty_collection("candidate-sites"))

    def select_aircraft(self, aircraft_id):
        profile = AircraftCNSProfileCatalog.find(self.session.state["aircraft_profiles"], str(aircraft_id or ""))
        if profile is None:
            raise ValueError("飞行器能力档案不存在")
        self.session.state["selected_aircraft_profile_id"] = profile["aircraft_id"]
        self.invalidation.workflow("aircraft_profile")
        return self._save()

    def import_aircraft_catalog(self, path):
        catalog = AircraftCNSProfileCatalog.load_path(path)
        self.session.state["aircraft_profiles"] = catalog
        selected = self.session.state.get("selected_aircraft_profile_id")
        if selected and AircraftCNSProfileCatalog.find(catalog, selected) is None:
            self.session.state["selected_aircraft_profile_id"] = None
        self.invalidation.workflow("aircraft_profile")
        return self._save()

    def import_device_catalog(self, path):
        catalog = DeviceCatalog.load_path(path)
        state = self.session.state
        state["device_catalog"] = catalog
        state["devices"] = DeviceCatalog.to_coverage_v1(catalog)
        self.invalidation.workflow("devices")
        return self._save()

    def set_required_cns(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("RequiredCNS 请求必须是对象")
        state = self.session.state
        current = deepcopy(state.get("required_cns"))
        scope, requirements = payload.get("scope", "project"), payload.get("requirements")
        if not isinstance(requirements, dict):
            raise ValueError("RequiredCNS requirements 必须是对象")
        if scope == "project":
            current["project_default"] = requirements
        elif scope == "route":
            route_id = str(payload.get("route_id") or "")
            if route_id not in {item.get("route_id") for item in state.get("scenario_routes", [])}:
                raise ValueError("RequiredCNS 航路不存在")
            current.setdefault("route_overrides", {})[route_id] = requirements
        else:
            raise ValueError("RequiredCNS scope 必须是 project 或 route")
        current["source"] = str(payload.get("source") or "用户配置")
        state["required_cns"] = normalize_required_cns(current)
        self.invalidation.workflow("required_cns")
        return self._save()

    def import_existing(self, payload):
        self.session.state["existing_cns_facilities"] = self.adapter.load_existing(payload)
        self.invalidation.workflow("existing_cns")
        return self._save()

    def import_candidates(self, payload):
        self.session.state["candidate_sites"] = self.adapter.load_candidates(payload)
        self.invalidation.workflow("candidate_sites")
        return self._save()

    def candidates_from_existing(self):
        facilities = self.session.state["existing_cns_facilities"].get("items", [])
        items = [{
            "site_id": item["site_id"], "name": item["name"], "coordinate": item["coordinate"],
            "elevation_m": item.get("elevation_m"), "site_type": "existing_facility",
            "available_subsystems": list(dict.fromkeys(device["subsystem"] for device in item.get("devices", []))),
            "usable": item.get("status") == "active", "locked": True,
            "source": "已有 CNS 设施", "metadata": {"facility_id": item["facility_id"]},
        } for item in facilities]
        self.session.state["candidate_sites"] = self.adapter.load_candidates({"items": items, "metadata": {"derived_from": "existing_cns_facilities"}})
        self.invalidation.workflow("candidate_sites")
        return self._save()

    def _save(self):
        self.session.save()
        return self.snapshot()
