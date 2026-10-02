// =========================================================
// Round 2.6 —— P17 连续服务可接受性（只读展示 + 显式动作）
//
// 设计约束（与 Round 2.4 的工程依据面板同一契约）：
//  * 本模块**只转印**后端 canonical 字段，绝不新增任何业务数值、绝不自行判定；
//  * 「连续缺口」与「运行可接受性」严格区分：coverage gap ≠ 自动 planning failure，
//    但 unacceptable / unknown 也绝不被说成"通过"；
//  * managed_gap 必须**强制披露**（service / 位置 / 连续长度 / 预计持续时间 / 阈值 /
//    mitigation / 依据），并明确写出"并非全覆盖"；
//  * 参数逐项显示 authority（显式工程证据 / 外部研究参考 / 内置工程基线 /
//    **尚无依据 evidence_required**），绝不让内置基线看起来像法规或厂家事实；
//  * **通信阈值必须分成两件事显示，绝不合并**：
//      - ``fc30.device_failsafe_fact``      —— FC30 设备 failsafe 事实（不是规划阈值）；
//      - ``fc30.project_planning_threshold`` —— 本项目规划阈值（用户显式登记，
//        未登记 ⇒ evidence_required，判定保持 unknown，fail-closed）；
//      - ``fc30.redundancy_degradation_threshold`` —— **独立阈值**，
//        绝不与"完全中断"合并成一个"通信中断上限"；
//  * 保护走廊逐项显示四分量（D_separation / V_relative / T_chain / D_maneuver /
//    D_uncertainty），其中 D_maneuver=50 m 的身份是 **engineering_baseline**（不是法规值）；
//  * 监视威胁**分层**显示：合作无人机 / RID（主要威胁）与非合作无人机 / Radar
//    （补充威胁）两块分开渲染，绝不给同一个"监视结论"；
//  * Radar 求解不可行是**能力限制（limitation）**，不是系统错误：用警告色块显示
//    后端 disclosure 原文，并写明它**不改变**主要威胁的判定，也不得说成「监视已完全满足」；
//  * baseline 与 post_plan 是**两层结论**：``post_plan_projection`` 为 null 时如实写
//    「尚未计算 post-plan 投影」，绝不冒充。
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

/**
 * 后端权威**分层状态**枚举（前端只转印，不硬编码判断）。
 *
 * 兼容映射：后端在分层内部可能仍是子系统状态词汇（``nominal`` /
 * ``acceptable_degraded``），它们与 ``satisfied`` / ``acceptable_with_managed_gap``
 * 是**同一个结论**。映射只做词形归一，绝不新增结论、绝不放宽 fail-closed。
 */
export const THREAT_LAYER_STATUS_ALIASES={
  nominal:'satisfied',
  acceptable_degraded:'acceptable_with_managed_gap',
  //: 子系统词汇里的"正常"与分层词汇里的"满足"是同一件事。
  satisfied:'satisfied',
};

/** 分层状态（raw）→ 权威分层状态（未知 raw 值原样返回，绝不猜成"满足"）。 */
export function threatLayerStatusOf(status){
  const raw=String(status||'');
  return THREAT_LAYER_STATUS_ALIASES[raw]||raw;
}

/**
 * 监视威胁**分层**状态 → 业务中文（后端权威枚举，前端只转印不硬编码判断）。
 *
 * 注意 ``limitation``：它是**能力限制**（黄色 / 橙色），**不是系统错误**，
 * 也不是主要威胁的不通过；绝不允许用 error / failed 的词去描述它。
 */
export const THREAT_LAYER_STATUS_TEXT={
  satisfied:{label:'满足',badge:'passed',
    note:'该分层的监视链结论满足要求。'},
  acceptable_with_managed_gap:{label:'有管理的缺口',badge:'pending_confirmation',
    note:'该分层存在已披露的缺口，但连续时长在工程阈值内。'},
  limitation:{label:'能力限制',badge:'warning',
    note:'这是**当前方案的能力限制**（黄色 / 橙色），不是系统错误，也不是主要威胁的不通过。'},
  unacceptable:{label:'不可接受',badge:'failed',
    note:'该分层的监视链结论不可接受。'},
  unknown:{label:'不可判定',badge:'missing_data',
    note:'该分层证据不足：保持 unknown（fail-closed），绝不当成通过。'},
  not_applicable:{label:'不适用',badge:'not_applicable',
    note:'该分层没有可评估的对象。'},
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
  //: Round 2.6：没有内置值、也没有显式记录 ⇒ 必须保持不可判定（fail-closed）。
  evidence_required:{label:'尚无依据（必须显式登记）',
    note:'该参数没有显式工程依据、也没有内置默认值：系统保持 evidence_required / unknown，'
      +'**绝不用 0 或设备 failsafe 门限代替**。'},
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

//: Round 2.6 的 P17 参数显示顺序。保护走廊的四个分量**逐项**在这里出现：
//: ``D_separation_m``（必须有显式依据）/ ``D_maneuver_m``（工程基线 50 m）/
//: ``D_uncertainty_m``（必须有显式依据）。Round 2.5 的 ``D_safety_m`` 已由
//: ``D_separation_m`` 取代（旧名只作兼容读取，不再单独显示，避免两个"安全距离"）。
const CONTINUOUS_PARAMETER_ORDER=[
  'c_full_outage_max_s','c_redundancy_degradation_max_s',
  'D_separation_m','D_maneuver_m','D_uncertainty_m',
  'T_chain_detect_track_s','T_chain_sensor_to_platform_s',
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

/** 威胁分层状态 → 业务中文（未知 raw 值如实显示，绝不猜成"满足"）。 */
function threatStatusOf(status){
  const raw=threatLayerStatusOf(status);
  return THREAT_LAYER_STATUS_TEXT[raw]||{
    label:raw||'—',badge:'not_calculated',
    note:'该分层状态不在后端权威枚举里：如实显示 raw 值，绝不冒充结论。',
  };
}

/** 值是否存在（``null`` / ``undefined`` / 空串都算"没有证据"）。 */
function isPresent(value){
  return value!==null&&value!==undefined&&value!=='';
}

/** 值 + 单位（缺值写「—」，**绝不用 0 冒充证据**）。 */
function metric(value,unit='',digits=1){
  if(!isPresent(value))return '—';
  const text=String(value);
  return unit?(text+' '+unit):text;
}

/** 数值 + 单位（固定小数位；缺值写「—」）。 */
function metricFixed(value,unit='',digits=1){
  return isPresent(value)?(number(value,digits)+(unit?' '+unit:'')):'—';
}

/** 纯文本数值（缺值写「—」）。 */
function metricPlain(value,digits=1){
  return isPresent(value)?number(value,digits):'—';
}

function textOf(value,fallback='未记录'){
  return isPresent(value)?String(value):fallback;
}

/** 布尔 → 中文（缺失同样如实写"未提供"）。 */
function booleanText(value){
  if(value===true)return '是';
  if(value===false)return '否';
  return '未提供';
}

/** 列表 → 顿号分隔文本（空列表写 fallback）。 */
function listText(values,fallback='—'){
  const items=(values||[]).filter(item=>isPresent(item)).map(item=>String(item));
  return items.length?items.join('、'):fallback;
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

/** 参数表（只转印后端 authority / source_type / 出处；绝不推导"生效值"）。 */
export function parameterRows(model){
  const parameters=model.parameters.parameters||{};
  const names=CONTINUOUS_PARAMETER_ORDER.filter(name=>parameters[name])
    .concat(Object.keys(parameters).filter(name=>!CONTINUOUS_PARAMETER_ORDER.includes(name)).sort());
  return names.map(name=>{
    const item=parameters[name]||{};
    const authority=authorityOf(item.authority);
    return {
      field:name,label:item.label||name,value:item.value,unit:item.unit,
      participating:item.participating,
      authority:item.authority,authorityText:authority.label,authorityNote:authority.note,
      //: Round 2.6：``evidence_required`` 是**权威枚举**（参数无依据），前端只转印。
      evidenceRequired:item.authority==='evidence_required',
      sourceType:item.source_type,sourceTypeText:sourceTypeOf(item.source_type),
      source:item.source||null,statement:item.statement||null,
      externalReference:item.external_reference||null,reason:item.reason||null,
      disclosure:item.report_disclosure||null,
      //: 旧项目里以 Round 2.5 旧名（D_safety_m）登记的证据被读取时，后端如实标注来源字段名。
      readFromLegacyField:item.read_from_legacy_field||null,
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

/**
 * 保护走廊行（Round 2.6 **四分量逐项**显示）。
 *
 * 只转印后端 ``routes[].corridor``：``D_separation_m`` / ``V_relative_mps`` /
 * ``T_chain_s`` / ``D_maneuver_m``（engineering_baseline 50 m，**不是法规值**）/
 * ``D_uncertainty_m`` / ``D_protection_m``（``formula`` 字段来自后端，前端不自己写公式）。
 *
 * 兼容：Round 2.5 的 ``D_safety_m`` 仍是同一物理量的镜像，作为 ``dSeparationM`` 的
 * **回退**读取（仅在新名缺失时）；``dSafetyM`` 字段名保留，值同为分隔距离。
 */
export function protectionCorridorRows(model){
  return (model.result.routes||[]).map(route=>{
    const corridor=route.corridor||{};
    const detection=route.first_detection_evidence||{};
    const acceptance=route.surveillance_acceptance||{};
    const dSeparationM=isPresent(corridor.D_separation_m)
      ?corridor.D_separation_m:corridor.D_safety_m;
    return {
      routeId:route.route_id,
      status:corridor.status,
      formula:corridor.formula,
      //: 四分量（Round 2.6 正式名）。
      dSeparationM,dSeparationAuthority:corridor.D_separation_authority||null,
      dManeuverM:corridor.D_maneuver_m,dManeuverAuthority:corridor.D_maneuver_authority||null,
      dManeuverSemantics:corridor.D_maneuver_semantics||null,
      dUncertaintyM:corridor.D_uncertainty_m,
      dUncertaintyAuthority:corridor.D_uncertainty_authority||null,
      //: 兼容镜像：Round 2.5 的 D_safety_m（同一物理量，不是第二个真值）。
      dSafetyM:isPresent(corridor.D_safety_m)?corridor.D_safety_m:null,
      vRelativeMps:corridor.V_relative_mps,relativeSpeedBasis:corridor.relative_speed_basis,
      tChainS:corridor.T_chain_s,tChainComponents:corridor.T_chain_components_s||{},
      dProtectionM:corridor.D_protection_m,
      cnsHalfWidthM:corridor.cns_requirement_corridor_half_width_m,
      outerHalfWidthM:corridor.outer_half_width_m,
      routeLengthM:corridor.route_length_m,
      detection,acceptance,
      firstDetectionDistanceM:detection.first_detection_distance_m,
      firstDetectionUsable:detection.usable===true,
      firstDetectionServiceKey:detection.service_key||null,
      firstDetectionCoverageStatus:detection.coverage_status||null,
      firstDetectionNotUsableReason:detection.not_usable_reason||null,
      acceptanceStatus:acceptance.status||null,
      tAvailableS:acceptance.t_available_s,
      tMarginS:acceptance.t_margin_s,
      acceptanceReason:acceptance.reason||null,
    };
  });
}

/**
 * 监视威胁**分层**行（Round 2.6）：合作无人机 / RID（主要威胁）与
 * 非合作无人机 / Radar（补充威胁）**分开**转印，绝不合并成一个监视结论。
 *
 * 逐层转印 ``layer`` / ``label`` / ``service_keys`` / ``status`` / ``t_margin_s`` /
 * ``first_detection_distance_m`` / ``limitations`` / ``reasons``。
 */
export function threatLayerRows(model){
  const rows=[];
  for(const route of model.result.routes||[]){
    const layers=route.threat_layers||{};
    for(const layer of ['cooperative','noncooperative']){
      const entry=layers[layer];
      if(!entry)continue;
      const rawStatus=String(entry.status||'not_applicable');
      //: 词形归一（nominal → satisfied），**不**新增结论、**不**放宽 fail-closed。
      const status=threatLayerStatusOf(rawStatus);
      const text=threatStatusOf(rawStatus);
      rows.push({
        routeId:route.route_id,
        layer:entry.layer||layer,
        label:entry.label||layer,
        serviceKeys:entry.service_keys||[],
        subsystems:entry.subsystems||[],
        status,rawStatus,statusText:text.label,statusNote:text.note,statusBadge:text.badge,
        //: ``limitation`` 是能力限制（黄色 / 橙色），**不是**系统错误。
        isLimitation:status==='limitation',
        tMarginS:entry.t_margin_s,
        firstDetectionDistanceM:entry.first_detection_distance_m,
        limitations:entry.limitations||[],
        reasons:entry.reasons||[],
        //: 哪个分层是主要威胁 / 补充威胁由后端给定，前端不硬编码。
        isPrimary:String(route.primary_threat_layer||'')===String(entry.layer||layer),
        isSupplementary:String(route.supplementary_threat_layer||'')===String(entry.layer||layer),
        supplementaryIsLimitation:route.supplementary_threat_is_limitation===true,
        threatLayerNote:route.threat_layer_note||null,
      });
    }
  }
  return rows;
}

/** 能力限制（limitation）行：后端 ``result.limitations[]`` 与逐 route 的并集，按 id 去重。 */
export function limitationRows(model){
  const rows=[],seen=new Set();
  const push=item=>{
    if(!item)return;
    const key=String(item.limitation_id||item.disclosure||JSON.stringify(item));
    if(seen.has(key))return;
    seen.add(key);
    rows.push({
      limitationId:item.limitation_id||null,
      layer:item.layer||null,
      capability:item.capability||null,
      status:String(item.status||'limitation'),
      //: 能力限制**不是**系统错误：``blocking_primary_threat`` 为 false 时逐字写出来。
      blockingPrimaryThreat:item.blocking_primary_threat===true,
      semantics:item.semantics||null,
      sourceStatus:item.source_status||null,
      solverStatus:item.solver_status||null,
      disclosure:item.disclosure||null,
      mustDiscloseInReport:item.must_disclose_in_report===true,
      noRelaxationApplied:item.no_relaxation_applied||null,
      routeId:item.route_id||null,
    });
  };
  for(const item of model.result.limitations||[])push(item);
  for(const route of model.result.routes||[])for(const item of route.limitations||[])push(item);
  return rows;
}

/**
 * baseline vs post_plan（Round 2.6 两层结论）。
 *
 * ``result.post_plan_projection == null`` 时**如实**返回 ``available:false``，
 * 由渲染层写出「尚未计算 post-plan 投影（P16 方案为空或未评估）」，绝不冒充。
 */
export function postPlanProjectionModel(model){
  const projection=model.result.post_plan_projection;
  const baselineStatus=model.result.baseline_status||model.status;
  if(!projection||typeof projection!=='object'){
    return {
      available:false,baselineStatus,postPlanStatus:model.result.post_plan_status??null,
      status:null,routeCount:null,appliedActionIds:[],projectionSemantics:null,
      comparison:null,improvedServices:[],remainingGaps:[],
      improvedServiceCount:null,remainingGapCount:null,comparisonProjection:null,
      disclosure:'尚未计算 post-plan 投影（P16 方案为空或未评估）。',
    };
  }
  const comparison=projection.comparison||null;
  return {
    available:projection.available===true||projection.status!=null,
    baselineStatus,postPlanStatus:model.result.post_plan_status??projection.status??null,
    status:projection.status??null,
    routeCount:projection.route_count??null,
    appliedActionIds:projection.applied_action_ids||[],
    projectionSemantics:projection.projection_semantics||null,
    persistedAsUpstream:projection.persisted_as_upstream===true,
    comparison,
    improvedServices:(comparison&&comparison.improved_services)||[],
    remainingGaps:(comparison&&comparison.remaining_gaps)||[],
    improvedServiceCount:comparison?comparison.improved_service_count:null,
    remainingGapCount:comparison?comparison.remaining_gap_count:null,
    comparisonProjection:(comparison&&comparison.projection)||null,
  };
}

/**
 * improved_services[] / remaining_gaps[] 的逐项行。
 *
 * 字段：``route_id, subsystem, service, status, kind, length_m, duration_s, limit_s,
 * exceeds_limit, improvement_kind, declared_improvement_m, baseline_length_m,
 * baseline_duration_s``。
 */
export function postPlanComparisonRows(model){
  const projection=postPlanProjectionModel(model);
  const map=(item,changed)=>(
    {
      routeId:item.route_id,subsystem:item.subsystem,service:item.service,
      status:item.status,kind:item.kind,
      kindText:item.kind?(EVENT_KIND_TEXT[item.kind]||item.kind):null,
      lengthM:item.length_m,durationS:item.duration_s,limitS:item.limit_s,
      exceedsLimit:item.exceeds_limit,improvementKind:item.improvement_kind||null,
      declaredImprovementM:item.declared_improvement_m,
      baselineLengthM:item.baseline_length_m,baselineDurationS:item.baseline_duration_s,
      reductionM:item.reduction_m??null,semantics:item.semantics||null,
      changed,
    }
  );
  return {
    projection,
    improved:(projection.improvedServices||[]).map(item=>map(item,'improved')),
    remaining:(projection.remainingGaps||[]).map(item=>map(item,'remaining_gap')),
  };
}

/** Step6 门禁摘要（供方案评审页与 Step5 共用；只转印后端结论）。 */
export function step6GateModel(flow){
  const model=continuousServiceModel(flow);
  const gate=model.step6Gate||{};
  return {
    status:String(gate.status||model.status),
    //: Round 2.6 新增字段（字段名以后端为准，取不到就留空/原样 null）。
    projectedStatus:gate.projected_status??null,
    variantId:gate.variant_id??null,
    variantSpecific:gate.variant_specific??null,
    limitations:gate.limitations||[],
    engineeredAssumptions:gate.engineered_assumptions||[],
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

/** 门禁判定：只允许后端给出的 allowed_statuses 内的结论进入正式方案评审。 */
export function step6GateAllows(status){
  return STEP6_ALLOWED_ACCEPTABILITY.includes(String(status||''));
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
    +'未提供显式依据时可能采用<b>内置工程基线</b>（不是法规阈值，也不是厂家事实），'
    +'也可能如实保持<b>尚无依据（evidence_required，fail-closed）</b>——'
    +'逐项以 authority 为准，前端不做任何补充判断。</div>'
    +'<div class="scroll-list cns-input-list">'+parameterRows(model).map(item=>
      '<div class="coverage-card" data-continuous-parameter="'+escapeHtml(item.field)+'"'
      +(item.evidenceRequired?' data-evidence-required="true"':'')+'>'
      +'<b>'+escapeHtml(item.label)+'（'+escapeHtml(item.field)+'）</b>'
      +'<span>取值 <b>'+(isPresent(item.value)?escapeHtml(String(item.value)):'—')
      +'</b>'+(item.unit?' '+escapeHtml(item.unit):'')+'</span>'
      +'<span>来源：'+escapeHtml(item.authorityText)+' · '+escapeHtml(item.sourceTypeText)+'</span>'
      +(item.evidenceRequired
        ?'<span><b>尚无依据（必须显式登记）</b>：'+escapeHtml(item.authorityNote)+'</span>'
        :'')
      +(item.readFromLegacyField
        ?'<span>读自 Round 2.5 旧字段名 '+escapeHtml(item.readFromLegacyField)+'（同一物理量）</span>'
        :'')
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

  /* -----------------------------------------------------------------------
   * 通信阈值：**必须分成两件事显示，绝不合并**
   *   A. FC30 设备事实（厂家 / 档案层面，不是规划阈值）
   *   B. 本项目规划阈值（用户显式登记；未登记 ⇒ evidence_required / unknown）
   *   C. 冗余退化最大允许时间（**独立阈值**，绝不与完全中断合并）
   * --------------------------------------------------------------------- */
  const deviceFact=fc30.device_failsafe_fact||{};
  const projectThreshold=fc30.project_planning_threshold||{};
  const redundancyThreshold=fc30.redundancy_degradation_threshold||{};
  const projectEvidenceRequired=projectThreshold.evidence_required===true;
  const thresholdBlock=wbBlock('通信阈值：设备事实 vs 本项目规划阈值（两块分开，绝不合并）',
    '<div class="parameter-note"><b>工程规划参数，由用户确认。</b>'
    +'下面 A / B / C 是三件**不同**的事：A 是设备行为事实，B 是本项目的完全中断规划阈值'
    +'（用户显式登记），C 是冗余退化阈值。'
    +'<b>设备 failsafe 门限（A）不等于本项目的规划阈值（B）</b>，'
    +'后端也明确禁止把三者合并成一个"通信中断上限"。</div>'

    +'<div class="coverage-card" data-continuous-threshold-block="device_failsafe_fact">'
    +'<b>通信阈值 A · FC30 设备事实：失联 &gt; '
    +escapeHtml(String(metric(deviceFact.value_s,'s',0)))+' 可触发 Failsafe RTH</b>'
    +'<span>kind='+escapeHtml(String(deviceFact.kind||'—'))
    +' · is_planning_threshold='+escapeHtml(booleanText(deviceFact.is_planning_threshold))
    +' · parameter='+escapeHtml(String(deviceFact.parameter||'—'))+'</span>'
    +'<span>取值 '+escapeHtml(metric(deviceFact.value_s,'s',0))
    +' · authority='+escapeHtml(String(deviceFact.authority||'—'))
    +' · source_type='+escapeHtml(String(deviceFact.source_type||'—'))+'</span>'
    +'<span>'+escapeHtml(textOf(deviceFact.statement,'（后端未提供 statement）'))+'</span>'
    +'<small>这是**设备 failsafe 事实**，不是法规阈值，也**不是**本项目的规划阈值；'
    +'它不参与 B 的取值。</small></div>'

    +'<div class="coverage-card" data-continuous-threshold-block="project_planning_threshold"'
    +(projectEvidenceRequired?' data-evidence-required="true"':'')+'>'
    +'<b>通信阈值 B · 本项目规划阈值：用户填写 '
    +(projectEvidenceRequired?'—（尚未登记）':escapeHtml(metric(projectThreshold.value_s,'s',1)))+'</b>'
    +'<span>kind='+escapeHtml(String(projectThreshold.kind||'—'))
    +' · is_planning_threshold='+escapeHtml(booleanText(projectThreshold.is_planning_threshold))
    +' · must_be_engineering_assumption='
    +escapeHtml(booleanText(projectThreshold.must_be_engineering_assumption))
    +' · evidence_required='+escapeHtml(booleanText(projectThreshold.evidence_required))+'</span>'
    +'<span>取值 '
    +(projectEvidenceRequired?'—（尚未登记）':escapeHtml(metric(projectThreshold.value_s,'s',1)))
    +' · authority='+escapeHtml(String(projectThreshold.authority||'—'))
    +' · source_type='+escapeHtml(String(projectThreshold.source_type||'—'))+'</span>'
    +'<span>'+escapeHtml(textOf(projectThreshold.statement,'（后端未提供 statement）'))+'</span>'
    +(projectThreshold.source
      ?'<small>来源：'+escapeHtml(String(projectThreshold.source))+'</small>':'')
    +(projectThreshold.reason
      ?'<small>原因：'+escapeHtml(String(projectThreshold.reason))+'</small>':'')
    +(projectEvidenceRequired
      ?'<div class="demo-note"><b>本项目规划阈值尚未登记 ⇒ 判定保持 '
        +'evidence_required / unknown（fail-closed）。</b>'
        +'设备 failsafe 门限（A）不会被自动采用为 B 的取值；'
        +'请在上方「本次评估生效的服务阈值」里显式登记并确认。</div>'
      :'')
    +'</div>'

    +'<div class="coverage-card" data-continuous-threshold-block="redundancy_degradation_threshold">'
    +'<b>通信阈值 C · 冗余退化最大允许时间 '
    +escapeHtml(metric(redundancyThreshold.value_s,'s',1))+'</b>'
    +'<span><b>独立阈值，绝不与完全中断合并</b>：separate_from_full_outage='
    +escapeHtml(booleanText(redundancyThreshold.separate_from_full_outage))
    +' · thresholds_are_separate='+escapeHtml(booleanText(fc30.thresholds_are_separate))
    +' · threshold_merge_forbidden='+escapeHtml(booleanText(fc30.threshold_merge_forbidden))+'</span>'
    +'<span>kind='+escapeHtml(String(redundancyThreshold.kind||'—'))
    +' · is_planning_threshold='+escapeHtml(booleanText(redundancyThreshold.is_planning_threshold))
    +' · parameter='+escapeHtml(String(redundancyThreshold.parameter||'—'))+'</span>'
    +'<span>'+escapeHtml(textOf(redundancyThreshold.statement,'（后端未提供 statement）'))+'</span>'
    +'<small>冗余退化（仍有链路、只是独立 provider 不足）与完全中断是两个不同的失效模式：'
    +'前者绝不用后者的阈值判定。</small></div>');

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
  const corridorBlock=wbBlock('水平航路保护走廊（四分量逐项）与监视验收',
    '<div class="parameter-note">保护走廊不是航路中心线：'
    +'<code>D_protection = D_separation + V_relative × T_chain + D_maneuver + D_uncertainty</code>；'
    +'监视验收 <code>T_available = (first_detection_distance − D_separation) / V_relative</code>、'
    +'<code>T_margin = T_available − T_chain</code>。T_margin ≥ 0 才算监视来得及介入。'
    +'四个距离分量里任何一个没有显式依据 ⇒ D_protection 不可判定（fail-closed），绝不静默按 0 计算。</div>'
    +'<div class="demo-note">四个分量逐项显示：<b>D_separation</b>（必须有显式工程依据）、'
    +'<b>V_relative</b> / <b>T_chain</b>、'
    +'<b>D_maneuver</b>（身份 <b>engineering_baseline</b>，内置 50 m，'
    +'<b>不是法规值</b>，也不是某机型的普遍制动距离事实）、'
    +'<b>D_uncertainty</b>（必须有显式工程依据）。</div>'
    +'<div class="demo-note">FC30 的 RTK 恢复 9–13 s 只作为**外部研究参考**登记，'
    +'不作为本评估的硬门。</div>'
    +'<div class="scroll-list cns-input-list">'+corridorRows.map(row=>
      '<div class="coverage-card" data-protection-corridor="'+escapeHtml(row.routeId)+'">'
      +'<b>航路 '+escapeHtml(row.routeId)+' · 走廊状态 '+escapeHtml(String(row.status||'—'))+'</b>'
      +'<span>公式（来自后端）：<code>'+escapeHtml(String(row.formula||'—'))+'</code></span>'
      +'<span>D_separation '+metricFixed(row.dSeparationM,'m',1)
      +'（'+escapeHtml(textOf(row.dSeparationAuthority,'—'))+'）'
      +' · V_relative '+metricFixed(row.vRelativeMps,'m/s',1)
      +'（'+escapeHtml(String(row.relativeSpeedBasis||'—'))+'）'
      +' · T_chain '+metricFixed(row.tChainS,'s',1)+'</span>'
      +'<span>D_maneuver '+metricFixed(row.dManeuverM,'m',1)
      +'（'+escapeHtml(textOf(row.dManeuverAuthority,'—'))+' · <b>engineering_baseline，不是法规值</b>'
      +' · D_maneuver_semantics='+escapeHtml(String(row.dManeuverSemantics||'—'))+'）'
      +' · D_uncertainty '+metricFixed(row.dUncertaintyM,'m',1)
      +'（'+escapeHtml(textOf(row.dUncertaintyAuthority,'—'))+'）</span>'
      +(isPresent(row.dSafetyM)
        ?'<span>兼容镜像 D_safety（Round 2.5 旧名，同一物理量）'
          +metricFixed(row.dSafetyM,'m',1)+'</span>':'')
      +'<span><b>D_protection '+metricFixed(row.dProtectionM,'m',1)+'</b>'
      +' · CNS 需求走廊半宽 '+metricFixed(row.cnsHalfWidthM,'m',1)
      +' → 保护走廊外半宽 '+metricFixed(row.outerHalfWidthM,'m',1)+'</span>'
      +'<span>首次探测距离 '+(row.firstDetectionUsable
        ?metricFixed(row.firstDetectionDistanceM,'m',1)
        :'不可用（'+escapeHtml(String(row.firstDetectionCoverageStatus||'unknown'))+'）')
      +' · 服务 '+escapeHtml(String(row.firstDetectionServiceKey||'—'))+'</span>'
      +'<span>监视验收 '+escapeHtml(String(row.acceptanceStatus||'unknown'))
      +' · T_available '+metricFixed(row.tAvailableS,'s',1)
      +' · <b>T_margin '+metricFixed(row.tMarginS,'s',1)+'</b></span>'
      +(row.firstDetectionNotUsableReason
        ?'<small>'+escapeHtml(row.firstDetectionNotUsableReason)+'</small>':'')
      +(row.acceptanceReason?'<small>'+escapeHtml(row.acceptanceReason)+'</small>':'')
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

  /* -----------------------------------------------------------------------
   * 监视威胁分层：合作无人机 / RID（主要威胁）与非合作无人机 / Radar
   * （补充威胁）**两个区块分开渲染**，绝不给同一个"监视结论"。
   * --------------------------------------------------------------------- */
  const layerRows=threatLayerRows(model);
  const layerCard=row=>
    '<div class="coverage-card" data-threat-layer="'+escapeHtml(row.layer)+'"'
    +' data-threat-layer-status="'+escapeHtml(row.status)+'"'
    +(row.isLimitation?' data-capability-limitation="true"':'')+'>'
    +'<b>'+escapeHtml(String(row.label||row.layer))
    +' · <span class="flow-badge flow-'+escapeHtml(row.statusBadge)+'">'
    +escapeHtml(row.statusText)+'</span></b>'
    +'<span>分层状态：'+escapeHtml(row.status)
    +'（后端原值 '+escapeHtml(row.rawStatus)+'）'
    +' · 定位：'+(row.isPrimary?'主要威胁':row.isSupplementary?'补充威胁':'未标注')
    +'</span>'
    +'<span>服务：'+escapeHtml(listText(row.serviceKeys))+'</span>'
    +'<span>T_margin '+metricFixed(row.tMarginS,'s',1)
    +' · 首次探测距离 '+metricFixed(row.firstDetectionDistanceM,'m',1)+'</span>'
    +'<span>'+escapeHtml(row.statusNote)+'</span>'
    +(row.threatLayerNote?'<small>'+escapeHtml(row.threatLayerNote)+'</small>':'')
    +(row.limitations.length
      ?'<small>该分层的 limitations：'+escapeHtml(JSON.stringify(row.limitations))+'</small>':'')
    +(row.reasons.length
      ?'<small>reasons：'+(row.reasons).map(reason=>escapeHtml(String(reason))).join('；')+'</small>':'')
    +'</div>';
  const cooperativeRows=layerRows.filter(row=>row.layer==='cooperative');
  const noncooperativeRows=layerRows.filter(row=>row.layer==='noncooperative');
  const threatBlock=wbBlock('监视威胁分层：合作（主要）与非合作（补充）分开显示',
    '<div class="parameter-note"><b>合作无人机 / RID（主要威胁）</b>与'
    +'<b>非合作无人机 / Radar（补充威胁）</b>是**两个分层**：它们分开评估、分开披露，'
    +'绝不合并成一个监视结论，也绝不把两个分层的结论相加或互相替代。</div>'
    +'<h3>合作无人机 / RID 合作监视（主要威胁）</h3>'
    +(cooperativeRows.length
      ?cooperativeRows.map(layerCard).join('')
      :'<div class="wb-empty">后端未下发合作分层结论。</div>')
    +'<h3>非合作无人机 / Radar 非合作监视（补充威胁）</h3>'
    +(noncooperativeRows.length
      ?noncooperativeRows.map(layerCard).join('')
      :'<div class="wb-empty">后端未下发非合作分层结论。</div>'));

  /* 能力限制（limitation）：**警告色块**，绝不是 error / 系统错误。 */
  const limits2=limitationRows(model);
  const limitationBlock=wbBlock('能力限制（limitation，不是系统错误）',
    limits2.length
      ?limits2.map(item=>
        '<div class="coverage-card" data-capability-limitation="true" data-limitation-id="'
        +escapeHtml(String(item.limitationId||''))+'" '
        +'style="border-left:3px solid var(--warn);background:var(--warn-bg)">'
        +'<b><span class="flow-badge flow-warning">能力限制（黄色 / 橙色）</span>'
        +escapeHtml(String(item.capability||item.limitationId||'—'))+'</b>'
        +'<span>limitation_id='+escapeHtml(String(item.limitationId||'—'))
        +' · layer='+escapeHtml(String(item.layer||'—'))
        +' · status='+escapeHtml(String(item.status||'—'))
        +' · blocking_primary_threat='+escapeHtml(booleanText(item.blockingPrimaryThreat))+'</span>'
        +'<span>source_status='+escapeHtml(String(item.sourceStatus||'—'))
        +' · solver_status='+escapeHtml(String(item.solverStatus||'—'))
        +' · must_disclose_in_report='+escapeHtml(booleanText(item.mustDiscloseInReport))+'</span>'
        +(item.routeId?'<span>航路 '+escapeHtml(String(item.routeId))+'</span>':'')
        +'<div class="demo-note">'+escapeHtml(textOf(item.disclosure,'（后端未提供 disclosure）'))+'</div>'
        +'<span><b>该限制不改变主要威胁（合作无人机 / RID）的判定；'
        +'不得表述为「监视已完全满足」。</b></span>'
        +(item.semantics?'<small>semantics：'+escapeHtml(String(item.semantics))+'</small>':'')
        +(item.noRelaxationApplied?'<small>'+escapeHtml(String(item.noRelaxationApplied))+'</small>':'')
        +'</div>').join('')
      :'<div class="wb-empty">后端未登记任何能力限制（limitation）。</div>');

  /* baseline vs post_plan：两层结论 + 比较。 */
  const postPlan=postPlanProjectionModel(model);
  const comparisonRows=postPlanComparisonRows(model);
  const comparisonCard=(row,title)=>
    '<div class="coverage-card" data-post-plan-row="'+escapeHtml(row.changed)+'">'
    +'<b>'+escapeHtml(title)+' · '+escapeHtml(String(row.routeId||'—'))
    +' · '+escapeHtml(String(row.service||row.subsystem||'—'))+'</b>'
    +'<span>subsystem='+escapeHtml(String(row.subsystem||'—'))
    +' · status='+escapeHtml(String(row.status||'—'))
    +' · kind='+escapeHtml(String(row.kindText||row.kind||'—'))+'</span>'
    +'<span>投影态长度 '+metricFixed(row.lengthM,'m',1)
    +' · 投影态持续时间 '+metricFixed(row.durationS,'s',1)
    +' · 阈值 '+(isPresent(row.limitS)?metricFixed(row.limitS,'s',1):'—')
    +' · exceeds_limit='+escapeHtml(booleanText(row.exceedsLimit))+'</span>'
    +'<span>improvement_kind='+escapeHtml(String(row.improvementKind||'—'))
    +' · declared_improvement_m '+metricFixed(row.declaredImprovementM,'m',1)+'</span>'
    +'<span>baseline_length_m '+metricFixed(row.baselineLengthM,'m',1)
    +' · baseline_duration_s '+metricFixed(row.baselineDurationS,'s',1)
    +(isPresent(row.reductionM)?' · reduction_m '+metricFixed(row.reductionM,'m',1):'')+'</span>'
    +(row.semantics?'<small>semantics：'+escapeHtml(String(row.semantics))+'</small>':'')
    +'</div>';
  const baselineBlock=wbBlock('baseline vs post_plan（两层结论，绝不互相冒充）',
    '<div class="parameter-note">两层结论必须分开看：<b>baseline_status</b> 是当前权威'
    +'ExistingCNS 下的结论；<b>post_plan_status</b> 是实施 P16 方案后的**投影态**结论。'
    +'顶层 status 在有投影时跟随投影态（Step6 就是依据该结论）。'
    +'投影态**不写回**上游（persisted_as_upstream=false）。</div>'
    +'<div class="demo-note" data-baseline-status="'+escapeHtml(String(postPlan.baselineStatus||'—'))+'">'
    +'baseline_status：<b>'+escapeHtml(acceptanceOf(postPlan.baselineStatus).label)
    +'</b>（'+escapeHtml(String(postPlan.baselineStatus||'—'))+'）</div>'
    +'<div class="demo-note" data-post-plan-status="'+escapeHtml(String(postPlan.postPlanStatus??'—'))+'">'
    +'post_plan_status：'+(postPlan.available&&postPlan.postPlanStatus
      ?'<b>'+escapeHtml(acceptanceOf(postPlan.postPlanStatus).label)+'</b>（'
        +escapeHtml(String(postPlan.postPlanStatus))+'）'
      :'—（尚未计算）')+'</div>'
    +(postPlan.available
      ?('<div class="parameter-note" data-post-plan-available="true">投影航路数 '
        +escapeHtml(String(postPlan.routeCount??'—'))
        +' · applied_action_ids：'+escapeHtml(listText(postPlan.appliedActionIds,'（空）'))
        +' · projection_semantics='+escapeHtml(String(postPlan.projectionSemantics||'—'))
        +' · persisted_as_upstream='+escapeHtml(booleanText(postPlan.persistedAsUpstream))+'</div>'
        +'<div class="parameter-note">比较：改进 '
        +escapeHtml(String(postPlan.improvedServiceCount??'—'))+' 项 · 剩余缺口 '
        +escapeHtml(String(postPlan.remainingGapCount??'—'))+' 项'
        +(postPlan.comparisonProjection
          ?'<br>比较依据指纹：baseline '
            +escapeHtml(String(postPlan.comparisonProjection.baseline_fingerprint||'—'))
            +' → post_plan '
            +escapeHtml(String(postPlan.comparisonProjection.post_plan_fingerprint||'—')):'')
        +'</div>'
        +'<h3>improved_services（实施后改进的服务）</h3>'
        +(comparisonRows.improved.length
          ?comparisonRows.improved.map(row=>comparisonCard(row,'改进项')).join('')
          :'<div class="wb-empty">没有登记任何改进项。</div>')
        +'<h3>remaining_gaps（实施后仍然存在的缺口）</h3>'
        +(comparisonRows.remaining.length
          ?comparisonRows.remaining.map(row=>comparisonCard(row,'剩余缺口')).join('')
          :'<div class="wb-empty">没有登记任何剩余缺口。</div>')
      )
      :('<div class="wb-empty" data-post-plan-available="false">'
        +escapeHtml(postPlan.disclosure)+'</div>')));

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
    +'阻止：不可接受（unacceptable）/ 不可判定（unknown）。'
    +'门禁的允许值来自后端 allowed_statuses，前端不硬编码放宽。</div>'
    +'<div class="parameter-note">projected_status：'
    +(gate.projectedStatus
      ?'<b>'+escapeHtml(acceptanceOf(gate.projectedStatus).label)+'</b>（'
        +escapeHtml(String(gate.projectedStatus))+'）':'-（后端未提供）')
    +' · variant_id='+escapeHtml(String(gate.variantId??'—'))
    +' · variant_specific='+escapeHtml(booleanText(gate.variantSpecific))+'</div>'
    +'<div class="parameter-note">当前门禁：<b>'
    +escapeHtml(gate.confirmationAllowed?'允许进入评审':'阻止进入评审')+'</b>'
    +(gate.blockingReason?'<br>'+escapeHtml(gate.blockingReason):'')
    +'</div>'
    +(gate.limitations.length
      ?'<div class="parameter-note">门禁携带的 limitations：'
        +escapeHtml(JSON.stringify(gate.limitations))+'</div>':'')
    +(gate.engineeredAssumptions.length
      ?('<div class="parameter-note">门禁携带的 engineered_assumptions：'
        +escapeHtml(JSON.stringify(gate.engineeredAssumptions))+'</div>')
      :''));

  const actionBlock=wbBlock('操作',
    '<div class="button-row"><button class="primary" id="evaluateContinuousService">'
    +'运行连续服务可接受性评估</button></div>'
    +'<div class="parameter-note">评估只读 P14 / P15 / P16 与显式工程依据，'
    +'不会重算上游，也不修改任何上游结果。</div>'
    +renderScenarioForm(scenario));

  const body=(segments?wbSegHint(segments,'cns-res-continuous'):'')
    +header+scenarioBlock+limitBlock+thresholdBlock+corridorBlock+eventBlock
    +threatBlock+limitationBlock+baselineBlock+managedBlock
    +parameterBlock+fc30Block+reasonBlock+gateBlock+actionBlock;
  return body;
}

function renderContinuousParameterForm(model){
  const limits=(model.policy||{}).service_acceptability_limits||{};
  const c=limits.C||{};
  return '<div class="cns-requirements">'
    +'<label>最大允许完全通信中断时间（s）<input class="panel-input" type="number" step="0.1" '
    +'id="continuousOutageLimit" value="'+(c.service_outage??'')+'" placeholder="留空 = 保持 evidence_required（不采用设备门限）"></label>'
    +'<label>冗余退化最大允许时间（s）<input class="panel-input" type="number" step="0.1" '
    +'id="continuousDegradationLimit" value="'+(c.redundancy_degradation??'')+'" placeholder="留空 = 使用内置工程基线"></label>'
    +'<label>策略来源<input class="panel-input" id="continuousPolicySource" value="'
    +escapeHtml((model.policy||{}).source||'')+'" placeholder="用户配置 / 工程基线条目"></label>'
    +'<label class="check-row"><input type="checkbox" id="continuousPolicyConfirmed"> 确认以上阈值为工程取值（绝不是法规阈值）</label>'
    +'<button class="secondary full" id="saveContinuousPolicy">保存阈值覆盖</button>'
    +'<div class="parameter-note">工程规划参数，由用户显式确认；'
    +'<b>设备 failsafe 门限不等于本规划阈值</b>。'
    +'「最大允许完全通信中断时间」留空即保持 evidence_required / unknown（fail-closed），'
    +'系统绝不自动采用设备的 3 s；「冗余退化最大允许时间」是**独立阈值**，'
    +'与完全中断分开保存、分开判定。</div>'
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
    +'<div class="parameter-note">本轮只支持 single_ownship=true · intruder_scope=other_uav_only · '
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
