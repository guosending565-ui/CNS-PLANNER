"""JSON-safe contracts for the additive ``population_shelter`` grid attribute.

Scope of this module — and nothing more:

* it defines the **per-grid** ``population_shelter`` attribute schema
  (``grid_id`` / ``population_density_people_km2`` / ``shelter_coefficient`` /
  ``raw_exposure`` / ``risk_index`` / ``status`` / ``source`` / ``provenance``);
* it defines the dimensionless search index used by
  ``Layered Risk-Aware Theta* V2``:

  ``risk_index = <existing normalized population factor> * shelter_coefficient``

  with ``0 <= shelter_coefficient <= 1`` and ``0 <= risk_index <= 1``;
* it defines the ``shelter_coefficient_policy`` which is the **only** source of a
  shelter coefficient value.  The planner itself never hardcodes ``1.0``.

Hard boundaries (enforced by code, not by convention):

* ``raw_exposure = population_density_people_km2 * shelter_coefficient`` is a **raw**
  product in ``person/km2``.  It is *not* the search index: a raw product has no
  fixed ``[0, 1]`` range and must never be used as a cost weight.  The search only
  ever consumes ``risk_index``.
* a missing population factor, a missing shelter coefficient or an unconfigured
  policy leaves the cell ``unresolved`` — **never 0**.  The planner converts an
  unresolved cell into a hard LOS rejection when the risk weight is positive.
* nothing here computes, stores or re-scales risk: the normalized population factor
  comes from the existing canonical ``grid_risk_v2`` ground/population evidence and
  is read through the existing canonical accessor.

This module imports no QGIS/GDAL, opens no file and writes nothing.
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real

# ``stable_fingerprint`` lives in ``domain/layered_route.py`` and is the project's single
# deterministic JSON fingerprint helper.  Importing it (instead of re-implementing it)
# keeps every layered fingerprint on one canonical definition.
from .layered_route import stable_fingerprint

SCHEMA_VERSION = "population-shelter-v1"

#: The single canonical source field the existing population mapping produces.
POPULATION_DENSITY_FIELD = "population_density_people_km2"

#: ``population_shelter`` cell status vocabulary.  There is no ``feasible`` here: this is
#: an attribute with evidence, not a verdict.
SHELTER_STATUSES = ("passed", "partial", "missing_data", "unresolved", "stale")

#: The added-by-user baseline.  ``shelter_coefficient = 1.0`` means "no sheltering is
#: assumed at all", i.e. the population exposure enters the objective unattenuated.
USER_DEFINED_BASELINE_SOURCE = "user_defined_baseline"
USER_DEFINED_BASELINE_EVIDENCE = (
    "用户明确要求：本轮 shelter_coefficient 全部取 1.0（不做遮盖折减）。该值以真正的 "
    "per-grid 字段保存，可随时由已确认的 shelter 数据整体替换。"
)

POLICY_PENDING_SOURCE = "未配置；必须由项目工程依据显式确认 shelter_coefficient_policy"

#: The policy ships ``not_configured`` so that nothing is silently assumed.  A project
#: that has explicitly confirmed the user-defined baseline is normalized to ``confirmed``
#: with ``provenance = user_defined_baseline``.
DEFAULT_POLICY_STATUS = "not_configured"


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if not _finite(value) else round(float(value), 9)


def _optional_text(value):
    text = str(value or "").strip()
    return text or None


# --------------------------------------------------------------------------- policy


def default_shelter_coefficient_policy():
    """No shelter coefficient is assumed without an explicit, confirmed policy."""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": DEFAULT_POLICY_STATUS,
        "status_reason": "shelter_coefficient_policy_not_configured",
        "default_coefficient": None,
        "per_grid_overrides": {},
        "unit": "dimensionless_shelter_coefficient_0_1",
        "range": [0.0, 1.0],
        "source": POLICY_PENDING_SOURCE,
        "evidence": None,
        "confirmed": False,
        "provenance": "not_configured",
        "parameter_status": "no_default_shelter_coefficient",
        "semantics": {
            "one_is_no_sheltering_at_all": True,
            "value_is_per_grid_data_not_an_algorithm_constant": True,
            "replaceable_by_confirmed_shelter_data": True,
            "null_is_not_zero": True,
            "raw_exposure_is_not_the_search_index": True,
        },
    }


def normalize_shelter_coefficient_policy(value):
    """Validate one explicit shelter-coefficient policy.

    ``default_coefficient`` is the coefficient the *policy* assigns to every grid cell;
    it is a data value carried into the per-grid attribute, never a constant baked into
    the planner.  ``per_grid_overrides`` can replace individual cells later without any
    algorithm change.
    """

    source = value if isinstance(value, dict) else {}
    result = default_shelter_coefficient_policy()
    default = source.get("default_coefficient")
    if default in (None, ""):
        coefficient = None
    else:
        coefficient = float(default)
        if not math.isfinite(coefficient) or not 0.0 <= coefficient <= 1.0:
            raise ValueError("default_coefficient 必须是 0..1 内的有限数")
    overrides = {}
    raw_overrides = source.get("per_grid_overrides")
    if isinstance(raw_overrides, dict):
        for grid_id, raw in raw_overrides.items():
            if raw in (None, ""):
                continue
            number = float(raw)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise ValueError(f"per_grid_overrides[{grid_id}] 必须是 0..1 内的有限数")
            overrides[str(grid_id)] = number
    provenance = _optional_text(source.get("provenance")) or (
        USER_DEFINED_BASELINE_SOURCE if source.get("user_defined_baseline") else None
    )
    source_text = _optional_text(source.get("source"))
    confirmed = bool(source.get("confirmed"))
    result.update({
        "default_coefficient": coefficient,
        "per_grid_overrides": overrides,
        "source": source_text or result["source"],
        "evidence": source.get("evidence") if isinstance(source.get("evidence"), dict) else None,
        "confirmed": confirmed,
        "provenance": provenance or result["provenance"],
    })
    if coefficient is None:
        result["status"] = DEFAULT_POLICY_STATUS
        result["status_reason"] = "shelter_coefficient_policy_not_configured"
        result["parameter_status"] = "no_default_shelter_coefficient"
        return result
    if not confirmed:
        result["status"] = "pending_confirmation"
        result["status_reason"] = "shelter_coefficient_policy_not_confirmed"
        return result
    result["status"] = "confirmed"
    result["status_reason"] = None
    result["parameter_status"] = "explicit_confirmed_coefficient"
    return result


def user_defined_baseline_policy(coefficient=1.0):
    """The explicit user-confirmed baseline (``shelter_coefficient = 1.0`` everywhere)."""

    return normalize_shelter_coefficient_policy({
        "default_coefficient": coefficient,
        "source": USER_DEFINED_BASELINE_SOURCE,
        "evidence": {"statement": USER_DEFINED_BASELINE_EVIDENCE},
        "confirmed": True,
        "provenance": USER_DEFINED_BASELINE_SOURCE,
        "user_defined_baseline": True,
    })


def shelter_policy_fingerprint(policy):
    policy = policy if isinstance(policy, dict) else {}
    return stable_fingerprint({
        "status": policy.get("status"),
        "default_coefficient": policy.get("default_coefficient"),
        "per_grid_overrides": policy.get("per_grid_overrides") or {},
        "source": policy.get("source"),
        "confirmed": bool(policy.get("confirmed")),
        "provenance": policy.get("provenance"),
    }, prefix="shelterpolicyv1-")


def shelter_coefficient_for_grid(policy, grid_id):
    """Per-grid coefficient from the policy, or ``None`` (missing is never 0)."""

    policy = policy if isinstance(policy, dict) else {}
    overrides = policy.get("per_grid_overrides")
    if isinstance(overrides, dict) and str(grid_id) in overrides:
        value = overrides[str(grid_id)]
        return float(value) if _finite(value) else None
    value = policy.get("default_coefficient")
    return float(value) if _finite(value) else None


# --------------------------------------------------------------------------- attribute


def empty_population_shelter_attribute():
    return {
        "schema_version": SCHEMA_VERSION,
        "attribute": "population_shelter",
        "status": "not_calculated",
        "source": None,
        "algorithm_id": None,
        "algorithm_version": None,
        "namespace": "population_shelter",
        "grid_level": None,
        "count": 0,
        "covered_count": 0,
        "metadata": {},
        "shelter_coefficient_policy": default_shelter_coefficient_policy(),
        "shelter_coefficient_policy_fingerprint": None,
        "cells": {},
        "provenance": {},
        "semantics": {
            "per_grid_data_not_algorithm_constant": True,
            "raw_exposure_definition": "population_density_people_km2 * shelter_coefficient",
            "risk_index_definition": "normalized_population_factor * shelter_coefficient",
            "risk_index_range": [0.0, 1.0],
            "risk_index_is_dimensionless_and_not_a_probability": True,
            "missing_population_factor_is_never_zero": True,
            "missing_shelter_coefficient_is_never_zero": True,
            "not_a_shelter_safety_verdict": True,
        },
    }


def shelter_cell(
    grid_id, *, population_density_people_km2, shelter_coefficient, raw_exposure,
    risk_index, normalized_population_factor, status, source, provenance=None,
    population_factor_status=None, reason=None,
):
    return {
        "grid_id": str(grid_id),
        "population_density_people_km2": _round(population_density_people_km2),
        "shelter_coefficient": (
            None if not _finite(shelter_coefficient) else round(float(shelter_coefficient), 9)
        ),
        "raw_exposure": _round(raw_exposure),
        "risk_index": None if not _finite(risk_index) else round(float(risk_index), 9),
        "normalized_population_factor": (
            None if not _finite(normalized_population_factor)
            else round(float(normalized_population_factor), 9)
        ),
        "population_factor_status": population_factor_status,
        "status": str(status),
        "reason": reason,
        "source": source,
        "provenance": deepcopy(provenance or {}),
        "unit": {
            "population_density_people_km2": "person/km2",
            "shelter_coefficient": "dimensionless_0_1",
            "raw_exposure": "person/km2",
            "risk_index": "dimensionless_0_1",
        },
    }


def normalize_population_shelter_attribute(value):
    if not isinstance(value, dict):
        return empty_population_shelter_attribute()
    result = empty_population_shelter_attribute()
    result.update(deepcopy(value))
    result["schema_version"] = SCHEMA_VERSION
    result["attribute"] = "population_shelter"
    result["shelter_coefficient_policy"] = normalize_shelter_coefficient_policy(
        result.get("shelter_coefficient_policy")
    )
    cells = result.get("cells")
    result["cells"] = cells if isinstance(cells, dict) else {}
    result["count"] = len(result["cells"])
    result["covered_count"] = sum(
        1 for cell in result["cells"].values()
        if isinstance(cell, dict) and cell.get("status") == "passed"
    )
    result.setdefault("metadata", {})
    result.setdefault("provenance", {})
    result.setdefault("semantics", empty_population_shelter_attribute()["semantics"])
    return result


def population_shelter_fingerprint(attribute):
    """Content fingerprint of the per-grid shelter field (cells + policy)."""

    attribute = attribute if isinstance(attribute, dict) else {}
    return stable_fingerprint({
        "attribute": "population_shelter",
        "status": attribute.get("status"),
        "grid_level": attribute.get("grid_level"),
        "algorithm_id": attribute.get("algorithm_id"),
        "algorithm_version": attribute.get("algorithm_version"),
        "policy_fingerprint": shelter_policy_fingerprint(
            attribute.get("shelter_coefficient_policy")
        ),
        "cells": {
            str(grid_id): {
                "population_density_people_km2": (cell or {}).get(
                    "population_density_people_km2"
                ),
                "shelter_coefficient": (cell or {}).get("shelter_coefficient"),
                "raw_exposure": (cell or {}).get("raw_exposure"),
                "risk_index": (cell or {}).get("risk_index"),
                "status": (cell or {}).get("status"),
            }
            for grid_id, cell in sorted((attribute.get("cells") or {}).items())
        },
    }, prefix="shelterfieldv1-")


# --------------------------------------------------------------------------- resolver


def resolve_population_shelter(
    *, grid, population_attribute, normalized_population_factors, policy,
):
    """Build the per-grid ``population_shelter`` field for one grid.

    ``normalized_population_factors`` maps ``grid_id -> normalized index in [0, 1]`` and
    comes from the **existing** canonical Risk Framework V2 population factor.  A cell
    without a resolved factor stays ``unresolved`` with ``risk_index = None``.

    Every cell carries a real ``shelter_coefficient`` value (from the confirmed policy),
    so the search index is genuine per-grid data that a future confirmed shelter dataset
    can replace wholesale.
    """

    attribute = empty_population_shelter_attribute()
    policy = normalize_shelter_coefficient_policy(policy)
    cells = {}
    factors = normalized_population_factors if isinstance(
        normalized_population_factors, dict
    ) else {}
    population_cells = ((population_attribute or {}).get("cells") or {})
    population_status = str((population_attribute or {}).get("status") or "not_calculated")
    source = {
        "population_attribute": {
            "status": population_status,
            "algorithm_id": (population_attribute or {}).get("algorithm_id"),
            "algorithm_version": (population_attribute or {}).get("algorithm_version"),
        },
        "normalized_population_factor_source": (
            "existing grid_risk_v2 canonical population factor (accessors_v2)"
        ),
        "shelter_policy_source": policy.get("source"),
        "shelter_policy_provenance": policy.get("provenance"),
    }
    counts = {"passed": 0, "unresolved": 0}
    grid_ids = [
        str(cell.get("grid_id")) for cell in (grid or {}).get("cells") or []
        if isinstance(cell, dict) and cell.get("grid_id")
    ]
    for grid_id in grid_ids:
        density_cell = population_cells.get(grid_id) if isinstance(
            population_cells, dict
        ) else None
        density = (density_cell or {}).get(POPULATION_DENSITY_FIELD)
        density = float(density) if _finite(density) else None
        coefficient = shelter_coefficient_for_grid(policy, grid_id)
        factor = factors.get(grid_id)
        factor = float(factor) if _finite(factor) else None
        raw_exposure = (
            density * coefficient
            if density is not None and coefficient is not None else None
        )
        risk_index = (
            factor * coefficient
            if factor is not None and coefficient is not None else None
        )
        reason = None
        status = "passed"
        if factor is None:
            status = "unresolved"
            reason = "normalized_population_factor_unresolved"
        elif coefficient is None:
            status = "unresolved"
            reason = "shelter_coefficient_unresolved"
        elif not 0.0 <= risk_index <= 1.0:
            status = "unresolved"
            reason = "risk_index_out_of_range"
            risk_index = None
        counts[status] += 1
        cells[grid_id] = shelter_cell(
            grid_id, population_density_people_km2=density, shelter_coefficient=coefficient,
            raw_exposure=raw_exposure, risk_index=risk_index,
            normalized_population_factor=factor, status=status,
            source={
                "population_density": "existing grid_attributes.population",
                "normalized_population_factor": "existing grid_risk_v2 population factor",
                "shelter_coefficient": policy.get("source"),
            },
            provenance={
                **source,
                "shelter_policy_fingerprint": shelter_policy_fingerprint(policy),
                "grid_id": grid_id,
                "population_cell_status": (density_cell or {}).get("status"),
                "population_cell_reason": (density_cell or {}).get("reason"),
            },
            population_factor_status="resolved" if factor is not None else "unresolved",
            reason=reason,
        )
    attribute.update({
        "status": (
            "passed" if cells and counts["unresolved"] == 0
            else "partial" if counts["passed"] else "missing_data"
        ),
        "source": source,
        "algorithm_id": "population-shelter-risk-index",
        "algorithm_version": "1.0",
        "grid_level": (grid or {}).get("level"),
        "cells": cells,
        "covered_count": counts["passed"],
        "count": len(cells),
        "shelter_coefficient_policy": policy,
        "shelter_coefficient_policy_fingerprint": shelter_policy_fingerprint(policy),
        "metadata": {"risk_index_counts": counts},
        "provenance": {
            "definition": "domain/population_shelter.py::resolve_population_shelter",
            "raw_exposure": "population_density_people_km2 * shelter_coefficient",
            "risk_index": "normalized_population_factor * shelter_coefficient",
            "shelter_coefficient_is_per_grid_data": True,
            "algorithm_hardcodes_no_coefficient": True,
        },
    })
    attribute["field_fingerprint"] = population_shelter_fingerprint(attribute)
    return attribute


__all__ = [
    "DEFAULT_POLICY_STATUS", "POPULATION_DENSITY_FIELD", "POLICY_PENDING_SOURCE",
    "SCHEMA_VERSION", "SHELTER_STATUSES", "USER_DEFINED_BASELINE_EVIDENCE",
    "USER_DEFINED_BASELINE_SOURCE",
    "default_shelter_coefficient_policy", "empty_population_shelter_attribute",
    "normalize_population_shelter_attribute", "normalize_shelter_coefficient_policy",
    "population_shelter_fingerprint", "resolve_population_shelter", "shelter_cell",
    "shelter_coefficient_for_grid", "shelter_policy_fingerprint",
    "stable_fingerprint", "user_defined_baseline_policy",
]
