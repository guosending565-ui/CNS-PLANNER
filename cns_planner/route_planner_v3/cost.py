"""Multi-component soft cost for Route Planner V3-A.

The result is always a **vector**, never a single opaque number.  Every component
records ``raw / normalized / weight / contribution / unit / source / semantics``
so an expert can audit each term separately.

* ``distance`` is the only physically additive term and is always reported in metres.
* ``population_risk``, ``traffic_risk`` and ``building_exposure`` only enter the
  scalar cost when the policy explicitly enables them *and* gives a non-negative weight.
* ``energy`` is ``pending_model`` and disabled: V3-A does not invent an energy formula.
* CNS is recorded as ``integration_mode=post_route_assessment`` with
  ``excluded_from_search_cost=true``; it never enters the search cost.

Because the scalar cost is ``distance + sum(weight * normalized)`` with
``normalized in [0, 1]`` per metre of edge, the admissible heuristic is the 3D
geometric distance times ``(1 + sum(enabled weights))``.
"""

from __future__ import annotations

from .contracts import (
    BUILDING_EXPOSURE_CLEARANCE_M, BUILDING_EXPOSURE_REFERENCE_M,
    V3_COST_VECTOR_SCHEMA_VERSION, normalize_v3_cost_vector,
)

#: Explicit, documented normalization references.  These are planning
#: normalizers, not safety thresholds: they define the [0, 1] scale of the soft
#: penalties and are recorded verbatim in the cost vector.
POPULATION_NORMALIZATION_PEOPLE = 10000.0
TRAFFIC_NORMALIZATION_AIRCRAFT = 100.0

#: Upper bound accepted for the sum of enabled soft weights.  The bound keeps
#: the documented admissible heuristic ("3D geometric distance x (1 + weight
#: sum)") a valid lower bound of every edge cost, including the documented
#: worst case of a full building-exposure penalty.
SOFT_WEIGHT_SUM_LIMIT = 1.0


class SoftCostModel:
    """Validated, explicit soft-cost configuration for one planning run."""

    def __init__(self, cost_model):
        self.cost_model = cost_model or {}
        components = self.cost_model.get("components") or {}
        self.enabled = {}
        self.weights = {}
        for name in ("distance", "population_risk", "traffic_risk", "building_exposure", "energy"):
            entry = components.get(name) or {}
            enabled = bool(entry.get("enabled"))
            weight = entry.get("weight")
            if name == "energy":
                enabled = False
                weight = None
            if enabled and weight is None:
                raise ValueError(f"启用 soft cost component {name} 必须显式提供非负 weight")
            self.enabled[name] = enabled
            self.weights[name] = None if weight is None else float(weight)
        penalty_weights = [
            self.weights[name] for name in ("population_risk", "traffic_risk", "building_exposure")
            if self.enabled[name]
        ]
        self.penalty_weight_sum = float(sum(penalty_weights))
        if self.penalty_weight_sum > SOFT_WEIGHT_SUM_LIMIT + 1e-9:
            raise ValueError(
                "启用 soft cost component 的 weight 之和不得超过 "
                f"{SOFT_WEIGHT_SUM_LIMIT}（保证启发函数仍为下界）"
            )
        self.cap = self.cost_model.get("scalar_cost_cap")
        self.cap_is_active = self.cap is not None
        self.total_weight_sum = 1.0 + self.penalty_weight_sum

    # ------------------------------------------------------------------ per edge

    def edge_penalty(self, distance_m, population_raw, traffic_raw, building_normalized):
        """Sum of explicitly weighted normalized penalties for one edge."""

        total = 0.0
        if self.enabled["population_risk"]:
            total += self.weights["population_risk"] * (float(population_raw) / POPULATION_NORMALIZATION_PEOPLE)
        if self.enabled["traffic_risk"]:
            total += self.weights["traffic_risk"] * (float(traffic_raw) / TRAFFIC_NORMALIZATION_AIRCRAFT)
        if self.enabled["building_exposure"]:
            total += self.weights["building_exposure"] * float(building_normalized)
        return total

    def edge_scalar_cost(self, distance_m, penalty):
        return float(distance_m) + float(penalty)


def building_exposure_normalized(altitude_m, required_clearance_m):
    """Dimensionless ``[0, 1]`` building proximity of one state.

    ``1`` at the required vertical clearance, ``0`` at or above
    ``BUILDING_EXPOSURE_CLEARANCE_M`` beyond it.  This is a documented
    engineering proxy for "how tight the vertical margin is"; it is **not** a
    collision probability and not an accident rate.
    """

    if required_clearance_m is None or altitude_m is None:
        return 0.0
    margin = float(altitude_m) - float(required_clearance_m)
    if margin <= 0:
        return 1.0
    if margin >= BUILDING_EXPOSURE_CLEARANCE_M:
        return 0.0
    return round(1.0 - margin / BUILDING_EXPOSURE_CLEARANCE_M, 12)


def build_cost_vector(*, distances, population_raw, traffic_raw, building_edges, model, weight_sum):
    """Assemble the JSON-safe multi-component cost vector for one candidate."""

    scalar = float(sum(distances))
    penalty_total = 0.0
    entries = {}

    distance_raw = round(scalar, 9)
    entries["distance"] = _entry(
        raw=distance_raw, normalized=1.0, weight=1.0, unit="m",
        source="v3_planner_internal_geodesic_edge_sum",
        semantics="真实三维航段长度（唯一具有物理单位的直接项）",
        enabled=True, status="passed",
        contribution=round(scalar, 9),
    )
    population_total = float(sum(population_raw))
    if model.enabled["population_risk"]:
        normalized = population_total / POPULATION_NORMALIZATION_PEOPLE
        contribution = model.weights["population_risk"] * normalized
        penalty_total += contribution
        entries["population_risk"] = _entry(
            raw=round(population_total, 9), normalized=round(normalized, 12),
            weight=model.weights["population_risk"], unit="person_m",
            source="environment.properties.population_per_cell",
            semantics="engineering_exposure_meter_person_not_probability",
            enabled=True, status="passed", contribution=round(contribution, 9),
        )
    else:
        entries["population_risk"] = _entry(
            raw=round(population_total, 9) if population_total else None, normalized=None,
            weight=model.weights["population_risk"], unit="person_m",
            source="environment.properties.population_per_cell",
            semantics="engineering_exposure_meter_person_not_probability",
            enabled=False, status="disabled_no_explicit_weight",
            reason="未在 policy.cost_model 中显式启用并给定非负 weight",
        )
    traffic_total = float(sum(traffic_raw))
    if model.enabled["traffic_risk"]:
        normalized = traffic_total / TRAFFIC_NORMALIZATION_AIRCRAFT
        contribution = model.weights["traffic_risk"] * normalized
        penalty_total += contribution
        entries["traffic_risk"] = _entry(
            raw=round(traffic_total, 9), normalized=round(normalized, 12),
            weight=model.weights["traffic_risk"], unit="aircraft_m",
            source="environment.properties.traffic_per_cell",
            semantics="engineering_exposure_meter_aircraft_not_conflict_probability",
            enabled=True, status="passed", contribution=round(contribution, 9),
        )
    else:
        entries["traffic_risk"] = _entry(
            raw=round(traffic_total, 9) if traffic_total else None, normalized=None,
            weight=model.weights["traffic_risk"], unit="aircraft_m",
            source="environment.properties.traffic_per_cell",
            semantics="engineering_exposure_meter_aircraft_not_conflict_probability",
            enabled=False, status="disabled_no_explicit_weight",
            reason="未在 policy.cost_model 中显式启用并给定非负 weight",
        )
    building_total = float(sum(building_edges))
    if model.enabled["building_exposure"]:
        contribution = model.weights["building_exposure"] * building_total
        penalty_total += contribution
        entries["building_exposure"] = _entry(
            raw=round(building_total, 12), normalized=round(building_total, 12),
            weight=model.weights["building_exposure"], unit="normalized_m",
            source=f"environment.buildings.required_clearance_egm2008_m; reference {BUILDING_EXPOSURE_REFERENCE_M:.0f} m / clearance {BUILDING_EXPOSURE_CLEARANCE_M:.0f} m",
            semantics="normalized_clearance_proximity_not_collision_probability",
            enabled=True, status="passed", contribution=round(contribution, 9),
        )
    else:
        entries["building_exposure"] = _entry(
            raw=round(building_total, 12) if building_total else None, normalized=None,
            weight=model.weights["building_exposure"], unit="normalized_m",
            source=f"environment.buildings.required_clearance_egm2008_m; reference {BUILDING_EXPOSURE_REFERENCE_M:.0f} m / clearance {BUILDING_EXPOSURE_CLEARANCE_M:.0f} m",
            semantics="normalized_clearance_proximity_not_collision_probability",
            enabled=False, status="disabled_no_explicit_weight",
            reason="未在 policy.cost_model 中显式启用并给定非负 weight",
        )
    entries["energy"] = _entry(
        raw=None, normalized=None, weight=None, unit="not_modelled",
        source="not_available",
        semantics="pending_model_disabled",
        enabled=False, status="pending_model",
        reason="V3-A 不发明 energy 公式；energy 默认 pending_model 且禁用，未进入 scalar cost。",
    )

    scalar_total = round(scalar + penalty_total, 9)
    cap_active = getattr(model, "cap_is_active", False)
    return normalize_v3_cost_vector({
        "schema_version": V3_COST_VECTOR_SCHEMA_VERSION,
        "components": entries,
        "scalar_cost_available": True,
        "scalar_cost": scalar_total,
        "scalar_cost_semantics": (
            "distance_m + sum(explicit_non_negative_weight_x_documented_normalized_component); "
            "不同单位不得直接相加，只有已明确 normalization 与非负 weight 的项进入此标量"
        ),
        "scalar_weight_sum": round(float(weight_sum), 9),
        "excluded_components": ["energy"],
        "scalar_cost_cap": model.cap,
        "scalar_cost_cap_active": cap_active,
        "cns_integration": {
            "integration_mode": "post_route_assessment",
            "excluded_from_search_cost": True,
            "semantics": "V3 第一阶段 CNS 不进搜索：Route Planning → CNS Assessment",
            "energy_and_cns_are_not_evaluated": True,
        },
        "total_raw_is_not_a_sum_of_units": True,
    })


def _entry(*, raw, normalized, weight, unit, source, semantics, enabled, status,
           contribution=None, reason=None):
    return {
        "raw": raw, "normalized": normalized, "weight": weight, "contribution": contribution,
        "unit": unit, "source": source, "semantics": semantics,
        "enabled": enabled, "status": status, "reason": reason,
    }


__all__ = [
    "POPULATION_NORMALIZATION_PEOPLE", "TRAFFIC_NORMALIZATION_AIRCRAFT",
    "SOFT_WEIGHT_SUM_LIMIT", "SoftCostModel", "building_exposure_normalized",
    "build_cost_vector",
]
