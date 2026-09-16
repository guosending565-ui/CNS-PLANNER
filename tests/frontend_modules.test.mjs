import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {lonLatToMercator,mercatorToLonLat} from '../cns_planner/web/js/map/projection.js';
import {createStore} from '../cns_planner/web/js/state/store.js';
import {buildGridOverlayCache,findGridCell} from '../cns_planner/web/js/map/grid_overlay.js';
import {algorithmManifestDetails,algorithmSelectionKey} from '../cns_planner/web/js/workflow/step01_project.js';
import {render as renderStep4,withLegacyRequiredAliases,requirementRecommendationSummary} from '../cns_planner/web/js/workflow/step04_operation.js';
import {render as renderStep2} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {filterReferenceSites,referenceOverlayModel,render as renderStep3,riskAwareRoutePanel} from '../cns_planner/web/js/workflow/step03_routes.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6,planReviewSummary} from '../cns_planner/web/js/workflow/step06_review.js';
import {sourceModeText,statusText} from '../cns_planner/web/js/workflow/common.js';

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

test('map exposes independent source airspace confirmed allowed and reference layer toggles',()=>{
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  for(const id of ['allowedAirspaceLayer','referenceRouteLayer','referenceRoutePointLayer','referenceLandingLayer','buildingClearanceLayer','terrain_dtmPath'])assert.match(html,new RegExp('id="'+id+'"'));
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
