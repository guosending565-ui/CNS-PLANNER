import {escapeHtml,shell,statusBadge,statusText} from './common.js';

function collectionList(collection,kind){
  const items=collection?.items||[];
  if(!items.length)return '<div class="empty-note">尚未导入</div>';
  return items.slice(0,20).map(item=>{
    const id=kind==='facility'?item.facility_id:item.site_id;
    const detail=kind==='facility'?(item.devices||[]).map(device=>device.subsystem+' '+(device.name||device.device_id||'')).join(' · '):(item.available_subsystems||[]).join('/');
    return '<div class="list-row"><span><b>'+escapeHtml(item.name||id)+'</b><br><small>'+escapeHtml(id)+' · '+escapeHtml(detail||'未配置设备')+'</small></span><small>'+escapeHtml((item.coordinate||[]).join(', '))+'</small></div>';
  }).join('')+(items.length>20?'<div class="empty-note">另有 '+(items.length-20)+' 项</div>':'');
}

function gapList(analysis){
  if(!analysis?.routes?.length)return '<div class="empty-note">尚未运行 CNS Gap Analysis</div>';
  const label=status=>status==='passed'?'满足':statusText(status);
  return analysis.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+statusBadge(route.status)+'</b>'+route.subsystems.map(item=>'<div class="gap-row"><strong>'+item.subsystem+'</strong><span>'+label(item.status)+'</span><span>覆盖 '+(item.coverage_ratio==null?'—':(item.coverage_ratio*100).toFixed(1)+'%')+'</span><span>缺口 '+(item.gap_length_m==null?'—':Math.round(item.gap_length_m)+' m')+'</span></div>').join('')+'</div>').join('');
}

function coverage3dList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未运行 3D 几何覆盖</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+statusBadge(route.status)+'</b>'+route.subsystems.map(item=>'<div class="gap-row"><strong>'+item.subsystem+'</strong><span>'+statusText(item.status)+'</span><span>覆盖 '+(item.covered_fraction==null?'—':(item.covered_fraction*100).toFixed(1)+'%')+'</span><span>未覆盖 '+(item.uncovered_length_m==null?'—':Math.round(item.uncovered_length_m)+' m')+'</span></div>').join('')+'</div>').join('');
}

function capabilityList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未计算 CNS Service Capability</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+statusBadge(route.status)+'</b>'+route.subsystems.map(item=>{const sample=(item.samples||[]).find(value=>(value.provider_evaluations||[]).length)||(item.samples||[])[0]||{},provider=(sample.provider_evaluations||[])[0]||{},margin=provider.link_budget?.link_margin_db;return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(item.status)+'</b><span>meets '+pct(item.meets_fraction)+' · fail '+pct(item.fail_fraction)+' · unknown '+pct(item.unknown_fraction)+'</span><span>scope '+escapeHtml(provider.model_scope||item.model_scope||'static_capability')+' · '+escapeHtml(provider.model_family||'模型未确认')+(Number.isFinite(margin)?' · link margin '+margin.toFixed(1)+' dB':'')+'</span><small>'+escapeHtml((provider.reasons||sample.reasons||[]).join('；')||'无额外 evidence')+'</small></div>';}).join('')+'</div>').join('');
}

function pct(value){return value==null?'—':(value*100).toFixed(1)+'%';}

function timelineList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未生成 Service Timeline</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+(route.duration_s==null?'时间未知':route.duration_s.toFixed(1)+' s')+'</b>'+route.subsystems.map(item=>{const duration=item.duration_by_state_s||{},length=item.length_by_state_m||{};return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml((item.states_present||[]).join('/'))+'</b><span>available '+formatMetric(duration.available,'s')+' · degraded '+formatMetric(duration.available_degraded,'s')+' · contingency '+formatMetric(duration.contingency,'s')+'</span><span>lost '+formatMetric(duration.lost,'s')+' · unknown '+formatMetric(duration.unknown,'s')+' / '+formatMetric(length.unknown,'m')+'</span></div>';}).join('')+'</div>').join('');
}

function protectionSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未计算 Protection Envelope</div>';
  return '<div class="coverage-card"><b>'+escapeHtml(result.status||'unknown')+' · '+escapeHtml(result.model_scope||'engineering_tactical_protection_envelope')+'</b><span>T_pre '+formatMetric(result.t_pre_s,'s')+' · D_reaction '+formatMetric(result.d_reaction_m,'m')+' · D_protect '+formatMetric(result.d_protect_m,'m')+'</span><small>'+escapeHtml((result.reasons||[]).join('；')||'输入已确认')+'</small></div>';
}

function gapV2List(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未运行 CNS Gap Analysis V2</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+escapeHtml(route.status||'unknown')+'</b>'+route.subsystems.map(item=>{
    const segments=(item.segments||[]).filter(segment=>segment.combined_status!=='satisfied'&&segment.combined_status!=='not_applicable').slice(0,12);
    const rows=segments.map(segment=>{const evidence=(segment.evidence||[]).map(value=>value.kind).filter(Boolean).join(' / ');return '<div class="list-row"><span><b>'+escapeHtml(segment.combined_status)+'</b> '+Math.round(segment.start_route_offset_m)+'–'+Math.round(segment.end_route_offset_m)+' m<br><small>'+escapeHtml((segment.gap_causes||[]).join(' / ')||'无结构化原因')+' · '+escapeHtml(segment.remediation_scope||'unknown')+'</small></span><small>'+escapeHtml((segment.reasons||[]).join('；')||'无额外原因')+'<br>evidence: '+escapeHtml(evidence||'unknown')+'</small></div>';}).join('');
    return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(item.status)+'</b><span>planning gap '+formatMetric(item.planning_assessment?.length_by_status_m?.confirmed_gap,'m')+' · runtime lost '+formatMetric(item.runtime_lost_length_m,'m')+' / '+formatMetric(item.runtime_lost_duration_s,'s')+'</span><span>combined gap '+formatMetric(item.gap_length_m,'m')+' · contingency '+formatMetric(item.contingency_exposure_length_m,'m')+' / '+formatMetric(item.contingency_exposure_duration_s,'s')+' · unknown '+formatMetric(item.unknown_length_m,'m')+'</span><span>max gap '+formatMetric(item.max_continuous_gap_length_m,'m')+' / '+formatMetric(item.max_continuous_gap_duration_s,'s')+'</span>'+rows+'</div>';
  }).join('')+'</div>').join('');
}

function sitePlanSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未运行 Reuse-first CNS Site Planner</div>';
  const impacts=new Map((result.candidate_impacts||[]).map(item=>[item.action_id,item]));
  const candidates=(result.candidate_actions||[]).slice(0,20).map(action=>{const impact=impacts.get(action.action_id)||{};return '<div class="list-row"><span><b>'+escapeHtml(action.action_id)+'</b><br><small>'+escapeHtml(action.reuse_class||'unknown')+' · '+escapeHtml(action.eligibility?.status||'unknown')+'</small></span><small>what-if gain '+formatMetric(impact.planning_gap_reduction_m,'m')+'</small></div>';}).join('');
  const selected=(result.selected_actions||[]).map(action=>'<div class="coverage-card"><b>'+escapeHtml(action.action_id)+'</b><span>'+escapeHtml(action.reuse_class)+' · '+escapeHtml(action.subsystem)+' · marginal '+formatMetric(action.marginal_planning_gap_reduction_m,'m')+'</span><small>'+escapeHtml(action.score_semantics||'action_count_proxy')+'</small></div>').join('');
  return '<div class="coverage-card"><b>'+escapeHtml(result.status||'unknown')+' · Proposal Only</b><span>target '+formatMetric(result.target_planning_gap_length_m,'m')+' · projected resolved '+formatMetric(result.resolved_planning_gap_length_m,'m')+' · remaining '+formatMetric(result.remaining_planning_gap_length_m,'m')+'</span><span>existing '+(result.existing_reuse_count||0)+' · shared '+(result.shared_site_reuse_count||0)+' · candidate '+(result.candidate_site_count||0)+' · new-build '+(result.new_build_count||0)+'</span><small>'+escapeHtml(result.cost_summary?.cost_semantics||'action_count_proxy_no_currency')+'；requires P12 closed-loop validation</small></div><h4>CandidateAction what-if</h4>'+candidates+'<h4>Selected proposal</h4>'+(selected||'<div class="empty-note">没有产生正 confirmed planning-gap reduction 的 eligible action</div>');
}

function formatMetric(value,unit){return Number.isFinite(value)?value.toFixed(1)+' '+unit:'—';}

export function render({flow}){
  const devices=(flow.devices||[]).map((device,index)=>'<div class="device-row"><b>'+device.subsystem+' · '+escapeHtml(device.model||device.name||device.device_id)+'</b><label>R(m)<input type="number" data-device-radius="'+index+'" value="'+device.radius_m+'"></label><label>MTBF(h)<input type="number" data-device-mtbf="'+index+'" value="'+(device.mtbf_h||device.mtbf)+'"></label><span>'+device.role+'</span></div>').join('');
  let result='<div class="empty-note">尚未运行 CoveragePlannerV1</div>';
  if(flow.coverage)result=Object.entries(flow.coverage.layers||{}).map(([key,layer])=>{const stats=layer.statistics;return '<div class="coverage-card"><b>'+key+' '+statusBadge(layer.status)+'</b><span>站点 '+stats.stations+' · 主站 '+stats.primary+' · 补盲 '+stats.gap+' · 共址 '+stats.colocated+'</span><span>平均重数 '+stats.average_multiplicity+' · 未覆盖 '+stats.uncovered_samples+'</span></div>';}).join('');
  const params=flow.defaults.engineering_parameters,existing=flow.existing_cns_facilities||{},candidates=flow.candidate_sites||{},catalog=flow.device_catalog||{},gaps=flow.cns_gap_analysis||{},coverage3d=flow.coverage_3d||{},capability=flow.cns_service_capability||{},timeline=flow.service_timeline||{},protection=flow.protection_envelope||{},gapV2=flow.cns_gap_analysis_v2||{},sitePolicy=flow.site_planning_policy||{},sitePlan=flow.cns_site_plan||{};
  const body='<div class="demo-note">DeviceCatalog：'+escapeHtml(catalog.source||flow.device_source)+' · '+(catalog.count||0)+' 型设备</div>'+
    '<div class="parameter-note">主站间距 '+params.primary_spacing_factor.value+'R · 共址半径 '+params.co_location_search_radius_m.value+'m<br>'+escapeHtml(params.primary_spacing_factor.source)+'</div>'+
    '<div class="device-list">'+devices+'</div><div class="button-row"><button class="secondary" id="saveDevices">保存设备参数</button><button class="primary" id="planCoverage">运行布站</button></div>'+
    '<h3>已有 CNS 设施 '+statusBadge(existing.status||'not_calculated')+'</h3><div class="panel-file-input"><input class="panel-input" id="existing_cnsPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseExisting">选择…</button></div><button class="secondary full" id="importExisting">导入已有设施</button><div class="scroll-list cns-input-list">'+collectionList(existing,'facility')+'</div>'+
    '<h3>CNS Gap Analysis '+statusBadge(gaps.status||'not_calculated')+'</h3><button class="primary full" id="analyzeGaps">分析当前运行航路缺口</button><div class="gap-results">'+gapList(gaps)+'</div>'+
    '<h3>3D Geometric Coverage '+statusBadge(coverage3d.status||'not_calculated')+'</h3><div class="parameter-note">几何覆盖 ≠ 真实 CNS 性能；传播、LOS、绕射、干扰、链路预算和传感器 Pd 均未评估。</div><label>sample spacing (m)<input class="panel-input" type="number" id="coverage3dSpacing" value="'+(coverage3d.parameters?.sample_spacing_m||flow.algorithm_selection?.coverage_model?.parameters?.sample_spacing_m||500)+'"></label><button class="secondary full" id="evaluateCoverage3d">运行 3D 几何覆盖</button><div class="gap-results">'+coverage3dList(coverage3d)+'</div>'+
    '<h3>CNS Service Capability '+statusBadge(capability.status||'not_calculated')+'</h3><div class="parameter-note">静态能力满足 ≠ 当前服务 available。模型成熟度：engineering baseline；LOS/绕射/干扰/负载/切换等未评估。</div><button class="secondary full" id="evaluateServiceCapability">评估技术感知静态能力</button><div class="gap-results">'+capabilityList(capability)+'</div>'+
    '<h3>C/N/S Service Timeline '+statusBadge(timeline.status||'not_calculated')+'</h3><div class="parameter-note">运行状态只来自显式 ServiceScenarioEvent；不从 P8 meets、ReliabilitySpec 或 MTBF 推断 available/outage。</div><button class="secondary full" id="evaluateServiceTimeline">生成服务时间线</button><div class="gap-results">'+timelineList(timeline)+'</div>'+
    '<h3>Tactical Protection Envelope '+statusBadge(protection.status||'not_calculated')+'</h3><div class="parameter-note">工程保护距离 ≠ 法规 Well-Clear / 正式 DAA Detection Volume。</div><button class="secondary full" id="evaluateProtectionEnvelope">计算工程保护距离</button><div class="gap-results">'+protectionSummary(protection)+'</div>'+
    '<h3>CNS Gap Analysis V2 '+statusBadge(gapV2.status||'not_calculated')+'</h3><div class="parameter-note">合并 P7 几何、P8 静态能力与 P9 运行时间线；Unknown 表示证据不足，不是危险等级，Gap 也不自动触发 Safety Event。</div><label class="check-row"><input type="checkbox" id="gapV2Protection" '+(gapV2.parameters?.evaluate_protection_margin?'checked':'')+'> 可选工程 Protection Margin（非 Well-Clear/认证判断）</label><button class="secondary full" id="evaluateGapV2">运行 Gap V2</button><div class="gap-results">'+gapV2List(gapV2)+'</div>'+
    '<h3>Reuse-first CNS Site Planner V1 '+statusBadge(sitePlan.status||'not_calculated')+'</h3><div class="parameter-note">仅目标化 confirmed planning gap；tier 固定为 Existing CNS → Existing Shared Site → Candidate Site → New-build Candidate。P10 remediation scope 仅为提示，收益必须经 P7/P8 what-if 确认。</div><label class="check-row"><input type="checkbox" id="sitePolicyConfirmed" '+(sitePolicy.confirmed?'checked':'')+'> 确认使用 reuse-first engineering policy</label><button class="secondary full" id="evaluateSitePlan">生成 Proposal</button><div class="parameter-note">Proposal 不修改 ExistingCNS，也不声明 Gap 已消除；P12 必须 apply + rerun 闭环复核。</div><div class="gap-results">'+sitePlanSummary(sitePlan)+'</div>'+
    '<h3>候选站址 '+statusBadge(candidates.status||'not_calculated')+'</h3><div class="panel-file-input"><input class="panel-input" id="candidate_sitesPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseCandidates">选择…</button></div><div class="button-row"><button class="secondary" id="importCandidates">导入候选站址</button><button class="secondary" id="deriveCandidates">从已有设施生成</button></div><div class="scroll-list cns-input-list">'+collectionList(candidates,'candidate')+'</div>'+
    '<div class="coverage-results">'+result+'</div><div class="flow-summary">生命风险：'+statusText(flow.risks.life.status)+' · 财产风险：'+statusText(flow.risks.property.status)+'<br>已有设施与候选站址仅作为规划输入，本轮不改变 CoveragePlannerV1。</div><button class="primary full" id="nextStep" '+(!flow.steps['5']?'disabled':'')+'>下一步：确认与导出</button>';
  return shell('05','设备与布站','设备库、已有设施和候选站址；V1 布站保持原有兼容输入。',body);
}

export function bind(c){
  const collect=()=>c.flow().devices.map((device,index)=>({...device,radius_m:Number(document.querySelector('[data-device-radius="'+index+'"]').value),mtbf:Number(document.querySelector('[data-device-mtbf="'+index+'"]').value)}));
  c.actionButton('saveDevices',()=>c.mutate('devices',{devices:collect()}));
  c.actionButton('planCoverage',async()=>{await c.mutate('devices',{devices:collect()});await c.mutate('coverage');});
  c.$('browseExisting').onclick=()=>c.openBrowser('existing_cns',c.$('existing_cnsPath').value);
  c.$('browseCandidates').onclick=()=>c.openBrowser('candidate_sites',c.$('candidate_sitesPath').value);
  c.actionButton('importExisting',()=>c.resourceAction('/api/existing-cns/import',{path:c.$('existing_cnsPath').value.trim()}));
  c.actionButton('importCandidates',()=>c.resourceAction('/api/candidate-sites/import',{path:c.$('candidate_sitesPath').value.trim()}));
  c.actionButton('deriveCandidates',()=>c.resourceAction('/api/candidate-sites/from-existing',{}));
  c.actionButton('analyzeGaps',()=>c.mutate('gap-analysis'));
  c.actionButton('evaluateCoverage3d',()=>c.resourceAction('/api/coverage-3d/evaluate',{parameters:{sample_spacing_m:Number(c.$('coverage3dSpacing').value),assumption:'user_engineering_sampling_assumption',confirmed:false}}));
  c.actionButton('evaluateServiceCapability',()=>c.resourceAction('/api/cns-service-capability/evaluate',{}));
  c.actionButton('evaluateServiceTimeline',()=>c.resourceAction('/api/service-timeline/evaluate',{}));
  c.actionButton('evaluateProtectionEnvelope',()=>c.resourceAction('/api/protection-envelope/evaluate',{}));
  c.actionButton('evaluateGapV2',()=>c.resourceAction('/api/cns-gap-analysis-v2',{parameters:{evaluate_protection_margin:c.$('gapV2Protection').checked}}));
  c.actionButton('evaluateSitePlan',()=>{const policy=structuredClone(c.flow().site_planning_policy||{});policy.confirmed=c.$('sitePolicyConfirmed').checked;policy.source='user_configuration';return c.resourceAction('/api/cns-site-plan',{site_planning_policy:policy});});
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(6);
}
