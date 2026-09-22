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

#: BUG-PERSIST-REUSE-TIER-001：共塔 tier 引入**之前**写入项目的官方 4 层 reuse tier 顺序。
#: 它只是“可机械迁移的历史格式”的唯一白名单：只有**逐元素完全相等**的旧政策才允许迁移，
#: 任何缺项、乱序、重复或自定义顺序都不在兼容范围内（保护不被放宽）。
LEGACY_REUSE_TIERS_V1 = (
    "existing_cns_facility",
    "existing_shared_site",
    "candidate_site",
    "new_build_candidate",
)

#: 共塔 tier 在旧 4 层顺序中的插入位置（``existing_shared_site`` 之后）。
_TOWER_COLOCATION_INSERT_AFTER = "existing_shared_site"


def canonicalize_reuse_tiers(value):
    """把持久化的 ``reuse_tiers`` 规范化为当前 :data:`REUSE_TIERS`。

    唯一被接受的两种输入：

    * 当前 5 层顺序（``REUSE_TIERS``）→ 原样接受；
    * 官方旧 4 层顺序（``LEGACY_REUSE_TIERS_V1``）→ 自动迁移为当前 5 层，
      在 ``existing_shared_site`` 之后插入 ``tower_colocation_host``。

    其它任何情况（缺项、乱序、重复、自定义顺序、类型错误）仍然 ``raise ValueError``：
    迁移是对**唯一已知历史格式**的确定性重写，不是对 tier 顺序保护的放宽。
    返回 ``(规范化后的 list, 是否发生了 legacy 迁移)``。
    """

    if not isinstance(value, (list, tuple)):
        raise ValueError("复用层级（reuse tier）顺序不可改变")
    tiers = tuple(value)
    if tiers == REUSE_TIERS:
        return list(REUSE_TIERS), False
    if tiers == LEGACY_REUSE_TIERS_V1:
        migrated = []
        for tier in LEGACY_REUSE_TIERS_V1:
            migrated.append(tier)
            if tier == _TOWER_COLOCATION_INSERT_AFTER:
                migrated.append(TOWER_COLOCATION_REUSE_CLASS)
        return migrated, True
    raise ValueError("复用层级（reuse tier）顺序不可改变")


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
    try:
        # BUG-PERSIST-REUSE-TIER-001：官方 legacy 4 层政策自动迁移到当前 5 层；
        # 其余任何缺项/乱序/重复/自定义顺序仍在这里 fail closed。
        result["reuse_tiers"], _migrated = canonicalize_reuse_tiers(result.get("reuse_tiers"))
    except ValueError as exc:
        raise ValueError("P11 reuse tier 顺序不可改变") from exc
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
