"""Re-run the D2-C0 fixture under the D2-C1 code and show the corrected proof semantics.

The D2-C0 record reported ``milp_infeasibility_proven = true`` with
``final_status = infeasible`` while the combined lookup was incomplete.  This probe points
the same D2-C0 fixture module at the D2-C1 tree and prints what the same run now reports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

C1 = Path(r"D:\Projects\CNS-PLANNER\_work\round34_t07d2c1_los_closeout")
sys.path.insert(0, str(C1))

from cns_planner.algorithms.radar_layout import milp as milp_module  # noqa: E402
from cns_planner.algorithms.radar_layout.candidates import build_candidates  # noqa: E402
from cns_planner.algorithms.radar_layout.terrain_los_lookup import (  # noqa: E402
    BUILDING_MODE_COMBINED,
    TERRAIN_LOS_MODE_ENFORCED,
    TerrainLosLookup,
)
from cns_planner.algorithms.radar_layout.v1 import actual_site_coverage  # noqa: E402
from cns_planner.gis import radar_building_los_adapter as building_adapter  # noqa: E402

sys.path.insert(0, str(C1 / "tests"))
from test_round34_t07d2c0_building_los_combined import (  # noqa: E402
    RADAR_TYPE_I,
    FakeBuildingSource,
    building_record,
    polygon,
    sample,
    tower,
)


def terrain_clear_evaluator(*, tower, sample, radar_type, geometry):
    return {
        "status": "clear", "reason": None,
        "terrain_profile": {"samples": [], "sample_count": 0},
        "computation_completion": "complete", "evidence_completion": "complete",
    }


def main():
    towers = [tower(0.0, 0.0, 10.0, tid="T1"), tower(0.0, 200.0, 10.0, tid="T2")]
    samples = [sample(60.0 + index * 60.0, 0.0, 10.0, sid=f"S{index}") for index in range(6)]
    records = [building_record(
        geometry=polygon(100.0, -10.0, 140.0, 10.0), height=60.0, bid="BLOCKER",
    )]
    source = FakeBuildingSource(records)

    def building_evaluator(*, tower, sample, radar_type, geometry):
        return building_adapter.evaluate_probe_building_los(
            tower, sample, radar_type=radar_type, building_source=source,
            transform=None, terrain_source=None,
        )

    lookup = TerrainLosLookup(
        mode=TERRAIN_LOS_MODE_ENFORCED, evaluator=terrain_clear_evaluator,
        building_evaluator=building_evaluator, building_mode=BUILDING_MODE_COMBINED,
        source_identity={"dem": "d2c0-fixture"},
        building_source_identity={"buildings": "d2c0-fixture"},
    )
    candidates = build_candidates(
        towers=towers, samples=samples, terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )
    coverage_is_complete = bool(candidates.get("coverage_semantics_complete", True))
    solve = milp_module.solve(
        panels=candidates["panels"], samples=candidates["samples"],
        required_counts=[1] * len(candidates["samples"]), radar_types=[RADAR_TYPE_I],
        objectives=("total_panel_count",), stage="d2c0_fixture_replay",
        options={"solver_time_limit_s": 10.0}, coverage_is_complete=coverage_is_complete,
    )
    coverage = actual_site_coverage(
        panels=candidates["panels"], samples=candidates["samples"],
        selected_panel_ids=solve.get("selected_panel_ids") or [],
        tower_records=candidates["towers"], terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )
    snapshot = lookup.snapshot()
    solver = solve["solver"]
    milp_by_panel = {
        panel["panel_id"]: sorted(int(i) for i in panel["covered_sample_indices"])
        for panel in candidates["panels"]
        if panel["panel_id"] in set(solve.get("selected_panel_ids") or [])
    }
    index_by_id = {item["sample_id"]: item["sample_index"] for item in candidates["samples"]}
    final_by_panel = {}
    for item in coverage:
        for panel in item["panels"]:
            final_by_panel.setdefault(panel["panel_id"], []).append(
                index_by_id[item["sample_id"]]
            )
    final_by_panel = {k: sorted(v) for k, v in final_by_panel.items()}
    report = {
        "replay_of": "round34_t07d2c0_changes.patch main fixture (25 m, 6 samples, 2 towers)",
        "under_code": "round34_t07d2c1_los_closeout",
        "d2c0_recorded": {
            "combined_lookup_complete": False,
            "incomplete_reason": "building_los_evidence_unknown",
            "milp_infeasibility_proven": True,
            "final_status": "infeasible",
            "coverage_and_milp_share_panel_ids": "set()",
            "geometric_covered_pair_count": 6,
            "enforced_covered_pair_count": 0,
            "actual_site_coverage": [],
        },
        "d2c1_replay": {
            "combined_lookup_complete": snapshot["complete"],
            "incomplete_reason": snapshot["incomplete_reason"],
            "combined_status_counts": snapshot["combined_status_counts"],
            "coverage_semantics_complete": candidates["coverage_semantics_complete"],
            "milp_raw_status": solver.get("milp_raw_status"),
            "milp_status": solver.get("status"),
            "milp_optimality_proven": solver.get("optimality_proven"),
            "milp_infeasibility_proven": solver.get("infeasibility_proven"),
            "proof_withheld_reason": solver.get("proof_withheld_reason"),
            "selected_panel_ids": list(solve.get("selected_panel_ids") or []),
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
            "covered_sample_ids": [
                item["sample_id"] for item in coverage if item["panels"]
            ],
            "coverage_status_by_sample": {
                item["sample_id"]: item["status"] for item in coverage
            },
        },
        "false_physical_infeasibility_proof_removed": (
            solver.get("infeasibility_proven") is not True
        ),
    }
    out = C1 / "_work" / "round34_t07d2c1_d2c0_fixture_replay.json"
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8", newline="",
    )
    print(json.dumps(report["d2c1_replay"], indent=2, ensure_ascii=False, default=str))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
