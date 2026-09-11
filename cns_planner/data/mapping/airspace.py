"""Map normalized airspace intersections onto existing standard grid IDs."""

from __future__ import annotations


class AirspaceGridService:
    algorithm_id = "airspace-grid-intersection"
    algorithm_version = "1.0"

    @classmethod
    def empty(cls, status="not_calculated", source=None, message=None):
        result = {
            "status": status,
            "source": source,
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "grid_level": None,
            "count": 0,
            "hit_count": 0,
            "cells": {},
        }
        if message:
            result["message"] = message
        return result

    def map(self, grid, adapter):
        cells = list((grid or {}).get("cells") or [])
        if not cells:
            return self.empty()
        try:
            mapped = adapter.intersections(cells, grid.get("workspace_bbox"))
            result_cells = {}
            hit_count = 0
            for cell in cells:
                mapped_cell = mapped.get(cell["grid_id"], {})
                airspaces = list(mapped_cell.get("airspaces") or [])
                coverage_ratio = mapped_cell.get("coverage_ratio")
                if airspaces:
                    hit_count += 1
                    status = "full_coverage" if coverage_ratio is not None and coverage_ratio >= 0.999999 else "partial_intersection"
                else:
                    status = "no_coverage"
                    coverage_ratio = 0.0
                result_cells[cell["grid_id"]] = {
                    "status": status,
                    "intersected_layer_count": len({item["layer_id"] for item in airspaces}),
                    "coverage_ratio": coverage_ratio,
                    "airspaces": airspaces,
                }
            return {
                "status": "passed",
                "source": adapter.describe(),
                "algorithm_id": self.algorithm_id,
                "algorithm_version": self.algorithm_version,
                "grid_level": grid.get("level"),
                "count": len(cells),
                "hit_count": hit_count,
                "cells": result_cells,
            }
        except (OSError, ValueError, RuntimeError) as exc:
            result = self.empty("failed", adapter.describe(), str(exc))
            result["grid_level"] = grid.get("level")
            result["count"] = len(cells)
            result["cells"] = {
                cell["grid_id"]: {
                    "status": "failed",
                    "intersected_layer_count": 0,
                    "coverage_ratio": None,
                    "airspaces": [],
                }
                for cell in cells
            }
            return result
