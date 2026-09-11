const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d'),map=$('map');
let state=null,flow=null,view=null,bitmap=null,imageView=null,timer,serial=0,drag=null,drawStart=null,draftWorkspace=null;
let currentStep=1,interactionMode='pan',browseKind='basemap',browseParent='',selectedFile='',renderController=null;
let gridDataSerial=0;
let gridDisplay={outline:true,theme:'none'};
let gridRenderCache={cells:[],byId:new Map(),spatial:null,populationBreaks:[],terrainBreaks:[]};
const populationPalette=['#fff7bc','#fee391','#fec44f','#fe9929','#cc4c02'];
const terrainPalette=['#2c7bb6','#abd9e9','#ffffbf','#fdae61','#d7191c'];
const riskPalette=['#2ca25f','#99d8c9','#fee08b','#f46d43','#a50026'],riskBreaks=[0,.2,.4,.6,.8,1];
const client=crypto.randomUUID(),onlineTiles=new OnlineTiles(()=>requestAnimationFrame(paint),text=>$('tileStatus').textContent=text);

async function api(url,options={}){
  const response=await fetch(url,{...options,headers:{'X-CNS-Token':state?.token||'',...options.headers}});
  const type=response.headers.get('content-type')||'';
  const data=type.includes('json')?await response.json():await response.blob();
  if(!response.ok)throw Error(data.error||'请求失败');
  return data;
}
async function mutate(action,payload={}){
  const data=await api('/api/workflow/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  flow=data;rebuildGridRenderCache();
  try{await syncGridApis();}catch(exc){showError('网格专题同步失败：'+exc.message);}
  renderWorkflow();paint();return data;
}
async function syncGridApis(){
  const request=++gridDataSerial;
  const [grid,attributes]=await Promise.all([api('/api/workspace/grid'),api('/api/workspace/grid/attributes')]);
  if(request!==gridDataSerial)return;
  flow={...flow,grid,grid_attributes:attributes};rebuildGridRenderCache();
}
function showError(message){$('error').hidden=!message;$('error').textContent=message||'';}
function panelError(message){const target=$('panelError');if(target)target.textContent=message||'';else showError(message);}
function size(){return [Math.max(1,Math.round(map.clientWidth)),Math.max(1,Math.round(map.clientHeight))];}
function fit(box){if(!box)return;const [w,h]=size();view={x:(box[0]+box[2])/2,y:(box[1]+box[3])/2,res:Math.max((box[2]-box[0])/w,(box[3]-box[1])/h)*1.1};queue();}
function mapBounds(){const [w,h]=size();return [view.x-w*view.res/2,view.y-h*view.res/2,view.x+w*view.res/2,view.y+h*view.res/2];}
function lonLatToMercator(lon,lat){const limited=Math.max(-85.05112878,Math.min(85.05112878,lat));return [lon*Math.PI/180*6378137,6378137*Math.log(Math.tan(Math.PI/4+limited*Math.PI/360))];}
function mercatorToLonLat(x,y){return [x/6378137*180/Math.PI,(2*Math.atan(Math.exp(y/6378137))-Math.PI/2)*180/Math.PI];}
function screenPoint(coordinate){const p=lonLatToMercator(coordinate[0],coordinate[1]),[w,h]=size();return [w/2+(p[0]-view.x)/view.res,h/2-(p[1]-view.y)/view.res];}
function eventLonLat(event){const rect=map.getBoundingClientRect(),[w,h]=size();return mercatorToLonLat(view.x+(event.clientX-rect.left-w/2)*view.res,view.y-(event.clientY-rect.top-h/2)*view.res);}
function visibleLonLatBounds(){
  const bbox=mapBounds(),southwest=mercatorToLonLat(bbox[0],bbox[1]),northeast=mercatorToLonLat(bbox[2],bbox[3]);
  return [southwest[0],southwest[1],northeast[0],northeast[1]];
}
function fitLonLatBbox(bbox){
  if(!bbox)return;
  const southwest=lonLatToMercator(bbox[0],bbox[1]),northeast=lonLatToMercator(bbox[2],bbox[3]);
  fit([southwest[0],southwest[1],northeast[0],northeast[1]]);
}

function paint(){
  const [w,h]=size();if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
  ctx.fillStyle='#f3f4f2';ctx.fillRect(0,0,w,h);onlineTiles.paint(ctx,view,w,h,'base');
  if(bitmap&&view&&imageView){const b=imageView;ctx.drawImage(bitmap,w/2+(b[0]-view.x)/view.res,h/2-(b[3]-view.y)/view.res,(b[2]-b[0])/view.res,(b[3]-b[1])/view.res);}
  onlineTiles.paint(ctx,view,w,h,'annotation');drawWorkflowOverlay();
}
function drawLine(path,color,width=3,dash=[]){
  if(!view||!path?.length)return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();
  path.forEach((point,index)=>{const [x,y]=screenPoint(point);index?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();ctx.restore();
}
function drawWorkspace(bbox,color='#147ac3'){
  if(!bbox)return;const a=screenPoint([bbox[0],bbox[1]]),b=screenPoint([bbox[2],bbox[3]]);
  ctx.save();ctx.fillStyle=color+'22';ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,4]);
  ctx.fillRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]));
  ctx.strokeRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]));ctx.restore();
}
function buildSpatialGrid(cells){
  if(!cells.length)return null;
  const bounds=[
    Math.min(...cells.map(item=>item.cell.bbox[0])),Math.min(...cells.map(item=>item.cell.bbox[1])),
    Math.max(...cells.map(item=>item.cell.bbox[2])),Math.max(...cells.map(item=>item.cell.bbox[3]))
  ];
  const columns=Math.min(64,Math.max(1,Math.ceil(Math.sqrt(cells.length)))),rows=columns,buckets=new Map();
  const width=(bounds[2]-bounds[0])/columns,height=(bounds[3]-bounds[1])/rows;
  const index=(value,start,span,count)=>Math.max(0,Math.min(count-1,Math.floor((value-start)/span)));
  for(const item of cells){
    const bbox=item.cell.bbox,x0=index(bbox[0],bounds[0],width,columns),x1=index(bbox[2],bounds[0],width,columns);
    const y0=index(bbox[1],bounds[1],height,rows),y1=index(bbox[3],bounds[1],height,rows);
    for(let x=x0;x<=x1;x++)for(let y=y0;y<=y1;y++){
      const key=x+':'+y;if(!buckets.has(key))buckets.set(key,[]);buckets.get(key).push(item);
    }
  }
  return {bounds,columns,rows,width,height,buckets,index};
}
function rebuildGridRenderCache(){
  const grid=flow?.grid,attributes=flow?.grid_attributes||{},population=attributes.population||{},terrain=attributes.terrain||{},airspace=attributes.airspace||{},traffic=attributes.traffic||{},conflict=attributes.conflict||{},risk=flow?.grid_risk||{};
  const geometryById=new Map((grid?.cells||[]).map(cell=>[cell.grid_id,cell]));
  const cells=[];
  for(const [gridId,cell] of geometryById)cells.push({
    cell,
    population:population.cells?.[gridId]||null,
    terrain:terrain.cells?.[gridId]||null,
    airspace:airspace.cells?.[gridId]||null,
    traffic:traffic.cells?.[gridId]||null,
    conflict:conflict.cells?.[gridId]||null,
    risk:risk.cells?.[gridId]||null
  });
  const usable=status=>status==='passed'||status==='missing_data';
  const populationValues=usable(population.status)?cells.map(item=>item.population?.value_mean).filter(Number.isFinite):[];
  const terrainValues=usable(terrain.status)?cells.map(item=>item.terrain?.mean_elevation).filter(Number.isFinite):[];
  gridRenderCache={
    cells,
    byId:new Map(cells.map(item=>[item.cell.grid_id,item])),
    spatial:buildSpatialGrid(cells),
    populationBreaks:GridTheme.quantileBreaks(populationValues),
    terrainBreaks:GridTheme.quantileBreaks(terrainValues)
  };
  if($('gridInfo'))$('gridInfo').hidden=true;
  updateGridNotice();
  updateGridThemeLegend();
}
function findGridCell(lon,lat){
  const spatial=gridRenderCache.spatial;if(!spatial)return null;
  const {bounds,columns,rows,width,height,buckets,index}=spatial;
  if(lon<bounds[0]||lon>=bounds[2]||lat<bounds[1]||lat>=bounds[3])return null;
  const candidates=buckets.get(index(lon,bounds[0],width,columns)+':'+index(lat,bounds[1],height,rows))||[];
  return candidates.find(item=>GridTheme.bboxContainsHalfOpen(item.cell.bbox,lon,lat))||null;
}
function drawGridThemes(){
  const kind={population:'population',terrain:'terrain',traffic_exposure:'traffic',conflict_exposure:'conflict'}[gridDisplay.theme]||null;
  const riskKind={ground_risk:'ground',airspace_risk:'airspace_constraint',overall_risk:'overall'}[gridDisplay.theme]||null;
  if(!view||(!kind&&!riskKind))return;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{}),usable=result.status==='passed'||result.status==='missing_data';
  const breaks=kind?(kind==='population'?gridRenderCache.populationBreaks:kind==='terrain'?gridRenderCache.terrainBreaks:riskBreaks):riskBreaks;
  const palette=kind?(kind==='population'?populationPalette:kind==='terrain'?terrainPalette:riskPalette):riskPalette,visible=visibleLonLatBounds();ctx.save();
  for(const item of gridRenderCache.cells){
    const bbox=item.cell.bbox;if(!GridTheme.bboxIntersects(bbox,visible))continue;
    const attributes=kind?item[kind]:item.risk?.[riskKind],value=kind?(kind==='population'?attributes?.value_mean:kind==='terrain'?attributes?.mean_elevation:kind==='traffic'?attributes?.traffic_density_norm:attributes?.conflict_rate_norm):attributes?.score;
    const hasData=usable&&attributes?.status==='passed'&&Number.isFinite(value);
    const southwest=screenPoint([bbox[0],bbox[1]]),northeast=screenPoint([bbox[2],bbox[3]]);
    ctx.globalAlpha=hasData ? .72 : .48;ctx.fillStyle=hasData?GridTheme.colorForValue(value,breaks,palette):GridTheme.NO_DATA_COLOR;
    ctx.fillRect(southwest[0],northeast[1],northeast[0]-southwest[0],southwest[1]-northeast[1]);
  }
  ctx.restore();
}
function drawStandardGrid(){
  const grid=flow?.grid;
  if(!view||!gridDisplay.outline||!$('gridLayer')?.checked||grid?.status!=='passed')return;
  const visible=visibleLonLatBounds();ctx.save();ctx.strokeStyle='#5a35a5';ctx.globalAlpha=.96;ctx.lineWidth=1.6;ctx.setLineDash([]);
  for(const cell of grid.cells||[]){
    const bbox=cell.bbox;
    if(!GridTheme.bboxIntersects(bbox,visible))continue;
    const southwest=screenPoint([bbox[0],bbox[1]]),northeast=screenPoint([bbox[2],bbox[3]]);
    ctx.strokeRect(southwest[0],northeast[1],northeast[0]-southwest[0],southwest[1]-northeast[1]);
  }
  ctx.restore();
}
function drawWorkflowOverlay(){
  if(!view||!flow)return;drawWorkspace(draftWorkspace||flow.workspace?.bbox);drawGridThemes();drawStandardGrid();
  for(const route of flow.scenario_routes||[])drawLine(route.path,'#7b8791',2,[7,5]);
  for(const route of flow.operational_routes||[])if(route.status==='passed')drawLine(route.path,'#0873cb',4);
  const colors={C:'#1574d4',N:'#c58a13',S:'#139b86'},toggles={C:'cLayer',N:'nLayer',S:'sLayer'};
  for(const [subsystem,layer] of Object.entries(flow.coverage?.layers||{})){
    if(!$(toggles[subsystem])?.checked)continue;
    for(const station of layer.stations||[]){
      const [x,y]=screenPoint(station.coordinate),radius=Math.max(5,station.radius_m/view.res);
      ctx.save();ctx.strokeStyle=colors[subsystem];ctx.fillStyle=colors[subsystem]+'20';ctx.lineWidth=1;ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fill();ctx.stroke();
      ctx.fillStyle=station.role==='gap'?'#fff':colors[subsystem];ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.restore();
    }
  }
  for(const node of flow.nodes||[]){
    const [x,y]=screenPoint(node.coordinate);ctx.save();ctx.fillStyle='#fff';ctx.strokeStyle='#d65432';ctx.lineWidth=3;ctx.beginPath();ctx.arc(x,y,7,0,Math.PI*2);ctx.fill();ctx.stroke();
    ctx.fillStyle='#233f56';ctx.font='600 11px Segoe UI';ctx.fillText(node.node_id,x+10,y-8);ctx.restore();
  }
}
function queue(){paint();clearTimeout(timer);serial++;renderController?.abort();timer=setTimeout(()=>{onlineTiles.update(view,...size(),$('online').checked);renderMap();},160);}
async function renderMap(){
  if(!view||!state?.bounds)return;const started=performance.now(),request=serial,box=mapBounds(),[w,h]=size(),factor=Math.min(1,1600/w,1000/h);
 const q=new URLSearchParams({
  bbox:box.join(','),
  w:Math.round(w*factor),
  h:Math.round(h*factor),

  pop:$('pop').checked?'1':'0',
  air:$('air').checked?'1':'0',

  terrain:$('terrain').checked?'1':'0',

  opacity:$('opacity').value/100,
  terrainOpacity:$('terrainOpacity').value/100,

  rev:state.revision,
  client,
  seq:request
});
  renderController=new AbortController();$('loading').hidden=false;$('loading').textContent='更新本地图层…';
  try{
    const response=await fetch('/api/render?'+q,{signal:renderController.signal});if(!response.ok)throw Error((await response.json()).error);
    const next=await createImageBitmap(await response.blob());if(request!==serial){next.close();return;}bitmap?.close();bitmap=next;imageView=box;paint();showError('');$('viewStatus').textContent='本地图层 '+(performance.now()-started).toFixed(0)+' ms';
  }catch(exc){if(request===serial&&exc.name!=='AbortError')showError(exc.message);}finally{if(request===serial)$('loading').hidden=true;}
}
function zoom(factor,x,y){if(!view)return;const [w,h]=size();x??=w/2;y??=h/2;const before=view.res;view.res=Math.max(.5,Math.min(200000,view.res*factor));view.x+=(x-w/2)*(before-view.res);view.y-=(y-h/2)*(before-view.res);queue();}

map.addEventListener('wheel',event=>{if(event.target!==canvas)return;event.preventDefault();const rect=map.getBoundingClientRect();zoom(event.deltaY>0?1.25:.8,event.clientX-rect.left,event.clientY-rect.top);},{passive:false});
canvas.addEventListener('pointerdown',event=>{
  if(!view)return;
  if(interactionMode==='workspace'){drawStart=eventLonLat(event);draftWorkspace=[drawStart[0],drawStart[1],drawStart[0],drawStart[1]];canvas.setPointerCapture(event.pointerId);return;}
  if(interactionMode==='node')return;
  drag={x:event.clientX,y:event.clientY,cx:view.x,cy:view.y};canvas.setPointerCapture(event.pointerId);map.classList.add('dragging');clearTimeout(timer);serial++;
});
canvas.addEventListener('pointermove',event=>{
  if(!view)return;
  if(drawStart){const now=eventLonLat(event);draftWorkspace=[Math.min(drawStart[0],now[0]),Math.min(drawStart[1],now[1]),Math.max(drawStart[0],now[0]),Math.max(drawStart[1],now[1])];paint();return;}
  if(drag){view.x=drag.cx-(event.clientX-drag.x)*view.res;view.y=drag.cy+(event.clientY-drag.y)*view.res;paint();}
  const point=eventLonLat(event);$('position').textContent=point[0].toFixed(5)+'° E / '+point[1].toFixed(5)+'° N · WGS84';
});
canvas.addEventListener('pointerup',async event=>{
  if(drawStart){drawStart=null;paint();renderWorkflow();return;}
  if(interactionMode==='node'){try{await mutate('node',{coordinate:eventLonLat(event)});}catch(exc){panelError(exc.message);}return;}
  if(drag){drag=null;map.classList.remove('dragging');queue();}
});
canvas.addEventListener('pointercancel',()=>{drawStart=null;drag=null;map.classList.remove('dragging');});
canvas.addEventListener('dblclick',event=>{if(interactionMode!=='pan')return;const rect=map.getBoundingClientRect();zoom(.5,event.clientX-rect.left,event.clientY-rect.top);});
function populationDisplayLabel(result){
  return result?.unit_status==='verified_from_raster_metadata'&&result.value_unit
    ? '人口源值（'+result.value_unit+'）'
    : '人口源值（单位未核实）';
}
function updateGridNotice(){
  const notice=$('gridNotice');if(!notice)return;
  const hasGrid=flow?.grid?.status==='passed'&&gridRenderCache.cells.length>0;
  notice.hidden=!gridDisplay.outline||hasGrid;
  notice.textContent='请先在第02步保存工作区以生成标准网格';
}
function updateGridThemeLegend(){
  const kind={population:'population',terrain:'terrain',traffic_exposure:'traffic',conflict_exposure:'conflict'}[gridDisplay.theme]||null,riskKind={ground_risk:'ground',airspace_risk:'airspace_constraint',overall_risk:'overall'}[gridDisplay.theme]||null,legend=$('gridThemeLegend');
  if(!legend)return;
  legend.hidden=!kind&&!riskKind;
  if(!kind&&!riskKind)return;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{}),breaks=kind?(kind==='population'?gridRenderCache.populationBreaks:kind==='terrain'?gridRenderCache.terrainBreaks:riskBreaks):riskBreaks;
  const populationVerified=result.unit_status==='verified_from_raster_metadata'&&result.value_unit;
  const riskTitles={ground:'Ground Risk',airspace_constraint:'Airspace Constraint Risk',overall:'Overall Risk'};
  const kindTitles={terrain:'平均高程',traffic:'Traffic Exposure',conflict:'Conflict Exposure'};
  const palette=kind?(kind==='population'?populationPalette:kind==='terrain'?terrainPalette:riskPalette):riskPalette,title=kind?(kind==='population'?(populationVerified?'人口源值':populationDisplayLabel(result)):kindTitles[kind]):riskTitles[riskKind];
  $('gridThemeLegendTitle').textContent=title;
  $('gridThemeLegendUnit').textContent=riskKind||kind==='traffic'||kind==='conflict'?'0–1':kind==='terrain'?(result.elevation_unit||'m'):result.unit_status==='verified_from_raster_metadata'?(result.value_unit||''):'';
  $('gridThemeGradient').style.background='linear-gradient(to right,'+palette.join(',')+')';
  const ticks=$('gridThemeTicks');ticks.replaceChildren();
  const shown=breaks.length?[breaks[0],breaks[Math.floor((breaks.length-1)/2)],breaks[breaks.length-1]]:[];
  for(const value of shown){const span=document.createElement('span');span.textContent=GridTheme.formatNumber(value);ticks.append(span);}
  if(!shown.length){const span=document.createElement('span');span.textContent='无有效值';ticks.append(span);}
  const path=result.source?.path||'',source=path.split(/[\\/]/).pop()||'未记录';
  $('gridThemeLegendNote').textContent=riskKind
    ? '相对风险指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+' · 完整度 '+GridTheme.formatNumber((result.data_completeness||0)*100)+'%'
    : kind==='traffic'||kind==='conflict'
      ? '相对暴露指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+' · '+statusText(result.status||'not_calculated')
    : kind==='population'
      ? '仅表达源值相对大小 · '+(result.unit_status||'unverified')+' · '+statusText(result.status||'not_calculated')
      : '均值分级 · '+(result.unit_status||'单位来源未知')+' · '+source+' · '+statusText(result.status||'not_calculated');
}
function formatGridDetails(item){
  const cell=item.cell,populationResult=flow?.grid_attributes?.population||{},terrainResult=flow?.grid_attributes?.terrain||{};
  const population=item.population||{},terrain=item.terrain||{},airspace=item.airspace||{},traffic=item.traffic||{},conflict=item.conflict||{},populationSamples=population.valid_sample_count||0,terrainSamples=terrain.valid_sample_count||0;
  const populationValues=populationSamples
    ? 'mean '+GridTheme.formatNumber(population.value_mean)+' · sum '+GridTheme.formatNumber(population.value_sum)
    : '无数据';
  const elevationUnit=terrainResult.elevation_unit||'m';
  const terrainPath=terrainResult.source?.path||'',terrainSource=terrainPath.split(/[\\/]/).pop()||'未记录';
  const terrainValues=terrainSamples
    ? 'mean '+GridTheme.formatNumber(terrain.mean_elevation)+' / min '+GridTheme.formatNumber(terrain.min_elevation)+' / max '+GridTheme.formatNumber(terrain.max_elevation)+' '+elevationUnit
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
  const trafficSummary='Traffic Exposure：'+statusText(traffic.status||'not_calculated')+' · flights '+(traffic.flight_count||0)+' · flight_seconds '+GridTheme.formatNumber(traffic.flight_seconds)+' · density '+GridTheme.formatNumber(traffic.traffic_density_raw)+' · normalized '+GridTheme.formatNumber(traffic.traffic_density_norm);
  const conflictSummary='Conflict Exposure：'+statusText(conflict.status||'not_calculated')+' · count '+(conflict.conflict_count||0)+' · rate '+GridTheme.formatNumber(conflict.conflict_rate)+' · normalized '+GridTheme.formatNumber(conflict.conflict_rate_norm);
  const risk=item.risk||{},ground=risk.ground||{},operationalAir=risk.air||{},airspaceRisk=risk.airspace_constraint||{},overall=risk.overall||{},riskResult=flow?.grid_risk||{};
  const p=ground.contributors?.population||{},t=ground.contributors?.terrain||{};
  const riskSummary='Ground Risk：'+riskValue(ground)+'\n'+
    '  P：'+factorValue(p)+'\n'+
    '  T：'+factorValue(t)+(t.raw?.relief===undefined?'':' · relief '+GridTheme.formatNumber(t.raw.relief))+'\n'+
    'Operational Air Risk：'+riskValue(operationalAir)+'\n'+
    'Airspace Constraint Risk：'+riskValue(airspaceRisk)+'\n'+
    'Overall Risk：'+riskValue(overall)+' · 完整度 '+GridTheme.formatNumber((overall.data_completeness||0)*100)+'%\n'+
    '风险语义：'+(overall.semantics||risk.semantics||'relative_index')+' · '+(riskResult.algorithm_id||'未计算')+'@'+(riskResult.algorithm_version||'-');
  return cell.grid_id+' · L'+cell.level+'\n'+
    populationDisplayLabel(populationResult)+'：样本 '+populationSamples+' · '+populationValues+' · '+(populationResult.unit_status||'unverified')+'\n'+
    'DEM：样本 '+terrainSamples+' · '+terrainValues+' · '+(terrainResult.unit_status||'单位来源未知')+' · '+terrainSource+'\n'+
    airspaceSummary+'\n'+trafficSummary+'\n'+conflictSummary+'\n'+riskSummary;
}
function riskValue(component){return component?.status==='passed'&&Number.isFinite(component.score)?GridTheme.formatNumber(component.score)+' / '+(component.level||'未分级'):'无数据（'+statusText(component?.status||'not_calculated')+'）';}
function factorValue(factor){return factor?.status==='passed'?'归一化 '+GridTheme.formatNumber(factor.normalized)+' · contribution '+GridTheme.formatNumber(factor.contribution):statusText(factor?.status||'not_available');}
canvas.addEventListener('click',event=>{
  const info=$('gridInfo');
  if(interactionMode!=='pan'||!gridRenderCache.cells.length){info.hidden=true;return;}
  const [lon,lat]=eventLonLat(event);
  const item=findGridCell(lon,lat);
  if(!item){info.hidden=true;return;}
  const rect=map.getBoundingClientRect();
  info.textContent=formatGridDetails(item);
  info.style.left=Math.max(8,Math.min(event.clientX-rect.left+12,rect.width-440))+'px';
  info.style.top=Math.max(8,event.clientY-rect.top-38)+'px';
  info.hidden=false;
});
map.addEventListener('keydown',event=>{if(event.key==='+'||event.key==='=')zoom(.5);if(event.key==='-')zoom(2);});
$('zoomIn').onclick=()=>zoom(.5);$('zoomOut').onclick=()=>zoom(2);$('fit').onclick=()=>fit(state?.bounds);
for(const id of ['air','pop','terrain'])$(id).onchange=queue;

function updateRasterLegends(){
  const populationLegend=$('populationRasterLegend');
  const terrainLegend=$('terrainRasterLegend');

  if(populationLegend){
    populationLegend.hidden=!$('pop').checked;
  }

  if(terrainLegend){
    terrainLegend.hidden=!$('terrain').checked;
  }
}

$('pop').addEventListener('change',updateRasterLegends);
$('terrain').addEventListener('change',updateRasterLegends);

updateRasterLegends();

$('opacity').oninput=()=>{
  $('opacityValue').textContent=$('opacity').value+'%';
  queue();
};

$('terrainOpacity').oninput=()=>{
  $('terrainOpacityValue').textContent=
    $('terrainOpacity').value+'%';
  queue();
};
$('online').onchange=()=>{onlineTiles.update(view,...size(),$('online').checked);paint();};
for(const id of ['gridLayer','cLayer','nLayer','sLayer'])$(id).onchange=()=>{
  if(id==='gridLayer')gridDisplay.outline=$('gridLayer').checked;
  if(id==='gridLayer'&&$('gridOutlineToggle'))$('gridOutlineToggle').checked=gridDisplay.outline;
  updateGridNotice();
  paint();
};
new ResizeObserver(()=>{if(view)queue();else paint();}).observe(map);

function statusText(status){return {not_calculated:'未计算',missing_data:'缺少数据',not_available:'不可用',unknown_category:'未知类别',pending_confirmation:'待确认',passed:'通过',failed:'失败',not_applicable:'不适用',stale:'已失效',ready:'正常',warning:'警告',error:'错误',no_coverage:'无覆盖',partial_intersection:'部分相交',full_coverage:'完全覆盖'}[status]||status;}
function statusBadge(status){return '<span class="flow-badge flow-'+status+'">'+statusText(status)+'</span>';}
function escapeHtml(value){const div=document.createElement('div');div.textContent=String(value??'');return div.innerHTML;}
function setStep(step){
  currentStep=Number(step);interactionMode='pan';draftWorkspace=null;
  document.querySelectorAll('[data-step]').forEach(button=>button.classList.toggle('active',Number(button.dataset.step)===currentStep));
  $('cnsLayers').hidden=currentStep<5;renderWorkflow();paint();
}
document.querySelectorAll('[data-step]').forEach(button=>button.onclick=()=>setStep(button.dataset.step));
function actionButton(id,handler){const button=$(id);if(button)button.onclick=async()=>{try{button.disabled=true;await handler();}catch(exc){panelError(exc.message);}finally{if(document.body.contains(button))button.disabled=false;}};}
function shell(number,title,text,body){return '<div class="section-label">'+number+' / 六步业务流程</div><h2>'+title+'</h2><p>'+text+'</p>'+body+'<div id="panelError" class="inline-error"></div>';}
function renderWorkflow(){
  if(!flow)return;const panel=$('workflowPanel');
  $('workflowStatus').textContent='项目：'+flow.project.name+' · 第 '+currentStep+' 步';

  const storage=state?.project_storage||{};

  $('projectRestore').textContent=storage.automatic
    ? (flow.last_saved_at
        ? '自动恢复项目已保存 · 建议另存到项目文件夹'
        : '当前使用自动恢复项目')
    : ('项目保存位置：'+(storage.directory||'未选择'));

  const renderers=[
    renderStep1,
    renderStep2,
    renderStep3,
    renderStep4,
    renderStep5,
    renderStep6
  ];

  panel.innerHTML=renderers[currentStep-1]();
  bindStep();
}

function renderStep1(){
  const storageDir=state?.project_storage?.directory||'';

  const body=
    '<label>项目名称</label>'+
    '<input class="panel-input" id="projectName" value="'+escapeHtml(flow.project.name)+'">'+

    '<label>项目数据存储位置</label>'+
    '<div class="panel-file-input">'+
      '<input class="panel-input" id="projectPath" value="'+escapeHtml(storageDir)+'" placeholder="请选择项目文件夹">'+
      '<button class="secondary" id="browseProject">选择…</button>'+
    '</div>'+

    '<div class="button-row project-buttons">'+
      '<button class="secondary" id="openProject">打开项目</button>'+
      '<button class="primary" id="saveProject">保存项目</button>'+
    '</div>'+

    '<div class="project-path-note">'+
      '保存后将在该目录生成 project_state.json 和 data_sources.json；之后的自动保存将写入该项目。'+
    '</div>'+

    '<label>地名搜索定位</label>'+
    '<input class="panel-input" id="placeSearch" type="search" placeholder="舟山市、朱家尖、普陀山">'+
    '<button class="secondary full" id="searchPlace">搜索定位</button>'+
    '<div id="placeResults" class="place-results" hidden></div>'+

    '<div class="flow-summary">'+
      statusBadge(state.data_health.status)+' '+
      state.data_health.label+'<br>'+
      state.layers.length+' 个本地图层 · 本地数据可独立工作'+
    '</div>'+

    '<button class="primary full" id="nextStep">下一步：工作区</button>';

  return shell(
    '01',
    '项目与数据',
    '设置项目名称和数据存储位置，检查数据后进入工作区。',
    body
  );
}

function renderStep2(){
  const workspace=flow.workspace,health=workspace?.health,grid=flow.grid,attributes=flow.grid_attributes||{},risk=flow.grid_risk||{};
  let summary='<div class="empty-note">尚未保存工作区。点击“框选工作区”后在地图拖出矩形。</div>';
  if(workspace){
    summary='<div class="metric-grid"><b>'+workspace.area_km2.toLocaleString()+' km²<small>工作区面积</small></b>'+
      '<b>'+statusText(health.population.status)+'<small>人口数据</small></b>'+
      '<b>'+statusText(health.airspace.status)+'<small>空域数据</small></b>'+
      '<b>'+health.loaded_layer_count+'<small>已加载图层</small></b></div>'+
      '<div class="missing-list">'+
  'DEM：'+statusText(health.terrain?.status||'missing_data')+
  ' · 建筑：missing_data'+
  ' · 财产：missing_data'+
'</div>'+
      '<div class="flow-summary">'+
  escapeHtml(health.terrain?.message||'DEM 状态未知')+'<br>'+(
    grid?.status==='passed'
      ? 'MH/T 4063.1 标准网格：L'+grid.level+' · '+grid.count+' 格'+(grid.coarsened?'（已按数量限制降级）':'')
      : '标准网格：未生成'
  )+'<br>'+
  '人口映射：'+statusText(attributes.population?.status||'not_calculated')+' · 已处理 '+(attributes.population?.count||0)+' 格（有效 '+(attributes.population?.covered_count||0)+' 格）<br>'+
  'DEM 映射：'+statusText(attributes.terrain?.status||'not_calculated')+' · 已处理 '+(attributes.terrain?.count||0)+' 格（有效 '+(attributes.terrain?.covered_count||0)+' 格）<br>'+
  '空域映射：'+statusText(attributes.airspace?.status||'not_calculated')+' · 已处理 '+(attributes.airspace?.count||0)+' 格（命中 '+(attributes.airspace?.hit_count||0)+' 格）<br>'+
  '交通暴露：'+statusText(attributes.traffic?.status||'not_calculated')+' · 已处理 '+(attributes.traffic?.count||0)+' 格（命中 '+(attributes.traffic?.covered_count||0)+' 格）<br>'+
  '冲突暴露：'+statusText(attributes.conflict?.status||'not_calculated')+' · 已处理 '+(attributes.conflict?.count||0)+' 格（命中 '+(attributes.conflict?.covered_count||0)+' 格）<br>'+
  '相对风险：'+statusText(risk.status||'not_calculated')+' · 完整度 '+GridTheme.formatNumber((risk.data_completeness||0)*100)+'%'+
'</div>';
  }
  const draft=draftWorkspace?'<div class="flow-summary">待保存：'+draftWorkspace.map(value=>value.toFixed(5)).join(', ')+'</div>':'';
  const themes='<div class="grid-theme-controls"><b>网格显示</b><label class="grid-outline"><input type="checkbox" id="gridOutlineToggle" '+(gridDisplay.outline?'checked':'')+'> 标准网格</label>'+
    '<span>专题模式</span><label><input type="radio" name="gridThemeMode" id="gridThemeNone" value="none" '+(gridDisplay.theme==='none'?'checked':'')+'> 无</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridPopulationTheme" value="population" '+(gridDisplay.theme==='population'?'checked':'')+'> 人口</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridTerrainTheme" value="terrain" '+(gridDisplay.theme==='terrain'?'checked':'')+'> DEM</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridTrafficTheme" value="traffic_exposure" '+(gridDisplay.theme==='traffic_exposure'?'checked':'')+'> Traffic Exposure</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridConflictTheme" value="conflict_exposure" '+(gridDisplay.theme==='conflict_exposure'?'checked':'')+'> Conflict Exposure</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridGroundRiskTheme" value="ground_risk" '+(gridDisplay.theme==='ground_risk'?'checked':'')+'> Ground Risk</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridAirRiskTheme" value="airspace_risk" '+(gridDisplay.theme==='airspace_risk'?'checked':'')+'> 空域约束风险</label>'+
    '<label><input type="radio" name="gridThemeMode" id="gridOverallRiskTheme" value="overall_risk" '+(gridDisplay.theme==='overall_risk'?'checked':'')+'> Overall Risk</label>'+
    '<small>'+populationDisplayLabel(attributes.population)+'</small></div>';
  const body='<div class="button-row"><button class="primary" id="drawWorkspace">框选工作区</button><button class="secondary" id="clearWorkspace">清除</button></div>'+
    draft+themes+summary+'<button class="primary full" id="saveWorkspace" '+(!draftWorkspace?'disabled':'')+'>保存工作区范围</button>'+
    '<button class="secondary full" id="nextStep" '+(!flow.steps['2']?'disabled':'')+'>下一步：航路设计</button>';
  return shell('02','工作区与环境','框选、重画并保存分析范围。',body);
}

function renderStep3(){
  const nodes=(flow.nodes||[]).map(node=>'<div class="list-row"><span><b>'+node.node_id+'</b> '+escapeHtml(node.name)+'<small>'+node.coordinate.map(value=>value.toFixed(5)).join(', ')+'</small></span><button data-delete-node="'+node.node_id+'">×</button></div>').join('');
  const routes=(flow.scenario_routes||[]).map(route=>{
    const result=(flow.operational_routes||[]).find(item=>item.route_id===route.route_id);
    return '<div class="list-row route-row"><span><b>'+route.route_id+'</b> '+route.direction+' '+statusBadge(result?.status||'not_calculated')+'</span><button data-delete-route="'+route.route_id+'">×</button></div>';
  }).join('');
  const body='<button class="'+(interactionMode==='node'?'primary':'secondary')+' full" id="addNodeMode">地图点击增加起降点</button>'+
    '<div class="scroll-list">'+(nodes||'<div class="empty-note">至少添加两个点</div>')+'</div>'+
    '<label>生成方向</label><select id="routeDirection"><option value="both">双向（独立生成两个 route_id）</option><option value="ab">A→B</option><option value="ba">B→A</option></select>'+
    '<div class="button-row"><button class="secondary" id="scenarioRoutes">生成场景航路</button><button class="primary" id="operationalRoutes">生成运行航路</button></div>'+
    '<div class="scroll-list route-list">'+(routes||'<div class="empty-note">尚无航路</div>')+'</div>'+
    '<div class="flow-summary">已退役编号：'+(flow.retired_route_ids.join(', ')||'无')+'<br>环境风险：'+statusText(flow.risks.environment.status)+'</div>'+
    '<button class="primary full" id="nextStep" '+(!flow.steps['3']?'disabled':'')+'>下一步：运行规则</button>';
  return shell('03','航路设计','地图点击增加起降点；场景与运行航路分别保存。',body);
}

function renderStep4(){
  const aircraft=flow.aircraft||{},rules=flow.rules||{},routes=flow.scenario_routes||[];
  const value=(object,key,fallback)=>object[key]??fallback;
  const routeOptions='<option value="">不指定</option>'+routes.map(route=>'<option value="'+route.route_id+'" '+(aircraft.route_id===route.route_id?'selected':'')+'>'+route.route_id+' '+route.direction+'</option>').join('');
  const body='<div class="demo-note">飞行器默认项：'+escapeHtml(flow.aircraft_source)+'</div>'+
    '<div class="form-grid">'+
    '<label>厂家<input id="manufacturer" value="'+escapeHtml(value(aircraft,'manufacturer','工程测试厂家'))+'"></label>'+
    '<label>型号<input id="model" value="'+escapeHtml(value(aircraft,'model','Demo-A1'))+'"></label>'+
    '<label>巡航速度 m/s<input type="number" id="cruise" value="'+value(aircraft,'cruise_speed_mps',25)+'"></label>'+
    '<label>最大速度 m/s<input type="number" id="maximum" value="'+value(aircraft,'max_speed_mps',40)+'"></label>'+
    '<label>MTBF h<input type="number" id="mtbf" value="'+value(aircraft,'mtbf_h',10000)+'"></label>'+
    '<label>绑定航路<select id="aircraftRoute">'+routeOptions+'</select></label>'+
    '<label>A→B 高度 m<input type="number" id="heightAB" value="'+value(rules,'height_ab_m',120)+'"></label>'+
    '<label>B→A 高度 m<input type="number" id="heightBA" value="'+value(rules,'height_ba_m',150)+'"></label>'+
    '<label>高度模式<select id="heightMode"><option value="different">双向不同高度</option><option value="same">同高度层</option></select></label>'+
    '<label>水平间隔 m<input type="number" id="separation" value="'+value(rules,'horizontal_separation_m',100)+'"></label>'+
    '<label>感知→平台 ms<input type="number" id="delaySensor" value="'+value(rules,'delay_sensor_to_platform_ms',500)+'"></label>'+
    '<label>平台→航空器 ms<input type="number" id="delayCommand" value="'+value(rules,'delay_platform_to_aircraft_ms',500)+'"></label></div>'+
    '<label>方向规则<select id="directionRule"><option>按航向分层</option><option>同一航路仅一架</option><option>同一方向仅一架</option></select></label>'+
    '<button class="primary full" id="saveRules">保存并校验规则</button>'+
    (flow.aircraft?'<div class="flow-summary">λ='+(flow.aircraft.lambda_per_hour*1000000).toFixed(3)+'×10⁻⁶/h · 总时延 '+rules.total_delay_ms+' ms · 反应距离 '+rules.reaction_distance_m+' m<br>'+statusBadge(rules.status)+' '+escapeHtml(rules.message)+'</div>':'')+
    '<button class="secondary full" id="nextStep" '+(!flow.steps['4']?'disabled':'')+'>下一步：设备与布站</button>';
  return shell('04','运行规则','飞行器、方向高度和两段时延进入同一规则校验。',body);
}

function renderStep5(){
  const devices=(flow.devices||[]).map((device,index)=>{
    const label=device.model||device.name||device.device_id;
    return '<div class="device-row"><b>'+device.subsystem+' · '+escapeHtml(label)+'</b>'+
      '<label>R(m)<input type="number" data-device-radius="'+index+'" value="'+device.radius_m+'"></label>'+
      '<label>MTBF(h)<input type="number" data-device-mtbf="'+index+'" value="'+(device.mtbf_h||device.mtbf)+'"></label><span>'+device.role+'</span></div>';
  }).join('');
  let result='<div class="empty-note">尚未运行 CoveragePlannerV1</div>';
  if(flow.coverage){
    result=Object.entries(flow.coverage.layers||{}).map(([key,layer])=>{
      const stats=layer.statistics;
      return '<div class="coverage-card"><b>'+key+' '+statusBadge(layer.status)+'</b><span>站点 '+stats.stations+' · 主站 '+stats.primary+' · 补盲 '+stats.gap+' · 共址 '+stats.colocated+'</span><span>平均重数 '+stats.average_multiplicity+' · 未覆盖 '+stats.uncovered_samples+'</span></div>';
    }).join('');
  }
  const params=flow.defaults.engineering_parameters;
  const body='<div class="demo-note">'+escapeHtml(flow.device_source)+' · demo/default</div>'+
    '<div class="parameter-note">主站间距 '+params.primary_spacing_factor.value+'R · 共址半径 '+params.co_location_search_radius_m.value+'m<br>'+escapeHtml(params.primary_spacing_factor.source)+'</div>'+
    '<div class="device-list">'+devices+'</div><div class="button-row"><button class="secondary" id="saveDevices">保存设备参数</button><button class="primary" id="planCoverage">运行布站</button></div>'+
    '<div class="coverage-results">'+result+'</div>'+
    '<div class="flow-summary">生命风险：'+statusText(flow.risks.life.status)+' · 财产风险：'+statusText(flow.risks.property.status)+'<br>overall_pass 不会因未知风险变为通过</div>'+
    '<button class="primary full" id="nextStep" '+(!flow.steps['5']?'disabled':'')+'>下一步：确认与导出</button>';
  return shell('05','设备与布站','C/N/S 独立规划、图层和统计；本轮使用工程测试数据。',body);
}

function renderStep6(){
  const review=flow.review,workspace=flow.workspace,coverage=flow.coverage;
  const labels={environment:'环境/GRC',technical:'技术/MTBF',life:'生命',property:'财产'};
  const risks=Object.entries(review.risks).map(([key,value])=>'<div class="review-row"><span>'+labels[key]+'</span>'+statusBadge(value.status)+'</div>').join('');
  const resultLabels={workspace:'工作区',grid:'标准网格',environment_risk:'环境风险',routes:'运行航路',coverage:'C/N/S 布站',technical_risk:'技术风险',report:'报告'};
  const dependencies=Object.entries(flow.result_statuses||{}).map(([key,value])=>'<div class="review-row"><span>'+resultLabels[key]+'</span>'+statusBadge(value)+'</div>').join('');
  const layers=Object.entries(coverage?.layers||{}).map(([key,value])=>key+'：'+value.statistics.stations+' 站 / '+statusText(value.status)).join('<br>');
  const body='<div class="review-block"><b>'+escapeHtml(flow.project.name)+'</b>'+
    '<span>数据源：'+statusText(state.data_health.status)+'</span><span>工作区：'+(workspace?workspace.area_km2+' km²':'未定义')+'</span>'+
    '<span>运行航路：'+flow.operational_routes.length+'（'+flow.operational_routes.map(item=>item.route_id).join(', ')+'）</span>'+
    '<span>飞行器：'+(flow.aircraft?escapeHtml(flow.aircraft.manufacturer+' '+flow.aircraft.model):'未设置')+'</span>'+
    '<span>规则：'+statusText(flow.rules?.status||'not_calculated')+'</span><span>'+layers+'</span></div>'+
    '<div class="risk-review">'+risks+'</div><div class="risk-review">'+dependencies+'</div><div class="overall-card">overall_status：'+statusBadge(review.overall_status)+'<br>overall_pass：'+String(review.overall_pass)+'</div>'+
    '<div class="button-row export-row"><a class="secondary button-link" download="project.json" href="/api/export/project">project.json</a><a class="secondary button-link" download="routes.geojson" href="/api/export/routes">routes.geojson</a><a class="secondary button-link" download="sites.geojson" href="/api/export/sites">sites.geojson</a></div>'+
    '<button class="primary full" id="saveAll">保存当前项目</button>';
  return shell('06','确认与导出','审查当前有效结果并导出可重新读取的数据。',body);
}

function bindStep(){
  if(currentStep===1){

  // 选择项目保存文件夹
  $('browseProject').onclick=()=>{
    openBrowser('project',$('projectPath').value);
  };

  // 保存项目
  $('saveProject').onclick=async()=>{
    const button=$('saveProject');
    const projectDir=$('projectPath').value.trim();
    const name=$('projectName').value.trim();

    if(!projectDir){
      return panelError('请先选择项目数据存储位置');
    }

    try{
      button.disabled=true;
      panelError('');

      // 先保存项目名称
      await api('/api/workflow/project',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name})
      });

      // 再保存到用户选择的项目目录
      const data=await api('/api/project/save-as',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          project_dir:projectDir
        })
      });

      update(data);

    }catch(exc){
      panelError('保存项目失败：'+exc.message);
    }finally{
      if(document.body.contains(button)){
        button.disabled=false;
      }
    }
  };

  // 打开指定项目
  $('openProject').onclick=async()=>{
    const button=$('openProject');
    const projectDir=$('projectPath').value.trim();

    if(!projectDir){
      return panelError('请先选择项目文件夹');
    }

    try{
      button.disabled=true;
      panelError('');

      const data=await api('/api/project/open',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          project_dir:projectDir
        })
      });

      update(data);

      bitmap?.close();
      bitmap=null;

      if(data.workflow?.workspace?.bbox)fitLonLatBbox(data.workflow.workspace.bbox);
      else if(data.bounds)fit(data.bounds);

    }catch(exc){
      panelError('打开项目失败：'+exc.message);
    }finally{
      if(document.body.contains(button)){
        button.disabled=false;
      }
    }
  };

  $('searchPlace').onclick=searchPlace;

  $('placeSearch').onkeydown=event=>{
    if(event.key==='Enter'){
      event.preventDefault();
      searchPlace();
    }
  };

  $('nextStep').onclick=()=>setStep(2);
}
  if(currentStep===2){
    $('gridOutlineToggle').onchange=event=>{gridDisplay.outline=event.target.checked;$('gridLayer').checked=gridDisplay.outline;updateGridNotice();paint();};
    document.querySelectorAll('[name="gridThemeMode"]').forEach(input=>input.onchange=event=>{if(event.target.checked){gridDisplay.theme=event.target.value;updateGridThemeLegend();paint();}});
    $('drawWorkspace').onclick=()=>{interactionMode='workspace';draftWorkspace=null;panelError('请在地图上按住并拖出矩形工作区');};
    actionButton('clearWorkspace',async()=>{draftWorkspace=null;interactionMode='pan';await mutate('workspace-clear');});
    actionButton('saveWorkspace',async()=>{await mutate('workspace',{bbox:draftWorkspace});interactionMode='pan';draftWorkspace=null;fitLonLatBbox(flow.workspace?.bbox);});
    if($('nextStep'))$('nextStep').onclick=()=>setStep(3);
  }
  if(currentStep===3){
    $('addNodeMode').onclick=()=>{interactionMode=interactionMode==='node'?'pan':'node';renderWorkflow();};
    actionButton('scenarioRoutes',()=>mutate('scenario',{direction:$('routeDirection').value}));
    actionButton('operationalRoutes',()=>mutate('operational'));
    document.querySelectorAll('[data-delete-node]').forEach(button=>button.onclick=()=>mutate('node-delete',{node_id:button.dataset.deleteNode}).catch(exc=>panelError(exc.message)));
    document.querySelectorAll('[data-delete-route]').forEach(button=>button.onclick=()=>mutate('route-delete',{route_id:button.dataset.deleteRoute}).catch(exc=>panelError(exc.message)));
    if($('nextStep'))$('nextStep').onclick=()=>setStep(4);
  }
  if(currentStep===4){
    if(flow.rules)$('heightMode').value=flow.rules.height_mode;
    actionButton('saveRules',async()=>{
      await mutate('rules',{
        manufacturer:$('manufacturer').value,model:$('model').value,cruise_speed:$('cruise').value,max_speed:$('maximum').value,
        mtbf:$('mtbf').value,route_id:$('aircraftRoute').value,height_ab:$('heightAB').value,height_ba:$('heightBA').value,
        height_mode:$('heightMode').value,horizontal_separation:$('separation').value,direction_rule:$('directionRule').value,
        delay_sensor:$('delaySensor').value,delay_command:$('delayCommand').value
      });
      if(flow.rules?.status==='passed')await mutate('operational');
    });
    if($('nextStep'))$('nextStep').onclick=()=>setStep(5);
  }
  if(currentStep===5){
    const collectDevices=()=>flow.devices.map((device,index)=>({
      ...device,
      radius_m:Number(document.querySelector('[data-device-radius="'+index+'"]').value),
      mtbf:Number(document.querySelector('[data-device-mtbf="'+index+'"]').value)
    }));
    actionButton('saveDevices',()=>mutate('devices',{devices:collectDevices()}));
    actionButton('planCoverage',async()=>{await mutate('devices',{devices:collectDevices()});await mutate('coverage');});
    if($('nextStep'))$('nextStep').onclick=()=>setStep(6);
  }
  if(currentStep===6){
    actionButton('saveAll',()=>mutate('save'));
  }
}

function getTiandituKey(){
  for(const source of state?.online_sources||[]){
    if(!source.browser_url?.includes('tianditu.gov.cn'))continue;
    try{const key=new URL(source.browser_url).searchParams.get('tk');if(key)return key;}catch(_){}
  }
  return '';
}
async function searchPlace(){
  const keyword=$('placeSearch').value.trim(),key=getTiandituKey();
  if(!keyword)return panelError('请输入地名');
  if(!key)return panelError('未配置天地图地名搜索 Key');
  try{
    const postStr={keyWord:keyword,level:12,mapBound:'73,18,135,54',queryType:7,start:0,count:8};
    const response=await fetch('https://api.tianditu.gov.cn/v2/search?'+new URLSearchParams({postStr:JSON.stringify(postStr),type:'query',tk:key}));
    const data=await response.json(),container=$('placeResults');container.replaceChildren();container.hidden=false;
    for(const poi of data.pois||[]){
      if(!poi.lonlat)continue;const [lon,lat]=poi.lonlat.split(',').map(Number);
      const button=document.createElement('button');button.className='place-result';button.textContent=poi.name+' · '+(poi.address||poi.city||'');
      button.onclick=()=>{const point=lonLatToMercator(lon,lat);view={x:point[0],y:point[1],res:100};queue();container.hidden=true;};container.append(button);
    }
    if(!container.children.length)container.textContent='没有找到可定位结果';
  }catch(exc){panelError('地名搜索失败：'+exc.message);}
}

async function downloadExport(kind){
  try{
    const response=await fetch('/api/export/'+kind,{headers:{'X-CNS-Token':state.token}});
    if(!response.ok)throw Error((await response.json()).error);
    const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download={project:'project.json',routes:'routes.geojson',sites:'sites.geojson'}[kind];link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(exc){panelError(exc.message);}
}

function healthLabel(status){
  return {ready:'正常',warning:'警告',error:'错误',checking:'检查中'}[status]||statusText(status);
}
function renderSourceCenter(data){
  const health=data.data_health||{status:'checking',label:'正在检查数据源',items:[]};
  const connection=$('connection');
  connection.className='status status-'+health.status;
  connection.innerHTML='<i class="dot"></i><span>'+health.label+'</span>';
  connection.title='打开统一数据源设置查看检查明细';
  const summary=$('healthSummary');
  summary.className='health-summary health-'+health.status;
  summary.textContent=health.label+' · '+health.stage+' 文件级检查；工作区覆盖在第 02 步检查';
  const list=$('sourceHealthList');
  list.replaceChildren();
  for(const item of health.items){
    const row=document.createElement('article');
    row.className='source-health-item';
    row.dataset.sourceId=item.id;
    row.innerHTML='<div><strong>'+escapeHtml(item.label)+'</strong><span class="health-badge health-'+item.status+'">'+healthLabel(item.status)+'</span></div><p>'+escapeHtml(item.message)+'</p><small>'+escapeHtml(item.category)+' · '+escapeHtml(item.formats)+(item.required?' · P1 必需':' · 后续/可选')+'</small>';
    list.append(row);
  }
  const parameters=$('parameterList');
  parameters.replaceChildren();
  const names={vertical_clearance_m:'垂直净空裕度',primary_spacing_factor:'主站间距系数',co_location_search_radius_m:'共址搜索半径'};
  for(const [key,value] of Object.entries(data.defaults?.engineering_parameters||{})){
    const row=document.createElement('div');
    row.className='parameter-row';
    row.textContent=(names[key]||key)+'：'+value.value+(key.endsWith('_m')?' m':'')+' · '+value.source;
    parameters.append(row);
  }
}

function update(data){
  state=data;
  flow=data.workflow;
  rebuildGridRenderCache();
  onlineTiles.configure(data.online_sources||[],data.revision);
  $('basemapPath').value=data.paths.basemap;
  $('populationPath').value=data.paths.population;
  $('terrainPath').value=data.paths.terrain||'';
  const population=data.population;
  $('rasterInfo').textContent=population.width?'人口栅格：'+population.width.toLocaleString()+' × '+population.height.toLocaleString()+' · '+population.crs+'\nNoData：'+population.nodata+' · '+population.unit:'尚未加载有效人口数据';
  const terrain=data.terrain||{};

$('terrainInfo').textContent=
  terrain.width
    ? 'GLO-30 地形：'+
      terrain.width.toLocaleString()+
      ' × '+
      terrain.height.toLocaleString()+
      ' · '+
      terrain.crs+
      '\nNoData：'+terrain.nodata+
      '\n像元大小：'+
      (terrain.pixel_size||[]).join(' × ')+
      '\n单位：'+(terrain.unit||'m')
    : '尚未加载有效地形 DEM';
  $('sourceSummary').textContent=data.layers.length+' 个本地图层 · '+data.paths.basemap.split(/[\\/]/).pop();
  renderSourceCenter(data);
  renderWorkflow();
  syncGridApis().then(()=>{renderWorkflow();paint();}).catch(exc=>showError('网格专题同步失败：'+exc.message));
  if(data.error)showError(data.error);
}

function openSettings(){$('settingsError').textContent='';$('settings').showModal();}
$('settingsBtn').onclick=$('connection').onclick=openSettings;
$('closeSettings').onclick=()=>$('settings').close();
function sourcePayload(){
  return {
    basemap:$('basemapPath').value,
    population:$('populationPath').value,
    terrain:$('terrainPath').value
  };
}
actionButton('validateSources',async()=>{
  const data=await api('/api/data-sources/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sourcePayload())});
  renderSourceCenter(data);
  $('settingsMessage').textContent='校验完成；尚未切换当前地图';
});
actionButton('applySources',async()=>{
  const data=await api('/api/data-sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sourcePayload())});
  update(data);bitmap?.close();bitmap=null;fit(data.bounds);
  $('settingsMessage').textContent='已保存并重新验证数据源';
});
$('checkOnline').onclick=async()=>{
  const button=$('checkOnline');

  // 如果 index.html 里没有专门的结果区域，
  // 就由 app.js 自动创建一个
  let result=$('onlineCheckResult');

  if(!result){
    result=document.createElement('div');
    result.id='onlineCheckResult';
    result.className='online-check-result';

    const list=$('sourceHealthList');

    if(list){
      list.parentNode.insertBefore(result,list);
    }
  }

  button.disabled=true;
  button.textContent='检查中…';

  if(result){
    result.className='online-check-result checking';
    result.textContent='正在重新请求在线服务，请稍候…';
  }

  $('settingsMessage').textContent='正在检查在线服务…';

  try{

    const data=
  await onlineTiles.checkSources();

    const lines=(data.results||[]).map(item=>{
      return (item.ok?'✓ ':'✕ ')+
        item.name+
        '：'+
        item.message;
    });

    if(result){
      result.className=
        'online-check-result '+(data.ok?'ok':'bad');

      result.textContent=
        lines.join('\n')||
        data.message||
        '没有可检查的在线服务';
    }

    $('settingsMessage').textContent=
      data.ok
        ? '在线服务重新检查完成：全部正常'
        : '在线服务重新检查完成：存在异常，请查看检查结果';

  }catch(exc){

    if(result){
      result.className='online-check-result bad';
      result.textContent='检查失败：'+exc.message;
    }

    $('settingsMessage').textContent=
      '在线服务检查失败';

  }finally{

    button.disabled=false;
    button.textContent='检查在线服务';

  }
};
function openBrowser(kind,initialPath=''){

  browseKind=kind;

  if(kind==='basemap'){
  $('fileFilter').textContent=
    '文件类型：QGIS 项目（.qgz / .qgs）';
}
else if(kind==='population'){
  $('fileFilter').textContent=
    '文件类型：人口栅格（.tif / .tiff）';
}
else if(kind==='terrain'){
  $('fileFilter').textContent=
    '文件类型：地形 DEM（.tif / .tiff）';
}
else if(kind==='project'){
  $('fileFilter').textContent=
    '请选择项目数据存储文件夹';
}

  $('selectFile').textContent=
    kind==='project'
      ? '选择当前文件夹'
      : '选择此文件';

  $('browser').showModal();

  browse(initialPath);
}
async function browse(path){

  $('browseError').textContent='';
  selectedFile='';
  $('selectFile').disabled=true;

  $('chosen').textContent=
    browseKind==='project'
      ? '请选择项目文件夹'
      : '请选择文件；单击文件后确认';

  try{

    const data=await api(
      '/api/browse?'+
      new URLSearchParams({
        path,
        kind:browseKind
      })
    );

    $('folder').value=data.path;
    browseParent=data.parent;
    $('entries').replaceChildren();

    // 项目模式：当前文件夹本身就可以被选择
    if(browseKind==='project' && data.path){

      selectedFile=data.path;

      $('chosen').textContent=
        '当前文件夹：'+data.path;

      $('selectFile').disabled=false;
    }

    for(const entry of data.entries){

      const button=document.createElement('button');

      button.className='entry';

      button.textContent=
        (entry.directory?'📁  ':'▧  ')+
        entry.name;

      button.onclick=()=>{

        // 文件夹：进入
        if(entry.directory){
          return browse(entry.path);
        }

        // 普通文件：选中
        $('entries')
          .querySelectorAll('button')
          .forEach(item=>{
            item.classList.remove('selected');
          });

        button.classList.add('selected');

        selectedFile=entry.path;

        $('chosen').textContent=entry.path;

        $('selectFile').disabled=false;
      };

      $('entries').append(button);
    }

    if(
      !data.entries.length &&
      browseKind!=='project'
    ){
      $('entries').textContent=
        '此目录没有匹配的文件或子目录';
    }

  }catch(exc){

    $('browseError').textContent=
      '无法浏览：'+exc.message;

  }
}
document.querySelectorAll('[data-browse]').forEach(button=>{
  button.onclick=()=>{

    const kind=button.dataset.browse;

    openBrowser(
      kind,
      $(kind+'Path').value
    );

  };
});
$('closeBrowser').onclick=()=>$('browser').close();
$('drives').onclick=()=>browse('');
$('parent').onclick=()=>browse(browseParent);
$('openFolder').onclick=()=>browse($('folder').value);
$('selectFile').onclick=()=>{

  const target=$(browseKind+'Path');

  if(selectedFile && target){
    target.value=selectedFile;
  }

  $('browser').close();
};

api('/api/state').then(data=>{
  update(data);
  if(data.workflow?.workspace?.bbox)fitLonLatBbox(data.workflow.workspace.bbox);
  else if(data.bounds)fit(data.bounds);
  else{$('loading').hidden=true;openSettings();}
}).catch(exc=>{showError('无法连接本机地图服务：'+exc.message);$('loading').hidden=true;});
