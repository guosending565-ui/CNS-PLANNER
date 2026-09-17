"""Synthetic route-planning benchmark evidence pack (expert review deliverable).

Runs the current V1/V2 planners over the synthetic fixtures in
:mod:`cns_planner.benchmark.fixtures` and writes a JSON + Markdown evidence pack
into the ignored ``outputs/`` tree.

Scope guarantees:

* no planner code is changed to make a case pass;
* a planner is only called when the case declares it applicable, otherwise the case
  is reported as ``not_applicable`` / ``missing_prerequisite``;
* the malformed-constraint case stops at the Application input boundary, so no
  planner is invoked at all;
* the pack never ranks, scores or recommends an algorithm.

Usage::

    python tools/route_planning_baseline.py [--out-dir outputs/route_baseline]
                                            [--case open_space] [--quiet]
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
from cns_planner.benchmark.quality import evaluate_route_quality  # noqa: E402
from cns_planner.data.mapping.airspace_eligibility import AirspaceEligibilityService  # noqa: E402
from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2  # noqa: E402


TOOL_VERSION = "1.0"
PACK_SCHEMA_VERSION = "route-baseline-evidence-1"
V1 = "route_planner_v1"
V2 = "risk_aware_route_planner_v2"
PLANNERS = (V1, V2)
DEFAULTS_PATH = ROOT / "cns_planner" / "config" / "defaults.json"

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


def _workspace_grid(case):
    service = WorkspaceGridService(preferred_level=case["workspace_level"])
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


def _airspace_eligibility(case, grid):
    """Synthetic confirmed allowed airspace = workspace shell (blocks stay excluded).

    The shell polygon is confirmed ``allowed``; blocked boxes are NOT declared
    ``blocked`` here, so they remain inside the allowed geometry and must be
    excluded by the planner's own hard-constraint handling.  That keeps the
    allowed-airspace fixture and the hard-constraint fixture independently
    observable instead of double-counting the same obstacle.
    """

    service = AirspaceEligibilityService()
    shell = _boxes_to_rectangles(case, grid)[0]
    feature_id = "SYN-ALLOWED-SHELL"
    result = service.build(
        grid.get("cells") or [],
        [{
            "feature_id": feature_id,
            "name": "合成 allowed 工作区外壳",
            "geometry": _rectangle_geometry(shell),
        }],
        {"items": [{
            "feature_id": feature_id,
            "route_eligibility": "allowed",
            "confirmed": True,
            "source": "synthetic_benchmark_fixture",
        }]},
    )
    result["synthetic_fixture"] = True
    return result


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


def _planner_context(case):
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
    grid = _workspace_grid(case)
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


def _run_planner(run, case, context):
    route = context["route"]
    constraints = context["hard_constraints"]
    if run["planner_id"] == V1:
        planner = RoutePlannerV1()
        started = time.perf_counter()
        result = planner.plan(route, case["workspace_bbox"], constraints)
        runtime_ms = (time.perf_counter() - started) * 1000.0
    else:
        planner = RiskAwareRoutePlannerV2(dict(run["parameters"]))
        started = time.perf_counter()
        result = planner.plan(
            route, context["grid"], context["grid_risk"], constraints,
            context["airspace_eligibility"],
        )
        runtime_ms = (time.perf_counter() - started) * 1000.0
    return result, runtime_ms


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


def _run_case(case):
    context = _planner_context(case)
    planners = {}
    for run in _planner_runs(case):
        planner_id, run_id = run["planner_id"], run["run_id"]
        applicability = _applicability(case, planner_id)
        if not applicability["applicable"]:
            planners[run_id] = {
                "planner": planner_id,
                "effective_parameters": dict(run["parameters"]),
                "status": applicability["status"],
                "applicability": applicability,
                "planner_invoked": False,
                "input_fingerprint": None,
                "reason": applicability["detail"],
                "runtime_ms": None,
                "quality": None,
                "risk_recomputed": False,
                "verdicts": {
                    "automatically_ranked": False,
                    "automatically_scored": False,
                    "preferred_algorithm": None,
                },
            }
            continue
        result, runtime_ms = _run_planner(run, case, context)
        evaluation = evaluate_route_quality(
            context["route"], result, context["hard_constraints"], runtime_ms,
        )
        planners[run_id] = {
            "planner": planner_id,
            "effective_parameters": dict(run["parameters"]),
            "status": evaluation["status"],
            "applicability": applicability,
            "planner_invoked": True,
            "input_fingerprint": evaluation["input_fingerprint"],
            "reason": evaluation["reason"],
            "algorithm_id": evaluation["algorithm_id"],
            "algorithm_version": evaluation["algorithm_version"],
            "runtime_ms": runtime_ms,
            "quality": evaluation["quality"],
            "planner_reported": evaluation["planner_reported"],
            "planner_reported_vs_measured": evaluation["planner_reported_vs_measured"],
            "risk_metrics": evaluation["risk_metrics"],
            "risk_metrics_source": evaluation["risk_metrics_source"],
            "risk_recomputed": False,
            "hard_constraint_input": evaluation["hard_constraint_input"],
            "allowed_airspace_feasibility": evaluation["allowed_airspace_feasibility"],
            "quality_expectations": _quality_expectations(case, planner_id, evaluation),
            "verdicts": evaluation["verdicts"],
            "path_point_count": len(result.get("path") or []),
        }
    return {
        "case_id": case["case_id"],
        "description": case["description"],
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
                }
                for run_id, block in item["planners"].items()
            ],
        })
    return rows


def _markdown(pack):
    lines = [
        "# 航路规划基线证据包（合成 benchmark）",
        "",
        f"- 生成时间（UTC）：{pack['generated_at']}",
        f"- 代码版本：`{pack['git_revision']}`",
        f"- 工具版本：`route_planning_baseline.py@{pack['tool_version']}` / schema `{pack['schema_version']}`",
        f"- Python：{pack['environment']['python']} · 平台：{pack['environment']['platform']}",
        f"- 输出：确定性合成算例，无真实数据、无 QGIS、无网络。",
        "",
        "> 本证据包为专家评审材料：只报告 manifest、输入、状态、质量指标与失败原因，",
        "> **不做自动排名、评分或算法推荐**。",
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
    lines += ["## 2. 用例状态总览", "", "| case | planner run | status |", "|---|---|---|"]
    for row in pack["case_summary"]:
        for index, run in enumerate(row["runs"]):
            case_label = f"`{row['case_id']}`" if index == 0 else ""
            lines.append(f"| {case_label} | `{run['run_id']}` | `{run['status']}` |")
    lines += ["", "## 3. 逐用例输入与结果", ""]
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
                f"- runtime_ms（仅报告）：{_fmt(block['runtime_ms'], 3)}",
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
    lines += [
        "## 4. 复现",
        "",
        "```powershell",
        "python tools/route_planning_baseline.py",
        "```",
        "",
        f"生成文件：`{pack['artifacts']['json']}` 与 `{pack['artifacts']['markdown']}`。",
        "",
        "## 5. 明确未做的事",
        "",
        "- 未修改 V1/V2 路径搜索核心、代价公式或输出契约；未为了让任何 case 通过而调整 planner。",
        "- 未新选算法，未实现 Theta*/RRT/Dubins/V3，未决定垂直间隔或 BBOX→polygon 变更。",
        "- 未修改 RiskModel、AirspacePolicy 规则或 BuildingClearance。",
        "- 未排名、未评分、未推荐算法。",
        "",
    ]
    return "\n".join(lines)


def _fmt(value, digits=3):
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return str(value)


def build_pack(case_ids=None):
    registry = build_default_algorithm_registry(json.loads(DEFAULTS_PATH.read_text(encoding="utf-8")))
    selected = benchmark_fixtures.cases()
    if case_ids:
        selected = [item for item in selected if item["case_id"] in set(case_ids)]
        if not selected:
            raise SystemExit(f"没有匹配的 case：{sorted(case_ids)}")
    results = [_run_case(item) for item in selected]
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "generated_at": _now(),
        "git_revision": _git_revision(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
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
        "not_done": [
            "未修改 V1/V2 路径搜索核心、代价公式或输出契约",
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
    pack["artifacts"] = {
        "json": str(json_path.relative_to(ROOT)).replace("\\", "/"),
        "markdown": str(markdown_path.relative_to(ROOT)).replace("\\", "/"),
    }
    json_path.write_text(
        json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8",
    )
    markdown_path.write_text(_markdown(pack), encoding="utf-8")
    return json_path, markdown_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="航路规划基线合成 benchmark 证据包")
    parser.add_argument("--out-dir", default="outputs/route_baseline")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    pack = build_pack(arguments.case)
    json_path, markdown_path = write_pack(pack, arguments.out_dir)
    if not arguments.quiet:
        for row in pack["case_summary"]:
            joined = "  ".join(f"{run['run_id']}={run['status']}" for run in row["runs"])
            print(f"{row['case_id']:<32} {joined}")
        print(f"\nJSON     : {json_path}")
        print(f"Markdown : {markdown_path}")
        print("提示：本证据包不排名/评分/推荐算法；runtime_ms 仅用于报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
