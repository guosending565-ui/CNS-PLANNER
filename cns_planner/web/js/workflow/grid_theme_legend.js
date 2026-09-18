// Grid-theme legend rendering for the map shell.  Kept out of ``main.js`` so the assembly
// module stays thin; the DOM ids and the produced strings are unchanged.
export const POPULATION_PALETTE=['#fff7bc','#fee391','#fec44f','#fe9929','#cc4c02'];
export const TERRAIN_PALETTE=['#2c7bb6','#abd9e9','#ffffbf','#fdae61','#d7191c'];
export const BUILDING_PALETTE=['#fff7ec','#fdd49e','#fc8d59','#d7301f','#7f0000'];
export const RISK_PALETTE=['#2ca25f','#99d8c9','#fee08b','#f46d43','#a50026'];
const THEME_KIND={
  population:'population',terrain:'terrain',traffic_exposure:'traffic',
  conflict_exposure:'conflict',building_density:'buildings',building_p95:'buildings',
  building_max:'buildings',
};
const THEME_RISK_KIND={ground_risk:'ground',overall_risk:'overall'};

export function gridThemeLegendModel(flow,cache,display){
  const kind=THEME_KIND[display.theme]||null,riskKind=THEME_RISK_KIND[display.theme]||null;
  if(!kind&&!riskKind)return null;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{});
  const buildingBreaks={
    building_density:cache.buildingCoverageBreaks,
    building_p95:cache.buildingP95Breaks,
    building_max:cache.buildingMaxBreaks,
  };
  const breaks=kind
    ?(kind==='population'?cache.populationBreaks:kind==='terrain'?cache.terrainBreaks:
      kind==='buildings'?buildingBreaks[display.theme]:[])
    :[];
  const riskTitles={ground:'Ground Risk',overall:'Overall Risk'};
  const kindTitles={
    terrain:'平均高程',traffic:'Traffic Exposure',conflict:'Conflict Exposure',
    buildings:{building_density:'建筑密度',building_p95:'P95 建筑高度',building_max:'最大建筑高度'}[display.theme],
  };
  const palette=kind
    ?(kind==='population'?POPULATION_PALETTE:kind==='terrain'?TERRAIN_PALETTE:
      kind==='buildings'?BUILDING_PALETTE:RISK_PALETTE)
    :RISK_PALETTE;
  const title=kind?(kind==='population'?'目标网格人口密度':kindTitles[kind]):riskTitles[riskKind];
  const unit=riskKind||kind==='traffic'||kind==='conflict'?'0–1'
    :kind==='terrain'?(result.elevation_unit||'m')
    :kind==='buildings'?(display.theme==='building_density'?'ratio':'m'):'person/km²';
  const path=result.source?.path||'',source=path.split(/[\\/]/).pop()||'未记录';
  const shown=breaks.length?[breaks[0],breaks[Math.floor((breaks.length-1)/2)],breaks[breaks.length-1]]:[];
  return {kind,riskKind,result,palette,title,unit,shown,source};
}

export function gridThemeLegendNote(model,statusText,formatNumber){
  const {riskKind,kind,result,source}=model;
  if(riskKind){
    return '相对风险指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+
      ' · 完整度 '+formatNumber((result.data_completeness||0)*100)+'%';
  }
  if(kind==='buildings'){
    return 'GBA L8 来源参数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+
      ' · 0 与无数据严格区分 · '+source+' · '+statusText(result.status||'not_calculated');
  }
  if(kind==='traffic'||kind==='conflict'){
    return '相对暴露指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+
      ' · '+statusText(result.status||'not_calculated');
  }
  if(kind==='population'){
    return 'WorldPop count 经面积权重守恒映射，再除以实际网格面积 · '+
      statusText(result.quantity_status||'not_calculated');
  }
  return '均值分级 · '+(result.unit_status||'单位来源未知')+' · '+source+' · '+
    statusText(result.status||'not_calculated');
}
