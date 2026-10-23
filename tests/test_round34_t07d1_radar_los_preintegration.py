"""Round34-T07-D1 — Radar Terrain LOS production preintegration."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from cns_planner.algorithms.radar_layout.candidates import build_candidates
from cns_planner.algorithms.radar_layout import v1 as radar_layout_v1
from cns_planner.algorithms.radar_layout.v1 import (
    actual_site_coverage, build_route_samples, solve_layout,
)
from cns_planner.application.radar_surveillance_layout_service import (
    RadarSurveillanceLayoutService,
)
from cns_planner.gis import radar_terrain_los_adapter
from cns_planner.algorithms.radar_layout.terrain_los_lookup import (
    LOS_LOOKUP_STATUS_GEOMETRY_FILTERED,
    LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE,
    TERRAIN_LOS_MODE_DISCLOSURE_ONLY,
    TERRAIN_LOS_MODE_DISABLED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
    normalize_terrain_los_mode,
)


def _tower(*, tower_id="T1", x=0.0, y=0.0, z=100.0):
    return {
        "tower_id": tower_id,
        "metric": [x, y],
        "origin_egm2008_m": z,
        "origin_source": "test",
        "origin_state": "usable_confirmed",
        "origin_evidence_level": "confirmed",
        "origin_availability": "usable",
    }


def _sample(index, *, x, y=0.0, z=100.0, route_id="R-TEST"):
    return {
        "sample_id": f"S{index}",
        "sample_index": index,
        "route_id": route_id,
        "metric": [x, y],
        "egm2008_m": z,
        "distance_along_route_m": float(index),
        "surface_class": "sea",
        "required_distinct_site_count": 1,
    }


class _Evaluator:
    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or ["clear"])

    def __call__(self, *, tower, sample, radar_type, geometry):
        self.calls.append((tower["tower_id"], sample["sample_id"], radar_type))
        status = self.statuses[min(len(self.calls) - 1, len(self.statuses) - 1)]
        return {
            "status": status,
            "reason": None if status == "clear" else f"test_{status}",
            "terrain_sample_count": 3,
        }


@pytest.mark.parametrize(
    "value",
    [TERRAIN_LOS_MODE_DISABLED, TERRAIN_LOS_MODE_DISCLOSURE_ONLY, TERRAIN_LOS_MODE_ENFORCED],
)
def test_mode_normalization_accepts_only_three_explicit_modes(value):
    assert normalize_terrain_los_mode(value) == value
    with pytest.raises(ValueError, match="terrain_los_mode"):
        normalize_terrain_los_mode("enabled")


def test_solver_rejects_mode_lookup_mismatch():
    lookup = TerrainLosLookup(mode="disabled", evaluator=_Evaluator())
    with pytest.raises(ValueError, match="lookup.mode"):
        solve_layout(
            towers=[_tower()], samples=[_sample(0, x=1000.0)],
            terrain_los_mode="enforced", terrain_los_lookup=lookup,
        )


def test_generated_route_samples_preserve_disabled_shape_and_opt_in_los_identity():
    legacy = build_route_samples(
        metric_path=[[0.0, 0.0], [100.0, 0.0]], spacing_m=25.0,
        route_id="R-GENERATED", fixed_altitude_m=100.0,
        altitude_layer_id="ALT-100",
    )
    expected_legacy_keys = {
        "sample_index", "sample_id", "geometry_index", "distance_along_route_m",
        "metric", "longitude", "latitude", "egm2008_m", "surface_class",
        "required_distinct_site_count", "refinement",
    }
    assert set(legacy["samples"][0]) == expected_legacy_keys

    built = build_route_samples(
        metric_path=[[0.0, 0.0], [100.0, 0.0]], spacing_m=25.0,
        route_id="R-GENERATED", fixed_altitude_m=100.0,
        altitude_layer_id="ALT-100", include_los_identity=True,
    )
    assert built["samples"]
    assert {item["route_id"] for item in built["samples"]} == {"R-GENERATED"}
    assert {item["altitude_layer_id"] for item in built["samples"]} == {"ALT-100"}


@pytest.mark.parametrize(
    ("radar_type", "distance_m"),
    [("radar_i", 120.0), ("radar_i", 3000.0), ("radar_ii", 200.0), ("radar_ii", 5000.0)],
)
def test_slant_range_boundaries_are_inclusive_and_evaluated(radar_type, distance_m):
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"})
    result = lookup.evaluate(_tower(), _sample(0, x=distance_m), radar_type)
    assert result["status"] == "clear"
    assert result["within_equipment_range"] is True
    assert result["slant_distance_m"] == pytest.approx(distance_m)
    assert len(evaluator.calls) == 1


@pytest.mark.parametrize(
    ("radar_type", "distance_m"),
    [("radar_i", 119.999), ("radar_i", 3000.001), ("radar_ii", 199.999), ("radar_ii", 5000.001)],
)
def test_out_of_equipment_range_never_calls_los(radar_type, distance_m):
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"})
    result = lookup.evaluate(_tower(), _sample(0, x=distance_m), radar_type)
    assert result["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert result["within_equipment_range"] is False
    assert evaluator.calls == []
    assert lookup.snapshot()["los_ray_count"] == 0
    assert lookup.snapshot()["terrain_sample_count"] == 0


def test_vertical_delta_can_exceed_slant_range_while_horizontal_is_inside():
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"})
    result = lookup.evaluate(_tower(z=0.0), _sample(0, x=2999.0, z=200.0), "radar_i")
    assert result["horizontal_distance_m"] < 3000.0
    assert result["slant_distance_m"] > 3000.0
    assert result["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert evaluator.calls == []


@pytest.mark.parametrize(("origin", "sample_z"), [(None, 100.0), (float("nan"), 100.0), (100.0, float("inf"))])
def test_missing_or_invalid_height_is_unknown_without_fabricated_zero(origin, sample_z):
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"})
    result = lookup.evaluate(_tower(z=origin), _sample(0, x=1000.0, z=sample_z), "radar_i")
    assert result["status"] == "unknown"
    assert result["reason"] == "geometry_inputs_incomplete"
    assert evaluator.calls == []


def test_geometry_elevation_filter_is_separate_from_range():
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"})
    result = lookup.evaluate(_tower(z=100.0), _sample(0, x=1000.0, z=0.0), "radar_i")
    assert result["status"] == LOS_LOOKUP_STATUS_GEOMETRY_FILTERED
    assert result["reason"] == "outside_elevation_interval"
    assert evaluator.calls == []


def test_lookup_cache_and_fingerprint_are_deterministic_and_input_isolated():
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(
        mode="disclosure_only",
        evaluator=evaluator,
        source_identity={"path": "dem.tif", "version": "a"},
        route_id="R-A",
        sampling={"spacing_m": 5.0},
    )
    first = lookup.evaluate(_tower(), _sample(1, x=1000.0, z=110.0), "radar_i")
    second = lookup.evaluate(deepcopy(_tower()), deepcopy(_sample(1, x=1000.0, z=110.0)), "radar_i")
    assert first == second
    assert len(evaluator.calls) == 1
    snapshot = lookup.snapshot()
    assert snapshot["cache_miss_count"] == 1
    assert snapshot["cache_hit_count"] == 1
    assert snapshot["los_ray_count"] == 1
    assert snapshot["terrain_sample_count"] == 3
    assert json.dumps(snapshot, sort_keys=True) == json.dumps(lookup.snapshot(), sort_keys=True)

    changed_source = TerrainLosLookup(
        mode="disclosure_only", evaluator=_Evaluator(),
        source_identity={"path": "dem.tif", "version": "b"}, route_id="R-A",
        sampling={"spacing_m": 5.0},
    )
    changed_height = TerrainLosLookup(
        mode="disclosure_only", evaluator=_Evaluator(),
        source_identity={"path": "dem.tif", "version": "a"}, route_id="R-A",
        sampling={"spacing_m": 5.0},
    )
    changed_source.evaluate(_tower(), _sample(1, x=1000.0, z=110.0), "radar_i")
    changed_height.evaluate(_tower(), _sample(1, x=1000.0, z=111.0), "radar_i")
    assert snapshot["input_fingerprint"] != changed_source.snapshot()["input_fingerprint"]
    assert snapshot["input_fingerprint"] != changed_height.snapshot()["input_fingerprint"]


def test_refinement_rename_of_same_physical_probe_reuses_cached_ray():
    evaluator = _Evaluator(["clear", "blocked"])
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
        route_id="R-TEST",
    )
    original = _sample(7, x=1000.0, z=110.0)
    renamed = {**original, "sample_id": "R000007", "refinement": True}
    first = lookup.evaluate(_tower(), original, "radar_i")
    second = lookup.evaluate(_tower(), renamed, "radar_i")
    assert first["status"] == "clear"
    assert second["status"] == "clear"
    assert len(evaluator.calls) == 1
    assert lookup.snapshot()["cache_hit_count"] == 1


def test_budget_exhaustion_returns_unknown_and_marks_execution_incomplete():
    evaluator = _Evaluator(["clear", "clear"])
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
        max_ray_count=1,
    )
    assert lookup.evaluate(_tower(), _sample(0, x=1000.0), "radar_i")["status"] == "clear"
    exhausted = lookup.evaluate(_tower(), _sample(1, x=1100.0), "radar_i")
    assert exhausted["status"] == "unknown"
    assert exhausted["reason"] == "los_computation_budget_exhausted"
    snapshot = lookup.snapshot()
    assert snapshot["complete"] is False
    assert snapshot["incomplete_reason"] == "los_computation_budget_exhausted"
    assert snapshot["los_ray_count"] == 1
    assert len(evaluator.calls) == 1


def test_unknown_los_marks_execution_incomplete_and_cannot_prove_physical_gap():
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=_Evaluator(["unknown"]),
        source_identity={"id": "missing-dem"},
    )
    result = solve_layout(
        towers=[_tower()], samples=[_sample(0, x=1000.0)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert result["terrain_los_evidence"]["complete"] is False
    assert result["solver"]["status"] == "search_incomplete"
    assert result["solver"]["optimality_proven"] is False
    assert result["solver"]["infeasibility_proven"] is False
    assert result["managed_physical_gap"] is False
    assert result["gap_classification"] == "unknown"


def _normalised(value):
    if isinstance(value, dict):
        return {
            key: _normalised(item)
            for key, item in value.items()
            if key not in {"elapsed_s", "remaining_s", "elapsed_seconds"}
        }
    if isinstance(value, list):
        return [_normalised(item) for item in value]
    return value


def _one_panel():
    return [{
        "panel_id": "P-T1-radar_i-90.000000",
        "tower_id": "T1",
        "radar_type": "radar_i",
        "azimuth_deg": 90.0,
        "covered_sample_indices": [0, 1, 2],
    }]


def test_explicit_disabled_is_deeply_equal_to_legacy_default_and_does_not_call_los():
    towers = [_tower()]
    samples = [_sample(0, x=1000.0)]
    legacy = solve_layout(towers=towers, samples=samples)
    evaluator = _Evaluator(["blocked"])
    lookup = TerrainLosLookup(
        mode="disabled", evaluator=evaluator, source_identity={"id": "must-not-open"},
    )
    explicit = solve_layout(
        towers=towers, samples=samples,
        terrain_los_mode="disabled", terrain_los_lookup=lookup,
    )
    assert _normalised(explicit) == _normalised(legacy)
    assert evaluator.calls == []
    assert "terrain_los_evidence" not in explicit


def test_disclosure_only_collects_evidence_without_changing_candidate_coverage():
    towers = [_tower()]
    samples = [_sample(0, x=1000.0)]
    baseline = build_candidates(towers=towers, samples=samples)
    evaluator = _Evaluator(["blocked"])
    lookup = TerrainLosLookup(
        mode="disclosure_only", evaluator=evaluator, source_identity={"id": "dem-a"},
    )
    disclosed = build_candidates(
        towers=towers, samples=samples,
        terrain_los_mode="disclosure_only", terrain_los_lookup=lookup,
    )
    assert disclosed["panels"] == baseline["panels"]
    assert disclosed["statistics"] == baseline["statistics"]
    assert disclosed["unusable_towers"] == baseline["unusable_towers"]
    assert disclosed["samples"][0]["route_id"] == "R-TEST"
    assert "route_id" not in baseline["samples"][0]
    # LOS is independent of azimuth, but the cache intentionally distinguishes radar_type
    # because Radar-I and Radar-II have different inclusive equipment ranges.
    assert lookup.snapshot()["status_counts"]["blocked"] == 2
    assert len(evaluator.calls) == 2


def test_enforced_coverage_is_subset_for_fixed_panels_and_samples():
    towers = [_tower()]
    samples = [
        _sample(0, x=1000.0),
        _sample(1, x=1100.0),
        _sample(2, x=1200.0),
    ]
    disabled = actual_site_coverage(
        panels=_one_panel(), samples=samples,
        selected_panel_ids=["P-T1-radar_i-90.000000"], tower_records=towers,
    )
    evaluator = _Evaluator(["clear", "blocked", "unknown"])
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
        route_id="R-TEST",
    )
    enforced = actual_site_coverage(
        panels=_one_panel(), samples=samples,
        selected_panel_ids=["P-T1-radar_i-90.000000"], tower_records=towers,
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    disabled_covered = {
        item["sample_id"] for item in disabled if item["actual_distinct_site_count"]
    }
    enforced_covered = {
        item["sample_id"] for item in enforced if item["actual_distinct_site_count"]
    }
    assert enforced_covered == {"S0"}
    assert enforced_covered < disabled_covered
    assert enforced[1]["status"] == "uncovered"
    assert enforced[2]["status"] == "uncovered"


def test_candidate_and_final_coverage_consume_the_same_cached_los_evidence():
    towers = [_tower()]
    samples = [_sample(0, x=1000.0), _sample(1, x=1100.0)]
    evaluator = _Evaluator(["clear", "blocked", "clear", "blocked", "clear"])
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
        route_id="R-TEST",
    )
    candidates = build_candidates(
        towers=towers, samples=samples,
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    panel = next(item for item in candidates["panels"] if item["radar_type"] == "radar_i")
    assert panel["covered_sample_indices"] == [0]
    before = lookup.snapshot()
    final = actual_site_coverage(
        panels=candidates["panels"], samples=samples,
        selected_panel_ids=[panel["panel_id"]], tower_records=candidates["towers"],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    after = lookup.snapshot()
    assert [item["actual_distinct_site_count"] for item in final] == [1, 0]
    assert after["los_ray_count"] == before["los_ray_count"]
    assert after["cache_hit_count"] > before["cache_hit_count"]

    new_probe = _sample(2, x=1200.0)
    actual_site_coverage(
        panels=candidates["panels"], samples=[new_probe],
        selected_panel_ids=[panel["panel_id"]], tower_records=candidates["towers"],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert lookup.snapshot()["los_ray_count"] == before["los_ray_count"] + 1


def test_enforced_incomplete_budget_never_claims_optimal_or_proven_infeasible():
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=_Evaluator(), source_identity={"id": "dem-a"},
        max_ray_count=0,
    )
    result = solve_layout(
        towers=[_tower()], samples=[_sample(0, x=1000.0)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert result["terrain_los_evidence"]["complete"] is False
    assert result["solver"]["status"] == "search_incomplete"
    assert result["solver"]["optimality_proven"] is False
    assert result["solver"]["infeasibility_proven"] is False


def test_full_pipeline_validation_and_refinement_reuse_los_and_never_readd_blocked():
    def evaluator(*, tower, sample, radar_type, geometry):
        return {
            "status": "clear" if geometry["horizontal_distance_m"] < 1050.0 else "blocked",
            "reason": None,
            "terrain_sample_count": 2,
        }

    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
        route_id="R-TEST", max_ray_count=20,
    )
    result = solve_layout(
        towers=[_tower()], samples=[_sample(0, x=1000.0)],
        validation_samples=[_sample(0, x=1100.0)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    evidence = result["terrain_los_evidence"]
    assert evidence["status_counts"]["blocked"] == 2
    assert evidence["los_ray_count"] == 4
    assert evidence["cache_hit_count"] > 0
    assert result["solver"]["status"] == "infeasible"
    assert result["solver"]["infeasibility_proven"] is True
    assert all(
        item["actual_distinct_site_count"] == 0
        for item in (result.get("validation") or {}).get("samples") or []
        if item["metric"] == [1100.0, 0.0]
    )


def test_lookup_fingerprint_is_independent_of_evaluation_order():
    samples = [_sample(0, x=1000.0), _sample(1, x=1100.0)]
    first = TerrainLosLookup(
        mode="disclosure_only", evaluator=_Evaluator(["clear", "blocked"]),
        source_identity={"id": "dem-a"}, route_id="R-TEST",
    )
    second = TerrainLosLookup(
        mode="disclosure_only", evaluator=_Evaluator(["blocked", "clear"]),
        source_identity={"id": "dem-a"}, route_id="R-TEST",
    )
    for sample in samples:
        first.evaluate(_tower(), sample, "radar_i")
    for sample in reversed(samples):
        second.evaluate(_tower(), sample, "radar_i")
    assert first.snapshot()["input_fingerprint"] == second.snapshot()["input_fingerprint"]


def test_lookup_fingerprint_isolates_route_origin_radar_type_and_mode():
    def fingerprint(*, route="R-A", origin=100.0, radar_type="radar_i", mode="enforced"):
        lookup = TerrainLosLookup(
            mode=mode, evaluator=_Evaluator(), source_identity={"id": "dem-a"},
            route_id=route,
        )
        lookup.evaluate(_tower(z=origin), _sample(0, x=1000.0, z=110.0), radar_type)
        return lookup.snapshot()["input_fingerprint"]

    baseline = fingerprint()
    assert baseline != fingerprint(route="R-B")
    assert baseline != fingerprint(origin=101.0)
    assert baseline != fingerprint(radar_type="radar_ii")
    assert baseline != fingerprint(mode="disclosure_only")


def test_budget_exhausted_by_validation_probe_is_search_incomplete():
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=_Evaluator(["clear", "clear", "clear"]),
        source_identity={"id": "dem-a"}, route_id="R-TEST", max_ray_count=2,
    )
    result = solve_layout(
        towers=[_tower()], samples=[_sample(0, x=1000.0)],
        validation_samples=[_sample(0, x=1100.0)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert result["terrain_los_evidence"]["complete"] is False
    assert result["solver"]["status"] == "search_incomplete"
    assert result["solver"]["optimality_proven"] is False
    assert result["solver"]["infeasibility_proven"] is False


def test_validation_budget_exhaustion_is_guarded_when_total_time_stops_refinement(monkeypatch):
    ticks = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(radar_layout_v1.time, "monotonic", lambda: next(ticks))
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=_Evaluator(["clear", "clear"]),
        source_identity={"id": "dem-a"}, route_id="R-TEST", max_ray_count=2,
    )
    result = solve_layout(
        towers=[_tower()], samples=[_sample(0, x=1000.0)],
        validation_samples=[_sample(1, x=1100.0)], options={"time_limit_s": 1.0},
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert result["time_budget"]["exhausted"] is True
    assert result["terrain_los_evidence"]["complete"] is False
    assert result["solver"]["status"] == "search_incomplete"
    assert result["solver"]["optimality_proven"] is False
    assert result["solver"]["infeasibility_proven"] is False


def test_origin_availability_is_preserved_and_part_of_cache_identity(monkeypatch):
    calls = []

    def evaluate(tower, probe, **kwargs):
        calls.append(kwargs["origin_availability"])
        return {
            "status": "clear" if kwargs["origin_availability"] == "usable" else "unknown",
            "reason": None,
            "terrain_sample_count": 0,
        }

    monkeypatch.setattr(radar_terrain_los_adapter, "evaluate_probe_terrain_los", evaluate)
    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced",
        source_factory=lambda path, variant: _TerrainSource(),
    )
    unavailable = {**_tower(), "origin_availability": "not_evaluable"}
    result = solve_layout(
        towers=[unavailable], samples=[_sample(0, x=1000.0)],
        terrain_los_mode="enforced", terrain_los_lookup=context.lookup,
    )
    assert calls == ["not_evaluable", "not_evaluable"]
    assert result["solver"]["status"] == "search_incomplete"
    assert result["managed_physical_gap"] is False

    direct = TerrainLosLookup(mode="enforced", evaluator=_Evaluator(["clear", "unknown"]))
    direct.evaluate(_tower(), _sample(0, x=1000.0), "radar_i")
    direct.evaluate(unavailable, _sample(0, x=1000.0), "radar_i")
    assert direct.snapshot()["cache_miss_count"] == 2
    assert direct.snapshot()["los_ray_count"] == 2
    context.close()


def test_device_origin_identity_survives_normalization_and_isolates_cache():
    base = {
        **_tower(),
        "device_instance_id": "RADAR-T1-A",
        "origin_input_fingerprint": "origin-a",
    }
    lookup = TerrainLosLookup(mode="enforced", evaluator=_Evaluator(["clear"]))
    built = build_candidates(
        towers=[base], samples=[_sample(0, x=1000.0)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert built["towers"][0]["device_instance_id"] == "RADAR-T1-A"
    assert built["towers"][0]["origin_input_fingerprint"] == "origin-a"

    changed = {
        **base,
        "device_instance_id": "RADAR-T1-B",
        "origin_input_fingerprint": "origin-b",
    }
    lookup.evaluate(changed, _sample(0, x=1000.0), "radar_i")
    assert lookup.snapshot()["los_ray_count"] == 3  # initial I/II plus changed-origin I.


def test_route_and_layer_identity_survive_normalization_and_reuse_validation_ray():
    evaluator = _Evaluator(["clear"])
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, source_identity={"id": "dem-a"},
    )
    sample = {**_sample(0, x=1000.0, route_id="R-A"), "altitude_layer_id": "ALT-100"}
    result = solve_layout(
        towers=[_tower()], samples=[sample], validation_samples=[deepcopy(sample)],
        terrain_los_mode="enforced", terrain_los_lookup=lookup,
    )
    assert result["solver"]["status"] == "optimal"
    assert lookup.snapshot()["los_ray_count"] == 2, evaluator.calls  # Radar-I + Radar-II.
    assert lookup.snapshot()["cache_hit_count"] > 0

    other_route = {**sample, "route_id": "R-B"}
    lookup.evaluate(_tower(), other_route, "radar_i")
    assert lookup.snapshot()["los_ray_count"] == 3


class _Dataset:
    def __init__(self):
        self.flush_count = 0

    def FlushCache(self):
        self.flush_count += 1


class _TerrainSource:
    def __init__(self):
        self.dataset = _Dataset()
        self.vertical_reference = "egm2008_orthometric"


def test_gis_execution_context_is_lazy_reuses_dem_and_closes(monkeypatch):
    opens = []
    sources = []
    datasets = []
    evaluations = []

    def factory(path, variant):
        opens.append((path, variant))
        source = _TerrainSource()
        sources.append(source)
        datasets.append(source.dataset)
        return source

    def evaluate(tower, probe, **kwargs):
        evaluations.append((tower["tower_id"], probe["sample_id"]))
        return {
            "status": "clear",
            "reason": None,
            "terrain_profile": {"sample_count": 7},
        }

    monkeypatch.setattr(radar_terrain_los_adapter, "evaluate_probe_terrain_los", evaluate)
    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced", route_id="R-TEST",
        source_factory=factory, max_ray_count=10,
    )
    assert context.snapshot()["dem_open_count"] == 0
    assert context.lookup.evaluate(_tower(), _sample(0, x=1000.0), "radar_i")["status"] == "clear"
    assert context.lookup.evaluate(_tower(), _sample(1, x=1100.0), "radar_i")["status"] == "clear"
    assert opens == [("X:/dem.tif", radar_terrain_los_adapter.SOURCE_VARIANT_NATIVE)]
    assert len(evaluations) == 2
    assert context.snapshot()["dem_open_count"] == 1
    context.close()
    assert datasets[0].flush_count == 1


def test_far_range_three_towers_eighteen_probes_opens_no_dem_and_casts_no_ray():
    opens = []

    def factory(path, variant):
        opens.append((path, variant))
        return _TerrainSource()

    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced", route_id="R-FAR",
        source_factory=factory, max_ray_count=100,
    )
    towers = [_tower(tower_id=f"T{i}", x=float(i * 100)) for i in range(3)]
    probes = [_sample(i, x=109000.0 + i * 10.0, route_id="R-FAR") for i in range(18)]
    results = [
        context.lookup.evaluate(tower, sample, "radar_i")
        for tower in towers for sample in probes
    ]
    assert len(results) == 54
    assert {item["status"] for item in results} == {"out_of_equipment_range"}
    snapshot = context.snapshot()
    assert snapshot["status_counts"]["out_of_equipment_range"] == 54
    assert snapshot["los_ray_count"] == 0
    assert snapshot["terrain_sample_count"] == 0
    assert snapshot["dem_open_count"] == 0
    assert opens == []
    context.close()


def test_gis_context_closes_open_source_after_cancellation(monkeypatch):
    source = _TerrainSource()
    dataset = source.dataset
    cancel = {"raise": False}

    def check():
        if cancel["raise"]:
            raise RuntimeError("cancelled")

    monkeypatch.setattr(
        radar_terrain_los_adapter,
        "evaluate_probe_terrain_los",
        lambda *args, **kwargs: {"status": "clear", "terrain_sample_count": 1},
    )
    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced", route_id="R-TEST",
        source_factory=lambda path, variant: source, cancel_check=check,
    )
    try:
        context.lookup.evaluate(_tower(), _sample(0, x=1000.0), "radar_i")
        cancel["raise"] = True
        with pytest.raises(RuntimeError, match="cancelled"):
            context.lookup.evaluate(_tower(), _sample(1, x=1100.0), "radar_i")
    finally:
        context.close()
    assert dataset.flush_count == 1


class _Session:
    def __init__(self):
        self.state = {}

    def save(self):
        raise AssertionError("production mode gate must run before any save")


def _service():
    return RadarSurveillanceLayoutService(
        _Session(), invalidation=object(), snapshot=lambda: {},
    )


#: Round34-T07-R 集成适配：正式仓库的 Radar 划设生产入口是
#: :meth:`RadarSurveillanceLayoutService.evaluate`（原工作副本的后台任务 ``plan``
#: 入口尚未进入正式仓库）。门禁语义与断言完全不变：非 ``disabled`` 的
#: ``terrain_los_mode`` 必须在任何 ``save`` 之前被拒绝。
@pytest.mark.parametrize("mode", ["enforced", "disclosure_only"])
def test_production_layout_plan_rejects_non_disabled_modes(mode):
    with pytest.raises(ValueError, match="生产 Radar"):
        _service().evaluate({"terrain_los_mode": mode})


def test_production_layout_plan_rejects_illegal_mode_without_fallback():
    with pytest.raises(ValueError, match="terrain_los_mode"):
        _service().evaluate({"terrain_los_mode": "maybe"})
