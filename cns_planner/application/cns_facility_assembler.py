"""CNS 设施装配 authority（**只读**）：canonical 结果 → 统一设施记录。

为什么必须有这一个模块
======================

通信 / RID / Radar / 导航完整性四张专题图（再加综合图）如果各自写一遍"从
``state["cns_corridor_site_plan"]["selected_actions"]`` 里挑自己那一类动作"的解析逻辑，
就会出现四套互相漂移的语义：某一张图把 ``action["confirmed"]`` 当成方案已确认、
另一张图把合成既有设施画成真实设施、第三张图把 Radar 候选塔画成已建 Radar 站。

因此本模块是 **CNS 专题制图的唯一设施 authority**：四张模板只消费这里产出的同一份
:class:`CnsFacilityRecord` 列表，不再自己解析任何 canonical 容器。

严格边界
========

* **只读**：不写任何 state / canonical 容器，不调用 ``session.save()``，
  不重算 P16 / P17 / Radar / RequiredCNS / LandMask 的任何业务结论。
* **不新造事实**：所有坐标、服务类型、半径、确认状态都逐字来自 canonical 结果；
  缺数据一律记 ``unavailable`` / ``unknown`` 并给出中文原因，绝不补 0、绝不偏移坐标。
* **确认语义唯一来源**：``confirmed_cns_plan.status``。P16 action 自带的
  ``confirmed`` 字段描述的是"**规划宿主 / 设备档案**已确认"，它**不等于**方案通过
  Step6 正式确认；两者在这里被显式区分，避免把规划提案画成已建设施。
* **同址多业务不靠坐标错位**：同一条真实坐标上的多个服务记录逐字节共享同一坐标，
  由渲染层用多符号叠加表达。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math

from ..reporting.map_templates import (
    CNS_COVERAGE_POLICY, SURVEILLANCE_SERVICE_RADAR, SURVEILLANCE_SERVICE_RID,
)

#: canonical 容器名（**只读**引用）。
SITE_PLAN_KEY = "cns_corridor_site_plan"
LEGACY_SITE_PLAN_KEY = "cns_site_plan"
EXISTING_FACILITIES_KEY = "existing_cns_facilities"
EXISTING_BASELINE_KEY = "cns_existing_baseline"
CONFIRMED_PLAN_KEY = "confirmed_cns_plan"
RADAR_LAYOUT_KEY = "radar_surveillance_layout"
CONTINUOUS_SERVICE_KEY = "continuous_service_acceptability"
NODES_KEY = "nodes"

#: 服务键（与 ``cns_service_contract`` 一致的 canonical 字符串）。
SERVICE_COMMUNICATION = "C:communication"
SERVICE_RID = "S:rid_cooperative"
SERVICE_RADAR = "S:radar_noncooperative"
SERVICE_NAVIGATION_INTEGRITY = "N:navigation_integrity_monitoring"

#: 身份：既有基线设施 vs P16 规划提案。
IDENTITY_EXISTING = "existing"
IDENTITY_PROPOSAL = "proposal"

#: 服务家族（图例与配色按家族取色）。
FAMILY_OF_SERVICE = {
    SERVICE_COMMUNICATION: "communication",
    SERVICE_RID: "rid",
    SERVICE_RADAR: "radar",
    SERVICE_NAVIGATION_INTEGRITY: "navigation",
}

#: 合成 / 工程验证数据的登记标记：这类设施**不是**现实中的既有设施，
#: 只有显式打开 ``show_synthetic_existing_facilities`` 才会进入图面，
#: 并且永远带着 ``dataset_class`` / ``real_world_facility`` 披露字段。
SYNTHETIC_DATASET_CLASSES = ("synthetic_engineering_validation", "synthetic")

#: 图面用的中文服务名（标签后缀与图例说明共用）。
SERVICE_LABEL_SUFFIX = {
    SERVICE_COMMUNICATION: "通信",
    SERVICE_RID: "RID",
    SERVICE_RADAR: "雷达",
    SERVICE_NAVIGATION_INTEGRITY: "导航",
}


def _finite(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _short(text, limit=16):
    value = str(text or "").strip()
    if not value:
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _coordinate_of(item):
    """坐标解析（**唯一入口**）：``coordinate`` 优先，其次 ``longitude`` / ``latitude``。"""

    if not isinstance(item, dict):
        return None, None
    coordinate = item.get("coordinate")
    longitude = latitude = None
    if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        longitude, latitude = _finite(coordinate[0]), _finite(coordinate[1])
    if longitude is None or latitude is None:
        longitude = _finite(item.get("longitude"))
        latitude = _finite(item.get("latitude"))
    if longitude is None or latitude is None:
        return None, None
    if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
        return None, None
    if longitude == 0.0 and latitude == 0.0:
        return None, None
    return longitude, latitude


@dataclass
class CnsFacilityRecord:
    """一件 CNS 设施 / 规划提案（既有与提案共用同一结构，靠 ``identity`` 区分）。"""

    record_id: str
    identity: str
    service_key: str
    family: str
    display_name: str
    longitude: float
    latitude: float
    subsystem: str = ""
    action_id: str = ""
    facility_id: str = ""
    device_id: str = ""
    site_id: str = ""
    tower_id: str = ""
    distinct_site_id: str = ""
    planner_family: str = ""
    endpoint_role: str = ""
    reuse_class: str = ""
    surface_class: str = ""
    #: **方案确认**（Step6）。规划提案在当前项目恒为 ``False``。
    confirmed: bool = False
    #: 上游记录自带的"宿主 / 设备档案已确认"标记（**不等于**方案确认）。
    source_confirmed: bool = False
    proposal_only: bool = True
    requires_site_survey: bool = False
    physical_mount_confirmed: bool = False
    coverage_radius_m: float | None = None
    source_result_fingerprint: str = ""
    source_container: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "record_id": self.record_id,
            "identity": self.identity,
            "service_key": self.service_key,
            "family": self.family,
            "display_name": self.display_name,
            "longitude": float(self.longitude),
            "latitude": float(self.latitude),
            "subsystem": self.subsystem,
            "action_id": self.action_id,
            "facility_id": self.facility_id,
            "device_id": self.device_id,
            "site_id": self.site_id,
            "tower_id": self.tower_id,
            "distinct_site_id": self.distinct_site_id,
            "planner_family": self.planner_family,
            "endpoint_role": self.endpoint_role,
            "reuse_class": self.reuse_class,
            "surface_class": self.surface_class,
            "confirmed": bool(self.confirmed),
            "source_confirmed": bool(self.source_confirmed),
            "proposal_only": bool(self.proposal_only),
            "requires_site_survey": bool(self.requires_site_survey),
            "physical_mount_confirmed": bool(self.physical_mount_confirmed),
            "coverage_radius_m": self.coverage_radius_m,
            "source_result_fingerprint": self.source_result_fingerprint,
            "source_container": self.source_container,
            "detail": deepcopy(self.detail),
        }


@dataclass
class CnsFacilityAssembly:
    """四张 CNS 模板共用的装配结果（全部 JSON-safe）。"""

    facilities: list = field(default_factory=list)
    existing: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    source_status: dict = field(default_factory=dict)
    boundaries: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    def for_service(self, service_key):
        return [item for item in self.facilities if item.service_key == service_key]


# --------------------------------------------------------------------------- 确认语义

def resolve_confirmation_semantics(state, *, route_id=None):
    """P16 方案的确认状态**唯一来源**：``confirmed_cns_plan``。

    返回 ``(confirmed, detail)``。当前权威项目是 ``status="not_confirmed"``，
    因此所有 ``selected_actions`` 一律表达为"规划提案（未确认）"。绝不因为
    action 自带 ``confirmed=true``（那是宿主 / 设备档案层面）而改变这里的结论。
    """

    plan = state.get(CONFIRMED_PLAN_KEY)
    plan = plan if isinstance(plan, dict) else {}
    status = str(plan.get("status") or "not_confirmed")
    confirmed = status == "confirmed"
    return confirmed, {
        "confirmed_plan_status": status,
        "confirmed_plan_id": plan.get("plan_id"),
        "confirmed_plan_decision": plan.get("decision"),
        "confirmation_source": CONFIRMED_PLAN_KEY,
        "confirmation_allowed": bool(
            (state.get("cns_plan_review") or {}).get("confirmation_allowed")
            if isinstance(state.get("cns_plan_review"), dict) else False
        ),
        "route_id": route_id,
        "semantics": (
            "p16_selected_actions_are_planning_proposals_until_confirmed_cns_plan_confirms_them"
        ),
        "proposal_label": "规划提案（未确认）" if not confirmed else "已确认方案",
    }


def resolve_step6_gate(state):
    """Step6 门禁（只读）：用于图件页脚披露"为什么当前方案未通过正式确认"。"""

    review = state.get("cns_plan_review")
    review = review if isinstance(review, dict) else {}
    continuous = state.get(CONTINUOUS_SERVICE_KEY)
    continuous = continuous if isinstance(continuous, dict) else {}
    return {
        "confirmation_allowed": bool(review.get("confirmation_allowed")),
        "plan_status": review.get("status"),
        "continuous_service_status": continuous.get("status"),
        "continuous_service_primary_threat_status": continuous.get("primary_threat_status"),
        "p17_algorithm_version": continuous.get("algorithm_version"),
        "p17_input_fingerprint": continuous.get("input_fingerprint"),
        "p17_plan_stage": continuous.get("plan_stage"),
    }


# --------------------------------------------------------------------------- P16

def _site_plan(state):
    for key in (SITE_PLAN_KEY, LEGACY_SITE_PLAN_KEY):
        plan = state.get(key)
        if isinstance(plan, dict) and plan:
            return key, plan
    return SITE_PLAN_KEY, {}


def _host_name(action):
    host = action.get("host")
    if isinstance(host, dict) and host.get("host_tower_name"):
        return _short(host.get("host_tower_name"))
    return ""


def _proposal_display_name(action, fallback):
    """提案名称：优先真实宿主名，其次真实 endpoint 节点名，最后退回服务名。"""

    name = _host_name(action)
    if name:
        return name
    takeoff = str(action.get("takeoff_landing_site_id") or action.get("site_id") or "").strip()
    return takeoff or fallback


def p16_facility_records(state, route_id, *, confirmed, node_names=None):
    """P16 ``selected_actions`` → 提案记录（**只保留该 route 的动作**）。

    绝不使用 ``candidate_actions``：未选中的候选站不是真实提案，画出来就是伪造。
    """

    container, plan = _site_plan(state)
    fingerprint = str(plan.get("input_fingerprint") or "")
    actions = plan.get("selected_actions") or []
    node_names = node_names or {}
    records, skipped = [], []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_route = str(action.get("route_id") or "")
        if route_id and action_route and action_route != str(route_id):
            skipped.append({"reason": "other_route", "action_id": action.get("action_id")})
            continue
        service_key = str(action.get("service_key") or "")
        if service_key not in FAMILY_OF_SERVICE:
            skipped.append({"reason": "unregistered_service_key", "action_id": action.get("action_id")})
            continue
        longitude, latitude = _coordinate_of(action)
        if longitude is None or latitude is None:
            skipped.append({"reason": "coordinate_missing", "action_id": action.get("action_id")})
            continue
        fallback = SERVICE_LABEL_SUFFIX.get(service_key, "规划提案")
        name = _proposal_display_name(action, fallback)
        site_id = str(action.get("site_id") or "")
        if site_id in node_names and not _host_name(action):
            name = node_names[site_id]
        tower_id = action.get("tower_id")
        endpoint_role = str(action.get("endpoint_role") or "")
        records.append(CnsFacilityRecord(
            record_id=str(action.get("action_id") or action.get("device_id") or site_id),
            identity=IDENTITY_PROPOSAL,
            service_key=service_key,
            family=FAMILY_OF_SERVICE[service_key],
            display_name=name,
            longitude=longitude,
            latitude=latitude,
            subsystem=str(action.get("subsystem") or ""),
            action_id=str(action.get("action_id") or ""),
            facility_id=str(action.get("facility_id") or ""),
            device_id=str(action.get("device_id") or ""),
            site_id=site_id,
            # endpoint 服务**没有** tower_id（监测点画在真实起降点上，不是铁塔）。
            tower_id=str(tower_id) if tower_id else "",
            distinct_site_id=str(action.get("distinct_site_id") or ""),
            planner_family=str(action.get("planner_family") or action.get("reuse_class") or ""),
            endpoint_role=endpoint_role,
            reuse_class=str(action.get("reuse_class") or ""),
            surface_class=str(action.get("surface_class") or ""),
            # 方案确认状态**只**来自 confirmed_cns_plan；action 自带的 confirmed
            # 只登记为 source_confirmed（宿主 / 设备档案层面）。
            confirmed=bool(confirmed),
            source_confirmed=bool(action.get("confirmed")),
            proposal_only=bool(action.get("proposal_only", True)),
            requires_site_survey=bool(action.get("requires_site_survey")),
            physical_mount_confirmed=bool(action.get("physical_mount_confirmed")),
            coverage_radius_m=_finite(action.get("coverage_radius_m")),
            source_result_fingerprint=fingerprint,
            source_container=container,
            detail={
                "action_type": action.get("action_type"),
                "planning_unit": action.get("planning_unit"),
                "equipment_selection_status": action.get("equipment_selection_status"),
                "required_redundancy": action.get("required_redundancy"),
                "distance_to_route_m": _finite(action.get("distance_to_route_m")),
                "eligibility": deepcopy(action.get("eligibility") or {}),
                "eligibility_basis": deepcopy(action.get("eligibility_basis") or {}),
                "planning_origin_status": action.get("origin_status"),
                "subsystem_mount_status": action.get("subsystem_mount_status"),
                "plan_status": plan.get("status"),
                "algorithm_id": plan.get("algorithm_id"),
                "algorithm_version": plan.get("algorithm_version"),
                "semantics": "p16_selected_action_planning_proposal_not_confirmed_facility",
            },
        ))
    return records, {
        "container": container,
        "status": plan.get("status"),
        "algorithm_id": plan.get("algorithm_id"),
        "algorithm_version": plan.get("algorithm_version"),
        "input_fingerprint": fingerprint,
        "selected_action_count": len(actions),
        "route_matched_count": len(records),
        "skipped": skipped[:8],
        "skipped_count": len(skipped),
        "candidate_actions_used": False,
        "semantics": "only_selected_actions_never_unselected_candidates",
    }


# --------------------------------------------------------------------------- 既有设施

def existing_facility_records(state, *, include_synthetic=False):
    """既有 CNS 设施（真实基线）→ 设施记录。

    **合成 / 工程验证数据默认被排除**：当前权威项目的 ``existing_cns_facilities``
    唯一一条记录带 ``dataset_class="synthetic_engineering_validation"`` 与
    ``real_world_facility=False``，把它画成"既有设施"就是伪造现实。默认不画，
    并在 ``evidence`` 里如实登记；只有显式打开 ``include_synthetic`` 才进入图面。
    """

    collection = state.get(EXISTING_FACILITIES_KEY)
    collection = collection if isinstance(collection, dict) else {}
    baseline = state.get(EXISTING_BASELINE_KEY)
    baseline = baseline if isinstance(baseline, dict) else {}
    items = [item for item in (collection.get("items") or []) if isinstance(item, dict)]
    records, excluded = [], []
    for facility in items:
        metadata = facility.get("metadata") if isinstance(facility.get("metadata"), dict) else {}
        dataset_class = str(metadata.get("dataset_class") or "")
        real_world = metadata.get("real_world_facility")
        synthetic = (
            dataset_class in SYNTHETIC_DATASET_CLASSES
            or real_world is False
        )
        if synthetic and not include_synthetic:
            excluded.append({
                "facility_id": facility.get("facility_id"),
                "dataset_class": dataset_class or None,
                "real_world_facility": real_world,
                "reason": "合成 / 工程验证基线设施，默认不作为现实既有设施绘制",
            })
            continue
        longitude, latitude = _coordinate_of(facility)
        if longitude is None or latitude is None:
            excluded.append({
                "facility_id": facility.get("facility_id"),
                "reason": "坐标缺失或非法",
            })
            continue
        for device in facility.get("devices") or []:
            if not isinstance(device, dict):
                continue
            service_key = str(
                device.get("service_key")
                or _device_service_key(device)
            )
            if service_key not in FAMILY_OF_SERVICE:
                continue
            coverage = device.get("coverage_geometry")
            coverage = coverage if isinstance(coverage, dict) else {}
            radius_by_surface = coverage.get("radius_by_surface")
            radius_by_surface = radius_by_surface if isinstance(radius_by_surface, dict) else {}
            records.append(CnsFacilityRecord(
                record_id=f"{facility.get('facility_id')}:{device.get('device_id')}",
                identity=IDENTITY_EXISTING,
                service_key=service_key,
                family=FAMILY_OF_SERVICE[service_key],
                display_name=_short(facility.get("name")) or str(
                    facility.get("facility_id") or "既有设施"
                ),
                longitude=longitude,
                latitude=latitude,
                subsystem=str(device.get("subsystem") or ""),
                facility_id=str(facility.get("facility_id") or ""),
                device_id=str(device.get("device_id") or ""),
                site_id=str(facility.get("site_id") or facility.get("facility_id") or ""),
                distinct_site_id=str(facility.get("site_id") or facility.get("facility_id") or ""),
                planner_family="existing_cns_facility",
                reuse_class=str(
                    (facility.get("planning_profile") or {}).get("reuse_class") or ""
                ) if isinstance(facility.get("planning_profile"), dict) else "",
                confirmed=True,
                source_confirmed=True,
                proposal_only=False,
                coverage_radius_m=_finite(radius_by_surface.get("land")),
                source_result_fingerprint=f"existingcns:{collection.get('collection_id')}:"
                                          f"{len(items)}",
                source_container=EXISTING_FACILITIES_KEY,
                detail={
                    "device_status": device.get("status"),
                    "dataset_class": dataset_class or None,
                    "real_world_facility": real_world,
                    "radius_by_surface_m": deepcopy(radius_by_surface),
                    "baseline_knowledge_status": baseline.get("knowledge_status"),
                    "baseline_planning_mode": baseline.get("planning_mode"),
                    "semantics": "confirmed_existing_cns_baseline_facility",
                },
            ))
    evidence = {
        "container": EXISTING_FACILITIES_KEY,
        "collection_status": collection.get("status"),
        "declared_count": collection.get("count"),
        "baseline_knowledge_status": baseline.get("knowledge_status"),
        "baseline_planning_mode": baseline.get("planning_mode"),
        "baseline_evidence_ref": baseline.get("evidence_ref"),
        "include_synthetic": bool(include_synthetic),
        "real_facility_records": len(records),
        "excluded": excluded[:8],
        "excluded_count": len(excluded),
        "semantics": "existing_cns_facilities_only_real_world_records_are_drawable_by_default",
    }
    if not records and excluded:
        evidence["note"] = (
            "本项目 existing_cns_facilities 的记录全部是合成 / 工程验证设施"
            "（real_world_facility=false），默认不作为现实既有设施绘制；"
            "既有 CNS 基线状态见 baseline_knowledge_status。"
        )
    return records, evidence


def _device_service_key(device):
    """设备 → 服务键：只读 device 的 ``type`` 字段，绝不推断新服务。"""

    device_type = device.get("type")
    device_type = device_type if isinstance(device_type, dict) else {}
    subsystem = str(device.get("subsystem") or "")
    technology = str(device_type.get("technology") or "")
    subtype = str(device_type.get("service_subtype") or "")
    if subsystem == "C":
        return SERVICE_COMMUNICATION
    if subtype == "navigation_integrity_monitoring" or technology == "gnss_integrity_monitoring":
        return SERVICE_NAVIGATION_INTEGRITY
    if technology == "network_remote_id" or subtype == "cooperative_surveillance":
        return SERVICE_RID
    if subtype == "noncooperative_surveillance":
        return SERVICE_RADAR
    return ""


# --------------------------------------------------------------------------- Radar

def radar_layout_for_route(state, route_id, altitude_layer_id=None):
    """Radar 划设结果（**只取本 route 自身那一条**）。

    绝不使用别的航路（例如 R0003）的历史 items：那是 stale 的另一条航路结论，
    拿它画 R0005 等于伪造覆盖。
    """

    collection = state.get(RADAR_LAYOUT_KEY)
    collection = collection if isinstance(collection, dict) else {}
    items = [item for item in (collection.get("items") or []) if isinstance(item, dict)]
    matched = [
        item for item in items
        if str(item.get("route_id") or "") == str(route_id or "")
        and (not altitude_layer_id or str(item.get("altitude_layer_id") or "") == str(altitude_layer_id))
    ]
    other = [
        {
            "route_id": item.get("route_id"),
            "altitude_layer_id": item.get("altitude_layer_id"),
            "status": item.get("status"),
            "selected_panel_count": item.get("selected_panel_count"),
            "algorithm_version": item.get("algorithm_version"),
            "stale_reason": item.get("stale_reason"),
        }
        for item in items
        if str(item.get("route_id") or "") != str(route_id or "")
    ]
    item = matched[0] if matched else {}
    gap = item.get("radar_gap") if isinstance(item.get("radar_gap"), dict) else {}
    solver = item.get("solver") if isinstance(item.get("solver"), dict) else {}
    selected_panels = item.get("selected_panels") or []
    selected_tower_ids = item.get("selected_tower_ids") or []
    limitations = [
        entry for entry in (
            (state.get(CONTINUOUS_SERVICE_KEY) or {}).get("limitations") or []
        )
        if isinstance(entry, dict)
        and str(entry.get("route_id") or "") == str(route_id or "")
        and str(entry.get("layer") or "") == "noncooperative"
    ] if isinstance(state.get(CONTINUOUS_SERVICE_KEY), dict) else []
    return {
        "available": bool(item),
        "route_id": item.get("route_id"),
        "altitude_layer_id": item.get("altitude_layer_id"),
        "altitude_m": _finite(item.get("altitude_m")),
        "algorithm_id": item.get("algorithm_id"),
        "algorithm_version": item.get("algorithm_version"),
        "status": item.get("status"),
        "gap_reason": item.get("gap_reason") or gap.get("gap_reason"),
        "gap_classification": item.get("gap_classification") or gap.get("gap_classification"),
        "managed_physical_gap": (
            item.get("managed_physical_gap")
            if item.get("managed_physical_gap") is not None
            else gap.get("managed_physical_gap")
        ),
        "infeasibility_proven": solver.get("infeasibility_proven"),
        "solver_status": solver.get("status"),
        "greedy_fallback_used": solver.get("greedy_fallback_used"),
        "selected_panel_count": int(item.get("selected_panel_count") or 0),
        "selected_panels": list(selected_panels),
        "selected_tower_count": int(item.get("selected_tower_count") or 0),
        "selected_tower_ids": [str(value) for value in selected_tower_ids],
        "radar_i_panel_count": int(item.get("radar_i_panel_count") or 0),
        "radar_ii_panel_count": int(item.get("radar_ii_panel_count") or 0),
        "candidate_tower_count": int(item.get("candidate_tower_count") or 0),
        "candidate_panel_count": int(item.get("candidate_panel_count") or 0),
        "candidate_tower_ids": _candidate_tower_ids(item),
        "input_fingerprint": item.get("input_fingerprint"),
        "infeasibility_reasons": list(item.get("infeasibility_reasons") or [])[:6],
        "infeasibility_reason_count": len(item.get("infeasibility_reasons") or []),
        "limitations": deepcopy(limitations),
        "collection_status": collection.get("status"),
        "other_route_items": other,
        "other_route_items_used": False,
        "policy": deepcopy(CNS_COVERAGE_POLICY.get(SERVICE_RADAR, {})),
        "semantics": "radar_layout_result_for_this_route_only_never_other_routes",
    }


def _candidate_tower_ids(item):
    """Radar-I **评估过的候选塔**（只是上下文，绝不是已建/已选站点）。"""

    statistics = item.get("candidate_statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    ids = []
    for entry in statistics.get("per_tower") or []:
        if isinstance(entry, dict) and entry.get("tower_id"):
            ids.append(str(entry["tower_id"]))
    if ids:
        return ids
    return [str(value) for value in (item.get("selected_tower_ids") or [])]


# --------------------------------------------------------------------------- 综合装配

def _node_names(state):
    names = {}
    for node in state.get(NODES_KEY) or []:
        if isinstance(node, dict) and node.get("node_id"):
            names[str(node["node_id"])] = _short(node.get("name"))
    return names


def _inside_extent(record, extent):
    if extent is None:
        return True
    return (
        float(extent.west) <= record.longitude <= float(extent.east)
        and float(extent.south) <= record.latitude <= float(extent.north)
    )


def assemble(state, route_id, *, extent=None, parameters=None):
    """四张 CNS 模板共用的**唯一**装配入口（只读）。"""

    parameters = parameters or {}
    state = state if isinstance(state, dict) else {}
    confirmed, confirmation = resolve_confirmation_semantics(state, route_id=route_id)
    node_names = _node_names(state)

    proposals, plan_evidence = p16_facility_records(
        state, route_id, confirmed=confirmed, node_names=node_names,
    )
    existing, existing_evidence = existing_facility_records(
        state,
        include_synthetic=bool(parameters.get("show_synthetic_existing_facilities")),
    )
    radar = radar_layout_for_route(
        state, route_id,
        altitude_layer_id=_altitude_layer_id(state, route_id),
    )

    warnings = []
    if not confirmed:
        warnings.append(
            "P16 selected_actions 是规划提案：confirmed_cns_plan.status="
            f"{confirmation['confirmed_plan_status']}，图上不得标为已确认设施"
        )
    if existing_evidence.get("note"):
        warnings.append(existing_evidence["note"])
    if radar.get("status") in ("infeasible", "stale"):
        warnings.append(
            f"Radar 划设对 {route_id} 的结论是 {radar.get('status')}："
            f"selected_panel_count={radar.get('selected_panel_count')}，"
            "图中不得出现 Radar 站址或扇区"
        )

    facilities = list(existing) + list(proposals)
    inside = [item for item in facilities if _inside_extent(item, extent)]
    outside = [item for item in facilities if not _inside_extent(item, extent)]
    step6 = resolve_step6_gate(state)
    return CnsFacilityAssembly(
        facilities=inside,
        existing=[item for item in inside if item.identity == IDENTITY_EXISTING],
        proposals=[item for item in inside if item.identity == IDENTITY_PROPOSAL],
        warnings=warnings,
        source_status={
            "confirmation": confirmation,
            "step6_gate": step6,
            "p16": plan_evidence,
            "existing_cns": existing_evidence,
            "radar": {
                key: radar.get(key) for key in (
                    "available", "route_id", "altitude_layer_id", "algorithm_version", "status",
                    "gap_reason", "gap_classification", "managed_physical_gap",
                    "infeasibility_proven", "selected_panel_count", "selected_tower_count",
                    "candidate_tower_count", "input_fingerprint", "collection_status",
                    "other_route_items_used",
                    #: Round31-C：分级规划结论必须随证据链一起可见，否则下游
                    #: （P15/P16/报告）看到的是"只有 I 型"的假象。
                    "stage", "radar_i_panel_count", "radar_ii_panel_count",
                    "radar_ii_site_count", "automatic_radar_ii_escalation", "escalation",
                )
            },
        },
        boundaries={
            "read_only": True,
            "writes_state": False,
            "recomputes_business_results": False,
            "uses_unselected_candidates": False,
            "uses_other_route_radar_items": False,
            "manufactures_radar_sites": False,
            "proposal_never_drawn_as_confirmed": True,
            "colocated_services_share_identical_coordinate": True,
        },
        evidence={
            "confirmation": confirmation,
            "step6_gate": step6,
            "p16": plan_evidence,
            "existing_cns": existing_evidence,
            "radar": radar,
            "facility_count": len(facilities),
            "facility_count_inside_extent": len(inside),
            "outside_extent": [item.to_dict() for item in outside][:6],
            "outside_extent_count": len(outside),
        },
    )


def _altitude_layer_id(state, route_id):
    """route 的固定高度层：只读 Radar 策略与 route 结果，不推断。"""

    collection = state.get(RADAR_LAYOUT_KEY)
    if isinstance(collection, dict):
        for item in collection.get("items") or []:
            if isinstance(item, dict) and str(item.get("route_id") or "") == str(route_id or ""):
                value = str(item.get("altitude_layer_id") or "")
                if value:
                    return value
    return None


# --------------------------------------------------------------------------- 覆盖几何

def coverage_circle_ring(longitude, latitude, radius_m, *, metric_crs="EPSG:32651",
                         segments=72):
    """**规划服务半径**的圆形环（WGS84 ``[[lon, lat], ...]``，闭合）。

    圆在显式米制 CRS 中按真实距离构造，再转回 WGS84 —— 因此它不是"按经度差近似"的
    椭圆。该几何只表达 ``planning service radius``，不是实测无线传播等值线。
    """

    radius = _finite(radius_m)
    if radius is None or radius <= 0:
        return []
    from pyproj import CRS, Transformer
    from shapely.geometry import Point
    from shapely.ops import transform as shapely_transform

    to_metric = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(metric_crs), always_xy=True,
    )
    to_wgs = Transformer.from_crs(
        CRS.from_user_input(metric_crs), CRS.from_epsg(4326), always_xy=True,
    )
    x, y = to_metric.transform(float(longitude), float(latitude))
    quad_segs = max(8, int(segments) // 4)
    circle = Point(x, y).buffer(radius, quad_segs=quad_segs)
    projected = shapely_transform(lambda xs, ys: to_wgs.transform(xs, ys), circle)
    if projected.is_empty:
        return []
    exterior = projected.exterior
    if exterior is None:  # pragma: no cover - 缓冲区必然有外环
        return []
    return [[round(float(px), 9), round(float(py), 9)] for px, py in exterior.coords]


def coverage_radius_for(record, *, surface_class=None):
    """一条记录在当前图面上的规划服务半径（米）。

    取值顺序：上游动作自带 ``coverage_radius_m`` → canonical 服务半径策略
    （按该记录真实 surface_class 取 land / coastal / sea 档）。
    **绝不**为了让圆好看而放宽半径。
    """

    if record.coverage_radius_m:
        return float(record.coverage_radius_m)
    policy = CNS_COVERAGE_POLICY.get(record.service_key)
    policy = policy if isinstance(policy, dict) else {}
    radius_by_surface = policy.get("radius_by_surface_m")
    radius_by_surface = radius_by_surface if isinstance(radius_by_surface, dict) else {}
    if not radius_by_surface:
        return None
    surface = str(surface_class or record.surface_class or "").strip()
    classes = [
        value.strip() for value in surface.replace(";", ",").split(",") if value.strip()
    ]
    candidates = []
    for name in classes:
        if name in radius_by_surface:
            candidates.append(_finite(radius_by_surface[name]))
        elif name == "coastal" and "coastal_uncertain" in radius_by_surface:
            candidates.append(_finite(radius_by_surface["coastal_uncertain"]))
    candidates = [value for value in candidates if value]
    if candidates:
        # 多表面混合站址：取**最大**半径，让覆盖圈覆盖它真实服务的所有表面。
        return max(candidates)
    return _finite(radius_by_surface.get("land"))


def rid_radius_pair():
    """RID 的两个登记半径（陆上/沿海 2 km 实线、海上最大 5 km 虚线）。"""

    policy = CNS_COVERAGE_POLICY.get(SERVICE_RID, {})
    radius_by_surface = policy.get("radius_by_surface_m") or {}
    return _finite(radius_by_surface.get("land")), _finite(radius_by_surface.get("sea"))


__all__ = [
    "CONFIRMED_PLAN_KEY", "CONTINUOUS_SERVICE_KEY", "CnsFacilityAssembly", "CnsFacilityRecord",
    "EXISTING_BASELINE_KEY", "EXISTING_FACILITIES_KEY", "FAMILY_OF_SERVICE",
    "IDENTITY_EXISTING", "IDENTITY_PROPOSAL", "NODES_KEY", "RADAR_LAYOUT_KEY",
    "SERVICE_COMMUNICATION", "SERVICE_LABEL_SUFFIX", "SERVICE_NAVIGATION_INTEGRITY",
    "SERVICE_RADAR", "SERVICE_RID", "SITE_PLAN_KEY", "SYNTHETIC_DATASET_CLASSES",
    "assemble", "coverage_circle_ring", "coverage_radius_for", "existing_facility_records",
    "p16_facility_records", "radar_layout_for_route", "resolve_confirmation_semantics",
    "resolve_step6_gate", "rid_radius_pair",
]
