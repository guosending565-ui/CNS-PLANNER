"""Risk Framework V2 factor extraction layer.

The factor layer answers exactly one question per ``(factor_id, grid_id)``:
*what canonical quantity did the existing sources actually produce, and how was
it rescaled onto a relative index?*  It never aggregates and it never invents
values: a factor without a canonical source stays ``unknown``/``missing_data``
with ``normalized_index = None``.

Canonical reuse (no new data product, no guessing):

* ``population_exposure``   ← ``population_density_people_km2``
* ``uav_traffic_exposure``  ← ``traffic_density_raw`` / ``traffic_density_norm``
* ``conflict_exposure``     ← ``conflict_rate`` / ``conflict_rate_norm``
* ``terrain_relief``        ← ``surface_elevation_max_m - surface_elevation_min_m``
* ``building_coverage``     ← ``building_coverage_ratio``
* ``building_height``       ← ``height_p95_m`` / ``height_max_m`` + ``valid_height_fraction``

``UAV traffic exposure`` is the existing ``TrafficGridService`` output
(``flight_count`` / ``flight_seconds``).  It is an **air traffic** exposure and
is only ever placed in the ``air_traffic`` domain — it is never ground
transport exposure.
"""

from __future__ import annotations

from copy import deepcopy

from ..domain.risk_v2 import (
    FACTOR_DEFINITIONS, FACTOR_IDS, FACTOR_INPUT_ATTRIBUTES, FACTOR_SOURCE_ROLE,
    NO_SOURCE_FACTOR_IDS, stable_fingerprint,
)
from .normalization import (
    CANONICAL_FIELD_METHOD, IDENTITY_METHOD, LOG1P_QUANTILE_METHOD,
    RATIO_QUANTILE_METHOD, clip, dataset_reference, finite,
    identity_ratio_index, log1p_quantile_index, normalization_record,
    ratio_quantile_index,
)

STATUS_PASSED = "passed"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing_data"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_AVAILABLE = "not_available"
STATUS_STALE = "stale"

READINESS_READY = "ready"
READINESS_PARTIAL = "partial"
READINESS_STALE = "stale"
READINESS_BLOCKED = "blocked"

#: Population density is only accepted in its canonical target-grid form.  The
#: legacy ``value_mean`` fallback of RiskModelV1 is intentionally not reused
#: here: it is a source-pixel statistic with different semantics.
POPULATION_FIELD = "population_density_people_km2"
TRAFFIC_RAW_FIELD = "traffic_density_raw"
TRAFFIC_NORM_FIELD = "traffic_density_norm"
CONFLICT_RAW_FIELD = "conflict_rate"
CONFLICT_NORM_FIELD = "conflict_rate_norm"
COVERAGE_FIELD = "building_coverage_ratio"
HEIGHT_P95_FIELD = "height_p95_m"
HEIGHT_MAX_FIELD = "height_max_m"
VALID_HEIGHT_FRACTION_FIELD = "valid_height_fraction"
SURFACE_MIN_FIELDS = ("surface_elevation_min_m", "min_elevation")
SURFACE_MAX_FIELDS = ("surface_elevation_max_m", "max_elevation")

_INVALID_COVERAGE_STATUSES = ("nodata_only", "outside_extent")


class FactorExtraction:
    """Extract canonical V2 factors from the standard grid attributes."""

    def __init__(self, grid, grid_attributes):
        self.grid = dict(grid or {})
        self.attributes = {
            name: (value if isinstance(value, dict) else {})
            for name, value in dict(grid_attributes or {}).items()
        }
        self.cell_ids = [
            cell.get("grid_id") for cell in (self.grid.get("cells") or [])
            if isinstance(cell, dict) and cell.get("grid_id")
        ]
        self.grid_level = self.grid.get("level")
        self._sources = {}

    # ------------------------------------------------------------------ public

    def extract(self):
        factors, references = {}, {}
        for builder in (
            self._ground_factors, self._air_traffic_factors,
            self._environment_obstacle_factors,
        ):
            records, refs = builder()
            factors.update(records)
            references.update(refs)
        factor_status = {
            factor_id: self._summarize(factor_id, factors.get(factor_id) or {}, references.get(factor_id))
            for factor_id in FACTOR_IDS
        }
        return {
            "factors": factors,
            "factor_status": factor_status,
            "references": references,
            "input_status": self._input_status(),
            "source_versions": self._source_versions(),
            "input_fingerprint": self._input_fingerprint(factors, references),
        }

    # ------------------------------------------------------------------ domains

    def _ground_factors(self):
        records = {
            "population_exposure": self._population_records(),
        }
        for factor_id in NO_SOURCE_FACTOR_IDS:
            if FACTOR_DEFINITIONS[factor_id]["domain"] == "ground":
                records[factor_id] = self._no_source_records(factor_id)
        return records, {"population_exposure": self._population_reference}

    def _air_traffic_factors(self):
        records = {
            "uav_traffic_exposure": self._canonical_index_records(
                "uav_traffic_exposure", "traffic", TRAFFIC_RAW_FIELD, TRAFFIC_NORM_FIELD,
            ),
            "conflict_exposure": self._canonical_index_records(
                "conflict_exposure", "conflict", CONFLICT_RAW_FIELD, CONFLICT_NORM_FIELD,
            ),
        }
        references = {
            factor_id: self._canonical_field_reference(factor_id, norm_field)
            for factor_id, norm_field in (
                ("uav_traffic_exposure", TRAFFIC_NORM_FIELD),
                ("conflict_exposure", CONFLICT_NORM_FIELD),
            )
        }
        return records, references

    def _environment_obstacle_factors(self):
        records = {
            "terrain_relief": self._terrain_records(),
            "building_coverage": self._building_coverage_records(),
            "building_height": self._building_height_records(),
        }
        references = {
            "terrain_relief": self._terrain_reference,
            "building_coverage": self._identity_reference(
                "building_coverage", "building_coverage_ratio",
            ),
            "building_height": self._building_height_reference,
        }
        return records, references

    # ------------------------------------------------------------------ ground

    def _population_records(self):
        attribute = self._attribute("population")
        unit_status = attribute.get("unit_status", "unverified")
        values = []
        for grid_id in self.cell_ids:
            cell = self._cell("population", grid_id)
            if self._unavailable("population", cell)[0]:
                continue
            if not self._population_coverage_usable(cell):
                continue
            value = cell.get(POPULATION_FIELD)
            if finite(value):
                values.append(float(value))
        self._population_reference = dataset_reference(
            values, source_field=POPULATION_FIELD,
        )
        normalization = normalization_record(LOG1P_QUANTILE_METHOD, self._population_reference)
        records = {}
        for grid_id in self.cell_ids:
            cell = self._cell("population", grid_id)
            status, reason = self._unavailable("population", cell)
            coverage = self._finite_or_none(cell.get("source_coverage_fraction"))
            quality = []
            value = cell.get(POPULATION_FIELD)
            if unit_status != "verified_from_raster_metadata":
                quality.append("unverified_population_unit")
            if status is None:
                if not self._population_coverage_usable(cell):
                    status = STATUS_MISSING
                    reason = cell.get("coverage_status") or "population_coverage_unusable"
                    coverage = 0.0
                elif not finite(value):
                    status = STATUS_MISSING
                    reason = "canonical_population_density_missing"
                elif self._population_reference.get("value") is None:
                    status = STATUS_MISSING
                    reason = "population_reference_unavailable"
                else:
                    if cell.get("coverage_status") == "partial":
                        quality.append("partial_source_coverage")
                    records[grid_id] = self._record(
                        "population_exposure", grid_id,
                        status=STATUS_PASSED, raw_value=float(value),
                        normalized_index=log1p_quantile_index(value, self._population_reference["value"]),
                        normalization=normalization, coverage=coverage,
                        quality_flags=quality, reason=None,
                    )
                    continue
            records[grid_id] = self._record(
                "population_exposure", grid_id, status=status, raw_value=(
                    float(value) if finite(value) else None
                ),
                normalized_index=None, normalization=normalization,
                coverage=coverage, quality_flags=quality, reason=reason,
            )
        return records

    def _population_coverage_usable(self, cell):
        if cell.get("coverage_status") in _INVALID_COVERAGE_STATUSES:
            return False
        coverage = self._finite_or_none(cell.get("source_coverage_fraction"))
        return coverage is None or coverage > 0

    # ------------------------------------------------------------- air traffic

    def _canonical_index_records(self, factor_id, attribute_name, raw_field, norm_field):
        normalization = normalization_record(
            CANONICAL_FIELD_METHOD,
            self._canonical_field_reference(factor_id, norm_field),
        )
        records = {}
        for grid_id in self.cell_ids:
            cell = self._cell(attribute_name, grid_id)
            status, reason = self._unavailable(attribute_name, cell)
            raw = self._finite_or_none(cell.get(raw_field))
            value = self._finite_or_none(cell.get(norm_field))
            if status is None:
                if value is None:
                    status, reason = STATUS_MISSING, f"canonical_{norm_field}_missing"
                else:
                    records[grid_id] = self._record(
                        factor_id, grid_id, status=STATUS_PASSED, raw_value=raw,
                        normalized_index=identity_ratio_index(value),
                        normalization=normalization, coverage=1.0,
                        quality_flags=[], reason=None,
                    )
                    continue
            records[grid_id] = self._record(
                factor_id, grid_id, status=status, raw_value=raw,
                normalized_index=None, normalization=normalization,
                coverage=1.0 if status != STATUS_STALE else None,
                quality_flags=[], reason=reason,
            )
        return records

    # ------------------------------------------------- environment / obstacle

    def _terrain_records(self):
        reliefs, raw_by_cell = [], {}
        for grid_id in self.cell_ids:
            cell = self._cell("terrain", grid_id)
            relief = self._relief(cell)
            if relief is not None and not self._unavailable("terrain", cell)[0]:
                reliefs.append(relief)
            raw_by_cell[grid_id] = relief
        self._terrain_reference = dataset_reference(
            reliefs, source_field="surface_elevation_max_m - surface_elevation_min_m",
        )
        normalization = normalization_record(RATIO_QUANTILE_METHOD, self._terrain_reference)
        records = {}
        for grid_id in self.cell_ids:
            cell = self._cell("terrain", grid_id)
            status, reason = self._unavailable("terrain", cell)
            relief = raw_by_cell[grid_id]
            quality = [] if self._canonical_surface_fields(cell) else ["legacy_elevation_alias"]
            if status is None:
                if relief is None:
                    status, reason = STATUS_MISSING, "canonical_surface_elevation_missing"
                elif self._terrain_reference.get("value") is None:
                    status, reason = STATUS_MISSING, "terrain_relief_reference_unavailable"
                else:
                    records[grid_id] = self._record(
                        "terrain_relief", grid_id, status=STATUS_PASSED,
                        raw_value=relief,
                        normalized_index=ratio_quantile_index(relief, self._terrain_reference["value"]),
                        normalization=normalization, coverage=1.0,
                        quality_flags=quality, reason=None,
                    )
                    continue
            records[grid_id] = self._record(
                "terrain_relief", grid_id, status=status, raw_value=relief,
                normalized_index=None, normalization=normalization,
                coverage=1.0 if status != STATUS_STALE else None,
                quality_flags=quality, reason=reason,
            )
        return records

    def _relief(self, cell):
        minimum, maximum = None, None
        for field in SURFACE_MIN_FIELDS:
            if finite(cell.get(field)):
                minimum = float(cell[field])
                break
        for field in SURFACE_MAX_FIELDS:
            if finite(cell.get(field)):
                maximum = float(cell[field])
                break
        if minimum is None or maximum is None:
            return None
        return maximum - minimum

    @staticmethod
    def _canonical_surface_fields(cell):
        return finite(cell.get("surface_elevation_min_m")) and finite(cell.get("surface_elevation_max_m"))

    def _building_coverage_records(self):
        attribute = self._attribute("buildings")
        value_semantics = (attribute.get("metadata") or {}).get("zero_semantics")
        normalization = normalization_record(
            IDENTITY_METHOD, self._identity_reference("building_coverage", COVERAGE_FIELD),
        )
        records = {}
        for grid_id in self.cell_ids:
            cell = self._cell("buildings", grid_id)
            status, reason = self._unavailable("buildings", cell)
            ratio = self._finite_or_none(cell.get(COVERAGE_FIELD))
            quality = ["building_coverage_is_not_sheltering"]
            if value_semantics:
                quality.append(f"zero_semantics={value_semantics}")
            if status is None:
                if ratio is None:
                    status, reason = STATUS_MISSING, "building_coverage_ratio_missing"
                else:
                    records[grid_id] = self._record(
                        "building_coverage", grid_id, status=STATUS_PASSED,
                        raw_value=ratio, normalized_index=identity_ratio_index(ratio),
                        normalization=normalization, coverage=1.0,
                        quality_flags=quality, reason=None,
                    )
                    continue
            records[grid_id] = self._record(
                "building_coverage", grid_id, status=status, raw_value=ratio,
                normalized_index=None, normalization=normalization,
                coverage=1.0 if status != STATUS_STALE else None,
                quality_flags=quality, reason=reason,
            )
        return records

    def _building_height_records(self):
        values, chosen_by_cell = [], {}
        for grid_id in self.cell_ids:
            cell = self._cell("buildings", grid_id)
            height, field = self._chosen_height(cell)
            chosen_by_cell[grid_id] = (height, field)
            if height is not None and not self._unavailable("buildings", cell)[0]:
                values.append(height)
        self._building_height_reference = dataset_reference(
            values, source_field=f"{HEIGHT_P95_FIELD} / {HEIGHT_MAX_FIELD}",
        )
        records = {}
        for grid_id in self.cell_ids:
            cell = self._cell("buildings", grid_id)
            status, reason = self._unavailable("buildings", cell)
            height, field = chosen_by_cell[grid_id]
            fraction = self._finite_or_none(cell.get(VALID_HEIGHT_FRACTION_FIELD))
            quality = []
            if field == HEIGHT_MAX_FIELD:
                quality.append("height_uses_max_fallback")
            if fraction is None:
                quality.append("valid_height_fraction_unknown")
            elif fraction < 1.0:
                quality.append("partial_building_height_coverage")
            normalization = normalization_record(
                RATIO_QUANTILE_METHOD, self._building_height_reference,
                reason=None if field is None else f"height_field={field}",
            )
            if status is None:
                if height is None:
                    status, reason = STATUS_MISSING, "building_height_missing"
                elif self._building_height_reference.get("value") is None:
                    status, reason = STATUS_MISSING, "building_height_reference_unavailable"
                else:
                    partial = fraction is None or fraction < 1.0
                    records[grid_id] = self._record(
                        "building_height", grid_id,
                        status=STATUS_PARTIAL if partial else STATUS_PASSED,
                        raw_value=height,
                        normalized_index=ratio_quantile_index(
                            height, self._building_height_reference["value"],
                        ),
                        normalization=normalization, coverage=fraction,
                        quality_flags=quality,
                        reason=(
                            "building_height_coverage_partial_unknown_heights_not_zero"
                            if fraction is None else
                            "building_height_coverage_partial" if fraction < 1.0 else None
                        ),
                        resolved=not partial,
                        provenance_extra={"height_field": field},
                    )
                    continue
            records[grid_id] = self._record(
                "building_height", grid_id, status=status, raw_value=height,
                normalized_index=None, normalization=normalization, coverage=fraction,
                quality_flags=quality, reason=reason,
                provenance_extra={"height_field": field},
            )
        return records

    @staticmethod
    def _chosen_height(cell):
        if finite(cell.get(HEIGHT_P95_FIELD)):
            return float(cell[HEIGHT_P95_FIELD]), HEIGHT_P95_FIELD
        if finite(cell.get(HEIGHT_MAX_FIELD)):
            return float(cell[HEIGHT_MAX_FIELD]), HEIGHT_MAX_FIELD
        return None, None

    # ---------------------------------------------------------------- no source

    def _no_source_records(self, factor_id):
        normalization = normalization_record(
            None, None, reason="no_canonical_source_available_in_this_system",
        )
        records = {}
        for grid_id in self.cell_ids:
            records[grid_id] = self._record(
                factor_id, grid_id, status=STATUS_UNKNOWN, raw_value=None,
                normalized_index=None, normalization=normalization, coverage=0.0,
                quality_flags=["no_canonical_source"], reason="no_canonical_source",
            )
        return records

    # ------------------------------------------------------------------ records

    def _record(
        self, factor_id, grid_id, *, status, raw_value, normalized_index,
        normalization, coverage, quality_flags, reason, resolved=None,
        provenance_extra=None,
    ):
        definition = FACTOR_DEFINITIONS[factor_id]
        role = FACTOR_SOURCE_ROLE[factor_id]
        source = self._source(role)
        attribute = self._attribute(role)
        cell = self._cell(role, grid_id)
        if resolved is None:
            resolved = status == STATUS_PASSED
        provenance = {
            "source_role": role,
            "source_id": source["source_id"],
            "source_fingerprint": source["source_fingerprint"],
            "attribute_status": attribute.get("status"),
            "attribute_algorithm_id": attribute.get("algorithm_id"),
            "attribute_algorithm_version": (
                attribute.get("algorithm_version") or attribute.get("sampling_version")
            ),
            "grid_level": attribute.get("grid_level"),
            "cell_status": cell.get("status"),
            "cell_reason": cell.get("reason"),
            "canonical_field": definition.get("source_field"),
            "grid_id": grid_id,
            "normalization_reference_fingerprint": (normalization or {}).get("reference_fingerprint"),
        }
        if provenance_extra:
            provenance.update(provenance_extra)
        return {
            "factor_id": factor_id,
            "domain": definition["domain"],
            "status": status,
            "resolved": bool(resolved),
            "raw_value": raw_value,
            "raw_unit": definition.get("raw_unit"),
            "normalized_index": (
                clip(normalized_index) if finite(normalized_index) else None
            ),
            "normalization": deepcopy(normalization),
            "source_role": role,
            "source_id": source["source_id"],
            "source_fingerprint": source["source_fingerprint"],
            "coverage": coverage if coverage is None else float(coverage),
            "quality_flags": list(quality_flags),
            "provenance": provenance,
            "reason": reason,
        }

    # ------------------------------------------------------------------ summary

    def _summarize(self, factor_id, records, reference):
        definition = FACTOR_DEFINITIONS[factor_id]
        counts = {}
        coverages = []
        flags = []
        normalization = None
        for record in records.values():
            counts[record["status"]] = counts.get(record["status"], 0) + 1
            if record["normalized_index"] is not None and finite(record.get("coverage")):
                coverages.append(float(record["coverage"]))
            for flag in record["quality_flags"]:
                if flag not in flags:
                    flags.append(flag)
            if normalization is None:
                normalization = record["normalization"]
        passed = counts.get(STATUS_PASSED, 0)
        partial = counts.get(STATUS_PARTIAL, 0)
        stale = counts.get(STATUS_STALE, 0)
        missing = sum(
            counts.get(name, 0) for name in (STATUS_MISSING, STATUS_NOT_AVAILABLE)
        )
        unknown = counts.get(STATUS_UNKNOWN, 0)
        total = len(records)
        if total and stale == total:
            status, readiness = STATUS_STALE, READINESS_STALE
        elif partial and not stale:
            status, readiness = STATUS_PARTIAL, READINESS_PARTIAL
        elif passed and not stale and not partial:
            status = STATUS_PASSED if missing == 0 and unknown == 0 else STATUS_PARTIAL
            readiness = READINESS_READY if status == STATUS_PASSED else READINESS_PARTIAL
        elif passed:
            status, readiness = STATUS_PARTIAL, READINESS_PARTIAL
        elif unknown:
            status, readiness = STATUS_UNKNOWN, READINESS_BLOCKED
        elif stale:
            status, readiness = STATUS_STALE, READINESS_STALE
        elif total:
            status, readiness = STATUS_MISSING, READINESS_BLOCKED
        else:
            status, readiness = STATUS_NOT_AVAILABLE, READINESS_BLOCKED
        source = self._source(FACTOR_SOURCE_ROLE[factor_id])
        return {
            "factor_id": factor_id,
            "domain": definition["domain"],
            "status": status,
            "readiness": readiness,
            "raw_unit": definition.get("raw_unit"),
            "cell_count": total,
            "status_counts": counts,
            "passed_cell_count": passed,
            "partial_cell_count": partial,
            "missing_cell_count": missing,
            "unknown_cell_count": unknown,
            "stale_cell_count": stale,
            "coverage": (sum(coverages) / len(coverages)) if coverages else None,
            "normalization": deepcopy(normalization),
            "reference": deepcopy(reference),
            "source_role": FACTOR_SOURCE_ROLE[factor_id],
            "source_id": source["source_id"],
            "source_fingerprint": source["source_fingerprint"],
            "quality_flags": flags,
            "resolved": readiness == READINESS_READY,
            "canonical_source_available": factor_id not in NO_SOURCE_FACTOR_IDS,
        }

    # -------------------------------------------------------------- fingerprint

    def _input_status(self):
        return {
            name: (self._attribute(name).get("status") or "not_calculated")
            for name in FACTOR_INPUT_ATTRIBUTES
        }

    def _source_versions(self):
        versions = {}
        for name in FACTOR_INPUT_ATTRIBUTES:
            attributes = self._attribute(name)
            source = self._source(name)
            versions[name] = {
                "status": attributes.get("status", "not_calculated"),
                "algorithm_id": attributes.get("algorithm_id"),
                "version": (
                    attributes.get("algorithm_version") or attributes.get("sampling_version")
                ),
                "source_fingerprint": source["source_fingerprint"],
                "source_id": source["source_id"],
            }
        return versions

    def _input_fingerprint(self, factors, references):
        payload = {
            "grid": {"level": self.grid_level, "cells": list(self.cell_ids)},
            "inputs": self._source_versions(),
            "input_status": self._input_status(),
            "references": {
                factor_id: reference for factor_id, reference in references.items()
            },
            "factor_inputs": {
                factor_id: {
                    grid_id: [record["status"], record["raw_value"], record["normalized_index"]]
                    for grid_id, record in (factors.get(factor_id) or {}).items()
                }
                for factor_id in FACTOR_IDS
            },
        }
        return stable_fingerprint(payload, prefix="riskinputv2-")

    # ------------------------------------------------------------------ helpers

    def _canonical_field_reference(self, factor_id, norm_field):
        record = {
            "mode": "source_provided_canonical_field",
            "field": norm_field,
            "value": None,
            "resolved_value": None,
            "source_role": FACTOR_SOURCE_ROLE[factor_id],
        }
        record["fingerprint"] = stable_fingerprint(record, prefix="riskrefv2-")
        return record

    def _identity_reference(self, factor_id, field):
        record = {
            "mode": "identity_ratio",
            "field": field,
            "value": 1.0,
            "resolved_value": 1.0,
            "source_role": FACTOR_SOURCE_ROLE[factor_id],
        }
        record["fingerprint"] = stable_fingerprint(record, prefix="riskrefv2-")
        return record

    def _attribute(self, name):
        return self.attributes.get(name) or {}

    def _cell(self, name, grid_id):
        cells = self._attribute(name).get("cells") or {}
        cell = cells.get(grid_id)
        return cell if isinstance(cell, dict) else {}

    def _source(self, name):
        if name not in self._sources:
            attribute = self._attribute(name)
            source = attribute.get("source") if isinstance(attribute.get("source"), dict) else None
            source_id = None
            if source:
                source_id = source.get("path") or source.get("source_id") or source.get("layer")
            record = {
                "source_role": name,
                "source_id": source_id,
                "source_fingerprint": stable_fingerprint({
                    "role": name,
                    "source": source,
                    "algorithm_id": attribute.get("algorithm_id"),
                    "algorithm_version": (
                        attribute.get("algorithm_version") or attribute.get("sampling_version")
                    ),
                    "grid_level": attribute.get("grid_level"),
                }, prefix="risksourcev2-"),
            }
            self._sources[name] = record
        return self._sources[name]

    def _unavailable(self, name, cell):
        attribute = self._attribute(name)
        if attribute.get("status") == "stale" or cell.get("status") == "stale":
            return STATUS_STALE, "input_stale"
        if cell.get("status") == "passed":
            return None, None
        if attribute.get("status") in (None, "not_calculated"):
            return STATUS_NOT_AVAILABLE, "attribute_not_calculated"
        return STATUS_MISSING, cell.get("reason") or cell.get("status") or "cell_missing_data"

    @staticmethod
    def _finite_or_none(value):
        return float(value) if finite(value) else None


__all__ = [
    "FactorExtraction", "READINESS_BLOCKED", "READINESS_PARTIAL", "READINESS_READY",
    "READINESS_STALE", "STATUS_MISSING", "STATUS_NOT_AVAILABLE", "STATUS_PARTIAL",
    "STATUS_PASSED", "STATUS_STALE", "STATUS_UNKNOWN",
]
