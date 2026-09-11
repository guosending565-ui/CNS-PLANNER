"""Map potential CPA conflict events onto existing grid IDs."""

from __future__ import annotations

from copy import deepcopy
import math

from .grid_spatial_index import GridBboxIndex


class ConflictGridService:
    algorithm_id = "uav-conflict-grid-exposure-v1"
    algorithm_version = "1.0"
    default_normalization = {"mode": "dataset_quantile", "quantile": 0.95, "value": None}

    @classmethod
    def empty(cls, status="not_calculated", source=None):
        return {
            "status": status, "source": source,
            "algorithm_id": cls.algorithm_id, "algorithm_version": cls.algorithm_version,
            "grid_level": None, "count": 0, "covered_count": 0,
            "simulation_seconds": None,
            "normalization": deepcopy(cls.default_normalization), "cells": {},
        }

    def map(self, grid, detection, simulation_seconds, parameters=None):
        cells = list((grid or {}).get("cells") or [])
        if not cells:
            return self.empty()
        duration = float(simulation_seconds or 0)
        if duration <= 0:
            raise ValueError("仿真时间必须为正数")
        index = GridBboxIndex(cells)
        events = {cell["grid_id"]: [] for cell in cells}
        for event in (detection or {}).get("events") or []:
            cell = index.find_point(event["coordinate"])
            if cell:
                events[cell["grid_id"]].append(deepcopy(event))
        rates = {grid_id: len(items) / duration for grid_id, items in events.items()}
        normalization = self._normalization(parameters, list(rates.values()))
        reference = normalization.get("resolved_value")
        result_cells = {}
        for cell in cells:
            grid_id = cell["grid_id"]
            rate = rates[grid_id]
            result_cells[grid_id] = {
                "status": "passed",
                "conflict_count": len(events[grid_id]),
                "conflict_rate": rate,
                "conflict_rate_norm": self._normalize(rate, reference),
                "conflict_points": events[grid_id],
            }
        covered = sum(value["conflict_count"] > 0 for value in result_cells.values())
        return {
            "status": "passed",
            "source": {
                "algorithm_id": (detection or {}).get("algorithm_id"),
                "algorithm_version": (detection or {}).get("algorithm_version"),
                "parameters": deepcopy((detection or {}).get("parameters")),
            },
            "algorithm_id": self.algorithm_id, "algorithm_version": self.algorithm_version,
            "grid_level": grid.get("level"), "count": len(cells),
            "covered_count": covered, "simulation_seconds": duration,
            "normalization": normalization, "cells": result_cells,
        }

    def _normalization(self, parameters, values):
        result = deepcopy(self.default_normalization)
        result.update(deepcopy((parameters or {}).get("normalization") or {}))
        if self._finite(result.get("value")):
            result["resolved_value"] = float(result["value"])
            return result
        positive = sorted(float(value) for value in values if self._finite(value) and value > 0)
        result["resolved_value"] = self._quantile(
            positive, result.get("quantile", 0.95)
        ) if positive else 0.0
        return result

    @classmethod
    def _quantile(cls, values, quantile):
        q = max(0.0, min(1.0, float(quantile))) if cls._finite(quantile) else 0.95
        position = (len(values) - 1) * q
        lower, upper = math.floor(position), math.ceil(position)
        ratio = position - lower
        return values[lower] + (values[upper] - values[lower]) * ratio

    @classmethod
    def _normalize(cls, value, reference):
        if not cls._finite(value):
            return None
        if float(value) == 0:
            return 0.0
        if not cls._finite(reference) or reference <= 0:
            return None
        return max(0.0, min(1.0, float(value) / float(reference)))

    @staticmethod
    def _finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
