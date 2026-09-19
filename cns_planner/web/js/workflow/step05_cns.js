import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock} from './common.js';

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

/*
 * P12 closed-loop residual semantics
 *
 * 后端当前只提供 legacy residual_gap_segments。
 * 不能把 residual_gap_segments 的数量直接解释成“规划残余”。
 *
 * planning_status === confirmed_gap  -> 规划残余
 * runtime_status  === confirmed_gap  -> 运行场景残余
 */
function asArray(value){
  return Array.isArray(value)?value:[];
}

function sumBy(items,field){
  return asArray(items).reduce((sum,item)=>{
    const value=Number(item?.[field]);
    return sum+(Number.isFinite(value)?value:0);
  },0);
}

function classifyClosedLoopResiduals(result){
  const residuals=asArray(result?.residual_gap_segments);

  const planningResiduals=residuals.filter(
    item=>item?.planning_status==='confirmed_gap'
  );

  const operationalResiduals=residuals.filter(
    item=>item?.runtime_status==='confirmed_gap'
  );

  const operationalKeys=new Set(
    operationalResiduals.map(item=>item?.segment_id).filter(Boolean)
  );

  residuals.forEach(item=>{
    const legacyOperational=
      item?.planning_status==='satisfied' &&
      item?.combined_status==='confirmed_gap' &&
      item?.runtime_status!=='confirmed_gap';

    const key=item?.segment_id;

    if(legacyOperational && (!key || !operationalKeys.has(key))){
      operationalResiduals.push(item);
      if(key)operationalKeys.add(key);
    }
  });

  return {
    residuals,
    planningResiduals,
    operationalResiduals,
    regressions:asArray(result?.regression_segments),
    planningResidualLengthM:sumBy(planningResiduals,'length_m'),
    operationalResidualLengthM:sumBy(operationalResiduals,'length_m'),
    operationalResidualDurationS:sumBy(operationalResiduals,'duration_s')
  };
}

function residualCauses(item){
  const causes=asArray(item?.gap_causes).filter(Boolean);
  return causes.length?causes.join(' / '):'未提供结构化原因';
}

function closedLoopResidualCard(item,kind){
  const title=kind==='planning'?'规划残余缺口':'运行场景残余';
  const startM=Number(item?.start_route_offset_m);
  const endM=Number(item?.end_route_offset_m);
  const lengthM=Number(item?.length_m);
  const startS=Number(item?.start_time_s);
  const endS=Number(item?.end_time_s);
  const durationS=Number(item?.duration_s);

  const rangeText=
    (Number.isFinite(startM)?startM.toFixed(1):'—')+
    '–'+
    (Number.isFinite(endM)?endM.toFixed(1):'—')+
    ' m';

  const timeText=
    (Number.isFinite(startS)?startS.toFixed(1):'—')+
    '–'+
    (Number.isFinite(endS)?endS.toFixed(1):'—')+
    ' s';

  return '<div class="coverage-card">'+
    '<b>'+escapeHtml(item?.route_id||'—')+' · '+escapeHtml(item?.subsystem||'—')+' · '+title+'</b>'+
    '<span>航路位置 '+rangeText+' · 长度 '+formatMetric(lengthM,'m')+'</span>'+
    '<span>时间 '+timeText+' · 持续 '+formatMetric(durationS,'s')+'</span>'+
    '<span>planning '+escapeHtml(item?.planning_status||'unknown')+
      ' · runtime '+escapeHtml(item?.runtime_status||'unknown')+
      ' · combined '+escapeHtml(item?.combined_status||'unknown')+'</span>'+
    '<small>原因：'+escapeHtml(residualCauses(item))+
      '；处置范围：'+escapeHtml(item?.remediation_scope||'unknown')+'</small>'+
    '</div>';
}

function closedLoopSummary(result){
  if(!result||result.status==='not_calculated'){
    return '<div class="empty-note">尚未生成 Closed-loop Preview</div>';
  }

  const prediction=result.prediction_comparison||{};
  const classified=classifyClosedLoopResiduals(result);

  const planningResiduals=classified.planningResiduals;
  const operationalResiduals=classified.operationalResiduals;
  const regressions=classified.regressions;

  const planningClosed=planningResiduals.length===0;
  const noRegression=regressions.length===0;

  const rows=(result.comparisons||[]).map(item=>
    '<div class="coverage-card">'+
      '<b>'+escapeHtml(item.route_id)+' · '+escapeHtml(item.subsystem)+'</b>'+
      '<span>planning gap '+formatMetric(item.before?.planning_confirmed_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.planning_confirmed_gap_length_m,'m')+
        ' · Δ '+formatMetric(item.delta?.planning_confirmed_gap_length_m,'m')+'</span>'+
      '<span>combined gap '+formatMetric(item.before?.combined_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.combined_gap_length_m,'m')+
        ' · unknown '+formatMetric(item.before?.unknown_length_m,'m')+
        ' → '+formatMetric(item.after?.unknown_length_m,'m')+'</span>'+
      '<span>runtime lost '+formatMetric(item.before?.runtime_lost_length_m,'m')+
        ' / '+formatMetric(item.before?.runtime_lost_duration_s,'s')+
        ' → '+formatMetric(item.after?.runtime_lost_length_m,'m')+
        ' / '+formatMetric(item.after?.runtime_lost_duration_s,'s')+'</span>'+
      '<span>contingency '+formatMetric(item.before?.contingency_exposure_length_m,'m')+
        ' → '+formatMetric(item.after?.contingency_exposure_length_m,'m')+
        ' · max gap '+formatMetric(item.before?.max_continuous_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.max_continuous_gap_length_m,'m')+'</span>'+
    '</div>'
  ).join('');

  const planningSummary=planningClosed
    ? '<div class="coverage-card"><b>规划缺口已闭合</b><span>planning residual 0.0 m</span><small>P12 应用后没有 planning_status = confirmed_gap 的 residual segment。</small></div>'
    : '<div class="coverage-card"><b>仍存在规划残余缺口</b><span>'+planningResiduals.length+
      ' 段 · '+formatMetric(classified.planningResidualLengthM,'m')+
      '</span><small>仅统计 planning_status = confirmed_gap。</small></div>';

  const operationalSummary=operationalResiduals.length
    ? '<div class="coverage-card"><b>仍存在运行场景残余</b><span>'+
      operationalResiduals.length+' 段 · '+
      formatMetric(classified.operationalResidualLengthM,'m')+
      ' · '+formatMetric(classified.operationalResidualDurationS,'s')+
      '</span><small>运行场景 residual 不等于规划建站失败；应结合 ServiceScenarioEvent / runtime evidence 处置。</small></div>'
    : '<div class="coverage-card"><b>无 confirmed 运行场景残余</b><span>operational residual 0.0 m</span></div>';

  const planningCards=planningResiduals.slice(0,12)
    .map(item=>closedLoopResidualCard(item,'planning'))
    .join('');

  const operationalCards=operationalResiduals.slice(0,12)
    .map(item=>closedLoopResidualCard(item,'operational'))
    .join('');

  const regressionCards=regressions.slice(0,12).map(item=>
    '<div class="list-row"><span><b>'+
      escapeHtml(item.route_id)+' · '+escapeHtml(item.subsystem)+
      '</b> '+Math.round(item.start_route_offset_m||0)+'–'+
      Math.round(item.end_route_offset_m||0)+
      ' m</span><small>'+
      escapeHtml(item.before_combined)+' → '+
      escapeHtml(item.after_combined)+'</small></div>'
  ).join('');

  const validationTitle=
    escapeHtml(result.validation_status||result.status||'unknown')+
    ' · '+
    escapeHtml(result.commit_status||'not_committed');

  const regressionSummary=noRegression
    ? '<div class="coverage-card"><b>Regression：0</b><span>未发现 confirmed regression</span></div>'
    : '<div class="coverage-card"><b>Regression：'+regressions.length+'</b><span>发现应用方案后 confirmed regression，需要人工复核。</span></div>';

  return '<div class="coverage-card">'+
      '<b>'+validationTitle+'</b>'+
      '<span>P11 predicted '+formatMetric(prediction.predicted_planning_gap_reduction_m,'m')+
        ' · P12 actual '+formatMetric(prediction.actual_planning_gap_reduction_m,'m')+
        ' · error '+formatMetric(prediction.prediction_error_m,'m')+'</span>'+
      '<span>realized '+(prediction.realized_fraction==null?'—':(prediction.realized_fraction*100).toFixed(1)+'%')+'</span>'+
      '<small>'+escapeHtml((result.reasons||[]).join('；')||'no additional reason')+'</small>'+
    '</div>'+
    planningSummary+
    operationalSummary+
    regressionSummary+
    rows+
    '<h4>Planning residuals</h4>'+
      (planningCards||'<div class="empty-note">无 confirmed planning residual gap</div>')+
    '<h4>Operational residuals</h4>'+
      (operationalCards||'<div class="empty-note">无 confirmed operational residual gap</div>')+
    '<h4>Regressions</h4>'+
      (regressionCards||'<div class="empty-note">未发现 confirmed regression</div>');
}

function corridorSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未评估 CNS Service Corridor</div>';
  if(!result.routes?.length)return '<div class="empty-note">Corridor 缺少可评估航路或已确认参数</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+escapeHtml(route.status||'unknown')+' · '+(route.voxel_count||0)+' voxels</b>'+((route.subsystems||[]).map(item=>'<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(item.status||'unknown')+'</b><span>voxel satisfied '+(item.satisfied_voxel_count||0)+' · deficit '+(item.confirmed_deficit_voxel_count||0)+' · unknown '+(item.unknown_voxel_count||0)+'</span><span>volume proxy satisfied '+pct(item.satisfied_volume_fraction)+' · deficit '+pct(item.confirmed_deficit_volume_fraction)+' · unknown '+pct(item.unknown_volume_fraction)+'</span><small>discretized volume proxy；representative voxel probe ≠ entire voxel guarantee</small></div>').join(''))+'</div>').join('');
}

function planningObjectivesPanel(flow){
  const routes=flow.operational_routes||[],firstRoute=routes[0]?.route_id||'',policy=flow.cns_planning_objectives||{routes:{}},entry=policy.routes?.[firstRoute]?.subsystems?.C?.objectives||{};
  const routeOptions=routes.map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  const field=(id,label,name,operator)=>'<label>'+label+' ('+operator+')<input class="panel-input" type="number" min="0" step="any" id="'+id+'" value="'+(entry[name]?.value??'')+'"></label>';
  return '<h3>CNS Spatial Planning Objectives</h3><div class="parameter-note">PlanningObjective 与 RequiredCNS 分离；没有 confirmed 显式目标时保持 objectives_not_configured，不提供监管或工程默认阈值。</div><div class="form-grid"><label>航路<select id="planningObjectiveRoute">'+routeOptions+'</select></label><label>分系统<select id="planningObjectiveSubsystem"><option>C</option><option>N</option><option>S</option></select></label>'+field('objectiveMinSatisfied','最小 satisfied volume fraction','min_satisfied_volume_fraction','≥')+field('objectiveMaxDeficit','最大 confirmed-deficit volume fraction','max_confirmed_deficit_volume_fraction','≤')+field('objectiveMaxUnknown','最大 unknown volume fraction','max_unknown_volume_fraction','≤')+field('objectiveMinRedundancy','最小 redundancy-satisfied volume fraction','min_redundancy_satisfied_volume_fraction','≥')+field('objectiveMaxContinuous','最大 spatial continuous-deficit projection m','max_continuous_deficit_projection_m','≤')+'<label>来源<input id="planningObjectiveSource" value="user_configuration"></label><label class="check-row"><input type="checkbox" id="planningObjectiveConfirmed">目标已确认</label></div><div class="button-row"><button class="secondary" id="savePlanningObjectives" '+(!routeOptions?'disabled':'')+'>保存目标</button><button class="primary" id="evaluateCorridorGap">评估 Corridor Gap</button></div>';
}

function corridorGapSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未评估 Corridor Gap</div>';
  if(!result.routes?.length)return '<div class="empty-note">需要 current P14 corridor assessment</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+escapeHtml(route.status||'unknown')+'</b>'+route.subsystems.map(item=>{const service=item.service||{},redundancy=item.redundancy||{},segments=item.continuous_deficit_segments||[],objectiveRows=(item.objective_results||[]).map(value=>'<div class="list-row"><span>'+escapeHtml(value.objective)+'</span><small>actual '+(value.actual==null?'—':Number(value.actual).toFixed(3))+' '+escapeHtml(value.operator||'')+' '+(value.target??'—')+' · '+escapeHtml(value.status)+'</small></div>').join('');return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(item.status)+' · '+escapeHtml(item.objective_status)+'</b><span>service satisfied '+pct(service.fractions?.satisfied)+' · deficit '+pct(service.fractions?.confirmed_deficit)+' · unknown '+pct(service.fractions?.unknown)+'</span><span>redundancy satisfied '+pct(redundancy.fractions?.satisfied)+' · deficit '+pct(redundancy.fractions?.confirmed_deficit)+' · unknown '+pct(redundancy.fractions?.unknown)+'</span><span>spatial continuous-deficit total '+formatMetric(item.total_confirmed_deficit_projection_m,'m')+' · max '+formatMetric(item.max_continuous_deficit_projection_m,'m')+' · segments '+segments.length+'</span>'+objectiveRows+'</div>';}).join('')+'</div>').join('');
}

function corridorSitePlanSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未运行 Corridor-aware Site Planner V2</div>';
  const selected=(result.selected_actions||[]).map(action=>'<div class="coverage-card"><b>'+escapeHtml(action.action_id)+'</b><span>'+escapeHtml(action.reuse_class||'unknown')+' · '+escapeHtml(action.subsystem||'')+' · unit-volume gain '+formatMetric(action.marginal_confirmed_requirement_unit_volume_gain,'m³·unit')+'</span><small>'+escapeHtml(action.score_semantics||'action_count_proxy')+'</small></div>').join('');
  const trace=(result.iteration_trace||[]).map(item=>'<div class="list-row"><span>#'+item.iteration+' '+escapeHtml(item.selected_action_id)+'</span><small>'+escapeHtml(item.reuse_class)+' · marginal '+formatMetric(item.marginal_impact?.confirmed_requirement_unit_volume_gain,'m³·unit')+'</small></div>').join('');
  const objectiveRows=[];for(const route of result.after?.routes||[])for(const subsystem of route.subsystems||[])for(const item of subsystem.objective_results||[])objectiveRows.push('<div class="list-row"><span>'+escapeHtml(route.route_id)+' '+escapeHtml(subsystem.subsystem)+' · '+escapeHtml(item.objective)+'</span><small>actual '+(item.actual==null?'—':Number(item.actual).toFixed(3))+' · '+escapeHtml(item.status)+'</small></div>');
  return '<div class="coverage-card"><b>'+escapeHtml(result.status||'unknown')+' · Proposal</b><span>targets '+(result.target_voxel_count||0)+' · selected '+(result.selected_actions||[]).length+' · confirmed unit-volume gain '+formatMetric(result.confirmed_requirement_unit_volume_gain,'m³·unit')+'</span><span>residual '+(result.residual_confirmed_targets||[]).length+' · evidence required '+(result.unknown_evidence_required||[]).length+' · '+escapeHtml(result.stop_reason||'')+'</span><small>Proposal only；Existing CNS/P14/P15 未修改，需用户确认后另行 apply。</small></div><h4>Selected actions / iteration gain</h4>'+(selected||'<div class="empty-note">没有 confirmed positive marginal action</div>')+trace+'<h4>Before / After objectives</h4>'+(objectiveRows.join('')||'<div class="empty-note">未配置 confirmed planning objectives</div>');
}

function formatMetric(value,unit){return Number.isFinite(value)?value.toFixed(1)+' '+unit:'—';}

function equipmentReferencePanel(flow){
  const catalog=flow.equipment_reference_catalog||{},items=catalog.items||[];
  const rows=items.map(item=>'<div class="coverage-card equipment-reference-card"><b>'+escapeHtml(item.name)+' <span>'+escapeHtml(item.equipment_id)+'</span></b><span>'+escapeHtml(item.manufacturer||'厂商待确认')+(item.model?' · '+escapeHtml(item.model):'')+' · '+escapeHtml((item.subsystems||[]).join('/')||'非CNS规划设备')+'</span><small>技术：'+escapeHtml((item.technology||[]).join('、')||'未标')+'；来源：'+escapeHtml(item.source?.file||'未登记')+' '+escapeHtml(item.source?.locator||'')+'</small><small>planning mapping：'+escapeHtml(item.planning_mapping?.status||'not_mapped')+'</small></div>').join('');
  return '<h3>真实设备资料库 '+statusBadge(catalog.status||'not_calculated')+'</h3><div class="parameter-note">equipment_reference_catalog 是来源事实模型，与当前算法 DeviceCatalog 分离。下列参数只读，不完整记录不会自动补 radius、MTBF、MTTR、cost 或 capacity，也不会自动参与 Coverage/Site Planner。</div><div class="scroll-list equipment-reference-list">'+(rows||'<div class="empty-note">尚无真实设备参考记录</div>')+'</div>';
}

export function render({flow}){
  const devices=(flow.devices||[]).map((device,index)=>'<div class="device-row"><b>'+device.subsystem+' · '+escapeHtml(device.model||device.name||device.device_id)+'</b><label>R(m)<input type="number" data-device-radius="'+index+'" value="'+device.radius_m+'"></label><label>MTBF(h)<input type="number" data-device-mtbf="'+index+'" value="'+(device.mtbf_h||device.mtbf)+'"></label><span>'+device.role+'</span></div>').join('');
  let result='<div class="empty-note">尚未运行 CoveragePlannerV1</div>';
  if(flow.coverage)result=Object.entries(flow.coverage.layers||{}).map(([key,layer])=>{const stats=layer.statistics;return '<div class="coverage-card"><b>'+key+' '+statusBadge(layer.status)+'</b><span>站点 '+stats.stations+' · 主站 '+stats.primary+' · 补盲 '+stats.gap+' · 共址 '+stats.colocated+'</span><span>平均重数 '+stats.average_multiplicity+' · 未覆盖 '+stats.uncovered_samples+'</span></div>';}).join('');
  const params=flow.defaults.engineering_parameters,existing=flow.existing_cns_facilities||{},candidates=flow.candidate_sites||{},catalog=flow.device_catalog||{},gaps=flow.cns_gap_analysis||{},coverage3d=flow.coverage_3d||{},capability=flow.cns_service_capability||{},corridor=flow.cns_corridor_assessment||{},corridorGap=flow.cns_corridor_gap_assessment||{},corridorSitePolicy=flow.corridor_site_planning_policy||{},corridorSitePlan=flow.cns_corridor_site_plan||{},timeline=flow.service_timeline||{},protection=flow.protection_envelope||{},gapV2=flow.cns_gap_analysis_v2||{},sitePolicy=flow.site_planning_policy||{},sitePlan=flow.cns_site_plan||{},closedLoop=flow.closed_loop_assessment||{};
  const operate='<div class="demo-note">DeviceCatalog：'+escapeHtml(catalog.source||flow.device_source)+' · '+(catalog.count||0)+' 型设备</div>'+
    equipmentReferencePanel(flow)+
    '<div class="parameter-note">主站间距 '+params.primary_spacing_factor.value+'R · 共址半径 '+params.co_location_search_radius_m.value+'m<br>'+escapeHtml(params.primary_spacing_factor.source)+'</div>'+
    '<div class="device-list">'+devices+'</div><div class="button-row"><button class="secondary" id="saveDevices">保存设备参数</button><button class="primary" id="planCoverage">运行布站</button></div>'+
    '<h3>已有 CNS 设施 '+statusBadge(existing.status||'not_calculated')+'</h3><div class="panel-file-input"><input class="panel-input" id="existing_cnsPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseExisting">选择…</button></div><button class="secondary full" id="importExisting">导入已有设施</button><div class="scroll-list cns-input-list">'+collectionList(existing,'facility')+'</div>'+
    '<h3>候选站址 '+statusBadge(candidates.status||'not_calculated')+'</h3><div class="panel-file-input"><input class="panel-input" id="candidate_sitesPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseCandidates">选择…</button></div><div class="button-row"><button class="secondary" id="importCandidates">导入候选站址</button><button class="secondary" id="deriveCandidates">从已有设施生成</button></div><div class="scroll-list cns-input-list">'+collectionList(candidates,'candidate')+'</div>';
  const resultPanel='<h3>CNS Gap Analysis '+statusBadge(gaps.status||'not_calculated')+'</h3><button class="primary full" id="analyzeGaps">分析当前运行航路缺口</button><div class="gap-results">'+gapList(gaps)+'</div>'+
    '<h3>3D Geometric Coverage '+statusBadge(coverage3d.status||'not_calculated')+'</h3><div class="parameter-note">几何覆盖 ≠ 真实 CNS 性能；传播、LOS、绕射、干扰、链路预算和传感器 Pd 均未评估。</div><label>sample spacing (m)<input class="panel-input" type="number" id="coverage3dSpacing" value="'+(coverage3d.parameters?.sample_spacing_m||flow.algorithm_selection?.coverage_model?.parameters?.sample_spacing_m||500)+'"></label><button class="secondary full" id="evaluateCoverage3d">运行 3D 几何覆盖</button><div class="gap-results">'+coverage3dList(coverage3d)+'</div>'+
    '<h3>CNS Service Capability '+statusBadge(capability.status||'not_calculated')+'</h3><div class="parameter-note">静态能力满足 ≠ 当前服务 available。模型成熟度：engineering baseline；LOS/绕射/干扰/负载/切换等未评估。</div><button class="secondary full" id="evaluateServiceCapability">评估技术感知静态能力</button><div class="gap-results">'+capabilityList(capability)+'</div>'+
    '<h3>CNS Service Requirement Corridor '+statusBadge(corridor.status||'not_calculated')+'</h3><div class="parameter-note">工程 CNS 服务需求走廊，不是 JARUS Operational Volume、U-space Surveillance Volume 或法规批准空间。水平为保守网格纳入，体积为 discretized volume proxy。</div><button class="secondary full" id="evaluateCorridor">评估 Corridor Voxels</button><div class="gap-results">'+corridorSummary(corridor)+'</div>'+
    planningObjectivesPanel(flow)+'<div class="parameter-note">Spatial continuous-deficit 是 corridor voxel 的保守纵向投影，不是 runtime outage、正式 ICAO continuity 或 availability probability。</div><div class="gap-results">'+corridorGapSummary(corridorGap)+'</div>';
  const advanced='<h3>Corridor-aware Reuse-first Site Planner V2 '+statusBadge(corridorSitePlan.status||'not_calculated')+'</h3><div class="parameter-note">只规划 P15 confirmed target voxels；通过累计 P14→P15 what-if 验证 service/独立冗余收益。Unknown 不触发建站，不评估共因故障。</div><label class="check-row"><input type="checkbox" id="corridorSitePolicyConfirmed" '+(corridorSitePolicy.confirmed?'checked':'')+'> 确认 corridor reuse-first planning policy</label><button class="secondary full" id="evaluateCorridorSitePlan">生成 P16 Proposal</button><div class="gap-results">'+corridorSitePlanSummary(corridorSitePlan)+'</div>'+
    '<h3>C/N/S Service Timeline '+statusBadge(timeline.status||'not_calculated')+'</h3><div class="parameter-note">运行状态只来自显式 ServiceScenarioEvent；不从 P8 meets、ReliabilitySpec 或 MTBF 推断 available/outage。</div><button class="secondary full" id="evaluateServiceTimeline">生成服务时间线</button><div class="gap-results">'+timelineList(timeline)+'</div>'+
    '<h3>Tactical Protection Envelope '+statusBadge(protection.status||'not_calculated')+'</h3><div class="parameter-note">工程保护距离 ≠ 法规 Well-Clear / 正式 DAA Detection Volume。</div><button class="secondary full" id="evaluateProtectionEnvelope">计算工程保护距离</button><div class="gap-results">'+protectionSummary(protection)+'</div>'+
    '<h3>CNS Gap Analysis V2 '+statusBadge(gapV2.status||'not_calculated')+'</h3><div class="parameter-note">合并 P7 几何、P8 静态能力与 P9 运行时间线；Unknown 表示证据不足，不是危险等级，Gap 也不自动触发 Safety Event。</div><label class="check-row"><input type="checkbox" id="gapV2Protection" '+(gapV2.parameters?.evaluate_protection_margin?'checked':'')+'> 可选工程 Protection Margin（非 Well-Clear/认证判断）</label><button class="secondary full" id="evaluateGapV2">运行 Gap V2</button><div class="gap-results">'+gapV2List(gapV2)+'</div>'+
    '<h3>Reuse-first CNS Site Planner V1 '+statusBadge(sitePlan.status||'not_calculated')+'</h3><div class="parameter-note">仅目标化 confirmed planning gap；tier 固定为 Existing CNS → Existing Shared Site → Candidate Site → New-build Candidate。P10 remediation scope 仅为提示，收益必须经 P7/P8 what-if 确认。</div><label class="check-row"><input type="checkbox" id="sitePolicyConfirmed" '+(sitePolicy.confirmed?'checked':'')+'> 确认使用 reuse-first engineering policy</label><button class="secondary full" id="evaluateSitePlan">生成 Proposal</button><div class="parameter-note">Proposal 不修改 ExistingCNS，也不声明 Gap 已消除；P12 必须 apply + rerun 闭环复核。</div><div class="gap-results">'+sitePlanSummary(sitePlan)+'</div>'+
    '<h3>Closed-loop Validation V1 '+statusBadge(closedLoop.status||'not_calculated')+'</h3><div class="parameter-note">Engineering closed-loop verification：Preview 只在 working copy 重跑 P7→P8→P9→P10，不修改项目；Apply 才正式提交。规划 residual 与运行场景 residual 分开解释；这不是真实 CNS 模型 validation 或认证结论。</div><div class="button-row"><button class="secondary" id="evaluateClosedLoop">Preview</button><button class="primary" id="applyClosedLoop" '+(closedLoop.validation_status==='validated_improvement'&&closedLoop.commit_status==='preview'?'':'disabled')+'>Apply validated assessment</button></div><div class="gap-results">'+closedLoopSummary(closedLoop)+'</div>';
  const body=wbPanel('operate',wbBlock('设备与布站输入',operate))
    +wbPanel('result',wbBlock('覆盖与缺口结果',resultPanel)+'<div class="coverage-results">'+result+'</div>')
    +wbPanel('advanced',wbBlock('高级与应用',advanced)
      +'<div class="flow-summary">生命风险：'+statusText(flow.risks.life.status)+' · 财产风险：'+statusText(flow.risks.property.status)+'<br>已有设施与候选站址仅作为规划输入，本轮不改变 CoveragePlannerV1。</div>')
    +'<button class="primary full" id="nextStep" '+(!flow.steps['5']?'disabled':'')+'>下一步：方案评审</button>';
  return shell('05','CNS规划','设备库、已有设施和候选站址；V1 布站保持原有兼容输入。',body);
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
  c.actionButton('evaluateCorridor',()=>c.resourceAction('/api/cns-service-corridor/evaluate',{}));
  c.actionButton('savePlanningObjectives',()=>{const policy=structuredClone(c.flow().cns_planning_objectives||{routes:{}}),routeId=c.$('planningObjectiveRoute').value,code=c.$('planningObjectiveSubsystem').value,confirmed=c.$('planningObjectiveConfirmed').checked,source=c.$('planningObjectiveSource').value.trim()||'user_configuration',specs={min_satisfied_volume_fraction:['objectiveMinSatisfied','>='],max_confirmed_deficit_volume_fraction:['objectiveMaxDeficit','<='],max_unknown_volume_fraction:['objectiveMaxUnknown','<='],min_redundancy_satisfied_volume_fraction:['objectiveMinRedundancy','>='],max_continuous_deficit_projection_m:['objectiveMaxContinuous','<=']};policy.routes=policy.routes||{};policy.routes[routeId]=policy.routes[routeId]||{route_id:routeId,subsystems:{}};policy.routes[routeId].subsystems=policy.routes[routeId].subsystems||{};policy.routes[routeId].subsystems[code]={objectives:Object.fromEntries(Object.entries(specs).filter(([,value])=>c.$(value[0]).value!=='').map(([name,[id,operator]])=>[name,{value:Number(c.$(id).value),operator,source,confirmed}]))};return c.resourceAction('/api/cns-planning-objectives',{cns_planning_objectives:policy});});
  c.actionButton('evaluateCorridorGap',()=>c.resourceAction('/api/cns-corridor-gap/evaluate',{}));
  c.actionButton('evaluateCorridorSitePlan',()=>{const policy=structuredClone(c.flow().corridor_site_planning_policy||{});policy.confirmed=c.$('corridorSitePolicyConfirmed').checked;policy.source='user_configuration';return c.resourceAction('/api/cns-corridor-site-plan/evaluate',{corridor_site_planning_policy:policy});});
  c.actionButton('evaluateServiceTimeline',()=>c.resourceAction('/api/service-timeline/evaluate',{}));
  c.actionButton('evaluateProtectionEnvelope',()=>c.resourceAction('/api/protection-envelope/evaluate',{}));
  c.actionButton('evaluateGapV2',()=>c.resourceAction('/api/cns-gap-analysis-v2',{parameters:{evaluate_protection_margin:c.$('gapV2Protection').checked}}));
  c.actionButton('evaluateSitePlan',()=>{const policy=structuredClone(c.flow().site_planning_policy||{});policy.confirmed=c.$('sitePolicyConfirmed').checked;policy.source='user_configuration';return c.resourceAction('/api/cns-site-plan',{site_planning_policy:policy});});
  c.actionButton('evaluateClosedLoop',()=>c.resourceAction('/api/cns-closed-loop/evaluate',{}));
  c.actionButton('applyClosedLoop',()=>c.resourceAction('/api/cns-closed-loop/apply',{application_id:c.flow().closed_loop_assessment?.application?.application_id}));
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(6);
}
