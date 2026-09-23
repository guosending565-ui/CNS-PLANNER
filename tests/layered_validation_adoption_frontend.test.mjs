/**
 * production LayeredRouteCandidate 的 Continuous Validation + Operational Adoption
 * 前端集成回归测试。
 *
 * 目标：锁定"只做前端集成"这一层契约，而不是重新验证后端数学：
 *  - Step03 结果区 res-feasibility 在既有 data readiness / building clearance 之后
 *    接入连续验证；操作区 op-operational 把 A. Legacy 运行航路生成 与
 *    B. Layered Candidate 发布 显式分开；
 *  - readiness / blockers 原样转印，前端不重新判断；
 *  - validated_candidate / failed / unresolved / validation_incomplete / not_ready / stale
 *    严格转印、互不混淆：unresolved 与 validation_incomplete 绝不显示为 failed；
 *  - 只有 current_applicability==='current' 才算"当前验证"；stale 只进历史；
 *  - evaluate-real 端点按契约调用，且本面板不写任何 operational state；
 *  - Preview 不写 state、不携带 Apply 副作用；2D path 不携带高度第三坐标；
 *  - route_id 冲突默认禁止 Apply（replace_existing 默认 false）；
 *  - Apply 必须先有当前 Preview、publication_allowed=true 且显式勾选确认；
 *  - Apply 提交的 expected_validation_fingerprint 只能是 Preview 冻结的值，
 *    Preview 后证据变化必须提示"证据已变化，请重新 Preview"且不自动重试；
 *  - Revoke 必须显式 confirmed，且是"撤销本次发布"而不是"删除航路"；
 *  - Legacy operationalRoutes 入口与契约不变；
 *  - Step03 现有 segments 数量、控件与 id 唯一性不回归。
 *
 * 这里自带一份最小 DOM 桩（与 workbench_shell.test.mjs 的桩同构），因此本文件
 * 可以独立运行：`node tests/layered_validation_adoption_frontend.test.mjs`。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {render as renderStep3,bind as bindStep3} from '../cns_planner/web/js/workflow/step03_routes.js';
import {createWorkbench} from '../cns_planner/web/js/workflow/workbench.js';
import {renderWorkflowSteps} from '../cns_planner/web/js/workflow/steps.js';
import {
  LAYERED_VALIDATION_DISTINCT_STATUSES,LAYERED_VALIDATION_STATUSES,
  layeredRouteValidationModel,layeredValidationCurrentValidation,layeredValidationDomainModel,
  layeredValidationFingerprint,layeredValidationStatusModel,renderLayeredRouteValidation,
} from '../cns_planner/web/js/workflow/layered_route_validation.js';
import {
  LAYERED_ADOPTION_CHAIN_STAGES,LAYERED_ADOPTION_EVIDENCE_CHANGED,LAYERED_ADOPTION_INTENT_CHANGED,
  LAYERED_ADOPTION_LEGACY_LABEL,LAYERED_ADOPTION_REVOKE_LABEL,LAYERED_ADOPTION_REVOKE_TARGET_NAME,
  LAYERED_ADOPTION_STATUSES,
  bindLayeredOperationalAdoption,
  cacheLayeredAdoptionPreview,clearLayeredAdoptionCache,layeredAdoptionApplyGuard,
  layeredAdoptionChainModel,layeredAdoptionLastApply,layeredAdoptionPreviewPayload,
  layeredAdoptionPreviewState,layeredAdoptionRevokeGuard,layeredAdoptionRevokePayload,
  layeredAdoptionRevokeTargets,
  layeredOperationalAdoptionModel,recordLayeredAdoptionApply,refreshLayeredAdoptionAffordances,
  renderLayeredOperationalAdoption,
  selectedLayeredAdoptionReplaceExisting,selectedLayeredAdoptionRevokeTarget,
  selectedLayeredAdoptionRevokeTargetId,syncSelectedLayeredAdoptionRevokeTarget,
} from '../cns_planner/web/js/workflow/layered_operational_adoption.js';

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
    this.checked=false;this.value='';this.disabled=false;this.onchange=null;
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

/** 支持 tag / .class / [attr] / [attr="value"] 的任意组合（如 input[name="x"]）。 */
function parseCompound(selector){
  const parts=[];
  const token=/^([A-Za-z][-A-Za-z0-9]*)|^\.([A-Za-z][-A-Za-z0-9_]*)|^\[([a-zA-Z-]+)(?:="([^"]*)")?\]/;
  let rest=String(selector).trim();
  while(rest){
    const match=token.exec(rest);
    if(!match)return [{kind:'none'}];
    if(match[1])parts.push({kind:'tag',name:match[1].toUpperCase()});
    else if(match[2])parts.push({kind:'class',name:match[2]});
    else parts.push({kind:'attr',name:match[3],value:match[4]===undefined?null:match[4]});
    rest=rest.slice(match[0].length);
  }
  return parts.length?parts:[{kind:'none'}];
}

function parseSelector(selector){
  return String(selector).split(',').map(part=>parseCompound(part));
}

function toDatasetKey(attribute){return attribute.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());}

function matchesSelector(node,parsed){
  const parts=Array.isArray(parsed[0])?parsed[0]:parsed;
  return parts.every(part=>{
    if(part.kind==='tag')return node.tagName===part.name;
    if(part.kind==='class')return node.classList.contains(part.name);
    if(part.kind!=='attr')return false;
    // 桩 DOM 同时保留 dataset（data-*）与 attributes（其余属性），
    // 因此 input[name="..."] / input[type="..."] 这类选择器也能被查询到。
    const base=part.name.startsWith('data-')?part.name.slice(5):part.name;
    const fromDataset=node.dataset[toDatasetKey(base)];
    const fromAttributes=node.attributes?node.attributes[part.name]:undefined;
    const current=fromDataset!==undefined?fromDataset:fromAttributes;
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
    if(name==='checked')node.checked=true;
    if(name==='disabled')node.disabled=true;
    if(name==='value')node.value=value;
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

// ---- flow 替身（与后端 result_snapshot / readiness_snapshot 同形） -------------

function sourceAudit(role,status='verified'){
  return {role,status,file_name:role+'.tif',size_bytes:1024,mtime_ns:'1',sha256:role+'-sha'};
}

function validationDomains({terrainStatus='passed',buildingStatus='passed'}={}){
  const domain=(status,minimumMargin)=>{
    const failed=status==='failed';
    const unresolved=status==='unresolved';
    return {
      status,
      reason:failed?'interval_below_required_clearance':unresolved?'insufficient_source_evidence':null,
      minimum_margin:minimumMargin,
      violations:failed?[{
        interval_id:'I-'+status+'-1',domain:status==='failed'?'terrain':'building',
        start_distance_m:120.5,end_distance_m:260.0,status:'failed',
        reason_id:'terrain_clearance_below_policy',source_id:'terrain_dtm',
      }]:[],
      unresolved:unresolved?[{
        interval_id:'U-'+status+'-1',domain:'terrain',start_distance_m:0.0,end_distance_m:100.0,
        status:'unresolved',reason_id:'native_terrain_pixel_nodata',source_id:'terrain_dtm',
      }]:[],
      evidence:{source:{id:'terrain_dtm',crs:'EPSG:32651'}},
    };
  };
  return {
    terrain:domain(terrainStatus,-5.0),
    building:domain(buildingStatus,12.4),
    airspace:{status:'not_applicable',applicability:'display_only',used_in_validation:false,
      used_in_fingerprint:false},
  };
}

/** 一条 LayeredRouteValidation 记录（严格与后端字段同形）。 */
function validationFixture({
  validationId='LRV-AAAAAAAAAAAA',status='validated_candidate',applicability='current',
  candidateFingerprint='cand-fp',pathFingerprint='path-fp',validationFingerprint='layeredvalidationv1-fp-1',
  staleReason=null,terrainStatus='passed',buildingStatus='passed',
}={}){
  const limitReached=status==='validation_incomplete';
  const notReady=status==='not_ready';
  return {
    schema_version:'layered-route-validation-v1',
    validation_id:validationId,
    status,
    status_reason:notReady?'validation_prerequisites_not_ready':null,
    blocking_reasons:notReady?[{
      reason_code:'current_route_risk_profile_missing',
      reason:'必须先存在该 current candidate 的 current RouteRiskProfile；classification 不作为 hard gate',
    }]:[],
    validated_at:'2026-02-01T00:00:00+00:00',
    candidate:{
      candidate_id:'LRC-1',route_id:'R-1',altitude_layer_id:'L8-LOW',
      candidate_fingerprint:candidateFingerprint,path_fingerprint:pathFingerprint,
      status:'candidate',current_applicability:'current',
    },
    route:{
      path_crs:'OGC:CRS84',path:[[122.0,30.0],[122.001,30.0]],
      metric_path:[[0,0],[100,0]],metric_crs:'EPSG:32651',nominal_altitude_m:100.0,
      vertical_reference:'egm2008_orthometric',altitude_model:'constant_cruise_altitude',
      semantics:'strategic_route_centerline_not_aircraft_kinematic_trajectory',
      two_dimensional_source_path:true,
    },
    domains:validationDomains({terrainStatus,buildingStatus}),
    minimum_margins:{terrain_vertical_m:-5.0,building_vertical_m:12.4},
    failed_intervals:terrainStatus==='failed'?[{
      interval_id:'I-1',domain:'terrain',start_distance_m:120.5,end_distance_m:260.0,
      status:'failed',reason_id:'terrain_clearance_below_policy',source_id:'terrain_dtm',
    }]:[],
    unresolved_intervals:terrainStatus==='unresolved'?[{
      interval_id:'U-1',domain:'terrain',start_distance_m:0.0,end_distance_m:100.0,
      status:'unresolved',reason_id:'native_terrain_pixel_nodata',source_id:'terrain_dtm',
    }]:[],
    critical_evidence:[],
    source_type:'configured_real_sources',
    source_audits:{terrain_dtm:sourceAudit('terrain_dtm'),buildings:sourceAudit('buildings')},
    policies:{terrain_vertical_clearance_m:10.0,building_horizontal_clearance_m:5.0,
      building_vertical_clearance_m:10.0},
    resource_limits:{max_evidence_items:null,observed_evidence_items:limitReached?1:2,
      limit_reached:limitReached,safety_parameter:false},
    validator_versions:{terrain:'source_native_terrain_validator_v1',
      building:'real_footprint_building_validator_v1'},
    fingerprints:{validation_fingerprint:validationFingerprint,candidate_fingerprint:candidateFingerprint,
      path_fingerprint:pathFingerprint,components:{}},
    operational_route:false,
    cns_assessed:false,
    current_applicability:applicability,
    stale_reason:staleReason,
    provenance:{source_adapter:'production-adapter',planning_or_geometry_mutation:false},
    semantics:{candidate_is_not_validation:true,validation_is_not_operational_route:true,
      airspace_not_applicable_display_only:true,resource_limit_is_computational_not_safety:true},
  };
}

/** readiness 替身：blockers 由参数决定，前端只负责转印。 */
function validationReadinessFixture({status='ready',candidateStatus='candidate',
  applicability='current',cruiseStatus='confirmed',profileId='RRP-1',
  chainStatus='ready',blockers=[]}={}){
  return {
    status,
    algorithm:{algorithm_id:'layered_candidate_continuous_validation_v1',algorithm_version:'1.0'},
    candidate:{candidate_id:'LRC-1',route_id:'R-1',altitude_layer_id:'L8-LOW',
      candidate_fingerprint:'cand-fp',path_fingerprint:'path-fp',status:candidateStatus,
      current_applicability:applicability},
    altitude_layer:{altitude_layer_id:'L8-LOW',nominal_altitude_m:100.0,
      vertical_reference:'egm2008_orthometric',confirmed:true,status:'confirmed',source:'engineering'},
    cruise_altitude:{status:cruiseStatus,altitude_egm2008_m:cruiseStatus==='confirmed'?100.0:null,
      vertical_reference:'egm2008_orthometric'},
    route_risk_profile:profileId?{profile_id:profileId,status:'passed',current_applicability:'current',
      classification_used_as_hard_gate:false,thresholds_used_in_validation_fingerprint:false}:{},
    source_chain:{status:chainStatus,source_type:'configured_real_sources',
      audits:{terrain_dtm:sourceAudit('terrain_dtm',chainStatus==='ready'?'verified':'not_verified'),
        buildings:sourceAudit('buildings',chainStatus==='ready'?'verified':'not_verified')},
      blockers:chainStatus==='ready'?[]:[{reason_code:'buildings_source_not_verified',
        reason:'buildings source audit 必须为 verified'}]},
    blockers,
    airspace:{status:'not_applicable',applicability:'display_only',used_in_validation:false,
      used_in_fingerprint:false},
    semantics:{route:'strategic_route_centerline_not_aircraft_kinematic_trajectory',
      no_replan_refine_rounding_or_cross_layer:true,constant_egm2008_cruise_altitude:true,
      risk_profile_is_audit_prerequisite_not_a_classification_gate:true},
  };
}

function adoptionReadinessFixture(validations){
  const options=validations.map(item=>({
    validation_id:item.validation_id,
    route_id:(item.candidate||{}).route_id,
    status:item.status,
    current_applicability:item.current_applicability,
    eligible:item.status==='validated_candidate'&&item.current_applicability==='current',
    reasons:item.status==='validated_candidate'&&item.current_applicability==='current'
      ?[]:['validation_status:'+item.status,'validation_not_current'],
  }));
  return {
    status:options.some(item=>item.eligible)?'ready':'not_ready',
    algorithm:{algorithm_id:'layered_operational_adoption_v1',algorithm_version:'1.0'},
    options,
    boundaries:{explicit_confirmation_required:true,configured_real_sources_only:true,
      candidate_validation_operational_are_distinct:true,route_altitude_profile_not_created:true,
      departure_arrival_procedure_not_created:true},
  };
}

function adoptionFixture({adoptionId='LRA-111111111111',status='published',
  applicability='current',routeId='R-1',validationId='LRV-AAAAAAAAAAAA',
  validationFingerprint='layeredvalidationv1-fp-1'}={}){
  return {
    schema_version:'layered-operational-adoption-v1',
    adoption_id:adoptionId,route_id:routeId,status,current_applicability:applicability,
    applied_at:'2026-02-01T01:00:00+00:00',revoked_at:status==='revoked'?'2026-02-01T02:00:00+00:00':null,
    validation_id:validationId,validation_fingerprint:validationFingerprint,
    candidate_id:'LRC-1',candidate_fingerprint:'cand-fp',
    projection_fingerprint:'layeredprojectionv1-proj',altitude_layer_id:'L8-LOW',
    source_type:'layered_candidate_operational_adoption_v1',replace_existing:false,
    before:{route:null,route_operating_layer:null},
    after:{route:{route_id:routeId,path:[[122.0,30.0],[122.001,30.0]]},
      route_operating_layer:{route_id:routeId,altitude_layer_id:'L8-LOW'}},
    ownership:{route_owned:true,route_operating_layer_owned:true},
    stale_reason:status==='stale'?'validation_stale':null,
    provenance:{explicit_confirmation:true,configured_real_sources:true,
      route_altitude_profile_created:false,departure_arrival_procedure_created:false},
  };
}

function baseFlow(overrides={}){
  const validations=overrides.validations||[validationFixture()];
  return {
    project:{name:'测试项目'},
    nodes:[{node_id:'N001',name:'A',coordinate:[122,30]},{node_id:'N002',name:'B',coordinate:[122.001,30]}],
    scenario_routes:[{route_id:'R-1',direction:'N001→N002',path:[[122,30],[122.001,30]]}],
    operational_routes:[],
    algorithm_selection:{route_planner:{algorithm_id:'layered_risk_aware_route_planner_v1',version:'1.0',parameters:{}}},
    algorithm_catalog:[],retired_route_ids:[],
    risks:{environment:{status:'not_calculated'}},
    steps:{1:true,2:true,3:true},
    spatial_3d:{route_altitude_profiles:{},altitude_layers:[],route_operating_layers:[],
      departure_arrival_procedures:[]},
    operational_timing:{route_motion_profiles:{}},
    route_vertical_profiles:{},
    building_clearance_policy:{status:'confirmed',horizontal_clearance_m:5.0,vertical_clearance_m:10.0},
    building_clearance_assessment:{},
    layered_route_feasibility_policy:{status:'confirmed',terrain_vertical_clearance_m:10.0},
    reference_routes:{items:[],points:[]},reference_landing_sites:{items:[]},
    route_planning_experiments:{},reference_route_links:{items:[]},
    reference_endpoint_candidates:{},data_readiness:{blocks:{}},
    workspace:{bbox:[122,29.9,122.002,30.1],area_km2:12.5,health:{loaded_layer_count:4}},
    grid:{status:'passed',level:8,count:4},
    // ---- production LayeredRouteCandidate Continuous Validation / Adoption additive 分片 ----
    layered_route_validation_readiness:overrides.readiness||validationReadinessFixture(),
    layered_route_validations:overrides.collection||{
      schema_version:'layered-route-validation-collection-v1',status:'passed',count:validations.length,
      active_validation_id:(validations.find(item=>item.status==='validated_candidate')||{}).validation_id||null,
      items:validations,notes:[],
    },
    layered_operational_adoption_readiness:overrides.adoptionReadiness||adoptionReadinessFixture(validations),
    layered_operational_adoptions:overrides.adoptions||{
      schema_version:'layered-operational-adoption-collection-v1',status:'not_calculated',count:0,items:[],
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

function mountStep3(document,flow,store={step:3,tab:'result',segs:{result:'res-feasibility'},scroll:0}){
  const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
  const root=renderWorkflowSteps({step:{render:renderStep3},context:stepContext(flow)});
  controller.mount({root,step:{number:3,title:'航路规划',note:''}});
  return {controller,root,store};
}

function textOf(node){
  if(!node)return '';
  let text=String(node.textContent||'');
  forEachDescendant(node,child=>{text+=String(child.textContent||'');});
  return text;
}

function classOf(node){
  return node?String(node.className||''):'';
}

// ---- 1. Step03 结构 -----------------------------------------------------------

test('step 03 keeps its segment count and mounts the validation panel after clearance',()=>{
  const flow=baseFlow();
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  // 分段数量与既有 13 个完全一致：本轮只改面板内容，不新增/删除 segment。
  assert.deepEqual(segs,[
    'op-sites','op-candidates','op-operational','op-altitude',
    'res-route','res-feasibility','res-risk-profile','res-compare',
    'adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile',
  ]);
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  // Continuous Validation 落在 res-feasibility，且在 data readiness / building clearance 之后。
  const feasibility=html.slice(html.indexOf('data-seg-name="res-feasibility"'),
    html.indexOf('data-seg-name="res-risk-profile"'));
  const readinessAt=feasibility.indexOf('数据就绪');
  const clearanceAt=feasibility.indexOf('三维建筑净空');
  const validationAt=feasibility.indexOf('连续验证就绪');
  assert.ok(readinessAt>=0&&clearanceAt>=0&&validationAt>=0,'all three blocks must be present');
  assert.ok(readinessAt<clearanceAt&&clearanceAt<validationAt,
    'validation must follow data readiness and building clearance');
  // 操作区 A/B 两块：Legacy 运行航路生成与 Layered 发布显式分开。
  assert.match(html,new RegExp(LAYERED_ADOPTION_LEGACY_LABEL.replace(/[.*+?^${}()|[\]\\/]/g,'\\$&')));
  assert.match(html,/B\. Layered Candidate 发布/);
  for(const id of ['scenarioRoutes','operationalRoutes','evaluateLayeredRouteValidation',
    'previewLayeredAdoption','applyLayeredAdoption','revokeLayeredAdoption',
    'layeredAdoptionApplyConfirmed','layeredAdoptionRevokeConfirmed']){
    assert.match(html,new RegExp('id="'+id+'"'),`#${id} must be rendered`);
  }
});

test('step 03 keeps every original control and unique ids after the additive panels',()=>{
  withStubDom(document=>{
    const {root}=mountStep3(document,baseFlow());
    for(const id of ['createOdRoute','saveRouteAltitude','saveRouteMotion','saveBuildingClearancePolicy',
      'evaluateRoutePlannerV3','scenarioRoutes','operationalRoutes','nextStep','evaluateRouteRiskProfile',
      'evaluateLayeredRouteValidation','previewLayeredAdoption','applyLayeredAdoption','revokeLayeredAdoption']){
      assert.ok(document.getElementById(id),`#${id} must stay mounted`);
    }
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,
      `duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
    // 新控件必须落在正常正文里，而不是 .wb-section-head
    for(const id of ['evaluateLayeredRouteValidation','previewLayeredAdoption','applyLayeredAdoption',
      'revokeLayeredAdoption']){
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
  });
});

// ---- 2. readiness / blockers 原样转印 -----------------------------------------

test('validation readiness and blockers are transcribed verbatim and gate the button',()=>{
  const blockers=[
    {reason_code:'current_candidate_missing',reason:'缺少 current LayeredRouteCandidate'},
    {reason_code:'altitude_layer_not_direct_egm2008',
      reason:'AltitudeLayer 必须已确认且 nominal_altitude_m 可直接解析为 EGM2008 orthometric'},
    {reason_code:'terrain_clearance_not_confirmed',
      reason:'layered_route_feasibility_policy.terrain_vertical_clearance_m 未确认'},
    {reason_code:'building_clearance_not_confirmed',
      reason:'building_clearance_policy 的 horizontal/vertical clearance 未确认'},
    {reason_code:'current_route_risk_profile_missing',
      reason:'必须先存在该 current candidate 的 current RouteRiskProfile；classification 不作为 hard gate'},
    {reason_code:'terrain_dtm_source_not_verified',reason:'terrain_dtm source audit 必须为 verified'},
    {reason_code:'buildings_source_not_verified',reason:'buildings source audit 必须为 verified'},
  ];
  const notReady=baseFlow({
    readiness:validationReadinessFixture({status:'not_ready',candidateStatus:null,applicability:null,
      cruiseStatus:'pending_confirmation',profileId:null,chainStatus:'not_ready',blockers}),
    collection:{status:'not_calculated',count:0,items:[],active_validation_id:null,notes:[]},
  });
  const html=renderLayeredRouteValidation(notReady);
  for(const blocker of blockers){
    assert.match(html,new RegExp(blocker.reason_code));
    assert.match(html,new RegExp(blocker.reason.replace(/[.*+?^${}()|[\]\\/]/g,'\\$&')));
  }
  assert.match(html,/id="evaluateLayeredRouteValidation" disabled/,
    'a not_ready readiness disables the button');
  assert.match(html,/not_ready（blocker 未清除）/);
  const model=layeredRouteValidationModel(notReady);
  assert.equal(model.readinessStatus,'not_ready');
  assert.deepEqual(model.blockers,blockers,'blockers are transcribed verbatim');

  const ready=renderLayeredRouteValidation(baseFlow());
  assert.doesNotMatch(ready,/id="evaluateLayeredRouteValidation" disabled/);
  assert.match(ready,/ready（无 blocker）/);
  // readiness 里出现的能力边界必须原样呈现
  assert.match(ready,/layered_candidate_continuous_validation_v1/);
  assert.match(ready,/egm2008_orthometric/);
  assert.match(ready,/classification 用作 hard gate false/);
  assert.match(ready,/阈值进入 validation fingerprint false/);
  assert.match(ready,/terrain_dtm-sha/);
  assert.match(ready,/buildings-sha/);
  assert.match(ready,/used_in_validation false/);
  assert.match(ready,/display_only/);
  assert.match(ready,/不 replan、不 refine、不四舍五入、不跨层/);
});

// ---- 3. 六个状态严格不混淆 -----------------------------------------------------

test('validated failed unresolved incomplete not_ready and stale never masquerade as each other',()=>{
  assert.deepEqual(LAYERED_VALIDATION_DISTINCT_STATUSES.slice().sort(),
    LAYERED_VALIDATION_STATUSES.slice().sort());
  const cases=[
    ['validated_candidate',{status:'validated_candidate'}],
    ['failed',{status:'failed',terrainStatus:'failed'}],
    ['unresolved',{status:'unresolved',terrainStatus:'unresolved'}],
    ['validation_incomplete',{status:'validation_incomplete'}],
    ['not_ready',{status:'not_ready'}],
    ['stale',{status:'stale',applicability:'stale',staleReason:'validation_dependencies_changed'}],
  ];
  for(const [status,overrides] of cases){
    const validation=validationFixture(overrides);
    const model=layeredValidationStatusModel(validation);
    assert.equal(model.status,status);
    for(const other of LAYERED_VALIDATION_DISTINCT_STATUSES){
      if(other===status)continue;
      const flags={validated_candidate:'isValidated',failed:'isFailed',unresolved:'isUnresolved',
        validation_incomplete:'isIncomplete',not_ready:'isNotReady',stale:'isStale'};
      assert.equal(model[flags[other]],false,`${status} must not report ${other}`);
    }
    assert.equal(model[({validated_candidate:'isValidated',failed:'isFailed',unresolved:'isUnresolved',
      validation_incomplete:'isIncomplete',not_ready:'isNotReady',stale:'isStale'})[status]],true);
  }
  // unresolved 与 validation_incomplete 绝不显示为 failed：状态类与文案双重确认。
  const unresolvedFlow=baseFlow({validations:[validationFixture({
    status:'unresolved',terrainStatus:'unresolved'})]});
  const unresolvedHtml=renderLayeredRouteValidation(unresolvedFlow);
  assert.match(unresolvedHtml,/flow-unresolved/);
  assert.doesNotMatch(unresolvedHtml,/flow-failed/,'unresolved must not render a failed badge');
  assert.match(unresolvedHtml,/native_terrain_pixel_nodata/);
  assert.match(unresolvedHtml,/unresolved 不是 failed/);

  const incompleteFlow=baseFlow({validations:[validationFixture({status:'validation_incomplete'})]});
  const incompleteHtml=renderLayeredRouteValidation(incompleteFlow);
  assert.match(incompleteHtml,/flow-validation_incomplete/);
  assert.doesNotMatch(incompleteHtml,/flow-failed/,'validation_incomplete must not render a failed badge');
  assert.match(incompleteHtml,/resource_limit 是计算资源上限/);
  assert.match(incompleteHtml,/safety_parameter false/);
  const incompleteModel=layeredRouteValidationModel(incompleteFlow);
  assert.equal(incompleteModel.current.status,'validation_incomplete');
  assert.equal(layeredValidationStatusModel(incompleteModel.current).isFailed,false);
  assert.equal(incompleteModel.current.resourceLimits.limit_reached,true);

  const failedFlow=baseFlow({validations:[validationFixture({status:'failed',terrainStatus:'failed'})]});
  const failedHtml=renderLayeredRouteValidation(failedFlow);
  assert.match(failedHtml,/flow-failed/);
  assert.doesNotMatch(failedHtml,/flow-unresolved/);
  assert.match(failedHtml,/terrain_clearance_below_policy/);

  const notReadyFlow=baseFlow({validations:[validationFixture({status:'not_ready',applicability:'not_ready'})]});
  const notReadyHtml=renderLayeredRouteValidation(notReadyFlow);
  assert.match(notReadyHtml,/flow-not_ready/);
  assert.match(notReadyHtml,/current_route_risk_profile_missing/);
  assert.doesNotMatch(notReadyHtml,/flow-failed/);
});

// ---- 4. stale 不冒充 current ---------------------------------------------------

test('a stale validation stays history and never becomes the current validation',()=>{
  const stale=validationFixture({status:'stale',applicability:'stale',
    staleReason:'validation_dependencies_changed'});
  const flow=baseFlow({validations:[stale]});
  const model=layeredRouteValidationModel(flow);
  assert.equal(model.current,null,'a stale validation is never the current validation');
  assert.equal(layeredValidationCurrentValidation(flow,'LRV-AAAAAAAAAAAA'),null);
  // 冻结的指纹仍然可读（历史证据），但不会因为读到指纹就变成 current
  assert.equal(model.validations[0].validationFingerprint,'layeredvalidationv1-fp-1');
  assert.equal(model.validations[0].currentApplicability,'stale');
  const html=renderLayeredRouteValidation(flow);
  assert.match(html,/LRV-AAAAAAAAAAAA/,'the stale record must stay listed');
  assert.match(html,/stale：只作为历史证据保留/);
  assert.match(html,/validation_dependencies_changed/);
  assert.match(html,/没有 current_applicability=current 的 validation/);
  assert.doesNotMatch(html,/当前有效验证（current_applicability=current） · flow-stale/,
    'the stale record must not be promoted into the current section');

  // 混合场景：stale 与 current 同时存在时，current 必须精确指向 current 的那条
  const mixed=baseFlow({validations:[
    validationFixture({validationId:'LRV-STALE000001',status:'stale',applicability:'stale',
      staleReason:'validation_dependencies_changed'}),
    validationFixture({validationId:'LRV-CURRENT00001'}),
  ]});
  const mixedModel=layeredRouteValidationModel(mixed);
  assert.equal(mixedModel.current.validationId,'LRV-CURRENT00001');
  assert.equal(mixedModel.validations.length,2,'the stale record stays in the history collection');
  assert.equal(layeredValidationFingerprint(mixed,'LRV-CURRENT00001'),'layeredvalidationv1-fp-1');

  // current_applicability 不是 current 的 validated_candidate 同样不被当作当前验证
  const staleInputs=baseFlow({validations:[validationFixture({
    applicability:'stale_inputs_changed'})]});
  assert.equal(layeredRouteValidationModel(staleInputs).current,null);
});

// ---- 5. domain / margins / intervals / source / fingerprint 转印 ----------------

test('domain status margins intervals source evidence and fingerprints are transcribed',()=>{
  const flow=baseFlow();
  const html=renderLayeredRouteValidation(flow);
  const model=layeredRouteValidationModel(flow);
  const current=model.current;
  assert.equal(current.minimumMargins.terrain_vertical_m,-5.0);
  assert.equal(layeredValidationDomainModel(current,'terrain').minimumMargin,-5.0);
  assert.equal(layeredValidationDomainModel(current,'building').minimumMargin,12.4);
  assert.equal(current.resourceLimits.safety_parameter,false);
  for(const expected of ['terrain_vertical_m','building_vertical_m','source_audits',
    'validator_versions','layeredvalidationv1-fp-1','cand-fp','path-fp','configured_real_sources',
    'Terrain（源生地形像素）','Building（真实建筑 footprint）','current_applicability']){
    assert.match(html,new RegExp(expected.replace(/[.*+?^${}()|[\]\\/]/g,'\\$&')),`${expected} must be shown`);
  }
  // failed / unresolved interval 两张表必须分别呈现，且只在对应状态下有内容
  assert.match(html,/failed_intervals/);
  assert.match(html,/unresolved_intervals（绝不显示为 failed）/);
  assert.match(html,/没有 failed interval/);

  const unresolved=baseFlow({validations:[validationFixture({
    status:'unresolved',terrainStatus:'unresolved'})]});
  const unresolvedHtml=renderLayeredRouteValidation(unresolved);
  assert.match(unresolvedHtml,/native_terrain_pixel_nodata/);
  assert.match(unresolvedHtml,/start 0\.000 m · end 100\.000 m/);
});

// ---- 6. evaluate-real 端点 -----------------------------------------------------

test('the validation panel only calls the evaluate-real endpoint and writes nothing else',async()=>{
  withStubDom(document=>{
    const flow=baseFlow();
    mountStep3(document,flow);
    document.querySelectorAll=selector=>findAll(document.body,selector);
    document.querySelector=selector=>findAll(document.body,selector)[0]||null;
    const calls=[],registered=[];
    const c={
      flow:()=>flow,$:id=>document.getElementById(id),
      panelError:message=>calls.push(['error',message]),
      // BUG-STEP03-RESOURCEACTION-001：局部 mutation 必须走 resourceMutationAndRefresh，
      // 不允许再用 resourceAction 把局部 response 当成完整 workflow。
      resourceMutationAndRefresh:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
      resourceAction:(path,payload)=>{calls.push(['unexpected-resourceAction',path,payload]);return Promise.resolve({});},
      actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;},
    };
    bindStep3(c);
    for(const id of ['evaluateLayeredRouteValidation','previewLayeredAdoption','applyLayeredAdoption',
      'revokeLayeredAdoption']){
      assert.ok(registered.includes(id),`bind() must register ${id}`);
      assert.equal(typeof document.getElementById(id).onclick,'function',`#${id} keeps its handler`);
    }
    return document.getElementById('evaluateLayeredRouteValidation').onclick().then(()=>{
      assert.deepEqual(calls.pop(),['/api/layered-route-validations/evaluate-real',
        {horizontal_crs:'EPSG:32651'}],
      'evaluate-real 必须走 mutation+refresh，并显式提交 horizontal_crs');
    });
  });
  // 源码层契约：面板只使用既有的四个端点，不做 replan、不碰地图、不删路由。
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_route_validation.js',import.meta.url),'utf8');
  assert.match(source,/\/api\/layered-route-validations\/evaluate-real/);
  assert.match(source,/resourceMutationAndRefresh/,'局部 response 不得直接覆盖全局 flow');
  assert.match(source,/horizontal_crs/,'payload 必须显式携带 horizontal_crs');
  assert.doesNotMatch(source,/c\.mutate\('|setLayer\(|setZoom|fitLonLatBbox|replan\(/);
  assert.doesNotMatch(source,/operational_routes\s*=/,'validation must never write operational_routes');
  assert.doesNotMatch(source,/import .*from '(?!\.\/common\.js)/,'no third-party dependency may be added');
});

test('the validation panel renders an explicit metric CRS input and refuses to run when it is empty',async()=>{
  const flow=baseFlow();
  const html=renderLayeredRouteValidation(flow);
  assert.match(html,/id="layeredValidationHorizontalCrs"/,'必须提供显式 horizontal_crs 输入框');
  assert.match(html,/EPSG:32651/,'当前舟山工程建议值必须可见/可预填');
  await new Promise(resolve=>{
    withStubDom(document=>{
      mountStep3(document,flow);
      document.querySelectorAll=selector=>findAll(document.body,selector);
      document.querySelector=selector=>findAll(document.body,selector)[0]||null;
      const calls=[];
      const c={
        flow:()=>flow,$:id=>document.getElementById(id),
        panelError:message=>calls.push(['error',message]),
        resourceMutationAndRefresh:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
        resourceAction:(path,payload)=>{calls.push(['unexpected-resourceAction',path,payload]);return Promise.resolve({});},
        // 与 main.js 的 actionButton 同构：handler 抛错时落到 panelError。
        actionButton:(id,handler)=>{
          const node=document.getElementById(id);
          if(node)node.onclick=async()=>{
            try{await handler();}catch(error){calls.push(['error',error.message]);}
          };
        },
      };
      bindStep3(c);
      const field=document.getElementById('layeredValidationHorizontalCrs');
      assert.ok(field,'the CRS input must exist');
      field.value='   ';
      return document.getElementById('evaluateLayeredRouteValidation').onclick().then(()=>{
        assert.equal(calls.some(item=>item[0]==='/api/layered-route-validations/evaluate-real'),false,
          '空 horizontal_crs 时不得发起任何请求');
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),
          /horizontal_crs/,'必须给出清楚的空值提示');
        resolve();
      });
    });
  });
});

// ---- 7. Preview 无 Apply 副作用 ------------------------------------------------

test('preview is cached module-locally with side_effects=false and never writes state',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  const preview={
    status:'ready',side_effects:false,publication_allowed:true,
    preview_fingerprint:'layeredpreviewv1-preview',
    validation_id:'LRV-AAAAAAAAAAAA',
    validation_fingerprint:'layeredvalidationv1-fp-1',
    projection:{
      status:'ready',validation_id:'LRV-AAAAAAAAAAAA',route_id:'R-1',
      projection_fingerprint:'layeredprojectionv1-proj',
      validation_fingerprint:'layeredvalidationv1-fp-1',
      route:{route_id:'R-1',status:'passed',kind:'layered_risk_aware_operational_route',
        path_crs:'OGC:CRS84',path:[[122.0,30.0],[122.001,30.0]]},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW',
        operating_mode:'fixed_cruise_layer',vertical_reference:'egm2008_orthometric',
        confirmed:true,active:true,adoption_owned:true},
      conflict:null,
      two_dimensional_path_only:true,
      altitude_representation:{carried_by:'RouteOperatingLayer -> AltitudeLayer',
        vertical_reference:'egm2008_orthometric',in_crs84_third_coordinate:false,
        route_altitude_profile_created:false},
      replacement_requested:false,apply_blocked_by_conflict:false,
    },
  };
  const before=JSON.stringify(flow);
  cacheLayeredAdoptionPreview(preview,'LRV-AAAAAAAAAAAA');
  assert.equal(JSON.stringify(flow),before,'Preview caching must not mutate the flow snapshot');
  const state=layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA');
  assert.equal(state.present,true);
  assert.equal(state.expired,false);
  assert.equal(state.publicationAllowed,true);
  assert.equal(state.validationFingerprint,'layeredvalidationv1-fp-1');

  const html=renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA');
  assert.match(html,/side_effects[\s\S]{0,80}?false/);
  assert.match(html,/Preview（不写 state）/);
  assert.match(html,/publication_allowed/);
  assert.match(html,/RouteOperatingLayer/);
  assert.match(html,/projection_fingerprint/);
  assert.match(html,/layeredprojectionv1-proj/);
  assert.match(html,/layeredvalidationv1-fp-1/);
  assert.match(html,/2D route path 只保存二维/);
  assert.match(html,/不创建 RouteAltitudeProfile/);
  assert.match(html,/Departure\/Arrival procedure/);
  // Preview 请求体只提交 validation_id 与默认 false 的 replace_existing
  assert.deepEqual(layeredAdoptionPreviewPayload('LRV-AAAAAAAAAAAA'),
    {validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false});
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_operational_adoption.js',import.meta.url),'utf8');
  assert.match(source,/side_effects/);
  assert.doesNotMatch(source,/c\.mutate\('|setLayer\(|setZoom|fitLonLatBbox|layerIds/,
    'the panel must not replan or touch map layers');
  assert.doesNotMatch(source,/departure-arrival-procedure|route-profile'/,
    'the panel must never create a profile or a departure/arrival procedure');
  clearLayeredAdoptionCache();
});

// ---- 8. conflict 默认 replace_existing=false / confirmed 默认 false ------------

test('conflict forbids apply by default and both checkboxes default to false',()=>{
  withStubDom(document=>{
    const flow=baseFlow();
    const {root}=mountStep3(document,flow);
    const applyConfirmed=document.getElementById('layeredAdoptionApplyConfirmed');
    const revokeConfirmed=document.getElementById('layeredAdoptionRevokeConfirmed');
    assert.ok(applyConfirmed&&revokeConfirmed,'both explicit confirmation checkboxes must exist');
    assert.equal(applyConfirmed.checked,false,'Apply confirmation defaults to false');
    assert.equal(revokeConfirmed.checked,false,'Revoke confirmation defaults to false');
    // 冲突未出现时，replace_existing 控件本身不渲染（默认即 false）
    assert.equal(document.getElementById('layeredAdoptionReplaceExisting'),null,
      'replace_existing must only appear when an actual conflict exists');
    assert.match(textOf(findByDataset(root,'segName','op-operational')),/route 冲突（默认 replace_existing=false）/);
    assert.match(textOf(findByDataset(root,'segName','op-operational')),/不自动覆盖/);
  });

  clearLayeredAdoptionCache();
  // 冲突场景：publication_allowed=false 且默认 replace_existing=false → 禁止 Apply
  const conflictFlow=baseFlow();
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:false,validation_id:'LRV-AAAAAAAAAAAA',
    projection:{status:'ready',route_id:'R-1',conflict:{route_id:'R-1',existing_status:'passed',
      existing_source_type:'manual_or_other_planner',
      replacement_requires_explicit_confirmation:true},
    route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
    route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}},
  },'LRV-AAAAAAAAAAAA');
  const blocked=layeredAdoptionApplyGuard(conflictFlow,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(blocked.allowed,false);
  assert.match(blocked.reason,/publication_allowed/);
  assert.equal(blocked.payload,null);
  const withReplace=layeredAdoptionApplyGuard(conflictFlow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(withReplace.allowed,false,'even replace_existing cannot bypass a blocked publication');
  assert.equal(withReplace.payload,null);

  // 冲突但 publication_allowed=true：这份 Preview 冻结的是 replace_existing=false，
  // 因此默认禁止 Apply；此时把 checkbox 改成 true 属于"发布意图已变化"，
  // 必须重新 Preview —— 绝不允许用现场 checkbox 覆盖冻结意图。
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:true,validation_id:'LRV-AAAAAAAAAAAA',
    projection:{status:'ready',route_id:'R-1',conflict:{route_id:'R-1',existing_status:'passed'},
      route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}},
  },'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  const defaultReplace=layeredAdoptionApplyGuard(conflictFlow,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(defaultReplace.allowed,false);
  assert.match(defaultReplace.reason,/replace_existing 默认 false/);
  const explicitReplace=layeredAdoptionApplyGuard(conflictFlow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(explicitReplace.allowed,false,
    'changing replace_existing must expire the frozen preview instead of allowing apply');
  assert.equal(explicitReplace.payload,null,'the checkbox must never overwrite the frozen intent');
  assert.match(explicitReplace.reason,new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
  // 只有重新 Preview（replace_existing=true）之后才允许 Apply，且提交的是这份冻结值
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:true,validation_id:'LRV-AAAAAAAAAAAA',
    projection:{status:'ready',route_id:'R-1',replacement_requested:true,
      conflict:{route_id:'R-1',existing_status:'passed'},
      route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}},
  },'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  const rePreviewed=layeredAdoptionApplyGuard(conflictFlow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(rePreviewed.allowed,true,'a re-preview freezing replace=true is the only way through');
  assert.equal(rePreviewed.payload.replace_existing,true);
  // 渲染层：冲突时 replace_existing 复选框出现，并按 Preview 冻结值回填（冻结 true → 勾选）
  const conflictHtml=renderLayeredOperationalAdoption(conflictFlow,'LRV-AAAAAAAAAAAA');
  assert.match(conflictHtml,/id="layeredAdoptionReplaceExisting" checked/);
  assert.match(conflictHtml,/Preview 冻结的 replace_existing true/);
  // 冻结 false 的冲突 Preview：复选框必须是不勾选的默认态
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:false,validation_id:'LRV-AAAAAAAAAAAA',
    projection:{status:'ready',route_id:'R-1',conflict:{route_id:'R-1',existing_status:'passed'},
      route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}},
  },'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  const defaultHtml=renderLayeredOperationalAdoption(conflictFlow,'LRV-AAAAAAAAAAAA');
  assert.match(defaultHtml,/id="layeredAdoptionReplaceExisting"/);
  assert.doesNotMatch(defaultHtml,/id="layeredAdoptionReplaceExisting" checked/);
  assert.match(defaultHtml,/默认 false/);
  clearLayeredAdoptionCache();
});

// ---- 9. Apply 必须依赖 Preview + 携带 Preview 的指纹 ---------------------------

test('apply requires a current preview and submits the preview fingerprint verbatim',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  // 1) 没有 Preview：一律拒绝
  const noPreview=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(noPreview.allowed,false);
  assert.match(noPreview.reason,/Apply 必须先 Preview/);
  assert.equal(noPreview.payload,null);

  // 2) 有 Preview 但没勾选确认：拒绝
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:true,validation_id:'LRV-AAAAAAAAAAAA',
    validation_fingerprint:'layeredvalidationv1-fp-1',
    projection:{status:'ready',route_id:'R-1',conflict:null,
      route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'},
      validation_fingerprint:'layeredvalidationv1-fp-1'},
  },'LRV-AAAAAAAAAAAA');
  const unconfirmed=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',{confirmed:false});
  assert.equal(unconfirmed.allowed,false);
  assert.match(unconfirmed.reason,/显式勾选确认/);

  // 3) 显式确认 + Preview 就绪：放行，且 expected 指纹只能来自 Preview
  const allowed=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(allowed.allowed,true);
  assert.deepEqual(allowed.payload,{
    validation_id:'LRV-AAAAAAAAAAAA',confirmed:true,replace_existing:false,
    expected_validation_fingerprint:'layeredvalidationv1-fp-1',
  });
  // Preview 冻结的指纹与当前 flow 一致，因此不能被"偷偷换成新 flow 值"
  assert.equal(allowed.payload.expected_validation_fingerprint,
    layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA').validationFingerprint);
  clearLayeredAdoptionCache();
});

test('a fingerprint change after preview blocks apply and demands a new preview',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  cacheLayeredAdoptionPreview({
    status:'ready',side_effects:false,publication_allowed:true,validation_id:'LRV-AAAAAAAAAAAA',
    validation_fingerprint:'layeredvalidationv1-fp-OLD',
    projection:{status:'ready',route_id:'R-1',conflict:null,validation_fingerprint:'layeredvalidationv1-fp-OLD',
      route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
      route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}},
  },'LRV-AAAAAAAAAAAA');
  // 证据变化：同一条 validation 的 fingerprint 变了（真实候选被重算）
  const changed=baseFlow({validations:[validationFixture({
    validationFingerprint:'layeredvalidationv1-fp-NEW'})]});
  const state=layeredAdoptionPreviewState(changed,'LRV-AAAAAAAAAAAA');
  assert.equal(state.expired,true);
  assert.equal(state.evidenceChanged,true);
  const guard=layeredAdoptionApplyGuard(changed,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(guard.allowed,false);
  assert.equal(guard.payload,null,'a changed fingerprint must never be silently substituted');
  assert.match(guard.reason,new RegExp(LAYERED_ADOPTION_EVIDENCE_CHANGED));
  // 渲染层必须把这个结论显式写出来
  const html=renderLayeredOperationalAdoption(changed,'LRV-AAAAAAAAAAAA');
  assert.match(html,new RegExp(LAYERED_ADOPTION_EVIDENCE_CHANGED));
  assert.match(html,/前端不会自动重试/);
  // 用户改选另一条 validation 也必须重新 Preview
  const other=baseFlow({validations:[validationFixture(),validationFixture({
    validationId:'LRV-BBBBBBBBBBBB',validationFingerprint:'layeredvalidationv1-fp-2'})]});
  const switched=layeredAdoptionApplyGuard(other,'LRV-BBBBBBBBBBBB',{confirmed:true});
  assert.equal(switched.allowed,false,'switching the selection invalidates the preview');
  assert.match(switched.reason,new RegExp(LAYERED_ADOPTION_EVIDENCE_CHANGED));
  clearLayeredAdoptionCache();
});

// ---- 10. Apply 结果只转印四个字段 ---------------------------------------------

test('apply result transcribes only the four contracted fields',()=>{
  clearLayeredAdoptionCache();
  assert.equal(layeredAdoptionLastApply(),null);
  const recorded=recordLayeredAdoptionApply({
    status:'passed',adoption_id:'LRA-222222222222',route_id:'R-1',
    route_operating_layer_created:true,route_altitude_profile_created:false,
    departure_arrival_procedure_created:false,
    snapshot:{layered_operational_adoptions:{count:1,items:[]}},
  });
  assert.deepEqual(recorded,{
    adoptionId:'LRA-222222222222',routeId:'R-1',routeOperatingLayerCreated:true,
    routeAltitudeProfileCreated:false,departureArrivalProcedureCreated:false,
  });
  const html=renderLayeredOperationalAdoption(baseFlow(),'LRV-AAAAAAAAAAAA');
  for(const expected of ['LRA-222222222222','route_operating_layer_created',
    'route_altitude_profile_created','departure_arrival_procedure_created']){
    assert.match(html,new RegExp(expected),`${expected} must be transcribed`);
  }
  clearLayeredAdoptionCache();
});

// ---- 11. Revoke 必须显式 confirmed --------------------------------------------

test('revoke requires explicit confirmation and never deletes the route',()=>{
  const adoption=adoptionFixture();
  const flow=baseFlow({adoptions:{schema_version:'layered-operational-adoption-collection-v1',
    status:'passed',count:1,items:[adoption]}});
  // 未确认 → 拒绝；显式确认 → 只提交 adoption_id + confirmed=true
  assert.deepEqual(layeredAdoptionRevokePayload('LRA-111111111111'),{
    adoption_id:'LRA-111111111111',confirmed:false});
  const blocked=layeredAdoptionRevokeGuard('LRA-111111111111',{confirmed:false});
  assert.equal(blocked.allowed,false);
  assert.match(blocked.reason,/显式确认/);
  assert.equal(blocked.payload,null);
  const allowed=layeredAdoptionRevokeGuard('LRA-111111111111',{confirmed:true});
  assert.deepEqual(allowed.payload,{adoption_id:'LRA-111111111111',confirmed:true});
  assert.equal(layeredAdoptionRevokeGuard('',{confirmed:true}).allowed,false);

  const html=renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA');
  // 中文引号在 HTML 里被转义为 &quot;：只匹配不被转义的前缀即可证明该文案存在。
  assert.match(html,/撤销本次发布（revoke）不是/);
  assert.ok(LAYERED_ADOPTION_REVOKE_LABEL.includes('不是"删除航路"'),'标签语义保持"撤销发布"而非"删除航路"');
  assert.match(html,/前端绝不直接删除 operational route/);
  assert.match(html,/published \/ stale \/ revoked \/ superseded 全部保留/);
  assert.match(html,/ownership route_owned true/);
  // 历史里 stale / revoked 记录仍然完整保留
  const history=baseFlow({adoptions:{schema_version:'layered-operational-adoption-collection-v1',
    status:'stale',count:2,items:[
      adoptionFixture({adoptionId:'LRA-OLD000000001',status:'stale',applicability:'superseded'}),
      adoptionFixture({adoptionId:'LRA-REVOKED0001',status:'revoked',applicability:'revoked'}),
    ]}});
  const historyHtml=renderLayeredOperationalAdoption(history,'LRV-AAAAAAAAAAAA');
  for(const expected of ['LRA-OLD000000001','LRA-REVOKED0001','flow-stale','flow-revoked','superseded']){
    assert.match(historyHtml,new RegExp(expected),`${expected} must stay in history`);
  }
  // 源码层：本模块绝不直接删除 operational route，也不调用任何 delete 端点
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_operational_adoption.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/route-delete|\/delete|operational_routes\s*=/);
});

// ---- 12. 状态链只转印、不推断 safe --------------------------------------------

test('the candidate to operational chain only transcribes backend state',()=>{
  const flow=baseFlow();
  const chain=layeredAdoptionChainModel(flow);
  assert.deepEqual(chain.stages.map(item=>item.stage),LAYERED_ADOPTION_CHAIN_STAGES);
  assert.equal(chain.neverInfersSafe,true);
  assert.equal(chain.candidateState,'candidate');
  assert.equal(chain.validationState,'validated_candidate');
  assert.equal(chain.operationalState,'not_published','no adoption means not_published');
  assert.equal(chain.publishedCount,0);
  const html=renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA');
  assert.match(html,/状态链 Candidate → RRP → Validation → Operational/);
  assert.match(html,/只转印后端状态，前端不推断 safe/);
  assert.match(html,/RRP 必须是 current，但 classification 不是 hard gate/);

  // 已发布：状态链必须转录 published，而不是由前端推断"安全"
  const published=baseFlow({adoptions:{schema_version:'layered-operational-adoption-collection-v1',
    status:'passed',count:1,items:[adoptionFixture()]}});
  const publishedChain=layeredAdoptionChainModel(published);
  assert.equal(publishedChain.operationalState,'published');
  assert.equal(publishedChain.publishedCount,1);
  const publishedHtml=renderLayeredOperationalAdoption(published,'LRV-AAAAAAAAAAAA');
  assert.match(publishedHtml,/flow-published/);
  assert.match(publishedHtml,/Select \/ Validate \/ Preview \/ Apply 互不等价/);
  assert.match(publishedHtml,/只有 Apply 才写 operational_routes/);
  // stale validation 不得被说成当前验证
  const staleValidation=baseFlow({validations:[validationFixture({
    status:'stale',applicability:'stale',staleReason:'validation_dependencies_changed'})]});
  const staleChain=layeredAdoptionChainModel(staleValidation);
  assert.equal(staleChain.validationState,'not_evaluated');
  assert.equal(staleChain.staleValidationCount,1);
  assert.equal(layeredOperationalAdoptionModel(staleValidation).options[0].eligible,false);
});

// ---- 13. Legacy operationalRoutes 仍存在且契约不变 -----------------------------

test('the legacy scenarioRoutes and operationalRoutes entries keep their contract',()=>{
  const flow=baseFlow();
  const html=renderStep3({flow,interactionMode:'pan',selectedReference:null});
  assert.match(html,/id="scenarioRoutes"/);
  assert.match(html,/id="operationalRoutes"/);
  assert.match(html,/生成场景航路（all-pairs，兼容）/);
  assert.match(html,/生成运行航路/);
  assert.match(html,/A\. 现有 \/ Legacy 运行航路生成（保持不变）/);
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step03_routes.js',import.meta.url),'utf8');
  // 旧端点与逻辑一字不改
  assert.match(source,/c\.actionButton\('scenarioRoutes',\(\)=>c\.mutate\('scenario',\{\}\)\)/);
  assert.match(source,/c\.actionButton\('operationalRoutes',\(\)=>c\.mutate\('operational'\)\)/);
  assert.match(source,/api\/workflow\/operational/);
  // 新模块不得触碰 legacy 生成入口
  const validationSource=readFileSync(new URL('../cns_planner/web/js/workflow/layered_route_validation.js',import.meta.url),'utf8');
  const adoptionSource=readFileSync(new URL('../cns_planner/web/js/workflow/layered_operational_adoption.js',import.meta.url),'utf8');
  for(const source_ of [validationSource,adoptionSource]){
    assert.doesNotMatch(source_,/mutate\('scenario'|mutate\('operational'|generate_operational/);
  }
  // 仍然通过真实 bind 注册两个 legacy 入口
  withStubDom(document=>{
    const mounted=baseFlow();
    mountStep3(document,mounted);
    document.querySelectorAll=selector=>findAll(document.body,selector);
    const calls=[],registered=[];
    const c={
      flow:()=>mounted,$:id=>document.getElementById(id),
      panelError:()=>{},mutate:(action,payload)=>{calls.push([action,payload]);return Promise.resolve({});},
      resourceAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
      resourceMutationAndRefresh:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
      computeAction:(path,payload)=>{calls.push([path,payload]);return Promise.resolve({});},
      actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;},
    };
    bindStep3(c);
    assert.ok(registered.includes('scenarioRoutes'));
    assert.ok(registered.includes('operationalRoutes'));
    document.getElementById('scenarioRoutes').onclick();
    assert.deepEqual(calls.pop(),['scenario',{}]);
    document.getElementById('operationalRoutes').onclick();
    assert.deepEqual(calls.pop(),['operational',undefined]);
  });
});

// ---- 14. Preview / Apply / Revoke 端点与守卫在真实 bind 上生效 ------------------

test('bind wires preview apply and revoke with the contracted payloads and order',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      const flow=baseFlow();
      mountStep3(document,flow);
      document.querySelectorAll=selector=>findAll(document.body,selector);
      document.querySelector=selector=>findAll(document.body,selector)[0]||null;
      const calls=[];
      let currentFlow=flow;
      const localMutation=(path,payload)=>{
        calls.push([path,payload]);
        if(path.endsWith('/preview')){
          return Promise.resolve({status:'ready',side_effects:false,publication_allowed:true,
            validation_id:payload.validation_id,validation_fingerprint:'layeredvalidationv1-fp-1',
            projection:{status:'ready',route_id:'R-1',conflict:null,
              projection_fingerprint:'layeredprojectionv1-proj',
              route:{route_id:'R-1',path:[[122,30],[122.001,30]],path_crs:'OGC:CRS84'},
              route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW'}}});
        }
        if(path.endsWith('/apply')){
          return Promise.resolve({status:'passed',adoption_id:'LRA-222222222222',route_id:'R-1',
            route_operating_layer_created:true,route_altitude_profile_created:false,
            departure_arrival_procedure_created:false});
        }
        return Promise.resolve({status:'passed'});
      };
      const c={
        flow:()=>currentFlow,$:id=>document.getElementById(id),
        panelError:message=>calls.push(['error',message]),
        // Preview 只读（side_effects=false）→ computeAction；apply/revoke 是局部 mutation
        // → resourceMutationAndRefresh。两者都不得把局部 response 当成完整 workflow。
        computeAction:localMutation,
        resourceMutationAndRefresh:localMutation,
        resourceAction:(path,payload)=>{
          calls.push(['unexpected-resourceAction',path,payload]);
          return localMutation(path,payload);
        },
        // 与 main.js 的 actionButton 同构：handler 抛错时落到 panelError，不向外冒泡。
        actionButton:(id,handler)=>{
          const node=document.getElementById(id);
          if(node)node.onclick=async()=>{
            try{await handler();}catch(error){calls.push(['error',error.message]);}
          };
        },
      };
      bindStep3(c);
      // 默认渲染出的 eligible option 必须已经选中（Select ≠ Validate ≠ Apply）
      const radios=findAll(document.body,'input[name="layeredAdoptionValidation"]');
      assert.equal(radios.length,1);
      assert.equal(radios[0].checked,true,'the first eligible validation is selected by default');
      assert.equal(radios[0].value,'LRV-AAAAAAAAAAAA');
      // Apply 未 Preview → 报错且不调用任何端点
      return document.getElementById('applyLayeredAdoption').onclick().then(()=>{
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),/Apply 必须先 Preview/);
        assert.equal(calls.some(item=>String(item[0]).endsWith('/apply')),false);
        // Preview → 只读端点，请求体不含 confirmed
        return document.getElementById('previewLayeredAdoption').onclick();
      }).then(()=>{
        const previewCall=calls.filter(item=>item[0]==='/api/layered-operational-adoptions/preview').pop();
        assert.deepEqual(previewCall[1],{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false});
        assert.equal('confirmed' in previewCall[1],false,'preview must not carry Apply-side fields');
        // Apply：未勾选确认 → 拒绝
        return document.getElementById('applyLayeredAdoption').onclick();
      }).then(()=>{
        assert.equal(calls.some(item=>String(item[0]).endsWith('/apply')),false,
          'apply must not run without the explicit confirmation checkbox');
        // 勾选确认 → Apply 携带 Preview 的指纹
        document.getElementById('layeredAdoptionApplyConfirmed').checked=true;
        return document.getElementById('applyLayeredAdoption').onclick();
      }).then(()=>{
        const applyCall=calls.filter(item=>String(item[0]).endsWith('/apply')).pop();
        assert.deepEqual(applyCall[1],{
          validation_id:'LRV-AAAAAAAAAAAA',confirmed:true,replace_existing:false,
          expected_validation_fingerprint:'layeredvalidationv1-fp-1',
        });
        // Apply 后 Preview 立即作废：不能二次 Apply
        return document.getElementById('applyLayeredAdoption').onclick().then(()=>{
          const applyCalls=calls.filter(item=>String(item[0]).endsWith('/apply'));
          assert.equal(applyCalls.length,1,'a consumed preview must not allow a second apply');
          // Revoke 未确认 → 拒绝
          return document.getElementById('revokeLayeredAdoption').onclick();
        });
      }).then(()=>{
        assert.equal(calls.some(item=>String(item[0]).endsWith('/revoke')),false);
        // Revoke 需要历史 adoption：没有 adoption 时也必须拒绝而不是静默成功
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),/没有可撤销的 adoption/);
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

// ---- 15. 业务边界文案 ---------------------------------------------------------

test('the panels state the operational boundaries they must not cross',()=>{
  const validationHtml=renderLayeredRouteValidation(baseFlow());
  assert.match(validationHtml,/不 replan/);
  assert.match(validationHtml,/不修改 candidate/);
  assert.match(validationHtml,/不写入 operational_routes/);
  assert.match(validationHtml,/Airspace 在本验证里是 display_only/);
  assert.match(validationHtml,/高度完全由 AltitudeLayer/);
  assert.match(validationHtml,/不构成适航或法规符合性结论/);
  const adoptionHtml=renderLayeredOperationalAdoption(baseFlow(),'LRV-AAAAAAAAAAAA');
  assert.match(adoptionHtml,/Select \/ Validate \/ Preview \/ Apply 互不等价/);
  assert.match(adoptionHtml,/Preview 不写 state/);
  assert.match(adoptionHtml,/不携带高度第三坐标/);
  assert.match(adoptionHtml,/高度由 RouteOperatingLayer → AltitudeLayer 承载/);
  assert.match(adoptionHtml,/绝不自动覆盖/);  assert.match(adoptionHtml,/expected_validation_fingerprint/);
  // 不做 CNS 评估 / 不合并 V3 语义
  for(const source_ of [validationHtml,adoptionHtml]){
    assert.doesNotMatch(source_,/SORA|cns_assessed true|CNS 合规结论/);
  }
  // 前端不实现 RRP 阈值判定或地图交互
  const validationSource=readFileSync(new URL('../cns_planner/web/js/workflow/layered_route_validation.js',import.meta.url),'utf8');
  const adoptionSource=readFileSync(new URL('../cns_planner/web/js/workflow/layered_operational_adoption.js',import.meta.url),'utf8');
  for(const source_ of [validationSource,adoptionSource]){
    assert.doesNotMatch(source_,/high_min|medium_min/,'the frontend must not re-derive RRP classification');
    assert.doesNotMatch(source_,/drawLine|\.arc\(|canvas|highlight/);
  }
  // 实现不得塞进 layered_theta_v2.js
  const theta=readFileSync(new URL('../cns_planner/web/js/workflow/layered_theta_v2.js',import.meta.url),'utf8');
  assert.doesNotMatch(theta,/layered-route-validations|layered-operational-adoptions/);
});

// ---- 16. conflict re-preview：Preview 冻结 replace_existing，Apply 只提交冻结值 ---------

/** 与后端 LayeredOperationalAdoptionService.preview 同形的假响应（publication_allowed 由 conflict 推导）。 */
function fakePreviewResponse(flow,payload,{conflict=true}={}){
  const replaceExisting=payload.replace_existing===true;
  const blocked=Boolean(conflict&&!replaceExisting);
  const projection={
    status:'ready',validation_id:payload.validation_id,route_id:'R-1',
    projection_fingerprint:'layeredprojectionv1-proj',
    validation_fingerprint:'layeredvalidationv1-fp-1',
    route:{route_id:'R-1',status:'passed',kind:'layered_risk_aware_operational_route',
      path_crs:'OGC:CRS84',path:[[122,30],[122.001,30]]},
    route_operating_layer:{route_id:'R-1',altitude_layer_id:'L8-LOW',
      operating_mode:'fixed_cruise_layer',vertical_reference:'egm2008_orthometric',
      confirmed:true,adoption_owned:true},
    conflict:conflict?{route_id:'R-1',existing_status:'passed',
      existing_source_type:'manual_or_other_planner',
      replacement_requires_explicit_confirmation:true}:null,
    replacement_requested:replaceExisting,
    apply_blocked_by_conflict:blocked,
    two_dimensional_path_only:true,
    altitude_representation:{carried_by:'RouteOperatingLayer -> AltitudeLayer',
      vertical_reference:'egm2008_orthometric',in_crs84_third_coordinate:false,
      route_altitude_profile_created:false},
  };
  return {status:'ready',projection,side_effects:false,
    publication_allowed:projection.status==='ready'&&!blocked,
    preview_fingerprint:'layeredpreviewv1-'+(replaceExisting?'replacement':'default')};
}

/**
 * 只挂载 adoption 面板而非整个 Step03：重新渲染时替换 host 的 children，
 * 并把被卸载节点从 id 索引里撤销，避免旧控件继续被 getElementById 命中。
 */
function mountAdoptionPanel(document,flow,selected='LRV-AAAAAAAAAAAA'){
  let host=document.getElementById('layeredAdoptionHost');
  if(!host){
    host=new StubNode('div');host.id='layeredAdoptionHost';
    document.body.append(host);document.__nodes.set(host.id,host);
  }
  forEachDescendant(host,node=>{
    if(node.id&&document.__nodes.get(node.id)===node)document.__nodes.delete(node.id);
  });
  host.replaceChildren(...parseHtml(renderLayeredOperationalAdoption(flow,selected)).children);
  registerTree(document.__nodes,host);
  return host;
}

/** adoption 面板的 controller 桩：actionButton 与 main.js 同构（handler 抛错落到 panelError）。 */
function adoptionController(document,flow,{resourceAction=null}={}){
  const calls=[];
  const localMutation=(path,payload)=>{
    calls.push([path,payload]);
    return Promise.resolve(resourceAction?resourceAction(path,payload):{status:'passed'});
  };
  const c={
    flow:()=>flow,$:id=>document.getElementById(id),
    panelError:message=>calls.push(['error',message]),
    // Preview → computeAction（只读）；Apply / Revoke → resourceMutationAndRefresh（局部 mutation）。
    computeAction:localMutation,
    resourceMutationAndRefresh:localMutation,
    resourceAction:(path,payload)=>{
      calls.push(['unexpected-resourceAction',path,payload]);
      return localMutation(path,payload);
    },
    actionButton:(id,handler)=>{
      const node=document.getElementById(id);
      if(node)node.onclick=async()=>{
        try{await handler();}catch(error){calls.push(['error',error.message]);}
      };
    },
  };
  return {c,calls};
}

function stubDocumentQueries(document){
  document.querySelectorAll=selector=>findAll(document.body,selector);
  document.querySelector=selector=>findAll(document.body,selector)[0]||null;
}

test('the first preview always submits replace_existing=false and freezes the conflict preview',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow();
      mountAdoptionPanel(document,flow);
      const {c,calls}=adoptionController(document,flow,
        {resourceAction:(path,payload)=>fakePreviewResponse(flow,payload)});
      bindLayeredOperationalAdoption(c);
      return document.getElementById('previewLayeredAdoption').onclick().then(()=>{
        const previewCall=calls.filter(item=>item[0]==='/api/layered-operational-adoptions/preview').pop();
        assert.deepEqual(previewCall[1],{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false},
          'the first preview must submit replace_existing=false');
        const state=layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA');
        assert.equal(state.present,true);
        assert.equal(state.replaceExisting,false,'the preview freezes replace_existing=false');
        assert.equal(state.publicationAllowed,false,'a discovered conflict is not publishable yet');
        assert.notEqual(state.conflict,null,'the conflict is part of the frozen preview');
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('a discovered conflict renders the replace checkbox unchecked and keeps apply disabled',()=>{
  clearLayeredAdoptionCache();
  withStubDom(document=>{
    stubDocumentQueries(document);
    const flow=baseFlow();
    mountAdoptionPanel(document,flow);
    // Preview 之前没有已知冲突：replace 控件不渲染，且没有 Preview 时 Apply 一律 disabled
    assert.equal(document.getElementById('layeredAdoptionReplaceExisting'),null,
      'replace_existing only appears once a preview reports a conflict');
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,'no preview → apply disabled');
    // Preview #1（replace=false）发现 conflict → 旧的 UI 不再成立，重新渲染后复选框出现但默认不勾选
    cacheLayeredAdoptionPreview(
      fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
      'LRV-AAAAAAAAAAAA',{replaceExisting:false});
    mountAdoptionPanel(document,flow);
    const replaceNode=document.getElementById('layeredAdoptionReplaceExisting');
    assert.ok(replaceNode,'a discovered conflict must render the replace checkbox');
    assert.equal(replaceNode.checked,false,'the checkbox defaults to unchecked');
    assert.match(textOf(replaceNode.closest('label')),/默认 false/);
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
      'publication_allowed=false → apply disabled');
    assert.match(textOf(document.getElementById('layeredAdoptionIntentChanged')),/^$/,
      'no intent change has happened yet');
  });
  clearLayeredAdoptionCache();
});

test('changing replace_existing expires the frozen preview and blocks apply',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow();
      cacheLayeredAdoptionPreview(
        fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
        'LRV-AAAAAAAAAAAA',{replaceExisting:false});
      mountAdoptionPanel(document,flow);
      const {c,calls}=adoptionController(document,flow,{});
      bindLayeredOperationalAdoption(c);
      // 用户勾选 replace：旧 Preview 立即视为 expired / intent_changed
      const replaceNode=document.getElementById('layeredAdoptionReplaceExisting');
      replaceNode.checked=true;
      replaceNode.onchange();
      const state=layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA',{replaceExisting:true});
      assert.equal(state.intentChanged,true);
      assert.equal(state.expired,true,'a replace_existing change expires the frozen preview');
      assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
        'an expired preview disables apply');
      assert.match(textOf(document.getElementById('layeredAdoptionIntentChanged')),
        new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
      // 显式确认也不能绕过：Apply 被 guard 拒绝，且不发任何端点请求
      document.getElementById('layeredAdoptionApplyConfirmed').checked=true;
      document.getElementById('layeredAdoptionApplyConfirmed').onchange();
      assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
        'confirmation cannot revive an expired preview');
      return document.getElementById('applyLayeredAdoption').onclick().then(()=>{
        assert.equal(calls.some(item=>String(item[0]).endsWith('/apply')),false);
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),
          new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('the second preview reads the checkbox and submits replace_existing=true',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow();
      mountAdoptionPanel(document,flow);
      const {c,calls}=adoptionController(document,flow,
        {resourceAction:(path,payload)=>fakePreviewResponse(flow,payload)});
      bindLayeredOperationalAdoption(c);
      // 第一次 Preview：没有已知冲突 → 提交 replace_existing=false，后端回报 conflict
      return document.getElementById('previewLayeredAdoption').onclick().then(()=>{
        assert.deepEqual(calls.filter(item=>String(item[0]).endsWith('/preview'))[0][1],
          {validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false});
        assert.equal(layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA').publicationAllowed,false);
        // 后端响应触发的重新渲染：conflict 进入 UI，复选框出现但默认不勾选
        mountAdoptionPanel(document,flow);
        bindLayeredOperationalAdoption(c);
        const replaceNode=document.getElementById('layeredAdoptionReplaceExisting');
        assert.ok(replaceNode,'the conflict must produce a replace checkbox');
        assert.equal(replaceNode.checked,false,'the replace checkbox defaults to unchecked');
        // 用户勾选并重新 Preview：这一次必须提交 replace_existing=true
        replaceNode.checked=true;
        replaceNode.onchange();
        return document.getElementById('previewLayeredAdoption').onclick();
      }).then(()=>{
        const previews=calls.filter(item=>item[0]==='/api/layered-operational-adoptions/preview');
        assert.equal(previews.length,2,'the user must preview again after changing the intent');
        assert.deepEqual(previews[0][1],{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false});
        assert.deepEqual(previews[1][1],{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:true},
          'the second preview submits the checkbox value');
        const state=layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA',{replaceExisting:true});
        assert.equal(state.replaceExisting,true,'the new preview freezes replace_existing=true');
        assert.equal(state.publicationAllowed,true,'replacement makes the conflict publishable');
        assert.equal(state.expired,false);
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('apply submits the replace_existing frozen by the second preview',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow();
      const applyResult={status:'passed',adoption_id:'LRA-222222222222',route_id:'R-1',
        route_operating_layer_created:true,route_altitude_profile_created:false,
        departure_arrival_procedure_created:false};
      const respond=(path,payload)=>String(path).endsWith('/preview')
        ?fakePreviewResponse(flow,payload):applyResult;
      cacheLayeredAdoptionPreview(
        fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
        'LRV-AAAAAAAAAAAA',{replaceExisting:false});
      mountAdoptionPanel(document,flow);
      let {c,calls}=adoptionController(document,flow,{resourceAction:respond});
      bindLayeredOperationalAdoption(c);
      const replaceNode=document.getElementById('layeredAdoptionReplaceExisting');
      replaceNode.checked=true;
      replaceNode.onchange();
      return document.getElementById('previewLayeredAdoption').onclick().then(()=>{
        // 第二次 Preview 完成后重新渲染（main.js 的 resourceAction 会 renderWorkflow）：
        // 复选框按这些 Preview 的冻结值回填。
        mountAdoptionPanel(document,flow);
        ({c,calls}=adoptionController(document,flow,{resourceAction:respond}));
        bindLayeredOperationalAdoption(c);
        assert.equal(document.getElementById('layeredAdoptionReplaceExisting').checked,true,
          'the frozen replace_existing is reflected back into the checkbox');
        assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
          'unconfirmed apply stays disabled');
        document.getElementById('layeredAdoptionApplyConfirmed').checked=true;
        document.getElementById('layeredAdoptionApplyConfirmed').onchange();
        assert.equal(document.getElementById('applyLayeredAdoption').disabled,false,
          'current preview + publication_allowed + confirmed → enabled');
        return document.getElementById('applyLayeredAdoption').onclick();
      }).then(()=>{
        const applyCall=calls.filter(item=>String(item[0]).endsWith('/apply')).pop();
        assert.deepEqual(applyCall[1],{
          validation_id:'LRV-AAAAAAAAAAAA',confirmed:true,replace_existing:true,
          expected_validation_fingerprint:'layeredvalidationv1-fp-1',
        },'apply must submit the frozen replace_existing and fingerprint');
        // Apply 消费掉这份 Preview：不能二次 Apply
        return document.getElementById('applyLayeredAdoption').onclick().then(()=>{
          assert.equal(calls.filter(item=>String(item[0]).endsWith('/apply')).length,1);
          assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
            'a consumed preview disables apply again');
          resolve();
        });
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('apply never substitutes the current checkbox for the frozen preview intent',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  // 冻结 false：现场改成 true 不算数
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  const changedIntent=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(changedIntent.allowed,false);
  assert.equal(changedIntent.payload,null,'the checkbox must never overwrite the frozen intent');
  assert.match(changedIntent.reason,new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
  // 冻结 true：现场取消勾选同样不算数
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:true}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  const cancelledIntent=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:false});
  assert.equal(cancelledIntent.allowed,false);
  assert.equal(cancelledIntent.payload,null);
  assert.match(cancelledIntent.reason,new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
  // 冻结 true + 现场一致 → 提交冻结值
  const frozen=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(frozen.allowed,true);
  assert.equal(frozen.payload.replace_existing,true);
  // 完全不读 checkbox 时，payload 仍然来自 Preview 冻结值
  const withoutUI=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',{confirmed:true});
  assert.equal(withoutUI.allowed,true);
  assert.equal(withoutUI.payload.replace_existing,true);
  assert.equal(withoutUI.payload.expected_validation_fingerprint,
    layeredAdoptionPreviewState(flow,'LRV-AAAAAAAAAAAA').validationFingerprint);
  clearLayeredAdoptionCache();
});

test('every replace_existing change demands a new preview before apply',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  // 1) 第二份 Preview 冻结 true：允许替换
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:true}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  assert.equal(layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true}).allowed,true);
  // 2) 用户取消勾选 → 这份 Preview 立刻不可用
  const back=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:false});
  assert.equal(back.allowed,false,'cancelling the intent must also demand a new preview');
  assert.match(back.reason,new RegExp(LAYERED_ADOPTION_INTENT_CHANGED));
  // 3) 重新 Preview(false)：conflict 又把它挡回 publication_allowed=false
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  const rePreviewed=layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:false});
  assert.equal(rePreviewed.allowed,false);
  assert.match(rePreviewed.reason,/publication_allowed/);
  assert.match(renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  clearLayeredAdoptionCache();
});

test('fingerprint and validation changes still expire a replacement preview',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:true}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  const changed=baseFlow({validations:[validationFixture({
    validationFingerprint:'layeredvalidationv1-fp-NEW'})]});
  const state=layeredAdoptionPreviewState(changed,'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  assert.equal(state.evidenceChanged,true);
  assert.equal(state.expired,true,'a fingerprint change still expires the preview');
  assert.equal(state.replaceExisting,true,'the frozen intent stays readable while expired');
  const guard=layeredAdoptionApplyGuard(changed,'LRV-AAAAAAAAAAAA',
    {confirmed:true,replaceExisting:true});
  assert.equal(guard.allowed,false);
  assert.match(guard.reason,new RegExp(LAYERED_ADOPTION_EVIDENCE_CHANGED));
  assert.match(renderLayeredOperationalAdoption(changed,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  // 切换 validation 同样让含 replace 意图的 Preview 过期
  const other=baseFlow({validations:[validationFixture(),validationFixture({
    validationId:'LRV-BBBBBBBBBBBB',validationFingerprint:'layeredvalidationv1-fp-2'})]});
  const switched=layeredAdoptionApplyGuard(other,'LRV-BBBBBBBBBBBB',
    {confirmed:true,replaceExisting:true});
  assert.equal(switched.allowed,false);
  assert.match(switched.reason,new RegExp(LAYERED_ADOPTION_EVIDENCE_CHANGED));
  // 渲染层：切到另一条 validation 后 Apply 立即禁用，并明确要求重新 Preview
  withStubDom(document=>{
    stubDocumentQueries(document);
    cacheLayeredAdoptionPreview(
      fakePreviewResponse(other,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false},{conflict:false}),
      'LRV-AAAAAAAAAAAA',{replaceExisting:false});
    mountAdoptionPanel(document,other,'LRV-AAAAAAAAAAAA');
    const {c}=adoptionController(document,other,{});
    bindLayeredOperationalAdoption(c);
    document.getElementById('layeredAdoptionApplyConfirmed').checked=true;
    document.getElementById('layeredAdoptionApplyConfirmed').onchange();
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,false);
    const radio=findAll(document.body,'input[name="layeredAdoptionValidation"]')
      .find(node=>node.value==='LRV-BBBBBBBBBBBB');
    assert.ok(radio,'the second validation must be selectable');
    radio.checked=true;
    radio.onchange();
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
      'switching the validation disables apply');
    assert.match(textOf(document.getElementById('layeredAdoptionIntentChanged')),
      new RegExp(LAYERED_ADOPTION_INTENT_CHANGED),
      'the UI must say the intent changed and a new preview is required');
  });
  clearLayeredAdoptionCache();
});

test('the apply button state follows the frozen preview publication flag and confirmation',()=>{
  clearLayeredAdoptionCache();
  const flow=baseFlow();
  // 无 Preview → disabled
  assert.match(renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  // Preview expired（fingerprint 变化）→ disabled
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false},{conflict:false}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  const changed=baseFlow({validations:[validationFixture({
    validationFingerprint:'layeredvalidationv1-fp-NEW'})]});
  assert.match(renderLayeredOperationalAdoption(changed,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  // publication_allowed != true → disabled
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:false}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:false});
  assert.match(renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  // 当前 Preview + publication_allowed=true → 渲染层启用；未显式确认时 bind 仍禁用
  cacheLayeredAdoptionPreview(
    fakePreviewResponse(flow,{validation_id:'LRV-AAAAAAAAAAAA',replace_existing:true}),
    'LRV-AAAAAAAAAAAA',{replaceExisting:true});
  assert.doesNotMatch(renderLayeredOperationalAdoption(flow,'LRV-AAAAAAAAAAAA'),
    /id="applyLayeredAdoption" disabled/);
  withStubDom(document=>{
    stubDocumentQueries(document);
    mountAdoptionPanel(document,flow);
    const {c}=adoptionController(document,flow,{});
    bindLayeredOperationalAdoption(c);
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,true,
      'an unconfirmed apply stays disabled');
    document.getElementById('layeredAdoptionApplyConfirmed').checked=true;
    document.getElementById('layeredAdoptionApplyConfirmed').onchange();
    assert.equal(document.getElementById('applyLayeredAdoption').disabled,false,
      'current preview + publication_allowed + confirmed → enabled');
    // 双重把守：未确认时 guard 也拒绝
    assert.equal(layeredAdoptionApplyGuard(flow,'LRV-AAAAAAAAAAAA',
      {confirmed:false,replaceExisting:true}).allowed,false);
  });
  clearLayeredAdoptionCache();
});

// ---- 17. Revoke：目标必须显式选择 ------------------------------------------------

function adoptionCollection(items){
  return {schema_version:'layered-operational-adoption-collection-v1',
    status:'passed',count:items.length,items};
}

test('revoke without an explicit target is refused',async()=>{
  clearLayeredAdoptionCache();
  assert.equal(LAYERED_ADOPTION_REVOKE_TARGET_NAME,'layeredAdoptionRevokeTarget',
    'the revoke target radio group keeps its contracted name');
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow({adoptions:adoptionCollection([
        adoptionFixture({adoptionId:'LRA-PUBLISHED01'}),
        adoptionFixture({adoptionId:'LRA-STALE000001',status:'stale',applicability:'superseded'}),
      ])});
      mountAdoptionPanel(document,flow);
      const {c,calls}=adoptionController(document,flow,{});
      bindLayeredOperationalAdoption(c);
      assert.equal(selectedLayeredAdoptionRevokeTarget(c),'','no adoption may be auto-selected');
      assert.equal(document.getElementById('revokeLayeredAdoption').disabled,true,
        'revoke is disabled until a target is explicitly chosen');
      assert.match(textOf(document.getElementById('layeredAdoptionRevokeTargetNote')),
        /尚未显式选择撤销目标/);
      // 即使勾选确认，也不能撤销一个没有被显式选择的目标
      document.getElementById('layeredAdoptionRevokeConfirmed').checked=true;
      document.getElementById('layeredAdoptionRevokeConfirmed').onchange();
      return document.getElementById('revokeLayeredAdoption').onclick().then(()=>{
        assert.equal(calls.some(item=>String(item[0]).endsWith('/revoke')),false,
          'revoke must not fire without an explicit target');
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),
          /没有可撤销的 adoption/);
        assert.equal(layeredAdoptionRevokeGuard('',{confirmed:true}).allowed,false);
        assert.equal(layeredAdoptionRevokePayload('',{confirmed:true}).adoption_id,'');
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('revoke posts exactly the explicitly selected adoption_id',async()=>{
  clearLayeredAdoptionCache();
  await new Promise(resolve=>{
    withStubDom(document=>{
      stubDocumentQueries(document);
      const flow=baseFlow({adoptions:adoptionCollection([
        adoptionFixture({adoptionId:'LRA-PUBLISHED01'}),
        adoptionFixture({adoptionId:'LRA-STALE000001',status:'stale',applicability:'superseded'}),
      ])});
      mountAdoptionPanel(document,flow);
      const {c,calls}=adoptionController(document,flow,{});
      bindLayeredOperationalAdoption(c);
      const radio=findAll(document.body,'input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]')
        .find(node=>node.value==='LRA-STALE000001');
      assert.ok(radio,'every non-revoked adoption gets its own radio');
      assert.match(textOf(radio.closest('label')),
        /LRA-STALE000001 · route R-1 · status stale · applicability superseded · ownership route_owned true/,
        'the target row must show adoption_id / route_id / status / applicability / ownership');
      radio.checked=true;
      radio.onchange();
      assert.equal(selectedLayeredAdoptionRevokeTarget(c),'LRA-STALE000001');
      assert.equal(selectedLayeredAdoptionRevokeTargetId(),'LRA-STALE000001');
      assert.equal(document.getElementById('revokeLayeredAdoption').disabled,false);
      assert.match(textOf(document.getElementById('layeredAdoptionRevokeTargetNote')),/LRA-STALE000001/);
      // 未确认 → 拒绝
      return document.getElementById('revokeLayeredAdoption').onclick().then(()=>{
        assert.equal(calls.some(item=>String(item[0]).endsWith('/revoke')),false);
        assert.match(calls.filter(item=>item[0]==='error').map(item=>item[1]).join(' '),
          /显式确认/);
        document.getElementById('layeredAdoptionRevokeConfirmed').checked=true;
        document.getElementById('layeredAdoptionRevokeConfirmed').onchange();
        return document.getElementById('revokeLayeredAdoption').onclick();
      }).then(()=>{
        const revokeCall=calls.filter(item=>String(item[0]).endsWith('/revoke')).pop();
        assert.deepEqual(revokeCall[1],{adoption_id:'LRA-STALE000001',confirmed:true},
          'revoke must post the explicitly selected adoption_id');
        assert.equal(revokeCall[1].route_id,undefined,'the frontend never addresses a route directly');
        resolve();
      });
    });
  });
  clearLayeredAdoptionCache();
});

test('multiple published stale and superseded adoptions never auto-select a revoke target',()=>{
  clearLayeredAdoptionCache();
  withStubDom(document=>{
    stubDocumentQueries(document);
    const flow=baseFlow({adoptions:adoptionCollection([
      adoptionFixture({adoptionId:'LRA-A00000000001',status:'published'}),
      adoptionFixture({adoptionId:'LRA-B00000000002',status:'stale',applicability:'superseded'}),
      adoptionFixture({adoptionId:'LRA-C00000000003',status:'stale',applicability:'stale'}),
      adoptionFixture({adoptionId:'LRA-D00000000004',status:'revoked',applicability:'revoked'}),
    ])});
    mountAdoptionPanel(document,flow);
    const model=layeredOperationalAdoptionModel(flow);
    assert.equal(model.adoptions.length,4,'the full history stays visible');
    const radios=findAll(document.body,'input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]');
    assert.equal(radios.length,3,'every non-revoked adoption gets exactly one radio');
    for(const radio of radios)assert.equal(radio.checked,false,'no adoption may be auto-selected');
    assert.equal(selectedLayeredAdoptionRevokeTargetId(),'');
    assert.deepEqual(layeredAdoptionRevokeTargets(model).map(item=>item.adoptionId),
      ['LRA-A00000000001','LRA-B00000000002','LRA-C00000000003'],
      'the target list is never reordered into "the latest one"');
    // 源码层：撤销目标绝不按顺序猜
    const source=readFileSync(new URL('../cns_planner/web/js/workflow/layered_operational_adoption.js',import.meta.url),'utf8');
    assert.doesNotMatch(source,/reverse\(\)/,'the revoke target must never be guessed by list order');
    assert.doesNotMatch(source,/find\(item=>item\.status!=='revoked'\)/);
  });
  clearLayeredAdoptionCache();
});

test('revoked adoptions are never selectable as revoke targets',()=>{
  clearLayeredAdoptionCache();
  withStubDom(document=>{
    stubDocumentQueries(document);
    const flow=baseFlow({adoptions:adoptionCollection([
      adoptionFixture({adoptionId:'LRA-LIVE00000001',status:'published'}),
      adoptionFixture({adoptionId:'LRA-REVOKED0001',status:'revoked',applicability:'revoked'}),
    ])});
    mountAdoptionPanel(document,flow);
    const {c}=adoptionController(document,flow,{});
    bindLayeredOperationalAdoption(c);
    const radios=findAll(document.body,'input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]');
    assert.deepEqual(radios.map(node=>node.value),['LRA-LIVE00000001']);
    assert.equal(radios.some(node=>node.value==='LRA-REVOKED0001'),false,
      'a revoked adoption must not offer a selectable target');
    const panelText=textOf(document.getElementById('layeredAdoptionHost'));
    assert.match(panelText,/LRA-REVOKED0001[\s\S]*?revoked：已撤销，不可作为撤销目标/);
    // revoked 记录仍然完整留在历史里（statusBadge 的 flow-revoked 只在 HTML 类名上）
    const panelHtml=renderLayeredOperationalAdoption(flow);
    assert.match(panelHtml,/flow-revoked/,'the revoked record stays in the history');
    // 即使被硬塞一个 revoked id，也不会成为撤销目标
    assert.equal(syncSelectedLayeredAdoptionRevokeTarget(c,'LRA-REVOKED0001'),'',
      'a revoked adoption can never become the target');
    assert.equal(selectedLayeredAdoptionRevokeTargetId(),'');
    assert.equal(selectedLayeredAdoptionRevokeTarget(c),'');
    // 用户先前显式选过的目标被后端撤销后：重新渲染即校正，不再指着 revoked 记录
    const liveRadio=findAll(document.body,'input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]')[0];
    liveRadio.checked=true;
    liveRadio.onchange();
    assert.equal(selectedLayeredAdoptionRevokeTargetId(),'LRA-LIVE00000001');
    const allRevoked=baseFlow({adoptions:adoptionCollection([
      adoptionFixture({adoptionId:'LRA-LIVE00000001',status:'revoked',applicability:'revoked'}),
      adoptionFixture({adoptionId:'LRA-REVOKED0001',status:'revoked',applicability:'revoked'}),
    ])});
    mountAdoptionPanel(document,allRevoked);
    assert.equal(selectedLayeredAdoptionRevokeTargetId(),'',
      'a target the backend revoked must not stay selected');
    assert.equal(findAll(document.body,'input[name="'+LAYERED_ADOPTION_REVOKE_TARGET_NAME+'"]').length,0);
    assert.equal(document.getElementById('revokeLayeredAdoption').disabled,true);
  });
  clearLayeredAdoptionCache();
});

test('the revoke confirmation and replace intent are read from the rendered panel only',()=>{
  clearLayeredAdoptionCache();
  withStubDom(document=>{
    stubDocumentQueries(document);
    const flow=baseFlow();
    mountAdoptionPanel(document,flow);
    const {c}=adoptionController(document,flow,{});
    bindLayeredOperationalAdoption(c);
    // 冲突 Preview 未出现时 checkbox 不存在：意图就是 false
    assert.equal(selectedLayeredAdoptionReplaceExisting(c),false);
    assert.equal(document.getElementById('layeredAdoptionRevokeConfirmed').checked,false);
    assert.equal(document.getElementById('layeredAdoptionApplyConfirmed').checked,false);
    const refreshed=refreshLayeredAdoptionAffordances(c);
    assert.equal(refreshed.applyEnabled,false,'no preview → nothing to apply');
    assert.equal(refreshed.revokeTarget,'');
    // 面板自己记录的选择不会被 refresh 自动填上
    assert.equal(selectedLayeredAdoptionRevokeTargetId(),'');
  });
  clearLayeredAdoptionCache();
});
