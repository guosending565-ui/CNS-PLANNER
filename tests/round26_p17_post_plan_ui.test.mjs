/**
 * Round 2.6 前端回归：连续服务可接受性的**阈值分离 / fail-closed / 分层监视 /
 * Radar 能力限制 / baseline vs post_plan / Step6 门禁**。
 *
 * 逐条锁定用户裁定（这些是 Round 2.6 的核心验收点，任何一条退回都判失败）：
 *  1. **设备 failsafe 事实 ≠ 项目规划阈值**：两块必须分开渲染，绝不合并；
 *     FC30 设备事实显示为「失联 > 3 s 可触发 Failsafe RTH」；
 *     项目规划阈值显示为「用户填写 X s」；
 *  2. ``evidence_required === true`` 的通信阈值必须显示**尚未登记 ⇒
 *     判定保持 evidence_required / unknown（fail-closed）**，绝不显示 3 s；
 *  3. 冗余退化最大允许时间显示为**独立阈值**，绝不与完全中断合并；
 *  4. 保护走廊四分量逐项显示（D_separation / V_relative / T_chain / D_maneuver /
 *     D_uncertainty / D_protection + 后端 formula），D_maneuver=50 m 标 engineering_baseline
 *     且明确"不是法规值"；
 *  5. 合作（RID 主要威胁）/ 非合作（Radar 补充威胁）**分两个区块**渲染；
 *  6. Radar 不可行 ⇒ 能力限制（黄色 / 橙色，**不是** error）显示 disclosure 原文，
 *     并写明它不改变主要威胁判定 / 不得表述为「监视已完全满足」；
 *     地图侧**绝不绘制任何 Radar 扇区 / 覆盖几何**；
 *  7. baseline vs post_plan 比较渲染；``post_plan_projection == null`` 时如实写
 *     「尚未计算 post-plan 投影（P16 方案为空或未评估）」；
 *  8. Step6 只允许 ``fully_satisfied`` / ``acceptable_with_managed_gap``，
 *     ``unacceptable`` / ``unknown`` 阻止；每个 variant 显示**自己的** P17 投影结论，
 *     ``evaluated_for_this_variant !== true`` ⇒ 不可判定 / 阻止；
 *  9. 标题不再带 ``（P17）``。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  ACCEPTABILITY_TEXT,PARAMETER_AUTHORITY_TEXT,STEP6_ALLOWED_ACCEPTABILITY,
  THREAT_LAYER_STATUS_TEXT,continuousServiceModel,limitationRows,parameterRows,
  postPlanComparisonRows,postPlanProjectionModel,protectionCorridorRows,
  renderContinuousServicePanel,step6GateAllows,step6GateModel,threatLayerRows,
} from '../cns_planner/web/js/workflow/continuous_service.js';
import {CNS_CANONICAL_CHAIN,render as renderStep05}
  from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  VARIANT_P17_PROJECTION_NOTE,mandatoryDisclosureModel,render as renderStep06,
  variantContinuousServiceProjection,
} from '../cns_planner/web/js/workflow/step06_review.js';
import {drawWorkflowLayers} from '../cns_planner/web/js/map/display_layers.js';
import {continuousServiceLegendModel,radarSurveillanceLayoutState}
  from '../cns_planner/web/js/map/cns_service_overlay.js';

// ---------------------------------------------------------------------------
// 1. P17 投影 fixture（结构与后端 /api/workflow → cns_continuous_service 同形）
// ---------------------------------------------------------------------------

const RADAR_DISCLOSURE='当前方案对合作无人机的监视链满足当前规划要求；非合作无人机补充监视能力'
  +'因 Radar 布局不可行尚未闭合，属于当前方案能力限制。';

const LIMITATION={
  limitation_id:'noncooperative_surveillance_limitation',layer:'noncooperative',
  capability:'Radar 非合作监视（补充威胁分层）',status:'limitation',
  blocking_primary_threat:false,
  semantics:'supplementary_capability_limitation_does_not_change_primary_threat_verdict',
  source_status:'infeasible',solver_status:'infeasible',
  disclosure:RADAR_DISCLOSURE,must_disclose_in_report:true,
  no_relaxation_applied:'本轮没有为了得到方案而扩大覆盖半径、改动 90° 面板或使用假塔。',
  route_id:'R1',
};

/** 参数：完全中断阈值 evidence_required（无依据）、D_separation 有依据、D_maneuver 工程基线。 */
function parametersFixture(){
  return {
    limits_resolution_order:'cns_continuous_service_policy → planning_evidence 显式记录 → 内置工程基线',
    parameters:{
      c_full_outage_max_s:{
        field:'c_full_outage_max_s',label:'最大允许完全通信中断时间',
        value:null,unit:'s',authority:'evidence_required',source_type:'unknown',
        statement:'最大允许完全通信中断时间必须由用户显式登记（engineering_assumption）；'
          +'未登记时保持 evidence_required / unknown，绝不采用 3 s。',
        source:'设备 failsafe 事实不得自动成为本项目的规划阈值。',
        reason:'尚无任何依据：必须由用户 / 工程依据显式提供',
      },
      c_redundancy_degradation_max_s:{
        field:'c_redundancy_degradation_max_s',label:'冗余退化最大允许时间',
        value:10,unit:'s',authority:'builtin_engineering_assumption',source_type:'internal_baseline',
        statement:'冗余退化最大允许时间＝10 s（工程基线，与完全中断是两个独立阈值）。',
      },
      D_separation_m:{
        field:'D_separation_m',label:'分隔距离（D_separation）',
        value:30,unit:'m',authority:'explicit_evidence',source_type:'engineering_assumption',
        statement:'分隔距离由本项目显式登记。',
      },
      D_maneuver_m:{
        field:'D_maneuver_m',label:'机动附加距离（D_maneuver，工程基线 50 m）',
        value:50,unit:'m',authority:'builtin_engineering_assumption',source_type:'internal_baseline',
        source:'Round 2.6 工程基线：机动附加距离固定取 50 m。它不是法规值。',
        statement:'D_maneuver＝50 m（engineering_baseline，可被显式工程依据替换）。',
      },
      D_uncertainty_m:{
        field:'D_uncertainty_m',label:'不确定度距离（D_uncertainty）',
        value:20,unit:'m',authority:'explicit_evidence',source_type:'engineering_assumption',
        statement:'不确定度距离由本项目显式登记。',
      },
    },
  };
}

const CORRIDOR={
  route_path:[[122.0,30.0],[122.1,30.0]],route_length_m:12000,
  cns_requirement_corridor_half_width_m:150,
  D_separation_m:30,D_separation_authority:'explicit_evidence',
  D_maneuver_m:50,D_maneuver_authority:'builtin_engineering_assumption',
  D_maneuver_semantics:'engineering_baseline_interface_not_regulatory_value',
  D_uncertainty_m:20,D_uncertainty_authority:'explicit_evidence',
  D_safety_m:30,
  V_relative_mps:40,relative_speed_basis:'conservative',T_chain_s:7,
  D_protection_m:380,outer_half_width_m:530,
  formula:'D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty',
  status:'evaluated',
};

const THREAT_LAYERS={
  cooperative:{
    layer:'cooperative',label:'合作无人机 / RID 合作监视（主要威胁）',
    service_keys:['S:rid_cooperative'],subsystems:['nominal'],status:'satisfied',
    t_margin_s:5.5,first_detection_distance_m:9000,limitations:[],
    reasons:['合作监视链满足当前规划要求'],
  },
  noncooperative:{
    layer:'noncooperative',label:'非合作无人机 / Radar 非合作监视（补充威胁）',
    service_keys:['S:radar_noncooperative'],subsystems:['limitation'],status:'limitation',
    t_margin_s:null,first_detection_distance_m:null,limitations:[LIMITATION],
    reasons:['Radar 布局不可行'],
  },
};

const RESULT_BASELINE={
  status:'unknown',plan_stage:'baseline',algorithm_id:'continuous_service_acceptability_v1',
  input_fingerprint:'base-fp',route_speed_mps:25,
  service_acceptability_limits:{C:{service_outage:null,redundancy_degradation:10}},
  route_count:1,
  routes:[{
    route_id:'R1',status:'unknown',route_length_m:12000,plan_stage:'baseline',
    corridor:CORRIDOR,threat_layers:THREAT_LAYERS,
    primary_threat_layer:'cooperative',primary_threat_status:'satisfied',
    supplementary_threat_layer:'noncooperative',supplementary_threat_status:'limitation',
    supplementary_threat_is_limitation:true,
    threat_layer_note:'合作无人机（RID）是主要威胁、非合作无人机（Radar）是补充威胁；'
      +'两者分开判定，补充威胁的能力限制不改变主要威胁的结论。',
    limitations:[LIMITATION],
    first_detection_evidence:{
      first_detection_distance_m:9000,service_key:'S:rid_cooperative',
      coverage_status:'covered',usable:true,
    },
    surveillance_acceptance:{status:'acceptable',t_available_s:12,t_margin_s:5.5,reason:null},
    subsystems:[{
      subsystem:'C',service:'C:communication',status:'unknown',events:[],event_count:0,
      limits_s:{service_outage:null,redundancy_degradation:10},longest_event:null,managed_gaps:[],
    }],
    events_by_kind:{service_outage:[],redundancy_degradation:[],
      navigation_degradation:[],surveillance_detection_gap:[]},
    managed_gaps:[],reasons:[],reason_codes:['no_outage_threshold_evidence'],
  }],
  parameters:parametersFixture().parameters,
  limitations:[LIMITATION],
  primary_threat_status:'satisfied',supplementary_threat_status:'limitation',
  baseline_status:'unknown',post_plan_status:null,baseline:null,post_plan_projection:null,
  reasons:[],reason_codes:['no_outage_threshold_evidence'],
  managed_gap_count:0,unacceptable_count:0,unknown_count:1,
  disclosure_lines:[
    '[能力限制] Radar 非合作监视（补充威胁分层）',RADAR_DISCLOSURE,
    '  披露语义：本限制**不改变**主要威胁（合作无人机 / RID）的判定，'
      +'但报告与方案评审必须同时显示；不得表述为「监视已完全满足」。',
  ],
  not_evaluated:{common_cause:'not_evaluated'},
};

const POST_PLAN_PROJECTION={
  available:true,status:'acceptable_with_managed_gap',route_count:1,
  applied_action_ids:['candidate_site:S1:C1','candidate_site:S2:C1'],
  persisted_as_upstream:false,
  projection_semantics:'hypothetical_post_plan_state_never_written_into_existing_cns',
  comparison:{
    improved_service_count:1,remaining_gap_count:1,
    improved_services:[{
      route_id:'R1',subsystem:'S',service:'S:rid_cooperative',status:'satisfied',
      kind:null,length_m:null,duration_s:null,limit_s:null,exceeds_limit:false,
      improvement_kind:'confirmed_gap_resolved',declared_improvement_m:null,
      baseline_length_m:2500,baseline_duration_s:100,
      reduction_m:2500,semantics:'gap_event_absent_in_post_plan_state',
    }],
    remaining_gaps:[{
      route_id:'R1',subsystem:'C',service:'C:communication',
      status:'acceptable_with_managed_gap',kind:'service_outage',length_m:60,
      duration_s:3,limit_s:3,exceeds_limit:false,
      improvement_kind:'declared_partial_improvement',declared_improvement_m:40,
      baseline_length_m:100,baseline_duration_s:5,
      semantics:'gap_event_still_present_in_post_plan_state',
    }],
    projection:{
      baseline_fingerprint:'base-fp',post_plan_fingerprint:'post-fp',
      applied_action_ids:['candidate_site:S1:C1','candidate_site:S2:C1'],
      projection_semantics:'hypothetical_post_plan_state_never_written_into_existing_cns',
      persisted_as_upstream:false,
    },
    comparison_basis:'longest_continuous_event_per_route_subsystem_service',
  },
};

const RESULT_POST_PLAN={
  ...RESULT_BASELINE,
  status:'acceptable_with_managed_gap',plan_stage:'post_plan',input_fingerprint:'post-fp',
  baseline_status:'unknown',post_plan_status:'acceptable_with_managed_gap',
  post_plan_projection:POST_PLAN_PROJECTION,
  managed_gap_count:1,unknown_count:0,
};

const FC30={
  aircraft_id:'FC30',selected_aircraft_id:'FC30',is_selected:true,
  facts:[{parameter:'rc_loss_failsafe_trigger_s',value:3,unit:'s',
    source_type:'confirmed_source_fact',not_a_regulatory_threshold:true,
    source:'FC30 机载档案（canonical）',
    statement:'在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失超过 3 s 触发 RTH。'}],
  device_failsafe_fact:{
    parameter:'rc_loss_failsafe_trigger_s',value_s:3,authority:'confirmed_source_fact',
    source_type:'confirmed_source_fact',kind:'device_failsafe_fact',is_planning_threshold:false,
    semantics:'device_failsafe_trigger_fact_not_regulatory_threshold',
    statement:'FC30 设备事实：在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失超过 3 s '
      +'触发自动返航。它不是法规阈值，也不是本项目的规划阈值。',
  },
  //: A 情形：用户**尚未登记**项目规划阈值 ⇒ evidence_required / unknown（fail-closed）。
  project_planning_threshold:{
    parameter:'c_full_outage_max_s',value_s:null,authority:'evidence_required',
    source_type:'unknown',kind:'project_planning_threshold',is_planning_threshold:true,
    must_be_engineering_assumption:true,evidence_required:true,
    statement:'本项目的「最大允许完全通信中断时间」由用户显式登记，身份必须是工程规划假设。'
      +'未登记时 P17 的通信判定保持 evidence_required / unknown（fail-closed），'
      +'绝不自动采用设备 failsafe 的 3 s。',
    source:null,reason:'尚无任何依据：必须由用户 / 工程依据显式提供',
  },
  redundancy_degradation_threshold:{
    parameter:'c_redundancy_degradation_max_s',value_s:10,
    authority:'builtin_engineering_assumption',source_type:'internal_baseline',
    kind:'project_planning_threshold',is_planning_threshold:true,separate_from_full_outage:true,
    statement:'冗余退化阈值与完全中断阈值是两个独立阈值，绝不合并。',
  },
  thresholds_are_separate:true,threshold_merge_forbidden:true,
  disclosure:'FC30 的 3 s 是设备 failsafe 触发门限，不是法规阈值。',
};

/** B 情形：用户已显式登记 12 s（engineering_assumption）。 */
function fc30WithRegisteredThreshold(value=12){
  return {
    ...FC30,
    project_planning_threshold:{
      ...FC30.project_planning_threshold,value_s:value,authority:'explicit_evidence',
      source_type:'engineering_assumption',evidence_required:false,
      source:'RFC-2026-001 工程评审结论',reason:null,
    },
  };
}

function projectionFixture({result=RESULT_BASELINE,fc30=FC30,step6Gate=null}={}){
  return {
    result,
    parameters:parametersFixture(),
    policy:{status:'configured',service_acceptability_limits:{C:{service_outage:null}},source:'用户配置'},
    operation_scenario:{single_ownship:true,intruder_scope:'other_uav_only',cruise_altitude:'ALT-100'},
    fc30,
    step6_gate:step6Gate||{
      status:result.status,confirmation_allowed:false,
      allowed_statuses:[...STEP6_ALLOWED_ACCEPTABILITY],
      projected_status:result.status,variant_id:null,variant_specific:false,
      limitations:[LIMITATION],
      engineered_assumptions:[{field:'c_full_outage_max_s',authority:'evidence_required'}],
      managed_gap_count:result.managed_gap_count,unacceptable_count:result.unacceptable_count,
      unknown_count:result.unknown_count,requires_managed_gap_disclosure:true,
      disclosure_lines:result.disclosure_lines,reasons:[],
    },
  };
}

function p17Flow({projection=null,review=null,confirmed=null,reports=null}={}){
  return {
    project:{name:'Round 2.6 测试项目'},
    workspace:null,aircraft:null,rules:null,coverage:null,coverage_3d:{},
    operational_routes:[{route_id:'R1',status:'not_calculated'}],
    result_statuses:{},
    cns_continuous_service:projection||projectionFixture(),
    cns_plan_review:review||{},
    confirmed_cns_plan:confirmed||{},
    cns_planning_reports:reports||{},
    steps:{'5':true,'6':false},
  };
}

const STATE={data_health:{status:'ready',label:'正常'}};

// ---------------------------------------------------------------------------
// 2. 设备 failsafe 事实 ≠ 本项目规划阈值（两块分开、不合并）
// ---------------------------------------------------------------------------

test('阈值分块：设备事实与项目规划阈值是两个独立 data 块，绝不合并',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/data-continuous-threshold-block="device_failsafe_fact"/);
  assert.match(html,/data-continuous-threshold-block="project_planning_threshold"/);
  assert.match(html,/data-continuous-threshold-block="redundancy_degradation_threshold"/);
  //: 三块必须彼此独立地出现一次（没有任何"合并成一块"的路径）。
  for(const block of ['device_failsafe_fact','project_planning_threshold',
    'redundancy_degradation_threshold']){
    const matches=[...html.matchAll(new RegExp(`data-continuous-threshold-block="${block}"`,'g'))];
    assert.equal(matches.length,1,`${block} 必须恰好出现一次`);
  }
  assert.match(html,/通信阈值：设备事实 vs 本项目规划阈值/);
  assert.match(html,/工程规划参数，由用户确认/);
});

test('设备事实按裁定文案显示「失联 > 3 s 可触发 Failsafe RTH」',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/FC30 设备事实：失联 &gt; 3 s 可触发 Failsafe RTH/);
  assert.match(html,/kind=device_failsafe_fact/);
  assert.match(html,/is_planning_threshold=否/);
  assert.match(html,/它不参与 B 的取值/);
  //: 设备事实绝不是规划阈值。
  const device=html.slice(html.indexOf('data-continuous-threshold-block="device_failsafe_fact"'),
    html.indexOf('data-continuous-threshold-block="project_planning_threshold"'));
  assert.ok(device.length>0);
  assert.doesNotMatch(device,/is_planning_threshold=是/);
});

test('项目规划阈值显示「用户填写 X s」，并保留 authority 与来源',()=>{
  const html=renderContinuousServicePanel(p17Flow({
    projection:projectionFixture({fc30:fc30WithRegisteredThreshold(12)}),
  }),[]);
  assert.match(html,/本项目规划阈值：用户填写 12 s/);
  assert.match(html,/RFC-2026-001 工程评审结论/);
  assert.match(html,/evidence_required=否/);
  //: 已登记时**不再**出现 fail-closed 的"尚未登记"提示。
  assert.doesNotMatch(html,/本项目规划阈值尚未登记/);
});

test('evidence_required 的通信阈值 fail-closed：显示尚未登记，绝不显示 3 s',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/本项目规划阈值：用户填写 —（尚未登记）/);
  assert.match(html,/evidence_required=是/);
  assert.match(html,
    /本项目规划阈值尚未登记 ⇒ 判定保持 evidence_required \/ unknown（fail-closed）/);
  assert.match(html,/设备 failsafe 门限（A）不会被自动采用为 B 的取值/);
  //: 项目规划阈值那一块里绝不能出现"3 s"（绝不自动采用设备门限）。
  const start=html.indexOf('data-continuous-threshold-block="project_planning_threshold"');
  const end=html.indexOf('data-continuous-threshold-block="redundancy_degradation_threshold"');
  const projectBlock=html.slice(start,end);
  assert.ok(projectBlock.length>0);
  assert.doesNotMatch(projectBlock,/取值 3 s|用户填写 3 s/,
    'fail-closed：项目规划阈值不得显示为设备门限 3 s');
  //: 参数表也逐项标出 evidence_required。
  assert.match(html,/data-evidence-required="true"/);
  assert.match(html,/尚无依据（必须显式登记）/);
});

test('冗余退化阈值显示为独立阈值，绝不与完全中断合并',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/冗余退化最大允许时间 10 s/);
  assert.match(html,/独立阈值，绝不与完全中断合并/);
  assert.match(html,/separate_from_full_outage=是/);
  assert.match(html,/thresholds_are_separate=是/);
  assert.match(html,/threshold_merge_forbidden=是/);
});

test('参数 authority 词表包含 evidence_required（尚无依据 / 必须显式登记）',()=>{
  assert.equal(PARAMETER_AUTHORITY_TEXT.evidence_required.label,'尚无依据（必须显式登记）');
  assert.match(PARAMETER_AUTHORITY_TEXT.evidence_required.note,/evidence_required/);
  assert.match(PARAMETER_AUTHORITY_TEXT.evidence_required.note,/绝不用 0/);
  const rows=parameterRows(continuousServiceModel(p17Flow()));
  const byField=Object.fromEntries(rows.map(row=>[row.field,row]));
  assert.equal(byField.c_full_outage_max_s.evidenceRequired,true);
  //: 参数表顺序包含四分量，且不再单独显示 Round 2.5 的 D_safety_m 输入。
  assert.ok(rows.some(row=>row.field==='D_separation_m'));
  assert.ok(rows.some(row=>row.field==='D_maneuver_m'));
  assert.ok(rows.some(row=>row.field==='D_uncertainty_m'));
});

// ---------------------------------------------------------------------------
// 3. 四分量保护走廊
// ---------------------------------------------------------------------------

test('保护走廊四分量逐项显示，D_maneuver=50 m 标 engineering_baseline（不是法规值）',()=>{
  const rows=protectionCorridorRows(continuousServiceModel(p17Flow()));
  const row=rows[0];
  assert.equal(row.dSeparationM,30);
  assert.equal(row.dManeuverM,50);
  assert.equal(row.dManeuverAuthority,'builtin_engineering_assumption');
  assert.equal(row.dUncertaintyM,20);
  assert.equal(row.vRelativeMps,40);
  assert.equal(row.tChainS,7);
  assert.equal(row.dProtectionM,380);
  assert.equal(row.formula,
    'D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty');

  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/data-protection-corridor="R1"/);
  assert.match(html,/D_separation 30\.0 m（explicit_evidence）/);
  assert.match(html,/D_maneuver 50\.0 m（builtin_engineering_assumption/);
  assert.match(html,/engineering_baseline，不是法规值/);
  assert.match(html,/D_maneuver_semantics=engineering_baseline_interface_not_regulatory_value/);
  assert.match(html,/D_uncertainty 20\.0 m（explicit_evidence）/);
  assert.match(html,/D_protection 380\.0 m/);
  assert.match(html,
    /D_protection = D_separation \+ V_relative × T_chain \+ D_maneuver \+ D_uncertainty/);
  assert.match(html,/公式（来自后端）/);
});

// ---------------------------------------------------------------------------
// 4. 合作 / 非合作监视分开显示
// ---------------------------------------------------------------------------

test('threatLayerRows 分开转印合作（主要）与非合作（补充）',()=>{
  const rows=threatLayerRows(continuousServiceModel(p17Flow()));
  assert.equal(rows.length,2);
  const byLayer=Object.fromEntries(rows.map(row=>[row.layer,row]));
  assert.equal(byLayer.cooperative.status,'satisfied');
  assert.equal(byLayer.cooperative.statusText,'满足');
  assert.equal(byLayer.cooperative.tMarginS,5.5);
  assert.equal(byLayer.cooperative.firstDetectionDistanceM,9000);
  assert.deepEqual(byLayer.cooperative.limitations,[]);
  assert.equal(byLayer.noncooperative.status,'limitation');
  assert.equal(byLayer.noncooperative.statusText,'能力限制');
  assert.equal(byLayer.noncooperative.isLimitation,true);
  assert.equal(byLayer.noncooperative.firstDetectionDistanceM,null);
  assert.equal(byLayer.noncooperative.limitations.length,1);
});

test('面板把合作与非合作渲染成两个独立区块（各自标题 + data 属性）',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/合作无人机 \/ RID 合作监视（主要威胁）/);
  assert.match(html,/非合作无人机 \/ Radar 非合作监视（补充威胁）/);
  assert.match(html,/data-threat-layer="cooperative"/);
  assert.match(html,/data-threat-layer="noncooperative"/);
  assert.match(html,/data-threat-layer-status="satisfied"/);
  assert.match(html,/data-threat-layer-status="limitation"/);
  assert.match(html,/绝对不合并|绝不合并成一个监视结论/);
  //: 非合作分层的卡片自身带能力限制标记（黄色 / 橙色语义）。
  assert.match(html,/data-threat-layer="noncooperative"[^>]*data-capability-limitation="true"/);
});

test('分层状态中文映射覆盖六个权威枚举，limitation 标 warning 不是 error',()=>{
  const expected={
    satisfied:'满足',acceptable_with_managed_gap:'有管理的缺口',limitation:'能力限制',
    unacceptable:'不可接受',unknown:'不可判定',not_applicable:'不适用',
  };
  for(const [status,label] of Object.entries(expected)){
    assert.equal(THREAT_LAYER_STATUS_TEXT[status].label,label);
  }
  assert.equal(THREAT_LAYER_STATUS_TEXT.limitation.badge,'warning');
  assert.notEqual(THREAT_LAYER_STATUS_TEXT.limitation.badge,'failed');
  assert.match(THREAT_LAYER_STATUS_TEXT.limitation.note,/不是系统错误/);
});

// ---------------------------------------------------------------------------
// 5. Radar 不可行：能力限制（非 error），且地图绝不画扇区
// ---------------------------------------------------------------------------

test('Radar 不可行显示为能力限制，disclosure 原文与"不改变主要威胁"逐字出现',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/能力限制（limitation，不是系统错误）/);
  assert.match(html,/能力限制（黄色 \/ 橙色）/);
  assert.match(html,new RegExp(RADAR_DISCLOSURE.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  assert.match(html,/该限制不改变主要威胁（合作无人机 \/ RID）的判定；不得表述为「监视已完全满足」/);
  assert.match(html,/blocking_primary_threat=否/);
  assert.match(html,/solver_status=infeasible/);
  assert.match(html,/data-limitation-id="noncooperative_surveillance_limitation"/);
});

test('limitationRows 只转印后端 limitations（顶层与逐 route 去重）',()=>{
  const rows=limitationRows(continuousServiceModel(p17Flow()));
  assert.equal(rows.length,1);
  assert.equal(rows[0].blockingPrimaryThreat,false);
  assert.equal(rows[0].status,'limitation');
  assert.equal(rows[0].routeId,'R1');
});

test('Radar 不可行时地图侧绝不绘制任何 Radar 扇区 / 覆盖几何',()=>{
  const flow={
    ...p17Flow(),
    radar_surveillance_layout:{
      status:'infeasible',
      items:[{
        route_id:'R1',status:'infeasible',demo_preview_only:true,
        candidate_tower_count:12,candidate_panel_count:24,
        selected_tower_count:0,selected_panel_count:0,
        solver:{status:'infeasible'},infeasibility_reasons:['no_feasible_panel_selection'],
        selected_panels:[{
          panel_id:'P1',tower_id:'T1',longitude:122.05,latitude:30.02,radar_type:'radar_i',
          azimuth_deg:90,panel_half_width_deg:45,horizontal_inner_radius_m:120,
          horizontal_outer_radius_m:3000,plane_intersection_status:'intersected',
        }],
      }],
    },
  };
  //: 让"非合作"成为该航路的监视证据来源：此时求解不可行 ⇒ 该分层没有任何可用几何，
  //: 覆盖点 / 缺口段 / 扇区一律不画（绝不伪造覆盖）。
  flow.cns_continuous_service.result.routes[0].first_detection_evidence={
    first_detection_distance_m:9000,service_key:'S:radar_noncooperative',
    coverage_status:'covered',usable:true,
  };
  const calls={beginPath:0,arc:0,fill:0,stroke:0,fillText:0,setLineDash:0,lineTo:0};
  const texts=[];
  const ctx={
    font:'',fillStyle:'',strokeStyle:'',lineWidth:1,globalAlpha:1,lineCap:'butt',
    save(){},restore(){},beginPath(){calls.beginPath+=1;},closePath(){},
    moveTo(){},lineTo(){calls.lineTo+=1;},arc(){calls.arc+=1;},rect(){},
    fill(){calls.fill+=1;},stroke(){calls.stroke+=1;},
    setLineDash(){calls.setLineDash+=1;},
    fillText(text){calls.fillText+=1;texts.push(String(text));},
    strokeText(){},measureText(value){return {width:String(value).length*6};},
    fillRect(){},strokeRect(){},drawImage(){},
  };
  drawWorkflowLayers({
    ctx,view:{x:0,y:0,res:100},flow,
    plan:{styles:{scenarioAlpha:1,scenarioWidth:2,operationalWidth:3,operationalAlpha:1,
      gapWidth:2,pointRadius:4,coverageRing:false,nameMode:'hidden'},
      nodes:[],landingSites:[],towers:[],cnsTowerCandidates:[],
      priorityIds:new Set(),priorityCoordinates:new Set(),
      selectedReferenceId:null,selectedScreen:null},
    layers:{radarSurveillanceLayer:true,surveillanceProtectionLayer:true,
      //: 显式关闭候选航路图层，本断言只关心 Radar / 监视覆盖是否画了几何。
      layeredCandidateLayer:false},
    screenPoint:coordinate=>[(Number(coordinate[0])-121.9)*10000,(30.1-Number(coordinate[1]))*10000],
    gridTheme:null,
    drawWorkspace:()=>{},drawGridThemes:()=>{},drawGridBoundaries:()=>{},
    drawBuildingFootprints:()=>{},drawConstraintLayer:()=>{},proposedPlanActions:()=>[],
  });
  //: 即使后端下发了"已选面板"，不可行时也绝不画任何几何（不得伪造扇区）。
  assert.equal(calls.beginPath,0,'不可行时不得产生任何路径（绝不伪造扇区）');
  assert.equal(calls.lineTo,0,'不可行时不得画任何线段');
  assert.equal(calls.arc,0,'不可行时不得画任何扇区 / 覆盖点（即使 evidence.usable=true）');
  assert.equal(calls.fill,0,'不可行时不得填充任何几何');
  assert.equal(calls.stroke,0,'不可行时不得描边任何几何');
  assert.ok(calls.fillText>0,'不可行时必须留下真实候选 / 能力限制说明');
  assert.ok(texts.some(text=>text.includes('能力限制（黄色/橙色，不是系统错误）')));
  assert.ok(texts.some(text=>text.includes('不绘制任何 Radar 扇区 / 覆盖几何')));
  assert.ok(texts.some(text=>text.includes('候选铁塔 12 个')),'必须如实给出真实候选规模');
  //: 主要威胁（合作 RID）的结论仍然照常显示：补充威胁的限制不改变主要威胁。
  assert.ok(texts.some(text=>text.includes('[合作 RID 主要] T_margin=5.5 s')));
});

test('能力限制图例只标黄色 / 橙色，绝不使用 error 语义',()=>{
  const flow={...p17Flow(),radar_surveillance_layout:{
    status:'infeasible',items:[{route_id:'R1',status:'infeasible',demo_preview_only:true,
      candidate_tower_count:12,candidate_panel_count:24,solver:{status:'infeasible'}}]}};
  const state=radarSurveillanceLayoutState(flow);
  assert.equal(state.infeasible,true);
  assert.equal(state.stateLabel,'能力限制（黄色 / 橙色）');
  const lines=continuousServiceLegendModel({flow});
  const limitation=lines.find(line=>line.id==='capability-limitation');
  assert.equal(limitation.limitation,true);
  assert.equal(limitation.state,'能力限制（黄色 / 橙色）');
  assert.doesNotMatch(limitation.state,/error|错误/i);
  assert.match(limitation.note,/不是系统错误/);
  assert.match(limitation.note,/不得表述为「监视已完全满足」/);
});

// ---------------------------------------------------------------------------
// 6. baseline vs post_plan
// ---------------------------------------------------------------------------

test('post_plan 可用时渲染两层状态与比较结果',()=>{
  const html=renderContinuousServicePanel(p17Flow({
    projection:projectionFixture({result:RESULT_POST_PLAN}),
  }),[]);
  assert.match(html,/baseline vs post_plan/);
  assert.match(html,/data-baseline-status="unknown"/);
  assert.match(html,/data-post-plan-status="acceptable_with_managed_gap"/);
  assert.match(html,/data-post-plan-available="true"/);
  assert.match(html,/投影航路数 1/);
  assert.match(html,/candidate_site:S1:C1、candidate_site:S2:C1/);
  assert.match(html,/persisted_as_upstream=否/);
  assert.match(html,/比较：改进 1 项 · 剩余缺口 1 项/);
  assert.match(html,/improved_services（实施后改进的服务）/);
  assert.match(html,/remaining_gaps（实施后仍然存在的缺口）/);
  assert.match(html,/data-post-plan-row="improved"/);
  assert.match(html,/data-post-plan-row="remaining_gap"/);
  assert.match(html,/improvement_kind=confirmed_gap_resolved/);
  assert.match(html,/improvement_kind=declared_partial_improvement/);
  assert.match(html,/declared_improvement_m 40\.0 m/);
  assert.match(html,/baseline_length_m 100\.0 m/);
  assert.match(html,/baseline_duration_s 5\.0 s/);
});

test('post_plan_projection 为 null 时如实写「尚未计算」，绝不冒充',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/data-post-plan-available="false"/);
  assert.match(html,/尚未计算 post-plan 投影（P16 方案为空或未评估）/);
  assert.match(html,/post_plan_status：—（尚未计算）/);
  assert.doesNotMatch(html,/data-post-plan-available="true"/);

  const model=postPlanProjectionModel(continuousServiceModel(p17Flow()));
  assert.equal(model.available,false);
  assert.equal(model.improvedServices.length,0);
  assert.equal(model.remainingGaps.length,0);
  assert.equal(model.improvedServiceCount,null,'缺失时必须是 null，绝不是 0');
});

test('postPlanComparisonRows 逐项转印比较字段',()=>{
  const model=continuousServiceModel(p17Flow({
    projection:projectionFixture({result:RESULT_POST_PLAN}),
  }));
  const rows=postPlanComparisonRows(model);
  assert.equal(rows.projection.available,true);
  assert.deepEqual(rows.improved.map(row=>row.changed),['improved']);
  assert.deepEqual(rows.remaining.map(row=>row.changed),['remaining_gap']);
  const remaining=rows.remaining[0];
  assert.equal(remaining.routeId,'R1');
  assert.equal(remaining.subsystem,'C');
  assert.equal(remaining.service,'C:communication');
  assert.equal(remaining.lengthM,60);
  assert.equal(remaining.durationS,3);
  assert.equal(remaining.limitS,3);
  assert.equal(remaining.exceedsLimit,false);
  assert.equal(remaining.declaredImprovementM,40);
  assert.equal(remaining.baselineLengthM,100);
  assert.equal(remaining.baselineDurationS,5);
});

// ---------------------------------------------------------------------------
// 7. Step6：门禁允许值 + 每个 variant 自己的 P17 投影
// ---------------------------------------------------------------------------

/** 一个 Plan Variant：携带**它自己**的 continuous_service_projection。 */
function variantFixture({variantId='PV-1',name='Baseline',projection=null}={}){
  return {
    variant_id:variantId,name,source:'baseline',selected_action_ids:['A1'],status:'evaluated',
    evaluation:{
      confirmation_gate:{status:'ready_for_confirmation'},
      comparison_matrix:[],action_summary:{explicit_costs_by_unit:{}}
      ,continuous_service_projection:projection,
    },
  };
}

function step6Flow(variants,selectedId='PV-1'){
  return {
    ...p17Flow(),
    steps:{'5':true,'6':true},
    cns_plan_review:{status:'current',selected_variant_id:selectedId,variants},
  };
}

test('Step6 门禁只允许两个结论：fully_satisfied / acceptable_with_managed_gap',()=>{
  assert.deepEqual(STEP6_ALLOWED_ACCEPTABILITY,
    ['fully_satisfied','acceptable_with_managed_gap']);
  assert.equal(step6GateAllows('fully_satisfied'),true);
  assert.equal(step6GateAllows('acceptable_with_managed_gap'),true);
  for(const blocked of ['unacceptable','unknown','limitation','not_applicable','not_calculated',null]){
    assert.equal(step6GateAllows(blocked),false,`${blocked} 必须阻止确认`);
  }
  //: 门禁区块如实显示后端允许值与 projected_status / variant 字段。
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.match(html,/允许：彻底满足（fully_satisfied）\/ 有管理的缺口（acceptable_with_managed_gap）/);
  assert.match(html,/projected_status/);
  assert.match(html,/variant_specific=/);
});

test('step6GateModel 转印 projected_status / variant_id / limitations / engineered_assumptions',()=>{
  const gate=step6GateModel(p17Flow({
    projection:projectionFixture({
      result:RESULT_POST_PLAN,
      step6Gate:{
        status:'acceptable_with_managed_gap',confirmation_allowed:true,
        allowed_statuses:[...STEP6_ALLOWED_ACCEPTABILITY],
        projected_status:'acceptable_with_managed_gap',variant_id:'PV-2',variant_specific:true,
        limitations:[LIMITATION],
        engineered_assumptions:[{field:'D_maneuver_m',authority:'builtin_engineering_assumption'}],
        managed_gap_count:1,unacceptable_count:0,unknown_count:0,
        requires_managed_gap_disclosure:true,disclosure_lines:[],reasons:[],
      },
    }),
  }));
  assert.equal(gate.projectedStatus,'acceptable_with_managed_gap');
  assert.equal(gate.variantId,'PV-2');
  assert.equal(gate.variantSpecific,true);
  assert.equal(gate.limitations.length,1);
  assert.equal(gate.engineeredAssumptions[0].field,'D_maneuver_m');
});

test('variant 卡片显示该 variant 自己的 P17 结论与其投影态语义',()=>{
  const variants=[variantFixture({projection:{
    evaluated_for_this_variant:true,evaluated_basis:'variant_projection',
    projected_status:'acceptable_with_managed_gap',status:'acceptable_with_managed_gap',
    baseline_status:'unknown',post_plan_status:'acceptable_with_managed_gap',
    projected_managed_gap_count:1,projected_unacceptable_count:0,projected_unknown_count:0,
    comparison:{improved_service_count:1,remaining_gap_count:1,improved_services:[],remaining_gaps:[]},
    limitations:[LIMITATION],applied_action_ids:['candidate_site:S1:C1'],
    variant_specific_gate:{status:'acceptable_with_managed_gap',confirmation_allowed:true,
      variant_specific:true,blocking_reason:null},
  }})];
  const html=renderStep06({state:STATE,flow:step6Flow(variants)});
  assert.match(html,/本 variant 实施后的 P17 结论/);
  assert.match(html,/data-variant-p17="PV-1"/);
  assert.match(html,/本 variant 实施后的 P17 结论：存在有管理的缺口（managed gap）/);
  assert.match(html,/projected_status=acceptable_with_managed_gap/);
  assert.match(html,/evaluated_for_this_variant=true/);
  assert.match(html,new RegExp(VARIANT_P17_PROJECTION_NOTE));
  assert.match(html,/该结论属于本 variant 的投影态，不是现网事实/);
  //: 每个 variant 必须显示**自己的**结论：第二个 variant 不得复制第一个。
  assert.match(html,/managed gap 1 · unacceptable 0 · unknown 0/);
});

test('evaluated_for_this_variant !== true ⇒ 显示尚未评估并保持不可判定 / 阻止',()=>{
  const variants=[variantFixture({variantId:'PV-9',name:'未评估方案',projection:{
    evaluated_for_this_variant:false,evaluated_basis:'unavailable',
    projected_status:null,status:null,limitations:[],applied_action_ids:['A1'],
    variant_specific_gate:{status:'unknown',confirmation_allowed:false,variant_specific:true,
      blocking_reason:'本 variant 的 P17 投影结论不可用（fail-closed）'},
  }})];
  const html=renderStep06({state:STATE,flow:step6Flow(variants,'PV-9')});
  assert.match(html,/本 variant 尚未做 P17 评估/);
  assert.match(html,/projected_status=缺失|projected_status=—/);
  assert.match(html,/不可判定 \/ 阻止/);
  assert.match(html,/evaluated_for_this_variant=false/);
  //: 缺 projected_status 时**绝不**显示为通过。
  assert.doesNotMatch(html,/本 variant 实施后的 P17 结论：完全满足/);
  assert.doesNotMatch(html,/evaluated_for_this_variant=true[^<]*完全满足/);
  //: blocker 列表如实登记（fail-closed 的可见性）。
  assert.match(html,/本 variant 尚未做 P17 评估/);
});

test('unacceptable / unknown 的 variant 一律显示为阻止',()=>{
  for(const [status,label] of [['unacceptable','不可接受'],['unknown','不可判定']]){
    const variants=[variantFixture({projection:{
      evaluated_for_this_variant:true,projected_status:status,status,
      limitations:[],variant_specific_gate:{status,confirmation_allowed:false,variant_specific:true},
    }})];
    const html=renderStep06({state:STATE,flow:step6Flow(variants)});
    assert.match(html,new RegExp(`本 variant 实施后的 P17 结论：${label}`));
    assert.match(html,/P17 门禁：阻止确认（fail-closed）/);
    assert.match(html,/本 variant 实施后的 P17 结论不允许确认/);
  }
});

test('variantContinuousServiceProjection 是纯转印 + fail-closed（不推导）',()=>{
  const passed=variantContinuousServiceProjection(variantFixture({projection:{
    evaluated_for_this_variant:true,projected_status:'fully_satisfied',
  }}));
  assert.equal(passed.passes,true);
  assert.equal(passed.label,'完全满足');
  const notEvaluated=variantContinuousServiceProjection(variantFixture({projection:{
    evaluated_for_this_variant:false,projected_status:'fully_satisfied',
  }}));
  assert.equal(notEvaluated.evaluated,false);
  assert.equal(notEvaluated.effectiveStatus,null);
  assert.equal(notEvaluated.passes,false,
    'evaluated_for_this_variant !== true 时即使后端给了 projected_status 也绝不放行');
  assert.equal(notEvaluated.label,'不可判定（尚未评估 ⇒ 阻止）');
  assert.equal(notEvaluated.statusText,'不可判定（unknown，fail-closed）',
    '中文状态词来自后端权威词表，绝不自造第二套取词');
  const missing=variantContinuousServiceProjection(variantFixture({projection:null}));
  assert.equal(missing.present,false);
  assert.equal(missing.passes,false);
  assert.match(missing.note,/fail-closed/);
  //: 仍然如实保留后端原值（供审计），只是不当作"通过"。
  assert.equal(notEvaluated.projectedStatus,'fully_satisfied');
});

test('Step6 强制披露中心集中显示 managed gap / 工程假设 / limitations / FC30 与阈值 / 保护参数',()=>{
  const model=mandatoryDisclosureModel(p17Flow());
  assert.ok(model.managedGapDisclosureLines.length>0);
  assert.ok(model.engineeredAssumptions.some(item=>item.field==='c_full_outage_max_s'
    &&item.evidenceRequired===true));
  assert.ok(model.engineeredAssumptions.some(item=>item.field==='D_maneuver_m'));
  assert.equal(model.limitations.length,1);
  assert.equal(model.fc30.deviceFailsafeFact.value_s,3);
  assert.equal(model.fc30.projectPlanningThreshold.evidence_required,true);
  assert.equal(model.fc30.redundancyDegradationThreshold.separate_from_full_outage,true);
  assert.ok(model.routeProtection.some(item=>item.field==='D_separation_m'));

  const html=renderStep06({state:STATE,flow:step6Flow([variantFixture()])});
  assert.match(html,/强制披露/);
  assert.match(html,/data-capability-limitation="true"/);
  assert.match(html,new RegExp(RADAR_DISCLOSURE.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  assert.match(html,/不得表述为「监视已完全满足」/);
  assert.match(html,/data-step6-threshold="device_failsafe_fact"/);
  assert.match(html,/data-step6-threshold="project_planning_threshold"/);
  assert.match(html,/data-step6-threshold="redundancy_degradation_threshold"/);
  assert.match(html,/尚未登记 ⇒ 判定保持 evidence_required \/ unknown（fail-closed）/);
  assert.match(html,/data-route-protection-parameter="D_maneuver_m"/);
  assert.match(html,/data-engineered-assumption="c_full_outage_max_s"/);
});

// ---------------------------------------------------------------------------
// 8. 命名清理：标题不再带（P17）
// ---------------------------------------------------------------------------

test('连续服务可接受性标题不再带（P17）代号（面板 / Step5 / 结果分段）',()=>{
  const html=renderContinuousServicePanel(p17Flow(),[]);
  assert.doesNotMatch(html,/（P17）/);
  assert.match(html,/连续服务可接受性结论/);
  const step5=renderStep05({flow:p17Flow()});
  assert.doesNotMatch(step5,/连续服务可接受性（P17）/);
  assert.match(step5,/连续服务可接受性/);
  assert.equal(CNS_CANONICAL_CHAIN.at(-1)[0],'cns-res-continuous');
  assert.equal(CNS_CANONICAL_CHAIN.at(-1)[1],'连续服务可接受性');
  //: 内部标识保持不变（只有显示名清理）。
  assert.equal(continuousServiceModel(p17Flow()).result.algorithm_id,
    'continuous_service_acceptability_v1');
});

test('不得在连续服务 UI 里残留任何（P17）显示串',()=>{
  for(const path of ['../cns_planner/web/js/workflow/continuous_service.js',
    '../cns_planner/web/js/workflow/step05_cns.js',
    '../cns_planner/web/js/workflow/step06_review.js',
    '../cns_planner/web/js/workflow/map_legend.js']){
    const source=readFileSync(new URL(path,import.meta.url),'utf8');
    const matches=[...source.matchAll(/'[^'\n]*（P17）[^'\n]*'/g)].map(match=>match[0]);
    assert.deepEqual(matches,[],`${path} 仍有（P17）显示串：${matches.join(' / ')}`);
  }
});

// ---------------------------------------------------------------------------
// 9. 可接受性词表（fail-closed 语义）
// ---------------------------------------------------------------------------

test('可接受性词表绝不把 unknown / limitation 说成通过',()=>{
  assert.match(ACCEPTABILITY_TEXT.unknown.note,/fail-closed/);
  assert.notEqual(ACCEPTABILITY_TEXT.unknown.label,'完全满足');
  assert.match(ACCEPTABILITY_TEXT.unacceptable.label,/不可接受/);
  assert.match(ACCEPTABILITY_TEXT.acceptable_with_managed_gap.note,/不是"全覆盖"/);
});
