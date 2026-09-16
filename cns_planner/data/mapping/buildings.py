"""Direct L8 building-environment mapping by canonical cell bounds."""

from __future__ import annotations

import math
import sqlite3

from ...gis.source_inspection import inspect_geopackage


class BuildingGridService:
    algorithm_id = "building-grid-direct-l8"
    algorithm_version = "1.0"

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
