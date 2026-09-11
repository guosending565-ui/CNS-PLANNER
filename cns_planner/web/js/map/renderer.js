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
  const kind={population:'population',terrain:'terrain',traffic_exposure:'traffic',conflict_exposure:'conflict'}[display.theme]||null;
  const riskKind={ground_risk:'ground',airspace_risk:'airspace_constraint',overall_risk:'overall'}[display.theme]||null;
  if(!view||(!kind&&!riskKind))return;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{}),usable=result.status==='passed'||result.status==='missing_data';
  const breaks=kind?(kind==='population'?cache.populationBreaks:kind==='terrain'?cache.terrainBreaks:riskBreaks):riskBreaks;
  const palette=kind?(kind==='population'?palettes.population:kind==='terrain'?palettes.terrain:palettes.risk):palettes.risk,visible=visibleBounds();ctx.save();
  for(const item of cache.cells){
    const bbox=item.cell.bbox;if(!gridTheme.bboxIntersects(bbox,visible))continue;
    const attributes=kind?item[kind]:item.risk?.[riskKind],value=kind?(kind==='population'?attributes?.value_mean:kind==='terrain'?attributes?.mean_elevation:kind==='traffic'?attributes?.traffic_density_norm:attributes?.conflict_rate_norm):attributes?.score;
    const hasData=usable&&attributes?.status==='passed'&&Number.isFinite(value),southwest=screenPoint([bbox[0],bbox[1]]),northeast=screenPoint([bbox[2],bbox[3]]);
    ctx.globalAlpha=hasData ? .72 : .48;ctx.fillStyle=hasData?gridTheme.colorForValue(value,breaks,palette):gridTheme.NO_DATA_COLOR;
    ctx.fillRect(southwest[0],northeast[1],northeast[0]-southwest[0],southwest[1]-northeast[1]);
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
