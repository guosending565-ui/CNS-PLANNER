/**
 * 绘制只读参考层（真实参考航线 + 真实航路点）。
 *
 * 线宽、透明度、点半径与标签门槛由调用方按地图显示分级（map/lod.js）给出，
 * 因此同一份 geometry 在 overview / medium / detail 下有不同的视觉权重；
 * 参考层本身仍然是"只读参考"，不参与任何业务约束。
 */
export function drawReferenceOverlay({ctx,view,screenPoint,drawLine,routes,points,
  routeWidth=1,routeAlpha=.3,routeDash=[10,5,2,5],
  pointRadius=2.4,pointAlpha=.55,labelMode='hidden',maxLabels=6}){
  const labels=[];
  ctx.save();
  ctx.globalAlpha=routeAlpha;
  for(const route of routes||[]){
    drawLine(ctx,screenPoint,view,route.path,'#c83f8c',routeWidth,routeDash);
  }
  ctx.restore();
  if(labelMode!=='hidden'){
    let placed=0;
    for(const route of routes||[]){
      if(placed>=maxLabels)break;
      if(!(route.path||[]).length)continue;
      const screen=screenPoint(route.path[Math.floor(route.path.length/2)]);
      if(drawLabel(ctx,route.name||route.route_number,screen[0]+7,screen[1]-7,labels))placed++;
    }
  }
  ctx.save();
  ctx.globalAlpha=pointAlpha;
  for(const point of points||[])drawPoint(ctx,view,screenPoint,point,labels,{pointRadius,labelMode});
  ctx.restore();
}

function diagnostic(collection,count,label){
  const status=collection?.status||'not_calculated';
  let reason=status;
  if(status==='requires_xlsx_or_csv_conversion')reason='ET 需先转换为 XLSX/CSV';
  else if(status==='not_calculated')reason='未配置来源';
  else if(status==='missing_data')reason='来源中没有可用记录';
  else if(status==='passed')reason='已载入';
  return {count:Number(count||0),status,reason,label:label+' '+Number(count||0)+' · '+reason};
}

export function referenceLayerDiagnostics(flow){
  const routes=flow?.reference_routes||{},landing=flow?.reference_landing_sites||{},towers=flow?.towers||{};
  return {
    routes:diagnostic(routes,routes.count,'航线'),
    points:diagnostic(routes,routes.point_count??routes.points?.length,'航路点'),
    landingSites:diagnostic(landing,landing.count,'起降点'),
    towers:diagnostic(towers,towers.count,'铁塔站址'),
  };
}

function drawLabel(ctx,value,x,y,occupied){
  const text=String(value||'').trim();if(!text)return false;
  const width=Math.min(150,Math.max(28,text.length*11)),box=[x-2,y-12,x+width,y+3];
  if(occupied.some(other=>!(box[2]<other[0]||box[0]>other[2]||box[3]<other[1]||box[1]>other[3])))return false;
  occupied.push(box);ctx.save();ctx.font='600 10px Segoe UI';ctx.fillStyle='#fff';ctx.strokeStyle='#fff';ctx.lineWidth=3;ctx.strokeText(text,x,y);ctx.fillStyle='#74315d';ctx.fillText(text,x,y);ctx.restore();
  return true;
}

function drawPoint(ctx,view,screenPoint,point,labels,{pointRadius=2.4,labelMode='hidden'}={}){
  if(!Array.isArray(point.coordinate))return;const [x,y]=screenPoint(point.coordinate),endpoint=point.position==='endpoint';ctx.save();ctx.fillStyle=endpoint?'#f08a24':'#fff3c4';ctx.strokeStyle='#783b69';ctx.lineWidth=endpoint?2:1.2;ctx.beginPath();
  if(endpoint)ctx.arc(x,y,pointRadius+1.4,0,Math.PI*2);else{ctx.moveTo(x,y-pointRadius-1);ctx.lineTo(x+pointRadius+1,y+pointRadius);ctx.lineTo(x-pointRadius-1,y+pointRadius);ctx.closePath();}ctx.fill();ctx.stroke();ctx.restore();
  if(labelMode==='detail'&&endpoint)drawLabel(ctx,point.name||String(point.sequence),x+7,y-6,labels);
}

export function hitReferenceObject(click,overlay,screenPoint){
  let best=null,bestDistance=Infinity;
  for(const point of overlay.referencePoints||[]){const screen=screenPoint(point.coordinate),distance=Math.hypot(click[0]-screen[0],click[1]-screen[1]);if(distance<10&&distance<bestDistance){bestDistance=distance;best={kind:'point',id:point.reference_route_point_id};}}
  if(best)return best;
  for(const route of overlay.referenceRoutes||[]){const screens=(route.path||[]).map(screenPoint);for(let index=1;index<screens.length;index++){const distance=segmentDistance(click,screens[index-1],screens[index]);if(distance<7&&distance<bestDistance){bestDistance=distance;best={kind:'route',id:route.reference_route_id};}}}
  return best;
}

function segmentDistance(point,left,right){
  const dx=right[0]-left[0],dy=right[1]-left[1],length=dx*dx+dy*dy;if(!length)return Math.hypot(point[0]-left[0],point[1]-left[1]);const t=Math.max(0,Math.min(1,((point[0]-left[0])*dx+(point[1]-left[1])*dy)/length)),x=left[0]+t*dx,y=left[1]+t*dy;return Math.hypot(point[0]-x,point[1]-y);
}
