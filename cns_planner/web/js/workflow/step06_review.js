import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock} from './common.js';

export function planReviewSummary(review,confirmed){
  const variants=review?.variants||[],selected=variants.find(item=>item.variant_id===review?.selected_variant_id)||null;
  return {variantCount:variants.length,selectedVariantId:selected?.variant_id||null,
    selectedActionIds:selected?.selected_action_ids||[],gate:selected?.evaluation?.confirmation_gate?.status||'not_evaluated',
    confirmedStatus:confirmed?.status||'not_confirmed',applyStatus:confirmed?.application?.status||'not_applied'};
}

export function render({state,flow}){
  const labels={environment:'环境/GRC',technical:'技术/MTBF',life:'生命',property:'财产'};
  const risks=Object.entries(flow.review.risks).map(([key,value])=>'<div class="review-row"><span>'+labels[key]+'</span>'+statusBadge(value.status)+'</div>').join('');
  const resultLabels={workspace:'工作区',grid:'标准网格',environment_risk:'环境风险',routes:'运行航路',coverage:'C/N/S 布站',cns_corridor_assessment:'CNS 服务需求走廊',cns_corridor_gap_assessment:'CNS 走廊空间缺口',cns_corridor_site_plan:'P16 走廊布站提案',cns_plan_review:'P18 Plan Review',technical_risk:'技术风险',report:'报告'};
  const dependencies=Object.entries(flow.result_statuses||{}).map(([key,value])=>'<div class="review-row"><span>'+escapeHtml(resultLabels[key]||key)+'</span>'+statusBadge(value)+'</div>').join('');
  const layers=Object.entries(flow.coverage?.layers||{}).map(([key,value])=>key+'：'+value.statistics.stations+' 站 / '+statusText(value.status)).join('<br>');
  const proposal=flow.cns_corridor_site_plan||{},proposalSummary='<div class="review-block"><b>P16 走廊站址规划提案（Corridor Site Plan Proposal）</b><span>状态：'+statusText(proposal.status||'not_calculated')+'</span><span>目标体素：'+(proposal.target_voxel_count||0)+' · 已选动作：'+(proposal.selected_actions||[]).length+'</span><span>确认需求单位体积收益：'+(Number.isFinite(proposal.confirmed_requirement_unit_volume_gain)?proposal.confirmed_requirement_unit_volume_gain.toFixed(1)+' m³·unit':'—')+'</span><small>Proposal only；本身不修改 Existing CNS，P18 负责人工比较、确认与受控 Apply。</small></div>';
  const recommendation=flow.required_cns_recommendation||{},adoption=flow.required_cns_adoption||{},requirementSummary='<div class="review-block"><b>需求依据与来源追溯（Requirement basis / provenance）</b><span>模型：'+escapeHtml((recommendation.algorithm_id||flow.algorithm_selection?.requirement_model?.algorithm_id||'manual_required_cns_v1')+'@'+(recommendation.algorithm_version||flow.algorithm_selection?.requirement_model?.version||'1.0'))+'</span><span>需求建议：'+statusText(recommendation.status||'not_calculated')+' · 采用状态：'+statusText(adoption.status||'not_adopted')+'</span><span>匹配规则：'+(recommendation.matched_policies||[]).length+' · 来源字段：'+Object.keys(recommendation.field_provenance||{}).length+'</span><small>需求与能力/服务分离；不代表自动法规合规。</small></div>';
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const variantCards=(review.variants||[]).map(variant=>{
    const evaluation=variant.evaluation||{},gate=evaluation.confirmation_gate||{},actions=variant.selected_action_ids||[];
    return '<div class="review-block plan-variant '+(variant.variant_id===review.selected_variant_id?'selected':'')+'"><b>'+escapeHtml(variant.name||variant.variant_id)+'</b><span>'+escapeHtml(variant.source||'')+' · '+actions.length+' 个动作 · '+statusText(gate.status||'not_evaluated')+'</span><small>'+escapeHtml(variant.variant_id||'')+'</small><button class="secondary selectPlanVariant" data-variant-id="'+escapeHtml(variant.variant_id||'')+'">选择此方案</button></div>';
  }).join('')||'<div class="empty">尚未初始化方案审查。</div>';
  const selected=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id),matrix=selected?.evaluation?.comparison_matrix||[];
  const matrixRows=matrix.map(row=>'<tr><td>'+escapeHtml(row.route_id)+'</td><td>'+escapeHtml(row.subsystem)+'</td><td>'+statusText(row.objective_status||'not_evaluated')+'</td><td>'+formatDist(row.service)+'</td><td>'+formatDist(row.redundancy)+'</td><td>'+number(row.total_confirmed_deficit_projection_m)+'</td><td>'+number(row.max_continuous_deficit_projection_m)+'</td><td>'+(row.unknown_voxel_ids||[]).length+'</td></tr>').join('');
  const comparison='<div class="review-block"><b>规划目标对比表（Objective Comparison Matrix；无自动总分/排名）</b><div class="table-wrap"><table><thead><tr><th>航路</th><th>C/N/S</th><th>规划目标</th><th>服务 满足/缺口/未知</th><th>冗余 满足/缺口/未知</th><th>缺口总长 m</th><th>最大连续缺口 m</th><th>证据不足体素</th></tr></thead><tbody>'+matrixRows+'</tbody></table></div><span>显式费用：'+formatCosts(selected?.evaluation?.action_summary)+'</span></div>';
  const reviewUi='<div class="review-block"><b>P18 方案审查（Plan Review）与受控应用</b><span>评审：'+statusText(review.status||'not_initialized')+' · 已确认方案：'+statusText(summary.confirmedStatus)+' · 应用：'+statusText(summary.applyStatus)+'</span><small>Plan Variant 是人工决策候选；系统不计算隐藏 overall score/rank。选择不改设施，确认只冻结快照，应用才事务提交。</small><div class="button-row"><button class="secondary" id="initializePlanReview">初始化方案审查</button><button class="secondary" id="evaluatePlanVariant" '+(!selected?'disabled':'')+'>重新评价所选方案</button></div></div><div class="variant-grid">'+variantCards+'</div>'+comparison+
    '<div class="review-block"><b>用户编辑方案（User-edited Variant）</b><label>纳入动作ID（逗号分隔）<input id="variantInclude" placeholder="candidate_site:S1:C1"></label><label>排除动作ID（逗号分隔）<input id="variantExclude"></label><label>方案名称<input id="variantName" value="用户方案"></label><button class="secondary" id="createPlanVariant" '+(!selected?'disabled':'')+'>克隆并创建方案</button></div>'+
    '<div class="review-block"><b>确认门禁（Confirmation gate）</b><span>'+statusText(selected?.evaluation?.confirmation_gate?.status||'not_evaluated')+'</span><label><input type="checkbox" id="confirmWithoutObjectives"> 未配置规划目标时仍确认（记录明确知情确认）</label><label>确认理由<input id="planDecisionReason" placeholder="请记录人工确认依据"></label><div class="button-row"><button class="secondary" id="confirmPlan" '+(!selected?'disabled':'')+'>确认所选方案</button><button class="primary" id="applyPlan" '+(confirmed.status!=='confirmed'?'disabled':'')+'>应用已确认方案</button></div><small>应用会重跑 P7→P10 与 P14→P15；验证不一致、出现确认回归或关键证据不足时整笔回滚。</small></div>';
  const reports=flow.cns_planning_reports||{},active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id),hasPlan=['confirmed','applied'].includes(confirmed.status),hasReport=Boolean(active),stale=active?.current_applicability==='stale_current_project';
  const reportUi='<div class="review-block"><b>CNS规划方案报告</b><span>方案状态：'+statusText(confirmed.status||'not_confirmed')+' · 报告状态：'+statusText(active?.current_applicability||reports.status||'not_calculated')+'</span><span>报告ID：'+escapeHtml(active?.report_id||'尚未生成')+' · 生成时间：'+escapeHtml(active?.generated_at||'—')+'</span>'+(stale?'<p class="inline-error">该报告对应旧项目状态，可继续下载，但不代表当前项目。请重新确认方案并生成新报告。</p>':'')+(!hasPlan?'<p class="empty">请先在方案审查中确认一个规划方案；当前只能预览草稿，不能生成正式报告。</p>':'')+'<div class="button-row"><button class="secondary" id="previewPlanningReport">预览报告</button><button class="primary" id="generatePlanningReport" '+(!hasPlan?'disabled':'')+'>生成正式报告</button></div><div class="button-row"><button class="secondary" id="downloadReportHtml" '+(!hasReport?'disabled':'')+'>下载HTML</button><button class="secondary" id="downloadReportPdf" '+(!hasReport?'disabled':'')+'>下载PDF</button><button class="secondary" id="downloadReportPackage" '+(!hasReport?'disabled':'')+'>下载规划数据包</button></div><small>PDF使用与HTML完全相同的冻结ReportDataModel和页面；如提示PDF能力缺失，请执行 <code>python -m playwright install chromium</code> 后重试。</small></div>';
  const overview='<div class="review-block"><b>'+escapeHtml(flow.project.name)+'</b><span>数据源：'+statusText(state.data_health.status)+'</span><span>工作区：'+(flow.workspace?flow.workspace.area_km2+' km²':'未定义')+'</span><span>运行航路：'+flow.operational_routes.length+'（'+flow.operational_routes.map(item=>item.route_id).join(', ')+'）</span><span>飞行器：'+(flow.aircraft?escapeHtml(flow.aircraft.manufacturer+' '+flow.aircraft.model):'未设置')+'</span><span>规则：'+statusText(flow.rules?.status||'not_calculated')+'</span><span>'+layers+'</span></div>';
  const riskPanel='<div class="risk-review">'+risks+'</div><div class="risk-review">'+dependencies+'</div><div class="overall-card">总体状态：'+statusBadge(flow.review.overall_status)+'<br>总体通过：'+(flow.review.overall_pass?'是':'否')+'</div>';
  const body=wbPanel('operate',
      wbBlock('方案审查与受控应用',overview+reviewUi))
    +wbPanel('result',
      wbBlock('报告与导出',reportUi)
      +wbBlock('总体状态',riskPanel)
      +'<div class="button-row export-row"><a class="secondary button-link" download="project.json" href="/api/export/project">项目JSON</a><a class="secondary button-link" download="routes.geojson" href="/api/export/routes">航路GeoJSON</a><a class="secondary button-link" download="sites.geojson" href="/api/export/sites">兼容站点GeoJSON</a></div>'
      +'<button class="primary full" id="saveAll">保存当前项目</button>')
    +wbPanel('advanced',
      wbBlock('证据与来源追溯',requirementSummary+proposalSummary));
  return shell('06','方案评审','比较客观指标，人工选择、确认，再事务式应用。',body);
}

export function bind(c){
  c.actionButton('saveAll',()=>c.mutate('save'));
  c.actionButton('initializePlanReview',()=>c.resourceAction('/api/cns-plan-review/initialize',{}));
  c.actionButton('evaluatePlanVariant',()=>c.resourceAction('/api/cns-plan-review/evaluate',{variant_id:c.flow().cns_plan_review?.selected_variant_id}));
  for(const button of document.querySelectorAll('.selectPlanVariant'))button.onclick=()=>c.resourceAction('/api/cns-plan-review/select',{variant_id:button.dataset.variantId});
  c.actionButton('createPlanVariant',()=>c.resourceAction('/api/cns-plan-review/variant',{
    base_variant_id:c.flow().cns_plan_review?.selected_variant_id,name:c.$('variantName').value,
    include_action_ids:list(c.$('variantInclude').value),exclude_action_ids:list(c.$('variantExclude').value),
  }));
  c.actionButton('confirmPlan',()=>c.resourceAction('/api/cns-plan-review/confirm',{
    variant_id:c.flow().cns_plan_review?.selected_variant_id,
    confirm_without_objectives:c.$('confirmWithoutObjectives').checked,
    source:'step06_user_confirmation',reason:c.$('planDecisionReason').value,
  }));
  c.actionButton('applyPlan',()=>c.resourceAction('/api/cns-plan-review/apply',{plan_id:c.flow().confirmed_cns_plan?.plan_id}));
  c.actionButton('previewPlanningReport',()=>c.previewPlanningReport());
  c.actionButton('generatePlanningReport',()=>c.resourceAction('/api/cns-planning-report/generate',{}));
  c.actionButton('downloadReportHtml',()=>c.downloadPlanningReport('html'));
  c.actionButton('downloadReportPdf',()=>c.downloadPlanningReport('pdf'));
  c.actionButton('downloadReportPackage',()=>c.downloadPlanningReport('package'));
}

function list(value){return String(value||'').split(',').map(item=>item.trim()).filter(Boolean);}
function number(value){return Number.isFinite(value)?value.toFixed(1):'—';}
function formatDist(value){const counts=value?.voxel_counts||{};return [counts.satisfied||0,counts.confirmed_deficit??counts.confirmed_gap??0,counts.unknown||0].join('/');}
function formatCosts(summary){const values=summary?.explicit_costs_by_unit||{},text=Object.entries(values).map(([unit,value])=>number(value)+' '+escapeHtml(unit)).join('；');return text||'action_count_proxy（未伪造货币成本）';}
