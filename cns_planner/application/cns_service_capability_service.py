"""Application use case for persisted static CNS service capability."""

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog


class CNSServiceCapabilityService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def capability_snapshot(self):
        return deepcopy(self.session.state["cns_service_capability"])

    def evaluate(self):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        for facility in facilities.get("items") or []:
            if isinstance(facility, dict):
                facility.pop("planning_profile", None)
                facility.pop("planning_origin", None)
                for device in facility.get("devices") or []:
                    if isinstance(device, dict):
                        device.pop("planning_origin", None)
        result = self.model.evaluate(
            state.get("coverage_3d") or {}, state.get("required_cns") or {}, profile,
            facilities, state.get("device_catalog") or {},
        )
        state["cns_service_capability"] = result
        self.invalidation.service_timeline()
        state["result_statuses"]["cns_service_capability"] = {
            "meets_under_model": "passed", "does_not_meet_under_model": "failed",
            "unknown": "pending_confirmation", "unsupported_model": "missing_data",
            "not_applicable": "not_applicable",
        }.get(result["status"], result["status"])
        state["result_statuses"]["report"] = "not_calculated"
        self.session.save()
        return self.snapshot()
