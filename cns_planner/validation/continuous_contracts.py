"""JSON-safe production-neutral continuous geometry and validation contracts.

V3-C is the stage that follows a V3-B ``refined_candidate``.  It takes that
candidate's already-recorded metric trajectory (the V3-B local metric frame) and

1. realizes it as a **C1 (position + heading) tangent-continuous** geometry --
   straight segments joined by explicit fillet *circular arcs* at the aircraft's
   explicit minimum turn radius;
2. linearizes the arcs with an **explicit** ``curve_chord_error_m`` (there is no
   safe default: an unstated chord error would silently claim a precision the
   geometry does not have) and keeps both the analytic geometry and the linearized
   representation, side by side, with the actual chord bound;
3. validates the realized route against source evidence: the **native** terrain raster pixels, the real
   building footprints (reusing the existing building-clearance roof semantics) and
   the explicit altitude / kinematic limits.

Design rules encoded here, which every consumer must keep:

* **C1 only.**  The realization guarantees position and heading continuity.
  Curvature can jump between a straight and an arc, so V3-C never claims
  continuous curvature / C2, and a clothoid transition is explicitly future work;
* **the arc is analytic; the polyline is an approximation.**  Vector predicates may
  be *exact on the linearized representation*, and that representation carries an
  explicit chord-error envelope.  Terrain checking is *native-raster* evidence, not
  a claim that the real world terrain is mathematically continuous;
* **unknown is never safe** -- inside ``unknown``/unconfirmed evidence, the domain
  is ``unresolved`` and the route can never reach ``validated_route``;
* a ``validated_route`` result is still **not an operational route** and **CNS has
  not been assessed**; ``operational_route`` / ``cns_assessed`` are forced false and
  V3-C never writes ``operational_routes``;
* V3-C never repairs or replans: a violation yields structured evidence plus
  ``replan_required=true`` and nothing else.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, TypedDict

V3C_MODEL_SCOPE = "continuous_geometry_realization_and_source_native_validation_v3c"
V3C_ALGORITHM_ID = "route_planner_v3_continuous_validation"
V3C_ALGORITHM_VERSION = "3.2-alpha"

CONTINUOUS_ROUTE_SCHEMA_VERSION = "3.2-continuous-route"
CONTINUOUS_PRIMITIVE_SCHEMA_VERSION = "3.2-continuous-primitive"
TURN_REALIZATION_SCHEMA_VERSION = "3.2-turn-realization"
VALIDATION_POLICY_SCHEMA_VERSION = "3.2-validation-policy"
VIOLATION_INTERVAL_SCHEMA_VERSION = "3.2-constraint-violation-interval"
DOMAIN_RESULT_SCHEMA_VERSION = "3.2-domain-validation-result"
CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION = "3.2-continuous-validation-result"

V3C_DISCLAIMER = (
    "V3-C continuous validated route：连续几何实现 + confirmed 源几何/原生栅格验证结果，"
    "仍然不是 operational route，CNS 尚未评估（cns_assessed=false）；"
    "不自动修路、不自动 replan，违反项只给出结构化证据与 replan_required=true。"
)

#: The only statuses a V3-C validation result may carry.
V3C_RESULT_STATUSES = (
    "validated_route", "failed", "unresolved", "not_ready", "validation_incomplete",
)

#: Statuses of one validation domain.
DOMAIN_STATUSES = ("passed", "failed", "unresolved", "skipped")

#: The domains V3-C validates.  ``geometry`` covers the realization itself
#: (C1 continuity, radius, chord bound), ``altitude`` the vertical profile bounds.
V3C_DOMAINS = ("geometry", "airspace", "terrain", "building", "altitude", "kinematics")
ACTIVE_V3C_DOMAINS = ("geometry", "terrain", "building", "altitude", "kinematics")

#: Result statuses that force a conservative verdict for the whole route.
STATUS_FOR_FAILURE = "failed"
STATUS_FOR_MISSING_EVIDENCE = "unresolved"
STATUS_FOR_STALE_SOURCE = "not_ready"
STATUS_FOR_RESOURCE_LIMIT = "validation_incomplete"

#: The explicit curve-error semantics.  A chord error is a *stated* engineering
#: tolerance, never a hidden default.
CURVE_ERROR_ENVELOPE_SEMANTICS = "linearized_arc_with_explicit_curve_chord_error_envelope"
CURVE_ERROR_REQUIRED = "curve_chord_error_m_is_required_and_has_no_default"
ANALYTIC_VS_LINEARIZED = (
    "arcs_are_analytic_circular_arcs_the_linestring_is_a_chord_bounded_approximation"
)
VECTOR_PREDICATE_SEMANTICS = (
    "vector_predicates_are_exact_on_the_linearized_representation_including_the_"
    "explicit_curve_chord_error_envelope_not_on_the_mathematical_curve"
)
TERRAIN_EVIDENCE_SEMANTICS = (
    "source_native_raster_validation_no_claim_of_mathematically_continuous_real_world_terrain"
)
CONTINUITY_SEMANTICS = (
    "position_and_heading_continuous_C1_curvature_may_jump_between_straight_and_arc_"
    "not_continuous_curvature_not_C2_clothoid_is_future_work"
)

#: Fingerprint components of a V3-C validation.
VALIDATION_FINGERPRINT_COMPONENTS = (
    "refinement_fingerprint", "continuous_policy_fingerprint", "curve_tolerance_fingerprint",
    "source_fingerprint", "crs_fingerprint", "validator_versions_fingerprint",
)

#: Validator versions.  They are part of the fingerprint: changing a validator
#: invalidates every stored validation even when the geometry did not move.
VALIDATOR_VERSIONS = {
    "continuous_geometry_realizer": "3.2",
    "vertical_profile_realizer": "3.2",
    "native_terrain_validator": "3.2",
    "building_polygon_validator": "3.3-height-status-multipart",
    "kinematic_validator": "3.2",
    "altitude_bounds_validator": "3.2",
    "continuous_geometry_validator": "3.2",
    "v3_continuous_validator": "3.2",
}

#: Tolerance used only for "<=" comparisons of exact geometry.
TOLERANCE = 1e-9


# --------------------------------------------------------------------------- helpers


def _finite(value):
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and float(value) == float(value)
    )


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


def contract_fingerprint(value, *, prefix=""):
    return prefix + sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode()
    ).hexdigest()


def finite_number(value):
    """Public finite-number test used by the geometry realizer and validators."""

    return _finite(value)


# --------------------------------------------------------------------------- types


class V3ValidationPolicy(TypedDict, total=False):
    """V3-C validation policy.  ``curve_chord_error_m`` has **no default value**."""

    schema_version: str
    policy_id: str
    curve_chord_error_m: float | None
    max_validation_samples: int
    max_runtime_s: float | None
    use_curve_error_envelope: bool
    airspace_allow_touching_blocked_boundary: bool
    self_intersection_is_failure: bool
    fail_closed_on_unknown: bool
    #: Optional explicit restatements of the re-validated safety parameters.
    aircraft_min_turn_radius_m: float | None
    min_altitude_egm2008_m: float | None
    max_altitude_egm2008_m: float | None
    terrain_clearance_m: float | None
    building_horizontal_clearance_m: float | None
    building_vertical_clearance_m: float | None
    max_climb_gradient: float | None
    max_descent_gradient: float | None
    source: str
    confirmed: bool
    status: str
    missing_parameters: list[str]
    reasons: list[str]
    semantics: dict[str, Any]


class ContinuousPrimitive3D(TypedDict, total=False):
    """One primitive of the realized continuous geometry.

    ``kind`` is ``straight`` (``arc`` is ``None``) or ``circular_arc`` (``arc`` holds
    the analytic centre/radius/angles/tangent points and ``linearization`` the
    explicit chord-error bound).  The vertical fields are EGM2008 altitude against the
    realized along-track distance.
    """

    schema_version: str
    primitive_id: str | None
    index: int | None
    kind: str
    distance_start_m: float | None
    distance_end_m: float | None
    horizontal_length_m: float | None
    length_3d_m: float | None
    z_start_egm2008_m: float | None
    z_end_egm2008_m: float | None
    gradient: float | None
    start_metric: list[float] | None
    end_metric: list[float] | None
    start_heading_deg: float | None
    end_heading_deg: float | None
    heading_change_deg: float | None
    vertical_reference: str
    vertical_interpolation: str
    sampled_points_metric: list[list[float]]
    arc: dict[str, Any] | None
    linearization: dict[str, Any] | None


class TurnRealization(TypedDict, total=False):
    """The analytic circular-arc fillet of one internal vertex (never a reduced radius)."""

    schema_version: str
    turn_id: str | None
    vertex_index: int | None
    vertex_metric: list[float] | None
    turn_angle_rad: float | None
    turn_angle_deg: float | None
    turn_direction: str | None
    radius_m: float | None
    min_turn_radius_m: float | None
    radius_was_reduced: bool
    radius_policy: str
    tangent_length_m: float | None
    tangent_points_metric: list[list[float]]
    arc_center_metric: list[float] | None
    arc_start_angle_rad: float | None
    arc_end_angle_rad: float | None
    arc_length_m: float | None
    arc: dict[str, Any] | None
    linearization: dict[str, Any] | None
    status: str
    failure_reason: str | None
    curve_chord_error_m: float | None
    chord_error_m: float | None
    linearization_segments: int | None
    semantics: str


class ContinuousRoute3D(TypedDict, total=False):
    """The realized continuous route: analytic primitives + bounded linearization + z(s)."""

    schema_version: str
    route_id: str | None
    status: str
    reason: str | None
    vertical_reference: str
    frame_id: str | None
    horizontal_crs: str | None
    horizontal_geometry: dict[str, Any]
    primitives: list[ContinuousPrimitive3D]
    turns: list[TurnRealization]
    turn_realization_status: str
    turn_realization_reasons: list[str]
    vertical: dict[str, Any]
    curve_error_envelope: dict[str, Any]
    operational_route: bool
    cns_assessed: bool
    final_validation_performed: bool
    semantics: dict[str, Any]


class ConstraintViolationInterval(TypedDict, total=False):
    """The unified violation / margin interval across every V3-C domain."""

    schema_version: str
    domain: str
    reason_id: str
    start_distance_m: float | None
    end_distance_m: float | None
    start_coordinate: list[float] | None
    end_coordinate: list[float] | None
    required: Any
    observed: Any
    margin: float | None
    evidence: dict[str, Any]
    severity: str


class DomainValidationResult(TypedDict, total=False):
    """One domain's verdict, its violation/unresolved intervals and its margins."""

    schema_version: str
    domain: str
    status: str
    reason: str | None
    evaluated: bool
    item_count: int
    failed_interval_count: int
    unresolved_interval_count: int
    violations: list[ConstraintViolationInterval]
    unresolved: list[ConstraintViolationInterval]
    minimum_margin: float | None
    margin_semantics: str | None
    evidence: dict[str, Any]
    resource_limited: bool
    semantics: dict[str, Any]


class V3ContinuousValidationResult(TypedDict, total=False):
    """The route-level V3-C verdict.

    ``operational_route`` / ``cns_assessed`` are **always false**: a ``validated_route``
    is a continuously validated route, not an operational one, and CNS has not been
    assessed.  ``replan_required`` carries the failure semantics; V3-C never repairs
    or replans.
    """

    schema_version: str
    status: str
    algorithm_id: str
    algorithm_version: str
    model_scope: str
    problem_id: str | None
    route_id: str | None
    experiment_id: str | None
    refinement_id: str | None
    reason: str | None
    readiness: dict[str, Any]
    validation_fingerprint: str | None
    fingerprint_components: dict[str, Any]
    refinement_fingerprint: str | None
    continuous_route: ContinuousRoute3D | None
    frame: dict[str, Any] | None
    source_audit: dict[str, Any]
    effective_policy: dict[str, Any] | None
    domains: dict[str, DomainValidationResult]
    domain_statuses: dict[str, str]
    violations: list[ConstraintViolationInterval]
    unresolved_evidence: list[ConstraintViolationInterval]
    min_margins: dict[str, float | None]
    kinematics: dict[str, Any]
    resource_limits: dict[str, Any]
    statistics: dict[str, Any]
    verdicts: dict[str, Any]
    operational_route: bool
    cns_assessed: bool
    final_validation_performed: bool
    v3c_validation_performed: bool
    disclaimer: str
    semantics: dict[str, Any]


# --------------------------------------------------------------------------- policy


def default_v3_validation_policy():
    """Explicit V3-C validation policy.  ``curve_chord_error_m`` has no default.

    The default is the *normalization* of an empty input, so a project round-trip
    (save -> load -> normalize) is byte-stable.
    """

    return normalize_v3_validation_policy(None)


def normalize_v3_validation_policy(value=None):
    source = value if isinstance(value, dict) else {}
    result = {
        "schema_version": VALIDATION_POLICY_SCHEMA_VERSION,
        "policy_id": _text(source.get("policy_id") or "v3c-validation-policy-default-empty"),
        "curve_chord_error_m": None,
        "max_validation_samples": 200000,
        "max_runtime_s": None,
        "use_curve_error_envelope": True,
        "airspace_allow_touching_blocked_boundary": False,
        "self_intersection_is_failure": False,
        "fail_closed_on_unknown": True,
        # Optional explicit restatements of the re-validated safety parameters.  When
        # omitted the V3-B planning policy supplies them; nothing is ever defaulted.
        "aircraft_min_turn_radius_m": None,
        "min_altitude_egm2008_m": None,
        "max_altitude_egm2008_m": None,
        "terrain_clearance_m": None,
        "building_horizontal_clearance_m": None,
        "building_vertical_clearance_m": None,
        "max_climb_gradient": None,
        "max_descent_gradient": None,
        "source": "未配置；curve_chord_error_m 必须由项目工程依据显式给出，没有默认安全值",
        "confirmed": False,
        "status": "pending_confirmation",
        "missing_parameters": [],
        "reasons": [],
    }
    result.update({
        "curve_chord_error_m": _optional_number(
            source.get("curve_chord_error_m"), "curve_chord_error_m", positive=True,
        ),
        "max_validation_samples": _optional_int(
            source.get("max_validation_samples"), "max_validation_samples", minimum=1,
        ) or 200000,
        "max_runtime_s": _optional_number(source.get("max_runtime_s"), "max_runtime_s", positive=True),
        "use_curve_error_envelope": bool(source.get("use_curve_error_envelope", True)),
        "airspace_allow_touching_blocked_boundary": bool(
            source.get("airspace_allow_touching_blocked_boundary", False)
        ),
        "self_intersection_is_failure": bool(source.get("self_intersection_is_failure", False)),
        "fail_closed_on_unknown": bool(source.get("fail_closed_on_unknown", True)),
        "aircraft_min_turn_radius_m": _optional_number(
            source.get("aircraft_min_turn_radius_m"), "aircraft_min_turn_radius_m", positive=True,
        ),
        "min_altitude_egm2008_m": _optional_number(
            source.get("min_altitude_egm2008_m"), "min_altitude_egm2008_m",
        ),
        "max_altitude_egm2008_m": _optional_number(
            source.get("max_altitude_egm2008_m"), "max_altitude_egm2008_m",
        ),
        "terrain_clearance_m": _optional_number(
            source.get("terrain_clearance_m"), "terrain_clearance_m", nonnegative=True,
        ),
        "building_horizontal_clearance_m": _optional_number(
            source.get("building_horizontal_clearance_m"), "building_horizontal_clearance_m",
            nonnegative=True,
        ),
        "building_vertical_clearance_m": _optional_number(
            source.get("building_vertical_clearance_m"), "building_vertical_clearance_m",
            nonnegative=True,
        ),
        "max_climb_gradient": _optional_number(
            source.get("max_climb_gradient"), "max_climb_gradient", nonnegative=True,
        ),
        "max_descent_gradient": _optional_number(
            source.get("max_descent_gradient"), "max_descent_gradient", nonnegative=True,
        ),
        "source": _text(source.get("source") or result["source"]),
        "confirmed": bool(source.get("confirmed", False)),
    })
    missing = []
    if result["curve_chord_error_m"] is None:
        missing.append("curve_chord_error_m")
    result["missing_parameters"] = missing
    if missing:
        result["reasons"].append(
            "缺少 curve_chord_error_m：圆弧 linearization 的弦高误差必须显式给出，"
            "禁止安全默认值或静默的无限精度假设"
        )
    if not result["confirmed"]:
        result["reasons"].append("V3-C validation policy 未经项目工程依据确认（confirmed=false）")
    result["status"] = (
        "confirmed" if result["confirmed"] and not missing
        else "blocked" if missing
        else "pending_confirmation"
    )
    result["semantics"] = {
        "curve_error": CURVE_ERROR_ENVELOPE_SEMANTICS,
        "curve_error_requirement": CURVE_ERROR_REQUIRED,
        "analytic_vs_linearized": ANALYTIC_VS_LINEARIZED,
        "vector_predicates": VECTOR_PREDICATE_SEMANTICS,
        "terrain_evidence": TERRAIN_EVIDENCE_SEMANTICS,
        "continuity": CONTINUITY_SEMANTICS,
        "unknown_is_never_safe": bool(result["fail_closed_on_unknown"]),
        "never_repairs_or_replans": True,
        "operational_route_always_false": True,
        "cns_assessed_always_false": True,
    }
    return result


# --------------------------------------------------------------------------- primitives


def empty_continuous_primitive(kind="straight"):
    return {
        "schema_version": CONTINUOUS_PRIMITIVE_SCHEMA_VERSION,
        "primitive_id": None,
        "index": None,
        "kind": kind if kind in ("straight", "circular_arc") else "straight",
        "distance_start_m": None,
        "distance_end_m": None,
        "horizontal_length_m": None,
        "length_3d_m": None,
        "z_start_egm2008_m": None,
        "z_end_egm2008_m": None,
        "gradient": None,
        "start_metric": None,
        "end_metric": None,
        "start_heading_deg": None,
        "end_heading_deg": None,
        "heading_change_deg": None,
        "vertical_reference": "egm2008_orthometric",
        "sampled_points_metric": [],
        "arc": None,
        "linearization": None,
    }


def empty_circular_arc():
    """Analytic circular-arc geometry (the *exact* object, not the sampled line)."""

    return {
        "center_metric": None,
        "radius_m": None,
        "start_angle_rad": None,
        "end_angle_rad": None,
        "sweep_angle_rad": None,
        "signed_sweep_rad": None,
        "arc_length_m": None,
        "chord_length_m": None,
        "tangent_points_metric": [],
        "turn_direction": None,
        "semantics": "analytic_circular_arc_exact_geometry",
    }


def empty_linearization():
    return {
        "method": None,
        "curve_chord_error_m": None,
        "chord_error_m": None,
        "chord_error_bound_m": None,
        "requested_max_chord_error_m": None,
        "segments": None,
        "angular_step_rad": None,
        "sampled_point_count": None,
        "analytic_geometry_retained": True,
        "not_the_mathematical_curve": True,
        "semantics": ANALYTIC_VS_LINEARIZED,
    }


def empty_turn_realization():
    return {
        "schema_version": TURN_REALIZATION_SCHEMA_VERSION,
        "turn_id": None,
        "vertex_index": None,
        "vertex_metric": None,
        "turn_angle_rad": None,
        "turn_angle_deg": None,
        "turn_direction": None,
        "radius_m": None,
        "min_turn_radius_m": None,
        "radius_was_reduced": False,
        "tangent_length_m": None,
        "tangent_points_metric": [],
        "arc_center_metric": None,
        "arc_start_angle_rad": None,
        "arc_end_angle_rad": None,
        "arc_length_m": None,
        "available_previous_length_m": None,
        "available_next_length_m": None,
        "status": "pending",
        "failure_reason": None,
        "curve_chord_error_m": None,
        "chord_error_m": None,
        "linearization_segments": None,
        "semantics": CONTINUITY_SEMANTICS,
    }


def empty_continuous_route():
    return {
        "schema_version": CONTINUOUS_ROUTE_SCHEMA_VERSION,
        "route_id": None,
        "status": "not_ready",
        "reason": None,
        "vertical_reference": "egm2008_orthometric",
        "frame_id": None,
        "horizontal_crs": None,
        "horizontal_geometry": {
            "analytic": {
                "primitive_count": 0,
                "straight_count": 0,
                "arc_count": 0,
                "turn_count": 0,
                "total_horizontal_length_m": None,
                "curvature_continuity": "C1_position_and_heading_only",
                "continuous_curvature": False,
                "clothoid": "future_work_not_implemented",
                "semantics": CONTINUITY_SEMANTICS,
            },
            "linearized": {
                "linestring_metric": [],
                "point_count": 0,
                "actual_max_chord_error_m": None,
                "curve_chord_error_m": None,
                "semantics": ANALYTIC_VS_LINEARIZED,
                "not_the_mathematical_curve": True,
            },
        },
        "primitives": [],
        "turns": [],
        "turn_realization_status": "not_run",
        "turn_realization_reasons": [],
        "vertical": {
            "method": "egm2008_orthometric_altitude_against_realized_along_track_distance",
            "total_distance_m": None,
            "z_start_egm2008_m": None,
            "z_end_egm2008_m": None,
            "min_z_egm2008_m": None,
            "max_z_egm2008_m": None,
            "climb_distance_m": None,
            "descent_distance_m": None,
            "level_distance_m": None,
            "max_climb_gradient_observed": None,
            "max_descent_gradient_observed": None,
        },
        "curve_error_envelope": {
            "curve_chord_error_m": None,
            "envelope_is_buffer_of": "linearized_linestring",
            "semantics": CURVE_ERROR_ENVELOPE_SEMANTICS,
        },
        "operational_route": False,
        "cns_assessed": False,
        "final_validation_performed": True,
        "semantics": {
            "scope": V3C_MODEL_SCOPE,
            "continuity": CONTINUITY_SEMANTICS,
            "continuous_curvature": False,
            "c2": False,
            "clothoid": "future_work_not_implemented",
            "operational_route": False,
            "cns_assessed": False,
            "never_repairs_or_replans": True,
        },
    }


# --------------------------------------------------------------------------- violations


def violation_interval(
    *, domain, reason_id, start_distance_m=None, end_distance_m=None, start_point=None,
    end_point=None, required=None, observed=None, margin=None, evidence=None, severity="violation",
):
    """One unified violation/margin interval across every V3-C domain."""

    return {
        "schema_version": VIOLATION_INTERVAL_SCHEMA_VERSION,
        "domain": _text(domain),
        "reason_id": _text(reason_id),
        "start_distance_m": None if start_distance_m is None else float(start_distance_m),
        "end_distance_m": None if end_distance_m is None else float(end_distance_m),
        "start_coordinate": None if start_point is None else [float(start_point[0]), float(start_point[1])],
        "end_coordinate": None if end_point is None else [float(end_point[0]), float(end_point[1])],
        "required": required,
        "observed": observed,
        "margin": None if margin is None else float(margin),
        "evidence": deepcopy(evidence or {}),
        "severity": _text(severity or "violation"),
    }


def empty_domain_result(domain, status="skipped"):
    return {
        "schema_version": DOMAIN_RESULT_SCHEMA_VERSION,
        "domain": _text(domain),
        "status": status if status in DOMAIN_STATUSES else "skipped",
        "reason": None,
        "evaluated": False,
        "item_count": 0,
        "failed_interval_count": 0,
        "unresolved_interval_count": 0,
        "violations": [],
        "unresolved": [],
        "minimum_margin": None,
        "margin_semantics": None,
        "evidence": {},
        "resource_limited": False,
        "semantics": {},
    }


# --------------------------------------------------------------------------- problem


def empty_v3_continuous_validation_problem():
    return {
        "schema_version": "3.2-continuous-validation-problem",
        "problem_id": "v3c-problem",
        "route_id": None,
        "refinement": {
            "experiment_id": None,
            "refinement_id": None,
            "status": None,
            "current_applicability": "unknown",
            "refinement_fingerprint": None,
            "metric_projection": [],
            "state_path": [],
            "frame": None,
            "fine_grid": None,
            "source_audit": {},
            "evidence_components": {},
        },
        #: The *effective* policy a V3-C run uses: the explicit V3-B planning policy
        #: (altitude band, terrain/building clearances, turn radius, gradient limits)
        #: overlaid with the explicit V3-C validation policy (curve chord error,
        #: sample budget, fail-closed options).  Never defaulted.
        "policy": default_v3_validation_policy(),
        #: The two explicit policies kept separately for provenance/audit.
        "planning_policy": {},
        "validation_policy": default_v3_validation_policy(),
        "aircraft_motion_limits": {},
        "domain_evidence": {},
        "source_audit": {},
        "expected_validation_fingerprint": None,
        "validation_fingerprint": None,
        "provenance": {},
    }


#: V3-C parameters that may come *only* from the V3-C validation policy: they are
#: additional to the V3-B planning policy and are meaningless without it.
_VALIDATION_ONLY_PARAMETERS = (
    "curve_chord_error_m", "max_validation_samples", "max_runtime_s",
    "use_curve_error_envelope", "airspace_allow_touching_blocked_boundary",
    "self_intersection_is_failure", "fail_closed_on_unknown",
)

#: Parameters that are *validation inputs* accepted from either policy.  An explicit
#: value in the V3-C validation policy wins (it is the more specific statement for
#: this run); otherwise the explicit V3-B planning policy value is used.  Neither is
#: ever defaulted.
_OVERRIDABLE_PARAMETERS = (
    "aircraft_min_turn_radius_m", "min_altitude_egm2008_m", "max_altitude_egm2008_m",
    "terrain_clearance_m", "building_horizontal_clearance_m", "building_vertical_clearance_m",
    "max_climb_gradient", "max_descent_gradient",
)


def effective_v3c_policy(planning_policy=None, validation_policy=None, *, explicit=None):
    """Merge the explicit V3-B planning policy with the explicit V3-C policy.

    The overlay is by **provenance**, not by convenience: the planning policy carries
    the safety parameters V3-C must re-validate (altitude band, clearances, turn
    radius, gradient limits) and the validation policy carries V3-C's own parameters
    (curve chord error, sample budget, fail-closed options).  A parameter stated
    explicitly in the validation policy overrides the planning value; no value is
    invented for a parameter neither side supplies.
    """

    from ..route_planner_v3.contracts import normalize_v3_planning_policy

    validation = normalize_v3_validation_policy(validation_policy)
    planning = normalize_v3_planning_policy(planning_policy)
    explicit = explicit if isinstance(explicit, dict) else {}
    merged = dict(planning)
    for name in _VALIDATION_ONLY_PARAMETERS:
        if validation.get(name) is not None or name in (
            "use_curve_error_envelope", "fail_closed_on_unknown",
            "self_intersection_is_failure", "airspace_allow_touching_blocked_boundary",
        ):
            merged[name] = validation.get(name)
    for name in _OVERRIDABLE_PARAMETERS:
        if name in explicit and explicit.get(name) not in (None, ""):
            merged[name] = validation.get(name)
    merged["validation_policy_status"] = validation.get("status")
    merged["validation_missing_parameters"] = list(validation.get("missing_parameters") or [])
    merged["validation_reasons"] = list(validation.get("reasons") or [])
    merged["validation_source"] = validation.get("source")
    merged["validation_confirmed"] = bool(validation.get("confirmed"))
    merged["validation_semantics"] = deepcopy(validation.get("semantics") or {})
    merged["curve_error_required"] = CURVE_ERROR_REQUIRED
    merged["semantics"] = deepcopy(planning.get("semantics") or {})
    merged["semantics"]["curve_error"] = CURVE_ERROR_ENVELOPE_SEMANTICS
    merged["semantics"]["continuity"] = CONTINUITY_SEMANTICS
    merged["semantics"]["vector_predicates"] = VECTOR_PREDICATE_SEMANTICS
    merged["semantics"]["terrain_evidence"] = TERRAIN_EVIDENCE_SEMANTICS
    return merged


def normalize_v3_continuous_validation_problem(value=None):
    source = value if isinstance(value, dict) else {}
    refinement = source.get("refinement") if isinstance(source.get("refinement"), dict) else {}
    validation_source = source.get("validation_policy") or source.get("policy") or {}
    validation_policy = normalize_v3_validation_policy(validation_source)
    planning_source = source.get("planning_policy")
    if planning_source is None:
        planning_source = (refinement.get("provenance") or {}).get("v3_planning_policy")
    result = empty_v3_continuous_validation_problem()
    result.update({
        "problem_id": _text(source.get("problem_id") or "v3c-problem"),
        "route_id": None if source.get("route_id") in (None, "") else _text(source.get("route_id")),
        "refinement": {
            "experiment_id": None if refinement.get("experiment_id") in (None, "") else _text(refinement.get("experiment_id")),
            "refinement_id": None if refinement.get("refinement_id") in (None, "") else _text(refinement.get("refinement_id")),
            "status": None if refinement.get("status") in (None, "") else _text(refinement.get("status")),
            "current_applicability": _text(refinement.get("current_applicability") or "unknown"),
            "refinement_fingerprint": (
                None if refinement.get("refinement_fingerprint") in (None, "")
                else _text(refinement.get("refinement_fingerprint"))
            ),
            "metric_projection": [
                [float(point[0]), float(point[1])]
                for point in refinement.get("metric_projection") or []
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ],
            "state_path": deepcopy(refinement.get("state_path") or []),
            "frame": deepcopy(refinement.get("frame") or {}) or None,
            "fine_grid": deepcopy(refinement.get("fine_grid") or {}) or None,
            "source_audit": deepcopy(refinement.get("source_audit") or {}),
            "evidence_components": deepcopy(refinement.get("evidence_components") or {}),
        },
        "planning_policy": effective_v3c_policy(
            planning_source, validation_policy,
            explicit=validation_source if isinstance(validation_source, dict) else {},
        ),
        "validation_policy": validation_policy,
        "aircraft_motion_limits": deepcopy(source.get("aircraft_motion_limits") or {}),
        "domain_evidence": deepcopy(source.get("domain_evidence") or {}),
        "source_audit": deepcopy(source.get("source_audit") or {}),
        "provenance": deepcopy(source.get("provenance") or {}),
    })
    result["policy"] = deepcopy(result["planning_policy"])
    result["expected_validation_fingerprint"] = (
        None if source.get("expected_validation_fingerprint") in (None, "")
        else _text(source.get("expected_validation_fingerprint"))
    )
    result["validation_fingerprint"] = validation_fingerprint(result)
    return result


def validation_fingerprint_components(problem):
    """The evidence a stored V3-C validation depends on.

    A change in **any** component makes the stored validation stale: the V3-B
    refinement it was built from, the continuous validation policy, the explicit
    curve tolerance, the source audits (terrain / buildings), the
    CRS/transform evidence and the validator versions themselves.
    """

    refinement = problem.get("refinement") or {}
    policy = problem.get("policy") or {}
    source_audit = problem.get("source_audit") or {}
    frame = refinement.get("frame") or {}
    return {
        "refinement_fingerprint": refinement.get("refinement_fingerprint"),
        "continuous_policy_fingerprint": contract_fingerprint(
            {key: value for key, value in policy.items() if key != "airspace_allow_touching_blocked_boundary"},
            prefix="V3CPOL-",
        ),
        "curve_tolerance_fingerprint": contract_fingerprint(
            {
                "curve_chord_error_m": policy.get("curve_chord_error_m"),
                "use_curve_error_envelope": policy.get("use_curve_error_envelope"),
            },
            prefix="V3CCURVE-",
        ),
        "source_fingerprint": (
            contract_fingerprint(
                {key: value for key, value in source_audit.items()
                 if key not in ("fingerprint", "airspace", "airspace_policy")},
                prefix="V3CSRC-",
            )
        ),
        "crs_fingerprint": contract_fingerprint(
            {
                "horizontal_crs": frame.get("horizontal_crs"),
                "horizontal_crs_source": frame.get("horizontal_crs_source"),
                "local_to_geographic": (frame.get("local_to_geographic") or {}).get("method"),
                "frame_id": frame.get("frame_id"),
                "vertical_reference": frame.get("vertical_reference"),
            },
            prefix="V3CCRS-",
        ),
        "validator_versions_fingerprint": contract_fingerprint(
            VALIDATOR_VERSIONS, prefix="V3CVAL-",
        ),
    }


def validation_fingerprint(problem):
    return contract_fingerprint(
        validation_fingerprint_components(problem), prefix="V3CVALID-",
    )


def evaluate_validation_applicability(recorded, current):
    """Compare a stored validation's fingerprint components against current evidence."""

    recorded = recorded if isinstance(recorded, dict) else {}
    current = current if isinstance(current, dict) else {}
    changed = [
        name for name in VALIDATION_FINGERPRINT_COMPONENTS
        if recorded.get(name) != current.get(name)
    ]
    status = "current" if not changed else "stale"
    return {
        "status": status,
        "changed_components": changed,
        "reasons": [] if status == "current" else [
            "V3-C validation 依赖的证据已变化（refinement/policy/curve tolerance/source/CRS/validator versions）："
            + ", ".join(changed)
        ],
        "semantics": "stale_when_refinement_policy_curve_tolerance_source_or_validator_changes",
        "recorded_fingerprint": contract_fingerprint(recorded, prefix="V3CVALID-") if recorded else None,
        "current_fingerprint": contract_fingerprint(current, prefix="V3CVALID-") if current else None,
    }


# --------------------------------------------------------------------------- result


def empty_v3_continuous_validation_result(status="not_ready"):
    return {
        "schema_version": CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION,
        "status": status if status in V3C_RESULT_STATUSES else "not_ready",
        "algorithm_id": V3C_ALGORITHM_ID,
        "algorithm_version": V3C_ALGORITHM_VERSION,
        "model_scope": V3C_MODEL_SCOPE,
        "problem_id": None,
        "route_id": None,
        "experiment_id": None,
        "refinement_id": None,
        "reason": None,
        "readiness": {},
        "validation_fingerprint": None,
        "fingerprint_components": {},
        "refinement_fingerprint": None,
        "continuous_route": None,
        "frame": None,
        "source_audit": {},
        "effective_policy": None,
        "domains": {},
        "domain_statuses": {},
        "violations": [],
        "unresolved_evidence": [],
        "min_margins": {
            "airshell_horizontal_m": None,
            "terrain_vertical_m": None,
            "building_horizontal_m": None,
            "building_vertical_m": None,
            "altitude_lower_m": None,
            "altitude_upper_m": None,
            "turn_radius_m": None,
            "climb_gradient_margin": None,
            "descent_gradient_margin": None,
        },
        "kinematics": {
            "minimum_turn_radius_observed_m": None,
            "required_minimum_turn_radius_m": None,
            "max_climb_gradient_observed": None,
            "max_descent_gradient_observed": None,
            "max_allowed_climb_gradient": None,
            "max_allowed_descent_gradient": None,
            "tangent_heading_continuity_verified": False,
            "self_intersection_diagnostic": None,
            "self_intersection_is_failure": False,
        },
        "resource_limits": {
            "max_validation_samples": None,
            "sample_count": 0,
            "max_runtime_s": None,
            "runtime_s": None,
            "resource_limited": False,
            "resource_limit_reason": None,
        },
        "statistics": {
            "domain_count": 0,
            "passed_domain_count": 0,
            "failed_domain_count": 0,
            "unresolved_domain_count": 0,
            "skipped_domain_count": 0,
            "violation_interval_count": 0,
            "unresolved_interval_count": 0,
            "primitive_count": 0,
            "arc_count": 0,
            "linearized_point_count": 0,
        },
        "verdicts": {
            "all_domains_passed": False,
            "replan_required": False,
            "automatic_repair_performed": False,
            "automatic_replan_performed": False,
            "validated_route_is_operational_route": False,
            "cns_assessed": False,
            "unknown_is_never_safe": True,
        },
        # Hard boundaries.  Even a ``validated_route`` keeps these false.
        "operational_route": False,
        "cns_assessed": False,
        "final_validation_performed": True,
        "v3c_validation_performed": True,
        "disclaimer": V3C_DISCLAIMER,
        "semantics": {
            "scope": V3C_MODEL_SCOPE,
            "continuous_geometry_realization": True,
            "source_native_validation": True,
            "continuity": CONTINUITY_SEMANTICS,
            "continuous_curvature": False,
            "c2": False,
            "clothoid": "future_work_not_implemented",
            "curve_error": CURVE_ERROR_ENVELOPE_SEMANTICS,
            "analytic_vs_linearized": ANALYTIC_VS_LINEARIZED,
            "vector_predicates": VECTOR_PREDICATE_SEMANTICS,
            "terrain_evidence": TERRAIN_EVIDENCE_SEMANTICS,
            "unknown_is_never_safe": True,
            "never_repairs_or_replans": True,
            "validated_route_is_not_operational_route": True,
            "cns_not_assessed": True,
            "next_stage": "V3-D_validated_route_operational_adapter_and_cns_assessment",
            "roadmap": [
                "V3-A_strategic", "V3-B_refinement", "V3-C_continuous_validation",
                "V3-D_operational_adapter_and_cns_assessment",
            ],
            "route_cns_joint_optimization": "future_backlog_not_implemented",
            "allowed_result_statuses": list(V3C_RESULT_STATUSES),
        },
    }


def normalize_v3_continuous_validation_result(value=None):
    result = empty_v3_continuous_validation_result()
    if not isinstance(value, dict):
        return result
    result.update(deepcopy(value))
    status = _text(value.get("status") or "not_ready")
    result["status"] = status if status in V3C_RESULT_STATUSES else "not_ready"
    result["schema_version"] = CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION
    result["algorithm_id"] = V3C_ALGORITHM_ID
    result["algorithm_version"] = V3C_ALGORITHM_VERSION
    result["model_scope"] = V3C_MODEL_SCOPE
    result["disclaimer"] = V3C_DISCLAIMER
    # The two boundaries that can never be relaxed, for any status.
    result["operational_route"] = False
    result["cns_assessed"] = False
    result["final_validation_performed"] = True
    result["v3c_validation_performed"] = True
    result.setdefault("domains", {})
    result.setdefault("violations", [])
    result.setdefault("fingerprint_components", {})
    verdicts = result.setdefault("verdicts", deepcopy(empty_v3_continuous_validation_result()["verdicts"]))
    verdicts["validated_route_is_operational_route"] = False
    verdicts["cns_assessed"] = False
    verdicts["automatic_repair_performed"] = False
    verdicts["automatic_replan_performed"] = False
    verdicts["unknown_is_never_safe"] = True
    return result


__all__ = [
    "ANALYTIC_VS_LINEARIZED", "CONTINUITY_SEMANTICS", "CONTINUOUS_PRIMITIVE_SCHEMA_VERSION",
    "CONTINUOUS_ROUTE_SCHEMA_VERSION", "CONTINUOUS_VALIDATION_RESULT_SCHEMA_VERSION",
    "CURVE_ERROR_ENVELOPE_SEMANTICS", "CURVE_ERROR_REQUIRED", "DOMAIN_RESULT_SCHEMA_VERSION",
    "DOMAIN_STATUSES", "STATUS_FOR_FAILURE", "STATUS_FOR_MISSING_EVIDENCE",
    "STATUS_FOR_RESOURCE_LIMIT", "STATUS_FOR_STALE_SOURCE", "TOLERANCE",
    "TERRAIN_EVIDENCE_SEMANTICS", "TURN_REALIZATION_SCHEMA_VERSION",
    "VALIDATION_FINGERPRINT_COMPONENTS", "VALIDATION_POLICY_SCHEMA_VERSION",
    "VALIDATOR_VERSIONS", "V3C_ALGORITHM_ID", "V3C_ALGORITHM_VERSION", "V3C_DISCLAIMER",
    "ACTIVE_V3C_DOMAINS", "V3C_DOMAINS", "V3C_MODEL_SCOPE", "V3C_RESULT_STATUSES",
    "VECTOR_PREDICATE_SEMANTICS",
    "VIOLATION_INTERVAL_SCHEMA_VERSION",
    # ---- types -----------------------------------------------------------------
    "ConstraintViolationInterval", "ContinuousPrimitive3D", "ContinuousRoute3D",
    "DomainValidationResult", "TurnRealization", "V3ContinuousValidationResult",
    "V3ValidationPolicy",
    "contract_fingerprint", "default_v3_validation_policy", "effective_v3c_policy",
    "empty_circular_arc",
    "empty_continuous_primitive", "empty_continuous_route", "empty_domain_result",
    "empty_linearization", "empty_turn_realization",
    "empty_v3_continuous_validation_problem", "empty_v3_continuous_validation_result",
    "evaluate_validation_applicability", "finite_number",
    "normalize_v3_continuous_validation_problem", "normalize_v3_continuous_validation_result",
    "normalize_v3_validation_policy", "validation_fingerprint",
    "validation_fingerprint_components", "violation_interval",
]
