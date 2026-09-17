"""Candidate refinement corridor for Route Planner V3-A.

A corridor is a **proposal window for the next stage's local refinement**, derived
from the coarse L8 strategic state path.  Its semantics are fixed to
``refinement_search_window_not_safety_corridor``: the N-ring support cells are a
search-space expansion, **not** a safety clearance, not an operational volume and
not a regulatory boundary.

The 30 m refinement itself, the exact polygon/terrain validator and the CNS joint
optimization are deliberately not implemented in V3-A.
"""

from __future__ import annotations

from hashlib import sha256
import json

from .contracts import empty_candidate_refinement_corridor

CORRIDOR_SEMANTICS = "refinement_search_window_not_safety_corridor"
RING_SEMANTICS = "n_ring_support_cells_expand_the_search_window_not_a_clearance"
NEXT_STAGE = "V3-B_30m_local_refinement_and_exact_validation"


def grid_index(cells):
    """Chebyshev index of the canonical grid: ``{grid_id: (level, column, row)}``."""

    index = {}
    for cell in cells or []:
        column, row = cell.get("column"), cell.get("row")
        if column is None or row is None:
            continue
        index[str(cell["grid_id"])] = (int(cell.get("level") or 0), int(column), int(row))
    return index


def ring_neighbors(cells, center_grid_id, ring_n):
    """Chebyshev N-ring support cells around ``center_grid_id`` (inclusive).

    ``ring_n=0`` returns the center itself.  When the canonical grid carries no
    unique column/row index the ring degrades to the center only: a search window
    is never guessed from geometry, and it is never a clearance value.
    """

    grid_id = str(center_grid_id)
    ring = max(0, int(ring_n))
    index = grid_index(cells)
    center = index.get(grid_id)
    if center is None:
        return [grid_id]
    if ring == 0:
        return [grid_id]
    level, column, row = center
    return [
        other for other, (other_level, other_column, other_row) in sorted(index.items())
        if other_level == level
        and abs(other_column - column) <= ring and abs(other_row - row) <= ring
    ]


def build_candidate_refinement_corridor(
    state_path, environment, *, ring_n=0, refinement_cell_size_m=None,
    explicit_margin_m=None, route_id=None,
):
    """Build the L8 center path + configurable N-ring + altitude envelope."""

    result = empty_candidate_refinement_corridor()
    ring = max(0, int(ring_n))
    margin = None if explicit_margin_m in (None, "") else float(explicit_margin_m)
    size = None if refinement_cell_size_m in (None, "") else float(refinement_cell_size_m)
    if not state_path:
        result.update({
            "semantics": CORRIDOR_SEMANTICS,
            "ring_n": ring,
            "refinement_cell_size_m": size,
            "next_stage": NEXT_STAGE,
            "ring_semantics": RING_SEMANTICS,
            "not_implemented_in_v3a": [
                "30m_local_refinement", "exact_polygon_terrain_final_validation",
            ],
        })
        return result

    cells = (environment or {}).get("cells") or []
    center_grid_ids, center_altitudes = [], []
    for record in state_path:
        grid_id = str(record["grid_id"])
        if grid_id not in center_grid_ids:
            center_grid_ids.append(grid_id)
        if int(record["altitude_index"]) not in center_altitudes:
            center_altitudes.append(int(record["altitude_index"]))
    center_altitudes.sort()

    support = []
    for grid_id in center_grid_ids:
        for cell in ring_neighbors(cells, grid_id, ring):
            if cell not in support:
                support.append(cell)

    altitudes = [float(record["altitude_egm2008_m"]) for record in state_path]
    lower, upper = min(altitudes), max(altitudes)
    if margin is not None:
        lower, upper = lower - margin, upper + margin

    payload = {
        "route_id": route_id, "centers": center_grid_ids, "altitudes": center_altitudes,
        "ring_n": ring, "support": support,
    }
    result.update({
        "corridor_id": "V3CORR-" + sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()[:12].upper(),
        "semantics": CORRIDOR_SEMANTICS,
        "ring_n": ring,
        "refinement_cell_size_m": size,
        "center_grid_ids": center_grid_ids,
        "center_altitude_indices": center_altitudes,
        "support_grid_ids": support,
        "altitude_envelope": {
            "lower_altitude_egm2008_m": round(lower, 9),
            "upper_altitude_egm2008_m": round(upper, 9),
            "vertical_reference": "egm2008_orthometric",
            "explicit_margin_m": margin,
            "semantics": "planned_altitude_extent_not_a_clearance_volume",
        },
        "center_state_count": len(state_path),
        "support_cell_count": len(support),
        "n_ring_is_not_a_safety_clearance": True,
        "ring_semantics": RING_SEMANTICS,
        "next_stage": NEXT_STAGE,
        "not_implemented_in_v3a": [
            "30m_local_refinement", "exact_polygon_terrain_final_validation",
        ],
    })
    return result


__all__ = [
    "CORRIDOR_SEMANTICS", "RING_SEMANTICS", "NEXT_STAGE",
    "build_candidate_refinement_corridor", "grid_index", "ring_neighbors",
]
