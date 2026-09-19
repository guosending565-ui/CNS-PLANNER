"""Use cases for independent building-clearance policy and assessment."""

from copy import deepcopy

from ..domain.building_clearance import normalize_building_clearance_policy


class BuildingClearanceService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def policy_snapshot(self):
        return deepcopy(self.session.state["building_clearance_policy"])

    def assessment_snapshot(self):
        return deepcopy(self.session.state["building_clearance_assessment"])

    def set_policy(self, payload):
        raw = payload.get("building_clearance_policy", payload) if isinstance(payload, dict) else payload
        candidate = normalize_building_clearance_policy(raw)
        if candidate != self.session.state["building_clearance_policy"]:
            self.session.state["building_clearance_policy"] = candidate
            self.invalidation.building_clearance("clearance_policy_changed")
            self.invalidation.layered_route_validation("building_clearance_policy_changed")
            self.session.save()
        return self.snapshot()

    def evaluate(self, adapter):
        state = self.session.state
        self.invalidation.route_vertical_profiles("building_clearance_changed")
        result = self.model.evaluate(
            state.get("operational_routes"), state.get("spatial_3d"),
            state.get("building_clearance_policy"), adapter,
        )
        state["building_clearance_assessment"] = result
        state.setdefault("result_statuses", {})["building_clearance"] = result["status"]
        self.invalidation.report("building_clearance_evaluated")
        self.session.save()
        return self.snapshot()
