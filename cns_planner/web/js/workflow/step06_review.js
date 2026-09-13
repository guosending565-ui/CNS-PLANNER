import {escapeHtml,shell,statusBadge,statusText} from './common.js';

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
  const proposal=flow.cns_corridor_site_plan||{},proposalSummary='<div class="review-block"><b>P16 Corridor Site Plan · Proposal</b><span>状态：'+escapeHtml(proposal.status||'not_calculated')+'</span><span>Target voxels：'+(proposal.target_voxel_count||0)+' · Selected actions：'+(proposal.selected_actions||[]).length+'</span><span>Requirement-unit volume gain：'+(Number.isFinite(proposal.confirmed_requirement_unit_volume_gain)?proposal.confirmed_requirement_unit_volume_gain.toFixed(1)+' m³·unit':'—')+'</span><small>Proposal only；本身不修改 Existing CNS，P18 负责人工比较、确认与受控 Apply。</small></div>';
  const recommendation=flow.required_cns_recommendation||{},adoption=flow.required_cns_adoption||{},requirementSummary='<div class="review-block"><b>Requirement basis / provenance</b><span>模型：'+escapeHtml((recommendation.algorithm_id||flow.algorithm_selection?.requirement_model?.algorithm_id||'manual_required_cns_v1')+'@'+(recommendation.algorithm_version||flow.algorithm_selection?.requirement_model?.version||'1.0'))+'</span><span>Recommendation：'+escapeHtml(recommendation.status||'not_calculated')+' · Adoption：'+escapeHtml(adoption.status||'not_adopted')+'</span><span>Matched policies：'+(recommendation.matched_policies||[]).length+' · Provenance fields：'+Object.keys(recommendation.field_provenance||{}).length+'</span><small>Requirement 与 capability/service 分离；不代表自动法规合规。</small></div>';
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const variantCards=(review.variants||[]).map(variant=>{
    const evaluation=variant.evaluation||{},gate=evaluation.confirmation_gate||{},actions=variant.selected_action_ids||[];
    return '<div class="review-block plan-variant '+(variant.variant_id===review.selected_variant_id?'selected':'')+'"><b>'+escapeHtml(variant.name||variant.variant_id)+'</b><span>'+escapeHtml(variant.source||'')+' · '+actions.length+' actions · '+escapeHtml(gate.status||'not_evaluated')+'</span><small>'+escapeHtml(variant.variant_id||'')+'</small><button class="secondary selectPlanVariant" data-variant-id="'+escapeHtml(variant.variant_id||'')+'">Select</button></div>';
  }).join('')||'<div class="empty">尚未 Initialize Plan Review。</div>';
  const selected=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id),matrix=selected?.evaluation?.comparison_matrix||[];
  const matrixRows=matrix.map(row=>'<tr><td>'+escapeHtml(row.route_id)+'</td><td>'+escapeHtml(row.subsystem)+'</td><td>'+escapeHtml(row.objective_status||'—')+'</td><td>'+formatDist(row.service)+'</td><td>'+formatDist(row.redundancy)+'</td><td>'+number(row.total_confirmed_deficit_projection_m)+'</td><td>'+number(row.max_continuous_deficit_projection_m)+'</td><td>'+(row.unknown_voxel_ids||[]).length+'</td></tr>').join('');
  const comparison='<div class="review-block"><b>Objective Comparison Matrix（无自动总分/排名）</b><div class="table-wrap"><table><thead><tr><th>Route</th><th>C/N/S</th><th>Objectives</th><th>Service S/D/U</th><th>Redundancy S/D/U</th><th>Total deficit m</th><th>Max deficit m</th><th>Unknown</th></tr></thead><tbody>'+matrixRows+'</tbody></table></div><span>Cost：'+formatCosts(selected?.evaluation?.action_summary)+'</span></div>';
  const reviewUi='<div class="review-block"><b>P18 Plan Review · Controlled Apply</b><span>Review：'+escapeHtml(review.status||'not_initialized')+' · Confirmed：'+escapeHtml(summary.confirmedStatus)+' · Apply：'+escapeHtml(summary.applyStatus)+'</span><small>Plan Variant 是人工决策候选；系统不计算隐藏 overall score/rank。Select 不改设施，Confirm 冻结快照，Apply 才事务提交。</small><div class="button-row"><button class="secondary" id="initializePlanReview">Initialize</button><button class="secondary" id="evaluatePlanVariant" '+(!selected?'disabled':'')+'>Re-evaluate Selected</button></div></div><div class="variant-grid">'+variantCards+'</div>'+comparison+
    '<div class="review-block"><b>User-edited Variant</b><label>Include action IDs（逗号分隔）<input id="variantInclude" placeholder="candidate_site:S1:C1"></label><label>Exclude action IDs（逗号分隔）<input id="variantExclude"></label><label>名称<input id="variantName" value="User Variant"></label><button class="secondary" id="createPlanVariant" '+(!selected?'disabled':'')+'>Clone / Create Variant</button></div>'+
    '<div class="review-block"><b>Confirmation gate</b><span>'+escapeHtml(selected?.evaluation?.confirmation_gate?.status||'not_evaluated')+'</span><label><input type="checkbox" id="confirmWithoutObjectives"> Confirm without configured objectives（显式 acknowledgement）</label><label>Decision reason<input id="planDecisionReason" placeholder="记录人工确认依据"></label><div class="button-row"><button class="secondary" id="confirmPlan" '+(!selected?'disabled':'')+'>Confirm Selected</button><button class="primary" id="applyPlan" '+(confirmed.status!=='confirmed'?'disabled':'')+'>Apply Confirmed Plan</button></div><small>Apply 会重跑 P7→P10 与 P14→P15；验证不一致、regression 或关键 unknown 时整笔回滚。</small></div>';
  const body='<div class="review-block"><b>'+escapeHtml(flow.project.name)+'</b><span>数据源：'+statusText(state.data_health.status)+'</span><span>工作区：'+(flow.workspace?flow.workspace.area_km2+' km²':'未定义')+'</span><span>运行航路：'+flow.operational_routes.length+'（'+flow.operational_routes.map(item=>item.route_id).join(', ')+'）</span><span>飞行器：'+(flow.aircraft?escapeHtml(flow.aircraft.manufacturer+' '+flow.aircraft.model):'未设置')+'</span><span>规则：'+statusText(flow.rules?.status||'not_calculated')+'</span><span>'+layers+'</span></div>'+requirementSummary+proposalSummary+reviewUi+'<div class="risk-review">'+risks+'</div><div class="risk-review">'+dependencies+'</div><div class="overall-card">overall_status：'+statusBadge(flow.review.overall_status)+'<br>overall_pass：'+String(flow.review.overall_pass)+'</div><div class="button-row export-row"><a class="secondary button-link" download="project.json" href="/api/export/project">project.json</a><a class="secondary button-link" download="routes.geojson" href="/api/export/routes">routes.geojson</a><a class="secondary button-link" download="sites.geojson" href="/api/export/sites">sites.geojson</a></div><button class="primary full" id="saveAll">保存当前项目</button>';
  return shell('06','Plan Review 与受控应用','比较客观指标，人工 Select、Confirm，再事务 Apply。',body);
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
}

function list(value){return String(value||'').split(',').map(item=>item.trim()).filter(Boolean);}
function number(value){return Number.isFinite(value)?value.toFixed(1):'—';}
function formatDist(value){const counts=value?.voxel_counts||{};return [counts.satisfied||0,counts.confirmed_deficit??counts.confirmed_gap??0,counts.unknown||0].join('/');}
function formatCosts(summary){const values=summary?.explicit_costs_by_unit||{},text=Object.entries(values).map(([unit,value])=>number(value)+' '+escapeHtml(unit)).join('；');return text||'action_count_proxy（未伪造货币成本）';}
