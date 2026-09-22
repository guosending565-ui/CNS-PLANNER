// 地图默认图层：应用首次打开时**只**勾选“QGIS 在线底图”。
//
// 锁定三件事：
//   1. index.html 的静态默认值：online 默认 checked，其余 17 个图层开关默认关闭；
//   2. main.js 的 gridDisplay.outline 初始值为 false（网格边界默认不绘制）；
//   3. open_project / 状态刷新不会重置用户的图层选择——用户进入系统后的手工选择必须保持。
//
// 这里只读源码与静态 DOM 默认值（与本目录其它前端测试一致：没有 jsdom，也不伪造浏览器）。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
const shell=readFileSync(new URL('../cns_planner/web/js/shell.js',import.meta.url),'utf8');

//: 图层抽屉里全部图层 checkbox 的 id（main.js LAYER_IDS + 栅格/网格/在线底图开关）。
const LAYER_IDS=['air','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','towerLayer',
  'gridLayer','pop','terrain','buildingFootprintLayer','online','buildingClearanceLayer',
  'v3CandidateLayer','layeredFeasibilityLayer','layeredCandidateLayer','existingCnsLayer',
  'candidateSiteLayer','cLayer','nLayer','sLayer'];

/** 图层抽屉里 id → <input> 标签原文。 */
function checkboxAttributes(source){
  const found=new Map();
  for(const match of source.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)){
    const id=/\bid="([^"]+)"/.exec(match[0]);
    if(id)found.set(id[1],match[0]);
  }
  return found;
}

function isChecked(attributes){return /\bchecked\b/.test(attributes);}

test('the layer drawer ships exactly one default layer: the QGIS online basemap',()=>{
  const boxes=checkboxAttributes(html);
  const defaults=[...boxes].filter(([,attributes])=>isChecked(attributes)).map(([id])=>id);
  assert.deepEqual(defaults,['online'],
    `首次打开只允许勾选 online，实际勾选：${defaults.join(', ')||'无'}`);
  for(const id of LAYER_IDS)assert.ok(boxes.has(id),`图层抽屉缺少图层开关 ${id}`);
});

test('every other map layer starts unchecked',()=>{
  const boxes=checkboxAttributes(html);
  for(const id of LAYER_IDS){
    if(id==='online')continue;
    assert.equal(isChecked(boxes.get(id)),false,`${id} 必须默认关闭`);
  }
});

test('the standard grid and its outline start disabled',()=>{
  assert.match(main,/let gridDisplay=\{outline:false,theme:'none'\}/,
    'gridDisplay.outline 必须默认 false');
  assert.equal(isChecked(checkboxAttributes(html).get('gridLayer')),false,
    '标准网格图层必须默认关闭');
});

test('opening or refreshing a project never resets the user layer selection',()=>{
  const openBody=main.slice(main.indexOf('async function openProject('),
    main.indexOf('function getTiandituKey('));
  assert.ok(openBody.length>0,'main.js 必须保留 openProject');
  assert.doesNotMatch(openBody,/\.checked\s*=/,'open_project 不得重置图层勾选');

  const updateBody=main.slice(main.indexOf('function update(data){'),
    main.indexOf('// ---- 启动装配'));
  assert.ok(updateBody.length>0,'main.js 必须保留 update');
  assert.doesNotMatch(updateBody,/\$\('(?:air|pop|terrain|online|gridLayer|[A-Za-z]+Layer)'\)\.checked\s*=/,
    '项目状态刷新不得重置图层勾选');

  assert.doesNotMatch(shell,/\.checked\s*=\s*true/,'壳层不得在启动时强制勾选图层');
});
