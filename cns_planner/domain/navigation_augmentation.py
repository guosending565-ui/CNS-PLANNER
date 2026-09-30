"""Navigation RTK augmentation service evidence（纯 domain，无 I/O、无几何半径）。

本模块把 Round A 建立的 **Navigation service contract** 升级为 P14 可消费的
**逐 voxel / 逐 sample 服务证据**，并定义地面导航增强设施的**工程规划单元**。

严格边界（Round C 冻结）：

* **只规划地面 RTK / PPP-RTK 导航增强设施**（reference station 网络）；
  绝不规划 GNSS 星座、INS、视觉导航，也绝不把机载 GNSS 接收机变成地面站；
* **baseline 距离只有一个权威来源**：RequiredCNS
  ``navigation.services["N:rtk_augmentation"].planning.max_reference_baseline_m``。
  本模块**不含任何默认基线距离**，也没有任何"覆盖半径"概念；
* ``max_reference_baseline_m`` / ``required_distinct_site_count`` 缺失或
  ``planning.confirmed != true`` ⇒ ``unknown``（fail-closed），绝不变成
  ``confirmed_deficit``，也绝不回落成 0 个 provider；
* 距离一律复用 :func:`cns_planner.domain.geodesy.distance_m`（项目唯一 geodesy 原语），
  本模块**不**实现第二个球面距离公式；
* 具体设备型号 / 厂家 / 频率 / 精度 / 可用性一律不虚构：可规划的只有
  :data:`REFERENCE_STATION_PLANNING_UNIT`（工程规划单元），
  ``equipment_selection_status = not_selected``。
"""

from __future__ import annotations

from copy import deepcopy

from .cns_service_contract import (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
)
from .cns_service_registry import service_requirement_for
from .geodesy import distance_m
from .route_safety_evidence_v2 import stable_fingerprint


SERVICE_KEY = SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION
SUBSYSTEM = "N"

#: 地面参考站的**工程规划单元**标识：它不是设备型号，也不是厂家规格，
#: 更不是 ``equipment_reference_catalog`` 的映射结果。
REFERENCE_STATION_PLANNING_UNIT = "reference_station_engineering_planning_unit"
PLANNING_UNIT_MATURITY = "engineering_planning_proposal"
EQUIPMENT_SELECTION_STATUS = "not_selected"

#: baseline 模型的唯一名称（与 Round A registry 校验一致）。
BASELINE_MODEL = "reference_station_baseline"
BASELINE_SEMANTICS = "engineering_reference_station_baseline_envelope"

#: 站点适 suitability 的证据来源键（顶层或 ``metadata`` 内都接受）。
SUITABILITY_FIELDS = (
    "confirmed",
    "planning_use_confirmed",
    "open_sky_confirmed",
    "surveyed_coordinate_confirmed",
    "stable_mount_confirmed",
    "backhaul_available",
    #: **Round C 扩展（显式）**：该站址上是否**已经建成**地面导航增强参考站。
    #: 未声明（``None``）时只有"已有 CNS 设施"来源被视为已建成；共塔 / 候选站址上的
    #: suitability 确认只表示"获准安装"，**不**等于"已建成"，因此不得在基线里充数。
    "reference_station_installed",
    "source",
    "notes",
)

STATUS_SATISFIED = "satisfied"
STATUS_CONFIRMED_DEFICIT = "confirmed_deficit"
STATUS_UNKNOWN = "unknown"

CAUSE_REFERENCE_STATION_DEFICIT = "reference_station_deficit"
CAUSE_CORRECTION_DELIVERY_DEFICIT = "correction_delivery_deficit"

REASON_POLICY_NOT_CONFIRMED = "navigation_augmentation_policy_not_confirmed"
REASON_BASELINE_MISSING = "max_reference_baseline_m_missing"
REASON_SITE_COUNT_MISSING = "required_distinct_site_count_missing"
REASON_DELIVERY_NOT_CONFIGURED = "correction_delivery_service_not_configured"
REASON_DELIVERY_UNKNOWN = "correction_delivery_unknown"
REASON_DELIVERY_DEFICIT = "correction_delivery_confirmed_deficit"


# ---------------------------------------------------------------------------
# 1. site suitability（fail-closed）
# ---------------------------------------------------------------------------


def normalize_navigation_site_suitability(value):
    """规范化 ``navigation_site_suitability``，缺失即"无证据"（绝不默认合格）。

    返回 ``None`` 表示该站点**没有任何** navigation suitability 证据；调用方必须把它
    当作"不可作为正式规划候选"，而**不是** ineligible，也**不是** eligible。

    最低 eligibility（Round C 冻结）：``confirmed is True`` **且**
    ``planning_use_confirmed is True``。普通通信铁塔 / 共塔候选 / ``available_subsystems``
    包含 ``N`` 都**不足以**满足它。
    """

    item = value if isinstance(value, dict) else None
    if item is None:
        return None
    result = {name: item.get(name) for name in SUITABILITY_FIELDS}
    for name in SUITABILITY_FIELDS:
        if name in ("source", "notes"):
            text = result.get(name)
            result[name] = None if text in (None, "") else str(text)
            continue
        if result.get(name) is None:
            continue
        result[name] = result.get(name) is True
    result["confirmed"] = result.get("confirmed") is True
    result["planning_use_confirmed"] = result.get("planning_use_confirmed") is True
    result["eligible_for_navigation_reference_station"] = bool(
        result["confirmed"] and result["planning_use_confirmed"]
    )
    return result


def site_suitability_for(site):
    """从站点/设施条目中取 suitability：顶层优先，其次 ``metadata``。"""

    item = site if isinstance(site, dict) else {}
    for container in (
        item,
        item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
    ):
        value = container.get("navigation_site_suitability")
        normalized = normalize_navigation_site_suitability(value)
        if normalized is not None:
            return normalized
    return None


# ---------------------------------------------------------------------------
# 2. required policy（唯一权威来源）
# ---------------------------------------------------------------------------


def navigation_requirement_for(required_cns, route_id=None):
    """取该 route（缺省 project_default）的 ``N:rtk_augmentation`` 显式要求。

    **只有显式** ``navigation.services["N:rtk_augmentation"]`` 才会返回非空值：
    legacy ``N`` 子系统要求、``service_key = N:navigation``、以及机载
    ``technology = gnss_rtk`` 都**不会**产生地面导航增强要求。
    """

    requirements = (
        ((required_cns or {}).get("route_overrides") or {}).get(str(route_id))
        if route_id not in (None, "")
        else None
    ) or (required_cns or {}).get("project_default") or {}
    navigation = requirements.get("navigation") or {}
    return service_requirement_for("N", navigation, SERVICE_KEY)


def navigation_planning(required_cns, route_id=None):
    """该 route 的 RTK planning 块（未显式要求服务时返回 ``None``）。"""

    requirement = navigation_requirement_for(required_cns, route_id)
    if requirement is None:
        return None
    planning = requirement.get("planning")
    return deepcopy(planning) if isinstance(planning, dict) else {}


def planning_policy_status(planning):
    """``planning`` 块 → 明确的可用性结论（不猜、不回落默认值）。"""

    item = planning if isinstance(planning, dict) else None
    if not item or item.get("confirmed") is not True:
        return {
            "ready": False, "reason": REASON_POLICY_NOT_CONFIRMED,
            "max_reference_baseline_m": None, "required_distinct_site_count": None,
            "delivery_service_key": None,
        }
    baseline = item.get("max_reference_baseline_m")
    count = item.get("required_distinct_site_count")
    if baseline in (None, ""):
        return {
            "ready": False, "reason": REASON_BASELINE_MISSING,
            "max_reference_baseline_m": None, "required_distinct_site_count": count,
            "delivery_service_key": item.get("delivery_service_key"),
        }
    if count in (None, ""):
        return {
            "ready": False, "reason": REASON_SITE_COUNT_MISSING,
            "max_reference_baseline_m": float(baseline), "required_distinct_site_count": None,
            "delivery_service_key": item.get("delivery_service_key"),
        }
    return {
        "ready": True, "reason": None,
        "max_reference_baseline_m": float(baseline),
        "required_distinct_site_count": int(count),
        "delivery_service_key": item.get("delivery_service_key"),
    }


# ---------------------------------------------------------------------------
# 3. confirmed navigation providers（site suitability fail-closed）
# ---------------------------------------------------------------------------


def collect_navigation_sites(
    *, existing_facilities=None, candidate_sites=None, tower_colocation=None,
):
    """收集 navigation reference station 的站址证据（确认与未确认分开）。

    只有显式 ``navigation_site_suitability`` 同时满足 ``confirmed`` 与
    ``planning_use_confirmed`` 的站点才进入 ``confirmed`` 结果。因此：

    * 普通 ``tower_colocation`` 候选**不会**因为存在于列表里就成为合格站；
    * ``available_subsystems`` 含 ``N`` **不会**让站点自动合格；
    * 声明了 suitability 但尚未确认的站既不是 eligible 也不是 ineligible，
      它以 ``status = unconfirmed_suitability`` 进入 ``unconfirmed``，
      使结论保持 ``unknown`` 而不是被误判成"没有可用站"的 deficit。

    返回 ``(confirmed, unconfirmed, planning_candidates)``：

    * ``confirmed``：**已建成**且 suitability 确认的导航站 → 基线 provider 池；
    * ``unconfirmed``：声明了 suitability 但未确认 → 保持 ``unknown`` 的证据；
    * ``planning_candidates``：suitability 已确认但**尚未建成**的站址 → 只有它们才是
      P16 的建站候选（"获批安装"不等于"已在服务"）。

    三者都按 ``distinct_site_id`` 去重：同一物理站点上的多个导航设备仍只算 1 个站。
    """

    best, pending, candidates = {}, {}, {}
    for entry in (
        _site_entries(existing_facilities, "existing_cns_facility"),
        _site_entries(candidate_sites, "candidate_site"),
        _site_entries(tower_colocation, "tower_colocation_host"),
    ):
        confirmed, unconfirmed, not_installed = entry
        for provider in confirmed:
            key = str(provider.get("distinct_site_id") or "")
            if not key:
                continue
            current = best.get(key)
            if current is None or _reuse_tier_rank(provider) < _reuse_tier_rank(current):
                best[key] = provider
        for provider in not_installed:
            key = str(provider.get("distinct_site_id") or provider.get("provider_id") or "")
            if not key:
                continue
            current = candidates.get(key)
            if current is None or _reuse_tier_rank(provider) < _reuse_tier_rank(current):
                candidates[key] = provider
        for provider in unconfirmed:
            key = str(provider.get("distinct_site_id") or provider.get("provider_id") or "")
            if not key:
                continue
            pending.setdefault(key, provider)
        for key in list(pending):
            if key in best:
                pending.pop(key)
    for key in list(candidates):
        if key in best:
            candidates.pop(key)
    return (
        sorted(best.values(), key=lambda item: item["provider_id"]),
        sorted(pending.values(), key=lambda item: item["provider_id"]),
        sorted(candidates.values(), key=lambda item: item["provider_id"]),
    )


def _site_entries(collection, origin):
    confirmed, unconfirmed, not_installed = [], [], []
    for site in (collection or {}).get("items") or []:
        if not isinstance(site, dict):
            continue
        suitability = site_suitability_for(site)
        if not suitability:
            # 没有任何 navigation suitability 证据的站点：既不是候选也不是反证。
            continue
        if suitability.get("eligible_for_navigation_reference_station") is not True:
            coordinate = site.get("coordinate")
            if not isinstance(coordinate, list) or len(coordinate) < 2:
                continue
            unconfirmed.append({
                "provider_id": f"{origin}:{site.get('facility_id') or site.get('site_id') or 'unknown'}",
                "service_key": SERVICE_KEY,
                "subsystem": SUBSYSTEM,
                "site_id": site.get("site_id") if site.get("site_id") is not None else site.get("facility_id"),
                "distinct_site_id": _distinct_site_id(site),
                "coordinate": [float(coordinate[0]), float(coordinate[1])],
                "status": "unconfirmed_suitability",
                "source": suitability.get("source"),
                "planning_origin": origin,
                "site_suitability": suitability,
                "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
                "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
                "maturity": PLANNING_UNIT_MATURITY,
            })
            continue
        if origin == "existing_cns_facility" and site.get("status") not in ("active", "passed", None):
            continue
        if origin == "candidate_site" and site.get("usable") is False:
            continue
        coordinate = site.get("coordinate")
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            continue
        distinct_site_id = _distinct_site_id(site)
        if not distinct_site_id:
            continue
        identifier = str(site.get("facility_id") or site.get("site_id") or distinct_site_id)
        provider = {
            "provider_id": f"{origin}:{identifier}",
            "service_key": SERVICE_KEY,
            "subsystem": SUBSYSTEM,
            "site_id": site.get("site_id") if site.get("site_id") is not None else site.get("facility_id"),
            "facility_id": site.get("facility_id"),
            "distinct_site_id": distinct_site_id,
            "coordinate": [float(coordinate[0]), float(coordinate[1])],
            "status": "confirmed",
            "source": suitability.get("source") or "navigation_site_suitability",
            "planning_origin": origin,
            "site_suitability": suitability,
            "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
            "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
            "maturity": PLANNING_UNIT_MATURITY,
        }
        if _reference_station_installed(suitability, origin):
            provider["reference_station_installed"] = True
            confirmed.append(provider)
        else:
            provider["reference_station_installed"] = False
            provider["status"] = "eligible_not_installed"
            not_installed.append(provider)
    return confirmed, unconfirmed, not_installed


def _reference_station_installed(suitability, origin):
    """该站址上是否**已经建成**地面导航增强参考站（默认 fail-closed）。"""

    declared = suitability.get("reference_station_installed")
    if declared is not None:
        return declared is True
    # 未显式声明时：只有"已有 CNS 设施"来源可视为已建成；共塔 / 候选站址上的
    # suitability 确认只表示"获准安装"，绝不在基线 provider 池里充数。
    return origin == "existing_cns_facility"


def _distinct_site_id(site):
    """物理站址身份：优先复用 canonical helper（Round A 解析顺序）。"""

    from .cns_service_contract import distinct_site_id_for

    identity = {"site_id": site.get("site_id"), "facility_id": site.get("facility_id")}
    if site.get("distinct_site_id"):
        identity["distinct_site_id"] = site.get("distinct_site_id")
    metadata = site.get("metadata") if isinstance(site.get("metadata"), dict) else None
    if metadata:
        identity["metadata"] = metadata
    return distinct_site_id_for(identity, site)


def _reuse_tier_rank(provider):
    return {
        "existing_cns_facility": 0,
        "tower_colocation_host": 1,
        "candidate_site": 2,
    }.get(str(provider.get("planning_origin") or ""), 3)


# ---------------------------------------------------------------------------
# 4. canonical navigation augmentation evidence
# ---------------------------------------------------------------------------


def build_navigation_service_evidence(
    required_cns, *, route_ids=None, existing_facilities=None,
    candidate_sites=None, tower_colocation=None,
):
    """构造 canonical navigation augmentation evidence；未显式要求时返回 ``None``。

    返回的 evidence 同时携带：

    * 唯一 policy authority（``max_reference_baseline_m`` /
      ``required_distinct_site_count`` / ``delivery_service_key``，缺失即 ``None``）；
    * **已确认**的 reference-station provider 列表（site suitability fail-closed）。

    同一物理站址上的多个导航设备只算一个 provider（``distinct_site_id`` 去重），
    绝不按 device / antenna 数量增加冗余。
    """

    explicit_ids = [
        str(route_id) for route_id in (route_ids or [])
        if navigation_requirement_for(required_cns, route_id) is not None
    ]
    for route_id in _override_route_ids(required_cns):
        if route_id not in explicit_ids:
            explicit_ids.append(route_id)
    explicit_ids = sorted({route_id for route_id in explicit_ids if route_id})
    if not explicit_ids:
        return None

    project_planning = navigation_planning(required_cns, None)
    routes = []
    for route_id in explicit_ids:
        requirement = navigation_requirement_for(required_cns, route_id)
        if requirement is None:
            requirement = navigation_requirement_for(required_cns, None) or {}
        planning = requirement.get("planning")
        if not isinstance(planning, dict):
            planning = project_planning or {}
        if not planning and project_planning:
            planning = project_planning
        policy = planning_policy_status(planning)
        routes.append({
            "route_id": route_id,
            "status": "current",
            "planning": deepcopy(planning),
            "policy": policy,
            "service_confirmed": requirement.get("confirmed") is True,
        })

    providers, unconfirmed_providers, planning_candidates = collect_navigation_sites(
        existing_facilities=existing_facilities,
        candidate_sites=candidate_sites,
        tower_colocation=tower_colocation,
    )
    result = {
        "service_key": SERVICE_KEY,
        "subsystem": SUBSYSTEM,
        "service_subtype": "navigation_augmentation",
        "technology": "gnss_rtk",
        "planner_family": "navigation_reference_station",
        "model": BASELINE_MODEL,
        "model_semantics": BASELINE_SEMANTICS,
        "provider_semantics": "ground_reference_station_not_airborne_receiver",
        "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
        "planning_unit_maturity": PLANNING_UNIT_MATURITY,
        "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
        "supported_delivery_service_keys": [SERVICE_KEY_COMMUNICATION],
        "routes": routes,
        "providers": providers,
        "unconfirmed_providers": unconfirmed_providers,
        "planning_candidates": planning_candidates,
        "provider_count": len(providers),
        "distinct_site_count": len(providers),
        "counting_basis": "distinct_site_id",
        "not_evaluated": {
            "device_model": "not_selected",
            "manufacturer_specification": "not_evaluated",
            "measured_service_distance": "not_evaluated",
            "terrain_or_obstruction": "not_evaluated",
            "ionosphere_or_multipath": "not_evaluated",
            "network_rtk_integrity": "not_evaluated",
        },
    }
    result["input_fingerprint"] = stable_fingerprint(
        {
            "service_key": SERVICE_KEY,
            "type": {"service_subtype": "navigation_augmentation", "technology": "gnss_rtk"},
            "routes": routes,
            "providers": providers,
            "planning_candidates": planning_candidates,
            "model": BASELINE_MODEL,
            "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
        },
        prefix="navservice-",
    )
    return result


def route_evidence(evidence, route_id):
    return next(
        (item for item in (evidence or {}).get("routes") or []
         if str(item.get("route_id")) == str(route_id)),
        None,
    )


def navigation_facilities(evidence, route_id=None):
    """把 evidence 的 confirmed provider 转成 caller-owned 设施集合。

    这是纯数据适配：provider 的坐标与物理站址身份原样保留，
    **不写回**正式 ``existing_cns_facilities``。
    """

    items, seen = [], set()
    for provider in (evidence or {}).get("providers") or []:
        key = str(provider.get("distinct_site_id") or provider.get("provider_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({
            "facility_id": provider.get("provider_id"),
            "site_id": provider.get("site_id"),
            "name": f"Navigation reference station {provider.get('site_id') or key}",
            "coordinate": deepcopy(provider.get("coordinate")),
            "vertical_profile": None,
            "devices": [],
            "status": "active",
            "source": provider.get("source"),
            "distinct_site_id": provider.get("distinct_site_id"),
            "metadata": {"navigation_reference_station_provider": True},
        })
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# 5. per-voxel evidence
# ---------------------------------------------------------------------------


def evidence_for_probe(evidence, route_id, probe, *, delivery=None):
    """计算某个 P14 voxel probe 的 navigation augmentation 证据。

    ``delivery`` 是**上游已算好的** correction delivery 依赖事实
    （来自同一个 P14 voxel 的 canonical ``C:communication`` 服务证据），
    形如 ``{"service_key": "C:communication", "status": ..., "target_id": ...
    , "evidence_fingerprint": ...}``。本函数**不重新计算** Communication。
    """

    if evidence is None:
        return None
    route = route_evidence(evidence, route_id)
    probe = probe if isinstance(probe, dict) else {}
    voxel_id = probe.get("voxel_id")
    coordinate = [probe.get("longitude"), probe.get("latitude")]
    base = {
        "service_key": SERVICE_KEY,
        "subsystem": SUBSYSTEM,
        "service_subtype": "navigation_augmentation",
        "technology": "gnss_rtk",
        "planner_family": "navigation_reference_station",
        "surface_dependent": False,
        "supports_site_planning": True,
        "voxel_id": voxel_id,
        "route_id": str(route_id),
        "coordinate": coordinate,
        "nearest_route_offset_m": probe.get("nearest_route_offset_m"),
        "counting_basis": "distinct_site_id",
        "model": BASELINE_MODEL,
        "model_semantics": BASELINE_SEMANTICS,
        "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
        "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
        "device_id": None,
        "providers": [],
        "distinct_site_ids": [],
        "distinct_site_count": None,
        "required_distinct_site_count": None,
        "max_reference_baseline_m": None,
        "geometry_status": STATUS_UNKNOWN,
        "delivery_service_key": None,
        "delivery_status": "not_evaluated",
        "delivery_reason": None,
        "dependency_only": False,
        "recommended_dependency_service": None,
        "gap_causes": [],
        "evidence_required_reasons": [],
        "source_evidence_fingerprint": (evidence or {}).get("input_fingerprint"),
        "input_fingerprint": (evidence or {}).get("input_fingerprint"),
    }
    if route is None:
        base["evidence_required_reasons"] = ["navigation_augmentation_route_evidence_missing"]
        base["status"] = STATUS_UNKNOWN
        return _finish(base, evidence)
    policy = route.get("policy") or {}
    base["max_reference_baseline_m"] = policy.get("max_reference_baseline_m")
    base["required_distinct_site_count"] = policy.get("required_distinct_site_count")
    delivery_key = policy.get("delivery_service_key")
    base["delivery_service_key"] = delivery_key
    #: policy 的权威性是 ``planning`` 块自己的事实（Round A 契约）：
    #: ``planning.confirmed`` 与两个工程变量一起决定可用性。服务级 ``confirmed``
    #: 只作为审计信息保留，绝不用它冒充 planning policy 的确认。
    base["service_confirmed"] = route.get("service_confirmed") is True
    if policy.get("ready") is not True:
        base["evidence_required_reasons"] = [policy.get("reason") or REASON_POLICY_NOT_CONFIRMED]
        base["status"] = STATUS_UNKNOWN
        return _resolve_dependency(base, evidence, delivery)

    baseline = float(policy["max_reference_baseline_m"])
    required = int(policy["required_distinct_site_count"])
    counted = _providers_within_baseline(evidence, coordinate, baseline)
    base["providers"] = counted
    base["distinct_site_ids"] = [item["distinct_site_id"] for item in counted]
    base["distinct_site_count"] = len(counted)
    if len(counted) >= required:
        base["geometry_status"] = STATUS_SATISFIED
        base["status"] = STATUS_SATISFIED
    elif _has_unconfirmed_site_evidence(evidence):
        # 仍有 suitability **未确认**的候选站：它既不能作为 confirmed eligible
        # action，也不构成"已确认没有站"的反证 ⇒ 结论保持 unknown（fail-closed），
        # 绝不把未确认候选当成已确认缺口。
        base["geometry_status"] = STATUS_UNKNOWN
        base["status"] = STATUS_UNKNOWN
        base["evidence_required_reasons"] = [
            "navigation_site_suitability_unconfirmed_candidates",
        ]
    else:
        base["geometry_status"] = STATUS_CONFIRMED_DEFICIT
        base["status"] = STATUS_CONFIRMED_DEFICIT
    return _resolve_dependency(base, evidence, delivery)


def _providers_within_baseline(evidence, coordinate, baseline):
    result = []
    if not _coordinate_usable(coordinate):
        return result
    for provider in (evidence or {}).get("providers") or []:
        site = provider.get("coordinate")
        if not _coordinate_usable(site):
            continue
        try:
            distance = distance_m(coordinate, site)
        except (TypeError, ValueError):
            continue
        if distance > float(baseline):
            continue
        result.append({
            "provider_id": provider.get("provider_id"),
            "distinct_site_id": provider.get("distinct_site_id"),
            "site_id": provider.get("site_id"),
            "facility_id": provider.get("facility_id"),
            "coordinate": deepcopy(site),
            "baseline_distance_m": distance,
            "within_baseline": True,
            "planning_origin": provider.get("planning_origin"),
            "source": provider.get("source"),
            "site_suitability": deepcopy(provider.get("site_suitability")),
            "equipment_selection_status": EQUIPMENT_SELECTION_STATUS,
            "planning_unit": REFERENCE_STATION_PLANNING_UNIT,
        })
    return sorted(result, key=lambda item: (item["baseline_distance_m"], str(item["distinct_site_id"])))


def _has_unconfirmed_site_evidence(evidence):
    return any(
        str(provider.get("status") or "") == "unconfirmed_suitability"
        for provider in (evidence or {}).get("unconfirmed_providers") or []
    )


def _resolve_dependency(base, evidence, delivery):
    """把 reference-station geometry 与 correction delivery 依赖合成最终状态。

    规则（Round C 冻结）：

    * ``delivery_service_key = None`` ⇒ 不得假设传输存在 ⇒ ``unknown``；
    * geometry satisfied + delivery satisfied ⇒ ``satisfied``；
    * geometry satisfied + delivery deficit ⇒ ``confirmed_deficit``
      （cause = ``correction_delivery_deficit``，``dependency_only = true``）；
    * geometry satisfied + delivery unknown ⇒ ``unknown``；
    * geometry deficit + delivery deficit ⇒ ``confirmed_deficit``，**两个 cause 都保留**；
    * geometry deficit + delivery unknown ⇒ ``confirmed_deficit``（已知缺口优先于未知），
      但 ``unknown`` 证据保留在 ``evidence_required_reasons`` / ``delivery_status``。
    """

    geometry = base.get("geometry_status")
    key = base.get("delivery_service_key")
    if key is None:
        base["delivery_status"] = "not_configured"
        base["delivery_reason"] = REASON_DELIVERY_NOT_CONFIGURED
        base["evidence_required_reasons"] = list(base.get("evidence_required_reasons") or []) + [
            REASON_DELIVERY_NOT_CONFIGURED,
        ]
        if geometry == STATUS_CONFIRMED_DEFICIT:
            # 已知的站址缺口优先于未知：不能因为 delivery 未配置就把已确认缺口
            # 降级成 unknown，但 correction delivery 的未知证据仍完整保留。
            base["gap_causes"] = [CAUSE_REFERENCE_STATION_DEFICIT]
            base["status"] = STATUS_CONFIRMED_DEFICIT
        else:
            base["status"] = STATUS_UNKNOWN
        return _finish(base, evidence)

    dependency = delivery if isinstance(delivery, dict) else None
    if dependency is None:
        base["delivery_status"] = STATUS_UNKNOWN
        base["delivery_reason"] = REASON_DELIVERY_UNKNOWN
    else:
        base["delivery_status"] = str(dependency.get("status") or STATUS_UNKNOWN)
        base["delivery_reason"] = dependency.get("reason")
        base["delivery_evidence_fingerprint"] = dependency.get("evidence_fingerprint")
        base["delivery_target_id"] = dependency.get("target_id")

    delivery_status = base["delivery_status"]
    causes = []
    if geometry == STATUS_CONFIRMED_DEFICIT:
        causes.append(CAUSE_REFERENCE_STATION_DEFICIT)
    if delivery_status == STATUS_CONFIRMED_DEFICIT:
        causes.append(CAUSE_CORRECTION_DELIVERY_DEFICIT)
    base["gap_causes"] = causes
    base["dependency_only"] = bool(
        geometry == STATUS_SATISFIED and delivery_status == STATUS_CONFIRMED_DEFICIT
    )
    base["recommended_dependency_service"] = (
        SERVICE_KEY_COMMUNICATION if causes and CAUSE_REFERENCE_STATION_DEFICIT not in causes
        else None
    )

    if causes:
        base["status"] = STATUS_CONFIRMED_DEFICIT
        if delivery_status == STATUS_UNKNOWN:
            base["evidence_required_reasons"] = list(base.get("evidence_required_reasons") or []) + [
                REASON_DELIVERY_UNKNOWN,
            ]
        elif delivery_status == STATUS_CONFIRMED_DEFICIT and CAUSE_REFERENCE_STATION_DEFICIT not in causes:
            base["evidence_required_reasons"] = list(base.get("evidence_required_reasons") or []) + [
                REASON_DELIVERY_DEFICIT,
            ]
    elif delivery_status == STATUS_UNKNOWN:
        base["status"] = STATUS_UNKNOWN
        base["evidence_required_reasons"] = list(base.get("evidence_required_reasons") or []) + [
            REASON_DELIVERY_UNKNOWN,
        ]
    elif geometry == STATUS_SATISFIED:
        base["status"] = STATUS_SATISFIED
    else:
        # 站址几何仍未确认（例如 suitability 未确认的候选站、或 policy 未确认）：
        # 即使 delivery 已满足，也**绝不**判成 satisfied（fail-closed）。
        base["status"] = STATUS_UNKNOWN
    return _finish(base, evidence)


def _finish(base, evidence):
    base["reasons"] = sorted(set(base.get("evidence_required_reasons") or []))
    base["input_fingerprint"] = stable_fingerprint(
        {
            "source_evidence_fingerprint": (evidence or {}).get("input_fingerprint"),
            "route_id": base.get("route_id"),
            "voxel_id": base.get("voxel_id"),
            "coordinate": base.get("coordinate"),
            "max_reference_baseline_m": base.get("max_reference_baseline_m"),
            "required_distinct_site_count": base.get("required_distinct_site_count"),
            "delivery_service_key": base.get("delivery_service_key"),
            "distinct_site_ids": base.get("distinct_site_ids"),
            "geometry_status": base.get("geometry_status"),
            "delivery_status": base.get("delivery_status"),
            "delivery_evidence_fingerprint": base.get("delivery_evidence_fingerprint"),
            "status": base.get("status"),
        },
        prefix="navvoxel-",
    )
    return base


def delivery_dependency_from_communication(communication_entry):
    """从**同一个 P14 voxel** 的 canonical ``C:communication`` 服务证据取依赖事实。

    本函数只**读取**已算好的 Communication 结果，绝不重新计算、也绝不消费
    ``available_subsystems`` 之类的"可用性声明"来假定传输存在。
    """

    entry = communication_entry if isinstance(communication_entry, dict) else None
    if entry is None:
        return None
    status = str(entry.get("status") or "").strip()
    mapping = {
        "satisfied": STATUS_SATISFIED,
        "confirmed_deficit": STATUS_CONFIRMED_DEFICIT,
        "unknown": STATUS_UNKNOWN,
    }
    return {
        "service_key": SERVICE_KEY_COMMUNICATION,
        "status": mapping.get(status, STATUS_UNKNOWN),
        "reason": (
            None if status in mapping
            else f"communication_service_evidence_{status or 'missing'}"
        ),
        "target_id": entry.get("target_id"),
        "evidence_fingerprint": (
            entry.get("input_fingerprint") or entry.get("source_evidence_fingerprint")
        ),
    }


def _coordinate_usable(coordinate):
    return (
        isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2
        and coordinate[0] is not None and coordinate[1] is not None
    )


def _override_route_ids(required_cns):
    result = []
    for route_id, requirements in ((required_cns or {}).get("route_overrides") or {}).items():
        navigation = (requirements or {}).get("navigation") or {}
        if service_requirement_for("N", navigation, SERVICE_KEY) is not None:
            result.append(str(route_id))
    return result


__all__ = [
    "BASELINE_MODEL", "BASELINE_SEMANTICS", "CAUSE_CORRECTION_DELIVERY_DEFICIT",
    "CAUSE_REFERENCE_STATION_DEFICIT", "EQUIPMENT_SELECTION_STATUS",
    "PLANNING_UNIT_MATURITY", "REFERENCE_STATION_PLANNING_UNIT",
    "REASON_BASELINE_MISSING", "REASON_DELIVERY_DEFICIT",
    "REASON_DELIVERY_NOT_CONFIGURED", "REASON_DELIVERY_UNKNOWN",
    "REASON_POLICY_NOT_CONFIRMED", "REASON_SITE_COUNT_MISSING", "SERVICE_KEY",
    "STATUS_CONFIRMED_DEFICIT", "STATUS_SATISFIED", "STATUS_UNKNOWN",
    "SUBSYSTEM", "SUITABILITY_FIELDS",
    "build_navigation_service_evidence", "collect_navigation_sites",
    "delivery_dependency_from_communication", "evidence_for_probe",
    "navigation_facilities", "navigation_planning", "navigation_requirement_for",
    "normalize_navigation_site_suitability", "planning_policy_status",
    "route_evidence", "site_suitability_for",
]
