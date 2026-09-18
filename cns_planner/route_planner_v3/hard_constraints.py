"""Separated hard-constraint evaluation for Route Planner V3-A.

``state_feasible``  — altitude band, terrain clearance, building clearance.
``transition_feasible`` — turn capability and climb/descent capability.

Both directions fail closed: a cell whose terrain or building evidence is
``unknown`` is **not** feasible. Airspace is display-only metadata. This module never touches GDAL/QGIS/files; it
only consumes the canonical :class:`~cns_planner.route_planner_v3.contracts.V3CellEnvironment`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import radians as _radians

#: Rejection reasons are stable identifiers so expert evidence can be aggregated.
STATE_REASONS = (
    "cell_not_in_environment",
    "altitude_above_max",
    "altitude_below_min",
    "terrain_clearance_unresolved",
    "below_terrain_clearance",
    "building_clearance_unresolved",
    "below_building_clearance",
    "altitude_index_out_of_band",
    "altitude_index_not_on_grid",
)
TRANSITION_REASONS = (
    "neighbor_not_adjacent",
    "heading_change_without_horizontal_motion",
    "turn_capability_unknown",
    "turn_radius_exceeded",
    "vertical_only_transition_not_modelled",
    "climb_capability_unknown",
    "climb_gradient_exceeded",
    "descent_capability_unknown",
    "descent_gradient_exceeded",
    "altitude_step_not_single_band",
    "altitude_band_exceeded",
    "transition_geometry_unresolved",
)

#: How many distinct rejecting states/edges are retained per reason.
MAX_AUDIT_SAMPLES = 200


@dataclass
class HardConstraintAudit:
    """Distinct-state/edge rejection tally, aggregated by stable reason."""

    state_rejections: dict = field(default_factory=dict)
    transition_rejections: dict = field(default_factory=dict)
    _state_samples: dict = field(default_factory=dict)
    _transition_samples: dict = field(default_factory=dict)
    _state_truncated: set = field(default_factory=set)
    _transition_truncated: set = field(default_factory=set)

    def record_state(self, reason, sample_key):
        self._record(self.state_rejections, self._state_samples, self._state_truncated, reason, sample_key)

    def record_transition(self, reason, sample_key):
        self._record(self.transition_rejections, self._transition_samples, self._transition_truncated, reason, sample_key)

    @staticmethod
    def _record(counts, samples, truncated, reason, sample_key):
        bucket = samples.setdefault(reason, set())
        if len(bucket) >= MAX_AUDIT_SAMPLES:
            if sample_key not in bucket:
                truncated.add(reason)
                counts[reason] = counts.get(reason, 0) + 1
            return
        if sample_key in bucket:
            return
        bucket.add(sample_key)
        counts[reason] = counts.get(reason, 0) + 1

    def summary(self, *, expanded_states, expanded_transitions, generated_states):
        state_total = sum(self.state_rejections.values())
        transition_total = sum(self.transition_rejections.values())
        return {
            "state_rejections": dict(sorted(self.state_rejections.items())),
            "transition_rejections": dict(sorted(self.transition_rejections.items())),
            "total_state_rejections": state_total,
            "total_transition_rejections": transition_total,
            "expanded_states": expanded_states,
            "expanded_transitions": expanded_transitions,
            "generated_states": generated_states,
            "audit_semantics": "distinct_state_and_edge_rejection_counts_with_stable_reason_ids",
            "audit_sample_cap_per_reason": MAX_AUDIT_SAMPLES,
            "audit_sample_truncated_reasons": sorted(self._state_truncated | self._transition_truncated),
            "unknown_is_never_feasible": True,
        }


class HardConstraintEvaluator:
    """State and transition feasibility against canonical environment evidence."""

    def __init__(self, environment, policy, *, climb_gradient, descent_gradient):
        self.policy = policy
        self.climb_gradient = climb_gradient
        self.descent_gradient = descent_gradient
        self.min_turn_radius_m = policy.get("aircraft_min_turn_radius_m")
        self.min_altitude = policy.get("min_altitude_egm2008_m")
        self.max_altitude = policy.get("max_altitude_egm2008_m")
        self.vertical_step_m = policy.get("vertical_step_m")
        self.terrain_clearance_m = policy.get("terrain_clearance_m")
        self.building_vertical_clearance_m = policy.get("building_vertical_clearance_m")
        self.cells = {str(cell["grid_id"]): cell for cell in (environment or {}).get("cells") or []}

    # ------------------------------------------------------------------ state

    def state_feasible(self, grid_id, altitude_index, altitude_egm2008_m):
        """Return ``(feasible, reason)`` for one search state."""

        cell = self.cells.get(grid_id)
        if cell is None:
            return False, "cell_not_in_environment"
        altitude = float(altitude_egm2008_m)
        if self.max_altitude is not None and altitude > float(self.max_altitude) + _TOLERANCE:
            return False, "altitude_above_max"
        if self.min_altitude is not None and altitude < float(self.min_altitude) - _TOLERANCE:
            return False, "altitude_below_min"
        terrain = cell.get("terrain") or {}
        floor = terrain.get("surface_clearance_egm2008_m")
        if terrain.get("data_status") != "passed" or floor is None:
            return False, "terrain_clearance_unresolved"
        if altitude < float(floor) - _TOLERANCE:
            return False, "below_terrain_clearance"
        buildings = cell.get("buildings") or {}
        roof = buildings.get("required_clearance_egm2008_m")
        if buildings.get("data_status") != "passed":
            return False, "building_clearance_unresolved"
        if roof is not None and altitude < float(roof) - _TOLERANCE:
            # ``data_status == passed`` with no roof is a *confirmed absence* of a
            # building constraint in that cell; a present roof is enforced.
            return False, "below_building_clearance"
        if self._altitude_index_consistent(altitude_index, altitude) is False:
            return False, "altitude_index_out_of_band"
        return True, None

    def _altitude_index_consistent(self, altitude_index, altitude):
        if self.min_altitude is None or self.vertical_step_m is None:
            return None
        expected = float(self.min_altitude) + int(altitude_index) * float(self.vertical_step_m)
        return abs(expected - float(altitude)) <= 1e-6

    # ------------------------------------------------------------------ transition

    def transition_feasible(
        self, *, source_grid_id, target_grid_id, source_altitude_index, target_altitude_index,
        horizontal_step_m, vertical_step_m, heading_change_deg, kind, neighbor=True,
    ):
        """Return ``(feasible, reason)`` for one motion primitive execution."""

        if not neighbor:
            return False, "neighbor_not_adjacent"
        step = abs(int(target_altitude_index) - int(source_altitude_index))
        if step > 1:
            return False, "altitude_step_not_single_band"
        if self.max_altitude is None or self.min_altitude is None:
            return False, "altitude_index_out_of_band"
        if float(vertical_step_m) > 0:
            if kind not in ("climb", "descend"):
                return False, "transition_geometry_unresolved"
            if float(horizontal_step_m) <= 0:
                return False, "vertical_only_transition_not_modelled"
            gradient = float(vertical_step_m) / float(horizontal_step_m)
            if kind == "climb":
                if self.climb_gradient is None:
                    return False, "climb_capability_unknown"
                if gradient > float(self.climb_gradient) + _TOLERANCE:
                    return False, "climb_gradient_exceeded"
            else:
                if self.descent_gradient is None:
                    return False, "descent_capability_unknown"
                if gradient > float(self.descent_gradient) + _TOLERANCE:
                    return False, "descent_gradient_exceeded"
        change = abs(float(heading_change_deg))
        if change > _TOLERANCE:
            if float(horizontal_step_m) <= 0:
                return False, "heading_change_without_horizontal_motion"
            if self.min_turn_radius_m is None:
                return False, "turn_capability_unknown"
            if float(self.min_turn_radius_m) * _radians(change) > float(horizontal_step_m) + _TOLERANCE:
                return False, "turn_radius_exceeded"
        return True, None


def seeded_rejection_summary(environment, evaluator, altitude_index=0, sample_limit=64):
    """Cell-level evidence gaps, measured once over a bounded sample.

    This reports how many cells carry unresolved terrain/building
    evidence in the *canonical environment itself*, independent of how much of
    the state space the search happened to expand.
    """

    counts = {
        "terrain_unknown_cells": 0,
        "building_unknown_cells": 0,
    }
    samples = []
    for index, cell in enumerate((environment or {}).get("cells") or []):
        grid_id = str(cell["grid_id"])
        terrain = cell.get("terrain") or {}
        buildings = cell.get("buildings") or {}
        if terrain.get("data_status") != "passed" or terrain.get("surface_clearance_egm2008_m") is None:
            counts["terrain_unknown_cells"] += 1
        if buildings.get("data_status") != "passed" or buildings.get("required_clearance_egm2008_m") is None:
            counts["building_unknown_cells"] += 1
        if index < sample_limit:
            feasible, reason = evaluator.state_feasible(
                grid_id, altitude_index,
                (evaluator.min_altitude or 0.0) + altitude_index * (evaluator.vertical_step_m or 0.0),
            )
            if not feasible:
                samples.append({"grid_id": grid_id, "reason": reason})
    counts["cell_count"] = len((environment or {}).get("cells") or [])
    counts["sampled_cell_states"] = min(sample_limit, counts["cell_count"])
    counts["sample_rejections"] = samples
    counts["semantics"] = "canonical_environment_evidence_gaps_independent_of_search_expansion"
    return counts


_TOLERANCE = 1e-9

__all__ = [
    "STATE_REASONS", "TRANSITION_REASONS", "MAX_AUDIT_SAMPLES",
    "HardConstraintAudit", "HardConstraintEvaluator", "seeded_rejection_summary",
]
