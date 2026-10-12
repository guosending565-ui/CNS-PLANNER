import assert from 'node:assert/strict';
import test from 'node:test';

import {
  RADAR_VISIBILITY_DISCLAIMER,
  radarVisibilityEvidenceModel,
  renderRadarVisibilityEvidence,
} from '../cns_planner/web/js/workflow/radar_visibility_evidence.js';

const EVIDENCE={
  algorithm_version:'radar_combined_visibility_v1',
  combined_visibility:'blocked',
  computation_completion:'complete',evidence_completion:'complete',
  terrain_los:{status:'clear',algorithm_version:'radar_terrain_los_v1_straight_ray_sampled_profile'},
  building_los:{
    status:'blocked',reason:'confirmed_building_prism_intersection',
    algorithm_version:'radar_building_los_v1_2_5d_prism',
    height_evidence_policy_id:'radar_building_los_height_evidence_policy_v1',
    obstruction_hits:[{building_id:'280247',height_evidence_level:'source_tagged'}],
    building_source_identity:{file_name:'zhoushan_buildings_v3.gpkg'},
  },
};

test('model transcribes all domains without recomputing visibility',()=>{
  const model=radarVisibilityEvidenceModel(EVIDENCE);
  assert.equal(model.combinedStatus,'blocked');
  assert.equal(model.terrainStatus,'clear');
  assert.equal(model.buildingStatus,'blocked');
  assert.equal(model.heightEvidenceLevel,'source_tagged');
  assert.equal(model.computationCompletion,'complete');
});

test('render follows conclusion reason source and discloses no detection probability',()=>{
  const html=renderRadarVisibilityEvidence(EVIDENCE);
  assert.match(html,/联合视线：blocked/);
  assert.match(html,/地形 LOS：clear/);
  assert.match(html,/建筑 LOS：blocked/);
  assert.match(html,/confirmed_building_prism_intersection/);
  assert.match(html,/source_tagged/);
  assert.match(html,/zhoushan_buildings_v3\.gpkg/);
  assert.match(html,new RegExp(RADAR_VISIBILITY_DISCLAIMER));
});

test('unknown and incomplete are explicit and never rendered as clear',()=>{
  const html=renderRadarVisibilityEvidence({
    ...EVIDENCE,combined_visibility:'unknown',computation_completion:'incomplete',
    evidence_completion:'incomplete',building_los:{status:'unknown',reason:'building_query_incomplete'},
  });
  assert.match(html,/联合视线：unknown/);
  assert.match(html,/计算完整度：incomplete/);
  assert.match(html,/证据完整度：incomplete/);
  assert.match(html,/building_query_incomplete/);
  assert.doesNotMatch(html,/联合视线：clear/);
});

test('missing evidence stays not evaluated and does not trigger work',()=>{
  const model=radarVisibilityEvidenceModel(null);
  assert.equal(model.combinedStatus,'not_evaluated');
  assert.equal(model.requested,false);
  assert.match(renderRadarVisibilityEvidence(null),/尚未执行建筑 LOS 联合诊断/);
});
