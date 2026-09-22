"""Map source population raster values onto existing standard grid IDs."""

from __future__ import annotations

from copy import deepcopy
import math

from ..source_profiles import WORLDPOP_R2025A
from ...domain.quantities import geographic_bbox_area_m2, quantity_value
from ...domain.population_nodata import (
    CONFIRMED_ZERO_COVERAGE_STATUS, confirmed_zero_allocation, nodata_semantics_record,
    normalize_population_nodata_policy, policy_fingerprint, policy_is_confirmed,
)
from ...gis.raster_adapter import GdalRasterAdapter


class PopulationGridService:
    algorithm_id = "population-grid-raw-statistics"
    sampling_version = "1.1"

    #: 统一映射结果的取值词汇（与建筑环境映射保持一致的三态）。
    POPULATION_MAPPING_STATUSES = ("passed", "partial", "unsupported")

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
            "value_covered_count": 0,
            "full_count": 0,
            "partial_count": 0,
            "missing_count": 0,
            "outside_count": 0,
            "nodata_only_count": 0,
            # 统一结果载体使用的稳定别名：full_cells / partial_cells / missing_cells / coverage_ratio。
            "full_cells": 0,
            "partial_cells": 0,
            "missing_cells": 0,
            "coverage_ratio": None,
            "value_status": "not_calculated",
            "coverage_status": "not_calculated",
            "coverage_summary": {
                "full": 0, "partial": 0, "missing": 0,
                "outside_extent": 0, "nodata_only": 0,
                "valid_covered_area_m2": 0.0, "target_area_m2": 0.0,
                "source_coverage_fraction": 0.0,
            },
            "quantity_status": "not_calculated",
            "source_profile": deepcopy(WORLDPOP_R2025A),
            "mapping": cls._mapping_contract(),
            "cells": {},
        }
        if message:
            result["message"] = message
        result["population_mapping"] = cls.population_mapping(result)
        return result

    @classmethod
    def population_mapping(cls, result):
        """统一的 ``PopulationMappingResult``（只做归一化统计，不重算任何数值）。

        字段：``coverage_ratio`` / ``full_cells`` / ``partial_cells`` / ``missing_cells`` /
        ``total_cells`` / ``covered_cells`` / ``unresolved_cells`` / ``status``。

        ``status`` 三态：

        * ``passed``：每一格都有明确结论（观测覆盖或已确认的零人口）；
        * ``partial``：部分格有观测覆盖，部分格仍无法判定（``missing_data`` 绝不当 0）；
        * ``unsupported``：无任何可用覆盖（未配置 / 源不可用）。

        计数以 ``cells`` 为唯一权威来源重新统计，避免历史记录里
        ``full_count`` 与 ``coverage_summary.full`` 互相矛盾时把旧状态带到界面上。
        """

        result = result if isinstance(result, dict) else {}
        cells = result.get("cells") if isinstance(result.get("cells"), dict) else {}
        total = int(result.get("count") or len(cells))
        counts = {
            "full": 0, "partial": 0, "outside_extent": 0,
            "nodata_only": 0, "missing_data": 0, CONFIRMED_ZERO_COVERAGE_STATUS: 0,
        }
        value_passed = 0
        for cell in cells.values():
            if not isinstance(cell, dict):
                continue
            coverage = str(cell.get("coverage_status") or "")
            if coverage in counts:
                counts[coverage] += 1
            else:
                counts["missing_data"] += 1
            if str(cell.get("value_status") or "") == "passed":
                value_passed += 1
        confirmed_zero = counts[CONFIRMED_ZERO_COVERAGE_STATUS]
        covered = counts["full"] + counts["partial"] + confirmed_zero
        unresolved = max(0, total - covered)
        summary = result.get("coverage_summary") if isinstance(result.get("coverage_summary"), dict) else {}
        area_ratio = summary.get("source_coverage_fraction")
        cell_ratio = (covered / total) if total else None
        if isinstance(area_ratio, (int, float)) and not isinstance(area_ratio, bool) and total:
            coverage_ratio = float(area_ratio)
        else:
            coverage_ratio = cell_ratio
        result_status = str(result.get("status") or "")
        if not total:
            status = "unsupported"
        elif covered == total:
            status = "passed"
        elif covered:
            status = "partial"
        else:
            status = "unsupported"
        return {
            "status": status,
            "total_cells": total,
            "covered_cells": covered,
            "unresolved_cells": unresolved,
            "coverage_ratio": coverage_ratio,
            "cell_coverage_ratio": cell_ratio,
            "area_coverage_ratio": (
                float(area_ratio) if isinstance(area_ratio, (int, float))
                and not isinstance(area_ratio, bool) else None
            ),
            "full_cells": counts["full"],
            "partial_cells": counts["partial"],
            "missing_cells": counts["missing_data"],
            "outside_cells": counts["outside_extent"],
            "nodata_only_cells": counts["nodata_only"],
            "confirmed_zero_cells": confirmed_zero,
            "value_covered_cells": value_passed,
            "value_status": result.get("value_status"),
            "coverage_status": result.get("coverage_status"),
            "mapping_status": result_status or "not_calculated",
            "source_available": bool(result.get("source")) and result_status != "failed",
            "participates_in_planner": result_status == "passed",
            "algorithm_id": result.get("algorithm_id"),
            "sampling_version": result.get("sampling_version"),
        }

    @classmethod
    def _with_population_mapping(cls, result):
        mapping = cls.population_mapping(result)
        result["population_mapping"] = mapping
        # 稳定别名：前端与外部消费者可以只读这四个键就拿到覆盖统计。
        result["coverage_ratio"] = mapping["coverage_ratio"]
        result["full_cells"] = mapping["full_cells"]
        result["partial_cells"] = mapping["partial_cells"]
        result["missing_cells"] = mapping["missing_cells"]
        return result

    def map(self, grid, source_path, nodata_semantics=None):
        cells = list((grid or {}).get("cells") or [])
        if not cells:
            return self.empty()
        if not source_path:
            return self._missing(cells, grid.get("level"), None, "未配置人口栅格")
        policy = normalize_population_nodata_policy(nodata_semantics)
        record = nodata_semantics_record(policy)
        try:
            adapter = self.adapter_factory(source_path)
            source = adapter.describe()
            unit = source.get("unit_metadata")
            profile = self._source_profile(source)
            attributes = {}
            raw_covered = 0
            coverage_counts = {
                "full": 0, "partial": 0, "nodata_only": 0, "outside_extent": 0,
                CONFIRMED_ZERO_COVERAGE_STATUS: 0,
            }
            value_covered = 0
            valid_area_total = 0.0
            target_area_total = 0.0
            for cell in cells:
                values = adapter.read_values(cell["bbox"])
                if values:
                    raw_covered += 1
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
                allocation = self._allocate_count(adapter, cell["bbox"], policy)
                if not values and allocation.get("coverage_status") not in (
                    CONFIRMED_ZERO_COVERAGE_STATUS, "outside_extent",
                ):
                    # 该格没有任何来源像元中心落在其内：`read_population_count` 的“极小面积
                    # 重叠”会把 2e-7 人的观测外推成 ~79 person/km² 的病态密度。项目已显式确认
                    # 该来源的 NoData 表示零人口，因此这种“无有效观测覆盖”的格子按**已知 0**
                    # 处理，原分配的 quality flags 原样保留作为审计信息；来源范围之外仍是 unknown。
                    replacement = confirmed_zero_allocation(
                        allocation.get("target_area_m2") or geographic_bbox_area_m2(cell["bbox"]),
                        policy,
                        nodata_pixel_count=allocation.get("nodata_pixel_count"),
                        extra_quality_flags=list(allocation.get("quality_flags") or []),
                        trigger="no_source_pixel_centre_inside_cell",
                    )
                    if replacement is not None:
                        allocation = replacement
                self._add_quantity_fields(item, cell["bbox"], allocation)
                if allocation.get("coverage_status") == CONFIRMED_ZERO_COVERAGE_STATUS:
                    # A confirmed zero is a *known* value: the cell is usable downstream,
                    # and it carries its own provenance instead of an observation.
                    item["status"] = "passed"
                    item["nodata_semantics"] = allocation.get("nodata_semantics") or record
                    item["nodata_pixel_count"] = allocation.get("nodata_pixel_count")
                value_covered += item["value_status"] == "passed"
                coverage = item["coverage_status"]
                if coverage in coverage_counts:
                    coverage_counts[coverage] += 1
                valid_area_total += float(item.get("valid_covered_area_m2") or 0.0)
                target_area_total += float(item.get("grid_area_m2") or 0.0)
                attributes[cell["grid_id"]] = item
            value_status = "passed" if value_covered == len(cells) else "missing_data"
            legacy_status = "passed" if raw_covered == len(cells) else "missing_data"
            status = value_status if hasattr(adapter, "read_population_count") else legacy_status
            coverage_status = self._dataset_coverage_status(coverage_counts, len(cells))
            missing_count = (
                len(cells) - coverage_counts["full"] - coverage_counts["partial"]
                - coverage_counts[CONFIRMED_ZERO_COVERAGE_STATUS]
            )
            return self._with_population_mapping({
                "status": status,
                "value_status": value_status,
                "coverage_status": coverage_status,
                "source": source,
                "algorithm_id": self.algorithm_id,
                "sampling_version": self.sampling_version,
                "value_unit": unit,
                "unit_status": "verified_from_raster_metadata" if unit else "unverified",
                "interpretation": "source_values_only",
                "quantity_status": value_status,
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
                "covered_count": raw_covered,
                "value_covered_count": value_covered,
                "raw_statistics_covered_count": raw_covered,
                "full_count": coverage_counts["full"],
                "partial_count": coverage_counts["partial"],
                "missing_count": missing_count,
                "outside_count": coverage_counts["outside_extent"],
                "nodata_only_count": coverage_counts["nodata_only"],
                "confirmed_zero_population_count": coverage_counts[CONFIRMED_ZERO_COVERAGE_STATUS],
                "nodata_semantics": record,
                "nodata_semantics_policy_fingerprint": (
                    policy_fingerprint(policy) if policy_is_confirmed(policy) else None
                ),
                "coverage_summary": {
                    "full": coverage_counts["full"],
                    "partial": coverage_counts["partial"],
                    "missing": missing_count,
                    "outside_extent": coverage_counts["outside_extent"],
                    "nodata_only": coverage_counts["nodata_only"],
                    "confirmed_zero_population": coverage_counts[CONFIRMED_ZERO_COVERAGE_STATUS],
                    "valid_covered_area_m2": valid_area_total,
                    "target_area_m2": target_area_total,
                    "source_coverage_fraction": valid_area_total / target_area_total if target_area_total > 0 else 0.0,
                },
                "cells": attributes,
            })
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
        result["value_status"] = status
        result["coverage_status"] = "missing_data"
        result["missing_count"] = len(cells)
        return self._with_population_mapping(result)

    @staticmethod
    def _empty_cell():
        return {
            "status": "missing_data",
            "valid_sample_count": 0,
            "value_sum": None,
            "value_mean": None,
            "value_min": None,
            "value_max": None,
            "value_status": "missing_data",
            "coverage_status": "nodata_only",
            "density_support_area_m2": None,
            "density_semantics": None,
            "valid_covered_area_m2": 0.0,
            "source_coverage_fraction": 0.0,
            "source_pixel_count": 0,
            "quality_flags": ["population_quantity_unavailable"],
        }

    @staticmethod
    def _allocate_count(adapter, bbox, nodata_semantics=None):
        reader = getattr(adapter, "read_population_count", None)
        if reader is None:
            return {
                "status": "missing_data", "value_status": "missing_data",
                "coverage_status": "nodata_only", "population_count_people": None,
                "target_area_m2": geographic_bbox_area_m2(bbox),
                "valid_covered_area_m2": 0.0,
                "source_coverage_fraction": 0.0, "source_pixel_count": 0,
                "quality_flags": ["population_count_reader_unavailable"],
            }
        try:
            return reader(bbox, nodata_semantics=nodata_semantics)
        except TypeError:
            # 既有/替身 adapter 只接受 bbox：保持完全一致的历史行为（绝不补 0）。
            return reader(bbox)

    @staticmethod
    def _add_quantity_fields(item, bbox, allocation):
        area_m2 = float(allocation.get("target_area_m2") or geographic_bbox_area_m2(bbox))
        count = allocation.get("population_count_people")
        covered_area_m2 = float(allocation.get("valid_covered_area_m2") or 0.0)
        value_status = allocation.get("value_status") or allocation.get("status") or "missing_data"
        coverage_status = allocation.get("coverage_status") or (
            "full" if allocation.get("source_coverage_fraction") == 1 else "partial"
            if value_status == "passed" else "nodata_only"
        )
        density_area_m2 = covered_area_m2 if coverage_status == "partial" else area_m2
        density = count / (density_area_m2 / 1_000_000.0) if count is not None and density_area_m2 > 0 else None
        quantity_status = value_status if count is not None else "missing_data"
        legacy_statistics_status = item.get("status", "missing_data")
        item.update({
            "value_status": quantity_status,
            "coverage_status": coverage_status,
            "quantity_status": quantity_status,
            "population_count_people": count,
            "population_density_people_km2": density,
            "density_support_area_m2": density_area_m2 if density is not None else None,
            "density_semantics": "observed_covered_area_density" if density is not None else None,
            "grid_area_m2": area_m2,
            "valid_covered_area_m2": covered_area_m2,
            "source_coverage_fraction": allocation.get("source_coverage_fraction"),
            "source_pixel_count": int(allocation.get("source_pixel_count") or 0),
            "quality_flags": list(allocation.get("quality_flags") or []),
            "legacy_statistics_status": legacy_statistics_status,
            "quantities": {
                "population_count": quantity_value(
                    count, "population_count", "person", status=quantity_status,
                    conversion={"method": "area_weighted_source_pixel_overlap"},
                    source=WORLDPOP_R2025A["source_id"], confirmed=True,
                ),
                "population_density": quantity_value(
                    density, "population_density", "person/km2", status=quantity_status,
                    conversion={
                        "denominator": "valid_covered_area_km2" if coverage_status == "partial" else "actual_grid_area_km2",
                        "density_support_area_m2": density_area_m2 if density is not None else None,
                        "density_semantics": "observed_covered_area_density" if density is not None else None,
                        "not_extrapolated": True,
                    },
                    source=WORLDPOP_R2025A["source_id"], confirmed=True,
                ),
            },
        })

    @staticmethod
    def _dataset_coverage_status(counts, total):
        confirmed_zero = counts.get(CONFIRMED_ZERO_COVERAGE_STATUS, 0)
        if confirmed_zero:
            # Confirmed zero-population cells are *known* values, but they are still not
            # observations: the dataset is reported as partial unless every cell is one.
            if counts["full"] or counts["partial"]:
                return "partial"
            if counts["outside_extent"] == total:
                return "outside_extent"
            if confirmed_zero == total:
                return CONFIRMED_ZERO_COVERAGE_STATUS
            return "partial"
        if counts["full"] == total:
            return "full"
        if counts["full"] or counts["partial"]:
            return "partial"
        if counts["outside_extent"] == total:
            return "outside_extent"
        if counts["nodata_only"] == total:
            return "nodata_only"
        return "missing_data"

    @classmethod
    def _mapping_contract(cls, source=None):
        return {
            "method": "area_weighted_source_pixel_overlap",
            "population_conservation": True,
            "interpolation": "none",
            "partial_coverage": "retain_observed_count_and_density_without_extrapolation",
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

    @classmethod
    def backfill_legacy(cls, value):
        """Add coverage/value semantics to saved V1.0 results without inventing data."""
        if not isinstance(value, dict):
            return cls.empty()
        cells = value.get("cells") if isinstance(value.get("cells"), dict) else {}
        counts = {"full": 0, "partial": 0, "nodata_only": 0, "outside_extent": 0}
        valid_area_total = target_area_total = 0.0
        value_covered = 0
        for cell in cells.values():
            if not isinstance(cell, dict):
                continue
            density, count = cell.get("population_density_people_km2"), cell.get("population_count_people")
            canonical = isinstance(density, (int, float)) and not isinstance(density, bool) and math.isfinite(density)
            fraction = cell.get("source_coverage_fraction")
            fraction = float(fraction) if isinstance(fraction, (int, float)) and math.isfinite(fraction) else 0.0
            coverage = cell.get("coverage_status")
            if coverage not in counts:
                coverage = "full" if canonical and fraction >= 0.999999 else "partial" if canonical and fraction > 0 else "nodata_only"
            value_status = cell.get("value_status") or ("passed" if canonical else "missing_data")
            area = float(cell.get("grid_area_m2") or 0.0)
            valid_area = float(cell.get("valid_covered_area_m2") or area * fraction)
            if canonical and coverage == "partial" and isinstance(count, (int, float)) and valid_area > 0:
                cell["population_density_people_km2"] = float(count) / (valid_area / 1_000_000.0)
            cell.update({
                "value_status": value_status,
                "quantity_status": value_status,
                "coverage_status": coverage,
                "valid_covered_area_m2": valid_area,
                "density_support_area_m2": cell.get("density_support_area_m2", valid_area if canonical else None),
                "density_semantics": cell.get("density_semantics", "observed_covered_area_density" if canonical else None),
                "quality_flags": list(cell.get("quality_flags") or (["partial_source_coverage", "not_extrapolated"] if coverage == "partial" else [])),
            })
            counts[coverage] += 1
            value_covered += value_status == "passed"
            valid_area_total += valid_area
            target_area_total += area
        total = int(value.get("count", len(cells)))
        missing = total - counts["full"] - counts["partial"]
        value_status = "stale" if value.get("status") == "stale" else value.get("value_status") or ("passed" if total and value_covered == total else "missing_data")
        value.update({
            "value_status": value_status,
            "quantity_status": value_status,
            "coverage_status": value.get("coverage_status") or cls._dataset_coverage_status(counts, total),
            "value_covered_count": value_covered,
            "full_count": counts["full"], "partial_count": counts["partial"],
            "missing_count": missing, "outside_count": counts["outside_extent"],
            "nodata_only_count": counts["nodata_only"],
            "coverage_summary": value.get("coverage_summary") or {
                "full": counts["full"], "partial": counts["partial"], "missing": missing,
                "outside_extent": counts["outside_extent"], "nodata_only": counts["nodata_only"],
                "valid_covered_area_m2": valid_area_total, "target_area_m2": target_area_total,
                "source_coverage_fraction": valid_area_total / target_area_total if target_area_total > 0 else 0.0,
            },
        })
        return cls._with_population_mapping(value)
