"""Independent conservative supercover traversal for ``Layered Risk-Aware Theta* V2``.

This module is a **pure algorithm** module.  It imports no V3 contract, no QGIS/GDAL and
no file: the traversal is deliberately re-derived here from the Amanatides & Woo grid
step so that the production V2 planner never depends on the V3 business contracts.  The
earlier V3 ``fine_search.supercover_line`` is the *algorithmic* idea that was reused (a
grid traversal that also reports the cells a segment only touches at a corner) — the V3
module is not imported, so a V3 contract change can never silently change V2 planning.

Why a supercover and not an endpoint test: a ``parent -> target`` Theta* shortcut is a
straight segment that can cross many cells.  Testing only its two endpoints would let the
segment pass straight through a blocked terrain cell, a building column or a confirmed
no-fly polygon.  Every crossed cell is therefore returned together with the **real length
of the segment inside that cell**, which is exactly what the risk integral needs::

    E_risk(segment) = Σ_cell (length_in_cell * risk_index_cell)

Boundary-touch semantics (conservative, fail-closed)::

    CONSERVATIVE_BOUNDARY_TOUCH

A cell that only *touches* the segment — at a corner or along an edge, with zero-length
contact — is still reported as crossed.  When the segment passes exactly through a grid
corner, the two orthogonal neighbours **and** the diagonal cell are all reported (each
once, in deterministic order, with the exact corner fraction).  This is intentionally
*more* inclusive than the mathematical supercover and is the safe direction: an
intermediate obstacle can never be skipped, and two blocked cells meeting at a corner can
never be slipped through diagonally.

Invariant::

    Σ_cell length_m == segment length  (within floating-point tolerance)

Zero-length boundary-touch entries contribute exactly ``0`` to the risk integral, so the
conservative inclusion never inflates the reported risk exposure.
"""

from __future__ import annotations

from math import inf, isclose

_TOLERANCE = 1e-9

#: Boundary-touch semantics actually implemented by :func:`supercover_traversal`.
CONSERVATIVE_BOUNDARY_TOUCH = (
    "corner_crossing_yields_both_orthogonal_neighbours_and_the_diagonal_cell_"
    "without_duplicates_zero_length_contact_reported_and_integrates_to_zero"
)

#: How the per-cell length is derived from the traversal parameters.
LENGTH_ASSIGNMENT_SEMANTICS = (
    "amantatides_woo_entry_fraction_differences_times_segment_length_"
    "sums_to_the_segment_length"
)

#: Corner-touch policy of the V2 search.
CORNER_TOUCH_POLICY = "conservative_never_pass_between_two_blocked_cells_at_a_corner"


def supercover_traversal(start_index, end_index):
    """Cells a segment passes through as ``(cell_index, entry_fraction)``.

    ``cell_index`` is the caller's own grid index tuple (for the MH/T L8 grid that is
    ``(level, column, row)``), with the level component carried through unchanged so the
    traversal result can be mapped straight back to a ``grid_id``.  ``entry_fraction`` is
    the fraction of the segment at which the cell is entered and is non-decreasing, so the
    cells can be zipped deterministically.  Boundary-touch cells that are entered at
    exactly the same fraction as their neighbour are included once.
    """

    prefix = tuple(start_index[:-2])
    start_column, start_row = int(start_index[-2]), int(start_index[-1])
    end_column, end_row = int(end_index[-2]), int(end_index[-1])
    delta_column, delta_row = end_column - start_column, end_row - start_row
    if delta_column == 0 and delta_row == 0:
        return [((*prefix, start_column, start_row), 0.0)]
    step_column = (delta_column > 0) - (delta_column < 0)
    step_row = (delta_row > 0) - (delta_row < 0)
    if delta_column != 0:
        t_max_column = 0.5 / abs(delta_column)
        t_delta_column = 1.0 / abs(delta_column)
    else:
        t_max_column, t_delta_column = inf, inf
    if delta_row != 0:
        t_max_row = 0.5 / abs(delta_row)
        t_delta_row = 1.0 / abs(delta_row)
    else:
        t_max_row, t_delta_row = inf, inf

    current = (start_column, start_row)
    traversed = [((*prefix, *current), 0.0)]
    seen = {(*prefix, *current)}
    guard = 6 * (abs(delta_column) + abs(delta_row)) + 8
    steps = 0
    while current != (end_column, end_row):
        steps += 1
        if steps > guard:
            break
        # Equal t_max values mean the segment passes exactly through the shared corner of
        # the current cell, both orthogonal neighbours and the diagonal cell -- the
        # boundary-touch case that must not be dropped and must not be crossable.
        crossed_corner = (
            delta_column != 0 and delta_row != 0
            and isclose(t_max_column, t_max_row, abs_tol=_TOLERANCE)
        )
        if crossed_corner:
            fraction = _clamp(min(t_max_column, t_max_row))
            _append(traversed, seen, prefix, (current[0] + step_column, current[1]), fraction)
            _append(traversed, seen, prefix, (current[0], current[1] + step_row), fraction)
            current = (current[0] + step_column, current[1] + step_row)
            _append(traversed, seen, prefix, current, fraction)
            t_max_column += t_delta_column
            t_max_row += t_delta_row
        elif t_max_column < t_max_row - _TOLERANCE:
            current = (current[0] + step_column, current[1])
            traversed.append(((*prefix, *current), _clamp(t_max_column)))
            seen.add((*prefix, *current))
            t_max_column += t_delta_column
        elif t_max_row < t_max_column - _TOLERANCE:
            current = (current[0], current[1] + step_row)
            traversed.append(((*prefix, *current), _clamp(t_max_row)))
            seen.add((*prefix, *current))
            t_max_row += t_delta_row
        else:
            current = (current[0] + step_column, current[1] + step_row)
            traversed.append(((*prefix, *current), _clamp(min(t_max_column, t_max_row))))
            seen.add((*prefix, *current))
            t_max_column += t_delta_column
            t_max_row += t_delta_row
    return traversed


def _clamp(value):
    return max(0.0, min(1.0, value))


def _append(traversed, seen, prefix, cell, fraction):
    key = (*prefix, *cell)
    if key in seen:
        return
    if traversed and fraction < traversed[-1][1]:
        fraction = traversed[-1][1]
    traversed.append((key, fraction))
    seen.add(key)


def traversal_cells_with_lengths(traversed, segment_length_m):
    """Attach the real in-cell length to every traversed cell.

    The length of a cell is ``(next_entry_fraction - entry_fraction) * segment_length``;
    the last cell closes on fraction ``1.0``.  The sum is exactly the segment length, and
    zero-length boundary-touch entries stay exactly ``0``.
    """

    length = float(segment_length_m) if segment_length_m else 0.0
    result = []
    for position, (cell, entry) in enumerate(traversed):
        end = traversed[position + 1][1] if position + 1 < len(traversed) else 1.0
        result.append({
            "cell_index": cell,
            "entry_fraction": round(float(entry), 12),
            "length_m": max(0.0, (float(end) - float(entry))) * length,
        })
    return result


__all__ = [
    "CONSERVATIVE_BOUNDARY_TOUCH", "CORNER_TOUCH_POLICY",
    "LENGTH_ASSIGNMENT_SEMANTICS",
    "supercover_traversal", "traversal_cells_with_lengths",
]
