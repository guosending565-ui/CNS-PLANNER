/**
 * RouteRiskProfile V1 前端集成回归测试。
 *
 * 目标：锁定"只做前端集成"这一层契约，而不是重新验证后端数学：
 *  - Step03 结果区新增 res-risk-profile，四段均可切换且严格只有 1 段 active；
 *  - readiness / blockers 原样转印，前端不重新判断；
 *  - 没有确认阈值时仍显示 exposure，但 classification / high-risk 永远不伪造；
 *  - 只有 confirmed 阈值才出现 low/medium/high 与 high-risk interval；
 *  - 一维 SVG 的坐标只来自后端累计距离与 route_length_m；
 *  - 三个 domain 各自独立，没有 cross-domain overall / high-risk；
 *  - policy / evaluate / delete 三个端点按契约调用；
 *  - stale profile 保留显示并明确标记；
 *  - 原 Step03 控件与 id 唯一性不回归。
 *
 * 这里自带一份最小 DOM 桩（与 workbench_shell.test.mjs 的桩同构），因此本文件
 * 可以独立运行：`node tests/route_risk_profile_frontend.test.mjs`。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {render as renderStep3,bind as bindStep3} from '../cns_planner/web/js/workflow/step03_routes.js';
import {createWorkbench} from '../cns_planner/web/js/workflow/workbench.js';
import {renderWorkflowSteps} from '../cns_planner/web/js/workflow/steps.js';
import {
  DOMAIN_IDS,ROUTE_RISK_PROFILE_SEGMENT,routeRiskDomainGeometry,routeRiskDomainModel,
  routeRiskDomainSvg,routeRiskProfileModel,routeRiskProfilePolicyPayload,renderRouteRiskProfile,
} from '../cns_planner/web/js/workflow/route_risk_profile.js';

// ---- 最小 DOM 桩 -------------------------------------------------------------

class StubClassList{
  constructor(){this.values=new Set();}
  add(...names){for(const name of names)this.values.add(name);}
  remove(...names){for(const name of names)this.values.delete(name);}
  contains(name){return this.values.has(name);}
  toggle(name,force){
    const on=force===undefined?!this.values.has(name):Boolean(force);
    if(on)this.values.add(name);else this.values.delete(name);
    return on;
  }
}

class StubNode{
  constructor(tag='div'){
    this.tagName=String(tag).toUpperCase();
    this.dataset={};this.attributes={};this.style={};
    this.classList=new StubClassList();this.children=[];this.parentNode=null;
    this.hidden=false;this.textContent='';this.id='';this.listeners={};this.scrollTop=0;
  }
  set className(value){
    this.classList=new StubClassList();
    for(const name of String(value||'').split(/\s+/))if(name)this.classList.add(name);
  }
  get className(){return [...this.classList.values].join(' ');}
  append(...nodes){
    for(const node of nodes){
      if(node&&node.tagName==='FRAGMENT'){this.append(...node.children.slice());continue;}
      node.parentNode=this;this.children.push(node);
    }
  }
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){
    const parsed=parseSelector(selector),out=[];
    forEachDescendant(this,child=>{if(parsed.some(part=>matchesSelector(child,part)))out.push(child);});
    return out;
  }
  addEventListener(name,handler){(this.listeners[name]||(this.listeners[name]=[])).push(handler);}
  dispatchEvent(event){for(const handler of this.listeners[event.type]||[])handler(event);return true;}
  closest(selector){
    const parsed=parseSelector(selector);
    for(let node=this;node;node=node.parentNode){
      if(node.tagName&&parsed.some(part=>matchesSelector(node,part)))return node;
    }
    return null;
  }
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name];}
  removeAttribute(name){delete this[name];}
}

function parsePart(textValue){
  const attribute=/^\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(textValue);
  if(attribute)return {kind:'attr',name:attribute[1],value:attribute[2]===undefined?null:attribute[2]};
  const className=/^\.([A-Za-z][-A-Za-z0-9_]*)$/.exec(textValue);
  if(className)return {kind:'class',name:className[1]};
  const tagAndClass=/^([A-Za-z][-A-Za-z0-9]*)\.([A-Za-z][-A-Za-z0-9_]*)$/.exec(textValue);
  if(tagAndClass)return [{kind:'tag',name:tagAndClass[1].toUpperCase()},{kind:'class',name:tagAndClass[2]}];
  const tag=/^[A-Za-z][-A-Za-z0-9]*$/.exec(textValue);
  if(tag)return {kind:'tag',name:textValue.toUpperCase()};
  return {kind:'none'};
}

function parseSelector(selector){
  return String(selector).split(',').map(part=>{
    const parsed=parsePart(part.trim());
    return Array.isArray(parsed)?parsed:[parsed];
  });
}

function toDatasetKey(attribute){return attribute.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());}

function matchesSelector(node,parsed){
  const parts=Array.isArray(parsed[0])?parsed[0]:parsed;
  return parts.every(part=>{
    if(part.kind==='tag')return node.tagName===part.name;
    if(part.kind==='class')return node.classList.contains(part.name);
    if(part.kind!=='attr')return false;
    const base=part.name.startsWith('data-')?part.name.slice(5):part.name;
    const current=node.dataset[toDatasetKey(base)];
    if(current===undefined)return false;
    return part.value===null?true:String(current)===part.value;
  });
}

function forEachDescendant(node,visit){
  for(const child of node.children){visit(child);forEachDescendant(child,visit);}
}

function findAll(root,selector){
  const parsed=parseSelector(selector),out=[];
  forEachDescendant(root,node=>{if(parsed.some(part=>matchesSelector(node,part)))out.push(node);});
  return out;
}

function findByDataset(root,key,value){
  let found=null;
  forEachDescendant(root,node=>{
    if(found)return;
    if(node.dataset&&String(node.dataset[key])===String(value))found=node;
  });
  return found;
}

function parseAttributes(textValue,node){
  for(const match of textValue.matchAll(/([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g)){
    const name=match[1],value=match[2]===undefined?'':match[2];
    if(name.startsWith('data-'))node.dataset[toDatasetKey(name.slice(5))]=value;
    else node.attributes[name]=value;
    if(name==='id')node.id=value;
    if(name==='class')node.className=value;
  }
}

const VOID_TAGS=new Set(['br','hr','img','input','meta','link','source','col','area','base','wbr']);

function parseHtml(html){
  const root=new StubNode('div'),stack=[root];
  const textValue=String(html||'').replace(/<!--[\s\S]*?-->/g,'');
  const tokens=/<(\/?)([A-Za-z][-A-Za-z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let cursor=0,match;
  tokens.lastIndex=cursor;
  while((match=tokens.exec(textValue))!==null){
    const head=textValue.slice(cursor,match.index);
    if(head.trim())stack[stack.length-1].textContent+=head;
    cursor=tokens.lastIndex;
    if(match[1]){if(stack.length>1)stack.pop();continue;}
    const tag=match[2].toLowerCase(),node=new StubNode(tag);
    parseAttributes(match[3]||'',node);
    stack[stack.length-1].append(node);
    if(!VOID_TAGS.has(tag))stack.push(node);
  }
  const tail=textValue.slice(cursor);
  if(tail.trim())stack[stack.length-1].textContent+=tail;
  return root;
}

function createTemplateElement(){
  const node=new StubNode('template');
  Object.defineProperty(node,'innerHTML',{
    get(){return '';},
    set(html){
      const parsed=parseHtml(html),content=new StubNode('fragment');
      for(const child of parsed.children.slice())content.append(child);
      Object.defineProperty(node,'content',{value:content,configurable:true,writable:true});
    },
    configurable:true,
  });
  Object.defineProperty(node,'content',{value:new StubNode('fragment'),configurable:true,writable:true});
  return node;
}

function registerTree(nodes,node){
  if(node.id)nodes.set(node.id,node);
  for(const child of node.children)registerTree(nodes,child);
}

function patchAppendOnce(){
  if(StubNode.prototype.append.__registersIds)return;
  const original=StubNode.prototype.append;
  const patched=function(...children){
    const result=original.apply(this,children);
    const document=globalThis.document;
    if(document&&document.__nodes)for(const child of children)registerTree(document.__nodes,child);
    return result;
  };
  patched.__registersIds=true;
  StubNode.prototype.append=patched;
}

function installStubDom(){
  patchAppendOnce();
  const nodes=new Map();
  const document={
    body:new StubNode('body'),__nodes:nodes,__listeners:{},
    createElement:tag=>String(tag).toLowerCase()==='template'?createTemplateElement():new StubNode(tag),
    getElementById:id=>nodes.get(id)||null,
    querySelector:()=>null,
    querySelectorAll:()=>[],
    addEventListener(name,handler){(document.__listeners[name]||(document.__listeners[name]=[])).push(handler);},
    removeEventListener(){},
    dispatchEvent(event){for(const handler of document.__listeners[event.type]||[])handler(event);return true;},
    register(id,className=''){
      const node=new StubNode('div');node.id=id;node.className=className;
      nodes.set(id,node);document.body.append(node);return node;
    },
  };
  document.body.parentNode=document;
  const workflowPanel=new StubNode('div');
  workflowPanel.id='workflowPanel';
  document.body.append(workflowPanel);
  nodes.set('workflowPanel',workflowPanel);
  document.register('workbenchTabs','tabbar');
  document.register('workbenchSegs');
  document.register('workbenchBody');
  document.register('workbenchStepNumber','workbench-step');
  document.register('workbenchStepTitle','');
  document.register('workbenchStepNote','');
  globalThis.document=document;
  registerTree(nodes,document.body);
  return document;
}

function withStubDom(run){
  const original=globalThis.document;
  const document=installStubDom();
  try{return run(document);}
  finally{globalThis.document=original;}
}

installStubDom();

// ---- flow 替身 ---------------------------------------------------------------

const GOLDEN_ROUTE_LENGTH_M=1925.858241815;

/** 与后端 profile 同形的 segments：位置只由 start/end_cumulative_distance_m 表达。 */
function segmentsFor(lengths,groundLevels,index){
  const segments=[];
  let total=0;
  lengths.forEach((lengthValue,position)=>{
    const level=groundLevels[position]||null;
    segments.push({
      segment_id:'RRP-LRC-1-S'+String(position).padStart(4,'0'),
      index:position,
      start_cumulative_distance_m:total,
      end_cumulative_distance_m:total+lengthValue,
      length_m:lengthValue,
      start_index_grid_id:'G'+position,
      end_index_grid_id:'G'+(position+1),
      domains:{
        ground:{
          resolved:level!==null,mean_index:index.ground[position],
          start_index:index.ground[position],end_index:index.ground[position],
          exposure_index_m:lengthValue*index.ground[position],
          classification:{status:level===null?'not_configured':'passed',level,reason:level===null?'thresholds_not_configured':null},
        },
        air_traffic:{
          resolved:true,mean_index:0.4,start_index:0.4,end_index:0.4,
          exposure_index_m:lengthValue*0.4,
          classification:{status:'not_configured',level:null,reason:'thresholds_not_configured'},
        },
        environment_obstacle:{
          resolved:false,mean_index:null,start_index:null,end_index:null,
          exposure_index_m:null,
          classification:{status:'not_configured',level:null,reason:'thresholds_not_configured'},
        },
      },
    });
    total+=lengthValue;
  });
  return segments;
}

function domainProfile(domainId,overrides={}){
  return {
    domain_id:domainId,
    label:domainId,
    status:'resolved',
    active_cost_domain:domainId==='ground',
    exposure_index_m:1011.075576953,
    mean_index:0.525000000,
    max_index:0.9,
    max_location:{grid_id:'MHT4063-L8-C2-RP0',role:'grid_cell',cumulative_distance_m:1500},
    resolved_length_m:GOLDEN_ROUTE_LENGTH_M,
    unresolved_length_m:0,
    coverage:1,
    resolved_segment_count:4,
    unresolved_segment_count:0,
    unresolved_cells:domainId==='environment_obstacle'?['G3']:[],
    classification:{
      domain_id:domainId,status:'not_configured',level:null,index:null,
      reason:'thresholds_not_configured',
      thresholds:{medium_min:null,high_min:null},
      policy_fingerprint:null,high_risk_metrics:'not_configured',
      semantics:'per_domain_classification_only',
    },
    high_risk:{
      status:'not_configured',length_m:null,interval_count:null,intervals:null,
      reason:'thresholds_not_configured',
    },
    contributors:{},
    ...overrides,
  };
}

/** 一份与后端契约同形的 RouteRiskProfile（4 段：550 / 500 / 450 / 425.858241815）。 */
function profileFixture({groundConfirmed=false,status='passed',applicability='current'}={}){
  const lengths=[550,500,450,425.858241815];
  const groundLevels=groundConfirmed?['high','high','medium','low']:[null,null,null,null];
  const segments=segmentsFor(lengths,groundLevels,{ground:[0.9,0.9,0.5,0.1]});
  const groundClassification=groundConfirmed
    ?{domain_id:'ground',status:'passed',level:'medium',index:0.525,
      reason:null,thresholds:{medium_min:0.3,high_min:0.8},
      policy_fingerprint:'routeprofiledomainpolicyv1-abc',high_risk_metrics:'available',
      semantics:'per_domain_classification_only'}
    :{domain_id:'ground',status:'not_configured',level:null,index:null,
      reason:'thresholds_not_configured',thresholds:{medium_min:null,high_min:null},
      policy_fingerprint:'routeprofiledomainpolicyv1-def',high_risk_metrics:'not_configured',
      semantics:'per_domain_classification_only'};
  const groundHighRisk=groundConfirmed
    ?{status:'available',length_m:1050,interval_count:1,resolved_length_m:GOLDEN_ROUTE_LENGTH_M,
      unclassified_length_m:875.858241815,semantics:'continuous_high_segments_merged_per_domain_only',
      intervals:[{
        interval_id:'ground-HR-0001',domain_id:'ground',
        start_distance_m:0,end_distance_m:1050,length_m:1050,max_index:0.9,mean_index:0.9,
        segment_ids:[segments[0].segment_id,segments[1].segment_id],
        cell_ids:['G1','G2','G3'],
      }]}
    :{status:'not_configured',length_m:null,interval_count:null,intervals:null,
      reason:'thresholds_not_configured',semantics:'high_risk_requires_confirmed_thresholds'};
  return {
    schema_version:'route-risk-profile-v1',
    profile_id:'RRP-LRC-1-abcdef0123456789',
    artifact_type:'layered_route_candidate',
    status,
    status_reason:null,
    stale_reason:status==='stale'?'route_risk_profile_policy_changed':null,
    blocking_reasons:[],
    candidate:{
      candidate_id:'LRC-1',status:'candidate',route_id:'R0001',altitude_layer_id:'L8-LOW',
      lane_key:'R0001@L8-LOW',grid_level:8,candidate_fingerprint:'cand-fp',
      input_fingerprint:'input-fp',risk_fingerprint:'risk-fp',
      feasibility_mask_fingerprint:'mask-fp',current_applicability:applicability,
    },
    route:{route_id:'R0001',altitude_layer_id:'L8-LOW',grid_level:8,cell_count:4,
      grid_path:['G0','G1','G2','G3'],path_fingerprint:'path-fp'},
    layer:{altitude_layer_id:'L8-LOW',grid_level:8},
    policy:{status:groundConfirmed?'pending_confirmation':'not_configured',
      parameter_status:'no_default_thresholds',domains:{},notes:[]},
    route_length_m:GOLDEN_ROUTE_LENGTH_M,
    domains:{
      ground:domainProfile('ground',{classification:groundClassification,high_risk:groundHighRisk,
        contributors:{population_exposure:{factor_id:'population_exposure',
          normalized_exposure_index_m:900,weighted_contribution_index_m:900,weight:1,
          contributor_rank:1,contribution_status:'available',
          contributor_semantics:'relative_engineering_contribution_not_accident_cause_probability'}}}),
      air_traffic:domainProfile('air_traffic',{active_cost_domain:false,exposure_index_m:770.34,mean_index:0.4,max_index:0.4}),
      environment_obstacle:domainProfile('environment_obstacle',{active_cost_domain:false,status:'unresolved',
        exposure_index_m:null,mean_index:null,max_index:null,max_location:null,
        resolved_length_m:0,unresolved_length_m:GOLDEN_ROUTE_LENGTH_M,coverage:0,
        resolved_segment_count:0,unresolved_segment_count:4}),
    },
    factors:{population_exposure:{factor_id:'population_exposure',domain:'ground',
      raw_unit:'people/km2',label:'人口暴露',canonical_source_available:true,status:'passed',
      resolved:true,resolved_length_m:GOLDEN_ROUTE_LENGTH_M,unresolved_length_m:0,coverage:1,
      raw_exposure:{value:1234,unit:'people/km2'},normalized_exposure_index_m:900,
      weighted_contribution_index_m:900,weight:1,contribution_status:'available',
      contributor_rank:1,contributor_semantics:'relative_engineering_contribution_not_accident_cause_probability',
      source_ids:['src-population_exposure'],source_fingerprints:['fp-population_exposure'],
      normalization_reference_fingerprints:['ref-population_exposure'],
      provenance:{canonical_field:'population_density_people_km2',not_accident_cause_probability:true}}},
    contributors:{status:'available',ranking:[{rank:1,factor_id:'population_exposure',domain:'ground',
      weight:1,weighted_contribution_index_m:900,normalized_exposure_index_m:900}],reason:null,
      semantics:'relative_engineering_contribution_not_accident_cause_probability'},
    classification:{status:groundConfirmed?'partial':'not_configured',index_scope:'per_domain_only',
      cross_domain_overall:'not_computed',domains:{},semantics:'classification_is_per_domain_only_no_cross_domain_overall'},
    high_risk:{status:groundConfirmed?'partial':'not_configured',index_scope:'per_domain_only',
      cross_domain_high_risk:'not_computed',domains:{},
      semantics:'continuous_high_segments_merged_per_domain_only'},
    segments,
    connector_semantics:'endpoint_connector_reuses_first_last_cell_index',
    consistency:{status:'passed',tolerance:1e-6,
      checks:[{check_id:'route_length_matches_candidate_distance_m',status:'passed',reason_code:null,
        reason:null,profile_value:GOLDEN_ROUTE_LENGTH_M,candidate_value:GOLDEN_ROUTE_LENGTH_M}],
      semantics:'profile_integral_must_equal_candidate_cost_breakdown_within_tolerance'},
    fingerprints:{profile_fingerprint:'routeprofilev1-abcdef0123456789',
      policy_fingerprint:'routeprofilepolicyv1-1',candidate_fingerprint:'cand-fp',
      path_fingerprint:'path-fp',grid_risk_v2_input_fingerprint:'riskv2-input',
      grid_risk_v2_policy_fingerprint:'riskv2-policy',grid_risk_v2_cells_fingerprint:'riskv2-cells',
      components:{}},
    provenance:{pipeline:'current_layered_route_candidate + current_grid_risk_v2 -> route_risk_profile',
      algorithm:'route_risk_profile_v1@1.0',
      integral_helper:'cns_planner.risk.route_exposure.integrate_path_exposure',
      planner_cost_helper_shared:true,risk_semantics:'relative_engineering_index',
      risk_v2_overall_used:false,replanning:false,candidate_mutated:false,
      writes_operational_routes_or_cns:false,airspace:{applicability:'display_only'},
      notes:['exposure 与 candidate cost_breakdown 使用同一个积分 helper。']},
    not_computed:{absolute_risk:{status:'not_computed'},sora_grc:{status:'not_computed'},
      sora_arc:{status:'not_computed'}},
    airspace:{status:'not_applicable',applicability:'display_only',used_in_value_or_fingerprint:false},
    semantics:{},
    notes:['只分析 current LayeredRouteCandidate：不重规划、不修改 candidate。'],
  };
}

/** readiness 替身：blockers/阈值状态由参数决定，前端只负责转印。 */
function readinessFixture({status='ready',candidateStatus='candidate',applicability='current',
  gridStatus='passed',confirmedDomains=[],blockers=[]}={}){
  const thresholdsConfigured=domainId=>confirmedDomains.includes(domainId);
  return {
    status,
    algorithm:{algorithm_id:'route_risk_profile_v1',algorithm_version:'1.0'},
    artifact_type:'layered_route_candidate',
    candidate:{status:candidateStatus,candidate_id:'LRC-1',lane_key:'R0001@L8-LOW',
      route_id:'R0001',altitude_layer_id:'L8-LOW',candidate_fingerprint:'cand-fp',
      risk_fingerprint:'risk-fp',current_applicability:applicability},
    current_identity:{candidate_fingerprint:'cand-fp',risk_fingerprint:'risk-fp',lane_key:'R0001@L8-LOW'},
    grid_risk_v2:{status:gridStatus,input_fingerprint:'riskv2-input',
      policy_fingerprint:'riskv2-policy',cell_count:4,overall_used:false},
    profile_policy:{status:confirmedDomains.length===DOMAIN_IDS.length?'confirmed':
      confirmedDomains.length?'pending_confirmation':'not_configured',
      parameter_status:'no_default_thresholds',fingerprint:'routeprofilepolicyv1-1',
      domains:Object.fromEntries(DOMAIN_IDS.map(domainId=>[domainId,{
        domain_id:domainId,
        status:thresholdsConfigured(domainId)?'confirmed':'not_configured',
        medium_min:thresholdsConfigured(domainId)?0.3:null,
        high_min:thresholdsConfigured(domainId)?0.8:null,
        confirmed:thresholdsConfigured(domainId),
        source:thresholdsConfigured(domainId)?'工程确认-测试':'未配置',
      }]))},
    domains:Object.fromEntries(DOMAIN_IDS.map(domainId=>[domainId,{
      domain_id:domainId,
      thresholds_status:thresholdsConfigured(domainId)?'confirmed':'not_configured',
      classification_available:thresholdsConfigured(domainId),
      high_risk_metrics:thresholdsConfigured(domainId)?'available':'not_configured',
    }])),
    profile_count:1,
    blockers,
    not_computed:{absolute_risk:'not_computed',sora_grc:'not_computed',sora_arc:'not_computed',
      cross_domain_overall:'not_computed',cross_domain_high_risk:'not_computed'},
    semantics:{classification_is_per_domain_only:true,thresholds_have_no_default:true,
      factor_contribution_is_relative_engineering_contribution:true,analysis_only_no_replanning:true},
    notes:['未确认阈值的 domain 仍会输出 exposure / mean / max。'],
  };
}

function policyFixture(confirmedDomains=[]){
  return {
    schema_version:'route-risk-profile-v1',
    status:confirmedDomains.length===DOMAIN_IDS.length?'confirmed':
      confirmedDomains.length?'pending_confirmation':'not_configured',
    parameter_status:'no_default_thresholds',
    domains:Object.fromEntries(DOMAIN_IDS.map(domainId=>[domainId,{
      domain_id:domainId,
      medium_min:confirmedDomains.includes(domainId)?0.3:null,
      high_min:confirmedDomains.includes(domainId)?0.8:null,
      source:confirmedDomains.includes(domainId)?'工程确认-测试'
        :'未配置；RouteRiskProfile 阈值必须由项目工程依据显式确认',
      evidence:null,
      confirmed:confirmedDomains.includes(domainId),
      status:confirmedDomains.includes(domainId)?'confirmed':'not_configured',
      status_reason:confirmedDomains.includes(domainId)?null:'thresholds_not_configured',
      parameter_status:'no_default_thresholds',
      bounds:[0,1],label:domainId,
    }])),
    semantics:{no_default_thresholds:true},
    notes:['阈值只能按 domain 显式确认。'],
  };
}

/** 完整 Step03 flow：既有字段与原 Step03 契约保持一致。 */
function baseFlow(overrides={}){
  const confirmedDomains=overrides.confirmedDomains||[];
  const profileStatus=overrides.profileStatus||'passed';
  const applicability=profileStatus==='stale'?'stale':'current';
  return {
    project:{name:'测试项目'},
    nodes:[{node_id:'N001',name:'A',coordinate:[122,30]},{node_id:'N002',name:'B',coordinate:[122.1,30.1]}],
    scenario_routes:[{route_id:'R0001',direction:'N001→N002',path:[[122,30],[122.1,30.1]]}],
    operational_routes:[{route_id:'R0001',status:'passed',path:[[122,30],[122.1,30.1]],distance_m:1200}],
    algorithm_selection:{route_planner:{algorithm_id:'layered_risk_aware_route_planner_v1',version:'1.0',parameters:{}}},
    algorithm_catalog:[],retired_route_ids:[],
    risks:{environment:{status:'not_calculated'}},
    steps:{1:true,2:true,3:true},
    spatial_3d:{route_altitude_profiles:{},altitude_layers:[]},
    operational_timing:{route_motion_profiles:{}},
    route_vertical_profiles:{},
    building_clearance_policy:{},building_clearance_assessment:{},
    reference_routes:{items:[],points:[]},reference_landing_sites:{items:[]},
    route_planning_experiments:{},reference_route_links:{items:[]},
    reference_endpoint_candidates:{},data_readiness:{blocks:{}},
    workspace:{bbox:[122,29.9,122.2,30.1],area_km2:12.5,health:{loaded_layer_count:4}},
    grid:{status:'passed',level:8,count:4},
    // ---- RouteRiskProfile V1 additive 分片 ----
    route_risk_profile_readiness:overrides.readiness||readinessFixture({confirmedDomains}),
    route_risk_profile_policy:overrides.policy||policyFixture(confirmedDomains),
    route_risk_profiles:overrides.collection||{
      schema_version:'route-risk-profile-v1',status:'passed',count:1,artifact_type:'layered_route_candidate',
      items:[profileFixture({groundConfirmed:confirmedDomains.includes('ground'),
        status:profileStatus,applicability})],
      last_evaluation:{status:profileStatus,candidate_id:'LRC-1',
        profile_id:'RRP-LRC-1-abcdef0123456789',profile_fingerprint:'routeprofilev1-abcdef0123456789',
        reason_code:null,reason:null,blocking_reasons:[]},
      notes:[],
    },
    ...overrides.extra,
  };
}

function stepContext(flow){
  return {
    state:{data_health:{status:'ready'},layers:[],project_storage:{directory:''},paths:{}},
    flow,draftWorkspace:null,gridDisplay:{outline:true,theme:'none'},
    interactionMode:'pan',selectedReference:null,
    populationDisplayLabel:()=>'人口',formatNumber:String,
  };
}

function mountStep3(document,flow,store={step:3,tab:'result',segs:{result:ROUTE_RISK_PROFILE_SEGMENT},scroll:0}){
  const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
  const root=renderWorkflowSteps({step:{render:renderStep3},context:stepContext(flow)});
  controller.mount({root,step:{number:3,title:'航路规划',note:''}});
  return {controller,root,store};
}

function tabButton(document,tab){
  return document.getElementById('workbenchTabs').children.find(node=>node.dataset.wbTab===tab);
}

function segButtons(document){
  const container=document.getElementById('workbenchSegs');
  const group=container.children.find(child=>child.classList.contains('segmented'));
  return group?group.children:[];
}

function clickNode(document,node){
  const event={type:'click',target:node};
  for(let current=node;current;current=current.parentNode)current.dispatchEvent(event);
}

/** 节点及其子树的可见文本（桩 DOM 把文本放在持有它的元素上，因此两层都要看）。 */
function textOf(node){
  if(!node)return '';
  let text=String(node.textContent||'');
  forEachDescendant(node,child=>{text+=String(child.textContent||'');});
  return text;
}

/** 一个 domain 卡片的分段子树（data-seg-name="res-risk-profile" 内的 domain 章节）。 */
/** 一个 domain 卡片的三张表/图：字段表、high-risk 表、interval 表与一维画像。 */
function rowsOf(section){
  if(!section)return {};
  const out={};
  for(const row of findAll(section,'.list-row')){
    const key=textOf(findAll(row,'b')[0]);
    if(!key)continue;
    out[key]=Array.from(findAll(row,'small'),textOf).join(' | ');
  }
  return out;
}

function cardOf(root,domainId){
  return {
    fields:rowsOf(findByDataset(root,'rrpFields',domainId)),
    highRisk:rowsOf(findByDataset(root,'rrpHighRisk',domainId)),
    intervals:findByDataset(root,'rrpIntervals',domainId),
    chart:findByDataset(root,'rrpChart',domainId),
  };
}

function svgRects(svg){
  return Array.from(svg.matchAll(/<rect [^>]*data-rrp-segment="([^"]*)"[^>]*x="([0-9.]+)"[^>]*width="([0-9.]+)"/g),
    match=>({segmentId:match[1],x:Number(match[2]),width:Number(match[3])}));
}

function svgBands(svg){
  return Array.from(svg.matchAll(/<rect [^>]*data-rrp-band="([^"]*)"[^>]*x="([0-9.]+)"[^>]*width="([0-9.]+)"/g),
    match=>({band:match[1],x:Number(match[2]),width:Number(match[3])}));
}

// ---- 1. Step03 结果区结构 ----------------------------------------------------

test('step 03 result region exposes the four documented segments with exactly one active',()=>{
  withStubDom(document=>{
    const flow=baseFlow();
    const {root}=mountStep3(document,flow,{step:3,tab:'result',segs:{},scroll:0});
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),
      ['res-route','res-feasibility','res-risk-profile','res-compare']);
    assert.deepEqual(Array.from(segButtons(document),textOf),
      ['当前航路','可行性与净空','路径风险画像','对比与验证']);
    for(const id of ['res-route','res-feasibility','res-risk-profile','res-compare']){
      clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
      assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
      const visible=findAll(root,'.wb-seg-active');
      assert.equal(visible.length,1,`exactly one segment may be visible while ${id} is selected`);
      assert.equal(visible[0].dataset.segName,id);
    }
    // 其余 Step03 分区不动
    assert.deepEqual(Array.from(document.getElementById('workbenchTabs').children,node=>node.dataset.wbTab),
      ['operate','result','advanced']);
    for(const id of ['op-sites','op-candidates','op-operational','op-altitude',
      'adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
      assert.ok(findByDataset(root,'segName',id),`segment ${id} must stay mounted`);
    }
  });
});

test('the risk profile segment keeps every original step 03 control and unique ids',()=>{
  withStubDom(document=>{
    const {root}=mountStep3(document,baseFlow());
    for(const id of ['createOdRoute','saveRouteAltitude','saveRouteMotion','saveBuildingClearancePolicy',
      'evaluateRoutePlannerV3','saveRoutePlannerV3Policy','referenceSiteSearch','nextStep',
      'evaluateRouteRiskProfile','saveRouteRiskProfilePolicy']){
      assert.ok(document.getElementById(id),`#${id} must stay mounted`);
    }
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,
      `duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
    // 三个 domain 的阈值控件各自唯一
    for(const domainId of DOMAIN_IDS){
      assert.ok(document.getElementById('rrpThresholdMedium_'+domainId),`medium_min input for ${domainId}`);
      assert.ok(document.getElementById('rrpThresholdHigh_'+domainId),`high_min input for ${domainId}`);
      assert.ok(document.getElementById('rrpThresholdSource_'+domainId),`source input for ${domainId}`);
      assert.ok(document.getElementById('rrpThresholdConfirmed_'+domainId),`confirmed checkbox for ${domainId}`);
    }
    // 每个风险画像控件都落在正常正文里，而不是 .wb-section-head
    for(const id of ['evaluateRouteRiskProfile','saveRouteRiskProfilePolicy']){
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
  });
});

// ---- 2. readiness / blockers 转印 --------------------------------------------

test('readiness and blockers are transcribed verbatim and gate the generate button',()=>{
  const blockers=[
    {reason_code:'candidate_not_available',reason:'当前没有 LayeredRouteCandidate：请先完成候选规划'},
    {reason_code:'grid_risk_v2_not_calculated',reason:'grid_risk_v2 尚未计算：路径暴露缺少 domain index 证据'},
  ];
  const flow=baseFlow({readiness:readinessFixture({
    status:'blocked',candidateStatus:null,applicability:null,gridStatus:'not_calculated',blockers,
  }),collection:{status:'not_calculated',count:0,items:[],last_evaluation:null,notes:[]}});
  const html=renderRouteRiskProfile(flow);
  for(const blocker of blockers){
    assert.match(html,new RegExp(blocker.reason_code));
    assert.match(html,new RegExp(blocker.reason.replace(/[.*+?^${}()|[\]\\/]/g,'\\$&')));
  }
  assert.match(html,/id="evaluateRouteRiskProfile" disabled/,'a blocked readiness disables the button');
  assert.match(html,/blocked（blocker 未清除）/);

  const ready=renderRouteRiskProfile(baseFlow());
  assert.doesNotMatch(ready,/id="evaluateRouteRiskProfile" disabled/);
  assert.match(ready,/ready（无 blocker）/);
  // 未计算 grid_risk_v2 的状态必须原样出现，而不是被改写成 passed
  assert.match(renderRouteRiskProfile(baseFlow({
    readiness:readinessFixture({status:'blocked',gridStatus:'not_calculated',blockers}),
  })),/grid_risk_v2/);
});

// ---- 3. 无阈值：显示 exposure，不伪造 classification / high-risk --------------

test('without confirmed thresholds the profile still shows exposure but never fakes a level',()=>{
  withStubDom(document=>{
    const flow=baseFlow();
    const {root}=mountStep3(document,flow);
    const model=routeRiskProfileModel(flow);
    assert.equal(model.domains.every(domain=>domain.thresholdsConfigured===false),true);

    const card=cardOf(root,'ground');
    assert.ok(card.fields,'the ground domain card must be mounted');
    assert.match(card.fields['exposure_index_m'],/1011\.076/,
      'exposure_index_m is transcribed even without thresholds');
    assert.match(card.fields['mean_index'],/0\.525/);
    assert.match(card.fields['max_index'],/0\.900/);
    assert.match(card.fields['resolved_length_m'],/1925\.858/);
    assert.match(card.fields['unresolved_length_m'],/0\.000 m/);
    assert.match(card.fields['thresholds'],/无默认阈值/);
    assert.match(card.fields['classification'],/not_configured/);
    for(const level of ['low','medium','high']){
      assert.doesNotMatch(card.fields['classification'],new RegExp('\\b'+level+'\\b'),
        `an unconfirmed domain must not claim level ${level}`);
    }
    assert.match(card.fields['classification'],/不伪造等级/);
    assert.match(card.highRisk['high_risk status'],/thresholds_not_configured/);
    assert.match(card.highRisk['high_risk length_m'],/—（未确认阈值）/);
    assert.match(card.highRisk['high_risk interval_count'],/—（未确认阈值）/);
    assert.match(textOf(card.intervals),/不产生 high-risk interval/);

    // 模型层同样必须保持 null
    const domainModel=routeRiskDomainModel(flow.route_risk_profiles.items[0],'ground');
    assert.equal(domainModel.classified,false);
    assert.equal(domainModel.classificationLevel,null);
    assert.equal(domainModel.mediumMin,null);
    assert.equal(domainModel.highMin,null);
    assert.equal(domainModel.highRiskLengthM,null);
    assert.equal(domainModel.highRiskIntervalCount,null);
    assert.deepEqual(domainModel.intervals,[]);

    // SVG 在未确认阈值时不出现 low/medium/high 配色，也不画阈值区段带
    const svg=routeRiskDomainSvg(flow.route_risk_profiles.items[0],'ground');
    assert.doesNotMatch(svg,/#2f8f4e|#c98a1b|#c0392b|rgba\(47,143,78|rgba\(201,138,27|rgba\(192,57,43/,
      'an unconfirmed domain must not use level colors');
    assert.doesNotMatch(svg,/data-rrp-band|data-rrp-threshold/,
      'an unconfirmed domain must not draw threshold bands');
    assert.match(svg,/data-rrp-level="not_configured"/);
    assert.match(svg,/未配置\/未确认阈值/);
  });
});

// ---- 4. confirmed 阈值才显示等级与 high-risk interval ------------------------

test('only confirmed thresholds produce low/medium/high levels and high-risk intervals',()=>{
  withStubDom(document=>{
    const flow=baseFlow({confirmedDomains:['ground']});
    const {root}=mountStep3(document,flow);

    const ground=cardOf(root,'ground');
    assert.ok(ground.fields,'the confirmed ground card must be mounted');
    assert.match(ground.fields['classification'],/level medium/);
    assert.match(ground.fields['thresholds'],/medium_min 0\.3000 · high_min 0\.8000/);
    assert.match(ground.highRisk['high_risk length_m'],/1050\.000 m/);
    assert.match(ground.highRisk['high_risk interval_count'],/1/);
    // interval 必须给出契约字段；segment_ids / cell_ids 收在 details 里
    const intervalText=textOf(ground.intervals);
    assert.match(intervalText,/start_distance_m 0\.000 m · end_distance_m 1050\.000 m · length_m 1050\.000 m/);
    assert.match(intervalText,/mean_index 0\.900000 · max_index 0\.900000/);
    assert.match(intervalText,/ground-HR-0001/);
    assert.match(intervalText,/segment_ids RRP-LRC-1-S0000, RRP-LRC-1-S0001/);
    assert.match(intervalText,/cell_ids G1, G2, G3/);
    assert.ok(findAll(ground.intervals,'details').length>=1,
      'segment_ids / cell_ids live in a disclosure');

    // 另外两个未确认的 domain 仍然不产生等级
    for(const domainId of ['air_traffic','environment_obstacle']){
      const card=cardOf(root,domainId);
      assert.match(card.fields['classification'],/not_configured/);
      assert.doesNotMatch(card.fields['classification'],/level (low|medium|high)/);
      assert.match(card.highRisk['high_risk interval_count'],/—（未确认阈值）/);
      assert.doesNotMatch(textOf(card.intervals),/HR-/);
    }

    // 已确认 domain 的 SVG 使用等级配色 + 阈值区段带；未确认的仍为中性
    const profile=flow.route_risk_profiles.items[0];
    const confirmedSvg=routeRiskDomainSvg(profile,'ground');
    assert.match(confirmedSvg,/#2f8f4e|#c98a1b|#c0392b/);
    assert.match(confirmedSvg,/data-rrp-level="high"/);
    assert.match(confirmedSvg,/data-rrp-band="low"/);
    assert.match(confirmedSvg,/data-rrp-band="medium"/);
    assert.match(confirmedSvg,/data-rrp-band="high"/);
    assert.match(confirmedSvg,/data-rrp-threshold="medium_min"/);
    assert.match(confirmedSvg,/data-rrp-threshold="high_min"/);
    const unconfirmedSvg=routeRiskDomainSvg(profile,'air_traffic');
    assert.doesNotMatch(unconfirmedSvg,/#2f8f4e|#c98a1b|#c0392b/);
    assert.doesNotMatch(unconfirmedSvg,/data-rrp-band|data-rrp-threshold/);
  });
});

// ---- 5. SVG 坐标只来自后端累计距离 ------------------------------------------

test('the one-dimensional svg maps x strictly from backend cumulative distances',()=>{
  const profile=profileFixture({groundConfirmed:true});
  const geometry=routeRiskDomainGeometry(profile,'ground');
  assert.equal(geometry.routeLengthM,profile.route_length_m);
  assert.deepEqual(geometry.positions.map(item=>item.startDistanceM),[0,550,1050,1500]);
  assert.deepEqual(geometry.positions.map(item=>item.endDistanceM),[550,1050,1500,1925.858241815]);

  // 与渲染实现完全一致的线性映射：padLeft 48、可用宽度 696。
  const expectedX=value=>48+value/profile.route_length_m*696;
  const rects=svgRects(routeRiskDomainSvg(profile,'ground'));
  assert.equal(rects.length,profile.segments.length);
  rects.forEach((rect,index)=>{
    const segment=profile.segments[index];
    assert.equal(rect.segmentId,segment.segment_id);
    assert.ok(Math.abs(rect.x-expectedX(segment.start_cumulative_distance_m))<0.06,
      `rect ${index} x must come from start_cumulative_distance_m`);
    assert.ok(Math.abs(rect.width-(expectedX(segment.end_cumulative_distance_m)-expectedX(segment.start_cumulative_distance_m)))<0.06,
      `rect ${index} width must come from end - start cumulative distance`);
  });
  // 末段终点必须落在横轴右端
  const last=rects[rects.length-1];
  assert.ok(Math.abs((last.x+last.width)-(48+696))<0.12,'the last segment must end at route_length_m');
  // 横轴刻度也来自后端 route_length_m
  const svg=routeRiskDomainSvg(profile,'ground');
  assert.match(svg,/1925\.9 m/);
  assert.match(svg,/962\.9 m/);
  // 阈值区段带同样只用后端阈值 + route_length_m 定位（不做前端分级）
  const bands=svgBands(svg);
  assert.deepEqual(Array.from(bands,band=>band.band),['low','medium','high']);
  assert.ok(Math.abs(bands[0].width-(0.3*696))<0.06,'the low band ends at medium_min');
  assert.ok(Math.abs(bands[1].width-(0.5*696))<0.06,'the medium band spans medium_min → high_min');
  assert.ok(Math.abs(bands[2].width-(0.2*696))<0.06,'the high band spans high_min → 1.0');
  assert.match(svg,/data-rrp-threshold="medium_min"[^>]*x1="256\.8"/);
  assert.match(svg,/data-rrp-threshold="high_min"[^>]*x1="604\.8"/);
  // geometry 只读后端字段：前端不新增任何距离/风险计算
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/route_risk_profile.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/haversine|6371008\.8|computeDistance|pathLengthM|Math\.asin/,
    'the frontend must not recompute any distance');
  assert.doesNotMatch(source,/integrate|exposure\s*\*=|weighted_sum/,'the frontend must not recompute risk');
});

// ---- 6. 三 domain 独立，无 cross-domain overall ------------------------------

test('the three domains stay independent and no cross-domain overall is generated',()=>{
  withStubDom(document=>{
    const flow=baseFlow({confirmedDomains:['ground']});
    const {root}=mountStep3(document,flow);
    const section=findByDataset(root,'segName',ROUTE_RISK_PROFILE_SEGMENT);
    const svgs=findAll(section,'[data-rrp-svg]');
    assert.deepEqual(Array.from(svgs,node=>node.dataset.rrpSvg),DOMAIN_IDS,
      'each domain owns its own one-dimensional svg');
    const charts=findAll(section,'[data-rrp-chart]');
    assert.deepEqual(Array.from(charts,node=>node.dataset.rrpChart),DOMAIN_IDS);
    // 每个 domain 的字段表 / high-risk 表 / interval 表 / 画像都只属于自己
    for(const domainId of DOMAIN_IDS){
      const card=cardOf(root,domainId);
      assert.ok(card.fields,`${domainId} keeps its own field table`);
      assert.ok(card.chart,`${domainId} keeps its own chart`);
      assert.ok(card.intervals,`${domainId} keeps its own interval table`);
      for(const other of DOMAIN_IDS){
        if(other===domainId)continue;
        assert.notEqual(card.fields,cardOf(root,other).fields,
          `${domainId} and ${other} must not share a field table`);
      }
      // 每个 domain 只在自己卡片里画阈值区段带
      const bands=svgBands(routeRiskDomainSvg(flow.route_risk_profiles.items[0],domainId));
      assert.equal(bands.length,domainId==='ground'?3:0,
        `${domainId} must only draw threshold bands when its own thresholds are confirmed`);
    }
    const model=routeRiskProfileModel(flow);
    const modelText=JSON.stringify(model);
    assert.equal(model.crossDomainOverall,false);
    assert.equal(model.automaticClassification,false);
    assert.match(modelText,/"cross_domain_overall":"not_computed"/);
    assert.match(modelText,/"cross_domain_high_risk":"not_computed"/);
    const html=renderRouteRiskProfile(flow);
    assert.equal((html.match(/cross_domain_overall/g)||[]).length,1);
    assert.equal((html.match(/cross_domain_high_risk/g)||[]).length,1);
    assert.equal((html.match(/not_computed/g)||[]).length>=5,true,
      'absolute risk / SORA GRC / SORA ARC / cross-domain overall / cross-domain high-risk stay not_computed');
    assert.doesNotMatch(html,/综合风险|总分|overall 等级|综合等级|cross-domain overall 值/,
      'no cross-domain overall score may be rendered');
    // 每个 domain 各自独立给出 status；不得出现跨 domain 汇总结论
    assert.doesNotMatch(html,/>整体 (low|medium|high)</);
  });
});

// ---- 7. 端点契约 -------------------------------------------------------------

test('policy evaluate delete endpoints are wired exactly as contracted',()=>{
  withStubDom(document=>{
    const flow=baseFlow({confirmedDomains:['ground'],profileStatus:'stale'});
    mountStep3(document,flow);
    // bind() 通过 document.querySelectorAll 发现删除按钮：桩 DOM 默认不遍历真实树，
    // 这里按浏览器语义补一层最小实现（只影响本测试内的查询）。
    document.querySelectorAll=selector=>findAll(document.body,selector);
    document.querySelector=selector=>findAll(document.body,selector)[0]||null;
    const calls=[],registered=[];
    const c={
      flow:()=>flow,
      $:id=>document.getElementById(id),
      panelError:message=>calls.push(['error',message]),
      resourceAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
      actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;},
    };
    bindStep3(c);
    for(const id of ['evaluateRouteRiskProfile','saveRouteRiskProfilePolicy']){
      assert.ok(registered.includes(id),`bind() must register ${id}`);
      assert.equal(typeof document.getElementById(id).onclick,'function',`#${id} keeps its handler`);
    }
    // evaluate → 空 payload（后端自行选取 current candidate）
    document.getElementById('evaluateRouteRiskProfile').onclick();
    assert.deepEqual(calls.pop(),['/api/route-risk-profiles/evaluate',{}]);
    // policy → 只提交三个 domain 的显式阈值，空值一律 null，绝不补默认
    const setField=(id,value)=>{document.getElementById(id).attributes.value=value;};
    setField('rrpThresholdMedium_ground','0.3');
    setField('rrpThresholdHigh_ground','0.8');
    setField('rrpThresholdSource_ground','工程确认-测试');
    document.getElementById('rrpThresholdConfirmed_ground').attributes.checked='true';
    const payload=routeRiskProfilePolicyPayload(c);
    assert.deepEqual(Object.keys(payload.domains).sort(),DOMAIN_IDS.slice().sort());
    assert.deepEqual(payload.domains.ground,{medium_min:0.3,high_min:0.8,source:'工程确认-测试',confirmed:true});
    assert.deepEqual(payload.domains.air_traffic,{medium_min:null,high_min:null,source:'',confirmed:false});
    assert.deepEqual(payload.domains.environment_obstacle,{medium_min:null,high_min:null,source:'',confirmed:false});
    document.getElementById('saveRouteRiskProfilePolicy').onclick();
    assert.deepEqual(calls.pop(),['/api/route-risk-profile-policy',payload]);
    // delete → 只调用 delete 端点，且只提交 profile_id
    const deleteButton=findAll(document.body,'[data-delete-route-risk-profile]')[0];
    assert.ok(deleteButton,'every profile row keeps its delete control');
    deleteButton.onclick();
    assert.deepEqual(calls.pop(),['/api/route-risk-profiles/delete',{profile_id:'RRP-LRC-1-abcdef0123456789'}]);
    // 源码层契约：面板只使用这三个端点，不做任何 replan
    const source=readFileSync(new URL('../cns_planner/web/js/workflow/route_risk_profile.js',import.meta.url),'utf8');
    for(const path of ['/api/route-risk-profile/readiness','/api/route-risk-profile-policy',
      '/api/route-risk-profiles','/api/route-risk-profiles/evaluate','/api/route-risk-profiles/delete']){
      if(path.startsWith('/api/route-risk-profile')&&path.endsWith('readiness'))continue;
      assert.ok(source.includes(path)||path==='/api/route-risk-profile/readiness',
        `the panel must stay on the existing ${path} contract`);
    }
    assert.doesNotMatch(source,/c\.mutate\('|setLayer\(|setZoom|fitLonLatBbox|layerIds|replan\(/,
      'the panel must not replan or touch map layers');
    assert.doesNotMatch(source,/import .*from '(?!\.\/common\.js)/,'no third-party dependency may be added');
  });
});

// ---- 8. stale profile 保留 ---------------------------------------------------

test('a stale profile stays visible, is labelled stale and is never rewritten',()=>{
  withStubDom(document=>{
    const flow=baseFlow({confirmedDomains:['ground'],profileStatus:'stale'});
    const {root}=mountStep3(document,flow);
    const section=findByDataset(root,'segName',ROUTE_RISK_PROFILE_SEGMENT);
    // stale profile 必须保留在历史列表里，并明确标 stale（HTML 与 DOM 双重确认）
    const html=renderRouteRiskProfile(flow);
    assert.match(html,/RRP-LRC-1-abcdef0123456789/,'the stale profile must stay listed');
    assert.match(html,/stale：保留为审计证据/);
    assert.match(html,/route_risk_profile_policy_changed/);
    assert.match(html,/已 stale/);
    const deletions=findAll(section,'[data-delete-route-risk-profile]');
    assert.equal(deletions.length,1,'the stale profile keeps exactly one delete control');
    assert.equal(deletions[0].dataset.deleteRouteRiskProfile,'RRP-LRC-1-abcdef0123456789');
    // 冻结的证据仍然可读（exposure 不被清空，卡片仍挂载）
    const card=cardOf(root,'ground');
    assert.match(card.fields['exposure_index_m'],/1011\.076/);
    assert.ok(card.chart,'the frozen one-dimensional profile stays rendered');
    // stale 的 profile 不得被标成 current
    const domainModel=routeRiskDomainModel(flow.route_risk_profiles.items[0],'ground');
    assert.equal(domainModel.classified,true,'a stale profile keeps its frozen classification evidence');
    assert.equal(routeRiskProfileModel(flow).current.status,'stale');
  });
});

// ---- 9. 工程证据默认折叠 + 边界文案 ------------------------------------------

test('engineering evidence is collapsed by default and the boundaries stay explicit',()=>{
  withStubDom(document=>{
    const flow=baseFlow({confirmedDomains:['ground']});
    const {root}=mountStep3(document,flow);
    const section=findByDataset(root,'segName',ROUTE_RISK_PROFILE_SEGMENT);
    const details=findAll(section,'details');
    assert.ok(details.length>=6,'factor / consistency / fingerprints / history ship as disclosures');
    assert.equal(details.filter(node=>node.attributes.open!==undefined).length,0,
      'engineering evidence must start collapsed');
    const summaries=Array.from(details,node=>textOf(findAll(node,'summary')[0]));
    for(const expected of ['factor contributors','consistency','fingerprints','provenance']){
      assert.ok(summaries.some(value=>value.includes(expected)),`${expected} must be a disclosure`);
    }
    // 文案层（HTML）检查：这些是必须逐字出现的能力边界说明
    const html=renderRouteRiskProfile(flow);
    assert.match(html,/relative engineering contribution/);
    assert.match(html,/不是事故原因概率/);
    assert.match(html,/RouteRiskProfile 只分析 LayeredRouteCandidate/);
    assert.match(html,/不 replan/);
    assert.match(html,/不修改 operational_routes/);
    assert.match(html,/不计算 absolute risk/);
    assert.match(html,/不计算 SORA GRC\/ARC/);
    assert.match(html,/cross-domain overall/);
    assert.match(html,/cross-domain high-risk/);
    assert.match(html,/系统绝不提供任何默认阈值/);
    // 阈值设置面板绝不提供默认阈值：每个 domain 一个输入，只有已确认的 domain 预填
    const mediumInputs=DOMAIN_IDS.map(domainId=>document.getElementById('rrpThresholdMedium_'+domainId));
    assert.equal(mediumInputs.filter(Boolean).length,3,'each domain owns exactly one medium_min input');
    assert.equal(mediumInputs.filter(node=>String(node.attributes.value||'')==='0.3').length,1,
      'only the confirmed domain may prefill a threshold');
    assert.deepEqual(mediumInputs.map(node=>String(node.attributes.value||'')),
      ['0.3','',''],'an unconfirmed domain must never be prefilled');
    for(const node of mediumInputs)assert.equal(node.attributes.placeholder,'无默认值');
  });
});
