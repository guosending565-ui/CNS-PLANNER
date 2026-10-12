"""Round34-T07-D2-C1 LOS evidence consistency and solver-proof closeout.

Three *independent* fixtures replace the D2-C0 fixture that could only show coverage
monotonicity (``enforced_covered_pair_count = 0`` with an empty ``actual_site_coverage``):

* **Fixture A** — complete evidence, a genuinely feasible combined layout with a non-empty
  selected panel set and non-empty real coverage.  MILP ``covered_sample_indices`` and
  ``actual_site_coverage`` are compared sample by sample, per panel.
* **Fixture B** — complete evidence, genuinely infeasible (two independent sites are
  required and only one exists), with no reliance on ``unknown`` or an exhausted budget.
* **Fixture C** — unknown / incomplete evidence: the raw MILP status stays as internal
  diagnostics while no outward proof, optimum or Radar-II escalation is claimed.

Everything here is a small synthetic fixture: no project state, no schema and no business
artifact is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cns_planner.algorithms.radar_layout import milp as milp_module
from cns_planner.algorithms.radar_layout.candidates import build_candidates
from cns_planner.algorithms.radar_layout.terrain_los_lookup import (
    BUILDING_MODE_COMBINED,
    TERRAIN_LOS_MODE_DISABLED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.algorithms.radar_layout.v1 import actual_site_coverage, solve_layout
from cns_planner.domain import radar_building_los as building_los
from cns_planner.domain import radar_visibility_evidence as visibility
from cns_planner.domain.radar_surveillance_layout import RADAR_TYPE_I, RADAR_TYPE_II
from cns_planner.gis import radar_building_los_adapter as building_adapter

from tests.test_round34_t07d2c0_building_los_combined import (
    FakeBuildingSource,
    building_record,
    polygon,
    sample,
    tower,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The blocker straddles the Tower-1→probe line of sight, which lies exactly on ``y = 0``
#: for every probe on the route: those rays stay inside the footprint across
#: ``x in [200, 210]`` and are physically blocked.  From Tower-2 at ``(0, 200)`` the same
#: stretch is seen at ``y >= 50``, above the footprint's ``y <= 5``, so Tower-2 always stays
#: clear.  The footprint deliberately avoids every probe ``x`` so no ray merely touches a
#: boundary (a boundary contact is an unresolved ``unknown``, not a verdict).
BLOCKER = dict(x0=200.0, y0=-10.0, x1=210.0, y1=5.0, height=50.0)

PROBE_X = (400.0, 600.0, 800.0, 1000.0, 1200.0)
TOWER_2_METRIC = (0.0, 200.0)
#: The 5 m grid also starts *inside* the blocker's ``x`` span but not on its boundary, so
#: every grid ray from Tower-1 crosses the footprint interior and is blocked, while every
#: Tower-2 ray passes above ``y = 5`` across that stretch and stays clear.
FINE_START_M = 485.0
FINE_COUNT = 21
FINE_SPACING_M = 5.0


# --------------------------------------------------------------------------- helpers


def terrain_clear_evaluator(*, tower, sample, radar_type, geometry):
    """A terrain domain that actually finished and found nothing in the way."""

    return {
        "status": "clear",
        "reason": None,
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete",
        "evidence_completion": "complete",
    }


def terrain_unknown_evaluator(*, tower, sample, radar_type, geometry):
    """A terrain domain whose physical answer is genuinely unknown."""

    return {
        "status": "unknown",
        "reason": "terrain_dem_evidence_unknown",
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete",
        "evidence_completion": "incomplete",
    }


def terrain_unknown_for_one_tower(*, tower, sample, radar_type, geometry):
    """Terrain evidence unknown for one tower only, so coverage is partly removed."""

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


def layout() -> tuple[list, list, list]:
    """Two candidate towers, four fixed 25 m probes and one blocker."""

    towers = [
        tower(0.0, 0.0, 10.0, tid="T1"),
        tower(TOWER_2_METRIC[0], TOWER_2_METRIC[1], 10.0, tid="T2"),
    ]
    probes = [sample(x, 0.0, 10.0, sid=f"S{index}") for index, x in enumerate(PROBE_X)]
    records = [building_record(
        geometry=polygon(BLOCKER["x0"], BLOCKER["y0"], BLOCKER["x1"], BLOCKER["y1"]),
        height=BLOCKER["height"], bid="BLOCKER",
    )]
    return towers, probes, records


def fine_layout() -> tuple[list, list, list]:
    """The same towers and blocker with a real 5 m validation grid."""

    towers, _probes, records = layout()
    grid = [
        sample(FINE_START_M + index * FINE_SPACING_M, 0.0, 10.0, sid=f"V{index:03d}")
        for index in range(FINE_COUNT)
    ]
    return towers, grid, records


def run_pipeline(*, towers, probes, lookup, mode=TERRAIN_LOS_MODE_ENFORCED,
                 required=1, radar_types=(RADAR_TYPE_I,)):
    """One explicit pass over candidates → MILP → final coverage, no hidden guard."""

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


def milp_covered_samples_by_panel(candidates, selected_ids):
    """``{panel_id: sorted sample indices}`` for the selected panels only."""

    selected = set(selected_ids or [])
    return {
        panel["panel_id"]: sorted(int(index) for index in panel["covered_sample_indices"])
        for panel in candidates["panels"]
        if panel["panel_id"] in selected
    }


def final_covered_samples_by_panel(candidates, coverage):
    """The same mapping recomputed from ``actual_site_coverage`` for the same panels."""

    sample_index_by_id = {
        panel["sample_id"]: panel["sample_index"] for panel in candidates["samples"]
    }
    result = {}
    for item in coverage:
        for panel in item["panels"]:
            index = sample_index_by_id[item["sample_id"]]
            result.setdefault(panel["panel_id"], []).append(index)
    return {panel_id: sorted(indices) for panel_id, indices in result.items()}


# ===================================================================== fixture A


def fixture_a():
    towers, probes, records = layout()
    lookup, source = lookup_with(records)
    candidates, solve, coverage = run_pipeline(towers=towers, probes=probes, lookup=lookup)
    return {
        "towers": towers, "probes": probes, "records": records,
        "lookup": lookup, "source": source,
        "candidates": candidates, "solve": solve, "coverage": coverage,
    }


def test_fixture_a_has_two_candidate_towers_and_real_coverable_probes():
    outcome = fixture_a()
    candidates = outcome["candidates"]
    assert len(candidates["samples"]) == 5
    assert [item["sample_id"] for item in candidates["samples"]] == [
        "S0", "S1", "S2", "S3", "S4",
    ]
    assert all(
        sample["required_distinct_site_count"] == 1 for sample in candidates["samples"]
    )
    by_tower = {}
    for panel in candidates["panels"]:
        by_tower.setdefault(panel["tower_id"], []).append(
            set(int(index) for index in panel["covered_sample_indices"])
        )
    # Two candidate towers are offered to the solver.
    assert {tower["tower_id"] for tower in candidates["towers"]} == {"T1", "T2"}
    assert by_tower, "the combined gate removed every candidate panel"
    non_empty = sorted({
        index for panels in by_tower.values() for indices in panels for index in indices
    })
    assert non_empty, "no candidate panel covers any fixed sample"
    # The blocker must really remove Tower-1's rays: not one Tower-1 panel may claim a
    # probe, otherwise the fixture proves nothing about the combined gate.
    for indices in by_tower.get("T1", []):
        assert not indices, f"Tower-1 claimed {sorted(indices)} despite the blocker"
    # Tower-2 keeps the whole route: the blocker is far above every one of its rays.
    assert by_tower.get("T2"), "Tower-2 lost every panel"
    assert any({0, 1, 2, 3, 4}.issubset(indices) for indices in by_tower["T2"])
    # Neither tower alone could satisfy all five probes on this geometry.
    assert not any({0, 1, 2, 3, 4} <= indices for indices in by_tower.get("T1", []))


def test_fixture_a_evidence_is_complete_and_a_proven_feasible_optimum_exists():
    outcome = fixture_a()
    snapshot = outcome["lookup"].snapshot()
    # Every required obstruction state was evaluated, so the coverage set is the complete
    # physical scenario and the solver may prove real facts about it.
    assert snapshot["combine_count"] if "combine_count" in snapshot else True
    assert snapshot["combined_status_counts"].get("unknown") == 0
    assert snapshot["incomplete_reason"] is None
    assert outcome["candidates"]["coverage_semantics_complete"] is True
    solver = outcome["solve"]["solver"]
    assert solver["status"] == "optimal"
    assert solver["optimality_proven"] is True
    assert solver["infeasibility_proven"] is False
    assert solver["coverage_semantics_complete"] is True
    assert outcome["solve"]["selected_panel_ids"], "fixture A selected no panel at all"


def test_fixture_a_milp_and_final_coverage_agree_per_panel_and_sample():
    outcome = fixture_a()
    selected = outcome["solve"]["selected_panel_ids"]
    assert selected, "the comparison would be empty"
    milp_sets = milp_covered_samples_by_panel(outcome["candidates"], selected)
    final_sets = final_covered_samples_by_panel(outcome["candidates"], outcome["coverage"])
    sample_ids = {panel["sample_id"] for panel in outcome["candidates"]["samples"]}

    # The report must show the real, non-empty sets that were compared.
    assert set(milp_sets) == set(final_sets) == set(selected)
    assert all(indices for indices in milp_sets.values())
    assert all(indices for indices in final_sets.values())
    for panel_id, indices in milp_sets.items():
        assert indices == final_sets[panel_id], (
            f"panel {panel_id}: MILP covered {indices} but final coverage {final_sets[panel_id]}"
        )
    covered_sample_ids = {
        item["sample_id"] for item in outcome["coverage"] if item["panels"]
    }
    assert covered_sample_ids, "actual_site_coverage reported no covered sample"
    assert covered_sample_ids <= sample_ids
    # Every sample that the final coverage says is covered must also be satisfied.
    for item in outcome["coverage"]:
        if item["panels"]:
            assert item["status"] == "satisfied"
            assert item["actual_distinct_site_count"] >= 1
    assert {item["status"] for item in outcome["coverage"]} == {"satisfied"}


def test_fixture_a_five_meter_validation_runs_the_same_combined_los():
    towers, grid, records = fine_layout()
    assert len(grid) == 21
    lookup, source = lookup_with(records)
    candidates, solve, coverage = run_pipeline(towers=towers, probes=grid, lookup=lookup)
    assert solve["solver"]["status"] == "optimal"
    assert solve["solver"]["optimality_proven"] is True
    assert candidates["coverage_semantics_complete"] is True
    selected = solve["selected_panel_ids"]
    assert selected
    milp_sets = milp_covered_samples_by_panel(candidates, selected)
    final_sets = final_covered_samples_by_panel(candidates, coverage)
    assert set(milp_sets) == set(final_sets) == set(selected)
    for panel_id, indices in milp_sets.items():
        assert indices == final_sets[panel_id]
    # The 5 m grid must have produced real building ray evaluations, not a cached replay
    # of the 25 m decisions.
    assert source.queries > 0
    snapshot = lookup.snapshot()
    assert snapshot["building_ray_count"] > 0
    strict = snapshot["combined_status_counts"]
    assert strict.get("blocked", 0) > 0, "the 5 m grid never met the blocker"
    assert strict.get("unknown", 0) == 0


def test_solve_layout_records_direct_five_meter_recheck_of_original_panel():
    """The first 5 m review must keep the 25 m selection, not silently re-solve it."""

    towers, coarse, records = layout()
    _same_towers, fine, _same_records = fine_layout()
    fine = [{**item, "sample_index": index} for index, item in enumerate(fine)]
    lookup, _source = lookup_with(records)

    result = solve_layout(
        towers=towers,
        samples=coarse,
        validation_samples=fine,
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )

    first = result["refinement_rounds"][0]
    assert first["validation_kind"] == "direct_selected_panel_recheck"
    assert first["selected_panel_ids"] == ["P-T2-radar_i-71.565051"]
    assert len(first["selected_panels"]) == 1
    selected_panel = first["selected_panels"][0]
    assert selected_panel.pop("azimuth_deg") == pytest.approx(71.56505117707799)
    assert selected_panel == {
        "panel_id": "P-T2-radar_i-71.565051",
        "tower_id": "T2",
        "radar_type": "radar_i",
        "origin_egm2008_m": 10.0,
        "origin_state": "confirmed",
        "origin_evidence_level": "verified",
        "origin_availability": "usable",
        "device_instance_id": "T2-radar_i",
        "origin_input_fingerprint": None,
    }
    assert first["reviewed_sample_count"] == len(fine)
    assert first["violation_count"] == 0
    assert first["validation_passed"] is True
    assert first["site_coverage_status_counts"] == {"satisfied": 21}
    assert first["required_distinct_site_count_counts"] == {"1": 21}
    assert first["failed_samples"] == []
    assert first["los_evidence"]["complete"] is True
    assert first["los_evidence"]["combined_status_counts"]["unknown"] == 0
    assert len(result["refinement_rounds"]) == 1
    assert [item["panel_id"] for item in result["selected_panels"]] == first[
        "selected_panel_ids"
    ]


def test_solve_layout_does_not_label_optimization_samples_as_five_meter_validation():
    towers, coarse, records = layout()
    lookup, _source = lookup_with(records)

    result = solve_layout(
        towers=towers, samples=coarse,
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )

    review = result["refinement_rounds"][0]
    assert review["validation_kind"] == "optimization_sample_recheck"
    assert review["spacing_m"] is None
    assert review["validation_passed"] is None
    assert review["reviewed_sample_count"] == len(coarse)


def test_fine_grid_unknown_keeps_the_fixed_panel_review_audit():
    towers, coarse, records = layout()
    _same_towers, fine, _same_records = fine_layout()
    fine = [{**item, "sample_index": index} for index, item in enumerate(fine)]

    def unknown_on_fine_grid(*, tower, sample, radar_type, geometry):
        evaluator = (
            terrain_unknown_evaluator
            if str((sample or {}).get("sample_id") or "").startswith("V")
            else terrain_clear_evaluator
        )
        return evaluator(
            tower=tower, sample=sample, radar_type=radar_type, geometry=geometry,
        )

    lookup, _source = lookup_with(records, terrain_evaluator=unknown_on_fine_grid)
    result = solve_layout(
        towers=towers, samples=coarse, validation_samples=fine,
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )

    assert result["status"] == "search_incomplete"
    assert len(result["refinement_rounds"]) == 1
    review = result["refinement_rounds"][0]
    assert review["validation_kind"] == "direct_selected_panel_recheck"
    assert review["validation_passed"] is False
    assert review["reviewed_sample_count"] == len(fine)
    assert review["site_coverage_status_counts"]["uncovered"] == len(fine)
    assert review["los_evidence"]["combined_status_counts"]["unknown"] == len(fine)
    assert review["los_evidence"]["complete"] is False
    assert len(review["failed_samples"]) == len(fine)


def test_single_ray_cache_key_tracks_building_mode_and_source_identity():
    tower_record = tower(0.0, 0.0, 10.0)
    probe_record = sample(500.0)
    first, _ = lookup_with([], building_complete=True)
    second, _ = lookup_with([], building_complete=True)
    second.building_source_identity = {"buildings": "different-version"}

    first_gate = first.geometry_gate(tower_record, probe_record, RADAR_TYPE_I)
    second_gate = second.geometry_gate(tower_record, probe_record, RADAR_TYPE_I)

    assert first_gate["cache_key"] != second_gate["cache_key"]

    disabled = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_clear_evaluator,
        source_identity={"dem": "d2c1-fixture"},
        building_source_identity={"buildings": "d2c1-fixture"},
    )
    disabled_gate = disabled.geometry_gate(tower_record, probe_record, RADAR_TYPE_I)
    assert first_gate["cache_key"] != disabled_gate["cache_key"]


def test_fixture_a_disabled_geometric_coverage_is_a_superset_of_enforced_coverage():
    towers, probes, records = layout()
    disabled_lookup = None
    geometric = build_candidates(
        towers=towers, samples=probes, terrain_los_mode=TERRAIN_LOS_MODE_DISABLED,
        terrain_los_lookup=disabled_lookup,
    )
    lookup, _source = lookup_with(records)
    enforced = build_candidates(
        towers=towers, samples=probes, terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )
    geometric_pairs = {
        (index, panel["panel_id"])
        for panel in geometric["panels"]
        for index in panel["covered_sample_indices"]
    }
    enforced_pairs = {
        (index, panel["panel_id"])
        for panel in enforced["panels"]
        for index in panel["covered_sample_indices"]
    }
    # Monotonicity is preserved, but it is explicitly NOT used as the consistency proof.
    assert enforced_pairs < geometric_pairs
    assert geometric["coverage_semantics_complete"] is True
    assert enforced["coverage_semantics_complete"] is True


# ===================================================================== fixture B


def fixture_b():
    towers, probes, records = layout()
    lookup, _source = lookup_with(records)
    return run_pipeline(towers=towers, probes=probes, lookup=lookup, required=2)


def test_fixture_b_is_genuinely_infeasible_from_complete_evidence():
    candidates, solve, coverage = fixture_b()
    snapshot_count = candidates["statistics"]["candidate_panel_count"]
    assert snapshot_count > 0
    # Complete evidence: nothing here is unknown, unevaluated, truncated or budget-cut.
    assert candidates["coverage_semantics_complete"] is True
    solver = solve["solver"]
    assert solver["milp_raw_status"] == "infeasible"
    assert solver["coverage_semantics_complete"] is True
    assert solver["infeasibility_proven"] is True
    assert solver["proof_withheld_reason"] is None
    assert solve["selected_panel_ids"] == []
    assert coverage
    assert all(item["status"] == "uncovered" for item in coverage)
    # The reason is a hard redundancy requirement, not fabricated by unknown evidence.
    per_sample_sites = {
        item["sample_id"]: item["actual_distinct_site_count"] for item in coverage
    }
    assert set(per_sample_sites) == {"S0", "S1", "S2", "S3", "S4"}
    assert all(count == 0 for count in per_sample_sites.values())

    diagnostic = milp_module.presolve_infeasibility(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[2] * len(candidates["samples"]), radar_types=[RADAR_TYPE_I],
    )
    assert diagnostic["insufficient_sample_count"] > 0
    insufficient_ids = {item["sample_id"] for item in diagnostic["insufficient_samples"]}
    assert insufficient_ids, "the infeasibility has no structural evidence"
    assert all(
        item["candidate_distinct_site_count"] < 2
        for item in diagnostic["insufficient_samples"]
    )


def test_fixture_b_solver_layout_uses_the_structural_proof_with_complete_evidence():
    towers, probes, records = layout()
    lookup, _source = lookup_with(records)
    candidates = build_candidates(
        towers=towers, samples=probes, terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )
    assert candidates["coverage_semantics_complete"] is True
    per_sample_sites = {
        index: len({
            panel["tower_id"] for panel in candidates["panels"]
            if index in panel["covered_sample_indices"]
        })
        for index in range(len(candidates["samples"]))
    }
    assert all(count >= 1 for count in per_sample_sites.values())
    # Require one more independent site than the complete evidence actually supports.
    demanded = {index: count + 1 for index, count in per_sample_sites.items()}
    structural = milp_module.presolve_infeasibility(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[demanded[index] for index in range(len(candidates["samples"]))],
        radar_types=[RADAR_TYPE_I],
    )
    assert structural["insufficient_sample_count"] == len(candidates["samples"])
    demanded_samples = [
        {**item, "required_distinct_site_count": demanded[position]}
        for position, item in enumerate(probes)
    ]
    result = solve_layout(
        towers=towers, samples=demanded_samples, options={"solver_time_limit_s": 10.0},
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED, terrain_los_lookup=lookup,
    )
    # Complete evidence: the structural verdict survives into the public result as a real
    # proof, and the downgrade path does not touch it.
    assert result["coverage_semantics_complete"] is True
    assert result["solver"]["infeasibility_proven"] is True
    assert result["solver"]["proof_withheld_reason"] is None
    assert "downgraded_from" not in result["solver"], (
        "complete evidence must not trigger the incomplete-LOS downgrade"
    )
    assert result["status"] == "infeasible"
    assert result["selected_panels"] == []
    assert result["gap_classification"] == "confirmed_gap"
    assert result["managed_physical_gap"] is True


# ===================================================================== fixture C


def test_fixture_c_budget_exhaustion_keeps_the_raw_status_but_withholds_the_proof():
    towers, probes, records = layout()
    lookup, _source = lookup_with(records, max_ray_count=1)
    candidates, solve, _coverage = run_pipeline(towers=towers, probes=probes, lookup=lookup)
    snapshot = lookup.snapshot()
    assert snapshot["complete"] is False
    assert snapshot["incomplete_reason"] == "los_computation_budget_exhausted"
    assert candidates["coverage_semantics_complete"] is False
    solver = solve["solver"]
    # The raw MILP verdict stays readable as internal diagnostics.
    assert solver["milp_raw_status"] in {
        "optimal", "infeasible", "time_limit", "iteration_limit", "solver_error",
        "solver_unavailable",
    }
    assert solver["infeasibility_proven"] is False
    assert solver["optimality_proven"] is False
    assert solver["proven_against_complete_physical_scenario"] is False
    assert solver["proof_withheld_reason"] == (
        "coverage_semantics_incomplete_los_evidence_not_a_physical_proof"
    )


def test_fixture_c_truncated_building_query_never_becomes_a_physical_proof():
    towers, probes, records = layout()
    lookup, _source = lookup_with(records, building_complete=False)
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    snapshot = lookup.snapshot()
    assert snapshot["complete"] is False
    # A truncated query leaves every physical ray undecided: no probe can be claimed.
    assert snapshot["combined_status_counts"].get("unknown", 0) > 0
    assert snapshot["incomplete_reason"] in {
        "building_los_evidence_unknown", "los_evidence_incomplete",
    }
    assert candidates["coverage_semantics_complete"] is False
    assert candidates["panels"] == [], (
        "a truncated building query must not leave a candidate panel claiming coverage"
    )
    assert all(not item["panels"] for item in coverage)
    solver = solve["solver"]
    assert solver["infeasibility_proven"] is False
    assert solver["optimality_proven"] is False
    assert solver["milp_raw_status"] in {
        "optimal", "infeasible", "time_limit", "iteration_limit", "solver_error",
        "solver_unavailable",
    }


def test_fixture_c_terrain_unknown_is_not_a_computation_budget_problem():
    towers, probes, records = layout()
    lookup, _source = lookup_with(
        records, terrain_evaluator=terrain_unknown_for_one_tower,
    )
    candidates, solve, coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
    )
    snapshot = lookup.snapshot()
    # A physical unknown is explicitly NOT an exhausted computation budget: every one of
    # the ten rays was really evaluated, so the terrain domain's own `unknown` answer is
    # what leaves that ray without a full verdict.
    assert snapshot["incomplete_reason"] == "terrain_los_evidence_unknown"
    assert snapshot["incomplete_reason"] != "los_computation_budget_exhausted"
    assert len(towers) * len(probes) == 10
    assert snapshot["cache_miss_count"] >= 10
    # An unknown physical domain still prevents claiming a proof over the complete scenario,
    # and it must never be silently promoted into a blocker.
    assert snapshot["complete"] is False
    assert candidates["coverage_semantics_complete"] is False
    solver = solve["solver"]
    assert solver["coverage_semantics_complete"] is False
    assert solver["infeasibility_proven"] is False
    assert solver["optimality_proven"] is False
    assert solver["milp_raw_status"] == "optimal", (
        "the raw MILP verdict must survive as internal diagnostics"
    )
    assert solver["proof_withheld_reason"] == (
        "coverage_semantics_incomplete_los_evidence_not_a_physical_proof"
    )
    assert {item["sample_id"] for item in coverage} == {"S0", "S1", "S2", "S3", "S4"}
    assert all(item["status"] == "satisfied" for item in coverage)
    assert not [item for item in coverage if not item["panels"]]
    # The unknown terrain rays made Tower-1 unusable, so the published plan rests on one
    # tower only — and it is still not allowed to claim a proven optimum.
    assert candidates["statistics"]["per_tower"][0]["radar_types"]["radar_i"][
        "physically_reachable_sample_count"
    ] == 0
    # The two incomplete-evidence variants must be distinguishable from this one: a real
    # budget cut reports a different reason than a physical unknown.
    budget_lookup, _ = lookup_with(records, max_ray_count=0)
    budget_lookup.evaluate(towers[0], probes[0], RADAR_TYPE_I)
    budget_snapshot = budget_lookup.snapshot()
    assert budget_snapshot["incomplete_reason"] == "los_computation_budget_exhausted"
    assert budget_snapshot["complete"] is False
    assert budget_snapshot["incomplete_reason"] != snapshot["incomplete_reason"]
    assert budget_snapshot["building_ray_count"] == 0


def test_fixture_c_solve_layout_returns_a_non_probative_status_and_no_escalation():
    towers, probes, records = layout()
    # One ray of budget: the coverage set is a tiny conservative subset, so the MILP may
    # well be "infeasible" there without saying anything about the physical scenario.
    lookup, _source = lookup_with(records, max_ray_count=1)
    result = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED, terrain_los_lookup=lookup,
    )
    assert lookup.snapshot()["complete"] is False
    assert lookup.snapshot()["incomplete_reason"] == "los_computation_budget_exhausted"
    assert result["coverage_semantics_complete"] is False
    assert result["solver"]["infeasibility_proven"] is False
    assert result["solver"]["optimality_proven"] is False
    assert result["status"] in {"search_incomplete", "solver_error", "refinement_incomplete"}
    assert result["status"] != "infeasible"
    assert result["gap_classification"] != "confirmed_gap"
    assert result["managed_physical_gap"] is False
    # No automatic Radar-II escalation because the LOS evidence was incomplete.
    assert result["allowed_radar_types"] == [RADAR_TYPE_I]
    assert result["automatic_radar_ii_escalation"] is False
    declared = {panel["radar_type"] for panel in result["selected_panels"]}
    assert declared <= {RADAR_TYPE_I}
    assert result["selected_panels"] == [], (
        "an incomplete coverage subset must not be published as a formal coverage conclusion"
    )
    # The raw diagnostics survive the downgrade.
    solver = result["solver"]
    assert solver["downgraded_from"] in {
        "optimal", "infeasible", "time_limit", "iteration_limit", "solver_error",
    }
    assert solver["downgrade_reason"] == "los_evidence_incomplete"
    assert solver["raw_status"] == solver["downgraded_from"]
    assert solver["milp_raw_status"] == solver["downgraded_from"]


# ============================================== status / completion orthogonality


@pytest.mark.parametrize(
    ("terrain", "building", "expected_combined", "expected_computation", "expected_evidence"),
    [
        # terrain clear / building clear, both complete → complete
        ({"status": "clear", "computation_completion": "complete",
          "evidence_completion": "complete"},
         {"status": "clear", "computation_completion": "complete",
          "evidence_completion": "complete"},
         "clear", "complete", "complete"),
        # terrain not_evaluated / building credible blocked → blocked, but incomplete
        (None,
         {"status": "blocked", "computation_completion": "complete",
          "evidence_completion": "complete"},
         "blocked", "incomplete", "incomplete"),
        # terrain unknown / building credible blocked → blocked, terrain stays unevaluated
        ({"status": "unknown", "computation_completion": "incomplete",
          "evidence_completion": "incomplete"},
         {"status": "blocked", "computation_completion": "complete",
          "evidence_completion": "complete"},
         "blocked", "incomplete", "incomplete"),
        # terrain clear / building predicted-only unknown → unknown, evidence incomplete
        ({"status": "clear", "computation_completion": "complete",
          "evidence_completion": "complete"},
         {"status": "unknown", "engineering_estimate_status": "blocked",
          "computation_completion": "complete", "evidence_completion": "incomplete"},
         "unknown", "complete", "incomplete"),
        # terrain unknown without finishing / building unknown → unknown, nothing complete
        ({"status": "unknown", "computation_completion": "not_evaluated",
          "evidence_completion": "not_evaluated"},
         {"status": "unknown", "computation_completion": "not_evaluated",
          "evidence_completion": "not_evaluated"},
         "unknown", "incomplete", "incomplete"),
        # geometry filtered / not_evaluated → no physical LOS verdict, not a physical unknown
        (None,
         {"status": "geometry_filtered", "computation_completion": "not_evaluated",
          "evidence_completion": "not_evaluated"},
         "geometry_filtered", "incomplete", "incomplete"),
        # budget-exhausted incomplete / clear → never a complete clear
        ({"status": "unknown", "computation_completion": "incomplete",
          "evidence_completion": "incomplete"},
         {"status": "clear", "computation_completion": "complete",
          "evidence_completion": "complete"},
         "unknown", "incomplete", "incomplete"),
    ],
)
def test_combined_status_and_completion_are_orthogonal(
    terrain, building, expected_combined, expected_computation, expected_evidence,
):
    combined = visibility.combine_visibility(terrain, building)
    assert combined["combined_visibility"] == expected_combined
    assert combined["computation_completion"] == expected_computation
    assert combined["evidence_completion"] == expected_evidence
    assert combined["verdict_is_orthogonal_to_completion"] is True


def test_blocked_verdict_never_forces_a_complete_claim_on_a_never_evaluated_domain():
    combined = visibility.combine_visibility(
        {"status": "clear", "computation_completion": "complete",
         "evidence_completion": "complete"},
        None,
    )
    assert combined["combined_visibility"] == "not_evaluated"
    assert combined["computation_completion"] == "incomplete"
    assert combined["evidence_completion"] == "incomplete"
    assert combined["domains_not_evaluated"] == ["building_los"]
    assert combined["search_incomplete"] is True


def test_lookup_propagates_incomplete_completion_for_a_decisive_block():
    records = [building_record(geometry=polygon(100, -10, 140, 10), height=30.0)]
    lookup, _source = lookup_with(records)
    result = lookup.evaluate(tower(0.0, 0.0, 10.0), sample(150.0), RADAR_TYPE_I)
    assert result["status"] == "blocked"
    assert result["computation_completion"] == "complete"
    # A decisive verdict is not allowed to promote a completion it did not earn.
    assert result["evidence_completion"] == "complete"
    assert result["combined_visibility"]["verdict_is_orthogonal_to_completion"] is True


# =========================================================== fingerprint separation


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


def test_combined_input_fingerprint_ignores_every_execution_receipt():
    baseline = visibility.combined_visibility_input_fingerprint(**fingerprint_inputs())
    slower = visibility.combined_visibility_input_fingerprint(**fingerprint_inputs())
    assert baseline == slower
    # The deprecated alias must produce the same semantic identity even when it is handed
    # budgets, completion and a building execution identity.
    legacy = visibility.combined_visibility_fingerprint(
        **fingerprint_inputs(),
        budgets={"max_rays": 8},
        completion={"state": "incomplete"},
        building_execution_identity={
            "elapsed_seconds": 9.5, "cache_hit_count": 0, "truncated": True,
        },
    )
    legacy_cached = visibility.combined_visibility_fingerprint(
        **fingerprint_inputs(),
        budgets={"max_rays": 4096},
        completion={"state": "complete"},
        building_execution_identity={
            "elapsed_seconds": 0.01, "cache_hit_count": 7, "truncated": False,
        },
    )
    assert legacy == legacy_cached
    # Same semantic inputs as the input fingerprint, minus the execution-only receipts.
    assert legacy == baseline


def test_combined_input_fingerprint_tracks_real_semantic_changes():
    baseline = visibility.combined_visibility_input_fingerprint(**fingerprint_inputs())
    for changed in (
        {"building_source_identity": {"file_name": "b.gpkg", "mtime_ns": 2}},
        {"terrain_source_identity": {"file_name": "d.tif", "mtime_ns": 3}},
        {"radar_geometry": {"version": "frozen-radar-geometry-v1_2"}},
        {"ray_model": {"id": building_los.RAY_MODEL_ID, "k_factor": 4.0 / 3.0}},
        {"crs": {"horizontal": "EPSG:32652", "vertical": "egm2008_orthometric"}},
        {"route_probe_identity": {"probe_id": "P2"}},
        {"building_input_fingerprint": "building-fp-2"},
    ):
        assert visibility.combined_visibility_input_fingerprint(
            **fingerprint_inputs(**changed)
        ) != baseline, changed


def test_combined_execution_identity_distinguishes_completion_but_not_the_input():
    inputs = fingerprint_inputs()
    baseline = visibility.combined_visibility_input_fingerprint(**inputs)
    complete = visibility.combined_visibility_execution_identity(
        input_fingerprint=baseline, budgets={"max_rays": 4096},
        completion={"state": "complete"}, search_incomplete=False,
        elapsed_seconds=0.01, cache_hit_count=7,
        query_receipt={"elapsed_seconds": 0.01, "truncated": False},
    )
    incomplete = visibility.combined_visibility_execution_identity(
        input_fingerprint=baseline, budgets={"max_rays": 1},
        completion={"state": "incomplete"},
        search_incomplete=True, incomplete_reason="los_computation_budget_exhausted",
        elapsed_seconds=0.01, cache_hit_count=7,
        query_receipt={"elapsed_seconds": 0.01, "truncated": True},
    )
    assert complete != incomplete
    assert complete["input_fingerprint"] == incomplete["input_fingerprint"] == baseline
    assert complete["execution_receipt_excluded_from_input_fingerprint"] is True
    assert incomplete["incomplete_reason"] == "los_computation_budget_exhausted"
    # Elapsed time stays a receipt: it never enters the input fingerprint.
    assert "elapsed_seconds" not in json.dumps(baseline)


def test_evidence_from_an_older_version_is_explicitly_stale_not_current():
    current = visibility.combined_visibility_evidence_compatibility(
        visibility.RADAR_COMBINED_VISIBILITY_VERSION, "fp",
    )
    assert current["compatible"] is True and current["stale"] is False
    assert current["silently_treated_as_current"] is False
    old = visibility.combined_visibility_evidence_compatibility(
        "radar_combined_visibility_v1", "fp",
    )
    assert old["compatible"] is False and old["stale"] is True
    assert old["reason"] == "combined_visibility_evidence_version_mismatch_explicit_stale"
    assert old["current_version"] == visibility.RADAR_COMBINED_VISIBILITY_VERSION
    assert old["current_fingerprint_version"] == (
        visibility.RADAR_COMBINED_FINGERPRINT_VERSION
    )
    # Evidence produced under the old schema therefore hashes differently.
    assert old["evidence_fingerprint"] != visibility.combined_visibility_input_fingerprint(
        **fingerprint_inputs()
    )


def test_official_radar_1_3_fingerprint_is_untouched_by_the_combined_split():
    from cns_planner.domain.radar_surveillance_layout import (
        ALGORITHM_VERSION,
        SCHEMA_VERSION,
        SEMANTICS_FINGERPRINT,
        radar_geometry_parameters,
    )

    assert ALGORITHM_VERSION == "1.3"
    assert SCHEMA_VERSION == "radar-surveillance-layout-v1"
    assert building_los.FROZEN_RADAR_GEOMETRY_VERSION == "radar_layout_geometry_v1_1"
    assert SEMANTICS_FINGERPRINT["geometry_version"] == "radar_layout_geometry_v1_1"
    assert SEMANTICS_FINGERPRINT["fixed_altitude_layer_id"] == "ALT-080"
    parameters = radar_geometry_parameters(RADAR_TYPE_I)
    assert parameters["min_slant_range_m"] == 120.0
    assert parameters["max_slant_range_m"] == 3000.0
    assert parameters["azimuth_half_width_deg"] == 45.0
    assert parameters["confirmed"] is True
    assert parameters["parameter_origin"] == (
        "source_device_specification_fixed_geometry_preset"
    )
    # The combined evidence versions moved forward; the official Radar 1.3 module did not.
    assert visibility.RADAR_COMBINED_VISIBILITY_VERSION.endswith("_v2")
    assert visibility.RADAR_COMBINED_FINGERPRINT_VERSION.endswith("_v2")


def test_production_entry_still_rejects_enforced_terrain_and_building_los():
    from cns_planner.application.radar_surveillance_layout_service import (
        RadarSurveillanceLayoutService,
    )

    service = RadarSurveillanceLayoutService.__new__(RadarSurveillanceLayoutService)
    with pytest.raises(ValueError):
        service.evaluate({"terrain_los_mode": "enforced"})
    with pytest.raises(ValueError):
        service.evaluate({"terrain_los_mode": "enforced", "building_los_mode": "combined"})


def test_disabled_mode_parity_keeps_the_frozen_layout_unchanged():
    towers, probes, _records = layout()
    baseline = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode="disabled", allow_mixed=False,
    )
    lookup = TerrainLosLookup(
        mode="disabled", evaluator=terrain_clear_evaluator,
        building_mode="disabled",
    )
    parity = solve_layout(
        towers=towers, samples=probes, options={"solver_time_limit_s": 10.0},
        terrain_los_mode="disabled", terrain_los_lookup=lookup, allow_mixed=False,
    )
    assert baseline["status"] == parity["status"]
    assert baseline["selected_panels"] == parity["selected_panels"]
    assert baseline["validation"] == parity["validation"]
    assert baseline["coverage_semantics_complete"] is True
    assert parity["coverage_semantics_complete"] is True
    assert "terrain_los_evidence" not in parity


def test_radar_ii_is_available_but_never_auto_escalated_without_proof():
    towers, probes, records = layout()
    lookup, _source = lookup_with(records)
    candidates, solve, _coverage = run_pipeline(
        towers=towers, probes=probes, lookup=lookup,
        radar_types=(RADAR_TYPE_I, RADAR_TYPE_II),
    )
    assert candidates["coverage_semantics_complete"] is True
    assert solve["solver"]["optimality_proven"] is True
    declared = {
        panel["radar_type"] for panel in candidates["panels"]
        if panel["panel_id"] in set(solve["selected_panel_ids"])
    }
    # Both device types are offered to the solver; nothing escalates on its own.
    assert declared <= {RADAR_TYPE_I, RADAR_TYPE_II}
    assert "radar_ii" in {
        panel["radar_type"] for panel in candidates["panels"]
    }


def test_main_workspace_is_still_untouched_by_this_round():
    import subprocess

    main_workspace = Path(r"D:\Projects\CNS-PLANNER")
    if not (main_workspace / ".git").exists():
        pytest.skip("main workspace git metadata unavailable")
    head = subprocess.run(
        ["git", "-C", str(main_workspace), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "68e2674"
    status = subprocess.run(
        ["git", "-C", str(main_workspace), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "_work/round34_t07d2c1_los_closeout" not in status
    stashes = subprocess.run(
        ["git", "-C", str(main_workspace), "stash", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert stashes.count("round34_t07d2c1") == 0
