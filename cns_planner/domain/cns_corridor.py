"""Schema-v2 additive contracts for an engineering CNS requirement corridor."""

from __future__ import annotations

from math import isfinite
from typing import TypedDict


class CNSCorridorSpec(TypedDict, total=False):
    route_id: str
    horizontal_half_width_m: float | None
    vertical_lower_margin_m: float | None
    vertical_upper_margin_m: float | None
    source: str | None
    confirmed: bool
    status: str


def default_cns_corridor_policy():
    return {
        "status": "pending_confirmation",
        "semantics": "engineering_cns_service_requirement_corridor",
        "regulatory_operational_volume": "not_evaluated",
        "routes": {},
    }


def normalize_corridor_spec(value, route_id=None):
    if not isinstance(value, dict):
        raise ValueError("CNSCorridorSpec 必须是对象")
    identifier = str(route_id or value.get("route_id") or "").strip()
    if not identifier:
        raise ValueError("CNSCorridorSpec.route_id 不能为空")
    fields = (
        "horizontal_half_width_m", "vertical_lower_margin_m",
        "vertical_upper_margin_m",
    )
    numbers = {name: _optional_nonnegative(value.get(name), name) for name in fields}
    complete = all(numbers[name] is not None for name in fields)
    confirmed = bool(value.get("confirmed", False)) and complete
    return {
        "route_id": identifier,
        **numbers,
        "source": value.get("source"),
        "confirmed": confirmed,
        "status": "confirmed" if confirmed else "pending_confirmation",
    }


def normalize_cns_corridor_policy(value=None):
    raw = value if isinstance(value, dict) else {}
    route_values = raw.get("routes") or {}
    if isinstance(route_values, list):
        route_values = {str(item.get("route_id") or ""): item for item in route_values}
    if not isinstance(route_values, dict):
        raise ValueError("cns_corridor_policy.routes 必须是对象或数组")
    routes = {
        str(route_id): normalize_corridor_spec(spec, str(route_id))
        for route_id, spec in route_values.items() if str(route_id).strip()
    }
    return {
        "status": "passed" if routes and all(item["status"] == "confirmed" for item in routes.values()) else "pending_confirmation",
        "semantics": "engineering_cns_service_requirement_corridor",
        "regulatory_operational_volume": "not_evaluated",
        "routes": routes,
    }


def empty_cns_corridor_assessment(status="not_calculated"):
    return {
        "status": status,
        "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0",
        "model_scope": "engineering_cns_service_requirement_corridor",
        "input_fingerprint": None,
        "corridor_geometry_fingerprint": None,
        "route_count": 0,
        "routes": [],
        "deficit_voxel_ids": [],
        "unknown_voxel_ids": [],
        "disclaimers": _disclaimers(),
    }


def corridor_disclaimers():
    return _disclaimers()


def _disclaimers():
    return {
        "jarus_operational_volume": "not_evaluated",
        "u_space_surveillance_volume": "not_evaluated",
        "regulatory_approval": "not_evaluated",
        "evaluation_semantics": "representative_voxel_probe_not_entire_voxel_guarantee",
        "horizontal_discretization": "conservative_grid_cell_inclusion_not_exact_buffer",
        "volume_semantics": "discretized_volume_proxy_not_exact_corridor_volume",
    }


def _optional_nonnegative(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not isfinite(number) or number < 0:
        raise ValueError(f"{field} 必须是非负有限数值")
    return number
