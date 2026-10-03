"""Pure deterministic ranking/assembly for P16 cumulative what-if planning."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json

from ..domain.corridor_site_planning import (
    corridor_voxel_entry_index, empty_cns_corridor_site_plan,
)


class CorridorReuseFirstSitePlannerV2:
    algorithm_id = "corridor_reuse_first_site_planner_v2"
    algorithm_version = "2.0"

    def __init__(self, parameters=None):
        self.parameters = deepcopy(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_cns_corridor_site_plan(status)

    def rank(self, actions, impacts):
        """Rank one reuse tier; application owns what-if execution and iteration."""

        candidates = []
        by_id = {str(action.get("action_id")): action for action in actions or []}
        for impact in impacts or []:
            action = by_id.get(str(impact.get("action_id")))
            gain = float(impact.get("confirmed_requirement_unit_volume_gain") or 0.0)
            if not action or action.get("eligibility", {}).get("status") != "eligible":
                continue
            if impact.get("status") != "eligible" or gain <= 0 or impact.get("regressions"):
                continue
            cost, unit = _confirmed_cost(action)
            candidates.append((action, impact, cost, unit))
        comparable = bool(candidates) and all(item[2] is not None and item[2] > 0 and item[3] for item in candidates)
        comparable = comparable and len({item[3] for item in candidates}) == 1
        ranked = []
        for action, impact, cost, unit in candidates:
            gain = float(impact.get("confirmed_requirement_unit_volume_gain") or 0.0)
            score = gain / cost if comparable else gain
            ranked.append({
                "action": deepcopy(action), "impact": deepcopy(impact),
                "selection_score": score,
                "score_semantics": "confirmed_unit_volume_gain_per_explicit_cost" if comparable else "confirmed_unit_volume_gain_per_action_count_proxy",
                "explicit_cost": cost, "cost_unit": unit,
            })
        ranked.sort(key=lambda item: (
            -int(item["impact"].get("newly_met_confirmed_objectives") or 0),
            -float(item["selection_score"]),
            -float(item["impact"].get("confirmed_requirement_unit_volume_gain") or 0.0),
            -float(item["impact"].get("max_continuous_deficit_projection_reduction_m") or 0.0),
            str(item["action"].get("action_id") or ""),
        ))
        return ranked

    def rank_continuous_service(self, actions, impacts):
        """Round 2.8：**连续服务**收益排名通道（离散增益为 0 时的第二通道）。

        为什么必须有它：真实项目 R0005 的 C 侧三个离散 objective 在投影态全部满足，
        于是所有"再加一座塔"的候选离散 unit 增益都是 0；但它们把连续缺口投影从
        143.54 m（9.57 s）直接降到 0 m，跨过用户显式登记的 3.0 s 阈值。旧排名通道只
        看离散增益，把这些真实收益静默丢弃，直接导致"P16 停止、P17 仍 unacceptable"。

        语义边界（绝不越界）：

        * 只在 ``status == eligible`` 且**连续缺口确实缩短**时进入候选；
        * 排序优先"跨越可接受阈值"，其次按最长中断缩减量、再按总投影缩减量；
        * 成本口径与 :meth:`rank` 完全一致（同 tier 内 gain / explicit cost，缺成本时
          退化为动作数代理），**绝不**因为换了收益维度就换一套成本假设。
        """

        candidates = []
        by_id = {str(action.get("action_id")): action for action in actions or []}
        for impact in impacts or []:
            action = by_id.get(str(impact.get("action_id")))
            if not action or action.get("eligibility", {}).get("status") != "eligible":
                continue
            if impact.get("status") != "eligible" or impact.get("regressions"):
                continue
            continuous = impact.get("continuous_service_gain") or {}
            reduction = float(continuous.get("longest_outage_reduction_m") or 0.0)
            total = float(continuous.get("total_projection_reduction_m") or 0.0)
            if reduction <= 0 and total <= 0:
                continue
            if continuous.get("threshold_available") is not True:
                #: 阈值不可判定时不得声称"跨越阈值"，但**真实缺口缩短**仍然是可确认收益。
                crossed = False
            else:
                crossed = continuous.get("threshold_crossed") is True
            cost, unit = _confirmed_cost(action)
            candidates.append((action, impact, cost, unit, crossed, reduction, total))
        comparable = bool(candidates) and all(
            item[2] is not None and item[2] > 0 and item[3] for item in candidates
        )
        comparable = comparable and len({item[3] for item in candidates}) == 1
        ranked = []
        for action, impact, cost, unit, crossed, reduction, total in candidates:
            score = reduction / cost if (comparable and cost) else reduction
            ranked.append({
                "action": deepcopy(action), "impact": deepcopy(impact),
                "selection_score": score,
                "score_semantics": (
                    "longest_continuous_service_outage_reduction_m_per_explicit_cost"
                    if comparable else
                    "longest_continuous_service_outage_reduction_m_per_action_count_proxy"
                ),
                "benefit_semantics": (
                    "longest_continuous_service_outage_reduction_m_same_projection_metric_as_p17"
                ),
                "threshold_crossed": crossed,
                "explicit_cost": cost, "cost_unit": unit,
            })
        ranked.sort(key=lambda item: (
            -int(bool(item.get("threshold_crossed"))),
            -float((item["impact"].get("continuous_service_gain") or {}).get("longest_outage_reduction_m") or 0.0),
            -float((item["impact"].get("continuous_service_gain") or {}).get("total_projection_reduction_m") or 0.0),
            -float(item["impact"].get("max_continuous_deficit_projection_reduction_m") or 0.0),
            str(item["action"].get("action_id") or ""),
        ))
        return ranked

    def assemble(self, *, baseline, final, policy, targets, actions, impacts,
                 selected, iteration_trace, unknown_evidence, consistency):
        before = _assessment_metrics(baseline)
        after = _assessment_metrics(final)
        gain = sum(float(item.get("marginal_confirmed_requirement_unit_volume_gain") or 0.0) for item in selected)
        reuse_counts = {
            tier: sum(item.get("reuse_class") == tier for item in selected)
            for tier in (policy or {}).get("reuse_tiers") or []
        }
        residual = _residual_targets(targets, final)
        unknown_residual = _residual_unknown_evidence(targets, final, selected)
        cost_summary = _cost_summary(selected)
        fingerprint_input = {
            "baseline_fingerprint": (baseline or {}).get("input_fingerprint"),
            "policy": policy or {}, "targets": targets or [], "actions": actions or [],
            "impacts": impacts or [], "parameters": self.parameters,
        }
        result = self.empty("proposal_ready" if selected else "no_eligible_proposal")
        result.update({
            "parameters": deepcopy(self.parameters), "planning_policy": deepcopy(policy or {}),
            "input_fingerprint": _fingerprint(fingerprint_input),
            "baseline_corridor_gap_fingerprint": (baseline or {}).get("input_fingerprint"),
            "target_voxel_count": len(targets or []), "targets": deepcopy(targets or []),
            "candidate_actions": deepcopy(actions or []), "candidate_impacts": deepcopy(impacts or []),
            "selected_actions": deepcopy(selected or []), "iteration_trace": deepcopy(iteration_trace or []),
            "confirmed_requirement_unit_volume_gain": gain,
            "benefit_semantics": "confirmed_requirement_unit_volume_gain_planning_proxy_not_risk_or_probability",
            "before": before, "after": after,
            "residual_confirmed_targets": residual,
            "residual_unknown_evidence": unknown_residual,
            #: Round 2.4：每个 P16 target 都必须能回答"为什么需要它"。按 canonical
            #: service 分组统计（required / 已确认缺口 / 仍缺证据 / 已选动作），
            #: 没有 ``service_key`` 的目标归入 ``legacy:<子系统>``，绝不混进正式服务口径。
            "target_service_groups": _target_service_groups(
                targets, residual, unknown_residual, selected,
            ),
            #: 即使一个动作都没选中，"为什么无法确认任何增益"也必须可读、可审计：
            #: 否则 no_eligible_proposal 只是一句无解释的结论。
            "candidate_unknown_reason_summary": _candidate_unknown_reason_summary(impacts),
            "unknown_evidence_required": deepcopy(unknown_evidence or []),
            "reuse_counts": reuse_counts, "cost_summary": cost_summary,
            "authoritative_combined_what_if_consistent": bool(consistency),
            "common_cause_or_shared_infrastructure_penalty": "not_evaluated",
        })
        return result


def _confirmed_cost(action):
    profile = action.get("planning_profile") or {}
    cost = profile.get("planning_cost")
    if profile.get("confirmed") is not True or cost is None:
        return None, None
    value = float(cost)
    return (value, str(profile.get("cost_unit") or "")) if value > 0 else (None, None)


def _cost_summary(selected):
    totals = {}
    for action in selected or []:
        if action.get("explicit_cost") is None or not action.get("cost_unit"):
            continue
        unit = str(action["cost_unit"])
        totals[unit] = totals.get(unit, 0.0) + float(action["explicit_cost"])
    return {
        "cost_semantics": "explicit_costs_grouped_by_unit_with_action_count_proxy",
        "selected_action_count": len(selected or []),
        "explicit_costs_by_unit": totals,
        "cross_unit_total": None,
    }


def _assessment_metrics(assessment):
    routes = []
    for route in (assessment or {}).get("routes") or []:
        routes.append({
            "route_id": route.get("route_id"),
            #: Round 2.2：unknown 体素与 confirmed target 体素的规模必须与 confirmed
            #: 结论并列披露，前端/报告才能显示"已确认改善 + 仍缺证据"。
            "confirmed_target_voxel_count": len(route.get("confirmed_target_voxel_ids") or []),
            "unknown_voxel_count": len(route.get("unknown_voxel_ids") or []),
            "subsystems": [{
                "subsystem": item.get("subsystem"),
                "service": deepcopy(item.get("service") or {}),
                "redundancy": deepcopy(item.get("redundancy") or {}),
                "combined": deepcopy(item.get("combined") or {}),
                "total_confirmed_deficit_projection_m": item.get("total_confirmed_deficit_projection_m"),
                "max_continuous_deficit_projection_m": item.get("max_continuous_deficit_projection_m"),
                "objective_status": item.get("objective_status"),
                "objective_results": deepcopy(item.get("objective_results") or []),
            } for item in route.get("subsystems") or []],
        })
    return {"status": (assessment or {}).get("status"), "routes": routes}


def _residual_targets(targets, final):
    entries = _voxel_entries(final)
    residual = []
    for target in targets or []:
        current = entries.get(target["target_id"])
        if not current or current.get("combined_status") == "confirmed_gap":
            residual.append({**deepcopy(target), "final_status": (current or {}).get("combined_status", "missing")})
    return residual


def _residual_unknown_evidence(targets, final, selected):
    """最终方案里仍然**证据不足**的 target（与 confirmed 残差分开、绝不合并）。

    两个来源分开计数、分开披露：

    * ``final_unknown_target_*`` —— 最终 what-if 走廊里 ``combined_status = unknown``
      或缺条目的 target；
    * ``selected_action_unknown_target_*`` —— 已选动作的 impact 中登记的 unknown target
      （这些 target 从未被当作 satisfied，也不会因为动作入选而消失）。

    ``semantics`` 明确写死"unknown 不得计入 satisfied / 冗余满足"，供报告与前端引用。
    """

    entries = _voxel_entries(final)
    final_unknown, final_missing = [], []
    for target in targets or []:
        current = entries.get(target["target_id"])
        if current is None:
            final_missing.append(target["target_id"])
        elif current.get("combined_status") == "unknown":
            final_unknown.append(target["target_id"])
    action_unknown = sorted({
        str(target_id)
        for action in selected or []
        for target_id in ((action.get("impact") or {}).get("unknown_targets") or [])
    })
    unknown_regressions = sorted({
        str(target_id)
        for action in selected or []
        for target_id in ((action.get("impact") or {}).get("unknown_regressions") or [])
    })
    selected_reason_counts = Counter()
    for action in selected or []:
        for reason, count in (((action.get("impact") or {}).get("unknown_reason_counts")) or {}).items():
            selected_reason_counts[str(reason)] += int(count or 0)
    return {
        "semantics": "unknown_evidence_is_disclosed_and_never_counted_as_satisfied_or_redundancy_met",
        "final_unknown_target_count": len(final_unknown),
        "final_unknown_target_ids": final_unknown,
        "final_missing_target_count": len(final_missing),
        "final_missing_target_ids": final_missing,
        "selected_action_unknown_target_count": len(action_unknown),
        "selected_action_unknown_target_ids": action_unknown,
        "selected_action_unknown_regression_count": len(unknown_regressions),
        "selected_action_unknown_regression_target_ids": unknown_regressions,
        "selected_action_unknown_reason_counts": dict(selected_reason_counts.most_common(8)),
    }


def _target_bucket(target):
    """target 的 canonical 分组键：显式 ``service_key``，否则 ``legacy:<子系统>``。"""

    key = str((target or {}).get("service_key") or "").strip()
    if key:
        return key
    return "legacy:" + str((target or {}).get("subsystem") or "?")


def _target_service_groups(targets, residual, unknown_residual, selected):
    """按 canonical service 分组的目标统计（只读聚合，供前端与报告分 service 展示）。

    * ``required``：该 service 的规划目标总数；
    * ``confirmed_gap``：最终仍然**已确认缺口**的目标数；
    * ``unknown``：最终仍然**证据不足**的目标数（绝不并入 confirmed 或 satisfied）；
    * ``selected``：已选动作中服务该 service 的动作数。
    """

    groups = {}

    def ensure(bucket):
        if bucket not in groups:
            is_legacy = bucket.startswith("legacy:")
            groups[bucket] = {
                #: 只有真正的 canonical service 才填 ``service_key``；
                #: ``legacy:<子系统>`` / ``unresolved:<target_id>`` 明确为 ``None``，
                #: 前端与报告据此绝不把 legacy 口径读成正式服务。
                "service_key": None if (is_legacy or bucket.startswith("unresolved:"))
                else bucket,
                "bucket": bucket,
                "legacy_subsystem": bucket.split(":", 1)[1] if is_legacy else None,
                "required": 0, "confirmed_gap": 0, "unknown": 0, "selected": 0,
            }
        return groups[bucket]

    by_target = {}
    for target in targets or []:
        bucket = _target_bucket(target)
        by_target[str(target.get("target_id"))] = bucket
        ensure(bucket)["required"] += 1
    for target in residual or []:
        ensure(_target_bucket(target))["confirmed_gap"] += 1
    for target_id in (unknown_residual or {}).get("final_unknown_target_ids") or []:
        #: 优先按 target 的 canonical service 归组；反查不到时**如实**归入
        #: ``unresolved:<target_id>``，绝不猜测所属服务。
        bucket = by_target.get(str(target_id)) or ("unresolved:" + str(target_id))
        ensure(bucket)["unknown"] += 1
    for action in selected or []:
        bucket = str(action.get("service_key") or "").strip()
        if not bucket:
            bucket = by_target.get(str(action.get("target_id") or ""), "")
        if not bucket:
            bucket = "legacy:" + str(action.get("subsystem") or "?")
        ensure(bucket)["selected"] += 1
    return [groups[key] for key in sorted(groups)]


def _candidate_unknown_reason_summary(impacts):
    """全部候选 what-if 的"无法确认增益"原因聚合（只读、供报告与前端解释）。"""

    counts = Counter()
    candidates_with_unknown = 0
    for impact in impacts or []:
        reasons = (impact or {}).get("unknown_reason_counts") or {}
        if reasons:
            candidates_with_unknown += 1
        for reason, count in reasons.items():
            counts[str(reason)] += int(count or 0)
    return {
        "semantics": (
            "why_candidate_actions_confirm_no_gain_aggregated_over_all_what_ifs_"
            "unknown_is_never_counted_as_satisfied"
        ),
        "candidates_with_unknown_targets": candidates_with_unknown,
        "reason_counts": dict(counts.most_common(8)),
    }


def _voxel_entries(assessment):
    """subsystem + service 两类 target_id → entry（与 P16 service 的索引同一契约）。"""

    return corridor_voxel_entry_index(assessment)


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
