/**
 * BUG-SHELTER-UI-001：`shelter_coefficient_policy.confirmed === true` 时，Step03 Theta* V2
 * 面板的 checkbox 必须是 checked；`false` 时必须 unchecked；source / default_coefficient
 * 必须原样回填；保存接口返回新 snapshot 后重新渲染仍然 checked。
 *
 * 说明：本测试**先固定契约**，再据此判断能否复现"后端 confirmed=true 但 UI 未 checked"。
 * 若无法复现，则只保留测试，不改业务代码，也绝不硬写 checked=true。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  bindLayeredThetaV2,layeredThetaV2Model,renderLayeredThetaV2Panel,
  thetaV2ShelterPolicyPayload,
} from '../cns_planner/web/js/workflow/layered_theta_v2.js';

const INPUT='id="thetaV2ShelterConfirmed"';
const SOURCE_INPUT='id="thetaV2ShelterSource"';
const COEFFICIENT_INPUT='id="thetaV2ShelterCoefficient"';

/** 只含 Theta* V2 shelter 投影所必需字段的最小 flow。 */
function flowWithShelfPolicy(policy,overrides={}){
  return {
    layered_route_planner_readiness:{
      status:'blocked',
      algorithm:{algorithm_id:'layered_risk_aware_theta_star_v2',algorithm_version:'2.0'},
      theta_star_v2:{status:'not_ready',blockers:[]},
    },
    shelter_coefficient_policy:policy,
    population_shelter:{status:'not_calculated',cells:{}},
    theta_v2_objective_policy:{
      risk_weight:0.8,turn_weight:0.1,distance_weight:0.1,confirmed:true,
    },
    max_route_risk_density:{threshold:1,confirmed:true,temporary:true},
    regulatory_constraints:{status:'not_configured'},
    communication_planning_field:{status:'not_configured'},
    layered_route_candidates:{status:'not_calculated',items:[]},
    ...overrides,
  };
}

function confirmedPolicy(overrides={}){
  return {
    schema_version:'population-shelter-v1',status:'confirmed',status_reason:null,
    default_coefficient:0.75,per_grid_overrides:{'MHT4063-L08-C00000001-M00000001':0.25},
    unit:'dimensionless_shelter_coefficient_0_1',range:[0,1],
    source:'工程确认-舟山',confirmed:true,provenance:'explicit_override',
    parameter_status:'explicit_confirmed_coefficient',
    ...overrides,
  };
}

/** 取出必需输入控件的完整标签，便于精确断言 checked 属性。 */
function inputTag(html,id){
  const index=html.indexOf('id="'+id+'"');
  assert.ok(index>=0,id+' 控件必须存在');
  const start=html.lastIndexOf('<input',index);
  const end=html.indexOf('>',index);
  return html.slice(start,end+1);
}

function isChecked(html,id){
  return /\schecked(?=[\s>])/.test(inputTag(html,id));
}

/** 模拟“保存 → 后端返回新 snapshot → 重新渲染”。 */
function afterSave(flow,before,fields){
  const payload=thetaV2ShelterPolicyPayload(
    {$(id){return {value:fields[id]===undefined?'':String(fields[id]),checked:fields[id]===true,attributes:{}};}},
    layeredThetaV2Model(flow).shelter.policy,
  );
  const saved={...before,...payload};
  return {payload,snapshot:saved,html:renderLayeredThetaV2Panel(flowWithShelfPolicy(saved))};
}

// ============================================================================
// 1. confirmed === true → checkbox 必须 checked
// ============================================================================

test('confirmed=true 时 shelter checkbox 必须 checked',()=>{
  const flow=flowWithShelfPolicy(confirmedPolicy());
  const policy=layeredThetaV2Model(flow).shelter.policy;
  assert.equal(policy.confirmed,true);
  assert.equal(isChecked(renderLayeredThetaV2Panel(flow),'thetaV2ShelterConfirmed'),true);
});

test('confirmed=false 时 shelter checkbox 必须 unchecked',()=>{
  const flow=flowWithShelfPolicy(confirmedPolicy({
    status:'pending_confirmation',confirmed:false,status_reason:'shelter_coefficient_policy_not_confirmed',
  }));
  assert.equal(layeredThetaV2Model(flow).shelter.policy.confirmed,false);
  assert.equal(isChecked(renderLayeredThetaV2Panel(flow),'thetaV2ShelterConfirmed'),false);
});

test('缺失 confirmed 字段（旧快照）保持 unchecked，绝不默认勾选',()=>{
  const policy=confirmedPolicy();
  delete policy.confirmed;
  const flow=flowWithShelfPolicy(policy);
  assert.equal(layeredThetaV2Model(flow).shelter.policy.confirmed,false);
  assert.equal(isChecked(renderLayeredThetaV2Panel(flow),'thetaV2ShelterConfirmed'),false);
});

test('confirmed 只有严格 true（字符串 / 1 都不算）才勾选',()=>{
  for(const value of ['true',1,'yes',{}]){
    const flow=flowWithShelfPolicy(confirmedPolicy({confirmed:value}));
    assert.equal(layeredThetaV2Model(flow).shelter.policy.confirmed,false,String(value));
    assert.equal(isChecked(renderLayeredThetaV2Panel(flow),'thetaV2ShelterConfirmed'),false,String(value));
  }
});

// ============================================================================
// 2. source / default_coefficient 正确回填
// ============================================================================

test('default_coefficient 与 source 正确回填',()=>{
  const html=renderLayeredThetaV2Panel(flowWithShelfPolicy(confirmedPolicy()));
  assert.match(inputTag(html,'thetaV2ShelterCoefficient'),/value="0\.75"/);
  assert.match(inputTag(html,'thetaV2ShelterSource'),/value="工程确认-舟山"/);
});

test('coefficient 为 null 时不伪造默认值，source 未配置时不回填占位文案',()=>{
  const html=renderLayeredThetaV2Panel(flowWithShelfPolicy(confirmedPolicy({
    default_coefficient:null,source:'未配置；必须由项目工程依据显式确认 shelter_coefficient_policy',
    status:'not_configured',confirmed:false,
  })));
  assert.match(inputTag(html,'thetaV2ShelterCoefficient'),/value=""/);
  assert.match(inputTag(html,'thetaV2ShelterSource'),/value=""/);
  assert.equal(isChecked(html,'thetaV2ShelterConfirmed'),false);
});

// ============================================================================
// 3. 保存 API 返回新 snapshot 后重新 render 仍 checked
// ============================================================================

test('保存 confirmed=true 后，用返回的 snapshot 重新渲染仍然 checked',()=>{
  const before=confirmedPolicy({
    default_coefficient:1,source:'user_defined_baseline',confirmed:false,
    provenance:'user_defined_baseline',status:'pending_confirmation',
  });
  const flow=flowWithShelfPolicy(before);
  assert.equal(layeredThetaV2Model(flow).shelter.policy.confirmed,false);

  const {payload,snapshot,html}=afterSave(flow,before,{
    thetaV2ShelterCoefficient:'0.75',thetaV2ShelterSource:'工程确认-舟山',
    thetaV2ShelterConfirmed:true,
  });
  // payload 必须原样带上 confirmed=true 与 per_grid_overrides。
  assert.equal(payload.confirmed,true);
  assert.equal(payload.default_coefficient,0.75);
  assert.equal(payload.source,'工程确认-舟山');
  assert.deepEqual(payload.per_grid_overrides,{'MHT4063-L08-C00000001-M00000001':0.25});

  // 后端返回的新 snapshot（这里就是保存后的 policy）必须能在重新渲染时保持 checked。
  assert.equal(snapshot.confirmed,true);
  assert.equal(isChecked(html,'thetaV2ShelterConfirmed'),true);
  assert.match(inputTag(html,'thetaV2ShelterCoefficient'),/value="0\.75"/);
  assert.match(inputTag(html,'thetaV2ShelterSource'),/value="工程确认-舟山"/);
});

test('保存 confirmed=false 后重新渲染必须 unchecked',()=>{
  const before=confirmedPolicy();
  const flow=flowWithShelfPolicy(before);
  assert.equal(isChecked(renderLayeredThetaV2Panel(flow),'thetaV2ShelterConfirmed'),true);

  const {snapshot,html}=afterSave(flow,before,{
    thetaV2ShelterCoefficient:'0.75',thetaV2ShelterSource:'工程确认-舟山',
    thetaV2ShelterConfirmed:false,
  });
  assert.equal(snapshot.confirmed,false);
  assert.equal(isChecked(html,'thetaV2ShelterConfirmed'),false);
});

// ============================================================================
// 4. 绑定层：保存走唯一的 shelter 端点，payload 直接来自控件
// ============================================================================

test('bind 注册 shelter 保存并只发往 /api/shelter-coefficient-policy',async()=>{
  const flow=flowWithShelfPolicy(confirmedPolicy());
  const fields={
    thetaV2ShelterCoefficient:'0.6',thetaV2ShelterSource:'工程确认-新依据',
    thetaV2ShelterConfirmed:true,
  };
  const calls=[],handlers={},registered=[];
  const c={
    flow:()=>flow,
    $:id=>(id==='saveThetaV2ShelterPolicy'||id in fields
      ?{value:id in fields?String(fields[id]):'',checked:fields[id]===true,attributes:{}}
      :null),
    panelError:()=>{},
    resourceAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
    actionButton:(id,handler)=>{registered.push(id);handlers[id]=handler;},
  };
  bindLayeredThetaV2(c);
  assert.ok(registered.includes('saveThetaV2ShelterPolicy'));
  await handlers.saveThetaV2ShelterPolicy();
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'/api/shelter-coefficient-policy');
  assert.equal(calls[0][1].confirmed,true);
  assert.equal(calls[0][1].default_coefficient,0.6);
  assert.equal(calls[0][1].source,'工程确认-新依据');
  // 既有 per_grid_overrides 必须原样回传，绝不在保存时清空。
  assert.deepEqual(calls[0][1].per_grid_overrides,{'MHT4063-L08-C00000001-M00000001':0.25});
});
