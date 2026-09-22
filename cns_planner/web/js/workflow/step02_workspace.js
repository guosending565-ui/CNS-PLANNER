// =========================================================
// Step02 环境建模：工作区 / 网格 / 映射 / 专题 / 风险 / 高度层
//
// 信息架构
//  - 操作：工作区范围 / 标准网格与建筑环境
//  - 结果：数据映射 / 专题浏览
//  - 高级：风险框架 / 高度层
//  每个一级标签下同一时刻只显示一个任务（由 workbench.js 的 .wb-seg-active 保证），
//  所有分段内容始终挂载 DOM：切换任务不重新渲染业务内容，表单草稿与控件引用不丢。
//
// 语义边界（本次重组只改展示组织，不改任何业务契约）
//  - 工作区框选 / 清除 / 保存的 DOM id、API path 与 payload 完全不变；
//  - 标准网格层级仍只有 L8 / L7 / L6：L8 是建筑环境直接映射，L7/L6 的建筑网格
//    仍为 unsupported，超过数量限制时仍由后端降级（coarsening），前端不聚合/插值；
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
import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbSegHint,wbCard,wbDisclosure} from './common.js';
import {LEGACY_RISK_V1_LABEL,renderRiskFrameworkV2Panel,riskFrameworkV2Model,riskV2ThemeOptions} from './risk_framework_v2.js';

// ---- 二级任务分段 -----------------------------------------------------------
// id 稳定（env-*），标签是第一视觉层的业务语言；工程编号与算法 id 只出现在
// 说明或高级标签的内容里，不作为导航语言。
export const WORKSPACE_SEGMENTS={
  operate:[['env-op-workspace','工作区范围'],['env-op-grid','标准网格与建筑环境']],
  result:[['env-res-mapping','数据映射'],['env-res-theme','专题浏览']],
  advanced:[['env-adv-risk','风险框架'],['env-adv-altitude','高度层']]
};

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
  const value=Number(ratio);
  return Number.isFinite(value)?Math.round(value*100)+'%':'—';
}

/**
 * 人口映射覆盖统计。优先读后端统一载体 ``population_mapping``；旧快照没有该键时，
 * 只用既有的 full/partial/missing/outside 计数做**展示换算**（不重算任何业务数值）。
 *
 * 统一载体存在时六类计数**必须可闭合**：
 * ``full + partial + nodata_only + confirmed_zero + missing + outside === total``，
 * 且 ``covered === full + partial + confirmed_zero``、``unresolved === total - covered``。
 * ``closed`` / ``closure`` 就是为了让界面与测试都能直接验证这条恒等式。
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
    return {
      unified:true,total,covered,unresolved,full,partial,nodataOnly,confirmedZero,missing,outside,
      ratio:mapping.coverage_ratio,closure,
      closed:closure===total&&covered===full+partial+confirmedZero&&unresolved===total-covered
    };
  }
  const full=Number(population?.full_count)||0,partial=Number(population?.partial_count)||0,
    missing=Number(population?.missing_count)||0,outside=Number(population?.outside_count)||0;
  const total=full+partial+missing+outside,covered=full+partial;
  // 旧快照无法把 nodata_only / confirmed_zero 从 missing 里分出来：保持 null 而不是伪造 0。
  return {
    unified:false,total,covered,unresolved:total-covered,full,partial,
    nodataOnly:null,confirmedZero:null,missing,outside,
    ratio:total?covered/total:null,closure:total,
    closed:covered===full+partial
  };
}

/** 统一载体下的人口映射计数明细行（数字可闭合；旧快照只显示它真正拥有的四类）。 */
export function populationCoverageNote(counts){
  const head='已覆盖 '+counts.covered+' / '+counts.total+' 格（'+percentText(counts.ratio)+'）';
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

function workspaceRangePanel(draftWorkspace){
  return '<div class="button-row"><button class="primary" id="drawWorkspace">框选工作区</button><button class="secondary" id="clearWorkspace">清除</button></div>'
    +(draftWorkspace?'<div class="flow-summary">待保存：'+draftWorkspace.map(value=>value.toFixed(5)).join(', ')+'</div>':'')
    +'<button class="primary full" id="saveWorkspace" '+(!draftWorkspace?'disabled':'')+'>保存工作区范围</button>'
    +'<div class="parameter-note">框选只产生草稿：未保存前不改变工作区，也不会触发网格、映射或风险重算。</div>';
}

// ---- 操作 · 标准网格与建筑环境 ------------------------------------------------

/** 既有 MH/T 4063.1 标准网格状态行（含数量限制降级提示），取值与原来一致。 */
function standardGridLine(flow){
  const grid=flow?.grid||{};
  return grid?.status==='passed'
    ?'MH/T 4063.1 标准网格：L'+grid.level+' · '+grid.count+' 格'+(grid.coarsened?'（已按数量限制降级）':'')
    :'标准网格：未生成';
}

function gridEnvironmentPanel(flow){
  const grid=flow?.grid||{},health=flow?.workspace?.health||{};
  const levelOptions='<option value="8" '+((grid?.preferred_level??8)===8?'selected':'')+'>L8（建筑环境直接映射）</option>'
    +'<option value="7" '+(grid?.preferred_level===7?'selected':'')+'>L7（建筑网格 unsupported）</option>'
    +'<option value="6" '+(grid?.preferred_level===6?'selected':'')+'>L6（建筑网格 unsupported）</option>';
  return '<label>目标标准网格层级<select id="workspaceGridLevel">'+levelOptions+'</select></label>'
    +'<small>若工作区过大超过网格数量限制，系统仍会降级层级，此时不会聚合/插值 L8 建筑高度。</small>'
    +'<div class="flow-summary">'+standardGridLine(flow)+'<br>DEM：'+escapeHtml(statusText(health.terrain?.status||'missing_data'))+' · '+escapeHtml(health.terrain?.message||'DEM 状态未知')+'</div>'
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

// ---- 渲染 -------------------------------------------------------------------

export function render({flow,draftWorkspace,gridDisplay,populationDisplayLabel,formatNumber}){
  const attributes=flow.grid_attributes||{},layers=(flow.spatial_3d||{}).altitude_layers||[];
  const OPERATE=WORKSPACE_SEGMENTS.operate,RESULT=WORKSPACE_SEGMENTS.result,ADVANCED=WORKSPACE_SEGMENTS.advanced;
  const body=wbPanel('operate','',{segments:[
      ['env-op-workspace','工作区范围',wbBlock('工作区范围',wbSegHint(OPERATE,'env-op-workspace')+workspaceRangePanel(draftWorkspace))],
      ['env-op-grid','标准网格与建筑环境',wbBlock('标准网格与建筑环境',wbSegHint(OPERATE,'env-op-grid')+gridEnvironmentPanel(flow))]
    ]})
    +wbPanel('result','',{segments:[
      ['env-res-mapping','数据映射',wbBlock('数据映射',wbSegHint(RESULT,'env-res-mapping')+workspaceMappingSummary(flow)+mappingStatusCards(flow)+populationNodataPanel(flow))],
      ['env-res-theme','专题浏览',wbBlock('专题浏览',wbSegHint(RESULT,'env-res-theme')+themePanel(gridDisplay,attributes,populationDisplayLabel))]
    ]})
    +wbPanel('advanced','',{segments:[
      ['env-adv-risk','风险框架',wbBlock('风险框架',wbSegHint(ADVANCED,'env-adv-risk')+riskFrameworkPanel(flow,formatNumber))],
      ['env-adv-altitude','高度层',wbBlock('3D 高度层（工程设定）',wbSegHint(ADVANCED,'env-adv-altitude')+altitudeLayerPanel(layers))]
    ]});
  return shell('02','环境建模','框选、重画并保存分析范围。',
    body
    +'<button class="secondary full" id="nextStep" '+(!flow.steps['2']?'disabled':'')+'>下一步：航路规划</button>');
}

export function bind(c){
  c.$('gridOutlineToggle').onchange=e=>c.setGridOutline(e.target.checked);document.querySelectorAll('[name="gridThemeMode"]').forEach(input=>input.onchange=e=>e.target.checked&&c.setGridTheme(e.target.value));
  c.$('drawWorkspace').onclick=c.startWorkspace;c.actionButton('clearWorkspace',c.clearWorkspace);c.actionButton('saveWorkspace',c.saveWorkspace);if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(3);
  if(c.$('evaluateRiskV2'))c.actionButton('evaluateRiskV2',()=>c.resourceAction('/api/grid-risk-v2/evaluate',{}));
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
  // 人口映射 stale 后的唯一出口：只重算人口映射（不重算其它映射、不重新生成网格）。
  if(c.$('remapPopulation'))c.actionButton('remapPopulation',()=>c.remapPopulation());
  c.actionButton('saveAltitudeLayer',()=>{const optionalNumber=id=>{const field=c.$(id);const value=field?String(field.value??'').trim():'';return value===''?null:Number(value);};const layerId=c.$('altitudeLayerId').value.trim();return c.resourceAction('/api/spatial-3d/altitude-layer',{altitude_layer_id:layerId,name:c.$('altitudeLayerName').value.trim()||layerId,nominal_altitude_m:optionalNumber('altitudeNominal'),lower_altitude_m:optionalNumber('altitudeLower'),upper_altitude_m:optionalNumber('altitudeUpper'),vertical_reference:c.$('altitudeReference').value,source:c.$('altitudeLayerSource').value.trim(),confirmed:c.$('altitudeLayerConfirmed').checked});});
  document.querySelectorAll('[data-delete-altitude-layer]').forEach(button=>button.onclick=async()=>{try{button.disabled=true;await c.resourceAction('/api/spatial-3d/altitude-layer/delete',{altitude_layer_id:button.dataset.deleteAltitudeLayer});}catch(error){c.panelError(error.message);}finally{if(document.body.contains(button))button.disabled=false;}});
}
