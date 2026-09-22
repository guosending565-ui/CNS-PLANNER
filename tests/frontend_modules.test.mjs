import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {lonLatToMercator,mercatorToLonLat} from '../cns_planner/web/js/map/projection.js';
import {createStore} from '../cns_planner/web/js/state/store.js';
import {buildGridOverlayCache,findGridCell} from '../cns_planner/web/js/map/grid_overlay.js';
import {drawGridTheme} from '../cns_planner/web/js/map/renderer.js';
import {referenceLayerDiagnostics} from '../cns_planner/web/js/map/reference_overlay.js';
import {algorithmManifestDetails,algorithmSelectionKey} from '../cns_planner/web/js/workflow/step01_project.js';
import {render as renderStep4,withLegacyRequiredAliases,requirementRecommendationSummary} from '../cns_planner/web/js/workflow/step04_operation.js';
import {render as renderStep2} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {bindLayeredCandidatePanel,filterReferenceSites,layeredCandidatePanel,referenceOverlayModel,render as renderStep3,riskAwareRoutePanel,plannerCardModel,routePlannerComparisonModel,effectiveParameters,findAlgorithmManifest,routeExperimentModel,routePlanningDiagnosticsModel,referenceLinkModel,routePlannerV3Model,routePlannerV3ReadinessModel,routePlannerV3Panel,routePlannerV3ValidationPanel,routePlannerV3ContinuousModel,routePlannerV3ValidationModel,routePlannerV3AdoptionPanel,routePlannerV3AdoptionModel,v3dExpectedFingerprint,V3_RESULT_STATUSES,V3B_RESULT_STATUSES,V3B_REFINED_LABEL,V3C_RESULT_STATUSES,V3C_DOMAINS,V3C_EVIDENCE_SOURCES,V3C_OPERATIONAL_LABEL,V3D_ADOPTION_STATUSES,V3D_ASSESSMENT_STATUSES,V3D_REQUIREMENT_VERDICTS,V3D_STAGES,V3D_DOWNSTREAM_RESULTS,V3D_PUBLISH_LABEL,V3D_SYNTHETIC_LABEL,V3D_CNS_SEPARATION_LABEL} from '../cns_planner/web/js/workflow/step03_routes.js';
import {routePlannerV3CnsSummary} from '../cns_planner/web/js/workflow/step04_operation.js';
import {v3OverlayModel} from '../cns_planner/web/js/map/route_planner_v3_overlay.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6,planReviewSummary} from '../cns_planner/web/js/workflow/step06_review.js';
import {sourceModeText,statusText} from '../cns_planner/web/js/workflow/common.js';
import {profileChart,renderRouteVerticalProfilePanel} from '../cns_planner/web/js/workflow/route_vertical_profile.js';
import {ADVANCED_PROFILE_LABEL,CRUISE_LAYER_MODE,LAYER_PENDING_LABEL,PRODUCTION_ROUTE_LABEL,READINESS_BUCKETS,routeOperatingModel,renderCruiseLayerPanel} from '../cns_planner/web/js/workflow/route_operating_layer.js';
import {protectionBudgetModel,renderProtectionBudget} from '../cns_planner/web/js/workflow/protection_budget.js';
import {encounterFrame,renderDaaEncounterLab} from '../cns_planner/web/js/workflow/daa_encounter_lab.js';
import {LEGACY_RISK_V1_LABEL,renderRiskFrameworkV2Panel,riskFrameworkV2Model,riskV2CellSummary,riskV2LegendModel,riskV2ThemeOptions} from '../cns_planner/web/js/workflow/risk_framework_v2.js';
import {COST_DOMAIN_LABELS,LAYERED_BLOCKED_NOTE,LAYERED_CANDIDATE_LABEL,LAYERED_PLANNER_ALGORITHM_TYPE,layeredEvaluatePayload,layeredOverlayModel,layeredPlannerUsesThetaStarV2,layeredPlanningRequestModel,layeredRequestPayload,layeredRoutePlannerModel,renderLayeredRoutePlannerPanel} from '../cns_planner/web/js/workflow/layered_route_planner.js';
import {THETA_STAR_V2_ALGORITHM_ID,THETA_STAR_V2_PANEL_TITLE,THETA_V2_EMPTY_ALTITUDE_CATALOG_NOTE,THETA_V2_EVALUATE_BLOCKED_NOTE,bindLayeredThetaV2,isLegacyV1CostBlocker,layeredThetaV2Model,renderLayeredThetaV2Panel,thetaV2ObjectivePolicyPayload,thetaV2RiskDensityPayload,thetaV2ShelterPolicyPayload} from '../cns_planner/web/js/workflow/layered_theta_v2.js';
import {LAYERED_FEASIBILITY_COLORS,currentLayeredCandidate,drawLayeredFeasibilityOverlay,layeredFeasibilityCells,layeredFeasibilityLegend} from '../cns_planner/web/js/map/layered_feasibility_overlay.js';
import {drawWorkflowLayers} from '../cns_planner/web/js/map/display_layers.js';
import {drawLayeredCandidateOverlay} from '../cns_planner/web/js/map/layered_candidate_overlay.js';
import {ROUTE_STYLES} from '../cns_planner/web/js/map/lod.js';
import {createApiClient} from '../cns_planner/web/js/api/client.js';

test('projection round trips WGS84 coordinates',()=>{
  const original=[120.1234,30.5678],restored=mercatorToLonLat(...lonLatToMercator(...original));
  assert.ok(Math.abs(restored[0]-original[0])<1e-8);
  assert.ok(Math.abs(restored[1]-original[1])<1e-8);
});

test('store merges explicit state updates',()=>{
  const store=createStore({server:null,ui:{step:1}});
  store.set({server:{revision:2}});
  assert.deepEqual(store.get(),{server:{revision:2},ui:{step:1}});
});

test('api client serializes writes and advances workflow revision headers',async()=>{
  const originalFetch=globalThis.fetch,calls=[];
  let active=0,maxActive=0,serverRevision=4;
  globalThis.fetch=async(url,options)=>{
    active++;maxActive=Math.max(maxActive,active);
    calls.push({url,headers:options.headers});
    await new Promise(resolve=>setTimeout(resolve,5));
    serverRevision++;active--;
    return {
      ok:true,status:200,
      headers:{get:name=>name.toLowerCase()==='content-type'?'application/json':(name.toLowerCase()==='x-cns-revision'?String(serverRevision):null)},
      json:async()=>({ok:true}),blob:async()=>null,
    };
  };
  try{
    const api=createApiClient(()=> 'token',()=>4);
    await Promise.all([
      api('/first',{method:'POST',body:'{}'}),
      api('/second',{method:'POST',body:'{}'}),
    ]);
    assert.equal(maxActive,1);
    assert.equal(calls[0].headers['X-CNS-Revision'],'4');
    assert.equal(calls[1].headers['X-CNS-Revision'],'5');
    assert.match(calls[0].headers['X-CNS-Request-Id'],/:1$/);
    assert.match(calls[1].headers['X-CNS-Request-Id'],/:2$/);
  }finally{globalThis.fetch=originalFetch;}
});

test('grid cache joins attributes by grid_id and uses half-open hit boundaries',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{status:'passed',cells:{A:{status:'passed',value_mean:5},B:{status:'passed',value_mean:10}}},terrain:{cells:{}}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:(bbox,x,y)=>x>=bbox[0]&&x<bbox[2]&&y>=bbox[1]&&y<bbox[3]};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.equal(cache.byId.get('A').population.value_mean,5);
  assert.equal(findGridCell(cache,121,30.5,theme).cell.grid_id,'B');
});

test('population theme prefers governed target-grid density and retains partial values',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{status:'missing_data',cells:{
    A:{status:'passed',quantity_status:'passed',population_density_people_km2:25,value_mean:999},
    B:{status:'passed',value_status:'passed',quantity_status:'missing_data',coverage_status:'partial',population_density_people_km2:50,value_mean:888}
  }},terrain:{cells:{}}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.deepEqual(cache.populationBreaks,[25,50]);
});

test('population renderer uses the same color scale and a distinct partial outline',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]}]},attributes={population:{status:'passed',cells:{A:{value_status:'passed',coverage_status:'partial',population_density_people_km2:25}}}};
  const theme={bboxIntersects:()=>true,colorForValue:()=> '#abc',NO_DATA_COLOR:'#gray'},cache=buildGridOverlayCache(grid,attributes,{}, {quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true});
  const calls=[],ctx={save(){},restore(){},fillRect(){calls.push(['fill',this.fillStyle,this.globalAlpha])},strokeRect(){calls.push(['stroke',this.strokeStyle])},setLineDash(){}};
  drawGridTheme({ctx,view:{},flow:{grid_attributes:attributes},cache,display:{theme:'population'},visibleBounds:()=>[0,0,180,90],screenPoint:p=>p,gridTheme:theme,palettes:{population:['#abc'],terrain:[],buildings:[],risk:[]},riskBreaks:[]});
  assert.deepEqual(calls[0],['fill','#abc',.55]);
  assert.deepEqual(calls[1],['stroke','#714f86']);
});

test('building grid cache preserves zero versus missing and exposes three theme breaks',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{cells:{}},terrain:{cells:{}},buildings:{status:'missing_data',cells:{
    A:{status:'passed',building_coverage_ratio:0,height_p95_m:null,height_max_m:null},
    B:{status:'missing_data',building_coverage_ratio:null,height_p95_m:null,height_max_m:null}
  }}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.deepEqual(cache.buildingCoverageBreaks,[0]);
  assert.equal(cache.byId.get('A').buildings.status,'passed');
  assert.equal(cache.byId.get('B').buildings.status,'missing_data');
});

test('algorithm selection key includes type id and exact version',()=>{
  assert.equal(algorithmSelectionKey({algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1'}),'risk_model|risk-model-v1-relative-index|1.1');
});

test('step 1 algorithm details preserve auditable manifest fields',()=>{
  const item={algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1',name:'Risk V1',provider:'CNS-PLANNER',maturity:'baseline',description:'relative',inputs:['grid'],outputs:['risk'],parameter_schema:{type:'object'},assumptions:['a'],limitations:['b'],references:[]};
  assert.deepEqual(algorithmManifestDetails(item),{identity:'risk-model-v1-relative-index@1.1',provider:'CNS-PLANNER',maturity:'baseline',inputs:'grid',outputs:'risk',assumptions:'a',limitations:'b',references:'未登记'});
});

test('P17 recommendation summary preserves pending/conflict/divergence semantics',()=>{
  assert.deepEqual(requirementRecommendationSummary({status:'conflict',matched_policies:[{}],unknown_policies:[{}],conflicts:[{}],current_vs_recommended_diff:[{},{}],current_required_cns_diverged:true}),{status:'conflict',matched:1,unknown:1,conflicts:1,changes:2,diverged:true});
});

test('step 3 exposes V2 risk parameters without changing the V1 panel',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  assert.equal(riskAwareRoutePanel({algorithm_selection:{route_planner:{algorithm_id:'route_planner_v1',version:'1.0'}}}), '');
  const html=riskAwareRoutePanel({algorithm_selection:{route_planner:{algorithm_id:'risk_aware_route_planner_v2',version:'2.0',parameters:{risk_weight_lambda:3,risk_component:'ground',unknown_risk_policy:'penalize',unknown_penalty_index:.8}}}});
  assert.match(html,/Risk-Aware Route Planner V2/);
  assert.match(html,/Risk weight λ/);
  assert.match(html,/Ground/);
  assert.match(html,/Penalize/);
  assert.match(html,/不是事故概率、SORA GRC 或 TLS/);
});

test('reference landing sites stay separate and support workspace search filters',()=>{
  const sites=[
    {reference_site_id:'A',name:'港务码头',region:'定海区',site_type:'起降点',coordinate:[122.1,30],quality:'parsed'},
    {reference_site_id:'B',name:'岛外点',region:'普陀区',site_type:'临时起降点',coordinate:[123,30],quality:'parsed'},
    {reference_site_id:'C',name:'无效点',region:'定海区',site_type:'起降点',coordinate:null,quality:'invalid'},
  ];
  assert.deepEqual(filterReferenceSites(sites,{workspace:{bbox:[122,29.9,122.2,30.1]},search:'码头',region:'定海区',siteType:'起降点'}).map(item=>item.reference_site_id),['A']);
});

test('step 3 overlay keeps reference route points scenario and operational routes separate with independent toggles',()=>{
  const flow={workspace:{bbox:[122,29,123,31]},reference_routes:{items:[{reference_route_id:'RR1',path:[[122.1,30],[122.3,30.2]]}],points:[{reference_route_point_id:'RP1',coordinate:[122.2,30.1]}]},reference_landing_sites:{items:[{reference_site_id:'LS1',coordinate:[122.4,30.2],quality:'parsed'}]},scenario_routes:[{route_id:'S1'}],operational_routes:[{route_id:'O1'}]};
  const all=referenceOverlayModel(flow,{routes:true,points:true,landingSites:true});
  assert.equal(all.referenceRoutes.length,1);
  assert.equal(all.referencePoints.length,1);
  assert.equal(all.referenceLandingSites.length,1);
  assert.deepEqual(all.scenarioRoutes,[{route_id:'S1'}]);
  assert.deepEqual(all.operationalRoutes,[{route_id:'O1'}]);
  const hidden=referenceOverlayModel(flow,{routes:false,points:false,landingSites:false});
  assert.equal(hidden.referenceRoutes.length,0);
  assert.equal(hidden.referencePoints.length,0);
  assert.equal(hidden.referenceLandingSites.length,0);
  assert.equal(hidden.scenarioRoutes.length,1);
  assert.equal(hidden.operationalRoutes.length,1);
});

test('reference layer diagnostics expose counts and ET conversion reason',()=>{
  const result=referenceLayerDiagnostics({reference_routes:{status:'requires_xlsx_or_csv_conversion',count:0,point_count:0},reference_landing_sites:{status:'passed',count:12}});
  assert.equal(result.routes.count,0);
  assert.match(result.routes.label,/ET 需先转换/);
  assert.match(result.points.label,/航路点 0/);
  assert.match(result.landingSites.label,/起降点 12/);
});

test('reference drawing is global while Step03 alone keeps hit interaction',()=>{
  const main=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
  const drawBlock=main.slice(main.indexOf('function drawWorkflowOverlay'),main.indexOf('function proposedPlanActions'));
  assert.match(drawBlock,/drawReferenceOverlay/);
  assert.doesNotMatch(drawBlock,/if\(currentStep===3\)/);
  assert.match(main,/if\(currentStep===3\)[\s\S]*hitReferenceObject/);
});

test('map exposes display-only source airspace and reference layer toggles',()=>{
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  for(const id of ['referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','referenceRouteStatus','referenceRoutePointStatus','referenceLandingStatus','reference_landing_sitesPath','reference_routesPath','buildingClearanceLayer','terrain_dtmPath'])assert.match(html,new RegExp('id="'+id+'"'));
  assert.doesNotMatch(html,/id="allowedAirspaceLayer"/);
  assert.match(html,/空域参考图层（仅显示，不参与路线约束）/);
  assert.match(html,/不代表已确认 WGS84/);
});

test('step 4 canonical seconds create exact V1 aliases',()=>{
  const result=withLegacyRequiredAliases({
    communication:{performance:{max_latency_s:0.25,min_redundancy:2}},
    navigation:{performance:{max_horizontal_error_m:3,integrity_required:'required',min_redundancy:1}},
    surveillance:{performance:{max_update_interval_s:2,min_redundancy:1}},
  });
  assert.equal(result.communication.latency_ms,250);
  assert.equal(result.navigation.accuracy_m,3);
  assert.equal(result.navigation.integrity,'required');
  assert.equal(result.surveillance.update_interval_s,2);
});

test('route vertical profile always refreshes every route and never fakes a per-route filter',()=>{
  const panel=renderRouteVerticalProfilePanel({status:'passed',profiles:[{route_id:'R1',status:'passed',samples:[],route_length_m:10}]},[{route_id:'R1',status:'passed'}]);
  assert.match(panel,/刷新全部剖面（全量评估）/);
  assert.match(panel,/当前全部运行航路/);
  assert.match(panel,/只切换下方图表的显示对象/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/route_vertical_profile.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/evaluate',\{route_id/);
  assert.match(source,/data-profile-view-selector/);
});

test('step 3 planner card is driven by algorithm_catalog without hardcoded limits',()=>{
  const manifest={algorithm_type:'route_planner',algorithm_id:'route_planner_v1',version:'1.0',name:'Route Planner V1',provider:'CNS-PLANNER',maturity:'engineering_baseline',description:'确定性 A*',inputs:['scenario_route'],outputs:['operational_route'],parameter_schema:{type:'object',properties:{grid_size:{type:'integer',minimum:2,default:56}}},assumptions:['经纬度工作区离散为规则网格'],limitations:['BBOX 硬约束','非米制搜索'],references:[]};
  const flow={algorithm_selection:{route_planner:{algorithm_type:'route_planner',algorithm_id:'route_planner_v1',version:'1.0',parameters:{}}},algorithm_catalog:[manifest],nodes:[],scenario_routes:[],operational_routes:[]};
  const model=plannerCardModel(flow);
  assert.equal(model.status,'passed');
  assert.equal(model.manifest.algorithm_id,'route_planner_v1');
  assert.deepEqual(model.effective_parameters,[{name:'grid_size',value:56,source:'schema_default',declared:true,minimum:2,maximum:undefined}]);
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/当前规划器/);
  assert.match(html,/algorithm_catalog \/ Manifest/);
  assert.match(html,/BBOX 硬约束/);
  assert.match(html,/有效参数/);
  assert.match(html,/来自 schema 默认值/);
});

test('step 3 planner card reports a missing manifest instead of inventing limits',()=>{
  const flow={algorithm_selection:{route_planner:{algorithm_id:'route_planner_v1',version:'9.9',parameters:{}}},algorithm_catalog:[],nodes:[],scenario_routes:[],operational_routes:[]};
  const model=plannerCardModel(flow);
  assert.equal(model.status,'manifest_missing');
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/没有精确匹配的 Manifest/);
  assert.doesNotMatch(html,/BBOX 硬约束/);
});

test('effective parameters prefer the live selection over schema defaults',()=>{
  const manifest={parameter_schema:{type:'object',properties:{risk_weight_lambda:{type:'number',default:0},risk_component:{enum:['overall','ground','air']},unknown_penalty_index:{type:['number','null']}}}};
  const params=effectiveParameters(manifest,{parameters:{risk_weight_lambda:4.5,risk_component:'ground',unknown_penalty_index:null}});
  assert.deepEqual(params.map(item=>[item.name,item.value,item.source]),[['risk_component','ground','selection'],['risk_weight_lambda',4.5,'selection']]);
  assert.equal(findAlgorithmManifest([manifest],'route_planner','x','1.0'),null);
});

test('step 3 side by side view never declares a winner',()=>{
  const flow={scenario_routes:[{route_id:'R0001',direction:'N001→N002'}],operational_routes:[{route_id:'R0001',algorithm_id:'risk_aware_route_planner_v2',status:'passed',path:[[0,0],[0.001,0]],distance_m:111.2,risk_exposure_index_m:5.5,max_risk_index:0.2}],reference_routes:{items:[]},nodes:[{node_id:'N001',name:'A',coordinate:[0,0]},{node_id:'N002',name:'B',coordinate:[0.001,0]}],algorithm_selection:{route_planner:{algorithm_id:'risk_aware_route_planner_v2',version:'2.0',parameters:{}}},algorithm_catalog:[]};
  const model=routePlannerComparisonModel(flow);
  assert.equal(model.hasV2,true);
  assert.equal(model.bothPresent,false);
  assert.equal(model.automatic_ranking,false);
  assert.equal(model.semantics,'factual_side_by_side_no_superiority_conclusion');
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/V1 \/ V2 结果并列/);
  assert.match(html,/不作优劣结论/);
  assert.doesNotMatch(html,/>更好</);
});

test('step 3 explicit OD panel creates a single pair and keeps the legacy generator',()=>{
  const flow={nodes:[{node_id:'N001',name:'A',coordinate:[0,0]},{node_id:'N002',name:'B',coordinate:[0.001,0]},{node_id:'N003',name:'C',coordinate:[0,0.001]}],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{algorithm_id:'route_planner_v1',version:'1.0',parameters:{}}},algorithm_catalog:[],retired_route_ids:[],risks:{environment:{status:'not_calculated'}},steps:{'3':false},spatial_3d:{route_altitude_profiles:{}},operational_timing:{route_motion_profiles:{}},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[],points:[]},reference_landing_sites:{items:[]},workspace:{bbox:[0,0,0.1,0.1]}};
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/起点 → 终点 创建航路/);
  assert.match(html,/id="odStartNode"/);
  assert.match(html,/id="odEndNode"/);
  assert.match(html,/id="createOdRoute"/);
  assert.match(html,/不会因为参考点数量自动生成全连接/);
  assert.match(html,/生成场景航路（all-pairs，兼容）/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
  assert.match(source,/mutate\('scenario-od'/);
});

test('step 3 experiment panel shows both planners without pretending to be the operational route',()=>{
  const experiment={experiment_id:'EXP-0123456789AB',created_at:'2026-01-01T00:00:00Z',grounding:'current_scenario_routes',source_type:'project',current_applicability:'current',scenario_fingerprint:'abcdef0123456789',verdicts:{automatically_ranked:false},
    runs:[
      {run_id:'route_planner_v1:1.0#1',algorithm_id:'route_planner_v1',algorithm_version:'1.0',status:'passed',planner_invoked:true,effective_parameters:{},result:{},quality:{quality:{path_length_m:1200.5,segment_count:4,turn_count:3,detour_factor:1.05},risk_metrics:{},deterministic_consistency:true},runtime:{per_route_ms_stats:{median:1.5}}},
      {run_id:'risk_aware_route_planner_v2:2.0#2',algorithm_id:'risk_aware_route_planner_v2',algorithm_version:'2.0',status:'missing_data',planner_invoked:true,effective_parameters:{risk_weight_lambda:0},reason:'没有 confirmed allowed airspace',result:{},quality:{quality:{path_length_m:0,segment_count:0,turn_count:0},risk_metrics:{},deterministic_consistency:true},runtime:{per_route_ms_stats:{median:5.0}}},
    ]};
  const flow={nodes:[{node_id:'N001',name:'A',coordinate:[122,30]},{node_id:'N002',name:'B',coordinate:[122.1,30.1]}],scenario_routes:[{route_id:'R0001',direction:'N001→N002'}],operational_routes:[{route_id:'R0001',status:'passed'}],algorithm_selection:{route_planner:{algorithm_id:'route_planner_v1',version:'1.0',parameters:{}}},algorithm_catalog:[],retired_route_ids:[],risks:{environment:{status:'not_calculated'}},steps:{},spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[],points:[]},reference_landing_sites:{items:[]},workspace:{bbox:[122,29.9,122.2,30.1]},route_planning_experiments:{count:1,active_experiment_id:'EXP-0123456789AB',records:[experiment],active_experiment:experiment},reference_route_links:{items:[]},reference_endpoint_candidates:{status:'blocked',reason:'source_crs_pending_confirmation'},data_readiness:{status:'partial',blocks:{}}};
  const model=routeExperimentModel(flow);
  assert.equal(model.count,1);
  assert.equal(model.hasBothPlanners,true);
  assert.equal(model.semantics,'experiment_is_not_current_operational_route');
  assert.equal(model.automatic_ranking,false);
  assert.equal(model.runs.length,2);
  assert.equal(model.runs[0].path_length_m,1200.5);
  assert.equal(model.runs[1].status,'missing_data');
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/规划器比较实验/);
  assert.match(html,/experiment ≠ current operational route/);
  assert.match(html,/不切换当前 planner/);
  assert.match(html,/不覆盖 operational_routes/);
  assert.match(html,/运行 V1 \+ V2 比较实验/);
  assert.match(html,/risk_aware_route_planner_v2/);
  assert.match(html,/没有 confirmed allowed airspace/);
});

test('step 3 route diagnostics show current and experiment evidence without ranking',()=>{
  const evaluation={route_id:'R1',status:'passed',algorithm_id:'route_planner_v1',quality:{vertex_count:3,segment_count:2,turn_count:1,total_heading_change_deg:45,max_heading_change_deg:45,min_segment_m:100,zigzag_index:.25},grid_behavior:{grid_level:7,horizontal_step_count:1,vertical_step_count:0,diagonal_step_count:1,direction_histogram:{E:1,NE:1}},risk_metrics:{},constraint_input_summary:{hard_constraint_count:2,allowed_airspace_status:'passed'},runtime_ms:3};
  const flow={route_planning_diagnostics:{status:'passed',planner:{algorithm_id:'route_planner_v1',version:'1.0',manifest_limitations:['BBOX 硬约束']},routes:[evaluation]},route_planning_experiments:{records:[{experiment_id:'EXP-0123456789AB',runs:[{algorithm_id:'route_planner_v1',quality:evaluation,runtime:{per_route_ms_stats:{median:4}}}]}]}};
  const model=routePlanningDiagnosticsModel(flow);
  assert.equal(model.current.length,1);
  assert.equal(model.experiments.length,1);
  assert.equal(model.current[0].zigzag_index,.25);
  assert.equal(model.automatic_ranking,false);
  const html=renderStep3({flow:{...flow,nodes:[],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},reference_landing_sites:{items:[]},reference_route_links:{},reference_endpoint_candidates:{},workspace:{bbox:[0,0,.1,.1]},risks:{},steps:{}},interactionMode:'pan'});
  assert.match(html,/航路规划诊断/);
  assert.match(html,/zigzag/);
  assert.match(html,/BBOX 硬约束/);
  assert.match(html,/不评分、不排名、不推荐算法/);
});

test('step 3 reference link panel requires explicit confirmation and blocks candidates without CRS',()=>{
  const flow={reference_routes:{items:[{reference_route_id:'RLR-1',name:'参考线',crs:{source_crs:{status:'pending_confirmation'}}}]},scenario_routes:[{route_id:'R0001',direction:'N001→N002'}],reference_route_links:{items:[]},reference_endpoint_candidates:{status:'blocked',reason:'source_crs_pending_confirmation',candidate_count:0},data_readiness:{status:'partial',blocks:{reference_routes:{source_crs_resolved:false}}}};
  const model=referenceLinkModel(flow);
  assert.equal(model.linkCount,0);
  assert.equal(model.candidateStatus,'blocked');
  assert.equal(model.requiresUserConfirmation,true);
  assert.equal(model.automaticAssociation,false);
  const html=renderStep3({flow:{...flow,nodes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_landing_sites:{items:[]},workspace:{bbox:[122,29.9,122.2,30.1]},route_planning_experiments:{},risks:{},steps:{}},interactionMode:'pan',selectedReference:null});
  assert.match(html,/参考航线 ↔ 当前 OD 关联/);
  assert.match(html,/必须由用户显式确认/);
  assert.match(html,/不得自动认定/);
  assert.match(html,/source_crs_pending_confirmation/);
});

test('step 3 has no AirspacePolicy editor and never infers eligibility',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/airspacePolicyReadinessModel|airspacePolicyEditorModel|airspacePolicyPanel/);
  assert.doesNotMatch(source,/data-save-airspace-policy|data-airspace-policy-value|saveAirspacePolicyBatch/);
  const flow={airspace_policies:{items:[{feature_id:'A',route_eligibility:'allowed',confirmed:true,source:{type:'doc'}},{feature_id:'B',route_eligibility:'unknown',confirmed:false,source:{type:'doc'}}]},data_readiness:{status:'partial',blocks:{airspace_policies:{status:'passed',count:2,route_eligibility_counts:{allowed:1,blocked:0,unknown:1},confirmed_count:1,unconfirmed_count:1,v2_readiness:{status:'blocked',reason:'only_unknown_policies_present'},never_inferred_from_layer_name_or_color:true}}}};
  const html=renderStep3({flow:{...flow,nodes:[],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},reference_landing_sites:{items:[]},route_planning_experiments:{},reference_route_links:{},reference_endpoint_candidates:{},workspace:{bbox:[122,29.9,122.2,30.1]},risks:{},steps:{}},interactionMode:'pan',selectedReference:null});
  assert.doesNotMatch(html,/AirspacePolicy 就绪总览/);
  assert.doesNotMatch(html,/saveAirspacePolicyBatch/);
  // No confirmed-allowed count and no V2 airspace readiness warning may be shown.
  assert.doesNotMatch(html,/confirmed_count|confirmed allowed|V2 readiness/);
});

test('the confirmed-allowed airspace overlay module is gone',()=>{
  assert.throws(()=>readFileSync(new URL('../cns_planner/web/js/map/airspace_policy_overlay.js',import.meta.url),'utf8'),/ENOENT/);
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  // The raw airspace reference layer toggle stays; the confirmed-allowed overlay does not.
  assert.match(html,/id="air"/);
  assert.doesNotMatch(html,/confirmed allowed/i);
  assert.doesNotMatch(html,/allowedAirspaceLayer/);
  // Source Center keeps only verify-source / confirm-CRS / preview-import actions.
  const center=readFileSync(new URL('../cns_planner/web/js/sources/source_center.js',import.meta.url),'utf8');
  assert.match(center,/data-verify-source/);
  assert.doesNotMatch(center,/airspace/i);
});

test('step 3 data readiness panel reports reference CRS and ET policy',()=>{
  const flow={nodes:[],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},reference_landing_sites:{items:[]},route_planning_experiments:{},reference_route_links:{},reference_endpoint_candidates:{},airspace_policies:{items:[]},workspace:{bbox:[122,29.9,122.2,30.1]},risks:{},steps:{},
    data_readiness:{status:'partial',reference_route_link_count:2,experiment_count:1,et_source_policy:'requires_xlsx_or_csv_conversion',blocks:{reference_landing_sites:{label:'landing_site',status:'not_calculated',count:0,source_crs:{value:null,status:'pending_confirmation'},representation_crs:{},source_crs_resolved:false,metric_measurement_status:'disabled_unresolved_source_crs'},reference_routes:{label:'reference_route',status:'passed',count:3,format:'csv',source_crs:{value:null,status:'pending_confirmation'},representation_crs:{},source_crs_resolved:false,metric_measurement_status:'disabled_unresolved_source_crs'},airspace_policies:{status:'pending_confirmation',count:0,route_eligibility_counts:{allowed:0,blocked:0,unknown:0},confirmed_count:0,unconfirmed_count:0,v2_readiness:{status:'blocked',reason:'no_confirmed_allowed_airspace_policy'}}}}};
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/数据就绪/);
  assert.match(html,/source_crs/);
  assert.match(html,/representation_crs/);
  assert.match(html,/disabled_unresolved_source_crs/);
  assert.match(html,/ET 仍要求人工转换/);
  assert.match(html,/reference route link 2/);
});

test('route vertical profile renders FABDEM flight building evidence and hover contract',()=>{
  const profile={route_id:'R1',status:'breach',vertical_reference:'egm2008_orthometric',route_length_m:100,sampling:{sample_count:2,actual_spacing_m:100},required_vertical_clearance_m:20,samples:[{distance_m:0,coordinate:[122,30],ground_egm2008_m:10,flight_egm2008_m:80,flight_agl_m:70,ground_clearance_m:70,status:'passed'},{distance_m:100,coordinate:[122.1,30],ground_egm2008_m:20,flight_egm2008_m:80,flight_agl_m:60,ground_clearance_m:60,status:'passed'}],building_intervals:[{building_id:'B1',start_distance_m:30,end_distance_m:50,roof_elevation_m:70,status:'breach'}],breach_intervals:[{}]};
  const svg=profileChart(profile),panel=renderRouteVerticalProfilePanel({status:'breach',profiles:[profile]},[{route_id:'R1',status:'passed'}]);
  assert.match(svg,/data-profile-svg/);
  assert.match(svg,/ground_egm2008_m|#6b5b3e/);
  assert.match(svg,/B1/);
  assert.match(panel,/采样不参与安全判定/);
  assert.match(panel,/悬停曲线/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/route_vertical_profile.js',import.meta.url),'utf8');
  assert.match(source,/onpointermove/);
  assert.match(source,/setProfileHover/);
  assert.match(readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8'),/profileHoverCoordinate/);
});

test('protection budget shows all components and unknown without map geometry',()=>{
  const components=Object.fromEntries(['detect','track','processing','decision','communication','aircraft_reaction'].map((name,index)=>[name,{value_s:index+1}]));
  const passed={status:'passed',components,t_pre_s:21,d_reaction_m:210,maneuver_distance_m:40,uncertainty_distance_m:10,d_protect_m:260};
  const model=protectionBudgetModel(passed),html=renderProtectionBudget(passed);
  assert.equal(model.parts.length,6);
  assert.match(html,/t_pre 21.00 s/);
  assert.match(html,/d_protect 260.00 m/);
  assert.match(html,/regulatory well-clear not evaluated/);
  assert.match(renderProtectionBudget({status:'unknown',components:{},reasons:['missing']}),/missing/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/protection_budget.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/drawLine|\.arc\(/);
});

test('step 4 separates aircraft capability and required performance UI',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}});return node;}};
  const html=renderStep4({flow:{aircraft_profiles:{count:1,items:[{aircraft_id:'A',name:'A',communication:{capabilities:['radio'],type:{technology:'4g'},confirmed:true},navigation:{},surveillance:{}}]},selected_aircraft_profile_id:'A',aircraft_source:'catalog',scenario_routes:[],required_cns:{project_default:{}},device_catalog:{items:[]},steps:{'4':false}}});
  assert.match(html,/Aircraft Capability/);
  assert.match(html,/Required CNS Performance/);
  assert.match(html,/Operation Context → RequiredCNS Recommendation/);
  assert.match(html,/Requirement Policies JSON/);
  assert.match(html,/最大时延 s/);
  assert.match(html,/Ground Device Capability/);
  assert.match(html,/ReliabilitySpec/);
  assert.match(html,/Safety Assessment Policy/);
  assert.match(html,/ARP4761A\/FAA-inspired engineering assessment/);
  assert.match(html,/不是认证结论/);
  assert.match(html,/Functional Coupling/);
  assert.match(html,/Coupled Event Preview/);
  assert.match(html,/不计算耦合概率/);
  assert.match(html,/Operational Timing & Service Scenario/);
  assert.match(html,/Response Time Budget/);
  assert.match(html,/Encounter Scenario/);
  assert.match(html,/P8 静态 capability 不会自动转为 P4 available/);
  assert.match(html,/CNS Service Requirement Corridor/);
  assert.match(html,/DAA Encounter Lab/);
  assert.match(html,/regulatory well-clear not evaluated/);
  assert.match(html,/ServiceState ≠ EncounterEvent ≠ SafetyEvent ≠ UnacceptableEvent/);
  assert.match(html,/不等同 JARUS Operational Volume/);
  assert.doesNotMatch(html,/最大时延 ms/);
});

test('DAA lab renders tracks CPA timeline and playback frame without hazard geometry',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}});return node;}};
  const own={track_id:'OWN',role:'ownship',samples:[{time_s:0,lon:0,lat:0,altitude_egm2008_m:100},{time_s:10,lon:.01,lat:0,altitude_egm2008_m:100}]};
  const intruder={track_id:'INT',role:'intruder',samples:[{time_s:0,lon:.005,lat:-.005,altitude_egm2008_m:110},{time_s:10,lon:.005,lat:.005,altitude_egm2008_m:110}]};
  const result={status:'engineering_event',tracks:[own,intruder],geometry:{cpa_position:{lon:.005,lat:0},horizontal_cpa_m:0,vertical_separation_at_cpa_m:10,time_to_horizontal_cpa_s:5},state_machine:{current_state:'WARNING',transitions:[{from_state:'TRACKED',to_state:'PREDICTED_CONFLICT',time_s:2,reason:'test'},{from_state:'PREDICTED_CONFLICT',to_state:'WARNING',time_s:3,reason:'test'}]},cns_gating:{C:{state:'available'},N:{state:'available_degraded'},S:{state:'available'},ownship_state_confidence:'degraded'}};
  const flow={operational_timing:{encounter_tracks:{OWN:own,INT:intruder},encounter_policies:{},maneuver_capability_profiles:{},maneuver_commands:{},encounter_lab:{}},encounter_3d_assessment:result,service_timeline:{routes:[]}};
  const html=renderDaaEncounterLab(flow),frame=encounterFrame(result,4);
  assert.match(html,/daa-track daa-ownship/);
  assert.match(html,/daa-cpa/);
  assert.match(html,/type="range"/);
  assert.match(html,/TRACKED → PREDICTED_CONFLICT/);
  assert.equal(frame.state,'WARNING');
  assert.ok(frame.ownship.lon>0);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/daa_encounter_lab.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/d_protect|hazard-zone|regulatory_buffer/);
});

test('P7-P12 workflow steps expose vertical, runtime, proposal and closed-loop contracts',()=>{
  const base={steps:{'2':false,'3':false,'5':false},spatial_3d:{altitude_layers:[],route_altitude_profiles:{}},operational_timing:{route_motion_profiles:{},service_scenarios:{},response_time_budgets:{},encounter_scenarios:{}},service_timeline:{},protection_envelope:{},grid_attributes:{},grid_risk:{},retired_route_ids:[],risks:{environment:{status:'not_calculated'},life:{status:'not_calculated'},property:{status:'not_calculated'}},scenario_routes:[],operational_routes:[],nodes:[],devices:[],defaults:{engineering_parameters:{primary_spacing_factor:{value:.9,source:'test'},co_location_search_radius_m:{value:1}}},existing_cns_facilities:{},candidate_sites:{},device_catalog:{},cns_gap_analysis:{},cns_gap_analysis_v2:{},coverage_3d:{},cns_service_capability:{},site_planning_policy:{},cns_site_plan:{},closed_loop_assessment:{},cns_corridor_assessment:{},cns_planning_objectives:{routes:{}},cns_corridor_gap_assessment:{},corridor_site_planning_policy:{},cns_corridor_site_plan:{}};
  const step2=renderStep2({flow:base,draftWorkspace:null,gridDisplay:{outline:true,theme:'none'},populationDisplayLabel:()=>'',formatNumber:String});
  const step3=renderStep3({flow:base,interactionMode:'pan'});
  const step5=renderStep5({flow:base});
  assert.match(step2,/EGM2008 orthometric/);
  assert.match(step2,/3D 高度层/);
  assert.match(step2,/建筑密度/);
  assert.match(step2,/P95 建筑高度/);
  assert.match(step2,/最大建筑高度/);
  assert.match(step2,/L8（建筑环境直接映射）/);
  assert.match(step3,/Route 3D Altitude Profile/);
  assert.match(step3,/Route Motion Profile/);
  assert.match(step3,/参考起降点/);
  assert.match(step3,/三维建筑净空/);
  assert.match(step3,/unknown\/unresolved 永远不视为 safe/);
  assert.match(step3,/reference_landing_sites 与 flow.nodes 严格分离/);
  assert.match(step5,/3D Geometric Coverage/);
  assert.match(step5,/几何覆盖 ≠ 真实 CNS 性能/);
  assert.match(step5,/CNS Service Capability/);
  assert.match(step5,/静态能力满足 ≠ 当前服务 available/);
  assert.match(step5,/C\/N\/S Service Timeline/);
  assert.match(step5,/Tactical Protection Envelope/);
  assert.match(step5,/工程保护距离 ≠ 法规 Well-Clear/);
  assert.match(step5,/CNS Gap Analysis V2/);
  assert.match(step5,/Unknown 表示证据不足/);
  assert.match(step5,/Gap 也不自动触发 Safety Event/);
  assert.match(step5,/Reuse-first CNS Site Planner V1/);
  assert.match(step5,/Proposal Only/);
  assert.match(step5,/P12/);
  assert.match(step5,/不修改 ExistingCNS/);
  assert.match(step5,/Closed-loop Validation V1/);
  assert.match(step5,/Preview 只在 working copy/);
  assert.match(step5,/Apply 才正式提交/);
  assert.match(step5,/不是真实 CNS 模型 validation 或认证结论/);
  assert.match(step5,/CNS Service Requirement Corridor/);
  assert.match(step5,/discretized volume proxy/);
  assert.match(step5,/CNS Spatial Planning Objectives/);
  assert.match(step5,/Spatial continuous-deficit/);
  assert.match(step5,/不是 runtime outage、正式 ICAO continuity/);
  assert.match(step5,/Corridor-aware Reuse-first Site Planner V2/);
  assert.match(step5,/累计 P14→P15 what-if/);
  assert.match(step5,/Unknown 不触发建站/);
  assert.match(step5,/P16 Proposal/);
  assert.match(step5,/真实设备资料库/);
  assert.match(step5,/与当前算法 DeviceCatalog 分离/);
});

test('step 6 keeps the corridor site result visibly proposal-only',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const html=renderStep6({state:{data_health:{status:'passed'}},flow:{project:{name:'P'},workspace:null,operational_routes:[],aircraft:null,rules:null,coverage:null,review:{risks:{},overall_status:'pending_confirmation',overall_pass:false},result_statuses:{cns_corridor_site_plan:'passed'},cns_corridor_site_plan:{status:'proposal_ready',target_voxel_count:2,selected_actions:[{}],confirmed_requirement_unit_volume_gain:10}}});
  assert.match(html,/P16 走廊站址规划提案/);
  assert.match(html,/Proposal only/);
  assert.match(html,/不修改 Existing CNS/);
  assert.match(html,/方案审查（Plan Review）与受控应用/);
  assert.match(html,/无自动总分\/排名/);
  assert.match(html,/CNS规划方案报告/);
  assert.match(html,/预览报告/);
  assert.match(html,/生成正式报告/);
  assert.match(html,/下载规划数据包/);
  assert.match(html,/请先在方案审查中确认一个规划方案/);
});

test('P18 review summary keeps selection confirmation and apply separate',()=>{  const result=planReviewSummary({selected_variant_id:'PV-1',variants:[{variant_id:'PV-1',selected_action_ids:['A'],evaluation:{confirmation_gate:{status:'ready_for_confirmation'}}}]},{status:'confirmed',application:{status:'not_applied'}});
  assert.deepEqual(result,{variantCount:1,selectedVariantId:'PV-1',selectedActionIds:['A'],gate:'ready_for_confirmation',confirmedStatus:'confirmed',applyStatus:'not_applied'});
});

test('Chinese UI labels keep internal enums stable and unknown conservative',()=>{
  assert.equal(statusText('confirmed_deficit'),'确认缺口');
  assert.equal(statusText('current'),'当前有效');
  assert.equal(statusText('stale'),'已失效');
  assert.equal(statusText('pending_confirmation'),'待确认');
  assert.equal(statusText('not_applicable'),'不适用');
  assert.equal(statusText('evidence_required'),'需要补充证据');
  assert.equal(statusText('unknown'),'证据不足/尚无法判断');
  assert.deepEqual(['real','synthetic','manual'].map(sourceModeText),['真实数据','模拟数据','人工录入']);
  assert.equal('confirmed_deficit','confirmed_deficit');
});

test('V3-A panel model keeps the candidate experimental and shows altitude/heading/cost',()=>{
  const flow={route_planner_v3_experiments:{status:'passed',count:1,active_experiment_id:'V3-1',
      architecture:'V3 原生 3D 战略规划',allowed_result_statuses:['strategic_candidate','failed','missing_data','pending_confirmation','not_ready','search_incomplete'],
      allowed_refinement_statuses:['refined_candidate','failed','not_ready','missing_data','search_incomplete'],
      v3b_architecture:'V3-B corridor-local',v3b_note:'V3-B 精化候选 ≠ validated route',
      note:'V3-A 战略规划实验 ≠ 运行航路',records:[{experiment_id:'V3-1',status:'strategic_candidate',distance_m:2777,state_count:18,refinement_count:0,refinement_status:null}]},
    route_planner_v3_detail:{records:[{experiment_id:'V3-1',result:{status:'strategic_candidate',route_id:'R1',
      distance_m:2777,operational_route:false,final_validation_performed:false,disclaimer:'不是 final safe',
      horizontal_projection:[[122,29.9],[122.02,29.92]],
      search_statistics:{expanded_states:17,runtime_ms:12.5},
      heuristic_semantics:{type:'admissible_3d_geometric_lower_bound',scale:1},
      hard_constraint_summary:{state_rejections:{below_terrain_clearance:4},transition_rejections:{turn_radius_exceeded:2},
        total_state_rejections:4,total_transition_rejections:2},
      cost_vector:{scalar_cost:2777,scalar_cost_available:true,excluded_components:['energy'],
        cns_integration:{integration_mode:'post_route_assessment',excluded_from_search_cost:true},
        components:{distance:{raw:2777,normalized:1,weight:1,contribution:2777,unit:'m',enabled:true,status:'passed',semantics:'真实三维航段长度'},
          energy:{raw:null,normalized:null,weight:null,contribution:null,unit:'not_modelled',enabled:false,status:'pending_model',semantics:'pending_model_disabled',reason:'V3-A 不发明 energy 公式'}}},
      candidate_refinement_corridor:{semantics:'refinement_search_window_not_safety_corridor',ring_n:1,
        center_grid_ids:['A','B'],support_grid_ids:['A','B','C'],refinement_cell_size_m:null,
        altitude_envelope:{lower_altitude_egm2008_m:100,upper_altitude_egm2008_m:200,explicit_margin_m:null},
        next_stage:'V3-B_corridor_local_refinement',n_ring_is_not_a_safety_clearance:true},
      state_path:[{grid_id:'A',altitude_egm2008_m:100,heading_deg:0,primitive_id:'h+1+0',climb_gradient:0.1,heading_change_deg:0},
        {grid_id:'B',altitude_egm2008_m:200,heading_deg:45,primitive_id:null,climb_gradient:null,heading_change_deg:null}]}}]}};
  const model=routePlannerV3Model(flow);
  assert.equal(model.candidate.status,'strategic_candidate');
  assert.equal(model.candidate.operationalRoute,false);
  assert.equal(model.candidate.finalValidationPerformed,false);
  assert.equal(model.candidate.stateCount,2);
  assert.deepEqual(model.candidate.altitudeRange,[100,200]);
  assert.equal(model.candidate.statePreview[0].headingDeg,0);
  assert.equal(model.candidate.statePreview[0].altitudeM,100);
  assert.equal(model.components.length,2);
  assert.equal(model.components[0].unit,'m');
  assert.equal(model.components[1].enabled,false);
  assert.equal(model.components[1].reason,'V3-A 不发明 energy 公式');
  assert.deepEqual(model.excludedComponents,['energy']);
  assert.equal(model.cnsIntegration.integration_mode,'post_route_assessment');
  assert.equal(model.cnsIntegration.excluded_from_search_cost,true);
  assert.equal(model.corridor.semantics,'refinement_search_window_not_safety_corridor');
  assert.equal(model.corridor.notSafetyClearance,true);
  assert.equal(model.corridor.nextStage,'V3-B_corridor_local_refinement');
  assert.equal(model.semantics,'experiment_is_not_an_operational_route');
  assert.equal(model.automaticRanking,false);
  assert.deepEqual(model.refinements,[]);
  assert.equal(model.refinementCount,0);
  assert.deepEqual(model.allowedRefinementStatuses,V3B_RESULT_STATUSES);
  assert.equal(model.refinementReadiness.status,'not_calculated');
  assert.equal(model.refinementReadiness.stage,'V3-B');
  assert.deepEqual(model.refinementApplicability.items,[]);
  assert.equal(model.refinementApplicability.staleCount,0);
});

test('V3-A readiness model exposes every domain, the missing real adapter and the V3-B hand-off',()=>{
  const readiness=routePlannerV3ReadinessModel({route_planner_v3_readiness:{status:'passed',stage:'V3-A',
    architecture:'arch',stage_scope:{implemented:['l8_strategic_search'],not_implemented:['exact_polygon_terrain_continuous_clearance_validation'],
      implemented_in_other_stages:{corridor_local_fine_refinement:'V3-B',exact_validation:'V3-C',validated_route_operational_adapter_and_cns_assessment:'V3-D'}},
    algorithm:{registered_in_algorithm_registry:false,default_route_planner:'route_planner_v1'},
    grid:{status:'regenerable_at_l8',l8_cell_count:324},
    policy:{confirmed:false,min_altitude_egm2008_m:null},
    policy_readiness:{status:'blocked',reasons:['缺少必需安全参数'],missing_parameters:['min_altitude_egm2008_m']},
    aircraft_readiness:{status:'blocked',reasons:['未解析']},
    environment_readiness:{status:'pending',reason:'真实 adapter 未实现'},
    real_data_readiness:{status:'blocked',adapter_status:'not_implemented_v3a',reason:'V3-A 不提供真实 adapter',
      v3b:{status:'blocked',adapter_status:'implemented_v3b_gis_adapter',blocking_reasons:['terrain_dtm_not_configured_or_missing'],
        resolution_policy:'explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant',terrain_dtm:null,buildings:null}},
    synthetic_environment_options:{terrain_profiles:['flat'],buildings_profiles:['none'],sources:['canonical_synthetic']}},
    route_planner_v3_refinement_readiness:{status:'blocked',stage:'V3-B',model_scope:'corridor_local_3d_refinement_v3b',
      fine_policy:{resolution_source:'explicit_configuration',resolution_m:5,status:'blocked'},
      blocking_reasons:['terrain_dtm_not_configured_or_missing'],
      environment_sources:['canonical_synthetic','configured_real_sources'],
      allowed_refinement_statuses:['refined_candidate','failed','not_ready','missing_data','search_incomplete']}});
  assert.equal(readiness.stage,'V3-A');
  assert.equal(readiness.policyReadiness.status,'blocked');
  assert.deepEqual(readiness.policyReadiness.missing_parameters,['min_altitude_egm2008_m']);
  assert.equal(readiness.aircraftReadiness.status,'blocked');
  assert.equal(readiness.realData.adapter_status,'not_implemented_v3a');
  assert.equal(readiness.scope.implemented[0],'l8_strategic_search');
  assert.equal(readiness.scope.not_implemented[0],'exact_polygon_terrain_continuous_clearance_validation');
  assert.equal(readiness.scope.implementedInOtherStages.corridor_local_fine_refinement,'V3-B');
  assert.equal(readiness.realData.v3b.adapter_status,'implemented_v3b_gis_adapter');
  assert.deepEqual(readiness.realData.v3b.blocking_reasons,['terrain_dtm_not_configured_or_missing']);
  assert.equal(readiness.refinementReadiness.stage,'V3-B');
  assert.equal(readiness.refinementReadiness.finePolicy.resolution_source,'explicit_configuration');
  assert.deepEqual(readiness.refinementReadiness.environmentSources,['canonical_synthetic','configured_real_sources']);
  assert.equal(readiness.refinementReadiness.status,'blocked');
  assert.equal(readiness.neverFinalValidated,true);
});

test('V3-A panel renders the disclaimer, corridor semantics and never claims a final route',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow={route_planner_v3_readiness:{status:'passed',stage:'V3-A',architecture:'arch',
      stage_scope:{implemented:[],not_implemented:[]},policy:{confirmed:false},policy_readiness:{status:'blocked',reasons:[],missing_parameters:[]},
      aircraft_readiness:{status:'blocked',reasons:[]},real_data_readiness:{status:'blocked',adapter_status:'not_implemented_v3a',reason:'未实现'},
      synthetic_environment_options:{terrain_profiles:['flat'],buildings_profiles:['none']}},
    route_planner_v3_experiments:{status:'not_calculated',count:0,note:'V3-A 战略规划实验 ≠ 运行航路',allowed_result_statuses:['strategic_candidate']}};
  const html=routePlannerV3Panel(flow);
  assert.match(html,/V3 战略规划实验/);
  assert.match(html,/不替换正式 V1\/V2 运行航路/);
  assert.match(html,/canonical synthetic/);
  assert.match(html,/未配置/);
  assert.match(html,/energy 默认 pending_model\/disabled/);
  assert.ok(!/final safe/.test(html)||/不是 final safe/.test(html));
  // The V3-B section renders even without any refinement, and it still refuses to
  // claim a validated route: the empty state says what has to happen first.
  assert.match(html,/V3-B corridor-local 精化候选/);
  assert.match(html,/尚无 V3-B 精化记录/);
  assert.match(html,/V3-B corridor-local 精化状态只允许 refined_candidate \/ failed \/ not_ready \/ missing_data \/ search_incomplete/);
  assert.match(html,/没有 validated\/final 状态/);
  assert.match(html,/stale：源\/corridor\/policy 变化后必须重跑/);
  assert.match(html,/id="saveRoutePlannerV3FinePolicy"/);
  assert.match(html,/id="evaluateRoutePlannerV3Refinement"/);
});

test('V3-A map overlay projects only the 2D candidate and the refinement window',()=>{
  const flow={grid:{cells:[{grid_id:'A',bbox:[122.0,29.9,122.001,29.901]},{grid_id:'C',bbox:[122.001,29.9,122.002,29.901]}]},
    route_planner_v3_experiments:{active_experiment:{experiment_id:'V3-1'}},
    route_planner_v3_detail:{records:[{experiment_id:'V3-1',result:{status:'strategic_candidate',
      horizontal_projection:[[122,29.9],[122.001,29.901]],
      candidate_refinement_corridor:{semantics:'refinement_search_window_not_safety_corridor',center_grid_ids:['A'],support_grid_ids:['A','C']}}}]}};
  const model=v3OverlayModel(flow);
  assert.deepEqual(model.path,[[122,29.9],[122.001,29.901]]);
  assert.equal(model.corridorCells.length,2);
  assert.equal(model.corridorCells[0].center,true);
  assert.equal(model.corridorCells[1].center,false);
  assert.equal(model.notSafetyCorridor,true);
  assert.equal(model.semantics,'refinement_search_window_not_safety_corridor');
  assert.deepEqual(model.refinedPath,[]);
  assert.equal(model.refinedStatus,null);
  assert.equal(model.refinedIsFinal,false);
  assert.equal(model.refinedV3cPending,true);
  assert.equal(model.refinedGridBounds,null);
  const hidden=v3OverlayModel(flow,{candidate:false,corridor:false});
  assert.deepEqual(hidden.path,[]);
  assert.deepEqual(hidden.corridorCells,[]);
});

// ---- Route Planner V3-B (corridor-local refinement) --------------------------------

function v3bRefinementRecord(){
  return {
    refinement_id:'V3B-ABC123DEF456',
    experiment_id:'V3-1',
    route_id:'R1',
    created_at:'2026-01-02T00:00:00Z',
    environment_source:'configured_real_sources',
    grounding:'corridor_local_fine_grid',
    current_applicability:'current',
    refinement_fingerprint:'V3BREF-0123456789ABCDEF',
    fingerprint_components:{strategic_fingerprint:'V3BSTRAT-1'},
    evidence_components:{strategic_fingerprint:'V3BSTRAT-1',source_fingerprint:'V3BSRC-1'},
    verdicts:{operational_route:false,final_validation_performed:false,exact_validation_performed:false,
      requires_v3c_exact_validation:true,automatic_ranking:false,automatically_scored:false},
    result:{
      status:'search_incomplete',
      reason:'达到 max_expanded_states=500：已保留当前精化候选，但未证明最优（搜索预算耗尽，不是 infeasible）',
      disclaimer:'V3-B refined candidate：corridor-local 米制细网格工程精化结果，不是 final safe / validated operational route；尚未执行 V3-C exact polygon / terrain / continuous clearance 验证，也未进入 V3-D operational adapter 与 CNS 评估。',
      final_validation_performed:false,operational_route:false,v3c_validation_pending:true,
      distance_m:1234.5,
      horizontal_projection:[[122,29.9],[122.01,29.91]],
      metric_projection:[[0,0],[1234.5,0]],
      fine_grid:{resolution_m:5,resolution_source:'explicit_configuration',requested_resolution_m:5,
        effective_source_resolution_m:30,resolution_deviation_m:0,nx:20,ny:10,cell_count:200,
        corridor_id:'V3CORR-1',corridor_ring_n:1,parent_grid_ids:['A'],
        mapping_method:'corridor_support_cells_to_local_metric_index_grid',
        not_a_safety_clearance:true,parent_grid_resolution_m:30},
      frame:{horizontal_crs:'EPSG:32651',horizontal_crs_source:'qgis_metric_transform',
        vertical_reference:'egm2008_orthometric',origin_metric:[0,0],
        axis:{column_axis:'east',row_axis:'north',index_origin:'south_west_corner'},resolution_m:5,
        metric_bounds:[0,0,100,50],
        local_to_geographic:{method:'qgis_projected_crs_inverse_transform',authority:'EPSG:32651',
          display_only:true,interpolated_from_parent_cells:false,note:'显式投影逆变换'},
        provenance:{adapter_id:'fine-environment-adapter'}},
      source_audit:{adapter_id:'fine-environment-adapter',adapter_version:'3.1',
        read_mode:'read_only_window',building_query_mode:'rtree_bbox_query',
        airspace_query_mode:'confirmed_policy_only',
        terrain_dtm:{role:'terrain_floor',dataset:'FABDEM',file_name:'fabdem.tif',size_bytes:10,mtime_ns:1,
          width:100,height:100,pixel_size:30,nodata:-9999,vertical_reference:'egm2008_orthometric',
          vertical_status:'confirmed',read_mode:'read_only_window'},
        buildings:{role:'building_envelope',file_name:'buildings.gpkg',layer:'buildings',size_bytes:10,
          mtime_ns:1,feature_count:12,crs:'EPSG:4326',extent:[1,2,3,4],spatial_index_available:true,
          query_mode:'rtree_bbox_query',height_field:'height_m'},
        airspace_policy:{role:'airspace',query_mode:'confirmed_policy_only',eligibility_status:'passed',
          algorithm_id:'airspace-eligibility-v1',algorithm_version:'1.0',allowed_grid_cell_count:4,
          confirmed_allowed_polygon_count:2,confirmed_blocked_polygon_count:1,
          inferred_from_name_or_color:false},
        metric_frame:{method:'qgis_projected_crs_inverse_transform'},
        risk_model:{algorithm_id:'risk-model-v1-relative-index',algorithm_version:'1.1',status:'passed',
          soft_fields_reused:true,reused_contributors:['ground.population','ground.traffic']},
        policy_fingerprint:'V3BPOL-XYZ',lineage:{},full_raster_resample:false,
        source_geometry_modified:false,exact_validation_performed:false,fingerprint:'V3BSRC-XYZ'},
      fine_grid_evidence:{resolution_m:5,resolution_source:'explicit_configuration',requested_resolution_m:5,
        effective_source_resolution_m:30,resolution_deviation_m:0,cell_count:200,environment_cell_count:180,
        corridor_support_cell_count:20,corridor_center_cell_count:5,
        terrain_sampling:'intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance',
        building_mapping:'buffered_footprint_conservative_envelope',horizonal_clearance_m:10,
        vertical_clearance_m:15,terrain_clearance_m:30,source_audit_fingerprint:'V3BSRC-XYZ',
        source_type:'real_sources',
        v3c_pending:['exact_polygon_membership','exact_terrain_profile_clearance',
          'continuous_clearance_along_the_full_trajectory','monotone_turn_curvature_verification']},
      search_statistics:{expanded_states:500,generated_states:900,expanded_transitions:3000,
        primitive_checks:4000,traversed_cell_checks:9000,max_stride_cells:2,expansion_cap:500,
        expansion_cap_reached:true,search_complete:false,
        search_completeness:'expansion_cap_reached_optimality_not_proven',resource_limited:true,
        resource_limit:'max_expanded_states',
        resource_limit_reason:'达到 policy.max_expanded_states=500：细网格搜索预算耗尽，未证明不可行，也未证明最优',
        optimality_proven:false,
        state_space_shape:{fine_cells:200,altitude_levels:3,heading_bins:8,naive_state_count:4800},
        endpoint_binding:{start:{fine_cell_id:'F1-0000-0000',method:'nearest_fine_cell'},
          goal:{fine_cell_id:'F1-0001-0001',method:'nearest_fine_cell'},
          semantics:'explicit_or_recorded_binding_not_assumed_membership'}},
      hard_constraint_summary:{state_rejections:{below_terrain_clearance:4},
        transition_rejections:{turn_radius_exceeded:2},
        traversed_cell_rejections:{traversed_building_envelope:3},total_state_rejections:4,
        total_transition_rejections:2,total_traversed_cell_rejections:3,primitive_checks:4000,
        traversed_cell_checks:9000,unknown_is_never_feasible:true,
        intermediate_obstacles_cannot_be_skipped_by_a_stride:true,audit_sample_cap_per_reason:200},
      cost_vector:{scalar_cost:1434.5,scalar_cost_available:true,
        scalar_cost_semantics:'sum_over_edges(length_m + sum_channels(weight_x_exposure_m))',
        scalar_weight_sum:0.5,scalar_weight_sum_is_not_bounded:true,
        scalar_weight_sum_not_used_by_the_heuristic:true,excluded_components:['energy'],
        scalar_cost_cap:null,scalar_cost_cap_active:false,
        cns_integration:{integration_mode:'post_route_assessment',excluded_from_search_cost:true},
        components:{
          distance:{raw:1234.5,normalized:1,weight:1,contribution:1234.5,unit:'m',
            source:'v3_planner_internal_geodesic_edge_sum',semantics:'真实三维航段长度',enabled:true,status:'passed'},
          population_risk:{raw:400,normalized:0.4,weight:0.5,contribution:200,unit:'index·m',
            source:'grid_risk.ground.contributors.population.normalized',semantics:'人口暴露相对指数',enabled:true,
            status:'passed',exposure_m:400,exposure_definition:'length_m_x_mean_index_of_edge_endpoints',
            normalized_index_statistics:{count:5,min:0.1,max:0.8,mean:0.4},source_resolution_m:30,
            mapping_method:'coarse_cell_index_upsampled_to_fine_cells',
            upsampled_without_new_information:true,provenance_sources:['grid_risk.ground.population'],
            provenance_note:'单一来源：canonical 母格 index 上采样',edge_count:4},
          energy:{raw:null,normalized:null,weight:null,contribution:null,unit:'not_modelled',enabled:false,
            status:'pending_model',semantics:'pending_model_disabled',reason:'V3 不发明 energy 公式'}}},
      motion_model:{model_id:'engineering_3d_motion_primitives_v3b_corridor_refinement',
        semantics:'engineering_arc_length_proxy',not_a_flight_dynamics_certification_model:true,
        multi_cell_stride:true,max_stride_cells:2,min_turn_radius_m:50,
        turn_constraint:'minimum_turn_radius_m * |dpsi| <= stride_m',traversed_cells_are_checked:true,
        altitude_interpolated_along_primitive:true,exact_curvature_validation:'not_implemented_V3-C'},
      state_path:[
        {index:0,fine_cell_id:'F1-0000-0000',parent_grid_id:'A',altitude_index:0,altitude_egm2008_m:100,
          heading_bin:0,heading_deg:0,x_metric:0,y_metric:0,x:122,y:29.9,z:100,
          vertical_reference:'egm2008_orthometric',primitive_id:'h+1+0',stride_cells:1,length_m:100,
          climb_gradient:0,heading_change_deg:0,turn_arc_required_m:0,turn_arc_available_m:10,
          traversed_cell_ids:['F1-0000-0001','F1-0000-0002'],soft_penalty:0,scalar_cost:100},
        {index:1,fine_cell_id:'F1-0001-0001',parent_grid_id:'A',altitude_index:1,altitude_egm2008_m:150,
          heading_bin:1,heading_deg:45,x_metric:100,y_metric:50,x:122.01,y:29.91,z:150,
          vertical_reference:'egm2008_orthometric',primitive_id:null,stride_cells:null,length_m:null,
          climb_gradient:null,heading_change_deg:null,turn_arc_required_m:null,turn_arc_available_m:null,
          traversed_cell_ids:[],soft_penalty:null,scalar_cost:null}],
      trajectory_summary:{state_count:2,edge_count:1,distance_m:1234.5,max_stride_cells:2,
        multi_cell_stride_edge_count:1,traversed_cell_check_count:9,climb_edge_count:0,descent_edge_count:0,
        level_edge_count:1,max_heading_change_deg:45,max_climb_gradient:0.1,altitude_min_egm2008_m:100,
        altitude_max_egm2008_m:150,start_fine_cell_id:'F1-0000-0000',goal_fine_cell_id:'F1-0001-0001',
        endpoint_binding:{},endpoint_binding_semantics:'explicit_or_recorded_binding_not_assumed_membership',
        turn_model:'engineering_arc_length_proxy'},
      semantics:{scope:'corridor_local_3d_refinement_v3b',not_final_safe:true,
        not_validated_operational_route:true,exact_validation_is_v3c:true,unknown_is_never_safe:true,
        terrain_floor_is_intersecting_pixel_max:true,building_envelope_is_conservative_not_exact:true,
        airspace_consumes_confirmed_policy_only:true,
        coarse_soft_fields_are_upsampled_without_new_information:true,turn_model:'engineering_arc_length_proxy',
        v3c_pending:['exact_polygon_membership','exact_terrain_profile_clearance',
          'continuous_clearance_along_the_full_trajectory','monotone_turn_curvature_verification'],
        allowed_result_statuses:['refined_candidate','failed','not_ready','missing_data','search_incomplete']},
    },
  };
}

function v3bFlow(refinement=v3bRefinementRecord()){
  return {
    grid:{cells:[{grid_id:'A',bbox:[122.0,29.9,122.001,29.901]}]},
    route_planner_v3_experiments:{status:'passed',count:1,active_experiment_id:'V3-1',
      architecture:'V3 原生 3D 战略规划',v3b_architecture:'V3-B corridor-local 米制细网格',
      note:'V3-A 战略规划实验 ≠ 运行航路',v3b_note:'V3-B corridor-local 精化候选 ≠ validated route',
      allowed_result_statuses:['strategic_candidate'],allowed_refinement_statuses:V3B_RESULT_STATUSES,
      active_experiment:{experiment_id:'V3-1',status:'strategic_candidate',refinement_count:1,
        refinement_id:'V3B-ABC123DEF456',refinement_status:'search_incomplete'},
      records:[{experiment_id:'V3-1',status:'strategic_candidate',distance_m:2777,state_count:3,
        refinement_count:1,refinement_status:'search_incomplete'}]},
    route_planner_v3_detail:{active_experiment_id:'V3-1',records:[{
      experiment_id:'V3-1',route_id:'R1',
      result:{status:'strategic_candidate',route_id:'R1',distance_m:2777,operational_route:false,
        final_validation_performed:false,disclaimer:'不是 final safe',
        horizontal_projection:[[122,29.9],[122.02,29.92]],
        search_statistics:{expanded_states:17,runtime_ms:12.5},
        heuristic_semantics:{type:'admissible_3d_geometric_lower_bound',scale:1},
        hard_constraint_summary:{state_rejections:{},transition_rejections:{},total_state_rejections:0,
          total_transition_rejections:0},
        cost_vector:{scalar_cost:2777,scalar_cost_available:true,excluded_components:['energy'],
          cns_integration:{integration_mode:'post_route_assessment',excluded_from_search_cost:true},
          components:{}},
        candidate_refinement_corridor:{semantics:'refinement_search_window_not_safety_corridor',ring_n:1,
          center_grid_ids:['A'],support_grid_ids:['A'],refinement_cell_size_m:null,
          altitude_envelope:{lower_altitude_egm2008_m:100,upper_altitude_egm2008_m:200,explicit_margin_m:null},
          next_stage:'V3-B_corridor_local_refinement',n_ring_is_not_a_safety_clearance:true},
        state_path:[{grid_id:'A',altitude_egm2008_m:100,heading_deg:0,primitive_id:'h+1+0',
          climb_gradient:0.1,heading_change_deg:0}]},
      refinements:refinement?[refinement]:[]}]},
    route_planner_v3_refinement_readiness:{status:'blocked',stage:'V3-B',
      model_scope:'corridor_local_3d_refinement_v3b',architecture:'V3-B corridor-local 米制细网格',
      note:'V3-B corridor-local 精化候选 ≠ validated route',
      stage_scope:{implemented:['corridor_local_metric_fine_grid','terrain_intersecting_pixel_max_floor'],
        not_implemented:['exact_polygon_membership','operational_adapter']},
      algorithm:{algorithm_id:'route_planner_v3_corridor_refinement',algorithm_version:'3.1-alpha',
        model_scope:'corridor_local_3d_refinement_v3b',registered_in_algorithm_registry:false,
        turn_model:'engineering_arc_length_proxy'},
      selected_strategic_candidate:{experiment_id:'V3-1',result_status:'strategic_candidate',route_id:'R1',
        corridor_id:'V3CORR-1',support_cell_count:20,refinement_count:1},
      fine_policy:{horizontal_crs:'EPSG:32651',resolution_source:'explicit_configuration',resolution_m:5,
        max_stride_cells:2,source:'project_engineering_basis',confirmed:true,status:'confirmed',
        missing_parameters:[],reasons:[]},
      v3_policy_readiness:{status:'ready',missing_parameters:[]},
      real_data_readiness:{status:'blocked',adapter_id:'fine-environment-adapter',adapter_version:'3.1',
        roles:['terrain_dtm','buildings','airspace'],
        terrain_dtm:'fabdem.tif',buildings:'buildings.gpkg',confirmed_allowed_grid_cells:4,
        airspace_eligibility_status:'passed',
        blocking_reasons:['terrain_dtm_not_configured_or_missing'],
        resolution_policy:'explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant',
        required_before_real_run:['FABDEM terrain_dtm with confirmed egm2008_orthometric vertical reference'],
        semantics:'readiness_report_only_no_data_read_no_fabricated_environment'},
      blocking_reasons:['terrain_dtm_not_configured_or_missing'],
      environment_sources:['canonical_synthetic','configured_real_sources'],
      resolution_policy:'explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant',
      allowed_refinement_statuses:V3B_RESULT_STATUSES},
    route_planner_v3_refinements:{status:'passed',count:1,stale_count:1,
      semantics:'stale_when_strategic_corridor_policy_or_source_audit_changes',
      components:['strategic_fingerprint','corridor_fingerprint','policy_fingerprint','source_fingerprint',
        'frame_fingerprint','fine_grid_fingerprint'],
      items:[{refinement_id:'V3B-ABC123DEF456',experiment_id:'V3-1',status:'search_incomplete',
        recorded_applicability:'current',current_applicability:'stale',
        changed_components:['source_fingerprint'],reasons:['refinement 依赖的证据已变化'],
        refinement_fingerprint:'V3BREF-0123456789ABCDEF',evidence_components:{}}]},
    route_planner_v3_readiness:{status:'passed',stage:'V3-A',architecture:'arch',
      stage_scope:{implemented:[],not_implemented:[],implemented_in_other_stages:{corridor_local_fine_refinement:'V3-B'}},
      policy:{confirmed:false},policy_readiness:{status:'blocked',reasons:[],missing_parameters:[]},
      aircraft_readiness:{status:'blocked',reasons:[]},
      real_data_readiness:{status:'blocked',adapter_status:'not_implemented_v3a',reason:'未实现',
        v3b:{status:'blocked',adapter_status:'implemented_v3b_gis_adapter',
          blocking_reasons:['terrain_dtm_not_configured_or_missing'],
          resolution_policy:'explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant',
          terrain_dtm:null,buildings:null}},
      synthetic_environment_options:{terrain_profiles:['flat'],buildings_profiles:['none']}},
  };
}

test('V3-B panel marks the refined candidate experimental and never claims validation',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3bFlow(),html=routePlannerV3Panel(flow);
  assert.match(html,/V3-B corridor-local 精化候选/);
  assert.ok(html.includes('refined candidate，未执行 V3-C 连续几何验证'));
  assert.ok(html.includes(V3B_REFINED_LABEL));
  assert.match(html,/<b>refined candidate，未执行 V3-C 连续几何验证<\/b>/);
  // refined vs coarse 2D comparison
  assert.match(html,/coarse（V3-A 战略候选）：state 1 · 3D distance 2777\.00 m/);
  assert.match(html,/refined（V3-B corridor-local 精化）：state 2 · 3D distance 1234\.50 m/);
  assert.match(html,/不是最终安全航路/);
  assert.match(html,/V3-D/);
  // fine grid + resolution provenance
  assert.match(html,/fine grid 与 resolution provenance/);
  assert.match(html,/resolution_source<\/b><small>explicit_configuration<\/small>/);
  assert.match(html,/fine cell_count<\/b><small>200<\/small>/);
  assert.match(html,/environment_cell_count<\/b><small>180<\/small>/);
  assert.match(html,/nx × ny<\/b><small>20 × 10<\/small>/);
  assert.match(html,/不是硬编码的 30 m 常数/);
  assert.match(html,/not_a_safety_clearance/);
  assert.match(html,/terrain_clearance_m \/ building horizonal_clearance_m \/ vertical_clearance_m<\/b><small>30\.00 m \/ 10\.00 m \/ 15\.00 m<\/small>/);
  assert.match(html,/intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance/);
  assert.match(html,/buffered_footprint_conservative_envelope/);
  // search scale + hard rejection
  assert.match(html,/search 规模与 hard rejection/);
  assert.match(html,/traversed cell rejection 总数<\/b><small>3<\/small>/);
  assert.match(html,/traversed_building_envelope/);
  assert.match(html,/primitive_checks<\/b><small>4000<\/small>/);
  assert.match(html,/traversed_cell_checks<\/b><small>9000<\/small>/);
  assert.match(html,/multi-cell stride edges \/ traversed cell checks<\/b><small>1 \/ 9<\/small>/);
  assert.match(html,/max heading change \/ max climb gradient<\/b><small>45\.00 ° \/ 0\.10<\/small>/);
  assert.match(html,/altitude range EGM2008 \(m\)<\/b><small>100\.00 m – 150\.00 m<\/small>/);
  assert.match(html,/frame metric_bounds（米制，不是经纬度）<\/b><small>\[0,0,100,50\]<\/small>/);
  // cost breakdown with exposure + provenance
  assert.match(html,/cost breakdown（含 exposure 与 provenance）/);
  assert.match(html,/exposure_m 400\.00/);
  assert.match(html,/source_resolution_m 30\.00 m/);
  assert.match(html,/mapping_method coarse_cell_index_upsampled_to_fine_cells/);
  assert.match(html,/provenance sources \[&quot;grid_risk\.ground\.population&quot;\]/);
  assert.match(html,/h = 纯 3D 几何距离/);
  // data provenance of the fine environment
  assert.match(html,/fabdem\.tif/);
  assert.match(html,/pixel_size 30\.00 m/);
  assert.match(html,/nodata -9999/);
  assert.match(html,/vertical_status confirmed/);
  assert.match(html,/buildings\.gpkg/);
  assert.match(html,/feature_count 12/);
  assert.match(html,/spatial_index_available true/);
  assert.match(html,/not applicable · display-only reference layer/);
  assert.match(html,/ground\.population/);
  assert.match(html,/full_raster_resample \/ source_geometry_modified \/ exact_validation_performed/);
  assert.match(html,/false \/ false \/ false/);
  assert.match(html,/exact_polygon_membership/);
  // verdicts: never a validated / final route
  assert.match(html,/operational_route=false/);
  assert.match(html,/final_validation_performed=false/);
  assert.match(html,/v3c_validation_pending=true/);
  const claims=[...html.matchAll(/final safe/g)];
  assert.ok(claims.length>0);
  assert.equal([...html.matchAll(/不是 final safe/g)].length,claims.length);
  const model=routePlannerV3Model(flow);
  assert.equal(model.refinements.length,1);
  assert.equal(model.refinements[0].finalValidationPerformed,false);
  assert.equal(model.refinements[0].operationalRoute,false);
  assert.equal(model.refinements[0].v3cPending,true);
  assert.equal(model.refinements[0].cellCount,200);
  assert.equal(model.refinements[0].environmentCellCount,180);
  assert.equal(model.refinements[0].hardRejection.totalTraversed,3);
  assert.equal(model.refinements[0].statePreview[0].traversedCellCount,2);
  assert.equal(model.refinements[0].statePreview[0].fineCellId,'F1-0000-0000');
  assert.equal(model.refinements[0].costComponents[1].upsampledWithoutNewInformation,true);
  assert.equal(model.refinements[0].costMeta.scalarWeightSumIsNotBounded,true);
  assert.equal(model.refinements[0].costMeta.scalarWeightSumNotUsedByTheHeuristic,true);
});

test('V3-B panel reports search_incomplete as resource limited instead of infeasible',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3bFlow(),html=routePlannerV3Panel(flow),model=routePlannerV3Model(flow);
  assert.match(html,/search_incomplete/);
  assert.match(html,/resource limited/);
  assert.match(html,/不是 infeasible/);
  assert.match(html,/未证明最优/);
  assert.match(html,/expansion_cap_reached=true/);
  assert.match(html,/expansion_cap_reached_optimality_not_proven/);
  assert.match(html,/minimum_turn_radius_m \* \|dpsi\| &lt;= stride_m/);
  assert.match(html,/exact_curvature_validation not_implemented_V3-C/);
  assert.match(html,/not_a_flight_dynamics_certification_model true/);
  assert.equal(model.refinements[0].status,'search_incomplete');
  assert.equal(model.refinements[0].resourceLimited,true);
  assert.equal(model.refinements[0].optimalityProven,false);
  assert.equal(model.refinements[0].searchCompleteness,'expansion_cap_reached_optimality_not_proven');
  assert.equal(model.refinements[0].expansionCapReached,true);
  assert.equal(model.refinements[0].resourceLimit,'max_expanded_states');
});

test('V3-B panel states that coarse to fine soft mapping gains no new source accuracy',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3bFlow(),html=routePlannerV3Panel(flow),model=routePlannerV3Model(flow);
  assert.match(html,/upsampled_without_new_information/);
  assert.match(html,/不获得新的原始精度/);
  assert.match(html,/coarse→fine 的 soft field/);
  assert.equal(model.refinements[0].costComponents[1].upsampledWithoutNewInformation,true);
  assert.equal(model.refinements[0].costComponents[1].sourceResolutionM,30);
  assert.equal(model.refinements[0].evidence.resolution_source,'explicit_configuration');
  assert.equal(model.refinements[0].semantics.coarse_soft_fields_are_upsampled_without_new_information,true);
});

test('V3-B readiness, fine policy hand-off and staleness are rendered',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3bFlow(),html=routePlannerV3Panel(flow),readiness=routePlannerV3ReadinessModel(flow);
  assert.match(html,/V3-B readiness/);
  assert.match(html,/corridor_local_metric_fine_grid/);
  assert.match(html,/exact_polygon_membership/);
  assert.match(html,/terrain_dtm_not_configured_or_missing/);
  assert.match(html,/explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant/);
  assert.match(html,/selected strategic candidate/);
  assert.match(html,/refinement_count 1/);
  assert.match(html,/id="v3bFineCrs"/);
  assert.match(html,/id="v3bFineResolutionSource"/);
  assert.match(html,/id="v3bFineResolution"/);
  assert.match(html,/id="v3bFineMaxStride"/);
  assert.match(html,/id="v3bFineConfirmed"/);
  assert.match(html,/禁止默认 30 m/);
  assert.match(html,/id="v3bEnvironmentSource"/);
  assert.match(html,/value="configured_real_sources"/);
  assert.match(html,/id="v3bRefinementCellSize"/);
  assert.match(html,/synthetic base_surface_elevation_m/);
  assert.match(html,/V3-B 精化历史与适用性/);
  assert.match(html,/current_applicability stale/);
  assert.match(html,/changed_components \[&quot;source_fingerprint&quot;\]/);
  assert.match(html,/stale_count 1/);
  assert.match(html,/stale：源\/corridor\/policy 变化后必须重跑/);
  assert.equal(readiness.refinementReadiness.status,'blocked');
  assert.equal(readiness.refinementReadiness.selectedCandidate.support_cell_count,20);
  assert.deepEqual(readiness.refinementReadiness.blockingReasons,['terrain_dtm_not_configured_or_missing']);
  assert.equal(readiness.scope.implementedInOtherStages.corridor_local_fine_refinement,'V3-B');
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
  assert.match(source,/\/api\/route-planner-v3\/fine-policy/);
  assert.match(source,/\/api\/route-planner-v3-refinements\/evaluate/);
  assert.match(source,/loadRoutePlannerV3Detail/);
  assert.match(source,/horizontal_crs:c\.\$\('v3bFineCrs'\)/);
});

test('V3-B statuses are extended and never include a validated status',()=>{
  assert.deepEqual(V3_RESULT_STATUSES,['strategic_candidate','failed','missing_data','pending_confirmation','not_ready','search_incomplete']);
  assert.deepEqual(V3B_RESULT_STATUSES,['refined_candidate','failed','not_ready','missing_data','search_incomplete']);
  for(const status of ['validated','final','operational_route','safe']){
    assert.ok(!V3_RESULT_STATUSES.includes(status));
    assert.ok(!V3B_RESULT_STATUSES.includes(status));
  }
  assert.equal(V3B_REFINED_LABEL,'refined candidate，未执行 V3-C 连续几何验证');
});

test('V3-B overlay draws both projections and keeps the corridor semantics',()=>{
  const flow=v3bFlow();
  const model=v3OverlayModel(flow);
  assert.deepEqual(model.path,[[122,29.9],[122.02,29.92]]);
  assert.deepEqual(model.refinedPath,[[122,29.9],[122.01,29.91]]);
  assert.equal(model.refinedStatus,'search_incomplete');
  assert.equal(model.refinedCellCount,200);
  assert.equal(model.refinedResolutionM,5);
  assert.equal(model.refinedId,'V3B-ABC123DEF456');
  assert.equal(model.refinedIsFinal,false);
  assert.equal(model.refinedV3cPending,true);
  assert.equal(model.corridorCells.length,1);
  assert.equal(model.corridorCells[0].center,true);
  assert.equal(model.notSafetyCorridor,true);
  assert.equal(model.semantics,'refinement_search_window_not_safety_corridor');
  // A 2-point (here: 1-point) refined path cannot place the fine-grid rectangle.
  assert.equal(model.refinedGridBounds,null);
  const without=structuredClone(flow);
  without.route_planner_v3_detail.records[0].refinements=[];
  const missing=v3OverlayModel(without);
  assert.deepEqual(missing.refinedPath,[]);
  assert.equal(missing.refinedStatus,null);
  assert.equal(missing.refinedCellCount,null);
  assert.equal(missing.refinedGridBounds,null);
  assert.deepEqual(missing.path,[[122,29.9],[122.02,29.92]]);
  const empty=structuredClone(flow);
  empty.route_planner_v3_detail.records[0].refinements[0].result.horizontal_projection=[];
  const noProjection=v3OverlayModel(empty);
  assert.deepEqual(noProjection.refinedPath,[]);
  assert.equal(noProjection.refinedStatus,'search_incomplete');
  // The refined projection is a separate toggle from the coarse candidate.
  assert.deepEqual(v3OverlayModel(flow,{refined:false}).refinedPath,[]);
  assert.deepEqual(v3OverlayModel(flow,{candidate:false}).path,[]);
  assert.equal(v3OverlayModel(flow,{candidate:false}).refinedPath.length,2);
});

// --------------------------------------------------------------------------------------
// V3-C: continuous geometry realization + source-native/vector validation
// --------------------------------------------------------------------------------------

function v3cValidationResult(overrides={}){
  const base={
    status:'validated_route',
    algorithm_id:'route_planner_v3_continuous_validation',algorithm_version:'3.2-alpha',
    model_scope:'continuous_geometry_realization_and_source_native_validation_v3c',
    validation_fingerprint:'V3CVALID-abc123',operational_route:false,cns_assessed:false,
    reason:'连续几何实现 + confirmed 源几何/原生栅格验证全部 passed；仍然不是 operational route，CNS 尚未评估',
    domain_statuses:{geometry:'passed',airspace:'passed',terrain:'passed',building:'passed',
      altitude:'passed',kinematics:'passed'},
    domains:{
      geometry:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}},
      airspace:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}},
      terrain:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}},
      building:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}},
      altitude:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}},
      kinematics:{status:'passed',reason:null,violations:[],unresolved:[],evaluated:true,evidence:{}}},
    violations:[],
    unresolved_evidence:[],
    min_margins:{airspace_horizontal_m:120.5,terrain_vertical_m:170.0,building_horizontal_m:35.0,
      building_vertical_m:125.0,altitude_lower_m:0.0,altitude_upper_m:200.0,turn_radius_m:0.0,
      climb_gradient_margin:0.08,descent_gradient_margin:0.08},
    kinematics:{minimum_turn_radius_observed_m:50.0,required_minimum_turn_radius_m:50.0,
      max_climb_gradient_observed:0.1,max_descent_gradient_observed:0.0,
      max_allowed_climb_gradient:0.18,max_allowed_descent_gradient:0.18,
      tangent_heading_continuity_verified:true,self_intersection_diagnostic:{available:true,self_intersecting:false},
      self_intersection_is_failure:false,
      turn_verification:'analytic_arc_radius_and_tangent_heading_not_the_v3b_arc_length_proxy'},
    resource_limits:{max_validation_samples:200000,sample_count:42,max_runtime_s:null,runtime_s:0.25,
      resource_limited:false,resource_limit_reason:null},
    curve_error:{requested_max_chord_error_m:0.5,actual_max_chord_error_m:0.428,
      method:'equal_angle_chord_sagitta_bounded_by_explicit_curve_chord_error',envelope_radius_m:0.5},
    continuous_route:{
      schema_version:'3.2-continuous-route',status:'realized',vertical_reference:'egm2008_orthometric',
      horizontal_geometry:{
        analytic:{primitive_count:3,straight_count:2,arc_count:1,turn_count:1,
          total_horizontal_length_m:1914.159,curvature_continuity:'C1_position_and_heading_only',
          continuous_curvature:false,c2:false,clothoid:'future_work_not_implemented',
          turn_radius_policy:'radius_equals_explicit_minimum_never_reduced_to_fit',
          radius_reduced_anywhere:false},
        linearized:{linestring_metric:[[0,0],[800,0],[1000,200],[1000,1000]],point_count:4,
          actual_max_chord_error_m:0.428,curve_chord_error_m:0.5,
          method:'equal_angle_chord_sagitta_bounded_by_explicit_curve_chord_error',
          not_the_mathematical_curve:true}},
      primitives:[{kind:'straight',sampled_points_metric:[[0,0],[800,0]]},
        {kind:'circular_arc',sampled_points_metric:[[800,0],[1000,200]]},
        {kind:'straight',sampled_points_metric:[[1000,200],[1000,1000]]}],
      turns:[{turn_id:'turn001',radius_m:50,turn_angle_deg:90}],
      vertical:{method:'egm2008_orthometric_altitude_against_realized_along_track_distance',
        total_distance_m:1914.159,min_z_egm2008_m:100,max_z_egm2008_m:300,
        max_climb_gradient_observed:0.1,max_descent_gradient_observed:0.0},
      operational_route:false,cns_assessed:false,final_validation_performed:true,
      semantics:{not_final_safe:true,never_repairs_or_replans:true},
      disclaimer:'V3-C continuous validated route：仍然不是 operational route，CNS 尚未评估'},
    source_audit:{adapter_id:'v3c_real_source_evidence_adapter',validation_evidence_source:'canonical_synthetic'},
    curve_chord_error_m:0.5,
    verdicts:{all_domains_passed:true,replan_required:false,automatic_repair_performed:false,
      automatic_replan_performed:false,validated_route_is_operational_route:false,cns_assessed:false,
      unknown_is_never_safe:true},
    semantics:{scope:'continuous_geometry_realization_and_source_native_validation_v3c',
      continuity:'C1',continuous_curvature:false,c2:false,
      clothoid:'future_work_not_implemented',terrain_evidence:'source_native_raster_validation',
      validated_route_is_not_operational_route:true,cns_not_assessed:true},
    disclaimer:'V3-C continuous validated route：连续几何实现 + confirmed 源几何/原生栅格验证结果，仍然不是 operational route，CNS 尚未评估',
  };
  return {...base,...overrides};
}

function v3cFlow(validationResult=v3cValidationResult()){
  const flow=v3bFlow();
  const refinement=flow.route_planner_v3_detail.records[0].refinements[0];
  refinement.validations=[{
    validation_id:'V3C-ABC123DEF456',refinement_id:refinement.refinement_id,
    experiment_id:'V3-1',created_at:'2026-01-01T00:00:00Z',evidence_source:'canonical_synthetic',
    current_applicability:'current',
    evidence_components:{refinement_fingerprint:'V3BREF-x',continuous_policy_fingerprint:'V3CPOL-x',
      curve_tolerance_fingerprint:'V3CCURVE-x',source_fingerprint:'V3CSRC-x',crs_fingerprint:'V3CCRS-x',
      validator_versions_fingerprint:'V3CVAL-x'},
    result:validationResult}];
  flow.route_planner_v3_experiments.v3c_architecture='V3-C 连续几何实现 + source-native 验证';
  flow.route_planner_v3_experiments.v3c_note='V3-C 连续验证 ≠ operational route';
  flow.route_planner_v3_experiments.allowed_validation_statuses=V3C_RESULT_STATUSES;
  flow.route_planner_v3_continuous_readiness={status:'passed',stage:'V3-C',
    model_scope:'continuous_geometry_realization_and_source_native_validation_v3c',
    architecture:'V3-C 连续几何实现 + source-native 验证',note:'V3-C 连续验证 ≠ operational route',
    stage_scope:{implemented:['c1_tangent_continuous_geometry_realization',
      'exact_airspace_route_envelope_validation','native_raster_terrain_validation'],
      not_implemented:['clothoid_or_continuous_curvature_transitions','operational_adapter']},
    algorithm:{algorithm_id:'route_planner_v3_continuous_validation',algorithm_version:'3.2-alpha',
      model_scope:'continuous_geometry_realization_and_source_native_validation_v3c',
      registered_in_algorithm_registry:false,validator_versions:{v3_continuous_validator:'3.2'}},
    selected_refinement:{experiment_id:'V3-1',refinement_id:refinement.refinement_id,
      result_status:'search_incomplete',current_applicability:'current',validation_count:1},
    validation_policy:{curve_chord_error_m:0.5,max_validation_samples:200000,max_runtime_s:null,
      use_curve_error_envelope:true,source:'project_engineering_basis',confirmed:true,status:'confirmed',
      missing_parameters:[],reasons:[],aircraft_min_turn_radius_m:null},
    v3_planning_policy:{aircraft_min_turn_radius_m:50,terrain_clearance_m:30,
      building_horizontal_clearance_m:15,building_vertical_clearance_m:30},
    real_data_readiness:{status:'blocked',adapter_id:'v3c_real_source_evidence_adapter',
      adapter_version:'3.2-alpha',blocking_reasons:['terrain_dtm_not_configured_or_missing']},
    blocking_reasons:[],evidence_sources:V3C_EVIDENCE_SOURCES,
    allowed_result_statuses:V3C_RESULT_STATUSES,
    boundaries:{operational_route_always_false:true,cns_assessed_always_false:true,
      never_repairs_or_replans:true,curve_error_has_no_default:true}};
  flow.route_planner_v3_validations={status:'passed',count:1,stale_count:0,validated_route_count:1,
    semantics:'stale_when_refinement_policy_curve_tolerance_source_or_validator_changes',
    components:['refinement_fingerprint','continuous_policy_fingerprint','curve_tolerance_fingerprint',
      'source_fingerprint','crs_fingerprint','validator_versions_fingerprint'],
    items:[{validation_id:'V3C-ABC123DEF456',refinement_id:refinement.refinement_id,experiment_id:'V3-1',
      status:validationResult.status,domain_statuses:validationResult.domain_statuses,
      recorded_applicability:'current',current_applicability:'current',changed_components:[],reasons:[],
      validation_fingerprint:'V3CVALID-abc123',evidence_components:{}}]};
  return flow;
}

test('V3-C validation model renders every domain, margin, turn radius and source tolerance',()=>{
  const flow=v3cFlow();
  const model=routePlannerV3ContinuousModel(flow);
  assert.equal(model.status,'passed');
  assert.equal(model.validationId,'V3C-ABC123DEF456');
  const validation=model.validationModel;
  assert.equal(validation.status,'validated_route');
  assert.deepEqual(validation.domains.map(item=>item.domain),V3C_DOMAINS);
  assert.ok(validation.domains.every(item=>item.status==='passed'));
  assert.equal(validation.margins.terrainVerticalM,170);
  assert.equal(validation.margins.airspaceHorizontalM,120.5);
  assert.equal(validation.kinematics.minimumTurnRadiusObservedM,50);
  assert.equal(validation.kinematics.tangentHeadingContinuityVerified,true);
  assert.equal(validation.analytic.arcCount,1);
  assert.equal(validation.analytic.continuousCurvature,false);
  assert.equal(validation.analytic.c2,false);
  assert.equal(validation.analytic.clothoid,'future_work_not_implemented');
  assert.equal(validation.analytic.radiusReducedAnywhere,false);
  assert.equal(validation.linearized.curveChordErrorM,0.5);
  assert.equal(validation.linearized.actualMaxChordErrorM,0.428);
  assert.equal(validation.linearized.notTheMathematicalCurve,true);
  assert.equal(validation.resourceLimits.sampleCount,42);
  assert.equal(validation.vertical.maxClimbGradient,0.1);
  // The two boundaries can never be relaxed, whatever the status.
  assert.equal(validation.operationalRoute,false);
  assert.equal(validation.cnsAssessed,false);
  assert.equal(validation.verdicts.validated_route_is_operational_route,false);
  assert.equal(validation.verdicts.cns_assessed,false);
});

test('V3-C panel states the operational/CNS disclaimer and reports violations and unresolved evidence',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const failed=v3cValidationResult({
    status:'failed',reason:'确定违反：terrain:below_native_terrain_clearance（不自动修路/不自动 replan）',
    domain_statuses:{geometry:'passed',airspace:'passed',terrain:'failed',building:'passed',
      altitude:'passed',kinematics:'passed'},
    violations:[{domain:'terrain',reason_id:'below_native_terrain_clearance',start_distance_m:120,
      end_distance_m:260,start_coordinate:null,end_coordinate:null,required:290,observed:200,margin:-90,
      evidence:{pixel:[5,3],source_value:260}}],
    unresolved_evidence:[{domain:'building',reason_id:'building_height_missing',start_distance_m:null,
      end_distance_m:null,required:'confirmed_height_and_ground_and_valid_geometry',observed:'unresolved',
      evidence:{building_id:'B9'}}]});
  const html=routePlannerV3ValidationPanel(v3cFlow(failed));
  assert.match(html,/V3-C/);
  assert.match(html,/V3-C validated route 仍不是 operational route；CNS 尚未评估/);
  assert.match(html,/validated_route/);
  assert.match(html,/failed/);
  assert.match(html,/below_native_terrain_clearance/);
  assert.match(html,/building_height_missing/);
  assert.match(html,/replan_required/);
  assert.match(html,/不自动修路/);
  assert.match(html,/source_native_raster_validation/);
  assert.match(html,/NoData/);
  assert.match(html,/curve_chord_error_m/);
  assert.match(html,/radius_reduced_anywhere/);
  assert.match(html,/C2 false/);
  assert.match(html,/clothoid future_work_not_implemented/);
  assert.match(html,/id="saveRoutePlannerV3ValidationPolicy"/);
  assert.match(html,/id="evaluateRoutePlannerV3Validation"/);
  // The main V3-A panel renders V3-A, V3-B and V3-C together.
  const combined=routePlannerV3Panel(v3cFlow(failed));
  assert.match(combined,/V3 战略规划实验/);
  assert.match(combined,/V3-B corridor-local 精化候选/);
  assert.match(combined,/V3-C 连续几何实现 \+ source-native\/vector 验证/);
});

test('V3-C statuses include validated_route but never claim an operational route or CNS assessment',()=>{
  assert.deepEqual(V3C_RESULT_STATUSES,['validated_route','failed','unresolved','not_ready','validation_incomplete']);
  assert.deepEqual(V3C_DOMAINS,['geometry','airspace','terrain','building','altitude','kinematics']);
  assert.equal(V3C_OPERATIONAL_LABEL,'V3-C validated route 仍不是 operational route；CNS 尚未评估');
  for(const wrong of ['operational_route','cns_assessed','final_safe','safe']){
    assert.ok(!V3C_RESULT_STATUSES.includes(wrong));
  }
});

test('V3-C overlay places the realized route and marks failed intervals without claiming operational status',()=>{
  const flow=v3cFlow(v3cValidationResult({
    status:'failed',
    domain_statuses:{geometry:'passed',airspace:'passed',terrain:'failed',building:'passed',
      altitude:'passed',kinematics:'passed'},
    violations:[{domain:'terrain',reason_id:'below_native_terrain_clearance',start_distance_m:800,
      end_distance_m:900,required:290,observed:200,margin:-90,evidence:{}}]}));
  const model=v3OverlayModel(flow);
  assert.equal(model.validatedStatus,'failed');
  assert.equal(model.validatedId,'V3C-ABC123DEF456');
  assert.equal(model.validatedIsOperationalRoute,false);
  assert.equal(model.validatedCnsAssessed,false);
  assert.equal(model.validatedArcCount,1);
  assert.equal(model.curveChordErrorM,0.5);
  assert.equal(model.validatedPath.length,4);
  assert.deepEqual(model.validatedPath[0],[122,29.9]);
  assert.equal(model.validatedDomainStatuses.terrain,'failed');
  assert.equal(model.validatedIntervals.length,1);
  assert.equal(model.validatedIntervals[0].kind,'violation');
  assert.ok(model.validatedIntervals[0].startCoordinate);
  assert.equal(model.validatedMargins.terrain_vertical_m,170);
  // Disabling the validated layer must not drop the earlier projections.
  assert.deepEqual(v3OverlayModel(flow,{validated:false}).validatedPath,[]);
  assert.equal(v3OverlayModel(flow,{validated:false}).refinedPath.length,2);
});

// --------------------------------------------------------------------------------------
// V3-D: operational adoption + CNS assessment bridge
// --------------------------------------------------------------------------------------

function v3dProjection(overrides={}){
  const path=[[122.0005,29.9005],[122.00778,29.90778],[122.01306,29.91306],[122.0195,29.9195]];
  const waypoints=path.map((point,index)=>({distance_along_route_m:index*0.0123,altitude_m:100}));
  return Object.assign({
    schema_version:'3.3-operational-projection',status:'ready',reason:null,
    projection_id:'V3DPROJ-ABCDEF1234567890',projection_fingerprint:'V3DPROJ-ABCDEF1234567890',
    validation_id:'V3C-ABC123DEF456',refinement_id:'V3B-ABC123DEF456',experiment_id:'V3-1',
    route_id:'R0001',
    route:{route_id:'R0001',start:[122.0005,29.9005],end:[122.0195,29.9195],
      start_node_id:'N001',end_node_id:'N002',direction:'ab',kind:'operational',status:'passed',
      path,
      provenance:{source_type:'v3c_validated_route',planner_family:'route_planner_v3',
        validation_id:'V3C-ABC123DEF456',validation_fingerprint:'V3CVALID-abc123',
        refinement_id:'V3B-ABC123DEF456',refinement_fingerprint:'V3BREF-x',
        curve_chord_error_m:0.5,horizontal_crs:'EPSG:32651',vertical_reference:'egm2008_orthometric',
        horizontal_representation:'two_dimensional_lon_lat_only',
        vertical_representation:'locked_route_altitude_profile_waypoints',
        crs_transform:{method:'explicit_projected_crs_to_ogc_crs84',authority:'EPSG:32651',
          target_crs:'OGC:CRS84',geodetic:true},
        cns_integration_mode:'post_route_assessment',cns_excluded_from_search_cost:true,
        simplification_applied:false}},
    profile:{route_id:'R0001',mode:'waypoint_linear',vertical_reference:'egm2008_orthometric',
      constant_altitude_m:null,waypoints,source:'v3c_validated_route',confirmed:true,derived:true,
      locked:true,locked_by_adoption:true,adoption_owned:true,status:'confirmed',
      distance_basis:'cumulative_2d_baseline_path_distance_matching_path_vertex_order',
      profile_derivation:'v3c_z_s_sampled_at_the_published_path_vertices_and_interpolated_linearly_on_the_published_profile_distance_basis',
      profile_semantics:'v3c_validated_realized_vertical_profile_not_an_independent_safety_verdict',
      vertex_order_semantics:'index_matched_to_published_path_vertices',
      v3_metric_length_m:2855.13,legacy_geodesic_length_m:2863.19,length_delta_m:8.06,
      curve_chord_error_m:0.5,requested_by_user:false},
    path_metrics:{v3_metric_length_m:2855.13,legacy_geodesic_length_m:2863.19,length_delta_m:8.06,
      distance_basis:'cumulative_2d_baseline_path_distance_matching_path_vertex_order',
      profile_length_m:0.0276,metric_vertex_count:4,published_vertex_count:4,
      curve_chord_error_m:0.5,simplification_applied:false},
    path_crs:'OGC:CRS84',horizontal_crs:'EPSG:32651',horizontal_crs_source:'explicit_configuration',
    transform:{method:'explicit_projected_crs_to_ogc_crs84',authority:'EPSG:32651',
      geodetic:true,target_crs:'OGC:CRS84'},
    two_dimensional_path_only:true,
    altitude_representation:{vertical_reference:'egm2008_orthometric',
      in_geojson_third_coordinate:false,carried_by:'locked_route_altitude_profile_waypoints',
      reason:'GeoJSON 第三坐标会被解释为 WGS84 ellipsoidal height'},
    profile_vertex_count:waypoints.length,
    compatibility:{projection_semantics:'validated_v3c_linearized_metric_geometry_transformed_to_ogc_crs84_two_dimensional_only',
      profile_semantics:'v3c_validated_realized_vertical_profile_not_an_independent_safety_verdict',
      profile_derivation:'v3c_z_s_sampled',path_and_profile_share_vertex_order:true,
      path_and_profile_share_distance_basis:true,simplification_applied:false,crs_mixing:false,
      published_metric_length_m:2855.13,published_geodesic_length_m:2863.19,length_delta_m:8.06,
      vertex_count:path.length,min_altitude_egm2008_m:100,max_altitude_egm2008_m:100},
    downstream_invalidation:V3D_DOWNSTREAM_RESULTS,
    route_identity:{route_id:'R0001',start:[122.0005,29.9005],end:[122.0195,29.9195],
      start_node_id:'N001',end_node_id:'N002',identity_preserved:true},
    issues:[],
    semantics:{authoritative_geometry_source:'v3c_continuous_route_linearized_metric_geometry',
      no_simplification:true,two_dimensional_path_only:true,
      egm2008_never_in_geojson_third_coordinate:true,cns_not_in_route_cost:true}},overrides);
}

function v3dFlow({projection=null,adoptions=null,assessment=null,publishOptions=null}={}){
  const flow=v3cFlow();
  flow.route_planner_v3_operational_publish={status:'passed',stage:'V3-D',
    model_scope:'v3c_validated_route_operational_adoption_and_cns_bridge_v3d',
    algorithm:{algorithm_id:'route_planner_v3_operational_adoption',algorithm_version:'3.3-alpha',
      registered_in_algorithm_registry:false},
    options:publishOptions||[{validation_id:'V3C-ABC123DEF456',route_id:'R0001',
      refinement_id:'V3B-ABC123DEF456',status:'validated_route',evidence_source:'canonical_synthetic',
      eligible:true,reasons:[],production_eligible:false,
      production_reasons:['production_apply_requires_configured_real_sources']}],
    blocking_reasons:[],boundaries:{production_apply_requires_configured_real_sources:true,
      synthetic_preview_only:true,cns_excluded_from_route_cost:true,
      route_safety_and_cns_compliance_are_separate:true,v3c_validation_history_immutable:true},
    note:'V3-D 桥接说明'};
  flow.v3_operational_adoptions=adoptions||{status:'passed',count:1,current_count:1,stale_count:0,
    revoked_count:0,items:[{adoption_id:'V3D-ABCDEF123456',route_id:'R0001',status:'published',
      current_applicability:'current',applied_at:'2026-01-01T00:00:00Z',
      evidence_source:'configured_real_sources',validation_ids:['V3C-ABC123DEF456'],
      validation_fingerprints:{'V3C-ABC123DEF456':'V3CVALID-abc123'},
      refinement_fingerprint:'V3BREF-x',projection_fingerprint:'V3DPROJ-ABCDEF1234567890',
      route_provenance:{},path_metrics:{},compatibility:{},before:{present:false},after:{present:true},
      cns_assessment:{bundle_id:null,assessment_status:'not_started',requirement_verdict:'unknown'},
      applicability_reasons:[],ownership:{route_owned:true,profile_owned:true,revocable:true}}]};
  flow.v3_cns_assessment={status:'not_calculated',count:0,items:[],stage_order:V3D_STAGES};
  if(assessment){
    flow.v3_cns_assessment={status:'passed',count:1,items:[assessment],stage_order:V3D_STAGES};
  }
  return flow;
}

function v3dBundle(overrides={}){
  const base={
    bundle_id:'V3CNS-V3DBUNDLE-AB',route_id:'R0001',adoption_id:'V3D-ABCDEF123456',
    validation_id:'V3C-ABC123DEF456',assessment_status:'complete',requirement_verdict:'does_not_meet',
    route_validation_status:'validated_route',route_validation_unchanged:true,
    blocking_reasons:[],computed_at:'2026-01-01T00:00:00Z',assessment_fingerprint:'V3DBUNDLE-AB',
    requested_stages:[...V3D_STAGES],
    stage_results:{
      P7:{stage:'P7',status:'passed',route_statuses:{R0001:'passed'},reason:null},
      P8:{stage:'P8',status:'does_not_meet_under_model',
        route_statuses:{R0001:'does_not_meet_under_model'},reason:null},
      P9:{stage:'P9',status:'passed',route_statuses:{R0001:'passed'},reason:null},
      P10:{stage:'P10',status:'confirmed_gap',route_statuses:{R0001:'confirmed_gap'},reason:null}},
    semantics:{route_safety_is_not_cns_compliance:true,
      assessment_completeness_is_not_requirement_verdict:true,
      cns_gap_never_rewrites_route_validation:true,cns_excluded_from_route_cost:true}};
  return {...base,...overrides};
}

test('V3-D adoption model surfaces the gate, projection and adoption state',()=>{
  const flow=v3dFlow({projection:v3dProjection()});
  const model=routePlannerV3AdoptionModel(flow);
  assert.equal(model.stage,'V3-D');
  assert.equal(model.algorithm.registered_in_algorithm_registry,false);
  assert.equal(model.options.length,1);
  assert.equal(model.options[0].eligible,true);
  assert.equal(model.options[0].productionEligible,false);
  assert.equal(model.adoptionCount,1);
  assert.equal(model.currentCount,1);
  assert.equal(model.adoptions[0].adoptionId,'V3D-ABCDEF123456');
  assert.equal(model.adoptions[0].ownership.route_owned,true);
  assert.equal(model.stageOrder.join(','),'P7,P8,P9,P10');
  assert.match(model.separationLabel,/route safety validation 与 CNS 结果严格分离/);
  assert.match(model.publishLabel,/发布为运行分析航路/);
  assert.match(model.syntheticLabel,/不可正式发布/);
  assert.equal(model.neverFinalValidated,true);
});

test('V3-D preview block shows route/profile/provenance and the downstream invalidation',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3dFlow({projection:v3dProjection()});
  const preview={status:'ready',previewId:'V3DPREV-1',previewFingerprint:'V3DPREV-1',
    publicationAllowed:false,evidenceSource:'canonical_synthetic',productionPublication:false,
    syntheticTestOnly:true,syntheticNotice:'canonical synthetic 证据仅用于测试：不可正式发布到 operational_routes',
    routeIds:['R0001'],projections:[v3dProjection()],blocked:[],
    downstreamInvalidation:V3D_DOWNSTREAM_RESULTS,
    operationalRoutesUntouched:true,spatial3dUntouched:true,cnsNotRun:true};
  const html=routePlannerV3AdoptionPanel(flow,preview);
  assert.match(html,/V3-D 发布为运行分析航路/);
  assert.match(html,/状态链（V3-A → V3-B → V3-C → V3-D）/);
  assert.match(html,/V3-D Preview/);
  assert.match(html,/不可正式发布到 operational_routes/);
  assert.match(html,/待发布的 operational projection/);
  assert.match(html,/path 仅二维 \[lon, lat\]/);
  assert.match(html,/locked true（by adoption true）/);
  assert.match(html,/v3_metric_length_m \/ legacy_geodesic_length_m/);
  assert.match(html,/path\/profile 顶点顺序与距离基准一致/);
  assert.match(html,/simplification_applied false/);
  assert.match(html,/crs_mixing false/);
  assert.match(html,/operational_routes \/ spatial_3d \/ CNS/);
  assert.match(html,/untouched=true \/ untouched=true \/ not_run=true/);
  assert.match(html,/TOCTOU/);
  assert.match(html,/id="previewRoutePlannerV3Adoption"/);
  assert.match(html,/id="applyRoutePlannerV3Adoption"/);
  assert.match(html,/id="revokeRoutePlannerV3Adoption"/);
  assert.match(html,/id="v3dConfirmed"/);
  // The panel also renders without a cached preview.
  const bare=routePlannerV3AdoptionPanel(flow,null);
  assert.match(bare,/V3 operational adoptions/);
  assert.match(bare,/V3D-ABCDEF123456/);
});

test('V3-D expects the stored validation fingerprint for the TOCTOU guard',()=>{
  const flow=v3dFlow();
  assert.equal(v3dExpectedFingerprint(flow,['V3C-ABC123DEF456']),'V3CVALID-abc123');
  assert.equal(v3dExpectedFingerprint(flow,[]),'V3CVALID-abc123');
  assert.equal(v3dExpectedFingerprint({route_planner_v3_detail:{records:[]}},[]),null);
});

test('V3-D statuses and vocabulary are closed and never merge safety with compliance',()=>{
  assert.deepEqual(V3D_ADOPTION_STATUSES,['published','stale','revoked']);
  assert.deepEqual(V3D_ASSESSMENT_STATUSES,['not_started','incomplete','complete','stale']);
  assert.deepEqual(V3D_REQUIREMENT_VERDICTS,['meets','does_not_meet','unknown']);
  assert.deepEqual(V3D_STAGES,['P7','P8','P9','P10']);
  assert.equal(V3D_PUBLISH_LABEL,'发布为运行分析航路：只把 current V3-C validated route 投影进既有 operational_routes，不改变 V3 验证结论');
  assert.equal(V3D_SYNTHETIC_LABEL,'canonical synthetic 证据仅用于测试，不可正式发布到 operational_routes');
  for(const wrong of ['operational_route','safe','cns_compliant']){
    assert.ok(!V3D_ASSESSMENT_STATUSES.includes(wrong));
    assert.ok(!V3D_REQUIREMENT_VERDICTS.includes(wrong));
  }
});

test('V3-D CNS summary reports route validation and CNS compliance side by side',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=v3dFlow({assessment:v3dBundle()});
  const html=routePlannerV3CnsSummary(flow);
  assert.match(html,/CNS Assessment Bridge/);
  assert.match(html,/Route validation（V3-C）/);
  assert.match(html,/validated_route/);
  assert.match(html,/Operational publication（V3-D）/);
  assert.match(html,/V3D-ABCDEF123456/);
  assert.match(html,/P7 geometry/);
  assert.match(html,/P8 capability/);
  assert.match(html,/P9 timeline/);
  assert.match(html,/P10 gap/);
  assert.match(html,/does_not_meet_under_model/);
  assert.match(html,/confirmed_gap/);
  assert.match(html,/Assessment completeness（证据完整度）/);
  assert.match(html,/complete/);
  assert.match(html,/Requirement verdict（需求满足度）/);
  assert.match(html,/does_not_meet/);
  assert.match(html,/评估可以 complete 而 verdict 为 does_not_meet/);
  assert.match(html,/CNS 缺口绝不回写成 route validation failed/);
  assert.match(html,/id="assessV3AdoptedRoute"/);
  // A not-yet-published adoption disables the bridge and says so explicitly.
  const unpublished=v3dFlow({adoptions:{status:'not_calculated',count:0,current_count:0,
    stale_count:0,revoked_count:0,items:[]}});
  const bare=routePlannerV3CnsSummary(unpublished);
  assert.match(bare,/未发布/);
  assert.match(bare,/无 V3-C validation/);
  assert.match(bare,/not_started/);
});

// ---- Layered Operational Route Architecture V1 ---------------------------------------

function cruiseFlow(overrides={}){
  return {
    operational_routes:[{route_id:'R0001',status:'passed'}],
    spatial_3d:{altitude_layers:[],route_operating_layers:[],departure_arrival_procedures:[],
      route_altitude_profiles:{}},
    route_operating_readiness:null,
    ...overrides,
  };
}

const CONFIRMED_LAYER={altitude_layer_id:'L-LOW',name:'低层',nominal_altitude_m:100,
  lower_altitude_m:50,upper_altitude_m:150,vertical_reference:'egm2008_orthometric',
  source:'工程确认',confirmed:true,status:'confirmed'};
const CONFIRMED_ASSIGNMENT={route_id:'R0001',altitude_layer_id:'L-LOW',
  operating_mode:CRUISE_LAYER_MODE,vertical_reference:'egm2008_orthometric',
  source:'工程确认',confirmed:true,status:'confirmed',active:true};
const CONFIRMED_DEPARTURE={procedure_id:'DP-1',procedure_type:'departure',route_id:'R0001',
  altitude_layer_id:'L-LOW',transition_mode:'climb_to_cruise_layer',missing_evidence:[],status:'confirmed'};

test('cruise layer vocabulary fixes one operating mode and one production route definition',()=>{
  assert.equal(CRUISE_LAYER_MODE,'fixed_cruise_layer');
  assert.equal(LAYER_PENDING_LABEL,'待工程确认');
  assert.match(PRODUCTION_ROUTE_LABEL,/固定巡航高度层/);
  assert.deepEqual(READINESS_BUCKETS.map(item=>item[0]),
    ['altitude_layer_catalog','route_layer_assignment','departure_procedure','arrival_procedure']);
  assert.match(ADVANCED_PROFILE_LABEL,/不是生产巡航高度层/);
});

test('cruise layer model keeps an unassigned route pending and never reads profile altitudes',()=>{
  const flow=cruiseFlow({spatial_3d:{altitude_layers:[CONFIRMED_LAYER],route_operating_layers:[],
    departure_arrival_procedures:[],route_altitude_profiles:{R0001:{route_id:'R0001',mode:'constant',
      constant_altitude_m:999,vertical_reference:'egm2008_orthometric',source:'user',confirmed:true}}}});
  const model=routeOperatingModel(flow);
  assert.equal(model.semantics.route_layer_is_never_matched_from_a_route_altitude_profile,true);
  assert.equal(model.semantics.assignment_source,'explicit_user_selection_only');
  assert.equal(model.semantics.default_altitudes_provided,false);
  assert.equal(model.confirmed_layer_count,1);
  assert.equal(model.routes[0].assignment,null);
  assert.equal(model.routes[0].cruise_status,'pending_confirmation');
  assert.equal(model.routes[0].nominal_altitude_m,null);
  assert.equal(model.routes[0].has_advanced_profile,true);
  assert.equal(model.routes[0].departure_status,'pending_confirmation');
  assert.equal(model.routes[0].arrival_status,'pending_confirmation');
});

test('cruise layer model reports the four readiness buckets and the explicit assignment',()=>{
  const flow=cruiseFlow({
    spatial_3d:{altitude_layers:[CONFIRMED_LAYER],route_operating_layers:[CONFIRMED_ASSIGNMENT],
      departure_arrival_procedures:[CONFIRMED_DEPARTURE],route_altitude_profiles:{}},
    route_operating_readiness:{status:'pending_confirmation',
      altitude_layer_catalog:{status:'confirmed',reasons:[]},
      route_layer_assignment:{status:'confirmed',reasons:[]},
      departure_procedure:{status:'confirmed',reasons:[]},
      arrival_procedure:{status:'pending_confirmation',reasons:['尚未配置任何 arrival procedure']}},
  });
  const model=routeOperatingModel(flow);
  assert.equal(model.routes[0].cruise_status,'confirmed');
  assert.equal(model.routes[0].nominal_altitude_m,100);
  assert.equal(model.routes[0].vertical_reference,'egm2008_orthometric');
  assert.equal(model.routes[0].departure_status,'confirmed');
  assert.equal(model.routes[0].arrival_status,'pending_confirmation');
  assert.deepEqual(model.readiness_rows.map(row=>row.key),
    ['altitude_layer_catalog','route_layer_assignment','departure_procedure','arrival_procedure']);
  assert.deepEqual(model.readiness_rows.map(row=>row.status),
    ['confirmed','confirmed','confirmed','pending_confirmation']);
  assert.equal(model.procedures.length,1);
});

test('cruise layer panel says 待工程确认 and ships no default real altitude',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const html=renderCruiseLayerPanel(cruiseFlow());
  assert.match(html,/id="cruiseLayerPanel"/);
  assert.match(html,/巡航高度层（生产主模式）/);
  assert.match(html,/待工程确认/);
  // 目录为空时给出明确提示（不是含糊的"尚未配置"），且不放弃待工程确认语义。
  assert.match(html,/高度层目录为空（共 0 层）/);
  assert.match(html,/不会自动选择或推断任何高度/);
  assert.match(html,/固定巡航高度层/);
  assert.match(html,/不根据 RouteAltitudeProfile 数值自动匹配/);
  assert.doesNotMatch(html,/value="(80|100|120|150)"/);
  // no internal R&D phase numbering in the new production panel
  assert.doesNotMatch(html,/P1\b|P7|P13|P20/);
  assert.match(html,/readiness（分开报告）/);
  assert.match(html,/离场 \/ 进场程序/);
  assert.match(html,/不补默认值/);
});

test('cruise layer panel renders the catalogue, the explicit assignment and procedure readiness',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=cruiseFlow({
    spatial_3d:{altitude_layers:[CONFIRMED_LAYER],route_operating_layers:[CONFIRMED_ASSIGNMENT],
      departure_arrival_procedures:[CONFIRMED_DEPARTURE],
      route_altitude_profiles:{R0001:{route_id:'R0001',mode:'constant',constant_altitude_m:999,
        vertical_reference:'egm2008_orthometric',source:'user',confirmed:true}}},
    route_operating_readiness:{status:'pending_confirmation',
      altitude_layer_catalog:{status:'confirmed',reasons:[]},
      route_layer_assignment:{status:'confirmed',reasons:[]},
      departure_procedure:{status:'confirmed',reasons:[]},
      arrival_procedure:{status:'pending_confirmation',reasons:['R0001：缺少 arrival procedure']}},
  });
  const html=renderCruiseLayerPanel(flow);
  assert.match(html,/已配置 AltitudeLayer/);
  assert.match(html,/L-LOW/);
  assert.match(html,/100 m nominal/);
  assert.match(html,/data-save-cruise-layer="R0001"/);
  assert.match(html,/data-delete-cruise-layer="R0001"/);
  assert.match(html,/data-delete-procedure="DP-1"/);
  assert.match(html,/R0001：缺少 arrival procedure/);
  assert.match(html,/id="saveProcedure"/);
  // the advanced/experimental profile altitude never leaks into the production cruise layer
  assert.doesNotMatch(html,/999/);
});

test('step 3 renders the cruise layer panel and keeps the advanced profile surface separate',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=cruiseFlow({nodes:[],scenario_routes:[],algorithm_selection:{route_planner:{}},
    algorithm_catalog:[],operational_timing:{},route_vertical_profiles:{},
    building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},
    reference_landing_sites:{items:[]},route_planning_experiments:{},reference_route_links:{},
    reference_endpoint_candidates:{},workspace:{bbox:[122,29.9,122.2,30.1]},risks:{},steps:{}});
  const html=renderStep3({flow,interactionMode:'pan'});
  assert.match(html,/id="cruiseLayerPanel"/);
  assert.match(html,/生产航路 = 离场程序/);
  assert.match(html,/高级\/实验：Route 3D Altitude Profile/);
  assert.match(html,/advanced_variable_profile/);
  // the old naked constant-altitude production entry is gone: explicit input, no default value
  assert.doesNotMatch(html,/id="routeAltitude" value=/);
  assert.match(html,/id="routeAltitude" placeholder="必须显式输入，无默认值"/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/<input class="panel-input" type="number" id="routeAltitude" value=/);
});

test('step 2 altitude layer editor requests an explicit nominal and confirms nothing by default',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step02_workspace.js',import.meta.url),'utf8');
  assert.match(source,/id="altitudeNominal"/);
  assert.match(source,/id="altitudeLayerConfirmed"/);
  assert.match(source,/nominal_altitude_m:optionalNumber\('altitudeNominal'\)/);
  assert.doesNotMatch(source,/value="(80|100|120|150)"/);
  assert.doesNotMatch(source,/id="altitudeLayerId"[^>]*value="/);
  // the catalogue write goes through the explicit single-layer endpoint
  assert.match(source,/\/api\/spatial-3d\/altitude-layer'/);
});

// ---- Risk Framework V2 (factor → ground / air_traffic / environment_obstacle) -------

function riskV2Flow(){
  return {
    steps:{'2':true},
    workspace:null,
    grid:null,
    grid_attributes:{},
    spatial_3d:{altitude_layers:[]},
    grid_risk:{status:'passed',algorithm_id:'risk-model-v1-relative-index',algorithm_version:'1.1',data_completeness:.8,parameter_status:'default_engineering_parameters'},
    risk_policy_v2:{
      status:'pending_confirmation',parameter_status:'no_default_production_risk_weights',
      domains:{
        ground:{domain_id:'ground',status:'pending_confirmation',method:null,weights:{},required_factors:[],confirmed:false,source:'未配置'},
        air_traffic:{domain_id:'air_traffic',status:'pending_confirmation',method:null,weights:{},required_factors:[],confirmed:false,source:'未配置'},
        environment_obstacle:{domain_id:'environment_obstacle',status:'pending_confirmation',method:null,weights:{},required_factors:[],confirmed:false,source:'未配置'},
      },
    },
    grid_risk_v2:{
      status:'pending_confirmation',algorithm_id:'risk-framework-v2-domains',algorithm_version:'2.0',
      risk_semantics:'relative_engineering_index',
      absolute_risk:{status:'not_computed',value:null},sora_grc:{status:'not_computed',value:null},sora_arc:{status:'not_computed',value:null},
      airspace:{status:'not_applicable',applicability:'display_only',used_in_value_or_fingerprint:false},
      policy_fingerprint:'riskpolicyv2-abc',input_fingerprint:'riskframeworkv2-def',data_completeness:0,
      domains:{
        ground:{domain_id:'ground',status:'pending_confirmation',index:null,aggregation_policy_fingerprint:'riskaggv2-1',unresolved:[],data_completeness:0},
        air_traffic:{domain_id:'air_traffic',status:'pending_confirmation',index:null,aggregation_policy_fingerprint:'riskaggv2-2',unresolved:[],data_completeness:0},
        environment_obstacle:{domain_id:'environment_obstacle',status:'pending_confirmation',index:null,aggregation_policy_fingerprint:'riskaggv2-3',unresolved:[],data_completeness:0},
      },
    },
    risk_framework_v2_readiness:{
      status:'pending_confirmation',
      policy:{status:'pending_confirmation',fingerprint:'riskpolicyv2-abc',default_production_risk_weights:false,domains:{}},
      factors:{
        population_exposure:{factor_id:'population_exposure',domain:'ground',status:'passed',readiness:'ready',cell_count:2,raw_unit:'people/km²',coverage:1,source_role:'population',source_id:'worldpop.tif',source_fingerprint:'risksourcev2-1',normalization:{method:'log1p_ratio_to_dataset_quantile',reference:{value:19.5,fingerprint:'riskrefv2-1'}},quality_flags:[],canonical_source_available:true},
        property_exposure:{factor_id:'property_exposure',domain:'ground',status:'unknown',readiness:'blocked',cell_count:2,raw_unit:null,source_role:'property_exposure',quality_flags:['no_canonical_source'],canonical_source_available:false},
        critical_infrastructure_exposure:{factor_id:'critical_infrastructure_exposure',domain:'ground',status:'unknown',readiness:'blocked',cell_count:2,quality_flags:['no_canonical_source'],canonical_source_available:false},
        uav_traffic_exposure:{factor_id:'uav_traffic_exposure',domain:'air_traffic',status:'passed',readiness:'ready',cell_count:2,raw_unit:'relative_index_0_1',source_role:'traffic',normalization:{method:'canonical_normalized_field',reference:{fingerprint:'riskrefv2-2'}},quality_flags:[],canonical_source_available:true},
        conflict_exposure:{factor_id:'conflict_exposure',domain:'air_traffic',status:'passed',readiness:'ready',cell_count:2,source_role:'conflict',normalization:{method:'canonical_normalized_field',reference:{fingerprint:'riskrefv2-3'}},quality_flags:[],canonical_source_available:true},
        terrain_relief:{factor_id:'terrain_relief',domain:'environment_obstacle',status:'passed',readiness:'ready',cell_count:2,raw_unit:'m',source_role:'terrain',normalization:{method:'ratio_to_dataset_quantile',reference:{value:19.2,fingerprint:'riskrefv2-4'}},quality_flags:[],canonical_source_available:true},
        building_coverage:{factor_id:'building_coverage',domain:'environment_obstacle',status:'passed',readiness:'ready',cell_count:2,raw_unit:'ratio_0_1',source_role:'buildings',quality_flags:['building_coverage_is_not_sheltering'],canonical_source_available:true},
        building_height:{factor_id:'building_height',domain:'environment_obstacle',status:'partial',readiness:'partial',cell_count:2,raw_unit:'m',source_role:'buildings',quality_flags:['partial_building_height_coverage'],canonical_source_available:true},
      },
      domains:{
        ground:{domain_id:'ground',status:'pending_confirmation',index:null,index_scope:'per_cell_only',aggregation_policy_fingerprint:'riskaggv2-1',data_completeness:0,unresolved:[]},
        air_traffic:{domain_id:'air_traffic',status:'pending_confirmation',index:null,index_scope:'per_cell_only',aggregation_policy_fingerprint:'riskaggv2-2',data_completeness:0,unresolved:[]},
        environment_obstacle:{domain_id:'environment_obstacle',status:'unresolved',index:null,index_scope:'per_cell_only',aggregation_policy_fingerprint:'riskaggv2-3',data_completeness:0,unresolved:['building_height']},
      },
      not_computed:{absolute_risk:{status:'not_computed'},sora_grc:{status:'not_computed'},sora_arc:{status:'not_computed'}},
      airspace:{status:'not_applicable',applicability:'display_only'},
    },
  };
}

test('step 2 risk framework V2 workbench separates factors, domains and legacy V1',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=riskV2Flow();
  const model=riskFrameworkV2Model(flow);
  assert.equal(model.riskSemantics,'relative_engineering_index');
  assert.equal(model.defaultProductionRiskWeights,false);
  assert.equal(model.absoluteRisk,'not_computed');
  assert.equal(model.soraGrc,'not_computed');
  assert.equal(model.soraArc,'not_computed');
  assert.equal(model.airspaceApplicability,'display_only');
  assert.equal(model.policyStatus,'pending_confirmation');
  assert.equal(model.factors.length,8);
  assert.equal(model.domains.length,3);
  assert.deepEqual(model.pendingDomains,['ground','air_traffic','environment_obstacle']);
  assert.equal(model.domains.find(item=>item.domainId==='ground').indexAvailable,false);
  assert.equal(model.domains.find(item=>item.domainId==='ground').index,null);
  assert.equal(model.domains.find(item=>item.domainId==='environment_obstacle').unresolved[0],'building_height');
  assert.equal(model.factors.find(item=>item.factorId==='uav_traffic_exposure').domain,'air_traffic');
  assert.equal(model.factors.find(item=>item.factorId==='property_exposure').canonicalSourceAvailable,false);

  const html=renderRiskFrameworkV2Panel(flow);
  assert.match(html,/Risk Framework V2/);
  assert.match(html,/relative_engineering_index/);
  assert.match(html,/聚合策略待确认/);
  assert.match(html,/未配置（无默认权重）/);
  assert.match(html,/not_computed/);
  assert.match(html,new RegExp(LEGACY_RISK_V1_LABEL));
  assert.match(html,/flight_count\/flight_seconds/);
  assert.match(html,/不是事故概率|不是<\/b>事故概率/);
  assert.doesNotMatch(html,/P1\b|P7\b|P13\b/);
  const step2=renderStep2({flow,draftWorkspace:null,gridDisplay:{outline:true,theme:'none'},populationDisplayLabel:()=>'',formatNumber:String});
  assert.match(step2,/Risk Framework V2/);
  assert.match(step2,new RegExp(LEGACY_RISK_V1_LABEL+' · 地面风险'));
  assert.match(step2,new RegExp(LEGACY_RISK_V1_LABEL+' · 综合风险'));
  assert.match(step2,/V2 因子 · 人口暴露/);
  assert.match(step2,/V2 域 · Ground/);
  assert.match(step2,/id="evaluateRiskV2"/);
});

test('no production risk weight ships in the V2 frontend or default policy',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/risk_framework_v2.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/overall_weights/);
  assert.doesNotMatch(source,/0\.7|0\.3/);
  assert.doesNotMatch(source,/weights:\{'ground'|weights:\{ground/);
  const options=riskV2ThemeOptions().map(item=>item[0]);
  assert.ok(options.includes('risk_v2:factor:population_exposure'));
  assert.ok(options.includes('risk_v2:domain:ground'));
  assert.ok(options.includes('ground_risk'));
  assert.ok(options.includes('overall_risk'));
  const labels=riskV2ThemeOptions().map(item=>item[1]).join('|');
  assert.match(labels,new RegExp(LEGACY_RISK_V1_LABEL+' · 地面风险'));
  assert.doesNotMatch(labels,/地面交通/);
});

test('risk V2 map layers never paint a pending domain index as zero',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const riskV2={cells:{
    A:{factors:{terrain_relief:{status:'passed',normalized_index:.5,raw_value:10}},domains:{ground:{status:'pending_confirmation',index:null}}},
    B:{factors:{terrain_relief:{status:'missing_data',normalized_index:null,raw_value:null}},domains:{ground:{status:'pending_confirmation',index:null}}},
  }};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true,bboxIntersects:()=>true,colorForValue:()=>'#abc',NO_DATA_COLOR:'#gray',formatNumber:String};
  const cache=buildGridOverlayCache(grid,{},{},theme,riskV2);
  assert.deepEqual(cache.v2Breaks.factors.get('terrain_relief'),[.5]);
  assert.deepEqual(cache.v2Breaks.domains.get('ground'),[]);
  const calls=[],ctx={save(){},restore(){},fillRect(){calls.push(['fill',this.fillStyle])},strokeRect(){},setLineDash(){}};
  drawGridTheme({ctx,view:{},flow:{grid_risk_v2:riskV2},cache,display:{theme:'risk_v2:domain:ground'},visibleBounds:()=>[0,0,180,90],screenPoint:p=>p,gridTheme:theme,palettes:{population:[],terrain:[],buildings:[],risk:['#abc']},riskBreaks:[]});
  assert.deepEqual(calls,[['fill','#gray'],['fill','#gray']]);
  const factorCalls=[],factorCtx={save(){},restore(){},fillRect(){factorCalls.push(['fill',this.fillStyle])},strokeRect(){},setLineDash(){}};
  drawGridTheme({ctx:factorCtx,view:{},flow:{grid_risk_v2:riskV2},cache,display:{theme:'risk_v2:factor:terrain_relief'},visibleBounds:()=>[0,0,180,90],screenPoint:p=>p,gridTheme:theme,palettes:{population:[],terrain:[],buildings:[],risk:['#abc']},riskBreaks:[]});
  assert.deepEqual(factorCalls,[['fill','#abc'],['fill','#gray']]);
  const legend=riskV2LegendModel('risk_v2:factor:terrain_relief',cache,theme);
  assert.match(legend.title,/Risk Framework V2/);
  assert.equal(legend.selection.id,'terrain_relief');
  assert.equal(riskV2LegendModel('ground_risk',cache,theme),null);
  const summary=riskV2CellSummary(riskV2.cells.A,String);
  assert.match(summary,/Risk Framework V2/);
  assert.match(summary,/ground/);
  assert.match(summary,/not_computed/);
  assert.doesNotMatch(summary,/overall_weights/);
});

// ---- Layered Risk-Aware Route Planner V1 -------------------------------------------

function layeredFlow(overrides={}){
  const base={
    scenario_routes:[
      {route_id:'R0001',direction:'N001→N002',start_node_id:'N001',end_node_id:'N002'},
    ],
    layered_route_planning_request:{status:'confirmed',confirmed:true,source:'工程确认-测试',
      scenario_route_id:'R0001',altitude_layer_id:'L8-LOW'},
    layered_route_feasibility_policy:{status:'confirmed',terrain_vertical_clearance_m:50,
      source:'工程确认-测试',fingerprint:'layeredfeasv1-abc',parameter_status:'explicit'},
    layered_route_cost_policy:{status:'confirmed',ground_lambda:2,air_traffic_lambda:0,
      environment_obstacle_lambda:null,source:'工程确认-测试',parameter_status:'explicit'},
    layered_route_planner_readiness:{
      status:'blocked',
      algorithm:{algorithm_id:'layered_route_planner_v1',algorithm_version:'1.0'},
      request:{status:'confirmed',scenario_route_id:'R0001',altitude_layer_id:'L8-LOW',confirmed:true},
      altitude_layer_catalog:{status:'configured',count:1,altitude_layer_ids:['L8-LOW'],
        selected_altitude_layer_id:'L8-LOW',
        cruise_altitude:{status:'confirmed',altitude_egm2008_m:300,vertical_reference:'egm2008_orthometric'}},
      scenario_route:{status:'resolved',route_id:'R0001',count:1},
      feasibility_policy:{status:'confirmed',terrain_vertical_clearance_m:50,fingerprint:'layeredfeasv1-abc'},
      cost_policy:{status:'confirmed',fingerprint:'layeredcostv1-abc',active_domains:['ground'],
        parameter_status:'explicit',domains:{
          ground:{lambda:2,enabled:true,configured:true},
          air_traffic:{lambda:0,enabled:false,configured:true},
          environment_obstacle:{lambda:null,enabled:false,configured:false},
        }},
      risk_framework_v2:{status:'passed',input_fingerprint:'riskv2-input',policy_fingerprint:'riskv2-policy',overall_used:false},
      building_clearance_policy:{status:'confirmed',vertical_clearance_m:10,reused_not_redefined:true},
      feasibility_mask:{status:'passed',counts:{feasible:3,blocked:1,unknown:1},mask_fingerprint:'layeredmaskv1-abc'},
      blockers:[{reason_code:'cost_weights_not_configured',reason:'λ 未全部确认'}],
      airspace:{status:'not_applicable',applicability:'display_only',used_in_mask_search_or_fingerprint:false},
      semantics:{no_default_clearance_or_lambda:true},
    },
    layered_route_candidates:{
      status:'passed',count:1,active_candidate_id:'LRC-R0001-L8-LOW-1',
      current_key:'R0001@L8-LOW',current_candidate_fingerprint:'layeredcandv1-current',
      items:[{
        candidate_id:'LRC-R0001-L8-LOW-1',route_id:'R0001',altitude_layer_id:'L8-LOW',
        lane_key:'R0001@L8-LOW',status:'candidate',current_applicability:'current',
        candidate_fingerprint:'layeredcandv1-current',feasibility_fingerprint:'layeredfeasibilityv1-a',
        risk_fingerprint:'layeredriskv1-a',policy_fingerprint:'layeredpolicyv1-a',
        distance_m:1200,optimization_cost:1800,detour_factor:1.2,grid_path:['A','B','C'],
        operational_route:false,cns_assessed:false,continuous_validation_required:true,
        route_operating_layer_created:false,search_incomplete:false,
        cost_breakdown:{distance_contribution_m:1200,
          lambda_weighted_contributions_m:{ground:600,air_traffic:null,environment_obstacle:null},
          domain_exposure_index_m:{ground:300,air_traffic:null,environment_obstacle:null},
          mean_domain_index:{ground:.25,air_traffic:null,environment_obstacle:null},
          lambdas:{ground:2,air_traffic:0,environment_obstacle:null},active_domains:['ground']},
      }],
      masks:{'R0001@L8-LOW':{status:'passed',altitude_layer_id:'L8-LOW',current_applicability:'current',
        mask_fingerprint:'layeredmaskv1-abc',counts:{feasible:3,blocked:1,unknown:1},
        airspace:{applicability:'display_only',used_in_mask:false},
        cells:{
          A:{grid_id:'A',status:'feasible',reason_code:null,reason:null},
          B:{grid_id:'B',status:'blocked',reason_code:'altitude_below_building_clearance_floor',reason:'low'},
          C:{grid_id:'C',status:'unknown',reason_code:'terrain_data_unavailable',reason:'nodata'},
        }}},
    },
  };
  return {...base,...overrides};
}

function layeredGrid(){
  return {cells:[
    {grid_id:'A',bbox:[122.0,30.0,122.01,30.01],center:[122.005,30.005]},
    {grid_id:'B',bbox:[122.01,30.0,122.02,30.01],center:[122.015,30.005]},
    {grid_id:'C',bbox:[122.02,30.0,122.03,30.01],center:[122.025,30.005]},
  ]};
}

test('layered planner model exposes readiness, policy and candidate facts',()=>{
  const model=layeredRoutePlannerModel(layeredFlow());
  assert.equal(model.status,'blocked');
  assert.equal(model.selectedRouteId,'R0001');
  assert.equal(model.selectedLayerId,'L8-LOW');
  assert.equal(model.cruiseAltitude.altitude_egm2008_m,300);
  assert.equal(model.blockers[0].reason_code,'cost_weights_not_configured');
  assert.equal(model.feasibility.clearance,50);
  assert.equal(model.cost.domains.length,3);
  assert.equal(model.cost.anyNull,true);
  assert.equal(model.cost.domains[0].state,'启用（λ>0）');
  assert.equal(model.cost.domains[1].state,'关闭（显式 0）');
  assert.equal(model.cost.domains[2].state,'待确认（null）');
  assert.equal(model.risk.overall_used,false);
  assert.equal(model.candidates.count,1);
  assert.equal(model.candidates.items[0].operationalRoute,false);
  assert.equal(model.candidates.items[0].continuousValidationRequired,true);
  assert.equal(model.candidates.items[0].routeOperatingLayerCreated,false);
  assert.equal(LAYERED_PLANNER_ALGORITHM_TYPE,'layered_route_planner');
});

test('layered planner panel never invents a default height, clearance or lambda',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=layeredFlow();
  const html=renderLayeredRoutePlannerPanel(flow);
  assert.match(html,/Layered Risk-Aware Route Planner V1/);
  assert.match(html,/coarse_strategic_vertical_envelope/);
  assert.match(html,/id="layeredAltitudeLayerSelect"/);
  assert.match(html,/id="layeredRouteSelect"/);
  assert.match(html,/id="layeredTerrainClearance" type="number" step="0.1" placeholder="必填，无默认值"/);
  assert.match(html,/id="layeredGroundLambda" type="number" step="0.1" placeholder="留空 = null（待确认）"/);
  assert.match(html,new RegExp(LAYERED_BLOCKED_NOTE));
  assert.match(html,/cost_weights_not_configured/);
  assert.match(html,/λ 启用（λ&gt;0） · 配置值 2\.0000/);
  assert.match(html,/λ 关闭（显式 0） · 配置值 0\.0000/);
  assert.match(html,/λ 待确认（null） · 配置值 null（待确认）/);
  assert.match(html,/unknown ≠ feasible ≠ blocked/);
  assert.match(html,/不得直接写入运行航路或 CNS/);
  assert.match(html,/不会写入 operational_routes \/ CNS/);
  assert.doesNotMatch(html,/\bP1\b|\bP13\b/);
  // A blank project shows no invented altitude / clearance / lambda: every field starts empty.
  const blank=layeredFlow();
  blank.layered_route_planning_request={status:'pending_confirmation',confirmed:false,source:null,
    scenario_route_id:null,altitude_layer_id:null};
  blank.layered_route_feasibility_policy={status:'blocked',terrain_vertical_clearance_m:null,
    source:'未配置；必须由项目工程依据显式确认 terrain_vertical_clearance_m',fingerprint:'layeredfeasv1-empty'};
  blank.layered_route_cost_policy={status:'pending_confirmation',ground_lambda:null,
    air_traffic_lambda:null,environment_obstacle_lambda:null,source:'未配置'};
  blank.layered_route_planner_readiness={...blank.layered_route_planner_readiness,
    status:'blocked',blockers:[
      {reason_code:'route_identity_not_selected',reason:'尚未确认显式规划请求'},
      {reason_code:'terrain_vertical_clearance_not_configured',reason:'无默认净空'},
      {reason_code:'cost_weights_not_configured',reason:'无默认 λ'},
    ],
    feasibility_policy:{status:'blocked',terrain_vertical_clearance_m:null,fingerprint:'layeredfeasv1-empty'},
    cost_policy:{status:'pending_confirmation',fingerprint:null,active_domains:[],parameter_status:'no_default_lambda',
      domains:{
        ground:{lambda:null,enabled:false,configured:false},
        air_traffic:{lambda:null,enabled:false,configured:false},
        environment_obstacle:{lambda:null,enabled:false,configured:false},
      }},
    altitude_layer_catalog:{status:'not_configured',count:0,altitude_layer_ids:[],
      selected_altitude_layer_id:null,cruise_altitude:{status:'blocked',altitude_egm2008_m:null,reason:'altitude_layer_missing'}},
    feasibility_mask:{status:'not_calculated',counts:{},mask_fingerprint:null}};
  blank.layered_route_candidates={status:'not_calculated',count:0,active_candidate_id:null,items:[],masks:{}};
  const blankHtml=renderLayeredRoutePlannerPanel(blank);
  assert.doesNotMatch(blankHtml,/<input[^>]*type="number"[^>]*value="[0-9]/);
  assert.match(blankHtml,/no default|无默认|待确认/);
  assert.match(blankHtml,/placeholder="必填，无默认值"/);
  assert.match(blankHtml,/placeholder="留空 = null（待确认）"/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_route_planner.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/\b(80|100|120|150)\b\s*;?\s*\/\/\s*default/);
  assert.doesNotMatch(source,/defaultValue/);
});

test('layered evaluate payload maps blank inputs to null and 0 stays an explicit value',()=>{
  const fields={
    layeredRouteSelect:'R0001',layeredAltitudeLayerSelect:'L8-LOW',
    layeredRequestSource:'工程确认-测试',layeredRequestConfirmed:true,
    layeredTerrainClearance:'',layeredFeasibilitySource:'',layeredFeasibilityConfirmed:false,
    layeredGroundLambda:'0',layeredAirLambda:'',layeredEnvironmentLambda:'1.5',
    layeredCostSource:'工程确认-测试',layeredCostConfirmed:true,
  };
  const get=id=>{
    const value=fields[id];
    if(typeof value==='boolean')return {checked:value,value:''};
    return {checked:false,value:value===undefined?'':String(value)};
  };
  const payload=layeredEvaluatePayload(get);
  assert.equal(payload.request.altitude_layer_id,'L8-LOW');
  assert.equal(payload.feasibility.terrain_vertical_clearance_m,null);
  assert.equal(payload.cost.ground_lambda,0);
  assert.equal(payload.cost.air_traffic_lambda,null);
  assert.equal(payload.cost.environment_obstacle_lambda,1.5);
  assert.equal(payload.cost.confirmed,true);
  const request=layeredRequestPayload(layeredFlow());
  assert.equal(request.scenario_route_id,'R0001');
  assert.equal(request.confirmed,true);
  assert.equal(COST_DOMAIN_LABELS.air_traffic,'Air / Traffic（空中交通暴露）');
});

test('layered feasibility overlay draws only the selected layer with three verdicts',()=>{
  const flow=layeredFlow();
  const cells=layeredFeasibilityCells(flow);
  assert.equal(cells.length,3);
  assert.equal(cells.find(item=>item.gridId==='A').status,'feasible');
  assert.equal(cells.find(item=>item.gridId==='B').status,'blocked');
  assert.equal(cells.find(item=>item.gridId==='C').status,'unknown');
  assert.equal(cells.find(item=>item.gridId==='C').color,LAYERED_FEASIBILITY_COLORS.unknown);
  const model=layeredOverlayModel(flow);
  assert.equal(model.available,true);
  assert.deepEqual(model.counts,{feasible:3,blocked:1,unknown:1});
  assert.equal(model.currentApplicability,'current');
  const candidate=currentLayeredCandidate(flow);
  assert.equal(candidate.candidate_id,'LRC-R0001-L8-LOW-1');
  const legend=layeredFeasibilityLegend();
  assert.deepEqual(legend.map(item=>item.status),['feasible','blocked','unknown']);
  // switch the selected layer: the overlay follows the lane, and a missing lane draws nothing
  const other=layeredFlow();
  other.layered_route_planning_request={...other.layered_route_planning_request,altitude_layer_id:'L8-HIGH'};
  assert.equal(layeredFeasibilityCells(other).length,0);
  assert.equal(layeredOverlayModel(other).available,false);
  assert.equal(currentLayeredCandidate(other),null);
});

test('layered feasibility overlay paints categorical cells only and never a candidate path',()=>{
  const flow=layeredFlow();
  const fills=[],strokes=[];
  const ctx={save(){},restore(){},fill(){fills.push(this.fillStyle)},stroke(){strokes.push(this.strokeStyle)},
    beginPath(){},moveTo(){},lineTo(){},closePath(){},setLineDash(){},globalAlpha:1};
  const theme={bboxIntersects:()=>true};
  const drawn=drawLayeredFeasibilityOverlay({
    ctx,view:{x:0,y:0,res:1},screenPoint:point=>[point[0],point[1]],flow,grid:layeredGrid(),gridTheme:theme,
  });
  assert.equal(drawn.cells,3);
  // Layered Route Map Evidence V1：feasibility overlay 以后不得再绘制 candidate route
  assert.equal(drawn.path,0,'the coarse feasibility mask never draws a candidate route');
  assert.deepEqual(strokes,[],'the feasibility mask only fills cells');
  assert.deepEqual(fills,[
    LAYERED_FEASIBILITY_COLORS.feasible,
    LAYERED_FEASIBILITY_COLORS.blocked,
    LAYERED_FEASIBILITY_COLORS.unknown,
  ]);
  // a candidate that is not current is not drawn as the selected-layer path
  const stale=layeredFlow();
  stale.layered_route_candidates={...stale.layered_route_candidates,
    items:[{...stale.layered_route_candidates.items[0],current_applicability:'stale'}]};
  assert.equal(currentLayeredCandidate(stale),null);
});

test('drawWorkflowLayers keeps the candidate layer and the evidence highlight independent',()=>{
  const flow={scenario_routes:[],operational_routes:[],nodes:[],reference_landing_sites:{items:[]},
    layered_route_planning_request:{scenario_route_id:'R0001',altitude_layer_id:'L8-LOW'},
    layered_route_candidates:{status:'passed',count:1,active_candidate_id:'LRC-R0001-L8-LOW-1',
      items:[{candidate_id:'LRC-R0001-L8-LOW-1',route_id:'R0001',altitude_layer_id:'L8-LOW',
        lane_key:'R0001@L8-LOW',status:'candidate',current_applicability:'current',
        path:[[122.0,30.0],[122.001,30.0004],[122.01,30.01]],grid_path:['A','B','C']}],
      masks:{'R0001@L8-LOW':{status:'passed',cells:{A:{grid_id:'A',status:'feasible'}}}}},
    grid:{cells:layeredGrid().cells}};
  const strokes=[];
  const ctx={save(){},restore(){},fill(){},stroke(){strokes.push({color:this.strokeStyle,width:this.lineWidth})},
    beginPath(){},moveTo(){},lineTo(){},closePath(){},setLineDash(){},globalAlpha:1};
  const plan={styles:ROUTE_STYLES.detail,pending:[],referenceRoutes:[],referencePoints:[],
    landingSites:[],nodes:[],priorityIds:new Set(),priorityCoordinates:new Set(),selectedScreen:null,
    selectedReferenceId:null,clusterCounts:{nodes:0,sites:0},level:'detail',levelLabel:'细节',
    resolution:'1.0 km/px',referencePointsVisible:false};
  const draw=input=>drawWorkflowLayers({ctx,view:{x:0,y:0,res:1},flow,screenPoint:point=>[point[0],point[1]],
    plan,layers:{layeredFeasibilityLayer:false,layeredCandidateLayer:true},gridTheme:{bboxIntersects:()=>true},
    drawWorkspace(){},drawGridThemes(){},drawGridBoundaries(){},...input});
  // 候选层打开：画 candidate 几何（Theta* any-angle 折线）
  draw({});
  assert.equal(strokes.length,1);
  assert.equal(strokes[0].color,'#123a5c');
  assert.equal(strokes[0].width,ROUTE_STYLES.detail.operationalWidth+0.4);
  // 候选层关闭：candidate 几何消失，feasibility 掩码仍然独立可画
  strokes.length=0;
  draw({layers:{layeredFeasibilityLayer:false,layeredCandidateLayer:false}});
  assert.deepEqual(strokes,[]);
  strokes.length=0;
  draw({layers:{layeredFeasibilityLayer:true,layeredCandidateLayer:false}});
  assert.deepEqual(strokes,[],'the feasibility mask fills cells and never strokes a route');
  // 临时 highlight 画在正常路线（含 candidate）之上：白描边 + evidence 色
  strokes.length=0;
  draw({layers:{layeredFeasibilityLayer:true,layeredCandidateLayer:true},
    routeEvidenceHighlight:{source:'route_risk_profile',profileId:'RRP-1',candidateId:'LRC-R0001-L8-LOW-1',
      segmentIds:['RRP-S0000'],path:[[122.0,30.0],[122.001,30.0]]}});
  assert.deepEqual(strokes,[
    {color:'#123a5c',width:ROUTE_STYLES.detail.operationalWidth+0.4},
    {color:'#ffffff',width:9},
    {color:'#c0392b',width:6},
  ],'the highlight is drawn after the candidate route');
  assert.equal(JSON.stringify(flow).includes('routeEvidenceHighlight'),false,
    'the highlight must never be written into the snapshot');
});

test('layered candidate vocabulary is closed and never claims operational status',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_route_planner.js',import.meta.url),'utf8');
  assert.match(source,/LAYERED_CANDIDATE_LABEL='分层候选（candidate，非运行航路）'/);
  assert.doesNotMatch(source,/operational_route:true|route_operating_layer_created:true/);
  const html=renderLayeredRoutePlannerPanel(layeredFlow());
  assert.match(html,/candidate 不会写入 operational_routes \/ CNS，也不会自动创建 RouteOperatingLayer。/);
  assert.doesNotMatch(html,/Risk V2 overall 使用|overall 参与 cost/);
  // a blocked run surfaces the candidate label with the explicit blocking reason
  const blocked=layeredFlow();
  blocked.layered_route_candidates={...blocked.layered_route_candidates,
    items:[{candidate_id:null,route_id:'R0001',altitude_layer_id:'L8-LOW',status:'blocked',
      current_applicability:'current',blocking_reasons:[{reason_code:'cost_weights_not_configured',reason:'λ 未确认'}]}]};
  const blockedHtml=renderLayeredRoutePlannerPanel(blocked);
  assert.match(blockedHtml,new RegExp('分层候选（candidate，非运行航路）'));
  assert.match(blockedHtml,/blocking：cost_weights_not_configured/);
});

// ---- Layered Risk-Aware Theta* V2 ---------------------------------------------------

/** 与后端 Theta* V2 snapshot 同形的 flow：legacy λ 仍然未确认，但 λ 不是 V2 的 blocker。 */
function thetaV2Flow(overrides={}){
  const base={
    scenario_routes:[
      {route_id:'R0001',direction:'N001→N002',start_node_id:'N001',end_node_id:'N002'},
    ],
    layered_route_planning_request:{status:'confirmed',confirmed:true,source:'工程确认-测试',
      scenario_route_id:'R0001',altitude_layer_id:'L8-LOW'},
    layered_route_feasibility_policy:{status:'confirmed',terrain_vertical_clearance_m:50,
      source:'工程确认-测试',fingerprint:'layeredfeasv2-abc',parameter_status:'explicit'},
    layered_route_cost_policy:{status:'pending_confirmation',ground_lambda:null,air_traffic_lambda:null,
      environment_obstacle_lambda:null,source:'未配置',parameter_status:'no_default_lambda'},
    layered_route_planner_readiness:{
      status:'blocked',
      algorithm:{algorithm_id:'layered_risk_aware_theta_star_v2',algorithm_version:'2.0'},
      request:{status:'confirmed',scenario_route_id:'R0001',altitude_layer_id:'L8-LOW',confirmed:true},
      altitude_layer_catalog:{status:'configured',count:1,altitude_layer_ids:['L8-LOW'],
        selected_altitude_layer_id:'L8-LOW',
        cruise_altitude:{status:'confirmed',altitude_egm2008_m:300,vertical_reference:'egm2008_orthometric'}},
      scenario_route:{status:'resolved',route_id:'R0001',count:1},
      feasibility_policy:{status:'confirmed',terrain_vertical_clearance_m:50,fingerprint:'layeredfeasv2-abc'},
      cost_policy:{status:'pending_confirmation',fingerprint:null,active_domains:[],parameter_status:'no_default_lambda',
        domains:{
          ground:{lambda:null,enabled:false,configured:false},
          air_traffic:{lambda:null,enabled:false,configured:false},
          environment_obstacle:{lambda:null,enabled:false,configured:false},
        }},
      risk_framework_v2:{status:'passed',input_fingerprint:'riskv2-input',policy_fingerprint:'riskv2-policy',overall_used:false},
      theta_star_v2:{
        algorithm:{algorithm_id:'layered_risk_aware_theta_star_v2',algorithm_version:'2.0',uses_theta_star:true},
        status:'ready',blockers:[],
        population_shelter:{status:'passed',cell_count:4,resolved_cell_count:4,field_fingerprint:'popshelterv1-abc',
          shelter_coefficient_policy:{status:'confirmed',default_coefficient:1,source:'user_defined_baseline',
            provenance:'user_defined_baseline',confirmed:true},
          raw_exposure_definition:'population_density_people_km2 * shelter_coefficient',
          risk_index_definition:'normalized_population_factor * shelter_coefficient',risk_index_range:[0,1]},
        objective:{formula:'J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + distance_weight * distance_m',
          risk_weight:0.8,turn_weight:0.1,distance_weight:0.1,provenance:'user_defined_baseline',
          objective_population_shelter_only:true},
        evaluation_constraint:{metric:'route_risk_density',threshold:1,
          source:'user_defined_temporary_wide_constraint',temporary:true,
          role:'candidate_evaluation_acceptance_constraint',objective_term:false},
        regulatory_constraints:{status:'not_evaluated',regulatory_compliance:'not_evaluated',
          constraint_dataset_status:'not_configured',constraint_count:0,confirmed_constraint_count:0,
          dataset_fingerprint:null,configured:false,
          statement:'未配置任何 regulatory constraint 数据集：本轮 regulatory_compliance=not_evaluated。'},
        communication:{interface:'communication_planning_field',status:'not_configured',provider:null,source:null,
          cell_count:0,informational_fingerprint:'commsfieldv1-empty',used_in_cost:false,used_as_constraint:false,
          affected_path_or_cost:false,readiness:'interface_declared_no_field_configured'},
        airspace:{status:'not_applicable',applicability:'display_only',used_in_search:false,
          used_in_hard_gate:false,used_in_fingerprint:false},
      },
      building_clearance_policy:{status:'confirmed',vertical_clearance_m:10,reused_not_redefined:true},
      feasibility_mask:{status:'passed',counts:{feasible:3,blocked:1,unknown:1},
        mask_fingerprint:'layeredmaskv2-abc',current_applicability:'current'},
      // 后端 readiness 里仍然带 legacy λ blocker：Theta* V2 绝不把它当 blocker。
      blockers:[{reason_code:'cost_policy_not_confirmed',
        reason:'LayeredRouteCostPolicy 未确认：λ 无默认值（null != 0）'}],
      sources:{terrain:{available:true,reason:null},population:{available:true,reason:null}},
    },
    shelter_coefficient_policy:{schema_version:'population-shelter-v1',status:'confirmed',status_reason:null,
      default_coefficient:1,per_grid_overrides:{},unit:'dimensionless_shelter_coefficient_0_1',range:[0,1],
      source:'user_defined_baseline',confirmed:true,provenance:'user_defined_baseline',
      parameter_status:'explicit_confirmed_coefficient'},
    population_shelter:{schema_version:'population-shelter-v1',attribute:'population_shelter',status:'passed',
      source:'derived',grid_level:8,count:4,covered_count:4,field_fingerprint:'popshelterv1-abc',
      shelter_coefficient_policy_fingerprint:'shelterpolicyv1-abc',cells:{
        A:{grid_id:'A',status:'passed'},B:{grid_id:'B',status:'passed'},
        C:{grid_id:'C',status:'passed'},D:{grid_id:'D',status:'passed'},
      }},
    regulatory_constraints:{status:'not_configured',count:0,items:[],dataset_fingerprint:null},
    communication_planning_field:{status:'not_configured',provider:null,count:0,cells:{}},
    theta_v2_objective_policy:{schema_version:'layered-theta-star-v2',status:'confirmed',
      risk_weight:0.8,turn_weight:0.1,distance_weight:0.1,source:'user_defined_baseline',
      provenance:'user_defined_baseline',confirmed:true,algorithm_policy_baseline:true,
      weights_are_editable:true,sum_constraint:1,sum_tolerance:1e-9,
      formula:'J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + distance_weight * distance_m',
      objective_population_shelter_only:true},
    max_route_risk_density:{schema_version:'layered-theta-star-v2',constraint_id:'max_route_risk_density',
      metric:'route_risk_density',threshold:1,comparison:'less_than_or_equal',
      unit:'dimensionless_length_weighted_mean_index',source:'user_defined_temporary_wide_constraint',
      confirmed:true,temporary:true,provenance:'user_defined_temporary_wide_constraint',status:'confirmed',
      role:'candidate_evaluation_acceptance_constraint',objective_term:false,changes_objective_weights:false},
    layered_route_candidates:{
      status:'passed',count:1,current_key:'R0001@L8-LOW',current_candidate_fingerprint:'thetacandv2-current',
      items:[{
        candidate_id:'LRC-R0001-L8-LOW-V2',route_id:'R0001',altitude_layer_id:'L8-LOW',lane_key:'R0001@L8-LOW',
        status:'candidate',current_applicability:'current',
        algorithm_id:'layered_risk_aware_theta_star_v2',algorithm_version:'2.0',
        distance_m:1234.567891,optimization_cost:321.307651,grid_path:['A','B','C'],
        planning_objective:{
          risk_exposure_index_m:246.913578,turn_count:2,total_heading_change_deg:47.5,turn_cost_m:3.2,
          distance_m:1234.567891,risk_weight:0.8,turn_weight:0.1,distance_weight:0.1,
          weighted_risk:197.530862,weighted_turn:0.32,weighted_distance:123.456789,total_cost:321.307651,
          formula:'J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + distance_weight * distance_m',
          objective_population_shelter_only:true,risk_v2_overall_used:false,
          route_risk_density_is_not_an_objective_term:true,policy_fingerprint:'thetaobjv2-abc',
          provenance:{definition:'domain/layered_theta_v2.py'},
        },
        route_risk_density:{metric:'route_risk_density',unit:'dimensionless_length_weighted_mean_index',
          definition:'risk_exposure_index_m / distance_m',risk_exposure_index_m:246.913578,
          distance_m:1234.567891,value:0.2,threshold:1,margin:0.8,status:'passed',reason:null,
          comparison:'less_than_or_equal',source:'user_defined_temporary_wide_constraint',
          temporary_constraint:true,confirmed:true,objective_term:false,changes_objective_weights:false},
        turn_statistics:{turn_count:2,total_heading_change_deg:47.5,turn_cost_m:3.2,
          turns:[{from_heading_deg:0,to_heading_deg:45,heading_change_deg:45,cost_m:2.8}],
          theta_min_deg:5,d_ref_m:100,semantics:'planning_smoothness_proxy_not_flight_dynamics_validation'},
        search_statistics:{expanded_labels:120,generated_labels:340,los_checks:88,los_shortcuts:31,
          rejected_terrain:2,rejected_building:1,rejected_regulatory:0,rejected_unknown:0,
          rejected_hard_constraint:0,rejected_outside_grid:0,rewired_parent_shortcuts:7,
          heading_bin_count:8,theta_min_deg:5,d_ref_m:100,
          d_ref_provenance:'derived_from_current_mh_t_l8_grid_typical_centre_to_centre_step_median_of_adjacent_cell_distances',
          search_completeness:'complete',
          search_limit:{max_expanded_labels:null,limit_reached:false,safety_parameter:false},
          risk_unresolved_cell_count:0},
        los_segments:[
          {from_grid_id:'A',to_grid_id:'B',length_m:600,risk_exposure_index_m:120.5,
            traversed_cells:['A','B'],outgoing_heading_deg:45,incoming_heading_deg:0,
            supercover:true,shortcut:true},
          {from_grid_id:'B',to_grid_id:'C',length_m:634.567891,risk_exposure_index_m:126.413578,
            traversed_cells:['B','C'],outgoing_heading_deg:90,incoming_heading_deg:45,
            supercover:true,shortcut:true},
        ],
        blocking_reasons:[],search_incomplete:false,mask_status:'passed',
        candidate_fingerprint:'thetacandv2-current',feasibility_fingerprint:'thetav2-feas',
        risk_fingerprint:'thetav2-risk',policy_fingerprint:'thetav2-policy',
        request_fingerprint:'thetav2-request',input_fingerprint:'thetav2-input',
        feasibility_mask_fingerprint:'layeredmaskv2-abc',
        communication_informational_fingerprint:'commsfieldv1-empty',
        provenance:{pipeline:'scenario_or_od_route -> explicit_fixed_altitude_H -> layered_route_candidate',
          search_semantics:{algorithm:'theta_star_any_angle_with_parent_los_rewiring'},
          feasibility_semantics:'coarse_strategic_vertical_envelope',
          risk_density_constraint:{threshold:1,objective_term:false,changes_objective_weights:false},
          communication:{readiness:'interface_declared_no_field_configured',affected_path_or_cost:false},
          airspace:{applicability:'display_only'}},
      }],
      masks:{'R0001@L8-LOW':{status:'passed',altitude_layer_id:'L8-LOW',current_applicability:'current',
        mask_fingerprint:'layeredmaskv2-abc',counts:{feasible:3,blocked:1,unknown:1},cells:{}}},
    },
  };
  return {...base,...overrides};
}

/** Theta* V2 / V1 共用的 bind 桩：只登记面板真实存在的控件。 */
function thetaV2BindHarness(flow,fields={}){
  const calls=[],registered=[],handlers={};
  const ids=new Set([...Object.keys(fields),'saveLayeredRequest','saveLayeredFeasibilityPolicy',
    'saveLayeredCostPolicy','saveThetaV2ShelterPolicy','saveThetaV2ObjectivePolicy',
    'saveThetaV2RiskDensity','evaluateLayeredCandidate']);
  const c={
    flow:()=>flow,
    $:id=>ids.has(id)
      ?{value:fields[id]===undefined?'':String(fields[id]),checked:fields[id]===true,attributes:{},dataset:{}}
      :null,
    panelError:message=>calls.push(['__error',message]),
    resourceAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
    actionButton:(id,handler)=>{registered.push(id);handlers[id]=handler;},
  };
  return {c,calls,registered,handlers};
}

test('the layered candidate bind switch follows the same exact algorithm id',async()=>{
  // V1：仍然绑定 legacy λ 流程（cost policy 保存 + evaluate 要求 λ）
  const v1=thetaV2BindHarness(layeredFlow());
  bindLayeredCandidatePanel(v1.c);
  assert.deepEqual(v1.registered,['saveLayeredRequest','saveLayeredFeasibilityPolicy',
    'saveLayeredCostPolicy','evaluateLayeredCandidate']);
  await v1.handlers.evaluateLayeredCandidate();
  assert.equal(v1.calls.length,1);
  assert.equal(v1.calls[0][0],'__error');
  assert.match(v1.calls[0][1],/λ/,'V1 仍然要求 legacy λ');

  // V2：绝不绑定 legacy cost policy，也不把 λ 当 blocker
  const v2=thetaV2BindHarness(thetaV2Flow());
  bindLayeredCandidatePanel(v2.c);
  assert.deepEqual(v2.registered,['saveLayeredRequest','saveLayeredFeasibilityPolicy',
    'saveThetaV2ShelterPolicy','saveThetaV2ObjectivePolicy','saveThetaV2RiskDensity',
    'evaluateLayeredCandidate']);
  assert.equal(v2.registered.includes('saveLayeredCostPolicy'),false,
    'Theta* V2 不得再注册 legacy cost policy 保存控件');
  await v2.handlers.evaluateLayeredCandidate();
  assert.deepEqual(v2.calls,[['/api/layered-route-candidates/evaluate-real',{}]]);
});

test('the layered candidate view is chosen by the exact algorithm id only',()=>{
  assert.equal(layeredPlannerUsesThetaStarV2(thetaV2Flow()),true);
  assert.equal(layeredPlannerUsesThetaStarV2(layeredFlow()),false);
  // 缺失算法信息或未知 id 时绝不冒充 Theta* V2：退回既有 V1 面板
  assert.equal(layeredPlannerUsesThetaStarV2({}),false);
  assert.equal(layeredPlannerUsesThetaStarV2({layered_route_planner_readiness:{algorithm:{algorithm_id:'risk_aware_route_planner_v2'}}}),false);

  const v1Html=layeredCandidatePanel(layeredFlow());
  assert.match(v1Html,/Layered Risk-Aware Route Planner V1/);
  assert.match(v1Html,/id="layeredGroundLambda"/);
  assert.doesNotMatch(v1Html,/Theta\* V2 candidate/);

  const v2Html=layeredCandidatePanel(thetaV2Flow());
  assert.ok(v2Html.includes(THETA_STAR_V2_PANEL_TITLE),'Theta* V2 标题必须出现');
  assert.match(v2Html,/layered_risk_aware_theta_star_v2/);
  assert.doesNotMatch(v2Html,/id="layeredGroundLambda"|id="layeredCostSource"|id="saveLayeredCostPolicy"/,
    'Theta* V2 视图不得再渲染 legacy λ cost policy 控件');
  assert.doesNotMatch(v2Html,/id="layeredRouteSelect"[^>]*A\* \+ legacy λ/);
});

test('theta v2 never treats the legacy lambda cost policy as a blocker',async()=>{
  const flow=thetaV2Flow();
  const model=layeredThetaV2Model(flow);
  assert.equal(model.blockers.legacyV1.length,1,'后端 readiness 的 legacy λ blocker 仍然原样保留');
  assert.equal(model.blockers.legacyV1[0].reasonCode,'cost_policy_not_confirmed');
  assert.equal(model.blockers.thetaV2.length,0,'legacy λ 不得进入 Theta* V2 的 blocker 集合');
  assert.equal(isLegacyV1CostBlocker({reason_code:'cost_weights_not_configured'}),true);
  assert.equal(isLegacyV1CostBlocker({reason_code:'population_shelter_field_missing'}),false);

  const html=renderLayeredThetaV2Panel(flow);
  assert.match(html,/不是<\/b> Theta\* V2 的 blocker/);
  assert.match(html,/legacy V1 λ blocker/);
  assert.doesNotMatch(html,/必须补齐 λ|请先补齐 confirmed 的高度层、clearance 与 λ|λ 未全部确认[\s\S]{0,40}blocker/,
    'Theta* V2 视图不得提示"必须补齐 λ"');
  assert.doesNotMatch(html,/id="evaluateLayeredCandidate" disabled/,
    'legacy λ 不得禁用 Theta* V2 的运行按钮');

  const {c,calls,handlers}=thetaV2BindHarness(flow);
  bindLayeredThetaV2(c);
  await handlers.evaluateLayeredCandidate();
  assert.equal(calls.some(call=>call[0]==='__error'),false,'λ 不确认时不得报 blocking 错误');
  assert.deepEqual(calls.pop(),['/api/layered-route-candidates/evaluate-real',{}]);

  // 真正的 Theta* V2 blocker（population_shelter 场缺失）仍然阻止运行
  const blocked=thetaV2Flow();
  blocked.layered_route_planner_readiness={
    ...blocked.layered_route_planner_readiness,
    theta_star_v2:{...blocked.layered_route_planner_readiness.theta_star_v2,
      status:'not_ready',
      blockers:[{reason_code:'population_shelter_field_missing',
        reason:'population_shelter 场缺失：risk weight>0 时 fail-closed，绝不补 0'}]},
  };
  const blockedHtml=renderLayeredThetaV2Panel(blocked);
  assert.match(blockedHtml,/population_shelter_field_missing/);
  assert.match(blockedHtml,/id="evaluateLayeredCandidate" disabled/);
  const blockedHarness=thetaV2BindHarness(blocked);
  bindLayeredThetaV2(blockedHarness.c);
  await blockedHarness.handlers.evaluateLayeredCandidate();
  assert.deepEqual(blockedHarness.calls,[['__error',THETA_V2_EVALUATE_BLOCKED_NOTE]]);
});

test('theta v2 panel renders the six first-visual layers in order with unique ids',()=>{
  const flow=thetaV2Flow();
  const model=layeredThetaV2Model(flow);
  assert.equal(model.algorithmId,'layered_risk_aware_theta_star_v2');
  assert.equal(model.algorithmVersion,'2.0');
  assert.equal(model.expectedAlgorithmId,THETA_STAR_V2_ALGORITHM_ID);
  const html=renderLayeredThetaV2Panel(flow);
  const order=['① 规划请求','② terrain / building feasibility','②b Regulatory','③ Population × Shelter',
    '④ Objective','⑤ Route Risk Density','⑥ Theta* V2 candidate 结果'];
  let cursor=-1;
  for(const marker of order){
    const at=html.indexOf(marker);
    assert.ok(at>cursor,`${marker} 必须按第一视觉层顺序出现`);
    cursor=at;
  }
  for(const id of ['layeredRouteSelect','layeredAltitudeLayerSelect','layeredRequestSource','layeredRequestConfirmed',
    'layeredTerrainClearance','layeredFeasibilitySource','layeredFeasibilityConfirmed','saveLayeredRequest',
    'saveLayeredFeasibilityPolicy','evaluateLayeredCandidate','thetaV2ShelterCoefficient','thetaV2ShelterSource',
    'thetaV2ShelterConfirmed','saveThetaV2ShelterPolicy','thetaV2RiskWeight','thetaV2TurnWeight',
    'thetaV2DistanceWeight','thetaV2ObjectiveSource','thetaV2ObjectiveConfirmed','saveThetaV2ObjectivePolicy',
    'thetaV2RiskDensityThreshold','thetaV2RiskDensitySource','thetaV2RiskDensityConfirmed',
    'thetaV2RiskDensityTemporary','saveThetaV2RiskDensity']){
    assert.ok(html.includes('id="'+id+'"'),`#${id} 必须存在`);
  }
  const ids=Array.from(html.matchAll(/id="([^"]+)"/g),match=>match[1]);
  assert.equal(new Set(ids).size,ids.length,
    `duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
});

test('theta v2 transcription: population x shelter, objective and risk density come from backend fields',()=>{
  const flow=thetaV2Flow();
  const model=layeredThetaV2Model(flow);
  assert.equal(model.shelter.policy.defaultCoefficient,1);
  assert.equal(model.shelter.policy.confirmed,true);
  assert.equal(model.shelter.field.cellCount,4);
  assert.equal(model.shelter.field.resolvedCellCount,4);
  assert.equal(model.shelter.field.unresolvedCellIds.length,0);
  assert.equal(model.shelter.field.fieldFingerprint,'popshelterv1-abc');
  assert.equal(model.shelter.field.riskIndexDefinition,'normalized_population_factor * shelter_coefficient');
  assert.equal(model.objective.riskWeight,0.8);
  assert.equal(model.objective.turnWeight,0.1);
  assert.equal(model.objective.distanceWeight,0.1);
  assert.equal(model.objective.weightsSum,1);
  assert.equal(model.objective.sumOk,true);
  assert.equal(model.objective.baseline,true);
  assert.equal(model.riskDensity.threshold,1);
  assert.equal(model.riskDensity.temporary,true);
  assert.equal(model.riskDensity.objectiveTerm,false);
  assert.equal(model.riskDensity.changesObjectiveWeights,false);
  assert.equal(model.regulatory.configured,false);
  assert.equal(model.communication.affectedPathOrCost,false);

  const html=renderLayeredThetaV2Panel(flow);
  assert.match(html,/risk_index = normalized population factor × shelter coefficient/);
  assert.match(html,/不是事故概率/);
  assert.match(html,/missing ≠ 0/);
  assert.match(html,/绝不补 0/);
  assert.match(html,/route_risk_density = risk_exposure_index_m \/ distance_m/);
  assert.match(html,/不是第四个 objective term/);
  assert.match(html,/J = wr\*E_risk \+ wt\*C_turn \+ wd\*L/);
  assert.match(html,/当前版本 communication 不影响 path \/ cost/);
  assert.match(html,/regulatory_compliance=not_evaluated|not_evaluated/);
});

test('theta v2 candidate metrics are transcribed verbatim and the boundaries stay explicit',()=>{
  const flow=thetaV2Flow();
  const model=layeredThetaV2Model(flow);
  const candidate=model.candidates.current;
  assert.ok(candidate,'current candidate 必须被识别');
  assert.equal(candidate.isCurrent,true);
  assert.equal(candidate.objective.riskExposureIndexM,246.913578);
  assert.equal(candidate.objective.turnCount,2);
  assert.equal(candidate.objective.totalHeadingChangeDeg,47.5);
  assert.equal(candidate.objective.turnCostM,3.2);
  assert.equal(candidate.objective.distanceM,1234.567891);
  assert.equal(candidate.objective.riskWeight,0.8);
  assert.equal(candidate.objective.turnWeight,0.1);
  assert.equal(candidate.objective.distanceWeight,0.1);
  assert.equal(candidate.objective.weightedRisk,197.530862);
  assert.equal(candidate.objective.weightedTurn,0.32);
  assert.equal(candidate.objective.weightedDistance,123.456789);
  assert.equal(candidate.objective.totalCost,321.307651);
  assert.equal(candidate.routeRiskDensity.value,0.2);
  assert.equal(candidate.routeRiskDensity.threshold,1);
  assert.equal(candidate.turnStatistics.turnCount,2);
  assert.equal(candidate.searchStatistics.rewired_parent_shortcuts,7);
  assert.equal(candidate.los.segmentCount,2);

  const html=renderLayeredThetaV2Panel(flow);
  for(const value of ['246.913578','47.500000','3.200000','1234.567891','197.530862','0.320000',
    '123.456789','321.307651','0.200000']){
    assert.ok(html.includes(value),`candidate 指标 ${value} 必须原样展示`);
  }
  for(const field of ['risk_exposure_index_m','turn_count','total_heading_change_deg','turn_cost_m','distance_m',
    'risk_weight','turn_weight','distance_weight','weighted_risk','weighted_turn','weighted_distance','total_cost']){
    assert.ok(html.includes('data-theta-v2-candidate-field="'+field+'"'),`${field} 必须以转印行展示`);
  }
  assert.match(html,/rewired_parent_shortcuts/);
  assert.match(html,/LOS-1/);
  assert.match(html,/LOS-2/);
  assert.match(html,/Theta\* V2 candidate ≠ operational route/);
  assert.match(html,/不自动 adopt/);
  assert.match(html,/RouteRiskProfile 是独立的 post-hoc 多域分析/);
  assert.match(html,/与 Theta\* objective 里的 population × shelter risk 不是同一件事/);
  // 前端不新增 objective / 风险 / 距离重算
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_theta_v2.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/haversine|6371008\.8|computeDistance|Math\.asin|risk_weight\s*\*?\s*=.*normalize/i);
  assert.doesNotMatch(source,/normalizeWeight|renormaliz|归一化\(/);
});

test('theta v2 policy POST contracts keep overrides, never normalize weights and never guess thresholds',async()=>{
  // shelter policy：per_grid_overrides 必须原样回传（保存绝不清空）
  const flow=thetaV2Flow();
  flow.shelter_coefficient_policy={...flow.shelter_coefficient_policy,default_coefficient:0.75,
    per_grid_overrides:{A:0.25,B:0.5},source:'工程确认-测试',provenance:'explicit_override'};
  const harness=thetaV2BindHarness(flow,{
    thetaV2ShelterCoefficient:'0.6',thetaV2ShelterSource:'工程确认-新依据',thetaV2ShelterConfirmed:true,
    thetaV2RiskWeight:'0.5',thetaV2TurnWeight:'0.5',thetaV2DistanceWeight:'0.5',
    thetaV2ObjectiveSource:'工程确认-测试',thetaV2ObjectiveConfirmed:true,
    thetaV2RiskDensityThreshold:'',thetaV2RiskDensitySource:'',thetaV2RiskDensityConfirmed:false,
    thetaV2RiskDensityTemporary:true,
  });
  bindLayeredThetaV2(harness.c);
  assert.deepEqual(harness.registered,['saveLayeredRequest','saveLayeredFeasibilityPolicy',
    'saveThetaV2ShelterPolicy','saveThetaV2ObjectivePolicy','saveThetaV2RiskDensity','evaluateLayeredCandidate']);
  const model=layeredThetaV2Model(flow);
  assert.deepEqual(thetaV2ShelterPolicyPayload(harness.c,model.shelter.policy),{
    default_coefficient:0.6,per_grid_overrides:{A:0.25,B:0.5},source:'工程确认-新依据',
    confirmed:true,provenance:'explicit_override',
  });
  // 未修改时必须沿用后端 provenance，而不是无端改写成 explicit_override
  const unchanged=thetaV2BindHarness(flow,{
    thetaV2ShelterCoefficient:'0.75',thetaV2ShelterSource:'工程确认-测试',thetaV2ShelterConfirmed:true,
  });
  assert.deepEqual(thetaV2ShelterPolicyPayload(unchanged.c,model.shelter.policy),{
    default_coefficient:0.75,per_grid_overrides:{A:0.25,B:0.5},source:'工程确认-测试',
    confirmed:true,provenance:'explicit_override',
  });
  // objective：原样提交，绝不静默归一化（0.5/0.5/0.5 直接送后端校验）
  assert.deepEqual(thetaV2ObjectivePolicyPayload(harness.c,model.objective),{
    risk_weight:0.5,turn_weight:0.5,distance_weight:0.5,source:'工程确认-测试',confirmed:true,
  });
  // risk density：空 threshold 提交 null（不猜值），其余字段照原样
  assert.deepEqual(thetaV2RiskDensityPayload(harness.c,model.riskDensity),{
    threshold:null,source:'user_defined_temporary_wide_constraint',confirmed:false,temporary:true,
  });
  // 端点契约：三个 POST 路径与 payload 一一对应
  await harness.handlers.saveThetaV2ShelterPolicy();
  assert.deepEqual(harness.calls.pop(),['/api/shelter-coefficient-policy',
    thetaV2ShelterPolicyPayload(harness.c,model.shelter.policy)]);
  await harness.handlers.saveThetaV2ObjectivePolicy();
  assert.deepEqual(harness.calls.pop(),['/api/theta-v2-objective-policy',
    thetaV2ObjectivePolicyPayload(harness.c,model.objective)]);
  await harness.handlers.saveThetaV2RiskDensity();
  assert.deepEqual(harness.calls.pop(),['/api/max-route-risk-density',
    thetaV2RiskDensityPayload(harness.c,model.riskDensity)]);
  await harness.handlers.saveLayeredRequest();
  assert.equal(harness.calls.pop()[0],'/api/layered-route-planning-request');
  await harness.handlers.saveLayeredFeasibilityPolicy();
  assert.equal(harness.calls.pop()[0],'/api/layered-route-feasibility-policy');

  // objective 权重和≠1 时只提示，不静默归一化
  const offSum=thetaV2Flow();
  offSum.theta_v2_objective_policy={...offSum.theta_v2_objective_policy,
    risk_weight:0.5,turn_weight:0.5,distance_weight:0.5,provenance:'explicit_override'};
  const offModel=layeredThetaV2Model(offSum);
  assert.equal(offModel.objective.weightsSum,1.5);
  assert.equal(offModel.objective.sumOk,false);
  const offHtml=renderLayeredThetaV2Panel(offSum);
  assert.match(offHtml,/后端会拒绝；前端不做静默归一化/);
  assert.match(offHtml,/最终以后端校验为准/);
});

test('step 03 mounts the theta v2 candidate panel without duplicate ids or legacy lambda controls',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow={
    ...thetaV2Flow(),
    nodes:[{node_id:'N001',name:'A',coordinate:[122,30]},{node_id:'N002',name:'B',coordinate:[122.1,30.1]}],
    operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],risks:{},steps:{3:true},
    spatial_3d:{altitude_layers:[{altitude_layer_id:'L8-LOW',name:'低层',nominal_altitude_m:300,
      vertical_reference:'egm2008_orthometric',status:'confirmed'}],route_altitude_profiles:{}},
    operational_timing:{route_motion_profiles:{}},route_vertical_profiles:{},
    building_clearance_policy:{status:'confirmed',vertical_clearance_m:10},building_clearance_assessment:{},
    reference_routes:{items:[],points:[]},reference_landing_sites:{items:[]},
    route_planning_experiments:{},reference_route_links:{items:[]},reference_endpoint_candidates:{},
    workspace:{bbox:[122,29.9,122.2,30.1]},
  };
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  // 「操作 → 分层候选」分区不变，只有面板内容切换成 Theta* V2
  assert.ok(html.includes('data-seg-name="op-candidates"')||html.includes('分层候选'),'分层候选分区必须保留');
  assert.match(html,/Layered Risk-Aware Theta\* V2/);
  assert.match(html,/⑥ Theta\* V2 candidate 结果/);
  assert.match(html,/<option value="L8-LOW" selected>/,'显式 AltitudeLayer 必须可选中');
  assert.doesNotMatch(html,/id="layeredGroundLambda"|id="layeredCostSource"|id="saveLayeredCostPolicy"/,
    'Theta* V2 页面不得再出现 legacy λ cost policy 控件');
  const ids=Array.from(html.matchAll(/id="([^"]+)"/g),match=>match[1]);
  assert.equal(new Set(ids).size,ids.length,
    `duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
});

// ---------------------------------------------------------------- altitude layer catalog lifecycle

test('step 03 altitude layer dropdown reads the restored catalog and never invents a layer',()=>{
  // 项目恢复补建的工程默认高度层：catalog 走 flow.spatial_3d.altitude_layers（readiness 的
  // catalog 只带 id 列表），下拉必须逐层来自后端，且不自动选中任何一层。
  const flow=layeredFlow({
    spatial_3d:{altitude_layers:[
      {altitude_layer_id:'ALT-060',name:'60 m 巡航高度层',nominal_altitude_m:60,
        lower_altitude_m:40,upper_altitude_m:70,vertical_reference:'egm2008_orthometric',
        source:'工程默认高度层（软件基线）',confirmed:true,status:'confirmed'},
      {altitude_layer_id:'ALT-080',name:'80 m 巡航高度层',nominal_altitude_m:80,
        lower_altitude_m:70,upper_altitude_m:90,vertical_reference:'egm2008_orthometric',
        source:'工程默认高度层（软件基线）',confirmed:true,status:'confirmed'},
      {altitude_layer_id:'ALT-100',name:'100 m 巡航高度层',nominal_altitude_m:100,
        lower_altitude_m:90,upper_altitude_m:125,vertical_reference:'egm2008_orthometric',
        source:'工程默认高度层（软件基线）',confirmed:true,status:'confirmed'},
      {altitude_layer_id:'ALT-150',name:'150 m 巡航高度层',nominal_altitude_m:150,
        lower_altitude_m:125,upper_altitude_m:175,vertical_reference:'egm2008_orthometric',
        source:'工程默认高度层（软件基线）',confirmed:true,status:'confirmed'},
      {altitude_layer_id:'ALT-200',name:'200 m 巡航高度层',nominal_altitude_m:200,
        lower_altitude_m:175,upper_altitude_m:250,vertical_reference:'egm2008_orthometric',
        source:'工程默认高度层（软件基线）',confirmed:true,status:'confirmed'},
    ]},
  });
  flow.layered_route_planning_request={status:'pending_confirmation',confirmed:false,source:null,
    scenario_route_id:'R0001',altitude_layer_id:null};
  flow.layered_route_planner_readiness={...flow.layered_route_planner_readiness,
    request:{status:'pending_confirmation',scenario_route_id:'R0001',altitude_layer_id:null,confirmed:false},
    altitude_layer_catalog:{status:'configured',count:5,
      altitude_layer_ids:['ALT-060','ALT-080','ALT-100','ALT-150','ALT-200'],selected_altitude_layer_id:null,
      cruise_altitude:{status:'blocked',altitude_egm2008_m:null,reason:'altitude_layer_missing'}}};

  const model=layeredPlanningRequestModel(flow);
  assert.equal(model.layerCatalogStatus,'configured');
  assert.deepEqual(model.layers.map(layer=>layer.layerId),
    ['ALT-060','ALT-080','ALT-100','ALT-150','ALT-200']);
  assert.deepEqual(model.layers.map(layer=>layer.nominal),[60,80,100,150,200]);
  // 目录有 5 层不等于替用户选择：selectedLayerId 保持 null。
  assert.equal(model.selectedLayerId,null);

  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const html=renderLayeredRoutePlannerPanel(flow);
  assert.match(html,/<option value="ALT-060"/);
  assert.match(html,/<option value="ALT-080"/);
  assert.match(html,/<option value="ALT-100"/);
  assert.match(html,/<option value="ALT-150"/);
  assert.match(html,/<option value="ALT-200"/);
  assert.doesNotMatch(html,/<option value="ALT-[0-9]+" selected>/,'默认高度层不得被自动选中');
});

test('step 03 shows an explicit notice when the altitude layer catalog is empty',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const flow=thetaV2Flow();
  flow.layered_route_planning_request={status:'pending_confirmation',confirmed:false,source:null,
    scenario_route_id:null,altitude_layer_id:null};
  flow.layered_route_planner_readiness={...flow.layered_route_planner_readiness,
    status:'blocked',
    request:{status:'pending_confirmation',scenario_route_id:null,altitude_layer_id:null,confirmed:false},
    altitude_layer_catalog:{status:'not_configured',count:0,altitude_layer_ids:[],
      selected_altitude_layer_id:null,
      cruise_altitude:{status:'blocked',altitude_egm2008_m:null,reason:'altitude_layer_missing'}},
    blockers:[{reason_code:'altitude_layer_not_found',reason:'selected AltitudeLayer 不存在'}],
    // 后端 Theta* V2 readiness 同时报告该高度层 blocker：前端只转印，不自行判断。
    theta_star_v2:{...flow.layered_route_planner_readiness.theta_star_v2,
      status:'blocked',
      blockers:[{reason_code:'altitude_layer_not_found',reason:'selected AltitudeLayer 不存在：None'}]},
  };
  flow.spatial_3d={altitude_layers:[]};

  const model=layeredThetaV2Model(flow);
  assert.equal(model.request.layers.length,0);
  assert.equal(model.request.layerCatalogStatus,'not_configured');
  const html=renderLayeredThetaV2Panel(flow);
  // 明确提示 + 后端 blocker 原文，且按钮保持 disabled：提示不等于放行高度检查。
  assert.ok(html.includes(THETA_V2_EMPTY_ALTITUDE_CATALOG_NOTE),'必须显示目录为空的明确提示');
  assert.match(html,/not_configured · 共 0 层/);
  assert.match(html,/高度层目录为空/);
  assert.match(html,/<option value="">高度层目录为空/);
  assert.match(html,/altitude_layer_not_found/);
  assert.match(html,/id="evaluateLayeredCandidate" disabled/,'目录为空时不得放行 Theta* V2');
  assert.match(html,/没有可选高度层/);
});

