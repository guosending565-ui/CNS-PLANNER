import assert from 'node:assert/strict';
import test from 'node:test';
import {ensureSessionToken,setSessionToken} from '../cns_planner/web/js/tasks.js';

test('concurrent task bootstrap uses one lightweight identity read', async()=>{
  setSessionToken(null);const calls=[];let release;
  const gate=new Promise(resolve=>{release=resolve;});
  const fetch=async path=>{calls.push(path);await gate;return {ok:true,json:async()=>({token:'local-test'})};};
  const a=ensureSessionToken(fetch),b=ensureSessionToken(fetch);release();
  assert.deepEqual(await Promise.all([a,b]),['local-test','local-test']);
  assert.deepEqual(calls,['/api/project/active']);
});

test('failed token read can recover on next task request',async()=>{
  setSessionToken(null);await assert.rejects(ensureSessionToken(async()=>{throw Error('offline');}));
  assert.equal(await ensureSessionToken(async path=>{assert.equal(path,'/api/project/active');return {ok:true,json:async()=>({token:'recovered'})};}),'recovered');
});