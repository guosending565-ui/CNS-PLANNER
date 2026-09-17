"""Application service for read-only route vertical profile derivation."""

from copy import deepcopy


class RouteVerticalProfileService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state["route_vertical_profiles"])

    def evaluate(self, sampler, payload=None):
        state = self.session.state
        result = self.model.evaluate(
            state.get("operational_routes"), state.get("spatial_3d"),
            state.get("building_clearance_assessment"), sampler,
        )
        state["route_vertical_profiles"] = result
        state.setdefault("result_statuses", {})["route_vertical_profiles"] = result["status"]
        self.invalidation.report("route_vertical_profiles_evaluated")
        self.session.save()
        return self.snapshot()
