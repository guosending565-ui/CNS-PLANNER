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
from .snapshot_read_pass import reused


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
        #: BUG-SHOT-011：只在**内容确实需要修正**时才写回。逐帧重建一个等价集合会让对象
        #: 身份每帧都变，而快照结构指纹正是用对象身份识别大对象 ⇒ 缓存永不命中，
        #: 每次 ``/api/workflow`` / ``/api/state`` 都要重付 48–56 s 的投影成本。
        current = state.get("route_risk_profiles")
        normalized = normalize_route_risk_profile_collection(current)
        if current is not normalized and normalized != current:
            state["route_risk_profiles"] = normalized
        state.setdefault("result_statuses", {}).setdefault("route_risk_profile", "not_calculated")
        return state

    # ------------------------------------------------------------------ snapshots

    def policy_snapshot(self):
        self.ensure_state()
        return deepcopy(self.session.state["route_risk_profile_policy"])

    def result_snapshot(self):
        """Read-only projection: every item carries a derived ``current_applicability``.

        Round32-J：一次快照构建内复用同一次读取结果（见
        :mod:`cns_planner.application.snapshot_read_pass`），避免在同一份未写入的
        state 上重复全量重算。返回浅拷贝顶层，调用方改写自身返回对象不影响复用缓存。
        """

        return dict(reused(
            self.session, "route_risk_profile.result_snapshot",
            self._build_result_snapshot,
        ))

    def _build_result_snapshot(self):
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
        """同步入口：**纯计算 + 唯一 production 写入**（与后台任务同源同形）。"""

        return self.apply_computed(self.plan(payload), save=save)

    def plan(self, payload=None):
        """**纯计算**：只读 state，绝不写盘、绝不 save、绝不触发失效传播。

        Round32-C：把同步 ``evaluate`` 的方法体拆成"纯计算 + 唯一写入者"两段，
        后台 heavy task 的 worker 只允许调用这一段。它是 ``(state, payload)`` 的
        确定性函数：同一 state + 同一 payload 必然得到同一 outcome。
        """

        state = self.ensure_state()
        payload = payload if isinstance(payload, dict) else {}
        policy_declared = isinstance(payload.get("policy"), dict)
        policy = (
            normalize_route_risk_profile_policy(payload["policy"])
            if policy_declared else state["route_risk_profile_policy"]
        )
        candidates = self.layered.result_snapshot()
        candidate = self._select_candidate(candidates, payload)
        identity = self._identity()
        base = {"policy": policy, "policy_declared": policy_declared, "identity": identity}
        if candidate is None:
            return {
                **base, "outcome": "rejected",
                "rejection": {
                    "status": "not_ready",
                    "reason_code": "candidate_not_available",
                    "reason": "当前没有 LayeredRouteCandidate：请先完成候选规划",
                    "candidate_id": payload.get("candidate_id"),
                },
            }
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
        # 证据不足穿越的 provenance：**只读**记录，绝不进入风险数学，也不改变任何权重。
        # 有了它，RouteRiskProfile 与后续 continuous validation / adoption 都能看到
        # "这条候选是靠 provisional policy 才被搜索出来的"。
        profile["constraint_field_applicability"] = {
            "contains_unknown_constraints": bool(
                candidate.get("contains_unknown_constraints")
            ),
            "traversed_unknown_cell_count": int(
                candidate.get("traversed_unknown_cell_count")
                or candidate.get("unknown_constraint_count") or 0
            ),
            "operational_applicability": str(
                candidate.get("operational_applicability") or "current"
            ),
            "unknown_constraint_policy": deepcopy(candidate.get("unknown_constraint_policy")),
            "unknown_affects_feasibility_only": True,
            "unknown_is_never_zero_risk": True,
            "risk_mathematics_unchanged": True,
            "objective_weights_unchanged_by_unknown": True,
            "provisional_only_never_publishable": True,
        }
        if profile["status"] != "passed":
            reasons = profile.get("blocking_reasons") or [{
                "reason_code": profile.get("status_reason") or "profile_not_available",
                "reason": "profile 未生成",
            }]
            return {
                **base, "outcome": "rejected", "rejected_profile": profile,
                "rejection": {
                    "status": profile["status"],
                    "reason_code": profile.get("status_reason"),
                    "reason": reasons[0].get("reason"),
                    "blocking_reasons": reasons,
                    "candidate_id": candidate.get("candidate_id") or payload.get("candidate_id"),
                },
            }
        return {
            **base, "outcome": "passed", "profile": profile,
            "fingerprint": profile["fingerprints"]["profile_fingerprint"],
            "candidate_id": candidate.get("candidate_id"),
        }

    def apply_computed(self, outcome, *, save=True):
        """**唯一** canonical 写入者（同步入口与后台任务 publish 阶段共用这一步）。

        写入步骤与拆分前的 ``evaluate`` 尾部逐字相同：policy 写回（若本次显式声明）、
        集合去重追加、``last_evaluation``、``result_statuses``、**同一个**
        ``session.save()``。worker 永远不调用它。
        """

        state = self.ensure_state()
        outcome = outcome if isinstance(outcome, dict) else {}
        if outcome.get("policy_declared") and isinstance(outcome.get("policy"), dict):
            state["route_risk_profile_policy"] = normalize_route_risk_profile_policy(
                outcome["policy"]
            )
        collection = normalize_route_risk_profile_collection(state["route_risk_profiles"])
        if str(outcome.get("outcome")) == "rejected":
            return self._finish(
                state, collection, {}, outcome.get("identity") or self._identity(),
                outcome.get("rejection") or {}, save=save,
                rejected_profile=outcome.get("rejected_profile"),
            )
        profile = outcome.get("profile")
        if not isinstance(profile, dict) or not profile:
            raise ValueError("航路风险画像没有产出结果")
        fingerprint = str(
            outcome.get("fingerprint")
            or (profile.get("fingerprints") or {}).get("profile_fingerprint")
        )
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
            "candidate_id": outcome.get("candidate_id"),
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
