"""Application use case for persisted CNS Gap Analysis."""

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog


class GapAnalysisService:
    def __init__(self, session, analyzer, snapshot):
        self.session, self.analyzer, self.snapshot = session, analyzer, snapshot

    def analyze(self):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {}, state.get("selected_aircraft_profile_id") or ""
        )
        catalog = state.get("device_catalog") or {}
        facilities = state.get("existing_cns_facilities") or {}
        if getattr(self.analyzer, "algorithm_id", None) == "cns_gap_analysis_v1":
            profile, catalog, facilities = self._v1_inputs(profile, catalog, facilities)
        result = self.analyzer.analyze(
            state.get("operational_routes") or [], state.get("required_cns") or {}, profile,
            facilities, catalog,
        )
        state["cns_gap_analysis"] = result
        state["result_statuses"]["cns_gap"] = "failed" if result["status"] == "gap" else result["status"]
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _v1_inputs(profile, catalog, facilities=None):
        """Exclude post-V1 additive fields from GapV1 behavior and fingerprints."""
        profile_view = deepcopy(profile)
        if profile_view:
            for name in ("communication", "navigation", "surveillance"):
                capability = profile_view.get(name)
                if isinstance(capability, dict):
                    capability.pop("reliability", None)
                    capability.pop("fallbacks", None)
        catalog_view = deepcopy(catalog)
        for device in catalog_view.get("items", []):
            if isinstance(device, dict):
                device.pop("reliability", None)
                device.pop("vertical_profile", None)
                device.pop("coverage_geometry", None)
                device.pop("service_model", None)
        facilities_view = deepcopy(facilities or {})
        for facility in facilities_view.get("items", []):
            if not isinstance(facility, dict):
                continue
            facility.pop("vertical_profile", None)
            facility.pop("planning_profile", None)
            facility.pop("planning_origin", None)
            for device in facility.get("devices", []):
                if isinstance(device, dict):
                    device.pop("planning_origin", None)
                    device.pop("vertical_profile", None)
                    device.pop("coverage_geometry", None)
                    device.pop("service_model", None)
        return profile_view, catalog_view, facilities_view
