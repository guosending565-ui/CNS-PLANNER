/**
 * Round37-T3D-2B：Radar 方向性三维覆盖的前端契约测试（Node，进程内运行）。
 *
 * 覆盖任务第 4/6 节里必须在**前端**锁定的行为：
 *
 *  1. Radar 图层**默认关闭**，且开启才请求（懒加载）；
 *  2. 每个真实面阵**单独成组**，可单独关闭；
 *  3. 整体关闭 → 全部 `show = false`；重新开启 → 按各自开关恢复；
 *  4. 缺证（blocked）面阵只留"缺证锚点"，**绝不**画楔体、绝不画全向圆；
 *  5. 预览实体与真实面阵实体**分属不同入口与不同分组**，且带常驻免责声明；
 *  6. 弹窗必须披露来源、型号、状态、限制（LOS unknown、斜距语义、禁止几何）；
 *  7. `LazyLayerRegistry` 既支持实体数组（既有全部图层，行为不变），
 *     也支持 `{ entities, setVisible }`（Radar 的整体 + 逐组两级开关）。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BASE_LAYER_DEFAULTS,
  BUSINESS_LAYER_DEFAULTS,
  LazyLayerRegistry,
} from '../cns_planner/web/js/visualization3d/basemap.js';
import {
  RADAR_PREVIEW_DISCLAIMER,
  addRadarPreviewEntities,
  addRadarVolumeEntities,
} from '../cns_planner/web/js/visualization3d/radar_volume.js';

const Cesium = {
  Cartesian3: { fromDegrees: (longitude, latitude, height) => ({ longitude, latitude, height }) },
  Color: {
    BLACK: { css: '#000' },
    fromCssColorString: (css) => ({
      css,
      alpha: 1,
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
    },
  };
  return { entities };
}

const deps = { Cesium, undulationAt: () => 2.5 };

function ring(count = 3, height = 30) {
  const rows = [];
  for (let index = 0; index <= count; index += 1) {
    rows.push([122.25 + index * 0.001, 29.84 + index * 0.001, height]);
  }
  return rows;
}

function availableVolume(panelId = 'P-1', azimuth = 49.38) {
  return {
    panel_id: panelId,
    tower_id: 'TOWER-1',
    radar_type: 'radar_i',
    radar_type_label: '中近程雷达Ⅰ型',
    azimuth_deg: azimuth,
    service_origin_egm2008_m: 21.28,
    origin_status: 'confirmed',
    origin_confirmed: true,
    parameter_source: 'radar_surveillance_layout.items[0].selected_panels[0]',
    layout_item_status: 'stale',
    layout_stale_reason: 'layered_validation_evidence_outdated',
    coverage_semantics: 'engineering_geometric_directional_coverage_not_measured',
    forbidden_geometry: ['omnidirectional_full_circle', 'hemisphere_360'],
    los: { status: 'unknown', reason: 'terrain_los_not_persisted' },
    azimuth_window: {
      center_deg: azimuth, start_deg: 4.38, end_deg: 94.38,
      span_deg: 90, wrapped_across_north: false,
    },
    geometry: {
      status: 'available',
      reason: null,
      min_slant_range_m: 120,
      max_slant_range_m: 3000,
      elevation_min_deg: 0,
      elevation_center_deg: 22.5,
      elevation_max_deg: 45,
      slant_range_semantics: 'slant',
      horizontal_radius_at_elevation_zero_m: 3000,
      outer_surface_rings: [ring(), ring(3, 60), ring(3, 90)],
      inner_surface_rings: [ring(3, 25), ring(3, 40)],
      side_fans: [
        { edge: 'start', ring: ring(2, 30) },
        { edge: 'end', ring: ring(2, 30) },
      ],
      ground_arc: ring(4, 21.28),
      near_arc: ring(3, 21.28),
      azimuth_center_ray: [ring(1, 21.28)[0], ring(1, 21.28)[1]],
    },
  };
}

function blockedVolume(panelId = 'P-BLOCKED') {
  return {
    panel_id: panelId,
    tower_id: 'TOWER-2',
    radar_type: 'radar_i',
    azimuth_deg: 120,
    longitude: 122.2,
    latitude: 29.8,
    service_origin_egm2008_m: null,
    origin_status: 'missing',
    origin_confirmed: false,
    parameter_source: 'radar_surveillance_layout.items[0].selected_panels[1]',
    layout_item_status: 'stale',
    forbidden_geometry: ['omnidirectional_full_circle'],
    los: { status: 'unknown', reason: 'terrain_los_not_persisted' },
    azimuth_window: { center_deg: 120, start_deg: 75, end_deg: 165, span_deg: 90 },
    geometry: {
      status: 'blocked',
      reason: 'tower_top_orthometric_m_not_resolved',
      required_evidence: ['tower_obstacle_profiles.items[TOWER-2].tower_top_orthometric_m'],
      outer_surface_rings: [],
      inner_surface_rings: [],
      side_fans: [],
      ground_arc: [],
      near_arc: [],
      azimuth_center_ray: [],
    },
  };
}

/* ------------------------------------------------------------------ 默认状态 */

test('radar layer is off by default and stays in the lazy business group', () => {
  assert.equal(BUSINESS_LAYER_DEFAULTS.radar, false);
  assert.equal(BASE_LAYER_DEFAULTS.imagery, true);
  assert.ok(Object.values(BUSINESS_LAYER_DEFAULTS).every((value) => value === false));
});

test('lazy registry only calls the loader when the layer is turned on', async () => {
  let calls = 0;
  const registry = new LazyLayerRegistry({
    radar: async () => { calls += 1; return []; },
  });
  assert.equal(await registry.setVisible('radar', false), true);
  assert.equal(calls, 0, '默认关闭时绝不请求几何');
  assert.equal(await registry.setVisible('radar', true), true);
  assert.equal(calls, 1);
  assert.equal(registry.loaded('radar'), true);
});

/* -------------------------------------------------------- 真实面阵：分组与清理 */

test('each real panel becomes its own group with a wedge', () => {
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(
    viewer, [availableVolume('P-1'), availableVolume('P-2', 200)], deps);

  assert.equal(result.groups.length, 2);
  assert.deepEqual(result.groups.map((group) => group.panel_id), ['P-1', 'P-2']);
  assert.ok(result.groups.every((group) => group.entities.length > 1));
  assert.ok(result.groups.every((group) => group.visible === true));
  assert.equal(result.groups[0].preview, false);
  // 楔体必须真的画出来（曲面/端面/地面弧），而不是只有一个锚点。
  assert.ok(result.entities.some((entity) => entity.polygon));
  assert.ok(result.entities.some((entity) => entity.polyline));
  assert.equal(result.diagnostics.skipped.length, 0);
});

test('per-panel visibility and layer-wide visibility compose', async () => {
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(viewer, [availableVolume('P-1'), availableVolume('P-2')], deps);
  const registry = new LazyLayerRegistry({
    radar: async () => ({ entities: result.entities, setVisible: result.setVisible }),
  });

  await registry.setVisible('radar', true);
  assert.ok(result.entities.every((entity) => entity.show === true));

  // 单独关闭第二个面阵：只有该组的实体被隐藏。
  const second = result.groups[1];
  second.visible = false;
  result.setVisible(true);
  assert.ok(result.groups[0].entities.every((entity) => entity.show === true));
  assert.ok(second.entities.every((entity) => entity.show === false));

  // 整体关闭：全部隐藏（不销毁 Viewer，也不移除实体）。
  await registry.setVisible('radar', false);
  assert.ok(result.entities.every((entity) => entity.show === false));
  assert.equal(viewer.entities.values.length, result.entities.length);

  // 重新开启：必须尊重"第二个面阵被单独关闭"的状态。
  await registry.setVisible('radar', true);
  assert.ok(result.groups[0].entities.every((entity) => entity.show === true));
  assert.ok(second.entities.every((entity) => entity.show === false));
});

test('blocked panel renders an evidence anchor only, never a wedge', () => {
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(viewer, [blockedVolume()], deps);

  assert.equal(result.groups.length, 1);
  assert.equal(result.groups[0].status, 'blocked');
  assert.equal(result.groups[0].entities.length, 1);
  const [anchor] = result.groups[0].entities;
  assert.ok(anchor.point, 'blocked 面阵只留缺证锚点');
  assert.equal(anchor.polygon, undefined);
  assert.equal(anchor.polyline, undefined);
  assert.equal(result.diagnostics.skipped.length, 1);
  assert.equal(result.diagnostics.skipped[0].reason, 'tower_top_orthometric_m_not_resolved');
});

test('unresolved geoid undulation never renders a wedge at height zero', () => {
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(
    viewer, [availableVolume()], { Cesium, undulationAt: () => null });
  assert.equal(result.groups[0].entities.length, 1);
  assert.equal(result.groups[0].entities[0].polygon, undefined);
  assert.equal(result.diagnostics.skipped[0].reason, 'egm2008_undulation_unavailable');
  assert.equal(result.groups[0].entities[0].position.height, 0, '锚点回落到 0 仅用于定位');
});

/* ------------------------------------------------------------------ 弹窗披露 */

test('popup discloses origin, model, status, limits and forbidden geometry', () => {
  const _viewer = fakeViewer();
  const volume = availableVolume();
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(viewer, [volume], deps);
  const info = result.entities[0].__cns;
  const flat = info.rows.map(([key, value]) => `${key}=${value}`).join('|');

  assert.equal(info.kind, 'radar-panel');
  assert.match(flat, /型号=中近程雷达Ⅰ型/);
  assert.match(flat, /水平波束=90°/);
  assert.match(flat, /仰角范围=0°–45°/);
  assert.match(flat, /近端斜距=120 m/);
  assert.match(flat, /远端斜距=3000 m/);
  assert.match(flat, /斜距语义=斜距/);
  assert.match(flat, /LOS 状态=unknown（terrain_los_not_persisted）/);
  assert.match(flat, /限制=.*未评估/);
  assert.match(flat, /禁止几何=.*omnidirectional_full_circle/);
  assert.match(flat, /来源容器=radar_surveillance_layout/);
});

test('blocked popup lists the missing evidence', () => {
  const viewer = fakeViewer();
  const result = addRadarVolumeEntities(viewer, [blockedVolume()], deps);
  const rows = result.entities[0].__cns.rows.map(([key, value]) => `${key}=${value}`).join('|');
  assert.match(rows, /几何状态=blocked/);
  assert.match(rows, /所需证据=.*tower_top_orthometric_m/);
});

/* ------------------------------------------------------- 预览与真实面阵隔离 */

test('preview entities are separated from the real project panels', () => {
  const projectViewer = fakeViewer();
  const project = addRadarVolumeEntities(projectViewer, [availableVolume('P-1')], deps);
  assert.equal(project.groups[0].preview, false);

  const previewViewer = fakeViewer();
  const preview = addRadarPreviewEntities(
    previewViewer, [{ ...availableVolume('PREVIEW-TOWER-1-radar_i-45.000'), preview: true }], deps);

  assert.equal(preview.groups[0].preview, true);
  assert.ok(preview.groups.every((group) => group.panel_id.startsWith('PREVIEW-')));
  // 预览实体与真实面阵实体**没有**任何共享引用。
  const projectEntities = new Set(project.entities);
  assert.ok(preview.entities.every((entity) => !projectEntities.has(entity)));

  const rows = preview.entities[0].__cns.rows.map(([key, value]) => `${key}=${value}`).join('|');
  assert.match(rows, /免责声明=设备参数示意，非已采纳布设，非已确认探测覆盖/);
  assert.match(rows, /隔离=.*不写入 ProjectState/);
  assert.equal(preview.entities[0].__cns.preview, true);
  assert.equal(project.entities[0].__cns.preview, undefined);
  assert.equal(preview.entities[0].__cns.title.startsWith('设备参数示意'), true);
});

test('preview disclaimer text is the single shared constant', () => {
  assert.equal(RADAR_PREVIEW_DISCLAIMER, '设备参数示意，非已采纳布设，非已确认探测覆盖');
});

/* ------------------------------------------------- LazyLayerRegistry 向后兼容 */

test('registry keeps the legacy array loader behaviour', async () => {
  const first = { show: true };
  const second = { show: true };
  const registry = new LazyLayerRegistry({ legacy: async () => [first, second] });
  await registry.setVisible('legacy', true);
  assert.deepEqual(registry.entities('legacy'), [first, second]);
  await registry.setVisible('legacy', false);
  assert.equal(first.show, false);
  assert.equal(second.show, false);
  assert.equal(await registry.setVisible('missing-layer', true), false);
});
