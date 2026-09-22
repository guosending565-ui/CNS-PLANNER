import {createApiClient} from './api/client.js';
import {createStore} from './state/store.js';
import {eventLonLat as projectEvent,lonLatToMercator,mercatorToLonLat,screenPoint as projectPoint} from './map/projection.js';
import {buildGridOverlayCache,findGridCell as hitGridCell} from './map/grid_overlay.js';
import {bindMapInteraction} from './map/interaction.js';
import {drawGridTheme,drawStandardGrid,drawWorkspace,drawLine} from './map/renderer.js';
import {hitReferenceObject as hitReferenceOverlay,drawReferenceOverlay,referenceLayerDiagnostics} from './map/reference_overlay.js';
import {buildDisplayPlan,drawWorkflowLayers,hitDisplayEntry,hitCnsTowerCandidate,entryExtent} from './map/display_layers.js';
import {attachBuildingFootprintLayer} from './map/building_footprint_layer.js';import {attachTowerReferenceLayer,towerDetailContext} from './map/tower_reference_layer.js';
import {updateLayeredLegends} from './workflow/layered_legend.js';import {updateMapLegend} from './workflow/map_legend.js';
import {renderWorkflowSteps} from './workflow/steps.js';
import {createWorkbench} from './workflow/workbench.js';
import {POPULATION_PALETTE,RISK_PALETTE,TERRAIN_PALETTE,BUILDING_PALETTE,gridThemeLegendModel,gridThemeLegendNote} from './workflow/grid_theme_legend.js';
import {escapeHtml as escapeValue,statusBadge as badgeFor,statusText as labelFor} from './workflow/common.js';
import {riskV2LegendModel} from './workflow/risk_framework_v2.js';
import {gridCellDetailsHtml,populationDisplayLabel} from './workflow/grid_details.js';
import {attachMeasureTool} from './map/measure_tool.js';
import {bindShell,bindLayerControls,updateLodBadge,renderRailSteps,layerSwitches} from './shell.js';
import * as Step01 from './workflow/step01_project.js';
import * as Step02 from './workflow/step02_workspace.js';
import * as Step03 from './workflow/step03_routes.js';
import * as Step04 from './workflow/step04_operation.js';
import * as Step05 from './workflow/step05_cns.js';
import * as Step06 from './workflow/step06_review.js';
import {createSourceCenter} from './sources/source_center.js';

const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d'),map=$('map');
const STEPS=[Step01,Step02,Step03,Step04,Step05,Step06];
// 统一的地图图层开关（图层抽屉里全部 checkbox 都在这里）：layeredFeasibilityLayer 只画
// coarse feasibility mask，layeredCandidateLayer 只画 current candidate，两者相互独立。
const LAYER_IDS=['buildingClearanceLayer','v3CandidateLayer','layeredFeasibilityLayer','layeredCandidateLayer','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','towerLayer','existingCnsLayer','candidateSiteLayer','cLayer','nLayer','sLayer','buildingFootprintLayer'];
let state=null,flow=null,view=null,bitmap=null,imageView=null,timer,serial=0,draftWorkspace=null;
let currentStep=1,interactionMode='pan',renderController=null,currentPlan=null;
let selectedReference=null,profileHoverCoordinate=null;
// 临时 evidence highlight（RouteRiskProfile → 地图联动）：纯 UI 状态，不写 ProjectState、
// 不调用 API、不改 zoom / layer / LOD，也不新增任何永久状态。
let routeEvidenceHighlight=null;
let gridDataSerial=0;
let gridDisplay={outline:false,theme:'none'};// 首次打开：网格边界默认关闭（图层抽屉里只有在线底图默认勾选）
let gridRenderCache={cells:[],byId:new Map(),spatial:null,populationBreaks:[],terrainBreaks:[],buildingCoverageBreaks:[],buildingP95Breaks:[],buildingMaxBreaks:[],v2Breaks:{factors:new Map(),domains:new Map()}};
const populationPalette=POPULATION_PALETTE,terrainPalette=TERRAIN_PALETTE;
const buildingPalette=BUILDING_PALETTE,riskPalette=RISK_PALETTE,riskBreaks=[0,.2,.4,.6,.8,1];
const client=crypto.randomUUID(),onlineTiles=new OnlineTiles(()=>requestAnimationFrame(paint),text=>$('tileStatus').textContent=text),measure=attachMeasureTool({$,canvas,paint,eventLonLat,getMode:()=>interactionMode,setMode:value=>{interactionMode=value;}});
const store=createStore({server:null,workflow:null,mapView:null,ui:{step:1,interactionMode:'pan',workbench:{step:1,tab:'operate',segs:{},scroll:0}}});
const api=createApiClient(()=>state?.token,()=>flow?.revision),buildingFootprints=attachBuildingFootprintLayer({api,$,paint,getView:()=>view,size,visibleLonLatBounds}),towerReference=attachTowerReferenceLayer({canvas,paint,getPlan:()=>currentPlan,isEnabled:()=>!!$('towerLayer')?.checked,getInfo:()=>$('gridInfo'),escapeHtml:escapeValue,getTowerContext:towerId=>towerDetailContext(flow,towerId)});// 建筑轮廓：默认关闭 + zoom LOD 按需拉取，装配全在模块内；铁塔交互同为只读叶子模块
// 右栏工作台视图状态：只保存展示导航（step / 一级 tab / 二级段 / 滚动位置）
const workbench=createWorkbench({
  getState:()=>store.get().ui.workbench,
  setState:value=>store.set({ui:{...store.get().ui,workbench:value}})
});
function layers(){return layerSwitches($,LAYER_IDS);}
async function applyWorkflow(data){flow=data;store.set({workflow:flow});rebuildGridRenderCache();try{await syncGridApis();}catch(exc){showError('网格专题同步失败：'+exc.message);}renderWorkflow();paint();return data;}
async function mutate(action,payload={}){return applyWorkflow(await api('/api/workflow/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));}
// GRID-L8-UNIFICATION：超限时后端返回 blocked（不是降级）并已把该状态落库；重新读取快照让界面显示阻断原因。
async function refreshWorkflow(){return applyWorkflow(await api('/api/workflow'));}
async function resourceAction(path,payload={}){const data=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});flow=data;store.set({workflow:flow});rebuildGridRenderCache();renderWorkflow();paint();return data;}
async function computeAction(path,payload={}){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
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
// fromScreen：屏幕像素 → EPSG:3857（fitScreenBox 与聚类放大沿用此语义）
function fromScreen(point){const [w,h]=size();return [view.x+(point[0]-w/2)*view.res,view.y-(point[1]-h/2)*view.res];}
// screenToLonLat：屏幕像素 → 经纬度。只供显示计划换算聚合中心，与 fromScreen 相互独立
function screenToLonLat(point){return mercatorToLonLat(...fromScreen(point));}
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
function fitScreenBox(box){
  if(!box)return;
  const first=mercatorToLonLat(...fromScreen([box[0],box[3]])),second=mercatorToLonLat(...fromScreen([box[2],box[1]]));
  fitLonLatBbox([first[0],first[1],second[0],second[1]]);
}
function paint(){
  const [w,h]=size();if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
  ctx.fillStyle='#f3f4f2';ctx.fillRect(0,0,w,h);onlineTiles.paint(ctx,view,w,h,'base');
  if(bitmap&&view&&imageView){const b=imageView;ctx.drawImage(bitmap,w/2+(b[0]-view.x)/view.res,h/2-(b[3]-view.y)/view.res,(b[2]-b[0])/view.res,(b[3]-b[1])/view.res);}
  onlineTiles.paint(ctx,view,w,h,'annotation');drawWorkflowOverlay();if(view)measure.draw(ctx,screenPoint);
}
function rebuildGridRenderCache(){
  gridRenderCache=buildGridOverlayCache(flow?.grid,flow?.grid_attributes||{},flow?.grid_risk||{},GridTheme,flow?.grid_risk_v2||null);
  if($('gridInfo'))$('gridInfo').hidden=true;
  updateGridNotice();updateGridThemeLegend();
}
function findGridCell(lon,lat){return hitGridCell(gridRenderCache,lon,lat,GridTheme);}
function drawGridThemes(){drawGridTheme({ctx,view,flow,cache:gridRenderCache,display:gridDisplay,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme,palettes:{population:populationPalette,terrain:terrainPalette,buildings:buildingPalette,risk:riskPalette},riskBreaks});}
function drawGridBoundaries(){drawStandardGrid({ctx,view,grid:flow?.grid,display:gridDisplay,enabled:$('gridLayer')?.checked,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme});}
function formatGridDetails(item){return gridCellDetailsHtml(item,flow,GridTheme.formatNumber,gridDisplay.theme);}
// CNS 缺口段计数：只读汇总，用于状态栏提示；绘制本身在 map/display_layers.js
function cnsGapSegments(){
  const analysis=flow?.cns_gap_analysis;
  if(!analysis||analysis.status==='stale')return 0;
  let count=0;
  for(const route of analysis.routes||[]){
    for(const subsystem of route.subsystems||[])count+=(subsystem.uncovered_segments||[]).length;
  }
  return count;
}
function referenceFilters(){
  const active=currentStep===3;  return {workspace:flow?.workspace,search:active?$('referenceSiteSearch')?.value||'':'',
    region:active?$('referenceSiteRegion')?.value||'':'',siteType:active?$('referenceSiteType')?.value||'':''};
}
// 尺度相关的显示决定全部来自 map/lod.js 与屏幕空间聚合，这里只组装输入
function displayPlan(){
  const switches=layers();
  // 参考航路点跟随图层开关进入计划；是否真正显示仍由 LOD 决定（overview/medium 隐藏）
  const overlay=Step03.referenceOverlayModel(flow,{routes:switches.referenceRouteLayer,points:switches.referenceRoutePointLayer,landingSites:false});
  return buildDisplayPlan({flow,view,size:size(),screenPoint,screenToLonLat,layers:switches,referenceOverlay:overlay,
    selectedReference,referenceFilters:referenceFilters(),filterReferenceSites:Step03.filterReferenceSites,towerHighlight:towerReference.highlightedTower()});
}
function drawWorkflowOverlay(){
  if(!view||!flow)return;
  currentPlan=displayPlan();
  updateLodBadge($('lodStatus'),currentPlan,escapeHtml);
  drawWorkflowLayers({
    ctx,view,flow,plan:currentPlan,layers:layers(),screenPoint,profileHoverCoordinate,gridTheme:GridTheme,
    towerHighlight:towerReference.highlightedTower(),routeEvidenceHighlight,
    proposedPlanActions,
    drawWorkspace:()=>drawWorkspace(ctx,screenPoint,draftWorkspace||flow.workspace?.bbox),
    drawGridThemes,drawGridBoundaries,drawBuildingFootprints:()=>buildingFootprints.draw(ctx,screenPoint)
  });
  // 参考层：只读参考数据；视觉层级 candidate > scenario / reference，因此参考线再压一层。
  const plan=currentPlan,switches=layers(),styles=plan.styles;
  drawReferenceOverlay({
    ctx,view,screenPoint,drawLine,
    routes:switches.referenceRouteLayer?plan.referenceRoutes:[],
    points:(switches.referenceRoutePointLayer&&plan.referencePointsVisible)?plan.referencePoints:[],
    routeWidth:styles.referenceWidth,routeAlpha:styles.referenceAlpha*.7,
    pointRadius:styles.pointRadius,pointAlpha:styles.pointAlpha,
    labelMode:plan.level==='detail'?'detail':'hidden'
  });
  if(selectedReference&&plan.selectedScreen){
    const [x,y]=plan.selectedScreen;ctx.save();
    ctx.fillStyle='#8f2f6b';ctx.strokeStyle='#fff';ctx.lineWidth=2;
    ctx.beginPath();ctx.arc(x,y,8,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.restore();
  }
}
function proposedPlanActions(value){
  const p16=value?.cns_corridor_site_plan||{},review=value?.cns_plan_review||{};
  if(value?.confirmed_cns_plan?.application?.status==='applied')return [];
  const variant=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id);
  if(!variant)return p16.selected_actions||[];
  const ids=new Set(variant.selected_action_ids||[]),catalog=[...(p16.candidate_actions||[]),...(p16.selected_actions||[])];
  return catalog.filter((item,index)=>ids.has(item.action_id)&&catalog.findIndex(other=>other.action_id===item.action_id)===index);
}
function queue(){paint();clearTimeout(timer);serial++;renderController?.abort();timer=setTimeout(()=>{onlineTiles.update(view,...size(),$('online').checked);renderMap();buildingFootprints.sync();},160);}
async function renderMap(){
  if(!view||!state?.bounds)return;const started=performance.now(),request=serial,box=mapBounds(),[w,h]=size(),factor=Math.min(1,1600/w,1000/h);
  const q=new URLSearchParams({
    bbox:box.join(','),w:Math.round(w*factor),h:Math.round(h*factor),
    pop:$('pop').checked?'1':'0',air:$('air').checked?'1':'0',terrain:$('terrain').checked?'1':'0',
    opacity:$('opacity').value/100,terrainOpacity:$('terrainOpacity').value/100,
    rev:state.revision,client,seq:request
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
  onPosition:(point,event)=>{$('position').textContent=point[0].toFixed(5)+'° E / '+point[1].toFixed(5)+'° N · WGS84';measure.hover(point,event);},
  onPanStart:()=>{clearTimeout(timer);serial++;}
});
canvas.addEventListener('click',event=>{
  const info=$('gridInfo');
  if(interactionMode!=='pan'){info.hidden=true;return;}const rect=map.getBoundingClientRect();
  // 可见聚合点的"放大到范围"优先于其下方被隐藏/弱化的参考对象命中
  const clusterTarget=hitClusterAt(event);
  if(clusterTarget&&clusterTarget.count>1){const box=entryExtent(clusterTarget);if(box)fitScreenBox(box);info.hidden=true;return;}
  if(towerReference.candidateClick(event))return;
  if(towerReference.detail(clusterTarget,event))return;// 铁塔详情：字段与"不派生通信能力"声明都在 tower_reference_layer.js
  // 参考对象交互只在 detail 档保留：overview/medium 下参考层被弱化或隐藏，不参与命中
  if(currentStep===3){
    const selected=hitReferenceObject(event);
    if(selected){selectedReference=selected;info.hidden=true;renderWorkflow();paint();return;}
  }
  const [lon,lat]=eventLonLat(event);
  const item=gridRenderCache.cells.length?findGridCell(lon,lat):null;
  if(!item){info.hidden=true;if(selectedReference){selectedReference=null;}return;}
  info.innerHTML=formatGridDetails(item);
  info.style.left=Math.max(8,Math.min(event.clientX-rect.left+12,rect.width-440))+'px';
  info.style.top=Math.max(8,event.clientY-rect.top-38)+'px';
  info.hidden=false;
});
function hitClusterAt(event){
  if(!currentPlan)return null;
  const rect=canvas.getBoundingClientRect(),click=[event.clientX-rect.left,event.clientY-rect.top];
  return hitDisplayEntry(currentPlan,click,{kind:'nodes'})||hitDisplayEntry(currentPlan,click,{kind:'sites'})||hitDisplayEntry(currentPlan,click,{kind:'towers'});
}
// 参考对象命中只在 detail 档保留（overview/medium 的参考层被弱化或隐藏，不参与交互）。
// 其中参考航路点还要满足"当前 LOD 允许显示且图层开关打开"——referencePointsVisible
// 已经把这两件事都算进去了，隐藏的点因此永远不会命中。
function hitReferenceObject(event){
  if(!currentPlan||currentPlan.level!=='detail')return null;
  const rect=canvas.getBoundingClientRect(),click=[event.clientX-rect.left,event.clientY-rect.top],
    points=currentPlan.referencePointsVisible?(currentPlan.referencePoints||[]):[],
    overlay=Step03.referenceOverlayModel(flow,{routes:$('referenceRouteLayer').checked,points:false,landingSites:false});
  return hitReferenceOverlay(click,{...overlay,referencePoints:points},screenPoint);
}
$('zoomIn').onclick=()=>zoom(.5);$('zoomOut').onclick=()=>zoom(2);$('fit').onclick=()=>fit(state?.bounds);
function updateGridNotice(){
  const notice=$('gridNotice');if(!notice)return;
  const hasGrid=flow?.grid?.status==='passed'&&gridRenderCache.cells.length>0;
  notice.hidden=!gridDisplay.outline||hasGrid;
  notice.textContent='请先在第02步保存工作区以生成标准网格';
}
function updateGridThemeLegend(){
  if(updateLayeredLegends({$,flow,formatNumber:GridTheme.formatNumber}))return;
  if(updateRiskV2Legend())return;
  const legend=$('gridThemeLegend'),model=gridThemeLegendModel(flow,gridRenderCache,gridDisplay);
  if(!legend)return;
  legend.hidden=!model;
  if(!model)return;
  $('gridThemeLegendTitle').textContent=model.title;
  $('gridThemeLegendUnit').textContent=model.unit;
  $('gridThemeGradient').style.background='linear-gradient(to right,'+model.palette.join(',')+')';
  const ticks=$('gridThemeTicks');ticks.replaceChildren();
  for(const value of (model.shown.length?model.shown:['无有效值'])){const span=document.createElement('span');span.textContent=typeof value==='number'?GridTheme.formatNumber(value):value;ticks.append(span);}
  $('gridThemeLegendNote').textContent=gridThemeLegendNote(model,statusText,GridTheme.formatNumber);
}
// Risk Framework V2 legend: factor/domain layers are relative engineering
// indices.  A pending/unresolved domain index renders as "no data", never as 0.
function updateRiskV2Legend(){
  const legend=$('gridThemeLegend'),model=riskV2LegendModel(gridDisplay.theme,gridRenderCache,GridTheme);
  if(!model||!legend)return false;
  legend.hidden=false;
  $('gridThemeLegendTitle').textContent=model.title;
  $('gridThemeLegendUnit').textContent=model.unit;
  $('gridThemeGradient').style.background='linear-gradient(to right,'+riskPalette.join(',')+')';
  const ticks=$('gridThemeTicks');ticks.replaceChildren();
  for(const value of (model.ticks.length?model.ticks:['无有效值'])){const span=document.createElement('span');span.textContent=value;ticks.append(span);}
  $('gridThemeLegendNote').textContent=model.note;
  return true;
}
function syncLayerControls(){
  bindLayerControls({
    $,layerIds:LAYER_IDS,queue,paint,
    setGridOutline(value){gridDisplay.outline=value;},
    updateGridNotice,updateGridThemeLegend,updateMapLegend,
    onOnlineTiles:()=>onlineTiles.update(view,...size(),$('online').checked)
  });
}
function statusText(status){return labelFor(status);}function statusBadge(status){return badgeFor(status);}function escapeHtml(value){return escapeValue(value);}
function setStep(step){
  currentStep=Number(step);interactionMode='pan';measure.sync();draftWorkspace=null;profileHoverCoordinate=null;
  routeEvidenceHighlight=null;
  store.set({ui:{...store.get().ui,step:currentStep,interactionMode}});
  workbench.clearState();
  $('cnsLayers').hidden=currentStep<5;
  renderWorkflow();paint();
}
for(const button of document.querySelectorAll('#steps [data-step]'))button.onclick=()=>setStep(button.dataset.step);
function actionButton(id,handler){const button=$(id);if(button)button.onclick=async()=>{try{button.disabled=true;await handler();}catch(exc){panelError(exc.message);}finally{if(document.body.contains(button))button.disabled=false;}};}
function renderWorkflow(){
  if(!flow)return;
  const referenceDiagnostics=referenceLayerDiagnostics(flow);
  for(const [id,item] of [['referenceRouteStatus',referenceDiagnostics.routes],['referenceRoutePointStatus',referenceDiagnostics.points],['referenceLandingStatus',referenceDiagnostics.landingSites],['towerLayerStatus',referenceDiagnostics.towers]]){
    const target=$(id);if(target){target.textContent=item.label;target.title=item.status+' · '+item.reason;}
  }
  const step=STEPS[currentStep-1],body=workbench.body();
  // mutation / 重新渲染后保持当前一级与二级标签以及滚动位置
  const scroll=body?body.scrollTop:0;
  store.set({ui:{...store.get().ui,workbench:{...store.get().ui.workbench,scroll}}});
  const rendered=renderWorkflowSteps({step,context:{state,flow,draftWorkspace,gridDisplay,interactionMode,selectedReference,populationDisplayLabel,formatNumber:GridTheme.formatNumber,routeEvidenceHighlight}});
  workbench.mount({root:rendered,step});
  step.bind(stepBindings());
  if(body)body.scrollTop=Math.min(scroll,Math.max(0,body.scrollHeight-body.clientHeight));
  const projectName=document.querySelector('[data-project-name]');
  if(projectName)projectName.textContent=flow.project.name;
  if($('workflowStatus')){
    const gaps=cnsGapSegments();
    $('workflowStatus').textContent='项目：'+flow.project.name+' · 第 '+currentStep+' 步'+(gaps?' · CNS 缺口段 '+gaps:'');
  }
  renderRailSteps($,flow,currentStep,selector=>document.querySelectorAll(selector));updateMapLegend({$,flow});
  const storage=state?.project_storage||{};
  if($('projectRestore'))$('projectRestore').textContent=storage.automatic
    ? (flow.last_saved_at?'自动恢复项目已保存 · 建议另存到项目文件夹':'当前使用自动恢复项目')
    : ('项目保存位置：'+(storage.directory||'未选择'));
}
function stepBindings(){return {
  $,flow:()=>flow,mutate,resourceAction,computeAction,panelError,setStep,openBrowser:sourceCenter.openBrowser,searchPlace,actionButton,paint,
  loadRoutePlannerV3Detail,
  saveProject,openProject,
  previewPlanningReport,downloadPlanningReport,
  selectReference(value){selectedReference=value;renderWorkflow();paint();},setProfileHover(value){profileHoverCoordinate=value;paint();},
  // RouteRiskProfile → 地图的临时联动：只改纯 UI 高亮状态并重绘。
  routeEvidence:{
    set(value){routeEvidenceHighlight=value||null;paint();},
    clear(){routeEvidenceHighlight=null;paint();},
  },
  setGridOutline(value){gridDisplay.outline=value;$('gridLayer').checked=value;updateGridNotice();paint();},
  setGridTheme(value){gridDisplay.theme=value;updateGridThemeLegend();paint();},remapPopulation:async()=>{const data=await resourceAction('/api/workspace/grid/population/remap',{});try{await syncGridApis();}catch(exc){showError('网格专题同步失败：'+exc.message);}renderWorkflow();paint();return data;},
  startWorkspace(){interactionMode='workspace';measure.sync();draftWorkspace=null;panelError('请在地图上按住并拖出矩形工作区');},
  clearWorkspace:async()=>{draftWorkspace=null;interactionMode='pan';measure.sync();await mutate('workspace-clear');},
  // 正式工作流只有一个 canonical 层级（MH/T 4063.1 L8）：grid_level 是常量，不来自任何下拉选择。
  // 超限时后端返回 blocked（不是降级）并已把该状态落库：重新读取快照让界面如实显示
  // "已阻断 + 需求格数 / 上限 / 建议"，再把可读错误交给统一的 actionButton 显示。
  saveWorkspace:async()=>{try{await mutate('workspace',{bbox:draftWorkspace,grid_level:Step02.OPERATIONAL_GRID_LEVEL});}catch(exc){try{await refreshWorkflow();}catch(_){/* 读取失败时仍把原始错误暴露给用户 */}throw exc;}interactionMode='pan';measure.sync();draftWorkspace=null;fitLonLatBbox(flow.workspace?.bbox);},
  toggleNodeMode(){interactionMode=interactionMode==='node'?'pan':'node';measure.sync();renderWorkflow();}
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
// 顶部全局"保存项目"：沿用既有保存语义（step01 保存按钮仍是同一个入口）
async function saveProject(projectDir,name){
  const directory=projectDir||state?.project_storage?.directory||'';
  if(!directory)return panelError('请先在第01步选择项目数据存储位置');
  const button=$('saveProjectTop')||$('saveProject');
  try{
    if(button)button.disabled=true;panelError('');
    const projectName=name||$('projectName')?.value||flow?.project?.name||'';
    const data=await api('/api/workflow/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:projectName})});
    flow=data;store.set({workflow:flow});
    update(await api('/api/project/save-as',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_dir:directory})}));
  }catch(exc){panelError('保存项目失败：'+exc.message);}
  finally{if(button&&document.body.contains(button))button.disabled=false;}
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
  rebuildGridRenderCache();buildingFootprints.reset();
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
  const terrain=data.terrain||{},terrainDtm=data.terrain_dtm||{};
  $('terrainDtmInfo').textContent=terrainDtm.width?'FABDEM DTM：'+terrainDtm.width.toLocaleString()+' × '+terrainDtm.height.toLocaleString()+' · '+terrainDtm.crs+'\n'+terrainDtm.dtype+' · NoData：'+terrainDtm.nodata+' · '+terrainDtm.vertical_reference+' ('+terrainDtm.vertical_status+')':'尚未加载有效 FABDEM DTM';
  $('terrainInfo').textContent=terrain.width?'GLO-30 DSM：'+terrain.width.toLocaleString()+' × '+terrain.height.toLocaleString()+' · '+terrain.crs+'\nNoData：'+terrain.nodata+'\n像元大小：'+(terrain.pixel_size||[]).join(' × ')+'\n单位：'+(terrain.unit||'m')+' · 水平 WGS84/EPSG:4326 · 垂直 EGM2008/EPSG:3855 · 1 arc-second':'尚未加载有效地形 DEM';
  $('sourceSummary').textContent=data.layers.length+' 个本地图层 · '+data.paths.basemap.split(/[\\/]/).pop();
  sourceCenter.render(data);
  renderWorkflow();
  syncGridApis().then(()=>{renderWorkflow();paint();}).catch(exc=>showError('网格专题同步失败：'+exc.message));
  if(data.error)showError(data.error);
}
// ---- 启动装配（只调用一次，避免重复 document / menu listener） -------------
bindShell({
  $,downloadExport,saveProject,panelError,
  previewReport:()=>previewPlanningReport(),
  generateReport:()=>resourceAction('/api/cns-planning-report/generate',{}),
  downloadReport:kind=>downloadPlanningReport(kind)
});
syncLayerControls();
new ResizeObserver(()=>{if(view)queue();else paint();}).observe(map);
// 两阶段 bootstrap：A 取不到 /api/state → 归因连接失败；B 已返回但前端初始化抛错
// → console.error 完整异常 + “前端初始化失败”，两者都不吞异常
function bootstrapFailure(message,exc){
  if(exc)console.error('[CNS Planner] '+message,exc);
  showError(message+'：'+(exc&&exc.message?exc.message:exc||''));
  $('loading').hidden=true;
}
api('/api/state').then(data=>{
  try{
    update(data);
    if(data.workflow?.workspace?.bbox)fitLonLatBbox(data.workflow.workspace.bbox);
    else if(data.bounds)fit(data.bounds);
    else{$('loading').hidden=true;$('settings').showModal();}
  }catch(exc){bootstrapFailure('前端初始化失败',exc);}
}).catch(exc=>bootstrapFailure('无法连接本机地图服务',exc));
