"""Transactional P12 preview/apply orchestration over the existing P7-P10 chain."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.closed_loop import (
    algorithm_run_summary, build_plan_application, compare_gap_results,
    current_baseline_fingerprint, stable_fingerprint,
)
from .project_state import assessment


class ClosedLoopService:
    def __init__(
        self, session, coverage_model, capability_model, timeline_model,
        gap_model, snapshot,
    ):
        self.session = session
        self.coverage_model = coverage_model
        self.capability_model = capability_model
        self.timeline_model = timeline_model
        self.gap_model = gap_model
        self.snapshot = snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state["closed_loop_assessment"])

    def evaluate(self, payload=None):
        original = deepcopy(self.session.state)
        try:
            result, _ = self._build_assessment(original)
            self.session.state["closed_loop_assessment"] = result
            self.session.state["result_statuses"]["closed_loop_assessment"] = _result_status(result)
            # Preview is the only persisted change; avoid touching project timestamps.
            self.session.repository.save(self.session.state)
        except Exception:
            self.session.state.clear()
            self.session.state.update(original)
            raise
        return self.snapshot()

    def apply(self, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("closed-loop apply 请求必须是对象")
        current = self.session.state
        stored = current.get("closed_loop_assessment") or {}
        application = stored.get("application") or {}
        requested_id = payload.get("application_id")
        if not requested_id:
            return self._rejected("stale_assessment", "apply 必须显式提交当前 application_id")
        if stored.get("commit_status") == "committed":
            if requested_id == application.get("application_id"):
                return self.snapshot()
            return self._rejected("stale_assessment", "application_id 与已提交 assessment 不一致")
        if stored.get("validation_status") != "validated_improvement":
            return self._rejected("not_validated", "仅 validated_improvement assessment 可提交")
        if requested_id != application.get("application_id"):
            return self._rejected("stale_assessment", "application_id 与当前 assessment 不一致")
        if (
            current_baseline_fingerprint(current) != application.get("baseline_fingerprint")
            or (current.get("cns_site_plan") or {}).get("input_fingerprint")
            != application.get("site_plan_fingerprint")
            or (current.get("cns_site_plan") or {}).get("status") != "proposal_ready"
        ):
            return self._rejected("stale_assessment", "project baseline 或 P11 site plan 已变化")

        original = deepcopy(current)
        try:
            reassessed, planned = self._build_assessment(original)
            if (
                reassessed.get("assessment_fingerprint") != stored.get("assessment_fingerprint")
                or reassessed.get("validation_status") != "validated_improvement"
            ):
                return self._rejected("stale_assessment", "重跑结果与 Preview assessment 不一致")
            committed = deepcopy(original)
            for name in (
                "existing_cns_facilities", "coverage_3d",
                "cns_service_capability", "service_timeline",
                "cns_gap_analysis_v2",
            ):
                committed[name] = deepcopy(planned[name])
            reassessed["commit_status"] = "committed"
            committed["closed_loop_assessment"] = reassessed
            _apply_post_commit_statuses(committed)
            current.clear()
            current.update(committed)
            self.session.save()
        except Exception:
            current.clear()
            current.update(original)
            raise
        return self.snapshot()

    def _build_assessment(self, source_state):
        site_plan = source_state.get("cns_site_plan") or {}
        if site_plan.get("status") != "proposal_ready":
            raise ValueError("P12 需要当前 proposal_ready 的 P11 site plan")
        selected = list(site_plan.get("selected_actions") or [])
        baseline_fingerprint = current_baseline_fingerprint(source_state)
        application = build_plan_application(site_plan, baseline_fingerprint)

        baseline = deepcopy(source_state)
        self._run_chain(baseline)
        planned = deepcopy(baseline)
        facilities, applied_refs = _apply_selected_actions(
            planned.get("existing_cns_facilities") or {}, selected, application,
        )
        planned["existing_cns_facilities"] = facilities
        application["applied_refs"] = applied_refs
        self._run_chain(planned)

        comparison = compare_gap_results(
            baseline["cns_gap_analysis_v2"], planned["cns_gap_analysis_v2"],
            site_plan.get("resolved_planning_gap_length_m"), selected,
        )
        result = {
            "status": _assessment_status(comparison["validation_status"]),
            "validation_status": comparison["validation_status"],
            "commit_status": "preview",
            "model_scope": "engineering_closed_loop_verification",
            "real_cns_model_validation": "not_evaluated",
            "certification_conclusion": "not_evaluated",
            "application": application,
            "algorithm_runs": {
                "baseline": algorithm_run_summary(baseline),
                "planned": algorithm_run_summary(planned),
            },
            "comparisons": comparison["comparisons"],
            "prediction_comparison": comparison["prediction_comparison"],
            "residual_gap_segments": comparison["residual_gap_segments"],
            "regression_segments": comparison["regression_segments"],
            "selected_action_summary": {
                "count": len(selected),
                "action_ids": list(application["selected_action_ids"]),
                "reuse_counts": deepcopy(site_plan.get("reuse_counts") or {}),
                "cost_summary": deepcopy(site_plan.get("cost_summary") or {}),
            },
            "reasons": comparison["reasons"],
            "safety_event_evaluation": "not_evaluated",
            "safety_improvement_claim": "prohibited",
        }
        result["assessment_fingerprint"] = stable_fingerprint({
            key: value for key, value in result.items()
            if key not in ("assessment_fingerprint", "commit_status")
        })
        return result, planned

    def _run_chain(self, state):
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        facilities = _algorithm_facilities(state.get("existing_cns_facilities") or {})
        coverage_model = _clone_model(
            self.coverage_model, state.get("coverage_3d") or {},
        )
        capability_model = _clone_model(
            self.capability_model, state.get("cns_service_capability") or {},
        )
        timeline_model = _clone_model(
            self.timeline_model, state.get("service_timeline") or {},
        )
        gap_model = _clone_model(
            self.gap_model, state.get("cns_gap_analysis_v2") or {},
        )
        state["coverage_3d"] = coverage_model.evaluate(
            state.get("operational_routes") or [], state.get("spatial_3d") or {},
            state.get("grid") or {}, state.get("grid_attributes") or {},
            facilities, state.get("device_catalog") or {},
        )
        state["cns_service_capability"] = capability_model.evaluate(
            state["coverage_3d"], state.get("required_cns") or {}, profile,
            facilities, state.get("device_catalog") or {},
        )
        state["service_timeline"] = timeline_model.evaluate(
            state["coverage_3d"], state["cns_service_capability"],
            state.get("required_cns") or {}, profile,
            state.get("operational_timing") or {},
        )
        state["cns_gap_analysis_v2"] = gap_model.analyze(
            state.get("required_cns") or {}, state["coverage_3d"],
            state["cns_service_capability"], state["service_timeline"],
            state.get("protection_envelope") or {},
            state.get("device_catalog") or {},
        )

    def _rejected(self, commit_status, reason):
        response = self.snapshot()
        assessment_value = deepcopy(self.session.state.get("closed_loop_assessment") or {})
        assessment_value["commit_status"] = commit_status
        assessment_value["reasons"] = [*(assessment_value.get("reasons") or []), reason]
        response["closed_loop_assessment"] = assessment_value
        return response


def _clone_model(model, current_result):
    result_parameters = current_result.get("parameters") if isinstance(current_result, dict) else None
    use_result = isinstance(result_parameters, dict) and (
        bool(result_parameters) or current_result.get("input_fingerprint") is not None
    )
    parameters = deepcopy(
        result_parameters if use_result else getattr(model, "parameters", {}) or {}
    )
    return model.__class__(parameters)


def _algorithm_facilities(value):
    facilities = deepcopy(value or {})
    for facility in facilities.get("items") or []:
        if isinstance(facility, dict):
            facility.pop("planning_profile", None)
            facility.pop("planning_origin", None)
            for device in facility.get("devices") or []:
                if isinstance(device, dict):
                    device.pop("planning_origin", None)
    return facilities


def _apply_selected_actions(existing, selected, application):
    facilities = deepcopy(existing or {})
    facilities.setdefault("items", [])
    refs = []
    application_id = application["application_id"]
    site_plan_fingerprint = application["site_plan_fingerprint"]
    seen_actions = set()
    for action in selected:
        action_id = str(action.get("action_id") or "")
        if not action_id or action_id in seen_actions:
            continue
        seen_actions.add(action_id)
        origin = {
            "application_id": application_id,
            "action_id": action_id,
            "site_plan_fingerprint": site_plan_fingerprint,
        }
        facility = _target_facility(facilities, action, origin)
        devices = facility.setdefault("devices", [])
        existing_origin = next((
            item for item in devices
            if (item.get("planning_origin") or {}).get("action_id") == action_id
        ), None)
        if existing_origin is None:
            if any(str(item.get("device_id") or "") == str(action.get("device_id") or "") for item in devices):
                refs.append({
                    "action_id": action_id, "facility_id": facility.get("facility_id"),
                    "device_id": action.get("device_id"), "status": "already_installed",
                })
                continue
            devices.append({
                "device_id": action.get("device_id"),
                "subsystem": action.get("subsystem"), "status": "active",
                "service_model": {"status": "missing_data"},
                "planning_origin": deepcopy(origin),
                "metadata": {"planning_origin": deepcopy(origin)},
            })
        facility_origin = facility.setdefault("planning_origin", {
            **origin, "action_ids": [],
        })
        facility_origin["action_ids"] = sorted(set([
            *(facility_origin.get("action_ids") or []), action_id,
        ]))
        refs.append({
            "action_id": action_id, "facility_id": facility.get("facility_id"),
            "device_id": action.get("device_id"),
            "site_id": action.get("site_id"), "status": "applied",
        })
    facilities["count"] = len(facilities["items"])
    facilities["status"] = "passed" if facilities["items"] else facilities.get("status", "not_calculated")
    return facilities, refs


def _target_facility(collection, action, origin):
    facility_id = action.get("facility_id")
    if facility_id:
        facility = next((
            item for item in collection["items"]
            if str(item.get("facility_id") or "") == str(facility_id)
        ), None)
        if facility is None:
            raise ValueError(f"P11 action 引用的 ExistingCNSFacility 不存在：{facility_id}")
        return facility
    site_id = str(action.get("site_id") or "")
    if not site_id:
        raise ValueError("P11 candidate/new-build action 缺少 site_id")
    reuse_class = str(action.get("reuse_class") or "candidate_site")
    deterministic = sha256(f"{reuse_class}|{site_id}".encode()).hexdigest()[:16]
    planned_id = f"P12-{deterministic}"
    facility = next((
        item for item in collection["items"]
        if str(item.get("facility_id") or "") == planned_id
    ), None)
    if facility is not None:
        return facility
    coordinate = deepcopy(action.get("coordinate"))
    vertical = deepcopy(action.get("vertical") or {})
    if not isinstance(coordinate, list) or len(coordinate) < 2:
        raise ValueError("P11 selected action 缺少显式坐标")
    facility = {
        "facility_id": planned_id, "site_id": site_id,
        "name": f"Planned {site_id}", "coordinate": coordinate,
        "vertical_profile": vertical, "devices": [], "status": "active",
        "source": "P12 closed-loop plan application",
        "planning_profile": deepcopy(action.get("planning_profile") or {}),
        "planning_origin": {
            **deepcopy(origin), "source_candidate_site_id": site_id,
            "reuse_class": reuse_class, "action_ids": [],
        },
        "metadata": {
            "source_candidate_site_id": site_id,
            "planning_origin": deepcopy(origin),
        },
    }
    collection["items"].append(facility)
    return facility


def _assessment_status(validation):
    return {
        "validated_improvement": "passed",
        "no_material_improvement": "passed",
        "regression": "failed",
        "inconclusive": "pending_confirmation",
        "not_applicable": "not_applicable",
    }[validation]


def _result_status(result):
    return _assessment_status(result.get("validation_status", "inconclusive"))


def _apply_post_commit_statuses(state):
    statuses = state.setdefault("result_statuses", {})
    statuses["coverage_3d"] = state["coverage_3d"].get("status", "not_calculated")
    statuses["cns_service_capability"] = {
        "meets_under_model": "passed", "does_not_meet_under_model": "failed",
        "unknown": "pending_confirmation", "unsupported_model": "missing_data",
        "not_applicable": "not_applicable",
    }.get(state["cns_service_capability"].get("status"), state["cns_service_capability"].get("status", "not_calculated"))
    statuses["service_timeline"] = state["service_timeline"].get("status", "not_calculated")
    statuses["cns_gap_v2"] = {
        "confirmed_gap": "failed", "unknown": "pending_confirmation",
        "missing_data": "missing_data", "not_applicable": "not_applicable",
    }.get(state["cns_gap_analysis_v2"].get("status"), "passed")
    statuses["closed_loop_assessment"] = _result_status(state["closed_loop_assessment"])
    if state.get("coverage") is not None:
        state["coverage"]["status"] = "stale"
        statuses["coverage"] = "stale"
    if (state.get("cns_gap_analysis") or {}).get("status") != "not_calculated":
        state["cns_gap_analysis"]["status"] = "stale"
        statuses["cns_gap"] = "stale"
    if (state.get("cns_site_plan") or {}).get("status") != "not_calculated":
        state["cns_site_plan"]["status"] = "stale"
        statuses["cns_site_plan"] = "stale"
    statuses["technical_risk"] = "stale"
    statuses["report"] = "stale"
    state.setdefault("risks", {})["technical"] = assessment(
        "stale", "P12 application 已提交；Safety Event/technical risk 未自动重评",
    )
