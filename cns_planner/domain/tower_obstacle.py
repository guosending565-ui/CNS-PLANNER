"""TowerObstacleProfile：航路障碍物/净空判定使用的**派生**事实。

三个铁塔角色必须严格分离：

* :mod:`cns_planner.domain.towers` —— ``TowerSite``：真实站址事实（只读导入，永远不变）；
* :mod:`cns_planner.domain.tower_colocation` —— ``TowerColocationCandidate``：CNS 规划宿主候选；
* **本模块** —— ``TowerObstacleProfile``：只在航路障碍物/净空判断里使用的派生高度事实。

本模块的边界：

* 绝不修改 ``TowerSite`` 契约，也不写回源数据；
* **不把源数据 ``elevation_m`` 当成 EGM2008 正高**。Excel 没有声明垂直基准，因此地形高程
  只来自已确认的 FABDEM DTM（``vertical_reference == "egm2008_orthometric"``）；
* 楼面塔必须加上**建筑高度**（塔不是从地面开始的）；建筑高度无法解析时状态为
  ``unresolved``，绝不假装建筑高度为 0（fail-closed）；
* 塔身高度只来自源文件真实存在的 ``height_m``；缺失即 ``unresolved``，不做任何推断；
* 垂直净空/水平净空**没有默认值**：未显式确认时状态是 ``not_configured``，不是 0。
"""

from __future__ import annotations

from copy import deepcopy

TOWER_OBSTACLE_SCHEMA_VERSION = 1
TOWER_OBSTACLE_COLLECTION_ID = "tower_obstacle_profiles"

EGM2008_ORTHOMETRIC = "egm2008_orthometric"

#: 站址细分类型里的"楼面/屋顶"识别特征。命中即判定为 rooftop（不推断塔身是否落地）。
#: "楼顶"是真实报送数据里明确属于 rooftop 的写法（例如"楼顶景观塔"），因此显式纳入。
ROOFTOP_MARKERS = ("楼面", "楼顶", "屋顶", "屋面", "rooftop", "roof")
#: 明确的地面识别特征。只有明确写了"地面/落地"才判定 ground。
GROUND_MARKERS = ("地面", "落地", "ground")

BASE_TYPES = ("ground", "rooftop", "unknown")

#: 状态取值：``resolved`` 表示塔顶 EGM2008 正高已经由真实事实推出来；否则 ``unresolved``。
# ``resolved`` is an algorithmic derivation state, not publication authority.
TOWER_OBSTACLE_STATUSES = ("resolved", "unresolved")

#: 垂直高度状态（unresolved 时说明到底缺什么，绝不含糊）。
VERTICAL_STATUSES = (
    "egm2008_orthometric_resolved",
    "tower_structure_height_missing",
    "terrain_elevation_unresolved",
    "building_height_unresolved",
    "base_type_unknown",
)

TOWER_CLEARANCE_POLICY_FIELDS = (
    "tower_vertical_clearance_m", "tower_horizontal_clearance_m",
)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value):
    if not _finite(value):
        return None
    number = float(value)
    return number


def classify_base_type(site_type, *, default=None):
    """从源数据的站址细分类型判定 base_type（ground / rooftop / unknown）。

    只认显式的地点词。**未知一律 ``unknown``**：把未知类型当作 ground 会低估楼面塔的
    障碍高度，这是不安全的方向。``default`` 只有在调用方显式配置了假设时才传入。
    """

    text = str(site_type or "").strip().lower()
    if text:
        if any(marker in text for marker in ROOFTOP_MARKERS):
            return "rooftop", "site_type_rooftop_marker"
        if any(marker in text for marker in GROUND_MARKERS):
            return "ground", "site_type_ground_marker"
    if default in ("ground", "rooftop"):
        return default, "explicit_policy_default_base_type"
    return "unknown", ("site_type_missing" if not text else "site_type_unclassified")


def default_tower_obstacle_policy():
    """障碍物派生策略。

    ``default_base_type`` 默认 ``None``：**不假设**任何站址类型是地面还是楼面。
    """

    return {
        "status": "pending_confirmation",
        "default_base_type": None,
        "source": "project_engineering_default",
        "confirmed": False,
    }


def normalize_tower_obstacle_policy(value):
    if value is None:
        return default_tower_obstacle_policy()
    if not isinstance(value, dict):
        raise ValueError("tower_obstacle_policy 必须是对象")
    result = default_tower_obstacle_policy()
    result.update(deepcopy(value))
    base = result.get("default_base_type")
    if base not in (None, "ground", "rooftop"):
        raise ValueError("tower_obstacle_policy.default_base_type 只能是 ground/rooftop/null")
    result["default_base_type"] = base
    result["confirmed"] = result.get("confirmed") is True
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    result["source"] = str(result.get("source") or "未记录")
    return result


def default_tower_clearance_policy():
    """航路塔净空策略。

    两个净空**都没有默认值**：没有工程依据的数值不会在这里被偷偷设定。
    未确认时航路可行性对塔的判定是 ``not_configured``（未知），不是 0、也不是"通过"。
    """

    return {
        "status": "pending_confirmation",
        "tower_vertical_clearance_m": None,
        "tower_horizontal_clearance_m": None,
        "source": "project_engineering_default",
        "confirmed": False,
    }


def normalize_tower_clearance_policy(value):
    if value is None:
        raw = {}
    elif isinstance(value, dict):
        raw = value
    else:
        raise ValueError("tower_clearance_policy 必须是对象")
    result = default_tower_clearance_policy()
    result.update(deepcopy(raw))
    for field in TOWER_CLEARANCE_POLICY_FIELDS:
        number = result.get(field)
        if number in (None, ""):
            result[field] = None
            continue
        try:
            number = float(number)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"tower_clearance_policy.{field} 必须是数值") from exc
        if number < 0:
            raise ValueError(f"tower_clearance_policy.{field} 不能为负")
        result[field] = number
    result["confirmed"] = result.get("confirmed") is True
    configured = (
        result["tower_vertical_clearance_m"] is not None
        and result["tower_horizontal_clearance_m"] is not None
    )
    result["status"] = (
        "confirmed" if result["confirmed"] and configured
        else "not_configured" if not configured
        else "pending_confirmation"
    )
    result["source"] = str(result.get("source") or "未记录")
    return result


def empty_tower_obstacle_profiles(status="not_calculated"):
    return {
        "status": status,
        "collection_id": TOWER_OBSTACLE_COLLECTION_ID,
        "schema_version": TOWER_OBSTACLE_SCHEMA_VERSION,
        "semantics": "derived_tower_obstacle_heights_for_route_clearance_only",
        "not_a_population_risk_factor": True,
        "not_a_risk_framework_v2_input": True,
        "count": 0,
        "resolved_count": 0,
        "confirmed_count": 0,
        "resolved_unconfirmed_count": 0,
        "unresolved_count": 0,
        "policy": normalize_tower_obstacle_policy(None),
        "source": None,
        "items": {},
        "warnings": [],
    }


def _canonical_vertical_reference(value):
    """垂直基准的**最小**规范化：只 strip + case-insensitive 识别 EGM2008 正高。

    真实 FABDEM metadata 的 casing 是 ``"EGM2008_orthometric"``，而常量是
    ``"egm2008_orthometric"``；大小写不同并不代表垂直基准不同。因此这里对
    已确认的基准做 canonical 归一化，成功即写回规范写法。

    这是**唯一**被归一化的取值：任何其它字符串原样保留（绝不放宽为"任意非空
    字符串即可信"），由调用方继续 fail-closed 判为 unresolved。
    """

    text = str(value or "").strip()
    if text.lower() == EGM2008_ORTHOMETRIC:
        return EGM2008_ORTHOMETRIC
    return text


def _terrain_reading(terrain):
    fact = terrain if isinstance(terrain, dict) else {}
    elevation = _number(fact.get("elevation_m"))
    reference = _canonical_vertical_reference(fact.get("vertical_reference"))
    passed = str(fact.get("status") or "") == "passed" and elevation is not None
    if passed and reference == EGM2008_ORTHOMETRIC:
        return {
            "status": "passed", "elevation_m": elevation,
            "vertical_reference": reference, "reason": None,
        }
    return {
        "status": "unresolved",
        "elevation_m": elevation,
        "vertical_reference": reference or "unknown",
        "reason": str(fact.get("reason") or (
            "terrain_vertical_reference_not_confirmed"
            if passed else "terrain_elevation_unavailable"
        )),
    }


def _building_reading(building):
    fact = building if isinstance(building, dict) else {}
    height = _number(fact.get("building_height_m"))
    fraction = fact.get("valid_height_fraction")
    fraction = float(fraction) if _finite(fraction) else None
    if str(fact.get("status") or "") != "passed":
        return {
            "status": "unresolved", "building_height_m": None, "valid_height_fraction": fraction,
            "source": fact.get("source"),
            "reason": str(fact.get("reason") or "building_height_unresolved"),
        }
    if height is None:
        return {
            "status": "unresolved", "building_height_m": None, "valid_height_fraction": fraction,
            "source": fact.get("source"), "reason": "building_height_missing",
        }
    if fraction is not None and fraction < 1.0:
        # 部分覆盖绝不平均、绝不补 0：这是"未知"，不是"高度更低"。
        return {
            "status": "unresolved", "building_height_m": None, "valid_height_fraction": fraction,
            "source": fact.get("source"), "reason": "building_height_partially_covered_unknown",
        }
    return {
        "status": "passed", "building_height_m": height, "valid_height_fraction": fraction,
        "source": fact.get("source"), "reason": None,
    }


def build_tower_obstacle_profile(tower, *, terrain=None, building=None, policy=None):
    """派生一个铁塔的障碍物高度事实（纯函数，不读文件、不猜基准）。

    ``terrain`` / ``building`` 是 GIS 边界产出的真实事实：

    * ``terrain``：``{"status","elevation_m","vertical_reference","reason"}``，
      高程必须是已确认的 EGM2008 正高（FABDEM DTM）；
    * ``building``：``{"status","building_height_m","valid_height_fraction","source","reason"}``。

    返回：TowerObstacleProfile（``status`` 为 ``resolved`` 或 ``unresolved``）。
    """

    if not isinstance(tower, dict):
        raise ValueError("铁塔条目必须是对象")
    tower_id = str(tower.get("tower_id") or "")
    if not tower_id:
        raise ValueError("TowerObstacleProfile 需要 tower_id")

    normalized_policy = normalize_tower_obstacle_policy(policy)
    site_type = tower.get("site_type")
    base_type, base_source = classify_base_type(
        site_type, default=normalized_policy.get("default_base_type"),
    )
    structure_height = _number(tower.get("height_m"))
    terrain_reading = _terrain_reading(terrain)
    building_reading = _building_reading(building)

    limitations = [
        "源数据 elevation_m 的垂直基准未经确认，因此不作为 EGM2008 正高使用",
        "塔顶高度是派生事实，只用于航路障碍物/净空判断，不进入任何人口风险或 Risk Framework V2",
    ]
    tower_top = None
    vertical_status = None
    reason = None

    if base_type == "unknown":
        vertical_status = "base_type_unknown"
        reason = "站址细分类型无法判定地面/楼面，不能假定塔从地面起算"
    elif structure_height is None:
        vertical_status = "tower_structure_height_missing"
        reason = "源数据没有真实塔身高度（height_m），不做推断"
    elif terrain_reading["status"] != "passed":
        vertical_status = "terrain_elevation_unresolved"
        reason = f"FABDEM DTM 地形正高不可用：{terrain_reading['reason']}"
    elif base_type == "ground":
        tower_top = terrain_reading["elevation_m"] + structure_height
        vertical_status = "egm2008_orthometric_resolved"
        limitations.append("地面塔：塔顶 = FABDEM DTM 地形正高 + 源数据塔身高度")
    elif building_reading["status"] != "passed":
        vertical_status = "building_height_unresolved"
        reason = (
            "楼面塔必须在塔身高度之上叠加建筑高度；"
            f"建筑高度未解析（{building_reading['reason']}），绝不当作 0"
        )
        limitations.append(
            "楼面塔建筑高度未解析：不生成具体 tower clearance floor，"
            "相关空间保持 unknown 并在路径搜索中 fail-closed"
        )
    else:
        tower_top = (
            terrain_reading["elevation_m"]
            + building_reading["building_height_m"]
            + structure_height
        )
        vertical_status = "egm2008_orthometric_resolved"
        limitations.append("楼面塔：塔顶 = FABDEM DTM 地形正高 + 建筑高度 + 源数据塔身高度")

    status = "resolved" if tower_top is not None else "unresolved"
    confirmation = tower.get("tower_top_confirmation")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    confirmation_authority = str(
        confirmation.get("authority") or confirmation.get("source") or ""
    ).strip()
    confirmed = bool(
        status == "resolved"
        and (tower.get("tower_top_confirmed") is True or confirmation.get("confirmed") is True)
        and confirmation_authority
    )
    if status == "unresolved":
        limitations.append(
            "塔顶高度未解析时不生成具体 tower clearance floor；相关空间保持 unknown，"
            "并在路径搜索中 fail-closed，不得作为已验证安全可通行区域"
        )

    return {
        "schema_version": TOWER_OBSTACLE_SCHEMA_VERSION,
        "tower_id": tower_id,
        "name": tower.get("name") or tower_id,
        "longitude": _number(tower.get("longitude")),
        "latitude": _number(tower.get("latitude")),
        "site_type": site_type,
        "base_type": base_type,
        "base_type_source": base_source,
        "terrain_elevation_m": terrain_reading["elevation_m"] if terrain_reading["status"] == "passed" else None,
        "terrain_vertical_reference": terrain_reading["vertical_reference"],
        "terrain_status": terrain_reading["status"],
        "terrain_reason": terrain_reading["reason"],
        "building_height_m": (
            building_reading["building_height_m"]
            if building_reading["status"] == "passed" else None
        ),
        "building_height_status": building_reading["status"],
        "building_height_source": building_reading.get("source"),
        "building_height_reason": building_reading["reason"],
        "tower_structure_height_m": structure_height,
        "tower_structure_height_source": "source_file_height_m" if structure_height is not None else None,
        "tower_top_orthometric_m": tower_top,
        "tower_top_status": (
            "confirmed" if confirmed else
            "resolved_unconfirmed" if status == "resolved" else "unknown"
        ),
        "confirmed": confirmed,
        "confirmation_authority": confirmation_authority or None,
        "confirmation_evidence": deepcopy(confirmation.get("evidence") or []),
        "horizontal_status": "source_coordinate_used_as_is",
        "vertical_status": vertical_status,
        "status": status,
        "reason": reason,
        "source": deepcopy(tower.get("source")),
        "evidence": deepcopy(tower.get("evidence") or []),
        "limitations": limitations,
    }


def build_tower_obstacle_profiles(towers, *, terrain_by_tower=None, building_by_tower=None, policy=None):
    """为一个 tower 集合派生障碍物事实；逐塔独立，绝不互相平均或合并。"""

    terrain_by_tower = terrain_by_tower or {}
    building_by_tower = building_by_tower or {}
    normalized_policy = normalize_tower_obstacle_policy(policy)
    items, warnings = {}, []
    for tower in towers or []:
        if not isinstance(tower, dict):
            continue
        tower_id = str(tower.get("tower_id") or "")
        if not tower_id:
            continue
        profile = build_tower_obstacle_profile(
            tower,
            terrain=terrain_by_tower.get(tower_id),
            building=building_by_tower.get(tower_id),
            policy=normalized_policy,
        )
        items[tower_id] = profile
        if profile["status"] == "unresolved":
            warnings.append(f"tower_obstacle_unresolved:{tower_id}:{profile['vertical_status']}")
        elif profile.get("confirmed") is not True:
            warnings.append(f"tower_obstacle_resolved_unconfirmed:{tower_id}")
    resolved = sum(1 for item in items.values() if item["status"] == "resolved")
    confirmed = sum(1 for item in items.values() if item.get("confirmed") is True)
    return {
        "status": "passed" if items else "missing_data",
        "collection_id": TOWER_OBSTACLE_COLLECTION_ID,
        "schema_version": TOWER_OBSTACLE_SCHEMA_VERSION,
        "semantics": "derived_tower_obstacle_heights_for_route_clearance_only",
        "not_a_population_risk_factor": True,
        "not_a_risk_framework_v2_input": True,
        "count": len(items),
        "resolved_count": resolved,
        "confirmed_count": confirmed,
        "resolved_unconfirmed_count": resolved - confirmed,
        "unresolved_count": len(items) - resolved,
        "policy": normalized_policy,
        "source": (towers[0].get("source") if towers else None),
        "items": items,
        "warnings": warnings[:50],
    }


def normalize_tower_obstacle_profiles(value):
    """Persistence round-trip：只保留契约字段，绝不重建或重新推断高度。"""

    empty = empty_tower_obstacle_profiles()
    if not isinstance(value, dict):
        return empty
    result = deepcopy(empty)
    result["status"] = str(value.get("status") or empty["status"])
    result["policy"] = normalize_tower_obstacle_policy(value.get("policy"))
    result["source"] = deepcopy(value.get("source"))
    items = value.get("items")
    if not isinstance(items, dict):
        items = {}
    kept = {}
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        record = deepcopy(item)
        record["tower_id"] = str(record.get("tower_id") or key)
        record["status"] = (
            "resolved" if str(record.get("status") or "") == "resolved" else "unresolved"
        )
        record["confirmed"] = record.get("confirmed") is True
        record["tower_top_status"] = (
            "confirmed" if record["confirmed"] and record["status"] == "resolved"
            else "resolved_unconfirmed" if record["status"] == "resolved" else "unknown"
        )
        record["base_type"] = (
            record.get("base_type") if record.get("base_type") in BASE_TYPES else "unknown"
        )
        kept[str(key)] = record
    result["items"] = kept
    result["count"] = len(kept)
    result["resolved_count"] = sum(1 for item in kept.values() if item["status"] == "resolved")
    result["confirmed_count"] = sum(
        1 for item in kept.values() if item.get("confirmed") is True
    )
    result["resolved_unconfirmed_count"] = (
        result["resolved_count"] - result["confirmed_count"]
    )
    result["unresolved_count"] = result["count"] - result["resolved_count"]
    result["warnings"] = [str(item) for item in (value.get("warnings") or [])][:50]
    return result


__all__ = [
    "BASE_TYPES", "EGM2008_ORTHOMETRIC", "GROUND_MARKERS", "ROOFTOP_MARKERS",
    "TOWER_OBSTACLE_COLLECTION_ID", "TOWER_OBSTACLE_SCHEMA_VERSION",
    "TOWER_OBSTACLE_STATUSES", "TOWER_CLEARANCE_POLICY_FIELDS", "VERTICAL_STATUSES",
    "build_tower_obstacle_profile", "build_tower_obstacle_profiles", "classify_base_type",
    "default_tower_clearance_policy", "default_tower_obstacle_policy",
    "empty_tower_obstacle_profiles", "normalize_tower_clearance_policy",
    "normalize_tower_obstacle_policy", "normalize_tower_obstacle_profiles",
]
