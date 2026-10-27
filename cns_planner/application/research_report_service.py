"""Deterministic research report generated from one published run manifest.

The report never reads a result artifact to *re-derive* a verdict.  Every status,
gate, SHA and limitation comes from the same manifest consumed by the 2D view.
Only the report's own presentation digest is computed here.
"""
from __future__ import annotations

from html import escape
import hashlib
import json
import re

REPORT_VERSION = "c3c-research-report-1"

RADAR_CHAPTER_ORDER = ("适用依据", "缺失输入", "受影响结论", "后续验证条件")


def _radar_paragraphs(radar, s_layer, admission, p18_gate, p18_record):
    """Radar 章节固定四段：适用依据 → 缺失输入 → 受影响结论 → 后续验证条件。"""

    unknown_voxels = (s_layer.get("voxel_counts") or {}).get("unknown")
    gate = (admission.get("p18_confirm_gate") or {})
    gate_text = "、".join(f"{key}={value}" for key, value in sorted(gate.items())) or "unknown"
    applied = [item for item in ((p18_record.get("variants") or []))
               if item.get("source") == "p16_auto"]
    not_applied = (applied[0].get("not_applied_action_ids") or []) if applied else []
    return [
        {"title": "适用依据",
         "text": (
             "Radar 结论只在已提供并确认的 radar_surveillance_layout（站址、型号、方位、"
             "俯仰与探测模型参数）与对应 Radar 政策下评价；本轮研究输入没有该容器，"
             "因此 Radar 不进入任何覆盖计算。")},
        {"title": "缺失输入",
         "text": (
             f"radar_surveillance_layout_present={_fmt(radar.get('radar_surveillance_layout_present'))}；"
             f"reason={_fmt(radar.get('reason')) or 'radar_layout_missing'}；"
             f"formal_status={_fmt(radar.get('formal_status'))}，"
             f"verdict={_fmt(radar.get('verdict'))}。缺失不等于 0，也不等于“未发现威胁”。")},
        {"title": "受影响结论",
         "text": (
             f"S 维持 unknown（unknown 体元 {_fmt(unknown_voxels)}）；RID 的 satisfied 不能补足 "
             f"Radar；P14/P15 为 business_failed（非 identity/stale）；P18 的 Confirm 门禁为 "
             f"{gate_text}，confirmation_allowed=false。")},
        {"title": "后续验证条件",
         "text": (
             "提供真实或经授权、可审计的 radar_surveillance_layout 与对应探测模型后，"
             "重新执行 P14→P15→P18；在此之前不得把 Radar 显示为 satisfied、不得把 unknown "
             "读作 pass，也不得以“排除 Radar”替代评价。P17 的非合作补充层仍为 unknown，"
             "只回答连续中断事件，不构成 Radar 通过结论。")},
    ]


def _action_role(action):
    family = str(action.get("planner_family") or "")
    if (action.get("kind") == "endpoint" or "endpoint_integrity" in family
            or (action.get("segment_index") is None and "middle" not in family)):
        return "endpoint"
    return "middle" if action.get("segment_index") is not None or "middle" in family else "other"

STATUS_LABELS = {
    "P14": "服务走廊业务结论",
    "P15": "能力缺口业务结论",
    "P16": "设施规划提案（仅 proposal）",
    "P17": "连续中断局部门禁",
    "P18": "方案评审投影",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fmt(value):
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _short(value, length=16):
    text = _fmt(value)
    return text if len(text) <= length else text[:length] + "…"


def report_model(manifest):
    """Collect every report fact from the manifest only."""

    business = manifest.get("business_state") or {}
    stages = manifest.get("stages") or {}
    admission = manifest.get("admission") or {}
    p16 = business.get("P16") or {}
    radar = business.get("surveillance_Radar") or {}
    s_layer = business.get("surveillance_S") or {}
    p14 = business.get("P14") or {}
    p15 = business.get("P15") or {}
    nm3 = manifest.get("nm3_supplement") or {}
    input_version = manifest.get("input_version") or {}
    radius = input_version.get("communication_radius") or {}
    params = manifest.get("research_parameters") or {}
    map_projection = manifest.get("map_projection") or {}
    p18_gate = (manifest.get("gates") or {}).get("p18_initialize") or {}
    p18_record = stages.get("P18") or {}
    stage_rows = []
    for stage in ("P14", "P15", "P16", "P17", "P18"):
        entry = stages.get(stage) or {}
        status = entry.get("result_status")
        if stage == "P17":
            status = (manifest.get("gates") or {}).get("p17_step6_gate", {}).get("status", status)
        elif stage == "P18":
            status = p18_gate.get("status", status)
        stage_rows.append({
            "stage": stage,
            "label": STATUS_LABELS[stage],
            "status": _fmt(status),
            "result_sha256": _fmt(entry.get("result_sha256")),
            "payload_sha256": _fmt(entry.get("payload_sha256")),
            "recomputed": entry.get("recomputed_in_this_run"),
            "authority": entry.get("authority"),
        })
    report = {
        "report_version": REPORT_VERSION,
        "run_id": manifest.get("run_id"),
        "task": manifest.get("task"),
        "route_id": manifest.get("route_id"),
        "created_at": manifest.get("created_at"),
        "authority": manifest.get("authority"),
        "formal_project_state_written": manifest.get("formal_project_state_written"),
        "raw_data_written": manifest.get("raw_data_written"),
        "git_written": manifest.get("git_written"),
        "identity": {
            "run_id": manifest.get("run_id"),
            "input_version_id": input_version.get("id"),
            "input_fingerprint": input_version.get("input_fingerprint"),
            "parameter_contract_version": params.get("contract_version"),
            "parameter_fingerprint": params.get("fingerprint"),
            "contract_version": manifest.get("contract_version"),
            "contract_versions": manifest.get("contract_versions") or {},
            "authoritative_result_list_sha256": manifest.get(
                "authoritative_result_list_sha256"),
        },
        "research_input": {
            "source_run_id": (manifest.get("provenance") or {}).get("source_run_id"),
            "source_state_sha256": (manifest.get("provenance") or {}).get("source_state_sha256"),
            "route_id": manifest.get("route_id"),
            "parameters": params,
            "parameter_options": (manifest.get("research_parameter_schema") or {}).get(
                "options") or [],
            "counts": map_projection.get("counts") or {},
        },
        "communication_contract": {
            "is_2km_by_surface": radius.get("communication_is_2km_by_surface"),
            "semantics": radius.get("semantics"),
            "by_service": radius.get("by_service") or {},
            "preparation": (manifest.get("provenance") or {}).get("input_preparation") or {},
        },
        "nm3": {
            "contract_version": nm3.get("contract_version"),
            "policy_id": nm3.get("policy_id"),
            "policy_version": nm3.get("policy_version"),
            "input_fingerprint": nm3.get("input_fingerprint"),
            "responsibilities": nm3.get("responsibilities") or [],
            "residual_gaps": nm3.get("residual_gaps") or [],
            "actual_delivery": nm3.get("actual_delivery", "unknown"),
            "proposal_only": nm3.get("proposal_only", True),
        },
        "nm3_baseline": {
            "semantics": (
                "NM3 基线是**规划前**的 4 项 N 职责与残余缺口；它不含 P16 选中动作，"
                "不得与 P16/P17/P18 的规划投影混读"),
            "contract_version": nm3.get("contract_version"),
            "policy": "%s@%s" % (_fmt(nm3.get("policy_id")), _fmt(nm3.get("policy_version"))),
            "input_fingerprint": nm3.get("input_fingerprint"),
            "responsibilities": nm3.get("responsibilities") or [],
            "residual_gaps": nm3.get("residual_gaps") or [],
        },
        "planning_projection": {
            "semantics": (
                "P16 选中动作 → P17 post-plan 投影 → P18 评审投影；中段动作只提案，"
                "未写入现有 CNS 设施、未 Apply"),
            "p16_status": p16.get("status"),
            "p16_selected_action_count": p16.get("selected_action_count"),
            "actions": [
                {
                    "action_id": action.get("action_id"),
                    "site_id": action.get("site_id"),
                    "segment_index": action.get("segment_index"),
                    "role": _action_role(action),
                    "planner_family": action.get("planner_family"),
                    "planning_status": action.get("planning_status"),
                }
                for action in (map_projection.get("n_actions") or [])
            ],
            "p17_post_plan_status": (manifest.get("gates") or {}).get(
                "p17_step6_gate", {}).get("post_plan_status"),
            "p17_applied_action_ids": (manifest.get("gates") or {}).get(
                "p17_step6_gate", {}).get("applied_action_ids") or [],
            "p18_variants": p18_record.get("variants") or [],
        },
        "coverage": {
            "P14_status": p14.get("status"),
            "P14_subsystems": p14.get("subsystems") or {},
            "P14_endpoint_status": p14.get("endpoint_status"),
            "P15_status": p15.get("status"),
            "P15_subsystems": p15.get("subsystems") or {},
            "C": business.get("communication_C") or {},
            "RID": business.get("navigation_RID") or {},
            "S": s_layer,
            "N": business.get("navigation_N") or {},
            "endpoint_gaps": business.get("navigation_endpoint_gaps") or {},
        },
        "stages": stage_rows,
        "radar": {
            "verdict": radar.get("verdict"),
            "formal_status": radar.get("formal_status"),
            "layout_present": radar.get("radar_surveillance_layout_present"),
            "reason": radar.get("reason"),
            "p17_supplementary_threat_status": radar.get(
                "p17_supplementary_threat_status"),
            "p17_threat_layers": manifest.get("p17_threat_layers") or {},
            "chapter_order": list(RADAR_CHAPTER_ORDER),
            "paragraphs": _radar_paragraphs(
                radar, s_layer, admission, p18_gate, p18_record),
        },
        "p16": {
            "status": p16.get("status"),
            "stop_reason": p16.get("stop_reason"),
            "selected_action_count": p16.get("selected_action_count"),
            "candidate_action_count": p16.get("candidate_action_count"),
            "cost_summary": p16.get("cost_summary"),
            "cost_status": "unknown",
            "proposal_only": True,
        },
        "p18": {
            "status": p18_gate.get("status"),
            "allowed": p18_gate.get("allowed"),
            "blocking_reason": p18_gate.get("blocking_reason"),
            "confirmed": p18_record.get("confirmed", False),
            "applied": p18_record.get("applied", False),
            "confirmation_allowed": admission.get("p18_confirmation_allowed", False),
            "confirm_apply_forbidden": True,
        },
        "actual_delivery": {
            "status": "unknown",
            "installed": False,
            "canonical_published": False,
            "proof": "not_proven",
            "statement": "所有设施/动作均为研究假设；actual_delivery=unknown，未证明实际交付、完好性或最优性。",
        },
        "admission": admission,
        "limitations": manifest.get("research_limitations") or [],
        "confirmation_allowed": False,
        "formal_release_allowed": False,
        "result_shas": {
            "authoritative_result_list_sha256": manifest.get(
                "authoritative_result_list_sha256"),
            "stages": {row["stage"]: row["result_sha256"] for row in stage_rows},
        },
    }
    report["report_payload_sha256"] = _sha(report)
    return report


def _md_table(rows, headers):
    output = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        output.append("| " + " | ".join(_fmt(value).replace("|", "\\|") for value in row) + " |")
    return "\n".join(output)


def build_markdown(manifest):
    model = report_model(manifest)
    identity = model["identity"]
    counts = model["research_input"]["counts"]
    radius = model["communication_contract"]
    report = [
        "# R0005 研究运行报告（research_isolated）",
        "",
        "> 本报告由已发布的同一个 run manifest 生成。研究结果不写入正式 ProjectState，"
        "不得作为正式发布、Confirm 或 Apply 的依据。",
        "",
        "## 1. 身份与结果 SHA",
        "",
        _md_table([
            ("run_id", identity["run_id"]),
            ("input_version", identity["input_version_id"]),
            ("input_fingerprint", identity["input_fingerprint"]),
            ("contract_version", identity["contract_version"]),
            ("authoritative_result_list_sha256", identity["authoritative_result_list_sha256"]),
            ("report_payload_sha256", model["report_payload_sha256"]),
        ], ("字段", "值")),
        "",
        "## 2. 研究输入",
        "",
        _md_table([
            ("route_id", model["route_id"]),
            ("source_run_id", model["research_input"]["source_run_id"]),
            ("source_state_sha256", model["research_input"]["source_state_sha256"]),
            ("站点数", counts.get("sites")),
            ("设备数", counts.get("devices")),
            ("既有宿主", counts.get("existing_hosts")),
            ("规划新建站址", counts.get("planned_new_sites")),
            ("N 动作", counts.get("n_actions")),
        ], ("字段", "值")),
        "",
        "### 可修改研究参数",
        "",
        _md_table([
            ("contract", (model["research_input"]["parameters"] or {}).get("contract_version")),
            ("values", (model["research_input"]["parameters"] or {}).get("values")),
            ("fingerprint", (model["research_input"]["parameters"] or {}).get("fingerprint")),
            ("changed_from_source", (model["research_input"]["parameters"] or {}).get("changed")),
        ], ("字段", "值")),
        "",
        "参数仅改变 N 中段监视宿主这一研究假设；不改变业务门限，也不代表安装或批准。",
        "",
        "## 3. 2 km 通信合同",
        "",
        _md_table([
            ("communication_is_2km_by_surface", radius.get("is_2km_by_surface")),
            ("radius_source", radius.get("semantics")),
            ("provider_sample_count", (radius.get("preparation") or {}).get(
                "communication_provider_sample_count")),
            ("all_effective_radii_2000m", (radius.get("preparation") or {}).get(
                "all_effective_radii_2000m")),
            ("surface_facts_status", (radius.get("preparation") or {}).get(
                "surface_class_facts_status")),
        ], ("字段", "值")),
        "",
        "通信半径来自 device `radius_by_surface` 的逐 provider 解析，不是只改一个 policy 常量。",
        "",
        "## 4. NM3 基线（pre-plan）",
        "",
        "NM3 基线是**规划前**的 4 项 N 职责与残余缺口；它不包含 P16 选中动作，"
        "不得与下面 P16/P17/P18 的规划投影混读。",
        "",
        _md_table([
            (row.get("responsibility_id"), row.get("role"), row.get("segment_index"),
             row.get("planning_status"),
             "%s/%s" % (len(row.get("installed_providers") or []),
                        len(row.get("hypothetical_providers") or [])))
            for row in (model["nm3_baseline"].get("responsibilities") or [])
        ], ("责任", "角色", "航段", "基线状态", "已装/假设 provider")),
        "",
        "### 4.1 P16/P17/P18 规划投影（proposal only）",
        "",
        _md_table([
            (row.get("action_id"), row.get("site_id"),
             row.get("segment_index") if row.get("role") == "middle" else "端点",
             row.get("role"), row.get("planning_status"))
            for row in (model["planning_projection"].get("actions") or [])
        ], ("动作", "站点", "航段", "角色", "状态")),
        "",
        _md_table([
            ("P16 selected_action_count",
             (model["planning_projection"].get("p16_selected_action_count"))),
            ("P17 post_plan_status",
             (model["planning_projection"].get("p17_post_plan_status"))),
            ("P18 p16_auto not_applied_action_ids",
             (model["planning_projection"].get("p18_variants") or [{}])[-1].get(
                 "not_applied_action_ids")),
        ], ("字段", "值")),
        "",
        "**基线与投影的区别：** 基线 4 项职责的 `planning_status=unknown` 是**规划前**事实；"
        "P16/P17/P18 的 satisfied 只属于**规划投影**。不得把基线 unknown 读成规划失败，"
        "也不得把投影 satisfied 读成已安装、已 Confirm 或已 Apply；中段动作未写入现有 CNS 设施。",
        "",
        "## 5. 覆盖与缺口",
        "",
        _md_table([
            ("P14", model["coverage"]["P14_status"], model["coverage"]["P14_subsystems"]),
            ("P15", model["coverage"]["P15_status"], model["coverage"]["P15_subsystems"]),
            ("C", (model["coverage"]["C"] or {}).get("verdict"),
             (model["coverage"]["C"] or {}).get("voxel_counts")),
            ("RID", (model["coverage"]["RID"] or {}).get("verdict"), None),
            ("Radar", (model["radar"]).get("formal_status"), (model["radar"]).get("verdict")),
            ("S", (model["coverage"]["S"] or {}).get("verdict"),
             (model["coverage"]["S"] or {}).get("voxel_counts")),
            ("N", (model["coverage"]["N"] or {}).get("verdict"), None),
        ], ("层", "状态", "证据/计数")),
        "",
        "## 6. P14–P18 状态",
        "",
        _md_table([
            (row["stage"], row["label"], row["status"], _short(row["result_sha256"], 24))
            for row in model["stages"]
        ], ("阶段", "含义", "状态", "结果 SHA（截断）")),
        "",
        "P14/P15 为 business_failed（S 的 Radar 证据未知），不是 identity/stale 失败。"
        "P16 为 proposal_ready，未 Adopt/Confirm/Apply。P18 禁止 Confirm/Apply。",
        "",
        "## 7. Radar 四段说明（适用依据—缺失输入—受影响结论—后续验证条件）",
        "",
    ]
    for paragraph in model["radar"]["paragraphs"]:
        report.append("**%s**：%s" % (paragraph["title"], paragraph["text"]))
        report.append("")
    report.extend([
        "## 8. 成本",
        "",
        "- P16 cost_status: **unknown**",
        "- cost_summary: `%s`" % _canonical(model["p16"]["cost_summary"]),
        "- 不得把未知成本写成 0、估算值或已确认预算。",
        "",
        "## 9. 实际交付未证明",
        "",
        "- actual_delivery: **unknown**",
        "- installed: **false**；canonical_published: **false**",
        "- 未证明实际交付、安装、完好性、最优性或正式发布资格。",
        "",
        "## 10. P18 门禁",
        "",
        _md_table([
            ("p18_initialize.status", model["p18"]["status"]),
            ("p18_initialize.allowed", model["p18"]["allowed"]),
            ("confirmation_allowed", model["p18"]["confirmation_allowed"]),
            ("confirmed", model["p18"]["confirmed"]),
            ("applied", model["p18"]["applied"]),
            ("blocking_reason", model["p18"]["blocking_reason"]),
        ], ("字段", "值")),
        "",
        "## 11. 限制与未证明项",
        "",
    ])
    for item in model["limitations"]:
        report.append("- `%s`: %s" % (
            item.get("limitation_id"), item.get("statement")))
    report.extend([
        "",
        "## 12. 结果清单",
        "",
        _md_table([
            (row["stage"], row["status"], row["result_sha256"], row["payload_sha256"])
            for row in model["stages"]
        ], ("阶段", "状态", "result_sha256", "payload_sha256")),
        "",
        "---",
        "",
        "报告版本：%s · 由 API 从 run manifest 生成 · 不重新计算任何业务结论。"
        % REPORT_VERSION,
    ])
    return "\n".join(report) + "\n"


def _table_html(lines):
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        rows.append(cells)
    if not rows:
        return ""
    header, *body = rows
    output = ["<table><thead><tr>"]
    output.extend("<th>%s</th>" % _inline(cell) for cell in header)
    output.append("</tr></thead><tbody>")
    for row in body:
        output.append("<tr>")
        output.extend("<td>%s</td>" % _inline(cell) for cell in row)
        output.append("</tr>")
    output.append("</tbody></table>")
    return "".join(output)


def _inline(text):
    """最小行内 Markdown：``**粗体**`` 与 `` `代码` ``（先转义，再加标签）。"""

    escaped = escape(str(text))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)
    return escaped


def build_html(manifest, markdown):
    model = report_model(manifest)
    body, table = [], []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("|"):
            table = []
            while index < len(lines) and lines[index].startswith("|"):
                table.append(lines[index])
                index += 1
            body.append(_table_html(table))
            continue
        if line.startswith("# "):
            body.append("<h1>%s</h1>" % _inline(line[2:]))
        elif line.startswith("## "):
            body.append("<h2>%s</h2>" % _inline(line[3:]))
        elif line.startswith("### "):
            body.append("<h3>%s</h3>" % _inline(line[4:]))
        elif line.startswith("> "):
            body.append("<blockquote>%s</blockquote>" % _inline(line[2:]))
        elif line.startswith("- "):
            body.append("<p class=\"bullet\">%s</p>" % _inline(line[2:]))
        elif line == "---":
            body.append("<hr>")
        elif line:
            body.append("<p>%s</p>" % _inline(line))
        index += 1
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>R0005 研究运行报告 %(run)s</title>
<style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1d2a35;margin:0}
main{max-width:1080px;margin:0 auto;padding:32px 40px 80px;background:#fff;min-height:100vh;box-sizing:border-box}
h1{font-size:26px;border-bottom:3px solid #1f6f8b;padding-bottom:12px}
h2{margin-top:34px;font-size:19px;color:#123a5c;border-left:5px solid #1f6f8b;padding-left:10px}
h3{font-size:15px;color:#3b4a57}
table{border-collapse:collapse;width:100%%;margin:10px 0;font-size:13px}
th,td{border:1px solid #cfd8df;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#e9f0f4}
blockquote{background:#fff8e6;border-left:5px solid #b8840f;margin:16px 0;padding:10px 14px;color:#5c4a1c}
.tableline{display:none}
.bullet::before{content:"• ";color:#1f6f8b}
hr{border:0;border-top:1px solid #cfd8df;margin:28px 0}
code{background:#eef2f5;padding:1px 4px}
</style></head><body><main>
%(body)s
</main></body></html>
""" % {"run": escape(str(model["run_id"])), "body": "\n".join(body)}


def build_report(manifest):
    markdown = build_markdown(manifest)
    html = build_html(manifest, markdown)
    model = report_model(manifest)
    digest = model["report_payload_sha256"]
    return {
        "markdown": markdown, "html": html,
        "report_payload_sha256": digest,
        "report_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "identity": model["identity"], "model": model,
    }


__all__ = ["REPORT_VERSION", "build_html", "build_markdown", "build_report", "report_model"]
