import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clampTaskPanelPosition, createTaskCenter, panelHtml, readTaskPanelUiState,
  TASK_PANEL_UI_STORAGE_KEY, writeTaskPanelUiState,
} from '../cns_planner/web/js/tasks.js';
import {
  LAST_EXPLICIT_PROJECT_KEY, readLastExplicitProject, recordExplicitProject,
  restoreLastExplicitProject,
} from '../cns_planner/web/js/state/explicit_project.js';
import {syncSourcePathInputs, SOURCE_PATH_INPUTS} from '../cns_planner/web/js/sources/source_inputs.js';
import {towerSourceCompatibilityNotice} from '../cns_planner/web/js/sources/source_center.js';
import {createShellActions} from '../cns_planner/web/js/workflow/shell_actions.js';
import {render as renderStep01} from '../cns_planner/web/js/workflow/step01_project.js';

function memoryStorage(initial={}) {
  const map=new Map(Object.entries(initial));
  return {getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,String(value)),removeItem:key=>map.delete(key)};
}

class FakeElement {
  constructor(id='',document=null){this.id=id;this.ownerDocument=document;this.style={};this.dataset={};this.hidden=false;this.children=[];this.className='';this.textContent='';}
  set innerHTML(value){
    this._innerHTML=value;
    if(this.id!=='cnsTaskCenter')return;
    const doc=this.ownerDocument;
    for(const id of ['cnsTaskPanel','cnsTaskToggle','cnsTaskPanelContent','cnsTaskErrorArea','cnsTaskList'])doc.nodes.set(id,new FakeElement(id,doc));
    const head=new FakeElement('',doc);head.className='cns-task-panel-head';doc.head=head;
  }
  get innerHTML(){return this._innerHTML||'';}
  querySelector(selector){return selector==='.cns-task-panel-head'?this.ownerDocument.head:null;}
  getBoundingClientRect(){
    return {left:parseFloat(this.style.left)||700,top:parseFloat(this.style.top)||500,
      width:this.dataset.collapsed==='true'?180:300,height:this.dataset.collapsed==='true'?50:200};
  }
  setPointerCapture(id){this.captured=id;} releasePointerCapture(id){this.released=id;}
}

function fakeDocument(){
  const nodes=new Map();
  const listeners={};
  const view={innerWidth:1000,innerHeight:700,addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener:name=>delete listeners[name]};
  const doc={nodes,defaultView:view,body:{appendChild(node){nodes.set(node.id,node);}},
    createElement(){return new FakeElement('',doc);},getElementById:id=>nodes.get(id)||null,
    querySelectorAll(){return [];}};
  doc.listeners=listeners;
  return doc;
}

test('任务 panel content 统一包含 list/starters/note/error，收起与展开覆盖全部内容',()=>{
  const html=panelHtml([],[{task_type:'x',task_name:'测试'}]);
  const content=html.slice(html.indexOf('id="cnsTaskPanelContent"'),html.lastIndexOf('</div>'));
  for(const marker of ['cnsTaskList','cns-task-starters','cns-task-note','cnsTaskErrorArea'])assert.match(content,new RegExp(marker));
  const storage=memoryStorage(),document=fakeDocument(),center=createTaskCenter({storage,document,fetch:async()=>({ok:true,json:async()=>[]})});
  center.mount();
  const toggle=document.getElementById('cnsTaskToggle'),contentNode=document.getElementById('cnsTaskPanelContent');
  toggle.onclick();assert.equal(contentNode.hidden,true);assert.equal(toggle.textContent,'展开');
  toggle.onclick();assert.equal(contentNode.hidden,false);assert.equal(toggle.textContent,'收起');
});

test('polling remount 后保持 collapsed，且 localStorage 可跨控制器恢复',async()=>{
  const storage=memoryStorage();writeTaskPanelUiState({collapsed:true,x:null,y:null},storage);
  const document=fakeDocument(),fetch=async url=>({ok:true,json:async()=>url.endsWith('/catalog')?{items:[]}:[]});
  const center=createTaskCenter({storage,document,fetch});
  await center.refresh();assert.equal(document.getElementById('cnsTaskPanelContent').hidden,true);
  const originalHead=document.head;
  await center.refresh();assert.equal(document.getElementById('cnsTaskPanelContent').hidden,true);
  assert.equal(document.head,originalHead,'任务数据未变化时 polling 不得销毁 pointer/focus 节点');
  assert.equal(readTaskPanelUiState(storage).collapsed,true);
});

test('drag 更新/保存位置，缓存位置恢复，toggle pointerdown 不启动拖动',()=>{
  const storage=memoryStorage();writeTaskPanelUiState({collapsed:false,x:100,y:120},storage);
  const document=fakeDocument(),center=createTaskCenter({storage,document,fetch:async()=>({ok:true,json:async()=>[]})});
  const host=center.mount(),head=document.head;
  assert.equal(host.style.left,'100px');assert.equal(host.style.top,'120px');
  head.onpointerdown({button:0,pointerId:1,clientX:10,clientY:20,target:{closest:()=>null},preventDefault(){}});
  head.onpointermove({clientX:70,clientY:90});head.onpointerup({pointerId:1});
  assert.deepEqual(readTaskPanelUiState(storage),{collapsed:false,x:160,y:190});
  head.dataset.dragging='false';
  head.onpointerdown({button:0,pointerId:2,clientX:1,clientY:1,target:{closest:selector=>selector==='button'?{}:null}});
  assert.equal(head.dataset.dragging,'false');
});

test('viewport clamp 不允许整个 panel 离开视口，窄视口仍保留 header',()=>{
  assert.deepEqual(clampTaskPanelPosition({x:950,y:690},{width:1000,height:700},{width:300,height:200}),{x:700,y:500});
  assert.deepEqual(clampTaskPanelPosition({x:50,y:50},{width:120,height:40},{width:180,height:50}),{x:0,y:0});
});

const explicitState=(directory='D:/projects/A',automatic=false,name='A')=>({
  project_storage:{directory,automatic,file:directory+'/project_state.json'},workflow:{project:{name},revision:1},paths:{},layers:[],population:{},terrain:{},terrain_dtm:{},data_health:{},defaults:{engineering_parameters:{}}
});

test('Save As 成功后缓存 explicit project；失败前不写缓存',async()=>{
  const storage=memoryStorage(),oldStorage=globalThis.localStorage,oldDocument=globalThis.document;
  globalThis.localStorage=storage;globalThis.document={body:{contains:()=>true}};
  try{
    const actions=createShellActions({getNode:()=>({disabled:false,value:'A'}),panelError:()=>{}});
    const state=explicitState('',true),fresh=explicitState('D:/projects/Saved',false,'Saved');
    const ok=await actions.saveProject({projectDir:'D:/projects/Saved',name:'Saved'},{api:async url=>url==='/api/state'?fresh:{project:{name:'Saved'}},state,flow:state.workflow,saveAs:async()=>{},applyFlow:()=>{},applyState:()=>{}});
    assert.equal(ok,true);assert.equal(readLastExplicitProject(storage).directory,'D:/projects/Saved');
  }finally{globalThis.localStorage=oldStorage;globalThis.document=oldDocument;}
});

test('Open 成功状态可立即缓存；服务器 active project 优先并同步缓存',async()=>{
  const storage=memoryStorage({[LAST_EXPLICIT_PROJECT_KEY]:JSON.stringify({directory:'D:/old',project_name:'old'})});
  const opened=explicitState('D:/projects/Open',false,'Open');recordExplicitProject(opened,storage,()=> '2026-09-27T00:00:00Z');
  let calls=0;const result=await restoreLastExplicitProject(opened,{storage,api:async()=>{calls++;}});
  assert.equal(result.state,opened);assert.equal(result.attempted,false);assert.equal(calls,0);
  assert.equal(readLastExplicitProject(storage).directory,'D:/projects/Open');
});

test('server automatic + cached explicit project 只恢复一次；失败不循环且不猜其它项目',async()=>{
  const storage=memoryStorage();storage.setItem(LAST_EXPLICIT_PROJECT_KEY,JSON.stringify({directory:'D:/chosen',project_name:'Chosen',recorded_at:'x'}));
  const automatic=explicitState('',true,'automatic'),restored=explicitState('D:/chosen',false,'Chosen');
  let calls=[];const success=await restoreLastExplicitProject(automatic,{storage,api:async(url,options)=>{calls.push(JSON.parse(options.body).project_dir);return restored;}});
  assert.equal(success.restored,true);assert.deepEqual(calls,['D:/chosen']);
  calls=[];const failed=await restoreLastExplicitProject(automatic,{storage,api:async(url,options)=>{calls.push(JSON.parse(options.body).project_dir);throw Error('missing');}});
  assert.equal(failed.state,automatic);assert.equal(failed.attempted,true);assert.deepEqual(calls,['D:/chosen']);
});

test('projectPath 使用服务器 project_storage.directory 立即回填',()=>{
  const state=explicitState('D:/projects/Current',false,'Current');
  const html=renderStep01({state,flow:{...state.workflow,steps:{'1':false},algorithm_selection:{},algorithm_catalog:[],grid_attributes:{},project:{name:'Current'},grid:{},workspace:null}});
  assert.match(html,/id="projectPath" value="D:\/projects\/Current"/);
});

test('Source Center 集中回填全部来源并显示 tower 缺路径兼容说明',()=>{
  const nodes={};for(const id of Object.values(SOURCE_PATH_INPUTS))nodes[id]={value:'old'};
  const paths=Object.fromEntries(Object.keys(SOURCE_PATH_INPUTS).map(role=>[role,'C:/data/'+role]));
  syncSourcePathInputs(paths,id=>nodes[id]);
  assert.equal(nodes.towersPath.value,'C:/data/towers');
  assert.equal(Object.keys(SOURCE_PATH_INPUTS).length,10);
  assert.equal(towerSourceCompatibilityNotice({paths:{},workflow:{towers:{count:7}}}),'已导入 7 个铁塔站址，但原始数据源路径未登记；请重新选择原始铁塔文件。');
  assert.equal(towerSourceCompatibilityNotice({paths:{towers:'x'},workflow:{towers:{count:7}}}), '');
});
