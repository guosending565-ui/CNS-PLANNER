#!/usr/bin/env python3
"""Descriptive V2 sensitivity runs for expert consultation.

This tool varies only declared inputs to the frozen V2 planner.  It does not edit
planner code, rank rows, select a parameter, or write results into a project.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]


def _load_baseline():
    path = ROOT / "tools" / "route_planning_baseline.py"
    spec = importlib.util.spec_from_file_location("route_planning_baseline_for_diagnostics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = _load_baseline()


def _path_fingerprint(path):
    return sha256(json.dumps(path or [], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _row(case, context, parameters):
    run = {
        "planner_id": BASELINE.V2,
        "run_id": "diagnostic_v2",
        "parameters": dict(parameters),
    }
    started = time.perf_counter()
    result = BASELINE._run_planner_once(run, case, context)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    diagnostics = BASELINE.evaluate_route_quality(
        context["route"], result, context["hard_constraints"], runtime_ms,
        grid=context["grid"], airspace_eligibility=context["airspace_eligibility"],
    )
    quality, risk = diagnostics["quality"], diagnostics["risk_metrics"]
    return {
        "status": diagnostics["status"],
        "reason": diagnostics["reason"],
        "length_m": quality["path_length_m"],
        "detour_factor": quality["detour_factor"],
        "turn_count": quality["turn_count"],
        "total_heading_change_deg": quality["total_heading_change_deg"],
        "max_heading_change_deg": quality["max_heading_change_deg"],
        "zigzag_index": quality["zigzag_index"],
        "risk_exposure_index_m": risk.get("risk_exposure_index_m"),
        "mean_risk_index": risk.get("mean_risk_index"),
        "max_risk_index": risk.get("max_risk_index"),
        "runtime_ms": runtime_ms,
        "path_fingerprint": _path_fingerprint(result.get("path")),
        "grid_level": diagnostics["grid_behavior"]["grid_level"],
        "cell_size_degrees": diagnostics["grid_behavior"]["cell_size_degrees"],
        "grid_steps": diagnostics["grid_behavior"],
        "verdicts": diagnostics["verdicts"],
    }


def lambda_sensitivity(case_id="risk_tradeoff", lambdas=(0, 0.5, 1, 2, 4, 8)):
    case = BASELINE.benchmark_fixtures.case(case_id)
    context = BASELINE._planner_context(case, max_cells=20000)
    rows = []
    for value in lambdas:
        row = _row(case, context, {
            "risk_weight_lambda": float(value), "risk_component": "overall",
        })
        row["risk_weight_lambda"] = float(value)
        rows.append(row)
    return {
        "analysis": "lambda_sensitivity",
        "case_id": case_id,
        "rows": rows,
        "automatic_recommendation": None,
        "semantics": "descriptive_only_frozen_v2_input_sweep",
    }


def grid_sensitivity(case_id="zigzag_open_grid_bias", levels=(6, 7, 8)):
    rows = []
    for level in levels:
        case = BASELINE.benchmark_fixtures.case(case_id)
        case["workspace_level"] = int(level)
        context = BASELINE._planner_context(case, max_cells=20000)
        row = _row(case, context, {
            "risk_weight_lambda": 0.0, "risk_component": "overall",
        })
        row["requested_grid_level"] = int(level)
        row["actual_grid_level"] = (context.get("grid") or {}).get("level")
        row["grid_cell_count"] = (context.get("grid") or {}).get("count")
        rows.append(row)
    return {
        "analysis": "grid_resolution_sensitivity",
        "case_id": case_id,
        "rows": rows,
        "automatic_recommendation": None,
        "semantics": "descriptive_only_existing_mht_levels_frozen_v2",
    }


def build_diagnostics():
    return {
        "schema_version": 1,
        "planner": "risk_aware_route_planner_v2@2.0",
        "planner_changes_allowed": False,
        "lambda_sensitivity": lambda_sensitivity(),
        "grid_sensitivity": grid_sensitivity(),
        "verdicts": {
            "automatically_ranked": False,
            "automatically_scored": False,
            "recommended_parameter": None,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="冻结 V2 的航路规划敏感性诊断")
    parser.add_argument("--output", default=None, help="可选 JSON 输出路径")
    arguments = parser.parse_args(argv)
    result = build_diagnostics()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.output:
        path = Path(arguments.output)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(path)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
