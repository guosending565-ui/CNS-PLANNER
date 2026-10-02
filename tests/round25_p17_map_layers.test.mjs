// Round 2.5 / P17「连续服务可接受性」的两个地图图层：
//   1. Route Protection Corridor（水平保护走廊）
//   2. Surveillance Protection Coverage（监视保护覆盖 / 缺口段）
//
// 锁定四件事：
//   * index.html 里两个 checkbox **默认关闭**（没有 checked）；
//   * main.js 的 PROJECT_REOPEN_RESET_LAYER_IDS 覆盖这两个 id（重开项目后仍关闭）；
//   * display_layers.js 的绘制入口：图层关闭时**零绘制**，开启时才画；
//   * routeOffsetToCoordinate：沿折线按累计距离取点、单调推进、越界 clamp。
//
// 与本目录其它前端测试一致：没有 jsdom，不伪造浏览器；绘制用最小 stub canvas context。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {drawWorkflowLayers,routeOffsetToCoordinate}
  from '../cns_planner/web/js/map/display_layers.js';

const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');

const ROUTE_PATH=[[122.0,30.0],[122.1,30.0],[122.1,30.08]];
const ROUTE_LENGTH_M=12000;

function checkboxAttributes(source){
  const found=new Map();
  for(const match of source.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)){
    const id=/\bid="([^"]+)"/.exec(match[0]);
    if(id)found.set(id[1],match[0]);
  }
  return found;
}

/** 最小 stub canvas context：只记录调用次数、文字与虚线样式。 */
function stubContext(){
  const calls={save:0,restore:0,beginPath:0,moveTo:0,lineTo:0,arc:0,fill:0,stroke:0,fillText:0,setLineDash:0};
  const texts=[],dashes=[];
  const ctx={
    font:'',fillStyle:'',strokeStyle:'',lineWidth:1,globalAlpha:1,lineCap:'butt',
    save(){calls.save+=1;},restore(){calls.restore+=1;},
    beginPath(){calls.beginPath+=1;},closePath(){},
    moveTo(){calls.moveTo+=1;},lineTo(){calls.lineTo+=1;},
    arc(){calls.arc+=1;},rect(){},
    fill(){calls.fill+=1;},stroke(){calls.stroke+=1;},
    setLineDash(dash){calls.setLineDash+=1;dashes.push(Array.isArray(dash)?dash.slice():dash);},
    fillText(text){calls.fillText+=1;texts.push(String(text));},
    strokeText(){},measureText(text){return {width:String(text).length*6};},
    fillRect(){},strokeRect(){},drawImage(){},
  };
  return {ctx,calls,texts,dashes};
}

/** P17 快照：只放本图层需要读取的字段（结构照抄后端 result.routes[]）。 */
function flowFixture(){
  return {
    cns_continuous_service:{
      result:{
        routes:[{
          route_id:'R1',status:'evaluated',route_length_m:ROUTE_LENGTH_M,route_speed_mps:25,
          corridor:{
            route_path:ROUTE_PATH,route_length_m:ROUTE_LENGTH_M,
            cns_requirement_corridor_half_width_m:150,
            D_safety_m:50,D_uncertainty_m:40,V_relative_mps:40,
            relative_speed_basis:'conservative_closing',T_chain_s:7,
            D_protection_m:480,outer_half_width_m:630,
            formula:'D_safety + V_relative * T_chain + D_uncertainty',status:'evaluated',
          },
          first_detection_evidence:{
            first_detection_distance_m:ROUTE_LENGTH_M,service_key:'S:radar_noncooperative',
            coverage_status:'covered',usable:true,
          },
          surveillance_acceptance:{status:'acceptable',t_available_s:9.5,t_margin_s:5.5,reason:null},
          events_by_kind:{
            surveillance_detection_gap:[
              {kind:'surveillance_detection_gap',start_offset_m:2000,end_offset_m:4500,length_m:2500},
            ],
          },
        }],
      },
    },
  };
}

function planFixture(){
  return {
    styles:{scenarioAlpha:1,scenarioWidth:2,operationalWidth:3,operationalAlpha:1,gapWidth:2,pointRadius:4,coverageRing:false,nameMode:'hidden'},
    nodes:[],landingSites:[],towers:[],cnsTowerCandidates:[],
    priorityIds:new Set(),priorityCoordinates:new Set(),
    selectedReferenceId:null,selectedScreen:null,
  };
}

//: 只用于把经纬度放到屏幕上的可逆线性换算（不参与被测实现，仅测试辅助）。
const screenPoint=coordinate=>[
  (Number(coordinate[0])-121.9)*10000,
  (30.1-Number(coordinate[1]))*10000,
];

/** 走真正的绘制入口 drawWorkflowLayers，返回 stub ctx 的调用记录。 */
function draw(layers){
  const {ctx,calls,texts,dashes}=stubContext();
  drawWorkflowLayers({
    ctx,view:{x:0,y:0,res:100},flow:flowFixture(),plan:planFixture(),layers,screenPoint,
    gridTheme:null,
    drawWorkspace:()=>{},drawGridThemes:()=>{},drawGridBoundaries:()=>{},
    drawBuildingFootprints:()=>{},drawConstraintLayer:()=>{},proposedPlanActions:()=>[],
  });
  return {calls,texts,dashes};
}

// ---- 1. 图层开关默认关闭 -------------------------------------------------------

test('P17 两个图层开关存在于图层抽屉且默认不勾选',()=>{
  const boxes=checkboxAttributes(html);
  for(const id of ['routeProtectionCorridorLayer','surveillanceProtectionLayer']){
    assert.ok(boxes.has(id),`图层抽屉缺少开关 ${id}`);
    assert.doesNotMatch(boxes.get(id),/\bchecked\b/,`${id} 必须默认关闭`);
  }
  const defaults=[...boxes].filter(([,attributes])=>/\bchecked\b/.test(attributes)).map(([id])=>id);
  assert.deepEqual(defaults,['online'],'新增 P17 图层不得改变"默认只勾选在线底图"的策略');
});

// ---- 2. 重开项目后仍然关闭 -----------------------------------------------------

test('重开项目会把 P17 图层复位为关闭，且不改变复位语义',()=>{
  const block=main.slice(
    main.indexOf('const PROJECT_REOPEN_RESET_LAYER_IDS='),
    main.indexOf('async function openProject('));
  assert.ok(block.length>0,'main.js 必须保留复位常量与函数');
  for(const id of ['routeProtectionCorridorLayer','surveillanceProtectionLayer']){
    assert.match(block,new RegExp("'"+id+"'"),`复位列表缺少 ${id}`);
  }
  assert.match(block,/removeAttribute\('checked'\)/,'复位必须只取消勾选');
  assert.doesNotMatch(block,/'online'/,'复位列表不得包含在线底图');
  // LAYER_IDS 必须包含两个 id，否则 layerSwitches 读不到、图层永远不画。
  const layerIds=/const LAYER_IDS=\[([^\]]*)\]/.exec(main);
  assert.ok(layerIds,'LAYER_IDS 必须保持单一字面量列表');
  for(const id of ['routeProtectionCorridorLayer','surveillanceProtectionLayer']){
    assert.match(layerIds[1],new RegExp("'"+id+"'"),`main.js 的 LAYER_IDS 缺少 ${id}`);
  }
});

// ---- 3. 默认关闭不绘制 / 开启才绘制 --------------------------------------------

test('P17 图层默认关闭时零绘制，开启后才绘制',()=>{
  const off=draw({});
  assert.equal(off.calls.beginPath,0,'图层未开启时不得有任何路径绘制');
  assert.equal(off.calls.fillText,0,'图层未开启时不得有任何标注文字');
  assert.equal(off.calls.stroke,0,'图层未开启时不得有任何描边');

  const explicitOff=draw({routeProtectionCorridorLayer:false,surveillanceProtectionLayer:false});
  assert.equal(explicitOff.calls.beginPath,0,'显式 false 同样不绘制');
  assert.equal(explicitOff.calls.fillText,0,'显式 false 同样不标注');

  const corridor=draw({routeProtectionCorridorLayer:true});
  assert.ok(corridor.calls.beginPath>0,'水平保护走廊开启后必须画走廊带 / 端面');
  assert.ok(corridor.calls.fill>0,'走廊带必须是半透明填充带');
  assert.ok(corridor.calls.fillText>0,'走廊必须标注 D_protection / V_rel / T_chain');
  assert.ok(corridor.texts.some(text=>text.includes('D_protection=')),
    '标注必须包含 D_protection');
  assert.ok(corridor.texts.some(text=>text.includes('D_protection=480 m')),'D_protection 取整到米');
  assert.ok(corridor.texts.some(text=>text.includes('V_rel=40.0 m/s')),'V_rel 保留 1 位小数');
  assert.ok(corridor.texts.some(text=>text.includes('T_chain=7.0 s')),'T_chain 保留 1 位小数');

  const surveillance=draw({surveillanceProtectionLayer:true});
  assert.ok(surveillance.calls.beginPath>0,'监视保护覆盖开启后必须画航路两端监视点');
  assert.ok(surveillance.calls.fillText>0,'必须标注 T_margin');
  assert.ok(surveillance.texts.some(text=>text.includes('T_margin=5.5 s')),'T_margin 保留 1 位小数');
});

test('P17 缺值时标注写「—」，绝不用 0 冒充证据',()=>{
  const flow=flowFixture();
  const corridor=flow.cns_continuous_service.result.routes[0].corridor;
  corridor.D_protection_m=null;corridor.V_relative_mps=undefined;corridor.T_chain_s=NaN;
  flow.cns_continuous_service.result.routes[0].surveillance_acceptance={status:'unknown'};
  const {ctx,calls,texts}=stubContext();
  drawWorkflowLayers({
    ctx,view:{x:0,y:0,res:100},flow,plan:planFixture(),
    layers:{routeProtectionCorridorLayer:true,surveillanceProtectionLayer:true},screenPoint,
    gridTheme:null,
    drawWorkspace:()=>{},drawGridThemes:()=>{},drawGridBoundaries:()=>{},
    drawBuildingFootprints:()=>{},drawConstraintLayer:()=>{},proposedPlanActions:()=>[],
  });
  assert.ok(calls.fillText>=2,'两个图层都必须留下标注');
  assert.ok(texts.some(text=>text.includes('D_protection=— m')),'缺失 D_protection 必须写「—」');
  assert.ok(texts.some(text=>text.includes('V_rel=— m/s')),'缺失 V_relative 必须写「—」');
  assert.ok(texts.some(text=>text.includes('T_chain=— s')),'缺失 T_chain 必须写「—」');
  assert.ok(texts.some(text=>text==='T_margin=—'),'缺失 T_margin 必须写 T_margin=—');
});

// ---- 4. 里程 → 经纬度（沿折线累计距离） ----------------------------------------

test('routeOffsetToCoordinate 沿折线按累计距离取点并 clamp 到端点',()=>{
  const path=[[0,0],[0,0.01]];
  assert.deepEqual(routeOffsetToCoordinate(path,1000,0),[0,0],'offset=0 必须落在起点');
  const mid=routeOffsetToCoordinate(path,1000,500);
  assert.ok(mid[1]>0&&mid[1]<0.01,'offset 在中间时坐标必须落在两端之间');
  assert.ok(Math.abs(mid[1]-0.005)<1e-9,'按里程线性插值');
  assert.deepEqual(routeOffsetToCoordinate(path,1000,1000),[0,0.01],'offset=全长落在终点');
  assert.deepEqual(routeOffsetToCoordinate(path,1000,5000),[0,0.01],'超界必须 clamp 到终点');
  assert.deepEqual(routeOffsetToCoordinate(path,1000,-100),[0,0],'负值必须 clamp 到起点');

  // 随 offset 单调推进
  const latitudes=[0,100,250,500,750,1000].map(offset=>routeOffsetToCoordinate(path,1000,offset)[1]);
  for(let index=1;index<latitudes.length;index+=1){
    assert.ok(latitudes[index]>=latitudes[index-1],'坐标必须随 offset 单调推进');
  }

  // 折线两段等长：按累计距离（而不是按顶点序号）取点
  const longPath=[[0,0],[0,1],[0,2]];
  assert.deepEqual(routeOffsetToCoordinate(longPath,200,50),[0,0.5]);
  assert.deepEqual(routeOffsetToCoordinate(longPath,200,150),[0,1.5]);
  assert.deepEqual(routeOffsetToCoordinate(longPath,200,200),[0,2]);

  // route_length_m 缺失时退化为"offset 即折线比例"，仍然 clamp
  assert.deepEqual(routeOffsetToCoordinate(path,NaN,0.5),[0,0.005]);
  assert.deepEqual(routeOffsetToCoordinate(path,NaN,3),[0,0.01]);
  // 空路径 / 非法 offset 不编造坐标
  assert.equal(routeOffsetToCoordinate([],1000,10),null);
  assert.equal(routeOffsetToCoordinate(path,1000,NaN),null);
});

// ---- 5. 覆盖缺口段（沿航路里程）按虚线绘制 -------------------------------------

test('surveillance_detection_gap 里程段在开启图层后按虚线绘制',()=>{
  const {calls,texts,dashes}=draw({surveillanceProtectionLayer:true});
  const dashed=dashes.filter(dash=>Array.isArray(dash)&&dash.length>0);
  assert.ok(dashed.length>0,'缺口段必须走虚线（setLineDash 收到非空 dash）');
  assert.deepEqual(dashed[0],[6,4],'缺口段虚线与既有缺口图层风格一致');
  assert.ok(calls.stroke>0,'缺口段必须真的描边');
  assert.ok(calls.arc>=2,'航路两端必须各画一个监视覆盖点');
  assert.ok(texts.some(text=>text.includes('T_margin=')),'必须标注 T_margin');

  // 关闭时不得出现任何虚线
  const off=draw({});
  assert.equal(off.dashes.length,0,'图层关闭时不得设置任何虚线');
});
