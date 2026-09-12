"""Application use case for the additive CNS Gap Analysis V2 result."""

from copy import deepcopy


class GapAnalysisV2Service:
    def __init__(self, session, analyzer, invalidation, snapshot):
        self.session = session
        self.analyzer = analyzer
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state["cns_gap_analysis_v2"])

    def evaluate(self, payload=None):
        state = self.session.state
        requested = (payload or {}).get("parameters") if isinstance(payload, dict) else None
        if requested is not None:
            if not isinstance(requested, dict):
                raise ValueError("Gap V2 parameters 必须是对象")
            self.analyzer.parameters = {
                "evaluate_protection_margin": False,
                **deepcopy(requested),
            }
        result = self.analyzer.analyze(
            state.get("required_cns") or {},
            state.get("coverage_3d") or {},
            state.get("cns_service_capability") or {},
            state.get("service_timeline") or {},
            state.get("protection_envelope") or {},
            state.get("device_catalog") or {},
        )
        self.invalidation.cns_site_plan()
        state["cns_gap_analysis_v2"] = result
        state["result_statuses"]["cns_gap_v2"] = {
            "confirmed_gap": "failed",
            "unknown": "pending_confirmation",
            "missing_data": "missing_data",
            "not_applicable": "not_applicable",
        }.get(result["status"], "passed")
        state["result_statuses"]["report"] = "not_calculated"
        self.session.save()
        return self.snapshot()
