"""Route-planning expert evidence pack (single reproducible entry point).

Runs the current V1/V2 planners over the synthetic fixtures in
:mod:`cns_planner.benchmark.fixtures`, repeats each run for runtime/fingerprint
statistics (OMPL-style ``--runs`` / ``--warmup`` semantics, without any OMPL
dependency), and writes a JSON + Markdown evidence pack into the ignored
``outputs/`` tree.  The pack also gathers project route experiments, confirmed
reference comparisons and data readiness when they exist.

Scope guarantees:

* no planner code is changed to make a case pass;
* a planner is only called when the case declares it applicable, otherwise the case
  is reported as ``not_applicable`` / ``missing_prerequisite``;
* the malformed-constraint case stops at the Application input boundary, so no
  planner is invoked at all;
* repetitions are used only for runtime statistics and determinism checks — the pack
  never ranks, scores or recommends an algorithm;
* if real project/reference data is absent or its CRS is unresolved the pack says
  ``NOT READY`` and reports the blockers instead of inventing data.

Usage::

    python tools/route_planning_baseline.py [--out-dir outputs/route_baseline]
                                            [--case open_space] [--runs 7]
                                            [--warmup 2] [--project PATH]
                                            [--quiet]
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import math
from pathlib import Path
import platform
import re
import socket
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cns_planner.algorithms.registry import build_default_algorithm_registry  # noqa: E402
from cns_planner.algorithms.route.v1 import RoutePlannerV1  # noqa: E402
from cns_planner.algorithms.grid.service import WorkspaceGridService  # noqa: E402
from cns_planner.application.constraint_validation import validate_hard_constraints  # noqa: E402
from cns_planner.benchmark import fixtures as benchmark_fixtures  # noqa: E402
from cns_planner.benchmark.geodesy import GEODESIC_BACKEND, METRIC_SEMANTICS  # noqa: E402
from cns_planner.benchmark.quality import evaluate_route_quality  # noqa: E402
from cns_planner.domain.reference_crs import is_resolved  # noqa: E402
from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2  # noqa: E402


TOOL_VERSION = "2.0"
PACK_SCHEMA_VERSION = "route-expert-evidence-2"
V1 = "route_planner_v1"
V2 = "risk_aware_route_planner_v2"
PLANNERS = (V1, V2)
DEFAULTS_PATH = ROOT / "cns_planner" / "config" / "defaults.json"

DEFAULT_RUNS = 5
DEFAULT_WARMUP = 1

NOT_APPLICABLE = "not_applicable"
MISSING_PREREQUISITE = "missing_prerequisite"
REJECTED = "rejected_at_input_boundary"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_revision():
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
        return f"unavailable: {completed.stderr.strip() or completed.returncode}"
    except Exception as exc:  # pragma: no cover - defensive, git may be absent
        return f"unavailable: {exc}"


def _environment():
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "hostname": socket.gethostname(),
        "cpu_count": _cpu_count(),
        "geodesic_backend": GEODESIC_BACKEND,
    }


def _cpu_count():
    try:
        import os

        return os.cpu_count()
    except Exception:  # pragma: no cover - defensive
        return None


def statistics_block(values, *, unit="ms"):
    """min/median/p95/max for a series of measurements (reported, never scored)."""

    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return {
            "count": 0, "min": None, "median": None, "p95": None, "max": None,
            "unit": unit, "used_for_ranking": False,
        }
    ordered = sorted(numbers)
    index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
        "mean": statistics.fmean(ordered),
        "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
        "unit": unit,
        "percentile_method": "nearest_rank_p95",
        "used_for_ranking": False,
        "used_for_scoring": False,
    }


def _result_fingerprint(result):
    """Stable fingerprint of a planner result's observable geometry and identity."""

    if not isinstance(result, dict):
        return None
    return sha256(json.dumps({
        "status": result.get("status"),
        "path": result.get("path"),
        "grid_path": result.get("grid_path"),
        "input_fingerprint": result.get("input_fingerprint"),
        "algorithm_id": result.get("algorithm_id"),
        "algorithm_version": result.get("algorithm_version"),
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _workspace_grid(case, max_cells=5000):
    service = WorkspaceGridService(preferred_level=case["workspace_level"], max_cells=max_cells)
    return service.generate(case["workspace_bbox"], case["workspace_level"])


def _snap(value, quantum):
    if quantum <= 0:
        return float(value)
    return round(round(float(value) / quantum) * quantum, 12)


def _grid_shape(case, grid):
    columns = sorted({round(float(cell["bbox"][0]), 12) for cell in grid["cells"]})
    rows = sorted({round(float(cell["bbox"][1]), 12) for cell in grid["cells"]})
    step_lon = (columns[1] - columns[0]) if len(columns) > 1 else 0.0
    step_lat = (rows[1] - rows[0]) if len(rows) > 1 else 0.0
    return len(columns), len(rows), step_lon, step_lat


def _boxes_to_rectangles(case, grid):
    """Snap synthetic world-coordinate boxes onto grid-cell boundary rectangles.

    Snapping keeps every synthetic allowed polygon exactly on cell boundaries so
    the coverage test never has to resolve a boundary-touching cell by luck.
    """

    west, south = float(case["workspace_bbox"][0]), float(case["workspace_bbox"][1])
    columns, rows, step_lon, step_lat = _grid_shape(case, grid)
    if step_lon <= 0 or step_lat <= 0:
        return [(west, south, west, south)]
    rectangles = [(west, south, west + columns * step_lon, south + rows * step_lat)]
    for box in case["blocked_boxes"] or []:
        low_lon, low_lat, high_lon, high_lat = (float(value) for value in box)
        index_west = max(0, math.floor((low_lon - west) / step_lon + 1e-9))
        index_south = max(0, math.floor((low_lat - south) / step_lat + 1e-9))
        index_east = min(columns, math.ceil((high_lon - west) / step_lon - 1e-9))
        index_north = min(rows, math.ceil((high_lat - south) / step_lat - 1e-9))
        if index_east <= index_west or index_north <= index_south:
            continue
        rectangles.append((
            _snap(west + step_lon * index_west, 1e-12),
            _snap(south + step_lat * index_south, 1e-12),
            _snap(west + step_lon * index_east, 1e-12),
            _snap(south + step_lat * index_north, 1e-12),
        ))
    return rectangles


def _rectangle_geometry(rectangle):
    west, south, east, north = rectangle
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south], [east, south], [east, north], [west, north], [west, south],
        ]],
    }


_GRID_ID_PATTERN = re.compile(
    r"^MHT4063-L(?P<level>\d+)-C(?P<column>\d+)-R(?P<sign>[PM])(?P<row>\d+)$"
)


def grid_indices(grid):
    """Exact ``(level, column, row)`` index per grid_id, mirroring the planner graph.

    Uses the published MH/T ``grid_id`` so neighbour lookup is integer-exact instead of
    floating-point based; ``None`` when any id does not follow the scheme.
    """

    result = {}
    seen = set()
    for cell in grid.get("cells") or []:
        grid_id = str(cell.get("grid_id") or "")
        match = _GRID_ID_PATTERN.match(grid_id)
        if match is None:
            return None
        row = int(match.group("row")) * (-1 if match.group("sign") == "M" else 1)
        index = (int(match.group("level")), int(match.group("column")), row)
        if index in seen:
            return None
        seen.add(index)
        result[grid_id] = index
    return result


def _adjacency_edges(grid):
    """Adjacency edges of the grid graph, built in O(cells * 8).

    This is intentionally *not* ``AirspaceEligibilityService.build``.  That service
    answers a harder question (full geometric coverage of every candidate edge by the
    union of allowed polygons) and does so with a pairwise candidate scan, which is
    quadratic in the cell count.  For a synthetic allowed *shell* — where every cell is
    inside the one confirmed allowed polygon by construction — the pairwise scan is pure
    overhead, and at MH/T level 8 it dominated the whole tool run.

    So the fixture builds the same information directly from exact MH/T indices: the
    allowed cell set is unambiguous, and the planner's own diagonal guards still enforce
    every traversal rule at search time.  Production eligibility is untouched.
    """

    ids = [str(cell["grid_id"]) for cell in grid.get("cells") or []]
    indices = grid_indices(grid)
    if indices is None:
        raise ValueError("合成 grid 缺少可解析的 MH/T grid_id，无法构造邻接")
    reverse = {value: key for key, value in indices.items()}
    edges = []
    for grid_id, (level, column, row) in indices.items():
        for dx, dy in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
            other = reverse.get((level, column + dx, row + dy))
            if other is None:
                continue
            # Emit each undirected edge once.  grid_ids are opaque strings whose sort
            # order is not guaranteed, so compare the parsed integer indices instead.
            if indices[other] > indices[grid_id]:
                edges.append([grid_id, other])
    return sorted(set(ids)), sorted(edges)


def _airspace_eligibility(case, grid):
    """Synthetic confirmed allowed airspace = the whole synthetic workspace shell.

    Blocked boxes are deliberately NOT declared ``blocked`` here, so they stay inside
    the allowed geometry and must be excluded by the planner's own hard-constraint
    handling.  That keeps the allowed-airspace fixture and the hard-constraint fixture
    independently observable instead of double-counting the same obstacle.
    """

    shell = _boxes_to_rectangles(case, grid)[0]
    allowed_ids, edges = _adjacency_edges(grid)
    feature_id = "SYN-ALLOWED-SHELL"
    return {
        "status": "passed" if allowed_ids else "missing_data",
        "message": "仅 confirmed allowed geometry 可用于运行航路",
        "algorithm_id": "confirmed-airspace-eligibility",
        "algorithm_version": "1.0",
        "allowed_grid_ids": allowed_ids,
        "connector_safe_grid_ids": allowed_ids,
        "allowed_edges": edges,
        "feature_counts": {"allowed": 1, "blocked": 0, "unknown": 0},
        "feature_fingerprint": sha256(
            json.dumps(_rectangle_geometry(shell), sort_keys=True).encode()
        ).hexdigest(),
        "policy_fingerprint": sha256(b"SYN-ALLOWED-SHELL").hexdigest(),
        "synthetic_fixture": True,
        "synthetic_construction": {
            "method": "direct_grid_adjacency_for_single_confirmed_allowed_shell",
            "why": (
                "AirspaceEligibilityService 的候选边全覆盖判定是 pairwise（O(n^2)），"
                "对单一 allowed 外壳属纯开销；合成 fixture 直接构造等价邻接，"
                "遍历规则仍由 planner 自身 guard 在搜索时执行。"
            ),
            "production_eligibility_unchanged": True,
        },
    }


def _build_grid_risk(case, grid):
    low, high, band = 0.05, 0.85, None
    if case.get("risk"):
        low = float(case["risk"].get("low", low))
        high = float(case["risk"].get("high", high))
        band = case["risk"].get("band")

    def inside(bbox):
        if not band:
            return False
        west, south, east, north = (float(value) for value in bbox)
        low_lon, low_lat, high_lon, high_lat = (float(value) for value in band)
        center = ((west + east) / 2.0, (south + north) / 2.0)
        return low_lon <= center[0] <= high_lon and low_lat <= center[1] <= high_lat

    cells = {}
    for cell in grid.get("cells") or []:
        score = high if inside(cell["bbox"]) else low
        component = {"score": score, "status": "passed", "source": "synthetic_benchmark_fixture"}
        cells[cell["grid_id"]] = {"overall": dict(component), "ground": None, "air": None}
    return {
        "status": "passed",
        "algorithm_id": "risk-model-v1-relative-index",
        "algorithm_version": "1.1",
        "semantics": "relative_engineering_index_not_probability",
        "synthetic_fixture": True,
        "cells": cells,
    }


def _scenario_route(case):
    return {
        "route_id": f"BENCH-{case['case_id']}",
        "start_node_id": "BENCH-START",
        "end_node_id": "BENCH-END",
        "start": [float(value) for value in case["start"]],
        "end": [float(value) for value in case["end"]],
        "direction": "BENCH-START→BENCH-END",
        "status": "passed",
        "kind": "scenario",
    }


def _context_hard_constraints(case):
    """Hard constraints a planner would really receive for this case.

    V1 plans on the workspace bbox and only ever sees hard constraints.  V2 plans on
    the MH/T grid and excludes obstacles through the same hard constraints plus the
    allowed-airspace set.  Blocked boxes are therefore materialized as hard
    constraints for both planners so the obstacle fixture is identical for both and
    is not silently dropped for V1.
    """

    constraints = [dict(item) for item in case["hard_constraints"]]
    for index, box in enumerate(case["blocked_boxes"] or []):
        constraints.append({
            "name": f"合成阻断盒 {index}",
            "bbox": [float(value) for value in box],
            "source": "synthetic_benchmark_fixture",
        })
    return constraints


def _planner_context(case, *, max_cells=5000):
    """Everything a planner would receive, or the reason it cannot run at all."""

    context = {
        "route": _scenario_route(case),
        "hard_constraints": _context_hard_constraints(case),
        "grid": None,
        "grid_risk": None,
        "airspace_eligibility": None,
    }
    try:
        # Application input boundary: identical validator the live service uses.
        context["hard_constraints"] = validate_hard_constraints(context["hard_constraints"])
    except ValueError as exc:
        context["input_rejection"] = str(exc)
        return context
    grid = _workspace_grid(case, max_cells=max_cells)
    context["grid"] = grid
    context["grid_risk"] = _build_grid_risk(case, grid)
    context["airspace_eligibility"] = _airspace_eligibility(case, grid)
    return context


def _applicability(case, planner_id):
    entry = (case.get("applicability") or {}).get(planner_id) or {}
    if case["application"] == "input_guard":
        return {
            "status": REJECTED,
            "applicable": False,
            "detail": "非法硬约束在 Application 输入边界被拒绝，planner 未被调用。",
        }
    if entry.get("applicable") is False:
        return {
            "status": NOT_APPLICABLE,
            "applicable": False,
            "detail": entry.get("note") or "该 planner 不适用于本用例。",
        }
    return {"status": "applicable", "applicable": True, "detail": entry.get("note")}


def _planner_runs(case):
    """Planner runs to execute for one case: V1/V2 plus explicit parameter variants.

    Every run keeps its declared algorithm id and effective parameters so the pack
    can state exactly what produced each result.
    """

    runs = [
        {"run_id": V1, "planner_id": V1, "parameters": {}},
        {"run_id": V2, "planner_id": V2, "parameters": {"risk_weight_lambda": 0.0, "risk_component": "overall"}},
    ]
    for variant in case.get("v2_parameter_variants") or []:
        runs.append({
            "run_id": variant["run_id"], "planner_id": V2,
            "parameters": dict(variant["parameters"]),
        })
    return runs


def _path_fingerprint(path):
    return sha256(
        json.dumps(path, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _repeat_run(run, case, context, *, runs, warmup):
    """Execute one planner run ``warmup + runs`` times, keeping the first result.

    Repetitions exist only to characterise runtime spread and result determinism.
    Every repetition is reported; none of them is treated as a better iteration.
    """

    for _ in range(max(0, int(warmup))):
        _run_planner_once(run, case, context)
    repetitions = []
    result = None
    for index in range(max(1, int(runs))):
        started = time.perf_counter()
        current = _run_planner_once(run, case, context)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if result is None:
            result = current
        repetitions.append({
            "index": index + 1,
            "runtime_ms": elapsed_ms,
            "result_fingerprint": _result_fingerprint(current),
            "path_fingerprint": _path_fingerprint(current.get("path")),
            "status": current.get("status"),
        })
    result_fingerprints = {item["result_fingerprint"] for item in repetitions}
    path_fingerprints = {item["path_fingerprint"] for item in repetitions}
    return result, repetitions, {
        "warmup_runs": max(0, int(warmup)),
        "measured_runs": len(repetitions),
        "runtime_ms": statistics_block([item["runtime_ms"] for item in repetitions]),
        "repetitions": repetitions,
        "result_fingerprint": repetitions[0]["result_fingerprint"],
        "path_fingerprint": repetitions[0]["path_fingerprint"],
        "deterministic_consistency": len(result_fingerprints) == 1 and len(path_fingerprints) == 1,
        "distinct_result_fingerprints": len(result_fingerprints),
        "distinct_path_fingerprints": len(path_fingerprints),
        "seed": None,
        "seed_status": "not_used_current_planners_are_deterministic_no_rng",
        "seed_note": "为未来随机 planner 预留；当前 V1/V2 不使用随机数，seed 保持 null。",
        "used_for_ranking": False,
        "used_for_scoring": False,
    }


def _run_planner_once(run, case, context):
    route = context["route"]
    constraints = context["hard_constraints"]
    if run["planner_id"] == V1:
        planner = RoutePlannerV1()
        return planner.plan(route, case["workspace_bbox"], constraints)
    planner = RiskAwareRoutePlannerV2(dict(run["parameters"]))
    return planner.plan(
        route, context["grid"], context["grid_risk"], constraints,
        context["airspace_eligibility"],
    )


def _quality_expectations(case, planner_id, evaluation):
    """Report the declared expectation as an observation, never as a pass/fail."""

    expectation = dict(case.get("quality_expectation") or {})
    observations = []
    if expectation.get("expect_input_rejected"):
        observations.append({
            "expectation": "input_rejected",
            "observed": evaluation["hard_constraint_input"]["status"] == "rejected",
        })
    detour = evaluation["quality"]["detour_factor"]
    if "detour_factor_max" in expectation:
        observations.append({
            "expectation": f"detour_factor<={expectation['detour_factor_max']}",
            "observed": detour is not None and detour <= expectation["detour_factor_max"],
        })
    if "detour_factor_min" in expectation:
        observations.append({
            "expectation": f"detour_factor>={expectation['detour_factor_min']}",
            "observed": detour is not None and detour >= expectation["detour_factor_min"],
        })
    if "turn_count" in expectation:
        observations.append({
            "expectation": f"turn_count=={expectation['turn_count']}",
            "observed": evaluation["quality"]["turn_count"] == expectation["turn_count"],
        })
    if expectation.get("expect_v2_failed") and planner_id == V2:
        observations.append({
            "expectation": "planner_status==failed",
            "observed": evaluation["status"] == "failed",
        })
    return {
        "declared": expectation,
        "observations": observations,
        "used_for_scoring": False,
        "note": "期望值仅作为可复现观察记录；本报告不据此排名或评分。",
    }


def _run_case(case, *, runs, warmup):
    context = _planner_context(case)
    planners = {}
    for run in _planner_runs(case):
        planner_id, run_id = run["planner_id"], run["run_id"]
        applicability = _applicability(case, planner_id)
        if not applicability["applicable"]:
            planners[run_id] = {
                "run_id": run_id,
                "planner": planner_id,
                "effective_parameters": dict(run["parameters"]),
                "status": applicability["status"],
                "applicability": applicability,
                "planner_invoked": False,
                "input_fingerprint": None,
                "reason": applicability["detail"],
                "runtime_ms": None,
                "runtime_statistics": None,
                "quality": None,
                "risk_recomputed": False,
                "verdicts": {
                    "automatically_ranked": False,
                    "automatically_scored": False,
                    "preferred_algorithm": None,
                },
            }
            continue
        result, repetitions, runtime_statistics = _repeat_run(
            run, case, context, runs=runs, warmup=warmup,
        )
        evaluation = evaluate_route_quality(
            context["route"], result, context["hard_constraints"],
            runtime_statistics["runtime_ms"]["median"],
            grid=context["grid"], airspace_eligibility=context["airspace_eligibility"],
        )
        evaluation["deterministic_consistency"] = runtime_statistics["deterministic_consistency"]
        planners[run_id] = {
            "run_id": run_id,
            "planner": planner_id,
            "effective_parameters": dict(run["parameters"]),
            "status": evaluation["status"],
            "applicability": applicability,
            "planner_invoked": True,
            "input_fingerprint": evaluation["input_fingerprint"],
            "result_fingerprint": runtime_statistics["result_fingerprint"],
            "path_fingerprint": runtime_statistics["path_fingerprint"],
            "reason": evaluation["reason"],
            "algorithm_id": evaluation["algorithm_id"],
            "algorithm_version": evaluation["algorithm_version"],
            "runtime_ms": runtime_statistics["runtime_ms"]["median"],
            "runtime_statistics": runtime_statistics,
            "repetitions": repetitions,
            "quality": evaluation["quality"],
            "planner_reported": evaluation["planner_reported"],
            "planner_reported_vs_measured": evaluation["planner_reported_vs_measured"],
            "risk_metrics": evaluation["risk_metrics"],
            "risk_metrics_source": evaluation["risk_metrics_source"],
            "risk_recomputed": False,
            "grid_behavior": evaluation["grid_behavior"],
            "constraint_input_summary": evaluation["constraint_input_summary"],
            "metric_semantics": evaluation["metric_semantics"],
            "hard_constraint_input": evaluation["hard_constraint_input"],
            "allowed_airspace_feasibility": evaluation["allowed_airspace_feasibility"],
            "quality_expectations": _quality_expectations(case, planner_id, evaluation),
            "verdicts": evaluation["verdicts"],
            "path_point_count": len(result.get("path") or []),
        }
    return {
        "case_id": case["case_id"],
        "description": case["description"],
        "expert_question": case["expert_question"],
        "demonstration": deepcopy(case.get("demonstration")),
        "application": case["application"],
        "planner_changes_allowed": False,
        "input_summary": {
            "workspace_bbox": case["workspace_bbox"],
            "workspace_level": case["workspace_level"],
            "grid_cell_count": (context["grid"] or {}).get("count"),
            "grid_cell_size_degrees": (context["grid"] or {}).get("cell_size_degrees"),
            "scenario_route_id": context["route"]["route_id"],
            "start": context["route"]["start"],
            "end": context["route"]["end"],
            "hard_constraint_count": len(context["hard_constraints"]),
            "hard_constraints": case["hard_constraints"],
            "blocked_boxes": case["blocked_boxes"],
            "synthetic_risk_band": (case.get("risk") or {}).get("band"),
            "v2_parameter_variants": case.get("v2_parameter_variants") or [],
            "airspace_eligibility_status": (context["airspace_eligibility"] or {}).get("status"),
            "airspace_allowed_grid_ids": len(
                (context["airspace_eligibility"] or {}).get("allowed_grid_ids") or []
            ),
            "input_boundary_rejection": context.get("input_rejection"),
        },
        "input_fingerprint": sha256(
            json.dumps({
                "case_id": case["case_id"],
                "start": case["start"], "end": case["end"],
                "hard_constraints": case["hard_constraints"],
                "blocked_boxes": case["blocked_boxes"],
                "risk": case.get("risk"),
                "v2_parameter_variants": case.get("v2_parameter_variants") or [],
            }, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "planners": planners,
    }


def _planner_manifest_block(registry):
    blocks = {}
    for planner_id in PLANNERS:
        selection = next(
            item for item in registry.manifests("route_planner") if item.algorithm_id == planner_id
        )
        blocks[planner_id] = selection.to_dict()
    return blocks


def _case_summary(results):
    rows = []
    for item in results:
        rows.append({
            "case_id": item["case_id"],
            "runs": [
                {
                    "run_id": run_id,
                    "planner": block["planner"],
                    "status": block["status"],
                    "planner_invoked": block["planner_invoked"],
                    "deterministic_consistency": (
                        (block.get("runtime_statistics") or {}).get("deterministic_consistency")
                    ),
                    "runtime_median_ms": (
                        ((block.get("runtime_statistics") or {}).get("runtime_ms") or {}).get("median")
                    ),
                }
                for run_id, block in item["planners"].items()
            ],
        })
    return rows


def _project_markdown(lines, project):
    lines += ["", "## 5. 项目级证据（route experiments / reference comparison / data readiness）", ""]
    if project["status"] != "passed":
        lines += [
            f"- 状态：**NOT READY** — `{project['status']}` / `{project.get('not_ready_reason')}`",
            f"- {project.get('note') or '未提供可读取的项目文件，未生成真实数据证据，也未造数据。'}",
            "",
        ]
        return lines
    readiness = project.get("data_readiness") or {}
    lines += [
        f"- 项目：`{project['project_path']}`",
        f"- reference source CRS 已确认：`{project['reference_source_crs_resolved']}`",
        f"- 数据就绪总体状态：`{readiness.get('status')}`",
        "",
        "### 5.1 数据就绪",
        "",
        "| 数据块 | status | count | source_crs | source_crs 已确认 | 米制度量 |",
        "|---|---|---|---|---|---|",
    ]
    for name, block in (readiness.get("blocks") or {}).items():
        source_crs = (block.get("source_crs") or {}).get("value") or "—"
        lines.append(
            f"| `{name}` | `{block.get('status')}` | {block.get('count')} | `{source_crs}` | "
            f"`{block.get('source_crs_resolved')}` | `{block.get('metric_measurement_status')}` |"
        )
    policies = (readiness.get("blocks") or {}).get("airspace_policies") or {}
    lines += [
        "",
        f"- AirspacePolicy：count={policies.get('count')} · confirmed={policies.get('confirmed_count')} · "
        f"allowed/blocked/unknown={json.dumps(policies.get('route_eligibility_counts') or {}, ensure_ascii=False)}",
        f"- V2 readiness：`{(policies.get('v2_readiness') or {}).get('status')}` "
        f"（原因 `{(policies.get('v2_readiness') or {}).get('reason')}`）",
        f"- ET 政策：`{readiness.get('et_source_policy')}`（不提供 ET parser）",
        f"- reference route link 数：{readiness.get('reference_route_link_count')} · experiment 数：{readiness.get('experiment_count')}",
        "",
        "### 5.2 Route experiments（实验 ≠ current operational route）",
        "",
    ]
    experiments = project.get("route_experiments") or {}
    records = experiments.get("records") or []
    if not records:
        lines.append("- 尚无 experiment 记录；未生成实验证据。")
    else:
        for record in records[:5]:
            lines.append(f"- `{record['experiment_id']}` · applicability=`{record.get('current_applicability')}` · "
                         f"grounding=`{record.get('grounding')}` · runs={len(record.get('runs') or [])}")
            for run in record.get("runs") or []:
                evaluation = run.get("quality") or {}
                quality = evaluation.get("quality") or {}
                stats = ((run.get("runtime") or {}).get("per_route_ms_stats") or {})
                lines.append(
                    f"  - `{run['run_id']}` `{run['status']}` · params "
                    f"`{json.dumps(run.get('effective_parameters') or {}, ensure_ascii=False, sort_keys=True)}` · "
                    f"len={_fmt(quality.get('path_length_m'))} m · determinism=`{evaluation.get('deterministic_consistency')}` · "
                    f"median(per route)={_fmt(stats.get('median'))} ms"
                )
    lines += ["", "### 5.3 Reference comparison（仅描述差异）", ""]
    comparisons = project.get("reference_comparisons") or {}
    lines.append(
        f"- 状态：`{comparisons.get('status')}` · 可用比较 {comparisons.get('comparison_count', 0)} · "
        f"被阻断 {comparisons.get('blocked_count', 0)} · reference route 数 {comparisons.get('reference_route_count', 0)} · "
        f"link 数 {comparisons.get('link_count', 0)}"
    )
    for item in comparisons.get("comparisons") or []:
        geodesic = item.get("geodesic") or {}
        plane = item.get("metric_plane") or {}
        lines += [
            f"- `{item['reference_route_id']}` ↔ `{item['scenario_route_id']}`："
            f"reference={_fmt(geodesic.get('reference_length_m'))} m · planned={_fmt(geodesic.get('planned_length_m'))} m · "
            f"Δ={_fmt(geodesic.get('length_delta_m'))} m · ratio={_fmt(geodesic.get('length_ratio'), 4)} · "
            f"start offset={_fmt(geodesic.get('start_offset_m'))} m · end offset={_fmt(geodesic.get('end_offset_m'))} m",
            f"  - 米制平面：Hausdorff={_fmt(plane.get('hausdorff_distance_m'))} m · "
            f"Fréchet={_fmt(plane.get('discrete_frechet_distance_m'))} m · "
            f"projection=`{(plane.get('projection') or {}).get('label')}` · spacing={_fmt(plane.get('sample_spacing_m'))} m",
            f"  - similarity_score=`{item.get('similarity_score')}` · ranking=`{item.get('ranking')}` · verdict=`{item.get('verdict')}`",
        ]
    for item in comparisons.get("blocked") or []:
        lines.append(
            f"- NOT READY `{item.get('reference_route_id')}` ↔ `{item.get('scenario_route_id')}`："
            f"原因 `{', '.join(item.get('reasons') or [])}`"
        )
    if not comparisons.get("comparisons") and not comparisons.get("blocked"):
        lines.append("- 尚无已确认的 reference route ↔ scenario route link，因此没有比较结果；系统不会自动关联。")
    lines.append("")
    return lines


def _markdown(pack):
    environment = pack["environment"]
    protocol = pack["repetition_protocol"]
    lines = [
        "# 航路规划专家证据包 V2（合成 benchmark + 项目证据）",
        "",
        f"- 生成时间（UTC）：{pack['generated_at']}",
        f"- 代码版本：`{pack['git_revision']}`",
        f"- 工具版本：`route_planning_baseline.py@{pack['tool_version']}` / schema `{pack['schema_version']}`",
        f"- Python：{environment['python']}（{environment.get('python_implementation')}） · 平台：{environment['platform']}",
        f"- 主机：{environment.get('hostname')} · CPU：{environment.get('cpu_count')} · 测地后端：`{environment.get('geodesic_backend')}`",
        f"- 重复协议：runs={protocol['runs']} · warmup={protocol['warmup']} · 统计={', '.join(protocol['statistics'])} · seed={protocol['seed']}",
        f"- 合成算例：确定性生成几何，无真实数据、无 QGIS、无网络。",
        "",
        "> 本证据包为专家评审材料：只报告 manifest、输入、状态、质量指标、重复统计与失败原因，",
        "> **不做自动排名、评分或算法推荐**。runtime 统计仅用于报告。",
        "",
        "## 1. 当前规划器 Manifest",
        "",
    ]
    for planner_id, manifest in pack["planner_manifests"].items():
        lines += [
            f"### `{manifest['algorithm_id']}@{manifest['version']}`",
            "",
            f"- 名称：{manifest['name']}",
            f"- 成熟度：`{manifest['maturity']}` · 提供方：{manifest['provider']}",
            f"- 说明：{manifest['description']}",
            f"- 输入：{', '.join(f'`{item}`' for item in manifest['inputs'])}",
            f"- 输出：{', '.join(f'`{item}`' for item in manifest['outputs'])}",
            f"- 参数 schema：`{json.dumps(manifest['parameter_schema'], ensure_ascii=False, sort_keys=True)}`",
            "- 假设：",
            *[f"  - {item}" for item in manifest["assumptions"]],
            "- 局限：",
            *[f"  - {item}" for item in manifest["limitations"]],
            "",
        ]
    lines += ["## 2. 质量度量口径", "",
              f"- 距离：`{pack['metric_semantics']['distance']}` · 椭球：{pack['metric_semantics']['ellipsoid']}",
              f"- 航向：`{pack['metric_semantics']['bearing']}` · 约定：{pack['metric_semantics']['bearing_convention']}",
              f"- 航向变化范围：{pack['metric_semantics']['heading_change_range_deg']} · 容差：{pack['metric_semantics']['heading_change_tolerance_deg']}°",
              f"- 后端：`{pack['metric_semantics']['backend']}`", "",
              "## 3. 用例状态总览", "", "| case | planner run | status | deterministic | runtime median ms |", "|---|---|---|---|---|"]
    for row in pack["case_summary"]:
        for index, run in enumerate(row["runs"]):
            case_label = f"`{row['case_id']}`" if index == 0 else ""
            lines.append(
                f"| {case_label} | `{run['run_id']}` | `{run['status']}` | "
                f"`{run.get('deterministic_consistency')}` | {_fmt(run.get('runtime_median_ms'))} |"
            )
    lines += ["", "## 4. 逐用例输入与结果（含重复统计）", ""]
    for item in pack["cases"]:
        summary = item["input_summary"]
        lines += [
            f"### `{item['case_id']}` — {item['description']}",
            "",
            f"- 适用模式：`{item['application']}`（不允许为通过而修改 planner：`{item['planner_changes_allowed']}`）",
            f"- 工作区：`{summary['workspace_bbox']}` level {summary['workspace_level']}",
            f"- 网格：{summary['grid_cell_count']} 格 · 格尺寸（度）{summary['grid_cell_size_degrees']}",
            f"- 场景航路：`{summary['scenario_route_id']}` {summary['start']} → {summary['end']}",
            f"- 硬约束：{summary['hard_constraint_count']} 项 `{json.dumps(summary['hard_constraints'], ensure_ascii=False)}`",
            f"- 合成阻断盒：`{json.dumps(summary['blocked_boxes'])}`",
            f"- 合成风险带：`{json.dumps(summary['synthetic_risk_band'])}`",
            f"- V2 参数变体：`{json.dumps(summary['v2_parameter_variants'], ensure_ascii=False)}`",
            f"- allowed airspace：`{summary['airspace_eligibility_status']}` / {summary['airspace_allowed_grid_ids']} 格",
        ]
        if summary["input_boundary_rejection"]:
            lines.append(f"- 输入边界拒绝：{summary['input_boundary_rejection']}")
        lines.append("")
        for run_id, block in item["planners"].items():
            heading = f"**{run_id}** — `{block['status']}`"
            if block.get("effective_parameters"):
                heading += f" · 有效参数 `{json.dumps(block['effective_parameters'], ensure_ascii=False, sort_keys=True)}`"
            lines.append(heading)
            lines.append("")
            if not block["planner_invoked"]:
                lines.append(f"- planner 未被调用：{block['reason']}")
                lines.append("")
                continue
            quality = block["quality"] or {}
            runtime = block.get("runtime_statistics") or {}
            stats = runtime.get("runtime_ms") or {}
            lines += [
                f"- 调用：`{block['algorithm_id']}@{block['algorithm_version']}` · fingerprint `{block['input_fingerprint']}`",
                f"- 结果原因：{block['reason']}",
                f"- 质量指标：path_length_m={_fmt(quality.get('path_length_m'))} · "
                f"detour_factor={_fmt(quality.get('detour_factor'), 4)} · "
                f"segment_count={quality.get('segment_count')} · turn_count={quality.get('turn_count')} · "
                f"total_heading_change_deg={_fmt(quality.get('total_heading_change_deg'))} · "
                f"max_heading_change_deg={_fmt(quality.get('max_heading_change_deg'))} · "
                f"min_segment_m={_fmt(quality.get('min_segment_m'))}",
                f"- 硬约束可行性：`{block['hard_constraint_input']['status']}`（{block['hard_constraint_input']['detail']}）",
                f"- allowed 可行性：`{block['allowed_airspace_feasibility']['status']}`（来自 planner status，未重新判定）",
                f"- 风险指标：`{json.dumps(block['risk_metrics'], ensure_ascii=False)}`（来源 `{block['risk_metrics_source']}`，未重算风险）",
                f"- planner 自报 vs 实测（长度/绕行差）："
                f"{_fmt(block['planner_reported_vs_measured'].get('path_length_m_delta'))} m / "
                f"{_fmt(block['planner_reported_vs_measured'].get('detour_factor_delta'), 6)}",
                f"- 重复统计（runs={stats.get('count')}，warmup={runtime.get('warmup_runs')}）："
                f"min={_fmt(stats.get('min'))} · median={_fmt(stats.get('median'))} · "
                f"p95={_fmt(stats.get('p95'))} · max={_fmt(stats.get('max'))} ms"
                f"（stdev={_fmt(stats.get('stdev'))}，仅报告）",
                f"- 确定性：result/path fingerprint 稳定性 `{runtime.get('deterministic_consistency')}`"
                f"（distinct result={runtime.get('distinct_result_fingerprints')} · "
                f"distinct path={runtime.get('distinct_path_fingerprints')}）· seed `{runtime.get('seed')}`",
                f"- result fingerprint：`{block.get('result_fingerprint')}` · path fingerprint：`{block.get('path_fingerprint')}`",
            ]
            for observation in block["quality_expectations"]["observations"]:
                lines.append(
                    f"- 声明期望 `{observation['expectation']}` → 观察结果 `{observation['observed']}`（不用于评分）"
                )
            if block["planner_reported"]:
                lines.append(
                    f"- planner 自报指标：`{json.dumps(block['planner_reported'], ensure_ascii=False)}`"
                )
            lines.append("")
    lines += _project_markdown([], pack["project_evidence"])
    lines += [
        "## 6. 复现",
        "",
        "```powershell",
        "# 合成 benchmark（重复统计）",
        f"python tools/route_planning_baseline.py --runs {protocol['runs']} --warmup {protocol['warmup']}",
        "",
        "# 追加项目级证据（experiments / reference comparison / data readiness）",
        f"python tools/route_planning_baseline.py --project <project.json>",
        "```",
        "",
        f"生成文件：`{pack['artifacts']['json']}` 与 `{pack['artifacts']['markdown']}`。",
        "",
        "## 7. 已知局限与待专家决策",
        "",
        "### 已知局限",
        "",
        "- 合成算例只覆盖几何/硬约束/风险带语义，不代表真实空域复杂度。",
        "- V1 为固定 56×56 经纬度网格 + BBOX 硬约束，非米制搜索，且不读取风险/高度/运动学。",
        "- V2 为二维战略水平规划，输出 MH/T 网格中心二维航路，不做平滑，不含高度。",
        "- 重复统计只描述同一进程内的 wall-clock 分布，不是跨机器性能基准。",
        "- 未提供 `--project` 或项目缺少参考航线/确认 CRS 时，真实数据证据为 NOT READY。",
        "",
        "### 待专家决策（本轮不决定）",
        "",
        "- D1：BS 是否复用航空器尺寸/速度参数，还是保持独立 BBOX；",
        "- D2：BBOX 硬约束是否升级为 polygon/精确几何边界；",
        "- D3：垂直间隔与高度层规则如何确定；",
        "- 是否引入 Theta*/RRT/Dubins/V3 或路径平滑；",
        "- V2 `risk_weight_lambda` 与最大相对风险阈值是否存在工程/运行依据；",
        "- reference route 与 OD 的关联口径（端点距离阈值、是否要求同一来源批次）。",
        "",
        "## 8. 明确未做的事",
        "",
        "- 未修改 V1/V2 路径搜索核心、代价公式、邻接或输出契约；未为了让任何 case 通过而调整 planner。",
        "- 未新选算法，未实现 Theta*/RRT/Dubins/V3，未决定垂直间隔或 BBOX→polygon 变更。",
        "- 未修改 RiskModel、AirspacePolicy 规则或 BuildingClearance。",
        "- 未排名、未评分、未推荐算法。",
        "- 未实现 ET parser；ET 仍要求人工转换为 XLSX/CSV。",
        "",
    ]
    return "\n".join(lines)


def _fmt(value, digits=3):
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return str(value)


def _project_evidence(project_path):
    """Project-level evidence: experiments, reference comparisons, data readiness.

    Read-only.  Missing or unresolved inputs are reported as ``NOT READY`` with
    explicit blockers; nothing is fabricated.
    """

    if not project_path:
        return {
            "status": "not_supplied",
            "project_path": None,
            "not_ready_reason": "no_project_path_argument",
            "route_experiments": None,
            "reference_comparisons": None,
            "data_readiness": None,
            "note": "未提供 --project，跳过项目级真实数据证据；本文件其余部分仍是合成算例证据。",
        }
    path = Path(project_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return {
            "status": "not_found",
            "project_path": str(path),
            "not_ready_reason": "project_file_not_found",
            "route_experiments": None,
            "reference_comparisons": None,
            "data_readiness": None,
        }
    from cns_planner.application.workflow_service import WorkflowService

    workflow = WorkflowService(path, DEFAULTS_PATH)
    experiments = workflow.route_experiments_snapshot()
    readiness = workflow.data_readiness_snapshot()
    comparisons = experiments.get("reference_comparisons") or {}
    reference_routes = workflow.state.get("reference_routes") or {}
    return {
        "status": "passed",
        "project_path": str(path),
        "not_ready_reason": None,
        "route_experiments": experiments,
        "reference_comparisons": comparisons,
        "data_readiness": readiness,
        "reference_source_crs_resolved": is_resolved(reference_routes.get("crs"), role="source_crs"),
        "note": (
            "真实数据证据只在该项目实际包含参考航线/实验时才有内容；"
            "CRS 未确认时明确 NOT READY，不造数据。"
        ),
    }


def build_pack(case_ids=None, *, runs=DEFAULT_RUNS, warmup=DEFAULT_WARMUP, project_path=None):
    registry = build_default_algorithm_registry(json.loads(DEFAULTS_PATH.read_text(encoding="utf-8")))
    selected = benchmark_fixtures.cases()
    if case_ids:
        selected = [item for item in selected if item["case_id"] in set(case_ids)]
        if not selected:
            raise SystemExit(f"没有匹配的 case：{sorted(case_ids)}")
    if runs < 1:
        raise SystemExit("--runs 必须 >= 1")
    if warmup < 0:
        raise SystemExit("--warmup 必须 >= 0")
    results = [_run_case(item, runs=runs, warmup=warmup) for item in selected]
    project = _project_evidence(project_path)
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now(),
        "git_revision": _git_revision(),
        "environment": _environment(),
        "metric_semantics": dict(METRIC_SEMANTICS),
        "repetition_protocol": {
            "runs": runs,
            "warmup": warmup,
            "statistics": ["min", "median", "p95", "max"],
            "deterministic_consistency_check": "result_and_path_fingerprint_stability",
            "seed": None,
            "seed_note": "为未来随机 planner 预留；当前 V1/V2 确定性算法不使用 seed。",
            "ompl_reference": (
                "重复运行/预热/统计口径参考 OMPL benchmark 语义；"
                "本工具不引入、不依赖 OMPL。"
            ),
            "used_for_ranking": False,
        },
        "data_provenance": {
            "source_type": "synthetic",
            "real_world_data": False,
            "qgis_used": False,
            "network_used": False,
            "deterministic": True,
        },
        "scope_statement": (
            "为航路规划专家评审准备的可信 baseline 证据包；"
            "不自动排名、评分或推荐算法。"
        ),
        "planner_changes_allowed": False,
        "planner_manifests": _planner_manifest_block(registry),
        "case_summary": _case_summary(results),
        "cases": results,
        "project_evidence": project,
        "not_done": [
            "未修改 V1/V2 路径搜索核心、代价公式、邻接或输出契约",
            "未把 BBOX 硬约束改为 polygon",
            "未决定垂直间隔",
            "未实现 Theta*/RRT/Dubins/V3",
            "未修改 RiskModel、AirspacePolicy 规则或 BuildingClearance",
            "未排名/评分/推荐算法",
        ],
    }


def write_pack(pack, out_dir):
    out_dir = Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "route_planning_baseline.json"
    markdown_path = out_dir / "route_planning_baseline.md"
    pack = deepcopy(pack)
    def display_path(path):
        try:
            return str(path.relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            return str(path)

    pack["artifacts"] = {
        "json": display_path(json_path),
        "markdown": display_path(markdown_path),
    }
    json_path.write_text(
        json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8",
    )
    markdown_path.write_text(_markdown(pack), encoding="utf-8")
    return json_path, markdown_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="航路规划专家证据包（合成 benchmark + 项目证据）")
    parser.add_argument("--out-dir", default="outputs/route_baseline")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"每个 planner run 的重复测量次数（默认 {DEFAULT_RUNS}）")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP,
                        help=f"正式测量前的预热次数（默认 {DEFAULT_WARMUP}）")
    parser.add_argument("--project", default=None,
                        help="可选：项目 JSON 路径，用于读取实验/参考比较/数据就绪证据")
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    pack = build_pack(
        arguments.case, runs=arguments.runs, warmup=arguments.warmup,
        project_path=arguments.project,
    )
    json_path, markdown_path = write_pack(pack, arguments.out_dir)
    if not arguments.quiet:
        for row in pack["case_summary"]:
            joined = "  ".join(f"{run['run_id']}={run['status']}" for run in row["runs"])
            print(f"{row['case_id']:<32} {joined}")
        project = pack["project_evidence"]
        print(f"\nproject evidence : {project['status']}"
              + (f" ({project['not_ready_reason']})" if project.get("not_ready_reason") else ""))
        print(f"runs/warmup      : {pack['repetition_protocol']['runs']}/{pack['repetition_protocol']['warmup']}")
        print(f"JSON     : {json_path}")
        print(f"Markdown : {markdown_path}")
        print("提示：本证据包不排名/评分/推荐算法；runtime 统计仅用于报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
