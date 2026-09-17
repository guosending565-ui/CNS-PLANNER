"""Application boundary for persisted engineering 3D encounter results."""

from copy import deepcopy


class Encounter3DService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session = session
        self.model = model
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state["encounter_3d_assessment"])

    def evaluate(self, payload=None):
        state, payload = self.session.state, payload or {}
        timing = state.get("operational_timing") or {}
        selection = {**(timing.get("encounter_lab") or {}), **(payload.get("selection") or {})}
        result = self.model.evaluate(
            timing, state.get("service_timeline") or {},
            state.get("protection_envelope") or {}, selection,
        )
        state["encounter_3d_assessment"] = result
        state.setdefault("result_statuses", {})["encounter_3d_assessment"] = result["status"]
        self.invalidation.report("encounter_3d_assessment_evaluated")
        self.session.save()
        return self.snapshot()
