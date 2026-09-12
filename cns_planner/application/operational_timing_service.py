"""Application use cases for operational timing, timeline and protection results."""

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.operational_timing import normalize_operational_timing


class OperationalTimingService:
    def __init__(self, session, timeline_model, protection_model, invalidation, snapshot):
        self.session = session
        self.timeline_model = timeline_model
        self.protection_model = protection_model
        self.invalidation = invalidation
        self.snapshot = snapshot

    def timing_snapshot(self):
        return deepcopy(self.session.state["operational_timing"])

    def timeline_snapshot(self):
        return deepcopy(self.session.state["service_timeline"])

    def protection_snapshot(self):
        return deepcopy(self.session.state["protection_envelope"])

    def set_timing(self, payload):
        raw = payload.get("operational_timing", payload) if isinstance(payload, dict) else payload
        candidate = normalize_operational_timing(raw)
        previous = self.session.state["operational_timing"]
        if candidate == previous:
            return self.snapshot()
        if (
            candidate["route_motion_profiles"] != previous.get("route_motion_profiles")
            or candidate["service_scenarios"] != previous.get("service_scenarios")
        ):
            self.invalidation.service_timeline()
        if (
            candidate["response_time_budgets"] != previous.get("response_time_budgets")
            or candidate["encounter_scenarios"] != previous.get("encounter_scenarios")
        ):
            self.invalidation.protection_envelope()
        self.session.state["operational_timing"] = candidate
        self.session.save()
        return self.snapshot()

    def evaluate_timeline(self, payload=None):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        result = self.timeline_model.evaluate(
            state.get("coverage_3d") or {},
            state.get("cns_service_capability") or {},
            state.get("required_cns") or {}, profile,
            state.get("operational_timing") or {},
        )
        self.invalidation.cns_gap_v2()
        state["service_timeline"] = result
        state["result_statuses"]["service_timeline"] = result["status"]
        state["result_statuses"]["report"] = "not_calculated"
        self.session.save()
        return self.snapshot()

    def evaluate_protection(self, payload=None):
        state = self.session.state
        timing = state.get("operational_timing") or {}
        payload = payload or {}
        budgets = timing.get("response_time_budgets") or {}
        encounters = timing.get("encounter_scenarios") or {}
        budget_id = str(payload.get("budget_id") or next(iter(budgets), ""))
        encounter_id = str(payload.get("encounter_id") or next(iter(encounters), ""))
        result = self.protection_model.evaluate(
            budgets.get(budget_id), encounters.get(encounter_id),
        )
        self.invalidation.protection_envelope()
        state["protection_envelope"] = result
        state["result_statuses"]["protection_envelope"] = (
            "pending_confirmation" if result["status"] == "unknown" else result["status"]
        )
        state["result_statuses"]["report"] = "not_calculated"
        self.session.save()
        return self.snapshot()
