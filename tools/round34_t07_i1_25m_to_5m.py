"""Generate the T07-I1 fixed-25m-panel to 5m validation evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.algorithms.radar_layout.terrain_los_lookup import (  # noqa: E402
    TERRAIN_LOS_MODE_ENFORCED,
)
from cns_planner.algorithms.radar_layout.v1 import solve_layout  # noqa: E402
from tools.round34_t07d2c1_fixture import (  # noqa: E402
    FINE_COUNT,
    FINE_SPACING_M,
    FINE_START_M,
    FORMAL_INPUTS,
    layout,
    lookup_with,
    run_pipeline,
    sample,
    sha256_of,
)

DEFAULT_OUTPUT = ROOT / "_work" / "round34_t07_i1_25m_to_5m_validation.json"


def _hashes():
    return {
        name: {"path": str(path), "sha256": sha256_of(path)}
        for name, path in FORMAL_INPUTS.items()
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    before = _hashes()

    towers, coarse, records = layout("radar_i")
    initial_lookup, _initial_source = lookup_with(records)
    _initial_candidates, initial_solve, _initial_coverage = run_pipeline(
        towers=towers, probes=coarse, lookup=initial_lookup,
    )
    initial_ids = set(initial_solve.get("selected_panel_ids") or [])
    fine = [
        {
            **sample(
                FINE_START_M + index * FINE_SPACING_M,
                0.0,
                10.0,
                sid=f"V{index:03d}",
            ),
            "sample_index": index,
        }
        for index in range(FINE_COUNT)
    ]
    lookup, _source = lookup_with(records)
    result = solve_layout(
        towers=towers,
        samples=coarse,
        validation_samples=fine,
        terrain_los_mode=TERRAIN_LOS_MODE_ENFORCED,
        terrain_los_lookup=lookup,
    )
    direct = result["refinement_rounds"][0]

    independent_lookup, _independent_source = lookup_with(records)
    _candidates, independent_solve, _coverage = run_pipeline(
        towers=towers, probes=fine, lookup=independent_lookup,
    )
    later_rounds = result["refinement_rounds"][1:]
    original_ids = initial_ids
    final_ids = {item["panel_id"] for item in result["selected_panels"]}
    payload = {
        "diagnostic_kind": "synthetic_fixture_fixed_25m_panel_direct_5m_recheck",
        "authoritative_project_result": False,
        "production_enforced_enabled": False,
        "original_solution": {
            "optimization_spacing_m": 25.0,
            "selected_panel_ids": sorted(original_ids),
            "selected_panels": direct["selected_panels"],
        },
        "direct_five_meter_validation": {
            "validation_kind": direct["validation_kind"],
            "spacing_m": direct["spacing_m"],
            "reviewed_sample_count": direct["reviewed_sample_count"],
            "selected_panel_ids_unchanged": set(direct["selected_panel_ids"]) == original_ids,
            "validation_passed": direct["validation_passed"],
            "site_coverage_status_counts": direct["site_coverage_status_counts"],
            "required_distinct_site_count_counts": direct[
                "required_distinct_site_count_counts"
            ],
            "violation_count": direct["violation_count"],
            "failed_samples": direct["failed_samples"],
            "los_evidence": direct["los_evidence"],
        },
        "refinement": {
            "entered": bool(later_rounds),
            "round_count": len(later_rounds),
            "rounds": later_rounds,
            "final_selected_panel_ids": sorted(final_ids),
            "selected_panel_ids_changed": final_ids != original_ids,
        },
        "independent_five_meter_resolve_not_used_as_validation": {
            "selected_panel_ids": independent_solve.get("selected_panel_ids") or [],
            "solver_status": independent_solve["solver"]["status"],
            "disclosure": (
                "该结果是独立 5m 重新求解，仅作对照；它不替代原 25m selected panel 的"
                "直接复核，也不用于宣称原方案通过。"
            ),
        },
        "result_status": result["status"],
        "before": before,
    }
    after = _hashes()
    payload["after"] = after
    payload["all_read_only_inputs_unchanged"] = before == after
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="",
    )
    print(json.dumps({
        "output": str(args.output),
        "original": sorted(original_ids),
        "direct_validation_passed": direct["validation_passed"],
        "violations": direct["violation_count"],
        "refinement_entered": bool(later_rounds),
        "independent_5m": independent_solve.get("selected_panel_ids") or [],
        "all_read_only_inputs_unchanged": payload["all_read_only_inputs_unchanged"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
