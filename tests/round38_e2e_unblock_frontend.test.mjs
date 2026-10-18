import test from 'node:test';
import assert from 'node:assert/strict';
import {createE2eConfirmation,PROVIDER_FACTS} from '../cns_planner/web/js/sources/e2e_confirmation.js';

for(const role of ['buildings','building_grid','communication_provider']) test(role+' opens read-only and requires explicit authorization',async()=>{
  const calls=[];const handlers={};let html='';
  const controls={};
  for(const name of ['value','evidence','ack','submit','close','error','identity']) controls['[data-'+name+']']={value:'',checked:false,disabled:name==='submit'};
  const dialog={style:{},set innerHTML(v){html=v;},querySelector:s=>controls[s],addEventListener:(k,v)=>{handlers[k]=v;},showModal(){},close(){},remove(){}};
  globalThis.document={createElement:()=>dialog,body:{append(){}}};
  const api=async(url,options)=>{calls.push({url,options});if(url==='/api/state')return {workflow:{revision:471,source_audits:{items:{[role]:{declared_crs:'EPSG:4326',version_fingerprint:'fp',sha256:'sha',status:'verified'}}}},project_storage:{file:'isolated/project.json'}};
    if(url.startsWith('/api/source-audits/identity'))return {revision:471,audit:{declared_crs:'EPSG:4326',version_fingerprint:'fp',sha256:'sha',status:'verified'},project_storage:{file:'isolated/project.json'}};
    return {provider:null,status:'not_configured',provider_options:[{id:'coverage_3d',status:'stale',source_fingerprint:'old'}]};};
  await createE2eConfirmation({api}).open(role);
  assert.ok(calls.every(c=>!c.options));assert.ok(html.includes('未')||html.includes('SHA256'));
  await controls['[data-submit]'].onclick();assert.ok(calls.every(c=>!c.options));
  controls['[data-value]'].value=role==='communication_provider'?'coverage_3d':'EPSG:4326';
  controls['[data-evidence]'].value='人工测试依据与限制';handlers.input();assert.equal(controls['[data-submit]'].disabled,true);
  controls['[data-ack]'].checked=true;handlers.input();await controls['[data-submit]'].onclick();
  const writes=calls.filter(c=>c.options);assert.equal(writes.length,1);
  const payload=JSON.parse(writes[0].options.body);assert.equal(payload.confirmed,true);assert.equal(payload.expected_revision,471);assert.equal(payload.source_fingerprint,role==='communication_provider'?'old':'fp');
});
test('all provider fact limits are disclosed',()=>assert.deepEqual(Object.keys(PROVIDER_FACTS),['device_catalog','coverage_3d','cns_service_capability']));
