import assert from 'node:assert/strict';
import test from 'node:test';

import {lonLatToMercator,mercatorToLonLat} from '../cns_planner/web/js/map/projection.js';
import {createStore} from '../cns_planner/web/js/state/store.js';
import {buildGridOverlayCache,findGridCell} from '../cns_planner/web/js/map/grid_overlay.js';
import {algorithmManifestDetails,algorithmSelectionKey} from '../cns_planner/web/js/workflow/step01_project.js';
import {render as renderStep4,withLegacyRequiredAliases} from '../cns_planner/web/js/workflow/step04_operation.js';

test('projection round trips WGS84 coordinates',()=>{
  const original=[120.1234,30.5678],restored=mercatorToLonLat(...lonLatToMercator(...original));
  assert.ok(Math.abs(restored[0]-original[0])<1e-8);
  assert.ok(Math.abs(restored[1]-original[1])<1e-8);
});

test('store merges explicit state updates',()=>{
  const store=createStore({server:null,ui:{step:1}});
  store.set({server:{revision:2}});
  assert.deepEqual(store.get(),{server:{revision:2},ui:{step:1}});
});

test('grid cache joins attributes by grid_id and uses half-open hit boundaries',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{status:'passed',cells:{A:{status:'passed',value_mean:5},B:{status:'passed',value_mean:10}}},terrain:{cells:{}}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:(bbox,x,y)=>x>=bbox[0]&&x<bbox[2]&&y>=bbox[1]&&y<bbox[3]};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.equal(cache.byId.get('A').population.value_mean,5);
  assert.equal(findGridCell(cache,121,30.5,theme).cell.grid_id,'B');
});

test('population theme prefers governed target-grid density and excludes partial NoData',()=>{
  const grid={cells:[{grid_id:'A',bbox:[120,30,121,31]},{grid_id:'B',bbox:[121,30,122,31]}]};
  const attributes={population:{status:'missing_data',cells:{
    A:{status:'passed',quantity_status:'passed',population_density_people_km2:25,value_mean:999},
    B:{status:'passed',quantity_status:'missing_data',population_density_people_km2:50,value_mean:888}
  }},terrain:{cells:{}}};
  const theme={quantileBreaks:values=>values,bboxContainsHalfOpen:()=>true};
  const cache=buildGridOverlayCache(grid,attributes,{},theme);
  assert.deepEqual(cache.populationBreaks,[25]);
});

test('algorithm selection key includes type id and exact version',()=>{
  assert.equal(algorithmSelectionKey({algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1'}),'risk_model|risk-model-v1-relative-index|1.1');
});

test('step 1 algorithm details preserve auditable manifest fields',()=>{
  const item={algorithm_type:'risk_model',algorithm_id:'risk-model-v1-relative-index',version:'1.1',name:'Risk V1',provider:'CNS-PLANNER',maturity:'baseline',description:'relative',inputs:['grid'],outputs:['risk'],parameter_schema:{type:'object'},assumptions:['a'],limitations:['b'],references:[]};
  assert.deepEqual(algorithmManifestDetails(item),{identity:'risk-model-v1-relative-index@1.1',provider:'CNS-PLANNER',maturity:'baseline',inputs:'grid',outputs:'risk',assumptions:'a',limitations:'b',references:'未登记'});
});

test('step 4 canonical seconds create exact V1 aliases',()=>{
  const result=withLegacyRequiredAliases({
    communication:{performance:{max_latency_s:0.25,min_redundancy:2}},
    navigation:{performance:{max_horizontal_error_m:3,integrity_required:'required',min_redundancy:1}},
    surveillance:{performance:{max_update_interval_s:2,min_redundancy:1}},
  });
  assert.equal(result.communication.latency_ms,250);
  assert.equal(result.navigation.accuracy_m,3);
  assert.equal(result.navigation.integrity,'required');
  assert.equal(result.surveillance.update_interval_s,2);
});

test('step 4 separates aircraft capability and required performance UI',()=>{
  globalThis.document={createElement:()=>{const node={innerHTML:''};Object.defineProperty(node,'textContent',{set(value){node.innerHTML=String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}});return node;}};
  const html=renderStep4({flow:{aircraft_profiles:{count:1,items:[{aircraft_id:'A',name:'A',communication:{capabilities:['radio'],type:{technology:'4g'},confirmed:true},navigation:{},surveillance:{}}]},selected_aircraft_profile_id:'A',aircraft_source:'catalog',scenario_routes:[],required_cns:{project_default:{}},device_catalog:{items:[]},steps:{'4':false}}});
  assert.match(html,/Aircraft Capability/);
  assert.match(html,/Required CNS Performance/);
  assert.match(html,/最大时延 s/);
  assert.match(html,/Ground Device Capability/);
  assert.match(html,/ReliabilitySpec/);
  assert.doesNotMatch(html,/最大时延 ms/);
});
