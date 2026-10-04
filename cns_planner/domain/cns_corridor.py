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
        #: 与 ``CNSServiceCorridorV1.algorithm_version`` 保持同步。
        "algorithm_version": "1.3",
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


# ---- Phase4-B9R.1：唯一的规模准入估算件 ------------------------------------------
#
# 异步提交路径（``cns_planner.tasks.task_specs``）与同步入口
# （``cns_planner.application.corridor_service``）**必须**用同一份实现估算同一份输入，
# 否则两条路径会出现"同一规模、两种准入"的第二种真值。这里只是把估算件的归属收到
# domain 层，估算本身的数学仍完全属于
# :func:`cns_planner.algorithms.corridor.v1.estimate_corridor_complexity`。


def corridor_complexity_preflight(inputs):
    """对一份已组装的服务走廊输入做规模估算（纯函数：不写 state、不改 result）。

    ``inputs`` 的字段名以 P14 ``evaluate`` 的入参为准（``existing_facilities`` /
    ``device_catalog`` / ``corridor_policy``）；估算结果里的每个量都显式标注为上界，
    不参与任何数学判定，也不进入 canonical assessment schema。
    """

    from ..algorithms.corridor.v1 import estimate_corridor_complexity

    inputs = inputs if isinstance(inputs, dict) else {}
    return estimate_corridor_complexity(
        inputs.get("routes") or [], inputs.get("spatial_3d") or {},
        inputs.get("grid") or {}, inputs.get("grid_attributes") or {},
        inputs.get("corridor_policy") or {},
        inputs.get("existing_facilities") or {},
        inputs.get("device_catalog") or {},
        #: Round 2：估算与正式评估共用同一份 surface 事实（provider 为 callable，
        #: 只在本进程内使用；它绝不进入 immutable snapshot）。
        surface_class_provider=inputs.get("surface_class_provider"),
    )


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
