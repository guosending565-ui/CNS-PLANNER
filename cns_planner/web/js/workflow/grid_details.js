// =========================================================
// 网格单元格点击详情
//
// 两级输出（BUG-MAP-001）：
//
//  1. ``gridCellSummary`` / ``gridCellSummaryHtml``：点击网格后**默认**显示的精确摘要
//     —— grid_id / L、人口（count + density + coverage）、地形关键高程、建筑 count / max、
//     当前专题关键值。**只有真实存在的值才出现**：缺失字段不占位、不补默认值、不补 0。
//  2. ``gridCellDetails`` / ``gridCellDetailsHtml``：原有完整诊断文本**一字不改地保留**，
//     在摘要下方放进「详细信息」展开区（默认折叠）。
//
// 安全：所有来自 flow / item 的文本都先经 ``escapeHtml`` 再拼接，调用方可以安全地把它
// 交给 ``innerHTML``；本模块绝不输出未转义的外部字符串。
// =========================================================
import {LEGACY_RISK_V1_LABEL,riskV2CellSummary} from './risk_framework_v2.js';
import {escapeHtml,statusText as labelFor} from './common.js';
import {parseRiskV2Theme,riskV2Value} from '../map/grid_overlay.js';

const statusText=status=>labelFor(status);

export function populationDisplayLabel(result){
  return result?.source_profile?.quantity==='population_count_per_source_pixel'
    ? '目标网格人口数（person）/人口密度（person/km²）'
    : '人口源值（兼容字段，非人数）';
}

function finite(value){return Number.isFinite(value);}

function riskValue(component,formatNumber){
  return component?.status==='passed'&&Number.isFinite(component.score)
    ? formatNumber(component.score)+' / '+(component.level||'未分级')
    : '无数据（'+statusText(component?.status||'not_calculated')+'）';
}

function factorValue(factor,formatNumber){
  return factor?.status==='passed'
    ? '归一化 '+formatNumber(factor.normalized)+' · contribution '+formatNumber(factor.contribution)
    : statusText(factor?.status||'not_available');
}

// ---- 默认精简摘要 -----------------------------------------------------------

/**
 * 人口摘要片段：count + density + coverage，仅有值的字段才出现。
 * 没有人数语义的旧快照退回源像元统计（标明"非人数"），仍然不补任何默认值。
 */
function populationSummary(item,formatNumber){
  const population=item?.population||{};
  const parts=[];
  if(finite(population.population_count_people))parts.push('count '+formatNumber(population.population_count_people)+' person');
  if(finite(population.population_density_people_km2))parts.push('density '+formatNumber(population.population_density_people_km2)+' person/km²');
  if(finite(population.source_coverage_fraction))parts.push('coverage '+formatNumber(population.source_coverage_fraction*100)+'%');
  else if(population.coverage_status)parts.push('coverage '+population.coverage_status);
  if(parts.length)return parts.join(' · ');
  if(finite(population.value_mean))return '源像元 mean '+formatNumber(population.value_mean)+'（非人数）';
  return null;
}

/** 地形关键高程：mean / min / max，仅有值的字段才出现。 */
function terrainSummary(item,flow,formatNumber){
  const terrain=item?.terrain||{};
  const unit=flow?.grid_attributes?.terrain?.elevation_unit||'m';
  const parts=[];
  if(finite(terrain.mean_elevation))parts.push('mean '+formatNumber(terrain.mean_elevation)+' '+unit);
  if(finite(terrain.min_elevation))parts.push('min '+formatNumber(terrain.min_elevation)+' '+unit);
  if(finite(terrain.max_elevation))parts.push('max '+formatNumber(terrain.max_elevation)+' '+unit);
  return parts.length?parts.join(' · '):null;
}

/** 建筑摘要：count / max —— 覆盖率与 P95 只在"当前专题"或"详细信息"里出现。 */
function buildingSummaryLine(item,formatNumber){
  const buildings=item?.buildings||{};
  const parts=[];
  if(finite(buildings.building_count))parts.push('count '+formatNumber(buildings.building_count));
  if(finite(buildings.height_max_m))parts.push('max '+formatNumber(buildings.height_max_m)+' m');
  return parts.length?parts.join(' · '):null;
}

/** 当前专题关键值：只映射"这一个专题真正展示的那个量"，无可用值时返回 null。 */
export function themeKeyValue(item,theme,formatNumber){
  const current=theme===null||theme===undefined?'none':String(theme);
  if(!current||current==='none')return null;
  const selection=parseRiskV2Theme(current);
  if(selection){
    const resolved=riskV2Value(item,selection);
    const label='Risk V2 '+(selection.kind==='domain'?'域':'因子')+' '+selection.id;
    return resolved.displayable
      ? {theme:current,label,value:formatNumber(resolved.value)+(resolved.partial?'（partial）':'')}
      : {theme:current,label,value:null};
  }
  const population=item?.population||{},terrain=item?.terrain||{},buildings=item?.buildings||{},
    traffic=item?.traffic||{},conflict=item?.conflict||{},risk=item?.risk||{};
  const passedValue=(component,format)=>
    component?.status==='passed'&&Number.isFinite(component.score)
      ? format(component.score,component.level)
      : null;
  const catalogue={
    population:{label:'人口密度',value:finite(population.population_density_people_km2)
      ?formatNumber(population.population_density_people_km2)+' person/km²'
      :(finite(population.value_mean)?formatNumber(population.value_mean)+'（源像元值，非人数）':null)},
    terrain:{label:'地形高程',value:finite(terrain.mean_elevation)
      ?formatNumber(terrain.mean_elevation)+' m':null},
    building_density:{label:'建筑密度',value:finite(buildings.building_coverage_ratio)
      ?formatNumber(buildings.building_coverage_ratio):null},
    building_p95:{label:'P95 建筑高度',value:finite(buildings.height_p95_m)
      ?formatNumber(buildings.height_p95_m)+' m':null},
    building_max:{label:'最大建筑高度',value:finite(buildings.height_max_m)
      ?formatNumber(buildings.height_max_m)+' m':null},
    traffic_exposure:{label:'交通暴露',value:finite(traffic.traffic_density_norm)
      ?formatNumber(traffic.traffic_density_norm):null},
    conflict_exposure:{label:'冲突暴露',value:finite(conflict.conflict_rate_norm)
      ?formatNumber(conflict.conflict_rate_norm):null},
    ground_risk:{label:'Ground Risk（'+LEGACY_RISK_V1_LABEL+'）',value:passedValue(
      risk.ground,(score,level)=>formatNumber(score)+' / '+(level||'未分级'))},
    overall_risk:{label:'Overall Risk（'+LEGACY_RISK_V1_LABEL+'）',value:passedValue(
      risk.overall,(score,level)=>formatNumber(score)+' / '+(level||'未分级'))},
  };
  const entry=catalogue[current];
  if(!entry||!entry.value)return null;
  return {theme:current,label:entry.label,value:entry.value};
}

/**
 * 点击网格的默认摘要（多行文本）。
 *
 * 只有真实存在的值才占一行：人口全空时给出该格的映射状态词，地形/建筑/专题完全无值时
 * 整行省略（绝不写 "0" 或 "无数据" 占位）。``theme`` 是当前地图专题（``gridDisplay.theme``）。
 */
export function gridCellSummary(item,flow,formatNumber,theme){
  const cell=item?.cell||{};
  const lines=[String(cell.grid_id||'未命名网格')+' · L'+String(cell.level??'—')];
  const population=populationSummary(item,formatNumber);
  lines.push('人口：'+(population||statusText(item?.population?.value_status||item?.population?.status||'missing_data')));
  const terrain=terrainSummary(item,flow,formatNumber);
  if(terrain)lines.push('地形：'+terrain);
  const buildings=buildingSummaryLine(item,formatNumber);
  if(buildings)lines.push('建筑：'+buildings);
  const themeEntry=themeKeyValue(item,theme,formatNumber);
  if(themeEntry)lines.push('当前专题（'+themeEntry.label+'）：'+themeEntry.value);
  return lines.join('\n');
}

/** 默认摘要的安全 HTML 片段（全部文本已转义）。 */
export function gridCellSummaryHtml(item,flow,formatNumber,theme){
  return '<div class="grid-info-summary">'+escapeHtml(gridCellSummary(item,flow,formatNumber,theme))+'</div>';
}

// ---- 完整诊断（原有行为，一字不改） -----------------------------------------

/** 一次点击命中的网格详情文本。 */
export function gridCellDetails(item,flow,formatNumber){
  const cell=item.cell;
  const populationResult=flow?.grid_attributes?.population||{},terrainResult=flow?.grid_attributes?.terrain||{};
  const population=item.population||{},terrain=item.terrain||{},buildings=item.buildings||{},
    airspace=item.airspace||{},traffic=item.traffic||{},conflict=item.conflict||{},
    populationSamples=population.valid_sample_count||0,terrainSamples=terrain.valid_sample_count||0;
  const populationCoverage=Number.isFinite(population.source_coverage_fraction)?formatNumber(population.source_coverage_fraction*100)+'%':'未知';
  const populationValues=Number.isFinite(population.population_count_people)
    ? '人口数 '+formatNumber(population.population_count_people)+' person · 密度 '+formatNumber(population.population_density_people_km2)+' person/km² · coverage '+populationCoverage+' · '+(population.coverage_status||'unknown')
    : populationSamples?'兼容源像元统计 mean '+formatNumber(population.value_mean)+' · sum '+formatNumber(population.value_sum)+'（非人数）':'无数据';
  const elevationUnit=terrainResult.elevation_unit||'m';
  const terrainPath=terrainResult.source?.path||'',terrainSource=terrainPath.split(/[\\/]/).pop()||'未记录';
  const terrainValues=terrainSamples
    ? 'mean '+formatNumber(terrain.mean_elevation)+' / min '+formatNumber(terrain.min_elevation)+' / max '+formatNumber(terrain.max_elevation)+' '+elevationUnit
    : '无数据';
  const airspaces=airspace.airspaces||[],shownAirspaces=airspaces.slice(0,8);
  const airspaceLines=shownAirspaces.map(hit=>{
    const identity=hit.feature_id===null||hit.feature_id===undefined?'':' #'+hit.feature_id;
    const classification=[hit.category,hit.type].filter(Boolean).join(' / ');
    const ratio=Number.isFinite(hit.intersection_ratio)?' · 覆盖 '+(hit.intersection_ratio*100).toFixed(1)+'%':' · 几何相交';
    const sourceAttributes=Object.entries(hit.source_attributes||{}).slice(0,6).map(([key,value])=>key+'='+value).join('，');
    return '  - '+(hit.name||hit.layer_id||'未命名图层')+identity+(classification?' · '+classification:'')+ratio+(sourceAttributes?' · '+sourceAttributes:'');
  });
  if(airspaces.length>shownAirspaces.length)airspaceLines.push('  - 另有 '+(airspaces.length-shownAirspaces.length)+' 个命中');
  const airspaceSummary='空域：'+statusText(airspace.status||'no_coverage')+' · 命中图层 '+(airspace.intersected_layer_count||0)+(airspaceLines.length?'\n'+airspaceLines.join('\n'):' · 无命中');
  const trafficSummary='Traffic Exposure：'+statusText(traffic.status||'not_calculated')+' · flights '+(traffic.flight_count||0)+' · flight_seconds '+formatNumber(traffic.flight_seconds)+' · density '+formatNumber(traffic.traffic_density_raw)+' · normalized '+formatNumber(traffic.traffic_density_norm);
  const conflictSummary='Conflict Exposure：'+statusText(conflict.status||'not_calculated')+' · count '+(conflict.conflict_count||0)+' · rate '+formatNumber(conflict.conflict_rate)+' · normalized '+formatNumber(conflict.conflict_rate_norm);
  const buildingSummary='建筑环境：'+statusText(buildings.status||'missing_data')+' · count '+formatNumber(buildings.building_count)+' · coverage '+formatNumber(buildings.building_coverage_ratio)+' · mean/P95/max '+formatNumber(buildings.height_mean_m)+' / '+formatNumber(buildings.height_p95_m)+' / '+formatNumber(buildings.height_max_m)+' m';
  const risk=item.risk||{},ground=risk.ground||{},operationalAir=risk.air||{},overall=risk.overall||{},riskResult=flow?.grid_risk||{};
  const p=ground.contributors?.population||{},t=ground.contributors?.terrain||{};
  const riskSummary=LEGACY_RISK_V1_LABEL+' · Ground Risk：'+riskValue(ground,formatNumber)+'\n'+
    '  P：'+factorValue(p,formatNumber)+'\n  T：'+factorValue(t,formatNumber)+(t.raw?.relief===undefined?'':' · relief '+formatNumber(t.raw.relief))+'\n'+
    'Operational Air Risk：'+riskValue(operationalAir,formatNumber)+'\nAirspace：not applicable（display-only reference layer）\n'+
    'Overall Risk：'+riskValue(overall,formatNumber)+' · 完整度 '+formatNumber((overall.data_completeness||0)*100)+'%\n'+
    '风险语义：'+(overall.semantics||risk.semantics||'relative_index')+' · '+(riskResult.algorithm_id||'未计算')+'@'+(riskResult.algorithm_version||'-');
  const v2Summary=riskV2CellSummary(item.risk_v2,formatNumber);
  return cell.grid_id+' · L'+cell.level+'\n'+
    populationDisplayLabel(populationResult)+'：样本 '+populationSamples+' · '+populationValues+' · '+(population.value_status||population.quantity_status||'missing_data')+'\n'+
    'DEM：样本 '+terrainSamples+' · '+terrainValues+' · '+(terrainResult.unit_status||'单位来源未知')+' · '+terrainSource+'\n'+
    buildingSummary+'\n'+airspaceSummary+'\n'+trafficSummary+'\n'+conflictSummary+'\n'+riskSummary+'\n'+v2Summary;
}

/**
 * 点击网格的完整弹窗内容：默认精简摘要 + 可折叠的「详细信息」。
 *
 * 展开区里就是改造前那份完整诊断文本，因此任何诊断能力都没有丢失；全部文本已转义，
 * 调用方可以安全地赋给 ``innerHTML``。
 */
export function gridCellDetailsHtml(item,flow,formatNumber,theme){
  return gridCellSummaryHtml(item,flow,formatNumber,theme)
    +'<details class="grid-info-detail"><summary>详细信息</summary>'
    +'<div class="grid-info-detail-body">'+escapeHtml(gridCellDetails(item,flow,formatNumber))+'</div>'
    +'</details>';
}
