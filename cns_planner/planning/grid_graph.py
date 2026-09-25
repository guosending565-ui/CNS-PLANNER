"""Canonical grid adjacency and coordinate lookup primitives.

This module deliberately contains no route objective or planner policy. Both the
production layered planners and compatibility planners consume this single graph.
"""

from __future__ import annotations

import math
import re


_GRID_ID = re.compile(
    r"^MHT4063-L(?P<level>\d+)-C(?P<column>\d+)-R(?P<sign>[PM])(?P<row>\d+)$"
)


class GridGraph:
    """Stable-id 8-neighbour graph built from canonical grid cells."""

    def __init__(self, cells):
        if not isinstance(cells, list) or not cells:
            raise ValueError("标准网格 cells 缺失")
        self.cells = {}
        self.centers = {}
        for raw in cells:
            grid_id = str((raw or {}).get("grid_id") or "")
            bbox = (raw or {}).get("bbox")
            if not grid_id or grid_id in self.cells:
                raise ValueError("标准网格 grid_id 缺失或重复")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise ValueError(f"网格 {grid_id} 缺少 bbox")
            west, south, east, north = (float(value) for value in bbox)
            if (
                not all(math.isfinite(value) for value in (west, south, east, north))
                or west >= east
                or south >= north
            ):
                raise ValueError(f"网格 {grid_id} bbox 无效")
            cell = dict(raw)
            cell["bbox"] = [west, south, east, north]
            center = cell.get("center")
            if not isinstance(center, (list, tuple)) or len(center) < 2:
                center = [(west + east) / 2.0, (south + north) / 2.0]
            cell["center"] = [float(center[0]), float(center[1])]
            self.cells[grid_id] = cell
            self.centers[grid_id] = cell["center"]
        self._extents = (
            min(cell["bbox"][0] for cell in self.cells.values()),
            min(cell["bbox"][1] for cell in self.cells.values()),
            max(cell["bbox"][2] for cell in self.cells.values()),
            max(cell["bbox"][3] for cell in self.cells.values()),
        )
        self.indices = self._indices()
        self.adjacency = (
            self._indexed_adjacency() if self.indices is not None else self._bbox_adjacency()
        )

    def containing_cell(self, point):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        lon, lat = float(point[0]), float(point[1])
        max_east, max_north = self._extents[2], self._extents[3]
        for grid_id in sorted(self.cells):
            west, south, east, north = self.cells[grid_id]["bbox"]
            in_lon = west <= lon < east or (
                math.isclose(east, max_east) and west <= lon <= east
            )
            in_lat = south <= lat < north or (
                math.isclose(north, max_north) and south <= lat <= north
            )
            if in_lon and in_lat:
                return grid_id
        return None

    def neighbors(self, grid_id):
        return self.adjacency.get(grid_id, ())

    def diagonal_guards(self, left, right):
        """Return orthogonal cells that must be traversable for a diagonal."""
        if self.indices is not None:
            a, b = self.indices[left], self.indices[right]
            if a[0] != b[0] or a[1] == b[1] or a[2] == b[2]:
                return None
            reverse = {value: key for key, value in self.indices.items()}
            candidates = ((a[0], b[1], a[2]), (a[0], a[1], b[2]))
            return tuple(reverse.get(value) for value in candidates)
        a, b = self.centers[left], self.centers[right]
        if math.isclose(a[0], b[0], abs_tol=1e-12) or math.isclose(
            a[1], b[1], abs_tol=1e-12
        ):
            return None
        center_index = {
            (round(value[0], 12), round(value[1], 12)): grid_id
            for grid_id, value in self.centers.items()
        }
        return (
            center_index.get((round(b[0], 12), round(a[1], 12))),
            center_index.get((round(a[0], 12), round(b[1], 12))),
        )

    def _indices(self):
        result = {}
        seen = set()
        for grid_id, cell in self.cells.items():
            explicit_column = cell.get("column", cell.get("col"))
            explicit_row = cell.get("row")
            if explicit_column is not None and explicit_row is not None:
                level = int(cell.get("level") or 0)
                index = level, int(explicit_column), int(explicit_row)
            else:
                match = _GRID_ID.fullmatch(grid_id)
                if match is None:
                    return None
                row = int(match.group("row")) * (
                    1 if match.group("sign") == "P" else -1
                )
                index = int(match.group("level")), int(match.group("column")), row
            if index in seen:
                return None
            result[grid_id] = index
            seen.add(index)
        return result

    def _indexed_adjacency(self):
        reverse = {value: key for key, value in self.indices.items()}
        result = {grid_id: [] for grid_id in self.cells}
        for grid_id, (level, column, row) in self.indices.items():
            for dx, dy in (
                (-1, -1), (-1, 0), (-1, 1), (0, -1),
                (0, 1), (1, -1), (1, 0), (1, 1),
            ):
                other = reverse.get((level, column + dx, row + dy))
                if other is not None:
                    result[grid_id].append(other)
            result[grid_id] = tuple(sorted(result[grid_id]))
        return result

    def _bbox_adjacency(self):
        edge_index = {}
        for grid_id, cell in self.cells.items():
            west, south, east, north = cell["bbox"]
            for axis, value in (("x", west), ("x", east), ("y", south), ("y", north)):
                edge_index.setdefault((axis, round(value, 12)), []).append(grid_id)
        result = {grid_id: set() for grid_id in self.cells}
        for grid_id, cell in self.cells.items():
            west, south, east, north = cell["bbox"]
            candidates = set()
            for axis, value in (("x", west), ("x", east), ("y", south), ("y", north)):
                candidates.update(edge_index.get((axis, round(value, 12)), ()))
            for other in candidates:
                if other != grid_id and _bbox_touches(
                    cell["bbox"], self.cells[other]["bbox"]
                ):
                    result[grid_id].add(other)
        return {grid_id: tuple(sorted(values)) for grid_id, values in result.items()}


def _bbox_touches(a, b):
    x_overlap = min(a[2], b[2]) - max(a[0], b[0])
    y_overlap = min(a[3], b[3]) - max(a[1], b[1])
    tolerance = 1e-11
    return x_overlap >= -tolerance and y_overlap >= -tolerance and (
        abs(x_overlap) <= tolerance or abs(y_overlap) <= tolerance
    )


__all__ = ["GridGraph"]
