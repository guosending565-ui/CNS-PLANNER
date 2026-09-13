"""Additive, threshold-free contracts for explicit CNS spatial planning objectives."""

from __future__ import annotations

from math import isfinite
from typing import TypedDict


OBJECTIVE_NAMES = (
    "min_satisfied_volume_fraction",
    "max_confirmed_deficit_volume_fraction",
    "max_unknown_volume_fraction",
    "min_redundancy_satisfied_volume_fraction",
    "max_continuous_deficit_projection_m",
)
OPERATORS = {">=", "<=", ">", "<", "=="}


class PlanningObjective(TypedDict, total=False):
    value: float | None
    operator: str | None
    source: str | None
    confirmed: bool
    status: str


def default_cns_planning_objectives():
    return {
        "status": "objectives_not_configured",
        "semantics": "explicit_spatial_planning_objectives_not_required_cns",
        "routes": {},
    }


def normalize_planning_objective(value, name):
    if name not in OBJECTIVE_NAMES:
        raise ValueError(f"不支持的 CNS planning objective：{name}")
    raw = value if isinstance(value, dict) else {}
    number = _optional_number(raw.get("value"), "value")
    if number is not None and name.endswith("_fraction") and not 0 <= number <= 1:
        raise ValueError(f"{name}.value 必须在 0..1")
    if number is not None and number < 0:
        raise ValueError(f"{name}.value 不得小于零")
    operator = str(raw.get("operator") or "").strip() or None
    if operator is not None and operator not in OPERATORS:
        raise ValueError(f"{name}.operator 无效")
    confirmed = bool(raw.get("confirmed", False)) and number is not None and operator is not None
    return {
        "value": number, "operator": operator, "source": raw.get("source"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed else "pending_confirmation",
    }


def normalize_cns_planning_objectives(value=None):
    raw = value if isinstance(value, dict) else {}
    route_values = raw.get("routes") or {}
    if not isinstance(route_values, dict):
        raise ValueError("cns_planning_objectives.routes 必须是对象")
    routes = {}
    configured = []
    for route_id, route_value in route_values.items():
        if not isinstance(route_value, dict):
            raise ValueError("route planning objectives 必须是对象")
        subsystem_values = route_value.get("subsystems", route_value)
        if not isinstance(subsystem_values, dict):
            raise ValueError("planning objective subsystems 必须是对象")
        subsystems = {}
        for code in ("C", "N", "S"):
            entry = subsystem_values.get(code) or {}
            objective_values = entry.get("objectives", entry) if isinstance(entry, dict) else {}
            objectives = {
                name: normalize_planning_objective(objective_values[name], name)
                for name in OBJECTIVE_NAMES if name in objective_values
            }
            if objectives:
                configured.extend(objectives.values())
                subsystems[code] = {"objectives": objectives}
        if subsystems:
            routes[str(route_id)] = {"route_id": str(route_id), "subsystems": subsystems}
    if not configured:
        status = "objectives_not_configured"
    elif all(item["status"] == "confirmed" for item in configured):
        status = "confirmed"
    else:
        status = "pending_confirmation"
    return {
        "status": status,
        "semantics": "explicit_spatial_planning_objectives_not_required_cns",
        "routes": routes,
    }


def empty_cns_corridor_gap_assessment(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "cns_corridor_gap_v1",
        "algorithm_version": "1.0",
        "model_scope": "spatial_corridor_service_redundancy_and_objectives",
        "continuity_semantics": "conservative_longitudinal_projection_of_corridor_voxel_deficits",
        "formal_continuity_probability": "not_evaluated",
        "input_fingerprint": None,
        "route_count": 0,
        "routes": [],
        "confirmed_target_voxel_ids": [],
        "unknown_voxel_ids": [],
        "not_evaluated": _not_evaluated(),
    }


def planning_not_evaluated():
    return _not_evaluated()


def _not_evaluated():
    return {
        name: "not_evaluated" for name in (
            "common_cause", "shared_power", "shared_backhaul",
            "tower_failure", "site_failure_propagation",
            "runtime_outage", "formal_continuity_probability",
        )
    }


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number
