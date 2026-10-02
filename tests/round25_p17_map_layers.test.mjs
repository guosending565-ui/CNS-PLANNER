// Round 2.5 → Round 2.6 /「连续服务可接受性」的地图图层（本文件保留原名）：
//   1. Route Protection Corridor（水平保护走廊，Round 2.6 四分量）
//   2. Surveillance Protection Coverage（监视保护覆盖 / 缺口段，按分层着色）
//   3. 图例必须**显式区分且不得混同**：合作监视（RID）/ 非合作监视（Radar 补充）/
//      能力限制（黄色 / 橙色，**不是**系统错误）
//
// 锁定：
//   * index.html 里图层 checkbox **默认关闭**（没有 checked）；
//   * main.js 的 PROJECT_REOPEN_RESET_LAYER_IDS 覆盖这些 id（重开项目后仍关闭）；
//   * display_layers.js 的绘制入口：图层关闭时**零绘制**，开启时才画；
//   * routeOffsetToCoordinate：沿折线按累计距离取点、单调推进、越界 clamp；
//   * **Radar 求解不可行时绝不绘制任何 Radar 扇区 / 覆盖几何**（不得伪造扇区），
//     改为在图例与图层说明里显示真实候选 / 能力限制摘要；
//   * 缺值一律写「—」，绝不用 0 冒充证据。
//
// 与本目录其它前端测试一致：没有 jsdom，不伪造浏览器；绘制用最小 stub canvas context。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {drawWorkflowLayers,routeOffsetToCoordinate}
  from '../cns_planner/web/js/map/display_layers.js';
import {
  CONTINUOUS_SERVICE_LEGEND_COLORS,continuousServiceLegendModel,radarCapabilityLimitationSummary,
  radarSurveillanceLayoutState,
} from '../cns_planner/web/js/map/cns_service_overlay.js';
import {CONTINUOUS_SERVICE_LEGEND_LABEL,mapLegendModel,renderMapLegend}
  from '../cns_planner/web/js/workflow/map_legend.js';

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

/**
 * P17 快照：只放本图层需要读取的字段（结构照抄后端 result.routes[]，Round 2.6）。
 *
 * 合作分层（RID 主要威胁）有可用探测证据；非合作分层（Radar 补充威胁）在
 * Radar 求解不可行时是**能力限制**。
 */
function flowFixture({radarStatus='proposal_ready',solverStatus='optimal'}={}){
  return {
    cns_continuous_service:{
      result:{
        primary_threat_status:'satisfied',
        supplementary_threat_status:'limitation',
        limitations:[{
          limitation_id:'noncooperative_surveillance_limitation',layer:'noncooperative',
          capability:'Radar 非合作监视（补充威胁分层）',status:'limitation',
          blocking_primary_threat:false,source_status:radarStatus,solver_status:solverStatus,
          disclosure:'当前方案对合作无人机的监视链满足当前规划要求；非合作无人机补充监视能力因'
            +' Radar 布局不可行尚未闭合，属于当前方案能力限制。',
          must_disclose_in_report:true,
        }],
        routes:[{
          route_id:'R1',status:'evaluated',route_length_m:ROUTE_LENGTH_M,route_speed_mps:25,
          primary_threat_layer:'cooperative',supplementary_threat_layer:'noncooperative',
          supplementary_threat_is_limitation:true,
          threat_layers:{
            cooperative:{
              layer:'cooperative',label:'合作无人机 / RID 合作监视（主要威胁）',
              service_keys:['S:rid_cooperative'],subsystems:['nominal'],status:'nominal',
              t_margin_s:5.5,first_detection_distance_m:ROUTE_LENGTH_M,limitations:[],reasons:[],
            },
            noncooperative:{
              layer:'noncooperative',label:'非合作无人机 / Radar 非合作监视（补充威胁）',
              service_keys:['S:radar_noncooperative'],subsystems:['limitation'],status:'limitation',
              t_margin_s:null,first_detection_distance_m:null,limitations:[],reasons:[],
            },
          },
          limitations:[{
            limitation_id:'noncooperative_surveillance_limitation',layer:'noncooperative',
            capability:'Radar 非合作监视（补充威胁分层）',status:'limitation',
            blocking_primary_threat:false,source_status:radarStatus,solver_status:solverStatus,
            disclosure:'非合作无人机补充监视能力因 Radar 布局不可行尚未闭合，属于当前方案能力限制。',
            must_disclose_in_report:true,
          }],
          corridor:{
            route_path:ROUTE_PATH,route_length_m:ROUTE_LENGTH_M,
            cns_requirement_corridor_half_width_m:150,
            //: Round 2.6 四分量（D_separation / D_maneuver / D_uncertainty + V_relative/T_chain）。
            D_separation_m:50,D_separation_authority:'explicit_evidence',
            D_maneuver_m:50,D_maneuver_authority:'builtin_engineering_assumption',
            D_maneuver_semantics:'engineering_baseline_interface_not_regulatory_value',
            D_uncertainty_m:40,D_uncertainty_authority:'explicit_evidence',
            D_safety_m:50,
            V_relative_mps:40,relative_speed_basis:'conservative_closing',T_chain_s:7,
            D_protection_m:420,outer_half_width_m:570,
            formula:'D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty',
            status:'evaluated',
          },
          first_detection_evidence:{
            first_detection_distance_m:ROUTE_LENGTH_M,service_key:'S:rid_cooperative',
            coverage_status:'covered',usable:true,
          },
          surveillance_acceptance:{status:'acceptable',t_available_s:9.5,t_margin_s:5.5,reason:null},
          events_by_kind:{
            surveillance_detection_gap:[
              {kind:'surveillance_detection_gap',service:'S:rid_cooperative',
                start_offset_m:2000,end_offset_m:4500,length_m:2500},
              //: 非合作（Radar）探测缺口段：图层必须独立标注，绝不与合作分层混同。
              {kind:'surveillance_detection_gap',service:'S:radar_noncooperative',
                start_offset_m:6000,end_offset_m:7000,length_m:1000},
            ],
          },
          first_detection_evidence_noncooperative:{
            first_detection_distance_m:8000,service_key:'S:radar_noncooperative',
            coverage_status:'covered',usable:true,
          },
        }],
      },
    },
    radar_surveillance_layout:{
      status:radarStatus,
      items:[{
        route_id:'R1',status:radarStatus,
        //: 与后端一致：候选条目标记 demo_preview_only，overlay 才会取到这一条。
        demo_preview_only:true,
        candidate_tower_count:37,candidate_panel_count:74,
        selected_tower_count:0,selected_panel_count:0,
        solver:{status:solverStatus,optimality_proven:solverStatus==='optimal'},
        infeasibility_reasons:solverStatus==='infeasible'?['no_feasible_panel_selection']:[],
        //: 已选面板：只在**真实几何存在**（后端平面交截半径 + 经纬度）时才允许绘制。
        selected_panels:[{
          panel_id:'PANEL-1',tower_id:'T1',tower_name:'T1',
          longitude:122.05,latitude:30.02,radar_type:'radar_i',
          azimuth_deg:90,panel_half_width_deg:45,
          horizontal_inner_radius_m:120,horizontal_outer_radius_m:3000,
          origin_egm2008_m:100,plane_intersection_status:'intersected',
        }],
      }],
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
function draw(layers,flow=flowFixture()){
  const {ctx,calls,texts,dashes}=stubContext();
  drawWorkflowLayers({
    ctx,view:{x:0,y:0,res:100},flow,plan:planFixture(),layers,screenPoint,
    gridTheme:null,
    drawWorkspace:()=>{},drawGridThemes:()=>{},drawGridBoundaries:()=>{},
    drawBuildingFootprints:()=>{},drawConstraintLayer:()=>{},proposedPlanActions:()=>[],
  });
  return {calls,texts,dashes};
}

// ---- 1. 图层开关默认关闭 -------------------------------------------------------

test('连续服务两个图层开关存在于图层抽屉且默认不勾选',()=>{
  const boxes=checkboxAttributes(html);
  for(const id of ['routeProtectionCorridorLayer','surveillanceProtectionLayer']){
    assert.ok(boxes.has(id),`图层抽屉缺少开关 ${id}`);
    assert.doesNotMatch(boxes.get(id),/\bchecked\b/,`${id} 必须默认关闭`);
  }
  const defaults=[...boxes].filter(([,attributes])=>/\bchecked\b/.test(attributes)).map(([id])=>id);
  assert.deepEqual(defaults,['online'],'新增连续服务图层不得改变"默认只勾选在线底图"的策略');
});

test('图层抽屉按 Round 2.6 语义命名合作 / 非合作监视',()=>{
  //: 单选框标签必须让用户分清"合作监视（RID）"与"非合作监视（Radar 补充）"。
  assert.match(html,/RID cooperative protection（合作监视保护范围/);
  assert.match(html,/Radar 非合作补充覆盖/);
  assert.match(html,/求解不可行时不画任何扇区/);
  assert.match(html,/Surveillance Protection Coverage（监视保护覆盖：合作 RID \/ 非合作 Radar 分层着色）/);
});

// ---- 2. 重开项目后仍然关闭 -----------------------------------------------------

test('重开项目会把连续服务图层复位为关闭，且不改变复位语义',()=>{
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

test('连续服务图层默认关闭时零绘制，开启后才绘制',()=>{
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
  assert.ok(corridor.texts.some(text=>text.includes('D_protection=420 m')),'D_protection 取整到米');
  assert.ok(corridor.texts.some(text=>text.includes('V_rel=40.0 m/s')),'V_rel 保留 1 位小数');
  assert.ok(corridor.texts.some(text=>text.includes('T_chain=7.0 s')),'T_chain 保留 1 位小数');

  const surveillance=draw({surveillanceProtectionLayer:true});
  assert.ok(surveillance.calls.beginPath>0,'监视保护覆盖开启后必须画航路两端监视点');
  assert.ok(surveillance.calls.fillText>0,'必须标注 T_margin');
  assert.ok(surveillance.texts.some(text=>text.includes('T_margin=5.5 s')),'T_margin 保留 1 位小数');
});

test('监视覆盖按合作 / 非合作分层分别标注，绝不合并成一个监视结论',()=>{
  const {texts}=draw({surveillanceProtectionLayer:true});
  assert.ok(texts.some(text=>text.includes('[合作 RID 主要] T_margin=5.5 s')),
    '合作分层必须有自己的 T_margin 标注');
  assert.ok(texts.some(text=>text.includes('[非合作 Radar 补充]')),
    '非合作分层必须有自己的（独立的）标注，不得与合作分层共用一行');
});

test('缺值时标注写「—」，绝不用 0 冒充证据',()=>{
  const flow=flowFixture();
  const corridor=flow.cns_continuous_service.result.routes[0].corridor;
  corridor.D_protection_m=null;corridor.V_relative_mps=undefined;corridor.T_chain_s=NaN;
  //: 没有可用探测证据 ⇒ 不画覆盖点（"没有覆盖几何"绝不用一个点冒充）。
  flow.cns_continuous_service.result.routes[0].first_detection_evidence={
    service_key:'S:rid_cooperative',usable:false,
  };
  flow.cns_continuous_service.result.routes[0].surveillance_acceptance={status:'unknown'};
  //: 分层自己的 T_margin 也缺失 ⇒ 必须写「—」，绝不回退到任何数字。
  flow.cns_continuous_service.result.routes[0].threat_layers.cooperative.t_margin_s=null;
  flow.cns_continuous_service.result.routes[0].events_by_kind={surveillance_detection_gap:[]};
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
  assert.ok(texts.some(text=>/T_margin=—/.test(text)),'缺失 T_margin 必须写 T_margin=—，绝不用 0');
  assert.equal(calls.arc,0,'usable=false 时不得画覆盖点（没有覆盖几何就不画）');
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

// ---- 6. Radar 不可行：能力限制（非 error），且绝不绘制任何扇区 -----------------

test('radarSurveillanceLayoutState 只识别显式不可行结论',()=>{
  assert.equal(radarSurveillanceLayoutState(flowFixture()).infeasible,false);
  assert.equal(radarSurveillanceLayoutState(flowFixture({solverStatus:'infeasible'})).infeasible,true);
  assert.equal(radarSurveillanceLayoutState(flowFixture({radarStatus:'infeasible'})).infeasible,true);
  //: 证据缺失（not_calculated / missing_data）不是"能力限制"。
  assert.equal(radarSurveillanceLayoutState(flowFixture({radarStatus:'not_calculated',
    solverStatus:'not_run'})).infeasible,false);
  const state=radarSurveillanceLayoutState(flowFixture({solverStatus:'infeasible'}));
  assert.equal(state.stateLabel,'能力限制（黄色 / 橙色）',
    '不可行必须标为能力限制，绝不是 error / 系统错误');
});

test('Radar 求解不可行时绝不绘制任何 Radar 扇区 / 覆盖几何',()=>{
  const infeasible=flowFixture({solverStatus:'infeasible',radarStatus:'infeasible'});
  const {calls,texts}=draw({radarSurveillanceLayer:true},infeasible);
  assert.equal(calls.beginPath,0,'不可行时不得产生任何路径（绝不伪造扇区）');
  assert.equal(calls.arc,0,'不可行时不得画任何塔位 / 覆盖点');
  assert.equal(calls.fill,0,'不可行时不得填充任何几何');
  assert.equal(calls.stroke,0,'不可行时不得描边任何几何');
  assert.ok(calls.fillText>0,'不可行时必须留下真实候选 / 能力限制说明');
  assert.ok(texts.some(text=>text.includes('能力限制（黄色/橙色，不是系统错误）')),
    '说明必须标为能力限制，不是系统错误');
  assert.ok(texts.some(text=>text.includes('不绘制任何 Radar 扇区 / 覆盖几何')),
    '必须明确写出不绘制任何扇区 / 覆盖几何');
  assert.ok(texts.some(text=>text.includes('候选铁塔 37 个')),'必须如实给出真实候选规模');
});

test('Radar 可行时仍按既有 overlay 绘制（能力限制不得误伤正常路径）',()=>{
  const feasible=flowFixture();
  const {calls}=draw({radarSurveillanceLayer:true},feasible);
  assert.ok(calls.beginPath>0,'可行时雷达 overlay 必须正常绘制');
});

test('连续服务图例显式区分合作监视 / 非合作监视 / 能力限制三类',()=>{
  const model=continuousServiceLegendModel({flow:flowFixture()});
  const byId=Object.fromEntries(model.map(line=>[line.id,line]));
  for(const id of ['layer-rid-cooperative-protection',
    'layer-radar-noncooperative-supplementary','capability-limitation']){
    assert.ok(byId[id],`图例缺少 ${id}`);
  }
  assert.match(byId['layer-rid-cooperative-protection'].label,/RID cooperative protection/);
  assert.match(byId['layer-rid-cooperative-protection'].state,/合作监视（RID）/);
  assert.match(byId['layer-radar-noncooperative-supplementary'].label,
    /Radar non-cooperative supplementary coverage/);
  assert.match(byId['layer-radar-noncooperative-supplementary'].state,/非合作监视（Radar 补充）/);
  //: 三类必须用**不同**的颜色，绝不混同。
  const colors=[CONTINUOUS_SERVICE_LEGEND_COLORS.cooperativeSurveillance,
    CONTINUOUS_SERVICE_LEGEND_COLORS.noncooperativeSurveillance,
    CONTINUOUS_SERVICE_LEGEND_COLORS.capabilityLimitation];
  assert.equal(new Set(colors).size,3,'合作 / 非合作 / 能力限制必须是三种不同颜色');
  assert.ok(byId['layer-rid-cooperative-protection'].note.includes('不是非合作监视能力'));
});

test('Radar 不可行时图例项状态标为能力限制，并给出真实候选 / 限制摘要',()=>{
  const flow=flowFixture({solverStatus:'infeasible',radarStatus:'infeasible'});
  const model=continuousServiceLegendModel({flow});
  const radar=model.find(line=>line.id==='layer-radar-noncooperative-supplementary');
  const limitation=model.find(line=>line.id==='capability-limitation');
  assert.equal(radar.state,'能力限制（黄色 / 橙色）');
  assert.equal(radar.limitation,true);
  assert.match(radar.note,/不绘制任何 Radar 扇区 \/ 覆盖几何/);
  assert.match(radar.note,/候选铁塔 37 个/);
  assert.equal(limitation.limitation,true);
  assert.match(limitation.note,/不是系统错误/);
  assert.match(limitation.note,/不得表述为「监视已完全满足」/);
  assert.match(limitation.note,/no_feasible_panel_selection/);

  const summary=radarCapabilityLimitationSummary(flow);
  assert.match(summary,/候选铁塔 37 个/);
  assert.match(summary,/候选面板 74 个/);
  assert.match(summary,/能力限制/);
  assert.equal(radarCapabilityLimitationSummary(flowFixture()),'',
    '可行时不得给出任何能力限制摘要');
});

test('统一图例渲染包含分层图例行与不可行摘要',()=>{
  const flow=flowFixture({solverStatus:'infeasible',radarStatus:'infeasible'});
  const model=mapLegendModel({flow});
  const group=model.groups.find(item=>item.title===CONTINUOUS_SERVICE_LEGEND_LABEL);
  assert.ok(group,'图例必须保留连续服务分区');
  const ids=group.lines.map(line=>line.id);
  for(const id of ['route-protection-corridor','surveillance-protection-coverage',
    'layer-rid-cooperative-protection','layer-radar-noncooperative-supplementary',
    'capability-limitation']){
    assert.ok(ids.includes(id),`图例分区缺少 ${id}`);
  }
  assert.ok(group.note&&group.note.includes('能力限制'),'不可行摘要必须出现在图例里');
  const rendered=renderMapLegend(model);
  assert.match(rendered,/data-legend-id="capability-limitation"/);
  assert.match(rendered,/data-capability-limitation="true"/);
  assert.match(rendered,/data-legend-note="true"/);
  //: 图例分区标题不再带（P17）代号。
  assert.doesNotMatch(rendered,/CNS 连续服务（P17）/);
});
