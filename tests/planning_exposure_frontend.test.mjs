/**
 * BUG-ROUTE-005（收口）：Step03 Theta* V2 面板的「陆地相对风险基线」前端契约。
 *
 * 锁定：
 *   * 输入框在未配置时给出工程建议值 0.08，但 **confirmed / enabled 默认都不勾选**
 *     （前端绝不替用户确认，也绝不自动启用）；
 *   * baseline 空值提交 null（null ≠ 0），后端保持 not_configured；
 *   * 旧的 land_population_floor 只原样回传（兼容记录），绝不换算成 baseline；
 *   * 保存只发往 /api/planning-exposure-policy；
 *   * 保存后（后端 projection）展示 baseline / land_count / water_count / applied /
 *     population_factor_source。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  THETA_V2_LAND_RISK_BASELINE_SUGGESTED,THETA_V2_PLANNING_FACTOR_FORMULA,
  bindLayeredThetaV2,layeredThetaV2Model,renderLayeredThetaV2Panel,
  thetaV2PlanningExposurePayload,
} from '../cns_planner/web/js/workflow/layered_theta_v2.js';

const BASELINE_INPUT='id="thetaV2LandRiskBaseline"';
const THRESHOLD_INPUT='id="thetaV2LandThreshold"';
const SOURCE_INPUT='id="thetaV2LandRiskSource"';
const ENABLED_CHECKBOX='id="thetaV2LandRiskEnabled"';
const CONFIRMED_CHECKBOX='id="thetaV2LandRiskConfirmed"';
const SAVE_BUTTON='id="saveThetaV2PlanningExposure"';

function configuredPolicy(overrides={}){
  return {
    schema_version:'planning-exposure-v2',status:'confirmed',status_reason:null,
    enabled:true,confirmed:true,
    land_relative_risk_baseline:0.08,land_relative_risk_baseline_range:[0,1],
    formula:THETA_V2_PLANNING_FACTOR_FORMULA,
    land_min_surface_elevation_m:0,land_detection_method:'confirmed_surface_elevation_max_threshold',
    land_population_floor:null,land_population_floor_deprecated:true,
    land_population_floor_used_for_planning:false,
    source:'工程确认-舟山',provenance:'explicit_override',
    ...overrides,
  };
}

function unconfiguredPolicy(overrides={}){
  return {
    schema_version:'planning-exposure-v2',status:'not_configured',
    status_reason:'land_relative_risk_baseline_not_configured',
    enabled:false,confirmed:false,land_relative_risk_baseline:null,
    land_relative_risk_baseline_range:[0,1],formula:THETA_V2_PLANNING_FACTOR_FORMULA,
    land_min_surface_elevation_m:null,
    land_detection_method:'confirmed_surface_elevation_max_threshold',
    land_population_floor:null,land_population_floor_deprecated:true,
    land_population_floor_used_for_planning:false,
    source:'未配置；必须由项目工程依据显式确认 planning_exposure_policy',
    provenance:'not_configured',
    ...overrides,
  };
}

function appliedExposure(overrides={}){
  return {
    attribute:'planning_exposure',status:'passed',reason:null,applied:true,enabled:true,
    formula:THETA_V2_PLANNING_FACTOR_FORMULA,
    land_relative_risk_baseline:0.08,land_water_gap:0.08,population_difference_retention:0.92,
    land_min_surface_elevation_m:0,land_count:6,water_count:3,
    unresolved_land_status_count:0,unresolved_population_factor_count:0,
    applied_count:9,land_baseline_raised_count:6,cell_count:9,
    policy_fingerprint:'planningexpv2-pol',field_fingerprint:'planningexpfieldv2-field',
    used_population_nodata_as_sea_proxy:false,
    ...overrides,
  };
}

/** 只含 Theta* V2 面板渲染所需字段的最小 flow。 */
function flowWith({policy=unconfiguredPolicy(),exposure={},shelter={}}={}){
  return {
    layered_route_planner_readiness:{
      status:'blocked',
      algorithm:{algorithm_id:'layered_risk_aware_theta_star_v2',algorithm_version:'2.0'},
      theta_star_v2:{status:'not_ready',blockers:[]},
    },
    shelter_coefficient_policy:{
      schema_version:'population-shelter-v1',status:'confirmed',status_reason:null,
      default_coefficient:1,per_grid_overrides:{},source:'user_defined_baseline',
      confirmed:true,provenance:'user_defined_baseline',parameter_status:'explicit_confirmed_coefficient',
    },
    population_shelter:{
      status:'passed',cells:{},count:9,covered_count:9,
      field_fingerprint:'popshelterv1-abc',
      population_factor_source:'canonical_risk_v2_population_factor',
      ...shelter,
    },
    theta_v2_objective_policy:{risk_weight:0.8,turn_weight:0.1,distance_weight:0.1,confirmed:true},
    max_route_risk_density:{threshold:1,confirmed:true,temporary:true},
    regulatory_constraints:{status:'not_configured'},
    communication_planning_field:{status:'not_configured'},
    layered_route_candidates:{status:'not_calculated',items:[]},
    planning_exposure_policy:policy,
    planning_exposure:exposure,
  };
}

function inputTag(html,id){
  const index=html.indexOf(id);
  assert.ok(index>=0,id+' 控件必须存在');
  const start=html.lastIndexOf('<input',index);
  const end=html.indexOf('>',index);
  return html.slice(start,end+1);
}

function isChecked(html,id){
  return /\schecked(?=[\s>])/.test(inputTag(html,id));
}

function exposureRow(html,key){
  const index=html.indexOf('data-planning-exposure-field="'+key+'"');
  assert.ok(index>=0,key+' 展示行必须存在');
  return html.slice(index,html.indexOf('</div>',index));
}

function payloadFor(fields,policy){
  // 与 bind 一致：payload 读取的是 model 投影，而不是后端原始 policy。
  const modelPolicy=layeredThetaV2Model(flowWith({policy})).planningExposure.policy;
  const c={$(id){return {
    value:fields[id]===undefined?'':String(fields[id]),
    checked:fields[id]===true,attributes:{},
  };}};
  return thetaV2PlanningExposurePayload(c,modelPolicy);
}

// ============================================================================
// 1. 未配置：默认输入 0.08，但绝不自动 confirmed / enabled
// ============================================================================

test('未配置时输入框给出工程建议值 0.08，但 confirmed 与 enabled 都不勾选',()=>{
  const model=layeredThetaV2Model(flowWith());
  assert.equal(model.planningExposure.policy.baseline,null);
  assert.equal(model.planningExposure.policy.baselineSuggested,THETA_V2_LAND_RISK_BASELINE_SUGGESTED);
  assert.equal(model.planningExposure.policy.confirmed,false);
  assert.equal(model.planningExposure.policy.enabled,false);
  assert.equal(model.planningExposure.field.applied,false);

  const html=renderLayeredThetaV2Panel(flowWith());
  assert.match(inputTag(html,BASELINE_INPUT),/value="0\.08"/);
  assert.match(inputTag(html,BASELINE_INPUT),/data-planning-exposure-suggested="0\.08"/);
  assert.equal(isChecked(html,ENABLED_CHECKBOX),false);
  assert.equal(isChecked(html,CONFIRMED_CHECKBOX),false);
  assert.ok(html.includes(SAVE_BUTTON),'保存按钮必须存在');
});

test('后端已确认的基线原样回填（不会被建议值覆盖）',()=>{
  const html=renderLayeredThetaV2Panel(flowWith({
    policy:configuredPolicy({land_relative_risk_baseline:0.07}),
    exposure:appliedExposure({land_relative_risk_baseline:0.07,land_water_gap:0.07}),
  }));
  assert.match(inputTag(html,BASELINE_INPUT),/value="0\.07"/);
  assert.match(inputTag(html,THRESHOLD_INPUT),/value="0"/);
  assert.match(inputTag(html,SOURCE_INPUT),/value="工程确认-舟山"/);
  assert.equal(isChecked(html,ENABLED_CHECKBOX),true);
  assert.equal(isChecked(html,CONFIRMED_CHECKBOX),true);
});

// ============================================================================
// 2. payload：显式提交、null ≠ 0、旧 floor 只回传不换算
// ============================================================================

test('payload 原样提交显式输入，并把 confirmed 交给后端裁决',()=>{
  const payload=payloadFor({
    thetaV2LandRiskBaseline:'0.08',thetaV2LandThreshold:'0',
    thetaV2LandRiskSource:'工程确认-舟山',
    thetaV2LandRiskEnabled:true,thetaV2LandRiskConfirmed:true,
  },unconfiguredPolicy());
  assert.deepEqual(payload,{
    enabled:true,land_relative_risk_baseline:0.08,land_min_surface_elevation_m:0,
    source:'工程确认-舟山',confirmed:true,
  });
});

test('baseline 空值提交 null（null ≠ 0），未勾选 confirmed 时提交 false',()=>{
  const payload=payloadFor({
    thetaV2LandRiskBaseline:'',thetaV2LandThreshold:'',
    thetaV2LandRiskSource:'',thetaV2LandRiskEnabled:true,thetaV2LandRiskConfirmed:false,
  },unconfiguredPolicy());
  assert.equal(payload.land_relative_risk_baseline,null);
  assert.equal(payload.land_min_surface_elevation_m,null);
  assert.equal(payload.confirmed,false);
  assert.equal(payload.enabled,true);
  // 未配置时的后端占位说明不作为 source 回传。
  assert.equal(payload.source,'');
});

test('显式 0 与 null 被严格区分',()=>{
  const zero=payloadFor({
    thetaV2LandRiskBaseline:'0',thetaV2LandThreshold:'0',
    thetaV2LandRiskSource:'工程确认-测试',thetaV2LandRiskEnabled:true,
    thetaV2LandRiskConfirmed:true,
  },unconfiguredPolicy());
  assert.equal(zero.land_relative_risk_baseline,0);
  assert.notEqual(zero.land_relative_risk_baseline,null);
});

test('旧项目的 land_population_floor 只被原样回传，绝不被换算成 baseline',()=>{
  const policy=unconfiguredPolicy({land_population_floor:5});
  const payload=payloadFor({
    thetaV2LandRiskBaseline:'0.08',thetaV2LandThreshold:'0',
    thetaV2LandRiskSource:'工程确认-舟山',
    thetaV2LandRiskEnabled:true,thetaV2LandRiskConfirmed:true,
  },policy);
  assert.equal(payload.land_population_floor,5,
    'deprecated floor 必须原样保留，绝不能在保存时被清空或改写');
  assert.equal(payload.land_relative_risk_baseline,0.08,
    'baseline 只能来自用户的显式输入，不能由 floor 推导');
});

test('用户没有输入 baseline 时，旧 floor 绝不会被当成基线提交',()=>{
  const policy=unconfiguredPolicy({land_population_floor:5});
  const payload=payloadFor({
    thetaV2LandRiskBaseline:'',thetaV2LandThreshold:'0',
    thetaV2LandRiskSource:'工程确认-舟山',
    thetaV2LandRiskEnabled:true,thetaV2LandRiskConfirmed:true,
  },policy);
  assert.equal(payload.land_relative_risk_baseline,null);
  assert.equal(payload.land_population_floor,5);
});

// ============================================================================
// 3. 绑定：只发往 planning-exposure-policy
// ============================================================================

test('bind 注册基线保存并只发往 /api/planning-exposure-policy',async()=>{
  const flow=flowWith();
  const fields={
    thetaV2LandRiskBaseline:'0.08',thetaV2LandThreshold:'0',
    thetaV2LandRiskSource:'工程确认-舟山',
    thetaV2LandRiskEnabled:true,thetaV2LandRiskConfirmed:true,
  };
  const calls=[],handlers={},registered=[];
  const c={
    flow:()=>flow,
    $:id=>(id==='saveThetaV2PlanningExposure'||id in fields
      ?{value:id in fields?String(fields[id]):'',checked:fields[id]===true,attributes:{}}
      :null),
    panelError:()=>{},
    resourceAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
    actionButton:(id,handler)=>{registered.push(id);handlers[id]=handler;},
  };
  bindLayeredThetaV2(c);
  assert.deepEqual(registered,['saveThetaV2PlanningExposure']);
  await handlers.saveThetaV2PlanningExposure();
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'/api/planning-exposure-policy');
  assert.equal(calls[0][1].land_relative_risk_baseline,0.08);
  assert.equal(calls[0][1].confirmed,true);
  assert.equal(calls[0][1].enabled,true);
});

// ============================================================================
// 4. 保存后展示：baseline / land_count / water_count / applied /
//    population_factor_source
// ============================================================================

test('保存后展示 baseline、land/water count、applied 与 population_factor_source',()=>{
  const flow=flowWith({
    policy:configuredPolicy(),
    exposure:appliedExposure(),
    shelter:{population_factor_source:'planning_exposure_land_relative_risk_baseline'},
  });
  const model=layeredThetaV2Model(flow);
  assert.equal(model.planningExposure.field.applied,true);
  assert.equal(model.planningExposure.field.baseline,0.08);
  assert.equal(model.planningExposure.field.landCount,6);
  assert.equal(model.planningExposure.field.waterCount,3);
  assert.equal(model.planningExposure.field.appliedCount,9);
  assert.equal(model.planningExposure.field.populationFactorSource,
    'planning_exposure_land_relative_risk_baseline');
  assert.equal(model.planningExposure.field.landWaterGap,0.08);
  assert.equal(model.planningExposure.field.retention,0.92);

  const html=renderLayeredThetaV2Panel(flow);
  assert.match(exposureRow(html,'land_relative_risk_baseline'),/0\.080000/);
  assert.match(exposureRow(html,'land_count'),/<small>6<\/small>/);
  assert.match(exposureRow(html,'water_count'),/<small>3<\/small>/);
  assert.match(exposureRow(html,'applied'),/<small>true/);
  assert.match(exposureRow(html,'applied'),/applied_count 9/);
  assert.match(exposureRow(html,'population_factor_source'),
    /planning_exposure_land_relative_risk_baseline/);
  assert.match(exposureRow(html,'land_water_gap'),/0\.080000000/);
  assert.match(exposureRow(html,'population_difference_retention'),/0\.920000000/);
  // 公式与"不 clip"边界必须在面板上可见。
  assert.ok(html.includes(THETA_V2_PLANNING_FACTOR_FORMULA));
  assert.match(html,/不做 clip/);
  assert.match(html,/land_water_gap_equals_b|land-water gap/);
});

test('面板明确标出 deprecated floor 与 terrain 缺失的 fail-closed 语义',()=>{
  const html=renderLayeredThetaV2Panel(flowWith({
    policy:unconfiguredPolicy({land_population_floor:5}),
  }));
  const row=exposureRow(html,'land_population_floor');
  assert.match(row,/<small>5\.000000 person/);
  assert.match(row,/deprecated/);
  assert.match(html,/换算成基线|换算成任何 b/);
  assert.match(html,/fail-closed/);
  assert.match(html,/0\.08 只是首轮工程建议值/);
});

test('未配置时展示 resolved=null 的基线，绝不显示成已确认的 0.08',()=>{
  const html=renderLayeredThetaV2Panel(flowWith());
  assert.match(exposureRow(html,'land_relative_risk_baseline'),/null（未配置/);
  assert.equal(isChecked(html,CONFIRMED_CHECKBOX),false);
});
