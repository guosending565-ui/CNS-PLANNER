"""JSON-safe contracts and comparison helpers for P12 closed-loop verification."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, TypedDict


class PlanApplication(TypedDict, total=False):
    application_id: str
    site_plan_fingerprint: str
    baseline_fingerprint: str
    selected_action_ids: list[str]
    applied_refs: list[dict[str, Any]]
    provenance: dict[str, Any]


class ClosedLoopAssessment(TypedDict, total=False):
    status: str
    validation_status: str
    commit_status: str
    model_scope: str
    assessment_fingerprint: str | None
    application: PlanApplication | None
    algorithm_runs: dict[str, Any]
    comparisons: list[dict[str, Any]]
    prediction_comparison: dict[str, Any]
    residual_gap_segments: list[dict[str, Any]]
    regression_segments: list[dict[str, Any]]
    selected_action_summary: dict[str, Any]
    reasons: list[str]


VALIDATION_STATUSES = (
    "validated_improvement", "no_material_improvement", "regression",
    "inconclusive", "not_applicable",
)

METRIC_FIELDS = (
    "planning_confirmed_gap_length_m",
    "combined_gap_length_m",
    "unknown_length_m",
    "contingency_exposure_length_m",
    "contingency_exposure_duration_s",
    "runtime_lost_length_m",
    "runtime_lost_duration_s",
    "max_continuous_gap_length_m",
    "max_continuous_gap_duration_s",
    "gap_segment_count",
)


def stable_fingerprint(value) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def empty_closed_loop_assessment(status="not_calculated"):
    return {
        "status": status,
        "validation_status": "not_applicable",
        "commit_status": "not_committed",
        "model_scope": "engineering_closed_loop_verification",
        "real_cns_model_validation": "not_evaluated",
        "certification_conclusion": "not_evaluated",
        "assessment_fingerprint": None,
        "application": None,
        "algorithm_runs": {"baseline": {}, "planned": {}},
        "comparisons": [],
        "prediction_comparison": {
            "predicted_planning_gap_reduction_m": 0.0,
            "actual_planning_gap_reduction_m": 0.0,
            "prediction_error_m": 0.0,
            "realized_fraction": None,
        },
        "residual_gap_segments": [],
        "regression_segments": [],
        "selected_action_summary": {},
        "reasons": [],
    }


def current_baseline_fingerprint(state) -> str:
    """Fingerprint only inputs and exact implementations used by the P12 rerun."""
    selections = state.get("algorithm_selection") or {}
    gap_parameters = (state.get("cns_gap_analysis_v2") or {}).get("parameters") or {}
    return stable_fingerprint({
        "operational_routes": state.get("operational_routes") or [],
        "spatial_3d": state.get("spatial_3d") or {},
        "grid": state.get("grid") or {},
        "terrain": ((state.get("grid_attributes") or {}).get("terrain") or {}),
        "required_cns": state.get("required_cns") or {},
        "aircraft_profiles": state.get("aircraft_profiles") or {},
        "selected_aircraft_profile_id": state.get("selected_aircraft_profile_id"),
        "existing_cns_facilities": state.get("existing_cns_facilities") or {},
        "candidate_sites": state.get("candidate_sites") or {},
        "device_catalog": state.get("device_catalog") or {},
        "operational_timing": state.get("operational_timing") or {},
        "protection_envelope": (
            state.get("protection_envelope") or {}
            if gap_parameters.get("evaluate_protection_margin") is True else None
        ),
        "algorithm_selection": {
            name: selections.get(name)
            for name in ("coverage_model", "service_model", "timeline_model")
        },
        "effective_parameters": {
            "coverage_3d": (state.get("coverage_3d") or {}).get("parameters") or {},
            "cns_service_capability": (state.get("cns_service_capability") or {}).get("parameters") or {},
            "service_timeline": (state.get("service_timeline") or {}).get("parameters") or {},
            "cns_gap_analysis_v2": gap_parameters,
        },
        "site_plan": {
            "status": (state.get("cns_site_plan") or {}).get("status"),
            "input_fingerprint": (state.get("cns_site_plan") or {}).get("input_fingerprint"),
            "selected_actions": (state.get("cns_site_plan") or {}).get("selected_actions") or [],
        },
    })


def build_plan_application(site_plan, baseline_fingerprint):
    actions = list((site_plan or {}).get("selected_actions") or [])
    action_ids = [str(item.get("action_id") or "") for item in actions]
    site_plan_fingerprint = (site_plan or {}).get("input_fingerprint")
    identity = stable_fingerprint([
        site_plan_fingerprint, baseline_fingerprint, action_ids,
    ])
    return {
        "application_id": f"P12-{identity[:20]}",
        "site_plan_fingerprint": site_plan_fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "selected_action_ids": action_ids,
        "applied_refs": [],
        "provenance": {
            "source": "P11 validated proposal",
            "planning_stage": "P12 closed-loop application",
            "deterministic_identity": True,
        },
    }


def algorithm_run_summary(state):
    return {
        name: {
            "algorithm_id": (state.get(name) or {}).get("algorithm_id"),
            "algorithm_version": (state.get(name) or {}).get("algorithm_version"),
            "input_fingerprint": (state.get(name) or {}).get("input_fingerprint"),
            "status": (state.get(name) or {}).get("status"),
        }
        for name in (
            "coverage_3d", "cns_service_capability", "service_timeline",
            "cns_gap_analysis_v2",
        )
    }


def compare_gap_results(baseline_gap, planned_gap, predicted_reduction_m, selected_actions):
    before = _subsystem_index(baseline_gap)
    after = _subsystem_index(planned_gap)
    keys = sorted(set(before) | set(after))
    comparisons = []
    regression_segments = []
    evidence_degradation = []
    confirmed_resolved = 0.0
    new_confirmed_gap = 0.0
    applicable = False

    for route_id, subsystem in keys:
        left = before.get((route_id, subsystem)) or {}
        right = after.get((route_id, subsystem)) or {}
        before_metrics = _metrics(left)
        after_metrics = _metrics(right)
        delta = {
            name: _difference(after_metrics.get(name), before_metrics.get(name))
            for name in METRIC_FIELDS
        }
        comparisons.append({
            "route_id": route_id, "subsystem": subsystem,
            "before": before_metrics, "after": after_metrics, "delta": delta,
        })
        if left.get("required") is True or right.get("required") is True:
            applicable = True
        transitions = _interval_transitions(route_id, subsystem, left, right)
        for item in transitions:
            length = item["length_m"]
            if item["before_planning"] == "confirmed_gap" and item["after_planning"] == "satisfied":
                confirmed_resolved += length
            if item["before_planning"] != "confirmed_gap" and item["after_planning"] == "confirmed_gap":
                new_confirmed_gap += length
            regressed = (
                item["before_planning"] == "satisfied" and item["after_planning"] == "confirmed_gap"
            ) or (
                item["before_combined"] in ("satisfied", "satisfied_by_contingency")
                and item["after_combined"] == "confirmed_gap"
            )
            if regressed:
                regression_segments.append(item)
            if item["before_planning"] == "confirmed_gap" and item["after_planning"] == "unknown":
                evidence_degradation.append(item)

    before_total = sum(item["before"]["planning_confirmed_gap_length_m"] for item in comparisons)
    after_total = sum(item["after"]["planning_confirmed_gap_length_m"] for item in comparisons)
    actual = confirmed_resolved - new_confirmed_gap
    predicted = float(predicted_reduction_m or 0.0)
    prediction = {
        "predicted_planning_gap_reduction_m": predicted,
        "actual_planning_gap_reduction_m": actual,
        "prediction_error_m": actual - predicted,
        "realized_fraction": actual / predicted if predicted > 0 else None,
        "confirmed_resolved_length_m": confirmed_resolved,
        "new_confirmed_planning_gap_length_m": new_confirmed_gap,
        "gap_to_unknown_length_m": sum(item["length_m"] for item in evidence_degradation),
    }
    reasons = []
    if not selected_actions or not applicable:
        validation = "not_applicable"
        reasons.append("没有可应用的 selected action 或 RequiredCNS 适用区间")
    elif regression_segments or after_total > before_total + 1e-7:
        validation = "regression"
        reasons.append("出现原 satisfied 区间转 confirmed_gap 或 planning gap 总量增加")
    elif evidence_degradation:
        validation = "inconclusive"
        reasons.append("存在 confirmed planning gap 转为 unknown；不得计为 resolved")
    elif actual > 1e-7:
        validation = "validated_improvement"
        reasons.append("confirmed planning gap 经同链路重跑后实际下降，且未发现 confirmed regression")
    else:
        uncertain = any(
            item["after"]["unknown_length_m"] > 1e-7
            for item in comparisons
        )
        validation = "inconclusive" if uncertain else "no_material_improvement"
        reasons.append(
            "planned 关键证据 unknown/missing，无法确认改善"
            if uncertain else "同链路重跑未产生可确认的 planning-gap 改善"
        )
    residual = [
        {"route_id": route_id, "subsystem": subsystem, **deepcopy(segment)}
        for (route_id, subsystem), value in after.items()
        for segment in value.get("segments") or []
        if segment.get("planning_status") == "confirmed_gap"
        or segment.get("combined_status") == "confirmed_gap"
    ]
    return {
        "validation_status": validation,
        "comparisons": comparisons,
        "prediction_comparison": prediction,
        "residual_gap_segments": residual,
        "regression_segments": regression_segments,
        "reasons": reasons,
    }


def _subsystem_index(gap):
    result = {}
    for route in (gap or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for subsystem in route.get("subsystems") or []:
            result[(route_id, str(subsystem.get("subsystem") or ""))] = subsystem
    return result


def _metrics(value):
    planning = (value.get("planning_assessment") or {}).get("length_by_status_m") or {}
    return {
        "planning_confirmed_gap_length_m": float(planning.get("confirmed_gap") or 0.0),
        "combined_gap_length_m": float(value.get("gap_length_m") or 0.0),
        "unknown_length_m": float(value.get("unknown_length_m") or 0.0),
        "contingency_exposure_length_m": float(value.get("contingency_exposure_length_m") or 0.0),
        "contingency_exposure_duration_s": _number(value.get("contingency_exposure_duration_s")),
        "runtime_lost_length_m": float(value.get("runtime_lost_length_m") or 0.0),
        "runtime_lost_duration_s": _number(value.get("runtime_lost_duration_s")),
        "max_continuous_gap_length_m": float(value.get("max_continuous_gap_length_m") or 0.0),
        "max_continuous_gap_duration_s": _number(value.get("max_continuous_gap_duration_s")),
        "gap_segment_count": int(value.get("gap_segment_count") or 0),
    }


def _difference(after, before):
    if after is None or before is None:
        return None
    return after - before


def _number(value):
    return None if value is None else float(value)


def _interval_transitions(route_id, subsystem, before, after):
    left = list(before.get("segments") or [])
    right = list(after.get("segments") or [])
    points = sorted({
        float(segment.get(key) or 0.0)
        for segment in [*left, *right]
        for key in ("start_route_offset_m", "end_route_offset_m")
    })
    result = []
    for start, end in zip(points, points[1:]):
        if end <= start + 1e-9:
            continue
        midpoint = (start + end) / 2.0
        left_segment = _segment_at(left, midpoint)
        right_segment = _segment_at(right, midpoint)
        result.append({
            "route_id": route_id, "subsystem": subsystem,
            "start_route_offset_m": start, "end_route_offset_m": end,
            "length_m": end - start,
            "before_segment_id": left_segment.get("segment_id"),
            "after_segment_id": right_segment.get("segment_id"),
            "before_planning": left_segment.get("planning_status", "unknown"),
            "after_planning": right_segment.get("planning_status", "unknown"),
            "before_combined": left_segment.get("combined_status", "unknown"),
            "after_combined": right_segment.get("combined_status", "unknown"),
        })
    return result


def _segment_at(segments, offset):
    for index, segment in enumerate(segments):
        start = float(segment.get("start_route_offset_m") or 0.0)
        end = float(segment.get("end_route_offset_m") or 0.0)
        if start - 1e-9 <= offset < end - 1e-9 or (
            index == len(segments) - 1 and start - 1e-9 <= offset <= end + 1e-9
        ):
            return segment
    return {}
