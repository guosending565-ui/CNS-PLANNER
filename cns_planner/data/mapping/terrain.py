"""Map source DEM elevation values onto existing standard grid IDs."""

from __future__ import annotations

from copy import deepcopy

from ..source_profiles import COPERNICUS_GLO30
from ...domain.quantities import quantity_value
from ...gis.raster_adapter import GdalRasterAdapter


class TerrainGridService:
    algorithm_id = "terrain-grid-elevation-statistics"
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
            "quantity_status": "not_calculated",
            "source_profile": deepcopy(COPERNICUS_GLO30),
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
            return self._missing(cells, grid.get("level"), None, "未配置 DEM 栅格")
        try:
            adapter = self.adapter_factory(source_path)
            source = adapter.describe()
            unit = source.get("unit_metadata") or "m"
            unit_status = "verified_from_raster_metadata" if source.get("unit_metadata") else "glo30_product_contract"
            attributes = {}
            covered = 0
            for cell in cells:
                values = adapter.read_values(cell["bbox"])
                if values:
                    covered += 1
                    mean = sum(values) / len(values)
                    attributes[cell["grid_id"]] = {
                        "status": "passed",
                        "valid_sample_count": len(values),
                        "mean_elevation": mean,
                        "min_elevation": min(values),
                        "max_elevation": max(values),
                        "surface_elevation_mean_m": mean,
                        "surface_elevation_min_m": min(values),
                        "surface_elevation_max_m": max(values),
                        "quantity": quantity_value(mean, "surface_elevation", "m", source=COPERNICUS_GLO30["source_id"], confirmed=True),
                    }
                else:
                    attributes[cell["grid_id"]] = self._empty_cell()
            return {
                "status": "passed" if covered == len(cells) else "missing_data",
                "source": source,
                "algorithm_id": self.algorithm_id,
                "sampling_version": self.sampling_version,
                "elevation_unit": unit,
                "unit_status": unit_status,
                "quantity_status": "passed" if covered == len(cells) else "missing_data",
                "quantity": "surface_elevation",
                "surface_model": "DSM",
                "source_profile": self._source_profile(source),
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
        result["elevation_unit"] = "m"
        result["unit_status"] = "glo30_product_contract"
        result["quantity_status"] = status
        return result

    @staticmethod
    def _empty_cell():
        return {
            "status": "missing_data",
            "valid_sample_count": 0,
            "mean_elevation": None,
            "min_elevation": None,
            "max_elevation": None,
            "surface_elevation_mean_m": None,
            "surface_elevation_min_m": None,
            "surface_elevation_max_m": None,
            "quantity": quantity_value(None, "surface_elevation", "m", source=COPERNICUS_GLO30["source_id"], confirmed=True),
        }

    @staticmethod
    def _source_profile(source):
        profile = deepcopy(COPERNICUS_GLO30)
        profile["provenance"] = {
            **profile.get("provenance", {}),
            "path": source.get("path"),
            "observed_band": source.get("band"),
            "observed_nodata": source.get("nodata"),
        }
        if source.get("crs"):
            profile["crs"] = {**profile.get("crs", {}), "observed": source["crs"]}
        if source.get("pixel_size"):
            profile["resolution"] = {**profile.get("resolution", {}), "observed_pixel_size": source["pixel_size"]}
        return profile
