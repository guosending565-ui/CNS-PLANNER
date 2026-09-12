"""JSON-safe contracts for reuse-first CNS site-planning proposals."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict


REUSE_TIERS = (
    "existing_cns_facility", "existing_shared_site", "candidate_site",
    "new_build_candidate",
)


class CandidateAction(TypedDict, total=False):
    action_id: str
    action_type: str
    site_id: str | None
    facility_id: str | None
    device_id: str
    subsystem: str
    reuse_class: str
    coordinate: list[float]
    vertical: dict[str, Any]
    source: str
    confirmed: bool
    eligibility: dict[str, Any]


class CandidateImpact(TypedDict, total=False):
    action_id: str
    status: str
    resolved_segment_ids: list[str]
    affected_segment_ids: list[str]
    planning_gap_reduction_m: float
    segment_resolutions: list[dict[str, Any]]
    evidence: list[dict[str, Any]]


def default_site_planning_policy():
    return {
        "status": "pending_confirmation",
        "strategy": "reuse_first_weighted_greedy_set_cover",
        "target_filter": "confirmed_planning_gap_ground_improvable",
        "reuse_tiers": list(REUSE_TIERS),
        "target_weight": "confirmed_planning_gap_length_m",
        "cost_policy": "explicit_confirmed_cost_else_action_count_proxy",
        "allow_arbitrary_new_coordinates": False,
        "source": "project_engineering_default",
        "confirmed": False,
        "parameters": {},
    }


def normalize_site_planning_policy(value):
    if value is None:
        return default_site_planning_policy()
    if not isinstance(value, dict):
        raise ValueError("site_planning_policy 必须是对象")
    result = default_site_planning_policy()
    result.update(deepcopy(value))
    if result.get("strategy") != "reuse_first_weighted_greedy_set_cover":
        raise ValueError("P11 仅支持 reuse_first_weighted_greedy_set_cover")
    if tuple(result.get("reuse_tiers") or ()) != REUSE_TIERS:
        raise ValueError("P11 reuse tier 顺序不可改变")
    if result.get("target_weight") != "confirmed_planning_gap_length_m":
        raise ValueError("P11 仅支持按 confirmed planning-gap length 加权")
    if result.get("allow_arbitrary_new_coordinates") is not False:
        raise ValueError("P11 禁止自动生成任意新坐标")
    if not isinstance(result.get("parameters"), dict):
        raise ValueError("site_planning_policy.parameters 必须是对象")
    result["confirmed"] = result.get("confirmed") is True
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    result["source"] = str(result.get("source") or "未记录")
    return result
