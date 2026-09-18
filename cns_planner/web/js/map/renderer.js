import {parseRiskV2Theme,riskV2Breaks,riskV2Value} from './grid_overlay.js';

export function drawLine(ctx,screenPoint,view,path,color,width=3,dash=[]){
  if(!view||!path?.length)return;
  ctx.save();ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();
  path.forEach((point,index)=>{const [x,y]=screenPoint(point);index?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();ctx.restore();
}

export function drawWorkspace(ctx,screenPoint,bbox,color='#147ac3'){
  if(!bbox)return;const a=screenPoint([bbox[0],bbox[1]]),b=screenPoint([bbox[2],bbox[3]]);
  ctx.save();ctx.fillStyle=color+'22';ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,4]);
  ctx.fillRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]));
  ctx.strokeRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]));ctx.restore();
}

export function drawGridTheme(options){
  const {ctx,view,flow,cache,display,visibleBounds,screenPoint,gridTheme,palettes,riskBreaks}=options;
  const kind={population:'population',terrain:'terrain',traffic_exposure:'traffic',conflict_exposure:'conflict',building_density:'buildings',building_p95:'buildings',building_max:'buildings'}[display.theme]||null;
  const riskKind={ground_risk:'ground',overall_risk:'overall'}[display.theme]||null;
  const v2=parseRiskV2Theme(display.theme);
  if(!view||(!kind&&!riskKind&&!v2))return;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{}),usable=result.status==='passed'||result.status==='missing_data';
  const buildingBreaks={building_density:cache.buildingCoverageBreaks,building_p95:cache.buildingP95Breaks,building_max:cache.buildingMaxBreaks};
  const breaks=v2?riskV2Breaks(cache,v2):(kind?(kind==='population'?cache.populationBreaks:kind==='terrain'?cache.terrainBreaks:kind==='buildings'?buildingBreaks[display.theme]:riskBreaks):riskBreaks);
  const palette=v2?palettes.risk:(kind?(kind==='population'?palettes.population:kind==='terrain'?palettes.terrain:kind==='buildings'?palettes.buildings:palettes.risk):palettes.risk),visible=visibleBounds();ctx.save();
  for(const item of cache.cells){
    const bbox=item.cell.bbox;if(!gridTheme.bboxIntersects(bbox,visible))continue;
    const attributes=kind?item[kind]:item.risk?.[riskKind],hasPopulationQuantity=Number.isFinite(attributes?.population_density_people_km2),populationValue=hasPopulationQuantity?attributes.population_density_people_km2:attributes?.value_mean,buildingValue=display.theme==='building_density'?attributes?.building_coverage_ratio:display.theme==='building_p95'?attributes?.height_p95_m:attributes?.height_max_m;
    // Risk Framework V2 never paints a pending/unresolved/missing value as zero.
    const v2Value=v2?riskV2Value(item,v2):null;
    const value=v2?v2Value.value:(kind?(kind==='population'?populationValue:kind==='terrain'?attributes?.mean_elevation:kind==='buildings'?buildingValue:kind==='traffic'?attributes?.traffic_density_norm:attributes?.conflict_rate_norm):attributes?.score);
    const semanticStatus=v2?v2Value.status:(kind==='population'&&hasPopulationQuantity?'passed':attributes?.status);
    const hasData=v2?v2Value.displayable:(usable&&semanticStatus==='passed'&&Number.isFinite(value)),southwest=screenPoint([bbox[0],bbox[1]]),northeast=screenPoint([bbox[2],bbox[3]]);
    const partial=v2?v2Value.partial:(kind==='population'&&hasData&&attributes.coverage_status==='partial');ctx.globalAlpha=hasData?(partial ? .55 : .72):.48;ctx.fillStyle=hasData?gridTheme.colorForValue(value,breaks,palette):gridTheme.NO_DATA_COLOR;
    ctx.fillRect(southwest[0],northeast[1],northeast[0]-southwest[0],southwest[1]-northeast[1]);
    if(partial){ctx.globalAlpha=.9;ctx.strokeStyle='#714f86';ctx.lineWidth=1.2;ctx.setLineDash([4,3]);ctx.strokeRect(southwest[0]+.6,northeast[1]+.6,northeast[0]-southwest[0]-1.2,southwest[1]-northeast[1]-1.2);ctx.setLineDash([]);}
  }
  ctx.restore();
}

export function drawStandardGrid(options){
  const {ctx,view,grid,display,enabled,visibleBounds,screenPoint,gridTheme}=options;
  if(!view||!display.outline||!enabled||grid?.status!=='passed')return;
  const visible=visibleBounds();ctx.save();ctx.strokeStyle='#5a35a5';ctx.globalAlpha=.96;ctx.lineWidth=1.6;ctx.setLineDash([]);
  for(const cell of grid.cells||[]){
    const bbox=cell.bbox;if(!gridTheme.bboxIntersects(bbox,visible))continue;
    const southwest=screenPoint([bbox[0],bbox[1]]),northeast=screenPoint([bbox[2],bbox[3]]);
    ctx.strokeRect(southwest[0],northeast[1],northeast[0]-southwest[0],southwest[1]-northeast[1]);
  }
  ctx.restore();
}
