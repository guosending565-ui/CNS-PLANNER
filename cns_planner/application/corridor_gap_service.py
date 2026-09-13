"""Application use cases for P15 objectives and corridor gap assessment."""

from copy import deepcopy

from ..domain.cns_planning_objectives import normalize_cns_planning_objectives


class CNSCorridorGapService:
    def __init__(self, session, analyzer, invalidation, snapshot):
        self.session, self.analyzer = session, analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def objectives_snapshot(self):
        return deepcopy(self.session.state.get("cns_planning_objectives") or {})

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_corridor_gap_assessment") or self.analyzer.empty())

    def set_objectives(self, payload):
        raw = payload.get("cns_planning_objectives", payload) if isinstance(payload, dict) else payload
        normalized = normalize_cns_planning_objectives(raw)
        if normalized != self.session.state.get("cns_planning_objectives"):
            self.session.state["cns_planning_objectives"] = normalized
            self.invalidation.cns_corridor_gap()
            self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None):
        if isinstance(payload, dict) and "cns_planning_objectives" in payload:
            normalized = normalize_cns_planning_objectives(payload["cns_planning_objectives"])
            if normalized != self.session.state.get("cns_planning_objectives"):
                self.session.state["cns_planning_objectives"] = normalized
                self.invalidation.cns_corridor_gap()
        state = self.session.state
        result = self.analyzer.evaluate(
            state.get("cns_corridor_assessment") or {},
            state.get("required_cns") or {},
            state.get("cns_planning_objectives") or {},
        )
        state["cns_corridor_gap_assessment"] = result
        state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = _result_status(result.get("status"))
        self.session.save()
        return self.snapshot()


def _result_status(status):
    return {
        "passed": "passed", "failed": "failed", "pending_confirmation": "pending_confirmation",
        "missing_data": "missing_data", "not_applicable": "not_applicable", "stale": "stale",
    }.get(status, "pending_confirmation")
