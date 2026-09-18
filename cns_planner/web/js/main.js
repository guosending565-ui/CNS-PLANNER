import {createApiClient} from './api/client.js';
import {createStore} from './state/store.js';
import {eventLonLat as projectEvent,lonLatToMercator,mercatorToLonLat,screenPoint as projectPoint} from './map/projection.js';
import {buildGridOverlayCache,findGridCell as hitGridCell} from './map/grid_overlay.js';
import {bindMapInteraction} from './map/interaction.js';
import {drawGridTheme,drawLine,drawStandardGrid,drawWorkspace} from './map/renderer.js';
import {drawReferenceOverlay,hitReferenceObject as hitReferenceOverlay,referenceLayerDiagnostics} from './map/reference_overlay.js';
import {drawBuildingClearanceOverlay} from './map/building_clearance_overlay.js';
import {drawV3CandidateOverlay,v3OverlayModel} from './map/route_planner_v3_overlay.js';
import {escapeHtml as escapeValue,statusBadge as badgeFor,statusText as labelFor} from './workflow/common.js';
import * as Step01 from './workflow/step01_project.js';
import * as Step02 from './workflow/step02_workspace.js';
import * as Step03 from './workflow/step03_routes.js';
import * as Step04 from './workflow/step04_operation.js';
import * as Step05 from './workflow/step05_cns.js';
import * as Step06 from './workflow/step06_review.js';
import {createSourceCenter} from './sources/source_center.js';
const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d'),map=$('map');
let state=null,flow=null,view=null,bitmap=null,imageView=null,timer,serial=0,draftWorkspace=null;
let currentStep=1,interactionMode='pan',renderController=null;
let selectedReference=null,profileHoverCoordinate=null;
let gridDataSerial=0;
let gridDisplay={outline:true,theme:'none'};
let gridRenderCache={cells:[],byId:new Map(),spatial:null,populationBreaks:[],terrainBreaks:[],buildingCoverageBreaks:[],buildingP95Breaks:[],buildingMaxBreaks:[]};
const populationPalette=['#fff7bc','#fee391','#fec44f','#fe9929','#cc4c02'];
const terrainPalette=['#2c7bb6','#abd9e9','#ffffbf','#fdae61','#d7191c'];
const buildingPalette=['#fff7ec','#fdd49e','#fc8d59','#d7301f','#7f0000'];
const riskPalette=['#2ca25f','#99d8c9','#fee08b','#f46d43','#a50026'],riskBreaks=[0,.2,.4,.6,.8,1];
const client=crypto.randomUUID(),onlineTiles=new OnlineTiles(()=>requestAnimationFrame(paint),text=>$('tileStatus').textContent=text);
const store=createStore({server:null,workflow:null,mapView:null,ui:{step:1,interactionMode:'pan'}});
const api=createApiClient(()=>state?.token);
async function mutate(action,payload={}){
  const data=await api('/api/workflow/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  flow=data;store.set({workflow:flow});rebuildGridRenderCache();
  try{await syncGridApis();}catch(exc){showError('网格专题同步失败：'+exc.message);}
  renderWorkflow();paint();return data;
}
async function resourceAction(path,payload={}){
  const data=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  flow=data;store.set({workflow:flow});rebuildGridRenderCache();renderWorkflow();paint();return data;
}
async function computeAction(path,payload={}){
  return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
}
// V3 candidate paths are large: the workflow snapshot carries summaries only, so the read-only panel pulls the frozen detail on demand.
async function loadRoutePlannerV3Detail(){
  const detail=await api('/api/route-planner-v3-experiments');
  flow={...flow,route_planner_v3_detail:detail};store.set({workflow:flow});renderWorkflow();paint();
  return detail;
}
async function syncGridApis(){
  const request=++gridDataSerial;
  const [grid,attributes]=await Promise.all([api('/api/workspace/grid'),api('/api/workspace/grid/attributes')]);
  if(request!==gridDataSerial)return;
  flow={...flow,grid,grid_attributes:attributes};store.set({workflow:flow});rebuildGridRenderCache();
}
function showError(message){$('error').hidden=!message;$('error').textContent=message||'';}
function panelError(message){const target=$('panelError');if(target)target.textContent=message||'';else showError(message);}
function size(){return [Math.max(1,Math.round(map.clientWidth)),Math.max(1,Math.round(map.clientHeight))];}
function fit(box){if(!box)return;const [w,h]=size();view={x:(box[0]+box[2])/2,y:(box[1]+box[3])/2,res:Math.max((box[2]-box[0])/w,(box[3]-box[1])/h)*1.1};queue();}
function mapBounds(){const [w,h]=size();return [view.x-w*view.res/2,view.y-h*view.res/2,view.x+w*view.res/2,view.y+h*view.res/2];}
function screenPoint(coordinate){const [w,h]=size();return projectPoint(coordinate,view,w,h);}
function eventLonLat(event){const [w,h]=size();return projectEvent(event,map,view,w,h);}
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
function rebuildGridRenderCache(){
  gridRenderCache=buildGridOverlayCache(flow?.grid,flow?.grid_attributes||{},flow?.grid_risk||{},GridTheme);
  if($('gridInfo'))$('gridInfo').hidden=true;
  updateGridNotice();updateGridThemeLegend();
}
function findGridCell(lon,lat){return hitGridCell(gridRenderCache,lon,lat,GridTheme);}
function drawGridThemes(){drawGridTheme({ctx,view,flow,cache:gridRenderCache,display:gridDisplay,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme,palettes:{population:populationPalette,terrain:terrainPalette,buildings:buildingPalette,risk:riskPalette},riskBreaks});}
function drawGridBoundaries(){drawStandardGrid({ctx,view,grid:flow?.grid,display:gridDisplay,enabled:$('gridLayer')?.checked,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme});}
function drawWorkflowOverlay(){
  if(!view||!flow)return;drawWorkspace(ctx,screenPoint,draftWorkspace||flow.workspace?.bbox);drawGridThemes();drawGridBoundaries();
  for(const route of flow.scenario_routes||[])drawLine(ctx,screenPoint,view,route.path,'#7b8791',2,[7,5]);
  for(const route of flow.operational_routes||[])if(route.status==='passed')drawLine(ctx,screenPoint,view,route.path,'#0873cb',4);
  if($('buildingClearanceLayer')?.checked)drawBuildingClearanceOverlay({ctx,screenPoint,drawLine,assessment:flow.building_clearance_assessment});
  if($('v3CandidateLayer')?.checked)drawV3CandidateOverlay({ctx,screenPoint,drawLine,model:v3OverlayModel(flow)});
  const overlay=Step03.referenceOverlayModel(flow,{routes:$('referenceRouteLayer')?.checked,points:$('referenceRoutePointLayer')?.checked,landingSites:false});
  drawReferenceOverlay({ctx,view,screenPoint,drawLine,routes:overlay.referenceRoutes,points:overlay.referencePoints});
  const filters={workspace:flow.workspace,search:currentStep===3?$('referenceSiteSearch')?.value||'':'',region:currentStep===3?$('referenceSiteRegion')?.value||'':'',siteType:currentStep===3?$('referenceSiteType')?.value||'':''};
  if($('referenceLandingLayer')?.checked)for(const site of Step03.filterReferenceSites(flow.reference_landing_sites?.items||[],filters))drawCnsInputPoint(site.coordinate,site.possible_duplicate?'#8b6b2f':'#2b8c82','diamond');
  const gapColors={C:'#d83b35',N:'#c26b16',S:'#9b3eb5'},gapToggles={C:'cLayer',N:'nLayer',S:'sLayer'};
  for(const route of flow.cns_gap_analysis?.status==='stale'?[]:(flow.cns_gap_analysis?.routes||[]))for(const subsystem of route.subsystems||[]){
    if(!$(gapToggles[subsystem.subsystem])?.checked)continue;
    for(const segment of subsystem.uncovered_segments||[])drawLine(ctx,screenPoint,view,segment.path,gapColors[subsystem.subsystem],6,[5,3]);
  }
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
  if(profileHoverCoordinate)drawCnsInputPoint(profileHoverCoordinate,'#111','circle');
  if($('existingCnsLayer')?.checked)for(const facility of flow.existing_cns_facilities?.items||[])drawCnsInputPoint(facility.coordinate,'#7256a1','circle');
  if($('candidateSiteLayer')?.checked)for(const site of flow.candidate_sites?.items||[])drawCnsInputPoint(site.coordinate,site.usable===false?'#8b949e':'#e07a26','diamond');
  if($('candidateSiteLayer')?.checked)for(const action of proposedPlanActions(flow))drawCnsInputPoint(action.coordinate,'#d12f8a','square');
}
function proposedPlanActions(value){
  const p16=value?.cns_corridor_site_plan||{},review=value?.cns_plan_review||{};
  if(value?.confirmed_cns_plan?.application?.status==='applied')return [];
  const variant=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id);
  if(!variant)return p16.selected_actions||[];
  const ids=new Set(variant.selected_action_ids||[]),catalog=[...(p16.candidate_actions||[]),...(p16.selected_actions||[])];
  return catalog.filter((item,index)=>ids.has(item.action_id)&&catalog.findIndex(other=>other.action_id===item.action_id)===index);
}
function drawCnsInputPoint(coordinate,color,shape){
  if(!Array.isArray(coordinate))return;
  const [x,y]=screenPoint(coordinate);ctx.save();ctx.fillStyle=color;ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.beginPath();
  if(shape==='diamond'){ctx.moveTo(x,y-7);ctx.lineTo(x+7,y);ctx.lineTo(x,y+7);ctx.lineTo(x-7,y);ctx.closePath();}else if(shape==='square'){ctx.rect(x-6,y-6,12,12);}else ctx.arc(x,y,6,0,Math.PI*2);
  ctx.fill();ctx.stroke();ctx.restore();
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
bindMapInteraction({
  map,canvas,getView:()=>view,setView:value=>{view=value;},getMode:()=>interactionMode,eventLonLat,zoom,queue,paint,
  onDraft:value=>{draftWorkspace=value;},onDraftComplete:renderWorkflow,
  onNode:async coordinate=>{try{await mutate('node',{coordinate});}catch(exc){panelError(exc.message);}},
  onPosition:point=>{$('position').textContent=point[0].toFixed(5)+'° E / '+point[1].toFixed(5)+'° N · WGS84';},
  onPanStart:()=>{clearTimeout(timer);serial++;}
});
function populationDisplayLabel(result){
  return result?.source_profile?.quantity==='population_count_per_source_pixel'
    ? '目标网格人口数（person）/人口密度（person/km²）'
    : '人口源值（兼容字段，非人数）';
}
function updateGridNotice(){
  const notice=$('gridNotice');if(!notice)return;
  const hasGrid=flow?.grid?.status==='passed'&&gridRenderCache.cells.length>0;
  notice.hidden=!gridDisplay.outline||hasGrid;
  notice.textContent='请先在第02步保存工作区以生成标准网格';
}
function updateGridThemeLegend(){
  const kind={population:'population',terrain:'terrain',traffic_exposure:'traffic',conflict_exposure:'conflict',building_density:'buildings',building_p95:'buildings',building_max:'buildings'}[gridDisplay.theme]||null,riskKind={ground_risk:'ground',overall_risk:'overall'}[gridDisplay.theme]||null,legend=$('gridThemeLegend');
  if(!legend)return;
  legend.hidden=!kind&&!riskKind;
  if(!kind&&!riskKind)return;
  const result=kind?(flow?.grid_attributes?.[kind]||{}):(flow?.grid_risk||{}),buildingBreaks={building_density:gridRenderCache.buildingCoverageBreaks,building_p95:gridRenderCache.buildingP95Breaks,building_max:gridRenderCache.buildingMaxBreaks},breaks=kind?(kind==='population'?gridRenderCache.populationBreaks:kind==='terrain'?gridRenderCache.terrainBreaks:kind==='buildings'?buildingBreaks[gridDisplay.theme]:riskBreaks):riskBreaks;
  const riskTitles={ground:'Ground Risk',overall:'Overall Risk'};
  const kindTitles={terrain:'平均高程',traffic:'Traffic Exposure',conflict:'Conflict Exposure',buildings:{building_density:'建筑密度',building_p95:'P95 建筑高度',building_max:'最大建筑高度'}[gridDisplay.theme]};
  const palette=kind?(kind==='population'?populationPalette:kind==='terrain'?terrainPalette:kind==='buildings'?buildingPalette:riskPalette):riskPalette,title=kind?(kind==='population'?'目标网格人口密度':kindTitles[kind]):riskTitles[riskKind];
  $('gridThemeLegendTitle').textContent=title;
  $('gridThemeLegendUnit').textContent=riskKind||kind==='traffic'||kind==='conflict'?'0–1':kind==='terrain'?(result.elevation_unit||'m'):kind==='buildings'?(gridDisplay.theme==='building_density'?'ratio':'m'):'person/km²';
  $('gridThemeGradient').style.background='linear-gradient(to right,'+palette.join(',')+')';
  const ticks=$('gridThemeTicks');ticks.replaceChildren();
  const shown=breaks.length?[breaks[0],breaks[Math.floor((breaks.length-1)/2)],breaks[breaks.length-1]]:[];
  for(const value of shown){const span=document.createElement('span');span.textContent=GridTheme.formatNumber(value);ticks.append(span);}
  if(!shown.length){const span=document.createElement('span');span.textContent='无有效值';ticks.append(span);}
  const path=result.source?.path||'',source=path.split(/[\\/]/).pop()||'未记录';
  $('gridThemeLegendNote').textContent=riskKind
    ? '相对风险指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+' · 完整度 '+GridTheme.formatNumber((result.data_completeness||0)*100)+'%'
    : kind==='buildings'
      ? 'GBA L8 来源参数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+' · 0 与无数据严格区分 · '+source+' · '+statusText(result.status||'not_calculated')
    : kind==='traffic'||kind==='conflict'
      ? '相对暴露指数 · '+(result.algorithm_id||'未计算')+'@'+(result.algorithm_version||'-')+' · '+statusText(result.status||'not_calculated')
    : kind==='population'
      ? 'WorldPop count 经面积权重守恒映射，再除以实际网格面积 · '+statusText(result.quantity_status||'not_calculated')
      : '均值分级 · '+(result.unit_status||'单位来源未知')+' · '+source+' · '+statusText(result.status||'not_calculated');
}
function formatGridDetails(item){
  const cell=item.cell,populationResult=flow?.grid_attributes?.population||{},terrainResult=flow?.grid_attributes?.terrain||{};
  const population=item.population||{},terrain=item.terrain||{},buildings=item.buildings||{},airspace=item.airspace||{},traffic=item.traffic||{},conflict=item.conflict||{},populationSamples=population.valid_sample_count||0,terrainSamples=terrain.valid_sample_count||0;
  const populationCoverage=Number.isFinite(population.source_coverage_fraction)?GridTheme.formatNumber(population.source_coverage_fraction*100)+'%':'未知';
  const populationValues=Number.isFinite(population.population_count_people)
    ? '人口数 '+GridTheme.formatNumber(population.population_count_people)+' person · 密度 '+GridTheme.formatNumber(population.population_density_people_km2)+' person/km² · coverage '+populationCoverage+' · '+(population.coverage_status||'unknown')
    : populationSamples?'兼容源像元统计 mean '+GridTheme.formatNumber(population.value_mean)+' · sum '+GridTheme.formatNumber(population.value_sum)+'（非人数）':'无数据';
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
  const buildingSummary='建筑环境：'+statusText(buildings.status||'missing_data')+' · count '+GridTheme.formatNumber(buildings.building_count)+' · coverage '+GridTheme.formatNumber(buildings.building_coverage_ratio)+' · mean/P95/max '+GridTheme.formatNumber(buildings.height_mean_m)+' / '+GridTheme.formatNumber(buildings.height_p95_m)+' / '+GridTheme.formatNumber(buildings.height_max_m)+' m';
  const risk=item.risk||{},ground=risk.ground||{},operationalAir=risk.air||{},overall=risk.overall||{},riskResult=flow?.grid_risk||{};
  const p=ground.contributors?.population||{},t=ground.contributors?.terrain||{};
  const riskSummary='Ground Risk：'+riskValue(ground)+'\n'+
    '  P：'+factorValue(p)+'\n'+
    '  T：'+factorValue(t)+(t.raw?.relief===undefined?'':' · relief '+GridTheme.formatNumber(t.raw.relief))+'\n'+
    'Operational Air Risk：'+riskValue(operationalAir)+'\n'+
    'Airspace：not applicable（display-only reference layer）\n'+
    'Overall Risk：'+riskValue(overall)+' · 完整度 '+GridTheme.formatNumber((overall.data_completeness||0)*100)+'%\n'+
    '风险语义：'+(overall.semantics||risk.semantics||'relative_index')+' · '+(riskResult.algorithm_id||'未计算')+'@'+(riskResult.algorithm_version||'-');
  return cell.grid_id+' · L'+cell.level+'\n'+
    populationDisplayLabel(populationResult)+'：样本 '+populationSamples+' · '+populationValues+' · '+(population.value_status||population.quantity_status||'missing_data')+'\n'+
    'DEM：样本 '+terrainSamples+' · '+terrainValues+' · '+(terrainResult.unit_status||'单位来源未知')+' · '+terrainSource+'\n'+
    buildingSummary+'\n'+airspaceSummary+'\n'+trafficSummary+'\n'+conflictSummary+'\n'+riskSummary;
}
function riskValue(component){return component?.status==='passed'&&Number.isFinite(component.score)?GridTheme.formatNumber(component.score)+' / '+(component.level||'未分级'):'无数据（'+statusText(component?.status||'not_calculated')+'）';}
function factorValue(factor){return factor?.status==='passed'?'归一化 '+GridTheme.formatNumber(factor.normalized)+' · contribution '+GridTheme.formatNumber(factor.contribution):statusText(factor?.status||'not_available');}
canvas.addEventListener('click',event=>{
  const info=$('gridInfo');
  if(interactionMode!=='pan'){info.hidden=true;return;}
  const [lon,lat]=eventLonLat(event);
  if(currentStep===3){
    const selected=hitReferenceObject(event);
    if(selected){selectedReference=selected;info.hidden=true;renderWorkflow();paint();return;}
  }
  if(!gridRenderCache.cells.length){info.hidden=true;return;}
  const item=findGridCell(lon,lat);
  if(!item){info.hidden=true;return;}
  const rect=map.getBoundingClientRect();
  info.textContent=formatGridDetails(item);
  info.style.left=Math.max(8,Math.min(event.clientX-rect.left+12,rect.width-440))+'px';
  info.style.top=Math.max(8,event.clientY-rect.top-38)+'px';
  info.hidden=false;
});
function hitReferenceObject(event){
  const rect=canvas.getBoundingClientRect(),click=[event.clientX-rect.left,event.clientY-rect.top],overlay=Step03.referenceOverlayModel(flow,{routes:$('referenceRouteLayer').checked,points:$('referenceRoutePointLayer').checked,landingSites:false});
  return hitReferenceOverlay(click,overlay,screenPoint);
}
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
for(const id of ['gridLayer','cLayer','nLayer','sLayer','existingCnsLayer','candidateSiteLayer','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','buildingClearanceLayer','v3CandidateLayer'])$(id).onchange=()=>{
  if(id==='gridLayer')gridDisplay.outline=$('gridLayer').checked;
  if(id==='gridLayer'&&$('gridOutlineToggle'))$('gridOutlineToggle').checked=gridDisplay.outline;
  updateGridNotice();
  paint();
};
new ResizeObserver(()=>{if(view)queue();else paint();}).observe(map);
function statusText(status){return labelFor(status);}
function statusBadge(status){return badgeFor(status);}
function escapeHtml(value){return escapeValue(value);}
function setStep(step){
  currentStep=Number(step);interactionMode='pan';draftWorkspace=null;profileHoverCoordinate=null;
  store.set({ui:{...store.get().ui,step:currentStep,interactionMode}});
  document.querySelectorAll('[data-step]').forEach(button=>button.classList.toggle('active',Number(button.dataset.step)===currentStep));
  $('cnsLayers').hidden=currentStep<5;renderWorkflow();paint();
}
document.querySelectorAll('[data-step]').forEach(button=>button.onclick=()=>setStep(button.dataset.step));
function actionButton(id,handler){const button=$(id);if(button)button.onclick=async()=>{try{button.disabled=true;await handler();}catch(exc){panelError(exc.message);}finally{if(document.body.contains(button))button.disabled=false;}};}
function renderWorkflow(){
  if(!flow)return;const panel=$('workflowPanel');
  const referenceDiagnostics=referenceLayerDiagnostics(flow);for(const [id,item] of [['referenceRouteStatus',referenceDiagnostics.routes],['referenceRoutePointStatus',referenceDiagnostics.points],['referenceLandingStatus',referenceDiagnostics.landingSites]]){const target=$(id);if(target){target.textContent=item.label;target.title=item.status+' · '+item.reason;}}
  $('workflowStatus').textContent='项目：'+flow.project.name+' · 第 '+currentStep+' 步';
  const storage=state?.project_storage||{};
  $('projectRestore').textContent=storage.automatic
    ? (flow.last_saved_at
        ? '自动恢复项目已保存 · 建议另存到项目文件夹'
        : '当前使用自动恢复项目')
    : ('项目保存位置：'+(storage.directory||'未选择'));
  const steps=[Step01,Step02,Step03,Step04,Step05,Step06],step=steps[currentStep-1];
  panel.innerHTML=step.render({state,flow,draftWorkspace,gridDisplay,interactionMode,selectedReference,populationDisplayLabel,formatNumber:GridTheme.formatNumber});
  step.bind(stepBindings());
}
function stepBindings(){return {
  $,flow:()=>flow,mutate,resourceAction,computeAction,panelError,setStep,openBrowser:sourceCenter.openBrowser,searchPlace,actionButton,paint,
  loadRoutePlannerV3Detail,
  saveProject,openProject,
  previewPlanningReport,downloadPlanningReport,
  selectReference(value){selectedReference=value;renderWorkflow();paint();},setProfileHover(value){profileHoverCoordinate=value;paint();},
  setGridOutline(value){gridDisplay.outline=value;$('gridLayer').checked=value;updateGridNotice();paint();},
  setGridTheme(value){gridDisplay.theme=value;updateGridThemeLegend();paint();},
  startWorkspace(){interactionMode='workspace';draftWorkspace=null;panelError('请在地图上按住并拖出矩形工作区');},
  clearWorkspace:async()=>{draftWorkspace=null;interactionMode='pan';await mutate('workspace-clear');},
  saveWorkspace:async()=>{await mutate('workspace',{bbox:draftWorkspace,grid_level:Number($('workspaceGridLevel')?.value||8)});interactionMode='pan';draftWorkspace=null;fitLonLatBbox(flow.workspace?.bbox);},
  toggleNodeMode(){interactionMode=interactionMode==='node'?'pan':'node';renderWorkflow();}
};}
async function previewPlanningReport(){
  const target=window.open('about:blank','_blank');
  try{
    const result=await computeAction('/api/cns-planning-report/preview',{}),blob=new Blob([result.html],{type:'text/html;charset=utf-8'}),url=URL.createObjectURL(blob);
    if(target)target.location.href=url;else throw Error('浏览器阻止了预览窗口，请允许本地工作台打开新窗口');
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    panelError('报告草稿已在新窗口打开；预览不会写入项目。');
  }catch(exc){if(target)target.close();throw Error('报告预览失败：'+exc.message+'。请检查项目状态后重试。');}
}
async function downloadPlanningReport(kind){
  const reports=flow?.cns_planning_reports||{},reportId=reports.active_report_id;
  if(!reportId)throw Error('尚无正式报告。请先确认方案并点击“生成正式报告”。');
  const url='/api/cns-planning-report/artifact?'+new URLSearchParams({report_id:reportId,kind});
  const blob=await api(url),objectUrl=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=objectUrl;link.download={html:'cns-planning-report.html',pdf:'cns-planning-report.pdf',package:'cns-planning-package.zip',json:'cns-planning-report.json'}[kind]||'report.bin';link.click();
  setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);
}
async function saveProject(projectDir,name){
  if(!projectDir)return panelError('请先选择项目数据存储位置');
  const button=$('saveProject');try{button.disabled=true;panelError('');await api('/api/workflow/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});update(await api('/api/project/save-as',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_dir:projectDir})}));}catch(exc){panelError('保存项目失败：'+exc.message);}finally{if(document.body.contains(button))button.disabled=false;}
}
async function openProject(projectDir){
  if(!projectDir)return panelError('请先选择项目文件夹');
  const button=$('openProject');try{button.disabled=true;panelError('');const data=await api('/api/project/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_dir:projectDir})});update(data);bitmap?.close();bitmap=null;if(data.workflow?.workspace?.bbox)fitLonLatBbox(data.workflow.workspace.bbox);else if(data.bounds)fit(data.bounds);}catch(exc){panelError('打开项目失败：'+exc.message);}finally{if(document.body.contains(button))button.disabled=false;}
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
const sourceCenter=createSourceCenter({
  $,api,onlineTiles,actionButton,
  onApplied(data){update(data);bitmap?.close();bitmap=null;fit(data.bounds);}
});
sourceCenter.bind();
function update(data){
  state=data;
  flow=data.workflow;
  store.set({server:state,workflow:flow,mapView:view});
  rebuildGridRenderCache();
  onlineTiles.configure(data.online_sources||[],data.revision);
  $('basemapPath').value=data.paths.basemap;
  $('populationPath').value=data.paths.population;
  $('terrainPath').value=data.paths.terrain||'';
  $('terrain_dtmPath').value=data.paths.terrain_dtm||'';
  $('buildingsPath').value=data.paths.buildings||'';
  $('building_gridPath').value=data.paths.building_grid||'';
  $('reference_landing_sitesPath').value=data.paths.reference_landing_sites||'';
  $('reference_routesPath').value=data.paths.reference_routes||'';
  const population=data.population;
  $('rasterInfo').textContent=population.width?'WorldPop R2025A：'+population.width.toLocaleString()+' × '+population.height.toLocaleString()+' · '+population.crs+'\nquantity：'+(population.quantity||'population_count_per_source_pixel')+' · unit：'+(population.unit||'person/source_pixel')+'\nresolution：3 arc-second · NoData：'+population.nodata+' · '+(population.verification?.status||'unverified'):'尚未加载有效人口数据';
  const terrain=data.terrain||{};
  const terrainDtm=data.terrain_dtm||{};
  $('terrainDtmInfo').textContent=terrainDtm.width?'FABDEM DTM：'+terrainDtm.width.toLocaleString()+' × '+terrainDtm.height.toLocaleString()+' · '+terrainDtm.crs+'\n'+terrainDtm.dtype+' · NoData：'+terrainDtm.nodata+' · '+terrainDtm.vertical_reference+' ('+terrainDtm.vertical_status+')':'尚未加载有效 FABDEM DTM';
  $('terrainInfo').textContent=terrain.width?'GLO-30 DSM：'+terrain.width.toLocaleString()+' × '+terrain.height.toLocaleString()+' · '+terrain.crs+'\nNoData：'+terrain.nodata+'\n像元大小：'+(terrain.pixel_size||[]).join(' × ')+'\n单位：'+(terrain.unit||'m')+' · 水平 WGS84/EPSG:4326 · 垂直 EGM2008/EPSG:3855 · 1 arc-second':'尚未加载有效地形 DEM';
  $('sourceSummary').textContent=data.layers.length+' 个本地图层 · '+data.paths.basemap.split(/[\\/]/).pop();
  sourceCenter.render(data);
  renderWorkflow();
  syncGridApis().then(()=>{renderWorkflow();paint();}).catch(exc=>showError('网格专题同步失败：'+exc.message));
  if(data.error)showError(data.error);
}
api('/api/state').then(data=>{
  update(data);
  if(data.workflow?.workspace?.bbox)fitLonLatBbox(data.workflow.workspace.bbox);
  else if(data.bounds)fit(data.bounds);
  else{$('loading').hidden=true;$('settings').showModal();}
}).catch(exc=>{showError('无法连接本机地图服务：'+exc.message);$('loading').hidden=true;});
