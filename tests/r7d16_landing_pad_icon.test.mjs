/**
 * R7-D16：起降点统一 PNG 的二维/三维同源契约。
 *
 * 只断言"同一份资源、同一套身份、可回退"：
 *  1. 静态资源就是用户指定的原始 PNG（SHA256 锁定）；
 *  2. 二维 marker 与 Cesium billboard 引用同一 URL；
 *  3. 二维保留 node_id / 坐标 / 名称与矢量兜底；三维 billboard 底部锚定；
 *  4. 起点 / 终点只按权威航路首末点区分，绝不按名称猜；
 *  5. 透明边缘在绘制期裁掉，不修改原文件。
 */

import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {
  landingPadRouteEndpoints, landingPadSiteModel,
} from '../cns_planner/web/js/visualization3d/landing_pads.js';

const ROOT = new URL('../', import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), 'utf8');
const LANDING_SHA256 = '37f83a99215bc49670b22ec5e6c700a583dc99c6d963536f133d25e40365e76b';
const ICON_URL = '/assets/symbols/landing_pad.png?v=r7d16';

test('R7-D16: the unified landing pad PNG keeps the supplied bytes', () => {
  const bytes = readFileSync(new URL('cns_planner/web/assets/symbols/landing_pad.png', ROOT));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), LANDING_SHA256);
});

test('R7-D16: 2D marker and 3D billboard share the same PNG URL', () => {
  const display = read('cns_planner/web/js/map/display_layers.js');
  const landing = read('cns_planner/web/js/visualization3d/landing_pads.js');
  const main = read('cns_planner/web/js/main.js');
  assert.match(display, new RegExp(ICON_URL.replace(/[.?]/g, '\\$&')));
  assert.match(landing, new RegExp(ICON_URL.replace(/[.?]/g, '\\$&')));
  assert.match(main, /landingPadIconUrl\(\)/);
  assert.match(main, /setLandingPadIconImage\(landingPadImage\)/);
});

test('R7-D16: 2D keeps identity, labels, vector fallback and alpha-edge crop', () => {
  const display = read('cns_planner/web/js/map/display_layers.js');
  // 透明边缘在绘制期裁掉
  assert.match(display, /landingPadContentCrop/);
  assert.match(display, /data\[\(y\*width\+x\)\*4\+3\]>8/);
  // 未加载时回退既有矢量符号
  assert.match(display, /if\(!drawn\)drawMarker\(ctx,'diamond'/);
  assert.match(display, /if\(!drawn\)drawMarker\(ctx,'circle'/);
  // 名称 / 坐标 / node_id 不被改写
  assert.match(display, /const coordinate=Array\.isArray\(node\.coordinate\)\?node\.coordinate\.join\(','\):''/);
  assert.match(display, /placer\.place\(ctx,rolePrefix\+\(node\.name\|\|node\.node_id\)/);
  // 起点 / 终点只按航路首末点
  assert.match(display, /startCoordinates\.add\(start\)/);
  assert.match(display, /endCoordinates\.add\(end\)/);
});

test('R7-D16: 3D billboard is bottom-anchored, distance-scaled and read-only', () => {
  const landing = read('cns_planner/web/js/visualization3d/landing_pads.js');
  assert.match(landing, /verticalOrigin: Cesium\.VerticalOrigin\.BOTTOM/);
  assert.match(landing, /scaleByDistance: new Cesium\.NearFarScalar/);
  assert.match(landing, /disableDepthTestDistance: Number\.POSITIVE_INFINITY/);
  assert.match(landing, /entity\.__cns = \{/);
  assert.match(landing, /kind: 'landing-pad'/);
  assert.doesNotMatch(landing, /POST|fetch\(/);
  const app = read('cns_planner/web/js/visualization3d/app.js');
  assert.match(app, /\['landingPads', '起降点（项目 \+ 参考；PNG billboard，只读）'/);
  assert.match(app, /BUSINESS_LAYER_DEFAULTS\.landingPads/);
});

test('R7-D16: start/end roles come from the unique canonical route endpoints', () => {
  const routes = [{
    route_id: 'R0005', authority: 'canonical_operational_route',
    path: [[122.10, 30.00], [122.20, 30.10]],
  }];
  const endpoints = landingPadRouteEndpoints(routes);
  assert.deepEqual(endpoints.start, [122.10, 30.00]);
  assert.deepEqual(endpoints.end, [122.20, 30.10]);

  const payload = {
    candidates: [
      {candidate_id: 'node:N1', source_id: 'N1', name: '东极起降点',
        candidate_kind: 'project_node', coordinate: [122.10, 30.00],
        spatial_use_status: 'eligible', coordinate_quality: 'recorded'},
      {candidate_id: 'node:N2', source_id: 'N2', name: '终点坪',
        candidate_kind: 'project_node', coordinate: [122.20, 30.10],
        spatial_use_status: 'eligible', coordinate_quality: 'recorded'},
      {candidate_id: 'node:N3', source_id: 'N3', name: '中间点',
        candidate_kind: 'project_node', coordinate: [122.15, 30.05],
        spatial_use_status: 'eligible', coordinate_quality: 'recorded'},
      {candidate_id: 'reference-landing-site:R1', source_id: 'R1', name: '参考点',
        candidate_kind: 'reference_landing_site', coordinate: [122.12, 30.02],
        spatial_use_status: 'eligible', coordinate_quality: 'recorded'},
      {candidate_id: 'tower-colocation:T1', source_id: 'T1', name: '塔',
        candidate_kind: 'tower_colocation', coordinate: [122.11, 30.01],
        spatial_use_status: 'eligible'},
    ],
  };
  const model = landingPadSiteModel(payload, endpoints);
  assert.equal(model.length, 4, 'only landing kinds enter the layer');
  const byId = Object.fromEntries(model.map((row) => [row.siteId, row]));
  assert.equal(byId.N1.role, 'start');
  assert.equal(byId.N2.role, 'end');
  assert.equal(byId.N3.role, 'normal');
  assert.equal(byId.R1.role, 'reference');
  assert.equal(byId.R1.ringColor, '#1a8677');
  assert.equal(byId.N1.ringColor, '#1f7a4d');
  assert.equal(byId.N2.ringColor, '#c2410c');
  // 没有唯一权威航路时不得猜起点/终点
  const noAuthority = landingPadSiteModel(payload, null);
  assert.ok(noAuthority.every((row) => row.role !== 'start' && row.role !== 'end'));
});
