"""Deterministic reuse-first weighted greedy set-cover proposal baseline."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ..domain.site_planning import REUSE_TIERS


class ReuseFirstSitePlannerV1:
    algorithm_id = "reuse_first_site_planner_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        self.parameters = deepcopy(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status,
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "parameters": {},
            "proposal_only": True,
            "requires_closed_loop_validation": True,
            "input_fingerprint": None,
            "target_segment_count": 0,
            "target_planning_gap_length_m": 0.0,
            "resolved_planning_gap_length_m": 0.0,
            "remaining_planning_gap_length_m": 0.0,
            "candidate_actions": [],
            "selected_actions": [],
            "unresolved_segments": [],
            "cost_summary": {
                "cost_semantics": "action_count_proxy_no_currency",
                "selected_action_count": 0,
                "explicit_costs_by_unit": {},
            },
        }

    def plan(self, targets, actions, impacts, policy):
        targets = deepcopy(targets or [])
        actions = deepcopy(actions or [])
        impacts = deepcopy(impacts or [])
        impact_by_action = {str(item.get("action_id")): item for item in impacts}
        action_by_id = {str(item.get("action_id")): item for item in actions}
        target_by_id = {str(item.get("segment_id")): item for item in targets}
        covered = {identifier: [] for identifier in target_by_id}
        selected = []

        for reuse_class in REUSE_TIERS:
            tier_ids = sorted(
                action_id for action_id, action in action_by_id.items()
                if action.get("reuse_class") == reuse_class
            )
            while True:
                ranked = []
                for action_id in tier_ids:
                    if action_id in {item["action_id"] for item in selected}:
                        continue
                    action = action_by_id[action_id]
                    impact = impact_by_action.get(action_id) or {}
                    marginal, intervals = _marginal_gain(impact, covered, target_by_id)
                    if action.get("eligibility", {}).get("status") != "eligible" or impact.get("status") != "eligible" or marginal <= 0:
                        continue
                    cost, cost_unit = _confirmed_cost(action)
                    score = marginal / cost if cost is not None and cost > 0 else marginal
                    ranked.append((
                        -score, -marginal, cost if cost is not None else float("inf"),
                        action_id, action, impact, intervals, score, marginal,
                    ))
                if not ranked:
                    break
                _, _, _, action_id, action, impact, intervals, score, marginal = min(ranked)
                for segment_id, values in intervals.items():
                    covered[segment_id] = _union_intervals([*covered[segment_id], *values])
                cost, cost_unit = _confirmed_cost(action)
                selected.append({
                    **deepcopy(action),
                    "impact": deepcopy(impact),
                    "marginal_planning_gap_reduction_m": marginal,
                    "selection_score": score,
                    "score_semantics": "marginal_gap_per_explicit_cost" if cost is not None else "marginal_gap_per_action_count_proxy",
                    "explicit_cost": cost,
                    "cost_unit": cost_unit,
                })

        target_length = sum(float(item.get("length_m") or 0.0) for item in targets)
        resolved_by_segment = {
            segment_id: _interval_length(intervals)
            for segment_id, intervals in covered.items()
        }
        resolved_length = sum(resolved_by_segment.values())
        unresolved = []
        for segment_id, target in target_by_id.items():
            remaining = max(0.0, float(target.get("length_m") or 0.0) - resolved_by_segment[segment_id])
            if remaining <= 1e-7:
                continue
            reasons = list(target.get("target_reasons") or [])
            joint = bool(target.get("requires_joint_optimization"))
            if joint:
                reasons.append("min_redundancy/独立性可能需要联合动作；P11 不推断独立冗余")
            if not reasons:
                reasons.append("没有单个 eligible action 产生正的 confirmed planning-gap reduction")
            unresolved.append({
                "segment_id": segment_id,
                "route_id": target.get("route_id"),
                "subsystem": target.get("subsystem"),
                "remaining_length_m": remaining,
                "reasons": list(dict.fromkeys(reasons)),
                "requires_joint_optimization": joint,
            })
        cost_summary = _cost_summary(selected)
        counts = {tier: sum(item.get("reuse_class") == tier for item in selected) for tier in REUSE_TIERS}
        fingerprint = sha256(json.dumps(
            [targets, actions, impacts, policy or {}, self.parameters],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        return {
            "status": "proposal_ready" if selected else "no_eligible_proposal",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters),
            "proposal_only": True,
            "requires_closed_loop_validation": True,
            "planning_policy": deepcopy(policy or {}),
            "input_fingerprint": fingerprint,
            "target_segment_count": len(targets),
            "target_planning_gap_length_m": target_length,
            "resolved_planning_gap_length_m": resolved_length,
            "remaining_planning_gap_length_m": max(0.0, target_length - resolved_length),
            "candidate_actions": actions,
            "candidate_impacts": impacts,
            "selected_actions": selected,
            "unresolved_segments": unresolved,
            "requires_joint_optimization": any(item["requires_joint_optimization"] for item in unresolved),
            "reuse_counts": counts,
            "existing_reuse_count": counts["existing_cns_facility"],
            "shared_site_reuse_count": counts["existing_shared_site"],
            "candidate_site_count": counts["candidate_site"],
            "new_build_count": counts["new_build_candidate"],
            "cost_summary": cost_summary,
            "target_weight_semantics": "confirmed_planning_gap_length_m_only",
            "risk_population_severity_weighting": "not_evaluated",
        }


def _marginal_gain(impact, covered, targets):
    intervals = {}
    for resolution in impact.get("segment_resolutions") or []:
        segment_id = str(resolution.get("segment_id") or "")
        if segment_id not in targets:
            continue
        proposed = _union_intervals(resolution.get("resolved_intervals") or [])
        remaining = _subtract_intervals(proposed, covered.get(segment_id) or [])
        if remaining:
            intervals[segment_id] = remaining
    return sum(_interval_length(values) for values in intervals.values()), intervals


def _confirmed_cost(action):
    profile = action.get("planning_profile") or {}
    cost = profile.get("planning_cost")
    if profile.get("confirmed") is not True or cost is None:
        return None, None
    return float(cost), profile.get("cost_unit")


def _cost_summary(selected):
    explicit = {}
    for action in selected:
        if action.get("explicit_cost") is None:
            continue
        unit = str(action.get("cost_unit") or "unknown_unit")
        explicit[unit] = explicit.get(unit, 0.0) + float(action["explicit_cost"])
    return {
        "cost_semantics": (
            "explicit_confirmed_cost_with_action_count_proxy_for_missing"
            if explicit else "action_count_proxy_no_currency"
        ),
        "selected_action_count": len(selected),
        "explicit_costs_by_unit": explicit,
        "currency_total": None,
    }


def _union_intervals(intervals):
    values = sorted(
        (float(item[0]), float(item[1]))
        for item in intervals if len(item) >= 2 and float(item[1]) > float(item[0])
    )
    result = []
    for start, end in values:
        if result and start <= result[-1][1] + 1e-7:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _subtract_intervals(proposed, covered):
    remaining = _union_intervals(proposed)
    for cut_start, cut_end in _union_intervals(covered):
        next_values = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                next_values.append([start, end])
                continue
            if cut_start > start:
                next_values.append([start, min(cut_start, end)])
            if cut_end < end:
                next_values.append([max(cut_end, start), end])
        remaining = next_values
    return remaining


def _interval_length(intervals):
    return sum(end - start for start, end in _union_intervals(intervals))

