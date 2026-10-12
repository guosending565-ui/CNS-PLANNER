"""Round34-T07-D2-C1 fixture runner.

Produces ``round34_t07d2c1_fixture_result.json`` from real executions of the isolated
D2-C1 tree: the three independent fixtures (complete/feasible, complete/infeasible,
incomplete evidence), the status/completion orthogonality table, the input-fingerprint vs
execution-identity separation, and the four-lineage downgrade of the public verdict.

Nothing here touches ProjectState, schema or a business artifact; no production entry is
enabled and no full Radar solve is run.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cns_planner.algorithms.radar_layout import milp as milp_module  # noqa: E402
from cns_planner.algorithms.radar_layout.candidates import build_candidates  # noqa: E402
from cns_planner.algorithms.radar_layout.terrain_los_lookup import (  # noqa: E402
    BUILDING_MODE_COMBINED,
    TERRAIN_LOS_MODE_DISABLED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.algorithms.radar_layout.v1 import (  # noqa: E402
    actual_site_coverage,
    solve_layout,
)
from cns_planner.domain import radar_building_los as building_los  # noqa: E402
from cns_planner.domain import radar_visibility_evidence as visibility  # noqa: E402
from cns_planner.domain.radar_surveillance_layout import (  # noqa: E402
    ALGORITHM_VERSION,
    SCHEMA_VERSION,
    SEMANTICS_FINGERPRINT,
    radar_geometry_parameters,
)
from cns_planner.gis import radar_building_los_adapter as building_adapter  # noqa: E402
from tests.test_round34_t07d2c1_los_closeout import (  # noqa: E402
    BLOCKER,
    FINE_COUNT,
    FINE_SPACING_M,
    FINE_START_M,
    PROBE_X,
    FakeBuildingSource,
    building_record,
    polygon,
    sample,
    tower,
)

OUT = REPO / "_work" / "round34_t07d2c1_fixture_result.json"
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


def terrain_clear_evaluator(*, tower, sample, radar_type, geometry):
    return {
        "status": "clear",
        "reason": None,
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete",
        "evidence_completion": "complete",
    }


def terrain_unknown_evaluator(*, tower, sample, radar_type, geometry):
    return {
        "status": "unknown",
        "reason": "terrain_dem_evidence_unknown",
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete",
        "evidence_completion": "incomplete",
    }


def terrain_unknown_for_one_tower(*, tower, sample, radar_type, geometry):
    if str((tower or {}).get("tower_id")) == "T1":
        return terrain_unknown_evaluator(
            tower=tower, sample=sample, radar_type=radar_type, geometry=geometry,
        )
    return terrain_clear_evaluator(
        tower=tower, sample=sample, radar_type=radar_type, geometry=geometry,
    )


def lookup_with(records, *, building_complete=True, terrain_evaluator=terrain_clear_evaluator,
                max_ray_count=4096):
    source = FakeBuildingSource(records, complete=building_complete)

    def building_evaluator(*, tower, sample, radar_type, geometry):
        return building_adapter.evaluate_probe_building_los(
            tower, sample, radar_type=radar_type, building_source=source,
            transform=None, terrain_source=None,
        )

    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_evaluator,
        building_evaluator=building_evaluator, building_mode=BUILDING_MODE_COMBINED,
        source_identity={"dem": "d2c1-fixture"},
        building_source_identity={"buildings": "d2c1-fixture"},
        max_ray_count=max_ray_count,
    )
    return lookup, source


def layout(radar_type):
    towers = [tower(0.0, 0.0, 10.0, tid="T1"), tower(0.0, 200.0, 10.0, tid="T2")]
    probes = [sample(x, 0.0, 10.0, sid=f"S{index}") for index, x in enumerate(PROBE_X)]
    records = [building_record(
        geometry=polygon(BLOCKER["x0"], BLOCKER["y0"], BLOCKER["x1"], BLOCKER["y1"]),
        height=BLOCKER["height"], bid="BLOCKER",
    )]
    return towers, probes, records


def run_pipeline(*, towers, probes, lookup, mode=TERRAIN_LOS_MODE_ENFORCED,
                 required=1, radar_types=("radar_i",)):
    candidates = build_candidates(
        towers=towers, samples=probes, terrain_los_mode=mode, terrain_los_lookup=lookup,
    )
    coverage_is_complete = bool(candidates.get("coverage_semantics_complete", True))
    solve = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[required] * len(candidates["samples"]),
        radar_types=list(radar_types), objectives=("total_panel_count",),
        stage="d2c1_fixture", options={"solver_time_limit_s": 10.0},
        coverage_is_complete=coverage_is_complete,
    )
    coverage = actual_site_coverage(
        panels=candidates["panels"], samples=candidates["samples"],
        selected_panel_ids=solve.get("selected_panel_ids") or [],
        tower_records=candidates["towers"], terrain_los_mode=mode,
        terrain_los_lookup=lookup,
    )
    return candidates, solve, coverage


def milp_sets(candidates, selected_ids):
    selected = set(selected_ids or [])
    return {
        panel["panel_id"]: sorted(int(index) for index in panel["covered_sample_indices"])
        for panel in candidates["panels"] if panel["panel_id"] in selected
    }


def final_sets(candidates, coverage):
    index_by_id = {item["sample_id"]: item["sample_index"] for item in candidates["samples"]}
    result = {}
    for item in coverage:
        for panel in item["panels"]:
            result.setdefault(panel["panel_id"], []).append(index_by_id[item["sample_id"]])
    return {panel_id: sorted(indices) for panel_id, indices in result.items()}


def solver_view(solve):
    solver = solve["solver"]
    return {
        "status": solver.get("status"),
        "milp_raw_status": solver.get("milp_raw_status"),
        "optimality_proven": solver.get("optimality_proven"),
        "infeasibility_proven": solver.get("infeasibility_proven"),
        "coverage_semantics_complete": solver.get("coverage_semantics_complete"),
        "proven_against_complete_physical_scenario": solver.get(
            "proven_against_complete_physical_scenario"
        ),
        "proof_withheld_reason": solver.get("proof_withheld_reason"),
        "downgraded_from": solver.get("downgraded_from"),
        "downgrade_reason": solver.get("downgrade_reason"),
        "raw_status": solver.get("raw_status"),
        "selected_panel_ids": list(solve.get("selected_panel_ids") or []),
    }


def lookup_view(lookup):
    snapshot = lookup.snapshot()
    return {
        key: snapshot.get(key) for key in (
            "mode", "building_mode", "complete", "incomplete_reason",
            "combined_status_counts", "status_counts", "los_ray_count",
            "building_ray_count", "building_candidate_count",
            "building_exact_intersection_count", "building_gate_filtered_count",
            "building_gate_unresolved_count", "cache_hit_count", "cache_miss_count",
            "terrain_sample_count", "combination_count", "max_ray_count",
        )
    }


def coverage_view(candidates, coverage):
    return {
        "sample_ids": [item["sample_id"] for item in candidates["samples"]],
        "status_by_sample": {
            item["sample_id"]: item["status"] for item in coverage
        },
        "panels_by_sample": {
            item["sample_id"]: [panel["panel_id"] for panel in item["panels"]]
            for item in coverage
        },
        "covered_sample_ids": [
            item["sample_id"] for item in coverage if item["panels"]
        ],
        "uncovered_sample_ids": [
            item["sample_id"] for item in coverage if not item["panels"]
        ],
    }


def build_fixture_a():
    towers, probes, records = layout("radar_i")
    lookup, source = lookup_with(records)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    selected = solve.get("selected_panel_ids") or []
    milp_by_panel = milp_sets(candidates, selected)
    final_by_panel = final_sets(candidates, coverage)
    shared = sorted(set(milp_by_panel) & set(final_by_panel))
    differences = {
        panel_id: {
            "milp": milp_by_panel.get(panel_id),
            "final_coverage": final_by_panel.get(panel_id),
        }
        for panel_id in sorted(set(milp_by_panel) | set(final_by_panel))
        if milp_by_panel.get(panel_id) != final_by_panel.get(panel_id)
    }
    panels = {
        panel["panel_id"]: {
            "tower_id": panel["tower_id"],
            "radar_type": panel["radar_type"],
            "azimuth_deg": panel["azimuth_deg"],
            "covered_sample_indices": sorted(panel["covered_sample_indices"]),
        }
        for panel in candidates["panels"]
    }
    return {
        "purpose": "complete evidence, genuinely feasible combined layout",
        "geometry": {
            "blocker": dict(BLOCKER),
            "probe_x_m": list(PROBE_X),
            "probe_evidence": "deterministic egm2008_m = 10.0, required = 1 site",
            "towers": [
                {"tower_id": t["tower_id"], "metric": t["metric"]} for t in towers
            ],
        },
        "candidate_panels": panels,
        "candidate_panel_count": len(panels),
        "lookup": lookup_view(lookup),
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "solver": solver_view(solve),
        "coverage": coverage_view(candidates, coverage),
        "selected_panel_ids": list(selected),
        "selected_panel_count": len(selected),
        "milp_covered_sample_indices_by_panel": milp_by_panel,
        "final_coverage_sample_indices_by_panel": final_by_panel,
        "compared_panel_ids": shared,
        "compared_panel_count": len(shared),
        "compared_sample_index_count": sum(len(v) for v in milp_by_panel.values()),
        "per_panel_sample_set_differences": differences,
        "milp_and_final_coverage_identical": not differences and bool(milp_by_panel),
        "empty_set_assertions_used": False,
        "building_source_queries": source.queries,
    }


def build_fixture_a_five_meter():
    towers, _probes, records = layout("radar_i")
    grid = [
        sample(FINE_START_M + index * FINE_SPACING_M, 0.0, 10.0, sid=f"V{index:03d}")
        for index in range(FINE_COUNT)
    ]
    lookup, source = lookup_with(records)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=grid, lookup=lookup,
    )
    selected = solve.get("selected_panel_ids") or []
    milp_by_panel = milp_sets(candidates, selected)
    final_by_panel = final_sets(candidates, coverage)
    return {
        "purpose": "5 m validation grid reuses the same combined LOS semantics",
        "grid": {
            "start_m": FINE_START_M, "spacing_m": FINE_SPACING_M, "count": FINE_COUNT,
            "sample_ids": [item["sample_id"] for item in candidates["samples"]],
        },
        "lookup": lookup_view(lookup),
        "building_source_queries": source.queries,
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "solver": solver_view(solve),
        "selected_panel_ids": list(selected),
        "milp_covered_sample_indices_by_panel": milp_by_panel,
        "final_coverage_sample_indices_by_panel": final_by_panel,
        "per_panel_sample_set_differences": {
            panel_id: {
                "milp": milp_by_panel.get(panel_id),
                "final_coverage": final_by_panel.get(panel_id),
            }
            for panel_id in sorted(set(milp_by_panel) | set(final_by_panel))
            if milp_by_panel.get(panel_id) != final_by_panel.get(panel_id)
        },
        "five_meter_ran_the_same_combined_los": (
            lookup.snapshot()["building_ray_count"] > 0 and source.queries > 0
        ),
    }


def build_fixture_b():
    towers, probes, records = layout("radar_i")
    lookup, _source = lookup_with(records)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup, required=2,
    )
    presolve = milp_module.presolve_infeasibility(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[2] * len(candidates["samples"]), radar_types=["radar_i"],
    )
    return {
        "purpose": "complete evidence, genuinely infeasible without unknown or budget cut",
        "required_distinct_site_count": 2,
        "lookup": lookup_view(lookup),
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "solver": solver_view(solve),
        "coverage": coverage_view(candidates, coverage),
        "presolve_insufficient_sample_count": presolve["insufficient_sample_count"],
        "presolve_insufficient_sample_ids": [
            item["sample_id"] for item in presolve["insufficient_samples"]
        ],
        "presolve_candidate_distinct_site_counts": presolve[
            "candidate_distinct_site_count"
        ],
        "unknown_or_budget_used_to_fabricate_infeasibility": False,
    }


def build_fixture_c():
    towers, probes, records = layout("radar_i")
    variants = {}

    lookup, _source = lookup_with(records, max_ray_count=0)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    variants["budget_exhausted"] = {
        "lookup": lookup_view(lookup),
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "solver": solver_view(solve),
        "coverage": coverage_view(candidates, coverage),
    }

    lookup, _source = lookup_with(records, building_complete=False)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    variants["building_query_truncated"] = {
        "lookup": lookup_view(lookup),
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "candidate_panel_count": len(candidates["panels"]),
        "solver": solver_view(solve),
        "coverage": coverage_view(candidates, coverage),
    }

    lookup, _source = lookup_with(
        records, terrain_evaluator=terrain_unknown_for_one_tower,
    )
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    variants["terrain_physical_unknown"] = {
        "lookup": lookup_view(lookup),
        "coverage_semantics_complete": candidates["coverage_semantics_complete"],
        "solver": solver_view(solve),
        "coverage": coverage_view(candidates, coverage),
    }
    for name, view in variants.items():
        solver = view["solver"]
        view["outward_infeasibility_proven"] = solver["infeasibility_proven"]
        view["outward_optimality_proven"] = solver["optimality_proven"]
        view["radar_ii_auto_escalation"] = False
        view["published_as_formal_coverage_conclusion"] = False

    lookup, _source = lookup_with(records, max_ray_count=0)
    layout_result = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED, terrain_los_lookup=lookup,
    )
    return {
        "purpose": "unknown or incomplete evidence must never look like a physical proof",
        "variants": variants,
        "solve_layout_public_verdict": {
            "status": layout_result["status"],
            "coverage_semantics_complete": layout_result["coverage_semantics_complete"],
            "solver": solver_view({"solver": layout_result["solver"]}),
            "gap_reason": layout_result["gap_reason"],
            "gap_classification": layout_result["gap_classification"],
            "managed_physical_gap": layout_result["managed_physical_gap"],
            "allowed_radar_types": layout_result["allowed_radar_types"],
            "automatic_radar_ii_escalation": layout_result[
                "automatic_radar_ii_escalation"
            ],
            "selected_panel_count": layout_result["selected_panel_count"],
            "terrain_los_evidence": layout_result.get("terrain_los_evidence"),
        },
    }


def build_orthogonality_table():
    cases = [
        {
            "terrain": "clear/evaluated/complete",
            "building": "clear/evaluated/complete",
            "terrain_input": {"status": "clear", "computation_completion": "complete",
                              "evidence_completion": "complete"},
            "building_input": {"status": "clear", "computation_completion": "complete",
                               "evidence_completion": "complete"},
        },
        {
            "terrain": "not_evaluated",
            "building": "credible blocked",
            "terrain_input": None,
            "building_input": {"status": "blocked", "computation_completion": "complete",
                               "evidence_completion": "complete"},
        },
        {
            "terrain": "unknown/evaluated/incomplete",
            "building": "credible blocked",
            "terrain_input": {"status": "unknown", "computation_completion": "incomplete",
                              "evidence_completion": "incomplete"},
            "building_input": {"status": "blocked", "computation_completion": "complete",
                               "evidence_completion": "complete"},
        },
        {
            "terrain": "clear/evaluated/complete",
            "building": "predicted-only unknown",
            "terrain_input": {"status": "clear", "computation_completion": "complete",
                              "evidence_completion": "complete"},
            "building_input": {"status": "unknown",
                               "engineering_estimate_status": "blocked",
                               "computation_completion": "complete",
                               "evidence_completion": "incomplete"},
        },
        {
            "terrain": "not_evaluated",
            "building": "geometry filtered",
            "terrain_input": None,
            "building_input": {"status": "geometry_filtered",
                               "computation_completion": "not_evaluated",
                               "evidence_completion": "not_evaluated"},
        },
        {
            "terrain": "incomplete/budget exhausted",
            "building": "clear/evaluated/complete",
            "terrain_input": {"status": "unknown", "computation_completion": "incomplete",
                              "evidence_completion": "incomplete"},
            "building_input": {"status": "clear", "computation_completion": "complete",
                               "evidence_completion": "complete"},
        },
    ]
    rows = []
    for case in cases:
        combined = visibility.combine_visibility(
            case["terrain_input"], case["building_input"],
        )
        rows.append({
            "terrain": case["terrain"],
            "building": case["building"],
            "combined_visibility": combined["combined_visibility"],
            "computation_completion": combined["computation_completion"],
            "evidence_completion": combined["evidence_completion"],
            "search_incomplete": combined["search_incomplete"],
            "domains_not_evaluated": combined["domains_not_evaluated"],
            "building_los_physical_unknown": combined["building_los_physical_unknown"],
            "credible_block_is_sufficient_for_direct_ray_block": combined[
                "credible_block_is_sufficient_for_direct_ray_block"
            ],
            "verdict_is_orthogonal_to_completion": combined[
                "verdict_is_orthogonal_to_completion"
            ],
        })
    return rows


def fingerprint_inputs(**overrides):
    values = {
        "terrain_input_fingerprint": "terrain-fp",
        "building_input_fingerprint": "building-fp",
        "route_probe_identity": {"probe_id": "P1"},
        "radar_origin": {"tower_id": "T1"},
        "radar_geometry": {"version": "frozen-radar-geometry-v1_1"},
        "terrain_source_identity": {"file_name": "d.tif", "mtime_ns": 1},
        "building_source_identity": {"file_name": "b.gpkg", "mtime_ns": 1},
        "crs": {"horizontal": "EPSG:32651", "vertical": "egm2008_orthometric"},
        "ray_model": {"id": building_los.RAY_MODEL_ID, "k_factor": 1.0},
    }
    values.update(overrides)
    return values


def build_fingerprint_audit():
    inputs = fingerprint_inputs()
    base = visibility.combined_visibility_input_fingerprint(**inputs)
    legacy_a = visibility.combined_visibility_fingerprint(
        **inputs, budgets={"max_rays": 8}, completion={"state": "incomplete"},
        building_execution_identity={"elapsed_seconds": 9.5, "cache_hit_count": 0},
    )
    legacy_b = visibility.combined_visibility_fingerprint(
        **inputs, budgets={"max_rays": 4096}, completion={"state": "complete"},
        building_execution_identity={"elapsed_seconds": 0.01, "cache_hit_count": 7},
    )
    semantic_changes = {
        key: visibility.combined_visibility_input_fingerprint(
            **fingerprint_inputs(**{key: value})
        ) != base
        for key, value in (
            ("building_source_identity", {"file_name": "b.gpkg", "mtime_ns": 2}),
            ("terrain_source_identity", {"file_name": "d.tif", "mtime_ns": 3}),
            ("radar_geometry", {"version": "frozen-radar-geometry-v1_2"}),
            ("ray_model", {"id": building_los.RAY_MODEL_ID, "k_factor": 4.0 / 3.0}),
            ("crs", {"horizontal": "EPSG:32652", "vertical": "egm2008_orthometric"}),
            ("route_probe_identity", {"probe_id": "P2"}),
        )
    }
    complete = visibility.combined_visibility_execution_identity(
        input_fingerprint=base, budgets={"max_rays": 4096},
        completion={"state": "complete"}, search_incomplete=False,
        elapsed_seconds=0.01, cache_hit_count=7,
    )
    incomplete = visibility.combined_visibility_execution_identity(
        input_fingerprint=base, budgets={"max_rays": 1},
        completion={"state": "incomplete"}, search_incomplete=True,
        incomplete_reason="los_computation_budget_exhausted",
        elapsed_seconds=0.02, cache_hit_count=7,
    )
    return {
        "input_fingerprint": base,
        "input_fingerprint_version": visibility.RADAR_COMBINED_FINGERPRINT_VERSION,
        "execution_fingerprint_version": (
            visibility.RADAR_COMBINED_EXECUTION_FINGERPRINT_VERSION
        ),
        "combined_visibility_version": visibility.RADAR_COMBINED_VISIBILITY_VERSION,
        "same_inputs_same_fingerprint": base == visibility.combined_visibility_input_fingerprint(
            **inputs
        ),
        "different_elapsed_same_input_fingerprint": legacy_a == base,
        "different_cache_hits_same_input_fingerprint": legacy_b == base,
        "execution_receipts_excluded_from_input_fingerprint": legacy_a == legacy_b == base,
        "completion_change_changes_execution_identity_only": complete != incomplete,
        "execution_identity_input_fingerprint_unchanged": (
            complete["input_fingerprint"] == incomplete["input_fingerprint"] == base
        ),
        "semantic_change_detected": semantic_changes,
        "all_semantic_changes_detected": all(semantic_changes.values()),
        "complete_execution_identity": complete,
        "incomplete_execution_identity": incomplete,
        "old_version_compatibility": visibility.combined_visibility_evidence_compatibility(
            "radar_combined_visibility_v1", "old-fingerprint",
        ),
        "current_version_compatibility": visibility.combined_visibility_evidence_compatibility(
            visibility.RADAR_COMBINED_VISIBILITY_VERSION, base,
        ),
    }


def build_parity():
    towers, probes, _records = layout("radar_i")
    baseline = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode=TERRAIN_LOS_MODE_DISABLED, allow_mixed=False,
    )
    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_DISABLED, evaluator=terrain_clear_evaluator,
        building_mode="disabled",
    )
    parity = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode=TERRAIN_LOS_MODE_DISABLED, terrain_los_lookup=lookup,
        allow_mixed=False,
    )
    parameters = radar_geometry_parameters("radar_i")
    return {
        "identical_status": baseline["status"] == parity["status"],
        "identical_selected_panels": baseline["selected_panels"] == parity["selected_panels"],
        "identical_validation": baseline["validation"] == parity["validation"],
        "terrain_los_evidence_absent": "terrain_los_evidence" not in parity,
        "coverage_semantics_complete": parity["coverage_semantics_complete"],
        "official_radar_1_3": {
            "algorithm_version": ALGORITHM_VERSION,
            "schema_version": SCHEMA_VERSION,
            "geometry_version": SEMANTICS_FINGERPRINT.get("geometry_version"),
            "frozen_radar_geometry_version": building_los.FROZEN_RADAR_GEOMETRY_VERSION,
            "algorithm_version_building": building_los.RADAR_BUILDING_LOS_ALGORITHM_VERSION,
            "parameters": parameters,
        },
    }


def build_read_only():
    hashes = {}
    for key, path in FORMAL_INPUTS.items():
        if not path.is_file():
            hashes[key] = {"present": False}
            continue
        hashes[key] = {
            "present": True, "sha256": sha256_of(path),
            "expected": {
                "project_state": "92a0000f56d5ad1dcb5d97c8344d427d68b4658b4b563cc4bf27f6e35d449d07",
                "buildings": "4f1657495d8422ac0d23973949d4bcbca59dcf07d59ed380ff9877afc7391b97",
                "terrain": "21406bd901fbebae211528a158ca95eef899e2074322625717b215b13d8799b8",
            }[key],
        }
        hashes[key]["matches_recorded"] = (
            hashes[key]["sha256"] == hashes[key]["expected"]
        )
    return {
        "formal_inputs": hashes,
        "all_read_only_inputs_match_recorded": all(
            item.get("matches_recorded", True) for item in hashes.values()
        ),
        "project_state_or_schema_written": False,
        "business_artifact_written": False,
        "production_terrain_los_enabled": False,
        "production_building_los_enabled": False,
        "authoritative_project_result": False,
    }


def main():
    started = time.perf_counter()
    payload = {
        "fixture_kind": "round34_t07d2c1_los_evidence_consistency_and_solver_proof_closeout",
        "authoritative_project_result": False,
        "production_terrain_los_enabled": False,
        "production_building_los_enabled": False,
        "project_state_or_schema_written": False,
        "business_artifact_written": False,
        "main_workspace_modified": False,
        "fixture_a_complete_evidence_feasible": build_fixture_a(),
        "fixture_a_five_meter_validation": build_fixture_a_five_meter(),
        "fixture_b_complete_evidence_infeasible": build_fixture_b(),
        "fixture_c_incomplete_evidence": build_fixture_c(),
        "status_and_completion_orthogonality": build_orthogonality_table(),
        "fingerprint_separation": build_fingerprint_audit(),
        "disabled_and_radar_1_3_parity": build_parity(),
        "read_only": build_read_only(),
    }
    payload["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8", newline="",
    )
    raw = OUT.read_bytes()
    print("wrote", OUT)
    print("bytes", len(raw))
    print("sha256", hashlib.sha256(raw).hexdigest())
    print("bom", raw[:3] == b"\xef\xbb\xbf")
    print("fixture_a identical", payload["fixture_a_complete_evidence_feasible"][
        "milp_and_final_coverage_identical"
    ], "selected", payload["fixture_a_complete_evidence_feasible"]["selected_panel_ids"])
    print("fixture_b infeasibility_proven", payload[
        "fixture_b_complete_evidence_infeasible"
    ]["solver"]["infeasibility_proven"])
    for name, view in payload["fixture_c_incomplete_evidence"]["variants"].items():
        print("fixture_c", name, view["lookup"]["incomplete_reason"],
              "proven", view["outward_infeasibility_proven"],
              view["outward_optimality_proven"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
