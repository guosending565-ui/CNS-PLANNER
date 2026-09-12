"""Conservative 3D/performance/runtime-aware CNS gap analysis V2."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math

from .model_v2 import COMBINED_GAP_STATUSES, GAP_CAUSES, REMEDIATION_SCOPES


SUBSYSTEM_NAMES = {"C": "communication", "N": "navigation", "S": "surveillance"}
PLANNING_MAP = {
    "meets_under_model": "satisfied",
    "does_not_meet_under_model": "confirmed_gap",
    "unknown": "unknown",
    "unsupported_model": "unknown",
    "not_applicable": "not_applicable",
}
RUNTIME_MAP = {
    "available": "satisfied",
    "available_degraded": "satisfied",
    "contingency": "satisfied_by_contingency",
    "lost": "confirmed_gap",
    "unknown": "unknown",
    "not_applicable": "not_applicable",
}


class CNSGapAnalyzerV2:
    algorithm_id = "cns_gap_analysis_v2"
    algorithm_version = "2.0"

    def __init__(self, parameters=None):
        self.parameters = {
            "evaluate_protection_margin": False,
            **deepcopy(parameters or {}),
        }

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status,
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "parameters": {"evaluate_protection_margin": False},
            "model_scope": "3d_performance_runtime_gap_analysis",
            "semantics": "gap_is_not_safety_event",
            "input_fingerprint": None,
            "route_count": 0,
            "routes": [],
            "protection_margin_assessment": "not_evaluated",
            "safety_event_evaluation": "not_evaluated",
        }

    def analyze(
        self, required_cns, coverage_3d, cns_service_capability,
        service_timeline, protection_envelope=None, device_catalog=None,
    ):
        protection_enabled = self.parameters.get("evaluate_protection_margin") is True
        protection_input = protection_envelope or {} if protection_enabled else None
        device_input = device_catalog or {} if protection_enabled else None
        inputs = [
            required_cns or {}, coverage_3d or {}, cns_service_capability or {},
            service_timeline or {}, protection_input, device_input, self.parameters,
        ]
        fingerprint = sha256(json.dumps(
            inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()

        p7_routes = _route_index(coverage_3d)
        p8_routes = _route_index(cns_service_capability)
        p9_routes = _route_index(service_timeline)
        route_ids = list(dict.fromkeys([*p7_routes, *p8_routes, *p9_routes]))
        routes = [
            self._route(
                route_id, p7_routes.get(route_id) or {},
                p8_routes.get(route_id) or {}, p9_routes.get(route_id) or {},
                _route_requirements(required_cns, route_id),
                protection_envelope or {}, device_catalog or {},
            )
            for route_id in route_ids
        ]
        assessment_status = _aggregate_status([
            subsystem["status"]
            for route in routes for subsystem in route["subsystems"]
        ]) if routes else "missing_data"
        return {
            "status": assessment_status,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters),
            "model_scope": "3d_performance_runtime_gap_analysis",
            "semantics": "gap_is_not_safety_event",
            "input_fingerprint": fingerprint,
            "route_count": len(routes),
            "routes": routes,
            "protection_margin_assessment": (
                "engineering_only" if protection_enabled else "not_evaluated"
            ),
            "regulatory_well_clear": "not_evaluated",
            "formal_detection_volume_compliance": "not_evaluated",
            "safety_event_evaluation": "not_evaluated",
        }

    def _route(self, route_id, p7_route, p8_route, p9_route, requirements, protection, devices):
        route_length = _route_length(p7_route, p8_route, p9_route)
        duration = _number_or_none(p9_route.get("duration_s"))
        p7_subsystems = _subsystem_index(p7_route)
        p8_subsystems = _subsystem_index(p8_route)
        p9_subsystems = _subsystem_index(p9_route)
        subsystems = []
        for code, name in SUBSYSTEM_NAMES.items():
            subsystems.append(self._subsystem(
                route_id, code, route_length, duration,
                requirements.get(name) or {},
                p7_subsystems.get(code) or {}, p8_subsystems.get(code) or {},
                p9_subsystems.get(code) or {}, protection, devices,
            ))
        return {
            "route_id": route_id,
            "route_length_m": route_length,
            "duration_s": duration,
            "status": _aggregate_status([item["status"] for item in subsystems]),
            "subsystems": subsystems,
        }

    def _subsystem(
        self, route_id, code, route_length, route_duration, required,
        p7_subsystem, p8_subsystem, p9_subsystem, protection, devices,
    ):
        required_flag = required.get("required")
        p8_samples = _samples(p8_subsystem)
        p9_intervals = sorted(
            p9_subsystem.get("intervals") or [],
            key=lambda item: float(item.get("start_route_offset_m") or 0.0),
        )
        breakpoints = {0.0, route_length}
        breakpoints.update(_bounded(
            item.get("distance_along_route_m"), route_length
        ) for item in p8_samples)
        for item in p9_intervals:
            breakpoints.add(_bounded(item.get("start_route_offset_m"), route_length))
            breakpoints.add(_bounded(item.get("end_route_offset_m"), route_length))
        ordered = sorted(point for point in breakpoints if point is not None)
        elementary = []
        for start, end in zip(ordered, ordered[1:]):
            if end - start <= 1e-9:
                continue
            elementary.append(_build_segment(
                route_id, code, start, end, required_flag,
                p7_subsystem, p8_subsystem, p8_samples,
                p9_subsystem, p9_intervals, route_length, route_duration,
                protection, devices,
                self.parameters.get("evaluate_protection_margin") is True,
            ))
        if not elementary and route_length == 0:
            elementary = [_build_segment(
                route_id, code, 0.0, 0.0, required_flag,
                p7_subsystem, p8_subsystem, p8_samples,
                p9_subsystem, p9_intervals, route_length, route_duration,
                protection, devices,
                self.parameters.get("evaluate_protection_margin") is True,
            )]
        segments = _merge_segments(elementary)
        for index, item in enumerate(segments, 1):
            item["segment_id"] = f"{route_id}:{code}:{index:03d}"
        return _subsystem_summary(code, required_flag, route_length, route_duration, segments)


def _build_segment(
    route_id, code, start, end, required_flag, p7_subsystem,
    p8_subsystem, p8_samples, p9_subsystem, p9_intervals,
    route_length, route_duration, protection, devices, protection_enabled,
):
    midpoint = (start + end) / 2.0 if end > start else start
    planning, planning_evidence = _planning_at(p8_subsystem, p8_samples, midpoint)
    runtime, runtime_evidence, runtime_interval = _runtime_at(p9_subsystem, p9_intervals, midpoint)
    if required_flag is False:
        planning = runtime = "not_applicable"
    elif required_flag is not True:
        planning = runtime = "unknown"

    p7_evidence = _geometry_at(p7_subsystem, midpoint)
    causes = []
    reasons = []
    evidence = []
    if planning_evidence:
        reasons.extend(planning_evidence.get("reasons") or [])
        evidence.append({"kind": "planning_capability", **deepcopy(planning_evidence)})
    if runtime_evidence:
        reasons.extend(runtime_evidence.get("reasons") or [])
        evidence.append({"kind": "runtime_timeline", **deepcopy(runtime_evidence)})
    if p7_evidence:
        evidence.append({"kind": "geometric_coverage_3d", **deepcopy(p7_evidence)})

    if planning == "confirmed_gap":
        causes.append("geometry_gap" if p7_evidence and p7_evidence.get("covered") is False else "static_service_mismatch")
    if runtime == "confirmed_gap":
        causes.append("runtime_service_loss")

    protection_result = _protection_assessment(
        code, planning_evidence, protection, devices, protection_enabled,
    )
    if protection_result["status"] == "confirmed_gap":
        causes.append("protection_margin_gap")
    if planning == "unknown" or runtime == "unknown" or protection_result["status"] == "unknown":
        causes.append("unknown_evidence")
    causes = _ordered_unique(causes, GAP_CAUSES)
    evidence.append({"kind": "protection_margin", **protection_result})
    combined = _combine(planning, runtime, protection_result["status"])
    start_time, end_time = _segment_times(
        start, end, runtime_interval, route_length, route_duration,
    )
    duration = end_time - start_time if start_time is not None and end_time is not None else None
    contingency = runtime == "satisfied_by_contingency"
    return {
        "segment_id": None,
        "route_id": route_id,
        "subsystem": code,
        "start_route_offset_m": start,
        "end_route_offset_m": end,
        "length_m": max(0.0, end - start),
        "start_time_s": start_time,
        "end_time_s": end_time,
        "duration_s": duration,
        "planning_status": planning,
        "runtime_status": runtime,
        "combined_status": combined,
        "gap_causes": causes,
        "reasons": _unique_values(reasons),
        "evidence": evidence,
        "contingency_exposure": {
            "active": contingency,
            "length_m": max(0.0, end - start) if contingency else 0.0,
            "duration_s": duration if contingency else 0.0 if duration is not None else None,
        },
        "remediation_scope": _remediation_scope(causes, planning_evidence),
        "protection_margin": protection_result,
    }


def _planning_at(subsystem, samples, offset):
    if subsystem.get("status") == "not_applicable":
        return "not_applicable", {"status": "not_applicable", "reasons": subsystem.get("reasons") or []}
    if not samples:
        return "unknown", {"status": "unknown", "reasons": subsystem.get("reasons") or ["P8 capability sample 缺失"]}
    if len(samples) == 1:
        sample = samples[0]
        return PLANNING_MAP.get(sample.get("status"), "unknown"), _sample_evidence(sample)
    for left, right in zip(samples, samples[1:]):
        left_offset = float(left.get("distance_along_route_m") or 0.0)
        right_offset = float(right.get("distance_along_route_m") or 0.0)
        if left_offset - 1e-9 <= offset <= right_offset + 1e-9:
            left_status = PLANNING_MAP.get(left.get("status"), "unknown")
            right_status = PLANNING_MAP.get(right.get("status"), "unknown")
            evidence = {
                "status": left_status if left_status == right_status else "unknown",
                "source_sample_offsets_m": [left_offset, right_offset],
                "source_sample_statuses": [left.get("status"), right.get("status")],
                "reasons": _unique_values([*(left.get("reasons") or []), *(right.get("reasons") or [])]),
                "evidence": [*(deepcopy(left.get("evidence") or [])), *(deepcopy(right.get("evidence") or []))],
                "provider_evaluations": [*(deepcopy(left.get("provider_evaluations") or [])), *(deepcopy(right.get("provider_evaluations") or []))],
                "conservative_transition": left_status != right_status,
            }
            return (left_status if left_status == right_status else "unknown"), evidence
    return "unknown", {"status": "unknown", "reasons": ["区间不在 P8 相邻 sample 支持范围内"]}


def _runtime_at(subsystem, intervals, offset):
    if subsystem.get("status") == "not_applicable":
        return "not_applicable", {"service_state": "not_applicable", "reasons": []}, None
    for index, interval in enumerate(intervals):
        start = float(interval.get("start_route_offset_m") or 0.0)
        end = float(interval.get("end_route_offset_m") or 0.0)
        is_last = index == len(intervals) - 1
        if start - 1e-9 <= offset < end - 1e-9 or (is_last and start - 1e-9 <= offset <= end + 1e-9):
            state = RUNTIME_MAP.get(interval.get("service_state"), "unknown")
            return state, {
                "service_state": interval.get("service_state"),
                "scenario_event_id": interval.get("scenario_event_id"),
                "fallback_used": deepcopy(interval.get("fallback_used")),
                "reasons": deepcopy(interval.get("reasons") or []),
                "evidence": deepcopy(interval.get("evidence") or []),
            }, interval
    return "unknown", {"service_state": "unknown", "reasons": ["P9 runtime interval 缺失"]}, None


def _geometry_at(subsystem, offset):
    samples = _samples(subsystem)
    if not samples:
        return None
    selected = min(samples, key=lambda item: abs(float(item.get("distance_along_route_m") or 0.0) - offset))
    return {
        "distance_along_route_m": selected.get("distance_along_route_m"),
        "covered": selected.get("covered"),
        "providers": deepcopy(selected.get("providers") or []),
        "status": subsystem.get("status"),
        "model_scope": subsystem.get("model_scope", "geometric_only"),
    }


def _protection_assessment(code, planning_evidence, protection, devices, enabled):
    base = {
        "status": "not_evaluated",
        "model_scope": "engineering_only",
        "regulatory_well_clear": "not_evaluated",
        "formal_detection_volume_compliance": "not_evaluated",
        "available_detection_range_m": None,
        "d_protect_m": protection.get("d_protect_m") if isinstance(protection, dict) else None,
        "protection_margin_m": None,
        "reasons": [],
    }
    if not enabled or code != "S":
        return base
    if not isinstance(protection, dict) or protection.get("status") != "passed" or protection.get("d_protect_m") is None:
        return {**base, "status": "unknown", "reasons": ["P9 Protection Envelope 缺失、未通过或未确认"]}
    detection_range = _confirmed_detection_range(planning_evidence, devices)
    if detection_range is None:
        return {**base, "status": "unknown", "reasons": ["缺少 P8 命中 provider 的 confirmed 实际监视探测距离"]}
    margin = detection_range - float(protection["d_protect_m"])
    return {
        **base,
        "status": "confirmed_gap" if margin < 0 else "satisfied",
        "available_detection_range_m": detection_range,
        "protection_margin_m": margin,
        "reasons": ["工程保护距离大于已确认监视探测距离"] if margin < 0 else [],
    }


def _confirmed_detection_range(planning_evidence, catalog):
    devices = {
        str(item.get("device_id")): item
        for item in (catalog or {}).get("items") or [] if isinstance(item, dict)
    }
    values = []
    for provider in (planning_evidence or {}).get("provider_evaluations") or []:
        if provider.get("status") != "meets_under_model":
            continue
        direct = provider.get("available_detection_range_m", provider.get("detection_range_m"))
        if direct is not None and provider.get("detection_range_confirmed") is True:
            values.append(float(direct))
            continue
        device = devices.get(str(provider.get("device_id") or "")) or {}
        model = device.get("service_model") or {}
        if model.get("confirmed") is not True:
            continue
        performance = (model.get("parameters") or {}).get("performance") or device.get("performance") or {}
        value = performance.get("min_detection_range_m", performance.get("detection_range_m"))
        if value is not None and math.isfinite(float(value)) and float(value) >= 0:
            values.append(float(value))
    return max(values) if values else None


def _combine(planning, runtime, protection):
    if "confirmed_gap" in (planning, runtime, protection):
        return "confirmed_gap"
    if "unknown" in (planning, runtime, protection):
        return "unknown"
    if planning == "not_applicable" and runtime == "not_applicable":
        return "not_applicable"
    if "not_applicable" in (planning, runtime):
        return "unknown"
    if runtime == "satisfied_by_contingency":
        return "satisfied_by_contingency"
    if planning == "satisfied" and runtime == "satisfied":
        return "satisfied"
    return "unknown"


def _segment_times(start, end, interval, route_length, route_duration):
    if interval:
        left = _number_or_none(interval.get("start_route_offset_m"))
        right = _number_or_none(interval.get("end_route_offset_m"))
        start_time = _number_or_none(interval.get("start_time_s"))
        end_time = _number_or_none(interval.get("end_time_s"))
        if None not in (left, right, start_time, end_time) and right > left:
            scale = (end_time - start_time) / (right - left)
            return start_time + (start - left) * scale, start_time + (end - left) * scale
    if route_duration is not None and route_length > 0:
        return start / route_length * route_duration, end / route_length * route_duration
    return None, None


def _merge_segments(items):
    merged = []
    for item in items:
        if merged and _merge_key(merged[-1]) == _merge_key(item) and abs(merged[-1]["end_route_offset_m"] - item["start_route_offset_m"]) <= 1e-7:
            previous = merged[-1]
            previous["end_route_offset_m"] = item["end_route_offset_m"]
            previous["length_m"] += item["length_m"]
            previous["end_time_s"] = item["end_time_s"]
            previous["duration_s"] = (
                previous["end_time_s"] - previous["start_time_s"]
                if previous["start_time_s"] is not None and previous["end_time_s"] is not None
                else None
            )
            previous["reasons"] = _unique_values([*previous["reasons"], *item["reasons"]])
            previous["evidence"] = _unique_json([*previous["evidence"], *item["evidence"]])
            previous["contingency_exposure"]["length_m"] += item["contingency_exposure"]["length_m"]
            durations = [previous["contingency_exposure"].get("duration_s"), item["contingency_exposure"].get("duration_s")]
            previous["contingency_exposure"]["duration_s"] = sum(durations) if all(value is not None for value in durations) else None
        else:
            merged.append(deepcopy(item))
    return merged


def _merge_key(item):
    return (
        item["planning_status"], item["runtime_status"], item["combined_status"],
        tuple(item["gap_causes"]), item["remediation_scope"],
        item["protection_margin"].get("status"),
        item["protection_margin"].get("protection_margin_m"),
    )


def _subsystem_summary(code, required, route_length, route_duration, segments):
    length = {status: 0.0 for status in COMBINED_GAP_STATUSES}
    duration = {status: 0.0 for status in COMBINED_GAP_STATUSES}
    planning_length = {status: 0.0 for status in COMBINED_GAP_STATUSES}
    runtime_length = {status: 0.0 for status in COMBINED_GAP_STATUSES}
    known_duration = True
    contingency_length = contingency_duration = 0.0
    runtime_lost_length = runtime_lost_duration = 0.0
    for item in segments:
        length[item["combined_status"]] += item["length_m"]
        planning_length[item["planning_status"]] += item["length_m"]
        runtime_length[item["runtime_status"]] += item["length_m"]
        if item["duration_s"] is None:
            known_duration = False
        else:
            duration[item["combined_status"]] += item["duration_s"]
            if item["runtime_status"] == "confirmed_gap":
                runtime_lost_duration += item["duration_s"]
        if item["runtime_status"] == "confirmed_gap":
            runtime_lost_length += item["length_m"]
        if item["runtime_status"] == "satisfied_by_contingency":
            contingency_length += item["length_m"]
            if item["duration_s"] is not None:
                contingency_duration += item["duration_s"]
    satisfied_length = length["satisfied"] + length["satisfied_by_contingency"]
    denominator = route_length if route_length > 0 else None
    gap_segments = [item for item in segments if item["combined_status"] == "confirmed_gap"]
    status = _aggregate_status([item["combined_status"] for item in segments])
    return {
        "subsystem": code,
        "required": required,
        "status": status,
        "planning_assessment": {
            "status": _aggregate_status([item["planning_status"] for item in segments]),
            "length_by_status_m": planning_length,
        },
        "operational_assessment": {
            "status": _aggregate_status([item["runtime_status"] for item in segments]),
            "length_by_status_m": runtime_length,
        },
        "route_length_m": route_length,
        "duration_s": route_duration,
        "segments": segments,
        "satisfied_length_m": satisfied_length,
        "gap_length_m": length["confirmed_gap"],
        "unknown_length_m": length["unknown"],
        "not_applicable_length_m": length["not_applicable"],
        "satisfied_fraction": satisfied_length / denominator if denominator else None,
        "gap_fraction": length["confirmed_gap"] / denominator if denominator else None,
        "unknown_fraction": length["unknown"] / denominator if denominator else None,
        "contingency_exposure_length_m": contingency_length,
        "contingency_exposure_duration_s": contingency_duration if known_duration else None,
        "runtime_lost_length_m": runtime_lost_length,
        "runtime_lost_duration_s": runtime_lost_duration if known_duration else None,
        "duration_by_status_s": duration if known_duration else None,
        "max_continuous_gap_length_m": max((item["length_m"] for item in gap_segments), default=0.0),
        "max_continuous_gap_duration_s": (
            max((item["duration_s"] or 0.0 for item in gap_segments), default=0.0)
            if known_duration else None
        ),
        "gap_segment_count": len(gap_segments),
        "length_conservation_m": sum(length.values()),
        "time_conservation_s": sum(duration.values()) if known_duration else None,
    }


def _aggregate_status(statuses):
    statuses = list(statuses)
    if not statuses:
        return "missing_data"
    if "confirmed_gap" in statuses:
        return "confirmed_gap"
    if "unknown" in statuses or "missing_data" in statuses:
        return "unknown"
    if "satisfied_by_contingency" in statuses:
        return "satisfied_by_contingency"
    if all(item == "not_applicable" for item in statuses):
        return "not_applicable"
    if "satisfied" in statuses:
        return "satisfied"
    return "unknown"


def _remediation_scope(causes, planning_evidence):
    scopes = set()
    if "geometry_gap" in causes:
        scopes.add("ground_service_candidate")
    if "runtime_service_loss" in causes:
        scopes.add("operational_scenario")
    if "unknown_evidence" in causes:
        scopes.add("evidence_collection")
    if "protection_margin_gap" in causes:
        scopes.update(("ground_service_candidate", "aircraft_or_requirement"))
    if "static_service_mismatch" in causes:
        evidence = (planning_evidence or {}).get("evidence") or []
        if any(item.get("kind") == "aircraft_capability" for item in evidence if isinstance(item, dict)):
            scopes.add("aircraft_or_requirement")
        else:
            scopes.add("ground_service_candidate")
    if len(scopes) > 1:
        return "mixed"
    result = next(iter(scopes), "unknown")
    return result if result in REMEDIATION_SCOPES else "unknown"


def _route_index(result):
    return {
        str(item.get("route_id") or ""): item
        for item in (result or {}).get("routes") or []
        if item.get("route_id") is not None
    }


def _subsystem_index(route):
    return {
        str(item.get("subsystem") or "").upper(): item
        for item in (route or {}).get("subsystems") or []
    }


def _route_requirements(required_cns, route_id):
    return deepcopy(
        ((required_cns or {}).get("route_overrides") or {}).get(route_id)
        or (required_cns or {}).get("project_default") or {}
    )


def _route_length(*routes):
    for route in routes:
        value = _number_or_none((route or {}).get("route_length_m"))
        if value is not None and value >= 0:
            return value
    return 0.0


def _samples(subsystem):
    return sorted(
        (subsystem or {}).get("samples") or [],
        key=lambda item: float(item.get("distance_along_route_m") or 0.0),
    )


def _bounded(value, maximum):
    number = _number_or_none(value)
    if number is None:
        return None
    return min(maximum, max(0.0, number))


def _sample_evidence(sample):
    return {
        "status": sample.get("status"),
        "source_sample_offsets_m": [sample.get("distance_along_route_m")],
        "source_sample_statuses": [sample.get("status")],
        "reasons": deepcopy(sample.get("reasons") or []),
        "evidence": deepcopy(sample.get("evidence") or []),
        "provider_evaluations": deepcopy(sample.get("provider_evaluations") or []),
        "conservative_transition": False,
    }


def _number_or_none(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ordered_unique(values, ordering):
    present = set(values)
    return [value for value in ordering if value in present]


def _unique_values(values):
    return list(dict.fromkeys(str(value) for value in values if value not in (None, "")))


def _unique_json(values):
    result, seen = [], set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result

