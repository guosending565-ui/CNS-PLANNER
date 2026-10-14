/**
 * Round38-T3D4 三维 CNS 站址 / 设备 / 固定目标带前端契约测试。
 *
 * 只验证**纯函数**与实体构造契约（用最小 Cesium 替身），不启动浏览器、不访问网络。
 *
 * 锁定本轮最关键的几条硬约定：
 *
 *  1. 设备类别配色必须是 C 绿 / N 黄 / RID 橙 / Radar 蓝，且与二维一致；
 *  2. **站址存在绝不推断设备已安装**：候选站址不得产生设备实体；
 *  3. 铁塔**只有**塔顶证据存在时才画竖直结构；缺塔顶一律不画结构；
 *  4. 地面站绝不继承塔顶高度；
 *  5. 固定目标带只有 current 或**明确标注的非权威历史预览**才可绘制，
 *     几何构造被阻断时 fail-closed，且绝不使用 P17 旧宽度。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CNS_LABEL_FONT,
  CNS_SERVICE_STYLE,
  CNS_SITE_STATE_STYLE,
  CNS_TOWER_STYLE,
  addCnsSiteEntities,
  deviceCategoryGroups,
  ellipsoidalHeight,
  sitePopupRows,
} from '../cns_planner/web/js/visualization3d/cns_sites.js';
import {
  FIXED_CORRIDOR_DISCLAIMER,
  FIXED_CORRIDOR_PREVIEW_DISCLAIMER,
  FIXED_CORRIDOR_STYLE,
  addFixedCorridorBandEntities,
  corridorPopupRows,
  corridorRingsFor,
} from '../cns_planner/web/js/visualization3d/fixed_corridor_band.js';
import { BUSINESS_LAYER_DEFAULTS } from '../cns_planner/web/js/visualization3d/basemap.js';

/* ----------------------------------------------------------- 最小 Cesium 替身 */

class FakeCartesian3 {
  constructor(longitude, latitude, height) {
    this.longitude = longitude;
    this.latitude = latitude;
    this.height = height;
  }
  static fromDegrees(longitude, latitude, height) {
    return new FakeCartesian3(longitude, latitude, height);
  }
}

class FakeColor {
  constructor(css) { this.css = css; this.alpha = 1; }
  static fromCssColorString(css) { return new FakeColor(String(css)); }
  withAlpha(value) { const next = new FakeColor(this.css); next.alpha = Number(value); return next; }
}

class FakeCartesian2 {
  constructor(x, y) { this.x = x; this.y = y; }
}

class FakePolygonHierarchy {
  constructor(positions) { this.positions = positions; }
}

class FakeEntity {
  constructor(options) {
    Object.assign(this, options);
    this.show = true;
  }
}

class FakeViewer {
  constructor() { this.entities = new FakeEntityStore(); }
}

class FakeEntityStore {
  constructor() { this.values = []; }
  add(options) { const entity = new FakeEntity(options); this.values.push(entity); return entity; }
  remove(entity) {
    const index = this.values.indexOf(entity);
    if (index >= 0) this.values.splice(index, 1);
  }
}

const Cesium = {
  Cartesian3: FakeCartesian3,
  Cartesian2: FakeCartesian2,
  Color: FakeColor,
  PolygonHierarchy: FakePolygonHierarchy,
  LabelStyle: { FILL_AND_OUTLINE: 2 },
};

//: geoid N 恒为 30.0 m：用于验证"逐点 N"确实被加上（而不是当 0）。
const undulationAt = () => 30.0;
const terrainHeightAt = (longitude, latitude) => 20.0 + horizontalOffset(longitude, latitude);
function horizontalOffset(longitude, latitude) {
  return (Number(longitude) - 122) * 100 + (Number(latitude) - 30) * 100;
}

/* ------------------------------------------------------------------ 站址记录 */

function towerSite(overrides = {}) {
  return {
    object_id: 'site:tower-colocation:T-TOP',
    site_id: 'tower-colocation:T-TOP',
    source_id: 'T-TOP', tower_id: 'T-TOP',
    name: '有塔顶证据的塔',
    site_kind: 'tower_colocation', site_kind_label: '既有铁塔（共址候选）',
    site_type: '角钢塔',
    site_state: 'planned_selected', site_state_label: '规划选中（待确认）',
    longitude: 122.25, latitude: 29.975,
    installation_position: 'tower_top', installation_position_label: '塔顶安装',
    spatial_use_status: 'eligible', spatial_block_reason: null,
    coordinate_source: 'real_tower_inventory', source_crs: null, data_source: null,
    terrain_elevation_m: 19.5,
    terrain_elevation_source: 'tower_obstacle_profiles.terrain_elevation_m',
    terrain_vertical_reference: 'egm2008_orthometric', terrain_status: 'passed',
    tower_structure_height_m: 25.0,
    tower_top_orthometric_m: 44.5, tower_top_status: 'resolved_unconfirmed',
    tower_top_confirmed: false, vertical_status: 'egm2008_orthometric_resolved',
    base_type: 'ground',
    structure_geometry_available: true,
    structure_geometry_basis: 'terrain_elevation_m → tower_top_orthometric_m（既有塔顶正高证据）',
    structure_geometry_blocked_reason: null,
    installed_device_ids: ['COMM-BASELINE-4KM'],
    existing_device_ids: [],
    planned_panel_count: 1,
    device_categories: ['C', 'Radar'],
    devices: [
      { device_id: 'COMM-BASELINE-4KM', service_key: 'C:communication',
        category: 'C', installation_status: 'installed_confirmed',
        confirmed: true, position_semantics: 'tower_top',
        physical_mount_confirmed: false,
        source: 'cns_corridor_site_plan.selected_actions' },
      { device_id: 'RP-1', service_key: 'S:radar_noncooperative', category: 'Radar',
        installation_status: 'planned_selected_unconfirmed', confirmed: false,
        position_semantics: 'tower_top', azimuth_deg: 45,
        source: 'radar_surveillance_layout.selected_panels' },
    ],
    never_infers_device_from_site_presence: true,
    ...overrides,
  };
}

function candidateSite(overrides = {}) {
  return {
    object_id: 'site:reference-landing-site:RLS-1',
    site_id: 'reference-landing-site:RLS-1',
    source_id: 'RLS-1', tower_id: null,
    name: '候选起降点',
    site_kind: 'reference_landing_site', site_kind_label: '参考起降点',
    site_type: '临时起降点',
    site_state: 'candidate_unselected', site_state_label: '候选未选中',
    longitude: 122.245, latitude: 29.972,
    installation_position: 'ground_site', installation_position_label: '地面站安装',
    spatial_use_status: 'eligible', spatial_block_reason: null,
    coordinate_source: 'reference_catalog', source_crs: null, data_source: null,
    terrain_elevation_m: 8.0,
    terrain_elevation_source: 'towers.elevation_m',
    terrain_vertical_reference: 'egm2008_orthometric', terrain_status: null,
    tower_structure_height_m: null,
    tower_top_orthometric_m: null, tower_top_status: 'unknown',
    tower_top_confirmed: false, vertical_status: 'unknown', base_type: 'unknown',
    structure_geometry_available: false,
    structure_geometry_basis: null,
    structure_geometry_blocked_reason: 'ground_site_has_no_confirmed_tower_structure_evidence',
    installed_device_ids: [], existing_device_ids: [],
    planned_panel_count: 0, device_categories: [], devices: [],
    never_infers_device_from_site_presence: true,
    ...overrides,
  };
}

/* -------------------------------------------------------------------- 断言 */

test('service palette is the required C green / N yellow / RID orange / Radar blue', () => {
  assert.equal(CNS_SERVICE_STYLE.C.color, '#37d67a');
  assert.equal(CNS_SERVICE_STYLE.N.color, '#ffd166');
  assert.equal(CNS_SERVICE_STYLE.RID.color, '#ff9f43');
  assert.equal(CNS_SERVICE_STYLE.Radar.color, '#4d9dff');
  assert.equal(CNS_SERVICE_STYLE.C.label, '通信');
  assert.equal(CNS_SERVICE_STYLE.N.label, '导航');
  assert.equal(CNS_SERVICE_STYLE.RID.label, 'RID 合作监视');
  assert.equal(CNS_SERVICE_STYLE.Radar.label, 'Radar 非合作监视');
});

test('the four site states have distinguishable symbols', () => {
  for (const key of ['existing_facility', 'planned_selected',
    'candidate_unselected', 'blocked']) {
    assert.ok(CNS_SITE_STATE_STYLE[key], `${key} must be styled`);
    assert.ok(CNS_SITE_STATE_STYLE[key].label);
  }
  // 候选站址必须比既有/规划站址更小更淡，否则会被读成"已布设"。
  assert.ok(CNS_SITE_STATE_STYLE.candidate_unselected.pixelSize
    < CNS_SITE_STATE_STYLE.existing_facility.pixelSize);
  assert.ok(CNS_SITE_STATE_STYLE.candidate_unselected.pixelSize
    < CNS_SITE_STATE_STYLE.planned_selected.pixelSize);
});

test('site label font stays readable (not a tiny font)', () => {
  const size = Number(/(\d+)px/.exec(CNS_LABEL_FONT)[1]);
  assert.ok(size >= 12, `label font ${CNS_LABEL_FONT} is too small`);
});

test('business layers stay off by default so only the basemap shows on open', () => {
  assert.equal(BUSINESS_LAYER_DEFAULTS.cnsSites, false);
  assert.equal(BUSINESS_LAYER_DEFAULTS.fixedCorridor, false);
  assert.equal(Object.values(BUSINESS_LAYER_DEFAULTS).every((value) => value === false), true);
});

test('ellipsoidal height adds the point-wise geoid undulation and never defaults to 0', () => {
  assert.equal(ellipsoidalHeight(44.5, 122.25, 29.975, undulationAt), 74.5);
  assert.equal(ellipsoidalHeight(null, 122.25, 29.975, undulationAt), null);
  assert.equal(ellipsoidalHeight(44.5, 122.25, 29.975, () => null), null);
  assert.equal(ellipsoidalHeight(44.5, 122.25, 29.975, () => undefined), null);
});

test('tower with evidenced top gets a vertical structure anchored on real heights', () => {
  const viewer = new FakeViewer();
  const result = addCnsSiteEntities(viewer, [towerSite()],
    { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.towers.length, 1);
  const structure = result.towers[0];
  assert.equal(structure.cylinder.length, 44.5 - 19.5);
  // 地面锚点用**椭球高**（正高 + N），不是正高本身。
  assert.equal(structure.position.height, 19.5 + 30.0);
  const rows = structure.__cns.rows;
  assert.ok(rows.some(([key]) => key === '结构绘制依据'));
  assert.ok(rows.some(([key, value]) => key === '高度基准' && String(value).includes('逐点 N')));
});

test('tower without tower-top evidence gets no vertical structure at all', () => {
  const viewer = new FakeViewer();
  const site = towerSite({
    site_id: 'tower-colocation:T-NOTOP', source_id: 'T-NOTOP', tower_id: 'T-NOTOP',
    site_state: 'candidate_unselected',
    tower_top_orthometric_m: null, tower_top_status: 'unknown',
    vertical_status: 'base_type_unknown', base_type: 'unknown',
    structure_geometry_available: false,
    structure_geometry_basis: null,
    structure_geometry_blocked_reason: 'tower_top_orthometric_m_unresolved_or_not_a_tower_site',
    installed_device_ids: [], planned_panel_count: 0,
    device_categories: [], devices: [],
  });
  const result = addCnsSiteEntities(viewer, [site],
    { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.towers.length, 0);
  // 没有圆柱实体被创建（只允许站点锚点）。
  assert.equal(viewer.entities.values.filter((row) => row.cylinder).length, 0);
  assert.ok(result.diagnostics.skipped.some(
    (row) => row.reason === 'tower_top_orthometric_m_unresolved_or_not_a_tower_site'));
});

test('candidate ground site never inherits tower-top height and draws no device', () => {
  const viewer = new FakeViewer();
  const result = addCnsSiteEntities(viewer, [candidateSite()],
    { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.towers.length, 0);
  assert.equal(result.devices.length, 0);
  assert.equal(viewer.entities.values.filter((row) => row.cylinder).length, 0);
  const anchor = result.groundSites[0];
  // 地面锚点用地面椭球高（8.0 + 30.0），**不是**任何塔顶高度。
  assert.equal(anchor.position.height, 38.0);
});

test('device markers use the per-category colour and carry the host identity', () => {
  const viewer = new FakeViewer();
  const result = addCnsSiteEntities(viewer, [towerSite()],
    { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.devices.length, 2);
  const byId = Object.fromEntries(result.devices.map((row) => [
    row.__cns.device.device_id, row]));
  assert.equal(byId['COMM-BASELINE-4KM'].point.color.css, '#37d67a');
  assert.equal(byId['RP-1'].point.color.css, '#4d9dff');
  // 塔顶设备画在塔顶椭球高之上。
  const expected = 44.5 + 30.0 + CNS_TOWER_STYLE.deviceRingOffsetM;
  assert.equal(byId['RP-1'].position.height, expected);
  const rows = byId['RP-1'].__cns.rows;
  assert.ok(rows.some(([key]) => key === '宿主布设状态'));
  assert.ok(rows.some(([key, value]) => key === '物理挂载确认'
    && String(value).includes('未确认')));
});

test('planned ground devices are drawn as a separate unconfirmed group', () => {
  const viewer = new FakeViewer();
  const result = addCnsSiteEntities(viewer, [], {
    Cesium, undulationAt, terrainHeightAt,
    plannedDevices: [{
      object_id: 'planned-device:add_navigation_integrity_monitor:R0005:destination:N001',
      action_id: 'add_navigation_integrity_monitor:R0005:destination:N001',
      device_id: 'NAVINT-R0005-destination-N001', service_key: 'N:navigation_integrity_monitoring',
      category: 'N', name: 'add_navigation_integrity_monitor',
      longitude: 122.21, latitude: 29.955,
      installation_status: 'planned_selected_unconfirmed', confirmed: true,
      equipment_selection_status: 'not_selected',
      distinct_site_id: 'takeoff_landing_site:N001', canonical_site_id: 'node:N001',
      linked_site_id: 'node:N001', linked_site_available: true,
      position_semantics: 'ground_site',
      source: 'cns_corridor_site_plan.selected_actions',
    }],
  });
  assert.equal(result.plans.length, 1);
  assert.equal(result.plans[0].point.color.css, '#ffd166');
  const rows = result.plans[0].__cns.rows;
  assert.ok(rows.some(([key, value]) => key === '安装状态'
    && String(value) === 'planned_selected_unconfirmed'));
  assert.ok(rows.some(([key, value]) => key === '宿主站址'
    && String(value).includes('node:N001')));
});

test('device category groups expose every category used by the sites', () => {
  const groups = deviceCategoryGroups([towerSite(), candidateSite()]);
  assert.deepEqual([...groups.keys()].sort(), ['C', 'Radar']);
  assert.equal(groups.get('C')[0].device.device_id, 'COMM-BASELINE-4KM');
});

test('site popup discloses device/evidence state for the clicked site', () => {
  const rows = Object.fromEntries(sitePopupRows(towerSite()));
  assert.equal(rows['布设状态'].includes('规划选中'), true);
  assert.equal(rows['安装位置语义'], '塔顶安装');
  assert.equal(rows['塔顶正高'].includes('resolved_unconfirmed'), true);
  assert.equal(rows['三维结构几何'].includes('terrain_elevation_m'), true);
  const candidateRows = Object.fromEntries(sitePopupRows(candidateSite()));
  assert.equal(candidateRows['塔顶正高'].includes('unknown'), true);
  assert.equal(candidateRows['设备'], '无已登记设备（候选站址不推断设备已安装）');
  assert.equal(candidateRows['节点总量'].includes('站址存在 ≠ 设备已安装'), true);
});

/* --------------------------------------------------------------- 固定目标带 */

function band(overrides = {}) {
  const ring = [[122.20, 29.95], [122.24, 29.96], [122.24, 29.98],
    [122.20, 29.97], [122.20, 29.95]];
  return {
    route_id: 'R0005',
    object_id: 'protection-corridor:R0005',
    route_geometry_fingerprint: 'layeredpathv1-abc',
    route_authority: { candidate_id: 'LRC2-R0005-ALT-100' },
    route_operational_status: 'stale',
    layer_key: 'cns_fixed_corridor',
    status: 'unavailable',
    reason: '固定 1000 m 保障宽带不是 current',
    currentness: 'stale',
    currentness_entry: { currentness: 'stale', reason: 'route_operational_status_not_passed' },
    drawable_as_current_band: false,
    preview_only: true,
    preview_allowed: true,
    preview_authority: 'non_authoritative_historical_preview_not_a_current_assurance_result',
    preview_polygons: [ring],
    must_be_labelled_non_authoritative: true,
    polygons: [],
    polygon_holes: {},
    width_policy_version: 'fixed-1000m-v1',
    corridor_half_width_m: 500.0,
    corridor_total_width_m: 1000.0,
    width_semantics: 'cns_service_target_band',
    geometry_algorithm_id: 'cns_fixed_corridor_buffer_v1',
    geometry_algorithm_version: '1.0',
    geometry_fingerprint: 'corridorgeometryv1-def',
    geometry_status: 'available',
    geometry_reason: null,
    construction_crs: 'EPSG:32651',
    projected_area_m2: 19000000.0,
    projected_to_geodesic_length_ratio: 1.0001,
    boundary_ring_count: 1,
    boundary_ring_drawn: 1,
    semantics: 'cns_service_target_band_engineering_reference_not_certified_coverage',
    not_device_coverage_area: true,
    not_certified_airspace: true,
    navigation_association_radius_m: 2000.0,
    disclaimer: FIXED_CORRIDOR_DISCLAIMER,
    vertical_extent_status: 'unconfirmed',
    vertical_extent_reason: 'no_confirmed_fixed_cruise_layer_for_this_route',
    altitude_layer_id: null,
    nominal_altitude_m: null,
    vertical_reference: null,
    altitude_layer_status: null,
    altitude_layer_confirmed: null,
    operating_layer_confirmed: null,
    climb_cruise_descent_available: false,
    vertical_volume_semantics: 'horizontal_projection_only_no_vertical_guarantee',
    may_claim_3d_certified_volume: false,
    ...overrides,
  };
}

test('only current or explicitly labelled preview rings may be drawn', () => {
  assert.deepEqual(corridorRingsFor(band()).authority, 'non_authoritative_preview');
  assert.deepEqual(
    corridorRingsFor(band({ drawable_as_current_band: true, preview_only: false,
      currentness: 'current', polygons: [[[1, 2], [3, 4], [5, 6], [1, 2]]] })).authority,
    'current');
  // 几何构造被阻断（没有可绘制环）⇒ fail-closed，绝不画近似带。
  assert.equal(corridorRingsFor(band({
    preview_allowed: false, preview_polygons: [], polygons: [],
    geometry_status: 'blocked',
  })).authority, null);
});

test('non-current band is drawn as a labelled non-authoritative preview', () => {
  const viewer = new FakeViewer();
  const result = addFixedCorridorBandEntities(viewer, [band()],
    { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.groups.length, 1);
  assert.equal(result.groups[0].authority, 'non_authoritative_preview');
  assert.equal(result.entities.length, 1);
  // 橙色边框 + 更低透明度：与 current 带一眼可分。
  assert.equal(result.entities[0].polygon.outlineColor.css, '#ff9f43');
  assert.ok(result.entities[0].polygon.material.alpha
    < FIXED_CORRIDOR_STYLE.fillAlpha);
  assert.ok(result.entities[0].name.includes('非权威历史预览'));
  assert.ok(result.entities[0].__cns.rows.some(
    ([key, value]) => key === '预览限制' && value === FIXED_CORRIDOR_PREVIEW_DISCLAIMER));
});

test('blocked geometry produces no band entity at all', () => {
  const viewer = new FakeViewer();
  const result = addFixedCorridorBandEntities(viewer, [band({
    preview_allowed: false, preview_polygons: [], polygons: [],
    geometry_status: 'blocked', geometry_reason: 'metric construction CRS unresolved',
  })], { Cesium, undulationAt, terrainHeightAt });
  assert.equal(result.entities.length, 0);
  assert.equal(result.groups.length, 0);
  assert.ok(result.diagnostics.skipped.length >= 1);
});

test('confirmed cruise layer lifts the band without claiming a certified volume', () => {
  const viewer = new FakeViewer();
  const result = addFixedCorridorBandEntities(viewer, [band({
    vertical_extent_status: 'confirmed', altitude_layer_id: 'ALT-100',
    nominal_altitude_m: 100.0, vertical_reference: 'egm2008_orthometric',
    may_claim_3d_certified_volume: false,
    vertical_volume_semantics: 'nominal_cruise_layer_slice_not_a_certified_protection_volume',
  })], { Cesium, undulationAt, terrainHeightAt });
  // 底面 + 顶面（切片可见性）共 2 个实体。
  assert.equal(result.entities.length, 2);
  const slice = result.entities[1];
  assert.ok(slice.__cns.rows.some(([key, value]) => key === '切片语义'
    && String(value).includes('不是')));
  for (const entity of result.entities) {
    const rows = Object.fromEntries(entity.__cns.rows);
    assert.equal(rows['三维保障体积'].includes('不声明'), true);
    assert.equal(rows['宽度策略'].includes('fixed-1000m-v1'), true);
    assert.equal(rows['宽度策略'].includes('500'), true);
  }
});

test('band popup keeps the fixed-band semantics distinct from P17 protection', () => {
  const rows = Object.fromEntries(corridorPopupRows(band(), 'non_authoritative_preview'));
  assert.equal(rows['限制'], FIXED_CORRIDOR_DISCLAIMER);
  assert.ok(FIXED_CORRIDOR_DISCLAIMER.includes('不是禁飞区或入侵预警边界'));
  assert.ok(FIXED_CORRIDOR_DISCLAIMER.includes('不构成法规批准空间'));
  assert.ok(rows['导航关联范围'].includes('2000'));
  assert.equal(rows['导航关联范围'].includes('两件事'), true);
});

test('band setVisible respects both the layer switch and the per-band switch', () => {
  const viewer = new FakeViewer();
  const result = addFixedCorridorBandEntities(viewer, [band()],
    { Cesium, undulationAt, terrainHeightAt });
  result.setVisible(false);
  assert.equal(result.entities.every((row) => row.show === false), true);
  result.setVisible(true);
  assert.equal(result.entities.every((row) => row.show === true), true);
  result.groups[0].visible = false;
  result.setVisible(true);
  assert.equal(result.entities.every((row) => row.show === false), true);
});
