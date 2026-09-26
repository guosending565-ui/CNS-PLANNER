"""Application use case for the additive CNS Gap Analysis V2 result."""

from copy import deepcopy

from ..compatibility.project_adapter import read_existing_legacy
from .production_write_authority import (
    runtime_compatibility_result, write_runtime_compatibility_result,
)


class GapAnalysisV2Service:
    def __init__(self, session, analyzer, invalidation, snapshot):
        self.session = session
        self.analyzer = analyzer
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self):
        runtime = runtime_compatibility_result(self.session, "cns_gap_analysis_v2")
        if runtime:
            return deepcopy(runtime)
        return read_existing_legacy(
            self.session.state, "cns_gap_analysis_v2", "CNSGapAnalyzerV2",
        )

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
        record = write_runtime_compatibility_result(
            self.session, "cns_gap_analysis_v2", result,
            source_algorithm={
                "algorithm_type": "cns_gap_analyzer",
                "algorithm_id": getattr(self.analyzer, "algorithm_id", None),
                "algorithm_version": getattr(self.analyzer, "algorithm_version", None),
                "class": type(self.analyzer).__name__,
            },
            note="Advanced 缺口对照仅在当前会话运行，不写入项目或 canonical gap。",
        )
        response = self.snapshot()
        response["cns_gap_analysis_v2"] = deepcopy(record)
        return response
