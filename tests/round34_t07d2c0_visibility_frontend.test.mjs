import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildingDomainState,
  radarVisibilityEvidenceModel,
  renderRadarVisibilityEvidence,
} from '../cns_planner/web/js/workflow/radar_visibility_evidence.js';

const FILTERED_EVIDENCE={
  algorithm_version:'radar_combined_visibility_v1',
  combined_visibility:'geometry_filtered',
  computation_completion:'incomplete',evidence_completion:'incomplete',
  terrain_los:{status:'clear'},
  building_los:{
    status:'out_of_equipment_range',reason:'outside_frozen_device_range',
    within_frozen_device_range:false,
    geometry_gate:{status:'outside_equipment_range',slant_distance_m:42.1559},
    building_source_identity:{file_name:'zhoushan_buildings_v3.gpkg'},
  },
};

test('frozen device geometry exclusion is classified as filtered, not physical unknown',()=>{
  assert.equal(buildingDomainState({status:'out_of_equipment_range'}),'geometry_filtered');
  assert.equal(buildingDomainState({status:'geometry_filtered'}),'geometry_filtered');
  assert.equal(buildingDomainState({status:'blocked'}),'evaluated');
  assert.equal(buildingDomainState({status:'unknown'}),'evaluated');
  assert.equal(buildingDomainState(null),'not_evaluated');
});

test('filtered ray is rendered as device geometry filtering and never as a verdict',()=>{
  const model=radarVisibilityEvidenceModel(FILTERED_EVIDENCE);
  assert.equal(model.combinedStatus,'geometry_filtered');
  assert.equal(model.geometryFiltered,true);
  assert.equal(model.withinFrozenDeviceRange,false);
  const html=renderRadarVisibilityEvidence(FILTERED_EVIDENCE);
  assert.match(html,/设备几何过滤/);
  assert.match(html,/不是物理 unknown/);
  assert.match(html,/outside_frozen_device_range/);
  assert.doesNotMatch(html,/联合视线：clear/);
  assert.doesNotMatch(html,/联合视线：blocked/);
});

test('an evaluated ray carries no device-filtering disclosure',()=>{
  const model=radarVisibilityEvidenceModel({
    ...FILTERED_EVIDENCE,
    combined_visibility:'blocked',
    building_los:{status:'blocked',reason:'confirmed_building_prism_intersection',
      within_frozen_device_range:true},
  });
  assert.equal(model.geometryFiltered,false);
  assert.equal(model.withinFrozenDeviceRange,true);
  assert.doesNotMatch(renderRadarVisibilityEvidence({
    ...FILTERED_EVIDENCE,
    combined_visibility:'blocked',
    building_los:{status:'blocked',reason:'confirmed_building_prism_intersection',
      within_frozen_device_range:true},
  }),/设备几何过滤/);
});
