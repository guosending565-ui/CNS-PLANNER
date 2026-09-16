export function drawBuildingClearanceOverlay({ctx,screenPoint,drawLine,assessment}){
  if(!assessment||assessment.status==='stale')return;
  for(const segment of assessment.breach_segments||[])drawLine(ctx,screenPoint,true,segment.path,'#d7263d',7);
  for(const building of assessment.critical_buildings||[])drawGeometry(ctx,screenPoint,building.geometry,'#d7263d');
}

function drawGeometry(ctx,screenPoint,geometry,color){
  if(!geometry?.coordinates)return;
  const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.type==='MultiPolygon'?geometry.coordinates:[];
  ctx.save();ctx.strokeStyle=color;ctx.fillStyle=color+'24';ctx.lineWidth=2;
  for(const polygon of polygons)for(const ring of polygon){ctx.beginPath();ring.forEach((point,index)=>{const [x,y]=screenPoint(point);index?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();ctx.fill();ctx.stroke();}
  ctx.restore();
}
