import assert from 'node:assert/strict';
import test from 'node:test';

import {lonLatToMercator,mercatorToLonLat} from '../cns_planner/web/js/map/projection.js';
import {createStore} from '../cns_planner/web/js/state/store.js';
import {buildGridOverlayCache,findGridCell} from '../cns_planner/web/js/map/grid_overlay.js';
import {algorithmManifestDetails,algorithmSelectionKey} from '../cns_planner/web/js/workflow/step01_project.js';
import {render as renderStep4,withLegacyRequiredAliases} from '../cns_planner/web/js/workflow/step04_operation.js';
import {render as renderStep2} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {render as renderStep3,riskAwareRoutePanel} from '../cns_planner/web/js/workflow/step03_routes.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6} from '../cns_planner/web/js/workflow/step06_review.js';

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

test('population theme prefers governed target-grid density and excludes partial NoData',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{status:'missing_data',cells:{
    A:{status:'passed',quantity_status:'passed',population_density_people_km2:25,value_mean:999},
    B:{status:'passed',quantity_status:'missing_data',population_density_people_km2:50,value_mean:888}
  }},terrain:{cells:{}}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.deepEqual(cache.populationBreaks,[25]);
});

test('algorithm selection key includes type id and exact version',()=>{
  assert.equal(algorithmSelectionKey({algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1'}),'risk_model|risk-model-v1-relative-index|1.1');
});

test('step 1 algorithm details preserve auditable manifest fields',()=>{
  const item={algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1',name:'Risk V1',provider:'CNS-PLANNER',maturity:'baseline',description:'relative',inputs:['grid'],outputs:['risk'],parameter_schema:{type:'object'},assumptions:['a'],limitations:['b'],references:[]};
  assert.deepEqual(algorithmManifestDetails(item),{identity:'risk-model-v1-relative-index@1.1',provider:'CNS-PLANNER',maturity:'baseline',inputs:'grid',outputs:'risk',assumptions:'a',limitations:'b',references:'未登记'});
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

test('step 4 separates aircraft capability and required performance UI',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}});return node;}};
  const html=renderStep4({flow:{aircraft_profiles:{count:1,items:[{aircraft_id:'A',name:'A',communication:{capabilities:['radio'],type:{technology:'4g'},confirmed:true},navigation:{},surveillance:{}}]},selected_aircraft_profile_id:'A',aircraft_source:'catalog',scenario_routes:[],required_cns:{project_default:{}},device_catalog:{items:[]},steps:{'4':false}}});
  assert.match(html,/Aircraft Capability/);
  assert.match(html,/Required CNS Performance/);
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
  assert.match(html,/不等同 JARUS Operational Volume/);
  assert.doesNotMatch(html,/最大时延 ms/);
});

test('P7-P12 workflow steps expose vertical, runtime, proposal and closed-loop contracts',()=>{
  const base={steps:{'2':false,'3':false,'5':false},spatial_3d:{altitude_layers:[],route_altitude_profiles:{}},operational_timing:{route_motion_profiles:{},service_scenarios:{},response_time_budgets:{},encounter_scenarios:{}},service_timeline:{},protection_envelope:{},grid_attributes:{},grid_risk:{},retired_route_ids:[],risks:{environment:{status:'not_calculated'},life:{status:'not_calculated'},property:{status:'not_calculated'}},scenario_routes:[],operational_routes:[],nodes:[],devices:[],defaults:{engineering_parameters:{primary_spacing_factor:{value:.9,source:'test'},co_location_search_radius_m:{value:1}}},existing_cns_facilities:{},candidate_sites:{},device_catalog:{},cns_gap_analysis:{},cns_gap_analysis_v2:{},coverage_3d:{},cns_service_capability:{},site_planning_policy:{},cns_site_plan:{},closed_loop_assessment:{},cns_corridor_assessment:{},cns_planning_objectives:{routes:{}},cns_corridor_gap_assessment:{},corridor_site_planning_policy:{},cns_corridor_site_plan:{}};
  const step2=renderStep2({flow:base,draftWorkspace:null,gridDisplay:{outline:true,theme:'none'},populationDisplayLabel:()=>'',formatNumber:String});
  const step3=renderStep3({flow:base,interactionMode:'pan'});
  const step5=renderStep5({flow:base});
  assert.match(step2,/EGM2008 orthometric/);
  assert.match(step2,/3D 高度层/);
  assert.match(step3,/Route 3D Altitude Profile/);
  assert.match(step3,/Route Motion Profile/);
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
});

test('step 6 keeps the corridor site result visibly proposal-only',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value)}});return node;}};
  const html=renderStep6({state:{data_health:{status:'passed'}},flow:{project:{name:'P'},workspace:null,operational_routes:[],aircraft:null,rules:null,coverage:null,review:{risks:{},overall_status:'pending_confirmation',overall_pass:false},result_statuses:{cns_corridor_site_plan:'passed'},cns_corridor_site_plan:{status:'proposal_ready',target_voxel_count:2,selected_actions:[{}],confirmed_requirement_unit_volume_gain:10}}});
  assert.match(html,/P16 Corridor Site Plan · Proposal/);
  assert.match(html,/Proposal only/);
  assert.match(html,/不修改 Existing CNS/);
});
