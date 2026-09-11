"""Map cumulative UAV trajectory residence time onto existing grid IDs."""

from __future__ import annotations

from copy import deepcopy
import math

from .grid_spatial_index import GridBboxIndex, bbox_area_km2, segment_fraction_in_bbox


class TrafficGridService:
    algorithm_id = "uav-traffic-grid-exposure-v1"
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

    def map(self, grid, simulation, parameters=None):
        cells = list((grid or {}).get("cells") or [])
        if not cells:
            return self.empty()
        duration = float((simulation or {}).get("simulation_seconds") or 0)
        if duration <= 0:
            raise ValueError("仿真时间必须为正数")
        index = GridBboxIndex(cells)
        seconds = {cell["grid_id"]: 0.0 for cell in cells}
        flights = {cell["grid_id"]: set() for cell in cells}
        for trajectory in simulation.get("trajectories") or []:
            uav_id = trajectory["uav_id"]
            samples = trajectory.get("samples") or []
            for left, right in zip(samples, samples[1:]):
                segment_seconds = float(right["time_s"]) - float(left["time_s"])
                if segment_seconds <= 0:
                    continue
                start, end = left["coordinate"], right["coordinate"]
                candidates = index.query([
                    min(start[0], end[0]), min(start[1], end[1]),
                    max(start[0], end[0]), max(start[1], end[1]),
                ])
                for cell in candidates:
                    fraction = segment_fraction_in_bbox(start, end, cell["bbox"])
                    if fraction <= 0:
                        continue
                    grid_id = cell["grid_id"]
                    seconds[grid_id] += segment_seconds * fraction
                    flights[grid_id].add(uav_id)
        raw = {}
        for cell in cells:
            area = bbox_area_km2(cell["bbox"])
            raw[cell["grid_id"]] = seconds[cell["grid_id"]] / (area * duration) if area > 0 else None
        normalization = self._normalization(parameters, list(raw.values()))
        reference = normalization.get("resolved_value")
        result_cells = {}
        for cell in cells:
            grid_id = cell["grid_id"]
            density = raw[grid_id]
            normalized = self._normalize(density, reference)
            result_cells[grid_id] = {
                "status": "passed" if normalized is not None else "missing_data",
                "flight_count": len(flights[grid_id]),
                "flight_seconds": seconds[grid_id],
                "grid_area_km2": bbox_area_km2(cell["bbox"]),
                "traffic_density_raw": density,
                "traffic_density_norm": normalized,
            }
        covered = sum(value["flight_count"] > 0 for value in result_cells.values())
        return {
            "status": "passed" if all(value["status"] == "passed" for value in result_cells.values()) else "missing_data",
            "source": {
                "algorithm_id": simulation.get("algorithm_id"),
                "algorithm_version": simulation.get("algorithm_version"),
                "input_fingerprint": simulation.get("input_fingerprint"),
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
        result["resolved_value"] = self._quantile(positive, result.get("quantile", 0.95)) if positive else 0.0
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
