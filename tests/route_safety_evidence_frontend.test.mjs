/**
 * Route Safety Evidence V2 / Theta* V2 search-parameter frontend contract tests.
 *
 * 两步边界：
 *   * Step03 的 Theta* V2 面板只转印后端 readiness 的 search parameters 与 provenance，并
 *     通过既有 ``/api/algorithms/select`` 显式改写；前端不推导 parameter_origin，也不把
 *     软件 baseline 说成工程确认参数；
 *   * Step06 的 Route Safety Evidence 面板只转印 ``route_safety_evidence_v2``：四个 domain
 *     分开显示，overall 只表示证据完整性，CNS 缺口只写成 operational support deficit。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  THETA_STAR_V2_ALGORITHM_ID,layeredThetaV2Model,renderLayeredThetaV2Panel,
  thetaV2SearchParametersPayload,
} from '../cns_planner/web/js/workflow/layered_theta_v2.js';
import {
  ROUTE_SAFETY_EVIDENCE_CNS_NOTE,ROUTE_SAFETY_EVIDENCE_DOMAINS,
  ROUTE_SAFETY_EVIDENCE_STATUS_NOTE,render as renderStep6,routeSafetyEvidenceModel,
} from '../cns_planner/web/js/workflow/step06_review.js';

const SEARCH_PARAMETERS={
  applicable:true,
  heading_bin_count:8,
  theta_min_deg:5.0,
  max_expanded_labels:null,
  d_ref_m:123.4,
  d_ref_provenance:'derived_from_current_mh_t_l8_grid_typical_centre_to_centre_step',
  parameter_origin:'software_baseline',
  engineering_confirmed:false,
  software_algorithm_baseline:true,
  fingerprint:'thetasearchv2-abc',
  provenance:{
    source:'cns_planner_software_algorithm_baseline',
    purpose:'search_discretization_and_planning_turn_smoothness_proxy',
    confirmed:false,
    engineering_confirmed:false,
    evidence:null,
    engineering_boundary:{
      software_algorithm_baseline_not_engineering_confirmed:true,
      theta_min_deg_is_not_aircraft_minimum_turn_angle:true,
      d_ref_m_is_not_aircraft_turn_radius:true,
    },
    semantics:{never_auto_upgraded_to_engineering_confirmed:true},
  },
};

function thetaFlow(overrides={}){
  return {
    scenario_routes:[],
    spatial_3d:{altitude_layers:[]},
    layered_route_planning_request:{status:'pending_confirmation'},
    layered_route_feasibility_policy:{status:'pending_confirmation'},
    layered_route_cost_policy:{ground_lambda:null},
    layered_route_candidates:{status:'not_calculated',count:0,items:[]},
    shelter_coefficient_policy:{status:'confirmed',default_coefficient:1.0,source:'user_defined_baseline'},
    population_shelter:{status:'passed',cells:{}},
    theta_v2_objective_policy:{status:'confirmed',risk_weight:0.8,turn_weight:0.1,distance_weight:0.1},
    max_route_risk_density:{status:'confirmed',threshold:1.0},
    regulatory_constraints:{status:'not_configured'},
    communication_planning_field:{status:'not_configured'},
    layered_route_planner_readiness:{
      status:'ready',
      algorithm:{algorithm_id:THETA_STAR_V2_ALGORITHM_ID,algorithm_version:'2.0'},
      theta_star_v2:{
        status:'ready',
        blockers:[],
        semantics:{},
        search_parameters:SEARCH_PARAMETERS,
        population_shelter:{status:'passed'},
        objective:{},
        evaluation_constraint:{},
        regulatory_constraints:{},
        communication:{},
      },
      blockers:[],
    },
    ...overrides,
  };
}

function field(value){return {value:String(value),attributes:{},checked:false};}
function checkbox(checked){return {value:'',attributes:{},checked};}
function closeFrom(nodes){
  return id=>(id in nodes?nodes[id]:null);
}

test('theta v2 search parameters are transcribed verbatim from backend readiness',()=>{
  const model=layeredThetaV2Model(thetaFlow());
  const parameters=model.searchParameters;
  assert.equal(parameters.applicable,true);
  assert.equal(parameters.headingBinCount,8);
  assert.equal(parameters.thetaMinDeg,5.0);
  assert.equal(parameters.maxExpandedLabels,null);
  assert.equal(parameters.parameterOrigin,'software_baseline');
  assert.equal(parameters.engineeringConfirmed,false);
  assert.equal(parameters.softwareAlgorithmBaseline,true);
  assert.equal(parameters.fingerprint,'thetasearchv2-abc');
  assert.equal(parameters.evidence,null);
  assert.equal(parameters.boundary.theta_min_deg_is_not_aircraft_minimum_turn_angle,true);

  const html=renderLayeredThetaV2Panel(thetaFlow());
  assert.match(html,/heading_bin_count/);
  assert.match(html,/theta_min_deg/);
  assert.match(html,/parameter_origin/);
  assert.match(html,/engineering_confirmed/);
  assert.match(html,/software_baseline/);
  assert.match(html,/不是航空器最小转弯角|不等于航空器最小转弯角/);
  assert.match(html,/id="saveThetaV2SearchParameters"/);
  // V1 已不是默认：面板必须标明当前 layered planner 的角色。
  assert.match(html,/production layered planner/);
});

test('a legacy layered planner reports no theta search parameters instead of inventing them',()=>{
  const flow=thetaFlow({
    layered_route_planner_readiness:{
      status:'ready',
      algorithm:{algorithm_id:'layered_route_planner_v1',algorithm_version:'1.0'},
      theta_star_v2:{
        status:'not_ready',blockers:[],semantics:{},
        search_parameters:{applicable:false,reason:'legacy_layered_planner_has_no_theta_search_parameters'},
      },
      blockers:[],
    },
  });
  const model=layeredThetaV2Model(flow);
  assert.equal(model.searchParameters.applicable,false);
  assert.equal(model.searchParameters.headingBinCount,null);
  assert.equal(model.searchParameters.thetaMinDeg,null);
  const html=renderLayeredThetaV2Panel(flow);
  assert.match(html,/legacy_layered_planner_has_no_theta_search_parameters/);
  assert.doesNotMatch(html,/id="saveThetaV2SearchParameters"/);
});

test('the search parameter payload posts the exact algorithm selection shape',()=>{
  const nodes={
    thetaV2HeadingBinCount:field(12),
    thetaV2ThetaMinDeg:field(2.5),
    thetaV2MaxExpandedLabels:field(''),
    thetaV2SearchParamSource:field('engineering_review'),
    thetaV2SearchParamEvidence:field('ENG-2026-001'),
    thetaV2SearchParamConfirmed:checkbox(true),
    thetaV2SearchParamEngineeringConfirmed:checkbox(true),
  };
  const payload=thetaV2SearchParametersPayload({$:closeFrom(nodes)},layeredThetaV2Model(
    thetaFlow()).searchParameters);
  assert.equal(payload.algorithm_type,'layered_route_planner');
  assert.equal(payload.algorithm_id,THETA_STAR_V2_ALGORITHM_ID);
  assert.equal(payload.version,'2.0');
  assert.equal(payload.parameters.heading_bin_count,12);
  assert.equal(payload.parameters.theta_min_deg,2.5);
  assert.equal(payload.parameters.max_expanded_labels,null);
  const provenance=payload.parameters.search_parameter_provenance;
  assert.equal(provenance.source,'engineering_review');
  assert.equal(provenance.confirmed,true);
  assert.equal(provenance.engineering_confirmed,true);
  assert.equal(provenance.evidence.reference,'ENG-2026-001');
  assert.equal(provenance.purpose,'search_discretization_and_planning_turn_smoothness_proxy');
  // 前端不声明 parameter_origin：它由后端按生效值推导。
  assert.equal('parameter_origin' in provenance,false);
});

test('an empty search parameter field is submitted as null and never as the baseline',()=>{
  const nodes={
    thetaV2HeadingBinCount:field(''),
    thetaV2ThetaMinDeg:field(''),
    thetaV2MaxExpandedLabels:field(''),
    thetaV2SearchParamSource:field(''),
    thetaV2SearchParamEvidence:field(''),
    thetaV2SearchParamConfirmed:checkbox(false),
    thetaV2SearchParamEngineeringConfirmed:checkbox(false),
  };
  const payload=thetaV2SearchParametersPayload({$:closeFrom(nodes)},layeredThetaV2Model(
    thetaFlow()).searchParameters);
  assert.equal(payload.parameters.heading_bin_count,null);
  assert.equal(payload.parameters.theta_min_deg,null);
  assert.equal(payload.parameters.max_expanded_labels,null);
  assert.equal(payload.parameters.search_parameter_provenance.evidence,null);
  assert.equal(payload.parameters.search_parameter_provenance.engineering_confirmed,false);
});

// ------------------------------------------------------------------ Step06

function safetyDomain(id,status,extra={}){
  return {domain_id:id,label:id,status,status_reason:null,hard_constraint_failure:false,
    metrics:{},evidence:{},sources:[],limitations:[],...extra};
}

function safetyFlow(overrides={}){
  const domains={
    geometry_obstacle:safetyDomain('geometry_obstacle','validated',{
      metrics:{terrain_status:'passed',terrain_minimum_margin_m:40.0,building_status:'passed',
        building_minimum_margin_m:30.0,fixed_cruise_altitude_m:100.0,
        unresolved_interval_count:0,failed_interval_count:0,resource_limited:false},
      evidence:{validation_status:'validated_candidate'},
      sources:[{role:'terrain_dtm',status:'verified',sha256:'abc'}],
      limitations:['validated_candidate 只表示现有 validator 未发现明确净空违规。'],
    }),
    ground_exposure:safetyDomain('ground_exposure','assessed',{
      metrics:{population_shelter_risk_exposure_index_m:12.5,route_risk_density:0.00625,
        route_risk_density_status:'passed',route_risk_density_threshold:1.0,
        profile_ground_mean_index:0.25,profile_ground_max_index:0.5,
        profile_ground_exposure_index_m:5.0,profile_ground_unresolved_length_m:0.0,
        profile_thresholds:{medium_min:null,high_min:null,status:'not_configured'},
        combined_overall_risk_score:null,combined_overall_not_computed:true},
      evidence:{
        theta_star_planning_objective:{weights:{risk:0.8,turn:0.1,distance:0.1},used_in_search:true},
        route_risk_density:{objective_term:false,value:0.00625},
      },
      sources:[{role:'route_risk_profile_ground_domain',algorithm_id:'route_risk_profile_v1'}],
      limitations:['Theta* objective 与 RouteRiskProfile 是两条独立证据，绝不合成 overall score。'],
    }),
    regulatory:safetyDomain('regulatory','not_configured',{
      metrics:{configured:false,evaluated:false,blocked_constraints:[],unresolved_constraints:[],
        segment_count:1},
      evidence:{dataset_status:'not_configured',display_only_airspace_used:false},
      sources:[{role:'regulatory_constraints',status:'not_configured'}],
      limitations:['未配置任何 regulatory constraint 数据集：not_configured 绝不等于 passed。'],
    }),
    cns_operational_support:safetyDomain('cns_operational_support','operational_support_deficit',{
      metrics:{
        subsystems:[
          {subsystem:'C',operational_support_verdict:'operational_support_deficit',
            coverage:{status:'passed'},capability:{status:'meets_under_model'},
            confirmed_gap:{status:'confirmed_gap',gap_length_m:400.0,continuous_deficit:{max_continuous_gap_length_m:400.0}}},
          {subsystem:'N',operational_support_verdict:'supported_under_model',
            coverage:{status:'passed'},capability:{status:'meets_under_model'},
            confirmed_gap:{status:'satisfied',gap_length_m:0.0}},
          {subsystem:'S',operational_support_verdict:'supported_under_model',
            coverage:{status:'passed'},capability:{status:'meets_under_model'},
            confirmed_gap:{status:'satisfied',gap_length_m:0.0}},
        ],
        operational_support_deficit:true,
        confirmed_gap_subsystems:['C'],
        unknown_subsystems:[],
      },
      limitations:['confirmed CNS gap = operational support deficit，不代表航路不安全。'],
    }),
  };
  return {
    result_statuses:{},
    cns_plan_review:{},
    confirmed_cns_plan:{},
    cns_planning_reports:{},
    required_cns_recommendation:{},
    required_cns_adoption:{},
    cns_corridor_site_plan:{},
    review:{risks:{}},
    coverage:{},
    operational_routes:[],
    aircraft:null,
    rules:null,
    data_health:{status:'passed'},
    route_safety_evidence_v2_readiness:{status:'ready',target:{scope:'current_published_layered_operational_adoption'}},
    route_safety_evidence_v2:{
      status:'passed',count:1,active_assessment_id:'RSE-1',
      items:[{
        assessment_id:'RSE-1',route_id:'R-1',adoption_id:'LRA-1',status:'evidence_complete',
        status_reason:null,current_applicability:'current',created_at:'2026-01-01T00:00:00',
        lineage:{status:'resolved',complete:true,missing_links:[],stale_links:[]},
        domains,
        evidence_summary:{status:'evidence_complete',hard_constraint_domains:[],
          unresolved_domains:[],incomplete_domains:['regulatory'],
          statement:ROUTE_SAFETY_EVIDENCE_STATUS_NOTE,
          is_safety_certification:false,produces_safety_score:false,produces_safety_ranking:false},
        limitations:['不产生 safe/unsafe 结论，也不产生 SORA GRC/ARC。'],
        fingerprints:{assessment_fingerprint:'routesafetyevidencev2-abc',
          operational_route_adoption_fingerprint:'rsa-adoption-1',validation_fingerprint:'validation-fp',
          candidate_fingerprint:'candidate-fp',route_risk_profile_fingerprint:'profile-fp',
          regulatory_dataset_fingerprint:'regulatory-fp',coverage_3d_fingerprint:'coverage-fp',
          cns_service_capability_fingerprint:'capability-fp',cns_gap_v2_fingerprint:'gap-fp',
          cns_corridor_fingerprint:null,evaluator_version:'route_safety_evidence_v2@2.0'},
        provenance:{upstream_modified:false,recomputed_upstream_algorithms:[]},
      }],
    },
    ...overrides,
  };
}

test('step 06 route safety evidence transcribes the backend evidence status verbatim',()=>{
  const model=routeSafetyEvidenceModel(safetyFlow());
  assert.equal(model.status,'evidence_complete');
  assert.equal(model.applicability,'current');
  assert.equal(model.lineage.complete,true);
  assert.deepEqual(model.domains.map(item=>item.id),ROUTE_SAFETY_EVIDENCE_DOMAINS.map(([id])=>id));
  assert.equal(model.domains[0].status,'validated');
  assert.equal(model.domains[3].status,'operational_support_deficit');
  // 前端绝不生成 overall safety score / 排名字段。
  assert.equal('safety_score' in model,false);
  assert.equal('overall_risk' in model,false);

  const html=renderStep6({state:{data_health:{status:'passed'}},flow:safetyFlow()});
  assert.match(html,/航路安全证据/);
  assert.match(html,/证据总体状态/);
  assert.ok(html.includes(ROUTE_SAFETY_EVIDENCE_STATUS_NOTE),
    'the disclaimer must be shown verbatim');
  for(const [id,label] of ROUTE_SAFETY_EVIDENCE_DOMAINS){
    assert.ok(html.includes('data-safety-domain="'+id+'"'),id+' card must be rendered');
    assert.ok(html.includes(label),label+' label must be rendered');
  }
  assert.match(html,/几何与障碍物/);
  assert.match(html,/地面暴露/);
  assert.match(html,/法规约束/);
  assert.match(html,/CNS 运行支持/);
  // 审计指纹 / 来源链路仍然折叠展示，但生产界面不再出现英文开发术语。
  assert.match(html,/高级：审计指纹与来源链路/);
  assert.match(html,/routesafetyevidencev2-abc/);
  assert.match(html,/id="evaluateRouteSafetyEvidenceV2"/);
});

test('the cns confirmed gap is shown as an operational support deficit and never as unsafe',()=>{
  const flow=safetyFlow();
  const html=renderStep6({state:{data_health:{status:'passed'}},flow});
  assert.ok(html.includes(ROUTE_SAFETY_EVIDENCE_CNS_NOTE),
    'the CNS boundary note must be shown verbatim');
  assert.match(html,/operational support deficit/);
  assert.match(html,/operational_support_deficit/);
  // 绝不用红色文案声称 route unsafe，也不声称安全认证。
  assert.doesNotMatch(html,/route unsafe/);
  assert.doesNotMatch(html,/safety_score/);
  assert.doesNotMatch(html,/安全认证通过/);
});

test('a not_ready assessment stays explicit and never claims a current verdict',()=>{
  const flow=safetyFlow({
    route_safety_evidence_v2_readiness:{status:'not_ready',target:{}},
    route_safety_evidence_v2:{status:'not_calculated',count:0,items:[]},
  });
  const model=routeSafetyEvidenceModel(flow);
  assert.equal(model.status,'not_ready');
  assert.equal(model.applicability,'not_evaluated');
  assert.equal(model.count,0);
  assert.equal(model.domains.every(item=>item.status==='not_ready'),true);
  const html=renderStep6({state:{data_health:{status:'passed'}},flow});
  assert.match(html,/航路安全证据/);
  assert.match(html,/尚未评估/);
});

test('step 06 posts exactly one explicit evaluation and never a background one',async()=>{
  const registered=[];
  const calls=[];
  const previousDocument=globalThis.document;
  globalThis.document={querySelectorAll:()=>[]};
  const c={
    flow:()=>safetyFlow(),
    mutate:()=>{},
    resourceAction:(path,payload)=>{calls.push([path,payload]);},
    previewPlanningReport:()=>{},
    downloadPlanningReport:()=>{},
    $:()=>null,
    actionButton:(id,handler)=>{registered.push([id,handler]);},
  };
  try{
    const {bind}=await import('../cns_planner/web/js/workflow/step06_review.js');
    bind(c);
    const ids=registered.map(([id])=>id);
    assert.equal(ids.filter(id=>id==='evaluateRouteSafetyEvidenceV2').length,1);
    const handler=registered.find(([id])=>id==='evaluateRouteSafetyEvidenceV2')[1];
    handler();
    assert.deepEqual(calls,[['/api/route-safety-evidence-v2/evaluate',{}]]);
  }finally{
    if(previousDocument===undefined)delete globalThis.document;
    else globalThis.document=previousDocument;
  }
});
