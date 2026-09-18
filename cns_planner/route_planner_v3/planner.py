"""L8 horizontal x altitude x heading strategic A* for Route Planner V3-A.

The search state is ``(grid_id, altitude_index, heading_bin)`` and is therefore
Markov with respect to turn capability: the heading is part of the state, so a
turn is constrained while the edge is generated instead of being checked after a
path already exists.

Heuristic (V3-A correctness fix): **``h`` is the plain 3D geometric distance to
the goal**.  Each edge costs at least its own geometric length, because

    edge_scalar_cost = length_m + sum_channels(weight_c * exposure_m_c)

with ``weight_c >= 0`` and ``exposure_m_c = length_m * mean(endpoint index)``
where every index is a provenance-carrying value in ``[0, 1]``.  The previous
``h = 3D distance * (1 + sum(weight))`` was **not** admissible whenever a soft
penalty could be ``0``, and its ``sum(weight) <= 1`` cap was a workaround for an
invented normalizer; both are removed.

Reaching ``max_expanded_states`` is reported as ``search_incomplete`` /
``resource_limited``: the search ran out of budget, which is a statement about
the search and never a proof of infeasibility.  A goal found before the cap is
kept, but flagged as not proven optimal.

Explicit non-goals of V3-A: no corridor-local fine refinement (V3-B), no exact
polygon/terrain final validator (V3-C), no CNS term in the search cost, no energy
model, and no claim that the produced candidate is a final safe or validated
operational route.
"""

from __future__ import annotations

from hashlib import sha256
from heapq import heappop, heappush
import json
import math
import time

from ..benchmark.geodesy import geodesic_distance_m
from .contracts import (
    SEARCH_COMPLETENESS, V3_STRATEGIC_RESULT_DISCLAIMER, build_v3_state,
    describe_altitude_states, empty_v3_strategic_result, heading_bin_center_deg,
    heading_bin_for_bearing, normalize_v3_planning_problem,
)
from .corridor import build_candidate_refinement_corridor
from .cost import (
    MAPPED_SOFT_CHANNELS, SoftCostModel, build_cost_vector,
    building_exposure_normalized, soft_channel_provenance,
    summarize_channel_exposures,
)
from .hard_constraints import (
    HardConstraintAudit, HardConstraintEvaluator, seeded_rejection_summary,
)
from .motion import (
    MOTION_MODEL_ID, MOTION_MODEL_SEMANTICS, MotionPrimitiveProvider,
    TransitionValidator, derive_grid_index, kinematic_readiness,
)
from .readiness import (
    evaluate_v3_readiness, readiness_overall, unresolved_soft_channel_cells,
)

#: Search outcomes that are not a completed search.
OUTCOME_EXHAUSTED = "exhausted"
OUTCOME_CAP_REACHED = "cap_reached"


class V3StrategicPlanner:
    """Deterministic 3D strategic candidate planner with a hard-constraint kernel."""

    algorithm_id = "route_planner_v3_strategic"
    algorithm_version = "3.0-alpha"
    model_scope = "three_dimensional_strategic_planning_v3a"
    uses_v3_native_3d = True

    def plan(self, problem):
        """Plan one 3D strategic candidate from a JSON-safe V3PlanningProblem."""

        started = time.perf_counter()
        normalized = normalize_v3_planning_problem(problem)
        fingerprint = v3_problem_fingerprint(normalized)
        readiness = evaluate_v3_readiness(normalized)
        overall = readiness_overall(readiness)
        if overall != "ready":
            return _finalize(_blocked_result(
                normalized, readiness, overall, fingerprint,
                _readiness_reason(overall, readiness),
            ), started)

        policy = normalized["policy"]
        environment = normalized["environment"]
        kinematics = kinematic_readiness(policy, normalized["aircraft_motion_limits"])
        audit = HardConstraintAudit()
        evaluator = HardConstraintEvaluator(
            environment, policy,
            climb_gradient=kinematics["climb_gradient"],
            descent_gradient=kinematics["descent_gradient"],
        )
        provider = MotionPrimitiveProvider(environment, policy["heading_bin_count"])
        validator = TransitionValidator(
            policy["aircraft_min_turn_radius_m"], kinematics["climb_gradient"],
            kinematics["descent_gradient"], heading_step_deg=provider.heading_step_deg,
        )
        altitude_states = describe_altitude_states(policy)
        altitudes = [item["altitude_egm2008_m"] for item in altitude_states]
        try:
            cost_model = SoftCostModel(policy["cost_model"])
        except ValueError as exc:
            readiness["cost_model"]["status"] = "blocked"
            readiness["cost_model"]["reasons"].append(str(exc))
            return _finalize(_blocked_result(
                normalized, readiness, "blocked", fingerprint, str(exc),
            ), started)

        # Fail closed before the search: an enabled mapped channel needs a
        # provenance normalized index on every cell.  Readiness already reports
        # this; the explicit check keeps the search itself free of any implicit
        # "missing soft field == 0 penalty" fallback.
        unresolved_soft = unresolved_soft_channel_cells(environment.get("cells") or [], policy)
        if any(unresolved_soft.values()):
            reason = (
                "启用 soft channel 缺少带 provenance 的 normalized index，"
                "且 planner 不提供隐式 normalizer：" + ", ".join(
                    f"{channel}({len(ids)})" for channel, ids in sorted(unresolved_soft.items()) if ids
                )
            )
            readiness["cost_model"]["status"] = "blocked"
            readiness["cost_model"]["reasons"].append(reason)
            return _finalize(_blocked_result(
                normalized, readiness, "blocked", fingerprint, reason,
            ), started)

        exit_state = _initial_state(normalized, provider, policy, evaluator, altitude_states, audit)
        if exit_state is not None:
            return _finalize(_blocked_result(
                normalized, readiness, "pending" if exit_state[1] == "missing_data" else "blocked",
                fingerprint, exit_state[2], status=exit_state[0],
                audit=audit, evaluator=evaluator, environment=environment,
            ), started)

        search = _Search(
            normalized=normalized, provider=provider, validator=validator,
            evaluator=evaluator, audit=audit, cost_model=cost_model,
            altitudes=altitudes, policy=policy,
        )
        outcome = search.run()
        if search.outcome_kind == OUTCOME_CAP_REACHED:
            # The expansion cap is a *resource* limit, never a feasibility verdict.
            # Returning ``failed`` here would claim "infeasible", which the search
            # cannot know: it simply stopped early.
            result = _resource_limited_result(
                normalized, readiness, fingerprint, outcome, search, audit, environment,
                evaluator,
            )
            return _finalize(result, started)
        if outcome is None:
            reason = "在显式运动能力与硬约束下未找到 3D 可行战略路径"
            status = "missing_data" if search.unknown_blocked else "failed"
            result = _blocked_result(
                normalized, readiness, "blocked" if status == "failed" else "pending",
                fingerprint, reason, status=status,
                audit=audit, evaluator=evaluator, environment=environment,
            )
            result["search_statistics"].update(search.statistics())
            result["hard_constraint_summary"].update(_hard_summary(
                audit, search, environment, evaluator,
            ))
            return _finalize(result, started)

        result = _candidate_result(
            normalized, readiness, fingerprint, outcome, search, audit, environment, evaluator,
        )
        return _finalize(result, started)


# --------------------------------------------------------------------------- search


class _Search:
    def __init__(self, *, normalized, provider, validator, evaluator, audit, cost_model,
                 altitudes, policy):
        self.problem = normalized
        self.provider = provider
        self.validator = validator
        self.evaluator = evaluator
        self.audit = audit
        self.cost_model = cost_model
        self.altitudes = altitudes
        self.policy = policy
        self.bin_count = int(policy["heading_bin_count"])
        self.step_m = float(policy["vertical_step_m"])
        cells = normalized["environment"]["cells"]
        self.cell_by_id = {str(cell["grid_id"]): cell for cell in cells}
        self.centers = {grid_id: (cell["center"][0], cell["center"][1]) for grid_id, cell in self.cell_by_id.items()}
        self.grid_index = derive_grid_index(self.cell_by_id)
        self.reverse_index = {
            value: grid_id for grid_id, value in self.grid_index.items()
        }
        self.start_grid = self._nearest_query_cell(normalized["start"])
        self.goal_grid = self._nearest_query_cell(normalized["goal"])
        self.start_altitude = self._nearest_altitude(normalized["start"])
        self.goal_altitude = self._nearest_altitude(normalized["goal"])
        self.expanded_states = 0
        self.generated_states = 0
        self.expanded_transitions = 0
        self.unknown_blocked = False
        self.cap_reached = False
        self.outcome_kind = OUTCOME_EXHAUSTED
        self.states = {}
        self.parents = {}
        self.edge_records = {}
        self.costs = {}
        self._state_cache = {}
        # ``h`` is the plain 3D geometric distance: there is no weight-derived
        # scale factor any more (it would be inadmissible whenever a soft penalty
        # can be 0).
        self._heuristic_scale = 1.0
        # Provenance-carrying normalized indices, taken verbatim from the canonical
        # environment.  A ``None`` here is a data gap and is refused rather than
        # treated as zero.
        self.soft_index = {
            channel: {
                grid_id: (((cell.get("soft_fields") or {}).get(channel) or {}).get("normalized_index"))
                for grid_id, cell in self.cell_by_id.items()
            }
            for channel in MAPPED_SOFT_CHANNELS
        }

    # ------------------------------------------------------------------ helpers

    def statistics(self):
        complete = not self.cap_reached and self.expanded_states > 0
        return {
            "expanded_states": self.expanded_states,
            "generated_states": self.generated_states,
            "expanded_transitions": self.expanded_transitions,
            "expansion_cap": self.policy["max_expanded_states"],
            "expansion_cap_reached": self.cap_reached,
            "search_complete": bool(complete),
            "search_completeness": (
                SEARCH_COMPLETENESS["search_incomplete_resource_limited"] if self.cap_reached
                else SEARCH_COMPLETENESS["complete"] if complete
                else SEARCH_COMPLETENESS["not_run"]
            ),
            "resource_limited": bool(self.cap_reached),
            "resource_limit": (
                "max_expanded_states" if self.cap_reached else None
            ),
            "resource_limit_reason": (
                "达到 policy.max_expanded_states="
                f"{self.policy['max_expanded_states']}：搜索预算耗尽，"
                "未证明不可行，也未证明最优"
                if self.cap_reached else None
            ),
            "state_space_shape": {
                "grid_cells": len(self.cell_by_id),
                "altitude_levels": len(self.altitudes),
                "heading_bins": self.bin_count,
                "naive_state_count": len(self.cell_by_id) * len(self.altitudes) * self.bin_count,
            },
        }

    def _nearest_query_cell(self, point):
        best, best_distance = None, None
        for grid_id, center in self.centers.items():
            value = geodesic_distance_m([point["x"], point["y"]], list(center))
            if best_distance is None or value < best_distance - 1e-9 or (
                math.isclose(value, best_distance, abs_tol=1e-9) and (best is None or grid_id < best)
            ):
                best, best_distance = grid_id, value
        return best

    def _nearest_altitude(self, point):
        if not self.altitudes:
            return 0
        target = point.get("z")
        if target is None:
            return 0
        return min(
            range(len(self.altitudes)),
            key=lambda index: (abs(self.altitudes[index] - float(target)), index),
        )

    def _goal_altitude_indices(self):
        """Feasible goal altitudes: one explicit index, or the whole band implicitly."""

        if (self.problem.get("goal") or {}).get("z_is_explicit"):
            return frozenset({self.goal_altitude})
        return frozenset(range(len(self.altitudes)))

    def _state(self, grid_id, altitude_index, heading_bin):
        key = (grid_id, altitude_index, heading_bin)
        cached = self._state_cache.get(key)
        if cached is None:
            altitude = self.altitudes[altitude_index]
            x, y = self.centers[grid_id]
            cached = (
                build_v3_state(grid_id, altitude_index, altitude, heading_bin, x, y), key,
            )
            self._state_cache[key] = cached
        return cached

    def _heuristic(self, grid_id, altitude_index):
        """Plain 3D geometric distance to the goal -- the admissible lower bound.

        No weight-derived inflation: every edge costs at least its geometric
        length, so the straight-line 3D distance never overestimates.
        """

        x, y = self.centers[grid_id]
        goal_x, goal_y = self.centers[self.goal_grid]
        horizontal = geodesic_distance_m([x, y], [goal_x, goal_y])
        vertical = abs(self.altitudes[altitude_index] - self.altitudes[self.goal_altitude])
        return math.hypot(horizontal, vertical) * self._heuristic_scale

    # ------------------------------------------------------------------ main loop

    def run(self):
        goal_grid = self.goal_grid
        goal_altitudes = self._goal_altitude_indices()
        counter = 0
        queue = []
        # The start has no history, so every heading bin is an equally acceptable
        # initial state.  Assuming a single "forward" heading would impose a turn
        # that no input asked for; constraining the heading later is the job of the
        # transition validator.
        for heading_bin in range(self.bin_count):
            state = (self.start_grid, self.start_altitude, heading_bin)
            self.costs[state] = 0.0
            heappush(
                queue,
                (self._heuristic(state[0], state[1]), 0.0, -heading_bin, state),
            )
        cap = int(self.policy["max_expanded_states"])
        while queue:
            _, current_cost, _, current = heappop(queue)
            if current_cost > self.costs.get(current, math.inf) + 1e-9:
                continue
            # The goal is a *cell* (plus an altitude only when the problem demanded an
            # explicit z), not a heading: any arrival heading is accepted, because the
            # heading state exists to constrain turns along the way, not to impose an
            # unstated final-heading requirement.
            if current[0] == goal_grid and current[1] in goal_altitudes:
                return self._reconstruct(current)
            if self.expanded_states >= cap:
                self.cap_reached = True
                self.outcome_kind = OUTCOME_CAP_REACHED
                # A feasible goal state may already have been *generated* but not yet
                # popped.  Keep it as an explicitly non-optimal candidate instead of
                # discarding the whole run.
                return self._best_known_goal(goal_grid, goal_altitudes)
            self.expanded_states += 1
            for record in self._expand(current):
                neighbour = record["target"]
                candidate = current_cost + record["scalar_cost"]
                known = self.costs.get(neighbour)
                if known is not None and candidate >= known - 1e-9:
                    if math.isclose(candidate, known, abs_tol=1e-9):
                        self._deterministic_tiebreak(current, neighbour, candidate, record)
                    continue
                self.costs[neighbour] = candidate
                self.parents[neighbour] = (current, record)
                counter += 1
                heuristic = self._heuristic(neighbour[0], neighbour[1])
                heappush(queue, (candidate + heuristic, candidate, counter, neighbour))
                self.generated_states += 1
        self.outcome_kind = OUTCOME_EXHAUSTED
        return None

    def _best_known_goal(self, goal_grid, goal_altitudes):
        """Cheapest *reachable* goal state found before the cap, or ``None``.

        The value is not proven optimal: the search stopped before the queue was
        exhausted, so a cheaper goal state may still exist.
        """

        candidates = [
            (cost, state) for state, cost in self.costs.items()
            if state[0] == goal_grid and state[1] in goal_altitudes
        ]
        if not candidates:
            return None
        cost, state = min(candidates, key=lambda item: (item[0], _state_order(item[1])))
        return self._reconstruct(state)

    def _deterministic_tiebreak(self, current, neighbour, candidate, record):
        existing = self.parents.get(neighbour)
        if existing is None:
            return
        if _state_order(current) < _state_order(existing[0]):
            self.parents[neighbour] = (current, record)

    def _expand(self, state):
        grid_id, altitude_index, heading_bin = state
        records = []
        for primitive in self.provider.for_state(grid_id, altitude_index):
            self.expanded_transitions += 1
            column, row = primitive["grid_delta"]
            target_altitude_index = altitude_index + int(primitive["altitude_delta_steps"])
            if target_altitude_index < 0 or target_altitude_index >= len(self.altitudes):
                self.audit.record_transition(
                    "altitude_band_exceeded", (grid_id, altitude_index, primitive["primitive_id"]),
                )
                continue
            if column == 0 and row == 0:
                target_grid = grid_id
                step_length = 0.0
                heading_change = 0.0
                target_heading_bin = heading_bin
            else:
                target_grid = self._indexed_neighbour(grid_id, column, row)
                neighbour = target_grid is not None
                if target_grid is None:
                    self.audit.record_transition(
                        "neighbor_not_adjacent", (grid_id, altitude_index, primitive["primitive_id"]),
                    )
                    continue
                step_length = self.provider.step_length_m(grid_id, target_grid)
                bearing = self.provider.bearing_deg(grid_id, target_grid)
                heading_change = abs(_normalized_delta(
                    bearing, heading_bin_center_deg(heading_bin, self.bin_count),
                ))
                target_heading_bin = heading_bin_for_bearing(bearing, self.bin_count)
            vertical_step = abs(self.altitudes[target_altitude_index] - self.altitudes[altitude_index])
            feasible, reason = self.evaluator.transition_feasible(
                source_grid_id=grid_id, target_grid_id=target_grid,
                source_altitude_index=altitude_index, target_altitude_index=target_altitude_index,
                horizontal_step_m=step_length, vertical_step_m=vertical_step,
                heading_change_deg=heading_change, kind=primitive["kind"],
            )
            if not feasible:
                self.audit.record_transition(
                    reason, (grid_id, altitude_index, primitive["primitive_id"]),
                )
                continue
            target_state = (target_grid, target_altitude_index, target_heading_bin)
            target_feasible, target_reason = self.evaluator.state_feasible(
                target_grid, target_altitude_index, self.altitudes[target_altitude_index],
            )
            if not target_feasible:
                self.audit.record_state(
                    target_reason, (target_grid, target_altitude_index, target_heading_bin),
                )
                if target_reason in ("terrain_clearance_unresolved", "building_clearance_unresolved"):
                    self.unknown_blocked = True
                continue
            records.append(self._edge_record(
                state, primitive, target_state, step_length, vertical_step, heading_change, target_grid,
            ))
        return records

    def _indexed_neighbour(self, grid_id, column, row):
        index = self.grid_index.get(grid_id)
        if index is None:
            return None
        level, current_column, current_row = index
        return self.reverse_index.get((level, current_column + column, current_row + row))

    def _edge_record(self, source_state, primitive, target_state, step_length, vertical_step,
                     heading_change, target_grid):
        source_grid, source_altitude, _ = source_state
        target_altitude = target_state[1]
        channel_indices = {}
        for channel in self.cost_model.enabled_penalty_channels:
            if channel == "building_exposure":
                pair = (
                    building_exposure_normalized(
                        self.altitudes[source_altitude], self._required_clearance(source_grid),
                    ),
                    building_exposure_normalized(
                        self.altitudes[target_altitude], self._required_clearance(target_grid),
                    ),
                )
            else:
                pair = (
                    self._soft_index(channel, source_grid),
                    self._soft_index(channel, target_grid),
                )
            channel_indices[channel] = pair
        penalty = self.cost_model.edge_penalty(step_length, channel_indices)
        scalar = self.cost_model.edge_scalar_cost(step_length, penalty)
        return {
            "target": target_state,
            "primitive_id": primitive["primitive_id"],
            "kind": primitive["kind"],
            "length_m": round(step_length, 9),
            "climb_gradient": round(vertical_step / step_length, 12) if step_length > 0 else None,
            "heading_change_deg": round(heading_change, 9),
            "altitude_change_m": round(self.altitudes[target_altitude] - self.altitudes[source_altitude], 9),
            "soft_penalty": round(penalty, 12),
            "scalar_cost": round(scalar, 9),
            "channel_indices": channel_indices,
        }

    def _soft_index(self, channel, grid_id):
        value = self.soft_index.get(channel, {}).get(grid_id)
        if value is None:
            # Unreachable: planner.plan refuses an enabled channel without a
            # provenance index before the search starts.
            raise ValueError(
                f"soft channel {channel} 在 cell {grid_id} 缺少 provenance normalized index；"
                "不得当作 0 penalty"
            )
        return float(value)

    def _required_clearance(self, grid_id):
        return (self.cell_by_id[grid_id].get("buildings") or {}).get("required_clearance_egm2008_m")

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


# --------------------------------------------------------------------------- results


def _initial_state(problem, provider, policy, evaluator, altitude_states, audit):
    """Validate endpoints before searching; returns None when the plan may run.

    An explicit endpoint ``z`` is a hard requirement and is checked here against the
    policy band; an endpoint without ``z`` has no altitude requirement.
    """

    if not altitude_states:
        return "pending_confirmation", "pending", "高度离散为空：缺少显式 min/max altitude 或 vertical_step_m"
    cells = {str(cell["grid_id"]): cell for cell in problem["environment"]["cells"]}
    for name, point in (("start", problem["start"]), ("goal", problem["goal"])):
        if point.get("z_is_explicit"):
            minimum = float(policy["min_altitude_egm2008_m"])
            maximum = float(policy["max_altitude_egm2008_m"])
            if point["z"] < minimum - 1e-9 or point["z"] > maximum + 1e-9:
                return "failed", "blocked", f"{name} 的显式 z 超出 policy 高度范围"
            if provider.cells and _nearest_grid_id(cells, point) is None:
                return "failed", "blocked", f"{name} 不在 canonical 环境中"
    if not cells:
        return "missing_data", "pending", "canonical V3CellEnvironment 为空"
    return None


def _nearest_grid_id(cells, point):
    best, best_distance = None, None
    for grid_id, cell in cells.items():
        center = cell.get("center") or []
        if len(center) < 2:
            continue
        value = geodesic_distance_m([point["x"], point["y"]], [center[0], center[1]])
        if best_distance is None or value < best_distance:
            best, best_distance = grid_id, value
    return best


def _blocked_result(problem, readiness, overall, fingerprint, reason, *, status=None,
                    audit=None, evaluator=None, environment=None):
    # Readiness that is blocked *or* pending means the search never ran, so the
    # result is ``not_ready`` rather than a confirmation-pending evaluation.
    result = empty_v3_strategic_result(status or "not_ready")
    result.update({
        "problem_id": problem["problem_id"],
        "route_id": problem["route_id"] or None,
        "readiness": readiness,
        "input_fingerprint": fingerprint,
        "effective_policy": problem["policy"],
        "reason": reason,
        "heuristic_semantics": _heuristic_semantics(problem, readiness, active=False),
        "cns_assessment": _cns_record(problem),
    })
    result["hard_constraint_summary"].update(_hard_summary(
        audit, None, environment, evaluator,
    ))
    result["search_statistics"]["expansion_cap"] = problem["policy"]["max_expanded_states"]
    result["search_statistics"]["search_completeness"] = SEARCH_COMPLETENESS["not_run"]
    result["search_statistics"]["state_space_shape"] = {
        "grid_cells": len((environment or {}).get("cells") or []),
        "altitude_levels": len(describe_altitude_states(problem["policy"])),
        "heading_bins": problem["policy"]["heading_bin_count"],
        "naive_state_count": (
            len((environment or {}).get("cells") or [])
            * len(describe_altitude_states(problem["policy"]))
            * problem["policy"]["heading_bin_count"]
        ),
    }
    return result


def _candidate_result(problem, readiness, fingerprint, outcome, search, audit, environment,
                      evaluator, *, status="strategic_candidate", reason=None,
                      optimality_proven=True):
    policy = problem["policy"]
    states = outcome["states"]
    edges = outcome["edges"]
    state_path = []
    for index, (grid_id, altitude_index, heading_bin) in enumerate(states):
        state, _ = search._state(grid_id, altitude_index, heading_bin)
        record = {
            "index": index,
            "grid_id": grid_id,
            "altitude_index": altitude_index,
            "altitude_egm2008_m": state["altitude_egm2008_m"],
            "heading_bin": heading_bin,
            "heading_deg": heading_bin_center_deg(heading_bin, search.bin_count),
            "x": state["x"], "y": state["y"], "z": state["z"],
            "vertical_reference": "egm2008_orthometric",
            "primitive_id": edges[index]["primitive_id"] if index < len(edges) else None,
            "length_m": edges[index]["length_m"] if index < len(edges) else None,
            "climb_gradient": edges[index]["climb_gradient"] if index < len(edges) else None,
            "heading_change_deg": edges[index]["heading_change_deg"] if index < len(edges) else None,
            "soft_penalty": edges[index]["soft_penalty"] if index < len(edges) else None,
            "scalar_cost": edges[index]["scalar_cost"] if index < len(edges) else None,
        }
        state_path.append(record)

    distances = [edge["length_m"] for edge in edges]
    # Provenance of the soft channels is aggregated from the cells the candidate
    # actually used, so the reported index scale can be traced back to its source.
    used_cells = []
    seen = set()
    for grid_id, _, _ in states:
        if grid_id in seen:
            continue
        seen.add(grid_id)
        used_cells.append(search.cell_by_id[grid_id])
    channel_exposures = {}
    for channel in search.cost_model.enabled_penalty_channels:
        channel_exposures[channel] = summarize_channel_exposures(
            channel, edges,
            provenance=soft_channel_provenance(
                used_cells, channel, (environment or {}).get("properties"),
            ),
        )
    cost_vector = build_cost_vector(
        distances=distances, channel_exposures=channel_exposures, model=search.cost_model,
    )
    distance_total = round(sum(distances), 9)
    projection = _projection(problem, state_path)
    result = empty_v3_strategic_result(status)
    result.update({
        "problem_id": problem["problem_id"],
        "route_id": problem["route_id"] or None,
        "reason": reason or "在显式硬约束与运动能力下找到 3D 战略候选路径",
        "readiness": readiness,
        "input_fingerprint": fingerprint,
        "effective_policy": policy,
        "heuristic_semantics": _heuristic_semantics(problem, readiness, active=True),
        "state_path": state_path,
        "horizontal_projection": projection,
        "distance_m": distance_total,
        "cost_vector": cost_vector,
        "candidate_refinement_corridor": build_candidate_refinement_corridor(
            state_path, environment,
            ring_n=int((problem.get("provenance") or {}).get("corridor_ring_n") or 0),
            refinement_cell_size_m=(problem.get("provenance") or {}).get("refinement_cell_size_m"),
            explicit_margin_m=(problem.get("provenance") or {}).get("corridor_altitude_margin_m"),
            route_id=problem["route_id"] or None,
        ),
        "cns_assessment": _cns_record(problem),
    })
    result["search_statistics"].update(search.statistics())
    result["search_statistics"]["expanded_states"] = search.expanded_states
    result["search_statistics"]["optimality_proven"] = bool(optimality_proven)
    result["hard_constraint_summary"].update(_hard_summary(
        audit, search, environment, evaluator,
    ))
    result["trajectory_summary"] = {
        "state_count": len(state_path),
        "edge_count": len(edges),
        "distance_m": distance_total,
        "climb_edge_count": sum(1 for edge in edges if edge["kind"] == "climb"),
        "descent_edge_count": sum(1 for edge in edges if edge["kind"] == "descend"),
        "level_edge_count": sum(1 for edge in edges if edge["kind"] == "horizontal_level"),
        "max_climb_gradient": max(
            [edge["climb_gradient"] for edge in edges if edge["climb_gradient"] is not None] or [None],
            default=None,
        ),
        "max_heading_change_deg": max([edge["heading_change_deg"] for edge in edges] or [0.0]),
        "altitude_min_egm2008_m": min(record["altitude_egm2008_m"] for record in state_path),
        "altitude_max_egm2008_m": max(record["altitude_egm2008_m"] for record in state_path),
        "start_grid_id": state_path[0]["grid_id"],
        "goal_grid_id": state_path[-1]["grid_id"],
        "goal_altitude_egm2008_m": state_path[-1]["altitude_egm2008_m"],
        "endpoint_grid_binding": "nearest_search_cell_center",
        "endpoint_binding_semantics": "strategic_grid_binding_not_exact_polygon_membership",
        "endpoint_altitude_semantics": (
            "problem.goal.z 为显式硬约束" if (problem.get("goal") or {}).get("z_is_explicit")
            else "problem.goal 未指定 z：终点不设高度要求，搜索取可行高度中的最小代价高度"
        ),
    }
    return result


def _resource_limited_result(problem, readiness, fingerprint, outcome, search, audit,
                             environment, evaluator):
    """``search_incomplete``: the expansion cap stopped the search.

    Two honest sub-cases are distinguished:

    * a feasible goal state was already generated -> it is reported as a
      *candidate path* whose optimality is explicitly **not** proven;
    * nothing reachable was found -> the result carries no path and states
      clearly that infeasibility is **not** proven either.
    """

    statistics = search.statistics()
    limit = problem["policy"]["max_expanded_states"]
    if outcome is not None:
        result = _candidate_result(
            problem, readiness, fingerprint, outcome, search, audit, environment, evaluator,
            status="search_incomplete",
            reason=(
                f"达到 max_expanded_states={limit}：已保留当前可行候选，"
                "但未证明最优（搜索预算耗尽，不是 infeasible）"
            ),
            optimality_proven=False,
        )
    else:
        result = _blocked_result(
            problem, readiness, "pending", fingerprint,
            (
                f"达到 max_expanded_states={limit}：搜索预算耗尽，"
                "本次运行未找到可行候选，但这不构成 infeasible 结论；"
                "请提高 max_expanded_states 或缩小问题规模后重跑"
            ),
            status="search_incomplete", audit=audit, evaluator=evaluator,
            environment=environment,
        )
        result["search_statistics"].update(statistics)
        result["hard_constraint_summary"].update(_hard_summary(
            audit, search, environment, evaluator,
        ))
    result["search_statistics"]["optimality_proven"] = False
    result["search_statistics"]["infeasibility_proven"] = False
    result["semantics"]["resource_limited_not_infeasible"] = True
    return result


def _hard_summary(audit, search, environment, evaluator):
    if audit is None:
        return {}
    summary = audit.summary(
        expanded_states=search.expanded_states if search else 0,
        expanded_transitions=search.expanded_transitions if search else 0,
        generated_states=search.generated_states if search else 0,
    )
    if environment is not None and evaluator is not None:
        summary["environment_evidence"] = seeded_rejection_summary(environment, evaluator)
    return summary


def _cns_record(problem):
    expected = problem.get("expected_cns_assessment") or {}
    return {
        "integration_mode": "post_route_assessment",
        "evaluated": False,
        "excluded_from_search_cost": True,
        "semantics": expected.get("semantics")
        or "V3 第一阶段 CNS 不进搜索：Route Planning → CNS Assessment",
        "next_stage": "V3-D_validated_route_operational_adapter_and_cns_assessment",
    }


def _heuristic_semantics(problem, readiness, *, active):
    """The heuristic is the plain 3D geometric distance.  No weight enters it.

    ``scale`` stays in the contract (consumers read it) but is exactly ``1`` and
    no longer derived from the soft weights.
    """

    policy = problem["policy"]
    cost_model = policy.get("cost_model") or {}
    components = cost_model.get("components") or {}
    penalties_enabled = sorted(
        name for name, item in components.items()
        if item.get("enabled") and name not in ("distance", "energy")
    )
    weight_sum = 0.0
    for name in penalties_enabled:
        weight = (components.get(name) or {}).get("weight")
        if weight is not None:
            weight_sum += float(weight)
    return {
        "active": bool(active),
        "type": "admissible_3d_geometric_distance_lower_bound",
        "definition": "h = 3D geodesic distance(state, goal)：不乘任何 soft weight 系数",
        "scale": 1.0,
        "scale_basis": "exactly 1 -- 几何距离本身；weight 与启发函数完全解耦",
        "admissibility_argument": (
            "每条边的标量代价 = 边长 + Σ(weight × exposure)，其中 weight 有限且 >= 0，"
            "exposure = 边长 × 端点 normalized index 的均值且 index ∈ [0,1]，"
            "因此每条边代价 >= 其 3D 几何边长，h = 3D 几何距离不会高估真实代价。"
            "soft penalty 可以为 0，所以旧版 h = 距离 × (1 + Σweight) 在 penalty 取 0 时并非下界；"
            "该写法与 Σweight <= 1 的限制均已删除"
        ),
        "soft_penalties_enabled": penalties_enabled,
        "soft_penalty_weight_sum_reported_only": round(weight_sum, 9),
        "soft_penalty_weight_sum_enters_heuristic": False,
        "soft_penalty_weight_sum_is_bounded": False,
        "soft_penalty_non_negative": True,
        "soft_index_domain": "[0, 1] provenance normalized index",
        "no_implicit_normalizer": True,
        "prefers_lower_candidate_scalar_cost": True,
        "not_a_flight_time_energy_or_cns_heuristic": True,
        "altitude_geometric_term": "included_vertical_delta",
    }


def _projection(problem, state_path):
    points = [[record["x"], record["y"]] for record in state_path]
    start, goal = problem["start"], problem["goal"]
    if points:
        if [start["x"], start["y"]] != points[0]:
            points.insert(0, [start["x"], start["y"]])
        if [goal["x"], goal["y"]] != points[-1]:
            points.append([goal["x"], goal["y"]])
    return points


def _readiness_reason(overall, readiness):
    domains = [
        (name, value) for name, value in readiness.items() if isinstance(value, dict)
    ]
    blocked = [name for name, value in domains if value.get("status") == "blocked"]
    pending = [name for name, value in domains if value.get("status") == "pending"]
    if blocked:
        return "V3 readiness blocked：" + ", ".join(sorted(blocked)) + "；真实数据/参数未准备好时不得硬跑"
    if pending:
        return "V3 readiness pending：" + ", ".join(sorted(pending)) + "；缺少显式确认"
    return "V3 readiness 未就绪"


def _normalized_delta(bearing, current_heading):
    delta = (float(bearing) - float(current_heading)) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


def _state_order(state):
    return (str(state[0]), int(state[1]), int(state[2]))


def _finalize(result, started):
    result["search_statistics"]["runtime_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    result["algorithm_id"] = V3StrategicPlanner.algorithm_id
    result["algorithm_version"] = V3StrategicPlanner.algorithm_version
    result["model_scope"] = V3StrategicPlanner.model_scope
    result["disclaimer"] = V3_STRATEGIC_RESULT_DISCLAIMER
    result["operational_route"] = False
    result["final_validation_performed"] = False
    semantics = result.setdefault("semantics", {})
    semantics.update({
        "scope": V3StrategicPlanner.model_scope,
        "not_final_safe": True,
        "not_validated_operational_route": True,
        "unknown_is_never_safe": True,
        "motion_model_id": MOTION_MODEL_ID,
        "motion_model": MOTION_MODEL_SEMANTICS,
        "search_state": "grid_id + altitude_index + heading_bin",
        "canonical_vertical_reference": "egm2008_orthometric",
        "heuristic_is_plain_geometric_distance": True,
        "soft_fields_are_provenance_normalized_indices": True,
        "no_hidden_soft_normalizer": True,
        "soft_weight_sum_is_not_bounded_and_not_used_by_the_heuristic": True,
        "expansion_cap_is_resource_limited_not_infeasible": True,
        "v3a_scope_limits": {
            "corridor_local_fine_refinement": "implemented_in_V3-B",
            "exact_polygon_terrain_continuous_clearance_validation": "not_implemented_V3-C",
            "validated_route_operational_adapter": "not_implemented_V3-D",
            "route_cns_joint_optimization": "future_backlog_not_V3-D",
            "energy_model": "pending_model_disabled",
        },
    })
    return result


def v3_problem_fingerprint(problem):
    payload = {
        "problem_id": problem.get("problem_id"),
        "route_id": problem.get("route_id"),
        "vertical_reference": problem.get("vertical_reference"),
        "start": problem.get("start"),
        "goal": problem.get("goal"),
        "policy": problem.get("policy"),
        "aircraft_motion_limits": problem.get("aircraft_motion_limits"),
        "environment": problem.get("environment"),
        "soft_cost_weights": problem.get("soft_cost_weights"),
        "provenance": problem.get("provenance"),
    }
    return sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


__all__ = ["V3StrategicPlanner", "v3_problem_fingerprint"]
