"""P16 cumulative corridor what-if orchestration (proposal only)."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

from ..algorithms.corridor.v1 import nearest_route_position
from ..domain.corridor_site_planning import (
    TARGET_SCOPE_SERVICE, corridor_target_id, corridor_voxel_entry_index,
    normalize_corridor_site_planning_policy,
)
from ..domain.cns_performance import type_gate_items
from ..domain.site_planning import REUSE_TIERS
from ..domain.surface_classification import (
    surface_class_provider_for, surface_facts_fingerprint_for,
)
from ..domain.cns_service_contract import (
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION, SERVICE_KEY_RADAR_NONCOOPERATIVE,
    SERVICE_SURFACE_POLICY, index_max_range_m, service_key_for, service_policy,
)
from ..domain.cns_service_registry import planner_family_for, service_registry_entry
from ..domain.navigation_augmentation import (
    build_navigation_service_evidence,
)
from ..domain.radar_service_evidence import (
    build_radar_service_evidence, radar_candidate_actions,
    radar_what_if_service_evidence,
)
from .navigation_reference_station_planning_service import (
    PLANNER_FAMILY as NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY,
    hypothetical_navigation_sites,
    navigation_evidence_required,
    navigation_reference_station_candidate_actions,
)
from .planning_evidence_service import (
    #: 需求块与机载档案必须与 P8 使用**同一份**入口，否则 P16 的残余原因会与
    #: 真实门禁口径分叉（Round 2.6.1 收口）。
    _requirement_blocks, _subsystem_name, aircraft_profile_with_evidence,
)
from .site_candidate_actions import candidate_actions
from .production_write_authority import assert_write_authority


class CorridorSitePlanningService:
    def __init__(self, session, planner, corridor_model, corridor_gap_analyzer,
                 invalidation, snapshot):
        self.session, self.planner = session, planner
        self.corridor_model, self.corridor_gap_analyzer = corridor_model, corridor_gap_analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_corridor_site_plan") or self.planner.empty())

    def evaluate(self, payload=None):
        assert_write_authority(self, "cns_corridor_site_plan")
        self.__dict__.pop("_navigation_evidence_cache", None)
        #: 走廊单元缓存的失效点：一次 P16 evaluate 内网格事实不变，跨 evaluate 必须重算。
        self.__dict__.pop("_prepared_cells_cache", None)
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("corridor site planning 请求必须是对象")
        state = self.session.state
        if "corridor_site_planning_policy" in payload:
            policy = normalize_corridor_site_planning_policy(payload["corridor_site_planning_policy"])
            if policy != state.get("corridor_site_planning_policy"):
                state["corridor_site_planning_policy"] = policy
                self.invalidation.cns_corridor_site_plan()
        baseline_corridor = state.get("cns_corridor_assessment") or {}
        baseline_gap = state.get("cns_corridor_gap_assessment") or {}
        if baseline_corridor.get("status") in (None, "stale", "not_calculated", "missing_data"):
            return self._save_missing("P14 cns_corridor_assessment 必须是 current")
        if baseline_gap.get("status") in (None, "stale", "not_calculated", "missing_data"):
            return self._save_missing("P15 cns_corridor_gap_assessment 必须是 current")
        targets, unknown = _targets(baseline_gap)
        unknown.extend(_objective_evidence_required(baseline_gap))
        ordinary_targets = [
            item for item in targets
            if item.get("service_key") != SERVICE_KEY_RADAR_NONCOOPERATIVE
        ]
        actions = candidate_actions(
            ordinary_targets, state.get("existing_cns_facilities") or {},
            state.get("candidate_sites") or {}, state.get("device_catalog") or {},
            state.get("tower_colocation_candidates") or {},
        )
        actions.extend(radar_candidate_actions(
            targets, state.get("radar_surveillance_layout") or {},
        ))
        #: Round C：导航增强站址候选**不**走普通 site × device 笛卡尔积，也**不**用
        #: radius circle planner；它只由显式的 navigation_site_suitability 站址产生。
        baseline_navigation_evidence = self._navigation_evidence(state)
        actions.extend(navigation_reference_station_candidate_actions(
            targets,
            existing_facilities=state.get("existing_cns_facilities") or {},
            candidate_sites=state.get("candidate_sites") or {},
            tower_colocation=state.get("tower_colocation_candidates") or {},
            navigation_evidence=baseline_navigation_evidence,
        ))
        if baseline_navigation_evidence is not None:
            unknown.extend(navigation_evidence_required(baseline_navigation_evidence))
        actions = sorted(actions, key=lambda item: item["action_id"])
        policy = state.get("corridor_site_planning_policy") or normalize_corridor_site_planning_policy()
        #: Round 2.2 性能：同一份 grid / terrain / surface facts 下的走廊单元只准备一次，
        #: 并在所有 what-if 之间复用（实测每轮重复准备 8008 个单元约 6.4 s）。
        #: 另对"几何上不可能触及任何走廊体素"的候选做**保守预筛**：这类动作重算前后
        #: 逐字段相同（P14 的 envelope 剪枝保证超出包络的 provider 连 unknown 都不产生），
        #: 因此预筛只否决 confirmed 增益必然为 0 的动作，绝不改变任何判定规则。
        prepared_cells = self._prepared_cells(state)
        prefilter = _build_prefilter_context(state, prepared_cells)
        selected, trace, all_impacts = [], [], []
        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        current_corridor, current_gap = deepcopy(baseline_corridor), deepcopy(baseline_gap)
        selected_ids = set()
        selected_radar_actions = []
        selected_navigation_actions = []
        stop_reason = None
        for tier in REUSE_TIERS:
            while True:
                if _confirmed_objectives_met(current_gap):
                    stop_reason = "all_evaluable_confirmed_objectives_met"
                    break
                tier_actions = [
                    action for action in actions
                    if action.get("reuse_class") == tier and action.get("action_id") not in selected_ids
                ]
                evaluated = []
                for action in tier_actions:
                    impact, _, _ = self._what_if(
                        action, facilities, current_gap, targets,
                        iteration=len(selected) + 1,
                        radar_actions=selected_radar_actions,
                        navigation_actions=selected_navigation_actions,
                        baseline_navigation_evidence=baseline_navigation_evidence,
                        prepared_cells=prepared_cells, prefilter=prefilter,
                    )
                    evaluated.append(impact)
                    all_impacts.append(deepcopy(impact))
                ranked = self.planner.rank(tier_actions, evaluated)
                if not ranked:
                    break
                winner = ranked[0]
                action, impact = winner["action"], winner["impact"]
                if action.get("planner_family") == "directional_radar":
                    selected_radar_actions.append(deepcopy(action))
                elif action.get("planner_family") == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
                    # 导航站是 caller-owned 假想 provider：**不**写入正式 ExistingCNS，
                    # 也不并入 facilities，只作为重算 P14/P15 的显式输入累积。
                    selected_navigation_actions.append(deepcopy(action))
                else:
                    facilities = _apply_cumulative_action(facilities, action)
                current_corridor, current_gap = self._rerun(
                    facilities, radar_actions=selected_radar_actions,
                    navigation_actions=selected_navigation_actions,
                    baseline_navigation_evidence=baseline_navigation_evidence,
                    prepared_cells=prepared_cells,
                )
                selected_ids.add(action["action_id"])
                chosen = {
                    **deepcopy(action), "impact": deepcopy(impact),
                    "iteration": len(selected) + 1,
                    "marginal_confirmed_requirement_unit_volume_gain": impact["confirmed_requirement_unit_volume_gain"],
                    "selection_score": winner["selection_score"],
                    "score_semantics": winner["score_semantics"],
                    "explicit_cost": winner["explicit_cost"], "cost_unit": winner["cost_unit"],
                }
                chosen.update(_selected_action_audit(chosen, targets, state))
                selected.append(chosen)
                trace.append({
                    "iteration": len(selected), "reuse_class": tier,
                    "selected_action_id": action["action_id"],
                    "marginal_impact": deepcopy(impact),
                    "corridor_fingerprint": current_corridor.get("input_fingerprint"),
                    "corridor_gap_fingerprint": current_gap.get("input_fingerprint"),
                })
            if stop_reason:
                break
        final_facilities = deepcopy(state.get("existing_cns_facilities") or {})
        final_radar_actions = []
        final_navigation_actions = []
        for action in selected:
            family = action.get("planner_family")
            if family == "directional_radar":
                final_radar_actions.append(deepcopy(action))
            elif family == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
                final_navigation_actions.append(deepcopy(action))
            else:
                final_facilities = _apply_cumulative_action(final_facilities, action)
        final_corridor, final_gap = (
            self._rerun(
                final_facilities, radar_actions=final_radar_actions,
                navigation_actions=final_navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells,
            ) if selected
            else (deepcopy(baseline_corridor), deepcopy(baseline_gap))
        )
        consistency = (
            final_corridor.get("input_fingerprint") == current_corridor.get("input_fingerprint")
            and final_gap.get("input_fingerprint") == current_gap.get("input_fingerprint")
        )
        result = self.planner.assemble(
            baseline=baseline_gap, final=final_gap, policy=policy,
            targets=targets, actions=actions, impacts=all_impacts,
            selected=selected, iteration_trace=trace,
            unknown_evidence=unknown, consistency=consistency,
        )
        final_impact = _impact({"action_id": "final-combined"}, baseline_gap, final_gap, targets)
        cumulative_gain = result.get("confirmed_requirement_unit_volume_gain", 0.0)
        authoritative_gain = final_impact.get("confirmed_requirement_unit_volume_gain", 0.0)
        result.update({
            "baseline_corridor_fingerprint": baseline_corridor.get("input_fingerprint"),
            "baseline_corridor_gap_fingerprint": baseline_gap.get("input_fingerprint"),
            "final_hypothetical_corridor_fingerprint": final_corridor.get("input_fingerprint"),
            "final_hypothetical_corridor_gap_fingerprint": final_gap.get("input_fingerprint"),
            "stop_reason": stop_reason or "no_positive_confirmed_marginal_gain",
            "cumulative_predicted_requirement_unit_volume_gain": cumulative_gain,
            "confirmed_requirement_unit_volume_gain": authoritative_gain,
            "combined_what_if_gain_difference": authoritative_gain - cumulative_gain,
            "final_combined_impact": final_impact,
            "final_hypothetical_evidence": {
                "corridor": deepcopy(final_corridor), "corridor_gap": deepcopy(final_gap),
                "persisted_as_upstream": False,
            },
        })
        result["origin_tier_statistics"] = _origin_tier_statistics(actions, selected)
        estimated_ids = sorted({
            str(item.get("tower_id")) for item in selected
            if item.get("origin_status") == "estimated" and item.get("tower_id")
        })
        result["estimated_origin_dependency_count"] = len(estimated_ids)
        result["estimated_origin_tower_ids"] = estimated_ids
        result["site_survey_required"] = bool(estimated_ids) or any(
            item.get("requires_site_survey") is True for item in selected
        )
        result["planning_height_assumptions"] = (
            ["部分共塔站址高程为工程估计，实施前需现场勘察确认。"]
            if estimated_ids else []
        )
        if not selected and (_confirmed_objectives_met(baseline_gap) or (not targets and not unknown)):
            result["status"] = "no_action_required"
            result["stop_reason"] = (
                "confirmed_objectives_already_met"
                if _confirmed_objectives_met(baseline_gap)
                else "no_confirmed_targets_or_unknown_evidence"
            )
        elif not selected and not targets and unknown:
            result["status"] = "evidence_required"
            result["stop_reason"] = "only_unknown_or_missing_evidence"
        elif not selected:
            result["status"] = "no_eligible_proposal"
        result["residual_gap_diagnostics"] = _residual_gap_diagnostics(
            baseline_gap, actions, state,
        ) if result.get("status") == "no_eligible_proposal" else []
        self.invalidation.cns_plan_review("p16_reevaluated")
        state["cns_corridor_site_plan"] = result
        state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = {
            "proposal_ready": "passed", "no_action_required": "passed",
            "evidence_required": "pending_confirmation",
            "no_eligible_proposal": "failed",
        }.get(result.get("status"), "missing_data")
        self.session.save()
        return self.snapshot()

    def _save_missing(self, reason):
        assert_write_authority(self, "cns_corridor_site_plan")
        self.invalidation.cns_plan_review("p16_became_missing")
        result = self.planner.empty("missing_data")
        result["reasons"] = [reason]
        self.session.state["cns_corridor_site_plan"] = result
        self.session.state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = "missing_data"
        self.session.save()
        return self.snapshot()

    def _what_if(
        self, action, facilities, before_gap, targets, iteration, radar_actions=None,
        navigation_actions=None, baseline_navigation_evidence=None,
        prepared_cells=None, prefilter=None,
    ):
        eligibility = action.get("eligibility") or {}
        if eligibility.get("status") != "eligible":
            return ({
                "action_id": action.get("action_id"), "iteration": iteration,
                "status": eligibility.get("status", "ineligible"),
                "confirmed_requirement_unit_volume_gain": 0.0,
                "newly_met_confirmed_objectives": 0,
                "reasons": deepcopy(eligibility.get("reasons") or []),
                "evidence": [], "regressions": [],
            }, None, None)
        prefiltered = _prefilter_impact(action, iteration, prefilter)
        if prefiltered is not None:
            return (prefiltered, None, None)
        family = action.get("planner_family")
        if family == "directional_radar":
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=[*(radar_actions or []), action],
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells,
            )
        elif family == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=radar_actions,
                navigation_actions=[*(navigation_actions or []), action],
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells,
            )
        else:
            after_corridor, after_gap = self._rerun(
                _apply_cumulative_action(facilities, action),
                radar_actions=radar_actions,
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells,
            )
        impact = _impact(action, before_gap, after_gap, targets)
        profile = provider_reason_profile(after_corridor)
        #: Round 2.3：把"哪一层拿不到证据"直接并入原因聚合 —— 否则真实项目里
        #: Communication（provider 类型门禁）与 RID（机载能力/服务模型）会被同一句
        #: 笼统文本淹没，`no_eligible_proposal` 无法自证是工程结论。
        if impact.get("unknown_targets"):
            aircraft = "、".join(
                f"{key}×{count}" for key, count in
                sorted((profile.get("p8_status_counts") or {}).items(),
                       key=lambda item: -item[1])[:3]
            )
            if aircraft:
                impact["unknown_reason_counts"][f"P8 判定汇总：{aircraft}"] = len(
                    impact["unknown_targets"]
                )
            summary = "、".join(
                f"{key}×{count}" for key, count in
                sorted((profile.get("provider_status_counts") or {}).items(),
                       key=lambda item: -item[1])[:4]
            )
            if summary:
                impact["unknown_reason_counts"][f"provider 判定汇总：{summary}"] = len(
                    impact["unknown_targets"]
                )
            services = "、".join(
                f"{key}×{count}" for key, count in
                sorted((profile.get("service_evidence_counts") or {}).items(),
                       key=lambda item: -item[1])[:4]
            )
            if services:
                impact["unknown_reason_counts"][f"P14 服务证据汇总：{services}"] = len(
                    impact["unknown_targets"]
                )
        impact.update({
            "iteration": iteration,
            "provider_reason_profile": profile,
            "evidence": [
                {"kind": "p14_cumulative_what_if", "algorithm_id": after_corridor.get("algorithm_id"),
                 "algorithm_version": after_corridor.get("algorithm_version"),
                 "input_fingerprint": after_corridor.get("input_fingerprint"), "persisted": False},
                {"kind": "p15_cumulative_what_if", "algorithm_id": after_gap.get("algorithm_id"),
                 "algorithm_version": after_gap.get("algorithm_version"),
                 "input_fingerprint": after_gap.get("input_fingerprint"), "persisted": False},
            ],
        })
        return impact, after_corridor, after_gap

    def _prepared_cells(self, state):
        """准备（并缓存）走廊候选单元，供一次 evaluate 内的所有 what-if 复用。

        只依赖 ``grid`` / ``grid_attributes`` / surface facts，因此在整个 evaluate 内
        是常量；缓存在 :meth:`evaluate` 入口清空，绝不跨请求复用。
        """

        cached = self.__dict__.get("_prepared_cells_cache")
        if cached is not None:
            return cached
        prepare = getattr(self.corridor_model, "prepare_cells", None)
        if prepare is None:
            return None
        cells = prepare(
            state.get("grid") or {}, state.get("grid_attributes") or {},
            surface_class_provider_for(state),
        )
        self.__dict__["_prepared_cells_cache"] = cells
        return cells

    def _navigation_evidence(self, state, navigation_actions=None):
        """构造 navigation augmentation 证据；未显式要求该服务时返回 ``None``。

        P16 的 what-if 会对同一批候选反复重跑，因此同一组导航动作的证据在**一次
        evaluate 内**缓存复用（证据只依赖 state 与动作集合，是确定性纯函数）。
        """

        key = tuple(sorted(str(item.get("action_id")) for item in navigation_actions or []))
        cache = self.__dict__.setdefault("_navigation_evidence_cache", {})
        if key and key in cache:
            return deepcopy(cache[key])
        evidence = build_navigation_service_evidence(
            state.get("required_cns") or {},
            route_ids=[item.get("route_id") for item in state.get("operational_routes") or []],
            existing_facilities=state.get("existing_cns_facilities") or {},
            candidate_sites=state.get("candidate_sites") or {},
            tower_colocation=state.get("tower_colocation_candidates") or {},
        )
        if evidence is not None and navigation_actions:
            _, evidence = hypothetical_navigation_sites(evidence, navigation_actions)
        if key:
            cache[key] = deepcopy(evidence)
        return evidence

    def _rerun(
        self, facilities, radar_actions=None, navigation_actions=None,
        baseline_navigation_evidence=None, prepared_cells=None,
    ):
        radar_evidence = None
        if radar_actions:
            radar_evidence = radar_what_if_service_evidence(
                self.session.state.get("required_cns") or {},
                self.session.state.get("radar_surveillance_layout") or {},
                radar_actions,
            )
        navigation_evidence = baseline_navigation_evidence
        if navigation_evidence is None:
            # 与正式 P14 同一口径：显式要求该服务才构造证据（否则保持 legacy shape）。
            navigation_evidence = self._navigation_evidence(self.session.state)
        if navigation_actions:
            navigation_evidence = self._navigation_evidence(
                self.session.state, navigation_actions,
            )
        return rerun_corridor_chain(
            self.session.state, self.corridor_model,
            self.corridor_gap_analyzer, facilities,
            radar_service_evidence=radar_evidence,
            navigation_service_evidence=navigation_evidence,
            prepared_cells=prepared_cells,
        )


def rerun_corridor_chain(
    state, corridor_model_prototype, corridor_gap_prototype, facilities,
    *, radar_service_evidence=None, navigation_service_evidence=None,
    prepared_cells=None,
):
    """Run the existing P14→P15 chain on caller-owned working data.

    ``prepared_cells`` 只用于跳过**同一份** grid / terrain / surface facts 下的重复准备
    （P16 cumulative what-if）。不传时 P14 自行准备，行为与结果逐字段不变。
    """
    profile = aircraft_profile_with_evidence(state)
    selections = state.get("algorithm_selection") or {}
    corridor_model = corridor_model_prototype.__class__(
        (state.get("cns_corridor_assessment") or {}).get("parameters")
        or getattr(corridor_model_prototype, "parameters", {})
    )
    if radar_service_evidence is None:
        radar_service_evidence = build_radar_service_evidence(
            state.get("required_cns") or {}, state.get("radar_surveillance_layout") or {},
            route_ids=[item.get("route_id") for item in state.get("operational_routes") or []],
        )
    corridor = corridor_model.evaluate(
        state.get("operational_routes") or [], state.get("spatial_3d") or {},
        state.get("grid") or {}, state.get("grid_attributes") or {},
        state.get("required_cns") or {}, profile, facilities,
        state.get("device_catalog") or {}, state.get("cns_corridor_policy") or {},
        coverage_parameters=((selections.get("coverage_model") or {}).get("parameters") or {}),
        capability_parameters=((selections.get("service_model") or {}).get("parameters") or {}),
        #: Round 2：P16 的 what-if 重算与正式 P14 共用同一份 surface facts，
        #: 否则 what-if 的 surface 判定会与基线不一致。
        surface_class_provider=surface_class_provider_for(state),
        surface_facts_fingerprint=surface_facts_fingerprint_for(state),
        radar_service_evidence=radar_service_evidence,
        navigation_service_evidence=navigation_service_evidence,
        prepared_cells=prepared_cells,
    )
    analyzer = corridor_gap_prototype.__class__(
        (state.get("cns_corridor_gap_assessment") or {}).get("parameters")
        or getattr(corridor_gap_prototype, "parameters", {})
    )
    gap = analyzer.evaluate(
        corridor, state.get("required_cns") or {},
        state.get("cns_planning_objectives") or {},
    )
    return corridor, gap


def _selected_action_audit(action, targets, state):
    """Attach the compact, evidence-tiered audit record required for each P16 selection."""

    target_ids = set(((action.get("impact") or {}).get("confirmed_targets") or []))
    matched = [item for item in targets or [] if item.get("target_id") in target_ids]
    surfaces = sorted({str(item.get("surface_class")) for item in matched if item.get("surface_class")})
    required = [int(item.get("required_units") or 1) for item in matched]
    coordinate = action.get("coordinate")
    distances = []
    if isinstance(coordinate, list) and len(coordinate) >= 2:
        for route in state.get("operational_routes") or []:
            if len(route.get("path") or []) >= 2:
                distances.append(float(nearest_route_position(route["path"], coordinate)["distance_m"]))
    radius_values = []
    device_id = str(action.get("device_id") or "")
    for device in (state.get("device_catalog") or {}).get("items") or []:
        if str(device.get("device_id") or "") != device_id:
            continue
        geometry = device.get("coverage_geometry") or {}
        mapping = geometry.get("radius_by_surface") or {}
        for surface in surfaces:
            value = mapping.get(surface)
            if isinstance(value, (int, float)):
                radius_values.append(float(value))
        if not radius_values:
            envelope = index_max_range_m(geometry)
            if envelope:
                radius_values.append(float(envelope))
        break
    return {
        "distance_to_route_m": min(distances) if distances else None,
        "coverage_radius_m": max(radius_values) if radius_values else None,
        "surface_class": surfaces[0] if len(surfaces) == 1 else (
            "mixed" if surfaces else None
        ),
        "surface_classes": surfaces,
        "required_redundancy": max(required) if required else None,
        "physical_mount_confirmed": False
        if action.get("reuse_class") == "tower_colocation_host"
        else action.get("physical_mount_confirmed"),
        "requires_site_survey": True
        if action.get("reuse_class") == "tower_colocation_host"
        else action.get("requires_site_survey"),
    }


def _origin_tier_statistics(actions, selected):
    """Candidate/eligible/selected counts split by service and origin evidence tier."""

    selected_ids = {str(item.get("action_id")) for item in selected or []}
    result = {}
    for service_key in ("C:communication", "S:rid_cooperative"):
        service_actions = [
            item for item in actions or []
            if item.get("service_key") == service_key
            and item.get("reuse_class") == "tower_colocation_host"
        ]
        service_result = {}
        for status, label in (("confirmed", "confirmed_origin"), ("estimated", "estimated_origin")):
            tier = [item for item in service_actions if item.get("origin_status") == status]
            service_result[label] = {
                "candidate": len(tier),
                "eligible": sum(
                    (item.get("eligibility") or {}).get("status") == "eligible" for item in tier
                ),
                "selected": sum(str(item.get("action_id")) in selected_ids for item in tier),
            }
        result[service_key] = service_result
    return result


def _residual_gap_diagnostics(baseline_gap, actions, state, *, nearby_limit=5):
    """Explain every unresolved continuous gap using nearby real-tower candidates.

    This is diagnostic only: it does not relax geometry, redundancy, device, or evidence
    gates and does not turn an unknown provider evaluation into confirmed coverage.
    """

    routes = {
        str(item.get("route_id") or ""): item
        for item in state.get("operational_routes") or []
    }
    profile = aircraft_profile_with_evidence(state) or {}
    required_blocks = _requirement_blocks(state)
    route_speed = _planning_route_speed_mps(profile)
    device_by_id = {
        str(item.get("device_id") or ""): item
        for item in (state.get("device_catalog") or {}).get("items") or []
    }
    diagnostics = []
    for route_gap in (baseline_gap or {}).get("routes") or []:
        route_id = str(route_gap.get("route_id") or "")
        route = routes.get(route_id) or {}
        path = route.get("path") or []
        if len(path) < 2:
            continue
        for subsystem in route_gap.get("subsystems") or []:
            service_entries = subsystem.get("service_redundancy") or []
            for segment in subsystem.get("continuous_deficit_segments") or []:
                start = segment.get("start_route_offset_m")
                end = segment.get("end_route_offset_m")
                length = segment.get("length_m")
                for service in service_entries:
                    service_key = str(service.get("service_key") or "")
                    tower_actions = [
                        item for item in actions or []
                        if item.get("reuse_class") == "tower_colocation_host"
                        and item.get("service_key") == service_key
                        and isinstance(item.get("coordinate"), list)
                        and len(item["coordinate"]) >= 2
                    ]
                    nearby = []
                    surfaces = sorted((service.get("surface_class_counts") or {}).keys())
                    surface = surfaces[0] if len(surfaces) == 1 else (
                        "mixed" if surfaces else None
                    )
                    required = service.get("required_distinct_site_count_by_surface") or {}
                    required_redundancy = max(required.values()) if required else None
                    for action in tower_actions:
                        nearest = nearest_route_position(path, action["coordinate"])
                        offset = float(nearest.get("route_offset_m") or 0.0)
                        if start is not None and end is not None and not (
                            float(start) <= offset <= float(end)
                        ):
                            continue
                        device = device_by_id.get(str(action.get("device_id") or "")) or {}
                        radius = _action_coverage_radius_m(device, surfaces)
                        category, reason = _tower_residual_reason(
                            action, service_key, profile, required_blocks,
                            float(nearest.get("distance_m") or 0.0), radius,
                        )
                        nearby.append({
                            "tower_id": action.get("tower_id"),
                            "origin_tier": action.get("origin_status"),
                            "distance_to_route_m": float(nearest.get("distance_m") or 0.0),
                            "nearest_route_chainage_m": offset,
                            "service_key": service_key,
                            "device_id": action.get("device_id"),
                            "coverage_radius_m": radius,
                            "eligibility": (action.get("eligibility") or {}).get("status"),
                            "residual_reason_category": category,
                            "why_cannot_eliminate_gap": reason,
                        })
                    nearby.sort(key=lambda item: (
                        item["distance_to_route_m"], str(item.get("tower_id") or "")
                    ))
                    diagnostics.append({
                        "route_id": route_id,
                        "segment_id": segment.get("segment_id"),
                        "service_key": service_key,
                        "start_chainage_m": start,
                        "end_chainage_m": end,
                        "length_m": length,
                        "duration_s": (
                            float(length) / route_speed
                            if isinstance(length, (int, float)) and route_speed else None
                        ),
                        "route_speed_mps": route_speed,
                        "surface_class": surface,
                        "required_redundancy": required_redundancy,
                        "nearby_real_towers": nearby[:nearby_limit],
                        "nearby_limit": nearby_limit,
                    })
    return diagnostics


def _planning_route_speed_mps(profile):
    value = profile.get("cruise_speed_mps")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    for item in (profile.get("metadata") or {}).get("fc30_facts") or []:
        if item.get("parameter") == "route_speed_mps":
            value = item.get("value")
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


def _action_coverage_radius_m(device, surfaces):
    geometry = device.get("coverage_geometry") or {}
    mapping = geometry.get("radius_by_surface") or {}
    values = [
        float(mapping[surface]) for surface in surfaces
        if isinstance(mapping.get(surface), (int, float))
    ]
    if values:
        return max(values)
    value = index_max_range_m(geometry)
    return float(value) if value else None


def _provider_type_gaps(required_type, device_type):
    """提供者（设备目录）相对需求类型门禁的**逐字段**缺口（P8 第一步的同一口径）。

    Round 2.6.1：缺声明一律是证据不足（``unknown``），只有真实冲突（接口无交集 /
    取值不匹配）才是确认不满足 —— 两者绝不混为一谈。
    """

    gaps = []
    for key, expected in type_gate_items(required_type):
        observed = (device_type or {}).get(key)
        if observed in (None, "", "unknown", []):
            gaps.append(f"provider 未声明 {key}（需求要求 {expected}）")
        elif key == "interfaces":
            if not set(expected).issubset(set(observed)):
                gaps.append(
                    f"provider interfaces 不满足（需求要求 {expected}，设备声明 {observed}）"
                )
        elif observed != expected:
            gaps.append(f"provider {key} 不匹配（需求要求 {expected}，设备声明 {observed}）")
    return gaps


def _aircraft_participation_gaps(required_type, profile, code, service_key):
    """机载档案相对**机载参与谓词**的逐字段缺口（P8 第二步的同一口径）。"""

    from ..domain.cns_service_contract import airborne_type_items

    capability = (profile or {}).get(_subsystem_name(code)) or {}
    declared_type = capability.get("type") if isinstance(capability.get("type"), dict) else {}
    gaps = []
    for field, expected in airborne_type_items(
        required_type, subsystem=code, service_key=service_key,
    ):
        declared = declared_type.get(field)
        if isinstance(expected, dict):
            entries = declared if isinstance(declared, list) else []
            matched = any(
                isinstance(entry, dict)
                and all(str(entry.get(key)) == str(value) for key, value in expected.items())
                for entry in entries
            )
            if not matched:
                gaps.append(f"机载未声明满足 {expected} 的 {field} 条目")
            continue
        if declared in (None, "", "unknown", []):
            gaps.append(f"机载未声明 {field}（需求要求 {expected}）")
        elif field == "interfaces":
            if not set(expected).issubset(set(declared)):
                gaps.append(
                    f"机载 interfaces 不满足（需求要求 {expected}，档案声明 {declared}）"
                )
        elif declared != expected:
            gaps.append(f"机载 {field} 不匹配（需求要求 {expected}，档案声明 {declared}）")
    return gaps


def _tower_residual_reason(action, service_key, profile, required_blocks, distance_m, radius_m):
    """这条共塔动作为什么不能把残余缺口升级为**已确认改善**（权威门禁口径）。

    Round 2.6.1 收口：原因必须来自真实门禁，而不是把某一侧写死。P8 的判定顺序是
    "先 provider 类型资格、后机载参与能力"，本函数按同一顺序披露。
    """

    eligibility = action.get("eligibility") or {}
    if eligibility.get("status") != "eligible":
        reasons = [str(item) for item in (eligibility.get("reasons") or [])]
        category = "device_incompatibility" if any(
            "不包含" in item or "unsupported" in item for item in reasons
        ) else "evidence_blocker"
        return category, "；".join(reasons) or "候选动作不可用"
    if radius_m is not None and distance_m > radius_m:
        return (
            "coverage_radius_insufficient",
            f"距航路 {distance_m:.1f} m，超过冻结覆盖半径 {radius_m:.1f} m",
        )
    code = str(service_key or "").split(":", 1)[0].strip().upper()
    required = (required_blocks or {}).get(_subsystem_name(code)) or {}
    required_type = required.get("type") if isinstance(required.get("type"), dict) else {}
    provider_gaps = _provider_type_gaps(required_type, action.get("device_type"))
    if provider_gaps:
        return (
            "provider_type_incompatibility",
            "提供者类型资格未确认（P8 第一步门禁）：" + "；".join(provider_gaps),
        )
    aircraft_gaps = _aircraft_participation_gaps(required_type, profile, code, service_key)
    if aircraft_gaps:
        return (
            "aircraft_participation_evidence_required",
            "机载参与能力证据不足（P8 第二步门禁）：" + "；".join(aircraft_gaps),
        )
    return (
        "other",
        "provider 类型资格与机载参与能力均已声明，但累计 what-if 仍未产生可确认的正向增益；"
        "详见 candidate_unknown_reason_summary 与 provider evidence profile",
    )


def _targets(assessment):
    """P15 → P16 targets。

    Round 2.1 契约：

    * **优先 service-level target**：P15 的 ``entry.services[]`` 中任何
      ``surface_dependent`` 的 service（Communication / RID）都会建立
      ``route|voxel|<service_key>`` target，并只读该 service 自己的
      ``required_distinct_site_count`` / ``distinct_site_count``（物理站址计数）。
      ``independence_group`` / ``confirmed_independent_provider_count`` **绝不**被用来
      代替 distinct-site 计数；
    * service 状态语义：``confirmed_deficit`` → confirmed target；``unknown`` →
      unknown evidence（绝不自动建站）；``satisfied`` → 不产生 target；缺少
      ``distinct_site_count`` 证据同样进入 unknown；
    * 只要该 voxel 存在**未满足**的 surface-dependent service evidence，就不再为其产生
      legacy subsystem target —— 否则同一缺口会被 subsystem 与 service 各计一次，并让
      RID 缺口被 S 类其它设备混池补盲；若该 voxel 的 surface service 全部 satisfied，
      则保持旧口径，绝不丢失可能的 legacy 缺口；
    * **legacy 项目**（没有任何 surface-dependent service evidence）保持旧的
      subsystem-level 行为，逐字段不变。
    """

    targets, unknown = [], []
    for route in assessment.get("routes") or []:
        route_id = str(route.get("route_id") or "")
        summaries = {str(item.get("subsystem")): item for item in route.get("subsystems") or []}
        confirmed = {
            (code, voxel_id)
            for code, summary in summaries.items()
            for voxel_id in summary.get("confirmed_target_voxel_ids") or []
        }
        unknown_ids = {
            (code, voxel_id)
            for code, summary in summaries.items()
            for voxel_id in summary.get("unknown_voxel_ids") or []
        }
        for voxel in route.get("voxels") or []:
            voxel_id = str(voxel.get("voxel_id") or "")
            for entry in voxel.get("subsystems") or []:
                code = str(entry.get("subsystem") or "")
                surface_services = [
                    item for item in entry.get("services") or []
                    if isinstance(item, dict) and (
                        item.get("surface_dependent") is True
                        or item.get("supports_site_planning") is True
                    )
                    and str(item.get("service_key") or "")
                ]
                # 只有**确实存在未满足**的 surface-dependent service 时才切换到 service 口径；
                # 全部 satisfied 时该 voxel 的（可能的）legacy 缺口仍按旧口径处理，绝不丢失。
                active_services = [
                    item for item in surface_services
                    if str(item.get("status") or "unknown") != "satisfied"
                ]
                if active_services:
                    targets.extend(_service_targets(
                        route_id, voxel_id, code, voxel, entry, active_services, unknown,
                    ))
                    continue
                target_id = corridor_target_id(route_id, voxel_id, code)
                if (code, voxel_id) in unknown_ids:
                    unknown.append({"target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                                    "subsystem": code, "reasons": deepcopy(entry.get("reasons") or [])})
                if (code, voxel_id) not in confirmed:
                    continue
                if entry.get("ground_provider_redundancy_status") == "not_applicable_to_site_provider_redundancy":
                    continue
                #: Round 2.4：**只有支持站址规划的 canonical 服务才产生建站目标**。
                #:
                #: 缺口登记在 legacy 子系统口径上时，其规范服务身份是
                #: ``N:navigation`` / ``S:surveillance`` —— 注册表把这两个身份的
                #: ``supports_site_planning`` 明确置为 ``False``（它们没有新的
                #: planner family，设备目录也不承载这类设备）。若仍然为它们产生
                #: 建站 target，就会造出一批**永远无法被任何候选动作满足**的目标
                #: （Round 2.3 实测：2745 个 N legacy target × 0 个候选动作），
                #: 从而把 Step6 的规划目标永久卡在 ``not_met``。
                #:
                #: 正确语义：**如实登记该缺口，但绝不把它当作新的建站规划目标**。
                #: 用户若确实要求地面导航增强，应显式声明 ``N:rtk_augmentation``
                #: （走 canonical adapter），而不是让 legacy 子系统缺口自动变成
                #: 规划目标。
                legacy_key = service_key_for(code)
                legacy_entry = service_registry_entry(legacy_key)
                if legacy_entry.get("supports_site_planning") is not True:
                    unknown.append({
                        "kind": "non_site_plannable_gap",
                        "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                        "subsystem": code, "service_key": legacy_key,
                        "planner_family": legacy_entry.get("planner_family"),
                        "requires_canonical_service_requirement": True,
                        "reasons": [
                            f"{legacy_entry.get('label')} 缺口不属于站址规划形态"
                            "（该服务没有 canonical planner family，设备目录也不承载这类设备）："
                            "如实登记，绝不产生无法被候选动作满足的建站目标。",
                            "如确实需要地面设备，请在 RequiredCNS 显式声明对应的 canonical 服务"
                            "（例如 Navigation 的 N:rtk_augmentation）。",
                        ],
                    })
                    continue
                required = entry.get("required_redundancy")
                required_units = max(1, int(required or 1))
                current_units = _confirmed_units(entry, required_units)
                if current_units is None:
                    unknown.append({"target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                                    "subsystem": code, "reasons": ["P15 confirmed target 缺少可确认 provider unit 证据"]})
                    continue
                targets.append({
                    "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                    "grid_id": voxel.get("grid_id"), "altitude_layer_id": voxel.get("altitude_layer_id"),
                    "subsystem": code, "required_units": required_units,
                    "current_units": current_units,
                    "remaining_units": max(0, required_units - current_units),
                    "discretized_volume_proxy_m3": voxel.get("discretized_volume_proxy_m3"),
                    "nearest_route_offset_m": voxel.get("nearest_route_offset_m"),
                    "causes": deepcopy(entry.get("causes") or []),
                    "source_status": entry.get("combined_status"),
                })
    return sorted(targets, key=lambda item: item["target_id"]), sorted(unknown, key=lambda item: item["target_id"])


def _service_targets(route_id, voxel_id, code, voxel, entry, surface_services, unknown):
    """由 surface-dependent / site-planned service evidence 建立 service-level targets。"""

    targets = []
    for service in surface_services:
        key = str(service.get("service_key") or "")
        target_id = corridor_target_id(route_id, voxel_id, key)
        status = str(service.get("status") or "unknown")
        if status == "satisfied":
            continue
        #: Round 2.4：不支持站址规划的 canonical 服务**绝不产生建站 target**
        #: （否则会造出永远无法被候选动作满足的规划目标，把 Step6 永久卡在 not_met）。
        entry_spec = service_registry_entry(key)
        if entry_spec.get("supports_site_planning") is not True:
            unknown.append({
                "kind": "non_site_plannable_gap",
                "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                "subsystem": code, "service_key": key,
                "planner_family": entry_spec.get("planner_family"),
                "requires_canonical_service_requirement": True,
                "reasons": [
                    f"{entry_spec.get('label')} 缺口不属于站址规划形态：如实登记，绝不建站。",
                ],
            })
            continue
        if key == SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION:
            _navigation_target(
                targets, unknown, route_id, voxel_id, code, voxel, entry, service, target_id,
            )
            continue
        required = service.get("required_distinct_site_count")
        current = service.get("distinct_site_count")
        if (
            status != "confirmed_deficit"
            or service.get("counting_basis") != "distinct_site_id"
            or required is None
            or current is None
        ):
            reasons = deepcopy(service.get("reasons") or [])
            reasons.append(
                "该 service 的物理 distinct-site 证据不足（或要求站址数未知）："
                "只登记 unknown evidence，绝不自动建站"
            )
            unknown.append({
                "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
                "subsystem": code, "service_key": key, "reasons": reasons,
            })
            continue
        required_units = max(1, int(required))
        current_units = min(required_units, int(current))
        targets.append({
            "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
            "grid_id": voxel.get("grid_id"), "altitude_layer_id": voxel.get("altitude_layer_id"),
            "subsystem": code, "service_key": key, "target_scope": TARGET_SCOPE_SERVICE,
            "planner_family": planner_family_for(key),
            "surface_class": service.get("surface_class") or entry.get("surface_class"),
            "counting_basis": "distinct_site_id",
            "required_units": required_units,
            "current_units": current_units,
            "remaining_units": max(0, required_units - current_units),
            "distinct_site_ids": deepcopy(service.get("distinct_site_ids")),
            "discretized_volume_proxy_m3": voxel.get("discretized_volume_proxy_m3"),
            "nearest_route_offset_m": voxel.get("nearest_route_offset_m"),
            "causes": deepcopy(entry.get("causes") or []),
            "source_status": entry.get("combined_status"),
        })
    return targets


def _confirmed_units(entry, required_units):
    if entry.get("combined_status") == "unknown":
        return None
    if required_units <= 1:
        return min(1, int(entry.get("qualified_provider_count") or 0))
    value = entry.get("confirmed_independent_provider_count")
    return None if value is None else min(required_units, int(value))


def _navigation_target(targets, unknown, route_id, voxel_id, code, voxel, entry, service, target_id):
    """``N:rtk_augmentation`` 的 P16 target：**只有站址缺口才生成建站 target**。

    Round C 冻结：

    * ``gap_causes`` 含 ``reference_station_deficit`` ⇒ 生成
      ``navigation_reference_station`` planner family 的建站 target（``remaining_units``
      只表示**站址**缺口，"correction delivery 未解决"另由 ``delivery_resolved = false``
      如实记录）；
    * 只有 ``correction_delivery_deficit``（站址几何已 satisfied）⇒ **绝不**生成导航站
      target，只登记 ``dependency_only = true`` 与
      ``recommended_dependency_service = C:communication``，把建设动作交给
      Communication planner；
    * 其它（``unknown`` / 证据不足）⇒ 只登记 unknown evidence，绝不自动建站。
    """

    causes = list(service.get("gap_causes") or [])
    status = str(service.get("status") or "unknown")
    required = service.get("required_distinct_site_count")
    current = service.get("distinct_site_count")
    if status != "confirmed_deficit" or "reference_station_deficit" not in causes:
        reasons = list(service.get("reasons") or [])
        reasons.append(
            "该 voxel 的 Navigation augmentation 缺口不含已确认的 reference-station 站址"
            "缺口：只登记 unknown / 依赖证据，绝不自动建导航站"
        )
        unknown.append({
            "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
            "subsystem": code, "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
            "dependency_only": "reference_station_deficit" not in causes
            and "correction_delivery_deficit" in causes,
            "recommended_dependency_service": (
                "C:communication" if "correction_delivery_deficit" in causes else None
            ),
            "reasons": reasons,
        })
        return
    if required is None or current is None:
        unknown.append({
            "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
            "subsystem": code, "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
            "reasons": ["Navigation augmentation 缺少 required/current distinct-site 证据"],
        })
        return
    required_units = max(1, int(required))
    current_units = min(required_units, int(current))
    targets.append({
        "target_id": target_id, "route_id": route_id, "voxel_id": voxel_id,
        "grid_id": voxel.get("grid_id"), "altitude_layer_id": voxel.get("altitude_layer_id"),
        "subsystem": code, "service_key": SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION,
        "target_scope": TARGET_SCOPE_SERVICE,
        "planner_family": planner_family_for(SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION),
        "surface_class": service.get("surface_class") or entry.get("surface_class"),
        "counting_basis": "distinct_site_id",
        "required_units": required_units,
        "current_units": current_units,
        "remaining_units": max(0, required_units - current_units),
        "distinct_site_ids": deepcopy(service.get("distinct_site_ids")),
        "gap_causes": causes,
        "delivery_resolved": "correction_delivery_deficit" not in causes,
        "delivery_status": service.get("delivery_status"),
        "delivery_service_key": service.get("delivery_service_key"),
        "max_reference_baseline_m": service.get("max_reference_baseline_m"),
        "discretized_volume_proxy_m3": voxel.get("discretized_volume_proxy_m3"),
        "nearest_route_offset_m": voxel.get("nearest_route_offset_m"),
        "causes": deepcopy(entry.get("causes") or []),
        "source_status": entry.get("combined_status"),
    })


def _service_confirmed_units(entry, required_units):
    """service-level target 的 current units：**只**取物理 distinct-site 计数。

    ``independence_group`` / ``confirmed_independent_provider_count`` 在这里没有任何
    位置：它们不是物理站址身份。缺 ``distinct_site_count`` 或状态为 ``unknown`` 时
    返回 ``None``（fail-closed，绝不当作 0 或 1 猜测）。
    """

    if not isinstance(entry, dict):
        return None
    if str(entry.get("status") or "unknown") == "unknown":
        return None
    if entry.get("counting_basis") != "distinct_site_id":
        return None
    counted = entry.get("distinct_site_count")
    if counted is None:
        return None
    return min(max(1, int(required_units or 1)), int(counted))


def _units_for_target(target, entry):
    if not isinstance(entry, dict):
        return None
    if target.get("service_key") == SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION:
        return _navigation_confirmed_units(target, entry)
    if target.get("service_key"):
        return _service_confirmed_units(entry, target.get("required_units"))
    return _confirmed_units(entry, target["required_units"])


def _navigation_confirmed_units(target, entry):
    """Navigation augmentation target 的 current units：**只**计已确认的站址基线几何。

    Correction delivery（``C:communication``）**绝不**计入导航站选址收益：纯通信缺口
    的 voxel 根本不会产生导航建站 target。这里只读 ``distinct_site_count``——**不**读
    ``gap_causes``，因为"缺口刚被补上"的那一刻 ``gap_causes`` 已经清空，若依赖它就会
    把真实收益误判成 0。缺物理站址证据（``None``）时返回 ``None``（fail-closed）。
    """

    if not isinstance(entry, dict):
        return None
    if str(entry.get("geometry_status") or "") == "unknown":
        return None
    counted = entry.get("distinct_site_count")
    if counted is None:
        return None
    required = max(1, int(target.get("required_units") or 1))
    return min(required, int(counted))


def _impact(action, before, after, targets):
    """一条 candidate action 的 what-if 影响（Round 2.2 unknown 语义）。

    **unknown 语义铁律（本轮裁定）**：

    * ``UNKNOWN ≠ PASS``：证据不足的 target **绝不**计入 ``confirmed_gain``，也绝不
      计入 ``service_resolved_volume_proxy_m3`` / ``redundancy_progress_volume_proxy_m3``；
    * ``UNKNOWN ≠ ZERO ENTIRE ACTION``：证据不足的 target 只被**独立登记与披露**，
      不得把一个动作对其它 confirmed target 的已确认增益整体抹掉；
    * 只有**真实的 confirmed 恶化**（原 satisfied voxel 变为 confirmed gap）才让整个
      动作变为 ``ineligible`` 并把增益归零；
    * ``confirmed_gain > 0`` 即允许进入候选评分，即便仍有 unknown target；此时
      ``evidence_status = confirmed_with_unknown_evidence``，且 unknown 清单照常保留。
    """

    before_entries, after_entries = _entry_map(before), _entry_map(after)
    gain = service_volume = redundancy_volume = 0.0
    progress, unknown, confirmed_targets = [], [], []
    remaining_confirmed = remaining_unknown = 0
    unknown_reasons = Counter()
    for target in targets:
        key = target["target_id"]
        left, right = before_entries.get(key), after_entries.get(key)
        if left is None or right is None:
            unknown.append(key)
            remaining_unknown += 1
            if left is not None and right is None:
                #: 该动作**覆盖到了**这个目标，但目标的服务证据在 what-if 里没有出现：
                #: 这正是"覆盖存在、合格性证据不足"的形态，必须如实说明，而不是复用
                #: baseline（已确认缺口）的原因文本。
                _finalize_unknown_reason(
                    "该动作覆盖到该目标后其服务证据仍未确认"
                    "（provider 类型/合格性证据不足）：缺口无法升级为已确认改善",
                    1, unknown_reasons,
                )
            else:
                _finalize_unknown_reason(
                    _target_unknown_reason(right or left, "target_entry_missing"),
                    1, unknown_reasons,
                )
            continue
        before_units = _units_for_target(target, left)
        after_units = _units_for_target(target, right)
        if before_units is None or after_units is None or right.get("combined_status") == "unknown":
            unknown.append(key)
            remaining_unknown += 1
            #: Round 2.3：原因必须能区分"哪一层拿不到可计数的单位"。站址计数契约
            #: （``counting_basis`` / ``distinct_site_count``）与服务状态（``status``）
            #: 一起披露，否则无法判断是几何、类型门禁还是站址身份导致的证据不足。
            if before_units is None or after_units is None:
                _finalize_unknown_reason(
                    f"{_target_unknown_reason(right, 'target_entry_missing')}"
                    f"｜计数契约 target={target.get('service_key') or target.get('subsystem')}"
                    f" before_units={before_units} after_units={after_units}"
                    f" before({left.get('counting_basis')}/{left.get('distinct_site_count')}"
                    f"/{left.get('status')})"
                    f" after({right.get('counting_basis')}/{right.get('distinct_site_count')}"
                    f"/{right.get('status')})",
                    1, unknown_reasons,
                )
            else:
                _finalize_unknown_reason(
                    _target_unknown_reason(right, "provider_evidence_incomplete"),
                    1, unknown_reasons,
                )
            continue
        if right.get("combined_status") == "confirmed_gap":
            remaining_confirmed += 1
        unit_gain = max(0, after_units - before_units)
        volume = float(target.get("discretized_volume_proxy_m3") or 0.0)
        gain += volume * unit_gain
        if left.get("service_status") == "confirmed_deficit" and right.get("service_status") == "satisfied":
            service_volume += volume
        if left.get("redundancy_status") == "confirmed_deficit" and after_units > before_units:
            redundancy_volume += volume * unit_gain
        if unit_gain:
            progress.append({
                "target_id": key, "before_units": before_units, "after_units": after_units,
                "unit_gain": unit_gain, "unit_volume_gain": volume * unit_gain,
                "after_combined_status": right.get("combined_status"),
            })
            confirmed_targets.append(key)
    regressions, unknown_regressions = _regressions(before_entries, after_entries)
    total_reduction, max_reduction = _continuous_reduction(before, after)
    newly_met = _newly_met_objectives(before, after)
    has_unknown = bool(unknown or unknown_regressions)
    if regressions:
        status, reasons = "ineligible", ["候选 what-if 使原 satisfied voxel 变为 confirmed gap"]
        gain = service_volume = redundancy_volume = 0.0
    elif gain > 0:
        #: 已确认增益成立 ⇒ 允许进入候选评分；unknown 只被独立保留与披露。
        status, reasons = "eligible", []
        if has_unknown:
            reasons.append(
                "该动作对部分 target 仍缺可确认证据：已确认增益有效，"
                "但 unknown 独立保留、不计入 satisfied、不计入冗余满足"
            )
    elif has_unknown:
        status, reasons = "unknown", [
            "what-if 产生 unknown 或关键 provider 证据不完整，且没有已确认增益"
        ]
    else:
        status, reasons = "ineligible", ["候选未产生 confirmed requirement-unit progress"]
    evidence_status = (
        "ineligible_confirmed_regression" if regressions
        else "no_confirmed_progress" if status == "ineligible"
        else "unknown_only" if status == "unknown"
        else "confirmed_with_unknown_evidence" if has_unknown
        else "fully_confirmed"
    )
    return {
        "action_id": action.get("action_id"), "status": status,
        "confirmed_requirement_unit_volume_gain": gain,
        #: 新语义的显式别名：与上面同值，供"只读已确认增益"的消费方直接使用。
        "confirmed_gain": gain,
        "confirmed_targets": confirmed_targets,
        "service_resolved_volume_proxy_m3": service_volume,
        "redundancy_progress_volume_proxy_m3": redundancy_volume,
        "newly_met_confirmed_objectives": newly_met,
        "total_continuous_deficit_projection_reduction_m": total_reduction,
        "max_continuous_deficit_projection_reduction_m": max_reduction,
        "target_progress": progress,
        #: 动作应用**之后**仍存在的缺口：confirmed 与 unknown 分开计数，绝不合并。
        "remaining_deficit": {
            "assessed_target_count": len(targets or []),
            "confirmed_deficit_targets": remaining_confirmed,
            "unknown_targets": remaining_unknown,
        },
        "evidence_status": evidence_status,
        "regressions": regressions, "unknown_regressions": unknown_regressions,
        "unknown_targets": unknown, "reasons": reasons,
        #: 不能确认增益的**原因分布**（小字段）：让"没有方案"成为可解释的工程结论，
        #: 而不是把几千条 target_id 丢给用户自己去猜。unknown 仍逐条保留在
        #: ``unknown_targets`` 里，这里只是可读聚合。
        "unknown_reason_counts": dict(unknown_reasons.most_common(8)),
    }


def _target_unknown_reason(entry, fallback):
    """从 P15/P16 entry 提取"为什么这条 target 证据不足"的可读原因（只读）。

    Round 2.3：原因文本必须**可区分**，否则 ``no_eligible_proposal`` 只能得到一句笼统的
    "证据不足"，无法判断是几何、类型门禁、服务模型、站址身份还是 surface 契约造成的。
    因此这里在既有 ``reasons`` 之前先带上**判定维度**（覆盖/服务/冗余/组合状态）。
    """

    if not isinstance(entry, dict):
        return fallback
    dimensions = []
    for label, key in (
        ("覆盖", "geometry_status"), ("服务", "service_status"),
        ("冗余", "redundancy_status"), ("组合", "combined_status"),
    ):
        value = entry.get(key)
        if value not in (None, ""):
            dimensions.append(f"{label}={value}")
    prefix = "（" + "、".join(dimensions) + "）" if dimensions else ""
    for reason in entry.get("reasons") or []:
        text = str(reason or "").strip()
        if text:
            return (prefix + text)[:200]
    status = str(entry.get("status") or entry.get("combined_status") or "").strip()
    return f"{fallback}（{status}）" if status else fallback


#: 上限：单条 action 的原因聚合最多保留的候选 reason 条数（小字段）。
#: Round 2.3：至少覆盖"每个 subsystem × 每个判定维度"一类，否则真实项目里
#: Communication（类型门禁）与 RID（机载能力）两类原因会互相挤掉。
PROVIDER_REASON_SAMPLE_LIMIT = 24


def _finalize_unknown_reason(reason, count, counter):
    """把候选 reason 文本规范化后并入计数（截断防爆，保留可读语义）。"""

    text = str(reason or "").strip() or "未记录原因"
    counter[text[:220]] += count


def provider_reason_profile(corridor):
    """what-if 之后 P14 的 provider 证据**原因画像**（只读聚合，小字段）。

    目的：让"这个动作覆盖到了目标，但增益无法确认"变成可审计的工程结论 —— 直接给出
    P8 的 ``provider_type_compatibility`` / ServiceModelSpec / 性能层各自的 status 分布、
    P14 体元的 **service 证据分布**（``service_redundancy`` 的 status / 计数契约），
    以及"覆盖存在却没拿到 qualified provider"时的**前几条真实 reason 文本**。

    只统计 ``covered is True`` 的体元（未覆盖的体元是已知缺口，不是证据不足）。
    """

    status_counts = Counter()
    stage_status_counts = Counter()
    aircraft_counts = Counter()
    service_counts = Counter()
    aircraft_reason_samples = []
    reason_samples = []
    covered_voxels = 0
    aircraft_reason_limit = 4
    for route in (corridor or {}).get("routes") or []:
        for voxel in route.get("voxels") or []:
            for entry in voxel.get("subsystems") or []:
                code = str(entry.get("subsystem") or "")
                if code not in ("C", "S"):
                    continue
                if (entry.get("geometry") or {}).get("covered") is not True:
                    continue
                covered_voxels += 1
                p8_status = str(entry.get("p8_status"))
                aircraft_counts[f"{code}:{p8_status}"] += 1
                if (
                    p8_status in ("does_not_meet_under_model", "unknown")
                    and len(aircraft_reason_samples) < aircraft_reason_limit
                ):
                    #: 覆盖成立却没走到服务证据层时，先披露**机载能力层**的真实结论
                    #: （否则只看到"没有服务条目"，无法区分是机载还是地面 provider 的问题）。
                    aircraft_reason_samples.append({
                        "subsystem": code, "voxel_id": voxel.get("voxel_id"),
                        "p8_status": p8_status,
                        "reasons": list(entry.get("reasons") or [])[:3],
                        "aircraft_evidence": [
                            item for item in entry.get("evidence") or []
                            if item.get("kind") == "aircraft_capability"
                        ][:1],
                    })
                services = entry.get("service_redundancy") or []
                if not services:
                    service_counts[f"{code}:no_service_entry"] += 1
                for service in services:
                    service_counts[
                        f"{code}:{service.get('service_key')}:{service.get('status')}"
                        f":{service.get('counting_basis')}:"
                        f"{service.get('distinct_site_count')}/"
                        f"{service.get('required_distinct_site_count')}"
                    ] += 1
                evaluations = entry.get("provider_evaluations") or []
                if not evaluations:
                    status_counts[f"{code}:no_provider_evaluation"] += 1
                    if len(reason_samples) < PROVIDER_REASON_SAMPLE_LIMIT:
                        reason_samples.append({
                            "subsystem": code, "voxel_id": voxel.get("voxel_id"),
                            "stage": None, "status": "missing",
                            "reason": f"覆盖成立但无 provider_evaluations（p8_status={entry.get('p8_status')}）",
                        })
                    continue
                for evaluation in evaluations:
                    status = str(evaluation.get("status"))
                    stage = evaluation.get("stage")
                    status_counts[f"{code}:{status}"] += 1
                    stage_status_counts[f"{code}:{stage or 'service_evaluation'}:{status}"] += 1
                    if (
                        stage == "provider_type_compatibility"
                        and status != "meets_under_model"
                        and len(reason_samples) < PROVIDER_REASON_SAMPLE_LIMIT
                    ):
                        reason_samples.append({
                            "subsystem": code, "voxel_id": voxel.get("voxel_id"),
                            "stage": stage, "status": status,
                            "reason": (evaluation.get("reasons") or [None])[0],
                            "canonical_service_identity": evaluation.get("service_key"),
                            "provider_device_id": evaluation.get("device_id"),
                        })
                    elif (
                        stage != "provider_type_compatibility"
                        and status == "unknown"
                        and len(reason_samples) < PROVIDER_REASON_SAMPLE_LIMIT
                    ):
                        reason_samples.append({
                            "subsystem": code, "voxel_id": voxel.get("voxel_id"),
                            "stage": stage or "service_evaluation", "status": status,
                            "reason": (evaluation.get("reasons") or [None])[0],
                            "canonical_service_identity": evaluation.get("service_key"),
                            "provider_device_id": evaluation.get("device_id"),
                        })
            if covered_voxels and len(reason_samples) >= PROVIDER_REASON_SAMPLE_LIMIT:
                break
    return {
        "covered_voxel_count": covered_voxels,
        "p8_status_counts": dict(aircraft_counts.most_common(12)),
        "aircraft_reason_samples": aircraft_reason_samples,
        "provider_status_counts": dict(status_counts.most_common(12)),
        "provider_stage_status_counts": dict(stage_status_counts.most_common(12)),
        "service_evidence_counts": dict(service_counts.most_common(12)),
        "reason_samples": reason_samples,
    }


def _build_prefilter_context(state, prepared_cells):
    """构造 P16 候选的**保守几何预筛**上下文；无法安全判定时返回 ``None``。

    预筛依据（全部来自既有事实，不引入任何新口径）：

    * 走廊体素的代表点都是某个网格单元的中心，且只可能出现在航路两侧
      ``horizontal_half_width_m + cell_half_diagonal`` 之内（P14 的走廊包含规则）；
    * P14 的几何判定先按 ``index_max_range_m``（设备包络）剪枝：**超出包络的 provider
      既不产生覆盖匹配，也不产生 unknown 证据**。

    因此：若某候选的新设备站点到航路的最近水平距离
    ``> envelope + max_half_width + max_cell_half_diagonal``，则该候选重算前后逐字段
    相同 ⇒ 它不可能产生任何 confirmed 增益、regression 或 unknown_regression。

    ``None`` 表示"缺少足够的静态事实进行安全判定"，此时**不做任何预筛**。
    """

    paths = [
        (str(route.get("route_id") or ""), list(route.get("path") or []))
        for route in state.get("operational_routes") or []
        if len(route.get("path") or []) >= 2
    ]
    if not paths:
        return None
    specs = ((state.get("cns_corridor_policy") or {}).get("routes") or {})
    half_widths = []
    for route_id, _ in paths:
        value = (specs.get(route_id) or {}).get("horizontal_half_width_m")
        if value is None:
            return None
        half_widths.append(float(value))
    diagonals = [
        float(item.get("half_diagonal") or 0.0) for item in prepared_cells or []
    ]
    if not diagonals:
        return None
    return {
        "state": state,
        "paths": paths,
        "max_half_width_m": max(half_widths),
        "max_cell_half_diagonal_m": max(diagonals),
    }


def _action_provider_envelope_m(action, state):
    """候选新设备在 P14 里的几何包络（``index_max_range_m``）。

    只读**设备目录**中该 ``device_id`` 的 canonical ``coverage_geometry``（
    ``_apply_cumulative_action`` 写入的 hypothetical 设备不带几何，P14 也是从设备目录
    合并几何的）。查不到时回落到该 service 的冻结 surface policy 上界；两者都不可用
    时返回 ``None``（调用方必须放弃预筛，绝不猜测）。
    """

    values = []
    device_id = str(action.get("device_id") or "")
    if device_id:
        for item in (state.get("device_catalog") or {}).get("items") or []:
            if str(item.get("device_id") or "") != device_id:
                continue
            value = index_max_range_m(item.get("coverage_geometry") or {})
            if value:
                values.append(float(value))
            break
    policy = service_policy(str(action.get("device_service_key") or ""))
    mapping = (policy or {}).get("radius_by_surface") or {}
    if mapping:
        values.append(max(float(item) for item in mapping.values()))
    return max(values) if values else None


def _prefilter_impact(action, iteration, prefilter):
    """对被预筛否决的候选返回**显式披露**的 impact；不满足预筛条件时返回 ``None``。

    返回的 impact 完整保留"为什么没有增益"的证据（最近航路距离、所需净空、包络半径），
    并显式声明它只用于候选预筛、最终方案仍由完整 target 集复核。
    """

    if prefilter is None:
        return None
    #: 挂在既有设施上的动作（复用既有站址）坐标不取自 action，保守起见不做预筛。
    if action.get("facility_id"):
        return None
    state = prefilter.get("state")
    coordinate = action.get("coordinate")
    if not coordinate or len(coordinate) < 2:
        return None
    envelope = _action_provider_envelope_m(action, state)
    if envelope is None:
        return None
    nearest = None
    for _, path in prefilter["paths"]:
        distance = float(nearest_route_position(path, coordinate)["distance_m"])
        nearest = distance if nearest is None else min(nearest, distance)
    if nearest is None:
        return None
    required_clearance = (
        envelope + prefilter["max_half_width_m"] + prefilter["max_cell_half_diagonal_m"]
    )
    if nearest <= required_clearance:
        return None
    return {
        "action_id": action.get("action_id"), "iteration": iteration,
        "status": "ineligible",
        "confirmed_requirement_unit_volume_gain": 0.0,
        "confirmed_gain": 0.0,
        "confirmed_targets": [], "unknown_targets": [], "unknown_regressions": [],
        "newly_met_confirmed_objectives": 0,
        "service_resolved_volume_proxy_m3": 0.0,
        "redundancy_progress_volume_proxy_m3": 0.0,
        "total_continuous_deficit_projection_reduction_m": 0.0,
        "max_continuous_deficit_projection_reduction_m": 0.0,
        "target_progress": [], "regressions": [],
        "evidence_status": "prefiltered_no_corridor_interaction",
        "prefilter": {
            "applied": True,
            "reason": "candidate_provider_envelope_cannot_reach_any_corridor_voxel",
            "nearest_route_distance_m": nearest,
            "required_clearance_m": required_clearance,
            "provider_envelope_m": envelope,
            "corridor_max_half_width_m": prefilter["max_half_width_m"],
            "max_cell_half_diagonal_m": prefilter["max_cell_half_diagonal_m"],
            "semantics": (
                "conservative_prefilter_provably_zero_impact_"
                "final_selection_reconfirmed_on_full_target_set"
            ),
        },
        "reasons": [
            "该候选的新设备几何包络无法触及任何走廊体素（超出 P14 包络剪枝范围）："
            "confirmed 增益必然为 0，因此不执行完整 what-if 重算"
        ],
        "evidence": [],
    }


def _entry_map(assessment):
    """P15 assessment → ``{target_id: entry}``（subsystem + service 两类 target）。

    与 planner 的 residual 判定共用 :func:`corridor_voxel_entry_index`，因此
    service-aware target 的生命周期与 legacy target 完全同构。
    """

    return corridor_voxel_entry_index(assessment)


def _regressions(before, after):
    failures, unknown = [], []
    for key, left in before.items():
        if left.get("combined_status") != "satisfied":
            continue
        right = after.get(key) or {}
        if right.get("combined_status") == "confirmed_gap":
            failures.append(key)
        elif right.get("combined_status") != "satisfied":
            unknown.append(key)
    return failures, unknown


def _summary_map(assessment):
    return {
        f"{route.get('route_id')}|{item.get('subsystem')}": item
        for route in assessment.get("routes") or [] for item in route.get("subsystems") or []
    }


def _continuous_reduction(before, after):
    left, right = _summary_map(before), _summary_map(after)
    total = max_reduction = 0.0
    for key, item in left.items():
        other = right.get(key) or {}
        total += max(0.0, float(item.get("total_confirmed_deficit_projection_m") or 0.0) - float(other.get("total_confirmed_deficit_projection_m") or 0.0))
        max_reduction += max(0.0, float(item.get("max_continuous_deficit_projection_m") or 0.0) - float(other.get("max_continuous_deficit_projection_m") or 0.0))
    return total, max_reduction


def _newly_met_objectives(before, after):
    left, right = _summary_map(before), _summary_map(after)
    count = 0
    for key, item in left.items():
        prior = {value.get("objective"): value for value in item.get("objective_results") or [] if value.get("confirmed") is True}
        later = {value.get("objective"): value for value in (right.get(key) or {}).get("objective_results") or [] if value.get("confirmed") is True}
        count += sum(value.get("status") != "met" and (later.get(name) or {}).get("status") == "met" for name, value in prior.items())
    return count


def _confirmed_objectives_met(assessment):
    confirmed = []
    for item in _summary_map(assessment).values():
        confirmed.extend(value for value in item.get("objective_results") or [] if value.get("confirmed") is True)
    return bool(confirmed) and all(item.get("status") in ("met", "not_applicable") for item in confirmed)


def _objective_evidence_required(assessment):
    result = []
    for route in assessment.get("routes") or []:
        for subsystem in route.get("subsystems") or []:
            for item in subsystem.get("objective_results") or []:
                if item.get("confirmed") is True and item.get("status") == "unknown":
                    result.append({
                        "kind": "planning_objective_evidence",
                        "route_id": route.get("route_id"),
                        "subsystem": subsystem.get("subsystem"),
                        "objective": item.get("objective"),
                        "reason": "confirmed planning objective 缺少可评估证据；不自动触发建站",
                    })
    return result


def _apply_cumulative_action(existing, action):
    """把一条 action 应用于**工作副本**，供 P16 cumulative what-if 使用。

    Round 2.1：hypothetical 设备/站点必须**完整保留物理站址身份**，否则重跑 P14/P15
    后会把"同一座塔上的新设备"误算成第二个 independent physical site：

    * ``service_key``（**设备自身显式声明**的服务身份；canonical 推导值绝不写入，
      否则会凭空把 legacy 设备升级成 surface-aware 服务）；
    * ``distinct_site_id``（canonical 物理站址身份，显式保留）；
    * ``planning_origin`` / ``host``（``distinct_site_id_for`` 的解析输入，按现有契约所需）。

    这里只复制身份字段，**不**复制 action 里的其它无关大 payload（planning_profile /
    vertical / eligibility 等都不写进 hypothetical 设备）。
    """

    collection = deepcopy(existing or {})
    collection.setdefault("items", [])
    if action.get("planner_family") == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
        #: Round C：导航站 action **绝不**写进正式 ExistingCNS / 工作设施集合。
        #: 它是 caller-owned 假想 provider，只经
        #: :func:`...navigation_reference_station_planning_service.hypothetical_navigation_sites`
        #: 作为重算 P14/P15 的显式输入累积。
        return collection
    identity_metadata = {"hypothetical_action_id": action["action_id"], "p16_proposal_only": True}
    host = deepcopy(action.get("host")) if isinstance(action.get("host"), dict) else None
    planning_origin = (
        deepcopy(action.get("planning_origin"))
        if isinstance(action.get("planning_origin"), dict)
        else None
    )
    if host:
        identity_metadata["host"] = host
    installed = {
        "device_id": action["device_id"], "subsystem": action["subsystem"],
        "status": "active", "service_model": {"status": "missing_data"},
        "metadata": identity_metadata,
    }
    if action.get("device_service_key"):
        installed["service_key"] = action["device_service_key"]
    if isinstance(action.get("device_type"), dict) and action["device_type"]:
        installed["type"] = deepcopy(action["device_type"])
    if action.get("distinct_site_id"):
        installed["distinct_site_id"] = action["distinct_site_id"]
    if planning_origin:
        installed["planning_origin"] = planning_origin
    if action.get("facility_id"):
        facility = next(item for item in collection["items"] if str(item.get("facility_id")) == str(action["facility_id"]))
    else:
        proposal_id = f"p16-proposal:{action.get('site_id')}"
        facility = next((item for item in collection["items"] if item.get("facility_id") == proposal_id), None)
        if facility is None:
            facility_metadata = {
                "proposal_only": True, "source_candidate_site_id": action.get("site_id"),
            }
            if host:
                facility_metadata["host"] = deepcopy(host)
            if planning_origin:
                facility_metadata["planning_origin"] = deepcopy(planning_origin)
            facility = {
                "facility_id": proposal_id, "site_id": action.get("site_id"),
                "name": f"P16 Proposal {action.get('site_id')}",
                "coordinate": deepcopy(action.get("coordinate")),
                "vertical_profile": deepcopy(action.get("vertical")),
                "devices": [], "status": "active", "source": "P16 cumulative hypothetical what-if",
                "metadata": facility_metadata,
            }
            if action.get("distinct_site_id"):
                facility["distinct_site_id"] = action["distinct_site_id"]
            collection["items"].append(facility)
    if not any(str(item.get("device_id")) == str(installed["device_id"]) for item in facility.get("devices") or []):
        facility.setdefault("devices", []).append(installed)
    collection["count"] = len(collection["items"])
    return collection
