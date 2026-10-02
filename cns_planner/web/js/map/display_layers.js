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
import {drawLayeredCandidateOverlay} from './layered_candidate_overlay.js';
import {drawRadarLayoutOverlay} from './radar_layout_overlay.js';
import {cnsServiceOverlayModel,drawCnsServiceOverlay,drawSurfaceFactsLayer} from './cns_service_overlay.js';
import {radarOverlayModel} from '../workflow/radar_surveillance_layout.js';
import {clusterPoints,toGeographic,extentOf,hitCluster} from './point_clustering.js';
import {
  displayStyle,TOWER_HIGHLIGHT_SIZE_PX,TOWER_SYMBOL_HALO_EXTRA_PX,TOWER_SYMBOL_STROKE_PX
} from './lod.js';

// ---- 样式（颜色只表达来源与状态，不表达通过与否） ----------------------------

const NODE_COLOR={fill:'#ffffff',stroke:'#c25233',label:'#22303c',radius:5.5};
const LANDING_COLOR={normal:'#1a8677',duplicate:'#8b6b2f',cluster:'#0f6f62'};
//: 真实通信铁塔站址：只表达"存在一个真实站址"，不表达任何通信能力/通过与否。
const TOWER_COLOR={normal:'#1f7a8c',cluster:'#155f6d'};
//: CNS 共塔候选 → 对应铁塔的高亮（纯 UI 关联提示，不画永久连接线）。
const TOWER_HIGHLIGHT_COLOR='#d12f8a';
const CNS_COLORS={existing:'#7256a1',candidate:'#e07a26',unusable:'#8b949e',planned:'#d12f8a',hover:'#111111',cluster:'#5c6d7d'};
const GAP_COLORS={C:'#d83b35',N:'#c26b16',S:'#9b3eb5'};
const SUBSYSTEM_COLORS={C:'#1574d4',N:'#b8840f',S:'#0f8a78'};
const SCENARIO_COLOR='#7b8791';
const OPERATIONAL_COLOR='#0b6bbd';
//: 临时 evidence highlight（RRP segment / high-risk interval hover）视觉权重最高：
//: 它画在正常路线与 candidate 之上，且只是纯 UI 状态，不影响任何业务层。
const ROUTE_EVIDENCE_COLOR='#c0392b';
//: Round 2.5 / P17 连续服务可接受性：两个**默认关闭**的地图图层配色
//: （水平航路保护走廊 / 监视保护覆盖）。颜色只表达"这属于哪个图层"，
//: 绝不表达通过与否，也不参与任何业务判定。
const PROTECTION_CORRIDOR_COLOR='#1f6f8b';
const SURVEILLANCE_PROTECTION_COLOR='#8b1f6f';
const SURVEILLANCE_UNUSABLE_COLOR='#8b949e';
//: 走廊单侧偏移的像素上限：20 px → 走廊带总宽 ≤ 40 px（远景不至于消失、近景不至于糊屏）。
const PROTECTION_CORRIDOR_MAX_HALF_PX=20;
//: view.res 不可用时的退化半宽（px）：只画一条粗半透明中心带，绝不假装知道米制宽度。
const PROTECTION_CORRIDOR_FALLBACK_HALF_PX=3;

const CLUSTER_COLORS={nodes:'#c25233',sites:LANDING_COLOR.cluster,referencePoints:'#783b69',towers:TOWER_COLOR.cluster};

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

// ---- P17 连续服务可接受性：里程 → 坐标 / 屏幕空间走廊偏移 --------------------

/**
 * 航路里程（米）→ 经纬度：沿折线按**累计距离**取点，越界时 clamp 到端点。
 *
 * ``routeLengthM`` 是后端给出的米制航路长度；折线的累计长度只用来确定"落在哪一段"，
 * 因此本函数**不做米制投影、不外推、不改变后端结论**：它只是把后端已经给出的里程
 * （缺口段的 ``start_offset_m`` / ``end_offset_m``）落回后端已经给出的 ``route_path``。
 * ``routeLengthM`` 缺失或非法时退化为"offset 即折线比例"（与 cns_service_overlay 的
 * ``routeOffsetFraction`` 同口径），并同样 clamp 到 [0,1]。
 *
 * @returns {[number,number]|null} 经纬度；路径为空返回 null
 */
export function routeOffsetToCoordinate(path,routeLengthM,offsetM){
  if(!Array.isArray(path)||!path.length)return null;
  const points=path
    .filter(point=>Array.isArray(point)&&point.length>=2)
    .map(point=>[Number(point[0]),Number(point[1])]);
  if(!points.length)return null;
  if(points.length===1)return [points[0][0],points[0][1]];
  const offset=Number(offsetM);
  if(!Number.isFinite(offset))return null;
  const segments=[];
  let total=0;
  for(let index=1;index<points.length;index+=1){
    const length=Math.hypot(
      points[index][0]-points[index-1][0],
      points[index][1]-points[index-1][1]
    );
    segments.push(length);total+=length;
  }
  if(!(total>0))return [points[0][0],points[0][1]];
  const routeLength=Number(routeLengthM);
  const ratio=Number.isFinite(routeLength)&&routeLength>0?offset/routeLength:offset;
  let target=Math.max(0,Math.min(1,ratio))*total;
  for(let index=1;index<points.length;index+=1){
    const length=segments[index-1];
    if(length<=0)continue;
    if(target<=length){
      const local=target/length;
      return [
        points[index-1][0]+(points[index][0]-points[index-1][0])*local,
        points[index-1][1]+(points[index][1]-points[index-1][1])*local
      ];
    }
    target-=length;
  }
  const last=points[points.length-1];
  return [last[0],last[1]];
}

/** 屏幕折线的顶点法向（首尾沿用单侧方向；零长度段退化为 [0,0]）。 */
function screenNormals(points){
  const normals=[];
  for(let index=0;index<points.length;index+=1){
    const previous=points[Math.max(0,index-1)];
    const next=points[Math.min(points.length-1,index+1)];
    let dx=next[0]-previous[0],dy=next[1]-previous[1];
    const length=Math.hypot(dx,dy);
    if(length>0){dx/=length;dy/=length;}else{dx=0;dy=0;}
    normals.push([-dy,dx]);
  }
  return normals;
}

/**
 * 走廊半宽（米）→ 单侧像素偏移。
 *
 * ``view.res`` 是 EPSG:3857 米/像素，因此地面米/像素 = res·cos(lat) ——
 * 与 cns_service_overlay / radar_layout_overlay 的既有换算**同式**，
 * 这里不引入第二套投影。结果 clamp 到 [1, 20] px。
 *
 * @returns {number|null} view.res 不可用时返回 null（调用方退化为固定线宽的粗带）
 */
function protectionHalfWidthPx(halfWidthM,view,latitudeDeg){
  const half=Number(halfWidthM);
  const res=Number(view?.res);
  if(!Number.isFinite(half)||half<=0)return null;
  if(!Number.isFinite(res)||res<=0)return null;
  const latitude=Number(latitudeDeg);
  const scale=Number.isFinite(latitude)
    ?Math.max(Math.cos(Math.max(-85.05112878,Math.min(85.05112878,latitude*Math.PI/180))),1e-9)
    :1;
  return Math.max(1,Math.min(half/(res*scale),PROTECTION_CORRIDOR_MAX_HALF_PX));
}

/** 标注数值：有限值按位数取整 / 保留小数，缺值（null / undefined / 空串 / 非数）一律写「—」，
 *  绝不把"没有证据"格式化成 0 冒充已评估的数值。 */
function formatMetric(value,digits=0){
  if(value===null||value===undefined||value==='')return '—';
  const number=Number(value);
  if(!Number.isFinite(number))return '—';
  return digits>0?number.toFixed(digits):String(Math.round(number));
}

/** 单条走廊：半透明带（外边界为 outer_half_width_m，屏幕空间法向偏移）+ 端面 + 参数标注。 */
function drawProtectionCorridor({ctx,view,screenPoint,path,corridor}){
  const screens=path
    .map(point=>screenPoint(point))
    .filter(point=>Number.isFinite(point?.[0])&&Number.isFinite(point?.[1]));
  if(screens.length<2)return false;
  const computed=protectionHalfWidthPx(corridor.outer_half_width_m,view,Number(path[0]?.[1]));
  const halfPx=computed??PROTECTION_CORRIDOR_FALLBACK_HALF_PX;
  const normals=screenNormals(screens);
  const offset=side=>screens.map((point,index)=>[
    point[0]+normals[index][0]*halfPx*side,
    point[1]+normals[index][1]*halfPx*side
  ]);
  const left=offset(1),right=offset(-1);

  // 半透明填充：只表达"走廊覆盖到这条带"，不表达通过与否。
  ctx.save();
  ctx.globalAlpha=computed===null?0.1:0.16;
  ctx.fillStyle=PROTECTION_CORRIDOR_COLOR;
  ctx.beginPath();
  ctx.moveTo(left[0][0],left[0][1]);
  for(const point of left.slice(1))ctx.lineTo(point[0],point[1]);
  for(const point of [...right].reverse())ctx.lineTo(point[0],point[1]);
  ctx.closePath();
  ctx.fill();
  // 外边界描边（走廊的左右两条边界）。
  ctx.globalAlpha=0.78;
  ctx.strokeStyle=PROTECTION_CORRIDOR_COLOR;
  ctx.lineWidth=1.1;
  ctx.setLineDash([]);
  for(const boundary of [left,right]){
    ctx.beginPath();
    ctx.moveTo(boundary[0][0],boundary[0][1]);
    for(const point of boundary.slice(1))ctx.lineTo(point[0],point[1]);
    ctx.stroke();
  }
  ctx.restore();

  // 中心线：真实航路几何原样绘制（复用既有 drawLine）。
  drawLine(ctx,screenPoint,view,path,PROTECTION_CORRIDOR_COLOR,1.6,[]);

  // 走廊端面（起点法向短横线）+ 参数标注：全部来自后端 corridor，缺值写「—」。
  const [x,y]=screens[0];
  const [nx,ny]=normals[0];
  const faceHalf=Math.max(halfPx,6);
  ctx.save();
  ctx.strokeStyle=PROTECTION_CORRIDOR_COLOR;
  ctx.lineWidth=1.6;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(x-nx*faceHalf,y-ny*faceHalf);
  ctx.lineTo(x+nx*faceHalf,y+ny*faceHalf);
  ctx.stroke();
  ctx.restore();
  drawText(ctx,
    'D_protection='+formatMetric(corridor.D_protection_m,0)+' m · V_rel='
      +formatMetric(corridor.V_relative_mps,1)+' m/s · T_chain='
      +formatMetric(corridor.T_chain_s,1)+' s',
    x-nx*faceHalf+6,y-ny*faceHalf-4,{color:PROTECTION_CORRIDOR_COLOR});
  return true;
}

/**
 * 水平航路保护走廊（Round 2.5 / P17）：**默认关闭**的地图图层。
 *
 * 只读 ``flow.cns_continuous_service.result.routes[].corridor``：
 * route_path（真实航路几何）、outer_half_width_m（D_protection + 安全/不确定度余量）、
 * D_protection_m / V_relative_mps / T_chain_s（标注）。前端不推导任何新结论。
 *
 * @returns {number} 实际绘制的走廊条数
 */
function drawRouteProtectionCorridors({ctx,view,screenPoint,flow}){
  const routes=flow?.cns_continuous_service?.result?.routes||[];
  let drawn=0;
  for(const route of routes){
    const corridor=route?.corridor;
    const path=corridor?.route_path;
    if(!Array.isArray(path)||path.length<2)continue;
    if(drawProtectionCorridor({ctx,view,screenPoint,path,corridor}))drawn+=1;
  }
  return drawn;
}

/**
 * 监视保护覆盖（Round 2.5 / P17）：**默认关闭**的地图图层。
 *
 * 只画后端已经给出的三类事实：
 *   * ``events_by_kind.surveillance_detection_gap``（沿航路**里程**的探测缺口段）→ 虚线；
 *   * ``first_detection_evidence.usable`` → 航路两端的监视覆盖点（不可用为空心灰点）；
 *   * ``surveillance_acceptance.t_margin_s`` → 时间余量标注（缺值写 ``T_margin=—``）。
 * 里程 → 经纬度由 routeOffsetToCoordinate 在真实 route_path 上插值，绝不外推。
 *
 * @returns {{gaps:number,points:number}}
 */
function drawSurveillanceProtectionCoverage({ctx,view,screenPoint,flow}){
  const routes=flow?.cns_continuous_service?.result?.routes||[];
  let gaps=0,points=0;
  for(const route of routes){
    const corridor=route?.corridor||{};
    const path=corridor.route_path;
    if(!Array.isArray(path)||path.length<2)continue;
    const routeLengthM=Number.isFinite(Number(corridor.route_length_m))
      ?Number(corridor.route_length_m)
      :Number(route.route_length_m);
    for(const gap of route.events_by_kind?.surveillance_detection_gap||[]){
      const from=routeOffsetToCoordinate(path,routeLengthM,gap?.start_offset_m);
      const to=routeOffsetToCoordinate(path,routeLengthM,gap?.end_offset_m);
      if(!from||!to)continue;
      drawLine(ctx,screenPoint,view,[from,to],SURVEILLANCE_PROTECTION_COLOR,2.6,[6,4]);
      gaps+=1;
    }
    const usable=route.first_detection_evidence?.usable===true;
    for(const coordinate of [path[0],path[path.length-1]]){
      const [x,y]=screenPoint(coordinate);
      if(!Number.isFinite(x)||!Number.isFinite(y))continue;
      ctx.save();
      ctx.beginPath();
      ctx.arc(x,y,4,0,Math.PI*2);
      ctx.fillStyle=usable?SURVEILLANCE_PROTECTION_COLOR:'#ffffff';
      ctx.strokeStyle=usable?SURVEILLANCE_PROTECTION_COLOR:SURVEILLANCE_UNUSABLE_COLOR;
      ctx.lineWidth=1.6;
      if(usable)ctx.fill();
      ctx.stroke();
      ctx.restore();
      points+=1;
    }
    const [marginX,marginY]=screenPoint(path[0]);
    if(!Number.isFinite(marginX)||!Number.isFinite(marginY))continue;
    const margin=Number(route.surveillance_acceptance?.t_margin_s);
    drawText(ctx,
      Number.isFinite(margin)?'T_margin='+formatMetric(margin,1)+' s':'T_margin=—',
      marginX+7,marginY-7,{color:SURVEILLANCE_PROTECTION_COLOR});
  }
  return {gaps,points};
}

// ---- 通信铁塔矢量符号（地图符号与图例符号的唯一来源） ------------------------
//
// MAP-TOWER-SYMBOL-V2：经典桁架通信铁塔轮廓（lattice tower silhouette）。识别特征：
//
//         │            顶部天线桅杆
//        ╱ ╲
//       ╱───╲          顶梁（塔腿起点）
//      ╱X   X╲         第 1 组 X 型斜撑
//     ╱───────╲        第 2 层水平横梁
//     ╱ X   X ╲        第 2 组 X 型斜撑
//    ╱─────────╲       第 3 层水平横梁
//    ╱  X   X  ╲       第 3 组 X 型斜撑
//   ╱───────────╲      宽底座上沿
//  ╱             ╲     两条向下张开的外侧塔腿
//
// 几何定义在归一化的 x∈[-0.65,0.65]、y∈[-1,1] 单位框内（y 向下为正，与 canvas 一致），
// 因此 **2 个单位 = 符号的可见高度**。canvas 绘制与图例 inline SVG 用**同一份线段表**，
// 图例因此永远不会画出第二套铁塔。
//
// 尺寸语义（MAP-TOWER-SYMBOL-V2 的核心修正）：符号尺寸一律是**屏幕像素高度**
// （``towerSymbolSizePx`` / ``TOWER_HIGHLIGHT_SIZE_PX``），由 map/lod.js 集中声明；
// 归一化坐标只在 ``traceTowerPath`` 里乘一次"半尺寸 px"。旧实现把 0.7 / 0.78 / 1
// 这种归一化倍数直接当像素用，导致 detail 档符号总高只有约 1.75 px（放大也看不见）。
//
// 只用本地 canvas Path / segments：无 emoji、无字体图标、无第三方 icon 包、无外部 SVG、
// 无网络图片。
export const TOWER_SYMBOL={
  //: [[起点], [终点]]，单位框内坐标（y 向下为正，与 canvas 一致）。
  segments:[
    [[0,-1],[0,-0.78]],              // 顶部天线桅杆
    [[-0.08,-0.78],[0.08,-0.78]],    // 顶梁（塔腿起点）
    [[-0.08,-0.78],[-0.65,1]],       // 左塔腿（向下张开）
    [[0.08,-0.78],[0.65,1]],         // 右塔腿（向下张开）
    [[-0.234,-0.3],[0.234,-0.3]],    // 第 2 层水平横梁
    [[-0.387,0.18],[0.387,0.18]],    // 第 3 层水平横梁
    [[-0.541,0.66],[0.541,0.66]],    // 第 4 层水平横梁（宽底座上沿）
    [[-0.08,-0.78],[0.234,-0.3]],    // X 型斜撑 1
    [[0.08,-0.78],[-0.234,-0.3]],
    [[-0.234,-0.3],[0.387,0.18]],    // X 型斜撑 2
    [[0.234,-0.3],[-0.387,0.18]],
    [[-0.387,0.18],[0.541,0.66]],    // X 型斜撑 3
    [[0.387,0.18],[-0.541,0.66]]
  ],
  //: 图例用 viewBox（正方形，保证 SVG 不被拉伸）。
  viewBox:'-1 -1 2 2',
  //: 归一化几何的可见高度（y 跨度）：2 个单位 = 1 个"符号高度"。
  heightUnits:2
};

/** 归一化坐标 → 屏幕坐标：只在这里乘一次半尺寸（px）。 */
function traceTowerPath(ctx,x,y,halfSizePx){
  ctx.beginPath();
  for(const [start,end] of TOWER_SYMBOL.segments){
    ctx.moveTo(x+start[0]*halfSizePx,y+start[1]*halfSizePx);
    ctx.lineTo(x+end[0]*halfSizePx,y+end[1]*halfSizePx);
  }
}

/**
 * 在 canvas 上画一个铁塔符号（白色 halo 在下，保证在海图 / 道路 / 建筑底图上可读）。
 *
 * @param {number} sizePx 符号的**可见高度（像素）**；``sizePx<=0`` 时什么都不画
 *   （overview 档的孤立单塔就是这种情形：它根本不进入绘制计划）。
 * @returns {boolean} 是否真的画了
 */
export function drawTowerSymbol(ctx,x,y,{
  sizePx=TOWER_HIGHLIGHT_SIZE_PX,color=TOWER_COLOR.normal,
  strokePx=TOWER_SYMBOL_STROKE_PX.detail,
  halo=true,haloExtraPx=TOWER_SYMBOL_HALO_EXTRA_PX
}={}){
  const size=Number(sizePx);
  if(!Number.isFinite(size)||size<=0)return false;
  const halfSizePx=size/2;
  ctx.save();
  ctx.lineCap='round';
  if(halo){
    traceTowerPath(ctx,x,y,halfSizePx);
    ctx.strokeStyle='#ffffff';ctx.lineWidth=strokePx+haloExtraPx;ctx.stroke();
  }
  traceTowerPath(ctx,x,y,halfSizePx);
  ctx.strokeStyle=color;ctx.lineWidth=strokePx;ctx.stroke();
  ctx.restore();
  return true;
}

/**
 * 同一份符号的 inline SVG（统一图例使用，无外部资源、无版权依赖）。
 *
 * ``size`` 与 canvas 的 ``sizePx`` 语义**完全一致**：符号的可见高度（px）。
 * viewBox 的高度是 ``heightUnits``（2），因此 SVG 的 width/height 就等于符号高度。
 */
export function towerSymbolSvg({
  size=20,color=TOWER_COLOR.normal,
  strokePx=TOWER_SYMBOL_STROKE_PX.detail,halo=true,haloExtraPx=TOWER_SYMBOL_HALO_EXTRA_PX
}={}){
  const path=TOWER_SYMBOL.segments
    .map(([start,end])=>'M'+start[0]+' '+start[1]+'L'+end[0]+' '+end[1])
    .join('');
  const haloPath=halo
    ?'<path d="'+path+'" fill="none" stroke="#ffffff" stroke-width="'+(strokePx+haloExtraPx)+'" stroke-linecap="round"/>'
    :'';
  return '<svg width="'+size+'" height="'+size+'" viewBox="'+TOWER_SYMBOL.viewBox+'" aria-hidden="true">'
    +haloPath
    +'<path d="'+path+'" fill="none" stroke="'+color+'" stroke-width="'+strokePx+'" stroke-linecap="round"/></svg>';
}

//: drawMarker 支持的形状；图例也只允许这些形状，避免图例与地图各画一套。
export const MARKER_SHAPES=['circle','diamond','square','triangle'];

/**
 * 基础标记形状的 inline SVG。
 *
 * 与 ``drawMarker`` 一一对应（同一个 shape 词表、同一套视觉语义），
 * 图例因此不会引入第二套符号语言；铁塔符号另有 :data:`TOWER_SYMBOL` 作为唯一来源。
 */
export function markerSymbolSvg(shape,{size=14,fill='#ffffff',stroke='#33424f',strokeWidth=1.4}={}){
  if(!MARKER_SHAPES.includes(shape))throw new Error('未知的地图标记形状：'+shape);
  const half=size/2,inset=strokeWidth;
  const body=shape==='circle'
    ?'<circle cx="'+half+'" cy="'+half+'" r="'+Math.max(1,half-inset)+'"/>'
    :shape==='diamond'
      ?'<polygon points="'+half+',1 '+(size-1)+','+half+' '+half+','+(size-1)+' 1,'+half+'"/>'
      :shape==='triangle'
        ?'<polygon points="'+half+',1 '+(size-1)+','+(size-1)+' 1,'+(size-1)+'"/>'
        :'<rect x="1" y="1" width="'+(size-2)+'" height="'+(size-2)+'"/>';
  return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 '+size+' '+size+'" aria-hidden="true">'
    +'<g fill="'+fill+'" stroke="'+stroke+'" stroke-width="'+strokeWidth+'">'+body+'</g></svg>';
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
  selectedReference=null,referenceFilters={},filterReferenceSites=null,towerHighlight=null
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
  // 参考起降点跟随图层抽屉的 referenceLandingLayer 开关；关闭时不进入计划（也就不绘制、不可命中）
  const landingSites=layers.referenceLandingLayer===false?[]:cluster(siteItems);

  // 真实通信铁塔站址：**默认关闭** —— 只有显式勾选 towerLayer 才进入计划，
  // 因此默认既不绘制也不可命中。373 个点一律复用同一套聚合（point_clustering），
  // 绝不铺开单点标记或名称标签。
  const towerItems=((flow?.towers?.items)||[]).map(tower=>({id:tower.tower_id,coordinate:tower.coordinate,tower}));
  const towers=layers.towerLayer===true?cluster(towerItems):[];
  // overview 档只显示聚合点：聚合不住的孤立单塔在这里就被移出计划，
  // 因此它既不会被绘制，也不会被命中（不会出现"点到看不见的点"）。
  const towerMarkerMode=styles.towerMarkerMode||'single';
  // overview 只显示聚合点；但被显式高亮的那个塔（CNS 共塔候选联动）始终保留，
  // 否则用户点了共塔候选却看不到对应铁塔。
  const visibleTowers=towerMarkerMode==='cluster'
    ?towers.filter(entry=>entry.count>1||(
      towerHighlight&&entry.anchor&&entry.anchor.tower
      &&String(entry.anchor.tower.tower_id)===String(towerHighlight)
    ))
    :towers;

  // CNS 共塔候选：真实铁塔派生出的**宿主候选**（不是已有设备）。只跟随候选站图层开关，
  // 并且只在地图上与宿主铁塔建立"点击即可高亮"的关联，绝不画大量永久连接线。
  const cnsTowerCandidates=layers.candidateSiteLayer===false?[]:
    ((flow?.tower_colocation_candidates?.items)||[]).map(site=>({
      id:String(site.site_id||''),
      coordinate:site.coordinate,
      screen:site.coordinate?screenPoint(site.coordinate):null,
      site,
      hostTowerId:String((((site.metadata||{}).host)||{}).host_tower_id||'')||null,
    })).filter(entry=>entry.screen);

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
    towerMarkerMode,
    // 铁塔符号的真实屏幕像素尺寸 / 笔画 / 命中半径：全部来自 map/lod.js 的同一套 LOD。
    towerSymbolSizePx:styles.towerSymbolSizePx||0,
    towerSymbolStrokePx:styles.towerSymbolStrokePx||TOWER_SYMBOL_STROKE_PX.detail,
    towerHighlightSizePx:styles.towerHighlightSizePx||TOWER_HIGHLIGHT_SIZE_PX,
    towerCandidateSizePx:styles.towerCandidateSizePx||TOWER_HIGHLIGHT_SIZE_PX,
    towerHighlightRingRadiusPx:styles.towerHighlightRingRadiusPx||15,
    towerHitRadiusPx:styles.towerHitRadiusPx||13,
    nodes,
    landingSites,
    towers:visibleTowers,
    cnsTowerCandidates,
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
      sites:landingSites.filter(entry=>entry.count>1).reduce((sum,entry)=>sum+entry.count,0),
      towers:towers.filter(entry=>entry.count>1).reduce((sum,entry)=>sum+entry.count,0)
    }
  };
}

// ---- 命中测试（避免 overview 下误选看不见的单点） ----------------------------

/** 节点 / 起降点 / 铁塔聚合命中。
 *
 * 命中半径必须与视觉尺寸一致：铁塔 detail 图标约 22 px 高（宽约 14 px），因此默认使用
 * ``plan.towerHitRadiusPx``（约 13 px），避免"看得见却点不中"。
 * **nodes / landingSites 的半径保持 9 px 不变**（显式传入 singleRadius 时也以显式值为准）。
 */
export function hitDisplayEntry(plan,point,{kind='nodes',singleRadius=null}={}){
  const entries=kind==='sites'?plan.landingSites:kind==='towers'?plan.towers:plan.nodes;
  const radius=singleRadius??(kind==='towers'?(plan?.towerHitRadiusPx??13):9);
  return hitCluster(entries,point,{singleRadius:radius});
}

export function entryExtent(entry){return extentOf(entry);}

/** CNS 共塔候选命中（用于"点击候选 → 高亮宿主铁塔"）。 */
export function hitCnsTowerCandidate(plan,point,{radius=11}={}){
  const [x,y]=point;
  let best=null,bestDistance=Infinity;
  for(const entry of plan?.cnsTowerCandidates||[]){
    const screen=entry.screen;
    if(!screen)continue;
    const distance=Math.hypot(screen[0]-x,screen[1]-y);
    if(distance<=radius&&distance<bestDistance){best=entry;bestDistance=distance;}
  }
  return best;
}

// ---- 绘制 ---------------------------------------------------------------------

/**
 * 绘制 workflow 叠加层。
 *
 * 视觉层级（从低到高）：scenario / reference route → operational route →
 * **LayeredRouteCandidate（当前规划结果主线）** → 临时 evidence highlight（最高）。
 *
 * @param {{ctx,view,flow,plan,layers,screenPoint,profileHoverCoordinate,routeEvidenceHighlight,
 *          draftBounds,gridDisplay,gridCache,visibleBounds,gridTheme,palettes,riskBreaks,
 *          drawWorkspace,drawGridThemes,drawGridBoundaries,drawBuildingFootprints,
 *          drawConstraintLayer,proposedPlanActions}} input
 */
export function drawWorkflowLayers({
  ctx,view,flow,plan,layers={},screenPoint,profileHoverCoordinate=null,routeEvidenceHighlight=null,gridTheme=null,
  towerHighlight=null,visibleBounds=null,
  drawWorkspace,drawGridThemes,drawGridBoundaries,drawBuildingFootprints,drawConstraintLayer,proposedPlanActions=()=>[]
}){
  const styles=plan.styles;
  const placer=createLabelPlacer();

  drawWorkspace();
  drawGridThemes();
  drawGridBoundaries();
  // 高度层障碍（Planning Constraint Field）：**可行性**图层，默认关闭。
  // 放在网格边界之上、航路与候选航路之下，避免遮挡规划结果本身。
  // 是否真的画由调用方按图层开关决定（main.js 的 drawConstraintLayer）。
  if(typeof drawConstraintLayer==='function')drawConstraintLayer();
  // 陆海分类（Surface Facts）：**默认关闭**的独立图层，只表达格心代表点分类。
  // 画在网格边界之上、建筑/航路之下；unknown 用中性色，绝不按海面着色。
  if(layers.surfaceFactsLayer===true&&typeof visibleBounds==='function'){
    drawSurfaceFactsLayer({
      ctx,grid:flow?.grid,facts:flow?.surface_class_facts,
      screenPoint,visibleBounds,gridTheme,
    });
  }
  // 建筑轮廓只读底图性质：画在网格之上、航路之下，避免遮挡规划结果。
  // 是否真的画、画多少由调用方（map/building_footprint_layer.js）按图层开关与 LOD 决定。
  if(typeof drawBuildingFootprints==='function')drawBuildingFootprints();

  // 1) 航路（当前选择增强，其余降低视觉权重）
  //    scenario 只是场景对照、reference 只是参考：candidate 是当前规划结果主线，
  //    因此这两类在这里都被压到 candidate 之下（颜色更浅、线更细、虚线）。
  const selectedRouteId=plan.selectedReferenceId||null;
  for(const route of flow.scenario_routes||[]){
    ctx.save();
    ctx.globalAlpha=styles.scenarioAlpha*.72;
    drawLine(ctx,screenPoint,view,route.path,SCENARIO_COLOR,styles.scenarioWidth,[7,5]);
    ctx.restore();
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
  //    图层 key 与图层抽屉里的 checkbox id 完全一致（shell.js 的 layerSwitches 直接
  //    以 checkbox id 作为 key），不再使用缩写 key，避免开关读不到、图层永远不画。
  if(layers.buildingClearanceLayer){
    drawBuildingClearanceOverlay({ctx,screenPoint,drawLine,assessment:flow.building_clearance_assessment});
  }
  if(layers.v3CandidateLayer){
    drawV3CandidateOverlay({ctx,screenPoint,drawLine,model:v3OverlayModel(flow)});
  }
  if(layers.layeredFeasibilityLayer){
    // 只画 selected-layer coarse feasibility mask：不再绘制 candidate route。
    drawLayeredFeasibilityOverlay({ctx,view,screenPoint,flow,grid:flow.grid,gridTheme:gridTheme});
  }
  if(layers.layeredCandidateLayer!==false){
    // current LayeredRouteCandidate：优先 candidate.path（Theta* V2 真实起终点 + any-angle）。
    // overview / medium / detail 都允许显示，只按既有 LOD 调整线宽与 alpha。
    const width=styles.operationalWidth+0.4;
    drawLayeredCandidateOverlay({
      ctx,screenPoint,flow,grid:flow.grid,
      style:{width,alpha:styles.operationalAlpha,dash:[]},
    });
  }
  // Radar Surveillance Layout V1：**默认关闭**。只画已选方案的 selected towers / panel 扇区
  // 与按覆盖结果着色的航路；候选铁塔用 plan.towers 的弱化单点（不走聚合，避免与塔层语义混淆）。
  // 绝不绘制未选 panel 的 coverage polygon（数百个多边形会造成地图性能问题）。
  if(layers.radarSurveillanceLayer===true){
    const candidateTowers=(flow?.towers?.items||[])
      .filter(tower=>Array.isArray(tower.coordinate))
      .map(tower=>({tower_id:tower.tower_id,coordinate:tower.coordinate}));
    drawRadarLayoutOverlay({
      ctx,view,screenPoint,
      model:radarOverlayModel(flow,candidateTowers),
    });
  }

  // CNS service overlay（Round 3）：**独立模块**，与 Radar 分支完全分离。
  // 四个开关全部默认关闭；全部关闭时不构造模型、不绘制（零成本）。
  // Communication / RID 只画全向圆与航路缺口段，**绝不**画 sector / 90° panel。
  // 站点选取与既有原则一致：已有 active provider、P16 selected_actions、
  // 方案审查当前选中 variant 的动作；**绝不**铺开全部候选站址。
  const cnsServiceOn=layers.cnsCommunicationLayer===true||layers.cnsRidLayer===true||layers.cnsNavigationLayer===true;
  if(cnsServiceOn||layers.cnsServiceGapLayer===true||layers.cnsFacilityPlanLayer===true){
    drawCnsServiceOverlay({
      ctx,view,screenPoint,layers,
      model:cnsServiceOverlayModel(flow,{selectedActions:proposedPlanActions(flow)}),
    });
  }

  // Round 2.5 / P17 连续服务可接受性：两个**默认关闭**的独立图层。
  // 只读 flow.cns_continuous_service 的 result（后端已给出的走廊几何与缺口里程），
  // 不构造模型、不推导结论、不写业务状态；两个开关都关闭时零成本（不进入循环）。
  if(layers.routeProtectionCorridorLayer===true){
    drawRouteProtectionCorridors({ctx,view,screenPoint,flow});
  }
  if(layers.surveillanceProtectionLayer===true){
    drawSurveillanceProtectionCoverage({ctx,view,screenPoint,flow});
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

  // 6) CNS 输入点（同样以 checkbox id 作为图层 key）
  if(layers.existingCnsLayer){
    for(const facility of flow.existing_cns_facilities?.items||[]){
      const [x,y]=screenPoint(facility.coordinate);
      drawMarker(ctx,'circle',x,y,styles.pointRadius+1,CNS_COLORS.existing,'#ffffff',1.2);
    }
  }
  if(layers.candidateSiteLayer){
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

  // 7.5) 真实通信铁塔站址（默认关闭，勾选后才进入 plan）：
  //      与起降点复用同一聚合与同一 LOD（map/lod.js 的 towerMarkerMode）：
  //        overview 只显示聚合点（孤立单塔不进入计划，也就不绘制、不可命中）；
  //        medium   仍以聚合为主，聚合不住的孤立塔画铁塔符号（约 16 px 高）；
  //        detail   每个真实铁塔画独立铁塔图标（约 22 px 高）。
  //      尺寸语义是**屏幕像素高度**（MAP-TOWER-SYMBOL-V2）；被 CNS 共塔候选联动的宿主塔
  //      在任何档位都至少按 detail 尺寸绘制并加高亮环，绝不因太小而丢失。
  //      单塔一律画**铁塔矢量符号**，且**绝不铺开 373 个名称标签**（hover/click 才显示）。
  for(const entry of plan.towers||[]){
    const [x,y]=entry.screen||entry.center;
    if(entry.count>1){drawAggregate(ctx,x,y,entry.count,CLUSTER_COLORS.towers);continue;}
    const tower=(entry.anchor&&entry.anchor.tower)||null;
    const highlighted=!!towerHighlight&&!!tower
      &&String(tower.tower_id)===String(towerHighlight);
    const sizePx=highlighted?plan.towerHighlightSizePx:plan.towerSymbolSizePx;
    drawTowerSymbol(ctx,x,y,{
      sizePx,
      color:highlighted?TOWER_HIGHLIGHT_COLOR:TOWER_COLOR.normal,
      strokePx:plan.towerSymbolStrokePx
    });
  }

  // 7.6) CNS 共塔候选（真实铁塔派生的宿主候选）：画成"候选色的铁塔符号"，
  //      与宿主铁塔建立视觉关联，但不画永久连接线；点击候选时由 towerHighlight 高亮宿主塔。
  //      图标几何与真实铁塔**完全相同**（同一 TOWER_SYMBOL），只有语义颜色与高亮环不同。
  for(const entry of plan.cnsTowerCandidates||[]){
    const [x,y]=entry.screen;
    if(!entry.hostTowerId)continue;
    const highlighted=String(towerHighlight||'')===entry.hostTowerId;
    drawTowerSymbol(ctx,x,y,{
      sizePx:plan.towerCandidateSizePx,color:CNS_COLORS.candidate,
      strokePx:plan.towerSymbolStrokePx
    });
    if(highlighted){
      ctx.save();ctx.strokeStyle=CNS_COLORS.candidate;ctx.lineWidth=1.8;
      ctx.beginPath();ctx.arc(x,y,plan.towerHighlightRingRadiusPx,0,Math.PI*2);ctx.stroke();ctx.restore();
    }
  }

  // 7.7) 共塔候选关联的宿主铁塔高亮：只在"当前被关联的那一个塔"周围画一圈。
  //      高亮不是只改颜色：环半径约 15 px，明显大于符号本身，overview/medium/detail 都看得见。
  if(towerHighlight){
    for(const entry of plan.towers||[]){
      const tower=(entry.anchor&&entry.anchor.tower)||null;
      if(!tower||String(tower.tower_id)!==String(towerHighlight))continue;
      const [x,y]=entry.screen||entry.center;
      ctx.save();
      ctx.strokeStyle=TOWER_HIGHLIGHT_COLOR;ctx.lineWidth=2.4;
      ctx.beginPath();ctx.arc(x,y,plan.towerHighlightRingRadiusPx,0,Math.PI*2);ctx.stroke();
      ctx.restore();
    }
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

  // 10) 临时 evidence highlight：视觉权重最高，画在全部正常路线与 candidate 之上。
  //     几何来自后端 RRP segment 的 start_coordinate / end_coordinate（原样绘制，不插值）；
  //     这里不做任何 zoom / layer / LOD 变更，也不写入任何业务状态。
  drawRouteEvidenceHighlight({ctx,view,screenPoint,highlight:routeEvidenceHighlight});
}

/**
 * 临时 evidence highlight：只在 highlight.path 至少有 2 个点时画一条高权重折线。
 * 缺失几何时什么都不画（绝不根据 distance 自行插值出坐标）。
 */
export function drawRouteEvidenceHighlight({ctx,view,screenPoint,highlight}){
  const path=highlight?.path;
  if(!Array.isArray(path)||path.length<2)return 0;
  // 白色描边在下、evidence 色在上：保证在 candidate / scenario 之上仍然可读。
  drawLine(ctx,screenPoint,view,path,'#ffffff',9,[]);
  drawLine(ctx,screenPoint,view,path,ROUTE_EVIDENCE_COLOR,6,[]);
  return path.length;
}