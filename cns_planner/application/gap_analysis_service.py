"""Application use case for persisted CNS Gap Analysis."""

from ..catalogs import AircraftCNSProfileCatalog


class GapAnalysisService:
    def __init__(self, session, analyzer, snapshot):
        self.session, self.analyzer, self.snapshot = session, analyzer, snapshot

    def analyze(self):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {}, state.get("selected_aircraft_profile_id") or ""
        )
        result = self.analyzer.analyze(
            state.get("operational_routes") or [], state.get("required_cns") or {}, profile,
            state.get("existing_cns_facilities") or {}, state.get("device_catalog") or {},
        )
        state["cns_gap_analysis"] = result
        state["result_statuses"]["cns_gap"] = "failed" if result["status"] == "gap" else result["status"]
        self.session.save()
        return self.snapshot()
