"""Round38-SITE-2 — Radar 候选站址的**共享站址资源接入层**（只读，不判定业务结论）。

Radar 保留自己既有的专用规划算法（``radar_layout``：定向单面阵候选 → dominated
剪枝 → MILP → 独立的 5 m 验证与 gap 裁定）。本模块**只**做一件事：把统一候选站址
资源投影成该算法可消费的**站址记录**，使它与其它 CNS 服务共用同一批真实站址。

支持的站址来源（与 :class:`cns_planner.application.cns_shared_site_candidate_service.CNSSharedSiteCandidateService`
的候选种类一一对应）::

    tower_colocation      → 373 座既有铁塔（塔顶安装位置，沿用既有塔顶正高建模）
    project_node          → 项目已登记起降点 / 节点（地面或场站安装位置）
    reference_landing_site→ 参考起降点目录中未与项目节点链接的记录（同上）
    existing_cns_facility → 真实、已显式确认的既有 CNS 设施站址（同上）

三条不可协商的边界
------------------

1. **候选资格 ≠ 已安装设备**：``radar_candidate`` 只说明该站址在**站址资源**意义上
   合格；服务资格一律是 ``not_evaluated``，安装条件缺失时是 ``blocked``。
2. **地面站绝不继承塔顶高度**：塔的雷达原点沿用
   ``tower_top_orthometric_m``；地面/场站站址的原点是
   ``地表正高（FABDEM，egm2008_orthometric） + 天线安装高度（显式证据）``。
   两者任一缺失即 ``blocked`` 且**不产生任何候选** —— 绝不填 0、绝不用塔顶兜底。
3. **没有第二条真实性通道**：合成 / 工程验证设施与未显式确认的设施由共享投影
   层排除；本模块只消费已经过门禁的候选，不自己发明站址、不生成自由坐标。
"""

from __future__ import annotations

from copy import deepcopy

from .cns_shared_site_candidate_service import CNSSharedSiteCandidateService
from ..domain.route_safety_evidence_v2 import stable_fingerprint
from ..gis.radar_layout_adapter import (
    PLANNING_ESTIMATE_ORIGIN_SOURCE as _PLANNING_ESTIMATE_ORIGIN_SOURCE,
    resolve_radar_origins,
)

#: 塔顶安装位置语义（V1.1 既有语义，保持逐字不变）。
INSTALLATION_POSITION_TOWER_TOP = "tower_top"
#: 地面 / 场站安装位置语义（SITE-2 新增；**不得**套用塔顶高度）。
INSTALLATION_POSITION_GROUND_SITE = "ground_site"

#: 允许作为雷达安装高度证据的来源标记（**只有这两类是显式证据**）。
#: ``engineering_assumption`` / ``not_configured`` 不是证据，绝不产生几何。
INSTALLATION_EVIDENCE_SOURCES = ("source_device_installation_height", "user_confirmed")

#: 唯一被接受的垂向基准（与既有塔顶/地形链同一基准）。
VERTICAL_REFERENCE = "egm2008_orthometric"

#: 未被共享投影提供者支持时的显式原因（不是"没有站址"，而是"未接线"）。
SHARED_PROVIDER_UNAVAILABLE = "shared_site_provider_unavailable"

#: 工程估算原点的显式来源标记（与 ``radar_layout_adapter`` **共用同一常量**，
#: 避免两处字面量漂移导致"规划用了估算、展示却查不到来源"）。
PLANNING_ESTIMATE_ORIGIN_SOURCE = _PLANNING_ESTIMATE_ORIGIN_SOURCE


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or abs(value) == float("inf"):
        return None
    return value


def _ground_site_coordinate(item):
    coordinate = item.get("coordinate")
    if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        longitude, latitude = _finite(coordinate[0]), _finite(coordinate[1])
        if longitude is not None and latitude is not None:
            return longitude, latitude
    return None


def installation_height_evidence(evidence, *, site_id, record=None):
    """规范化一站点址的**显式**雷达天线安装高度证据。

    只有同时满足下列条件才返回可用高度：

    * 存在数值 ``installation_height_m``（或 ``height_m``）且有限、非负；
    * ``source`` 属于 :data:`INSTALLATION_EVIDENCE_SOURCES`；
    * ``confirmed is True``。

    其余一律返回 ``(None, reason)``：缺失是 ``unknown``，不是 0，也不是塔顶高度。
    """

    source_id = str(site_id)
    payload = evidence if isinstance(evidence, dict) else {}
    record = record if isinstance(record, dict) else {}
    if not payload:
        #: 兼容读取站址记录自带的安装证据块（``radar_installation``）；它同样必须
        #: 显式声明来源与确认，仍然不接受任何默认挂高。
        candidate_block = record.get("radar_installation")
        payload = candidate_block if isinstance(candidate_block, dict) else {}
    if not payload:
        return None, "ground_site_radar_installation_height_unresolved"
    height = _finite(payload.get("installation_height_m"))
    if height is None:
        height = _finite(payload.get("height_m"))
    if height is None or height < 0:
        return None, "ground_site_radar_installation_height_invalid"
    if str(payload.get("source") or "") not in INSTALLATION_EVIDENCE_SOURCES:
        return None, "ground_site_radar_installation_height_source_not_evidenced"
    if payload.get("confirmed") is not True:
        return None, "ground_site_radar_installation_height_unconfirmed"
    return height, None


def sampled_ground_elevations(items, *, terrain_sampler):
    """用既有 FABDEM 采样器解析地面站地表正高；不可用一律 ``unresolved``（绝不填 0）。

    ``items`` 每项需要 ``site_id`` / ``longitude`` / ``latitude``。
    """

    result = {
        str(item.get("site_id") or ""): {
            "status": "unresolved", "elevation_m": None,
            "vertical_reference": "unknown", "reason": "terrain_source_not_configured",
        }
        for item in items or []
    }
    if not callable(terrain_sampler) or not items:
        return result
    points = [
        {
            "key": str(item.get("site_id") or ""),
            "longitude": _finite(item.get("longitude")),
            "latitude": _finite(item.get("latitude")),
        }
        for item in items or []
    ]
    if any(point["longitude"] is None or point["latitude"] is None for point in points):
        return result
    try:
        sampled = terrain_sampler(points)
    except Exception as exc:  # pragma: no cover - GIS 运行时失败
        return {
            key: {
                "status": "unresolved", "elevation_m": None,
                "vertical_reference": "unknown",
                "reason": f"terrain_sampling_failed:{exc}",
            }
            for key in result
        }
    sampled = sampled if isinstance(sampled, dict) else {}
    for key in result:
        fact = sampled.get(key) or {}
        elevation = fact.get("elevation_m")
        vertical = str(fact.get("vertical_reference") or "")
        passed = (
            str(fact.get("status") or "") == "passed"
            and elevation is not None
            and vertical == VERTICAL_REFERENCE
        )
        result[key] = {
            "status": "passed" if passed else "unresolved",
            "elevation_m": float(elevation) if passed else None,
            "vertical_reference": vertical or "unknown",
            "reason": None if passed else str(
                fact.get("reason") or "ground_site_terrain_elevation_unresolved"
            ),
            "rejected_vertical_reference": None if passed or not vertical else vertical,
        }
    return result


def _tower_record(candidate):
    return {
        "site_id": str(candidate.get("source_id") or ""),
        "tower_id": str(candidate.get("source_id") or ""),
        "name": candidate.get("name"),
        "candidate_kind": candidate.get("candidate_kind"),
        "site_type": candidate.get("site_type"),
        "longitude": (candidate.get("coordinate") or [None, None])[0],
        "latitude": (candidate.get("coordinate") or [None, None])[1],
        "installation_position": INSTALLATION_POSITION_TOWER_TOP,
        "radar_origin_basis": "tower_top_orthometric_m",
        "radar_candidate": False,
        #: 原点来源身份：默认无来源（严格塔顶事实或显式工程估算都尚未接线）。
        "origin_egm2008_m": None,
        "origin_source": None,
        "origin_estimate": False,
        "origin_reason": None,
        "device_installed": False,
        "service_qualification_status": "not_evaluated",
        "block_reason": None,
        "required_evidence": [],
        "source_crs": deepcopy(candidate.get("source_crs")),
        "provenance": deepcopy(candidate.get("provenance")),
        "data_source": deepcopy(candidate.get("data_source")),
    }


def _ground_record(candidate, *, candidate_kind):
    return {
        "site_id": str(candidate.get("source_id") or ""),
        "name": candidate.get("name"),
        "candidate_kind": candidate_kind,
        "site_type": candidate.get("site_type"),
        "longitude": (candidate.get("coordinate") or [None, None])[0],
        "latitude": (candidate.get("coordinate") or [None, None])[1],
        "installation_position": INSTALLATION_POSITION_GROUND_SITE,
        "radar_origin_basis": "ground_site_antenna_installation",
        "radar_candidate": False,
        "device_installed": False,
        "existing_device_ids": deepcopy(
            (candidate.get("existing_installation") or {}).get("device_ids") or []
        ),
        "service_qualification_status": "not_evaluated",
        "block_reason": None,
        "required_evidence": [VERTICAL_REFERENCE, "radar_antenna_installation_height"],
        "source_crs": deepcopy(candidate.get("source_crs")),
        "provenance": deepcopy(candidate.get("provenance")),
        "data_source": deepcopy(candidate.get("data_source")),
    }


def radar_candidate_sites(*, shared_index, installation_evidence=None,
                          terrain_sampler=None, planning_origin_estimates=None):
    """把统一候选站址投影成 Radar 专用站址输入记录。

    返回 ``{"sites", "usable", "blocked", "statistics"}``：

    * ``sites``   —— 全部真实站址记录（含被阻断者，便于审计与前端披露）；
    * ``usable``  —— 通过输入适配层、可交给既有 Radar 算法评估的站址；
    * ``blocked`` —— 保留身份与原因，``radar_candidate=False``。

    输入适配层**只**做来源身份、CRS 证据与安装高度证据的门禁；覆盖收益、可行性与
    gap 裁定仍然完全由既有 ``radar_layout`` 算法负责。
    """

    evidence = installation_evidence if isinstance(installation_evidence, dict) else {}
    estimates = (
        planning_origin_estimates if isinstance(planning_origin_estimates, dict) else {}
    )
    index = shared_index if isinstance(shared_index, dict) else {}
    candidates = index.get("candidates")
    if candidates is None:
        return {
            "sites": [], "usable": [], "blocked": [],
            "statistics": {
                "status": "unavailable",
                "reason": str(index.get("status") or SHARED_PROVIDER_UNAVAILABLE),
                "source_policy": "shared_site_candidates_only",
                "site_count": 0, "usable_count": 0, "blocked_count": 0,
            },
        }

    sites, ground_pairs = [], []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        kind = str(candidate.get("candidate_kind") or "")
        if kind == "tower_colocation":
            record = _tower_record(candidate)
            estimate = _finite(estimates.get(record["site_id"]))
            if estimate is not None:
                #: 既有共塔工程估算路径的产物（显式 opt-in 时由调用方传入）。它只在
                #: 严格塔顶事实未解析时才被当作规划原点，且必须保留估算身份标记。
                record["planning_origin_estimate_egm2008_m"] = estimate
            if candidate.get("spatial_use_status") != "eligible":
                record["block_reason"] = str(
                    candidate.get("spatial_block_reason") or "spatial_evidence_blocked"
                )
            else:
                record["radar_candidate"] = True
            sites.append(record)
            continue
        if kind not in ("project_node", "reference_landing_site", "existing_cns_facility"):
            continue
        record = _ground_record(candidate, candidate_kind=kind)
        sites.append(record)
        ground_pairs.append((record, candidate))

    heights = {}
    height_reasons = {}
    evidence_payloads = {}
    for record, candidate in ground_pairs:
        site_id = record["site_id"]
        payload = evidence.get(site_id)
        if not isinstance(payload, dict):
            payload = candidate.get("radar_installation")
        evidence_payloads[site_id] = payload if isinstance(payload, dict) else {}
        heights[site_id], height_reasons[site_id] = installation_height_evidence(
            evidence.get(site_id), site_id=site_id, record=candidate,
        )
    # 没有已确认安装高度时不打开 FABDEM；缺证先按缺证 blocked，而不是刷新页面即采样 DEM。
    elevations = sampled_ground_elevations(
        [record for record, candidate in ground_pairs
         if candidate.get("spatial_use_status") == "eligible"
         and heights.get(record["site_id"]) is not None],
        terrain_sampler=terrain_sampler,
    )
    blocked_reasons = {}
    for record, candidate in ground_pairs:
        site_id = record["site_id"]
        if candidate.get("spatial_use_status") != "eligible":
            record["block_reason"] = str(
                candidate.get("spatial_block_reason") or "spatial_evidence_blocked"
            )
            blocked_reasons[record["block_reason"]] = (
                blocked_reasons.get(record["block_reason"], 0) + 1
            )
            continue
        height, height_reason = heights.get(site_id), height_reasons.get(site_id)
        elevation = elevations.get(site_id) or {}
        ground = _finite(elevation.get("elevation_m"))
        record["ground_elevation_status"] = elevation.get("status")
        record["ground_elevation_reason"] = elevation.get("reason")
        record["ground_elevation_egm2008_m"] = ground
        record["installation_height_m"] = height
        if height is None:
            record["block_reason"] = str(height_reason)
        elif elevation.get("status") != "passed":
            record["block_reason"] = str(
                elevation.get("reason") or "ground_site_terrain_elevation_unresolved"
            )
        if record["block_reason"]:
            blocked_reasons[record["block_reason"]] = (
                blocked_reasons.get(record["block_reason"], 0) + 1
            )
            continue
        record["radar_candidate"] = True
        record["radar_origin_egm2008_m"] = ground + height
        #: 已解析的两项证据原样回传，使 GIS 适配层不必再看第二份证据表；它仍然
        #: 会逐项复核来源与确认标记，绝不接受无来源的数值。
        record["ground_elevation"] = {
            "elevation_m": ground, "vertical_reference": VERTICAL_REFERENCE,
            "status": "passed",
        }
        payload = evidence_payloads.get(site_id) or {}
        record["antenna_installation_height"] = {
            "installation_height_m": height,
            "source": str(payload.get("source") or ""),
            "confirmed": True,
            "installation_status": payload.get("installation_status"),
            "height_semantics": payload.get("height_semantics"),
            "device_installed": payload.get("device_installed") is True,
        }
        record["device_installed"] = payload.get("device_installed") is True

    usable, blocked = [], []
    for record in sites:
        if record.get("radar_candidate"):
            usable.append(record)
        else:
            blocked.append(record)
    kind_counts = {}
    for record in sites:
        key = str(record.get("candidate_kind") or "")
        kind_counts[key] = kind_counts.get(key, 0) + 1
    tower_records = [
        record for record in sites
        if record.get("installation_position") == INSTALLATION_POSITION_TOWER_TOP
    ]
    return {
        "sites": sites,
        "usable": usable,
        "blocked": blocked,
        "statistics": {
            "status": "passed",
            "source_policy": "shared_site_candidates_only",
            "installation_position_policy": {
                INSTALLATION_POSITION_TOWER_TOP: "tower_top_orthometric_m",
                INSTALLATION_POSITION_GROUND_SITE:
                    "ground_elevation_plus_evidenced_antenna_installation_height",
            },
            "ground_site_inherits_tower_top_height": False,
            "unknown_is_not_zero": True,
            "site_count": len(sites),
            "usable_count": len(usable),
            "blocked_count": len(blocked),
            "source_kind_counts": dict(sorted(kind_counts.items())),
            "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
            #: Round38-INT-A：塔站址原点来源分层（严格塔顶 vs 显式工程估算）。
            #: 两者都进入工程规划候选，但语义与权威性严格不同，必须分别计数。
            #: 原点在 ``radar_site_inputs`` 的补全阶段才落到记录上，因此这三个计数字段
            #: 由该阶段（唯一知道最终 ``usable`` 记录的地方）回填。
            "tower_site_count": len(tower_records),
            "tower_strict_origin_count": None,
            "tower_planning_estimate_origin_count": None,
            "planning_estimate_origin_source": PLANNING_ESTIMATE_ORIGIN_SOURCE,
            "planning_estimate_is_not_confirmed_tower_top": True,
        },
    }


def candidate_site_fingerprint_component(snapshot):
    """统一候选站址的**廉价确定性**指纹分量（站点身份 + 空间资格 + CRS 证据）。

    它使"站址集合或资格一变，旧 Radar 方案必然 stale"可解释；不含任何几何计算、
    不做地形采样，因此进指纹是安全的。
    """

    index = snapshot if isinstance(snapshot, dict) else {"status": SHARED_PROVIDER_UNAVAILABLE}
    candidates = index.get("candidates")
    if not isinstance(candidates, list):
        return {
            "source": "shared_site_candidates",
            "status": str(index.get("status") or "unavailable"),
            "count": 0,
            "fingerprint": None,
        }
    return {
        "source": "shared_site_candidates",
        "status": index.get("status"),
        "count": len(candidates),
        "fingerprint": stable_fingerprint(
            [
                {
                    "candidate_id": item.get("candidate_id"),
                    "candidate_kind": item.get("candidate_kind"),
                    "coordinate": item.get("coordinate"),
                    "spatial_use_status": item.get("spatial_use_status"),
                    "spatial_block_reason": item.get("spatial_block_reason"),
                    "source_crs": (item.get("source_crs") or {}).get("value"),
                    "source_crs_confirmed": (
                        (item.get("source_crs") or {}).get("confirmed") is True
                    ),
                }
                for item in sorted(
                    (candidate for candidate in candidates if isinstance(candidate, dict)),
                    key=lambda candidate: str(candidate.get("candidate_id") or ""),
                )
            ],
            prefix="radarcandidatesites-",
        ),
    }


def resolve_shared_site_origins(sites, *, mount_assumption=None,
                                planning_origin_estimates=None):
    """用 GIS 边界的既有原点解析器解析统一站址的雷达原点（塔顶 / 地面站各自语义）。

    ``planning_origin_estimates``（可选）沿用既有共塔工程估算路径：只在严格塔顶事实
    未解析时生效，生效时由解析器逐条标注估算身份；本函数不自己推导任何高度。
    """

    resolved = resolve_radar_origins(
        towers=list(sites or []), obstacle_profiles={},
        mount_assumption=mount_assumption,
        ground_site_installation_heights=None,
        ground_site_elevations=None,
        planning_origin_estimates=planning_origin_estimates,
    )
    return resolved


def _profile_items(obstacle_profiles):
    """``tower_obstacle_profiles`` 的 ``tower_id -> 档案`` 索引（历史两种形态都接受）。"""

    profiles = obstacle_profiles if isinstance(obstacle_profiles, dict) else {}
    items = profiles.get("items") if isinstance(profiles.get("items"), dict) else profiles
    return items if isinstance(items, dict) else {}


def radar_site_inputs(*, shared_snapshot=None, session=None, shared_sites=None,
                      landing_sites_provider=None, towers_provider=None,
                      existing_facilities_provider=None, installation_evidence=None,
                      terrain_sampler=None, mount_assumption=None,
                      obstacle_profiles=None, planning_origin_estimates=None):
    """组合入口：共享候选站址 → Radar 站址输入 → 既有原点解析器。

    站址来源只有**一条**通道：与生产完全相同的
    :class:`CNSSharedSiteCandidateService` 快照（``shared_sites`` 实例或显式注入的
    三个 provider 之一，二者都走同一实现），不存在第二套站址口径。

    ``obstacle_profiles`` 只用于给**经过站址门禁的铁塔**补上既有
    ``tower_top_orthometric_m`` 事实——塔顶高度仍然由既有
    ``tower_obstacle_profiles`` 建模，本模块绝不自己推导塔高、也绝不把它用于地面站。

    ``planning_origin_estimates`` 是**可选**的 ``{tower_id: 估算原点 EGM2008 正高}``：
    它只来自既有共塔工程估算路径（``terrain_plus_source_tower_height``），并且只有
    调用方**显式打开**该工程假设时才传入。它**只**在严格塔顶事实未解析时生效，
    生效时逐条标注 ``origin_source`` / ``origin_estimate`` / 限制说明，
    **不**升级为已确认塔顶正高、**不**用于地面站、**不**替代 LOS 验证。
    """

    estimates = (
        planning_origin_estimates if isinstance(planning_origin_estimates, dict) else {}
    )
    snapshot = shared_snapshot if isinstance(shared_snapshot, dict) else None
    if snapshot is None:
        if shared_sites is not None:
            snapshot = shared_sites.snapshot()
        elif session is not None and callable(landing_sites_provider):
            snapshot = CNSSharedSiteCandidateService(
                session, landing_sites_provider, towers_provider,
                existing_facilities_provider,
            ).snapshot()
        else:
            snapshot = {"status": SHARED_PROVIDER_UNAVAILABLE}
    result = radar_candidate_sites(
        shared_index=snapshot, installation_evidence=installation_evidence,
        terrain_sampler=terrain_sampler,
        planning_origin_estimates=estimates,
    )
    profiles = _profile_items(obstacle_profiles)
    for record in result["usable"]:
        if record.get("installation_position") != INSTALLATION_POSITION_TOWER_TOP:
            continue
        profile = profiles.get(record["site_id"]) or {}
        top = _finite(profile.get("tower_top_orthometric_m"))
        if top is not None:
            record["origin_egm2008_m"] = top
            record["origin_source"] = "tower_top_orthometric_m"
            record["origin_reason"] = None
        else:
            # 严格塔顶事实未解析：只有在调用方**显式打开**既有共塔工程估算策略时，
            # 才退回该估算原点，并且必须逐条标注估算身份与限制。
            estimate = _finite(record.get("planning_origin_estimate_egm2008_m"))
            if estimate is not None:
                record["origin_egm2008_m"] = estimate
                record["origin_source"] = PLANNING_ESTIMATE_ORIGIN_SOURCE
                record["origin_parameter_origin"] = "engineering_estimate"
                record["origin_estimate"] = True
                record["origin_estimate_limitations"] = [
                    "工程估算 = FABDEM 地形正高 + 源表塔身高度；"
                    "不是已确认 EGM2008 塔顶正高",
                    "未计入潜在建筑屋面高度（楼面塔可能低估真实安装高程）",
                    "仅用于明确声明假设的工程规划与非权威展示；"
                    "不得据此认定 Radar LOS 已验证通过",
                ]
                record["origin_reason"] = (
                    "tower_top_egm2008_unresolved_planning_estimate_used:"
                    f"{profile.get('vertical_status') or 'tower_obstacle_profile_missing'}"
                )
            elif not record.get("origin_reason"):
                record["origin_reason"] = (
                    "tower_top_egm2008_unresolved:"
                    f"{profile.get('vertical_status') or 'tower_obstacle_profile_missing'}"
                )
    result["shared_snapshot_status"] = snapshot.get("status")
    # 服务层以顶层 status 判定共享站址通道是否已接通；缺少该字段会让完整结果被误判
    # 为 unavailable，并静默回退到 tower-only 路径。
    result["status"] = (result.get("statistics") or {}).get("status")
    result["resolved"] = resolve_shared_site_origins(
        result["usable"], mount_assumption=mount_assumption,
        planning_origin_estimates=estimates,
    )
    #: 原点来源分层计数必须在这里回填：只有本阶段同时掌握"最终可用的塔站址"与
    #: "该记录的原点到底来自严格塔顶事实还是显式工程估算"。
    tower_records = [
        record for record in result["usable"]
        if record.get("installation_position") == INSTALLATION_POSITION_TOWER_TOP
    ]
    statistics = result.get("statistics")
    if isinstance(statistics, dict):
        statistics["tower_site_count"] = len(tower_records)
        statistics["tower_strict_origin_count"] = sum(
            1 for record in tower_records
            if isinstance(record.get("origin_egm2008_m"), (int, float))
            and record.get("origin_source") == "tower_top_orthometric_m"
        )
        statistics["tower_planning_estimate_origin_count"] = sum(
            1 for record in tower_records
            if record.get("origin_source") == PLANNING_ESTIMATE_ORIGIN_SOURCE
        )
    return result


__all__ = [
    "INSTALLATION_EVIDENCE_SOURCES", "INSTALLATION_POSITION_GROUND_SITE",
    "INSTALLATION_POSITION_TOWER_TOP", "PLANNING_ESTIMATE_ORIGIN_SOURCE",
    "SHARED_PROVIDER_UNAVAILABLE",
    "VERTICAL_REFERENCE", "installation_height_evidence", "radar_candidate_sites",
    "radar_site_inputs", "resolve_shared_site_origins", "sampled_ground_elevations",
]
