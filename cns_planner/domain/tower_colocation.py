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
PLANNING_SERVICE_ORIGIN_POLICIES = (None, "terrain_plus_source_tower_height")

#: 宿主可用性语义（三层，严格区分）：
#:
#: * ``site_position_available``  —— 位置是**源数据事实**（真实站址存在）；
#: * ``planning_host_use_confirmed`` —— **规划层**：用户确认允许把真实铁塔作为
#:   Preferred Host Site 参与 P11/P16 的工程 what-if 比较；
#: * ``physical_mount_confirmed`` —— **物理实施层**：某个 C/N/S 设备是否真的能装到
#:   这座塔上。本阶段**没有**逐塔现场调查数据，因此恒为 ``False``；
#: * ``requires_site_survey`` —— 上述未核实的直接后果，恒为 ``True``。
#:
#: 全局策略确认**只**提升规划层：绝不意味着"373 个铁塔均已确认可以物理安装设备"。
HOST_AVAILABILITY_DEFAULTS = {
    "site_position_available": True,
    "planning_host_use_confirmed": False,
    "physical_mount_confirmed": False,
    "requires_site_survey": True,
    #: legacy 字段：只作为旧项目输入别名读取，canonical 值恒为 False。
    "device_mount_confirmed": False,
}

#: 规划宿主的可用状态（规划层）。
PLANNING_HOST_STATUSES = ("not_confirmed", "eligible")
#: 分系统物理安装状态（实施层）。``unverified`` = 没有证据，**不是**"都能装"。
SUBSYSTEM_MOUNT_STATUSES = ("unverified", "declared_compatible", "declared_not_compatible")


def default_tower_colocation_policy():
    """共塔优先策略（规划层 / 物理实施层严格分离）。

    ``planning_host_use_confirmed=False`` 时共塔候选只是"已识别但未启用"：它**不会**
    被选中（fail-closed），普通候选站照常工作，因此不存在任何权重或默认值被偷偷设定。

    物理实施层没有可确认的输入：``physical_mount_confirmed`` 恒为 ``False``、
    ``requires_site_survey`` 恒为 ``True``（除非未来接入逐塔现场调查数据）。
    """

    return {
        "status": "pending_confirmation",
        "strategy": "prefer_tower_colocation_fallback_to_plain_candidates",
        "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
        "host_pool": "real_tower_sites",
        "service_origin_assumption": None,
        # Explicit opt-in for planning-only estimates on towers whose physical tower-top
        # fact remains unresolved.  This is independent from host-use permission.
        "planning_service_origin_policy": None,
        #: 规划层：允许把真实铁塔作为 Preferred Host Site（工程规划假设）。
        "planning_host_use_confirmed": False,
        #: 物理实施层：恒 False / 恒 True（本阶段无逐塔调查数据）。
        "physical_mount_confirmed": False,
        "requires_site_survey": True,
        #: legacy 输入别名（canonical 值恒为 False）。
        "device_mount_confirmed": False,
        "source": "project_engineering_default",
        "confirmed": False,
        # ``enabled`` 与两个状态投影都是 normalize 计算出的派生标记；默认策略里显式给出，
        # 保证"默认值"与"normalize 结果"形状完全一致（save/reopen 幂等）。
        "enabled": False,
        "planning_host_status": "not_confirmed",
        "subsystem_mount_status": "unverified",
        "limitations_note": (
            "prefer 而不是 force：没有合适铁塔时必须允许新建普通站点；"
            "规划宿主确认不等于物理安装确认，仍需现场勘察"
        ),
    }


def normalize_tower_colocation_policy(value):
    """归一化共塔策略；legacy ``device_mount_confirmed`` 只作为输入别名读取。

    兼容规则（不破坏旧项目 reopen）：

    * 旧项目用 ``confirmed=true`` + ``device_mount_confirmed=true`` 表达"允许共塔布设"，
      在新的规范里映射为 ``planning_host_use_confirmed=true``（仍保持启用）；
    * 旧字段 ``device_mount_confirmed`` 在 canonical state 里恒为 ``False`` ——
      它表达的是物理安装，而本阶段没有逐塔证据。
    """

    if value is None:
        return default_tower_colocation_policy()
    if not isinstance(value, dict):
        raise ValueError("tower_colocation_policy 必须是对象")
    raw = deepcopy(value)
    result = default_tower_colocation_policy()
    result.update(raw)
    if result.get("strategy") != "prefer_tower_colocation_fallback_to_plain_candidates":
        raise ValueError("共塔策略只支持 prefer_tower_colocation_fallback_to_plain_candidates")
    if result.get("reuse_class") != TOWER_COLOCATION_REUSE_CLASS:
        raise ValueError(f"共塔宿主 reuse_class 固定为 {TOWER_COLOCATION_REUSE_CLASS}")
    assumption = result.get("service_origin_assumption")
    if assumption not in SERVICE_ORIGIN_ASSUMPTIONS:
        raise ValueError("tower_colocation_policy.service_origin_assumption 无效")
    result["service_origin_assumption"] = assumption
    estimate_policy = result.get("planning_service_origin_policy")
    if estimate_policy not in PLANNING_SERVICE_ORIGIN_POLICIES:
        raise ValueError("tower_colocation_policy.planning_service_origin_policy 无效")
    result["planning_service_origin_policy"] = estimate_policy

    # 规划层：新字段优先；未提供时按 legacy 语义（confirmed + device_mount_confirmed）
    # 从**原始输入**推断 —— 不能读 result，因为 result 已经带上了默认值 False。
    planning_host = raw.get("planning_host_use_confirmed")
    if planning_host is None:
        planning_host = bool(
            raw.get("confirmed") is True and raw.get("device_mount_confirmed") is True
        )
    result["planning_host_use_confirmed"] = planning_host is True
    # 物理实施层：本阶段没有逐塔现场调查数据，永远是保守值。
    result["physical_mount_confirmed"] = False
    result["requires_site_survey"] = True
    # legacy 字段只保留可读性：canonical 语义由上面两个字段承载。
    result["confirmed"] = result["planning_host_use_confirmed"]
    result["device_mount_confirmed"] = False

    enabled = result["planning_host_use_confirmed"] and (
        assumption is not None or estimate_policy is not None
    )
    result["status"] = "confirmed" if enabled else "pending_confirmation"
    result["enabled"] = enabled
    result["planning_host_status"] = "eligible" if enabled else "not_confirmed"
    result["subsystem_mount_status"] = "unverified"
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
        "tower_count": 0,
        "confirmed_origin_count": 0,
        "estimated_planning_origin_count": 0,
        "unusable_count": 0,
        "items": [],
        "policy": normalize_tower_colocation_policy(None),
        "source": None,
        "warnings": [],
    }


def declared_navigation_site_suitability(tower):
    """铁塔上**已声明**的 ``navigation_site_suitability``（顶层优先，其次 ``metadata``）。

    这是**纯值搬运**：只判断"用户是否显式声明过 suitability"，绝不判断该站址是否合格
    （``confirmed`` / ``planning_use_confirmed`` 的合格性判定唯一归属
    :mod:`cns_planner.domain.navigation_augmentation` 的 fail-closed 规则）。
    未声明时返回 ``None``，因此 legacy payload 的键集合与指纹完全不变。
    """

    item = tower if isinstance(tower, dict) else {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for container in (item, metadata):
        value = container.get("navigation_site_suitability")
        if isinstance(value, dict):
            return deepcopy(value)
    return None


def _source_height_column(tower):
    """Return persisted source-column evidence, with one audited legacy-source migration."""

    column = (tower.get("source_columns") or {}).get("height_m")
    if column:
        return str(column)
    source = tower.get("source") if isinstance(tower.get("source"), dict) else {}
    if source.get("file_name") == "航路航线规划-铁塔数据.xlsx":
        # Pre-Round-2.6.1 project states did not retain matched headers.  The immutable
        # source workbook has been audited: column 8 is exactly 塔身高度(m).
        return "塔身高度(m)"
    return None


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
    origin_confirmed = bool(
        resolved and normalized["planning_host_use_confirmed"]
        and assumption == "tower_top_agl_0"
    )
    terrain = profile.get("terrain_elevation_m")
    structure_height = profile.get("tower_structure_height_m")
    estimate_allowed = (
        normalized["planning_host_use_confirmed"]
        and normalized.get("planning_service_origin_policy")
        == "terrain_plus_source_tower_height"
    )
    estimate_calculable = (
        not resolved and isinstance(terrain, (int, float))
        and isinstance(structure_height, (int, float))
    )
    planning_estimated = bool(estimate_allowed and estimate_calculable)
    planning_origin = (
        float(top) if origin_confirmed else
        float(terrain) + float(structure_height) if planning_estimated else None
    )
    origin_status = "confirmed" if origin_confirmed else "estimated" if planning_estimated else "unusable"
    origin_method = (
        "confirmed_tower_top" if origin_confirmed else
        "terrain_plus_source_tower_height" if planning_estimated else None
    )
    origin_authority = (
        "derived_confirmed" if origin_confirmed else
        "engineering_estimate" if planning_estimated else None
    )
    source_height_column = _source_height_column(tower)
    suitability = declared_navigation_site_suitability(tower)

    limitations = [
        "共塔候选只是宿主候选，不代表铁塔上已有或可以安装任何 CNS 设备",
        "物理安装条件未逐塔核实：需现场勘察（requires_site_survey=true）",
        "设备型号、挂高与性能参数必须来自 device catalog / 用户确认，绝不从铁塔数据推断",
        "源数据 elevation_m 的垂直基准未确认，只作留档，不作为 EGM2008 正高",
    ]
    if not resolved:
        limitations.append(
            "塔顶 EGM2008 正高未解析：不生成具体 tower clearance floor，"
            "相关空间保持 unknown 并在路径搜索中 fail-closed"
        )
        if planning_estimated:
            limitations.append(
                "未计入潜在建筑屋面高度；该值仅作为规划服务原点估计，可能低估真实安装高程，"
                "实际安装需现场勘察确认。"
            )
    elif not normalized["enabled"]:
        limitations.append("共塔规划宿主尚未确认：候选不会被选中，普通候选站照常可用")
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
        "planning_service_origin_egm2008_m": planning_origin,
        "planning_origin_status": origin_status,
        "planning_origin_authority": origin_authority,
        "planning_origin_method": origin_method,
        "planning_origin_components": {
            "terrain_elevation_egm2008_m": terrain,
            "source_tower_height_m": structure_height,
            "building_height_m": profile.get("building_height_m") if resolved else None,
            "mount_offset_m": 0.0 if planning_origin is not None else None,
            "mount_offset_authority": "engineering_assumption" if planning_origin is not None else None,
            "source_column": source_height_column,
            "original_statement": (
                f"源字段 {source_height_column}，单位 m"
                if structure_height is not None and source_height_column else None
            ),
            "assumption_statement": (
                "将源塔身高度解释为相对安装基面的铁塔结构高度" if structure_height is not None else None
            ),
        },
        "planning_origin_limitations": deepcopy(limitations),
        "planning_origin_estimated": planning_estimated,
        "origin_status": origin_status,
        "origin_authority": origin_authority,
        "origin_method": origin_method,
    }
    planning_host = normalized["planning_host_use_confirmed"]
    planning_profile = {
        "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
        # 规划层：允许把该塔作为 Preferred Host Site（工程规划假设）。
        # **不**表示物理安装已确认 —— 物理层由 metadata.host / metadata.planning_host 承载。
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
        # 空数组的含义是"没有证据"，**不是**"所有分系统都能装"。
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
                "terrain_elevation_egm2008_m": terrain,
                "source_tower_height_m": structure_height,
                # 三层语义：位置是事实；规划层由策略确认；物理层永远是未核实。
                "site_position_available": HOST_AVAILABILITY_DEFAULTS["site_position_available"],
                "planning_host_use_confirmed": planning_host,
                "physical_mount_confirmed": False,
                "requires_site_survey": True,
                # legacy 字段：保留可读性，canonical 值恒为 False。
                "device_mount_confirmed": False,
            },
            #: 规划层 / 实施层状态投影（P11/P16 action 与 Proposal 直接消费）。
            "planning_host": {
                "planning_host_status": "eligible" if normalized["enabled"] else "not_confirmed",
                "subsystem_mount_status": "unverified",
                "physical_mount_status": "unverified",
                "physical_mount_confirmed": False,
                "requires_site_survey": True,
                "declared_available_subsystems": [],
                "subsystem_mount_note": (
                    "没有任何逐塔分系统安装证据：既不能推断塔上已装设备，"
                    "也不能推断某个 C/N/S 设备一定装得上"
                ),
            },
            "planning_origin": {
                "origin": TOWER_COLOCATION_ORIGIN,
                "host_tower_id": tower_id,
                "source": deepcopy(tower.get("source")),
                "status": origin_status,
                "authority": origin_authority,
                "method": origin_method,
                "service_origin_egm2008_m": planning_origin,
                "estimated": planning_estimated,
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
            #: Round D：铁塔上**已显式声明**的导航基准站站址适用性必须原样过继到共塔
            #: 候选，否则每次重新派生共塔候选都会把用户已确认的 suitability 丢掉。
            #: 缺失即**不写该键**（保持既有 payload 形状与指纹不变）；这里只搬运事实，
            #: 不做任何合格性判定（判定只在 navigation_augmentation 的 fail-closed 规则里）。
            **(
                {"navigation_site_suitability": suitability}
                if suitability is not None else {}
            ),
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
    confirmed_count = sum(
        (item.get("vertical_profile") or {}).get("planning_origin_status") == "confirmed"
        for item in items
    )
    estimated_count = sum(
        (item.get("vertical_profile") or {}).get("planning_origin_status") == "estimated"
        for item in items
    )
    return {
        "status": "passed" if items else "missing_data",
        "collection_id": TOWER_COLOCATION_COLLECTION_ID,
        "schema_version": TOWER_COLOCATION_SCHEMA_VERSION,
        "semantics": "preferred_host_site_candidates_derived_from_real_towers",
        "not_a_cns_facility": True,
        "creates_existing_facilities": False,
        "invents_device_parameters": False,
        "count": len(items),
        "confirmed_origin_count": confirmed_count,
        "estimated_planning_origin_count": estimated_count,
        "unusable_count": len(items) - confirmed_count - estimated_count,
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
    result["tower_count"] = int(value.get("tower_count") or result["count"])
    for key in ("confirmed_origin_count", "estimated_planning_origin_count", "unusable_count"):
        result[key] = int(value.get(key) or 0)
    result["warnings"] = [str(item) for item in (value.get("warnings") or [])][:50]
    return result


__all__ = [
    "HOST_AVAILABILITY_DEFAULTS", "PLANNING_HOST_STATUSES", "SERVICE_ORIGIN_ASSUMPTIONS",
    "PLANNING_SERVICE_ORIGIN_POLICIES",
    "SUBSYSTEM_MOUNT_STATUSES",
    "TOWER_COLOCATION_COLLECTION_ID", "TOWER_COLOCATION_ORIGIN",
    "TOWER_COLOCATION_SCHEMA_VERSION",
    "build_tower_colocation_candidates", "declared_navigation_site_suitability",
    "default_tower_colocation_policy", "empty_tower_colocation_candidates",
    "normalize_tower_colocation_candidates", "normalize_tower_colocation_policy",
    "tower_colocation_candidate",
]
