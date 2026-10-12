"""Round34-T07-D2-A — bounded streaming Terrain LOS capacity scheduling."""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.algorithms.radar_layout.terrain_los_lookup import TerrainLosLookup
from cns_planner.application.radar_surveillance_layout_service import (
    RadarSurveillanceLayoutService,
)
from cns_planner.gis import radar_terrain_los_adapter
from tools.round34_t07d2a_los_capacity import (
    _load_selected_top_level,
    _stratified,
)


def _tower(tower_id="T1", *, x=0.0, z=100.0, origin_state="usable_estimated"):
    return {
        "tower_id": tower_id,
        "metric": [float(x), 0.0],
        "origin_egm2008_m": z,
        "origin_source": "fixture",
        "origin_state": origin_state,
        "origin_evidence_level": "estimated",
        "origin_availability": "usable",
    }


def _sample(index, *, x, z=100.0, route_id="R-D2A"):
    return {
        "sample_id": f"P{index}",
        "sample_index": index,
        "route_id": route_id,
        "altitude_layer_id": "ALT-100",
        "metric": [float(x), 0.0],
        "egm2008_m": z,
    }


def _combination(index, *, x, tower=None, sample=None):
    return {
        "tower": tower or _tower(),
        "sample": sample or _sample(index, x=x),
        "radar_type": "radar_i",
    }


class _Evaluator:
    def __init__(self):
        self.calls = []

    def __call__(self, *, tower, sample, radar_type, geometry):
        self.calls.append((tower["tower_id"], sample["sample_id"], radar_type))
        distance = geometry["horizontal_distance_m"]
        status = "clear" if distance <= 1000.0 else "blocked"
        return {
            "status": status,
            "reason": None if status == "clear" else "fixture_obstruction",
            "terrain_sample_count": 4,
        }


def _run(combinations, *, batch_size, max_ray_count=20, **kwargs):
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, route_id="R-D2A",
        source_identity={"path": "fixture-dem", "variant": "native"},
        max_ray_count=max_ray_count,
    )
    report = lookup.evaluate_batches(
        combinations, batch_size=batch_size, retain_records=True, **kwargs,
    )
    return report, lookup, evaluator


def test_batched_and_single_batch_complete_fixture_are_equivalent():
    combinations = [_combination(i, x=x) for i, x in enumerate((500, 900, 1100, 2500))]
    single, _, _ = _run(deepcopy(combinations), batch_size=99)
    batched, _, _ = _run(deepcopy(combinations), batch_size=2)

    stable_keys = (
        "total_combinations", "geometry_filtered_combinations",
        "geometry_unknown_combinations", "los_eligible_combinations",
        "executed_rays", "los_status_counts", "not_sampled_count",
        "computation_complete", "evidence_complete",
    )
    assert {key: single[key] for key in stable_keys} == {
        key: batched[key] for key in stable_keys
    }
    assert sorted((item["cache_key"], item["status"]) for item in single["records"]) == sorted(
        (item["cache_key"], item["status"]) for item in batched["records"]
    )


def test_reordered_batches_preserve_complete_set_and_classification():
    combinations = [_combination(i, x=x) for i, x in enumerate((500, 900, 1100, 2500))]
    forward, _, _ = _run(deepcopy(combinations), batch_size=2)
    reverse, _, _ = _run(list(reversed(deepcopy(combinations))), batch_size=3)

    assert forward["los_status_counts"] == reverse["los_status_counts"]
    assert {item["cache_key"] for item in forward["records"]} == {
        item["cache_key"] for item in reverse["records"]
    }


def test_duplicate_physical_ray_is_sampled_once_across_batch_boundaries():
    first = _combination(0, x=800.0)
    renamed = deepcopy(first)
    renamed["sample"]["sample_id"] = "RENAMED-PROBE"
    report, lookup, evaluator = _run([first, renamed], batch_size=1)

    assert report["total_combinations"] == 2
    assert report["executed_rays"] == 1
    assert report["executed_los_status_counts"] == {
        "clear": 1, "blocked": 0, "unknown": 0,
    }
    assert report["los_status_counts"] == {"clear": 2, "blocked": 0, "unknown": 0}
    assert report["cache_hit_count"] == 1
    assert len(evaluator.calls) == 1
    assert lookup.snapshot()["combination_count"] == 1


def test_geometry_gate_keeps_slant_range_and_unknown_out_of_dem_and_cache():
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, route_id="R-D2A", max_ray_count=10,
    )
    combinations = [
        _combination(0, x=2999.0, tower=_tower(z=0.0), sample=_sample(0, x=2999.0, z=200.0)),
        _combination(1, x=500.0, tower=_tower(z=None)),
        _combination(2, x=800.0),
    ]
    report = lookup.evaluate_batches(combinations, batch_size=2, retain_records=True)

    assert report["geometry_filtered_combinations"] == 1
    assert report["geometry_unknown_combinations"] == 1
    assert report["los_eligible_combinations"] == 1
    assert report["executed_rays"] == 1
    assert report["classification_counts"]["out_of_equipment_range"] == 1
    assert report["classification_counts"]["geometry_unknown"] == 1
    assert evaluator.calls == [("T1", "P2", "radar_i")]
    assert lookup.snapshot()["combination_count"] == 1


def test_ray_budget_discloses_not_sampled_separately_from_physical_unknown():
    calls = []

    def evaluator(*, tower, sample, radar_type, geometry):
        calls.append(sample["sample_id"])
        return {
            "status": "unknown" if sample["sample_id"] == "P0" else "clear",
            "reason": "terrain_nodata" if sample["sample_id"] == "P0" else None,
            "terrain_sample_count": 2,
        }

    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, route_id="R-D2A", max_ray_count=2,
    )
    report = lookup.evaluate_batches(
        [_combination(i, x=500.0 + 100.0 * i) for i in range(4)],
        batch_size=2, retain_records=True,
    )

    assert report["executed_rays"] == 2
    assert report["los_status_counts"] == {"clear": 1, "blocked": 0, "unknown": 1}
    assert report["not_sampled_count"] == 2
    assert report["classification_counts"]["physical_unknown"] == 1
    assert report["classification_counts"]["not_sampled"] == 2
    assert report["computation_complete"] is False
    assert report["completion_status"] == "search_incomplete"
    assert report["budget_exhausted"] is True
    assert report["budget_exhausted_reasons"] == ["ray_budget_exhausted"]
    assert {item["status"] for item in report["records"]} == {"unknown", "clear", "not_sampled"}
    assert calls == ["P0", "P1"]
    assert lookup.snapshot()["complete"] is False
    assert lookup.snapshot()["incomplete_reason"] == "los_computation_budget_exhausted"


def test_elapsed_budget_stops_new_rays_but_finishes_geometry_accounting():
    now = [0.0]

    def evaluator(**kwargs):
        del kwargs
        now[0] += 0.6
        return {"status": "clear", "terrain_sample_count": 1}

    lookup = TerrainLosLookup(
        mode="enforced", evaluator=evaluator, route_id="R-D2A", max_ray_count=10,
    )
    report = lookup.evaluate_batches(
        [_combination(i, x=500.0 + 100.0 * i) for i in range(4)],
        batch_size=3, max_elapsed_seconds=1.0, clock=lambda: now[0],
    )

    assert report["total_combinations"] == 4
    assert report["los_eligible_combinations"] == 4
    assert report["executed_rays"] == 2
    assert report["not_sampled_count"] == 2
    assert report["budget_exhausted_reasons"] == ["elapsed_time_budget_exhausted"]
    assert report["elapsed_seconds"] == pytest.approx(1.2)
    assert report["elapsed_budget_overshoot_seconds"] == pytest.approx(0.2)
    assert report["elapsed_budget_checkpoint"] == (
        "batch_and_ray_boundaries_in_flight_evaluator_not_preempted"
    )
    assert lookup.snapshot()["complete"] is False
    assert lookup.snapshot()["incomplete_reason"] == "los_elapsed_time_budget_exhausted"


def test_disabled_batch_parity_never_opens_evaluator_and_is_complete():
    evaluator = _Evaluator()
    lookup = TerrainLosLookup(mode="disabled", evaluator=evaluator, route_id="R-D2A")
    report = lookup.evaluate_batches(
        [_combination(0, x=800.0), _combination(1, x=4000.0)], batch_size=1,
    )

    assert evaluator.calls == []
    assert report["executed_rays"] == 0
    assert report["classification_counts"]["disabled"] == 1
    assert report["classification_counts"]["out_of_equipment_range"] == 1
    assert report["computation_complete"] is True


class _Dataset:
    def __init__(self):
        self.flush_count = 0

    def FlushCache(self):
        self.flush_count += 1


class _TerrainSource:
    def __init__(self):
        self.dataset = _Dataset()
        self.vertical_reference = "egm2008_orthometric"


def test_batch_boundary_cancellation_releases_single_open_dem(monkeypatch):
    source = _TerrainSource()
    dataset = source.dataset
    boundary_checks = [0]

    def cancel_check():
        boundary_checks[0] += 1
        if boundary_checks[0] == 4:  # batch 1 boundary + two per-ray checks + batch 2 boundary
            raise RuntimeError("cancelled at batch boundary")

    monkeypatch.setattr(
        radar_terrain_los_adapter, "evaluate_probe_terrain_los",
        lambda *args, **kwargs: {"status": "clear", "terrain_sample_count": 1},
    )
    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced", route_id="R-D2A",
        source_factory=lambda path, variant: source, cancel_check=cancel_check,
        max_ray_count=10,
    )
    with pytest.raises(RuntimeError, match="batch boundary"):
        with context:
            context.lookup.evaluate_batches(
                [_combination(i, x=500.0 + i * 100.0) for i in range(4)],
                batch_size=2,
            )

    assert dataset.flush_count == 1
    assert context.snapshot()["dem_open_count"] == 1


def test_batch_evaluator_exception_releases_open_dem(monkeypatch):
    source = _TerrainSource()
    dataset = source.dataset

    def fail_after_open(*args, **kwargs):
        del args, kwargs
        raise OSError("fixture native read failed")

    monkeypatch.setattr(
        radar_terrain_los_adapter, "evaluate_probe_terrain_los", fail_after_open,
    )
    context = radar_terrain_los_adapter.TerrainLosExecutionContext(
        terrain_path="X:/dem.tif", mode="enforced", route_id="R-D2A",
        source_factory=lambda path, variant: source,
    )
    with pytest.raises(OSError, match="native read failed"):
        with context:
            context.lookup.evaluate_batches(
                [_combination(0, x=800.0)], batch_size=1,
            )
    assert dataset.flush_count == 1


class _Session:
    def __init__(self):
        self.state = {}

    def save(self):
        raise AssertionError("production gate must reject before save")


@pytest.mark.parametrize("mode", ["enforced", "disclosure_only"])
def test_production_api_still_rejects_non_disabled_modes(mode):
    #: Round34-T07-R 集成适配：正式仓库的生产入口是 ``evaluate``（原工作副本的
    #: 后台任务 ``plan`` 入口尚未进入正式仓库）；门禁与断言语义不变。
    service = RadarSurveillanceLayoutService(
        _Session(), invalidation=object(), snapshot=lambda: {},
    )
    with pytest.raises(ValueError, match="生产 Radar"):
        service.evaluate({"terrain_los_mode": mode})


def test_capacity_tool_streams_only_selected_top_level_values(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"revision":442,"huge":[1,2,3],"towers":{"count":373},'
        '"operational_routes":[{"route_id":"R0005"}]}',
        encoding="utf-8",
    )
    selected = _load_selected_top_level(
        state_path, {"revision", "towers", "operational_routes"},
    )
    assert selected == {
        "revision": 442,
        "towers": {"count": 373},
        "operational_routes": [{"route_id": "R0005"}],
    }


def test_stratified_selection_is_deterministic_and_spans_endpoints():
    values = list(range(20))
    assert _stratified(values, 5) == [0, 5, 10, 14, 19]
    assert _stratified(values, 99) == values
