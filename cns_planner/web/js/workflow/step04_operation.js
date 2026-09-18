import {escapeHtml,shell,statusBadge,wbPanel,wbSection} from './common.js';
import {renderProtectionBudget} from './protection_budget.js';
import {renderDaaEncounterLab,bindDaaEncounterLab} from './daa_encounter_lab.js';
import {
  V3D_CNS_SEPARATION_LABEL, V3D_STAGES, routePlannerV3AdoptionModel,
} from './step03_routes.js';

const value=(object,key,fallback='')=>object?.[key]??fallback;
const numberValue=value=>value===null||value===undefined?'':value;
const requiredValue=value=>value===true?'yes':value===false?'no':'pending';
const selected=(value,current)=>value===current?'selected':'';
const options=(values,current,pending=false)=>(pending?'<option value="">待确认</option>':'')+values.map(item=>'<option value="'+item+'" '+selected(item,current)+'>'+item+'</option>').join('');
const C_TECH=['unknown','4g','5g','dedicated_radio','satellite','wifi','other'];
const C_SCOPE=['unknown','public','private','dedicated','managed_service','other'];
const N_TECH=['unknown','gnss','gnss_rtk','inertial','visual','terrestrial','hybrid','other'];
const S_COOP=['cooperative','non_cooperative','mixed'];
const S_MODE=['active','passive','mixed'];
const S_TECH=['unknown','adsb','radar','multilateration','eo_ir','acoustic','network_remote_id','other'];

function input(id,label,current,type='number'){
  const attrs=type==='number'?'type="number" step="any" min="0"':'type="text"';
  return '<label>'+label+'<input '+attrs+' id="'+id+'" value="'+escapeHtml(numberValue(current))+'"></label>';
}
function selectInput(id,label,values,current,pending=false){return '<label>'+label+'<select id="'+id+'">'+options(values,current,pending)+'</select></label>';}
function header(prefix,title,data){return '<legend>'+title+' '+statusBadge(data.status||'pending_confirmation')+'</legend><label>是否需要<select id="'+prefix+'Required"><option value="pending" '+selected('pending',requiredValue(data.required))+'>待确认</option><option value="yes" '+selected('yes',requiredValue(data.required))+'>需要</option><option value="no" '+selected('no',requiredValue(data.required))+'>不需要</option></select></label>';}
function confirmation(prefix,data){return input(prefix+'Source','来源',data.source,'text')+'<label class="check-row"><input type="checkbox" id="'+prefix+'Confirmed" '+(data.confirmed?'checked':'')+'>参数已确认</label>'+input(prefix+'Contingency','Contingency',typeof data.contingency==='string'?data.contingency:'','text');}

function communication(data){const type=data.type||{},p=data.performance||{};return header('c','Communication',data)+input('cServiceType','Service type',type.service_type,'text')+selectInput('cTechnology','Technology',C_TECH,type.technology||'unknown')+selectInput('cNetworkScope','Network scope',C_SCOPE,type.network_scope||'unknown')+input('cInterfaces','Interfaces（逗号分隔）',(type.interfaces||[]).join(', '),'text')+input('cCoverage','覆盖要求 %',data.coverage_requirement)+input('cMaxGap','最大缺口 m',data.max_gap_m)+input('cMaxLatency','最大时延 s',p.max_latency_s)+input('cLostLink','失链阈值 s',p.lost_link_threshold_s)+input('cContinuousOutage','最大连续中断 s',p.max_continuous_outage_s)+input('cCumulativeOutage','最大累计中断 s',p.max_cumulative_outage_s)+input('cAvailability','最小可用度 0..1',p.min_availability)+input('cRedundancy','最小冗余',p.min_redundancy)+confirmation('c',data);}
function navigation(data){const type=data.type||{},p=data.performance||{};return header('n','Navigation',data)+selectInput('nTechnology','Technology',N_TECH,type.technology||'unknown')+input('nCoverage','覆盖要求 %',data.coverage_requirement)+input('nHorizontalError','最大水平误差 m',p.max_horizontal_error_m)+input('nVerticalError','最大垂直误差 m',p.max_vertical_error_m)+input('nIntegrity','完整性要求',p.integrity_required,'text')+input('nTimeToAlert','最大告警时间 s',p.max_time_to_alert_s)+input('nDegradation','最大降级时间 s',p.max_degradation_time_s)+input('nAvailability','最小可用度 0..1',p.min_availability)+input('nRedundancy','最小冗余',p.min_redundancy)+confirmation('n',data);}
function surveillance(data){const type=data.type||{},p=data.performance||{};return header('s','Surveillance',data)+selectInput('sTargetCooperation','Target cooperation',S_COOP,type.target_cooperation,true)+selectInput('sSensorMode','Sensor mode',S_MODE,type.sensor_mode,true)+selectInput('sTechnology','Technology',S_TECH,type.technology||'unknown')+input('sCoverage','覆盖要求 %',data.coverage_requirement)+input('sDetectionRange','最小探测距离 m',p.min_detection_range_m)+input('sDetectionProbability','最小探测概率 0..1',p.min_detection_probability)+input('sMaxUpdate','最大更新间隔 s',p.max_update_interval_s)+input('sTrackLoss','最大航迹丢失 s',p.max_track_loss_s)+input('sAlertLatency','最大告警时延 s',p.max_alert_latency_s)+input('sAvailability','最小可用度 0..1',p.min_availability)+input('sRedundancy','最小冗余',p.min_redundancy)+confirmation('s',data);}

export function withLegacyRequiredAliases(requirements){
  const result=structuredClone(requirements),c=result.communication,n=result.navigation,s=result.surveillance;
  c.latency_ms=c.performance.max_latency_s==null?null:c.performance.max_latency_s*1000;c.redundancy=c.performance.min_redundancy;
  n.accuracy_m=n.performance.max_horizontal_error_m;n.integrity=n.performance.integrity_required;n.redundancy=n.performance.min_redundancy;
  s.update_interval_s=s.performance.max_update_interval_s;s.redundancy=s.performance.min_redundancy;
  return result;
}

function capabilitySummary(item){
  const pairs=object=>Object.entries(object||{}).filter(([,current])=>current!==null&&current!==undefined&&current!==''&&(!Array.isArray(current)||current.length)).map(([key,current])=>key+'='+([].concat(current).join('|'))).join(', ')||'待确认';
  const reliability=data=>{const current=data?.reliability||{};return (current.model||'unknown')+', MTBF='+(current.mtbf_h??'null')+' h, availability='+(current.availability??'null')+', source='+(current.source||'未记录')+', '+(current.status||'missing_data');};
  const summary=(label,data)=>label+' type['+pairs(data?.type)+'] performance['+pairs(data?.performance)+'] capabilities['+((data?.capabilities||[]).join(', ')||'待确认')+'] contingency['+(typeof data?.contingency==='string'?data.contingency:'待确认')+'] fallbacks='+(data?.fallbacks?.length||0)+' reliability['+reliability(data)+'] '+(data?.confirmation_status||(data?.confirmed?'confirmed':'pending_confirmation'));
  return '<div class="flow-summary"><strong>Aircraft Capability</strong><br>'+escapeHtml(summary('C',item.communication))+'<br>'+escapeHtml(summary('N',item.navigation))+'<br>'+escapeHtml(summary('S',item.surveillance))+'</div>';
}

function safetyPolicyPanel(policy={}){
  const failureConditions=policy.failure_conditions||[],unacceptableEvents=policy.unacceptable_events||[];
  const dependencies=policy.functional_dependencies||[],coupledConditions=policy.coupled_conditions||[];
  const fcOptions=failureConditions.map(item=>'<option value="'+escapeHtml(item.failure_condition_id)+'">'+escapeHtml(item.failure_condition_id+' · '+item.subsystem+' · '+item.failure_mode)+'</option>').join('');
  const coupledOptions=coupledConditions.map(item=>'<option value="'+escapeHtml(item.coupled_condition_id)+'">'+escapeHtml(item.coupled_condition_id+' · '+item.logic)+'</option>').join('');
  const records=failureConditions.map(item=>item.failure_condition_id+': severity='+item.severity+', '+item.status).join(' · ')||'无';
  const firstDependency=dependencies.find(item=>item.dependency_id===coupledConditions[0]?.functional_dependency_ref);
  const previewObservations=(firstDependency?.stages||[]).map((stage,index)=>({ref:stage.event_ref,subsystem:stage.subsystem,status:'triggered',start_s:index*2,end_s:index*2+1,source:'manual_preview'}));
  return '<h3>Safety Assessment Policy</h3>'+
    '<div class="demo-note">ARP4761A/FAA-inspired engineering assessment，仅用于工程分析，不是认证结论。ServiceState、FailureCondition 与 UnacceptableEvent 为不同层级；服务 lost 不会自动成为 unacceptable/catastrophic。</div>'+
    '<div class="flow-summary">'+statusBadge(policy.status||'pending_confirmation')+
    ' source='+escapeHtml(policy.source||'未记录')+' · FC '+failureConditions.length+
    ' · UE '+unacceptableEvents.length+' · FTA '+(policy.fault_trees||[]).length+
    ' · FMEA '+(policy.fmea_records||[]).length+'<br>'+escapeHtml(records)+'</div>'+
    '<div class="form-grid"><label>策略来源<input id="safetyPolicySource" value="'+escapeHtml(policy.source||'project_template')+'"></label>'+
    '<label class="check-row"><input type="checkbox" id="safetyPolicyConfirmed" '+(policy.confirmed?'checked':'')+'>策略已确认</label>'+
    '<label>Failure Condition<select id="safetyFailureCondition">'+fcOptions+'</select></label>'+
    '<label>上游 Service State<select id="safetyServiceState"><option>available</option><option>available_degraded</option><option>contingency</option><option>lost</option><option>unknown</option><option>not_applicable</option></select></label></div>'+
    '<button class="secondary" id="saveSafetyPolicy">保存 Safety Policy</button> '+
    '<button class="secondary" id="previewSafetyEvent">事件预览（不保存）</button>'+
    '<pre class="flow-summary" id="safetyEventPreview">选择 Failure Condition 与 Service State 后预览。</pre>'+
    '<h3>Functional Coupling</h3>'+
    '<div class="demo-note">C+S / C+N / N+S 模板仅为未确认研究假设，必须按运行条件确认；P6 不计算耦合概率，也不假设各分系统独立。</div>'+
    '<div class="flow-summary">Dependencies '+dependencies.length+' · Coupled Conditions '+coupledConditions.length+' · Coupled UE '+(policy.coupled_unacceptable_events||[]).length+'</div>'+
    '<label>Coupled Condition<select id="coupledCondition">'+coupledOptions+'</select></label>'+
    '<label>EventObservations JSON<textarea id="coupledObservations" rows="7">'+escapeHtml(JSON.stringify(previewObservations,null,2))+'</textarea></label>'+
    '<label>OperationalContext JSON<textarea id="coupledOperationalContext" rows="3">{}</textarea></label>'+
    '<button class="secondary" id="previewCoupledEvent">Coupled Event Preview（不保存）</button>'+
    '<pre class="flow-summary" id="coupledEventPreview">选择 Coupled Condition 并提供观察数据后预览。</pre>';
}

function operationalTimingPanel(flow){
  const timing=flow.operational_timing||{},routes=flow.operational_routes||[];
  const routeOptions=routes.map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  const firstRoute=routes[0]?.route_id||'',scenario=(timing.service_scenarios||{})[firstRoute]||{},events=scenario.events||[{event_id:'EVENT-1',subsystem:'C',start_s:0,end_s:10,external_state:'unknown',type:{},performance:{},source:'user_configuration',confirmed:false}];
  const budget=Object.values(timing.response_time_budgets||{})[0]||{},components=budget.components||{};
  const encounter=Object.values(timing.encounter_scenarios||{})[0]||{};
  const timeInput=(id,label,key)=>input(id,label,components[key]?.value_s);
  return '<h3>Operational Timing & Service Scenario</h3>'+
    '<div class="demo-note">P8 静态 capability 不会自动转为 P4 available；只有 confirmed ServiceScenarioEvent 才产生运行状态。</div>'+
    '<label>运行航路<select id="serviceScenarioRoute">'+routeOptions+'</select></label>'+
    '<label>ServiceScenarioEvent JSON<textarea id="serviceScenarioEvents" rows="8">'+escapeHtml(JSON.stringify(events,null,2))+'</textarea></label>'+
    '<label class="check-row"><input type="checkbox" id="serviceScenarioConfirmed" '+(scenario.confirmed?'checked':'')+'>场景与事件已确认</label>'+
    '<button class="secondary full" id="saveServiceScenario" '+(!routeOptions?'disabled':'')+'>保存 Service Scenario</button>'+
    '<h3>Response Time Budget</h3><div class="form-grid">'+
    timeInput('rtDetect','Detect s','detect')+timeInput('rtTrack','Track s','track')+
    timeInput('rtProcessing','Processing s','processing')+timeInput('rtDecision','Decision s','decision')+
    timeInput('rtCommunication','Communication s','communication')+timeInput('rtReaction','Aircraft reaction s','aircraft_reaction')+
    input('rtSource','时间预算来源',budget.source,'text')+
    '<label class="check-row"><input type="checkbox" id="rtConfirmed" '+(budget.confirmed?'checked':'')+'>各时间分量已确认</label></div>'+
    '<button class="secondary full" id="saveResponseBudget">保存 Response Time Budget</button>'+
    '<h3>Encounter Scenario</h3><div class="form-grid">'+
    input('encounterRelativeSpeed','Relative closing speed m/s',encounter.relative_closing_speed_mps)+
    input('encounterManeuver','Maneuver distance m',encounter.maneuver_distance_m)+
    input('encounterUncertainty','Uncertainty distance m',encounter.uncertainty_distance_m)+
    input('encounterSource','Encounter 来源',encounter.source,'text')+
    '<label class="check-row"><input type="checkbox" id="encounterConfirmed" '+(encounter.confirmed?'checked':'')+'>Encounter 参数已确认</label></div>'+
    '<button class="secondary full" id="saveEncounterScenario">保存 Encounter Scenario</button>'+
    '<div class="parameter-note">Relative closing speed 必须显式输入，不会用 ownship/cruise speed 代替。</div>';
}

function corridorPolicyPanel(flow){
  const routes=flow.operational_routes||[],policy=flow.cns_corridor_policy||{routes:{}},first=routes[0]?.route_id||'',spec=(policy.routes||{})[first]||{};
  const routeOptions=routes.map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  return '<h3>CNS Service Requirement Corridor</h3><div class="demo-note">Engineering CNS service requirement corridor；不等同 JARUS Operational Volume、U-space Surveillance Volume 或法规批准空间。宽度和垂向余量必须显式确认。</div>'+selectInput('corridorRoute','运行航路',routes.map(item=>item.route_id),first)+
    '<div class="form-grid">'+input('corridorHalfWidth','水平半宽 m',spec.horizontal_half_width_m)+input('corridorLower','下垂向余量 m',spec.vertical_lower_margin_m)+input('corridorUpper','上垂向余量 m',spec.vertical_upper_margin_m)+input('corridorSource','来源',spec.source,'text')+'<label class="check-row"><input type="checkbox" id="corridorConfirmed" '+(spec.confirmed?'checked':'')+'>走廊参数已确认</label></div><button class="secondary full" id="saveCorridorPolicy" '+(!routeOptions?'disabled':'')+'>保存并评估 Corridor</button>';
}

const OPERATION_CONTEXT_OPTIONS={
  operation_mode:['unknown','vlos','bvlos','bvlos_with_airspace_observer','other'],
  airspace_context:['unknown','controlled','uncontrolled','u_space','other'],
  uas_traffic_context:['unknown','single_uas','multiple_uas'],
  traffic_mix:['unknown','uas_only','mixed_manned_unmanned'],
  manned_traffic_density:['unknown','low','medium','high'],
};

export function requirementRecommendationSummary(recommendation={}){
  return {
    status:recommendation.status||'not_calculated',
    matched:(recommendation.matched_policies||[]).length,
    unknown:(recommendation.unknown_policies||[]).length,
    conflicts:(recommendation.conflicts||[]).length,
    changes:(recommendation.current_vs_recommended_diff||[]).length,
    diverged:recommendation.current_required_cns_diverged===true,
  };
}

function requirementPolicyPanel(flow){
  const context=flow.cns_operation_context||{project_default:{},route_overrides:{}},policies=flow.cns_requirement_policies||{items:[]};
  const recommendation=flow.required_cns_recommendation||{},summary=requirementRecommendationSummary(recommendation);
  const routes=flow.scenario_routes||[],scopeOptions='<option value="project">项目默认上下文</option>'+routes.map(route=>'<option value="'+escapeHtml(route.route_id)+'">航路覆盖：'+escapeHtml(route.route_id)+'</option>').join('');
  const fields=Object.entries(OPERATION_CONTEXT_OPTIONS).map(([name,values])=>selectInput('operationContext_'+name,name,values,context.project_default?.[name]?.value||'unknown')).join('');
  const diff=(recommendation.current_vs_recommended_diff||[]).slice(0,12).map(item=>item.field_path+': '+JSON.stringify(item.current)+' → '+JSON.stringify(item.recommended)).join('\n')||'无可显示变更';
  const provenance=Object.entries(recommendation.field_provenance||{}).slice(0,10).map(([field,items])=>field+' ← '+items.map(item=>item.policy_id+' ('+(item.reference||'无reference')+(item.clause?' §'+item.clause:'')+')').join(', ')).join('\n')||'尚无 provenance';
  const model=flow.algorithm_selection?.requirement_model||{};
  return '<h3>Operation Context → RequiredCNS Recommendation</h3>'+
    '<div class="demo-note">'+escapeHtml((model.algorithm_id||'manual_required_cns_v1')+'@'+(model.version||'1.0'))+'；运行上下文只通过 confirmed、可追溯 Policy 生成建议，不自动判断法规合规，也不会自动修改正式 RequiredCNS。</div>'+
    '<label>上下文作用域<select id="operationContextScope">'+scopeOptions+'</select></label><div class="form-grid">'+fields+input('operationContextSource','上下文来源','user_configuration','text')+'<label class="check-row"><input type="checkbox" id="operationContextConfirmed">字段均已确认</label></div>'+
    '<button class="secondary full" id="saveOperationContext">保存 Operation Context</button>'+
    '<label>Requirement Policies JSON<textarea id="requirementPoliciesJson" rows="10">'+escapeHtml(JSON.stringify(policies.items||[],null,2))+'</textarea></label>'+
    '<button class="secondary full" id="saveRequirementPolicies">保存 Policy 列表</button>'+
    '<div class="flow-summary">Recommendation '+statusBadge(summary.status)+' · matched '+summary.matched+' · unknown '+summary.unknown+' · conflicts '+summary.conflicts+' · changes '+summary.changes+(summary.diverged?' · <strong>current 已与 recommendation 分叉</strong>':'')+'<pre>'+escapeHtml(diff)+'\n\n'+escapeHtml(provenance)+'</pre></div>'+
    '<div class="button-row"><button class="secondary" id="evaluateRequiredRecommendation">Evaluate Recommendation</button><button class="primary" id="adoptRequiredRecommendation" '+(summary.status!=='recommendation_ready'||summary.diverged?'disabled':'')+'>Adopt（显式更新 RequiredCNS）</button></div>';
}

//: A V3 adopted route's CNS assessment summary.  Route safety and CNS compliance are
//: reported side by side and never merged into one pass/fail badge.
export function routePlannerV3CnsSummary(flow){
  const model=routePlannerV3AdoptionModel(flow);
  const assessment=model.assessments[0]||null;
  const publication=model.adoptions.find(item=>item.status!=='revoked')||null;
  const rows=[
    ['Route validation（V3-C）',
      assessment?statusBadge(assessment.routeValidationStatus||'not_ready')+' '
        +escapeHtml(assessment.routeValidationStatus||'—')+' · unchanged '
        +escapeHtml(String(assessment.routeValidationUnchanged)):'无 V3-C validation'],
    ['Operational publication（V3-D）',
      publication?statusBadge(publication.status)+' '+escapeHtml(publication.adoptionId||'—')
        +' · route '+escapeHtml(publication.routeId||'—')+' · applicability '
        +escapeHtml(publication.currentApplicability||'—'):'未发布'],
    ['P7 geometry',stageText(assessment,'P7')],
    ['P8 capability',stageText(assessment,'P8')],
    ['P9 timeline',stageText(assessment,'P9')],
    ['P10 gap',stageText(assessment,'P10')],
    ['Assessment completeness（证据完整度）',
      assessment?statusBadge(assessment.assessmentStatus)+' '+escapeHtml(assessment.assessmentStatus):'not_started'],
    ['Requirement verdict（需求满足度）',
      assessment?statusBadge(assessment.requirementVerdict)+' '+escapeHtml(assessment.requirementVerdict):'unknown']];
  const blocking=(assessment?.blockingReasons||[]).length
    ?'<div class="parameter-note">blocking_reasons '+escapeHtml(JSON.stringify(assessment.blockingReasons))+'</div>':'';
  return '<h3>V3 adopted route · CNS Assessment Bridge '+statusBadge(assessment?assessment.assessmentStatus:'not_started')+'</h3>'
    +'<div class="parameter-note"><b>'+escapeHtml(V3D_CNS_SEPARATION_LABEL)+'</b><br>'
    +'评估完整度与需求满足度是两个独立维度：评估可以 complete 而 verdict 为 does_not_meet，'
    +'此时 V3-C 仍然是 validated_route（CNS 缺口绝不回写成 route validation failed）。'
    +'CNS 结果不进入 V3 cost/search。</div>'
    +'<div class="scroll-list route-list">'+rows.map(row=>'<div class="list-row route-row"><span><b>'
      +escapeHtml(row[0])+'</b><small>'+row[1]+'</small></span></div>').join('')+'</div>'
    +blocking
    +'<div class="button-row"><button class="primary" id="assessV3AdoptedRoute" '
    +(publication?'':'disabled')+'>运行既有 P7→P8→P9→P10 CNS 评估</button></div>'
    +'<div class="parameter-note">桥接只编排既有 P7/P8/P9/P10 服务，不复制或改写任何 CNS 公式；'
    +'stages '+escapeHtml((model.stageOrder||V3D_STAGES).join(' → '))
    +' · 缺参数/RequiredCNS/aircraft/timing 时返回 incomplete 与 blocking reason，不造默认值。</div>';
}

function stageText(assessment,stage){
  if(!assessment)return '未运行';
  const entry=(assessment.stageResults||{})[stage]||{};
  const statuses=entry.route_statuses||{};
  const detail=Object.entries(statuses).map(([routeId,status])=>routeId+':'+status).join(' · ');
  return statusBadge(entry.status||'not_run')+' '+escapeHtml(entry.status||'not_run')
    +(detail?'<br><small>'+escapeHtml(detail)+'</small>':'');
}

export function render({flow}){
  const aircraft=flow.aircraft||{},rules=flow.rules||{},catalog=flow.aircraft_profiles||{items:[]};
  const v3Panel=routePlannerV3CnsSummary(flow);
  const selectedProfile=flow.selected_aircraft_profile_id||aircraft.aircraft_id||'';
  const profileOptions='<option value="">自定义/未选择</option>'+catalog.items.map(item=>'<option value="'+escapeHtml(item.aircraft_id)+'" '+selected(item.aircraft_id,selectedProfile)+'>'+escapeHtml(item.name)+' · '+escapeHtml(item.aircraft_id)+'</option>').join('');
  const routeOptions='<option value="">不指定</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'" '+selected(route.route_id,aircraft.route_id)+'>'+route.route_id+' '+route.direction+'</option>').join('');
  const scopeOptions='<option value="project">项目默认需求</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'">航路覆盖：'+route.route_id+'</option>').join('');
  const requirements=flow.required_cns?.project_default||{},c=requirements.communication||{},n=requirements.navigation||{},s=requirements.surveillance||{};
  const profileSummary=catalog.items.filter(item=>item.aircraft_id===selectedProfile).map(capabilitySummary).join('');
  const devices=(flow.device_catalog?.items||[]).map(item=>item.subsystem+':'+(item.type?.technology||'unknown')+' / '+(item.reliability?.source||'未记录')+' / '+(item.reliability?.status||'missing_data')).join(' · ')||'未加载';
  const safetyPanel=safetyPolicyPanel(flow.safety_policy||{});
  const timingPanel=operationalTimingPanel(flow);
  const corridorPanel=corridorPolicyPanel(flow);
  const requirementPanel=requirementPolicyPanel(flow);
  const operate='<div class="demo-note">AircraftCNSProfileCatalog：'+escapeHtml(flow.aircraft_source)+' · '+catalog.count+' 条；机载能力不会自动成为任务需求</div><label>飞行器能力档案<select id="aircraftProfile">'+profileOptions+'</select></label>'+profileSummary+
    '<div class="form-grid"><label>厂家<input id="manufacturer" value="'+escapeHtml(value(aircraft,'manufacturer','工程测试厂家'))+'"></label><label>型号<input id="model" value="'+escapeHtml(value(aircraft,'model','Demo-A1'))+'"></label><label>巡航速度 m/s<input type="number" id="cruise" value="'+value(aircraft,'cruise_speed_mps',25)+'"></label><label>最大速度 m/s<input type="number" id="maximum" value="'+value(aircraft,'max_speed_mps',40)+'"></label><label>MTBF h<input type="number" id="mtbf" value="'+value(aircraft,'mtbf_h',10000)+'"></label><label>绑定航路<select id="aircraftRoute">'+routeOptions+'</select></label><label>A→B 高度 m<input type="number" id="heightAB" value="'+value(rules,'height_ab_m',120)+'"></label><label>B→A 高度 m<input type="number" id="heightBA" value="'+value(rules,'height_ba_m',150)+'"></label><label>高度模式<select id="heightMode"><option value="different">双向不同高度</option><option value="same">同高度层</option></select></label><label>水平间隔 m<input type="number" id="separation" value="'+value(rules,'horizontal_separation_m',100)+'"></label><label>感知→平台 ms<input type="number" id="delaySensor" value="'+value(rules,'delay_sensor_to_platform_ms',500)+'"></label><label>平台→航空器 ms<input type="number" id="delayCommand" value="'+value(rules,'delay_platform_to_aircraft_ms',500)+'"></label></div><label>方向规则<select id="directionRule"><option>按航向分层</option><option>同一航路仅一架</option><option>同一方向仅一架</option></select></label><button class="primary full" id="saveRules">保存并校验规则</button>'+
    (flow.aircraft?'<div class="flow-summary">Legacy λ='+(flow.aircraft.lambda_per_hour*1000000).toFixed(3)+'×10⁻⁶/h（兼容字段，非 P4 ReliabilitySpec 推断） · 总时延 '+rules.total_delay_ms+' ms · 反应距离 '+rules.reaction_distance_m+' m<br>'+statusBadge(rules.status)+' '+escapeHtml(rules.message)+'</div>':'');
  const resultPanel='<h3>Required CNS Performance</h3><label>需求作用域<select id="requiredScope">'+scopeOptions+'</select></label><div class="cns-requirements"><fieldset class="cns-requirement">'+communication(c)+'</fieldset><fieldset class="cns-requirement">'+navigation(n)+'</fieldset><fieldset class="cns-requirement">'+surveillance(s)+'</fieldset></div><button class="secondary full" id="saveRequiredCns">保存 RequiredCNS</button><div class="parameter-note">时间规范字段统一使用秒；旧毫秒/精度/更新间隔字段由兼容层同步。未知参数保持 pending_confirmation，不提供安全阈值默认值。</div>'+requirementPanel+'<div class="demo-note">ReliabilitySpec 是统计属性，不会随机决定当前服务状态；demo 与未确认参数仅作待核实输入。</div><div class="flow-summary"><strong>Ground Device Capability</strong><br>'+escapeHtml(devices)+'</div>';
  const body=wbPanel('operate',wbSection('飞行器与运行规则',operate))
    +wbPanel('result',wbSection('需求与设备能力',resultPanel))
    +wbPanel('advanced',wbSection('高级运行模型',timingPanel+renderProtectionBudget(flow.protection_envelope)+renderDaaEncounterLab(flow)+v3Panel+corridorPanel+safetyPanel))
    +'<button class="secondary full" id="nextStep" '+(!flow.steps['4']?'disabled':'')+'>下一步：CNS规划</button>';
  return shell('04','运行规则','Aircraft Capability、Required CNS Performance 与地面设备能力相互独立。',body);
}

export function bind(c){
  const flow=c.flow();if(flow.rules)c.$('heightMode').value=flow.rules.height_mode;
  bindDaaEncounterLab(c);
  c.$('aircraftProfile').onchange=event=>{const profile=flow.aircraft_profiles.items.find(item=>item.aircraft_id===event.target.value);if(!profile)return;c.$('manufacturer').value=profile.manufacturer||'';c.$('model').value=profile.model||'';for(const [id,key] of [['cruise','cruise_speed_mps'],['maximum','max_speed_mps'],['mtbf','mtbf_h']])if(profile[key]!=null)c.$(id).value=profile[key];};
  c.actionButton('saveRules',async()=>{await c.mutate('rules',{aircraft_id:c.$('aircraftProfile').value,manufacturer:c.$('manufacturer').value,model:c.$('model').value,cruise_speed:c.$('cruise').value,max_speed:c.$('maximum').value,mtbf:c.$('mtbf').value,route_id:c.$('aircraftRoute').value,height_ab:c.$('heightAB').value,height_ba:c.$('heightBA').value,height_mode:c.$('heightMode').value,horizontal_separation:c.$('separation').value,direction_rule:c.$('directionRule').value,delay_sensor:c.$('delaySensor').value,delay_command:c.$('delayCommand').value});if(c.flow().rules?.status==='passed')await c.mutate('operational');});
  const optional=id=>c.$(id).value===''?null:Number(c.$(id).value),textValue=id=>c.$(id).value.trim()||null,required=id=>({yes:true,no:false,pending:null})[c.$(id).value],interfaces=id=>(c.$(id).value||'').split(',').map(item=>item.trim()).filter(Boolean);
  const timingClone=()=>structuredClone(c.flow().operational_timing||{route_motion_profiles:{},service_scenarios:{},response_time_budgets:{},encounter_scenarios:{}});
  const operationContextFields=Object.keys(OPERATION_CONTEXT_OPTIONS);
  const fillOperationContext=()=>{const state=c.flow().cns_operation_context||{},scope=c.$('operationContextScope').value,base=state.project_default||{},override=scope==='project'?{}:(state.route_overrides?.[scope]||{});for(const name of operationContextFields)c.$('operationContext_'+name).value=(override[name]||base[name]||{}).value||'unknown';};
  c.$('operationContextScope').onchange=fillOperationContext;
  c.actionButton('saveOperationContext',()=>{const scope=c.$('operationContextScope').value,source=textValue('operationContextSource')||'user_configuration',confirmed=c.$('operationContextConfirmed').checked,context=Object.fromEntries(operationContextFields.map(name=>[name,{value:c.$('operationContext_'+name).value,source,confirmed}]));return c.resourceAction('/api/cns-operation-context',{scope:scope==='project'?'project':'route',route_id:scope==='project'?null:scope,context});});
  c.actionButton('saveRequirementPolicies',()=>c.resourceAction('/api/cns-requirement-policies',{items:JSON.parse(c.$('requirementPoliciesJson').value||'[]'),source:'user_configuration'}));
  c.actionButton('evaluateRequiredRecommendation',()=>c.resourceAction('/api/cns-required-recommendation/evaluate',{}));
  c.actionButton('adoptRequiredRecommendation',()=>c.resourceAction('/api/cns-required-recommendation/adopt',{source:'explicit_user_adoption'}));
  c.actionButton('saveServiceScenario',()=>{const timing=timingClone(),routeId=c.$('serviceScenarioRoute').value,confirmed=c.$('serviceScenarioConfirmed').checked,events=JSON.parse(c.$('serviceScenarioEvents').value||'[]').map(item=>({...item,confirmed}));timing.service_scenarios=timing.service_scenarios||{};timing.service_scenarios[routeId]={scenario_id:'scenario-'+routeId,route_id:routeId,events,source:'user_configuration',confirmed};return c.resourceAction('/api/operational-timing',{operational_timing:timing});});
  c.actionButton('saveResponseBudget',()=>{const timing=timingClone(),confirmed=c.$('rtConfirmed').checked,source=textValue('rtSource')||'user_configuration',ids={detect:'rtDetect',track:'rtTrack',processing:'rtProcessing',decision:'rtDecision',communication:'rtCommunication',aircraft_reaction:'rtReaction'};timing.response_time_budgets=timing.response_time_budgets||{};timing.response_time_budgets['default-response']={budget_id:'default-response',source,confirmed,components:Object.fromEntries(Object.entries(ids).map(([name,id])=>[name,{value_s:optional(id),source,confirmed}]))};return c.resourceAction('/api/operational-timing',{operational_timing:timing});});
  c.actionButton('saveEncounterScenario',()=>{const timing=timingClone(),confirmed=c.$('encounterConfirmed').checked;timing.encounter_scenarios=timing.encounter_scenarios||{};timing.encounter_scenarios['default-encounter']={encounter_id:'default-encounter',relative_closing_speed_mps:optional('encounterRelativeSpeed'),maneuver_distance_m:optional('encounterManeuver'),uncertainty_distance_m:optional('encounterUncertainty'),source:textValue('encounterSource')||'user_configuration',confirmed};return c.resourceAction('/api/operational-timing',{operational_timing:timing});});
  c.actionButton('evaluateProtectionBudget',()=>c.resourceAction('/api/protection-envelope/evaluate',{}));
  // The bridge only orchestrates the existing P7-P10 services; it never reimplements them.
  if(c.$('assessV3AdoptedRoute'))c.actionButton('assessV3AdoptedRoute',()=>c.resourceAction('/api/v3-cns-assessment/evaluate',{}));
  c.actionButton('saveCorridorPolicy',()=>{const policy=structuredClone(c.flow().cns_corridor_policy||{routes:{}}),routeId=c.$('corridorRoute').value;policy.routes=policy.routes||{};policy.routes[routeId]={route_id:routeId,horizontal_half_width_m:optional('corridorHalfWidth'),vertical_lower_margin_m:optional('corridorLower'),vertical_upper_margin_m:optional('corridorUpper'),source:textValue('corridorSource')||'user_configuration',confirmed:c.$('corridorConfirmed').checked};return c.resourceAction('/api/cns-service-corridor/evaluate',{cns_corridor_policy:policy});});
  c.actionButton('saveRequiredCns',()=>{
    const selectedScope=c.$('requiredScope').value;
    const requirements=withLegacyRequiredAliases({
      communication:{required:required('cRequired'),coverage_requirement:optional('cCoverage'),max_gap_m:optional('cMaxGap'),type:{service_type:textValue('cServiceType'),technology:c.$('cTechnology').value,network_scope:c.$('cNetworkScope').value,interfaces:interfaces('cInterfaces')},performance:{max_latency_s:optional('cMaxLatency'),lost_link_threshold_s:optional('cLostLink'),max_continuous_outage_s:optional('cContinuousOutage'),max_cumulative_outage_s:optional('cCumulativeOutage'),min_availability:optional('cAvailability'),min_redundancy:optional('cRedundancy')},contingency:textValue('cContingency'),source:textValue('cSource'),confirmed:c.$('cConfirmed').checked},
      navigation:{required:required('nRequired'),coverage_requirement:optional('nCoverage'),type:{technology:c.$('nTechnology').value},performance:{max_horizontal_error_m:optional('nHorizontalError'),max_vertical_error_m:optional('nVerticalError'),integrity_required:textValue('nIntegrity'),max_time_to_alert_s:optional('nTimeToAlert'),max_degradation_time_s:optional('nDegradation'),min_availability:optional('nAvailability'),min_redundancy:optional('nRedundancy')},contingency:textValue('nContingency'),source:textValue('nSource'),confirmed:c.$('nConfirmed').checked},
      surveillance:{required:required('sRequired'),coverage_requirement:optional('sCoverage'),type:{target_cooperation:textValue('sTargetCooperation'),sensor_mode:textValue('sSensorMode'),technology:c.$('sTechnology').value},performance:{min_detection_range_m:optional('sDetectionRange'),min_detection_probability:optional('sDetectionProbability'),max_update_interval_s:optional('sMaxUpdate'),max_track_loss_s:optional('sTrackLoss'),max_alert_latency_s:optional('sAlertLatency'),min_availability:optional('sAvailability'),min_redundancy:optional('sRedundancy')},contingency:textValue('sContingency'),source:textValue('sSource'),confirmed:c.$('sConfirmed').checked},
    });
    return c.mutate('required-cns',{scope:selectedScope==='project'?'project':'route',route_id:selectedScope==='project'?null:selectedScope,requirements});
  });
  c.actionButton('saveSafetyPolicy',()=>{
    const policy=structuredClone(c.flow().safety_policy||{});
    policy.source=c.$('safetyPolicySource').value.trim()||'project_template';
    policy.confirmed=c.$('safetyPolicyConfirmed').checked;
    policy.status=policy.confirmed?'passed':'pending_confirmation';
    return c.resourceAction('/api/cns/safety-policy',{safety_policy:policy});
  });
  c.actionButton('previewSafetyEvent',async()=>{
    const policy=c.flow().safety_policy||{};
    const fc=(policy.failure_conditions||[]).find(item=>item.failure_condition_id===c.$('safetyFailureCondition').value);
    const ue=(policy.unacceptable_events||[]).find(item=>(item.failure_condition_refs||[]).includes(fc?.failure_condition_id));
    const result=await c.computeAction('/api/cns/events/evaluate',{failure_condition:fc,unacceptable_event:ue,service_state:{service_state:c.$('safetyServiceState').value},operational_context:{subsystem:fc?.subsystem}});
    c.$('safetyEventPreview').textContent='FailureCondition: '+(result.failure_condition?.status||'unknown')+' · UnacceptableEvent: '+(result.unacceptable_event?.status||'unknown')+' · severity '+(result.unacceptable_event?.severity||'unknown');
  });
  const couplingInput=()=>{
    const policy=c.flow().safety_policy||{},condition=(policy.coupled_conditions||[]).find(item=>item.coupled_condition_id===c.$('coupledCondition').value);
    const dependency=(policy.functional_dependencies||[]).find(item=>item.dependency_id===condition?.functional_dependency_ref);
    const coupledUe=(policy.coupled_unacceptable_events||[]).find(item=>(item.coupled_condition_refs||[]).includes(condition?.coupled_condition_id));
    return {condition,dependency,coupledUe};
  };
  if(c.$('coupledCondition'))c.$('coupledCondition').onchange=()=>{
    const {dependency}=couplingInput();
    const observations=(dependency?.stages||[]).map((stage,index)=>({ref:stage.event_ref,subsystem:stage.subsystem,status:'triggered',start_s:index*2,end_s:index*2+1,source:'manual_preview'}));
    c.$('coupledObservations').value=JSON.stringify(observations,null,2);
  };
  c.actionButton('previewCoupledEvent',async()=>{
    const {condition,dependency,coupledUe}=couplingInput();
    const observations=JSON.parse(c.$('coupledObservations').value||'[]'),operationalContext=JSON.parse(c.$('coupledOperationalContext').value||'{}');
    const result=await c.computeAction('/api/cns/coupled-events/evaluate',{functional_dependency:dependency,coupled_condition:condition,coupled_unacceptable_event:coupledUe,observations,operational_context:operationalContext});
    c.$('coupledEventPreview').textContent='CoupledCondition: '+(result.coupled_condition?.status||'unknown')+' · CoupledUE: '+(result.coupled_unacceptable_event?.status||'unknown')+' · probability '+(result.probability_status||'not_calculated');
  });
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(5);
}
