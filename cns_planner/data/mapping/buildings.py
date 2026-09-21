"""Direct L8 building-environment mapping by canonical cell bounds."""

from __future__ import annotations

import math
import sqlite3

from ...gis.source_inspection import inspect_geopackage


class BuildingGridService:
    algorithm_id = "building-grid-direct-l8"
    algorithm_version = "1.0"

    #: 本服务**能正确映射**的网格层级。``zhoushan_building_grid_L8.gpkg`` 是预先按 L8
    #: 聚合好的事实表，跨层级平均/插值是明确禁止的，所以这里只有 L8。
    AVAILABLE_LEVELS = (8,)
    #: 默认层级：调用方不显式指定时必须使用的层级。
    PREFERRED_LEVEL = 8
    #: 未选定层级时的兜底层级（与 ``PREFERRED_LEVEL`` 一致，保留别名便于调用方表达意图）。
    DEFAULT_LEVEL = 8
    #: 与工作区网格的已知落差（只声明，不在本轮改变行为）。
    #: ``WorkspaceGridService`` 默认 ``preferred_level=7``；二者不一致时工作区会被
    #: coarsen 到 L6/L7，建筑事实整表不可用（详见 docs/10-Phase3已知限制与待办.md §1）。
    PREFERRED_WORKSPACE_LEVEL_ALIGNMENT = "not_aligned_workspace_default_is_7"

    @classmethod
    def capabilities(cls, *, source_path=None, declared_level=None):
        """能力声明：``available_levels`` / ``preferred_level``，**不改变任何映射行为**。

        现有调用方（``map(grid, source_path)`` 与 ``level != 8 ⇒ unsupported``）保持完全
        不变；本方法只是把既有契约显式化，让 readiness / UI / 测试不必再猜层级。
        """

        available = list(cls.AVAILABLE_LEVELS)
        level = declared_level if declared_level is not None else cls.PREFERRED_LEVEL
        usable = level in cls.AVAILABLE_LEVELS
        return {
            "role": "building_grid",
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "available_levels": available,
            "preferred_level": cls.PREFERRED_LEVEL,
            "default_level": cls.DEFAULT_LEVEL,
            "source_declared_level": declared_level,
            "declared_level_usable": usable,
            "source_path_configured": bool(source_path),
            "mapping": "exact_bounds_not_grid_key",
            "cross_level_aggregation_allowed": False,
            "cross_level_interpolation_allowed": False,
            "unusable_reason": None if usable else "unsupported_grid_level",
            "unusable_message": (
                None if usable else
                "building_grid 当前仅允许 L8 直接映射；禁止跨层级平均或插值"
            ),
            "workspace_level_alignment": cls.PREFERRED_WORKSPACE_LEVEL_ALIGNMENT,
            "limitations": [
                "只有恰好 L8 的工作区网格能直接映射建筑事实表。",
                "工作区被 coarsen 到其他层级时 status=unsupported、逐格 missing_data，"
                "unknown 绝不当 0。",
                "层级无关的建筑事实获取是已记录的后续需求，本轮不实现。",
            ],
            "future_work": "level_independent_building_fact_acquisition",
        }

    @classmethod
    def empty(cls, status="not_calculated", source=None, message=None):
        result = {
            "status": status, "source": source,
            "algorithm_id": cls.algorithm_id, "algorithm_version": cls.algorithm_version,
            "grid_level": None, "count": 0, "covered_count": 0,
            "metadata": {}, "cells": {},
        }
        if message:
            result["message"] = message
        return result

    def map(self, grid, source_path):
        cells = list((grid or {}).get("cells") or [])
        level = (grid or {}).get("level")
        if not cells:
            return self.empty()
        if level != 8:
            return self._unsupported(cells, level, source_path)
        if not source_path:
            return self._missing(cells, level, None, "未配置 L8 建筑环境网格")
        try:
            metadata = inspect_geopackage(source_path, "building_grid")
            rows = self._rows(metadata, grid.get("workspace_bbox"), source_path)
            by_bounds = {self._key(row[0:4]): row[4:] for row in rows}
            extent = metadata["extent"]
            mapped, covered = {}, 0
            for cell in cells:
                bbox = cell["bbox"]
                row = by_bounds.get(self._key(bbox))
                inside = _contains(extent, bbox)
                if row is not None:
                    mapped[cell["grid_id"]] = self._cell(*row)
                    covered += 1
                elif inside:
                    mapped[cell["grid_id"]] = self._zero_cell()
                    covered += 1
                else:
                    mapped[cell["grid_id"]] = self._missing_cell("outside_coverage")
            status = "passed" if covered == len(cells) else "missing_data"
            return {
                "status": status,
                "source": {key: metadata.get(key) for key in (
                    "path", "layer", "crs", "extent", "feature_count", "geometry_type",
                )},
                "algorithm_id": self.algorithm_id,
                "algorithm_version": self.algorithm_version,
                "grid_level": level, "count": len(cells), "covered_count": covered,
                "metadata": {
                    "mapping": "exact_bounds_not_grid_key",
                    "source_grid_level": 8,
                    "zero_semantics": "inside_declared_source_extent_without_feature",
                    "outside_semantics": "missing_data",
                    "fields": sorted(metadata["fields"]),
                },
                "cells": mapped,
            }
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            return self._missing(cells, level, {"path": str(source_path)}, str(exc), "failed")

    @staticmethod
    def _rows(metadata, bbox, source_path):
        table = _quote(metadata["layer"])
        rtree = _quote(metadata["rtree_table"])
        west, south, east, north = (float(value) for value in bbox)
        connection = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        try:
            sql = (
                f"SELECT g.west,g.south,g.east,g.north,g.building_count,g.building_area_m2,"
                f"g.building_coverage_ratio,g.height_mean_m,g.height_p95_m,g.height_max_m,"
                f"g.valid_height_fraction FROM {table} g JOIN {rtree} r ON r.id=g.fid "
                "WHERE r.maxx>? AND r.minx<? AND r.maxy>? AND r.miny<?"
            )
            return connection.execute(sql, (west, east, south, north)).fetchall()
        finally:
            connection.close()

    @staticmethod
    def _key(bbox):
        return tuple(round(float(value), 9) for value in bbox)

    @staticmethod
    def _cell(count, area, ratio, mean, p95, maximum, valid_fraction):
        ratio = _finite(ratio)
        return {
            "status": "passed", "building_count": int(count or 0),
            "building_area_m2": _finite(area) or 0.0,
            "building_coverage_ratio": ratio if ratio is not None else 0.0,
            "height_mean_m": _finite(mean), "height_p95_m": _finite(p95),
            "height_max_m": _finite(maximum),
            "valid_height_fraction": _finite(valid_fraction),
            "building_exposure": max(0.0, min(1.0, ratio or 0.0)),
        }

    @classmethod
    def _zero_cell(cls):
        return cls._cell(0, 0.0, 0.0, None, None, None, None)

    @staticmethod
    def _missing_cell(reason):
        return {
            "status": "missing_data", "reason": reason,
            "building_count": None, "building_area_m2": None,
            "building_coverage_ratio": None, "height_mean_m": None,
            "height_p95_m": None, "height_max_m": None,
            "valid_height_fraction": None, "building_exposure": None,
        }

    def _unsupported(self, cells, level, source):
        result = self.empty("unsupported", {"path": str(source)} if source else None,
                            "building_grid 当前仅允许 L8 直接映射；禁止跨层级平均或插值")
        result.update({"grid_level": level, "count": len(cells), "cells": {
            cell["grid_id"]: self._missing_cell("unsupported_grid_level") for cell in cells
        }})
        return result

    def _missing(self, cells, level, source, message, status="missing_data"):
        result = self.empty(status, source, message)
        result.update({"grid_level": level, "count": len(cells), "cells": {
            cell["grid_id"]: self._missing_cell("source_unavailable") for cell in cells
        }})
        return result


def _finite(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _contains(outer, inner):
    return (
        outer and all(value is not None for value in outer)
        and outer[0] <= inner[0] and outer[1] <= inner[1]
        and outer[2] >= inner[2] and outer[3] >= inner[3]
    )


def _quote(identifier):
    return '"' + str(identifier).replace('"', '""') + '"'
