"""Layered Risk-Aware Theta* V2 — true any-angle search on the MH/T L8 grid.

Pipeline::

    scenario/OD route -> explicit confirmed AltitudeLayer (fixed H = nominal_altitude_m)
      -> coarse terrain/building feasibility mask (existing LayerFeasibilityMask semantics)
      -> population x shelter risk field (per-grid, replaceable)
      -> virtual endpoint anchoring (exact OD -> containing cell + 1-ring LOS anchors)
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
"arrived heading east" and price the turn.  The bin is a **state key only**: the turn angle
itself is always computed from the actual bearings of the two LOS segments the route flies
(BUG-TURN-STAT-001).  A heading-aware A* (adjacent-cell moves only) is explicitly *not* what
this planner does.

The exact OD endpoints are not snapped onto a cell centre: each endpoint contributes a small
set of candidate anchors (its containing cell plus the 1-ring) and each candidate is linked by
a straight LOS connector that goes through the same supercover LOS and hard gate as every
search segment.  The existing objective picks the entry/exit geometry (BUG-ROUTE-006).

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

from ..domain.geodesy import distance_m
from ..domain.building_clearance import building_roof_elevation, evaluate_vertical_clearance
from ..domain.communication_planning_field import communication_readiness
from ..domain.layered_route import (
    COARSE_ENVELOPE_SEMANTICS, COST_DOMAIN_IDS, PLANNING_TERMINAL_SEMANTICS,
    PLANNING_TERMINAL_STATUSES, annotate_terminal_status, candidate_fingerprint,
    default_layered_route_candidate, feasibility_policy_fingerprint,
    mask_fingerprint, planning_status_semantics, request_fingerprint, stable_fingerprint,
    terminal_reason_code,
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
from ..domain.planning_constraint_field import (
    CONTAINS_UNKNOWN_CONSTRAINTS_REASON, PROVISIONAL_ROUTE_BLOCK_STATEMENT,
    normalize_unknown_policy,
)
from ..domain.regulatory_constraints import (
    evaluate_regulatory_intersection, is_configured as regulatory_is_configured,
    regulatory_compliance_record, regulatory_constraints_fingerprint,
)
from ..risk.accessors_v2 import cell_factor_index
from ..planning.grid_graph import GridGraph
from .supercover import (
    CONSERVATIVE_BOUNDARY_TOUCH, CORNER_TOUCH_POLICY, LENGTH_ASSIGNMENT_SEMANTICS,
    supercover_traversal, traversal_cells_with_lengths,
)

#: Risk Framework V2 factor used as the normalized population factor.
POPULATION_FACTOR_ID = "population_exposure"

#: Endpoint virtual-anchoring aperture (BUG-ROUTE-006).
#: 候选锚点 = 精确端点所在格 + 1-ring；只有"明显位于起飞/进入方向前方"（与精确端点指向
#: 另一端的方位夹角不超过该值）的锚点才进入候选集，避免候选里包含需要主动反向的几何。
ENDPOINT_ANCHOR_APERTURE_DEG = 90.0

#: Endpoint virtual-anchoring semantics recorded on every candidate (BUG-ROUTE-006).
ENDPOINT_ANCHOR_SEMANTICS = {
    "definition": "exact_od_virtual_endpoint_anchoring",
    "anchor_candidates": "containing_cell_plus_one_ring",
    "candidate_link": "straight_los_connector_checked_by_the_same_supercover_and_hard_gate",
    "selection": "best_complete_exact_od_objective_over_all_anchor_combinations",
    "fixed_cell_centre_connector_removed": True,
    "connector_distance_and_risk_are_priced_in_the_full_od_objective": True,
    "connector_turn_is_not_charged_to_cruise_turn_cost": True,
    "connector_turn_semantics": "departure_climb_and_arrival_descent_belong_to_the_transition",
    "first_path_point_is_the_exact_start": True,
    "last_path_point_is_the_exact_end": True,
    "no_fabricated_straight_segment": True,
    "aperture_deg": ENDPOINT_ANCHOR_APERTURE_DEG,
}

#: Turn-accounting semantics (BUG-TURN-STAT-001).
TURN_ACCOUNTING_SEMANTICS = {
    "definition": "turn_angles_use_actual_los_segment_bearings",
    "heading_bin_used_as_the_turn_angle": False,
    "heading_bin_role": "state_partition_and_search_size_control_only",
    "turn_delta_source": "actual_los_segment_bearing_previous_and_current",
    "turn_state_input": "real_incoming_bearing_stored_per_label",
    "start_connector_is_the_first_reference_bearing_when_it_has_length": True,
    "endpoint_connector_turn_is_not_charged": True,
    "recomputable_from_candidate_path": True,
    "theta_min_deg_unchanged": True,
}

#: 能力声明（Phase 3.5，**只声明**，不改变搜索行为）。
#: Theta* V2 在"当前工作区网格层级"的水平面上搜索；建筑事实来自 L8 building_grid，
#: 因此 legacy / diagnostic 的 L7/L6 工作区（正式入口已不再产生）拿不到 L8 建筑事实
#: （见 docs/10-Phase3已知限制与待办.md §1）；正式工作区恒为 L8，建筑约束始终参与。
PLANNER_CAPABILITY = {
    "algorithm_id": ALGORITHM_ID,
    "algorithm_version": ALGORITHM_VERSION,
    "horizontal_grid_levels": [8],
    "available_levels": [8],
    "preferred_level": 8,
    "level_binding": "current_workspace_grid_level_used_as_is",
    "building_fact_level": 8,
    "cross_level_aggregation_allowed": False,
    "notes": [
        "搜索发生在工作区当前网格层级的水平面上，算法本身不做层级转换。",
        "建筑垂向包线只消费 L8 building_grid 事实；正式入口的工作区恒为 L8，"
        "因此建筑约束在正式规划中始终参与。",
        "legacy / diagnostic 的非 L8 工作区（已不由正式入口产生）仍会因层级不匹配而拿不到"
        "L8 建筑事实，此时算法绝不静默降级、绝不把 unknown 当 0。",
        "层级无关的建筑事实获取是已记录的后续需求，本轮不实现，也不静默降级。",
    ],
    "future_work": "level_independent_building_fact_acquisition",
}

#: Traversal rejection vocabulary.
LOS_REJECTION_REASONS = (
    "terrain", "building", "tower", "airspace", "critical_site", "regulatory",
    "unknown", "hard_constraint", "outside_grid",
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
    "heuristic": (
        "(distance_weight + risk_weight * min_risk_index) * "
        "max(0, straight_line_distance_to_target_centre - terminal_stub_length_m)"
    ),
    # BUG-TURN-STAT-001：状态仍按 heading bin 离散（搜索规模控制），但 turn delta /
    # turn_count / total_heading_change_deg / turn_cost 一律使用**实际 LOS segment
    # bearing**，绝不用 bin center 代替真实方位。
    "turn_is_priced_between_actual_los_segment_bearings": True,
    "heading_bin_used_for": "state_partition_and_search_size_control_only",
    "heading_bin_never_used_as_the_turn_angle": True,
    # BUG-ROUTE-006：精确 OD 端点通过虚拟锚定进入搜索。
    "endpoint_anchoring": "exact_od_virtual_endpoint_anchoring",
    "endpoint_anchor_candidates": "containing_cell_plus_one_ring_los_candidates",
    "endpoint_anchor_selection": "existing_objective_over_all_anchor_combinations",
    "exact_od_terminal_segments_are_priced": True,
    "start_connector_is_a_ledger_segment": "exact_start_to_selected_source_anchor",
    "end_connector_is_a_ledger_segment": "selected_target_anchor_to_exact_end",
    "endpoint_connector_turn_is_excluded_from_cruise_turn_cost": True,
    "goal_acceptance": (
        "every_target_heading_label_is_priced_with_the_complete_exact_od_ledger_and_the_"
        "search_ends_only_when_the_queue_lower_bound_cannot_improve_the_best_complete_goal"
    ),
    "edge_length_semantics": "real_los_segment_length_m_never_a_re_added_origin_to_neighbour_distance",
    "risk_v2_overall_not_used": True,
    "search_objective_risk_scope": "population_x_shelter_only",
    "communication_affects_path_or_cost": False,
    "airspace_not_used": True,
    "airspace_hard_exclusion_from_constraint_field": True,
    # Terminal-status semantics (Phase 3.5).  Reaching the expansion cap stops the
    # search before reachability is decided, so it is reported as ``search_incomplete``
    # and is never evidence that the airspace is infeasible.
    "terminal_statuses": list(PLANNING_TERMINAL_STATUSES),
    "terminal_status_semantics": dict(PLANNING_TERMINAL_SEMANTICS),
    "expansion_cap_status": "search_incomplete",
    "search_budget_exhausted_is_not_infeasibility": True,
    "no_path_requires_a_completed_exhaustive_search": True,
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


def _inside_aperture(bearing_deg, forward_deg, aperture_deg):
    """Whether ``bearing_deg`` is within ``aperture_deg`` of the forward heading.

    Used **only** to bound the endpoint-anchor candidate set: an anchor behind the
    departure heading (or behind the arrival heading) would add a deliberate reversal to the
    route, which is never an entry/exit geometry worth proposing.  ``None`` means the
    aperture cannot be evaluated, in which case the candidate is kept.
    """

    if bearing_deg is None or forward_deg is None:
        return True
    return abs(normalize_heading_delta(forward_deg, bearing_deg)) <= float(aperture_deg) + 1e-9


def _opposite_bearing(bearing_deg):
    return None if bearing_deg is None else round((float(bearing_deg) + 180.0) % 360.0, 9)


def _endpoint_anchor_candidates(
    *, graph, point, other_point, aperture_deg, which,
):
    """Virtual endpoint anchors: the cell the exact endpoint sits in plus its 1-ring.

    BUG-ROUTE-006 的最小收口：精确 OD 端点不再被强制吸附到"所在格的格心"。候选锚点集
    只包含

    * 精确端点所在格（``containing_cell``，若它在当前网格内），以及
    * 该格的 1-ring 邻居（同样必须属于当前网格）。

    每一个候选锚点都会在搜索里生成一条 ``精确端点 -> 锚点格心`` 的 LOS connector；该
    connector 与普通搜索段走**同一个** ``line_of_sight``（supercover + 硬约束 gate +
    regulatory + risk 积分），因此不存在"只做端点检测"的捷径。最终由既有 objective 在
    所有候选组合里选优，而不是固定经过所属格中心。

    ``aperture_deg`` 只用于把明显反向的锚点排除出候选项（见 :func:`_inside_aperture`），
    它不改变任何已入选 connector 的成本计算。候选的 ``link_bearing_deg`` 是 **connector
    实际被飞行的方向**：source 锚点是 ``exact start -> anchor``，target 锚点是
    ``anchor -> exact end``（即回到精确终点的方向），两端的入/出航向因此都能与
    connector 几何一致。
    """

    anchor_grid = graph.containing_cell(point)
    if anchor_grid is None or anchor_grid not in graph.cells:
        return []
    forward = grid_bearing_deg(point, other_point)
    ordered = [anchor_grid] + [
        neighbour for neighbour in graph.neighbors(anchor_grid)
        if neighbour != anchor_grid
    ]
    candidates = []
    for grid_id in ordered:
        center = [float(value) for value in graph.centers[grid_id]]
        if which == "source":
            link_bearing = grid_bearing_deg(point, center)
        else:
            link_bearing = grid_bearing_deg(center, point)
        if not _inside_aperture(link_bearing, forward, aperture_deg):
            continue
        candidates.append({
            "grid_id": grid_id,
            "point": center,
            "link_bearing_deg": link_bearing,
            "length_m": distance_m(point, center),
            "which": which,
            "anchor_grid_id": anchor_grid,
        })
    return candidates


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
    regulatory, risk_indices, statistics, counted=True,
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

    ``counted=False`` marks an audit re-traversal: it performs exactly the same checks and
    returns exactly the same geometry, but it does not move the search's LOS counters (the
    counters describe the search, not the audit).
    """

    if counted:
        statistics["los_checks"] += 1
    start_index = index_map.index_of(source_id)
    end_index = index_map.index_of(target_id)
    if start_index is None or end_index is None:
        if counted:
            statistics["rejected_unknown"] += 1
        return _los_rejection("outside_grid", source_id, "cell_index_unavailable")

    traversed = supercover_traversal(start_index, end_index)
    segment_length = distance_m(source_point, target_point)
    cells = traversal_cells_with_lengths(traversed, segment_length)
    resolved = []
    for entry in cells:
        grid_id = index_map.grid_id_at(entry["cell_index"])
        if grid_id is None:
            if counted:
                statistics["rejected_unknown"] += 1
            return _los_rejection("outside_grid", None, "traversed_cell_not_in_grid")
        entry["grid_id"] = grid_id
        resolved.append(entry)
        rejection = gate(grid_id)
        if rejection is not None:
            if counted:
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
            if counted:
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
            if counted:
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
        if counted:
            statistics["rejected_unknown"] += 1
        return _los_rejection("unknown", None, "risk_evidence_unresolved_inside_shortcut")

    if counted:
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
        constraint_field=None, unknown_constraint_policy=None,
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
        unknown_policy = normalize_unknown_policy(
            unknown_constraint_policy
            if unknown_constraint_policy is not None
            else (constraint_field or {}).get("unknown_policy")
        )
        fingerprints = self.fingerprints(
            request=request, scenario_route=scenario_route, grid=grid, layer_mask=mask,
            grid_risk_v2=grid_risk_v2, objective_policy=objective,
            risk_density_constraint=constraint, population_shelter=population_shelter,
            shelter_policy=shelter_policy, regulatory_constraints=regulatory_constraints,
            hard_constraints=hard_constraints,
            building_clearance_policy=building_clearance_policy,
            feasibility_policy=feasibility_policy, source_audits=source_audits,
            constraint_field=constraint_field,
            unknown_constraint_policy=unknown_policy,
        )
        communication = communication_readiness(communication_field)

        def blocked(status, reason, code, **extra):
            """Structured non-path result.

            ``status`` is one of the terminal planning statuses — ``invalid_input`` (the
            request itself cannot be interpreted), ``not_ready`` / ``missing_data``
            (a precondition is not satisfied yet) or ``blocked`` (a fully evaluated hard
            constraint forbids the endpoints).  "No path found" is reported separately as
            ``no_path`` / ``search_incomplete`` by :meth:`plan`.
            """

            return annotate_terminal_status(self._result(
                status=status, request=request, fingerprints=fingerprints, reason=reason,
                blocking_reasons=[{
                    "reason_code": terminal_reason_code(status, code),
                    "reason": reason,
                    "terminal_status": str(status),
                    "terminal_status_semantics": planning_status_semantics(status),
                }],
                objective_policy=objective, risk_density_constraint=constraint,
                communication=communication, **extra,
            ), status)

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
            return blocked(
                "invalid_input", "当前 MH/T 标准网格不可用（cells 为空）", "grid_unavailable",
            )
        if str(mask.get("status") or "") != "passed" or not mask.get("cells"):
            return blocked(
                "invalid_input", "LayerFeasibilityMask 缺失或不可用",
                "feasibility_mask_unavailable",
            )
        if isinstance(constraint_field, dict) and (
            str(constraint_field.get("altitude_layer_id") or "")
            != str(request.get("altitude_layer_id") or "")
        ):
            return blocked(
                "invalid_input",
                "Planning Constraint Field 与所选 AltitudeLayer 不一致",
                "constraint_field_altitude_layer_mismatch",
            )

        route = scenario_route if isinstance(scenario_route, dict) else {}
        route_id = str(route.get("route_id") or "")
        start, end = route.get("start"), route.get("end")
        if not route_id or not _point(start) or not _point(end):
            return blocked(
                "invalid_input", "scenario/OD 航路端点或 route_id 缺失",
                "scenario_route_endpoints_missing",
            )

        graph = GridGraph(list(grid["cells"]))
        index_map = GridIndexMap(graph)
        source, target = graph.containing_cell(start), graph.containing_cell(end)
        if source is None or target is None:
            return blocked(
                "invalid_input", "起点或终点不在当前标准网格内", "endpoints_outside_grid",
            )
        # ---- 虚拟端点锚定（BUG-ROUTE-006）：精确 OD 不再被强制吸附到所属格格心。
        # 端点所在格 + 其 1-ring 可通行候选格各自生成一条 ``精确端点 -> 锚点格心`` 的 LOS
        # connector，最终由既有 objective 在候选组合里选优（见 ``_theta_star``）。
        endpoints = {
            "start_point": [float(value) for value in start],
            "end_point": [float(value) for value in end],
            "aperture_deg": ENDPOINT_ANCHOR_APERTURE_DEG,
            "source_anchors": _endpoint_anchor_candidates(
                graph=graph, point=list(start), other_point=list(end),
                aperture_deg=ENDPOINT_ANCHOR_APERTURE_DEG, which="source",
            ),
            "target_anchors": _endpoint_anchor_candidates(
                graph=graph, point=list(end), other_point=list(start),
                aperture_deg=ENDPOINT_ANCHOR_APERTURE_DEG, which="target",
            ),
        }

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
            constraint_field=constraint_field, unknown_policy=unknown_policy,
        )
        statistics = self._statistics(
            None, d_ref=d_ref, risk_weight=risk_weight, turn_weight=turn_weight,
            distance_weight=float(weights["distance"]),
        )
        search = _theta_star(
            graph=graph, index_map=index_map, endpoints=endpoints, gate=gate,
            altitude=altitude, regulatory=regulatory_constraints,
            risk_indices=risk_indices, weights=weights, d_ref=d_ref,
            heading_bin_count=self.parameters["heading_bin_count"],
            theta_min_deg=self.parameters["theta_min_deg"],
            max_expanded_labels=self.parameters["max_expanded_labels"],
        )
        statistics.update(search["statistics"])
        statistics["endpoint_anchors"] = search.get("endpoint_anchors")
        statistics["endpoint_anchor_policy"] = ENDPOINT_ANCHOR_SEMANTICS
        statistics["d_ref_m"] = _round(d_ref)
        statistics["risk_unresolved_cell_count"] = len(risk_unresolved)
        # BUG-ROUTE-004 evidence: the ledger the search actually minimised and the ledger the
        # exact-OD audit recomputed must describe one and the same route.
        statistics["search_termination"] = search.get("termination")
        statistics["goal_candidates"] = list(search.get("goal_candidates") or [])
        statistics["goal_ledger_consistency"] = _goal_ledger_consistency(search, weights)

        if search["path"] is None:
            # A stopped-because-budget-exhausted search proves nothing about reachability, so
            # it is reported as ``search_incomplete``; only a search that ran to exhaustion
            # may report ``no_path``.  Neither is ever a claim that the airspace is unusable.
            incomplete = bool(search["cap_reached"])
            statistics["search_completeness"] = (
                "expansion_cap_reached_optimality_not_proven"
                if incomplete else "search_exhausted_no_traversable_path"
            )
            return annotate_terminal_status(self._result(
                status="search_incomplete" if incomplete else "no_path",
                request=request, fingerprints=fingerprints,
                reason=(
                    "Theta* 搜索预算耗尽（达到 max_expanded_labels）：可达性未被证明，"
                    "这不是空域不可行，也不代表没有航路；请提高搜索预算或缩小问题规模后重跑"
                    if incomplete else
                    "Theta* 完整搜索结束但未找到 any-angle 可用路径"
                    "（地形/建筑/硬约束/regulatory/风险证据任一阻断）"
                ),
                blocking_reasons=[{
                    "reason_code": (
                        "search_budget_exhausted" if incomplete else "no_traversable_path"
                    ),
                    "reason": (
                        "达到 max_expanded_labels：搜索预算耗尽，可达性未被证明"
                        if incomplete else "搜索完整结束且不存在可行路径"
                    ),
                    "resource_limit": "max_expanded_labels" if incomplete else None,
                    "reachability_proven": not incomplete,
                    "optimality_proven": False,
                }],
                objective_policy=objective, risk_density_constraint=constraint,
                communication=communication,
                mask=mask, statistics=statistics, search_incomplete=incomplete,
                mask_status=mask.get("status"),
            ), "search_incomplete" if incomplete else "no_path")

        # The reported objective is derived from the search's authoritative exact-OD ledger,
        # which ``_theta_star`` recomputes from the finalized geometry and connector
        # classification *after* the goal is chosen.  Deriving it from a stale search-phase
        # ledger would make ``turn_statistics`` disagree with ``candidate.path``
        # (BUG-TURN-STAT-001).
        metrics = _objective_metrics(search, weights, d_ref, self.parameters["theta_min_deg"])
        crossed = {
            entry.get("grid_id")
            for segment in search.get("los_segments") or []
            for entry in segment.get("traversed_cells") or []
            if entry.get("grid_id")
        }
        unknown_crossed = sorted(crossed & gate_diagnostics["constraint_unknown"])
        statistics["unknown_constraint_count"] = len(unknown_crossed)
        # 逐 cell 的**穿越证据不足单元数**：与 candidate 上的字段同名同义，
        # RouteRiskProfile / validation 直接读它，不必再去数 los_segments。
        statistics["traversed_unknown_cell_count"] = len(unknown_crossed)
        statistics["unknown_constraint_grid_ids"] = unknown_crossed
        evaluation = evaluate_route_risk_density(
            risk_exposure_index_m=metrics["risk_exposure_index_m"],
            distance_m=metrics["distance_m"], constraint=constraint,
        )
        straight = distance_m(start, end)
        statistics["search_completeness"] = (
            "expansion_cap_reached_optimality_not_proven"
            if search["cap_reached"] else "optimal_path_found"
        )
        return annotate_terminal_status(self._result(
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
            constraint_field=constraint_field, unknown_policy=unknown_policy,
            unknown_constraint_count=len(unknown_crossed),
            traversed_unknown_cell_count=len(unknown_crossed),
            contains_unknown_constraints=bool(unknown_crossed),
        ), "candidate")

    # ------------------------------------------------------------------ fingerprints

    def fingerprints(
        self, *, request, scenario_route, grid, layer_mask, grid_risk_v2, objective_policy,
        risk_density_constraint, population_shelter=None, shelter_policy=None,
        regulatory_constraints=None, hard_constraints=None,
        building_clearance_policy=None, feasibility_policy=None, source_audits=None,
        cost_policy=None, communication_field=None,
        constraint_field=None, unknown_constraint_policy=None,
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
            "selected_altitude_layer_identity": {
                "altitude_layer_id": (request or {}).get("altitude_layer_id"),
                "nominal_altitude_m": ((layer_mask or {}).get("cruise_altitude") or {}).get(
                    "altitude_egm2008_m"
                ),
                "vertical_reference": ((layer_mask or {}).get("cruise_altitude") or {}).get(
                    "vertical_reference", "egm2008_orthometric"
                ),
            },
            "planning_constraint_field_fingerprint": (
                (constraint_field or {}).get("constraint_field_fingerprint")
            ),
            "unknown_constraint_policy": normalize_unknown_policy(
                unknown_constraint_policy
                if unknown_constraint_policy is not None
                else (constraint_field or {}).get("unknown_policy")
            ),
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
            "los_shortcuts": 0, "rejected_terrain": 0,
"rejected_building": 0,
"rejected_tower": 0,
"rejected_airspace": 0,
"rejected_critical_site": 0,
"rejected_regulatory": 0,
"rejected_unknown": 0,
"rejected_hard_constraint": 0,
"rejected_outside_grid": 0,
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
        constraint_field=None, unknown_policy=None, unknown_constraint_count=0,
        traversed_unknown_cell_count=None, contains_unknown_constraints=None,
    ):
        objective_policy = objective_policy or default_theta_v2_objective_policy()
        risk_density_constraint = risk_density_constraint or default_risk_density_constraint()
        candidate = default_layered_route_candidate(
            (request or {}).get("scenario_route_id") or _od_label(request),
            (request or {}).get("altitude_layer_id"), status,
        )
        weights = objective_weights(objective_policy)
        # 证据不足穿越的**唯一**读数：优先用显式的 traversed 计数，回退到既有
        # unknown_constraint_count（两者语义一致，都是"实际穿越的 unknown 格数"）。
        traversed_unknown = int(
            traversed_unknown_cell_count
            if traversed_unknown_cell_count is not None else (unknown_constraint_count or 0)
        )
        contains_unknown = (
            bool(contains_unknown_constraints) if contains_unknown_constraints is not None
            else traversed_unknown > 0
        )
        unknown_constraint_count = traversed_unknown
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
            "heuristic": "admissible_lower_bound_on_the_complete_exact_od_objective",
            "risk_v2_overall_used": False,
            "risk_density_added_to_objective": False,
        }
        # ---- 证据不足穿越的显式 provenance（provisional only） ----------------------
        # unknown 只影响 **feasibility**：它既不参与风险成本，也不改变 objective 权重，
        # 更不会被当成 0 风险。这里只把"这条候选穿越了几个证据不足单元"如实写进
        # candidate 的 warnings / blocking_reasons，供 RouteRiskProfile 与 validation 读取。
        provisional_statement = PROVISIONAL_ROUTE_BLOCK_STATEMENT.format(
            count=traversed_unknown
        )
        unknown_warnings = [{
            "reason": "route_traverses_unknown_constraints",
            CONTAINS_UNKNOWN_CONSTRAINTS_REASON: True,
            "traversed_unknown_cell_count": traversed_unknown,
            "unknown_constraint_count": traversed_unknown,
            "operational_applicability": "provisional_only",
            "operational_adoption_allowed": False,
            "statement": provisional_statement,
        }] if contains_unknown else []
        unknown_blockers = [{
            "reason_code": CONTAINS_UNKNOWN_CONSTRAINTS_REASON,
            "reason": provisional_statement,
            CONTAINS_UNKNOWN_CONSTRAINTS_REASON: True,
            "traversed_unknown_cell_count": traversed_unknown,
            "unknown_constraint_count": traversed_unknown,
            "status": "candidate",
            "operational_applicability": "provisional_only",
            "operational_adoption_allowed": False,
        }] if contains_unknown else []
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
            "blocking_reasons": list(blocking_reasons or []) + unknown_blockers,
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
            "planning_constraint_field_fingerprint": fingerprints["components"].get(
                "planning_constraint_field_fingerprint"
            ),
            "unknown_constraint_policy": deepcopy(
                fingerprints["components"].get("unknown_constraint_policy")
            ),
            "unknown_constraint_count": traversed_unknown,
            # 逐 cell 的穿越计数（与 unknown_constraint_count 同义，名字与需求逐字一致）；
            # contains_unknown_constraints 是 RouteRiskProfile / validation / adoption 的
            # 统一判定入口。
            "traversed_unknown_cell_count": traversed_unknown,
            "contains_unknown_constraints": contains_unknown,
            "operational_applicability": "provisional_only" if contains_unknown else "current",
            "operational_adoption_allowed": not contains_unknown,
            "maturity": "provisional",
            "completion_status": (
                "completed_with_warnings" if contains_unknown else "completed"
            ),
            "warnings": unknown_warnings,
            "search_incomplete": bool(search_incomplete),
            "mask_status": mask_status,
            "provenance": {
                "pipeline": (
                    "scenario_or_od_route -> explicit_fixed_altitude_H -> terrain_building_"
                    "feasibility_mask -> population_x_shelter_risk -> virtual_endpoint_"
                    "anchoring -> heading_aware_multi_label_theta_star_with_parent_los_"
                    "rewiring -> candidate_evaluation -> layered_route_candidate"
                ),
                "fingerprint_components": fingerprints["components"],
                "search_semantics": SEARCH_SEMANTICS,
                "search_parameters": search_view,
                "endpoint_anchoring": deepcopy(ENDPOINT_ANCHOR_SEMANTICS),
                "endpoint_anchors": deepcopy(
                    (statistics or {}).get("endpoint_anchors")
                ),
                "turn_accounting": deepcopy(TURN_ACCOUNTING_SEMANTICS),
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
                "planning_constraint_field": {
                    "fingerprint": fingerprints["components"].get(
                        "planning_constraint_field_fingerprint"
                    ),
                    "used_as_feasibility": bool(
                        fingerprints["components"].get("planning_constraint_field_fingerprint")
                    ),
                    "used_as_risk_cost": False,
                    "airspace_and_protected_sites_are_hard_only_when_confirmed": True,
                    # 证据不足只影响 feasibility：unknown 永远不会被当作 0 风险，也不会
                    # 改变 population × shelter 风险数学或 objective 权重。
                    "unknown_policy": normalize_unknown_policy(unknown_policy),
                    "traversed_unknown_cell_count": traversed_unknown,
                    "contains_unknown_constraints": contains_unknown,
                    "operational_applicability": (
                        "provisional_only" if contains_unknown else "current"
                    ),
                    "unknown_affects_feasibility_only": True,
                    "unknown_is_never_zero_risk": True,
                    "objective_weights_unchanged_by_unknown": True,
                    "provisional_only_never_publishable": True,
                },
                # The legacy display-only airspace product remains outside the planner.
                # Confirmed hard exclusions enter only through the separately fingerprinted
                # Planning Constraint Field above.
                "airspace": {
                    "status": "not_applicable", "applicability": "display_only",
                    "used_in_search": False, "used_in_fingerprint": False,
                    "semantics": "legacy_display_airspace_not_a_planner_input",
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
        "cruise_turn_count": 0, "cruise_total_heading_change_deg": 0.0,
        "cruise_turn_cost_m": 0.0, "connector_turn_count": 0,
        "connector_total_heading_change_deg": 0.0, "connector_turn_cost_m": 0.0,
        "cruise_turn_semantics": (
            "endpoint_connector_turn_is_reported_separately_and_never_charged_to_"
            "cruise_turn_cost"
        ),
        "bearing_semantics": TURN_ACCOUNTING_SEMANTICS["definition"],
        "heading_bin_used_as_the_turn_angle": False,
        "semantics": "planning_smoothness_proxy_not_flight_dynamics_validation",
    }


# --------------------------------------------------------------------------- hard gate


def _build_gate(
    *, graph, index_map, mask, hard_constraints, regulatory, altitude,
    constraint_field=None, unknown_policy=None,
):
    """Per-cell hard gate derived from the **existing** feasibility mask semantics.

    ``mask`` is the existing ``LayerFeasibilityMask``: its cells already encode

    * ``H >= terrain_max_egm2008 + explicit terrain clearance`` (terrain floor), and
    * the coarse strategic building floor
      ``terrain_max + height_max + existing confirmed building vertical clearance``.

    A building cell is therefore **not** blocked merely because a building exists: it is
    blocked only when ``H`` is below the building floor, and it is traversable over the
    obstacle when ``H`` is at or above it.  ``building_count == 0`` carries no vertical
    building constraint at all, and an unresolved height/terrain/fact is ``unknown`` —
    which the legacy LOS traversal treats as non-crossable when no explicit Planning
    Constraint Field is supplied.  With a field, that field is the feasibility authority
    and its explicit unknown policy controls provisional traversal.
    """

    mask_cells = mask.get("cells") or {}
    hard_blocked = {
        grid_id for grid_id, cell in graph.cells.items()
        if any(
            _positive_bbox_intersection(cell["bbox"], _constraint_bbox(item))
            for item in hard_constraints or []
        )
    }
    field_cells = {
        str(item.get("grid_id")): item
        for item in (constraint_field or {}).get("cells") or []
        if isinstance(item, dict) and item.get("grid_id")
    }
    explicit_field = isinstance(constraint_field, dict) and bool(field_cells)
    allow_unknown = normalize_unknown_policy(unknown_policy)["allow_unknown_for_provisional"]
    diagnostics = {
        "hard_blocked": hard_blocked, "gate_reasons": {}, "gate_domains": {},
        "constraint_blocked": set(), "constraint_unknown": set(),
        "allow_unknown_for_provisional": allow_unknown,
    }

    def classify(grid_id):
        if grid_id in hard_blocked:
            return "hard_constraint", "hard_constraint_intersection"
        constraint = field_cells.get(grid_id)
        if explicit_field and not isinstance(constraint, dict):
            diagnostics["constraint_unknown"].add(grid_id)
            if not allow_unknown:
                return "unknown", "cell_outside_planning_constraint_field"
        elif isinstance(constraint, dict):
            outcome = str(constraint.get("outcome") or "unknown")
            if outcome == "blocked":
                blockers = [
                    item for item in constraint.get("blocked_by") or []
                    if item in ("terrain", "building", "tower", "airspace", "critical_site")
                ]
                domain = blockers[0] if blockers else "hard_constraint"
                diagnostics["constraint_blocked"].add(grid_id)
                return domain, "planning_constraint_field_blocked"
            if outcome == "unknown":
                diagnostics["constraint_unknown"].add(grid_id)
                if not allow_unknown:
                    return "unknown", "planning_constraint_field_unknown"
            elif outcome != "pass":
                diagnostics["constraint_unknown"].add(grid_id)
                if not allow_unknown:
                    return "unknown", "planning_constraint_field_outcome_invalid"
            # An explicit field consumes the same obstacle facts and policies and is the
            # production feasibility authority.  Do not apply the legacy mask a second time:
            # doing so would silently turn an allowed provisional unknown back into blocked.
            return None, None
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
            # 真实铁塔塔高是**障碍物/净空**约束，不是风险项，也不进入 objective。
            if reason_code == "altitude_below_tower_clearance_floor":
                return "tower", reason_code
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
    *, graph, index_map, endpoints, gate, altitude, regulatory,
    risk_indices, weights, d_ref, heading_bin_count, theta_min_deg, max_expanded_labels,
):
    """Heading-aware multi-label Theta* with parent LOS rewiring and virtual OD endpoints.

    Labels are ``(grid_id, incoming_heading_bin)``.  For every generated label two routes
    are considered, exactly as in Theta*:

    * **path 1** — a direct step from the popped label to an adjacent cell, which requires
      the ``parent -> neighbour`` supercover LOS to be clear; the new label's parent is the
      popped label (this is what preserves the parent chain);
    * **path 2** — the *shortcut*: the popped label's own parent's parent (the
      grandparent) is path-1-reachable from the neighbour, so ``neighbour`` is rewired to
      the grandparent and the straight ``grandparent -> neighbour`` segment is taken.

    The heuristic is ``(distance_weight + risk_weight * min_risk_index) *
    max(0, straight_line_distance_to_exact_end - max_terminal_stub_length)``: every
    remaining metre costs at least ``distance_weight`` plus ``risk_weight`` times the
    smallest risk index on the grid, and a turn is never negative.  It is therefore an
    admissible lower bound on the *complete exact-OD objective*, which is what the
    termination rule below needs.

    **Turn accounting (BUG-TURN-STAT-001).**  A label's state is still discretized by
    ``heading_bin_count`` — that is what keeps the search finite — but the turn is priced
    between the **actual LOS segment bearings** the route flies, never between heading-bin
    centres.  Each label therefore stores the real bearing of its own incoming segment
    (``real_bearing``); the bin is only a state key.  The start connector
    (``exact start -> selected source anchor``) is a real flown segment and is the reference
    for the first search segment when it has a positive length.

    **Virtual endpoint anchoring (BUG-ROUTE-006).**  The exact OD endpoints are no longer
    snapped onto the centre of the cell they fall in.  ``endpoints["source_anchors"]`` /
    ``endpoints["target_anchors"]`` hold the candidate anchors (containing cell + its
    1-ring), and every candidate contributes a straight ``exact endpoint <-> anchor centre``
    connector that is gated, risk-integrated and measured by the **same** ``line_of_sight``
    the search uses.  All combinations take part in one search, so the existing objective
    selects the entry/exit geometry.  Connector distance and risk are fully priced in the
    OD objective; connector turn is configured but **not charged to the cruise turn cost**,
    because departure climb and arrival descent belong to the transition semantics that
    follow this planner (``SEARCH_SEMANTICS["endpoint_connector_turn_is_excluded_..."]``).
    """

    weight_distance = float(weights["distance"])
    weight_risk = float(weights["risk"])
    weight_turn = float(weights["turn"])
    statistics = {
        "expanded_labels": 0, "generated_labels": 0, "los_checks": 0, "los_shortcuts": 0,
        "rejected_terrain": 0,
"rejected_building": 0,
"rejected_tower": 0,
"rejected_airspace": 0,
"rejected_critical_site": 0,
"rejected_regulatory": 0,
"rejected_unknown": 0,
"rejected_hard_constraint": 0,
"rejected_outside_grid": 0,
        "rewired_parent_shortcuts": 0,
    }

    def los(from_id, to_id, from_point, to_point, counted=True):
        return line_of_sight(
            graph, index_map, source_id=from_id, target_id=to_id,
            source_point=from_point, target_point=to_point, gate=gate,
            altitude_m=altitude, regulatory=regulatory, risk_indices=risk_indices,
            statistics=statistics, counted=counted,
        )

    start_point = [float(value) for value in endpoints["start_point"]]
    end_point = [float(value) for value in endpoints["end_point"]]

    # ------------------------------------------------------------------ endpoint anchors
    source_links = []
    for candidate in endpoints.get("source_anchors") or []:
        record = los(
            candidate["grid_id"], candidate["grid_id"], start_point, candidate["point"],
        )
        if not record["ok"]:
            continue
        source_links.append({
            **candidate,
            "record": record,
            "length_m": float(record.get("segment_length_m") or 0.0),
            "bearing_deg": candidate.get("link_bearing_deg"),
        })
    if not source_links:
        # No gated/free departure connector exists at all: the exact origin cannot leave.
        return _unsolved_search(
            statistics, cap_reached=False, termination="no_traversable_source_anchor",
            los_records={}, endpoint_anchors={
                "source_anchor_count": len(endpoints.get("source_anchors") or []),
                "admitted_source_anchor_count": 0,
                "target_anchor_count": len(endpoints.get("target_anchors") or []),
                "admitted_target_anchor_count": None,
            },
        )
    target_links = []
    for candidate in endpoints.get("target_anchors") or []:
        record = los(
            candidate["grid_id"], candidate["grid_id"], candidate["point"], end_point,
        )
        if not record["ok"]:
            continue
        target_links.append({
            **candidate,
            "record": record,
            "length_m": float(record.get("segment_length_m") or 0.0),
            "bearing_deg": candidate.get("link_bearing_deg"),
        })
    if not target_links:
        return _unsolved_search(
            statistics, cap_reached=False, termination="no_traversable_target_anchor",
            los_records={}, endpoint_anchors={
                "source_anchor_count": len(endpoints.get("source_anchors") or []),
                "admitted_source_anchor_count": len(source_links),
                "target_anchor_count": len(endpoints.get("target_anchors") or []),
                "admitted_target_anchor_count": 0,
            },
        )
    source_link_index = {item["grid_id"]: index for index, item in enumerate(source_links)}
    target_link_index = {item["grid_id"]: index for index, item in enumerate(target_links)}
    #: The start labels themselves: a segment *leaving* one of them is the departure
    #: transition turn (never charged), exactly like the arrival connector turn.
    start_label_keys = {
        (item["grid_id"], heading_bin_for_bearing(item["bearing_deg"], heading_bin_count))
        for item in source_links
    }
    endpoint_anchors = {
        "source_anchor_count": len(endpoints.get("source_anchors") or []),
        "admitted_source_anchor_count": len(source_links),
        "target_anchor_count": len(endpoints.get("target_anchors") or []),
        "admitted_target_anchor_count": len(target_links),
        "source_anchor_grid_ids": sorted(source_link_index),
        "target_anchor_grid_ids": sorted(target_link_index),
        "aperture_deg": endpoints.get("aperture_deg"),
        "candidate_search_space": len(source_links) * len(target_links),
        "exact_start_equals_first_path_point": True,
        "exact_end_equals_last_path_point": True,
    }

    # Admissible lower bound on the remaining objective: every remaining metre costs at
    # least ``distance_weight + risk_weight * min_risk_index`` and a turn is never negative.
    # The largest terminal connector is subtracted because at most that much of the
    # remaining distance is already travelled inside the arrival anchor candidate.
    max_terminal_length = max(float(item["length_m"]) for item in target_links)
    risk_floor = min(float(value) for value in risk_indices.values()) if risk_indices else 0.0
    heuristic_factor = weight_distance + weight_risk * risk_floor

    def heuristic(grid_id):
        remaining = distance_m(graph.centers[grid_id], end_point) - max_terminal_length
        return heuristic_factor * max(0.0, remaining)

    costs = {}
    parents = {}
    incoming = {}
    edge_bearing = {}
    accumulated = {}
    depth = {}
    #: Real (never bin-centre) bearing of the segment that leads into each label.
    real_bearing = {}
    queue = []
    los_records = {}
    expanded = 0
    cap_reached = False
    termination = "queue_exhausted"
    best_goal = None
    goal_candidates = []
    closed = set()
    label_sequence = 0

    for index, link in enumerate(source_links):
        key = (
            link["grid_id"],
            heading_bin_for_bearing(link["bearing_deg"], heading_bin_count),
        )
        start_ledger = _extend_ledger(
            _empty_ledger(),
            risk_exposure=link["record"]["risk_exposure_index_m"],
            distance=link["length_m"],
            previous_heading=None, new_heading=link["bearing_deg"],
            weights=(weight_risk, weight_turn, weight_distance), d_ref=d_ref,
            theta_min_deg=theta_min_deg,
        )
        start_cost = _ledger_total_cost(start_ledger, weight_risk, weight_turn, weight_distance)
        label_sequence += 1
        costs[key] = start_cost
        parents[key] = None
        incoming[key] = None
        edge_bearing[key] = link["bearing_deg"]
        accumulated[key] = start_ledger
        depth[key] = 1
        real_bearing[key] = link["bearing_deg"]
        heappush(
            queue,
            (start_cost + heuristic(link["grid_id"]), start_cost, label_sequence,
             link["grid_id"], key[1]),
        )

    while queue:
        queue_bound, _, _, current_grid, current_bin = heappop(queue)
        current_key = (current_grid, current_bin)
        current_cost = costs.get(current_key)
        if current_cost is None:
            continue
        # A queued entry is ranked by ``cost + admissible_remaining``, so once the best
        # queued bound can no longer beat the best *complete* OD goal, no unexplored or
        # unpriced route can improve it either.
        if best_goal is not None and queue_bound >= best_goal["total_cost"] - 1e-9:
            termination = "queue_lower_bound_exceeds_best_goal"
            break
        if current_key in closed:
            continue
        if max_expanded_labels is not None and expanded >= max_expanded_labels:
            cap_reached = True
            termination = "expansion_cap_reached"
            break
        expanded += 1
        statistics["expanded_labels"] = expanded
        closed.add(current_key)
        current_point = graph.centers[current_grid]
        current_ledger = accumulated[current_key]
        current_bearing = real_bearing.get(current_key)
        current_bin_heading = (
            None if current_bearing is None
            else heading_bin_center_deg(current_bin, heading_bin_count)
        )
        terminal_index = target_link_index.get(current_grid)
        if terminal_index is not None:
            # A goal label is never accepted merely because it reached an arrival anchor:
            # the ``anchor centre -> exact end`` connector belongs to the same OD objective,
            # and labels arriving under different incoming bearings pay a different terminal
            # turn.  Every goal label is priced completely and the cheapest complete OD
            # objective wins.  The goal label is not expanded further.
            terminal = target_links[terminal_index]
            # Connector accounting: ``anchor centre -> exact end`` prices its distance and
            # risk but is not charged a turn (departure/arrival transition semantics).  The
            # search uses exactly the same accounting the final audit uses, which is what
            # keeps ``search_goal_ledger`` and the recomputed exact-OD ledger identical.
            complete_ledger = _extend_ledger(
                current_ledger,
                risk_exposure=terminal["record"]["risk_exposure_index_m"],
                distance=terminal["length_m"],
                previous_heading=None, new_heading=None,
                is_connector_turn=True,
                weights=(weight_risk, weight_turn, weight_distance), d_ref=d_ref,
                theta_min_deg=theta_min_deg,
            )
            complete_cost = _ledger_total_cost(
                complete_ledger, weight_risk, weight_turn, weight_distance
            )
            goal_candidates.append({
                "grid_id": current_grid, "incoming_heading_bin": current_bin,
                "incoming_heading_deg": current_bin_heading,
                "incoming_bearing_deg": current_bearing,
                "terminal_heading_deg": (
                    None if terminal["bearing_deg"] is None
                    else heading_bin_center_deg(
                        heading_bin_for_bearing(terminal["bearing_deg"], heading_bin_count),
                        heading_bin_count,
                    )
                ),
                "terminal_bearing_deg": terminal["bearing_deg"],
                "terminal_length_m": _round(terminal["length_m"]),
                "terminal_link_index": terminal_index,
                "risk_exposure_index_m": _round(complete_ledger["risk"]),
                "turn_cost_m": _round(complete_ledger["turn"]),
                "distance_m": _round(complete_ledger["distance"]),
                "total_cost": _round(complete_cost),
            })
            if best_goal is None or complete_cost < best_goal["total_cost"] - 1e-9:
                best_goal = {
                    "key": current_key, "total_cost": complete_cost,
                    "ledger": complete_ledger, "terminal_index": terminal_index,
                }
            continue

        parent_key = incoming[current_key]
        grandparent_key = incoming.get(parent_key) if parent_key is not None else None
        grandparent_grid = parents.get(parent_key) if parent_key is not None else None
        path2_eligible = grandparent_grid is not None and grandparent_key is not None
        source_anchor_grids = {
            item["grid_id"] for item in source_links
        }

        for neighbour in graph.neighbors(current_grid):
            if neighbour in source_anchor_grids:
                # A source anchor is the route's fixed virtual origin: its label ledger is
                # exactly the ``exact start -> anchor`` connector and must not be re-relaxed
                # from the middle of the search.  Doing so would move the origin into the
                # route and charge the first segment a turn against a heading never flown.
                continue
            guards = graph.diagonal_guards(current_grid, neighbour)
            if guards is not None and any(
                guard is None or gate(guard) is not None for guard in guards
            ):
                continue
            neighbour_point = graph.centers[neighbour]

            # ---- path 1: direct step, requires the current -> neighbour LOS to be clear.
            direct = los(current_grid, neighbour, current_point, neighbour_point)
            if not direct["ok"]:
                continue
            direct_bearing = grid_bearing_deg(current_point, neighbour_point)
            direct_bin = heading_bin_for_bearing(direct_bearing, heading_bin_count)
            direct_ledger = _extend_ledger(
                current_ledger, risk_exposure=direct["risk_exposure_index_m"],
                # The ledger accumulates the **real length of this LOS segment**: the
                # objective's distance term must describe the chain actually being
                # installed, not a repeatedly re-added origin-to-neighbour distance.
                distance=direct["segment_length_m"],
                previous_heading=current_bearing,
                # BUG-TURN-STAT-001: the turn is priced between the **actual** bearings of
                # the two LOS segments, never between their heading-bin centres.
                new_heading=direct_bearing,
                # A segment leaving a source anchor turns out of the departure connector:
                # that turn belongs to the departure transition and is never charged to the
                # cruise turn cost -- exactly like the arrival side.  Without this the search
                # would minimise a cost the reported objective does not use.
                is_connector_turn=current_key in start_label_keys,
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
                edge_bearing=edge_bearing, real_bearing=real_bearing,
                accumulated=accumulated, closed=closed, depth=depth,
                statistics=statistics, queue=queue, los_records=los_records,
                los_result=direct, label_sequence=label_sequence,
            )
            label_sequence = statistics["generated_labels"]

            if not path2_eligible:
                continue
            # ---- path 2: the Theta* shortcut grandparent -> neighbour.
            grandparent_point = graph.centers[grandparent_grid]
            if grandparent_grid == neighbour:
                continue
            shortcut = los(grandparent_grid, neighbour, grandparent_point, neighbour_point)
            if not shortcut["ok"]:
                continue
            grandparent_cost = costs.get(grandparent_key)
            if grandparent_cost is None:
                continue
            grandparent_ledger = accumulated.get(grandparent_key)
            if grandparent_ledger is None:
                continue
            grandparent_bearing = real_bearing.get(grandparent_key)
            shortcut_bearing = grid_bearing_deg(grandparent_point, neighbour_point)
            shortcut_bin = heading_bin_for_bearing(shortcut_bearing, heading_bin_count)
            shortcut_ledger = _extend_ledger(
                # The rewritten chain is ``... -> grandparent -> neighbour``, so the ledger
                # must extend the **grandparent's** ledger and price the real
                # ``grandparent -> neighbour`` LOS segment.  Extending the current label's
                # own ledger would keep the replaced ``grandparent -> current`` leg in the
                # cost of a route that no longer contains it.
                grandparent_ledger, risk_exposure=shortcut["risk_exposure_index_m"],
                distance=shortcut["segment_length_m"],
                previous_heading=grandparent_bearing,
                new_heading=shortcut_bearing,
                weights=(weight_risk, weight_turn, weight_distance), d_ref=d_ref,
                theta_min_deg=theta_min_deg,
            )
            # ``grandparent_key`` becomes the new label's parent: the shortcut *replaces*
            # the two-segment detour through ``current_key`` with the single straight
            # ``grandparent -> neighbour`` segment, which is exactly what Theta* rewiring
            # means.  Keeping ``current_key`` as the parent instead would leave a phantom
            # vertex at ``current_grid`` in the reconstructed route geometry.
            _relax(
                grid_id=neighbour,
                bin_index=shortcut_bin,
                bearing=shortcut_bearing,
                ledger=shortcut_ledger,
                risk_weight=weight_risk, turn_weight=weight_turn,
                distance_weight=weight_distance,
                parent_key=grandparent_key,
                parent_grid=grandparent_grid,
                heuristic=heuristic,
                costs=costs, parents=parents, incoming=incoming,
                edge_bearing=edge_bearing, real_bearing=real_bearing,
                accumulated=accumulated, closed=closed, depth=depth,
                statistics=statistics, queue=queue, los_records=los_records,
                los_result=shortcut, label_sequence=label_sequence, is_rewire=True,
            )
            label_sequence = statistics["generated_labels"]

    if best_goal is None:
        return _unsolved_search(
            statistics, cap_reached=cap_reached, termination=termination,
            los_records=los_records, goal_candidates=goal_candidates,
            endpoint_anchors=endpoint_anchors,
        )

    best_goal_key = best_goal["key"]

    # Reconstruct the label chain.  ``grid_path`` is the route's *vertex* cell sequence: the
    # cells the any-angle route actually turns at.  A straight segment between two
    # consecutive vertices is a Theta* LOS shortcut, so consecutive vertices are frequently
    # non-adjacent -- that is the whole point.
    chain = []
    key = best_goal_key
    while key is not None:
        chain.append(key)
        key = incoming[key]
    chain.reverse()
    grid_path = [item[0] for item in chain]
    start_link = source_links[source_link_index.get(grid_path[0], 0)]
    terminal_link = target_links[best_goal["terminal_index"]]

    points = [list(start_point)] + [list(graph.centers[item]) for item in grid_path] + [
        list(end_point)
    ]
    route_points = []
    for point in points:
        if route_points and point == route_points[-1]:
            continue
        route_points.append(point)

    los_segments = _route_segment_audit(
        graph, index_map, grid_path, start_point, end_point, start_link, terminal_link,
        gate, altitude, regulatory, risk_indices, statistics, heading_bin_count,
    )

    # Authoritative ledger: recomputed from the *final* parent chain and the finalized
    # endpoint connectors, with the **real** segment bearings.  It is derived from the same
    # exact-OD segment audit the goal labels were priced with, which is why the winning
    # search ledger and this ledger describe one and the same route.
    #
    # Turn charging rule (BUG-TURN-STAT-001 + BUG-ROUTE-006): the first and the last segment
    # are endpoint connectors, so a turn that touches either of them is a departure/arrival
    # transition turn and is *not* charged to the cruise turn cost.  The rule is keyed on the
    # segment positions of the finalized geometry, which makes ``turn_statistics`` exactly
    # reproducible from ``candidate.path`` alone -- including the degenerate case where an
    # endpoint already sits on its anchor centre and its connector therefore has zero length
    # and does not appear in ``candidate.path`` at all.
    connector_positions = {0, len(los_segments) - 1}
    ledger = _empty_ledger()
    previous_heading = None
    for position, segment in enumerate(los_segments):
        heading = segment["outgoing_bearing_deg"]
        ledger = _extend_ledger(
            ledger,
            risk_exposure=segment["risk_exposure_index_m"],
            distance=segment["length_m"],
            previous_heading=previous_heading,
            new_heading=heading,
            is_connector_turn=(
                position in connector_positions
                or (position - 1) in connector_positions
            ),
            weights=(weights["risk"], weights["turn"], weights["distance"]),
            d_ref=d_ref, theta_min_deg=theta_min_deg, segment_index=position,
        )
        segment["incoming_heading_deg"] = (
            None if previous_heading is None
            else _bin_centre_heading(previous_heading, heading_bin_count)
        )
        segment["incoming_bearing_deg"] = previous_heading
        previous_heading = heading
    accumulated[best_goal_key] = ledger
    return {
        "path": route_points, "grid_path": grid_path,
        "label_chain": [
            {"grid_id": item[0], "incoming_heading_bin": item[1]} for item in chain
        ],
        "accumulated": ledger,
        "search_goal_ledger": best_goal["ledger"],
        "goal_candidates": goal_candidates,
        "termination": termination,
        "los_segments": los_segments,
        "los_records": los_records,
        "endpoint_anchors": endpoint_anchors,
        "statistics": statistics, "cap_reached": cap_reached,
    }


def _bin_centre_heading(bearing_deg, bin_count):
    """Snap a raw bearing to its heading-bin centre in the search's own binning.

    Used **only** for reporting/compatibility fields tied to the search's own discretization
    (``label_chain``, ``guest_heading_deg`` style columns and the historical
    ``outgoing_heading_deg``).  It is never used to compute a turn angle or a turn cost --
    that always uses the real segment bearing (BUG-TURN-STAT-001).
    """

    if bearing_deg is None:
        return None
    return heading_bin_center_deg(
        heading_bin_for_bearing(bearing_deg, bin_count), bin_count
    )


def _route_segment_audit(
    graph, index_map, grid_path, start_point, end_point, start_link, terminal_link,
    gate, altitude, regulatory, risk_indices, statistics, heading_bin_count,
):
    """One entry per route segment, in order, with its crossed cells and real headings.

    Each segment is re-traversed with the **same** supercover LOS routine the search used,
    so the reported crossed cells, in-cell lengths and risk integral are the authoritative
    ones for the finalized geometry.  The re-traversal is an audit: it passes
    ``counted=False`` and therefore does not move any search counter.

    Two endpoint connectors always appear in the list and are marked as such:

    * the first segment is the departure connector ``exact start -> selected source anchor``
      (zero length when the exact origin already sits on the anchor centre);
    * the last segment is the arrival connector ``selected target anchor -> exact end``.

    Both carry their distance and risk into the OD objective; neither is charged a turn
    (see ``SEARCH_SEMANTICS``).  ``start_link`` is retained here because it *is* the first
    segment's geometry and provenance: the audit re-traverses it with ``counted=False``
    rather than re-deriving it, so the audit can never describe a different connector than
    the one the objective was minimised over.
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
            counted=False,
        )
        outward = grid_bearing_deg(entry, exit_point)
        # The route's first segment is always its departure connector: ``exact start ->
        # selected source anchor``.  It can be zero length (the exact origin already sits on
        # the anchor centre), in which case it contributes nothing to distance, risk or turn,
        # but it still means the first search segment is not a cruise turn -- exactly like the
        # arrival side.
        is_start_connector = position == 0 and previous_grid is None
        segments.append({
            "segment_index": len(segments),
            "from_grid_id": previous_grid or grid_id,
            "to_grid_id": grid_id,
            "from_coordinate": entry,
            "to_coordinate": exit_point,
            "length_m": _round(distance_m(entry, exit_point)),
            "traversed_cells": list((record or {}).get("cells") or []),
            "risk_exposure_index_m": _round((record or {}).get("risk_exposure_index_m")),
            # Real geometry, and the value the ledger turns are computed from.
            "outgoing_bearing_deg": outward,
            # Discretized compatibility view of the same segment.
            "outgoing_heading_deg": _bin_centre_heading(outward, heading_bin_count),
            "incoming_bearing_deg": None,
            "incoming_heading_deg": None,
            "connector": "start_connector" if is_start_connector else "cruise",
            "supercover": True,
            "shortcut": True,
        })
        previous_grid = grid_id
    if grid_path:
        exit_point = list(graph.centers[grid_path[-1]])
        # The terminal connector must be audited with **exactly** the anchor the search
        # priced it with (``terminal_link["grid_id"]``).  Re-deriving it with
        # ``containing_cell(end_point)`` can name a different -- adjacent -- cell when the
        # exact endpoint sits on or next to a cell boundary, which would make the audit
        # describe a different connector than the one the objective was minimised over.
        to_grid = (terminal_link or {}).get("grid_id") or (
            graph.containing_cell(end_point) or grid_path[-1]
        )
        record = line_of_sight(
            graph, index_map,
            source_id=previous_grid or grid_path[-1],
            target_id=to_grid,
            source_point=exit_point, target_point=list(end_point), gate=gate,
            altitude_m=altitude, regulatory=regulatory, risk_indices=risk_indices,
            statistics=audit_statistics, counted=False,
        )
        outward = grid_bearing_deg(exit_point, end_point)
        segments.append({
            "segment_index": len(segments),
            "from_grid_id": grid_path[-1],
            "to_grid_id": to_grid,
            "from_coordinate": exit_point,
            "to_coordinate": list(end_point),
            "length_m": _round(distance_m(exit_point, end_point)),
            "traversed_cells": list((record or {}).get("cells") or []),
            "risk_exposure_index_m": _round((record or {}).get("risk_exposure_index_m")),
            "outgoing_bearing_deg": outward,
            "outgoing_heading_deg": _bin_centre_heading(outward, heading_bin_count),
            "incoming_bearing_deg": None,
            "incoming_heading_deg": None,
            "connector": "terminal_connector",
            "supercover": True,
            "shortcut": True,
        })
    return segments


def _cruise_turn_statistics(ledger):
    """Turn statistics restricted to the cruise segments (endpoint connectors excluded).

    ``cruise_turn_*`` is what the *planning* turn term describes: the smoothness of the
    cruise portion.  Departure/arrival transition turns are reported separately
    (``connector_turn_*``) instead of being mixed into the cruise turn cost.
    """

    cruise_turns = [
        turn for turn in ledger.get("turns") or [] if not turn.get("connector")
    ]
    connector_turns = [
        turn for turn in ledger.get("turns") or [] if turn.get("connector")
    ]
    return {
        "cruise_turn_count": len(cruise_turns),
        "cruise_total_heading_change_deg": round(
            sum(float(item["heading_change_deg"]) for item in cruise_turns), 9
        ),
        "cruise_turn_cost_m": round(
            sum(float(item.get("cost_m") or 0.0) for item in cruise_turns), 9
        ),
        "connector_turn_count": len(connector_turns),
        "connector_total_heading_change_deg": round(
            sum(float(item["heading_change_deg"]) for item in connector_turns), 9
        ),
        "connector_turn_cost_m": round(
            sum(float(item.get("cost_m") or 0.0) for item in connector_turns), 9
        ),
        "cruise_turn_semantics": (
            "endpoint_connector_turn_is_reported_separately_and_never_charged_to_"
            "cruise_turn_cost"
        ),
    }


def _ledger_total_cost(ledger, weight_risk, weight_turn, weight_distance):
    """``J = wr*E_risk + wt*C_turn + wd*L`` for one accumulated ledger.

    Kept in one place so the search's own goal pricing and the reported objective can never
    drift apart: both call this with the same weights.
    """

    return (
        weight_risk * float(ledger["risk"])
        + weight_turn * float(ledger["turn"])
        + weight_distance * float(ledger["distance"])
    )


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


def _goal_ledger_consistency(search, weights):
    """搜索端 winning goal ledger 与最终 exact-OD audit ledger 的一致性证据。

    两者必须描述同一条 route：搜索选择 goal 时使用的 distance / risk / turn 账本，就是
    candidate 最终报告的 ``planning_objective``。任何不一致都是 BUG-ROUTE-004 的回归。
    """

    item = search if isinstance(search, dict) else {}
    audit = item.get("accumulated") or _empty_ledger()
    winning = item.get("search_goal_ledger") or _empty_ledger()
    weight_risk = float(weights["risk"])
    weight_turn = float(weights["turn"])
    weight_distance = float(weights["distance"])
    search_cost = _ledger_total_cost(winning, weight_risk, weight_turn, weight_distance)
    audit_cost = _ledger_total_cost(audit, weight_risk, weight_turn, weight_distance)
    difference = abs(search_cost - audit_cost)
    return {
        "search_total_cost": _round(search_cost),
        "audit_total_cost": _round(audit_cost),
        "absolute_difference": _round(difference),
        "consistent": bool(difference <= 1e-6),
        "search_distance_m": _round(winning["distance"]),
        "audit_distance_m": _round(audit["distance"]),
        "search_risk_exposure_index_m": _round(winning["risk"]),
        "audit_risk_exposure_index_m": _round(audit["risk"]),
        "search_turn_cost_m": _round(winning["turn"]),
        "audit_turn_cost_m": _round(audit["turn"]),
        "semantics": (
            "winning_search_goal_ledger_must_equal_the_exact_od_audit_ledger_for_"
            "distance_risk_and_turn"
        ),
    }


def _unsolved_search(
    statistics, *, cap_reached, termination, los_records, goal_candidates=(),
    endpoint_anchors=None,
):
    """Structured "no goal label was priced" search result (never a fabricated ledger)."""

    return {
        "path": None, "grid_path": None, "los_segments": [], "los_records": los_records,
        "accumulated": None, "search_goal_ledger": None,
        "goal_candidates": list(goal_candidates), "termination": termination,
        "statistics": statistics, "cap_reached": cap_reached,
        "endpoint_anchors": endpoint_anchors,
    }


def _ledger_distance(ledger):
    return float(ledger.get("ledger_distance", ledger.get("distance", 0.0)))


def _relax(
    *, grid_id, bin_index, bearing, ledger, risk_weight, turn_weight, distance_weight,
    parent_key, parent_grid, heuristic, costs, parents, incoming, edge_bearing,
    real_bearing, accumulated, closed, statistics, queue, los_records, los_result, depth,
    label_sequence, is_rewire=False,
):
    """Install (or improve) one label.

    **Label-setting with closed labels.**  A label that has already been expanded is never
    reopened.  This is what keeps the cumulative ledger exact: a Theta* shortcut can
    legitimately reach a cell under a *different* prefix than the staircase that was
    expanded first, and reopening it would leave every label already derived from the old
    prefix describing a route that no longer exists.  Freezing a closed label makes the
    prefix ledger of every descendant stay the authoritative description of the chain it
    actually hangs from.

    **Equal-cost tie-break.**  When two chains price the *identical* objective, the one
    with fewer labels wins.  This never changes an objective value, never reopens a closed
    label and never introduces a post-processing smoother: it only makes the search prefer
    the straight LOS shortcut over an equally priced staircase, which is the any-angle
    character of Theta* itself.  If both chains have the same length as well, the one with
    the smaller accumulated turn is installed, so the label's stored real bearing (the input
    of the next turn charge) is the best one available for that ``(grid, bin)`` state.

    ``real_bearing`` stores the **actual** bearing of the segment leading into the label;
    the ``bin_index`` only partitions the state space.
    """

    key = (grid_id, bin_index)
    statistics["generated_labels"] += 1
    if key in closed:
        return
    cost = _ledger_total_cost(ledger, risk_weight, turn_weight, distance_weight)
    new_depth = int(depth.get(parent_key, 0)) + 1
    known = costs.get(key)
    if known is not None:
        if cost > known + 1e-9:
            return
        if cost >= known - 1e-9:
            if new_depth >= int(depth.get(key, new_depth)):
                return
            previous = accumulated.get(key) or _empty_ledger()
            if new_depth == int(depth.get(key, new_depth)) and float(
                ledger["turn"]
            ) >= float(previous["turn"]) - 1e-12:
                return
    costs[key] = cost
    depth[key] = new_depth
    parents[key] = parent_grid
    incoming[key] = parent_key
    edge_bearing[key] = bearing
    real_bearing[key] = bearing
    accumulated[key] = ledger
    # The admitted traversal is recorded under the ``(from, to)`` grid pair the route
    # geometry actually uses, so the reported crossed cells are the search's own.
    los_records[(parent_grid, grid_id)] = los_result
    if is_rewire:
        statistics["rewired_parent_shortcuts"] += 1
    heappush(
        queue,
        (cost + heuristic(grid_id), cost, label_sequence, grid_id, bin_index),
    )


def _extend_ledger(
    base, *, risk_exposure, distance, previous_heading, new_heading, weights, d_ref,
    theta_min_deg, is_connector_turn=False, segment_index=None,
):
    """Extend a label's objective ledger with one straight sub-segment.

    ``previous_heading`` / ``new_heading`` are **real segment bearings** in the planning
    frame, not heading-bin centres (BUG-TURN-STAT-001): the bin discretizes the state, never
    the angle.  A ``None`` previous heading means there is no preceding turn (the route
    start), which is never charged.

    ``is_connector_turn=True`` marks a departure/arrival transition turn: it is recorded
    (so ``turn_statistics`` can report it separately) but **never charged** to the cruise
    turn cost.  The flag is decided by the caller from the finalized segment positions, so
    the same rule can be replayed from ``candidate.path`` alone.
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
        if delta > float(theta_min_deg):
            cost = float(d_ref) * (1.0 + delta / 180.0) if d_ref else 0.0
            if not is_connector_turn:
                ledger["turn"] += cost
                ledger["turn_count"] += 1
                ledger["heading_change"] += delta
            ledger["turns"].append({
                "from_heading_deg": round(float(previous_heading), 9),
                "to_heading_deg": round(float(new_heading), 9),
                "heading_change_deg": round(delta, 9),
                "cost_m": _round(cost),
                "segment_index": segment_index,
                "connector": bool(is_connector_turn),
                "charged": not is_connector_turn,
            })
    return ledger


def _turn_statistics(ledger, *, d_ref, theta_min_deg):
    """``turn_statistics`` for one finalized ledger.

    ``turn_count`` / ``total_heading_change_deg`` / ``turn_cost_m`` are exactly the charged
    cruise turns of the ledger the objective used, and they can be recomputed from
    ``candidate.path`` plus the endpoint connector classification alone
    (BUG-TURN-STAT-001).  Departure/arrival transition turns are reported separately in
    ``turns`` (``connector=true``, ``charged=false``) and in the ``connector_*`` block, never
    inside the cruise turn cost.
    """

    cruise = _cruise_turn_statistics(ledger)
    return {
        "turn_count": int(ledger["turn_count"]),
        "total_heading_change_deg": round(float(ledger["heading_change"]), 9),
        "turn_cost_m": _round(ledger["turn"]),
        "turns": list(ledger.get("turns") or []),
        "theta_min_deg": theta_min_deg,
        "d_ref_m": _round(d_ref),
        "semantics": "planning_smoothness_proxy_not_flight_dynamics_validation",
        "bearing_semantics": TURN_ACCOUNTING_SEMANTICS["definition"],
        "heading_bin_used_as_the_turn_angle": False,
        **cruise,
    }


def _objective_metrics(search, weights, d_ref, theta_min_deg):
    accumulated = search["accumulated"]
    risk = float(accumulated["risk"])
    turn = float(accumulated["turn"])
    distance = float(accumulated["distance"])
    terms = weighted_terms(risk, turn, distance, {"risk_weight": weights["risk"],
                                                 "turn_weight": weights["turn"],
                                                 "distance_weight": weights["distance"],
                                                 "confirmed": True})
    cruise = _cruise_turn_statistics(accumulated)
    objective = {
        "risk_exposure_index_m": _round(risk),
        "turn_count": int(accumulated["turn_count"]),
        "total_heading_change_deg": round(float(accumulated["heading_change"]), 9),
        "turn_cost_m": _round(turn),
        "distance_m": _round(distance),
        "cruise_turn_count": cruise["cruise_turn_count"],
        "cruise_total_heading_change_deg": cruise["cruise_total_heading_change_deg"],
        "cruise_turn_cost_m": cruise["cruise_turn_cost_m"],
        **{key: terms[key] for key in (
            "risk_weight", "turn_weight", "distance_weight",
            "weighted_risk", "weighted_turn", "weighted_distance", "total_cost",
        )},
        "formula": OBJECTIVE_FORMULA,
        "objective_population_shelter_only": True,
        "turn_semantics": TURN_ACCOUNTING_SEMANTICS["definition"],
    }
    turn_statistics = _turn_statistics(accumulated, d_ref=d_ref, theta_min_deg=theta_min_deg)
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
