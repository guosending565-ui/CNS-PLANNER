import assert from 'node:assert/strict';
import { researchRunView } from '../cns_planner/web/js/research_runs.js';

const manifest = {
  run_id: 'c3a-test', input_version: { id: 'input-1' }, contract_version: 'contract-1',
  authoritative_result_list_sha256: 'sha',
  business_state: { P14: {status: 'failed'}, P15: {status: 'failed'}, P16: {status: 'proposal_ready'},
    surveillance_Radar: {verdict: 'unknown'}, surveillance_S: {verdict: 'unknown'} },
  gates: {p17_step6_gate: {status: 'fully_satisfied', confirmation_allowed: true}},
  admission: {p18_confirmation_allowed: false}, research_limitations: [{limitation_id: 'radar_missing'}],
};
const task = {advanced: {status: 'succeeded'}, result_ready: true};
const view = researchRunView(task, manifest);
assert.equal(view.taskCompleted, true);
assert.equal(view.confirmationAllowed, false);
assert.equal(view.P14.status, 'failed');
assert.equal(view.P15.status, 'failed');
assert.equal(view.P16.status, 'proposal_ready');
assert.equal(view.P17.status, 'fully_satisfied');
assert.match(view.P17.label, /连续中断/);
assert.equal(view.Radar.status, 'unknown');
assert.equal(view.S.status, 'unknown');
assert.equal(view.limitations.length, 1);
assert.equal(researchRunView({advanced: {status: 'running'}}, null).confirmationAllowed, false);
assert.equal(researchRunView({advanced: {status: 'failed'}}, null).taskCompleted, false);
assert.throws(() => researchRunView({...task, run_id: 'other'}, manifest), /identity/);
console.log('C3-A frontend consumer PASS');
