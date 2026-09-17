"""Motion primitives and transition validation for Route Planner V3-A.

Motion capability is enforced **during edge generation and validation**, not by
inspecting a finished path.  Every accepted edge records its ``primitive_id``,
step length, climb gradient and heading change.

The implemented model is an explicit *engineering baseline*: a discrete
8-direction horizontal move set combined with one altitude step, checked against
an explicit minimum turn radius and an explicit climb/descent gradient.  It is
**not** a flight-dynamics or handling-qualities certification model, and it does
not implement Dubins/Reeds-Shepp arcs, bank angle, wind or energy management.
"""

from __future__ import annotations

from math import radians

from ..benchmark.geodesy import geodesic_bearing_deg, geodesic_distance_m
from .contracts import heading_bin_center_deg, heading_bin_for_bearing

#: Deterministic, ordered horizontal move set (E, NE, N, NW, W, SW, S, SE).
_GRID_DELTAS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

MOTION_MODEL_ID = "engineering_3d_motion_primitives_v3a"
MOTION_MODEL_SEMANTICS = (
    "engineering_kinematic_limits_implemented_in_edge_generation_"
    "not_a_flight_dynamics_certification_model"
)
#: Numerical tolerance only: it never relaxes a limit, it only keeps a
#: primitive that sits exactly on the explicit limit from being rejected.
_COMPARISON_TOLERANCE = 1e-9


def geodesic_step_m(left_center, right_center):
    return geodesic_distance_m(list(left_center), list(right_center))


def geodesic_bearing(left_center, right_center):
    return geodesic_bearing_deg(list(left_center), list(right_center))


def normalize_heading_delta(degrees_value):
    """Signed heading delta in (-180, 180]."""

    delta = float(degrees_value) % 360.0
    if delta > 180.0:
        delta -= 360.0
    return delta


class MotionPrimitiveProvider:
    """Deterministic 3D primitive set for one canonical environment."""

    def __init__(self, environment, heading_bin_count=8):
        if not isinstance(environment, dict) or not environment.get("cells"):
            raise ValueError("MotionPrimitiveProvider 需要 canonical V3CellEnvironment")
        count = int(heading_bin_count)
        if count < 4 or 360 % count != 0:
            raise ValueError("heading_bin_count 必须是不小于 4 且整除 360 的整数")
        self.heading_bin_count = count
        self.heading_step_deg = round(360.0 / count, 9)
        self.centers = {
            str(cell["grid_id"]): (float(cell["center"][0]), float(cell["center"][1]))
            for cell in environment["cells"]
        }
        self.cells = {str(cell["grid_id"]): cell for cell in environment["cells"]}
        self.adjacency = _adjacency(self.cells)
        self._bearing_cache = {}
        self._distance_cache = {}
        self._primitive_cache = {}
        self._precompute_edges()

    # ------------------------------------------------------------------ geometry

    def _precompute_edges(self):
        """Measure every canonical edge exactly once.

        The state space is grid x altitude x heading, so one geometric edge is
        traversed many times.  Measuring it once keeps a strategic run
        deterministic and independent of how much of the state space the search
        happens to expand.
        """

        for grid_id in self.centers:
            for neighbour in self.adjacency.get(grid_id, ()):
                key = (grid_id, neighbour)
                if key in self._distance_cache:
                    continue
                left, right = self.centers[grid_id], self.centers[neighbour]
                self._distance_cache[key] = geodesic_step_m(left, right)
                self._bearing_cache[key] = geodesic_bearing(left, right)

    def neighbors(self, grid_id):
        return self.adjacency.get(grid_id, ())

    def bearing_deg(self, left, right):
        key = (left, right)
        value = self._bearing_cache.get(key)
        if value is None:
            value = geodesic_bearing(self.centers[left], self.centers[right])
            self._bearing_cache[key] = value
        return value

    def step_length_m(self, left, right):
        key = (left, right)
        value = self._distance_cache.get(key)
        if value is None:
            value = geodesic_step_m(self.centers[left], self.centers[right])
            self._distance_cache[key] = value
        return value

    def arrival_heading_bin(self, previous_grid_id, grid_id):
        """Heading bin established by flying ``previous -> grid_id``."""

        if previous_grid_id is None or previous_grid_id not in self.centers:
            return None
        return heading_bin_for_bearing(
            self.bearing_deg(previous_grid_id, grid_id), self.heading_bin_count,
        )

    def required_heading_change_deg(self, previous_grid_id, grid_id, current_heading_bin):
        """Geometric heading change of continuing into ``grid_id``.

        This is derived from the movement geometry, never from a declared value,
        so a primitive can never claim a turn it does not perform.
        """

        if previous_grid_id is None:
            return None
        bearing = self.bearing_deg(previous_grid_id, grid_id)
        current = heading_bin_center_deg(current_heading_bin, self.heading_bin_count)
        return round(abs(normalize_heading_delta(bearing - current)), 9)

    # ------------------------------------------------------------------ primitives

    def for_state(self, grid_id, altitude_index):
        key = (grid_id, int(altitude_index))
        cached = self._primitive_cache.get(key)
        if cached is not None:
            return cached
        items = []
        for column, row in _GRID_DELTAS:
            items.append({
                "primitive_id": f"h{column:+d}{row:+d}",
                "kind": "horizontal_level",
                "grid_delta": [column, row],
                "altitude_delta_steps": 0,
                "requires_horizontal_motion": True,
                "requires_altitude_change": False,
            })
        for delta, kind in ((-1, "descend"), (1, "climb")):
            items.append({
                "primitive_id": f"v{delta:+d}",
                "kind": kind,
                "grid_delta": [0, 0],
                "altitude_delta_steps": delta,
                "requires_horizontal_motion": False,
                "requires_altitude_change": True,
            })
        for column, row in _GRID_DELTAS:
            for delta, kind in ((-1, "descend"), (1, "climb")):
                items.append({
                    "primitive_id": f"h{column:+d}{row:+d}v{delta:+d}",
                    "kind": kind,
                    "grid_delta": [column, row],
                    "altitude_delta_steps": delta,
                    "requires_horizontal_motion": True,
                    "requires_altitude_change": True,
                })
        self._primitive_cache[key] = tuple(items)
        return self._primitive_cache[key]


class TransitionValidator:
    """Turn / climb / descent capability gate for one edge.

    ``min_turn_radius_m`` uses an explicit arc-length interpretation: a heading
    change ``dpsi`` needs at least ``R * |dpsi|`` metres of horizontal arc before
    the new heading is established, and that arc is compared against this edge's
    horizontal step length.  A hover (zero horizontal motion) cannot turn, and a
    purely vertical transition is not modelled.
    """

    def __init__(self, minimum_turn_radius_m, max_climb_gradient, max_descent_gradient,
                 heading_step_deg=None):
        self.minimum_turn_radius_m = minimum_turn_radius_m
        self.max_climb_gradient = max_climb_gradient
        self.max_descent_gradient = max_descent_gradient
        self.heading_step_deg = heading_step_deg

    def evaluate(self, *, horizontal_step_m, vertical_step_m, heading_change_deg, kind):
        """Return ``(feasible, reason)``; ``reason`` is None when feasible."""

        horizontal = float(horizontal_step_m)
        vertical = float(vertical_step_m)
        change = abs(float(heading_change_deg))
        if vertical > 0 and horizontal <= 0:
            return False, "vertical_only_transition_not_modelled"
        if change > _COMPARISON_TOLERANCE:
            if horizontal <= 0:
                return False, "heading_change_without_horizontal_motion"
            if self.minimum_turn_radius_m is None:
                return False, "turn_capability_unknown"
            if float(self.minimum_turn_radius_m) * radians(change) > horizontal + _COMPARISON_TOLERANCE:
                return False, "turn_radius_exceeded"
        if vertical > 0:
            gradient = vertical / horizontal
            if kind == "climb":
                limit, name = self.max_climb_gradient, "climb_gradient_exceeded"
                if limit is None:
                    return False, "climb_capability_unknown"
            else:
                limit, name = self.max_descent_gradient, "descent_gradient_exceeded"
                if limit is None:
                    return False, "descent_capability_unknown"
            if gradient > float(limit) + _COMPARISON_TOLERANCE:
                return False, name
        return True, None


# --------------------------------------------------------------------------- readiness


def kinematic_readiness(policy, aircraft):
    """Which climb/descent capability is resolved, and from what evidence.

    A rate limit is only convertible with an explicit planning speed.  V3 never
    guesses a speed, so ``max_climb_rate_mps`` without ``planning_speed_mps``
    leaves the capability unresolved and blocks readiness.
    """

    policy_speed = policy.get("planning_speed_mps")
    aircraft_speed = aircraft.get("planning_speed_mps")
    speed = policy_speed if policy_speed is not None else aircraft_speed
    speed_source = (
        "policy" if policy_speed is not None
        else "aircraft_motion_limits" if aircraft_speed is not None
        else "unresolved_no_planning_speed_is_ever_guessed"
    )
    climb = _resolve_capability(policy, aircraft, "climb", speed)
    descent = _resolve_capability(policy, aircraft, "descent", speed)
    return {
        "climb_gradient": climb["gradient"],
        "climb_gradient_source": climb["source"],
        "descent_gradient": descent["gradient"],
        "descent_gradient_source": descent["source"],
        "planning_speed_mps": speed,
        "planning_speed_source": speed_source,
        "resolved": climb["gradient"] is not None and descent["gradient"] is not None,
        "semantics": "rise_over_run_gradient_not_percent_no_speed_is_ever_guessed",
    }


def _resolve_capability(policy, aircraft, kind, speed):
    for name, label in ((policy, "policy"), (aircraft, "aircraft_motion_limits")):
        direct = name.get(f"max_{kind}_gradient")
        if direct is not None:
            return {"gradient": float(direct), "source": f"{label}_direct_{kind}_gradient"}
    for name, label in ((policy, "policy"), (aircraft, "aircraft_motion_limits")):
        rate = name.get(f"max_{kind}_rate_mps")
        if rate is not None and speed:
            return {
                "gradient": float(rate) / float(speed),
                "source": f"{label}_max_{kind}_rate_mps_over_explicit_planning_speed_mps",
            }
        if rate is not None:
            return {
                "gradient": None,
                "source": f"{label}_max_{kind}_rate_mps_without_explicit_planning_speed_blocked",
            }
    return {"gradient": None, "source": f"no_{kind}_capability_given"}


def derive_grid_index(cells):
    """Chebyshev index ``{grid_id: (level, column, row)}`` for the canonical grid.

    Explicit ``column``/``row`` are used when the grid provides them; otherwise a
    stable index is derived from each cell's own west/south bound and the grid's
    smallest cell size.  V3 never guesses topology from geometry *when an index
    exists*, but a grid that does not publish one still has a deterministic
    Chebyshev structure and that structure -- not a geometric proximity guess --
    is what the adjacency uses.
    """

    explicit = {}
    for grid_id, cell in cells.items():
        column, row = cell.get("column"), cell.get("row")
        if column is None or row is None:
            explicit = {}
            break
        explicit[grid_id] = (int(cell.get("level") or 0), int(column), int(row))
    if explicit and len(set(explicit.values())) == len(explicit):
        return explicit
    bounds = {}
    for grid_id, cell in cells.items():
        bbox = cell.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return {}
        bounds[grid_id] = [float(value) for value in bbox]
    if not bounds:
        return {}
    west = min(bbox[0] for bbox in bounds.values())
    south = min(bbox[1] for bbox in bounds.values())
    lon_step = _smallest_step(sorted({round(bbox[0], 12) for bbox in bounds.values()}))
    lat_step = _smallest_step(sorted({round(bbox[1], 12) for bbox in bounds.values()}))
    if lon_step <= 0 or lat_step <= 0:
        return {}
    index = {}
    for grid_id, bbox in bounds.items():
        index[grid_id] = (
            int(cells[grid_id].get("level") or 0),
            int(round((bbox[0] - west) / lon_step)),
            int(round((bbox[1] - south) / lat_step)),
        )
    return index if len(set(index.values())) == len(index) else {}


def _smallest_step(values):
    differences = [b - a for a, b in zip(values, values[1:]) if b - a > 1e-12]
    return min(differences) if differences else 0.0


def _adjacency(cells):
    index = derive_grid_index(cells)
    if not index:
        return _bbox_adjacency(cells)
    reverse = {value: key for key, value in index.items()}
    adjacency = {}
    for grid_id, (level, column, row) in index.items():
        adjacency[grid_id] = tuple(sorted(
            reverse[(level, column + dx, row + dy)]
            for dx, dy in _GRID_DELTAS
            if (level, column + dx, row + dy) in reverse
        ))
    return adjacency


def _bbox_adjacency(cells):
    tolerance = 1e-11
    boxes = {}
    for grid_id, cell in cells.items():
        bbox = cell.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return {grid_id: () for grid_id in cells}
        boxes[grid_id] = [float(value) for value in bbox]
    adjacency = {}
    for grid_id, bbox in boxes.items():
        row = []
        for other, other_bbox in boxes.items():
            if other == grid_id:
                continue
            x_overlap = min(bbox[2], other_bbox[2]) - max(bbox[0], other_bbox[0])
            y_overlap = min(bbox[3], other_bbox[3]) - max(bbox[1], other_bbox[1])
            if x_overlap >= -tolerance and y_overlap >= -tolerance and (
                abs(x_overlap) <= tolerance or abs(y_overlap) <= tolerance
            ):
                row.append(other)
        adjacency[grid_id] = tuple(sorted(row))
    return adjacency


__all__ = [
    "MOTION_MODEL_ID", "MOTION_MODEL_SEMANTICS", "MotionPrimitiveProvider",
    "TransitionValidator", "kinematic_readiness", "normalize_heading_delta",
    "geodesic_bearing", "geodesic_step_m", "derive_grid_index",
]
