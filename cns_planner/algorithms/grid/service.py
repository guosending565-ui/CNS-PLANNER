"""Workspace adapter for the existing MH/T 4063.1 planar grid utilities."""

from __future__ import annotations

from fractions import Fraction
import math
import os

from .mht4063 import LEVEL_SIZE_DEGREES, cell_bounds, validate_level


#: Default ceiling on standard cells per workspace.  It is unchanged from the historic
#: behaviour; ``CNS_GRID_MAX_CELLS`` only overrides the *default*, and an explicit
#: ``max_cells`` argument always wins.
DEFAULT_MAX_CELLS = 5000
MAX_CELLS_ENV = "CNS_GRID_MAX_CELLS"

#: Sphere radius used for the **display** metadata below.  It is the same constant the
#: existing ``distance_m`` helper uses, so the reported grid edge length and every other
#: distance in the workspace agree with each other.
EARTH_RADIUS_M = 6371008.8

#: How the metre-valued cell size is derived from the authoritative degree-valued level table.
CELL_SIZE_MEASUREMENT = "geodesic_edge_lengths_from_mht4063_degree_span_at_cell_centre_latitude"
CELL_SIZE_SOURCE = "mht4063_level_size_degrees"


def cell_size_metadata(level, *, latitude_deg):
    """Metre-valued cell geometry for one MH/T level at one latitude (BUG-GRID-001).

    MH/T 4063.1 defines every planar level as a **square in degrees**, which is *not* a
    square in metres: a longitude degree is shorter than a latitude degree by ``cos(lat)``.
    The authoritative definition therefore stays ``LEVEL_SIZE_DEGREES`` and this helper only
    *reports* the resulting metre edges of that same definition -- it never invents a second
    grid definition, and the frontend must never re-derive it from raw degrees.

    * ``resolution_x`` -- east-west edge length in metres at ``latitude_deg``;
    * ``resolution_y`` -- north-south edge length in metres (independent of longitude);
    * ``cell_size_m`` -- the area-equivalent edge length ``sqrt(resolution_x * resolution_y)``,
      i.e. a single representative size for a cell that is not square in metres.
    """

    validate_level(level)
    lon_span, lat_span = LEVEL_SIZE_DEGREES[level]
    resolution_y = math.radians(float(lat_span)) * EARTH_RADIUS_M
    resolution_x = (
        math.radians(float(lon_span)) * EARTH_RADIUS_M
        * math.cos(math.radians(float(latitude_deg)))
    )
    return {
        "cell_size_m": math.sqrt(resolution_x * resolution_y),
        "resolution_x": resolution_x,
        "resolution_y": resolution_y,
    }


def level_metadata(level, *, latitude_deg):
    """The single, additive grid-level metadata block consumed by the UI (BUG-GRID-001)."""

    lon_span, lat_span = LEVEL_SIZE_DEGREES[level]
    measured = cell_size_metadata(level, latitude_deg=latitude_deg)
    return {
        "level": int(level),
        **{key: round(float(value), 9) for key, value in measured.items()},
        "unit": "m",
        "cell_size_degrees": [float(lon_span), float(lat_span)],
        "reference_latitude_deg": round(float(latitude_deg), 9),
        "measurement": CELL_SIZE_MEASUREMENT,
        "source": CELL_SIZE_SOURCE,
        # A level is square in degrees, not in metres: the two edges differ by cos(latitude).
        "square_in_degrees_not_in_metres": True,
        "never_estimated_on_the_frontend": True,
    }


def _resolve_max_cells(value):
    if value is not None:
        candidate = value
    else:
        candidate = os.environ.get(MAX_CELLS_ENV)
        if candidate is None or not str(candidate).strip():
            return DEFAULT_MAX_CELLS
    try:
        resolved = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_cells 必须是正整数") from exc
    if resolved < 1:
        raise ValueError("max_cells 必须是正整数")
    return resolved


class WorkspaceGridService:
    """Select standard cells intersecting a workspace without GIS dependencies."""

    standard = "MH/T 4063.1-2026"
    id_scheme = "mht4063-global-index-v1"

    def __init__(self, preferred_level: int = 7, max_cells=None):
        validate_level(preferred_level)
        self.preferred_level = preferred_level
        self.max_cells = _resolve_max_cells(max_cells)

    def empty(self) -> dict:
        return {
            "status": "not_calculated",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "preferred_level": self.preferred_level,
            "max_cells": self.max_cells,
            "level": None,
            "coarsened": False,
            "workspace_bbox": None,
            "cell_size_degrees": None,
            "level_metadata": None,
            "count": 0,
            "cells": [],
            "capabilities": self.capabilities(),
        }

    def capabilities(self, *, preferred_level=None, max_cells=None):
        """能力声明：``available_levels`` / ``preferred_level``，**不改变生成行为**。

        工作区网格可以生成任意 MH/T 标准层级；但只有 ``preferred_level``（默认 L7）是
        默认选择，且在 ``max_cells`` 之下会被静默 coarsen。这里只把这两件事说清楚。
        """

        level = self.preferred_level if preferred_level is None else preferred_level
        validate_level(level)
        limit = self.max_cells if max_cells is None else _resolve_max_cells(max_cells)
        return {
            "role": "workspace_grid",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "available_levels": list(LEVEL_SIZE_DEGREES),
            "preferred_level": level,
            "instance_preferred_level": self.preferred_level,
            "max_cells": limit,
            "coarsening": "silent_step_down_until_cell_count_within_max_cells",
            "coarsening_is_explicit_in_response": True,
            "coarsened_flag_key": "coarsened",
            "explicit_level_request_supported": True,
            # BUG-GRID-001：格网尺寸只有这一个来源，前端不得自行按经纬度估算。
            "level_metadata_key": "level_metadata",
            "level_metadata_units": "m",
            "level_metadata_derived_from": CELL_SIZE_SOURCE,
            "level_metadata_describes": "the_level_actually_generated_after_coarsening",
            "frontend_must_not_estimate_cell_size": True,
            "limitations": [
                "请求的层级可能被静默 coarsen 到更粗层级（响应中的 coarsened=true 记录了这一事实）。",
                "需要恰好 L8 的下游（building_grid 事实表）在大工作区上因此不可用。",
            ],
        }

    def generate(self, workspace_bbox, preferred_level: int | None = None,
                 max_cells: int | None = None) -> dict:
        """Standard cells for ``workspace_bbox`` at the finest level within the limit.

        ``max_cells`` lets a caller explicitly raise (or lower) the per-workspace cell ceiling
        for one request — required whenever a finer level (e.g. L8, which is the only level the
        L8 building-environment facts can be mapped onto) must actually be selected instead of
        being silently coarsened away.
        """

        limit = _resolve_max_cells(max_cells) if max_cells is not None else self.max_cells
        west, south, east, north = self._validate_bbox(workspace_bbox)
        requested_level = self.preferred_level if preferred_level is None else preferred_level
        validate_level(requested_level)

        selected = None
        for level in range(requested_level, 0, -1):
            column_range, row_range = self._index_ranges((west, south, east, north), level)
            count = len(column_range) * len(row_range)
            if count <= limit:
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
        # The metadata always describes the level that was actually generated (post
        # coarsening), never the requested one: a silently coarsened workspace must never
        # report a finer cell size than the cells it really contains.
        reference_latitude = (south + north) / 2.0
        return {
            "status": "passed",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "preferred_level": requested_level,
            "max_cells": limit,
            "level": level,
            "coarsened": level != requested_level,
            "workspace_bbox": [west, south, east, north],
            "cell_size_degrees": [float(lon_size), float(lat_size)],
            "level_metadata": level_metadata(level, latitude_deg=reference_latitude),
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
            # Per-cell metre geometry from the **same** authoritative level table.  The UI
            # displays / measures with this instead of estimating a size from raw degrees.
            **{
                key: round(float(value), 9)
                for key, value in cell_size_metadata(
                    level, latitude_deg=(south + north) / 2.0
                ).items()
            },
        }

    @staticmethod
    def _grid_id(level, column, row):
        row_code = f"P{row:08d}" if row >= 0 else f"M{abs(row):08d}"
        return f"MHT4063-L{level:02d}-C{column:08d}-R{row_code}"
