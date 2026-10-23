import assert from 'node:assert/strict';
import {catalogItemForService} from '../cns_planner/web/js/workflow/cns_service_evidence.js';
const old = {device_id:'old', service_key:'C:communication', enabled:false,coverage_geometry:{radius_by_surface:{land:4000}}};
const current = {...old, device_id:'new', enabled:true};
assert.equal(catalogItemForService({device_catalog:{items:[old,current]}},'C:communication')?.device_id,'new');
assert.equal(catalogItemForService({device_catalog:{items:[old]}},'C:communication'),null);
console.log('2 active-profile assertions passed');
