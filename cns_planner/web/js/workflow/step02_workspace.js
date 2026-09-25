// =========================================================
// Step02 环境与风险：工作区 / 网格 / 高度层 / 映射 / 约束场 / 专题 / 风险
//
// 信息架构（B4X 六区：目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步）
//  - 操作：工作区范围 / 标准网格与建筑环境 / 固定巡航高度层
//  - 结果：数据映射 / 规划约束场 / 专题浏览
//  - 高级：风险框架 / 高度层（工程设定）
//  每个一级标签下同一时刻只显示一个任务（由 workbench.js 的 .wb-seg-active 保证），
//  所有分段内容始终挂载 DOM：切换任务不重新渲染业务内容，表单草稿与控件引用不丢。
//
// 语义边界（本次重组只改展示组织，不改任何业务契约）
//  - 工作区框选 / 清除 / 保存的 DOM id、API path 与 payload 完全不变；
//  - **标准规划网格只有一个 canonical 层级：MH/T 4063.1 L8**（GRID-L8-UNIFICATION）。
//    正式工作流不再向用户暴露 L6/L7 选择：后端超限时返回 status=blocked，前端如实显示
//    可读错误（需求格数 / 资源上限 / 建议），绝不出现"选了 L8、实际静默 L7"的模糊状态，
//    也绝不显示伪通过。L6/L7 的底层算法仅保留给 legacy / unit test / diagnostic；
//  - **风险场 = 软成本（哪里风险高）**；**约束场 = 可行性（哪里不能飞）**。
//    两者是两份独立证据，本产品绝不把它们合并成一张"综合风险"，
//    障碍物也绝不进入航路风险数学（B3X 契约）；
//  - 固定巡航高度层是完全目录驱动的（B4X §9）：绝不写死 ALT-080 / 80 m；
//  - 数据映射卡只转印现有 flow.workspace.health 与 flow.grid_attributes 的
//    status / 计数：不重算状态、不新增数据状态、绝不把缺失数据写成 passed；
//  - 专题浏览只切地图配色（gridDisplay.outline / theme）：不自动改 zoom、图层，
//    也不改任何业务状态；专题清单仍由 riskV2ThemeOptions() 提供；
//  - 垂向基准与 nominal 高度只能显式填写：不猜 datum、不提供任何默认真实高度，
//    缺 nominal 的高度层保持 pending_confirmation（绝不取上下界中值）；
//  - Population NoData Semantics 只是把后端已存的**显式工程确认**投影出来：mode 只有
//    nodata_is_zero_population 一种取值，且必须 source + evidence + confirmed 齐全才可保存；
//    未确认时如实显示未配置，绝不推断确认，也绝不把零人口说成安全/适飞结论。
// =========================================================
import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbSegHint,wbCard,wbDisclosure,primaryAction,blockerList,nextStepBar,readinessText,workflowStatusText,assessmentText,emptyReasonText,inputRequirementText,constraintOutcomeText,CONSTRAINT_OUTCOME_TEXT,sourceStateText,advancedAuditNote,canonicalNodeLabel} from './common.js';
import {LEGACY_RISK_V1_LABEL,renderRiskFrameworkV2Panel,riskFrameworkV2Model,riskV2ThemeOptions} from './risk_framework_v2.js';
import {altitudeLayerSelector,altitudeLayerCatalogSummary,bindAltitudeLayerSelector,selectedAltitudeLayerId,altitudeLayerDetailRows,ALTITUDE_LAYER_EMPTY_NOTE} from './altitude_layers.js';
import {constraintSummaryHtml,constraintLoadErrorHtml} from './constraint_field.js';

// ---- 二级任务分段 -----------------------------------------------------------
// id 稳定（env-*），标签是第一视觉层的业务语言；工程编号与算法 id 只出现在
// 说明或高级标签的内容里，不作为导航语言。
//
// B4X 变更：`operate` 新增「固定巡航高度层」、`result` 新增「规划约束场」。
// 原有 6 个分段 id 与顺序完全不变（既有测试与用户的导航习惯都按 id 恢复）。
export const WORKSPACE_SEGMENTS={
  operate:[
    ['env-op-workspace','工作区范围'],
    ['env-op-grid','标准网格与建筑环境'],
    ['env-op-altitude','固定巡航高度层']
  ],
  result:[
    ['env-res-mapping','数据映射'],
    ['env-res-constraint','规划约束场'],
    ['env-res-theme','专题浏览']
  ],
  advanced:[['env-adv-risk','风险框架'],['env-adv-altitude','高度层']]
};

// ---- 每个一级标签在"六区"中的位置（B4X 第 4 节） ----------------------------
// 六区：目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步。
// 这里只做**导航语义标注**（面板顶部一行文字），不新造 DOM 结构、不改任何 id。
const SECTION_ROLE={
  operate:'目标 · 输入准备 · 主操作',
  result:'阻塞项与工程假设 · 结果',
  advanced:'高级 / 审计信息'
};
const SECTION_ORDER=['目标','输入准备','阻塞项与工程假设','主操作','结果','下一步'];

/** 六区标记行：告诉用户当前标签承担六区中的哪几区。 */
function sectionRoleLine(tab){
  const role=SECTION_ROLE[tab]||SECTION_ROLE.advanced;
  return '<div class="wb-section-role" data-section-role="'+escapeHtml(tab)+'">'
    +'<b>本区对应六区：</b>'+escapeHtml(role)
    +'<small>六区顺序：'+escapeHtml(SECTION_ORDER.join(' → '))+'</small></div>';
}

// ---- 纯展示辅助（只读 flow / 已有展示状态，不修改任何业务数据） ---------------

/**
 * 统一映射结果的展示文案。三态词汇来自后端 ``population_mapping`` /
 * ``environment_mapping``；没有该载体的旧快照回退到既有的 status 文案。
 */
const MAPPING_STATUS_TEXT={passed:'通过',partial:'部分覆盖',unsupported:'不可用'};

/** 建筑环境映射的"依据"标签（只做取词映射，不推断任何业务语义）。 */
const BUILDING_MAPPING_BASIS_TEXT={
  l8_building_grid_fact_table:'L8 建筑环境网格直接映射',
  exact_footprint_intersection_and_centroid_allocation:'原始建筑足迹精确聚合'
};

function mappingStatusLabel(status,fallbackStatus){
  if(status&&MAPPING_STATUS_TEXT[status])return MAPPING_STATUS_TEXT[status];
  return statusText(fallbackStatus||status||'not_calculated');
}

function percentText(ratio){
  // null / undefined 一律显示 "—"：**绝不**把"缺失"渲染成 "0%"（Number(null)===0）。
  if(ratio===null||ratio===undefined)return '—';
  const value=Number(ratio);
  return Number.isFinite(value)?Math.round(value*100)+'%':'—';
}

/** 只接受后端给出的有限数值；其余（含 null / 字符串 / NaN）保持"未提供"。 */
function finiteRatio(value){
  return typeof value==='number'&&Number.isFinite(value)?value:null;
}

/**
 * 人口映射覆盖统计。优先读后端统一载体 ``population_mapping``；旧快照没有该键时，
 * 只用既有的 full/partial/missing/outside 计数做**展示换算**（不重算任何业务数值）。
 *
 * 统一载体存在时六类计数**必须可闭合**：
 * ``full + partial + nodata_only + confirmed_zero + missing + outside === total``，
 * 且 ``covered === full + partial + confirmed_zero``、``unresolved === total - covered``。
 * ``closed`` / ``closure`` 就是为了让界面与测试都能直接验证这条恒等式。
 *
 * BUG-UI-POP-002：``cellRatio`` 与 ``areaRatio`` 是**两个不同口径**的量，必须分开展示。
 * ``cell_coverage_ratio`` 是格网判定口径（covered/total），``area_coverage_ratio`` 是映射
 * 有效面积口径（valid_covered_area / target_area）。旧快照缺少新 ratio 时保持 ``null``，
 * 由展示层显示 "—"，**绝不**用另一个口径的数字顶替，也绝不伪造百分比。
 */
export function populationCoverage(population){
  const mapping=population?.population_mapping;
  if(mapping){
    const full=Number(mapping.full_cells)||0,partial=Number(mapping.partial_cells)||0,
      nodataOnly=Number(mapping.nodata_only_cells)||0,
      confirmedZero=Number(mapping.confirmed_zero_cells)||0,
      missing=Number(mapping.missing_cells)||0,outside=Number(mapping.outside_cells)||0;
    const total=Number(mapping.total_cells)||0,covered=Number(mapping.covered_cells)||0,
      unresolved=Number(mapping.unresolved_cells)||0;
    const closure=full+partial+nodataOnly+confirmedZero+missing+outside;
    const cellRatio=finiteRatio(mapping.cell_coverage_ratio)
      ??(total?covered/total:null);
    return {
      unified:true,total,covered,unresolved,full,partial,nodataOnly,confirmedZero,missing,outside,
      cellRatio,
      areaRatio:finiteRatio(mapping.area_coverage_ratio),
      ratio:mapping.coverage_ratio,closure,
      closed:closure===total&&covered===full+partial+confirmedZero&&unresolved===total-covered
    };
  }
  const full=Number(population?.full_count)||0,partial=Number(population?.partial_count)||0,
    missing=Number(population?.missing_count)||0,outside=Number(population?.outside_count)||0;
  const total=full+partial+missing+outside,covered=full+partial;
  // 旧快照无法把 nodata_only / confirmed_zero 从 missing 里分出来：保持 null 而不是伪造 0。
  // 它也没有面积口径的 ratio：areaRatio 保持 null（展示为 "—"），绝不用格网口径顶替。
  return {
    unified:false,total,covered,unresolved:total-covered,full,partial,
    nodataOnly:null,confirmedZero:null,missing,outside,
    cellRatio:total?covered/total:null,areaRatio:null,
    ratio:total?covered/total:null,closure:total,
    closed:covered===full+partial
  };
}

/**
 * 统一载体下的人口映射计数明细行（数字可闭合；旧快照只显示它真正拥有的四类）。
 *
 * BUG-UI-POP-002：``格网判定 X / Y（cell%）`` 与 ``映射有效面积覆盖 area%`` 分开写。
 * 之前这里把面积口径的 ``coverage_ratio`` 直接写在 "已覆盖 X / Y 格" 后面，产生了
 * "986/986 格（90%）" 这种自相矛盾的显示。
 */
export function populationCoverageNote(counts){
  const head='格网判定 '+counts.covered+' / '+counts.total+'（'+percentText(counts.cellRatio)+'）'
    +' · 映射有效面积覆盖 '+percentText(counts.areaRatio);
  if(!counts.unified){
    return head+' · full '+counts.full+' / partial '+counts.partial
      +' / missing '+counts.missing+' / outside '+counts.outside;
  }
  return head+' · full '+counts.full+' / partial '+counts.partial
    +' / nodata_only '+counts.nodataOnly+' / confirmed_zero '+counts.confirmedZero
    +' / missing '+counts.missing+' / outside '+counts.outside
    +' · 未判定 '+counts.unresolved;
}

/**
 * 人口映射状态：stale 优先（保持既有语义），其次统一载体 ``population_mapping.status``，
 * 最后回退到 value_status / status。**不再**因为 value_status=missing_data 就只显示"缺少数据"：
 * 覆盖统计始终随卡片给出。
 */
function populationMappingStatus(population){
  if(population?.status==='stale')return statusText('stale');
  return mappingStatusLabel(population?.population_mapping?.status,
    population?.value_status||population?.status||'not_calculated');
}

/**
 * 数据映射状态卡：人口 / 地形 DEM / 空域 / 建筑 / 交通暴露 / 冲突暴露。
 * 所有状态与计数直接来自现有 flow.grid_attributes，不重算、不伪造 passed；
 * 未计算的映射保持 not_calculated，未提供的计数与原摘要一样显示 0。
 * @returns {string} 纯 HTML（两列卡片，窄容器由容器查询降成一列）
 */
export function mappingStatusCards(flow){
  const attributes=flow?.grid_attributes||{},buildings=attributes.buildings||{};
  const population=attributes.population||{},terrain=attributes.terrain||{},
    airspace=attributes.airspace||{},traffic=attributes.traffic||{},conflict=attributes.conflict||{};
  const populationCounts=populationCoverage(population);
  const buildingCard=buildingMappingCard(buildings);
  return '<div class="env-metric-cards">'+[
    wbCard('人口映射',populationMappingStatus(population),populationCoverageNote(populationCounts)),
    wbCard('地形 DEM 映射',statusText(terrain.status||'not_calculated'),
      '已处理 '+(terrain.count||0)+' 格（有效 '+(terrain.covered_count||0)+' 格）'),
    wbCard('低空空域映射',statusText(airspace.status||'not_calculated'),
      '已处理 '+(airspace.count||0)+' 格（命中 '+(airspace.hit_count||0)+' 格）'),
    wbCard('建筑环境映射',buildingCard.value,buildingCard.note),
    wbCard('交通暴露映射',statusText(traffic.status||'not_calculated'),
      '已处理 '+(traffic.count||0)+' 格（命中 '+(traffic.covered_count||0)+' 格）'),
    wbCard('冲突暴露映射',statusText(conflict.status||'not_calculated'),
      '已处理 '+(conflict.count||0)+' 格（命中 '+(conflict.covered_count||0)+' 格）')
  ].join('')+'</div>';
}

/**
 * 建筑环境映射卡的**值**（``wbCard`` 的第二个参数）。
 *
 * 统一载体 ``environment_mapping`` 是权威来源：``status`` 三态 + ``total_cells`` /
 * ``covered_cells`` / ``unresolved_cells``。旧快照没有该载体时回退到既有
 * ``buildings.status`` / ``covered_count``，行为与改造前一致。
 */
function buildingMappingCard(buildings){
  const mapping=buildings?.environment_mapping;
  const total=Number(mapping?.total_cells??buildings?.count)||0;
  const covered=Number(mapping?.covered_cells??buildings?.covered_count)||0;
  const unresolved=Number(mapping?.unresolved_cells??Math.max(0,total-covered))||0;
  const ratio=mapping?.coverage_ratio??(total?covered/total:null);
  const basis=mapping
    ?(BUILDING_MAPPING_BASIS_TEXT[mapping.mapping_basis]||'未记录映射依据')
    :'既有建筑网格映射';
  return {
    value:mappingStatusLabel(mapping?.status,buildings?.status),
    note:'已覆盖 '+covered+' / '+total+' 格（'+percentText(ratio)+'） · 未判定 '+unresolved+' 格 · '+basis
  };
}

/**
 * 建筑环境链摘要：FABDEM DTM → GBA buildings → L8 grid → 建筑网格映射。
 * 只读 flow.workspace.health 与 flow.grid_attributes.buildings 的现有状态，
 * 不假定任何数据已就绪（缺失一律 missing_data）。
 * @returns {string} 纯 HTML
 */
export function buildingEnvironmentSummary(flow){
  const health=flow?.workspace?.health||{},buildings=flow?.grid_attributes?.buildings||{};
  const mapping=buildings.environment_mapping;
  const card=buildingMappingCard(buildings);
  // 层级无关事实获取：工作区不是 L8 时，事实来自按当前层级的精确足迹聚合。
  const levelNote=mapping?.level_independent_facts
    ?'（按当前 L'+mapping.grid_level+' 层级精确聚合原始建筑足迹，未做跨层级平均/插值）'
    :'';
  return '<div class="flow-summary"><b>建筑环境链</b><br>FABDEM DTM：'+escapeHtml(statusText(health.terrain_dtm?.status||'missing_data'))
    +' · GBA buildings：'+escapeHtml(statusText(health.buildings?.status||'missing_data'))
    +' · L8 grid：'+escapeHtml(statusText(health.building_grid?.status||'missing_data'))
    +'<br>建筑网格映射：'+escapeHtml(card.value)+' · '+escapeHtml(card.note)+levelNote+'</div>';
}

// ---- 结果 · 数据映射 · Population NoData Semantics -----------------------------

/**
 * 唯一被允许的确认模式：与后端 ``domain/population_nodata.py`` 的
 * ``MODE_NODATA_IS_ZERO_POPULATION`` **逐字一致**。任何其他取值都视为未确认。
 */
export const POPULATION_NODATA_MODE='nodata_is_zero_population';

/**
 * Population NoData 语义的只读投影：status / mode / source / evidence / confirmed
 * 全部来自 ``flow.population_nodata_policy``（后端 state），**不推断、不伪造确认**。
 */
export function populationNodataModel(flow){
  const policy=flow?.population_nodata_policy||{};
  const population=flow?.grid_attributes?.population||{};
  const evidence=policy.evidence&&typeof policy.evidence==='object'&&!Array.isArray(policy.evidence)
    ?policy.evidence:null;
  return {
    status:policy.status||'not_configured',
    statusReason:policy.status_reason||null,
    mode:policy.mode||null,
    source:policy.source||null,
    sourceId:policy.source_id||null,
    evidence,evidenceKeys:evidence?Object.keys(evidence):[],
    confirmed:policy.confirmed===true,
    confirmedAt:policy.confirmed_at||null,
    statement:policy.statement||'',
    neverConverts:Array.isArray(policy.never_converts)?policy.never_converts:[],
    semantics:policy.semantics||{},
    mappingStatus:population.status||'not_calculated',
    mappingStale:population.status==='stale',
    mappingCounts:populationCoverage(population)
  };
}

/**
 * 保存前的显式校验（与后端 ``normalize_population_nodata_policy`` 的接受条件一致）：
 * mode 只能是 ``nodata_is_zero_population``，且必须同时具备 source + 非空 evidence + confirmed。
 * 任一缺失就抛出可读错误，绝不静默降级成"已确认"。
 */
export function populationNodataPayload({mode,source,sourceId,evidence,confirmed}={}){
  const wanted=String(mode||'').trim();
  const owner=String(source||'').trim();
  const ownerId=String(sourceId||'').trim();
  const raw=evidence===null||evidence===undefined?'':String(evidence).trim();
  if(wanted!==POPULATION_NODATA_MODE)throw new Error('mode 只允许 '+POPULATION_NODATA_MODE);
  if(!owner)throw new Error('必须显式填写 source（确认人或工程依据来源）');
  if(confirmed!==true)throw new Error('必须显式勾选工程确认才能保存');
  let parsed=null;
  if(raw){
    try{parsed=JSON.parse(raw);}catch(_){throw new Error('evidence 必须是合法 JSON');}
  }
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)||!Object.keys(parsed).length){
    throw new Error('evidence 必须是至少含一个字段的 JSON 对象');
  }
  return {population_nodata_policy:{
    mode:POPULATION_NODATA_MODE,role:'population',source:owner,
    source_id:ownerId||null,evidence:parsed,confirmed:true
  }};
}

/**
 * Population NoData Semantics 面板（Step02 → 结果 → 数据映射，紧邻人口映射卡）。
 *
 * 明示三条边界：只作用于来源 extent 之内、outside_extent 永远是 unknown、
 * 零人口不是安全/适飞结论。policy 变化后人口映射会 stale，这里给出"仅重算人口映射"入口。
 */
export function populationNodataPanel(flow){
  const model=populationNodataModel(flow);
  const evidenceText=model.evidence?JSON.stringify(model.evidence):'未提供';
  // source / source_id / evidence 键名 / mode 都可能由用户填写：一律先转义再拼 HTML。
  const sourceText=escapeHtml([model.source,model.sourceId].filter(Boolean).join(' · '))||'未提供';
  const evidenceKeys=model.evidenceKeys.map(key=>escapeHtml(key)).join('、');
  const cards=[
    wbCard('Population NoData 语义',escapeHtml(statusText(model.status)),
      'mode '+escapeHtml(model.mode||'未设置')+' · confirmed '+(model.confirmed?'是':'否')
      +' · role population'),
    wbCard('来源与证据',model.source?'已记录':'未提供',
      'source '+sourceText+' · evidence '+(evidenceKeys||'未提供')),
  ];
  const staleNotice=model.mappingStale
    ?'<div class="parameter-note">NoData 语义已变化：人口映射当前为 <b>stale</b>。'
      +'点击“仅重算人口映射”才会用当前来源与当前语义重新映射（不会自动重算，也不会重算其它映射）。</div>'
    :'';
  const hasGrid=flow?.grid?.status==='passed';
  return '<div class="parameter-note">Population NoData Semantics 是<b>显式工程确认</b>：只有确认后，'
    +'<b>来源 extent 之内</b>、全部像元均为 NoData 的格子才被记录为<b>已知的 0 人口暴露</b>；'
    +'未确认时这些格子保持 missing_data（missing_data ≠ zero）。</div>'
    +'<div class="env-metric-cards">'+cards.join('')+'</div>'
    +staleNotice
    +'<div class="form-grid">'
    +'<label>mode<select id="populationNodataMode"><option value="">请选择（不猜）</option>'
    +'<option value="'+POPULATION_NODATA_MODE+'" '+(model.mode===POPULATION_NODATA_MODE?'selected':'')+'>'
    +POPULATION_NODATA_MODE+'</option></select></label>'
    +'<label>source（确认人 / 工程依据来源，必填）<input class="panel-input" id="populationNodataSource" value="'
    +escapeHtml(model.source||'')+'" placeholder="必填"></label>'
    +'<label>source id（可空）<input class="panel-input" id="populationNodataSourceId" value="'
    +escapeHtml(model.sourceId||'')+'"></label>'
    +'</div>'
    +'<label>evidence（JSON 对象，必须非空）<textarea class="panel-input" id="populationNodataEvidence" rows="3" '
    +'placeholder=\'{"product":"WorldPop Population Counts R2025A","note":"海上/无人区像元为 NoData"}\'>'
    +escapeHtml(model.evidence?JSON.stringify(model.evidence,null,1):'')+'</textarea></label>'
    +'<label class="check-row"><input type="checkbox" id="populationNodataConfirmed" '
    +(model.confirmed?'checked':'')+'>该来源的 NoData 表示零人口，已由工程依据确认</label>'
    +'<div class="button-row"><button class="secondary" id="savePopulationNodata">保存 NoData 语义</button>'
    +'<button class="secondary" id="revokePopulationNodata">撤回确认</button></div>'
    +'<button class="secondary full" id="remapPopulation" '+(hasGrid?'':'disabled')+'>仅重算人口映射</button>'
    +'<div class="parameter-note">边界：只作用于人口来源 extent <b>之内</b>的 NoData；'
    +'<b>outside_extent 永远是 unknown</b>，绝不转换，也不填补部分覆盖像元。'
    +'<b>零人口不是安全结论、也不是适飞结论</b>：该确认不改变任何算法、阈值、验证或采用语义，'
    +'并且可以随时撤回（撤回后行为与确认前完全一致）。'
    +'确认后若人口源或语义变化，人口映射会按既有 invalidation 变为 stale。</div>'
    +wbDisclosure('当前确认原文（statement / evidence）',
      '<div class="flow-summary">status '+escapeHtml(model.status)
      +' · status_reason '+escapeHtml(model.statusReason||'—')
      +' · confirmed_at '+escapeHtml(model.confirmedAt||'—')
      +' · never_converts '+escapeHtml(model.neverConverts.join('、')||'—')
      +'<br>statement：'+escapeHtml(model.statement||'—')
      +'<br>evidence：'+escapeHtml(evidenceText)+'</div>');
}

// ---- 操作 · 工作区范围 -------------------------------------------------------

/**
 * 清工作区的破坏性后果（BUG-WORKSPACE-CLEAR-002）：只读计数，不推断任何结论。
 *
 * 清工作区**确实**会删除项目节点、场景航路与运行航路，并清除工作区与标准网格；
 * 依赖它们的 layered 结果会按既有 invalidation 语义失效。前端只如实列出数量与范围，
 * 不改变"清除即删除"的业务语义，也不把任何下游结果说成仍然 current。
 */
export function workspaceClearConsequences(flow){
  const snapshot=flow||{};
  const list=value=>Array.isArray(value)?value:[];
  const items=value=>list((value||{}).items).length;
  return {
    nodes:list(snapshot.nodes).length,
    scenarioRoutes:list(snapshot.scenario_routes).length,
    operationalRoutes:list(snapshot.operational_routes).length,
    layeredCandidates:items(snapshot.layered_route_candidates),
    routeRiskProfiles:items(snapshot.route_risk_profiles),
    layeredValidations:items(snapshot.layered_route_validations),
    layeredAdoptions:items(snapshot.layered_operational_adoptions),
  };
}

export const WORKSPACE_CLEAR_INVALIDATION_NOTE='清工作区后：layered planning request 被重置为「未配置」'
  +'（不保留任何悬空的 route / node 引用），layered 候选与 feasibility mask、RouteRiskProfile、'
  +'layered validations、operational adoptions 及其拥有的运行航路状态按既有 invalidation 语义全部变为 stale，'
  +'必须由用户显式重算；这些结果绝不会被自动标成 current。';

/** 二次确认正文：明确被删除的数量与将失效的下游范围（绝不淡化破坏性）。 */
export function workspaceClearConfirmationMessage(flow){
  const counts=workspaceClearConsequences(flow);
  return '确认清除工作区？\n\n'
    +'将被真实删除：项目节点 '+counts.nodes+' 个 · 场景航路 '+counts.scenarioRoutes+' 条 · '
    +'运行航路 '+counts.operationalRoutes+' 条；工作区范围与 MH/T 标准网格同时被清除。\n\n'
    +'下游将失效（标 stale，不会自动重算、也不会伪装成 current）：\n'
    +'layered 候选 '+counts.layeredCandidates+' 条 · RouteRiskProfile '+counts.routeRiskProfiles+' 条 · '
    +'layered validations '+counts.layeredValidations+' 条 · operational adoptions '+counts.layeredAdoptions+' 条。\n'
    +'layered planning request 重置为「未配置」（不保留悬空 route / node 引用）。\n\n'
    +'此操作不可撤销。';
}

/**
 * 执行前的二次确认。
 *
 * 默认使用 ``window.confirm``；返回 ``true`` 才允许执行清除。没有可用确认通道时一律
 * 返回 ``false``（破坏性操作在无法确认时绝不执行）。
 */
export function confirmWorkspaceClear(flow,confirmImpl){
  const message=workspaceClearConfirmationMessage(flow);
  const ask=typeof confirmImpl==='function'
    ?confirmImpl
    :(typeof window!=='undefined'&&typeof window.confirm==='function'
      ?prompt=>window.confirm(prompt)
      :null);
  if(!ask)return false;
  return ask(message)===true;
}

function workspaceRangePanel(draftWorkspace){
  return '<div class="button-row"><button class="primary" id="drawWorkspace">框选工作区</button><button class="secondary" id="clearWorkspace">清除</button></div>'
    +(draftWorkspace?'<div class="flow-summary">待保存：'+draftWorkspace.map(value=>value.toFixed(5)).join(', ')+'</div>':'')
    +'<button class="primary full" id="saveWorkspace" '+(!draftWorkspace?'disabled':'')+'>保存工作区范围</button>'
    +'<div class="parameter-note">框选只产生草稿：未保存前不改变工作区，也不会触发网格、映射或风险重算。</div>'
    +'<div class="parameter-note" data-clear-workspace-warning><b>清除是破坏性操作</b>：'
    +'会删除项目节点、场景航路与运行航路，并清除工作区与标准网格。点击后会二次确认，'
    +'并明确列出将被删除的数量，以及将失效的下游（'+escapeHtml(WORKSPACE_CLEAR_INVALIDATION_NOTE)+'）。</div>';
}

// ---- 操作 · 标准网格与建筑环境 ------------------------------------------------

/**
 * 正式业务的 canonical 空间索引层级：与后端
 * ``cns_planner.algorithms.grid.service.OPERATIONAL_GRID_LEVEL`` **逐字一致**。
 * 前端不得再提供第二个可选的正式层级。
 */
export const OPERATIONAL_GRID_LEVEL=8;

/**
 * 「标准规划网格」固定展示块（GRID-L8-UNIFICATION）。
 *
 * 正式工作流只有一个 canonical 层级，因此这里**不再有 L6/L7 下拉选择**，而是固定展示
 * 层级语义，并如实转印后端返回的**实际**层级、格数、资源上限与 ``level_metadata``
 * 的米制边长（BUG-GRID-001：尺寸只有后端一个权威来源，前端绝不按经纬度估算）。
 *
 * 三态都有明确显示，绝不出现模糊状态：
 *  * ``passed``  → canonical level / actual level / 格数 / 上限 / resolution_x × resolution_y；
 *  * ``blocked`` → 可读错误（原因码、需求格数、上限、缩小范围或提高上限的建议）；
 *  * 其它       → 未生成（保存工作区后在 L8 上生成）。
 *
 * 旧项目里遗留的 L7/L6 快照（``coarsened=true``）会被明确标注为"实际层级低于 canonical"，
 * 提示重新保存工作区，而不会被悄悄当成 L8。
 */
export function standardGridPanel(flow){
  const grid=flow?.grid||{};
  const canonical=Number.isFinite(Number(grid.canonical_level))
    ?Number(grid.canonical_level):OPERATIONAL_GRID_LEVEL;
  const ceiling=Number.isFinite(Number(grid.max_cells))?Number(grid.max_cells):null;
  const header='<div class="flow-summary" id="workspaceGridLevel" data-grid-level="'+OPERATIONAL_GRID_LEVEL+'">'
    +'<b>标准规划网格</b><br>MH/T 4063.1 · L'+OPERATIONAL_GRID_LEVEL
    +'<br><small>用于人口 / 地形 / 建筑 / 风险 / TowerObstacle / Theta* V2</small></div>';
  if(grid.status==='blocked'){
    const required=Number(grid.required_cells);
    const code=grid.blocked_code||grid.error?.code||'operational_grid_cell_ceiling_exceeded';
    const message=grid.blocked_message||grid.error?.message||'工作区超出标准网格资源上限';
    const detail='L'+canonical+' 需求 '+count(required)+' 格 · 资源上限 '+count(ceiling)+' 格';
    return header
      +'<div class="parameter-note" id="workspaceGridBlocked"><b>标准规划网格：已阻断</b>（'+escapeHtml(code)+'）'
      +'<br>'+escapeHtml(detail)
      +'<br>'+escapeHtml(message)
      +'<br>正式工作流不允许降级到 L7/L6：请缩小工作区范围，或显式提高 max_cells 资源上限。</div>';
  }
  if(grid.status!=='passed'){
    return header+'<div class="parameter-note">标准规划网格：未生成。保存工作区范围后会在 L8 上生成标准网格。</div>';
  }
  const metadata=grid.level_metadata||null;
  const size=metadata&&Number.isFinite(metadata.resolution_x)&&Number.isFinite(metadata.resolution_y)
    ?' · 格网 '+Math.round(metadata.resolution_x)+' × '+Math.round(metadata.resolution_y)+' m（后端 level_metadata，非前端估算）'
    :'';
  const legacy=Number(grid.level)!==OPERATIONAL_GRID_LEVEL||grid.coarsened===true
    ?' · <b>实际层级低于 canonical L'+OPERATIONAL_GRID_LEVEL+'</b>：这是旧项目快照，'
      +'请重新保存工作区范围以在 L8 上重建空间索引'
    :'';
  return header+'<div class="flow-summary">canonical level L'+canonical+' · actual level L'+grid.level
    +' · 格数 '+count(grid.count)+' / 上限 '+count(ceiling)+size+legacy+'</div>';
}

function count(value){
  const number=Number(value);
  return Number.isFinite(number)?number:'—';
}

function gridEnvironmentPanel(flow){
  const health=flow?.workspace?.health||{};
  return standardGridPanel(flow)
    +'<div class="parameter-note">正式工作流只使用 MH/T 4063.1 <b>L8</b> 作为 canonical 空间索引；'
    +'max_cells 只是软件资源保护上限，不是空间工程参数。工作区超出上限时会被<b>明确阻断</b>，'
    +'系统不会生成 L7/L6 网格，也不会聚合/插值 L8 建筑高度。</div>'
    +'<div class="flow-summary">DEM：'+escapeHtml(statusText(health.terrain?.status||'missing_data'))+' · '+escapeHtml(health.terrain?.message||'DEM 状态未知')+'</div>'
    +buildingEnvironmentSummary(flow)
    +wbDisclosure('建筑环境链工程说明','<div class="parameter-note">0 表示覆盖范围内确认无该项；灰色表示无数据/范围外。建筑风险权重保持 0，净空评估独立。</div>');
}


// ---- 结果 · 数据映射 ---------------------------------------------------------

/** 工作区摘要（面积 / 数据可用性 / 已加载图层）与缺失项提示：取值与原来一致。 */
function workspaceMappingSummary(flow){
  const workspace=flow?.workspace,health=workspace?.health,attributes=flow?.grid_attributes||{};
  if(!workspace)return '<div class="empty-note">尚未保存工作区。点击“框选工作区”后在地图拖出矩形。</div>';
  return '<div class="metric-grid"><b>'+(workspace.area_km2).toLocaleString()+' km²<small>工作区面积</small></b>'
    +'<b>'+escapeHtml(statusText(health?.population?.status||'missing_data'))+'<small>人口数据</small></b>'
    +'<b>'+escapeHtml(statusText(health?.airspace?.status||'missing_data'))+'<small>空域数据</small></b>'
    +'<b>'+escapeHtml(String(health?.loaded_layer_count||0))+'<small>已加载图层</small></b></div>'
    // 建筑 / 财产状态必须读真实映射结果，绝不再硬编码"missing_data"（那会把旧状态写死在界面上）。
    +'<div class="missing-list">DEM：'+escapeHtml(statusText(health?.terrain?.status||'missing_data'))
    +' · 建筑：'+escapeHtml(statusText(attributes.buildings?.status||'missing_data'))
    +' · 财产：'+escapeHtml(statusText(attributes.property_exposure?.status||'missing_data'))+'</div>';
}

// ---- 结果 · 专题浏览 ---------------------------------------------------------

function themePanel(gridDisplay,attributes,populationDisplayLabel){
  const checked=value=>gridDisplay.theme===value?'checked':'';
  const ids={none:'gridThemeNone',population:'gridPopulationTheme',terrain:'gridTerrainTheme'};
  const themeId=value=>ids[value]||'gridTheme-'+String(value).replace(/[^A-Za-z0-9_-]/g,'-');
  // 专题清单：基础专题 + riskV2ThemeOptions()（V2 因子 / V2 域 / Legacy Risk V1）
  const themeOptions=[
    ['none','无'],
    ['population','人口密度'],
    ['terrain','地形 DEM'],
    ['building_density','建筑密度'],
    ['building_p95','P95 建筑高度'],
    ['building_max','最大建筑高度'],
    ['traffic_exposure','交通暴露'],
    ['conflict_exposure','冲突暴露'],
    ...riskV2ThemeOptions(),
  ];
  return '<div class="grid-theme-controls">'
    +'<div class="grid-control-title">网格显示</div>'
    +'<label class="grid-outline"><input type="checkbox" id="gridOutlineToggle" '+(gridDisplay.outline?'checked':'')+'><span>标准网格</span></label>'
    +'<div class="grid-theme-title">专题模式</div>'
    +'<div class="grid-theme-options">'
    +themeOptions.map(([value,label])=>'<label class="grid-theme-option"><input type="radio" name="gridThemeMode" id="'+themeId(value)+'" value="'+value+'" '+checked(value)+'><span>'+label+'</span></label>').join('')
    +'</div>'
    +'<small class="grid-theme-note">'+populationDisplayLabel(attributes.population)+'</small>'
    +'</div>'
    +'<div class="parameter-note">切换专题只改变地图配色，不改变工作区、标准网格、映射或风险状态，也不会自动调整地图缩放与图层开关。</div>';
}

// ---- 高级 · 风险框架 ---------------------------------------------------------

function riskFrameworkPanel(flow,formatNumber){
  const model=riskFrameworkV2Model(flow);
  const legacy=flow?.grid_risk||{};
  const completeness=Number.isFinite(legacy.data_completeness)?legacy.data_completeness*100:null;
  const cards=[
    wbCard('风险框架 V2',statusText(model.status),model.riskSemantics),
    wbCard('风险策略',statusText(model.policyStatus),model.policyParameterStatus),
    wbCard('待确认风险域',String(model.pendingDomains.length),model.pendingDomains.length?model.pendingDomains.join('、'):'三个域均已有 confirmed policy'),
    wbCard(LEGACY_RISK_V1_LABEL,statusText(legacy.status||'not_calculated'),'完整度 '+(completeness===null?'—':formatNumber(completeness)+'%'))
  ];
  // 大段工程解释（factor → domain 明细、fingerprint、Legacy V1 语义）收进可折叠细节；
  // 面板内控件（含 evaluateRiskV2）始终挂载 DOM，展开即可操作。
  return '<div class="env-metric-cards">'+cards.join('')+'</div>'
    +'<div class="parameter-note">V2 只是 relative engineering index：不是事故概率、不是 SORA GRC/ARC，也不是绝对安全风险；没有 confirmed aggregation policy 时不显示伪 0。</div>'
    +wbDisclosure('Risk Framework V2 工程面板 · 因子 / 域明细与重新评估',renderRiskFrameworkV2Panel(flow));
}

// ---- 高级 · 高度层 -----------------------------------------------------------

/** 已配置高度层列表：缺 nominal 的高度层保持 pending_confirmation，不自动补值。 */
function altitudeLayerCatalogue(layers){
  const rows=layers.map(item=>{
    const nominal=item.nominal_altitude_m===null||item.nominal_altitude_m===undefined?'nominal 未配置（待工程确认）':escapeHtml(String(item.nominal_altitude_m))+' m nominal';
    return '<div class="list-row"><span><b>'+escapeHtml(item.name||item.altitude_layer_id)+'</b> '+statusBadge(item.status||'pending_confirmation')+'<small>'+escapeHtml(item.altitude_layer_id)+' · '+nominal+' · '+item.lower_altitude_m+'–'+item.upper_altitude_m+' m · '+escapeHtml(item.vertical_reference)+'</small></span><button data-delete-altitude-layer="'+escapeHtml(item.altitude_layer_id)+'">×</button></div>';
  }).join('');
  return '<div class="scroll-list">'+(rows||'<div class="empty-note">尚未定义高度层；步骤 03 的巡航高度层业务面板会明确显示“待工程确认”。</div>')+'</div>';
}

function altitudeLayerPanel(layers){
  return '<div class="form-grid">'
    +'<label>layer id<input class="panel-input" id="altitudeLayerId" placeholder="必填"></label>'
    +'<label>名称<input class="panel-input" id="altitudeLayerName" placeholder="可空，默认同 layer id"></label>'
    +'<label>垂向基准<select id="altitudeReference"><option value="">请选择（不猜）</option><option value="egm2008_orthometric">EGM2008 orthometric</option><option value="agl">AGL</option><option value="wgs84_ellipsoidal">WGS84 ellipsoidal</option><option value="unknown">unknown</option></select></label>'
    +'<label>nominal 高度 m<input class="panel-input" type="number" step="any" id="altitudeNominal" placeholder="必须显式填写"></label>'
    +'<label>下界 m<input class="panel-input" type="number" step="any" id="altitudeLower" placeholder="必须显式填写"></label>'
    +'<label>上界 m<input class="panel-input" type="number" step="any" id="altitudeUpper" placeholder="必须显式填写"></label>'
    +'<label>来源/依据<input class="panel-input" id="altitudeLayerSource" placeholder="工程依据、文件或评审记录"></label>'
    +'</div>'
    +'<label class="check-row"><input type="checkbox" id="altitudeLayerConfirmed">垂向基准与高度已由工程依据确认</label>'
    +'<button class="secondary full" id="saveAltitudeLayer">保存高度层（显式工程设定）</button>'
    +altitudeLayerCatalogue(layers)
    +wbDisclosure('垂向基准与 nominal 高度规则','<div class="flow-summary">内部 canonical vertical datum：EGM2008 orthometric。垂向基准与 nominal 高度只能显式填写：系统不猜垂向基准，也不提供任何默认真实高度。<b>缺少 nominal 的高度层保持待工程确认</b>（绝不自动取上下界中值）。高度层不预生成 voxel；巡航高度层的业务分配在步骤 03。</div>');
}

// ---- 操作 · 固定巡航高度层（Step2 / Step3 共用同一个 selector） ----------------

/**
 * Step2 的固定巡航高度层选择。
 *
 * 语义边界（B4X 第 9 节）：
 *  - 选项完全来自实际 AltitudeLayer 目录，**绝不写死 ALT-080 / 80 m**；
 *  - 这里只做"选择"（纯展示状态），**不生成任何高度层、也不重算任何结果**；
 *  - 生成约束场是用户在主操作区显式点击的动作，见 :func:`constraintFieldPanel`。
 */
export function altitudeSelectionPanel(flow,constraint){
  const summary=altitudeLayerCatalogSummary(flow);
  const selected=(constraint&&constraint.altitudeLayerId)||'';
  return sectionRoleLine('operate')
    +'<div class="parameter-note">固定巡航高度层是<b>正式航路</b>与<b>规划约束场</b>的共同输入：'
    +'两者必须使用同一层。系统不会自动为任何航路选择或分配高度。</div>'
    +altitudeLayerSelector({
      flow,id:'altitudeLayerSelector',selected,
      label:'固定巡航高度层',
      note:'选项来自项目的高度层目录（共 '+summary.total+' 层）。Step3 的正式航路规划使用同一层。',
      attribute:'data-altitude-layer-selector'
    })
    +(summary.total
      ?'<div class="parameter-note">需要新建或修改高度层？到「高级 → 高度层」按工程依据显式填写 nominal 高度与垂向基准；'
        +'缺少 nominal 的高度层保持待工程确认，不会进入正式规划。</div>'
      :'');
}

// ---- 结果 · 规划约束场（Planning Constraint Field） ----------------------------

/**
 * 约束场只读展示（B4X 第 10 / 11 / 12 节）。
 *
 * 与风险场**严格分离**：这里回答"哪里不能飞"（可行性），
 * 专题浏览与风险框架回答"哪里风险高"（软成本）。二者绝不合并成一张"综合风险"。
 */
export function constraintFieldPanel(flow,constraint){
  const context=constraint||{};
  const model=context.model||null;
  const altitudeLayerId=context.altitudeLayerId||'';
  const blockerItems=constraintBlockerItems(model);
  const canGenerate=Boolean(altitudeLayerId)&&flow?.grid?.status==='passed';
  const generateReason=!altitudeLayerId
    ?'请先在上方「固定巡航高度层」中选择一个高度层'
    :flow?.grid?.status!=='passed'
      ?'请先保存工作区范围以生成标准规划网格'
      :'';
  const body=[
    sectionRoleLine('result'),
    '<div class="parameter-note" data-semantic-split="risk-vs-constraint">'
      +'<b>风险场 = 软成本（哪里风险高）</b>；<b>约束场 = 可行性（哪里不能飞）</b>。'
      +'两者是两份独立证据，本产品绝不把它们合成一张"综合风险"表：'
      +'障碍物不进航路风险数学，风险分数也不参与可行性判定。</div>',
    context.error
      ?constraintLoadErrorHtml(altitudeLayerId,{message:context.error})
      :constraintSummaryHtml(model||{},{
        formatNumber:value=>Number(value).toLocaleString(),
        freshness:context.freshness,
        altitudeLayerLabelText:context.altitudeLayerLabel,
        mapLoaded:context.mapLoaded
      }),
    blockerList(blockerItems,
      altitudeLayerId?'当前高度层没有阻塞项记录':'尚未选择高度层，因此没有可评估的阻塞项'),
    primaryAction(
      '<button class="primary" id="generateConstraintField" '+(canGenerate?'':'disabled')+'>生成该高度层的规划约束场</button>',
      {note:canGenerate?'生成会写入项目结果（可重算），但不会修改任何净空策略、风险模型或航路。':generateReason}
    ),
    '<div class="parameter-note">约束场生成使用<b>已确认</b>的净空策略与<b>已确认</b>的障碍源；'
      +'任何缺证据的域都会如实保留为「证据不足」，绝不会被当成可通行。'
      +'证据不足的单元不允许发布为运行航路。</div>',
    mapLayerHint(altitudeLayerId)
  ].join('');
  return body;
}

/** 约束场的阻塞项/工程假设清单（只读模型，不推断）。 */
function constraintBlockerItems(model){
  if(!model||!model.present)return [];
  const items=[];
  const outcomes=model.outcomes||{};
  if(Number(outcomes.blocked)>0){
    const rows=Object.entries(model.blockedBy||{})
      .filter(([,value])=>Number(value)>0)
      .map(([domain,value])=>constraintOutcomeText('blocked')+'：'+domainLabel(domain)+' '+Number(value).toLocaleString()+' 格');
    items.push({kind:'blocker',text:'该高度层存在硬约束单元（不可穿越）',detail:rows.join(' · ')});
  }
  if(Number(outcomes.unknown)>0){
    items.push({
      kind:'assumption',
      text:'存在证据不足单元：'+Number(outcomes.unknown).toLocaleString()+' 格',
      detail:'证据不足不等于安全。是否允许候选航路试算穿越，由后端 unknown policy 决定；'
        +'穿越证据不足单元的候选永远不能发布为运行航路。'
    });
  }
  if(model.staleReason){
    items.push({kind:'blocker',text:'约束场需要重新计算',detail:String(model.staleReason)});
  }
  return items;
}

function domainLabel(domain){
  return {terrain:'地形',building:'建筑',tower:'铁塔',airspace:'禁飞/受限区域',critical_site:'保护要地'}[domain]||String(domain);
}

/** 地图图层提示：默认关闭，勾选后才读取逐格明细。 */
function mapLayerHint(altitudeLayerId){
  if(!altitudeLayerId)return '<div class="wb-empty">选择高度层后，可在地图图层抽屉中勾选「高度层障碍」查看逐格结果。</div>';
  return '<div class="parameter-note">地图默认只显示在线底图。要查看当前高度层的障碍格，请在图层抽屉中勾选'
    +'<b>「高度层障碍」</b>（默认关闭；默认只画障碍，可另行打开"证据不足 / 可通行"）。'
    +'逐格明细按需读取，只包含网格编号、结果与阻挡原因，几何复用标准网格。</div>';
}

// ---- 目标 / 下一步 -----------------------------------------------------------

/**
 * 本步目标（六区第一区）：用一句业务语言说清这一步要产出什么，
 * 而不是让用户从一长串表单里自己猜。
 */
function objectivePanel(flow){
  const workspace=flow?.workspace,grid=flow?.grid||{};
  const lines=[
    {label:'工作区',value:workspace?(workspace.area_km2).toLocaleString()+' km²':'尚未保存'},
    {label:'标准规划网格',value:grid.status==='passed'?(grid.count||0).toLocaleString()+' 格（L'+String(grid.level??'—')+'）'
      :grid.status==='blocked'?'已阻断':sourceStateText('not_calculated')},
    {label:'固定巡航高度层',value:(flow?.spatial_3d?.altitude_layers||[]).length+' 层可选'}
  ];
  return '<div class="flow-summary"><b>本步目标</b>：确定分析范围与标准规划网格，建立环境模型与航路风险场，'
    +'并按固定巡航高度层生成 Planning Constraint Field（哪里不能飞）。'
    +'<br><b>环境模型</b>（'+escapeHtml(canonicalNodeLabel('environment'))+'）与<b>航路风险场</b>'
    +'（'+escapeHtml(canonicalNodeLabel('risk_field'))+'）是本步的两个产物。</div>'
    +'<div class="metric-grid">'+lines.map(line=>'<b>'+escapeHtml(line.value)+'<small>'+escapeHtml(line.label)+'</small></b>').join('')+'</div>';
}

/**
 * 下一步（六区第六区）：只有本步真实满足后端 steps 门禁时才可点击。
 * 不满足时给出**中文原因**，绝不显示一个原因不明的灰按钮。
 */
function environmentNextStep(flow){
  const gridPassed=flow?.grid?.status==='passed';
  const blocked=flow?.grid?.status==='blocked';
  const reason=gridPassed?'':(blocked
    ?'标准规划网格已阻断：请缩小工作区范围，或显式提高资源上限后重新保存工作区'
    :'请先框选并保存工作区范围，生成标准规划网格');
  return nextStepBar({
    enabled:Boolean(flow?.steps&&flow.steps['2']),
    label:'下一步：航路规划与发布',
    reason,
    note:'进入下一步不会自动计算任何结果；航路候选、风险画像与安全验证都由你在第 03 步显式发起。'
  });
}

// ---- 渲染 -------------------------------------------------------------------

export function render({flow,draftWorkspace,gridDisplay,populationDisplayLabel,formatNumber,constraint}){
  const attributes=flow.grid_attributes||{},layers=(flow.spatial_3d||{}).altitude_layers||[];
  const context=constraint||{};
  const OPERATE=WORKSPACE_SEGMENTS.operate,RESULT=WORKSPACE_SEGMENTS.result,ADVANCED=WORKSPACE_SEGMENTS.advanced;
  const body=wbPanel('operate','',{segments:[
      ['env-op-workspace','工作区范围',wbBlock('工作区范围',
        wbSegHint(OPERATE,'env-op-workspace')+objectivePanel(flow)+workspaceRangePanel(draftWorkspace))],
      ['env-op-grid','标准网格与建筑环境',wbBlock('标准网格与建筑环境',
        wbSegHint(OPERATE,'env-op-grid')+gridEnvironmentPanel(flow)
        +blockerList(gridBlockerItems(flow),'标准规划网格没有阻塞项'))],
      ['env-op-altitude','固定巡航高度层',wbBlock('固定巡航高度层',
        wbSegHint(OPERATE,'env-op-altitude')+altitudeSelectionPanel(flow,context))]
    ]})
    +wbPanel('result','',{segments:[
      ['env-res-mapping','数据映射',wbBlock('数据映射',
        wbSegHint(RESULT,'env-res-mapping')+workspaceMappingSummary(flow)+mappingStatusCards(flow)+populationNodataPanel(flow))],
      ['env-res-constraint','规划约束场',wbBlock('规划约束场',
        wbSegHint(RESULT,'env-res-constraint')+constraintFieldPanel(flow,context))],
      ['env-res-theme','专题浏览',wbBlock('专题浏览',
        wbSegHint(RESULT,'env-res-theme')+themePanel(gridDisplay,attributes,populationDisplayLabel))]
    ]})
    +wbPanel('advanced','',{segments:[
      ['env-adv-risk','风险框架',wbBlock('风险框架',
        wbSegHint(ADVANCED,'env-adv-risk')+riskFrameworkPanel(flow,formatNumber))],
      ['env-adv-altitude','高度层',wbBlock('3D 高度层（工程设定）',
        wbSegHint(ADVANCED,'env-adv-altitude')+altitudeLayerPanel(layers))]
    ]});
  return shell('02','环境与风险','确定分析范围与规划网格，建立环境模型与航路风险场，并按高度层生成规划约束场。',
    body
    +environmentNextStep(flow));
}

/** 网格与环境的阻塞项（只读 flow 现有状态，不推断）。 */
function gridBlockerItems(flow){
  const grid=flow?.grid||{},health=flow?.workspace?.health||{};
  const items=[];
  if(grid.status==='blocked'){
    items.push({
      kind:'blocker',
      text:'标准规划网格已阻断：工作区超出资源上限',
      detail:(grid.blocked_message||grid.error?.message||'请缩小工作区范围或显式提高资源上限')
    });
  }
  const terrain=health.terrain?.status||'missing_data';
  if(terrain!=='passed'){
    items.push({kind:'blocker',text:'地形数据：'+statusText(terrain),detail:health.terrain?.message||''});
  }
  const buildings=flow?.grid_attributes?.buildings?.status||'missing_data';
  if(buildings==='missing_data'){
    items.push({kind:'assumption',text:'建筑环境数据：'+statusText(buildings),
      detail:'建筑相关的约束与净空判定会保持证据不足，不会被当成"没有建筑"。'});
  }
  return items;
}

export function bind(c){
  c.$('gridOutlineToggle').onchange=e=>c.setGridOutline(e.target.checked);document.querySelectorAll('[name="gridThemeMode"]').forEach(input=>input.onchange=e=>e.target.checked&&c.setGridTheme(e.target.value));
  c.$('drawWorkspace').onclick=c.startWorkspace;c.actionButton('clearWorkspace',c.clearWorkspace);c.actionButton('saveWorkspace',c.saveWorkspace);if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(3);
  if(c.$('evaluateRiskV2'))c.actionButton('evaluateRiskV2',()=>c.resourceAction('/api/grid-risk-v2/evaluate',{}));
  // 高度层选择：Step2 与 Step3 共用同一个 selector（同一个 id、同一份目录）。
  // 选择本身只切展示；生成约束场必须由下面的按钮显式触发。
  bindAltitudeLayerSelector(document,{
    id:'altitudeLayerSelector',
    onChange:value=>{if(c.constraintField)c.constraintField.select(value);}
  });
  // 主操作：生成该高度层的约束场（后端 POST，成功后重读 workflow 快照）。
  c.actionButton('generateConstraintField',async()=>{
    const altitudeLayerId=(c.constraintField&&c.constraintField.presentation().altitudeLayerId)
      ||selectedAltitudeLayerId(document,'altitudeLayerSelector');
    if(!c.constraintField)throw Error('约束场入口不可用，请刷新项目状态');
    if(!altitudeLayerId)throw Error('请先选择固定巡航高度层');
    await c.constraintField.generate(altitudeLayerId);
  });
  // Population NoData 语义：保存即显式工程确认；撤回回到"未确认"（绝不补 0）。
  c.actionButton('savePopulationNodata',()=>c.resourceAction('/api/population-nodata-policy',populationNodataPayload({
    mode:c.$('populationNodataMode')?.value,
    source:c.$('populationNodataSource')?.value,
    sourceId:c.$('populationNodataSourceId')?.value,
    evidence:c.$('populationNodataEvidence')?.value,
    confirmed:c.$('populationNodataConfirmed')?.checked===true
  })));
  c.actionButton('revokePopulationNodata',()=>c.resourceAction('/api/population-nodata-policy',{
    population_nodata_policy:{confirmed:false}
  }));
  // 人口映射过期后的唯一出口：只重算人口映射（不重算其它映射、不重新生成网格）。
  if(c.$('remapPopulation'))c.actionButton('remapPopulation',()=>c.remapPopulation());
  c.actionButton('saveAltitudeLayer',()=>{const optionalNumber=id=>{const field=c.$(id);const value=field?String(field.value??'').trim():'';return value===''?null:Number(value);};const layerId=c.$('altitudeLayerId').value.trim();return c.resourceAction('/api/spatial-3d/altitude-layer',{altitude_layer_id:layerId,name:c.$('altitudeLayerName').value.trim()||layerId,nominal_altitude_m:optionalNumber('altitudeNominal'),lower_altitude_m:optionalNumber('altitudeLower'),upper_altitude_m:optionalNumber('altitudeUpper'),vertical_reference:c.$('altitudeReference').value,source:c.$('altitudeLayerSource').value.trim(),confirmed:c.$('altitudeLayerConfirmed').checked});});
  document.querySelectorAll('[data-delete-altitude-layer]').forEach(button=>button.onclick=async()=>{try{button.disabled=true;await c.resourceAction('/api/spatial-3d/altitude-layer/delete',{altitude_layer_id:button.dataset.deleteAltitudeLayer});}catch(error){c.panelError(error.message);}finally{if(document.body.contains(button))button.disabled=false;}});
}
