/**
 * 专题成果图 **前端状态集成测试**（Step06 ↔ GET /api/map-figures/state）。
 *
 * 这是本轮 code review 修复项的端到端证据链，全部在真实前端模块上执行
 * （不 mock 业务函数，只替换 DOM / fetch 边界）：
 *
 *   初始无图
 *     → 触发「生成 PNG」（POST /api/map-figures/export 走 computeAction）
 *     → refresh state（GET /api/map-figures/state）
 *     → active record 出现且 Step06 立刻显示当前图件
 *     → 下载 PNG 使用**新** figure_id（kind=png）
 *     → 下载 Spec 使用**同一个** figure_id（kind=spec）
 *
 * 同时验证两条硬契约：
 *
 *  1. **export response 不被安装成 workflow**：flow 对象在整个导出前后逐字节不变
 *     （旧实现走 resourceAction，会把局部响应当 workflow 快照安装，从而污染 flow）；
 *  2. **Step06 不再依赖 flow.map_figures**：flow 里即使存在旧容器也不被消费。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {bind,render as renderStep06} from
  '../cns_planner/web/js/workflow/step06_review.js';
import {createShellActions} from
  '../cns_planner/web/js/workflow/shell_actions.js';
import {resetMapFigureState} from
  '../cns_planner/web/js/workflow/map_figure_state.js';

const ROUTE={route_id:'R0007',path:[[122.10,30.01],[122.24,30.02]]};
const FIGURE_ID='MF-'+'c'.repeat(32);
const STATE={data_health:{status:'passed'},project_storage:{}};

/** 极简 DOM：只实现本链用到的 getElementById / createElement / querySelectorAll。 */
function installDom(){
  const nodes=new Map();
  const ensure=id=>{
    if(!nodes.has(id))nodes.set(id,{id,onclick:null,disabled:false,value:'',style:{},dataset:{}});
    return nodes.get(id);
  };
  const region={
    id:'mapFigureRegion',
    _html:'',
    get innerHTML(){return this._html;},
    set innerHTML(value){
      this._html=String(value);
      for(const match of this._html.matchAll(/<button[^>]*id="([^"]+)"[^>]*>/g)){
        const node=ensure(match[1]);
        node.disabled=/\sdisabled(\s|>)/.test(match[0]);
      }
      for(const match of this._html.matchAll(/<(?:select|div|input)[^>]*id="([^"]+)"[^>]*>/g)){
        ensure(match[1]);
      }
      // 模拟浏览器 select：优先 selected，否则取第一条未 disabled 的 option。
      for(const select of this._html.matchAll(
        /<select[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)){
        const options=[...select[2].matchAll(/<option value="([^"]*)"([^>]*)>/g)];
        const chosen=options.find(option=>/\sselected(?:\s|$)/.test(option[2]))
          ||options.find(option=>!/\sdisabled(?:\s|$)/.test(option[2]));
        ensure(select[1]).value=chosen?.[1]||'';
      }
    },
  };
  nodes.set('mapFigureRegion',region);
  const previousDocument=globalThis.document;
  const previousURL=globalThis.URL;
  const previousWindow=globalThis.window;
  globalThis.document={
    getElementById:id=>nodes.get(id)||null,
    createElement:()=>({click(){},style:{},set href(value){this._href=value;},get href(){return this._href;}}),
    querySelectorAll:()=>[],
    body:{contains:()=>false},
  };
  globalThis.URL={createObjectURL:()=>'blob:artifact',revokeObjectURL:()=>{}};
  globalThis.window={open:()=>({location:{href:''}})};
  return {
    nodes,region,
    restore(){
      globalThis.document=previousDocument;
      globalThis.URL=previousURL;
      if(previousWindow===undefined)delete globalThis.window;else globalThis.window=previousWindow;
    },
  };
}

/** 假服务端：图件索引在**项目目录外部**，只有 GET /api/map-figures/state 能读到。 */
function fakeServer(){
  const server={
    items:[],active:null,exportCalls:0,stateCalls:0,artifactCalls:[],exportPayloads:[],
  };
  const api=async url=>{
    const text=String(url);
    if(text==='/api/map-figures/state'){
      server.stateCalls+=1;
      return {
        catalog:{available_template_ids:['route_overview_v1'],templates:[]},
        records:{items:server.items.map(item=>({...item})),active_figure_id:server.active,count:server.items.length,project_revision:9},
      };
    }
    if(text.startsWith('/api/map-figures/artifact?')){
      server.artifactCalls.push(text);
      return new Blob();
    }
    throw Error('未预期的 GET：'+text);
  };
  // computeAction：原样返回响应，**不安装**成 workflow（与 main.js 的真实实现一致）。
  const computeAction=async (path,payload)=>{
    assert.equal(path,'/api/map-figures/export');
    server.exportCalls+=1;
    server.exportPayloads.push({...payload});
    const record={
      figure_id:FIGURE_ID,template_id:payload.template_id,title:'航路周边状况图',
      route_id:payload.route_id,format:'png',dpi:payload.dpi,project_revision:9,
      generated_at:'2026-09-30T00:00:00Z',image_bytes:1024,current_applicability:'current',
      omitted_layers:[],relative_path:'artifacts/map_figures/routes/R0007/route_overview_v1/'
        +FIGURE_ID+'/figure.png',
    };
    server.items=[record];
    server.active=FIGURE_ID;
    return {figure_id:FIGURE_ID,record,spec:{},reused:false,image_bytes:1024};
  };
  return {server,api,computeAction};
}

function flowFixture(legacyRecords=[],legacyActive=null,routes=[ROUTE]){
  return {
    revision:9,
    operational_routes:routes,
    map_figures:{items:legacyRecords,active_figure_id:legacyActive},
    cns_planning_reports:{records:[],active_report_id:null},
    confirmed_cns_plan:{status:'not_confirmed'},
    cns_plan_review:{variants:[],status:'not_initialized'},
    workspace:null,aircraft:null,rules:{status:'not_calculated'},
    coverage_3d:{status:'not_calculated',routes:[]},
    result_statuses:{},review:{},steps:{},defaults:{},
    route_safety_evidence_v2:{items:[]},route_safety_evidence_v2_readiness:{},
  };
}

test('初始无图 → export → refresh state → 当前图件出现 → PNG/Spec 用同一个新 figure_id',async()=>{
  const dom=installDom();
  try{
    resetMapFigureState();
    const {server,api,computeAction}=fakeServer();
    const flow=flowFixture();
    const flowBefore=JSON.stringify(flow);
    const shellActions=createShellActions({getNode:()=>null,panelError:()=>{}});
    const notices=[];
    const context={
      $:id=>dom.nodes.get(id)||null,
      api,computeAction,
      flow:()=>flow,
      panelError:(message,tone)=>notices.push({message,tone}),
      actionButton:(id,handler)=>{const node=dom.nodes.get(id);if(node)node.onclick=handler;},
      previewMapFigure:async()=>({ok:true}),
      downloadMapFigure:specOnly=>shellActions.downloadMapFigure(specOnly,{api,onMissing:()=>{}}),
    };

    // 模拟真实挂载顺序：渲染面板 → 挂到 DOM → 绑定。
    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    // 首次进入必须读取一次真实图件状态。
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(server.stateCalls,1,'首次进入应读取一次 map-figures/state');
    assert.match(dom.region.innerHTML,/尚未生成/);
    assert.equal(dom.nodes.get('downloadMapFigure').disabled,true);
    assert.equal(dom.nodes.get('exportMapFigure').disabled,false);

    // 触发「生成 PNG」。
    await dom.nodes.get('exportMapFigure').onclick();
    assert.equal(server.exportCalls,1);
    assert.equal(server.stateCalls,2,'导出成功后必须重新读取图件状态');

    // 局部刷新后的区域：当前图件立即出现，下载按钮解禁。
    const after=dom.region.innerHTML;
    assert.match(after,/当前图件/);
    assert.match(after,/artifacts\/map_figures\/routes/);
    assert.doesNotMatch(after,/尚未生成/);
    assert.equal(dom.nodes.get('downloadMapFigure').disabled,false);
    assert.equal(dom.nodes.get('downloadMapFigureSpec').disabled,false);
    assert.equal(notices.at(-1).tone,'success');

    // **workflow flow 没有被 export response 覆盖**。
    assert.equal(JSON.stringify(flow),flowBefore,'export 不得修改 workflow flow');
    assert.equal(flow.map_figures.items.length,0,'图件索引不得写回 flow.map_figures');

    // 下载 PNG：使用新的 active figure_id。
    await dom.nodes.get('downloadMapFigure').onclick();
    assert.equal(server.artifactCalls.length,1);
    assert.match(server.artifactCalls[0],new RegExp('figure_id='+FIGURE_ID));
    assert.match(server.artifactCalls[0],/kind=png/);

    // 下载 Spec：同一个 figure_id + kind=spec。
    await dom.nodes.get('downloadMapFigureSpec').onclick();
    assert.equal(server.artifactCalls.length,2);
    assert.match(server.artifactCalls[1],new RegExp('figure_id='+FIGURE_ID));
    assert.match(server.artifactCalls[1],/kind=spec/);
  }finally{
    dom.restore();
  }
});

test('flow.map_figures 里的历史记录不会污染面板，也不会被当作可下载图件',async()=>{
  const dom=installDom();
  try{
    resetMapFigureState();
    const {api,computeAction}=fakeServer();
    const legacy={figure_id:'MF-'+'d'.repeat(32),template_id:'route_overview_v1',
      route_id:'R0007',project_revision:9,current_applicability:'current'};
    const flow=flowFixture([legacy],legacy.figure_id);
    const shellActions=createShellActions({getNode:()=>null,panelError:()=>{}});
    const context={
      $:id=>dom.nodes.get(id)||null,api,computeAction,flow:()=>flow,
      panelError:()=>{},actionButton:(id,handler)=>{const node=dom.nodes.get(id);if(node)node.onclick=handler;},
      previewMapFigure:async()=>({ok:true}),
      downloadMapFigure:specOnly=>shellActions.downloadMapFigure(specOnly,{api,onMissing:()=>{}}),
    };
    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    await new Promise(resolve=>setImmediate(resolve));
    // 服务端索引为空 → 面板必须显示"尚未生成"，且下载被禁用（旧容器不作数）。
    assert.match(dom.region.innerHTML,/尚未生成/);
    assert.equal(dom.nodes.get('downloadMapFigure').disabled,true);
    await assert.rejects(
      ()=>dom.nodes.get('downloadMapFigure').onclick(),
      /尚无.*专题成果图/);
  }finally{
    dom.restore();
  }
});

test('项目 A → B → A：按 authoritative identity 清空并重新读取各自图件索引',async()=>{
  const dom=installDom();
  try{
    resetMapFigureState();
    const route={route_id:'R-A',path:[[122.0,30.0],[122.2,30.0]]};
    const a1={
      figure_id:'MF-'+'1'.repeat(32),template_id:'route_overview_v1',title:'A1',
      route_id:'R-A',format:'png',dpi:300,project_revision:1,
      generated_at:'2026-09-30T00:00:00Z',image_bytes:900,
      current_applicability:'current',omitted_layers:[],
    };
    const projects={
      A:{items:[a1],active:a1.figure_id},
      B:{items:[],active:null},
    };
    let project='A';
    let flow=flowFixture([],null,[route]);
    const calls=[];
    const api=async url=>{
      assert.equal(String(url),'/api/map-figures/state');
      calls.push(project);
      const current=projects[project];
      return {catalog:{available_template_ids:['route_overview_v1']},records:{
        items:current.items.map(item=>({...item})),active_figure_id:current.active,
        count:current.items.length,project_revision:1,
      }};
    };
    const context={
      $:id=>dom.nodes.get(id)||null,api,flow:()=>flow,
      projectOpenStep:()=>({identity:'project:'+project}),
      computeAction:async()=>{throw Error('本测试不应导出');},panelError:()=>{},
      actionButton:(id,handler)=>{const node=dom.nodes.get(id);if(node)node.onclick=handler;},
      previewMapFigure:async()=>({ok:true}),downloadMapFigure:async()=>({ok:true}),
    };

    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(dom.region.innerHTML,/A1/);

    project='B';
    flow=flowFixture([],null,[route]);
    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    assert.doesNotMatch(dom.region.innerHTML,/A1/,'发起 B 的 GET 前就必须擦掉 A1');
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(dom.region.innerHTML,/尚未生成任何图件/);

    project='A';
    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(dom.region.innerHTML,/A1/);
    assert.deepEqual(calls,['A','B','A']);
  }finally{
    dom.restore();
  }
});

test('R1 → R2：export 局部刷新后保持 R2，后续 preview/export 仍发送 R2',async()=>{
  const dom=installDom();
  try{
    resetMapFigureState();
    const r1={route_id:'R1',path:[[122.0,30.0],[122.1,30.0]]};
    const r2={route_id:'R2',path:[[122.2,30.0],[122.3,30.0]]};
    const flow=flowFixture([],null,[r1,r2]);
    const {server,api,computeAction}=fakeServer();
    const previews=[];
    const context={
      $:id=>dom.nodes.get(id)||null,api,computeAction,flow:()=>flow,
      projectOpenStep:()=>({identity:'project:routes'}),panelError:()=>{},
      actionButton:(id,handler)=>{const node=dom.nodes.get(id);if(node)node.onclick=handler;},
      previewMapFigure:async input=>{previews.push({...input});return {ok:true};},
      downloadMapFigure:async()=>({ok:true}),
    };

    dom.region.innerHTML=renderStep06({state:STATE,flow});
    bind(context);
    await new Promise(resolve=>setImmediate(resolve));
    dom.nodes.get('mapFigureRoute').value='R2';

    await dom.nodes.get('exportMapFigure').onclick();
    assert.equal(server.exportPayloads[0].route_id,'R2');
    assert.equal(dom.nodes.get('mapFigureRoute').value,'R2');

    await dom.nodes.get('previewMapFigure').onclick();
    assert.equal(previews.at(-1).route_id,'R2');
    await dom.nodes.get('exportMapFigure').onclick();
    assert.equal(server.exportPayloads.at(-1).route_id,'R2');
    assert.equal(dom.nodes.get('mapFigureRoute').value,'R2');
  }finally{
    dom.restore();
  }
});
