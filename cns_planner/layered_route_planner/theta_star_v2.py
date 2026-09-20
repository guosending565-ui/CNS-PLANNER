"""Layered Risk-Aware Theta* V2 — true any-angle search on the MH/T L8 grid.

Pipeline::

    scenario/OD route -> explicit confirmed AltitudeLayer (fixed H = nominal_altitude_m)
      -> coarse terrain/building feasibility mask (existing LayerFeasibilityMask semantics)
      -> population x shelter risk field (per-grid, replaceable)
      -> heading-aware multi-label Theta* with parent LOS rewiring
      -> objective J = 0.8*E_risk + 0.1*C_turn + 0.1*L
      -> candidate evaluation: max_route_risk_density (wide temporary constraint)
      -> LayeredRouteCandidate

This is a **real Theta-star** search, not A* followed by a smoother.  When a ``parent`` has a
clear line of sight to a ``target``, the search rewires ``target``'s parent to the
grandparent and takes the straight ``parent -> target`` shortcut — the path is any-angle
*during* the search.

Heading awareness is what makes the turn term meaningful.  A node is labelled by
``(grid_id, incoming_heading_bin)`` so the search can tell "arrived heading north" from
"arrived heading east" and price the turn.  The Theta* rewiring is nevertheless retained:
the heading of a shortcut label comes from the **actual parent -> target bearing**, so the
label stays consistent with the geometry.  A heading-aware A* (adjacent-cell moves only)
is explicitly *not* what this planner does.

What this module deliberately is not:

* not free 3D, no dynamic altitude, no cross-layer edge: ``z(x, y) = H`` is fixed by the
  explicitly selected layer;
* no HTAF / Safety V2 / communication optimization.  A communication field, when present,
  never changes the path or the cost in this round;
* never writes ``operational_routes`` / CNS results and never creates a
  ``RouteOperatingLayer``.  The output is a candidate that still requires the existing
  continuous validation.

The module is pure Python: no QGIS/GDAL, no raster, no file access.
"""

from __future__ import annotations

from copy import deepcopy
from heapq import heappop, heappush
import math

from ..algorithms.coverage.v1 import distance_m
from ..domain.building_clearance import building_roof_elevation, evaluate_vertical_clearance
from ..domain.communication_planning_field import communication_readiness
from ..domain.layered_route import (
    COARSE_ENVELOPE_SEMANTICS, COST_DOMAIN_IDS, candidate_fingerprint,
    default_layered_route_candidate, feasibility_policy_fingerprint,
    mask_fingerprint, request_fingerprint, stable_fingerprint,
)
from ..domain.layered_theta_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION, D_REF_PROVENANCE, OBJECTIVE_FORMULA,
    default_risk_density_constraint, default_theta_v2_objective_policy,
    default_theta_v2_search_parameters, evaluate_route_risk_density,
    normalize_risk_density_constraint, normalize_theta_v2_objective_policy,
    normalize_theta_v2_search_parameters, objective_policy_fingerprint,
    objective_weights, risk_density_constraint_fingerprint, search_parameter_fingerprint,
    theta_v2_search_parameter_view, weighted_terms,
)
from ..domain.population_shelter import (
    population_shelter_fingerprint, shelter_policy_fingerprint,
)
from ..domain.regulatory_constraints import (
    evaluate_regulatory_intersection, is_configured as regulatory_is_configured,
    regulatory_compliance_record, regulatory_constraints_fingerprint,
)
from ..risk.accessors_v2 import cell_factor_index
from ..route_planner.risk_aware_v2 import GridGraph
from .supercover import (
    CONSERVATIVE_BOUNDARY_TOUCH, CORNER_TOUCH_POLICY, LENGTH_ASSIGNMENT_SEMANTICS,
    supercover_traversal, traversal_cells_with_lengths,
)

#: Risk Framework V2 factor used as the normalized population factor.
POPULATION_FACTOR_ID = "population_exposure"

#: Traversal rejection vocabulary.
LOS_REJECTION_REASONS = (
    "terrain", "building", "regulatory", "unknown", "hard_constraint", "outside_grid",
)

SEARCH_SEMANTICS = {
    "algorithm": "theta_star_any_angle_with_parent_los_rewiring",
    "state": "(grid_id, incoming_heading_bin)",
    "heading_aware_multi_label": True,
    "parent_los_rewiring": True,
    "not_astar_plus_post_smoothing": True,
    "not_heading_only_astar": True,
    "los_is_supercover_grid_traversal_not_endpoint_test": True,
    "los_also_performs_the_hard_gate": True,
    "corner_touch_policy": CORNER_TOUCH_POLICY,
    "boundary_touch_semantics": CONSERVATIVE_BOUNDARY_TOUCH,
    "per_cell_length_semantics": LENGTH_ASSIGNMENT_SEMANTICS,
    "fixed_cruise_altitude": "z(x, y) = H = confirmed AltitudeLayer.nominal_altitude_m",
    "no_free_3d_state": True,
    "no_dynamic_altitude": True,
    "no_cross_layer_edge": True,
    "heuristic": "distance_weight * straight_line_distance_to_goal",
    "risk_v2_overall_not_used": True,
    "search_objective_risk_scope": "population_x_shelter_only",
    "communication_affects_path_or_cost": False,
    "airspace_not_used": True,
}

#: Objective / evaluation semantics recorded on every candidate.
PLANNING_OBJECTIVE_PROVENANCE = {
    "definition": "domain/layered_theta_v2.py",
    "objective_population_shelter_only": True,
    "risk_exposure_term": "sum_over_crossed_cells(length_in_cell * risk_index_cell)",
    "turn_cost_term": "d_ref_m * sum(indicator(|dpsi| > theta_min_deg) * (1 + |dpsi| / 180))",
    "distance_term": "route_distance_m",
    "reviewed_baseline": "user_defined_baseline",
    "route_risk_density_is_evaluation_not_an_objective_term": True,
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _round(value):
    return None if value is None else round(float(value), 9)


def _point(value):
    return isinstance(value, (list, tuple)) and len(value) >= 2 and all(
        _finite(item) for item in value[:2]
    )


def grid_bearing_deg(start_point, end_point):
    """Bearing in degrees, 0 = north, clockwise, in the current planning frame."""

    delta_lon = float(end_point[0]) - float(start_point[0])
    delta_lat = float(end_point[1]) - float(start_point[1])
    if math.isclose(delta_lon, 0.0, abs_tol=1e-15) and math.isclose(
        delta_lat, 0.0, abs_tol=1e-15
    ):
        return None
    return round(math.degrees(math.atan2(delta_lon, delta_lat)) % 360.0, 9)


def heading_bin_for_bearing(bearing_deg, bin_count):
    if bearing_deg is None:
        return None
    width = 360.0 / float(bin_count)
    return int(math.floor((float(bearing_deg) % 360.0) / width + 0.5)) % int(bin_count)


def heading_bin_center_deg(bin_index, bin_count):
    return round((float(bin_index) + 0.5) * (360.0 / float(bin_count)) % 360.0, 9)


def normalize_heading_delta(from_deg, to_deg):
    delta = (float(to_deg) - float(from_deg)) % 360.0
    return delta - 360.0 if delta > 180.0 else delta


def derive_d_ref_m(graph):
    """``D_ref`` derived from the current L8 grid's typical centre-to-centre step.

    The median of the adjacent-cell centre distances is used: it is robust against an
    irregular boundary cell, and it is recorded with provenance instead of being an
    invented constant.  No real aircraft turn radius is ever guessed here.
    """

    lengths = sorted(
        distance_m(graph.centers[grid_id], graph.centers[neighbour])
        for grid_id in graph.cells
        for neighbour in graph.neighbors(grid_id)
        if neighbour > grid_id and neighbour in graph.centers
    )
    if not lengths:
        return None
    middle = len(lengths) // 2
    if len(lengths) % 2:
        return float(lengths[middle])
    return (float(lengths[middle - 1]) + float(lengths[middle])) / 2.0


# --------------------------------------------------------------------------- index mapping


class GridIndexMap:
    """Bidirectional ``grid_id <-> (level, column, row)`` map with a local reverse cache."""

    def __init__(self, graph):
        self.graph = graph
        self.forward = dict(graph.indices or {})
        self.reverse = {value: grid_id for grid_id, value in self.forward.items()}
        self._local = {}

    @property
    def available(self):
        return bool(self.forward)

    def index_of(self, grid_id):
        return self.forward.get(grid_id)

    def grid_id_at(self, index):
        grid_id = self.reverse.get(index)
        if grid_id is not None:
            return grid_id
        return self._local.get(index)

    def register_local(self, index, grid_id):
        """Cache an index -> grid_id mapping discovered from that cell's own bbox.

        Used only when the canonical ``grid_id`` pattern is unavailable; the mapping is
        then derived from the cell's own bbox, never fabricated.
        """

        if index is not None:
            self._local[index] = grid_id

    def index_of_point(self, point):
        for grid_id in sorted(self.graph.cells):
            west, south, east, north = self.graph.cells[grid_id]["bbox"]
            max_east = max(c["bbox"][2] for c in self.graph.cells.values())
            max_north = max(c["bbox"][3] for c in self.graph.cells.values())
            in_lon = west <= float(point[0]) < east or (
                math.isclose(east, max_east) and west <= float(point[0]) <= east
            )
            in_lat = south <= float(point[1]) < north or (
                math.isclose(north, max_north) and south <= float(point[1]) <= north
            )
            if in_lon and in_lat:
                return self.forward.get(grid_id)
        return None


# --------------------------------------------------------------------------- LOS


def line_of_sight(
    graph, index_map, *, source_id, target_id, source_point, target_point, gate, altitude_m,
    regulatory, risk_indices, statistics,
):
    """One supercover LOS traversal that also performs the whole hard gate.

    Returns ``{"ok": bool, "cells": [{"grid_id", "length_m"}], "rejection": ...}``.

    A single traversal serves both purposes on purpose: the cells the shortcut crosses are
    exactly the cells whose risk index must be integrated *and* the cells whose terrain /
    building / hard-constraint / regulatory state decides whether the shortcut exists at
    all.  Splitting them would allow the two to drift apart.

    Any crossed cell that is blocked *or unknown* makes ``ok = False``.  A cell that only
    touches the segment at a corner is included, so a shortcut can never pass between two
    blocked cells.
    """

    statistics["los_checks"] += 1
    start_index = index_map.index_of(source_id)
    end_index = index_map.index_of(target_id)
    if start_index is None or end_index is None:
        statistics["rejected_unknown"] += 1
        return _los_rejection("outside_grid", source_id, "cell_index_unavailable")

    traversed = supercover_traversal(start_index, end_index)
    segment_length = distance_m(source_point, target_point)
    cells = traversal_cells_with_lengths(traversed, segment_length)
    resolved = []
    for entry in cells:
        grid_id = index_map.grid_id_at(entry["cell_index"])
        if grid_id is None:
            statistics["rejected_unknown"] += 1
            return _los_rejection("outside_grid", None, "traversed_cell_not_in_grid")
        entry["grid_id"] = grid_id
        resolved.append(entry)
        rejection = gate(grid_id)
        if rejection is not None:
            statistics[f"rejected_{rejection['domain']}"] += 1
            return {
                "ok": False, "cells": [], "segment_length_m": segment_length,
                "rejection": {**rejection, "grid_id": grid_id},
            }

    if regulatory_is_configured(regulatory):
        evaluation = evaluate_regulatory_intersection(
            regulatory, start=source_point, end=target_point,
            altitude_egm2008_m=altitude_m,
        )
        if evaluation["blocked"]:
            statistics["rejected_regulatory"] += 1
            return {
                "ok": False, "cells": [], "segment_length_m": segment_length,
                "rejection": {
                    "domain": "regulatory", "reason_code": "confirmed_no_fly_zone_intersected",
                    "reason": (
                        "该 LOS shortcut 与已确认禁飞 polygon 水平相交，且当前巡航高度落入其 "
                        f"vertical scope：{evaluation['blocked_by']}"
                    ),
                    "constraint_ids": evaluation["blocked_by"],
                },
            }
        if evaluation["unresolved"]:
            # A configured but unresolved constraint is never treated as safe.
            statistics["rejected_unknown"] += 1
            return {
                "ok": False, "cells": [], "segment_length_m": segment_length,
                "rejection": {
                    "domain": "unknown",
                    "reason_code": "regulatory_constraint_evidence_unresolved",
                    "reason": (
                        "该 LOS shortcut 与已配置 regulatory constraint 水平相交，但其 "
                        "confirmed 状态或 vertical scope 未解析：fail-closed"
                    ),
                    "unresolved": evaluation["unresolved"],
                },
            }

    exposure = 0.0
    complete = True
    for entry in resolved:
        index = risk_indices.get(entry["grid_id"])
        if index is None:
            complete = False
            break
        entry["risk_index"] = index
        entry["risk_contribution_m"] = entry["length_m"] * float(index)
        exposure += entry["risk_contribution_m"]
    if not complete:
        statistics["rejected_unknown"] += 1
        return _los_rejection("unknown", None, "risk_evidence_unresolved_inside_shortcut")

    statistics["los_shortcuts"] += 1
    return {
        "ok": True,
        "cells": [
            {
                "grid_id": entry["grid_id"],
                "length_m": _round(entry["length_m"]),
                "entry_fraction": entry["entry_fraction"],
                "risk_index": _round(entry.get("risk_index")),
                "risk_contribution_m": _round(entry.get("risk_contribution_m")),
            }
            for entry in resolved
        ],
        "segment_length_m": segment_length,
        "risk_exposure_index_m": exposure,
        "rejection": None,
    }


def _los_rejection(domain, grid_id, reason_code):
    return {
        "ok": False, "cells": [], "rejection": {
            "domain": domain, "grid_id": grid_id, "reason_code": reason_code,
        }, "segment_length_m": None,
    }


# --------------------------------------------------------------------------- planner


class LayeredRiskAwareThetaStarV2:
    algorithm_id = ALGORITHM_ID
    algorithm_version = ALGORITHM_VERSION
    uses_explicit_altitude_layer = True
    uses_risk_framework_v2_domains = False
    uses_risk_v2_overall = False
    operational_route = False
    #: True Theta*: the search itself is any-angle.
    uses_theta_star = True
    uses_population_shelter_risk = True

    def __init__(self, parameters=None):
        supplied = dict(parameters or {})
        allowed = {
            "heading_bin_count", "theta_min_deg", "max_expanded_labels", "d_ref_m",
            "objective_policy", "max_route_risk_density", "search_parameter_provenance",
        }
        extra = set(supplied) - allowed
        if extra:
            raise ValueError(f"Layered Risk-Aware Theta* V2 不接受参数：{sorted(extra)}")
        self.search_parameters = normalize_theta_v2_search_parameters(supplied)
        #: The declared provenance of the two search parameters.  The default is the
        #: project's software algorithm baseline; an explicit algorithm selection that
        #: changes either value is reported as ``explicit_algorithm_selection``.
        self.search_parameter_provenance = deepcopy(
            self.search_parameters["search_parameter_provenance"]
        )
        self.objective_policy = normalize_theta_v2_objective_policy(
            supplied.get("objective_policy")
        )
        self.risk_density_constraint = normalize_risk_density_constraint(
            supplied.get("max_route_risk_density")
        )
        self.parameters = {
            "heading_bin_count": self.search_parameters["heading_bin_count"],
            "theta_min_deg": self.search_parameters["theta_min_deg"],
            "max_expanded_labels": self.search_parameters["max_expanded_labels"],
            "d_ref_m": self.search_parameters["d_ref_m"],
            "search_parameter_provenance": deepcopy(self.search_parameter_provenance),
        }

    # ------------------------------------------------------------------ public API

    def plan(
        self, *, request, scenario_route, grid, layer_mask, grid_risk_v2,
        feasibility_policy, population_shelter=None, shelter_policy=None,
        regulatory_constraints=None, communication_field=None,
        hard_constraints=None, building_clearance_policy=None, source_audits=None,
        objective_policy=None, risk_density_constraint=None, cost_policy=None,
    ):
        """Plan one fixed-altitude any-angle Theta* candidate.

        ``cost_policy`` (the V1 ``LayeredRouteCostPolicy``) is accepted and ignored: the
        registry and the application service drive both layered planners through one call
        shape, and V2's objective is ``J = 0.8*E_risk + 0.1*C_turn + 0.1*L`` rather than the
        legacy ``Σ λ_domain · mean_index`` soft cost.
        """
        grid = grid if isinstance(grid, dict) else {}
        mask = layer_mask if isinstance(layer_mask, dict) else {}
        objective = normalize_theta_v2_objective_policy(
            objective_policy if objective_policy is not None else self.objective_policy
        )
        constraint = normalize_risk_density_constraint(
            risk_density_constraint if risk_density_constraint is not None
            else self.risk_density_constraint
        )
        fingerprints = self.fingerprints(
            request=request, scenario_route=scenario_route, grid=grid, layer_mask=mask,
            grid_risk_v2=grid_risk_v2, objective_policy=objective,
            risk_density_constraint=constraint, population_shelter=population_shelter,
            shelter_policy=shelter_policy, regulatory_constraints=regulatory_constraints,
            hard_constraints=hard_constraints,
            building_clearance_policy=building_clearance_policy,
            feasibility_policy=feasibility_policy, source_audits=source_audits,
        )
        communication = communication_readiness(communication_field)

        def blocked(status, reason, code, **extra):
            return self._result(
                status=status, request=request, fingerprints=fingerprints, reason=reason,
                blocking_reasons=[{"reason_code": code, "reason": reason}],
                objective_policy=objective, risk_density_constraint=constraint,
                communication=communication, **extra,
            )

        if not isinstance(request, dict) or request.get("status") != "confirmed":
            return blocked(
                "not_ready",
                "显式 LayeredRoutePlanningRequest 未确认：必须显式选择 scenario/OD 与 "
                "AltitudeLayer，不从 profile 推断高度层",
                str((request or {}).get("status_reason") or "planning_request_not_confirmed"),
            )
        cruise = (mask or {}).get("cruise_altitude") or {}
        altitude = cruise.get("altitude_egm2008_m")
        if str(cruise.get("status") or "") != "confirmed" or not _finite(altitude):
            return blocked(
                "not_ready",
                "selected AltitudeLayer 无法解析为 canonical EGM2008 固定巡航高度 H",
                str(cruise.get("reason") or "fixed_cruise_altitude_not_confirmed"),
            )
        altitude = float(altitude)
        if not grid.get("cells"):
            return blocked("missing_data", "当前 MH/T L8 标准网格不可用", "grid_unavailable")
        if str(mask.get("status") or "") != "passed" or not mask.get("cells"):
            return blocked(
                "missing_data", "LayerFeasibilityMask 缺失或不可用", "feasibility_mask_unavailable",
            )

        route = scenario_route if isinstance(scenario_route, dict) else {}
        route_id = str(route.get("route_id") or "")
        start, end = route.get("start"), route.get("end")
        if not route_id or not _point(start) or not _point(end):
            return blocked(
                "missing_data", "scenario/OD 航路端点或 route_id 缺失",
                "scenario_route_endpoints_missing",
            )

        graph = GridGraph(list(grid["cells"]))
        index_map = GridIndexMap(graph)
        source, target = graph.containing_cell(start), graph.containing_cell(end)
        if source is None or target is None:
            return blocked("blocked", "起点或终点不在当前标准网格内", "endpoints_outside_grid")

        weights = objective_weights(objective)
        risk_weight = float(weights["risk"])
        population_attribute = population_shelter if isinstance(population_shelter, dict) else {}
        shelter_cells = population_attribute.get("cells") or {}
        risk_indices, risk_unresolved = _risk_indices(graph, shelter_cells)

        # Fail-closed: a positive risk weight without per-grid population x shelter risk
        # evidence stops the run.  A missing risk index is never replaced by 0.
        if risk_weight > 0.0 and not shelter_cells:
            return blocked(
                "missing_data",
                "risk weight > 0 但 population_shelter 场缺失：population/shelter risk 无默认值，"
                "绝不补 0",
                "population_shelter_field_missing",
            )
        if risk_weight > 0.0 and risk_unresolved:
            return blocked(
                "missing_data",
                "risk weight > 0 但部分 L8 cell 的 population_shelter risk_index 未解析："
                "fail-closed，绝不补 0",
                "population_shelter_risk_unresolved",
                statistics=self._statistics(
                    None, risk_unresolved=risk_unresolved, risk_weight=risk_weight,
                ),
            )

        d_ref = self.parameters["d_ref_m"]
        if d_ref is None:
            d_ref = derive_d_ref_m(graph)
        turn_weight = float(weights["turn"])
        if turn_weight > 0.0 and (d_ref is None or d_ref <= 0):
            return blocked(
                "missing_data",
                "turn weight > 0 但无法从当前 L8 网格派生 D_ref：不猜真实航空器转弯半径",
                "d_ref_unavailable",
            )

        gate, gate_diagnostics = _build_gate(
            graph=graph, index_map=index_map, mask=mask, hard_constraints=hard_constraints,
            regulatory=regulatory_constraints, altitude=altitude,
        )
        if source in gate_diagnostics["hard_blocked"] or target in gate_diagnostics["hard_blocked"]:
            return blocked("blocked", "起点或终点落入显式硬约束范围", "endpoint_in_hard_constraint")

        statistics = self._statistics(
            None, d_ref=d_ref, risk_weight=risk_weight, turn_weight=turn_weight,
            distance_weight=float(weights["distance"]),
        )
        search = _theta_star(
            graph=graph, index_map=index_map, source=source, target=target,
            start_point=list(start), end_point=list(end), gate=gate, altitude=altitude,
            regulatory=regulatory_constraints, risk_indices=risk_indices,
            weights=weights, d_ref=d_ref,
            heading_bin_count=self.parameters["heading_bin_count"],
            theta_min_deg=self.parameters["theta_min_deg"],
            max_expanded_labels=self.parameters["max_expanded_labels"],
        )
        statistics.update(search["statistics"])
        statistics["d_ref_m"] = _round(d_ref)
        statistics["risk_unresolved_cell_count"] = len(risk_unresolved)

        if search["path"] is None:
            statistics["search_completeness"] = (
                "expansion_cap_reached_optimality_not_proven"
                if search["cap_reached"] else "no_traversable_path"
            )
            return blocked(
                "blocked",
                "Theta* 未找到 any-angle 可用路径（地形/建筑/硬约束/regulatory/风险证据任一阻断）",
                "no_traversable_path",
                mask=mask, statistics=statistics, search_incomplete=search["cap_reached"],
            )

        metrics = _objective_metrics(search, weights, d_ref, self.parameters["theta_min_deg"])
        evaluation = evaluate_route_risk_density(
            risk_exposure_index_m=metrics["risk_exposure_index_m"],
            distance_m=metrics["distance_m"], constraint=constraint,
        )
        straight = distance_m(start, end)
        statistics["search_completeness"] = (
            "expansion_cap_reached_optimality_not_proven"
            if search["cap_reached"] else "optimal_path_found"
        )
        return self._result(
            status="candidate", request=request, fingerprints=fingerprints,
            reason=(
                "Layered Risk-Aware Theta* V2 candidate 规划完成（任何角度搜索 + parent LOS "
                "rewiring + population×shelter 风险）"
            ),
            path=search["path"], grid_path=search["grid_path"], mask=mask,
            distance_m=metrics["distance_m"], straight_line_distance_m=straight,
            detour_factor=metrics["distance_m"] / straight if straight > 0 else 1.0,
            optimization_cost=metrics["total_cost"], objective=metrics["objective"],
            risk_density=evaluation, evaluation={"route_risk_density": evaluation},
            turn_statistics=metrics["turn_statistics"],
            los_segments=search["los_segments"],
            statistics=statistics,
            objective_policy=objective, risk_density_constraint=constraint,
            communication=communication,
            regulatory=regulatory_compliance_record(regulatory_constraints),
            search_incomplete=search["cap_reached"],
        )

    # ------------------------------------------------------------------ fingerprints

    def fingerprints(
        self, *, request, scenario_route, grid, layer_mask, grid_risk_v2, objective_policy,
        risk_density_constraint, population_shelter=None, shelter_policy=None,
        regulatory_constraints=None, hard_constraints=None,
        building_clearance_policy=None, feasibility_policy=None, source_audits=None,
        cost_policy=None, communication_field=None,
    ):
        """Declared dependency fingerprint of one Theta* V2 candidate.

        ``cost_policy`` (the V1 ``LayeredRouteCostPolicy``) and ``communication_field`` are
        accepted for call-shape compatibility and are **not** fingerprinted: V2 does not
        consume the legacy domain lambdas, and the communication field is not a planning
        input this round (it has a separate informational fingerprint).
        """
        route = scenario_route if isinstance(scenario_route, dict) else {}
        grid_cells = (grid or {}).get("cells") or []
        population_attribute = population_shelter if isinstance(
            population_shelter, dict
        ) else {}
        policy = shelter_policy if isinstance(shelter_policy, dict) else (
            population_attribute.get("shelter_coefficient_policy") or {}
        )
        components = {
            "scenario_route_id": route.get("route_id"),
            "scenario_route_geometry": {
                "start": list(route.get("start") or []) or None,
                "end": list(route.get("end") or []) or None,
            },
            "grid_identity": stable_fingerprint(
                [
                    {
                        "grid_id": cell.get("grid_id"), "level": cell.get("level"),
                        "bbox": cell.get("bbox"),
                    }
                    for cell in sorted(grid_cells, key=lambda item: str(item.get("grid_id")))
                ],
                prefix="layeredgridv2-",
            ),
            "altitude_layer_id": (request or {}).get("altitude_layer_id"),
            "fixed_cruise_altitude": (
                ((layer_mask or {}).get("cruise_altitude") or {}).get("altitude_egm2008_m")
            ),
            "request_fingerprint": request_fingerprint(request),
            "hard_constraints": list(hard_constraints or []),
            "terrain_source_audit": _grid_audit(source_audits, "terrain_dtm"),
            "building_source_audit": _grid_audit(source_audits, "buildings"),
            "building_grid_source_audit": _grid_audit(source_audits, "building_grid"),
            "building_clearance_policy": {
                "status": (building_clearance_policy or {}).get("status"),
                "vertical_clearance_m": (building_clearance_policy or {}).get("vertical_clearance_m"),
                "horizontal_clearance_m": (building_clearance_policy or {}).get("horizontal_clearance_m"),
                "source": (building_clearance_policy or {}).get("source"),
            },
            "feasibility_policy": feasibility_policy_fingerprint(feasibility_policy),
            "feasibility_mask_fingerprint": (layer_mask or {}).get("mask_fingerprint"),
            # Population source/normalization and the per-grid shelter field.  These are
            # planning inputs, so they are optimization-fingerprint components.
            "population_source_and_normalization": _population_source_view(grid_risk_v2),
            "shelter_field_fingerprint": population_shelter_fingerprint(population_attribute),
            "shelter_policy_fingerprint": shelter_policy_fingerprint(policy),
            "objective_policy": objective_policy_fingerprint(objective_policy),
            # The effective search parameters *and* their declared provenance: an explicit
            # algorithm selection that rewrites heading_bin_count / theta_min_deg changes
            # the candidate fingerprint instead of silently keeping the software baseline.
            "theta_parameters": search_parameter_fingerprint(self.search_parameters),
            "risk_density_constraint": risk_density_constraint_fingerprint(
                risk_density_constraint
            ),
            # Only a *configured* regulatory dataset is a planning input.  An unconfigured
            # interface contributes a constant digest, so declaring the interface does not
            # by itself change the optimization fingerprint.
            "regulatory_constraints": regulatory_constraints_fingerprint(
                regulatory_constraints
            ),
            "regulatory_constraints_configured": regulatory_is_configured(
                regulatory_constraints
            ),
            "planner_version": f"{ALGORITHM_ID}@{ALGORITHM_VERSION}",
        }
        return {
            "components": components,
            "candidate_fingerprint": candidate_fingerprint(components),
            "input_fingerprint": stable_fingerprint(components, prefix="layeredinputv2-"),
            "feasibility_fingerprint": stable_fingerprint({
                "feasibility_policy": components["feasibility_policy"],
                "mask": components["feasibility_mask_fingerprint"],
                "terrain_source_audit": components["terrain_source_audit"],
                "building_source_audit": components["building_source_audit"],
                "building_grid_source_audit": components["building_grid_source_audit"],
                "building_clearance_policy": components["building_clearance_policy"],
            }, prefix="layeredfeasibilityv2-"),
            "risk_fingerprint": stable_fingerprint({
                "population_source_and_normalization": components[
                    "population_source_and_normalization"
                ],
                "shelter_field_fingerprint": components["shelter_field_fingerprint"],
                "shelter_policy_fingerprint": components["shelter_policy_fingerprint"],
                "objective_policy": components["objective_policy"],
            }, prefix="layeredriskv2-"),
            "policy_fingerprint": stable_fingerprint({
                "objective_policy": components["objective_policy"],
                "theta_parameters": components["theta_parameters"],
                "risk_density_constraint": components["risk_density_constraint"],
                "regulatory_constraints": components["regulatory_constraints"],
            }, prefix="layeredpolicyv2-"),
            # Informational only: the communication field is not a planning input in this
            # round, so it must never influence the optimization fingerprint.
            "communication_informational_fingerprint": stable_fingerprint({
                "interface": "communication_planning_field",
                "used_in_cost": False,
                "used_as_constraint": False,
            }, prefix="commsinfov2-"),
            "request_fingerprint": components["request_fingerprint"],
        }

    # ------------------------------------------------------------------ result

    def _statistics(
        self, search, *, d_ref=None, risk_weight=None, turn_weight=None,
        distance_weight=None, risk_unresolved=None,
    ):
        record = {
            "expanded_labels": 0, "generated_labels": 0, "los_checks": 0,
            "los_shortcuts": 0, "rejected_terrain": 0, "rejected_building": 0,
            "rejected_regulatory": 0, "rejected_unknown": 0,
            "rejected_hard_constraint": 0, "rejected_outside_grid": 0,
            "rewired_parent_shortcuts": 0, "heading_bin_count": self.parameters["heading_bin_count"],
            "theta_min_deg": self.parameters["theta_min_deg"],
            "d_ref_m": _round(d_ref), "d_ref_provenance": D_REF_PROVENANCE,
            "search_parameter_provenance": deepcopy(self.search_parameter_provenance),
            "risk_weight": risk_weight, "turn_weight": turn_weight,
            "distance_weight": distance_weight,
            "search_limit": {
                "max_expanded_labels": self.parameters["max_expanded_labels"],
                "limit_reached": False, "safety_parameter": False,
            },
            "search_completeness": "not_started",
            "risk_unresolved_cell_count": len(risk_unresolved or []),
        }
        if search is not None:
            record.update(search)
        return record

    def _result(
        self, *, status, request, fingerprints, reason, blocking_reasons=None, path=None,
        grid_path=None, mask=None, distance_m=None, straight_line_distance_m=None,
        detour_factor=None, optimization_cost=None, objective=None, evaluation=None,
        risk_density=None, turn_statistics=None, los_segments=None, statistics=None,
        objective_policy=None, risk_density_constraint=None, communication=None,
        regulatory=None, search_incomplete=False, mask_status=None,
    ):
        objective_policy = objective_policy or default_theta_v2_objective_policy()
        risk_density_constraint = risk_density_constraint or default_risk_density_constraint()
        candidate = default_layered_route_candidate(
            (request or {}).get("scenario_route_id") or _od_label(request),
            (request or {}).get("altitude_layer_id"), status,
        )
        weights = objective_weights(objective_policy)
        # The effective search parameters of this run, including the grid-derived D_ref when
        # it was not supplied explicitly.  ``statistics`` already carries that value, so the
        # candidate and the readiness view never have to re-derive it.
        effective_d_ref = self.parameters["d_ref_m"]
        if isinstance(statistics, dict) and statistics.get("d_ref_m") is not None:
            effective_d_ref = statistics.get("d_ref_m")
        search_view = theta_v2_search_parameter_view({
            "heading_bin_count": self.parameters["heading_bin_count"],
            "theta_min_deg": self.parameters["theta_min_deg"],
            "max_expanded_labels": self.parameters["max_expanded_labels"],
            "d_ref_m": effective_d_ref,
            "search_parameter_provenance": deepcopy(self.search_parameter_provenance),
        })
        objective_record = {
            "risk_exposure_index_m": None, "turn_count": 0,
            "total_heading_change_deg": 0.0, "turn_cost_m": None, "distance_m": None,
            "risk_weight": weights["risk"], "turn_weight": weights["turn"],
            "distance_weight": weights["distance"],
            "weighted_risk": None, "weighted_turn": None, "weighted_distance": None,
            "total_cost": None, "formula": OBJECTIVE_FORMULA,
            "objective_population_shelter_only": True,
            "risk_v2_overall_used": False,
            "route_risk_density_is_not_an_objective_term": True,
            "provenance": dict(PLANNING_OBJECTIVE_PROVENANCE),
            "policy_fingerprint": objective_policy_fingerprint(objective_policy),
            "weights_provenance": objective_policy.get("provenance"),
            "d_ref_m": self.parameters["d_ref_m"],
            "theta_min_deg": self.parameters["theta_min_deg"],
            "search_parameters": search_view,
        }
        if objective:
            objective_record.update(objective)
        cost_breakdown = {
            # Objective terms of Theta* V2.
            "objective": objective_record,
            "distance_contribution_m": _round((objective or {}).get("weighted_distance")),
            "risk_contribution_m": _round((objective or {}).get("weighted_risk")),
            "turn_contribution_m": _round((objective or {}).get("weighted_turn")),
            "weights": dict(weights),
            "active_objective_terms": [
                term_id for term_id, value in weights.items() if float(value) > 0.0
            ],
            # Compatibility block: the existing RouteRiskProfile reads the per-domain
            # exposure/mean/lambda keys.  Theta* V2 does not weight the three legacy Risk
            # Framework V2 domains, so the lambdas stay exactly 0 and the profile remains
            # consistent instead of being declared stale.
            "lambda_weighted_contributions_m": {domain_id: None for domain_id in COST_DOMAIN_IDS},
            "domain_exposure_index_m": {domain_id: None for domain_id in COST_DOMAIN_IDS},
            "mean_domain_index": {domain_id: None for domain_id in COST_DOMAIN_IDS},
            "lambdas": {domain_id: 0.0 for domain_id in COST_DOMAIN_IDS},
            "active_domains": [],
            "risk_framework_v2_domains_used_in_objective": False,
            "formula": OBJECTIVE_FORMULA,
            "legacy_formula_not_used": "edge_cost = d * (1 + sum_lambda_domain * mean_domain_index)",
            "heuristic": "distance_weight * straight_line_distance_to_goal_admissible",
            "risk_v2_overall_used": False,
            "risk_density_added_to_objective": False,
        }
        candidate.update({
            "algorithm_id": ALGORITHM_ID,
            "algorithm_version": ALGORITHM_VERSION,
            "candidate_id": _candidate_id(fingerprints, request),
            "path": list(path or []),
            "grid_path": list(grid_path or []),
            "cell_count": len(grid_path or []),
            "distance_m": _round(distance_m),
            "straight_line_distance_m": _round(straight_line_distance_m),
            "detour_factor": None if detour_factor is None else round(float(detour_factor), 9),
            "optimization_cost": _round(optimization_cost),
            "cost_breakdown": cost_breakdown,
            "planning_objective": objective_record,
            "search_parameters": search_view,
            "evaluation": evaluation or {"route_risk_density": risk_density},
            "route_risk_density": risk_density,
            "turn_statistics": turn_statistics or _empty_turn_statistics(),
            "los_segments": list(los_segments or []),
            "search_statistics": statistics or {},
            "statistics": statistics or {},
            "reason": reason,
            "blocking_reasons": list(blocking_reasons or []),
            "input_fingerprint": fingerprints["input_fingerprint"],
            "feasibility_fingerprint": fingerprints["feasibility_fingerprint"],
            "risk_fingerprint": fingerprints["risk_fingerprint"],
            "policy_fingerprint": fingerprints["policy_fingerprint"],
            "request_fingerprint": fingerprints["request_fingerprint"],
            "candidate_fingerprint": fingerprints["candidate_fingerprint"],
            "communication_informational_fingerprint": fingerprints.get(
                "communication_informational_fingerprint"
            ),
            "feasibility_mask_fingerprint": (mask or {}).get("mask_fingerprint"),
            "search_incomplete": bool(search_incomplete),
            "mask_status": mask_status,
            "provenance": {
                "pipeline": (
                    "scenario_or_od_route -> explicit_fixed_altitude_H -> terrain_building_"
                    "feasibility_mask -> population_x_shelter_risk -> heading_aware_multi_label_"
                    "theta_star_with_parent_los_rewiring -> candidate_evaluation -> "
                    "layered_route_candidate"
                ),
                "fingerprint_components": fingerprints["components"],
                "search_semantics": SEARCH_SEMANTICS,
                "search_parameters": search_view,
                "feasibility_semantics": COARSE_ENVELOPE_SEMANTICS,
                "objective": dict(PLANNING_OBJECTIVE_PROVENANCE),
                "risk_density_constraint": {
                    "threshold": risk_density_constraint.get("threshold"),
                    "source": risk_density_constraint.get("source"),
                    "temporary": risk_density_constraint.get("temporary"),
                    "role": risk_density_constraint.get("role"),
                    "objective_term": False,
                    "changes_objective_weights": False,
                },
                "regulatory_compliance": regulatory or {
                    "regulatory_compliance": "not_evaluated",
                    "status": "not_evaluated",
                },
                "communication": communication or communication_readiness(None),
                "airspace": {
                    "status": "not_applicable", "applicability": "display_only",
                    "used_in_search": False, "used_in_fingerprint": False,
                    "semantics": "display_only_airspace_never_enters_theta_star_hard_gate_or_fingerprint",
                },
            },
        })
        return candidate


# --------------------------------------------------------------------------- risk indices


def _risk_indices(graph, shelter_cells):
    """Per-cell dimensionless ``risk_index`` from the per-grid population x shelter field."""

    indices, unresolved = {}, []
    for grid_id in graph.cells:
        cell = shelter_cells.get(grid_id) if isinstance(shelter_cells, dict) else None
        value = (cell or {}).get("risk_index")
        if _finite(value) and 0.0 <= float(value) <= 1.0:
            indices[grid_id] = float(value)
        else:
            unresolved.append(grid_id)
    return indices, sorted(unresolved)


def _population_source_view(grid_risk_v2):
    """Population source + normalization provenance from the canonical V2 result."""

    item = grid_risk_v2 if isinstance(grid_risk_v2, dict) else {}
    reference = (item.get("references") or {}).get(POPULATION_FACTOR_ID) or {}
    summary = (item.get("factor_status") or {}).get(POPULATION_FACTOR_ID) or {}
    return {
        "factor_id": POPULATION_FACTOR_ID,
        "input_fingerprint": item.get("input_fingerprint"),
        "policy_fingerprint": item.get("policy_fingerprint"),
        "reference": reference,
        "normalization": summary.get("normalization"),
        "source_id": summary.get("source_id"),
        "source_fingerprint": summary.get("source_fingerprint"),
        "status": summary.get("status"),
    }


def _grid_audit(source_audits, role):
    items = (source_audits or {}).get("items")
    entry = items.get(role) if isinstance(items, dict) else None
    if not isinstance(entry, dict):
        return None
    return {
        "status": entry.get("status"), "source_id": entry.get("source_id"),
        "version_fingerprint": entry.get("version_fingerprint"),
        "sha256": entry.get("sha256"), "size_bytes": entry.get("size_bytes"),
        "mtime_ns": entry.get("mtime_ns"),
    }


def _od_label(request):
    item = request if isinstance(request, dict) else {}
    start, end = item.get("start_node_id"), item.get("end_node_id")
    return f"{start}->{end}" if start and end else None


def _candidate_id(fingerprints, request):
    item = request if isinstance(request, dict) else {}
    route_id = item.get("scenario_route_id") or _od_label(request) or "route"
    epoch = (item.get("evidence") or {}).get("evaluated_at")
    suffix = str(epoch) if epoch else fingerprints["candidate_fingerprint"][:16]
    return f"LRC2-{route_id}-{item.get('altitude_layer_id') or 'layer'}-{suffix}"


def _empty_turn_statistics():
    return {
        "turn_count": 0, "total_heading_change_deg": 0.0, "turn_cost_m": 0.0,
        "turns": [], "theta_min_deg": None, "d_ref_m": None,
        "semantics": "planning_smoothness_proxy_not_flight_dynamics_validation",
    }


# --------------------------------------------------------------------------- hard gate


def _build_gate(*, graph, index_map, mask, hard_constraints, regulatory, altitude):
    """Per-cell hard gate derived from the **existing** feasibility mask semantics.

    ``mask`` is the existing ``LayerFeasibilityMask``: its cells already encode

    * ``H >= terrain_max_egm2008 + explicit terrain clearance`` (terrain floor), and
    * the coarse strategic building floor
      ``terrain_max + height_max + existing confirmed building vertical clearance``.

    A building cell is therefore **not** blocked merely because a building exists: it is
    blocked only when ``H`` is below the building floor, and it is traversable over the
    obstacle when ``H`` is at or above it.  ``building_count == 0`` carries no vertical
    building constraint at all, and an unresolved height/terrain/fact is ``unknown`` —
    which the LOS traversal treats as non-crossable.
    """

    mask_cells = mask.get("cells") or {}
    hard_blocked = {
        grid_id for grid_id, cell in graph.cells.items()
        if any(
            _positive_bbox_intersection(cell["bbox"], _constraint_bbox(item))
            for item in hard_constraints or []
        )
    }
    diagnostics = {"hard_blocked": hard_blocked, "gate_reasons": {}, "gate_domains": {}}

    def classify(grid_id):
        if grid_id in hard_blocked:
            return "hard_constraint", "hard_constraint_intersection"
        cell = mask_cells.get(grid_id)
        if not isinstance(cell, dict):
            return "unknown", "cell_outside_feasibility_mask"
        status = str(cell.get("status") or "")
        if status == "feasible":
            return None, None
        reason_code = str(cell.get("reason_code") or "")
        if status == "blocked":
            if reason_code == "altitude_below_terrain_floor":
                return "terrain", reason_code
            if reason_code == "altitude_below_building_clearance_floor":
                return "building", reason_code
            return "unknown", reason_code or "blocked_without_terrain_or_building_reason"
        return "unknown", reason_code or "feasibility_unknown"

    def gate(grid_id):
        domain, reason_code = classify(grid_id)
        diagnostics["gate_reasons"][grid_id] = reason_code
        diagnostics["gate_domains"][grid_id] = domain
        if domain is None:
            return None
        return {"domain": domain, "reason_code": reason_code}

    return gate, diagnostics


def _positive_bbox_intersection(a, b):
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return False
    try:
        return (
            min(a[2], float(b[2])) > max(a[0], float(b[0]))
            and min(a[3], float(b[3])) > max(a[1], float(b[1]))
        )
    except (TypeError, ValueError):
        return False


def _constraint_bbox(item):
    return item.get("bbox") if isinstance(item, dict) else None


# --------------------------------------------------------------------------- Theta*


def _theta_star(
    *, graph, index_map, source, target, start_point, end_point, gate, altitude, regulatory,
    risk_indices, weights, d_ref, heading_bin_count, theta_min_deg, max_expanded_labels,
):
    """Heading-aware multi-label Theta* with parent LOS rewiring.

    Labels are ``(grid_id, incoming_heading_bin)``.  For every generated label two routes
    are considered, exactly as in Theta*:

    * **path 1** — a direct step from the popped label to an adjacent cell, which requires
      the ``parent -> neighbour`` supercover LOS to be clear; the new label's parent is the
      popped label (this is what preserves the parent chain);
    * **path 2** — the *shortcut*: the popped label's own parent's parent (the
      grandparent) is path-1-reachable from the neighbour, so ``neighbour`` is rewired to
      the grandparent and the straight ``grandparent -> neighbour`` segment is taken.

    The heuristic is ``distance_weight * straight_line_distance_to_goal``.  With
    non-negative risk and turn penalties this stays an admissible, consistent lower bound
    on the remaining distance term, so the search keeps its optimality character under the
    label-setting rule.  No goal heading is required, which is what makes the relaxation
    valid: a straight line is always available to every goal label.
    """

    weight_distance = float(weights["distance"])
    weight_risk = float(weights["risk"])
    weight_turn = float(weights["turn"])
    statistics = {
        "expanded_labels": 0, "generated_labels": 0, "los_checks": 0, "los_shortcuts": 0,
        "rejected_terrain": 0, "rejected_building": 0, "rejected_regulatory": 0,
        "rejected_unknown": 0, "rejected_hard_constraint": 0, "rejected_outside_grid": 0,
        "rewired_parent_shortcuts": 0,
    }

    def heuristic(grid_id):
        return weight_distance * distance_m(graph.centers[grid_id], graph.centers[target])

    def los(from_id, to_id, from_point, to_point):
        return line_of_sight(
            graph, index_map, source_id=from_id, target_id=to_id,
            source_point=from_point, target_point=to_point, gate=gate,
            altitude_m=altitude, regulatory=regulatory, risk_indices=risk_indices,
            statistics=statistics,
        )

    start_grid = source
    # The route has no preceding turn, so the start label carries **no** reference heading:
    # the first real segment is never charged one.  The bin is still filled from the initial
    # direction to the goal, which only labels the start state; it is not a flown heading.
    start_bin = heading_bin_for_bearing(
        grid_bearing_deg(start_point, end_point), heading_bin_count
    )
    start_key = (start_grid, start_bin)

    costs = {start_key: 0.0}
    parents = {start_key: None}        # grid_id of the label's Theta* parent (None at start)
    incoming = {start_key: None}       # the parent label key the route actually comes from
    edge_bearing = {start_key: None}   # bearing of the route into this label (None at start)
    # ``accumulated`` is *derived* from the label chain, never carried incrementally: a
    # Theta* rewiring replaces a label's parent, and an incrementally summed ledger would
    # then describe a route the finalized chain no longer is.  Deriving it from the chain
    # keeps ``costs`` and ``incoming`` describing exactly the same route.
    accumulated = {start_key: _empty_ledger()}
    queue = [(heuristic(start_grid), 0.0, 0, start_grid, start_bin)]
    los_records = {}
    expanded = 0
    cap_reached = False
    best_goal_key = None
    closed = set()

    while queue:
        _, _, _, current_grid, current_bin = heappop(queue)
        current_key = (current_grid, current_bin)
        current_cost = costs.get(current_key)
        if current_cost is None:
            continue
        if current_grid == target:
            # A goal label is popped and immediately closed: the remaining queue can only
            # hold labels whose cost is at least this one, so this is the optimum under the
            # label-setting rule.
            best_goal_key = current_key
            break
        if current_key in closed:
            continue
        if max_expanded_labels is not None and expanded >= max_expanded_labels:
            cap_reached = True
            break
        expanded += 1
        statistics["expanded_labels"] = expanded
        closed.add(current_key)
        current_point = graph.centers[current_grid]
        parent_key = incoming[current_key]
        grandparent_grid = (
            parents.get(parent_key) if parent_key is not None else None
        )
        path2_eligible = grandparent_grid is not None
        current_ledger = accumulated[current_key]
        # Turn cost is measured between the **discretized** headings, which is the whole
        # reason the state carries a heading bin: two labels that reach the same cell with
        # different incoming headings are genuinely different states.
        current_bin_heading = (
            None if current_key == start_key else heading_bin_center_deg(
                current_bin, heading_bin_count
            )
        )
        parent_ledger = accumulated.get(parent_key) if parent_key is not None else None
        parent_bin_heading = (
            None if parent_key is None or parent_key == start_key else heading_bin_center_deg(
                parent_key[1], heading_bin_count
            )
        )

        for neighbour in graph.neighbors(current_grid):
            if neighbour == start_grid:
                # The start label is the route's fixed origin: ``costs[start_key] == 0`` and
                # its ledger is empty.  Re-relaxing it — which the Theta* shortcut can
                # legitimately propose once an adjacent label is closed — would move the
                # origin into the middle of the route and charge the first segment a turn
                # against a heading that was never flown.
                continue
            guards = graph.diagonal_guards(current_grid, neighbour)
            if guards is not None and any(
                guard is None or gate(guard) is not None for guard in guards
            ):
                continue
            neighbour_point = graph.centers[neighbour]

            # ---- path 1: direct step, requires the parent -> neighbour LOS to be clear.
            direct = los(current_grid, neighbour, current_point, neighbour_point)
            if not direct["ok"]:
                continue
            direct_bearing = grid_bearing_deg(current_point, neighbour_point)
            direct_bin = heading_bin_for_bearing(direct_bearing, heading_bin_count)
            direct_ledger = _extend_ledger(
                current_ledger, risk_exposure=direct["risk_exposure_index_m"],
                # Geometric prefix length: the distance term must describe the chain this
                # relaxation is installing, not a detour an earlier chain accumulated.
                distance=distance_m(start_point, neighbour_point),
                previous_heading=current_bin_heading,
                new_heading=heading_bin_center_deg(direct_bin, heading_bin_count),
                weights=(weight_risk, weight_turn, weight_distance), d_ref=d_ref,
                theta_min_deg=theta_min_deg,
            )
            _relax(
                grid_id=neighbour,
                bin_index=direct_bin,
                bearing=direct_bearing,
                ledger=direct_ledger,
                risk_weight=weight_risk, turn_weight=weight_turn,
                distance_weight=weight_distance,
                parent_key=current_key,
                parent_grid=current_grid,
                heuristic=heuristic,
                costs=costs, parents=parents, incoming=incoming,
                edge_bearing=edge_bearing, accumulated=accumulated, closed=closed,
                statistics=statistics, queue=queue, los_records=los_records,
                los_result=direct,
            )

            if not path2_eligible:
                continue
            # ---- path 2: the Theta* shortcut grandparent -> neighbour.
            grandparent_point = graph.centers[grandparent_grid]
            if grandparent_grid == neighbour:
                continue
            shortcut = los(grandparent_grid, neighbour, grandparent_point, neighbour_point)
            if not shortcut["ok"]:
                continue
            grandparent_cost = costs.get(parent_key)
            if grandparent_cost is None:
                continue
            shortcut_bearing = grid_bearing_deg(grandparent_point, neighbour_point)
            shortcut_bin = heading_bin_for_bearing(shortcut_bearing, heading_bin_count)
            shortcut_ledger = _extend_ledger(
                parent_ledger, risk_exposure=shortcut["risk_exposure_index_m"],
                distance=distance_m(start_point, neighbour_point),
                previous_heading=parent_bin_heading,
                new_heading=heading_bin_center_deg(shortcut_bin, heading_bin_count),
                weights=(weight_risk, weight_turn, weight_distance), d_ref=d_ref,
                theta_min_deg=theta_min_deg,
            )
            # ``parent_key``'s own parent becomes the new label's parent: the shortcut
            # *replaces* the two-segment detour through ``current_key`` with the single
            # straight ``grandparent -> neighbour`` segment, which is exactly what Theta*
            # rewiring means.  Keeping ``current_key`` as the parent instead would leave a
            # phantom vertex at ``current_grid`` in the reconstructed route geometry.
            _relax(
                grid_id=neighbour,
                bin_index=shortcut_bin,
                bearing=shortcut_bearing,
                ledger=shortcut_ledger,
                risk_weight=weight_risk, turn_weight=weight_turn,
                distance_weight=weight_distance,
                parent_key=incoming[parent_key],
                parent_grid=grandparent_grid,
                heuristic=heuristic,
                costs=costs, parents=parents, incoming=incoming,
                edge_bearing=edge_bearing, accumulated=accumulated, closed=closed,
                statistics=statistics, queue=queue, los_records=los_records,
                los_result=shortcut, is_rewire=True,
            )

    if best_goal_key is None:
        return {
            "path": None, "grid_path": None, "los_segments": [], "los_records": {},
            "accumulated": None, "statistics": statistics, "cap_reached": cap_reached,
        }

    # Reconstruct the label chain.  ``grid_path`` is the route's *vertex* cell sequence: the
    # cells the any-angle route actually turns at.  A straight segment between two
    # consecutive vertices is a Theta* LOS shortcut, so consecutive vertices are frequently
    # non-adjacent -- that is the whole point.  ``los_records`` holds the traversal that
    # admitted each of those segments, so the reported crossed-cell lists and the risk
    # integral are the search's own, not a re-derivation.
    chain = []
    key = best_goal_key
    while key is not None:
        chain.append(key)
        key = incoming[key]
    chain.reverse()
    grid_path = [item[0] for item in chain]

    points = [list(start_point)] + [list(graph.centers[item]) for item in grid_path] + [
        list(end_point)
    ]
    route_points = []
    for point in points:
        if route_points and point == route_points[-1]:
            continue
        route_points.append(point)

    los_segments = _route_segment_audit(
        graph, index_map, grid_path, start_point, end_point, gate, altitude, regulatory,
        risk_indices, statistics,
    )

    # Authoritative ledger: recomputed from the *final* parent chain, so the reported
    # objective can never describe a route the chain no longer is.
    ledger = _empty_ledger()
    previous_heading = None
    for segment in los_segments:
        heading = segment["outgoing_heading_deg"]
        ledger = _extend_ledger(
            ledger,
            risk_exposure=segment["risk_exposure_index_m"],
            distance=segment["length_m"],
            previous_heading=previous_heading,
            new_heading=heading,
            weights=(weights["risk"], weights["turn"], weights["distance"]),
            d_ref=d_ref, theta_min_deg=theta_min_deg,
        )
        segment["incoming_heading_deg"] = previous_heading
        previous_heading = heading
    accumulated[best_goal_key] = ledger

    return {
        "path": route_points, "grid_path": grid_path,
        "label_chain": [
            {"grid_id": item[0], "incoming_heading_bin": item[1]} for item in chain
        ],
        "accumulated": ledger,
        "los_segments": los_segments,
        "los_records": los_records,
        "statistics": statistics, "cap_reached": cap_reached,
    }


def _bin_centre_heading(bearing_deg):
    """Snap a raw bearing to its heading-bin centre in the 360-degree fine binning.

    The reported audit headings are the discretized headings the turn cost was actually
    priced between, so ``turn_statistics`` and ``planning_objective`` can be reproduced from
    the reported segments alone.
    """

    if bearing_deg is None:
        return None
    return heading_bin_center_deg(heading_bin_for_bearing(bearing_deg, 360), 360)


def _route_segment_audit(
    graph, index_map, grid_path, start_point, end_point, gate, altitude, regulatory,
    risk_indices, statistics,
):
    """One entry per route segment, in order, with its crossed cells and headings.

    Each segment is re-traversed with the **same** supercover LOS routine the search used,
    so the reported crossed cells, in-cell lengths and risk integral are the authoritative
    ones for the finalized geometry.  The re-traversal is an audit: it does not move any
    search counter.
    """

    audit_statistics = {key: 0 for key in statistics if key.startswith("rejected_")}
    audit_statistics.update({"los_checks": 0, "los_shortcuts": 0})
    audit_statistics["rejected_unknown"] = 0
    segments = []
    previous_grid = None
    for position, grid_id in enumerate(grid_path):
        entry = (
            list(start_point) if position == 0 else list(graph.centers[grid_path[position - 1]])
        )
        exit_point = list(graph.centers[grid_id])
        record = line_of_sight(
            graph, index_map,
            source_id=(previous_grid or grid_id), target_id=grid_id,
            source_point=entry, target_point=exit_point, gate=gate, altitude_m=altitude,
            regulatory=regulatory, risk_indices=risk_indices, statistics=audit_statistics,
        )
        outward = grid_bearing_deg(entry, exit_point)
        segments.append({
            "from_grid_id": previous_grid or grid_id,
            "to_grid_id": grid_id,
            "from_coordinate": entry,
            "to_coordinate": exit_point,
            "length_m": _round(distance_m(entry, exit_point)),
            "traversed_cells": list((record or {}).get("cells") or []),
            "risk_exposure_index_m": _round((record or {}).get("risk_exposure_index_m")),
            "outgoing_heading_deg": _bin_centre_heading(outward),
            "supercover": True,
            "shortcut": True,
        })
        previous_grid = grid_id
    if grid_path:
        exit_point = list(graph.centers[grid_path[-1]])
        record = line_of_sight(
            graph, index_map,
            source_id=(previous_grid or grid_path[-1]),
            target_id=(graph.containing_cell(end_point) or grid_path[-1]),
            source_point=exit_point, target_point=list(end_point), gate=gate,
            altitude_m=altitude, regulatory=regulatory, risk_indices=risk_indices,
            statistics=audit_statistics,
        )
        outward = grid_bearing_deg(exit_point, end_point)
        segments.append({
            "from_grid_id": grid_path[-1],
            "to_grid_id": graph.containing_cell(end_point) or grid_path[-1],
            "from_coordinate": exit_point,
            "to_coordinate": list(end_point),
            "length_m": _round(distance_m(exit_point, end_point)),
            "traversed_cells": list((record or {}).get("cells") or []),
            "risk_exposure_index_m": _round((record or {}).get("risk_exposure_index_m")),
            "outgoing_heading_deg": _bin_centre_heading(outward),
            "supercover": True,
            "shortcut": True,
        })
    return segments


def _zero_length_ledger_segments(segments):
    """Drop the zero-length end connectors so the ledger only prices real motion."""

    return [item for item in segments if (item["length_m"] or 0.0) > 0.0 or item[
        "outgoing_heading_deg"
    ] is not None]


def _empty_ledger():
    return {
        "risk": 0.0, "turn": 0.0, "distance": 0.0, "turn_count": 0,
        "heading_change": 0.0, "turns": [],
    }


def _ledger_distance(ledger):
    return float(ledger.get("ledger_distance", ledger.get("distance", 0.0)))


def _relax(
    *, grid_id, bin_index, bearing, ledger, risk_weight, turn_weight, distance_weight,
    parent_key, parent_grid, heuristic, costs, parents, incoming, edge_bearing,
    accumulated, closed, statistics, queue, los_records, los_result, is_rewire=False,
):
    """Install (or improve) one label.

    **Label-setting with closed labels.**  A label that has already been expanded is never
    reopened.  This is what keeps the cumulative ledger exact: a Theta* shortcut can
    legitimately reach a cell under a *different* prefix than the staircase that was
    expanded first, and reopening it would leave every label already derived from the old
    prefix describing a route that no longer exists.  Freezing a closed label makes the
    prefix ledger of every descendant stay the authoritative description of the chain it
    actually hangs from.
    """

    key = (grid_id, bin_index)
    statistics["generated_labels"] += 1
    if key in closed:
        return
    cost = (
        risk_weight * ledger["risk"] + turn_weight * ledger["turn"]
        + distance_weight * ledger["distance"]
    )
    known = costs.get(key)
    if known is not None and cost >= known - 1e-9:
        return
    costs[key] = cost
    parents[key] = parent_grid
    incoming[key] = parent_key
    edge_bearing[key] = bearing
    accumulated[key] = ledger
    # The admitted traversal is recorded under the ``(from, to)`` grid pair the route
    # geometry actually uses, so the reported crossed cells are the search's own.
    los_records[(parent_grid, grid_id)] = los_result
    if is_rewire:
        statistics["rewired_parent_shortcuts"] += 1
    heappush(
        queue,
        (cost + heuristic(grid_id), cost, statistics["generated_labels"], grid_id, bin_index),
    )


def _extend_ledger(
    base, *, risk_exposure, distance, previous_heading, new_heading, weights, d_ref,
    theta_min_deg,
):
    """Extend a label's objective ledger with one straight sub-segment.

    ``previous_heading`` / ``new_heading`` are **discretized heading-bin centres**, not raw
    bearings: the label state is ``(grid_id, incoming_heading_bin)``, so the turn is priced
    between the bins the aircraft is modelled as holding.  A ``None`` previous heading means
    there is no preceding turn (the route start), which is never charged.
    """

    weight_risk, weight_turn, weight_distance = weights
    ledger = {
        "risk": float(base["risk"]) + float(risk_exposure or 0.0),
        "turn": float(base["turn"]),
        "distance": float(base["distance"]) + float(distance or 0.0),
        "turn_count": int(base["turn_count"]),
        "heading_change": float(base["heading_change"]),
        "turns": list(base["turns"]),
    }
    if previous_heading is not None and new_heading is not None:
        delta = abs(normalize_heading_delta(previous_heading, new_heading))
        if delta > float(theta_min_deg) and d_ref:
            cost = float(d_ref) * (1.0 + delta / 180.0)
            ledger["turn"] += cost
            ledger["turn_count"] += 1
            ledger["heading_change"] += delta
            ledger["turns"].append({
                "from_heading_deg": round(float(previous_heading), 9),
                "to_heading_deg": round(float(new_heading), 9),
                "heading_change_deg": round(delta, 9),
                "cost_m": _round(cost),
            })
    return ledger


def _objective_metrics(search, weights, d_ref, theta_min_deg):
    accumulated = search["accumulated"]
    risk = float(accumulated["risk"])
    turn = float(accumulated["turn"])
    distance = float(accumulated["distance"])
    terms = weighted_terms(risk, turn, distance, {"risk_weight": weights["risk"],
                                                 "turn_weight": weights["turn"],
                                                 "distance_weight": weights["distance"],
                                                 "confirmed": True})
    objective = {
        "risk_exposure_index_m": _round(risk),
        "turn_count": int(accumulated["turn_count"]),
        "total_heading_change_deg": round(float(accumulated["heading_change"]), 9),
        "turn_cost_m": _round(turn),
        "distance_m": _round(distance),
        **{key: terms[key] for key in (
            "risk_weight", "turn_weight", "distance_weight",
            "weighted_risk", "weighted_turn", "weighted_distance", "total_cost",
        )},
        "formula": OBJECTIVE_FORMULA,
        "objective_population_shelter_only": True,
    }
    turn_statistics = {
        "turn_count": int(accumulated["turn_count"]),
        "total_heading_change_deg": round(float(accumulated["heading_change"]), 9),
        "turn_cost_m": _round(turn),
        "turns": list(accumulated["turns"]),
        "theta_min_deg": theta_min_deg,
        "d_ref_m": _round(d_ref),
        "semantics": "planning_smoothness_proxy_not_flight_dynamics_validation",
    }
    return {
        "risk_exposure_index_m": risk,
        "turn_cost_m": turn,
        "distance_m": distance,
        "total_cost": terms["total_cost"],
        "objective": objective,
        "turn_statistics": turn_statistics,
    }


__all__ = [
    "ALGORITHM_ID", "ALGORITHM_VERSION", "GridIndexMap", "LOS_REJECTION_REASONS",
    "PLANNING_OBJECTIVE_PROVENANCE", "POPULATION_FACTOR_ID", "SEARCH_SEMANTICS",
    "LayeredRiskAwareThetaStarV2", "derive_d_ref_m", "grid_bearing_deg",
    "heading_bin_center_deg", "heading_bin_for_bearing", "line_of_sight",
    "normalize_heading_delta",
]
