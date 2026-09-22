// 真实通信铁塔站址图层：默认关闭、复用既有聚合、不铺开名称、不新建地图状态机。
//
// 与本目录其它前端测试一致：只读源码与静态 DOM 默认值，没有 jsdom，不伪造浏览器。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
const display=readFileSync(new URL('../cns_planner/web/js/map/display_layers.js',import.meta.url),'utf8');
const sources=readFileSync(new URL('../cns_planner/web/js/sources/source_center.js',import.meta.url),'utf8');
const towerLayer=readFileSync(new URL('../cns_planner/web/js/map/tower_reference_layer.js',import.meta.url),'utf8');

function checkboxAttributes(source){
  const found=new Map();
  for(const match of source.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)){
    const id=/\bid="([^"]+)"/.exec(match[0]);
    if(id)found.set(id[1],match[0]);
  }
  return found;
}

test('the layer drawer ships a tower layer switch that is off by default',()=>{
  const boxes=checkboxAttributes(html);
  assert.ok(boxes.has('towerLayer'),'图层抽屉必须提供 towerLayer 开关');
  assert.doesNotMatch(boxes.get('towerLayer'),/\bchecked\b/,'towerLayer 必须默认关闭');
  assert.ok(html.includes('id="towerLayerStatus"'),'必须提供铁塔站址状态提示位');
  const defaults=[...boxes].filter(([,attributes])=>/\bchecked\b/.test(attributes)).map(([id])=>id);
  assert.deepEqual(defaults,['online'],'新增铁塔图层不得改变"默认只勾选在线底图"的策略');
});

test('the source center exposes a towers path input and browser filter',()=>{
  assert.ok(html.includes('id="towersPath"'),'数据源设置必须提供 towersPath 输入框');
  assert.ok(html.includes('data-browse="towers"'),'towersPath 必须有浏览按钮');
  assert.match(sources,/towers:\$\('towersPath'\)\.value/,'payload 必须提交 towers 路径');
  assert.match(sources,/towers:'文件类型：通信铁塔站址/,'文件浏览必须给出 towers 的文件类型提示');
  assert.match(sources,/data-import-towers/,'必须提供显式的铁塔导入按钮');
  assert.match(sources,/\/api\/towers\/import/,'导入按钮必须调用 /api/towers/import');
});

test('the tower layer is only planned when explicitly switched on',()=>{
  assert.match(display,/layers\.towerLayer===true\?cluster\(towerItems\):\[\]/,
    'towerLayer 必须只在显式勾选（===true）时才进入绘制计划');
  assert.ok(display.includes("kind==='towers'?plan.towers"),'命中测试必须支持 towers');
  assert.equal(main.includes("'towerLayer'"),true,'main.js LAYER_IDS 必须包含 towerLayer');
  assert.equal(main.includes("kind:'towers'"),true,'main.js 必须把 towers 纳入命中链');
});

test('towers reuse the existing clustering and never fan out name labels',()=>{
  assert.ok(display.includes("from './point_clustering.js'"),
    'towers 必须复用既有 point_clustering 模块');
  assert.match(display,/const towerItems=[\s\S]*?const towers=layers\.towerLayer===true\?cluster\(towerItems\):\[\];/,
    'towers 必须复用既有 cluster（point_clustering），不新建聚合实现');
  assert.match(display,/for\(const entry of plan\.towers\|\|\[\]\)\{[\s\S]{0,300}?drawAggregate\(ctx,x,y,entry\.count,CLUSTER_COLORS\.towers\)/,
    '聚合点必须走既有 drawAggregate');
  const towerLoop=display.slice(display.indexOf('for(const entry of plan.towers'),
    display.indexOf('// 8) 项目起降点'));
  assert.ok(towerLoop.length>0,'必须存在 towers 绘制循环');
  assert.doesNotMatch(towerLoop,/placer\.place/,'铁塔图层绝不铺开名称标签');
});

test('the tower hover/click surface stays reference-only',()=>{
  assert.ok(main.includes('attachTowerReferenceLayer('),'main.js 必须挂载铁塔交互层');
  assert.ok(main.includes('towerReference.detail('),'main.js click 必须走模块的 detail()');
  assert.match(towerLayer,/canvas\.addEventListener\('mousemove'/,'必须提供 hover 简短提示');
  assert.match(towerLayer,/聚合 '\+hit\.count\+' 个通信铁塔站址/,'聚合 hover 必须只显示数量');
  for(const field of ['tower_id','name','longitude','latitude','elevation_m','height_m','district','site_type','source'])
    assert.ok(towerLayer.includes(field),`click 详情必须展示源字段 ${field}`);
  assert.match(towerLayer,/不代表 CNS 设备、覆盖能力或可用性/,'click 详情必须声明不派生通信能力');
  assert.doesNotMatch(towerLayer,/coverage_radius|transmit_power|frequency|antenna_height/,
    'click 详情不得展示被禁止的通信性能字段');
  // 只读交互层：不得调用 API、不得写业务状态。
  assert.doesNotMatch(towerLayer,/fetch\(|resourceAction\(|mutate\(/,'铁塔交互层不得触发任何请求或状态写入');
});

test('no second map state machine is introduced for towers',()=>{
  const imports=[...display.matchAll(/^import .*$/gm)].map(match=>match[0]);
  assert.equal(imports.some(line=>/tower/i.test(line)),false,
    'display_layers 不得引入新的地图绘制模块');
  assert.equal(main.includes("step_towers"),false,'towers 不得新增工作流步骤模块');
  assert.equal(main.includes("towerState"),false,'towers 不得新增独立地图状态对象');
  // 交互层是叶子模块：只依赖 display_layers 的命中函数，不反向被绘制依赖。
  assert.doesNotMatch(towerLayer,/from '\.\/(lod|point_clustering)\.js'/,
    '交互层不得自建 LOD / 聚合实现');
});
