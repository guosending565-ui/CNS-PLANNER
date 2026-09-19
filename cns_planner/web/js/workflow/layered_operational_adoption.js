// =========================================================
// Layered Candidate 的 Operational Adoption 工作台面板（production）
//
// 硬边界（全部只做"转印"）：
//  * 本面板与既有「运行航路生成 / Legacy operationalRoutes」完全分开：
//    Legacy 的 /api/workflow/operational 与 operational_routes 语义不变，
//    本模块也绝不删除或改写它们；
//  * Select / Validate / Preview / Apply 互不等价：只有 Apply 才写 operational_routes；
//  * Preview 不写 state（后端强制 side_effects=false，前端只缓存响应）；
//  * 2D route path 不携带高度第三坐标：高度由 RouteOperatingLayer → AltitudeLayer 承载，
//    本流程不创建 RouteAltitudeProfile，也不创建 Departure/Arrival procedure；
//  * route_id 冲突时默认禁止 Apply：replace_existing 默认 false，绝不自动覆盖；
//  * Preview 是一次冻结：{validation_id, validation_fingerprint, replace_existing,
//    publication_allowed, preview response} 五者绑定在一起，Apply 只提交这份冻结结果；
//  * 首次无已知冲突时 Preview 提交 replace_existing=false；Preview 发现 conflict 后
//    UI 才出现 replace checkbox（默认不勾选）；用户改变 replace_existing 后旧 Preview
//    立即视为 expired/intent_changed：Apply 禁用、必须重新 Preview；
//  * Apply 必须先有"当前 preview"且 publication_allowed=true，并显式勾选确认；
//  * expected_validation_fingerprint 与 replace_existing 都只能来自刚 Preview 的那份冻结结果：
//    validation 选择 / fingerprint / replace_existing 任一变化都必须提示"请重新 Preview"，
//    绝不自动重试、绝不换成新 flow 值、绝不用当前 checkbox 覆盖 Preview 意图；
//  * Revoke 必须显式选择目标 adoption（绝不自动选择，也绝不按列表顺序猜测目标），
//    显式确认后只提交 adoption_id + confirmed：它是"撤销本次发布"，不是"删除航路"；
//  * published / stale / revoked / superseded 历史与 ownership 信息全部保留，
//    revoked 记录永不出现在可撤销目标里。
// =========================================================
import {escapeHtml,statusBadge,wbBlock,wbDisclosure} from './common.js';

//: 后端 domain.layered_operational_adoption.ADOPTION_STATUSES 的前端镜像。
export const LAYERED_ADOPTION_STATUSES=['published','stale','revoked'];
//: 后端 result_snapshot 里的 current_applicability 取值（含被新 adoption 取代的 superseded）。
export const LAYERED_ADOPTION_APPLICABILITIES=['current','stale','revoked','superseded','ownership_changed','stale_validation'];
//: 状态链四节点：Candidate → RRP → Validation → Operational。
export const LAYERED_ADOPTION_CHAIN_STAGES=[
  'candidate','route_risk_profile','validation','operational'
];

export const LAYERED_ADOPTION_LEGACY_LABEL='A. 现有 / Legacy 运行航路生成（保持不变）';
export const LAYERED_ADOPTION_LEGACY_NOTE='「生成场景航路（all-pairs，兼容）」与「生成运行航路」'
  +'仍走既有 /api/workflow/scenario 与 /api/workflow/operational，契约不变；'
  +'本节的 Layered Candidate 发布流程不替代、不合并、不改写它们。';
export const LAYERED_ADOPTION_PUBLISH_LABEL='B. Layered Candidate 发布（validation → operational adoption）';
export const LAYERED_ADOPTION_SCOPE_NOTE='Select / Validate / Preview / Apply 互不等价：'
  +'只有 Apply 才写 operational_routes，Preview 不写 state。';
export const LAYERED_ADOPTION_PATH_LABEL='2D route path 只保存二维 [lon, lat]，'
  +'不携带高度第三坐标；高度由 RouteOperatingLayer → AltitudeLayer 承载。';
export const LAYERED_ADOPTION_BOUNDARY_NOTE='本流程不创建 RouteAltitudeProfile，'
  +'也不创建 Departure/Arrival procedure。';
export const LAYERED_ADOPTION_CONFIRM_NOTE='Apply 必须先有当前 Preview 且 publication_allowed=true，'
  +'并显式勾选确认；expected_validation_fingerprint 只来自刚 Preview 的那条 validation。';
export const LAYERED_ADOPTION_CONFLICT_NOTE='route_id 冲突时默认禁止 Apply：'
  +'replace_existing 默认 false，绝不自动覆盖既有航路。';
export const LAYERED_ADOPTION_REVOKE_LABEL='撤销本次发布（revoke）不是"删除航路"';
export const LAYERED_ADOPTION_EVIDENCE_CHANGED='证据已变化，请重新 Preview';
//: 用户改变 replace_existing（或切换 validation / 证据变化）后，旧 Preview 的意图不再成立。
export const LAYERED_ADOPTION_INTENT_CHANGED='发布意图已变化，请重新 Preview';
export const LAYERED_ADOPTION_REVOKE_TARGET_NAME='layeredAdoptionRevokeTarget';
export const LAYERED_ADOPTION_REVOKE_TARGET_NOTE='撤销目标必须显式选择：'
  +'前端不自动选择任何 adoption，也不按顺序猜测目标；选定目标只改前端选择，'
  +'删除/恢复 route 一律由后端 revoke 语义决定。';
export const LAYERED_ADOPTION_REVOKE_TARGET_UNSELECTABLE='revoked：已撤销，不可作为撤销目标';
export const LAYERED_ADOPTION_SAFE_LABEL='只转印后端状态，前端不推断 safe';

const text=value=>String(value??'');
const finite=value=>Number.isFinite(Number(value))&&value!==null&&value!=='';
const fmt=(value,digits=3)=>finite(value)?Number(value).toFixed(digits):'—';
const short=value=>{const value_=text(value);return value_||'—';};

function jsonInline(value){
  try{return JSON.stringify(value===undefined?null:value);}catch(error){return String(value);}
}

function rows(items){
  return (items||[]).filter(Boolean).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item[0])
    +'</b><small>'+item[1]+'</small></span></div>').join('');
}

function twoDimensionalPath(path){
  return (path||[]).every(point=>Array.isArray(point)&&point.length===2);
}

/** 状态徽章：class 只允许 kebab-case，后端原始状态原样出现在文本里。 */
function chainBadge(state,tone){
  return '<span class="flow-badge flow-'+tone+'">'+escapeHtml(state)+'</span>';
}

// ---------------------------------------------------------------- model

/**
 * 只读投影：readiness / options / adoptions 全部来自后端 snapshot。
 * @param {object} flow published workflow snapshot
 */
export function layeredOperationalAdoptionModel(flow){
  const snapshot=flow||{};
  const readiness=snapshot.layered_operational_adoption_readiness||{};
  const validationReadiness=snapshot.layered_route_validation_readiness||{};
  const validationCollection=snapshot.layered_route_validations||{};
  const collection=snapshot.layered_operational_adoptions||{};
  const items=Array.isArray(collection.items)?collection.items:[];
  const options=Array.isArray(readiness.options)?readiness.options:[];
  const validations=Array.isArray(validationCollection.items)?validationCollection.items:[];
  return {
    readinessStatus:text(readiness.status||'not_ready'),
    algorithm:readiness.algorithm||{},
    boundaries:readiness.boundaries||{},
    options:options.map(option=>({
      validationId:text(option.validation_id),
      routeId:text(option.route_id),
      status:text(option.status),
      currentApplicability:text(option.current_applicability),
      eligible:option.eligible===true,
      reasons:option.reasons||[],
    })),
    eligibleCount:options.filter(option=>option.eligible===true).length,
    candidate:{...((validationReadiness.candidate)||{})},
    riskProfile:{...((validationReadiness.route_risk_profile)||{})},
    validation:{
      status:text(validationReadiness.status||'not_ready'),
      algorithm:validationReadiness.algorithm||{},
      /** 只有"当前适用"的那条 validation 状态才代表现在的证据；其余一律进历史。 */
      currentStatus:(function(){
        const current=validations.find(item=>item&&item.current_applicability==='current');
        return current?text(current.status):'not_evaluated';
      })(),
      currentValidationId:(function(){
        const current=validations.find(item=>item&&item.current_applicability==='current');
        return current?text(current.validation_id):'';
      })(),
      collectionStatus:text(validationCollection.status||'not_calculated'),
      staleCount:validations.filter(item=>item&&item.status==='stale').length,
    },
    collectionStatus:text(collection.status||'not_calculated'),
    count:Number.isFinite(Number(collection.count))?Number(collection.count):items.length,
    adoptions:items.map(item=>({
      adoptionId:text(item?.adoption_id),
      routeId:text(item?.route_id),
      status:text(item?.status||'published'),
      currentApplicability:text(item?.current_applicability),
      staleReason:text(item?.stale_reason),
      appliedAt:text(item?.applied_at),
      revokedAt:text(item?.revoked_at),
      validationId:text(item?.validation_id),
      validationFingerprint:text(item?.validation_fingerprint),
      candidateId:text(item?.candidate_id),
      candidateFingerprint:text(item?.candidate_fingerprint),
      projectionFingerprint:text(item?.projection_fingerprint),
      altitudeLayerId:text(item?.altitude_layer_id),
      sourceType:text(item?.source_type),
      replaceExisting:item?.replace_existing===true,
      ownership:item?.ownership||{},
      before:item?.before||{},
      after:item?.after||{},
      provenance:item?.provenance||{},
    })),
    scopeNote:LAYERED_ADOPTION_SCOPE_NOTE,
    pathNote:LAYERED_ADOPTION_PATH_LABEL,
    boundaryNote:LAYERED_ADOPTION_BOUNDARY_NOTE,
    confirmNote:LAYERED_ADOPTION_CONFIRM_NOTE,
    conflictNote:LAYERED_ADOPTION_CONFLICT_NOTE,
    safeLabel:LAYERED_ADOPTION_SAFE_LABEL,
  };
}

/**
 * 状态链 Candidate → RRP → Validation → Operational。
 *
 * 只转印后端状态：任一段都不得被前端说成"安全"或"已验证"。
 */
export function layeredAdoptionChainModel(flow){
  const model=layeredOperationalAdoptionModel(flow);
  const candidateState=text(model.candidate.status||'not_available');
  const profileState=model.riskProfile.profile_id
    ?text(model.riskProfile.status||'not_evaluated')+' / current_applicability '
      +text(model.riskProfile.current_applicability||'not_evaluated')
    :'no current profile';
  const validationState=text(model.validation.currentStatus||'not_evaluated');
  const published=model.adoptions.filter(item=>item.status==='published').length;
  const operationalState=published?'published':'not_published';
  const toneFor=(state,tone)=>{
    if(tone)return tone;
    if(state==='stale'||state==='revoked')return 'stale';
    if(state==='failed'||state==='unresolved'||state==='validation_incomplete')return state;
    if(state==='not_ready'||state==='not_evaluated'||state==='not_available'||state==='not_published')return 'pending_confirmation';
    return 'passed';
  };
  const stages=[
    {stage:'candidate',label:'Candidate',state:candidateState,tone:toneFor(candidateState),
      note:'candidate 不是 validation，也不是运行航路'},
    {stage:'route_risk_profile',label:'RRP',state:profileState,tone:toneFor(text(model.riskProfile.status)),
      note:'RRP 必须是 current，但 classification 不是 hard gate'},
    {stage:'validation',label:'Validation',state:validationState,tone:toneFor(validationState),
      note:'只有 current_applicability=current 的 validated_candidate 才可进入发布选择'},
    {stage:'operational',label:'Operational',state:operationalState,tone:toneFor(operationalState),
      note:'只有 Apply 才写 operational_routes；前端不推断 safe'},
  ];
  return {
    stages,
    candidateState,profileState,validationState,operationalState,
    currentValidationId:model.validation.currentValidationId,
    validationStatus:model.validation.status,
    staleValidationCount:model.validation.staleCount,
    publishedCount:published,
    staleCount:model.adoptions.filter(item=>item.status==='stale').length,
    revokedCount:model.adoptions.filter(item=>item.status==='revoked').length,
    neverInfersSafe:true,
  };
}

export function layeredAdoptionOptionModel(model,validationId){
  return model.options.find(option=>option.validationId===text(validationId))||null;
}

// ---------------------------------------------------------------- preview cache (module-local)

/**
 * 模块内 Preview 缓存：保存最近一次 Preview 的响应，以及它当时冻结的
 * validation / fingerprint / replace_existing。
 *
 * 它是"是否允许 Apply"的唯一依据：Apply 绝不在现场替换 expected fingerprint，
 * 也绝不用当前 checkbox 覆盖 Preview 时刻的 replace_existing 意图，
 * 更不在证据或意图变化后自动重试。
 */
let previewCache=null;
//: Apply 成功后只转印的四个字段（响应原文，不在前端推断）。
let lastApplyCache=null;
/**
 * Revoke 目标的显式选择（Select）。
 *
 * 默认是空串：面板绝不自动选择任何 adoption，也绝不按顺序猜目标。
 * 只有用户显式点选 radio 才会写入这里，重新渲染时只回填"用户已经显式选过的"那一条。
 */
let revokeTargetSelection='';

export function layeredAdoptionPreview(){return previewCache;}

export function layeredAdoptionLastApply(){return lastApplyCache;}

/** 当前显式选择的撤销目标（未显式选择时为空串）。 */
export function selectedLayeredAdoptionRevokeTargetId(){return revokeTargetSelection;}

export function clearLayeredAdoptionPreview(){previewCache=null;}

export function clearLayeredAdoptionCache(){
  previewCache=null;lastApplyCache=null;revokeTargetSelection='';
}

/** Apply 响应的原样转印：只保留契约里的四个字段。 */
export function recordLayeredAdoptionApply(result){
  const value=result&&typeof result==='object'?result:null;
  if(!value){lastApplyCache=null;return null;}
  lastApplyCache={
    adoptionId:text(value.adoption_id),
    routeId:text(value.route_id),
    routeOperatingLayerCreated:value.route_operating_layer_created===true,
    routeAltitudeProfileCreated:value.route_altitude_profile_created===true,
    departureArrivalProcedureCreated:value.departure_arrival_procedure_created===true,
  };
  return lastApplyCache;
}

/**
 * 冻结一份 Preview 响应。
 *
 * 冻结内容：validation_id / validation_fingerprint / replace_existing / publication_allowed
 * 与这份响应原文。replace_existing 优先取调用方（bind 从 checkbox 读到的真实意图），
 * 缺失时才退回后端 projection.replacement_requested —— 绝不由前端发明。
 *
 * @param {object} preview /preview 响应原文
 * @param {string|null} validationId 发起 Preview 时锚定的 validation
 * @param {{replaceExisting:boolean|null}} options 发起 Preview 时提交的 replace_existing
 */
export function cacheLayeredAdoptionPreview(preview,validationId=null,{replaceExisting=null}={}){
  const value=preview&&typeof preview==='object'?preview:null;
  if(!value){previewCache=null;return null;}
  const anchored=text(validationId)||text(value.validation_id);
  const projection=value.projection||{};
  const intent=(replaceExisting===null||replaceExisting===undefined)
    ?projection.replacement_requested===true
    :replaceExisting===true;
  previewCache={
    preview:value,
    validationId:anchored,
    // 这是 Preview 时刻由后端冻结的指纹：Apply 只允许提交它。
    validationFingerprint:text(value.validation_fingerprint||
      projection.validation_fingerprint||'')||null,
    // 这是 Preview 时刻冻结的发布意图：Apply 只允许提交它。
    replaceExisting:intent,
    publicationAllowed:value.publication_allowed===true,
    conflict:projection.conflict||null,
    cachedAt:Date.now(),
  };
  return previewCache;
}

/**
 * 缓存是否仍与当前 flow / 用户选择一致。
 *
 * 三种变化都让 Preview 过期：validation 选择变化、证据（validation fingerprint）变化、
 * 发布意图（replace_existing）变化。
 *
 * @param {object} flow 当前 snapshot
 * @param {string|null} validationId 当前 UI 选择的 validation
 * @param {{replaceExisting:boolean|undefined}} options 当前 UI 的 replace_existing；
 *   传 undefined 表示"本次调用不比较发布意图"（只用于与 UI 无关的纯证据比对）。
 */
export function layeredAdoptionPreviewState(flow,validationId=null,{replaceExisting=undefined}={}){
  const cache=previewCache;
  const wanted=text(validationId);
  const compareIntent=replaceExisting!==undefined&&replaceExisting!==null;
  const uiIntent=replaceExisting===true;
  if(!cache){
    return {present:false,expired:false,selectionChanged:false,evidenceChanged:false,intentChanged:false,
      validationId:'',validationFingerprint:null,currentFingerprint:null,
      replaceExisting:compareIntent?uiIntent:false,currentReplaceExisting:uiIntent,
      publicationAllowed:false,conflict:null,preview:null};
  }
  const currentFingerprint=layeredValidationFingerprintFromFlow(flow,cache.validationId);
  const selectionChanged=Boolean(wanted)&&wanted!==cache.validationId;
  const evidenceChanged=Boolean(cache.validationFingerprint)
    &&text(currentFingerprint)!==text(cache.validationFingerprint);
  const intentChanged=compareIntent&&uiIntent!==(cache.replaceExisting===true);
  return {
    present:true,
    expired:selectionChanged||evidenceChanged||intentChanged,
    selectionChanged,
    evidenceChanged,
    intentChanged,
    validationId:cache.validationId,
    validationFingerprint:cache.validationFingerprint,
    currentFingerprint,
    // Preview 冻结的 replace_existing：Apply 唯一被允许提交的值。
    replaceExisting:cache.replaceExisting===true,
    currentReplaceExisting:compareIntent?uiIntent:cache.replaceExisting===true,
    publicationAllowed:cache.publicationAllowed===true,
    conflict:cache.conflict,
    preview:cache.preview,
  };
}

/** 从 flow 里读取某条 validation 的 validation fingerprint（只用于比对，不用于发明新值）。 */
export function layeredValidationFingerprintFromFlow(flow,validationId){
  const wanted=text(validationId);
  if(!wanted)return null;
  const items=((flow||{}).layered_route_validations||{}).items;
  const found=(Array.isArray(items)?items:[]).find(item=>text(item?.validation_id)===wanted)||null;
  return found?text((found.fingerprints||{}).validation_fingerprint)||null:null;
}

/** Preview 请求体：只提交 validation_id 与显式 replace_existing（默认 false）。 */
export function layeredAdoptionPreviewPayload(validationId,{replaceExisting=false}={}){
  return {validation_id:text(validationId),replace_existing:replaceExisting===true};
}

/**
 * Apply 的硬守卫（纯函数，便于独立验证）。
 *
 * 事务边界：payload 里的 replace_existing 与 expected_validation_fingerprint
 * 都只来自 Preview 冻结值；options.replaceExisting 只是"当前 UI 意图"，
 * 用于检测意图是否已经偏离那次 Preview —— 偏离即拒绝，绝不覆盖。
 *
 * @param {object} flow 当前 snapshot
 * @param {string} validationId 用户在 UI 里选择的 eligible validation
 * @param {{confirmed:boolean,replaceExisting:boolean|undefined}} options 用户显式输入
 * @returns {{allowed:boolean,reason:string,payload:object|null}}
 */
export function layeredAdoptionApplyGuard(flow,validationId,{confirmed=false,replaceExisting=undefined}={}){
  const state=layeredAdoptionPreviewState(flow,validationId,{replaceExisting});
  if(!state.present)return {allowed:false,reason:'Apply 必须先 Preview：当前没有可用的 Preview 结果',payload:null};
  if(state.intentChanged){
    return {
      allowed:false,
      reason:LAYERED_ADOPTION_INTENT_CHANGED+'（Preview 冻结的 replace_existing='
        +String(state.replaceExisting)+'，与当前选择 '+String(state.currentReplaceExisting)+' 不一致）',
      payload:null,
    };
  }
  if(state.expired){
    return {
      allowed:false,
      reason:LAYERED_ADOPTION_EVIDENCE_CHANGED+'（Preview 锚定的 validation / fingerprint 与当前证据不一致）',
      payload:null,
    };
  }
  if(!state.publicationAllowed){
    return {allowed:false,reason:'Preview 的 publication_allowed 不是 true，禁止 Apply',payload:null};
  }
  if(confirmed!==true){
    return {allowed:false,reason:'Apply 需要显式勾选确认（confirmed=true）',payload:null};
  }
  if(state.conflict&&state.replaceExisting!==true){
    return {allowed:false,reason:'route_id 冲突：'+LAYERED_ADOPTION_CONFLICT_NOTE,payload:null};
  }
  return {
    allowed:true,
    reason:'',
    payload:{
      validation_id:text(validationId),
      confirmed:true,
      // 只提交 Preview 冻结的 replace_existing：不读当前 checkbox、不做临时改写。
      replace_existing:state.replaceExisting===true,
      // 只提交 Preview 时刻冻结的指纹，绝不在这里读取新的 flow 值。
      expected_validation_fingerprint:state.validationFingerprint,
    },
  };
}

/** Revoke 请求体：必须显式 confirmed=true，且只提交 adoption_id。 */
export function layeredAdoptionRevokePayload(adoptionId,{confirmed=false}={}){
  return {adoption_id:text(adoptionId),confirmed:confirmed===true};
}

export function layeredAdoptionRevokeGuard(adoptionId,{confirmed=false}={}){
  const wanted=text(adoptionId);
  if(!wanted)return {
    allowed:false,
    reason:'没有可撤销的 adoption：请先显式选择一个 status != revoked 的撤销目标',
    payload:null,
  };
  if(confirmed!==true)return {allowed:false,reason:'Revoke 需要显式确认（confirmed=true）',payload:null};
  return {allowed:true,reason:'',payload:layeredAdoptionRevokePayload(wanted,{confirmed:true})};
}

/**
 * 可撤销目标：只有 status != revoked 的 adoption 才能被选中。
 *
 * 它是纯函数投影，绝不排序成"最近一条"：调用方不选择时就没有目标。
 */
export function layeredAdoptionRevokeTargets(model){
  const items=model&&Array.isArray(model.adoptions)?model.adoptions:[];
  return items.filter(item=>item&&item.status!=='revoked');
}

// ---------------------------------------------------------------- render

function legacyBlock(){
  return wbBlock(LAYERED_ADOPTION_LEGACY_LABEL,
    '<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_LEGACY_NOTE)+'</div>'
    +'<div class="button-row"><button class="secondary" id="scenarioRoutes">生成场景航路（all-pairs，兼容）</button>'
    +'<button class="primary" id="operationalRoutes">生成运行航路</button></div>');
}

function chainBlock(flow,model){
  const chain=layeredAdoptionChainModel(flow);
  const chainRows=chain.stages.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(item.label)+'</b> '+chainBadge(item.state,item.tone)
    +'<small>'+escapeHtml(item.note)+'</small></span></div>').join('');
  return '<h4>状态链 Candidate → RRP → Validation → Operational</h4>'
    +'<div class="scroll-list route-list">'+chainRows+'</div>'
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_SAFE_LABEL)
    +'。published '+escapeHtml(String(chain.publishedCount))
    +' · stale '+escapeHtml(String(chain.staleCount))
    +' · revoked '+escapeHtml(String(chain.revokedCount))
    +' · stale validation '+escapeHtml(String(chain.staleValidationCount))+'</div>'
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_SCOPE_NOTE)+'</div>';
}

function optionBlock(model,selected){
  const optionRows=model.options.length?model.options.map(option=>{
    const checked=option.validationId===text(selected)?' checked':'';
    const disabled=option.eligible?'':' disabled';
    return '<label class="check-row"><input type="radio" name="layeredAdoptionValidation" id="layeredAdoptionOption_'
      +escapeHtml(option.validationId)+'" value="'+escapeHtml(option.validationId)+'"'+checked+disabled+'>'
      +escapeHtml(option.validationId)+' · '+statusBadge(option.status)
      +' · current_applicability '+escapeHtml(short(option.currentApplicability))
      +' · eligible '+escapeHtml(String(option.eligible))
      +' · route '+escapeHtml(short(option.routeId))
      +' · reasons '+escapeHtml(jsonInline(option.reasons||[]))+'</label>';
  }).join(''):'<div class="empty-note">尚无 validation 记录：请先在「结果 → 可行性与净空」完成连续验证</div>';
  return '<h4>可选择的 eligible validation</h4><div class="scroll-list route-list">'+optionRows+'</div>'
    +'<div class="parameter-note">只有 status=validated_candidate 且 current_applicability=current 且真实来源链就绪的 '
    +'validation 才 eligible；Select 不等于 Validate，也不等于 Apply。</div>';
}

function intentNote(state){
  // 发布意图变化的显式提示：由 bind 在用户改变 replace_existing 时点亮。
  return '<div class="parameter-note" id="layeredAdoptionIntentChanged"'
    +(state.intentChanged?'':' hidden')+'>'
    +(state.intentChanged?escapeHtml(LAYERED_ADOPTION_INTENT_CHANGED):'')+'</div>';
}

function conflictBlock(state){
  const conflict=state.conflict;
  if(!conflict)return '<div class="parameter-note">route_id 无冲突：Apply 不需要 replace_existing。</div>'
    +intentNote(state);
  // Preview 冻结的 replace_existing 回填到 checkbox：用户看到的初始勾选状态就是那份 Preview 的意图。
  const checked=state.replaceExisting===true?' checked':'';
  return '<div class="parameter-note"><b>route_id 冲突</b>：route '
    +escapeHtml(short(conflict.route_id))+' · existing_status '+escapeHtml(short(conflict.existing_status))
    +' · existing_source_type '+escapeHtml(short(conflict.existing_source_type))
    +'<br>'+escapeHtml(LAYERED_ADOPTION_CONFLICT_NOTE)+'</div>'
    +'<label class="check-row"><input type="checkbox" id="layeredAdoptionReplaceExisting"'+checked+'>'
    +'显式允许替换既有 route_id（replace_existing，默认 false；不会自动覆盖）</label>'
    +'<div class="parameter-note">Preview 冻结的 replace_existing '
    +escapeHtml(String(state.replaceExisting===true))
    +'：改变它会立即让这份 Preview 过期，必须重新 Preview 才能 Apply。</div>'
    +intentNote(state);
}

function previewBlock(state){
  if(!state.present)return '<div class="empty-note">尚无 Preview：Preview 只读、不写 state，也不会改动 operational_routes。</div>';
  const preview=state.preview||{};
  const projection=preview.projection||{};
  const route=projection.route||{};
  const assignment=projection.route_operating_layer||{};
  const altitude=projection.altitude_representation||{};
  const path=route.path||[];
  const expiredLabel=state.intentChanged&&!state.evidenceChanged&&!state.selectionChanged
    ?LAYERED_ADOPTION_INTENT_CHANGED:LAYERED_ADOPTION_EVIDENCE_CHANGED;
  return '<h4>Preview（不写 state）'+(state.expired?' · <b>'
    +escapeHtml(expiredLabel)+'</b>':'')+'</h4>'
    +'<div class="scroll-list route-list">'+rows([
      ['side_effects',escapeHtml(String(preview.side_effects===true))+'（Preview 绝不写 state）'],
      ['publication_allowed',statusBadge(preview.publication_allowed===true?'passed':'not_ready')
        +' '+escapeHtml(String(preview.publication_allowed===true))],
      ['status / preview_fingerprint',escapeHtml(short(preview.status))+' · '
        +escapeHtml(short(preview.preview_fingerprint))],
      ['validation_id / validation fingerprint',escapeHtml(short(preview.validation_id||state.validationId))
        +' · '+escapeHtml(short(state.validationFingerprint))],
      ['Preview 冻结的 replace_existing（Apply 只提交它）',escapeHtml(String(state.replaceExisting===true))],
      ['projection route','route_id '+escapeHtml(short(route.route_id))
        +' · kind '+escapeHtml(short(route.kind))+' · status '+escapeHtml(short(route.status))
        +'<br>顶点数 '+escapeHtml(String(path.length))
        +' · 二维 [lon, lat] '+escapeHtml(String(twoDimensionalPath(path)))
        +' · path_crs '+escapeHtml(short(route.path_crs))],
      ['RouteOperatingLayer','altitude_layer_id '+escapeHtml(short(assignment.altitude_layer_id))
        +' · operating_mode '+escapeHtml(short(assignment.operating_mode))
        +' · vertical_reference '+escapeHtml(short(assignment.vertical_reference))
        +' · confirmed '+escapeHtml(String(assignment.confirmed===true))
        +'<br>adoption_owned '+escapeHtml(String(assignment.adoption_owned===true))],
      ['conflict',escapeHtml(jsonInline(projection.conflict||null))],
      ['projection_fingerprint',escapeHtml(short(projection.projection_fingerprint))],
      ['current_applicability',escapeHtml(short((preview.validation||{}).current_applicability
        ||(preview.current_applicability)))+' · 只有 current 才可 Apply'],
    ])+'</div>'
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_PATH_LABEL)+'<br>'
    +escapeHtml(LAYERED_ADOPTION_BOUNDARY_NOTE)+'<br>'
    +'altitude 表示：carried_by '+escapeHtml(short(altitude.carried_by))
    +' · in_crs84_third_coordinate '+escapeHtml(String(altitude.in_crs84_third_coordinate===true))
    +' · route_altitude_profile_created '+escapeHtml(String(altitude.route_altitude_profile_created===true))
    +'</div>';
}

/**
 * Apply 按钮的可用性：只有"当前 Preview + 未过期 + publication_allowed=true"才静态启用。
 * 显式确认由 guard 与 bind 的实时刷新共同把守（未勾选时同样禁用）。
 */
function applyEnabledByPreview(state){
  return state.present&&!state.expired&&state.publicationAllowed===true;
}

function applyBlock(flow,model,selected){
  const state=layeredAdoptionPreviewState(flow,selected);
  const summary='<div class="scroll-list route-list">'+rows([
    ['当前选择（Select）',escapeHtml(short(selected))+' · eligible '
      +escapeHtml(String((layeredAdoptionOptionModel(model,selected)||{}).eligible===true))],
    ['Preview 锚定 validation',escapeHtml(short(state.validationId))
      +(state.expired?' · <b>'+escapeHtml(state.intentChanged&&!state.evidenceChanged&&!state.selectionChanged
        ?LAYERED_ADOPTION_INTENT_CHANGED:LAYERED_ADOPTION_EVIDENCE_CHANGED)+'</b>':'')],
    ['Preview fingerprint（Apply 将提交它）',escapeHtml(short(state.validationFingerprint))],
    ['当前 flow 的同一条 validation fingerprint',escapeHtml(short(state.currentFingerprint))
      +(state.evidenceChanged?' · 已变化':' · 未变化')],
    ['Preview 冻结的 replace_existing（Apply 将提交它）',escapeHtml(String(state.replaceExisting===true))
      +(state.intentChanged?' · 与当前 checkbox 不一致':'')],
    ['publication_allowed',escapeHtml(String(state.publicationAllowed))],
    ['route 冲突（默认 replace_existing=false）',escapeHtml(jsonInline(state.conflict||null))],
  ])+'</div>';
  return '<h4>Apply（必须显式确认）</h4>'
    +'<div id="layeredAdoptionSelectedValidation" data-selected-validation="'+escapeHtml(text(selected))
    +'" data-preview-fingerprint="'+escapeHtml(short(state.validationFingerprint))
    +'" data-preview-replace-existing="'+escapeHtml(String(state.replaceExisting===true))
    +'" data-preview-expired="'+escapeHtml(String(state.expired))+'"></div>'
    +summary
    +conflictBlock(state)
    +'<label class="check-row"><input type="checkbox" id="layeredAdoptionApplyConfirmed">'
    +'我已复核当前 Preview 内容，确认执行 Apply（默认不勾选）</label>'
    +'<div class="button-row"><button class="secondary" id="previewLayeredAdoption">Preview（只读）</button>'
    +'<button class="primary" id="applyLayeredAdoption"'+(applyEnabledByPreview(state)?'':' disabled')+'>'
    +'Apply（需当前 Preview + 显式确认）</button></div>'
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_CONFIRM_NOTE)+'<br>'
    +'若 fingerprint 或 replace_existing 已变化：'+escapeHtml(LAYERED_ADOPTION_EVIDENCE_CHANGED)
    +' / '+escapeHtml(LAYERED_ADOPTION_INTENT_CHANGED)
    +'，前端不会自动重试、不会偷偷换成新的 flow 值，也不会用当前 checkbox 覆盖 Preview 意图。</div>';
}

/**
 * 撤销目标区块：每个 status != revoked 的 adoption 单独一个 radio。
 *
 * 绝不自动选择任何 adoption；revoked 记录只列出、不可作为目标。
 */
function revokeTargetBlock(model){
  const targets=layeredAdoptionRevokeTargets(model);
  const wanted=text(revokeTargetSelection);
  // 目标可能已经被后端撤销或不再存在：渲染时校正，避免陈旧选择继续指向 revoked 记录。
  const stillValid=targets.some(item=>item.adoptionId===wanted);
  if(!stillValid)revokeTargetSelection='';
  const selected=stillValid?wanted:'';
  const targetRows=model.adoptions.length?model.adoptions.map(item=>{
    const label=escapeHtml(short(item.adoptionId))
      +' · route '+escapeHtml(short(item.routeId))
      +' · status '+escapeHtml(short(item.status))
      +' · applicability '+escapeHtml(short(item.currentApplicability))
      +' · ownership route_owned '+escapeHtml(String((item.ownership||{}).route_owned===true))
      +' / route_operating_layer_owned '
      +escapeHtml(String((item.ownership||{}).route_operating_layer_owned===true));
    if(item.status==='revoked'){
      // revoked 记录不可选：不渲染可选 radio（只在历史里保留）。
      return '<label class="check-row"><input type="radio" disabled>'+label
        +' · '+escapeHtml(LAYERED_ADOPTION_REVOKE_TARGET_UNSELECTABLE)+'</label>';
    }
    const checked=item.adoptionId===selected?' checked':'';
    return '<label class="check-row"><input type="radio" name="'+escapeHtml(LAYERED_ADOPTION_REVOKE_TARGET_NAME)
      +'" id="layeredAdoptionRevokeTarget_'+escapeHtml(item.adoptionId)
      +'" value="'+escapeHtml(item.adoptionId)+'"'+checked+'>'+label+'</label>';
  }).join(''):'<div class="empty-note">尚无 adoption：没有可撤销的发布记录。</div>';
  return '<h4>撤销目标（必须显式选择，不会自动选择）</h4>'
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_REVOKE_TARGET_NOTE)+'</div>'
    +'<div id="layeredAdoptionRevokeTargetMarker" data-selected-revoke-target="'+escapeHtml(selected)+'"></div>'
    +'<div class="scroll-list route-list">'+targetRows+'</div>'
    +'<label class="check-row"><input type="checkbox" id="layeredAdoptionRevokeConfirmed">'
    +'确认撤销上面显式选中的 adoption（'+escapeHtml(LAYERED_ADOPTION_REVOKE_LABEL)+'）</label>'
    +'<div class="button-row"><button class="secondary" id="revokeLayeredAdoption"'
    +(selected?'':' disabled')+'>撤销本次发布（需显式目标 + 显式确认）</button></div>'
    +'<div class="parameter-note" id="layeredAdoptionRevokeTargetNote">'
    +(selected?'当前撤销目标 '+escapeHtml(selected):'尚未显式选择撤销目标：Revoke 被禁用')
    +'</div>';
}

function resultBlock(model){
  const applied=layeredAdoptionLastApply();
  if(!applied)return '<h4>Apply 结果转印</h4><div class="empty-note">'
    +'尚无 Apply：本会话还没有可转印的 adoption 结果（Apply 才写 operational_routes）</div>';
  return '<h4>Apply 结果转印（后端响应原文，不做任何推断）</h4>'
    +'<div class="scroll-list route-list">'+rows([
      ['adoption_id',escapeHtml(short(applied.adoptionId))],
      ['route_id',escapeHtml(short(applied.routeId))],
      ['route_operating_layer_created',escapeHtml(String(applied.routeOperatingLayerCreated))],
      ['route_altitude_profile_created',escapeHtml(String(applied.routeAltitudeProfileCreated))],
      ['departure_arrival_procedure_created',escapeHtml(String(applied.departureArrivalProcedureCreated))],
      ['当前 adoption 记录数',escapeHtml(String(model.count))],
    ])+'</div>'
    +'<div class="parameter-note">Apply 只写 operational_routes 与 RouteOperatingLayer；'
    +'不创建 RouteAltitudeProfile，也不创建 Departure/Arrival procedure。</div>';
}

function historyBlock(model){
  if(!model.adoptions.length)return '';
  const historyRows=model.adoptions.map(item=>'<div class="list-row route-row"><span><b>'
    +escapeHtml(short(item.adoptionId))+'</b> '+statusBadge(item.status)
    +' · applicability '+escapeHtml(short(item.currentApplicability))
    +(item.status==='published'?'':' · stale_reason '+escapeHtml(short(item.staleReason)))
    +'<small>route '+escapeHtml(short(item.routeId))+' · validation '+escapeHtml(short(item.validationId))
    +' · validation fingerprint '+escapeHtml(short(item.validationFingerprint))
    +' · projection fingerprint '+escapeHtml(short(item.projectionFingerprint))+'</small>'
    +'<small>ownership route_owned '+escapeHtml(String((item.ownership||{}).route_owned===true))
    +' · route_operating_layer_owned '+escapeHtml(String((item.ownership||{}).route_operating_layer_owned===true))
    +' · replace_existing '+escapeHtml(String(item.replaceExisting))+'</small>'
    +'<small>applied_at '+escapeHtml(short(item.appliedAt))
    +(item.revokedAt?' · revoked_at '+escapeHtml(short(item.revokedAt)):'')+'</small>'
    +'</span></div>').join('');
  return '<h4>adoption 历史（published / stale / revoked / superseded 全部保留）</h4>'
    +'<div class="parameter-note">collection status '+statusBadge(model.collectionStatus)
    +' · 共 '+escapeHtml(String(model.count))+' 条。'+escapeHtml(LAYERED_ADOPTION_REVOKE_LABEL)
    +'；前端绝不直接删除 operational route。</div>'
    +'<div class="scroll-list route-list">'+historyRows+'</div>';
}

/**
 * 渲染 Layered Candidate Operational Adoption 面板。
 * @param {object} flow published workflow snapshot
 * @param {string} selected 当前用户选择的 validation_id（Select）
 */
export function renderLayeredOperationalAdoption(flow,selected=''){
  const model=layeredOperationalAdoptionModel(flow);
  const chosen=text(selected)
    ||(model.options.find(option=>option.eligible)||{}).validationId||'';
  return '<div class="parameter-note"><b>'+escapeHtml(LAYERED_ADOPTION_PUBLISH_LABEL)+'</b><br>'
    +escapeHtml(LAYERED_ADOPTION_SCOPE_NOTE)+'</div>'
    +chainBlock(flow,model)
    // path / 高度 / profile 边界与是否有 Preview 无关：始终呈现。
    +'<div class="parameter-note">'+escapeHtml(LAYERED_ADOPTION_PATH_LABEL)+'<br>'
    +escapeHtml(LAYERED_ADOPTION_BOUNDARY_NOTE)+'<br>'
    +escapeHtml(LAYERED_ADOPTION_CONFLICT_NOTE)+'</div>'
    +optionBlock(model,chosen)
    +previewBlock(layeredAdoptionPreviewState(flow,chosen))
    +applyBlock(flow,model,chosen)
    +revokeTargetBlock(model)
    +resultBlock(model)
    +historyBlock(model)
    +wbDisclosure('ownership / before-after 详细证据',
      '<div class="scroll-list route-list">'+(model.adoptions.length?model.adoptions.map(item=>
        '<div class="list-row"><span><b>'+escapeHtml(short(item.adoptionId))+'</b><small>ownership '
        +escapeHtml(jsonInline(item.ownership))+'</small><small>before '+escapeHtml(jsonInline(item.before))
        +'</small><small>after '+escapeHtml(jsonInline(item.after))+'</small><small>provenance '
        +escapeHtml(jsonInline(item.provenance))+'</small></span></div>').join('')
        :'<div class="empty-note">尚无 adoption 证据</div>')+'</div>');
}

//: 「操作 → 运行航路」里 Layered 发布区的 section 标题（由 step03 组合使用）。
export const LAYERED_ADOPTION_SECTION_TITLE=LAYERED_ADOPTION_PUBLISH_LABEL;

/**
 * 用户当前选择（Select）。
 *
 * 面板自身在隐藏节点里记录当前选择（data-selected-validation），radio 的 change 事件
 * 同步它。这里优先读该节点，避免依赖 DOM 查询顺序；只有当节点缺席时才回退到 radio。
 */
export function selectedLayeredAdoptionValidation(c){
  if(c&&typeof c.$==='function'){
    const marker=c.$('layeredAdoptionSelectedValidation');
    const value=marker&&marker.dataset?text(marker.dataset.selectedValidation):'';
    if(value)return value;
  }
  const root=globalThis.document;
  const groups=root&&typeof root.querySelectorAll==='function'
    ?root.querySelectorAll('input[name="layeredAdoptionValidation"]'):[];
  for(const node of groups||[])if(node&&node.checked)return text(node.value);
  return '';
}

/** 把用户选择写回面板自己的标记节点（Select 只改前端选择，不触发任何后端动作）。 */
export function syncSelectedLayeredAdoptionValidation(c,validationId){
  if(!c||typeof c.$!=='function')return '';
  const marker=c.$('layeredAdoptionSelectedValidation');
  if(marker&&marker.dataset)marker.dataset.selectedValidation=text(validationId);
  return text(validationId);
}

/**
 * 当前 UI 的 replace_existing 意图。
 *
 * 冲突未出现时 checkbox 不渲染 —— 此时意图是 false（协议：首次无已知冲突 replace=false）。
 */
export function selectedLayeredAdoptionReplaceExisting(c){
  if(!c||typeof c.$!=='function')return false;
  const node=c.$('layeredAdoptionReplaceExisting');
  return Boolean(node&&node.checked===true);
}

/**
 * 当前显式选择的撤销目标（Select）。
 *
 * 与 validation 选择同构：优先读模块自己的标记节点，节点缺席时才回退到 radio。
 * 绝不自动选择、绝不回退到"最近一条 adoption"。
 */
export function selectedLayeredAdoptionRevokeTarget(c){
  if(c&&typeof c.$==='function'){
    const marker=c.$('layeredAdoptionRevokeTargetMarker');
    const value=marker&&marker.dataset?text(marker.dataset.selectedRevokeTarget):'';
    if(value)return value;
  }
  const root=globalThis.document;
  const groups=root&&typeof root.querySelectorAll==='function'
    ?root.querySelectorAll('input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]'):[];
  for(const node of groups||[])if(node&&node.checked)return text(node.value);
  return '';
}

/**
 * 记录用户显式选择的撤销目标。
 *
 * 只有 status != revoked 且在可撤销目标集合里的 adoption 才被接受：
 * 其余输入一律视为"未选择"（空串），避免用一个不存在的目标去调后端。
 */
export function syncSelectedLayeredAdoptionRevokeTarget(c,adoptionId,model=null){
  const wanted=text(adoptionId);
  const targets=layeredAdoptionRevokeTargets(
    model||layeredOperationalAdoptionModel(c&&typeof c.flow==='function'?c.flow():null));
  const accepted=targets.some(item=>item.adoptionId===wanted)?wanted:'';
  revokeTargetSelection=accepted;
  if(c&&typeof c.$==='function'){
    const marker=c.$('layeredAdoptionRevokeTargetMarker');
    if(marker&&marker.dataset)marker.dataset.selectedRevokeTarget=accepted;
    const note=c.$('layeredAdoptionRevokeTargetNote');
    if(note)note.textContent=accepted?'当前撤销目标 '+accepted:'尚未显式选择撤销目标：Revoke 被禁用';
  }
  return accepted;
}

/**
 * 实时刷新按钮可用性与"意图已变化"提示。
 *
 * 渲染是无状态字符串：用户在 checkbox / radio 上的改动不会自动重渲染，
 * 因此由这里把"当前 Preview + 未过期 + publication_allowed + 显式确认"的结论写回 DOM。
 * 它只改前端禁用状态与提示文案，不触发任何后端动作，也不改 Preview 缓存。
 */
export function refreshLayeredAdoptionAffordances(c){
  const flow=typeof c?.flow==='function'?c.flow():null;
  const validationId=selectedLayeredAdoptionValidation(c);
  const state=layeredAdoptionPreviewState(flow,validationId,
    {replaceExisting:selectedLayeredAdoptionReplaceExisting(c)});
  const confirmed=Boolean(c&&typeof c.$==='function'&&c.$('layeredAdoptionApplyConfirmed')
    &&c.$('layeredAdoptionApplyConfirmed').checked);
  const applyNode=c&&typeof c.$==='function'?c.$('applyLayeredAdoption'):null;
  const applyEnabled=state.present&&!state.expired&&state.publicationAllowed===true&&confirmed;
  if(applyNode&&'disabled' in applyNode)applyNode.disabled=!applyEnabled;
  // 三种过期原因都必须在 UI 上说清楚：换 validation 或改 replace 意图 → 发布意图已变化；
  // 证据（fingerprint）变化 → 证据已变化。
  const message=state.intentChanged||state.selectionChanged
    ?LAYERED_ADOPTION_INTENT_CHANGED
    :(state.evidenceChanged?LAYERED_ADOPTION_EVIDENCE_CHANGED:'');
  const intentNode=c&&typeof c.$==='function'?c.$('layeredAdoptionIntentChanged'):null;
  if(intentNode){
    intentNode.textContent=message;
    if('hidden' in intentNode)intentNode.hidden=!message;
  }
  const target=selectedLayeredAdoptionRevokeTarget(c);
  const revokeNode=c&&typeof c.$==='function'?c.$('revokeLayeredAdoption'):null;
  if(revokeNode&&'disabled' in revokeNode)revokeNode.disabled=!target;
  return {applyEnabled,intentChanged:state.intentChanged===true,expired:state.expired===true,
    publicationAllowed:state.publicationAllowed===true,revokeTarget:target,message};
}

/**
 * main.js 的 actionButton 在 handler 结束后会把 disabled 复位，
 * 因此在下一个微任务里再刷新一次，保证"Preview 已消费/已过期"的禁用状态不被抹掉。
 */
function refreshLayeredAdoptionAffordancesSoon(c){
  refreshLayeredAdoptionAffordances(c);
  if(typeof queueMicrotask==='function')queueMicrotask(()=>refreshLayeredAdoptionAffordances(c));
}

/**
 * 绑定 Preview / Apply / Revoke。
 *
 * 副作用边界：Preview 不写 state（后端强制 side_effects=false），
 * 只有 /apply 与 /revoke 会写 operational state。
 *
 * 事务边界：任何 Select / replace_existing 变化都让旧 Preview 立即过期，
 * Apply 只提交 Preview 冻结的 replace_existing 与 validation fingerprint。
 */
export function bindLayeredOperationalAdoption(c){
  if(!c||typeof c.$!=='function')return;
  // Select 只改前端选择并让旧 Preview 失效：不触发任何后端动作。
  const root=globalThis.document;
  const options=root&&typeof root.querySelectorAll==='function'
    ?root.querySelectorAll('input[name="layeredAdoptionValidation"]'):[];
  for(const node of options||[]){
    node.onchange=()=>{
      if(!node.checked)return;
      syncSelectedLayeredAdoptionValidation(c,node.value);
      refreshLayeredAdoptionAffordances(c);
    };
  }
  // 撤销目标：只有用户显式点选才写入；revoked 记录不渲染可选 radio。
  const revokeOptions=root&&typeof root.querySelectorAll==='function'
    ?root.querySelectorAll('input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]'):[];
  for(const node of revokeOptions||[]){
    node.onchange=()=>{
      if(!node.checked)return;
      syncSelectedLayeredAdoptionRevokeTarget(c,node.value);
      refreshLayeredAdoptionAffordances(c);
    };
  }
  // 发布意图改变 → 旧 Preview 立即不可用于 Apply。
  const replaceNode=c.$('layeredAdoptionReplaceExisting');
  if(replaceNode)replaceNode.onchange=()=>refreshLayeredAdoptionAffordances(c);
  const applyConfirmedNode=c.$('layeredAdoptionApplyConfirmed');
  if(applyConfirmedNode)applyConfirmedNode.onchange=()=>refreshLayeredAdoptionAffordances(c);
  const revokeConfirmedNode=c.$('layeredAdoptionRevokeConfirmed');
  if(revokeConfirmedNode)revokeConfirmedNode.onchange=()=>refreshLayeredAdoptionAffordances(c);
  if(c.$('previewLayeredAdoption'))c.actionButton('previewLayeredAdoption',async()=>{
    const validationId=selectedLayeredAdoptionValidation(c);
    if(!validationId)throw new Error('请先选择一个 eligible validation');
    // 第二次 Preview 必须读取 checkbox：replace_existing 的意图在这里冻结。
    const replaceExisting=selectedLayeredAdoptionReplaceExisting(c);
    const response=await c.resourceAction('/api/layered-operational-adoptions/preview',
      layeredAdoptionPreviewPayload(validationId,{replaceExisting}));
    // 只缓存响应：Preview 时刻的指纹与 replace_existing 就是 Apply 唯一被允许提交的值。
    cacheLayeredAdoptionPreview(response,validationId,{replaceExisting});
    refreshLayeredAdoptionAffordancesSoon(c);
  });
  if(c.$('applyLayeredAdoption'))c.actionButton('applyLayeredAdoption',async()=>{
    const validationId=selectedLayeredAdoptionValidation(c);
    const confirmed=c.$('layeredAdoptionApplyConfirmed')?c.$('layeredAdoptionApplyConfirmed').checked:false;
    // checkbox 只是"当前 UI 意图"，guard 用它判断意图是否已偏离 Preview，绝不覆盖冻结值。
    const replaceExisting=selectedLayeredAdoptionReplaceExisting(c);
    const guard=layeredAdoptionApplyGuard(c.flow(),validationId,{confirmed,replaceExisting});
    if(!guard.allowed)throw new Error(guard.reason);
    const result=await c.resourceAction('/api/layered-operational-adoptions/apply',guard.payload);
    recordLayeredAdoptionApply(result);
    // Apply 成功后 Preview 立即作废：不允许用同一个 Preview 二次 Apply。
    clearLayeredAdoptionPreview();
    refreshLayeredAdoptionAffordancesSoon(c);
  });
  if(c.$('revokeLayeredAdoption'))c.actionButton('revokeLayeredAdoption',async()=>{
    // 目标只能来自用户显式选择：绝不按列表顺序猜一条。
    const target=selectedLayeredAdoptionRevokeTarget(c);
    const confirmed=c.$('layeredAdoptionRevokeConfirmed')?c.$('layeredAdoptionRevokeConfirmed').checked:false;
    const guard=layeredAdoptionRevokeGuard(target,{confirmed});
    if(!guard.allowed)throw new Error(guard.reason);
    await c.resourceAction('/api/layered-operational-adoptions/revoke',guard.payload);
  });
  // 装载后立刻按"当前 Preview + 当前勾选状态"刷新一次按钮状态：
  // 未勾选确认、Preview 已过期或已被消费时，Apply 一律显示为 disabled。
  refreshLayeredAdoptionAffordances(c);
}
