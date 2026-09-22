"""JSON-safe contracts for reuse-first CNS site-planning proposals."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict


REUSE_TIERS = (
    "existing_cns_facility", "existing_shared_site", "tower_colocation_host",
    "candidate_site", "new_build_candidate",
)

#: 真实铁塔共塔宿主（Preferred Host Site）的 reuse_class。
#: 语义是 **prefer** 共塔，不是 force：该 tier 排在普通 candidate_site / new_build 之前，
#: 但没有合适铁塔时后面两个 tier 照常补盲。
TOWER_COLOCATION_REUSE_CLASS = "tower_colocation_host"


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
    planning_profile: dict[str, Any]
    #: 共塔候选的宿主溯源（``host_type`` / ``host_tower_id`` / ...）；其它站点为 None。
    host: dict[str, Any] | None
    planning_origin: dict[str, Any] | None
    #: 规划层宿主状态（``eligible`` / ``not_confirmed``）；非共塔站点为 None。
    planning_host_status: str | None
    #: 分系统物理安装证据状态（``unverified`` / ``declared_compatible`` /
    #: ``declared_not_compatible``）。``unverified`` 表示**没有证据**，不是"都能装"。
    subsystem_mount_status: str | None
    #: 物理安装是否已确认；共塔候选恒为 ``False``（除非有逐塔现场调查数据）。
    physical_mount_confirmed: bool | None
    #: 是否需要现场勘察；共塔候选恒为 ``True``。
    requires_site_survey: bool | None
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
