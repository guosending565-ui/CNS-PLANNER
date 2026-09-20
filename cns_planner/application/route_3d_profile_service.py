"""Production Route3DProfile V1 — application service.

Thin, explicit and additive: it derives a **distance-parameterised 3D profile**
(``takeoff_event → climb → cruise → descent → landing_event``) from one already published
layered operational route, and nothing else.  It never replans, never performs a 3D search,
never invents a Dubins/minimum-snap trajectory and never judges kinematic feasibility.

Reads (all mandatory, nothing is defaulted)::

    passed operational route
      + current LayeredOperationalAdoption
      + active confirmed RouteOperatingLayer (fixed_cruise_layer, EGM2008)
      + confirmed AltitudeLayer H
      + confirmed departure procedure  -> terminal_altitude_egm2008_m + join distance
      + confirmed arrival procedure    -> terminal_altitude_egm2008_m + leave distance

``terminal_altitude_egm2008_m`` must be an **explicit** number carried by *manual* or
*verified* source/evidence.  The service never substitutes a FABDEM / landing-platform
surface elevation and never derives a join/leave distance from ``cruise_speed``.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.route_3d_profile import (
    CRUISE_VALIDATION_CURRENT, DIAGNOSTIC_BOUNDARY, PROFILE_VERSION, SCHEMA_VERSION,
    SEMANTICS, TERMINAL_TRANSITION_VALIDATION_NOT_EVALUATED, VERTICAL_REFERENCE,
    diagnostics_block, normalize_route_3d_profiles, profile_fingerprint,
    route_3d_profile_id, transition_diagnostic, validation_boundary,
)
from ..domain.route_safety_evidence_v2 import stable_fingerprint, utc_now
from ..domain.spatial_3d import UNRECORDED_SOURCE
from ..algorithms.coverage.geometric_3d import path_length_m

#: Where an explicit terminal altitude may be declared on a procedure's ``vertical_profile``.
#: The first present key wins; a declared-but-unrecorded value stays missing.
_TERMINAL_ALTITUDE_KEYS = (
    "terminal_altitude_egm2008_m", "terminal_altitude_m", "terminal_altitude",
)
#: The provenance keys that must accompany a terminal altitude.
_TERMINAL_SOURCE_KEYS = ("terminal_altitude_source", "source")
_TERMINAL_EVIDENCE_KEYS = ("terminal_altitude_evidence", "evidence_reference", "reference")

#: Source kinds accepted as "explicit manual / verified" terminal-altitude evidence.
_ACCEPTED_TERMINAL_SOURCE_KINDS = ("manual", "verified")

#: The provenance markers that identify a value that was fabricated from the terrain model.
_FABRICATED_MARKERS = (
    "fabdem", "dem", "dtm", "terrain", "surface_elevation", "platform", "landing_platform",
)

READINESS_SEMANTICS = {
    "passed_operational_route_required": True,
    "current_layered_operational_adoption_required": True,
    "active_confirmed_route_operating_layer_required": True,
    "confirmed_altitude_layer_required": True,
    "confirmed_departure_and_arrival_procedure_required": True,
    "explicit_join_and_leave_distance_required": True,
    "explicit_terminal_altitude_with_source_and_evidence_required": True,
    "fabdem_platform_height_is_never_used_as_terminal_altitude": True,
    "cruise_speed_never_derives_join_or_leave_distance": True,
    "no_default_parameter_is_inferred": True,
    "missing_item_is_not_ready_and_not_zero": True,
    "explicit_user_evaluation_required": True,
}

BOUNDARIES = {
    "thin_derivation_layer_not_a_planner": True,
    "no_3d_search": True,
    "no_dubins_airplane_or_minimum_snap": True,
    "no_kinematic_or_flight_dynamics_validation": True,
    "takeoff_and_landing_are_endpoint_events_only": True,
    "never_writes_route_altitude_profiles": True,
    "never_writes_layered_route_validation": True,
    "never_stales_candidate_validation_or_adoption": True,
    "produces_safe_or_unsafe_verdict": False,
    "automatic_generation": False,
}

#: Results that a Route3DProfile change stales (strictly downstream).  ``coverage_3d`` and
#: ``route_vertical_profiles`` are staled by the invalidation chain; ``route_safety_evidence_v2``
#: is staled by the same pass because it consumes ``coverage_3d``.  The propagation itself is
#: owned by :meth:`InvalidationService.route_3d_profile_changed`.
DOWNSTREAM_RESULTS = ("coverage_3d", "route_vertical_profiles", "route_safety_evidence_v2")


# ---------------------------------------------------------------------------- geometry


def build_geometry(route_length_m, cruise_altitude_m, terminal_departure_m,
                   terminal_arrival_m, join_distance_m, leave_distance_m):
    """The exact four geometric anchor points, then de-duplicated to distinct points.

    ``(0, z0) → (s_join, H) → (s_leave, H) → (L, z1)``

    Returns ``(waypoints, reasons)``.  A non-empty ``reasons`` list means the geometry is
    **unresolved**: the explicit user input is never repaired, clamped or rewritten.
    """

    reasons = []
    values = {
        "route_length_m": route_length_m,
        "cruise_altitude_m": cruise_altitude_m,
        "terminal_altitude_egm2008_m(departure)": terminal_departure_m,
        "terminal_altitude_egm2008_m(arrival)": terminal_arrival_m,
        "join_distance_along_route_m": join_distance_m,
        "leave_distance_along_route_m": leave_distance_m,
    }
    for name, value in values.items():
        if value is None:
            reasons.append(f"缺少显式 {name}：不推断、不取默认值")
    if reasons:
        return [], reasons
    length = float(route_length_m)
    if length <= 0:
        reasons.append("route_length_m 必须大于零")
    join, leave = float(join_distance_m), float(leave_distance_m)
    if join < 0 or leave < 0:
        reasons.append("join/leave 里程不得为负")
    if join > length or leave > length:
        reasons.append(
            "join/leave 里程越界（必须满足 0 <= s_join <= s_leave <= L="
            f"{length}）：不修正用户输入"
        )
    if join > leave:
        reasons.append(
            "transition overlap：s_join > s_leave，爬升与下降区间重叠；不修正用户输入"
        )
    if reasons:
        return [], reasons
    anchors = [
        {"distance_along_route_m": 0.0, "altitude_m": float(terminal_departure_m)},
        {"distance_along_route_m": join, "altitude_m": float(cruise_altitude_m)},
        {"distance_along_route_m": leave, "altitude_m": float(cruise_altitude_m)},
        {"distance_along_route_m": length, "altitude_m": float(terminal_arrival_m)},
    ]
    # Only *fully identical* points are removed: a zero-length constant segment is preserved
    # when its altitude genuinely changes (a vertical step at one station), and no invented
    # vertical-hover segment is ever added.
    waypoints = []
    for point in anchors:
        if waypoints and waypoints[-1] == point:
            continue
        waypoints.append(point)
    return waypoints, []


def build_phases(route_length_m, cruise_altitude_m, terminal_departure_m,
                 terminal_arrival_m, join_distance_m, leave_distance_m):
    """The five phase records.  Terminal events are endpoint events with zero horizontal
    extent — V1 never fabricates a zero-horizontal-distance vertical-hover segment."""

    length = float(route_length_m)
    join, leave = float(join_distance_m), float(leave_distance_m)
    z0, z1, h = float(terminal_departure_m), float(terminal_arrival_m), float(cruise_altitude_m)
    phases = [
        _phase("takeoff_event", start=0.0, end=0.0, start_altitude=z0, end_altitude=z0,
               endpoint_event=True),
        _phase("climb", start=0.0, end=join, start_altitude=z0, end_altitude=h),
        _phase("cruise", start=join, end=leave, start_altitude=h, end_altitude=h),
        _phase("descent", start=leave, end=length, start_altitude=h, end_altitude=z1,
               endpoint_event=leave == length),
        _phase("landing_event", start=length, end=length, start_altitude=z1, end_altitude=z1,
               endpoint_event=True),
    ]
    return phases


def _phase(phase_id, *, start, end, start_altitude, end_altitude, endpoint_event=False):
    return {
        "phase_id": phase_id,
        "start_distance_along_route_m": start,
        "end_distance_along_route_m": end,
        "horizontal_length_m": end - start,
        "start_altitude_m": start_altitude,
        "end_altitude_m": end_altitude,
        "altitude_change_m": end_altitude - start_altitude,
        "altitude_semantics": (
            "endpoint_event_no_horizontal_extent" if endpoint_event else "piecewise_linear"
        ),
        "vertical_reference": VERTICAL_REFERENCE,
        "endpoint_event": endpoint_event,
        "evidence": {
            "geometry": "explicit_join_leave_distance_along_route_m",
            "altitude": "explicit_altitude_layer_or_terminal_altitude_evidence",
        },
        "semantics": {
            "no_fabricated_zero_horizontal_distance_vertical_segment": True,
            "altitude_is_piecewise_linear_in_distance_along_route": not endpoint_event,
        },
    }


# ------------------------------------------------------------------------------ helpers


def _route_records(state):
    return {
        str(item.get("route_id")): item
        for item in state.get("operational_routes") or []
        if isinstance(item, dict)
    }


def _layer_records(spatial):
    return {
        str(item.get("altitude_layer_id")): item
        for item in spatial.get("altitude_layers") or []
        if isinstance(item, dict)
    }


def _active_assignment(spatial, route_id):
    return next((
        item for item in spatial.get("route_operating_layers") or []
        if isinstance(item, dict)
        and str(item.get("route_id")) == route_id
        and item.get("active", True) is True
    ), None)


def _procedure(spatial, route_id, procedure_type):
    items = [
        item for item in spatial.get("departure_arrival_procedures") or []
        if isinstance(item, dict)
        and str(item.get("route_id")) == route_id
        and str(item.get("procedure_type")) == procedure_type
    ]
    return items[0] if items else None


def _current_adoption(state, route_id):
    collection = state.get("layered_operational_adoptions") or {}
    published = [
        item for item in collection.get("items") or []
        if isinstance(item, dict) and item.get("status") == "published"
    ]
    wanted = [item for item in published if str(item.get("route_id")) == route_id]
    current = [item for item in wanted if item.get("current_applicability") == "current"]
    if current:
        return current[0], "current"
    if wanted:
        return wanted[0], str(wanted[0].get("current_applicability") or "not_current")
    return None, "missing"


def _finite(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _procedure_vertical(procedure):
    return procedure.get("vertical_profile") if isinstance(
        procedure.get("vertical_profile"), dict
    ) else {}


def resolve_terminal_altitude(procedure):
    """Resolve the **explicit** EGM2008 terminal altitude of one procedure.

    Never defaults, never reads FABDEM / a landing-platform elevation, never interpolates
    from the route.  An explicit number *without* recorded source and evidence is treated as
    missing: a value with no provenance is not admissible evidence.
    """

    result = {
        "present": False,
        "altitude_m": None,
        "source": None,
        "evidence": None,
        "basis": None,
        "reasons": [],
    }
    if not isinstance(procedure, dict):
        result["reasons"].append("缺少 procedure：无法解析 terminal altitude")
        return result
    vertical = _procedure_vertical(procedure)
    value, key = None, None
    for candidate in _TERMINAL_ALTITUDE_KEYS:
        if vertical.get(candidate) not in (None, ""):
            value, key = vertical.get(candidate), candidate
            break
    source = None
    for candidate in _TERMINAL_SOURCE_KEYS:
        if vertical.get(candidate) not in (None, ""):
            source = str(vertical.get(candidate)).strip()
            break
    evidence = None
    for candidate in _TERMINAL_EVIDENCE_KEYS:
        candidate_value = vertical.get(candidate)
        if isinstance(candidate_value, dict):
            candidate_value = candidate_value.get("evidence_reference") or candidate_value.get(
                "reference"
            )
        if candidate_value not in (None, ""):
            evidence = candidate_value
            break
    altitude = _finite(value)
    if altitude is None:
        result["reasons"].append(
            "缺少显式 terminal_altitude_egm2008_m：绝不使用 FABDEM / 起降平台高度作为默认值"
        )
        return result
    result.update({"present": True, "altitude_m": altitude, "basis": key})
    if not source or source == UNRECORDED_SOURCE:
        result["present"] = False
        result["reasons"].append("terminal altitude 缺少显式 source：无来源的数值不作为证据")
    if not evidence:
        result["present"] = False
        result["reasons"].append("terminal altitude 缺少 evidence/reference：无证据的数值不作为证据")
    if not result["present"]:
        return result
    result["source"] = source
    result["evidence"] = evidence
    marker = f"{source} {evidence}".lower()
    if any(item in marker for item in _FABRICATED_MARKERS) and not any(
        item in marker for item in _ACCEPTED_TERMINAL_SOURCE_KINDS
    ):
        # A terrain-derived platform height must never masquerade as a terminal altitude.
        result["present"] = False
        result["reasons"].append(
            "terminal altitude 的来源看起来是地形/起降平台推导值（FABDEM/DEM/DTM/platform）："
            "必须来自显式 manual/verified 来源，绝不后台默认地形高度"
        )
        return result
    if not any(item in marker for item in _ACCEPTED_TERMINAL_SOURCE_KINDS):
        result["present"] = False
        result["reasons"].append(
            "terminal altitude 来源既不是 manual 也不是 verified：不接受后台/隐含推导值"
        )
        return result
    return result


def resolve_join_leave(procedure, expected_kind):
    """Resolve one **explicit** join/leave ``distance_along_route_m``.

    The value comes from ``join_leave_point`` only.  ``cruise_speed`` (or any other
    performance parameter) is never used to derive it.
    """

    result = {"present": False, "distance_m": None, "kind": None, "reasons": []}
    if not isinstance(procedure, dict):
        result["reasons"].append("缺少 procedure：无法解析 join/leave 里程")
        return result
    point = procedure.get("join_leave_point")
    if not isinstance(point, dict):
        result["reasons"].append(
            f"缺少显式 join_leave_point（{expected_kind}）：绝不根据 cruise_speed 推导里程"
        )
        return result
    kind = str(point.get("kind") or "")
    if kind != expected_kind:
        result["reasons"].append(
            f"join_leave_point.kind 必须是 {expected_kind}（当前 {kind or '未记录'}）"
        )
        return result
    distance = _finite(point.get("distance_along_route_m"))
    if distance is None:
        result["reasons"].append(
            f"缺少显式 {expected_kind} distance_along_route_m："
            "绝不根据 cruise_speed 或任何性能参数推导里程"
        )
        return result
    result.update({"present": True, "distance_m": distance, "kind": kind,
                   "source": str(point.get("source") or "join_leave_point")})
    return result


def _route_length(route):
    """The operational route length used as the V1 distance basis.

    The recorded ``distance_m`` is authoritative, but the *consumers* of the profile
    (``Coverage3D`` / ``RouteVerticalProfile``) parameterise the same route by its **geometric**
    path length.  A mismatch would silently shift ``s_join``/``s_leave`` along the route, so the
    two values must agree; otherwise the derivation is unresolved instead of being repaired.
    """

    path = route.get("path") or []
    geometric = path_length_m(path) if len(path) >= 2 else None
    distance = _finite(route.get("distance_m"))
    if distance is None or distance <= 0:
        if geometric is not None and geometric > 0:
            return geometric, "geometric_path_length_m"
        return None, "unresolved"
    if geometric is None or geometric <= 0:
        return distance, "operational_route_distance_m"
    tolerance = max(1.0, 0.005 * geometric)
    if abs(distance - geometric) > tolerance:
        return None, "unresolved"
    return distance, "operational_route_distance_m"


# ------------------------------------------------------------------------------- models


def _missing(status, reasons):
    return {"status": status, "reasons": [str(item) for item in reasons]}


def resolve_inputs(state, route_id):
    """Resolve every declared input of one Route3DProfile.  Nothing is inferred."""

    spatial = state.get("spatial_3d") or {}
    routes = _route_records(state)
    route = routes.get(route_id)
    blockers = []
    if route is None:
        return {
            "route": None,
            "blockers": ["passed operational route 不存在"],
            "missing": ["passed_operational_route"],
        }
    if str(route.get("status")) != "passed":
        blockers.append(
            f"operational route 状态不是 passed（{route.get('status')}）：不接受非 passed 航路"
        )
    path = route.get("path") or []
    if len(path) < 2:
        blockers.append("operational route 顶点不足（需要 >= 2 个顶点）")
    length, basis = _route_length(route)
    if length is None:
        blockers.append(
            "operational route 的 distance_m 与其几何 path 长度不一致（或完全缺失）："
            "profile 的里程参数化必须与消费方使用同一长度，绝不修正或截断用户输入"
        )
    assignment = _active_assignment(spatial, route_id)
    layer = None
    if assignment is None:
        blockers.append("缺少 active RouteOperatingLayer：不从 RouteAltitudeProfile 推断巡航高度")
        blockers.append(
            "缺少 AltitudeLayer H：没有显式巡航高度层，绝不从上下界或高级剖面推断高度"
        )
    else:
        if not assignment.get("confirmed"):
            blockers.append("RouteOperatingLayer 未显式确认")
        if str(assignment.get("status")) != "confirmed":
            blockers.append(
                f"RouteOperatingLayer 状态不是 confirmed（{assignment.get('status')}）"
            )
        if str(assignment.get("operating_mode")) != "fixed_cruise_layer":
            blockers.append("RouteOperatingLayer operating_mode 不是 fixed_cruise_layer")
        if str(assignment.get("vertical_reference")) != VERTICAL_REFERENCE:
            blockers.append("RouteOperatingLayer vertical_reference 不是 egm2008_orthometric")
        if str(assignment.get("source") or "") in ("", UNRECORDED_SOURCE):
            blockers.append("RouteOperatingLayer 缺少 source/evidence 来源")
        layer = _layer_records(spatial).get(str(assignment.get("altitude_layer_id") or ""))
        if layer is None:
            blockers.append(
                f"引用的 AltitudeLayer 不存在：{assignment.get('altitude_layer_id')}"
            )
        else:
            if not layer.get("confirmed") or str(layer.get("status")) != "confirmed":
                blockers.append("AltitudeLayer H 未确认")
            if str(layer.get("vertical_reference")) != VERTICAL_REFERENCE:
                blockers.append("AltitudeLayer H vertical_reference 不是 egm2008_orthometric")
            if layer.get("nominal_altitude_m") is None:
                blockers.append("AltitudeLayer H 缺少显式 nominal_altitude_m：绝不取上下界中值")
    adoption, applicability = _current_adoption(state, route_id)
    if adoption is None:
        blockers.append("缺少 published layered operational adoption")
    elif applicability != "current":
        blockers.append(
            f"layered operational adoption 不是 current（{applicability}）"
        )
    procedures = {}
    for procedure_type, expected_kind in (("departure", "join"), ("arrival", "leave")):
        procedure = _procedure(spatial, route_id, procedure_type)
        if procedure is None:
            procedures[procedure_type] = _missing(
                "not_ready", [f"缺少 confirmed {procedure_type} procedure"],
            )
            blockers.append(f"缺少 confirmed {procedure_type} procedure")
            continue
        reasons = []
        if not procedure.get("confirmed") or str(procedure.get("status")) != "confirmed":
            reasons.append(f"{procedure_type} procedure 未确认")
        if str(procedure.get("source") or "") in ("", UNRECORDED_SOURCE):
            reasons.append(f"{procedure_type} procedure 缺少 source/evidence")
        terminal = resolve_terminal_altitude(procedure)
        if not terminal["present"]:
            reasons.extend(terminal["reasons"])
        join_leave = resolve_join_leave(procedure, expected_kind)
        if not join_leave["present"]:
            reasons.extend(join_leave["reasons"])
        rate_key = "climb_rate_mps" if procedure_type == "departure" else "descent_rate_mps"
        rate = _finite(_procedure_vertical(procedure).get(rate_key))
        if rate is None or rate <= 0:
            # ``climb_rate_mps`` / ``descent_rate_mps`` are explicit performance evidence.  They
            # never derive the join/leave distance, but without them the procedure has not
            # declared its transition performance and no diagnostic number may be emitted.
            reasons.append(
                f"缺少显式 {rate_key}（performance evidence 必须为正数）："
                "不补默认爬升/下降率，也不输出 diagnostic 数值"
            )
        procedures[procedure_type] = {
            "procedure_id": procedure.get("procedure_id"),
            "procedure_type": procedure_type,
            "status": "passed" if not reasons else "not_ready",
            "reasons": reasons,
            "terminal_altitude": terminal,
            "join_leave": join_leave,
            "rate_key": rate_key,
            "rate_mps": rate,
            "transition_mode": procedure.get("transition_mode"),
        }
        blockers.extend(f"{procedure_type}：{reason}" for reason in reasons)
    return {
        "route": route,
        "route_length_m": length,
        "route_length_basis": basis,
        "geometric_path_length_m": (
            path_length_m(route.get("path") or []) if len(route.get("path") or []) >= 2 else None
        ),
        "assignment": assignment,
        "layer": layer,
        "adoption": adoption,
        "adoption_applicability": applicability,
        "procedures": procedures,
        "blockers": blockers,
        "missing": list(blockers),
    }


def readiness_snapshot(state):
    """Per-route readiness of the additive Route3DProfile derivation."""

    spatial = state.get("spatial_3d") or {}
    stored = normalize_route_3d_profiles(spatial.get("route_3d_profiles"))
    routes = []
    for route in state.get("operational_routes") or []:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("route_id"))
        resolved = resolve_inputs(state, route_id)
        blockers = list(resolved["blockers"])
        routes.append({
            "route_id": route_id,
            "status": "ready" if not blockers else "not_ready",
            "ready": not blockers,
            "blockers": blockers,
            "route_status": route.get("status"),
            "adoption_id": (resolved.get("adoption") or {}).get("adoption_id"),
            "altitude_layer_id": (
                (resolved.get("assignment") or {}).get("altitude_layer_id")
            ),
            "cruise_altitude_m": (resolved.get("layer") or {}).get("nominal_altitude_m"),
            "departure_procedure_id": (
                (resolved.get("procedures") or {}).get("departure") or {}
            ).get("procedure_id"),
            "arrival_procedure_id": (
                (resolved.get("procedures") or {}).get("arrival") or {}
            ).get("procedure_id"),
            "profile_present": bool(stored),
        })
    overall = (
        "not_ready" if not routes or any(not item["ready"] for item in routes)
        else "ready"
    )
    return {
        "status": overall,
        "profile_version": PROFILE_VERSION,
        "operating_mode": "production_route_3d_profile_v1",
        "semantics": deepcopy(READINESS_SEMANTICS),
        "boundaries": deepcopy(BOUNDARIES),
        "route_count": len(routes),
        "ready_count": sum(1 for item in routes if item["ready"]),
        "blockers": [f"{item['route_id']}：{reason}" for item in routes for reason in item["blockers"]],
        "routes": routes,
        "stored_profile_count": len(stored),
        "explicit_evaluation_required": True,
        "automatic_generation": False,
        "produces_safe_or_unsafe_verdict": False,
        "next_stage": "vertical_transition_continuous_validation",
        "next_stage_implemented": False,
    }


def fingerprint_components(state, route_id, resolved):
    """The declared upstream dependency set of one Route3DProfile.

    Binds the operational route + adoption, the RouteOperatingLayer, the AltitudeLayer, the
    departure/arrival procedures *including their terminal altitude evidence*, and the
    planner/profile version.  Any upstream change therefore produces a different fingerprint
    and makes a stored profile stale.
    """

    route = resolved.get("route") or {}
    assignment = resolved.get("assignment") or {}
    layer = resolved.get("layer") or {}
    adoption = resolved.get("adoption") or {}
    procedures = resolved.get("procedures") or {}
    path = route.get("path") or []

    def procedure_components(entry):
        if not entry or not entry.get("procedure_id"):
            return None
        terminal = entry.get("terminal_altitude") or {}
        join_leave = entry.get("join_leave") or {}
        return {
            "procedure_id": entry.get("procedure_id"),
            "procedure_type": entry.get("procedure_type"),
            "status": entry.get("status"),
            "transition_mode": entry.get("transition_mode"),
            "rate_key": entry.get("rate_key"),
            "rate_mps": entry.get("rate_mps"),
            "terminal_altitude_m": terminal.get("altitude_m"),
            "terminal_altitude_source": terminal.get("source"),
            "terminal_altitude_evidence": terminal.get("evidence"),
            "terminal_altitude_basis": terminal.get("basis"),
            "join_leave_kind": join_leave.get("kind"),
            "join_leave_distance_along_route_m": join_leave.get("distance_m"),
        }

    return {
        "profile_version": PROFILE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "operational_route": {
            "route_id": route_id,
            "status": route.get("status"),
            "distance_m": route.get("distance_m"),
            "resolved_length_m": resolved.get("route_length_m"),
            "geometric_path_length_m": resolved.get("geometric_path_length_m"),
            "length_basis": resolved.get("route_length_basis"),
            "vertex_count": len(path),
            "path_fingerprint": stable_fingerprint(path, prefix="routepath-"),
        },
        "operational_adoption": {
            "adoption_id": adoption.get("adoption_id"),
            "status": adoption.get("status"),
            "current_applicability": resolved.get("adoption_applicability"),
            "projection_fingerprint": adoption.get("projection_fingerprint"),
            "validation_fingerprint": adoption.get("validation_fingerprint"),
            "candidate_fingerprint": adoption.get("candidate_fingerprint"),
        },
        "route_operating_layer": {
            "altitude_layer_id": assignment.get("altitude_layer_id"),
            "operating_mode": assignment.get("operating_mode"),
            "vertical_reference": assignment.get("vertical_reference"),
            "status": assignment.get("status"),
            "source": assignment.get("source"),
            "evidence_fingerprint": stable_fingerprint(
                assignment.get("evidence") or {}, prefix="roleevidence-",
            ),
        },
        "altitude_layer": {
            "altitude_layer_id": layer.get("altitude_layer_id"),
            "nominal_altitude_m": layer.get("nominal_altitude_m"),
            "lower_altitude_m": layer.get("lower_altitude_m"),
            "upper_altitude_m": layer.get("upper_altitude_m"),
            "vertical_reference": layer.get("vertical_reference"),
            "status": layer.get("status"),
            "source": layer.get("source"),
            "evidence_fingerprint": stable_fingerprint(
                layer.get("evidence") or {}, prefix="layerevidence-",
            ),
        },
        "departure_procedure": procedure_components(procedures.get("departure")),
        "arrival_procedure": procedure_components(procedures.get("arrival")),
        "vertical_reference": VERTICAL_REFERENCE,
    }


def _provenance(state, route_id, resolved, components):
    assignment = resolved.get("assignment") or {}
    layer = resolved.get("layer") or {}
    adoption = resolved.get("adoption") or {}
    procedures = resolved.get("procedures") or {}
    departure = procedures.get("departure") or {}
    arrival = procedures.get("arrival") or {}
    return {
        "derivation": "published_fixed_cruise_route_to_distance_parameterised_3d_profile",
        "profile_version": PROFILE_VERSION,
        "route_length_basis": resolved.get("route_length_basis"),
        "distance_parameterisation": {
            "route_length_m": resolved.get("route_length_m"),
            "geometric_path_length_m": resolved.get("geometric_path_length_m"),
            "same_length_as_consumers": True,
            "semantics": (
                "Coverage3D / RouteVerticalProfile 以几何 path 长度参数化该航路，"
                "因此 profile 的 L 必须与之相等；不一致时直接 unresolved，不做修正。"
            ),
        },
        "reads": {
            "operational_route": route_id,
            "layered_operational_adoption": adoption.get("adoption_id"),
            "route_operating_layer": assignment.get("altitude_layer_id"),
            "altitude_layer": layer.get("altitude_layer_id"),
            "departure_procedure": departure.get("procedure_id"),
            "arrival_procedure": arrival.get("procedure_id"),
        },
        "terminal_altitude_sources": {
            "departure": {
                "altitude_m": (departure.get("terminal_altitude") or {}).get("altitude_m"),
                "source": (departure.get("terminal_altitude") or {}).get("source"),
                "evidence": (departure.get("terminal_altitude") or {}).get("evidence"),
                "basis": (departure.get("terminal_altitude") or {}).get("basis"),
            },
            "arrival": {
                "altitude_m": (arrival.get("terminal_altitude") or {}).get("altitude_m"),
                "source": (arrival.get("terminal_altitude") or {}).get("source"),
                "evidence": (arrival.get("terminal_altitude") or {}).get("evidence"),
                "basis": (arrival.get("terminal_altitude") or {}).get("basis"),
            },
        },
        "join_leave_sources": {
            "join_distance_along_route_m": (
                (departure.get("join_leave") or {}).get("distance_m")
            ),
            "leave_distance_along_route_m": (
                (arrival.get("join_leave") or {}).get("distance_m")
            ),
            "derived_from_cruise_speed": False,
        },
        "upstream_modified": False,
        "recomputed_algorithms": [],
        "fingerprint_components": deepcopy(components),
    }


def _limitations():
    return [
        "Route3DProfile V1 是薄层派生：不重规划、不做 3D 搜索、不做 Dubins Airplane / "
        "minimum-snap，也不做运动学或飞行动力学可行性判断。",
        "takeoff_event / landing_event 只是端点事件：V1 不虚构零水平距离的垂直悬停段。",
        "climb/descent 的 rate 只作为显式性能证据用于 diagnostic（"
        f"{DIAGNOSTIC_BOUNDARY}）；几何以显式 s_join/s_leave 为准，绝不由 cruise_speed 反推。",
        "现有 LayeredRouteValidation 只验证固定 H 巡航，不为 climb/descent 背书："
        f"cruise_validation={CRUISE_VALIDATION_CURRENT}，"
        f"terminal_transition_validation={TERMINAL_TRANSITION_VALIDATION_NOT_EVALUATED}，"
        "full_3d_geometry_validated=false。",
        "terminal altitude 必须来自显式 manual/verified source + evidence："
        "绝不后台默认 FABDEM 或起降平台高度。",
        "本产物不是 safe/unsafe 结论，也不是安全认证或安全评分。",
    ]


def derive_profile(state, route_id, *, created_at=None):
    """Derive one Route3DProfile record.  Fail-closed: never repairs user input."""

    resolved = resolve_inputs(state, route_id)
    route = resolved.get("route")
    procedures = resolved.get("procedures") or {}
    departure = procedures.get("departure") or {}
    arrival = procedures.get("arrival") or {}
    adoption = resolved.get("adoption") or {}
    assignment = resolved.get("assignment") or {}
    components = fingerprint_components(state, route_id, resolved)
    identity = profile_fingerprint(components)
    base = {
        "profile_id": route_3d_profile_id(route_id),
        "route_id": route_id,
        "adoption_id": adoption.get("adoption_id"),
        "altitude_layer_id": assignment.get("altitude_layer_id"),
        "departure_procedure_id": departure.get("procedure_id"),
        "arrival_procedure_id": arrival.get("procedure_id"),
        "vertical_reference": VERTICAL_REFERENCE,
        "route_length_m": resolved.get("route_length_m"),
        "route_length_basis": resolved.get("route_length_basis"),
        "created_at": created_at or utc_now(),
        "validation_boundary": validation_boundary(),
        "semantics": deepcopy(SEMANTICS),
        "limitations": _limitations(),
        "profile_version": PROFILE_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    if resolved["blockers"]:
        return {
            **base,
            "status": "not_ready",
            "current_applicability": "not_ready",
            "cruise_altitude_m": (resolved.get("layer") or {}).get("nominal_altitude_m"),
            "terminal_altitude_m": {},
            "join_leave": {},
            "waypoints": [],
            "phases": [],
            "diagnostics": {
                "policy": DIAGNOSTIC_BOUNDARY,
                "climb": None,
                "descent": None,
                "evaluates_real_flight_feasibility": False,
                "not_aircraft_kinematic_validation": True,
            },
            "provenance": _provenance(state, route_id, resolved, components),
            "fingerprints": {
                "profile_fingerprint": None,
                "components": components,
                "profile_version": PROFILE_VERSION,
                "evaluator_version": PROFILE_VERSION,
            },
            "reasons": list(resolved["blockers"]),
        }
    layer = resolved.get("layer") or {}
    cruise = _finite(layer.get("nominal_altitude_m"))
    departure_terminal = departure.get("terminal_altitude") or {}
    arrival_terminal = arrival.get("terminal_altitude") or {}
    join = (departure.get("join_leave") or {}).get("distance_m")
    leave = (arrival.get("join_leave") or {}).get("distance_m")
    z0 = departure_terminal.get("altitude_m")
    z1 = arrival_terminal.get("altitude_m")
    waypoints, geometry_reasons = build_geometry(
        resolved.get("route_length_m"), cruise, z0, z1, join, leave,
    )
    if geometry_reasons:
        return {
            **base,
            "status": "unresolved",
            "current_applicability": "unresolved",
            "cruise_altitude_m": cruise,
            "terminal_altitude_m": {
                "departure_egm2008_m": z0,
                "arrival_egm2008_m": z1,
                "vertical_reference": VERTICAL_REFERENCE,
                "source": "procedure_vertical_profile_explicit_evidence",
            },
            "join_leave": {
                "join_distance_along_route_m": join,
                "leave_distance_along_route_m": leave,
                "derived_from_cruise_speed": False,
            },
            "waypoints": [],
            "phases": [],
            "diagnostics": {
                "policy": DIAGNOSTIC_BOUNDARY,
                "climb": None,
                "descent": None,
                "evaluates_real_flight_feasibility": False,
                "not_aircraft_kinematic_validation": True,
            },
            "provenance": _provenance(state, route_id, resolved, components),
            "fingerprints": {
                "profile_fingerprint": None,
                "components": components,
                "profile_version": PROFILE_VERSION,
                "evaluator_version": PROFILE_VERSION,
            },
            "reasons": list(geometry_reasons),
        }
    length = float(resolved["route_length_m"])
    phases = build_phases(length, cruise, z0, z1, join, leave)
    climb_rate = departure.get("rate_mps")
    descent_rate = arrival.get("rate_mps")
    climb_diagnostic = transition_diagnostic(
        cruise - z0, float(join) - 0.0, climb_rate,
    )
    descent_diagnostic = transition_diagnostic(
        z1 - cruise, length - float(leave), descent_rate,
    )
    diagnostics = diagnostics_block(
        climb_diagnostic, descent_diagnostic, climb_rate, descent_rate,
    )
    return {
        **base,
        "status": "passed",
        "current_applicability": "current",
        "cruise_altitude_m": cruise,
        "terminal_altitude_m": {
            "departure_egm2008_m": z0,
            "arrival_egm2008_m": z1,
            "vertical_reference": VERTICAL_REFERENCE,
            "source": "procedure_vertical_profile_explicit_evidence",
            "departure_source": departure_terminal.get("source"),
            "departure_evidence": departure_terminal.get("evidence"),
            "arrival_source": arrival_terminal.get("source"),
            "arrival_evidence": arrival_terminal.get("evidence"),
        },
        "join_leave": {
            "join_distance_along_route_m": join,
            "leave_distance_along_route_m": leave,
            "derived_from_cruise_speed": False,
        },
        "waypoints": waypoints,
        "phases": phases,
        "phase_lengths_m": {
            "climb": float(join),
            "cruise": float(leave) - float(join),
            "descent": length - float(leave),
        },
        "diagnostics": diagnostics,
        "provenance": _provenance(state, route_id, resolved, components),
        "fingerprints": {
            "profile_fingerprint": identity,
            "components": components,
            "profile_version": PROFILE_VERSION,
            "evaluator_version": PROFILE_VERSION,
        },
        "reasons": [],
    }


def current_applicability(state, profile):
    """Recompute whether a stored profile still matches the current upstream facts."""

    if not isinstance(profile, dict):
        return "not_ready", None
    status = str(profile.get("status") or "not_ready")
    if status != "passed":
        return str(profile.get("current_applicability") or "not_ready"), None
    route_id = str(profile.get("route_id") or "")
    if route_id not in _route_records(state):
        return "stale", "operational_route_missing"
    resolved = resolve_inputs(state, route_id)
    if resolved["blockers"]:
        return "stale", "upstream_blockers:" + ";".join(resolved["blockers"][:3])
    expected = profile_fingerprint(fingerprint_components(state, route_id, resolved))
    stored = (profile.get("fingerprints") or {}).get("profile_fingerprint")
    if expected != stored:
        return "stale", "profile_fingerprint_changed"
    return "current", None


# ------------------------------------------------------------------------------ service


class Route3DProfileService:
    """Explicit CRUD + derivation for the additive Production Route3DProfile V1."""

    def __init__(self, session, invalidation, snapshot):
        self.session, self.invalidation, self.snapshot = session, invalidation, snapshot

    # ---- container ----------------------------------------------------------------
    @property
    def _spatial(self):
        return self.session.state.setdefault("spatial_3d", {})

    def _stored(self):
        return normalize_route_3d_profiles(self._spatial.get("route_3d_profiles"))

    # ---- read-only projections ----------------------------------------------------
    def readiness(self):
        return deepcopy(readiness_snapshot(self.session.state))

    def result_snapshot(self):
        state = self.session.state
        profiles = self._stored()
        projected = []
        for key, profile in profiles.items():
            entry = deepcopy(profile)
            applicability, reason = current_applicability(state, profile)
            entry["current_applicability"] = applicability
            if applicability == "stale":
                entry["stale_reason"] = reason
                entry["status"] = "stale"
            projected.append(entry)
        collection = {
            "status": (
                "not_calculated" if not projected
                else "stale" if all(item["current_applicability"] == "stale" for item in projected)
                else "passed" if all(item["current_applicability"] == "current" for item in projected)
                else "pending_confirmation"
            ),
            "profile_version": PROFILE_VERSION,
            "count": len(projected),
            "items": projected,
            "profiles_by_route": {
                item["route_id"]: item for item in projected
            },
            "semantics": {
                "read_only_projection": True,
                "current_applicability_is_recomputed": True,
                "explicit_evaluation_required": True,
                "automatic_generation": False,
            },
            "boundaries": deepcopy(BOUNDARIES),
        }
        return collection

    def readiness_snapshot(self):
        return self.readiness()

    # ---- explicit derivation ------------------------------------------------------
    def evaluate(self, payload=None, *, save=True):
        payload = payload if isinstance(payload, dict) else {}
        state = self.session.state
        route_ids = [str(item.get("route_id")) for item in state.get("operational_routes") or []]
        requested = payload.get("route_id")
        if requested in (None, "", "all"):
            targets = route_ids
        else:
            wanted = str(requested)
            if wanted not in route_ids:
                raise ValueError(f"运行航路不存在：{wanted}")
            targets = [wanted]
        if not targets:
            raise ValueError("当前没有运行航路：Route3DProfile 只能在显式 evaluate 时生成")
        records = self._stored()
        results = []
        for route_id in targets:
            record = derive_profile(state, route_id)
            records[record["profile_id"]] = record
            results.append(deepcopy(record))
        self._spatial["route_3d_profiles"] = records
        state.setdefault("result_statuses", {})["route_3d_profiles"] = (
            "passed" if all(item["status"] == "passed" for item in results)
            else "unresolved" if any(item["status"] == "unresolved" for item in results)
            else "not_ready"
        )
        if save:
            self._commit("route_3d_profile_evaluated")
            return self.snapshot()
        return {"evaluated": results, **self.result_snapshot()}

    def delete(self, profile_id=None, *, route_id=None, save=True):
        records = self._stored()
        wanted = str(profile_id or "").strip()
        target_route = str(route_id or "").strip()
        removed = [
            key for key, item in records.items()
            if (wanted and key == wanted) or (target_route and item.get("route_id") == target_route)
        ]
        if not removed:
            raise ValueError("Route3DProfile 不存在")
        for key in removed:
            records.pop(key, None)
        self._spatial["route_3d_profiles"] = records
        if save:
            self._commit("route_3d_profile_deleted")
            return self.snapshot()
        return {"deleted": removed, **self.result_snapshot()}

    # ---- invalidation -------------------------------------------------------------
    def stale_for_reason(self, reason="route_3d_profile_input_changed"):
        """Mark every stored Route3DProfile stale.

        This is strictly downstream: it never marks a Theta* candidate, a validation or an
        adoption stale, and it never deletes a stored profile (audit evidence is preserved).
        """

        records = self._stored()
        changed = []
        for key, item in records.items():
            if item.get("status") == "stale":
                continue
            item["status"] = "stale"
            item["current_applicability"] = "stale"
            item["stale_reason"] = str(reason)
            changed.append(key)
        if changed:
            self._spatial["route_3d_profiles"] = records
            state = self.session.state
            state.setdefault("result_statuses", {})["route_3d_profiles"] = "stale"
        return {"stale_profile_ids": changed}

    # ---- internals ----------------------------------------------------------------
    def _commit(self, reason):
        """Persist an explicit evaluate/delete.

        The service deliberately does **not** stale the profile set it just wrote: the
        ``route_3d_profile`` invalidator exists for *upstream* changes (route / layer /
        procedure) and is driven by :class:`InvalidationService`.  Downstream propagation of an
        explicit change is owned by ``WorkflowService``.
        """

        self.session.save()
