"""Application what-if orchestration for proposal-only reuse-first site planning."""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.site_planning import TOWER_COLOCATION_REUSE_CLASS, normalize_site_planning_policy
from ..gap.v2 import CNSGapAnalyzerV2
from .production_write_authority import (
    runtime_compatibility_result, write_runtime_compatibility_result,
)


class SitePlanningService:
    def __init__(
        self, session, planner, coverage_model, capability_model,
        invalidation, snapshot,
    ):
        self.session = session
        self.planner = planner
        self.coverage_model = coverage_model
        self.capability_model = capability_model
        self.invalidation = invalidation
        self.snapshot = snapshot

    def result_snapshot(self):
        runtime = runtime_compatibility_result(self.session, "cns_site_plan")
        if runtime:
            return deepcopy(runtime)
        return deepcopy(self.session.state["cns_site_plan"])

    def evaluate(self, payload=None):
        state = self.session.state
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("site planning 请求必须是对象")
        policy = (
            normalize_site_planning_policy(payload["site_planning_policy"])
            if "site_planning_policy" in payload
            else deepcopy(state.get("site_planning_policy") or {})
        )
        targets = _planning_targets(
            state.get("cns_gap_analysis_v2") or {}, state.get("required_cns") or {},
        )
        actions = _candidate_actions(
            targets, state.get("existing_cns_facilities") or {},
            state.get("candidate_sites") or {}, state.get("device_catalog") or {},
            state.get("tower_colocation_candidates") or {},
        )
        impacts = [self._what_if(action, targets) for action in actions]
        result = self.planner.plan(
            targets, actions, impacts, policy,
        )
        record = write_runtime_compatibility_result(
            self.session, "cns_site_plan", result,
            source_algorithm={
                "algorithm_type": "site_planner",
                "algorithm_id": getattr(self.planner, "algorithm_id", None),
                "algorithm_version": getattr(self.planner, "algorithm_version", None),
                "class": type(self.planner).__name__,
            },
            note=(
                "旧版站址试算：不用于正式规划，不写入项目状态；正式设施规划由"
                "走廊站址规划服务生成。"
            ),
        )
        response = self.snapshot()
        response["compatibility_cns_site_plan"] = record
        # 旧 API 响应继续提供 cns_site_plan 视图，但只来自会话级缓存。
        response["cns_site_plan"] = record
        response["compatibility_write"] = True
        response["canonical_facility_plan_unchanged"] = True
        return response

    def _what_if(self, action, targets):
        eligibility = action.get("eligibility") or {}
        if eligibility.get("status") != "eligible":
            return _empty_impact(
                action["action_id"], eligibility.get("status", "ineligible"),
                eligibility.get("reasons") or [],
            )
        state = self.session.state
        facilities = _apply_hypothetical_action(
            state.get("existing_cns_facilities") or {}, action,
        )
        coverage_model = self.coverage_model.__class__(
            (state.get("coverage_3d") or {}).get("parameters")
            or getattr(self.coverage_model, "parameters", {}),
        )
        capability_model = self.capability_model.__class__(
            (state.get("cns_service_capability") or {}).get("parameters")
            or getattr(self.capability_model, "parameters", {}),
        )
        what_if_coverage = coverage_model.evaluate(
            state.get("operational_routes") or [], state.get("spatial_3d") or {},
            state.get("grid") or {}, state.get("grid_attributes") or {},
            facilities, state.get("device_catalog") or {},
        )
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {},
            state.get("selected_aircraft_profile_id") or "",
        )
        what_if_capability = capability_model.evaluate(
            what_if_coverage, state.get("required_cns") or {}, profile,
            facilities, state.get("device_catalog") or {},
        )
        what_if_gap = CNSGapAnalyzerV2({"evaluate_protection_margin": False}).analyze(
            state.get("required_cns") or {}, what_if_coverage,
            what_if_capability, state.get("service_timeline") or {}, {}, {},
        )
        resolutions, unknown = _resolved_intervals(targets, what_if_gap)
        reduction = sum(item["planning_gap_reduction_m"] for item in resolutions)
        affected = [item["segment_id"] for item in resolutions if item["planning_gap_reduction_m"] > 0]
        resolved = [
            item["segment_id"] for item in resolutions
            if item["planning_gap_reduction_m"] >= item["target_length_m"] - 1e-7
        ]
        if reduction > 0:
            status, reasons = "eligible", []
        elif unknown:
            status, reasons = "unknown", ["what-if 的 P7/P8 planning evidence 不完整"]
        else:
            status, reasons = "ineligible", ["单个 action 未产生正的 confirmed planning-gap reduction"]
        return {
            "action_id": action["action_id"],
            "status": status,
            "resolved_segment_ids": resolved,
            "affected_segment_ids": affected,
            "planning_gap_reduction_m": reduction,
            "segment_resolutions": resolutions,
            "reasons": reasons,
            "evidence": [
                {
                    "kind": "p7_what_if",
                    "algorithm_id": what_if_coverage.get("algorithm_id"),
                    "algorithm_version": what_if_coverage.get("algorithm_version"),
                    "input_fingerprint": what_if_coverage.get("input_fingerprint"),
                    "persisted": False,
                },
                {
                    "kind": "p8_what_if",
                    "algorithm_id": what_if_capability.get("algorithm_id"),
                    "algorithm_version": what_if_capability.get("algorithm_version"),
                    "input_fingerprint": what_if_capability.get("input_fingerprint"),
                    "persisted": False,
                },
                {
                    "kind": "gap_v2_planning_comparison",
                    "algorithm_id": what_if_gap.get("algorithm_id"),
                    "algorithm_version": what_if_gap.get("algorithm_version"),
                    "resolved_intervals": deepcopy(resolutions),
                    "runtime_used_for_benefit": False,
                },
            ],
        }


def _planning_targets(gap_v2, required_cns):
    targets = []
    for route in (gap_v2 or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        requirements = (
            ((required_cns or {}).get("route_overrides") or {}).get(route_id)
            or (required_cns or {}).get("project_default") or {}
        )
        for subsystem in route.get("subsystems") or []:
            code = str(subsystem.get("subsystem") or "")
            requirement = requirements.get({"C": "communication", "N": "navigation", "S": "surveillance"}.get(code, "")) or {}
            redundancy = (requirement.get("performance") or {}).get("min_redundancy", requirement.get("redundancy"))
            for segment in subsystem.get("segments") or []:
                causes = set(segment.get("gap_causes") or [])
                ground_hint = segment.get("remediation_scope") in ("ground_service_candidate", "mixed")
                structured_ground_cause = "geometry_gap" in causes or ("static_service_mismatch" in causes and ground_hint)
                if segment.get("planning_status") != "confirmed_gap" or not structured_ground_cause:
                    continue
                targets.append({
                    "segment_id": str(segment.get("segment_id") or ""),
                    "route_id": route_id,
                    "subsystem": code,
                    "start_route_offset_m": float(segment.get("start_route_offset_m") or 0.0),
                    "end_route_offset_m": float(segment.get("end_route_offset_m") or 0.0),
                    "length_m": float(segment.get("length_m") or 0.0),
                    "gap_causes": list(segment.get("gap_causes") or []),
                    "source_remediation_scope": segment.get("remediation_scope"),
                    "target_reasons": list(segment.get("reasons") or []),
                    "requires_joint_optimization": bool(redundancy and int(redundancy) > 1),
                })
    return targets


def _candidate_actions(targets, existing, candidates, catalog, tower_colocation=None):
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
    # 真实铁塔共塔候选（TowerColocationCandidate）：复用同一个 CandidateSite 契约，
    # 因此和用户导入的候选站址走同一条 action 生成路径。它们的 reuse_class 是
    # ``tower_colocation_host``，由 ReuseFirstSitePlannerV1 的 tier 词典序保证
    # "共塔优先、普通候选站 fallback"。
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
        metadata.get("planning_host") if isinstance(metadata.get("planning_host"), dict) else None
    )
    is_tower_host = reuse_class == TOWER_COLOCATION_REUSE_CLASS
    # 站点显式声明的可用分系统（仅当源数据/用户真的声明过）。
    declared = [
        str(item).upper() for item in (site.get("available_subsystems") or []) if str(item).strip()
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
    if geometry.get("confirmed") is not True or geometry.get("model") not in ("sphere", "hemisphere") or geometry.get("slant_range_m") is None:
        unknown.append("缺少 confirmed sphere/hemisphere coverage geometry")
    model = device.get("service_model") or {}
    if model.get("model_family") == "unsupported":
        reasons.append("device service model unsupported")
    elif model.get("confirmed") is not True:
        unknown.append("device ServiceModelSpec 未确认")
    coordinate = site.get("coordinate")
    if not isinstance(coordinate, list) or len(coordinate) < 2:
        reasons.append("站点坐标缺失")

    # 分系统兼容性（两层语义，绝不把"无证据"当成"都能装"）：
    #   * 站点**显式声明**了可用分系统且不含该设备的分系统 ⇒ 硬冲突（ineligible）；
    #   * 站点声明了且包含 ⇒ declared_compatible；
    #   * 站点没有任何声明（例如真实铁塔候选 available_subsystems=[]）⇒ **unverified**：
    #     它既不是"已证明可装"，也**不**因此把规划方案排除掉 ——
    #     共塔方案仍可作为工程 Proposal 参与 P11/P16 what-if 比较。
    if declared and device_subsystem and device_subsystem not in declared:
        reasons.append("站点声明的可用分系统不包含该设备分系统")
        subsystem_mount_status = "declared_not_compatible"
    elif declared:
        subsystem_mount_status = "declared_compatible"
    else:
        subsystem_mount_status = "unverified"

    status = "ineligible" if reasons else "unknown" if unknown else "eligible"
    eligibility_reasons = [*reasons, *unknown]
    action_type = "add_device_to_existing_facility" if is_existing else "add_device_to_explicit_site"
    return {
        "action_id": f"{reuse_class or 'unknown'}:{identifier}:{device_id}",
        "action_type": action_type,
        "site_id": site.get("site_id"),
        "facility_id": site.get("facility_id") if is_existing else None,
        "device_id": device_id,
        "subsystem": device.get("subsystem"),
        "reuse_class": reuse_class,
        "coordinate": deepcopy(coordinate),
        "vertical": vertical,
        "planning_profile": profile,
        # 共塔候选的宿主溯源：只搬运事实，不推断任何设备/安装结论。
        "host": deepcopy(host),
        "planning_origin": deepcopy(metadata.get("planning_origin")),
        # 规划层 / 实施层状态（共塔候选才适用；其它站点为 None = 不适用）。
        "planning_host_status": (
            (planning_host or {}).get("planning_host_status") if is_tower_host else None
        ),
        "subsystem_mount_status": subsystem_mount_status,
        "physical_mount_confirmed": False if is_tower_host else None,
        "requires_site_survey": True if is_tower_host else None,
        "source": str(profile.get("source") or site.get("source") or "未记录"),
        "confirmed": status == "eligible",
        "eligibility": {"status": status, "reasons": eligibility_reasons},
    }


def _apply_hypothetical_action(existing, action):
    collection = deepcopy(existing or {})
    collection.setdefault("items", [])
    installed = {
        "device_id": action["device_id"],
        "subsystem": action["subsystem"],
        "status": "active",
        # P8 treats an installed service model as a facility-level override.
        # Mark the override as absent so the hypothetical provider continues
        # to use the confirmed DeviceCatalog model instead of shadowing it
        # with an empty mapping.
        "service_model": {"status": "missing_data"},
        "metadata": {"hypothetical_action_id": action["action_id"]},
    }
    if action.get("facility_id"):
        facility = next(
            item for item in collection["items"]
            if str(item.get("facility_id")) == str(action["facility_id"])
        )
        facility.setdefault("devices", []).append(installed)
    else:
        collection["items"].append({
            "facility_id": f"proposal:{action['site_id']}",
            "site_id": action["site_id"],
            "name": f"Proposal {action['site_id']}",
            "coordinate": deepcopy(action["coordinate"]),
            "vertical_profile": deepcopy(action["vertical"]),
            "devices": [installed], "status": "active",
            "source": "P11 hypothetical what-if",
            "metadata": {"proposal_only": True, "action_id": action["action_id"]},
        })
    collection["count"] = len(collection["items"])
    return collection


def _resolved_intervals(targets, what_if_gap):
    routes = {str(item.get("route_id")): item for item in what_if_gap.get("routes") or []}
    results, unknown = [], False
    for target in targets:
        route = routes.get(target["route_id"]) or {}
        subsystem = next(
            (item for item in route.get("subsystems") or [] if item.get("subsystem") == target["subsystem"]),
            {},
        )
        resolved = []
        observed = False
        for segment in subsystem.get("segments") or []:
            start = max(target["start_route_offset_m"], float(segment.get("start_route_offset_m") or 0.0))
            end = min(target["end_route_offset_m"], float(segment.get("end_route_offset_m") or 0.0))
            if end <= start:
                continue
            observed = True
            if segment.get("planning_status") == "satisfied":
                resolved.append([start, end])
            elif segment.get("planning_status") == "unknown":
                unknown = True
        reduction = sum(end - start for start, end in _union_intervals(resolved))
        results.append({
            "segment_id": target["segment_id"],
            "target_length_m": target["length_m"],
            "resolved_intervals": _union_intervals(resolved),
            "planning_gap_reduction_m": reduction,
            "status": "resolved" if reduction >= target["length_m"] - 1e-7 else "partially_resolved" if reduction > 0 else "unknown" if not observed else "unresolved",
        })
    return results, unknown


def _union_intervals(intervals):
    values = sorted((float(item[0]), float(item[1])) for item in intervals if float(item[1]) > float(item[0]))
    result = []
    for start, end in values:
        if result and start <= result[-1][1] + 1e-7:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _empty_impact(action_id, status, reasons):
    return {
        "action_id": action_id, "status": status,
        "resolved_segment_ids": [], "affected_segment_ids": [],
        "planning_gap_reduction_m": 0.0, "segment_resolutions": [],
        "reasons": list(reasons), "evidence": [],
    }
