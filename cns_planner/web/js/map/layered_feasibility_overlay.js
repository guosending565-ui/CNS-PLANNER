// Selected-layer coarse feasibility mask overlay for the Layered Risk-Aware Route Planner.
//
// Only the *selected* (scenario route, altitude layer) lane is rendered, and only three
// categorical verdicts exist: feasible / blocked / unknown.  ``unknown`` is drawn as
// "no evidence" — it is never drawn as feasible and never as a numeric 0.
//
// Layered Route Map Evidence V1 split: this module draws the coarse feasibility mask **only**.
// The current LayeredRouteCandidate path moved to ``layered_candidate_overlay.js`` so the mask
// and the candidate route can be switched (and audited) independently.  ``currentLayeredCandidate``
// stays exported for backward compatibility, but nothing here draws a candidate route.
export const LAYERED_FEASIBILITY_COLORS={
  feasible:'#2f9e6f',
  blocked:'#d7263d',
  unknown:'#aeb7c2',
};
export const LAYERED_FEASIBILITY_LABELS={
  feasible:'可行（coarse 垂向包络满足）',
  blocked:'不可行（低于 terrain / building floor）',
  unknown:'证据不足（missing/NoData/未确认）',
};

export function layeredFeasibilityCells(flow){
  const mask=flow?.layered_route_candidates?.masks||{};
  const request=flow?.layered_route_planning_request||{};
  const key=(request.scenario_route_id||'od')+'@'+(request.altitude_layer_id||'layer');
  const item=mask[key];
  if(!item||!item.cells)return [];
  return Object.values(item.cells).map(cell=>({
    gridId:cell.grid_id,status:cell.status,color:LAYERED_FEASIBILITY_COLORS[cell.status]||LAYERED_FEASIBILITY_COLORS.unknown,
  }));
}

export function currentLayeredCandidate(flow){
  const collection=flow?.layered_route_candidates||{};
  const active=(collection.items||[]).find(item=>item.candidate_id&&item.candidate_id===collection.active_candidate_id);
  if(!active||active.status!=='candidate')return null;
  // A stale/not-current candidate is never drawn as the selected-layer path.
  if(active.current_applicability&&active.current_applicability!=='current')return null;
  const request=flow?.layered_route_planning_request||{};
  const key=(request.scenario_route_id||'od')+'@'+(request.altitude_layer_id||'layer');
  const sameLane=active.lane_key?active.lane_key===key:(
    (active.route_id||'od')+'@'+(active.altitude_layer_id||'layer')===key
  );
  return sameLane?active:null;
}

export function layeredFeasibilityLegend(){
  return Object.keys(LAYERED_FEASIBILITY_COLORS).map(status=>({
    status,color:LAYERED_FEASIBILITY_COLORS[status],label:LAYERED_FEASIBILITY_LABELS[status],
  }));
}

function cellPath(ctx,screenPoint,bbox){
  const corners=[[bbox[0],bbox[1]],[bbox[2],bbox[1]],[bbox[2],bbox[3]],[bbox[0],bbox[3]]];
  ctx.beginPath();
  corners.forEach((corner,index)=>{
    const point=screenPoint(corner);
    if(index===0)ctx.moveTo(point[0],point[1]);else ctx.lineTo(point[0],point[1]);
  });
  ctx.closePath();
}

//: 只画 selected-layer coarse feasibility mask。
//: candidate route 已拆到 ``layered_candidate_overlay.js``，本函数**不再**绘制任何候选航路，
//: 返回值里的 ``path`` 因此恒为 0（保留该字段只为兼容既有调用方与审计）。
export function drawLayeredFeasibilityOverlay({ctx,view,screenPoint,flow,grid,gridTheme}){
  if(!view||!flow||!grid?.cells?.length)return {cells:0,path:0};
  const cells=layeredFeasibilityCells(flow);
  let drawn=0;
  for(const item of cells){
    const cell=(grid.cells||[]).find(candidate=>candidate.grid_id===item.gridId);
    if(!cell||!cell.bbox)continue;
    if(!gridTheme.bboxIntersects(cell.bbox,view))continue;
    ctx.save();
    cellPath(ctx,screenPoint,cell.bbox);
    ctx.globalAlpha=item.status==='feasible'?0.34:0.42;
    ctx.fillStyle=item.color;
    ctx.fill();
    ctx.restore();
    drawn+=1;
  }
  return {cells:drawn,path:0};
}
