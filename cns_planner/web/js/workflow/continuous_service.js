// =========================================================
// Round 2.5 —— P17 连续服务 / 不可接受事件评估（只读展示 + 显式动作）
//
// 设计约束（与 Round 2.4 的工程依据面板同一契约）：
//  * 本模块**只转印**后端 canonical 字段，绝不新增任何业务数值、绝不自行判定；
//  * 「连续缺口」与「运行可接受性」严格区分：coverage gap ≠ 自动 planning failure，
//    但 unacceptable / unknown 也绝不被说成"通过"；
//  * managed_gap 必须**强制披露**（service / 位置 / 连续长度 / 预计持续时间 / 阈值 /
//    mitigation / 依据），并明确写出"并非全覆盖"；
//  * 参数逐项显示 authority（显式工程证据 / 外部研究参考 / 内置工程基线），
//    绝不让内置基线看起来像法规或厂家事实；
//  * 内置基线里 3 s 的定位是 **FC30 设备 failsafe 触发事实**，不是法规阈值，
//    该表述在本面板与报告中逐字保留。
// =========================================================

import {blockerList,escapeHtml,wbBlock,wbSegHint} from './common.js';

export const CONTINUOUS_SERVICE_EVALUATE_ENDPOINT='/api/cns-continuous-service/evaluate';
export const CONTINUOUS_SERVICE_POLICY_ENDPOINT='/api/cns-continuous-service/policy';
export const OPERATION_SCENARIO_ENDPOINT='/api/cns-continuous-service/scenario';
export const PLANNING_EVIDENCE_ENDPOINT='/api/planning-evidence';

//: 运行可接受性结论 → 业务中文（**绝不**把 unknown / unacceptable 说成"通过"）。
export const ACCEPTABILITY_TEXT={
  fully_satisfied:{label:'完全满足',note:'航路上没有连续服务缺口。',badge:'passed'},
  acceptable_with_managed_gap:{label:'存在有管理的缺口（managed gap）',
    note:'航路上**确实存在**连续缺口，但连续时长在工程阈值内：必须逐段披露，绝不是"全覆盖"。',
    badge:'pending_confirmation'},
  unacceptable:{label:'不可接受（unacceptable）',
    note:'已确认的连续缺口超过阈值，或保护链时间余量为负。',badge:'failed'},
  unknown:{label:'不可判定（unknown，fail-closed）',
    note:'证据不足：系统保持 unknown（fail-closed）并阻止进入正式方案评审，绝不放行。',
    badge:'missing_data'},
  not_calculated:{label:'尚未评估',note:'尚未运行连续服务可接受性评估。',badge:'not_calculated'},
  not_applicable:{label:'没有可评估的航路',note:'当前没有任何可评估的运行航路。',badge:'not_applicable'},
};

//: 子系统判定词汇 → 业务中文。
export const SUBSYSTEM_STATUS_TEXT={
  nominal:'正常',acceptable_degraded:'可接受的降级',acceptable_with_managed_gap:'有管理的缺口',
  unacceptable:'不可接受',unknown:'不可判定',not_applicable:'不适用',
};

//: 连续事件类型 → 业务中文。
export const EVENT_KIND_TEXT={
  service_outage:'服务中断（全失联）',
  redundancy_degradation:'冗余退化（仍有链路，独立 provider 不足）',
  navigation_degradation:'导航降级（RTK → GNSS 回退）',
  surveillance_detection_gap:'监视保护走廊覆盖缺口',
};

//: 参数来源 authority → 可读标签与说明（**绝不**把内置基线写成法规事实）。
export const PARAMETER_AUTHORITY_TEXT={
  explicit_evidence:{label:'显式工程依据',note:'由本项目显式登记，逐条带出处。'},
  policy_override:{label:'策略覆盖',note:'由参数策略显式覆盖（可保存、可重开）。'},
  builtin_engineering_assumption:{label:'内置工程基线',
    note:'未提供显式依据时采用的内置默认值：**不是法规阈值、也不是厂家事实**。'},
  derived_from_route_service_evidence:{label:'航路服务证据推导',
    note:'由 P15 航路内该 service 的已确认缺口结构推导（不是时长阈值）。'},
  unknown:{label:'尚无依据',note:'保持 unknown；不参与判定。'},
};

//: 来源类型 → 业务中文（与 planning_evidence 的四类一一对应）。
export const SOURCE_TYPE_TEXT={
  confirmed_source_fact:'有正式资料支持的事实',
  external_reference:'外部研究参考（非法规、非厂家事实）',
  engineering_assumption:'工程规划假设',
  internal_baseline:'内置工程基线（非法规事实）',
  unknown:'尚无依据',
  mixed:'混合来源',
};

//: 允许进入正式方案评审的结论（与后端 STEP6_ALLOWED_ACCEPTABILITY 一致）。
export const STEP6_ALLOWED_ACCEPTABILITY=['fully_satisfied','acceptable_with_managed_gap'];

const CONTINUOUS_PARAMETER_ORDER=[
  'c_full_outage_max_s','c_redundancy_degradation_max_s',
  'D_safety_m','D_uncertainty_m','T_chain_detect_track_s','T_chain_sensor_to_platform_s',
  'T_chain_platform_processing_s','T_chain_platform_to_aircraft_s',
  'T_chain_aircraft_response_manoeuvre_s','relative_speed_basis',
  'design_intruder_speed_mps','nominal_closing_speed_mps','conservative_closing_speed_mps',
  'navigation_route_containment_accuracy_m','rtk_availability','gnss_availability',
  'gnss_accuracy_along_route_m','navigation_degradation_time_s',
];

const EVENT_KIND_ORDER=['service_outage','redundancy_degradation','navigation_degradation','surveillance_detection_gap'];

function number(value,digits=1){
  return Number.isFinite(Number(value))?Number(value).toFixed(digits):'—';
}

function acceptanceOf(status){
  return ACCEPTABILITY_TEXT[status]||{label:String(status||'—'),note:'',badge:'not_calculated'};
}

function authorityOf(authority){
  return PARAMETER_AUTHORITY_TEXT[authority]||{label:String(authority||'—'),note:''};
}

function sourceTypeOf(sourceType){
  return SOURCE_TYPE_TEXT[sourceType]||String(sourceType||'—');
}

/** P17 投影（后端已下发的只读对象；缺失时返回空结构，绝不猜）。 */
export function continuousServiceModel(flow){
  const projection=(flow&&flow.cns_continuous_service)||{};
  const result=projection.result||(flow&&flow.continuous_service_acceptability)||{};
  return {
    result,
    parameters:projection.parameters||{},
    policy:projection.policy||{},
    scenario:projection.operation_scenario||{},
    fc30:projection.fc30||{},
    step6Gate:projection.step6_gate||{},
    status:String(result.status||'not_calculated'),
  };
}

/** 最长连续缺口的可读行（逐子系统；缺值如实写"—"）。 */
export function longestGapRows(model){
  const rows=[];
  for(const route of model.result.routes||[]){
    for(const item of route.subsystems||[]){
      const longest=item.longest_event;
      rows.push({
        routeId:route.route_id,
        subsystem:item.subsystem,
        status:item.status,
        statusText:SUBSYSTEM_STATUS_TEXT[item.status]||String(item.status||'—'),
        kind:longest?longest.kind:null,
        kindText:longest?(EVENT_KIND_TEXT[longest.kind]||longest.kind):'无连续缺口',
        startOffsetM:longest?longest.start_offset_m:null,
        endOffsetM:longest?longest.end_offset_m:null,
        lengthM:longest?longest.length_m:null,
        durationS:longest?longest.duration_s:null,
        limitS:longest?longest.limit_s:null,
      });
    }
  }
  return rows;
}

/** managed_gap 的逐段披露行（**强制**：service / 位置 / 长度 / 时长 / 阈值 / mitigation / 依据）。 */
export function managedGapRows(model){
  const rows=[];
  for(const route of model.result.routes||[]){
    for(const gap of route.managed_gaps||[]){
      rows.push({
        routeId:route.route_id,service:gap.service,kind:gap.kind,
        kindText:EVENT_KIND_TEXT[gap.kind]||gap.kind,
        startOffsetM:gap.start_offset_m,endOffsetM:gap.end_offset_m,
        lengthM:gap.length_m,durationS:gap.duration_s,limitS:gap.limit_s,
        mitigation:gap.mitigation,basis:gap.basis,
      });
    }
  }
  return rows;
}

/** 参数表（只转印后端 authority / source_type / 出处）。 */
export function parameterRows(model){
  const parameters=model.parameters.parameters||{};
  const names=CONTINUOUS_PARAMETER_ORDER.filter(name=>parameters[name])
    .concat(Object.keys(parameters).filter(name=>!CONTINUOUS_PARAMETER_ORDER.includes(name)).sort());
  return names.map(name=>{
    const item=parameters[name]||{};
    return {
      field:name,label:item.label||name,value:item.value,unit:item.unit,
      authority:item.authority,authorityText:authorityOf(item.authority).label,
      sourceType:item.source_type,sourceTypeText:sourceTypeOf(item.source_type),
      source:item.source||null,statement:item.statement||null,
      externalReference:item.external_reference||null,reason:item.reason||null,
      disclosure:item.report_disclosure||null,
    };
  });
}

/** 连续事件（C/N/S 合并，按类型分组）。 */
export function continuousEventRows(model){
  const rows=[];
  for(const route of model.result.routes||[]){
    for(const kind of EVENT_KIND_ORDER){
      for(const event of (route.events_by_kind||{})[kind]||[]){
        rows.push({
          routeId:route.route_id,subsystem:event.subsystem,service:event.service,
          kind:event.kind,kindText:EVENT_KIND_TEXT[event.kind]||event.kind,
          startOffsetM:event.start_offset_m,endOffsetM:event.end_offset_m,
          lengthM:event.length_m,durationS:event.duration_s,limitS:event.limit_s,
          exceedsLimit:event.exceeds_limit,
        });
      }
    }
  }
  return rows;
}

/** 保护走廊行（公式与**实际参数值**都要显示）。 */
export function protectionCorridorRows(model){
  return (model.result.routes||[]).map(route=>{
    const corridor=route.corridor||{};
    return {
      routeId:route.route_id,
      status:corridor.status,
      formula:corridor.formula,
      dSafetyM:corridor.D_safety_m,dUncertaintyM:corridor.D_uncertainty_m,
      vRelativeMps:corridor.V_relative_mps,relativeSpeedBasis:corridor.relative_speed_basis,
      tChainS:corridor.T_chain_s,tChainComponents:corridor.T_chain_components_s||{},
      dProtectionM:corridor.D_protection_m,
      cnsHalfWidthM:corridor.cns_requirement_corridor_half_width_m,
      outerHalfWidthM:corridor.outer_half_width_m,
      routeLengthM:corridor.route_length_m,
      detection:route.first_detection_evidence||{},
      acceptance:route.surveillance_acceptance||{},
    };
  });
}

/** Step6 门禁摘要（供方案评审页与 Step5 共用；只转印后端结论）。 */
export function step6GateModel(flow){
  const model=continuousServiceModel(flow);
  const gate=model.step6Gate||{};
  return {
    status:String(gate.status||model.status),
    confirmationAllowed:gate.confirmation_allowed===true,
    allowedStatuses:gate.allowed_statuses||STEP6_ALLOWED_ACCEPTABILITY,
    managedGapCount:Number(gate.managed_gap_count||0),
    unacceptableCount:Number(gate.unacceptable_count||0),
    unknownCount:Number(gate.unknown_count||0),
    requiresDisclosure:gate.requires_managed_gap_disclosure===true,
    disclosureLines:gate.disclosure_lines||[],
    blockingReason:gate.blocking_reason||null,
    reasons:gate.reasons||[],
  };
}

/** 渲染面板（返回 HTML；交互由 bindContinuousServicePanel 绑定）。 */
export function renderContinuousServicePanel(flow,segments){
  const model=continuousServiceModel(flow);
  const text=acceptanceOf(model.status);
  const scenario=model.scenario||{};
  const fc30=model.fc30||{};
  const gate=step6GateModel(flow);

  const header=wbBlock('连续服务可接受性结论',
    '<div class="demo-note"><b>'+escapeHtml(text.label)+'</b>（'+escapeHtml(model.status)+'）—— '
    +escapeHtml(text.note)+'</div>'
    +'<div class="parameter-note">运行可接受性：'
    +'<b>完全满足</b> / <b>有管理的缺口</b> 可进入正式方案评审；'
    +'<b>不可接受</b> 与 <b>不可判定（unknown）</b> 一律 fail-closed，阻止进入评审。<br>'
    +'核心命题：<b>coverage gap ≠ 自动 planning failure</b> —— P15 的空间缺口只说明"哪里没有覆盖"，'
    +'是否可接受由本评估按连续中断时长、导航回退链与保护链时间余量判定。</div>'
    +'<div class="parameter-note">当前结论：可接受 '+gate.managedGapCount+' 段 managed gap · 不可接受 '
    +gate.unacceptableCount+' 条航路 · 不可判定 '+gate.unknownCount+' 条航路。</div>');

  const scenarioBlock=wbBlock('操作场景（engineering_assumption）',
    '<div class="demo-note">single_ownship='+escapeHtml(String(scenario.single_ownship))
    +' · intruder_scope='+escapeHtml(String(scenario.intruder_scope))
    +' · cruise_altitude='+escapeHtml(String(scenario.cruise_altitude))
    +' · design_intruder_speed='+number(scenario.design_intruder_speed_mps)+' m/s'
    +' · nominal_closing='+number(scenario.nominal_closing_speed_mps)+' m/s'
    +' · conservative_closing='+number(scenario.conservative_closing_speed_mps)+' m/s</div>'
    +'<div class="parameter-note">来源：'+escapeHtml(scenario.intruder_speed_source||'未记录')
    +'（source_type='+escapeHtml(scenario.intruder_speed_source_type||'—')+'）。'
    +'这些数值是工程假设，不代表实测交通统计。</div>');

  const parameterBlock=wbBlock('评估参数与逐项来源',
    '<div class="parameter-note">阈值与保护链参数都可由「工程依据 / 规划假设」显式覆盖；'
    +'未提供显式依据时采用<b>内置工程基线</b>（不是法规阈值，也不是厂家事实）。'
    +'C 全失联基线 3 s 的依据是 <b>FC30 设备 failsafe 触发事实</b>（遥控信号丢失超过 3 s 触发 RTH）。</div>'
    +'<div class="scroll-list cns-input-list">'+parameterRows(model).map(item=>
      '<div class="coverage-card" data-continuous-parameter="'+escapeHtml(item.field)+'">'
      +'<b>'+escapeHtml(item.label)+'（'+escapeHtml(item.field)+'）</b>'
      +'<span>取值 <b>'+escapeHtml(String(item.value??'—'))+'</b>'
      +(item.unit?' '+escapeHtml(item.unit):'')+'</span>'
      +'<span>来源：'+escapeHtml(item.authorityText)+' · '+escapeHtml(item.sourceTypeText)+'</span>'
      +(item.externalReference?'<span>外部参考：'+escapeHtml(item.externalReference)+'</span>':'')
      +(item.source?'<small>'+escapeHtml(item.source)+'</small>':'')
      +(item.reason?'<small>'+escapeHtml(item.reason)+'</small>':'')
      +'</div>').join('')+'</div>');

  const effectiveLimits=model.result.service_acceptability_limits||{};
  const limitBlock=wbBlock('本次评估生效的服务阈值',
    '<div class="demo-note">C 服务中断（全失联）上限 '
    +number((effectiveLimits.C||{}).service_outage)+' s · C 冗余退化上限 '
    +number((effectiveLimits.C||{}).redundancy_degradation)+' s · S 服务中断上限 '
    +number((effectiveLimits.S||{}).service_outage)+' s · S 冗余退化上限 '
    +number((effectiveLimits.S||{}).redundancy_degradation)+' s</div>'
    +'<div class="parameter-note">解析顺序：'+escapeHtml(
      model.parameters.limits_resolution_order
      ||'cns_continuous_service_policy → planning_evidence 显式记录 → 内置工程基线')+'</div>'
    +renderContinuousParameterForm(model));

  const fc30Block=wbBlock('FC30 canonical 机载档案（事实与出处）',
    '<div class="demo-note">当前选定档案：'+escapeHtml(String(fc30.selected_aircraft_id||'—'))
    +(fc30.is_selected?'（就是 FC30）':'（不是 FC30：P17 使用**当前选定档案**的航路速度）')+'</div>'
    +'<div class="parameter-note">'+escapeHtml(fc30.disclosure||'')+'</div>'
    +'<div class="scroll-list cns-input-list">'+(fc30.facts||[]).map(fact=>
      '<div class="coverage-card"><b>'+escapeHtml(fact.parameter)+'＝'
      +escapeHtml(String(fact.value??'—'))+(fact.unit?' '+escapeHtml(fact.unit):'')+'</b>'
      +'<span>'+escapeHtml(fact.statement||'')+'</span>'
      +'<small>来源：'+escapeHtml(fact.source||'未记录')
      +' · source_type='+escapeHtml(fact.source_type||'—')
      +(fact.not_a_regulatory_threshold?' · <b>不是法规阈值</b>':'')+'</small></div>').join('')
    +'</div>');

  const corridorRows=protectionCorridorRows(model);
  const corridorBlock=wbBlock('水平航路保护走廊与监视验收',
    '<div class="parameter-note">保护走廊不是航路中心线：'
    +'<code>D_protection = D_safety + V_relative × T_chain + D_uncertainty</code>；'
    +'监视验收 <code>T_available = (first_detection_distance − D_safety) / V_relative</code>、'
    +'<code>T_margin = T_available − T_chain</code>。T_margin ≥ 0 才算监视来得及介入。</div>'
    +'<div class="demo-note">FC30 的 RTK 恢复 9–13 s 只作为**外部研究参考**登记，'
    +'不作为本评估的硬门。</div>'
    +'<div class="scroll-list cns-input-list">'+corridorRows.map(row=>
      '<div class="coverage-card" data-protection-corridor="'+escapeHtml(row.routeId)+'">'
      +'<b>航路 '+escapeHtml(row.routeId)+' · 走廊状态 '+escapeHtml(String(row.status||'—'))+'</b>'
      +'<span>D_safety '+number(row.dSafetyM)+' m · D_uncertainty '+number(row.dUncertaintyM)
      +' m · V_relative '+number(row.vRelativeMps)+' m/s（'+escapeHtml(String(row.relativeSpeedBasis||'—'))+'）'
      +' · T_chain '+number(row.tChainS)+' s</span>'
      +'<span><b>D_protection '+number(row.dProtectionM)+' m</b>'
      +' · CNS 需求走廊半宽 '+number(row.cnsHalfWidthM)+' m'
      +' → 保护走廊外半宽 '+number(row.outerHalfWidthM)+' m</span>'
      +'<span>首次探测距离 '+(row.detection.usable===true?number(row.detection.first_detection_distance_m)+' m':'不可用（'+
        escapeHtml(String(row.detection.coverage_status||'unknown'))+'）')
      +' · 服务 '+escapeHtml(String(row.detection.service_key||'—'))+'</span>'
      +'<span>监视验收 '+escapeHtml(String((row.acceptance||{}).status||'unknown'))
      +' · T_available '+number((row.acceptance||{}).t_available_s)+' s'
      +' · <b>T_margin '+number((row.acceptance||{}).t_margin_s)+' s</b></span>'
      +((row.detection||{}).not_usable_reason?'<small>'+escapeHtml(row.detection.not_usable_reason)+'</small>':'')
      +((row.acceptance||{}).reason?'<small>'+escapeHtml(row.acceptance.reason)+'</small>':'')
      +'</div>').join('')+'</div>');

  const longestRows=longestGapRows(model);
  const eventBlock=wbBlock('C / N / S 连续事件与最长连续缺口',
    '<div class="parameter-note">持续时间 = 沿航路缺口长度 ÷ 当前航路速度'
    +'（'+(model.result.speed_basis?
      escapeHtml(String(model.result.speed_basis.route_speed_source||''))+'＝'
      +number(model.result.route_speed_mps)+' m/s':'未提供航路速度')
    +'）。缺口长度是 P15 走廊体元缺口的**保守纵向投影**。</div>'
    +'<div class="scroll-list cns-input-list">'+longestRows.map(row=>
      '<div class="coverage-card"><b>'+escapeHtml(row.routeId)+' · '+escapeHtml(row.subsystem)
      +' · '+escapeHtml(row.statusText)+'</b>'
      +'<span>最长连续缺口：'+escapeHtml(row.kindText)+'</span>'
      +'<span>位置 '+number(row.startOffsetM)+'–'+number(row.endOffsetM)+' m'
      +' · 连续长度 '+number(row.lengthM)+' m'
      +' · 预计持续时间 '+number(row.durationS)+' s'
      +' · 阈值 '+(row.limitS==null?'—':number(row.limitS)+' s')+'</span>'
      +'</div>').join('')+'</div>');

  const managedRows=managedGapRows(model);
  const managedBlock=wbBlock('managed gap 强制披露',
    managedRows.length
      ? '<div class="demo-note">以下缺口**真实存在**，只因连续时长在工程阈值内被接受为「有管理的缺口」：'
        +'<b>绝不是全覆盖</b>。</div>'
        +'<div class="scroll-list cns-input-list">'+managedRows.map(row=>
          '<div class="coverage-card">'
          +'<b>'+escapeHtml(row.routeId)+' · '+escapeHtml(String(row.service||''))+'</b>'
          +'<span>'+escapeHtml(row.kindText)+' · 位置 '+number(row.startOffsetM)+'–'
          +number(row.endOffsetM)+' m · 连续长度 '+number(row.lengthM)+' m'
          +' · 预计持续时间 '+number(row.durationS)+' s · 阈值 '+number(row.limitS)+' s</span>'
          +'<span>缓解措施（mitigation）：'+escapeHtml(String(row.mitigation||'未记录'))+'</span>'
          +'<span>依据/假设（basis）：'+escapeHtml(String(row.basis||'未记录'))+'</span>'
          +'</div>').join('')+'</div>'
        +((model.result.disclosure_lines||[]).length
          ? '<div class="parameter-note">报告披露原文：<br>'
            +(model.result.disclosure_lines||[]).map(line=>escapeHtml(line)).join('<br>')+'</div>'
          : '')
      : '<div class="wb-empty">当前没有被接受为 managed gap 的连续缺口。</div>');

  const reasonBlock=wbBlock('判定原因与未评估项',
    '<div class="demo-note">'+(model.result.reasons||[]).length
      ? (model.result.reasons||[]).map(reason=>escapeHtml(reason)).join('<br>')
      : '当前没有额外原因。'+'</div>'
    +'<div class="parameter-note">未评估（如实登记，绝不冒充结论）：'
    +escapeHtml(Object.keys(model.result.not_evaluated||{}).join('、')||'—')+'</div>');

  const gateBlock=wbBlock('进入正式方案评审的门禁',
    '<div class="demo-note">允许：彻底满足（fully_satisfied）/ 有管理的缺口（acceptable_with_managed_gap）；'
    +'阻止：不可接受（unacceptable）/ 不可判定（unknown）。</div>'
    +'<div class="parameter-note">当前门禁：<b>'
    +escapeHtml(gate.confirmationAllowed?'允许进入评审':'阻止进入评审')+'</b>'
    +(gate.blockingReason?'<br>'+escapeHtml(gate.blockingReason):'')
    +'</div>');

  const actionBlock=wbBlock('操作',
    '<div class="button-row"><button class="primary" id="evaluateContinuousService">'
    +'运行连续服务可接受性评估</button></div>'
    +'<div class="parameter-note">评估只读 P14 / P15 / P16 与显式工程依据，'
    +'不会重算上游，也不修改任何上游结果。</div>'
    +renderScenarioForm(scenario));

  const body=(segments?wbSegHint(segments,'cns-res-continuous'):'')
    +header+scenarioBlock+limitBlock+corridorBlock+eventBlock+managedBlock
    +parameterBlock+fc30Block+reasonBlock+gateBlock+actionBlock;
  return body;
}

function renderContinuousParameterForm(model){
  const limits=(model.policy||{}).service_acceptability_limits||{};
  const c=limits.C||{};
  return '<div class="cns-requirements">'
    +'<label>C 服务中断上限（s）<input class="panel-input" type="number" step="0.1" '
    +'id="continuousOutageLimit" value="'+(c.service_outage??'')+'" placeholder="留空 = 使用内置基线"></label>'
    +'<label>C 冗余退化上限（s）<input class="panel-input" type="number" step="0.1" '
    +'id="continuousDegradationLimit" value="'+(c.redundancy_degradation??'')+'" placeholder="留空 = 使用内置基线"></label>'
    +'<label>策略来源<input class="panel-input" id="continuousPolicySource" value="'
    +escapeHtml((model.policy||{}).source||'')+'" placeholder="用户配置 / 工程基线条目"></label>'
    +'<label class="check-row"><input type="checkbox" id="continuousPolicyConfirmed"> 确认以上阈值为工程取值（绝不是法规阈值）</label>'
    +'<button class="secondary full" id="saveContinuousPolicy">保存阈值覆盖</button>'
    +'</div>';
}

function renderScenarioForm(scenario){
  return '<div class="cns-requirements">'
    +'<label>设计入侵者速度（m/s）<input class="panel-input" type="number" step="0.1" '
    +'id="scenarioIntruderSpeed" value="'+escapeHtml(String(scenario.design_intruder_speed_mps??''))+'"></label>'
    +'<label>标称接近速度（m/s）<input class="panel-input" type="number" step="0.1" '
    +'id="scenarioNominalClosing" value="'+escapeHtml(String(scenario.nominal_closing_speed_mps??''))+'"></label>'
    +'<label>保守接近速度（m/s）<input class="panel-input" type="number" step="0.1" '
    +'id="scenarioConservativeClosing" value="'+escapeHtml(String(scenario.conservative_closing_speed_mps??''))+'"></label>'
    +'<button class="secondary full" id="saveOperationScenario">保存操作场景（engineering_assumption）</button>'
    +'<div class="parameter-note">Round 2.5 只支持 single_ownship=true · intruder_scope=other_uav_only · '
    +'cruise_altitude=ALT-100；其它口径会被后端拒绝（fail-closed）。</div>'
    +'</div>';
}

/**
 * 绑定 P17 面板的全部交互（与人工点击走**完全相同**的正式接口）。
 *
 * @param {object} c 工作台控制器
 */
export function bindContinuousServicePanel(c){
  if(!c)return;
  const mutation=(path,payload)=>(c.resourceMutationAndRefresh
    ? c.resourceMutationAndRefresh(path,payload)
    : c.resourceAction(path,payload));
  const evaluate=()=>mutation(CONTINUOUS_SERVICE_EVALUATE_ENDPOINT,{});
  if(c.$('evaluateContinuousService'))c.actionButton('evaluateContinuousService',evaluate);

  if(c.$('saveContinuousPolicy'))c.actionButton('saveContinuousPolicy',()=>{
    const outage=c.$('continuousOutageLimit')?.value.trim();
    const degradation=c.$('continuousDegradationLimit')?.value.trim();
    const limits={C:{}};
    if(outage!=='')limits.C.service_outage=Number(outage);
    if(degradation!=='')limits.C.redundancy_degradation=Number(degradation);
    const payload={
      service_acceptability_limits:Object.keys(limits.C).length?limits:{},
      source:c.$('continuousPolicySource')?.value.trim()||'user_configuration',
      confirmed:c.$('continuousPolicyConfirmed')?.checked===true,
    };
    return mutation(CONTINUOUS_SERVICE_POLICY_ENDPOINT,payload);
  });

  if(c.$('saveOperationScenario'))c.actionButton('saveOperationScenario',()=>{
    const payload={cns_operation_scenario:{
      single_ownship:true,intruder_scope:'other_uav_only',cruise_altitude:'ALT-100',
    }};
    const fields=[['scenarioIntruderSpeed','design_intruder_speed_mps'],
      ['scenarioNominalClosing','nominal_closing_speed_mps'],
      ['scenarioConservativeClosing','conservative_closing_speed_mps']];
    for(const [id,name] of fields){
      const value=c.$(id)?.value.trim();
      if(value!=='')payload.cns_operation_scenario[name]=Number(value);
    }
    return mutation(OPERATION_SCENARIO_ENDPOINT,payload);
  });
}
