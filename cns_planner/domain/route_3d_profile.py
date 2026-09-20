"""Production Route3DProfile V1 — distance-parameterised 3D profile of a published route.

This module is a **thin derivation layer**, not a new planner.  It takes one already
published layered operational route (``OperationalRoute`` + current
``LayeredOperationalAdoption`` + active confirmed ``RouteOperatingLayer`` + confirmed
``AltitudeLayer`` + confirmed departure/arrival procedures) and derives the piecewise
linear vertical profile

    (0, z0) → (s_join, H) → (s_leave, H) → (L, z1)

parameterised by distance along the route, so the *existing* consumers
(``RouteVerticalProfile``, ``Coverage3D``, ``BuildingClearance``, C3D corridor) can read a
real climb → cruise → descent profile through the unchanged
``effective_route_vertical_context`` interface.

Explicit non-goals of V1 (deliberately **not** implemented here):

* no 3D search, no Dubins Airplane / minimum-snap trajectory, no kinematic safety model;
* no invented zero-horizontal-distance vertical hover segment: ``takeoff_event`` and
  ``landing_event`` are endpoint events only;
* no default terminal altitude — not even from FABDEM / a landing-platform elevation;
* no join/leave distance derived from ``cruise_speed`` or any other implicit parameter.

The ``climb_rate_mps`` / ``descent_rate_mps`` of the procedures stay **explicit performance
evidence** and are only used for the clearly labelled diagnostic block
(``diagnostic_not_aircraft_kinematic_validation``): this module never decides whether a real
aircraft could fly the transition.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TypedDict

from .route_safety_evidence_v2 import stable_fingerprint

#: Contract version of the derived profile itself.
SCHEMA_VERSION = "route-3d-profile-v1"
PROFILE_VERSION = "route_3d_profile_v1@1.0"
PROFILE_KIND = "production_route_3d_profile"

#: The vertical datum every derived altitude is expressed in.  A profile whose terminal
#: altitudes are not already EGM2008 orthometric is ``unresolved``, never converted here.
VERTICAL_REFERENCE = "egm2008_orthometric"

#: Profile lifecycle vocabulary.  There is deliberately no ``safe``/``unsafe`` here.
PROFILE_STATUSES = (
    "not_ready", "unresolved", "failed", "pending_confirmation", "passed",
)

#: Applicability of a stored profile against the *current* upstream facts.
PROFILE_APPLICABILITY = ("current", "stale", "unresolved", "not_ready")

#: The five phase identifiers of the profile.  ``takeoff_event`` / ``landing_event`` have
#: zero horizontal extent in V1 by construction.
PHASE_IDS = ("takeoff_event", "climb", "cruise", "descent", "landing_event")

PHASE_LABELS = {
    "takeoff_event": "起飞事件（端点，无水平段）",
    "climb": "爬升",
    "cruise": "巡航",
    "descent": "下降",
    "landing_event": "着陆事件（端点，无水平段）",
}

#: Route Operating / validation boundaries recorded on every profile.
CRUISE_VALIDATION_CURRENT = "validated/current"
TERMINAL_TRANSITION_VALIDATION_NOT_EVALUATED = "not_evaluated"

SEMANTICS = {
    "thin_derivation_layer_not_a_planner": True,
    "no_3d_search": True,
    "no_dubins_airplane_or_minimum_snap": True,
    "no_kinematic_or_flight_dynamics_validation": True,
    "takeoff_and_landing_are_endpoint_events_only": True,
    "no_fabricated_zero_horizontal_distance_hover_segment": True,
    "distance_basis_is_route_operating_distance_parametrisation": True,
    "terminal_altitude_is_never_defaulted": True,
    "fabdem_platform_height_is_never_a_terminal_altitude": True,
    "join_leave_distance_is_never_derived_from_cruise_speed": True,
    "climb_descent_rate_is_performance_evidence_only": True,
    "cruise_validation_does_not_cover_climb_or_descent": True,
    "stale_profile_never_falls_back_to_constant_h": True,
}

#: The label the diagnostic block must carry verbatim.
DIAGNOSTIC_BOUNDARY = "diagnostic_not_aircraft_kinematic_validation"


class Route3DProfile(TypedDict, total=False):
    """One distance-parameterised 3D profile of one published operational route."""

    profile_id: str
    route_id: str
    adoption_id: str
    altitude_layer_id: str
    departure_procedure_id: str
    arrival_procedure_id: str
    status: str
    current_applicability: str
    vertical_reference: str
    route_length_m: float
    waypoints: list[dict]
    phases: list[dict]
    fingerprints: dict
    provenance: dict
    limitations: list[str]
    # ---- additive derivation detail (never required, never inferred) ---------------
    route_length_basis: str
    cruise_altitude_m: float
    terminal_altitude_m: dict
    join_leave: dict
    diagnostics: dict
    validation_boundary: dict
    semantics: dict


def empty_route_3d_profiles():
    """The additive container.  It ships empty: nothing is ever generated in background."""

    return {}


def route_3d_profile_id(route_id):
    """Stable, deterministic profile id for one route (never a random identifier)."""

    return "R3DP-" + stable_fingerprint(
        {"route_id": str(route_id or "")}, prefix="",
    )[:12].upper()


def profile_fingerprint(components):
    """Fingerprint of the declared upstream dependency set of one profile."""

    return stable_fingerprint(components, prefix="route3dprofile-")


def normalize_route_3d_phase(value):
    if not isinstance(value, dict):
        raise ValueError("phase 必须是对象")
    phase_id = str(value.get("phase_id") or "").strip()
    if phase_id not in PHASE_IDS:
        raise ValueError(f"不支持的 phase_id：{phase_id}")
    result = {
        "phase_id": phase_id,
        "label": PHASE_LABELS[phase_id],
        "start_distance_along_route_m": _nonnegative(
            value.get("start_distance_along_route_m"), "start_distance_along_route_m",
        ),
        "end_distance_along_route_m": _nonnegative(
            value.get("end_distance_along_route_m"), "end_distance_along_route_m",
        ),
        "horizontal_length_m": _nonnegative(
            value.get("horizontal_length_m"), "horizontal_length_m",
        ),
        "start_altitude_m": _optional_number(value.get("start_altitude_m"), "start_altitude_m"),
        "end_altitude_m": _optional_number(value.get("end_altitude_m"), "end_altitude_m"),
        "altitude_change_m": _optional_number(value.get("altitude_change_m"), "altitude_change_m"),
        "altitude_semantics": str(value.get("altitude_semantics") or "piecewise_linear"),
        "vertical_reference": str(value.get("vertical_reference") or VERTICAL_REFERENCE),
        "endpoint_event": bool(value.get("endpoint_event", False)),
        "evidence": _json_object(value.get("evidence"), "phase evidence"),
        "semantics": _json_object(value.get("semantics"), "phase semantics"),
    }
    if result["end_distance_along_route_m"] < result["start_distance_along_route_m"]:
        raise ValueError("phase 的结束里程不得小于起始里程")
    return result


def normalize_route_3d_profile(value):
    """JSON-safe, idempotent normalisation of one stored profile.

    Normalisation never repairs a conclusion: a stored ``status``/``current_applicability``
    is preserved verbatim, and nothing is filled in from a default.
    """

    if not isinstance(value, dict):
        raise ValueError("Route3DProfile 必须是对象")
    route_id = str(value.get("route_id") or "").strip()
    if not route_id:
        raise ValueError("Route3DProfile 缺少 route_id")
    status = str(value.get("status") or "not_ready")
    if status not in PROFILE_STATUSES:
        status = "not_ready"
    applicability = str(value.get("current_applicability") or "not_ready")
    if applicability not in PROFILE_APPLICABILITY:
        applicability = "not_ready"
    waypoints = []
    for item in value.get("waypoints") or []:
        if not isinstance(item, dict):
            raise ValueError("waypoint 必须是对象")
        waypoints.append({
            "distance_along_route_m": _nonnegative(
                item.get("distance_along_route_m"), "distance_along_route_m",
            ),
            "altitude_m": _number(item.get("altitude_m"), "altitude_m"),
        })
    phases = [normalize_route_3d_phase(item) for item in value.get("phases") or []]
    result = {
        "profile_id": str(value.get("profile_id") or route_3d_profile_id(route_id)),
        "profile_kind": PROFILE_KIND,
        "profile_version": PROFILE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "route_id": route_id,
        "adoption_id": _optional_text(value.get("adoption_id")),
        "altitude_layer_id": _optional_text(value.get("altitude_layer_id")),
        "departure_procedure_id": _optional_text(value.get("departure_procedure_id")),
        "arrival_procedure_id": _optional_text(value.get("arrival_procedure_id")),
        "status": status,
        "current_applicability": applicability,
        "stale_reason": value.get("stale_reason"),
        "vertical_reference": str(value.get("vertical_reference") or VERTICAL_REFERENCE),
        "route_length_m": _optional_number(value.get("route_length_m"), "route_length_m"),
        "route_length_basis": str(value.get("route_length_basis") or "unresolved"),
        "cruise_altitude_m": _optional_number(value.get("cruise_altitude_m"), "cruise_altitude_m"),
        "terminal_altitude_m": _json_object(
            value.get("terminal_altitude_m"), "terminal_altitude_m",
        ),
        "join_leave": _json_object(value.get("join_leave"), "join_leave"),
        "waypoints": waypoints,
        "phases": phases,
        "diagnostics": _json_object(value.get("diagnostics"), "diagnostics"),
        "validation_boundary": _json_object(
            value.get("validation_boundary"), "validation_boundary",
        ),
        "fingerprints": _json_object(value.get("fingerprints"), "fingerprints"),
        "provenance": _json_object(value.get("provenance"), "provenance"),
        "limitations": [str(item) for item in value.get("limitations") or []],
        "reasons": [str(item) for item in value.get("reasons") or []],
        "created_at": value.get("created_at"),
        "semantics": {**deepcopy(SEMANTICS), **(
            value.get("semantics") if isinstance(value.get("semantics"), dict) else {}
        )},
    }
    return result


def normalize_route_3d_profiles(value):
    """Normalise the additive ``spatial_3d.route_3d_profiles`` container."""

    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("route_3d_profiles 必须是对象")
    result = {}
    for key, item in value.items():
        profile = normalize_route_3d_profile(item)
        result[profile["profile_id"] or str(key)] = profile
    return result


def validation_boundary(recorded_cruise_validation=None):
    """The explicit validation boundary carried by every Route3DProfile.

    The existing ``LayeredRouteValidation`` validates a *fixed cruise altitude* only; it is
    never extended here to cover the climb/descent geometry.  ``full_3d_geometry_validated``
    is ``False`` **on the stored derivation**: the transition verdict is owned by the separate
    additive ``VerticalTransitionValidation`` artifact, and the read-only projection of the
    profile reports that verdict next to this block (see
    :func:`cns_planner.application.route_3d_profile_service.Route3DProfileService.result_snapshot`).
    The stored record itself is never rewritten by a transition evaluation.
    """

    return {
        "cruise_validation": str(recorded_cruise_validation or CRUISE_VALIDATION_CURRENT),
        "cruise_validation_scope": "fixed_cruise_altitude_only",
        "terminal_transition_validation": TERMINAL_TRANSITION_VALIDATION_NOT_EVALUATED,
        "full_3d_geometry_validated": False,
        "climb_descent_is_not_validated_by_the_cruise_validator": True,
        "next_stage": "vertical_transition_continuous_validation",
        "next_stage_implemented": True,
        "never_rewrites_layered_route_validation": True,
        "produces_safe_or_unsafe_verdict": False,
        # ``full_3d_geometry_validated`` is a *geometry evidence* statement only.
        "full_3d_geometry_validated_is_not_aircraft_kinematic_validation": True,
        "full_3d_geometry_validated_is_not_terminal_procedure_certification": True,
        "full_3d_geometry_validated_is_not_route_safe": True,
        "transition_verdict_is_projected_not_stored": True,
    }


def diagnostics_block(departure_diagnostic, arrival_diagnostic, climb_rate_mps, descent_rate_mps):
    """The clearly bounded performance diagnostic of the two transitions.

    ``implied_horizontal_speed_mps`` is a *derived* number from the explicit
    ``s_join``/``s_leave`` geometry and the explicit rate evidence.  It is not a statement
    about aircraft capability.
    """

    return {
        "policy": DIAGNOSTIC_BOUNDARY,
        "boundary_statement": (
            "以下数值只是由显式 join/leave 里程与显式爬升/下降率推出的诊断量，"
            "不判断真实飞行动力学可行性，不构成运动学或安全验证。"
        ),
        "climb_rate_mps": climb_rate_mps,
        "descent_rate_mps": descent_rate_mps,
        "climb": departure_diagnostic,
        "descent": arrival_diagnostic,
        "rate_source": "procedure_vertical_profile_explicit_performance_evidence",
        "geometry_source": "explicit_join_leave_distance_along_route_m",
        "join_leave_distance_is_not_derived_from_cruise_speed": True,
        "evaluates_real_flight_feasibility": False,
        "not_aircraft_kinematic_validation": True,
    }


def transition_diagnostic(altitude_delta_m, transition_horizontal_distance_m, rate_mps):
    """One transition diagnostic.  ``duration`` / ``implied speed`` may stay unresolved."""

    delta = _optional_number(altitude_delta_m, "altitude_delta_m")
    distance = _optional_number(transition_horizontal_distance_m, "transition_horizontal_distance_m")
    rate = _optional_number(rate_mps, "rate_mps")
    duration = None
    implied = None
    reasons = []
    if delta is None:
        reasons.append("缺少显式高度差：无法计算 duration")
    if distance is None:
        reasons.append("缺少显式过渡水平距离")
    if rate is None or rate <= 0:
        reasons.append("缺少显式性能证据（rate 必须为正）")
    if delta is not None and rate is not None and rate > 0:
        duration = abs(delta) / rate
    if duration is not None and distance is not None:
        implied = (distance / duration) if duration > 0 else None
    return {
        "altitude_delta_m": delta,
        "transition_horizontal_distance_m": distance,
        "rate_mps": rate,
        "duration_s": duration,
        "implied_horizontal_speed_mps": implied,
        "duration_basis": "abs(altitude_delta_m) / rate_mps",
        "implied_horizontal_speed_basis": "transition_horizontal_distance_m / duration_s",
        "unresolved_reasons": reasons,
        "boundary": DIAGNOSTIC_BOUNDARY,
    }


def _number(value, field):
    from math import isfinite

    if value in (None, ""):
        raise ValueError(f"{field} 必须是有限数值")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} 必须是有限数值")
    return number


def _optional_number(value, field):
    return None if value in (None, "") else _number(value, field)


def _nonnegative(value, field):
    number = _number(value, field)
    if number < 0:
        raise ValueError(f"{field} 不得小于零")
    return number


def _optional_text(value):
    if value in (None, ""):
        return None
    return str(value)


def _json_object(value, field):
    from json import dumps, loads

    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    try:
        return loads(dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 JSON-safe 对象") from exc


__all__ = [
    "CRUISE_VALIDATION_CURRENT", "DIAGNOSTIC_BOUNDARY", "PHASE_IDS", "PHASE_LABELS",
    "PROFILE_APPLICABILITY", "PROFILE_KIND", "PROFILE_STATUSES", "PROFILE_VERSION",
    "SCHEMA_VERSION", "SEMANTICS", "TERMINAL_TRANSITION_VALIDATION_NOT_EVALUATED",
    "VERTICAL_REFERENCE",
    "diagnostics_block", "empty_route_3d_profiles", "normalize_route_3d_phase",
    "normalize_route_3d_profile", "normalize_route_3d_profiles", "profile_fingerprint",
    "route_3d_profile_id", "transition_diagnostic", "validation_boundary",
]
