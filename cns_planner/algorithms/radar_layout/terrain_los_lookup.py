"""Bounded, deterministic Terrain LOS lookup for Radar layout preintegration.

The lookup owns no GIS resource and performs no terrain sampling itself.  A caller-provided
evaluator adapts the existing GIS LOS implementation; this module only applies the frozen Radar
geometry gate, cache identity, execution budget, and three-mode contract.
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from itertools import islice

from ...domain.radar_surveillance_layout import radar_geometry_parameters
from ...domain.radar_terrain_los import (
    LOS_STATUS_BLOCKED,
    LOS_STATUS_CLEAR,
    LOS_STATUS_UNKNOWN,
    RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
    stable_fingerprint,
)
from .geometry import geometry_evaluation


TERRAIN_LOS_MODE_DISABLED = "disabled"
TERRAIN_LOS_MODE_DISCLOSURE_ONLY = "disclosure_only"
TERRAIN_LOS_MODE_ENFORCED = "enforced"
TERRAIN_LOS_MODES = (
    TERRAIN_LOS_MODE_DISABLED,
    TERRAIN_LOS_MODE_DISCLOSURE_ONLY,
    TERRAIN_LOS_MODE_ENFORCED,
)

LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE = "out_of_equipment_range"
LOS_LOOKUP_STATUS_GEOMETRY_FILTERED = "geometry_filtered"
LOS_LOOKUP_STATUS_DISABLED = "disabled"
LOS_LOOKUP_STATUS_ELIGIBLE = "los_eligible"
LOS_LOOKUP_STATUS_NOT_SAMPLED = "not_sampled"
LOS_LOOKUP_STATUSES = (
    LOS_STATUS_CLEAR,
    LOS_STATUS_BLOCKED,
    LOS_STATUS_UNKNOWN,
    LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE,
    LOS_LOOKUP_STATUS_GEOMETRY_FILTERED,
    LOS_LOOKUP_STATUS_DISABLED,
)

TERRAIN_LOS_ENFORCED_SEMANTICS_VERSION = "1.4-preintegration"
DEFAULT_MAX_RAY_COUNT = 4096
#: Building domain composition of the one shared lookup (D2-C0 pre-integration).
BUILDING_MODE_DISABLED = "disabled"
BUILDING_MODE_COMBINED = "combined"
BUILDING_MODES = (BUILDING_MODE_DISABLED, BUILDING_MODE_COMBINED)
#: The building LOS gate and this lookup share one status vocabulary.  The building
#: module spells the filtered states itself (importing this module from it would close an
#: import cycle), so the equivalence is exported for an explicit regression assertion
#: rather than being assumed.
BUILDING_GATE_FILTERED_STATUS_EQUIVALENCE = (
    LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE,
    LOS_LOOKUP_STATUS_GEOMETRY_FILTERED,
    LOS_STATUS_UNKNOWN,
)


def normalize_terrain_los_mode(value):
    mode = TERRAIN_LOS_MODE_DISABLED if value in (None, "") else str(value).strip()
    if mode not in TERRAIN_LOS_MODES:
        raise ValueError(
            "terrain_los_mode 仅允许 disabled、disclosure_only 或 enforced"
        )
    return mode


def validate_terrain_los_lookup(mode, lookup):
    """Return normalized mode and reject missing or mismatched lookup contracts."""

    normalized = normalize_terrain_los_mode(mode)
    if lookup is None:
        if normalized != TERRAIN_LOS_MODE_DISABLED:
            raise ValueError("非 disabled terrain_los_mode 必须提供统一 terrain_los_lookup")
        return normalized
    lookup_mode = getattr(lookup, "mode", None)
    if lookup_mode not in TERRAIN_LOS_MODES or lookup_mode != normalized:
        raise ValueError("terrain_los_mode 必须与 terrain_los_lookup.mode 一致")
    return normalized


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _metric(value):
    if not (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and _finite(value[0])
        and _finite(value[1])
    ):
        return None
    return [float(value[0]), float(value[1])]


def _terrain_sample_count(result):
    direct = result.get("terrain_sample_count")
    if isinstance(direct, int) and not isinstance(direct, bool) and direct >= 0:
        return direct
    profile = result.get("terrain_profile")
    if isinstance(profile, dict):
        count = profile.get("sample_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
    return 0


class TerrainLosLookup:
    """One execution context for cached ``tower × probe × radar_type`` LOS evidence.

    D2-C0 pre-integration: when a ``building_evaluator`` is supplied, the same lookup,
    the same cache key, the same geometry gate and the same budgets also carry the
    building (2.5D prism) domain, and every consumer of this lookup therefore sees one
    consistent combined visibility evidence for candidates, MILP coverage sets,
    ``actual_site_coverage`` and the 5 m validation/refinement.
    """

    def __init__(self, *, mode=TERRAIN_LOS_MODE_DISABLED, evaluator=None,
                 building_evaluator=None, building_mode=BUILDING_MODE_DISABLED,
                 source_identity=None, building_source_identity=None, route_id=None,
                 sampling=None, max_ray_count=DEFAULT_MAX_RAY_COUNT, cancel_check=None):
        self.mode = normalize_terrain_los_mode(mode)
        self.evaluator = evaluator
        self.building_evaluator = building_evaluator if callable(building_evaluator) else None
        if building_mode not in BUILDING_MODES:
            raise ValueError("building_mode 仅允许 disabled 或 combined")
        if building_mode == BUILDING_MODE_COMBINED and self.building_evaluator is None:
            raise ValueError("building_mode=combined 必须提供 building_evaluator")
        self.building_mode = building_mode
        self.source_identity = deepcopy(source_identity)
        self.building_source_identity = deepcopy(building_source_identity)
        self.route_id = None if route_id in (None, "") else str(route_id)
        self.sampling = deepcopy(sampling) if isinstance(sampling, dict) else {}
        if not isinstance(max_ray_count, int) or isinstance(max_ray_count, bool) or max_ray_count < 0:
            raise ValueError("max_ray_count 必须是非负整数")
        self.max_ray_count = max_ray_count
        self.cancel_check = cancel_check if callable(cancel_check) else None
        self._cache = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._ray_count = 0
        self._terrain_sample_count = 0
        self._building_ray_count = 0
        self._building_candidate_count = 0
        self._building_exact_intersection_count = 0
        self._building_gate_filtered_count = 0
        self._building_gate_unresolved_count = 0
        self._combined_status_counts = {
            LOS_STATUS_CLEAR: 0, LOS_STATUS_BLOCKED: 0, LOS_STATUS_UNKNOWN: 0,
        }
        self._elapsed_seconds = 0.0
        self._complete = True
        self._incomplete_reason = None

    def _geometry(self, tower, sample, geometry):
        if isinstance(geometry, dict):
            required = (
                "horizontal_distance_m", "vertical_delta_m", "slant_distance_m",
                "elevation_deg", "bearing_deg",
            )
            if all(_finite(geometry.get(key)) for key in required):
                return {key: float(geometry[key]) for key in required}
            return None
        tower_metric = _metric(tower.get("metric"))
        sample_metric = _metric(sample.get("metric"))
        origin = tower.get("origin_egm2008_m")
        target = sample.get("egm2008_m")
        if tower_metric is None or sample_metric is None or not _finite(origin) or not _finite(target):
            return None
        return geometry_evaluation(
            origin_egm2008_m=float(origin),
            sample_egm2008_m=float(target),
            sample_metric=sample_metric,
            tower_metric=tower_metric,
        )

    def _key(self, tower, sample, radar_type, geometry):
        payload = {
            "route_id": self.route_id or sample.get("route_id"),
            "tower_id": str(tower.get("tower_id") or ""),
            "tower_metric": _metric(tower.get("metric")),
            "origin_egm2008_m": tower.get("origin_egm2008_m"),
            "origin_source": tower.get("origin_source"),
            "origin_state": tower.get("origin_state"),
            "origin_evidence_level": tower.get("origin_evidence_level"),
            "origin_availability": tower.get("origin_availability"),
            "device_instance_id": tower.get("device_instance_id"),
            "origin_input_fingerprint": tower.get("origin_input_fingerprint"),
            # Internal sample ids are intentionally excluded: refinement renames an existing
            # physical validation probe (for example V000007 -> R000007).  LOS belongs to the
            # physical ray, so route + metric + EGM2008 height + radar type is the stable identity.
            "sample_metric": _metric(sample.get("metric")),
            "sample_egm2008_m": sample.get("egm2008_m"),
            "altitude_layer_id": sample.get("altitude_layer_id"),
            "radar_type": str(radar_type),
            "geometry": geometry,
            "source_identity": self.source_identity,
            "building_source_identity": self.building_source_identity,
            "sampling": self.sampling,
            "mode": self.mode,
            "building_mode": self.building_mode,
            "los_core_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
            "enforced_semantics_version": TERRAIN_LOS_ENFORCED_SEMANTICS_VERSION,
        }
        return stable_fingerprint(payload)

    def geometry_gate(self, tower, sample, radar_type, *, geometry=None):
        """Classify one combination without opening DEM or mutating lookup state.

        The frozen device decision comes from the single shared classifier in
        ``domain.radar_building_los`` so this lookup and the building LOS adapter can
        never disagree about whether a ray belongs to the device geometry.
        """

        tower = tower if isinstance(tower, dict) else {}
        sample = sample if isinstance(sample, dict) else {}
        parameters = radar_geometry_parameters(radar_type)
        values = self._geometry(tower, sample, geometry)
        key = self._key(tower, sample, radar_type, values)
        common = {
            "tower_id": str(tower.get("tower_id") or ""),
            "sample_id": str(sample.get("sample_id") or sample.get("voxel_id") or ""),
            "route_id": self.route_id or sample.get("route_id"),
            "radar_type": str(radar_type),
            "cache_key": key,
            "horizontal_distance_m": None if values is None else values["horizontal_distance_m"],
            "vertical_delta_m": None if values is None else values["vertical_delta_m"],
            "slant_distance_m": None if values is None else values["slant_distance_m"],
            "elevation_deg": None if values is None else values["elevation_deg"],
            "within_equipment_range": None,
            "terrain_sample_count": 0,
        }
        if values is None:
            return {
                **common, "status": LOS_STATUS_UNKNOWN,
                "reason": "geometry_inputs_incomplete", "geometry": None,
            }
        # Deferred import: the building LOS domain module imports the pure geometry
        # helpers of this package, so a module-level import here would close a cycle.
        from ...domain.radar_building_los import classify_frozen_device_geometry
        decision = classify_frozen_device_geometry(
            radar_type=radar_type, geometry=values, origin_metric=None,
            target_metric=None, origin_egm2008_m=None, target_egm2008_m=None,
        )
        common["within_equipment_range"] = decision["within_equipment_range"]
        if decision["status"] == "out_of_equipment_range":
            status = LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
            reason = "outside_inclusive_slant_range"
        elif decision["status"] == "geometry_filtered":
            status = LOS_LOOKUP_STATUS_GEOMETRY_FILTERED
            reason = "outside_elevation_interval"
        elif self.mode == TERRAIN_LOS_MODE_DISABLED:
            status = LOS_LOOKUP_STATUS_DISABLED
            reason = None
        else:
            status = LOS_LOOKUP_STATUS_ELIGIBLE
            reason = None
        return {**common, "status": status, "reason": reason, "geometry": values}

    def _building_gate(self, tower, sample, radar_type, values):
        """Frozen device geometry gate of the building domain, or ``None`` if unused."""

        if self.building_mode != BUILDING_MODE_COMBINED:
            return None
        # Deferred import: the building LOS domain module imports the pure geometry
        # helpers of this package, so a module-level import here would close a cycle.
        from ...domain.radar_building_los import frozen_geometry_gate

        return frozen_geometry_gate(
            radar_type=radar_type, origin_metric=None, target_metric=None,
            origin_egm2008_m=None, target_egm2008_m=None, geometry=values,
        )

    def _evaluate_building(self, tower, sample, radar_type, values):
        if self.cancel_check is not None:
            self.cancel_check()
        started = time.perf_counter()
        evaluated = self.building_evaluator(
            tower=tower, sample=sample, radar_type=str(radar_type),
            geometry=deepcopy(values),
        )
        self._elapsed_seconds += max(0.0, time.perf_counter() - started)
        self._building_ray_count += 1
        evaluated = evaluated if isinstance(evaluated, dict) else {}
        self._building_candidate_count += int(evaluated.get("candidate_building_count") or 0)
        self._building_exact_intersection_count += int(
            evaluated.get("exact_intersection_count") or 0
        )
        return evaluated

    def _set_incomplete_reason(self, reason):
        """Record the most specific incomplete reason seen by this lookup.

        A generic "the evidence is incomplete" must never overwrite the concrete cause
        that was already observed (an exhausted budget, a truncated query, an unknown
        physical domain), so the snapshot keeps reporting the actionable reason.
        """

        current = self._incomplete_reason
        if current is None:
            self._incomplete_reason = reason
            return
        priority = {
            # A concrete domain that is still undecided is the most actionable cause, ahead
            # of the generic "this ray's evidence is incomplete" roll-up.
            "building_los_evidence_unknown": 3,
            "los_evidence_unknown": 3,
            "los_evidence_incomplete": 2,
            "los_computation_budget_exhausted": 2,
        }
        if priority.get(str(reason), 1) > priority.get(str(current), 1):
            self._incomplete_reason = reason

    @staticmethod
    def _incomplete_reason_for(combined, terrain_result, building_result):
        """Name the most specific reason this combined ray is not fully decided."""

        if str((combined or {}).get("combined_visibility")) == LOS_STATUS_UNKNOWN:
            return (
                "building_los_evidence_unknown" if building_result is not None
                else "los_evidence_unknown"
            )
        for domain, name in (
            ((combined or {}).get("terrain_los"), "terrain"),
            ((combined or {}).get("building_los"), "building"),
        ):
            if not isinstance(domain, dict):
                continue
            if str(domain.get("status") or "") != LOS_STATUS_UNKNOWN:
                continue
            if str(domain.get("domain_state") or "") == "evaluated":
                return f"{name}_los_evidence_unknown"
        return "los_evidence_incomplete"

    def _combine_domains(self, gate, terrain_result, building_result):
        """Merge Terrain + Building without letting either domain hide the other.

        ``combine_visibility`` owns the whole verdict/completion algebra.  This method
        must never promote a completion because the status looks decisive: a filtered or
        never-evaluated domain keeps the combined completion incomplete even when the
        other domain already reports a credible physical block.
        """

        from ...domain.radar_visibility_evidence import combine_visibility

        combined = combine_visibility(terrain_result, building_result)
        status = str(combined.get("combined_visibility") or LOS_STATUS_UNKNOWN)
        if status not in (LOS_STATUS_CLEAR, LOS_STATUS_BLOCKED, LOS_STATUS_UNKNOWN):
            # A filtered/not-evaluated ray is not a physical verdict.
            status = LOS_STATUS_UNKNOWN
        computation = str(combined.get("computation_completion") or "incomplete")
        evidence = str(combined.get("evidence_completion") or "incomplete")
        if status == LOS_STATUS_UNKNOWN:
            self._complete = False
            reason = (
                self._incomplete_reason
                or self._incomplete_reason_for(combined, terrain_result, building_result)
            )
        elif computation != "complete" or evidence != "complete":
            # A decisive status must not hide an unevaluated domain, a budget-cut
            # computation or insufficient evidence: the global search was not fully
            # evaluated even though this single ray has a verdict.
            self._complete = False
            reason = self._incomplete_reason_for(combined, terrain_result, building_result)
        else:
            reason = None
        if reason is not None:
            self._set_incomplete_reason(reason)
        self._combined_status_counts[status] += 1
        result = {
            **gate,
            "status": status,
            "reason": (
                (building_result or {}).get("reason") if status == LOS_STATUS_BLOCKED
                and building_result is not None
                and str(building_result.get("status")) == LOS_STATUS_BLOCKED
                else (terrain_result or {}).get("reason") if terrain_result is not None
                else None
            ),
            "terrain_los": deepcopy(terrain_result),
            "building_los": deepcopy(building_result),
            "combined_visibility": combined,
            "combined_visibility_status": status,
            "computation_completion": computation,
            "evidence_completion": evidence,
            "building_los_executed": building_result is not None,
            # Both domains answer the same ray, so the combined evidence is only
            # complete when neither domain is unknown or filtered out.
            "terrain_los_status": (
                None if terrain_result is None else terrain_result.get("status")
            ),
            "building_los_status": (
                None if building_result is None else building_result.get("status")
            ),
            "terrain_sample_count": _terrain_sample_count(terrain_result or {}),
        }
        return result

    def evaluate(self, tower, sample, radar_type, *, geometry=None):
        tower = tower if isinstance(tower, dict) else {}
        sample = sample if isinstance(sample, dict) else {}
        gate = self.geometry_gate(tower, sample, radar_type, geometry=geometry)
        values = gate.pop("geometry")
        key = gate["cache_key"]
        if key in self._cache:
            self._cache_hits += 1
            return deepcopy(self._cache[key])
        self._cache_misses += 1
        building_gate = self._building_gate(tower, sample, radar_type, values)
        if building_gate is not None and not building_gate["within_frozen_device_range"]:
            self._building_gate_filtered_count += 1
            if not building_gate["device_geometry_resolved"]:
                self._building_gate_unresolved_count += 1
        if gate["status"] != LOS_LOOKUP_STATUS_ELIGIBLE:
            # The terrain domain is filtered, disabled or has unexplained geometry.  A
            # combined lookup still reports the building domain's own gate verdict so
            # every consumer reads one consistent structure for the same ray.
            if building_gate is not None and gate["status"] != LOS_STATUS_UNKNOWN:
                result = self._combine_domains(gate, dict(gate), {
                    "status": building_gate["status"],
                    "reason": building_gate["reason"],
                    "computation_completion": (
                        "complete" if building_gate["within_frozen_device_range"]
                        else "not_evaluated"
                    ),
                    "evidence_completion": "not_evaluated",
                    "geometry_gate": deepcopy(building_gate),
                    "within_frozen_device_range": bool(
                        building_gate["within_frozen_device_range"]
                    ),
                    "building_los_physical_unknown": False,
                })
            else:
                result = gate
        elif self._ray_count >= self.max_ray_count:
            self._complete = False
            self._incomplete_reason = "los_computation_budget_exhausted"
            result = {
                **gate, "status": LOS_STATUS_UNKNOWN,
                "reason": self._incomplete_reason,
            }
        elif not callable(self.evaluator):
            self._complete = False
            self._incomplete_reason = "los_evaluator_unavailable"
            result = {
                **gate, "status": LOS_STATUS_UNKNOWN,
                "reason": self._incomplete_reason,
            }
        else:
            if self.cancel_check is not None:
                self.cancel_check()
            started = time.perf_counter()
            evaluated = self.evaluator(
                tower=tower, sample=sample, radar_type=str(radar_type),
                geometry=deepcopy(values),
            )
            self._elapsed_seconds += max(0.0, time.perf_counter() - started)
            self._ray_count += 1
            evaluated = evaluated if isinstance(evaluated, dict) else {}
            status = str(evaluated.get("status") or LOS_STATUS_UNKNOWN)
            if status not in (LOS_STATUS_CLEAR, LOS_STATUS_BLOCKED, LOS_STATUS_UNKNOWN):
                status = LOS_STATUS_UNKNOWN
            sample_count = _terrain_sample_count(evaluated)
            self._terrain_sample_count += sample_count
            terrain_result = {
                **gate,
                **deepcopy(evaluated),
                "status": status,
                "terrain_sample_count": sample_count,
            }
            if building_gate is not None and building_gate["within_frozen_device_range"]:
                building_result = self._evaluate_building(
                    tower, sample, radar_type, values,
                )
                result = self._combine_domains(gate, terrain_result, building_result)
            elif building_gate is not None:
                # Device geometry already excluded this ray: the building domain is
                # filtered, not unknown, and no building query was issued.
                result = self._combine_domains(gate, terrain_result, {
                    "status": building_gate["status"],
                    "reason": building_gate["reason"],
                    "computation_completion": "not_evaluated",
                    "evidence_completion": "not_evaluated",
                    "geometry_gate": deepcopy(building_gate),
                    "within_frozen_device_range": False,
                    "building_los_physical_unknown": False,
                })
            else:
                result = terrain_result

        if result.get("status") == LOS_STATUS_UNKNOWN:
            self._complete = False
            if self._incomplete_reason is None:
                self._incomplete_reason = str(
                    result.get("reason") or "los_evidence_unknown"
                )

        self._cache[key] = deepcopy(result)
        return deepcopy(result)

    def evaluate_batches(self, combinations, *, batch_size=64,
                         max_elapsed_seconds=None, retain_records=False,
                         evidence_limit_per_class=3, clock=None):
        """Stream deterministic combinations through geometry gate and bounded LOS.

        ``combinations`` yields mappings with ``tower``, ``sample``, ``radar_type`` and an
        optional precomputed ``geometry``.  Geometry-filtered combinations never enter the
        lookup cache.  Once either execution budget is exhausted, remaining inputs are still
        geometry-classified but are reported as ``not_sampled`` rather than clear/blocked.
        """

        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")
        if max_elapsed_seconds is not None and (
            not _finite(max_elapsed_seconds) or float(max_elapsed_seconds) < 0.0
        ):
            raise ValueError("max_elapsed_seconds 必须是非负有限数")
        if (
            not isinstance(evidence_limit_per_class, int)
            or isinstance(evidence_limit_per_class, bool)
            or evidence_limit_per_class < 0
        ):
            raise ValueError("evidence_limit_per_class 必须是非负整数")
        now = clock if callable(clock) else time.perf_counter
        started = now()
        before = self.snapshot()
        iterator = iter(combinations or ())
        classification_counts = {
            "clear": 0,
            "blocked": 0,
            "physical_unknown": 0,
            "geometry_unknown": 0,
            LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE: 0,
            LOS_LOOKUP_STATUS_GEOMETRY_FILTERED: 0,
            LOS_LOOKUP_STATUS_DISABLED: 0,
            LOS_LOOKUP_STATUS_NOT_SAMPLED: 0,
        }
        evidence_samples = {key: [] for key in classification_counts}
        executed_los_status_counts = {
            LOS_STATUS_CLEAR: 0, LOS_STATUS_BLOCKED: 0, LOS_STATUS_UNKNOWN: 0,
        }
        records = []
        total = eligible = batches = 0
        budget_reasons = []

        def _record(item, classification):
            classification_counts[classification] += 1
            if len(evidence_samples[classification]) < evidence_limit_per_class:
                evidence_samples[classification].append(deepcopy(item))
            if retain_records:
                records.append(deepcopy(item))

        while True:
            batch = list(islice(iterator, batch_size))
            if not batch:
                break
            batches += 1
            if self.cancel_check is not None:
                self.cancel_check()
            for combination in batch:
                combination = combination if isinstance(combination, dict) else {}
                tower = combination.get("tower")
                sample = combination.get("sample")
                radar_type = combination.get("radar_type")
                geometry = combination.get("geometry")
                gate = self.geometry_gate(tower, sample, radar_type, geometry=geometry)
                values = gate.pop("geometry")
                total += 1
                gate_status = gate["status"]
                if gate_status == LOS_STATUS_UNKNOWN:
                    _record(gate, "geometry_unknown")
                    continue
                if gate_status != LOS_LOOKUP_STATUS_ELIGIBLE:
                    _record(gate, gate_status)
                    continue
                eligible += 1
                cached = gate["cache_key"] in self._cache
                elapsed_exhausted = (
                    max_elapsed_seconds is not None
                    and max(0.0, now() - started) >= float(max_elapsed_seconds)
                )
                ray_exhausted = self._ray_count >= self.max_ray_count
                if not cached and (elapsed_exhausted or ray_exhausted):
                    reason = (
                        "elapsed_time_budget_exhausted"
                        if elapsed_exhausted else "ray_budget_exhausted"
                    )
                    self._complete = False
                    self._incomplete_reason = (
                        "los_elapsed_time_budget_exhausted"
                        if elapsed_exhausted else "los_computation_budget_exhausted"
                    )
                    if reason not in budget_reasons:
                        budget_reasons.append(reason)
                    _record({
                        **gate,
                        "status": LOS_LOOKUP_STATUS_NOT_SAMPLED,
                        "reason": reason,
                        "complete": False,
                    }, LOS_LOOKUP_STATUS_NOT_SAMPLED)
                    continue
                rays_before = self._ray_count
                result = self.evaluate(
                    tower, sample, radar_type, geometry=values,
                )
                status = result.get("status")
                if self._ray_count > rays_before and status in executed_los_status_counts:
                    executed_los_status_counts[status] += 1
                building_gate = (result.get("building_los") or {}).get("geometry_gate")
                if isinstance(building_gate, dict) and not building_gate.get(
                    "within_frozen_device_range"
                ) and status == LOS_STATUS_UNKNOWN:
                    # The building domain was excluded by the frozen device geometry;
                    # that is a filtered combination, never building physical unknown.
                    _record(result, LOS_LOOKUP_STATUS_GEOMETRY_FILTERED)
                    continue
                if status == LOS_STATUS_CLEAR:
                    classification = "clear"
                elif status == LOS_STATUS_BLOCKED:
                    classification = "blocked"
                elif status == LOS_STATUS_UNKNOWN:
                    classification = "physical_unknown"
                else:
                    classification = str(status)
                _record(result, classification)

        elapsed = max(0.0, now() - started)
        after = self.snapshot()
        skipped = classification_counts[LOS_LOOKUP_STATUS_NOT_SAMPLED]
        geometry_filtered = (
            classification_counts[LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE]
            + classification_counts[LOS_LOOKUP_STATUS_GEOMETRY_FILTERED]
        )
        computation_complete = skipped == 0
        evidence_complete = (
            computation_complete
            and classification_counts["physical_unknown"] == 0
            and classification_counts["geometry_unknown"] == 0
        )
        executed = after["los_ray_count"] - before["los_ray_count"]
        los_status_counts = {
            "clear": classification_counts["clear"],
            "blocked": classification_counts["blocked"],
            "unknown": classification_counts["physical_unknown"],
        }
        return {
            "total_combinations": total,
            "batch_size": batch_size,
            "batch_count": batches,
            "geometry_filtered_combinations": geometry_filtered,
            "geometry_unknown_combinations": classification_counts["geometry_unknown"],
            "los_eligible_combinations": eligible,
            "executed_rays": executed,
            "executed_los_status_counts": executed_los_status_counts,
            "los_status_counts": los_status_counts,
            "classification_counts": classification_counts,
            "not_sampled_count": skipped,
            "skipped_by_budget": skipped,
            "cache_hit_count": after["cache_hit_count"] - before["cache_hit_count"],
            "cache_miss_count": after["cache_miss_count"] - before["cache_miss_count"],
            "terrain_sample_count": (
                after["terrain_sample_count"] - before["terrain_sample_count"]
            ),
            "max_ray_count": self.max_ray_count,
            "max_elapsed_seconds": max_elapsed_seconds,
            "elapsed_seconds": round(elapsed, 6),
            "elapsed_budget_overshoot_seconds": round(
                max(0.0, elapsed - float(max_elapsed_seconds))
                if max_elapsed_seconds is not None else 0.0,
                6,
            ),
            "elapsed_budget_checkpoint": (
                "batch_and_ray_boundaries_in_flight_evaluator_not_preempted"
            ),
            "ray_throughput_per_second": (
                round(executed / elapsed, 6) if elapsed > 0.0 else None
            ),
            "budget_exhausted": bool(budget_reasons),
            "budget_exhausted_reasons": budget_reasons,
            "computation_complete": computation_complete,
            "evidence_complete": evidence_complete,
            "complete": evidence_complete,
            "completion_status": "complete" if computation_complete else "search_incomplete",
            "incomplete_reason": (
                budget_reasons[0]
                if budget_reasons
                else (None if evidence_complete else "physical_or_geometry_unknown")
            ),
            "evidence_samples": evidence_samples,
            "records": records if retain_records else None,
            "full_los_matrix_embedded": False,
        }

    def snapshot(self):
        records = [self._cache[key] for key in sorted(self._cache)]
        status_counts = {
            status: sum(1 for item in records if item.get("status") == status)
            for status in LOS_LOOKUP_STATUSES
        }
        compact_inputs = {
            "mode": self.mode,
            "building_mode": self.building_mode,
            "route_id": self.route_id,
            "source_identity": self.source_identity,
            "building_source_identity": self.building_source_identity,
            "sampling": self.sampling,
            "max_ray_count": self.max_ray_count,
            "los_core_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
            "enforced_semantics_version": TERRAIN_LOS_ENFORCED_SEMANTICS_VERSION,
            "evaluated_combination_fingerprints": sorted(self._cache),
        }
        return {
            "mode": self.mode,
            "building_mode": self.building_mode,
            "enforced_semantics_version": TERRAIN_LOS_ENFORCED_SEMANTICS_VERSION,
            "los_core_version": RADAR_TERRAIN_LOS_ALGORITHM_VERSION,
            "input_fingerprint": stable_fingerprint(compact_inputs),
            "combination_count": len(records),
            "status_counts": status_counts,
            "combined_status_counts": dict(self._combined_status_counts),
            "los_ray_count": self._ray_count,
            "building_ray_count": self._building_ray_count,
            "building_candidate_count": self._building_candidate_count,
            "building_exact_intersection_count": self._building_exact_intersection_count,
            "building_gate_filtered_count": self._building_gate_filtered_count,
            "building_gate_unresolved_count": self._building_gate_unresolved_count,
            "terrain_sample_count": self._terrain_sample_count,
            "cache_hit_count": self._cache_hits,
            "cache_miss_count": self._cache_misses,
            "max_ray_count": self.max_ray_count,
            "complete": self._complete,
            "incomplete_reason": self._incomplete_reason,
            "elapsed_seconds": round(self._elapsed_seconds, 6),
            "full_los_matrix_embedded": False,
        }


__all__ = [
    "BUILDING_GATE_FILTERED_STATUS_EQUIVALENCE", "BUILDING_MODE_COMBINED",
    "BUILDING_MODE_DISABLED", "BUILDING_MODES",
    "DEFAULT_MAX_RAY_COUNT", "LOS_LOOKUP_STATUS_DISABLED", "LOS_LOOKUP_STATUS_ELIGIBLE",
    "LOS_LOOKUP_STATUS_GEOMETRY_FILTERED", "LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE",
    "LOS_LOOKUP_STATUS_NOT_SAMPLED",
    "LOS_LOOKUP_STATUSES", "TERRAIN_LOS_ENFORCED_SEMANTICS_VERSION",
    "TERRAIN_LOS_MODE_DISABLED", "TERRAIN_LOS_MODE_DISCLOSURE_ONLY",
    "TERRAIN_LOS_MODE_ENFORCED", "TERRAIN_LOS_MODES", "TerrainLosLookup",
    "normalize_terrain_los_mode",
    "validate_terrain_los_lookup",
]
