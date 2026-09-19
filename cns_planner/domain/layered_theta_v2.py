"""JSON-safe contracts for the ``Layered Risk-Aware Theta* V2`` planning objective.

The objective is the user-confirmed baseline::

    J = risk_weight * E_risk + turn_weight * C_turn + distance_weight * L

with the default, explicitly **user-defined** weights::

    risk = 0.8, turn = 0.1, distance = 0.1    (sum = 1)

``E_risk`` is the population × shelter risk exposure integral (metres of "risk index
length"), ``C_turn`` is a planning turn-smoothness cost expressed in metres through
``D_ref``, and ``L`` is the route length in metres.  Because all three weights are
non-negative and only the distance term pays for pure length, the straight-line metre
distance — scaled by ``distance_weight`` — stays an admissible, consistent heuristic.

The separate **evaluation** constraint ``max_route_risk_density`` is *not* a fourth
objective term.  It is applied after the search, as an acceptance/candidate-evaluation
constraint on

    route_risk_density = risk_exposure_index_m / distance_m

and it never changes the 0.8 / 0.1 / 0.1 weights or the planned geometry.

``route_risk_density`` is deliberately **not** re-added to the objective: the risk
exposure already appears there, and adding the density would count the same risk twice.
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

from .layered_route import stable_fingerprint

SCHEMA_VERSION = "layered-theta-star-v2"

ALGORITHM_ID = "layered_risk_aware_theta_star_v2"
ALGORITHM_VERSION = "2.0"

OBJECTIVE_TERM_IDS = ("risk", "turn", "distance")

#: The confirmed user baseline weights.  Marked with ``user_defined_baseline`` provenance.
USER_DEFINED_BASELINE_WEIGHTS = {"risk": 0.8, "turn": 0.1, "distance": 0.1}
USER_DEFINED_BASELINE_SOURCE = "user_defined_baseline"

OBJECTIVE_FORMULA = (
    "J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + "
    "distance_weight * distance_m"
)

#: The user's explicitly requested, deliberately wide **temporary** evaluation constraint.
TEMPORARY_WIDE_THRESHOLD = 1.0
TEMPORARY_WIDE_CONSTRAINT_SOURCE = "user_defined_temporary_wide_constraint"

DEFAULT_HEADING_BIN_COUNT = 8
DEFAULT_THETA_MIN_DEG = 5.0

#: ``D_ref`` is derived from the current L8 grid's typical step, never invented.
D_REF_PROVENANCE = (
    "derived_from_current_mh_t_l8_grid_typical_centre_to_centre_step_"
    "median_of_adjacent_cell_distances"
)

EVALUATION_STATUSES = ("passed", "failed", "unresolved")

ROUTE_RISK_DENSITY_UNIT = "dimensionless_length_weighted_mean_index"

OBJECTIVE_SEMANTICS = {
    "objective_population_shelter_only": True,
    "risk_v2_overall_not_used": True,
    "three_terms_only": True,
    "weights_non_negative_and_sum_to_one": True,
    "turn_cost_is_planning_smoothness_not_flight_dynamics": True,
    "route_risk_density_is_evaluation_not_a_fourth_objective_term": True,
    "route_risk_density_not_double_counted": True,
    "distance_weight_pays_for_pure_length": True,
    "heuristic_is_admissible_scaled_straight_line": True,
}


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if not _finite(value) else round(float(value), 9)


# --------------------------------------------------------------------------- objective policy


def default_theta_v2_objective_policy():
    """The confirmed user baseline: ``0.8 / 0.1 / 0.1``, editable, non-negative, sum = 1."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "confirmed",
        "status_reason": None,
        "risk_weight": USER_DEFINED_BASELINE_WEIGHTS["risk"],
        "turn_weight": USER_DEFINED_BASELINE_WEIGHTS["turn"],
        "distance_weight": USER_DEFINED_BASELINE_WEIGHTS["distance"],
        "source": USER_DEFINED_BASELINE_SOURCE,
        "evidence": {
            "statement": "用户已确定 baseline：J = 0.8*E_risk + 0.1*C_turn + 0.1*L",
            "editable": True,
        },
        "confirmed": True,
        "provenance": USER_DEFINED_BASELINE_SOURCE,
        "algorithm_policy_baseline": True,
        "weights_are_editable": True,
        "sum_constraint": 1.0,
        "sum_tolerance": 1e-9,
        "formula": OBJECTIVE_FORMULA,
        "objective_population_shelter_only": True,
        "semantics": deepcopy(OBJECTIVE_SEMANTICS),
    }


def normalize_theta_v2_objective_policy(value):
    """Validate editable, non-negative weights that sum to 1.

    A caller may supply any subset; the remaining terms keep the user baseline.  A
    supplied set that does not sum to 1 within ``sum_tolerance`` is rejected loudly —
    silently re-normalizing would change the confirmed baseline.
    """

    source = value if isinstance(value, dict) else {}
    result = default_theta_v2_objective_policy()
    weights = {}
    for term_id in OBJECTIVE_TERM_IDS:
        raw = source.get(f"{term_id}_weight")
        if raw in (None, ""):
            weights[term_id] = result[f"{term_id}_weight"]
            continue
        number = float(raw)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{term_id}_weight 必须是有限非负数")
        weights[term_id] = number
    total = math.fsum(weights.values())
    tolerance = float(source.get("sum_tolerance") or result["sum_tolerance"])
    if abs(total - 1.0) > tolerance:
        raise ValueError(
            f"objective weight 之和必须为 1（当前 {total!r}，容差 {tolerance!r}）："
            "不允许静默归一化"
        )
    provenance = str(source.get("provenance") or "").strip() or (
        USER_DEFINED_BASELINE_SOURCE
        if weights == USER_DEFINED_BASELINE_WEIGHTS else
        "explicit_override"
    )
    result.update({
        "risk_weight": weights["risk"],
        "turn_weight": weights["turn"],
        "distance_weight": weights["distance"],
        "provenance": provenance,
        "source": str(source.get("source") or "").strip() or provenance,
        "confirmed": bool(source.get("confirmed", True)),
        "algorithm_policy_baseline": provenance == USER_DEFINED_BASELINE_SOURCE,
    })
    if source.get("evidence") not in (None, ""):
        result["evidence"] = deepcopy(source["evidence"])
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    result["status_reason"] = None if result["confirmed"] else "objective_policy_not_confirmed"
    return result


def objective_weights(policy):
    policy = policy if isinstance(policy, dict) else default_theta_v2_objective_policy()
    return {
        term_id: float(policy[f"{term_id}_weight"]) for term_id in OBJECTIVE_TERM_IDS
    }


def objective_policy_fingerprint(policy):
    normalized = normalize_theta_v2_objective_policy(policy)
    return stable_fingerprint({
        "weights": objective_weights(normalized),
        "provenance": normalized.get("provenance"),
        "source": normalized.get("source"),
        "confirmed": bool(normalized.get("confirmed")),
    }, prefix="thetaobjv2-")


def weighted_terms(risk_exposure_index_m, turn_cost_m, distance_m, policy):
    """The fully auditable three-term decomposition, or ``None`` per unresolved term."""

    weights = objective_weights(policy)
    values = {
        "risk": risk_exposure_index_m, "turn": turn_cost_m, "distance": distance_m,
    }
    weighted, missing = {}, []
    for term_id, value in values.items():
        if _finite(value):
            weighted[term_id] = float(weights[term_id]) * float(value)
        else:
            weighted[term_id] = None
            missing.append(term_id)
    total = None if missing else math.fsum(weighted.values())
    return {
        "risk_weight": weights["risk"],
        "turn_weight": weights["turn"],
        "distance_weight": weights["distance"],
        "weighted_risk": _round(weighted["risk"]),
        "weighted_turn": _round(weighted["turn"]),
        "weighted_distance": _round(weighted["distance"]),
        "total_cost": _round(total),
        "unresolved_terms": missing,
    }


# --------------------------------------------------------------------------- theta parameters


def default_theta_v2_search_parameters():
    return {
        "schema_version": SCHEMA_VERSION,
        "heading_bin_count": DEFAULT_HEADING_BIN_COUNT,
        "theta_min_deg": DEFAULT_THETA_MIN_DEG,
        "d_ref_m": None,
        "d_ref_provenance": D_REF_PROVENANCE,
        "max_expanded_labels": None,
        "search_completeness": "not_started",
        "semantics": {
            "state": "(grid_id, incoming_heading_bin)",
            "heading_aware_multi_label": True,
            "parent_los_rewiring_retained": True,
            "not_a_heading_only_astar": True,
            "heading_bin_count_is_an_explicit_algorithm_parameter": True,
            "turn_radius_never_guessed": True,
            "theta_min_deg_is_an_explicit_algorithm_policy_parameter": True,
        },
    }


def normalize_theta_v2_search_parameters(value):
    source = value if isinstance(value, dict) else {}
    result = default_theta_v2_search_parameters()
    bins = source.get("heading_bin_count") or result["heading_bin_count"]
    bins = int(bins)
    if bins < 4 or 360 % bins != 0:
        raise ValueError("heading_bin_count 必须是不小于 4 且整除 360 的整数")
    theta_min = source.get("theta_min_deg")
    theta_min = float(DEFAULT_THETA_MIN_DEG if theta_min in (None, "") else theta_min)
    if not math.isfinite(theta_min) or not 0.0 <= theta_min <= 180.0:
        raise ValueError("theta_min_deg 必须位于 0..180")
    d_ref = source.get("d_ref_m")
    d_ref = float(d_ref) if _finite(d_ref) else None
    if d_ref is not None and d_ref <= 0:
        raise ValueError("d_ref_m 必须为正数")
    limit = source.get("max_expanded_labels")
    if limit in (None, ""):
        limit = None
    else:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("max_expanded_labels 必须是正整数或 null")
    result.update({
        "heading_bin_count": bins,
        "theta_min_deg": theta_min,
        "d_ref_m": d_ref,
        "max_expanded_labels": limit,
    })
    return result


def search_parameter_fingerprint(parameters):
    normalized = normalize_theta_v2_search_parameters(parameters)
    return stable_fingerprint({
        "heading_bin_count": normalized["heading_bin_count"],
        "theta_min_deg": normalized["theta_min_deg"],
        "d_ref_m": _round(normalized["d_ref_m"]),
        "d_ref_provenance": normalized["d_ref_provenance"],
        "max_expanded_labels": normalized["max_expanded_labels"],
    }, prefix="thetasearchv2-")


# --------------------------------------------------------------------------- risk-density constraint


def default_risk_density_constraint():
    """The user's deliberately wide temporary acceptance constraint (``1.0``)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "constraint_id": "max_route_risk_density",
        "metric": "route_risk_density",
        "threshold": TEMPORARY_WIDE_THRESHOLD,
        "comparison": "less_than_or_equal",
        "unit": ROUTE_RISK_DENSITY_UNIT,
        "source": TEMPORARY_WIDE_CONSTRAINT_SOURCE,
        "evidence": {
            "statement": (
                "用户明确要求先采用特别宽泛的临时约束 max_route_risk_density = 1.0；"
                "未来可直接收紧阈值，无需修改算法结构。"
            ),
        },
        "confirmed": True,
        "temporary": True,
        "provenance": TEMPORARY_WIDE_CONSTRAINT_SOURCE,
        "status": "confirmed",
        "role": "candidate_evaluation_acceptance_constraint",
        "objective_term": False,
        "changes_objective_weights": False,
        "semantics": {
            "evaluation_only_not_an_objective_term": True,
            "does_not_change_the_0_8_0_1_0_1_weights": True,
            "applied_to_a_finished_candidate": True,
            "threshold_is_tightenable_without_algorithm_change": True,
            "unresolved_when_distance_is_not_positive": True,
            "missing_risk_evidence_is_never_zero": True,
            "candidate_acceptance_only_not_regulatory": True,
        },
    }


def normalize_risk_density_constraint(value):
    if value in (None, ""):
        return default_risk_density_constraint()
    if not isinstance(value, dict):
        raise ValueError("max_route_risk_density constraint 必须是对象")
    result = default_risk_density_constraint()
    raw_threshold = value.get("threshold")
    if raw_threshold in (None, ""):
        threshold = result["threshold"]
    else:
        threshold = float(raw_threshold)
        if not math.isfinite(threshold) or threshold < 0:
            raise ValueError("max_route_risk_density 阈值必须是有限非负数")
    result.update({
        "threshold": threshold,
        "source": str(value.get("source") or "").strip() or result["source"],
        "confirmed": bool(value.get("confirmed", True)),
        "temporary": bool(value.get("temporary", True)),
    })
    if value.get("evidence") not in (None, ""):
        result["evidence"] = deepcopy(value["evidence"])
    result["provenance"] = (
        TEMPORARY_WIDE_CONSTRAINT_SOURCE
        if result["temporary"] and threshold == TEMPORARY_WIDE_THRESHOLD else
        str(value.get("provenance") or "explicit_override")
    )
    result["status"] = "confirmed" if result["confirmed"] else "pending_confirmation"
    return result


def risk_density_constraint_fingerprint(constraint):
    normalized = normalize_risk_density_constraint(constraint)
    return stable_fingerprint({
        "constraint_id": normalized["constraint_id"],
        "metric": normalized["metric"],
        "threshold": normalized["threshold"],
        "comparison": normalized["comparison"],
        "unit": normalized["unit"],
        "source": normalized["source"],
        "confirmed": bool(normalized["confirmed"]),
        "temporary": bool(normalized["temporary"]),
    }, prefix="riskdensityv2-")


def evaluate_route_risk_density(*, risk_exposure_index_m, distance_m, constraint):
    """Evaluate the metric against the constraint.

    ``value / threshold / status / margin`` are always reported.  ``distance_m <= 0``
    makes the metric **unresolved** (never 0), and missing risk evidence with a positive
    distance does the same — a missing integral is never treated as a clean route.
    """

    normalized = normalize_risk_density_constraint(constraint)
    threshold = normalized["threshold"]
    record = {
        "metric": "route_risk_density",
        "unit": ROUTE_RISK_DENSITY_UNIT,
        "definition": "risk_exposure_index_m / distance_m",
        "risk_exposure_index_m": _round(risk_exposure_index_m),
        "distance_m": _round(distance_m),
        "value": None,
        "threshold": _round(threshold),
        "margin": None,
        "status": "unresolved",
        "reason": None,
        "comparison": normalized["comparison"],
        "source": normalized["source"],
        "temporary_constraint": bool(normalized["temporary"]),
        "confirmed": bool(normalized["confirmed"]),
        "objective_term": False,
        "changes_objective_weights": False,
        "semantics": deepcopy(normalized["semantics"]),
    }
    if not _finite(risk_exposure_index_m):
        record["reason"] = "risk_exposure_evidence_missing"
        return record
    if not _finite(distance_m) or float(distance_m) <= 0.0:
        record["reason"] = "route_distance_not_positive"
        return record
    value = float(risk_exposure_index_m) / float(distance_m)
    record.update({
        "value": round(value, 9),
        "margin": round(float(threshold) - value, 9),
        "status": "passed" if value <= float(threshold) else "failed",
    })
    return record


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "DEFAULT_HEADING_BIN_COUNT",
    "DEFAULT_THETA_MIN_DEG", "D_REF_PROVENANCE", "EVALUATION_STATUSES",
    "OBJECTIVE_FORMULA", "OBJECTIVE_SEMANTICS", "OBJECTIVE_TERM_IDS",
    "ROUTE_RISK_DENSITY_UNIT", "SCHEMA_VERSION", "TEMPORARY_WIDE_CONSTRAINT_SOURCE",
    "TEMPORARY_WIDE_THRESHOLD", "USER_DEFINED_BASELINE_SOURCE",
    "USER_DEFINED_BASELINE_WEIGHTS",
    "default_risk_density_constraint", "default_theta_v2_objective_policy",
    "default_theta_v2_search_parameters", "evaluate_route_risk_density",
    "normalize_risk_density_constraint", "normalize_theta_v2_objective_policy",
    "normalize_theta_v2_search_parameters", "objective_policy_fingerprint",
    "objective_weights", "risk_density_constraint_fingerprint",
    "search_parameter_fingerprint", "weighted_terms",
]
