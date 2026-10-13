import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BUSINESS_LAYER_DEFAULTS,
  LazyLayerRegistry,
} from '../cns_planner/web/js/visualization3d/basemap.js';
import {
  PROTECTION_PREVIEW_DISCLAIMER,
  addProtectionPreviewEntities,
  addProtectionVolumeEntities,
  removeProtectionEntities,
} from '../cns_planner/web/js/visualization3d/protection_volume.js';


const Cesium = {
  Cartesian3: { fromDegrees: (longitude, latitude, height) => ({ longitude, latitude, height }) },
  Color: {
    BLACK: { css: '#000' },
    fromCssColorString: (css) => ({
      css,
      withAlpha(alpha) { return { css, alpha }; },
    }),
  },
  PolygonHierarchy: class PolygonHierarchy {
    constructor(positions) { this.positions = positions; }
  },
  ArcType: { NONE: 0 },
};

function fakeViewer() {
  const entities = {
    values: [],
    add(entity) { this.values.push(entity); return entity; },
    remove(entity) {
      const index = this.values.indexOf(entity);
      if (index >= 0) this.values.splice(index, 1);
      return index >= 0;
    },
  };
  return { entities, marker: Symbol('viewer') };
}

const polygon = {
  type: 'Polygon',
  coordinates: [[
    [122.20, 29.90], [122.22, 29.90], [122.22, 29.91],
    [122.20, 29.91], [122.20, 29.90],
  ]],
};

function historicalVolume() {
  return {
    object_id: 'protection-corridor:R0005:ALT-100',
    mode: 'project_result',
    status: 'historical_stale',
    route_id: 'R0005',
    route_authority: 'canonical_operational_routes',
    route_status: 'stale',
    route_length_m: 9904.422448544,
    outer_half_width_m: 990,
    p17_status: 'stale',
    p17_stale_reason: 'p16_or_upstream_changed',
    p17_algorithm_version: '2.2',
    current_code_algorithm_version: '2.1',
    algorithm_version_conflict: true,
    protection_input_fingerprint: 'p17-input',
    protection_geometry_identity: 'protectiongeometryv1-historical',
    geometry_dimension: 'horizontal_boundary_with_altitude_slice',
    vertical_extent_status: 'vertical_extent_unconfirmed',
    nominal_altitude_slice_egm2008_m: 100,
    required_evidence: ['confirmed_vertical_lower_and_upper_interfaces'],
    current: false,
    historical: true,
    stale: true,
    geometry: { status: 'available', geometry: polygon, projected_area_m2: 1000 },
  };
}

function previewVolume() {
  return {
    ...historicalVolume(),
    object_id: 'protection-preview:preview-fp',
    mode: 'engineering_scenario_preview',
    status: 'engineering_preview',
    historical: false,
    stale: false,
    horizontal_half_width_m: 250,
    geometry_dimension: 'three_dimensional_volume',
    vertical_extent_status: 'user_supplied_engineering_preview',
    nominal_altitude_slice_egm2008_m: undefined,
    lower_altitude_m: 90,
    upper_altitude_m: 125,
    disclaimer: '自定义保护区示意，非已确认 P17 结果',
  };
}

function conflictVolume() {
  return {
    ...historicalVolume(),
    object_id: 'protection-corridor:R0005:ALT-100:demo-conflict',
    status: 'geometry_version_conflict',
    route_authority: 'engineering_demo_projection',
    geometry_dimension: 'none',
    route_anchor: [122.21, 29.905],
    geometry: { status: 'blocked', reason: 'geometry_version_conflict', geometry: null },
  };
}

const deps = { Cesium, undulationAt: () => 2.5 };

test('protection layer is off by default and loads only when enabled', async () => {
  assert.equal(BUSINESS_LAYER_DEFAULTS.protection, false);
  let calls = 0;
  const viewer = fakeViewer();
  const registry = new LazyLayerRegistry({
    protection: async () => {
      calls += 1;
      return addProtectionVolumeEntities(viewer, [historicalVolume()], deps);
    },
  });
  await registry.setVisible('protection', false);
  assert.equal(calls, 0);
  await registry.setVisible('protection', true);
  assert.equal(calls, 1);
  assert.equal(registry.loaded('protection'), true);
  assert.equal(viewer.marker, viewer.marker, '图层切换不替换 Viewer');
});

test('historical P17 renders a nominal-height slice without extrusion', () => {
  const viewer = fakeViewer();
  const result = addProtectionVolumeEntities(viewer, [historicalVolume()], deps);

  assert.equal(result.groups.length, 1);
  assert.equal(result.groups[0].historical, true);
  assert.ok(result.entities.some((entity) => entity.polygon));
  assert.ok(result.entities.some((entity) => entity.polyline));
  assert.ok(result.entities.every((entity) => entity.__cns.kind === 'p17-protection'));
  assert.ok(result.entities.filter((entity) => entity.polygon)
    .every((entity) => entity.polygon.extrudedHeight === undefined));
  const rows = result.entities[0].__cns.rows.map(([key, value]) => `${key}=${value}`).join('|');
  assert.match(rows, /半宽=990 m/);
  assert.match(rows, /版本=持久化 2.2；当前代码 2.1/);
  assert.match(rows, /当前性=historical\/stale/);
  assert.match(rows, /垂直范围=vertical_extent_unconfirmed/);
});

test('engineering preview renders a separate full volume and disclaimer', () => {
  const viewer = fakeViewer();
  const result = addProtectionPreviewEntities(viewer, [previewVolume()], deps);

  assert.equal(result.groups[0].preview, true);
  assert.ok(result.groups[0].object_id.startsWith('protection-preview:'));
  assert.ok(result.entities.some((entity) => entity.polygon));
  assert.ok(result.entities.length >= 4, '上下截面和侧面组成完整工程示意体');
  assert.equal(PROTECTION_PREVIEW_DISCLAIMER, '自定义保护区示意，非已确认 P17 结果');
  const rows = result.entities[0].__cns.rows.map(([key, value]) => `${key}=${value}`).join('|');
  assert.match(rows, /免责声明=自定义保护区示意，非已确认 P17 结果/);
  assert.match(rows, /高度=90–125 m EGM2008/);
  assert.equal(result.entities[0].__cns.preview, true);
});

test('geometry conflict produces an evidence marker and never a corridor', () => {
  const viewer = fakeViewer();
  const result = addProtectionVolumeEntities(viewer, [conflictVolume()], deps);

  assert.equal(result.entities.length, 1);
  assert.ok(result.entities[0].point);
  assert.equal(result.entities[0].polygon, undefined);
  assert.equal(result.entities[0].polyline, undefined);
  assert.equal(result.diagnostics.skipped[0].reason, 'geometry_version_conflict');
});

test('project and preview groups are independent and preview can be cleared', () => {
  const viewer = fakeViewer();
  const project = addProtectionVolumeEntities(viewer, [historicalVolume()], deps);
  const preview = addProtectionPreviewEntities(viewer, [previewVolume()], deps);
  const projectEntities = new Set(project.entities);
  assert.ok(preview.entities.every((entity) => !projectEntities.has(entity)));

  project.setVisible(false);
  assert.ok(project.entities.every((entity) => entity.show === false));
  assert.ok(preview.entities.every((entity) => entity.show !== false));
  removeProtectionEntities(viewer, preview.entities);
  assert.ok(project.entities.every((entity) => viewer.entities.values.includes(entity)));
  assert.ok(preview.entities.every((entity) => !viewer.entities.values.includes(entity)));
});

test('missing nominal altitude slice fails closed instead of collapsing to 0 m', () => {
  const viewer = fakeViewer();
  const volume = historicalVolume();
  volume.nominal_altitude_slice_egm2008_m = null;
  volume.route_anchor = [122.21, 29.905];
  const result = addProtectionVolumeEntities(viewer, [volume], deps);

  assert.equal(result.diagnostics.skipped[0].reason, 'nominal_altitude_missing');
  assert.ok(result.entities.every((entity) => entity.polygon === undefined),
    '缺失名义高度不得经 Number(null)=0 生成 0 m 切片');
  assert.ok(result.entities.every((entity) => entity.point), '缺证时只允许缺证标记');
  const rows = result.entities[0].__cns.rows.map(([key, value]) => `${key}=${value}`).join('|');
  assert.match(rows, /高度=缺失/);
});

test('preview volume without explicit vertical bounds never becomes 0 m geometry', () => {
  const viewer = fakeViewer();
  const volume = {
    ...previewVolume(), lower_altitude_m: null, route_anchor: [122.21, 29.905],
  };
  const result = addProtectionPreviewEntities(viewer, [volume], deps);

  assert.equal(result.diagnostics.skipped[0].reason, 'vertical_extent_unconfirmed');
  assert.ok(result.entities.every((entity) => entity.polygon === undefined),
    '缺垂直范围时不得凭 Number(null)=0 生成实体');
  assert.ok(result.entities.every((entity) => entity.point), '缺证时只允许缺证标记');
});

test('unresolved geoid fails closed instead of treating orthometric height as ellipsoidal', () => {
  const viewer = fakeViewer();
  const result = addProtectionVolumeEntities(
    viewer, [historicalVolume()], { Cesium, undulationAt: () => null });
  assert.equal(result.entities.length, 0);
  assert.equal(result.diagnostics.skipped[0].reason, 'egm2008_undulation_unavailable');
});
