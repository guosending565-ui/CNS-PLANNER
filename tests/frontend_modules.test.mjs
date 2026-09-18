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
import {filterReferenceSites,referenceOverlayModel,render as renderStep3,riskAwareRoutePanel,plannerCardModel,routePlannerComparisonModel,effectiveParameters,findAlgorithmManifest,routeExperimentModel,routePlanningDiagnosticsModel,referenceLinkModel,routePlannerV3Model,routePlannerV3ReadinessModel,routePlannerV3Panel,routePlannerV3ValidationPanel,routePlannerV3ContinuousModel,routePlannerV3ValidationModel,routePlannerV3AdoptionPanel,routePlannerV3AdoptionModel,v3dExpectedFingerprint,V3_RESULT_STATUSES,V3B_RESULT_STATUSES,V3B_REFINED_LABEL,V3C_RESULT_STATUSES,V3C_DOMAINS,V3C_EVIDENCE_SOURCES,V3C_OPERATIONAL_LABEL,V3D_ADOPTION_STATUSES,V3D_ASSESSMENT_STATUSES,V3D_REQUIREMENT_VERDICTS,V3D_STAGES,V3D_DOWNSTREAM_RESULTS,V3D_PUBLISH_LABEL,V3D_SYNTHETIC_LABEL,V3D_CNS_SEPARATION_LABEL} from '../cns_planner/web/js/workflow/step03_routes.js';
import {routePlannerV3CnsSummary} from '../cns_planner/web/js/workflow/step04_operation.js';
import {v3OverlayModel} from '../cns_planner/web/js/map/route_planner_v3_overlay.js';
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
  assert.match(html,/provenance sources \["grid_risk\.ground\.population"\]/);
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
  assert.match(html,/minimum_turn_radius_m \* \|dpsi\| <= stride_m/);
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
  assert.match(html,/changed_components \["source_fingerprint"\]/);
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
