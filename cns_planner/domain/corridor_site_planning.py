"""P16 JSON-safe corridor-aware site-planning contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from .site_planning import REUSE_TIERS, canonicalize_reuse_tiers


class CorridorTargetVoxel(TypedDict, total=False):
    """P16 target 契约（legacy subsystem 口径 + Round 2.1 service 口径）。

    * legacy：``route_id|voxel_id|subsystem``，读 ``required_units`` /
      ``current_units``（= ``confirmed_independent_provider_count`` 截断）；
    * service-aware：``route_id|voxel_id|service_key``，``required_units`` /
      ``current_units`` 一律来自 ``required_distinct_site_count`` /
      ``distinct_site_count``（物理站址计数），并显式给出 ``counting_basis``。
    """

    target_id: str
    route_id: str
    voxel_id: str
    subsystem: str
    service_key: str
    target_scope: str
    surface_class: str
    counting_basis: str
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


#: P16 target 的两个作用域（Round 2.1）。
#:
#: * ``subsystem`` —— legacy 口径：``route_id|voxel_id|subsystem``，读 subsystem entry 的
#:   ``required_redundancy`` / ``qualified_provider_count`` /
#:   ``confirmed_independent_provider_count``；
#: * ``service``   —— surface-dependent service 口径：``route_id|voxel_id|service_key``，
#:   读该 service 自己的 ``required_distinct_site_count`` / ``distinct_site_count``。
#:
#: legacy target 与 service target **绝不共用 target_id**，因此两条路径互不覆盖。
TARGET_SCOPE_SUBSYSTEM = "subsystem"
TARGET_SCOPE_SERVICE = "service"

#: service 池状态 → legacy 组合状态（P16 what-if 的 regression / 判定复用同一套词汇）。
SERVICE_STATUS_TO_COMBINED_STATUS = {
    "satisfied": "satisfied",
    "confirmed_deficit": "confirmed_gap",
    "unknown": "unknown",
    "not_applicable": "not_applicable",
}


def corridor_target_id(route_id, voxel_id, key):
    """P16 target 的**稳定且唯一**标识：``route_id|voxel_id|key``。

    ``key`` 是 subsystem 码（legacy）或 canonical ``service_key``（service-aware），
    两者不会冲突（service_key 形如 ``C:communication`` / ``S:rid_cooperative``）。
    """

    return f"{route_id}|{voxel_id}|{key}"


def service_entry_view(service):
    """把一个 P15 ``entry.services[]`` 条目映射为 P16 可消费的稳定视图。

    只做**字段改名与状态映射**，不引入任何新口径：

    * ``combined_status`` ← ``SERVICE_STATUS_TO_COMBINED_STATUS[status]``（regression 复用）；
    * ``service_status`` / ``redundancy_status`` ← service 自己的 ``status``；
    * ``entry_scope = "service"`` 让计数口径明确走 ``distinct_site_count``。
    """

    if not isinstance(service, dict):
        return {}
    status = str(service.get("status") or "unknown")
    return {
        **service,
        "entry_scope": TARGET_SCOPE_SERVICE,
        "service_status": status,
        "redundancy_status": status,
        "combined_status": SERVICE_STATUS_TO_COMBINED_STATUS.get(status, "unknown"),
    }


def corridor_voxel_entry_index(assessment):
    """P15 assessment → ``{target_id: entry}``，同时索引 subsystem 与 service 两类 target。

    legacy key 为 subsystem entry 本体（字段不变），service key 为
    :func:`service_entry_view` 的映射视图。P16 的 ``_impact`` 与 planner 的
    residual 判定因此消费**同一份**索引，两条路径的 target_id 契约不会漂移。
    """

    result = {}
    for route in (assessment or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for voxel in route.get("voxels") or []:
            voxel_id = str(voxel.get("voxel_id") or "")
            for entry in voxel.get("subsystems") or []:
                if not isinstance(entry, dict):
                    continue
                result[corridor_target_id(route_id, voxel_id, entry.get("subsystem"))] = entry
                for service in entry.get("services") or []:
                    if not isinstance(service, dict):
                        continue
                    key = str(service.get("service_key") or "")
                    if not key:
                        continue
                    result[corridor_target_id(route_id, voxel_id, key)] = service_entry_view(service)
    return result


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
