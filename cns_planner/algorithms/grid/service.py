"""Workspace adapter for the existing MH/T 4063.1 planar grid utilities."""

from __future__ import annotations

from fractions import Fraction
import math

from .mht4063 import LEVEL_SIZE_DEGREES, cell_bounds, validate_level


class WorkspaceGridService:
    """Select standard cells intersecting a workspace without GIS dependencies."""

    standard = "MH/T 4063.1-2026"
    id_scheme = "mht4063-global-index-v1"

    def __init__(self, preferred_level: int = 7, max_cells: int = 5000):
        validate_level(preferred_level)
        if not isinstance(max_cells, int) or max_cells < 1:
            raise ValueError("max_cells 必须是正整数")
        self.preferred_level = preferred_level
        self.max_cells = max_cells

    def empty(self) -> dict:
        return {
            "status": "not_calculated",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "preferred_level": self.preferred_level,
            "level": None,
            "coarsened": False,
            "workspace_bbox": None,
            "cell_size_degrees": None,
            "count": 0,
            "cells": [],
        }

    def generate(self, workspace_bbox, preferred_level: int | None = None) -> dict:
        west, south, east, north = self._validate_bbox(workspace_bbox)
        requested_level = self.preferred_level if preferred_level is None else preferred_level
        validate_level(requested_level)

        selected = None
        for level in range(requested_level, 0, -1):
            column_range, row_range = self._index_ranges((west, south, east, north), level)
            count = len(column_range) * len(row_range)
            if count <= self.max_cells:
                selected = level, column_range, row_range
                break
        if selected is None:
            raise ValueError("工作区标准网格数量超过限制")

        level, column_range, row_range = selected
        cells = [
            self._cell(level, column, row)
            for row in row_range
            for column in column_range
        ]
        lon_size, lat_size = LEVEL_SIZE_DEGREES[level]
        return {
            "status": "passed",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "preferred_level": requested_level,
            "level": level,
            "coarsened": level != requested_level,
            "workspace_bbox": [west, south, east, north],
            "cell_size_degrees": [float(lon_size), float(lat_size)],
            "count": len(cells),
            "cells": cells,
        }

    @staticmethod
    def _validate_bbox(workspace_bbox) -> tuple[float, float, float, float]:
        if not isinstance(workspace_bbox, (list, tuple)) or len(workspace_bbox) != 4:
            raise ValueError("工作区必须包含西、南、东、北四个坐标")
        values = tuple(float(value) for value in workspace_bbox)
        west, south, east, north = values
        if not all(math.isfinite(value) for value in values):
            raise ValueError("工作区坐标必须是有限数值")
        if not (-180 <= west < east <= 180 and -88 < south < north < 88):
            raise ValueError("工作区超出当前 MH/T 4063.1 非极地区范围")
        return west, south, east, north

    @staticmethod
    def _index_ranges(bbox, level):
        west, south, east, north = (Fraction(str(value)) for value in bbox)
        lon_size, lat_size = LEVEL_SIZE_DEGREES[level]
        lon_origin = Fraction(-180, 1)
        lat_origin = Fraction(0, 1)

        first_column = int((west - lon_origin) // lon_size)
        last_column = WorkspaceGridService._last_intersecting_index(east, lon_origin, lon_size)
        first_row = int((south - lat_origin) // lat_size)
        last_row = WorkspaceGridService._last_intersecting_index(north, lat_origin, lat_size)
        return range(first_column, last_column + 1), range(first_row, last_row + 1)

    @staticmethod
    def _last_intersecting_index(value, origin, size):
        quotient, remainder = divmod(value - origin, size)
        return int(quotient - 1 if remainder == 0 else quotient)

    @classmethod
    def _cell(cls, level, column, row):
        lon_size, lat_size = LEVEL_SIZE_DEGREES[level]
        center_lon = Fraction(-180, 1) + (Fraction(column, 1) + Fraction(1, 2)) * lon_size
        center_lat = (Fraction(row, 1) + Fraction(1, 2)) * lat_size
        west, south, east, north = cell_bounds(float(center_lon), float(center_lat), level)
        grid_id = cls._grid_id(level, column, row)
        ring = [
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]
        return {
            "grid_id": grid_id,
            "level": level,
            "bbox": [west, south, east, north],
            "center": [(west + east) / 2, (south + north) / 2],
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    @staticmethod
    def _grid_id(level, column, row):
        row_code = f"P{row:08d}" if row >= 0 else f"M{abs(row):08d}"
        return f"MHT4063-L{level:02d}-C{column:08d}-R{row_code}"
