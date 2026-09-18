// =========================================================
// 网格单元格点击详情
//
// 纯格式化：把一次点击命中的网格属性渲染成可复制的文本证据。
// 所有字段都来自 flow，未提供的字段一律显示"无数据"，不补默认值。
// =========================================================
import {LEGACY_RISK_V1_LABEL,riskV2CellSummary} from './risk_framework_v2.js';
import {statusText as labelFor} from './common.js';

const statusText=status=>labelFor(status);

export function populationDisplayLabel(result){
  return result?.source_profile?.quantity==='population_count_per_source_pixel'
    ? '目标网格人口数（person）/人口密度（person/km²）'
    : '人口源值（兼容字段，非人数）';
}

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
