"""Workspace adapter for the existing MH/T 4063.1 planar grid utilities."""

from __future__ import annotations

from fractions import Fraction
import math
import os

from .mht4063 import LEVEL_SIZE_DEGREES, cell_bounds, validate_level


#: 正式业务（canonical operational）空间索引层级：MH/T 4063.1 L8。
#:
#: 人口 / 地形 / 建筑 / Risk Framework V2 / population×shelter / tower feasibility /
#: Theta* V2 全部以工作区的**实际 L8** 网格为 canonical spatial index；L8 建筑环境事实表
#: 也只能映射到恰好 L8 上。因此正式工作流不再把 L6/L7 当作自动降级目标。
OPERATIONAL_GRID_LEVEL = 8

#: 正式工作流的网格数量上限（软件资源保护阈值，**不是**空间工程参数）。
#:
#: 它只保护内存/渲染/搜索预算；它绝不改变 MH/T 编码、分辨率或任何空间语义。
#: ``CNS_GRID_MAX_CELLS`` 只覆盖**默认值**，显式 ``max_cells`` 参数始终优先。
DEFAULT_MAX_CELLS = 12000
MAX_CELLS_ENV = "CNS_GRID_MAX_CELLS"

#: 正式入口的语义标签：恰好 canonical 层级，超限即阻断，绝不 silent coarsen。
OPERATIONAL_GRID_SEMANTICS = "strict_canonical_level_no_silent_coarsening"

#: 正式入口被阻断时的可读原因码。
OPERATIONAL_GRID_BLOCKED_CODE = "operational_grid_cell_ceiling_exceeded"

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


def resolve_operational_level(value):
    """正式入口的层级参数解析：``None`` 或 canonical L8，其它层级**明确拒绝**。

    正式工作流不再向用户提供 L6/L7 选择，因此这里既不静默改成 L8，也不接受 L6/L7：
    非 canonical 取值一律抛可读错误，避免"用户选了 L7、系统却做了别的事"这种模糊状态。
    L6/L7 的**算法**仍然存在，但只能通过底层 ``generate()`` 显式用于
    legacy / unit test / diagnostic。
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return OPERATIONAL_GRID_LEVEL
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"标准规划网格层级必须是 MH/T 4063.1 L{OPERATIONAL_GRID_LEVEL}"
        ) from exc
    if level != OPERATIONAL_GRID_LEVEL:
        raise ValueError(
            f"正式工作流只使用 MH/T 4063.1 L{OPERATIONAL_GRID_LEVEL}；"
            f"L{level} 只保留给 legacy / unit test / diagnostic，"
            "不再作为正式工作流的自动降级目标。"
        )
    return level


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


class OperationalGridBlockedError(ValueError):
    """正式工作流无法在 canonical 层级（L8）上表达该工作区。

    这是**明确的阻断**，不是失败后的降级：调用方必须缩小工作区或显式提高资源上限，
    绝不能拿 L7/L6 网格继续业务流程。``details`` 携带结构化原因（供 API / UI 如实展示）。
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = dict(details or {})


class WorkspaceGridService:
    """Select standard cells intersecting a workspace without GIS dependencies."""

    standard = "MH/T 4063.1-2026"
    id_scheme = "mht4063-global-index-v1"

    def __init__(self, preferred_level: int = OPERATIONAL_GRID_LEVEL, max_cells=None):
        validate_level(preferred_level)
        self.preferred_level = preferred_level
        self.max_cells = _resolve_max_cells(max_cells)

    def empty(self) -> dict:
        return {
            "status": "not_calculated",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            # 正式业务 canonical 层级：即使还没有网格，界面也必须能如实说明"标准规划网格 = L8"。
            "canonical_level": OPERATIONAL_GRID_LEVEL,
            "operational_level": OPERATIONAL_GRID_LEVEL,
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
        """能力声明：层级、coarsen 语义与正式入口的 strict 语义。

        **两层语义必须分清**（这正是 GRID-L8-UNIFICATION 的收口点）：

        * 正式业务入口（:meth:`generate_operational`，以及 ``WorkspaceService`` /
          正式 API）：恰好 ``OPERATIONAL_GRID_LEVEL``（L8），超限即阻断，
          **不允许 silent coarsening**；
        * 底层通用算法（:meth:`generate`，legacy / unit test / diagnostic）：
          仍然支持逐级下降的多层级能力，本轮不删除，但不再是正式工作流的降级目标。
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
            "canonical_operational_level": OPERATIONAL_GRID_LEVEL,
            "operational_level": OPERATIONAL_GRID_LEVEL,
            "operational_semantics": OPERATIONAL_GRID_SEMANTICS,
            "operational_entry": "generate_operational",
            "operational_silent_coarsening_allowed": False,
            "operational_blocked_on_ceiling": True,
            "operational_blocked_code": OPERATIONAL_GRID_BLOCKED_CODE,
            "max_cells": limit,
            "max_cells_is_resource_guard_only": True,
            "max_cells_is_spatial_parameter": False,
            # 底层通用 generate() 的多层级 coarsening 能力保留（legacy / tests / diagnostics）。
            "coarsening": "silent_step_down_until_cell_count_within_max_cells",
            "coarsening_scope": "legacy_generate_only",
            "coarsening_is_explicit_in_response": True,
            "coarsened_flag_key": "coarsened",
            "explicit_level_request_supported": True,
            # BUG-GRID-001：格网尺寸只有这一个来源，前端不得自行按经纬度估算。
            "level_metadata_key": "level_metadata",
            "level_metadata_units": "m",
            "level_metadata_derived_from": CELL_SIZE_SOURCE,
            "level_metadata_describes": "the_level_actually_generated",
            "frontend_must_not_estimate_cell_size": True,
            "limitations": [
                "正式工作流只使用 L8；请求层级与实际上限冲突时返回 blocked，绝不降级。",
                "底层 generate() 仍可按 legacy 语义逐级下降（仅限 test / diagnostic / 历史项目回放）。",
            ],
        }

    def generate_operational(self, workspace_bbox, *, level=None, max_cells: int | None = None) -> dict:
        """**正式业务入口**：在 canonical 层级（L8）上生成工作区标准网格。

        与 :meth:`generate` 的关键差别只有一条：**禁止 silent coarsening**。

        * 预计 L8 cell_count <= ``max_cells`` → 生成实际 L8
          （``level == preferred_level == 8``、``coarsened is False``）；
        * 预计 L8 cell_count >  ``max_cells`` → 返回 ``status="blocked"`` 的**显式**结果，
          **不生成任何 L7/L6 网格**，也不返回伪 passed。调用方据此阻断业务流程，
          并提示用户缩小工作区或显式提高资源上限。

        ``level`` 只接受 ``None`` / ``8``（正式 UI 已不再暴露 L6/L7）；其它取值由
        :func:`resolve_operational_level` 明确拒绝。返回的 blocked 结果仍然携带
        ``canonical_level`` / ``preferred_level`` / ``max_cells`` / ``required_cells``
        与结构化 ``error``，便于 UI 如实显示原因。
        """

        limit = _resolve_max_cells(max_cells) if max_cells is not None else self.max_cells
        west, south, east, north = self._validate_bbox(workspace_bbox)
        level = resolve_operational_level(level)
        column_range, row_range = self._index_ranges((west, south, east, north), level)
        count = len(column_range) * len(row_range)
        if count > limit:
            return {
                "status": "blocked",
                "standard": self.standard,
                "id_scheme": self.id_scheme,
                "canonical_level": level,
                "operational_level": level,
                "preferred_level": level,
                "level": None,
                "coarsened": False,
                "coarsening_allowed": False,
                "max_cells": limit,
                "workspace_bbox": [west, south, east, north],
                "cell_size_degrees": None,
                "level_metadata": None,
                "required_cells": count,
                "count": 0,
                "cells": [],
                "blocked": True,
                "blocked_code": OPERATIONAL_GRID_BLOCKED_CODE,
                "blocked_message": self._ceiling_message(count, limit),
                "error": {
                    "code": OPERATIONAL_GRID_BLOCKED_CODE,
                    "message": self._ceiling_message(count, limit),
                    "level": level,
                    "required_cells": count,
                    "max_cells": limit,
                    "coarsened": False,
                    "fallback_level_generated": None,
                    "suggestions": [
                        "缩小工作区范围，使 L8 网格数量不超过资源上限。",
                        "或显式提高 max_cells（仅提高软件资源上限，不改变任何空间语义）。",
                    ],
                },
                "capabilities": self.capabilities(max_cells=limit),
            }

        cells = [
            self._cell(level, column, row)
            for row in row_range
            for column in column_range
        ]
        lon_size, lat_size = LEVEL_SIZE_DEGREES[level]
        reference_latitude = (south + north) / 2.0
        return {
            "status": "passed",
            "standard": self.standard,
            "id_scheme": self.id_scheme,
            "canonical_level": level,
            "operational_level": level,
            "preferred_level": level,
            "level": level,
            "coarsened": False,
            "coarsening_allowed": False,
            "max_cells": limit,
            "workspace_bbox": [west, south, east, north],
            "cell_size_degrees": [float(lon_size), float(lat_size)],
            "level_metadata": level_metadata(level, latitude_deg=reference_latitude),
            "count": len(cells),
            "cells": cells,
            "blocked": False,
            "capabilities": self.capabilities(max_cells=limit),
        }

    @staticmethod
    def _ceiling_message(required_cells, max_cells):
        return (
            f"工作区在 MH/T 4063.1 L{OPERATIONAL_GRID_LEVEL} 下需要 {required_cells} 格，"
            f"超过当前资源上限 {max_cells} 格。正式工作流不允许静默降级到 L7/L6："
            "请缩小工作区范围，或显式提高 max_cells 资源上限。"
        )


    def generate(self, workspace_bbox, preferred_level: int | None = None,
                 max_cells: int | None = None) -> dict:
        """**底层通用算法**：在 ``max_cells`` 之内选择最细的层级（legacy 语义）。

        本方法保留逐级下降的多层级能力，供 legacy / unit test / diagnostic 使用；
        正式业务入口是 :meth:`generate_operational`（恰好 L8，超限即阻断）。

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
