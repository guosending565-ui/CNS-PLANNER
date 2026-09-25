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
  constraintUnknownSummary,constraintFreshness,CONSTRAINT_OUTCOME_COLOR
} from './presentation.js';

/** 只读读取端点（B4X 新增；仅用于展示）。 */
export const CONSTRAINT_FIELD_SUMMARY_ENDPOINT='/api/planning-constraint-field';
export const CONSTRAINT_FIELD_MAP_ENDPOINT='/api/planning-constraint-field/map';

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
  const unknownNote=count(outcomes.unknown)
    ?'<div class="parameter-note" data-unknown-warning="true"><b>证据不足 '+formatNumber(count(outcomes.unknown))
      +' 格</b>：这些格子既不是可通行也不是障碍——规划不能把它们当作安全。'
      +'<b>证据不足不等于可通行</b>。'
      +(item.unknownWarning&&item.unknownWarning.provisional_traversal_allowed
        ?'当前 unknown policy 允许候选航路<b>试算</b>穿越证据不足单元，但此类候选永远不能发布为运行航路。'
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

export {constraintOutcomeText,constraintBlockerText,constraintBlockerList};
