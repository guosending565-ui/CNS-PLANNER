import test from 'node:test';
import assert from 'node:assert/strict';
import {planningEvidenceItemLine, renderPlanningEvidence} from '../cns_planner/web/js/workflow/planning_evidence.js';

async function harness(){
  const m=await import('../cns_planner/web/js/workflow/step01_project.js?case='+Math.random());
  const nodes=Object.fromEntries(['projectPath','openProject','projectOpenStatus','saveProject','projectName','searchPlace','placeSearch'].map(id=>[id,{value:'',dataset:{},isConnected:true}]));
  globalThis.document={getElementById:id=>nodes[id]||null,querySelectorAll:()=>[]};
  let result={ok:true,identity:'p@471',facts:{name:'历史项目',revision:471}}, calls=0, pending;
  const c={$:id=>nodes[id]||null,projectOpenStep:()=>({identity:'p@471',directory:'D:/project-a'}),openProject:async(d,{onStage})=>{calls++;onStage('activating');if(pending)await pending;return result;}};
  m.setProjectDirectoryDraft('D:/project-a');m.bind(c);
  return {m,nodes,c,setResult:r=>result=r,setPending:p=>pending=p,calls:()=>calls};
}
test('成功与already_active收口到opened，重绘保留名称和revision，目录变更清除成功',async()=>{
 const h=await harness();await h.nodes.openProject.onclick();
 assert.equal(h.nodes.projectOpenStatus.dataset.state,'opened');
 assert.match(h.nodes.projectOpenStatus.innerHTML,/历史项目/);
 assert.match(h.nodes.projectOpenStatus.innerHTML,/471/);
 assert.match(h.m.projectOpenPanel({draft:'D:/project-a'}),/data-state="opened"/);
 h.setResult({ok:true,already_active:true,identity:'p@471',facts:{name:'历史项目',revision:471}});
 await h.nodes.openProject.onclick();assert.equal(h.nodes.projectOpenStatus.dataset.state,'opened');
 h.nodes.projectPath.value='D:/project-b';h.nodes.projectPath.oninput();
 assert.equal(h.nodes.projectOpenStatus.dataset.state,'selected');
 assert.doesNotMatch(h.nodes.projectOpenStatus.innerHTML,/历史项目/);
 h.m.setProjectDirectoryDraft('D:/project-c');assert.match(h.m.projectOpenPanel({draft:'D:/project-c'}),/data-state="selected"/);
 h.setResult({ok:false,error:new Error('打开失败')});await h.nodes.openProject.onclick();
 assert.equal(h.nodes.projectOpenStatus.dataset.state,'failed');assert.doesNotMatch(h.nodes.projectOpenStatus.innerHTML,/历史项目/);
});
test('在途优先于旧成功，activated重绘与重复点击保护保留',async()=>{
 const h=await harness();let finish;h.setPending(new Promise(r=>finish=r));
 const opened=h.nodes.openProject.onclick();assert.equal(h.nodes.projectOpenStatus.dataset.state,'activated');
 await h.nodes.openProject.onclick();assert.equal(h.calls(),1);finish();await opened;
 assert.equal(h.nodes.projectOpenStatus.dataset.state,'opened');
});
test('opening优先，成功后再次失败不保留成功事实',async()=>{
 const h=await harness();await h.nodes.openProject.onclick();
 let finish;h.c.openProject=async()=>{await new Promise(r=>finish=r);throw new Error('读取快照失败');};
 const pending=h.nodes.openProject.onclick();assert.equal(h.nodes.projectOpenStatus.dataset.state,'opening');
 finish();await pending;assert.equal(h.nodes.projectOpenStatus.dataset.state,'failed');
 assert.doesNotMatch(h.nodes.projectOpenStatus.innerHTML,/历史项目/);
 h.nodes.projectPath.value='D:/other';h.nodes.projectPath.oninput();assert.equal(h.nodes.projectOpenStatus.dataset.state,'selected');
});
test('历史依据与字段语义安全去掉行内标记，保留换行且仅转义一次',()=>{
 const text='**专用通信网络**\n`设备 failsafe 事实` <img src=x onerror="alert(1)"> & 已披露 $&';
 const item={evidence_id:'e1',source_type:'engineering_assumption',statement:text,reason:'**工程依据**',report_disclosure:'`非厂家事实`'};
 const html=planningEvidenceItemLine(item);
 assert.doesNotMatch(html,/\*\*|`|<img/);assert.match(html,/专用通信网络<br>设备 failsafe 事实/);
 assert.match(html,/&lt;img/);assert.match(html,/&amp; 已披露/);assert.doesNotMatch(html,/&amp;lt;/);
 assert.equal(item.statement,text);
 const flow={planning_evidence:{field_status:[{field:'x',status:'evidence_required',semantics:text}]},planning_evidence_fields:{fields:{x:{scopes:['operation'],semantics:text}}},operational_routes:[{route_id:'R0005'}]};
 const rendered=renderPlanningEvidence(flow);assert.doesNotMatch(rendered,/\*\*|`|<img/);assert.match(rendered,/专用通信网络<br>设备 failsafe 事实/);assert.match(rendered,/\$&amp;/);
});
