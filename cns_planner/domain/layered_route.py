"""JSON-safe contracts for the Layered Risk-Aware Route Planner V1.

The production route is ``scenario/OD route -> explicit AltitudeLayer -> terrain/building
feasibility mask -> MH/T L8 A* -> Risk Framework V2 soft cost -> LayeredRouteCandidate``.

Hard boundaries encoded here (all of them are enforced by code, not by convention):

* the cruise altitude layer is **explicitly selected** — never inferred from a
  ``RouteAltitudeProfile`` and never given a default value;
* ``terrain_vertical_clearance_m`` has **no default**: ``null`` is "not yet confirmed by
  engineering", and ``null != 0`` (an explicit ``0`` is legal and means "zero clearance");
* the building vertical clearance is **not** redefined here — the single canonical
  definition is ``domain/building_clearance.py`` (``building_roof_elevation`` /
  ``evaluate_vertical_clearance``) and its confirmed project policy;
* this is a ``coarse_strategic_vertical_envelope``, not an exact footprint or horizontal
  clearance verdict; horizontal/exact building clearance belongs to the later continuous
  validation stage;
* a candidate is never an operational route (``operational_route=false``,
  ``continuous_validation_required=true``) and never creates a ``RouteOperatingLayer``;
* current airspace stays ``display_only`` and never enters the mask, the search or a
  fingerprint.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from numbers import Real

SCHEMA_VERSION = "layered-route-planner-v1"

#: The Risk Framework V2 domains that may carry a soft cost weight.
COST_DOMAIN_IDS = ("ground", "air_traffic", "environment_obstacle")
DOMAIN_LABELS = {
    "ground": "Ground（地面暴露）",
    "air_traffic": "Air / Traffic（空中交通暴露）",
    "environment_obstacle": "Environment / Obstacle（工程环境-障碍物）",
}
#: ``grid_risk_v2`` cell 的 canonical domain 容器是 nested ``cell["domains"][domain_id]``。
#: 读取规则**只有一份**实现：``cns_planner.risk.accessors_v2``（planner cost / profiler /
#: profile fingerprint 共用）。本契约模块不再声明任何 flat ``cell[domain_id]`` 映射。
DOMAIN_RISK_CELL_CONTAINER_KEY = "domains"

#: Feasibility verdict vocabulary of the layer mask.
FEASIBILITY_STATUSES = ("feasible", "blocked", "unknown")
#: Candidate status vocabulary.  ``blocked`` covers a missing/unconfirmed input, a fully
#: blocked mask and an unreachable goal; a blocked result never claims infeasibility proof.
CANDIDATE_STATUSES = (
    "candidate", "blocked", "missing_data", "stale", "not_ready", "search_incomplete",
)

REQUEST_PENDING_SOURCE = "未记录"
FEASIBILITY_POLICY_PENDING_SOURCE = "未配置；必须由项目工程依据显式确认 terrain_vertical_clearance_m"
COST_POLICY_PENDING_SOURCE = "未配置；Layered Route Planner cost weight 必须由项目工程依据显式确认"
BUILDING_CLEARANCE_PENDING_SOURCE = "未配置；必须由项目工程依据确认"

FEASIBILITY_REASON_CODES = (
    "terrain_data_unavailable",
    "terrain_clearance_missing",
    "terrain_clearance_not_confirmed",
    "altitude_below_terrain_floor",
    "building_grid_missing_or_outside_coverage",
    "building_height_or_ground_elevation_unresolved",
    "altitude_below_building_clearance_floor",
    "building_vertical_clearance_not_confirmed",
)

#: The mask is a strategic vertical envelope: it answers "is this whole L8 cell above the
#: conservative floor?", not "is the exact footprint clear?".
COARSE_ENVELOPE_SEMANTICS = {
    "feasibility_scope": "coarse_strategic_vertical_envelope",
    "not_exact_footprint": True,
    "not_horizontal_clearance": True,
    "horizontal_clearance_deferred_to_continuous_validation": True,
    "terrain_floor": "intersecting_fabdem_cell_max_egm2008_plus_explicit_terrain_clearance",
    "terrain_sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
    "building_floor": (
        "terrain_cell_max_plus_verified_building_height_max_plus_existing_confirmed_"
        "building_vertical_clearance"
    ),
    "building_clearance_definition": "domain/building_clearance.py (single canonical roof rule)",
    "zero_buildings_means_no_vertical_building_constraint": True,
    "partial_height_coverage_is_unknown_never_zero": True,
    "unknown_is_never_feasible": True,
    "airspace": "display_only_not_used_for_feasibility",
}

POLICY_SEMANTICS = {
    "null_is_not_zero": True,
    "explicit_zero_is_legal": True,
    "no_default_clearance_or_lambda": True,
    "pending_policy_blocks_planning": True,
    "confirmed_requires_explicit_source": True,
    "cost_domain_disabled_iff_lambda_is_zero": True,
    "enabled_domain_requires_resolved_v2_index": True,
    "no_unknown_penalty_and_no_default_risk": True,
    "airspace_is_not_a_policy_input": True,
}

CANDIDATE_SEMANTICS = {
    "artifact": "layered_route_candidate",
    "not_an_operational_route": True,
    "never_writes_operational_routes_or_cns": True,
    "never_creates_route_operating_layer": True,
    "requires_continuous_validation_before_any_operational_use": True,
    "fixed_cruise_layer_is_explicitly_selected": True,
    "vertical_transition_never_enters_horizontal_search": True,
    "single_layer_search_no_cross_layer_edge": True,
    "risk_is_v2_domain_relative_engineering_index": True,
    "risk_v2_overall_not_used": True,
    "clearance_breach_is_feasibility_not_risk": True,
    "airspace_is_display_only": True,
}


# --------------------------------------------------------------------------- helpers


def stable_fingerprint(value, *, prefix=""):
    """Deterministic fingerprint for a JSON-safe value."""

    return prefix + sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
    ).hexdigest()


def _json_object(value, field):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 JSON-safe 对象") from exc


def _optional_json_object(value, field):
    return None if value in (None, "") else _json_object(value, field)


def _text(value, default=""):
    text = str(value or "").strip()
    return text or default


def _optional_text(value):
    text = str(value or "").strip()
    return text or None


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _optional_number(value, field):
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _optional_nonnegative(value, field):
    number = _optional_number(value, field)
    if number is not None and number < 0:
        raise ValueError(f"{field} 必须是有限非负数")
    return number


def _confirmed_with_source(value, source, field):
    confirmed = bool(value)
    if confirmed and not source:
        raise ValueError(f"{field} 声明 confirmed 时必须提供显式 source")
    return confirmed


# --------------------------------------------------------------------------- request


def default_layered_route_request():
    """No scenario route, no OD pair and no altitude layer is ever assumed."""

    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": None,
        "scenario_route_id": None,
        "start_node_id": None,
        "end_node_id": None,
        "altitude_layer_id": None,
        "source": REQUEST_PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "status": "pending_confirmation",
        "status_reason": "route_identity_not_selected",
        "parameter_status": "no_default_altitude_layer",
        "layer_resolution": "not_resolved",
        "explicit_layer_selection_only": True,
    }


def normalize_layered_route_request(value):
    """Validate one explicit layered planning request.

    The altitude layer must be selected explicitly.  A route identity is either a
    ``scenario_route_id`` or an explicit OD node pair; neither is ever inferred.
    """

    source = value if isinstance(value, dict) else {}
    result = default_layered_route_request()
    route_id = _optional_text(source.get("scenario_route_id"))
    start = _optional_text(source.get("start_node_id"))
    end = _optional_text(source.get("end_node_id"))
    if route_id and (start or end):
        raise ValueError("scenario_route_id 与显式 OD 节点对不得同时提供")
    source_text = _text(source.get("source"), REQUEST_PENDING_SOURCE)
    explicit_source = source_text != REQUEST_PENDING_SOURCE
    confirmed = _confirmed_with_source(source.get("confirmed"), explicit_source, "planning request")
    layer_id = _optional_text(source.get("altitude_layer_id"))
    result.update({
        "request_id": _optional_text(source.get("request_id")),
        "scenario_route_id": route_id,
        "start_node_id": start,
        "end_node_id": end,
        "altitude_layer_id": layer_id,
        "source": source_text,
        "evidence": _optional_json_object(source.get("evidence"), "evidence"),
        "confirmed": confirmed,
    })
    if route_id is None and not (start and end):
        result["status"] = "pending_confirmation"
        result["status_reason"] = "route_identity_not_selected"
        return result
    if layer_id is None:
        result["status"] = "blocked"
        result["status_reason"] = "altitude_layer_not_explicitly_selected"
        return result
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "planning_request_not_confirmed"
        return result
    if not explicit_source:
        raise ValueError("planning request 声明 confirmed 时必须提供显式 source")
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def request_fingerprint(request):
    return stable_fingerprint(
        {
            "scenario_route_id": (request or {}).get("scenario_route_id"),
            "start_node_id": (request or {}).get("start_node_id"),
            "end_node_id": (request or {}).get("end_node_id"),
            "altitude_layer_id": (request or {}).get("altitude_layer_id"),
            "source": (request or {}).get("source"),
            "confirmed": bool((request or {}).get("confirmed")),
        },
        prefix="layeredreqv1-",
    )


# --------------------------------------------------------------- altitude layer resolution
#
# ``AltitudeLayer`` stays the single existing contract in ``domain/spatial_3d.py``.  This
# resolver only answers "can the selected layer be read as a canonical EGM2008 cruise
# altitude?"; an AGL/WGS84 layer without an explicit conversion is reported blocked.  No
# datum, geoid or height is ever guessed.


def resolve_cruise_altitude(layer, *, surface_elevation_m=None, geoid_undulation_m=None):
    """Resolve one explicit ``AltitudeLayer`` into a canonical EGM2008 cruise altitude."""

    item = layer if isinstance(layer, dict) else {}
    layer_id = _optional_text(item.get("altitude_layer_id"))
    result = {
        "altitude_layer_id": layer_id,
        "status": "blocked",
        "altitude_egm2008_m": None,
        "nominal_altitude_m": _optional_number(item.get("nominal_altitude_m"), "nominal_altitude_m"),
        "vertical_reference": _optional_text(item.get("vertical_reference")) or "unknown",
        "declared_status": item.get("status"),
        "conversion": None,
        "reason": None,
    }
    if layer_id is None:
        result["reason"] = "altitude_layer_missing"
        return result
    # A layer without an explicit nominal altitude is reported as such first: the missing
    # value is the root cause, and the layer is (correctly) also still pending.
    if result["nominal_altitude_m"] is None:
        result["reason"] = "nominal_altitude_missing"
        return result
    if str(item.get("status") or "") != "confirmed":
        result["reason"] = "altitude_layer_pending_confirmation"
        return result
    nominal = result["nominal_altitude_m"]
    reference = result["vertical_reference"]
    if reference == "unknown":
        result["reason"] = "vertical_reference_unknown"
        return result
    if reference == "egm2008_orthometric":
        result["status"] = "confirmed"
        result["altitude_egm2008_m"] = nominal
        result["conversion"] = "already_canonical_egm2008_orthometric"
        return result
    if reference == "agl":
        surface = _optional_number(surface_elevation_m, "surface_elevation_m")
        if surface is None:
            result["reason"] = "agl_requires_explicit_dem_surface_elevation"
            return result
        result["status"] = "confirmed"
        result["altitude_egm2008_m"] = surface + nominal
        result["conversion"] = "agl_plus_explicit_egm2008_surface_elevation"
        return result
    if reference == "wgs84_ellipsoidal":
        undulation = _optional_number(geoid_undulation_m, "geoid_undulation_m")
        if undulation is None:
            result["reason"] = "wgs84_ellipsoidal_requires_explicit_geoid_undulation"
            return result
        result["status"] = "confirmed"
        result["altitude_egm2008_m"] = nominal - undulation
        result["conversion"] = "ellipsoidal_minus_explicit_geoid_undulation"
        return result
    result["reason"] = f"unsupported_vertical_reference:{reference}"
    return result


# --------------------------------------------------------------------------- policies


def default_layered_route_feasibility_policy():
    """No default terrain clearance exists: the policy ships ``blocked``."""

    return {
        "schema_version": SCHEMA_VERSION,
        "terrain_vertical_clearance_m": None,
        "source": FEASIBILITY_POLICY_PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "status": "blocked",
        "status_reason": "terrain_vertical_clearance_not_configured",
        "parameter_status": "no_default_clearance",
        "building_clearance_policy_role": "domain/building_clearance.py::building_clearance_policy",
        "building_clearance_is_reused_not_redefined": True,
    }


def normalize_layered_route_feasibility_policy(value):
    source = value if isinstance(value, dict) else {}
    result = default_layered_route_feasibility_policy()
    clearance = _optional_nonnegative(
        source.get("terrain_vertical_clearance_m"), "terrain_vertical_clearance_m",
    )
    source_text = _text(source.get("source"), FEASIBILITY_POLICY_PENDING_SOURCE)
    explicit_source = source_text != FEASIBILITY_POLICY_PENDING_SOURCE
    confirmed = _confirmed_with_source(
        source.get("confirmed"), explicit_source, "feasibility policy",
    )
    result.update({
        "terrain_vertical_clearance_m": clearance,
        "source": source_text,
        "evidence": _optional_json_object(source.get("evidence"), "evidence"),
        "confirmed": confirmed,
    })
    if clearance is None:
        result["status"] = "blocked"
        result["status_reason"] = "terrain_vertical_clearance_not_configured"
        return result
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "feasibility_policy_not_confirmed"
        return result
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def default_layered_route_cost_policy():
    """Every cost weight defaults to ``null`` (pending) — ``null != 0``."""

    return {
        "schema_version": SCHEMA_VERSION,
        "ground_lambda": None,
        "air_traffic_lambda": None,
        "environment_obstacle_lambda": None,
        "source": COST_POLICY_PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "status": "pending_confirmation",
        "status_reason": "cost_weights_not_configured",
        "parameter_status": "no_default_lambda",
        "null_is_not_zero": True,
        "explicit_zero_is_legal": True,
    }


def normalize_layered_route_cost_policy(value):
    source = value if isinstance(value, dict) else {}
    result = default_layered_route_cost_policy()
    weights = {}
    for domain_id in COST_DOMAIN_IDS:
        field = f"{domain_id}_lambda"
        raw = source.get(field)
        if raw in (None, ""):
            weights[domain_id] = None
        else:
            number = float(raw)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{field} 必须是有限非负数")
            weights[domain_id] = number
    source_text = _text(source.get("source"), COST_POLICY_PENDING_SOURCE)
    explicit_source = source_text != COST_POLICY_PENDING_SOURCE
    confirmed = _confirmed_with_source(source.get("confirmed"), explicit_source, "cost policy")
    result.update({
        "ground_lambda": weights["ground"],
        "air_traffic_lambda": weights["air_traffic"],
        "environment_obstacle_lambda": weights["environment_obstacle"],
        "source": source_text,
        "evidence": _optional_json_object(source.get("evidence"), "evidence"),
        "confirmed": confirmed,
    })
    if any(item is None for item in weights.values()):
        result["status"] = "pending_confirmation"
        result["status_reason"] = "cost_weights_not_configured"
        return result
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "cost_policy_not_confirmed"
        return result
    result["status"] = "confirmed"
    result["status_reason"] = None
    return result


def cost_lambdas(cost_policy):
    """Return the three explicit lambdas (``None`` means "not provided")."""

    policy = cost_policy if isinstance(cost_policy, dict) else {}
    return {
        domain_id: policy.get(f"{domain_id}_lambda") for domain_id in COST_DOMAIN_IDS
    }


def active_cost_domains(cost_policy):
    """Domains whose lambda is > 0.  ``0`` disables a domain, ``null`` blocks planning."""

    return tuple(
        domain_id for domain_id, value in cost_lambdas(cost_policy).items()
        if _finite(value) and float(value) > 0.0
    )


def cost_policy_is_runnable(cost_policy):
    """A cost policy may only feed the search when every lambda is an explicit number."""

    policy = cost_policy if isinstance(cost_policy, dict) else {}
    if str(policy.get("status") or "") != "confirmed":
        return False, str(policy.get("status_reason") or "cost_policy_not_confirmed")
    if any(value is None for value in cost_lambdas(policy).values()):
        return False, "cost_weights_not_configured"
    return True, None


def feasibility_policy_is_runnable(feasibility_policy):
    policy = feasibility_policy if isinstance(feasibility_policy, dict) else {}
    if str(policy.get("status") or "") != "confirmed":
        return False, str(policy.get("status_reason") or "feasibility_policy_not_confirmed")
    if policy.get("terrain_vertical_clearance_m") is None:
        return False, "terrain_vertical_clearance_not_configured"
    return True, None


def feasibility_policy_fingerprint(policy):
    return stable_fingerprint(
        {
            "terrain_vertical_clearance_m": (policy or {}).get("terrain_vertical_clearance_m"),
            "source": (policy or {}).get("source"),
            "confirmed": bool((policy or {}).get("confirmed")),
            "status": (policy or {}).get("status"),
        },
        prefix="layeredfeasv1-",
    )


def cost_policy_fingerprint(policy):
    return stable_fingerprint(
        {
            "lambdas": cost_lambdas(policy),
            "source": (policy or {}).get("source"),
            "confirmed": bool((policy or {}).get("confirmed")),
            "status": (policy or {}).get("status"),
        },
        prefix="layeredcostv1-",
    )


# --------------------------------------------------------------------------- masks


def default_layer_feasibility_mask(altitude_layer_id=None, status="not_calculated"):
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "altitude_layer_id": altitude_layer_id,
        "grid_level": None,
        "cell_count": 0,
        "counts": {"feasible": 0, "blocked": 0, "unknown": 0},
        "cells": {},
        "terrain_vertical_clearance_m": None,
        "building_vertical_clearance_m": None,
        "building_clearance_policy_status": None,
        "building_clearance_source": None,
        "cruise_altitude": None,
        "feasibility_policy_fingerprint": None,
        "request_fingerprint": None,
        "input_fingerprint": None,
        "mask_fingerprint": None,
        "source_audits": {},
        "adapter": None,
        "airspace": {
            "status": "not_applicable",
            "applicability": "display_only",
            "role": "display_only_reference_layer",
            "used_in_mask": False,
            "semantics": "display_only_airspace_not_used_for_feasibility_or_fingerprint",
        },
        "semantics": deepcopy(COARSE_ENVELOPE_SEMANTICS),
        "notes": [
            "这是 coarse_strategic_vertical_envelope：按 selected layer + L8 cell 的保守垂向包络，"
            "不是 exact footprint，也不是水平/精确建筑净空结论。",
            "building_count=0 表示该格没有建筑垂向约束；valid_height_fraction<1 或缺失高度/地面"
            "高程一律 unknown，绝不当 0。",
            "unknown 绝不参与 A* 搜索，且绝不等于 feasible。",
            "适飞空域仍是 display_only，不进入 mask、搜索或 fingerprint。",
        ],
    }


def normalize_layer_feasibility_mask(value):
    # 读取性能：mask 可能携带上万个 cell（每个 cell 带 terrain/building 诊断）。
    # 这里只做"新的顶层容器 + 归一化字段"的投影，逐 cell 明细共享只读引用，
    # 不再为每次读取深拷贝整份 mask —— 归一化结果与字段语义完全不变。
    if not isinstance(value, dict):
        return default_layer_feasibility_mask()
    result = default_layer_feasibility_mask(
        value.get("altitude_layer_id"), str(value.get("status") or "not_calculated"),
    )
    result.update(value)
    result["schema_version"] = SCHEMA_VERSION
    cells = result.get("cells")
    result["cells"] = cells if isinstance(cells, dict) else {}
    counts = result.get("counts")
    if not isinstance(counts, dict):
        counts = {"feasible": 0, "blocked": 0, "unknown": 0}
    else:
        counts = dict(counts)
    for key in FEASIBILITY_STATUSES:
        counts.setdefault(key, 0)
    result["counts"] = counts
    result["cell_count"] = len(result["cells"])
    result.setdefault("semantics", deepcopy(COARSE_ENVELOPE_SEMANTICS))
    result.setdefault("notes", default_layer_feasibility_mask()["notes"])
    result.setdefault("airspace", default_layer_feasibility_mask()["airspace"])
    result.setdefault("source_audits", {})
    return result


def mask_fingerprint(mask):
    """Deterministic content fingerprint of one layer feasibility mask."""

    return stable_fingerprint(
        {
            "altitude_layer_id": (mask or {}).get("altitude_layer_id"),
            "grid_level": (mask or {}).get("grid_level"),
            "terrain_vertical_clearance_m": (mask or {}).get("terrain_vertical_clearance_m"),
            "building_vertical_clearance_m": (mask or {}).get("building_vertical_clearance_m"),
            "building_clearance_source": (mask or {}).get("building_clearance_source"),
            "cruise_altitude": (mask or {}).get("cruise_altitude"),
            "feasibility_policy_fingerprint": (mask or {}).get("feasibility_policy_fingerprint"),
            "request_fingerprint": (mask or {}).get("request_fingerprint"),
            "input_fingerprint": (mask or {}).get("input_fingerprint"),
            "source_audits": (mask or {}).get("source_audits") or {},
            "cells": (mask or {}).get("cells") or {},
        },
        prefix="layeredmaskv1-",
    )


def _margin(altitude, floor):
    if altitude is None or floor is None:
        return None
    return round(float(altitude) - float(floor), 9)


def mask_cell(
    grid_id, *, status, cruise_altitude_egm2008_m, terrain_elevation_m,
    terrain_floor_egm2008_m, terrain_clearance_m, building_count,
    building_height_max_m, building_required_clearance_egm2008_m,
    reason_code, reason=None, provenance=None,
):
    return {
        "grid_id": str(grid_id),
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "cruise_altitude_egm2008_m": cruise_altitude_egm2008_m,
        "terrain_elevation_max_egm2008_m": terrain_elevation_m,
        "terrain_floor_egm2008_m": terrain_floor_egm2008_m,
        "terrain_vertical_clearance_m": terrain_clearance_m,
        "terrain_margin_m": _margin(cruise_altitude_egm2008_m, terrain_floor_egm2008_m),
        "building_count": building_count,
        "building_height_max_m": building_height_max_m,
        "building_required_clearance_egm2008_m": building_required_clearance_egm2008_m,
        "building_margin_m": _margin(
            cruise_altitude_egm2008_m, building_required_clearance_egm2008_m,
        ),
        "provenance": deepcopy(provenance or {}),
        "feasibility": "coarse_strategic_vertical_envelope",
    }


def build_layers_dict(grid_cells):
    """Deterministic, ordered ``cell -> mask_cell`` mapping."""

    return {
        str(cell["grid_id"]): deepcopy(cell)
        for cell in sorted(grid_cells or [], key=lambda item: str(item["grid_id"]))
    }


# --------------------------------------------------------------------------- candidates


def default_layered_route_candidate(
    route_id=None, altitude_layer_id=None, status="not_calculated",
):
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": None,
        "status": status,
        "route_id": route_id,
        "altitude_layer_id": altitude_layer_id,
        "path": [],
        "grid_path": [],
        "distance_m": None,
        "straight_line_distance_m": None,
        "detour_factor": None,
        "optimization_cost": None,
        "cost_breakdown": {
            "distance_contribution_m": None,
            "lambda_weighted_contributions_m": {
                domain_id: None for domain_id in COST_DOMAIN_IDS
            },
            "domain_exposure_index_m": {
                domain_id: None for domain_id in COST_DOMAIN_IDS
            },
            "mean_domain_index": {domain_id: None for domain_id in COST_DOMAIN_IDS},
            "lambdas": {domain_id: None for domain_id in COST_DOMAIN_IDS},
            "formula": "edge_cost = d * (1 + sum_lambda_domain * mean_domain_index)",
        },
        "reason": None,
        "blocking_reasons": [],
        "algorithm_id": "layered_route_planner_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": None,
        "feasibility_fingerprint": None,
        "risk_fingerprint": None,
        "policy_fingerprint": None,
        "request_fingerprint": None,
        "candidate_fingerprint": None,
        "cell_count": 0,
        "operational_route": False,
        "cns_assessed": False,
        "continuous_validation_required": True,
        "route_operating_layer_created": False,
        "operational_routes_untouched": True,
        "feasibility_mask_fingerprint": None,
        "provenance": {},
        "semantics": deepcopy(CANDIDATE_SEMANTICS),
        "notes": [
            "结果只是 candidate：不得直接写入 operational_routes / CNS，也不自动创建 "
            "RouteOperatingLayer。",
            "正式运行前必须经过后续 continuous validation（精确 footprint 与水平净空）。",
            "本轮只搜索 selected altitude layer 内的 MH/T L8 feasible cells，不跨层、不做自由 3D state。",
        ],
    }


def normalize_layered_route_candidate(value):
    # 读取性能：候选记录只做顶层投影，嵌套载荷（path / planning_objective / 统计）
    # 共享只读引用，避免每次读取深拷贝整份候选记录。字段与状态机语义不变。
    if not isinstance(value, dict):
        return default_layered_route_candidate()
    result = default_layered_route_candidate(
        value.get("route_id"), value.get("altitude_layer_id"),
        str(value.get("status") or "not_calculated"),
    )
    result.update(value)
    result["schema_version"] = SCHEMA_VERSION
    # The candidate contract is enforced unconditionally: no construction path can turn a
    # candidate into an operational route.
    result["operational_route"] = False
    result["cns_assessed"] = False
    result["continuous_validation_required"] = True
    result["route_operating_layer_created"] = False
    result["operational_routes_untouched"] = True
    result.setdefault("semantics", deepcopy(CANDIDATE_SEMANTICS))
    result.setdefault("notes", default_layered_route_candidate()["notes"])
    # Additive compatibility: the ``(route, altitude layer)`` lane a record belongs to.
    result.setdefault(
        "lane_key",
        f"{result.get('route_id') or 'od'}@{result.get('altitude_layer_id') or 'layer'}",
    )
    result.setdefault("statistics", {})
    result.setdefault("search_incomplete", False)
    result.setdefault("stale_reason", None)
    breakdown = result.get("cost_breakdown")
    if not isinstance(breakdown, dict):
        breakdown = deepcopy(default_layered_route_candidate()["cost_breakdown"])
    for key in (
        "lambda_weighted_contributions_m", "domain_exposure_index_m",
        "mean_domain_index", "lambdas",
    ):
        current = breakdown.get(key)
        if not isinstance(current, dict):
            current = {}
        breakdown[key] = {
            domain_id: current.get(domain_id) for domain_id in COST_DOMAIN_IDS
        }
    breakdown.setdefault("distance_contribution_m", None)
    breakdown.setdefault(
        "formula", "edge_cost = d * (1 + sum_lambda_domain * mean_domain_index)",
    )
    result["cost_breakdown"] = breakdown
    return result


def empty_layered_route_candidate_collection():
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_calculated",
        "count": 0,
        "active_candidate_id": None,
        "items": [],
        "masks": {},
        "notes": [
            "独立 candidate 容器：不写入 operational_routes、algorithm_selection、spatial_3d "
            "或任何 CNS 结果，也不自动创建 RouteOperatingLayer。",
        ],
    }


def normalize_layered_route_candidate_collection(value):
    # 读取性能：容器可能承载多个上万个 cell 的 feasibility mask。这里只做顶层投影，
    # 逐 cell 明细由 normalize_layer_feasibility_mask 共享只读引用，不做整树深拷贝。
    if not isinstance(value, dict):
        return empty_layered_route_candidate_collection()
    result = empty_layered_route_candidate_collection()
    result.update(value)
    result["schema_version"] = SCHEMA_VERSION
    items = [item for item in result.get("items") or [] if isinstance(item, dict)]
    result["items"] = [normalize_layered_route_candidate(item) for item in items]
    result["count"] = len(result["items"])
    masks = result.get("masks")
    result["masks"] = {
        str(key): normalize_layer_feasibility_mask(item)
        for key, item in (masks or {}).items() if isinstance(item, dict)
    } if isinstance(masks, dict) else {}
    active = result.get("active_candidate_id")
    if active is not None and active not in {item.get("candidate_id") for item in result["items"]}:
        result["active_candidate_id"] = None
    result["status"] = str(
        result.get("status")
        or ("passed" if result["items"] else "not_calculated")
    )
    result.setdefault("notes", empty_layered_route_candidate_collection()["notes"])
    return result


def candidate_fingerprint(components):
    """Deterministic fingerprint over the candidate's declared dependency components.

    The components never contain current airspace.
    """

    return stable_fingerprint(components, prefix="layeredcandv1-")


CANDIDATE_FINGERPRINT_COMPONENTS = (
    "scenario_route_id", "grid_identity", "altitude_layer_id", "request_fingerprint",
    "hard_constraints", "terrain_source_audit", "building_source_audit",
    "building_grid_source_audit", "building_clearance_policy",
    "risk_framework_v2_input_fingerprint", "risk_framework_v2_policy_fingerprint",
    "cost_policy", "feasibility_policy", "feasibility_mask_fingerprint",
    "planner_version",
)

__all__ = [
    "CANDIDATE_FINGERPRINT_COMPONENTS", "CANDIDATE_SEMANTICS", "CANDIDATE_STATUSES",
    "COARSE_ENVELOPE_SEMANTICS", "COST_DOMAIN_IDS", "DOMAIN_LABELS",
    "DOMAIN_RISK_CELL_CONTAINER_KEY", "FEASIBILITY_POLICY_PENDING_SOURCE", "FEASIBILITY_REASON_CODES",
    "FEASIBILITY_STATUSES", "POLICY_SEMANTICS", "SCHEMA_VERSION",
    "BUILDING_CLEARANCE_PENDING_SOURCE", "COST_POLICY_PENDING_SOURCE",
    "REQUEST_PENDING_SOURCE",
    "active_cost_domains", "build_layers_dict", "candidate_fingerprint",
    "cost_lambdas", "cost_policy_fingerprint", "cost_policy_is_runnable",
    "default_layer_feasibility_mask", "default_layered_route_candidate",
    "default_layered_route_cost_policy", "default_layered_route_feasibility_policy",
    "default_layered_route_request", "empty_layered_route_candidate_collection",
    "feasibility_policy_fingerprint", "feasibility_policy_is_runnable", "mask_cell",
    "mask_fingerprint", "normalize_layer_feasibility_mask",
    "normalize_layered_route_candidate", "normalize_layered_route_candidate_collection",
    "normalize_layered_route_cost_policy", "normalize_layered_route_feasibility_policy",
    "normalize_layered_route_request", "request_fingerprint",
    "resolve_cruise_altitude", "stable_fingerprint",
]
