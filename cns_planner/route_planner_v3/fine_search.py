"""V3-B corridor-local fine refinement search.

State: ``(fine_cell_id, altitude_index, heading_bin)`` -- the same Markov shape as
V3-A, but now over the corridor-local metric fine grid, with EGM2008 as the only
vertical reference and the same explicit planning policy.

What is different from V3-A, on purpose:

* **multi-cell stride.**  ``FineMotionPrimitive`` moves ``stride`` fine cells at a
  time instead of one, because a single fine cell provides so little arc length
  that ``R * dpsi <= stride_m`` would reject almost every turn.  A turn primitive
  must still satisfy the existing engineering arc-length constraint
  ``R * |dpsi| <= stride_m``.  The model remains an **engineering arc-length
  proxy**, not exact curvature; the continuous V3-C validation is still pending;
* **every traversed cell is checked.**  Each primitive records its
  ``traversed_cell_ids`` and the entry fraction at which the path enters each of
  them, then re-checks airspace / terrain / building against the altitude
  *interpolated along the path*.  A stride can therefore never jump over an
  intermediate obstacle or over an intermediate terrain spike;
* readiness still fails closed: unknown source evidence, a stale strategic
  fingerprint or a blocked policy stops the run instead of being guessed.
"""

from __future__ import annotations

from heapq import heappop, heappush
from math import atan2, degrees, hypot, inf, isclose, radians
import time

from ..benchmark.geodesy import geodesic_distance_m
from .contracts import (
    SEARCH_COMPLETENESS, build_v3_state, describe_altitude_states,
    heading_bin_center_deg, heading_bin_for_bearing,
)
from .cost import (
    MAPPED_SOFT_CHANNELS, SoftCostModel, build_cost_vector,
    building_exposure_normalized, soft_channel_provenance,
    summarize_channel_exposures,
)
from .fine_contracts import (
    FINE_ALGORITHM_ID, FINE_ALGORITHM_VERSION, FINE_MODEL_SCOPE,
    REFINEMENT_FINGERPRINT_COMPONENTS, REFINEMENT_TURN_MODEL,
    TERRAIN_SAMPLING_METHOD, V3B_DISCLAIMER, V3B_RESULT_STATUSES, V3C_PENDING,
    empty_v3_refinement_result, normalize_v3_refinement_problem,
    refinement_fingerprint_components,
)
from .fine_grid import fine_adjacency, fine_cells_from_spec, fine_index
from .hard_constraints import HardConstraintAudit
from .motion import (
    TransitionValidator, kinematic_readiness, normalize_heading_delta,
)
from .readiness import (
    aircraft_readiness, cost_model_readiness, policy_readiness, readiness_overall,
    unresolved_soft_channel_cells,
)

#: Reasons a *traversed* (intermediate) fine cell rejects a primitive.
TRAVERSED_REASONS = (
    "traversed_cell_not_in_environment",
    "traversed_airspace_not_confirmed_allowed",
    "traversed_airspace_unknown",
    "traversed_terrain_clearance_unresolved",
    "traversed_below_terrain_clearance",
    "traversed_building_clearance_unresolved",
    "traversed_below_building_clearance",
    "traversed_cell_outside_corridor_altitude_envelope",
)

#: Fixed, ordered grid move set (E, NE, N, NW, W, SW, S, SE).
_GRID_DELTAS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

_COMPARISON_TOLERANCE = 1e-9


def grid_bearing_deg(delta_column, delta_row):
    """Local metric grid bearing in degrees, 0 = north, clockwise.

    The fine grid is a *local metric* construction, so the bearing is measured in
    that local frame -- not a geodesic azimuth.  The frame (origin, axis, CRS) is
    recorded in ``LocalMetricPlanningFrame`` precisely so this is auditable.
    """

    return round(degrees(atan2(float(delta_column), float(delta_row))) % 360.0, 9)


#: Boundary-touch semantics of :func:`supercover_line`.
#:
#: The traversal is **conservative**: a cell that only *touches* the segment -- at
#: a corner or along an edge, with zero-length contact -- counts as traversed,
#: because V3-B interpolates and hard-checks the altitude at every reported cell.
#: Concretely, when the segment passes exactly through a grid corner, three cells
#: share that point: the two orthogonal neighbours and the diagonal cell.  All
#: three are reported (deduplicated, in a deterministic order).  This is
#: deliberately *more* inclusive than the mathematical supercover and is the safe
#: direction: an intermediate obstacle or terrain spike can never be skipped.
SUPERCOVER_BOUNDARY_SEMANTICS = (
    "conservative_boundary_touch_included_corner_crossing_yields_both_orthogonal_"
    "neighbours_and_the_diagonal_cell_without_duplicates"
)


def supercover_line(start, end):
    """Cells a straight segment passes through, as ``(cell, entry_fraction)``.

    Amanatides & Woo voxel traversal in grid-index space with **conservative
    boundary-touch semantics** (see :data:`SUPERCOVER_BOUNDARY_SEMANTICS`).  Every
    cell the segment touches -- including the start and end cells, and both
    orthogonal neighbours plus the diagonal cell at an exact corner crossing -- is
    reported with the segment fraction at which it is *entered*, which is what the
    altitude interpolation for the hard-fact checks uses.  Fractions are
    non-decreasing, so they can be zipped with the cell ids.

    Corner handling is what keeps this honest: a naive diagonal advance at an exact
    corner step would silently drop the two orthogonal neighbours, and a cell the
    path only grazes is still a cell the route occupies.
    """

    start_row, start_column = int(start[0]), int(start[1])
    end_row, end_column = int(end[0]), int(end[1])
    traversed, _corners = _supercover_traversal(start_row, start_column, end_row, end_column)
    return traversed


def supercover_line_with_corners(start, end):
    """As :func:`supercover_line`, plus the exact grid corners the segment crossed."""

    traversed, corners = _supercover_traversal(
        int(start[0]), int(start[1]), int(end[0]), int(end[1]),
    )
    return traversed, corners


def corner_crossing_points(traversed):
    """Grid corners a traversal passed exactly through, in traversal order.

    Reported in grid space: ``(row + 0.5, column + 0.5)`` marks the shared corner of
    the current cell and both orthogonal neighbours (the diagonal cell's opposite
    corner).  For the canonical form returned by :func:`supercover_line`, a real
    corner crossing is a diagonal step whose two cells share the **same** entry
    fraction -- the fraction of the corner itself.
    """

    corners = []
    for (previous, previous_entry), (following, following_entry) in zip(
        traversed or [], (traversed or [])[1:],
    ):
        row_delta = following[0] - previous[0]
        column_delta = following[1] - previous[1]
        if row_delta and column_delta and previous_entry == following_entry:
            corners.append((previous[0] + row_delta / 2, previous[1] + column_delta / 2))
    return corners


def _supercover_traversal(start_row, start_column, end_row, end_column):
    """Amanatides & Woo traversal returning ``(cells, corner_crossings)``."""

    delta_row, delta_column = end_row - start_row, end_column - start_column
    if delta_row == 0 and delta_column == 0:
        return [((start_row, start_column), 0.0)], []
    step_row = (delta_row > 0) - (delta_row < 0)
    step_column = (delta_column > 0) - (delta_column < 0)
    if delta_row != 0:
        t_max_row = ((start_row + 0.5 * step_row) - start_row) / delta_row
        t_delta_row = 1.0 / abs(delta_row)
    else:
        t_max_row, t_delta_row = inf, inf
    if delta_column != 0:
        t_max_column = ((start_column + 0.5 * step_column) - start_column) / delta_column
        t_delta_column = 1.0 / abs(delta_column)
    else:
        t_max_column, t_delta_column = inf, inf
    current = (start_row, start_column)
    traversed = [(current, 0.0)]
    seen = {current}
    corner_crossings = []
    guard = 0
    while current != (end_row, end_column):
        guard += 1
        if guard > 6 * (abs(delta_row) + abs(delta_column)) + 8:
            break
        # ``t_max_row`` is when the segment crosses the vertical grid line at
        # ``column + step_column * 0.5`` and ``t_max_column`` when it crosses the
        # horizontal line at ``row + step_row * 0.5``.  Their equality therefore
        # means the segment passes exactly through the shared corner of the current
        # cell, the two orthogonal neighbours and the diagonal cell -- which is the
        # boundary-touch case that must not be dropped.
        crossed_corner = (
            delta_row != 0 and delta_column != 0
            and abs(t_max_row - t_max_column) <= _COMPARISON_TOLERANCE
        )
        if crossed_corner:
            # Compute the entry fractions *before* advancing t_max_*, so the corner
            # cells share the exact corner parameter.
            corner_entry = max(0.0, min(1.0, min(t_max_row, t_max_column)))
            _append_traversed(
                traversed, seen, (current[0] + step_row, current[1]), corner_entry,
            )
            _append_traversed(
                traversed, seen, (current[0], current[1] + step_column), corner_entry,
            )
            current = (current[0] + step_row, current[1] + step_column)
            traversed.append((current, corner_entry))
            seen.add(current)
            corner_crossings.append((current[0] - step_row + 0.5 * step_row,
                                     current[1] - step_column + 0.5 * step_column))
            t_max_row += t_delta_row
            t_max_column += t_delta_column
        elif t_max_row < t_max_column - _COMPARISON_TOLERANCE:
            current = (current[0] + step_row, current[1])
            traversed.append((current, max(0.0, min(1.0, t_max_row))))
            seen.add(current)
            t_max_row += t_delta_row
        elif t_max_column < t_max_row - _COMPARISON_TOLERANCE:
            current = (current[0], current[1] + step_column)
            traversed.append((current, max(0.0, min(1.0, t_max_column))))
            seen.add(current)
            t_max_column += t_delta_column
        else:
            # Numerical tie without an exact corner: advance diagonally only.
            current = (current[0] + step_row, current[1] + step_column)
            traversed.append((current, max(0.0, min(1.0, min(t_max_row, t_max_column)))))
            seen.add(current)
            t_max_row += t_delta_row
            t_max_column += t_delta_column
    return traversed, corner_crossings


def _append_traversed(traversed, seen, cell, entry):
    """Record a boundary-touch cell once, keeping fractions non-decreasing."""

    if cell in seen:
        return
    if traversed and entry < traversed[-1][1]:
        entry = traversed[-1][1]
    traversed.append((cell, entry))
    seen.add(cell)


class FinePrimitiveProvider:
    """Deterministic multi-cell-stride primitives over the fine grid."""

    def __init__(self, cells, heading_bin_count=8, max_stride_cells=1):
        if not cells:
            raise ValueError("FinePrimitiveProvider 需要非空 fine grid")
        self.cells = {str(cell["fine_cell_id"]): cell for cell in cells}
        self.index = fine_index(cells)
        self.reverse = {value: key for key, value in self.index.items()}
        self.adjacency = fine_adjacency(cells)
        self.bin_count = int(heading_bin_count)
        if self.bin_count < 4 or 360 % self.bin_count != 0:
            raise ValueError("heading_bin_count 必须是不小于 4 且整除 360 的整数")
        self.heading_step_deg = round(360.0 / self.bin_count, 9)
        self.max_stride_cells = max(1, int(max_stride_cells))
        self._cache = {}

    def stride_length_m(self, left, right):
        a, b = self.cells[left]["center_metric"], self.cells[right]["center_metric"]
        return hypot(b[0] - a[0], b[1] - a[1])

    def for_state(self, fine_cell_id):
        cached = self._cache.get(fine_cell_id)
        if cached is not None:
            return cached
        row, column = self.index[fine_cell_id]
        items = []
        for stride in range(1, self.max_stride_cells + 1):
            for delta_column, delta_row in _GRID_DELTAS:
                target_index = (row + delta_row * stride, column + delta_column * stride)
                target = self.reverse.get(target_index)
                if target is None:
                    continue
                traversed = supercover_line((row, column), target_index)
                ids = [self.reverse[item[0]] for item in traversed if item[0] in self.reverse]
                if not ids or ids[0] != fine_cell_id:
                    continue
                fractions = [item[1] for item in traversed if item[0] in self.reverse]
                if len(ids) != len(fractions):
                    fractions = [index / max(1, len(ids) - 1) for index in range(len(ids))]
                stride_m = self.stride_length_m(fine_cell_id, target)
                bearing = grid_bearing_deg(
                    delta_column * stride, delta_row * stride,
                )
                for altitude_delta, kind in ((0, "horizontal_level"), (-1, "descend"), (1, "climb")):
                    items.append({
                        "primitive_id": (
                            f"f{delta_column:+d}{delta_row:+d}"
                            f"s{stride}" + ("" if altitude_delta == 0 else f"v{altitude_delta:+d}")
                        ),
                        "kind": kind,
                        "grid_delta_cells": [delta_column * stride, delta_row * stride],
                        "stride_cells": stride,
                        "stride_m": round(stride_m, 9),
                        "bearing_deg": bearing,
                        "altitude_delta_steps": altitude_delta,
                        "traversed_cell_ids": ids,
                        "traversed_entry_fractions": [round(value, 12) for value in fractions],
                        "requires_horizontal_motion": True,
                        "requires_altitude_change": altitude_delta != 0,
                        "turn_model": REFINEMENT_TURN_MODEL,
                    })
        self._cache[fine_cell_id] = tuple(items)
        return self._cache[fine_cell_id]


# --------------------------------------------------------------------------- readiness


def _entry(name, status, reasons, **extra):
    record = {"name": name, "status": status, "reasons": list(reasons)}
    record.update(extra)
    return record


def _downgrade(entry, status):
    if entry["status"] == "ready":
        entry["status"] = status


def evaluate_refinement_readiness(problem):
    """Domains: strategic source / corridor / frame / airspace / terrain / building /
    policy / aircraft / cost model / refinement fingerprint."""

    environment = problem.get("environment") or {}
    cells = environment.get("cells") or []
    policy = problem.get("policy") or {}
    properties = environment.get("properties") or {}
    return {
        "strategic_source": _strategic_source(problem),
        "corridor": _corridor(problem),
        "frame": _frame(problem),
        "airspace": _fine_airspace(cells),
        "terrain": _fine_terrain(cells, properties),
        "building": _fine_building(cells, properties),
        "policy": policy_readiness(policy),
        "aircraft": aircraft_readiness(policy, problem.get("aircraft_motion_limits") or {}),
        "cost_model": cost_model_readiness(policy, cells, id_field="fine_cell_id"),
        "refinement_fingerprint": _fingerprint_domain(problem),
    }


def _strategic_source(problem):
    strategic = problem.get("strategic_candidate") or {}
    status = strategic.get("status")
    applicability = str(strategic.get("current_applicability") or "unknown")
    entry = _entry(
        "strategic_source", "ready", [],
        strategic_status=status, current_applicability=applicability,
        experiment_id=strategic.get("experiment_id"),
        semantics="v3b_only_refines_a_selected_current_v3a_strategic_candidate",
    )
    if status != "strategic_candidate":
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"V3-B 只能在选定的 V3-A strategic_candidate 上运行，当前 status={status or 'unknown'}"
        )
    if applicability != "current":
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"V3-A 战略候选不是 current（{applicability}）：stale 候选不得进入精化"
        )
    if not strategic.get("strategic_fingerprint"):
        entry["status"] = "blocked"
        entry["reasons"].append("缺少 strategic fingerprint：无法建立 refinement fingerprint")
    return entry


def _corridor(problem):
    corridor = problem.get("corridor") or {}
    centers = list(corridor.get("center_grid_ids") or [])
    support = list(corridor.get("support_grid_ids") or [])
    entry = _entry(
        "corridor", "ready", [], center_count=len(centers), support_count=len(support),
        corridor_id=corridor.get("corridor_id"), ring_n=corridor.get("ring_n"),
        semantics="corridor_is_the_refinement_search_window_not_a_safety_volume",
    )
    if not centers:
        entry["status"] = "blocked"
        entry["reasons"].append("缺少 V3-A candidate refinement corridor：没有精化搜索窗口")
    if corridor.get("semantics") != "refinement_search_window_not_safety_corridor":
        entry["status"] = "blocked"
        entry["reasons"].append("corridor semantics 不是 refinement_search_window_not_safety_corridor")
    return entry


def _frame(problem):
    frame = problem.get("frame") or {}
    grid = problem.get("fine_grid") or {}
    entry = _entry(
        "frame", "ready", [], horizontal_crs=frame.get("horizontal_crs"),
        resolution_m=frame.get("resolution_m"),
        resolution_source=grid.get("resolution_source"),
        nx=grid.get("nx"), ny=grid.get("ny"), cell_count=grid.get("cell_count"),
        mapping_method=grid.get("mapping_method"),
        local_to_geographic=frame.get("local_to_geographic"),
        semantics="corridor_local_metric_frame_with_explicit_resolution_provenance",
    )
    if not frame.get("horizontal_crs"):
        _downgrade(entry, "pending")
        entry["reasons"].append("未声明 local metric CRS（horizontal_crs）")
    if not frame.get("metric_bounds"):
        entry["status"] = "blocked"
        entry["reasons"].append("缺少 corridor metric bounds：不得在没有窗口的情况下构造 fine grid")
    if grid.get("resolution_m") is None:
        entry["status"] = "blocked"
        entry["reasons"].append(
            "缺少显式 horizontal resolution：不得把 30 m 写成安全常数，"
            "必须来自 explicit_configuration 或 DTM 有效分辨率"
        )
    if grid.get("resolution_source") not in ("explicit_configuration", "dtm_effective_resolution"):
        entry["status"] = "blocked"
        entry["reasons"].append("resolution_source 不可追溯")
    if not grid.get("cell_count"):
        entry["status"] = "blocked"
        entry["reasons"].append("fine grid 没有 cell")
    return entry


def _fine_airspace(cells):
    counts = {"confirmed_allowed": 0, "confirmed_restricted": 0, "unknown": 0}
    unconfirmed = 0
    for cell in cells:
        airspace = cell.get("airspace") or {}
        status = str(airspace.get("status") or "unknown")
        counts[status if status in counts else "unknown"] += 1
        if not airspace.get("policy_confirmed"):
            unconfirmed += 1
    entry = _entry(
        "airspace", "ready", [], status_counts=counts, cell_count=len(cells),
        unconfirmed_policy_cell_count=unconfirmed,
        semantics="only_confirmed_allowed_fine_cells_are_feasible_never_inferred_from_name_or_color",
    )
    if not cells:
        entry["status"] = "blocked"
        entry["reasons"].append("fine environment 为空，没有可判定单元")
    elif counts["confirmed_allowed"] == 0:
        entry["status"] = "blocked"
        entry["reasons"].append("没有任何 confirmed allowed fine cell；unknown 一律 fail-closed")
    if counts["unknown"]:
        entry["reasons"].append(f"{counts['unknown']} 个 fine cell 空域状态为 unknown，搜索中不可行")
    return entry


def _fine_terrain(cells, properties):
    unresolved = sorted(
        str(cell["fine_cell_id"]) for cell in cells
        if (cell.get("terrain") or {}).get("data_status") != "passed"
        or (cell.get("terrain") or {}).get("surface_clearance_egm2008_m") is None
    )
    entry = _entry(
        "terrain", "ready", [], cell_count=len(cells),
        unresolved_cell_count=len(unresolved), unresolved_samples=unresolved[:20],
        sampling=TERRAIN_SAMPLING_METHOD,
        semantics="intersecting_valid_pixel_max_egm2008_plus_explicit_clearance_fail_closed_on_nodata",
    )
    if not cells:
        entry["status"] = "blocked"
        entry["reasons"].append("没有 fine cell，无法判定 terrain clearance")
        return entry
    if unresolved:
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"{len(unresolved)} 个 fine cell 的 DTM 证据 unknown/NoData：unknown 一律不可行"
        )
    if properties.get("terrain_clearance_m") is None:
        _downgrade(entry, "pending")
        entry["reasons"].append("fine environment 未声明 terrain_clearance_m")
    return entry


def _fine_building(cells, properties):
    unknown = sorted(
        str(cell["fine_cell_id"]) for cell in cells
        if (cell.get("buildings") or {}).get("data_status") != "passed"
    )
    constrained = sorted(
        str(cell["fine_cell_id"]) for cell in cells
        if (cell.get("buildings") or {}).get("required_clearance_egm2008_m") is not None
    )
    entry = _entry(
        "building", "ready", [], cell_count=len(cells),
        unknown_cell_count=len(unknown), unknown_samples=unknown[:20],
        constrained_cell_count=len(constrained),
        confirmed_absence_cell_count=len(cells) - len(unknown) - len(constrained),
        semantics=(
            "footprint_buffered_by_explicit_horizontal_clearance_then_max_ground_height_"
            "vertical_clearance; unknown_height_blocks; conservative_envelope_not_exact_polygon"
        ),
    )
    if not cells:
        entry["status"] = "blocked"
        entry["reasons"].append("没有 fine cell，无法判定 building clearance")
        return entry
    if unknown:
        entry["status"] = "blocked"
        entry["reasons"].append(
            f"{len(unknown)} 个 fine cell 的 building 证据 unknown（缺 height 或 DTM）：unknown 一律不可行"
        )
    if properties.get("building_horizontal_clearance_m") is None or properties.get("building_vertical_clearance_m") is None:
        _downgrade(entry, "pending")
        entry["reasons"].append("fine environment 未声明 building horizontal/vertical clearance")
    return entry


def _fingerprint_domain(problem):
    entry = _entry(
        "refinement_fingerprint", "ready", [],
        refinement_fingerprint=problem.get("refinement_fingerprint"),
        components=dict(problem.get("fingerprint_components") or {}),
        semantics="refinement_fingerprint_covers_strategic_corridor_policy_source_frame_and_grid",
    )
    expected = problem.get("expected_refinement_fingerprint")
    if expected is not None and expected != problem.get("refinement_fingerprint"):
        entry["status"] = "blocked"
        entry["reasons"].append(
            "expected_refinement_fingerprint 与当前 strategic/corridor/policy/source/frame/grid "
            "不一致：refinement 已 stale，必须重跑"
        )
    return entry


# --------------------------------------------------------------------------- planner


class V3RefinementPlanner:
    """Deterministic corridor-local fine refinement with a hard-constraint kernel."""

    algorithm_id = FINE_ALGORITHM_ID
    algorithm_version = FINE_ALGORITHM_VERSION
    model_scope = FINE_MODEL_SCOPE
    uses_corridor_local_fine_grid = True
    turn_model = REFINEMENT_TURN_MODEL

    def plan(self, problem):
        started = time.perf_counter()
        normalized = normalize_v3_refinement_problem(problem)
        normalized["fingerprint_components"] = refinement_fingerprint_components(normalized)
        readiness = evaluate_refinement_readiness(normalized)
        overall = readiness_overall(readiness)
        fingerprint = normalized["refinement_fingerprint"]
        if overall != "ready":
            environment = normalized.get("environment") or {}
            missing = environment.get("status") == "missing_data" or not environment.get("cells")
            return _finalize(_blocked_result(
                normalized, readiness, fingerprint, _readiness_reason(overall, readiness),
                status="missing_data" if missing else "not_ready",
            ), started)

        policy = normalized["policy"]
        cells = normalized["environment"]["cells"]
        kinematics = kinematic_readiness(policy, normalized["aircraft_motion_limits"])
        try:
            cost_model = SoftCostModel(policy["cost_model"])
        except ValueError as exc:
            readiness["cost_model"]["status"] = "blocked"
            readiness["cost_model"]["reasons"].append(str(exc))
            return _finalize(_blocked_result(normalized, readiness, fingerprint, str(exc)), started)
        unresolved = unresolved_soft_channel_cells(cells, policy, id_field="fine_cell_id")
        if any(unresolved.values()):
            reason = (
                "启用 soft channel 在 fine cell 上缺少带 provenance 的 normalized index，"
                "coarse→fine 只做 upsampled_without_new_information 的复制："
                + ", ".join(f"{channel}({len(ids)})" for channel, ids in sorted(unresolved.items()) if ids)
            )
            readiness["cost_model"]["status"] = "blocked"
            readiness["cost_model"]["reasons"].append(reason)
            return _finalize(_blocked_result(normalized, readiness, fingerprint, reason), started)

        search = _RefinementSearch(
            normalized, kinematics=kinematics, cost_model=cost_model, readiness=readiness,
        )
        endpoint_problem = search.endpoint_problem
        if endpoint_problem is not None:
            return _finalize(_blocked_result(
                normalized, readiness, fingerprint, endpoint_problem[1],
                status=endpoint_problem[0],
            ), started)

        outcome = search.run()
        if search.cap_reached:
            return _finalize(_resource_limited_result(
                normalized, readiness, fingerprint, outcome, search,
            ), started)
        if outcome is None:
            status = "missing_data" if search.unknown_blocked else "failed"
            result = _blocked_result(
                normalized, readiness, fingerprint,
                "在 corridor-local fine grid 的显式硬约束与运动能力下未找到可行精化路径",
                status=status,
            )
            result["search_statistics"].update(search.statistics())
            result["hard_constraint_summary"] = _hard_summary(search, readiness)
            return _finalize(result, started)
        return _finalize(_candidate_result(
            normalized, readiness, fingerprint, outcome, search,
        ), started)


def _components(problem):
    """Kept for callers that want the fingerprint components of a raw problem."""

    return refinement_fingerprint_components(problem)


class _RefinementSearch:
    def __init__(self, problem, *, kinematics, cost_model, readiness):
        self.problem = problem
        self.policy = problem["policy"]
        self.environment = problem["environment"]
        self.cells = list(self.environment["cells"])
        self.cell_by_id = {str(cell["fine_cell_id"]): cell for cell in self.cells}
        self.cost_model = cost_model
        self.readiness = readiness
        self.bin_count = int(problem["heading_bin_count"])
        self.altitude_states = _refinement_altitude_states(problem)
        self.altitudes = [item["altitude_egm2008_m"] for item in self.altitude_states]
        self.provider = FinePrimitiveProvider(
            self.cells, self.bin_count, problem.get("max_stride_cells") or 1,
        )
        self.validator = TransitionValidator(
            self.policy.get("aircraft_min_turn_radius_m"),
            kinematics["climb_gradient"], kinematics["descent_gradient"],
            heading_step_deg=self.provider.heading_step_deg,
        )
        self.state_audit = HardConstraintAudit()
        self.transition_audit = HardConstraintAudit()
        self.traversed_audit = HardConstraintAudit()
        self.expanded_states = 0
        self.generated_states = 0
        self.expanded_transitions = 0
        self.primitive_checks = 0
        self.traversed_cell_checks = 0
        self.unknown_blocked = False
        self.cap_reached = False
        self.costs = {}
        self.parents = {}
        self._state_cache = {}
        self.soft_index = {
            channel: {
                fine_cell_id: (((cell.get("soft_fields") or {}).get(channel) or {}).get("normalized_index"))
                for fine_cell_id, cell in self.cell_by_id.items()
            }
            for channel in MAPPED_SOFT_CHANNELS
        }
        self.endpoint_problem = self._resolve_endpoints()

    # ------------------------------------------------------------------ endpoints

    def _resolve_endpoints(self):
        strategic = self.problem["strategic_candidate"]
        if not self.altitudes:
            return "not_ready", "fine refinement 高度离散为空：缺少显式 altitude band 或 vertical_step_m"
        self.goal_altitude_indices = (
            frozenset({self._nearest_altitude(strategic.get("goal_altitude_egm2008_m"))})
            if strategic.get("explicit_goal_altitude")
            else frozenset(range(len(self.altitudes)))
        )
        start_cell, start_method = self._bind(
            strategic.get("start_fine_cell_id"), strategic.get("start_metric"),
            strategic.get("start_point"),
        )
        goal_cell, goal_method = self._bind(
            strategic.get("goal_fine_cell_id"), strategic.get("goal_metric"),
            strategic.get("goal_point"),
        )
        if start_cell is None or goal_cell is None:
            return "not_ready", (
                "无法把 V3-A 战略候选端点绑定到 corridor-local fine cell："
                "需要显式 fine cell id、metric 坐标或经纬度端点"
            )
        self.start_grid, self.goal_grid = start_cell, goal_cell
        self.start_altitude = self._nearest_altitude(strategic.get("start_altitude_egm2008_m"))
        self.goal_altitude = self._nearest_altitude(strategic.get("goal_altitude_egm2008_m"))
        self.endpoint_binding = {
            "start": {"fine_cell_id": start_cell, "method": start_method},
            "goal": {"fine_cell_id": goal_cell, "method": goal_method},
            "semantics": "endpoint_binding_is_recorded_not_assumed",
        }
        return None

    def _bind(self, explicit_cell_id, metric_point, geographic_point):
        if explicit_cell_id and explicit_cell_id in self.cell_by_id:
            return explicit_cell_id, "explicit_fine_cell_id"
        if metric_point is not None:
            return self._nearest_by(metric_point, "center_metric"), "nearest_center_metric"
        if geographic_point is not None:
            candidates = [
                cell for cell in self.cells if cell.get("center") is not None
            ]
            if candidates:
                best, best_distance = None, None
                for cell in candidates:
                    value = geodesic_distance_m(
                        list(geographic_point), list(cell["center"]),
                    )
                    if best_distance is None or value < best_distance - 1e-9 or (
                        isclose(value, best_distance, abs_tol=1e-9)
                        and (best is None or cell["fine_cell_id"] < best)
                    ):
                        best, best_distance = str(cell["fine_cell_id"]), value
                if best is not None:
                    return best, "nearest_geographic_center_metric_grid"
        ordered = sorted(
            self.cell_by_id,
            key=lambda cell_id: (
                self.cell_by_id[cell_id].get("row") or 0,
                self.cell_by_id[cell_id].get("column") or 0,
                cell_id,
            ),
        )
        return (ordered[0] if ordered else None), "deterministic_first_cell_fallback"

    def _nearest_by(self, point, field):
        best, best_distance = None, None
        for cell in self.cells:
            center = cell.get(field)
            if not center:
                continue
            value = hypot(center[0] - float(point[0]), center[1] - float(point[1]))
            if best_distance is None or value < best_distance - 1e-9 or (
                isclose(value, best_distance, abs_tol=1e-9)
                and (best is None or cell["fine_cell_id"] < best)
            ):
                best, best_distance = str(cell["fine_cell_id"]), value
        return best

    def _nearest_altitude(self, target):
        if not self.altitudes:
            return 0
        if target is None:
            return 0
        return min(
            range(len(self.altitudes)),
            key=lambda index: (abs(self.altitudes[index] - float(target)), index),
        )

    # ------------------------------------------------------------------ statistics

    def statistics(self):
        complete = not self.cap_reached and self.expanded_states > 0
        limit = self.policy["max_expanded_states"]
        return {
            "expanded_states": self.expanded_states,
            "generated_states": self.generated_states,
            "expanded_transitions": self.expanded_transitions,
            "primitive_checks": self.primitive_checks,
            "traversed_cell_checks": self.traversed_cell_checks,
            "max_stride_cells": self.provider.max_stride_cells,
            "expansion_cap": limit,
            "expansion_cap_reached": self.cap_reached,
            "search_complete": bool(complete),
            "search_completeness": (
                SEARCH_COMPLETENESS["search_incomplete_resource_limited"] if self.cap_reached
                else SEARCH_COMPLETENESS["complete"] if complete
                else SEARCH_COMPLETENESS["not_run"]
            ),
            "resource_limited": bool(self.cap_reached),
            "resource_limit": "max_expanded_states" if self.cap_reached else None,
            "resource_limit_reason": (
                f"达到 policy.max_expanded_states={limit}：细网格搜索预算耗尽，"
                "未证明不可行，也未证明最优"
                if self.cap_reached else None
            ),
            "state_space_shape": {
                "fine_cells": len(self.cell_by_id),
                "altitude_levels": len(self.altitudes),
                "heading_bins": self.bin_count,
                "naive_state_count": len(self.cell_by_id) * len(self.altitudes) * self.bin_count,
            },
            "endpoint_binding": getattr(self, "endpoint_binding", None),
        }

    # ------------------------------------------------------------------ search

    def _state(self, fine_cell_id, altitude_index, heading_bin):
        key = (fine_cell_id, altitude_index, heading_bin)
        cached = self._state_cache.get(key)
        if cached is None:
            altitude = self.altitudes[altitude_index]
            center = self.cell_by_id[fine_cell_id].get("center_metric") or [0.0, 0.0]
            state = build_v3_state(fine_cell_id, altitude_index, altitude, heading_bin, center[0], center[1])
            state["center_geographic"] = self.cell_by_id[fine_cell_id].get("center")
            cached = (state, key)
            self._state_cache[key] = cached
        return cached

    def _heuristic(self, fine_cell_id, altitude_index):
        goal = self.cell_by_id[self.goal_grid].get("center_metric") or [0.0, 0.0]
        center = self.cell_by_id[fine_cell_id].get("center_metric") or [0.0, 0.0]
        horizontal = hypot(goal[0] - center[0], goal[1] - center[1])
        vertical = abs(self.altitudes[altitude_index] - self.altitudes[self.goal_altitude])
        return (horizontal ** 2 + vertical ** 2) ** 0.5

    def run(self):
        counter = 0
        queue = []
        for heading_bin in range(self.bin_count):
            state = (self.start_grid, self.start_altitude, heading_bin)
            self.costs[state] = 0.0
            heappush(queue, (self._heuristic(state[0], state[1]), 0.0, -heading_bin, state))
        cap = int(self.policy["max_expanded_states"])
        while queue:
            _, current_cost, _, current = heappop(queue)
            if current_cost > self.costs.get(current, inf) + 1e-9:
                continue
            if current[0] == self.goal_grid and current[1] in self.goal_altitude_indices:
                return self._reconstruct(current)
            if self.expanded_states >= cap:
                self.cap_reached = True
                return self._best_known_goal()
            self.expanded_states += 1
            for record in self._expand(current):
                neighbour = record["target"]
                candidate = current_cost + record["scalar_cost"]
                known = self.costs.get(neighbour)
                if known is not None and candidate >= known - 1e-9:
                    if isclose(candidate, known, abs_tol=1e-9):
                        self._tiebreak(current, neighbour, record)
                    continue
                self.costs[neighbour] = candidate
                self.parents[neighbour] = (current, record)
                counter += 1
                heappush(queue, (
                    candidate + self._heuristic(neighbour[0], neighbour[1]),
                    candidate, counter, neighbour,
                ))
                self.generated_states += 1
        return None

    def _best_known_goal(self):
        candidates = [
            (cost, state) for state, cost in self.costs.items()
            if state[0] == self.goal_grid and state[1] in self.goal_altitude_indices
        ]
        if not candidates:
            return None
        cost, state = min(candidates, key=lambda item: (item[0], _state_order(item[1])))
        return self._reconstruct(state)

    def _tiebreak(self, current, neighbour, record):
        existing = self.parents.get(neighbour)
        if existing is None:
            return
        if _state_order(current) < _state_order(existing[0]):
            self.parents[neighbour] = (current, record)

    def _expand(self, state):
        fine_cell_id, altitude_index, heading_bin = state
        records = []
        for primitive in self.provider.for_state(fine_cell_id):
            self.expanded_transitions += 1
            self.primitive_checks += 1
            target_altitude_index = altitude_index + int(primitive["altitude_delta_steps"])
            if target_altitude_index < 0 or target_altitude_index >= len(self.altitudes):
                self.transition_audit.record_transition(
                    "altitude_band_exceeded", (fine_cell_id, altitude_index, primitive["primitive_id"]),
                )
                continue
            target_cell = primitive["traversed_cell_ids"][-1]
            target_altitude = self.altitudes[target_altitude_index]
            source_altitude = self.altitudes[altitude_index]
            heading_change = abs(normalize_heading_delta(
                primitive["bearing_deg"] - heading_bin_center_deg(heading_bin, self.bin_count),
            ))
            vertical_step = abs(target_altitude - source_altitude)
            feasible, reason = self.validator.evaluate(
                horizontal_step_m=primitive["stride_m"], vertical_step_m=vertical_step,
                heading_change_deg=heading_change, kind=primitive["kind"],
            )
            if not feasible:
                self.transition_audit.record_transition(
                    reason, (fine_cell_id, altitude_index, primitive["primitive_id"]),
                )
                continue
            target_feasible, target_reason = _state_feasible(
                self.cell_by_id, fine_cell_id=target_cell, altitude=target_altitude,
            )
            if not target_feasible:
                self.state_audit.record_state(
                    target_reason, (target_cell, target_altitude_index, heading_bin),
                )
                if target_reason in (
                    "terrain_clearance_unresolved", "building_clearance_unresolved", "airspace_unknown",
                ):
                    self.unknown_blocked = True
                continue
            traversed_ok, traversed_reason, traversed_cell = self._check_traversed(
                primitive, source_altitude, target_altitude,
            )
            if not traversed_ok:
                self.traversed_audit.record_state(
                    traversed_reason,
                    (traversed_cell, altitude_index, primitive["primitive_id"]),
                )
                if traversed_reason in (
                    "traversed_terrain_clearance_unresolved",
                    "traversed_building_clearance_unresolved",
                    "traversed_airspace_unknown",
                ):
                    self.unknown_blocked = True
                continue
            target_heading_bin = heading_bin_for_bearing(primitive["bearing_deg"], self.bin_count)
            target_state = (target_cell, target_altitude_index, target_heading_bin)
            records.append(self._edge_record(
                state, primitive, target_state, heading_change, vertical_step, target_cell,
            ))
        return records

    def _check_traversed(self, primitive, source_altitude, target_altitude):
        """Every traversed cell must be clear at the interpolated path altitude.

        The primitive records the fraction at which the path *enters* each cell;
        the altitude there is ``z_source + (z_target - z_source) * fraction``.
        Because each cell's exit is the next cell's entry, checking entries plus
        the target state covers the whole segment.
        """

        ids = primitive["traversed_cell_ids"]
        fractions = primitive["traversed_entry_fractions"]
        envelope = (self.problem.get("corridor") or {}).get("altitude_envelope") or {}
        lower = envelope.get("lower_altitude_egm2008_m")
        upper = envelope.get("upper_altitude_egm2008_m")
        for cell_id, fraction in zip(ids, fractions):
            cell = self.cell_by_id.get(cell_id)
            self.traversed_cell_checks += 1
            if cell is None:
                return False, "traversed_cell_not_in_environment", cell_id
            altitude = float(source_altitude) + (float(target_altitude) - float(source_altitude)) * float(fraction)
            if lower is not None and altitude < float(lower) - _COMPARISON_TOLERANCE:
                return False, "traversed_cell_outside_corridor_altitude_envelope", cell_id
            if upper is not None and altitude > float(upper) + _COMPARISON_TOLERANCE:
                return False, "traversed_cell_outside_corridor_altitude_envelope", cell_id
            airspace = str((cell.get("airspace") or {}).get("status") or "unknown")
            if airspace == "unknown":
                return False, "traversed_airspace_unknown", cell_id
            if airspace != "confirmed_allowed":
                return False, "traversed_airspace_not_confirmed_allowed", cell_id
            terrain = cell.get("terrain") or {}
            floor = terrain.get("surface_clearance_egm2008_m")
            if terrain.get("data_status") != "passed" or floor is None:
                return False, "traversed_terrain_clearance_unresolved", cell_id
            if altitude < float(floor) - _COMPARISON_TOLERANCE:
                return False, "traversed_below_terrain_clearance", cell_id
            buildings = cell.get("buildings") or {}
            if buildings.get("data_status") != "passed":
                return False, "traversed_building_clearance_unresolved", cell_id
            required = buildings.get("required_clearance_egm2008_m")
            if required is not None and altitude < float(required) - _COMPARISON_TOLERANCE:
                return False, "traversed_below_building_clearance", cell_id
        return True, None, None

    def _edge_record(self, source_state, primitive, target_state, heading_change, vertical_step,
                     target_cell):
        source_cell, source_altitude_index, _ = source_state
        target_altitude_index = target_state[1]
        length = float(primitive["stride_m"])
        channel_indices = {}
        for channel in self.cost_model.enabled_penalty_channels:
            if channel == "building_exposure":
                pair = (
                    building_exposure_normalized(
                        self.altitudes[source_altitude_index], self._required_clearance(source_cell),
                    ),
                    building_exposure_normalized(
                        self.altitudes[target_altitude_index], self._required_clearance(target_cell),
                    ),
                )
            else:
                pair = (self._soft_index(channel, source_cell), self._soft_index(channel, target_cell))
            channel_indices[channel] = pair
        penalty = self.cost_model.edge_penalty(length, channel_indices)
        return {
            "target": target_state,
            "primitive_id": primitive["primitive_id"],
            "kind": primitive["kind"],
            "stride_cells": primitive["stride_cells"],
            "length_m": round(length, 9),
            "climb_gradient": round(vertical_step / length, 12) if length > 0 else None,
            "heading_change_deg": round(heading_change, 9),
            "turn_arc_required_m": (
                None if self.policy.get("aircraft_min_turn_radius_m") is None
                else round(float(self.policy["aircraft_min_turn_radius_m"]) * radians(heading_change), 9)
            ),
            "turn_arc_available_m": round(length, 9),
            "altitude_change_m": round(
                self.altitudes[target_altitude_index] - self.altitudes[source_altitude_index], 9,
            ),
            "traversed_cell_ids": list(primitive["traversed_cell_ids"]),
            "traversed_entry_fractions": list(primitive["traversed_entry_fractions"]),
            "soft_penalty": round(penalty, 12),
            "scalar_cost": round(self.cost_model.edge_scalar_cost(length, penalty), 9),
            "channel_indices": channel_indices,
        }

    def _soft_index(self, channel, fine_cell_id):
        value = self.soft_index.get(channel, {}).get(fine_cell_id)
        if value is None:
            raise ValueError(
                f"soft channel {channel} 在 fine cell {fine_cell_id} 缺少 provenance normalized index"
            )
        return float(value)

    def _required_clearance(self, fine_cell_id):
        return (self.cell_by_id[fine_cell_id].get("buildings") or {}).get("required_clearance_egm2008_m")

    def _reconstruct(self, goal_state):
        states, edges = [goal_state], []
        current = goal_state
        while current in self.parents:
            parent, record = self.parents[current]
            edges.append(record)
            states.append(parent)
            current = parent
        states.reverse()
        edges.reverse()
        return {"states": states, "edges": edges, "scalar_cost": self.costs[goal_state]}


def _refinement_altitude_states(problem):
    """Altitude discretisation restricted to the corridor's own altitude envelope."""

    policy = problem["policy"]
    states = describe_altitude_states(policy)
    envelope = (problem.get("corridor") or {}).get("altitude_envelope") or {}
    lower = envelope.get("lower_altitude_egm2008_m")
    upper = envelope.get("upper_altitude_egm2008_m")
    if lower is None and upper is None:
        return states
    result = []
    for index, item in enumerate(states):
        altitude = item["altitude_egm2008_m"]
        if lower is not None and altitude < float(lower) - 1e-9:
            continue
        if upper is not None and altitude > float(upper) + 1e-9:
            continue
        result.append({**item, "altitude_index": len(result)})
    return result


def _state_feasible(cell_by_id, *, fine_cell_id, altitude):
    cell = cell_by_id.get(fine_cell_id)
    if cell is None:
        return False, "cell_not_in_environment"
    airspace = str((cell.get("airspace") or {}).get("status") or "unknown")
    if airspace == "unknown":
        return False, "airspace_unknown"
    if airspace != "confirmed_allowed":
        return False, "airspace_not_confirmed_allowed"
    terrain = cell.get("terrain") or {}
    floor = terrain.get("surface_clearance_egm2008_m")
    if terrain.get("data_status") != "passed" or floor is None:
        return False, "terrain_clearance_unresolved"
    if altitude < float(floor) - 1e-9:
        return False, "below_terrain_clearance"
    buildings = cell.get("buildings") or {}
    if buildings.get("data_status") != "passed":
        return False, "building_clearance_unresolved"
    required = buildings.get("required_clearance_egm2008_m")
    if required is not None and altitude < float(required) - 1e-9:
        return False, "below_building_clearance"
    return True, None


# --------------------------------------------------------------------------- results


def _hard_summary(search, readiness):
    state = search.state_audit.summary(
        expanded_states=search.expanded_states,
        expanded_transitions=search.expanded_transitions,
        generated_states=search.generated_states,
    )
    transition = search.transition_audit.summary(
        expanded_states=search.expanded_states,
        expanded_transitions=search.expanded_transitions,
        generated_states=search.generated_states,
    )
    traversed = search.traversed_audit.summary(
        expanded_states=search.expanded_states,
        expanded_transitions=search.expanded_transitions,
        generated_states=search.generated_states,
    )
    return {
        "state_rejections": state["state_rejections"],
        "transition_rejections": transition["transition_rejections"],
        "traversed_cell_rejections": traversed["state_rejections"],
        "total_state_rejections": state["total_state_rejections"],
        "total_transition_rejections": transition["total_transition_rejections"],
        "total_traversed_cell_rejections": traversed["total_state_rejections"],
        "expanded_states": search.expanded_states,
        "expanded_transitions": search.expanded_transitions,
        "generated_states": search.generated_states,
        "primitive_checks": search.primitive_checks,
        "traversed_cell_checks": search.traversed_cell_checks,
        "audit_semantics": "distinct_rejection_counts_with_stable_reason_ids",
        "audit_sample_cap_per_reason": 200,
        "unknown_is_never_feasible": True,
        "intermediate_obstacles_cannot_be_skipped_by_a_stride": True,
        "readiness_cost_model": (readiness or {}).get("cost_model", {}).get("status"),
    }


def _blocked_result(problem, readiness, fingerprint, reason, *, status="not_ready"):
    result = empty_v3_refinement_result(status)
    result.update({
        "problem_id": problem["problem_id"],
        "route_id": problem.get("route_id"),
        "reason": reason,
        "readiness": readiness,
        "refinement_fingerprint": fingerprint,
        "fingerprint_components": dict(problem.get("fingerprint_components") or {}),
        "strategic_fingerprint": (problem.get("strategic_candidate") or {}).get("strategic_fingerprint"),
        "corridor_id": (problem.get("corridor") or {}).get("corridor_id"),
        "frame": problem.get("frame"),
        "fine_grid": problem.get("fine_grid"),
        "source_audit": dict(problem.get("source_audit") or {}),
        "effective_policy": problem.get("policy"),
        "motion_model": _motion_model(problem),
        "fine_grid_evidence": _fine_evidence(problem),
    })
    result["search_statistics"]["expansion_cap"] = problem["policy"]["max_expanded_states"]
    result["search_statistics"]["search_completeness"] = SEARCH_COMPLETENESS["not_run"]
    return result


def _candidate_result(problem, readiness, fingerprint, outcome, search, *, status="refined_candidate",
                      reason=None, optimality_proven=True):
    states = outcome["states"]
    edges = outcome["edges"]
    state_path = []
    for index, (fine_cell_id, altitude_index, heading_bin) in enumerate(states):
        state, _ = search._state(fine_cell_id, altitude_index, heading_bin)
        state_path.append({
            "index": index,
            "fine_cell_id": fine_cell_id,
            "parent_grid_id": search.cell_by_id[fine_cell_id].get("parent_grid_id"),
            "altitude_index": altitude_index,
            "altitude_egm2008_m": state["altitude_egm2008_m"],
            "heading_bin": heading_bin,
            "heading_deg": heading_bin_center_deg(heading_bin, search.bin_count),
            "x_metric": state["x"],
            "y_metric": state["y"],
            "x": (state.get("center_geographic") or [None, None])[0],
            "y": (state.get("center_geographic") or [None, None])[1],
            "z": state["z"],
            "vertical_reference": "egm2008_orthometric",
            "primitive_id": edges[index]["primitive_id"] if index < len(edges) else None,
            "kind": edges[index]["kind"] if index < len(edges) else None,
            "stride_cells": edges[index]["stride_cells"] if index < len(edges) else None,
            "length_m": edges[index]["length_m"] if index < len(edges) else None,
            "climb_gradient": edges[index]["climb_gradient"] if index < len(edges) else None,
            "heading_change_deg": edges[index]["heading_change_deg"] if index < len(edges) else None,
            "turn_arc_required_m": edges[index]["turn_arc_required_m"] if index < len(edges) else None,
            "turn_arc_available_m": edges[index]["turn_arc_available_m"] if index < len(edges) else None,
            "traversed_cell_ids": edges[index]["traversed_cell_ids"] if index < len(edges) else [],
            "traversed_entry_fractions": (
                edges[index]["traversed_entry_fractions"] if index < len(edges) else []
            ),
            "soft_penalty": edges[index]["soft_penalty"] if index < len(edges) else None,
            "scalar_cost": edges[index]["scalar_cost"] if index < len(edges) else None,
        })
    distances = [edge["length_m"] for edge in edges]
    used_cells, seen = [], set()
    for fine_cell_id, _, _ in states:
        if fine_cell_id in seen:
            continue
        seen.add(fine_cell_id)
        used_cells.append(search.cell_by_id[fine_cell_id])
    channel_exposures = {}
    for channel in search.cost_model.enabled_penalty_channels:
        channel_exposures[channel] = summarize_channel_exposures(
            channel, edges,
            provenance=soft_channel_provenance(
                used_cells, channel, (search.environment or {}).get("properties"),
            ),
        )
    cost_vector = build_cost_vector(
        distances=distances, channel_exposures=channel_exposures, model=search.cost_model,
    )
    metric_points = [[record["x_metric"], record["y_metric"]] for record in state_path]
    geographic_points = [
        [record["x"], record["y"]] for record in state_path
        if record["x"] is not None and record["y"] is not None
    ]
    result = empty_v3_refinement_result(status)
    result.update({
        "problem_id": problem["problem_id"],
        "route_id": problem.get("route_id"),
        "reason": reason or "在 corridor-local fine grid 上找到满足显式硬约束与运动能力的精化候选",
        "readiness": readiness,
        "refinement_fingerprint": fingerprint,
        "fingerprint_components": dict(problem.get("fingerprint_components") or {}),
        "strategic_fingerprint": (problem.get("strategic_candidate") or {}).get("strategic_fingerprint"),
        "corridor_id": (problem.get("corridor") or {}).get("corridor_id"),
        "frame": problem.get("frame"),
        "fine_grid": problem.get("fine_grid"),
        "source_audit": dict(problem.get("source_audit") or {}),
        "effective_policy": problem.get("policy"),
        "motion_model": _motion_model(problem),
        "state_path": state_path,
        "metric_projection": metric_points,
        "horizontal_projection": geographic_points,
        "distance_m": round(sum(distances), 9),
        "cost_vector": cost_vector,
        "fine_grid_evidence": _fine_evidence(problem),
    })
    result["search_statistics"].update(search.statistics())
    result["search_statistics"]["optimality_proven"] = bool(optimality_proven)
    result["hard_constraint_summary"] = _hard_summary(search, readiness)
    result["trajectory_summary"] = {
        "state_count": len(state_path),
        "edge_count": len(edges),
        "distance_m": result["distance_m"],
        "max_stride_cells": max([edge["stride_cells"] for edge in edges] or [0]),
        "multi_cell_stride_edge_count": sum(1 for edge in edges if edge["stride_cells"] > 1),
        "traversed_cell_check_count": sum(len(edge["traversed_cell_ids"]) for edge in edges),
        "climb_edge_count": sum(1 for edge in edges if edge["kind"] == "climb"),
        "descent_edge_count": sum(1 for edge in edges if edge["kind"] == "descend"),
        "level_edge_count": sum(1 for edge in edges if edge["kind"] == "horizontal_level"),
        "max_heading_change_deg": max([edge["heading_change_deg"] for edge in edges] or [0.0]),
        "max_climb_gradient": max(
            [edge["climb_gradient"] for edge in edges if edge["climb_gradient"] is not None] or [None],
            default=None,
        ),
        "altitude_min_egm2008_m": min(record["altitude_egm2008_m"] for record in state_path),
        "altitude_max_egm2008_m": max(record["altitude_egm2008_m"] for record in state_path),
        "start_fine_cell_id": state_path[0]["fine_cell_id"],
        "goal_fine_cell_id": state_path[-1]["fine_cell_id"],
        "endpoint_binding": search.endpoint_binding,
        "endpoint_binding_semantics": "explicit_or_recorded_binding_not_assumed_membership",
        "turn_model": REFINEMENT_TURN_MODEL,
    }
    return result


def _resource_limited_result(problem, readiness, fingerprint, outcome, search):
    limit = problem["policy"]["max_expanded_states"]
    if outcome is not None:
        result = _candidate_result(
            problem, readiness, fingerprint, outcome, search,
            status="search_incomplete",
            reason=(
                f"达到 max_expanded_states={limit}：已保留当前精化候选，"
                "但未证明最优（搜索预算耗尽，不是 infeasible）"
            ),
            optimality_proven=False,
        )
    else:
        result = _blocked_result(
            problem, readiness, fingerprint,
            (
                f"达到 max_expanded_states={limit}：细网格搜索预算耗尽，"
                "本次运行未找到可行精化候选，但这不构成 infeasible 结论"
            ),
            status="search_incomplete",
        )
        result["search_statistics"].update(search.statistics())
        result["hard_constraint_summary"] = _hard_summary(search, readiness)
    result["search_statistics"]["optimality_proven"] = False
    result["search_statistics"]["infeasibility_proven"] = False
    result["semantics"]["resource_limited_not_infeasible"] = True
    return result


def _motion_model(problem):
    policy = problem.get("policy") or {}
    return {
        "model_id": "engineering_3d_motion_primitives_v3b_corridor_refinement",
        "semantics": REFINEMENT_TURN_MODEL,
        "not_a_flight_dynamics_certification_model": True,
        "multi_cell_stride": True,
        "max_stride_cells": problem.get("max_stride_cells"),
        "min_turn_radius_m": policy.get("aircraft_min_turn_radius_m"),
        "turn_constraint": "minimum_turn_radius_m * |dpsi| <= stride_m",
        "traversed_cells_are_checked": True,
        "altitude_interpolated_along_primitive": True,
        "exact_curvature_validation": "not_implemented_V3-C",
    }


def _fine_evidence(problem):
    environment = problem.get("environment") or {}
    grid = problem.get("fine_grid") or {}
    properties = environment.get("properties") or {}
    return {
        "resolution_m": grid.get("resolution_m"),
        "resolution_source": grid.get("resolution_source"),
        "requested_resolution_m": grid.get("requested_resolution_m"),
        "effective_source_resolution_m": grid.get("effective_source_resolution_m"),
        "resolution_deviation_m": grid.get("resolution_deviation_m"),
        "cell_count": grid.get("cell_count"),
        "environment_cell_count": len(environment.get("cells") or []),
        "corridor_support_cell_count": len(((problem.get("corridor") or {}).get("support_grid_ids")) or []),
        "corridor_center_cell_count": len(((problem.get("corridor") or {}).get("center_grid_ids")) or []),
        "terrain_sampling": TERRAIN_SAMPLING_METHOD,
        "building_mapping": "buffered_footprint_conservative_envelope",
        "horizonal_clearance_m": properties.get("building_horizontal_clearance_m"),
        "vertical_clearance_m": properties.get("building_vertical_clearance_m"),
        "terrain_clearance_m": properties.get("terrain_clearance_m"),
        "source_audit_fingerprint": (environment.get("source_audit") or {}).get("fingerprint"),
        "source_type": environment.get("source_type"),
        "v3c_pending": list(V3C_PENDING),
    }


def _readiness_reason(overall, readiness):
    domains = [(name, value) for name, value in readiness.items() if isinstance(value, dict)]
    blocked = [name for name, value in domains if value.get("status") == "blocked"]
    pending = [name for name, value in domains if value.get("status") == "pending"]
    if blocked:
        return "V3-B readiness blocked：" + ", ".join(sorted(blocked)) + "；证据/参数未准备好时不得硬跑"
    if pending:
        return "V3-B readiness pending：" + ", ".join(sorted(pending)) + "；缺少显式确认"
    return "V3-B readiness 未就绪"


def _state_order(state):
    return (str(state[0]), int(state[1]), int(state[2]))


def _finalize(result, started):
    result["search_statistics"]["runtime_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    result["algorithm_id"] = FINE_ALGORITHM_ID
    result["algorithm_version"] = FINE_ALGORITHM_VERSION
    result["model_scope"] = FINE_MODEL_SCOPE
    result["disclaimer"] = V3B_DISCLAIMER
    result["operational_route"] = False
    result["final_validation_performed"] = False
    result["v3c_validation_pending"] = True
    if result.get("status") not in V3B_RESULT_STATUSES:
        result["status"] = "not_ready"
    semantics = result.setdefault("semantics", {})
    semantics.update({
        "scope": FINE_MODEL_SCOPE,
        "not_final_safe": True,
        "not_validated_operational_route": True,
        "exact_validation_is_v3c": True,
        "unknown_is_never_safe": True,
        "terrain_floor_is_intersecting_pixel_max": True,
        "building_envelope_is_conservative_not_exact": True,
        "airspace_consumes_confirmed_policy_only": True,
        "coarse_soft_fields_are_upsampled_without_new_information": True,
        "turn_model": REFINEMENT_TURN_MODEL,
        "v3c_pending": list(V3C_PENDING),
        "refinement_fingerprint_components": list(REFINEMENT_FINGERPRINT_COMPONENTS),
        "allowed_result_statuses": list(V3B_RESULT_STATUSES),
    })
    return result


__all__ = [
    "SUPERCOVER_BOUNDARY_SEMANTICS", "TRAVERSED_REASONS", "FinePrimitiveProvider",
    "V3RefinementPlanner", "corner_crossing_points", "evaluate_refinement_readiness",
    "grid_bearing_deg", "supercover_line",
]
