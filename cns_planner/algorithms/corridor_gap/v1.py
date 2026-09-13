"""P15 spatial service, redundancy and explicit-objective assessment."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from ..service_capability.v1 import (
    NON_SITE_NAVIGATION, confirmed_independent_provider_count,
)
from ...domain.cns_planning_objectives import (
    OBJECTIVE_NAMES, empty_cns_corridor_gap_assessment, planning_not_evaluated,
)


class CNSCorridorGapAnalyzerV1:
    algorithm_id = "cns_corridor_gap_v1"
    algorithm_version = "1.0"
    model_scope = "spatial_corridor_service_redundancy_and_objectives"
    continuity_semantics = "conservative_longitudinal_projection_of_corridor_voxel_deficits"

    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return empty_cns_corridor_gap_assessment(status)

    def evaluate(self, corridor_assessment, required_cns, planning_objectives):
        corridor_status = (corridor_assessment or {}).get("status")
        if corridor_status in (None, "not_calculated", "stale", "missing_data"):
            result = self.empty("missing_data")
            result.update({
                "parameters": deepcopy(self.parameters),
                "reasons": ["P14 cns_corridor_assessment 必须是 current 且可评估"],
                "input_status": {"cns_corridor_assessment": corridor_status or "missing_data"},
            })
            return result
        routes = []
        objective_routes = (planning_objectives or {}).get("routes") or {}
        for corridor_route in (corridor_assessment or {}).get("routes") or []:
            route_id = str(corridor_route.get("route_id") or "")
            requirements = ((required_cns or {}).get("route_overrides") or {}).get(route_id) or (required_cns or {}).get("project_default") or {}
            objective_config = ((objective_routes.get(route_id) or {}).get("subsystems") or {})
            routes.append(self._route(corridor_route, requirements, objective_config))
        fingerprint = _fingerprint({
            "corridor_assessment": corridor_assessment or {},
            "required_cns": required_cns or {},
            "planning_objectives": planning_objectives or {},
            "parameters": self.parameters,
        })
        target_ids = sorted({
            voxel_id for route in routes for subsystem in route.get("subsystems") or []
            for voxel_id in subsystem.get("confirmed_target_voxel_ids") or []
        })
        unknown_ids = sorted({
            voxel_id for route in routes for subsystem in route.get("subsystems") or []
            for voxel_id in subsystem.get("unknown_voxel_ids") or []
        })
        return {
            "status": _aggregate([route["status"] for route in routes]),
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "model_scope": self.model_scope, "parameters": deepcopy(self.parameters),
            "continuity_semantics": self.continuity_semantics,
            "formal_continuity_probability": "not_evaluated",
            "input_fingerprint": fingerprint, "route_count": len(routes), "routes": routes,
            "confirmed_target_voxel_ids": target_ids, "unknown_voxel_ids": unknown_ids,
            "input_provenance": {
                "corridor_algorithm_id": (corridor_assessment or {}).get("algorithm_id"),
                "corridor_algorithm_version": (corridor_assessment or {}).get("algorithm_version"),
                "corridor_input_fingerprint": (corridor_assessment or {}).get("input_fingerprint"),
            },
            "not_evaluated": planning_not_evaluated(),
        }

    def _route(self, route, requirements, objective_config):
        route_id = str(route.get("route_id") or "")
        route_length = float(route.get("route_length_m") or 0.0)
        assessed_voxels = []
        for voxel in route.get("voxels") or []:
            entries = []
            for code, name in (("C", "communication"), ("N", "navigation"), ("S", "surveillance")):
                source = next((item for item in voxel.get("subsystems") or [] if item.get("subsystem") == code), {})
                entries.append(_evaluate_voxel_subsystem(code, source, requirements.get(name) or {}))
            assessed_voxels.append({
                "voxel_id": voxel.get("voxel_id"), "grid_id": voxel.get("grid_id"),
                "altitude_layer_id": voxel.get("altitude_layer_id"),
                "nearest_route_offset_m": voxel.get("nearest_route_offset_m"),
                "cell_half_diagonal_m": voxel.get("cell_half_diagonal_m"),
                "discretized_volume_proxy_m3": voxel.get("discretized_volume_proxy_m3"),
                "subsystems": entries,
            })
        subsystems = [
            _summarize_subsystem(
                route_id, code, route_length, assessed_voxels,
                ((objective_config.get(code) or {}).get("objectives") or {}),
            )
            for code in ("C", "N", "S")
        ]
        return {
            "route_id": route_id, "route_length_m": route_length,
            "status": _aggregate([item["status"] for item in subsystems]),
            "voxel_count": len(assessed_voxels), "voxels": assessed_voxels,
            "subsystems": subsystems,
        }


def _evaluate_voxel_subsystem(code, source, required):
    service_status = str(source.get("planning_status") or "unknown")
    evaluations = deepcopy(source.get("provider_evaluations") or [])
    # P8 may retain provider_type_compatibility gates when the later aircraft or
    # service-model evaluation cannot run.  Those gates are evidence about type
    # compatibility, not provider service evaluations, and must never increase
    # P15 redundancy.
    qualified = [
        item for item in evaluations
        if item.get("status") == "meets_under_model"
        and item.get("stage") != "provider_type_compatibility"
    ]
    qualified_count = len(qualified)
    independent_count = confirmed_independent_provider_count(qualified)
    required_flag = required.get("required")
    required_redundancy = ((required.get("performance") or {}).get("min_redundancy")
                           or required.get("redundancy"))
    non_site_navigation = code == "N" and (
        str(((required.get("type") or {}).get("technology") or "")).lower() in NON_SITE_NAVIGATION
        or any(item.get("kind") == "aircraft_navigation" for item in source.get("evidence") or [])
    )
    reasons, evidence = [], [{
        "kind": "p14_service_evidence", "p8_status": source.get("p8_status"),
        "provider_evaluations": evaluations, "source_reasons": deepcopy(source.get("reasons") or []),
        "source_evidence": deepcopy(source.get("evidence") or []),
    }]
    if required_flag is False or service_status == "not_applicable":
        redundancy_status = "not_applicable"
        ground_status = "not_applicable"
    elif non_site_navigation:
        redundancy_status = "not_applicable"
        ground_status = "not_applicable_to_site_provider_redundancy"
        independent_count = None
        reasons.append("非站基导航保持 P14 服务语义，不评估地面 provider 冗余")
    elif required_flag is not True or required_redundancy is None:
        redundancy_status = "unknown"
        ground_status = "unknown"
        reasons.append("RequiredCNS min_redundancy 未确认")
    else:
        required_redundancy = int(required_redundancy)
        complete_fail_evidence = bool(evaluations) and all(
            item.get("status") == "does_not_meet_under_model" for item in evaluations
        )
        if required_redundancy <= 1:
            if qualified_count >= 1:
                redundancy_status = "satisfied"
            elif service_status == "confirmed_deficit" or complete_fail_evidence:
                redundancy_status = "confirmed_deficit"
            else:
                redundancy_status = "unknown"
        elif qualified_count == 0:
            redundancy_status = "confirmed_deficit" if service_status == "confirmed_deficit" or complete_fail_evidence else "unknown"
        elif independent_count is None:
            redundancy_status = "unknown"
        else:
            redundancy_status = "satisfied" if independent_count >= required_redundancy else "confirmed_deficit"
        ground_status = redundancy_status
        if redundancy_status == "confirmed_deficit":
            reasons.append("已确认的合格独立 provider 数量低于 RequiredCNS")
        elif redundancy_status == "unknown":
            reasons.append("provider 独立性或合格性证据不足")
    causes = []
    if service_status == "confirmed_deficit":
        causes.append("service_deficit")
    elif service_status == "unknown":
        causes.append("unknown_service_evidence")
    if redundancy_status == "confirmed_deficit":
        causes.append("redundancy_deficit")
    elif redundancy_status == "unknown":
        causes.append("unknown_redundancy_evidence")
    if service_status == "not_applicable":
        combined = "not_applicable"
    elif "service_deficit" in causes or "redundancy_deficit" in causes:
        combined = "confirmed_gap"
    elif "unknown_service_evidence" in causes or "unknown_redundancy_evidence" in causes:
        combined = "unknown"
    else:
        combined = "satisfied"
    return {
        "subsystem": code, "service_status": service_status,
        "redundancy_status": redundancy_status,
        "ground_provider_redundancy_status": ground_status,
        "qualified_provider_count": qualified_count,
        "confirmed_independent_provider_count": independent_count,
        "required_redundancy": required_redundancy,
        "combined_status": combined, "causes": causes,
        "reasons": [*deepcopy(source.get("reasons") or []), *reasons],
        "evidence": evidence,
    }


def _summarize_subsystem(route_id, code, route_length, voxels, objective_config):
    selected = [
        (voxel, next(item for item in voxel["subsystems"] if item["subsystem"] == code))
        for voxel in voxels
    ]
    applicable = [(voxel, item) for voxel, item in selected if item["combined_status"] != "not_applicable"]
    service_classes = ("satisfied", "confirmed_deficit", "unknown")
    redundancy_classes = ("satisfied", "confirmed_deficit", "unknown", "not_applicable")
    combined_classes = ("satisfied", "confirmed_gap", "unknown")
    service = _distribution(applicable, "service_status", service_classes)
    redundancy = _distribution(applicable, "redundancy_status", redundancy_classes)
    combined = _distribution(applicable, "combined_status", combined_classes)
    raw_intervals = []
    for voxel, item in applicable:
        if item["combined_status"] != "confirmed_gap":
            continue
        offset, half = voxel.get("nearest_route_offset_m"), voxel.get("cell_half_diagonal_m")
        if offset is None or half is None:
            continue
        raw_intervals.append({
            "start_m": max(0.0, float(offset) - float(half)),
            "end_m": min(route_length, float(offset) + float(half)),
            "causes": sorted(cause for cause in item["causes"] if cause in ("service_deficit", "redundancy_deficit")),
            "voxel_ids": [voxel["voxel_id"]],
        })
    segments = _merge_by_cause(route_id, code, raw_intervals)
    union = _merge_union(raw_intervals)
    total_projection = sum(item[1] - item[0] for item in union)
    max_projection = max((item[1] - item[0] for item in union), default=0.0)
    redundancy_not_applicable = bool(applicable) and all(
        item["redundancy_status"] == "not_applicable" for _, item in applicable
    )
    actuals = {
        "min_satisfied_volume_fraction": service["fractions"]["satisfied"],
        "max_confirmed_deficit_volume_fraction": combined["fractions"]["confirmed_gap"],
        "max_unknown_volume_fraction": combined["fractions"]["unknown"],
        "min_redundancy_satisfied_volume_fraction": None if redundancy_not_applicable else redundancy["fractions"]["satisfied"],
        "max_continuous_deficit_projection_m": max_projection,
    }
    not_applicable = bool(selected) and not applicable
    objective_results, objective_status = _evaluate_objectives(
        objective_config, actuals, not_applicable,
        {"min_redundancy_satisfied_volume_fraction"} if redundancy_not_applicable else set(),
    )
    confirmed_ids = [voxel["voxel_id"] for voxel, item in applicable if item["combined_status"] == "confirmed_gap"]
    unknown_ids = [voxel["voxel_id"] for voxel, item in applicable if item["combined_status"] == "unknown"]
    causes = sorted({cause for _, item in applicable for cause in item["causes"]})
    if objective_status == "objectives_not_met":
        causes.append("planning_objective_gap")
    if not selected:
        status = "pending_confirmation"
    elif not_applicable:
        status = "not_applicable"
    elif confirmed_ids or objective_status == "objectives_not_met":
        status = "failed"
    elif unknown_ids or objective_status == "objectives_unknown":
        status = "pending_confirmation"
    else:
        status = "passed"
    return {
        "subsystem": code, "status": status,
        "required_voxel_count": len(applicable),
        "required_volume_proxy_m3": combined["required_volume_proxy_m3"],
        "service": service, "p14_service": deepcopy(service),
        "redundancy": redundancy, "combined": combined,
        "continuous_deficit_semantics": CNSCorridorGapAnalyzerV1.continuity_semantics,
        "continuous_deficit_segments": segments,
        "total_confirmed_deficit_projection_m": total_projection,
        "max_continuous_deficit_projection_m": max_projection,
        "objective_status": objective_status, "objective_results": objective_results,
        "confirmed_target_voxel_ids": confirmed_ids, "unknown_voxel_ids": unknown_ids,
        "causes": causes,
    }


def _distribution(items, field, classes):
    counts = {name: sum(value.get(field) == name for _, value in items) for name in classes}
    volumes = {
        name: sum(float(voxel.get("discretized_volume_proxy_m3") or 0.0) for voxel, value in items if value.get(field) == name)
        for name in classes
    }
    all_known = all(voxel.get("discretized_volume_proxy_m3") is not None for voxel, _ in items)
    required = sum(volumes.values()) if all_known else None
    return {
        "voxel_counts": counts, "volume_proxy_m3": volumes,
        "fractions": {name: volumes[name] / required if required not in (None, 0) else None for name in classes},
        "required_volume_proxy_m3": required,
        "known_required_volume_proxy_m3": sum(volumes.values()),
    }


def _merge_by_cause(route_id, code, intervals):
    ordered = sorted(intervals, key=lambda item: (tuple(item["causes"]), item["start_m"], item["end_m"], item["voxel_ids"]))
    groups = []
    for item in ordered:
        key = tuple(item["causes"])
        if groups and groups[-1]["cause_key"] == key and item["start_m"] <= groups[-1]["end_m"] + 1e-9:
            groups[-1]["end_m"] = max(groups[-1]["end_m"], item["end_m"])
            groups[-1]["voxel_ids"] = sorted(set(groups[-1]["voxel_ids"] + item["voxel_ids"]))
        else:
            groups.append({**deepcopy(item), "cause_key": key})
    groups.sort(key=lambda item: (item["start_m"], item["end_m"], item["cause_key"]))
    return [{
        "segment_id": f"{route_id}:{code}:spatial-deficit:{index + 1}",
        "route_id": route_id, "subsystem": code,
        "start_route_offset_m": item["start_m"], "end_route_offset_m": item["end_m"],
        "length_m": item["end_m"] - item["start_m"],
        "causes": list(item["cause_key"]), "voxel_ids": item["voxel_ids"],
        "semantics": CNSCorridorGapAnalyzerV1.continuity_semantics,
    } for index, item in enumerate(groups)]


def _merge_union(intervals):
    result = []
    for item in sorted(intervals, key=lambda value: (value["start_m"], value["end_m"])):
        if result and item["start_m"] <= result[-1][1] + 1e-9:
            result[-1][1] = max(result[-1][1], item["end_m"])
        else:
            result.append([item["start_m"], item["end_m"]])
    return result


def _evaluate_objectives(config, actuals, not_applicable, not_applicable_objectives=None):
    not_applicable_objectives = not_applicable_objectives or set()
    configured = [(name, config[name]) for name in OBJECTIVE_NAMES if name in config]
    confirmed = [(name, item) for name, item in configured if item.get("confirmed") is True and item.get("status") == "confirmed"]
    results = []
    for name, objective in configured:
        actual = actuals.get(name)
        if not_applicable or name in not_applicable_objectives:
            status = "not_applicable"
        elif objective.get("confirmed") is not True or objective.get("status") != "confirmed" or actual is None:
            status = "unknown"
        else:
            status = "met" if _compare(actual, objective["operator"], objective["value"]) else "not_met"
        results.append({
            "objective": name, "status": status, "actual": actual,
            "operator": objective.get("operator"), "target": objective.get("value"),
            "source": objective.get("source"), "confirmed": objective.get("confirmed") is True,
        })
    if not confirmed:
        return results, "objectives_not_configured"
    confirmed_results = [item for item in results if item["confirmed"]]
    if any(item["status"] == "not_met" for item in confirmed_results):
        return results, "objectives_not_met"
    if any(item["status"] == "unknown" for item in confirmed_results):
        return results, "objectives_unknown"
    if confirmed_results and all(item["status"] in ("met", "not_applicable") for item in confirmed_results):
        return results, "objectives_met" if not not_applicable else "not_applicable"
    return results, "objectives_unknown"


def _compare(actual, operator, target):
    return {
        ">=": actual >= target, "<=": actual <= target,
        ">": actual > target, "<": actual < target,
        "==": math.isclose(actual, target, abs_tol=1e-12),
    }[operator]


def _aggregate(statuses):
    if not statuses:
        return "missing_data"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status in ("pending_confirmation", "missing_data") for status in statuses):
        return "pending_confirmation"
    if all(status == "not_applicable" for status in statuses):
        return "not_applicable"
    return "passed"


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
