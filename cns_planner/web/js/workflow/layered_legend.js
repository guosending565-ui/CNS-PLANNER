import {layeredFeasibilityLegend} from '../map/layered_feasibility_overlay.js';
import {layeredCandidateOverlayModel,LAYERED_CANDIDATE_COLORS} from '../map/layered_candidate_overlay.js';

// Layered Risk-Aware Route Planner V1 map legend.  The selected layer's coarse feasibility
// mask is categorical: ``unknown`` renders as "no evidence" and is never painted as feasible
// and never as a numeric 0.  Kept out of ``main.js`` so the assembly module stays thin.
export const LAYERED_FEASIBILITY_LABEL='Selected layer 可行性（coarse 垂向包络）';
export const LAYERED_CANDIDATE_LABEL='分层候选航路（current LayeredRouteCandidate）';

export function layeredFeasibilityLegendModel(flow){
  const request=flow?.layered_route_planning_request||{};
  const key=(request.scenario_route_id||'od')+'@'+(request.altitude_layer_id||'layer');
  const mask=(flow?.layered_route_candidates?.masks||{})[key]||null;
  const counts=mask?.counts||{};
  return {
    altitudeLayerId:request.altitude_layer_id||null,
    status:mask?.status||'not_calculated',
    currentApplicability:mask?.current_applicability||'stale',
    counts:{
      feasible:Number(counts.feasible)||0,
      blocked:Number(counts.blocked)||0,
      unknown:Number(counts.unknown)||0,
    },
    items:layeredFeasibilityLegend(),
  };
}

export function timelineNote(model){
  return '非 exact footprint；水平/精确净空留给后续连续验证。适飞空域仍 display_only。';
}

export function renderLayeredFeasibilityLegend(target,flow,formatNumber){
  if(!target)return false;
  const model=layeredFeasibilityLegendModel(flow);
  const rows=model.items.map(item=>
    '<div class="layer-row"><span><i class="swatch" style="background:'+item.color+'"></i> '+item.label+
    '</span><b>'+formatNumber(model.counts[item.status])+'</b></div>').join('');
  target.innerHTML='<div class="section-label">'+LAYERED_FEASIBILITY_LABEL+'</div>'+rows+
    '<div class="small">'+(model.altitudeLayerId||'未选择高度层')+' · '+model.status+
    ' · '+model.currentApplicability+' · '+timelineNote(model)+'</div>';
  target.hidden=false;
  return true;
}

//: candidate 图例的几何来源措辞：权威路径优先，旧记录才允许显式 legacy fallback。
export const LAYERED_CANDIDATE_GEOMETRY_NOTE='优先 candidate.path（Theta* V2 真实起终点 + any-angle）；'
  +'只有旧记录没有 candidate.path 时才退回 grid_path → grid cell center，并显式标注 legacy fallback。';

export function layeredCandidateLegendModel(flow){
  const model=layeredCandidateOverlayModel(flow,{cells:flow?.grid?.cells||[]});
  return {
    label:LAYERED_CANDIDATE_LABEL,
    color:LAYERED_CANDIDATE_COLORS.current,
    candidateId:model.candidateId,
    laneKey:model.laneKey,
    geometrySource:model.geometry?model.geometry.geometrySource:null,
    legacyGridPathFallback:model.geometry?model.geometry.legacyGridPathFallback===true:false,
    pointCount:model.geometry?model.geometry.path.length:0,
    available:Boolean(model.geometry),
  };
}

/** 分层候选航路的图例：只说明当前画的是什么，不做任何规划结论。 */
export function renderLayeredCandidateLegend(target,flow){
  if(!target)return false;
  const model=layeredCandidateLegendModel(flow);
  const label=model.legacyGridPathFallback
    ?'legacy grid_path fallback（旧记录没有 candidate.path）'
    :'candidate.path（权威几何）';
  target.innerHTML='<div class="section-label">'+LAYERED_CANDIDATE_LABEL+'</div>'
    +'<div class="layer-row"><span><i class="swatch" style="background:'+model.color+'"></i> '
    +(model.available?label:'没有 current candidate')+'</span></div>'
    +'<div class="small">'+LAYERED_CANDIDATE_GEOMETRY_NOTE+'</div>'
    +'<div class="small">'+(model.candidateId||'—')+' · '+(model.laneKey||'—')
    +' · geometry '+((model.geometrySource)||'—')+' · points '+model.pointCount+'</div>';
  target.hidden=false;
  return true;
}

/**
 * 图层抽屉里的两个 layered 图例：各自独立跟随自己的开关，一个关闭不影响另一个；
 * 两个都打开时两个图例都在（它们在图例区各自占一行，互不覆盖）。
 *
 * @param {{$,flow,formatNumber}} input
 * @returns {boolean} 是否有任一层 layered 图例被渲染（调用方据此让出网格图例）
 */
export function updateLayeredLegends({$,flow,formatNumber}){
  const feasibilityTarget=$('layeredFeasibilityLegend'),candidateTarget=$('layeredCandidateLegend');
  let rendered=false;
  if($('layeredFeasibilityLayer')?.checked===false){
    if(feasibilityTarget)feasibilityTarget.hidden=true;
  }else if(renderLayeredFeasibilityLegend(feasibilityTarget,flow,formatNumber)){
    rendered=true;
  }
  if($('layeredCandidateLayer')?.checked===false){
    if(candidateTarget)candidateTarget.hidden=true;
  }else if(renderLayeredCandidateLegend(candidateTarget,flow)){
    rendered=true;
  }
  return rendered;
}
