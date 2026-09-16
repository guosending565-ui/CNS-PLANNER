export function drawConfirmedAllowedAirspace(ctx,screenPoint,airspace){
  const features=(airspace?.airspace_eligibility?.features||airspace?.features||[]).filter(item=>item.route_eligibility==='allowed'&&item.policy_confirmed===true);
  ctx.save();ctx.fillStyle='#35b86b2e';ctx.strokeStyle='#168649';ctx.lineWidth=2;ctx.setLineDash([4,3]);
  for(const feature of features)drawGeometry(ctx,screenPoint,feature.geometry);
  ctx.restore();
}

function drawGeometry(ctx,screenPoint,geometry){
  const groups=geometry?.type==='Polygon'?[geometry.coordinates]:geometry?.type==='MultiPolygon'?geometry.coordinates:[];
  for(const rings of groups||[]){
    ctx.beginPath();
    for(const ring of rings||[]){
      ring.forEach((coordinate,index)=>{const [x,y]=screenPoint(coordinate);if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);});
      ctx.closePath();
    }
    ctx.fill('evenodd');ctx.stroke();
  }
}
