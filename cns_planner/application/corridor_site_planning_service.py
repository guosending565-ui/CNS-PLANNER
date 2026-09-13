"""P16 cumulative corridor what-if orchestration (proposal only)."""

from __future__ import annotations

from copy import deepcopy

from ..catalogs import AircraftCNSProfileCatalog
from ..domain.corridor_site_planning import normalize_corridor_site_planning_policy
from ..domain.site_planning import REUSE_TIERS
from .site_planning_service import _candidate_actions


class CorridorSitePlanningService:
    def __init__(self, session, planner, corridor_model, corridor_gap_analyzer,
                 invalidation, snapshot):
        self.session, self.planner = session, planner
        self.corridor_model, self.corridor_gap_analyzer = corridor_model, corridor_gap_analyzer
        self.invalidation, self.snapshot = invalidation, snapshot

    def result_snapshot(self):
        return deepcopy(self.session.state.get("cns_corridor_site_plan") or self.planner.empty())

    def evaluate(self, payload=None):
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
        actions = _candidate_actions(
            targets, state.get("existing_cns_facilities") or {},
            state.get("candidate_sites") or {}, state.get("device_catalog") or {},
        )
        policy = state.get("corridor_site_planning_policy") or normalize_corridor_site_planning_policy()
        selected, trace, all_impacts = [], [], []
        facilities = deepcopy(state.get("existing_cns_facilities") or {})
        current_corridor, current_gap = deepcopy(baseline_corridor), deepcopy(baseline_gap)
        selected_ids = set()
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
                    )
                    evaluated.append(impact)
                    all_impacts.append(deepcopy(impact))
                ranked = self.planner.rank(tier_actions, evaluated)
                if not ranked:
                    break
                winner = ranked[0]
                action, impact = winner["action"], winner["impact"]
                facilities = _apply_cumulative_action(facilities, action)
                current_corridor, current_gap = self._rerun(facilities)
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
        for action in selected:
            final_facilities = _apply_cumulative_action(final_facilities, action)
        final_corridor, final_gap = (
            self._rerun(final_facilities) if selected
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
        state["cns_corridor_site_plan"] = result
        state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = (
            "passed" if result.get("status") == "proposal_ready" else "missing_data"
        )
        self.session.save()
        return self.snapshot()

    def _save_missing(self, reason):
        result = self.planner.empty("missing_data")
        result["reasons"] = [reason]
        self.session.state["cns_corridor_site_plan"] = result
        self.session.state.setdefault("result_statuses", {})["cns_corridor_site_plan"] = "missing_data"
        self.session.save()
        return self.snapshot()

    def _what_if(self, action, facilities, before_gap, targets, iteration):
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
        after_corridor, after_gap = self._rerun(_apply_cumulative_action(facilities, action))
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

    def _rerun(self, facilities):
        state = self.session.state
        profile = AircraftCNSProfileCatalog.find(
            state.get("aircraft_profiles") or {}, state.get("selected_aircraft_profile_id") or "",
        )
        selections = state.get("algorithm_selection") or {}
        corridor_model = self.corridor_model.__class__(
            (state.get("cns_corridor_assessment") or {}).get("parameters")
            or getattr(self.corridor_model, "parameters", {})
        )
        corridor = corridor_model.evaluate(
            state.get("operational_routes") or [], state.get("spatial_3d") or {},
            state.get("grid") or {}, state.get("grid_attributes") or {},
            state.get("required_cns") or {}, profile, facilities,
            state.get("device_catalog") or {}, state.get("cns_corridor_policy") or {},
            coverage_parameters=((selections.get("coverage_model") or {}).get("parameters") or {}),
            capability_parameters=((selections.get("service_model") or {}).get("parameters") or {}),
        )
        analyzer = self.corridor_gap_analyzer.__class__(
            (state.get("cns_corridor_gap_assessment") or {}).get("parameters")
            or getattr(self.corridor_gap_analyzer, "parameters", {})
        )
        gap = analyzer.evaluate(
            corridor, state.get("required_cns") or {},
            state.get("cns_planning_objectives") or {},
        )
        return corridor, gap


def _targets(assessment):
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
                target_id = f"{route_id}|{voxel_id}|{code}"
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


def _confirmed_units(entry, required_units):
    if entry.get("combined_status") == "unknown":
        return None
    if required_units <= 1:
        return min(1, int(entry.get("qualified_provider_count") or 0))
    value = entry.get("confirmed_independent_provider_count")
    return None if value is None else min(required_units, int(value))


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
        before_units = _confirmed_units(left, target["required_units"])
        after_units = _confirmed_units(right, target["required_units"])
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
    result = {}
    for route in assessment.get("routes") or []:
        route_id = str(route.get("route_id") or "")
        for voxel in route.get("voxels") or []:
            for entry in voxel.get("subsystems") or []:
                result[f"{route_id}|{voxel.get('voxel_id')}|{entry.get('subsystem')}"] = entry
    return result


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
    collection = deepcopy(existing or {})
    collection.setdefault("items", [])
    installed = {
        "device_id": action["device_id"], "subsystem": action["subsystem"],
        "status": "active", "service_model": {"status": "missing_data"},
        "metadata": {"hypothetical_action_id": action["action_id"], "p16_proposal_only": True},
    }
    if action.get("facility_id"):
        facility = next(item for item in collection["items"] if str(item.get("facility_id")) == str(action["facility_id"]))
    else:
        proposal_id = f"p16-proposal:{action.get('site_id')}"
        facility = next((item for item in collection["items"] if item.get("facility_id") == proposal_id), None)
        if facility is None:
            facility = {
                "facility_id": proposal_id, "site_id": action.get("site_id"),
                "name": f"P16 Proposal {action.get('site_id')}",
                "coordinate": deepcopy(action.get("coordinate")),
                "vertical_profile": deepcopy(action.get("vertical")),
                "devices": [], "status": "active", "source": "P16 cumulative hypothetical what-if",
                "metadata": {"proposal_only": True, "source_candidate_site_id": action.get("site_id")},
            }
            collection["items"].append(facility)
    if not any(str(item.get("device_id")) == str(installed["device_id"]) for item in facility.get("devices") or []):
        facility.setdefault("devices", []).append(installed)
    collection["count"] = len(collection["items"])
    return collection
