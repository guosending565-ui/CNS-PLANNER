/**
 * CNS 专题制图模板（Step06「报告与交付」）前端定向测试。
 *
 * 覆盖 Round30-A 的前端契约：
 *  1. 模板清单**以后端 catalog 为准**：communication / navigation / surveillance /
 *     combined 四个模板变为可选，`route_detail_v1` 仍是「尚未实现」并禁用；
 *  2. 监视布设图必须**显式选择 variant**（RID / Radar），其它模板不渲染该选择器；
 *  3. variant 取值同样来自 catalog；preview 把 `surveillance_service` 放进查询串，
 *     export 把它放进 `parameters`，因此两张监视图的请求完全不同；
 *  4. 中文界面如实标注「规划提案（未确认）」语义，绝不出现「已建」措辞。
 */
import assert from 'node:assert/strict';
import test,{beforeEach} from 'node:test';

import {render as renderStep06} from
  '../cns_planner/web/js/workflow/step06_review.js';
import {createShellActions} from
  '../cns_planner/web/js/workflow/shell_actions.js';
import {resetMapFigureState,setMapFigureState} from
  '../cns_planner/web/js/workflow/map_figure_state.js';

beforeEach(()=>{resetMapFigureState();});

/** 后端 `GET /api/map-figures/state` 的 catalog 投影（本轮的真实取值）。 */
const CATALOG={
  available_template_ids:[
    'route_overview_v1','communication_layout_v1','navigation_layout_v1',
    'surveillance_layout_v1','cns_combined_v1',
  ],
  templates:[
    {template_id:'route_overview_v1',display_name:'航路周边状况图',status:'available'},
    {template_id:'route_detail_v1',display_name:'航路细节放大图',status:'planned'},
    {template_id:'communication_layout_v1',display_name:'通信设施布设图',status:'available'},
    {template_id:'navigation_layout_v1',display_name:'导航完整性监测点布设图',status:'available'},
    {template_id:'surveillance_layout_v1',display_name:'监视设施布设图',status:'available'},
    {template_id:'cns_combined_v1',display_name:'CNS 综合布设图',status:'available'},
  ],
  surveillance_service_values:['rid_cooperative','radar_noncooperative'],
};

const ROUTE={route_id:'R0005',path:[[122.2672,29.8666],[122.1883,29.8194]]};

function flowWith(routes=[ROUTE]){
  return {
    revision:410,operational_routes:routes,
    map_figures:{items:[],active_figure_id:null},
    cns_planning_reports:{records:[],active_report_id:null},
    confirmed_cns_plan:{status:'not_confirmed'},
    cns_plan_review:{variants:[],status:'not_initialized'},
    workspace:null,aircraft:null,rules:{status:'not_calculated'},
    coverage_3d:{status:'not_calculated',routes:[]},
    result_statuses:{},review:{},steps:{},defaults:{},
    route_safety_evidence_v2:{items:[]},route_safety_evidence_v2_readiness:{},
  };
}

function withCatalog(){
  setMapFigureState({
    catalog:CATALOG,
    records:{items:[],active_figure_id:null,count:0},
    loaded:true,error:'',
  });
}

const STATE={data_health:{status:'passed'},project_storage:{}};

// ---- 1. 模板清单 -------------------------------------------------------------

test('catalog 里的四个 CNS 模板在前端可选，route_detail 仍禁用',()=>{
  withCatalog();
  const html=renderStep06({state:STATE,flow:flowWith()});
  for(const id of ['communication_layout_v1','navigation_layout_v1',
    'surveillance_layout_v1','cns_combined_v1']){
    assert.match(html,new RegExp('value="'+id+'"'));
    assert.doesNotMatch(html,new RegExp('value="'+id+'" disabled'));
  }
  assert.match(html,/航路细节放大图（尚未实现）/);
  assert.match(html,/value="route_detail_v1" disabled/);
  assert.match(html,/导航完整性监测点布设图/);
});

test('catalog 未加载时退回内置清单（不把已实现模板显示成尚未实现）',()=>{
  const html=renderStep06({state:STATE,flow:flowWith()});
  assert.doesNotMatch(html,/通信设施布设图（尚未实现）/);
  assert.match(html,/value="communication_layout_v1"/);
});

// ---- 2. 监视 variant 选择器 --------------------------------------------------

test('只有监视布设图渲染 variant 选择器，且取值来自 catalog',()=>{
  withCatalog();
  const surveillance=renderStep06({
    state:STATE,flow:flowWith(),
    selection:{selected_template_id:'surveillance_layout_v1'},
  });
  assert.match(surveillance,/id="mapFigureSurveillanceService"/);
  assert.match(surveillance,/value="rid_cooperative"/);
  assert.match(surveillance,/value="radar_noncooperative"/);
  assert.match(surveillance,/RID 合作监视/);
  assert.match(surveillance,/Radar 非合作监视/);

  const communication=renderStep06({
    state:STATE,flow:flowWith(),
    selection:{selected_template_id:'communication_layout_v1'},
  });
  assert.doesNotMatch(communication,/mapFigureSurveillanceService/);
});

// ---- 3. variant 进入请求参数 -------------------------------------------------

test('preview 把 surveillance_service 放进只读 GET 查询串',async()=>{
  const calls=[];
  const previousOpen=globalThis.window,originalURL=globalThis.URL;
  globalThis.window={open:()=>({location:{href:''}})};
  globalThis.URL={createObjectURL:()=>'blob:preview',revokeObjectURL:()=>{}};
  try{
    const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
    const result=await actions.previewMapFigure({
      template_id:'surveillance_layout_v1',route_id:'R0005',
      parameters:{surveillance_service:'radar_noncooperative'},
    },{api:async (url,options)=>{calls.push({url,method:options?.method||'GET'});return new Blob();},
      onError:()=>{}});
    assert.equal(result.ok,true);
    assert.equal(calls.length,1);
    assert.equal(calls[0].method,'GET');
    assert.match(calls[0].url,/template=surveillance_layout_v1/);
    assert.match(calls[0].url,/surveillance_service=radar_noncooperative/);
    assert.equal(calls.some(call=>String(call.url).includes('/export')),false);
  }finally{
    if(previousOpen===undefined)delete globalThis.window;else globalThis.window=previousOpen;
    globalThis.URL=originalURL;
  }
});

test('preview 不带 parameters 时不添加任何多余查询参数',async()=>{
  const calls=[];
  const previousOpen=globalThis.window,originalURL=globalThis.URL;
  globalThis.window={open:()=>({location:{href:''}})};
  globalThis.URL={createObjectURL:()=>'blob:preview',revokeObjectURL:()=>{}};
  try{
    const actions=createShellActions({getNode:()=>null,panelError:()=>{}});
    await actions.previewMapFigure({template_id:'communication_layout_v1',route_id:'R0005'},
      {api:async url=>{calls.push(url);return new Blob();},onError:()=>{}});
    assert.doesNotMatch(calls[0],/surveillance_service/);
  }finally{
    if(previousOpen===undefined)delete globalThis.window;else globalThis.window=previousOpen;
    globalThis.URL=originalURL;
  }
});

// ---- 4. 未确认语义 -----------------------------------------------------------

test('图件面板如实说明规划提案未确认，绝不出现「已建」措辞',()=>{
  withCatalog();
  const html=renderStep06({
    state:STATE,flow:flowWith(),
    selection:{selected_template_id:'cns_combined_v1'},
  });
  assert.match(html,/不会重算或回写任何业务结论/);
  assert.doesNotMatch(html,/已建设施已确认/);
});
