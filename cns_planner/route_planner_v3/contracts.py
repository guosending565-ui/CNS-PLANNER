"""JSON-safe, versioned Route Planner V3 contracts.

Design rules encoded here:

* geometry is ``x/y/z`` where ``z`` is always EGM2008 orthometric height -- the
  project's existing canonical vertical reference (``domain.spatial_3d``);
* a Markov search state additionally carries ``grid_id``, ``altitude_index`` and
  ``heading_bin`` so that turn capability is a property of the *state*, not a
  post-processing check;
* **no safety parameter has a default**.  A missing or unconfirmed altitude
  bound, clearance, turn radius or climb capability must surface as
  ``pending_confirmation`` / ``not_ready`` instead of a guessed value;
* nothing here reads GDAL, QGIS or any file.  The algorithm only ever consumes a
  canonical :class:`V3CellEnvironment`.
"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import TypedDict

V3_MODEL_SCOPE = "three_dimensional_strategic_planning_v3a"

V3_STATE_SCHEMA_VERSION = "3.0-state"
V3_POLICY_SCHEMA_VERSION = "3.0-policy"
V3_ENVIRONMENT_SCHEMA_VERSION = "3.0-environment"
V3_RESULT_SCHEMA_VERSION = "3.0-strategic-result"
REFINEMENT_CORRIDOR_SCHEMA_VERSION = "3.0-refinement-corridor"
AIRCRAFT_MOTION_LIMITS_SCHEMA_VERSION = "3.0-aircraft-motion-limits"
V3_COST_VECTOR_SCHEMA_VERSION = "3.0-cost-vector"

V3_STRATEGIC_RESULT_DISCLAIMER = (
    "V3-A strategic candidate："
    "3D + heading 战略搜索结果，不是 final safe / validated operational route；"
    "未做 30 m 局部精化、未做 exact polygon/terrain 最终判定、未做 CNS 联合优化。"
)

#: The only statuses a V3-A result may carry.
V3_RESULT_STATUSES = (
    "strategic_candidate", "failed", "missing_data", "pending_confirmation", "not_ready",
)

#: Readiness statuses for the six independent readiness domains.
READINESS_STATUSES = ("ready", "pending", "blocked")

#: Soft-cost components.  They are reported as a vector, never auto-summed.
COST_COMPONENTS = ("distance", "population_risk", "traffic_risk", "building_exposure", "energy")

#: Data completeness of one canonical cell.
ENVIRONMENT_DATA_STATUSES = ("passed", "unknown", "restricted")

#: Explicit airspace classification of one canonical cell.
AIRSPACE_STATUSES = ("confirmed_allowed", "confirmed_restricted", "unknown")

#: Motion primitive kinds actually modelled in V3-A.
PRIMITIVE_KINDS = ("horizontal_level", "climb", "descend", "hover")

#: Building exposure reference height used by the documented normalization.
BUILDING_EXPOSURE_REFERENCE_M = 100.0
#: Vertical clearance that maps to "zero building exposure" at the current altitude.
BUILDING_EXPOSURE_CLEARANCE_M = 50.0


class V3State(TypedDict):
    """Markov search state: horizontal cell + altitude quantisation + heading bin."""

    grid_id: str
    altitude_index: int
    altitude_egm2008_m: float
    heading_bin: int
    x: float
    y: float
    z: float


class MotionPrimitive3D(TypedDict, total=False):
    """One deterministic 3D motion primitive with its recorded characteristics."""

    primitive_id: str
    kind: str
    grid_delta: list[int]
    altitude_delta_steps: int
    heading_change_deg: float
    requires_horizontal_motion: bool
    requires_altitude_change: bool


class AircraftMotionLimits(TypedDict, total=False):
    """Explicit aircraft kinematic capability.  No value is ever assumed."""

    schema_version: str
    aircraft_id: str
    max_climb_gradient: float | None
    max_descent_gradient: float | None
    max_climb_rate_mps: float | None
    max_descent_rate_mps: float | None
    planning_speed_mps: float | None
    min_turn_radius_m: float | None
    confirmed: bool
    status: str
    source: str


class V3PlanningPolicy(TypedDict, total=False):
    """Explicit planning/safety policy.  Missing values are never defaulted."""

    schema_version: str
    policy_id: str
    min_altitude_egm2008_m: float | None
    max_altitude_egm2008_m: float | None
    vertical_step_m: float | None
    terrain_clearance_m: float | None
    building_horizontal_clearance_m: float | None
    building_vertical_clearance_m: float | None
    aircraft_min_turn_radius_m: float | None
    max_climb_gradient: float | None
    max_descent_gradient: float | None
    max_climb_rate_mps: float | None
    max_descent_rate_mps: float | None
    planning_speed_mps: float | None
    heading_bin_count: int
    max_expanded_states: int
    cost_model: dict
    source: str
    confirmed: bool
    status: str
    missing_parameters: list[str]


class V3CellEnvironment(TypedDict, total=False):
    """Canonical per-cell environment consumed by the V3 kernel.

    ``terrain.surface_clearance_egm2008_m`` is the *maximum safe flight height
    floor* for the cell: ``max(surface_elevation_max) + terrain_clearance_m``.
    ``buildings.required_clearance_egm2008_m`` is
    ``roof_elevation_max + building_vertical_clearance_m``.  Both are
    EGM2008 orthometric and both fail closed when ``data_status != passed``.
    """

    schema_version: str
    status: str
    canonical_vertical_reference: str
    source_type: str
    source_detail: dict
    grid_level: int | None
    properties: dict
    cells: list[dict]


class V3CostVector(TypedDict, total=False):
    """Multi-component soft cost.  Raw units are never summed directly."""

    schema_version: str
    components: dict
    scalar_cost_available: bool
    scalar_cost: float | None
    scalar_cost_semantics: str
    scalar_weight_sum: float
    excluded_components: list[str]
    cns_integration: dict
    total_raw_is_not_a_sum_of_units: bool


class V3PlanningProblem(TypedDict, total=False):
    """Complete, JSON-safe planning problem handed to the V3 kernel."""

    schema_version: str
    problem_id: str
    route_id: str
    vertical_reference: str
    start: dict
    goal: dict
    policy: dict
    aircraft_motion_limits: dict
    environment: dict
    soft_cost_weights: dict
    expected_cns_assessment: dict
    provenance: dict


class V3Readiness(TypedDict, total=False):
    """Per-domain readiness with explicit reasons."""

    status: str
    aircraft: dict
    cost_model: dict
    airspace: dict
    terrain: dict
    building: dict
    policy: dict


class V3StrategicResult(TypedDict, total=False):
    """Result of one V3-A strategic search."""

    schema_version: str
    status: str
    algorithm_id: str
    algorithm_version: str
    model_scope: str
    problem_id: str
    route_id: str
    reason: str
    readiness: dict
    input_fingerprint: str
    effective_policy: dict
    heuristic_semantics: dict
    search_statistics: dict
    hard_constraint_summary: dict
    state_path: list[dict]
    horizontal_projection: list[list[float]]
    distance_m: float | None
    cost_vector: dict
    candidate_refinement_corridor: dict | None
    cns_assessment: dict
    disclaimer: str
    operational_route: bool
    final_validation_performed: bool
    semantics: dict


class CandidateRefinementCorridor(TypedDict, total=False):
    """Proposal window for a later local refinement stage -- not a safety volume."""

    schema_version: str
    corridor_id: str
    semantics: str
    ring_n: int
    refinement_cell_size_m: float | None
    center_grid_ids: list[str]
    center_altitude_indices: list[int]
    support_grid_ids: list[str]
    altitude_envelope: dict
    center_state_count: int
    support_cell_count: int
    n_ring_is_not_a_safety_clearance: bool
    next_stage: str


# --------------------------------------------------------------------------- helpers


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value))


def _optional_number(value, field, *, nonnegative=False, positive=False):
    if value in (None, ""):
        return None
    if not _finite(value):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if nonnegative and number < 0:
        raise ValueError(f"{field} 不得小于零")
    if positive and number <= 0:
        raise ValueError(f"{field} 必须大于零")
    return number


def _optional_int(value, field, *, minimum=None):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
        raise ValueError(f"{field} 必须是整数")
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} 不得小于 {minimum}")
    return number


def _text(value, default=""):
    return str(value if value is not None else default)


def _optional_bbox(value, grid_id):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"cell {grid_id} bbox 必须是 4 项")
    numbers = [float(item) for item in value]
    if not all(isfinite(item) for item in numbers):
        raise ValueError(f"cell {grid_id} bbox 必须是有限数值")
    return numbers


# --------------------------------------------------------------------------- policy


def default_v3_policy():
    """An intentionally *empty* policy: every safety parameter is unset."""

    return {
        "schema_version": V3_POLICY_SCHEMA_VERSION,
        "policy_id": "v3-policy-default-empty",
        "min_altitude_egm2008_m": None,
        "max_altitude_egm2008_m": None,
        "vertical_step_m": None,
        "terrain_clearance_m": None,
        "building_horizontal_clearance_m": None,
        "building_vertical_clearance_m": None,
        "aircraft_min_turn_radius_m": None,
        "max_climb_gradient": None,
        "max_descent_gradient": None,
        "max_climb_rate_mps": None,
        "max_descent_rate_mps": None,
        "planning_speed_mps": None,
        "heading_bin_count": 8,
        "max_expanded_states": 200000,
        "cost_model": default_v3_cost_model(),
        "source": "未配置；所有安全参数必须由项目工程依据显式提供",
        "confirmed": False,
        "status": "pending_confirmation",
        "missing_parameters": [],
    }


def default_v3_cost_model():
    """Soft costs are declared but disabled until explicitly enabled + weighted."""

    components = {}
    for name in COST_COMPONENTS:
        components[name] = {
            "enabled": False,
            "weight": None,
            "unit": {
                "distance": "m",
                "population_risk": "person_m",
                "traffic_risk": "aircraft_m",
                "building_exposure": "normalized_m",
                "energy": "not_modelled",
            }[name],
            "normalization": {
                "distance": "raw_metres",
                "population_risk": f"per_edge_raw_/{10000.0}_people",
                "traffic_risk": f"per_edge_raw_/{100.0}_aircraft",
                "building_exposure": f"clearance_ratio_over_{BUILDING_EXPOSURE_REFERENCE_M:.0f}_m",
                "energy": "not_modelled",
            }[name],
            "source": "policy.cost_model",
            "semantics": {
                "distance": "真实三维航段长度，唯一具有物理单位的直接项",
                "population_risk": "engineering_exposure_meter_person_not_probability",
                "traffic_risk": "engineering_exposure_meter_aircraft_not_conflict_probability",
                "building_exposure": "normalized_clearance_proximity_not_collision_probability",
                "energy": "pending_model_disabled",
            }[name],
        }
    components["energy"].update({
        "enabled": False, "weight": None, "status": "pending_model",
        "reason": "V3-A 不发明 energy 公式；energy 默认 pending_model 且禁用。",
    })
    return {
        "components": components,
        "scalar_cost_cap": None,
        "scalar_cost_cap_semantics": "optional_explicit_engineering_cap_never_defaulted",
    }


#: Parameters with no default.  Climb/descent capability and the turn radius are
#: required; a *rate* limit is an alternative source for the gradient (see
#: ``motion.kinematic_readiness``) and is therefore not itself required, while an
#: explicit planning speed is only needed when a rate is the chosen source.
_POLICY_DIRECT_PARAMETERS = (
    "min_altitude_egm2008_m", "max_altitude_egm2008_m", "vertical_step_m",
    "terrain_clearance_m", "building_horizontal_clearance_m",
    "building_vertical_clearance_m", "aircraft_min_turn_radius_m",
    "max_climb_gradient", "max_descent_gradient",
)


def normalize_v3_planning_policy(value=None):
    source = value if isinstance(value, dict) else {}
    result = default_v3_policy()
    result.update({
        "policy_id": _text(source.get("policy_id") or result["policy_id"]),
        "min_altitude_egm2008_m": _optional_number(source.get("min_altitude_egm2008_m"), "min_altitude_egm2008_m"),
        "max_altitude_egm2008_m": _optional_number(source.get("max_altitude_egm2008_m"), "max_altitude_egm2008_m"),
        "vertical_step_m": _optional_number(source.get("vertical_step_m"), "vertical_step_m", positive=True),
        "terrain_clearance_m": _optional_number(source.get("terrain_clearance_m"), "terrain_clearance_m", nonnegative=True),
        "building_horizontal_clearance_m": _optional_number(source.get("building_horizontal_clearance_m"), "building_horizontal_clearance_m", nonnegative=True),
        "building_vertical_clearance_m": _optional_number(source.get("building_vertical_clearance_m"), "building_vertical_clearance_m", nonnegative=True),
        "aircraft_min_turn_radius_m": _optional_number(source.get("aircraft_min_turn_radius_m"), "aircraft_min_turn_radius_m", nonnegative=True),
        "max_climb_gradient": _optional_number(source.get("max_climb_gradient"), "max_climb_gradient", nonnegative=True),
        "max_descent_gradient": _optional_number(source.get("max_descent_gradient"), "max_descent_gradient", nonnegative=True),
        "max_climb_rate_mps": _optional_number(source.get("max_climb_rate_mps"), "max_climb_rate_mps", nonnegative=True),
        "max_descent_rate_mps": _optional_number(source.get("max_descent_rate_mps"), "max_descent_rate_mps", nonnegative=True),
        "planning_speed_mps": _optional_number(source.get("planning_speed_mps"), "planning_speed_mps", positive=True),
        "heading_bin_count": _optional_int(source.get("heading_bin_count"), "heading_bin_count", minimum=4) or 8,
        "max_expanded_states": _optional_int(source.get("max_expanded_states"), "max_expanded_states", minimum=1) or 200000,
        "cost_model": normalize_cost_model(source.get("cost_model")),
        "source": _text(source.get("source") or result["source"]),
        "confirmed": bool(source.get("confirmed", False)),
    })
    bins = result["heading_bin_count"]
    if 360 % bins != 0:
        raise ValueError("heading_bin_count 必须整除 360")
    minimum, maximum = result["min_altitude_egm2008_m"], result["max_altitude_egm2008_m"]
    if minimum is not None and maximum is not None and maximum < minimum:
        raise ValueError("max_altitude_egm2008_m 不得低于 min_altitude_egm2008_m")
    result["missing_parameters"] = [
        name for name in _POLICY_DIRECT_PARAMETERS if result.get(name) is None
    ]
    if minimum is not None and maximum is not None and result["vertical_step_m"] is not None:
        if maximum > minimum and result["vertical_step_m"] > (maximum - minimum):
            raise ValueError("vertical_step_m 不得大于高度范围")
    result["status"] = (
        "confirmed"
        if result["confirmed"] and not result["missing_parameters"]
        else "pending_confirmation"
    )
    return result


def normalize_cost_model(value=None):
    source = value if isinstance(value, dict) else {}
    result = default_v3_cost_model()
    supplied = source.get("components") if isinstance(source.get("components"), dict) else {}
    for name in COST_COMPONENTS:
        entry = supplied.get(name) if isinstance(supplied.get(name), dict) else {}
        current = result["components"][name]
        enabled = bool(entry.get("enabled", current["enabled"]))
        weight = _optional_number(entry.get("weight"), f"cost_model.{name}.weight", nonnegative=True)
        if enabled and weight is None:
            raise ValueError(f"启用 component {name} 必须显式提供非负 weight")
        current.update({
            "enabled": enabled,
            "weight": weight,
            "source": _text(entry.get("source") or current["source"]),
        })
        if name == "energy":
            # The energy model does not exist in V3-A; it can never be enabled.
            current["enabled"] = False
            current["weight"] = None
            current["status"] = "pending_model"
            current["reason"] = "V3-A 不发明 energy 公式；energy 默认 pending_model 且禁用。"
    result["scalar_cost_cap"] = _optional_number(
        source.get("scalar_cost_cap"), "scalar_cost_cap", nonnegative=True,
    )
    return result


def describe_altitude_states(policy):
    """Discretise the explicit altitude band; never invents bounds or a step."""

    minimum, maximum = policy.get("min_altitude_egm2008_m"), policy.get("max_altitude_egm2008_m")
    step = policy.get("vertical_step_m")
    if minimum is None or maximum is None or step is None:
        return []
    count = int(round((maximum - minimum) / step)) + 1
    return [
        {
            "altitude_index": index,
            "altitude_egm2008_m": round(minimum + index * step, 9),
            "vertical_reference": "egm2008_orthometric",
        }
        for index in range(max(count, 1))
    ]


# --------------------------------------------------------------------------- aircraft


def default_aircraft_motion_limits():
    return {
        "schema_version": AIRCRAFT_MOTION_LIMITS_SCHEMA_VERSION,
        "aircraft_id": "",
        "max_climb_gradient": None,
        "max_descent_gradient": None,
        "max_climb_rate_mps": None,
        "max_descent_rate_mps": None,
        "planning_speed_mps": None,
        "min_turn_radius_m": None,
        "confirmed": False,
        "status": "pending_confirmation",
        "source": "未配置；未从 Aircraft 目录或 cruise speed 借用任何运动能力",
    }


def normalize_aircraft_motion_limits(value=None):
    source = value if isinstance(value, dict) else {}
    result = default_aircraft_motion_limits()
    result.update({
        "aircraft_id": _text(source.get("aircraft_id")),
        "max_climb_gradient": _optional_number(source.get("max_climb_gradient"), "max_climb_gradient", nonnegative=True),
        "max_descent_gradient": _optional_number(source.get("max_descent_gradient"), "max_descent_gradient", nonnegative=True),
        "max_climb_rate_mps": _optional_number(source.get("max_climb_rate_mps"), "max_climb_rate_mps", nonnegative=True),
        "max_descent_rate_mps": _optional_number(source.get("max_descent_rate_mps"), "max_descent_rate_mps", nonnegative=True),
        "planning_speed_mps": _optional_number(source.get("planning_speed_mps"), "planning_speed_mps", positive=True),
        "min_turn_radius_m": _optional_number(source.get("min_turn_radius_m"), "min_turn_radius_m", nonnegative=True),
        "confirmed": bool(source.get("confirmed", False)),
        "source": _text(source.get("source") or result["source"]),
    })
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    return result


# --------------------------------------------------------------------------- environment


def empty_v3_cell_environment(status="missing_data", reason="没有可用的 canonical V3CellEnvironment"):
    return {
        "schema_version": V3_ENVIRONMENT_SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "canonical_vertical_reference": "egm2008_orthometric",
        "source_type": None,
        "source_detail": {},
        "grid_level": None,
        "properties": {
            "cell_size_m": None,
            "terrain_clearance_m": None,
            "building_horizontal_clearance_m": None,
            "building_vertical_clearance_m": None,
        },
        "cells": [],
    }


def normalize_v3_cell_environment(value=None):
    if not isinstance(value, dict):
        return empty_v3_cell_environment()
    source = value
    reference = _text(source.get("canonical_vertical_reference") or "egm2008_orthometric")
    if reference != "egm2008_orthometric":
        raise ValueError("V3 只接受 canonical vertical reference egm2008_orthometric")
    cells = []
    for raw in source.get("cells") or []:
        cells.append(_normalize_environment_cell(raw))
    return {
        "schema_version": V3_ENVIRONMENT_SCHEMA_VERSION,
        "status": _text(source.get("status") or ("passed" if cells else "missing_data")),
        "reason": source.get("reason"),
        "canonical_vertical_reference": reference,
        "source_type": _text(source.get("source_type") or "unspecified"),
        "source_detail": deepcopy(source.get("source_detail") or {}),
        "grid_level": _optional_int(source.get("grid_level"), "grid_level"),
        "properties": deepcopy(source.get("properties") or {}),
        "cells": cells,
    }


def _normalize_environment_cell(raw):
    if not isinstance(raw, dict):
        raise ValueError("V3CellEnvironment cell 必须是对象")
    grid_id = _text(raw.get("grid_id")).strip()
    if not grid_id:
        raise ValueError("V3CellEnvironment cell 缺少 grid_id")
    center = raw.get("center")
    if not isinstance(center, (list, tuple)) or len(center) < 2:
        raise ValueError(f"V3CellEnvironment cell {grid_id} 缺少 center")
    x, y = (float(center[0]), float(center[1]))
    if not (_finite(x) and _finite(y)):
        raise ValueError(f"V3CellEnvironment cell {grid_id} center 必须是有限数值")
    terrain = raw.get("terrain") if isinstance(raw.get("terrain"), dict) else {}
    buildings = raw.get("buildings") if isinstance(raw.get("buildings"), dict) else {}
    airspace = raw.get("airspace") if isinstance(raw.get("airspace"), dict) else {}
    cell_size = _optional_number(raw.get("cell_size_m"), "cell_size_m", positive=True)
    terrain_status = _text(terrain.get("data_status") or "unknown")
    building_status = _text(buildings.get("data_status") or "unknown")
    airspace_status = _text(airspace.get("status") or "unknown")
    if terrain_status not in ENVIRONMENT_DATA_STATUSES:
        raise ValueError(f"cell {grid_id} terrain.data_status 无效：{terrain_status}")
    if building_status not in ENVIRONMENT_DATA_STATUSES:
        raise ValueError(f"cell {grid_id} buildings.data_status 无效：{building_status}")
    if airspace_status not in AIRSPACE_STATUSES:
        raise ValueError(f"cell {grid_id} airspace.status 无效：{airspace_status}")
    return {
        "grid_id": grid_id,
        "center": [x, y],
        "cell_size_m": cell_size,
        # The canonical grid index travels with the cell so the state space keeps a
        # deterministic Chebyshev topology instead of re-deriving it from geometry.
        "level": _optional_int(raw.get("level"), "level"),
        "column": _optional_int(raw.get("column"), "column"),
        "row": _optional_int(raw.get("row"), "row"),
        "bbox": _optional_bbox(raw.get("bbox"), grid_id),
        "terrain": {
            "data_status": terrain_status,
            "surface_elevation_max_egm2008_m": _optional_number(
                terrain.get("surface_elevation_max_egm2008_m"), "surface_elevation_max_egm2008_m",
            ),
            "surface_clearance_egm2008_m": _optional_number(
                terrain.get("surface_clearance_egm2008_m"), "surface_clearance_egm2008_m",
            ),
        },
        "buildings": {
            "data_status": building_status,
            "roof_elevation_max_egm2008_m": _optional_number(
                buildings.get("roof_elevation_max_egm2008_m"), "roof_elevation_max_egm2008_m",
            ),
            "required_clearance_egm2008_m": _optional_number(
                buildings.get("required_clearance_egm2008_m"), "required_clearance_egm2008_m",
            ),
            "horizontal_clearance_m": _optional_number(
                buildings.get("horizontal_clearance_m"), "horizontal_clearance_m", nonnegative=True,
            ),
        },
        "airspace": {
            "status": airspace_status,
            "feature_id": _text(airspace.get("feature_id")) or None,
        },
    }


# --------------------------------------------------------------------------- state


def v3_state_id(grid_id, altitude_index, heading_bin):
    return f"{grid_id}|{int(altitude_index)}|{int(heading_bin)}"


def build_v3_state(grid_id, altitude_index, altitude_egm2008_m, heading_bin, x, y):
    return {
        "grid_id": _text(grid_id),
        "altitude_index": int(altitude_index),
        "altitude_egm2008_m": float(altitude_egm2008_m),
        "heading_bin": int(heading_bin),
        "x": float(x),
        "y": float(y),
        "z": float(altitude_egm2008_m),
    }


def heading_bin_for_bearing(bearing_deg, bin_count):
    count = _optional_int(bin_count, "heading_bin_count", minimum=4)
    if count is None:
        raise ValueError("heading_bin_count 缺失")
    step = 360.0 / float(count)
    return int(round((float(bearing_deg) % 360.0) / step)) % count


def heading_bin_center_deg(heading_bin, bin_count):
    step = 360.0 / float(bin_count)
    return round((float(heading_bin) % int(bin_count)) * step, 9)


# --------------------------------------------------------------------------- problem


def normalize_v3_planning_problem(value=None):
    source = value if isinstance(value, dict) else {}
    policy = normalize_v3_planning_policy(source.get("policy"))
    aircraft = normalize_aircraft_motion_limits(source.get("aircraft_motion_limits"))
    environment = normalize_v3_cell_environment(source.get("environment"))
    start = _normalize_point(source.get("start"), "start")
    goal = _normalize_point(source.get("goal"), "goal")
    weights = {}
    for name, raw in (source.get("soft_cost_weights") or {}).items():
        if name not in COST_COMPONENTS:
            raise ValueError(f"未知 soft cost component：{name}")
        weights[name] = _optional_number(raw, f"soft_cost_weights.{name}", nonnegative=True)
    return {
        "schema_version": V3_STATE_SCHEMA_VERSION,
        "problem_id": _text(source.get("problem_id") or "v3-problem"),
        "route_id": _text(source.get("route_id")),
        "vertical_reference": "egm2008_orthometric",
        "start": start,
        "goal": goal,
        "policy": policy,
        "aircraft_motion_limits": aircraft,
        "environment": environment,
        "soft_cost_weights": weights,
        "expected_cns_assessment": deepcopy(source.get("expected_cns_assessment") or {
            "integration_mode": "post_route_assessment",
            "excluded_from_search_cost": True,
            "semantics": "V3 第一阶段 CNS 不进搜索；Route Planning → CNS Assessment。",
        }),
        "provenance": deepcopy(source.get("provenance") or {}),
    }


def _normalize_point(value, field):
    """Accept ``[x, y]`` / ``[x, y, z]`` or an already normalized point mapping.

    ``z`` is optional.  When it is **not** supplied the endpoint carries no altitude
    requirement at all: the search reaches the endpoint cell at whatever altitude is
    feasible, and the achieved altitude is reported in the path.  When ``z`` *is*
    supplied it becomes a hard requirement, so an endpoint that cannot legally hold
    that altitude fails instead of being silently adjusted.
    """

    if isinstance(value, dict):
        candidate = [value.get("x"), value.get("y")]
        if value.get("z") is not None:
            candidate.append(value.get("z"))
        value = candidate
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError(f"{field} 必须是 [x, y] 或 [x, y, z]")
    x, y = float(value[0]), float(value[1])
    if not (_finite(x) and _finite(y)):
        raise ValueError(f"{field} x/y 必须是有限数值")
    z = None if len(value) < 3 or value[2] in (None, "") else float(value[2])
    if z is not None and not _finite(z):
        raise ValueError(f"{field} z 必须是有限数值")
    return {
        "x": x, "y": y, "z": z, "vertical_reference": "egm2008_orthometric",
        "z_is_explicit": z is not None,
        "altitude_requirement": (
            "explicit_z_required_at_endpoint" if z is not None
            else "no_endpoint_altitude_requirement_any_feasible_altitude"
        ),
    }


# --------------------------------------------------------------------------- cost vector


def normalize_v3_cost_vector(value=None):
    source = value if isinstance(value, dict) else {}
    components = {}
    for name in COST_COMPONENTS:
        raw = (source.get("components") or {}).get(name)
        entry = raw if isinstance(raw, dict) else {}
        components[name] = {
            "raw": _optional_number(entry.get("raw"), f"cost_vector.{name}.raw"),
            "normalized": _optional_number(entry.get("normalized"), f"cost_vector.{name}.normalized"),
            "weight": _optional_number(entry.get("weight"), f"cost_vector.{name}.weight", nonnegative=True),
            "contribution": _optional_number(entry.get("contribution"), f"cost_vector.{name}.contribution"),
            "unit": entry.get("unit"),
            "source": entry.get("source"),
            "semantics": entry.get("semantics"),
            "enabled": bool(entry.get("enabled", False)),
            "status": _text(entry.get("status") or "not_calculated"),
            "reason": entry.get("reason"),
        }
    return {
        "schema_version": V3_COST_VECTOR_SCHEMA_VERSION,
        "components": components,
        "scalar_cost_available": bool(source.get("scalar_cost_available", False)),
        "scalar_cost": _optional_number(source.get("scalar_cost"), "scalar_cost"),
        "scalar_cost_semantics": _text(
            source.get("scalar_cost_semantics")
            or "distance_m_plus_explicitly_normalized_non_negative_weighted_components"
        ),
        "scalar_weight_sum": _optional_number(source.get("scalar_weight_sum"), "scalar_weight_sum", nonnegative=True) or 0.0,
        "excluded_components": list(source.get("excluded_components") or []),
        "cns_integration": deepcopy(source.get("cns_integration") or {
            "integration_mode": "post_route_assessment",
            "excluded_from_search_cost": True,
        }),
        "total_raw_is_not_a_sum_of_units": True,
    }


# --------------------------------------------------------------------------- corridor


def empty_candidate_refinement_corridor():
    return {
        "schema_version": REFINEMENT_CORRIDOR_SCHEMA_VERSION,
        "corridor_id": None,
        "semantics": "refinement_search_window_not_safety_corridor",
        "ring_n": 0,
        "refinement_cell_size_m": None,
        "center_grid_ids": [],
        "center_altitude_indices": [],
        "support_grid_ids": [],
        "altitude_envelope": {
            "lower_altitude_egm2008_m": None,
            "upper_altitude_egm2008_m": None,
            "vertical_reference": "egm2008_orthometric",
            "explicit_margin_m": None,
            "semantics": "planned_altitude_extent_not_a_clearance_volume",
        },
        "center_state_count": 0,
        "support_cell_count": 0,
        "n_ring_is_not_a_safety_clearance": True,
        "next_stage": "V3-B_30m_local_refinement_and_exact_validation",
    }


def normalize_candidate_refinement_corridor(value=None):
    result = empty_candidate_refinement_corridor()
    if not isinstance(value, dict):
        return result
    source = value
    envelope = source.get("altitude_envelope") if isinstance(source.get("altitude_envelope"), dict) else {}
    result.update({
        "corridor_id": None if source.get("corridor_id") in (None, "") else _text(source.get("corridor_id")),
        # The semantics and the N-ring disclaimer are contract constants: they can
        # never be re-labelled by a caller, so no consumer can mistake the corridor
        # for a safety volume.
        "semantics": result["semantics"],
        "ring_n": _optional_int(source.get("ring_n"), "ring_n", minimum=0) or 0,
        "refinement_cell_size_m": _optional_number(
            source.get("refinement_cell_size_m"), "refinement_cell_size_m", positive=True,
        ),
        "center_grid_ids": [_text(item) for item in source.get("center_grid_ids") or []],
        "center_altitude_indices": [
            int(item) for item in source.get("center_altitude_indices") or []
        ],
        "support_grid_ids": [_text(item) for item in source.get("support_grid_ids") or []],
        "altitude_envelope": {
            "lower_altitude_egm2008_m": _optional_number(
                envelope.get("lower_altitude_egm2008_m"), "lower_altitude_egm2008_m",
            ),
            "upper_altitude_egm2008_m": _optional_number(
                envelope.get("upper_altitude_egm2008_m"), "upper_altitude_egm2008_m",
            ),
            "vertical_reference": "egm2008_orthometric",
            "explicit_margin_m": _optional_number(
                envelope.get("explicit_margin_m"), "explicit_margin_m", nonnegative=True,
            ),
            "semantics": "planned_altitude_extent_not_a_clearance_volume",
        },
        "center_state_count": int(source.get("center_state_count") or 0),
        "support_cell_count": int(source.get("support_cell_count") or 0),
        "n_ring_is_not_a_safety_clearance": True,
        "next_stage": _text(source.get("next_stage") or result["next_stage"]),
    })
    return result


# --------------------------------------------------------------------------- result


def v3_readiness_summary(readiness):
    """Aggregate domain readiness without hiding the individual reasons."""

    source = readiness if isinstance(readiness, dict) else {}
    domains = {
        key: value for key, value in source.items()
        if key not in ("status", "overall_status") and isinstance(value, dict)
    }
    statuses = [str(value.get("status") or "pending") for value in domains.values()]
    if "blocked" in statuses:
        overall = "blocked"
    elif statuses and all(value == "ready" for value in statuses):
        overall = "ready"
    else:
        overall = "pending"
    return {
        "status": overall,
        "airspace": source.get("airspace"),
        "terrain": source.get("terrain"),
        "building": source.get("building"),
        "policy": source.get("policy"),
        "aircraft": source.get("aircraft"),
        "cost_model": source.get("cost_model"),
    }


def empty_v3_strategic_result(status="not_ready"):
    return {
        "schema_version": V3_RESULT_SCHEMA_VERSION,
        "status": status if status in V3_RESULT_STATUSES else "not_ready",
        "algorithm_id": "route_planner_v3_strategic",
        "algorithm_version": "3.0-alpha",
        "model_scope": V3_MODEL_SCOPE,
        "problem_id": None,
        "route_id": None,
        "reason": None,
        "readiness": None,
        "input_fingerprint": None,
        "effective_policy": None,
        "heuristic_semantics": None,
        "search_statistics": {
            "expanded_states": 0, "generated_states": 0, "rejected_states": 0,
            "expanded_transitions": 0, "runtime_ms": 0.0,
            "expansion_cap": None, "expansion_cap_reached": False,
        },
        "hard_constraint_summary": {
            "state_rejections": {}, "transition_rejections": {},
            "total_state_rejections": 0, "total_transition_rejections": 0,
        },
        "state_path": [],
        "horizontal_projection": [],
        "distance_m": None,
        "cost_vector": normalize_v3_cost_vector(None),
        "candidate_refinement_corridor": None,
        "cns_assessment": {
            "integration_mode": "post_route_assessment",
            "evaluated": False,
            "excluded_from_search_cost": True,
            "semantics": "V3 第一阶段 CNS 不进搜索；Route Planning → CNS Assessment。",
        },
        "disclaimer": V3_STRATEGIC_RESULT_DISCLAIMER,
        "operational_route": False,
        "final_validation_performed": False,
        "semantics": {
            "scope": V3_MODEL_SCOPE,
            "not_final_safe": True,
            "not_validated_operational_route": True,
            "unknown_is_never_safe": True,
            "motion_model": "engineering_kinematic_limits_implemented_in_edge_generation_not_flight_dynamics_certification",
        },
    }


def normalize_v3_strategic_result(value=None):
    result = empty_v3_strategic_result()
    if not isinstance(value, dict):
        return result
    source = value
    status = _text(source.get("status") or "not_ready")
    result.update(deepcopy(source))
    result["status"] = status if status in V3_RESULT_STATUSES else "not_ready"
    result["schema_version"] = V3_RESULT_SCHEMA_VERSION
    result["model_scope"] = V3_MODEL_SCOPE
    result["cost_vector"] = normalize_v3_cost_vector(source.get("cost_vector"))
    result["candidate_refinement_corridor"] = (
        normalize_candidate_refinement_corridor(source.get("candidate_refinement_corridor"))
        if source.get("candidate_refinement_corridor") else None
    )
    result["state_path"] = deepcopy(source.get("state_path") or [])
    result["horizontal_projection"] = deepcopy(source.get("horizontal_projection") or [])
    result["disclaimer"] = V3_STRATEGIC_RESULT_DISCLAIMER
    result["operational_route"] = False
    result["final_validation_performed"] = False
    return result


def empty_v3_experimental_session():
    """Independent additive container.  Never the operational planner result."""

    return {
        "status": "not_calculated",
        "collection_id": "route-planner-v3-experiments",
        "schema_version": 1,
        "active_experiment_id": None,
        "count": 0,
        "records": [],
        "note": (
            "V3-A 实验容器：只保存 3D 战略候选与 readiness 证据；"
            "不写入 operational_routes，不切换 algorithm_selection，"
            "也不修改 V1/V2 或 spatial_3d 语义。"
        ),
    }


def normalize_v3_experiments(value=None):
    result = empty_v3_experimental_session()
    if value is None:
        return result
    if not isinstance(value, dict):
        raise ValueError("route_planner_v3_experiments 必须是对象")
    records = []
    for raw in value.get("records") or []:
        if not isinstance(raw, dict):
            raise ValueError("V3 experiment record 必须是对象")
        entry = deepcopy(raw)
        entry.setdefault("experiment_id", "V3-UNKNOWN")
        entry["result"] = normalize_v3_strategic_result(entry.get("result"))
        entry.setdefault("readiness", (entry["result"] or {}).get("readiness"))
        entry.setdefault("policy", None)
        entry.setdefault("environment_source", None)
        entry.setdefault("current_applicability", "unknown")
        records.append(entry)
    result["records"] = records
    result["count"] = len(records)
    active = value.get("active_experiment_id")
    result["active_experiment_id"] = active if active in {item["experiment_id"] for item in records} else (records[0]["experiment_id"] if records else None)
    result["status"] = "passed" if records else "not_calculated"
    return result
