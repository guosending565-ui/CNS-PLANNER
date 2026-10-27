import assert from 'node:assert/strict';
import {actionIsEndpoint, shortSiteLabel} from '../cns_planner/web/js/research_map_renderer.js';

const endpoint = {
  action_id: 'add_navigation_integrity_monitor:R0005:origin:N005',
  planner_family: 'endpoint_integrity_monitor',
  segment_index: null,
  kind: 'other', // C3-B 投影的旧显示标签
};
const middle = {
  action_id: 'navigation_integrity_endpoint_middle_reuse_v1:R0005:NEW-425000-3301500:3',
  planner_family: 'navigation_integrity_endpoint_middle_reuse_v1',
  segment_index: 3,
  kind: 'middle',
};
assert.equal(actionIsEndpoint(endpoint), true, 'endpoint actions must classify as endpoint');
assert.equal(actionIsEndpoint(middle), false, 'middle actions must not classify as endpoint');

assert.equal(shortSiteLabel({site_id: 'NEW-425000-3301500', kind: 'planned_new_site'}),
  '规划 NEW-425000');
assert.equal(shortSiteLabel({
  site_id: 'takeoff_landing_site:N006', kind: 'existing_host'}), '起降点 N006');
assert.equal(shortSiteLabel({
  site_id: 'tower-colocation:330903500010001628', kind: 'existing_host'}), '塔共址 …1628');

console.log('C3C_RESEARCH_MAP_LABELS_OK');
