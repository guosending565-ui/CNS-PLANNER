"""Parameterized relative grid-risk model V1.

Scores are relative engineering indices in ``[0, 1]``, not accident
probabilities. Absolute ground risk remains an explicit uncomputed extension.
"""

from __future__ import annotations

from copy import deepcopy
import math
from numbers import Real
from typing import Mapping


class RiskModelV1:
    algorithm_id = "risk-model-v1-relative-index"
    algorithm_version = "1.1"
    default_parameters = {
        "parameter_status": "default_engineering_parameters",
        "risk_semantics": "relative_index",
        "population_reference": {"mode": "dataset_quantile", "quantile": 0.95, "value": None},
        "terrain_relief_reference": {"mode": "dataset_quantile", "quantile": 0.95, "value": None},
        "ground_formula": {
            "terrain_multiplier": 0.2,
            "building_additive_multiplier": 0.0,
        },
        "ground_completeness_weights": {
            "population": 0.4, "traffic": 0.4, "terrain": 0.2, "buildings": 0.0,
        },
        "ground_parameter_status": "default_engineering_parameters",
        "airspace_category_weights": {},
        "unknown_category_policy": "missing",
        "unknown_category_default_weight": None,
        "operational_air": {"traffic_alpha": 0.5},
        "operational_air_parameter_status": "default_engineering_parameters",
        "overall_weights": {"ground": 0.7, "air": 0.3},
        "overall_weight_status": "engineering_default",
        "risk_levels": [
            {"min": 0.0, "max": 0.2, "level": "very_low"},
            {"min": 0.2, "max": 0.4, "level": "low"},
            {"min": 0.4, "max": 0.6, "level": "medium"},
            {"min": 0.6, "max": 0.8, "level": "high"},
            {"min": 0.8, "max": 1.0, "level": "very_high", "include_max": True},
        ],
        "absolute_ground_risk": {
            "status": "not_calculated",
            "required_inputs": [
                "failure_probability_or_rate", "ground_impact_probability",
                "exposure", "consequence",
            ],
        },
    }
    input_names = (
        "population", "terrain", "airspace", "buildings",
        "property_exposure", "infrastructure", "towers", "traffic", "conflict",
    )

    @classmethod
    def empty(cls, status="not_calculated", parameters=None):
        return {
            "status": status,
            "semantics": "relative_index",
            "algorithm_id": cls.algorithm_id,
            "algorithm_version": cls.algorithm_version,
            "parameters": deepcopy(parameters or cls.default_parameters),
            "input_status": {},
            "source_versions": {},
            "input_versions": {},
            "grid_level": None,
            "count": 0,
            "data_completeness": 0.0,
            "cells": {},
        }

    def evaluate(self, grid, grid_attributes, parameters=None):
        effective = self._merge(self.default_parameters, parameters or {})
        attributes = dict(grid_attributes or {})
        cells = list((grid or {}).get("cells") or [])
        effective["population_reference"] = self._resolve_reference(
            effective.get("population_reference"),
            self._population_values(attributes.get("population") or {}),
        )
        effective["terrain_relief_reference"] = self._resolve_reference(
            effective.get("terrain_relief_reference"),
            self._terrain_reliefs(attributes.get("terrain") or {}),
        )
        result = self.empty(parameters=effective)
        result["input_status"] = {
            name: (attributes.get(name) or {}).get("status", "not_calculated")
            for name in self.input_names
        }
        result["source_versions"] = {
            name: self._source_version(attributes.get(name) or {})
            for name in self.input_names
        }
        result["input_versions"] = deepcopy(result["source_versions"])
        if not cells or (grid or {}).get("status") != "passed":
            return result

        result["grid_level"] = grid.get("level")
        result["count"] = len(cells)
        result_cells = {}
        for grid_cell in cells:
            grid_id = grid_cell["grid_id"]
            ground = self._ground_risk(grid_id, attributes, effective)
            airspace_constraint = self._airspace_constraint_risk(
                grid_id, attributes.get("airspace") or {}, effective
            )
            air = self._operational_air_risk(grid_id, attributes, effective)
            overall = self._overall_risk(ground, air, effective)
            result_cells[grid_id] = {
                "ground": ground,
                "air": air,
                "airspace_constraint": airspace_constraint,
                "overall": overall,
                "contributors": deepcopy(overall["contributors"]),
                "status": overall["status"],
                "semantics": "relative_index",
                "data_completeness": overall["data_completeness"],
            }
        result["cells"] = result_cells
        statuses = {cell["status"] for cell in result_cells.values()}
        result["status"] = (
            "stale" if "stale" in statuses
            else "missing_data" if "missing_data" in statuses
            else "passed"
        )
        result["data_completeness"] = sum(
            cell["data_completeness"] for cell in result_cells.values()
        ) / len(result_cells)
        return result

    def _ground_risk(self, grid_id, attributes, parameters):
        contributors = {
            "population": self._population_factor(
                grid_id, attributes.get("population") or {},
                parameters["population_reference"].get("resolved_value"),
            ),
            "terrain": self._terrain_factor(
                grid_id, attributes.get("terrain") or {},
                parameters["terrain_relief_reference"].get("resolved_value"),
            ),
            "traffic": self._traffic_factor(
                grid_id, attributes.get("traffic") or {}
            ),
            "buildings": self._extension_factor(
                grid_id, attributes.get("buildings") or {}, "building_exposure"
            ),
            "property_exposure": self._extension_factor(
                grid_id, attributes.get("property_exposure") or {}, "property_exposure"
            ),
            "infrastructure": self._extension_factor(
                grid_id, attributes.get("infrastructure") or {}, "infrastructure_exposure"
            ),
        }
        formula = parameters.get("ground_formula") or {}
        support_weights = parameters.get("ground_completeness_weights") or {}
        configured_support = {
            name: self._weight(support_weights.get(name))
            for name in ("population", "traffic", "terrain", "buildings")
        }
        support_total = sum(configured_support.values())
        support_available = sum(
            weight for name, weight in configured_support.items()
            if contributors[name]["status"] == "passed"
        )
        completeness = support_available / support_total if support_total > 0 else 0.0
        required = (contributors["population"], contributors["traffic"])
        if any(item["status"] == "stale" for item in required):
            status, score = "stale", None
        elif any(item["status"] != "passed" for item in required):
            status, score = "missing_data", None
        else:
            population = contributors["population"]["normalized"]
            traffic = contributors["traffic"]["normalized"]
            base = population * traffic
            terrain_uplift = 0.0
            terrain_lambda = self._weight(formula.get("terrain_multiplier"))
            if contributors["terrain"]["status"] == "passed":
                terrain_uplift = base * terrain_lambda * contributors["terrain"]["normalized"]
            building_additive = 0.0
            building_lambda = self._weight(formula.get("building_additive_multiplier"))
            if contributors["buildings"]["status"] == "passed":
                building_additive = building_lambda * contributors["buildings"]["normalized"]
            contributors["population"]["contribution"] = base
            contributors["traffic"]["contribution"] = base
            contributors["terrain"]["contribution"] = terrain_uplift if contributors["terrain"]["status"] == "passed" else None
            contributors["buildings"]["contribution"] = building_additive if contributors["buildings"]["status"] == "passed" else None
            status, score = "passed", self._clip(base + terrain_uplift + building_additive)
        unit_status = contributors["population"].get("population_unit_status")
        return {
            "status": status,
            "score": score,
            "value": score,
            "level": self._risk_level(score, parameters.get("risk_levels") or []),
            "unit": "index_0_1" if score is not None else None,
            "semantics": "relative_ground_risk",
            "risk_semantics": "relative_only" if unit_status != "verified_from_raster_metadata" else "relative_index",
            "data_completeness": completeness,
            "contributors": contributors,
            "absolute_risk": self._absolute_ground_risk(parameters),
        }

    def _traffic_factor(self, grid_id, attributes):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(attributes, cell)
        value = cell.get("traffic_density_norm")
        return {
            "raw": cell.get("traffic_density_raw"),
            "normalized": self._clip(float(value)) if self._finite(value) else None,
            "weight": None, "normalized_weight": None, "contribution": None,
            "status": unavailable or ("passed" if self._finite(value) else "missing_data"),
            "flight_seconds": cell.get("flight_seconds"),
            "flight_count": cell.get("flight_count"),
        }

    def _conflict_factor(self, grid_id, attributes):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(attributes, cell)
        value = cell.get("conflict_rate_norm")
        return {
            "raw": cell.get("conflict_rate"),
            "normalized": self._clip(float(value)) if self._finite(value) else None,
            "weight": None, "normalized_weight": None, "contribution": None,
            "status": unavailable or ("passed" if self._finite(value) else "missing_data"),
            "conflict_count": cell.get("conflict_count"),
        }

    def _operational_air_risk(self, grid_id, attributes, parameters):
        contributors = {
            "traffic": self._traffic_factor(grid_id, attributes.get("traffic") or {}),
            "conflict": self._conflict_factor(grid_id, attributes.get("conflict") or {}),
        }
        alpha = self._clip((parameters.get("operational_air") or {}).get("traffic_alpha", 0.5))
        score, completeness, status = self._weighted_factors(
            contributors, {"traffic": alpha, "conflict": 1.0 - alpha}
        )
        return {
            "status": status, "score": score, "value": score,
            "level": self._risk_level(score, parameters.get("risk_levels") or []),
            "unit": "index_0_1" if score is not None else None,
            "semantics": "operational_air_relative_risk",
            "data_completeness": completeness,
            "contributors": contributors,
        }

    def _population_factor(self, grid_id, attributes, reference):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(attributes, cell)
        unit_status = attributes.get("unit_status", "unverified")
        result = {
            "raw": cell.get("value_mean"), "normalized": None,
            "weight": None, "normalized_weight": None, "contribution": None,
            "status": unavailable,
            "population_unit_status": unit_status,
            "risk_semantics": "relative_only" if unit_status != "verified_from_raster_metadata" else "relative_index",
        }
        value = cell.get("value_mean")
        if unavailable or not self._finite(value):
            result["status"] = unavailable or "missing_data"
            return result
        if not self._finite(reference) or reference <= 0:
            result["status"] = "missing_data"
            result["reason"] = "population_reference_unavailable"
            return result
        result["normalized"] = self._clip(
            math.log1p(max(float(value), 0.0)) / math.log1p(reference)
        )
        result["status"] = "passed"
        return result

    def _terrain_factor(self, grid_id, attributes, reference):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(attributes, cell)
        minimum, maximum = cell.get("min_elevation"), cell.get("max_elevation")
        relief = (
            float(maximum) - float(minimum)
            if self._finite(minimum) and self._finite(maximum) else None
        )
        result = {
            "raw": {
                "relief": relief, "mean_elevation": cell.get("mean_elevation"),
                "min_elevation": minimum, "max_elevation": maximum,
            },
            "normalized": None, "weight": None,
            "normalized_weight": None, "contribution": None,
            "status": unavailable,
        }
        if unavailable or relief is None:
            result["status"] = unavailable or "missing_data"
            return result
        if not self._finite(reference) or reference <= 0:
            result["status"] = "missing_data"
            result["reason"] = "terrain_relief_reference_unavailable"
            return result
        result["normalized"] = self._clip(relief / reference)
        result["status"] = "passed"
        return result

    def _extension_factor(self, grid_id, attributes, value_name):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(attributes, cell)
        value = cell.get("relative_index", cell.get(value_name))
        return {
            "raw": value,
            "normalized": self._clip(float(value)) if self._finite(value) else None,
            "weight": None, "normalized_weight": None, "contribution": None,
            "status": unavailable or ("passed" if self._finite(value) else "not_available"),
        }

    def _weighted_factors(self, contributors, weights):
        configured = {name: self._weight(weights.get(name)) for name in contributors}
        total_configured = sum(configured.values())
        stale = any(
            item["status"] == "stale" and configured[name] > 0
            for name, item in contributors.items()
        )
        available_weight = sum(
            configured[name] for name, item in contributors.items()
            if item["status"] == "passed" and item["normalized"] is not None
        )
        for name, item in contributors.items():
            weight = configured[name]
            item["weight"] = weight
            if item["status"] == "passed" and item["normalized"] is not None and available_weight > 0:
                item["normalized_weight"] = weight / available_weight
                item["contribution"] = item["normalized"] * item["normalized_weight"]
        completeness = available_weight / total_configured if total_configured > 0 else 0.0
        if stale:
            return None, completeness, "stale"
        contributions = [item["contribution"] for item in contributors.values() if item["contribution"] is not None]
        if not contributions or available_weight <= 0:
            return None, completeness, "missing_data"
        return self._clip(sum(contributions)), completeness, "passed"

    def _airspace_constraint_risk(self, grid_id, attributes, parameters):
        cell = (attributes.get("cells") or {}).get(grid_id) or {}
        unavailable = self._input_unavailable(
            attributes, cell,
            {"no_coverage", "partial_intersection", "full_coverage", "passed"},
        )
        result = {
            "status": unavailable or "missing_data", "score": None, "value": None,
            "level": None, "unit": None,
            "semantics": "airspace_constraint_risk",
            "data_completeness": 0.0, "contributors": [],
        }
        if unavailable:
            return result
        hits = list(cell.get("airspaces") or [])
        if cell.get("status") == "no_coverage" and not hits:
            result.update({
                "status": "passed", "score": 0.0, "value": 0.0,
                "level": self._risk_level(0.0, parameters.get("risk_levels") or []),
                "unit": "index_0_1", "data_completeness": 1.0,
            })
            return result
        if not hits:
            return result

        policy = parameters.get("unknown_category_policy", "missing")
        weights = parameters.get("airspace_category_weights") or {}
        contributors = [self._airspace_hit(hit, weights, policy, parameters) for hit in hits]
        resolved = [item for item in contributors if item["status"] == "passed"]
        completeness = len(resolved) / len(contributors)
        result["contributors"] = contributors
        result["data_completeness"] = completeness
        if policy == "missing" and len(resolved) != len(contributors):
            return result
        if not resolved:
            return result
        score = self._clip(max(item["risk_index"] for item in resolved))
        result.update({
            "status": "passed", "score": score, "value": score,
            "level": self._risk_level(score, parameters.get("risk_levels") or []),
            "unit": "index_0_1",
        })
        return result

    def _airspace_hit(self, hit, category_weights, policy, parameters):
        ratio = hit.get("intersection_ratio")
        category, weight = self._category_weight(hit, category_weights)
        result = {
            "layer_id": hit.get("layer_id"), "feature_id": hit.get("feature_id"),
            "category": hit.get("category"), "type": hit.get("type"),
            "matched_category": category, "raw": ratio, "normalized": None,
            "weight": weight, "contribution": None, "risk_index": None,
            "status": "passed",
        }
        if not self._finite(ratio):
            result["status"] = "missing_data"
            result["reason"] = "intersection_ratio_unavailable"
            return result
        if weight is None:
            if policy == "zero":
                weight = 0.0
                result["reason"] = "unknown_category_zero_policy"
            elif policy == "default_weight" and self._finite(parameters.get("unknown_category_default_weight")):
                weight = float(parameters["unknown_category_default_weight"])
                result["reason"] = "unknown_category_default_weight"
            else:
                result["status"] = "unknown_category"
                result["reason"] = "unknown_category"
                return result
        normalized_ratio = self._clip(float(ratio))
        result["normalized"] = normalized_ratio
        result["weight"] = float(weight)
        result["contribution"] = normalized_ratio * float(weight)
        result["risk_index"] = self._clip(result["contribution"])
        return result

    def _overall_risk(self, ground, air, parameters):
        weights = parameters.get("overall_weights") or {}
        parts = {"ground": ground, "air": air}
        configured = {name: self._weight(weights.get(name)) for name in parts}
        total_configured = sum(configured.values())
        available = {
            name: part for name, part in parts.items()
            if part.get("status") == "passed" and self._finite(part.get("score"))
        }
        available_weight = sum(configured[name] for name in available)
        contributors = {}
        for name, part in parts.items():
            normalized_weight = configured[name] / available_weight if name in available and available_weight > 0 else None
            contributors[name] = {
                "status": part.get("status"), "raw": part.get("score"),
                "weight": configured[name], "normalized_weight": normalized_weight,
                "contribution": part.get("score") * normalized_weight if normalized_weight is not None else None,
            }
        completeness = (
            sum(configured[name] * available[name].get("data_completeness", 0.0) for name in available)
            / total_configured if total_configured > 0 else 0.0
        )
        if any(part.get("status") == "stale" for part in parts.values()):
            status, score = "stale", None
        elif available_weight <= 0:
            status, score = "missing_data", None
        else:
            status = "passed"
            score = self._clip(sum(
                item["contribution"] for item in contributors.values()
                if item["contribution"] is not None
            ))
        return {
            "status": status, "score": score, "value": score,
            "level": self._risk_level(score, parameters.get("risk_levels") or []),
            "unit": "index_0_1" if score is not None else None,
            "semantics": "relative_index", "data_completeness": completeness,
            "contributors": contributors,
        }

    @staticmethod
    def _absolute_ground_risk(parameters):
        configured = parameters.get("absolute_ground_risk") or {}
        return {
            "status": "not_calculated", "value": None, "unit": None,
            "formula": "P_failure * P_ground_impact * Exposure * Consequence",
            "required_inputs": deepcopy(configured.get("required_inputs") or []),
            "reason": "requires_verified_aircraft_failure_impact_exposure_and_consequence_models",
        }

    @staticmethod
    def _input_unavailable(attributes, cell, valid_cell_statuses=None):
        valid = valid_cell_statuses or {"passed"}
        if attributes.get("status") == "stale" or cell.get("status") == "stale":
            return "stale"
        if cell.get("status") in valid:
            return None
        return "not_available" if attributes.get("status") == "not_calculated" else "missing_data"

    @classmethod
    def _risk_level(cls, score, levels):
        if not cls._finite(score):
            return None
        value = float(score)
        for item in levels:
            lower, upper = item.get("min"), item.get("max")
            if not cls._finite(lower) or not cls._finite(upper):
                continue
            if float(lower) <= value < float(upper) or item.get("include_max") and value == float(upper):
                return item.get("level")
        return None

    @staticmethod
    def _category_weight(hit, weights):
        lookup = {str(key).casefold(): value for key, value in weights.items()}
        for value in (hit.get("category"), hit.get("type")):
            if value is not None and str(value).casefold() in lookup:
                weight = lookup[str(value).casefold()]
                return value, max(0.0, float(weight)) if RiskModelV1._finite(weight) else None
        return None, None

    @staticmethod
    def _population_values(attributes):
        return [
            cell.get("value_mean") for cell in (attributes.get("cells") or {}).values()
            if cell.get("status") == "passed" and RiskModelV1._finite(cell.get("value_mean"))
        ]

    @staticmethod
    def _terrain_reliefs(attributes):
        values = []
        for cell in (attributes.get("cells") or {}).values():
            minimum, maximum = cell.get("min_elevation"), cell.get("max_elevation")
            if cell.get("status") == "passed" and RiskModelV1._finite(minimum) and RiskModelV1._finite(maximum):
                values.append(float(maximum) - float(minimum))
        return values

    @classmethod
    def _resolve_reference(cls, specification, values):
        if isinstance(specification, Real) and not isinstance(specification, bool):
            value = float(specification)
            return {"mode": "explicit", "value": value, "resolved_value": value}
        result = deepcopy(specification) if isinstance(specification, Mapping) else {}
        explicit = result.get("value")
        if cls._finite(explicit):
            result["resolved_value"] = float(explicit)
            return result
        quantile = result.get("quantile", 0.95)
        result["resolved_value"] = cls._quantile(values, quantile) if values else None
        return result

    @classmethod
    def _quantile(cls, values, quantile):
        clean = sorted(float(value) for value in values if cls._finite(value))
        if not clean:
            return None
        q = cls._clip(float(quantile)) if cls._finite(quantile) else 0.95
        position = (len(clean) - 1) * q
        lower, upper = math.floor(position), math.ceil(position)
        ratio = position - lower
        return clean[lower] + (clean[upper] - clean[lower]) * ratio

    @staticmethod
    def _source_version(attributes):
        return {
            "status": attributes.get("status", "not_calculated"),
            "algorithm_id": attributes.get("algorithm_id"),
            "version": attributes.get("algorithm_version") or attributes.get("sampling_version"),
            "source": deepcopy(attributes.get("source")),
        }

    @classmethod
    def _merge(cls, base, overrides):
        result = deepcopy(base)
        for key, value in dict(overrides).items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = cls._merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    @staticmethod
    def _weight(value):
        return max(0.0, float(value)) if RiskModelV1._finite(value) else 0.0

    @staticmethod
    def _finite(value):
        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))

    @staticmethod
    def _clip(value):
        return max(0.0, min(1.0, float(value)))
