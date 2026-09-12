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

export function render({flow}){
  const devices=(flow.devices||[]).map((device,index)=>'<div class="device-row"><b>'+device.subsystem+' · '+escapeHtml(device.model||device.name||device.device_id)+'</b><label>R(m)<input type="number" data-device-radius="'+index+'" value="'+device.radius_m+'"></label><label>MTBF(h)<input type="number" data-device-mtbf="'+index+'" value="'+(device.mtbf_h||device.mtbf)+'"></label><span>'+device.role+'</span></div>').join('');
  let result='<div class="empty-note">尚未运行 CoveragePlannerV1</div>';
  if(flow.coverage)result=Object.entries(flow.coverage.layers||{}).map(([key,layer])=>{const stats=layer.statistics;return '<div class="coverage-card"><b>'+key+' '+statusBadge(layer.status)+'</b><span>站点 '+stats.stations+' · 主站 '+stats.primary+' · 补盲 '+stats.gap+' · 共址 '+stats.colocated+'</span><span>平均重数 '+stats.average_multiplicity+' · 未覆盖 '+stats.uncovered_samples+'</span></div>';}).join('');
  const params=flow.defaults.engineering_parameters,existing=flow.existing_cns_facilities||{},candidates=flow.candidate_sites||{},catalog=flow.device_catalog||{},gaps=flow.cns_gap_analysis||{},coverage3d=flow.coverage_3d||{},capability=flow.cns_service_capability||{};
  const body='<div class="demo-note">DeviceCatalog：'+escapeHtml(catalog.source||flow.device_source)+' · '+(catalog.count||0)+' 型设备</div>'+
    '<div class="parameter-note">主站间距 '+params.primary_spacing_factor.value+'R · 共址半径 '+params.co_location_search_radius_m.value+'m<br>'+escapeHtml(params.primary_spacing_factor.source)+'</div>'+
    '<div class="device-list">'+devices+'</div><div class="button-row"><button class="secondary" id="saveDevices">保存设备参数</button><button class="primary" id="planCoverage">运行布站</button></div>'+
    '<h3>已有 CNS 设施 '+statusBadge(existing.status||'not_calculated')+'</h3><div class="panel-file-input"><input class="panel-input" id="existing_cnsPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseExisting">选择…</button></div><button class="secondary full" id="importExisting">导入已有设施</button><div class="scroll-list cns-input-list">'+collectionList(existing,'facility')+'</div>'+
    '<h3>CNS Gap Analysis '+statusBadge(gaps.status||'not_calculated')+'</h3><button class="primary full" id="analyzeGaps">分析当前运行航路缺口</button><div class="gap-results">'+gapList(gaps)+'</div>'+
    '<h3>3D Geometric Coverage '+statusBadge(coverage3d.status||'not_calculated')+'</h3><div class="parameter-note">几何覆盖 ≠ 真实 CNS 性能；传播、LOS、绕射、干扰、链路预算和传感器 Pd 均未评估。</div><label>sample spacing (m)<input class="panel-input" type="number" id="coverage3dSpacing" value="'+(coverage3d.parameters?.sample_spacing_m||flow.algorithm_selection?.coverage_model?.parameters?.sample_spacing_m||500)+'"></label><button class="secondary full" id="evaluateCoverage3d">运行 3D 几何覆盖</button><div class="gap-results">'+coverage3dList(coverage3d)+'</div>'+
    '<h3>CNS Service Capability '+statusBadge(capability.status||'not_calculated')+'</h3><div class="parameter-note">静态能力满足 ≠ 当前服务 available。模型成熟度：engineering baseline；LOS/绕射/干扰/负载/切换等未评估。</div><button class="secondary full" id="evaluateServiceCapability">评估技术感知静态能力</button><div class="gap-results">'+capabilityList(capability)+'</div>'+
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
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(6);
}
