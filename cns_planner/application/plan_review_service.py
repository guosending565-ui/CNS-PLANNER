"""P18 human review, variant comparison, confirmation and controlled apply."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from ..domain.plan_review import (
    action_summary, comparison_matrix, confirmation_gate, empty_confirmed_plan,
    empty_plan_review, fingerprint, make_variant, review_baseline_fingerprint,
    review_input_fingerprints,
)
from ..domain.closed_loop import _interval_transitions, _subsystem_index
from ..domain.reporting import mark_active_report_stale
from .closed_loop_service import rerun_p7_p10_chain
from .continuous_service_service import STEP6_ALLOWED_ACCEPTABILITY
from .corridor_site_planning_service import rerun_corridor_chain
from .production_write_authority import assert_write_authority
from .result_currentness import projected_result


class PlanReviewService:
    """Application-only decision workflow; no planning formula lives here."""

    def __init__(self, session, coverage_model, capability_model, timeline_model,
                 gap_model, corridor_model, corridor_gap_analyzer, snapshot,
                 invalidation=None, continuous_service=None, plan_projection=None):
        self.session, self.snapshot = session, snapshot
        self.coverage_model, self.capability_model = coverage_model, capability_model
        self.timeline_model, self.gap_model = timeline_model, gap_model
        self.corridor_model, self.corridor_gap_analyzer = corridor_model, corridor_gap_analyzer
        self.invalidation = invalidation
        #: Round 2.5：P17 连续服务可接受性的门禁投影。它为 None 时（旧装配）P18 不施加
        #: P17 门禁——但生产组合根一定会注入它。
        self.continuous_service = continuous_service
        #: Round 2.6：投影态构建器。每个 Plan Variant 都要用它算出**自己的**
        #: post-plan P17 结论（不再复制权威结论）。为 None 时该变体如实记录
        #: ``evaluated_for_this_variant=false``（fail-closed，绝不冒充）。
        self.plan_projection = plan_projection

    def snapshot_result(self):
        return {
            "cns_plan_review": deepcopy(self.session.state.get("cns_plan_review") or empty_plan_review()),
            "confirmed_cns_plan": deepcopy(self.session.state.get("confirmed_cns_plan") or empty_confirmed_plan()),
            #: Round 2.5：P17 的 Step6 门禁投影（只读；前端据此显示"是否放行 + 强制披露"）。
            "continuous_service_gate": self._p17_gate(raise_on_block=False),
            #: Round 2.6：每个 variant **自己的** P17 投影门禁（Step6 实际依据）。
            "variant_continuous_service_gates": self._variant_gates(),
        }

    def _variant_gates(self):
        review = self.session.state.get("cns_plan_review") or {}
        return {
            str(variant.get("variant_id")): self._variant_p17_gate(
                variant, raise_on_block=False,
            )
            for variant in review.get("variants") or []
        }

    def _p17_gate(self, *, raise_on_block=True):
        """P17 连续服务可接受性的 Step6 门禁（**fail-closed**）。

        * ``fully_satisfied`` / ``acceptable_with_managed_gap`` ⇒ 放行
          （managed_gap 必须在 review/report 中强制披露，绝不表述为"全覆盖"）；
        * ``unacceptable`` / ``unknown``（含"尚未评估"/"已 stale"）⇒ 阻止。

        Round 2.6：权威 ``status`` 已经是 **post-plan 投影态**结论；``baseline_status``
        与投影结果一并返回，Step6 必须同时显示两层。
        """

        if self.continuous_service is None:
            return {"status": "not_configured", "confirmation_allowed": True,
                    "allowed_statuses": [], "blocking_reason": None}
        gate = self.continuous_service.step6_gate()
        if gate.get("confirmation_allowed") is not True:
            status = str(gate.get("status") or "not_calculated")
            reason = (
                "P17 连续服务可接受性为 unknown（证据缺失）——必须 fail-closed，"
                "请先补齐工程依据并重新评估"
                if status in ("unknown", "stale", "not_calculated")
                else f"P17 连续服务可接受性为 {status}——不可接受，不得进入正式评审"
            )
            gate = {**gate, "blocking_reason": reason}
            if raise_on_block:
                raise ValueError(f"P18 需要可接受的 P17 结论：{reason}")
        return gate

    def _variant_p17_gate(self, variant, *, raise_on_block=True):
        """**变体级** P17 门禁（Round 2.6）。

        Step6 评审必须依据【该 variant 实施后的 P17】，而不是复制当前权威 P17。
        ``evaluated_for_this_variant != true`` 时一律 fail-closed（阻止）。
        """

        projection = (
            (variant.get("evaluation") or {}).get("continuous_service_projection") or {}
        )
        gate = projection.get("variant_specific_gate") or {
            "status": "unknown", "confirmation_allowed": False,
            "variant_specific": True,
        }
        gate = {
            **gate,
            "allowed_statuses": list(STEP6_ALLOWED_ACCEPTABILITY),
            "evaluated_for_this_variant": projection.get("evaluated_for_this_variant") is True,
            "baseline_status": projection.get("baseline_status"),
            "projected_status": projection.get("projected_status"),
            "managed_gap_count": int(projection.get("projected_managed_gap_count") or 0),
            "limitations": deepcopy(projection.get("limitations") or []),
            "requires_managed_gap_disclosure": bool(
                projection.get("disclosure_lines")
            ),
            "disclosure_lines": deepcopy(projection.get("disclosure_lines") or []),
        }
        if gate.get("confirmation_allowed") is not True:
            status = str(gate.get("status") or "unknown")
            reason = (
                "本 variant 的 P17 投影结论不可用或为 unknown（证据缺失）——fail-closed，"
                "不得进入正式确认"
                if status in ("unknown", "stale", "not_calculated")
                else f"本 variant 实施后的 P17 投影结论为 {status}——不可接受，不得确认"
            )
            gate = {**gate, "blocking_reason": reason}
            if raise_on_block:
                raise ValueError(f"P18 需要该 variant 自身可接受的 P17 投影结论：{reason}")
        return gate

    def initialize(self, payload=None):
        state = self.session.state
        self._require_current_inputs(state)
        p17 = self._p17_gate()
        baseline_fp = review_baseline_fingerprint(state)
        #: Round 29-J：初始化记录同样只读投影（此处门禁已通过，投影与 raw 结论一致；
        #: 但消费方一律不直读 raw 容器，语义口径唯一）。
        p16 = projected_result(state, "cns_corridor_site_plan") or {}
        p16_fp = p16.get("input_fingerprint")
        variants = [make_variant(baseline_fp, [], "baseline", "Baseline", p16_fp)]
        auto_ids = sorted(str(item.get("action_id")) for item in p16.get("selected_actions") or [])
        if auto_ids:
            variants.append(make_variant(baseline_fp, auto_ids, "p16_auto", "P16 Auto Proposal", p16_fp))
        review = empty_plan_review("current")
        review.update({
            "review_id": f"PR-{baseline_fp[:20]}", "baseline_fingerprint": baseline_fp,
            "input_fingerprints": review_input_fingerprints(state), "variants": variants,
            "continuous_service_gate": deepcopy(p17),
            "initialized_from": {
                "p14_fingerprint": (projected_result(state, "cns_corridor_assessment") or {}).get("input_fingerprint"),
                "p15_fingerprint": (projected_result(state, "cns_corridor_gap_assessment") or {}).get("input_fingerprint"),
                "p16_fingerprint": p16_fp, "p16_status": p16.get("status"),
                "p17_status": p17.get("status"),
                "p17_input_fingerprint": p17.get("input_fingerprint"),
            },
        })
        for variant in review["variants"]:
            self._evaluate_variant(state, review, variant)
        review["selected_variant_id"] = review["variants"][0]["variant_id"]
        mark_active_report_stale(state, "P18 review reinitialized")
        state["cns_plan_review"] = review
        state.setdefault("result_statuses", {})["cns_plan_review"] = "passed"
        self.session.save()
        return self.snapshot()

    def create_variant(self, payload):
        review = self._current_review()
        base = self._variant(review, payload.get("base_variant_id") or review.get("selected_variant_id"))
        confirmed = self.session.state.get("confirmed_cns_plan") or {}
        includes = set(str(item) for item in payload.get("include_action_ids") or [])
        excludes = set(str(item) for item in payload.get("exclude_action_ids") or [])
        ids = (set(base.get("selected_action_ids") or []) | includes) - excludes
        self._actions(ids)
        if confirmed.get("status") == "confirmed" and base.get("variant_id") == confirmed.get("variant_id") and ids == set(base.get("selected_action_ids") or []):
            raise ValueError("Confirmed variant 不可原地修改；请 include/exclude 后 clone")
        candidate = make_variant(
            review["baseline_fingerprint"], ids, "user_edited",
            payload.get("name") or "User Variant",
            (projected_result(self.session.state, "cns_corridor_site_plan") or {}).get(
                "input_fingerprint"),
            payload.get("notes") or "",
        )
        existing = next((item for item in review["variants"] if item["variant_id"] == candidate["variant_id"]), None)
        if existing is None:
            self._evaluate_variant(self.session.state, review, candidate)
            review["variants"].append(candidate)
            mark_active_report_stale(self.session.state, "P18 variant set changed")
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None):
        review = self._current_review()
        requested = (payload or {}).get("variant_id")
        variants = [self._variant(review, requested)] if requested else review["variants"]
        for variant in variants:
            self._evaluate_variant(self.session.state, review, variant)
        mark_active_report_stale(self.session.state, "P18 variant reevaluated")
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def select(self, payload):
        review = self._current_review()
        variant = self._variant(review, payload.get("variant_id"))
        mark_active_report_stale(self.session.state, "P18 selected variant changed")
        review["selected_variant_id"] = variant["variant_id"]
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def confirm(self, payload):
        assert_write_authority(self, "confirmed_cns_plan")
        #: Round 2.5：P17 门禁在 Confirm 时**再次**校验（review 初始化后证据可能已变化）。
        p17_gate = self._p17_gate()
        review = self._current_review()
        variant = self._variant(review, payload.get("variant_id") or review.get("selected_variant_id"))
        #: Round 2.6：确认还必须通过**该 variant 自己**的 P17 投影门禁
        #: （Step6 依据【该 variant 实施后的 P17】，而不是权威 P17）。
        variant_p17_gate = self._variant_p17_gate(variant)
        evaluation = variant.get("evaluation") or {}
        gate = evaluation.get("confirmation_gate") or {}
        acknowledgement = None
        if gate.get("status") == "objectives_not_configured" and payload.get("confirm_without_objectives") is True:
            acknowledgement = {
                "kind": "confirm_without_objectives", "confirmed": True,
                "source": str(payload.get("source") or "user_confirmation"),
                "reason": str(payload.get("reason") or ""),
            }
            if not acknowledgement["reason"]:
                raise ValueError("无规划目标确认必须记录 reason")
        elif gate.get("status") != "ready_for_confirmation":
            raise ValueError(f"variant 不满足 Confirm 门禁：{gate.get('status')}")
        mark_active_report_stale(self.session.state, "P18 confirmed plan changed")
        prior = deepcopy(self.session.state.get("confirmed_cns_plan") or empty_confirmed_plan())
        history = deepcopy(prior.get("history") or [])
        if prior.get("status") in ("confirmed", "applied"):
            history.append({key: deepcopy(prior.get(key)) for key in ("plan_id", "variant_id", "decision", "variant", "application", "current_applicability")})
        identity = fingerprint([review["review_id"], variant["variant_id"], evaluation.get("planned_p15_fingerprint")])
        plan = empty_confirmed_plan("confirmed")
        plan.update({
            "plan_id": f"CP-{identity[:20]}", "variant_id": variant["variant_id"],
            "decision": "confirmed", "variant": deepcopy(variant), "history": history,
            "current_applicability": "current", "confirmed_actions": deepcopy(evaluation.get("actions") or []),
            "before_p15": deepcopy(evaluation.get("before_p15")), "after_p15": deepcopy(evaluation.get("after_p15")),
            "required_cns_fingerprint": fingerprint(self.session.state.get("required_cns") or {}),
            "requirement_basis": {
                "recommendation": deepcopy(self.session.state.get("required_cns_recommendation") or {}),
                "adoption": deepcopy(self.session.state.get("required_cns_adoption") or {}),
            },
            "source_fingerprints": {
                **deepcopy(review.get("input_fingerprints") or {}),
                "review_baseline": review.get("baseline_fingerprint"),
                "planned_p14": evaluation.get("planned_p14_fingerprint"),
                "planned_p15": evaluation.get("planned_p15_fingerprint"),
            },
            "acknowledgements": [acknowledgement] if acknowledgement else [],
            "continuous_service_gate": deepcopy(p17_gate),
            #: Round 2.6：被确认的方案携带**它自己的**投影态 P17 结论与门禁。
            "variant_continuous_service_gate": deepcopy(variant_p17_gate),
            "variant_continuous_service_projection": deepcopy(
                evaluation.get("continuous_service_projection") or {}
            ),
            "managed_gap_disclosure": deepcopy(
                (variant_p17_gate.get("disclosure_lines") or [])
                or (p17_gate.get("disclosure_lines") or [])
            ),
            "limitation_disclosure": deepcopy(variant_p17_gate.get("limitations") or []),
            "confirmation": {"source": str(payload.get("source") or "user_confirmation"), "reason": str(payload.get("reason") or "")},
            "application": {"status": "not_applied", "application_id": f"PVAPP-{variant['variant_id']}"},
        })
        self.session.state["confirmed_cns_plan"] = plan
        self.session.save()
        return self.snapshot()

    def apply(self, payload):
        assert_write_authority(self, "confirmed_cns_plan")
        state = self.session.state
        confirmed = state.get("confirmed_cns_plan") or {}
        requested = str((payload or {}).get("plan_id") or "")
        if not requested or requested != confirmed.get("plan_id"):
            return self._rejected("stale_plan", "Apply 必须指定当前 confirmed plan_id")
        if confirmed.get("status") == "applied" and (confirmed.get("application") or {}).get("status") == "applied":
            return self.snapshot()
        if confirmed.get("status") != "confirmed" or confirmed.get("current_applicability") != "current":
            return self._rejected("stale_plan", "仅 current confirmed plan 可 Apply")
        current = review_input_fingerprints(state)
        stored = confirmed.get("source_fingerprints") or {}
        if any(current.get(key) != stored.get(key) for key in current):
            return self._rejected("stale_plan", "RequiredCNS/routes/设施/候选/设备/P14-P16 baseline 已变化")
        original = deepcopy(state)
        try:
            baseline = deepcopy(original)
            rerun_p7_p10_chain(baseline, self.coverage_model, self.capability_model, self.timeline_model, self.gap_model)
            working = deepcopy(baseline)
            facilities, refs = _apply_actions(
                working.get("existing_cns_facilities") or {},
                confirmed.get("confirmed_actions") or [],
                (confirmed.get("application") or {}).get("application_id"),
                confirmed.get("variant_id"),
            )
            working["existing_cns_facilities"] = facilities
            rerun_p7_p10_chain(working, self.coverage_model, self.capability_model, self.timeline_model, self.gap_model)
            p10_gate = _p10_gate(baseline.get("cns_gap_analysis_v2") or {}, working.get("cns_gap_analysis_v2") or {})
            if p10_gate["status"] != "passed":
                return self._rejected(p10_gate["status"], p10_gate["reason"])
            planned_p14, planned_p15 = rerun_corridor_chain(
                working, self.corridor_model, self.corridor_gap_analyzer, facilities,
            )
            evaluation = (confirmed.get("variant") or {}).get("evaluation") or {}
            if (
                planned_p14.get("input_fingerprint") != evaluation.get("planned_p14_fingerprint")
                or planned_p15.get("input_fingerprint") != evaluation.get("planned_p15_fingerprint")
            ):
                return self._rejected("validation_mismatch", "Apply P14/P15 与 confirmed variant Preview 不一致")
            committed_plan = deepcopy(confirmed)
            committed_plan["status"] = "applied"
            committed_plan["application"].update({
                "status": "applied", "applied_refs": refs,
                "planning_origin": {"plan_id": confirmed.get("plan_id"), "variant_id": confirmed.get("variant_id")},
            })
            facilities_changed = facilities != (original.get("existing_cns_facilities") or {})
            if facilities_changed:
                state["existing_cns_facilities"] = facilities
                if self.invalidation is None:
                    raise RuntimeError("PlanReview Apply 缺少统一 existing_cns 失效服务")
                # 唯一 authoritative invalidation path：ExistingCNS 是事实变更，所有
                # canonical 下游由依赖图统一标 stale，working-copy 试算结果不提交。
                self.invalidation.workflow("existing_cns")
            state["confirmed_cns_plan"] = committed_plan
            review = state.get("cns_plan_review")
            if isinstance(review, dict):
                review["status"] = "applied"
                review.pop("stale_reason", None)
                state.setdefault("result_statuses", {})["cns_plan_review"] = "passed"
            mark_active_report_stale(state, "P18 confirmed plan applied")
            state.setdefault("result_statuses", {})["report"] = "stale"
            self.session.save()
        except Exception:
            _restore_failed_apply(state, original)
            raise
        return self.snapshot()

    def _evaluate_variant(self, state, review, variant):
        actions = self._actions(variant.get("selected_action_ids") or [])
        facilities, refs = _apply_actions(
            deepcopy(state.get("existing_cns_facilities") or {}), actions,
            f"PVAPP-{variant['variant_id']}", variant["variant_id"],
        )
        p14, p15 = rerun_corridor_chain(state, self.corridor_model, self.corridor_gap_analyzer, facilities)
        baseline = projected_result(state, "cns_corridor_gap_assessment") or {}
        #: Round 2.6：本 variant **自己的** post-plan P17 投影结论。
        #: 绝不再复制权威 P17 结论，也绝不在 Confirm 之前改写现网事实。
        projection_result, projected, projection_meta = self._variant_p17(actions)
        variant["evaluation"] = {
            "status": "evaluated", "actions": actions, "applied_refs": refs,
            "before_p15": deepcopy(baseline), "after_p15": deepcopy(p15),
            "planned_p14_fingerprint": p14.get("input_fingerprint"),
            "planned_p15_fingerprint": p15.get("input_fingerprint"),
            "authoritative_hypothetical": {"p14": deepcopy(p14), "p15": deepcopy(p15), "persisted_as_upstream": False},
            "comparison_matrix": comparison_matrix(p15),
            "confirmation_gate": confirmation_gate(baseline, p15),
            "action_summary": action_summary(actions),
            "continuous_service_projection": self._variant_p17_projection(
                state, actions, projection_result, projected, projection_meta,
            ),
        }
        variant["status"] = "evaluated"

    def _variant_p17(self, actions):
        """算本 variant 的 projected P17（只读；不写任何状态）。"""

        if self.plan_projection is None or self.continuous_service is None:
            return None, None, {
                "available": False,
                "unavailable_reason": "projection_builder_not_configured",
            }
        projection, result = self.continuous_service.evaluate_actions(
            actions, projection_label="plan_variant",
        )
        return (result, result, projection) if result is not None else (
            None, None, projection or {"available": False},
        )

    @staticmethod
    def _variant_p17_projection(state, actions, projection_result, projected, projection_meta):
        """把本 variant 的投影结论整理成前端 / Step6 消费的结构。

        三种情形必须**严格区分**（Round 2.6）：

        1. ``selected_action_ids`` 为空 ⇒ 本 variant 就是**当前权威状态的 no-op**，
           因此它的 P17 结论**就是**权威 P17 结论。这不算"没有评估"，也不算"复制别人的
           结论"——它评估的对象确实是同一个状态（``evaluated_basis =
           "authoritative_current_state"``）。
        2. 有 action 且投影可评估 ⇒ 本 variant **自己的**投影态结论
           （``evaluated_basis = "variant_projection"``）。
        3. 有 action 但投影不可用 ⇒ 如实记录 ``evaluated_for_this_variant=false``，
           并 fail-closed（绝不复用权威结论冒充）。
        """

        authoritative = state.get("continuous_service_acceptability") or {}
        comparison = ((projected or {}).get("post_plan_projection") or {}).get("comparison") or {}
        has_actions = bool(actions)
        evaluated = (projected is not None) or not has_actions
        status = (
            (projected or {}).get("status") if projected is not None
            else authoritative.get("status")
        )
        disclosure = (
            (projected or {}).get("disclosure_lines") if projected is not None
            else authoritative.get("disclosure_lines")
        ) or []
        limitations = (
            (projected or {}).get("limitations") if projected is not None
            else authoritative.get("limitations")
        ) or []
        return {
            #: Round 2.6 关键修复：本字段由 false 收口为**真实评估结果**。
            "evaluated_for_this_variant": evaluated,
            "evaluated_basis": (
                "variant_projection" if projected is not None
                else "authoritative_current_state" if not has_actions
                else "unavailable"
            ),
            "projected_status": status,
            "status": status,
            "baseline_status": (
                (projected or {}).get("baseline_status") if projected is not None
                else authoritative.get("baseline_status")
            ),
            "post_plan_status": (
                (projected or {}).get("post_plan_status") if projected is not None
                else authoritative.get("post_plan_status")
            ),
            "projected_managed_gap_count": (
                (projected or {}).get("managed_gap_count") if projected is not None
                else authoritative.get("managed_gap_count")
            ),
            "projected_unacceptable_count": (
                (projected or {}).get("unacceptable_count") if projected is not None
                else authoritative.get("unacceptable_count")
            ),
            "projected_unknown_count": (
                (projected or {}).get("unknown_count") if projected is not None
                else authoritative.get("unknown_count")
            ),
            "primary_threat_status": (
                (projected or {}).get("primary_threat_status") if projected is not None
                else authoritative.get("primary_threat_status")
            ),
            "supplementary_threat_status": (
                (projected or {}).get("supplementary_threat_status") if projected is not None
                else authoritative.get("supplementary_threat_status")
            ),
            "limitations": deepcopy(limitations),
            "disclosure_lines": deepcopy(disclosure),
            "variant_specific_gate": {
                "status": status if evaluated else "unknown",
                "confirmation_allowed": bool(
                    evaluated and status in STEP6_ALLOWED_ACCEPTABILITY
                ),
                "variant_specific": True,
                "evaluated_basis": (
                    "variant_projection" if projected is not None
                    else "authoritative_current_state" if not has_actions
                    else "unavailable"
                ),
                "blocking_reason": None if evaluated else (
                    "本 variant 的 P17 投影结论不可用（fail-closed）"
                ),
            },
            "comparison": deepcopy(comparison),
            "applied_action_ids": deepcopy(
                (projection_meta or {}).get("applied_action_ids")
                or [str(item.get("action_id") or "") for item in actions or []]
            ),
            "projection_semantics": (projection_meta or {}).get("projection_semantics"),
            "persisted_as_upstream": False,
            "written_into_existing_cns": False,
            "authoritative_status": authoritative.get("status"),
            "authoritative_managed_gap_count": authoritative.get("managed_gap_count"),
            "note": (
                "本结论是**本 variant 假设实施后**的投影态评估（hypothetical），"
                "不是现网事实；Apply 之前绝不写入 ExistingCNS。"
                if projected is not None else
                "本 variant 没有 plan action：它的评估对象**就是**当前权威状态，"
                "因此复用权威 P17 结论（这不是「复制别人的结论」，而是同一个评估对象）。"
                if not has_actions else
                "本 variant 的 P17 投影结论不可用：如实保持不可判定（fail-closed），"
                "绝不复用权威结论冒充本 variant 的结论。"
            ),
        }

    def _actions(self, action_ids):
        wanted = set(str(item) for item in action_ids)
        p16 = projected_result(self.session.state, "cns_corridor_site_plan") or {}
        catalog = {}
        for item in [*(p16.get("candidate_actions") or []), *(p16.get("selected_actions") or [])]:
            if item.get("action_id"):
                catalog[str(item["action_id"])] = deepcopy(item)
        missing = sorted(wanted - set(catalog))
        if missing:
            raise ValueError(f"variant action 不属于当前 P16：{', '.join(missing)}")
        return [catalog[item] for item in sorted(wanted)]

    def _current_review(self):
        review = deepcopy(self.session.state.get("cns_plan_review") or {})
        if review.get("status") != "current" or review.get("baseline_fingerprint") != review_baseline_fingerprint(self.session.state):
            raise ValueError("P18 review 未初始化或已 stale")
        return review

    @staticmethod
    def _variant(review, variant_id):
        value = next((item for item in review.get("variants") or [] if item.get("variant_id") == variant_id), None)
        if value is None:
            raise ValueError("Plan Variant 不存在")
        return value

    @staticmethod
    def readiness_blockers(state):
        """P18 初始化所需的 current 输入**只读**清单（与 fail-closed 门禁同一条规则）。

        Round32-H：诊断草稿必须能说清"为什么现在不能确认"，而这条规则**只能有
        一份实现**。``_require_current_inputs`` 因此改为消费本函数并抛出**第一条**
        原因（消息逐字不变），报告草稿则把全部原因原样列出。
        """

        #: Round 29-J：Step6（P18 initialize）的 currentness 门禁必须消费唯一权威
        #: ``projected_result`` —— 含算法语义 stale 只读投影。旧算法语义版本的 P14/P15/P16
        #: 即使 raw status 仍是 passed/failed，也必须在这里 fail-closed，绝不放行。
        blockers = []
        for name in ("cns_corridor_assessment", "cns_corridor_gap_assessment"):
            if (projected_result(state, name) or {}).get("status") in (None, "not_calculated", "missing_data", "stale"):
                blockers.append(f"P18 需要 current {name}")
        # P16 必须 current：candidate_sites / site_planner /
        # corridor_site_planning_policy 的变化只让 P16 变 stale（P14/P15 仍 current），
        # device catalog 的变化会连带 P14；两种情况下若放行，P18 都可能依据**旧**的
        # P16 candidate_actions / selected_actions 初始化正式评审，基于已经变化的
        # 候选站或设备。上游 P14/P15 的重算是否让 P16 失效由 ``conclusion_changed``
        # 决定：同一 input_fingerprint 的无变化重算不会制造 stale，因此这里的
        # fail-closed 不会让审阅无法初始化。
        proposal = projected_result(state, "cns_corridor_site_plan") or {}
        if proposal.get("status") not in ("proposal_ready", "no_action_required", "no_eligible_proposal", "evidence_required"):
            blockers.append("P18 需要 current P16 proposal/status")
        return blockers

    @staticmethod
    def _require_current_inputs(state):
        blockers = PlanReviewService.readiness_blockers(state)
        if blockers:
            raise ValueError(blockers[0])

    def _rejected(self, status, reason):
        response = self.snapshot()
        result = deepcopy(response.get("confirmed_cns_plan") or {})
        result["apply_attempt"] = {"status": status, "reason": reason}
        response["confirmed_cns_plan"] = result
        return response


def _apply_actions(existing, actions, application_id, plan_id):
    facilities = deepcopy(existing or {}); facilities.setdefault("items", [])
    refs = []
    for action in sorted(actions, key=lambda item: str(item.get("action_id") or "")):
        action_id = str(action.get("action_id") or "")
        origin = {"application_id": application_id, "action_id": action_id, "confirmed_plan_id": plan_id}
        if action.get("facility_id"):
            facility = next((item for item in facilities["items"] if str(item.get("facility_id")) == str(action["facility_id"])), None)
            if facility is None: raise ValueError(f"ExistingCNSFacility 不存在：{action.get('facility_id')}")
        else:
            site_id = str(action.get("site_id") or "")
            coordinate = deepcopy(action.get("coordinate"))
            if not site_id or not isinstance(coordinate, list) or len(coordinate) < 2:
                raise ValueError("candidate/new-build action 缺少显式 site/coordinate")
            facility_id = f"P18-{sha256((str(action.get('reuse_class'))+'|'+site_id).encode()).hexdigest()[:16]}"
            facility = next((item for item in facilities["items"] if item.get("facility_id") == facility_id), None)
            if facility is None:
                facility = {"facility_id": facility_id, "site_id": site_id, "name": f"Confirmed Plan {site_id}",
                            "coordinate": coordinate, "vertical_profile": deepcopy(action.get("vertical") or {}),
                            "devices": [], "status": "active", "source": "P18 controlled plan application",
                            "planning_origin": {**origin, "source_candidate_site_id": site_id},
                            "metadata": {"source_candidate_site_id": site_id}}
                facilities["items"].append(facility)
        devices = facility.setdefault("devices", [])
        existing_device = next((item for item in devices if str(item.get("device_id")) == str(action.get("device_id"))), None)
        if existing_device is None:
            devices.append({"device_id": action.get("device_id"), "subsystem": action.get("subsystem"),
                            "status": "active", "service_model": {"status": "missing_data"},
                            "planning_origin": origin, "metadata": {"planning_origin": origin}})
            status = "applied"
        else:
            status = "already_installed"
        refs.append({"action_id": action_id, "facility_id": facility.get("facility_id"),
                     "device_id": action.get("device_id"), "status": status})
    facilities["count"] = len(facilities["items"])
    if facilities["items"]: facilities["status"] = "passed"
    return facilities, refs


def _p10_gate(before, after):
    left, right = _subsystem_index(before), _subsystem_index(after)
    for route_id, subsystem in sorted(set(left) | set(right)):
        for item in _interval_transitions(route_id, subsystem, left.get((route_id, subsystem)) or {}, right.get((route_id, subsystem)) or {}):
            if (
                item["before_planning"] == "satisfied"
                or item["before_combined"] in ("satisfied", "satisfied_by_contingency")
            ) and item["after_combined"] == "confirmed_gap":
                return {"status": "regression", "reason": "P10 出现新的 confirmed planning regression"}
            if item["before_combined"] == "satisfied" and item["after_combined"] == "unknown":
                return {"status": "evidence_required", "reason": "P10 satisfied 区间变为 unknown；不能作为已验证 Apply"}
    return {"status": "passed", "reason": None}


def _restore_failed_apply(state, original):
    """Rollback only after a failed targeted commit; never use whole-table commit semantics."""

    for key in tuple(state):
        if key not in original:
            del state[key]
    for key, value in original.items():
        state[key] = deepcopy(value)
