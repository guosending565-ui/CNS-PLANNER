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

    #: 层级无关建筑事实获取：按**当前工作区层级**对原始 footprint 做精确几何聚合
    #: （不是跨层级平均、不是插值）。实现位于 GIS 边界
    #: ``cns_planner.gis.building_footprint_aggregation``，语义与既有 L8 事实表生成脚本一致。
    FOOTPRINT_AGGREGATION_METHOD = "exact_footprint_intersection_and_centroid_allocation"
    #: 统一映射结果的取值词汇（passed / partial / unsupported）。
    ENVIRONMENT_MAPPING_STATUSES = ("passed", "partial", "unsupported")

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
            "footprint_aggregation": {
                "available": True,
                "method": cls.FOOTPRINT_AGGREGATION_METHOD,
                "requires_original_footprints": True,
                "requires_geometry_libraries": True,
                "cross_level_averaging": False,
                "cross_level_interpolation": False,
                "note": (
                    "源 footprint 可用时按**当前工作区层级**做精确面几何聚合，"
                    "因此建筑事实不再被 L8 绑死；聚合语义与 L8 事实表生成脚本一致。"
                ),
            },
            "limitations": [
                "只有恰好 L8 的工作区网格能直接映射建筑事实表。",
                "其它层级由源 footprint 精确聚合获得事实；聚合不可用（无 footprint 源 / "
                "无几何库 / 无空间索引）时保持 status=unsupported、逐格 missing_data，"
                "unknown 绝不当 0。",
            ],
            "future_work": "level_independent_building_fact_acquisition",
            "future_work_status": "implemented_via_exact_footprint_aggregation",
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
        result["environment_mapping"] = cls.environment_mapping(result)
        return result

    def map(self, grid, source_path, *, footprint_source=None, footprint_aggregator=None,
            footprint_layer_name=None):
        """建筑环境映射。

        三条互不混淆的路径：

        1. **恰好 L8 且 L8 事实表可用** → 既有的按 ``grid_id`` bbox 直接映射（行为完全不变）；
        2. **层级无关事实获取**（用户明确要求：不论层级都要能拿到可用于航路规划的建筑事实）→
           通过注入的 ``footprint_aggregator`` 按当前工作区层级对原始 footprint 做**精确几何
           聚合**。聚合实现位于 GIS 边界，本层不 import QGIS/shapely；
        3. **降级**：既有的 ``unsupported`` / ``missing_data`` 语义（无源、层级不可用且没有
           聚合能力时）完全保留，绝不伪造建筑事实。

        返回结果始终带统一载体 ``environment_mapping``：
        ``{status, total_cells, covered_cells, unresolved_cells, ...}``。
        """

        cells = list((grid or {}).get("cells") or [])
        level = (grid or {}).get("level")
        if not cells:
            return self.empty()

        l8_result = None
        if level == 8 and source_path:
            l8_result = self._map_l8(grid, cells, level, source_path)
            if l8_result.get("status") != "failed":
                return self._with_environment_mapping(l8_result)

        aggregated, aggregation_error = self._aggregate(
            cells, level, footprint_source, footprint_aggregator, footprint_layer_name,
        )
        if aggregated is not None:
            return self._with_environment_mapping(aggregated)

        if l8_result is not None:
            return self._with_environment_mapping(l8_result, aggregation_error=aggregation_error)
        if level != 8:
            result = self._unsupported(cells, level, source_path)
        elif not source_path:
            result = self._missing(cells, level, None, "未配置 L8 建筑环境网格")
        else:
            result = self._missing(
                cells, level, {"path": str(source_path)}, "L8 建筑环境网格不可用",
            )
        return self._with_environment_mapping(result, aggregation_error=aggregation_error)

    # ------------------------------------------------------------------ 层级无关聚合

    @classmethod
    def _aggregate(cls, cells, level, footprint_source, aggregator, layer_name):
        """按当前层级对原始 footprint 做精确聚合；不可用时返回 ``(None, 原因)``。"""

        if not footprint_source:
            return None, None
        if aggregator is None:
            return None, "footprint_aggregator_not_configured"
        try:
            result = aggregator(
                cells, footprint_source, grid_level=level, layer_name=layer_name,
            )
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if not isinstance(result, dict) or not result.get("cells"):
            return None, "footprint_aggregator_returned_no_cells"
        return result, None

    # ------------------------------------------------------------------ 统一结果载体

    @classmethod
    def _with_environment_mapping(cls, result, *, aggregation_error=None):
        result["environment_mapping"] = cls.environment_mapping(
            result, aggregation_error=aggregation_error,
        )
        return result

    @classmethod
    def environment_mapping(cls, result, *, aggregation_error=None):
        """统一的 ``BuildingEnvironmentMappingResult``。

        字段：``total_cells`` / ``covered_cells`` / ``unresolved_cells`` / ``status``。
        状态词汇固定为 ``passed`` / ``partial`` / ``unsupported``：

        * ``passed``：网格内每一格都有明确建筑事实（含"范围内确认无建筑"的已知 0 语义）；
        * ``partial``：部分格有事实、部分格无法判定（``unknown`` 绝不当 0）；
        * ``unsupported``：一格都拿不到（源缺失、不可读、层级不适用且无聚合能力）。

        本方法**只做归一化统计**，不重算任何建筑数值，也不改变既有 ``status`` / ``cells``。
        """

        result = result if isinstance(result, dict) else {}
        cells = result.get("cells") if isinstance(result.get("cells"), dict) else {}
        total = int(result.get("count") or len(cells))
        covered = 0
        reasons = {}
        for cell in cells.values():
            if not isinstance(cell, dict):
                continue
            if str(cell.get("status") or "") == "passed":
                covered += 1
                continue
            reason = str(cell.get("reason") or cell.get("status") or "unresolved")
            reasons[reason] = reasons.get(reason, 0) + 1
        unresolved = max(0, total - covered)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        aggregation_method = metadata.get("aggregation_method")
        if aggregation_method:
            basis = aggregation_method
        elif result.get("grid_level") == 8 and result.get("source"):
            basis = "l8_building_grid_fact_table"
        else:
            basis = None
        result_status = str(result.get("status") or "")
        if total and not unresolved:
            status = "passed"
        elif covered:
            status = "partial"
        else:
            status = "unsupported"
        mapping = {
            "status": status,
            "total_cells": total,
            "covered_cells": covered,
            "unresolved_cells": unresolved,
            "coverage_ratio": (covered / total) if total else None,
            "mapping_basis": basis,
            "grid_level": result.get("grid_level"),
            "level_aligned": result.get("grid_level") == 8,
            "level_independent_facts": bool(aggregation_method),
            "source_available": bool(result.get("source")) and result_status != "failed",
            "facts_available": covered > 0,
            "participates_in_planner": covered > 0,
            "unresolved_reasons": reasons,
            "mapping_status": result_status or "not_calculated",
            "algorithm_id": result.get("algorithm_id"),
            "algorithm_version": result.get("algorithm_version"),
        }
        if aggregation_error:
            mapping["aggregation_error"] = str(aggregation_error)
        return mapping

    def _map_l8(self, grid, cells, level, source_path):
        """既有的"恰好 L8 直接映射"实现（本轮除拆出方法外未做任何修改）。"""

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
