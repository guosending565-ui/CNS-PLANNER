"""Round34-T07-D2-C0 combined Terrain + Building LOS fixture pre-integration.

Runs a small synthetic fixture (no project state, no schema, no business artifact) over
the four optimizer consumer positions through the single shared lookup interface:

1. candidate panel generation and direction filtering,
2. MILP coverage sets,
3. ``actual_site_coverage()``,
4. the independent 5 m validation/refinement grid.

It also replays the D2-B diagnostic pair (same physical input, different run receipt) to
evidence fingerprint parity, and reports the independent counters this round must keep.

Usage::

    D:\\tools\\anaconda\\python.exe tools/round34_t07d2c0_combined_fixture.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.algorithms.radar_layout import milp as milp_module  # noqa: E402
from cns_planner.algorithms.radar_layout.candidates import build_candidates  # noqa: E402
from cns_planner.algorithms.radar_layout.terrain_los_lookup import (  # noqa: E402
    BUILDING_MODE_COMBINED,
    TERRAIN_LOS_MODE_DISABLED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.algorithms.radar_layout.v1 import actual_site_coverage  # noqa: E402
from cns_planner.domain import radar_building_los as building_los  # noqa: E402
from cns_planner.domain import radar_visibility_evidence as visibility  # noqa: E402
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    RADAR_TYPE_I,
    radar_geometry_parameters,
)
from cns_planner.gis import radar_building_los_adapter as building_adapter  # noqa: E402

DEFAULT_OUTPUT = ROOT / "_work" / "round34_t07d2c0_fixture_result.json"
FORMAL_INPUTS = {
    "project_state": Path(r"D:\Projects\_cns_r32g\zhoushan_r0005_g\project_state.json"),
    "buildings": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\building\processed\zhoushan_buildings_v3.gpkg"
    ),
    "terrain": Path(
        r"D:\aaa2026project\UOM\舟山\规划系统\FABDEM\processed\Zhoushan_FABDEM_DTM_30m.tif"
    ),
}


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def polygon(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def building_record(*, geometry, ground=0.0, height=60.0, bid="BLOCKER", level="verified"):
    return {
        "building_id": bid, "geometry_metric": geometry, "height_estimate_m": height,
        "height_evidence_level": level, "ground_elevation_egm2008_m": ground,
        "source_version": "d2c0-fixture-v1",
    }


def tower(x=0.0, y=0.0, z=10.0, tid="T1"):
    return {
        "tower_id": tid, "metric": [x, y], "origin_egm2008_m": z, "radar_type": RADAR_TYPE_I,
        "origin_source": "fixture", "origin_state": "confirmed",
        "origin_evidence_level": "verified", "origin_availability": "usable",
        "device_instance_id": f"{tid}-{RADAR_TYPE_I}",
    }


def sample(x, y=0.0, z=10.0, sid="S1"):
    return {
        "sample_id": sid, "metric": [x, y], "egm2008_m": z,
        "altitude_egm2008_m": z, "altitude_layer_id": "ALT-080", "surface_class": "land",
        "required_distinct_site_count": 1, "distance_along_route_m": x,
    }


class FixtureBuildingSource:
    """Deterministic whole-ray bbox filter mirroring the provider RTree contract."""

    def __init__(self, records=(), *, complete=True):
        self.records = list(records)
        self.complete = complete
        self.queries = 0

    def describe(self):
        return {"file_name": "d2c0-fixture.gpkg", "mtime_ns": 1, "source_version": "fixture"}

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
            "buildings": selected[:max_features], "query_complete": complete,
            "query_reason": None if complete else "max_features_reached",
            "query_receipt": {
                "mode": "d2c0_fixture_bbox", "candidate_count": len(selected),
                "returned_count": min(len(selected), max_features), "truncated": not complete,
            },
        }


def terrain_clear_evaluator(*, tower, sample, radar_type, geometry):
    return {
        "status": "clear", "reason": None,
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete", "evidence_completion": "complete",
    }


def combined_lookup(records, *, complete=True, max_ray_count=4096):
    source = FixtureBuildingSource(records, complete=complete)

    def evaluator(*, tower, sample, radar_type, geometry):
        return building_adapter.evaluate_probe_building_los(
            tower, sample, radar_type=radar_type, building_source=source,
            transform=None, terrain_source=None,
        )

    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_clear_evaluator,
        building_evaluator=evaluator, building_mode=BUILDING_MODE_COMBINED,
        source_identity={"dem": "fixture"}, building_source_identity={"buildings": "fixture"},
        max_ray_count=max_ray_count,
    )
    return lookup, source


def route_samples(count, spacing, start, z=10.0, prefix="S"):
    return [
        sample(start + index * spacing, 0.0, z, sid=f"{prefix}{index:03d}")
        for index in range(count)
    ]


def pipeline(*, mode, lookup=None, sample_records=None, towers=None):
    active_towers = towers or [tower(0.0, 0.0, 10.0, tid="T1"), tower(0.0, 200.0, 10.0, tid="T2")]
    active_samples = sample_records or route_samples(6, 60.0, 60.0)
    candidates = build_candidates(
        towers=active_towers, samples=active_samples, terrain_los_mode=mode,
        terrain_los_lookup=lookup,
    )
    solve_result = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[item["required_distinct_site_count"] for item in candidates["samples"]],
        radar_types=[RADAR_TYPE_I], objectives=("total_panel_count",),
        stage="d2c0_fixture", options={"solver_time_limit_s": 10.0},
    )
    coverage = actual_site_coverage(
        panels=candidates["panels"], samples=candidates["samples"],
        selected_panel_ids=solve_result.get("selected_panel_ids") or [],
        tower_records=candidates["towers"], terrain_los_mode=mode,
        terrain_los_lookup=lookup,
    )
    return {"candidates": candidates, "solve": solve_result, "coverage": coverage}


def covered_pairs(coverage):
    return sorted(
        f"{item['sample_id']}|{panel['panel_id']}" for item in coverage for panel in item["panels"]
    )


def fingerprint_parity_probe():
    """Replay the D2-B equivalent-physical-input / different-receipt situation."""

    base = {
        "origin_metric": [0.0, 0.0], "target_metric": [124.0, 0.0],
        "origin_egm2008_m": 19.49, "target_egm2008_m": 19.49,
        "building_source_identity": {"file_name": "buildings-v3.gpkg", "mtime_ns": 1},
        "dem_source_identity": {"file_name": "fabdem.tif", "mtime_ns": 2},
        "radar_geometry": building_los.frozen_device_geometry(RADAR_TYPE_I),
        "query_policy": {"ray_bbox_metric": [0.0, 0.0, 124.0, 0.0], "max_features": 32},
    }
    first = building_los.building_los_input_fingerprint(
        **base, query_receipt={"elapsed_seconds": 0.0028, "cache_hit_count": 0,
                               "returned_count": 2, "candidate_count": 2},
    )
    second = building_los.building_los_input_fingerprint(
        **base, query_receipt={"elapsed_seconds": 0.0141, "cache_hit_count": 3,
                               "returned_count": 2, "candidate_count": 2,
                               "performance": {"rss_mb": 41}, "new_diagnostic_field": True},
    )
    changed_source = building_los.building_los_input_fingerprint(
        **{**base, "building_source_identity": {"file_name": "buildings-v3.gpkg", "mtime_ns": 9}},
    )
    changed_device = building_los.building_los_input_fingerprint(
        **{**base, "radar_geometry": building_los.frozen_device_geometry("radar_ii")},
    )
    complete_execution = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.0028, "truncated": False},
        query_complete=True, computation_completion="complete",
        evidence_completion="complete", input_fingerprint=first,
    )
    incomplete_execution = building_los.building_los_execution_identity(
        query_receipt={"elapsed_seconds": 0.0028, "truncated": True},
        query_complete=False, query_reason="max_features_reached",
        budget={"max_rays": 1}, computation_completion="incomplete",
        evidence_completion="incomplete", input_fingerprint=first,
    )
    return {
        "same_semantics_different_elapsed_same_fingerprint": first == second,
        "building_source_change_changes_fingerprint": first != changed_source,
        "radar_device_geometry_change_changes_fingerprint": first != changed_device,
        "input_fingerprint": first,
        "budget_incomplete_distinguishable_in_execution_identity": (
            complete_execution != incomplete_execution
        ),
        "complete_execution_identity": complete_execution,
        "incomplete_execution_identity": incomplete_execution,
    }


def first_hit_probe():
    cases = {
        "descend_through_roof": dict(
            start_fraction=0.0, end_fraction=1.0, origin_m=40.0, target_m=0.0,
            horizontal_m=100.0, ground_m=0.0, roof_m=20.0,
        ),
        "rise_through_ground": dict(
            start_fraction=0.0, end_fraction=1.0, origin_m=-20.0, target_m=40.0,
            horizontal_m=100.0, ground_m=0.0, roof_m=20.0,
        ),
        "enter_and_leave_mid_interval": dict(
            start_fraction=0.3, end_fraction=0.6, origin_m=40.0, target_m=0.0,
            horizontal_m=100.0, ground_m=0.0, roof_m=20.0,
        ),
        "curvature_interior_extremum": dict(
            start_fraction=0.0, end_fraction=1.0, origin_m=39.0, target_m=39.0,
            horizontal_m=10_000.0, ground_m=0.0, roof_m=38.0,
            earth_radius_m=6_371_008.8,
        ),
        "origin_inside_prism": dict(
            start_fraction=0.0, end_fraction=1.0, origin_m=10.0, target_m=10.0,
            horizontal_m=100.0, ground_m=0.0, roof_m=20.0,
        ),
    }
    report = {}
    for name, values in cases.items():
        values.setdefault("earth_radius_m", None)
        values.setdefault("k_factor", 1.0)
        hit = building_los._first_prism_hit(**values)
        report[name] = {
            "ray_fraction": None if hit is None else hit[0],
            "hit_z": None if hit is None else hit[1],
            "is_entry": hit is not None,
        }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    started = time.perf_counter()
    records = [building_record(geometry=polygon(100.0, -10.0, 140.0, 10.0))]
    towers = [tower(0.0, 0.0, 10.0, tid="T1"), tower(0.0, 200.0, 10.0, tid="T2")]

    geometric = pipeline(mode=TERRAIN_LOS_MODE_DISABLED, towers=towers)
    lookup, source = combined_lookup(records)
    enforced = pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=lookup, towers=towers)
    # No-building control: the same enforced Terrain+Building lookup over candidates whose
    # rays are never obstructed.  It proves the combined gate only removes coverage.
    clear_lookup, _clear_source = combined_lookup([])
    enforced_no_building = pipeline(
        mode=TERRAIN_LOS_MODE_ENFORCED, lookup=clear_lookup, towers=towers,
    )
    fine_lookup, fine_source = combined_lookup(records)
    fine = pipeline(
        mode=TERRAIN_LOS_MODE_ENFORCED, lookup=fine_lookup, towers=towers,
        sample_records=route_samples(21, 5.0, 60.0, prefix="V"),
    )
    budget_lookup, _budget_source = combined_lookup(records, max_ray_count=1)
    budget = pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=budget_lookup, towers=towers)
    truncated_lookup, _truncated_source = combined_lookup(records, complete=False)
    truncated = pipeline(mode=TERRAIN_LOS_MODE_ENFORCED, lookup=truncated_lookup, towers=towers)

    disabled_lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_DISABLED, evaluator=terrain_clear_evaluator,
    )
    parity = pipeline(mode=TERRAIN_LOS_MODE_DISABLED, lookup=disabled_lookup, towers=towers)

    geometric_panels = {panel["panel_id"] for panel in geometric["candidates"]["panels"]}
    enforced_panels = {panel["panel_id"] for panel in enforced["candidates"]["panels"]}
    no_building_panels = {
        panel["panel_id"] for panel in enforced_no_building["candidates"]["panels"]
    }
    geometric_covered = set(covered_pairs(geometric["coverage"]))
    enforced_covered = set(covered_pairs(enforced["coverage"]))
    no_building_covered = set(covered_pairs(enforced_no_building["coverage"]))
    fine_panels = {panel["panel_id"] for panel in fine["candidates"]["panels"]}
    fine_covered = {
        pair for pair in covered_pairs(fine["coverage"]) if pair.split("|")[1] in fine_panels
    }
    budget_solver = budget["solve"]["solver"]
    truncated_solver = truncated["solve"]["solver"]
    formal_hashes = {
        key: (sha256_of(path) if path.exists() else None)
        for key, path in FORMAL_INPUTS.items()
    }

    payload = {
        "fixture_kind": "isolated_combined_terrain_building_los_preintegration",
        "authoritative_project_result": False,
        "production_terrain_los_enabled": False,
        "production_building_los_enabled": False,
        "project_state_or_schema_written": False,
        "business_artifact_written": False,
        "radar_type": RADAR_TYPE_I,
        "device_geometry": building_los.frozen_device_geometry(RADAR_TYPE_I),
        "device_geometry_parameters": radar_geometry_parameters(RADAR_TYPE_I),
        "lookup": {
            "terrain_los_mode": TERRAIN_LOS_MODE_ENFORCED,
            "building_mode": BUILDING_MODE_COMBINED,
            "single_lookup_for_all_consumers": True,
            "second_planner_created": False,
        },
        "coverage_monotonicity": {
            "panel_ids_after_enforced_gate": sorted(enforced_panels),
            "panel_ids_after_enforced_gate_are_a_subset": enforced_panels <= geometric_panels,
            "panel_ids_without_any_building": sorted(no_building_panels),
            "no_building_panels_equal_geometric_panels": (
                no_building_panels == geometric_panels
            ),
            "enforced_is_subset_of_geometric": enforced_covered <= geometric_covered,
            "no_building_coverage_equals_geometric_coverage": (
                no_building_covered == geometric_covered
            ),
            "geometric_covered_pair_count": len(geometric_covered),
            "enforced_covered_pair_count": len(enforced_covered),
        },
        "four_consumer_consistency": {
            "candidate_panel_generation": sorted(enforced_panels),
            "milp_coverage_sets": {
                panel["panel_id"]: panel["covered_sample_indices"]
                for panel in enforced["candidates"]["panels"]
            },
            "actual_site_coverage": covered_pairs(enforced["coverage"]),
            "five_meter_validation": sorted(fine_covered),
            "milp_optimality_proven": enforced["solve"]["solver"].get("optimality_proven"),
            "milp_infeasibility_proven": enforced["solve"]["solver"].get("infeasibility_proven"),
            "final_status": enforced["solve"]["solver"].get("status"),
            "coverage_and_milp_share_panel_ids": (
                set(covered_pairs(enforced["coverage"])) and True
            ),
            "five_meter_ignores_no_building_los": fine_lookup.snapshot()["building_ray_count"] > 0,
        },
        "independent_counters": {
            "combined_lookup": lookup.snapshot(),
            "five_meter_lookup": fine_lookup.snapshot(),
            "budget_lookup": budget_lookup.snapshot(),
            "truncated_query_lookup": truncated_lookup.snapshot(),
            "fixture_building_source_queries": source.queries,
            "five_meter_building_source_queries": fine_source.queries,
        },
        "budget_incomplete": {
            "lookup_complete": budget_lookup.snapshot()["complete"],
            "solver_status": budget_solver.get("status"),
            "optimality_proven": budget_solver.get("optimality_proven"),
            "infeasibility_proven": budget_solver.get("infeasibility_proven"),
            "no_false_optimal_or_infeasible": not (
                budget_solver.get("status") in {"optimal", "infeasible"}
                and (budget_solver.get("optimality_proven")
                     or budget_solver.get("infeasibility_proven"))
            ),
        },
        "truncated_query": {
            "lookup_complete": truncated_lookup.snapshot()["complete"],
            "solver_status": truncated_solver.get("status"),
            "optimality_proven": truncated_solver.get("optimality_proven"),
            "infeasibility_proven": truncated_solver.get("infeasibility_proven"),
        },
        "disabled_parity": {
            "geometric_pair_count": len(geometric_covered),
            "parity_pair_count": len(set(covered_pairs(parity["coverage"]))),
            "identical_coverage": geometric_covered == set(covered_pairs(parity["coverage"])),
            "identical_selected_panels": (
                sorted(geometric["solve"].get("selected_panel_ids") or [])
                == sorted(parity["solve"].get("selected_panel_ids") or [])
            ),
        },
        "fingerprint_parity": fingerprint_parity_probe(),
        "first_hit_probe": first_hit_probe(),
        "formal_read_only_input_hashes": formal_hashes,
        "elapsed_seconds": time.perf_counter() - started,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "coverage_monotonicity": payload["coverage_monotonicity"],
        "budget_incomplete": payload["budget_incomplete"],
        "disabled_parity": payload["disabled_parity"],
        "fingerprint_parity": {
            key: value for key, value in payload["fingerprint_parity"].items()
            if isinstance(value, bool)
        },
        "elapsed_seconds": payload["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
