// =========================================================
// 分层候选航路地图层（Layered Route Map Evidence V1）
//
// 职责边界（只做"地图表达"，不碰任何业务状态）：
//  * 只读取 published workflow snapshot 里的 LayeredRouteCandidate；
//  * 只画 status==='candidate' 且 current_applicability==='current' 的 current 候选，
//    stale / blocked / not_calculated 的候选永远不会被画成"当前规划结果"；
//  * **权威几何是 candidate.path**（Theta* V2 的真实起终点 + any-angle 折线）：
//    只要 candidate.path 至少有 2 个有效二维点，就原样绘制，不做简化、不做偏移、
//    不做重采样、不做四舍五入；
//  * 只有"旧记录确实没有 candidate.path"时才允许退回 grid_path → grid cell center，
//    并且必须显式返回 geometrySource='legacy_grid_path_fallback'，绝不静默 fallback；
//  * 视觉权重：candidate 是当前规划结果主线，比 scenario / reference route 更重，
//    但低于临时 evidence highlight（后者由 display_layers.js 绘制）。
//
// 本模块不读取也不写入 zoom / layer / LOD：线宽与透明度由调用方按现有 LOD 传入。
// =========================================================
import {drawLine} from './renderer.js';

export const LAYERED_CANDIDATE_COLORS={
  current:'#123a5c',
};
export const LAYERED_CANDIDATE_SEMANTICS={
  authoritativeGeometry:'candidate.path',
  legacyFallback:'grid_path -> grid cell center',
  legacyFallbackSource:'legacy_grid_path_fallback',
};

/** 分层候选航路的权威几何标识（图例、测试与绘制共用同一份定义）。 */
export const LAYERED_CANDIDATE_GEOMETRY_SOURCE='candidate_path';

//: 当前候选的默认视觉权重。overview / medium / detail 都允许显示 current candidate，
//: 只由既有 LOD 调整线宽与 alpha（这里给的是调用方未提供时的兜底值）。
export const LAYERED_CANDIDATE_STYLE={width:3.4,alpha:1,dash:[]};

/** 一个点是可用于绘制的地理坐标：二维且两个分量都是有限数。 */
export function isDrawableCoordinate(point){
  return Array.isArray(point)&&point.length>=2&&Number.isFinite(point[0])&&Number.isFinite(point[1]);
}

/** candidate.path 是否为"权威且可用"的几何（至少 2 个有效点）。 */
export function candidatePathIsAuthoritative(path){
  return Array.isArray(path)&&path.filter(isDrawableCoordinate).length>=2;
}

/** 只保留有效点，且不改变点序（不做任何几何运算）。 */
function drawablePoints(path){
  return (Array.isArray(path)?path:[]).filter(isDrawableCoordinate).map(point=>[point[0],point[1]]);
}

/**
 * 全部 active/current 的 LayeredRouteCandidate。
 *
 * 判定完全来自后端字段，前端不重新判断候选是否有效：
 *   status==='candidate' && current_applicability==='current'
 */
export function activeLayeredCandidates(flow){
  const collection=flow?.layered_route_candidates||{};
  return (collection.items||[]).filter(item=>
    item&&item.status==='candidate'&&item.current_applicability==='current');
}

/** 当前选中的 lane key（来自规划请求，与 feasibility mask 同一把 key）。 */
function currentLaneKey(flow){
  const request=flow?.layered_route_planning_request||{};
  return (request.scenario_route_id||'od')+'@'+(request.altitude_layer_id||'layer');
}

export function layeredCandidateLaneKey(flow){
  return currentLaneKey(flow);
}

/**
 * 当前候选：active_candidate_id 指向的 current 候选，且属于当前 selected lane。
 *
 * @param {object} flow published workflow snapshot
 * @param {{laneRequired?:boolean}} [options] laneRequired=false 时只按 current 判定
 */
export function currentLayeredRouteCandidate(flow,{laneRequired=true}={}){
  const collection=flow?.layered_route_candidates||{};
  const id=collection.active_candidate_id||null;
  const pool=activeLayeredCandidates(flow);
  const active=(id?pool.filter(item=>item.candidate_id===id):pool);
  const candidate=active[0]||null;
  if(!candidate)return null;
  if(!laneRequired)return candidate;
  const key=currentLaneKey(flow);
  const sameLane=candidate.lane_key
    ?candidate.lane_key===key
    :((candidate.route_id||'od')+'@'+(candidate.altitude_layer_id||'layer')===key);
  return sameLane?candidate:null;
}

/** 按 candidate_id 在 current 候选集合里查找（RRP → 地图联动用）。 */
export function currentLayeredCandidateById(flow,candidateId){
  if(!candidateId)return null;
  return activeLayeredCandidates(flow).find(item=>item.candidate_id===candidateId)||null;
}

/**
 * 旧记录兼容：grid_path（grid_id 序列）→ grid cell center。
 *
 * 只有在 candidate.path 不存在（或不足 2 个有效点）时才会被调用，
 * 并且结果必须带着 geometrySource='legacy_grid_path_fallback' 返回 —— 不静默。
 */
export function legacyGridPathFallback(candidate,{cells=[]}={}){
  const centers=new Map((cells||[]).map(cell=>[String(cell.grid_id),cell.center]));
  const points=(candidate?.grid_path||[]).map(id=>centers.get(String(id))).filter(isDrawableCoordinate);
  if(points.length<2)return null;
  return {
    geometrySource:LAYERED_CANDIDATE_SEMANTICS.legacyFallbackSource,
    legacyGridPathFallback:true,
    path:points.map(point=>[point[0],point[1]]),
    gridIds:(candidate?.grid_path||[]).map(String),
  };
}

/**
 * candidate 的地图几何。
 *
 * @returns {{geometrySource:'candidate_path'|'legacy_grid_path_fallback',path:Array,
 *            legacyGridPathFallback:boolean,gridIds:Array}|null}
 *   null 表示没有任何可绘制几何（绝不编造路径）。
 */
export function candidateMapGeometry(candidate,{cells=[]}={}){
  if(!candidate)return null;
  const path=drawablePoints(candidate.path);
  if(path.length>=2){
    return {
      geometrySource:LAYERED_CANDIDATE_GEOMETRY_SOURCE,
      legacyGridPathFallback:false,
      path,
      gridIds:[],
    };
  }
  return legacyGridPathFallback(candidate,{cells});
}

/** 当前候选 + 其几何（一次性给渲染与测试共用）。 */
export function layeredCandidateOverlayModel(flow,options={}){
  const laneKey=currentLaneKey(flow);
  const candidate=currentLayeredRouteCandidate(flow,options);
  return {
    laneKey,
    candidateId:candidate?.candidate_id||null,
    candidate,
    geometry:candidateMapGeometry(candidate,{cells:options.cells||flow?.grid?.cells||[]}),
  };
}

/**
 * 绘制"分层候选航路"。
 *
 * @param {{ctx,screenPoint,flow,layers,level?,style?}} input
 *   style 只承载线宽 / alpha / dash —— 由调用方按现有 LOD 给出，本模块不改 LOD。
 */
export function drawLayeredCandidateOverlay({
  ctx,screenPoint,flow,grid=null,style=LAYERED_CANDIDATE_STYLE,
}){
  const cells=grid?.cells||flow?.grid?.cells||[];
  const model=layeredCandidateOverlayModel(flow,{cells});
  const geometry=model.geometry;
  if(!geometry)return {drawn:false,points:0,geometrySource:null,legacyGridPathFallback:false,candidateId:model.candidateId};
  const width=Number.isFinite(style?.width)?style.width:LAYERED_CANDIDATE_STYLE.width;
  const alpha=Number.isFinite(style?.alpha)?style.alpha:LAYERED_CANDIDATE_STYLE.alpha;
  const dash=Array.isArray(style?.dash)?style.dash:LAYERED_CANDIDATE_STYLE.dash;
  ctx.save();
  ctx.globalAlpha=alpha;
  drawLine(ctx,screenPoint,true,geometry.path,LAYERED_CANDIDATE_COLORS.current,width,dash);
  ctx.restore();
  return {
    drawn:true,
    points:geometry.path.length,
    geometrySource:geometry.geometrySource,
    legacyGridPathFallback:geometry.legacyGridPathFallback===true,
    candidateId:model.candidateId,
  };
}
