// MAP-TOWER-SYMBOL-V2：通信铁塔地图符号的尺寸模型、桁架几何、LOD、命中半径与图例一致性。
//
// 现场缺陷：真实通信铁塔图层有 373 个站址，但地图放大到 detail LOD 也几乎看不见 ——
// 旧实现把 ``towerSymbolScale``（0.7 / 0.78 / 1）当成 canvas 像素乘数，而 TOWER_SYMBOL 的
// y 跨度只有约 1.75 单位 ⇒ detail 档符号总高度也只有约 1.75 px。
//
// 本文件锁定修复后的语义：
//  * 尺寸一律是**屏幕像素高度**，medium ≈ 16 px、detail ≈ 22 px、highlight ≥ detail；
//  * 几何是经典桁架塔（桅杆 / 张开塔腿 / ≥3 层横梁 / X 型斜撑 / 宽底座）；
//  * 命中半径与视觉尺寸一致（约 12～14 px），且不影响 nodes / landingSites；
//  * overview 仍然只画聚合点；candidate/highlight 使用**同一份**几何；
//  * 图例 inline SVG 与 canvas 共用同一 TOWER_SYMBOL；无外部资源、无 emoji、无字体图标。
//
// 与其它前端测试一致：只读源码 + 直接调用模块的纯函数，没有 jsdom。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  ROUTE_STYLES,TOWER_HIGHLIGHT_RING_RADIUS_PX,TOWER_HIGHLIGHT_SIZE_PX,TOWER_HIT_RADIUS_PX,
  TOWER_MARKER_MODES,TOWER_SYMBOL_SIZE_PX,TOWER_SYMBOL_STROKE_PX,displayStyle
} from '../cns_planner/web/js/map/lod.js';
import {
  TOWER_SYMBOL,buildDisplayPlan,drawTowerSymbol,hitDisplayEntry,towerSymbolSvg
} from '../cns_planner/web/js/map/display_layers.js';
import {TOWER_LEGEND_LABEL,mapLegendModel} from '../cns_planner/web/js/workflow/map_legend.js';

const display=readFileSync(new URL('../cns_planner/web/js/map/display_layers.js',import.meta.url),'utf8');
const lod=readFileSync(new URL('../cns_planner/web/js/map/lod.js',import.meta.url),'utf8');
const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
const legend=readFileSync(new URL('../cns_planner/web/js/workflow/map_legend.js',import.meta.url),'utf8');
const towerLayer=readFileSync(new URL('../cns_planner/web/js/map/tower_reference_layer.js',import.meta.url),'utf8');

// ---- 夹具 ---------------------------------------------------------------------

const PIXELS_PER_DEGREE=1000;
const ORIGIN=[122.00,30.00];
const ORIGIN_SCREEN=[400,300];
const SIZE=[800,600];
const toScreen=coordinate=>[
  ORIGIN_SCREEN[0]+(coordinate[0]-ORIGIN[0])*PIXELS_PER_DEGREE,
  ORIGIN_SCREEN[1]+(coordinate[1]-ORIGIN[1])*PIXELS_PER_DEGREE
];
const toLonLat=point=>[
  ORIGIN[0]+(point[0]-ORIGIN_SCREEN[0])/PIXELS_PER_DEGREE,
  ORIGIN[1]+(point[1]-ORIGIN_SCREEN[1])/PIXELS_PER_DEGREE
];

/** 373 个真实铁塔：与真实导入规模一致（密集分布 ⇒ overview/medium 会聚合）。 */
function towers(count=373){
  return Array.from({length:count},(_,index)=>({
    tower_id:'T'+String(index+1).padStart(4,'0'),
    name:'站址'+index,
    coordinate:[ORIGIN[0]+0.00008*(index%20),ORIGIN[1]+0.00008*Math.floor(index/20)],
    site_type:'地面角钢塔'
  }));
}

function flowFixture({items=null,nodes=[],colocation=[]}={}){
  return {
    nodes,reference_landing_sites:{items:[]},scenario_routes:[],
    towers:{status:'passed',count:(items||towers()).length,items:items||towers()},
    tower_colocation_candidates:{status:'passed',count:colocation.length,items:colocation}
  };
}

function plan({res,flow=null,layers={towerLayer:true},towerHighlight=null}={}){
  return buildDisplayPlan({
    flow:flow||flowFixture(),view:{res},size:SIZE,screenPoint:toScreen,screenToLonLat:toLonLat,
    layers,referenceOverlay:{referenceRoutes:[],referencePoints:[]},
    selectedReference:null,referenceFilters:{},filterReferenceSites:null,towerHighlight
  });
}

/** 记录所有 moveTo/lineTo 点与每次 stroke 的样式（不需要真实 canvas）。 */
function recordingCtx(){
  const points=[],strokes=[];
  const ctx={
    save(){},restore(){},beginPath(){},
    moveTo(x,y){points.push([x,y]);},
    lineTo(x,y){points.push([x,y]);},
    arc(){},
    stroke(){strokes.push({width:ctx._width,style:ctx._style});},
    set strokeStyle(value){ctx._style=value;},get strokeStyle(){return ctx._style;},
    set lineWidth(value){ctx._width=value;},get lineWidth(){return ctx._width;},
    set lineCap(value){}
  };
  return {ctx,points,strokes};
}

function bounds(points){
  const xs=points.map(point=>point[0]),ys=points.map(point=>point[1]);
  return {
    width:Math.max(...xs)-Math.min(...xs),
    height:Math.max(...ys)-Math.min(...ys)
  };
}

// ======================================================================================
// 1) 桁架塔几何
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: the symbol is a classic lattice tower silhouette',()=>{
  assert.ok(TOWER_SYMBOL.segments.length>=12,'桁架塔符号必须是多段矢量路径');
  assert.equal(TOWER_SYMBOL.heightUnits,2,'归一化几何的高度必须是 2 个单位');

  // 顶部天线桅杆：唯一一段竖直且位于最高点。
  const mast=TOWER_SYMBOL.segments.find(([start,end])=>start[0]===0&&end[0]===0);
  assert.ok(mast,'必须有竖直的顶部天线桅杆');
  const lowest=Math.min(...TOWER_SYMBOL.segments.flat().map(point=>point[1]));
  assert.equal(mast[0][1],lowest,'桅杆必须从符号最高点开始');
  assert.ok(mast[1][1]>mast[0][1],'天线自上向下绘制');

  // 两条向下张开的外侧塔腿。
  const legs=TOWER_SYMBOL.segments.filter(([start,end])=>
    start[1]!==end[1]&&start[0]*end[0]>0&&Math.abs(end[1]-start[1])>=1.5);
  assert.equal(legs.length,2,'必须恰好有两条贯穿全高的外侧塔腿');
  for(const [start,end] of legs){
    assert.ok(Math.abs(end[0])>Math.abs(start[0])&&end[1]>start[1],'塔腿必须向下张开');
  }

  // ≥3 层水平横梁，且越往下越宽。
  const beams=TOWER_SYMBOL.segments.filter(([start,end])=>start[1]===end[1]&&start[0]*end[0]<0);
  assert.ok(beams.length>=3,`至少要 3 层水平横梁，实际 ${beams.length}`);
  const widths=beams.map(([start,end])=>Math.abs(end[0])*2).sort((a,b)=>a-b);
  assert.ok(widths[widths.length-1]>=0.8,'底座上沿必须明显宽于其它横梁');
  assert.ok(widths[0]<widths[widths.length-1],'横梁宽度必须随高度递增');

  // X 型斜撑：两条穿越同一竖直区间的交叉斜线成对出现（≥2 组）。
  const legKeys=new Set(legs.map(segment=>segment.join('|')));
  const diagonals=TOWER_SYMBOL.segments
    .filter(segment=>!legKeys.has(segment.join('|')))
    .filter(([start,end])=>start[1]!==end[1]&&start[0]*end[0]<0);
  assert.ok(diagonals.length>=4,`至少要 2 组 X 型斜撑，实际 ${diagonals.length} 条斜线`);
  const keys=new Set(TOWER_SYMBOL.segments.map(([a,b])=>`${a}|${b}`));
  for(const [a,b] of TOWER_SYMBOL.segments){
    if(a[0]===0&&b[0]===0)continue;
    const mirroredForward=`${[-a[0],a[1]]}|${[-b[0],b[1]]}`;
    const mirroredReverse=`${[-b[0],b[1]]}|${[-a[0],a[1]]}`;
    assert.ok(keys.has(mirroredForward)||keys.has(mirroredReverse),`缺少镜像线段 ${a}->${b}`);
  }

  // 宽底座。
  const widest=Math.max(...TOWER_SYMBOL.segments.flat().map(point=>Math.abs(point[0])));
  assert.ok(widest>=0.5,`底座半宽必须明显张开，实际 ${widest}`);
});

// ======================================================================================
// 2) 真实像素尺寸模型（旧版 1.75 px 的直接修复）
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: sizePx is a real on-screen pixel height',()=>{
  for(const sizePx of [16,20,22,24]){
    const {ctx,points}=recordingCtx();
    assert.equal(drawTowerSymbol(ctx,100,100,{sizePx}),true);
    const measured=bounds(points);
    assert.equal(Math.round(measured.height),sizePx,`符号可见高度必须等于 sizePx=${sizePx}`);
    // 桁架塔的宽高比约 0.65（x 半宽 0.65 / y 半高 1）。
    assert.ok(measured.width>sizePx*0.5&&measured.width<sizePx*0.8,
      `符号宽度应约 ${Math.round(sizePx*0.65)} px，实际 ${measured.width}`);
  }
  // 以 detail 档为准：必须比旧实现（约 1.75 px）大一个数量级。
  const {ctx,points}=recordingCtx();
  drawTowerSymbol(ctx,100,100,{sizePx:TOWER_SYMBOL_SIZE_PX.detail});
  assert.ok(bounds(points).height>=20,'detail 单塔必须肉眼明显可见（≥20 px）');
  assert.ok(bounds(points).height/1.75>10,'必须比旧 scale 模型大一个数量级');
});

test('MAP-TOWER-SYMBOL-V2: a non-positive size draws nothing',()=>{
  const {ctx,strokes}=recordingCtx();
  assert.equal(drawTowerSymbol(ctx,10,10,{sizePx:0}),false,'overview 档单塔不绘制');
  assert.equal(drawTowerSymbol(ctx,10,10,{sizePx:-4}),false);
  assert.equal(strokes.length,0);
});

test('MAP-TOWER-SYMBOL-V2: halo is wider than the body stroke and both stay visible',()=>{
  const {ctx,strokes}=recordingCtx();
  drawTowerSymbol(ctx,10,10,{sizePx:22,strokePx:2.1});
  assert.equal(strokes.length,2,'必须先画白色 halo 再画本体');
  assert.equal(strokes[0].style,'#ffffff');
  assert.equal(strokes[0].width,2.1+2,'halo 必须比主体再宽约 2 px');
  assert.equal(strokes[1].width,2.1);
  assert.ok(strokes[1].width>=1.5,'detail 主体 stroke 不得细到消失');
});

// ======================================================================================
// 3) LOD 尺寸基线
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: LOD declares pixel sizes rather than invisible multipliers',()=>{
  assert.equal(TOWER_SYMBOL_SIZE_PX.overview,0,'overview 只画聚合点，单塔高度为 0');
  assert.ok(TOWER_SYMBOL_SIZE_PX.medium>=14&&TOWER_SYMBOL_SIZE_PX.medium<=16,
    'medium 孤立单塔应约 14～16 px');
  assert.ok(TOWER_SYMBOL_SIZE_PX.detail>=20&&TOWER_SYMBOL_SIZE_PX.detail<=24,
    'detail 单塔应约 20～24 px');
  assert.ok(TOWER_HIGHLIGHT_SIZE_PX>=TOWER_SYMBOL_SIZE_PX.detail,'highlight 至少是 detail 尺寸');
  assert.ok(TOWER_HIGHLIGHT_RING_RADIUS_PX>=14&&TOWER_HIGHLIGHT_RING_RADIUS_PX<=16,
    '高亮环半径应约 14～16 px');
  assert.ok(TOWER_SYMBOL_STROKE_PX.medium>=1.5&&TOWER_SYMBOL_STROKE_PX.detail>=1.8,
    '主体 stroke 不得细到消失');

  // 旧的归一化倍数语义必须彻底消失（文档注释里可以解释历史，但代码里不得再赋值/消费）。
  assert.doesNotMatch(lod,/towerSymbolScale\s*[:=]/,'不得再出现把归一化倍数当像素用的旧字段');
  assert.doesNotMatch(display,/towerSymbolScale/);

  for(const level of ['overview','medium','detail']){
    const styles=ROUTE_STYLES[level];
    assert.equal(styles.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX[level],`${level} 档尺寸`);
    assert.ok(styles.towerHitRadiusPx>=12&&styles.towerHitRadiusPx<=14,`${level} 命中半径`);
  }
  assert.equal(displayStyle({res:100}).styles.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX.detail);
  assert.equal(displayStyle({res:1000}).styles.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX.medium);
  assert.equal(displayStyle({res:5000}).styles.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX.overview);
});

test('MAP-TOWER-SYMBOL-V2: the display plan carries the pixel sizes of the current LOD',()=>{
  const detail=plan({res:100});
  assert.equal(detail.level,'detail');
  assert.equal(detail.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX.detail);
  assert.equal(detail.towerMarkerMode,TOWER_MARKER_MODES.detail);

  const medium=plan({res:1000});
  assert.equal(medium.towerSymbolSizePx,TOWER_SYMBOL_SIZE_PX.medium);
  assert.equal(medium.towerMarkerMode,TOWER_MARKER_MODES.medium);

  const overview=plan({res:5000});
  assert.equal(overview.towerSymbolSizePx,0);
  assert.equal(overview.towerMarkerMode,TOWER_MARKER_MODES.overview);
});

// ======================================================================================
// 4) 可见性与聚合语义不变
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: overview still clusters, detail still draws every tower',()=>{
  const detail=plan({res:100});
  assert.equal(detail.towers.length,373,'detail 档逐塔显示');
  assert.ok(detail.towers.every(entry=>entry.single));

  const medium=plan({res:1000});
  assert.ok(medium.towers.every(entry=>entry.count>1||entry.single),'medium 只放行孤立塔');

  const overview=plan({res:5000});
  assert.ok(overview.towers.length>0,'overview 仍有聚合点');
  assert.ok(overview.towers.every(entry=>entry.count>1),'overview 只显示聚合点');
  assert.ok(overview.towers.length<373);
});

test('MAP-TOWER-SYMBOL-V2: a highlighted host tower survives the overview filter',()=>{
  // 一个远离塔群的宿主塔：它在 overview 档被 highlight 时必须单独显示。
  const host={tower_id:'T-HOST',name:'孤立宿主',coordinate:[ORIGIN[0]+0.3,ORIGIN[1]+0.2]};
  const overview=plan({
    res:5000,flow:flowFixture({items:[...towers(372),host]}),towerHighlight:'T-HOST'
  });
  const shown=overview.towers.filter(entry=>entry.count===1);
  assert.equal(shown.length,1,'overview 下只额外保留被高亮的宿主塔');
  assert.equal(shown[0].anchor.tower.tower_id,'T-HOST');
  assert.equal(overview.towerHighlightSizePx,TOWER_HIGHLIGHT_SIZE_PX);
});

// ======================================================================================
// 5) 命中半径与视觉尺寸一致
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: the tower hit radius matches the drawn size',()=>{
  const detail=plan({res:100,flow:flowFixture({items:[
    {tower_id:'T1',coordinate:[ORIGIN[0],ORIGIN[1]]}
  ]})});
  assert.ok(detail.towerHitRadiusPx>=12&&detail.towerHitRadiusPx<=14);
  const entry=detail.towers[0];
  const [x,y]=entry.screen;
  // 视觉半高 = sizePx/2 ≈ 11 px：12 px 内必须命中，20 px 外必须落空。
  assert.equal(hitDisplayEntry(detail,[x+12,y],{kind:'towers'}),entry);
  assert.equal(hitDisplayEntry(detail,[x,y+12],{kind:'towers'}),entry);
  assert.equal(hitDisplayEntry(detail,[x+20,y],{kind:'towers'}),null);
  assert.match(display,/towerHitRadiusPx\?\?13/,'铁塔默认命中半径必须来自 LOD 声明');
});

test('MAP-TOWER-SYMBOL-V2: node and landing-site hit radii are untouched',()=>{
  const flow=flowFixture({items:[],nodes:[{node_id:'N1',coordinate:[ORIGIN[0],ORIGIN[1]]}]});
  const detail=plan({res:100,flow});
  const [x,y]=toScreen([ORIGIN[0],ORIGIN[1]]);
  // 12 px 对 nodes 仍然超出既有 9 px 半径 ⇒ 不得因为铁塔改动而放大。
  assert.equal(hitDisplayEntry(detail,[x+12,y],{kind:'nodes'}),null);
  assert.equal(hitDisplayEntry(detail,[x+8,y],{kind:'nodes'}).anchor.node.node_id,'N1');
  assert.match(display,/kind==='towers'\?\(plan\?\.towerHitRadiusPx\?\?13\):9/,
    '命中半径必须是 towers 专属，nodes/sites 保持 9 px');
});

// ======================================================================================
// 6) candidate / highlight 使用同一形状
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: colocation candidates and highlights share the new geometry',()=>{
  const colocation=[{
    site_id:'tower-colocation:T0001',coordinate:[ORIGIN[0],ORIGIN[1]],
    planning_profile:{reuse_class:'tower_colocation_host',confirmed:false,status:'pending_confirmation'},
    metadata:{host:{host_type:'tower',host_tower_id:'T0001',host_tower_name:'站址0'}}
  }];
  const built=plan({res:100,flow:flowFixture({colocation}),layers:{towerLayer:true,candidateSiteLayer:true}});
  assert.equal(built.cnsTowerCandidates.length,1);
  assert.ok(built.towerCandidateSizePx>=20,'共塔宿主候选不得再用微型图标');
  assert.ok(built.towerHighlightRingRadiusPx>=14);

  // 两条绘制路径都调用同一 drawTowerSymbol，几何因此保证一致。
  const source=display.slice(display.indexOf('for(const entry of plan.towers'),
    display.indexOf('// 8) 项目起降点'));
  assert.equal((source.match(/drawTowerSymbol\(ctx,x,y,\{/g)||[]).length>=2,true,
    '真实铁塔与共塔候选必须共用同一 drawTowerSymbol 调用');
  assert.match(source,/sizePx:plan\.towerCandidateSizePx/);
  assert.match(source,/plan\.towerHighlightRingRadiusPx/);
  assert.doesNotMatch(source,/drawMarker\(ctx,'(square|diamond)'/,'铁塔图层不得退回方形/菱形标记');
});

test('MAP-TOWER-SYMBOL-V2: the highlighted host tower gets a ring, not just a colour change',()=>{
  assert.match(display,/ctx\.arc\(x,y,plan\.towerHighlightRingRadiusPx,0,Math\.PI\*2\)/);
  assert.ok(TOWER_HIGHLIGHT_RING_RADIUS_PX>TOWER_SYMBOL_SIZE_PX.detail/2,
    '高亮环必须明显大于符号本体');
});

// ======================================================================================
// 7) 图例与 canvas 共用同一份符号
// ======================================================================================

test('MAP-TOWER-SYMBOL-V2: the legend SVG is generated from the same TOWER_SYMBOL geometry',()=>{
  const path=TOWER_SYMBOL.segments
    .map(([start,end])=>'M'+start[0]+' '+start[1]+'L'+end[0]+' '+end[1])
    .join('');
  const svg=towerSymbolSvg({size:20,color:'#1f7a8c',strokePx:1.8});
  assert.ok(svg.includes('d="'+path+'"'),'图例必须使用与 canvas 相同的线段表');
  assert.match(svg,/width="20" height="20"/,'图例铁塔的可视高度约 20 px');
  assert.match(svg,/viewBox="-1 -1 2 2"/);
  assert.match(svg,/stroke-width="1.8"/,'图例线条必须清晰');
  assert.doesNotMatch(svg,/https?:|\.png|\.svg"|emoji|&#x/,'不得引用外部资源或字体图标');

  const line=mapLegendModel({flow:flowFixture(),towerLayerOn:false})
    .groups.flatMap(group=>group.lines).find(item=>item.label===TOWER_LEGEND_LABEL);
  assert.ok(line,'图例必须包含通信铁塔一行');
  assert.ok(line.symbol.includes('d="'+path+'"'),'图例行必须与 canvas 同源');
  assert.match(line.symbol,/width="20"/);
  assert.match(legend,/towerSymbolSvg\(\{size:20/);
  // canvas 与 SVG 都读同一份 TOWER_SYMBOL.segments（至少两处消费）。
  assert.equal((display.match(/TOWER_SYMBOL\.segments/g)||[]).length>=2,true);
});

test('MAP-TOWER-SYMBOL-V2: no second symbol source or external asset is introduced',()=>{
  const imports=[...display.matchAll(/^import .*$/gm)].map(match=>match[0]);
  assert.equal(imports.some(text=>/icon|emoji|fontawesome|sprite/i.test(text)),false,
    '不得引入第三方 icon 包或字体图标');
  assert.doesNotMatch(display,/\.svg'|\.png'|https?:\/\//,'符号不得来自外部文件或网络');
  assert.equal(main.includes('towerSymbolScale'),false);
  assert.equal(legend.includes('towerSymbolScale'),false);
  assert.equal(towerLayer.includes('towerSymbolScale'),false);
});

test('MAP-TOWER-SYMBOL-V2: the tower layer stays opt-in and read-only',()=>{
  assert.match(display,/layers\.towerLayer===true\?cluster\(towerItems\):\[\]/,
    'towerLayer 仍必须默认关闭、显式勾选后才进入计划');
  assert.doesNotMatch(display.slice(display.indexOf('for(const entry of plan.towers'),
    display.indexOf('// 8) 项目起降点')),/placer\.place/,'铁塔图层仍不得铺开名称标签');
});
