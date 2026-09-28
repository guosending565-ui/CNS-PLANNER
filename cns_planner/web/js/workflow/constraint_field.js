/**
 * =========================================================================
 * Planning Constraint Field 前端模块（B4X §10 / §11 / §12）
 * =========================================================================
 *
 * 职责
 * ----
 *  - 通过**只读 HTTP 路径**读取约束场摘要与地图紧凑结果：
 *      ``GET /api/planning-constraint-field?altitude_layer_id=…``       summary
 *      ``GET /api/planning-constraint-field/map?altitude_layer_id=…``   紧凑 cells
 *    浏览器**不接触任何文件系统路径**（sidecar 由后端读取）。
 *  - 把 raw enum 转成集中式中文（`presentation.js`）：可通行 / 障碍 / 证据不足。
 *  - 提供地图覆盖层需要的数据模型与图层开关语义（障碍优先、unknown 计数永不被藏）。
 *
 * 硬边界（与 B3X/B4X 一致）
 * ------------------------
 *  - **风险场 ≠ 约束场**：本模块只处理 feasibility（能不能飞），完全不读风险评分；
 *  - `unknown` **绝不等价于** pass：即使地图默认不画 unknown，状态卡也必须给出计数与提示；
 *  - 不在前端重算任何约束判定：三态、blocked_by、计数全部来自后端；
 *  - 24,300 格量级：地图接口只返回 ``grid_id / outcome / blocked_by`` 三个字段，
 *    几何由前端既有的 ``grid_id → cell`` 索引补齐，绝不重复下发 GeoJSON。
 */
import {
  constraintOutcomeText,constraintBlockerText,constraintBlockerList,
  constraintUnknownSummary,constraintFreshness,CONSTRAINT_OUTCOME_COLOR,
  PROVISIONAL_TRAVERSAL_SCOPE
} from './presentation.js';

/** 只读读取端点（B4X 新增；仅用于展示）。 */
export const CONSTRAINT_FIELD_SUMMARY_ENDPOINT='/api/planning-constraint-field';
export const CONSTRAINT_FIELD_MAP_ENDPOINT='/api/planning-constraint-field/map';
/** PCF 前置配置（provisional 穿越策略 / confirmed_none 显式声明）读写端点。 */
export const CONSTRAINT_CONFIGURATION_ENDPOINT='/api/planning-constraint-field/configuration';

/** 地图图层开关 id（必须与 index.html 中的 checkbox id 逐字一致）。 */
export const CONSTRAINT_LAYER_ID='altitudeConstraintLayer';
/** 可通行 / 证据不足子开关：默认关闭，避免遮挡障碍单元。 */
export const CONSTRAINT_PASS_LAYER_ID='altitudeConstraintPassLayer';
export const CONSTRAINT_UNKNOWN_LAYER_ID='altitudeConstraintUnknownLayer';

/** 约束场三态的默认地图显示策略（B4X §12）。 */
export const CONSTRAINT_LAYER_DEFAULTS={
  blocked:true,
  unknown:false,
  pass:false
};

/** 约束场地图配色（与 presentation.js 同源，禁止第二套颜色）。 */
export const CONSTRAINT_LAYER_COLORS=CONSTRAINT_OUTCOME_COLOR;

// ---- 读取 -------------------------------------------------------------------

/** summary 请求 URL（只带 altitude_layer_id；几何不在后端重复下发）。 */
export function constraintSummaryUrl(altitudeLayerId){
  const id=String(altitudeLayerId||'').trim();
  if(!id)return '';
  return CONSTRAINT_FIELD_SUMMARY_ENDPOINT+'?'+new URLSearchParams({altitude_layer_id:id});
}

/** 地图请求 URL（可带 bbox；bbox 为空时不加参数）。 */
export function constraintMapUrl(altitudeLayerId,bbox=null){
  const id=String(altitudeLayerId||'').trim();
  if(!id)return '';
  const query=new URLSearchParams({altitude_layer_id:id});
  if(Array.isArray(bbox)&&bbox.length===4)query.set('bbox',bbox.map(Number).join(','));
  return CONSTRAINT_FIELD_MAP_ENDPOINT+'?'+query.toString();
}

/**
 * 把地图接口响应规范成绘制模型。
 *
 * 关键：``status`` 只有 ``passed`` 时才给出可绘制 cells；``cells_unavailable`` /
 * ``not_calculated`` / ``altitude_layer_required`` 一律返回空 cells 并保留原因，
 * 绝不把"读不到明细"画成"全部可通行"。
 */
export function constraintMapModel(response){
  const data=response&&typeof response==='object'?response:{};
  const status=String(data.status||'not_calculated');
  const usable=status==='passed';
  return {
    status,
    usable,
    reason:String(data.reason||''),
    altitudeLayerId:data.altitude_layer_id?String(data.altitude_layer_id):'',
    fieldId:data.field_id?String(data.field_id):'',
    nominalAltitudeM:Number.isFinite(Number(data.nominal_altitude_m))?Number(data.nominal_altitude_m):null,
    verticalReference:data.vertical_reference?String(data.vertical_reference):'',
    fingerprint:data.constraint_field_fingerprint?String(data.constraint_field_fingerprint):'',
    counts:data.counts&&typeof data.counts==='object'?data.counts:null,
    totalCount:Number(data.total_count)||(Array.isArray(data.cells)?data.cells.length:0),
    bbox:Array.isArray(data.bbox)?data.bbox:null,
    geometrySource:String(data.geometry_source||''),
    cells:usable&&Array.isArray(data.cells)
      ?data.cells.filter(item=>item&&item.grid_id).map(item=>({
        gridId:String(item.grid_id),
        outcome:['pass','blocked','unknown'].includes(String(item.outcome))?String(item.outcome):'unknown',
        blockedBy:Array.isArray(item.blocked_by)?item.blocked_by.map(String):[]
      }))
      :[]
  };
}

/** 读取 summary 与地图结果（两个只读 GET；失败时抛出可读中文错误）。 */
export async function loadConstraintField(api,altitudeLayerId,{bbox=null}={}){
  const summaryUrl=constraintSummaryUrl(altitudeLayerId);
  if(!summaryUrl)throw new Error('请先选择固定巡航高度层');
  const summary=await api(summaryUrl);
  const mapUrl=constraintMapUrl(altitudeLayerId,bbox);
  const map=mapUrl?await api(mapUrl):null;
  return {summary,map:constraintMapModel(map)};
}

// ---- 展示模型 ---------------------------------------------------------------

/**
 * 约束场展示模型。同时消费两个来源：
 *  - ``collection``：``/api/planning-constraint-field`` 的 summary 集合
 *    （含 counts / warnings / fingerprint / artifact_ref）；
 *  - ``map``：``constraintMapModel()`` 的地图紧凑结果。
 * 二者不一致时以 collection 的 counts 为准（它是落库事实），map 只提供逐格明细。
 */
export function constraintFieldModel({collection,map,altitudeLayerId,workspaceIdentity}={}){
  const items=(collection&&Array.isArray(collection.items))?collection.items:[];
  const wanted=String(altitudeLayerId||'');
  const summary=items.find(item=>String(item.altitude_layer_id||'')===wanted)||null;
  const counts=summary&&summary.counts?summary.counts:(map&&map.counts?map.counts:null);
  const outcomes={
    pass:Number(counts?.pass)||0,
    blocked:Number(counts?.blocked)||0,
    unknown:Number(counts?.unknown)||0,
    total:Number(counts?.total)||0
  };
  const blockedBy=(counts&&counts.blocked_by)||{};
  const warnings=summary&&Array.isArray(summary.warnings)?summary.warnings:[];
  const unknownWarning=warnings.find(item=>String(item.reason||'')==='unknown_constraints_remain')||null;
  return {
    altitudeLayerId:wanted,
    summary,
    map:map||null,
    present:Boolean(summary),
    status:summary?String(summary.status||'not_calculated'):'not_calculated',
    fieldId:summary?String(summary.field_id||''):'',
    nominalAltitudeM:summary?summary.nominal_altitude_m:null,
    verticalReference:summary?String(summary.vertical_reference||''):'',
    fingerprint:summary?String(summary.constraint_field_fingerprint||''):'',
    policyFingerprint:summary?summary.policy_fingerprint:null,
    gridIdentity:summary?summary.grid_identity:null,
    unknownPolicy:summary?summary.unknown_policy||null:null,
    artifactRef:summary?summary.artifact_ref||null:null,
    staleReason:summary?summary.stale_reason||null:null,
    warnings,
    unknownWarning,
    outcomes,
    blockedBy,
    counts,
    workspaceIdentity:workspaceIdentity||null,
    // 地图可绘制性：只有 map.status==='passed' 才拿到逐格明细。
    mapStatus:map?map.status:'not_calculated',
    mapReason:map?map.reason:'',
    cells:map&&map.usable?map.cells:[]
  };
}

/**
 * 约束场「新鲜度」中文（B4X §10：主界面只显示"当前 / 需要重新计算"，指纹放高级）。
 *
 * 判定只读后端字段：
 *  - summary 缺失 → 尚未计算；
 *  - ``stale_reason`` 存在 → 需要重新计算（并给出中文原因）；
 *  - workspace identity 与当前工作区不一致 → 需要重新计算（对象不再对应同一工作区）；
 *  - 否则 → 当前。
 */
export function constraintFieldFreshness(model,{workspaceIdentity=null}={}){
  if(!model||!model.present)return {state:'not_calculated',label:constraintFreshness('not_calculated')};
  if(model.staleReason){
    return {state:'stale',label:constraintFreshness('stale'),reason:String(model.staleReason)};
  }
  const current=workspaceIdentity||null;
  const stored=model.workspaceIdentity||null;
  if(current&&stored&&workspaceChanged(stored,current)){
    return {state:'stale',label:constraintFreshness('stale'),reason:'工作区已变化'};
  }
  return {state:'current',label:constraintFreshness('current')};
}

function workspaceChanged(stored,current){
  const left=stored&&typeof stored==='object'?stored:{};
  const right=current&&typeof current==='object'?current:{};
  const leftId=String(left.workspace_id??''),rightId=String(right.workspace_id??'');
  if(leftId&&rightId&&leftId!==rightId)return true;
  const leftRevision=left.revision,rightRevision=right.revision;
  if(leftRevision!==undefined&&rightRevision!==undefined&&leftRevision!==null&&rightRevision!==null){
    return Number(leftRevision)!==Number(rightRevision);
  }
  return false;
}

// ---- 状态卡 HTML ------------------------------------------------------------

function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}

function count(value){return Number.isFinite(Number(value))?Number(value):0;}

/**
 * 约束场状态卡（Step2 / Step3 共用同一个组件）。
 *
 * 主界面只显示业务词汇；fingerprint / policy fingerprint / grid identity / artifact
 * 一律进入「高级」折叠区（B4X §10）。
 */
export function constraintSummaryHtml(model,{
  formatNumber=value=>String(value),
  freshness=null,
  altitudeLayerLabelText='',
  mapLoaded=false
}={}){
  const item=model||{};
  if(!item.altitudeLayerId){
    return '<div class="wb-empty">尚未选择固定巡航高度层：请先在上方选择高度层，系统不会自动挑选。</div>';
  }
  const fresh=freshness||constraintFieldFreshness(item);
  const status='<span class="constraint-freshness" data-state="'+escapeHtml(fresh.state)+'">'
    +escapeHtml(fresh.label)+(fresh.reason?'<small>'+escapeHtml(fresh.reason)+'</small>':'')+'</span>';
  if(!item.present){
    return '<div class="constraint-card" data-status="not_calculated">'
      +'<div class="constraint-card-head"><b>规划约束场</b>'+status+'</div>'
      +'<div class="wb-empty">该高度层尚未生成 Planning Constraint Field。'
      +'约束场是可行性事实（能否飞），与航路风险场（软成本）相互独立。</div></div>';
  }
  const outcomes=item.outcomes||{};
  const cards=[
    ['可通行',count(outcomes.pass),'pass'],
    ['障碍',count(outcomes.blocked),'blocked'],
    ['证据不足',count(outcomes.unknown),'unknown']
  ].map(([label,value,outcome])=>'<b data-outcome="'+outcome+'">'+formatNumber(value)
    +'<small>'+escapeHtml(label)+'</small></b>').join('');
  const blockedBy=item.blockedBy||{};
  const blockerRows=['terrain','building','tower','airspace','critical_site']
    .map(domain=>({domain,value:count(blockedBy[domain])}))
    .filter(row=>row.value>0);
  const blockerText=blockerRows.length
    ?blockerRows.map(row=>constraintBlockerText([row.domain])+' '+formatNumber(row.value)).join(' · ')
    :'没有阻挡单元';
  // provisional 是否生效：读后端 unknown_policy（落库事实），而不是前端开关状态。
  const provisionalEnabled=Boolean(
    (item.unknownPolicy&&item.unknownPolicy.allow_unknown_for_provisional===true)
    ||(item.unknownWarning&&item.unknownWarning.provisional_traversal_allowed===true)
  );
  const unknownNote=count(outcomes.unknown)
    ?'<div class="parameter-note" data-unknown-warning="true"><b>证据不足 '+formatNumber(count(outcomes.unknown))
      +' 格</b>：这些格子既不是可通行也不是障碍——规划不能把它们当作安全。'
      +'<b>证据不足不等于可通行</b>。'
      +(provisionalEnabled
        ?'<b class="constraint-provisional-flag">已启用候选试算穿越证据不足单元。</b>'
          +'证据不足仍不是安全通过，仅允许生成候选航路；此类候选永远不能发布为运行航路。'
        :'当前 unknown policy 不允许穿越证据不足单元。')
      +'</div>'
    :'';
  const mapNote=mapLoaded
    ?''
    :'<div class="parameter-note">地图明细尚未读取：勾选图层抽屉中的「高度层障碍」后按需读取'
      +'（只返回 grid_id / 结果 / 阻挡原因，几何复用标准网格，不重复下发）。</div>';
  const advanced='<details class="algorithm-detail"><summary>高级 / 审计信息 · 指纹与来源</summary>'
    +'<div class="flow-summary">field_id '+escapeHtml(item.fieldId||'—')
    +'<br>constraint_field_fingerprint '+escapeHtml(item.fingerprint||'—')
    +'<br>policy_fingerprint '+escapeHtml(item.policyFingerprint?JSON.stringify(item.policyFingerprint):'—')
    +'<br>grid_identity '+escapeHtml(item.gridIdentity?JSON.stringify(item.gridIdentity):'—')
    +'<br>artifact_ref '+escapeHtml(item.artifactRef?JSON.stringify(item.artifactRef):'—')
    +'<br>map_status '+escapeHtml(item.mapStatus||'—')
    +(item.mapReason?' · '+escapeHtml(item.mapReason):'')
    +'<br>unknown_policy '+escapeHtml(item.unknownPolicy?JSON.stringify(item.unknownPolicy):'—')
    +'</div></details>';
  return '<div class="constraint-card" data-status="'+escapeHtml(item.status)+'">'
    +'<div class="constraint-card-head"><b>规划约束场</b>'
    +escapeHtml(altitudeLayerLabelText||item.altitudeLayerId)+status+'</div>'
    +'<div class="metric-grid constraint-metrics">'+cards+'</div>'
    +'<div class="constraint-blockers"><b>阻挡原因分布</b><span>'+escapeHtml(blockerText)+'</span></div>'
    +unknownNote+mapNote+advanced+'</div>';
}

/**
 * 单个约束格的详情 HTML（地图点击弹窗专用）。
 *
 * 契约（B4X §12.1）：
 *  - 优先显示：高度层 / 状态 / 主要阻挡原因 / 关键净空值；
 *  - **绝不**把 `blocked_by:["terrain","building"]` 这类 raw JSON 直接丢给用户；
 *  - unknown 格显示证据不足原因；unknown 永不显示成"通过"；
 *  - 详细 evidence 进"高级"折叠。
 */
export function constraintCellDetailsHtml(cell,{
  altitudeLayerLabelText='',
  gridId='',
  keyClearance=''
}={}){
  const item=cell&&typeof cell==='object'?cell:{};
  const outcome=['pass','blocked','unknown'].includes(String(item.outcome))?String(item.outcome):'unknown';
  const lines=[
    '<b>规划约束场单元</b>',
    '高度层：'+escapeHtml(altitudeLayerLabelText||item.altitude_layer_id||'—'),
    '网格编号：'+escapeHtml(gridId||item.grid_id||'—'),
    '状态：<b data-outcome="'+outcome+'">'+escapeHtml(constraintOutcomeText(outcome))+'</b>'
  ];
  if(outcome==='blocked'){
    lines.push('主要阻挡原因：<b>'+escapeHtml(constraintBlockerText(item.blocked_by))+'</b>');
  }
  if(outcome==='unknown'){
    const reasons=constraintUnknownSummary(item.unknown_reasons||[]);
    lines.push('证据不足原因：'+escapeHtml(reasons.length?reasons.join('；'):'尚未记录原因'));
    lines.push('<small>证据不足不等于安全：该单元既不能当作可通行，也不能当作障碍。</small>');
  }
  if(outcome==='pass'){
    lines.push('<small>可通行：在当前高度层与已确认净空策略下没有发现硬约束。</small>');
  }
  if(keyClearance)lines.push('关键净空：'+escapeHtml(keyClearance));
  const evidence=constraintUnknownSummary(item.unknown_reasons||[]);
  const advanced=(item.evidence_refs||item.unknown_reasons||[]).length
    ?'<details class="algorithm-detail"><summary>高级 / 审计信息 · 原始证据引用</summary>'
      +'<div class="flow-summary">outcome '+escapeHtml(outcome)
      +'<br>blocked_by '+escapeHtml(JSON.stringify(item.blocked_by||[]))
      +'<br>unknown_reasons '+escapeHtml(JSON.stringify(item.unknown_reasons||[]))
      +'<br>evidence_refs '+escapeHtml(JSON.stringify(item.evidence_refs||[]))
      +'</div></details>'
    :'';
  return lines.join('<br>')+advanced;
}

/**
 * 由地图紧凑结果构建 ``grid_id → 约束`` 索引（纯 Map，不依赖任何 geometry）。
 *
 * 绘制时用前端既有的标准网格几何补齐 bbox；索引缺失的 grid_id 会被显式报告，
 * 绝不静默丢弃（那会让障碍单元在地图上"消失"）。
 */
export function constraintCellIndex(cells){
  const map=new Map();
  const missing=[];
  for(const cell of cells||[]){
    const id=String(cell?.gridId||'');
    if(!id)continue;
    map.set(id,cell);
  }
  return {map,missing,size:map.size};
}

/**
 * 把约束索引与标准网格几何合并成可绘制的条目列表。
 *
 * @param {{cells:Array,cellsById:Map<string,{cell:object}>}} input
 *   cells 来自 :func:`constraintMapModel`，cellsById 是前端既有网格索引
 *   （``gridRenderCache.byId``，key=grid_id，value.cell.bbox）。
 */
export function constraintOverlayEntries({cells,cellsById}={}){
  const entries=[];
  const unresolved=[];
  for(const item of cells||[]){
    const record=cellsById?.get?cellsById.get(item.gridId):null;
    const bbox=record?.cell?.bbox;
    if(!Array.isArray(bbox)||bbox.length!==4){unresolved.push(item.gridId);continue;}
    entries.push({gridId:item.gridId,outcome:item.outcome,blockedBy:item.blockedBy,bbox});
  }
  return {entries,unresolved};
}

/** 三态计数（只用给定 entries；用于图例与"当前视口"文案）。 */
export function constraintOutcomeStats(entries){
  const stats={pass:0,blocked:0,unknown:0,total:0};
  for(const entry of entries||[]){
    const key=['pass','blocked','unknown'].includes(entry.outcome)?entry.outcome:'unknown';
    stats[key]+=1;stats.total+=1;
  }
  return stats;
}

/** 图例模型：即便 unknown 图层默认关闭，图例也必须列出它的数量。 */
export function constraintLegendModel(model,entries){
  const stats=constraintOutcomeStats(entries);
  const counts=model?.outcomes||{};
  return {
    title:'高度层障碍（规划约束场）',
    note:'约束场 = 可行性（能不能飞）；风险场 = 软成本（哪里风险高）。两者不合并成一张"综合风险"。',
    rows:[
      {outcome:'blocked',label:constraintOutcomeText('blocked'),count:stats.blocked||count(counts.blocked),color:CONSTRAINT_LAYER_COLORS.blocked},
      {outcome:'unknown',label:constraintOutcomeText('unknown'),count:stats.unknown||count(counts.unknown),color:CONSTRAINT_LAYER_COLORS.unknown},
      {outcome:'pass',label:constraintOutcomeText('pass'),count:stats.pass||count(counts.pass),color:CONSTRAINT_LAYER_COLORS.pass}
    ]
  };
}

/**
 * 约束场图层开关状态（从图层抽屉的 checkbox 读取；默认只画障碍）。
 *
 * 与 `shell.js` 的 `layerSwitches` 语义逐字一致（`checked !== false`），
 * 因此未挂载 checkbox 的纯逻辑测试环境不会意外开启任何图层。
 */
export function constraintLayerState($){
  const read=id=>$(id)?.checked!==false;
  return {
    enabled:read(CONSTRAINT_LAYER_ID),
    blocked:read(CONSTRAINT_LAYER_ID),
    unknown:read(CONSTRAINT_UNKNOWN_LAYER_ID),
    pass:read(CONSTRAINT_PASS_LAYER_ID)
  };
}

/** 约束场状态卡的错误行（读取失败时显示真实原因，不统一写成"失败"）。 */
export function constraintLoadErrorHtml(altitudeLayerId,error){
  const reason=error&&error.message?error.message:'数据源不可用';
  return '<div class="constraint-card" data-status="unavailable">'
    +'<div class="constraint-card-head"><b>规划约束场</b>'
    +'<span class="constraint-freshness" data-state="unavailable">数据源不可用</span></div>'
    +'<div class="wb-empty">高度层 '+escapeHtml(altitudeLayerId||'—')+' 的约束场读取失败：'
    +escapeHtml(reason)+'</div></div>';
}

// ---- 前置配置：provisional 穿越策略 / confirmed_none 显式声明 -------------------

/** 域状态 → 中文（只有这三个状态，且 confirmed_none 只能由用户显式确认）。 */
export const DOMAIN_DATASET_STATE_TEXT={
  confirmed_present:'已确认存在该类约束数据',
  confirmed_none:'已由用户显式确认范围内无该类约束',
  not_configured:'未配置（保持证据不足）'
};

/** 前置配置读取 URL（只读 GET）。 */
export function constraintConfigurationUrl(){return CONSTRAINT_CONFIGURATION_ENDPOINT;}

function evidenceToText(value){
  if(Array.isArray(value)){
    return value.map(item=>typeof item==='string'?item:JSON.stringify(item)).join('\n');
  }
  if(value===null||value===undefined)return '';
  return typeof value==='string'?value:JSON.stringify(value);
}

/**
 * 后端前置配置 → 前端表单模型。
 *
 * 缺失 / 读不到一律按"未启用 provisional 穿越、未声明 confirmed_none"处理：
 * 前端**绝不**自行推断某个域"没有约束"。
 */
export function constraintConfigurationModel(response){
  const data=response&&typeof response==='object'?response:{};
  const policy=data.unknown_policy_configuration&&typeof data.unknown_policy_configuration==='object'
    ?data.unknown_policy_configuration:{};
  const unknownPolicy=policy.unknown_policy&&typeof policy.unknown_policy==='object'
    ?policy.unknown_policy:{};
  const declarations=data.restricted_area_declarations
    &&typeof data.restricted_area_declarations==='object'?data.restricted_area_declarations:{};
  const domainStates=data.domain_states&&typeof data.domain_states==='object'?data.domain_states:{};
  const domains={};
  for(const domain of ['airspace','critical_site']){
    const item=declarations[domain]&&typeof declarations[domain]==='object'?declarations[domain]:{};
    const state=domainStates[domain]&&typeof domainStates[domain]==='object'?domainStates[domain]:{};
    // 兼容两种来源：后端配置快照（domain_states.status）与 workflow 快照里的持久化声明
    // （declarations[domain].confirmed_none）。两者都缺 → 未声明。
    const status=String(state.status||(item.confirmed_none===true?'confirmed_none':'not_configured'));
    domains[domain]={
      confirmedNone:item.confirmed_none===true||status==='confirmed_none',
      source:item.source===null||item.source===undefined?'':String(item.source),
      evidence:evidenceToText(item.evidence),
      status:['confirmed_present','confirmed_none','not_configured'].includes(status)
        ?status:'not_configured',
      authorityComplete:state.declaration_authority_complete===true
    };
  }
  return {
    present:Boolean(data&&typeof data==='object'&&Object.keys(data).length),
    allowUnknownForProvisional:unknownPolicy.allow_unknown_for_provisional===true,
    policyStatus:String(policy.status||'not_configured'),
    policyRequested:policy.requested===true,
    policyConfirmed:policy.confirmed===true,
    policyAuthorityComplete:policy.authority_complete===true,
    statement:String(policy.statement||PROVISIONAL_TRAVERSAL_SCOPE),
    provisionalSource:policy.source===null||policy.source===undefined?'':String(policy.source),
    provisionalEvidence:evidenceToText(policy.evidence),
    declarations:domains,
    domainStates,
    regulatoryBridge:data.regulatory_bridge||null,
    semantics:data.semantics||null
  };
}

/** 前置配置表单 HTML（Step2 规划约束场面板内；纯函数，便于审计与测试）。 */
export function constraintConfigurationHtml(model,{disabled=false}={}){
  const item=model||constraintConfigurationModel(null);
  const domainRows=[
    ['airspace','pcfAirspace','受限空域','我已确认当前项目范围内没有已知受限空域数据/约束'],
    ['critical_site','pcfCriticalSite','保护要地','我已确认当前项目范围内没有已知保护要地约束']
  ].map(([key,id,label,text])=>{
    const value=(item.declarations&&item.declarations[key])||{};
    const stateText=DOMAIN_DATASET_STATE_TEXT[value.status]||DOMAIN_DATASET_STATE_TEXT.not_configured;
    return '<div class="constraint-config-domain" data-domain="'+key+'">'
      +'<label class="constraint-config-check"><input type="checkbox" id="'+id+'ConfirmedNone"'
      +(value.confirmedNone?' checked':'')+(disabled?' disabled':'')+'>'+escapeHtml(text)+'</label>'
      +'<div class="constraint-config-fields">'
      +'<label>source<input type="text" id="'+id+'Source" value="'+escapeHtml(value.source||'')+'"'
      +' placeholder="例如：Phase4人工验收工程确认"'+(disabled?' disabled':'')+'></label>'
      +'<label>evidence<textarea id="'+id+'Evidence" rows="2"'
      +' placeholder="例如：当前项目范围内未提供受限区数据，仅用于候选规划试算"'
      +(disabled?' disabled':'')+'>'+escapeHtml(value.evidence||'')+'</textarea></label>'
      +'</div>'
      +'<div class="parameter-note" data-domain-state="'+escapeHtml(value.status)+'">'
      +'<b>'+escapeHtml(label)+'</b> 当前状态：'+escapeHtml(stateText)
      +'。confirmed_none 的语义是「用户显式确认当前项目范围内按当前依据无该类约束」，'
      +'<b>不是</b>「系统自动判断没有」；未勾选或缺少 source / evidence 时继续按证据不足处理。</div>'
      +'</div>';
  }).join('');
  const notice=item.allowUnknownForProvisional
    ?'<div class="parameter-note constraint-provisional-notice" data-provisional="enabled">'
      +'<b>已启用候选试算穿越证据不足单元。证据不足仍不是安全通过，仅允许生成候选航路。</b></div>'
    :'<div class="parameter-note" data-provisional="disabled">'
      +'当前<b>不允许</b>候选航路穿越证据不足单元：证据不足单元在路径搜索中保持不可穿越。</div>';
  return '<div class="constraint-config" data-configuration="true">'
    +'<div class="constraint-card-head"><b>规划约束场前置配置</b>'
    +'<small>全部需要用户显式确认，系统不提供任何默认值</small></div>'
    +'<label class="constraint-config-check"><input type="checkbox" id="pcfAllowUnknownProvisional"'
    +(item.allowUnknownForProvisional?' checked':'')+(disabled?' disabled':'')+'>'
    +'允许候选航路试算穿越证据不足单元</label>'
    +'<div class="parameter-note">'+escapeHtml(item.statement||PROVISIONAL_TRAVERSAL_SCOPE)+'</div>'
    +'<div class="constraint-config-fields">'
    +'<label>source<input type="text" id="pcfProvisionalSource"'
    +' value="'+escapeHtml(item.provisionalSource||'')+'"'
    +' placeholder="例如：Phase4人工验收工程确认"'+(disabled?' disabled':'')+'></label>'
    +'<label>evidence<textarea id="pcfProvisionalEvidence" rows="2"'
    +' placeholder="例如：当前项目范围证据不足，仅用于候选规划试算"'
    +(disabled?' disabled':'')+'>'+escapeHtml(item.provisionalEvidence||'')+'</textarea></label>'
    +'</div>'
    +notice+domainRows
    +'<div class="parameter-note">保存后已生成的约束场会标记为「需要重新计算」：'
    +'配置变化绝不静默沿用旧结果。</div>'
    +'<button class="primary" id="saveConstraintConfiguration"'+(disabled?' disabled':'')
    +'>保存前置配置</button>'
    +'</div>';
}

/** 从表单读取提交 payload（只读 DOM；缺字段如实为空，绝不补默认值）。 */
export function constraintConfigurationPayload($){
  const node=id=>(typeof $==='function'?$(id):null);
  const value=id=>{const item=node(id);return item?String(item.value??'').trim():'';};
  const checked=id=>{const item=node(id);return item?item.checked===true:false;};
  const evidence=raw=>raw?raw.split('\n').map(line=>line.trim()).filter(Boolean):[];
  const declarations={};
  for(const [key,id] of [['airspace','pcfAirspace'],['critical_site','pcfCriticalSite']]){
    declarations[key]={
      confirmed_none:checked(id+'ConfirmedNone'),
      source:value(id+'Source')||null,
      evidence:evidence(value(id+'Evidence'))
    };
  }
  return {
    unknown_policy:{
      allow_unknown_for_provisional:checked('pcfAllowUnknownProvisional'),
      source:value('pcfProvisionalSource')||null,
      evidence:evidence(value('pcfProvisionalEvidence'))
    },
    restricted_area_declarations:declarations
  };
}

export {constraintOutcomeText,constraintBlockerText,constraintBlockerList};
