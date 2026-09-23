// Radar Surveillance Layout V1 map overlay —「80m固定高度航路方向性雷达几何初步划设方案」。
//
// 绘制内容（**只画已选方案**，避免性能问题）：
//   1. 所有候选铁塔（弱化小点）；
//   2. selected towers（强调）；
//   3. selected panel 在 80 m 平面的有效方向扇区 / 环带（inner/outer radius + ±45°）；
//   4. 航路按覆盖结果着色：satisfied / under_redundant / uncovered / unknown。
//
// 明确不画：所有未选 panel 的 coverage polygon（会一次性产生数百个多边形，造成地图卡顿）。
// 地图无旋转（正北朝上），因此方位角 = 屏幕上自正北顺时针的角度。
export const RADAR_COVERAGE_COLORS={
  satisfied:'#1f8a4c',
  under_redundant:'#d98b0b',
  uncovered:'#c62828',
  unknown:'#8b949e',
};

export const RADAR_COVERAGE_LABELS={
  satisfied:'满足（达到要求的独立站址数）',
  under_redundant:'冗余不足（站址数少于要求）',
  uncovered:'未覆盖（0 个站址）',
  unknown:'证据不足（地表分类未知，fail-closed）',
};

export const RADAR_PANEL_COLORS={radar_i:'#1565c0',radar_ii:'#6a1b9a'};

/** EPSG:3857 projected units/pixel → local ground metres/pixel. */
export function groundMetresPerPixel(projectedUnitsPerPixel,latitudeDeg){
  const resolution=Number(projectedUnitsPerPixel);
  const latitude=Number(latitudeDeg);
  if(!Number.isFinite(resolution)||resolution<=0)return 1;
  if(!Number.isFinite(latitude))return resolution;
  const scale=Math.cos(Math.max(-85.05112878,Math.min(85.05112878,latitude))*Math.PI/180);
  return resolution*Math.max(scale,1e-9);
}

export function radarSectorPixelRadius(radiusM,view,latitudeDeg){
  return Number(radiusM)/groundMetresPerPixel(view?.res,latitudeDeg);
}

export function radarCoverageLegend(){
  return Object.keys(RADAR_COVERAGE_COLORS).map(status=>({
    status,color:RADAR_COVERAGE_COLORS[status],label:RADAR_COVERAGE_LABELS[status],
  }));
}

function sectorPath(ctx,center,innerPx,outerPx,azimuthDeg,halfWidthDeg){
  // 屏幕上正北朝上：dx=sin(azimuth)，dy=-cos(azimuth)。
  const toScreen=(azimuth,r)=>{
    const radians=azimuth*Math.PI/180;
    return [center[0]+Math.sin(radians)*r,center[1]-Math.cos(radians)*r];
  };
  const start=azimuthDeg-halfWidthDeg,end=azimuthDeg+halfWidthDeg;
  const steps=12;
  ctx.beginPath();
  if(innerPx<=0.5){
    ctx.moveTo(center[0],center[1]);
    for(let index=0;index<=steps;index+=1){
      const point=toScreen(start+(end-start)*index/steps,outerPx);
      ctx.lineTo(point[0],point[1]);
    }
  }else{
    let point=toScreen(start,outerPx);
    ctx.moveTo(point[0],point[1]);
    for(let index=1;index<=steps;index+=1){
      point=toScreen(start+(end-start)*index/steps,outerPx);
      ctx.lineTo(point[0],point[1]);
    }
    for(let index=steps;index>=0;index-=1){
      point=toScreen(start+(end-start)*index/steps,innerPx);
      ctx.lineTo(point[0],point[1]);
    }
  }
  ctx.closePath();
}

/**
 * 绘制雷达初步划设 overlay。
 *
 * ``model`` 来自 ``radarOverlayModel(flow, candidateTowers)``；
 * ``screenPoint`` 是 ``map/projection`` 提供的经纬度 → 屏幕坐标投影。
 */
export function drawRadarLayoutOverlay({ctx,view,screenPoint,model,radiusScaleM=1}){
  if(!view||!model)return {candidateTowers:0,selectedTowers:0,panels:0,routeSegments:0,legend:radarCoverageLegend()};
  const drawn={candidateTowers:0,selectedTowers:0,panels:0,routeSegments:0,sectorsClipped:0};

  // 1) 候选铁塔（弱化）
  ctx.save();
  ctx.globalAlpha=0.28;
  ctx.fillStyle='#1f7a8c';
  for(const tower of model.candidateTowers||[]){
    if(!Array.isArray(tower?.coordinate))continue;
    const [x,y]=screenPoint(tower.coordinate);
    if(!Number.isFinite(x)||!Number.isFinite(y))continue;
    ctx.beginPath();
    ctx.arc(x,y,1.6,0,Math.PI*2);
    ctx.fill();
    drawn.candidateTowers+=1;
  }
  ctx.restore();

  // 2) selected panel 的 80 m 平面有效方向扇区 / 环带
  for(const panel of model.panels||[]){
    const [x,y]=screenPoint(panel.coordinate);
    if(!Number.isFinite(x)||!Number.isFinite(y))continue;
    const latitude=panel.coordinate[1];
    const innerPx=radarSectorPixelRadius(
      panel.displayRadiusInnerM*radiusScaleM,view,latitude
    );
    const outerPx=radarSectorPixelRadius(
      panel.displayRadiusOuterM*radiusScaleM,view,latitude
    );
    if(outerPx<2)continue;
    ctx.save();
    sectorPath(ctx,[x,y],Math.min(innerPx,outerPx),outerPx,panel.azimuth_deg,panel.half_width_deg);
    ctx.globalAlpha=0.14;
    ctx.fillStyle=RADAR_PANEL_COLORS[panel.radar_type]||RADAR_PANEL_COLORS.radar_i;
    ctx.fill();
    ctx.globalAlpha=0.55;
    ctx.strokeStyle=RADAR_PANEL_COLORS[panel.radar_type]||RADAR_PANEL_COLORS.radar_i;
    ctx.lineWidth=1;
    ctx.stroke();
    ctx.restore();
    drawn.panels+=1;
  }

  // 3) selected towers（强调）
  ctx.save();
  ctx.strokeStyle='#0d3b66';
  ctx.fillStyle='#ffffff';
  ctx.lineWidth=1.6;
  for(const tower of model.selectedTowers||[]){
    const [x,y]=screenPoint(tower.coordinate);
    if(!Number.isFinite(x)||!Number.isFinite(y))continue;
    ctx.beginPath();
    ctx.arc(x,y,3.4,0,Math.PI*2);
    ctx.fill();
    ctx.stroke();
    drawn.selectedTowers+=1;
  }
  ctx.restore();

  // 4) 航路按覆盖结果着色（消费后端已合并的 coverage_profile 线段，不在前端重算几何）
  ctx.save();
  ctx.lineWidth=3.2;
  ctx.lineCap='round';
  for(const segment of model.routeCoverageColours||[]){
    const [x1,y1]=screenPoint(segment.from);
    const [x2,y2]=screenPoint(segment.to);
    if(![x1,y1,x2,y2].every(Number.isFinite))continue;
    ctx.strokeStyle=RADAR_COVERAGE_COLORS[segment.status]||RADAR_COVERAGE_COLORS.unknown;
    ctx.beginPath();
    ctx.moveTo(x1,y1);
    ctx.lineTo(x2,y2);
    ctx.stroke();
    drawn.routeSegments+=1;
  }
  ctx.restore();

  return {...drawn,legend:radarCoverageLegend()};
}
