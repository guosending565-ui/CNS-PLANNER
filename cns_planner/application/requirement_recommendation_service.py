"""Application orchestration for P17 recommendation preview and explicit adoption."""

from copy import deepcopy
from hashlib import sha256
import json

from ..domain.cns_inputs import normalize_required_cns
from ..domain.requirement_policy import (
    empty_operation_context, empty_required_cns_adoption,
    empty_required_cns_recommendation, empty_requirement_policies,
    normalize_operation_context, normalize_requirement_policies,
)
from ..domain.reporting import mark_active_report_stale


class RequirementRecommendationService:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model, self.invalidation, self.snapshot = session, model, invalidation, snapshot

    def context_snapshot(self):
        return deepcopy(self.session.state.get("cns_operation_context") or empty_operation_context())

    def policies_snapshot(self):
        return deepcopy(self.session.state.get("cns_requirement_policies") or empty_requirement_policies())

    def result_snapshot(self):
        result = deepcopy(self.session.state.get("required_cns_recommendation") or empty_required_cns_recommendation())
        expected = (result.get("input_fingerprints") or {}).get("current_required_cns")
        current_fingerprint = _fingerprint(normalize_required_cns(self.session.state.get("required_cns")))
        adoption = self.session.state.get("required_cns_adoption") or {}
        adopted_current = (
            adoption.get("status") == "adopted"
            and adoption.get("recommendation_fingerprint") == result.get("input_fingerprint")
            and adoption.get("required_cns_fingerprint") == current_fingerprint
        )
        result["current_required_cns_diverged"] = bool(expected and expected != current_fingerprint and not adopted_current)
        result["adoption_status"] = "adopted_current" if adopted_current else adoption.get("status", "not_adopted")
        return result

    def adoption_snapshot(self):
        return deepcopy(self.session.state.get("required_cns_adoption") or empty_required_cns_adoption())

    def set_context(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Operation Context 请求必须是对象")
        state = self.session.state
        if "project_default" in payload or "route_overrides" in payload:
            candidate = payload
        else:
            candidate = deepcopy(state.get("cns_operation_context") or empty_operation_context())
            scope = payload.get("scope", "project")
            context = payload.get("context")
            if not isinstance(context, dict):
                raise ValueError("Operation Context context 必须是对象")
            if scope == "project":
                candidate["project_default"] = context
            elif scope == "route":
                route_id = str(payload.get("route_id"))
                if route_id not in self._route_ids():
                    raise ValueError("Operation Context 航路不存在")
                candidate.setdefault("route_overrides", {})[route_id] = context
            else:
                raise ValueError("Operation Context scope 必须是 project 或 route")
        state["cns_operation_context"] = normalize_operation_context(candidate)
        self.invalidation.requirement_recommendation("operation_context_changed")
        self.session.save()
        return self.snapshot()

    def set_policies(self, payload):
        self.session.state["cns_requirement_policies"] = normalize_requirement_policies(payload)
        self.invalidation.requirement_recommendation("requirement_policies_changed")
        self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None):
        state = self.session.state
        result = self.model.evaluate(
            state.get("required_cns"), state.get("cns_operation_context"),
            state.get("cns_requirement_policies"), self._route_ids(),
        )
        mark_active_report_stale(state, "P17 requirement recommendation reevaluated")
        state["required_cns_recommendation"] = result
        state.setdefault("result_statuses", {})["required_cns_recommendation"] = result.get("result_status", "not_calculated")
        self.session.save()
        return self.snapshot()

    def adopt(self, payload=None):
        state = self.session.state
        result = state.get("required_cns_recommendation") or {}
        if result.get("status") == "conflict" or result.get("conflicts"):
            raise ValueError("Recommendation 存在 policy conflict，禁止 Adopt")
        if result.get("status") != "recommendation_ready" or not isinstance(result.get("recommended_required_cns"), dict):
            raise ValueError("Recommendation 尚未完整可采用")
        actual = self._input_fingerprints()
        if actual != result.get("input_fingerprints"):
            raise ValueError("Recommendation 已过期，请重新 evaluate")
        adopted = normalize_required_cns(result["recommended_required_cns"])
        adopted["source"] = "explicit_adoption_from_required_cns_recommendation"
        state["required_cns"] = adopted
        state["required_cns_adoption"] = {
            "status": "adopted", "recommendation_fingerprint": result.get("input_fingerprint"),
            "algorithm_id": result.get("algorithm_id"), "algorithm_version": result.get("algorithm_version"),
            "context_fingerprint": actual["operation_context"],
            "policy_fingerprint": actual["requirement_policies"],
            "required_cns_fingerprint": _fingerprint(adopted),
            "source": str((payload or {}).get("source") or "explicit_user_adoption"),
            "provenance": deepcopy(result.get("field_provenance") or {}),
        }
        self.invalidation.workflow("required_cns")
        self.session.save()
        return self.snapshot()

    def _input_fingerprints(self):
        state = self.session.state
        return {
            "current_required_cns": _fingerprint(normalize_required_cns(state.get("required_cns"))),
            "operation_context": _fingerprint(normalize_operation_context(state.get("cns_operation_context"))),
            "requirement_policies": _fingerprint(normalize_requirement_policies(state.get("cns_requirement_policies"))),
            "route_ids": _fingerprint(self._route_ids()),
        }

    def _route_ids(self):
        state = self.session.state
        return sorted({
            str(item.get("route_id")) for key in ("scenario_routes", "operational_routes")
            for item in state.get(key, []) if item.get("route_id")
        })


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
