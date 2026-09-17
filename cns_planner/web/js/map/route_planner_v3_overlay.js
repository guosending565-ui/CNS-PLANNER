// Route Planner V3-A map overlay: candidate 2D projection + refinement-window cells.
//
// The panel and this overlay deliberately show only the *2D projection*.  Altitude
// and heading live in the trusted-state details, and the corridor is drawn as a
// search window (never as a safety volume).

export function v3OverlayModel(flow,{candidate=true,corridor=true}={}){
  const collection=flow?.route_planner_v3_experiments||{};
  const summary=collection.active_experiment||null;
  const detail=flow?.route_planner_v3_detail||{};
  const record=(detail.records||[]).find(item=>item.experiment_id===summary?.experiment_id)||null;
  const result=record?.result||null;
  const path=result?.horizontal_projection||[];
  const cells=new Map((flow?.grid?.cells||[]).map(cell=>[cell.grid_id,cell]));
  const supportIds=corridor?(result?.candidate_refinement_corridor?.support_grid_ids||[]):[];
  const centerIds=new Set(result?.candidate_refinement_corridor?.center_grid_ids||[]);
  return {
    status:result?.status||summary?.status||'not_calculated',
    path:candidate?path:[],
    corridorCells:supportIds.map(grid_id=>({grid_id,bbox:cells.get(grid_id)?.bbox||null,center:centerIds.has(grid_id)})).filter(item=>item.bbox),
    semantics:result?.candidate_refinement_corridor?.semantics||null,
    notSafetyCorridor:true,
  };
}

export function drawV3CandidateOverlay({ctx,screenPoint,drawLine,model}){
  if(!model||!model.path.length)return;
  ctx.save();
  for(const cell of model.corridorCells||[]){
    const [west,south,east,north]=cell.bbox;
    const [x0,y0]=screenPoint([west,north]),[x1,y1]=screenPoint([east,south]);
    ctx.fillStyle=cell.center?'#4c6ef524':'#4c6ef510';
    ctx.strokeStyle=cell.center?'#4c6ef5':'#4c6ef566';
    ctx.lineWidth=cell.center?1.4:0.8;
    ctx.fillRect(x0,y0,x1-x0,y1-y0);
    ctx.strokeRect(x0,y0,x1-x0,y1-y0);
  }
  ctx.restore();
  drawLine(ctx,screenPoint,true,model.path,'#4c6ef5',3,[6,4]);
  for(const point of model.path)drawMarker(ctx,screenPoint,point);
}

function drawMarker(ctx,screenPoint,point){
  const [x,y]=screenPoint(point);
  ctx.save();
  ctx.fillStyle='#4c6ef5';
  ctx.beginPath();
  ctx.arc(x,y,3,0,Math.PI*2);
  ctx.fill();
  ctx.restore();
}
