"""Map source population raster values onto existing standard grid IDs."""

from __future__ import annotations

from copy import deepcopy

from ..source_profiles import WORLDPOP_R2025A
from ...domain.quantities import geographic_bbox_area_m2, quantity_value
from ...gis.raster_adapter import GdalRasterAdapter


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
            "quantity_status": "not_calculated",
            "source_profile": deepcopy(WORLDPOP_R2025A),
            "mapping": cls._mapping_contract(),
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
            profile = self._source_profile(source)
            attributes = {}
            covered = 0
            quantity_covered = 0
            for cell in cells:
                values = adapter.read_values(cell["bbox"])
                if values:
                    covered += 1
                    item = {
                        "status": "passed",
                        "valid_sample_count": len(values),
                        "value_sum": sum(values),
                        "value_mean": sum(values) / len(values),
                        "value_min": min(values),
                        "value_max": max(values),
                    }
                else:
                    item = self._empty_cell()
                allocation = self._allocate_count(adapter, cell["bbox"])
                self._add_quantity_fields(item, cell["bbox"], allocation)
                quantity_covered += allocation.get("status") == "passed"
                attributes[cell["grid_id"]] = item
            return {
                "status": "passed" if covered == len(cells) else "missing_data",
                "source": source,
                "algorithm_id": self.algorithm_id,
                "sampling_version": self.sampling_version,
                "value_unit": unit,
                "unit_status": "verified_from_raster_metadata" if unit else "unverified",
                "interpretation": "source_values_only",
                "quantity_status": "passed" if quantity_covered == len(cells) else "missing_data",
                "quantity": "population_count_per_source_pixel",
                "unit": "person/source_pixel",
                "target_quantities": {
                    "population_count_people": "person",
                    "population_density_people_km2": "person/km2",
                },
                "source_profile": profile,
                "mapping": self._mapping_contract(source),
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
        result["quantity_status"] = status
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

    @staticmethod
    def _allocate_count(adapter, bbox):
        reader = getattr(adapter, "read_population_count", None)
        if reader is None:
            return {
                "status": "missing_data", "population_count_people": None,
                "target_area_m2": geographic_bbox_area_m2(bbox),
                "source_coverage_fraction": None, "source_pixel_count": 0,
            }
        return reader(bbox)

    @staticmethod
    def _add_quantity_fields(item, bbox, allocation):
        area_m2 = float(allocation.get("target_area_m2") or geographic_bbox_area_m2(bbox))
        count = allocation.get("population_count_people")
        density = count / (area_m2 / 1_000_000.0) if count is not None and area_m2 > 0 else None
        quantity_status = allocation.get("status") if count is not None else "missing_data"
        item.update({
            "quantity_status": quantity_status,
            "population_count_people": count,
            "population_density_people_km2": density,
            "grid_area_m2": area_m2,
            "source_coverage_fraction": allocation.get("source_coverage_fraction"),
            "source_pixel_count": int(allocation.get("source_pixel_count") or 0),
            "quantities": {
                "population_count": quantity_value(
                    count, "population_count", "person", status=quantity_status,
                    conversion={"method": "area_weighted_source_pixel_overlap"},
                    source=WORLDPOP_R2025A["source_id"], confirmed=True,
                ),
                "population_density": quantity_value(
                    density, "population_density", "person/km2", status=quantity_status,
                    conversion={"denominator": "actual_grid_area_km2"},
                    source=WORLDPOP_R2025A["source_id"], confirmed=True,
                ),
            },
        })

    @classmethod
    def _mapping_contract(cls, source=None):
        return {
            "method": "area_weighted_source_pixel_overlap",
            "population_conservation": True,
            "interpolation": "none",
            "assumptions": [
                "uniform_population_within_each_source_pixel",
                "source_pixel_footprint_bbox_exact_for_north_up_wgs84",
            ],
            "source_resolution": (source or {}).get("pixel_size") or deepcopy(WORLDPOP_R2025A["resolution"]),
        }

    @staticmethod
    def _source_profile(source):
        profile = deepcopy(WORLDPOP_R2025A)
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
