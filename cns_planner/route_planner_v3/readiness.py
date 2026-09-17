"""Per-domain V3 readiness.  Nothing is guessed, and nothing runs while not ready.

Two distinct conditions are reported separately:

``blocked``
    the safety evidence itself is missing or unusable -- unknown/NoData terrain or
    building evidence, no confirmed allowed airspace, no canonical environment.
``pending``
    the evidence could exist but an explicit policy parameter or source
    declaration has not been supplied/confirmed yet.

Both stop the search: a blocked environment never runs, and V3-A never invents a
safety parameter, a weight or an energy model to make a run possible.
"""

from __future__ import annotations

from .contracts import MAPPED_SOFT_CHANNELS
from .motion import kinematic_readiness

#: Policy parameters with no default at all: without them no search may run.
REQUIRED_POLICY_PARAMETERS = (
    "min_altitude_egm2008_m", "max_altitude_egm2008_m", "vertical_step_m",
    "terrain_clearance_m", "building_horizontal_clearance_m",
    "building_vertical_clearance_m", "aircraft_min_turn_radius_m",
)
#: Thresholds the canonical environment must declare for its own adjudication.
REQUIRED_ENVIRONMENT_PROPERTIES = (
    "terrain_clearance_m", "building_horizontal_clearance_m", "building_vertical_clearance_m",
)

READINESS_STATUSES = ("ready", "pending", "blocked")


def _entry(name, status, reasons, **extra):
    record = {"name": name, "status": status, "reasons": list(reasons)}
    record.update(extra)
    return record


def _downgrade(entry, status):
    if entry["status"] == "ready":
        entry["status"] = status


def evaluate_v3_readiness(problem):
    """Readiness for airspace / terrain / building / policy / aircraft / cost model."""

    policy = problem.get("policy") or {}
    aircraft = problem.get("aircraft_motion_limits") or {}
    environment = problem.get("environment") or {}
    cells = environment.get("cells") or []
    return {
        "airspace": _airspace(cells, environment),
        "terrain": _terrain(cells, policy, environment),
        "building": _building(cells, policy, environment),
        "policy": _policy(policy),
        "aircraft": _aircraft(policy, aircraft),
        "cost_model": _cost_model(policy, cells),
    }


def cost_enabled_channels(policy):
    """Soft channels the policy explicitly enabled (energy can never be enabled)."""

    components = (policy.get("cost_model") or {}).get("components") or {}
    return sorted(
        name for name in MAPPED_SOFT_CHANNELS
        if (components.get(name) or {}).get("enabled") and name != "energy"
    )


def unresolved_soft_channel_cells(cells, policy, id_field="grid_id"):
    """Cells that do not carry a provenance ``[0, 1]`` index for an enabled channel.

    This is the fail-closed half of the V3-A correctness fix: the planner has no
    hidden normalizer any more, so an enabled mapped channel whose index is absent
    (or is not ``status=passed``) must block the run instead of being costed as
    ``0``.  The building channel is derived from the state's own vertical margin
    and therefore cannot be "missing" in this sense.

    ``id_field`` lets the same check run over corridor-local fine cells
    (``fine_cell_id``) without duplicating the rule.
    """

    result = {}
    for channel in cost_enabled_channels(policy):
        missing = sorted(
            str(cell[id_field]) for cell in cells
            if ((cell.get("soft_fields") or {}).get(channel) or {}).get("status") != "passed"
            or ((cell.get("soft_fields") or {}).get(channel) or {}).get("normalized_index") is None
        )
        result[channel] = missing
    return result


def policy_readiness(policy):
    """Public alias of the policy domain (shared by V3-A and V3-B)."""

    return _policy(policy)


def aircraft_readiness(policy, aircraft):
    """Public alias of the aircraft domain (shared by V3-A and V3-B)."""

    return _aircraft(policy, aircraft)


def cost_model_readiness(policy, cells=None, id_field="grid_id"):
    """Public alias of the cost-model domain, usable for fine cells as well."""

    return _cost_model(policy, cells, id_field=id_field)


def _airspace(cells, environment):
    counts = {"confirmed_allowed": 0, "confirmed_restricted": 0, "unknown": 0}
    for cell in cells:
        status = str((cell.get("airspace") or {}).get("status") or "unknown")
        counts[status if status in counts else "unknown"] += 1
    entry = _entry(
        "airspace", "ready", [], status_counts=counts, cell_count=len(cells),
        evaluated=bool(cells),
        semantics="only_confirmed_allowed_cells_are_feasible_never_inferred_from_layer_name_or_color",
    )
    if not cells:
        entry["status"] = "blocked"
        entry["evaluated"] = False
        entry["reasons"].append("canonical V3CellEnvironment 为空，没有任何可判定单元")
    elif environment.get("status") == "missing_data":
        entry["status"] = "blocked"
        entry["reasons"].append(environment.get("reason") or "环境整体状态为 missing_data")
    elif counts["confirmed_allowed"] == 0:
        entry["status"] = "blocked"
        entry["reasons"].append(
            "没有任何 confirmed allowed 空域单元；unknown 一律 fail-closed，不得当作允许"
        )
    if counts["unknown"] and entry["status"] == "ready":
        entry["reasons"].append(
            f"{counts['unknown']} 个单元空域状态为 unknown，这些单元在搜索中不可行"
        )
    return entry


def _terrain(cells, policy, environment):
    """Terrain readiness.

    Terrain has no "confirmed absence": every cell needs a surface clearance
    floor, so ``None`` or ``data_status != passed`` blocks the run.
    """

    unresolved = sorted(
        str(cell["grid_id"]) for cell in cells
        if (cell.get("terrain") or {}).get("data_status") != "passed"
        or (cell.get("terrain") or {}).get("surface_clearance_egm2008_m") is None
    )
    entry = _entry(
        "terrain", "ready", [], cell_count=len(cells),
        unresolved_cell_count=len(unresolved), unresolved_samples=unresolved[:20],
        semantics="cell_level_surface_clearance_floor_required_fail_closed_on_unknown",
    )
    if not cells:
        entry["status"] = "blocked"
        entry["reasons"].append("没有 canonical cell，无法判定 terrain clearance")
        return entry
    if unresolved:
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"{len(unresolved)} 个单元的 terrain 证据 unknown/NoData：unknown 一律不可行"
        )
    if policy.get("terrain_clearance_m") is None:
        _downgrade(entry, "pending")
        entry["reasons"].append("policy.terrain_clearance_m 未显式配置")
    if not (environment.get("properties") or {}).get("terrain_clearance_m"):
        _downgrade(entry, "pending")
        entry["reasons"].append("canonical 环境未声明 terrain_clearance_m 属性来源")
    return entry


def _building(cells, policy, environment):
    """Building readiness.

    ``buildings.data_status == "passed"`` with no
    ``required_clearance_egm2008_m`` means "this cell is a *confirmed absence* of
    a building constraint" -- not missing data.  Only ``data_status != passed``
    (unknown/NoData evidence) blocks, and such cells stay infeasible in search.
    """

    unknown = sorted(
        str(cell["grid_id"]) for cell in cells
        if (cell.get("buildings") or {}).get("data_status") != "passed"
    )
    constrained = sorted(
        str(cell["grid_id"]) for cell in cells
        if (cell.get("buildings") or {}).get("required_clearance_egm2008_m") is not None
    )
    entry = _entry(
        "building", "ready", [], cell_count=len(cells),
        unknown_cell_count=len(unknown), unknown_samples=unknown[:20],
        constrained_cell_count=len(constrained),
        confirmed_absence_cell_count=len(cells) - len(unknown) - len(constrained),
        semantics=(
            "data_status_unknown_blocks; data_status_passed_without_a_clearance_is_"
            "a_confirmed_absence_of_a_building_constraint"
        ),
    )
    if not cells:
        entry["status"] = "blocked"
        entry["reasons"].append("没有 canonical cell，无法判定 building clearance")
        return entry
    if unknown:
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"{len(unknown)} 个单元的 building 证据 unknown/NoData：unknown 一律不可行"
        )
    if policy.get("building_horizontal_clearance_m") is None or policy.get("building_vertical_clearance_m") is None:
        _downgrade(entry, "pending")
        entry["reasons"].append(
            "policy.building_horizontal_clearance_m / building_vertical_clearance_m 未显式配置"
        )
    if not (environment.get("properties") or {}).get("building_vertical_clearance_m"):
        _downgrade(entry, "pending")
        entry["reasons"].append("canonical 环境未声明 building_vertical_clearance_m 属性来源")
    return entry


def _policy(policy):
    missing = [name for name in REQUIRED_POLICY_PARAMETERS if policy.get(name) is None]
    entry = _entry(
        "policy", "ready", [], missing_parameters=missing,
        confirmed=bool(policy.get("confirmed")), source=policy.get("source"),
        semantics="no_safety_parameter_has_a_default_value",
    )
    if missing:
        entry["status"] = "blocked"
        entry["reasons"].append("缺少必需安全参数：" + ", ".join(missing))
    if not policy.get("confirmed"):
        _downgrade(entry, "pending")
        entry["reasons"].append("policy 未经项目工程依据确认（confirmed=false）")
    minimum, maximum = policy.get("min_altitude_egm2008_m"), policy.get("max_altitude_egm2008_m")
    if minimum is not None and maximum is not None and maximum < minimum:
        entry["status"] = "blocked"
        entry["reasons"].append("高度上界低于下界")
    if not (policy.get("cost_model") or {}).get("components"):
        entry["status"] = "blocked"
        entry["reasons"].append("policy.cost_model 缺失")
    return entry


def _aircraft(policy, aircraft):
    kinematics = kinematic_readiness(policy, aircraft)
    entry = _entry(
        "aircraft", "ready", [], **kinematics,
        aircraft_id=aircraft.get("aircraft_id") or None,
        min_turn_radius_m=policy.get("aircraft_min_turn_radius_m"),
        turn_radius_source=(
            "policy.aircraft_min_turn_radius_m" if policy.get("aircraft_min_turn_radius_m") is not None
            else "unresolved_no_turn_radius_is_ever_guessed"
        ),
        confirmed=bool(aircraft.get("confirmed")),
        source=aircraft.get("source"),
        motion_model="engineering_kinematic_limits_not_flight_dynamics_certification",
    )
    if policy.get("aircraft_min_turn_radius_m") is None:
        entry["status"] = "blocked"
        entry["reasons"].append("policy.aircraft_min_turn_radius_m 未显式配置，转弯能力未知")
    if not kinematics["resolved"]:
        entry["status"] = "blocked"
        if kinematics["climb_gradient"] is None:
            entry["reasons"].append(
                "climb 能力未解析：" + kinematics["climb_gradient_source"]
                + "（只有 max_climb_rate_mps 而没有 explicit planning_speed_mps 时禁止换算）"
            )
        if kinematics["descent_gradient"] is None:
            entry["reasons"].append(
                "descent 能力未解析：" + kinematics["descent_gradient_source"]
            )
    return entry


def _cost_model(policy, cells=None, id_field="grid_id"):
    """Soft-cost configuration readiness.

    The soft cost vector is optional by design: with no component explicitly
    enabled the scalar cost still exists and reduces to ``distance`` alone.  That
    is a *ready* configuration, so this domain never blocks a run for that
    reason -- but it records, explicitly, that only distance is active and that
    energy stays ``pending_model``.

    An *enabled* mapped channel is different: it needs a provenance
    ``[0, 1]`` normalized index on every canonical cell.  Missing evidence blocks
    the run, because the planner has no hidden normalizer and must never read a
    missing soft field as a zero penalty.
    """

    components = (policy.get("cost_model") or {}).get("components") or {}
    enabled = sorted(
        name for name, item in components.items() if item.get("enabled") and name != "energy"
    )
    disabled = sorted(name for name, item in components.items() if not item.get("enabled"))
    invalid = sorted(
        name for name, item in components.items()
        if item.get("enabled") and name != "energy" and item.get("weight") is None
    )
    cells = list(cells or [])
    unresolved = unresolved_soft_channel_cells(cells, policy, id_field) if cells else {}
    entry = _entry(
        "cost_model", "ready", [], enabled_components=[name for name in enabled if name != "distance"],
        disabled_components=disabled,
        distance_component="always_enabled_with_weight_1",
        energy_status="pending_model",
        scalar_cost_semantics=(
            "sum_over_edges(length_m + sum_channels(weight_x_exposure_m)); "
            "exposure_m = length_m x mean_endpoint_normalized_index"
        ),
        weight_policy="explicit_finite_non_negative_weights_no_sum_cap_no_hidden_normalizer",
        heuristic_policy="plain_3d_geometric_distance_weights_never_enter_the_heuristic",
        soft_field_channels_checked=sorted(unresolved),
        semantics="weights_are_never_defaulted_never_recommended_and_never_negative",
    )
    if invalid:
        entry["status"] = "blocked"
        entry["reasons"].append(
            "启用但缺少非负 weight 的 component：" + ", ".join(invalid)
        )
    unresolved_summary = {
        channel: {"unresolved_cell_count": len(ids), "samples": ids[:20]}
        for channel, ids in unresolved.items() if ids
    }
    entry["unresolved_soft_channels"] = unresolved_summary
    if unresolved_summary:
        entry["status"] = "blocked"
        for channel, detail in sorted(unresolved_summary.items()):
            entry["reasons"].append(
                f"启用 soft channel {channel}，但 {detail['unresolved_cell_count']} 个 canonical cell "
                "缺少带 provenance 的 normalized index（unknown 不得当作 0 cost；"
                "planner 不提供隐式 normalizer）"
            )
    if not [name for name in enabled if name != "distance"]:
        entry["reasons"].append(
            "未启用任何 soft penalty component：scalar cost 目前等价于 distance 项（不是错误，但不含取舍）"
        )
    entry["reasons"].append("energy 保持 pending_model/disabled：V3 不发明 energy 公式")
    entry["reasons"].append("CNS 记录为 post_route_assessment 且 excluded_from_search_cost=true")
    entry["reasons"].append(
        "启发函数为纯 3D 几何距离：每条边代价 >= 边长，因此与 weight 之和无关，不存在 weight 上限"
    )
    return entry


def readiness_overall(readiness):
    statuses = [
        str(value.get("status"))
        for key, value in (readiness or {}).items()
        if key != "status" and isinstance(value, dict)
    ]
    if "blocked" in statuses:
        return "blocked"
    if "pending" in statuses:
        return "pending"
    return "ready"


__all__ = [
    "REQUIRED_POLICY_PARAMETERS", "REQUIRED_ENVIRONMENT_PROPERTIES",
    "READINESS_STATUSES", "aircraft_readiness", "cost_enabled_channels",
    "cost_model_readiness", "evaluate_v3_readiness", "policy_readiness",
    "readiness_overall", "unresolved_soft_channel_cells",
]
