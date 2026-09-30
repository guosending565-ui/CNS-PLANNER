"""P16 cumulative corridor what-if orchestration (proposal only)."""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.corridor_site_planning import (
    TARGET_SCOPE_SERVICE, corridor_target_id, corridor_voxel_entry_index,
    normalize_corridor_site_planning_policy,
)
from ..domain.site_planning import REUSE_TIERS
from ..domain.surface_classification import (
    surface_class_provider_for, surface_facts_fingerprint_for,
)
from ..domain.cns_service_contract import (
    SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION, SERVICE_KEY_RADAR_NONCOOPERATIVE,
)
from ..domain.cns_service_registry import planner_family_for
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
        family = action.get("planner_family")
        if family == "directional_radar":
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=[*(radar_actions or []), action],
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
            )
        elif family == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=radar_actions,
                navigation_actions=[*(navigation_actions or []), action],
                baseline_navigation_evidence=baseline_navigation_evidence,
            )
        else:
            after_corridor, after_gap = self._rerun(
                _apply_cumulative_action(facilities, action),
                radar_actions=radar_actions,
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
            )
        impact = _impact(action, before_gap, after_gap, targets)
        impact.update({
            "iteration": iteration,
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
        baseline_navigation_evidence=None,
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
        )


def rerun_corridor_chain(
    state, corridor_model_prototype, corridor_gap_prototype, facilities,
    *, radar_service_evidence=None, navigation_service_evidence=None,
):
    """Run the existing P14→P15 chain on caller-owned working data."""
    profile = AircraftCNSProfileCatalog.find(
        state.get("aircraft_profiles") or {}, state.get("selected_aircraft_profile_id") or "",
    )
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
    before_entries, after_entries = _entry_map(before), _entry_map(after)
    gain = service_volume = redundancy_volume = 0.0
    progress, unknown = [], []
    for target in targets:
        key = target["target_id"]
        left, right = before_entries.get(key), after_entries.get(key)
        if not left or not right:
            unknown.append(key)
            continue
        before_units = _units_for_target(target, left)
        after_units = _units_for_target(target, right)
        if before_units is None or after_units is None or right.get("combined_status") == "unknown":
            unknown.append(key)
            continue
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
    regressions, unknown_regressions = _regressions(before_entries, after_entries)
    total_reduction, max_reduction = _continuous_reduction(before, after)
    newly_met = _newly_met_objectives(before, after)
    if regressions:
        status, reasons = "ineligible", ["候选 what-if 使原 satisfied voxel 变为 confirmed gap"]
    elif unknown_regressions or unknown:
        status, reasons = "unknown", ["what-if 产生 unknown 或关键 provider 证据不完整"]
        gain = service_volume = redundancy_volume = 0.0
    elif gain > 0:
        status, reasons = "eligible", []
    else:
        status, reasons = "ineligible", ["候选未产生 confirmed requirement-unit progress"]
    return {
        "action_id": action.get("action_id"), "status": status,
        "confirmed_requirement_unit_volume_gain": gain,
        "service_resolved_volume_proxy_m3": service_volume,
        "redundancy_progress_volume_proxy_m3": redundancy_volume,
        "newly_met_confirmed_objectives": newly_met,
        "total_continuous_deficit_projection_reduction_m": total_reduction,
        "max_continuous_deficit_projection_reduction_m": max_reduction,
        "target_progress": progress, "regressions": regressions,
        "unknown_regressions": unknown_regressions,
        "unknown_targets": unknown, "reasons": reasons,
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
