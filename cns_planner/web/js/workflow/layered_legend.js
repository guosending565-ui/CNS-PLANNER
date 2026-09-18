import {layeredFeasibilityLegend} from '../map/layered_feasibility_overlay.js';

// Layered Risk-Aware Route Planner V1 map legend.  The selected layer's coarse feasibility
// mask is categorical: ``unknown`` renders as "no evidence" and is never painted as feasible
// and never as a numeric 0.  Kept out of ``main.js`` so the assembly module stays thin.
export const LAYERED_FEASIBILITY_LABEL='Selected layer 可行性（coarse 垂向包络）';

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
