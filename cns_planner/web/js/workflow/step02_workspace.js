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
//    缺 nominal 的高度层保持 pending_confirmation（绝不取上下界中值）。
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
 * 人口映射状态：与原摘要同一取值来源——stale 优先，其次 value_status，最后 status。
 * 只做取值映射，不做任何重算。
 */
function populationMappingStatus(population){
  return statusText(population?.status==='stale'?'stale':population?.value_status||population?.status||'not_calculated');
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
  return '<div class="env-metric-cards">'+[
    wbCard('人口映射',populationMappingStatus(population),
      'full '+(population.full_count||0)+' / partial '+(population.partial_count||0)+' / missing '+(population.missing_count||0)+' / outside '+(population.outside_count||0)+' 格'),
    wbCard('地形 DEM 映射',statusText(terrain.status||'not_calculated'),
      '已处理 '+(terrain.count||0)+' 格（有效 '+(terrain.covered_count||0)+' 格）'),
    wbCard('低空空域映射',statusText(airspace.status||'not_calculated'),
      '已处理 '+(airspace.count||0)+' 格（命中 '+(airspace.hit_count||0)+' 格）'),
    wbCard('建筑环境映射',statusText(buildings.status||'not_calculated'),
      '已覆盖 '+(buildings.covered_count||0)+' / '+(buildings.count||0)+' 格'),
    wbCard('交通暴露映射',statusText(traffic.status||'not_calculated'),
      '已处理 '+(traffic.count||0)+' 格（命中 '+(traffic.covered_count||0)+' 格）'),
    wbCard('冲突暴露映射',statusText(conflict.status||'not_calculated'),
      '已处理 '+(conflict.count||0)+' 格（命中 '+(conflict.covered_count||0)+' 格）')
  ].join('')+'</div>';
}

/**
 * 建筑环境链摘要：FABDEM DTM → GBA buildings → L8 grid → 建筑网格映射。
 * 只读 flow.workspace.health 与 flow.grid_attributes.buildings 的现有状态，
 * 不假定任何数据已就绪（缺失一律 missing_data）。
 * @returns {string} 纯 HTML
 */
export function buildingEnvironmentSummary(flow){
  const health=flow?.workspace?.health||{},buildings=flow?.grid_attributes?.buildings||{};
  return '<div class="flow-summary"><b>建筑环境链</b><br>FABDEM DTM：'+escapeHtml(statusText(health.terrain_dtm?.status||'missing_data'))
    +' · GBA buildings：'+escapeHtml(statusText(health.buildings?.status||'missing_data'))
    +' · L8 grid：'+escapeHtml(statusText(health.building_grid?.status||'missing_data'))
    +'<br>建筑网格映射：'+escapeHtml(statusText(buildings.status||'not_calculated'))
    +' · '+(buildings.covered_count||0)+' / '+(buildings.count||0)+' 格</div>';
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
  const workspace=flow?.workspace,health=workspace?.health;
  if(!workspace)return '<div class="empty-note">尚未保存工作区。点击“框选工作区”后在地图拖出矩形。</div>';
  return '<div class="metric-grid"><b>'+(workspace.area_km2).toLocaleString()+' km²<small>工作区面积</small></b>'
    +'<b>'+escapeHtml(statusText(health?.population?.status||'missing_data'))+'<small>人口数据</small></b>'
    +'<b>'+escapeHtml(statusText(health?.airspace?.status||'missing_data'))+'<small>空域数据</small></b>'
    +'<b>'+escapeHtml(String(health?.loaded_layer_count||0))+'<small>已加载图层</small></b></div>'
    +'<div class="missing-list">DEM：'+escapeHtml(statusText(health?.terrain?.status||'missing_data'))+' · 建筑：missing_data · 财产：missing_data</div>';
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
      ['env-res-mapping','数据映射',wbBlock('数据映射',wbSegHint(RESULT,'env-res-mapping')+workspaceMappingSummary(flow)+mappingStatusCards(flow))],
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
  c.actionButton('saveAltitudeLayer',()=>{const optionalNumber=id=>{const field=c.$(id);const value=field?String(field.value??'').trim():'';return value===''?null:Number(value);};const layerId=c.$('altitudeLayerId').value.trim();return c.resourceAction('/api/spatial-3d/altitude-layer',{altitude_layer_id:layerId,name:c.$('altitudeLayerName').value.trim()||layerId,nominal_altitude_m:optionalNumber('altitudeNominal'),lower_altitude_m:optionalNumber('altitudeLower'),upper_altitude_m:optionalNumber('altitudeUpper'),vertical_reference:c.$('altitudeReference').value,source:c.$('altitudeLayerSource').value.trim(),confirmed:c.$('altitudeLayerConfirmed').checked});});
  document.querySelectorAll('[data-delete-altitude-layer]').forEach(button=>button.onclick=async()=>{try{button.disabled=true;await c.resourceAction('/api/spatial-3d/altitude-layer/delete',{altitude_layer_id:button.dataset.deleteAltitudeLayer});}catch(error){c.panelError(error.message);}finally{if(document.body.contains(button))button.disabled=false;}});
}
