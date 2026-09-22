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
import {render as renderStep2,bind as bindStep2} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {render as renderStep3} from '../cns_planner/web/js/workflow/step03_routes.js';
import {render as renderStep4,bind as bindStep4} from '../cns_planner/web/js/workflow/step04_operation.js';
import {render as renderStep5,bind as bindStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep6,bind as bindStep6} from '../cns_planner/web/js/workflow/step06_review.js';
import {createWorkbench} from '../cns_planner/web/js/workflow/workbench.js';
import {riskV2ThemeOptions} from '../cns_planner/web/js/workflow/risk_framework_v2.js';
import {renderWorkflowSteps} from '../cns_planner/web/js/workflow/steps.js';

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
    this.attributes={};
    this.style={};
    this.classList=new StubClassList();
    this.children=[];
    this.parentNode=null;
    this.hidden=false;
    this.textContent='';
    this.id='';
    this.listeners={};
    this.scrollTop=0;
    this.scrollHeight=0;
    this.clientHeight=0;
  }
  set className(value){
    this.classList=new StubClassList();
    for(const name of String(value||'').split(/\s+/))if(name)this.classList.add(name);
  }
  get className(){return [...this.classList.values].join(' ');}
  append(...nodes){
    for(const node of nodes){
      // DocumentFragment 语义：插入其子节点而不是 fragment 本身
      if(node&&node.tagName==='FRAGMENT'){
        this.append(...node.children.slice());
        continue;
      }
      node.parentNode=this;
      this.children.push(node);
    }
  }
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  removeChild(node){this.children=this.children.filter(child=>child!==node);node.parentNode=null;return node;}
  contains(node){return this.children.includes(node);}
  get firstElementChild(){return this.children[0]||null;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  querySelectorAll(selector){
    const parsed=parseSelector(selector);
    const out=[];
    forEachDescendant(this,child=>{if(parsed.some(part=>matchesSelector(child,part)))out.push(child);});
    return out;
  }
  addEventListener(name,handler){(this.listeners[name]||(this.listeners[name]=[])).push(handler);}
  removeEventListener(name,handler){
    const list=this.listeners[name]||[];
    const index=list.indexOf(handler);
    if(index>=0)list.splice(index,1);
  }
  dispatchEvent(event){
    for(const handler of this.listeners[event.type]||[])handler(event);
    return true;
  }
  /** 只沿父链匹配，不遍历子树；语义足够支撑 document 级点击委托。
   *  与 querySelectorAll 一致地支持逗号选择器（closest('[data-wb-tab],[data-wb-seg]')）。 */
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

// 支持四种选择器：标签名、.class、tag.class、[attr]/[attr="value"]（本项目模板只用到这些）
function parsePart(text){
  const attribute=/^\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(text);
  if(attribute)return {kind:'attr',name:attribute[1],value:attribute[2]===undefined?null:attribute[2]};
  const className=/^\.([A-Za-z][-A-Za-z0-9_]*)$/.exec(text);
  if(className)return {kind:'class',name:className[1]};
  const tagAndClass=/^([A-Za-z][-A-Za-z0-9]*)\.([A-Za-z][-A-Za-z0-9_]*)$/.exec(text);
  if(tagAndClass)return [
    {kind:'tag',name:tagAndClass[1].toUpperCase()},
    {kind:'class',name:tagAndClass[2]}
  ];
  const tag=/^[A-Za-z][-A-Za-z0-9]*$/.exec(text);
  if(tag)return {kind:'tag',name:text.toUpperCase()};
  return {kind:'none'};
}

function parseSelector(selector){
  return String(selector).split(',').map(part=>{
    const parsed=parsePart(part.trim());
    return Array.isArray(parsed)?parsed:[parsed];
  });
}

function matchesSelector(node,parsed){
  const parts=Array.isArray(parsed[0])?parsed[0]:parsed;
  return parts.every(part=>{
    if(part.kind==='tag')return node.tagName===part.name;
    if(part.kind==='class')return node.classList.contains(part.name);
    if(part.kind!=='attr')return false;
    // 与 parseAttributes 一致：data-* 属性落在 dataset 上（键名去掉 data- 前缀）
    const base=part.name.startsWith('data-')?part.name.slice(5):part.name;
    const key=toDatasetKey(base);
    const current=node.dataset[key];
    if(current===undefined)return false;
    return part.value===null?true:String(current)===part.value;
  });
}

/** 在树中查找匹配的节点（测试辅助，等价于 querySelectorAll）。 */
function findAll(root,selector){
  const parts=parseSelector(selector),out=[];
  forEachDescendant(root,node=>{
    if(parts.some(part=>matchesSelector(node,part)))out.push(node);
  });
  return out;
}

/** 派发一次点击：等价于浏览器中从事件目标冒泡到 document 的委托过程。 */
function clickNode(document,node){
  const event={type:'click',target:node};
  for(let current=node;current;current=current.parentNode){
    current.dispatchEvent(event);
  }
}

// ---- 极简 HTML 解析：让 template.innerHTML 生成真实节点树 -------------------
// 只支持本项目模板用到的写法（标签、引号包裹的属性、布尔属性），用于端到端
// 验证 render → mount 之后 DOM 是否完整，而不是只检查 HTML 字符串。

function toDatasetKey(attribute){
  return attribute.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
}

function parseAttributes(text,node){
  for(const match of text.matchAll(/([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g)){
    const name=match[1],value=match[2]===undefined?'':match[2];
    if(name.startsWith('data-'))node.dataset[toDatasetKey(name.slice(5))]=value;
    else node.attributes[name]=value;
    if(name==='id')node.id=value;
    if(name==='class')node.className=value;
  }
}

function parseHtml(html){
  const root=new StubNode('div');
  const stack=[root];
  const text=String(html||'').replace(/<!--[\s\S]*?-->/g,'');
  // 标签必须以字母或 / 紧跟 "<"：业务文案里的 "Legacy / Risk-Aware V2" 之类
  // 裸 "<"、"/" 组合不会被误判成标签边界。
  const tokens=/<(\/?)([A-Za-z][-A-Za-z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let cursor=0,match;
  while((match=tokenRegexStep(tokens,text,cursor))!==null){
    const textPart=text.slice(cursor,match.index);
    if(textPart.trim())stack[stack.length-1].textContent+=textPart;
    cursor=tokens.lastIndex;
    if(match[1]){
      if(stack.length>1)stack.pop();
      continue;
    }
    const tag=match[2].toLowerCase();
    const node=new StubNode(tag);
    parseAttributes(match[3]||'',node);
    stack[stack.length-1].append(node);
    // void 元素（input/img/br…）没有子节点，但仍必须建节点并登记 id
    if(!VOID_TAGS.has(tag))stack.push(node);
  }
  const tail=text.slice(cursor);
  if(tail.trim())stack[stack.length-1].textContent+=tail;
  return root;
}

function tokenRegexStep(regex,text,cursor){
  regex.lastIndex=cursor;
  const match=regex.exec(text);
  return match?match:null;
}

const VOID_TAGS=new Set(['br','hr','img','input','meta','link','source','col','area','base','wbr']);

function forEachDescendant(node,visit){
  for(const child of node.children){
    visit(child);
    forEachDescendant(child,visit);
  }
}

/** 在子树中按 dataset 键值查找（不依赖选择器实现，语义与 querySelector 一致）。 */
function findByDataset(root,key,value){
  let found=null;
  forEachDescendant(root,node=>{
    if(found)return;
    if(node.dataset&&String(node.dataset[key])===String(value))found=node;
  });
  return found;
}

function createTemplateElement(){
  const node=new StubNode('template');
  Object.defineProperty(node,'innerHTML',{
    get(){return '';},
    set(html){
      const parsed=parseHtml(html);
      const content=new StubNode('fragment');
      for(const child of parsed.children.slice())content.append(child);
      Object.defineProperty(node,'content',{value:content,configurable:true,writable:true});
    },
    configurable:true
  });
  Object.defineProperty(node,'content',{value:new StubNode('fragment'),configurable:true,writable:true});
  return node;
}

/** 在子树中登记 id（append 时同步注册），等价于浏览器语义。 */
function registerTree(nodes,node){
  if(node.id)nodes.set(node.id,node);
  for(const child of node.children)registerTree(nodes,child);
}

/** 让 StubNode.append 自动登记 id；只打一次补丁，避免重复调用时叠加。 */
function patchAppendOnce(){
  if(StubNode.prototype.append.__dshRegistersIds)return;
  const original=StubNode.prototype.append;
  const patched=function(...children){
    const result=original.apply(this,children);
    // 归属当前桩文档：withStubDom 会替换 globalThis.document，
    // 因此登记必须按本次调用的文档进行，否则 id 会写进上一次的索引。
    const document=globalThis.document;
    if(document&&document.__nodes)for(const child of children)registerTree(document.__nodes,child);
    return result;
  };
  patched.__dshRegistersIds=true;
  StubNode.prototype.append=patched;
}

function installStubDom(){
  patchAppendOnce();
  const nodes=new Map();
  const document={
    body:new StubNode('body'),
    __nodes:nodes,
    __listeners:{},
    createElement:tag=>String(tag).toLowerCase()==='template'?createTemplateElement():new StubNode(tag),
    getElementById:id=>nodes.get(id)||null,
    querySelector:()=>null,
    querySelectorAll:()=>[],
    addEventListener(name,handler){(document.__listeners[name]||(document.__listeners[name]=[])).push(handler);},
    removeEventListener(){},
    dispatchEvent(event){
      for(const handler of document.__listeners[event.type]||[])handler(event);
      return true;
    },
    register(id,className=''){const node=new StubNode('div');node.id=id;node.className=className;nodes.set(id,node);document.body.append(node);return node;}
  };
  // 浏览器语义：body 的父节点是 document，事件才能从目标冒泡到 document 委托监听
  document.body.parentNode=document;
  const workflowPanel=new StubNode('div');
  workflowPanel.id='workflowPanel';
  document.body.append(workflowPanel);
  nodes.set('workflowPanel',workflowPanel);
  // 右侧工作台的固定骨架：一级标签容器、二级分段容器、滚动区、步骤标题区
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
  for(const required of ['op-sites','op-candidates','op-operational','op-altitude','res-route','res-feasibility','res-risk-profile','res-compare','adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
    assert.ok(segs.includes(required),`missing segment ${required}`);
  }
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  // 每个二级分段都挂在一个一级面板下
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
});

// ---- 真实集成：render → renderWorkflowSteps → mount，只用生产路径 ----------
//
// 这一段是本轮修复的核心回归。它刻意**不**传任何 panels/segments metadata：
// 工作台必须从真实步骤模块渲染出的 DOM 里自行发现一级面板与二级分段，
// 并使用真实的点击委托导航，否则生产环境依旧是"没有标签、点了没反应"。

function mountRealStep(render,store,stepNo=1,title='步骤'){
  const controller=createWorkbench({
    getState:()=>store,
    setState:value=>Object.assign(store,value)
  });
  const root=renderWorkflowSteps({step:{render},context:stepContext()});
  controller.mount({root,step:{number:stepNo,title,note:''}});
  return {controller,root};
}

function tabButtons(document){
  return document.getElementById('workbenchTabs').children;
}

function segButtons(document){
  const container=document.getElementById('workbenchSegs');
  const group=container.children.find(child=>child.classList.contains('segmented'));
  return group?group.children:[];
}

/** 直接读节点文本（button 的 textContent 会被 parseHtml 的文本聚合影响）。 */
function segControls(document){
  return segButtons(document).map(node=>node.textContent);
}

function activateProbe(target){
  if(!target||!target.dataset)return null;
  if(target.dataset.wbTab)return {tab:target.dataset.wbTab};
  if(target.dataset.wbSeg)return {seg:target.dataset.wbSeg,segSet:target.dataset.wbSegSet};
  return null;
}

test('step 01 mount generates the three first-level tabs from the real DOM',()=>{
  withStubDom(document=>{
    const store={step:1,tab:'operate',segs:{},scroll:0};
    mountRealStep(renderStep1,store,1,'项目准备');
    const tabs=tabButtons(document);
    assert.deepEqual(tabs.map(node=>node.textContent),['操作','结果','高级'],'operate/result/advanced must all be generated');
    assert.deepEqual(tabs.map(node=>node.dataset.wbTab),['operate','result','advanced']);
    assert.equal(tabs[0].classList.contains('tab-active'),true);
    // 一级容器自身就是 .tabbar，不能再嵌套一层 .tabbar
    assert.equal(document.getElementById('workbenchTabs').classList.contains('tabbar'),true);
    assert.equal(findAll(document.getElementById('workbenchTabs'),'.tabbar').length,0,'no .tabbar > .tabbar');
    assert.equal(document.getElementById('workbenchSegs').hidden,true,'step 01 has no second-level segments');
  });
});

test('first-level clicks navigate and switch the visible panel',()=>{
  withStubDom(document=>{
    const store={step:1,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep1,store,1,'项目准备');
    const clicked=[];
    for(const label of ['结果','高级','操作']){
      const target=tabButtons(document).find(node=>node.textContent===label);
      assert.ok(target,`tab button ${label} must exist`);
      clickNode(document,target);
      clicked.push(label);
      const expected={操作:'operate',结果:'result',高级:'advanced'}[label];
      assert.equal(root.dataset.tab,expected,`clicking ${label} sets data-tab`);
      assert.equal(store.tab,expected,`clicking ${label} writes the workbench state`);
      assert.equal(tabButtons(document).find(node=>node.classList.contains('tab-active')).textContent,label);
    }
    // 一级面板始终在 DOM 中（由 CSS 属性选择器决定显隐），切换不重新渲染业务内容
    for(const group of ['operate','result','advanced']){
      assert.ok(findByDataset(root,'panelGroup',group),`${group} panel stays mounted`);
    }
  });
});

test('step 03 discovers its segments from the active panel and restores selection',()=>{
  withStubDom(document=>{
    const store={step:3,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep3,store,3,'航路规划');

    // 默认：操作 → op-sites，4 个二级按钮
    assert.equal(root.dataset.tab,'operate');
    assert.equal(root.dataset.seg,'op-sites','operate defaults to the first segment');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),
      ['op-sites','op-candidates','op-operational','op-altitude']);

    // 结果 → res-route，4 个二级按钮
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(root.dataset.tab,'result');
    assert.equal(root.dataset.seg,'res-route','result defaults to the first segment');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),
      ['res-route','res-feasibility','res-risk-profile','res-compare']);

    // 高级 → adv-reference，5 个二级按钮
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(root.dataset.tab,'advanced');
    assert.equal(root.dataset.seg,'adv-reference','advanced defaults to the first segment');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),
      ['adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']);
    // 二级按钮名称来自分段自身的 data-seg-label，不是外部 manifest
    assert.deepEqual(segButtons(document).map(node=>node.textContent),
      ['参考数据与关联','Legacy / Risk-Aware V2','V3 实验','规划诊断','剖面与运动']);

    // 选择 adv-diagnostics 后重新 render + mount，仍恢复 advanced + adv-diagnostics
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='adv-diagnostics'));
    assert.equal(root.dataset.seg,'adv-diagnostics');
    assert.equal(store.segs.advanced,'adv-diagnostics','segment choice is written to the workbench state');

    const remounted=mountRealStep(renderStep3,store,3,'航路规划');
    assert.equal(remounted.root.dataset.tab,'advanced','tab survives a re-render');
    assert.equal(remounted.root.dataset.seg,'adv-diagnostics','segment survives a re-render');
    assert.equal(segButtons(document).find(node=>node.classList.contains('seg-active')).dataset.wbSeg,'adv-diagnostics');

    // 切回操作再切回高级：该 tab 之前选过的分段必须恢复
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='operate'));
    assert.equal(remounted.root.dataset.seg,'op-sites');
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(remounted.root.dataset.seg,'adv-diagnostics','a previously chosen segment is restored');
  });
});

test('step 02/04/05/06 business controls live in a normal section body',()=>{
  const expected=[
    ['02',renderStep2,['drawWorkspace','clearWorkspace','saveWorkspace','gridOutlineToggle']],
    ['04',renderStep4,['manufacturer','model','cruise','saveRules','aircraftProfile']],
    ['05',renderStep5,['saveDevices','planCoverage','analyzeGaps','sitePolicyConfirmed']],
    ['06',renderStep6,['initializePlanReview','confirmPlan','applyPlan','previewPlanningReport','generatePlanningReport']]
  ];
  for(const [number,render,ids] of expected){
    withStubDom(document=>{
      const store={step:Number(number),tab:'operate',segs:{},scroll:0};
      const {root}=mountRealStep(render,store,Number(number),'步骤 '+number);
      const missing=ids.filter(id=>!document.getElementById(id));
      assert.deepEqual(missing,[],`step ${number} controls must stay mounted: ${missing.join(', ')}`);
      for(const id of ids){
        const node=document.getElementById(id);
        assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
        assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
      }
      // 三个一级面板都生成
      for(const group of ['operate','result','advanced']){
        assert.ok(findByDataset(root,'panelGroup',group),`step ${number} ${group} panel`);
      }
    });
  }
});

test('no .wb-section-head carries block-level business content',()=>{
  for(const [number,render] of STEP_RENDERS){
    const html=render(stepContext());
    for(const head of html.match(/<div class="wb-section-head">[\s\S]*?<\/div>/g)||[]){
      // 只允许 <h3> 与状态徽章（<span class="flow-badge …">）
      for(const tag of ['input','select','button','table','fieldset','section','p']){
        assert.doesNotMatch(head,new RegExp('<'+tag+'[\\s>]'),`step ${number} .wb-section-head carries a <${tag}> body`);
      }
      const divs=[...head.matchAll(/<div[\s>]/g)].length;
      assert.ok(divs<=1,`step ${number} .wb-section-head carries a nested <div> body`);
      assert.match(head,/<h3>/,'a section head must keep its title');
    }
  }
});

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
  for(const required of ['op-sites','op-candidates','op-operational','op-altitude','res-route','res-feasibility','res-risk-profile','res-compare','adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
    assert.ok(segs.includes(required),`missing segment ${required}`);
  }
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  // 每个二级分段都挂在一个一级面板下，并且自描述名称与 id 成对出现
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  // 自描述：data-seg-label（单数）与 data-seg-name 成对出现，data-seg-labels 是面板级汇总
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');
  // 显隐不再由 CSS 枚举选择器承担：只允许两个状态类，禁止按 id 枚举或按前缀兜底
  const css=readFileSync(new URL('../cns_planner/web/css/components.css',import.meta.url),'utf8');
  assert.match(css,/\.wb-panel\{display:none\}/);
  assert.match(css,/\.wb-panel-active\{display:block\}/);
  assert.match(css,/\.wb-seg\{display:none\}/);
  assert.match(css,/\.wb-seg-active\{display:block\}/);
  assert.doesNotMatch(css,/data-seg-name/, 'segment visibility must not enumerate data-seg-name selectors');
  assert.doesNotMatch(css,/data-seg-name\^=/, 'no prefix fallback for segment visibility');
  assert.doesNotMatch(css,/\[data-tab=/, 'root data-tab must not drive panel visibility');
});

test('every step generates three tabs, unique ids and bindable controls',()=>{
  const modules={
    '01':'step01_project.js','02':'step02_workspace.js','03':'step03_routes.js',
    '04':'step04_operation.js','05':'step05_cns.js','06':'step06_review.js'
  };
  const segmentOwners=new Map();
  for(const [number,render] of STEP_RENDERS){
    withStubDom(document=>{
      const store={step:Number(number),tab:'operate',segs:{},scroll:0};
      const {root}=mountRealStep(render,store,Number(number),'步骤 '+number);
      // A. 三个一级标签都能生成（面板来自真实 DOM，不依赖 step.panels）
      assert.deepEqual(tabButtons(document).map(node=>node.dataset.wbTab),['operate','result','advanced'],`step ${number} tabs`);
      // 无重复 id：每个 id 在整步内只出现一次
      const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
      assert.equal(new Set(ids).size,ids.length,`step ${number} duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
      // 每个分段 id 只属于一个一级标签，避免跨标签串显
      for(const node of findAll(root,'[data-seg-name]')){
        const owner=node.parentNode&&node.parentNode.dataset?node.parentNode.dataset.panelGroup:'';
        if(segmentOwners.has(node.dataset.segName)){
          assert.equal(segmentOwners.get(node.dataset.segName),owner,`segment ${node.dataset.segName} declared twice`);
        }
        segmentOwners.set(node.dataset.segName,owner);
      }
      // bind() 中 c.$()/actionButton() 无条件查询的控件必须在挂载后仍然存在；
      // if(c.$('X')) 守卫的可选控件、以及只在特定算法/状态下面板才渲染的控件
      // （bind 已用守卫保护）允许缺席。
      const source=readFileSync(new URL('../cns_planner/web/js/workflow/'+modules[number],import.meta.url),'utf8');
      const optional=new Set();
      for(const match of source.matchAll(/if\(c\.\$\('([A-Za-z_][A-Za-z0-9_]*)'\)\)/g))optional.add(match[1]);
      // 条件渲染（另一个模块导出的面板只在特定状态下输出控件）
      for(const id of ['routeRiskLambda','routeRiskComponent','routeUnknownPolicy','routeUnknownPenalty','routeMaxRisk'])optional.add(id);
      const required=new Set();
      for(const match of source.matchAll(/(?:c\.\$\(|c\.actionButton\()'([A-Za-z_][A-Za-z0-9_]*)'/g)){
        // 以 "_" 结尾的是模板拼接 id（'operationContext_'+name），不是静态控件
        if(match[1].endsWith('_'))continue;
        if(!optional.has(match[1]))required.add(match[1]);
      }
      const missing=[...required].filter(id=>!document.getElementById(id));
      assert.deepEqual(missing,[],`step ${number} bind() queries missing controls: ${missing.join(', ')}`);
      // 反向契约：bind() 无条件读取的控件必须真的渲染出来（否则点击即抛异常）
      assert.doesNotMatch(source, /c\.\$\('routeDirection'\)/, 'bind() must not read a control the panel no longer renders');
    });
  }
});

test('step 03 exposes all thirteen segments through real click navigation',()=>{
  withStubDom(document=>{
    const store={step:3,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep3,store,3,'航路规划');
    const expected={
      operate:['op-sites','op-candidates','op-operational','op-altitude'],
      result:['res-route','res-feasibility','res-risk-profile','res-compare'],
      advanced:['adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']
    };
    for(const [tab,segments] of Object.entries(expected)){
      clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===tab));
      assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),segments,`${tab} segments`);
      // 每个分段都能被真实点击选中，并落到根节点的 data-seg 上
      for(const id of segments){
        clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
        assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
        assert.equal(store.segs[tab],id,`clicking ${id} stores it under ${tab}`);
        assert.equal(segButtons(document).find(node=>node.classList.contains('seg-active')).dataset.wbSeg,id);
      }
    }
    // 隐藏面板里的控件仍然挂载（因此现有 bind() 不会失效）
    for(const id of ['createOdRoute','saveRouteAltitude','saveRouteMotion','saveBuildingClearancePolicy','aircraftProfile']){
      const node=document.getElementById(id);
      if(node)assert.ok(findByDataset(root,'panelGroup','operate')||findByDataset(root,'panelGroup','advanced'),`#${id} stays mounted`);
    }
  });
});

test('main.js keeps no workbench DOM navigation details',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
  assert.doesNotMatch(source,/data-wb-tab|data-wb-seg|wbTab|wbSeg/, 'main.js must not bind workbench tab/segment clicks');
  assert.doesNotMatch(source,/workbenchTabs|workbenchSegs/, 'main.js must not build workbench navigation DOM');
  assert.doesNotMatch(source,/workbench\.navigate\(/, 'navigation belongs to createWorkbench');
  assert.match(source,/createWorkbench\(\{/, 'main.js only constructs the workbench controller');
});

test('workbench navigation state survives a re-render',()=>{  withStubDom(document=>{
    const store={step:3,tab:'advanced',segs:{advanced:'adv-diagnostics'},scroll:120};
    const controller=createWorkbench({
      getState:()=>store,
      setState:value=>Object.assign(store,value)
    });
    const root=renderWorkflowSteps({step:{render:renderStep3},context:stepContext()});
    // 真实步骤模块不导出 panels/segments：工作台只能从 DOM 恢复导航
    controller.mount({root,step:{number:3,title:'航路规划',note:''}});
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

// ---- 启动级集成回归：render → renderWorkflowSteps → mount -------------------
//
// 这一段锁定真实回归：shell() 必须返回唯一根容器，否则 mount() 的
// replaceChildren(root) 会丢弃全部业务面板与控件。

function mountStep01(document){
  const controller=createWorkbench({
    getState:()=>({step:1,tab:'operate',segs:{},scroll:0}),
    setState:()=>{}
  });
  const root=renderWorkflowSteps({
    step:{render:renderStep1},
    context:{
      state:baseState(),flow:baseFlow(),draftWorkspace:null,
      gridDisplay:{outline:true,theme:'none'},interactionMode:'pan',selectedReference:null,
      populationDisplayLabel:()=>'人口',formatNumber:value=>String(value)
    }
  });
  controller.mount({root,step:{number:1,title:'项目准备',note:'',panels:{operate:1,result:1,advanced:1}}});
  return {controller,root};
}

test('step 01 controls survive render → mount',()=>{
  withStubDom(document=>{
    const {root}=mountStep01(document);
    const panel=document.getElementById('workflowPanel');
    assert.ok(root,'renderWorkflowSteps must return the panel root');
    assert.equal(root.classList.contains('wb-root'),true,'the root keeps the wb-root class');
    assert.equal(panel.children.length,1,'workflowPanel holds exactly the mounted root');
    assert.equal(panel.children[0],root,'the mounted node is the rendered root, not a bare header');
    for(const id of ['projectName','projectPath','browseProject','saveProject','nextStep']){
      assert.ok(document.getElementById(id),`#${id} must survive mount (registered: ${[...document.__nodes.keys()].join(',')})`);
    }
    for(const panelName of ['operate','result','advanced']){
      assert.ok(findByDataset(root,'panelGroup',panelName),`missing ${panelName} panel after mount`);
    }
    // 标题元数据节点常驻 DOM（后续每次导航都要读它），但被隐藏、不重复显示标题
    const head=root.querySelector('[data-workbench-head]');
    assert.ok(head,'the head metadata node must stay mounted for later navigation');
    assert.equal(head.hidden,true,'the head metadata node never participates in display');
  });
});

test('shell markup has exactly one root container',()=>{
  const html=renderStep1(stepContext());
  const roots=[...html.matchAll(/class="wb-root"/g)].length;
  assert.equal(roots,1,'shell() must emit exactly one wb-root container');
  assert.match(html,/^<div class="wb-root">/,'the root container opens the markup');
  assert.match(html,/<\/div>$/,'the root container closes the markup');
  // 标题占位必须在根容器内部，而不是它的兄弟
  assert.match(html,/<div class="wb-root"><div data-workbench-head/);
  assert.match(html,/data-panel-group="operate"/);
  assert.match(html,/id="projectName"/);
});

test('template parsing registers every id including void elements',()=>{
  const template=createTemplateElement();
  template.innerHTML='<div class="wb-root"><div data-workbench-head></div><label>x</label><input class="panel-input" id="projectName" value="T"><div class="panel-file-input"><input class="panel-input" id="projectPath" placeholder="sel"><button class="secondary" id="browseProject">选择…</button></div></div>';
  const ids=[];
  forEachDescendant(template.content,node=>{if(node.id)ids.push(node.id);});
  assert.deepEqual(ids,['projectName','projectPath','browseProject'],'stub must register every id');
});

test('main.js wires layer switches and bootstrap once',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/main.js',import.meta.url),'utf8');
  const shellSource=readFileSync(new URL('../cns_planner/web/js/shell.js',import.meta.url),'utf8');
  // LAYER_IDS 必须包含参考航路点图层，displayPlan 依赖该 key
  assert.match(source,/const LAYER_IDS=\[[^\]]*'referenceRoutePointLayer'/);
  assert.match(source,/const LAYER_IDS=\[[^\]]*'referenceLandingLayer'/);
  // Layered Route Map Evidence V1：可行性掩码与候选航路是两个独立图层开关
  assert.match(source,/const LAYER_IDS=\[[^\]]*'layeredFeasibilityLayer'/);
  assert.match(source,/const LAYER_IDS=\[[^\]]*'layeredCandidateLayer'/);
  // 统一开关集合通过 layerIds 传入，避免同一 id 被重复绑定
  assert.match(source,/layerIds:LAYER_IDS/);
  assert.match(shellSource,/\[\.\.\.layerIds,'gridLayer'\]/);
  assert.doesNotMatch(shellSource,/\[\.\.\.layerIds,'gridLayer','referenceRoutePointLayer'\]/);
  // bindShell 只能装配一次
  assert.equal([...source.matchAll(/^bindShell\(\{/gm)].length,1,'bindShell must be called exactly once');
  // 两阶段 bootstrap：区分请求失败与前端初始化失败，且都不吞异常
  assert.match(source,/无法连接本机地图服务/);
  assert.match(source,/前端初始化失败/);
  assert.match(source,/bootstrapFailure\('前端初始化失败',exc\)/);
  assert.match(source,/bootstrapFailure\('无法连接本机地图服务',exc\)/);
  assert.match(source,/console\.error\('\[CNS Planner\] '\+message,exc\)/);
});

// ---- 真实显隐状态回归 -------------------------------------------------------
//
// 这一组断言刻意**不看** dataset 属性或按钮高亮，而是直接检查真实 DOM 节点上
// 的可见状态类：上一轮回归正是"按钮高亮变了、正文仍然从第一个分段开始"，
// 因为显隐由枚举 CSS 选择器与错误的前缀兜底承担。

/** 真实可见的一级面板（.wb-panel-active）。 */
function activePanels(root){return findAll(root,'.wb-panel-active');}

/** 真实可见的二级分段（.wb-seg-active）。 */
function activeSegments(root){return findAll(root,'.wb-seg-active');}

/** 只用真实节点断言：同一时刻严格只有 1 个面板 / 1 个分段可见。 */
function assertOnlyVisible(root,panelGroup,segmentName){
  const panels=activePanels(root);
  assert.equal(panels.length,1,`exactly one panel may be visible, got ${panels.map(node=>node.dataset.panelGroup).join(',')}`);
  assert.equal(panels[0].dataset.panelGroup,panelGroup,`visible panel must be ${panelGroup}`);
  const segments=activeSegments(root);
  if(!segmentName){
    assert.equal(segments.length,0,'a panel without segments keeps every segment hidden');
    return;
  }
  assert.equal(segments.length,1,`exactly one segment may be visible, got ${segments.map(node=>node.dataset.segName).join(',')}`);
  assert.equal(segments[0].dataset.segName,segmentName,`visible segment must be ${segmentName}`);
}

test('step 03 second-level navigation really switches the visible DOM node',()=>{
  withStubDom(document=>{
    const store={step:3,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep3,store,3,'航路规划');

    // 操作 → 起降点与OD
    assertOnlyVisible(root,'operate','op-sites');

    // 操作 → 高度与程序：只有 op-altitude 可见
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='op-altitude'));
    assertOnlyVisible(root,'operate','op-altitude');
    assert.equal(activeSegments(root)[0],findByDataset(root,'segName','op-altitude'),'the visible node is the real op-altitude element');

    // 结果 → 对比与验证：只有 res-compare 可见
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assertOnlyVisible(root,'result','res-route');
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='res-compare'));
    assertOnlyVisible(root,'result','res-compare');

    // 高级 → 规划诊断：只有 adv-diagnostics 可见
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assertOnlyVisible(root,'advanced','adv-reference');
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='adv-diagnostics'));
    assertOnlyVisible(root,'advanced','adv-diagnostics');

    // 切回来的历史选择同样落到真实 DOM 上
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='operate'));
    assertOnlyVisible(root,'operate','op-altitude');
  });
});

test('every step keeps exactly one visible panel and its own title after navigation',()=>{
  for(const [number,render] of STEP_RENDERS){
    withStubDom(document=>{
      const store={step:Number(number),tab:'operate',segs:{},scroll:0};
      const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
      const root=renderWorkflowSteps({step:{render},context:stepContext()});
      // 与 main.js 完全一致：step 对象只有 render/bind，没有 number/title/note
      controller.mount({root,step:{render}});
      const head=root.querySelector('[data-workbench-head]');
      const expected={number:head.dataset.workbenchNumber,title:head.dataset.workbenchTitle,note:head.dataset.workbenchNote};
      const read=()=>({
        number:document.getElementById('workbenchStepNumber').textContent,
        title:document.getElementById('workbenchStepTitle').textContent,
        note:document.getElementById('workbenchStepNote').textContent
      });
      assert.equal(read().number,expected.number,`step ${number} shows its own number after mount`);
      assert.equal(read().title,expected.title,`step ${number} shows its own title after mount`);
      if(expected.note)assert.equal(read().note,expected.note,`step ${number} shows its own note after mount`);

      // 逐一点击一级标签与二级分段：标题必须始终是本步骤的真实编号与名称
      for(const label of ['result','advanced','operate','advanced']){
        clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===label));
        // 真实 DOM：只有该标签面板可见，且它下面严格只有 1 个分段可见（有分段时）
        const panel=findByDataset(root,'panelGroup',label);
        const owned=findAll(panel,'[data-seg-name]').map(node=>node.dataset.segName);
        const visiblePanels=activePanels(root),visibleSegments=activeSegments(root);
        assert.equal(visiblePanels.length,1,`step ${number} shows exactly one panel after clicking ${label}`);
        assert.equal(visiblePanels[0].dataset.panelGroup,label,`step ${number} shows the ${label} panel`);
        assert.equal(visibleSegments.length,owned.length>0?1:0,`step ${number} shows exactly one segment under ${label}`);
        if(owned.length)assert.ok(owned.includes(visibleSegments[0].dataset.segName),`step ${number} visible segment belongs to ${label}`);
        assert.equal(read().number,expected.number,`step ${number} number survives clicking ${label}`);
        assert.equal(read().title,expected.title,`step ${number} title survives clicking ${label}`);
        assert.notEqual(read().number,'00',`step ${number} must never fall back to 00`);
        assert.ok(read().title.length>0,`step ${number} title must never be empty`);
        const count=segButtons(document).length;
        for(let index=0;index<count;index++){
          const button=segButtons(document)[index];
          if(button)clickNode(document,button);
          assert.equal(read().number,expected.number,`step ${number} number survives segment clicks`);
          assert.equal(read().title,expected.title,`step ${number} title survives segment clicks`);
          assert.notEqual(read().number,'00',`step ${number} must never fall back to 00`);
        }
      }
    });
  }
});

test('explicit navigation returns to the top while a re-render keeps the position',()=>{
  withStubDom(document=>{
    const store={step:3,tab:'operate',segs:{},scroll:0};
    const {controller,root}=mountRealStep(renderStep3,store,3,'航路规划');
    const body=document.getElementById('workbenchBody');
    // 让滚动范围可计算，模拟真实容器的 scrollHeight / clientHeight
    body.scrollHeight=1200;body.clientHeight=400;

    // 同一视图的 mutation/重渲染：由 main.js 保存并恢复滚动位置，mount 不得重置
    body.scrollTop=240;
    store.scroll=body.scrollTop;
    const remounted=renderWorkflowSteps({step:{render:renderStep3},context:stepContext()});
    controller.mount({root:remounted,step:{render:renderStep3}});
    body.scrollTop=Math.min(store.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
    assert.equal(body.scrollTop,240,'a re-render keeps the scroll position');
    assert.equal(store.scroll,240,'a re-render keeps the stored scroll position');

    // 显式切换一级标签：回到该任务顶部，同时把 scroll 状态归零
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(body.scrollTop,0,'switching a tab scrolls back to the top');
    assert.equal(store.scroll,0,'switching a tab resets the stored scroll');

    // 显式切换二级分段：同样回到顶部
    body.scrollTop=320;
    store.scroll=320;
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='adv-diagnostics'));
    assert.equal(body.scrollTop,0,'switching a segment scrolls back to the top');
    assert.equal(store.scroll,0,'switching a segment resets the stored scroll');
    assertOnlyVisible(remounted,'advanced','adv-diagnostics');
  });
});

test('step 05 device parameters use a two-layer card layout and keep the collect contract',()=>{
  const css=readFileSync(new URL('../cns_planner/web/css/components.css',import.meta.url),'utf8');
  const rule=/\.device-row\{[\s\S]*?\}/.exec(css);
  assert.ok(rule,'.device-row must have its own layout rule');
  // 不再依赖四列布局；名称/角色一行，两个参数各占半宽
  assert.match(rule[0],/grid-template-areas/,'.device-row must use grid-template-areas');
  assert.match(rule[0],/"name\s+role"/,'the first row is name + role');
  assert.match(rule[0],/"radius\s+mtbf"/,'the second row is radius + mtbf');
  assert.doesNotMatch(rule[0],/1\.5fr/,'.device-row must not keep the cramped four-column template');
  assert.equal((rule[0].match(/minmax\(0,1fr\)/g)||[]).length,2,'two equal columns for the numeric inputs');
  assert.match(css,/@container workbench \(max-width:340px\)[\s\S]*?"name"[\s\S]*?"role"[\s\S]*?"radius"[\s\S]*?"mtbf"/,'narrow content must degrade to a single column');
  const bodyTheme=readFileSync(new URL('../cns_planner/web/css/workbench.css',import.meta.url),'utf8');
  assert.match(bodyTheme,/container:workbench\s*\/\s*inline-size/,'the workbench body must be the query container');

  withStubDom(document=>{
    const context=stepContext();
    context.flow.devices=[
      {subsystem:'C',model:'Radio-1',device_id:'D1',radius_m:120,mtbf_h:2000,role:'primary'},
      {subsystem:'N',model:'Nav-1',device_id:'D2',radius_m:80,mtbf:1500,role:'gap'}
    ];
    const store={step:5,tab:'operate',segs:{},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    const root=renderWorkflowSteps({step:{render:renderStep5},context});
    controller.mount({root,step:{render:renderStep5}});

    const rows=findAll(root,'.device-row');
    assert.equal(rows.length,2,'every device keeps its own row');
    for(const [index,row] of rows.entries()){
      // 结构：名称 + 弱化 role + 两个半宽输入，全部在同一张卡片里
      assert.equal(findAll(row,'.device-name').length,1,`device ${index} keeps its name`);
      assert.equal(findAll(row,'.device-role').length,1,`device ${index} keeps its role badge`);
      const radius=findAll(row,'.device-field-radius');
      const mtbf=findAll(row,'.device-field-mtbf');
      assert.equal(radius.length,1,`device ${index} keeps exactly one R field`);
      assert.equal(mtbf.length,1,`device ${index} keeps exactly one MTBF field`);
      // bind/collect 契约：按索引查询的两个 data 属性必须原样保留
      assert.ok(findByDataset(row,'deviceRadius',String(index)),`device ${index} keeps data-device-radius`);
      assert.ok(findByDataset(row,'deviceMtbf',String(index)),`device ${index} keeps data-device-mtbf`);
      assert.ok(findByDataset(radius[0],'deviceRadius',String(index)),`the R input sits inside its own field`);
      assert.ok(findByDataset(mtbf[0],'deviceMtbf',String(index)),`the MTBF input sits inside its own field`);
    }
    // 值本身没有被改写（radius_m / mtbf_h / mtbf 原样输出）
    assert.equal(findByDataset(root,'deviceRadius','0').attributes.value,'120');
    assert.equal(findByDataset(root,'deviceMtbf','0').attributes.value,'2000');
    assert.equal(findByDataset(root,'deviceMtbf','1').attributes.value,'1500','mtbf fallback field is preserved');
    // 设备名称允许换行、role 弱化显示，两者都不再是表格列
    assert.match(findByDataset(root,'deviceRadius','0').parentNode.className,/device-field/,'the R input lives in a labelled field');
    assert.equal(findAll(rows[0],'.device-name')[0].textContent,'C · Radio-1','the device name keeps subsystem · model');
    assert.equal(findAll(rows[0],'.device-role')[0].textContent,'primary','the role stays available as a weak badge');
    // 按钮保留：正常两列（窄屏换行由 @container 负责）
    assert.ok(document.getElementById('saveDevices'),'save devices stays mounted');
    assert.ok(document.getElementById('planCoverage'),'run site planning stays mounted');
  });
});

// ---- Step05：任务式二级工作台 ------------------------------------------------
//
// Step05 曾经是三个超长页面。重组后每个一级标签下都是一组任务分段，
// 这一组断言锁定"任务可切换、始终只有一个分段可见、控件与 bind 契约不变"。

const STEP05_SEGMENTS={
  operate:[['cns-op-devices','设备与参数'],['cns-op-existing','已有设施'],['cns-op-candidates','候选站址']],
  result:[['cns-res-coverage','基础覆盖'],['cns-res-capability','3D与能力'],['cns-res-corridor','服务走廊'],['cns-res-gap','规划目标与缺口']],
  advanced:[['cns-adv-site','走廊站址优化'],['cns-adv-timeline','运行时间线'],['cns-adv-gapv2','保护与 Gap V2'],['cns-adv-closedloop','Legacy与闭环']]
};

/** Step05 的关键控件：既有业务 id，重组后必须一个不少。 */
const STEP05_CONTROLS=[
  'saveDevices','planCoverage','existing_cnsPath','browseExisting','importExisting',
  'candidate_sitesPath','browseCandidates','importCandidates','deriveCandidates',
  'analyzeGaps','coverage3dSpacing','evaluateCoverage3d','evaluateServiceCapability',
  'evaluateCorridor','planningObjectiveRoute','planningObjectiveSubsystem',
  'objectiveMinSatisfied','objectiveMaxDeficit','objectiveMaxUnknown','objectiveMinRedundancy',
  'objectiveMaxContinuous','planningObjectiveSource','planningObjectiveConfirmed',
  'savePlanningObjectives','evaluateCorridorGap','corridorSitePolicyConfirmed',
  'evaluateCorridorSitePlan','evaluateServiceTimeline','evaluateProtectionEnvelope',
  'gapV2Protection','evaluateGapV2','sitePolicyConfirmed','evaluateSitePlan',
  'evaluateClosedLoop','applyClosedLoop','nextStep'
];

test('step 05 declares the documented task segments',()=>{
  const html=renderStep5(stepContext());
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  const expected=Object.values(STEP05_SEGMENTS).flat().map(item=>item[0]);
  assert.deepEqual(segs.slice().sort(),expected.slice().sort(),'step 05 segment ids');
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');
  // 业务语言标签逐条锁定：段按钮的名称来自分段自身，而不是外部 metadata
  for(const [id,label] of Object.values(STEP05_SEGMENTS).flat()){
    assert.ok(html.includes('data-seg-name="'+id+'" data-seg-label="'+label+'"'),`segment ${id} keeps its business label`);
  }
});

test('step 05 task navigation keeps exactly one segment visible and every control mounted',()=>{
  withStubDom(document=>{
    const store={step:5,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep5,store,5,'CNS规划');

    // 入口：操作只显示"设备与参数"，其他任务不出现在首屏
    assertOnlyVisible(root,'operate','cns-op-devices');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),STEP05_SEGMENTS.operate.map(item=>item[0]));
    assert.deepEqual(segButtons(document).map(node=>node.textContent),STEP05_SEGMENTS.operate.map(item=>item[1]));

    for(const [tab,segments] of Object.entries(STEP05_SEGMENTS)){
      clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===tab));
      assert.equal(root.dataset.tab,tab);
      assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),segments.map(item=>item[0]),`${tab} segment buttons`);
      assert.deepEqual(segButtons(document).map(node=>node.textContent),segments.map(item=>item[1]),`${tab} segment labels`);
      for(const [id] of segments){
        clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
        assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
        assert.equal(store.segs[tab],id,`clicking ${id} stores it under ${tab}`);
        // 每次严格只有 1 个分段可见：同组其他分段与别的一级标签都不能串显
        assertOnlyVisible(root,tab,id);
      }
    }

    // 11 个分段始终挂载 DOM（3 操作 + 4 结果 + 4 高级），只切 .wb-seg-active
    const expectedSegments=Object.values(STEP05_SEGMENTS).flat().map(item=>item[0]);
    assert.deepEqual(findAll(root,'[data-seg-name]').map(node=>node.dataset.segName).sort(),expectedSegments.slice().sort(),'every segment stays mounted');
    // 全部原关键控件仍然存在，并且落在正常正文里（不是 .wb-section-head）
    const missing=STEP05_CONTROLS.filter(id=>!document.getElementById(id));
    assert.deepEqual(missing,[],`step 05 controls stay mounted: ${missing.join(', ')}`);
    for(const id of STEP05_CONTROLS){
      if(id==='nextStep')continue; // 步骤级"下一步"始终在所有面板之外，不属于任何 segment
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,`step 05 duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
  });
});

test('step 05 bind() resolves every control it queries',()=>{
  withStubDom(document=>{
    const store={step:5,tab:'operate',segs:{},scroll:0};
    mountRealStep(renderStep5,store,5,'CNS规划');
    const c={
      flow:()=>({devices:[],cns_planning_objectives:{routes:{}},corridor_site_planning_policy:{},site_planning_policy:{},closed_loop_assessment:{}}),
      mutate:()=>{},resourceAction:()=>{},setStep:()=>{},openBrowser:()=>{},
      $:id=>document.getElementById(id),actionButton:()=>{}
    };
    // 无条件访问的控件若缺席，这里会直接抛 TypeError
    bindStep5(c);
    assert.ok(document.getElementById('nextStep').onclick,'nextStep stays wired');
  });
});

test('step 05 keeps the chosen task after a re-render and returns to the top on a switch',()=>{
  withStubDom(document=>{
    const store={step:5,tab:'advanced',segs:{advanced:'cns-adv-gapv2'},scroll:0};
    const {controller,root}=mountRealStep(renderStep5,store,5,'CNS规划');
    assert.equal(root.dataset.tab,'advanced','the stored tab is restored');
    assert.equal(root.dataset.seg,'cns-adv-gapv2','the stored segment is restored');
    assertOnlyVisible(root,'advanced','cns-adv-gapv2');

    // mutation / renderWorkflow() 重新挂载：保持当前segment 与滚动位置
    const body=document.getElementById('workbenchBody');
    body.scrollHeight=1600;body.clientHeight=400;
    body.scrollTop=360;store.scroll=360;
    const remounted=renderWorkflowSteps({step:{render:renderStep5},context:stepContext()});
    controller.mount({root:remounted,step:{render:renderStep5}});
    body.scrollTop=Math.min(store.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
    assert.equal(remounted.dataset.seg,'cns-adv-gapv2','a re-render keeps the current task');
    assert.equal(body.scrollTop,360,'a re-render keeps the scroll position');
    assertOnlyVisible(remounted,'advanced','cns-adv-gapv2');

    // 显式切换一级/二级：回到该任务顶部
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(remounted.dataset.seg,'cns-res-coverage','a tab switch falls back to its first task');
    assert.equal(body.scrollTop,0,'switching a tab returns to the top');
    assertOnlyVisible(remounted,'result','cns-res-coverage');
    body.scrollTop=280;store.scroll=280;
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='cns-res-gap'));
    assert.equal(body.scrollTop,0,'switching a segment returns to the top');
    assert.equal(store.scroll,0,'switching a segment resets the stored scroll');
    assertOnlyVisible(remounted,'result','cns-res-gap');
  });
});

test('workbench navigation exposes tablist, tab and tabpanel ARIA state',()=>{
  const html=readFileSync(new URL('../cns_planner/web/index.html',import.meta.url),'utf8');
  assert.match(html,/id="workbenchTabs" role="tablist"/,'the first-level navigation is a tablist');
  assert.match(html,/id="workbenchSegs" role="tablist"/,'the second-level navigation is a tablist');
  withStubDom(document=>{
    const store={step:5,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep5,store,5,'CNS规划');
    assert.deepEqual(tabButtons(document).map(node=>node.getAttribute('role')),['tab','tab','tab']);
    assert.deepEqual(tabButtons(document).map(node=>node.getAttribute('aria-selected')),['true','false','false']);
    assert.equal(findByDataset(root,'panelGroup','operate').getAttribute('role'),'tabpanel');
    assert.equal(findByDataset(root,'panelGroup','result').getAttribute('role'),undefined,'a hidden panel must not claim tabpanel');
    // tablist 与 tab 之间的布局层不承担语义
    const group=document.getElementById('workbenchSegs').children[0];
    assert.equal(group.getAttribute('role'),'presentation');
    assert.deepEqual(segButtons(document).map(node=>node.getAttribute('role')),['tab','tab','tab']);
    assert.deepEqual(segButtons(document).map(node=>node.getAttribute('aria-selected')),['true','false','false']);

    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.deepEqual(tabButtons(document).map(node=>node.getAttribute('aria-selected')),['false','true','false']);
    assert.deepEqual(segButtons(document).map(node=>node.getAttribute('aria-selected')),['true','false','false','false']);
    assert.equal(findByDataset(root,'panelGroup','result').getAttribute('role'),'tabpanel');
    assert.equal(findByDataset(root,'panelGroup','operate').getAttribute('role'),undefined);

    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='cns-res-corridor'));
    assert.deepEqual(segButtons(document).map(node=>node.getAttribute('aria-selected')),['false','false','true','false']);
  });
});

// ---- Step04：任务式二级工作台 ------------------------------------------------
//
// Step04 曾经是三个"超长页面"。重组后每个一级标签下都是一组任务分段，
// 这一组断言锁定"任务可切换、始终只有一个分段可见、控件与 bind 契约不变"，
// 并且 Aircraft Capability / Required CNS / Ground Device Capability 三层各自独立成段。

const STEP04_SEGMENTS={
  operate:[['run-op-aircraft','飞行器能力'],['run-op-rules','飞行规则']],
  result:[['run-res-required','CNS需求'],['run-res-recommend','需求建议'],['run-res-corridor','服务走廊']],
  advanced:[['run-adv-timing','时间与场景'],['run-adv-safety','安全与耦合'],['run-adv-v3','V3 CNS评估']]
};

/** Step04 的关键控件：既有业务 id，重组后必须一个不少（条件渲染的 DAA 滑块除外）。 */
const STEP04_CONTROLS=[
  // 操作 · 飞行器能力
  'aircraftProfile','manufacturer','model','cruise','maximum','mtbf',
  // 操作 · 飞行规则
  'aircraftRoute','heightAB','heightBA','heightMode','separation','delaySensor','delayCommand',
  'directionRule','saveRules',
  // 结果 · CNS需求
  'requiredScope','saveRequiredCns',
  'cRequired','cServiceType','cTechnology','cNetworkScope','cInterfaces','cCoverage','cMaxGap',
  'cMaxLatency','cLostLink','cContinuousOutage','cCumulativeOutage','cAvailability','cRedundancy',
  'cSource','cConfirmed','cContingency',
  'nRequired','nTechnology','nCoverage','nHorizontalError','nVerticalError','nIntegrity',
  'nTimeToAlert','nDegradation','nAvailability','nRedundancy','nSource','nConfirmed','nContingency',
  'sRequired','sTargetCooperation','sSensorMode','sTechnology','sCoverage','sDetectionRange',
  'sDetectionProbability','sMaxUpdate','sTrackLoss','sAlertLatency','sAvailability','sRedundancy',
  'sSource','sConfirmed','sContingency',
  // 结果 · 需求建议（Policy JSON / diff / provenance 收进 disclosure，但控件仍挂载）
  'operationContextScope','operationContext_operation_mode','operationContext_airspace_context',
  'operationContext_uas_traffic_context','operationContext_traffic_mix',
  'operationContext_manned_traffic_density','operationContextSource','operationContextConfirmed',
  'saveOperationContext','requirementPoliciesJson','saveRequirementPolicies',
  'evaluateRequiredRecommendation','adoptRequiredRecommendation',
  // 结果 · 服务走廊
  'corridorRoute','corridorHalfWidth','corridorLower','corridorUpper','corridorSource',
  'corridorConfirmed','saveCorridorPolicy',
  // 高级 · 时间与场景
  'serviceScenarioRoute','serviceScenarioEvents','serviceScenarioConfirmed','saveServiceScenario',
  'rtDetect','rtTrack','rtProcessing','rtDecision','rtCommunication','rtReaction','rtSource',
  'rtConfirmed','saveResponseBudget','encounterRelativeSpeed','encounterManeuver',
  'encounterUncertainty','encounterSource','encounterConfirmed','saveEncounterScenario',
  'evaluateProtectionBudget','daaTracks','daaPolicy','daaCapability','daaCommand',
  'daaServiceRoute','daaBudget','saveDaaEncounter','evaluateDaaEncounter',
  // 高级 · 安全与耦合
  'safetyPolicySource','safetyPolicyConfirmed','safetyFailureCondition','safetyServiceState',
  'saveSafetyPolicy','previewSafetyEvent','safetyEventPreview','coupledCondition',
  'coupledObservations','coupledOperationalContext','previewCoupledEvent','coupledEventPreview',
  // 高级 · V3 CNS评估
  'assessV3AdoptedRoute',
  // 步骤级
  'nextStep'
];

test('step 04 declares the documented task segments',()=>{
  const html=renderStep4(stepContext());
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  const expected=Object.values(STEP04_SEGMENTS).flat().map(item=>item[0]);
  assert.deepEqual(segs.slice().sort(),expected.slice().sort(),'step 04 segment ids');
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');
  // 业务语言标签逐条锁定：段按钮的名称来自分段自身，而不是外部 metadata
  for(const [id,label] of Object.values(STEP04_SEGMENTS).flat()){
    assert.ok(html.includes('data-seg-name="'+id+'" data-seg-label="'+label+'"'),`segment ${id} keeps its business label`);
    // 第一视觉层不把工程编号（P 编号 / 算法 id）当导航名称
    assert.doesNotMatch(label,/^P\d|_v\d/,`segment ${id} must use business language`);
  }
});

test('step 04 task navigation keeps exactly one segment visible and every control mounted',()=>{
  withStubDom(document=>{
    const store={step:4,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep4,store,4,'运行规则');

    // 入口：操作只显示"飞行器能力"
    assertOnlyVisible(root,'operate','run-op-aircraft');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),STEP04_SEGMENTS.operate.map(item=>item[0]));
    assert.deepEqual(segButtons(document).map(node=>node.textContent),STEP04_SEGMENTS.operate.map(item=>item[1]));

    for(const [tab,segments] of Object.entries(STEP04_SEGMENTS)){
      clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===tab));
      assert.equal(root.dataset.tab,tab);
      assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),segments.map(item=>item[0]),`${tab} segment buttons`);
      assert.deepEqual(segButtons(document).map(node=>node.textContent),segments.map(item=>item[1]),`${tab} segment labels`);
      for(const [id] of segments){
        clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
        assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
        assert.equal(store.segs[tab],id,`clicking ${id} stores it under ${tab}`);
        // 每次严格只有 1 个分段可见：同组其他分段与别的一级标签都不能串显
        assertOnlyVisible(root,tab,id);
      }
    }

    // 8 个分段始终挂载 DOM（2 操作 + 3 结果 + 3 高级），只切 .wb-seg-active
    const expectedSegments=Object.values(STEP04_SEGMENTS).flat().map(item=>item[0]);
    assert.deepEqual(findAll(root,'[data-seg-name]').map(node=>node.dataset.segName).sort(),expectedSegments.slice().sort(),'every segment stays mounted');
    // 全部原关键控件仍然存在，并且落在正常正文里（不是 .wb-section-head）
    const missing=STEP04_CONTROLS.filter(id=>!document.getElementById(id));
    assert.deepEqual(missing,[],`step 04 controls stay mounted: ${missing.join(', ')}`);
    for(const id of STEP04_CONTROLS){
      if(id==='nextStep')continue; // 步骤级"下一步"始终在所有面板之外，不属于任何 segment
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,`step 04 duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
  });
});

test('step 04 keeps every task owning its own controls',()=>{
  withStubDom(document=>{
    const store={step:4,tab:'operate',segs:{},scroll:0};
    mountRealStep(renderStep4,store,4,'运行规则');
    const ownerOf=id=>{const node=document.getElementById(id);const seg=node&&node.closest('[data-seg-name]');return seg?seg.dataset.segName:null;};
    const expected={
      aircraftProfile:'run-op-aircraft',manufacturer:'run-op-aircraft',cruise:'run-op-aircraft',mtbf:'run-op-aircraft',
      aircraftRoute:'run-op-rules',heightAB:'run-op-rules',heightMode:'run-op-rules',separation:'run-op-rules',
      directionRule:'run-op-rules',saveRules:'run-op-rules',
      requiredScope:'run-res-required',saveRequiredCns:'run-res-required',cConfirmed:'run-res-required',sCoverage:'run-res-required',
      operationContextScope:'run-res-recommend',saveOperationContext:'run-res-recommend',
      requirementPoliciesJson:'run-res-recommend',saveRequirementPolicies:'run-res-recommend',
      evaluateRequiredRecommendation:'run-res-recommend',adoptRequiredRecommendation:'run-res-recommend',
      corridorRoute:'run-res-corridor',corridorConfirmed:'run-res-corridor',saveCorridorPolicy:'run-res-corridor',
      serviceScenarioRoute:'run-adv-timing',
      saveResponseBudget:'run-adv-timing',saveEncounterScenario:'run-adv-timing',
      evaluateProtectionBudget:'run-adv-timing',evaluateDaaEncounter:'run-adv-timing',
      saveSafetyPolicy:'run-adv-safety',previewSafetyEvent:'run-adv-safety',
      coupledCondition:'run-adv-safety',previewCoupledEvent:'run-adv-safety',
      assessV3AdoptedRoute:'run-adv-v3'
    };
    for(const [id,segment] of Object.entries(expected)){
      assert.equal(ownerOf(id),segment,`#${id} belongs to ${segment}`);
    }
    // 三个能力层级仍然各自独立成段：机载能力 ≠ Required CNS ≠ 地面设备能力/走廊
    assert.notEqual(ownerOf('aircraftProfile'),ownerOf('saveRequiredCns'),'aircraft capability and required CNS stay in separate tasks');
    assert.notEqual(ownerOf('saveRequiredCns'),ownerOf('corridorRoute'),'required CNS and corridor stay in separate tasks');
  });
});

test('step 04 bind() resolves every control it queries',()=>{
  withStubDom(document=>{
    const store={step:4,tab:'operate',segs:{},scroll:0};
    mountRealStep(renderStep4,store,4,'运行规则');
    const registered=[];
    const c={
      flow:()=>({rules:{height_mode:'different'},operational_timing:{},encounter_3d_assessment:{},aircraft_profiles:{items:[]}}),
      mutate:()=>{},resourceAction:()=>{},computeAction:()=>{},setStep:()=>{},
      $:id=>document.getElementById(id),
      actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;}
    };
    // 无条件访问的控件若缺席，这里会直接抛 TypeError
    bindStep4(c);
    assert.ok(document.getElementById('nextStep').onclick,'nextStep stays wired');
    for(const id of ['saveRules','saveRequiredCns','saveOperationContext','saveRequirementPolicies',
      'evaluateRequiredRecommendation','adoptRequiredRecommendation','saveServiceScenario','saveResponseBudget',
      'saveEncounterScenario','evaluateProtectionBudget','saveCorridorPolicy','saveSafetyPolicy',
      'previewSafetyEvent','previewCoupledEvent','saveDaaEncounter','evaluateDaaEncounter','assessV3AdoptedRoute']){
      assert.ok(registered.includes(id),`bind() must still register ${id}`);
    }
    // 按钮契约不变：只重组展示层，端点与动作名保持原样
    const source=readFileSync(new URL('../cns_planner/web/js/workflow/step04_operation.js',import.meta.url),'utf8');
    for(const path of ['/api/cns-operation-context','/api/cns-requirement-policies',
      '/api/cns-required-recommendation/evaluate','/api/cns-required-recommendation/adopt',
      '/api/operational-timing','/api/protection-envelope/evaluate',
      '/api/v3-cns-assessment/evaluate','/api/cns-service-corridor/evaluate','/api/cns/safety-policy',
      '/api/cns/events/evaluate','/api/cns/coupled-events/evaluate']){
      assert.ok(source.includes(path),`step 04 must keep the ${path} contract`);
    }
    // DAA Encounter Lab 仍由 Step04 组合进"时间与场景"任务，端点不变
    const daaSource=readFileSync(new URL('../cns_planner/web/js/workflow/daa_encounter_lab.js',import.meta.url),'utf8');
    assert.ok(daaSource.includes('/api/encounter-3d/evaluate'),'the DAA encounter lab keeps its endpoint');
    assert.ok(source.includes("c.mutate('rules'"),'saveRules keeps c.mutate(rules)');
    assert.ok(source.includes("c.mutate('required-cns'"),'saveRequiredCns keeps c.mutate(required-cns)');
  });
});

test('step 04 keeps the chosen task after a re-render and returns to the top on a switch',()=>{
  withStubDom(document=>{
    const store={step:4,tab:'advanced',segs:{advanced:'run-adv-safety'},scroll:0};
    const {controller,root}=mountRealStep(renderStep4,store,4,'运行规则');
    assert.equal(root.dataset.tab,'advanced','the stored tab is restored');
    assert.equal(root.dataset.seg,'run-adv-safety','the stored segment is restored');
    assertOnlyVisible(root,'advanced','run-adv-safety');

    // mutation / renderWorkflow() 重新挂载：保持当前任务与滚动位置
    const body=document.getElementById('workbenchBody');
    body.scrollHeight=1600;body.clientHeight=400;
    body.scrollTop=360;store.scroll=360;
    const remounted=renderWorkflowSteps({step:{render:renderStep4},context:stepContext()});
    controller.mount({root:remounted,step:{render:renderStep4}});
    body.scrollTop=Math.min(store.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
    assert.equal(remounted.dataset.seg,'run-adv-safety','a re-render keeps the current task');
    assert.equal(body.scrollTop,360,'a re-render keeps the scroll position');
    assertOnlyVisible(remounted,'advanced','run-adv-safety');

    // 显式切换一级/二级：回到该任务顶部
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(remounted.dataset.seg,'run-res-required','a tab switch falls back to its first task');
    assert.equal(body.scrollTop,0,'switching a tab returns to the top');
    assertOnlyVisible(remounted,'result','run-res-required');
    body.scrollTop=280;store.scroll=280;
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='run-res-corridor'));
    assert.equal(body.scrollTop,0,'switching a segment returns to the top');
    assert.equal(store.scroll,0,'switching a segment resets the stored scroll');
    assertOnlyVisible(remounted,'result','run-res-corridor');

    // 切回高级：该 tab 之前选过的任务必须恢复
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(remounted.dataset.seg,'run-adv-safety','a previously chosen task is restored');
    assertOnlyVisible(remounted,'advanced','run-adv-safety');
  });
});

// ---- Step02：环境建模工作台 -------------------------------------------------
//
// Step02 曾经是"一页三块"（操作 / 结果 / 高级各一大段）。重组后每个一级标签下
// 都是一组任务分段，这一组断言锁定：
//  - 操作 2 段 / 结果 2 段 / 高级 2 段，逐个可切且严格只有 1 段可见；
//  - 全部既有 DOM id 一个不少（无重复 id），bind() 无条件查询的控件都能命中；
//  - 数据映射卡只转印 flow 现有状态：stale 保持 stale、未计算保持未计算；
//  - 专题浏览只切 UI 任务与地图配色，不自动改工作区、网格、缩放或图层；
//  - 工作区 / 网格层级 / Risk V2 / 高度层的 API 与语义契约保持不变。

const STEP02_SEGMENTS={
  operate:[['env-op-workspace','工作区范围'],['env-op-grid','标准网格与建筑环境']],
  result:[['env-res-mapping','数据映射'],['env-res-theme','专题浏览']],
  advanced:[['env-adv-risk','风险框架'],['env-adv-altitude','高度层']]
};

/** 专题清单：8 个基础专题 + riskV2ThemeOptions()（V2 因子 / V2 域 / Legacy V1）。 */
const STEP02_THEME_VALUES=[
  'none','population','terrain','building_density','building_p95','building_max','traffic_exposure','conflict_exposure',
  ...riskV2ThemeOptions().map(item=>item[0])
];

/** Step02 的关键控件：既有业务 id，重组后必须一个不少。 */
const STEP02_CONTROLS=[
  // 操作 · 工作区范围
  'drawWorkspace','clearWorkspace','saveWorkspace',
  // 操作 · 标准网格与建筑环境
  'workspaceGridLevel',
  // 结果 · 专题浏览
  'gridOutlineToggle','gridThemeNone','gridPopulationTheme','gridTerrainTheme',
  // 高级 · 风险框架
  'evaluateRiskV2',
  // 高级 · 高度层
  'altitudeLayerId','altitudeLayerName','altitudeReference','altitudeNominal','altitudeLower','altitudeUpper',
  'altitudeLayerSource','altitudeLayerConfirmed','saveAltitudeLayer'
];

/** 与 step02_workspace.js 的 themeId() 一致：基础专题沿用稳定的固定 id。 */
function step02ThemeId(value){
  const ids={none:'gridThemeNone',population:'gridPopulationTheme',terrain:'gridTerrainTheme'};
  return ids[value]||'gridTheme-'+String(value).replace(/[^A-Za-z0-9_-]/g,'-');
}

/** 一份完整的 Step02 flow 替身：只包含 render 真正读取的字段。 */
function step02Flow(overrides={}){
  return {
    steps:{'2':true},
    workspace:{bbox:[122,29.9,122.2,30.1],area_km2:12.5,health:{
      population:{status:'passed'},airspace:{status:'passed'},terrain:{status:'passed',message:'FABDEM 已确认 EGM2008 orthometric'},
      terrain_dtm:{status:'passed'},buildings:{status:'passed'},building_grid:{status:'passed'},loaded_layer_count:4}},
    grid:{status:'passed',level:8,count:10,preferred_level:8,coarsened:false},
    grid_attributes:{
      population:{status:'passed',value_status:'quantity',full_count:3,partial_count:1,missing_count:2,outside_count:0},
      terrain:{status:'passed',count:10,covered_count:9},
      airspace:{status:'passed',count:10,hit_count:4},
      buildings:{status:'passed',count:10,covered_count:7},
      traffic:{status:'passed',count:10,covered_count:2},
      conflict:{status:'passed',count:10,covered_count:1}
    },
    grid_risk:{status:'passed',algorithm_id:'risk-model-v1-relative-index',algorithm_version:'1.1',data_completeness:.8},
    grid_risk_v2:{status:'pending_confirmation',risk_semantics:'relative_engineering_index',data_completeness:0},
    risk_policy_v2:{status:'pending_confirmation',parameter_status:'no_default_production_risk_weights',domains:{}},
    risk_framework_v2_readiness:{status:'pending_confirmation',factors:{},domains:{}},
    spatial_3d:{altitude_layers:[
      {altitude_layer_id:'L-120',name:'巡航层',status:'confirmed',nominal_altitude_m:120,lower_altitude_m:100,upper_altitude_m:150,vertical_reference:'egm2008_orthometric'},
      {altitude_layer_id:'L-PENDING',name:'待确认层',status:'pending_confirmation',nominal_altitude_m:null,lower_altitude_m:60,upper_altitude_m:80,vertical_reference:'unknown'}
    ]},
    ...overrides
  };
}

function mountStep02(document,{tab='operate',segs={},flow=step02Flow()}={}){
  const context=stepContext();
  context.flow=flow;
  const store={step:2,tab,segs,scroll:0};
  const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
  const root=renderWorkflowSteps({step:{render:renderStep2},context});
  controller.mount({root,step:{number:2,title:'环境建模',note:''}});
  return {controller,root,store,context};
}

test('step 02 declares the documented task segments',()=>{
  const html=renderStep2(stepContext());
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  const expected=Object.values(STEP02_SEGMENTS).flat().map(item=>item[0]);
  assert.deepEqual(segs.slice().sort(),expected.slice().sort(),'step 02 segment ids');
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');
  for(const [id,label] of Object.values(STEP02_SEGMENTS).flat()){
    assert.ok(html.includes('data-seg-name="'+id+'" data-seg-label="'+label+'"'),`segment ${id} keeps its business label`);
    // 第一视觉层不把工程编号 / 算法 id 当导航名称
    assert.doesNotMatch(label,/^[A-Za-z]|_v\d|\d+_/,`segment ${id} must use business language`);
  }
  // 操作 2 段 / 结果 2 段 / 高级 2 段
  assert.deepEqual(STEP02_SEGMENTS.operate.map(item=>item[0]),['env-op-workspace','env-op-grid']);
  assert.deepEqual(STEP02_SEGMENTS.result.map(item=>item[0]),['env-res-mapping','env-res-theme']);
  assert.deepEqual(STEP02_SEGMENTS.advanced.map(item=>item[0]),['env-adv-risk','env-adv-altitude']);
});

test('step 02 task navigation keeps exactly one segment visible and every control mounted',()=>{
  withStubDom(document=>{
    const {root,store}=mountStep02(document);

    // 入口：操作只显示"工作区范围"
    assertOnlyVisible(root,'operate','env-op-workspace');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),STEP02_SEGMENTS.operate.map(item=>item[0]));
    assert.deepEqual(segButtons(document).map(node=>node.textContent),STEP02_SEGMENTS.operate.map(item=>item[1]));

    for(const [tab,segments] of Object.entries(STEP02_SEGMENTS)){
      clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===tab));
      assert.equal(root.dataset.tab,tab);
      assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),segments.map(item=>item[0]),`${tab} segment buttons`);
      assert.deepEqual(segButtons(document).map(node=>node.textContent),segments.map(item=>item[1]),`${tab} segment labels`);
      for(const [id] of segments){
        clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
        assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
        assert.equal(store.segs[tab],id,`clicking ${id} stores it under ${tab}`);
        // 每次严格只有 1 个分段可见：同组其他分段与别的一级标签都不能串显
        assertOnlyVisible(root,tab,id);
      }
    }

    // 6 个分段始终挂载 DOM（2 操作 + 2 结果 + 2 高级），只切 .wb-seg-active
    const expectedSegments=Object.values(STEP02_SEGMENTS).flat().map(item=>item[0]);
    assert.deepEqual(findAll(root,'[data-seg-name]').map(node=>node.dataset.segName).sort(),expectedSegments.slice().sort(),'every segment stays mounted');

    // 全部既有控件仍然存在，并且落在正常正文里（不是 .wb-section-head）
    const missing=STEP02_CONTROLS.filter(id=>!document.getElementById(id));
    assert.deepEqual(missing,[],`step 02 controls stay mounted: ${missing.join(', ')}`);
    for(const id of STEP02_CONTROLS){
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
    assert.ok(document.getElementById('nextStep'),'#nextStep stays mounted outside the task sections');

    // 无重复 id：每个 id 在整步内只出现一次
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,`step 02 duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);

    // 专题清单：8 个基础专题 + riskV2ThemeOptions()，每个都是独立的互斥 radio
    for(const value of STEP02_THEME_VALUES){
      const node=document.getElementById(step02ThemeId(value));
      assert.ok(node,`theme radio for ${value} must be mounted`);
      assert.equal(node.attributes.value,value,`theme radio ${value} keeps its value`);
      assert.equal(node.attributes.type,'radio',`theme ${value} must be an exclusive radio`);
      assert.equal(node.attributes.name,'gridThemeMode',`theme ${value} must join the gridThemeMode group`);
    }
  });
});

test('step 02 mapping cards transcribe the existing flow state without recomputing it',()=>{
  withStubDom(document=>{
    const {root}=mountStep02(document,{tab:'result',segs:{result:'env-res-mapping'},flow:step02Flow({
      grid_attributes:{
        population:{status:'stale',value_status:'partial',full_count:0,partial_count:1,missing_count:2,outside_count:4},
        terrain:{status:'not_calculated'},
        airspace:{status:'missing_data',count:10},
        buildings:{status:'passed',count:10,covered_count:7},
        traffic:{},
        conflict:{status:'unknown'}
      }
    })});
    assertOnlyVisible(root,'result','env-res-mapping');
    const segment=findByDataset(root,'segName','env-res-mapping');
    const cards=findAll(segment,'.metric-card').map(card=>({
      label:findAll(card,'.metric-label')[0].textContent,
      value:findAll(card,'.metric-value')[0].textContent,
      note:findAll(card,'.metric-note')[0].textContent
    }));
    const cardOf=label=>cards.find(card=>card.label===label);
    for(const label of ['人口映射','地形 DEM 映射','低空空域映射','建筑环境映射','交通暴露映射','冲突暴露映射']){
      assert.ok(cardOf(label),`mapping card "${label}" must exist`);
    }
    // 取值原样透出：stale / not_calculated / missing_data / unknown 都不被改写成 passed
    assert.equal(cardOf('人口映射').value,'已失效','a stale population mapping stays stale');
    // 人口映射不再只显示 value_status：覆盖统计始终随卡片给出（含 full/partial/missing/outside）。
    assert.equal(cardOf('人口映射').note,'已覆盖 1 / 7 格（14%） · full 0 / partial 1 / missing 2 / outside 4','population counts are shown as coverage statistics');
    assert.equal(cardOf('地形 DEM 映射').value,'未计算','an untouched terrain mapping stays not_calculated');
    assert.equal(cardOf('低空空域映射').value,'缺少数据','a missing airspace mapping stays missing_data');
    assert.equal(cardOf('建筑环境映射').value,'通过','a passed building mapping is mapped as-is');
    assert.equal(cardOf('建筑环境映射').note,'已覆盖 7 / 10 格（70%） · 未判定 3 格 · 既有建筑网格映射','the building mapping keeps its existing counts and states its basis');
    assert.equal(cardOf('交通暴露映射').value,'未计算','an empty traffic mapping is never faked as passed');
    assert.equal(cardOf('冲突暴露映射').value,'证据不足/尚无法判断','unknown is exposed as-is');
    // 工作区摘要只读现有 health：面积 / 数据状态 / 已加载图层
    const summary=findAll(segment,'.metric-grid').flatMap(grid=>findAll(grid,'b')).map(node=>node.textContent);
    assert.deepEqual(summary.slice(0,4),['12.5 km²','通过','通过','4'],'the workspace summary reads the existing health values');
    // 未保存工作区时保持空状态，同时映射卡仍然显示"未计算"，不伪造数据
    assert.ok(findAll(segment,'.empty-note').length===0,'a saved workspace shows no empty note');
  });

  withStubDom(document=>{
    const {root}=mountStep02(document,{tab:'result',segs:{result:'env-res-mapping'},flow:step02Flow({workspace:null,grid_attributes:{}})});
    const segment=findByDataset(root,'segName','env-res-mapping');
    assert.ok(findAll(segment,'.empty-note').some(node=>node.textContent.includes('尚未保存工作区')),'an unsaved workspace explains why there is no mapping');
    const values=findAll(segment,'.metric-value').map(node=>node.textContent);
    // BUG-POP-001 追加 Population NoData 语义两张卡：未确认时如实显示 not_configured / 未提供，
    // 绝不把"没有确认"渲染成已确认，也不影响其余六张映射卡的状态词。
    assert.deepEqual(values,['未计算','未计算','未计算','未计算','未计算','未计算','not_configured','未提供'],'every unmapped layer stays not_calculated and the unconfirmed NoData semantics is reported as-is');
    const labels=findAll(segment,'.metric-label').map(node=>node.textContent);
    assert.ok(labels.includes('Population NoData 语义'),'the NoData semantics card lives beside the population mapping card');
    assert.ok(labels.includes('来源与证据'),'source / evidence are visibly reported even when unset');
  });
});

test('step 02 theme browser switches only the UI task and the map theme',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step02_workspace.js',import.meta.url),'utf8');
  // 专题与网格开关只由用户操作驱动：render 不做任何自动切换
  assert.equal((source.match(/setGridTheme/g)||[]).length,1,'setGridTheme is only called from the radio change handler');
  assert.equal((source.match(/setGridOutline/g)||[]).length,1,'setGridOutline is only called from the checkbox handler');
  assert.doesNotMatch(source,/setZoom|zoomTo|fitLonLatBbox|setLayer\(|layerIds/,'主题切换不得自动改地图缩放、图层或视野');
  // 两列专题按钮：窄容器由既有 workbench 容器查询降成一列
  const css=readFileSync(new URL('../cns_planner/web/css/components.css',import.meta.url),'utf8');
  assert.match(css,/\.grid-theme-options\{display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/,'专题按钮保持两列');
  const containerRule=/@container workbench \(max-width:340px\)\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(containerRule,'the existing workbench container query must stay');
  assert.match(containerRule[1],/\.grid-theme-options,[\s\S]*?\.env-metric-cards\{grid-template-columns:minmax\(0,1fr\)\}/,'窄容器下专题按钮降成一列');

  withStubDom(document=>{
    const {root}=mountStep02(document,{tab:'result',segs:{result:'env-res-theme'}});
    assertOnlyVisible(root,'result','env-res-theme');
    // 当前主题是唯一勾选项，其余专题都不预勾选
    assert.ok('checked' in document.getElementById('gridThemeNone').attributes,'the active theme stays the only checked radio');
    for(const value of STEP02_THEME_VALUES.slice(1)){
      assert.equal(document.getElementById(step02ThemeId(value)).attributes.checked,undefined,`${value} must not be pre-checked`);
    }
    // bind 只把用户操作映射到显示状态：不触发工作区、网格或任何业务动作
    // 桩 DOM 的 document.querySelectorAll 默认返回空，这里按真实语义补一层最小实现，
    // 以便验证专题 radio 与网格开关真正接到了显示状态上。
    document.querySelectorAll=selector=>selector==='[name="gridThemeMode"]'
      ?STEP02_THEME_VALUES.map(value=>document.getElementById(step02ThemeId(value)))
      :findAll(document.body,selector);
    const calls=[];
    const c={
      setGridTheme:value=>calls.push(['theme',value]),setGridOutline:value=>calls.push(['outline',value]),
      setStep:()=>calls.push(['step']),startWorkspace:()=>calls.push(['start']),
      clearWorkspace:()=>calls.push(['clear']),saveWorkspace:()=>calls.push(['save']),
      resourceAction:()=>calls.push(['resource']),panelError:()=>{},
      $:id=>document.getElementById(id),actionButton:()=>{}
    };
    bindStep2(c);
    document.getElementById('gridTerrainTheme').onchange({target:{checked:true,value:'terrain'}});
    assert.deepEqual(calls,[['theme','terrain']],'勾选专题只改变地图配色');
    document.getElementById('gridOutlineToggle').onchange({target:{checked:false}});
    assert.deepEqual(calls,[['theme','terrain'],['outline',false]],'网格开关只改 display.outline');
  });
});

test('step 02 keeps the chosen task after a re-render and returns to the top on a switch',()=>{
  withStubDom(document=>{
    const store={step:2,tab:'advanced',segs:{advanced:'env-adv-altitude'},scroll:0};
    const {controller,root}=mountRealStep(renderStep2,store,2,'环境建模');
    assert.equal(root.dataset.tab,'advanced','the stored tab is restored');
    assert.equal(root.dataset.seg,'env-adv-altitude','the stored segment is restored');
    assertOnlyVisible(root,'advanced','env-adv-altitude');

    // mutation / renderWorkflow() 重新挂载：保持当前任务与滚动位置
    const body=document.getElementById('workbenchBody');
    body.scrollHeight=1600;body.clientHeight=400;
    body.scrollTop=360;store.scroll=360;
    const remounted=renderWorkflowSteps({step:{render:renderStep2},context:stepContext()});
    controller.mount({root:remounted,step:{render:renderStep2}});
    body.scrollTop=Math.min(store.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
    assert.equal(remounted.dataset.seg,'env-adv-altitude','a re-render keeps the current task');
    assert.equal(body.scrollTop,360,'a re-render keeps the scroll position');
    assertOnlyVisible(remounted,'advanced','env-adv-altitude');

    // 显式切换一级/二级：回到该任务顶部
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(remounted.dataset.seg,'env-res-mapping','a tab switch falls back to its first task');
    assert.equal(body.scrollTop,0,'switching a tab returns to the top');
    assertOnlyVisible(remounted,'result','env-res-mapping');
    body.scrollTop=280;store.scroll=280;
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='env-res-theme'));
    assert.equal(body.scrollTop,0,'switching a segment returns to the top');
    assert.equal(store.scroll,0,'switching a segment resets the stored scroll');
    assertOnlyVisible(remounted,'result','env-res-theme');

    // 切回高级：该 tab 之前选过的任务必须恢复
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(remounted.dataset.seg,'env-adv-altitude','a previously chosen task is restored');
    assertOnlyVisible(remounted,'advanced','env-adv-altitude');
  });
});

test('step 02 keeps the workspace, grid, risk V2 and altitude contracts',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step02_workspace.js',import.meta.url),'utf8');
  // API path 与 payload 保持原样
  for(const path of ['/api/grid-risk-v2/evaluate','/api/spatial-3d/altitude-layer/delete']){
    assert.ok(source.includes(path),`step 02 must keep the ${path} contract`);
  }
  assert.match(source,/\/api\/spatial-3d\/altitude-layer'/,'the altitude layer write keeps its explicit endpoint');
  assert.match(source,/nominal_altitude_m:optionalNumber\('altitudeNominal'\)/,'nominal is read from the explicit input only');
  assert.doesNotMatch(source,/value="(80|100|120|150)"/,'no default real altitude may ship');
  // 既有网格层级语义与专题入口不变
  assert.match(source,/L8（建筑环境直接映射）/);
  assert.match(source,/L7（建筑网格 unsupported）/);
  assert.match(source,/L6（建筑网格 unsupported）/);
  assert.match(source,/riskV2ThemeOptions\(\)/);
  assert.match(source,/populationDisplayLabel\(attributes\.population\)/);
  // 垂向基准不猜、不补默认高度
  assert.match(source,/系统不猜垂向基准，也不提供任何默认真实高度/);
  assert.match(source,/EGM2008 orthometric/);

  withStubDom(document=>{
    const {root}=mountStep02(document,{tab:'advanced',segs:{advanced:'env-adv-risk'}});
    // Risk Framework V2 面板（含 evaluateRiskV2）始终挂载，工程解释可折叠
    assertOnlyVisible(root,'advanced','env-adv-risk');
    assert.ok(document.getElementById('evaluateRiskV2'),'#evaluateRiskV2 stays mounted inside the risk task');
    assert.ok(findAll(root,'.algorithm-detail').length>=1,'the long engineering explanation lives in a disclosure');
    // 高度层列表：缺 nominal 的高度层保持待工程确认，绝不自动补值
    const altitude=findByDataset(root,'segName','env-adv-altitude');
    const rows=findAll(altitude,'.list-row').map(row=>({name:findAll(row,'b')[0].textContent,note:findAll(row,'small').map(node=>node.textContent).join(' | ')}));
    const pending=rows.find(row=>row.name==='待确认层');
    assert.ok(pending,'the pending altitude layer stays listed');
    assert.match(pending.note,/nominal 未配置（待工程确认）/,'a missing nominal stays pending_confirmation');
    assert.match(pending.note,/60–80 m/,'the explicit bounds are still shown');
    assert.match(pending.note,/unknown/,'the vertical reference is never guessed');
    assert.equal(findAll(altitude,'[data-delete-altitude-layer]').length,2,'every configured layer keeps its delete control');

    // bind() 的每个静态查询都必须命中真实挂载的 DOM（否则点击即抛 TypeError）
    const queried=new Set();
    for(const match of source.matchAll(/c\.\$\('([A-Za-z_][A-Za-z0-9_]*)'\)/g))queried.add(match[1]);
    for(const match of source.matchAll(/c\.actionButton\('([A-Za-z_][A-Za-z0-9_]*)'/g))queried.add(match[1]);
    const missing=[...queried].filter(id=>!document.getElementById(id));
    assert.deepEqual(missing,[],`step 02 bind() queries missing controls: ${missing.join(', ')}`);
    const registered=[];
    const c={$:id=>document.getElementById(id),actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;},
      clearWorkspace:()=>{},saveWorkspace:()=>{},remapPopulation:()=>{},
      resourceAction:()=>{},panelError:()=>{},setGridTheme:()=>{},setGridOutline:()=>{}};
    bindStep2(c);
    // 本轮新增三项：保存 / 撤回 Population NoData 语义，以及 population-only remap。
    assert.deepEqual(registered,['clearWorkspace','saveWorkspace','evaluateRiskV2','savePopulationNodata','revokePopulationNodata','remapPopulation','saveAltitudeLayer'],'bind() registers exactly the existing actions');
    for(const id of registered)assert.ok(document.getElementById(id).onclick,`#${id} keeps its handler`);
  });
});

// ---- Step06：评审 / 决策 / 交付工作台 ---------------------------------------
//
// Step06 曾经是三个"超长页面"。重组后每个一级标签下都是一组任务分段，
// 这一组断言锁定"任务可切换、始终只有一个分段可见、控件与 bind 契约不变"，
// 并且锁定三条业务边界：
//  - Select ≠ Confirm ≠ Apply：confirmed 前 applyPlan 必须 disabled；
//  - 比较卡与完整矩阵来自同一份 comparison_matrix，不引入 overall score / rank；
//  - 报告 stale 与"无 plan 不能生成正式报告"的既有语义不变。

const STEP06_SEGMENTS={
  operate:[['review-op-overview','评审概览'],['review-op-compare','方案比较'],['review-op-edit','方案编辑'],['review-op-confirm','确认与应用']],
  result:[['review-res-status','状态总览'],['review-res-report','报告与交付']],
  advanced:[['review-adv-requirement','需求依据'],['review-adv-proposal','布站提案证据']]
};

/** Step06 的关键控件：既有业务 id，重组后必须一个不少。 */
const STEP06_CONTROLS=[
  // 操作 · 评审概览 / 方案比较
  'initializePlanReview','evaluatePlanVariant',
  // 操作 · 方案编辑
  'variantInclude','variantExclude','variantName','createPlanVariant',
  // 操作 · 确认与应用
  'confirmWithoutObjectives','planDecisionReason','confirmPlan','applyPlan',
  // 结果 · 报告与交付
  'previewPlanningReport','generatePlanningReport',
  'downloadReportHtml','downloadReportPdf','downloadReportPackage','saveAll'
];

/** 一份完整的 Step06 flow 替身：只包含 render 真正读取的字段。 */
function step06Flow(overrides={}){
  return {
    project:{name:'测试项目'},
    workspace:null,
    operational_routes:[],
    aircraft:null,
    rules:null,
    coverage:null,
    review:{risks:{},overall_status:'pending_confirmation',overall_pass:false},
    result_statuses:{},
    cns_plan_review:{},
    confirmed_cns_plan:{},
    cns_planning_reports:{},
    ...overrides
  };
}

/** 与 step06_review.js 的 number() 一致：有限数保留一位小数，否则 —。 */
function numberField(value){return Number.isFinite(value)?value.toFixed(1):'—';}

/** 一个已评价的方案：comparison_matrix 同时喂给紧凑卡与完整矩阵。 */
const STEP06_MATRIX=[
  {route_id:'R0001',subsystem:'C',objective_status:'objectives_met',service:{voxel_counts:{satisfied:12,confirmed_deficit:0,unknown:1}},redundancy:{voxel_counts:{satisfied:9,confirmed_deficit:2,unknown:0}},total_confirmed_deficit_projection_m:120.5,max_continuous_deficit_projection_m:40,unknown_voxel_ids:['V-1']},
  {route_id:'R0001',subsystem:'N',objective_status:'objectives_unknown',service:{voxel_counts:{satisfied:5,confirmed_deficit:3,unknown:4}},redundancy:{voxel_counts:{satisfied:6,confirmed_deficit:1,unknown:2}},total_confirmed_deficit_projection_m:300,max_continuous_deficit_projection_m:150.25,unknown_voxel_ids:['V-2','V-3']}
];
const STEP06_VARIANT={
  variant_id:'PV-1',name:'Baseline',source:'baseline',selected_action_ids:['A1'],
  status:'evaluated',evaluation:{
    confirmation_gate:{status:'ready_for_confirmation'},comparison_matrix:STEP06_MATRIX,
    action_summary:{explicit_costs_by_unit:{},reuse_class_counts:{}}
  }
};
function step06ReviewFlow(overrides={}){
  return step06Flow({
    cns_plan_review:{status:'current',selected_variant_id:'PV-1',variants:[STEP06_VARIANT]},
    ...overrides
  });
}

test('step 06 declares the documented task segments',()=>{
  const html=renderStep6(stepContext());
  const segs=[...html.matchAll(/data-seg-name="([a-z0-9-]+)"/g)].map(match=>match[1]);
  const expected=Object.values(STEP06_SEGMENTS).flat().map(item=>item[0]);
  assert.deepEqual(segs.slice().sort(),expected.slice().sort(),'step 06 segment ids');
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');
  for(const [id,label] of Object.values(STEP06_SEGMENTS).flat()){
    assert.ok(html.includes('data-seg-name="'+id+'" data-seg-label="'+label+'"'),`segment ${id} keeps its business label`);
    // 第一视觉层不把工程编号（P 编号 / 算法 id）当导航名称
    assert.doesNotMatch(label,/^P\d|_v\d/,`segment ${id} must use business language`);
  }
  // 操作 4 段 / 结果 2 段 / 高级 2 段
  assert.deepEqual(STEP06_SEGMENTS.operate.map(item=>item[0]),['review-op-overview','review-op-compare','review-op-edit','review-op-confirm']);
  assert.deepEqual(STEP06_SEGMENTS.result.map(item=>item[0]),['review-res-status','review-res-report']);
  assert.deepEqual(STEP06_SEGMENTS.advanced.map(item=>item[0]),['review-adv-requirement','review-adv-proposal']);
});

test('step 06 task navigation keeps exactly one segment visible and every control mounted',()=>{
  withStubDom(document=>{
    const context=stepContext();
    context.flow=step06ReviewFlow();
    const store={step:6,tab:'operate',segs:{},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    const root=renderWorkflowSteps({step:{render:renderStep6},context});
    controller.mount({root,step:{number:6,title:'方案评审',note:''}});

    // 入口：操作只显示"评审概览"
    assertOnlyVisible(root,'operate','review-op-overview');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),STEP06_SEGMENTS.operate.map(item=>item[0]));
    assert.deepEqual(segButtons(document).map(node=>node.textContent),STEP06_SEGMENTS.operate.map(item=>item[1]));

    for(const [tab,segments] of Object.entries(STEP06_SEGMENTS)){
      clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab===tab));
      assert.equal(root.dataset.tab,tab);
      assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),segments.map(item=>item[0]),`${tab} segment buttons`);
      assert.deepEqual(segButtons(document).map(node=>node.textContent),segments.map(item=>item[1]),`${tab} segment labels`);
      for(const [id] of segments){
        clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg===id));
        assert.equal(root.dataset.seg,id,`clicking ${id} selects it`);
        assert.equal(store.segs[tab],id,`clicking ${id} stores it under ${tab}`);
        // 每次严格只有 1 个分段可见：同组其他分段与别的一级标签都不能串显
        assertOnlyVisible(root,tab,id);
      }
    }

    // 8 个分段始终挂载 DOM（4 操作 + 2 结果 + 2 高级），只切 .wb-seg-active
    const expectedSegments=Object.values(STEP06_SEGMENTS).flat().map(item=>item[0]);
    assert.deepEqual(findAll(root,'[data-seg-name]').map(node=>node.dataset.segName).sort(),expectedSegments.slice().sort(),'every segment stays mounted');
    // 全部原关键控件仍然存在，并且落在正常正文里（不是 .wb-section-head）
    const missing=STEP06_CONTROLS.filter(id=>!document.getElementById(id));
    assert.deepEqual(missing,[],`step 06 controls stay mounted: ${missing.join(', ')}`);
    for(const id of STEP06_CONTROLS){
      const node=document.getElementById(id);
      assert.equal(node.closest('.wb-section-head'),null,`#${id} must not sit inside .wb-section-head`);
      assert.ok(node.closest('.wb-section'),`#${id} must sit inside a .wb-section body`);
    }
    const ids=findAll(root,'[id]').map(node=>node.id).filter(Boolean);
    assert.equal(new Set(ids).size,ids.length,`step 06 duplicate ids: ${ids.filter((id,index)=>ids.indexOf(id)!==index).join(', ')}`);
    // 导出链接与两个 GeoJSON 端点原样保留
    const links=findAll(root,'.button-link').map(node=>node.attributes.href);
    for(const path of ['/api/export/project','/api/export/routes','/api/export/sites']){
      assert.ok(links.includes(path),`step 06 keeps the ${path} export link`);
    }
  });
});

test('step 06 comparison cards and the full objective matrix share one matrix source',()=>{
  withStubDom(document=>{
    const context=stepContext();
    context.flow=step06ReviewFlow();
    const store={step:6,tab:'operate',segs:{operate:'review-op-compare'},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    const root=renderWorkflowSteps({step:{render:renderStep6},context});
    controller.mount({root,step:{number:6,title:'方案评审',note:''}});
    assertOnlyVisible(root,'operate','review-op-compare');

    // 第一视觉层：每个 route × subsystem 一张紧凑比较卡
    const segment=findByDataset(root,'segName','review-op-compare');
    const cards=findAll(segment,'.comparison-card');
    assert.equal(cards.length,STEP06_MATRIX.length,'one comparison card per comparison_matrix row');
    // 桩 DOM 只记录节点自身的文本，因此逐节点取文本（浏览器 textContent 会递归聚合）
    const itemText=card=>findAll(card,'.comparison-item').flatMap(item=>findAll(item,'span').concat(findAll(item,'b'),findAll(item,'small')).map(node=>node.textContent));
    for(const [index,row] of STEP06_MATRIX.entries()){
      const card=cards[index];
      const head=findAll(card,'b')[0].textContent;
      assert.ok(head.includes(row.route_id),`comparison card ${index} shows route ${row.route_id}`);
      assert.ok(head.includes(row.subsystem),`comparison card ${index} shows subsystem ${row.subsystem}`);
      const items=itemText(card);
      assert.equal(items.length,6,'each card keeps label + triple + note for service and redundancy');
      // 每张卡都显式标注 满足/缺口/未知 两个维度，不合并成单一分数
      assert.ok(items.includes('服务 满足/缺口/未知'),'the card labels the service satisfied/deficit/unknown triple');
      assert.ok(items.includes('冗余 满足/缺口/未知'),'the card labels the redundancy satisfied/deficit/unknown triple');
      const service=row.service.voxel_counts;
      assert.ok(items.includes([service.satisfied||0,service.confirmed_deficit||0,service.unknown||0].join('/')),`card ${index} prints the raw service counts`);
      const redundancy=row.redundancy.voxel_counts;
      assert.ok(items.includes([redundancy.satisfied||0,redundancy.confirmed_deficit||0,redundancy.unknown||0].join('/')),`card ${index} prints the raw redundancy counts`);
      const notes=items.join(' | ');
      assert.ok(notes.includes(numberField(row.total_confirmed_deficit_projection_m)),`card ${index} shows the total deficit projection`);
      assert.ok(notes.includes(numberField(row.max_continuous_deficit_projection_m)),`card ${index} shows the max continuous deficit`);
      assert.ok(notes.includes(String((row.unknown_voxel_ids||[]).length)),`card ${index} shows the unknown voxel count`);
    }

    // 完整 Objective Comparison Matrix 仍然保留，收进 wbDisclosure 的 <details>
    assert.equal(findAll(segment,'.algorithm-detail').length,1,'the full matrix lives in exactly one disclosure');
    const headers=findAll(segment,'th').map(node=>node.textContent);
    assert.deepEqual(headers,['航路','C/N/S','规划目标','服务 满足/缺口/未知','冗余 满足/缺口/未知','缺口总长 m','最大连续缺口 m','证据不足体素']);
    const rows=findAll(segment,'tr');
    assert.equal(rows.length,STEP06_MATRIX.length+1,'header row plus one row per matrix row');
    const headerCells=findAll(rows[0],'th').length;
    for(const [index,row] of STEP06_MATRIX.entries()){
      const cells=findAll(rows[index+1],'td').map(node=>node.textContent);
      assert.equal(cells.length,headerCells,'the full matrix keeps all eight columns');
      assert.equal(cells[0],row.route_id,'the full matrix shows the same route id');
      assert.equal(cells[1],row.subsystem,'the full matrix shows the same subsystem');
      assert.equal(cells[3],[row.service.voxel_counts.satisfied||0,row.service.voxel_counts.confirmed_deficit||0,row.service.voxel_counts.unknown||0].join('/'),'the full matrix prints the same service counts');
      assert.equal(cells[5],numberField(row.total_confirmed_deficit_projection_m),'the full matrix prints the same total deficit');
      assert.equal(cells[7],String((row.unknown_voxel_ids||[]).length),'the full matrix prints the same unknown voxel count');
    }
    // 同一份数据：紧凑卡与完整矩阵都只来自 selected.evaluation.comparison_matrix
    const source=readFileSync(new URL('../cns_planner/web/js/workflow/step06_review.js',import.meta.url),'utf8');
    assert.equal((source.match(/comparison_matrix/g)||[]).length,2,'both surfaces read the same comparison_matrix field');
    assert.doesNotMatch(source,/automatic_score|overall_score|automatic_rank|winner|ranking/i,'no hidden score/rank/winner logic may be introduced');
  });
});

test('step 06 keeps Select, Confirm and Apply separate and disabled before the gate opens',()=>{
  withStubDom(document=>{
    const store={step:6,tab:'operate',segs:{operate:'review-op-confirm'},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    // A. 未初始化：没有可选方案，Confirm/Apply 都不可用
    const initial=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:step06Flow()}});
    controller.mount({root:initial,step:{number:6,title:'方案评审',note:''}});
    const disabled=id=>document.getElementById(id).attributes.disabled!==undefined;
    assert.equal(disabled('applyPlan'),true,'applyPlan is disabled without a confirmed plan');
    assert.equal(document.getElementById('applyPlan').dataset.applyGate,'blocked');
    assert.equal(disabled('confirmPlan'),true,'confirmPlan is disabled without a selected variant');
    assert.equal(document.getElementById('confirmPlan').dataset.gate,'not_evaluated');
    assert.equal(disabled('createPlanVariant'),true,'no variant can be cloned before one is selected');

    // B. 已选择且门禁 ready：Confirm 可用，但还没确认 → Apply 仍不可用
    const selected=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:step06ReviewFlow()}});
    controller.mount({root:selected,step:{number:6,title:'方案评审',note:''}});
    assert.equal(disabled('confirmPlan'),false,'confirmPlan opens when the gate is ready_for_confirmation');
    assert.equal(disabled('createPlanVariant'),false,'a user variant can be cloned from the selection');
    assert.equal(disabled('applyPlan'),true,'confirm and apply must not be treated as equivalent');
    assert.equal(document.getElementById('confirmPlan').dataset.gate,'ready_for_confirmation');
    // 知情确认必须由用户显式勾选：系统从不预勾选，也不替用户确认
    assert.equal(document.getElementById('confirmWithoutObjectives').attributes.checked,undefined,'the acknowledgement is never pre-checked');

    // B2. 未配置规划目标：勾选框可用，但确认理由未填前不预勾选
    const ackFlow=step06ReviewFlow({
      cns_plan_review:{status:'current',selected_variant_id:'PV-1',variants:[{...STEP06_VARIANT,evaluation:{
        confirmation_gate:{status:'objectives_not_configured',requires_confirm_without_objectives_acknowledgement:true},
        comparison_matrix:STEP06_MATRIX,action_summary:{explicit_costs_by_unit:{}}
      }}]}
    });
    const ack=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:ackFlow}});
    controller.mount({root:ack,step:{number:6,title:'方案评审',note:''}});
    assert.equal(disabled('confirmWithoutObjectives'),false,'the acknowledgement opens when the gate demands it');
    assert.equal(disabled('confirmPlan'),false,'the acknowledgement path may still be confirmed');
    assert.equal(document.getElementById('confirmPlan').dataset.gate,'objectives_not_configured');

    // C. confirmed + current：Apply 打开，且从不与 Confirm 并排
    const confirmedFlow=step06ReviewFlow({
      confirmed_cns_plan:{status:'confirmed',plan_id:'CP-1',current_applicability:'current',application:{status:'not_applied'}}
    });
    const confirmed=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:confirmedFlow}});
    controller.mount({root:confirmed,step:{number:6,title:'方案评审',note:''}});
    assert.equal(disabled('applyPlan'),false,'a current confirmed plan may be applied');
    assert.equal(document.getElementById('applyPlan').dataset.applyGate,'ready');
    const confirmRow=document.getElementById('confirmPlan').parentNode;
    const applyRow=document.getElementById('applyPlan').parentNode;
    assert.notEqual(confirmRow,applyRow,'Confirm and Apply must not share one button container');
    assert.ok(confirmRow.classList.contains('review-block'),'Confirm sits in its own step block');
    assert.ok(applyRow.classList.contains('review-block'),'Apply sits in its own step block');
  });
});

test('step 06 report gate, stale semantics and bind contract stay unchanged',()=>{
  const source=readFileSync(new URL('../cns_planner/web/js/workflow/step06_review.js',import.meta.url),'utf8');
  // 端点与动作名保持原样
  for(const path of ['/api/cns-plan-review/initialize','/api/cns-plan-review/evaluate','/api/cns-plan-review/select',
    '/api/cns-plan-review/variant','/api/cns-plan-review/confirm','/api/cns-plan-review/apply',
    '/api/cns-planning-report/generate']){
    assert.ok(source.includes(path),`step 06 must keep the ${path} contract`);
  }
  // 报告生成条件与 stale 语义不变
  assert.match(source,/\['confirmed','applied'\]\.includes\(confirmed\.status\)/,'only a confirmed/applied plan may generate a formal report');
  assert.match(source,/stale_current_project/,'the stale report state keeps its own marker');
  assert.match(source,/该报告对应旧项目状态，可继续下载/,'a stale report stays downloadable and is clearly flagged');

  withStubDom(document=>{
    const store={step:6,tab:'result',segs:{result:'review-res-report'},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    const disabled=id=>document.getElementById(id).attributes.disabled!==undefined;
    // 无 plan：只能预览草稿，正式报告 disabled
    const empty=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:step06Flow()}});
    controller.mount({root:empty,step:{number:6,title:'方案评审',note:''}});
    assert.equal(disabled('generatePlanningReport'),true,'a formal report needs a confirmed plan');
    assert.equal(document.getElementById('generatePlanningReport').dataset.reportGate,'blocked');
    assert.equal(disabled('previewPlanningReport'),false,'the draft preview stays available without a plan');
    assert.equal(disabled('downloadReportHtml'),true,'there is nothing to download before a report exists');

    // stale 报告：仍可下载，不自动覆盖
    const staleFlow=step06Flow({
      confirmed_cns_plan:{status:'confirmed',plan_id:'CP-1',current_applicability:'stale',application:{status:'not_applied'}},
      cns_planning_reports:{status:'stale',active_report_id:'RPT-1',records:[{report_id:'RPT-1',current_applicability:'stale_current_project',generated_at:'2026-01-01T00:00:00'}]}
    });
    const stale=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow:staleFlow}});
    controller.mount({root:stale,step:{number:6,title:'方案评审',note:''}});
    assert.equal(disabled('downloadReportHtml'),false,'a stale report stays downloadable');
    assert.equal(disabled('downloadReportPdf'),false,'a stale report keeps its PDF download');
    assert.equal(disabled('downloadReportPackage'),false,'a stale report keeps its package download');
    const reportSegment=findByDataset(stale,'segName','review-res-report');
    const flagged=findAll(reportSegment,'.inline-error').some(node=>node.textContent.includes('该报告对应旧项目状态'));
    assert.ok(flagged,'a stale report keeps its explicit stale warning');

    // bind() 只查询真实挂载的控件：无条件读取若缺席会抛 TypeError
    const registered=[];
    const c={
      flow:()=>step06ReviewFlow(),
      mutate:()=>{},resourceAction:()=>{},
      $:id=>document.getElementById(id),
      actionButton:(id,handler)=>{registered.push(id);const node=document.getElementById(id);if(node)node.onclick=handler;}
    };
    bindStep6(c);
    assert.deepEqual(registered,['saveAll','initializePlanReview','evaluatePlanVariant','createPlanVariant','confirmPlan','applyPlan',
      'previewPlanningReport','generatePlanningReport','downloadReportHtml','downloadReportPdf','downloadReportPackage',
      'evaluateRouteSafetyEvidenceV2']);
    for(const button of document.querySelectorAll('.selectPlanVariant'))assert.ok(button.onclick,'every variant card keeps its select handler');
  });
});

test('step 06 status overview groups the existing statuses without recomputing them',()=>{
  withStubDom(document=>{
    const flow=step06ReviewFlow({
      confirmed_cns_plan:{status:'confirmed',plan_id:'CP-1',current_applicability:'current',application:{status:'not_applied'}},
      result_statuses:{routes:'passed',coverage:'stale',cns_corridor_gap_assessment:'passed',cns_plan_review:'passed',report:'stale'},
      review:{risks:{environment:{status:'passed'},technical:{status:'failed'},life:{status:'not_calculated'},property:{status:'passed'}},overall_status:'pending_confirmation',overall_pass:false}
    });
    const store={step:6,tab:'result',segs:{result:'review-res-status'},scroll:0};
    const controller=createWorkbench({getState:()=>store,setState:value=>Object.assign(store,value)});
    const root=renderWorkflowSteps({step:{render:renderStep6},context:{state:baseState(),flow}});
    controller.mount({root,step:{number:6,title:'方案评审',note:''}});
    assertOnlyVisible(root,'result','review-res-status');
    const segment=findByDataset(root,'segName','review-res-status');
    // 按业务组展示：规划输入 / CNS 规划 / 风险 / 方案与交付 + 总体状态
    const titles=[...findAll(segment,'h3'),...findAll(segment,'b')].map(node=>node.textContent);
    for(const group of ['规划输入','CNS 规划','风险','方案与交付','总体状态']){
      assert.ok(titles.includes(group),`status overview keeps the "${group}" group`);
    }
    // 只映射现有 status：取值原样透出，不重新计算任何结论
    const rows=findAll(segment,'.review-row').map(row=>({
      label:findAll(row,'span')[0].textContent,
      badge:findAll(row,'span.flow-badge').length?findAll(row,'span.flow-badge')[0].textContent:'',
      value:findAll(row,'span')[1]?findAll(row,'span')[1].textContent:''
    }));
    const valueOf=label=>rows.find(row=>row.label===label);
    assert.equal(valueOf('运行航路').badge,'通过','a passed result status is mapped as-is');
    assert.equal(valueOf('基础覆盖').badge,'已失效','a stale result status stays stale');
    assert.equal(valueOf('走廊空间缺口评估').badge,'通过','the corridor gap status is mapped as-is');
    assert.equal(valueOf('技术风险').badge,'失败','the risk group reads flow.review.risks');
    assert.equal(valueOf('规划报告').badge,'已失效','the report status is mapped as-is');
    assert.equal(valueOf('总体状态').badge,'待确认','the overall status is not recomputed');
    assert.equal(valueOf('总体通过').value,'否','overall_pass is shown as a plain yes/no');
    // 未计算的条目显示"未计算"，而不是被省略或伪造成通过
    assert.equal(valueOf('3D 几何覆盖').badge,'未计算','an untouched result stays not_calculated');
  });
});

test('step 06 keeps the chosen task after a re-render and returns to the top on a switch',()=>{
  withStubDom(document=>{
    const context=stepContext();
    context.flow=step06ReviewFlow();
    const store={step:6,tab:'advanced',segs:{advanced:'review-adv-proposal'},scroll:0};
    const {controller,root}=mountRealStep(renderStep6,store,6,'方案评审');
    assert.equal(root.dataset.tab,'advanced','the stored tab is restored');
    assert.equal(root.dataset.seg,'review-adv-proposal','the stored segment is restored');
    assertOnlyVisible(root,'advanced','review-adv-proposal');

    // mutation / renderWorkflow() 重新挂载：保持当前任务与滚动位置
    const body=document.getElementById('workbenchBody');
    body.scrollHeight=1600;body.clientHeight=400;
    body.scrollTop=360;store.scroll=360;
    const remounted=renderWorkflowSteps({step:{render:renderStep6},context});
    controller.mount({root:remounted,step:{render:renderStep6}});
    body.scrollTop=Math.min(store.scroll,Math.max(0,body.scrollHeight-body.clientHeight));
    assert.equal(remounted.dataset.seg,'review-adv-proposal','a re-render keeps the current task');
    assert.equal(body.scrollTop,360,'a re-render keeps the scroll position');
    assertOnlyVisible(remounted,'advanced','review-adv-proposal');

    // 显式切换一级/二级：回到该任务顶部
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(remounted.dataset.seg,'review-res-status','a tab switch falls back to its first task');
    assert.equal(body.scrollTop,0,'switching a tab returns to the top');
    assertOnlyVisible(remounted,'result','review-res-status');
    body.scrollTop=280;store.scroll=280;
    clickNode(document,segButtons(document).find(node=>node.dataset.wbSeg==='review-res-report'));
    assert.equal(body.scrollTop,0,'switching a segment returns to the top');
    assert.equal(store.scroll,0,'switching a segment resets the stored scroll');
    assertOnlyVisible(remounted,'result','review-res-report');

    // 切回高级：该 tab 之前选过的任务必须恢复
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='advanced'));
    assert.equal(remounted.dataset.seg,'review-adv-proposal','a previously chosen task is restored');
    assertOnlyVisible(remounted,'advanced','review-adv-proposal');
  });
});

test('step 05 device list scrolls inside its own box instead of overflowing',()=>{
  const css=readFileSync(new URL('../cns_planner/web/css/components.css',import.meta.url),'utf8');
  const rule=/\.device-list\{([^}]*)\}/.exec(css);
  assert.ok(rule,'.device-list must keep its own rule');
  const body=rule[1];
  assert.match(body,/max-height:250px/,'the device list keeps its bounded height');
  assert.match(body,/overflow-y:auto/,'the device list must scroll vertically inside the box');
  assert.match(body,/overflow-x:hidden/,'the device list must not overflow horizontally');
  assert.match(body,/overscroll-behavior:contain/,'device scrolling must not chain to the workbench body');
});

export {StubNode,installStubDom,renderWorkflowSteps,renderStep1,createWorkbench};
