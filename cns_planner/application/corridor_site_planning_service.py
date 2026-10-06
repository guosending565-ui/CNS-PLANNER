"""P16 cumulative corridor what-if orchestration (proposal only)."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import time

from ..algorithms.corridor.v1 import nearest_route_position
from ..domain.corridor_site_planning import (
    TARGET_SCOPE_ENDPOINT, TARGET_SCOPE_SERVICE, corridor_target_id,
    corridor_voxel_entry_index, endpoint_state_entry_index, endpoint_target_id,
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
from ..domain.navigation_integrity_monitoring import (
    PLANNER_FAMILY as ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY,
    endpoint_gap_evidence, hypothetical_endpoint_monitor_evidence,
    navigation_integrity_monitor_actions,
)
from ..domain.radar_service_evidence import (
    #: Round 29-K：canonical P16 **不**再使用 ``radar_candidate_actions``（Radar 候选
    #: 面板的枚举与优化只属于上游 ``radar_surveillance_layout``）；这里只保留 Radar
    #: **证据**适配（P14 的什么-if 与正式链共用同一份实现）。
    build_radar_service_evidence,
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
    #: Round 2.7：设备目录同理 —— 设备侧的工程规划假设（``scope=device``）必须
    #: 在 P16 的每一个 provider 判定消费点都生效，否则"同一份输入两种判定"会再现。
    _requirement_blocks, _subsystem_name, aircraft_profile_with_evidence,
    device_catalog_with_evidence,
)
from .site_candidate_actions import candidate_actions
from .corridor_service import apply_algorithm_semantics_stale
from .result_currentness import projected_result
from .production_write_authority import assert_write_authority


class CorridorSitePlanningService:
    def __init__(self, session, planner, corridor_model, corridor_gap_analyzer,
                 invalidation, snapshot):
        self.session, self.planner = session, planner
        self.corridor_model, self.corridor_gap_analyzer = corridor_model, corridor_gap_analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        """只读投影：算法语义版本变化时如实标注 stale（绝不误判 current）。"""

        result = self.session.state.get("cns_corridor_site_plan") or self.planner.empty()
        return apply_algorithm_semantics_stale(
            result, self.planner.algorithm_id, self.planner.algorithm_version,
        )

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
        #: Round 29-J：P16 的 P14/P15 前置门禁必须消费**有效** currentness（唯一权威
        #: ``projected_result``），绝不直接读 raw 容器 status —— 否则旧算法语义版本的
        #: P14/P15（raw status 仍可能是 passed/failed）会被误当作 current，P16 就会
        #: 基于旧结论产出新提案。stored payload 原样保留，这里只做只读投影。
        baseline_corridor = projected_result(state, "cns_corridor_assessment") or {}
        baseline_gap = projected_result(state, "cns_corridor_gap_assessment") or {}
        if baseline_corridor.get("status") in (None, "stale", "not_calculated", "missing_data"):
            return self._save_missing("P14 cns_corridor_assessment 必须是 current")
        if baseline_gap.get("status") in (None, "stale", "not_calculated", "missing_data"):
            return self._save_missing("P15 cns_corridor_gap_assessment 必须是 current")
        targets, unknown = _targets(baseline_gap)
        unknown.extend(_objective_evidence_required(baseline_gap))
        #: Round 29-K：Radar 的规划 authority 是 ``radar_surveillance_layout``；
        #: canonical P16 **不再**枚举 Radar candidate panels（见
        #: :meth:`_canonical_candidate_actions`）。Radar 的缺口在这里按上游 layout 的
        #: canonical verdict 只读分类：proven managed gap / 证据不足（evidence_required）。
        (
            terminal_managed_gaps, radar_authority_unknowns, actionable_targets,
        ) = _radar_gap_authority(targets, state)
        unknown.extend(radar_authority_unknowns)
        actions, baseline_navigation_evidence = self._canonical_candidate_actions(
            state, targets, baseline_gap,
        )
        if baseline_navigation_evidence is not None:
            unknown.extend(navigation_evidence_required(baseline_navigation_evidence))
        policy = state.get("corridor_site_planning_policy") or normalize_corridor_site_planning_policy()
        #: Round 2.2 性能：同一份 grid / terrain / surface facts 下的走廊单元只准备一次，
        #: 并在所有 what-if 之间复用（实测每轮重复准备 8008 个单元约 6.4 s）。
        #: 另对"几何上不可能触及任何走廊体素"的候选做**保守预筛**：这类动作重算前后
        #: 逐字段相同（P14 的 envelope 剪枝保证超出包络的 provider 连 unknown 都不产生），
        #: 因此预筛只否决 confirmed 增益必然为 0 的动作，绝不改变任何判定规则。
        prepared_cells = self._prepared_cells(state)
        prefilter = _build_prefilter_context(state, prepared_cells)
        #: Round 2.8：**连续服务目标**的阈值与速度必须与 P17 同源（用户显式登记的
        #: ``c_full_outage_max_s`` × 选定机载档案航路速度）。解析不出就保持不可判定，
        #: 绝不回落到 3 s 之类的默认值。
        continuous_inputs = self._continuous_inputs(state)
        continuous_threshold_m = continuous_inputs.get("threshold_m")
        continuous_route_speed_mps = continuous_inputs.get("route_speed_mps")
        continuous_aircraft_id = continuous_inputs.get("aircraft_id")
        selection_limit = _intervention_selection_limit(policy)
        #: Round 30-C3B（收口）：**只读**停止诊断。它把"为什么停"拆成三类互斥结论
        #: （达上限且确实还有正向候选 / 候选池自然耗尽 / 全部服务已可接受），并如实说明
        #: "达上限时究竟探测评估了多少候选"，避免把 ``intervention_selection_limit_reached``
        #: 误读成"已经没有可用候选"，也避免把 ``no_positive_*`` 误读成"只是撞到了 cap"。
        #: 它不参与任何判定，也不改变候选排序 / 选中序列 / cap 默认值 / P15 / P17 语义。
        stop_diagnostics = _new_stop_diagnostics(selection_limit)
        selected, trace, all_impacts = [], [], []
        continuous_impacts = []
        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        current_corridor, current_gap = deepcopy(baseline_corridor), deepcopy(baseline_gap)
        #: Round 29-G：endpoint 服务的 caller-owned 假想证据链。它**不**进入
        #: facilities，也不触发任何走廊重算；只重算 endpoint 证据本身。
        baseline_endpoint_evidence = deepcopy(
            (baseline_corridor or {}).get("endpoint_service_evidence")
        )
        current_endpoint_evidence = deepcopy(baseline_endpoint_evidence)
        selected_ids = set()
        selected_radar_actions = []
        selected_navigation_actions = []
        selected_endpoint_actions = []
        stop_reason = None
        performance = _new_performance_profile(actions)
        #: Round 30-C3B（收口）：达选择上限时只读 existence probe 的**逐 tier 复用缓存**。
        #: 探测到正向候选时直接早停（不生成完整 candidate_impacts）；探测不到时把这份
        #: 已算出的 impacts 交给旧完整评估路径复用，绝不重复计算，也绝不改变旧语义。
        limit_probe_impacts = None
        limit_probe_exhausted = False

        def apply_winner(action, impact, score, cost, cost_unit, score_semantics, tier):
            """把一条中选动作应用到工作副本，并登记 selected / trace（唯一实现）。"""

            nonlocal facilities, current_corridor, current_gap, current_endpoint_evidence
            if action.get("planner_family") == "directional_radar":
                selected_radar_actions.append(deepcopy(action))
            elif action.get("planner_family") == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
                # 导航站是 caller-owned 假想 provider：**不**写入正式 ExistingCNS，
                # 也不并入 facilities，只作为重算 P14/P15 的显式输入累积。
                selected_navigation_actions.append(deepcopy(action))
            elif action.get("planner_family") == ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY:
                #: Round 29-G：endpoint 完整性监测动作**只**更新 endpoint 假想证据。
                #: 它不影响 corridor C/RID/Radar 几何，因此**绝不**重跑 P14/P15。
                selected_endpoint_actions.append(deepcopy(action))
                current_endpoint_evidence = hypothetical_endpoint_monitor_evidence(
                    current_endpoint_evidence, [action],
                )
                current_gap = _project_endpoint_gap(current_gap, current_endpoint_evidence)
                performance["endpoint_fast_path_apply_count"] += 1
            else:
                facilities = _apply_cumulative_action(facilities, action)
            if action.get("planner_family") != ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY:
                current_corridor, current_gap = self._rerun(
                    facilities, radar_actions=selected_radar_actions,
                    navigation_actions=selected_navigation_actions,
                    baseline_navigation_evidence=baseline_navigation_evidence,
                    prepared_cells=prepared_cells, performance=performance,
                )
                current_gap = _project_endpoint_gap(current_gap, current_endpoint_evidence)
            selected_ids.add(action["action_id"])
            chosen = {
                **deepcopy(action), "impact": deepcopy(impact),
                "iteration": len(selected) + 1,
                "marginal_confirmed_requirement_unit_volume_gain": impact["confirmed_requirement_unit_volume_gain"],
                "marginal_continuous_service_gain": deepcopy(impact.get("continuous_service_gain")),
                "selection_score": score,
                "score_semantics": score_semantics,
                "explicit_cost": cost, "cost_unit": cost_unit,
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

        for tier in REUSE_TIERS:
            while True:
                #: 停止条件（Round 2.8 与 P17 一致化）：
                #: 离散 objective 满足 **且** 连续服务投影达到可接受（或阈值证据不足，
                #: 此时不得声称"已满足连续服务"，只能如实继续/报告）。
                if _confirmed_objectives_met(current_gap) and (
                    _continuous_service_acceptance(
                        current_gap, continuous_threshold_m,
                        continuous_route_speed_mps, continuous_aircraft_id,
                    ).get("acceptable") is not False
                ):
                    stop_reason = "all_required_services_acceptable"
                    break
                #: Round 30-C3B（性能 + 披露，收口后）：达到干预上限时**先**做一次**只读**
                #: existence probe，而不是为"已经注定不会被采用"的候选做整轮 what-if
                #: （真实项目 742 个候选 ≈ 4.5 分钟），也不是无条件宣布"达上限"。
                #:
                #: 语义边界（本轮裁决）：``intervention_selection_limit_reached`` 只在 probe
                #: **真的找到**一个正向候选时成立；若剩余候选全部探测完仍无正向候选，则
                #: **不**给出"达上限"结论，而是回到旧路径（完整评估 → 自然耗尽 →
                #: ``_no_positive_candidate_stop_reason(...)``），旧 ``stop_reason`` 词表语义
                #: 逐字保持。候选池已空（无 pending）时同样保持原路径。
                if len(selected) >= selection_limit and not limit_probe_exhausted:
                    pending = _pending_candidate_profile(actions, selected_ids)
                    if pending["total"] > 0:
                        probe = self._probe_positive_remaining_candidates(
                            actions, selected_ids, tier, facilities, current_gap, targets,
                            len(selected) + 1, selected_radar_actions,
                            selected_navigation_actions, baseline_navigation_evidence,
                            prepared_cells, prefilter, continuous_threshold_m,
                            continuous_route_speed_mps, continuous_aircraft_id,
                            current_endpoint_evidence, selected_endpoint_actions,
                        )
                        if probe["found"]:
                            stop_reason = "intervention_selection_limit_reached"
                            stop_diagnostics.update({
                                "selection_limit_reached": True,
                                "full_candidate_evaluation_skipped_at_limit": True,
                                "limit_positive_probe_performed": True,
                                "limit_positive_probe_evaluated_count": probe["evaluated"],
                                "limit_positive_probe_found": True,
                                "limit_positive_probe_seconds": probe["seconds"],
                                "pending_candidate_count": pending["total"],
                                "pending_candidates_by_tier": pending["by_tier"],
                                "pending_candidates_by_service": pending["by_service"],
                            })
                            break
                        #: 探测不到任何正向候选 ⇒ **不**下"达上限"结论：如实登记 probe 事实，
                        #: 并把已算出的 impacts 交给下面的完整评估复用（同一 facilities /
                        #: 投影态 / iteration，因此不会重复计算），随后由旧路径给出
                        #: ``no_positive_*``。后续 tier 不再重复探测（已全量探测过）。
                        stop_diagnostics.update({
                            "limit_positive_probe_performed": True,
                            "limit_positive_probe_evaluated_count": probe["evaluated"],
                            "limit_positive_probe_found": False,
                            "limit_positive_probe_seconds": probe["seconds"],
                        })
                        limit_probe_impacts = probe["impacts"]
                        limit_probe_exhausted = True
                tier_actions = [
                    action for action in actions
                    if action.get("reuse_class") == tier and action.get("action_id") not in selected_ids
                ]
                probed = (limit_probe_impacts or {}).get(tier) or {}
                evaluated = []
                for action in tier_actions:
                    impact = probed.get(str(action.get("action_id")))
                    if impact is None:
                        impact, _, _ = self._what_if(
                            action, facilities, current_gap, targets,
                            iteration=len(selected) + 1,
                            radar_actions=selected_radar_actions,
                            navigation_actions=selected_navigation_actions,
                            baseline_navigation_evidence=baseline_navigation_evidence,
                            prepared_cells=prepared_cells, prefilter=prefilter,
                            continuous_threshold_m=continuous_threshold_m,
                            route_speed_mps=continuous_route_speed_mps,
                            aircraft_id=continuous_aircraft_id,
                            endpoint_evidence=current_endpoint_evidence,
                            endpoint_actions=selected_endpoint_actions,
                            performance=performance,
                        )
                    evaluated.append(impact)
                    all_impacts.append(deepcopy(impact))
                ranked = self.planner.rank(tier_actions, evaluated)
                #: Round 2.8：连续服务排名通道。只在"离散排名拿不到任何正向候选、
                #: 且当前投影态连续服务**明确不合格**"时启用，因此绝不改变既有选中序列。
                #:
                #: 性能与语义边界（Round 2.8 实测收口）：该通道**只**接受"使连续服务从
                #: 不可接受变为可接受"（``threshold_crossed``）的候选。仅把缺口缩短、
                #: 却仍超过阈值并不构成停止理由，因此不进入候选池 —— 否则在真实项目
                #: （750 个候选 / 2745 个目标）里会为大量"只缩短一部分"的塔反复执行完整
                #: P14/P15 重算，并把 P16 选站数推到干预上限（实测单次评估劣化到数十分钟）。
                #: 只做部分缩短的候选仍照常登记在 ``continuous_service_candidate_impacts``
                #: 与各候选的 impact 里，可审计、不回退。
                rankable_continuous = (
                    [item for item in evaluated if _continuous_rankable(item)]
                    if (not ranked and _continuous_service_unacceptable(
                        current_gap, continuous_threshold_m,
                        continuous_route_speed_mps, continuous_aircraft_id))
                    else []
                )
                continuous_ranked = (
                    self.planner.rank_continuous_service(tier_actions, rankable_continuous)
                    if rankable_continuous else []
                )
                if not ranked and not continuous_ranked:
                    continuous_impacts.extend(
                        deepcopy(item) for item in evaluated
                        if (item or {}).get("status") == "eligible"
                    )
                    #: 本层（reuse tier）已无可推进的候选：如实登记本轮的正向候选计数，
                    #: 供"所有层都耗尽"这一自然停止结论留下证据。
                    _record_iteration_positive(stop_diagnostics, evaluated, tier_actions)
                    break
                _record_iteration_positive(stop_diagnostics, evaluated, tier_actions)
                if len(selected) >= selection_limit:
                    #: 兜底（与 C3B 之前逐字一致）：正常路径上 probe 已在上方短路；
                    #: 只有"probe 判据与正式排名判据不一致"时才会走到这里。它同时是
                    #: "绝不越过 cap 再选下一个动作"的硬保证：先停，再披露。
                    pending = _pending_candidate_profile(actions, selected_ids)
                    stop_reason = "intervention_selection_limit_reached"
                    stop_diagnostics.update({
                        "selection_limit_reached": True,
                        "pending_candidate_count": pending["total"],
                        "pending_candidates_by_tier": pending["by_tier"],
                        "pending_candidates_by_service": pending["by_service"],
                    })
                    break
                if not ranked:
                    winner = continuous_ranked[0]
                else:
                    winner = ranked[0]
                apply_winner(
                    winner["action"], winner["impact"], winner["selection_score"],
                    winner["explicit_cost"], winner["cost_unit"],
                    winner["score_semantics"], tier,
                )
            if stop_reason:
                break
        if stop_reason is None:
            #: 所有 reuse tier 都已没有正向候选 ⇒ 这才是"候选池耗尽"的自然停止；
            #: 它与"达上限早停"互斥；两者都不成立时即为"全部服务已可接受"。
            stop_diagnostics["candidate_pool_exhausted"] = True
        final_facilities = deepcopy(state.get("existing_cns_facilities") or {})
        final_radar_actions = []
        final_navigation_actions = []
        final_endpoint_actions = []
        for action in selected:
            family = action.get("planner_family")
            if family == "directional_radar":
                final_radar_actions.append(deepcopy(action))
            elif family == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
                final_navigation_actions.append(deepcopy(action))
            elif family == ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY:
                #: endpoint 动作**不**进入 facilities，也**不**触发走廊重算。
                final_endpoint_actions.append(deepcopy(action))
            else:
                final_facilities = _apply_cumulative_action(final_facilities, action)
        corridor_selected = bool(
            final_radar_actions or final_navigation_actions
            or any(
                action.get("planner_family") not in (
                    "directional_radar", NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY,
                    ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY,
                ) for action in selected
            )
        )
        if corridor_selected:
            final_corridor, final_gap = self._rerun(
                final_facilities, radar_actions=final_radar_actions,
                navigation_actions=final_navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells, performance=performance,
            )
        else:
            final_corridor, final_gap = deepcopy(baseline_corridor), deepcopy(baseline_gap)
        final_endpoint_evidence = (
            hypothetical_endpoint_monitor_evidence(
                baseline_endpoint_evidence, final_endpoint_actions)
            if final_endpoint_actions else current_endpoint_evidence
        )
        final_gap = _project_endpoint_gap(final_gap, final_endpoint_evidence)
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
        final_impact = _impact(
            {"action_id": "final-combined"}, baseline_gap, final_gap, targets,
            continuous_threshold_m=continuous_threshold_m,
            route_speed_mps=continuous_route_speed_mps,
            aircraft_id=continuous_aircraft_id,
        )
        cumulative_gain = result.get("confirmed_requirement_unit_volume_gain", 0.0)
        authoritative_gain = final_impact.get("confirmed_requirement_unit_volume_gain", 0.0)
        baseline_acceptance = _continuous_service_acceptance(
            baseline_gap, continuous_threshold_m, continuous_route_speed_mps,
            continuous_aircraft_id,
        )
        final_acceptance = _continuous_service_acceptance(
            final_gap, continuous_threshold_m, continuous_route_speed_mps,
            continuous_aircraft_id,
        )
        stop_reason = stop_reason or _no_positive_candidate_stop_reason(
            final_acceptance, result.get("status"),
            threshold_available=continuous_threshold_m is not None,
        )
        result.update({
            "baseline_corridor_fingerprint": baseline_corridor.get("input_fingerprint"),
            "baseline_corridor_gap_fingerprint": baseline_gap.get("input_fingerprint"),
            #: Round 30-C3B（收口）：停止诊断（只读披露）。三态互斥：
            #: ``selection_limit_reached`` + ``full_candidate_evaluation_skipped_at_limit``
            #: （达工程上限，且只读 probe 真的找到了正向候选 ⇒ 未排名不等于"没有可用候选"）、
            #: ``candidate_pool_exhausted``（所有 reuse tier 都没有正向候选，旧
            #: ``no_positive_*`` 词表语义保持）、两者皆否 ⇒ ``all_required_services_acceptable``。
            "stop_diagnostics": _finalize_stop_diagnostics(stop_diagnostics, stop_reason),
            "final_hypothetical_corridor_fingerprint": final_corridor.get("input_fingerprint"),
            "final_hypothetical_corridor_gap_fingerprint": final_gap.get("input_fingerprint"),
            "stop_reason": stop_reason,
            #: Round 2.8：连续服务停止条件与本轮基线/最终投影态的可读摘要。
            "continuous_service_stop_policy": (
                "continuous_service_objective_uses_the_same_threshold_and_the_same_"
                "projection_metric_as_p17"
            ),
            "continuous_service_threshold_m": continuous_threshold_m,
            "continuous_service_route_speed_mps": continuous_route_speed_mps,
            "baseline_continuous_service_acceptance": baseline_acceptance,
            "final_continuous_service_acceptance": final_acceptance,
            "continuous_service_acceptable": final_acceptance.get("acceptable"),
            "continuous_service_residual_segments": _continuous_residual_segments(
                final_gap, continuous_threshold_m,
            ),
            "continuous_service_candidate_impacts": continuous_impacts,
            "intervention_selection_limit": selection_limit,
            "cumulative_predicted_requirement_unit_volume_gain": cumulative_gain,
            "confirmed_requirement_unit_volume_gain": authoritative_gain,
            "combined_what_if_gain_difference": authoritative_gain - cumulative_gain,
            "final_combined_impact": final_impact,
            "final_hypothetical_evidence": {
                "corridor": deepcopy(final_corridor), "corridor_gap": deepcopy(final_gap),
                "persisted_as_upstream": False,
            },
        })
        #: Round 29-K：Radar 的规划 authority 在上游 ``radar_surveillance_layout``。
        #: P16 **只读**披露其结论：proven managed gap 是**事实缺口**，绝不被改写成
        #: satisfied，也绝不进入 P16 的 action search（``p16_actionable=false``）。
        result["terminal_managed_gaps"] = deepcopy(terminal_managed_gaps)
        result["terminal_managed_gap_count"] = len(terminal_managed_gaps)
        result["p16_actionable_target_count"] = len(actionable_targets)
        result["p16_actionable_target_ids"] = sorted(
            str(item.get("target_id") or "") for item in actionable_targets
        )
        result["radar_planning_authority"] = {
            "service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE,
            "planning_owner": RADAR_PLANNING_OWNER,
            "canonical_p16_candidate_family": False,
            "p16_reads_upstream_selected_panels": True,
            "semantics": (
                "radar_candidate_panels_are_enumerated_and_optimized_only_by_"
                "radar_surveillance_layout_p16_never_reruns_that_optimization"
            ),
        }
        result["origin_tier_statistics"] = _origin_tier_statistics(actions, selected)
        #: Round 29-G：P16 第一阶段的**性能画像**（只读计数与耗时，不参与任何判定）。
        result["performance_profile"] = _finalize_performance_profile(performance, selected)
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
        if not selected and _confirmed_objectives_met(baseline_gap) and (
            baseline_acceptance.get("acceptable") is not False
        ):
            result["status"] = "no_action_required"
            result["stop_reason"] = "confirmed_objectives_already_met"
        elif not selected and not actionable_targets and not unknown:
            #: Round 29-K：除「上游规划权威已证明不可行的 managed Radar 缺口」之外
            #: 没有任何可行动目标 ⇒ 这不是「P16 找不到方案」，而是「该缺口不属于
            #: P16 的规划权限、也不再重评」。factual gap 仍原样保留给
            #: P17 / Step6 / report。
            result["status"] = "no_action_required"
            result["stop_reason"] = (
                "only_terminal_managed_gaps_outside_p16_authority"
                if terminal_managed_gaps else "no_confirmed_targets_or_unknown_evidence"
            )
        elif not selected and not actionable_targets and unknown:
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

    def preflight_first_reuse_iteration(self, *, candidate_limit=None, tier_index=0,
                                        family_filter=None, measure_candidates=False):
        """**只读 FULL 诊断入口**：canonical P16 候选 / 保守预筛 / 第一轮重算规模。

        与正式 ``evaluate`` 的候选构造（``_canonical_candidate_actions``）和
        ``_what_if`` **完全同源**，但：

        * 不 ``select``、不 ``apply``、不写 ``cns_corridor_site_plan``、
          不写 ``result_statuses``、**不 save**（ProjectState 逐字节不变）；
        * 只统计 ``REUSE_TIERS[tier_index]`` 的**第一轮**候选规模：不进入该 tier
          的第二轮，也不进入后续 tier；
        * ``measure_candidates=True`` 时才真正执行候选的 ``_what_if``（完整
          P14→P15 重算）并给出逐候选耗时；默认只做只读统计，因此默认调用是毫秒级。

        Round 29-K 起 local impact 引擎已从 production 撤销，因此本入口**只有**
        FULL 一条路径：它给出的候选数量与耗时就是正式 P16 的真实值。
        """

        started = time.perf_counter()
        state = self.session.state
        baseline_corridor = projected_result(state, "cns_corridor_assessment") or {}
        baseline_gap = projected_result(state, "cns_corridor_gap_assessment") or {}
        if baseline_corridor.get("status") in (None, "stale", "not_calculated", "missing_data"):
            raise ValueError("P14 cns_corridor_assessment 必须是 current")
        if baseline_gap.get("status") in (None, "stale", "not_calculated", "missing_data"):
            raise ValueError("P15 cns_corridor_gap_assessment 必须是 current")

        targets, unknown = _targets(baseline_gap)
        unknown.extend(_objective_evidence_required(baseline_gap))
        terminal_managed_gaps, radar_unknowns, actionable_targets = _radar_gap_authority(
            targets, state,
        )
        unknown.extend(radar_unknowns)
        actions, baseline_navigation_evidence = self._canonical_candidate_actions(
            state, targets, baseline_gap,
        )
        prepared_cells = self._prepared_cells(state)
        prefilter = _build_prefilter_context(state, prepared_cells)
        continuous_inputs = self._continuous_inputs(state)
        preparation_seconds = time.perf_counter() - started

        def statistics(members):
            eligible = [
                item for item in members
                if (item.get("eligibility") or {}).get("status") == "eligible"
            ]
            filtered = [
                item for item in eligible if _prefilter_impact(item, 1, prefilter) is not None
            ]
            return {
                "candidate_count": len(members),
                "eligible_count": len(eligible),
                "prefiltered_count": len(filtered),
                "full_rerun_count": len(eligible) - len(filtered),
                "planner_family_counts": dict(sorted(Counter(
                    _action_planner_family(item) for item in members
                ).items())),
                "service_key_counts": dict(sorted(Counter(
                    str(item.get("service_key") or "") for item in members
                ).items())),
            }

        tier_statistics = []
        for name in REUSE_TIERS:
            entry = {"tier": name}
            entry.update(statistics([item for item in actions if item.get("reuse_class") == name]))
            tier_statistics.append(entry)

        tier = REUSE_TIERS[int(tier_index)]
        tier_actions = [item for item in actions if item.get("reuse_class") == tier]
        tier_stat = statistics(tier_actions)
        evaluated_actions = [
            item for item in tier_actions
            if (item.get("eligibility") or {}).get("status") == "eligible"
            and _prefilter_impact(item, 1, prefilter) is None
        ]
        if family_filter:
            evaluated_actions = [
                item for item in evaluated_actions
                if _action_planner_family(item) == str(family_filter)
            ]
        measured_pool = evaluated_actions
        if candidate_limit:
            measured_pool = evaluated_actions[: int(candidate_limit)]

        per_candidate = []
        measured = []
        if measure_candidates and measured_pool:
            performance = _new_performance_profile(actions)
            for action in measured_pool:
                candidate_started = time.perf_counter()
                impact, _corridor, _gap = self._what_if(
                    action, deepcopy(state.get("existing_cns_facilities") or {}),
                    baseline_gap, targets, iteration=1,
                    radar_actions=[], navigation_actions=[],
                    baseline_navigation_evidence=baseline_navigation_evidence,
                    prepared_cells=prepared_cells, prefilter=prefilter,
                    continuous_threshold_m=continuous_inputs.get("threshold_m"),
                    route_speed_mps=continuous_inputs.get("route_speed_mps"),
                    aircraft_id=continuous_inputs.get("aircraft_id"),
                    endpoint_evidence=deepcopy(
                        baseline_corridor.get("endpoint_service_evidence")),
                    endpoint_actions=[], performance=performance,
                )
                per_candidate.append(time.perf_counter() - candidate_started)
                measured.append({
                    "action_id": action.get("action_id"),
                    "service_key": action.get("service_key"),
                    "planner_family": _action_planner_family(action),
                    "impact_status": (impact or {}).get("status"),
                })
        ordered = sorted(per_candidate)
        all_tiers_shape = _candidate_shape_statistics(actions)
        tier_shape = _candidate_shape_statistics(tier_actions)
        return {
            "reuse_tier": tier, "tier_index": int(tier_index),
            "tier_statistics": tier_statistics,
            "candidate_total": len(actions),
            "tier_candidate_total": len(tier_actions),
            "tier_eligible_total": tier_stat["eligible_count"],
            "prefiltered_count": tier_stat["prefiltered_count"],
            "full_rerun_candidate_count": len(evaluated_actions),
            "measured_candidate_count": len(measured),
            "measured_candidates": measured,
            #: **all_tiers（跨全部 reuse tier）** 的 family 维度计数。
            "candidate_family_counts": all_tiers_shape["all_tiers_planner_family_counts"],
            #: **all_tiers** 的 reuse tier 维度计数（与上面的 family 计数口径不同）。
            "candidate_reuse_class_counts": all_tiers_shape["all_tiers_reuse_class_counts"],
            "candidate_family_by_reuse_class": (
                all_tiers_shape["all_tiers_family_by_reuse_class"]
            ),
            #: 两个维度各自**完整**的只读统计块（含语义标注，供审计直接引用）。
            "candidate_shape_all_tiers": all_tiers_shape,
            "tier_candidate_shape": {**tier_shape, "tier": tier},
            "candidate_counting_semantics": (
                "all_tiers_planner_family_counts_counts_every_reuse_tier_"
                "tier_candidate_shape_counts_only_the_selected_reuse_tier_"
                "omnidirectional_site_is_not_tower_colocation_host"
            ),
            "candidate_service_key_counts": dict(sorted(Counter(
                str(item.get("service_key") or "") for item in actions
            ).items())),
            "radar_canonical_p16_candidate_count": len([
                item for item in actions
                if item.get("service_key") == SERVICE_KEY_RADAR_NONCOOPERATIVE
            ]),
            "radar_terminal_managed_gap_count": len(terminal_managed_gaps),
            "terminal_managed_gaps": deepcopy(terminal_managed_gaps),
            "p16_actionable_target_count": len(actionable_targets),
            "target_total": len(targets),
            "unknown_evidence_total": len(unknown),
            "local_impact_engine_present": False,
            "preparation_seconds": preparation_seconds,
            "per_candidate_seconds": [round(value, 3) for value in per_candidate],
            "per_candidate_median_seconds": (
                round(ordered[len(ordered) // 2], 3) if ordered else None),
            "per_candidate_p95_seconds": (
                round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 3)
                if ordered else None),
            "measured_wall_seconds": sum(per_candidate),
            "semantics": (
                "read_only_canonical_p16_candidate_preflight_full_rerun_only_"
                "never_selects_never_writes_never_saves"
            ),
        }

    def _save_missing(self, reason):
        assert_write_authority(self, "cns_corridor_site_plan")
        self.invalidation.cns_plan_review("p16_became_missing")
        result = self.planner.empty("missing_data")
        result["reasons"] = [reason]
        self.session.state["cns_corridor_site_plan"] = result
        self.session.state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = "missing_data"
        self.session.save()
        return self.snapshot()

    def _continuous_inputs(self, state):
        """P16 连续服务停止条件所需的阈值 / 速度（与 P17 **同源**，只读）。

        Round 2.8：P16 必须能回答"实施本方案后连续服务是否可接受"。它**不允许**
        自己另立一份阈值或速度，因此这里直接复用 P17 的评估器与参数解析：

        * ``c_full_outage_max_s`` —— ``ContinuousServiceAcceptabilityV1._parameters``
          经 ``resolve_continuous_parameter``（显式证据优先，``C`` 无内置基线）；
        * 航路速度 —— ``fc30_planning_speeds(aircraft_profile_with_evidence(state))``，
          与 P17 把缺口长度换算成时长用的是**同一个**事实。

        任何一项不可判定时返回 ``threshold_m=None``，P16 会如实披露
        ``threshold_evidence_required``，**绝不**假装连续服务已满足。
        """

        from ..algorithms.continuous_service.v1 import (
            ContinuousServiceAcceptabilityV1, _limit as _resolve_limit,
        )
        from ..domain.fc30_profile import fc30_planning_speeds

        profile = aircraft_profile_with_evidence(state)
        policy = state.get("cns_continuous_service_policy") or {}
        parameters = ContinuousServiceAcceptabilityV1._parameters(
            state.get("planning_evidence") or {}, policy,
        )
        seconds = _resolve_limit(parameters, "C", "service_outage", policy)
        speeds = fc30_planning_speeds(profile)
        speed = speeds.get("route_speed_mps")
        speed_value = float(speed) if isinstance(speed, (int, float)) and float(speed) > 0 else None
        threshold_m = (
            None if (seconds is None or speed_value is None or float(seconds) <= 0)
            else float(seconds) * speed_value
        )
        return {
            "threshold_m": threshold_m,
            "threshold_s": None if seconds is None else float(seconds),
            "route_speed_mps": speed_value,
            "aircraft_id": (profile or {}).get("aircraft_id"),
            "threshold_parameter": "c_full_outage_max_s",
            "threshold_authority": (
                (parameters.get("c_full_outage_max_s") or {}).get("authority")
            ),
        }

    def _what_if(
        self, action, facilities, before_gap, targets, iteration, radar_actions=None,
        navigation_actions=None, baseline_navigation_evidence=None,
        prepared_cells=None, prefilter=None, continuous_threshold_m=None,
        route_speed_mps=None, aircraft_id=None, endpoint_evidence=None,
        endpoint_actions=None, performance=None,
    ):
        eligibility = action.get("eligibility") or {}
        if eligibility.get("status") != "eligible":
            return ({
                "action_id": action.get("action_id"), "iteration": iteration,
                "status": eligibility.get("status", "ineligible"),
                "confirmed_requirement_unit_volume_gain": 0.0,
                "confirmed_gain": 0.0,
                "continuous_service_gain": None,
                "continuous_service_gain_threshold_crossed": False,
                "continuous_service_gain_improved": False,
                "newly_met_confirmed_objectives": 0,
                "reasons": deepcopy(eligibility.get("reasons") or []),
                "evidence": [], "regressions": [],
            }, None, None)
        family = action.get("planner_family")
        if family == ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY:
            #: Round 29-G：endpoint 完整性监测的**专用 what-if**。
            #: 只重算 caller-owned 假想 endpoint provider 证据 → endpoint_gap_evidence，
            #: **绝不**重跑完整 P14/P15 走廊链：该动作不影响 corridor C/RID/Radar 几何。
            if performance is not None:
                performance["endpoint_fast_path_count"] += 1
            started = time.perf_counter()
            after_endpoint_evidence = hypothetical_endpoint_monitor_evidence(
                endpoint_evidence, [*(endpoint_actions or []), action],
            )
            after_gap = _project_endpoint_gap(before_gap, after_endpoint_evidence)
            if performance is not None:
                performance["endpoint_fast_path_seconds"] += time.perf_counter() - started
            impact = _impact(
                action, before_gap, after_gap, targets,
                continuous_threshold_m=continuous_threshold_m,
                route_speed_mps=route_speed_mps, aircraft_id=aircraft_id,
            )
            impact.update(_endpoint_what_if_evidence(action, after_endpoint_evidence))
            impact["iteration"] = iteration
            impact["provider_reason_profile"] = provider_reason_profile(None)
            return impact, None, after_gap
        prefiltered = _prefilter_impact(action, iteration, prefilter)
        if prefiltered is not None:
            return (prefiltered, None, None)
        if family == "directional_radar":
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=[*(radar_actions or []), action],
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells, performance=performance,
            )
        elif family == NAVIGATION_REFERENCE_STATION_PLANNER_FAMILY:
            after_corridor, after_gap = self._rerun(
                facilities, radar_actions=radar_actions,
                navigation_actions=[*(navigation_actions or []), action],
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells, performance=performance,
            )
        else:
            after_corridor, after_gap = self._rerun(
                _apply_cumulative_action(facilities, action),
                radar_actions=radar_actions,
                navigation_actions=navigation_actions,
                baseline_navigation_evidence=baseline_navigation_evidence,
                prepared_cells=prepared_cells, performance=performance,
            )
        #: 走廊链重跑不会携带 caller-owned 的 endpoint 假想证据（P14 只从 facilities
        #: 构造 endpoint evidence），因此这里**必须**把当前 endpoint 投影态重新叠加回去，
        #: 否则已被 endpoint 动作解决的缺口会在走廊候选的 what-if 里"复活"。
        after_gap = _project_endpoint_gap(after_gap, endpoint_evidence)
        impact = _impact(
            action, before_gap, after_gap, targets,
            continuous_threshold_m=continuous_threshold_m,
            route_speed_mps=route_speed_mps, aircraft_id=aircraft_id,
        )
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

    def _canonical_candidate_actions(self, state, targets, baseline_gap):
        """P16 canonical candidate families 的**唯一**构造入口（Round 29-K 业务裁定）。

        ``S:radar_noncooperative`` 的规划 authority 是 ``radar_surveillance_layout``：
        真实铁塔 → Radar-I → bearing / bearing±45° → dominated pruning → MILP →
        5m validation → canonical gap verdict。它已经完成 Radar 候选面板的枚举与优化，
        因此 canonical P16 **绝不再**调用 ``radar_candidate_actions`` 做第二次优化。

        P16 只负责它自己拥有的 planner family：

        * ``C:communication`` / ``S:rid_cooperative`` → ``omnidirectional_site``；
        * ``N:navigation_integrity_monitoring`` → ``endpoint_integrity_monitor``；
        * ``N:rtk_augmentation`` → ``navigation_reference_station``。

        Radar **证据**继续进入 P14/P15：``_rerun`` 依 ``radar_surveillance_layout``
        现场构造 ``radar_service_evidence``（只读消费上游 ``selected_panels``），
        这里绝不追加任何 Radar 面板动作。
        """

        ordinary_targets = [
            item for item in targets
            if item.get("service_key") != SERVICE_KEY_RADAR_NONCOOPERATIVE
        ]
        actions = candidate_actions(
            ordinary_targets, state.get("existing_cns_facilities") or {},
            state.get("candidate_sites") or {}, device_catalog_with_evidence(state),
            state.get("tower_colocation_candidates") or {},
        )
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
        actions.extend(navigation_integrity_monitor_actions(
            (baseline_gap or {}).get("endpoint_service_gaps") or {},
        ))
        return sorted(actions, key=lambda item: item["action_id"]), baseline_navigation_evidence

    def _positive_gain_candidate(self, action, impact, continuous_channel):
        """该候选是否**真正正向** —— 与正式路径同一判据（只读；Round 30-C3B 收口）。

        "正向"在这里的唯一含义是"正式选择循环在这一点上会把它当作赢家候选"，因此判据
        只能是**正式排名器本身**（绝不另立一套近似的 gain > 0 规则）：

        * 离散通道：``rank([action], [impact])`` 非空 —— 即 action 资格 eligible、
          impact ``status == eligible``、已确认增益 > 0、无 regression，成本可比性只影响
          评分不影响候选资格，与正式路径逐字段同一实现；
        * 连续服务通道：当前投影态**明确**不合格（``continuous_channel``）、该候选
          ``_continuous_rankable``（跨越可接受阈值 —— 正式通道的入口门禁，只缩短仍
          不合格的候选**不**构成停止理由）且 ``rank_continuous_service([action],
          [impact])`` 非空，与正式路径同一实现。
        """

        if self.planner.rank([action], [impact]):
            return True
        return bool(
            continuous_channel
            and _continuous_rankable(impact)
            and self.planner.rank_continuous_service([action], [impact])
        )

    def _probe_positive_remaining_candidates(
        self, actions, selected_ids, current_tier, facilities, current_gap, targets,
        iteration, radar_actions, navigation_actions, baseline_navigation_evidence,
        prepared_cells, prefilter, continuous_threshold_m, route_speed_mps,
        aircraft_id, endpoint_evidence, endpoint_actions,
    ):
        """达选择上限时的**只读** existence probe（Round 30-C3B 收口）。

        它只回答一个问题：从**当前** reuse tier 起，按原 REUSE_TIERS / candidate 顺序，
        是否还存在真正正向的候选？"正向"由 :meth:`_positive_gain_candidate` 用正式排名器
        判定，因此"probe 找到正向候选"与"正式循环在这一刻会选出赢家"是同一件事。

        边界（全部只读）：

        * 不修改候选顺序、tier 顺序、选中序列、facilities、投影态、P15/P17、cap 默认值；
        * 找到**第一个**正向候选即短路返回（真实项目通常只需极少数几次 what-if）；
        * 探测不到任何正向候选时返回 ``found=False`` 与已算出的 ``impacts``，调用方据此
          回到**旧**路径（完整评估 → 自然耗尽 → ``_no_positive_candidate_stop_reason``），
          旧 ``stop_reason`` 词表语义逐字保持；
        * 排在 ``current_tier`` **之前**的 tier 不回看：旧实现同样不回看（tier 只向前推进），
          回看会凭空多出"达上限"结论；
        * 每个候选只做**一次** what-if，且与正式评估使用同一 facilities / 投影态 /
          ``iteration`` / prefilter，因此结果可直接复用，不存在第二套口径；
        * probe 自身的耗时**单独**核算（``limit_positive_probe_seconds``），**不**并入
          ``performance_profile``：后者是"选择循环的候选 what-if 画像"，语义保持不变。

        返回 ``{"found", "evaluated", "tier", "action_id", "seconds", "impacts"}``，其中
        ``impacts`` 是 tier → action_id → impact 的缓存。
        """

        performance = _new_performance_profile([])
        started = time.perf_counter()
        continuous_channel = _continuous_service_unacceptable(
            current_gap, continuous_threshold_m, route_speed_mps, aircraft_id,
        )
        impacts, evaluated = {}, 0
        for tier in REUSE_TIERS[REUSE_TIERS.index(current_tier):]:
            tier_actions = [
                action for action in actions
                if action.get("reuse_class") == tier
                and action.get("action_id") not in selected_ids
            ]
            impacts[tier] = {}
            for action in tier_actions:
                impact, _, _ = self._what_if(
                    action, facilities, current_gap, targets, iteration=iteration,
                    radar_actions=radar_actions, navigation_actions=navigation_actions,
                    baseline_navigation_evidence=baseline_navigation_evidence,
                    prepared_cells=prepared_cells, prefilter=prefilter,
                    continuous_threshold_m=continuous_threshold_m,
                    route_speed_mps=route_speed_mps, aircraft_id=aircraft_id,
                    endpoint_evidence=endpoint_evidence,
                    endpoint_actions=endpoint_actions, performance=performance,
                )
                evaluated += 1
                impacts[tier][str(action.get("action_id"))] = impact
                if self._positive_gain_candidate(action, impact, continuous_channel):
                    return {
                        "found": True, "evaluated": evaluated, "tier": tier,
                        "action_id": action.get("action_id"),
                        "seconds": round(time.perf_counter() - started, 3),
                        "impacts": impacts,
                    }
        return {
            "found": False, "evaluated": evaluated, "tier": None, "action_id": None,
            "seconds": round(time.perf_counter() - started, 3), "impacts": impacts,
        }

    def _rerun(
        self, facilities, radar_actions=None, navigation_actions=None,
        baseline_navigation_evidence=None, prepared_cells=None, performance=None,
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
        if performance is not None:
            performance["full_corridor_rerun_count"] += 1
        return rerun_corridor_chain(
            self.session.state, self.corridor_model,
            self.corridor_gap_analyzer, facilities,
            radar_service_evidence=radar_evidence,
            navigation_service_evidence=navigation_evidence,
            prepared_cells=prepared_cells, timings=performance,
        )



def rerun_corridor_chain(
    state, corridor_model_prototype, corridor_gap_prototype, facilities,
    *, radar_service_evidence=None, navigation_service_evidence=None,
    prepared_cells=None, timings=None,
):
    """Run the existing P14→P15 chain on caller-owned working data.

    ``prepared_cells`` 只用于跳过**同一份** grid / terrain / surface facts 下的重复准备
    （P16 cumulative what-if）。不传时 P14 自行准备，行为与结果逐字段不变。

    ``timings`` 是可选的**只读性能画像累加器**（P16 第一阶段 profiling）：它只累加
    P14 / P15 各自耗时，绝不参与任何判定。
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
    started = time.perf_counter()
    corridor = corridor_model.evaluate(
        state.get("operational_routes") or [], state.get("spatial_3d") or {},
        state.get("grid") or {}, state.get("grid_attributes") or {},
        state.get("required_cns") or {}, profile, facilities,
        device_catalog_with_evidence(state), state.get("cns_corridor_policy") or {},
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
    if timings is not None:
        timings["p14_seconds"] += time.perf_counter() - started
    analyzer = corridor_gap_prototype.__class__(
        (state.get("cns_corridor_gap_assessment") or {}).get("parameters")
        or getattr(corridor_gap_prototype, "parameters", {})
    )
    started = time.perf_counter()
    gap = analyzer.evaluate(
        corridor, state.get("required_cns") or {},
        state.get("cns_planning_objectives") or {},
    )
    if timings is not None:
        timings["p15_seconds"] += time.perf_counter() - started
    return corridor, gap


def _action_planner_family(action):
    """动作的 canonical planner family：显式字段优先，否则由 ``service_key`` 解析。

    ``candidate_actions`` 产出的普通站点动作**不带** ``planner_family`` 字段，其族别
    由 canonical registry 依 ``service_key`` 决定（``C:communication`` /
    ``S:rid_cooperative`` → ``omnidirectional_site``）。这里复用**同一个**解析入口，
    绝不自己维护第二份 service → family 映射。
    """

    family = str(action.get("planner_family") or "")
    if family:
        return family
    key = str(action.get("service_key") or "")
    if not key:
        return ""
    try:
        return str(planner_family_for(key))
    except Exception:  # noqa: BLE001 - 未知 service_key 如实返回空串，不猜测
        return ""


def _candidate_shape_statistics(actions):
    """canonical 候选的**两个互不替代**的只读统计维度（Round29-K1 审计口径收口）。

    * ``all_tiers_planner_family_counts`` —— **family 维度**：一个 family 的计数跨越
      **全部** reuse tier（例如 ``omnidirectional_site`` 同时含 tower / 已有设施 /
      候选站址三类动作）；
    * ``all_tiers_reuse_class_counts`` —— **tier 维度**：一个 tier 的计数跨越全部
      planner family（例如 ``tower_colocation_host`` 里的动作全是 omnidirectional）；
    * ``all_tiers_family_by_reuse_class`` —— 两维交叉表。

    这两个维度**绝不可互相顶替**：把某个 tier 内的 omnidirectional 数量当成全局
    omnidirectional 数量会得出错误的 before/after 对比（本轮真实权威项目的审计里
    ``tower_colocation_host`` 的 746 与全局 ``omnidirectional_site`` 的 750 就是两种
    口径）。本函数只统计，不排序、不筛选、不写 state。
    """

    family_counts = Counter(_action_planner_family(item) for item in actions)
    reuse_counts = Counter(str(item.get("reuse_class") or "") for item in actions)
    by_reuse = {}
    for item in actions:
        reuse = str(item.get("reuse_class") or "")
        by_reuse.setdefault(reuse, Counter())[_action_planner_family(item)] += 1
    return {
        "all_tiers_candidate_total": len(actions),
        "all_tiers_planner_family_counts": dict(sorted(family_counts.items())),
        "all_tiers_reuse_class_counts": dict(sorted(reuse_counts.items())),
        "all_tiers_family_by_reuse_class": {
            reuse: dict(sorted(counts.items())) for reuse, counts in sorted(by_reuse.items())
        },
        "semantics": (
            "planner_family_is_an_all_tiers_dimension_reuse_class_is_a_tier_dimension_"
            "never_substitute_one_for_the_other"
        ),
    }



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
    for device in (device_catalog_with_evidence(state)).get("items") or []:
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
        for item in (device_catalog_with_evidence(state)).get("items") or []
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
      unknown evidence（绝不自动建站）；``satisfied`` → 不产生 target；
      ``not_applicable`` → **完全跳过**（既不建 target，也不登记 unknown evidence：
      它根本不属于该 service 的适用范围，例如 fixed-cruise Radar 的 off-layer 体元）；
      缺少 ``distinct_site_count`` 证据同样进入 unknown；
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
                #: Round 29-N：service-level ``not_applicable`` **完全跳过** ——
                #: 它不是 satisfied，也**不是**未满足证据。只有 ``confirmed_deficit``
                #: 产生 confirmed target，``unknown`` 产生 unknown evidence。
                #: 把 not_applicable 当 active 会凭空造出"永远无法被任何候选动作满足"
                #: 的目标（真实 bug：fixed-cruise Radar 的 off-layer 体元）。
                applicable_services = [
                    item for item in surface_services
                    if str(item.get("status") or "unknown") != "not_applicable"
                ]
                active_services = [
                    item for item in applicable_services
                    if str(item.get("status") or "unknown") != "satisfied"
                ]
                if active_services:
                    targets.extend(_service_targets(
                        route_id, voxel_id, code, voxel, entry, active_services, unknown,
                    ))
                    continue
                if surface_services and not applicable_services:
                    #: 本体元全部 service 证据对本走廊口径都不适用：既不产生 target，
                    #: 也不产生 unknown evidence，更不回落到 legacy 子系统口径。
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
    #: Round 29-G：route-endpoint 服务的建站目标（按 service_scope dispatch），
    #: 与走廊体素 target 并列进入同一 target 集，但绝不共用 target_id 形状。
    targets.extend(_endpoint_targets(assessment))
    return sorted(targets, key=lambda item: item["target_id"]), sorted(unknown, key=lambda item: item["target_id"])


#: Round 29-K：Radar 缺口的规划 authority。P16 **只读**消费，绝不二次优化、
#: 也绝不二次推导 canonical gap 结论（``gap_reason`` / ``gap_classification`` /
#: ``managed_physical_gap`` 一律从上游 layout 原样转印）。
RADAR_PLANNING_OWNER = "radar_surveillance_layout"

#: 上游 layout 中**尚未给出已证明结论**的状态：证据不足 / 未评估 / 搜索未完成。
#: 它们都**不是** managed physical gap，P16 保持 unknown/evidence_required。
RADAR_NON_TERMINAL_LAYOUT_STATUSES = (
    "search_incomplete", "refinement_incomplete", "unresolved", "solver_error",
    "solver_unavailable", "not_ready", "stale", "not_calculated",
)


def _radar_layout_item_index(layout):
    """``{route_id: item}`` 的只读索引（同一 route 多条时取第一条，绝不猜测合并）。"""

    index = {}
    for item in (layout or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        route_id = str(item.get("route_id") or "")
        if route_id and route_id not in index:
            index[route_id] = item
    return index


def _radar_authority_reason(status, classification, layout_stale=False):
    """上游 layout 未给出 managed gap 时的**如实**原因（绝不升级为 confirmed）。"""

    if layout_stale:
        return (
            "上游 radar_surveillance_layout 的结果由**旧算法语义版本**产生"
            "（algorithm_semantics_changed）：必须先用当前算法重算 layout；在此之前"
            "P16 绝不把它当作 current 的可行 / 不可行结论。"
        )
    if classification == "confirmed_gap":
        return (
            "上游 radar_surveillance_layout 给出 confirmed gap，但未登记为 managed "
            "physical gap：P16 不据此声称可行或不可行，只登记证据要求。"
        )
    if status == "proposal_ready":
        return (
            "上游 radar_surveillance_layout 已给出方案（proposal_ready）：P16 只读消费"
            "其 selected_panels，绝不追加第二份雷达面板。"
        )
    return (
        f"上游 radar_surveillance_layout 未给出已证明结论（status={status or 'missing'}）："
        "这不是 managed physical gap，P16 保持 unknown/evidence_required，"
        "绝不自行补面阵绕过上游规划权威。"
    )


def _radar_gap_authority(targets, state):
    """按上游 layout 的 canonical verdict 对 P15 的 Radar 缺口做**只读分类**。

    业务裁定（Round 29-K）：

    * 只有 ``status=infeasible`` **且** ``gap_classification=confirmed_gap`` **且**
      ``managed_physical_gap is True`` 才是**已证明**的物理限制，登记为
      ``terminal_managed_gaps``（``p16_actionable=false``）。它是**事实缺口**，
      绝不被改写成 satisfied，也绝不让 P16 继续搜索面板或无限重评；
    * ``search_incomplete`` / ``refinement_incomplete`` / ``unresolved`` / ``stale``
      等**不是** managed physical gap：如实登记为 unknown/evidence_required，P16
      绝不把它转成 confirmed infeasible，也绝不自补面阵绕过上游；
    * 上游 layout 自身若由**旧算法语义版本**产生，则它整体不可作为 current 结论
      （只读投影给出 ``algorithm_semantics_changed``）——此时一律走
      unknown/evidence_required，**绝不**产生 terminal managed gap；
    * ``proposal_ready`` 说明上游已给出方案：P16 只读消费其 ``selected_panels``。

    返回 ``(terminal_managed_gaps, radar_unknown_evidence, actionable_targets)``。
    """

    projected_layout = projected_result(state, "radar_surveillance_layout")
    layout_stale = bool(
        isinstance(projected_layout, dict)
        and projected_layout.get("status") == "stale"
        and projected_layout.get("stale_reason") == "algorithm_semantics_changed"
    )
    index = _radar_layout_item_index(state.get("radar_surveillance_layout") or {})
    terminal, unknowns, actionable = [], [], []
    for target in targets or []:
        if str(target.get("service_key") or "") != SERVICE_KEY_RADAR_NONCOOPERATIVE:
            actionable.append(target)
            continue
        route_id = str(target.get("route_id") or "")
        item = index.get(route_id) or {}
        status = str(item.get("status") or "")
        classification = item.get("gap_classification")
        managed = item.get("managed_physical_gap")
        solver = item.get("solver") or {}
        stale = layout_stale or status == "stale"
        if (
            not stale
            and status == "infeasible"
            and classification == "confirmed_gap"
            and managed is True
        ):
            terminal.append({
                "target_id": target.get("target_id"),
                "route_id": route_id,
                "service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE,
                "source": RADAR_PLANNING_OWNER,
                "planning_owner": RADAR_PLANNING_OWNER,
                "layout_status": status,
                "gap_reason": item.get("gap_reason"),
                "gap_classification": classification,
                "managed_physical_gap": True,
                "solver_infeasibility_proven": solver.get("infeasibility_proven"),
                "proof_basis": (
                    "solver_infeasibility_proven"
                    if solver.get("infeasibility_proven") is True
                    else "layout_canonical_presolve_or_solver_verdict"
                ),
                "gap_kind": "non_actionable_terminal_managed_gap",
                "p16_actionable": False,
                "factual_gap": True,
                "satisfied": False,
                "reason": (
                    "Radar 规划 authority 已在上游 radar_surveillance_layout 证明当前约束下"
                    "不可行：P16 不再枚举雷达候选面板，也不把它当成可由 P16 消除的目标。"
                ),
            })
            continue
        unknowns.append({
            "kind": "radar_planning_authority_upstream",
            "target_id": target.get("target_id"),
            "route_id": route_id,
            "subsystem": target.get("subsystem"),
            "service_key": SERVICE_KEY_RADAR_NONCOOPERATIVE,
            "source": RADAR_PLANNING_OWNER,
            "planning_owner": RADAR_PLANNING_OWNER,
            "layout_status": status or None,
            "layout_algorithm_semantics_stale": stale,
            "gap_reason": item.get("gap_reason"),
            "gap_classification": classification,
            "managed_physical_gap": managed,
            "p16_actionable": False,
            "requires_upstream_layout_evidence": True,
            "reasons": [_radar_authority_reason(status, classification, stale)],
        })
    return terminal, unknowns, actionable


def _service_targets(route_id, voxel_id, code, voxel, entry, surface_services, unknown):
    """由 surface-dependent / site-planned service evidence 建立 service-level targets。"""

    targets = []
    for service in surface_services:
        key = str(service.get("service_key") or "")
        target_id = corridor_target_id(route_id, voxel_id, key)
        status = str(service.get("status") or "unknown")
        if status == "satisfied":
            continue
        if status == "not_applicable":
            #: Round 29-N：service-level ``not_applicable`` 既不是缺口也不是证据不足，
            #: 绝不建 target、也绝不登记 unknown（防御性判定，调用方已先行过滤）。
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


def _endpoint_targets(assessment):
    """P16 **endpoint targets**：只来自 P15 ``endpoint_service_gaps`` 的已确认缺口。

    Route-endpoint 服务（当前唯一成员是 ``N:navigation_integrity_monitoring``）的正式状态
    **只**来自 endpoint 证据，不参与走廊体素聚合，因此它的建站目标也必须在 endpoint 口径
    显式建立 —— 否则该缺口既没有 corridor target、也没有 endpoint target，永远无法被任何
    候选动作看见（Round 29-F 已确认的真实后果）。

    dispatch **按 ``service_scope`` 判定**，绝不硬编码子系统或服务键：任何注册为
    ``service_scope == "route_endpoints"`` 且支持站址规划的服务都走这条路径。
    """

    targets = []
    for route in (assessment or {}).get("endpoint_service_gaps", {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        key = str(route.get("service_key") or "")
        if not key:
            continue
        spec = service_registry_entry(key)
        if spec.get("service_scope") != "route_endpoints":
            continue
        if spec.get("supports_site_planning") is not True:
            continue
        for gap in route.get("confirmed_endpoint_gaps") or []:
            role = str(gap.get("endpoint_role") or "")
            if not role:
                continue
            targets.append({
                "target_id": endpoint_target_id(route_id, role, key),
                "route_id": route_id, "voxel_id": None,
                "subsystem": spec.get("subsystem"), "service_key": key,
                "target_scope": TARGET_SCOPE_ENDPOINT,
                "planner_family": spec.get("planner_family"),
                "endpoint_role": role,
                "takeoff_landing_site_id": gap.get("takeoff_landing_site_id"),
                "surface_class": None,
                "counting_basis": "distinct_site_id",
                "required_units": 1, "current_units": 0, "remaining_units": 1,
                "distinct_site_ids": [],
                #: endpoint 没有走廊体素体积：收益按**已确认 endpoint requirement unit**
                #: 计数（工程规划代理量），绝不借用任何体素体积冒充收益。
                "discretized_volume_proxy_m3": None,
                "unit_gain_weight": 1.0,
                "benefit_basis": "confirmed_endpoint_requirement_unit_count",
                "nearest_route_offset_m": None,
                "causes": deepcopy(gap.get("reasons") or []),
                "source_status": "confirmed_gap",
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
    if target.get("target_scope") == TARGET_SCOPE_ENDPOINT:
        return _endpoint_confirmed_units(entry)
    if target.get("service_key") == SERVICE_KEY_NAVIGATION_RTK_AUGMENTATION:
        return _navigation_confirmed_units(target, entry)
    if target.get("service_key"):
        return _service_confirmed_units(entry, target.get("required_units"))
    return _confirmed_units(entry, target["required_units"])


def _endpoint_confirmed_units(entry):
    """endpoint target 的 current units：**只**计"该起降点是否已装上 monitor"。

    这条计数契约与 ``_service_confirmed_units`` 同构：``planning_status`` 是**站址/设备
    侧**的满足度（``required_count`` 个 monitor 是否已安装），而 ``combined_status``
    是综合结论（还叠加 Communication delivery 依赖）。

    因此安装 monitor **只**解决 ``*_integrity_monitor_missing``；若 C delivery 仍是
    ``confirmed_deficit``，该 target 的 ``combined_status`` 仍是 ``confirmed_gap``，
    照常登记为 residual —— 既不会把已确认的站址进展误判成 0，也绝不把
    delivery 缺口悄悄升级成 satisfied。

    ``planning_status`` 为 ``unknown`` 时返回 ``None``（fail-closed，绝不猜测）。
    """

    planning = str(entry.get("planning_status") or "unknown")
    if planning == "unknown":
        return None
    return 1 if planning == "satisfied" else 0


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


def _impact(action, before, after, targets, continuous_threshold_m=None,
            route_speed_mps=None, aircraft_id=None):
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

    Round 2.8 additive：本函数同时给出 **continuous_service_gain**（连续服务收益）。
    离散 unit 增益为 0 但能把连续缺口缩短 / 跨过阈值（例如 143.54 m → 0 m，
    9.57 s → 0 s）的真实候选，**必须**能被 P16 当作正向动作看见，否则会再次出现
    "P16 已停止、P17 仍 unacceptable" 的矛盾。
    """

    before_acceptance = _continuous_service_acceptance(
        before, continuous_threshold_m, route_speed_mps, aircraft_id,
    )
    after_acceptance = _continuous_service_acceptance(
        after, continuous_threshold_m, route_speed_mps, aircraft_id,
    )
    continuous_gain = _continuous_service_gain(
        before, after, before_acceptance, after_acceptance,
    )
    continuous_reduction_m = float(
        continuous_gain.get("longest_outage_reduction_m") or 0.0
    )

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
        volume = _target_benefit_weight(target)
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
    elif continuous_reduction_m > 0 and continuous_gain.get("threshold_available") is True:
        #: Round 2.8：离散 unit 增益为 0，但**连续服务缺口确实被缩短**。
        #: 旧模型会把它归入 unknown/ineligible 并静默丢弃，于是出现"P16 停止、
        #: P17 仍 unacceptable"。这里明确登记为连续服务正向动作。
        #:
        #: **fail-closed 边界**：只有本项目确实登记了 C 全失联阈值（阈值可判定）时，
        #: "缺口缩短"才允许作为正向动作。阈值不可判定时保持原有 unknown / ineligible
        #: 结论——``UNKNOWN ≠ PASS`` 在连续服务维度同样成立，绝不因为"缺口变小了"
        #: 就绕过"没有阈值证据"这件事。
        status, reasons = "eligible", [
            "该动作未产生新的已确认 requirement-unit 进度（离散目标已满足），"
            "但缩短了连续服务缺口投影："
            f"最长 {continuous_gain['before_longest_deficit_m']:.3f} m → "
            f"{continuous_gain['after_longest_deficit_m']:.3f} m"
            "（与 P17 同一指标、同一阈值，绝不作为风险或概率解释）"
        ]
        if has_unknown:
            reasons.append(
                "该动作对部分 target 仍缺可确认证据：连续服务收益有效，"
                "但 unknown 独立保留、不计入 satisfied、不计入冗余满足"
            )
    elif continuous_reduction_m > 0:
        #: 缺口缩短但阈值证据缺失：如实登记"不可判定"，不得升级为合格动作。
        status, reasons = "unknown", [
            "该动作缩短了连续服务缺口投影，但本项目未登记 C 全失联阈值证据，"
            "连续服务收益**不可判定**（fail-closed，绝不按默认阈值放行）"
        ]
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
        else "continuous_service_only" if gain <= 0 < continuous_reduction_m
        else "fully_confirmed"
    )
    return {
        "action_id": action.get("action_id"), "status": status,
        "confirmed_requirement_unit_volume_gain": gain,
        #: 新语义的显式别名：与上面同值，供"只读已确认增益"的消费方直接使用。
        "confirmed_gain": gain,
        #: Round 2.8：连续服务收益（与离散体积增益**分开**披露，绝不混同）。
        "continuous_service_gain": continuous_gain,
        "continuous_service_gain_threshold_crossed": continuous_gain["threshold_crossed"],
        "continuous_service_gain_improved": continuous_reduction_m > 0,
        "continuous_service_gain_semantics": CONTINUOUS_SERVICE_GAIN_SEMANTICS,
        "before_continuous_service_acceptance": before_acceptance,
        "after_continuous_service_acceptance": after_acceptance,
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

    **边界方向（Round 29-I 收口；此方向极易被写反，务必不要反）**：

    ``required_clearance`` 是三项之和，于是

        ``max_cell_half_diagonal`` 越大 ⇒ ``required_clearance`` 越大
        ⇒ 越**难**满足 ``nearest > required_clearance``
        ⇒ 越**难** prefilter ⇒ 进入 full rerun 的候选**越多**。

    即：half_diagonal 取**偏小**值会让预筛**过于激进**（可能排掉本应重算的候选），
    属于乐观/不安全方向；只有取**偏大**值才是保守方向。

    真实 L8 metric half diagonal 由每个 cell bbox 的西南角→东北角测地距离之半给出
    （见 :func:`...algorithms.corridor.v1._prepare_cells`），且**随纬度单调递减**。
    因此 ``max_cell_half_diagonal`` 必须取自**实际参与评估的 prepared_cells** 的最大值
    （即最低纬度那一格）；**不得**用"航路最高纬度"或任何单点合成值替代 —— 那只会得到
    真实上界的下界。缺少 prepared_cells 时本函数返回 ``None``（不做任何预筛）。

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
        for item in (device_catalog_with_evidence(state)).get("items") or []:
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
        #: Round 2.8：预筛结论同样必须显式声明连续服务收益为**不可确认**，
        #: 而不是留空让消费方误读成"没有连续收益"。
        "continuous_service_gain": None,
        "continuous_service_gain_threshold_crossed": False,
        "continuous_service_gain_improved": False,
        "continuous_service_gain_unavailable_reason": "prefiltered_no_corridor_interaction",
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
    """P15 assessment → ``{target_id: entry}``（subsystem + service + endpoint 三类 target）。

    与 planner 的 residual 判定共用 :func:`corridor_voxel_entry_index`，因此
    service-aware target 的生命周期与 legacy target 完全同构；endpoint target 另由
    :func:`endpoint_state_entry_index` 索引**完整 endpoint 状态**（含 satisfied，
    否则"缺口被解决"会被误读成"条目缺失"）。
    """

    entries = corridor_voxel_entry_index(assessment)
    entries.update(endpoint_state_entry_index(assessment))
    return entries


def _route_corridor_status(route):
    """route 的**走廊本体**状态（不含 endpoint 覆盖），与 P15 ``_aggregate`` 同一规则。"""

    statuses = [
        str(item.get("status") or "missing_data")
        for item in (route or {}).get("subsystems") or []
    ]
    return _aggregate_statuses(statuses)


def _aggregate_statuses(statuses):
    """P15 route 级状态聚合规则的**唯一**副本（failed > pending/missing > all n/a > passed）。"""

    statuses = list(statuses or [])
    if not statuses:
        return "missing_data"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status in ("pending_confirmation", "missing_data") for status in statuses):
        return "pending_confirmation"
    if all(status == "not_applicable" for status in statuses):
        return "not_applicable"
    return "passed"


def _endpoint_overlay_status(corridor_status, endpoint_route):
    """endpoint 缺口覆盖 route 状态的**唯一**规则（与 P14/P15 的既有语义逐字一致）。"""

    status = str((endpoint_route or {}).get("status") or "")
    if status == "confirmed_deficit":
        return "failed"
    if status == "unknown" and corridor_status == "passed":
        return "pending_confirmation"
    return corridor_status


def _project_endpoint_gap(gap, endpoint_evidence):
    """把（可能已被 endpoint 动作更新的）endpoint 证据投影回 P15 gap。

    **只**改 endpoint 相关字段：``endpoint_service_gaps`` 与每个 route 的
    ``endpoint_services`` / ``status``。走廊子系统的任何字段（含逐 voxel 明细）逐项不变
    —— 因此这条路径既不需要、也不允许重跑 P14/P15。

    ``endpoint_evidence`` 为 ``None``（本项目未要求该服务）或没有任何匹配 route 时**原样
    返回** ``gap``（连一层拷贝都不做）。

    **性能契约（Round 29-G）**：本函数返回的 gap 与输入 gap **共享只读的**
    ``routes[].voxels`` 子树 —— 真实项目有 2844 个体素，逐候选 deepcopy 一次约 0.1 s，
    而 endpoint 专用 what-if 会对同一 gap 反复投影。调用方**不得**就地修改返回值里的
    voxel；需要长期持有或被后续就地修改时必须自行 ``deepcopy``。
    """

    if endpoint_evidence is None or gap is None:
        return gap
    endpoint_gaps = endpoint_gap_evidence(endpoint_evidence)
    by_route = {
        str(item.get("route_id")): item for item in endpoint_gaps.get("routes") or []
    }
    routes = []
    changed = False
    for route in gap.get("routes") or []:
        endpoint = by_route.get(str(route.get("route_id") or ""))
        if endpoint is None:
            routes.append(route)
            continue
        changed = True
        corridor_status = _route_corridor_status(route)
        routes.append({
            **route,
            "endpoint_services": [deepcopy(endpoint)],
            "status": _endpoint_overlay_status(corridor_status, endpoint),
        })
    if not changed:
        return gap
    return {
        **gap,
        "routes": routes,
        "endpoint_service_gaps": endpoint_gaps,
        "status": _aggregate_statuses(
            [route.get("status") for route in routes]
        ),
    }


def _endpoint_what_if_evidence(action, after_endpoint_evidence):
    """endpoint 专用 what-if 的**证据块**（显式声明未重跑走廊链）。"""

    return {
        "evidence": [{
            "kind": "endpoint_integrity_monitor_incremental_what_if",
            "action_id": action.get("action_id"),
            "endpoint_role": action.get("endpoint_role"),
            "takeoff_landing_site_id": action.get("takeoff_landing_site_id"),
            "planning_family": ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY,
            "input_fingerprint": (after_endpoint_evidence or {}).get("input_fingerprint"),
            "persisted": False,
            "corridor_rerun": False,
            "semantics": (
                "endpoint_only_incremental_what_if_never_reruns_p14_p15_"
                "because_the_action_cannot_change_corridor_geometry"
            ),
        }],
    }


def _target_benefit_weight(target):
    """P16 target 的收益权重。

    * 走廊体素 target：离散体积代理 ``discretized_volume_proxy_m3``（既有口径，逐项不变）；
    * endpoint target：没有体素体积，收益按**已确认 endpoint requirement unit 个数**计
      （``unit_gain_weight``，明确是工程规划代理量，不是风险或概率）。
    """

    volume = (target or {}).get("discretized_volume_proxy_m3")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        return float(volume)
    weight = (target or {}).get("unit_gain_weight")
    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
        return float(weight)
    return 0.0


def _new_performance_profile(actions):
    """P16 第一阶段的性能画像累加器（纯计数，不参与任何判定）。"""

    by_family = Counter()
    by_type = Counter()
    for action in actions or []:
        by_family[str(action.get("planner_family") or "unknown")] += 1
        by_type[str(action.get("action") or action.get("action_type") or "unknown")] += 1
    return {
        "candidate_count_by_planner_family": dict(sorted(by_family.items())),
        "candidate_count_by_action_type": dict(sorted(by_type.items())),
        "full_corridor_rerun_count": 0,
        "endpoint_fast_path_count": 0,
        "endpoint_fast_path_apply_count": 0,
        "endpoint_fast_path_seconds": 0.0,
        "p14_seconds": 0.0,
        "p15_seconds": 0.0,
    }


def _finalize_performance_profile(performance, selected):
    """收口性能画像：补上派生量与语义声明（**只读**，绝不改变判定）。"""

    profile = deepcopy(performance or {})
    by_family = profile.get("candidate_count_by_planner_family") or {}
    profile["corridor_full_rerun_required_planner_families"] = sorted(
        key for key in by_family
        if key not in (ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY,)
    )
    profile["endpoint_fast_path_planner_family"] = ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY
    profile["p14_seconds"] = round(float(profile.get("p14_seconds") or 0.0), 3)
    profile["p15_seconds"] = round(float(profile.get("p15_seconds") or 0.0), 3)
    profile["endpoint_fast_path_seconds"] = round(
        float(profile.get("endpoint_fast_path_seconds") or 0.0), 6
    )
    profile["corridor_full_reruns_avoided_by_endpoint_fast_path"] = (
        profile.get("endpoint_fast_path_apply_count", 0)
    )
    profile["selected_action_count"] = len(selected or [])
    profile["selected_count_by_planner_family"] = dict(sorted(Counter(
        str(action.get("planner_family") or "unknown") for action in selected or []
    ).items()))
    profile["semantics"] = (
        "read_only_p16_phase1_profile_candidate_counts_and_elapsed_seconds_"
        "endpoint_actions_never_trigger_a_full_p14_p15_rerun"
    )
    return profile


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


def _continuous_service_unacceptable(assessment, threshold_m, route_speed_mps, aircraft_id=None):
    """当前投影态的连续服务是否**明确**不合格（阈值可判定且超限）。"""

    return _continuous_service_acceptance(
        assessment, threshold_m, route_speed_mps, aircraft_id,
    ).get("acceptable") is False


def _continuous_rankable(impact):
    """该候选是否构成"连续服务可接受性被建立"的可停止理由（Round 2.8 收口）。

    只有 ``threshold_crossed`` 为真才进入连续服务候选池。理由：

    * "把不合格缩短为另一个不合格"**不是**停止理由，也不改变 P17 的 unacceptable；
    * 真实项目里有大量这样的候选，逐个执行完整 P14/P15 重算会把 P16 单次评估从数分钟
      推到数十分钟，并把选站数无意义地推到干预上限；
    * 该候选的连续收益仍然完整登记在它的 ``impact`` 与
      ``continuous_service_candidate_impacts`` 里，**不**丢证据。
    """

    if not isinstance(impact, dict) or impact.get("status") != "eligible":
        return False
    gain = impact.get("continuous_service_gain") or {}
    return gain.get("threshold_crossed") is True


def _confirmed_objectives_met(assessment):
    confirmed = []
    for item in _summary_map(assessment).values():
        confirmed.extend(value for value in item.get("objective_results") or [] if value.get("confirmed") is True)
    return bool(confirmed) and all(item.get("status") in ("met", "not_applicable") for item in confirmed)


#: Round 2.8：单次 P16 提案最多选中的干预动作数（**工程上限**，不是结论放宽）。
#: 超过它意味着"再多选站也无法收敛到可接受"，必须停下来把残余缺口作为正式结论披露，
#: 而不是无限迭代。可由 ``corridor_site_planning_policy.parameters`` 显式覆盖。
DEFAULT_INTERVENTION_SELECTION_LIMIT = 6


def _intervention_selection_limit(policy):
    """本次 P16 的干预选择上限（正整数；非法值退回工程默认）。"""

    raw = ((policy or {}).get("parameters") or {}).get("max_selected_interventions")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVENTION_SELECTION_LIMIT
    return value if value > 0 else DEFAULT_INTERVENTION_SELECTION_LIMIT


#: Round 30-C3B（收口后）：停止诊断的语义声明（**只读披露**，不参与选择或目标判定）。
STOP_DIAGNOSTICS_SEMANTICS = (
    "read_only_stop_diagnostics_never_participates_in_selection_or_objective_"
    "evaluation__at_the_selection_limit_a_read_only_existence_probe_reuses_the_"
    "formal_what_if_and_the_formal_rankers_so_intervention_selection_limit_reached_"
    "means_a_truly_positive_candidate_was_actually_found_and_if_no_positive_candidate_"
    "remains_the_legacy_no_positive_candidate_stop_reason_vocabulary_is_kept__"
    "full_candidate_evaluation_skipped_at_limit_means_only_that_the_remaining_"
    "candidates_were_not_fully_ranked_it_is_not_a_no_gain_conclusion__"
    "limit_positive_probe_evaluated_count_is_how_many_candidates_the_probe_actually_"
    "evaluated_before_short_circuiting__"
    "pending_candidate_count_is_how_many_candidates_were_pending_at_the_stop_point_"
    "it_is_not_an_evaluated_count_and_not_a_no_gain_conclusion__"
    "candidate_pool_exhausted_is_the_only_natural_no_positive_candidate_stop"
)


def _new_stop_diagnostics(selection_limit):
    """本轮 P16 停止诊断的初值（每次 evaluate 各一份，绝不跨轮复用）。"""

    return {
        "version": 2,
        "selection_limit": selection_limit,
        "selection_limit_reached": False,
        "full_candidate_evaluation_skipped_at_limit": False,
        "limit_positive_probe_performed": False,
        "limit_positive_probe_evaluated_count": 0,
        "limit_positive_probe_found": False,
        "limit_positive_probe_seconds": None,
        "candidate_pool_exhausted": False,
        "evaluated_iteration_count": 0,
        "last_evaluated_iteration_positive_count": None,
        "last_evaluated_iteration_positive_by_service": None,
        "pending_candidate_count": None,
        "pending_candidates_by_tier": None,
        "pending_candidates_by_service": None,
        "converged_without_positive_candidate": False,
        "stop_reason": None,
        "semantics": STOP_DIAGNOSTICS_SEMANTICS,
    }


def _record_iteration_positive(stop_diagnostics, evaluated, tier_actions):
    """登记某一轮 what-if 的正向（离散增益 > 0）候选计数（只读统计）。"""

    by_id = {str(item.get("action_id")): item for item in (tier_actions or [])}
    counts = Counter()
    positive = 0
    for impact in evaluated or []:
        if float((impact or {}).get("confirmed_requirement_unit_volume_gain") or 0.0) <= 0.0:
            continue
        positive += 1
        action = by_id.get(str((impact or {}).get("action_id"))) or {}
        counts[str(action.get("service_key") or "<unknown>")] += 1
    stop_diagnostics["evaluated_iteration_count"] += 1
    stop_diagnostics["last_evaluated_iteration_positive_count"] = positive
    stop_diagnostics["last_evaluated_iteration_positive_by_service"] = dict(sorted(counts.items()))


def _pending_candidate_profile(actions, selected_ids):
    """达干预上限时**尚未选中**的候选规模（按 reuse tier / service_key）。"""

    by_tier, by_service = Counter(), Counter()
    for action in actions or []:
        if str(action.get("action_id")) in selected_ids:
            continue
        by_tier[str(action.get("reuse_class") or "<none>")] += 1
        by_service[str(action.get("service_key") or "<none>")] += 1
    return {
        "total": sum(by_tier.values()),
        "by_tier": dict(sorted(by_tier.items())),
        "by_service": dict(sorted(by_service.items())),
    }


def _finalize_stop_diagnostics(stop_diagnostics, stop_reason):
    """补齐最终 ``stop_reason`` 并给出互斥的收敛结论（只读）。"""

    final = deepcopy(stop_diagnostics)
    final["stop_reason"] = stop_reason
    final["converged_without_positive_candidate"] = bool(
        final.get("candidate_pool_exhausted")
    )
    return final


def _no_positive_candidate_stop_reason(acceptance, plan_status, threshold_available):
    """没有正向候选时的**可区分**停止原因（Round 2.8）。

    旧实现无论哪种情况都写 ``no_positive_confirmed_marginal_gain``，于是
    "离散覆盖目标已满足、但连续服务仍不合格"会被读成"全部目标已满足"。

    与循环内的 ``all_required_services_acceptable`` 配合使用，形成完整的停止原因词表：

    * ``all_required_services_acceptable``                        —— 离散 + 连续全部达标
    * ``continuous_service_acceptable_no_remaining_improvement``  —— 连续达标，无剩余可改善
    * ``coverage_objectives_met_but_continuous_service_unacceptable``
                                                                  —— 离散达标但连续不合格
    * ``no_positive_remaining_candidate_continuous_service_threshold_unknown``
                                                                  —— 阈值证据缺失，不得声称达标
    * ``no_positive_confirmed_marginal_gain``                     —— 本项目未登记连续服务
      阈值证据（该阈值不在本轮判据内），保持既有词表逐字不变
    * ``no_positive_remaining_candidate``                         —— 其它情形

    ``threshold_available=False`` 时的关键裁定：**没有阈值证据 ⇒ 连续服务不是本轮
    判据**，因此停止原因必须保持既有措辞，而不是给出一个暗示"连续服务不合格"的
    ``..._threshold_unknown``。否则没有登记该阈值的项目（例如 Round 2.7 的 fixture）
    会凭空多出一条并不存在的工程结论。
    """

    acceptable = (acceptance or {}).get("acceptable")
    if acceptable is False:
        return "coverage_objectives_met_but_continuous_service_unacceptable"
    if acceptable is None:
        return (
            "no_positive_remaining_candidate_continuous_service_threshold_unknown"
            if threshold_available else "no_positive_confirmed_marginal_gain"
        )
    if plan_status == "proposal_ready":
        return "continuous_service_acceptable_no_remaining_improvement"
    return "no_positive_remaining_candidate"


def _continuous_service_acceptance(assessment, threshold_m, route_speed_mps, aircraft_id=None):
    """P15 连续亏空投影的**可接受性**判定（Round 2.8，与 P17 同一口径）。

    为什么必须存在这一层：Round 2.8 之前 P16 的停止条件只看 P15 的**离散** objective
    （体积分数 / 冗余满足分数 / unknown 比例）。真实项目 R0005 里这三个目标在投影态
    **全部满足**（C 的 ``min_satisfied_volume_fraction >= 0.95`` 达标），于是 P16 打印
    ``all_evaluable_confirmed_objectives_met`` 并停止；而同一份投影态的**最长连续缺口**
    仍是 143.54 m ＝ 9.57 s ＞ 3.0 s（P17 判定 ``unacceptable``）。两者不是矛盾，而是
    P16 的停止条件根本不消费"连续服务"这一目标。

    本函数**不做任何新的假设**：

    * 阈值来自 :meth:`...ContinuousServiceService.continuous_service_threshold_m`，
      即"用户在规划证据里显式登记的 ``c_full_outage_max_s`` × 选定机载档案航路速度"；
      阈值不可判定（``None``）时返回 ``threshold_available=False``，绝不回落到任何默认值；
    * 指标直接用 P15 自己的 ``max_continuous_deficit_projection_m``（P17 的
      ``service_outage`` 事件长度正是它，Round 2.8 已逐字段核对：两者同为 143.5437… m）。
    """

    subsystems, longest, longest_key, unacceptable = [], 0.0, None, 0
    for key, item in sorted(_summary_map(assessment).items()):
        deficit = item.get("max_continuous_deficit_projection_m")
        deficit = 0.0 if deficit is None else float(deficit)
        duration = (
            None if route_speed_mps in (None, 0) else deficit / float(route_speed_mps)
        )
        exceeds = (
            None if threshold_m is None else bool(deficit > float(threshold_m) + 1e-9)
        )
        subsystems.append({
            "key": key, "subsystem": item.get("subsystem"),
            "max_continuous_deficit_projection_m": deficit,
            "longest_outage_duration_s": duration,
            "threshold_m": None if threshold_m is None else float(threshold_m),
            "exceeds_threshold": exceeds,
        })
        if exceeds:
            unacceptable += 1
        if deficit > longest:
            longest, longest_key = deficit, key
    if threshold_m is None:
        acceptable = None
        status = "threshold_evidence_required"
    elif unacceptable:
        acceptable, status = False, "continuous_service_unacceptable"
    else:
        acceptable, status = True, "continuous_service_acceptable"
    return {
        "semantics": (
            "p16_continuous_service_objective_uses_the_same_threshold_and_the_same_"
            "projection_metric_as_p17_never_a_second_formula"
        ),
        "status": status,
        "acceptable": acceptable,
        "threshold_available": threshold_m is not None,
        "threshold_s": (
            None if (threshold_m is None or route_speed_mps in (None, 0))
            else float(threshold_m) / float(route_speed_mps)
        ),
        "threshold_m": None if threshold_m is None else float(threshold_m),
        "route_speed_mps": None if route_speed_mps is None else float(route_speed_mps),
        "aircraft_id": aircraft_id,
        "longest_deficit_m": longest,
        "longest_deficit_key": longest_key,
        "longest_outage_duration_s": (
            None if route_speed_mps in (None, 0) else longest / float(route_speed_mps)
        ),
        "exceeds_threshold": None if threshold_m is None else bool(unacceptable),
        "unacceptable_subsystem_count": unacceptable,
        "subsystems": subsystems,
    }


#: 连续服务收益的语义（前端 / 报告逐字引用；绝不与离散体积增益混同）。
CONTINUOUS_SERVICE_GAIN_SEMANTICS = (
    "continuous_service_gain_is_longest_projected_outage_reduction_on_the_same_"
    "conservative_longitudinal_projection_used_by_p17"
)


def _continuous_service_gain(before, after, before_acceptance, after_acceptance):
    """一条候选动作的**连续服务收益**（Round 2.8 P16 收益模型修复）。

    P16 原来的收益模型只有"已确认 requirement-unit 体积增益"。真实项目里存在这样的
    候选：离散 unit 增益为 0（P15 离散目标已满足，没有新的 unit 可确认），但它把
    连续缺口从 143.54 m 直接降到 0 m（9.57 s → 0 s，跨过 3.0 s 阈值）。这类动作在旧
    模型里被静默丢弃，正是"P16 停止但 P17 仍不合格"的第二条根因。

    本函数只**减少量**与**阈值跨越**，绝不改变任何判定规则，也绝不把收益说成风险或概率。
    """

    before_map, after_map = _summary_map(before), _summary_map(after)
    before_subs = {item["key"]: item for item in (before_acceptance or {}).get("subsystems") or []}
    after_subs = {item["key"]: item for item in (after_acceptance or {}).get("subsystems") or []}
    per_subsystem, total = [], 0.0
    for key, left in sorted(before_map.items()):
        right = after_map.get(key) or {}
        before_deficit = float(left.get("max_continuous_deficit_projection_m") or 0.0)
        after_deficit = float(right.get("max_continuous_deficit_projection_m") or 0.0)
        reduction = max(0.0, before_deficit - after_deficit)
        before_entry, after_entry = before_subs.get(key) or {}, after_subs.get(key) or {}
        became_acceptable = bool(
            before_entry.get("exceeds_threshold") is True
            and after_entry.get("exceeds_threshold") is False
        )
        total += reduction
        per_subsystem.append({
            "key": key, "subsystem": left.get("subsystem"),
            "before_max_continuous_deficit_projection_m": before_deficit,
            "after_max_continuous_deficit_projection_m": after_deficit,
            "reduction_m": reduction,
            "before_outage_duration_s": before_entry.get("longest_outage_duration_s"),
            "after_outage_duration_s": after_entry.get("longest_outage_duration_s"),
            "threshold_m": after_entry.get("threshold_m"),
            "before_exceeds_threshold": before_entry.get("exceeds_threshold"),
            "after_exceeds_threshold": after_entry.get("exceeds_threshold"),
            "became_acceptable": became_acceptable,
        })
    acceptable_gain = bool(
        (before_acceptance or {}).get("acceptable") is False
        and (after_acceptance or {}).get("acceptable") is True
    )
    before_longest = float((before_acceptance or {}).get("longest_deficit_m") or 0.0)
    after_longest = float((after_acceptance or {}).get("longest_deficit_m") or 0.0)
    return {
        "semantics": CONTINUOUS_SERVICE_GAIN_SEMANTICS,
        "threshold_available": bool((after_acceptance or {}).get("threshold_available")),
        "threshold_m": (after_acceptance or {}).get("threshold_m"),
        "route_speed_mps": (after_acceptance or {}).get("route_speed_mps"),
        "before_acceptable": (before_acceptance or {}).get("acceptable"),
        "after_acceptable": (after_acceptance or {}).get("acceptable"),
        "before_longest_deficit_m": before_longest,
        "after_longest_deficit_m": after_longest,
        "longest_outage_reduction_m": max(0.0, before_longest - after_longest),
        "longest_outage_reduction_s": max(
            0.0,
            float((before_acceptance or {}).get("longest_outage_duration_s") or 0.0)
            - float((after_acceptance or {}).get("longest_outage_duration_s") or 0.0),
        ),
        "total_projection_reduction_m": total,
        "threshold_crossed": acceptable_gain,
        "acceptability_improved": bool(
            (before_acceptance or {}).get("acceptable") is False
            and (after_acceptance or {}).get("acceptable") is True
        ),
        "per_subsystem": per_subsystem,
    }


def _continuous_residual_segments(assessment, threshold_m):
    """投影态里仍然超过阈值的连续缺口段（可读诊断用；不改任何判定）。"""

    segments = []
    for route in (assessment or {}).get("routes") or []:
        for item in route.get("subsystems") or []:
            for segment in item.get("continuous_deficit_segments") or []:
                length = float(segment.get("length_m") or 0.0)
                segments.append({
                    "route_id": route.get("route_id"),
                    "subsystem": item.get("subsystem"),
                    "start_chainage_m": float(segment.get("start_route_offset_m") or 0.0),
                    "end_chainage_m": float(segment.get("end_route_offset_m") or 0.0),
                    "length_m": length,
                    "causes": list(segment.get("causes") or []),
                    "exceeds_threshold": (
                        None if threshold_m is None else bool(length > float(threshold_m) + 1e-9)
                    ),
                })
    return segments


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
    if action.get("planner_family") == ENDPOINT_INTEGRITY_MONITOR_PLANNER_FAMILY:
        #: Round 29-G：endpoint 完整性监测 action 同理 —— 它是 caller-owned 的**假想
        #: endpoint provider**，只经
        #: :func:`...navigation_integrity_monitoring.hypothetical_endpoint_monitor_evidence`
        #: 更新 endpoint 证据。绝不能被当作普通走廊 radio device 写进 facilities
        #: （那会错误地影响 corridor C/RID/Radar 几何判定）。
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
    #: Round 2.8 幂等性：连续服务迭代会反复对同一 facilities 集合应用动作，同一 device
    #: **绝不**因为重复应用而被计入两次（否则独立站址数会被自己虚增）。
    if not any(str(item.get("device_id")) == str(installed["device_id"]) for item in facility.get("devices") or []):
        facility.setdefault("devices", []).append(installed)
    collection["count"] = len(collection["items"])
    return collection
