"""JSON-safe contracts and objective comparison for controlled CNS plan review."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, TypedDict


class PlanVariant(TypedDict, total=False):
    variant_id: str
    name: str
    source: str
    selected_action_ids: list[str]
    source_p16_fingerprint: str | None
    baseline_fingerprint: str
    notes: str
    evaluation: dict[str, Any]
    status: str


def fingerprint(value):
    return sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def review_baseline_fingerprint(state):
    selections = state.get("algorithm_selection") or {}
    return fingerprint({
        "required_cns": state.get("required_cns") or {},
        "aircraft": {
            "selected_id": state.get("selected_aircraft_profile_id"),
            "profiles": state.get("aircraft_profiles") or {},
        },
        "operational_routes": state.get("operational_routes") or [],
        "existing_cns_facilities": state.get("existing_cns_facilities") or {},
        "candidate_sites": state.get("candidate_sites") or {},
        "device_catalog": state.get("device_catalog") or {},
        "spatial_3d": state.get("spatial_3d") or {},
        "grid": state.get("grid") or {},
        "terrain": ((state.get("grid_attributes") or {}).get("terrain") or {}),
        "cns_corridor_policy": state.get("cns_corridor_policy") or {},
        "cns_planning_objectives": state.get("cns_planning_objectives") or {},
        "operational_timing": state.get("operational_timing") or {},
        "protection_envelope": _result_identity(state.get("protection_envelope") or {}),
        "p7_p10": {
            name: _result_identity(state.get(name) or {})
            for name in ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_analysis_v2")
        },
        "p14": _result_identity(state.get("cns_corridor_assessment") or {}),
        "p15": _result_identity(state.get("cns_corridor_gap_assessment") or {}),
        "p16": {
            **_result_identity(state.get("cns_corridor_site_plan") or {}),
            "status": (state.get("cns_corridor_site_plan") or {}).get("status"),
            "candidate_actions": (state.get("cns_corridor_site_plan") or {}).get("candidate_actions") or [],
            "selected_action_ids": sorted(str(item.get("action_id")) for item in (state.get("cns_corridor_site_plan") or {}).get("selected_actions") or []),
        },
        "algorithm_selection": {
            name: selections.get(name)
            for name in ("coverage_model", "service_model", "timeline_model", "cns_gap_analyzer", "corridor_model", "corridor_gap_analyzer", "site_planner")
        },
    })


def review_input_fingerprints(state):
    return {
        "baseline": review_baseline_fingerprint(state),
        "required_cns": fingerprint(state.get("required_cns") or {}),
        "routes": fingerprint(state.get("operational_routes") or []),
        "existing_cns": fingerprint(state.get("existing_cns_facilities") or {}),
        "candidate_sites": fingerprint(state.get("candidate_sites") or {}),
        "device_catalog": fingerprint(state.get("device_catalog") or {}),
        "p14": fingerprint(_result_identity(state.get("cns_corridor_assessment") or {})),
        "p15": fingerprint(_result_identity(state.get("cns_corridor_gap_assessment") or {})),
        "p16": fingerprint({
            **_result_identity(state.get("cns_corridor_site_plan") or {}),
            "status": (state.get("cns_corridor_site_plan") or {}).get("status"),
            "candidate_actions": (state.get("cns_corridor_site_plan") or {}).get("candidate_actions") or [],
            "selected_actions": (state.get("cns_corridor_site_plan") or {}).get("selected_actions") or [],
        }),
    }


def variant_id(baseline_fingerprint, action_ids):
    identity = fingerprint([str(baseline_fingerprint), sorted(set(str(item) for item in action_ids))])
    return f"PV-{identity[:20]}"


def make_variant(baseline_fingerprint, action_ids, source, name, p16_fingerprint, notes=""):
    action_ids = sorted(set(str(item) for item in action_ids if str(item)))
    return {
        "variant_id": variant_id(baseline_fingerprint, action_ids),
        "name": str(name or source), "source": source,
        "selected_action_ids": action_ids,
        "source_p16_fingerprint": p16_fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "notes": str(notes or ""), "status": "not_evaluated", "evaluation": None,
    }


def empty_plan_review(status="not_initialized"):
    return {
        "status": status, "model_scope": "human_reviewed_plan_decision",
        "automatic_overall_score": None, "automatic_rank": None,
        "review_id": None, "baseline_fingerprint": None,
        "input_fingerprints": {}, "selected_variant_id": None,
        "variants": [], "initialized_from": {}, "reasons": [],
    }


def empty_confirmed_plan(status="not_confirmed"):
    return {
        "status": status, "plan_id": None, "variant_id": None,
        "decision": None, "variant": None, "application": None,
        "current_applicability": "not_evaluated", "history": [],
    }


def comparison_matrix(assessment):
    rows = []
    for route in (assessment or {}).get("routes") or []:
        for item in route.get("subsystems") or []:
            rows.append({
                "route_id": route.get("route_id"), "subsystem": item.get("subsystem"),
                "status": item.get("status"),
                "required_volume_proxy_m3": item.get("required_volume_proxy_m3"),
                "service": _distribution(item.get("service")),
                "combined": _distribution(item.get("combined")),
                "redundancy": _distribution(item.get("redundancy")),
                "total_confirmed_deficit_projection_m": item.get("total_confirmed_deficit_projection_m"),
                "max_continuous_deficit_projection_m": item.get("max_continuous_deficit_projection_m"),
                "objective_status": item.get("objective_status"),
                "objective_results": deepcopy(item.get("objective_results") or []),
                "residual_target_voxel_ids": deepcopy(item.get("confirmed_target_voxel_ids") or []),
                "unknown_voxel_ids": deepcopy(item.get("unknown_voxel_ids") or []),
            })
    return rows


def confirmation_gate(baseline, planned):
    left, right = _voxel_index(baseline), _voxel_index(planned)
    regressions, unknown_regressions = [], []
    for key, before in left.items():
        if before.get("combined_status") != "satisfied":
            continue
        after = right.get(key) or {}
        if after.get("combined_status") == "confirmed_gap":
            regressions.append(key)
        elif after.get("combined_status") != "satisfied":
            unknown_regressions.append(key)
    rows = comparison_matrix(planned)
    confirmed_objectives = [
        objective for row in rows for objective in row["objective_results"]
        if objective.get("confirmed") is True
    ]
    objective_failures = [item for item in confirmed_objectives if item.get("status") == "not_met"]
    objective_unknown = [item for item in confirmed_objectives if item.get("status") == "unknown"]
    unknown_voxels = sorted({voxel for row in rows for voxel in row["unknown_voxel_ids"]})
    if regressions:
        status = "regression"
    elif unknown_regressions or unknown_voxels or objective_unknown:
        status = "evidence_required"
    elif objective_failures:
        status = "objectives_not_met"
    elif not confirmed_objectives:
        status = "objectives_not_configured"
    else:
        status = "ready_for_confirmation"
    return {
        "status": status, "confirmation_allowed": status == "ready_for_confirmation",
        "regressions": regressions, "unknown_regressions": unknown_regressions,
        "unknown_voxel_ids": unknown_voxels,
        "objective_failures": objective_failures, "objective_unknown": objective_unknown,
        "confirmed_objective_count": len(confirmed_objectives),
        "requires_confirm_without_objectives_acknowledgement": status == "objectives_not_configured",
    }


def action_summary(actions):
    reuse_counts, costs = {}, {}
    for action in actions:
        reuse_class = str(action.get("reuse_class") or "unknown")
        reuse_counts[reuse_class] = reuse_counts.get(reuse_class, 0) + 1
        profile = action.get("planning_profile") or {}
        cost = action.get("explicit_cost", action.get("planning_cost", profile.get("planning_cost")))
        unit = action.get("cost_unit", profile.get("cost_unit"))
        if cost is not None and unit:
            costs[str(unit)] = costs.get(str(unit), 0.0) + float(cost)
    return {
        "selected_count": len(actions), "reuse_class_counts": dict(sorted(reuse_counts.items())),
        "explicit_costs_by_unit": dict(sorted(costs.items())), "cross_unit_total": None,
        "cost_semantics": "explicit_costs_grouped_by_unit_no_cross_unit_total",
    }


def _distribution(value):
    value = value or {}
    return {
        "voxel_counts": deepcopy(value.get("voxel_counts") or {}),
        "volume_proxy_m3": deepcopy(value.get("volume_proxy_m3") or {}),
        "fractions": deepcopy(value.get("fractions") or {}),
    }


def _voxel_index(assessment):
    return {
        f"{route.get('route_id')}|{voxel.get('voxel_id')}|{item.get('subsystem')}": item
        for route in (assessment or {}).get("routes") or []
        for voxel in route.get("voxels") or [] for item in voxel.get("subsystems") or []
    }


def _result_identity(value):
    return {
        "status": value.get("status"), "algorithm_id": value.get("algorithm_id"),
        "algorithm_version": value.get("algorithm_version"),
        "input_fingerprint": value.get("input_fingerprint"),
        "parameters": value.get("parameters") or {},
    }
