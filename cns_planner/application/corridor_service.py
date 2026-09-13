"""Application use case for persisted P14 corridor policy and assessment."""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.cns_corridor import normalize_cns_corridor_policy


class CNSCorridorService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_corridor_assessment") or self.model.empty())

    def evaluate(self, payload=None):
        state = self.session.state
        raw_policy = payload.get("cns_corridor_policy", payload.get("policy")) if isinstance(payload, dict) else None
        if raw_policy is not None:
            policy = normalize_cns_corridor_policy(raw_policy)
            if policy != state.get("cns_corridor_policy"):
                state["cns_corridor_policy"] = policy
                self.invalidation.cns_corridor()
        policy = state.get("cns_corridor_policy") or normalize_cns_corridor_policy()
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        selections = state.get("algorithm_selection") or {}
        result = self.model.evaluate(
            state.get("operational_routes") or [], state.get("spatial_3d") or {},
            state.get("grid") or {}, state.get("grid_attributes") or {},
            state.get("required_cns") or {}, profile,
            state.get("existing_cns_facilities") or {}, state.get("device_catalog") or {},
            policy,
            coverage_parameters=((selections.get("coverage_model") or {}).get("parameters") or {}),
            capability_parameters=((selections.get("service_model") or {}).get("parameters") or {}),
        )
        state["cns_corridor_assessment"] = result
        self.invalidation.cns_corridor_gap()
        state.setdefault("result_statuses", {})["cns_corridor_assessment"] = _result_status(result.get("status"))
        self.session.save()
        return self.snapshot()


def _result_status(status):
    return {
        "passed": "passed", "failed": "failed", "not_applicable": "not_applicable",
        "pending_confirmation": "pending_confirmation", "missing_data": "missing_data",
        "unresolved": "missing_data", "stale": "stale",
    }.get(status, "pending_confirmation")
