#!/usr/bin/env python3
"""Generate the fixed expert-consultation brief from descriptive evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = _load("route_planning_baseline_for_brief", "route_planning_baseline.py")
DIAGNOSTICS = _load("route_planning_diagnostics_for_brief", "route_planning_diagnostics.py")

EXPERT_QUESTIONS = [
    {"id": "P1", "question": "战略航路应采用 2D 水平规划 + 独立高度，还是三维联合规划？"},
    {"id": "P2", "question": "MH/T 网格应作为搜索空间，还是仅作为风险索引？"},
    {"id": "P3", "question": "约束几何应采用 polygon、栅格还是混合表达？"},
    {"id": "P4", "question": "转弯半径、航向、爬升等运动学条件应进入搜索还是后处理？"},
    {"id": "P5", "question": "距离 + 风险应采用加权和、约束、分层还是 Pareto 表达？"},
    {"id": "P6", "question": "A* 格网偏置应采用 any-angle、平滑还是运动学 planner 哪类思路？"},
    {"id": "P7", "question": "网格分辨率依赖应如何处理、报告和验证？"},
    {"id": "P8", "question": "应如何定义验证指标，以及真实航线应作为何种用途的证据？"},
]

OPEN_ITEMS = {
    "DATA-1": "reference CRS：舟山参考点/线坐标系待权威确认。",
    "DATA-2": "ET→XLSX/CSV：ET 必须人工转换，系统不解析。",
    "DATA-3": "AirspacePolicy：allowed/blocked/unknown 与 confirmed 需逐 feature 确认。",
    "EXPERT-1": "constraint geometry：BBOX/polygon/raster/混合表达。",
    "EXPERT-2": "vertical/altitude：二维+独立高度或三维联合规划。",
    "EXPERT-3": "state space/algorithm：搜索状态空间与算法类别。",
    "EXPERT-4": "kinematics：运动学约束进入搜索或后处理。",
    "EXPERT-5": "objective/risk cost：距离与风险的目标表达。",
}

_ACTION_ITEMS = {
    "DATA-1": "由数据方给出权威 CRS 证据并写入 reference source_crs（禁止猜测 WGS84/CGCS2000）。",
    "DATA-2": "人工把 .et 转换为 XLSX/CSV 后重新导入；系统不实现 ET parser。",
    "DATA-3": "逐 AirspaceFeature 显式确认 allowed/blocked/unknown 与 confirmed（禁止按图层名或颜色推断）。",
}


def real_data_verdict(project_evidence):
    """Explicit DATA-1/2/3 readiness verdict, or an honest NOT READY reason.

    The project-level ``status`` only says whether the project file could be read and
    its evidence collected.  It is NOT a real-data readiness statement, so this helper
    derives the readiness the expert brief actually needs to show.
    """

    if not isinstance(project_evidence, dict) or project_evidence.get("status") != "passed":
        return {
            "status": "NOT_READY",
            "reason": (project_evidence or {}).get("not_ready_reason") or "project_evidence_unavailable",
            "detail": (project_evidence or {}).get("note"),
            "data_readiness": None,
            "pending_items": ["DATA-1", "DATA-2", "DATA-3"],
            "blocks": {},
            "airspace_policies": {
                "status": None, "count": None, "confirmed_count": None,
                "route_eligibility_counts": None,
                "v2_readiness": {"status": "unknown", "reason": "project_evidence_unavailable"},
                "never_inferred_from_layer_name_or_color": True,
            },
            "items": {
                key: {"item": key, "status": "NOT_READY", "reason": "project_evidence_unavailable",
                      "action": _ACTION_ITEMS[key]}
                for key in ("DATA-1", "DATA-2", "DATA-3")
            },
            "metric_measurement_enabled": False,
            "note": "未提供可读取的项目：真实数据证据 NOT READY，未造数据。",
        }

    readiness = project_evidence.get("data_readiness") or {}
    blocks = readiness.get("blocks") or {}
    landing = blocks.get("reference_landing_sites") or {}
    routes = blocks.get("reference_routes") or {}
    policies = blocks.get("airspace_policies") or {}
    v2 = policies.get("v2_readiness") or {}

    items = {}
    crs_resolved = bool(landing.get("source_crs_resolved")) and bool(routes.get("source_crs_resolved"))
    items["DATA-1"] = {
        "item": "DATA-1",
        "status": "confirmed" if crs_resolved else "NOT_READY",
        "reason": None if crs_resolved else (
            landing.get("unresolved_reason") or routes.get("unresolved_reason")
            or "source_crs_pending_confirmation"
        ),
        "action": _ACTION_ITEMS["DATA-1"],
        "landing_site_source_crs_resolved": bool(landing.get("source_crs_resolved")),
        "reference_route_source_crs_resolved": bool(routes.get("source_crs_resolved")),
        "landing_site_count": landing.get("count"),
        "reference_route_count": routes.get("count"),
    }
    et_only = (
        routes.get("format") == "et"
        or routes.get("status") == "requires_xlsx_or_csv_conversion"
    )
    routes_loaded = routes.get("status") == "passed" and bool(routes.get("count"))
    items["DATA-2"] = {
        "item": "DATA-2",
        "status": "converted" if routes_loaded else "NOT_READY",
        "reason": None if routes_loaded else (
            "requires_xlsx_or_csv_conversion" if et_only
            else "reference_routes_not_imported_from_xlsx_or_csv"
        ),
        "action": _ACTION_ITEMS["DATA-2"],
        "reference_route_format": routes.get("format"),
        "reference_route_status": routes.get("status"),
        "et_parser": readiness.get("et_parser"),
    }
    policy_ready = v2.get("status") == "ready"
    items["DATA-3"] = {
        "item": "DATA-3",
        "status": "confirmed" if policy_ready else "NOT_READY",
        "reason": None if policy_ready else (v2.get("reason") or "no_confirmed_allowed_airspace_policy"),
        "action": _ACTION_ITEMS["DATA-3"],
        "policy_count": policies.get("count"),
        "confirmed_count": policies.get("confirmed_count"),
        "route_eligibility_counts": policies.get("route_eligibility_counts"),
        "v2_readiness": v2.get("status"),
    }
    pending = [key for key, value in items.items() if value["status"] == "NOT_READY"]
    return {
        "status": "NOT_READY" if pending else "READY",
        "reason": None if not pending else "pending_data_items",
        "pending_items": pending,
        "data_readiness": readiness.get("status"),
        "blocks": {
            name: {
                "status": block.get("status"),
                "count": block.get("count"),
                "format": block.get("format"),
                "source_crs_resolved": block.get("source_crs_resolved"),
                "metric_measurement_status": block.get("metric_measurement_status"),
            }
            for name, block in blocks.items() if name != "airspace_policies"
        },
        "airspace_policies": {
            "status": policies.get("status"),
            "count": policies.get("count"),
            "confirmed_count": policies.get("confirmed_count"),
            "route_eligibility_counts": policies.get("route_eligibility_counts"),
            "v2_readiness": v2,
            "never_inferred_from_layer_name_or_color": policies.get(
                "never_inferred_from_layer_name_or_color"
            ),
        },
        "reference_route_link_count": readiness.get("reference_route_link_count"),
        "experiment_count": readiness.get("experiment_count"),
        "metric_measurement_enabled": crs_resolved,
        "items": items,
        "note": (
            "真实数据仍 NOT READY 的条目已逐项列出；禁止猜 CRS、禁止解析 ET、"
            "禁止自动确认 AirspacePolicy。"
        ),
    }


def observed_findings(sensitivities):
    """State what the frozen planners actually did.  Facts only, no recommendation."""

    lambda_rows = sensitivities["lambda_sensitivity"]["rows"]
    grid_rows = sensitivities["grid_sensitivity"]["rows"]
    findings = []

    valued = [row for row in lambda_rows if row.get("risk_exposure_index_m") is not None]
    if valued:
        cheapest = min(valued, key=lambda row: row["length_m"])
        safest = min(valued, key=lambda row: row["risk_exposure_index_m"])
        findings.append({
            "id": "OBS-LAMBDA",
            "observation": (
                "λ 从 0 增大时，V2 用更长路径换取更低 risk exposure，"
                "并在某个 λ 之后收敛到同一路径。"
            ),
            "cheapest_length_m": cheapest["length_m"],
            "cheapest_risk_exposure": cheapest["risk_exposure_index_m"],
            "cheapest_lambda": cheapest["risk_weight_lambda"],
            "lowest_risk_exposure": safest["risk_exposure_index_m"],
            "lowest_risk_exposure_length_m": safest["length_m"],
            "lowest_risk_exposure_lambda": safest["risk_weight_lambda"],
            "distinct_paths": len({row["path_fingerprint"] for row in lambda_rows}),
            "note": "只描述取舍曲线；没有工程依据判定哪个 λ 正确，本材料不推荐取值。",
        })

    if grid_rows:
        coarse, fine = grid_rows[0], grid_rows[-1]
        used = sorted({
            name
            for row in grid_rows
            for name, count in ((row.get("grid_steps") or {}).get("direction_histogram") or {}).items()
            if count
        })
        findings.append({
            "id": "OBS-GRID",
            "observation": (
                "同一 OD 在不同 MH/T level 下路径几何不同：网格越细，路径越贴近目标方位、"
                "zigzag_index 越低，但步数与顶点数越多。"
            ),
            "rows": [
                {
                    "requested_grid_level": row["requested_grid_level"],
                    "actual_grid_level": row["actual_grid_level"],
                    "cell_count": row.get("grid_cell_count"),
                    "length_m": row["length_m"],
                    "detour_factor": row["detour_factor"],
                    "turn_count": row["turn_count"],
                    "zigzag_index": row["zigzag_index"],
                    "direction_histogram": (row.get("grid_steps") or {}).get("direction_histogram"),
                }
                for row in grid_rows
            ],
            "coarse_to_fine_length_delta_m": fine["length_m"] - coarse["length_m"],
            "coarse_to_fine_zigzag_ratio": (
                fine["zigzag_index"] / coarse["zigzag_index"]
                if coarse["zigzag_index"] else None
            ),
            "used_directions": used,
            "note": "只描述分辨率依赖；本材料不推荐 level，也不声明哪条路径更好。",
        })

    if grid_rows and set(used) and len(used) < 8:
        findings.append({
            "id": "OBS-DIRECTION-BIAS",
            "observation": (
                "在斜向 OD 上，8 邻域网格路径只使用了部分方向（本例仅 "
                + "/".join(used)
                + "），其余方向未出现；这是网格离散与 equally-cost 走法的直接结果，"
                "而不是路径被平滑或优化过。"
            ),
            "used_directions": used,
            "unused_directions": sorted(
                {"E", "W", "N", "S", "NE", "NW", "SE", "SW"} - set(used)
            ),
            "note": (
                "只陈述方向分布事实。是否为偏置、是否需要用 any-angle/平滑/运动学方法消除，"
                "属于待专家回答的问题（P6），本材料不代答。"
            ),
        })
    return findings


def build_brief(project_path=None):
    # The brief consumes manifests, per-case status/metrics and project evidence.  It
    # does not report repetition statistics, so a single run per case is enough; the
    # repeated-run evidence belongs to tools/route_planning_baseline.py.
    benchmark = BASELINE.build_pack(runs=1, warmup=0, project_path=project_path)
    sensitivities = DIAGNOSTICS.build_diagnostics()
    project_evidence = benchmark["project_evidence"]
    return {
        "schema_version": 1,
        "title": "航路规划问题定义 + 专家咨询诊断基线",
        "project_and_task": {
            "project": "CNS Planner",
            "task": "战略二维水平航路规划；高度后续独立处理",
            "strategic_route_is_not_tactical_daa": True,
            "experts_use_cns_planner": False,
        },
        "current_data_and_constraints": {
            "problem_definition": "docs/route_planning_problem_definition.md",
            "hard_constraint_contract": "axis_aligned_bbox",
            "airspace_policy": "explicit_confirmed_allowed_only",
            "risk_semantics": "relative_engineering_index_not_probability",
            "real_data_readiness": real_data_verdict(project_evidence),
            "project_evidence_status": project_evidence.get("status"),
            "project_evidence_note": project_evidence.get("note"),
        },
        "v1_v2_mechanisms_and_limitations": benchmark["planner_manifests"],
        "synthetic_benchmark": benchmark["cases"],
        "lambda_sensitivity": sensitivities["lambda_sensitivity"],
        "grid_sensitivity": sensitivities["grid_sensitivity"],
        "observed_findings": observed_findings(sensitivities),
        "typical_problems": [
            "8-neighbour grid direction bias and zigzag geometry",
            "path geometry changes with MH/T grid resolution",
            "BBOX envelope may overblock relative to a source polygon",
            "risk lambda changes length/exposure tradeoff without an approved calibration basis",
            "reference CRS and AirspacePolicy can keep real-data evidence NOT READY",
        ],
        "open_items": OPEN_ITEMS,
        "questions_for_experts": EXPERT_QUESTIONS,
        "scope_guards": [
            "No planner ranking, scoring, parameter recommendation or expert answer is generated.",
            "V1/V2 search core, neighbourhood, cost, hard constraints and output contracts are frozen.",
            "No V3, Theta*, RRT, Dubins or smoothing implementation is included.",
        ],
    }


def _markdown(brief):
    readiness = brief["current_data_and_constraints"]["real_data_readiness"]
    lines = [
        "# 航路规划专家咨询材料", "",
        "## 项目 / 战略航路任务", "",
        "CNS Planner 当前任务是战略二维水平 route；高度后续独立处理。战略规划不等于 DAA 战术避碰。专家只需提供算法/建模思路，不需要使用 CNS Planner。", "",
        "## 当前数据与约束", "",
        "- hard constraint 当前为 axis-aligned BBOX。",
        "- V2 仅使用 confirmed allowed airspace；unknown 不得猜测。",
        "- risk 为相对工程指数，不是概率或法规结论。",
        f"- 真实数据 readiness：**{readiness['status']}**；数据块 readiness=`{readiness.get('data_readiness')}`。", "",
    ]
    if readiness["status"] != "READY":
        lines += [f"- NOT READY 原因：`{readiness.get('reason')}`", ""]
        lines += ["| 条目 | 状态 | 原因 | 需要的人工动作 |", "|---|---|---|---|"]
        for key, item in readiness["items"].items():
            lines.append(
                f"| **{key}** | `{item['status']}` | `{item['reason'] or '—'}` | {item['action']} |"
            )
        lines.append("")
    lines += [
        f"- reference landing sites：`{(readiness.get('blocks') or {}).get('reference_landing_sites', {}).get('count')}` 条 · "
        f"source CRS 已确认 `{(readiness.get('blocks') or {}).get('reference_landing_sites', {}).get('source_crs_resolved')}`",
        f"- reference routes：`{(readiness.get('blocks') or {}).get('reference_routes', {}).get('count')}` 条 · "
        f"format `{(readiness.get('blocks') or {}).get('reference_routes', {}).get('format')}` · "
        f"status `{(readiness.get('blocks') or {}).get('reference_routes', {}).get('status')}`",
        f"- AirspacePolicy：`{readiness['airspace_policies'].get('count')}` 条 · "
        f"confirmed `{readiness['airspace_policies'].get('confirmed_count')}` · "
        f"V2 readiness `{(readiness['airspace_policies'].get('v2_readiness') or {}).get('status')}` "
        f"(`{(readiness['airspace_policies'].get('v2_readiness') or {}).get('reason')}`)",
        f"- 米制度量可用：`{readiness.get('metric_measurement_enabled')}`（{readiness.get('note')}）",
        "",
        "## V1 / V2 机制与已知限制", "",
    ]
    for planner_id, manifest in brief["v1_v2_mechanisms_and_limitations"].items():
        lines += [f"### {planner_id}@{manifest.get('version')}", "", manifest.get("description") or "", ""]
        lines += [f"- {item}" for item in manifest.get("limitations") or []]
        lines.append("")
    lines += ["## Synthetic benchmark", ""]
    for case in brief["synthetic_benchmark"]:
        lines.append(f"- `{case['case_id']}`：{case['description']} 专家问题：{case['expert_question']}")
    lines += ["", "## Lambda sensitivity", "", "仅列事实，不推荐 λ。", ""]
    for row in brief["lambda_sensitivity"]["rows"]:
        lines.append(
            f"- λ={row['risk_weight_lambda']}: status={row['status']}, length={row['length_m']:.3f} m, "
            f"risk_exposure={row['risk_exposure_index_m']}, turns={row['turn_count']}, runtime={row['runtime_ms']:.3f} ms"
        )
    lines += ["", "## Grid sensitivity", "", "仅列分辨率依赖，不推荐 level。", ""]
    for row in brief["grid_sensitivity"]["rows"]:
        lines.append(
            f"- L{row['actual_grid_level']}: cells={row['grid_cell_count']}, status={row['status']}, "
            f"length={row['length_m']:.3f} m, turns={row['turn_count']}, zigzag={row['zigzag_index']:.6f}"
        )
    lines += ["", "## 观测事实（只描述，不推荐）", ""]
    for item in brief.get("observed_findings") or []:
        lines.append(f"### {item['id']}")
        lines.append("")
        lines.append(item["observation"])
        lines.append("")
        if item["id"] == "OBS-GRID":
            lines += ["| level | cells | length_m | detour | turns | zigzag | E/NE |",
                      "|---|---|---|---|---|---|---|"]
            for row in item["rows"]:
                histogram = row["direction_histogram"] or {}
                lines.append(
                    f"| L{row['actual_grid_level']} | {row['cell_count']} | {row['length_m']:.1f} | "
                    f"{row['detour_factor']:.4f} | {row['turn_count']} | {row['zigzag_index']:.6f} | "
                    f"{histogram.get('E', 0)}/{histogram.get('NE', 0)} |"
                )
            lines.append("")
        elif item["id"] == "OBS-DIRECTION-BIAS":
            lines.append(f"- 使用方向：{', '.join(item['used_directions'])}")
            lines.append(f"- 未出现方向：{', '.join(item['unused_directions'])}")
            lines.append("")
        lines.append(f"> {item['note']}")
        lines.append("")
    lines += ["## 典型问题", ""] + [f"- {item}" for item in brief["typical_problems"]]
    lines += ["", "## 真实数据 readiness", "",
              f"整体：**{readiness['status']}** · 数据块 readiness `{readiness.get('data_readiness')}`"]
    for key, item in readiness["items"].items():
        lines.append(
            f"- **{key}** `{item['status']}`：{item.get('reason') or '已满足'} → {item['action']}"
        )
    lines += [""]
    lines += ["## 待专家回答问题", ""] + [f"- **{item['id']}** {item['question']}" for item in EXPERT_QUESTIONS]
    lines += ["", "## DATA / EXPERT 未决项", ""] + [f"- **{key}** {value}" for key, value in OPEN_ITEMS.items()]
    lines += ["", "本材料只陈述问题和现有证据，不代专家回答，不自动评分、排名或推荐参数。", ""]
    return "\n".join(lines)


def write_brief(brief, out_dir="outputs/route_expert_brief"):
    directory = Path(out_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "route_planning_expert_brief.json"
    md_path = directory / "route_planning_expert_brief.md"
    json_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(brief), encoding="utf-8")
    return md_path, json_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成航路规划专家咨询材料")
    parser.add_argument("--project", default=None)
    parser.add_argument("--out-dir", default="outputs/route_expert_brief")
    arguments = parser.parse_args(argv)
    md_path, json_path = write_brief(build_brief(arguments.project), arguments.out_dir)
    print(md_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
