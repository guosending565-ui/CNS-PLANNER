"""三维 CNS 覆盖图层的**唯一**只读站址/设备解析入口。

为什么需要这一层：通信包络与 RID footprint 都必须建立在"**真实站址坐标 + 已确认
安装高程 + 设备几何契约**"之上，而这两条链路绝不允许各自发展一套解析口径。
本模块只做只读解析，且**全部复用**既有权威函数：

* :func:`cns_planner.domain.cns_service_contract.service_contract_for` —— 服务契约；
* :func:`...effective_radius_m` / :func:`...index_max_range_m` / :func:`...geometry_usable`
  —— 按 surface 解析几何规划半径（半径权威性铁律）；
* :func:`...distinct_site_id_for` —— 物理站址身份；
* :func:`...is_rid_declaration` —— RID 显式类型判定（只看 service_key / technology）。

**解析顺序与 P7 逐项一致**（``geometric_3d.build_geometric_providers``）：
安装记录 vertical_profile → 站址 vertical_profile → 设备 catalog vertical_profile，
取第一个 ``service_origin_egm2008_m`` 非空的。任何不一致都会被显式标注，绝不静默改派。

本模块**零写入**：不 import 任何写入权威，不注册失效入口，不写 artifact。
"""

from __future__ import annotations

from ..domain.cns_service_contract import (
    SERVICE_KEY_COMMUNICATION,
    SERVICE_KEY_RID_COOPERATIVE,
    distinct_site_id_for,
    effective_radius_m,
    geometry_usable,
    index_max_range_m,
    is_rid_declaration,
    normalize_surface_class,
    radius_by_surface_of,
    service_contract_for,
    service_key_for,
)

#: 站址/设备解析结果的成熟度取值。
ORIGIN_CONFIRMED = "confirmed"
ORIGIN_UNCONFIRMED = "unconfirmed"
ORIGIN_MISSING = "missing"


def items_of(container):
    """统一取出容器条目（``{"items": [...]}`` / ``{"items": {...}}`` / 裸列表）。"""

    if isinstance(container, dict):
        raw = container.get("items")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return list(raw.values())
        return []
    if isinstance(container, list):
        return container
    return []


def _coordinate_of(site):
    coordinate = site.get("coordinate")
    if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
        try:
            return float(coordinate[0]), float(coordinate[1])
        except (TypeError, ValueError):
            return None
    return None


def _vertical_candidates(installed, site_vertical, device):
    """与 P7 完全一致的候选顺序；返回第一个带 ``service_origin_egm2008_m`` 的块。"""

    return [
        installed.get("vertical_profile"),
        site_vertical,
        (device or {}).get("vertical_profile"),
    ]


def resolve_origin(candidates):
    """解析服务原点（EGM2008 正高）与**确认语义**。

    返回 ``(origin_m, block, status, reason)``：

    * 有值且 ``confirmed`` 为真 ⇒ ``confirmed``；
    * 有值但未确认 ⇒ ``unconfirmed``（可展示，但必须标注）；
    * 没有任何候选给出数值 ⇒ ``(None, None, missing, ...)``——
      **未确认安装高度时绝不生成确定的三维空间包络**。
    """

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("service_origin_egm2008_m")
        if value in (None, ""):
            continue
        try:
            origin = float(value)
        except (TypeError, ValueError):
            continue
        confirmed = bool(candidate.get("confirmed"))
        declared_status = str(candidate.get("status") or "").strip() or None
        if confirmed and declared_status in (None, "confirmed"):
            return origin, candidate, ORIGIN_CONFIRMED, None
        return origin, candidate, ORIGIN_UNCONFIRMED, (
            "service_origin_not_confirmed"
            if not confirmed else f"service_origin_status_{declared_status}"
        )
    return None, None, ORIGIN_MISSING, "service_origin_egm2008_m_not_recorded"


def _surface_policy_matches(geometry, expected):
    """``radius_by_surface`` 是否与给定策略**逐项相等**（用于如实标注 RID 几何一致性）。"""

    mapping = radius_by_surface_of(geometry)
    if mapping is None or not isinstance(expected, dict):
        return False
    for name in ("land", "coastal_uncertain", "sea"):
        if name not in mapping:
            return False
        if abs(float(mapping[name]) - float(expected[name])) > 1e-6:
            return False
    return True


def installed_service_units(state):
    """列出**全部**已安装服务单元（站址 × 已安装设备）的只读解析结果。

    每个单元包含：站址坐标、物理站址身份、服务契约、几何契约、服务原点与确认语义。
    **不过滤**未确认安装高度的单元 —— 它们必须出现在响应里并被显式标注为
    ``origin_status != confirmed``，而不是被静默丢弃（"缺失不等于 0"）。
    """

    catalog = {
        str(device.get("device_id")): device
        for device in items_of((state or {}).get("device_catalog"))
        if isinstance(device, dict)
    }
    spatial = (state or {}).get("spatial_3d") or {}
    site_profiles = spatial.get("site_vertical_profiles") or {}
    units = []
    for site in items_of((state or {}).get("existing_cns_facilities")):
        if not isinstance(site, dict):
            continue
        coordinate = _coordinate_of(site)
        site_key = str(site.get("site_id") or site.get("facility_id") or "")
        site_vertical = (
            site_profiles.get(site_key) if isinstance(site_profiles, dict) else None
        ) or site.get("vertical_profile") or {}
        for installed in (site.get("devices") or []):
            if not isinstance(installed, dict):
                continue
            device = catalog.get(str(installed.get("device_id"))) or installed
            geometry = (device.get("coverage_geometry")
                        or installed.get("coverage_geometry") or {})
            identity = {**installed, **device}
            if isinstance(device.get("type"), dict) or isinstance(installed.get("type"), dict):
                identity["type"] = {
                    **(installed.get("type") or {}), **(device.get("type") or {}),
                }
            subsystem = str(installed.get("subsystem") or device.get("subsystem") or "")
            origin, origin_block, origin_status, origin_reason = resolve_origin(
                _vertical_candidates(installed, site_vertical, device))
            contract = service_contract_for(subsystem, identity)
            units.append({
                "site_id": site.get("site_id"),
                "facility_id": site.get("facility_id"),
                "site_name": site.get("name"),
                "site_status": site.get("status"),
                "longitude": coordinate[0] if coordinate else None,
                "latitude": coordinate[1] if coordinate else None,
                "coordinate_available": coordinate is not None,
                "device_id": identity.get("device_id"),
                "device_name": device.get("name") or installed.get("name"),
                "subsystem": subsystem,
                "service_key": contract.get("service_key"),
                "service_contract": contract,
                "geometry": geometry,
                "geometry_usable": bool(geometry_usable(geometry)),
                "radius_by_surface": radius_by_surface_of(geometry),
                "index_max_range_m": index_max_range_m(geometry),
                "service_origin_egm2008_m": origin,
                "service_origin_block": origin_block,
                "origin_status": origin_status,
                "origin_reason": origin_reason,
                "origin_source": (origin_block or {}).get("source"),
                "origin_surface_vertical_reference": (
                    (origin_block or {}).get("surface_vertical_reference")),
                "distinct_site_id": distinct_site_id_for(site, identity),
                "rid_type_declared": bool(is_rid_declaration(identity)),
                "declared_service_key": identity.get("service_key"),
                "explicit_service_key": service_key_for(subsystem, identity),
                "device_catalog_matched": str(installed.get("device_id")) in catalog,
            })
    return units


def communication_units(state):
    """通信（``C:communication``）服务单元。"""

    return [
        unit for unit in installed_service_units(state)
        if unit["subsystem"] == "C" or unit["service_key"] == SERVICE_KEY_COMMUNICATION
    ]


def rid_units(state, *, rid_radius_by_surface):
    """RID 服务单元，**分两类**且各自如实标注声明依据。

    1. ``explicit_rid_declaration``：设备显式声明 ``S:rid_cooperative`` 或
       ``technology=network_remote_id``（唯一真正的 RID 判定，见
       :func:`...is_rid_declaration`）。
    2. ``surface_radius_matches_rid_policy``：子系统 S 的设备**未**显式声明 RID 类型，
       但其 ``coverage_geometry.radius_by_surface`` 与冻结的
       :data:`...RID_RADIUS_BY_SURFACE` **逐项相等**。这是一个可核验的**几何策略
       事实**，不是类型猜测；因此按 RID 陆海分区策略生成工程几何，同时必须携带
       ``rid_type_declared = false`` 与明确说明，绝不允许被读成"设备已确认是 RID"。
    """

    units = []
    for unit in installed_service_units(state):
        if unit["subsystem"] != "S":
            continue
        if unit["service_key"] == SERVICE_KEY_RID_COOPERATIVE:
            units.append({
                **unit,
                "declaration_basis": "explicit_rid_declaration",
                "declaration_note": (
                    "设备显式声明 RID（service_key=S:rid_cooperative 或 "
                    "technology=network_remote_id）"),
            })
            continue
        if _surface_policy_matches(unit["geometry"], rid_radius_by_surface):
            units.append({
                **unit,
                "declaration_basis": "surface_radius_matches_rid_policy",
                "declaration_note": (
                    "该站址的 S 设备**未**显式声明 RID 类型；其 "
                    "coverage_geometry.radius_by_surface 与 RID 冻结策略逐项相等，"
                    "因此按 RID 陆海分区策略生成工程几何。这是**几何策略一致**，"
                    "不是设备类型确认，不得作为 RID 部署证据。"),
            })
    return units


def effective_radius_for(unit, surface_class):
    """按 surface 解析某单元的几何规划半径（复用权威函数，绝不自行取数）。"""

    return effective_radius_m(unit.get("geometry") or {}, surface_class)


# ------------------------------------------------------------------ Radar 面阵


def tower_profile_index(state):
    """``tower_obstacle_profiles.items`` 的 tower_id → 档案索引。

    该容器**不是**外置 scope，因此磁盘上的 ProjectState 就带着全部塔顶派生事实。
    ``items`` 既可能是列表也可能是以 tower_id 为键的字典（历史两种形态都存在），
    两种都按 tower_id 归一化，避免任何一方被判成"没有塔顶证据"。
    """

    block = (state or {}).get("tower_obstacle_profiles")
    raw = block.get("items") if isinstance(block, dict) else None
    index = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                index[str(value.get("tower_id") or key)] = value
    for value in items_of(block):
        if isinstance(value, dict) and value.get("tower_id") is not None:
            index.setdefault(str(value.get("tower_id")), value)
    return index


def radar_panel_origin(tower_profile):
    """Radar 相位中心原点的**只读**解析（EGM2008 正高）与确认语义。

    返回 ``(origin_m, status, reason, source, evidence_level)``：

    * ``tower_top_orthometric_m`` 有值且 ``confirmed is True`` ⇒ ``confirmed``；
    * 有值但未确认 ⇒ ``unconfirmed``（可展示，但**必须**标注，且绝不允许被当作
      已确认安装高度）；
    * 没有值 ⇒ ``missing``：``unknown`` 绝不当作 0 m，也不退回源数据
      ``elevation_m``（其垂直基准未经确认，明确不得作为 EGM2008 正高使用）。
    """

    profile = tower_profile if isinstance(tower_profile, dict) else {}
    value = profile.get("tower_top_orthometric_m")
    reason = None
    if value in (None, ""):
        status = ORIGIN_MISSING
        reason = str(profile.get("reason") or "").strip() or str(
            profile.get("tower_top_status") or ""
        ).strip() or "tower_top_orthometric_m_not_resolved"
        return None, status, reason, profile.get("source"), profile.get("tower_top_status")
    try:
        origin = float(value)
    except (TypeError, ValueError):
        return (None, ORIGIN_MISSING,
                str(profile.get("tower_top_status") or "tower_top_orthometric_m_invalid"),
                profile.get("source"), profile.get("tower_top_status"))
    confirmed = profile.get("confirmed") is True
    status = ORIGIN_CONFIRMED if confirmed else ORIGIN_UNCONFIRMED
    reason = None if confirmed else "tower_top_orthometric_m_not_confirmed"
    return origin, status, reason, {
        "container": "tower_obstacle_profiles.items",
        "field": "tower_top_orthometric_m",
        "vertical_reference": profile.get("terrain_vertical_reference"),
        "tower_top_status": profile.get("tower_top_status"),
        "terrain_elevation_m": profile.get("terrain_elevation_m"),
        "building_height_m": profile.get("building_height_m"),
        "tower_structure_height_m": profile.get("tower_structure_height_m"),
        "base_type": profile.get("base_type"),
    }, profile.get("tower_top_status")


def radar_units(state):
    """列出**真实** ``radar_surveillance_layout.items[].selected_panels`` 的只读解析。

    这是 Radar 三维覆盖的**唯一**面板来源：绝不自行枚举候选方向、绝不补默认方位、
    绝不用全向圆替代。每个单元都带上面板自身的参数、所属 layout item 的当前性与
    该塔的塔顶正高证据（含确认语义）。

    ``selected_panels`` 属于已登记的外置 scope，运行中的 ``state`` 由
    ``restore_compacted_results`` 水合，因此这里读到的就是持久化的真实面板；
    若某个项目确实没有面板，返回空列表并**不**伪造任何几何。
    """

    state = state or {}
    layout = state.get("radar_surveillance_layout") or {}
    tower_index = tower_profile_index(state)
    units = []
    for item_index, item in enumerate(items_of(layout)):
        if not isinstance(item, dict):
            continue
        selected_tower_ids = {str(value) for value in (item.get("selected_tower_ids") or [])}
        selected = item.get("selected_panels")
        if not isinstance(selected, list):
            # 未水合（磁盘原始文档）时如实留空，绝不退回候选面板集合。
            selected = []
        for panel_index, panel in enumerate(selected):
            if not isinstance(panel, dict):
                continue
            tower_id = str(panel.get("tower_id") or "")
            profile = tower_index.get(tower_id) or {}
            origin, origin_status, origin_reason, origin_source, evidence = (
                radar_panel_origin(profile)
            )
            units.append({
                "unit_index": len(units),
                "panel_id": panel.get("panel_id"),
                "tower_id": panel.get("tower_id"),
                "radar_type": panel.get("radar_type"),
                "azimuth_deg": panel.get("azimuth_deg"),
                "panel_half_width_deg": panel.get("panel_half_width_deg"),
                "declared_azimuth_deg": panel.get("azimuth_deg"),
                "longitude": profile.get("longitude"),
                "latitude": profile.get("latitude"),
                "coordinate_available": (
                    profile.get("longitude") is not None
                    and profile.get("latitude") is not None
                ),
                "tower_name": profile.get("name"),
                "tower_profile_found": bool(profile),
                "tower_id_in_selected_tower_ids": tower_id in selected_tower_ids,
                "tower_profile_status": profile.get("status"),
                "tower_confirmed": profile.get("confirmed") is True,
                "tower_top_status": profile.get("tower_top_status"),
                "tower_top_orthometric_m": profile.get("tower_top_orthometric_m"),
                "origin_egm2008_m": origin,
                "origin_status": origin_status,
                "origin_reason": origin_reason,
                "origin_source": origin_source,
                "origin_evidence_level": evidence,
                "layout_item_index": item_index,
                "layout_route_id": item.get("route_id"),
                "layout_item_status": item.get("status"),
                "layout_stale_reason": item.get("stale_reason"),
                "layout_algorithm_version": item.get("algorithm_version"),
                "layout_demo_preview_only": item.get("demo_preview_only") is True,
                "layout_proposal_only": item.get("proposal_only") is True,
                "layout_operationally_adopted": bool(item.get("operationally_adopted")),
                "layout_altitude_layer_id": item.get("altitude_layer_id"),
                "layout_altitude_m": item.get("altitude_m"),
                "layout_radar_origin_semantics": (
                    (item.get("radar_origin") or {}).get("semantics")
                    if isinstance(item.get("radar_origin"), dict) else None
                ),
                "panel_index": panel_index,
                "parameter_source": (
                    "radar_surveillance_layout.items[%d].selected_panels[%d]"
                    % (item_index, panel_index)
                ),
            })
    return units


def normalized_surface(value):
    return normalize_surface_class(value)


__all__ = [
    "ORIGIN_CONFIRMED", "ORIGIN_MISSING", "ORIGIN_UNCONFIRMED",
    "communication_units", "effective_radius_for", "installed_service_units",
    "items_of", "normalized_surface", "radar_panel_origin", "radar_units",
    "resolve_origin", "rid_units", "tower_profile_index",
]
