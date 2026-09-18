/**
 * 前端工作台（右侧当前步骤面板）结构回归测试。
 *
 * 目标：锁定"信息架构"这一层契约，避免后续改动把它退回成一条长页面：
 *  - 每一步都渲染 操作 / 结果 / 高级 三个一级标签面板；
 *  - Step03 的二级分段齐全，且 id 在整步内唯一；
 *  - 同一时刻只有一个一级面板与一个二级面板可见（由 CSS 属性选择器保证）；
 *  - 一级标签与二级分段切换后，重新渲染不会回到第一个标签。
 *
 * 这些断言只检查展示组织，不涉及任何业务语义或数据模型。
 */
import assert from 'node:assert/strict';
import {readFileSync,readdirSync,statSync} from 'node:fs';
import test from 'node:test';
import path from 'node:path';

import {render as renderStep1} from '../cns_planner/web/js/workflow/step01_project.js';
import {render as renderStep2} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {render as renderStep3} from '../cns_planner/web/js/workflow/step03_routes.js';
import {render as renderStep4} from '../cns_planner/web/js/workflow/step04_operation.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6} from '../cns_planner/web/js/workflow/step06_review.js';
import {createWorkbench} from '../cns_planner/web/js/workflow/workbench.js';

// ---- 最小 DOM 桩：只支撑被测模块真正用到的接口 ------------------------------
// 这些步骤面板与工作台控制器不依赖浏览器布局，因此可以用一个极小的桩在
// Node 中验证"信息架构"契约；桩不模拟任何业务行为。

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
    this.dataset={};
    this.style={};
    this.classList=new StubClassList();
    this.children=[];
    this.parentNode=null;
    this.hidden=false;
    this.textContent='';
    this.id='';
  }
  set className(value){
    this.classList=new StubClassList();
    for(const name of String(value||'').split(/\s+/))if(name)this.classList.add(name);
  }
  get className(){return [...this.classList.values].join(' ');}
  append(...nodes){for(const node of nodes){node.parentNode=this;this.children.push(node);}}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  removeChild(node){this.children=this.children.filter(child=>child!==node);node.parentNode=null;return node;}
  contains(node){return this.children.includes(node);}
  get firstElementChild(){return this.children[0]||null;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){
    const [attribute,value]=parseAttributeSelector(selector);
    const out=[];
    const walk=node=>{
      for(const child of node.children){
        if(matches(child,attribute,value))out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  addEventListener(){}
  removeEventListener(){}
  setAttribute(name,value){this[name]=value;}
  getAttribute(name){return this[name];}
  removeAttribute(name){delete this[name];}
}

function parseAttributeSelector(selector){
  const match=/^\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(String(selector).trim());
  if(!match)return [null,null];
  return [match[1],match[2]===undefined?null:match[2]];
}

function matches(node,attribute,value){
  if(!attribute)return false;
  const key=attribute.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
  const current=node.dataset[key];
  if(current===undefined)return false;
  return value===null?true:String(current)===value;
}

/** 安装最小 DOM 桩，返回全部已注册 id 的索引。 */
function installStubDom(){
  const nodes=new Map();
  const document={
    body:new StubNode('body'),
    createElement:tag=>new StubNode(tag),
    getElementById:id=>nodes.get(id)||null,
    querySelector:()=>null,
    querySelectorAll:()=>[],
    addEventListener(){},
    register(id){const node=new StubNode('div');node.id=id;nodes.set(id,node);return node;}
  };
  globalThis.document=document;
  return document;
}

function withStubDom(run){
  const original=globalThis.document;
  const document=installStubDom();
  try{return run(document);}
  finally{globalThis.document=original;}
}

installStubDom();

// ---- 步骤上下文 -------------------------------------------------------------

function baseFlow(){
  return {
    project:{name:'测试项目'},
    nodes:[{node_id:'N001',name:'A',coordinate:[122,30]},{node_id:'N002',name:'B',coordinate:[122.1,30.1]}],
    scenario_routes:[{route_id:'R0001',direction:'N001→N002',path:[[122,30],[122.1,30.1]]}],
    operational_routes:[{route_id:'R0001',status:'passed',path:[[122,30],[122.1,30.1]],distance_m:1200}],
    algorithm_selection:{route_planner:{algorithm_id:'layered_risk_aware_route_planner_v1',version:'1.0',parameters:{}}},
    algorithm_catalog:[],
    retired_route_ids:[],
    risks:{environment:{status:'not_calculated'},life:{status:'not_calculated'},property:{status:'not_calculated'}},
    steps:{1:true,2:true,3:true},
    spatial_3d:{route_altitude_profiles:{},altitude_layers:[]},
    operational_timing:{route_motion_profiles:{}},
    route_vertical_profiles:{},
    building_clearance_policy:{},
    building_clearance_assessment:{},
    reference_routes:{items:[],points:[]},
    reference_landing_sites:{items:[]},
    route_planning_experiments:{},
    reference_route_links:{items:[]},
    reference_endpoint_candidates:{},
    data_readiness:{blocks:{}},
    workspace:{bbox:[122,29.9,122.2,30.1],area_km2:12.5,health:{population:{status:'passed'},airspace:{status:'passed'},terrain:{status:'passed'},loaded_layer_count:4}},
    grid:{status:'passed',level:8,count:10},
    review:{risks:{environment:{status:'passed'},technical:{status:'passed'},life:{status:'passed'},property:{status:'passed'}},overall_status:'passed',overall_pass:true},
    result_statuses:{},
    coverage:{layers:{}},
    cns_plan_review:{},
    confirmed_cns_plan:{},
    cns_planning_reports:{},
    required_cns:{project_default:{communication:{},navigation:{},surveillance:{}}},
    aircraft_profiles:{items:[]},
    device_catalog:{items:[]},
    existing_cns_facilities:{items:[]},
    candidate_sites:{items:[]},
    devices:[],
    defaults:{engineering_parameters:{primary_spacing_factor:{value:3,source:'x'},co_location_search_radius_m:{value:300,source:'x'}}},
    rules:{status:'passed',message:'',height_mode:'different'}
  };
}

function baseState(){
  return {data_health:{status:'ready',label:'正常'},layers:[],project_storage:{directory:''},paths:{},population:{},terrain:{},terrain_dtm:{}};
}

const STEP_RENDERS=[
  ['01',context=>renderStep1(context)],
  ['02',context=>renderStep2(context)],
  ['03',context=>renderStep3(context)],
  ['04',context=>renderStep4(context)],
  ['05',context=>renderStep5(context)],
  ['06',context=>renderStep6(context)]
];

function stepContext(){
  return {
    state:baseState(),flow:baseFlow(),draftWorkspace:null,
    gridDisplay:{outline:true,theme:'none'},
    interactionMode:'pan',selectedReference:null,
    populationDisplayLabel:()=>'人口',formatNumber:value=>String(value)
  };
}

// ---- 测试 -------------------------------------------------------------------

test('every step renders exactly one panel per workbench tab',()=>{
  for(const [number,render] of STEP_RENDERS){
    const html=render(stepContext());
    const tabs=[...html.matchAll(/data-panel-group="([a-z]+)"/g)].map(match=>match[1]);
    assert.deepEqual(tabs.slice().sort(),['advanced','operate','result'],`step ${number} tab panels`);
  }
});

test('step 03 exposes the documented second-level segments',()=>{
  const html=renderStep3(stepContext());
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  for(const required of ['op-sites','op-candidates','op-operational','op-altitude','res-route','res-feasibility','res-compare','adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
    assert.ok(segs.includes(required),`missing segment ${required}`);
  }
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  // 每个二级分段都挂在一个一级面板下
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
});

test('workbench navigation state survives a re-render',()=>{
  withStubDom(document=>{
    const store={step:1,tab:'advanced',segs:{advanced:'adv-diagnostics'},scroll:120};
    const controller=createWorkbench({
      getState:()=>store,
      setState:value=>Object.assign(store,value)
    });
    const root=document.createElement('div');
    root.className='wb-root';
    controller.mount({root,step:{number:3,title:'航路规划',note:'',panels:{operate:1,result:1,advanced:1},segments:[{id:'adv-diagnostics',label:'诊断',tab:'advanced'}]}});
    assert.equal(root.id,'workbenchPanel','the mounted panel carries a stable id');
    assert.equal(root.dataset.tab,'advanced','the previous first-level tab is restored');
    assert.equal(root.dataset.seg,'adv-diagnostics','the previous second-level segment is restored');
    assert.equal(store.scroll,120,'scroll position is preserved by the caller, not reset here');
  });
});

test('the shell keeps the left rail, map and workbench as distinct regions',()=>{
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  for(const id of ['appShell','steps','map','canvas','workflowPanel','layerDrawer','lodStatus','workbenchBody']){
    assert.match(html,new RegExp('id="'+id+'"'));
  }
  // 图层抽屉取代了左栏的图层长列表
  assert.match(html,/id="layerDrawer"[\s\S]*id="gridLayer"/);
  assert.doesNotMatch(html,/class="sidebar"/);
  assert.doesNotMatch(html,/id="workflowPanel"><div class="section-label"/);
});

test('no stylesheet keeps a fixed 400px sidebar or a 72vh panel',()=>{
  const directory=new URL('../cns_planner/web/css/',import.meta.url);
  const files=readdirSync(directory).filter(name=>name.endsWith('.css'));
  for(const name of files){
    const css=readFileSync(new URL(name,directory),'utf8');
    assert.doesNotMatch(css,/width:\s*400px\s*!important/,`${name} still forces a 400px sidebar`);
    assert.doesNotMatch(css,/72vh/,`${name} still reserves a 72vh panel`);
    assert.doesNotMatch(css,/!important/,`${name} uses !important patches`);
  }
});

test('LOD thresholds live in one module only',()=>{
  const files=[];
  const walk=directory=>{
    for(const name of readdirSync(directory)){
      const full=path.join(directory,name);
      if(statSync(full).isDirectory())walk(full);
      else if(name.endsWith('.js'))files.push(full);
    }
  };
  walk('cns_planner/web/js');
  const lod=readFileSync('cns_planner/web/js/map/lod.js','utf8');
  assert.match(lod,/LOD_THRESHOLDS/);
  assert.match(lod,/CLUSTER_PIXEL_THRESHOLD/);
  for(const file of files){
    if(file.endsWith(path.join('map','lod.js')))continue;
    const source=readFileSync(file,'utf8');
    assert.doesNotMatch(source,/view\.res\s*<\s*\d{3,}/,`${file} hardcodes a view.res threshold`);
    assert.doesNotMatch(source,/view\.res\s*>\s*\d{3,}/,`${file} hardcodes a view.res threshold`);
  }
});

export {StubNode};
