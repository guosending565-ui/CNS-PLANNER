"""Pure deterministic ranking/assembly for P16 cumulative what-if planning."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ..domain.corridor_site_planning import empty_cns_corridor_site_plan


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


def _voxel_entries(assessment):
    output = {}
    for route in (assessment or {}).get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for voxel in route.get("voxels") or []:
            for item in voxel.get("subsystems") or []:
                output[f"{route_id}|{voxel.get('voxel_id')}|{item.get('subsystem')}"] = item
    return output


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
