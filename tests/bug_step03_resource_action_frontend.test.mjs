/**
 * BUG-STEP03-RESOURCEACTION-001 + BUG-WORKSPACE-CLEAR-002 的前端回归测试。
 *
 * 锁定两件事：
 *  1. 返回**局部对象**的 mutation 端点绝不允许把 response 当成完整 workflow 写进全局 flow
 *     （否则 flow.project / workspace / grid 立刻消失，页面抛 reading 'name'）；
 *  2. 清除工作区在执行前二次确认，并明确 nodes / scenario_routes / operational_routes 数量
 *     与将失效的下游。
 *
 * 独立运行：`node --test tests/bug_step03_resource_action_frontend.test.mjs`
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {createResourceMutationAndRefresh} from '../cns_planner/web/js/api/client.js';
import {
  confirmWorkspaceClear,workspaceClearConfirmationMessage,workspaceClearConsequences,
} from '../cns_planner/web/js/workflow/step02_workspace.js';

const read=relative=>readFileSync(new URL('../cns_planner/web/js/'+relative,import.meta.url),'utf8');

// ---- 1. 局部 response 绝不覆盖全局 flow -------------------------------------------

function fullWorkflow(){
  return {
    revision:7,project:{name:'舟山工程',project_id:'P-1'},
    workspace:{bbox:[122.0,29.9,122.02,29.92]},
    grid:{status:'passed',level:8,cells:[{grid_id:'G-1'}]},
    layered_route_validations:{status:'not_calculated',items:[],count:0},
  };
}

test('a local mutation response never replaces the global workflow',async()=>{
  const full=fullWorkflow();
  const applied=[];
  let flow=null;
  const localResponse={status:'validated_candidate',count:1,items:[{validation_id:'LRV-1'}]};
  const mutate=createResourceMutationAndRefresh({
    post:async(path,payload)=>{
      assert.equal(path,'/api/layered-route-validations/evaluate-real');
      assert.deepEqual(payload,{horizontal_crs:'EPSG:32651'});
      // 局部端点：响应体里没有 project / workspace / grid。
      assert.equal('project' in localResponse,false);
      return localResponse;
    },
    refresh:async()=>full,
    apply:async(data)=>{applied.push(data);flow=data;return data;},
  });

  const returned=await mutate('/api/layered-route-validations/evaluate-real',
    {horizontal_crs:'EPSG:32651'});

  assert.equal(returned,localResponse,'调用方仍然拿到 POST 的局部响应');
  assert.deepEqual(applied,[full],'全局 flow 只能由完整 workflow 快照写入');
  assert.equal(flow.project.name,'舟山工程');
  assert.deepEqual(flow.workspace.bbox,[122.0,29.9,122.02,29.92]);
  assert.equal(flow.grid.status,'passed');
  // 局部响应绝不会被当成 workflow 的 layered_route_validations。
  assert.equal(flow.layered_route_validations.status,'not_calculated');
});

test('the workflow renderer keeps working after a local mutation',async()=>{
  const full=fullWorkflow();
  let flow=full;
  // 与 main.js renderWorkflow 同构的最小渲染：读取 flow.project.name / flow.grid。
  const render=()=>({title:flow.project.name,grid:flow.grid.status,warning:flow.workspace.health});
  const mutate=createResourceMutationAndRefresh({
    post:async()=>({status:'passed',profile_id:'R3D-1'}),
    refresh:async()=>full,
    apply:async(data)=>{flow=data;return data;},
  });
  await mutate('/api/route-3d-profiles/evaluate',{route_id:'R-1'});
  assert.doesNotThrow(render,'局部 mutation 后页面不得抛 reading name');
  assert.equal(render().title,'舟山工程');
});

test('the helper refuses to be constructed without its three dependencies',()=>{
  assert.throws(()=>createResourceMutationAndRefresh({post:()=>{}}),/post \/ refresh \/ apply/);
});

// ---- 2. 端点契约：局部返回的调用必须走 helper，preview 必须只读 ---------------------

const LOCAL_MUTATION_ENDPOINTS=[
  ['workflow/layered_route_validation.js','/api/layered-route-validations/evaluate-real'],
  ['workflow/layered_operational_adoption.js','/api/layered-operational-adoptions/apply'],
  ['workflow/layered_operational_adoption.js','/api/layered-operational-adoptions/revoke'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3/policy'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3/fine-policy'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3/validation-policy'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3-operational-adoptions/apply'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3-operational-adoptions/revoke'],
  ['workflow/route3d_profile.js','/api/route-3d-profiles/evaluate'],
  ['workflow/route3d_profile.js','/api/route-3d-profiles/delete'],
];

const READ_ONLY_PREVIEW_ENDPOINTS=[
  ['workflow/layered_operational_adoption.js','/api/layered-operational-adoptions/preview'],
  ['workflow/step03_routes.js','/api/research/route-planner-v3-operational-adoptions/preview'],
];

test('every local-object endpoint is called through mutation + refresh',()=>{
  for(const [file,endpoint] of LOCAL_MUTATION_ENDPOINTS){
    const source=read(file);
    const quoted="'"+endpoint+"'";
    const index=source.indexOf(quoted);
    assert.notEqual(index,-1,`${file} 必须仍然调用 ${endpoint}`);
    const before=source.slice(Math.max(0,index-120),index);
    assert.match(before,/resourceMutationAndRefresh/,
      `${endpoint} 必须通过 resourceMutationAndRefresh 调用`);
    assert.doesNotMatch(before,/resourceAction\(/,
      `${endpoint} 返回局部对象，不得使用 resourceAction`);
  }
});

test('read-only preview endpoints use computeAction and never touch the global flow',()=>{
  for(const [file,endpoint] of READ_ONLY_PREVIEW_ENDPOINTS){
    const source=read(file);
    const index=source.indexOf("'"+endpoint+"'");
    assert.notEqual(index,-1,`${file} 必须仍然调用 ${endpoint}`);
    const before=source.slice(Math.max(0,index-120),index);
    assert.match(before,/computeAction/,`${endpoint} 只读预览必须用 computeAction`);
    assert.doesNotMatch(before,/resourceAction\(/,
      `${endpoint} 不得用 resourceAction 覆盖全局 flow`);
  }
});

test('main.js wires the helper to the real api client and exposes it to the panels',()=>{
  const main=read('main.js');
  assert.match(main,/createResourceMutationAndRefresh\(\{/);
  assert.match(main,/refresh:\(\)=>api\('\/api\/workflow'\)/);
  assert.match(main,/apply:applyWorkflow/);
  assert.match(main,/resourceAction,resourceMutationAndRefresh,computeAction/,
    '步骤模块必须能拿到 helper');
  const helper=main.slice(main.indexOf('const resourceMutationAndRefresh='),
    main.indexOf('async function resourceAction('));
  assert.notEqual(helper,'');
  assert.doesNotMatch(helper,/flow=/,'helper 绝不能把局部 response 写进全局 flow');
});

test('B7 advanced actions use compatibility and research namespaces only',()=>{
  const step03=read('workflow/step03_routes.js');
  const step04=read('workflow/step04_operation.js');
  const step05=read('workflow/step05_cns.js');
  assert.match(step03,/\/api\/compatibility\/route-planner\/evaluate/);
  assert.match(step03,/\/api\/compatibility\/selection/);
  assert.match(step03,/\/api\/research\/route-planner-v3/);
  assert.doesNotMatch(step03,/c\.mutate\('operational'/);
  assert.doesNotMatch(step03,/api\/algorithms\/select/,
    '归档规划器参数不得再走已关闭的 algorithm_selection 写入通道');
  assert.match(step04,/\/api\/research\/v3-cns-assessment\/evaluate/);
  assert.doesNotMatch(step04,/c\.mutate\('operational'/);
  for(const endpoint of [
    '/api/compatibility/coverage/evaluate',
    '/api/compatibility/cns-gap-analysis-v1/evaluate',
    '/api/compatibility/cns-gap-analysis-v2/evaluate',
    '/api/compatibility/site-plan/evaluate',
  ])assert.ok(step05.includes(endpoint),`Step5 Advanced must call ${endpoint}`);
});

// ---- 3. 清工作区：执行前二次确认 + 明确的破坏性说明 --------------------------------

const CLEAR_FLOW={
  nodes:[{node_id:'N1'},{node_id:'N2'}],
  scenario_routes:[{route_id:'R1'},{route_id:'R2'},{route_id:'R3'}],
  operational_routes:[{route_id:'R1'}],
  layered_route_candidates:{items:[{candidate_id:'C1'}]},
  route_risk_profiles:{items:[{profile_id:'P1'},{profile_id:'P2'}]},
  layered_route_validations:{items:[{validation_id:'V1'}]},
  layered_operational_adoptions:{items:[{adoption_id:'A1'}]},
};

test('workspace clear counts what will be destroyed without inventing anything',()=>{
  assert.deepEqual(workspaceClearConsequences(CLEAR_FLOW),{
    nodes:2,scenarioRoutes:3,operationalRoutes:1,
    layeredCandidates:1,routeRiskProfiles:2,layeredValidations:1,layeredAdoptions:1,
  });
  // 空项目：全部为 0，不伪造成 1 或省略。
  assert.deepEqual(workspaceClearConsequences({}),{
    nodes:0,scenarioRoutes:0,operationalRoutes:0,
    layeredCandidates:0,routeRiskProfiles:0,layeredValidations:0,layeredAdoptions:0,
  });
});

test('the confirmation message names the counts and the downstream that goes stale',()=>{
  const message=workspaceClearConfirmationMessage(CLEAR_FLOW);
  for(const expected of ['项目节点 2 个','场景航路 3 条','运行航路 1 条',
    'layered 候选 1 条','RouteRiskProfile 2 条','layered validations 1 条',
    'operational adoptions 1 条','stale','未配置','不可撤销']){
    assert.match(message,new RegExp(expected.replace(/[.*+?^${}()|[\]\\/]/g,'\\$&')),
      `确认文案必须包含 ${expected}`);
  }
});

test('cancelling the confirmation stops the destructive clear',()=>{
  let asked=null;
  const cancelled=confirmWorkspaceClear(CLEAR_FLOW,text=>{asked=text;return false;});
  assert.equal(cancelled,false);
  assert.equal(asked,workspaceClearConfirmationMessage(CLEAR_FLOW));
  assert.equal(confirmWorkspaceClear(CLEAR_FLOW,()=>true),true);
  assert.equal(confirmWorkspaceClear(CLEAR_FLOW,()=>undefined),false,
    '确认通道没有明确返回 true 时不得执行清除');
});

test('the workspace panel states the destructive consequence and main.js asks first',()=>{
  const panel=read('workflow/step02_workspace.js');
  assert.match(panel,/data-clear-workspace-warning/);
  assert.match(panel,/破坏性/);
  assert.match(panel,/confirmWorkspaceClear/);
  const main=read('main.js');
  assert.match(main,/if\(!Step02\.confirmWorkspaceClear\(flow\)\)return;/,
    '清除工作区必须先经过二次确认');
});
