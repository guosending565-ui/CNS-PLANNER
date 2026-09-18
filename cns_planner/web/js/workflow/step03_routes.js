import {escapeHtml,shell,statusBadge,statusText} from './common.js';
import {bindRouteVerticalProfile,renderRouteVerticalProfilePanel} from './route_vertical_profile.js';
import {ADVANCED_PROFILE_LABEL,bindCruiseLayer,renderCruiseLayerPanel} from './route_operating_layer.js';

function jsonInline(value){
  try{return JSON.stringify(value);}catch(error){return String(value);}
}

function metric(value,unit=''){const number=Number(value);return value!==null&&value!==undefined&&Number.isFinite(number)?number.toFixed(2)+(unit?' '+unit:''):'—';}

export const PLANNER_V1='route_planner_v1';
export const PLANNER_V2='risk_aware_route_planner_v2';
export const ROUTE_PLANNER_TYPE='route_planner';

function turnCount(path){
  const points=(path||[]).filter(point=>Array.isArray(point)&&point.length>=2);
  let turns=0;
  for(let index=1;index<points.length-1;index++){
    const a=points[index-1],b=points[index],c=points[index+1];
    if(Math.abs((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))>1e-12)turns++;
  }
  return turns;
}

function pathLengthM(path){let total=0;for(let index=1;index<(path||[]).length;index++){const a=path[index-1],b=path[index],lat1=a[1]*Math.PI/180,lat2=b[1]*Math.PI/180,dlat=lat2-lat1,dlon=(b[0]-a[0])*Math.PI/180,h=Math.sin(dlat/2)**2+Math.cos(lat1)*Math.cos(lat2)*Math.sin(dlon/2)**2;total+=6371008.8*2*Math.asin(Math.sqrt(h));}return (path||[]).length>1?total:null;}

export function filterReferenceSites(items,{workspace=null,search='',region='',siteType=''}={}){
  const bbox=workspace?.bbox||null,needle=String(search||'').trim().toLocaleLowerCase();
  return (items||[]).filter(item=>{
    const point=item.coordinate;
    if(!Array.isArray(point)||point.length!==2||item.quality==='invalid')return false;
    if(bbox&&!(bbox[0]<=point[0]&&point[0]<=bbox[2]&&bbox[1]<=point[1]&&point[1]<=bbox[3]))return false;
    if(region&&item.region!==region)return false;
    if(siteType&&item.site_type!==siteType)return false;
    const haystack=[item.name,item.location,item.region,item.site_type,item.reference_site_id].join(' ').toLocaleLowerCase();
    return !needle||haystack.includes(needle);
  });
}

function pointInWorkspace(point,workspace){
  const bbox=workspace?.bbox;
  return Array.isArray(point)&&point.length===2&&(!bbox||(bbox[0]<=point[0]&&point[0]<=bbox[2]&&bbox[1]<=point[1]&&point[1]<=bbox[3]));
}

function routeTouchesWorkspace(route,workspace){
  const path=route.path||[],bbox=workspace?.bbox;
  if(!bbox)return path.length>1;
  if(!path.length)return false;
  const xs=path.map(point=>point?.[0]).filter(Number.isFinite),ys=path.map(point=>point?.[1]).filter(Number.isFinite);
  return xs.length>1&&Math.max(...xs)>=bbox[0]&&Math.min(...xs)<=bbox[2]&&Math.max(...ys)>=bbox[1]&&Math.min(...ys)<=bbox[3];
}

export function referenceOverlayModel(flow,layers={routes:true,points:true,landingSites:true}){
  const catalog=flow?.reference_routes||{};
  return {
    referenceRoutes:layers.routes?(catalog.items||[]).filter(item=>routeTouchesWorkspace(item,flow?.workspace)):[],
    referencePoints:layers.points?(catalog.points||[]).filter(item=>pointInWorkspace(item.coordinate,flow?.workspace)):[],
    referenceLandingSites:layers.landingSites?filterReferenceSites(flow?.reference_landing_sites?.items||[],{workspace:flow?.workspace}):[],
    scenarioRoutes:flow?.scenario_routes||[],
    operationalRoutes:flow?.operational_routes||[],
  };
}

export function findAlgorithmManifest(catalog,algorithmType,algorithmId,version){
  return (catalog||[]).find(item=>item.algorithm_type===algorithmType&&item.algorithm_id===algorithmId&&String(item.version)===String(version))||null;
}

function schemaDefaults(manifest){
  const properties=manifest?.parameter_schema?.properties||{},result={};
  Object.entries(properties).forEach(([name,schema])=>{if(schema&&Object.prototype.hasOwnProperty.call(schema,'default'))result[name]=schema.default;});
  return result;
}

export function effectiveParameters(manifest,selection){
  const supplied=(selection&&typeof selection.parameters==='object'&&selection.parameters)?selection.parameters:{},result=schemaDefaults(manifest);
  Object.entries(supplied).forEach(([name,value])=>{if(value!==null)result[name]=value;});
  const declared=manifest?.parameter_schema?.properties||{};
  return Object.entries(result).filter(([name])=>Object.prototype.hasOwnProperty.call(declared,name)||name in supplied)
    .sort(([a],[b])=>a.localeCompare(b)).map(([name,value])=>{
      const schema=declared[name]||{};
      return {name,value,source:Object.prototype.hasOwnProperty.call(supplied,name)?'selection':'schema_default',declared:Object.keys(declared).includes(name),
        minimum:schema.minimum,maximum:schema.maximum};
    });
}

export function plannerCardModel(flow){
  const selection=flow?.algorithm_selection?.[ROUTE_PLANNER_TYPE]||{};
  if(!selection.algorithm_id)return null;
  const manifest=findAlgorithmManifest(flow?.algorithm_catalog,ROUTE_PLANNER_TYPE,selection.algorithm_id,selection.version);
  if(!manifest){
    return {status:'manifest_missing',selection:{algorithm_id:selection.algorithm_id,version:selection.version},
      message:'当前算法选择在 algorithm_catalog 中没有精确匹配的 Manifest；不显示任何推断的限制说明。'};
  }
  return {status:'passed',manifest,selection:{algorithm_id:manifest.algorithm_id,version:manifest.version},
    effective_parameters:effectiveParameters(manifest,selection),active:true};
}

function listBlock(title,values){
  const items=(values||[]).filter(Boolean);
  return '<div class="parameter-note"><b>'+escapeHtml(title)+'</b>'+(items.length?'<ul>'+items.map(item=>'<li>'+escapeHtml(item)+'</li>').join('')+'</ul>':'<br>—')+'</div>';
}

function plannerCard(model){
  if(!model)return '';
  if(model.status!=='passed')return '<h3>当前规划器</h3><div class="parameter-note">'+escapeHtml(model.message||'Manifest 不可用')+'</div>';
  const m=model.manifest,parameters=model.effective_parameters||[];
  const rows=parameters.length?parameters.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.name)+'</b><small>'+escapeHtml(String(item.value))+' · '+(item.source==='selection'?'来自当前参数选择':'来自 schema 默认值')+(item.declared?'':' · schema 未声明')+'</small></span></div>').join(''):'<div class="empty-note">Manifest 未声明有效参数</div>';
  return '<h3>当前规划器 '+statusBadge('passed')+'</h3>'
    +'<div class="parameter-note">以下内容直接来自 algorithm_catalog / Manifest，前端不硬编码算法限制。<br>'
    +'<b>'+escapeHtml(m.name||m.algorithm_id)+'</b> · <code>'+escapeHtml(m.algorithm_id)+'@'+escapeHtml(m.version)+'</code> · 成熟度 <code>'+escapeHtml(m.maturity)+'</code> · 提供方 '+escapeHtml(m.provider)+'</div>'
    +'<div class="flow-summary">'+escapeHtml(m.description||'')+'</div>'
    +listBlock('输入 contract',m.inputs)+listBlock('假设 assumptions',m.assumptions)+listBlock('局限 limitations',m.limitations)
    +'<h3>有效参数</h3><div class="scroll-list">'+rows+'</div>';
}

function plannerEntry(route,result,plannerId){
  if(!result||result.algorithm_id!==plannerId)return null;
  return {status:result.status,
    path_length_m:Number.isFinite(result.distance_m)?result.distance_m:pathLengthM(result.path),
    segment_count:Array.isArray(result.path)?result.path.length:null,
    turn_count:turnCount(result.path),
    risk_exposure_index_m:result.risk_exposure_index_m??null,
    max_risk_index:result.max_risk_index??null};
}

export function routePlannerComparisonModel(flow){
  const results=flow?.operational_routes||[];
  const rows=(flow?.scenario_routes||[]).map(route=>{
    const result=results.find(item=>item.route_id===route.route_id);
    return {route_id:route.route_id,direction:route.direction||'',
      [PLANNER_V1]:plannerEntry(route,result,PLANNER_V1),
      [PLANNER_V2]:plannerEntry(route,result,PLANNER_V2)};
  });
  const hasV1=rows.some(row=>row[PLANNER_V1]),hasV2=rows.some(row=>row[PLANNER_V2]);
  const hasReference=(flow?.reference_routes?.items||[]).length>0;
  return {rows,hasV1,hasV2,bothPresent:hasV1&&hasV2,hasReference,
    referenceNote:hasReference?'真实参考航线只与运行航线作长度/几何并列，不作优劣结论。':'当前项目没有参考航线可比。',
    semantics:'factual_side_by_side_no_superiority_conclusion',automatic_ranking:false};
}

function comparisonPanelV2(flow,model){
  if(!model.rows.length)return '';
  if(!model.bothPresent)return '<h3>V1 / V2 结果并列</h3><div class="parameter-note">当前只有 '+(model.hasV2?'V2':'V1')+' 结果；切换到另一个规划器并重新生成运行航路后才会出现并列比较。本面板只做事实并列，不作优劣结论。</div>';
  const body=model.rows.map(row=>'<div class="list-row route-row"><span><b>'+escapeHtml(row.route_id)+'</b> '+escapeHtml(row.direction)+'<small>V1 length '+metric(row[PLANNER_V1]?.path_length_m,'m')+' · vertices '+escapeHtml(String(row[PLANNER_V1]?.segment_count??'—'))+' · turns '+escapeHtml(String(row[PLANNER_V1]?.turn_count??'—'))+'</small><small>V2 length '+metric(row[PLANNER_V2]?.path_length_m,'m')+' · vertices '+escapeHtml(String(row[PLANNER_V2]?.segment_count??'—'))+' · turns '+escapeHtml(String(row[PLANNER_V2]?.turn_count??'—'))+' · risk exposure '+metric(row[PLANNER_V2]?.risk_exposure_index_m,'index·m')+'</small></span></div>').join('');
  return '<h3>V1 / V2 结果并列</h3><div class="parameter-note">只并列展示：长度/几何来自各自 planner 的真实输出与已发布路径；risk exposure 为 V2 自报相对工程指数。'
    +'<b>本面板不判定“更好”</b>，不排名、不评分、不推荐算法。V1 顶点更少是因为它对共线点做了简化，V2 保留完整 grid path 与网格中心（无 smoothing），这只说明输出契约不同。</div><div class="scroll-list route-list">'+body+'</div>';
}

function endpointOptions(nodes,selectedId){
  return (nodes||[]).map(node=>'<option value="'+escapeHtml(node.node_id)+'" '+(node.node_id===selectedId?'selected':'')+'>'+escapeHtml(node.node_id)+' · '+escapeHtml(node.name)+'</option>').join('');
}

function scenarioOptions(routes,selectedId){
  return (routes||[]).map(route=>'<option value="'+escapeHtml(route.route_id)+'" '+(route.route_id===selectedId?'selected':'')+'>'+escapeHtml(route.route_id)+' · '+escapeHtml(route.direction||'')+'</option>').join('');
}

function referenceRouteOptions(items,selectedId){
  return (items||[]).map(item=>'<option value="'+escapeHtml(item.reference_route_id)+'" '+(item.reference_route_id===selectedId?'selected':'')+'>'+escapeHtml(item.name||item.route_number||item.reference_route_id)+'</option>').join('');
}

// ---- experiment vs current operational route ----------------------------------------

export function routeExperimentModel(flow){
  const collection=flow?.route_planning_experiments||{},records=collection.records||[];
  const byId=new Map(records.map(item=>[item.experiment_id,item]));
  const active=collection.active_experiment||byId.get(collection.active_experiment_id)||records[0]||null;
  const runs=active?(active.runs||[]).map(run=>{
    const evaluation=run.quality||{},quality=evaluation.quality||{},stats=((run.runtime||{}).per_route_ms_stats)||{};
    return {run_id:run.run_id,algorithm_id:run.algorithm_id,algorithm_version:run.algorithm_version,
      status:run.status,reason:run.reason,effective_parameters:run.effective_parameters||{},
      planner_invoked:run.planner_invoked!==false,
      path_length_m:quality.path_length_m??null,segment_count:quality.segment_count??null,
      turn_count:quality.turn_count??null,detour_factor:quality.detour_factor??null,
      risk_exposure_index_m:(evaluation.risk_metrics||{}).risk_exposure_index_m??null,
      deterministic_consistency:evaluation.deterministic_consistency??null,
      runtime_median_ms:stats.median??null,result_fingerprint:run.result_fingerprint??null};
  }):[];
  return {count:records.length,active_experiment_id:collection.active_experiment_id||null,
    active:active?{experiment_id:active.experiment_id,created_at:active.created_at,grounding:active.grounding,
      source_type:active.source_type,current_applicability:active.current_applicability,
      scenario_fingerprint:active.scenario_fingerprint,
      planner_context_fingerprint:active.planner_context_fingerprint,
      scenario_inputs_changed:active.scenario_inputs_changed,
      context_inputs_changed:active.context_inputs_changed,verdicts:active.verdicts||{}}:null,
    runs,hasBothPlanners:runs.some(run=>run.algorithm_id===PLANNER_V1)&&runs.some(run=>run.algorithm_id===PLANNER_V2),
    operationalRouteCount:(flow?.operational_routes||[]).length,
    semantics:'experiment_is_not_current_operational_route',automatic_ranking:false};
}

function diagnosticRowsFromEvaluation(evaluation,origin){
  const values=Array.isArray(evaluation?.results)?evaluation.results:[evaluation];
  return values.filter(Boolean).map(item=>{
    const q=item.quality||{},g=item.grid_behavior||{},risk=item.risk_metrics||{},constraints=item.constraint_input_summary||{};
    return {origin,route_id:item.route_id,status:item.status,algorithm_id:item.algorithm_id,
      vertex_count:q.vertex_count,segment_count:q.segment_count,turn_count:q.turn_count,
      total_heading_change_deg:q.total_heading_change_deg,max_heading_change_deg:q.max_heading_change_deg,
      min_segment_m:q.min_segment_m,zigzag_index:q.zigzag_index,
      grid_level:g.grid_level,horizontal_steps:g.horizontal_step_count,vertical_steps:g.vertical_step_count,
      diagonal_steps:g.diagonal_step_count,direction_histogram:g.direction_histogram||{},
      risk_exposure_index_m:risk.risk_exposure_index_m,mean_risk_index:risk.mean_risk_index,max_risk_index:risk.max_risk_index,
      hard_constraint_count:constraints.hard_constraint_count,
      runtime_ms:item.runtime_ms};
  });
}

export function routePlanningDiagnosticsModel(flow){
  const snapshot=flow?.route_planning_diagnostics||{},current=(snapshot.routes||[]).flatMap(item=>diagnosticRowsFromEvaluation(item,'current'));
  const records=flow?.route_planning_experiments?.records||[],experiments=[];
  records.forEach(record=>(record.runs||[]).forEach(run=>{
    diagnosticRowsFromEvaluation(run.quality||{},'experiment '+record.experiment_id).forEach(item=>experiments.push({...item,algorithm_id:item.algorithm_id||run.algorithm_id,runtime_ms:item.runtime_ms??run.runtime?.per_route_ms_stats?.median}));
  }));
  return {status:snapshot.status||'not_calculated',planner:snapshot.planner||{},current,experiments,
    automatic_ranking:false,automatic_scoring:false,semantics:'descriptive_diagnostics_only'};
}

function diagnosticRow(item){
  return '<div class="list-row route-row"><span><b>'+escapeHtml(item.route_id||item.algorithm_id||'route')+'</b> '+statusBadge(item.status||'not_calculated')
    +'<small>'+escapeHtml(item.origin)+' · planner '+escapeHtml(item.algorithm_id||'—')+' · vertices '+escapeHtml(String(item.vertex_count??'—'))+' · segments '+escapeHtml(String(item.segment_count??'—'))+' · turns '+escapeHtml(String(item.turn_count??'—'))+'</small>'
    +'<small>heading total/max '+metric(item.total_heading_change_deg,'°')+' / '+metric(item.max_heading_change_deg,'°')+' · min segment '+metric(item.min_segment_m,'m')+' · zigzag '+metric(item.zigzag_index)+'</small>'
    +'<small>grid L'+escapeHtml(String(item.grid_level??'—'))+' · H/V/D '+escapeHtml(String(item.horizontal_steps??'—'))+'/'+escapeHtml(String(item.vertical_steps??'—'))+'/'+escapeHtml(String(item.diagonal_steps??'—'))+' · directions '+escapeHtml(jsonInline(item.direction_histogram||{}))+'</small>'
    +'<small>risk exposure/mean/max '+metric(item.risk_exposure_index_m,'index·m')+' / '+metric(item.mean_risk_index)+' / '+metric(item.max_risk_index)+' · runtime '+metric(item.runtime_ms,'ms')+'</small>'
    +'<small>hard constraints '+escapeHtml(String(item.hard_constraint_count??'—'))+' · airspace display-only</small></span></div>';
}

function routePlanningDiagnosticsPanel(flow){
  const model=routePlanningDiagnosticsModel(flow),limitations=model.planner.manifest_limitations||[];
  const rows=[...model.current,...model.experiments].map(diagnosticRow).join('');
  return '<h3>航路规划诊断 '+statusBadge(model.status)+'</h3><div class="parameter-note">当前 planner <code>'+escapeHtml(model.planner.algorithm_id||'—')+'@'+escapeHtml(model.planner.version||'—')+'</code>。指标仅描述 published route、grid behavior、risk 与约束输入；不评分、不排名、不推荐算法。</div>'
    +'<div class="flow-summary">Manifest limitations：'+escapeHtml(limitations.join('；')||'未提供')+'</div>'
    +'<div class="scroll-list route-list">'+(rows||'<div class="empty-note">暂无 current operational route 或 experiment diagnostics。</div>')+'</div>';
}

function experimentRunBlock(run){
  const parameters=Object.keys(run.effective_parameters||{}).length?jsonInline(run.effective_parameters):'{}';
  const metrics=run.planner_invoked
    ?'<small>length '+metric(run.path_length_m,'m')+' · vertices '+escapeHtml(String(run.segment_count??'—'))+' · turns '+escapeHtml(String(run.turn_count??'—'))+' · detour '+metric(run.detour_factor)+' · risk exposure '+metric(run.risk_exposure_index_m,'index·m')+' · median '+metric(run.runtime_median_ms,'ms')+'</small>'
    :'<small>planner 未运行</small>';
  return '<div class="list-row route-row"><span><b>'+escapeHtml(run.algorithm_id)+'@'+escapeHtml(run.algorithm_version)+'</b> '+statusBadge(run.status||'not_calculated')+'<small>run '+escapeHtml(run.run_id)+' · 参数 '+escapeHtml(parameters)+' · deterministic '+escapeHtml(String(run.deterministic_consistency))+'</small>'
    +metrics
    +(run.reason?'<small>原因：'+escapeHtml(run.reason)+'</small>':'')
    +'</span></div>';
}

// ---- Route Planner V3-A / V3-B (read-only / experimental panels) --------------------

export const V3_EXPERIMENT_NOTE='V3-A 战略规划实验 ≠ 运行航路：只写入 route_planner_v3_experiments，不切换当前 planner，也不覆盖 operational_routes 或 spatial_3d。';
export const V3_RESULT_STATUSES=['strategic_candidate','failed','missing_data','pending_confirmation','not_ready','search_incomplete'];
//: V3-B result statuses.  There is deliberately no "validated"/"final" status here.
export const V3B_RESULT_STATUSES=['refined_candidate','failed','not_ready','missing_data','search_incomplete'];
//: The label every V3-B rendering must carry verbatim: a refined candidate is not
//: a final safe route and the continuous-geometry validation is V3-C's job.
export const V3B_REFINED_LABEL='refined candidate，未执行 V3-C 连续几何验证';
export const V3B_ENVIRONMENT_SOURCES=['canonical_synthetic','configured_real_sources'];
export const V3B_RESOLUTION_SOURCES=['explicit_configuration','dtm_effective_resolution'];
// ---- Route Planner V3-C (continuous geometry + source-native validation) -------------
//: V3-C result statuses.  ``validated_route`` exists, but it is NOT an operational
//: route and CNS has not been assessed -- the label below is not optional.
export const V3C_RESULT_STATUSES=['validated_route','failed','unresolved','not_ready','validation_incomplete'];
export const V3C_DOMAINS=['geometry','airspace','terrain','building','altitude','kinematics'];
export const V3C_EVIDENCE_SOURCES=['canonical_synthetic','configured_real_sources'];
//: The disclaimer every V3-C rendering must carry verbatim.
export const V3C_OPERATIONAL_LABEL='V3-C validated route 仍不是 operational route；CNS 尚未评估';
export const V3C_REFINED_LABEL='continuous 实现 + confirmed 源几何/原生栅格验证';
const V3C_NOTE_FALLBACK='V3-C：把 V3-B refined candidate 实现为 C1（position+heading 连续）连续几何，圆弧 chord-error 显式；'
  +'再用 confirmed 空域 polygon、native FABDEM 像元、真实 footprint roof 与显式高度/运动学逐 domain 验证。'
  +'vector predicate 对 linearized representation（含显式 curve-error envelope）精确；terrain 是 source-native raster evidence。';
// ---- Route Planner V3-D (operational adoption + CNS assessment bridge) ---------------
export const V3D_ADOPTION_STATUSES=['published','stale','revoked'];
export const V3D_ADOPTION_APPLICABILITY=['current','stale','revoked'];
export const V3D_ASSESSMENT_STATUSES=['not_started','incomplete','complete','stale'];
export const V3D_REQUIREMENT_VERDICTS=['meets','does_not_meet','unknown'];
export const V3D_STAGES=['P7','P8','P9','P10'];
//: The boundary label every V3-D rendering must carry verbatim.
export const V3D_PUBLISH_LABEL='发布为运行分析航路：只把 current V3-C validated route 投影进既有 operational_routes，不改变 V3 验证结论';
export const V3D_SYNTHETIC_LABEL='canonical synthetic 证据仅用于测试，不可正式发布到 operational_routes';
export const V3D_CNS_SEPARATION_LABEL='route safety validation 与 CNS 结果严格分离：CNS 不满足不等于 route unsafe，route validated 也不等于 CNS 合规';
//: The downstream results a V3-D publish stales (never the route it just published).
export const V3D_DOWNSTREAM_RESULTS=['coverage_3d','cns_service_capability','service_timeline',
  'cns_gap_v2','cns_site_plan','closed_loop_assessment','building_clearance','route_vertical_profiles',
  'cns_corridor_assessment','cns_corridor_gap_assessment','cns_corridor_site_plan','report'];
const V3D_NOTE_FALLBACK='V3-D：把 current V3-C validated route 以显式、可追溯、事务式方式发布到既有 operational_routes + spatial_3d 高度剖面接口，'
  +'并复用既有 P7/P8/P9/P10 做 CNS Assessment。EGM2008 正高只写入 locked profile，绝不写入 GeoJSON 第三坐标；CNS 绝不反馈 V3 cost。';
//: Fallback V3-B note, used while a snapshot carries no ``v3b_note`` yet.
const V3B_NOTE_FALLBACK='V3-B corridor-local 精化候选 ≠ validated route：只在选定且 current 的 V3-A strategic_candidate 的 corridor 内做米制细网格工程精化；未做 V3-C exact polygon/terrain/continuous clearance 验证，也不写 operational_routes、algorithm_selection 或 spatial_3d。';

function v3RefinementReadinessModel(flow){
  const fine=flow?.route_planner_v3_refinement_readiness||{};
  return {
    status:fine.status||'not_calculated',stage:fine.stage||'V3-B',
    modelScope:fine.model_scope||'',architecture:fine.architecture||'',note:fine.note||'',
    scope:fine.stage_scope||{implemented:[],not_implemented:[]},
    algorithm:fine.algorithm||{},selectedCandidate:fine.selected_strategic_candidate||null,
    finePolicy:fine.fine_policy||{},
    v3PolicyReadiness:fine.v3_policy_readiness||{status:'unknown',missing_parameters:[]},
    realDataReadiness:fine.real_data_readiness||{status:'unknown',blocking_reasons:[]},
    blockingReasons:fine.blocking_reasons||[],
    environmentSources:fine.environment_sources||V3B_ENVIRONMENT_SOURCES,
    resolutionPolicy:fine.resolution_policy||'',
    allowedRefinementStatuses:fine.allowed_refinement_statuses||V3B_RESULT_STATUSES};
}

function v3RefinementApplicabilityModel(flow){
  const snapshot=flow?.route_planner_v3_refinements||{};
  return {
    status:snapshot.status||'not_calculated',count:snapshot.count||0,staleCount:snapshot.stale_count||0,
    semantics:snapshot.semantics||'stale_when_strategic_corridor_policy_or_source_audit_changes',
    components:snapshot.components||[],
    items:(snapshot.items||[]).map(item=>({refinementId:item.refinement_id,experimentId:item.experiment_id,
      status:item.status,recordedApplicability:item.recorded_applicability,
      currentApplicability:item.current_applicability,changedComponents:item.changed_components||[],
      reasons:item.reasons||[],refinementFingerprint:item.refinement_fingerprint,
      evidenceComponents:item.evidence_components||{}}))};
}

export function routePlannerV3ReadinessModel(flow){
  const snapshot=flow?.route_planner_v3_readiness||{};
  const scope=snapshot.stage_scope||{implemented:[],not_implemented:[]};
  const realData=snapshot.real_data_readiness||{status:'unknown',reasons:[]};
  return {status:snapshot.status||'not_calculated',stage:snapshot.stage||'V3-A',
    architecture:snapshot.architecture||'',scope:{...scope,implementedInOtherStages:scope.implemented_in_other_stages||{}},
    algorithm:snapshot.algorithm||{},grid:snapshot.grid||{},policy:snapshot.policy||{},
    policyReadiness:snapshot.policy_readiness||{status:'unknown',reasons:[],missing_parameters:[]},
    aircraftReadiness:snapshot.aircraft_readiness||{status:'unknown',reasons:[]},
    environmentReadiness:snapshot.environment_readiness||{status:'unknown',reason:''},
    realData:{...realData,v3b:realData.v3b||{status:'unknown',adapter_status:null,blocking_reasons:[],
      resolution_policy:null,terrain_dtm:null,buildings:null}},
    syntheticOptions:snapshot.synthetic_environment_options||{terrain_profiles:[],buildings_profiles:[],sources:[]},
    refinementReadiness:v3RefinementReadinessModel(flow),
    neverFinalValidated:true};
}

function refinementCostComponents(cost){
  return Object.entries(cost?.components||{}).map(([name,entry])=>({
    name,raw:entry.raw,normalized:entry.normalized,weight:entry.weight,contribution:entry.contribution,
    unit:entry.unit,source:entry.source,semantics:entry.semantics,enabled:entry.enabled===true,
    status:entry.status,reason:entry.reason,
    exposureM:entry.exposure_m??null,exposureDefinition:entry.exposure_definition||null,
    indexStatistics:entry.normalized_index_statistics||{},
    sourceResolutionM:entry.source_resolution_m??null,mappingMethod:entry.mapping_method||null,
    upsampledWithoutNewInformation:entry.upsampled_without_new_information===true,
    provenanceSources:entry.provenance_sources||[],provenanceNote:entry.provenance_note||null,
    edgeCount:entry.edge_count??null}));
}

function routePlannerV3RefinementModel(item){
  const result=item?.result||{};
  const path=result.state_path||[];
  const stats=result.search_statistics||{};
  const hard=result.hard_constraint_summary||{};
  const trajectory=result.trajectory_summary||{};
  const evidence=result.fine_grid_evidence||{};
  const grid=result.fine_grid||{};
  const cost=result.cost_vector||{};
  const altitudes=path.map(state=>state.altitude_egm2008_m).filter(Number.isFinite);
  return {
    refinementId:item?.refinement_id||null,experimentId:item?.experiment_id||null,
    createdAt:item?.created_at||null,environmentSource:item?.environment_source||null,
    grounding:item?.grounding||'corridor_local_fine_grid',
    status:result.status||item?.status||'not_ready',reason:result.reason||null,
    resolutionM:evidence.resolution_m??grid.resolution_m??null,
    resolutionSource:evidence.resolution_source??grid.resolution_source??null,
    requestedResolutionM:evidence.requested_resolution_m??grid.requested_resolution_m??null,
    effectiveSourceResolutionM:evidence.effective_source_resolution_m??grid.effective_source_resolution_m??null,
    resolutionDeviationM:evidence.resolution_deviation_m??grid.resolution_deviation_m??null,
    cellCount:evidence.cell_count??grid.cell_count??null,
    environmentCellCount:evidence.environment_cell_count??null,
    corridorSupportCellCount:evidence.corridor_support_cell_count??null,
    corridorCenterCellCount:evidence.corridor_center_cell_count??null,
    expandedStates:stats.expanded_states??null,generatedStates:stats.generated_states??null,
    primitiveChecks:stats.primitive_checks??hard.primitive_checks??null,
    traversedCellChecks:stats.traversed_cell_checks??hard.traversed_cell_checks??null,
    maxStrideCells:stats.max_stride_cells??trajectory.max_stride_cells??null,
    stateCount:trajectory.state_count??path.length,
    distanceM:result.distance_m??null,scalarCost:cost.scalar_cost??null,
    hardRejection:{state:hard.state_rejections||{},transition:hard.transition_rejections||{},
      traversedCell:hard.traversed_cell_rejections||{},
      totalState:hard.total_state_rejections??0,totalTransition:hard.total_transition_rejections??0,
      totalTraversed:hard.total_traversed_cell_rejections??0},
    hardSummary:hard,searchStatistics:stats,
    searchCompleteness:stats.search_completeness||'search_did_not_run',
    resourceLimited:stats.resource_limited===true,expansionCapReached:stats.expansion_cap_reached===true,
    expansionCap:stats.expansion_cap??null,resourceLimit:stats.resource_limit||null,
    resourceLimitReason:stats.resource_limit_reason||null,optimalityProven:stats.optimality_proven===true,
    stateSpaceShape:stats.state_space_shape||{},
    endpointBinding:stats.endpoint_binding||trajectory.endpoint_binding||null,
    multiCellStrideEdgeCount:trajectory.multi_cell_stride_edge_count??null,
    traversedCellCheckCount:trajectory.traversed_cell_check_count??null,
    maxHeadingChangeDeg:trajectory.max_heading_change_deg??null,
    maxClimbGradient:trajectory.max_climb_gradient??null,
    altitudeRange:altitudes.length?[Math.min(...altitudes),Math.max(...altitudes)]:null,
    finalValidationPerformed:false,operationalRoute:false,v3cPending:true,
    disclaimer:result.disclaimer||'',
    projection:result.horizontal_projection||[],metricProjection:result.metric_projection||[],
    fineGrid:result.fine_grid||null,frame:result.frame||null,sourceAudit:result.source_audit||{},
    evidence,
    costComponents:refinementCostComponents(cost),
    costMeta:{scalarCost:cost.scalar_cost??null,scalarCostAvailable:cost.scalar_cost_available===true,
      scalarWeightSum:cost.scalar_weight_sum??null,
      scalarWeightSumIsNotBounded:cost.scalar_weight_sum_is_not_bounded===true,
      scalarWeightSumNotUsedByTheHeuristic:cost.scalar_weight_sum_not_used_by_the_heuristic===true,
      scalarCostCap:cost.scalar_cost_cap??null,scalarCostCapActive:cost.scalar_cost_cap_active===true,
      semantics:cost.scalar_cost_semantics||'',cnsIntegration:cost.cns_integration||{}},
    trajectory,motionModel:result.motion_model||{},semantics:result.semantics||{},
    readiness:item?.readiness||{},readinessOverall:item?.readiness_overall||null,
    verdicts:item?.verdicts||{},fingerprintComponents:item?.fingerprint_components||{},
    currentApplicability:item?.current_applicability||null,
    recordedApplicability:item?.recorded_applicability||null,
    refinementFingerprint:item?.refinement_fingerprint??result.refinement_fingerprint??null,
    evidenceComponents:item?.evidence_components||{},
    statePreview:path.slice(0,24).map(state=>({fineCellId:state.fine_cell_id,altitudeM:state.altitude_egm2008_m,
      headingDeg:state.heading_deg,primitiveId:state.primitive_id,strideCells:state.stride_cells,
      traversedCellCount:(state.traversed_cell_ids||[]).length,climbGradient:state.climb_gradient}))};
}

export function routePlannerV3Model(flow){
  const collection=flow?.route_planner_v3_experiments||{};
  const detail=flow?.route_planner_v3_detail||{};
  const records=detail.records||[];
  const activeId=collection.active_experiment_id||null;
  const record=records.find(item=>item.experiment_id===activeId)||records[0]||null;
  const result=record?.result||null;
  const cost=result?.cost_vector||{};
  const components=Object.entries(cost.components||{}).map(([name,entry])=>({
    name,raw:entry.raw,normalized:entry.normalized,weight:entry.weight,contribution:entry.contribution,
    unit:entry.unit,source:entry.source,semantics:entry.semantics,enabled:entry.enabled===true,
    status:entry.status,reason:entry.reason}));
  const corridor=result?.candidate_refinement_corridor||null;
  const path=result?.state_path||[];
  return {
    status:collection.status||'not_calculated',
    count:collection.count||0,activeId,
    architecture:collection.architecture||'',
    allowedStatuses:collection.allowed_result_statuses||V3_RESULT_STATUSES,
    candidate:result?{
      status:result.status,routeId:result.route_id,reason:result.reason,
      distanceM:result.distance_m,scalarCost:cost.scalar_cost??null,
      scalarAvailable:cost.scalar_cost_available===true,
      stateCount:path.length,
      expandedStates:result.search_statistics?.expanded_states??null,
      runtimeMs:result.search_statistics?.runtime_ms??null,
      hardSummary:result.hard_constraint_summary||{},
      heuristic:result.heuristic_semantics||{},
      disclaimer:result.disclaimer||'',
      operationalRoute:result.operational_route===true,
      finalValidationPerformed:result.final_validation_performed===true,
      altitudeRange:path.length?[Math.min(...path.map(item=>item.altitude_egm2008_m)),Math.max(...path.map(item=>item.altitude_egm2008_m))]:null,
      projection:result.horizontal_projection||[],
      statePreview:path.slice(0,24).map(item=>({gridId:item.grid_id,altitudeM:item.altitude_egm2008_m,
        headingDeg:item.heading_deg,primitiveId:item.primitive_id,climbGradient:item.climb_gradient,
        headingChangeDeg:item.heading_change_deg})),
    }:null,
    components,
    excludedComponents:cost.excluded_components||[],
    cnsIntegration:cost.cns_integration||{},
    corridor:corridor?{
      semantics:corridor.semantics,ringN:corridor.ring_n,
      centerCount:(corridor.center_grid_ids||[]).length,
      supportCount:(corridor.support_grid_ids||[]).length,
      refinementCellSizeM:corridor.refinement_cell_size_m,
      altitudeEnvelope:corridor.altitude_envelope||null,
      nextStage:corridor.next_stage,
      notSafetyClearance:corridor.n_ring_is_not_a_safety_clearance===true,
    }:null,
    recent:(collection.records||[]).map(item=>({experimentId:item.experiment_id,createdAt:item.created_at,
      status:item.status,distanceM:item.distance_m,stateCount:item.state_count,
      expandedStates:item.expanded_states,corridorSupportCount:item.corridor_support_count,
      corridorRingN:item.corridor_ring_n,refinementCount:item.refinement_count,
      refinementStatus:item.refinement_status,refinementId:item.refinement_id,
      refinementDistanceM:item.refinement_distance_m,refinementResolutionM:item.refinement_resolution_m,
      refinementResolutionSource:item.refinement_resolution_source,
      refinementCellCount:item.refinement_cell_count,
      refinementEnvironmentCellCount:item.refinement_environment_cell_count,
      refinementExpandedStates:item.refinement_expanded_states,
      refinementScalarCost:item.refinement_scalar_cost,
      refinementEnvironmentSource:item.refinement_environment_source,
      refinementFinalValidationPerformed:false})),
    v3bArchitecture:collection.v3b_architecture||'',
    v3bNote:collection.v3b_note||V3B_NOTE_FALLBACK,
    allowedRefinementStatuses:collection.allowed_refinement_statuses||V3B_RESULT_STATUSES,
    refinementCount:(record?.refinements||[]).length||collection.active_experiment?.refinement_count||0,
    refinements:(record?.refinements||[]).map(routePlannerV3RefinementModel),
    refinementReadiness:v3RefinementReadinessModel(flow),
    refinementApplicability:v3RefinementApplicabilityModel(flow),
    note:collection.note||V3_EXPERIMENT_NOTE,
    semantics:'experiment_is_not_an_operational_route',automaticRanking:false};
}

function v3ReadinessRows(model){
  const rows=[
    {label:'空域 (airspace)',status:'not_applicable',reason:'仅显示参考图层，不参与路线约束'},
    {label:'地形 (terrain)',status:model.environmentReadiness.status,reason:'逐格 canonical 评估；unknown/NoData fail-closed'},
    {label:'建筑 (building)',status:model.environmentReadiness.status,reason:'逐格 canonical 评估；unknown fail-closed'},
    {label:'Policy',status:model.policyReadiness.status,reason:(model.policyReadiness.reasons||[]).join('；')||'参数完整'},
    {label:'Aircraft 运动能力',status:model.aircraftReadiness.status,reason:(model.aircraftReadiness.reasons||[]).join('；')||'explicit'},
    {label:'Cost model',status:'ready',reason:'weight 全部显式；energy pending_model/disabled'},
  ];
  return rows.map(row=>'<div class="list-row"><span><b>'+escapeHtml(row.label)+'</b> '+statusBadge(row.status)+'<small>'+escapeHtml(row.reason)+'</small></span></div>').join('');
}

function v3ParameterRows(model){
  const policy=model.policy||{};
  const names=[['min_altitude_egm2008_m','最小高度 EGM2008 (m)'],['max_altitude_egm2008_m','最大高度 EGM2008 (m)'],
    ['vertical_step_m','垂向步长 (m)'],['terrain_clearance_m','地形净空 (m)'],
    ['building_horizontal_clearance_m','建筑水平净空 (m)'],['building_vertical_clearance_m','建筑垂直净空 (m)'],
    ['aircraft_min_turn_radius_m','最小转弯半径 (m)'],['max_climb_gradient','最大爬升梯度'],
    ['max_descent_gradient','最大下降梯度'],['planning_speed_mps','规划速度 (m/s)']];
  return names.map(([key,label])=>'<div class="list-row"><span><b>'+escapeHtml(label)+'</b><small>'
    +(policy[key]===null||policy[key]===undefined?'<b>未配置</b>':escapeHtml(String(policy[key])))
    +'</small></span></div>').join('');
}

function v3CostRows(model){
  if(!model.components.length)return '<div class="empty-note">尚无 cost vector。</div>';
  return model.components.map(item=>'<div class="list-row route-row"><span><b>'+escapeHtml(item.name)+'</b> '
    +(item.enabled?statusBadge('passed'):statusBadge('not_applicable'))
    +'<small>raw '+metric(item.raw)+' '+escapeHtml(item.unit||'')+' · normalized '+metric(item.normalized)
    +' · weight '+(item.weight===null||item.weight===undefined?'—':escapeHtml(String(item.weight)))
    +' · contribution '+metric(item.contribution)+'</small>'
    +'<small>'+escapeHtml(item.semantics||'')+'</small>'
    +(item.enabled?'':'<small>'+escapeHtml(item.reason||item.status||'')+'</small>')+'</span></div>').join('');
}

function v3StateRows(model){
  if(!model.candidate)return '';
  return model.candidate.statePreview.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.gridId)+'</b>'
    +'<small>高度 '+metric(item.altitudeM,'m')+' · heading '+metric(item.headingDeg,'°')
    +' · primitive '+escapeHtml(item.primitiveId||'—')+' · climb '+metric(item.climbGradient)
    +' · Δheading '+metric(item.headingChangeDeg,'°')+'</small></span></div>').join('');
}

// ---- V3-B corridor-local refinement blocks ------------------------------------------

function v3bRows(rows){
  return rows.map(row=>'<div class="list-row"><span><b>'+escapeHtml(row[0])+'</b><small>'+row[1]+'</small></span></div>').join('');
}

function v3bRefinementBlock(model,refinement){
  const candidate=model.candidate;
  const header='<h3>V3-B corridor-local 精化候选 '+statusBadge(refinement?.status||'not_calculated')+'</h3>';
  if(!refinement)return header+'<div class="empty-note">尚无 V3-B 精化记录：先显式保存 V3-B fine policy，再对选定且 current 的 V3-A strategic_candidate 运行 corridor-local 精化。</div>';
  const summary='<div class="flow-summary"><b>'+V3B_REFINED_LABEL+'</b>'
    +'<br>refinement <code>'+escapeHtml(refinement.refinementId||'—')+'</code> · environment_source '+escapeHtml(refinement.environmentSource||'—')
    +' · grounding '+escapeHtml(refinement.grounding||'—')+' · '+escapeHtml(refinement.createdAt||'')
    +'<br>coarse（V3-A 战略候选）：state '+escapeHtml(String(candidate?.stateCount??'—'))+' · 3D distance '+metric(candidate?.distanceM,'m')
    +'<br>refined（V3-B corridor-local 精化）：state '+escapeHtml(String(refinement.stateCount??'—'))+' · 3D distance '+metric(refinement.distanceM,'m')
    +' · scalar cost '+metric(refinement.scalarCost)+' · expanded '+escapeHtml(String(refinement.expandedStates??'—'))
    +'<br>fine cell 数 '+escapeHtml(String(refinement.cellCount??'—'))+' · environment cell 数 '+escapeHtml(String(refinement.environmentCellCount??'—'))
    +' · resolution '+metric(refinement.resolutionM,'m')+' · source '+escapeHtml(refinement.resolutionSource||'—')
    +'<br>state 适用性 recorded '+escapeHtml(refinement.recordedApplicability||'—')+' / current '+escapeHtml(refinement.currentApplicability||'—')
    +'<br>reason '+escapeHtml(refinement.reason||'—')
    +'<br>verdicts：operational_route='+escapeHtml(String(refinement.operationalRoute))
    +' · final_validation_performed='+escapeHtml(String(refinement.finalValidationPerformed))
    +' · v3c_validation_pending='+escapeHtml(String(refinement.v3cPending))
    +'<br>V3-B 精化只是某一个 V3-A strategic candidate 的 corridor 内米制细网格工程精化：<b>不是最终安全航路</b>，'
    +'不是 operational route，也不是 exactly validated。exact polygon / terrain / continuous clearance 验证属于 V3-C；'
    +'operational adapter 与 CNS 评估属于 V3-D。本面板不声明该候选为已验证/final 航路。'
    +'<br><b>'+escapeHtml(refinement.disclaimer||'')+'</b></div>';
  return header+summary;
}

function v3bFineGridBlock(refinement){
  const header='<h3>fine grid 与 resolution provenance</h3>';
  if(!refinement)return header+'<div class="empty-note">尚无 V3-B fine grid。</div>';
  const grid=refinement.fineGrid||{},frame=refinement.frame||{},mapping=frame.local_to_geographic||{};
  const axis=frame.axis||{};
  const rows=[
    ['resolution_m',metric(refinement.resolutionM,'m')],
    ['resolution_source',escapeHtml(refinement.resolutionSource||'未记录')],
    ['requested_resolution_m',metric(refinement.requestedResolutionM,'m')],
    ['effective_source_resolution_m',metric(refinement.effectiveSourceResolutionM,'m')],
    ['resolution_deviation_m',metric(refinement.resolutionDeviationM,'m')],
    ['nx × ny',escapeHtml(String(grid.nx??'—'))+' × '+escapeHtml(String(grid.ny??'—'))],
    ['fine cell_count',escapeHtml(String(refinement.cellCount??'—'))],
    ['environment_cell_count',escapeHtml(String(refinement.environmentCellCount??'—'))],
    ['corridor support / center cell',escapeHtml(String(refinement.corridorSupportCellCount??'—'))+' / '+escapeHtml(String(refinement.corridorCenterCellCount??'—'))],
    ['local metric CRS',escapeHtml(frame.horizontal_crs||'未记录')+'（source '+escapeHtml(frame.horizontal_crs_source||'—')+'）'],
    ['local → geographic method',escapeHtml(mapping.method||'未提供（仅米制坐标）')+' · display_only '+escapeHtml(String(mapping.display_only!==false))+' · interpolated_from_parent_cells '+escapeHtml(String(mapping.interpolated_from_parent_cells===true))],
    ['axis / index origin',escapeHtml([axis.column_axis,axis.row_axis,axis.index_origin].filter(Boolean).join(' / ')||'—')],
    ['frame metric_bounds（米制，不是经纬度）',escapeHtml(jsonInline(frame.metric_bounds||null))],
    ['mapping_method / parent resolution',escapeHtml(grid.mapping_method||'—')+' · '+metric(grid.parent_grid_resolution_m,'m')],
    ['terrain_clearance_m / building horizonal_clearance_m / vertical_clearance_m',
      metric(refinement.evidence?.terrain_clearance_m,'m')+' / '+metric(refinement.evidence?.horizonal_clearance_m,'m')+' / '+metric(refinement.evidence?.vertical_clearance_m,'m')],
    ['terrain_sampling / building_mapping',escapeHtml(refinement.evidence?.terrain_sampling||'—')+' · '+escapeHtml(refinement.evidence?.building_mapping||'—')],
    ['not_a_safety_clearance',escapeHtml(String(grid.not_a_safety_clearance!==false))],
    ['resolution_deviation source',escapeHtml(grid.resolution_source||refinement.resolutionSource||'—')]];
  return header
    +'<div class="scroll-list route-list">'+v3bRows(rows)+'</div>'
    +'<div class="parameter-note">fine resolution 只可能来自 <code>explicit_configuration</code>（用户显式配置）或 '
    +'<code>dtm_effective_resolution</code>（DTM 自身有效分辨率）：<b>永远不是硬编码的 30 m 常数</b>，也不构成安全净空（not_a_safety_clearance）。'
    +'当前来源 <code>'+escapeHtml(refinement.resolutionSource||'未记录')+'</code>。</div>'
    +'<div class="parameter-note">coarse→fine 的 soft field 只做 <code>upsampled_without_new_information</code> 复制：'
    +'fine grid <b>不获得新的原始精度</b>（soft index 仍来自母格/既有 canonical 风险层，细网格只是把它复制到更细的索引上，不产生新证据）。</div>';
}

function v3bSearchBlock(refinement){
  if(!refinement)return '';
  const hard=refinement.hardRejection||{};
  const rows=[
    ['expanded_states',escapeHtml(String(refinement.expandedStates??'—'))],
    ['generated_states',escapeHtml(String(refinement.generatedStates??'—'))],
    ['primitive_checks',escapeHtml(String(refinement.primitiveChecks??'—'))],
    ['traversed_cell_checks',escapeHtml(String(refinement.traversedCellChecks??'—'))],
    ['max_stride_cells',escapeHtml(String(refinement.maxStrideCells??'—'))],
    ['traversed cell rejection 总数',escapeHtml(String(hard.totalTraversed??0))],
    ['state / transition rejection 总数',escapeHtml(String(hard.totalState??0))+' / '+escapeHtml(String(hard.totalTransition??0))],
    ['expansion_cap / reached',escapeHtml(String(refinement.expansionCap??'—'))+' / '+escapeHtml(String(refinement.expansionCapReached))],
    ['multi-cell stride edges / traversed cell checks',escapeHtml(String(refinement.multiCellStrideEdgeCount??'—'))+' / '+escapeHtml(String(refinement.traversedCellCheckCount??'—'))],
    ['max heading change / max climb gradient',metric(refinement.maxHeadingChangeDeg,'°')+' / '+metric(refinement.maxClimbGradient)],
    ['altitude range EGM2008 (m)',refinement.altitudeRange?metric(refinement.altitudeRange[0],'m')+' – '+metric(refinement.altitudeRange[1],'m'):'—'],
    ['search_completeness',escapeHtml(refinement.searchCompleteness||'—')]];
  return '<h3>search 规模与 hard rejection</h3>'
    +'<div class="scroll-list route-list">'+v3bRows(rows)+'</div>'
    +'<div class="parameter-note">state_space_shape '+escapeHtml(jsonInline(refinement.stateSpaceShape||{}))
    +'<br>endpoint_binding '+escapeHtml(jsonInline(refinement.endpointBinding||{}))+'</div>'
    +'<div class="parameter-note">state rejection：'+escapeHtml(jsonInline(hard.state||{}))
    +'<br>transition rejection：'+escapeHtml(jsonInline(hard.transition||{}))
    +'<br>traversed cell rejection：'+escapeHtml(jsonInline(hard.traversedCell||{}))
    +' · traversed_cell_checks '+escapeHtml(String(refinement.traversedCellChecks??'—'))+'</div>'
    +'<div class="parameter-note">unknown 永远不可行（unknown_is_never_feasible='+escapeHtml(String(refinement.hardSummary?.unknown_is_never_feasible!==false))
    +'）· 多格 stride 不能跳过中间障碍（intermediate_obstacles_cannot_be_skipped_by_a_stride='
    +escapeHtml(String(refinement.hardSummary?.intermediate_obstacles_cannot_be_skipped_by_a_stride!==false))+'）· audit sample cap per reason '
    +escapeHtml(String(refinement.hardSummary?.audit_sample_cap_per_reason??'—'))+'。</div>'
    +'<div class="parameter-note"><b>search_incomplete 语义</b>：达到 expansion cap（expansion_cap_reached='+escapeHtml(String(refinement.expansionCapReached))
    +'）时结果为 <code>search_incomplete</code>：这是 resource limited（搜索预算耗尽），<b>不是 infeasible</b>，也<b>未证明最优</b>（optimality_proven='
    +escapeHtml(String(refinement.optimalityProven))+'；resource_limit='+escapeHtml(refinement.resourceLimit||'—')+'）。'
    +escapeHtml(refinement.resourceLimitReason||'')+'</div>'
    +'<div class="parameter-note">motion model '+escapeHtml(refinement.motionModel?.model_id||'—')
    +' · turn constraint '+escapeHtml(refinement.motionModel?.turn_constraint||'—')
    +' · exact_curvature_validation '+escapeHtml(refinement.motionModel?.exact_curvature_validation||'—')
    +' · not_a_flight_dynamics_certification_model '+escapeHtml(String(refinement.motionModel?.not_a_flight_dynamics_certification_model!==false))
    +' · altitude_interpolated_along_primitive '+escapeHtml(String(refinement.motionModel?.altitude_interpolated_along_primitive!==false))+'。</div>';
}

function v3bCostBlock(refinement){
  if(!refinement)return '';
  const meta=refinement.costMeta||{};
  const rows=refinement.costComponents.length?refinement.costComponents.map(item=>{
    const statistics=item.indexStatistics||{};
    return '<div class="list-row route-row"><span><b>'+escapeHtml(item.name)+'</b> '
      +(item.enabled?statusBadge('passed'):statusBadge('not_applicable'))
      +'<small>raw '+metric(item.raw)+' '+escapeHtml(item.unit||'')+' · normalized '+metric(item.normalized)
      +' · weight '+(item.weight===null||item.weight===undefined?'—':escapeHtml(String(item.weight)))
      +' · contribution '+metric(item.contribution)+' · edge_count '+escapeHtml(String(item.edgeCount??'—'))+'</small>'
      +'<small>exposure_m '+metric(item.exposureM)+' · exposure_definition '+escapeHtml(item.exposureDefinition||'—')
      +' · index statistics min/max/mean '+metric(statistics.min)+' / '+metric(statistics.max)+' / '+metric(statistics.mean)
      +' （count '+escapeHtml(String(statistics.count??'—'))+'）</small>'
      +'<small>source_resolution_m '+metric(item.sourceResolutionM,'m')+' · mapping_method '+escapeHtml(item.mappingMethod||'—')
      +' · upsampled_without_new_information '+escapeHtml(String(item.upsampledWithoutNewInformation))+'</small>'
      +'<small>provenance sources '+escapeHtml(jsonInline(item.provenanceSources||[]))+(item.provenanceNote?' · '+escapeHtml(item.provenanceNote):'')+'</small>'
      +'<small>'+escapeHtml(item.semantics||'')+'</small>'
      +(item.enabled?'':'<small>'+escapeHtml(item.reason||item.status||'')+'</small>')+'</span></div>';}).join('')
    :'<div class="empty-note">尚无 V3-B cost vector。</div>';
  return '<h3>cost breakdown（含 exposure 与 provenance）</h3>'
    +'<div class="parameter-note">scalar = Σ_edges( length_m + Σ_channels weight × exposure_m )；'
    +'exposure_m = length_m × (index_source + index_target) / 2，index 必须带 provenance 且位于 [0,1]。'
    +'<br>weight 之和没有上限（scalar_weight_sum_is_not_bounded='+escapeHtml(String(meta.scalarWeightSumIsNotBounded))
    +'，scalar_weight_sum='+metric(meta.scalarWeightSum)+'），并且<b>从不进入启发函数</b>'
    +'（scalar_weight_sum_not_used_by_the_heuristic='+escapeHtml(String(meta.scalarWeightSumNotUsedByTheHeuristic))
    +'）：h = 纯 3D 几何距离，与 weight 之和无关。scalar_cost_cap '+metric(meta.scalarCostCap)
    +'（active='+escapeHtml(String(meta.scalarCostCapActive))+'）；energy 保持 pending_model/disabled；CNS 记录为 '
    +escapeHtml((meta.cnsIntegration||{}).integration_mode||'post_route_assessment')+' 且 excluded_from_search_cost='
    +escapeHtml(String((meta.cnsIntegration||{}).excluded_from_search_cost===true))+'。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>';
}

function v3bProvenanceBlock(refinement){
  if(!refinement)return '';
  const audit=refinement.sourceAudit||{},terrain=audit.terrain_dtm||null,buildings=audit.buildings||null;
  const airspace=audit.airspace_policy||null,risk=audit.risk_model||{};
  const terrainText=terrain?escapeHtml(terrain.file_name||terrain.dataset||'—')
    +' · role '+escapeHtml(terrain.role||'—')+' · pixel_size '+metric(terrain.pixel_size,'m')
    +' · nodata '+escapeHtml(String(terrain.nodata??'—'))+' · vertical_reference '+escapeHtml(terrain.vertical_reference||'—')
    +' · vertical_status '+escapeHtml(terrain.vertical_status||'—')+' · size_bytes '+escapeHtml(String(terrain.size_bytes??'—'))
    +' · '+escapeHtml(String(terrain.width??'—'))+'×'+escapeHtml(String(terrain.height??'—'))
    :'未记录（canonical synthetic 无真实 DTM）';
  const buildingText=buildings?escapeHtml(buildings.file_name||'—')+' · layer '+escapeHtml(buildings.layer||'—')
    +' · feature_count '+escapeHtml(String(buildings.feature_count??'—'))+' · spatial_index_available '+escapeHtml(String(buildings.spatial_index_available))
    +' · query_mode '+escapeHtml(buildings.query_mode||'—')+' · crs '+escapeHtml(buildings.crs||'—')
    +' · height_field '+escapeHtml(buildings.height_field||'—')
    :'未记录（canonical synthetic 无建筑源）';
  const airspaceText='not applicable · display-only reference layer';
  const rows=[
    ['read_mode / building_query_mode / airspace_query_mode',escapeHtml([audit.read_mode,audit.building_query_mode,audit.airspace_query_mode].filter(Boolean).join(' · ')||'—')],
    ['terrain_dtm',terrainText],
    ['buildings',buildingText],
    ['airspace_policy',airspaceText],
    ['risk_model（soft contributor 复用）',escapeHtml(risk.algorithm_id||'—')+'@'+escapeHtml(risk.algorithm_version||'—')
      +' · status '+escapeHtml(risk.status||'—')+' · soft_fields_reused '+escapeHtml(String(risk.soft_fields_reused===true))
      +' · reused_contributors '+escapeHtml(jsonInline(risk.reused_contributors||[]))],
    ['metric_frame',escapeHtml(jsonInline(audit.metric_frame||{}))],
    ['full_raster_resample / source_geometry_modified / exact_validation_performed',escapeHtml(String(audit.full_raster_resample===true))+' / '
      +escapeHtml(String(audit.source_geometry_modified===true))+' / '+escapeHtml(String(audit.exact_validation_performed===true))],
    ['policy_fingerprint',escapeHtml(audit.policy_fingerprint||'—')],
    ['source audit fingerprint',escapeHtml(audit.fingerprint||refinement.evidence?.source_audit_fingerprint||'—')],
    ['source_type / lineage',escapeHtml(refinement.evidence?.source_type||'—')+' · '+escapeHtml(jsonInline(audit.lineage||{}))],
    ['adapter',escapeHtml(audit.adapter_id||'—')+'@'+escapeHtml(audit.adapter_version||'—')]];
  const pending=(refinement.evidence?.v3c_pending||refinement.semantics?.v3c_pending||[]);
  return '<h3>data provenance（fine environment source audit）</h3>'
    +'<div class="scroll-list route-list">'+v3bRows(rows)+'</div>'
    +'<div class="parameter-note"><b>V3-C pending</b>（连续几何验证尚未执行）：'
    +escapeHtml(pending.join(' / ')||'—')
    +'<br>terrain hard floor = 相交有效 FABDEM 像元的<b>最大</b> EGM2008 高程 + explicit terrain clearance'
    +'（绝不是 center sample 或 mean）；building = footprint 按 explicit horizontal clearance 缓存成的保守包络，'
    +'required floor = ground + height + vertical clearance（不是 exact polygon clearance）；airspace 仅供显示。</div>';
}

function v3bReadinessBlock(model){
  const readiness=model.refinementReadiness||{},scope=readiness.scope||{implemented:[],not_implemented:[]};
  const real=readiness.realDataReadiness||{},selected=readiness.selectedCandidate,fine=readiness.finePolicy||{};
  const policyRows=[
    ['status / stage',escapeHtml(readiness.status)+' · '+escapeHtml(readiness.stage)],
    ['model_scope / algorithm',escapeHtml(readiness.modelScope||'—')+' · '+escapeHtml((readiness.algorithm||{}).algorithm_id||'—')
      +'@'+escapeHtml((readiness.algorithm||{}).algorithm_version||'—')
      +' · registered_in_algorithm_registry '+escapeHtml(String((readiness.algorithm||{}).registered_in_algorithm_registry===true))],
    ['resolution_policy',escapeHtml(readiness.resolutionPolicy||'—')],
    ['real data adapter',escapeHtml(real.adapter_id||'—')+'@'+escapeHtml(real.adapter_version||'—')+' · status '+escapeHtml(real.status||'—')
      +' · airspace display-only / not applicable'],
    ['real terrain_dtm / buildings',escapeHtml(real.terrain_dtm||'未配置')+' · '+escapeHtml(real.buildings||'未配置')],
    ['v3_policy_readiness',escapeHtml((readiness.v3PolicyReadiness||{}).status||'—')+' · missing '
      +escapeHtml(jsonInline((readiness.v3PolicyReadiness||{}).missing_parameters||[]))],
    ['environment_sources',escapeHtml((readiness.environmentSources||[]).join(' / '))],
    ['allowed_refinement_statuses',escapeHtml((readiness.allowedRefinementStatuses||[]).join(' / '))]];
  const candidateText=selected
    ?'experiment '+escapeHtml(selected.experiment_id||'—')+' · result_status '+escapeHtml(selected.result_status||'—')
      +' · route '+escapeHtml(selected.route_id||'—')+' · corridor '+escapeHtml(selected.corridor_id||'—')
      +' · support cells '+escapeHtml(String(selected.support_cell_count??'—'))+' · refinement_count '+escapeHtml(String(selected.refinement_count??0))
    :'未选中：需要 current 的 V3-A strategic_candidate + corridor（先运行 V3-A 实验）';
  const resolutionSourceOptions=V3B_RESOLUTION_SOURCES.map(value=>'<option value="'+escapeHtml(value)+'" '+(fine.resolution_source===value?'selected':'')+'>'+escapeHtml(value)+'</option>').join('');
  const environmentOptions=(readiness.environmentSources||V3B_ENVIRONMENT_SOURCES).map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('');
  return '<h3>V3-B readiness '+statusBadge(readiness.status||'not_calculated')+'</h3>'
    +'<div class="parameter-note">'+escapeHtml(readiness.note||'')+'<br>'+escapeHtml(readiness.architecture||model.v3bArchitecture||'')+'</div>'
    +listBlock('V3-B implemented',scope.implemented)+listBlock('V3-B not_implemented',scope.not_implemented)
    +'<div class="scroll-list route-list">'+v3bRows(policyRows)+'</div>'
    +'<div class="parameter-note"><b>blocking_reasons</b> '+escapeHtml(jsonInline(readiness.blockingReasons||[]))
    +'<br>real_data blocking_reasons '+escapeHtml(jsonInline(real.blocking_reasons||[]))
    +'<br>required_before_real_run '+escapeHtml(jsonInline(real.required_before_real_run||[]))
    +'<br>'+escapeHtml(real.semantics||'')+'</div>'
    +'<div class="parameter-note">selected strategic candidate：'+candidateText+'</div>'
    +'<h3>V3-B 输入：fine policy（显式，无默认）</h3>'
    +'<div class="form-grid"><label>local metric CRS<input class="panel-input" id="v3bFineCrs" placeholder="例如 EPSG:32651（必须显式）" value="'+escapeHtml(fine.horizontal_crs||'')+'"></label>'
    +'<label>resolution_source<select id="v3bFineResolutionSource"><option value="">未显式选择（无默认）</option>'+resolutionSourceOptions+'</select></label>'
    +'<label>resolution_m (m，可空)<input class="panel-input" type="number" step="any" id="v3bFineResolution" placeholder="禁止默认 30 m" value="'+escapeHtml(fine.resolution_m??'')+'"></label>'
    +'<label>max_stride_cells<input class="panel-input" type="number" min="1" id="v3bFineMaxStride" value="'+escapeHtml(fine.max_stride_cells??1)+'"></label></div>'
    +'<label>fine policy 来源<input class="panel-input" id="v3bFineSource" value="'+escapeHtml(fine.source||'')+'"></label>'
    +'<label class="check-row"><input type="checkbox" id="v3bFineConfirmed" '+(fine.confirmed?'checked':'')+'>fine policy 已由项目工程依据确认</label>'
    +'<div class="button-row"><button class="secondary" id="saveRoutePlannerV3FinePolicy">保存 V3-B fine policy</button></div>'
    +'<h3>V3-B 输入：corridor-local 精化</h3>'
    +'<div class="form-grid"><label>environment_source<select id="v3bEnvironmentSource">'+environmentOptions+'</select></label>'
    +'<label>refinement_cell_size_m（显式细网格分辨率 m）<input class="panel-input" type="number" step="any" min="0.001" id="v3bRefinementCellSize" placeholder="留空则使用已保存的 fine policy"></label>'
    +'<label>max_stride_cells<input class="panel-input" type="number" min="1" id="v3bMaxStride" value="'+escapeHtml(fine.max_stride_cells??1)+'"></label>'
    +'<label>synthetic base_surface_elevation_m<input class="panel-input" type="number" step="any" id="v3bSyntheticBaseElevation" value="0"></label></div>'
    +'<div class="button-row"><button class="primary" id="evaluateRoutePlannerV3Refinement">运行 V3-B corridor-local 精化</button></div>'
    +'<div class="parameter-note">refinement_cell_size_m 与 max_stride_cells 会作为 synthetic fine spec 的 resolution_m / max_stride_cells 原样提交'
    +'（advanced 的 terrain_spikes / building_blocks 不在 UI 暴露，保持省略）。canonical_synthetic 只接受 explicit_configuration 分辨率；'
    +'dtm_effective_resolution 需要 configured_real_sources 与真实 DTM。缺少候选/未确认 policy 时后端会明确拒绝，不构造假环境。</div>';
}

function v3bHistoryBlock(model){
  const applicability=model.refinementApplicability||{items:[]};
  const rows=applicability.items.length?applicability.items.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(item.refinementId||'—')+'</b> '+statusBadge(item.status||'not_calculated')
    +'<small>experiment '+escapeHtml(item.experimentId||'—')+' · recorded_applicability '+escapeHtml(item.recordedApplicability||'—')
    +' · current_applicability '+escapeHtml(item.currentApplicability||'—')+'</small>'
    +'<small>changed_components '+escapeHtml(jsonInline(item.changedComponents||[]))+'</small>'
    +(item.reasons||[]).map(reason=>'<small>'+escapeHtml(reason)+'</small>').join('')
    +'<small>refinement fingerprint '+escapeHtml(String(item.refinementFingerprint||'—').slice(0,28))+'</small></span></div>').join('')
    :'<div class="empty-note">尚无 V3-B 精化记录</div>';
  return '<h3>V3-B 精化历史与适用性 '+statusBadge(applicability.status||'not_calculated')+'</h3>'
    +'<div class="parameter-note">stale_count '+escapeHtml(String(applicability.staleCount??0))+' / 总数 '+escapeHtml(String(applicability.count??0))
    +' · 指纹组件 '+escapeHtml((applicability.components||[]).join(', ')||'—')
    +'<br><b>stale：源/corridor/policy 变化后必须重跑</b>——strategic / corridor / policy / source / frame / fine grid 任一组件变化即失效，'
    +'旧精化结论不得复用。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>';
}

// ---- V3-C continuous validation (read-only projection) -------------------------------

function v3cContinuousReadinessModel(flow){
  const readiness=flow?.route_planner_v3_continuous_readiness||{};
  return {
    status:readiness.status||'not_calculated',stage:readiness.stage||'V3-C',
    modelScope:readiness.model_scope||'',
    architecture:readiness.architecture||'',note:readiness.note||V3C_NOTE_FALLBACK,
    scope:readiness.stage_scope||{implemented:[],not_implemented:[]},
    algorithm:readiness.algorithm||{},
    selectedRefinement:readiness.selected_refinement||null,
    validationPolicy:readiness.validation_policy||{},
    planningPolicy:readiness.v3_planning_policy||{},
    realDataReadiness:readiness.real_data_readiness||{status:'unknown',blocking_reasons:[]},
    blockingReasons:readiness.blocking_reasons||[],
    evidenceSources:readiness.evidence_sources||V3C_EVIDENCE_SOURCES,
    allowedResultStatuses:readiness.allowed_result_statuses||V3C_RESULT_STATUSES,
    boundaries:readiness.boundaries||{},
  };
}

export function routePlannerV3ValidationModel(item){
  const result=item?.result||{};
  const route=result.continuous_route||{};
  const analytic=(route.horizontal_geometry||{}).analytic||{};
  const linearized=(route.horizontal_geometry||{}).linearized||{};
  const vertical=route.vertical||{};
  const kinematics=result.kinematics||{};
  const margins=result.min_margins||{};
  const limits=result.resource_limits||{};
  return {
    validationId:item?.validation_id||null,refinementId:item?.refinement_id||null,
    experimentId:item?.experiment_id||null,createdAt:item?.created_at||null,
    evidenceSource:item?.evidence_source||null,
    status:result.status||'not_ready',reason:result.reason||null,
    domainStatuses:result.domain_statuses||{},
    domains:V3C_DOMAINS.map(domain=>({
      domain,status:(result.domain_statuses||{})[domain]||'skipped',
      reason:((result.domains||{})[domain]||{}).reason||null,
      violations:(((result.domains||{})[domain]||{}).violations||[]).length,
      unresolved:(((result.domains||{})[domain]||{}).unresolved||[]).length,
      evaluated:((result.domains||{})[domain]||{}).evaluated===true,
    })),
    violations:(result.violations||[]).map(v=>({
      domain:v.domain,reasonId:v.reason_id,startDistanceM:v.start_distance_m,endDistanceM:v.end_distance_m,
      startCoordinate:v.start_coordinate,endCoordinate:v.end_coordinate,
      required:v.required,observed:v.observed,margin:v.margin,evidence:v.evidence||{},
    })),
    unresolved:(result.unresolved_evidence||[]).map(v=>({
      domain:v.domain,reasonId:v.reason_id,startDistanceM:v.start_distance_m,endDistanceM:v.end_distance_m,
      required:v.required,observed:v.observed,evidence:v.evidence||{},
    })),
    margins:{
      airspaceHorizontalM:margins.airspace_horizontal_m,terrainVerticalM:margins.terrain_vertical_m,
      buildingHorizontalM:margins.building_horizontal_m,buildingVerticalM:margins.building_vertical_m,
      altitudeLowerM:margins.altitude_lower_m,altitudeUpperM:margins.altitude_upper_m,
      turnRadiusM:margins.turn_radius_m,climbGradientMargin:margins.climb_gradient_margin,
      descentGradientMargin:margins.descent_gradient_margin},
    analytic:{primitiveCount:analytic.primitive_count,straightCount:analytic.straight_count,
      arcCount:analytic.arc_count,turnCount:analytic.turn_count,
      totalHorizontalLengthM:analytic.total_horizontal_length_m,
      curvatureContinuity:analytic.curvature_continuity,
      continuousCurvature:analytic.continuous_curvature===true,
      c2:analytic.c2===true,clothoid:analytic.clothoid,
      turnRadiusPolicy:analytic.turn_radius_policy,
      radiusReducedAnywhere:analytic.radius_reduced_anywhere===true},
    linearized:{pointCount:linearized.point_count,
      actualMaxChordErrorM:linearized.actual_max_chord_error_m,
      curveChordErrorM:linearized.curve_chord_error_m,method:linearized.method,
      notTheMathematicalCurve:linearized.not_the_mathematical_curve===true},
    vertical:{totalDistanceM:vertical.total_distance_m,minZ:vertical.min_z_egm2008_m,
      maxZ:vertical.max_z_egm2008_m,maxClimbGradient:vertical.max_climb_gradient_observed,
      maxDescentGradient:vertical.max_descent_gradient_observed,
      method:vertical.method},
    kinematics:{minimumTurnRadiusObservedM:kinematics.minimum_turn_radius_observed_m,
      requiredMinimumTurnRadiusM:kinematics.required_minimum_turn_radius_m,
      maxClimbGradientObserved:kinematics.max_climb_gradient_observed,
      maxDescentGradientObserved:kinematics.max_descent_gradient_observed,
      maxAllowedClimbGradient:kinematics.max_allowed_climb_gradient,
      maxAllowedDescentGradient:kinematics.max_allowed_descent_gradient,
      tangentHeadingContinuityVerified:kinematics.tangent_heading_continuity_verified===true,
      selfIntersectionIsFailure:kinematics.self_intersection_is_failure===true,
      turnVerification:kinematics.turn_verification},
    resourceLimits:{maxValidationSamples:limits.max_validation_samples,sampleCount:limits.sample_count,
      maxRuntimeS:limits.max_runtime_s,runtimeS:limits.runtime_s,
      resourceLimited:limits.resource_limited===true,resourceLimitReason:limits.resource_limit_reason},
    curveError:result.curve_error||item?.curve_error||null,
    sourceAudit:result.source_audit||{},
    evidenceComponents:item?.evidence_components||{},
    currentApplicability:item?.current_applicability||null,
    validationFingerprint:result.validation_fingerprint||null,
    verdicts:result.verdicts||{},
    semantics:result.semantics||{},
    // Hard boundaries: never render a V3-C result as an operational route.
    operationalRoute:false,cnsAssessed:false,
    projection:((route.primitives||[]).flatMap(p=>p.sampled_points_metric||[])),
    disclaimer:result.disclaimer||'',
  };
}

export function routePlannerV3ContinuousModel(flow){
  const readiness=v3cContinuousReadinessModel(flow);
  const snapshot=flow?.route_planner_v3_validations||{};
  const collection=flow?.route_planner_v3_experiments||{};
  const detail=flow?.route_planner_v3_detail||{};
  const records=detail.records||[];
  const activeId=collection.active_experiment_id||null;
  const record=records.find(item=>item.experiment_id===activeId)||records[0]||null;
  const refinement=(record?.refinements||[])[0]||null;
  const validation=(refinement?.validations||[])[0]||null;
  const applicability={
    status:snapshot.status||'not_calculated',count:snapshot.count||0,staleCount:snapshot.stale_count||0,
    validatedRouteCount:snapshot.validated_route_count||0,semantics:snapshot.semantics||'',
    components:snapshot.components||[],
    items:(snapshot.items||[]).map(item=>({validationId:item.validation_id,refinementId:item.refinement_id,
      experimentId:item.experiment_id,status:item.status,domainStatuses:item.domain_statuses||{},
      recordedApplicability:item.recorded_applicability,currentApplicability:item.current_applicability,
      changedComponents:item.changed_components||[],reasons:item.reasons||[],
      validationFingerprint:item.validation_fingerprint,evidenceComponents:item.evidence_components||{}}))};
  return {
    readiness,
    status:collection.status||'not_calculated',
    note:collection.v3c_note||V3C_NOTE_FALLBACK,
    architecture:collection.v3c_architecture||'',
    allowedResultStatuses:collection.allowed_validation_statuses||V3C_RESULT_STATUSES,
    activeExperimentId:activeId,
    validationId:validation?.validation_id||null,
    validationModel:validation?routePlannerV3ValidationModel(validation):null,
    refinementId:refinement?.refinement_id||null,
    applicability,
    refinementStale:(refinement?.current_applicability||null)==='stale',
  };
}

function v3cRows(rows){
  return (rows||[]).map(row=>'<div class="list-row route-row"><span><b>'+escapeHtml(row[0])+'</b>'
    +'<small>'+row[1]+'</small></span></div>').join('');
}

function v3cDomainBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const rows=validation.domains.map(item=>[
    item.domain,statusBadge(item.status)+(item.reason?' · '+escapeHtml(item.reason):'')
      +' · violation '+escapeHtml(String(item.violations))+' · unresolved '+escapeHtml(String(item.unresolved))
      +' · evaluated '+escapeHtml(String(item.evaluated))]);
  return '<h3>V3-C domain status '+statusBadge(validation.status)+'</h3>'
    +'<div class="parameter-note"><b>'+escapeHtml(V3C_OPERATIONAL_LABEL)+'</b>'
    +'<br>只有 geometry / terrain / building / altitude / kinematics 全部 passed 才是 '
    +escapeHtml('validated_route')+'；确定违反 ⇒ failed（replan_required，不自动修路）；缺证据 ⇒ unresolved；'
    +'源/refined candidate stale ⇒ not_ready；资源上限 ⇒ validation_incomplete（绝不是 failed）。</div>'
    +'<div class="scroll-list route-list">'+v3cRows(rows)+'</div>';
}

function v3cMarginBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const m=validation.margins;
  const rows=[
    ['terrain 垂向最小 margin (m)',metric(m.terrainVerticalM,'m')],
    ['building 水平 / 垂向最小 margin (m)',metric(m.buildingHorizontalM,'m')+' / '+metric(m.buildingVerticalM,'m')],
    ['altitude lower / upper margin (m)',metric(m.altitudeLowerM,'m')+' / '+metric(m.altitudeUpperM,'m')],
    ['turn radius margin (m)',metric(m.turnRadiusM,'m')],
    ['climb / descent gradient margin',metric(m.climbGradientMargin)+' / '+metric(m.descentGradientMargin)]];
  return '<h3>最小 margin（各 domain）</h3><div class="scroll-list route-list">'+v3cRows(rows)+'</div>';
}

function v3cViolationBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const rows=(validation.violations||[]).length
    ?validation.violations.map(item=>'<div class="list-row route-row"><span><b>'
      +escapeHtml(item.domain)+' · '+escapeHtml(item.reasonId)+'</b>'
      +'<small>distance '+metric(item.startDistanceM,'m')+' → '+metric(item.endDistanceM,'m')
      +' · required '+escapeHtml(String(item.required??'—'))+' · observed '+escapeHtml(String(item.observed??'—'))
      +' · margin '+metric(item.margin)+'</small>'
      +'<small>evidence '+escapeHtml(jsonInline(item.evidence||{}))+'</small></span></div>').join('')
    :'<div class="empty-note">没有 violation interval</div>';
  const unresolvedRows=(validation.unresolved||[]).length
    ?validation.unresolved.map(item=>'<div class="list-row route-row"><span><b>'+escapeHtml(item.domain)
      +' · '+escapeHtml(item.reasonId)+'</b><small>required '+escapeHtml(String(item.required??'—'))
      +' · observed '+escapeHtml(String(item.observed??'—'))+'</small></span></div>').join('')
    :'<div class="empty-note">没有 unresolved interval</div>';
  return '<h3>violation intervals（统一 domain / reason / distance / required / observed / margin / evidence）</h3>'
    +'<div class="parameter-note">失败只给出结构化证据与 replan_required=true；V3-C 不自动修路、不自动 replan。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>'
    +'<h3>unresolved evidence（unknown 一律不等于 safe）</h3>'
    +'<div class="scroll-list route-list">'+unresolvedRows+'</div>';
}

function v3cGeometryBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const a=validation.analytic,l=validation.linearized,v=validation.vertical;
  const rows=[
    ['primitive / straight / arc（解析几何）',escapeHtml(String(a.primitiveCount??'—'))+' / '+escapeHtml(String(a.straightCount??'—'))+' / '+escapeHtml(String(a.arcCount??'—'))],
    ['turn count / turn radius policy',escapeHtml(String(a.turnCount??'—'))+' · '+escapeHtml(a.turnRadiusPolicy||'—')],
    ['radius_reduced_anywhere',escapeHtml(String(a.radiusReducedAnywhere===true))+'（V3-C 绝不减小 R 以勉强通过转弯）'],
    ['curvature continuity',escapeHtml(a.curvatureContinuity||'—')+' · continuous_curvature '+escapeHtml(String(a.continuousCurvature===true))
      +' · C2 '+escapeHtml(String(a.c2===true))+' · clothoid '+escapeHtml(a.clothoid||'future_work_not_implemented')],
    ['realized horizontal length (m)',metric(a.totalHorizontalLengthM,'m')],
    ['linearized point count / method',escapeHtml(String(l.pointCount??'—'))+' · '+escapeHtml(l.method||'—')],
    ['curve_chord_error_m（显式，无默认）',metric(l.curveChordErrorM,'m')],
    ['actual max chord bound (m)',metric(l.actualMaxChordErrorM,'m')+'（必须 ≤ 显式 tolerance）'],
    ['linearized ≠ 数学曲线',escapeHtml(String(l.notTheMathematicalCurve===true))],
    ['vertical z(s) method / min / max',escapeHtml(v.method||'—')+' · '+metric(v.minZ,'m')+' / '+metric(v.maxZ,'m')],
    ['max climb / descent gradient observed',metric(v.maxClimbGradient)+' / '+metric(v.maxDescentGradient)]];
  return '<h3>连续几何实现（analytic + explicit chord error）</h3>'
    +'<div class="parameter-note">只保证 <b>position + heading 连续（C1）</b>；曲率在 straight↔arc 处可跳变，'
    +'禁止声明 continuous-curvature / C2。圆弧是解析几何，折线是按 explicit curve_chord_error_m 的有界近似；'
    +'vector predicate 对 linearized representation（含 error envelope）精确。</div>'
    +'<div class="scroll-list route-list">'+v3cRows(rows)+'</div>';
}

function v3cKinematicBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const k=validation.kinematics;
  const rows=[
    ['minimum_turn_radius_observed / required (m)',metric(k.minimumTurnRadiusObservedM,'m')+' / '+metric(k.requiredMinimumTurnRadiusM,'m')],
    ['max climb observed / allowed',metric(k.maxClimbGradientObserved)+' / '+metric(k.maxAllowedClimbGradient)],
    ['max descent observed / allowed',metric(k.maxDescentGradientObserved)+' / '+metric(k.maxAllowedDescentGradient)],
    ['tangent heading continuity verified',escapeHtml(String(k.tangentHeadingContinuityVerified===true))],
    ['turn verification',escapeHtml(k.turnVerification||'—')],
    ['self-intersection（diagnostic）',escapeHtml(String(k.selfIntersectionIsFailure===true))+' · 只有 policy 显式规定才算 failure']];
  return '<h3>kinematics（analytic R + tangent heading）</h3>'
    +'<div class="parameter-note">转弯由 <b>解析圆弧</b> 重新验证 R ≥ Rmin 与 tangent heading 连续；'
    +'V3-B 的 R·|Δψ| 弧长代理不是最终转弯验证。</div>'
    +'<div class="scroll-list route-list">'+v3cRows(rows)+'</div>';
}

function v3cSourceBlock(model){
  const validation=model.validationModel;
  if(!validation)return '';
  const audit=validation.sourceAudit||{};
  const curve=validation.curveError||{};
  const limits=validation.resourceLimits;
  const rows=[
    ['evidence source / sample_count',escapeHtml(validation.evidenceSource||'—')+' · '+escapeHtml(String(limits.sampleCount??'—'))],
    ['max_validation_samples / resource_limited',escapeHtml(String(limits.maxValidationSamples??'—'))+' · '+escapeHtml(String(limits.resourceLimited))],
    ['runtime_s / max_runtime_s',escapeHtml(String(limits.runtimeS??'—'))+' / '+escapeHtml(String(limits.maxRuntimeS??'—'))],
    ['resource_limit_reason',escapeHtml(limits.resourceLimitReason||'—')],
    ['curve error method / actual / requested',escapeHtml(curve.method||'—')+' · '+metric(curve.actual_max_chord_error_m,'m')
      +' / '+metric(curve.requested_max_chord_error_m,'m')],
    ['terrain 语义',escapeHtml('source_native_raster_validation')+'（不声称真实世界地形数学连续 exact）'],
    ['NoData policy',escapeHtml('node 级 NoData ⇒ unresolved；禁止 bilinear 填补、禁止当 0')],
    ['adapter',escapeHtml(audit.adapter_id||'—')+' · '+escapeHtml(jsonInline(audit.validation_evidence_source||''))],
    ['validation fingerprint',escapeHtml(String(validation.validationFingerprint||'—').slice(0,32))],
    ['fingerprint components',escapeHtml(jsonInline(Object.keys(validation.evidenceComponents||{})))]];
  return '<h3>source / tolerance / fingerprint</h3>'
    +'<div class="scroll-list route-list">'+v3cRows(rows)+'</div>';
}

function v3cReadinessBlock(model){
  const readiness=model.readiness||{},scope=readiness.scope||{implemented:[],not_implemented:[]};
  const policy=readiness.validationPolicy||{},planning=readiness.planningPolicy||{};
  const real=readiness.realDataReadiness||{};
  const selected=readiness.selectedRefinement;
  const policyRows=[
    ['status / stage / model_scope',escapeHtml(readiness.status)+' · '+escapeHtml(readiness.stage)+' · '+escapeHtml(readiness.modelScope||'—')],
    ['algorithm',escapeHtml((readiness.algorithm||{}).algorithm_id||'—')+'@'+escapeHtml((readiness.algorithm||{}).algorithm_version||'—')
      +' · registered_in_algorithm_registry '+escapeHtml(String((readiness.algorithm||{}).registered_in_algorithm_registry===true))],
    ['validator versions',escapeHtml(jsonInline((readiness.algorithm||{}).validator_versions||{}))],
    ['validation policy status',escapeHtml(policy.status||'—')+' · curve_chord_error_m '+metric(policy.curve_chord_error_m,'m')
      +' · missing '+escapeHtml(jsonInline(policy.missing_parameters||[]))],
    ['planning policy（重验证的安全参数来源）','aircraft_min_turn_radius_m '+metric(planning.aircraft_min_turn_radius_m,'m')
      +' · terrain_clearance_m '+metric(planning.terrain_clearance_m,'m')
      +' · building h/v '+metric(planning.building_horizontal_clearance_m,'m')+' / '+metric(planning.building_vertical_clearance_m,'m')],
    ['real data adapter',escapeHtml(real.adapter_id||'—')+'@'+escapeHtml(real.adapter_version||'—')+' · status '+escapeHtml(real.status||'—')],
    ['evidence_sources',escapeHtml((readiness.evidenceSources||[]).join(' / '))],
    ['allowed_result_statuses',escapeHtml((readiness.allowedResultStatuses||[]).join(' / '))]];
  const refinementText=selected
    ?'refinement '+escapeHtml(selected.refinement_id||'—')+' · experiment '+escapeHtml(selected.experiment_id||'—')
      +' · result_status '+escapeHtml(selected.result_status||'—')+' · applicability '+escapeHtml(selected.current_applicability||'—')
      +' · validation_count '+escapeHtml(String(selected.validation_count??0))
    :'未选中：需要 current 的 V3-B refined_candidate（先运行 V3-B corridor-local 精化）';
  const sourceOptions=(readiness.evidenceSources||V3C_EVIDENCE_SOURCES).map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('');
  return '<h3>V3-C readiness '+statusBadge(readiness.status||'not_calculated')+'</h3>'
    +'<div class="parameter-note">'+escapeHtml(readiness.note||'')+'<br>'+escapeHtml(readiness.architecture||model.architecture||'')+'</div>'
    +listBlock('V3-C implemented',scope.implemented)+listBlock('V3-C not_implemented',scope.not_implemented)
    +'<div class="scroll-list route-list">'+v3cRows(policyRows)+'</div>'
    +'<div class="parameter-note"><b>blocking_reasons</b> '+escapeHtml(jsonInline(readiness.blockingReasons||[]))
    +'<br>boundaries '+escapeHtml(jsonInline(readiness.boundaries||{}))+'</div>'
    +'<div class="parameter-note">selected refined candidate：'+refinementText+'</div>'
    +'<h3>V3-C 输入：validation policy（显式，无默认）</h3>'
    +'<div class="form-grid"><label>curve_chord_error_m（必须显式，无安全默认）<input class="panel-input" type="number" step="any" min="0.000001" id="v3cChordError" placeholder="例如 0.5" value="'+escapeHtml(policy.curve_chord_error_m??'')+'"></label>'
    +'<label>max_validation_samples<input class="panel-input" type="number" min="1" id="v3cMaxSamples" value="'+escapeHtml(policy.max_validation_samples??200000)+'"></label>'
    +'<label>max_runtime_s（可空）<input class="panel-input" type="number" step="any" min="0.000001" id="v3cMaxRuntime" value="'+escapeHtml(policy.max_runtime_s??'')+'"></label>'
    +'<label>use_curve_error_envelope<select id="v3cEnvelope"><option value="true" '+((policy.use_curve_error_envelope!==false)?'selected':'')+'>true</option><option value="false" '+((policy.use_curve_error_envelope===false)?'selected':'')+'>false</option></select></label></div>'
    +'<label>validation policy 来源<input class="panel-input" id="v3cPolicySource" value="'+escapeHtml(policy.source||'')+'"></label>'
    +'<label class="check-row"><input type="checkbox" id="v3cPolicyConfirmed" '+(policy.confirmed?'checked':'')+'>validation policy 已由项目工程依据确认</label>'
    +'<div class="button-row"><button class="secondary" id="saveRoutePlannerV3ValidationPolicy">保存 V3-C validation policy</button></div>'
    +'<h3>V3-C 输入：连续验证</h3>'
    +'<div class="form-grid"><label>evidence_source<select id="v3cEvidenceSource">'+sourceOptions+'</select></label></div>'
    +'<div class="button-row"><button class="primary" id="evaluateRoutePlannerV3Validation" '+(readiness.status==='passed'?'':'disabled')+'>运行 V3-C 连续几何与源几何验证</button></div>'
    +'<div class="parameter-note"><b>'+escapeHtml(V3C_OPERATIONAL_LABEL)+'</b>：即使 status=validated_route，也强制 '
    +'operational_route=false、cns_assessed=false，且不写 operational_routes / algorithm_selection / spatial_3d。'
    +'缺少 curve_chord_error_m 或 Rmin 时后端明确拒绝，不构造假证据。</div>';
}

function v3cHistoryBlock(model){
  const applicability=model.applicability||{items:[]};
  const rows=applicability.items.length?applicability.items.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(item.validationId||'—')+'</b> '+statusBadge(item.status||'not_calculated')
    +'<small>refinement '+escapeHtml(item.refinementId||'—')+' · experiment '+escapeHtml(item.experimentId||'—')
    +' · recorded '+escapeHtml(item.recordedApplicability||'—')+' · current '+escapeHtml(item.currentApplicability||'—')+'</small>'
    +'<small>domain statuses '+escapeHtml(jsonInline(item.domainStatuses||{}))+'</small>'
    +'<small>changed_components '+escapeHtml(jsonInline(item.changedComponents||[]))+'</small>'
    +(item.reasons||[]).map(reason=>'<small>'+escapeHtml(reason)+'</small>').join('')
    +'<small>validation fingerprint '+escapeHtml(String(item.validationFingerprint||'—').slice(0,28))+'</small></span></div>').join('')
    :'<div class="empty-note">尚无 V3-C validation 记录</div>';
  return '<h3>V3-C validation 历史与适用性 '+statusBadge(applicability.status||'not_calculated')+'</h3>'
    +'<div class="parameter-note">stale_count '+escapeHtml(String(applicability.staleCount??0))+' / 总数 '+escapeHtml(String(applicability.count??0))
    +' · validated_route '+escapeHtml(String(applicability.validatedRouteCount??0))
    +' · 指纹组件 '+escapeHtml((applicability.components||[]).join(', ')||'—')
    +'<br>refinement fingerprint / continuous policy / curve tolerance / source audit / CRS-transform / validator versions 任一变化即 stale，必须重跑。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>';
}

export function routePlannerV3ValidationPanel(flow){
  const model=routePlannerV3ContinuousModel(flow);
  const validation=model.validationModel;
  const summary=validation?'<div class="flow-summary"><b>'+escapeHtml(V3C_OPERATIONAL_LABEL)+'</b>'
    +'<br>validation '+escapeHtml(validation.validationId||'—')+' '+statusBadge(validation.status)
    +' · evidence_source '+escapeHtml(validation.evidenceSource||'—')
    +' · arc/straight/turn '+escapeHtml(String((validation.analytic||{}).arcCount??'—'))+' / '
    +escapeHtml(String((validation.analytic||{}).straightCount??'—'))+' / '+escapeHtml(String((validation.analytic||{}).turnCount??'—'))
    +'<br>curve_chord_error_m '+metric((validation.linearized||{}).curveChordErrorM,'m')
    +' · actual max chord '+metric((validation.linearized||{}).actualMaxChordErrorM,'m')
    +' · min turn radius observed '+metric((validation.kinematics||{}).minimumTurnRadiusObservedM,'m')
    +'<br>max climb / descent '+metric((validation.vertical||{}).maxClimbGradient)+' / '+metric((validation.vertical||{}).maxDescentGradient)
    +'<br>realized distance '+metric((validation.vertical||{}).totalDistanceM,'m')
    +' · violations '+escapeHtml(String((validation.violations||[]).length))
    +' · unresolved '+escapeHtml(String((validation.unresolved||[]).length))
    +'<br>'+escapeHtml(validation.disclaimer||'')+'</div>'
    :(model.refinementStale
      ?'<div class="parameter-note"><b>V3-B refinement 已 stale</b>：stale 的 refined candidate 不得进入 V3-C，必须先重跑 V3-B。</div>'
      :'<div class="empty-note">尚无 V3-C validation：保存已确认的 validation policy，再对 current 的 V3-B refined_candidate 运行连续验证。</div>');
  return '<h3>V3-C 连续几何实现 + source-native/vector 验证 '+statusBadge(model.status)+'</h3>'
    +'<div class="parameter-note">'+escapeHtml(model.note||'')+'<br>'+escapeHtml(model.architecture||'')+'</div>'
    +summary
    +v3cDomainBlock(model)
    +v3cMarginBlock(model)
    +v3cViolationBlock(model)
    +v3cGeometryBlock(model)
    +v3cKinematicBlock(model)
    +v3cSourceBlock(model)
    +v3cReadinessBlock(model)
    +v3cHistoryBlock(model);
}

function v3bPanel(model){
  const refinement=(model.refinements||[])[0]||null;
  return v3bRefinementBlock(model,refinement)
    +v3bFineGridBlock(refinement)
    +v3bSearchBlock(refinement)
    +v3bCostBlock(refinement)
    +v3bProvenanceBlock(refinement)
    +v3bReadinessBlock(model)
    +v3bHistoryBlock(model);
}

export function routePlannerV3Panel(flow){
  const model=routePlannerV3Model(flow);
  const readiness=routePlannerV3ReadinessModel(flow);
  const candidate=model.candidate;
  const candidateBlock=candidate
    ?'<div class="flow-summary"><b>'+escapeHtml(String(candidate.routeId||'—'))+'</b> '+statusBadge(candidate.status)
      +'<br>3D distance '+metric(candidate.distanceM,'m')+' · scalar cost '+metric(candidate.scalarCost)
      +' · 3D state 数 '+escapeHtml(String(candidate.stateCount))
      +' · expanded '+escapeHtml(String(candidate.expandedStates??'—'))+' · runtime '+metric(candidate.runtimeMs,'ms')
      +(candidate.altitudeRange?'<br>高度范围 '+metric(candidate.altitudeRange[0],'m')+' – '+metric(candidate.altitudeRange[1],'m'):'')
      +'<br>hard state rejection '+escapeHtml(String(candidate.hardSummary.total_state_rejections??0))
      +' · hard transition rejection '+escapeHtml(String(candidate.hardSummary.total_transition_rejections??0))
      +'<br>heuristic '+escapeHtml(candidate.heuristic.type||'—')+' · scale '+escapeHtml(String(candidate.heuristic.scale??'—'))
      +'<br><b>'+escapeHtml(candidate.disclaimer)+'</b></div>'
      +'<h3>Hard-constraint rejection 统计</h3><div class="flow-summary">state：'+escapeHtml(jsonInline(candidate.hardSummary.state_rejections||{}))
      +'<br>transition：'+escapeHtml(jsonInline(candidate.hardSummary.transition_rejections||{}))+'</div>'
      +'<h3>3D state（前 24 个，含高度/heading）</h3><div class="scroll-list route-list">'+(v3StateRows(model)||'<div class="empty-note">无 state</div>')+'</div>'
    :'<div class="empty-note">尚无 V3-A 战略候选。运行实验只会写入独立实验容器，不改变运行航路。</div>';
  const corridor=model.corridor
    ?'<div class="flow-summary">corridor semantics <code>'+escapeHtml(model.corridor.semantics)+'</code>'
      +'<br>N-ring '+escapeHtml(String(model.corridor.ringN))+' · center cells '+escapeHtml(String(model.corridor.centerCount))
      +' · support cells '+escapeHtml(String(model.corridor.supportCount))
      +' · refinement cell size '+(model.corridor.refinementCellSizeM===null?'未配置':metric(model.corridor.refinementCellSizeM,'m'))
      +'<br>高度包络 '+metric(model.corridor.altitudeEnvelope?.lower_altitude_egm2008_m,'m')+' – '+metric(model.corridor.altitudeEnvelope?.upper_altitude_egm2008_m,'m')
      +' （margin '+(model.corridor.altitudeEnvelope?.explicit_margin_m??'—')+'）'
      +'<br><b>N-ring 不是安全净空值</b>；corridor 只是下一阶段局部精化的搜索窗口。next stage：'+escapeHtml(model.corridor.nextStage||'—')+'</div>'
    :'<div class="empty-note">尚无 candidate corridor。</div>';
  const history=model.recent.length
    ?model.recent.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.experimentId)+'</b> '+statusBadge(item.status||'not_calculated')
      +'<small>'+escapeHtml(item.createdAt||'')+' · 3D distance '+metric(item.distanceM,'m')+' · state '+escapeHtml(String(item.stateCount??'—'))
      +' · expanded '+escapeHtml(String(item.expandedStates??'—'))+' · corridor support '+escapeHtml(String(item.corridorSupportCount??'—'))
      +' · refinement '+escapeHtml(String(item.refinementCount??0))+' ('+escapeHtml(item.refinementStatus||'—')+')</small></span></div>').join('')
    :'<div class="empty-note">尚无 V3-A 实验记录</div>';
  return '<h3>V3 战略规划实验 '+statusBadge(model.status)+'</h3>'
    +'<div class="parameter-note">'+escapeHtml(model.note)+' 记录数 '+escapeHtml(String(model.count))+'。'
    +'本面板为只读/实验面板：<b>不替换正式 V1/V2 运行航路</b>，结果状态只允许 '
    +escapeHtml(model.allowedStatuses.join(' / '))+'。</div>'
    +'<div class="flow-summary">'+escapeHtml(model.architecture)+'</div>'
    +'<h3>V3 readiness</h3><div class="parameter-note">terrain / building / policy / aircraft / cost-model 分别给出状态与原因；空域为 display-only / not applicable。'
    +'真实数据 adapter：'+escapeHtml(readiness.realData.status||'blocked')+' · '+escapeHtml(readiness.realData.adapter_status||'—')
    +'<br>'+escapeHtml(readiness.realData.reason||'')+'</div>'
    +'<div class="scroll-list route-list">'+v3ReadinessRows(readiness)+'</div>'
    +'<h3>显式安全参数（无默认值）</h3><div class="scroll-list route-list">'+v3ParameterRows(model)+'</div>'
    +'<h3>实验输入</h3>'
    +'<div class="form-grid"><label>环境来源<select id="v3EnvironmentSource"><option value="canonical_synthetic">canonical synthetic</option><option value="configured_real_sources">configured real sources</option></select></label>'
    +'<label>地形剖面<select id="v3TerrainProfile">'+(readiness.syntheticOptions.terrain_profiles||[]).map(name=>'<option value="'+escapeHtml(name)+'">'+escapeHtml(name)+'</option>').join('')+'</select></label></div>'
    +'<div class="form-grid"><label>地形基准高程 (m)<input class="panel-input" type="number" step="any" id="v3BaseElevation" value="0"></label>'
    +'<label>地形起伏/脊高 (m)<input class="panel-input" type="number" step="any" id="v3TerrainHeight" value="0"></label></div>'
    +'<div class="form-grid"><label>建筑剖面<select id="v3BuildingsProfile">'+(readiness.syntheticOptions.buildings_profiles||[]).map(name=>'<option value="'+escapeHtml(name)+'">'+escapeHtml(name)+'</option>').join('')+'</select></label>'
    +'<label>建筑数<input class="panel-input" type="number" min="0" id="v3BuildingCount" value="0"></label></div>'
    +'<label>建筑高度 (m)<input class="panel-input" type="number" step="any" id="v3BuildingHeight" value="0"></label>'
    +'<div class="form-grid"><label>corridor N-ring<input class="panel-input" type="number" min="0" id="v3CorridorRing" value="0"></label>'
    +'<label>refinement cell size (m，可空)<input class="panel-input" type="number" step="any" id="v3RefinementCellSize" placeholder="V3-B 参数"></label></div>'
    +'<div class="parameter-note">合成算例的 terrain/building 值全部来自这里的显式输入，不从任何文件或图层推断；V3-A 不做 30 m 细化，也不做 exact polygon/terrain 最终判定。</div>'
    +'<h3>Policy（显式安全参数）</h3>'
    +'<div class="form-grid"><label>最小高度 (m)<input class="panel-input" type="number" step="any" id="v3MinAltitude" value="'+escapeHtml(readiness.policy.min_altitude_egm2008_m??'')+'"></label>'
    +'<label>最大高度 (m)<input class="panel-input" type="number" step="any" id="v3MaxAltitude" value="'+escapeHtml(readiness.policy.max_altitude_egm2008_m??'')+'"></label>'
    +'<label>垂向步长 (m)<input class="panel-input" type="number" step="any" id="v3VerticalStep" value="'+escapeHtml(readiness.policy.vertical_step_m??'')+'"></label>'
    +'<label>地形净空 (m)<input class="panel-input" type="number" step="any" id="v3TerrainClearance" value="'+escapeHtml(readiness.policy.terrain_clearance_m??'')+'"></label>'
    +'<label>建筑水平净空 (m)<input class="panel-input" type="number" step="any" id="v3BuildingHorizontal" value="'+escapeHtml(readiness.policy.building_horizontal_clearance_m??'')+'"></label>'
    +'<label>建筑垂直净空 (m)<input class="panel-input" type="number" step="any" id="v3BuildingVertical" value="'+escapeHtml(readiness.policy.building_vertical_clearance_m??'')+'"></label>'
    +'<label>最小转弯半径 (m)<input class="panel-input" type="number" step="any" id="v3TurnRadius" value="'+escapeHtml(readiness.policy.aircraft_min_turn_radius_m??'')+'"></label>'
    +'<label>爬升梯度<input class="panel-input" type="number" step="any" id="v3ClimbGradient" value="'+escapeHtml(readiness.policy.max_climb_gradient??'')+'"></label>'
    +'<label>下降梯度<input class="panel-input" type="number" step="any" id="v3DescentGradient" value="'+escapeHtml(readiness.policy.max_descent_gradient??'')+'"></label>'
    +'<label>规划速度 (m/s)<input class="panel-input" type="number" step="any" id="v3PlanningSpeed" value="'+escapeHtml(readiness.policy.planning_speed_mps??'')+'"></label></div>'
    +'<label>参数来源<input class="panel-input" id="v3PolicySource" value="'+escapeHtml(readiness.policy.source||'')+'"></label>'
    +'<label class="check-row"><input type="checkbox" id="v3PolicyConfirmed" '+(readiness.policy.confirmed?'checked':'')+'>参数已由项目工程依据确认</label>'
    +'<div class="button-row"><button class="secondary" id="saveRoutePlannerV3Policy">保存 V3 policy</button>'
    +'<button class="primary" id="evaluateRoutePlannerV3" '+(readiness.status==='passed'?'':'disabled')+'>运行 V3 战略规划实验</button></div>'
    +'<div class="parameter-note">缺少任一必需安全参数时 readiness 为 blocked，实验拒绝运行；V3-A 禁止猜默认安全值。只有 rate 而没有 explicit planning_speed_mps 时不会换算梯度。</div>'
    +'<h3>V3-A 战略候选 '+statusBadge(candidate?.status||'not_calculated')+'</h3>'+candidateBlock
    +'<h3>Cost breakdown（vector，不是单一总数）</h3><div class="parameter-note">不同单位不直接相加；只有已明确 normalization 与非负 weight 的 component 才进入 scalar cost。'
    +'energy 默认 pending_model/disabled；CNS 记录为 '+escapeHtml(model.cnsIntegration.integration_mode||'post_route_assessment')
    +' 且 excluded_from_search_cost='+escapeHtml(String(model.cnsIntegration.excluded_from_search_cost===true))+'。</div>'
    +'<div class="scroll-list route-list">'+v3CostRows(model)+'</div>'
    +'<h3>Candidate refinement corridor</h3>'+corridor
    +'<div class="parameter-note">V3-B corridor-local 精化状态只允许 '+escapeHtml(model.allowedRefinementStatuses.join(' / '))
    +'：没有 validated/final 状态。'+escapeHtml(model.v3bNote||'')+'</div>'
    +v3bPanel(model)
    +v3cValidationPanel(flow)
    +v3dAdoptionPanel(flow)
    +'<h3>V3-A 实验记录</h3>'+(model.recent.length?'<div class="button-row"><button class="secondary" id="deleteRoutePlannerV3">删除当前实验</button></div>':'')
    +'<div class="scroll-list route-list">'+history+'</div>';
}

// V3-C is rendered from its own model so its status stays independent from V3-A/V3-B.
function v3cValidationPanel(flow){
  return routePlannerV3ValidationPanel(flow);
}

// ---- V3-D operational adoption + CNS assessment ---------------------------------------

function v3OperationalProjectionModel(projection){
  if(!projection)return null;
  const route=projection.route||{},profile=projection.profile||{};
  const waypoints=profile.waypoints||[];
  return {
    projectionId:projection.projection_id||null,
    projectionFingerprint:projection.projection_fingerprint||null,
    validationId:projection.validation_id||null,
    refinementId:projection.refinement_id||null,
    routeId:projection.route_id||null,
    status:projection.status||'not_ready',reason:projection.reason||null,
    routeKind:route.kind||null,routeStatus:route.status||null,
    vertexCount:(route.path||[]).length,
    pathIsTwoDimensional:(route.path||[]).every(point=>point.length===2),
    pathStart:(route.path||[])[0]||null,pathEnd:(route.path||[])[route.path.length-1]||null,
    provenance:route.provenance||{},
    profileMode:profile.mode||null,profileLocked:profile.locked===true,
    profileLockedByAdoption:profile.locked_by_adoption===true,
    profileStatus:profile.status||null,verticalReference:profile.vertical_reference||null,
    profileVertexCount:waypoints.length,
    profileStart:waypoints[0]||null,profileEnd:waypoints[waypoints.length-1]||null,
    pathMetrics:projection.path_metrics||{},
    compatibility:projection.compatibility||{},
    transform:projection.transform||{},
    horizontalCrs:projection.horizontal_crs||null,
    altitudeRepresentation:projection.altitude_representation||{},
    downstreamInvalidation:projection.downstream_invalidation||[]};
}

export function routePlannerV3AdoptionModel(flow){
  const publish=flow?.route_planner_v3_operational_publish||{};
  const adoptions=flow?.v3_operational_adoptions||{};
  const assessment=flow?.v3_cns_assessment||{};
  return {
    status:publish.status||'not_calculated',stage:publish.stage||'V3-D',
    modelScope:publish.model_scope||'',
    algorithm:publish.algorithm||{},
    options:(publish.options||[]).map(option=>({
      validationId:option.validation_id,routeId:option.route_id,refinementId:option.refinement_id,
      status:option.status,evidenceSource:option.evidence_source,eligible:option.eligible===true,
      reasons:option.reasons||[],productionEligible:option.production_eligible===true,
      productionReasons:option.production_reasons||[]})),
    blockingReasons:publish.blocking_reasons||[],
    boundaries:publish.boundaries||{},
    note:publish.note||V3D_NOTE_FALLBACK,
    adoptionStatus:adoptions.status||'not_calculated',
    adoptionCount:adoptions.count||0,currentCount:adoptions.current_count||0,
    staleCount:adoptions.stale_count||0,revokedCount:adoptions.revoked_count||0,
    adoptions:(adoptions.items||[]).map(item=>({
      adoptionId:item.adoption_id,routeId:item.route_id,status:item.status,
      currentApplicability:item.current_applicability,appliedAt:item.applied_at,
      evidenceSource:item.evidence_source,validationIds:item.validation_ids||[],
      validationFingerprints:item.validation_fingerprints||{},
      refinementFingerprint:item.refinement_fingerprint,
      projectionFingerprint:item.projection_fingerprint,
      routeProvenance:item.route_provenance||{},pathMetrics:item.path_metrics||{},
      compatibility:item.compatibility||{},before:item.before||{},after:item.after||{},
      cnsAssessment:item.cns_assessment||{},
      applicabilityReasons:item.applicability_reasons||[],ownership:item.ownership||{}})),
    assessmentStatus:assessment.status||'not_calculated',
    assessmentCount:assessment.count||0,
    assessments:(assessment.items||[]).map(item=>({
      bundleId:item.bundle_id,routeId:item.route_id,adoptionId:item.adoption_id,
      validationId:item.validation_id,assessmentStatus:item.assessment_status,
      requirementVerdict:item.requirement_verdict,blockingReasons:item.blocking_reasons||[],
      routeValidationStatus:item.route_validation_status,
      routeValidationUnchanged:item.route_validation_unchanged===true,
      stageResults:item.stage_results||{},requestedStages:item.requested_stages||[],
      computedAt:item.computed_at,assessmentFingerprint:item.assessment_fingerprint,
      semantics:item.semantics||{}})),
    stageOrder:assessment.stage_order||V3D_STAGES,
    separationLabel:V3D_CNS_SEPARATION_LABEL,
    publishLabel:V3D_PUBLISH_LABEL,
    syntheticLabel:V3D_SYNTHETIC_LABEL,
    neverFinalValidated:true};
}

function v3dRows(rows){
  return (rows||[]).map(row=>'<div class="list-row route-row"><span><b>'+escapeHtml(row[0])+'</b>'
    +'<small>'+row[1]+'</small></span></div>').join('');
}

function v3dStatusChain(model,validationModel){
  // The four-stage chain: strategic -> refined -> validated -> published.
  const chain=[
    ['V3-A strategic',model.status==='not_calculated'?'未运行':'见 V3-A 面板'],
    ['V3-B refined',model.status==='not_calculated'?'未运行':'见 V3-B 面板'],
    ['V3-C validated',validationModel?validationModel.status:'未运行'],
    ['V3-D published',model.adoptionCount?('已发布 '+model.adoptionCount+' 条'):'未发布']];
  return '<h3>状态链（V3-A → V3-B → V3-C → V3-D）</h3><div class="scroll-list route-list">'
    +v3dRows(chain.map(([name,value])=>[name,statusBadge(value)+' '+escapeHtml(String(value))]))+'</div>';
}

function v3dPreviewBlock(model,preview){
  if(!preview)return '';
  const rows=[
    ['preview_id / fingerprint',escapeHtml(preview.previewId||'—')+' · '+escapeHtml(String(preview.previewFingerprint||'—').slice(0,24))],
    ['status / publication_allowed',statusBadge(preview.status)+' · publication_allowed '+escapeHtml(String(preview.publicationAllowed))],
    ['evidence_source',escapeHtml(preview.evidenceSource||'—')+' · production '+escapeHtml(String(preview.productionPublication))
      +' · synthetic_test_only '+escapeHtml(String(preview.syntheticTestOnly))],
    ['route_ids',escapeHtml(jsonInline(preview.routeIds||[]))],
    ['downstream invalidation',escapeHtml(jsonInline(preview.downstreamInvalidation||[]))],
    ['operational_routes / spatial_3d / CNS',escapeHtml('untouched='+String(preview.operationalRoutesUntouched))
      +' / '+escapeHtml('untouched='+String(preview.spatial3dUntouched))+' / '+escapeHtml('not_run='+String(preview.cnsNotRun))]];
  const blocked=(preview.blocked||[]).length
    ?preview.blocked.map(item=>'<div class="list-row route-row"><span><b>'+escapeHtml(item.validationId||'—')
      +'</b>'+statusBadge('blocked')+'<small>route '+escapeHtml(item.routeId||'—')+'</small>'
      +'<small>reasons '+escapeHtml(jsonInline(item.reasons||[]))+'</small></span></div>').join('')
    :'<div class="empty-note">没有被拒绝的 validation</div>';
  return '<h3>V3-D Preview '+statusBadge(preview.status)+'（只读，不写入）</h3>'
    +(preview.syntheticTestOnly?'<div class="parameter-note"><b>'+escapeHtml(V3D_SYNTHETIC_LABEL)+'</b></div>':'')
    +'<div class="scroll-list route-list">'+v3dRows(rows)+'</div>'
    +'<div class="parameter-note">确认无误后再 Apply：Apply 必须显式 confirmed=true，并提交 expected_validation_fingerprint（TOCTOU 防护）。</div>'
    +'<h3>被拒绝的 validation（batch atomic：任一失败则全部不写）</h3><div class="scroll-list route-list">'+blocked+'</div>';
}

function v3dProjectionBlock(model,preview){
  const projections=(preview?.projections||[]);
  if(!projections.length)return '';
  const blocks=projections.map(projection=>{
    const p=v3OperationalProjectionModel(projection);
    const rows=[
      ['route',escapeHtml(p.routeId||'—')+' · kind '+escapeHtml(p.routeKind||'—')+' · status '+escapeHtml(p.routeStatus||'—')],
      ['path 顶点数 / 起点 / 终点',escapeHtml(String(p.vertexCount))+' · '+escapeHtml(jsonInline(p.pathStart||[]))+' → '+escapeHtml(jsonInline(p.pathEnd||[]))],
      ['path 仅二维 [lon, lat]',escapeHtml(String(p.pathIsTwoDimensional))+'（EGM2008 正高绝不写入第三坐标）'],
      ['高度表示',escapeHtml(p.verticalReference||'—')+' · carried_by '+escapeHtml(p.altitudeRepresentation.carried_by||'—')],
      ['profile mode / locked / status',escapeHtml(p.profileMode||'—')+' · locked '+escapeHtml(String(p.profileLocked))
        +'（by adoption '+escapeHtml(String(p.profileLockedByAdoption))+'） · '+escapeHtml(p.profileStatus||'—')],
      ['profile 顶点数 / 首 / 末',escapeHtml(String(p.profileVertexCount))+' · '+escapeHtml(jsonInline(p.profileStart||{}))+' → '+escapeHtml(jsonInline(p.profileEnd||{}))],
      ['v3_metric_length_m / legacy_geodesic_length_m',metric(p.pathMetrics.v3_metric_length_m,'m')+' / '+metric(p.pathMetrics.legacy_geodesic_length_m,'m')],
      ['length_delta_m / distance_basis',metric(p.pathMetrics.length_delta_m,'m')+' · '+escapeHtml(p.pathMetrics.distance_basis||'—')],
      ['curve_chord_error_m',metric(p.pathMetrics.curve_chord_error_m,'m')],
      ['transform',escapeHtml(p.transform.method||'—')+' · '+escapeHtml(p.transform.authority||'—')+' → '+escapeHtml(p.transform.target_crs||'—')],
      ['path/profile 顶点顺序与距离基准一致',escapeHtml(String(p.compatibility.path_and_profile_share_vertex_order))
        +' / '+escapeHtml(String(p.compatibility.path_and_profile_share_distance_basis))
        +' · simplification_applied '+escapeHtml(String(p.compatibility.simplification_applied))
        +' · crs_mixing '+escapeHtml(String(p.compatibility.crs_mixing))],
      ['provenance',escapeHtml(p.provenance.source_type||'—')+' · validation '+escapeHtml(p.provenance.validation_id||'—')
        +' · refinement fingerprint '+escapeHtml(String(p.refinementId||'—'))],
      ['projection fingerprint',escapeHtml(String(p.projectionFingerprint||'—').slice(0,28))]];
    return '<h4>'+escapeHtml(p.routeId||'—')+'</h4><div class="scroll-list route-list">'+v3dRows(rows)+'</div>';
  }).join('');
  return '<h3>待发布的 operational projection</h3>'
    +'<div class="parameter-note">'+escapeHtml(V3D_PUBLISH_LABEL)+'：path 只保存二维 [lon, lat]，'
    +'完整 analytic geometry 不复制进 operational_routes；高度由 locked profile 承载。</div>'+blocks;
}

function v3dAdoptionBlock(model){
  if(!model.adoptions.length)return '<h3>V3 operational adoptions</h3><div class="empty-note">尚无 V3 operational adoption</div>';
  const rows=model.adoptions.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(item.adoptionId||'—')+'</b> '+statusBadge(item.status||'published')
    +' · applicability '+escapeHtml(item.currentApplicability||'—')
    +'<small>route '+escapeHtml(item.routeId||'—')+' · applied '+escapeHtml(item.appliedAt||'—')
    +' · evidence_source '+escapeHtml(item.evidenceSource||'—')+'</small>'
    +'<small>validation '+escapeHtml(jsonInline(item.validationIds||[]))+' · refinement fingerprint '
    +escapeHtml(String(item.refinementFingerprint||'—').slice(0,20))+'</small>'
    +'<small>ownership route/profile/revocable '+escapeHtml(String((item.ownership||{}).route_owned))
    +' / '+escapeHtml(String((item.ownership||{}).profile_owned))+' / '+escapeHtml(String((item.ownership||{}).revocable))+'</small>'
    +'<small>before '+escapeHtml(jsonInline(item.before||{}))+'</small>'
    +'<small>after '+escapeHtml(jsonInline(item.after||{}))+'</small>'
    +(item.applicabilityReasons||[]).map(reason=>'<small>'+escapeHtml(reason)+'</small>').join('')
    +'<small>CNS '+escapeHtml(jsonInline(item.cnsAssessment||{}))+'</small></span></div>').join('');
  return '<h3>V3 operational adoptions '+statusBadge(model.adoptionStatus)+'</h3>'
    +'<div class="parameter-note">current '+escapeHtml(String(model.currentCount))+' · stale '+escapeHtml(String(model.staleCount))
    +' · revoked '+escapeHtml(String(model.revokedCount))+'。Revoke 只移除该 adoption 仍拥有的 route/profile，不误删其他 planner 的 route。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>';
}

export function routePlannerV3AdoptionPanel(flow,preview=null){
  const model=routePlannerV3AdoptionModel(flow);
  const validation=routePlannerV3ContinuousModel(flow).validationModel;
  const optionRows=model.options.length?model.options.map(option=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(option.validationId)+'</b> '+statusBadge(option.eligible?'ready':'blocked')
    +'<small>route '+escapeHtml(option.routeId||'—')+' · status '+escapeHtml(option.status||'—')
    +' · evidence_source '+escapeHtml(option.evidenceSource||'—')+'</small>'
    +'<small>production_eligible '+escapeHtml(String(option.productionEligible))
    +' · reasons '+escapeHtml(jsonInline(option.reasons||[]))+'</small>'
    +'<small>production reasons '+escapeHtml(jsonInline(option.productionReasons||[]))+'</small></span></div>').join('')
    :'<div class="empty-note">尚无 V3-C validation 记录</div>';
  const evidenceOptions=['configured_real_sources','canonical_synthetic']
    .map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('');
  return '<h3>V3-D 发布为运行分析航路（operational adoption）'+statusBadge(model.status)+'</h3>'
    +v3dStatusChain(model,validation)
    +'<div class="parameter-note">'+escapeHtml(model.note)+'</div>'
    +'<div class="parameter-note"><b>'+escapeHtml(V3D_CNS_SEPARATION_LABEL)+'</b></div>'
    +'<h3>可发布的 current V3-C validated route</h3><div class="scroll-list route-list">'+optionRows+'</div>'
    +'<div class="parameter-note"><b>blocking_reasons</b> '+escapeHtml(jsonInline(model.blockingReasons))
    +'<br>boundaries '+escapeHtml(jsonInline(model.boundaries))
    +'<br>production Apply 只接受 configured_real_sources；canonical_synthetic 只能 Preview。</div>'
    +'<h3>Preview / Apply</h3>'
    +'<div class="form-grid"><label>evidence_source<select id="v3dEvidenceSource">'+evidenceOptions+'</select></label>'
    +'<label>validation_ids（逗号分隔，可空=全部 eligible）<input class="panel-input" id="v3dValidationIds" placeholder="V3C-..."></label></div>'
    +'<div class="button-row"><button class="secondary" id="previewRoutePlannerV3Adoption">Preview（只读）</button>'
    +'<button class="primary" id="applyRoutePlannerV3Adoption">Apply（需显式确认）</button>'
    +'<button class="secondary" id="revokeRoutePlannerV3Adoption">Revoke（需显式确认）</button></div>'
    +'<label class="check-row"><input type="checkbox" id="v3dConfirmed">我已复核 Preview 内容并确认 Apply/Revoke</label>'
    +v3dPreviewBlock(model,preview)
    +v3dProjectionBlock(model,preview)
    +v3dAdoptionBlock(model);
}

// V3-D is rendered from its own model so its status stays independent from V3-A/B/C.
// The cached Preview response is display-only: Preview never writes project state.
function v3dAdoptionPanel(flow){
  return routePlannerV3AdoptionPanel(flow,v3dPreviewCache);
}

function optionalNumber(value){const text=String(value??'').trim();return text===''?null:Number(text);}

export function bindRoutePlannerV3(c){
  if(c.$('saveRoutePlannerV3Policy'))c.actionButton('saveRoutePlannerV3Policy',()=>c.resourceAction('/api/route-planner-v3/policy',{
    min_altitude_egm2008_m:optionalNumber(c.$('v3MinAltitude').value),
    max_altitude_egm2008_m:optionalNumber(c.$('v3MaxAltitude').value),
    vertical_step_m:optionalNumber(c.$('v3VerticalStep').value),
    terrain_clearance_m:optionalNumber(c.$('v3TerrainClearance').value),
    building_horizontal_clearance_m:optionalNumber(c.$('v3BuildingHorizontal').value),
    building_vertical_clearance_m:optionalNumber(c.$('v3BuildingVertical').value),
    aircraft_min_turn_radius_m:optionalNumber(c.$('v3TurnRadius').value),
    max_climb_gradient:optionalNumber(c.$('v3ClimbGradient').value),
    max_descent_gradient:optionalNumber(c.$('v3DescentGradient').value),
    planning_speed_mps:optionalNumber(c.$('v3PlanningSpeed').value),
    source:c.$('v3PolicySource').value.trim(),
    confirmed:c.$('v3PolicyConfirmed').checked}));
  if(c.$('evaluateRoutePlannerV3'))c.actionButton('evaluateRoutePlannerV3',async()=>{
    await c.resourceAction('/api/route-planner-v3-experiments/evaluate',{
      environment_source:c.$('v3EnvironmentSource').value,
      synthetic_spec:{profile_id:'ui_synthetic',terrain_profile:c.$('v3TerrainProfile').value,
        base_surface_elevation_m:optionalNumber(c.$('v3BaseElevation').value)??0,
        ridge_height_m:optionalNumber(c.$('v3TerrainHeight').value)??0,
        terrain_relative_amplitude_m:optionalNumber(c.$('v3TerrainHeight').value)??0,
        buildings_profile:c.$('v3BuildingsProfile').value,
        building_height_m:optionalNumber(c.$('v3BuildingHeight').value)??0,
        building_count:optionalNumber(c.$('v3BuildingCount').value)??0},
      corridor_ring_n:optionalNumber(c.$('v3CorridorRing').value)??0,
      refinement_cell_size_m:optionalNumber(c.$('v3RefinementCellSize').value)});
    if(c.loadRoutePlannerV3Detail)await c.loadRoutePlannerV3Detail();
  });
  if(c.$('saveRoutePlannerV3FinePolicy'))c.actionButton('saveRoutePlannerV3FinePolicy',()=>c.resourceAction('/api/route-planner-v3/fine-policy',{
    horizontal_crs:c.$('v3bFineCrs').value.trim()||null,
    resolution_source:c.$('v3bFineResolutionSource').value||null,
    resolution_m:optionalNumber(c.$('v3bFineResolution').value),
    max_stride_cells:optionalNumber(c.$('v3bFineMaxStride').value),
    source:c.$('v3bFineSource').value.trim(),
    confirmed:c.$('v3bFineConfirmed').checked}));
  if(c.$('evaluateRoutePlannerV3Refinement'))c.actionButton('evaluateRoutePlannerV3Refinement',async()=>{
    // The fine resolution / stride are explicit inputs: they are forwarded both as the
    // direct request fields and inside the synthetic fine spec (never a hidden 30 m).
    const cellSize=optionalNumber(c.$('v3bRefinementCellSize').value);
    const stride=optionalNumber(c.$('v3bMaxStride').value);
    await c.resourceAction('/api/route-planner-v3-refinements/evaluate',{
      environment_source:c.$('v3bEnvironmentSource').value,
      refinement_cell_size_m:cellSize,
      max_stride_cells:stride,
      synthetic_fine_spec:{profile_id:'ui_synthetic_fine',
        base_surface_elevation_m:optionalNumber(c.$('v3bSyntheticBaseElevation').value)??0,
        resolution_m:cellSize,
        max_stride_cells:stride??1}});
    if(c.loadRoutePlannerV3Detail)await c.loadRoutePlannerV3Detail();
  });
  if(c.$('deleteRoutePlannerV3'))c.actionButton('deleteRoutePlannerV3',async()=>{
    const model=routePlannerV3Model(c.flow());
    if(!model.activeId)throw new Error('没有可删除的 V3 实验');
    await c.resourceAction('/api/route-planner-v3-experiments/delete',{experiment_id:model.activeId});
  });
  // ---- V3-C -------------------------------------------------------------------
  if(c.$('saveRoutePlannerV3ValidationPolicy'))c.actionButton('saveRoutePlannerV3ValidationPolicy',()=>c.resourceAction('/api/route-planner-v3/validation-policy',{
    curve_chord_error_m:optionalNumber(c.$('v3cChordError').value),
    max_validation_samples:optionalNumber(c.$('v3cMaxSamples').value),
    max_runtime_s:optionalNumber(c.$('v3cMaxRuntime').value),
    use_curve_error_envelope:c.$('v3cEnvelope').value==='true',
    source:c.$('v3cPolicySource').value.trim(),
    confirmed:c.$('v3cPolicyConfirmed').checked}));
  if(c.$('evaluateRoutePlannerV3Validation'))c.actionButton('evaluateRoutePlannerV3Validation',async()=>{
    await c.resourceAction('/api/route-planner-v3-validations/evaluate',{
      evidence_source:c.$('v3cEvidenceSource').value});
    if(c.loadRoutePlannerV3Detail)await c.loadRoutePlannerV3Detail();
  });
  // ---- V3-D -------------------------------------------------------------------
  if(c.$('previewRoutePlannerV3Adoption'))c.actionButton('previewRoutePlannerV3Adoption',async()=>{
    const payload=v3dPayload(c);
    // Preview writes nothing; the response is cached only to show the projections.
    const result=await c.resourceAction('/api/route-planner-v3-operational-adoptions/preview',payload);
    v3dPreviewCache=result?.data??result??null;
  });
  if(c.$('applyRoutePlannerV3Adoption'))c.actionButton('applyRoutePlannerV3Adoption',async()=>{
    if(!c.$('v3dConfirmed').checked)throw new Error('Apply 需要显式确认：请先复核 Preview 并勾选确认');
    const payload=v3dPayload(c);
    payload.confirmed=true;
    // TOCTOU guard: send the fingerprint observed at Preview time so a changed
    // validation is rejected instead of silently published.
    const fingerprint=v3dExpectedFingerprint(c.flow(),payload.validation_ids);
    if(fingerprint)payload.expected_validation_fingerprint=fingerprint;
    await c.resourceAction('/api/route-planner-v3-operational-adoptions/apply',payload);
    v3dPreviewCache=null;
    if(c.loadRoutePlannerV3Detail)await c.loadRoutePlannerV3Detail();
  });
  if(c.$('revokeRoutePlannerV3Adoption'))c.actionButton('revokeRoutePlannerV3Adoption',async()=>{
    if(!c.$('v3dConfirmed').checked)throw new Error('Revoke 需要显式确认');
    const model=routePlannerV3AdoptionModel(c.flow());
    const target=model.adoptions.find(item=>item.status!=='revoked');
    if(!target)throw new Error('没有可撤销的 V3 operational adoption');
    await c.resourceAction('/api/route-planner-v3-operational-adoptions/revoke',{
      confirmed:true,adoption_id:target.adoptionId});
    if(c.loadRoutePlannerV3Detail)await c.loadRoutePlannerV3Detail();
  });
}

//: Last Preview response, kept only for display (it is never persisted).
let v3dPreviewCache=null;

export function v3dPreviewModel(){return v3dPreviewCache;}

//: The validation fingerprint currently stored for the selected validation ids.
export function v3dExpectedFingerprint(flow,validationIds){
  const detail=flow?.route_planner_v3_detail||{};
  const wanted=new Set(validationIds||[]);
  for(const record of detail.records||[]){
    for(const refinement of record.refinements||[]){
      for(const validation of refinement.validations||[]){
        const id=String(validation.validation_id||'');
        const result=validation.result||{};
        if(wanted.size&&!wanted.has(id))continue;
        const fingerprint=result.validation_fingerprint||validation.fingerprint;
        if(fingerprint)return String(fingerprint);
      }
    }
  }
  return null;
}

function v3dPayload(c){
  const raw=String(c.$('v3dValidationIds')?.value||'').trim();
  const validation_ids=raw?raw.split(',').map(value=>value.trim()).filter(Boolean):[];
  return {evidence_source:c.$('v3dEvidenceSource')?.value||'configured_real_sources',validation_ids};
}

function experimentPanelV3(flow){
  return routePlannerV3Panel(flow);
}

function experimentPanel(flow){
  const model=routeExperimentModel(flow);
  const active=model.active;
  const header='<h3>规划器比较实验 '+statusBadge(active?'passed':'not_calculated')+'</h3>'
    +'<div class="parameter-note"><b>experiment ≠ current operational route</b>：实验记录独立保存在 <code>route_planning_experiments</code>，'
    +'运行比较<b>不切换当前 planner</b>，也<b>不覆盖 operational_routes</b>。当前正式运行航路 '+model.operationalRouteCount+' 条，实验记录 '+model.count+' 条。</div>';
  const body=active
    ?'<div class="flow-summary">experiment <code>'+escapeHtml(active.experiment_id)+'</code> · '+escapeHtml(active.created_at||'')+' · grounding '+escapeHtml(active.grounding)+' · 输入适用性 '+escapeHtml(active.current_applicability)+'<br>scenario changed '+escapeHtml(String(active.scenario_inputs_changed))+' · context changed '+escapeHtml(String(active.context_inputs_changed))+'<br>scenario fingerprint <code>'+escapeHtml(String(active.scenario_fingerprint||'').slice(0,16))+'</code> · context fingerprint <code>'+escapeHtml(String(active.planner_context_fingerprint||'').slice(0,16))+'</code></div>'
      +'<div class="scroll-list route-list">'+(model.runs.map(experimentRunBlock).join('')||'<div class="empty-note">实验没有 run 记录</div>')+'</div>'
    :'<div class="empty-note">尚无实验记录。运行实验只会写入实验集合，不会改变当前运行航路。</div>';
  const controls='<div class="button-row"><button class="secondary" id="evaluateRouteExperiment" '+((flow.scenario_routes||[]).length?'':'disabled')+'>运行 V1 + V2 比较实验</button>'
    +(active?'<button class="secondary" id="deleteRouteExperiment">删除该实验</button>':'')+'</div>'
    +'<div class="parameter-note">实验只做事实并列：不排名、不评分、不推荐算法；runtime 统计仅用于报告。</div>';
  return header+body+controls;
}

// ---- reference route ↔ OD explicit link --------------------------------------------

export function referenceLinkModel(flow){
  const links=(flow?.reference_route_links?.items)||[],referenceRoutes=(flow?.reference_routes?.items)||[],scenarios=flow?.scenario_routes||[];
  const candidates=flow?.reference_endpoint_candidates||{};
  return {links:links.map(item=>({link_id:item.link_id,reference_route_id:item.reference_route_id,
      scenario_route_id:item.scenario_route_id,confirmed:item.confirmed===true,origin:item.origin})),
    linkCount:links.length,referenceRouteCount:referenceRoutes.length,scenarioCount:scenarios.length,
    candidateStatus:candidates.status||'not_calculated',candidateReason:candidates.reason||null,
    candidateCount:candidates.candidate_count||0,candidates:candidates.candidates||[],
    requiresUserConfirmation:true,automaticAssociation:false,
    referenceSourceCrsResolved:flow?.data_readiness?.blocks?.reference_routes?.source_crs_resolved===true};
}

function referenceLinkPanel(flow){
  const model=referenceLinkModel(flow);
  const disabled=model.referenceRouteCount&&model.scenarioCount?'':'disabled';
  const linkRows=model.links.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.reference_route_id)+'</b><small>↔ '+escapeHtml(item.scenario_route_id)+' · confirmed '+escapeHtml(String(item.confirmed))+' · origin '+escapeHtml(item.origin||'user')+'</small></span><button data-delete-reference-link="'+escapeHtml(item.link_id)+'">×</button></div>').join('');
  const candidateRows=model.candidates.slice(0,10).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.reference_route_id)+'</b><small>↔ '+escapeHtml(item.scenario_route_id)+' · 起点偏移 '+metric(item.start_offset_m,'m')+' · 终点偏移 '+metric(item.end_offset_m,'m')+' · state '+escapeHtml(item.state)+'</small></span><button class="secondary" data-confirm-reference-link="'+escapeHtml(item.reference_route_id)+'|'+escapeHtml(item.scenario_route_id)+'">确认关联</button></div>').join('');
  const candidateBlock=model.candidateStatus==='passed'
    ?'<div class="scroll-list route-list">'+(candidateRows||'<div class="empty-note">没有未关联的候选对</div>')+'</div>'
    :'<div class="parameter-note">候选提示不可用：<code>'+escapeHtml(model.candidateReason||model.candidateStatus)+'</code>。端点距离候选需要 reference source CRS 已确认；系统不会自动关联。</div>';
  return '<h3>参考航线 ↔ 当前 OD 关联 '+statusBadge(model.linkCount?'passed':'not_calculated')+'</h3>'
    +'<div class="parameter-note">关联必须由用户显式确认。系统只在 source CRS 已确认后给出端点距离候选，<b>不得自动认定</b>；'
    +'reference source CRS 已确认：'+escapeHtml(String(model.referenceSourceCrsResolved))+'。</div>'
    +'<div class="form-grid"><label>参考航线<select id="linkReferenceRoute">'+referenceRouteOptions((flow.reference_routes||{}).items)+'</select></label><label>场景航路<select id="linkScenarioRoute">'+scenarioOptions(flow.scenario_routes)+'</select></label></div>'
    +'<button class="secondary full" id="createReferenceLink" '+disabled+'>确认关联</button>'
    +'<h3>已确认关联</h3><div class="scroll-list route-list">'+(linkRows||'<div class="empty-note">尚无已确认关联</div>')+'</div>'
    +'<h3>候选提示（仅提示，需确认）</h3>'+candidateBlock;
}

// ---- data readiness (airspace is a display-only reference layer: no editor) ------

function readinessBlockRows(block){
  const crs=block||{};
  const sourceCrs=(crs.source_crs||{}).value||'—',representation=(crs.representation_crs||{}).value||'—';
  return '<div class="list-row"><span><b>'+escapeHtml(block.label||'')+'</b><small>status '+escapeHtml(crs.status||'not_calculated')+' · count '+escapeHtml(String(crs.count??0))+' · format '+escapeHtml(crs.format||'—')+'</small><small>source_crs '+escapeHtml(sourceCrs)+' ('+escapeHtml(String(crs.source_crs_resolved)) +') · representation_crs '+escapeHtml(representation)+' · 米制度量 '+escapeHtml(crs.metric_measurement_status||'—')+'</small></div>';
}

function dataReadinessPanel(flow){
  const readiness=flow?.data_readiness||{},blocks=readiness.blocks||{};
  const list=['reference_landing_sites','reference_routes'].map(name=>blocks[name]?readinessBlockRows(blocks[name]):'').join('');
  return '<h3>数据就绪 '+statusBadge(readiness.status||'not_calculated')+'</h3>'
    +'<div class="parameter-note">只读汇总：参考起降点 CRS、参考航线 CRS/格式/数量。空域仅为显示图层，不构成 readiness 阻断。ET 仍要求人工转换为 XLSX/CSV。</div>'
    +'<div class="scroll-list route-list">'+(list||'<div class="empty-note">尚无就绪信息</div>')+'</div>'
    +'<div class="flow-summary">reference route link '+escapeHtml(String(readiness.reference_route_link_count??0))+' · experiment '+escapeHtml(String(readiness.experiment_count??0))+' · ET 政策 '+escapeHtml(readiness.et_source_policy||'requires_xlsx_or_csv_conversion')+'</div>';
}

function odScenarioPanel(flow){
  const nodes=flow.nodes||[];
  const disabled=nodes.length<2?'disabled':'';
  const first=nodes[0]?.node_id,last=nodes[nodes.length-1]?.node_id;
  return '<h3>起点 → 终点 创建航路</h3><div class="parameter-note">显式指定两个 node，只创建这一条（或这一对）场景航路，不会因为参考点数量自动生成全连接。创建后会替换当前场景航路并清空运行航路，需要重新生成运行航路。</div>'
    +'<div class="form-grid"><label>起点<select id="odStartNode">'+endpointOptions(nodes,first)+'</select></label><label>终点<select id="odEndNode">'+endpointOptions(nodes,last)+'</select></label></div>'
    +'<label>方向<select id="odDirection"><option value="ab">仅 起点→终点</option><option value="ba">仅 终点→起点</option><option value="both" selected>双向（两个 route_id）</option></select></label>'
    +'<button class="primary full" id="createOdRoute" '+disabled+'>创建航路</button>'
    +'<div class="parameter-note">兼容说明：下方“生成场景航路”保留原 all-pairs 行为，供旧项目继续使用；新项目优先使用本面板。</div>';
}

function selectedReferencePanel(flow,selected){
  if(!selected)return '<div class="empty-note">点击地图或下方列表中的真实航线/航路点查看来源事实。</div>';
  const catalog=flow.reference_routes||{};
  const item=selected.kind==='route'
    ?(catalog.items||[]).find(value=>value.reference_route_id===selected.id)
    :(catalog.points||[]).find(value=>value.reference_route_point_id===selected.id);
  if(!item)return '<div class="empty-note">所选参考对象当前不可用</div>';
  const source=item.source||{},coordinate=Array.isArray(item.coordinate)?item.coordinate.map(value=>Number(value).toFixed(6)).join(', '):'—';
  return '<div class="reference-detail"><b>'+escapeHtml(item.name||item.route_number||selected.id)+'</b><small>分类 '+escapeHtml(item.category||item.type||'未注明')+' · 航线编号 '+escapeHtml(item.route_number||'—')+(selected.kind==='point'?' · 点序 '+escapeHtml(item.sequence):'')+'</small><small>坐标 '+escapeHtml(coordinate)+' · CRS '+escapeHtml(item.crs_status||'pending_confirmation')+'</small><small>source '+escapeHtml(source.file||'未记录')+(source.sheet?' / '+escapeHtml(source.sheet):'')+(source.row?' / row '+escapeHtml(source.row):'')+'</small></div>';
}

function referenceRoutesPanel(flow,selected){
  const catalog=flow.reference_routes||{},routes=catalog.items||[],points=catalog.points||[];
  const preview=flow.reference_route_import_preview||null;
  const previewHtml=preview?'<div class="flow-summary"><b>导入预览</b> '+escapeHtml(preview.status||'')+' · routes '+escapeHtml(String(preview.route_count??0))+' · points '+escapeHtml(String(preview.point_count??0))+' · valid/invalid '+escapeHtml(String(preview.valid_coordinate_count??0))+'/'+escapeHtml(String(preview.invalid_coordinate_count??0))+' · sequence gaps '+escapeHtml(String((preview.sequence_gaps||[]).length))+' · duplicate conflicts '+escapeHtml(String((preview.duplicate_route_numbers||[]).length))+'<br>columns '+escapeHtml((preview.columns||[]).join(', '))+'<br>warnings '+escapeHtml((preview.warnings||[]).join(', ')||'无')+(preview.preview_id&&preview.status==='ready_for_confirmation'?'<br><button class="primary compact" id="confirmReferenceRouteImport" data-preview-id="'+escapeHtml(preview.preview_id)+'">确认替换 reference_routes</button>':'')+'</div>':'';
  const rows=routes.map(route=>'<button class="list-row reference-route-row" data-select-reference-route="'+escapeHtml(route.reference_route_id)+'"><span><b>'+escapeHtml(route.name||route.reference_route_id)+'</b><small>航线编号 '+escapeHtml(route.route_number)+' · '+escapeHtml(route.category||'分类未注明')+' · '+(route.ordered_points||[]).length+' 点 · '+metric(route.length_m,'m')+'</small></span></button>').join('');
  return '<h3>真实参考航线 '+statusBadge(catalog.status||'not_calculated')+'</h3><div class="parameter-note">reference_routes / route points 为只读参考层，不会写入 flow.nodes、scenario_routes 或 operational_routes。全部中间点均按 sequence 保留；CRS 为 pending_confirmation 时仅按源数值临时显示。</div>'+previewHtml+selectedReferencePanel(flow,selected)+'<div class="flow-summary">航线 '+(catalog.count||0)+' 条 · 航路点 '+(catalog.point_count||points.length)+' 个</div><div class="scroll-list">'+(rows||'<div class="empty-note">尚无已转换 CSV/XLSX/GeoJSON 参考航线；ET 需先转换。</div>')+'</div>';
}

function comparisonPanel(flow,selected){
  const references=flow.reference_routes?.items||[],operational=(flow.operational_routes||[]).filter(item=>item.status==='passed');
  const reference=(selected?.kind==='route'&&references.find(item=>item.reference_route_id===selected.id))||references[0];
  const planned=operational[0];
  return '<div class="route-comparison"><b>真实参考航线 vs 系统规划运行航线</b><br>参考航线：'+(reference?escapeHtml(reference.name)+' · '+metric(reference.length_m,'m'):'不存在')+'<br>运行航线：'+(planned?escapeHtml(planned.route_id)+' · '+metric(planned.distance_m??pathLengthM(planned.path),'m'):'不存在')+'<br>两者是否都存在：'+(reference&&planned?'是':'否')+'（仅并列展示，不作优劣评分）</div>';
}

function referenceLandingPanel(flow){
  const catalog=flow.reference_landing_sites||{},items=filterReferenceSites(catalog.items||[],{workspace:flow.workspace});
  const regions=[...new Set(items.map(item=>item.region).filter(Boolean))].sort(),types=[...new Set(items.map(item=>item.site_type).filter(Boolean))].sort();
  const added=new Set((flow.nodes||[]).map(item=>item.reference_site_id).filter(Boolean));
  const rows=items.map(item=>'<div class="list-row reference-site-row" data-reference-site data-search="'+escapeHtml([item.name,item.location,item.reference_site_id].join(' ').toLocaleLowerCase())+'" data-region="'+escapeHtml(item.region||'')+'" data-site-type="'+escapeHtml(item.site_type||'')+'"><span><b>'+escapeHtml(item.name)+'</b><small>'+escapeHtml(item.region||'未标地区')+' · '+escapeHtml(item.site_type||'类型未标')+' · '+escapeHtml(item.quality)+' · '+item.coordinate.map(value=>Number(value).toFixed(5)).join(', ')+'</small><small>'+escapeHtml(item.reference_site_id)+(item.possible_duplicate?' · 疑似重复':'')+'</small></span><button class="secondary" data-add-reference-site="'+escapeHtml(item.reference_site_id)+'" '+(added.has(item.reference_site_id)?'disabled':'')+'>'+(added.has(item.reference_site_id)?'已加入':'加入项目')+'</button></div>').join('');
  return '<h3>参考起降点 '+statusBadge(catalog.status||'not_calculated')+'</h3><div class="parameter-note">reference_landing_sites 与 flow.nodes 严格分离。源文件未声明 CRS，全部保持 pending_confirmation；地图位置仅按源数值 [lon,lat] 临时展示，点击“加入项目”后才创建 node。</div><label>搜索<input class="panel-input" id="referenceSiteSearch" placeholder="名称、位置或稳定 ID"></label><div class="form-grid"><label>区域<select id="referenceSiteRegion"><option value="">全部区域</option>'+regions.map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('')+'</select></label><label>类型<select id="referenceSiteType"><option value="">全部类型</option>'+types.map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('')+'</select></label></div><div class="flow-summary" id="referenceSiteCount">工作区内 '+items.length+' / 全部 '+(catalog.count||0)+' 条；疑似重复只标记、不合并。</div><div class="scroll-list reference-site-list">'+(rows||'<div class="empty-note">当前工作区没有可展示的参考起降点</div>')+'</div>';
}

export function riskAwareRoutePanel(flow){
  const selection=flow.algorithm_selection?.route_planner||{};
  if(selection.algorithm_id!=='risk_aware_route_planner_v2'||selection.version!=='2.0')return '';
  const p=selection.parameters||{},lambda=p.risk_weight_lambda??0,component=p.risk_component||'overall',policy=p.unknown_risk_policy||'block';
  return '<h3>Risk-Aware Route Planner V2</h3><div class="parameter-note">直接使用 MH/T grid_id 与 RiskModelV1 相对工程指数；不是事故概率、SORA GRC 或 TLS。P13 仅规划二维战略水平航路，高度剖面仍由 P7 独立配置。</div><label>Risk weight λ<input class="panel-input" type="number" min="0" step="any" id="routeRiskLambda" value="'+escapeHtml(lambda)+'"></label><label>Risk component<select id="routeRiskComponent"><option value="overall" '+(component==='overall'?'selected':'')+'>Overall</option><option value="ground" '+(component==='ground'?'selected':'')+'>Ground</option><option value="air" '+(component==='air'?'selected':'')+'>Air</option></select></label><label>Unknown risk policy<select id="routeUnknownPolicy"><option value="block" '+(policy==='block'?'selected':'')+'>Block（默认）</option><option value="penalize" '+(policy==='penalize'?'selected':'')+'>Penalize</option></select></label><label>Unknown penalty index（penalize 时必须显式填写）<input class="panel-input" type="number" min="0" max="1" step="any" id="routeUnknownPenalty" value="'+escapeHtml(p.unknown_penalty_index??'')+'"></label><label>Max relative risk index（可空；仅工程阈值）<input class="panel-input" type="number" min="0" max="1" step="any" id="routeMaxRisk" value="'+escapeHtml(p.max_relative_risk_index??'')+'"></label><button class="secondary full" id="saveRiskRouteParameters">保存 V2 参数</button>';
}

function buildingClearancePanel(flow){
  const policy=flow.building_clearance_policy||{},result=flow.building_clearance_assessment||{},stats=result.statistics||{};
  const critical=(result.critical_buildings||[]).slice(0,12).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.building_id||'unknown')+'</b><small>水平 '+metric(item.horizontal_minimum_m,'m')+' · 地面/屋顶 '+metric(item.ground_elevation_m,'m')+' / '+metric(item.roof_elevation_m,'m')+' · '+escapeHtml(item.vertical_status||'unknown')+'</small></span></div>').join('');
  const unresolved=(result.routes||[]).map(item=>item.unresolved_building_count||0).reduce((a,b)=>a+b,0);
  return '<h3>三维建筑净空 '+statusBadge(result.status||'not_calculated')+'</h3><div class="parameter-note">FABDEM DTM footprint median + GBA height_m 构成 LoD1 棱柱；GLO-30 DSM 不参与屋顶高程。建筑环境风险与本评估彼此独立。</div><div class="form-grid"><label>水平净空 (m)<input class="panel-input" type="number" min="0" step="any" id="buildingHorizontalClearance" value="'+escapeHtml(policy.horizontal_clearance_m??'')+'"></label><label>垂直净空 (m)<input class="panel-input" type="number" min="0" step="any" id="buildingVerticalClearance" value="'+escapeHtml(policy.vertical_clearance_m??'')+'"></label><label>最小建筑高度 (m，可空)<input class="panel-input" type="number" min="0" step="any" id="buildingMinHeight" value="'+escapeHtml(policy.min_building_height_m??'')+'"></label><label>地形起伏复核阈值 (m，可空)<input class="panel-input" type="number" min="0" step="any" id="buildingReliefReview" value="'+escapeHtml(policy.terrain_relief_review_m??'')+'"></label></div><label>工程参数来源<input class="panel-input" id="buildingClearanceSource" value="'+escapeHtml(policy.source||'')+'"></label><label class="check-row"><input type="checkbox" id="buildingClearanceConfirmed" '+(policy.confirmed?'checked':'')+'>参数已由工程依据确认</label><div class="button-row"><button class="secondary" id="saveBuildingClearancePolicy">保存参数</button><button class="primary" id="evaluateBuildingClearance">执行净空分析</button></div><div class="flow-summary">breach '+(stats.breach_count||0)+' · safe routes '+(stats.safe_route_count||0)+' · unknown routes '+(stats.unknown_route_count||0)+' · unresolved buildings '+unresolved+'<br>unknown/unresolved 永远不视为 safe；本结果不构成认证或法规符合性结论。</div><h3>Closest / critical buildings</h3><div class="scroll-list">'+(critical||'<div class="empty-note">尚无评估证据</div>')+'</div>';
}

export function render({flow,interactionMode,selectedReference=null}){
  const nodes=(flow.nodes||[]).map(node=>'<div class="list-row"><span><b>'+node.node_id+'</b> '+escapeHtml(node.name)+'<small>'+node.coordinate.map(value=>value.toFixed(5)).join(', ')+(node.reference_site_id?' · 来源 '+escapeHtml(node.reference_site_id):' · 手工点')+'</small></span><button data-delete-node="'+node.node_id+'">×</button></div>').join('');
  const routes=(flow.scenario_routes||[]).map(route=>{const result=(flow.operational_routes||[]).find(item=>item.route_id===route.route_id),v2=result?.algorithm_id==='risk_aware_route_planner_v2',details=v2?'<small>Distance '+metric(result.distance_m,'m')+' · Risk exposure '+metric(result.risk_exposure_index_m,'index·m')+' · Mean '+metric(result.mean_risk_index)+' · Max '+metric(result.max_risk_index)+' · Detour '+metric(result.detour_factor)+'</small>':'';return '<div class="list-row route-row"><span><b>'+route.route_id+'</b> '+route.direction+' '+statusBadge(result?.status||'not_calculated')+details+'</span><button data-delete-route="'+route.route_id+'">×</button></div>';}).join('');
  const routeOptions=(flow.operational_routes||[]).map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  const profiles=Object.values(flow.spatial_3d?.route_altitude_profiles||{}).map(item=>{const locked=item.locked_by_adoption===true||item.locked===true;return '<div class="list-row"><span><b>'+escapeHtml(item.route_id)+'</b><small>'+escapeHtml(item.mode)+' · '+(item.constant_altitude_m===null||item.constant_altitude_m===undefined?'无 constant 值':escapeHtml(String(item.constant_altitude_m))+' m')+' '+escapeHtml(item.vertical_reference)+' · '+escapeHtml(item.source||'')+(item.derived?' · derived':'')+'</small>'+(locked?'<small>V3-D locked · advanced_variable_profile / v3c_validated_route（只读）</small>':'')+'</span></div>';}).join('');
  // The naked "Constant altitude" production entry is gone: the production cruise layer is
  // chosen in the 巡航高度层 panel.  This panel stays as the independent advanced/V3 profile
  // surface and never feeds the production cruise layer automatically.
  const altitude='<h3>高级/实验：Route 3D Altitude Profile</h3><div class="parameter-note">'+escapeHtml(ADVANCED_PROFILE_LABEL)+'。此处保存的 constant / waypoint 剖面不会创建或改写生产巡航高度层，也不会被自动匹配到任何 AltitudeLayer；V3-D validated route 导出的 locked profile 保持只读。系统不提供默认真实高度。</div><div class="panel-file-input"><select id="altitudeRoute">'+routeOptions+'</select><select id="routeVerticalReference"><option value="">请选择垂向基准（不猜）</option><option value="egm2008_orthometric">EGM2008 orthometric</option><option value="agl">AGL</option><option value="wgs84_ellipsoidal">WGS84 ellipsoidal</option></select></div><label>Constant altitude (m)<input class="panel-input" type="number" step="any" id="routeAltitude" placeholder="必须显式输入，无默认值"></label><button class="secondary full" id="saveRouteAltitude" '+(!routeOptions?'disabled':'')+'>保存高级高度剖面</button><div class="scroll-list">'+(profiles||'<div class="empty-note">尚未配置高级高度剖面</div>')+'</div>';
  const motionProfiles=Object.values(flow.operational_timing?.route_motion_profiles||{}).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.route_id)+'</b><small>'+escapeHtml(item.mode)+' · '+(item.constant_ground_speed_mps??'待确认')+' m/s · '+escapeHtml(item.status)+'</small></span></div>').join('');
  const motion='<h3>Route Motion Profile</h3><div class="demo-note">P9 仅实现 confirmed constant ground speed；不会借用 Aircraft cruise speed。</div><label>运行航路<select id="motionRoute">'+routeOptions+'</select></label><label>Constant ground speed (m/s)<input class="panel-input" type="number" min="0" step="any" id="routeGroundSpeed" placeholder="必须显式输入"></label><button class="secondary full" id="saveRouteMotion" '+(!routeOptions?'disabled':'')+'>保存航路运动剖面</button><div class="scroll-list">'+(motionProfiles||'<div class="empty-note">尚未配置航路运动剖面</div>')+'</div>';
  const body=referenceRoutesPanel(flow,selectedReference)+referenceLandingPanel(flow)+dataReadinessPanel(flow)+'<h3>项目起降点</h3><button class="'+(interactionMode==='node'?'primary':'secondary')+' full" id="addNodeMode">地图点击增加起降点</button><div class="scroll-list">'+(nodes||'<div class="empty-note">至少添加两个点</div>')+'</div>'+odScenarioPanel(flow)+'<h3>旧：生成方向</h3><label>生成方向</label><select id="routeDirection"><option value="both">双向（独立生成两个 route_id）</option><option value="ab">A→B</option><option value="ba">B→A</option></select>'+plannerCard(plannerCardModel(flow))+riskAwareRoutePanel(flow)+'<div class="button-row"><button class="secondary" id="scenarioRoutes">生成场景航路（all-pairs，兼容）</button><button class="primary" id="operationalRoutes">生成运行航路</button></div><div class="scroll-list route-list">'+(routes||'<div class="empty-note">尚无航路</div>')+'</div>'+renderCruiseLayerPanel(flow)+experimentPanelV3(flow)+experimentPanel(flow)+routePlanningDiagnosticsPanel(flow)+comparisonPanelV2(flow,routePlannerComparisonModel(flow))+referenceLinkPanel(flow)+comparisonPanel(flow,selectedReference)+altitude+renderRouteVerticalProfilePanel(flow.route_vertical_profiles,flow.operational_routes)+motion+buildingClearancePanel(flow)+'<div class="flow-summary">已退役编号：'+((flow.retired_route_ids||[]).join(', ')||'无')+'<br>环境风险：'+statusText(flow.risks?.environment?.status||'not_calculated')+'</div><button class="primary full" id="nextStep" '+(!flow.steps?.['3']?'disabled':'')+'>下一步：运行规则</button>';
  return shell('03','航路设计','地图点击增加起降点；场景与运行航路分别保存。',body);
}
export function bind(c){
  bindRouteVerticalProfile(c);
  bindRoutePlannerV3(c);
  bindCruiseLayer(c);
  c.$('addNodeMode').onclick=c.toggleNodeMode;c.actionButton('scenarioRoutes',()=>c.mutate('scenario',{direction:c.$('routeDirection').value}));c.actionButton('operationalRoutes',()=>c.mutate('operational'));
  if(c.$('createOdRoute'))c.actionButton('createOdRoute',()=>{const start=c.$('odStartNode').value,end=c.$('odEndNode').value;if(start===end)throw new Error('起点与终点不能相同');return c.mutate('scenario-od',{start_node_id:start,end_node_id:end,direction:c.$('odDirection').value});});
  if(c.$('evaluateRouteExperiment'))c.actionButton('evaluateRouteExperiment',()=>c.resourceAction('/api/route-experiments/evaluate',{grounding:'current_scenario_routes'}));
  if(c.$('deleteRouteExperiment'))c.actionButton('deleteRouteExperiment',()=>{const model=routeExperimentModel(c.flow());if(!model.active_experiment_id)throw new Error('没有可删除的实验');return c.resourceAction('/api/route-experiments/delete',{experiment_id:model.active_experiment_id});});
  if(c.$('confirmReferenceRouteImport'))c.actionButton('confirmReferenceRouteImport',()=>c.resourceAction('/api/reference-routes/import-confirm',{preview_id:c.$('confirmReferenceRouteImport').dataset.previewId}));
  if(c.$('createReferenceLink'))c.actionButton('createReferenceLink',()=>c.resourceAction('/api/reference-route-links/create',{reference_route_id:c.$('linkReferenceRoute').value,scenario_route_id:c.$('linkScenarioRoute').value,confirmed:true}));
  document.querySelectorAll('[data-delete-reference-link]').forEach(button=>button.onclick=()=>c.resourceAction('/api/reference-route-links/delete',{link_id:button.dataset.deleteReferenceLink}).catch(error=>c.panelError(error.message)));
  document.querySelectorAll('[data-confirm-reference-link]').forEach(button=>button.onclick=()=>{const [referenceRouteId,scenarioRouteId]=button.dataset.confirmReferenceLink.split('|');return c.resourceAction('/api/reference-route-links/create',{reference_route_id:referenceRouteId,scenario_route_id:scenarioRouteId,confirmed:true,origin:'user',source:{type:'user_confirmation_from_endpoint_candidate'}}).catch(error=>c.panelError(error.message));});
  const applyReferenceFilter=()=>{const search=c.$('referenceSiteSearch').value.trim().toLocaleLowerCase(),region=c.$('referenceSiteRegion').value,siteType=c.$('referenceSiteType').value;let visible=0;document.querySelectorAll('[data-reference-site]').forEach(row=>{const show=(!search||row.dataset.search.includes(search))&&(!region||row.dataset.region===region)&&(!siteType||row.dataset.siteType===siteType);row.hidden=!show;if(show)visible++;});const count=c.$('referenceSiteCount');if(count)count.textContent='当前筛选 '+visible+' 条；疑似重复只标记、不合并。';c.paint();};
  c.$('referenceSiteSearch').oninput=applyReferenceFilter;c.$('referenceSiteRegion').onchange=applyReferenceFilter;c.$('referenceSiteType').onchange=applyReferenceFilter;
  document.querySelectorAll('[data-add-reference-site]').forEach(button=>button.onclick=async()=>{try{button.disabled=true;await c.resourceAction('/api/reference-landing-sites/add-to-project',{reference_site_id:button.dataset.addReferenceSite});}catch(error){c.panelError(error.message);button.disabled=false;}});
  document.querySelectorAll('[data-select-reference-route]').forEach(button=>button.onclick=()=>c.selectReference({kind:'route',id:button.dataset.selectReferenceRoute}));
  if(c.$('saveRiskRouteParameters'))c.actionButton('saveRiskRouteParameters',()=>{const current=c.flow().algorithm_selection.route_planner,numberOrNull=id=>{const value=c.$(id).value.trim();return value===''?null:Number(value);};return c.resourceAction('/api/algorithms/select',{algorithm_type:'route_planner',algorithm_id:current.algorithm_id,version:current.version,parameters:{risk_weight_lambda:Number(c.$('routeRiskLambda').value),risk_component:c.$('routeRiskComponent').value,unknown_risk_policy:c.$('routeUnknownPolicy').value,unknown_penalty_index:numberOrNull('routeUnknownPenalty'),max_relative_risk_index:numberOrNull('routeMaxRisk')}});});
  document.querySelectorAll('[data-delete-node]').forEach(button=>button.onclick=()=>c.mutate('node-delete',{node_id:button.dataset.deleteNode}).catch(error=>c.panelError(error.message)));
  document.querySelectorAll('[data-delete-route]').forEach(button=>button.onclick=()=>c.mutate('route-delete',{route_id:button.dataset.deleteRoute}).catch(error=>c.panelError(error.message)));
  c.actionButton('saveRouteAltitude',()=>{const altitudeField=c.$('routeAltitude'),altitudeValue=altitudeField.value.trim()===''?null:Number(altitudeField.value);return c.resourceAction('/api/spatial-3d/route-profile',{route_id:c.$('altitudeRoute').value,mode:'constant',vertical_reference:c.$('routeVerticalReference').value,constant_altitude_m:altitudeValue,source:'user_configuration',confirmed:true});});
  c.actionButton('saveRouteMotion',()=>{const timing=structuredClone(c.flow().operational_timing||{route_motion_profiles:{},service_scenarios:{},response_time_budgets:{},encounter_scenarios:{}}),routeId=c.$('motionRoute').value;timing.route_motion_profiles=timing.route_motion_profiles||{};timing.route_motion_profiles[routeId]={route_id:routeId,mode:'constant_ground_speed_mps',constant_ground_speed_mps:Number(c.$('routeGroundSpeed').value),source:'user_configuration',confirmed:true};return c.resourceAction('/api/operational-timing',{operational_timing:timing});});
  const optional=id=>{const value=c.$(id).value.trim();return value===''?null:Number(value);};
  c.actionButton('saveBuildingClearancePolicy',()=>c.resourceAction('/api/building-clearance/policy',{horizontal_clearance_m:optional('buildingHorizontalClearance'),vertical_clearance_m:optional('buildingVerticalClearance'),min_building_height_m:optional('buildingMinHeight'),terrain_relief_review_m:optional('buildingReliefReview'),source:c.$('buildingClearanceSource').value.trim(),confirmed:c.$('buildingClearanceConfirmed').checked}));
  c.actionButton('evaluateBuildingClearance',()=>c.resourceAction('/api/building-clearance/evaluate',{}));
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(4);
}
