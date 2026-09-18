/**
 * 地图显示分级（LOD）与屏幕空间点聚合的单元测试。
 *
 * 这两块决定"缩小看概况、放大看细节"，属于纯展示逻辑：
 *  - 不修改 flow、不修改数据；
 *  - 阈值集中在 map/lod.js；
 *  - 聚合只在屏幕空间发生，单点永远沿用原始坐标。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {LOD_THRESHOLDS,CLUSTER_PIXEL_THRESHOLD,LOD_LABELS,ROUTE_STYLES,
  lodLevel,displayStyle,resolutionLabel,visibleAt} from '../cns_planner/web/js/map/lod.js';
import {clusterPoints,toGeographic,hitCluster,extentOf,clusterLabel} from '../cns_planner/web/js/map/point_clustering.js';

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
