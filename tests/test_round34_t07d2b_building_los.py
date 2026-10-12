"""Round34-T07-D2-B building LOS and combined visibility contracts."""

from __future__ import annotations

import pytest

from cns_planner.domain import radar_building_los as building_los
from cns_planner.domain import radar_visibility_evidence as visibility
from cns_planner.domain.radar_surveillance_layout import (
    radar_geometry_parameters as geometry_parameters,
)
from cns_planner.gis import radar_building_los_adapter as building_adapter


def polygon(x0=4.0, y0=-1.0, x1=6.0, y1=1.0, holes=None):
    coordinates = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
    coordinates.extend(holes or [])
    return {"type": "Polygon", "coordinates": coordinates}


def building(*, geometry=None, level="verified", height=20.0, ground=0.0, bid="B1",
             derived_assumption=None):
    record = {
        "building_id": bid,
        "geometry_metric": geometry or polygon(),
        "height_estimate_m": height,
        "height_evidence_level": level,
        "ground_elevation_egm2008_m": ground,
        "source_version": "buildings-v3-fixture",
    }
    if derived_assumption is not None:
        record["derived_assumption"] = derived_assumption
    return record


def evaluate(items=(), **overrides):
    values = {
        "origin_metric": [0.0, 0.0],
        "target_metric": [10.0, 0.0],
        "origin_egm2008_m": 10.0,
        "target_egm2008_m": 10.0,
        "buildings": list(items),
        "query_complete": True,
        "building_source_identity": {"file_name": "buildings-v3.gpkg", "mtime_ns": 1},
    }
    values.update(overrides)
    return building_los.evaluate_building_los(**values)


def test_no_building_and_complete_query_is_clear():
    result = evaluate()
    assert result["status"] == "clear"
    assert result["computation_completion"] == "complete"
    assert result["candidate_building_count"] == 0


def test_trusted_building_prism_crossing_is_blocked_with_hit_geometry():
    result = evaluate([building()])
    assert result["status"] == "blocked"
    assert result["engineering_estimate_status"] == "blocked"
    hit = result["obstruction_hits"][0]
    assert hit["building_id"] == "B1"
    assert hit["footprint_part_index"] == 0
    assert hit["ray_fraction"] == pytest.approx(0.4)
    assert hit["hit_x"] == pytest.approx(4.0)
    assert hit["hit_z"] == pytest.approx(10.0)
    assert hit["evidence_classification"] == "confirmed"


def test_bbox_candidate_that_does_not_touch_ray_is_clear():
    result = evaluate([building(geometry=polygon(4, 2, 6, 3))])
    assert result["status"] == "clear"
    assert result["exact_intersection_count"] == 0


def test_ray_above_roof_is_clear():
    result = evaluate([building(height=5.0)], origin_egm2008_m=20.0, target_egm2008_m=20.0)
    assert result["status"] == "clear"


def test_polygon_hole_does_not_create_a_false_block():
    hole = [[3, -1], [7, -1], [7, 1], [3, 1], [3, -1]]
    result = evaluate(
        [building(geometry=polygon(2, -2, 8, 2, holes=[hole]))],
        origin_metric=[4.0, 0.0], target_metric=[6.0, 0.0],
    )
    assert result["status"] == "clear"
    assert result["exact_intersection_count"] == 0


def test_multipolygon_preserves_part_index():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [polygon(1, 3, 2, 4)["coordinates"], polygon(7, -1, 8, 1)["coordinates"]],
    }
    result = evaluate([building(geometry=geometry)])
    assert result["status"] == "blocked"
    assert result["obstruction_hits"][0]["footprint_part_index"] == 1


def test_invalid_bowtie_is_repaired_in_memory():
    bowtie = {"type": "Polygon", "coordinates": [[[4, -1], [6, 1], [4, 1], [6, -1], [4, -1]]]}
    result = evaluate([building(geometry=bowtie)])
    assert result["geometry_quality_counts"]["repaired"] == 1
    assert result["source_geometry_modified"] is False


def test_tangent_point_is_unknown_not_false_block_or_clear():
    result = evaluate([building(geometry=polygon(4, 0, 6, 2))])
    assert result["status"] == "unknown"
    assert result["reason"] == "building_boundary_contact_unresolved"


def test_rooftop_origin_contact_only_does_not_ignore_later_interior():
    footprint = polygon(-1, -1, 6, 1)
    rising = evaluate(
        [building(geometry=footprint, height=10.0)],
        origin_egm2008_m=10.0,
        target_egm2008_m=30.0,
    )
    # The ray only touches the roof plane at the origin and leaves it upwards.
    assert rising["status"] == "clear"
    through = evaluate(
        [building(geometry=footprint, height=20.0)],
        origin_egm2008_m=10.0,
        target_egm2008_m=10.0,
    )
    assert through["status"] == "blocked"
    # The origin already sits between this prism's ground and roof, so the first entry
    # along origin -> probe is the origin itself, not an arbitrary interior point.
    hit = through["obstruction_hits"][0]
    assert hit["ray_fraction"] == pytest.approx(0.0)
    assert hit["hit_x"] == pytest.approx(0.0)
    assert hit["hit_z"] == pytest.approx(10.0)
    assert hit["hit_derivation_status"] == "exact_xy_polygon_interval_first_entry_bounded_root"


def test_same_xy_origin_target_is_unknown():
    result = evaluate(target_metric=[0.0, 0.0])
    assert result["status"] == "unknown"
    assert result["reason"] == "ray_horizontal_geometry_unresolved"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"height": None}, "building_height_missing"),
        ({"ground": None}, "building_footprint_ground_elevation_unresolved"),
    ],
)
def test_missing_prism_input_is_unknown(changes, reason):
    result = evaluate([building(**changes)])
    assert result["status"] == "unknown"
    assert reason in result["unknown_reasons"]


@pytest.mark.parametrize("level", ["predicted", "unknown"])
def test_untrusted_height_never_promotes_engineering_block_to_physical_block(level):
    result = evaluate([building(level=level)])
    assert result["engineering_estimate_status"] == "blocked"
    assert result["status"] == "unknown"
    assert result["obstruction_hits"][0]["evidence_classification"] in {"estimated", "unknown"}


def test_derived_height_requires_its_disclosed_assumption_to_support_blocked():
    missing = evaluate([building(level="derived")])
    backed = evaluate([building(level="derived", derived_assumption="levels_times_3m")])
    assert missing["status"] == "unknown"
    assert missing["engineering_estimate_status"] == "blocked"
    assert backed["status"] == "blocked"


def test_truncated_query_cannot_return_clear():
    result = evaluate([], query_complete=False, query_reason="max_features_reached")
    assert result["status"] == "unknown"
    assert result["computation_completion"] == "incomplete"
    assert result["reason"] == "building_query_incomplete"


def test_curvature_uses_shared_terrain_ray_height_model():
    result = evaluate(
        [building(geometry=polygon(4900, -2, 5100, 2), height=9.5)],
        target_metric=[10000.0, 0.0],
        earth_radius_m=6_371_008.8,
        k_factor=1.0,
    )
    assert result["status"] == "blocked"
    assert result["ray_model_id"] == "radar_los_shared_ray_height_v1"


def test_building_source_change_changes_fingerprint():
    first = evaluate([building()])
    second = evaluate(
        [building()], building_source_identity={"file_name": "buildings-v3.gpkg", "mtime_ns": 2}
    )
    assert first["input_fingerprint"] != second["input_fingerprint"]


class FakeBuildingSource:
    def __init__(self, buildings=(), *, complete=True, identity=None):
        self.buildings = list(buildings)
        self.complete = complete
        self.identity = identity or {"file_name": "buildings-v3.gpkg", "mtime_ns": 1}
        self.queries = []

    def describe(self):
        return dict(self.identity)

    def query_ray_bbox(self, metric_bbox, *, transform, terrain_source, max_features):
        self.queries.append((list(metric_bbox), max_features))
        return {
            "buildings": list(self.buildings),
            "query_complete": self.complete,
            "query_reason": None if self.complete else "max_features_reached",
            "query_receipt": {"mode": "provider_rtree_ray_bbox", "candidate_count": len(self.buildings)},
        }


def tower(x=0.0, y=0.0, z=10.0):
    return {"tower_id": "T1", "metric": [x, y], "origin_egm2008_m": z}


def probe(x=10.0, y=0.0, z=10.0):
    return {"probe_id": "P1", "metric": [x, y], "altitude_egm2008_m": z}


def test_adapter_frozen_range_gate_skips_building_query():
    # 10 m horizontal / same height is inside neither device range; Radar-I is the
    # narrowest gate and must reject it before any building query happens.
    source = FakeBuildingSource([building()])
    result = building_adapter.evaluate_probe_building_los(
        tower(), probe(), radar_type="radar_i", building_source=source, transform=None,
        terrain_source=None,
    )
    assert result["status"] == building_los.GATE_OUTSIDE_STATUS
    assert result["reason"] == "outside_frozen_device_range"
    assert result["within_frozen_device_range"] is False
    assert result["geometry_gate"]["device_geometry"]["radar_type"] == "radar_i"
    assert source.queries == []


def in_range_geometry(*, radar_type="radar_i"):
    return float(geometry_parameters(radar_type)["min_slant_range_m"])


def test_adapter_queries_the_whole_ray_bbox_not_route_bbox():
    source = FakeBuildingSource()
    distance = geometry_parameters("radar_i")["min_slant_range_m"]
    result = building_adapter.evaluate_probe_building_los(
        tower(x=5, y=2), probe(x=5.0 + distance, y=2.0), radar_type="radar_i",
        building_source=source, transform=None, terrain_source=None,
    )
    assert result["status"] == "clear"
    assert result["within_frozen_device_range"] is True
    assert source.queries == [
        ([5.0, 2.0, 5.0 + distance, 2.0], building_adapter.DEFAULT_MAX_FEATURES_PER_RAY)
    ]


def test_adapter_propagates_query_truncation_as_incomplete_unknown():
    source = FakeBuildingSource(complete=False)
    result = building_adapter.evaluate_probe_building_los(
        tower(), probe(x=in_range_geometry()), radar_type="radar_i",
        building_source=source, transform=None, terrain_source=None,
    )
    assert result["status"] == "unknown"
    assert result["computation_completion"] == "incomplete"


def test_execution_context_has_independent_ray_and_query_budgets():
    source = FakeBuildingSource()
    context = building_adapter.BuildingLosExecutionContext(
        building_source=source, terrain_source=None, transform=None,
        max_rays=1, max_queries=1, max_features_per_ray=5,
    )
    distance = geometry_parameters("radar_i")["min_slant_range_m"]
    first = context.evaluate(tower(), probe(x=distance), radar_type="radar_i")
    second = context.evaluate(tower(), probe(x=distance + 10.0), radar_type="radar_i")
    assert first["status"] == "clear"
    assert second["status"] == "unknown"
    assert second["reason"] == "building_los_budget_exhausted"
    assert context.receipt()["ray_count"] == 1
    assert context.receipt()["query_count"] == 1
    assert context.receipt()["budget_exhausted"] is True


def test_execution_context_time_budget_exhaustion_is_explicit():
    context = building_adapter.BuildingLosExecutionContext(
        building_source=FakeBuildingSource(), terrain_source=None, transform=None,
        max_rays=1, max_queries=1, max_elapsed_seconds=0.0,
    )
    result = context.evaluate(
        tower(), probe(x=in_range_geometry()), radar_type="radar_i",
    )
    assert result["status"] == "unknown"
    assert result["reason"] == "building_los_budget_exhausted"
    assert context.receipt()["max_elapsed_seconds"] == 0.0
    assert context.receipt()["computation_completion"] == "incomplete"


def test_building_fact_cache_isolated_by_building_dem_and_height_source_identity():
    cache = building_adapter.BuildingFactCache()
    base = {"building_source": {"mtime_ns": 1}, "building_id": "B", "height_source": "v3",
            "dem_source": {"mtime_ns": 10}}
    cache.put(base, {"value": 1})
    assert cache.get(base) == {"value": 1}
    for changed in (
        {**base, "building_source": {"mtime_ns": 2}},
        {**base, "height_source": "v4"},
        {**base, "dem_source": {"mtime_ns": 11}},
    ):
        assert cache.get(changed) is None


def test_batch_context_matches_single_evaluation_for_fixture():
    distance = geometry_parameters("radar_i")["min_slant_range_m"]
    source_single = FakeBuildingSource([building(geometry=polygon_wide())])
    expected = building_adapter.evaluate_probe_building_los(
        tower(), probe(x=distance), radar_type="radar_i", building_source=source_single,
        transform=None, terrain_source=None,
    )
    source_batch = FakeBuildingSource([building(geometry=polygon_wide())])
    context = building_adapter.BuildingLosExecutionContext(
        building_source=source_batch, terrain_source=None, transform=None,
        max_rays=4, max_queries=4,
    )
    actual = context.evaluate(tower(), probe(x=distance), radar_type="radar_i")
    assert actual["status"] == expected["status"]
    assert actual["obstruction_hits"] == expected["obstruction_hits"]


def polygon_wide():
    return polygon(0.0 - 1.0, -1.0, 3000.0, 1.0)


def test_adapter_fingerprint_is_stable_when_only_query_timing_changes():
    import time

    distance = geometry_parameters("radar_i")["min_slant_range_m"]
    source = FakeBuildingSource([building(geometry=polygon_wide())])
    first = building_adapter.evaluate_probe_building_los(
        tower(), probe(x=distance), radar_type="radar_i", building_source=source,
        transform=None, terrain_source=None,
    )
    # Advance the wall clock so the two runs really do carry different receipt timing
    # instead of relying on the resolution of two back-to-back perf_counter calls.
    time.sleep(0.002)
    second = building_adapter.evaluate_probe_building_los(
        tower(), probe(x=distance), radar_type="radar_i", building_source=source,
        transform=None, terrain_source=None,
    )
    assert first["query_receipt"]["elapsed_seconds"] != second["query_receipt"]["elapsed_seconds"]
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert (
        first["execution_identity"]["run_receipt"].get("elapsed_seconds") is not None
    )


@pytest.mark.parametrize(
    ("terrain", "building_status", "expected"),
    [
        ("clear", "clear", "clear"),
        ("blocked", "clear", "blocked"),
        ("clear", "blocked", "blocked"),
        ("blocked", "blocked", "blocked"),
        ("clear", "unknown", "unknown"),
        ("unknown", "clear", "unknown"),
        ("unknown", "blocked", "blocked"),
        ("clear", "not_evaluated", "not_evaluated"),
    ],
)
def test_combined_visibility_table(terrain, building_status, expected):
    result = visibility.combine_visibility(
        {"status": terrain, "computation_completion": "complete"},
        {"status": building_status, "computation_completion": "complete"},
    )
    assert result["combined_visibility"] == expected
    assert result["terrain_los"]["status"] == terrain
    assert result["building_los"]["status"] == building_status


def test_predicted_building_block_stays_unknown_in_combined_main_verdict():
    estimated = evaluate([building(level="predicted")])
    result = visibility.combine_visibility({"status": "clear"}, estimated)
    assert estimated["engineering_estimate_status"] == "blocked"
    assert result["combined_visibility"] == "unknown"


def test_confirmed_block_can_decide_visibility_while_other_domain_stays_incomplete():
    result = visibility.combine_visibility(
        {"status": "blocked", "computation_completion": "complete"}, None,
    )
    assert result["combined_visibility"] == "blocked"
    assert result["building_los"]["status"] == "not_evaluated"
    assert result["computation_completion"] == "incomplete"


def test_ray_geometry_3d_is_renderer_neutral_and_keeps_vertical_reference():
    ray = visibility.ray_geometry_3d(
        tower_id="T1", radar_device_id="R-I", probe_id="P1",
        origin_metric=[1, 2], origin_egm2008_m=30,
        target_metric=[4, 6], target_egm2008_m=33,
        horizontal_crs="EPSG:32651", terrain_status="clear",
        building_status="blocked", combined_status="blocked",
        input_fingerprint="abc",
    )
    assert ray["slant_distance_m"] == pytest.approx((5 ** 2 + 3 ** 2) ** 0.5)
    assert ray["vertical_crs"] == "egm2008_orthometric"
    assert ray["ray_model_id"] == building_los.RAY_MODEL_ID


def test_terrain_3d_hit_keeps_sample_interval_and_does_not_fabricate_exact_xyz():
    hits = visibility.obstruction_hits_3d(
        terrain_los={
            "status": "blocked",
            "terrain_profile": {"samples": [{
                "status": "blocked", "distance_m": 40.0,
                "distance_interval_m": [37.5, 42.5], "terrain_max_m": 12.0,
            }]},
        },
        building_los={"status": "clear", "obstruction_hits": []},
    )
    assert hits[0]["type"] == "terrain"
    assert hits[0]["along_ray_interval_m"] == [37.5, 42.5]
    assert hits[0]["hit_x"] is None
    assert hits[0]["hit_derivation_status"] == "sample_interval_not_exact_intersection"


def test_combined_fingerprint_binds_sources_budgets_and_completion():
    first = visibility.combined_visibility_fingerprint(
        terrain_input_fingerprint="t", building_input_fingerprint="b",
        route_probe_identity={"probe_id": "P1"}, radar_origin={"tower_id": "T1"},
        radar_geometry={"version": "frozen"}, terrain_source_identity={"mtime": 1},
        building_source_identity={"mtime": 1}, crs={"horizontal": "EPSG:32651"},
        ray_model={"k_factor": 1}, budgets={"max_rays": 64}, completion={"state": "complete"},
    )
    second = visibility.combined_visibility_fingerprint(
        terrain_input_fingerprint="t", building_input_fingerprint="b",
        route_probe_identity={"probe_id": "P1"}, radar_origin={"tower_id": "T1"},
        radar_geometry={"version": "frozen"}, terrain_source_identity={"mtime": 1},
        building_source_identity={"mtime": 2}, crs={"horizontal": "EPSG:32651"},
        ray_model={"k_factor": 1}, budgets={"max_rays": 64}, completion={"state": "complete"},
    )
    assert first != second
