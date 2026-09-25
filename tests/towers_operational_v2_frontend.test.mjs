// Towers Operational Integration V2 前端测试（MAP-TOWER-ICON + FRONTEND）。
//
// 与本目录其它前端测试一致：静态源码断言 + 直接调用模块的纯函数，没有 jsdom，
// 也不伪造浏览器。锁定的是"铁塔是正式地图对象、图例同一套、LOD 复用既有实现"。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {ROUTE_STYLES,TOWER_MARKER_MODES,displayStyle,towerSingleVisible}
  from '../cns_planner/web/js/map/lod.js';
import {buildDisplayPlan,TOWER_SYMBOL,drawTowerSymbol,towerSymbolSvg,markerSymbolSvg,
  MARKER_SHAPES,hitCnsTowerCandidate} from '../cns_planner/web/js/map/display_layers.js';
import {mapLegendModel,renderMapLegend,BUSINESS_LEGEND_LABEL,TOWER_LEGEND_LABEL}
  from '../cns_planner/web/js/workflow/map_legend.js';
import {towerDetailHtml,towerTopHtml,towerColocationDetailHtml}
  from '../cns_planner/web/js/map/tower_reference_layer.js';

const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
const display=readFileSync(new URL('../cns_planner/web/js/map/display_layers.js',import.meta.url),'utf8');
const lod=readFileSync(new URL('../cns_planner/web/js/map/lod.js',import.meta.url),'utf8');
const css=readFileSync(new URL('../cns_planner/web/css/map.css',import.meta.url),'utf8');
const step05=readFileSync(new URL('../cns_planner/web/js/workflow/step05_cns.js',import.meta.url),'utf8');
const step03=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
const routerSource=readFileSync(new URL('../cns_planner/api/router.py',import.meta.url),'utf8');
const towerLayer=readFileSync(new URL('../cns_planner/web/js/map/tower_reference_layer.js',import.meta.url),'utf8');

// ---- 显示计划夹具 ---------------------------------------------------------------

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
    longitude:ORIGIN[0]+0.00008*(index%20),
    latitude:ORIGIN[1]+0.00008*Math.floor(index/20),
    coordinate:[ORIGIN[0]+0.00008*(index%20),ORIGIN[1]+0.00008*Math.floor(index/20)],
    site_type:'地面角钢塔',
  }));
}

function flowFixture({items=null,colocation=[]}={}){
  return {
    nodes:[],reference_landing_sites:{items:[]},scenario_routes:[],
    towers:{status:'passed',count:(items||towers()).length,items:items||towers()},
    tower_colocation_candidates:{status:'passed',count:colocation.length,items:colocation},
  };
}

function plan({res,flow=null,layers={towerLayer:true},towerHighlight=null}={}){
  return buildDisplayPlan({
    flow:flow||flowFixture(),view:{res},size:SIZE,screenPoint:toScreen,screenToLonLat:toLonLat,
    layers,referenceOverlay:{referenceRoutes:[],referencePoints:[]},
    selectedReference:null,referenceFilters:{},filterReferenceSites:null,towerHighlight
  });
}

// ======================================================================================
// MAP-TOWER-ICON
// ======================================================================================

test('MAP-TOWER-ICON: the tower symbol is a local vector path with real tower features',()=>{
  assert.ok(TOWER_SYMBOL.segments.length>=8,'铁塔符号必须是多段矢量路径');
  // 顶部天线：从最高点向下的一小段竖线
  const mast=TOWER_SYMBOL.segments.find(([start,end])=>start[0]===0&&end[0]===0);
  assert.ok(mast,'必须有一段竖直的顶部天线');
  assert.ok(mast[0][1]<mast[1][1],'天线自上向下绘制');
  // 左右对称：每个线段的镜像也必须存在（塔架识别特征；横梁允许方向相反）
  const keys=new Set(TOWER_SYMBOL.segments.map(([a,b])=>`${a}|${b}`));
  for(const [a,b] of TOWER_SYMBOL.segments){
    if(a[0]===0&&b[0]===0)continue;
    const mirroredForward=`${[-a[0],a[1]]}|${[-b[0],b[1]]}`;
    const mirroredReverse=`${[-b[0],b[1]]}|${[-a[0],a[1]]}`;
    assert.ok(keys.has(mirroredForward)||keys.has(mirroredReverse),
      `缺少镜像线段 ${a}->${b}`);
  }
  const svg=towerSymbolSvg();
  assert.match(svg,/^<svg /);
  assert.match(svg,/stroke-width/);
  assert.doesNotMatch(svg,/https?:|\.png|\.svg"|emoji/,'图例符号不得引用外部资源');
});

test('MAP-TOWER-ICON: the legend and the map share one symbol definition',()=>{
  // towerSymbolSvg 与 drawTowerSymbol 都读同一个 TOWER_SYMBOL.segments
  assert.match(display,/for\(const \[start,end\] of TOWER_SYMBOL\.segments\)/);
  assert.equal((display.match(/TOWER_SYMBOL\.segments/g)||[]).length>=2,true,
    'canvas 绘制与 inline SVG 必须共用同一份线段表');
  // 画布函数可执行（记录调用即可，不需要真实 canvas）
  const strokes=[];
  const ctx={save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},
    stroke(){strokes.push(true);},set strokeStyle(value){this._stroke=value;},
    get strokeStyle(){return this._stroke;},set lineWidth(value){this._width=value;},
    get lineWidth(){return this._width;},set lineCap(value){}};
  drawTowerSymbol(ctx,10,10,{sizePx:22});
  assert.equal(strokes.length,2,'符号必须先画白色描边再画本体');
});

test('MAP-TOWER-ICON: the legend lives inside the one existing map legend container',()=>{
  assert.ok(html.includes('id="legend"'),'必须保留统一图例容器');
  assert.ok(html.includes('id="businessLegend"'),'统一图例内必须有业务数据分区容器');
  const legendBlock=html.slice(html.indexOf('id="legend"'),html.indexOf('id="gridNotice"'));
  assert.ok(legendBlock.includes('id="businessLegend"'),'业务分区必须在 #legend 内部');
  assert.equal(html.split('id="businessLegend"').length-1,1,'不得再造第二套图例容器');
  assert.equal(html.includes('id="towerLegend"'),false,'不得新建 Towers 专用 legend');
  assert.match(css,/\.legend>#businessLegend\{display:block/);
  assert.match(css,/\.legend-line\{display:flex/);
});

test('MAP-TOWER-ICON: the tower legend row is rendered with the shared tower symbol',()=>{
  const model=mapLegendModel({flow:flowFixture(),towerLayerOn:false});
  const titles=model.groups.map(group=>group.title);
  assert.ok(titles.includes(BUSINESS_LEGEND_LABEL),'真实业务数据分组必须存在');
  const line=model.groups.flatMap(group=>group.lines).find(item=>item.label===TOWER_LEGEND_LABEL);
  assert.ok(line,'图例必须包含通信铁塔一行');
  assert.match(line.symbol,/^<svg /,'铁塔图例符号必须是 inline SVG');
  assert.match(line.symbol,new RegExp(TOWER_SYMBOL.segments.length>0?'path':'never'));
  assert.equal(line.state,'图层默认关闭','图例必须如实说明默认关闭');
  const rendered=renderMapLegend(model);
  assert.match(rendered,new RegExp(TOWER_LEGEND_LABEL));
  assert.match(rendered,/data-legend-id="tower-reference"/);
  // 起降点 / 参考航路 / 航路点与铁塔同属一套 legend
  for(const id of ['reference-landing','reference-route','reference-route-point']){
    assert.match(rendered,new RegExp(`data-legend-id="${id}"`));
  }
  assert.ok(MARKER_SHAPES.includes('diamond')&&MARKER_SHAPES.includes('triangle'));
});

test('MAP-TOWER-ICON: detail LOD draws every real tower, overview clusters only',()=>{
  const detail=plan({res:100});
  assert.equal(detail.level,'detail');
  assert.equal(detail.towers.length,373,'detail 档必须逐塔显示 373 个铁塔');
  assert.ok(detail.towers.every(entry=>entry.single),'detail 档不再聚合');
  assert.equal(detail.towerMarkerMode,TOWER_MARKER_MODES.detail);

  const overview=plan({res:5000});
  assert.equal(overview.level,'overview');
  assert.ok(overview.towers.length>0,'overview 仍有聚合点');
  assert.ok(overview.towers.every(entry=>entry.count>1),'overview 只显示聚合点');
  assert.ok(overview.towers.length<373);

  const medium=plan({res:1000});
  assert.equal(medium.level,'medium');
  assert.equal(medium.towerMarkerMode,TOWER_MARKER_MODES.medium);
  assert.equal(towerSingleVisible('overview'),false);
  assert.equal(towerSingleVisible('medium'),true);
  assert.equal(towerSingleVisible('detail'),true);
});

test('MAP-TOWER-ICON: LOD reuses the existing style bundle and adds no second threshold set',()=>{
  for(const level of ['overview','medium','detail']){
    assert.ok(ROUTE_STYLES[level].towerMarkerMode,'每档都必须复用既有 displayStyle 给出 tower 模式');
  }
  assert.equal(displayStyle({res:5000}).styles.towerMarkerMode,TOWER_MARKER_MODES.overview);
  assert.equal(displayStyle({res:100}).styles.towerMarkerMode,TOWER_MARKER_MODES.detail);
  // 没有第二套 zoom/阈值常量
  assert.doesNotMatch(lod,/TOWER_ZOOM|towerThreshold|towerClusterPixels/);
});

test('MAP-TOWER-ICON: the single-tower branch draws the tower symbol, never a square',()=>{
  const towerLoop=display.slice(
    display.indexOf('for(const entry of plan.towers'),
    display.indexOf('// 7.6) CNS 共塔候选')
  );
  // MAP-TOWER-SYMBOL-V2：单塔分支必须按**真实像素尺寸**调用共享符号绘制。
  assert.match(towerLoop,/drawTowerSymbol\(ctx,x,y,\{/);
  assert.match(towerLoop,/sizePx/);
  assert.equal(towerLoop.includes("drawMarker(ctx,'square'"),false,
    'detail 档不得退回方形标记');
  assert.equal(towerLoop.includes("drawMarker(ctx,'diamond'"),false);
});

test('MAP-TOWER-ICON: the tower layer never fans out labels and stays hover/click only',()=>{
  const towerLoop=display.slice(
    display.indexOf('for(const entry of plan.towers'),
    display.indexOf('// 7.6) CNS 共塔候选')
  );
  assert.ok(towerLoop.length>0,'必须存在 towers 绘制循环');
  assert.doesNotMatch(towerLoop,/placer\.place/,'铁塔图层绝不铺开名称标签');
  assert.match(towerLayer,/canvas\.addEventListener\('mousemove'/);
  assert.match(main,/towerReference\.detail\(/);
});

test('FRONTEND: the default layer selection is unchanged',()=>{
  const boxes=new Map();
  for(const match of html.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)){
    const id=/\bid="([^"]+)"/.exec(match[0]);
    if(id)boxes.set(id[1],match[0]);
  }
  assert.ok(boxes.has('towerLayer'));
  assert.doesNotMatch(boxes.get('towerLayer'),/\bchecked\b/,'towerLayer 必须默认关闭');
  const defaults=[...boxes].filter(([,attributes])=>/\bchecked\b/.test(attributes)).map(([id])=>id);
  assert.deepEqual(defaults,['online']);
  assert.ok(main.includes("'towerLayer'"));
});

// ======================================================================================
// CNS 共塔候选 → 铁塔高亮
// ======================================================================================

function colocationItem(towerId='T0001',longitude=ORIGIN[0],latitude=ORIGIN[1]){
  return {
    site_id:'tower-colocation:'+towerId,coordinate:[longitude,latitude],
    available_subsystems:[],usable:true,locked:false,
    planning_profile:{reuse_class:'tower_colocation_host',confirmed:false,
      add_device_allowed:null,status:'pending_confirmation'},
    metadata:{host:{host_type:'tower',host_tower_id:towerId,host_tower_name:'站址0',
      host_site_type:'地面角钢塔',site_position_available:true,device_mount_confirmed:false}},
  };
}

test('FRONTEND: cns colocation candidates reach the plan and are hit-testable',()=>{
  const items=[colocationItem()];
  const result=plan({res:100,flow:flowFixture({colocation:items})});
  assert.equal(result.cnsTowerCandidates.length,1);
  assert.equal(result.cnsTowerCandidates[0].hostTowerId,'T0001');
  const hit=hitCnsTowerCandidate(result,[400,300]);
  assert.equal(hit.hostTowerId,'T0001');
  assert.equal(hitCnsTowerCandidate(result,[0,0]),null);
  // 候选站图层关闭时既不可见也不可命中
  const off=plan({res:100,flow:flowFixture({colocation:items}),layers:{towerLayer:true,candidateSiteLayer:false}});
  assert.equal(off.cnsTowerCandidates.length,0);
  assert.equal(hitCnsTowerCandidate(off,[400,300]),null);
});

test('FRONTEND: clicking a colocation candidate highlights its host tower',()=>{
  assert.match(main,/towerReference\.candidateClick\(event\)/);
  assert.match(main,/towerHighlight:towerReference\.highlightedTower\(\)/);
  // 命中与高亮都封装在只读交互模块里，main.js 只调用一行
  assert.ok(towerLayer.includes('export function showColocationCandidate'));
  assert.match(towerLayer,/highlightedTowerId=found\.hostTowerId/);
  assert.match(towerLayer,/highlightedTower\(\)\{return highlightedTowerId;\}/);
  const attach=towerLayer.slice(towerLayer.indexOf('export function attachTowerReferenceLayer'));
  assert.doesNotMatch(attach,/resourceAction\(|mutate\(|fetch\(|api\(/,'候选命中不得触发任何请求或状态写入');
  // 高亮绘制：只画一圈，不画永久连接线
  assert.match(display,/TOWER_HIGHLIGHT_COLOR/);
  assert.match(display,/String\(towerHighlight\|\|''\)===entry\.hostTowerId/);
  assert.doesNotMatch(display,/drawLine\(ctx,screenPoint,view,\[.*host/,'不得画候选到铁塔的永久连接线');
  // overview 下被高亮的塔仍然保留，否则点了候选看不到塔
  assert.match(display,/towerHighlight&&entry\.anchor&&entry\.anchor\.tower/);
});

test('FRONTEND: the tower click detail shows source fields, tower top and colocation link',()=>{
  const tower={tower_id:'T1',name:'站址1',longitude:122.1,latitude:30.1,elevation_m:12,
    height_m:45,district:'定海区',site_type:'地面角钢塔',source:{file_name:'t.xlsx',sheet:'S',row:3}};
  const plain=towerDetailHtml(tower,value=>String(value??''));
  for(const field of ['T1','站址1','122.1','30.1','12','45','定海区','地面角钢塔','t.xlsx','S','3']){
    assert.ok(plain.includes(field),`click 详情必须展示 ${field}`);
  }
  assert.match(plain,/规划采用塔顶高程/);
  assert.match(plain,/CNS 共塔候选：否/);
  const withProfile=towerDetailHtml(tower,value=>String(value??''),{
    obstacleProfile:{status:'resolved',base_type:'ground',terrain_elevation_m:100,
      building_height_m:null,tower_structure_height_m:45,tower_top_orthometric_m:145},
    colocation:{site_id:'tower-colocation:T1',metadata:{host:{host_type:'tower'}},
      planning_profile:{reuse_class:'tower_colocation_host',status:'confirmed'}},
  });
  assert.match(withProfile,/145\.0 m EGM2008/);
  assert.match(withProfile,/CNS 共塔候选：<b>是<\/b>/);
  assert.match(withProfile,/tower-colocation:T1/);
  // 未解析时明确写"未解析"，绝不编造数字
  const unresolved=towerTopHtml({status:'unresolved',base_type:'rooftop',vertical_status:'building_height_unresolved'},String);
  assert.match(unresolved,/未解析/);
  assert.match(unresolved,/building_height_unresolved/);
  assert.doesNotMatch(unresolved,/\d+\.\d+ m EGM2008/);
  const candidate=towerColocationDetailHtml(colocationItem(),String);
  assert.match(candidate,/CNS 共塔候选（宿主）/);
  assert.match(candidate,/Tower ID T0001/);
  assert.match(candidate,/设备挂载未确认/);
  assert.doesNotMatch(candidate,/coverage_radius|transmit_power|frequency/);
});

test('FRONTEND: Step05 shows a colocation badge and keeps reuse classes distinguishable',()=>{
  assert.match(step05,/共塔候选（真实铁塔宿主）/);
  assert.match(step05,/towerColocationList/);
  assert.match(step05,/tower_colocation_host:'共塔候选'/);
  assert.match(step05,/reuseClassLabel/);
  assert.match(step05,/deriveTowerColocation/);
  assert.match(step05,/\/api\/tower-obstacle-profiles\/evaluate/);
  assert.match(step05,/已有站点 /);
  assert.match(step05,/普通候选 /);
  // 共塔候选列表明确声明"不是已有设备 / 无设备参数"
  assert.match(step05,/不是已有 CNS 设备，也不带任何设备性能参数/);
});

// ---- 验收前三项收口：两个 Policy UI + 旧说明文字 -------------------------------------

test('FRONTEND: Step05 ships the Tower Colocation Policy form on the existing endpoint',()=>{
  assert.match(step05,/export function towerColocationPolicyForm/);
  const form=step05.slice(step05.indexOf('export function towerColocationPolicyForm'),
    step05.indexOf('function gapList('));
  for(const field of ['towerColocationOrigin','towerColocationPlanningHost',
    'towerColocationSource','saveTowerColocationPolicy']){
    assert.ok(form.includes(field),`共塔策略表单必须提供 ${field}`);
  }
  // 正式字段：service_origin_assumption / planning_host_use_confirmed / source
  assert.match(form,/policy\.service_origin_assumption/);
  assert.match(form,/tower_top_agl_0/);
  assert.match(form,/policy\.planning_host_use_confirmed/);
  assert.match(form,/policy\.source/);
  // FIX-TOWER-SEM-001：不得再用"该铁塔确实可以安装设备"这样的全局物理安装 checkbox
  assert.doesNotMatch(form,/设备挂载已确认/);
  assert.doesNotMatch(form,/该铁塔确实可以安装设备/);
  assert.doesNotMatch(form,/towerColocationMount/);
  assert.match(form,/允许真实铁塔作为共塔规划宿主候选（工程规划假设，不涉及物理安装确认）/);
  assert.match(form,/物理安装条件：<b>未逐塔核实，需现场勘察<\/b>/);
  assert.match(form,/physical_mount_confirmed=false · requires_site_survey=true/);
  // 复用现有端点：不新增第二套 API/contract
  const bind=step05.slice(step05.indexOf('export function bind(c)'));
  assert.match(bind,/saveTowerColocationPolicy[\s\S]{0,600}\/api\/tower-obstacle-profiles\/evaluate/);
  assert.match(bind,/service_origin_assumption:c\.\$\('towerColocationOrigin'\)\.value\|\|null/);
  assert.match(bind,/planning_host_use_confirmed:c\.\$\('towerColocationPlanningHost'\)\.checked/);
  assert.match(bind,/source:c\.\$\('towerColocationSource'\)\.value\.trim\(\)\|\|'user_configuration'/);
  assert.doesNotMatch(bind,/device_mount_confirmed:/,'不得再提交全局物理安装确认');
  assert.doesNotMatch(bind,/\/api\/tower-colocation-policy/,'不得新增共塔策略端点');
  assert.doesNotMatch(bind,/\/api\/tower-obstacle-profiles\/policy/);
  // 未确认仍然 ineligible；只有两个条件（B4X：界面文案不再裸露 ineligible 这个 raw 值）
  assert.match(form,/未启用（共塔候选尚不满足规划条件）/);
  assert.match(form,/两个条件（规划宿主允许 \+ 服务原点假设）满足才会进入规划/);
  assert.match(form,/分系统是否真的装得上保持未核实/);
  // 表单确实挂载在候选站址面板里
  assert.match(step05,/\+\s*towerColocationPolicyForm\(flow\)/);
});

test('FRONTEND: Step03 ships the Tower Clearance Policy form with no hidden defaults',()=>{
  assert.match(step03,/function towerClearancePanel\(flow\)/);
  const panel=step03.slice(step03.indexOf('function towerClearancePanel(flow)'),
    step03.indexOf('export function render({flow,interactionMode'));
  for(const field of ['towerVerticalClearance','towerHorizontalClearance',
    'towerClearanceConfirmed','towerClearanceSource','saveTowerClearancePolicy']){
    assert.ok(panel.includes(field),`塔净空面板必须提供 ${field}`);
  }
  assert.match(panel,/tower_vertical_clearance_m/);
  assert.match(panel,/tower_horizontal_clearance_m/);
  // 没有隐藏默认值：两个输入框都显式声明"必须显式填写，无默认值"
  assert.equal((panel.match(/必须显式填写，无默认值/g)||[]).length,2);
  assert.match(panel,/fail-closed/);
  // 面板挂在"结果 → 可行性与净空"段（建筑净空之后、连续验证之前）
  assert.match(step03,/buildingClearancePanel\(flow\)\+towerClearancePanel\(flow\)\+renderLayeredRouteValidation\(flow\)/);
  // 保存走既有 state 字段 + 显式确认
  const bind=step03.slice(step03.indexOf('c.actionButton(\'saveTowerClearancePolicy\''));
  assert.match(bind,/resourceAction\('\/api\/tower-clearance-policy'/);
  assert.match(bind,/tower_vertical_clearance_m:optional\('towerVerticalClearance'\)/);
  assert.match(bind,/tower_horizontal_clearance_m:optional\('towerHorizontalClearance'\)/);
  assert.match(bind,/confirmed:c\.\$\(\'towerClearanceConfirmed\'\)\.checked/);
  // 前后端契约一致：端点已注册
  assert.match(routerSource,/\/api\/tower-clearance-policy/);
  assert.match(routerSource,/set_tower_clearance_policy/);
});

test('FRONTEND: both policy forms actually render the acceptance fields',async()=>{
  // 运行时渲染（不只是源码正则）：确认模板真的产出了可提交的字段。
  const step05Module=await import('../cns_planner/web/js/workflow/step05_cns.js');
  const step03Module=await import('../cns_planner/web/js/workflow/step03_routes.js');
  const colocation=step05Module.towerColocationPolicyForm({
    tower_colocation_candidates:{count:3,policy:{
      service_origin_assumption:'tower_top_agl_0',planning_host_use_confirmed:true,
      physical_mount_confirmed:false,requires_site_survey:true,
      planning_host_status:'eligible',subsystem_mount_status:'unverified',
      source:'user_configuration',enabled:true,status:'confirmed',
    }},
    tower_obstacle_profiles:{resolved_count:200,unresolved_count:173},
  });
  for(const id of ['towerColocationOrigin','towerColocationPlanningHost',
    'towerColocationSource','saveTowerColocationPolicy']){
    assert.ok(colocation.includes(`id="${id}"`),`共塔策略表单缺少 ${id}`);
  }
  // 物理安装确认没有 UI 入口（不存在这样的全局 checkbox）
  assert.equal(colocation.includes('id="towerColocationMount"'),false);
  assert.equal(colocation.includes('id="towerColocationConfirmed"'),false);
  assert.match(colocation,/value="tower_top_agl_0" selected/);
  assert.match(colocation,/id="towerColocationPlanningHost" checked/);
  assert.match(colocation,/物理安装条件：<b>未逐塔核实，需现场勘察<\/b>/);
  assert.match(colocation,/规划宿主/);
  assert.match(colocation,/eligible/);
  assert.match(colocation,/分系统安装证据 未核实/);
  assert.match(colocation,/策略 已启用/);
  assert.match(colocation,/共塔候选 3 个 · 塔顶已解析 200 · 未解析 173/);

  const clearance=step03Module.towerClearancePanel({
    tower_clearance_policy:{status:'not_configured',confirmed:false},
    tower_obstacle_profiles:{resolved_count:256,unresolved_count:117},
    towers:{count:373},
  });
  for(const id of ['towerVerticalClearance','towerHorizontalClearance',
    'towerClearanceConfirmed','towerClearanceSource','saveTowerClearancePolicy']){
    assert.ok(clearance.includes(`id="${id}"`),`塔净空面板缺少 ${id}`);
  }
  // 未配置时两个输入框必须为空（不预填任何值）
  assert.equal((clearance.match(/id="tower(Vertical|Horizontal)Clearance" placeholder="必须显式填写，无默认值" value=""/g)||[]).length,2);
  assert.doesNotMatch(clearance,/id="towerVerticalClearance"[^>]*value="[0-9]/);
  assert.doesNotMatch(clearance,/id="towerHorizontalClearance"[^>]*value="[0-9]/);
  assert.match(clearance,/铁塔 373 个 · 塔顶已解析 256 · 未解析 117/);
  assert.match(clearance,/fail-closed/);
  // 已配置时回填
  const filled=step03Module.towerClearancePanel({
    tower_clearance_policy:{status:'confirmed',confirmed:true,source:'user_configuration',
      tower_vertical_clearance_m:25,tower_horizontal_clearance_m:80},
    tower_obstacle_profiles:{},towers:{count:373},
  });
  assert.match(filled,/id="towerVerticalClearance"[^>]*value="25"/);
  assert.match(filled,/id="towerHorizontalClearance"[^>]*value="80"/);
  assert.match(filled,/id="towerClearanceConfirmed" checked/);
});

test('FRONTEND: the Proposal surfaces the two-layer host/mount status',()=>{
  // P11/P16 Proposal 行必须显式显示 planning_host_status / subsystem_mount_status /
  // physical_mount_confirmed / requires_site_survey —— 不能只显示一个 eligible。
  assert.match(step05,/分系统安装 '\+escapeHtml\(action\.subsystem_mount_status\|\|'unverified'\)/);
  assert.match(step05,/物理安装'\+\(action\.physical_mount_confirmed===true\?'已确认':'未核实'\)/);
  assert.match(step05,/action\.requires_site_survey\?'（需现场勘察）':''/);
  // B4X：规划宿主状态仍显式回显后端字段，但显示文字经集中词表中文化
  //（not_applicable → 不适用），因此断言改为锁定"字段仍被展示"这一点。
  assert.match(step05,/规划宿主 '\+escapeHtml\(hostStatusLabel\(action\.planning_host_status\|\|'not_applicable'\)\)/);
  assert.match(step05,/function hostStatusLabel\(value\)/);
  // 共塔候选列表也不再声称"设备挂载已确认"
  assert.doesNotMatch(step05,/设备挂载'\+\(host\.device_mount_confirmed/);
  assert.match(step05,/物理安装 未核实（需现场勘察） · 分系统证据状态/);
});

test('FRONTEND: the Step05 reuse-tier description matches the real code order',()=>{
  const expected='Existing CNS → Existing Shared Site → Tower Colocation Host'
    +'（真实铁塔共塔宿主）→ Candidate Site → New-build Candidate';
  assert.ok(step05.includes(expected),'说明文字必须与 REUSE_TIERS 完全一致');
  assert.doesNotMatch(step05,/tier 固定为 Existing CNS → Existing Shared Site → Candidate Site/,
    '不得保留缺少共塔 tier 的旧说明');
  assert.match(step05,/prefer 共塔而不是 force/);
  // 与代码里的真实顺序一致
  const tiers=['existing_cns_facility','existing_shared_site','tower_colocation_host',
    'candidate_site','new_build_candidate'];
  const domainSource=readFileSync(
    new URL('../cns_planner/domain/site_planning.py',import.meta.url),'utf8');
  const block=domainSource.slice(domainSource.indexOf('REUSE_TIERS = ('),
    domainSource.indexOf('TOWER_COLOCATION_REUSE_CLASS'));
  let cursor=-1;
  for(const tier of tiers){
    const at=block.indexOf(`"${tier}"`);
    assert.ok(at>cursor,`REUSE_TIERS 顺序必须是 ${tiers.join(' → ')}`);
    cursor=at;
  }
});
