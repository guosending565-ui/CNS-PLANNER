/**
 * 专题成果图（Step06「报告与交付」）前端定向测试。
 *
 * 覆盖：
 *  1. Step06 渲染出「专题成果图」入口（模板 / 航路下拉 + 预览 / 生成 PNG / 下载）；
 *  2. 没有可制图航路时入口**禁用**并给出中文原因，且不会自动触发生成；
 *  3. 图件记录状态、revision、未显示图层原因如实显示；
 *  4. previewMapFigure 是显式动作：缺少航路时不发请求；有航路时只发一次 GET 预览；
 *  5. downloadMapFigure 只读已生成产物，没有图件时给出中文提示而不是静默失败。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {mapFiguresModel,render as renderStep06} from
  '../cns_planner/web/js/workflow/step06_review.js';
import {createShellActions} from
  '../cns_planner/web/js/workflow/shell_actions.js';

function flowWith(routes=[],records=[],activeId=null){
  return {
    revision:9,
    operational_routes:routes,
    map_figures:{items:records,active_figure_id:activeId},
    cns_planning_reports:{records:[],active_report_id:null},
    confirmed_cns_plan:{status:'not_confirmed'},
    cns_plan_review:{variants:[],status:'not_initialized'},
    // 面板渲染所需的既有只读字段（本测试只验证专题图段落）。
    workspace:null,aircraft:null,rules:{status:'not_calculated'},
    coverage_3d:{status:'not_calculated',routes:[]},
    result_statuses:{},review:{},steps:{},defaults:{},
    route_safety_evidence_v2:{items:[]},route_safety_evidence_v2_readiness:{},
  };
}

const ROUTE_A={route_id:'R0001',path:[[122.05,29.95],[122.2,30.0]]};
const ROUTE_B={route_id:'R0002',path:[[122.0,29.9]]};
// 面板渲染所需的既有只读服务器状态（本测试只验证专题图段落）。
const STATE={data_health:{status:'passed'},project_storage:{}};
const RECORD={
  figure_id:'MF-'+'a'.repeat(32),template_id:'route_overview_v1',title:'航路周边总览图（R0001）',
  route_id:'R0001',format:'png',dpi:300,project_revision:9,generated_at:'2026-09-29T00:00:00Z',
  image_bytes:984766,current_applicability:'current',
  omitted_layers:[{layer_key:'airport',display_name:'机场',reason:'本机未配置该数据源'}],
};

// ---- 1. 入口渲染 ------------------------------------------------------------

test('Step06 报告与交付段落包含专题成果图入口与两个主按钮',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_A])});
  assert.match(html,/专题成果图/);
  assert.match(html,/id="mapFigureTemplate"/);
  assert.match(html,/id="mapFigureRoute"/);
  assert.match(html,/id="previewMapFigure"/);
  assert.match(html,/id="exportMapFigure"/);
  assert.match(html,/航路周边状况图/);
});

test('planned 模板在下拉里被禁用并标注尚未实现',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_A])});
  assert.match(html,/航路细节放大图（尚未实现）/);
  assert.match(html,/value="route_detail_v1" disabled/);
});

test('没有可制图航路时按钮禁用并给出中文原因',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([])});
  assert.match(html,/id="previewMapFigure" disabled/);
  assert.match(html,/id="exportMapFigure" disabled/);
  assert.match(html,/还没有权威运行航路/);
});

test('几何顶点不足的航路被标注且不计入可制图数量',()=>{
  const model=mapFiguresModel(flowWith([ROUTE_B]));
  assert.equal(model.routeCount,1);
  assert.equal(model.plottableCount,0);
  assert.equal(model.canGenerate,false);
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_B])});
  assert.match(html,/几何顶点不足/);
});

// ---- 2. 记录状态与缺数据说明 ------------------------------------------------

test('已生成图件显示模板 / 航路 / 状态 / revision / 格式与 DPI',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_A],[RECORD],RECORD.figure_id)});
  assert.match(html,/航路周边总览图（R0001）/);
  assert.match(html,/PNG/);
  assert.match(html,/生成时间/);
  assert.match(html,/图件 revision/);
});

test('缺数据图层用简短中文写明未显示原因',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_A],[RECORD],RECORD.figure_id)});
  assert.match(html,/本图未显示 机场：本机未配置该数据源/);
});

test('没有图件时下载按钮禁用',()=>{
  const html=renderStep06({state:STATE,flow:flowWith([ROUTE_A])});
  assert.match(html,/id="downloadMapFigure" disabled/);
  assert.match(html,/id="downloadMapFigureSpec" disabled/);
});

// ---- 3. previewMapFigure：显式动作、只读、不自动触发 -------------------------

test('previewMapFigure 缺少航路时不发任何请求',async()=>{
  const calls=[];
  const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
  const result=await actions.previewMapFigure({template_id:'route_overview_v1',route_id:''},
    {api:async url=>{calls.push(url);return new Blob();},onError:()=>{}});
  assert.equal(result.ok,false);
  assert.equal(calls.length,0);
  assert.match(result.message,/请先选择一条权威运行航路/);
});

test('previewMapFigure 只发起一次只读 GET，且不会触发生成',async()=>{
  const calls=[];
  const previousOpen=globalThis.window;
  globalThis.window={open:()=>({location:{href:''}})};
  const originalURL=globalThis.URL;
  globalThis.URL={createObjectURL:()=>'blob:preview',revokeObjectURL:()=>{}};
  try{
    const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
    const result=await actions.previewMapFigure({template_id:'route_overview_v1',route_id:'R0001'},
      {api:async (url,options)=>{calls.push({url,method:options?.method||'GET'});return new Blob();},onError:()=>{}});
    assert.equal(result.ok,true);
    assert.equal(calls.length,1);
    assert.equal(calls[0].method,'GET');
    assert.match(calls[0].url,/^\/api\/map-figures\/preview\?/);
    assert.match(calls[0].url,/template=route_overview_v1/);
    assert.match(calls[0].url,/route_id=R0001/);
    assert.equal(calls.some(call=>String(call.url).includes('/export')),false);
  }finally{
    if(previousOpen===undefined)delete globalThis.window;else globalThis.window=previousOpen;
    globalThis.URL=originalURL;
  }
});

// ---- 4. downloadMapFigure：只读已有产物 -------------------------------------

test('downloadMapFigure 没有图件时给出中文提示而不是静默失败',async()=>{
  const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
  await assert.rejects(
    ()=>actions.downloadMapFigure(false,{api:async()=>new Blob(),flow:flowWith([ROUTE_A]),onMissing:()=>{}}),
    /尚无.*专题成果图/);
});

test('downloadMapFigure 只请求 artifact 端点且按图号取名',async()=>{
  const calls=[];
  const previousDocument=globalThis.document;
  globalThis.document={createElement:()=>({click(){},set href(value){this._href=value;},get href(){return this._href;}})};
  const originalURL=globalThis.URL;
  globalThis.URL={createObjectURL:()=>'blob:artifact',revokeObjectURL:()=>{}};
  try{
    const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
    const result=await actions.downloadMapFigure(false,{
      api:async url=>{calls.push(url);return new Blob();},
      flow:flowWith([ROUTE_A],[RECORD],RECORD.figure_id),onMissing:()=>{},
    });
    assert.equal(result.ok,true);
    assert.equal(result.figureId,RECORD.figure_id);
    assert.equal(calls.length,1);
    assert.match(calls[0],/^\/api\/map-figures\/artifact\?figure_id=MF-/);
  }finally{
    globalThis.document=previousDocument;
    globalThis.URL=originalURL;
  }
});
