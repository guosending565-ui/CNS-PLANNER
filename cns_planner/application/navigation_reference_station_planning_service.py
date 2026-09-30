"""Navigation reference-station planning（P16 专用 planner family，纯函数）。

Round C 冻结的边界：

* 本模块**只**为 ``navigation_reference_station`` 规划 family 生成站址候选，
  **绝不**进入普通 ``candidate_actions()`` 的 site × device 笛卡尔积，
  也**绝不**使用 radius circle planner；
* 规划对象是**地面 GNSS / RTK / PPP-RTK 导航增强设施**；GNSS 星座、INS、视觉导航、
  机载 GNSS 接收机一律不在范围内；
* **不虚构设备**：可规划单元是
  :data:`cns_planner.domain.navigation_augmentation.REFERENCE_STATION_PLANNING_UNIT`
  （工程规划单元），``device_id`` 可以为 ``None``，
  ``equipment_selection_status = not_selected``；
* 站址必须显式具备 ``navigation_site_suitability``（``confirmed`` 且
  ``planning_use_confirmed``）。普通通信铁塔、共塔候选、``available_subsystems``
  含 ``N`` 都**不**构成合格站址；
* 收益口径只来自上游 what-if 重算：**已确认的 reference-station 站址缺口**改善，
  correction delivery 缺口既不产生也不扣减导航站收益。

本模块本身不执行 what-if（那是应用层 P16 的职责），只负责"哪些站址可以成为
confirmed 候选 / 哪些必须停在 evidence_required"，以及复用优先 tier 的保持。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.cns_service_registry import planner_family_for
from ..domain.navigation_augmentation import (
    EQUIPMENT_SELECTION_STATUS,
    PLANNING_UNIT_MATURITY,
    REFERENCE_STATION_PLANNING_UNIT,
    SERVICE_KEY,
    navigation_facilities,
)
from ..domain.site_planning import (
    REUSE_TIERS, TOWER_COLOCATION_REUSE_CLASS,
)


PLANNER_FAMILY = planner_family_for(SERVICE_KEY)
ACTION_TYPE_ADD_REFERENCE_STATION = "add_navigation_reference_station"

#: 复用优先 tier（与项目既有工程思想一致），但塔类站点必须先有 suitability 确认。
REUSE_CLASS_BY_ORIGIN = {
    "existing_cns_facility": "existing_cns_facility",
    "existing_shared_site": "existing_shared_site",
    "tower_colocation_host": TOWER_COLOCATION_REUSE_CLASS,
    "candidate_site": "candidate_site",
    "new_build_candidate": "new_build_candidate",
}


def navigation_targets(targets):
    """只筛出**真正需要建导航站**的 ``N:rtk_augmentation`` target。

    ``dependency_only``（站址几何已 satisfied、只有 correction delivery 缺口）与
    ``target_scope != "service"`` 的条目一律排除：那类 voxel 的建设动作属于
    ``C:communication`` planner，导航侧只保留依赖记录。
    """

    return [
        item for item in targets or []
        if str(item.get("service_key") or "") == SERVICE_KEY
        and str(item.get("planner_family") or PLANNER_FAMILY) == PLANNER_FAMILY
        and bool(item.get("target_id"))
        and item.get("target_scope") == "service"
        and item.get("dependency_only") is not True
    ]


def navigation_reference_station_candidate_actions(
    targets, *, existing_facilities=None, candidate_sites=None, tower_colocation=None,
    navigation_evidence=None,
):
    """生成 confirmed 站址的建站候选 action（未确认 suitability 的站绝不进入）。

    站址来源与复用优先顺序（与项目既有 reuse-first 思想一致）：

    1. 已有 CNS 设施中显式确认的 navigation 站址（``existing_cns_facility``）；
    2. 已确认 suitability 的共塔宿主（``tower_colocation_host``）；
    3. 可复用的候选站址（``candidate_site``）。

    **不生成任意新坐标**：没有显式来源就没有 action。
    """

    selected_targets = navigation_targets(targets)
    if not selected_targets:
        return []
    sites = _eligible_sites(navigation_evidence=navigation_evidence)
    actions = []
    for site in sites:
        for target in selected_targets:
            actions.append(_action(site, target))
    return sorted(actions, key=lambda item: (item["reuse_class"], item["action_id"]))


def navigation_evidence_required(sites_evidence):
    """未确认 suitability 的站址 → P16 必须如实返回 ``evidence_required`` 的证据。"""

    result = []
    for provider in (sites_evidence or {}).get("unconfirmed_providers") or []:
        suitability = provider.get("site_suitability") or {}
        missing = [
            name for name in ("confirmed", "planning_use_confirmed")
            if suitability.get(name) is not True
        ]
        result.append({
            "kind": "navigation_site_suitability",
            "service_key": SERVICE_KEY,
            "distinct_site_id": provider.get("distinct_site_id"),
            "site_id": provider.get("site_id"),
            "planning_origin": provider.get("planning_origin"),
            "missing_confirmation": missing,
            "reason": (
                "站址的 navigation_site_suitability 未同时确认 confirmed 与 "
                "planning_use_confirmed：不得作为 confirmed eligible 导航站候选"
            ),
        })
    return result


def hypothetical_navigation_sites(evidence, actions):
    """把已选择的导航站 action 变成 **caller-owned** 的假想站址（不写正式 ExistingCNS）。

    返回 ``(existing_facilities, navigation_evidence)``：前者用于 P14 的几何 provider
    重建（保持物理站址身份），后者是重算后的 navigation augmentation 证据。
    """

    working = deepcopy(evidence or {})
    providers = [deepcopy(item) for item in working.get("providers") or []]
    seen = {str(item.get("distinct_site_id") or "") for item in providers}
    added = set()
    for action in actions or []:
        if str(action.get("service_key") or "") != SERVICE_KEY:
            continue
        key = str(action.get("distinct_site_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        added.add(key)
        providers.append({
            "provider_id": action.get("action_id"),
            "service_key": SERVICE_KEY,
            "subsystem": "N",
            "site_id": action.get("site_id"),
            "facility_id": action.get("facility_id"),
            "distinct_site_id": action.get("distinct_site_id"),
            "coordinate": deepcopy(action.get("coordinate")),
            "status": "confirmed",
            "source": action.get("source"),
            "planning_origin": action.get("reuse_class"),
            "site_suitability": deepcopy(action.get("site_suitability")),
            "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
            "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
            "maturity": PLANNING_UNIT_MATURITY,
            "hypothetical_action_id": action.get("action_id"),
            "persisted_as_upstream": False,
        })
    working["providers"] = sorted(providers, key=lambda item: str(item.get("provider_id")))
    working["provider_count"] = len(working["providers"])
    working["distinct_site_count"] = len(working["providers"])
    # 已进入假想 provider 池的站址不再同时留在"建站候选"里，避免同一站址被重复规划。
    working["planning_candidates"] = [
        item for item in working.get("planning_candidates") or []
        if str(item.get("distinct_site_id") or "") not in added
    ]
    facilities = navigation_facilities(working)
    return facilities, working


def _eligible_sites(*, navigation_evidence=None):
    """P16 建站候选：**只**取 suitability 已确认但**尚未建成**的站址。

    候选来源（与项目既有复用优先顺序一致）：

    1. 已有 CNS 设施中显式确认的 navigation 站址（``existing_cns_facility``）；
    2. 已确认 suitability 的共塔宿主（``tower_colocation_host``）；
    3. 可复用的候选站址（``candidate_site``）。

    **不生成任意新坐标**：没有显式来源就没有 action。已经建成（即已在基线 provider
    池中服务）的站址不产生"新增"动作。
    """

    result = {}
    for candidate in (navigation_evidence or {}).get("planning_candidates") or []:
        distinct_site_id = str(candidate.get("distinct_site_id") or "")
        if not distinct_site_id:
            continue
        suitability = candidate.get("site_suitability") or {}
        if suitability.get("eligible_for_navigation_reference_station") is not True:
            continue
        coordinate = candidate.get("coordinate")
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            continue
        reuse_class = _reuse_class(candidate.get("planning_origin"))
        current = result.get(distinct_site_id)
        if current is None or _tier_index(reuse_class) < _tier_index(current["reuse_class"]):
            result[distinct_site_id] = {
                "distinct_site_id": distinct_site_id,
                "site_id": candidate.get("site_id"),
                "facility_id": candidate.get("facility_id"),
                "coordinate": [float(coordinate[0]), float(coordinate[1])],
                "reuse_class": reuse_class,
                "site_suitability": deepcopy(suitability),
                "source": candidate.get("source") or "navigation_site_suitability",
                "planning_profile": {},
                "vertical": {},
                "host": None,
                "planning_origin": deepcopy(candidate.get("planning_origin")),
            }
    return sorted(result.values(), key=lambda item: (_tier_index(item["reuse_class"]), item["distinct_site_id"]))


def _reuse_class(origin):
    declared = str(origin or "").strip()
    if declared in REUSE_TIERS:
        return declared
    return REUSE_CLASS_BY_ORIGIN.get(declared, "new_build_candidate")


def _tier_index(reuse_class):
    try:
        return REUSE_TIERS.index(reuse_class)
    except ValueError:
        return len(REUSE_TIERS)


def _action(site, target):
    return {
        "action_id": f"navigation-reference-station:{site['distinct_site_id']}",
        "action_type": ACTION_TYPE_ADD_REFERENCE_STATION,
        "route_id": target.get("route_id"),
        "subsystem": "N",
        "service_key": SERVICE_KEY,
        "planner_family": PLANNER_FAMILY,
        "device_id": None,
        "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
        "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
        "maturity": PLANNING_UNIT_MATURITY,
        "not_a_device_model": True,
        "site_id": site.get("site_id"),
        "facility_id": site.get("facility_id"),
        "distinct_site_id": site.get("distinct_site_id"),
        "reuse_class": site.get("reuse_class"),
        "coordinate": deepcopy(site.get("coordinate")),
        "vertical": deepcopy(site.get("vertical")),
        "host": deepcopy(site.get("host")),
        "planning_origin": deepcopy(site.get("planning_origin")),
        "planning_profile": deepcopy(site.get("planning_profile")),
        "site_suitability": deepcopy(site.get("site_suitability")),
        "source": site.get("source"),
        "confirmed": True,
        "persisted_as_upstream": False,
        "eligibility": {"status": "eligible", "reasons": []},
        "target_id": target.get("target_id"),
        "target_scope": target.get("target_scope"),
        "benefit_semantics": "confirmed_reference_station_deficit_gain",
        "delivery_semantics": (
            "correction_delivery_deficit 不由导航站解决；该 voxel 的 "
            "N:rtk_augmentation 仍需 C:communication rerun 满足"
        ),
    }


__all__ = [
    "ACTION_TYPE_ADD_REFERENCE_STATION", "PLANNER_FAMILY", "REUSE_CLASS_BY_ORIGIN",
    "hypothetical_navigation_sites", "navigation_evidence_required",
    "navigation_reference_station_candidate_actions", "navigation_targets",
]
