/**
 * 地图显示分级（LOD）与屏幕空间点聚合的单元测试。
 *
 * 这两块决定"缩小看概况、放大看细节"，属于纯展示逻辑：
 *  - 不修改 flow、不修改数据；
 *  - 阈值集中在 map/lod.js；
 *  - 聚合只在屏幕空间发生，单点永远沿用原始坐标。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {LOD_THRESHOLDS,CLUSTER_PIXEL_THRESHOLD,LOD_LABELS,ROUTE_STYLES,
  lodLevel,displayStyle,resolutionLabel,visibleAt} from '../cns_planner/web/js/map/lod.js';
import {clusterPoints,toGeographic,hitCluster,extentOf,clusterLabel} from '../cns_planner/web/js/map/point_clustering.js';
import {buildDisplayPlan} from '../cns_planner/web/js/map/display_layers.js';

// ---- LOD 分档 ---------------------------------------------------------------

test('lod level uses the documented three bands',()=>{
  assert.equal(lodLevel(LOD_THRESHOLDS.medium+1),'overview');
  assert.equal(lodLevel(LOD_THRESHOLDS.medium),'medium');
  assert.equal(lodLevel(LOD_THRESHOLDS.detail+1),'medium');
  assert.equal(lodLevel(LOD_THRESHOLDS.detail),'detail');
  assert.equal(lodLevel(1),'detail');
});

test('lod level degrades when the screen is crowded',()=>{
  assert.equal(lodLevel(LOD_THRESHOLDS.detail+1,5000),'overview');
  assert.equal(lodLevel(LOD_THRESHOLDS.detail,5000),'medium');
  // 稀疏时保持原档位
  assert.equal(lodLevel(LOD_THRESHOLDS.detail+1,10),'medium');
});

test('missing or invalid resolution is never treated as detail',()=>{
  for(const value of [undefined,null,0,-1,Number.NaN,Number.POSITIVE_INFINITY]){
    assert.equal(lodLevel(value),'overview',`res=${String(value)}`);
  }
});

test('overview hides names and aggregate markers while detail expands them',()=>{
  assert.equal(ROUTE_STYLES.overview.nameMode,'hidden');
  assert.equal(ROUTE_STYLES.overview.markerMode,'cluster');
  assert.equal(ROUTE_STYLES.overview.showAllNames,false);
  assert.equal(ROUTE_STYLES.overview.coverageRing,false);
  assert.equal(ROUTE_STYLES.detail.nameMode,'avoid');
  assert.equal(ROUTE_STYLES.detail.coverageRing,true);
  // 线宽随尺度收敛：越远越轻，绝不反过来
  assert.ok(ROUTE_STYLES.overview.referenceWidth<ROUTE_STYLES.detail.referenceWidth);
  assert.ok(ROUTE_STYLES.overview.operationalWidth<ROUTE_STYLES.detail.operationalWidth);
  assert.ok(ROUTE_STYLES.overview.routeAlpha<ROUTE_STYLES.detail.routeAlpha);
});

test('reference route points are hidden at overview scale',()=>{
  assert.equal(visibleAt('overview','referencePoints'),false);
  assert.equal(visibleAt('medium','referencePoints'),'near');
  assert.equal(visibleAt('detail','referencePoints'),true);
  assert.equal(visibleAt('overview','referenceRoutes'),true);
});

test('displayStyle returns one consistent bundle',()=>{
  const style=displayStyle({res:LOD_THRESHOLDS.detail+1});
  assert.equal(style.level,'medium');
  assert.equal(style.label,LOD_LABELS.medium);
  assert.equal(style.clusterPixels,CLUSTER_PIXEL_THRESHOLD.medium);
  assert.equal(style.styles,ROUTE_STYLES.medium);
  assert.match(style.resolution,/m\/px/);
  assert.match(resolutionLabel(3000),/km\/px/);
  assert.equal(resolutionLabel(0),'—');
});

// ---- 屏幕空间聚合 -----------------------------------------------------------

const project=coordinate=>coordinate;              // 屏幕坐标即"坐标"
const fromScreen=point=>[point[0]/1000,point[1]/1000];

test('clustering merges only points inside the pixel threshold',()=>{
  const items=[
    {id:'a',coordinate:[0,0]},
    {id:'b',coordinate:[5,0]},
    {id:'c',coordinate:[500,500]}
  ];
  const result=clusterPoints(items,project,20);
  assert.equal(result.entries.length,2);
  const merged=result.entries.find(entry=>entry.count===2);
  assert.deepEqual(merged.members.map(item=>item.id),['a','b']);
  const single=result.entries.find(entry=>entry.count===1);
  assert.equal(single.anchor.id,'c');
  assert.equal(single.single,true);
});

test('a zero threshold disables clustering and keeps original coordinates',()=>{
  const items=[{id:'a',coordinate:[0,0]},{id:'b',coordinate:[0.0001,0.0001]}];
  for(const threshold of [0,-5,undefined,Number.NaN]){
    const result=clusterPoints(items,project,threshold);
    assert.equal(result.entries.length,2);
    for(const entry of result.entries)assert.equal(entry.single,true);
    assert.deepEqual(result.entries[0].coordinate,items[0].coordinate);
  }
});

test('geographic conversion never moves a single point',()=>{
  const items=[{id:'a',coordinate:[12,34]},{id:'b',coordinate:[100,100]},{id:'c',coordinate:[102,100]}];
  const entries=toGeographic(clusterPoints(items,project,20).entries,fromScreen);
  const single=entries.find(entry=>entry.anchor.id==='a');
  assert.deepEqual(single.coordinate,[12,34],'single points keep their exact coordinate');
  const merged=entries.find(entry=>entry.count===2);
  assert.ok(Array.isArray(merged.coordinate),'a cluster gets a geographic display centre');
  assert.notEqual(merged.coordinate,items[1].coordinate);
});

test('cluster hit testing covers every member and singles stay small',()=>{
  const items=[
    {id:'a',coordinate:[0,0]},
    {id:'b',coordinate:[30,0]},
    {id:'c',coordinate:[600,600]}
  ];
  const entries=clusterPoints(items,project,40).entries;
  const merged=entries.find(entry=>entry.count===2);
  // 成员 b 的屏幕位置必须命中该聚合点
  const hitB=hitCluster(entries,[30,0]);
  assert.equal(hitB,merged);
  const box=extentOf(merged);
  assert.ok(box&&box[0]<=0&&box[2]>=30,'extent covers all members for zoom-to-fit');
  assert.equal(extentOf(entries.find(entry=>entry.single)),null);
  assert.equal(hitCluster(entries,[6000,6000]),null);
  assert.equal(clusterLabel(merged),'2处');
  assert.equal(clusterLabel(merged,{countSuffix:'个'}),'2个');
  assert.equal(clusterLabel(entries.find(entry=>entry.single),{singleLabel:'N001'}),'N001');
});

test('clustering ignores unrenderable coordinates',()=>{
  const items=[{id:'a',coordinate:[0,0]},{id:'b',coordinate:null},{id:'c'},{id:'d',coordinate:[Number.NaN,1]}];
  const result=clusterPoints(items,project,10);
  assert.equal(result.entries.length,1);
  assert.equal(result.entries[0].anchor.id,'a');
});

// ---- 显示计划（buildDisplayPlan）--------------------------------------------
//
// 投影把工作点平移到屏幕中心，并保持 1000 像素/度的比例，屏幕距离可直接读出；
// screenToLonLat 是"像素 → 经纬度"的反换算，用来区分"经纬度"与"EPSG:3857"语义。

const PIXELS_PER_DEGREE=1000;
const ORIGIN=[122.00,30.00];
const ORIGIN_SCREEN=[400,300];
const toScreen=coordinate=>[
  ORIGIN_SCREEN[0]+(coordinate[0]-ORIGIN[0])*PIXELS_PER_DEGREE,
  ORIGIN_SCREEN[1]+(coordinate[1]-ORIGIN[1])*PIXELS_PER_DEGREE
];
const toLonLat=point=>[
  ORIGIN[0]+(point[0]-ORIGIN_SCREEN[0])/PIXELS_PER_DEGREE,
  ORIGIN[1]+(point[1]-ORIGIN_SCREEN[1])/PIXELS_PER_DEGREE
];
const SIZE=[800,600];

function planFlow(){
  return {
    nodes:[
      {node_id:'N001',coordinate:[122.00,30.00]},
      {node_id:'N002',coordinate:[122.01,30.00]}
    ],
    reference_landing_sites:{items:[
      {reference_site_id:'S1',coordinate:[122.002,30.00]},
      {reference_site_id:'S2',coordinate:[122.004,30.00]}
    ]},
    scenario_routes:[{route_id:'R0001',path:[[122.00,30.00],[122.01,30.00]]}]
  };
}

function plan({res,layers={},referencePoints=[]}){
  return buildDisplayPlan({
    flow:planFlow(),
    view:{res},
    size:SIZE,
    screenPoint:toScreen,
    screenToLonLat:toLonLat,
    layers,
    referenceOverlay:{referenceRoutes:[],referencePoints},
    selectedReference:null,
    referenceFilters:{},
    filterReferenceSites:null
  });
}

test('display plan follows the LOD rule for reference route points',()=>{
  const points=[{reference_route_point_id:'P1',coordinate:[122.001,30.00],position:'endpoint'}];
  // overview：不显示
  assert.equal(plan({res:LOD_THRESHOLDS.medium+10,layers:{referenceRoutePointLayer:true},referencePoints:points}).referencePointsVisible,false);
  // medium：默认不显示
  assert.equal(plan({res:LOD_THRESHOLDS.detail+10,layers:{referenceRoutePointLayer:true},referencePoints:points}).referencePointsVisible,false);
  // medium + 显式要求：显示
  assert.equal(plan({res:LOD_THRESHOLDS.detail+10,layers:{referenceRoutePointLayer:true,referencePointsDetail:true},referencePoints:points}).referencePointsVisible,true);
  // detail：显示
  assert.equal(plan({res:LOD_THRESHOLDS.detail,layers:{referenceRoutePointLayer:true},referencePoints:points}).referencePointsVisible,true);
  // detail 但图层关闭：不显示
  assert.equal(plan({res:LOD_THRESHOLDS.detail,layers:{referenceRoutePointLayer:false},referencePoints:points}).referencePointsVisible,false);
});

test('reference route points reach the plan whenever the layer is on',()=>{
  const points=[{reference_route_point_id:'P1',coordinate:[122.001,30.00],position:'endpoint'}];
  for(const res of [LOD_THRESHOLDS.medium+10,LOD_THRESHOLDS.detail+10,LOD_THRESHOLDS.detail]){
    const result=plan({res,layers:{referenceRoutePointLayer:true},referencePoints:points});
    assert.equal(result.referencePoints.length,1,`res=${res} must carry the points into the plan`);
  }
  const off=plan({res:LOD_THRESHOLDS.detail,layers:{referenceRoutePointLayer:false},referencePoints:[]});
  assert.equal(off.referencePoints.length,0);
});

test('reference landing sites follow the referenceLandingLayer switch',()=>{
  // 关闭：不进入计划（既不绘制也不可命中）
  const closed=plan({res:LOD_THRESHOLDS.medium+10,layers:{referenceLandingLayer:false}});
  assert.equal(closed.landingSites.length,0,'landing sites must be empty when the layer is off');
  assert.equal(closed.clusterCounts.sites,0);
  // 打开：按当前 LOD 聚合（overview 下 2 个邻近点合并为 1 个聚合条目）
  const open=plan({res:LOD_THRESHOLDS.medium+10,layers:{referenceLandingLayer:true}});
  assert.equal(open.landingSites.length,1);
  assert.equal(open.landingSites[0].count,2);
  assert.equal(open.clusterCounts.sites,2);
  // detail 档展开为单点
  const detail=plan({res:LOD_THRESHOLDS.detail,layers:{referenceLandingLayer:true}});
  assert.equal(detail.landingSites.length,2);
  assert.ok(detail.landingSites.every(entry=>entry.single));
  // 未显式提供该 key 时保持向后兼容（默认视为开启）
  const implicit=plan({res:LOD_THRESHOLDS.detail,layers:{}});
  assert.equal(implicit.landingSites.length,2);
});

test('cluster display centres are lon/lat while single points stay untouched',()=>{
  const result=plan({res:LOD_THRESHOLDS.medium+10,layers:{referenceRoutePointLayer:true}});
  assert.equal(result.level,'overview');
  const merged=result.nodes.find(entry=>entry.count>1);
  assert.ok(merged,'nodes 10 screen pixels apart must merge at overview scale');
  const [lon,lat]=merged.coordinate;
  assert.ok(Math.abs(lon-122.005)<.001,'cluster centre is a longitude, not an EPSG:3857 metre value');
  assert.ok(Math.abs(lat-30)<.001,'cluster centre is a latitude, not an EPSG:3857 metre value');
  const detailPlan=plan({res:LOD_THRESHOLDS.detail,layers:{referenceRoutePointLayer:true}});
  const single=detailPlan.nodes.find(entry=>entry.single);
  assert.ok(single,'detail scale must expand the single nodes');
  assert.deepEqual(single.coordinate,single.anchor.coordinate,'single points keep their exact coordinate');
});

test('overview aggregation never reuses the EPSG:3857 screen inverse',()=>{
  // 屏幕坐标量级（约 1e2）与墨卡托量级（约 1e7）相差 5 个数量级，
  // 如果误用 3857 反投影，聚合中心会落到经纬度范围之外。
  const result=plan({res:LOD_THRESHOLDS.medium+10,layers:{referenceRoutePointLayer:true}});
  for(const entry of result.nodes){
    if(entry.single)continue;
    const [lon,lat]=entry.coordinate;
    assert.ok(lon>-180&&lon<180,`longitude out of range: ${lon}`);
    assert.ok(lat>-90&&lat<90,`latitude out of range: ${lat}`);
  }
});

// ---- main.js 交互契约（源码级）----------------------------------------------

const mainSource=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
const slice=(from,to)=>mainSource.slice(mainSource.indexOf(from),mainSource.indexOf(to));

test('main.js feeds the reference point layer into the display plan',()=>{
  const plan_=slice('function displayPlan(){','function drawWorkflowOverlay(){');
  assert.match(plan_,/points:switches\.referenceRoutePointLayer/);
  assert.doesNotMatch(plan_,/points:false/);
  // 计划使用 screenToLonLat（经纬度），而不是 fromScreen（EPSG:3857）
  assert.match(plan_,/screenToLonLat/);
  assert.doesNotMatch(plan_,/\bfromScreen,/);
});

test('hidden reference points are not hit-testable',()=>{
  const hit=slice('function hitReferenceObject(event){','bindShell({');
  assert.match(hit,/currentPlan\.level!=='detail'/);
  assert.match(hit,/currentPlan\.referencePointsVisible\?\(currentPlan\.referencePoints\|\|\[\]\):\[\]/);
  // 命中测试不得退回"按图层 checkbox 直接取点"
  assert.doesNotMatch(hit,/points:\$\('referenceRoutePointLayer'\)\.checked/);
});

test('a visible cluster wins over reference hit-testing below it',()=>{
  const click=slice("canvas.addEventListener('click'",'function hitClusterAt(event){');
  const clusterIndex=click.indexOf('hitClusterAt(event)');
  const referenceIndex=click.indexOf('hitReferenceObject(event)');
  assert.ok(clusterIndex>=0&&referenceIndex>=0,'both hit tests run on click');
  assert.ok(clusterIndex<referenceIndex,'the cluster zoom must be evaluated first');
  assert.match(click,/clusterTarget\.count>1/);
});

