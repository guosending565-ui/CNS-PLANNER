"""Neutral candidate-action construction shared by canonical facility planning."""

from __future__ import annotations

from copy import deepcopy

from ..domain.cns_service_contract import (
    distinct_site_id_for, geometry_usable, service_contract_for,
)
from ..domain.site_planning import TOWER_COLOCATION_REUSE_CLASS


def candidate_actions(targets, existing, candidates, catalog, tower_colocation=None):
    target_subsystems = {item["subsystem"] for item in targets}
    devices = [
        item for item in (catalog or {}).get("items") or []
        if item.get("enabled", True) and item.get("subsystem") in target_subsystems
    ]
    actions = []
    for facility in (existing or {}).get("items") or []:
        installed = {str(item.get("device_id") or "") for item in facility.get("devices") or []}
        for device in devices:
            actions.append(_action(facility, device, installed, is_existing=True))
    for site in (tower_colocation or {}).get("items") or []:
        for device in devices:
            actions.append(_action(site, device, set(), is_existing=False))
    for site in (candidates or {}).get("items") or []:
        for device in devices:
            actions.append(_action(site, device, set(), is_existing=False))
    return sorted(actions, key=lambda item: item["action_id"])


def _action(site, device, installed, is_existing):
    profile = deepcopy(site.get("planning_profile") or {})
    vertical = deepcopy(site.get("vertical_profile") or {})
    reuse_class = profile.get("reuse_class")
    identifier = str(site.get("facility_id") if is_existing else site.get("site_id") or "")
    device_id = str(device.get("device_id") or "")
    device_subsystem = str(device.get("subsystem") or "")
    metadata = site.get("metadata") if isinstance(site.get("metadata"), dict) else {}
    host = metadata.get("host") if isinstance(metadata.get("host"), dict) else None
    planning_host = (
        metadata.get("planning_host")
        if isinstance(metadata.get("planning_host"), dict)
        else None
    )
    is_tower_host = reuse_class == TOWER_COLOCATION_REUSE_CLASS
    declared = [
        str(item).upper()
        for item in (site.get("available_subsystems") or [])
        if str(item).strip()
    ]
    reasons, unknown = [], []
    if profile.get("confirmed") is not True:
        unknown.append("planning_profile 未确认")
    if profile.get("add_device_allowed") is not True:
        reasons.append("站点未明确允许增加设备")
    if not reuse_class:
        unknown.append("reuse_class 缺失")
    if is_existing and site.get("status") not in ("active", "passed", None):
        reasons.append("ExistingCNS 设施不可用")
    if not is_existing and site.get("usable") is not True:
        reasons.append("CandidateSite unusable")
    if not is_existing and site.get("locked") is True:
        reasons.append("CandidateSite locked")
    if device_id in installed:
        reasons.append("设施已安装同一 device")
    if vertical.get("confirmed") is not True or vertical.get("service_origin_egm2008_m") is None:
        unknown.append("缺少 confirmed EGM2008 service origin")
    geometry = device.get("coverage_geometry") or {}
    #: Round 2 修复：不再要求 legacy ``slant_range_m``。surface-aware 服务
    #: （Communication / RID）的几何由 ``coverage_geometry.radius_by_surface``
    #: 承载，``slant_range_m`` 合法地保持 ``None``；继续用旧的
    #: ``slant_range_m != None`` 门禁会把它们错误判成"缺少 coverage geometry"。
    #: 统一复用 Round 1 的 :func:`geometry_usable`：
    #: confirmed hemisphere/sphere + （合法 slant_range_m **或** 合法 radius_by_surface）。
    if geometry.get("confirmed") is not True or not geometry_usable(geometry):
        unknown.append("缺少 confirmed sphere/hemisphere coverage geometry")
    model = device.get("service_model") or {}
    if model.get("model_family") == "unsupported":
        reasons.append("device service model unsupported")
    elif model.get("confirmed") is not True:
        unknown.append("device ServiceModelSpec 未确认")
    coordinate = site.get("coordinate")
    if not isinstance(coordinate, list) or len(coordinate) < 2:
        reasons.append("站点坐标缺失")
    if declared and device_subsystem and device_subsystem not in declared:
        reasons.append("站点声明的可用分系统不包含该设备分系统")
        subsystem_mount_status = "declared_not_compatible"
    elif declared:
        subsystem_mount_status = "declared_compatible"
    else:
        subsystem_mount_status = "unverified"

    status = "ineligible" if reasons else "unknown" if unknown else "eligible"
    action_type = (
        "add_device_to_existing_facility"
        if is_existing
        else "add_device_to_explicit_site"
    )
    #: Round 2 additive：action 明确携带 service identity 与**物理站址身份**。
    #: ``distinct_site_id`` 只由 canonical helper 解析（TowerColocation →
    #: ``tower:<host_tower_id>``；Existing → ``site:<site_id>``，无 site_id 才
    #: ``facility:<facility_id>``；Candidate → ``site:<site_id>``；无法确认即 ``None``），
    #: **绝不**由 ``device_id`` 或坐标推断。
    contract = service_contract_for(device_subsystem, device)
    identity = dict(device)
    if isinstance(device.get("type"), dict):
        identity["type"] = deepcopy(device["type"])
    distinct_site_id = distinct_site_id_for(site, identity)
    return {
        "action_id": f"{reuse_class or 'unknown'}:{identifier}:{device_id}",
        "action_type": action_type,
        "site_id": site.get("site_id"),
        "facility_id": site.get("facility_id") if is_existing else None,
        "device_id": device_id,
        "subsystem": device.get("subsystem"),
        "service_key": contract.get("service_key"),
        "distinct_site_id": distinct_site_id,
        "reuse_class": reuse_class,
        "coordinate": deepcopy(coordinate),
        "vertical": vertical,
        "planning_profile": profile,
        "host": deepcopy(host),
        "planning_origin": deepcopy(metadata.get("planning_origin")),
        "planning_host_status": (
            (planning_host or {}).get("planning_host_status") if is_tower_host else None
        ),
        "subsystem_mount_status": subsystem_mount_status,
        "physical_mount_confirmed": False if is_tower_host else None,
        "requires_site_survey": True if is_tower_host else None,
        "source": str(profile.get("source") or site.get("source") or "未记录"),
        "confirmed": status == "eligible",
        "eligibility": {"status": status, "reasons": [*reasons, *unknown]},
    }
