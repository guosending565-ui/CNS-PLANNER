"""TowerColocationCandidate：把真实铁塔派生为 **CNS 规划宿主候选**。

语义（用户要求，务必不要混淆）：

* 真实铁塔 = **Preferred Host Site / 共塔候选宿主**；
* 设备是否真正安装，仍由 CNS planning / adoption 产生；
* **绝不**因为铁塔存在就自动生成 C 或 N 或 S 的 existing facility；
* **绝不**从铁塔数据推断发射功率、频率、覆盖半径、容量、MTBF、MTTR 或成本 ——
  设备参数只能来自 device catalog / `equipment_reference_catalog` / 用户确认。

派生出的条目复用**既有 CandidateSite contract 形状**（``site_id`` / ``name`` /
``coordinate`` / ``site_type`` / ``usable`` / ``locked`` / ``source`` / ``metadata`` /
``vertical_profile`` / ``planning_profile``），因此：

* 不新建第二套 candidate 契约；
* ``metadata.host`` 承载宿主溯源（``host_tower_id`` / ``planning_origin`` 等），
  ``metadata`` 在 reopen 时被原样保留；
* 规划优先级由 ``planning_profile.reuse_class == "tower_colocation_host"`` 表达，
  由 :data:`cns_planner.domain.site_planning.REUSE_TIERS` 的**词典序**保证
  "共塔优先、普通站 fallback"。
"""

from __future__ import annotations

from copy import deepcopy

from .site_planning import TOWER_COLOCATION_REUSE_CLASS

TOWER_COLOCATION_SCHEMA_VERSION = 1
TOWER_COLOCATION_COLLECTION_ID = "tower_colocation_candidates"
TOWER_COLOCATION_ORIGIN = "tower_colocation"

#: 服务原点假设：只有显式确认后，才允许把"塔顶"当作设备服务原点。
SERVICE_ORIGIN_ASSUMPTIONS = (None, "tower_top_agl_0")

#: 宿主可用性语义：位置可用是源事实，设备能否安装**必须**人工确认。
HOST_AVAILABILITY_DEFAULTS = {
    "site_position_available": True,
    "device_mount_confirmed": False,
}


def default_tower_colocation_policy():
    """共塔优先策略。

    ``confirmed=False`` 时共塔候选只是"已识别但未启用"：它**不会**被选中
    （fail-closed），普通候选站照常工作，因此不存在任何权重或默认值被偷偷设定。
    """

    return {
        "status": "pending_confirmation",
        "strategy": "prefer_tower_colocation_fallback_to_plain_candidates",
        "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
        "host_pool": "real_tower_sites",
        "service_origin_assumption": None,
        "device_mount_confirmed": False,
        "source": "project_engineering_default",
        "confirmed": False,
        # ``enabled`` 是 normalize 计算出的派生标记；默认策略里显式给出，
        # 保证"默认值"与"normalize 结果"形状完全一致（save/reopen 幂等）。
        "enabled": False,
        "limitations_note": (
            "prefer 而不是 force：没有合适铁塔时必须允许新建普通站点"
        ),
    }


def normalize_tower_colocation_policy(value):
    if value is None:
        return default_tower_colocation_policy()
    if not isinstance(value, dict):
        raise ValueError("tower_colocation_policy 必须是对象")
    result = default_tower_colocation_policy()
    result.update(deepcopy(value))
    if result.get("strategy") != "prefer_tower_colocation_fallback_to_plain_candidates":
        raise ValueError("共塔策略只支持 prefer_tower_colocation_fallback_to_plain_candidates")
    if result.get("reuse_class") != TOWER_COLOCATION_REUSE_CLASS:
        raise ValueError(f"共塔宿主 reuse_class 固定为 {TOWER_COLOCATION_REUSE_CLASS}")
    assumption = result.get("service_origin_assumption")
    if assumption not in SERVICE_ORIGIN_ASSUMPTIONS:
        raise ValueError("tower_colocation_policy.service_origin_assumption 无效")
    result["service_origin_assumption"] = assumption
    result["device_mount_confirmed"] = result.get("device_mount_confirmed") is True
    result["confirmed"] = result.get("confirmed") is True
    enabled = (
        result["confirmed"]
        and result["device_mount_confirmed"]
        and assumption is not None
    )
    result["status"] = "confirmed" if enabled else "pending_confirmation"
    result["enabled"] = enabled
    result["source"] = str(result.get("source") or "未记录")
    return result


def empty_tower_colocation_candidates(status="not_calculated"):
    return {
        "status": status,
        "collection_id": TOWER_COLOCATION_COLLECTION_ID,
        "schema_version": TOWER_COLOCATION_SCHEMA_VERSION,
        "semantics": "preferred_host_site_candidates_derived_from_real_towers",
        "not_a_cns_facility": True,
        "creates_existing_facilities": False,
        "invents_device_parameters": False,
        "count": 0,
        "items": [],
        "policy": normalize_tower_colocation_policy(None),
        "source": None,
        "warnings": [],
    }


def tower_colocation_candidate(tower, *, obstacle_profile=None, policy=None):
    """把一个真实铁塔派生为一条共塔候选（CandidateSite 形状）。

    ``obstacle_profile`` 是同一塔的 :mod:`cns_planner.domain.tower_obstacle` 结果；
    只有它 ``resolved`` 且策略显式确认"塔顶作为服务原点"时，
    ``vertical_profile.confirmed`` 才为 True。否则保持 pending，规划侧按 unknown 处理。
    """

    if not isinstance(tower, dict):
        raise ValueError("铁塔条目必须是对象")
    tower_id = str(tower.get("tower_id") or "")
    if not tower_id:
        raise ValueError("共塔候选需要 tower_id")
    longitude = tower.get("longitude")
    latitude = tower.get("latitude")
    if not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float)):
        raise ValueError("共塔候选需要真实经纬度")

    normalized = normalize_tower_colocation_policy(policy)
    profile = obstacle_profile if isinstance(obstacle_profile, dict) else {}
    top = profile.get("tower_top_orthometric_m")
    resolved = profile.get("status") == "resolved" and isinstance(top, (int, float))
    assumption = normalized.get("service_origin_assumption")
    origin_confirmed = bool(resolved and normalized["enabled"] and assumption == "tower_top_agl_0")

    limitations = [
        "共塔候选只是宿主候选，不代表铁塔上已有或可以安装任何 CNS 设备",
        "设备型号、挂高与性能参数必须来自 device catalog / 用户确认，绝不从铁塔数据推断",
        "源数据 elevation_m 的垂直基准未确认，只作留档，不作为 EGM2008 正高",
    ]
    if not resolved:
        limitations.append("塔顶 EGM2008 正高未解析：service origin 保持未确认（fail-closed）")
    elif not normalized["enabled"]:
        limitations.append("共塔布设策略尚未确认：候选不会被选中，普通候选站照常可用")
    elif assumption != "tower_top_agl_0":
        limitations.append("未确认任何服务原点假设：不把塔顶当作设备服务原点")

    site_id = f"tower-colocation:{tower_id}"
    vertical_profile = {
        "surface_elevation_m": top if resolved else None,
        "surface_vertical_reference": "egm2008_orthometric",
        "mount_height_agl_m": 0.0 if (resolved and assumption == "tower_top_agl_0") else None,
        "service_origin_egm2008_m": top if origin_confirmed else None,
        "legacy_elevation_m": tower.get("elevation_m"),
        "source": "tower_colocation:tower_top_egm2008_from_fabdem_dtm_plus_structure_height",
        "confirmed": origin_confirmed,
        "status": "confirmed" if origin_confirmed else "pending_confirmation",
    }
    planning_profile = {
        "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
        "add_device_allowed": True if normalized["enabled"] else None,
        "planning_cost": None,
        "cost_unit": None,
        "source": "tower_colocation_policy" if normalized["enabled"] else "tower_colocation_policy_unconfirmed",
        "confirmed": normalized["enabled"],
        "status": "confirmed" if normalized["enabled"] else "pending_confirmation",
    }
    return {
        "site_id": site_id,
        "name": tower.get("name") or tower_id,
        "coordinate": [float(longitude), float(latitude)],
        "elevation_m": tower.get("elevation_m"),
        "site_type": tower.get("site_type") or "tower",
        # 铁塔不声明可用分系统：不杜撰塔上能装 C/N/S 中的哪一个。
        "available_subsystems": [],
        "usable": True,
        "locked": False,
        "source": "真实通信铁塔站址（只读导入）",
        "vertical_profile": vertical_profile,
        "planning_profile": planning_profile,
        "metadata": {
            "semantics": "tower_colocation_host_candidate",
            "candidate_id": site_id,
            "candidate_origin": TOWER_COLOCATION_ORIGIN,
            "host": {
                "host_type": "tower",
                "host_tower_id": tower_id,
                "host_tower_name": tower.get("name") or tower_id,
                "host_site_type": tower.get("site_type"),
                "host_district": tower.get("district"),
                "host_elevation_m": tower.get("elevation_m"),
                "host_structure_height_m": tower.get("height_m"),
                "site_position_available": HOST_AVAILABILITY_DEFAULTS["site_position_available"],
                "device_mount_confirmed": bool(
                    normalized["enabled"] and normalized.get("device_mount_confirmed")
                ),
            },
            "planning_origin": {
                "origin": TOWER_COLOCATION_ORIGIN,
                "host_tower_id": tower_id,
                "source": deepcopy(tower.get("source")),
            },
            "obstacle_profile": {
                "status": profile.get("status"),
                "base_type": profile.get("base_type"),
                "vertical_status": profile.get("vertical_status"),
                "tower_top_orthometric_m": top if resolved else None,
                "terrain_elevation_m": profile.get("terrain_elevation_m"),
                "building_height_m": profile.get("building_height_m"),
            },
            "source": deepcopy(tower.get("source")),
            "evidence": deepcopy(tower.get("evidence") or []),
            "limitations": limitations,
        },
    }


def build_tower_colocation_candidates(towers, *, obstacle_profiles=None, policy=None):
    """逐塔派生共塔候选；任何一塔失败都不影响其它塔，也不影响普通候选站。"""

    normalized = normalize_tower_colocation_policy(policy)
    item_profiles = (
        obstacle_profiles.get("items") if isinstance(obstacle_profiles, dict) else None
    ) or {}
    items, warnings = [], []
    for tower in towers or []:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        try:
            items.append(tower_colocation_candidate(
                tower, obstacle_profile=item_profiles.get(tower_id), policy=normalized,
            ))
        except ValueError as exc:
            warnings.append(f"tower_colocation_candidate_skipped:{tower_id or 'unknown'}:{exc}")
    return {
        "status": "passed" if items else "missing_data",
        "collection_id": TOWER_COLOCATION_COLLECTION_ID,
        "schema_version": TOWER_COLOCATION_SCHEMA_VERSION,
        "semantics": "preferred_host_site_candidates_derived_from_real_towers",
        "not_a_cns_facility": True,
        "creates_existing_facilities": False,
        "invents_device_parameters": False,
        "count": len(items),
        "items": items,
        "policy": normalized,
        "source": (towers[0].get("source") if towers else None),
        "warnings": warnings[:50],
    }


def normalize_tower_colocation_candidates(value):
    """Persistence round-trip：形状与 CandidateSite 一致，绝不重新推断。"""

    empty = empty_tower_colocation_candidates()
    if not isinstance(value, dict):
        return empty
    result = deepcopy(empty)
    result["status"] = str(value.get("status") or empty["status"])
    result["policy"] = normalize_tower_colocation_policy(value.get("policy"))
    result["source"] = deepcopy(value.get("source"))
    items = value.get("items")
    result["items"] = [deepcopy(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    result["count"] = len(result["items"])
    result["warnings"] = [str(item) for item in (value.get("warnings") or [])][:50]
    return result


__all__ = [
    "HOST_AVAILABILITY_DEFAULTS", "SERVICE_ORIGIN_ASSUMPTIONS",
    "TOWER_COLOCATION_COLLECTION_ID", "TOWER_COLOCATION_ORIGIN",
    "TOWER_COLOCATION_SCHEMA_VERSION",
    "build_tower_colocation_candidates", "default_tower_colocation_policy",
    "empty_tower_colocation_candidates", "normalize_tower_colocation_candidates",
    "normalize_tower_colocation_policy", "tower_colocation_candidate",
]
