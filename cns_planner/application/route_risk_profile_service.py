"""RouteRiskProfile V1 use cases.

Owns exactly two additive products:

* ``route_risk_profile_policy`` — 每个 domain 显式确认的 ``{medium_min, high_min}``（无默认值）；
* ``route_risk_profiles``       — 独立 profile 容器（current candidate 的路径风险画像）。

服务是纯增量分析：它从不重规划、不修改 candidate、不写 ``operational_routes`` /
``algorithm_selection`` / ``spatial_3d`` / ``route_operating_layers`` / ``grid_risk_v2`` 或任何
CNS 结果。旧 profile 在输入变化后只被标 ``stale``（保留为审计证据），不删除、不覆盖。

一致性 gate 失败（candidate 非 current、fingerprint 不匹配、profile 指标与 candidate
``cost_breakdown`` 不一致）时返回 ``inconsistent_evidence``，且**不保存**该 profile。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.route_risk_profile import (
    ALGORITHM_ID, ALGORITHM_VERSION, ARTIFACT_TYPE, DOMAIN_IDS,
    default_route_risk_profile_collection, normalize_route_risk_profile_collection,
    normalize_route_risk_profile_policy, route_risk_profile_policy_fingerprint,
)
from ..risk.route_profile import RouteRiskProfiler


class RouteRiskProfileService:
    def __init__(self, session, invalidation, snapshot, layered_route_planner_service, profiler=None):
        self.session = session
        self.invalidation = invalidation
        self.snapshot = snapshot
        #: The Layered Route Planner service is the only candidate authority: it provides the
        #: current-lane identity and the candidate/mask projection.
        self.layered = layered_route_planner_service
        self.profiler = profiler or RouteRiskProfiler()
        self.ensure_state()

    # ------------------------------------------------------------------ state

    def ensure_state(self):
        """Idempotent backfill for legacy projects (schema v2 without profile keys)."""

        state = self.session.state
        state["route_risk_profile_policy"] = normalize_route_risk_profile_policy(
            state.get("route_risk_profile_policy")
        )
        state["route_risk_profiles"] = normalize_route_risk_profile_collection(
            state.get("route_risk_profiles")
        )
        state.setdefault("result_statuses", {}).setdefault("route_risk_profile", "not_calculated")
        return state

    # ------------------------------------------------------------------ snapshots

    def policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["route_risk_profile_policy"])

    def result_snapshot(self):
        """Read-only projection: every item carries a derived ``current_applicability``."""

        state = self.ensure_state()
        collection = normalize_route_risk_profile_collection(state.get("route_risk_profiles"))
        candidates = self._candidate_index()
        projected = []
        for item in collection["items"]:
            entry = deepcopy(item)
            entry["current_applicability"] = self._applicability(item, candidates)
            projected.append(entry)
        collection["items"] = projected
        collection["count"] = len(projected)
        return collection

    def readiness_snapshot(self):
        state = self.ensure_state()
        policy = state["route_risk_profile_policy"]
        collection = self.layered.result_snapshot()
        identity = self._identity()
        candidate = self._select_candidate(collection, {})
        risk = state.get("grid_risk_v2") or {}
        blockers = []
        if not (state.get("grid") or {}).get("cells"):
            blockers.append({
                "reason_code": "grid_unavailable",
                "reason": "当前项目没有 MH/T 标准网格",
            })
        if candidate is None:
            blockers.append({
                "reason_code": "candidate_not_available",
                "reason": "当前没有 LayeredRouteCandidate：请先完成候选规划",
            })
        elif candidate.get("status") != "candidate":
            blockers.append({
                "reason_code": "candidate_not_current",
                "reason": f"当前 candidate 状态为 {candidate.get('status')}，不是 candidate/current",
            })
        if str(risk.get("status") or "") in ("", "not_calculated"):
            blockers.append({
                "reason_code": "grid_risk_v2_not_calculated",
                "reason": "grid_risk_v2 尚未计算：路径暴露缺少 domain index 证据",
            })
        thresholds_configured = {
            domain_id: policy["domains"][domain_id]["status"] == "confirmed"
            for domain_id in DOMAIN_IDS
        }
        return {
            "status": "ready" if not blockers else "blocked",
            "algorithm": {"algorithm_id": ALGORITHM_ID, "algorithm_version": ALGORITHM_VERSION},
            "artifact_type": ARTIFACT_TYPE,
            "candidate": {
                "status": (candidate or {}).get("status"),
                "candidate_id": (candidate or {}).get("candidate_id"),
                "lane_key": (candidate or {}).get("lane_key"),
                "route_id": (candidate or {}).get("route_id"),
                "altitude_layer_id": (candidate or {}).get("altitude_layer_id"),
                "candidate_fingerprint": (candidate or {}).get("candidate_fingerprint"),
                "risk_fingerprint": (candidate or {}).get("risk_fingerprint"),
                "current_applicability": (candidate or {}).get("current_applicability"),
            },
            "current_identity": identity,
            "grid_risk_v2": {
                "status": risk.get("status", "not_calculated"),
                "input_fingerprint": risk.get("input_fingerprint"),
                "policy_fingerprint": risk.get("policy_fingerprint"),
                "cell_count": len(risk.get("cells") or {}),
                "overall_used": False,
            },
            "profile_policy": {
                "status": policy.get("status"),
                "parameter_status": policy.get("parameter_status"),
                "fingerprint": route_risk_profile_policy_fingerprint(policy),
                "domains": {
                    domain_id: {
                        "domain_id": domain_id,
                        "status": policy["domains"][domain_id]["status"],
                        "medium_min": policy["domains"][domain_id]["medium_min"],
                        "high_min": policy["domains"][domain_id]["high_min"],
                        "confirmed": bool(policy["domains"][domain_id]["confirmed"]),
                        "source": policy["domains"][domain_id]["source"],
                    }
                    for domain_id in DOMAIN_IDS
                },
            },
            "domains": {
                domain_id: {
                    "domain_id": domain_id,
                    "thresholds_status": policy["domains"][domain_id]["status"],
                    "classification_available": thresholds_configured[domain_id],
                    "high_risk_metrics": (
                        "available" if thresholds_configured[domain_id] else "not_configured"
                    ),
                }
                for domain_id in DOMAIN_IDS
            },
            "profile_count": len(state["route_risk_profiles"]["items"]),
            "blockers": blockers,
            "not_computed": {
                "absolute_risk": "not_computed",
                "sora_grc": "not_computed",
                "sora_arc": "not_computed",
                "cross_domain_overall": "not_computed",
                "cross_domain_high_risk": "not_computed",
            },
            "airspace": {
                "status": "not_applicable",
                "applicability": "display_only",
                "used_in_value_or_fingerprint": False,
            },
            "semantics": {
                "classification_is_per_domain_only": True,
                "thresholds_have_no_default": True,
                "factor_contribution_is_relative_engineering_contribution": True,
                "analysis_only_no_replanning": True,
            },
            "notes": [
                "未确认阈值的 domain 仍会输出 exposure / mean / max，但 classification 与 "
                "high-risk 指标为 not_configured / null。",
                "profile 只分析 current candidate；candidate/risk/policy 变化只会 stale profile。",
            ],
        }

    # ------------------------------------------------------------------ writes

    def set_policy(self, payload):
        raw = (
            payload.get("route_risk_profile_policy", payload)
            if isinstance(payload, dict) else payload
        )
        candidate = normalize_route_risk_profile_policy(raw)
        if candidate != self.session.state["route_risk_profile_policy"]:
            self.session.state["route_risk_profile_policy"] = candidate
            # Only the additive RouteRiskProfile result goes stale: legacy routes, V3
            # experiments/adoptions, operational_routes and CNS results are untouched.
            self.invalidation.route_risk_profile("route_risk_profile_policy_changed")
            self.session.save()
        return self.snapshot()

    def delete_profile(self, profile_id):
        state = self.ensure_state()
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        remaining = [item for item in collection["items"] if item.get("profile_id") != profile_id]
        if len(remaining) == len(collection["items"]):
            raise ValueError(f"未找到 RouteRiskProfile：{profile_id}")
        collection["items"] = remaining
        collection["count"] = len(remaining)
        collection["status"] = "passed" if remaining else "not_calculated"
        state["route_risk_profiles"] = collection
        state.setdefault("result_statuses", {})["route_risk_profile"] = collection["status"]
        self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, payload=None, *, save=True):
        state = self.ensure_state()
        payload = payload if isinstance(payload, dict) else {}
        if isinstance(payload.get("policy"), dict):
            state["route_risk_profile_policy"] = normalize_route_risk_profile_policy(
                payload["policy"]
            )
        policy = state["route_risk_profile_policy"]
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        candidates = self.layered.result_snapshot()
        candidate = self._select_candidate(candidates, payload)
        identity = self._identity()
        if candidate is None:
            return self._finish(
                state, collection, payload, identity, {
                    "status": "not_ready",
                    "reason_code": "candidate_not_available",
                    "reason": "当前没有 LayeredRouteCandidate：请先完成候选规划",
                }, save=save,
            )
        expected = {
            "candidate_fingerprint": identity.get("candidate_fingerprint"),
            "risk_fingerprint": identity.get("risk_fingerprint"),
            "lane_key": identity.get("lane_key"),
            "current_applicability": candidate.get("current_applicability"),
        }
        profile = self.profiler.evaluate(
            candidate=candidate, grid=state.get("grid") or {},
            grid_risk_v2=state.get("grid_risk_v2") or {},
            profile_policy=policy, expected=expected,
        )
        profile["policy"] = deepcopy(policy)
        if profile["status"] != "passed":
            reasons = profile.get("blocking_reasons") or [{
                "reason_code": profile.get("status_reason") or "profile_not_available",
                "reason": "profile 未生成",
            }]
            return self._finish(
                state, collection, payload, identity, {
                    "status": profile["status"],
                    "reason_code": profile.get("status_reason"),
                    "reason": reasons[0].get("reason"),
                    "blocking_reasons": reasons,
                    "candidate_id": candidate.get("candidate_id"),
                }, save=save, rejected_profile=profile,
            )
        fingerprint = profile["fingerprints"]["profile_fingerprint"]
        items = [
            item for item in collection["items"]
            if item.get("profile_id") != profile["profile_id"]
            and item.get("fingerprints", {}).get("profile_fingerprint") != fingerprint
        ]
        items.append(profile)
        collection["items"] = items
        collection["count"] = len(items)
        collection["status"] = "passed"
        collection["last_evaluation"] = {
            "status": "passed",
            "candidate_id": candidate.get("candidate_id"),
            "profile_id": profile["profile_id"],
            "profile_fingerprint": fingerprint,
            "reason_code": None,
            "reason": None,
            "blocking_reasons": [],
        }
        state["route_risk_profiles"] = collection
        state.setdefault("result_statuses", {})["route_risk_profile"] = "passed"
        if save:
            self.session.save()
        return self.snapshot()

    # ------------------------------------------------------------------ invalidation

    def refresh_for_reason(self, reason="route_risk_profile_input_changed"):
        """Stale every RouteRiskProfile (grid_risk_v2 / policy / global input change).

        Only ``route_risk_profiles`` is touched; the frozen evidence of a stale profile is
        kept for audit.  Legacy routes, V3 and CNS results are never invalidated here.
        """

        state = self.ensure_state()
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        if not collection["items"]:
            return
        for item in collection["items"]:
            if item.get("status") != "stale":
                item["status"] = "stale"
            item["stale_reason"] = str(reason)
        collection["status"] = "stale"
        collection["stale_reason"] = str(reason)
        state["route_risk_profiles"] = collection
        state.setdefault("result_statuses", {})["route_risk_profile"] = "stale"

    def stale_for_candidates(self, candidate_ids, reason="candidate_changed"):
        """Stale only the profiles that reference one of the given candidates."""

        wanted = {str(value) for value in (candidate_ids or []) if value}
        if not wanted:
            return
        state = self.ensure_state()
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        changed = False
        for item in collection["items"]:
            if str((item.get("candidate") or {}).get("candidate_id")) not in wanted:
                continue
            if item.get("status") != "stale":
                item["status"] = "stale"
                changed = True
            item["stale_reason"] = str(reason)
        if not changed:
            return
        state["route_risk_profiles"] = collection
        if all(item.get("status") == "stale" for item in collection["items"]):
            collection["status"] = "stale"
            state.setdefault("result_statuses", {})["route_risk_profile"] = "stale"

    def reconcile(self, reason="candidate_changed", *, save=True):
        """Stale profiles whose referenced candidate is gone, replaced or stale."""

        state = self.ensure_state()
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        if not collection["items"]:
            return self.snapshot() if save else None
        candidates = self._candidate_index(project=True)
        changed = False
        for item in collection["items"]:
            if item.get("status") == "stale":
                continue
            record = item.get("candidate") or {}
            current = candidates.get(str(record.get("candidate_id")))
            if (
                current is None
                or current.get("status") != "candidate"
                or current.get("candidate_fingerprint") != record.get("candidate_fingerprint")
                or current.get("current_applicability") != "current"
            ):
                item["status"] = "stale"
                item["stale_reason"] = str(reason)
                changed = True
        if changed:
            state["route_risk_profiles"] = collection
            if all(item.get("status") == "stale" for item in collection["items"]):
                collection["status"] = "stale"
                state.setdefault("result_statuses", {})["route_risk_profile"] = "stale"
            if save:
                self.session.save()
        return self.snapshot() if save else None

    # ------------------------------------------------------------------ helpers

    def _finish(self, state, collection, payload, identity, rejection, *, save=True,
                rejected_profile=None):
        """Record an explicit rejection: a failed profile is never stored as a result."""

        collection["last_evaluation"] = {
            "status": rejection.get("status"),
            "candidate_id": rejection.get("candidate_id") or payload.get("candidate_id"),
            "profile_id": (rejected_profile or {}).get("profile_id"),
            "profile_fingerprint": None,
            "reason_code": rejection.get("reason_code"),
            "reason": rejection.get("reason"),
            "blocking_reasons": list(rejection.get("blocking_reasons") or []),
            "current_identity": deepcopy(identity),
        }
        state["route_risk_profiles"] = collection
        state.setdefault("result_statuses", {})["route_risk_profile"] = (
            "passed" if collection["items"] else "not_calculated"
        )
        if save:
            self.session.save()
        return self.snapshot()

    def _identity(self):
        provider = getattr(self.layered, "current_identity", None)
        if not callable(provider):
            return {}
        try:
            return deepcopy(provider())
        except (TypeError, ValueError, RuntimeError):
            return {}

    def _candidate_index(self, *, project=False):
        collection = self.layered.result_snapshot()
        if project:
            return {
                str(item.get("candidate_id")): item for item in collection.get("items") or []
            }
        return {str(item.get("candidate_id")): item for item in collection.get("items") or []}

    @staticmethod
    def _select_candidate(collection, payload):
        items = [item for item in (collection or {}).get("items") or [] if isinstance(item, dict)]
        candidate_id = payload.get("candidate_id")
        if candidate_id:
            return next(
                (item for item in items if item.get("candidate_id") == candidate_id), None,
            )
        lane_key = payload.get("lane_key")
        if lane_key:
            return next(
                (item for item in reversed(items)
                 if item.get("lane_key") == lane_key and item.get("status") == "candidate"),
                None,
            )
        active = (collection or {}).get("active_candidate_id")
        if active:
            found = next((item for item in items if item.get("candidate_id") == active), None)
            if found is not None:
                return found
        return next(
            (item for item in reversed(items) if item.get("status") == "candidate"), None,
        )

    @staticmethod
    def _applicability(profile, candidates):
        if profile.get("status") == "stale":
            return "stale"
        record = profile.get("candidate") or {}
        current = candidates.get(str(record.get("candidate_id")))
        if current is None:
            return "stale_candidate_removed"
        if current.get("status") != "candidate":
            return "stale_candidate_status_changed"
        if current.get("candidate_fingerprint") != record.get("candidate_fingerprint"):
            return "stale_candidate_changed"
        if current.get("current_applicability") != "current":
            return "stale_request_or_inputs_changed"
        return "current"

    @staticmethod
    def empty_collection():
        return default_route_risk_profile_collection()


__all__ = ["RouteRiskProfileService"]
