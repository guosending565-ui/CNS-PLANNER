"""Map source population raster values onto existing standard grid IDs."""

from __future__ import annotations

from .raster_grid_adapter import GdalRasterAdapter


class PopulationGridService:
    algorithm_id = "population-grid-raw-statistics"
    sampling_version = "1.0"

    def __init__(self, adapter_factory=GdalRasterAdapter):
        self.adapter_factory = adapter_factory

    @classmethod
    def empty(cls, status="not_calculated", source=None, message=None):
        result = {
            "status": status,
            "source": source,
            "algorithm_id": cls.algorithm_id,
            "sampling_version": cls.sampling_version,
            "grid_level": None,
            "count": 0,
            "covered_count": 0,
            "cells": {},
        }
        if message:
            result["message"] = message
        return result

    def map(self, grid, source_path):
        cells = list((grid or {}).get("cells") or [])
        if not cells:
            return self.empty()
        if not source_path:
            return self._missing(cells, grid.get("level"), None, "未配置人口栅格")
        try:
            adapter = self.adapter_factory(source_path)
            source = adapter.describe()
            unit = source.get("unit_metadata")
            attributes = {}
            covered = 0
            for cell in cells:
                values = adapter.read_values(cell["bbox"])
                if values:
                    covered += 1
                    attributes[cell["grid_id"]] = {
                        "status": "passed",
                        "valid_sample_count": len(values),
                        "value_sum": sum(values),
                        "value_mean": sum(values) / len(values),
                        "value_min": min(values),
                        "value_max": max(values),
                    }
                else:
                    attributes[cell["grid_id"]] = self._empty_cell()
            return {
                "status": "passed" if covered == len(cells) else "missing_data",
                "source": source,
                "algorithm_id": self.algorithm_id,
                "sampling_version": self.sampling_version,
                "value_unit": unit,
                "unit_status": "verified_from_raster_metadata" if unit else "unverified",
                "interpretation": "source_values_only",
                "grid_level": grid.get("level"),
                "count": len(cells),
                "covered_count": covered,
                "cells": attributes,
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return self._missing(cells, grid.get("level"), {"path": str(source_path)}, str(exc), "failed")

    def _missing(self, cells, level, source, message, status="missing_data"):
        result = self.empty(status, source, message)
        result["grid_level"] = level
        result["count"] = len(cells)
        result["cells"] = {cell["grid_id"]: self._empty_cell() for cell in cells}
        result["value_unit"] = None
        result["unit_status"] = "unverified"
        result["interpretation"] = "source_values_only"
        return result

    @staticmethod
    def _empty_cell():
        return {
            "status": "missing_data",
            "valid_sample_count": 0,
            "value_sum": None,
            "value_mean": None,
            "value_min": None,
            "value_max": None,
        }
