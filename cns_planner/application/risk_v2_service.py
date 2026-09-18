"""Use cases for the additive Risk Framework V2.

Owns exactly two persisted products:

* ``risk_policy_v2`` — explicit, traceable, confirmed aggregation policy;
* ``grid_risk_v2``   — factor / domain relative engineering index result.

The service is additive by construction: it never touches ``grid_risk``
(legacy Risk V1), ``grid_attributes``, ``operational_routes`` or any CNS result.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.risk_v2 import (
    DOMAIN_IDS, FACTOR_DEFINITIONS, FACTOR_IDS, NO_SOURCE_FACTOR_IDS,
    empty_grid_risk_v2, normalize_grid_risk_v2, normalize_risk_policy_v2,
    policy_fingerprint,
)


class RiskFrameworkV2Service:
    def __init__(self, session, model, invalidation, snapshot):
        self.session, self.model = session, model
        self.invalidation, self.snapshot = invalidation, snapshot
        self.ensure_state()

    def ensure_state(self):
        """Idempotent backfill for legacy projects (schema v2 without V2 keys)."""

        state = self.session.state
        state["risk_policy_v2"] = normalize_risk_policy_v2(state.get("risk_policy_v2"))
        state["grid_risk_v2"] = normalize_grid_risk_v2(state.get("grid_risk_v2"))
        state.setdefault("result_statuses", {}).setdefault("grid_risk_v2", "not_calculated")
        return state

    # ------------------------------------------------------------------ snapshots

    def policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["risk_policy_v2"])

    def result_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["grid_risk_v2"])

    def readiness_snapshot(self):
        state = self.ensure_state()
        policy = state["risk_policy_v2"]
        result = state["grid_risk_v2"]
        factor_status = result.get("factor_status") or {}
        factors = {}
        for factor_id in FACTOR_IDS:
            summary = factor_status.get(factor_id) or {}
            definition = FACTOR_DEFINITIONS[factor_id]
            factors[factor_id] = {
                "factor_id": factor_id,
                "domain": definition["domain"],
                "raw_unit": definition.get("raw_unit"),
                "source_role": definition.get("source_role"),
                "status": summary.get("status", "not_calculated"),
                "readiness": summary.get("readiness", "blocked"),
                "cell_count": summary.get("cell_count", 0),
                "status_counts": summary.get("status_counts", {}),
                "coverage": summary.get("coverage"),
                "source_id": summary.get("source_id"),
                "source_fingerprint": summary.get("source_fingerprint"),
                "normalization": summary.get("normalization"),
                "reference": summary.get("reference"),
                "quality_flags": list(summary.get("quality_flags") or []),
                "canonical_source_available": factor_id not in NO_SOURCE_FACTOR_IDS,
            }
        domains = {}
        for domain_id in DOMAIN_IDS:
            domain = (result.get("domains") or {}).get(domain_id) or {}
            policy_domain = (policy.get("domains") or {}).get(domain_id) or {}
            domains[domain_id] = {
                "domain_id": domain_id,
                "status": domain.get("status", "not_calculated"),
                "index": domain.get("index"),
                "index_scope": domain.get("index_scope", "per_cell_only"),
                "policy_status": policy_domain.get("status", "pending_confirmation"),
                "method": policy_domain.get("method"),
                "weights": deepcopy(policy_domain.get("weights") or {}),
                "required_factors": list(policy_domain.get("required_factors") or []),
                "aggregation_policy_fingerprint": domain.get("aggregation_policy_fingerprint"),
                "data_completeness": domain.get("data_completeness", 0.0),
                "unresolved": list(domain.get("unresolved") or []),
                "cell_status_counts": domain.get("cell_status_counts", {}),
                "reason": domain.get("reason"),
            }
        return {
            "status": self._readiness_status(policy, result),
            "algorithm": {
                "algorithm_id": result.get("algorithm_id") or self.model.algorithm_id,
                "algorithm_version": result.get("algorithm_version") or self.model.algorithm_version,
            },
            "risk_semantics": "relative_engineering_index",
            "result_status": result.get("status", "not_calculated"),
            "result_stale_reason": result.get("stale_reason"),
            "policy": {
                "status": policy.get("status", "pending_confirmation"),
                "parameter_status": policy.get("parameter_status"),
                "fingerprint": policy_fingerprint(policy),
                "default_production_risk_weights": False,
                "domains": {
                    domain_id: {
                        "domain_id": domain_id,
                        "status": ((policy.get("domains") or {}).get(domain_id) or {}).get("status"),
                        "method": ((policy.get("domains") or {}).get(domain_id) or {}).get("method"),
                        "weights": deepcopy(
                            ((policy.get("domains") or {}).get(domain_id) or {}).get("weights") or {}
                        ),
                        "required_factors": list(
                            ((policy.get("domains") or {}).get(domain_id) or {}).get("required_factors") or []
                        ),
                        "source": ((policy.get("domains") or {}).get(domain_id) or {}).get("source"),
                        "confirmed": bool(
                            ((policy.get("domains") or {}).get(domain_id) or {}).get("confirmed")
                        ),
                    }
                    for domain_id in DOMAIN_IDS
                },
            },
            "factors": factors,
            "domains": domains,
            "not_computed": {
                "absolute_risk": deepcopy(result.get("absolute_risk") or {}),
                "sora_grc": deepcopy(result.get("sora_grc") or {}),
                "sora_arc": deepcopy(result.get("sora_arc") or {}),
            },
            "airspace": deepcopy(result.get("airspace") or {}),
            "input_fingerprint": result.get("input_fingerprint"),
            "semantics": deepcopy(result.get("semantics") or {}),
        }

    # -------------------------------------------------------------------- writes

    def set_policy(self, payload):
        raw = payload.get("risk_policy_v2", payload) if isinstance(payload, dict) else payload
        candidate = normalize_risk_policy_v2(raw)
        if candidate != self.session.state["risk_policy_v2"]:
            self.session.state["risk_policy_v2"] = candidate
            # Only grid_risk_v2 (and its future derived results) go stale: the
            # current V1 grid_risk, routes and CNS results do not consume V2 yet.
            self.invalidation.risk_v2("risk_policy_v2_changed")
            self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None, *, save=True):
        state = self.ensure_state()
        settings = payload if isinstance(payload, dict) else {}
        parameters = {
            "policy": state["risk_policy_v2"],
            "risk_levels_by_domain": settings.get("risk_levels_by_domain") or {},
        }
        result = self.model.evaluate(
            state.get("grid"), state.get("grid_attributes") or {}, parameters,
        )
        state["grid_risk_v2"] = result
        state.setdefault("result_statuses", {})["grid_risk_v2"] = result.get("status", "not_calculated")
        if save:
            self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _readiness_status(policy, result):
        status = result.get("status", "not_calculated")
        if status == "not_calculated":
            return "not_calculated"
        if status == "stale":
            return "stale"
        if policy.get("status") != "confirmed":
            return "pending_confirmation"
        if status == "unresolved":
            return "unresolved"
        if status == "passed":
            return "ready"
        return status

    @staticmethod
    def empty_result():
        return empty_grid_risk_v2()


__all__ = ["RiskFrameworkV2Service"]
