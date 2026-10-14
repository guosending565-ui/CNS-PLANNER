"""三维 CNS 站址与设备身份的**只读投影**（Round38-T3D4）。

职责边界（违反即返工）：

* **不发明身份**：站址身份**只**来自既有共享候选站址投影
  （:class:`cns_planner.application.cns_shared_site_candidate_service.CNSSharedSiteCandidateService`），
  安装位置语义**只**由站址种类与显式身份字段决定，绝不从"站址存在"推断"设备已安装"；
* **不发明高度**：铁塔塔顶正高只来自既有 ``tower_obstacle_profiles``；地面站高度只来自
  既有显式安装高度证据（``radar_shared_site_adapter.installation_height_evidence``）；
  缺证一律 ``unknown``，**绝不填 0、绝不用塔顶兜底、绝不从站址推断覆盖**；
* **不重跑算法**：不调用 Theta*/MILP/P14/P17/覆盖计算，只做只读重采样；
* **零写入**：不 import 写入权威、不 ``save()``、不递增 ``revision``、不触发失效传播。

为什么需要这一层：二维系统已经按 ``C`` / ``N`` / ``RID`` / ``Radar`` 四类设备身份
表达布设状态，三维若各自再解析一遍站址与设备关系，就会出现"二维说未安装、三维画成
已布设"的身份分叉。这里把四类身份的解析与配色口径收在一个模块里，由三维服务与前端
共同消费同一份记录。
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.tower_colocation import (
    tower_planning_service_origin_estimates as _tower_planning_service_origin_estimates,
)

#: 设备类别（与前端 ``CNS_SERVICE_STYLE`` 键一一对应）。
SERVICE_CATEGORY_COMMUNICATION = "C"
SERVICE_CATEGORY_NAVIGATION = "N"
SERVICE_CATEGORY_RID = "RID"
SERVICE_CATEGORY_RADAR = "Radar"
SERVICE_CATEGORY_OTHER = "other"

#: 设备类别显示名（中文界面口径，与二维 ``service_semantics.js`` 逐字一致）。
SERVICE_CATEGORY_LABELS = {
    SERVICE_CATEGORY_COMMUNICATION: "通信",
    SERVICE_CATEGORY_NAVIGATION: "导航",
    SERVICE_CATEGORY_RID: "RID 合作监视",
    SERVICE_CATEGORY_RADAR: "Radar 非合作监视",
    SERVICE_CATEGORY_OTHER: "未归类",
}

#: 设备类别配色（需求指定：C 绿 / N 黄 / RID 橙 / Radar 蓝）。
SERVICE_CATEGORY_COLORS = {
    SERVICE_CATEGORY_COMMUNICATION: "#37d67a",
    SERVICE_CATEGORY_NAVIGATION: "#ffd166",
    SERVICE_CATEGORY_RID: "#ff9f43",
    SERVICE_CATEGORY_RADAR: "#4d9dff",
    SERVICE_CATEGORY_OTHER: "#9fb0c0",
}

#: 安装位置语义（塔顶 / 地面站），与 ``radar_shared_site_adapter`` 同值。
INSTALLATION_POSITION_TOWER_TOP = "tower_top"
INSTALLATION_POSITION_GROUND_SITE = "ground_site"

#: 站址布设状态（四态，前端据此区分图形与图例，**绝不**把候选画成已布设）。
SITE_STATE_EXISTING = "existing_facility"
SITE_STATE_PLANNED = "planned_selected"
SITE_STATE_CANDIDATE = "candidate_unselected"
SITE_STATE_BLOCKED = "blocked"

SITE_STATE_LABELS = {
    SITE_STATE_EXISTING: "既有真实设施",
    SITE_STATE_PLANNED: "规划选中（待确认）",
    SITE_STATE_CANDIDATE: "候选未选中",
    SITE_STATE_BLOCKED: "blocked / 缺证",
}

#: 站址种类显示名。
SITE_KIND_LABELS = {
    "tower_colocation": "既有铁塔（共址候选）",
    "project_node": "项目起降点",
    "reference_landing_site": "参考起降点",
    "existing_cns_facility": "既有 CNS 设施",
}

#: 唯一被接受的垂向基准。
VERTICAL_REFERENCE = "egm2008_orthometric"

#: 三维对象 ID 前缀（与 ``visualization_3d_service.OBJECT_ID_PREFIXES`` 同家族）。
OBJECT_ID_PREFIX_SITE = "site:"
OBJECT_ID_PREFIX_PLANNED = "planned-device:"

#: 每类身份的硬声明：前端与报告都据此生成披露，不允许被省略。
SITE_STATE_DISCLOSURE = {
    SITE_STATE_EXISTING: "既有真实且已显式确认的设施；仍不等于该站址具备任何服务资格。",
    SITE_STATE_PLANNED: "规划算法选中的待确认站址；**不是**已安装设备。",
    SITE_STATE_CANDIDATE: "仅表示该站址位置可用；**不含**任何已安装设备、也未通过服务资格。",
    SITE_STATE_BLOCKED: "空间证据或安装高度证据缺失，已 fail-closed；不生成任何设备几何。",
}


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or abs(number) == float("inf"):
        return None
    return number


def classify_service(*, service_key=None, subsystem=None, device_id=None):
    """把设备身份归类到四类之一（**只看显式声明**，绝不猜测）。"""

    key = (_text(service_key) or "").strip()
    lowered = key.lower()
    if lowered.startswith("c:") or _text(subsystem) == "C":
        return SERVICE_CATEGORY_COMMUNICATION
    if lowered.startswith("s:rid") or lowered.startswith("rid"):
        return SERVICE_CATEGORY_RID
    if lowered.startswith("s:radar") or lowered.startswith("radar"):
        return SERVICE_CATEGORY_RADAR
    if lowered.startswith("n:"):
        return SERVICE_CATEGORY_NAVIGATION
    # 未显式声明服务键时按设备 ID 前缀做**可核验**的命名约定归类（不猜服务能力）。
    device = (_text(device_id) or "").upper()
    if device.startswith("RID-"):
        return SERVICE_CATEGORY_RID
    if device.startswith("RADAR-"):
        return SERVICE_CATEGORY_RADAR
    if device.startswith("NAV") or device.startswith("NAVINT-"):
        return SERVICE_CATEGORY_NAVIGATION
    if device.startswith("COMM-"):
        return SERVICE_CATEGORY_COMMUNICATION
    return SERVICE_CATEGORY_OTHER


def service_category_labels():
    return deepcopy(SERVICE_CATEGORY_LABELS)


def service_category_colors():
    return deepcopy(SERVICE_CATEGORY_COLORS)


def _tower_profile_index(state):
    """``tower_obstacle_profiles.items`` 的 tower_id → 档案索引（两种历史形态都接受）。"""

    block = (state or {}).get("tower_obstacle_profiles")
    raw = block.get("items") if isinstance(block, dict) else None
    index = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict) and (
                    value.get("tower_top_orthometric_m") is not None
                    or value.get("terrain_elevation_m") is not None):
                index[str(value.get("tower_id") or key)] = value
    if isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict) and value.get("tower_id") is not None:
                index.setdefault(str(value["tower_id"]), value)
    return index


def _tower_identity_index(state):
    index = {}
    container = (state or {}).get("towers")
    rows = container.get("items") if isinstance(container, dict) else container
    for row in rows or []:
        if isinstance(row, dict) and row.get("tower_id") is not None:
            index[str(row["tower_id"])] = row
    return index


def _installed_devices_by_host(state):
    """已安装设备 → 宿主站址（**只**取显式确认的安装动作）。

    数据来源是既有的 ``cns_corridor_site_plan.selected_actions``：它由生产站点规划
    写入，每条都可能带 ``planning_origin.host_tower_id`` 或 ``distinct_site_id``。
    **只有** ``confirmed is True`` 且 ``action_type == "add_device_to_explicit_site"``
    的动作才被当作"已安装设备"；其余一律不产生设备实体。
    """

    by_host = {}
    container = (state or {}).get("cns_corridor_site_plan")
    actions = container.get("selected_actions") if isinstance(container, dict) else None
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        if action.get("confirmed") is not True:
            continue
        if str(action.get("action_type") or "") != "add_device_to_explicit_site":
            continue
        planning_origin = action.get("planning_origin")
        host_tower_id = None
        if isinstance(planning_origin, dict):
            host_tower_id = _text(planning_origin.get("host_tower_id"))
        host = action.get("host")
        if host_tower_id is None and isinstance(host, dict):
            host_tower_id = _text(host.get("host_tower_id"))
        distinct_site_id = _text(action.get("distinct_site_id")) or ""
        tower_host = (
            host_tower_id
            or (distinct_site_id.split(":", 1)[1] if distinct_site_id.startswith("tower:") else None)
        )
        device = {
            "device_id": _text(action.get("device_id")),
            "service_key": _text(action.get("device_service_key")),
            "name": _text(action.get("device_name")) or _text(action.get("device_id")),
            "installation_status": "installed_confirmed",
            "confirmed": True,
            "position_semantics": (
                INSTALLATION_POSITION_TOWER_TOP if tower_host
                else INSTALLATION_POSITION_GROUND_SITE
            ),
            "physical_mount_confirmed": action.get("physical_mount_confirmed") is True,
            "planning_host_use_confirmed": (
                (host or {}).get("planning_host_use_confirmed") is True
                if isinstance(host, dict) else False
            ),
            "source": "cns_corridor_site_plan.selected_actions",
            "action_id": _text(action.get("action_id")),
        }
        device["category"] = classify_service(
            service_key=device["service_key"], device_id=device["device_id"])
        if tower_host:
            by_host.setdefault(f"tower-colocation:{tower_host}", []).append(device)
        elif distinct_site_id:
            by_host.setdefault(
                canonical_site_key(distinct_site_id, tower_host=None), []).append(device)
    return by_host


#: 站点规划动作的 ``distinct_site_id`` 前缀 → 共享候选站址的 ``candidate_id`` 前缀。
#: 两套命名都来自生产代码，三维层只做**显式映射**，绝不按名字相似度猜站址身份。
SITE_KEY_PREFIX_MAP = {
    "takeoff_landing_site:": "node:",
    "tower:": "tower-colocation:",
    "existing_cns_facility:": "existing-cns-facility:",
    "reference_landing_site:": "reference-landing-site:",
}


def canonical_site_key(distinct_site_id, *, tower_host=None):
    """把站点规划动作的站址标识归一成共享候选站址的 ``candidate_id``。"""

    if tower_host:
        return f"tower-colocation:{tower_host}"
    text = _text(distinct_site_id) or ""
    if not text:
        return ""
    for prefix, mapped in SITE_KEY_PREFIX_MAP.items():
        if text.startswith(prefix):
            return f"{mapped}{text[len(prefix):]}"
    return text


def _radar_panels_by_tower(state):
    """既有 ``radar_surveillance_layout`` 已登记的面阵 → 塔（规划选中站址的显式证据）。"""

    by_tower = {}
    container = (state or {}).get("radar_surveillance_layout")
    rows = container.get("items") if isinstance(container, dict) else container
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        for panel in item.get("selected_panels") or []:
            if not isinstance(panel, dict):
                continue
            tower_id = _text(panel.get("tower_id"))
            if not tower_id:
                continue
            by_tower.setdefault(tower_id, []).append({
                "device_id": _text(panel.get("panel_id")),
                "service_key": "S:radar_noncooperative",
                "name": _text(panel.get("radar_type")) or "Radar 面阵",
                "installation_status": "planned_selected_unconfirmed",
                "confirmed": False,
                "category": SERVICE_CATEGORY_RADAR,
                "position_semantics": INSTALLATION_POSITION_TOWER_TOP,
                "azimuth_deg": panel.get("azimuth_deg"),
                "source": "radar_surveillance_layout.selected_panels",
            })
    return by_tower


def _planned_ground_actions(state):
    """规划站点方案里**选中**的地面站动作（待确认，绝不当作已安装）。"""

    planned = []
    container = (state or {}).get("cns_corridor_site_plan")
    actions = container.get("selected_actions") if isinstance(container, dict) else None
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("action_type") or "") == "add_device_to_explicit_site":
            continue
        coordinate = action.get("coordinate")
        if not (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2):
            continue
        service_key = _text(action.get("device_service_key"))
        planned.append({
            "action_id": _text(action.get("action_id")),
            "device_id": _text(action.get("device_id")),
            "service_key": service_key,
            "name": _text(action.get("action")) or _text(action.get("device_id")),
            "category": classify_service(
                service_key=service_key, device_id=action.get("device_id")),
            "longitude": _finite(coordinate[0]),
            "latitude": _finite(coordinate[1]),
            "installation_status": "planned_selected_unconfirmed",
            "confirmed": action.get("confirmed") is True,
            "equipment_selection_status": _text(action.get("equipment_selection_status")),
            "distinct_site_id": _text(action.get("distinct_site_id")),
            "canonical_site_id": canonical_site_key(action.get("distinct_site_id")),
            "position_semantics": INSTALLATION_POSITION_GROUND_SITE,
            "source": "cns_corridor_site_plan.selected_actions",
        })
    return [row for row in planned
            if row["longitude"] is not None and row["latitude"] is not None]


def _blocked_radar_sites(state, blocked_radar):
    """Radar 输入适配层判定为 blocked 的站址（保留身份与原因，绝不画设备）。"""

    rows = []
    for record in blocked_radar or []:
        if not isinstance(record, dict):
            continue
        longitude = _finite(record.get("longitude"))
        latitude = _finite(record.get("latitude"))
        if longitude is None or latitude is None:
            continue
        rows.append({
            "site_id": _text(record.get("site_id")),
            "name": _text(record.get("name")),
            "longitude": longitude,
            "latitude": latitude,
            "site_kind": _text(record.get("candidate_kind")),
            "installation_position": _text(record.get("installation_position")),
            "block_reason": _text(record.get("block_reason")),
            "required_evidence": deepcopy(record.get("required_evidence") or []),
        })
    return rows


def site_records(*, state, shared_snapshot=None, radar_inputs=None):
    """把权威只读快照投影成三维站址记录（按四类身份分层）。

    返回 ``{"sites", "site_devices", "planned_devices", "blocked_sites",
    "counts", "disclosure"}``；每一项都带 ``object_id`` 供前端与二维共用同一身份。
    """

    state = state if isinstance(state, dict) else {}
    snapshot = shared_snapshot if isinstance(shared_snapshot, dict) else {}
    candidates = snapshot.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    profiles = _tower_profile_index(state)
    towers = _tower_identity_index(state)
    #: 既有共塔工程估算原点（``{tower_id: EGM2008 正高}``）：与 Radar 规划链共用
    #: **同一**只读访问器与同一道门禁（未显式 opt-in 时为空）。三维层绝不自己推导
    #: 塔高：这里只是把已经存在的估算值取出来用于**明确标注的工程示意**。
    planning_estimates = _tower_planning_service_origin_estimates(state)
    installed = _installed_devices_by_host(state)
    radar_panels = _radar_panels_by_tower(state)
    planned_ground = _planned_ground_actions(state)
    planned_site_ids = {
        row["canonical_site_id"] for row in planned_ground if row.get("canonical_site_id")
    }
    radar_plan = radar_inputs if isinstance(radar_inputs, dict) else {}

    sites = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        coordinate = candidate.get("coordinate")
        if not (isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2):
            continue
        longitude, latitude = _finite(coordinate[0]), _finite(coordinate[1])
        if longitude is None or latitude is None:
            continue
        kind = str(candidate.get("candidate_kind") or "")
        candidate_id = _text(candidate.get("candidate_id")) or ""
        source_id = _text(candidate.get("source_id")) or ""
        tower_id = source_id if kind == "tower_colocation" else None
        profile = profiles.get(tower_id) if tower_id else None
        tower_row = towers.get(tower_id) if tower_id else None

        tower_top = _finite((profile or {}).get("tower_top_orthometric_m"))
        terrain = _finite((profile or {}).get("terrain_elevation_m"))
        if terrain is None:
            terrain = _finite((tower_row or {}).get("elevation_m"))
            terrain_source = "towers.elevation_m"
        else:
            terrain_source = "tower_obstacle_profiles.terrain_elevation_m"
        structure_height = _finite((profile or {}).get("tower_structure_height_m"))
        if structure_height is None:
            structure_height = _finite((tower_row or {}).get("height_m"))

        installation_position = (
            INSTALLATION_POSITION_TOWER_TOP if tower_id
            else INSTALLATION_POSITION_GROUND_SITE
        )
        installation = candidate.get("existing_installation") or {}
        installed_devices = deepcopy(installed.get(candidate_id) or [])
        existing_device_ids = deepcopy(installation.get("device_ids") or []) \
            if isinstance(installation, dict) else []
        panels = deepcopy(radar_panels.get(tower_id) or []) if tower_id else []

        if kind == "existing_cns_facility":
            site_state = SITE_STATE_EXISTING
        elif (candidate_id in planned_site_ids or installed_devices
              or existing_device_ids or panels):
            # 该站址被**显式登记**为规划选中站址（站点规划选中的动作指向它），或已有
            # 显式记录的 CNS 设备身份（已确认安装动作、既有设备清单、已登记面阵）。
            # 仅有坐标的候选站址仍然不在此列。
            site_state = SITE_STATE_PLANNED
        elif str(candidate.get("spatial_use_status") or "") != "eligible":
            site_state = SITE_STATE_BLOCKED
        else:
            site_state = SITE_STATE_CANDIDATE

        # 结构高度只在**真实 tower 记录**上回显；它本身不是"已确认高度"。
        # 严格塔顶事实优先；严格事实缺失时，只有在既有共塔工程估算**已显式 opt-in**
        # 的前提下才退回工程示意高度，并逐条标注估算身份（绝不冒充已确认塔顶）。
        planning_estimate = _finite(planning_estimates.get(tower_id)) if tower_id else None
        planning_estimate_used = bool(
            tower_top is None and planning_estimate is not None
            and installation_position == INSTALLATION_POSITION_TOWER_TOP
        )
        geometry_available = installation_position == INSTALLATION_POSITION_TOWER_TOP \
            and terrain is not None and (tower_top is not None or planning_estimate_used)
        geometry_authority = (
            "resolved_tower_top" if tower_top is not None
            else "planning_estimate" if planning_estimate_used else None
        )
        sites.append({
            "object_id": f"{OBJECT_ID_PREFIX_SITE}{candidate_id}",
            "site_id": candidate_id,
            "source_id": source_id,
            "tower_id": tower_id,
            "facility_id": source_id if kind == "existing_cns_facility" else None,
            "name": _text(candidate.get("name")) or source_id or candidate_id,
            "site_kind": kind,
            "site_kind_label": SITE_KIND_LABELS.get(kind, kind or "未登记"),
            "site_type": _text(candidate.get("site_type")),
            "site_state": site_state,
            "site_state_label": SITE_STATE_LABELS.get(site_state, site_state),
            "longitude": longitude,
            "latitude": latitude,
            "installation_position": installation_position,
            "installation_position_label": (
                "塔顶安装" if installation_position == INSTALLATION_POSITION_TOWER_TOP
                else "地面站安装"
            ),
            "spatial_use_status": _text(candidate.get("spatial_use_status")),
            "spatial_block_reason": _text(candidate.get("spatial_block_reason")),
            "coordinate_source": _text(candidate.get("coordinate_source")),
            "source_crs": deepcopy(candidate.get("source_crs")),
            "data_source": deepcopy(candidate.get("data_source")),
            # ---- 垂直证据（缺证一律 None，绝不填 0） -------------------------
            "terrain_elevation_m": terrain,
            "terrain_elevation_source": terrain_source if terrain is not None else None,
            "terrain_vertical_reference": (
                (profile or {}).get("terrain_vertical_reference")
                if profile else (VERTICAL_REFERENCE if terrain is not None else None)
            ),
            "terrain_status": (profile or {}).get("terrain_status"),
            "tower_structure_height_m": structure_height,
            "tower_top_orthometric_m": tower_top,
            "tower_top_status": (profile or {}).get("tower_top_status") or "unknown",
            "tower_top_confirmed": (profile or {}).get("confirmed") is True,
            "vertical_status": (profile or {}).get("vertical_status") or "unknown",
            "base_type": (profile or {}).get("base_type") or "unknown",
            "structure_geometry_available": bool(geometry_available),
            #: ``resolved_tower_top`` = 有完整垂直基准的塔顶事实；
            #: ``planning_estimate`` = 只有既有工程估算证据（源表海拔 + 塔身高度），
            #: 只能作明确标注的工程示意。二者**绝不**混为一谈。
            "structure_geometry_authority": geometry_authority,
            "planning_estimate_origin_egm2008_m": planning_estimate,
            "planning_estimate_used_for_geometry": planning_estimate_used,
            "structure_geometry_basis": (
                "terrain_elevation_m → tower_top_orthometric_m（既有塔顶正高证据）"
                if tower_top is not None and installation_position == INSTALLATION_POSITION_TOWER_TOP
                else "terrain_elevation_m → engineering_estimate"
                     "（源表海拔 + 塔身高度；非已确认 EGM2008 塔顶正高）"
                if planning_estimate_used else None
            ),
            "structure_geometry_blocked_reason": None if geometry_available else (
                "tower_top_orthometric_m_unresolved_and_no_planning_estimate"
                if installation_position == INSTALLATION_POSITION_TOWER_TOP
                else "ground_site_has_no_confirmed_tower_structure_evidence"
            ),
            # ---- 设备身份（不推断安装） --------------------------------------
            "installed_device_ids": [
                row.get("device_id") for row in installed_devices if row.get("device_id")
            ],
            "existing_device_ids": [str(value) for value in existing_device_ids],
            "planned_panel_count": len(panels),
            "device_categories": sorted({
                row.get("category") for row in installed_devices + panels
                if row.get("category")
            }),
            "devices": installed_devices + panels,
            "never_infers_device_from_site_presence": True,
        })

    # ---- 规划选中（但不在共享候选快照里的）地面站动作 ------------------------
    known_site_ids = {row["site_id"] for row in sites}
    planned_devices = []
    for row in planned_ground:
        canonical_id = row.get("canonical_site_id")
        planned_devices.append({
            **row,
            "object_id": f"{OBJECT_ID_PREFIX_PLANNED}{row.get('action_id') or row.get('device_id')}",
            "linked_site_id": canonical_id if canonical_id in known_site_ids else None,
            "linked_site_available": canonical_id in known_site_ids,
        })

    blocked_sites = _blocked_radar_sites(state, radar_plan.get("blocked"))
    plan = radar_plan.get("statistics") if isinstance(radar_plan, dict) else None
    counts = {
        "total_sites": len(sites),
        "existing_facilities": sum(
            row["site_state"] == SITE_STATE_EXISTING for row in sites),
        "planned_selected": sum(
            row["site_state"] == SITE_STATE_PLANNED for row in sites),
        "candidate_unselected": sum(
            row["site_state"] == SITE_STATE_CANDIDATE for row in sites),
        "spatial_blocked": sum(
            row["site_state"] == SITE_STATE_BLOCKED for row in sites),
        "tower_sites": sum(row["tower_id"] is not None for row in sites),
        "ground_sites": sum(row["tower_id"] is None for row in sites),
        "towers_with_structure_geometry": sum(
            row["structure_geometry_available"] for row in sites),
        "towers_with_resolved_tower_top_geometry": sum(
            row["structure_geometry_authority"] == "resolved_tower_top" for row in sites),
        "towers_with_planning_estimate_geometry": sum(
            row["structure_geometry_authority"] == "planning_estimate" for row in sites),
        "towers_missing_tower_top": sum(
            row["tower_id"] is not None and not row["structure_geometry_available"]
            for row in sites),
        "installed_device_count": sum(
            len(row["devices"]) for row in sites
            if row["site_state"] == SITE_STATE_EXISTING
            or row["installed_device_ids"]),
        "planned_device_count": len(planned_devices),
        "planned_panel_count": sum(row["planned_panel_count"] for row in sites),
        "blocked_radar_site_count": len(blocked_sites),
        "radar_usable_count": (plan or {}).get("usable_count"),
        "radar_site_count": (plan or {}).get("site_count"),
    }
    return {
        "sites": sites,
        "planned_devices": planned_devices,
        "blocked_sites": blocked_sites,
        "counts": counts,
        "disclosure": deepcopy(SITE_STATE_DISCLOSURE),
    }


__all__ = [
    "INSTALLATION_POSITION_GROUND_SITE", "INSTALLATION_POSITION_TOWER_TOP",
    "OBJECT_ID_PREFIX_PLANNED", "OBJECT_ID_PREFIX_SITE",
    "SERVICE_CATEGORY_COLORS", "SERVICE_CATEGORY_COMMUNICATION",
    "SERVICE_CATEGORY_LABELS", "SERVICE_CATEGORY_NAVIGATION", "SERVICE_CATEGORY_OTHER",
    "SERVICE_CATEGORY_RADAR", "SERVICE_CATEGORY_RID", "SITE_KIND_LABELS",
    "SITE_KIND_LABELS", "SITE_KEY_PREFIX_MAP",
    "SITE_STATE_BLOCKED", "SITE_STATE_CANDIDATE", "SITE_STATE_DISCLOSURE",
    "SITE_STATE_EXISTING", "SITE_STATE_LABELS", "SITE_STATE_PLANNED",
    "VERTICAL_REFERENCE", "canonical_site_key", "classify_service",
    "service_category_colors", "service_category_labels", "site_records",
]
