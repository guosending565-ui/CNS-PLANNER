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
from .closed_loop_service import rerun_p7_p10_chain
from .corridor_site_planning_service import rerun_corridor_chain
from .project_state import assessment


class PlanReviewService:
    """Application-only decision workflow; no planning formula lives here."""

    def __init__(self, session, coverage_model, capability_model, timeline_model,
                 gap_model, corridor_model, corridor_gap_analyzer, snapshot):
        self.session, self.snapshot = session, snapshot
        self.coverage_model, self.capability_model = coverage_model, capability_model
        self.timeline_model, self.gap_model = timeline_model, gap_model
        self.corridor_model, self.corridor_gap_analyzer = corridor_model, corridor_gap_analyzer

    def snapshot_result(self):
        return {
            "cns_plan_review": deepcopy(self.session.state.get("cns_plan_review") or empty_plan_review()),
            "confirmed_cns_plan": deepcopy(self.session.state.get("confirmed_cns_plan") or empty_confirmed_plan()),
        }

    def initialize(self, payload=None):
        state = self.session.state
        self._require_current_inputs(state)
        baseline_fp = review_baseline_fingerprint(state)
        p16 = state.get("cns_corridor_site_plan") or {}
        p16_fp = p16.get("input_fingerprint")
        variants = [make_variant(baseline_fp, [], "baseline", "Baseline", p16_fp)]
        auto_ids = sorted(str(item.get("action_id")) for item in p16.get("selected_actions") or [])
        if auto_ids:
            variants.append(make_variant(baseline_fp, auto_ids, "p16_auto", "P16 Auto Proposal", p16_fp))
        review = empty_plan_review("current")
        review.update({
            "review_id": f"PR-{baseline_fp[:20]}", "baseline_fingerprint": baseline_fp,
            "input_fingerprints": review_input_fingerprints(state), "variants": variants,
            "initialized_from": {
                "p14_fingerprint": (state.get("cns_corridor_assessment") or {}).get("input_fingerprint"),
                "p15_fingerprint": (state.get("cns_corridor_gap_assessment") or {}).get("input_fingerprint"),
                "p16_fingerprint": p16_fp, "p16_status": p16.get("status"),
            },
        })
        for variant in review["variants"]:
            self._evaluate_variant(state, review, variant)
        review["selected_variant_id"] = review["variants"][0]["variant_id"]
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
            (self.session.state.get("cns_corridor_site_plan") or {}).get("input_fingerprint"),
            payload.get("notes") or "",
        )
        existing = next((item for item in review["variants"] if item["variant_id"] == candidate["variant_id"]), None)
        if existing is None:
            self._evaluate_variant(self.session.state, review, candidate)
            review["variants"].append(candidate)
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def evaluate(self, payload=None):
        review = self._current_review()
        requested = (payload or {}).get("variant_id")
        variants = [self._variant(review, requested)] if requested else review["variants"]
        for variant in variants:
            self._evaluate_variant(self.session.state, review, variant)
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def select(self, payload):
        review = self._current_review()
        variant = self._variant(review, payload.get("variant_id"))
        review["selected_variant_id"] = variant["variant_id"]
        self.session.state["cns_plan_review"] = review
        self.session.save()
        return self.snapshot()

    def confirm(self, payload):
        review = self._current_review()
        variant = self._variant(review, payload.get("variant_id") or review.get("selected_variant_id"))
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
            "confirmation": {"source": str(payload.get("source") or "user_confirmation"), "reason": str(payload.get("reason") or "")},
            "application": {"status": "not_applied", "application_id": f"PVAPP-{variant['variant_id']}"},
        })
        self.session.state["confirmed_cns_plan"] = plan
        self.session.save()
        return self.snapshot()

    def apply(self, payload):
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
            working["cns_corridor_assessment"], working["cns_corridor_gap_assessment"] = planned_p14, planned_p15
            committed_plan = deepcopy(confirmed)
            committed_plan["status"] = "applied"
            committed_plan["application"].update({
                "status": "applied", "applied_refs": refs,
                "planning_origin": {"plan_id": confirmed.get("plan_id"), "variant_id": confirmed.get("variant_id")},
            })
            working["confirmed_cns_plan"] = committed_plan
            _post_apply_statuses(working)
            state.clear(); state.update(working)
            self.session.save()
        except Exception:
            state.clear(); state.update(original)
            raise
        return self.snapshot()

    def _evaluate_variant(self, state, review, variant):
        actions = self._actions(variant.get("selected_action_ids") or [])
        facilities, refs = _apply_actions(
            deepcopy(state.get("existing_cns_facilities") or {}), actions,
            f"PVAPP-{variant['variant_id']}", variant["variant_id"],
        )
        p14, p15 = rerun_corridor_chain(state, self.corridor_model, self.corridor_gap_analyzer, facilities)
        baseline = state.get("cns_corridor_gap_assessment") or {}
        variant["evaluation"] = {
            "status": "evaluated", "actions": actions, "applied_refs": refs,
            "before_p15": deepcopy(baseline), "after_p15": deepcopy(p15),
            "planned_p14_fingerprint": p14.get("input_fingerprint"),
            "planned_p15_fingerprint": p15.get("input_fingerprint"),
            "authoritative_hypothetical": {"p14": deepcopy(p14), "p15": deepcopy(p15), "persisted_as_upstream": False},
            "comparison_matrix": comparison_matrix(p15),
            "confirmation_gate": confirmation_gate(baseline, p15),
            "action_summary": action_summary(actions),
        }
        variant["status"] = "evaluated"

    def _actions(self, action_ids):
        wanted = set(str(item) for item in action_ids)
        p16 = self.session.state.get("cns_corridor_site_plan") or {}
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
    def _require_current_inputs(state):
        for name in ("cns_corridor_assessment", "cns_corridor_gap_assessment"):
            if (state.get(name) or {}).get("status") in (None, "not_calculated", "missing_data", "stale"):
                raise ValueError(f"P18 需要 current {name}")
        if (state.get("cns_corridor_site_plan") or {}).get("status") not in ("proposal_ready", "no_action_required", "no_eligible_proposal", "evidence_required"):
            raise ValueError("P18 需要 current P16 proposal/status")

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


def _post_apply_statuses(state):
    statuses = state.setdefault("result_statuses", {})
    statuses["coverage_3d"] = (state.get("coverage_3d") or {}).get("status", "not_calculated")
    capability_status = (state.get("cns_service_capability") or {}).get("status")
    statuses["cns_service_capability"] = {
        "meets_under_model": "passed", "does_not_meet_under_model": "failed",
        "unknown": "pending_confirmation", "unsupported_model": "missing_data",
        "not_applicable": "not_applicable",
    }.get(capability_status, capability_status or "not_calculated")
    statuses["service_timeline"] = (state.get("service_timeline") or {}).get("status", "not_calculated")
    gap_status = (state.get("cns_gap_analysis_v2") or {}).get("status")
    statuses["cns_gap_v2"] = {
        "confirmed_gap": "failed", "unknown": "pending_confirmation",
        "missing_data": "missing_data", "not_applicable": "not_applicable",
    }.get(gap_status, "passed")
    for key in ("cns_corridor_assessment", "cns_corridor_gap_assessment"):
        result_status = (state.get(key) or {}).get("status")
        statuses[key] = {
            "passed": "passed", "failed": "failed", "pending_confirmation": "pending_confirmation",
            "missing_data": "missing_data", "not_applicable": "not_applicable",
            "unresolved": "missing_data", "stale": "stale",
        }.get(result_status, "pending_confirmation")
    for key, value_key in (("coverage", "coverage"), ("cns_gap", "cns_gap_analysis"), ("cns_site_plan", "cns_site_plan"),
                           ("closed_loop_assessment", "closed_loop_assessment"), ("cns_corridor_site_plan", "cns_corridor_site_plan")):
        value = state.get(value_key)
        if isinstance(value, dict) and value.get("status") != "not_calculated": value["status"] = "stale"
        if value is not None: statuses[key] = "stale"
    statuses["technical_risk"] = statuses["report"] = "stale"
    if isinstance(state.get("cns_plan_review"), dict):
        state["cns_plan_review"]["status"] = "applied"
        statuses["cns_plan_review"] = "passed"
    state.setdefault("risks", {})["technical"] = assessment("stale", "P18 controlled apply 已提交；P5/P6 safety 未自动重评")
