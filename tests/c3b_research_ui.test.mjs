import assert from 'node:assert/strict';
import {
  buildMapModel, manifestIdentity, parameterChanged, parameterValue, reportUrl,
  statusText,
} from '../cns_planner/web/js/research_map.js';
import { researchRunView } from '../cns_planner/web/js/research_runs.js';

const schema = {
  contract_version: 'c3b-research-parameters-1',
  ids: ['navigation_middle_monitor_sites'],
  default: ['SITE-A', 'SITE-B'],
  options: [
    {site_id: 'SITE-A', label: 'A', host_kind: 'planned_new_site', subsystems: ['C', 'N', 'S']},
    {site_id: 'SITE-B', label: 'B', host_kind: 'planned_new_site', subsystems: ['C', 'N', 'S']},
    {site_id: 'SITE-C', label: 'C', host_kind: 'existing_host', subsystems: ['C', 'N', 'S']},
  ],
};

assert.deepEqual(parameterValue(schema, {navigation_middle_monitor_sites: ['SITE-C', 'SITE-B']}),
  {navigation_middle_monitor_sites: ['SITE-C', 'SITE-B']});
assert.equal(parameterChanged(schema, {navigation_middle_monitor_sites: ['SITE-C', 'SITE-B']}), true);
assert.equal(parameterChanged(schema, {navigation_middle_monitor_sites: ['SITE-A', 'SITE-B']}), false);
assert.throws(() => parameterValue(schema, {navigation_middle_monitor_sites: ['SITE-A', 'SITE-A']}),
  /两个不同/);

function manifest(overrides = {}) {
  return {
    run_id: 'run-1', contract_version: 'contract-1',
    input_version: {id: 'in-1', input_fingerprint: 'input-1', parameter_fingerprint: 'param-1'},
    authoritative_result_list_sha256: 'list-1',
    business_state: {
      P14: {status: 'failed'}, P15: {status: 'failed'}, P16: {status: 'proposal_ready'},
      surveillance_Radar: {formal_status: 'not_calculated', verdict: 'unknown'},
      surveillance_S: {verdict: 'unknown'}, communication_C: {verdict: 'satisfied'},
      navigation_N: {verdict: 'unknown'}, navigation_RID: {verdict: 'satisfied'},
    },
    gates: {p17_step6_gate: {status: 'fully_satisfied'},
      p18_initialize: {status: 'review_initialized', allowed: true}},
    admission: {p18_confirmation_allowed: false},
    research_limitations: [],
    map_projection: {
      run_id: 'run-1', authority: 'research_isolated',
      counts: {sites: 7, devices: 18, existing_hosts: 5, planned_new_sites: 2, n_actions: 4},
      layers: ['C', 'RID', 'Radar', 'S', 'N'].map(id => ({
        id, status: id === 'C' || id === 'RID' ? 'satisfied'
          : id === 'Radar' ? 'not_calculated' : 'unknown',
        conclusion: id === 'C' || id === 'RID' ? 'satisfied'
          : id === 'Radar' ? 'unknown' : 'unknown',
        features: [], feature_count: 0,
      })),
    },
    ...overrides,
  };
}

const model = buildMapModel(manifest());
assert.equal(model.counts.sites, 7);
assert.equal(model.counts.devices, 18);
assert.equal(model.counts.n_actions, 4);
assert.equal(model.layers.get('Radar').status, 'not_calculated');
assert.equal(model.layers.get('S').status, 'unknown');
assert.throws(() => buildMapModel(manifest({
  map_projection: {...manifest().map_projection, run_id: 'other'}})), /identity/);
const identity = manifestIdentity(manifest());
assert.equal(identity.inputVersionId, 'in-1');
assert.equal(identity.resultListSha256, 'list-1');
assert.equal(reportUrl('run-1', 'markdown'), '/api/research/runs/run-1/report?format=markdown');
assert.match(statusText('not_calculated'), /未计算/);
assert.match(statusText('unknown'), /不得读作满足/);

const view = researchRunView({result_summary: {
  run_id: 'run-1', input_version: {id: 'in-1', input_fingerprint: 'input-1'},
  contract_version: 'contract-1', authoritative_result_list_sha256: 'list-1',
}, advanced: {status: 'succeeded'}, result_ready: true}, manifest());
assert.equal(view.identity.consistent, true);
assert.equal(view.confirmationAllowed, false);
assert.equal(view.P18.status, 'review_initialized');
assert.throws(() => researchRunView({result_summary: {
  run_id: 'run-2', input_version: {id: 'in-1'}, contract_version: 'contract-1',
}}, manifest()), /identity/);
console.log('C3-B research frontend PASS');
