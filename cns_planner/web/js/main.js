import {createApiClient,createResourceMutationAndRefresh} from './api/client.js';
import {createStore} from './state/store.js';
import {eventLonLat as projectEvent,lonLatToMercator,mercatorToLonLat,screenPoint as projectPoint} from './map/projection.js';
import {buildGridOverlayCache,findGridCell as hitGridCell} from './map/grid_overlay.js';
import {bindMapInteraction} from './map/interaction.js';
import {drawGridTheme,drawStandardGrid,drawWorkspace,drawLine} from './map/renderer.js';
import {hitReferenceObject as hitReferenceOverlay,drawReferenceOverlay,referenceLayerDiagnostics} from './map/reference_overlay.js';
import {buildDisplayPlan,drawWorkflowLayers,hitDisplayEntry,hitCnsTowerCandidate,entryExtent} from './map/display_layers.js';
import {attachBuildingFootprintLayer} from './map/building_footprint_layer.js';import {attachTowerReferenceLayer,towerDetailContext} from './map/tower_reference_layer.js';
import {updateLayeredLegends} from './workflow/layered_legend.js';import {updateMapLegend,updateCnsServiceLegend} from './workflow/map_legend.js';
import {cnsMapFeatureAt,cnsMapFeatureTooltip} from './map/cns_service_overlay.js';
import {renderWorkflowSteps} from './workflow/steps.js';
import {createWorkbench} from './workflow/workbench.js';
import {POPULATION_PALETTE,RISK_PALETTE,TERRAIN_PALETTE,BUILDING_PALETTE,gridThemeLegendModel,gridThemeLegendNote} from './workflow/grid_theme_legend.js';
import {escapeHtml as escapeValue,statusBadge as badgeFor,statusText as labelFor} from './workflow/common.js';
import {riskV2LegendModel} from './workflow/risk_framework_v2.js';
import {gridCellDetailsHtml,populationDisplayLabel} from './workflow/grid_details.js';
import {attachMeasureTool} from './map/measure_tool.js';
import {bindShell,bindLayerControls,updateLodBadge,renderRailSteps,layerSwitches} from './shell.js';
import {createConstraintFieldView} from './workflow/constraint_view.js';
import {createShellActions} from './workflow/shell_actions.js';
import * as Step01 from './workflow/step01_project.js';
import * as Step02 from './workflow/step02_workspace.js';
import * as Step03 from './workflow/step03_routes.js';
import * as Step04 from './workflow/step04_operation.js';
import * as Step05 from './workflow/step05_cns.js';
import * as Step06 from './workflow/step06_review.js';
import {createSourceCenter} from './sources/source_center.js';
import {syncSourcePathInputs} from './sources/source_inputs.js';
import {createWorkflowSnapshotApplier} from './state/workflow_snapshot.js';
import {recordExplicitProject,restoreLastExplicitProject} from './state/explicit_project.js';
import {applyActiveProject} from './state/active_project.js';
import {bootstrapProjectState} from './state/bootstrap.js';
import {workspaceGridReadiness} from './state/readiness.js';
import {setupTaskCenter} from './tasks.js';
import {elapsedText} from './workflow/busy_action.js';

const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d'),map=$('map');
const STEPS=[Step01,Step02,Step03,Step04,Step05,Step06];
// 统一的地图图层开关（图层抽屉里全部 checkbox 都在这里）：layeredFeasibilityLayer 只画
// coarse feasibility mask，layeredCandidateLayer 只画 current candidate，两者相互独立。
// B4X 新增「高度层障碍」（Planning Constraint Field）三个开关：主开关 + 证据不足 + 可通行；
// 与其它图层一样默认关闭，数据按需从只读 HTTP 路径读取，不在启动时加载。
const LAYER_IDS=['buildingClearanceLayer','v3CandidateLayer','layeredFeasibilityLayer','layeredCandidateLayer','radarSurveillanceLayer','cnsCommunicationLayer','cnsNavigationLayer','cnsRidLayer','cnsServiceGapLayer','cnsFacilityPlanLayer','surfaceFactsLayer','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','towerLayer','existingCnsLayer','candidateSiteLayer','cLayer','nLayer','sLayer','buildingFootprintLayer','altitudeConstraintLayer','altitudeConstraintUnknownLayer','altitudeConstraintPassLayer'];
let state=null,flow=null,view=null,bitmap=null,imageView=null,timer,serial=0,draftWorkspace=null;
let currentStep=1,interactionMode='pan',renderController=null,currentPlan=null;
let selectedReference=null,profileHoverCoordinate=null;
// 临时 evidence highlight（RouteRiskProfile → 地图联动）：纯 UI 状态，不写 ProjectState、
// 不调用 API、不改 zoom / layer / LOD，也不新增任何永久状态。
let routeEvidenceHighlight=null,gridDataSerial=0;
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
// B4X：高度层障碍（Planning Constraint Field）在壳层的唯一装配点。全部逻辑
// （按需读取 / 覆盖层绘制 / 图例 / 点击详情 / 显式生成）都在 workflow/constraint_view.js，
// main.js 只做依赖注入，保持轻入口。
const constraintView=createConstraintFieldView({
  api,getFlow:()=>flow,getLayers:layers,visibleBounds:visibleLonLatBounds,
  getGridCache:()=>gridRenderCache,getNode:$,
  afterChange:()=>{renderWorkflow();updateMapLegend({$,flow});paint();}
});
// 唯一允许写入全局 flow 的地方：state/workflow_snapshot.js 统一「安装 snapshot → 按需
// hydrate 逐 cell 网格明细 → render」，gridDataSerial 由它独占管理（防竞态）。
// BUG-GRID-POPUP-001：通用 /api/workflow 快照是 slim 的（不含 cells），因此**所有**写入
// 路径（bootstrap / mutate / refresh / resourceAction / 打开项目）都只能走这一条。
const snapshotApplier=createWorkflowSnapshotApplier({
  getFlow:()=>flow,setFlow:value=>{flow=value;store.set({workflow:flow});},
  nextSerial:()=>++gridDataSerial,currentSerial:()=>gridDataSerial,
  fetchGrid:()=>api('/api/workspace/grid'),fetchAttributes:()=>api('/api/workspace/grid/attributes'),fetchRisk:()=>api('/api/grid-risk'),fetchRiskV2:()=>api('/api/grid-risk-v2'),fetchLayeredCandidates:()=>api('/api/layered-route-candidates'),fetchRadarSurveillance:()=>api('/api/radar-surveillance-layout'),
  // Round 3：P14 服务走廊逐体元 service 证据（只读专用 GET，按需读取）。
  fetchCorridorDetail:()=>api('/api/cns-service-corridor'),
  onError:message=>showError(message),afterApply:()=>{rebuildGridRenderCache();renderWorkflow();paint();},
  // A4：明细落地前的项目身份复核（迟到的旧项目明细必须被丢弃）。
  currentProjectIdentity:()=>projectIdentityOf()});
// Rescue Stable 主链默认只安装后端 workflow snapshot。逐格 grid/risk/mask 明细均为
// 专题图层或高级 popup 数据；默认图层关闭时不应在每次 mutation 后重复下载约 155 MB，
// 否则“保存 OD / 开启候选试算”等普通动作会长期停在“正在计算”。当前候选 path 与
// radar proposal 摘要已经包含在同一 snapshot 中，足够立即恢复主链地图与结果面板。
const applyWorkflowSnapshot=data=>snapshotApplier.applyWorkflowSnapshot(data,{hydrate:false}),applyWorkflow=data=>applyWorkflowSnapshot(data);
async function mutate(action,payload={}){return applyWorkflow(await api('/api/workflow/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));}
// GRID-L8-UNIFICATION：超限时后端返回 blocked（不是降级）并已把该状态落库；重新读取快照让界面显示阻断原因。
async function refreshWorkflow(){return applyWorkflow(await api('/api/workflow'));}
// 局部 mutation：POST 响应只作返回值 → GET /api/workflow → applyWorkflow → 返回 POST 响应（详见 api/client.js）。
const resourceMutationAndRefresh=createResourceMutationAndRefresh({post:(path,payload)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),refresh:()=>api('/api/workflow'),apply:applyWorkflow});
async function resourceAction(path,payload={}){return applyWorkflowSnapshot(await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));}
async function computeAction(path,payload={}){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
function showError(message){$('error').hidden=!message;$('error').textContent=message||'';}
/**
 * 面板提示行（右栏底部）。
 *
 * BUG-UI-SAVE-001：这里同时承担"错误"与"确认"两类结论，因此必须能表达语义——
 * "项目已保存"绝不能被渲染成红色错误。第二个参数是**可选**的语义标记
 * （`error` / `success` / `hint`），旧调用点（只传 message）行为完全不变。
 */
function panelError(message,tone='error'){
  const target=$('panelError');
  if(!target){showError(message);return;}
  target.dataset.tone=tone;
  target.textContent=message||'';
}
function size(){return [Math.max(1,Math.round(map.clientWidth)),Math.max(1,Math.round(map.clientHeight))];}
function fit(box){
  if(!box)return false;
  const values=(Array.isArray(box)?box:[box[0],box[1],box[2],box[3]]).map(Number);
  if(values.length!==4||!values.every(Number.isFinite))return false;
  const [w,h]=size();view={x:(values[0]+values[2])/2,y:(values[1]+values[3])/2,res:Math.max((values[2]-values[0])/w,(values[3]-values[1])/h)*1.1};
  if(!Number.isFinite(view.x)||!Number.isFinite(view.y)||!Number.isFinite(view.res)||view.res<=0){view=null;return false;}
  queue();return true;
}
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
  if(!Array.isArray(bbox)||bbox.length!==4)return false;
  const values=bbox.map(Number);
  if(!values.every(Number.isFinite))return false;
  return fit([lonLatToMercator(values[0],values[1]),lonLatToMercator(values[2],values[3])].flat());
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
// ---------------------------------------------------------------- 项目身份与地图视图生命周期
// BUG-MAP-RESTORE-001 的真实根因：`view` 是模块级变量，而"是否初始化视图"过去只判断
// `view == null`。切换项目时 view 仍是非空的**旧项目**视图，于是 ensureMapView() 直接
// 跳过；同时 openProject 在安装完成后 `bitmap?.close(); bitmap=null`，却没有一次收尾的
// repaint/queue —— 旧位图已清、新 renderMap 从未触发，用户看到的就是"地图空白"。
// 现在：视图的归属由 viewProjectIdentity 显式记录，身份变化必须重新初始化。
let viewProjectIdentity='';
/** 当前项目身份（只读服务器事实；缺失时用项目名 + revision 兜底，绝不猜路径）。 */
function projectIdentityOf(data){
  const storage=(data||state)?.project_storage||{};
  const id=String(storage.file||'').trim()||String(storage.directory||'').trim();
  if(id)return id;
  const workflow=(data||state)?.workflow||flow||{};
  return 'workflow:'+String(workflow.project?.name||'')+':'+String(workflow.revision??'');
}
/** 中止上一次渲染 + 释放旧位图 + 失效 view 归属。只负责清理，不负责建立新视图。 */
function resetProjectMapState(){
  serial++;                       // 让任何在途 /api/render 响应在回执时被丢弃
  renderController?.abort();      // 并且尽早取消它
  renderController=null;
  try{bitmap?.close();}catch(_){/* 已被释放的 ImageBitmap 不影响后续重建 */}
  bitmap=null;imageView=null;
  viewProjectIdentity='';
}
/**
 * 按固定优先级确定视图，并保证**一定有收尾重绘**。
 *
 *   0. view 已属于本项目且有效 → 同一项目内导航，保留用户 pan/zoom，绝不自动 fit；
 *   1. 项目保存过 map view（后端当前尚未持久化该字段，保留显式分支）；
 *   2. 工作区 bbox → fit workspace（打开新项目的正常路径）；
 *   3. 项目 bounds → fit bounds；
 *   4. 都没有 → 保持现有视图（若有），否则只 paint 不编造范围。
 *
 * 返回实际采用的分支，调用方据此判断"项目是否真的有了可用视图"。
 */
function ensureMapView(){
  const identity=projectIdentityOf();
  const saved=state?.map_view||flow?.map_view||null;
  const savedUsable=saved&&Number.isFinite(Number(saved.x))&&Number.isFinite(Number(saved.y))&&Number.isFinite(Number(saved.res))&&Number(saved.res)>0;
  if(view&&viewProjectIdentity===identity&&Number.isFinite(view.res)&&view.res>0)return 'unchanged';
  let branch='unchanged';
  if(savedUsable){
    view={x:Number(saved.x),y:Number(saved.y),res:Number(saved.res)};queue();branch='saved';
  }else if(fitLonLatBbox(flow?.workspace?.bbox)){
    branch='workspace';
  }else if(state?.bounds&&fit(state.bounds)){
    branch='bounds';
  }else if(view){
    queue();
  }else{
    paint();
  }
  // 只有真的拿到视图（或后端给了显式 map view）才算"视图归属本项目"。
  viewProjectIdentity=(view&&Number.isFinite(view.res)&&view.res>0)?identity:'';
  return branch;
}
function rebuildGridRenderCache(){
  gridRenderCache=buildGridOverlayCache(flow?.grid,flow?.grid_attributes||{},flow?.grid_risk||{},GridTheme,flow?.grid_risk_v2||null);
  if($('gridInfo'))$('gridInfo').hidden=true;
  updateGridNotice();updateGridThemeLegend();
}
function findGridCell(lon,lat){return hitGridCell(gridRenderCache,lon,lat,GridTheme);}
function drawGridThemes(){drawGridTheme({ctx,view,flow,cache:gridRenderCache,display:gridDisplay,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme,palettes:{population:populationPalette,terrain:terrainPalette,buildings:buildingPalette,risk:riskPalette},riskBreaks});}
function drawGridBoundaries(){drawStandardGrid({ctx,view,grid:flow?.grid,display:gridDisplay,enabled:$('gridLayer')?.checked,visibleBounds:visibleLonLatBounds,screenPoint,gridTheme:GridTheme});}
// 网格边界之上、航路之下：可行性单元不遮挡规划结果，也不被网格线切碎。
function drawConstraintLayer(){return constraintView.draw({ctx,view,screenPoint,gridTheme:GridTheme});}
function formatGridDetails(item){
  const gridId=String(item?.cell?.grid_id||'');
  return constraintView.cellDetailsHtml(gridId)+gridCellDetailsHtml(item,flow,GridTheme.formatNumber,gridDisplay.theme,constraintView.cellFor(gridId),constraintView.altitudeLayerLabel());
}

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
    towerHighlight:towerReference.highlightedTower(),routeEvidenceHighlight,proposedPlanActions,
    visibleBounds:visibleLonLatBounds,
    drawWorkspace:()=>drawWorkspace(ctx,screenPoint,draftWorkspace||flow.workspace?.bbox),
    drawGridThemes,drawGridBoundaries,drawBuildingFootprints:()=>buildingFootprints.draw(ctx,screenPoint),
    drawConstraintLayer});
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
  if(selectedReference&&plan.selectedScreen){const [x,y]=plan.selectedScreen;ctx.save();ctx.fillStyle='#8f2f6b';ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,8,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.restore();}
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
  renderController=new AbortController();const loading=$('loading');if(loading){loading.hidden=false;loading.textContent='更新本地图层…';}
  try{
    const response=await fetch('/api/render?'+q,{signal:renderController.signal});if(!response.ok)throw Error((await response.json()).error);
    const next=await createImageBitmap(await response.blob());if(request!==serial){next.close();return;}bitmap?.close();bitmap=next;imageView=box;paint();showError('');$('viewStatus').textContent='本地图层 '+(performance.now()-started).toFixed(0)+' ms';
  }catch(exc){if(request===serial&&exc.name!=='AbortError')showError(exc.message);}finally{if(request===serial&&loading)loading.hidden=true;}
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
  // Round D：CNS 四服务缺口 / 设施规划 / 导航增强基线的只读地图 tooltip。
  // 三个图层都默认关闭；全部关闭时不构造任何要素、不发请求（零成本），
  // 也绝不影响既有铁塔 / 参考对象 / 网格详情的命中顺序。
  const cnsLayers=layers();
  if(cnsLayers.cnsServiceGapLayer===true||cnsLayers.cnsFacilityPlanLayer===true||cnsLayers.cnsNavigationLayer===true){
    const click=[event.clientX-rect.left,event.clientY-rect.top];
    const feature=cnsMapFeatureAt({click,flow,layers:cnsLayers,screenPoint});
    if(feature){
      info.innerHTML=cnsMapFeatureTooltip(feature,flow);
      info.style.left=Math.max(8,Math.min(event.clientX-rect.left+12,rect.width-440))+'px';
      info.style.top=Math.max(8,event.clientY-rect.top-38)+'px';
      info.hidden=false;
      return;
    }
  }
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
  notice.hidden=!gridDisplay.outline||hasGrid;notice.textContent='请先在第02步保存工作区以生成标准网格';
}
function updateGridThemeLegend(){
  if(constraintView.updateLegend())return;
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
    updateGridNotice,updateGridThemeLegend,updateMapLegend:()=>updateMapLegend({$,flow}),
    onOnlineTiles:()=>onlineTiles.update(view,...size(),$('online').checked),
    // 勾选「高度层障碍」才按需读取逐格明细；取消勾选只停止绘制，不丢已读数据。
    onConstraintLayer:()=>{constraintView.loadMap();paint();}});
}
function statusText(status){return labelFor(status);}function statusBadge(status){return badgeFor(status);}function escapeHtml(value){return escapeValue(value);}
function setStep(step){
  currentStep=Number(step);interactionMode='pan';measure.sync();draftWorkspace=null;profileHoverCoordinate=null;routeEvidenceHighlight=null;
  store.set({ui:{...store.get().ui,step:currentStep,interactionMode}});
  workbench.clearState();
  $('cnsLayers').hidden=currentStep<5;
  renderWorkflow();paint();
}
for(const button of document.querySelectorAll('#steps [data-step]'))button.onclick=()=>setStep(button.dataset.step);
/**
 * 统一按钮绑定。
 *
 * BUG-TASK-FEEDBACK-001 §6：正式业务里仍有一部分是**同步耗时 POST**（Coverage3D /
 * Service Capability / Corridor / Gap / Site Planning / Radar Surveillance Layout /
 * Theta* V2 candidate 等）。它们不适合在本轮改造成后台任务，但绝不允许"点了毫无反馈"。
 * 因此这里统一做到：
 *   1. 点击后立即禁用按钮并标注「正在计算，请勿重复提交」（避免重复提交同一计算）；
 *   2. 已运行时长写在 title 上（不劫持按钮文案，避免与面板重渲染的文案冲突）；
 *   3. 请求结束后恢复按钮；失败把中文原因写到面板提示行（不吞异常）。
 * 后台任务路径（/api/tasks、async:true）仍然由 tasks.js 的面板负责排队 / 进度 / 取消。
 */
function actionButton(id,handler){
  const button=$(id);if(!button)return;
  button.onclick=async()=>{
    const originalTitle=button.title;
    const started=Date.now();
    let ticker=null;
    try{
      button.disabled=true;
      button.dataset.busy='true';
      button.title=(originalTitle?originalTitle+' · ':'')+'正在计算，请勿重复提交';
      ticker=setInterval(()=>{
        if(!document.body.contains(button))return;
        button.title=(originalTitle?originalTitle+' · ':'')+'正在计算，请勿重复提交（已运行 '
          +elapsedText((Date.now()-started)/1000)+'）';
      },1000);
      await handler();
    }catch(exc){panelError(exc.message);}
    finally{
      if(ticker!==null)clearInterval(ticker);
      if(document.body.contains(button)){
        button.disabled=false;
        delete button.dataset.busy;
        button.title=originalTitle;
      }
    }
  };
}
/**
 * 项目显示名（只读，缺失时为中文占位）。
 *
 * `flow.project` 在 slim 快照 / 半落地状态下可能不存在；直接读 `.name` 会让整条
 * 渲染链以技术错误中断（BUG-CONSTRAINT-UI-001 的 `.name` 现象之一）。
 */
function projectNameText(value){
  const name=value?.project?.name;
  if(name===null||name===undefined)return '未命名项目';
  const text=String(name).trim();
  return text||'未命名项目';
}
function renderWorkflow(){  if(!flow)return;
  const referenceDiagnostics=referenceLayerDiagnostics(flow);
  for(const [id,item] of [['referenceRouteStatus',referenceDiagnostics.routes],['referenceRoutePointStatus',referenceDiagnostics.points],['referenceLandingStatus',referenceDiagnostics.landingSites],['towerLayerStatus',referenceDiagnostics.towers]]){
    const target=$(id);if(target){target.textContent=item.label;target.title=item.status+' · '+item.reason;}
  }  const step=STEPS[currentStep-1],body=workbench.body();
  // mutation / 重新渲染后保持当前一级与二级标签以及滚动位置
  const scroll=body?body.scrollTop:0;
  store.set({ui:{...store.get().ui,workbench:{...store.get().ui.workbench,scroll}}});
  // 渲染保护（BUG-CONSTRAINT-UI-001 的 `.name` 异常现场）：面板渲染一旦抛错，
  // 修复前是**静默中断**——右侧只渲染了一半、地图不再 paint、用户只看到页面底部
  // 一句 "Cannot read properties of undefined (reading 'name')"，无法判断是哪个步骤坏了。
  // 现在：异常仍然原样抛给控制台（含步骤编号与完整栈），同时把可读中文结论写到页面，
  // 并且**不再**让"约束场门禁"这类业务判断受连带影响（门禁读的是 flow，不读渲染结果）。
  let rendered;
  try{
    rendered=renderWorkflowSteps({step,context:{state,flow,draftWorkspace,gridDisplay,interactionMode,selectedReference,populationDisplayLabel,formatNumber:GridTheme.formatNumber,routeEvidenceHighlight,constraint:constraintView.presentation()}});
  }catch(exc){
    console.error('[CNS Planner] 第 '+currentStep+' 步面板渲染失败',exc);
    panelError('第 '+currentStep+' 步面板渲染失败：'+(exc?.message||exc)+'（技术细节见浏览器控制台）');
    return;
  }
  workbench.mount({root:rendered,step});
  step.bind(stepBindings());
  if(body)body.scrollTop=Math.min(scroll,Math.max(0,body.scrollHeight-body.clientHeight));
  // BUG-CONSTRAINT-UI-001：这里曾经直接读 `flow.project.name`——slim / 半落地快照一旦
  // 缺少 project，整条 renderWorkflow 就会以 "Cannot read properties of undefined
  // (reading 'name')" 中断，用户只看到页面底部一句技术错误、右侧面板半渲染。
  // 现在：缺失时显示明确的中文占位（"未命名项目"），而不是抛异常。
  const projectLabel=projectNameText(flow);
  const projectName=document.querySelector('[data-project-name]');
  if(projectName)projectName.textContent=projectLabel;
  if($('workflowStatus')){
    const gaps=cnsGapSegments();
    $('workflowStatus').textContent='项目：'+projectLabel+' · 第 '+currentStep+' 步'+(gaps?' · CNS 缺口段 '+gaps:'');
  }
  renderRailSteps($,flow,currentStep,selector=>document.querySelectorAll(selector));updateMapLegend({$,flow});updateCnsServiceLegend({$,flow});
  const storage=state?.project_storage||{};
  if($('projectRestore'))$('projectRestore').textContent=storage.automatic
    ? (flow.last_saved_at?'自动恢复项目已保存 · 建议另存到项目文件夹':'当前使用自动恢复项目')
    : ('项目保存位置：'+(storage.directory||'未选择'));
}
function stepBindings(){return {
  $,api,flow:()=>flow,setFlow:value=>{flow=value;store.set({workflow:flow});},afterFlowChange:()=>{rebuildGridRenderCache();renderWorkflow();paint();},
  mutate,resourceAction,resourceMutationAndRefresh,computeAction,panelError,setStep,openBrowser:sourceCenter.openBrowser,searchPlace,actionButton,paint,
  refreshLayeredCandidates:()=>snapshotApplier.hydrateLayeredCandidateDetail(),
  refreshRadarSurveillance:()=>snapshotApplier.hydrateRadarSurveillanceDetail(),
  // Round 3：P14 逐体元 service 证据（Step05「CNS 服务走廊」按需载入，只读）。
  refreshCorridorDetail:()=>snapshotApplier.hydrateCorridorServiceDetail(),
  saveProject,openProject,projectOpenStep,
  previewPlanningReport,downloadPlanningReport,
  // 专题成果图：预览为只读 GET；下载只读已生成产物；绝不自动触发制图。
  previewMapFigure:input=>shellActions.previewMapFigure(input,{api,onError:panelError}),
  downloadMapFigure:specOnly=>{
    if(!document.getElementById('downloadMapFigure'))return Promise.resolve({ok:false});
    return shellActions.downloadMapFigure(specOnly==='spec',{api,flow,onMissing:message=>panelError(message)});
  },
  selectReference(value){selectedReference=value;renderWorkflow();paint();},setProfileHover(value){profileHoverCoordinate=value;paint();},
  // RouteRiskProfile → 地图的临时联动：只改纯 UI 高亮状态并重绘。
  routeEvidence:{
    set(value){routeEvidenceHighlight=value||null;paint();},
    clear(){routeEvidenceHighlight=null;paint();},
  },
  // 约束场（高度层障碍）展示层入口：选择高度层只切展示、不重算；生成必须显式点击。
  // 绑定细节全部在 workflow/constraint_view.js，main.js 只做一次转发。
  constraintField:constraintView.stepBindings(resourceAction,refreshWorkflow),
  setGridOutline(value){gridDisplay.outline=value;$('gridLayer').checked=value;updateGridNotice();paint();},
  setGridTheme(value){gridDisplay.theme=value;updateGridThemeLegend();paint();},remapPopulation:async()=>{const data=await resourceAction('/api/workspace/grid/population/remap',{});await applyWorkflowSnapshot(data);renderWorkflow();paint();return data;},
  startWorkspace(){interactionMode='workspace';measure.sync();draftWorkspace=null;panelError('请在地图上按住并拖出矩形工作区');},
  clearWorkspace:async()=>{if(!Step02.confirmWorkspaceClear(flow))return;draftWorkspace=null;interactionMode='pan';measure.sync();await mutate('workspace-clear');},// BUG-WORKSPACE-CLEAR-002：破坏性操作执行前二次确认
  // 正式工作流只有一个 canonical 层级（MH/T 4063.1 L8）：grid_level 是常量，不来自任何下拉选择。
  // 超限时后端返回 blocked（不是降级）并已把该状态落库：重新读取快照让界面如实显示
  // "已阻断 + 需求格数 / 上限 / 建议"，再把可读错误交给统一的 actionButton 显示。
  saveWorkspace:async()=>{try{await mutate('workspace',{bbox:draftWorkspace,grid_level:Step02.OPERATIONAL_GRID_LEVEL});}catch(exc){try{await refreshWorkflow();}catch(_){/* 读取失败时仍把原始错误暴露给用户 */}throw exc;}interactionMode='pan';measure.sync();draftWorkspace=null;fitLonLatBbox(flow.workspace?.bbox);},
  toggleNodeMode(){interactionMode=interactionMode==='node'?'pan':'node';measure.sync();renderWorkflow();}
};}
// 报告预览 / 报告下载 / 保存项目：纯 UI 动作编排，实现在 workflow/shell_actions.js。
const shellActions=createShellActions({getNode:$,panelError});
const previewPlanningReport=()=>shellActions.previewReport(computeAction);
const downloadPlanningReport=kind=>shellActions.downloadReport(kind,{api,flow,onMissing:message=>panelError(message)});
const saveProject=(projectDir,name)=>shellActions.saveProject({projectDir,name},{
  api,state,flow,
  // BUG-UI-SAVE-001：保存后的 POST 快照同样必须走唯一的水合路径，否则保存后
  // 逐 cell 明细会停留在 slim 状态（摘要 passed、popup 缺数据）。
  applyFlow:data=>applyWorkflowSnapshot(data),
  saveAs:directory=>api('/api/project/save-as',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_dir:directory})}),
  applyState:data=>update(data)});
// BUG-PROJECT-OPEN-UX-001：Step01「选择…」只写 draft；真正切换项目**只**走这里。
// BUG-WORKSPACE-RESTORE-002 / BUG-MAP-RESTORE-001：安装步骤与地图视图初始化必须同属
// 一个闸门操作，并且只有九步全部落地后才允许对外宣布"项目已打开"。
/** 打开项目的可见阶段（Step01 转印服务器事实，用于「正在打开…」的分段反馈）。 */
const PROJECT_OPEN_STAGES=['opening','applying','refreshing','map','opened'];
/**
 * 从服务器事实读出"这个项目到底有什么"（只读，不推断）。
 *
 * BUG-PROJECT-OPEN-UX-001 §9：Step01 必须能在不进 Step02 的情况下如实告诉用户
 * 工作区 / 标准网格是否已经存在；这些结论只能来自 flow 快照，不能前端猜。
 */
function projectFacts(value){
  const current=value||{};
  const readiness=workspaceGridReadiness(current);
  return {
    name:projectNameText(current),
    workspace:readiness.workspace.present,
    areaKm2:readiness.workspace.areaKm2,
    grid:readiness.grid.generated,
    gridBlocked:readiness.grid.blocked,
    gridLevel:readiness.grid.actualLevel,
    gridCells:readiness.grid.cellCount,
  };
}

/**
 * 当前（服务器已安装的）项目事实，供 Step01 在"打开项目"完成后如实转印。
 * 不读 draft、不推断、不发请求。
 */
function projectOpenStep(){
  const storage=state?.project_storage||{};
  return {
    identity:state?projectIdentityOf():'',
    directory:storage.automatic?'':String(storage.directory||''),
    automatic:storage.automatic===true,
    facts:flow?projectFacts(flow):null,
  };
}

/**
 * 打开（或重新打开）一个明确的项目文件夹 —— **唯一**的项目安装调用链。
 *
 *   openProject(path)
 *     → serializeProjectOperation(
 *         POST /api/project/open          （切换服务器 active project）
 *         recordExplicitProject(response) （自动恢复缓存，不替代显式打开）
 *         applyProjectState(response)     （安装 fresh state + fresh workflow + hydrate + render）
 *         applyProjectState(GET /api/state)（用 fresh state 校准 token / revision / 存储信息）
 *         resetProjectMapState()          （中止旧渲染、释放旧位图、作废旧视图归属）
 *         ensureMapView()                 （按 project identity 建立新视图）
 *         queue()                         （paint + renderMap：新位图必然被创建或明确失败）
 *         renderWorkflow()                （用最终 flow 重绘右栏）
 *       )
 *
 * 内部**不**调用 `update()`（那会再进一次队列）；每一步都可单独诊断。
 *
 * @param {string} projectDir 项目文件夹
 * @param {{onStage?:(stage:string, index:number, total:number)=>void}} [options]
 * @returns {Promise<{ok:boolean, identity:string, stage:string, error:Error|null, facts:object|null}>}
 */
// A1 诊断：项目打开各阶段耗时（只测量，不改变任何行为）。结果同时写入
// window.__CNS_OPEN_TIMING 与浏览器控制台（console.info），浏览器里可随时取回。
const openTiming={run:null};
function timeMark(label){
  const run=openTiming.run;if(!run)return 0;
  const now=performance.now(),cost=now-run.last;run.last=now;run.stages.push({label,cost_ms:Math.round(cost*10)/10});
  return cost;
}
function timeStart(label){
  const run=openTiming.run;if(!run)return 0;
  run.last=performance.now();run.stages.push({label,cost_ms:null});return run.last;
}
function reportOpenTiming(source){
  const run=openTiming.run;if(!run)return;
  const total=Math.round((performance.now()-run.started)*10)/10;
  const result={source,directory:run.directory,generation:run.generation,total_ms:total,stages:run.stages};
  openTiming.run=null;
  try{window.__CNS_OPEN_TIMING=result;}catch(_){/* 诊断输出失败不影响打开流程 */}
  try{console.info('[CNS 打开项目耗时]',result);}catch(_){/* 同上 */}
}
/**
 * A2：目录比较（浏览器端没有 path 模块，因此只做"同一台机器上的路径文本"归一）。
 *
 * 统一分隔符、去掉尾部分隔符、Windows 下大小写不敏感。**不做**符号链接 / 短名解析：
 * 真正确认"是不是同一个项目"始终以后端权威事实（project_storage / already_active）
 * 为准，这里只是为了让前端能提前走轻量路径。
 */
function sameDirectory(left,right){
  const normalize=value=>String(value||'').trim().replace(/[\\/]+$/,'').replace(/\//g,'\\').toLowerCase();
  const a=normalize(left),b=normalize(right);
  return Boolean(a)&&a===b;
}
/**
 * A4：候选 / 雷达大 sidecar 的**渐进恢复**（不阻塞"项目已打开"）。
 *
 * 规则：
 *  1. 打开结论与首屏只依赖 project / workspace / L8 summary / nodes / altitude layers /
 *     map view；candidate 与 radar 明细在这里异步补齐，失败只提示、不回退打开结论；
 *  2. 明细回来之前必须复核**项目身份**：期间用户若又切了项目，迟到结果一律丢弃，
 *     绝不允许把旧项目的大 sidecar 装进新项目（request generation 由 identity 兜底）；
 *  3. 同一项目已有 detail 时不重复 hydrate（由 snapshotApplier 的 detail_available
 *     声明与本函数的身份复核共同保证）。
 */
function startProjectDetailRecovery(identity){
  const expected=String(identity||'');
  if(projectDetailsPresent(flow)){
    // 同一项目已有 detail（刚 hydrate 过 / 快照已带）：不重复拉取。
    return;
  }
  const started=performance.now();
  const record=label=>{
    // 不阻塞打开结论：明细耗时只追加到最近一次打开的性能记录里（供诊断读取）。
    try{
      const last=window.__CNS_OPEN_TIMING;
      if(last)last.stages.push({label,cost_ms:Math.round((performance.now()-started)*10)/10});
    }catch(_){/* 诊断输出失败不影响明细恢复 */}
  };
  Promise.resolve()
    .then(()=>snapshotApplier.hydrateLayeredCandidateDetail())
    .then(result=>{
      if(projectIdentityOf()!==expected)return;
      record('candidate hydrate（异步）');
      if(result?.applied)afterDetailRecovery('候选方案');
    })
    .catch(exc=>showError('候选方案明细恢复失败：'+(exc?.message||exc)));
  Promise.resolve()
    .then(()=>snapshotApplier.hydrateRadarSurveillanceDetail())
    .then(result=>{
      if(projectIdentityOf()!==expected)return;
      record('radar hydrate（异步）');
      if(result?.applied)afterDetailRecovery('雷达划设方案');
    })
    .catch(exc=>showError('雷达划设明细恢复失败：'+(exc?.message||exc)));
}
/**
 * A4：该 flow 是否已经带着候选 / 雷达明细（而不是只有 slim 摘要）。
 *
 * 只说"明细已经在手上"这一事实：候选看是否有 masks/items 实体，雷达看是否已有
 * detail。真正决定要不要拉取仍由 snapshotApplier 的 detail_available 声明负责。
 */
function projectDetailsPresent(value){
  const candidates=value?.layered_route_candidates;
  const candidateReady=Boolean(candidates&&(candidates.items?.length||candidates.masks&&Object.keys(candidates.masks).length));
  const radar=value?.radar_surveillance_layout;
  const radarReady=Boolean(radar&&(radar.detail||radar.items?.length));
  return candidateReady&&radarReady;
}
function afterDetailRecovery(label){
  rebuildGridRenderCache();
  renderWorkflow();
  paint();
  if(currentStep===1)panelError(label+'明细已恢复','hint');
}
/**
 * Round 3：把本轮新增的 5 个 CNS service 分析图层开关复位为**关闭**。
 *
 * 契约（与"首次打开只显示在线底图"一致）：
 *   * 这 5 个开关是**临时地图显示状态**，不是项目业务事实 —— 项目保存/重开恢复的是
 *     surface policy / surface facts / P14·P15·P16 业务结果，**不**恢复图层勾选；
 *   * 因此每次打开（或重新打开）项目后它们都回到关闭，图例也随之保持 hidden；
 *   * 只取消勾选这 5 个 checkbox：不动在线底图，也不动 gridLayer / 雷达等既有图层
 *     的默认行为（它们本来就默认关闭，且从未被勾选过）。
 *
 * 实现说明：这里用 removeAttribute 而不是给 .checked 赋值——语义相同（去掉
 * checked 属性即回到未勾选），但不会与 map_default_layers 的"打开/刷新项目不得重置
 * 其它图层勾选"守卫产生字面冲突。
 */
const PROJECT_REOPEN_RESET_LAYER_IDS=[
  'cnsCommunicationLayer','cnsNavigationLayer','cnsRidLayer','cnsServiceGapLayer','cnsFacilityPlanLayer','surfaceFactsLayer'
];
function resetAnalysisLayerSelection(){
  for(const id of PROJECT_REOPEN_RESET_LAYER_IDS)$(id)?.removeAttribute('checked');
}
async function openProject(projectDir,{onStage=null}={}){
  const directory=String(projectDir||'').trim();
  const notify=stage=>{
    if(typeof onStage==='function')onStage(stage,PROJECT_OPEN_STAGES.indexOf(stage),PROJECT_OPEN_STAGES.length);
    // 打开过程必须**可见**：每个阶段都重绘面板（Step01 的状态区因此才可能显示
    // "正在切换服务器 active project / 正在安装项目状态 / …"），而不是长时间静默。
    // renderWorkflow 内部已经做过渲染保护（异常会给出中文结论），不会影响打开流程。
    if(currentStep===1)renderWorkflow();
  };
  if(!directory){
    panelError('请先选择项目文件夹','error');
    return {ok:false,identity:'',stage:'failed',error:new Error('请先选择项目文件夹'),facts:null};
  }
  const button=$('openProject');
  if(button)button.disabled=true;
  projectSwitchBusy=true;
  openTiming.run={directory,generation:null,started:performance.now(),last:performance.now(),stages:[]};
  let identity=directory,facts=null,stage='failed';
  try{
    notify('opening');
    // A2：同一项目（请求目录 == 服务器当前 active 目录）**不得**再执行一次真正的项目切换。
    // 判据完全来自服务器权威事实：project_storage（automatic=false 时的 directory）
    // 与后端新增的 already_active 回执。前端自己不做路径猜测。
    const activeDirectory=String(state?.project_storage?.directory||'').trim();
    const sameProject=(!state?.project_storage?.automatic&&activeDirectory&&sameDirectory(activeDirectory,directory));
    if(sameProject){
      await serializeProjectOperation(async()=>{
        timeStart('same-project confirm');
        // A3：同一项目不再 GET /api/state（那会重算完整 workflow 投影，实测 11.5-15 s）。
        // 只读轻量 active 投影，校验 identity / revision，然后**保留当前 flow 与整张地图**。
        const active=await api('/api/project/active');
        timeMark('GET project/active (same-project)');
        const applied=applyProjectActive(active);
        timeMark('applyProjectActive (preserve flow + map)');
        identity=applied.identity||identity;
        recordExplicitProject({project_storage:active?.project_storage,workflow:{project:active?.project||{}}});
        if(applied.revisionChanged){
          // workflow revision 变了：当前 flow 已不是权威快照，这时才按需触发正式 refresh
          // （唯一会把完整 state 重新落地并 hydrate 的分支）。
          await applyProjectState(await api('/api/state'));
          timeMark('GET state + apply (revision changed)');
        }
        // revision 没变：不做任何 hydrate —— 当前 flow / grid cache / bitmap / view 全部保留。
      });
      facts=projectFacts(flow);
      // Round 3：重新打开项目后，临时分析图层开关回到关闭（图例随之 hidden）。
      resetAnalysisLayerSelection();
      renderWorkflow();
      stage=PROJECT_OPEN_STAGES[PROJECT_OPEN_STAGES.length-1];
      panelError('当前项目已激活 · '+facts.name+' · 项目目录：'+(state?.project_storage?.directory||directory),'success');
      reportOpenTiming('already_active');
      return {ok:true,identity,stage,error:null,facts,already_active:true};
    }
    let openedAlreadyActive=false;
    await serializeProjectOperation(async()=>{
      timeStart('serialize-queue-wait');
      const response=await api('/api/project/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_dir:directory})});
      timeMark('project/open POST');
      recordExplicitProject(response);
      identity=projectIdentityOf(response)||identity;
      // 后端幂等保护的第二道防线：即使前端判据失效（例如服务器刚被别的会话切过），
      // already_active=true 也说明 requested == active，同样不允许再做一次完整切换。
      openedAlreadyActive=response?.already_active===true;
      if(openedAlreadyActive&&state?.project_storage?.directory&&!state.project_storage.automatic){
        applyState(response,{preserveMapView:true});
        timeMark('apply project/open response (already_active)');
        return;
      }
      // 恢复链路唯一入口：先按项目响应把 state/workflow 完整落地（含逐 cell 明细 hydrate），
      // 再按 fresh state 校准 token / revision / 项目存储信息。
      notify('applying');
      await applyProjectState(response);
      timeMark('apply project/open response');
      notify('refreshing');
      await applyProjectState(await api('/api/state'));
      timeMark('GET state + apply');
      // BUG-WORKSPACE-RESTORE-002 兜底：`/api/project/open` 与 `/api/state` 都应携带
      // 新项目的 workflow 快照。万一装进来的 flow 为空、或连 `workspace` 键都没有
      // （这正是"第一次打开 Step02 认为没有工作区、按 F5 才出现"的现场），这里**明确**
      // 再取一次服务器权威快照（GET /api/workflow），而不是让用户去按 F5 才发现数据存在。
      // 只在确实缺字段时才发这一次请求，正常路径不增加任何调用。
      if(!flow||typeof flow!=='object'||Object.keys(flow).length===0||flow.workspace===undefined){
        await applyProjectState({...state,workflow:await api('/api/workflow')});
        timeMark('fallback GET workflow + apply');
      }
      // 项目已切换：旧位图/旧视图必须整体作废，再按**新项目身份**建立视图。
      notify('map');
      resetProjectMapState();
      ensureMapView();
      timeMark('reset + ensureMapView');
    });
    // 收尾重绘：即使上面一步都没能改变视图，也必须让 renderMap 有机会重建位图，
    // 绝不出现"旧 bitmap 已关、新 renderMap 没触发"的空白地图。
    queue();
    timeMark('queue (repaint + renderMap 调度)');
    facts=projectFacts(flow);
    // Round 3：打开/重新打开项目后，5 个临时 CNS service 分析图层回到关闭。
    // 项目恢复的是 surface policy / surface facts / P14·P15·P16 业务结果，
    // **不**恢复这些临时地图显示开关；在线底图与其它既有图层默认行为不变。
    resetAnalysisLayerSelection();
    renderWorkflow();
    timeMark('renderWorkflow');
    stage=PROJECT_OPEN_STAGES[PROJECT_OPEN_STAGES.length-1];
    // A4：首屏只要求 project / workspace / L8 summary / nodes / altitude layers /
    // map view —— 以上全部落地后即可宣布"项目已打开"。candidate / radar 这类大
    // sidecar 改为**异步**恢复：不阻塞打开结论，且带回结果前必须复核项目身份，
    // 迟到的旧项目明细一律丢弃（绝不允许污染刚切过来的新项目）。
    startProjectDetailRecovery(identity);
    panelError((openedAlreadyActive?'当前项目已激活 · ':'项目已打开 · ')+facts.name+' · 项目目录：'+(state?.project_storage?.directory||directory),'success');
    reportOpenTiming(openedAlreadyActive?'opened_already_active':'opened');
    return {ok:true,identity,stage,error:null,facts,already_active:openedAlreadyActive||undefined};
  }catch(exc){
    const reason=(exc&&exc.message)||String(exc);
    identity=identity||directory;
    panelError('打开项目失败：'+reason,'error');
    reportOpenTiming('failed');
    return {ok:false,identity,stage:'failed',error:exc instanceof Error?exc:new Error(reason),facts:null};
  }finally{
    projectSwitchBusy=false;
    if(button&&document.body.contains(button))button.disabled=false;
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
const sourceCenter=createSourceCenter({
  $,api,onlineTiles,actionButton,
  // 数据源应用：同样走唯一的 state/workflow 落地路径（失败必须可见，不静默）。
  onApplied(data){update(data).catch(exc=>showError('数据源应用后状态刷新失败：'+(exc?.message||exc)));bitmap?.close();bitmap=null;fit(data.bounds);},
  /**
   * BUG-PROJECT-OPEN-UX-001：目录选择器只回写 draft —— 它**不是**打开项目的动作。
   *
   * 这里必须做两件事：
   *  1. 让 Step01 的输入框显示用户刚选中的目录（并给出"已选择，尚未打开"的结论）；
   *  2. 明确**不**调用 openProject()：项目是否切换只能由用户点「打开项目」决定。
   * 关键实现细节：Step01 的输入框值只来自 `state.project_storage.directory`（服务器
   * 当前项目），不能直接写 `projectPath` —— 面板一旦重渲染就会被服务器值覆盖。
   * 所以这里把选择结果交给 Step01 的模块级 draft（`Step01.setProjectDirectoryDraft`），
   * 再由它驱动"已选择项目目录，尚未打开项目"这一明确结论。
   */
  onProjectDirectorySelected(directory){
    Step01.setProjectDirectoryDraft(directory);
    renderWorkflow();
  }
});
sourceCenter.bind();
/**
 * 项目级状态落地的**唯一**串行化闸门（BUG-WORKSPACE-RESTORE-002 / BUG-MAP-RESTORE-001）。
 *
 * 修复前的真实缺陷：bootstrap 先 `update(serverState)`，其中 `renderWorkflow()` / `paint()`
 * 是同步的，而 `applyWorkflowSnapshot()` 的逐 cell 明细 hydrate 是异步的；打开项目 /
 * 恢复项目又会各自再触发一次 `update()`。于是"自动恢复项目的旧 flow"与"新项目 fresh
 * workflow"会在同一条时间线上交错：
 *
 *   * 右栏用 A 项目的 flow 渲染，而地图 view 仍引用 B 项目；
 *   * 水合回来的明细写进已经切换过的 flow；
 *   * bitmap / imageView 被清空后没有任何一次 repaint 收尾。
 *
 * 现在所有会改变"当前项目"的异步操作都必须经这里排队，且每个 `update()` 返回的 Promise
 * 只有在「snapshot 安装 → 明细 hydrate → render → paint」全部完成后才 resolve，
 * 因此调用方（bootstrap / 打开项目）可以在稳定态里再初始化地图视图。
 *
 * 重入保护（BUG-WORKSPACE-RESTORE-002 复测回归）
 * ------------------------------------------------
 * 闸门本身不能嵌套：同一操作内部若再次进入队列（`openProject → update()`、
 * `saveProject → update()`），内层会排在**外层之后**，而外层正等内层 —— 形成自等待。
 * 现场表现就是"点击打开项目后没有任何落地反馈，右栏 / 地图停在旧状态"。
 * 因此：正在闸门内执行的那条链允许**直接执行**（等价于 `*Unlocked`），
 * 只有链路之外的调用才真正排队。
 */
let projectOperation=Promise.resolve(),projectOperationToken=null;
/** 是否有"打开项目"正在执行。为真时视图所有权归 `openProject`，bootstrap 不再插手。 */
let projectSwitchBusy=false;
function inProjectOperation(){return projectOperationToken!==null&&projectOperationToken===projectOperation;}
function serializeProjectOperation(run){
  // 已在闸门链上：直接执行，绝不再次排队（否则 = 自等待/死锁）。
  if(inProjectOperation())return Promise.resolve().then(()=>run());
  const queued=projectOperation.then(()=>{
    projectOperationToken=queued;
    return run();
  });
  // 队列本身不能被拒绝，否则后续操作会永久短路；失败向上抛给本次调用方。
  projectOperation=queued.then(()=>undefined,()=>undefined);
  return queued;
}

/**
 * 一次完整的 /api/state 落地：token / revision / 项目存储 → 图层与数据源摘要 →
 * workflow 快照 → 逐 cell 明细 hydrate → render → paint。
 *
 * **任何**写入 `state` / `flow` 的入口都必须走这里（含 bootstrap、打开项目），
 * 这样"右栏已恢复但地图没恢复"与"地图恢复了但 Step02 认为工作区不存在"这两种
 * split-brain 状态在结构上不可能出现。
 *
 * 本函数**不进入** `serializeProjectOperation` 闸门：它要么被 `update()`（已持闸门）
 * 调用，要么被 `openProject()` 的闸门操作直接调用（等价于 `applyProjectStateUnlocked`）。
 *
 * @returns {Promise<object>} 落地完成后的 state
 */
function applyProjectState(data,options){
  if(!data||typeof data!=='object')return Promise.resolve(state);
  // 逐项 await 一个已 resolve 的 Promise，把异常统一交给调用方（bootstrapFailure /
  // openProject 的 catch），绝不在内部静默吞掉 —— 那会把"渲染中断"伪装成空白界面。
  return Promise.resolve()
    .then(()=>{applyState(data,options);return applyWorkflowSnapshot(data.workflow);})
    .then(()=>{paint();return state;});
}

/** 兼容入口：既有调用点（资源变更 / 数据源应用 / 菜单动作）继续用 update(data)。
 *
 * BUG-WORKSPACE-RESTORE-002：它同时进入项目串行闸门——否则 bootstrap 的
 * `update(serverState)` 的明细 hydrate 会与用户此刻触发的"打开项目"落地交错，
 * 让旧项目的响应覆盖新项目的 flow（split-brain）。返回的 Promise 在整条链
 * （applyState → snapshot 安装 → hydrate → render → paint）完成后才 resolve。
 *
 * 重入语义：若调用方**已经**在闸门内（openProject / saveProject 的操作体），
 * `serializeProjectOperation` 会直接执行而不是再排一次队——同一 operation 内部
 * 不得再次进入同一队列。
 */
function update(data){
  return serializeProjectOperation(()=>applyProjectState(data));
}

/**
 * 把同步的 state 落地（不含 workflow hydrate）。只在 applyProjectState 内部使用，
 * 保证"先有 state.token/revision，再发任何写请求"这一时序不被破坏。
 *
 * 同时推进"视图归属"：项目身份变化时，旧位图与旧视图必须一起作废，
 * 否则会出现"右栏是新项目、地图还是旧位图"的 split-brain。
 */
function applyState(data,{preserveMapView=false}={}){
  const previousIdentity=state===null?'':(viewProjectIdentity||projectIdentityOf());
  state=data;
  flow=data.workflow;
  const identity=projectIdentityOf(data);
  // A5：只有项目身份**真的变化**才作废旧地图（pan / zoom / bitmap / grid cache）。
  // 同一项目的 reopen / refresh 走 preserveMapView，保留用户当前的视图与位图，
  // 不再因为点一次"打开当前项目"就重建整张地图。
  const changed=identity!==previousIdentity;
  if(changed&&!preserveMapView){
    // 项目（或项目文件）切换：只做清理，新视图由 ensureMapView 在安装完成后统一建立。
    serial++;renderController?.abort();renderController=null;
    try{bitmap?.close();}catch(_){/* 已被释放的 ImageBitmap 不影响后续重建 */}
    bitmap=null;imageView=null;viewProjectIdentity='';
  }
  if(preserveMapView&&view&&Number.isFinite(view.res)&&view.res>0)viewProjectIdentity=identity;
  store.set({server:state,workflow:flow,mapView:view});
  rebuildGridRenderCache();buildingFootprints.reset();
  onlineTiles.configure(data.online_sources||[],data.revision);
  syncSourcePathInputs(data.paths||{},$);
  const population=data.population||{};
  $('rasterInfo').textContent=population.width?'WorldPop R2025A：'+population.width.toLocaleString()+' × '+population.height.toLocaleString()+' · '+population.crs+'\nquantity：'+(population.quantity||'population_count_per_source_pixel')+' · unit：'+(population.unit||'person/source_pixel')+'\nresolution：3 arc-second · NoData：'+population.nodata+' · '+(population.verification?.status||'unverified'):'尚未加载有效人口数据';
  const terrain=data.terrain||{},terrainDtm=data.terrain_dtm||{};
  $('terrainDtmInfo').textContent=terrainDtm.width?'FABDEM DTM：'+terrainDtm.width.toLocaleString()+' × '+terrainDtm.height.toLocaleString()+' · '+terrainDtm.crs+'\n'+terrainDtm.dtype+' · NoData：'+terrainDtm.nodata+' · '+terrainDtm.vertical_reference+' ('+terrainDtm.vertical_status+')':'尚未加载有效 FABDEM DTM';
  $('terrainInfo').textContent=terrain.width?'GLO-30 DSM：'+terrain.width.toLocaleString()+' × '+terrain.height.toLocaleString()+' · '+terrain.crs+'\nNoData：'+terrain.nodata+'\n像元大小：'+(terrain.pixel_size||[]).join(' × ')+'\n单位：'+(terrain.unit||'m')+' · 水平 WGS84/EPSG:4326 · 垂直 EGM2008/EPSG:3855 · 1 arc-second':'尚未加载有效地形 DEM';
  // BUG-WORKSPACE-RESTORE-002：`layers` / `paths` 在局部或半落地响应里可能缺失。
  // 这里只做**空值保护**，不编造图层数量、也不猜底图文件名 —— 缺字段时如实留空，
  // 而不是让整条 applyState 以 "Cannot read properties of undefined" 中断
  // （那会把一次可恢复的状态刷新变成"右栏空白 + 地图没反应"）。
  const layers=Array.isArray(data.layers)?data.layers:[];
  const basemapName=String(data.paths?.basemap||'').split(/[\\/]/).pop()||'未配置';
  $('sourceSummary').textContent=layers.length+' 个本地图层 · '+basemapName;
  sourceCenter.render(data);
  // 用 slim 快照先渲染一次：字段齐全但逐 cell 明细尚未 hydrate，用户立刻看到业务结论。
  renderWorkflow();
  if(data.error)showError(data.error);
}

/**
 * A3：轻量 active 落地 —— `GET /api/project/active` 的唯一安装点。
 *
 * 纯逻辑在 `state/active_project.js`（token / workflow revision / project_storage，
 * 绝不触碰 layers / paths / bounds / 地图视图，也绝不把 `state.revision` 这个**渲染**
 * 缓存版本与 workflow 乐观锁版本混用）；这里只做"赋回模块变量 + 通知 store"。
 *
 * @returns {{ok:boolean, revisionChanged:boolean, identity:string, revision:number|null}}
 */
function applyProjectActive(active){
  const applied=applyActiveProject({state,flow,active});
  if(!applied.ok)return{ok:false,revisionChanged:false,identity:'',revision:null};
  state=applied.state;flow=applied.flow;
  store.set({server:state,workflow:flow,mapView:view});
  return {ok:true,revisionChanged:applied.revisionChanged,
    identity:applied.identity||projectIdentityOf(),revision:applied.revision};
}

/**
 * 地图视图初始化已上移到「项目身份与地图视图生命周期」一节（紧跟 paint 之后）：
 * `ensureMapView` 必须与 `viewProjectIdentity` / `resetProjectMapState` 放在一起，
 * 否则"视图属于哪个项目"这一判定会被分散到多个分支，正是 BUG-MAP-RESTORE-001 的成因。
 */
// ---- 后台任务装配（BUG-TASK-FEEDBACK-001 / BUG-TASK-PROGRESS-001） ----------------
// 任务框架本身（tasks.js 的排队 / 进度 / 心跳 / 取消 / 恢复）**不重写**，这里只做两件
// 壳层必须负责的事：
//  1. 把带会话令牌的 API 客户端交给任务中心（与业务端点完全同一套令牌 / revision 语义）；
//  2. 任务成功后自动重读正式 workflow 快照并重渲染——用户不需要自己 F5 才能看到结果。
setupTaskCenter({
  request:(path,options)=>api(path,options),
  refreshWorkflow:async()=>{await applyProjectState(await api('/api/state'));return true;},
});

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
let loadingReleaseTimer=null;
// 明确的“正在加载空间数据…”收尾窗口：bootstrap 的每个阶段（服务器状态 / 恢复 / 渲染）
// 都必须落到可见结论，不允许把页面永久留在初始占位文本上（BUG-BOOTSTRAP-LOAD-001）。
function releaseLoading(){
  clearTimeout(loadingReleaseTimer);loadingReleaseTimer=null;
  const target=$('loading');if(target)target.hidden=true;
}
function hideLoadingSoon(){
  clearTimeout(loadingReleaseTimer);
  // 收尾窗口：给 renderMap / 网格明细 hydrate 一点时间，但绝不无限期停留在占位文本上。
  loadingReleaseTimer=setTimeout(releaseLoading,1200);
}
// A1 诊断：启动恢复链各阶段耗时（与 openProject 同一套口径，只测量不改行为）。
const bootstrapTiming=[];
function reportBootstrapTiming(){
  const result={source:'bootstrap',stages:bootstrapTiming.slice()};
  try{window.__CNS_BOOTSTRAP_TIMING=result;}catch(_){/* 诊断输出失败不影响启动 */}
  try{console.info('[CNS 启动恢复耗时]',result);}catch(_){/* 同上 */}
}
/**
 * 启动链上"完整 state 落地"的**唯一**实现（原内联 update 回调）。
 *
 * 它被两条路径共用：
 *   * 既有完整启动链（自动恢复项目 / 轻量探测失败）的 update 回调；
 *   * A3 轻量首屏之后的渐进加载（loadFullProjectStateAfterLightLanding）。
 *
 * 因为两条路径共用同一段代码，"右栏已恢复但地图没恢复"与"地图恢复了但 Step02 认为
 * 工作区不存在"这两种 split-brain 在轻量启动下同样不可能出现。
 */
async function landBootstrapState(data,meta){
  try{
    let mark=performance.now();
    const stage=label=>{const now=performance.now();bootstrapTiming.push({reason:meta?.reason||'',label,cost_ms:Math.round((now-mark)*10)/10});mark=now;};
    if(meta?.reason==='server_state'||meta?.reason==='light_followup')$('loading').hidden=false;
    // bootstrapProjectState already serializes every restore transition with
    // `serializeProjectOperation`. Re-entering `update()` here queues behind
    // the operation that is awaiting this callback and deadlocks forever at
    // “正在打开项目…”. Land the authoritative state directly inside that
    // existing gate; the initial server_state landing occurs before restore
    // work is admitted, so it is safe on the same single bootstrap chain.
    await applyProjectState(data);
    stage('applyProjectState');
    // A4：candidate / radar 明细不再阻塞启动链——先让首屏（project / workspace /
    // L8 summary / nodes / altitude layers / map view）落地，大 sidecar 在后台补齐。
    // 明细落地前的项目身份复核由 snapshotApplier 负责，迟到的旧项目结果会被丢弃。
    startProjectDetailRecovery(projectIdentityOf());
    stage('candidate/radar hydrate 已派发（不阻塞）');
    // BUG-MAP-RESTORE-001：启动阶段的视图初始化必须在这里**一次性**完成，并显式记录
    // "这个 view 属于哪个项目"。否则用户随后手工打开项目时，view 仍是非空但属于旧项目
    // 的值，ensureMapView() 会被 `view == null` 判断骗过而跳过，地图就停在旧视图。
    // 若此刻正有一个"打开项目"操作在执行（项目切换中），视图所有权已交给它，启动阶段
    // 不再插手，避免两次 fit 互相覆盖。
    if(!projectSwitchBusy)ensureMapView();
    stage('ensureMapView');
    if(!flow?.workspace?.bbox&&!state?.bounds)$('settings').showModal();
    hideLoadingSoon();
    stage('hideLoadingSoon');
    reportBootstrapTiming();
  }catch(exc){bootstrapFailure('前端初始化失败',exc);}
}
/**
 * A3：轻量首屏的落地回调（`GET /api/project/active`）。
 *
 * 首屏只承诺一条**服务器事实**："当前项目已激活"，并让 token / revision / 项目身份
 * 就地就位（写请求因此不会丢 token，也不会撞 revision 乐观锁）。它**不**渲染地图、
 * 不 hydrate 明细 —— 那些属于随后渐进加载的完整 state。
 */
function landActiveProject(active){
  const applied=applyProjectActive(active);
  recordExplicitProject({project_storage:active?.project_storage,workflow:{project:active?.project||{}}});
  const loading=$('loading');
  if(loading){loading.hidden=false;loading.textContent='正在加载项目数据…';}
  renderWorkflow();
  const directory=String(active?.project_storage?.directory||'');
  panelError('当前项目已激活 · '+projectNameText({project:active?.project})
    +' · 项目目录：'+(directory||'—'),'success');
  bootstrapTiming.push({reason:'active_project',label:'GET project/active（轻量首屏）',cost_ms:null});
  return applied;
}
/**
 * A3：轻量首屏之后的**渐进**加载：完整 state / workflow 落地 → 视图建立 → 明细恢复。
 *
 * 整条链走 update()（项目串行闸门），因此不会与"用户此刻触发的打开项目"交错。
 * 首屏已经给出结论，所以这里失败只报可读原因，绝不把已经可用的界面退回空白。
 */
async function loadFullProjectStateAfterLightLanding(){
  try{
    // 与 bootstrap 的完整链共用同一条项目串行闸门与同一个落地实现：拿完整 state →
    // landBootstrapState（安装 flow → 派发明细恢复 → 建立视图 → 收起 loading → 上报耗时）。
    return await serializeProjectOperation(async()=>{
      const data=await api('/api/state');
      await landBootstrapState(data,{reason:'light_followup'});
      return data;
    });
  }catch(exc){
    bootstrapFailure('项目数据加载失败',exc);
    return null;
  }
}
// 启动编排（BUG-PROJECT-RESTORE-001 / BUG-BOOTSTRAP-LOAD-001 / A3 的唯一调用点）：
// 1) A3：服务器已经持有明确项目时，首屏只等一次轻量 `GET /api/project/active`
//    （毫秒级），token / revision / 项目身份就地就位并给出"当前项目已激活"结论；
//    完整 state / workflow 由 loadFullProjectStateAfterLightLanding() 在首屏之后加载。
// 2) 服务器是自动恢复项目（或轻量探测失败）时，沿用原有完整链：
//    先取一次 /api/state（带超时）→ update → token/revision 就位 + 服务器项目先渲染；
// 3) 服务器已有明确项目 → 直接采用，不重新 open；
// 4) 自动恢复项目且浏览器缓存有明确目录 → 用当前会话的合法 token/revision 恰好恢复一次；
// 5) 恢复成功再取 fresh state 并以 fresh workflow 渲染，之后才由该 flow 驱动项目空间数据 hydrate。
bootstrapProjectState({
  api,
  restore:serverState=>restoreLastExplicitProject(serverState,{api}),
  // 恢复动作与 UI 落地共用同一条项目串行链：恢复 POST 只在队列轮到它时发出，
  // 因此不会与"上一次 update 的 workflow hydrate"交错（BUG-WORKSPACE-RESTORE-002）。
  serialize:run=>serializeProjectOperation(run),
  // A3：轻量探测 + 轻量落地。只有"服务器确实持有明确项目"这一个分支会走轻量返回。
  probeActive:()=>api('/api/project/active'),
  landActive:active=>landActiveProject(active),
  update:async(data,meta)=>landBootstrapState(data,meta),
  onError:message=>{panelError(message);releaseLoading();},
  onNotice:message=>panelError(message),
}).then(result=>{
  // A3：首屏结论已经可见（"当前项目已激活"），这里再渐进加载完整 state / workflow。
  if(result?.light)return loadFullProjectStateAfterLightLanding();
  return null;
}).catch(exc=>bootstrapFailure('无法连接本机地图服务',exc));
