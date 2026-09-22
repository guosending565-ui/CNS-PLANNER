"""P16 JSON-safe corridor-aware site-planning contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from .site_planning import REUSE_TIERS, canonicalize_reuse_tiers


class CorridorTargetVoxel(TypedDict, total=False):
    target_id: str
    route_id: str
    voxel_id: str
    subsystem: str
    required_units: int
    current_units: int
    remaining_units: int
    discretized_volume_proxy_m3: float
    causes: list[str]


class CorridorCandidateImpact(TypedDict, total=False):
    action_id: str
    status: str
    confirmed_requirement_unit_volume_gain: float
    newly_met_confirmed_objectives: int
    service_resolved_volume_proxy_m3: float
    redundancy_progress_volume_proxy_m3: float
    reasons: list[str]
    evidence: list[dict[str, Any]]


def default_corridor_site_planning_policy():
    return {
        "status": "pending_confirmation",
        "strategy": "corridor_reuse_first_cumulative_what_if",
        "target_filter": "p15_confirmed_target_voxels_only",
        "reuse_tiers": list(REUSE_TIERS),
        "benefit_semantics": "confirmed_requirement_unit_volume_gain",
        "cost_policy": "same_confirmed_cost_unit_else_action_count_proxy",
        "stop_policy": "confirmed_objectives_met_else_no_positive_marginal_gain",
        "allow_arbitrary_new_coordinates": False,
        "source": "project_engineering_default",
        "confirmed": False,
        "parameters": {},
    }


def normalize_corridor_site_planning_policy(value=None):
    if value is None:
        return default_corridor_site_planning_policy()
    if not isinstance(value, dict):
        raise ValueError("corridor_site_planning_policy 必须是对象")
    result = default_corridor_site_planning_policy()
    result.update(deepcopy(value))
    if result.get("strategy") != "corridor_reuse_first_cumulative_what_if":
        raise ValueError("P16 仅支持 corridor_reuse_first_cumulative_what_if")
    try:
        # BUG-PERSIST-REUSE-TIER-001：与 P11 复用**同一**迁移规则
        # （``canonicalize_reuse_tiers``）：当前 5 层原样接受，官方旧 4 层自动迁移，
        # 其余缺项/乱序/重复/自定义顺序仍然 raise，不放宽 tier 顺序保护。
        result["reuse_tiers"], _migrated = canonicalize_reuse_tiers(result.get("reuse_tiers"))
    except ValueError as exc:
        raise ValueError("P16 reuse tier 顺序不可改变") from exc
    if result.get("allow_arbitrary_new_coordinates") is not False:
        raise ValueError("P16 禁止自动生成任意新坐标")
    if not isinstance(result.get("parameters"), dict):
        raise ValueError("corridor_site_planning_policy.parameters 必须是对象")
    result["confirmed"] = result.get("confirmed") is True
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    result["source"] = str(result.get("source") or "未记录")
    return result


def empty_cns_corridor_site_plan(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "corridor_reuse_first_site_planner_v2",
        "algorithm_version": "2.0",
        "parameters": {},
        "proposal_only": True,
        "requires_user_confirmation_and_apply": True,
        "input_fingerprint": None,
        "baseline_corridor_fingerprint": None,
        "baseline_corridor_gap_fingerprint": None,
        "target_voxel_count": 0,
        "targets": [],
        "candidate_actions": [],
        "candidate_impacts": [],
        "selected_actions": [],
        "iteration_trace": [],
        "confirmed_requirement_unit_volume_gain": 0.0,
        "before": {}, "after": {},
        "residual_confirmed_targets": [],
        "unknown_evidence_required": [],
        "final_hypothetical_corridor_fingerprint": None,
        "final_hypothetical_corridor_gap_fingerprint": None,
        "not_evaluated": {
            "common_cause": "not_evaluated",
            "shared_power": "not_evaluated",
            "shared_backhaul": "not_evaluated",
            "tower_failure": "not_evaluated",
            "site_failure_propagation": "not_evaluated",
            "risk_or_probability": "not_evaluated",
        },
    }
