// =========================================================
// LayeredRouteCandidate 的 Continuous Validation 工作台面板（production）
//
// 硬边界（全部只做"转印"，前端不重算任何净空、margin 或结论）：
//  * 只验证 current LayeredRouteCandidate 的**原始二维 path** 在显式、恒定的
//    EGM2008 正高巡航高度下的净空；不 replan、不 refine、不做四舍五入、不跨层、
//    不改 candidate，也不写 operational_routes；
//  * 后端状态严格转印，绝不互相冒充：
//      validated_candidate / failed / unresolved / validation_incomplete /
//      not_ready / stale；
//  * unresolved 与 validation_incomplete 永远不显示为 failed；
//  * 只有 current_applicability==='current' 的 validation 才是"当前有效验证"，
//    stale 只作为历史证据保留；
//  * RouteRiskProfile 必须是 current，但它的 classification **不是** hard gate：
//    profile 只作为审计前置条件出现，阈值不进入 validation fingerprint；
//  * airspace 在本验证里是 display_only / not_applicable，不参与验证与指纹；
//  * source/evidence、resource_limits、fingerprint 全部原样展示。
// =========================================================
import {escapeHtml,statusBadge,wbBlock,wbDisclosure} from './common.js';

//: 后端 domain.layered_route_validation.VALIDATION_STATUSES 的前端镜像（只用于分组展示）。
export const LAYERED_VALIDATION_STATUSES=[
  'validated_candidate','failed','unresolved','not_ready','validation_incomplete','stale'
];
//: 只有这四种是"真正跑过证据"的终态；not_ready / stale 是过程态与历史态。
export const LAYERED_VALIDATION_TERMINAL_STATUSES=[
  'validated_candidate','failed','unresolved','validation_incomplete'
];
//: 必须显式区分开的状态集合：前端不得把 unresolved / validation_incomplete 说成 failed。
export const LAYERED_VALIDATION_DISTINCT_STATUSES=[
  'validated_candidate','failed','unresolved','validation_incomplete','not_ready','stale'
];
export const LAYERED_VALIDATION_DOMAIN_IDS=['terrain','building'];
export const LAYERED_VALIDATION_DOMAIN_LABELS={
  terrain:'Terrain（源生地形像素）',
  building:'Building（真实建筑 footprint）',
};

export const LAYERED_VALIDATION_LABEL='LayeredRouteCandidate 连续验证（candidate validation，不是运行航路）';
export const LAYERED_VALIDATION_SCOPE_NOTE='不 replan、不 refine、不四舍五入、不跨层、'
  +'不修改 candidate，也不创建或改写 operational_routes；本验证不构成适航或法规符合性结论。';
export const LAYERED_VALIDATION_RRP_NOTE='RouteRiskProfile 必须是 current，'
  +'但它的 classification / 阈值不是本验证的 hard gate（阈值不进入 validation fingerprint）。';
export const LAYERED_VALIDATION_AIRSPACE_NOTE='Airspace 在本验证里是 display_only / '
  +'not_applicable：既不参与验证结论，也不进入 validation fingerprint。';
export const LAYERED_VALIDATION_RESOURCE_LIMIT_NOTE='resource_limit 是计算资源上限，'
  +'不是安全参数：达到上限只产生 validation_incomplete，绝不产生 failed。';
export const LAYERED_VALIDATION_ALTITUDE_NOTE='高度完全由 AltitudeLayer → '
  +'confirmed EGM2008 cruise altitude 承载；二维 path 不携带高度第三坐标。';
export const LAYERED_VALIDATION_STALE_LABEL='stale：只作为历史证据保留，绝不冒充当前验证';

const text=value=>String(value??'');
const finite=value=>Number.isFinite(Number(value))&&value!==null&&value!=='';
const fmt=(value,digits=3)=>finite(value)?Number(value).toFixed(digits):'—';
const raw=value=>value===null||value===undefined||value===''?'—':String(value);
const short=value=>{const value_=text(value);return value_||'—';};

function jsonInline(value){
  try{return JSON.stringify(value===undefined?null:value);}catch(error){return String(value);}
}

function rows(items){
  return (items||[]).filter(Boolean).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item[0])
    +'</b><small>'+item[1]+'</small></span></div>').join('');
}

function intervalText(interval){
  const value=interval||{};
  return 'start '+fmt(value.start_distance_m,3)+' m · end '+fmt(value.end_distance_m,3)+' m'
    +' · status '+escapeHtml(short(value.status))
    +' · reason '+escapeHtml(short(value.reason_id||value.reason))
    +' · domain '+escapeHtml(short(value.domain))
    +' · source '+escapeHtml(short(value.source_id||(value.source||{}).id));
}

// ---------------------------------------------------------------- model

/**
 * 只读投影：readiness / validations 全部来自后端 snapshot，前端不重新判断任何结论。
 * @param {object} flow published workflow snapshot
 */
export function layeredRouteValidationModel(flow){
  const snapshot=flow||{};
  const readiness=snapshot.layered_route_validation_readiness||{};
  const collection=snapshot.layered_route_validations||{};
  const items=Array.isArray(collection.items)?collection.items:[];
  const sourceChain=readiness.source_chain||{};
  // 先做逐条投影，再从中挑出"当前验证"：current 必须是投影后的记录，
  // 否则 snake_case 字段（minimum_margins / resource_limits …）会漏掉。
  const validations=items.map(item=>({
    validationId:text(item?.validation_id),
    status:text(item?.status||'not_ready'),
    statusReason:text(item?.status_reason),
    currentApplicability:text(item?.current_applicability),
    staleReason:text(item?.stale_reason),
    validatedAt:text(item?.validated_at),
    routeId:text((item?.candidate||{}).route_id),
    candidateId:text((item?.candidate||{}).candidate_id),
    altitudeLayerId:text((item?.candidate||{}).altitude_layer_id),
    sourceType:text(item?.source_type),
    validationFingerprint:text((item?.fingerprints||{}).validation_fingerprint),
    candidateFingerprint:text((item?.fingerprints||{}).candidate_fingerprint),
    pathFingerprint:text((item?.fingerprints||{}).path_fingerprint),
    blockingReasons:item?.blocking_reasons||[],
    domains:item?.domains||{},
    // 以下三项严格来自后端 snake_case 字段，前端只做键名映射、不补默认值。
    minimumMargins:item?.minimum_margins||{},
    failedIntervals:item?.failed_intervals||[],
    unresolvedIntervals:item?.unresolved_intervals||[],
    criticalEvidence:item?.critical_evidence||[],
    resourceLimits:item?.resource_limits||{},
    policies:item?.policies||{},
    sourceAudits:item?.source_audits||{},
    validatorVersions:item?.validator_versions||{},
    provenance:item?.provenance||{},
    semantics:item?.semantics||{},
  }));
  // 只有后端明确标 current_applicability==='current' 的 validation 才是当前验证：
  // 找不到就是 null（stale / not_ready / 历史记录绝不顶替 current）。
  const current=validations.find(item=>item.currentApplicability==='current')||null;
  return {
    readinessStatus:text(readiness.status||'not_ready'),
    algorithm:readiness.algorithm||{},
    candidate:readiness.candidate||{},
    altitudeLayer:readiness.altitude_layer||null,
    cruiseAltitude:readiness.cruise_altitude||{},
    riskProfile:readiness.route_risk_profile||{},
    sourceChain,
    sourceAudits:sourceChain.audits||{},
    // clearance policy 原样来自 snapshot：前端只展示，不补默认值、不重算。
    policies:{
      terrain:snapshot.layered_route_feasibility_policy||{},
      building:snapshot.building_clearance_policy||{},
    },
    airspace:readiness.airspace||{},
    semantics:readiness.semantics||{},
    blockers:Array.isArray(readiness.blockers)?readiness.blockers:[],
    collectionStatus:text(collection.status||'not_calculated'),
    count:Number.isFinite(Number(collection.count))?Number(collection.count):items.length,
    activeValidationId:collection.active_validation_id||null,
    current,
    validations,
    scopeNote:LAYERED_VALIDATION_SCOPE_NOTE,
    rrpNote:LAYERED_VALIDATION_RRP_NOTE,
    airspaceNote:LAYERED_VALIDATION_AIRSPACE_NOTE,
    resourceLimitNote:LAYERED_VALIDATION_RESOURCE_LIMIT_NOTE,
    altitudeNote:LAYERED_VALIDATION_ALTITUDE_NOTE,
  };
}

/** 状态语义：只由后端 status / current_applicability 决定，前端不推断 safe。 */
export function layeredValidationStatusModel(validation){
  if(!validation)return null;
  const status=text(validation.status||'not_ready');
  const applicability=text(validation.current_applicability);
  return {
    status,
    applicability,
    statusReason:text(validation.status_reason),
    staleReason:text(validation.stale_reason),
    isTerminal:LAYERED_VALIDATION_TERMINAL_STATUSES.includes(status),
    isValidated:status==='validated_candidate',
    isFailed:status==='failed',
    isUnresolved:status==='unresolved',
    isIncomplete:status==='validation_incomplete',
    isNotReady:status==='not_ready',
    isStale:status==='stale',
    // 只有"当前适用"才允许把记录当作现在有效的证据。
    isCurrentApplicable:applicability==='current',
    isCurrent:status==='validated_candidate'&&applicability==='current',
  };
}

export function layeredValidationDomainModel(validation,domainId){
  const domain=(validation?.domains||{})[domainId];
  if(!domain)return null;
  return {
    domainId,
    label:LAYERED_VALIDATION_DOMAIN_LABELS[domainId]||domainId,
    status:text(domain.status||'not_evaluated'),
    reason:text(domain.reason),
    minimumMargin:finite(domain.minimum_margin)?Number(domain.minimum_margin):null,
    violationCount:(domain.violations||[]).length,
    unresolvedCount:(domain.unresolved||[]).length,
    violations:domain.violations||[],
    unresolved:domain.unresolved||[],
    evidence:domain.evidence||{},
  };
}

export function layeredValidationCurrentValidation(flow,validationId){
  const wanted=text(validationId);
  if(!wanted)return null;
  const items=((flow||{}).layered_route_validations||{}).items;
  const found=(Array.isArray(items)?items:[]).find(item=>text(item?.validation_id)===wanted)||null;
  return found&&found.current_applicability==='current'?found:null;
}

/** 当前验证的 validation fingerprint（Apply 的 expected 值必须来自它，绝不现场发明）。 */
export function layeredValidationFingerprint(flow,validationId){
  const found=layeredValidationCurrentValidation(flow,validationId)
    ||(((flow||{}).layered_route_validations||{}).items||[])
      .find(item=>text(item?.validation_id)===text(validationId))||null;
  return found?text((found.fingerprints||{}).validation_fingerprint)||null:null;
}

// ---------------------------------------------------------------- render

function readinessBlock(model){
  const candidate=model.candidate||{},layer=model.altitudeLayer||{},cruise=model.cruiseAltitude||{};
  const profile=model.riskProfile||{},chain=model.sourceChain||{};
  const terrainAudit=model.sourceAudits.terrain_dtm||{},buildingAudit=model.sourceAudits.buildings||{};
  const rowsAll=[
    ['algorithm',escapeHtml(short(model.algorithm.algorithm_id))+' · version '
      +escapeHtml(short(model.algorithm.algorithm_version))],
    ['readiness status',statusBadge(model.readinessStatus)+' · candidate '+statusBadge(text(candidate.status||'not_ready'))
      +' · current_applicability '+escapeHtml(short(candidate.current_applicability))],
    ['candidate',escapeHtml(short(candidate.candidate_id))+' · route '+escapeHtml(short(candidate.route_id))
      +' · path fingerprint '+escapeHtml(short(candidate.path_fingerprint))
      +'<br>candidate fingerprint '+escapeHtml(short(candidate.candidate_fingerprint))],
    ['AltitudeLayer / confirmed EGM2008 cruise',escapeHtml(short(layer.altitude_layer_id))
      +' · vertical_reference '+escapeHtml(short(layer.vertical_reference))
      +' · confirmed '+escapeHtml(String(layer.confirmed===true))
      +'<br>cruise status '+escapeHtml(short(cruise.status))
      +' · altitude_egm2008_m '+fmt(cruise.altitude_egm2008_m,3)+' m'],
    ['terrain clearance','status '+escapeHtml(short(model.policies.terrain.status))
      +' · terrain_vertical_clearance_m '+fmt(model.policies.terrain.terrain_vertical_clearance_m,3)+' m'],
    ['building clearance','status '+escapeHtml(short(model.policies.building.status))
      +' · horizontal_clearance_m '+fmt(model.policies.building.horizontal_clearance_m,3)+' m'
      +' · vertical_clearance_m '+fmt(model.policies.building.vertical_clearance_m,3)+' m'],
    ['current RouteRiskProfile',profile.profile_id?escapeHtml(short(profile.profile_id))+' · status '
      +escapeHtml(short(profile.status))+' · current_applicability '+escapeHtml(short(profile.current_applicability))
      +'<br>classification 用作 hard gate '+escapeHtml(String(profile.classification_used_as_hard_gate===true))
      +' · 阈值进入 validation fingerprint '+escapeHtml(String(profile.thresholds_used_in_validation_fingerprint===true))
      :'—（缺少 current profile：'+escapeHtml(LAYERED_VALIDATION_RRP_NOTE)+'）'],
    ['verified source chain','status '+escapeHtml(short(chain.status))+' · source_type '+escapeHtml(short(chain.source_type))
      +'<br>terrain_dtm '+escapeHtml(short(terrainAudit.status))+' · '+escapeHtml(short(terrainAudit.file_name))
      +' · sha256 '+escapeHtml(short(terrainAudit.sha256))
      +'<br>buildings '+escapeHtml(short(buildingAudit.status))+' · '+escapeHtml(short(buildingAudit.file_name))
      +' · sha256 '+escapeHtml(short(buildingAudit.sha256))],
    ['airspace','status '+escapeHtml(short(model.airspace.status))
      +' · applicability '+escapeHtml(short(model.airspace.applicability))
      +' · used_in_validation '+escapeHtml(String(model.airspace.used_in_validation===true))
      +' · used_in_fingerprint '+escapeHtml(String(model.airspace.used_in_fingerprint===true))
      +'<br>'+escapeHtml(LAYERED_VALIDATION_AIRSPACE_NOTE)],
    ['semantics',escapeHtml(jsonInline(model.semantics))],
  ];
  const blockers=model.blockers.length
    ?rows(model.blockers.map(item=>[short(item.reason_code),escapeHtml(short(item.reason))]))
    :'<div class="empty-note">readiness 无 blocker</div>';
  const button=model.readinessStatus==='ready'?'':' disabled';
  // 标题只允许静态业务语言：状态徽章一律放在正文里（wbBlock 的第二个参数只能是徽章，
  // 标题会被转义，动态状态放进标题会显示成 &lt;span …&gt; 文本）。
  return wbBlock('连续验证就绪（Continuous Validation readiness）',
    '<div class="scroll-list route-list">'+rows([
      ['readiness status',statusBadge(model.readinessStatus)
        +(model.readinessStatus==='ready'?' · ready（无 blocker）':' · not_ready（blocker 未清除）')],
    ])+'</div>'
    +'<div class="parameter-note"><b>'+escapeHtml(LAYERED_VALIDATION_LABEL)+'</b><br>'
    +escapeHtml(LAYERED_VALIDATION_SCOPE_NOTE)+'<br>'
    +escapeHtml(LAYERED_VALIDATION_RRP_NOTE)+'<br>'
    +escapeHtml(LAYERED_VALIDATION_ALTITUDE_NOTE)+'</div>'
    +'<div class="scroll-list route-list">'+rows(rowsAll)+'</div>'
    +'<h4>blockers（原样转印）</h4><div class="scroll-list route-list">'+blockers+'</div>'
    +'<div class="button-row"><button class="primary" id="evaluateLayeredRouteValidation"'+button
    +'>运行 LayeredRouteCandidate 连续验证</button></div>'
    +'<div class="parameter-note">验证不 replan、不 refine、不改 candidate，也不写入 operational_routes；'
    +'证据与 fingerprint 只在后端计算。</div>',
    statusBadge(model.readinessStatus));
}

/** policy 只用于展示：原样读 snapshot 里的 clearance policy，不在前端重算。 */
function domainBlock(validation,domainId){
  const domain=layeredValidationDomainModel(validation,domainId);
  if(!domain)return wbBlock(LAYERED_VALIDATION_DOMAIN_LABELS[domainId]||domainId,
    '<div class="empty-note">该 domain 没有证据（validation 尚未真正评估本 domain）</div>');
  const violationRows=domain.violations.length
    ?rows(domain.violations.map(item=>[short(item.interval_id||item.reason_id),intervalText(item)]))
    :'<div class="empty-note">无 failed interval</div>';
  const unresolvedRows=domain.unresolved.length
    ?rows(domain.unresolved.map(item=>[short(item.interval_id||item.reason_id),intervalText(item)]))
    :'<div class="empty-note">无 unresolved interval</div>';
  const evidence=domain.evidence||{};
  return wbBlock(domain.label,
    '<div class="scroll-list route-list">'+rows([
      ['domain status',statusBadge(domain.status)],
      ['minimum_margin',fmt(domain.minimumMargin,3)+' m'],
      ['reason',escapeHtml(short(domain.reason))],
      ['failed / unresolved interval 数',escapeHtml(String(domain.violationCount))+' / '
        +escapeHtml(String(domain.unresolvedCount))],
      ['source',escapeHtml(jsonInline(evidence.source||{}))],
    ])+'</div>'
    +'<h4>failed_intervals</h4><div class="scroll-list route-list">'+violationRows+'</div>'
    +'<h4>unresolved_intervals</h4><div class="scroll-list route-list">'+unresolvedRows+'</div>'
    +(domain.unresolvedCount?'<div class="parameter-note"><b>unresolved 不是 failed</b>：'
      +'证据不足（例如源像素 nodata）只表示本段尚未判定，绝不表示净空突破。</div>':'')
    +(domain.domainId==='building'?'<div class="parameter-note">粗 L8 建筑高度不构成最终判定；'
      +'本 domain 只使用真实 footprint 证据。</div>':''),
    statusBadge(domain.status));
}

function validationBlock(validation){
  const status=layeredValidationStatusModel(validation);
  const margins=validation.minimumMargins||{};
  const limits=validation.resourceLimits||{};
  const failed=validation.failedIntervals||[],unresolved=validation.unresolvedIntervals||[];
  const failedRows=failed.length
    ?rows(failed.map(item=>[short(item.domain)+' · '+short(item.interval_id||item.reason_id),intervalText(item)]))
    :'<div class="empty-note">没有 failed interval</div>';
  const unresolvedRows=unresolved.length
    ?rows(unresolved.map(item=>[short(item.domain)+' · '+short(item.interval_id||item.reason_id),intervalText(item)]))
    :'<div class="empty-note">没有 unresolved interval</div>';
  const incomplete=status.isIncomplete
    ?'<div class="parameter-note"><b>validation_incomplete</b>：'+escapeHtml(LAYERED_VALIDATION_RESOURCE_LIMIT_NOTE)
      +'</div>':'';
  const notReady=status.isNotReady&&(validation.blockingReasons||[]).length
    ?'<div class="scroll-list route-list">'
      +rows(validation.blockingReasons.map(item=>[short(item.reason_code),escapeHtml(short(item.reason))]))+'</div>'
    :'';
  return wbBlock('当前有效验证（current_applicability=current）',
    '<div class="scroll-list route-list">'+rows([
      ['validation status',statusBadge(status.status)],
      ['validation_id / status',escapeHtml(short(validation.validationId))+' · '+statusBadge(status.status)
        +' · status_reason '+escapeHtml(short(status.statusReason))],
      ['current_applicability',escapeHtml(short(status.applicability))
        +' · 只有 current 才是现在有效的证据；stale 只进历史'],
      ['candidate / route / altitude layer',escapeHtml(short(validation.candidateId))+' · '
        +escapeHtml(short(validation.routeId))+' · '+escapeHtml(short(validation.altitudeLayerId))],
      ['validated_at / source_type',escapeHtml(short(validation.validatedAt))+' · '+escapeHtml(short(validation.sourceType))],
      ['minimum_margins','terrain_vertical_m '+fmt(margins.terrain_vertical_m,3)
        +' m · building_vertical_m '+fmt(margins.building_vertical_m,3)+' m'],
      ['failed / unresolved interval 数',escapeHtml(String(failed.length))+' / '+escapeHtml(String(unresolved.length))],
      ['resource_limits','max_evidence_items '+raw(limits.max_evidence_items)
        +' · observed_evidence_items '+raw(limits.observed_evidence_items)
        +' · limit_reached '+escapeHtml(String(limits.limit_reached===true))
        +' · safety_parameter '+escapeHtml(String(limits.safety_parameter===true))],
      ['policies','terrain '+fmt((validation.policies||{}).terrain_vertical_clearance_m,3)+' m'
        +' · building horizontal '+fmt((validation.policies||{}).building_horizontal_clearance_m,3)+' m'
        +' / vertical '+fmt((validation.policies||{}).building_vertical_clearance_m,3)+' m'],
      ['validation fingerprint',escapeHtml(short(validation.validationFingerprint))],
      ['candidate fingerprint',escapeHtml(short(validation.candidateFingerprint))],
      ['candidate / path fingerprint',escapeHtml(short(validation.candidateFingerprint))+' · '
        +escapeHtml(short(validation.pathFingerprint))],
      ['source_audits',escapeHtml(jsonInline(validation.sourceAudits))],
      ['evidence / provenance',escapeHtml(jsonInline(validation.provenance))],
      ['validator_versions',escapeHtml(jsonInline(validation.validatorVersions))],
    ])+'</div>'
    +incomplete+notReady
    +domainBlock(validation,'terrain')+domainBlock(validation,'building')
    +'<h4>failed_intervals</h4><div class="scroll-list route-list">'+failedRows+'</div>'
    +'<h4>unresolved_intervals（绝不显示为 failed）</h4><div class="scroll-list route-list">'
    +unresolvedRows+'</div>'
    +wbDisclosure('semantics / 能力边界',
      '<div class="parameter-note">'+escapeHtml(jsonInline(validation.semantics))+'<br>'
      +escapeHtml(LAYERED_VALIDATION_SCOPE_NOTE)+'<br>'
      +escapeHtml(LAYERED_VALIDATION_AIRSPACE_NOTE)+'</div>'));
}

function historyBlock(model){
  if(!model.validations.length)return wbBlock('验证历史','<div class="empty-note">尚无 validation 记录</div>');
  const rowsAll=model.validations.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(short(item.validationId))+'</b> '+statusBadge(item.status)
    +(item.currentApplicability==='current'?' <small>当前有效（current_applicability=current）</small>'
      :'<small>applicability '+escapeHtml(short(item.currentApplicability))+'</small>')
    +'<small>route '+escapeHtml(short(item.routeId))+' · candidate '+escapeHtml(short(item.candidateId))
    +' · layer '+escapeHtml(short(item.altitudeLayerId))+' · source '+escapeHtml(short(item.sourceType))+'</small>'
    +'<small>validation fingerprint '+escapeHtml(short(item.validationFingerprint))
    +' · path fingerprint '+escapeHtml(short(item.pathFingerprint))+'</small>'
    +'<small>validated_at '+escapeHtml(short(item.validatedAt))
    +(item.status==='stale'?' · stale_reason '+escapeHtml(short(item.staleReason)):'' )
    +'</small>'
    +(item.status==='stale'?'<small>'+escapeHtml(LAYERED_VALIDATION_STALE_LABEL)+'</small>':'')
    +(item.blockingReasons||[]).map(reason=>'<small>blocking_reason '
      +escapeHtml(short(reason.reason_code))+' · '+escapeHtml(short(reason.reason))+'</small>').join('')
    +'</span></div>').join('');
  return wbBlock('验证历史（stale 只作为历史证据保留）',
    '<div class="parameter-note">共 '+escapeHtml(String(model.count))+' 条 · collection status '
    +statusBadge(model.collectionStatus)+'。前端绝不把 stale 记录当成 current 验证。</div>'
    +'<div class="scroll-list route-list">'+rowsAll+'</div>');
}

/**
 * 渲染 LayeredRouteCandidate 连续验证面板（= 本文件的公开渲染入口）。
 *
 * 面板默认可见性由后端 readiness 决定：只有 readiness=ready 时评估按钮才可用。
 */
export function renderLayeredRouteValidation(flow){
  const model=layeredRouteValidationModel(flow);
  const current=model.current;
  return readinessBlock(model)
    +(current?validationBlock(current):wbBlock('当前有效验证（current_applicability=current）',
      '<div class="empty-note">当前没有 current_applicability=current 的 validation；'
      +'stale / not_ready 记录只在下方历史中保留，绝不冒充当前验证。</div>'))
    +historyBlock(model);
}

// ---------------------------------------------------------------- bind

/**
 * 绑定连续验证控件。
 *
 * 副作用边界：唯一写入动作是
 *   POST /api/layered-route-validations/evaluate-real
 * 它只产生一条 validation 记录；本面板不发任何 operational 写入。
 */
export function bindLayeredRouteValidation(c){
  if(!c||typeof c.$!=='function')return;
  if(!c.$('evaluateLayeredRouteValidation'))return;
  c.actionButton('evaluateLayeredRouteValidation',()=>c.resourceAction(
    '/api/layered-route-validations/evaluate-real',{}));
}
