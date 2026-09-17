// Route Planner V3-A/V3-B map overlay: coarse strategic candidate + refinement corridor
// + V3-B corridor-local refined candidate.
//
// The panel and this overlay deliberately show only the *2D projection*.  Altitude
// and heading live in the trusted-state details, and the corridor is drawn as a
// search window (never as a safety volume).  The V3-B refined projection is a
// solid line in a distinct colour: it is an engineering refinement inside one
// strategic candidate's corridor, **not** a validated operational route -- the
// exact polygon/terrain/continuous-clearance validation is V3-C.

const COARSE_COLOR = '#4c6ef5';
const REFINED_COLOR = '#e8590c';
//: V3-C realized route: the *continuous* geometry (analytic arcs, linearized for
//: display) with its explicit chord-error envelope.  It is still not an operational
//: route, so it is drawn as a distinct validated-style line, never as a route of record.
const VALIDATED_COLOR = '#2f9e44';
const VALIDATION_FAILURE_COLOR = '#c92a2a';
const VALIDATION_UNRESOLVED_COLOR = '#f08c00';

export function v3OverlayModel(flow,{candidate=true,corridor=true,refined=true,validated=true}={}){
  const collection=flow?.route_planner_v3_experiments||{};
  const summary=collection.active_experiment||null;
  const detail=flow?.route_planner_v3_detail||{};
  const records=detail.records||[];
  const activeId=summary?.experiment_id||detail.active_experiment_id||null;
  const record=records.find(item=>item.experiment_id===activeId)||records[0]||null;
  const result=record?.result||null;
  const path=result?.horizontal_projection||[];
  const cells=new Map((flow?.grid?.cells||[]).map(cell=>[cell.grid_id,cell]));
  const supportIds=corridor?(result?.candidate_refinement_corridor?.support_grid_ids||[]):[];
  const centerIds=new Set(result?.candidate_refinement_corridor?.center_grid_ids||[]);
  // V3-A stores refinements newest-first, so refinements[0] is the latest one.
  const refinement=refined?((record?.refinements||[])[0]||null):null;
  const refinementResult=refinement?.result||null;
  const evidence=refinementResult?.fine_grid_evidence||{};
  const fineGrid=refinementResult?.fine_grid||null;
  const validation=validated?((refinement?.validations||[])[0]||null):null;
  const validationResult=validation?.result||null;
  const continuous=validationResult?.continuous_route||null;
  const linearized=((continuous||{}).horizontal_geometry||{}).linearized||{};
  const analytic=((continuous||{}).horizontal_geometry||{}).analytic||{};
  const projected=(flow?.route_planner_v3_validations?.items||[])[0]||null;
  // The realized route lives in the V3-B *local metric* frame.  Every state of the
  // refined candidate carries both metric and geographic coordinates, so the affine
  // mapping can be fitted and the realized linearized LineString placed honestly --
  // and skipped (never guessed) when the fit is not available.
  const project=affineFromStatePath(refinementResult);
  const metricPairs=((refinementResult?.state_path)||[])
    .map(item=>({metric:[item.x_metric,item.y_metric],geographic:[item.x,item.y]}))
    .filter(item=>item.metric.every(Number.isFinite)&&item.geographic.every(Number.isFinite));
  const projectMetric=project?project:affineProjection(metricPairs);
  const validatedPath=validated&&projectMetric
    ?(linearized.linestring_metric||[]).map(point=>projectMetric(point))
      .filter(point=>point&&point.every(Number.isFinite))
    :[];
  const validatedIntervalSegments=validated&&projectMetric
    ?summarizeIntervals(validationResult).map(item=>({
      ...item,
      startCoordinate:pointAtMetricDistance(linearized.linestring_metric||[],projectMetric,item.startDistanceM),
      endCoordinate:pointAtMetricDistance(linearized.linestring_metric||[],projectMetric,item.endDistanceM),
    }))
    :summarizeIntervals(validationResult);
  return {
    status:result?.status||summary?.status||'not_calculated',
    path:candidate?path:[],
    corridorCells:supportIds.map(grid_id=>({grid_id,bbox:cells.get(grid_id)?.bbox||null,center:centerIds.has(grid_id)})).filter(item=>item.bbox),
    semantics:result?.candidate_refinement_corridor?.semantics||null,
    notSafetyCorridor:true,
    refinedPath:refinementResult?.horizontal_projection||[],
    refinedStatus:refinementResult?.status||null,
    refinedCellCount:evidence.cell_count??fineGrid?.cell_count??null,
    refinedResolutionM:evidence.resolution_m??fineGrid?.resolution_m??null,
    refinedId:refinement?.refinement_id||null,
    refinedIsFinal:false,
    refinedV3cPending:true,
    refinedGridBounds:refinedGridBounds(refinementResult),
    // ---- V3-C realized route + validation evidence -------------------------
    validatedStatus:validationResult?.status||null,
    validatedId:validation?.validation_id||null,
    validatedIsOperationalRoute:false,
    validatedCnsAssessed:false,
    validatedPointCount:linearized.point_count??null,
    validatedArcCount:analytic.arc_count??null,
    curveChordErrorM:linearized.curve_chord_error_m??null,
    validatedPath,
    validatedGeometryPlaceable:Boolean(validatedPath.length),
    validatedGeometrySemantics:'realized_c1_linearized_representation_analytic_arcs_with_explicit_chord_error',
    validatedDomainStatuses:(validationResult?.domain_statuses)||(projected?.domain_statuses)||{},
    validatedIntervals:validatedIntervalSegments,
    validatedMargins:validationResult?.min_margins||null,
  };
}

//: The refined state path supplies (metric, geographic) pairs.  Three or more spread
//: points give the full affine fit; exactly two give the scale/translation fit that the
//: V3-B local metric frame actually defines (uniform local scale, north-up), computed
//: from the pair itself.  With fewer than two pairs nothing is placed -- never guessed.
function affineFromStatePath(result){
  const pairs=((result?.state_path)||[])
    .map(item=>({metric:[item.x_metric,item.y_metric],geographic:[item.x,item.y]}))
    .filter(item=>item.metric.every(Number.isFinite)&&item.geographic.every(Number.isFinite));
  const affine=affineProjection(pairs);
  if(affine)return affine;
  return scaleTranslationProjection(pairs);
}

function scaleTranslationProjection(pairs){
  if((pairs||[]).length<2)return null;
  const [a,b]=pairs;
  const metricDistance=Math.hypot(b.metric[0]-a.metric[0],b.metric[1]-a.metric[1]);
  const geographicDistance=Math.hypot(b.geographic[0]-a.geographic[0],b.geographic[1]-a.geographic[1]);
  if(!(metricDistance>0)||!(geographicDistance>0))return null;
  const scale=geographicDistance/metricDistance;
  const offsetX=a.geographic[0]-a.metric[0]*scale;
  const offsetY=a.geographic[1]-a.metric[1]*scale;
  return metric=>[offsetX+metric[0]*scale,offsetY+metric[1]*scale];
}

//: Point on the linearized polyline at an along-track distance, then projected.
function pointAtMetricDistance(points, project, distanceM){
  if(!points.length||!Number.isFinite(distanceM))return null;
  let travelled=0;
  for(let index=0;index<points.length-1;index++){
    const a=points[index],b=points[index+1];
    const length=Math.hypot(b[0]-a[0],b[1]-a[1]);
    if(travelled+length>=distanceM||index===points.length-2){
      const ratio=length<=0?0:Math.max(0,Math.min(1,(distanceM-travelled)/length));
      const metric=[a[0]+(b[0]-a[0])*ratio,a[1]+(b[1]-a[1])*ratio];
      const projected=project(metric);
      return projected&&projected.every(Number.isFinite)?projected:null;
    }
    travelled+=length;
  }
  return null;
}

//: Failed / unresolved along-track intervals as distance ranges.  The map has no
//: distance→position mapping for the realized geometry, so the overlay reports the
//: intervals as data instead of drawing a guessed segment.
function summarizeIntervals(result){
  if(!result)return [];
  const map=(items,kind)=>(items||[]).map(item=>({
    kind,domain:item.domain,reasonId:item.reason_id,
    startDistanceM:item.start_distance_m,endDistanceM:item.end_distance_m,
    margin:item.margin??null,
  }));
  return [
    ...map(result.violations,'violation'),
    ...map(result.unresolved_evidence,'unresolved'),
  ];
}

//: The fine grid's geographic extent, or ``null`` when the payload cannot supply it.
//
// The fine frame records a *metric* bbox plus, at most, a description of the
// local → geographic mapping -- never the transform coefficients.  So the only
// honest way to place the rectangle is to fit the affine mapping from the
// refined state path itself (each state carries both ``x_metric``/``y_metric`` and
// ``x``/``y``).  We skip the rectangle rather than guess whenever the frame does
// not declare a mapping, when the mapping is itself an interpolation of parent
// cells, or when the available points are (nearly) collinear.
function refinedGridBounds(result){
  const frame=result?.frame||null,metricBounds=frame?.metric_bounds;
  const mapping=frame?.local_to_geographic||{};
  if(!frame||!Array.isArray(metricBounds)||metricBounds.length!==4)return null;
  if(!metricBounds.every(value=>Number.isFinite(value)))return null;
  if(!mapping.method||mapping.interpolated_from_parent_cells===true)return null;
  const pairs=(result?.state_path||[]).map(item=>({metric:[item.x_metric,item.y_metric],geographic:[item.x,item.y]}))
    .filter(item=>item.metric.every(Number.isFinite)&&item.geographic.every(Number.isFinite));
  const project=affineProjection(pairs);
  if(!project)return null;
  const corners=[[metricBounds[0],metricBounds[1]],[metricBounds[2],metricBounds[1]],[metricBounds[2],metricBounds[3]],[metricBounds[0],metricBounds[3]]].map(project);
  const xs=corners.map(point=>point[0]),ys=corners.map(point=>point[1]);
  return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
}

function triangleArea(a,b,c){
  return Math.abs((b[0]-a[0])*(c[1]-a[1])-(c[0]-a[0])*(b[1]-a[1]));
}

function affineProjection(pairs){
  if((pairs||[]).length<3)return null;
  let best=null;
  for(let i=1;i<pairs.length;i++)for(let j=i+1;j<pairs.length;j++){
    const area=triangleArea(pairs[0].metric,pairs[i].metric,pairs[j].metric);
    if(!best||area>best.area)best={area,points:[pairs[0],pairs[i],pairs[j]]};
  }
  const xs=pairs.map(item=>item.metric[0]),ys=pairs.map(item=>item.metric[1]);
  const span=Math.max((Math.max(...xs)-Math.min(...xs))*(Math.max(...ys)-Math.min(...ys)),Number.EPSILON);
  if(!best||best.area/span<1e-3)return null;
  const [p0,p1,p2]=best.points;
  const determinant=(p1.metric[0]-p0.metric[0])*(p2.metric[1]-p0.metric[1])-(p2.metric[0]-p0.metric[0])*(p1.metric[1]-p0.metric[1]);
  if(!Number.isFinite(determinant)||Math.abs(determinant)<Number.EPSILON)return null;
  const coefficients=[0,1].map(axis=>{
    const v0=p0.geographic[axis],v1=p1.geographic[axis],v2=p2.geographic[axis];
    const b=((v1-v0)*(p2.metric[1]-p0.metric[1])-(v2-v0)*(p1.metric[1]-p0.metric[1]))/determinant;
    const c=((v2-v0)*(p1.metric[0]-p0.metric[0])-(v1-v0)*(p2.metric[0]-p0.metric[0]))/determinant;
    return {a:v0-b*p0.metric[0]-c*p0.metric[1],b,c};
  });
  return metric=>coefficients.map(axis=>axis.a+axis.b*metric[0]+axis.c*metric[1]);
}

export function drawV3CandidateOverlay({ctx,screenPoint,drawLine,model}){
  if(!model)return;
  const path=model.path||[],refinedPath=model.refinedPath||[];
  if(refinedPath.length&&model.refinedGridBounds)drawFineGridExtent(ctx,screenPoint,model.refinedGridBounds);
  if(path.length){
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
    drawLine(ctx,screenPoint,true,path,COARSE_COLOR,3,[6,4]);
    for(const point of path)drawMarker(ctx,screenPoint,point);
  }
  if(refinedPath.length){
    drawLine(ctx,screenPoint,true,refinedPath,REFINED_COLOR,3,[]);
    for(const point of refinedPath)drawRefinedMarker(ctx,screenPoint,point);
  }
  drawValidatedOverlay(ctx,screenPoint,drawLine,model);
}

//: The realized continuous route (solid), its failed/unresolved along-track intervals
//: (thick markers) and its status colour.  The line stays a *validated* route, never an
//: operational one -- the panel carries the operational/CNS disclaimer.
function drawValidatedOverlay(ctx,screenPoint,drawLine,model){
  const validatedPath=model.validatedPath||[];
  if(!validatedPath.length)return;
  const colour=model.validatedStatus==='validated_route'?VALIDATED_COLOR
    :model.validatedStatus==='failed'?VALIDATION_FAILURE_COLOR
    :model.validatedStatus==='unresolved'?VALIDATION_UNRESOLVED_COLOR
    :VALIDATED_COLOR;
  drawLine(ctx,screenPoint,true,validatedPath,colour,3,[]);
  for(const item of model.validatedIntervals||[]){
    if(!item.startCoordinate)continue;
    const itemColour=item.kind==='violation'?VALIDATION_FAILURE_COLOR:VALIDATION_UNRESOLVED_COLOR;
    drawIntervalSpan(ctx,screenPoint,drawLine,item.startCoordinate,item.endCoordinate,itemColour);
  }
  drawValidatedMarker(ctx,screenPoint,validatedPath);
}

function drawIntervalSpan(ctx,screenPoint,drawLine,start,end,colour){
  const [x0,y0]=screenPoint(start),[x1,y1]=screenPoint(end);
  ctx.save();
  ctx.fillStyle=colour;
  ctx.beginPath();
  ctx.arc(x0,y0,6,0,Math.PI*2);
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x1,y1,6,0,Math.PI*2);
  ctx.fill();
  ctx.strokeStyle=colour;
  ctx.lineWidth=6;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(x0,y0);
  ctx.lineTo(x1,y1);
  ctx.stroke();
  ctx.restore();
}

function drawValidatedMarker(ctx,screenPoint,path){
  const point=path[Math.floor(path.length/2)];
  if(!point)return;
  const [x,y]=screenPoint(point);
  ctx.save();
  ctx.fillStyle='#ffffff';
  ctx.strokeStyle=VALIDATED_COLOR;
  ctx.lineWidth=2;
  ctx.beginPath();
  ctx.arc(x,y,4,0,Math.PI*2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawFineGridExtent(ctx,screenPoint,bounds){
  const [west,south,east,north]=bounds;
  const [x0,y0]=screenPoint([west,north]),[x1,y1]=screenPoint([east,south]);
  ctx.save();
  ctx.strokeStyle=REFINED_COLOR;ctx.lineWidth=1;ctx.setLineDash([2,3]);
  ctx.strokeRect(x0,y0,x1-x0,y1-y0);
  ctx.restore();
}

function drawMarker(ctx,screenPoint,point){
  const [x,y]=screenPoint(point);
  ctx.save();
  ctx.fillStyle=COARSE_COLOR;
  ctx.beginPath();
  ctx.arc(x,y,3,0,Math.PI*2);
  ctx.fill();
  ctx.restore();
}

function drawRefinedMarker(ctx,screenPoint,point){
  const [x,y]=screenPoint(point);
  ctx.save();
  ctx.fillStyle='#ffffff';
  ctx.strokeStyle=REFINED_COLOR;
  ctx.lineWidth=2;
  ctx.beginPath();
  ctx.rect(x-3,y-3,6,6);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}
