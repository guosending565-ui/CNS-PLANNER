// =========================================================
// 地图显示层（workflow 叠加绘制）
//
// 职责边界：
//  - 只读取 flow 做显示，绝不写入业务状态；
//  - 所有"多大、多粗、显示不显示"的决定来自 map/lod.js；
//  - 点聚合来自 map/point_clustering.js，只是显示逻辑；
//  - 真实 geometry 原样绘制，不做简化、不做偏移。
//
// main.js 只提供视图、投影与图层开关，不再散落固定 marker 与固定线宽。
// =========================================================
import {drawLine} from './renderer.js';
import {drawBuildingClearanceOverlay} from './building_clearance_overlay.js';
import {drawV3CandidateOverlay,v3OverlayModel} from './route_planner_v3_overlay.js';
import {drawLayeredFeasibilityOverlay} from './layered_feasibility_overlay.js';
import {clusterPoints,toGeographic,extentOf,hitCluster} from './point_clustering.js';
import {displayStyle} from './lod.js';

// ---- 样式（颜色只表达来源与状态，不表达通过与否） ----------------------------

const NODE_COLOR={fill:'#ffffff',stroke:'#c25233',label:'#22303c',radius:5.5};
const LANDING_COLOR={normal:'#1a8677',duplicate:'#8b6b2f',cluster:'#0f6f62'};
const CNS_COLORS={existing:'#7256a1',candidate:'#e07a26',unusable:'#8b949e',planned:'#d12f8a',hover:'#111111',cluster:'#5c6d7d'};
const GAP_COLORS={C:'#d83b35',N:'#c26b16',S:'#9b3eb5'};
const SUBSYSTEM_COLORS={C:'#1574d4',N:'#b8840f',S:'#0f8a78'};
const SCENARIO_COLOR='#7b8791';
const OPERATIONAL_COLOR='#0b6bbd';

const CLUSTER_COLORS={nodes:'#c25233',sites:LANDING_COLOR.cluster,referencePoints:'#783b69'};

// ---- 绘制小工具 ---------------------------------------------------------------

function drawMarker(ctx,shape,x,y,radius,fill,stroke,strokeWidth=1.4){
  ctx.save();
  ctx.fillStyle=fill;
  ctx.strokeStyle=stroke;
  ctx.lineWidth=strokeWidth;
  ctx.beginPath();
  if(shape==='diamond'){ctx.moveTo(x,y-radius);ctx.lineTo(x+radius,y);ctx.lineTo(x,y+radius);ctx.lineTo(x-radius,y);ctx.closePath();}
  else if(shape==='square'){ctx.rect(x-radius,y-radius,radius*2,radius*2);}
  else if(shape==='triangle'){ctx.moveTo(x,y-radius);ctx.lineTo(x+radius,y+radius*.8);ctx.lineTo(x-radius,y+radius*.8);ctx.closePath();}
  else ctx.arc(x,y,radius,0,Math.PI*2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawText(ctx,text,x,y,{color='#33424f',font='600 10px "Segoe UI",sans-serif'}={}){
  ctx.save();
  ctx.font=font;
  ctx.lineWidth=3;
  ctx.strokeStyle='#ffffff';
  ctx.strokeText(text,x,y);
  ctx.fillStyle=color;
  ctx.fillText(text,x,y);
  ctx.restore();
}

/** 标签避让：普通标签不允许堆叠。 */
function createLabelPlacer(){
  const taken=[];
  const overlaps=box=>taken.some(other=>!(box[2]<other[0]||box[0]>other[2]||box[3]<other[1]||box[1]>other[3]));
  return {
    reserve(box){taken.push(box);},
    place(ctx,text,x,y,{color,font='600 10px "Segoe UI",sans-serif'}={}){
      if(!text)return false;
      ctx.save();ctx.font=font;
      const width=ctx.measureText(text).width;
      ctx.restore();
      const box=[x-1,y-10,x+width+1,y+3];
      if(overlaps(box))return false;
      taken.push(box);
      drawText(ctx,text,x,y,{color,font});
      return true;
    }
  };
}

function drawAggregate(ctx,x,y,count,color){
  drawMarker(ctx,'circle',x,y,10.5,'#ffffff',color,2);
  drawMarker(ctx,'circle',x,y,3,color,color,1);
  drawText(ctx,String(count),x+14,y-11,{color,font:'700 11px "Segoe UI",sans-serif'});
}

// ---- 点标记计划（LOD + 屏幕空间聚合） -----------------------------------------

/**
 * 构建一次绘制的点显示计划。
 *
 * screenToLonLat 的语义是"屏幕像素 → 经纬度（lon/lat）"，只用于把聚合显示
 * 中心换算回地理坐标；单个点永远沿用原始坐标，因此不会引入任何漂移。
 * 它既不是 EPSG:3857 反投影，也不是 fitScreenBox 使用的那个换算。
 *
 * @param {{flow,view,size,screenPoint,screenToLonLat,layers,referenceOverlay,
 *          selectedReference,referenceFilters,filterReferenceSites}} input
 * @returns {{level,levelLabel,resolution,styles,clusterPixels,nodes,landingSites,
 *            referencePoints,referenceRoutes,referencePointsVisible,priorityIds,
 *            priorityCoordinates,selectedReferenceId,selectedScreen,clusterCounts}}
 */
export function buildDisplayPlan({
  flow,view,size,screenPoint,screenToLonLat,layers={},referenceOverlay={referenceRoutes:[],referencePoints:[]},
  selectedReference=null,referenceFilters={},filterReferenceSites=null
}){
  const style=displayStyle({
    res:view?view.res:0,
    featureCount:(flow?.nodes||[]).length+(flow?.reference_landing_sites?.items||[]).length
  });
  const styles=style.styles;
  const [width,height]=size;
  const onScreen=entry=>{
    const point=entry.screen||entry.center||[0,0];
    return point[0]>-90&&point[1]>-90&&point[0]<width+90&&point[1]<height+90;
  };

  // 聚合只改显示位置：单点沿用原始坐标，聚合中心换算回经纬度显示坐标。
  // 聚合阈值来自 displayStyle 顶层（styles 只承载线宽/透明度等绘制参数）。
  const cluster=items=>toGeographic(
    clusterPoints(items,screenPoint,style.clusterPixels).entries,
    screenToLonLat
  ).filter(onScreen);

  const nodeItems=(flow?.nodes||[]).map(node=>({id:node.node_id,coordinate:node.coordinate,node}));
  const catalog=(flow?.reference_landing_sites?.items)||[];
  const filtered=typeof filterReferenceSites==='function'?filterReferenceSites(catalog,referenceFilters):catalog;
  const siteItems=filtered.map(site=>({id:site.reference_site_id,coordinate:site.coordinate,site}));

  const nodes=cluster(nodeItems);
  const landingSites=layers.landingSites===false?[]:cluster(siteItems);

  // 参考航路点的显示条件 = 图层开关 → LOD 语义：
  //   overview 一律不显示；medium 默认不显示（仅调用方显式要求时显示）；detail 显示。
  const referencePointsVisible=layers.referenceRoutePointLayer!==false&&(
    style.level==='detail'||(style.level==='medium'&&layers.referencePointsDetail===true));

  // 业务优先对象：当前选择的参考对象、当前航路端点
  const priorityIds=new Set();
  const priorityCoordinates=new Set();
  if(selectedReference&&selectedReference.id)priorityIds.add(String(selectedReference.id));
  for(const route of flow?.scenario_routes||[]){
    const path=route.path||[];
    if(path.length)priorityCoordinates.add(path[0].join(','));
    if(path.length>1)priorityCoordinates.add(path[path.length-1].join(','));
  }

  const selectedScreen=(()=>{
    if(!selectedReference)return null;
    const model=referenceOverlay||{};
    if(selectedReference.kind==='route'){
      const route=(model.referenceRoutes||[]).find(item=>item.reference_route_id===selectedReference.id);
      if(!route||!(route.path||[]).length)return null;
      return screenPoint(route.path[Math.floor(route.path.length/2)]);
    }
    const point=(model.referencePoints||[]).find(item=>item.reference_route_point_id===selectedReference.id);
    return point?screenPoint(point.coordinate):null;
  })();

  return {
    level:style.level,
    levelLabel:style.label,
    resolution:style.resolution,
    styles,
    clusterPixels:style.clusterPixels,
    nodes,
    landingSites,
    referenceRoutes:(model=>(model.referenceRoutes||[]))(referenceOverlay||{}),
    referencePoints:(model=>(model.referencePoints||[]))(referenceOverlay||{}),
    referencePointsVisible,
    priorityIds,
    priorityCoordinates,
    selectedReferenceId:selectedReference&&selectedReference.kind==='route'?selectedReference.id:null,
    selectedScreen,
    gridTheme:null,
    clusterCounts:{
      nodes:nodes.filter(entry=>entry.count>1).reduce((sum,entry)=>sum+entry.count,0),
      sites:landingSites.filter(entry=>entry.count>1).reduce((sum,entry)=>sum+entry.count,0)
    }
  };
}

// ---- 命中测试（避免 overview 下误选看不见的单点） ----------------------------

/** 节点 / 起降点聚合命中。 */
export function hitDisplayEntry(plan,point,{kind='nodes',singleRadius=9}={}){
  const entries=kind==='sites'?plan.landingSites:plan.nodes;
  return hitCluster(entries,point,{singleRadius});
}

export function entryExtent(entry){return extentOf(entry);}

// ---- 绘制 ---------------------------------------------------------------------

/**
 * 绘制 workflow 叠加层。
 * @param {{ctx,view,flow,plan,layers,screenPoint,currentStep,profileHoverCoordinate,
 *          draftBounds,gridDisplay,gridCache,visibleBounds,gridTheme,palettes,riskBreaks,
 *          drawWorkspace,drawGridThemes,drawGridBoundaries,proposedPlanActions}} input
 */
export function drawWorkflowLayers({
  ctx,view,flow,plan,layers={},screenPoint,profileHoverCoordinate=null,gridTheme=null,
  drawWorkspace,drawGridThemes,drawGridBoundaries,proposedPlanActions=()=>[]
}){
  const styles=plan.styles;
  const placer=createLabelPlacer();

  drawWorkspace();
  drawGridThemes();
  drawGridBoundaries();

  // 1) 航路（当前选择增强，其余降低视觉权重）
  const selectedRouteId=plan.selectedReferenceId||null;
  for(const route of flow.scenario_routes||[]){
    drawLine(ctx,screenPoint,view,route.path,SCENARIO_COLOR,styles.scenarioWidth,[7,5]);
  }
  for(const route of flow.operational_routes||[]){
    if(route.status!=='passed')continue;
    const emphasized=!selectedRouteId||route.route_id===selectedRouteId;
    const width=emphasized?styles.operationalWidth:Math.max(1,styles.operationalWidth-1.2);
    ctx.save();
    if(!emphasized)ctx.globalAlpha=styles.operationalAlpha*.55;
    drawLine(ctx,screenPoint,view,route.path,OPERATIONAL_COLOR,width);
    ctx.restore();
  }

  // 2) 专题覆盖（沿用既有 overlay 模块，几何与语义不变）
  if(layers.buildingClearance){
    drawBuildingClearanceOverlay({ctx,screenPoint,drawLine,assessment:flow.building_clearance_assessment});
  }
  if(layers.v3Candidate){
    drawV3CandidateOverlay({ctx,screenPoint,drawLine,model:v3OverlayModel(flow)});
  }
  if(layers.layeredFeasibility){
    drawLayeredFeasibilityOverlay({ctx,view,screenPoint,flow,grid:flow.grid,gridTheme:gridTheme});
  }

  // 3) 参考航线与航路点由 main.js 的 drawWorkflowOverlay 显式调用
  //    drawReferenceOverlay（保持该参考层入口唯一且可审计）。

  // 4) CNS 缺口线：整体降低线宽，避免抢地图
  const gapToggles={C:'cLayer',N:'nLayer',S:'sLayer'};
  const gapRoutes=flow.cns_gap_analysis?.status==='stale'?[]:(flow.cns_gap_analysis?.routes||[]);
  for(const route of gapRoutes){
    for(const subsystem of route.subsystems||[]){
      if(layers[gapToggles[subsystem.subsystem]]===false)continue;
      for(const segment of subsystem.uncovered_segments||[]){
        drawLine(ctx,screenPoint,view,segment.path,GAP_COLORS[subsystem.subsystem],styles.gapWidth,[5,3]);
      }
    }
  }

  // 5) CNS 覆盖：overview 不画大圈，避免大片色块
  for(const [subsystem,layer] of Object.entries(flow.coverage?.layers||{})){
    if(layers[{C:'cLayer',N:'nLayer',S:'sLayer'}[subsystem]]===false)continue;
    const color=SUBSYSTEM_COLORS[subsystem];
    for(const station of layer.stations||[]){
      const [x,y]=screenPoint(station.coordinate);
      if(styles.coverageRing){
        const radius=Math.max(4,station.radius_m/view.res);
        ctx.save();
        ctx.strokeStyle=color;ctx.fillStyle=color+'1a';ctx.lineWidth=1;
        ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fill();ctx.stroke();
        ctx.restore();
      }
      drawMarker(ctx,'circle',x,y,styles.coverageRing?3.4:2.4,station.role==='gap'?'#ffffff':color,'#ffffff',1.1);
    }
  }

  // 6) CNS 输入点
  if(layers.existingCns){
    for(const facility of flow.existing_cns_facilities?.items||[]){
      const [x,y]=screenPoint(facility.coordinate);
      drawMarker(ctx,'circle',x,y,styles.pointRadius+1,CNS_COLORS.existing,'#ffffff',1.2);
    }
  }
  if(layers.candidateSites){
    for(const site of flow.candidate_sites?.items||[]){
      const [x,y]=screenPoint(site.coordinate);
      drawMarker(ctx,'diamond',x,y,styles.pointRadius+1.4,site.usable===false?CNS_COLORS.unusable:CNS_COLORS.candidate,'#ffffff',1.2);
    }
    for(const action of proposedPlanActions(flow)){
      const [x,y]=screenPoint(action.coordinate);
      drawMarker(ctx,'square',x,y,styles.pointRadius+1,CNS_COLORS.planned,'#ffffff',1.2);
    }
  }
  if(profileHoverCoordinate){
    const [x,y]=screenPoint(profileHoverCoordinate);
    drawMarker(ctx,'circle',x,y,styles.pointRadius+2,CNS_COLORS.hover,'#ffffff',1.4);
  }

  // 7) 参考起降点：聚合显示，绝不铺开大图标
  for(const entry of plan.landingSites){
    const [x,y]=entry.screen||entry.center;
    if(entry.count>1){drawAggregate(ctx,x,y,entry.count,LANDING_COLOR.cluster);continue;}
    const site=entry.anchor.site||{};
    const color=site.possible_duplicate?LANDING_COLOR.duplicate:LANDING_COLOR.normal;
    drawMarker(ctx,'diamond',x,y,styles.pointRadius+1.4,color,'#ffffff',1.2);
  }

  // 8) 项目起降点：聚合 + 名称策略（默认不铺开名称，且始终避让）
  for(const entry of plan.nodes){
    const [x,y]=entry.screen||entry.center;
    if(entry.count>1){drawAggregate(ctx,x,y,entry.count,CLUSTER_COLORS.nodes);continue;}
    const node=entry.anchor.node||{};
    const id=String(node.node_id||'');
    const coordinate=Array.isArray(node.coordinate)?node.coordinate.join(','):'';
    const priority=plan.priorityIds.has(id)||plan.priorityCoordinates.has(coordinate);
    drawMarker(ctx,'circle',x,y,priority?NODE_COLOR.radius+1:NODE_COLOR.radius,NODE_COLOR.fill,NODE_COLOR.stroke,priority?2.6:2);
    // 名称策略：overview 默认不显示；medium/detail 做碰撞避让
    if(priority){
      placer.place(ctx,node.node_id,x+9,y-7,{color:NODE_COLOR.label});
      continue;
    }
    if(styles.nameMode==='hidden')continue;
    placer.place(ctx,node.node_id,x+9,y-7,{color:NODE_COLOR.label,font:'500 9px "Segoe UI",sans-serif'});
  }

  // 9) 当前选择的参考对象：名称单独占位
  if(plan.selectedScreen){
    placer.place(ctx,plan.selectedReferenceId||'当前选择',plan.selectedScreen[0]+10,plan.selectedScreen[1]-9,{color:'#8f2f6b'});
  }
}