"""Additive JSON-safe contracts for route timing and tactical protection inputs."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, TypedDict


SERVICE_SUBSYSTEMS = ("C", "N", "S")
EXTERNAL_STATES = ("available", "degraded", "unavailable", "unknown")
RESPONSE_COMPONENTS = (
    "detect", "track", "processing", "decision", "communication",
    "aircraft_reaction",
)


class RouteMotionProfile(TypedDict, total=False):
    route_id: str
    mode: str
    constant_ground_speed_mps: float | None
    waypoints: list[dict[str, Any]]
    source: str
    confirmed: bool
    status: str


class ServiceScenarioEvent(TypedDict, total=False):
    event_id: str
    subsystem: str
    start_s: float
    end_s: float
    external_state: str
    type: dict[str, Any]
    performance: dict[str, Any]
    redundancy: int | None
    source: str
    confirmed: bool
    status: str


class ResponseTimeBudget(TypedDict, total=False):
    budget_id: str
    components: dict[str, dict[str, Any]]
    source: str
    confirmed: bool
    status: str


class EncounterScenario(TypedDict, total=False):
    encounter_id: str
    relative_closing_speed_mps: float | None
    maneuver_distance_m: float | None
    uncertainty_distance_m: float | None
    source: str
    confirmed: bool
    status: str


def empty_operational_timing():
    return {
        "status": "pending_confirmation",
        "route_motion_profiles": {},
        "service_scenarios": {},
        "response_time_budgets": {},
        "encounter_scenarios": {},
    }


def normalize_operational_timing(value):
    raw = value if isinstance(value, dict) else {}
    motion = _object(raw.get("route_motion_profiles"), "route_motion_profiles")
    scenarios = _object(raw.get("service_scenarios"), "service_scenarios")
    budgets = _object(raw.get("response_time_budgets"), "response_time_budgets")
    encounters = _object(raw.get("encounter_scenarios"), "encounter_scenarios")
    result = {
        "route_motion_profiles": {
            str(key): normalize_route_motion_profile({**item, "route_id": str(key)})
            for key, item in motion.items()
        },
        "service_scenarios": {
            str(key): normalize_service_scenario({**item, "route_id": str(key)})
            for key, item in scenarios.items()
        },
        "response_time_budgets": {
            str(key): normalize_response_time_budget({**item, "budget_id": str(key)})
            for key, item in budgets.items()
        },
        "encounter_scenarios": {
            str(key): normalize_encounter_scenario({**item, "encounter_id": str(key)})
            for key, item in encounters.items()
        },
    }
    records = [item for group in result.values() for item in group.values()]
    result["status"] = (
        "passed" if records and all(item.get("status") == "confirmed" for item in records)
        else "pending_confirmation"
    )
    return result


def normalize_route_motion_profile(value):
    if not isinstance(value, dict):
        raise ValueError("RouteMotionProfile 必须是对象")
    route_id = _identifier(value.get("route_id"), "route_id")
    mode = str(value.get("mode") or "constant_ground_speed_mps")
    if mode not in ("constant_ground_speed_mps", "waypoint_linear"):
        raise ValueError("RouteMotionProfile.mode 无效")
    speed = _optional_number(value.get("constant_ground_speed_mps"), "constant_ground_speed_mps")
    if speed is not None and speed <= 0:
        raise ValueError("constant_ground_speed_mps 必须大于零")
    waypoints = value.get("waypoints") or []
    if not isinstance(waypoints, list):
        raise ValueError("RouteMotionProfile.waypoints 必须是数组")
    confirmed = bool(value.get("confirmed", False))
    implemented = mode == "constant_ground_speed_mps" and speed is not None
    return {
        "route_id": route_id, "mode": mode,
        "constant_ground_speed_mps": speed,
        "waypoints": deepcopy(waypoints),
        "source": str(value.get("source") or "未记录"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed and implemented else "pending_confirmation",
    }


def normalize_service_scenario(value):
    if not isinstance(value, dict):
        raise ValueError("ServiceScenario 必须是对象")
    route_id = _identifier(value.get("route_id"), "route_id")
    scenario_id = str(value.get("scenario_id") or f"scenario-{route_id}")
    events = value.get("events") or []
    if not isinstance(events, list):
        raise ValueError("ServiceScenario.events 必须是数组")
    normalized = [normalize_service_scenario_event(item) for item in events]
    _reject_overlaps(normalized)
    confirmed = bool(value.get("confirmed", False))
    return {
        "scenario_id": scenario_id, "route_id": route_id,
        "events": normalized, "source": str(value.get("source") or "未记录"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed and all(item["status"] == "confirmed" for item in normalized) else "pending_confirmation",
    }


def normalize_service_scenario_event(value):
    if not isinstance(value, dict):
        raise ValueError("ServiceScenarioEvent 必须是对象")
    event_id = _identifier(value.get("event_id", value.get("id")), "event_id")
    subsystem = str(value.get("subsystem") or "").upper()
    if subsystem not in SERVICE_SUBSYSTEMS:
        raise ValueError("ServiceScenarioEvent.subsystem 必须是 C/N/S")
    start = _nonnegative(value.get("start_s"), "start_s")
    end = _nonnegative(value.get("end_s"), "end_s")
    if end <= start:
        raise ValueError("ServiceScenarioEvent.end_s 必须大于 start_s")
    external_state = str(value.get("external_state") or "unknown").lower()
    if external_state not in EXTERNAL_STATES:
        raise ValueError("ServiceScenarioEvent.external_state 无效")
    type_data, performance = value.get("type") or {}, value.get("performance") or {}
    if not isinstance(type_data, dict) or not isinstance(performance, dict):
        raise ValueError("ServiceScenarioEvent type/performance 必须是对象")
    redundancy = value.get("redundancy")
    if redundancy not in (None, ""):
        redundancy_number = _number(redundancy, "ServiceScenarioEvent.redundancy")
        if redundancy_number <= 0 or not redundancy_number.is_integer():
            raise ValueError("ServiceScenarioEvent.redundancy 必须是正整数")
        redundancy = int(redundancy_number)
    else:
        redundancy = None
    confirmed = bool(value.get("confirmed", False))
    return {
        "event_id": event_id, "subsystem": subsystem,
        "start_s": start, "end_s": end, "external_state": external_state,
        "type": deepcopy(type_data), "performance": deepcopy(performance),
        "redundancy": redundancy, "source": str(value.get("source") or "未记录"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed else "pending_confirmation",
    }


def normalize_response_time_budget(value):
    if not isinstance(value, dict):
        raise ValueError("ResponseTimeBudget 必须是对象")
    budget_id = _identifier(value.get("budget_id"), "budget_id")
    raw_components = value.get("components") or {}
    if not isinstance(raw_components, dict):
        raise ValueError("ResponseTimeBudget.components 必须是对象")
    components = {}
    for name in RESPONSE_COMPONENTS:
        raw = raw_components.get(name)
        if raw is None:
            components[name] = {
                "value_s": None, "source": "未记录", "confirmed": False,
                "status": "missing_data",
            }
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"ResponseTimeBudget.{name} 必须包含 value_s/source/confirmed")
        number = _optional_nonnegative(raw.get("value_s"), f"{name}.value_s")
        confirmed = bool(raw.get("confirmed", False))
        components[name] = {
            "value_s": number, "source": str(raw.get("source") or "未记录"),
            "confirmed": confirmed,
            "status": "confirmed" if confirmed and number is not None else "pending_confirmation" if number is not None else "missing_data",
        }
    confirmed = bool(value.get("confirmed", False))
    complete = all(item["status"] == "confirmed" for item in components.values())
    return {
        "budget_id": budget_id, "components": components,
        "source": str(value.get("source") or "未记录"), "confirmed": confirmed,
        "status": "confirmed" if confirmed and complete else "pending_confirmation",
    }


def normalize_encounter_scenario(value):
    if not isinstance(value, dict):
        raise ValueError("EncounterScenario 必须是对象")
    encounter_id = _identifier(value.get("encounter_id"), "encounter_id")
    confirmed = bool(value.get("confirmed", False))
    values = {
        field: _optional_nonnegative(value.get(field), field)
        for field in (
            "relative_closing_speed_mps", "maneuver_distance_m",
            "uncertainty_distance_m",
        )
    }
    complete = all(item is not None for item in values.values())
    return {
        "encounter_id": encounter_id, **values,
        "source": str(value.get("source") or "未记录"), "confirmed": confirmed,
        "status": "confirmed" if confirmed and complete else "pending_confirmation",
    }


def distance_to_time_s(distance_along_route_m, ground_speed_mps):
    distance = _nonnegative(distance_along_route_m, "distance_along_route_m")
    speed = _number(ground_speed_mps, "ground_speed_mps")
    if speed <= 0:
        raise ValueError("ground_speed_mps 必须大于零")
    return distance / speed


def _reject_overlaps(events):
    for subsystem in SERVICE_SUBSYSTEMS:
        ordered = sorted(
            (item for item in events if item["subsystem"] == subsystem),
            key=lambda item: (item["start_s"], item["end_s"]),
        )
        for previous, current in zip(ordered, ordered[1:]):
            if current["start_s"] < previous["end_s"]:
                raise ValueError(
                    f"{subsystem} ServiceScenarioEvent 时间重叠，P9 不猜测优先级"
                )


def _object(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"operational_timing.{field} 必须是对象")
    return value


def _identifier(value, field):
    identifier = str(value or "").strip()
    if not identifier:
        raise ValueError(f"{field} 不能为空")
    return identifier


def _number(value, field):
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _nonnegative(value, field):
    number = _number(value, field)
    if number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_number(value, field):
    return None if value in (None, "") else _number(value, field)


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    return _nonnegative(value, field)
