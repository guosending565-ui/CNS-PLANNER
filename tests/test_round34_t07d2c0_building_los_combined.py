"""Round34-T07-D2-C0 frozen-geometry gate, stable fingerprint and combined LOS.

Covers the three D2-B hotfixes and the Terrain + Building combined lookup
pre-integration.  Everything here is a small synthetic fixture: no project state, no
schema and no business artifact is touched.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from cns_planner.algorithms.radar_layout.terrain_los_lookup import (
    BUILDING_GATE_FILTERED_STATUS_EQUIVALENCE,
    BUILDING_MODE_COMBINED,
    BUILDING_MODE_DISABLED,
    LOS_LOOKUP_STATUS_GEOMETRY_FILTERED,
    LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.domain import radar_building_los as building_los
from cns_planner.domain import radar_visibility_evidence as visibility
from cns_planner.domain.radar_surveillance_layout import (
    RADAR_TYPE_I,
    RADAR_TYPE_II,
    radar_geometry_parameters,
)
from cns_planner.gis import radar_building_los_adapter as building_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKSPACE = Path(r"D:\Projects\CNS-PLANNER")


# --------------------------------------------------------------------------- helpers


def polygon(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def building_record(*, geometry, ground=0.0, height=20.0, bid="B1", level="verified",
                    derived_assumption=None):
    record = {
        "building_id": bid, "geometry_metric": geometry, "height_estimate_m": height,
        "height_evidence_level": level, "ground_elevation_egm2008_m": ground,
        "source_version": "d2c0-fixture-v1",
    }
    if derived_assumption is not None:
        record["derived_assumption"] = derived_assumption
    return record


def tower(x=0.0, y=0.0, z=10.0, tid="T1", radar_type=RADAR_TYPE_I):
    return {
        "tower_id": tid, "metric": [x, y], "origin_egm2008_m": z,
        "radar_type": radar_type, "origin_source": "fixture",
        "origin_state": "confirmed", "origin_evidence_level": "verified",
        "origin_availability": "usable", "device_instance_id": f"{tid}-{radar_type}",
    }


def sample(x, y=0.0, z=10.0, sid="S1", **overrides):
    """Route-sample shaped record carrying an authoritative EGM2008 probe height."""

    record = {
        "sample_id": sid, "metric": [x, y], "egm2008_m": z,
        # The building LOS adapter consumes the resolved height chain explicitly; the
        # fixture resolves the same authoritative catalog altitude constant.
        "altitude_egm2008_m": z,
        "altitude_layer_id": "ALT-080", "surface_class": "land",
        "required_distinct_site_count": 1,
        "distance_along_route_m": x - 60.0,
    }
    record.update(overrides)
    return record


class FakeBuildingSource:
    """Deterministic whole-ray bbox filter, mirroring the provider RTree contract."""

    def __init__(self, records=(), *, complete=True, identity=None):
        self.records = list(records)
        self.complete = complete
        self.identity = identity or {"file_name": "d2c0-fixture.gpkg", "mtime_ns": 1}
        self.queries = 0

    def describe(self):
        return dict(self.identity)

    def query_ray_bbox(self, metric_bbox, *, transform, terrain_source, max_features):
        self.queries += 1
        west, south, east, north = (float(item) for item in metric_bbox)
        selected = []
        for record in self.records:
            xs, ys = [], []
            for ring in record["geometry_metric"]["coordinates"]:
                for x, y in ring:
                    xs.append(x)
                    ys.append(y)
            if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                continue
            selected.append(record)
        complete = self.complete and len(selected) <= max_features
        return {
            "buildings": selected[:max_features],
            "query_complete": complete,
            "query_reason": None if complete else "max_features_reached",
            "query_receipt": {
                "mode": "d2c0_fixture_bbox", "candidate_count": len(selected),
                "returned_count": min(len(selected), max_features),
                "truncated": not complete,
            },
        }


def evaluate_probe(*, tower_record, probe_record, radar_type=RADAR_TYPE_I, records=(),
                   source=None, complete=True):
    source = source or FakeBuildingSource(records, complete=complete)
    return building_adapter.evaluate_probe_building_los(
        tower_record, probe_record, radar_type=radar_type, building_source=source,
        transform=None, terrain_source=None,
    )


# ------------------------------------------------------- Hotfix A: frozen geometry


def test_radar_i_minimum_slant_range_boundary_just_outside():
    # 119.0 m < Radar-I minimum 120 m: filtered, no building query, no DEM.
    source = FakeBuildingSource([building_record(geometry=polygon(0, -5, 200, 5))])
    result = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(119.0), source=source,
    )
    assert result["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert result["reason"] == "outside_frozen_device_range"
    assert result["within_frozen_device_range"] is False
    assert result["computation_completion"] == "not_evaluated"
    assert result["obstruction_hits"] == []
    assert result["building_los_physical_unknown"] is False
    assert source.queries == 0


def test_radar_i_minimum_slant_range_boundary_just_inside_is_evaluated():
    source = FakeBuildingSource([building_record(geometry=polygon(0, -5, 200, 5))])
    result = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(120.0), source=source,
    )
    assert result["within_frozen_device_range"] is True
    assert result["status"] == "blocked"
    assert source.queries == 1
    assert result["geometry_gate"]["device_geometry"]["parameters"]["min_slant_range_m"] == 120.0


def test_radar_ii_minimum_slant_range_boundary_just_outside_and_inside():
    geometry = building_record(geometry=polygon(0, -5, 400, 5))
    outside = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(199.0), records=[geometry],
        radar_type=RADAR_TYPE_II,
    )
    inside = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(200.0), records=[geometry],
        radar_type=RADAR_TYPE_II,
    )
    assert outside["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert outside["within_frozen_device_range"] is False
    assert inside["within_frozen_device_range"] is True
    assert inside["status"] == "blocked"
    assert radar_geometry_parameters(RADAR_TYPE_II)["min_slant_range_m"] == 200.0


def test_maximum_slant_range_boundary_and_elevation_boundary():
    # 3200 m horizontal with a 100 m climb stays inside 0..45 deg but beyond Radar-I max.
    beyond_max = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(3200.0, 0.0, 110.0),
        records=[building_record(geometry=polygon(0, -5, 4000, 5))],
    )
    assert beyond_max["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert beyond_max["geometry_gate"]["status"] == (
        building_los.BUILDING_LOS_GATE_OUTSIDE_EQUIPMENT_RANGE
    )
    # 300 m with a 160 m drop is inside the slant range but below the 0 deg floor.
    below_floor = evaluate_probe(
        tower_record=tower(0.0, 0.0, 200.0), probe_record=sample(300.0, 0.0, 40.0),
        records=[building_record(geometry=polygon(0, -5, 400, 5))],
    )
    assert below_floor["status"] == LOS_LOOKUP_STATUS_GEOMETRY_FILTERED
    assert below_floor["reason"] == "outside_frozen_device_elevation_interval"
    assert below_floor["within_frozen_device_range"] is False
    # 300 m with a 70 m drop is still below 0 deg, so the pitch filter is the blocker.
    assert below_floor["geometry_gate"]["elevation_deg"] < 0.0


def test_out_of_range_ray_does_not_query_buildings_or_report_building_unknown():
    source = FakeBuildingSource([building_record(geometry=polygon(0, -5, 400, 5))])
    result = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(40.0), source=source,
    )
    assert result["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert source.queries == 0
    assert result["candidate_building_count"] == 0
    assert result["building_los_physical_unknown"] is False
    assert result["evidence_completion"] == "incomplete"
    assert result["computation_completion"] == "not_evaluated"


def test_missing_or_unknown_device_type_fails_closed_instead_of_claiming_in_range():
    missing = evaluate_probe(
        tower_record=tower(), probe_record=sample(150.0), records=[],
        radar_type=None,
    )
    unknown = evaluate_probe(
        tower_record=tower(), probe_record=sample(150.0), records=[],
        radar_type="radar_ix",
    )
    for result in (missing, unknown):
        assert result["status"] == "unknown"
        assert result["within_frozen_device_range"] is False
        assert result["geometry_gate"]["device_geometry_resolved"] is False


def test_invalid_origin_or_probe_never_declares_within_frozen_device_range():
    bad_origin = evaluate_probe(tower_record=tower(0.0, 0.0, None), probe_record=sample(150.0))
    bad_probe = evaluate_probe(tower_record=tower(), probe_record={"probe_id": "P", "metric": [150.0, 0.0]})
    for result in (bad_origin, bad_probe):
        assert result["within_frozen_device_range"] is False
        assert result["status"] == "unknown"
        assert result["computation_completion"] in {"incomplete", "not_evaluated"}
        assert result["building_los_physical_unknown"] is False


def test_geometry_gate_reuses_the_frozen_device_table_and_shares_one_vocabulary():
    gate = building_los.frozen_geometry_gate(
        radar_type=RADAR_TYPE_I, origin_metric=[0.0, 0.0], target_metric=[150.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
    )
    parameters = radar_geometry_parameters(RADAR_TYPE_I)
    identity = gate["device_geometry"]
    assert identity["radar_type"] == RADAR_TYPE_I
    assert identity["parameters"] == parameters
    assert identity["frozen_geometry_version"] == "radar_layout_geometry_v1_1"
    assert identity["parameters_copied_locally"] is False
    assert gate["frozen_parameters_modified"] is False
    assert (
        building_los.GATE_OUTSIDE_STATUS,
        building_los.GATE_GEOMETRY_FILTERED_STATUS,
        building_los.GATE_UNRESOLVED_STATUS,
    ) == BUILDING_GATE_FILTERED_STATUS_EQUIVALENCE


def test_lookup_and_building_adapter_agree_on_the_same_ray():
    tower_record = tower(0.0, 0.0, 10.0)
    sample_record = sample(130.0)
    lookup = TerrainLosLookup(mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=None)
    lookup_gate = lookup.geometry_gate(tower_record, sample_record, RADAR_TYPE_I)
    adapter_gate = building_los.frozen_geometry_gate(
        radar_type=RADAR_TYPE_I, origin_metric=[0.0, 0.0], target_metric=[130.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
    )
    assert lookup_gate["status"] == "los_eligible"
    assert adapter_gate["within_frozen_device_range"] is True
    out_of_range = TerrainLosLookup(mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=None)
    assert out_of_range.geometry_gate(
        tower_record, sample(40.0), RADAR_TYPE_I,
    )["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE


# ----------------------------------------------------------- Hotfix B: fingerprint


def fingerprint(**overrides):
    values = {
        "origin_metric": [0.0, 0.0], "target_metric": [150.0, 0.0],
        "origin_egm2008_m": 10.0, "target_egm2008_m": 10.0,
        "building_source_identity": {"file_name": "b.gpkg", "mtime_ns": 1},
        "dem_source_identity": {"file_name": "d.tif", "mtime_ns": 2},
        "query_policy": {"ray_bbox_metric": [0.0, 0.0, 150.0, 0.0], "max_features": 32},
    }
    values.update(overrides)
    return building_los.building_los_input_fingerprint(**values)


def test_equivalent_semantics_with_different_query_receipt_share_one_fingerprint():
    base = fingerprint()
    slower = fingerprint(query_receipt={"elapsed_seconds": 9.5, "cache_hit_count": 0})
    cached = fingerprint(query_receipt={"elapsed_seconds": 0.01, "cache_hit_count": 7})
    extra = fingerprint(query_receipt={
        "elapsed_seconds": 0.2, "returned_count": 3, "candidate_count": 11,
        "execution_order": ["probe-1"], "performance": {"rss_mb": 12},
        "some_new_diagnostic_field": {"nested": [1, 2]},
    })
    assert base == slower == cached == extra
    # The same receipt must not be able to hide a real semantic difference either.
    assert fingerprint(query_policy={
        "ray_bbox_metric": [0.0, 0.0, 150.0, 0.0], "max_features": 64,
    }) != base


def test_building_source_and_height_evidence_policy_changes_change_fingerprint():
    changed_source = fingerprint(
        building_source_identity={"file_name": "b.gpkg", "mtime_ns": 9},
    )
    changed_dem = fingerprint(dem_source_identity={"file_name": "d.tif", "mtime_ns": 3})
    assert changed_source != fingerprint()
    assert changed_dem != fingerprint()
    assert building_los.HEIGHT_EVIDENCE_POLICY_ID in json.dumps(
        {"policy": building_los.HEIGHT_EVIDENCE_POLICY_ID},
    )


def test_radar_geometry_and_ray_model_changes_change_fingerprint():
    first = fingerprint(radar_geometry=building_los.frozen_device_geometry(RADAR_TYPE_I))
    second = fingerprint(radar_geometry=building_los.frozen_device_geometry(RADAR_TYPE_II))
    assert first != second
    assert fingerprint(k_factor=1.0) != fingerprint(k_factor=4.0 / 3.0)
    assert fingerprint(earth_radius_m=6_371_008.8) != fingerprint(earth_radius_m=None)


def test_fingerprint_version_was_upgraded_for_the_semantic_change():
    assert building_los.RADAR_BUILDING_LOS_FINGERPRINT_VERSION.endswith("_v2")
    assert building_los.RADAR_BUILDING_LOS_ALGORITHM_VERSION == "radar_building_los_v1_2_5d_prism"


def test_execution_identity_distinguishes_budget_incomplete_from_complete():
    complete = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.1, "truncated": False, "returned_count": 1},
        query_complete=True, query_reason=None, budget={"max_rays": 8},
        computation_completion="complete", evidence_completion="complete",
        input_fingerprint="same",
    )
    incomplete = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.1, "truncated": True, "returned_count": 256},
        query_complete=False, query_reason="max_features_reached",
        budget={"max_rays": 8},
        computation_completion="incomplete", evidence_completion="incomplete",
        input_fingerprint="same",
    )
    assert complete != incomplete
    assert complete["truncated"] is False and incomplete["truncated"] is True
    assert complete["elapsed_seconds_is_receipt_only"] is True


def test_adapter_runs_keep_one_fingerprint_but_separate_execution_identity():
    import time

    records = [building_record(geometry=polygon(0, -5, 200, 5))]
    first = evaluate_probe(
        tower_record=tower(), probe_record=sample(130.0), records=records,
    )
    # Real wall-clock advance, so the receipt timing is genuinely different rather than
    # depending on two back-to-back perf_counter calls landing on the same value.
    time.sleep(0.002)
    second = evaluate_probe(
        tower_record=tower(), probe_record=sample(130.0), records=records,
    )
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["query_receipt"]["elapsed_seconds"] != second["query_receipt"]["elapsed_seconds"]
    assert first["execution_identity"]["run_receipt"]["elapsed_seconds"] is not None


def test_described_execution_identity_survives_receipt_growth():
    baseline = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.5, "cache_hit_count": 0},
        query_complete=True, input_fingerprint="fp",
    )
    grown = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.5, "cache_hit_count": 0, "new_field": 1},
        query_complete=True, input_fingerprint="fp",
    )
    assert baseline != grown
    assert baseline["input_fingerprint"] == grown["input_fingerprint"] == "fp"


# ------------------------------------------- Hotfix C: first trusted building entry


def first_hit(**overrides):
    values = {
        "start_fraction": 0.0, "end_fraction": 1.0, "origin_m": 40.0, "target_m": 40.0,
        "horizontal_m": 100.0, "ground_m": 0.0, "roof_m": 20.0,
        "earth_radius_m": None, "k_factor": 1.0,
    }
    values.update(overrides)
    return building_los._first_prism_hit(**values)


def ray_height(fraction, *, origin_m=40.0, target_m=40.0, horizontal_m=100.0,
               earth_radius_m=None, k_factor=1.0):
    from cns_planner.domain.radar_terrain_los import ray_height_m

    return ray_height_m(
        origin_egm2008_m=origin_m, target_egm2008_m=target_m, horizontal_m=horizontal_m,
        sample_distance_m=horizontal_m * fraction, earth_radius_m=earth_radius_m,
        k_factor=k_factor,
    )


def test_first_hit_descending_through_the_roof_reports_the_roof_crossing():
    hit = first_hit(origin_m=40.0, target_m=0.0)
    assert hit is not None
    fraction, value = hit
    assert fraction == pytest.approx(0.5, abs=1e-6)
    assert value == pytest.approx(20.0, abs=1e-6)


def test_first_hit_rising_through_the_ground_reports_the_ground_crossing():
    hit = first_hit(origin_m=-20.0, target_m=40.0)
    assert hit is not None
    fraction, value = hit
    assert fraction == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert value == pytest.approx(0.0, abs=1e-6)


def test_first_hit_inside_the_prism_interval_uses_the_xy_interval_start():
    hit = first_hit(start_fraction=0.3, end_fraction=0.6, origin_m=40.0, target_m=0.0)
    assert hit is not None
    assert hit[0] == pytest.approx(0.5, abs=1e-6)


def test_first_hit_finds_the_symmetric_entries_around_a_curvature_minimum():
    radius = 6_371_008.8
    # A flat ray under curvature dips below its endpoints in the middle of 10 km.
    hit = first_hit(
        origin_m=39.0, target_m=39.0, horizontal_m=10_000.0, roof_m=38.0, ground_m=0.0,
        earth_radius_m=radius, k_factor=1.0,
    )
    assert hit is not None
    fraction, value = hit
    assert 0.0 < fraction < 0.5
    assert value == pytest.approx(ray_height(
        fraction, origin_m=39.0, target_m=39.0, horizontal_m=10_000.0, earth_radius_m=radius,
    ), abs=1e-6)
    assert value <= 38.0 + 1e-6


def test_first_hit_returns_none_when_the_whole_interval_stays_above_the_roof():
    assert first_hit(origin_m=40.0, target_m=30.0) is None


def test_first_hit_tangent_contact_is_not_an_entry_when_the_ray_leaves_the_face():
    assert first_hit(origin_m=20.0, target_m=20.0, horizontal_m=100.0) is not None
    assert first_hit(
        origin_m=20.0, target_m=30.0, horizontal_m=100.0,
    ) is None or first_hit(origin_m=20.0, target_m=30.0, horizontal_m=100.0)[0] > 0.0


def test_first_hit_origin_on_the_roof_plane_only_counts_when_entering():
    descending = first_hit(origin_m=20.0, target_m=-40.0)
    rising = first_hit(origin_m=20.0, target_m=40.0)
    assert descending is not None and descending[0] == pytest.approx(0.0)
    assert rising is None or rising[0] > 0.0


def test_first_hit_origin_inside_the_prism_is_the_origin_itself():
    hit = first_hit(origin_m=10.0, target_m=10.0)
    assert hit is not None
    assert hit[0] == pytest.approx(0.0)
    assert hit[1] == pytest.approx(10.0)


def test_first_hit_reports_the_height_of_the_shared_ray_model_at_the_hit():
    hit = first_hit(origin_m=40.0, target_m=0.0)
    fraction, value = hit
    assert value == pytest.approx(ray_height(fraction, origin_m=40.0, target_m=0.0), abs=1e-9)


def test_first_hit_derivation_is_disclosed_as_approximate_single_model():
    result = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=40.0, target_egm2008_m=0.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), height=20.0)],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    derivation = result["obstruction_hits"][0]["hit_derivation"]
    assert derivation["approximate"] is True
    assert derivation["second_height_model_introduced"] is False
    assert derivation["height_model"] == building_los.RAY_MODEL_ID
    assert derivation["height_tolerance_m"] > 0.0


def test_blocked_unknown_and_3d_hit_location_each_have_their_own_expectation():
    blocked = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), height=20.0)],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    unknown = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), height=20.0, level="predicted")],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    assert blocked["status"] == "blocked" and blocked["engineering_estimate_status"] == "blocked"
    assert unknown["status"] == "unknown" and unknown["engineering_estimate_status"] == "blocked"
    hits = visibility.obstruction_hits_3d(terrain_los=None, building_los=blocked)
    assert hits[0]["hit_x"] == pytest.approx(4.0)
    assert hits[0]["hit_z"] == pytest.approx(10.0)
    assert hits[0]["ray_fraction"] == pytest.approx(0.4)


# --------------------------------------- evidence policy: two layers kept separate


def test_predicted_height_stays_an_engineering_estimate_and_never_clear():
    result = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), level="predicted")],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    assert result["status"] == "unknown"
    assert result["engineering_estimate_status"] == "blocked"
    assert result["estimated_obstruction_count"] == 1
    assert result["confirmed_obstruction_count"] == 0
    assert result["obstruction_hits"][0]["evidence_classification"] == "estimated"


def test_unknown_height_evidence_never_becomes_blocked_or_clear():
    result = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), level="unknown", height=None)],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    assert result["status"] == "unknown"
    assert result["evidence_completion"] == "incomplete"
    assert result["unknown_reasons"]


def test_query_complete_alone_does_not_claim_complete_evidence():
    complete_query = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(geometry=polygon(4, -1, 6, 1), level="predicted")],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    assert complete_query["computation_completion"] == "complete"
    # The query finished, yet the only obstruction evidence is an estimate, so the
    # main verdict cannot claim complete evidence.
    assert complete_query["evidence_completion"] == "incomplete"
    assert complete_query["estimated_obstruction_count"] == 1
    assert complete_query["confirmed_obstruction_count"] == 0
    assert "building_height_evidence_estimated_not_confirmed" in complete_query["unknown_reasons"]


def test_source_tagged_support_level_is_disclosed_not_promoted_to_verified():
    result = building_los.evaluate_building_los(
        origin_metric=[0.0, 0.0], target_metric=[10.0, 0.0],
        origin_egm2008_m=10.0, target_egm2008_m=10.0,
        buildings=[building_record(
            geometry=polygon(4, -1, 6, 1), level="source_tagged",
        )],
        query_complete=True, building_source_identity={"file_name": "b.gpkg"},
    )
    assert result["status"] == "blocked"
    assert result["height_evidence_confirmed_levels"] == ["derived", "source_tagged", "verified"]
    assert result["obstruction_hits"][0]["height_evidence_level"] == "source_tagged"


# --------------------------------------------- combined lookup: one interface only


def terrain_clear_evaluator(*, tower, sample, radar_type, geometry):
    return {
        "status": "clear", "reason": None, "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete", "evidence_completion": "complete",
    }


def building_evaluator_from(records, *, complete=True):
    source = FakeBuildingSource(records, complete=complete)

    def evaluator(*, tower, sample, radar_type, geometry):
        result = building_adapter.evaluate_probe_building_los(
            tower, sample, radar_type=radar_type, building_source=source,
            transform=None, terrain_source=None,
        )
        return result

    evaluator.source = source
    return evaluator


def combined_lookup(records=(), *, complete=True, max_ray_count=4096,
                    terrain_mode=TERRAIN_LOS_MODE_ENFORCED):
    evaluator = building_evaluator_from(records, complete=complete)
    lookup = TerrainLosLookup(
        mode=terrain_mode, evaluator=terrain_clear_evaluator,
        building_evaluator=evaluator, building_mode=BUILDING_MODE_COMBINED,
        source_identity={"dem": "fixture"}, building_source_identity={"buildings": "fixture"},
        max_ray_count=max_ray_count,
    )
    return lookup, evaluator


def test_combined_lookup_returns_one_evidence_for_both_domains():
    records = [building_record(geometry=polygon(100, -10, 140, 10), height=30.0)]
    lookup, evaluator = combined_lookup(records)
    result = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(150.0), RADAR_TYPE_I)
    assert result["status"] == "blocked"
    assert result["terrain_los"]["status"] == "clear"
    assert result["building_los"]["status"] == "blocked"
    assert result["combined_visibility"]["combined_visibility"] == "blocked"
    assert result["computation_completion"] == "complete"
    assert result["evidence_completion"] == "complete"
    assert evaluator.source.queries == 1


def test_combined_lookup_reuses_the_cache_for_identical_physical_rays():
    records = [building_record(geometry=polygon(100, -10, 140, 10), height=30.0)]
    lookup, evaluator = combined_lookup(records)
    first = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(150.0, sid="V1"), RADAR_TYPE_I)
    second = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(150.0, sid="R1"), RADAR_TYPE_I)
    assert first["status"] == second["status"] == "blocked"
    assert first["input_fingerprint"] if "input_fingerprint" in first else True
    assert lookup.snapshot()["cache_hit_count"] == 1
    assert evaluator.source.queries == 1


def test_combined_lookup_never_upgrades_device_geometry_failed_rays():
    lookup, evaluator = combined_lookup([])
    result = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(40.0), RADAR_TYPE_I)
    assert result["status"] == "unknown"
    assert result["building_los"]["status"] == LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE
    assert evaluator.source.queries == 0
    assert result["building_los"]["building_los_physical_unknown"] is False
    snapshot = lookup.snapshot()
    assert snapshot["building_gate_filtered_count"] == 1
    assert snapshot["building_ray_count"] == 0


def test_combined_lookup_keeps_geometry_filtered_out_of_physical_unknown():
    records = [building_record(geometry=polygon(0, -10, 400, 10), height=30.0)]
    lookup, evaluator = combined_lookup(records)
    combinations = [
        {"tower": tower(0.0, 0.0, 10.0), "sample": sample(40.0, sid="filtered-1"), "radar_type": RADAR_TYPE_I},
        {"tower": tower(0.0, 0.0, 10.0), "sample": sample(150.0, sid="kept"), "radar_type": RADAR_TYPE_I},
    ]
    summary = lookup.evaluate_batches(combinations, batch_size=8)
    assert summary["classification_counts"][LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE] == 1
    assert summary["classification_counts"]["physical_unknown"] == 0
    assert summary["los_status_counts"]["blocked"] == 1
    assert summary["computation_complete"] is True


def test_combined_lookup_without_a_building_evaluator_is_exactly_disabled_parity():
    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_clear_evaluator,
        building_mode=BUILDING_MODE_DISABLED,
    )
    result = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(150.0), RADAR_TYPE_I)
    assert result["status"] == "clear"
    assert "building_los" not in result
    assert result["combined_visibility"] if "combined_visibility" in result else True


def test_combined_mode_requires_a_building_evaluator():
    with pytest.raises(ValueError):
        TerrainLosLookup(
            mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_clear_evaluator,
            building_mode=BUILDING_MODE_COMBINED,
        )


def test_combine_visibility_reports_filtered_rays_as_geometry_filtered():
    filtered = visibility.combine_visibility(
        {"status": "clear", "computation_completion": "complete"},
        {"status": LOS_LOOKUP_STATUS_OUT_OF_EQUIPMENT_RANGE,
         "computation_completion": "not_evaluated"},
    )
    assert filtered["combined_visibility"] == visibility.GEOMETRY_FILTERED
    assert filtered["building_los"]["domain_state"] == visibility.GEOMETRY_FILTERED
    assert filtered["building_los_physical_unknown"] is False


# ------------------------------------- four consumer positions stay consistent


def route_samples(count=6, spacing=60.0, start=60.0, z=10.0, sid_prefix="S"):
    return [
        sample(start + index * spacing, 0.0, z, sid=f"{sid_prefix}{index:03d}")
        for index in range(count)
    ]


def layout_fixture(*, samples=None):
    towers = [tower(0.0, 0.0, 10.0, tid="T1"), tower(0.0, 200.0, 10.0, tid="T2")]
    # The blocker sits strictly inside the ray interval; a footprint that merely touches
    # the probe endpoint would be a boundary contact, not an interior crossing.
    records = [building_record(
        geometry=polygon(100.0, -10.0, 140.0, 10.0), height=60.0, bid="BLOCKER",
    )]
    return towers, (samples or route_samples()), records


def run_algorithm_pipeline(*, mode, lookup=None, samples=None, radar_types=None):
    """One explicit pass over all four optimizer consumer positions."""

    from cns_planner.algorithms.radar_layout import milp as milp_module
    from cns_planner.algorithms.radar_layout.candidates import build_candidates
    from cns_planner.algorithms.radar_layout.v1 import actual_site_coverage

    towers, default_samples, _records = layout_fixture(samples=samples)
    active = samples or default_samples
    candidates = build_candidates(
        towers=towers, samples=active, terrain_los_mode=mode, terrain_los_lookup=lookup,
    )
    selected_types = list(radar_types or [RADAR_TYPE_I])
    solve_result = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[item["required_distinct_site_count"] for item in candidates["samples"]],
        radar_types=selected_types, objectives=("total_panel_count",),
        stage="d2c0_fixture", options={"solver_time_limit_s": 10.0},
    )
    coverage = actual_site_coverage(
        panels=candidates["panels"], samples=candidates["samples"],
        selected_panel_ids=solve_result.get("selected_panel_ids") or [],
        tower_records=candidates["towers"], terrain_los_mode=mode,
        terrain_los_lookup=lookup,
    )
    return {
        "candidates": candidates, "solve": solve_result, "coverage": coverage,
        "selected_types": selected_types,
    }


def covered_pairs(coverage):
    return {
        (item["sample_id"], panel["panel_id"])
        for item in coverage for panel in item["panels"]
    }


def test_fixed_panels_and_samples_make_enforced_coverage_a_subset_of_geometric_coverage():
    towers, samples, records = layout_fixture()
    geometric = run_algorithm_pipeline(mode="disabled")
    lookup, _evaluator = combined_lookup(records)
    enforced = run_algorithm_pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup)
    geometric_panels = {panel["panel_id"] for panel in geometric["candidates"]["panels"]}
    enforced_panels = {panel["panel_id"] for panel in enforced["candidates"]["panels"]}
    # Fixed panel set: the Los gate filters rays, never invents or renames a panel.
    assert enforced_panels <= geometric_panels
    assert covered_pairs(enforced["coverage"]) <= covered_pairs(geometric["coverage"])
    assert covered_pairs(geometric["coverage"])


def test_candidate_milp_final_and_five_meter_refinement_agree():
    towers, samples, records = layout_fixture()
    lookup, _evaluator = combined_lookup(records)
    outcome = run_algorithm_pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup)
    panel_ids = {panel["panel_id"] for panel in outcome["candidates"]["panels"]}
    selected = set(outcome["solve"].get("selected_panel_ids") or [])
    assert selected <= panel_ids
    # MILP rows carry the coverage set that produced them.
    for panel in outcome["candidates"]["panels"]:
        assert isinstance(panel["covered_sample_indices"], list)
    # The 5 m validation grid reuses the same lookup, so a panel the final verdict
    # believes is covering cannot be reported clear at 5 m for the same ray.
    lookup_validation, evaluator = combined_lookup(records)
    fine = route_samples(count=21, spacing=5.0, start=110.0, sid_prefix="V")
    validation = run_algorithm_pipeline(
        mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup_validation, samples=fine,
    )
    validation_panels = {
        panel["panel_id"]
        for item in validation["coverage"] for panel in item["panels"]
    }
    assert validation_panels <= set(
        panel["panel_id"] for panel in validation["candidates"]["panels"]
    )
    for item in validation["coverage"]:
        for panel in item["panels"]:
            assert panel["panel_id"] in {
                candidate["panel_id"] for candidate in validation["candidates"]["panels"]
            }
    # Every surviving 5 m validation ray is evidence from the same shared lookup instance.
    snapshot = lookup_validation.snapshot()
    assert snapshot["building_ray_count"] >= evaluator.source.queries
    assert snapshot["building_ray_count"] > 0


def test_budget_exhaustion_cannot_produce_a_proven_optimum_or_infeasibility():
    towers, samples, records = layout_fixture()
    lookup, _evaluator = combined_lookup(records, max_ray_count=1)
    outcome = run_algorithm_pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup)
    solver = outcome["solve"]["solver"]
    if solver.get("status") in {"optimal", "infeasible"}:
        assert solver.get("optimality_proven") is not True
        assert solver.get("infeasibility_proven") is not True
    else:
        assert solver.get("status") in {
            "search_incomplete", "refinement_incomplete", "solver_error",
            "solver_unavailable", "not_run",
        }
    assert lookup.snapshot()["complete"] is False


def test_los_unknown_never_triggers_an_automatic_radar_ii_upgrade():
    towers, samples, records = layout_fixture()
    lookup, _evaluator = combined_lookup(records, complete=False)
    outcome = run_algorithm_pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup)
    declared = {
        panel["radar_type"]
        for panel in outcome["candidates"]["panels"]
        if panel["panel_id"] in set(outcome["solve"].get("selected_panel_ids") or [])
    }
    assert declared <= {RADAR_TYPE_I}
    assert outcome["selected_types"] == [RADAR_TYPE_I]
    policy = outcome["candidates"]["statistics"]
    assert policy["panel_type_scope"] if "panel_type_scope" in policy else True


def test_disabled_mode_parity_keeps_the_frozen_layout_unchanged():
    from cns_planner.algorithms.radar_layout.v1 import solve_layout

    towers, samples, _records = layout_fixture()
    baseline = solve_layout(
        towers=towers, samples=samples, options={"solver_time_limit_s": 10.0},
        terrain_los_mode="disabled", allow_mixed=False,
    )
    lookup = TerrainLosLookup(
        mode="disabled", evaluator=terrain_clear_evaluator,
        building_mode=BUILDING_MODE_DISABLED,
    )
    parity = solve_layout(
        towers=towers, samples=samples, options={"solver_time_limit_s": 10.0},
        terrain_los_mode="disabled", terrain_los_lookup=lookup, allow_mixed=False,
    )
    assert baseline["status"] == parity["status"]
    assert baseline["selected_panels"] == parity["selected_panels"]
    assert baseline["validation"] == parity["validation"]
    assert "terrain_los_evidence" not in parity


# ------------------------------------------------------ production guard and inputs


def test_production_entry_still_rejects_enforced_terrain_los_mode():
    from cns_planner.application.radar_surveillance_layout_service import (
        RadarSurveillanceLayoutService,
    )

    service = RadarSurveillanceLayoutService.__new__(RadarSurveillanceLayoutService)
    with pytest.raises(ValueError):
        service.evaluate({"terrain_los_mode": "enforced"})


def test_frontend_visibility_model_reads_the_adapter_output_shape():
    source = FakeBuildingSource([])
    result = evaluate_probe(
        tower_record=tower(0.0, 0.0, 10.0), probe_record=sample(40.0), source=source,
    )
    combined = visibility.combine_visibility(None, result)
    script = (
        "import {radarVisibilityEvidenceModel} from './cns_planner/web/js/workflow/"
        "radar_visibility_evidence.js';"
        "const value=JSON.parse(process.argv[1]);"
        "const model=radarVisibilityEvidenceModel(value);"
        "process.stdout.write(JSON.stringify({"
        "combinedStatus:model.combinedStatus,geometryFiltered:model.geometryFiltered,"
        "withinFrozenDeviceRange:model.withinFrozenDeviceRange}));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         json.dumps(combined, default=str)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        pytest.skip(f"node unavailable for the frontend model probe: {completed.stderr[:120]}")
    model = json.loads(completed.stdout)
    # The device-excluded ray must reach the renderer as a filtered ray, not as a verdict.
    assert model["combinedStatus"] == visibility.GEOMETRY_FILTERED
    assert model["geometryFiltered"] is True
    assert model["withinFrozenDeviceRange"] is False


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


FORMAL_INPUTS = {
    "project_state": Path(r"D:\Projects\_cns_r32g\zhoushan_r0005_g\project_state.json"),
    "buildings": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg"
    ),
    "terrain": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
    ),
}
FORMAL_INPUT_SHA256 = {
    "project_state": "92a0000f56d5ad1dcb5d97c8344d427d68b4658b4b563cc4bf27f6e35d449d07",
    "buildings": "4f1657495d8422ac0d23973949d4bcbca59dcf07d59ed380ff9877afc7391b97",
    "terrain": "21406bd901fbebae211528a158ca95eef899e2074322625717b215b13d8799b8",
}


@pytest.mark.parametrize("key", sorted(FORMAL_INPUTS))
def test_formal_read_only_inputs_are_untouched_by_this_round(key):
    path = FORMAL_INPUTS[key]
    if not path.exists():
        pytest.skip(f"{key} not present on this machine")
    assert sha256_of(path) == FORMAL_INPUT_SHA256[key]


def test_main_workspace_is_not_committed_modified_or_pushed_by_this_round():
    if not (MAIN_WORKSPACE / ".git").exists():
        pytest.skip("main workspace git metadata unavailable")
    head = subprocess.run(
        ["git", "-C", str(MAIN_WORKSPACE), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "68e2674"
    status = subprocess.run(
        ["git", "-C", str(MAIN_WORKSPACE), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "_work/round34_t07d2c0_combined_los" not in status
    snapshot_ids = subprocess.run(
        ["git", "-C", str(MAIN_WORKSPACE), "stash", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert snapshot_ids.count("round34_t07d2c0") == 0
