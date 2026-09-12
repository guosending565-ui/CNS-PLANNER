import {escapeHtml,shell,statusBadge} from './common.js';

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
  const fcOptions=failureConditions.map(item=>'<option value="'+escapeHtml(item.failure_condition_id)+'">'+escapeHtml(item.failure_condition_id+' · '+item.subsystem+' · '+item.failure_mode)+'</option>').join('');
  const records=failureConditions.map(item=>item.failure_condition_id+': severity='+item.severity+', '+item.status).join(' · ')||'无';
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
    '<pre class="flow-summary" id="safetyEventPreview">选择 Failure Condition 与 Service State 后预览。</pre>';
}

export function render({flow}){
  const aircraft=flow.aircraft||{},rules=flow.rules||{},catalog=flow.aircraft_profiles||{items:[]};
  const selectedProfile=flow.selected_aircraft_profile_id||aircraft.aircraft_id||'';
  const profileOptions='<option value="">自定义/未选择</option>'+catalog.items.map(item=>'<option value="'+escapeHtml(item.aircraft_id)+'" '+selected(item.aircraft_id,selectedProfile)+'>'+escapeHtml(item.name)+' · '+escapeHtml(item.aircraft_id)+'</option>').join('');
  const routeOptions='<option value="">不指定</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'" '+selected(route.route_id,aircraft.route_id)+'>'+route.route_id+' '+route.direction+'</option>').join('');
  const scopeOptions='<option value="project">项目默认需求</option>'+(flow.scenario_routes||[]).map(route=>'<option value="'+route.route_id+'">航路覆盖：'+route.route_id+'</option>').join('');
  const requirements=flow.required_cns?.project_default||{},c=requirements.communication||{},n=requirements.navigation||{},s=requirements.surveillance||{};
  const profileSummary=catalog.items.filter(item=>item.aircraft_id===selectedProfile).map(capabilitySummary).join('');
  const devices=(flow.device_catalog?.items||[]).map(item=>item.subsystem+':'+(item.type?.technology||'unknown')+' / '+(item.reliability?.source||'未记录')+' / '+(item.reliability?.status||'missing_data')).join(' · ')||'未加载';
  const safetyPanel=safetyPolicyPanel(flow.safety_policy||{});
  const body='<div class="demo-note">AircraftCNSProfileCatalog：'+escapeHtml(flow.aircraft_source)+' · '+catalog.count+' 条；机载能力不会自动成为任务需求</div><label>飞行器能力档案<select id="aircraftProfile">'+profileOptions+'</select></label>'+profileSummary+
    '<div class="form-grid"><label>厂家<input id="manufacturer" value="'+escapeHtml(value(aircraft,'manufacturer','工程测试厂家'))+'"></label><label>型号<input id="model" value="'+escapeHtml(value(aircraft,'model','Demo-A1'))+'"></label><label>巡航速度 m/s<input type="number" id="cruise" value="'+value(aircraft,'cruise_speed_mps',25)+'"></label><label>最大速度 m/s<input type="number" id="maximum" value="'+value(aircraft,'max_speed_mps',40)+'"></label><label>MTBF h<input type="number" id="mtbf" value="'+value(aircraft,'mtbf_h',10000)+'"></label><label>绑定航路<select id="aircraftRoute">'+routeOptions+'</select></label><label>A→B 高度 m<input type="number" id="heightAB" value="'+value(rules,'height_ab_m',120)+'"></label><label>B→A 高度 m<input type="number" id="heightBA" value="'+value(rules,'height_ba_m',150)+'"></label><label>高度模式<select id="heightMode"><option value="different">双向不同高度</option><option value="same">同高度层</option></select></label><label>水平间隔 m<input type="number" id="separation" value="'+value(rules,'horizontal_separation_m',100)+'"></label><label>感知→平台 ms<input type="number" id="delaySensor" value="'+value(rules,'delay_sensor_to_platform_ms',500)+'"></label><label>平台→航空器 ms<input type="number" id="delayCommand" value="'+value(rules,'delay_platform_to_aircraft_ms',500)+'"></label></div><label>方向规则<select id="directionRule"><option>按航向分层</option><option>同一航路仅一架</option><option>同一方向仅一架</option></select></label><button class="primary full" id="saveRules">保存并校验规则</button>'+
    (flow.aircraft?'<div class="flow-summary">Legacy λ='+(flow.aircraft.lambda_per_hour*1000000).toFixed(3)+'×10⁻⁶/h（兼容字段，非 P4 ReliabilitySpec 推断） · 总时延 '+rules.total_delay_ms+' ms · 反应距离 '+rules.reaction_distance_m+' m<br>'+statusBadge(rules.status)+' '+escapeHtml(rules.message)+'</div>':'')+
    '<h3>Required CNS Performance</h3><label>需求作用域<select id="requiredScope">'+scopeOptions+'</select></label><div class="cns-requirements"><fieldset class="cns-requirement">'+communication(c)+'</fieldset><fieldset class="cns-requirement">'+navigation(n)+'</fieldset><fieldset class="cns-requirement">'+surveillance(s)+'</fieldset></div><button class="secondary full" id="saveRequiredCns">保存 RequiredCNS</button><div class="parameter-note">时间规范字段统一使用秒；旧毫秒/精度/更新间隔字段由兼容层同步。未知参数保持 pending_confirmation，不提供安全阈值默认值。</div><div class="demo-note">ReliabilitySpec 是统计属性，不会随机决定当前服务状态；demo 与未确认参数仅作待核实输入。</div><div class="flow-summary"><strong>Ground Device Capability</strong><br>'+escapeHtml(devices)+'</div>'+safetyPanel+'<button class="secondary full" id="nextStep" '+(!flow.steps['4']?'disabled':'')+'>下一步：设备与布站</button>';
  return shell('04','运行规则','Aircraft Capability、Required CNS Performance 与地面设备能力相互独立。',body);
}

export function bind(c){
  const flow=c.flow();if(flow.rules)c.$('heightMode').value=flow.rules.height_mode;
  c.$('aircraftProfile').onchange=event=>{const profile=flow.aircraft_profiles.items.find(item=>item.aircraft_id===event.target.value);if(!profile)return;c.$('manufacturer').value=profile.manufacturer||'';c.$('model').value=profile.model||'';for(const [id,key] of [['cruise','cruise_speed_mps'],['maximum','max_speed_mps'],['mtbf','mtbf_h']])if(profile[key]!=null)c.$(id).value=profile[key];};
  c.actionButton('saveRules',async()=>{await c.mutate('rules',{aircraft_id:c.$('aircraftProfile').value,manufacturer:c.$('manufacturer').value,model:c.$('model').value,cruise_speed:c.$('cruise').value,max_speed:c.$('maximum').value,mtbf:c.$('mtbf').value,route_id:c.$('aircraftRoute').value,height_ab:c.$('heightAB').value,height_ba:c.$('heightBA').value,height_mode:c.$('heightMode').value,horizontal_separation:c.$('separation').value,direction_rule:c.$('directionRule').value,delay_sensor:c.$('delaySensor').value,delay_command:c.$('delayCommand').value});if(c.flow().rules?.status==='passed')await c.mutate('operational');});
  const optional=id=>c.$(id).value===''?null:Number(c.$(id).value),textValue=id=>c.$(id).value.trim()||null,required=id=>({yes:true,no:false,pending:null})[c.$(id).value],interfaces=id=>(c.$(id).value||'').split(',').map(item=>item.trim()).filter(Boolean);
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
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(5);
}
