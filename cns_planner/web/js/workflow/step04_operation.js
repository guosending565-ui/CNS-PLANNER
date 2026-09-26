import {
  escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbSegHint,wbDisclosure,wbLine,
  blockerList,nextStepBar,primaryAction,advancedAuditNote,sourceStateText,emptyReasonText,
  workflowStatusText,
} from './common.js';
import {renderProtectionBudget} from './protection_budget.js';
import {renderDaaEncounterLab,bindDaaEncounterLab} from './daa_encounter_lab.js';
import {
  V3D_CNS_SEPARATION_LABEL, V3D_STAGES, routePlannerV3AdoptionModel,
} from './step03_routes.js';

// 二级任务分段：id 稳定（run-*），标签是第一视觉层的业务语言。
// 与 Step05 使用同一套 wbPanel/wbBlock/wbSegHint 机制：同一时刻只显示一个任务。
const OPERATE_SEGMENTS=[
  ['run-op-aircraft','飞行器能力'],
  ['run-op-rules','飞行规则']
];
const RESULT_SEGMENTS=[
  ['run-res-required','CNS需求'],
  ['run-res-recommend','需求建议'],
  ['run-res-corridor','服务走廊']
];
const ADVANCED_SEGMENTS=[
  ['run-adv-timing','时间与场景'],
  ['run-adv-safety','安全与耦合'],
  ['run-adv-v3','V3 CNS评估']
];

// ---- 六区结构与三层独立性（B4X §4 / §19） -----------------------------------
//
// 本步骤的六区语义（目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步）
// 落在**既有**的一级标签与二级分段内部：一级 operate/result/advanced 与全部
// `run-*` 分段 id 保持不变，六区只由分段内的区块顺序与中文取词表达。
//  - 每个二级分段最多一个 primary 按钮（主操作），其余一律 secondary 或留在高级区；
//  - 阻塞项 / 工程假设一律经 blockerList()；空状态给明确说明，绝不写成"通过"；
//  - 开发阶段编号（P7/P8/P9/P10 等）与版本代号只允许出现在 advanced 标签，
//    并且包进 advancedAuditNote()。
/** 六区小标题：只做展示分组，不新增 DOM id，也不改变任何控件的分段归属。 */
const zone=(title,body)=>'<h4>'+escapeHtml(title)+'</h4>'+(body||'');
/** 数值 + 单位的可读文本；缺失时明确写"未配置"，不显示 undefined/null。 */
const unitValue=(current,unit)=>current===null||current===undefined||current===''?'未配置':current+unit;

/**
 * 采用状态的 raw → raw 归一：`adopted_current` 是后端"当前仍然有效的采用"。
 * 这里**只做取值归一**，中文一律由 presentation.js 的 statusText() 提供
 * （not_adopted → 尚未采用、adopted → 已采用、superseded → 已被取代），
 * 不新增第二套中文词表。
 */
const adoptionKey=raw=>raw==='adopted_current'?'adopted':(raw||'not_adopted');
/** 当前需求建议的采用状态（只读，不改变任何后端判定或 gate）。 */
function requirementAdoptionKey(flow){
  const recommendation=flow?.required_cns_recommendation||{};
  return adoptionKey(recommendation.adoption_status||flow?.required_cns_adoption?.status||'not_adopted');
}
/** 采用依据：说明"正式 CNS需求"当前是怎么来的；不显示 fingerprint 等开发标识。 */
function adoptionBasisText(key){
  if(key==='adopted')return '正式 CNS需求由用户在需求建议中显式采用写入。';
  if(key==='superseded')return '此前的采用记录已被手工直接编辑正式需求取代，需重新评估并重新采用。';
  return '正式 CNS需求当前来自用户手工配置或工程默认值，尚未采用任何需求建议。';
}

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
function confirmation(prefix,data){return input(prefix+'Source','来源',data.source,'text')+'<label class="check-row"><input type="checkbox" id="'+prefix+'Confirmed" '+(data.confirmed?'checked':'')+'>参数已确认</label>'+input(prefix+'Contingency','应急降级方案',typeof data.contingency==='string'?data.contingency:'','text');}

function communication(data){const type=data.type||{},p=data.performance||{};return header('c','通信',data)+input('cServiceType','服务类型',type.service_type,'text')+selectInput('cTechnology','通信技术',C_TECH,type.technology||'unknown')+selectInput('cNetworkScope','网络范围',C_SCOPE,type.network_scope||'unknown')+input('cInterfaces','接口（逗号分隔）',(type.interfaces||[]).join(', '),'text')+input('cCoverage','覆盖要求 %',data.coverage_requirement)+input('cMaxGap','最大缺口 m',data.max_gap_m)+input('cMaxLatency','最大时延 s',p.max_latency_s)+input('cLostLink','失链阈值 s',p.lost_link_threshold_s)+input('cContinuousOutage','最大连续中断 s',p.max_continuous_outage_s)+input('cCumulativeOutage','最大累计中断 s',p.max_cumulative_outage_s)+input('cAvailability','最小可用度 0..1',p.min_availability)+input('cRedundancy','最小冗余',p.min_redundancy)+confirmation('c',data);}
function navigation(data){const type=data.type||{},p=data.performance||{};return header('n','导航',data)+selectInput('nTechnology','导航技术',N_TECH,type.technology||'unknown')+input('nCoverage','覆盖要求 %',data.coverage_requirement)+input('nHorizontalError','最大水平误差 m',p.max_horizontal_error_m)+input('nVerticalError','最大垂直误差 m',p.max_vertical_error_m)+input('nIntegrity','完整性要求',p.integrity_required,'text')+input('nTimeToAlert','最大告警时间 s',p.max_time_to_alert_s)+input('nDegradation','最大降级时间 s',p.max_degradation_time_s)+input('nAvailability','最小可用度 0..1',p.min_availability)+input('nRedundancy','最小冗余',p.min_redundancy)+confirmation('n',data);}
function surveillance(data){const type=data.type||{},p=data.performance||{};return header('s','监视',data)+selectInput('sTargetCooperation','目标协作能力',S_COOP,type.target_cooperation,true)+selectInput('sSensorMode','传感器模式',S_MODE,type.sensor_mode,true)+selectInput('sTechnology','监视技术',S_TECH,type.technology||'unknown')+input('sCoverage','覆盖要求 %',data.coverage_requirement)+input('sDetectionRange','最小探测距离 m',p.min_detection_range_m)+input('sDetectionProbability','最小探测概率 0..1',p.min_detection_probability)+input('sMaxUpdate','最大更新间隔 s',p.max_update_interval_s)+input('sTrackLoss','最大航迹丢失 s',p.max_track_loss_s)+input('sAlertLatency','最大告警时延 s',p.max_alert_latency_s)+input('sAvailability','最小可用度 0..1',p.min_availability)+input('sRedundancy','最小冗余',p.min_redundancy)+confirmation('s',data);}

export function withLegacyRequiredAliases(requirements){
  const result=structuredClone(requirements),c=result.communication,n=result.navigation,s=result.surveillance;
  c.latency_ms=c.performance.max_latency_s==null?null:c.performance.max_latency_s*1000;c.redundancy=c.performance.min_redundancy;
  n.accuracy_m=n.performance.max_horizontal_error_m;n.integrity=n.performance.integrity_required;n.redundancy=n.performance.min_redundancy;
  s.update_interval_s=s.performance.max_update_interval_s;s.redundancy=s.performance.min_redundancy;
  return result;
}

function capabilitySummary(item){
  // 航空器配置是**工程规划基线**：这里只转印档案里已有的字段，全部状态 raw enum
  // 经 presentation.js 的 statusText() 取词，绝不手写第二套中文判断。
  const pairs=object=>Object.entries(object||{}).filter(([,current])=>current!==null&&current!==undefined&&current!==''&&(!Array.isArray(current)||current.length)).map(([key,current])=>key+'='+([].concat(current).join('|'))).join('；')||'待确认';
  const reliability=data=>{const current=data?.reliability||{};return '模型 '+statusText(current.model||'unknown')+'，MTBF '+unitValue(current.mtbf_h,' h')+'，可用度 '+unitValue(current.availability,'')+'，来源 '+(current.source||'未记录')+'，状态 '+statusText(current.status||'missing_data');};
  const summary=(label,data)=>label+'：类型['+pairs(data?.type)+'] 性能['+pairs(data?.performance)+'] 能力['+((data?.capabilities||[]).join('、')||'待确认')+'] 应急降级方案['+(typeof data?.contingency==='string'?data.contingency:'待确认')+'] 备用方案 '+(data?.fallbacks?.length||0)+' 项 可靠性['+reliability(data)+'] 确认状态 '+statusText(data?.confirmed?'confirmed':'pending_confirmation');
  return '<div class="flow-summary"><strong>航空器配置（通用工程航空器配置）</strong><br>工程规划基线，不代表具体机型实测参数。<br>'+escapeHtml(summary('通信',item.communication))+'<br>'+escapeHtml(summary('导航',item.navigation))+'<br>'+escapeHtml(summary('监视',item.surveillance))+'</div>';
}

function safetyPolicyPanel(policy={}){
  const failureConditions=policy.failure_conditions||[],unacceptableEvents=policy.unacceptable_events||[];
  const dependencies=policy.functional_dependencies||[],coupledConditions=policy.coupled_conditions||[];
  const fcOptions=failureConditions.map(item=>'<option value="'+escapeHtml(item.failure_condition_id)+'">'+escapeHtml(item.failure_condition_id+' · '+item.subsystem+' · '+item.failure_mode)+'</option>').join('');
  const coupledOptions=coupledConditions.map(item=>'<option value="'+escapeHtml(item.coupled_condition_id)+'">'+escapeHtml(item.coupled_condition_id+' · '+item.logic)+'</option>').join('');
  const records=failureConditions.map(item=>item.failure_condition_id+'：严重度 '+statusText(item.severity)+'，状态 '+statusText(item.status)).join(' · ')||'无';
  const firstDependency=dependencies.find(item=>item.dependency_id===coupledConditions[0]?.functional_dependency_ref);
  const previewObservations=(firstDependency?.stages||[]).map((stage,index)=>({ref:stage.event_ref,subsystem:stage.subsystem,status:'triggered',start_s:index*2,end_s:index*2+1,source:'manual_preview'}));
  return '<h3>Safety Assessment Policy</h3>'+
    '<div class="demo-note">ARP4761A/FAA-inspired engineering assessment，仅用于工程分析，不是认证结论。ServiceState、FailureCondition 与 UnacceptableEvent 为不同层级；服务 lost 不会自动成为 unacceptable/catastrophic。</div>'+
    '<div class="flow-summary">'+statusBadge(policy.status||'pending_confirmation')+
    ' 来源='+escapeHtml(policy.source||'未记录')+' · FC '+failureConditions.length+
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
    advancedAuditNote('C+S / C+N / N+S 模板仅为未确认研究假设，必须按运行条件确认；P6 不计算耦合概率，也不假设各分系统独立。')+
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
    advancedAuditNote('P8 静态 capability 不会自动转为 P4 available；只有 confirmed ServiceScenarioEvent 才产生运行状态。')+
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
  const corridorBlockers=routes.length?[]:[{kind:'blocker',text:'当前没有运行航路：'+sourceStateText('not_configured')+'，无法定义服务走廊。'}];
  return '<h3>CNS 服务需求走廊</h3>'
    +zone('本段目标','<div class="demo-note">工程 CNS 服务需求走廊：不等同 JARUS Operational Volume、U-space Surveillance Volume 或法规批准空间。水平半宽与垂向余量必须显式确认，系统不会给出默认宽度。</div>')
    +zone('输入准备',selectInput('corridorRoute','运行航路',routes.map(item=>item.route_id),first)+
      '<div class="form-grid">'+input('corridorHalfWidth','水平半宽 m',spec.horizontal_half_width_m)+input('corridorLower','下垂向余量 m',spec.vertical_lower_margin_m)+input('corridorUpper','上垂向余量 m',spec.vertical_upper_margin_m)+input('corridorSource','来源',spec.source,'text')+'<label class="check-row"><input type="checkbox" id="corridorConfirmed" '+(spec.confirmed?'checked':'')+'>走廊参数已确认</label></div>')
    +zone('阻塞项与工程假设',blockerList(corridorBlockers,'当前没有阻塞项。走廊参数未确认前只作为草稿保存，不参与能力缺口评估。'))
    +zone('主操作',primaryAction('<button class="primary full" id="saveCorridorPolicy" '+(!routeOptions?'disabled':'')+'>保存并评估服务走廊</button>',{note:'保存后立即评估该航路的 CNS 服务需求走廊。'}));
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
  const diff=(recommendation.current_vs_recommended_diff||[]).slice(0,12).map(item=>item.field_path+'：'+JSON.stringify(item.current)+' → '+JSON.stringify(item.recommended)).join('\n')||'暂无可显示的差异';
  const provenance=Object.entries(recommendation.field_provenance||{}).slice(0,10).map(([field,items])=>field+' ← '+items.map(item=>item.policy_id+'（'+(item.reference||'未登记引用')+(item.clause?' §'+item.clause:'')+'）').join('、')).join('\n')||'尚无字段来源追溯记录';
  const adoption=requirementAdoptionKey(flow);
  const blockers=[
    ...(recommendation.status==='conflict'||summary.conflicts?[{kind:'blocker',text:'需求政策之间存在冲突（'+summary.conflicts+' 条）：冲突未解决前不能采用建议。'}]:[]),
    ...(summary.diverged?[{kind:'blocker',text:'正式 CNS需求已与当前建议分叉：需重新评估后再决定是否采用。'}]:[]),
    ...(adoption==='not_adopted'?[{kind:'assumption',text:'正式 CNS需求尚未采用任何需求建议。',detail:adoptionBasisText('not_adopted')}]:[]),
    ...(adoption==='superseded'?[{kind:'assumption',text:'此前的采用记录已被取代。',detail:adoptionBasisText('superseded')}]:[]),
    ...(summary.unknown?[{kind:'assumption',text:'有 '+summary.unknown+' 条需求政策因证据不足未匹配，建议可能不完整。'}]:[])
  ];
  return '<h3>运行上下文 → 正式 CNS需求建议</h3>'
    +zone('本段目标','<div class="demo-note">需求建议是<strong>候选结论</strong>，不是正式需求。运行上下文只通过已确认、可追溯的需求政策生成建议：不自动判断法规合规，也不会自动修改正式 CNS需求。</div>')
    +zone('输入准备','<label>上下文作用域<select id="operationContextScope">'+scopeOptions+'</select></label><div class="form-grid">'+fields+'<label>上下文来源<input type="text" id="operationContextSource" value="" placeholder="留空表示按人工录入处理"></label><label class="check-row"><input type="checkbox" id="operationContextConfirmed">字段均已确认</label></div>'
      +'<button class="secondary full" id="saveOperationContext">保存运行上下文</button>'
      // 需求政策 JSON 与建议差异 / 字段来源追溯是工程输入与证据，
      // 默认收进 disclosure 以减少第一视觉层占用；控件仍挂载在 DOM 中且可编辑。
      +wbDisclosure('需求政策 JSON（工程输入，默认收起）',
        '<label>需求政策 JSON<textarea id="requirementPoliciesJson" rows="10">'+escapeHtml(JSON.stringify(policies.items||[],null,2))+'</textarea></label>'+
        '<button class="secondary full" id="saveRequirementPolicies">保存需求政策列表</button>'))
    +zone('阻塞项与工程假设',blockerList(blockers,'当前没有阻塞项。需求建议仍需用户显式采用后才会成为正式 CNS需求。'))
    +zone('结果','<div class="flow-summary">需求建议 '+statusBadge(summary.status)+' · 已匹配政策 '+summary.matched+' · 证据不足未匹配 '+summary.unknown+' · 冲突 '+summary.conflicts+' · 与正式需求差异 '+summary.changes+(summary.diverged?' · <strong>正式需求已与建议分叉</strong>':'')
      +'<br>采用状态 '+statusBadge(adoption)+' · '+escapeHtml(adoptionBasisText(adoption))
      +wbDisclosure('建议差异 / 字段来源追溯','<pre>'+escapeHtml(diff)+'\n\n'+escapeHtml(provenance)+'</pre>')
      +'</div>')
    +zone('主操作',primaryAction('<button class="secondary" id="evaluateRequiredRecommendation">重新评估需求建议</button><button class="primary" id="adoptRequiredRecommendation" '+(summary.status!=='recommendation_ready'||summary.diverged?'disabled':'')+'>采用为正式 CNS需求</button>',
      {note:'需求建议不会自动成为正式 CNS需求：只有点击「采用为正式 CNS需求」后，系统才会把当前建议写入正式需求；重新评估建议不会改动正式需求。'}));
}

//: A V3 adopted route's CNS assessment summary.  Route safety and CNS compliance are
//: reported side by side and never merged into one pass/fail badge.
//: 本分段位于 advanced 标签，是**研究对照 / 高级**信息：开发阶段编号只在这里出现，
//: 并统一包进 advancedAuditNote()；评估结果不参与正式 CNS需求的采用。
export function routePlannerV3CnsSummary(flow){
  const model=routePlannerV3AdoptionModel(flow);
  const assessment=model.assessments[0]||null;
  const publication=model.adoptions.find(item=>item.status!=='revoked')||null;
  const rows=[
    ['Route validation（V3-C）',
      assessment?statusBadge(assessment.routeValidationStatus||'not_ready')+' '
        +escapeHtml(statusText(assessment.routeValidationStatus||'—'))+' · 与上次结果一致：'
        +(assessment.routeValidationUnchanged?'是':'否'):'无 V3-C validation'],
    ['Operational publication（V3-D）',
      publication?statusBadge(publication.status)+' '+escapeHtml(publication.adoptionId||'—')
        +' · 航路 '+escapeHtml(publication.routeId||'—')+' · 适用性 '
        +escapeHtml(statusText(publication.currentApplicability||'—')):'未发布'],
    ['Assessment completeness（证据完整度）',
      assessment?statusBadge(assessment.assessmentStatus)+' '+escapeHtml(statusText(assessment.assessmentStatus)):'not_started'],
    ['Requirement verdict（需求满足度）',
      assessment?statusBadge(assessment.requirementVerdict)+' '+escapeHtml(statusText(assessment.requirementVerdict)):'unknown']];
  const stageRows=[
    ['P7 geometry',stageText(assessment,'P7')],
    ['P8 capability',stageText(assessment,'P8')],
    ['P9 timeline',stageText(assessment,'P9')],
    ['P10 gap',stageText(assessment,'P10')]];
  const blocking=(assessment?.blockingReasons||[]).length
    ?'<div class="parameter-note">阻塞原因（blocking_reasons）'+escapeHtml(JSON.stringify(assessment.blockingReasons))+'</div>':'';
  const rowList=items=>'<div class="scroll-list route-list">'+items.map(row=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(row[0])+'</b><small>'+row[1]+'</small></span></div>').join('')+'</div>';
  return '<h3>V3 adopted route · CNS Assessment Bridge '+statusBadge(assessment?assessment.assessmentStatus:'not_started')+'</h3>'
    +advancedAuditNote('本分段是研究对照 / 高级信息：P7 / P8 / P9 / P10 属开发阶段编号，只在本高级 / 审计区出现；V3 CNS 评估不参与正式 CNS需求的采用，评估结果也不会写入正式需求。')
    +'<div class="parameter-note"><b>'+escapeHtml(V3D_CNS_SEPARATION_LABEL)+'</b><br>'
    +'评估完整度与需求满足度是两个独立维度：评估可以 complete 而 verdict 为 does_not_meet，'
    +'此时 V3-C 仍然是 validated_route（CNS 缺口绝不回写成 route validation failed）。'
    +'CNS 结果不进入 V3 cost/search。</div>'
    +rowList(rows)
    +advancedAuditNote('开发阶段明细（研究对照 / 高级）：P7 geometry · P8 capability · P9 timeline · P10 gap')
    +rowList(stageRows)
    +blocking
    +'<div class="parameter-note">历史 V3 CNS 评估仅供只读审计；本页面不再提供重新运行入口。'
    +'stages '+escapeHtml((model.stageOrder||V3D_STAGES).join(' → '))
    +'。</div>';
}

function stageText(assessment,stage){
  if(!assessment)return '未运行';
  const entry=(assessment.stageResults||{})[stage]||{};
  const statuses=entry.route_statuses||{};
  const detail=Object.entries(statuses).map(([routeId,status])=>routeId+'：'+statusText(status)).join(' · ');
  return statusBadge(entry.status||'not_run')+' '+escapeHtml(statusText(entry.status||'not_run'))
    +(detail?'<br><small>'+escapeHtml(detail)+'</small>':'');
}

export function render({flow}){
  const aircraft=flow.aircraft||{},rules=flow.rules||{},catalog=flow.aircraft_profiles||{items:[]};
  const v3Panel=routePlannerV3CnsSummary(flow);
  const selectedProfile=flow.selected_aircraft_profile_id||aircraft.aircraft_id||'';
  const profileOptions='<option value="">自定义/未选择</option>'+catalog.items.map(item=>'<option value="'+escapeHtml(item.aircraft_id)+'" '+selected(item.aircraft_id,selectedProfile)+'>'+escapeHtml(item.name)+' · '+escapeHtml(item.aircraft_id)+'</option>').join('');
  const routeOptions='<option value="">不指定</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'" '+selected(route.route_id,aircraft.route_id)+'>'+route.route_id+' '+route.direction+'</option>').join('');
  const scopeOptions='<option value="project">项目默认需求</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'">航路覆盖：'+route.route_id+'</option>').join('');
  const requiredCns=flow.required_cns||{},requirements=requiredCns.project_default||{},c=requirements.communication||{},n=requirements.navigation||{},s=requirements.surveillance||{};
  const selectedProfileItem=catalog.items.find(item=>item.aircraft_id===selectedProfile)||null;
  const profileSummary=selectedProfileItem?capabilitySummary(selectedProfileItem):'';
  const deviceItems=flow.device_catalog?.items||[];
  const devices=deviceItems.map(item=>item.subsystem+'：'+(item.type?.technology||statusText('unknown'))+' / '+(item.reliability?.source||'未记录')+' / '+statusText(item.reliability?.status||'missing_data')).join(' · ')||emptyReasonText('not_configured','设备目录尚无条目');
  const safetyPanel=safetyPolicyPanel(flow.safety_policy||{});
  const timingPanel=operationalTimingPanel(flow);
  const corridorPanel=corridorPolicyPanel(flow);
  const requirementPanel=requirementPolicyPanel(flow);

  // ---- 三层独立与需求链路（结果标签第一屏逐项展示，绝不合并成单一结论） --------
  // 航空器配置 ≠ 运行约束 ≠ 地面设备能力；CNS需求建议 ≠ 正式 CNS需求。
  const recommendation=flow.required_cns_recommendation||{},recommendationSummary=requirementRecommendationSummary(recommendation);
  const adoption=requirementAdoptionKey(flow);
  const aircraftDisplay=selectedProfileItem
    ?(selectedProfileItem.name+' · '+selectedProfileItem.aircraft_id)
    :(selectedProfile?'已选档案：'+selectedProfile:'通用工程航空器配置');
  const layerPanel=wbBlock('三层独立与需求链路',
    '<div class="demo-note">航空器配置、运行约束与地面设备能力是三个互相独立的层级；CNS需求建议与正式 CNS需求也是两件不同的事——正式需求只有在用户显式采用建议（或手工配置）之后才会来自建议。三层各自独立成段，不互相推断，也不合并成单一结论。</div>'
    +wbLine('航空器配置：'+aircraftDisplay,'','工程规划基线，不代表具体机型实测参数；机载能力不会自动成为任务需求。')
    +wbLine('运行约束：高度 A→B '+unitValue(rules.height_ab_m,' m')+' · B→A '+unitValue(rules.height_ba_m,' m')+' · 水平间隔 '+unitValue(rules.horizontal_separation_m,' m'),'','规则校验 '+statusText(rules.status||'not_calculated'))
    +wbLine('需求来源：'+(requiredCns.source||'未记录'),'','正式需求的来源记录在正式需求侧；建议的来源与采用依据单独记录在采用状态中。')
    +wbLine('CNS需求建议：'+statusText(recommendationSummary.status),'','已匹配政策 '+recommendationSummary.matched+' · 证据不足未匹配 '+recommendationSummary.unknown+' · 冲突 '+recommendationSummary.conflicts+' · 与正式需求差异 '+recommendationSummary.changes)
    +wbLine('正式 CNS需求：'+statusText(requiredCns.status||'pending_confirmation'),'','只有用户手工配置或显式采用需求建议后才会变化。')
    +wbLine('采用状态：'+statusText(adoption),'',adoptionBasisText(adoption)));
  // ---- 操作：航空器配置（输入准备） / 运行约束（输入准备 + 主操作） -----------
  const aircraftBlockers=[
    ...(catalog.items.length?[]:[{kind:'blocker',text:'航空器能力档案目录为空：'+sourceStateText('not_configured')+'，本段只能使用手工填写的工程默认值。'}]),
    ...(selectedProfile?[]:[{kind:'assumption',text:'尚未选择航空器能力档案，当前显示的是通用工程航空器配置。',detail:'工程规划基线，不代表具体机型实测参数；选择档案后才会载入对应条目。'}])
  ];
  const aircraftPanel='<h3>通用工程航空器配置</h3>'+
    zone('本段目标','<div class="demo-note">确定本次规划的<strong>航空器配置</strong>（机载通信 / 导航 / 监视能力）。航空器配置是工程规划基线，不代表具体机型实测参数；机载能力不会自动成为任务需求，也不等同于运行约束或地面设备能力。</div>')+
    zone('输入准备','<div class="demo-note">航空器能力档案目录：来源 '+escapeHtml(String(flow.aircraft_source||'未记录'))+' · '+numberValue(catalog.count??catalog.items.length)+' 条</div><label>航空器能力档案<select id="aircraftProfile">'+profileOptions+'</select></label>'+
      '<div class="form-grid"><label>厂家<input id="manufacturer" value="'+escapeHtml(value(aircraft,'manufacturer','工程测试厂家'))+'"></label><label>型号<input id="model" value="'+escapeHtml(value(aircraft,'model','Demo-A1'))+'"></label><label>巡航速度 m/s<input type="number" id="cruise" value="'+value(aircraft,'cruise_speed_mps',25)+'"></label><label>最大速度 m/s<input type="number" id="maximum" value="'+value(aircraft,'max_speed_mps',40)+'"></label><label>MTBF h<input type="number" id="mtbf" value="'+value(aircraft,'mtbf_h',10000)+'"></label></div>')+
    zone('阻塞项与工程假设',blockerList(aircraftBlockers,'当前没有阻塞项。航空器配置为工程规划基线，仍需工程确认后才代表具体机型实测参数。'))+
    zone('结果',profileSummary||emptyReasonText('no_result','尚未选择或载入航空器能力档案'));
  const rulesBlockers=[
    ...(flow.aircraft?[]:[{kind:'blocker',text:'尚无航空器配置：运行规则的指令时延与反应距离无法校验。'}]),
    ...(rules.status==='passed'?[]:[{kind:'assumption',text:'运行规则当前状态：'+statusText(rules.status||'not_calculated')+'。',detail:'保存并校验通过后，本步骤才具备进入下一步的规则前提。'}])
  ];
  const rulesSummary=flow.aircraft
    ?'<div class="flow-summary">规则校验 '+statusBadge(rules.status||'not_calculated')+' '+escapeHtml(rules.message||'')+'<br>总时延 '+numberValue(rules.total_delay_ms)+' ms · 反应距离 '+numberValue(rules.reaction_distance_m)+' m<br>兼容字段（旧版 λ 到达率假设）：'+(Number(flow.aircraft.lambda_per_hour||0)*1000000).toFixed(3)+'×10⁻⁶/h —— 该字段仅为历史兼容保留，不是可靠性实测参数，也不参与正式 CNS需求推断。</div>'
    :emptyReasonText('not_calculated','尚未保存并校验运行规则');
  const rulesPanel='<h3>运行约束（飞行规则）</h3>'+
    zone('本段目标','<div class="demo-note">确定本次规划的<strong>运行约束</strong>：高度、水平间隔、方向规则与指令时延。运行约束独立于航空器配置与正式 CNS需求，不把机载能力当成任务需求。</div>')+
    zone('输入准备','<div class="form-grid"><label>绑定航路<select id="aircraftRoute">'+routeOptions+'</select></label><label>A→B 高度 m<input type="number" id="heightAB" value="'+value(rules,'height_ab_m',120)+'"></label><label>B→A 高度 m<input type="number" id="heightBA" value="'+value(rules,'height_ba_m',150)+'"></label><label>高度模式<select id="heightMode"><option value="different">双向不同高度</option><option value="same">同高度层</option></select></label><label>水平间隔 m<input type="number" id="separation" value="'+value(rules,'horizontal_separation_m',100)+'"></label><label>感知→平台 ms<input type="number" id="delaySensor" value="'+value(rules,'delay_sensor_to_platform_ms',500)+'"></label><label>平台→航空器 ms<input type="number" id="delayCommand" value="'+value(rules,'delay_platform_to_aircraft_ms',500)+'"></label></div><label>方向规则<select id="directionRule"><option>按航向分层</option><option>同一航路仅一架</option><option>同一方向仅一架</option></select></label>')+
    zone('阻塞项与工程假设',blockerList(rulesBlockers,'当前没有阻塞项。运行规则参数仍以工程输入为准，校验结论见下方结果区。'))+
    zone('主操作',primaryAction('<button class="primary full" id="saveRules">保存并校验运行规则</button>',{note:'保存后立即校验高度、水平间隔、方向规则与指令时延，并据此更新推进状态。'}))+
    zone('结果',rulesSummary);
  // ---- 结果：正式 CNS需求 / 需求建议 / 服务走廊 -------------------------------
  // 航空器配置（操作标签）≠ 运行约束（操作标签）≠ 地面设备能力（本标签）：
  // 三者各自独立成段，不合并成单一结论，也不互相推断。
  const requiredCnsBlockers=[
    ...(requiredCns.status==='passed'?[]:[{kind:'assumption',text:'正式 CNS需求当前状态：'+statusText(requiredCns.status||'pending_confirmation')+'。',detail:'未确认的参数保持待确认，系统不提供安全阈值默认值。'}])
  ];
  const requiredCnsPanel='<h3>正式 CNS需求性能要求</h3>'+
    zone('本段目标','<div class="demo-note">确定<strong>正式 CNS需求</strong>：这是任务对 CNS 的性能要求，既不是机载能力，也不是地面设备能力。正式需求只能由用户手工配置，或经「需求建议 → 显式采用」写入。</div>')+
    zone('结果概览',layerPanel)+
    zone('输入准备','<label>需求作用域<select id="requiredScope">'+scopeOptions+'</select></label><div class="cns-requirements"><fieldset class="cns-requirement">'+communication(c)+'</fieldset><fieldset class="cns-requirement">'+navigation(n)+'</fieldset><fieldset class="cns-requirement">'+surveillance(s)+'</fieldset></div>')+
    zone('阻塞项与工程假设',blockerList(requiredCnsBlockers,'当前没有阻塞项。未确认的参数保持待确认，不代表已满足。'))+
    zone('主操作',primaryAction('<button class="primary full" id="saveRequiredCns">保存正式 CNS需求</button>',{note:'保存后按所选作用域写入正式需求；重新评估需求建议不会自动改动正式需求。'}))+
    zone('结果说明','<div class="parameter-note">时间规范字段统一使用秒；旧毫秒/精度/更新间隔字段由兼容层同步。未知参数保持待确认，系统不提供安全阈值默认值。</div>');
  const groundCapabilityPanel='<div class="demo-note">可靠性参数是统计属性，不会随机决定当前服务状态；演示数据与未确认参数只作为待核实输入。地面设备能力独立于航空器配置与正式 CNS需求。</div><div class="flow-summary"><strong>地面设备能力</strong><br>'+escapeHtml(devices)+'</div><div class="parameter-note">设备目录状态：'+sourceStateText(deviceItems.length?'ready':'not_configured')+'。设备能力不会自动成为任务需求，也不会自动写入正式 CNS需求。</div>';
  // ---- 高级：时间与场景 / 安全与耦合 / V3 CNS评估（研究对照） -----------------
  const nextEnabled=!!flow.steps['4'];
  const body=wbPanel('operate','',{segments:[
      ['run-op-aircraft','飞行器能力',wbBlock('航空器配置（飞行器能力）',wbSegHint(OPERATE_SEGMENTS,'run-op-aircraft')+aircraftPanel)],
      ['run-op-rules','飞行规则',wbBlock('运行约束（飞行规则）',wbSegHint(OPERATE_SEGMENTS,'run-op-rules')+rulesPanel)]
    ]})
    +wbPanel('result','',{segments:[
      ['run-res-required','CNS需求',wbBlock('正式 CNS需求',wbSegHint(RESULT_SEGMENTS,'run-res-required')+requiredCnsPanel)
        +wbBlock('地面设备能力',groundCapabilityPanel)],
      ['run-res-recommend','需求建议',wbBlock('需求建议',wbSegHint(RESULT_SEGMENTS,'run-res-recommend')+requirementPanel)],
      ['run-res-corridor','服务走廊',wbBlock('CNS 服务走廊',wbSegHint(RESULT_SEGMENTS,'run-res-corridor')+corridorPanel)]
    ]})
    +wbPanel('advanced','',{segments:[
      ['run-adv-timing','时间与场景',wbBlock('时间与场景',wbSegHint(ADVANCED_SEGMENTS,'run-adv-timing')+timingPanel+renderProtectionBudget(flow.protection_envelope)+renderDaaEncounterLab(flow))],
      ['run-adv-safety','安全与耦合',wbBlock('安全与耦合',wbSegHint(ADVANCED_SEGMENTS,'run-adv-safety')+safetyPanel)],
      ['run-adv-v3','V3 CNS评估',wbBlock('V3 CNS评估（研究对照 · 高级）',wbSegHint(ADVANCED_SEGMENTS,'run-adv-v3')+v3Panel)]
    ]})
    +nextStepBar({
      enabled:nextEnabled,
      label:'下一步：CNS 规划',
      reason:nextEnabled?'':'第 4 步尚未达到可继续条件（'+workflowStatusText('blocked')+'）：运行规则校验通过并确认正式 CNS需求后，才能进入 CNS 规划。',
      note:nextEnabled?'将进入第 5 步：CNS 规划。':''
    });
  return shell('04','运行规则','航空器配置、运行约束与地面设备能力三层相互独立；需求建议不会自动成为正式 CNS需求。',body);
}

export function bind(c){
  const flow=c.flow();if(flow.rules)c.$('heightMode').value=flow.rules.height_mode;
  bindDaaEncounterLab(c);
  c.$('aircraftProfile').onchange=event=>{const profile=flow.aircraft_profiles.items.find(item=>item.aircraft_id===event.target.value);if(!profile)return;c.$('manufacturer').value=profile.manufacturer||'';c.$('model').value=profile.model||'';for(const [id,key] of [['cruise','cruise_speed_mps'],['maximum','max_speed_mps'],['mtbf','mtbf_h']])if(profile[key]!=null)c.$(id).value=profile[key];};
  c.actionButton('saveRules',()=>c.mutate('rules',{aircraft_id:c.$('aircraftProfile').value,manufacturer:c.$('manufacturer').value,model:c.$('model').value,cruise_speed:c.$('cruise').value,max_speed:c.$('maximum').value,mtbf:c.$('mtbf').value,route_id:c.$('aircraftRoute').value,height_ab:c.$('heightAB').value,height_ba:c.$('heightBA').value,height_mode:c.$('heightMode').value,horizontal_separation:c.$('separation').value,direction_rule:c.$('directionRule').value,delay_sensor:c.$('delaySensor').value,delay_command:c.$('delayCommand').value}));
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
    c.$('safetyEventPreview').textContent='失效条件：'+statusText(result.failure_condition?.status||'unknown')+' · 不可接受事件：'+statusText(result.unacceptable_event?.status||'unknown')+' · 严重度：'+statusText(result.unacceptable_event?.severity||'unknown');
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
    c.$('coupledEventPreview').textContent='耦合条件：'+statusText(result.coupled_condition?.status||'unknown')+' · 耦合不可接受事件：'+statusText(result.coupled_unacceptable_event?.status||'unknown')+' · 概率：'+statusText(result.probability_status||'not_calculated');
  });
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(5);
}
