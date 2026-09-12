"""Deterministic route service timeline driven only by explicit scenarios."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ...domain.operational_timing import distance_to_time_s
from ...safety.service_state import evaluate_service_state


SUBSYSTEM_NAMES = {"C": "communication", "N": "navigation", "S": "surveillance"}
SERVICE_STATES = (
    "available", "available_degraded", "contingency", "lost", "unknown",
    "not_applicable",
)


class RouteServiceTimelineV1:
    algorithm_id = "route_service_timeline_v1"
    algorithm_version = "1.0"

    def __init__(self, parameters=None):
        self.parameters = dict(parameters or {})

    @classmethod
    def empty(cls, status="not_calculated"):
        return {
            "status": status, "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version, "parameters": {},
            "model_scope": "explicit_scenario_service_timeline",
            "input_fingerprint": None, "route_count": 0, "routes": [],
            "reliability_sampling": "prohibited",
        }

    def evaluate(
        self, coverage_3d, cns_service_capability, required_cns,
        aircraft_profile, operational_timing,
    ):
        capability_routes = {
            str(item.get("route_id")): item
            for item in (cns_service_capability or {}).get("routes") or []
        }
        timing = operational_timing or {}
        timeline_inputs = {
            "route_motion_profiles": timing.get("route_motion_profiles") or {},
            "service_scenarios": timing.get("service_scenarios") or {},
        }
        routes = []
        for route in (coverage_3d or {}).get("routes") or []:
            route_id = str(route.get("route_id") or "")
            routes.append(self._route(
                route,
                capability_routes.get(route_id) or {},
                _route_requirements(required_cns, route_id),
                aircraft_profile or {},
                (timing.get("route_motion_profiles") or {}).get(route_id),
                (timing.get("service_scenarios") or {}).get(route_id),
            ))
        fingerprint = sha256(json.dumps([
            coverage_3d or {}, cns_service_capability or {}, required_cns or {},
            aircraft_profile or {}, timeline_inputs, self.parameters,
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {
            "status": "passed" if any(item["status"] == "passed" for item in routes) else "missing_data",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(self.parameters),
            "model_scope": "explicit_scenario_service_timeline",
            "input_fingerprint": fingerprint, "route_count": len(routes),
            "routes": routes, "reliability_sampling": "prohibited",
            "semantics": "runtime_state_requires_explicit_external_event",
        }

    def _route(self, route, capability_route, requirements, aircraft, motion, scenario):
        route_id = str(route.get("route_id") or "")
        distance = float(route.get("route_length_m") or 0.0)
        p7_samples = sorted(
            route.get("samples") or [],
            key=lambda item: float(item.get("distance_along_route_m") or 0.0),
        )
        speed = _confirmed_speed(motion)
        if speed is None:
            return _unknown_route(route_id, distance, p7_samples, requirements, motion)
        duration = distance_to_time_s(distance, speed)
        timed_samples = [
            {
                **deepcopy(sample),
                "time_from_start_s": distance_to_time_s(
                    sample.get("distance_along_route_m") or 0.0, speed
                ),
            }
            for sample in p7_samples
        ]
        capability_subsystems = {
            item.get("subsystem"): item
            for item in capability_route.get("subsystems") or []
        }
        events = list((scenario or {}).get("events") or [])
        scenario_confirmed = (scenario or {}).get("confirmed") is True
        subsystems = []
        for code, name in SUBSYSTEM_NAMES.items():
            required = requirements.get(name) or {}
            capability = aircraft.get(name) or {}
            capability_result = capability_subsystems.get(code) or {}
            subsystem_events = [item for item in events if item.get("subsystem") == code]
            subsystems.append(_subsystem_timeline(
                code, distance, duration, speed, timed_samples, required,
                capability, capability_result, subsystem_events,
                scenario_confirmed,
            ))
        return {
            "route_id": route_id, "status": "passed",
            "route_length_m": distance, "duration_s": duration,
            "motion_profile": deepcopy(motion), "samples": timed_samples,
            "subsystems": subsystems,
        }


def _subsystem_timeline(
    code, route_length, duration, speed, timed_samples, required, aircraft,
    capability_result, events, scenario_confirmed,
):
    if required.get("required") is False:
        interval = _interval(
            0.0, duration, speed, "not_applicable",
            ["该分系统未被 RequiredCNS 要求"], [], None, None,
        )
        return _subsystem_result(code, route_length, duration, [interval])
    boundaries = {0.0, duration}
    boundaries.update(
        min(duration, max(0.0, float(item.get("time_from_start_s") or 0.0)))
        for item in timed_samples
    )
    for event in events:
        boundaries.add(min(duration, max(0.0, float(event["start_s"]))))
        boundaries.add(min(duration, max(0.0, float(event["end_s"]))))
        for limit in _fallback_limits(required, aircraft):
            transition = float(event["start_s"]) + limit
            if 0.0 < transition < duration:
                boundaries.add(transition)
    ordered = sorted(boundaries)
    intervals = []
    for start, end in zip(ordered, ordered[1:]):
        if end <= start:
            continue
        midpoint = (start + end) / 2.0
        event = next(
            (item for item in events if item["start_s"] <= midpoint < item["end_s"]),
            None,
        )
        static_state = _capability_at(capability_result, midpoint * speed)
        static_evidence = {
            "kind": "p8_static_capability", "status": static_state,
            "does_not_imply_runtime_available": True,
        }
        if event is None:
            state_result = {
                "service_state": "unknown", "fallback_used": None,
                "reasons": ["无显式 runtime snapshot/service scenario event"],
                "evidence": [static_evidence],
            }
            event_id = None
        elif not scenario_confirmed or event.get("confirmed") is not True:
            state_result = {
                "service_state": "unknown", "fallback_used": None,
                "reasons": ["ServiceScenario/Event 未确认"],
                "evidence": [static_evidence],
            }
            event_id = event.get("event_id")
        else:
            elapsed = max(0.0, end - float(event["start_s"]))
            snapshot = {
                "status": event["external_state"],
                "type": deepcopy(event.get("type") or {}),
                "performance": deepcopy(event.get("performance") or {}),
                "redundancy": event.get("redundancy"),
                "source": event.get("source"),
                "outage_duration_s": elapsed if event["external_state"] == "unavailable" else None,
                "degradation_duration_s": elapsed if event["external_state"] == "degraded" else None,
            }
            state_result = evaluate_service_state(required, aircraft, snapshot)
            state_result["evidence"] = [static_evidence, *(state_result.get("evidence") or [])]
            event_id = event.get("event_id")
        intervals.append(_interval(
            start, end, speed, state_result["service_state"],
            state_result.get("reasons") or [], state_result.get("evidence") or [],
            state_result.get("fallback_used"), event_id,
        ))
    return _subsystem_result(code, route_length, duration, _merge_intervals(intervals))


def _subsystem_result(code, route_length, duration, intervals):
    duration_by_state = {state: 0.0 for state in SERVICE_STATES}
    length_by_state = {state: 0.0 for state in SERVICE_STATES}
    for item in intervals:
        duration_by_state[item["service_state"]] += item["duration_s"]
        length_by_state[item["service_state"]] += item["length_m"]
    return {
        "subsystem": code, "status": "passed", "intervals": intervals,
        "route_length_m": route_length, "duration_s": duration,
        "duration_by_state_s": duration_by_state,
        "length_by_state_m": length_by_state,
        "unknown_duration_s": duration_by_state["unknown"],
        "unknown_length_m": length_by_state["unknown"],
        "states_present": [state for state, value in duration_by_state.items() if value > 0],
    }


def _interval(start, end, speed, state, reasons, evidence, fallback, event_id):
    if state not in SERVICE_STATES:
        raise ValueError(f"不支持的 ServiceState：{state}")
    return {
        "start_time_s": start, "end_time_s": end,
        "duration_s": end - start,
        "start_route_offset_m": start * speed,
        "end_route_offset_m": end * speed,
        "length_m": (end - start) * speed,
        "service_state": state, "scenario_event_id": event_id,
        "fallback_used": deepcopy(fallback),
        "reasons": deepcopy(reasons), "evidence": deepcopy(evidence),
    }


def _merge_intervals(intervals):
    result = []
    for item in intervals:
        if result and _merge_key(result[-1]) == _merge_key(item):
            previous = result[-1]
            previous["end_time_s"] = item["end_time_s"]
            previous["end_route_offset_m"] = item["end_route_offset_m"]
            previous["duration_s"] += item["duration_s"]
            previous["length_m"] += item["length_m"]
        else:
            result.append(deepcopy(item))
    return result


def _merge_key(item):
    return json.dumps([
        item["service_state"], item.get("scenario_event_id"),
        item.get("fallback_used"), item.get("reasons"), item.get("evidence"),
    ], sort_keys=True, ensure_ascii=False)


def _capability_at(subsystem, distance):
    samples = sorted(
        subsystem.get("samples") or [],
        key=lambda item: float(item.get("distance_along_route_m") or 0.0),
    )
    if not samples:
        return subsystem.get("status") or "unknown"
    selected = samples[0]
    for item in samples:
        if float(item.get("distance_along_route_m") or 0.0) <= distance:
            selected = item
        else:
            break
    return selected.get("status") or "unknown"


def _fallback_limits(required, aircraft):
    performance = required.get("performance") or {}
    limits = [
        performance.get("lost_link_threshold_s"),
        performance.get("max_continuous_outage_s"),
        performance.get("max_degradation_time_s"),
        performance.get("max_track_loss_s"),
    ]
    limits.extend(
        item.get("max_bridge_time_s")
        for item in aircraft.get("fallbacks") or []
        if isinstance(item, dict) and item.get("confirmed") is True
    )
    return sorted({float(item) for item in limits if item not in (None, "") and float(item) >= 0})


def _confirmed_speed(profile):
    if not isinstance(profile, dict):
        return None
    if profile.get("mode") != "constant_ground_speed_mps":
        return None
    if profile.get("confirmed") is not True or profile.get("status") != "confirmed":
        return None
    speed = profile.get("constant_ground_speed_mps")
    if speed is None or float(speed) <= 0:
        return None
    return float(speed)


def _unknown_route(route_id, distance, samples, requirements, motion):
    subsystems = []
    for code, name in SUBSYSTEM_NAMES.items():
        required = requirements.get(name) or {}
        state = "not_applicable" if required.get("required") is False else "unknown"
        subsystems.append({
            "subsystem": code, "status": "missing_data", "intervals": [],
            "route_length_m": distance, "duration_s": None,
            "duration_by_state_s": {item: None for item in SERVICE_STATES},
            "length_by_state_m": {
                item: distance if item == state else 0.0 for item in SERVICE_STATES
            },
            "unknown_duration_s": None,
            "unknown_length_m": distance if state == "unknown" else 0.0,
            "states_present": [state],
        })
    return {
        "route_id": route_id, "status": "missing_data",
        "route_length_m": distance, "duration_s": None,
        "motion_profile": deepcopy(motion),
        "samples": [{**deepcopy(item), "time_from_start_s": None} for item in samples],
        "subsystems": subsystems,
        "reasons": ["缺少 confirmed constant_ground_speed_mps，未猜测速度"],
    }


def _route_requirements(required_cns, route_id):
    return (
        ((required_cns or {}).get("route_overrides") or {}).get(route_id)
        or (required_cns or {}).get("project_default")
        or {}
    )
