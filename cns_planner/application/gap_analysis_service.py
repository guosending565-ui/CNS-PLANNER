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
        if getattr(self.analyzer, "algorithm_id", None) == "cns_gap_analysis_v1":
            profile, catalog = self._v1_inputs(profile, catalog)
        result = self.analyzer.analyze(
            state.get("operational_routes") or [], state.get("required_cns") or {}, profile,
            state.get("existing_cns_facilities") or {}, catalog,
        )
        state["cns_gap_analysis"] = result
        state["result_statuses"]["cns_gap"] = "failed" if result["status"] == "gap" else result["status"]
        self.session.save()
        return self.snapshot()

    @staticmethod
    def _v1_inputs(profile, catalog):
        """Exclude P4-only fields from V1 behavior and input fingerprints."""
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
        return profile_view, catalog_view
