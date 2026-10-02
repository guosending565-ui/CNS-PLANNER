"""Standalone, escaped Chinese HTML rendering from one frozen ReportDataModel."""

from __future__ import annotations

from html import escape
import json


STATUS_ZH = {
    "passed": "通过", "failed": "失败", "confirmed_deficit": "确认缺口",
    "confirmed_gap": "确认缺口", "satisfied": "满足", "unknown": "证据不足/尚无法判断",
    "not_applicable": "不适用", "pending_confirmation": "待确认", "current": "当前有效",
    "stale": "已失效", "stale_current_project": "对应旧项目状态", "confirmed": "已确认",
    "applied": "已应用", "real": "真实数据", "synthetic": "模拟数据", "manual": "人工录入",
    "objectives_met": "规划目标满足", "objectives_not_met": "规划目标未满足",
    "objectives_unknown": "规划目标证据不足", "objectives_not_configured": "未配置规划目标",
}


class HtmlReportRenderer:
    template_version = "cns-planning-report-zh-v1"

    def render(self, model):
        sections = model.get("sections") or {}
        project = sections.get("project_overview") or {}
        rows = (model.get("statistics") or {}).get("rows") or []
        return "<!doctype html>\n" + f"""<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(project.get('name') or 'CNS规划方案报告')}</title>
<style>{_CSS}</style></head><body><main>
<header><p class="eyebrow">CNS-PLANNER · {e(model.get('report_mode'))}</p><h1>CNS规划方案报告</h1>
<p class="lead">{e(project.get('name') or '未命名项目')} · {e(model.get('plan_status_label'))}</p>
<p class="muted">生成时间：{e(model.get('generated_at'))} · ReportDataModel：{e(model.get('report_data_fingerprint'))}</p></header>
{_warnings(model.get('disclaimers') or [])}
<section><h2>1. 项目概述</h2>{_kv(project)}</section>
<section><h2>2. 运行场景与需求依据</h2>{_requirement_basis(sections.get('operation_and_requirement_basis') or {})}</section>
<section><h2>3. 数据基础</h2>{_sources(sections.get('data_foundation') or {})}</section>
<section><h2>4. 建筑环境与建筑净空安全</h2><p class="note">FABDEM 非测绘级 DTM；GBA 高度非实测真值；结果仅供工程评估。</p>{_json_details(sections.get('building_environment_and_clearance'))}</section>
<section><h2>5. 航路、高度与三维服务需求走廊</h2>{_route_svg(sections)}<p class="note">走廊为工程CNS服务需求走廊，不等同法规Operational Volume或批准空间。</p>{_json_details(sections.get('routes_altitude_corridor'))}</section>
<section><h2>6. 所需CNS性能（RequiredCNS）</h2>{_json_details(sections.get('required_cns'))}</section>
<section><h2>7. P10中心线CNS缺口</h2>{_result_summary(sections.get('centerline_gap_p10'))}</section>
<section><h2>8. P14三维服务空间</h2><p class="note">离散体积代理 / 代表点评价，不是整个体素的性能保证。</p>{_result_summary(sections.get('spatial_service_p14'))}</section>
<section><h2>9. P15服务、冗余、空间连续缺口与规划目标</h2>{_statistics(rows)}<p class="note">空间连续缺口投影，不是运行时连续性概率。</p></section>
<section><h2>10. P17 连续服务可接受性（含 post-plan 投影态）</h2>{_continuous_service(sections.get('continuous_service_acceptability_p17') or {})}</section>
<section><h2>11. P18方案比较与人工决策</h2>{_decision(sections.get('plan_review_p18') or {})}</section>
<section><h2>12. 最终设施方案</h2>{_facility_svg(sections)}{_json_details(sections.get('final_facility_plan'))}</section>
<section><h2>13. Before / After 与残余问题</h2>{_json_details(sections.get('before_after_residual'))}</section>
<section><h2>14. 算法、数据、指纹与来源审计</h2>{_audit(sections.get('audit') or {})}</section>
<section><h2>15. 局限与未评估事项</h2>{_limitations(sections.get('limitations') or {})}</section>
</main></body></html>"""


def e(value):
    return escape(str(value if value is not None else "—"), quote=True)


def zh(value):
    return STATUS_ZH.get(str(value), str(value))


def _warnings(values):
    return '<aside class="warning"><b>使用边界</b><ul>' + ''.join(f'<li>{e(item)}</li>' for item in values) + '</ul></aside>'


def _kv(value):
    return '<dl>' + ''.join(f'<dt>{e(key)}</dt><dd>{e(zh(item))}</dd>' for key, item in (value or {}).items() if not isinstance(item, (dict, list))) + '</dl>'


def _requirement_basis(value):
    recommendation=value.get("recommendation") or {}; adoption=value.get("adoption") or {}
    return f'<p>需求推荐状态：<b>{e(zh(recommendation.get("status") or "not_calculated"))}</b>；采用状态：<b>{e(zh(adoption.get("status") or "not_adopted"))}</b></p>' + _json_details(value)


def _sources(value):
    rows=[]
    for key,item in (value or {}).items():
        item=item or {}; mode=item.get("source_mode",item.get("source_type"))
        rows.append(f'<tr><td>{e(key)}</td><td>{e(item.get("name") or item.get("source_id"))}</td><td>{e(zh(mode))}</td><td>{e(item.get("version"))}</td><td>{e(item.get("quantity"))}</td><td>{e(item.get("unit"))}</td><td>{e((item.get("verification") or {}).get("status"))}</td></tr>')
    return '<table><thead><tr><th>数据类别</th><th>逻辑来源</th><th>来源类型</th><th>版本</th><th>物理量</th><th>单位</th><th>核验</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'


def _statistics(rows):
    body=[]
    for item in rows:
        combined=(item.get("combined") or {}).get("fractions") or {}; redundancy=(item.get("redundancy") or {}).get("fractions") or {}
        body.append(f'<tr><td>{e(item.get("route_id"))}</td><td>{e(_subsystem(item.get("subsystem")))}</td><td>{pct(combined.get("satisfied"))}</td><td>{pct(combined.get("confirmed_gap",combined.get("confirmed_deficit")))}</td><td>{pct(combined.get("unknown"))}</td><td>{pct(redundancy.get("satisfied"))}</td><td>{e(item.get("total_confirmed_deficit_projection_m"))}</td><td>{e(item.get("max_continuous_deficit_projection_m"))}</td><td>{e(zh(item.get("objective_status")))}</td></tr>')
    chart=_bar_svg(rows)
    return chart+'<table><thead><tr><th>航路</th><th>分系统</th><th>满足</th><th>确认缺口</th><th>证据不足</th><th>冗余满足</th><th>缺口投影总长(m)</th><th>最大连续缺口(m)</th><th>规划目标</th></tr></thead><tbody>'+''.join(body)+'</tbody></table>'


def _decision(value):
    plan=value.get("confirmed_plan") or {}; variant=plan.get("variant") or {}; gate=(variant.get("evaluation") or {}).get("confirmation_gate") or {}
    return f'<p>方案：<b>{e(plan.get("plan_id"))}</b> · {e(zh(plan.get("status")))}</p><p>Plan Variant：{e(variant.get("name"))} / {e(variant.get("variant_id"))} · 门禁：{e(zh(gate.get("status")))}</p><p>该方案由人工选择和确认；系统未生成自动综合评分或排名。显式费用按 cost_unit 分组，禁止跨单位合计。</p>'+_json_details(value)


def _result_summary(value):
    value=value or {}; return f'<p>状态：<b>{e(zh(value.get("status")))}</b> · 算法：<code>{e(value.get("algorithm_id"))}@{e(value.get("algorithm_version"))}</code> · 输入指纹：<code>{e(value.get("input_fingerprint"))}</code></p>'+_json_details(value)


def _audit(value):
    return '<p>来源链语义：<code>w3c_prov_inspired_not_full_prov_compliance</code></p>'+_json_details(value)


def _limitations(value):
    return '<table><tbody>'+''.join(f'<tr><th>{e(key)}</th><td>{e(zh(item))}</td></tr>' for key,item in value.items())+'</tbody></table>'


def _continuous_service(value):
    """Round 2.6：P17 分段渲染 —— 两层结论、强制披露、威胁分层与能力限制。

    报告**必须**逐字保留披露原文；绝不把 managed gap 说成"全覆盖"，也绝不把
    Radar 不可行说成"监视完全满足"。
    """

    value = value or {}
    disclosure = value.get("disclosure_lines") or []
    limitations = value.get("limitations") or []
    gaps = value.get("managed_gaps") or []
    remaining = value.get("remaining_gaps") or []
    improved = value.get("improved_services") or []
    header = (
        f'<p>当前结论：<b>{e(zh(value.get("status")))}</b>；'
        f'baseline（现网）：<b>{e(zh(value.get("baseline_status")))}</b>；'
        f'post-plan（P16 方案实施后投影态）：<b>{e(zh(value.get("post_plan_status")))}</b></p>'
        f'<p>评估对象：{"该方案实施后的投影态（hypothetical，未写入现网事实）" if value.get("evaluates_post_plan_projection") else "仅当前权威状态（没有可用的 post-plan 投影）"}'
        f'；纳入的动作：{e("、".join(value.get("applied_action_ids") or []) or "（无）")}</p>'
        f'<p>主要威胁（合作无人机 / RID）：<b>{e(zh(value.get("primary_threat_status")))}</b>；'
        f'补充威胁（非合作无人机 / Radar）：<b>{e(zh(value.get("supplementary_threat_status")))}</b></p>'
    )
    risk_lines = [
        "本分段的 managed gap 与能力限制**不是**全覆盖：缺口真实存在，仅因连续时长在工程阈值内被接受。",
        "能力限制（例如 Radar 非合作监视不可行）**不改变**主要威胁（合作无人机 / RID）的判定，但不得表述为「监视完全满足」。",
    ]
    protection = value.get("route_protection") or {}
    protection_rows = "".join(
        f'<tr><td>{e(row.get("route_id"))}</td><td>{e(row.get("D_separation_m"))}</td>'
        f'<td>{e(row.get("V_relative_mps"))}</td><td>{e(row.get("T_chain_s"))}</td>'
        f'<td>{e(row.get("D_maneuver_m"))}</td><td>{e(row.get("D_uncertainty_m"))}</td>'
        f'<td>{e(row.get("D_protection_m"))}</td><td>{e(zh(row.get("status")))}</td></tr>'
        for row in protection.get("routes") or []
    )
    protection_table = (
        f'<p>保护走廊公式：<code>{e(protection.get("formula"))}</code>'
        f'（D_maneuver 为 engineering_baseline 接口，不代表法规值或某机型普遍制动距离）</p>'
        '<table><thead><tr><th>航路</th><th>D_separation(m)</th><th>V_relative(m/s)</th>'
        '<th>T_chain(s)</th><th>D_maneuver(m)</th><th>D_uncertainty(m)</th>'
        '<th>D_protection(m)</th><th>状态</th></tr></thead><tbody>'
        + protection_rows + '</tbody></table>'
    )
    thresholds = value.get("communication_thresholds") or {}
    threshold_text = (
        f'<p>通信阈值（**互相独立，绝不合并**）：C 完全中断 '
        f'{e((thresholds.get("service_acceptability_limits") or {}).get("C", {}).get("service_outage"))} s'
        f' · C 冗余退化 '
        f'{e((thresholds.get("service_acceptability_limits") or {}).get("C", {}).get("redundancy_degradation"))} s'
        f'；来源：{e(thresholds.get("policy_source"))}</p>'
        '<p>FC30 的「遥控信号丢失 &gt; 3 s 触发 Failsafe RTH」只是<b>设备 failsafe 事实</b>，'
        '不等于本项目的规划阈值；规划阈值由用户以工程规划假设显式登记（未登记时判定保持 '
        'evidence_required / unknown）。</p>'
    )
    gap_rows = "".join(
        f'<tr><td>{e(item.get("route_id"))}</td><td>{e(item.get("service"))}</td>'
        f'<td>{e(zh(item.get("kind")))}</td><td>{e(item.get("length_m"))}</td>'
        f'<td>{e(item.get("duration_s"))}</td><td>{e(item.get("limit_s"))}</td></tr>'
        for item in gaps
    )
    gap_table = (
        '<p>已接受为 managed gap 的连续缺口（真实存在，非全覆盖）：</p>'
        '<table><thead><tr><th>航路</th><th>服务</th><th>类型</th><th>长度(m)</th>'
        '<th>持续时间(s)</th><th>阈值(s)</th></tr></thead><tbody>'
        + gap_rows + '</tbody></table>'
    ) if gaps else '<p>没有被接受为 managed gap 的连续缺口。</p>'
    remaining_rows = "".join(
        f'<tr><td>{e(item.get("route_id"))}</td><td>{e(item.get("service"))}</td>'
        f'<td>{e(item.get("length_m"))}</td><td>{e(item.get("duration_s"))}</td>'
        f'<td>{e(item.get("limit_s"))}</td><td>{e(item.get("improvement_kind"))}</td></tr>'
        for item in remaining
    )
    remaining_table = (
        '<p>方案实施后**仍然存在**的连续缺口：</p>'
        '<table><thead><tr><th>航路</th><th>服务</th><th>长度(m)</th><th>持续时间(s)</th>'
        '<th>阈值(s)</th><th>改善依据</th></tr></thead><tbody>'
        + remaining_rows + '</tbody></table>'
    ) if remaining else '<p>方案实施后没有剩余连续缺口。</p>'
    improvement_table = (
        f'<p>被方案改善的服务数：{e(len(improved))}（改善必须有可核查依据：缺口事件消失或 P15 显式声明缓解量）。</p>'
    )
    limitation_html = (
        '<aside class="warning"><b>能力限制（不是系统错误）</b><ul>'
        + ''.join(f'<li>{e(item.get("disclosure") or item.get("capability"))}</li>' for item in limitations)
        + '</ul></aside>'
    ) if limitations else '<p>当前没有登记能力限制。</p>'
    disclosure_html = (
        '<aside class="warning"><b>强制披露原文</b><ul>'
        + ''.join(f'<li>{e(line)}</li>' for line in disclosure)
        + '</ul></aside>'
    ) if disclosure else ''
    return (
        header + _warnings(risk_lines) + protection_table + threshold_text
        + gap_table + remaining_table + improvement_table
        + limitation_html + disclosure_html + _json_details(value)
    )


def _json_details(value):
    text=json.dumps(value or {},ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
    return f'<details><summary>展开审计数据（保留英文schema字段）</summary><pre>{e(text)}</pre></details>'


def _route_svg(sections):
    routes=((sections.get("routes_altitude_corridor") or {}).get("routes") or [])
    points=[point for route in routes for point in route.get("path") or [] if isinstance(point,list) and len(point)>=2]
    return _map_svg(points, [])


def _facility_svg(sections):
    block=sections.get("final_facility_plan") or {}; facilities=(block.get("existing_facilities") or {}).get("items") or []
    points=[]; planned=[]
    for item in facilities:
        point=item.get("coordinate")
        if isinstance(point,list) and len(point)>=2: points.append(point)
    if block.get("plan_status") == "confirmed":
        for action in block.get("confirmed_actions") or []:
            point=action.get("coordinate")
            if isinstance(point,list) and len(point)>=2: planned.append(point)
    routes=((sections.get("routes_altitude_corridor") or {}).get("routes") or [])
    route_points=[point for route in routes for point in route.get("path") or [] if isinstance(point,list) and len(point)>=2]
    return _map_svg(route_points,points,planned)


def _map_svg(route_points, facilities, planned=None):
    planned=planned or []; all_points=[*route_points,*facilities,*planned]
    if not all_points:return '<p class="empty">暂无可绘制的航路或设施坐标。</p>'
    xs=[float(p[0]) for p in all_points]; ys=[float(p[1]) for p in all_points]; minx,maxx=min(xs),max(xs);miny,maxy=min(ys),max(ys)
    dx=max(maxx-minx,1e-9);dy=max(maxy-miny,1e-9)
    def xy(p):return (30+(float(p[0])-minx)/dx*540,270-(float(p[1])-miny)/dy*240)
    path=' '.join(('M' if i==0 else 'L')+f'{xy(p)[0]:.2f},{xy(p)[1]:.2f}' for i,p in enumerate(route_points))
    dots=''.join(f'<circle cx="{xy(p)[0]:.2f}" cy="{xy(p)[1]:.2f}" r="5"/>' for p in facilities)
    proposals=''.join(f'<rect class="planned" x="{xy(p)[0]-5:.2f}" y="{xy(p)[1]-5:.2f}" width="10" height="10"/>' for p in planned)
    return f'<svg class="map" viewBox="0 0 600 300" role="img" aria-label="航路与现有、规划CNS设施示意图"><rect width="600" height="300"/><path d="{path}"/><g>{dots}{proposals}</g><text x="18" y="292">圆点=现有/已应用设施；方块=已确认待应用设施；纯内嵌工程示意图</text></svg>'


def _bar_svg(rows):
    bars=[]
    for index,item in enumerate(rows[:12]):
        fractions=(item.get("combined") or {}).get("fractions") or {}; sat=float(fractions.get("satisfied") or 0); gap=float(fractions.get("confirmed_gap",fractions.get("confirmed_deficit")) or 0); unk=float(fractions.get("unknown") or 0)
        y=18+index*24; bars.append(f'<text x="0" y="{y+12}">{e(item.get("route_id"))}-{e(item.get("subsystem"))}</text><rect x="120" y="{y}" width="{sat*420:.2f}" height="16" class="sat"/><rect x="{120+sat*420:.2f}" y="{y}" width="{gap*420:.2f}" height="16" class="gap"/><rect x="{120+(sat+gap)*420:.2f}" y="{y}" width="{unk*420:.2f}" height="16" class="unk"/>')
    height=max(55,32+len(rows[:12])*24)
    return f'<svg class="chart" viewBox="0 0 560 {height}" role="img" aria-label="CNS满足、确认缺口和证据不足统计图">'+''.join(bars)+'</svg>'


def _subsystem(code):
    return {"C":"通信（Communication, C）","N":"导航（Navigation, N）","S":"监视（Surveillance, S）"}.get(code,code)


def pct(value):
    return "—" if value is None else f"{float(value)*100:.1f}%"


_CSS="""*{box-sizing:border-box}body{margin:0;background:#eef1ef;color:#17211c;font:14px/1.55 system-ui,"Microsoft YaHei","Noto Sans CJK SC",sans-serif}main{max-width:1040px;margin:auto;background:#fff;padding:34px 44px}h1{font-size:32px;margin:.2em 0}h2{border-bottom:2px solid #1e6b50;padding-bottom:6px;margin-top:30px}.eyebrow{color:#1e6b50;font-weight:700}.lead{font-size:18px}.muted,.note{color:#5d6a63}.warning{border-left:5px solid #d99022;background:#fff8e9;padding:14px 20px}section{break-inside:avoid}table{width:100%;border-collapse:collapse;margin:12px 0;font-size:12px}th,td{border:1px solid #ccd5d0;padding:6px;text-align:left;vertical-align:top}th{background:#edf4f0}dl{display:grid;grid-template-columns:180px 1fr}dt,dd{padding:5px;border-bottom:1px solid #e7ece9;margin:0}pre{white-space:pre-wrap;word-break:break-word;background:#f4f6f5;padding:12px;font-size:10px}code{word-break:break-all}.map,.chart{width:100%;height:auto;background:#f6f8f7;border:1px solid #ccd5d0}.map>rect{fill:#f5f7f6}.map path{fill:none;stroke:#1f6ec2;stroke-width:3}.map circle{fill:#24764f;stroke:#fff;stroke-width:1.5}.map .planned{fill:#d12f8a;stroke:#fff;stroke-width:1.5}.map text,.chart text{font-size:10px;fill:#56635c}.chart .sat{fill:#2e8b57}.chart .gap{fill:#c84630}.chart .unk{fill:#89938e}.empty{color:#77827c}@page{size:A4;margin:14mm}@media print{body{background:#fff}main{padding:0;max-width:none}a{color:inherit;text-decoration:none}details{display:block}details>summary{display:none}}"""
