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
  /** 只沿父链匹配，不遍历子树；语义足够支撑 document 级点击委托。 */
  closest(selector){
    const parsed=parseSelector(selector);
    for(let node=this;node;node=node.parentNode){
      if(node.tagName&&matchesSelector(node,parsed))return node;
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
  // 右侧工作台的固定骨架：一级标签容器、二级分段容器、滚动区
  document.register('workbenchTabs','tabbar');
  document.register('workbenchSegs');
  document.register('workbenchBody');
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
  for(const required of ['op-sites','op-candidates','op-operational','op-altitude','res-route','res-feasibility','res-compare','adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
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

    // 结果 → res-route，3 个二级按钮
    clickNode(document,tabButtons(document).find(node=>node.dataset.wbTab==='result'));
    assert.equal(root.dataset.tab,'result');
    assert.equal(root.dataset.seg,'res-route','result defaults to the first segment');
    assert.deepEqual(segButtons(document).map(node=>node.dataset.wbSeg),
      ['res-route','res-feasibility','res-compare']);

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
  for(const required of ['op-sites','op-candidates','op-operational','op-altitude','res-route','res-feasibility','res-compare','adv-reference','adv-legacy','adv-experiment','adv-diagnostics','adv-profile']){
    assert.ok(segs.includes(required),`missing segment ${required}`);
  }
  assert.equal(new Set(segs).size,segs.length,'segment ids must be unique');
  // 每个二级分段都挂在一个一级面板下，并且自描述名称与 id 成对出现
  assert.equal((html.match(/data-seg-set="(?!none)/g)||[]).length,3,'three panels carry segments');
  // 自描述：data-seg-label（单数）与 data-seg-name 成对出现，data-seg-labels 是面板级汇总
  assert.equal((html.match(/data-seg-label="/g)||[]).length,segs.length,'every segment declares its own label');  // 分段可见性由 CSS 承载：每个分段 id 必须有对应规则（显式列表或通用兜底）
  const css=readFileSync(new URL('../cns_planner/web/css/components.css',import.meta.url),'utf8');
  for(const id of segs)assert.match(css,new RegExp('data-seg-name="'+id+'"|data-seg-name\\^'),`${id} has no visibility rule`);
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

test('step 03 exposes all twelve segments through real click navigation',()=>{
  withStubDom(document=>{
    const store={step:3,tab:'operate',segs:{},scroll:0};
    const {root}=mountRealStep(renderStep3,store,3,'航路规划');
    const expected={
      operate:['op-sites','op-candidates','op-operational','op-altitude'],
      result:['res-route','res-feasibility','res-compare'],
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
    // 标题占位被搬走后不应留下重复标题，也不能带走业务内容
    assert.equal(root.querySelector('[data-workbench-head]'),null,'the head placeholder is removed');
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

export {StubNode,installStubDom,renderWorkflowSteps,renderStep1,createWorkbench};
