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
import {filterReferenceSites,referenceOverlayModel,render as renderStep3,riskAwareRoutePanel,plannerCardModel,routePlannerComparisonModel,effectiveParameters,findAlgorithmManifest,routeExperimentModel,routePlanningDiagnosticsModel,referenceLinkModel,airspacePolicyReadinessModel} from '../cns_planner/web/js/workflow/step03_routes.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6,planReviewSummary} from '../cns_planner/web/js/workflow/step06_review.js';
import {sourceModeText,statusText} from '../cns_planner/web/js/workflow/common.js';
import {profileChart,renderRouteVerticalProfilePanel} from '../cns_planner/web/js/workflow/route_vertical_profile.js';
import {protectionBudgetModel,renderProtectionBudget} from '../cns_planner/web/js/workflow/protection_budget.js';
import {encounterFrame,renderDaaEncounterLab} from '../cns_planner/web/js/workflow/daa_encounter_lab.js';

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

test('map exposes independent source airspace confirmed allowed and reference layer toggles',()=>{
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  for(const id of ['allowedAirspaceLayer','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','referenceRouteStatus','referenceRoutePointStatus','referenceLandingStatus','reference_landing_sitesPath','reference_routesPath','buildingClearanceLayer','terrain_dtmPath'])assert.match(html,new RegExp('id="'+id+'"'));
  assert.match(html,/空域源图层（非政策结论）/);
  assert.match(html,/适飞空域（confirmed allowed）/);
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

test('step 3 airspace policy readiness counts only explicit policy values',()=>{
  const flow={airspace_policies:{items:[{feature_id:'A',route_eligibility:'allowed',confirmed:true,source:{type:'doc'}},{feature_id:'B',route_eligibility:'unknown',confirmed:false,source:{type:'doc'}}]},data_readiness:{status:'partial',blocks:{airspace_policies:{status:'passed',count:2,route_eligibility_counts:{allowed:1,blocked:0,unknown:1},confirmed_count:1,unconfirmed_count:1,v2_readiness:{status:'blocked',reason:'only_unknown_policies_present'},never_inferred_from_layer_name_or_color:true}}}};
  const model=airspacePolicyReadinessModel(flow);
  assert.equal(model.count,2);
  assert.deepEqual(model.eligibilityCounts,{allowed:1,blocked:0,unknown:1});
  assert.equal(model.confirmedCount,1);
  assert.equal(model.neverInferred,true);
  assert.equal(model.v2Readiness.status,'blocked');
  const html=renderStep3({flow:{...flow,nodes:[],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},reference_landing_sites:{items:[]},route_planning_experiments:{},reference_route_links:{},reference_endpoint_candidates:{},workspace:{bbox:[122,29.9,122.2,30.1]},risks:{},steps:{}},interactionMode:'pan',selectedReference:null});
  assert.match(html,/AirspacePolicy 就绪总览/);
  assert.match(html,/绝不按图层颜色或名称自动推断/);
  assert.match(html,/V2 readiness/);
});

test('step 3 data readiness panel reports reference CRS and ET policy',()=>{
  const flow={nodes:[],scenario_routes:[],operational_routes:[],algorithm_selection:{route_planner:{}},algorithm_catalog:[],spatial_3d:{},operational_timing:{},route_vertical_profiles:{},building_clearance_policy:{},building_clearance_assessment:{},reference_routes:{items:[]},reference_landing_sites:{items:[]},route_planning_experiments:{},reference_route_links:{},reference_endpoint_candidates:{},airspace_policies:{items:[]},workspace:{bbox:[122,29.9,122.2,30.1]},risks:{},steps:{},
    data_readiness:{status:'partial',reference_route_link_count:2,experiment_count:1,et_source_policy:'requires_xlsx_or_csv_conversion',blocks:{reference_landing_sites:{label:'landing_site',status:'not_calculated',count:0,source_crs:{value:null,status:'pending_confirmation'},representation_crs:{},source_crs_resolved:false,metric_measurement_status:'disabled_unresolved_source_crs'},reference_routes:{label:'reference_route',status:'passed',count:3,format:'csv',source_crs:{value:null,status:'pending_confirmation'},representation_crs:{},source_crs_resolved:false,metric_measurement_status:'disabled_unresolved_source_crs'},airspace_policies:{status:'pending_confirmation',count:0,route_eligibility_counts:{allowed:0,blocked:0,unknown:0},confirmed_count:0,unconfirmed_count:0,v2_readiness:{status:'blocked',reason:'no_confirmed_allowed_airspace_policy'}}}}};
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/数据就绪/);
  assert.match(html,/source_crs/);
  assert.match(html,/representation_crs/);
  assert.match(html,/disabled_unresolved_source_crs/);
  assert.match(html,/不提供 ET parser/);
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

test('P18 review summary keeps selection confirmation and apply separate',()=>{
  const result=planReviewSummary({selected_variant_id:'PV-1',variants:[{variant_id:'PV-1',selected_action_ids:['A'],evaluation:{confirmation_gate:{status:'ready_for_confirmation'}}}]},{status:'confirmed',application:{status:'not_applied'}});
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
