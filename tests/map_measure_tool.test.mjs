/**
 * BUG-MAP-001：地图测距工具的前端契约测试。
 *
 * 覆盖两件事：
 * 1. **距离数学**：WGS84 经纬度按 R = 6371008.8 m 的球面（haversine）地表距离；
 *    < 1 km 显示 m，>= 1 km 显示 km；非法输入绝不返回 0。
 * 2. **交互**：``interactionMode === 'measure'`` 时**绝不** pan、不加节点、不框工作区、
 *    不触发双击 zoom；单击加点、双击完成、Esc / 清除退出；整条链路不调用任何 API。
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  EARTH_RADIUS_M,MEASURE_HINT,attachMeasureTool,createMeasureTool,formatDistance,
  haversineDistanceM,measureModel,pathDistanceM,
} from '../cns_planner/web/js/map/measure_tool.js';
import {bindMapInteraction} from '../cns_planner/web/js/map/interaction.js';

const ROOT=new URL('../',import.meta.url);
const readFile=path=>readFileSync(new URL(path,ROOT),'utf8');
const closeTo=(actual,expected,tolerance=1e-6)=>{
  assert.ok(Math.abs(actual-expected)<=tolerance,`${actual} 与 ${expected} 相差超过 ${tolerance}`);
};

// ============================================================================
// 1. 距离数学
// ============================================================================

test('haversine uses the WGS84 mean radius 6371008.8 for surface distance',()=>{
  assert.equal(EARTH_RADIUS_M,6371008.8);
  // 1° 纬度 ≈ R · π/180 = 111195.0802335329 m
  closeTo(haversineDistanceM([122,30],[122,31]),EARTH_RADIUS_M*Math.PI/180,1e-6);
  // 赤道上 1° 经度与 1° 纬度等价
  closeTo(haversineDistanceM([0,0],[1,0]),EARTH_RADIUS_M*Math.PI/180,1e-6);
  // 同一点是 0，且 45° 处的 1° 经度必须小于赤道（球面语义，不是平面投影）
  assert.equal(haversineDistanceM([122,30],[122,30]),0);
  assert.ok(haversineDistanceM([0,45],[1,45])<haversineDistanceM([0,0],[1,0]));
});

test('haversine never turns invalid input into a zero distance',()=>{
  for(const broken of [null,undefined,[],[1],['a','b'],[NaN,30],[122,Infinity],'122,30',{}]){
    assert.equal(haversineDistanceM(broken,[122,30]),null,String(broken));
    assert.equal(haversineDistanceM([122,30],broken),null,String(broken));
  }
});

test('distance formatting switches from m to km exactly at one kilometre',()=>{
  assert.equal(formatDistance(0),'0 m');
  assert.equal(formatDistance(999),'999 m');
  assert.equal(formatDistance(999.4),'999 m');
  assert.equal(formatDistance(1000),'1.00 km');
  assert.equal(formatDistance(1500),'1.50 km');
  assert.equal(formatDistance(111195.08),'111.20 km');
  for(const broken of [null,undefined,NaN,'x',-1])assert.equal(formatDistance(broken),'—');
});

test('measureModel accumulates per-segment and total surface distance',()=>{
  const model=measureModel([[122,30],[122,31],[123,31]]);
  const first=EARTH_RADIUS_M*Math.PI/180;
  closeTo(model.segments[0].lengthM,first,1e-6);
  closeTo(model.segments[0].cumulativeM,first,1e-6);
  closeTo(model.totalM,pathDistanceM([[122,30],[122,31],[123,31]]),1e-9);
  assert.equal(model.segments[0].label,'111.20 km');
  assert.ok(model.segments[1].lengthM<first,'高纬度的 1° 经度必须更短');
  assert.equal(model.complete,true);
  assert.equal(model.totalLabel,formatDistance(model.totalM));
  assert.equal(measureModel([[122,30]]).totalLabel,null,'单点不得显示 0 m');
  assert.deepEqual(measureModel([null,'x',[1]]).points,[]);
});

// ============================================================================
// 2. 测距状态机
// ============================================================================

test('measure tool only collects points while it is active',()=>{
  const tool=createMeasureTool();
  tool.addPoint([122,30]);
  assert.deepEqual(tool.state().points,[],'未激活时不得收集任何点');
  tool.setActive(true);
  tool.addPoint([122,30]);
  tool.addPoint([122,31]);
  assert.equal(tool.state().points.length,2);
  assert.equal(tool.totalLabel(),'111.20 km');
});

test('measure tool is idempotent, finish freezes the reading and clear drops every point',()=>{
  const tool=createMeasureTool();
  tool.setActive(true);
  tool.addPoint([122,30]);
  tool.addPoint([122,31]);
  // 重复设成同一个状态绝不能丢掉已经点好的顶点。
  tool.setActive(true);
  assert.equal(tool.state().points.length,2);

  tool.finish();
  tool.setHover([123,31]);
  assert.equal(tool.state().hover,null,'完成之后悬停不得再改变读数');
  assert.equal(tool.state().points.length,2);

  tool.clear();
  assert.deepEqual(tool.state().points,[]);
  assert.equal(tool.totalLabel(),null);

  tool.addPoint([123,31]);
  tool.setActive(false);
  assert.deepEqual(tool.state().points,[],'退出测距必须丢弃全部点');
  assert.equal(tool.isActive(),false);
});

test('measure tool describes hover and never mutates anything but its own state',()=>{
  const updates=[];
  const tool=createMeasureTool({onUpdate:state=>updates.push(state.points.length)});
  tool.setActive(true);
  tool.addPoint([122,30]);
  tool.setHover([122,30.5],[10,10]);
  assert.deepEqual(tool.state().hover,[122,30.5]);
  assert.ok(updates.length>=3);

  const calls=[];
  const ctx={
    save(){calls.push('save')},restore(){calls.push('restore')},beginPath(){},moveTo(){},lineTo(){},
    stroke(){calls.push('stroke')},arc(){calls.push('arc')},fill(){},fillText(){},fillRect(){},
    setLineDash(){},measureText(){return {width:20}},font:'',textAlign:'',textBaseline:'',
    fillStyle:'',strokeStyle:'',lineWidth:0,globalAlpha:1,
  };
  assert.equal(tool.draw(ctx,point=>[point[0],point[1]]),1);
  assert.ok(calls.includes('arc'),'每个顶点必须画出可见标记');
  assert.ok(calls.includes('stroke'));
  // 退出后不再绘制任何东西。
  tool.setActive(false);
  assert.equal(tool.draw(ctx,point=>point),0);
});

// ============================================================================
// 3. 与地图交互的耦合（BUG-MAP-001 的核心要求）
// ============================================================================

function fakeTarget(){
  const listeners=new Map();
  return {
    listeners,
    addEventListener(type,handler){listeners.set(type,handler);},
    setPointerCapture(){},
    classList:{add(){},remove(){}},
    getBoundingClientRect(){return {left:0,top:0,width:800,height:600};},
    dispatch(type,event={}){
      const handler=listeners.get(type);
      return handler?handler(event):undefined;
    }
  };
}

function bindFake(getMode){
  const map=fakeTarget(),canvas=fakeTarget();
  const seen={pans:0,nodes:[],drafts:[],completes:0,zooms:0,positions:0};
  bindMapInteraction({
    map,canvas,getView:()=>({x:0,y:0,res:100}),setView(){},getMode,
    eventLonLat:event=>[event.lon??122,event.lat??30],
    zoom(){seen.zooms++;},queue(){},paint(){},
    onDraft:value=>seen.drafts.push(value),onDraftComplete(){seen.completes++;},
    onNode:async coordinate=>{seen.nodes.push(coordinate);},
    onPosition(){seen.positions++;},onPanStart(){seen.pans++;}
  });
  return {map,canvas,seen};
}

test('measure mode never pans, never draws a workspace box and never adds a node',async()=>{
  const {canvas,seen}=bindFake(()=>'measure');

  await canvas.dispatch('pointerdown',{clientX:10,clientY:20,pointerId:1});
  await canvas.dispatch('pointermove',{clientX:40,clientY:50});
  await canvas.dispatch('pointerup',{clientX:40,clientY:50});

  assert.equal(seen.pans,0,'测距时不得平移地图');
  assert.equal(seen.drafts.length,0,'测距时不得框选工作区');
  assert.equal(seen.completes,0);
  assert.deepEqual(seen.nodes,[],'测距时不得给当前工作区加节点');
  assert.ok(seen.positions>=1,'测距时仍应更新经纬度读数');
});

test('measure mode never triggers the double-click zoom',()=>{
  const {canvas,seen}=bindFake(()=>'measure');
  canvas.dispatch('dblclick',{clientX:40,clientY:50});
  assert.equal(seen.zooms,0);

  const pan=bindFake(()=>'pan');
  pan.canvas.dispatch('dblclick',{clientX:40,clientY:50});
  assert.equal(pan.seen.zooms,1,'pan 模式的双击 zoom 必须保持不变');
});

test('workspace, node and pan modes keep their existing behaviour',async()=>{
  const workspace=bindFake(()=>'workspace');
  await workspace.canvas.dispatch('pointerdown',{clientX:10,clientY:20,pointerId:1,lon:122,lat:30});
  assert.equal(workspace.seen.drafts.length,1);
  assert.equal(workspace.seen.pans,0);

  const node=bindFake(()=>'node');
  await node.canvas.dispatch('pointerdown',{clientX:10,clientY:20,pointerId:1});
  await node.canvas.dispatch('pointerup',{clientX:10,clientY:20,lon:122.5,lat:30.5});
  assert.deepEqual(node.seen.nodes,[[122.5,30.5]]);
  assert.equal(node.seen.pans,0);

  const pan=bindFake(()=>'pan');
  await pan.canvas.dispatch('pointerdown',{clientX:10,clientY:20,pointerId:1});
  assert.equal(pan.seen.pans,1);
});

// ============================================================================
// 4. 壳层装配（源码级锁定，main.js 是浏览器入口，无法在 node 里直接加载）
// ============================================================================

test('map toolbar exposes the measure tools and the shell wires them read-only',()=>{
  const html=readFile('cns_planner/web/index.html');
  assert.match(html,/id="measureTool"[^>]*aria-label="测距"/);
  assert.match(html,/id="measureClear"/);
  assert.match(html,/id="measureStatus"/);

  const main=readFile('cns_planner/web/js/main.js');
  // 入口文件必须保持精简（tests/test_architecture.py 限制 450 行）：装配在模块内，
  // main.js 只有一次接线、一次绘制与模式同步。
  assert.match(main,/import \{attachMeasureTool\} from '\.\/map\/measure_tool\.js'/);
  assert.match(main,/,measure=attachMeasureTool\(\{\$,canvas,paint,eventLonLat,getMode:\(\)=>interactionMode,setMode:value=>\{interactionMode=value;\}\}\);/);
  assert.match(main,/if\(view\)measure\.draw\(ctx,screenPoint\)/);
  assert.match(main,/measure\.sync\(\)/);
  assert.match(main,/measure\.hover\(point,event\)/);
  // 测距时命中网格也不得弹出详情：非 pan 模式一律提前返回。
  assert.match(main,/if\(interactionMode!=='pan'\)\{info\.hidden=true;return;\}/);
  assert.equal((main.match(/interactionMode==='measure'/g)||[]).length,0,'测距的交互细节不得回流到壳层');

  const measure=readFile('cns_planner/web/js/map/measure_tool.js');
  assert.match(measure,/export function attachMeasureTool/);
  assert.match(measure,/button\.onclick=\(\)=>enter\(getMode\(\)!=='measure'\)/);
  assert.match(measure,/if\(event\.detail>1\)return;/);
  assert.match(measure,/canvas\.addEventListener\('dblclick',\(\)=>finish\(\)\)/);
  assert.match(measure,/event\.key!=='Escape'\|\|getMode\(\)!=='measure'/);
  assert.doesNotMatch(measure,/fetch\(|XMLHttpRequest|\/api\//,'测距工具不得调用任何 API');
  assert.doesNotMatch(measure,/projectState|store\.set|resourceAction|mutate\(/,'测距工具不得写任何业务状态');
});

test('attachMeasureTool owns the toolbar button, the Escape key and the canvas clicks',()=>{
  const listeners=new Map();
  const canvas={addEventListener(type,handler){listeners.set(type,handler);},
    dispatch(type,event={}){const handler=listeners.get(type);return handler?handler(event):undefined;}};
  const button={onclick:null,attrs:{},active:false,
    classList:{toggle(name,on){button.active=Boolean(on);}},
    setAttribute(name,value){button.attrs[name]=value;}};
  const clear={onclick:null};
  const status={hidden:true,textContent:''};
  const elements={measureTool:button,measureClear:clear,measureStatus:status};
  const keydowns=[];
  let mode='pan',paints=0;
  const previousDocument=globalThis.document;
  globalThis.document={addEventListener:(type,handler)=>{if(type==='keydown')keydowns.push(handler);}};
  let tool;
  try{
    tool=attachMeasureTool({
      $:id=>elements[id]||null,canvas,paint:()=>{paints++;},
      eventLonLat:event=>[event.lon,event.lat],getMode:()=>mode,setMode:value=>{mode=value;},
    });
  }finally{globalThis.document=previousDocument;}

  assert.equal(status.hidden,true,'未进入测距时不得显示读数');
  button.onclick();
  assert.equal(mode,'measure');
  assert.equal(button.attrs['aria-pressed'],'true');
  assert.equal(status.hidden,false);
  assert.equal(status.textContent,MEASURE_HINT+' · 尚未落点');

  canvas.dispatch('click',{detail:1,lon:122,lat:30});
  canvas.dispatch('click',{detail:1,lon:122,lat:31});
  assert.equal(tool.state().points.length,2);
  assert.equal(status.textContent,MEASURE_HINT+' · 顶点 2 · 累计 111.20 km');

  // 双击的第二次 click（detail > 1）不再加点，dblclick 才是"完成"。
  canvas.dispatch('click',{detail:2,lon:123,lat:31});
  assert.equal(tool.state().points.length,2);
  canvas.dispatch('dblclick',{});
  assert.equal(tool.isFinished(),true);
  assert.match(status.textContent,/^测距完成 · 顶点 2 · 累计 111\.20 km · Esc 退出$/);

  // pan 模式下的 dblclick 不得改变测距状态（那是 interaction.js 的 zoom 行为）。
  mode='pan';
  canvas.dispatch('dblclick',{});

  // Esc 退出并丢弃全部点。
  button.onclick();
  canvas.dispatch('click',{detail:1,lon:122,lat:30});
  keydowns[0]({key:'Escape',preventDefault(){}});
  assert.equal(mode,'pan');
  assert.deepEqual(tool.state().points,[]);
  assert.equal(status.hidden,true);

  // 「清除」同样清空并退出。
  button.onclick();
  canvas.dispatch('click',{detail:1,lon:122,lat:30});
  assert.equal(tool.state().points.length,1);
  clear.onclick();
  assert.equal(mode,'pan');
  assert.deepEqual(tool.state().points,[]);
  assert.ok(paints>=5,'每次状态变化都必须请求重绘');
});
