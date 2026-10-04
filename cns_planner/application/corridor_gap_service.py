"""Application use cases for P15 objectives and corridor gap assessment."""

from copy import deepcopy

from ..domain.cns_planning_objectives import normalize_cns_planning_objectives
from .corridor_service import conclusion_changed
from .result_currentness import apply_algorithm_semantics_stale, projected_result


class CNSCorridorGapService:
    def __init__(self, session, analyzer, invalidation, snapshot):
        self.session, self.analyzer = session, analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def objectives_snapshot(self):
        return deepcopy(self.session.state.get("cns_planning_objectives") or {})

    def result_snapshot(self):
        """只读投影：算法语义版本变化时如实标注 stale（绝不误判 current）。"""

        result = self.session.state.get("cns_corridor_gap_assessment") or self.analyzer.empty()
        return apply_algorithm_semantics_stale(
            result, self.analyzer.algorithm_id, self.analyzer.algorithm_version,
        )

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
        previous = state.get("cns_corridor_gap_assessment") or {}
        #: Round 29-J：P15 只接受**有效 current** 的 P14。旧算法语义版本的 P14 在这里
        #: 被只读投影为 stale（stored payload 原样保留），算法层随即按既有 fail-closed
        #: 规则返回 missing_data —— 绝不用旧 P14 得出新 P15。
        result = self.analyzer.evaluate(
            projected_result(state, "cns_corridor_assessment"),
            state.get("required_cns") or {},
            state.get("cns_planning_objectives") or {},
        )
        state["cns_corridor_gap_assessment"] = result
        # 只有结论真的变化时 P16 才失效：重算得到同一 fingerprint 时 P16 的基线
        # 仍然成立，把它标成 stale 只会让 P18 的门禁（要求 current P16）无法满足。
        if conclusion_changed(previous, result):
            self.invalidation.cns_corridor_site_plan()
        state.setdefault("result_statuses", {})["cns_corridor_gap_assessment"] = _result_status(result.get("status"))
        self.session.save()
        return self.snapshot()


def _result_status(status):
    return {
        "passed": "passed", "failed": "failed", "pending_confirmation": "pending_confirmation",
        "missing_data": "missing_data", "not_applicable": "not_applicable", "stale": "stale",
    }.get(status, "pending_confirmation")
