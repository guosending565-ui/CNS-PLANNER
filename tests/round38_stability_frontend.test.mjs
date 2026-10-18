import test from 'node:test';
import assert from 'node:assert/strict';
import {createApiClient} from '../cns_planner/web/js/api/client.js';
import {createE2eConfirmation} from '../cns_planner/web/js/sources/e2e_confirmation.js';
import {createTaskCenter,rememberTaskId,setSessionToken} from '../cns_planner/web/js/tasks.js';
import {candidateModel} from '../cns_planner/web/js/workflow/layered_theta_v2.js';
import {comparisonUnknownVoxelCount} from '../cns_planner/web/js/workflow/step06_review.js';

test('missing revision header must not poison the next write with revision zero', async()=>{
  const original=globalThis.fetch;const calls=[];
  globalThis.fetch=async(url,options)=>{calls.push(options);return {ok:true,headers:{get:n=>n==='content-type'?'application/json':null},json:async()=>({workflow:{revision:478}})};};
  try {const api=createApiClient(()=> 'token',()=>478);await api('/api/state');await api('/write',{method:'POST'});assert.equal(calls[1].headers['X-CNS-Revision'],'478');}
  finally {globalThis.fetch=original;}
});

function harness(){
  const controls=Object.fromEntries(['value','evidence','ack','submit','close','error','identity'].map(n=>['[data-'+n+']',{value:'',checked:false,disabled:n==='submit',textContent:''}]));
  const handlers={};let closed=false;let revision=478,fp='old',status='verified',fail=null,delay=null;const posts=[];
  const identity=()=>({revision,project:{name:'test'},project_storage:{file:'isolated.json'},audit:{status,sha256:'sha',version_fingerprint:fp,declared_crs:'EPSG:4326'}});
  const dialog={style:{},set innerHTML(v){},querySelector:s=>controls[s],addEventListener:(n,f)=>{handlers[n]=f;},showModal(){},close(){closed=true;},remove(){}};
  globalThis.document={createElement:()=>dialog,body:{append(){}}};
  const api=async(url,options)=>{if(options){posts.push(JSON.parse(options.body));if(delay)await delay;if(fail)throw fail;return {};}
    if(url==='/api/state')return {workflow:{revision,source_audits:{items:{buildings:identity().audit}}},project_storage:{file:'isolated.json'}};
    return identity();};
  return {controls,handlers,posts,api,set revision(v){revision=v;},set fp(v){fp=v;},set status(v){status=v;},set fail(v){fail=v;},set delay(v){delay=v;},get closed(){return closed;},authorize(){controls['[data-value]'].value='EPSG:4326';controls['[data-evidence]'].value='keep this evidence';controls['[data-ack]'].checked=true;handlers.input();},submit(){return controls['[data-submit]'].onclick();}};
}

for(const change of ['revision','source'])test('CRS '+change+' change preserves draft and requires a second authorization',async()=>{
  const h=harness();await createE2eConfirmation({api:h.api}).open('buildings');h.authorize();
  if(change==='revision')h.revision=479;else h.fp='new';
  await h.submit();assert.equal(h.posts.length,0);assert.equal(h.controls['[data-ack]'].checked,false);
  assert.equal(h.controls['[data-evidence]'].value,'keep this evidence');assert.equal(h.controls['[data-value]'].value,'EPSG:4326');
  assert.match(h.controls['[data-error]'].textContent,/→/);h.authorize();await h.submit();
  assert.equal(h.posts.length,1);assert.equal(h.posts[0].expected_revision,change==='revision'?479:478);
  assert.equal(h.posts[0].source_fingerprint,change==='source'?'new':'old');assert.equal(h.closed,true);
});

test('CRS request in flight cannot be submitted twice, even after another input event',async()=>{
  const h=harness();await createE2eConfirmation({api:h.api}).open('buildings');h.authorize();let release;h.delay=new Promise(r=>release=r);
  const first=h.submit();await new Promise(r=>setImmediate(r));h.handlers.input();const second=h.submit();release();await Promise.all([first,second]);assert.equal(h.posts.length,1);
});

for(const code of [400,409,0])test('CRS error '+code+' retains draft and labels the failure',async()=>{
  const h=harness();await createE2eConfirmation({api:h.api}).open('buildings');h.authorize();h.fail=Object.assign(Error(code===400?'项目 revision 已变化，请重新核对':'failed'),{status:code});await h.submit();
  assert.equal(h.closed,false);assert.equal(h.controls['[data-evidence]'].value,'keep this evidence');
  assert.match(h.controls['[data-error]'].textContent,code===400?/业务版本冲突/:code===409?/乐观锁冲突/:/网络/);
});

test('slow task polling is single flight and multiple completed tasks refresh state once',async()=>{
  globalThis.document=undefined;
  setSessionToken('token');let releases;let calls=0,settled=0;
  const data=new Map();const storage={getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v)};
  rememberTaskId('a',storage);rememberTaskId('b',storage);
  const ready=new Promise(r=>releases=r);
  const center=createTaskCenter({document:null,storage,fetch:async(path)=>{
    if(path.endsWith('/catalog'))return {items:[]};calls++;await ready;
    return ['a','b'].map(task_id=>({task_id,advanced:{status:'succeeded'},result_summary:{}}));
  },onTaskSettled:()=>settled++});
  const one=center.refresh(),two=center.refresh();releases();await Promise.all([one,two]);
  assert.equal(calls,1);assert.equal(settled,1);
});

test('stale or missing-authority candidates never display current publication authority',()=>{
  const stale=candidateModel({status:'stale',current_applicability:'stale'},false);
  assert.equal(stale.operationalApplicability,'stale');assert.equal(stale.operationalAdoptionAllowed,false);
  const unknown=candidateModel({status:'candidate',current_applicability:'current'},true);
  assert.equal(unknown.operationalApplicability,'unknown');assert.equal(unknown.operationalAdoptionAllowed,false);
});

test('review unknown counts survive summary projection without defaulting to pass',()=>{
  assert.equal(comparisonUnknownVoxelCount({unknown_voxel_ids_count:7}),7);
  assert.equal(comparisonUnknownVoxelCount({unknown_voxel_ids_count:0}),0);
  assert.equal(comparisonUnknownVoxelCount({unknown_voxel_ids:['a','b']}),2);
});
