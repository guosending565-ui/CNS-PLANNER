// =========================================================
// Round38-N2：导航服务监测规划二维界面与地图图层定向测试
//
// 锁定：
//   1. API 请求及参数修改正确（端点、payload、只发已知键）；
//   2. 同一航路不同几何不得混用（authority + 完整几何指纹双重比对）；
//   3. 端点强制站址不可关闭（前端无关闭入口 + payload 不含关闭值）；
//   4. CRS blocked / 风险 stale / 时间 unknown 都如实展示；
//   5. 2000 m 导航关联与 500 m 固定半宽严格分离；
//   6. 地图图层按用户操作开启，不改变默认底图状态；
//   7. 已规划 / 既有设施 / 不可用站址图例区分正确；
//   8. 不产生 ProjectState 写入（前端不调用任何写接口）。
//
// 与本目录其它前端测试一致：没有 jsdom、不伪造浏览器；绘制用最小 stub canvas context。
// =========================================================

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  FORCE_ENDPOINT_NOTE, NAVIGATION_PARAMETER_DEFAULTS, NAVIGATION_PARAMETER_FIELDS,
  NAVIGATION_MONITORING_SIMULATION_ENDPOINT, SHARED_SITE_CANDIDATES_ENDPOINT,
  FIXED_CORRIDOR_GEOMETRY_ENDPOINT, REFERENCE_CRS_BLOCKED_TEXT,
  WIDTH_VS_ASSOCIATION_NOTE, PROVISIONING_TEXT, TEST_FIXTURE_LABEL,
  fixedCorridorGeometryModel, fullWidthServiceStatusModel,
  navigationParameterDraft, navigationParameterPayload, navigationSimulationModel,
  percentText, metricText, renderNavigationMonitoringPanel, riskPriorityStatusText,
  routeIdentityConsistency, routeIdentityModel, sharedSiteCandidateModel,
  spatialBlockReasonText, toleranceStatusText, simulationStatusText,
  bindNavigationMonitoringPanel, resetNavigationMonitoringBinding,
  refreshNavigationCandidateSummary,
} from '../cns_planner/web/js/workflow/navigation_monitoring.js';

import {
  NAVIGATION_DEVICE_SHAPES, NAVIGATION_MONITORING_LEGEND_CHECKBOX,
  NAVIGATION_MONITORING_LEGEND_SYMBOLS, associationSegments,
  drawNavigationMonitoringLayer, hitNavigationMonitoringFeature,
  navigationMonitoringFeatureTooltip, navigationMonitoringLayerModel,
  pointToSegmentDistance, withScreenCoordinates,
} from '../cns_planner/web/js/map/navigation_monitoring_overlay.js';

const INDEX_HTML = readFileSync(
  new URL('../cns_planner/web/index.html', import.meta.url), 'utf8');
const MAIN_SOURCE = readFileSync(
  new URL('../cns_planner/web/js/main.js', import.meta.url), 'utf8');
const PANEL_SOURCE = readFileSync(
  new URL('../cns_planner/web/js/workflow/navigation_monitoring.js', import.meta.url), 'utf8');
const OVERLAY_SOURCE = readFileSync(
  new URL('../cns_planner/web/js/map/navigation_monitoring_overlay.js', import.meta.url), 'utf8');

const ROUTE_PATH = [[122.10, 30.00], [122.20, 30.00], [122.20, 30.10]];
const ROUTE_LENGTH_M = 20000;

// ---- fixtures ---------------------------------------------------------------

function candidateSnapshot(overrides = {}) {
  return {
    status: 'passed',
    candidate_semantics: 'read_only_potential_hosts',
    corridor_width_policy_version: 'fixed-1000m-v1',
    corridor_half_width_m: 500,
    full_width_service_assurance_status: 'not_evaluated',
    candidates: [
      {candidate_id: 'node:N005', source_id: 'N005', candidate_kind: 'project_node',
       coordinate: [122.10, 30.00], spatial_use_status: 'eligible'},
      {candidate_id: 'node:N006', source_id: 'N006', candidate_kind: 'project_node',
       coordinate: [122.20, 30.10], spatial_use_status: 'eligible'},
      {candidate_id: 'tower-colocation:T1', source_id: 'T1',
       candidate_kind: 'tower_colocation', coordinate: [122.15, 30.00],
       spatial_use_status: 'eligible'},
      {candidate_id: 'reference-landing-site:RLS-1', source_id: 'RLS-1',
       candidate_kind: 'reference_landing_site', coordinate: [122.12, 30.02],
       spatial_use_status: 'blocked', spatial_block_reason: 'reference_source_crs_unresolved'},
      {candidate_id: 'reference-landing-site:RLS-2', source_id: 'RLS-2',
       candidate_kind: 'reference_landing_site', coordinate: [122.13, 30.03],
       spatial_use_status: 'blocked', spatial_block_reason: 'reference_source_crs_unresolved'},
    ],
    spatial_candidates: [
      {candidate_id: 'node:N005'}, {candidate_id: 'node:N006'},
      {candidate_id: 'tower-colocation:T1'},
    ],
    counts: {total: 5, nodes: 2, reference_landing_sites: 2, towers: 1,
             spatial_candidates: 3, spatial_blocked: 2},
    source_counts: {project_nodes: 2, reference_landing_site_records: 98, tower_records: 373},
    reason_counts: {reference_source_crs_unresolved: 2},
    ...overrides,
  };
}

function simulationResult(overrides = {}) {
  return {
    status: 'passed',
    reason: null,
    route_identity: {
      route_authority: 'canonical_operational_route', route_id: 'R0005',
      route_geometry_fingerprint: 'layeredpathv1-abc123def456',
      point_count: 11, currentness: 'current', source_crs: 'OGC:CRS84',
      construction_crs: 'EPSG:32651', candidate_id: 'LRC2-R0005-ALT-100-layeredcandv1-91',
    },
    candidate_snapshot_fingerprint: 'navsites-deadbeef',
    parameter_fingerprint: 'navmon-cafebabe',
    result_fingerprint: 'navmon-feedface',
    candidate_counts: {total: 5, spatial_candidates: 3},
    parameters: {
      monitor_association_radius_m: 2000, gap_tolerance_s: 10, route_speed_mps: 15,
      risk_priority_weight: 3, max_optional_monitor_sites: 4, force_endpoint_monitors: true,
      parameter_sources: {
        monitor_association_radius_m: 'default', gap_tolerance_s: 'default',
        route_speed_mps: 'default', risk_priority_weight: 'default',
        max_optional_monitor_sites: 'default', force_endpoint_monitors: 'fixed_constraint',
      },
    },
    corridor_width_policy_version: 'fixed-1000m-v1',
    corridor_half_width_m: 500,
    full_width_service_assurance_status: 'not_evaluated',
    semantic_disclaimer: '中心线关联仿真，不代表左右各500米全宽CNS保障能力',
    mandatory_requirements: [
      {position: 'start', site_id: 'node:N005', status: 'satisfied', reason: null},
      {position: 'end', site_id: 'node:N006', status: 'satisfied', reason: null},
    ],
    selected_mandatory_site_ids: ['node:N005', 'node:N006'],
    selected_optional_sites: [{site_id: 'tower-colocation:T1', gain_score: 1234.5}],
    pruned_optional_site_ids: [],
    unselected_candidate_ids: [],
    selection_stop_reason: 'no_positive_incremental_association',
    devices: [
      {device_id: 'simulation:N:node:N005', site_id: 'node:N005', service_key: 'N',
       provisioning: 'simulation_planned'},
      {device_id: 'simulation:N:node:N006', site_id: 'node:N006', service_key: 'N',
       provisioning: 'simulation_planned'},
      {device_id: 'EXISTING-N-1', site_id: 'tower-colocation:T1', service_key: 'N',
       provisioning: 'reused_existing'},
    ],
    merged_intervals_m: [[0, 9000], [9500, 20000]],
    associated_length_m: 19500,
    associated_ratio: 0.975,
    route_length_m: ROUTE_LENGTH_M,
    risk_associated_length_m: 0,
    gaps: [{
      start_m: 9000, end_m: 9500, length_m: 500, duration_s: 33.33,
      speed_source: 'route_speed_assumption', gap_tolerance_s: 10,
      tolerance_status: 'exceeded',
    }],
    longest_gap_m: 500,
    longest_gap_duration_s: 33.33,
    all_gaps_within_tolerance: false,
    risk_priority_status: 'available',
    risk_identity: {profile_id: 'RRP-1', profile_fingerprint: 'routeprofilev1-xyz'},
    risk_unavailable_reasons: [],
    three_dimensional_assurance_status: 'not_evaluated',
    three_dimensional_assurance_reason: 'route_altitude_profile_unavailable',
    route_elevation_used_for_association: false,
    writes_performed: 0,
    ...overrides,
  };
}

function blockedResult(overrides = {}) {
  return {
    status: 'blocked',
    reason: 'mandatory_endpoint_spatial_evidence_unavailable',
    route_identity: {
      route_authority: 'canonical_operational_route', route_id: 'R0005',
      route_geometry_fingerprint: 'layeredpathv1-abc123def456', point_count: 11,
    },
    parameters: {
      monitor_association_radius_m: 2000, gap_tolerance_s: 10, route_speed_mps: 15,
      risk_priority_weight: 3, max_optional_monitor_sites: 4, force_endpoint_monitors: true,
      parameter_sources: {force_endpoint_monitors: 'fixed_constraint'},
    },
    corridor_width_policy_version: 'fixed-1000m-v1',
    corridor_half_width_m: 500,
    full_width_service_assurance_status: 'not_evaluated',
    mandatory_requirements: [
      {position: 'start', site_id: 'node:N005', status: 'blocked',
       reason: 'spatial_evidence_unavailable'},
      {position: 'end', site_id: 'node:N006', status: 'blocked',
       reason: 'spatial_evidence_unavailable'},
    ],
    selected_mandatory_site_ids: [], selected_optional_sites: [], devices: [],
    merged_intervals_m: [], associated_length_m: null, associated_ratio: null,
    route_length_m: null, gaps: [], longest_gap_m: null, longest_gap_duration_s: null,
    risk_priority_status: 'unavailable',
    risk_identity: null,
    risk_unavailable_reasons: ['current_candidate_unavailable', 'current_risk_profile_unavailable'],
    three_dimensional_assurance_status: 'not_evaluated',
    writes_performed: 0,
    ...overrides,
  };
}

function stubContext() {
  const calls = {save: 0, restore: 0, beginPath: 0, moveTo: 0, lineTo: 0,
                 closePath: 0, fill: 0, stroke: 0, setLineDash: 0};
  const dashes = [];
  const ctx = {
    font: '', fillStyle: '', strokeStyle: '', lineWidth: 1, globalAlpha: 1, lineCap: 'butt',
    save() { calls.save += 1; }, restore() { calls.restore += 1; },
    beginPath() { calls.beginPath += 1; }, closePath() { calls.closePath += 1; },
    moveTo() { calls.moveTo += 1; }, lineTo() { calls.lineTo += 1; },
    arc() {}, rect() {}, fill() { calls.fill += 1; }, stroke() { calls.stroke += 1; },
    setLineDash(dash) { calls.setLineDash += 1; dashes.push(Array.isArray(dash) ? dash.slice() : dash); },
    fillText() {}, strokeText() {}, measureText(text) { return {width: String(text).length * 6}; },
    fillRect() {}, strokeRect() {}, drawImage() {},
  };
  return {ctx, calls, dashes};
}

const screenPoint = coordinate => [coordinate[0] * 100, coordinate[1] * 100];

// =============================================================================
// 1. API 请求与参数
// =============================================================================

test('the panel consumes exactly the two N1 read-only endpoints', () => {
  assert.equal(SHARED_SITE_CANDIDATES_ENDPOINT, '/api/cns-shared-site-candidates');
  assert.equal(NAVIGATION_MONITORING_SIMULATION_ENDPOINT, '/api/navigation-monitoring-simulation');
  assert.equal(FIXED_CORRIDOR_GEOMETRY_ENDPOINT, '/api/fixed-corridor-geometry');
  for (const endpoint of [SHARED_SITE_CANDIDATES_ENDPOINT,
    NAVIGATION_MONITORING_SIMULATION_ENDPOINT, FIXED_CORRIDOR_GEOMETRY_ENDPOINT]) {
    assert.ok(PANEL_SOURCE.includes(endpoint), `panel must declare ${endpoint}`);
  }
});

test('the frontend never reimplements siting: no algorithm constants in the panel', () => {
  // 选址算法只允许在后端。注意 ``merged_intervals_m`` / ``selected_optional_sites``
  // 是**后端响应字段名**（只读转印），不算重实现；这里禁止的是算法实现痕迹。
  for (const forbidden of ['_circle_intervals', 'discriminant', 'sqrt(',
    'selectOptionalSites', 'associateRoute', 'gainScore =', 'incrementalGain']) {
    assert.equal(PANEL_SOURCE.includes(forbidden), false,
      `panel must not reimplement siting (${forbidden})`);
  }
  assert.ok(PANEL_SOURCE.includes('不重新实现选址算法'));
});

test('parameter defaults match the N1 contract exactly', () => {
  assert.deepEqual(NAVIGATION_PARAMETER_DEFAULTS, {
    monitor_association_radius_m: 2000, gap_tolerance_s: 10, route_speed_mps: 15,
    risk_priority_weight: 3, max_optional_monitor_sites: 4, force_endpoint_monitors: true,
  });
});

test('parameter payload only carries registered keys and drops blanks', () => {
  const payload = navigationParameterPayload({
    monitor_association_radius_m: '2500', gap_tolerance_s: '', route_speed_mps: '18',
    risk_priority_weight: '3', max_optional_monitor_sites: '2.9',
    force_endpoint_monitors: false, unknown_key: 1,
  });
  assert.deepEqual(payload, {
    monitor_association_radius_m: 2500, route_speed_mps: 18,
    risk_priority_weight: 3, max_optional_monitor_sites: 2,
  });
  // force_endpoint_monitors 是固定约束：前端**从不**把它放进 payload。
  assert.equal('force_endpoint_monitors' in payload, false);
  assert.equal('unknown_key' in payload, false);
});

test('parameter draft round-trips backend parameters and forces the fixed constraint', () => {
  const draft = navigationParameterDraft({
    monitor_association_radius_m: 3000, gap_tolerance_s: 20,
    max_optional_monitor_sites: 0,
  });
  assert.equal(draft.monitor_association_radius_m, 3000);
  assert.equal(draft.gap_tolerance_s, 20);
  assert.equal(draft.max_optional_monitor_sites, 0);
  assert.equal(draft.route_speed_mps, 15, 'missing keys fall back to the documented default');
  assert.equal(draft.force_endpoint_monitors, true);
});

// =============================================================================
// 2. 端点强制站址不可关闭
// =============================================================================

test('the endpoint monitors are rendered read-only with no way to disable them', () => {
  const field = NAVIGATION_PARAMETER_FIELDS.find(item => item.key === 'force_endpoint_monitors');
  assert.ok(field, 'the fixed constraint must be a declared parameter field');
  assert.equal(field.editable, false, 'the user must not be able to edit it');
  assert.match(FORCE_ENDPOINT_NOTE, /不可关闭/);

  const html = renderNavigationMonitoringPanel({}, {result: simulationResult()});
  assert.match(html, /id="navigationForceEndpointMonitors"[^>]*checked[^>]*disabled/,
    'the checkbox must be checked and disabled');
  assert.doesNotMatch(html, /id="navigation_force_endpoint_monitors"/,
    'no editable input may exist for the fixed constraint');
  assert.ok(html.includes(FORCE_ENDPOINT_NOTE));
});

test('no code path sends force_endpoint_monitors=false', () => {
  for (const source of [PANEL_SOURCE, OVERLAY_SOURCE]) {
    assert.doesNotMatch(source, /force_endpoint_monitors\s*:\s*false/);
  }
});

// =============================================================================
// 3. 同一航路不同几何不得混用
// =============================================================================

test('route identity keeps authority and the complete geometry fingerprint separate', () => {
  const model = routeIdentityModel(simulationResult().route_identity);
  assert.equal(model.routeAuthority, 'canonical_operational_route');
  assert.equal(model.routeAuthorityText, '正式运行航路（canonical）');
  assert.equal(model.routeGeometryFingerprint, 'layeredpathv1-abc123def456');
  assert.equal(model.fingerprintPrefix, 'layeredpathv1-');
  assert.notEqual(model.routeAuthority, model.routeGeometryFingerprint,
    'authority must never be conflated with the geometry fingerprint');
});

test('a different geometry for the same route id is rejected', () => {
  const authoritative = simulationResult().route_identity;
  const same = routeIdentityConsistency(authoritative, {
    route_id: 'R0005', route_authority: 'canonical_operational_route',
    route_geometry_fingerprint: 'layeredpathv1-abc123def456',
  });
  assert.deepEqual(same, {consistent: true, reason: null});

  const differentGeometry = routeIdentityConsistency(authoritative, {
    route_id: 'R0005', route_authority: 'canonical_operational_route',
    route_geometry_fingerprint: 'layeredpathv1-999999999999',
  });
  assert.equal(differentGeometry.consistent, false);
  assert.equal(differentGeometry.reason, 'route_geometry_fingerprint_mismatch');

  const differentAuthority = routeIdentityConsistency(authoritative, {
    route_id: 'R0005', route_authority: 'engineering_demo_projection',
    route_geometry_fingerprint: 'layeredpathv1-abc123def456',
  });
  assert.equal(differentAuthority.consistent, false);
  assert.equal(differentAuthority.reason, 'route_authority_mismatch');
});

test('route identity comparison fails closed when either fingerprint is missing', () => {
  const authoritative = simulationResult().route_identity;
  assert.deepEqual(routeIdentityConsistency(authoritative, {route_id: 'R0005'}),
    {consistent: false, reason: 'requested_geometry_fingerprint_missing'});
  assert.deepEqual(routeIdentityConsistency({route_id: 'R0005'}, authoritative),
    {consistent: false, reason: 'authoritative_geometry_fingerprint_missing'});
});

test('the panel renders both the authority and the full geometry fingerprint', () => {
  const html = renderNavigationMonitoringPanel({}, {result: simulationResult()});
  assert.match(html, /航路权威身份/);
  assert.match(html, /route authority/);
  assert.match(html, /完整几何指纹/);
  assert.match(html, /layeredpathv1-/);
  assert.match(html, /正式运行航路（canonical）/);
  // 同一航路不同几何不得混用：界面必须提供显式的 route_id 输入。
  assert.match(html, /id="navigationRouteId"/);
  assert.match(html, /id="navigationRouteSource"/);
});

// =============================================================================
// 4. CRS blocked / 风险 stale / 时间 unknown 如实展示
// =============================================================================

test('the CRS blocked sentence is shown verbatim when reference CRS is unresolved', () => {
  const model = sharedSiteCandidateModel(candidateSnapshot());
  assert.equal(model.reasonRows.length, 1);
  assert.equal(model.reasonRows[0].reason, 'reference_source_crs_unresolved');
  assert.match(model.reasonRows[0].text, /坐标系尚未确认/);

  const html = renderNavigationMonitoringPanel({}, {
    candidates: candidateSnapshot(), result: blockedResult(),
  });
  assert.ok(html.includes(REFERENCE_CRS_BLOCKED_TEXT),
    'the exact CRS blocked sentence must appear');
  assert.ok(html.includes('人工 CRS 确认入口保留'));
  assert.ok(html.includes('不自动假设 EPSG:4326/4490'));
});

test('a blocked simulation stays blocked and never renders as passed', () => {
  const model = navigationSimulationModel(blockedResult());
  assert.equal(model.status, 'blocked');
  assert.match(model.statusText, /已阻断/);
  assert.equal(model.statusText.includes('通过'), false);
  assert.match(model.reasonText, /起终点监测设备缺少可用的空间证据/);
  assert.equal(model.writesPerformed, 0);

  const html = renderNavigationMonitoringPanel({}, {result: blockedResult()});
  assert.match(html, /已阻断/);
  assert.equal(/仿真结论<\/b><\/span><small>通过/.test(html), false);
});

test('the unavailable risk profile is reported as not applied, with no fake high-risk segments', () => {
  const model = navigationSimulationModel(blockedResult());
  assert.equal(model.riskPriorityApplied, false);
  assert.equal(model.riskPriorityStatus, 'unavailable');
  assert.equal(model.riskPriorityStatusText, '未应用风险优先排序');
  assert.deepEqual(model.riskUnavailableRows.map(row => row.text), [
    '当前候选航路明细不可用', '没有 current 的风险画像',
  ]);
  assert.equal(riskPriorityStatusText('unavailable'), '未应用风险优先排序');

  const html = renderNavigationMonitoringPanel({}, {result: blockedResult()});
  assert.match(html, /未应用风险优先排序/);
  assert.match(html, /绝不生成虚假的高风险航段/);
});

test('an unknown gap duration stays undetermined and never becomes satisfied', () => {
  assert.equal(toleranceStatusText('undetermined'), '不可判定（速度证据不足）');
  assert.equal(toleranceStatusText('satisfied'), '在容忍时间内');
  assert.equal(toleranceStatusText('exceeded'), '超出容忍时间');

  const unknown = simulationResult({
    status: 'undetermined',
    gaps: [{start_m: 9000, end_m: 9500, length_m: 500, duration_s: null,
            speed_source: 'segment_speed_evidence_incomplete', gap_tolerance_s: 10,
            tolerance_status: 'undetermined'}],
    longest_gap_duration_s: 0,
    all_gaps_within_tolerance: false,
  });
  const model = navigationSimulationModel(unknown);
  assert.equal(model.status, 'undetermined');
  assert.equal(model.gapRows[0].toleranceStatus, 'undetermined');
  assert.match(model.gapRows[0].toleranceStatusText, /不可判定/);
  assert.equal(model.allGapsWithinTolerance, false, 'unknown must not be reported as within tolerance');

  const html = renderNavigationMonitoringPanel({}, {result: unknown});
  assert.match(html, /不可判定（速度证据不足）/);
});

test('missing metrics render as an em dash instead of zero', () => {
  assert.equal(metricText(null), '—');
  assert.equal(metricText(undefined), '—');
  assert.equal(metricText(''), '—');
  assert.equal(metricText(0, 0, '个'), '0 个');
  assert.equal(percentText(null), '—');
  assert.equal(percentText(0), '0.00%');

  const model = navigationSimulationModel(blockedResult());
  assert.equal(model.associatedLengthM, null);
  assert.equal(model.associatedRatio, null);
  assert.equal(model.longestGapDurationS, null);
  const html = renderNavigationMonitoringPanel({}, {result: blockedResult()});
  assert.match(html, /—/);
});

// =============================================================================
// 5. 2000 m 关联与 500 m 固定半宽严格分离
// =============================================================================

test('the association radius and the fixed band are never conflated', () => {
  const model = navigationSimulationModel(simulationResult());
  assert.equal(model.parameters.monitor_association_radius_m, 2000);
  assert.equal(model.corridorHalfWidthM, 500);
  assert.equal(model.corridorWidthPolicyVersion, 'fixed-1000m-v1');
  assert.equal(model.fullWidthServiceAssuranceStatus, 'not_evaluated');
  assert.equal(model.semanticDisclaimer,
    '中心线关联仿真，不代表左右各500米全宽CNS保障能力');

  assert.ok(WIDTH_VS_ASSOCIATION_NOTE.includes('2000'));
  assert.ok(WIDTH_VS_ASSOCIATION_NOTE.includes('500'));
  assert.ok(WIDTH_VS_ASSOCIATION_NOTE.includes('不得'));
  assert.ok(WIDTH_VS_ASSOCIATION_NOTE.includes('不是真实认证导航覆盖区'));
});

test('the association ratio never substitutes for the full-width coverage', () => {
  const model = navigationSimulationModel(simulationResult());
  assert.equal(model.associatedRatio, 0.975);
  // 全宽状态**没有** current 的 P14/P15 结果 ⇒ 必须保持 not_evaluated。
  const fullWidth = fullWidthServiceStatusModel({
    corridorStatus: 'stale', corridorCurrent: false, identityMatched: false,
  });
  assert.equal(fullWidth.evaluated, false);
  assert.equal(fullWidth.status, 'not_evaluated');
  assert.equal(fullWidth.reason, 'corridor_result_not_current');
  assert.match(fullWidth.note, /不得用导航关联率替代/);

  // 身份不匹配同样不得给出有效结论。
  const mismatched = fullWidthServiceStatusModel({
    corridorStatus: 'passed', corridorCurrent: true, identityMatched: false,
  });
  assert.equal(mismatched.status, 'not_evaluated');
  assert.equal(mismatched.reason, 'corridor_identity_not_matched');

  // 只有 current + 身份匹配才允许有效结论。
  const evaluated = fullWidthServiceStatusModel({
    corridorStatus: 'passed', corridorCurrent: true, identityMatched: true,
  });
  assert.equal(evaluated.evaluated, true);
  assert.equal(evaluated.status, 'passed');
});

test('the fixed corridor band is only drawable when the backend says current', () => {
  const base = {
    status: 'available', route_id: 'R0005', corridor_half_width_m: 500,
    corridor_total_width_m: 1000, width_policy_version: 'fixed-1000m-v1',
    geometry_fingerprint: 'corridorgeometryv1-abc',
    geometry: {type: 'Polygon', coordinates: [[[122.1, 30.0], [122.2, 30.0], [122.2, 30.1], [122.1, 30.0]]]},
  };
  const stale = fixedCorridorGeometryModel({...base, currentness: 'stale',
    currentness_reason: 'corridor_result_not_current'});
  assert.equal(stale.drawable, false, 'a stale band must not be drawn');
  assert.equal(stale.currentnessReason, 'corridor_result_not_current');

  const current = fixedCorridorGeometryModel({...base, currentness: 'current'});
  assert.equal(current.drawable, true);
  assert.equal(current.corridorHalfWidthM, 500);

  const blocked = fixedCorridorGeometryModel({...base, status: 'blocked',
    currentness: 'stale', geometry: null});
  assert.equal(blocked.drawable, false);
  assert.equal(blocked.geometry, null);

  // 面板必须同时说明两者不同，且不把 stale 宽带画成有效边界。
  const html = renderNavigationMonitoringPanel({}, {
    result: simulationResult(),
    fixedCorridorGeometry: {...base, currentness: 'stale',
      currentness_reason: 'corridor_result_not_current'},
  });
  assert.ok(html.includes(WIDTH_VS_ASSOCIATION_NOTE));
  assert.match(html, /corridor_result_not_current/);
});

test('the map layer never draws a 2000 m association circle or coverage polygon', () => {
  const model = navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  });
  assert.equal(model.drawable, true);
  // 只有航路 + 航段 + 设备：没有任何 2000 m 半径圆或覆盖多边形。
  assert.equal('associationCircle' in model, false);
  assert.equal('coveragePolygon' in model, false);
  // 图层模型不得携带关联半径**字段**（routeLengthM 里的数字与半径无关）。
  const keys = new Set();
  const walk = value => {
    if (Array.isArray(value)) { value.forEach(walk); return; }
    if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) { keys.add(key); walk(item); }
    }
  };
  walk(model);
  assert.equal(keys.has('monitor_association_radius_m'), false);
  assert.equal(keys.has('association_radius_m'), false);
  assert.equal(keys.has('parameters'), false);
  // 关联半径只作为参数留在面板，图层不消费它。
  assert.doesNotMatch(OVERLAY_SOURCE, /monitor_association_radius_m/);
});

// =============================================================================
// 6. 图层按用户操作开启，默认状态不变
// =============================================================================

test('the navigation monitoring layer is declared and starts unchecked', () => {
  assert.match(INDEX_HTML, new RegExp(`id="${NAVIGATION_MONITORING_LEGEND_CHECKBOX}"`),
    'index.html must declare the layer checkbox');
  const tag = new RegExp(`<input[^>]*id="${NAVIGATION_MONITORING_LEGEND_CHECKBOX}"[^>]*>`)
    .exec(INDEX_HTML);
  assert.ok(tag, 'the checkbox tag must be findable');
  assert.equal(/\bchecked\b/.test(tag[0]), false, 'the layer must default to off');
  assert.match(MAIN_SOURCE, new RegExp(`'${NAVIGATION_MONITORING_LEGEND_CHECKBOX}'`),
    'main.js LAYER_IDS must contain the layer id');
});

test('the layer model draws nothing until the user enables it', () => {
  const off = navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: false,
  });
  assert.equal(off.drawable, false);
  assert.equal(off.reason, 'layer_disabled');
  assert.deepEqual(off.devices, []);
  assert.deepEqual(off.segments, []);

  const {ctx, calls} = stubContext();
  const result = drawNavigationMonitoringLayer({ctx, screenPoint, model: off});
  assert.equal(result.drawn, false);
  assert.equal(calls.save, 0, 'a disabled layer must not touch the canvas at all');

  const on = navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  });
  const drawn = drawNavigationMonitoringLayer({ctx, screenPoint, model: on});
  assert.equal(drawn.drawn, true);
  assert.ok(drawn.devices > 0);
  assert.ok(drawn.segments > 0);
  assert.ok(calls.save > 0);
});

test('the default basemap state is untouched by the new layer', () => {
  // 只有 online 默认勾选；新图层不得改变这条基线。
  const boxes = [...INDEX_HTML.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)]
    .map(match => match[0]);
  const checked = boxes.filter(tag => /\bchecked\b/.test(tag))
    .map(tag => /\bid="([^"]+)"/.exec(tag)?.[1]);
  assert.deepEqual(checked, ['online'],
    `only the online basemap may be checked by default, got: ${checked.join(', ')}`);
  // 重开项目时新图层必须回到关闭（与其它分析层同一策略）。
  const reset = /const PROJECT_REOPEN_RESET_LAYER_IDS=\[([^\]]*)\]/.exec(MAIN_SOURCE);
  assert.ok(reset, 'PROJECT_REOPEN_RESET_LAYER_IDS must stay a single literal list');
  assert.match(reset[1], new RegExp(`'${NAVIGATION_MONITORING_LEGEND_CHECKBOX}'`));
});

// =============================================================================
// 7. 图例区分：已规划 / 既有设施 / 不可用站址
// =============================================================================

test('the legend distinguishes planned, reused and blocked devices', () => {
  assert.equal(PROVISIONING_TEXT.simulation_planned, '仿真规划（尚未建成）');
  assert.equal(PROVISIONING_TEXT.reused_existing, '复用既有设备');

  const model = navigationSimulationModel(simulationResult());
  const bySite = new Map(model.siteRows.map(row => [row.siteId, row]));
  assert.equal(bySite.get('node:N005').provisioningText, '仿真规划（尚未建成）');
  assert.equal(bySite.get('node:N005').roleText, '起终点强制设备');
  assert.equal(bySite.get('tower-colocation:T1').provisioningText, '复用既有设备');
  assert.equal(bySite.get('tower-colocation:T1').roleText, '可选监测站址');

  const html = renderNavigationMonitoringPanel({}, {
    candidates: candidateSnapshot(), result: simulationResult(),
  });
  assert.match(html, /仿真规划（尚未建成）/);
  assert.match(html, /复用既有设备/);
});

test('the mandatory and tower devices use different shapes, not just different colours', () => {
  assert.notEqual(NAVIGATION_DEVICE_SHAPES.mandatory, NAVIGATION_DEVICE_SHAPES.tower);
  assert.equal(NAVIGATION_DEVICE_SHAPES.mandatory, 'triangle');
  assert.equal(NAVIGATION_DEVICE_SHAPES.tower, 'square');

  const model = navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  });
  const mandatory = model.devices.filter(item => item.role === 'mandatory');
  const optional = model.devices.filter(item => item.role === 'optional');
  assert.equal(mandatory.length, 2);
  assert.equal(optional.length, 1);
  assert.deepEqual([...new Set(mandatory.map(item => item.shape))], ['triangle']);
  assert.deepEqual([...new Set(optional.map(item => item.shape))], ['square']);
});

test('blocked candidates keep their count and reason in the legend and panel', () => {
  const model = sharedSiteCandidateModel(candidateSnapshot());
  assert.equal(model.counts.spatial_blocked, 2);
  assert.equal(model.blockedCandidates.length, 2);
  assert.equal(model.reasonRows[0].count, 2);
  assert.equal(spatialBlockReasonText('reference_source_crs_unresolved'),
    '参考数据源坐标系尚未确认（CRS 未裁定）');

  const html = renderNavigationMonitoringPanel({}, {
    candidates: candidateSnapshot(), result: simulationResult(),
  });
  assert.match(html, /不可用候选/);
  assert.match(html, /参考数据源坐标系尚未确认（CRS 未裁定）/);
  assert.match(html, /data-navigation-block-reason="reference_source_crs_unresolved"/);
  assert.match(html, /既有铁塔/);
  assert.match(html, /参考起降点/);
});

test('the legend symbols cover route, devices, segments and the fixed band', () => {
  for (const id of ['navigation-route', 'navigation-mandatory-device',
    'navigation-tower-device', 'navigation-associated-segment',
    'navigation-unassociated-segment', 'navigation-fixed-corridor']) {
    assert.ok(NAVIGATION_MONITORING_LEGEND_SYMBOLS.includes(id), `missing legend symbol ${id}`);
  }
});

test('the map wording never claims certified safety for a segment', () => {
  for (const source of [PANEL_SOURCE, OVERLAY_SOURCE]) {
    for (const forbidden of ['安全航段', '已认证', '认证覆盖', 'certified coverage']) {
      assert.equal(source.includes(forbidden), false,
        `must not use misleading wording: ${forbidden}`);
    }
  }
  // 「未关联」必须附带"不表示不安全"的说明。
  assert.match(OVERLAY_SOURCE, /不表示该航段不安全/);
  assert.match(OVERLAY_SOURCE, /未关联/);
});

// =============================================================================
// 8. 不产生 ProjectState 写入
// =============================================================================

test('the panel and overlay never call any write endpoint', () => {
  for (const source of [PANEL_SOURCE, OVERLAY_SOURCE]) {
    for (const method of ['method:\'POST\'', 'method: "POST"', 'PUT', 'DELETE']) {
      assert.equal(source.includes(method), false, `no write verb allowed: ${method}`);
    }
    for (const forbidden of ['mutate(', 'resourceAction(', 'applyWorkflowSnapshot',
      'session.save', 'state[']) {
      assert.equal(source.includes(forbidden), false, `no state write allowed: ${forbidden}`);
    }
  }
  assert.match(PANEL_SOURCE, /仿真结果不写入 ProjectState/);
});

test('the simulation response declares zero writes', () => {
  assert.equal(navigationSimulationModel(simulationResult()).writesPerformed, 0);
  assert.equal(navigationSimulationModel(blockedResult()).writesPerformed, 0);
  const html = renderNavigationMonitoringPanel({}, {result: simulationResult()});
  assert.match(html, /写入 ProjectState/);
});

// =============================================================================
// 9. 地图要素：关联 / 未关联航段与缺口详情
// =============================================================================

test('association segments map backend intervals onto the route geometry', () => {
  const segments = associationSegments(ROUTE_PATH, ROUTE_LENGTH_M, {
    mergedIntervals: [[0, 9000], [9500, 20000]],
    gaps: [{start_m: 9000, end_m: 9500}],
  });
  assert.equal(segments.length, 3);
  assert.deepEqual(segments.map(item => item.kind),
    ['associated', 'associated', 'unassociated']);
  for (const segment of segments) {
    assert.ok(Array.isArray(segment.from) && segment.from.length === 2);
    assert.ok(Array.isArray(segment.to) && segment.to.length === 2);
  }
  // 未关联段必须正好落在缺口里程上。
  const gap = segments.find(item => item.kind === 'unassociated');
  assert.equal(gap.startM, 9000);
  assert.equal(gap.endM, 9500);
});

test('association segments are empty without a usable route geometry', () => {
  assert.deepEqual(associationSegments([], ROUTE_LENGTH_M, {mergedIntervals: [[0, 1]]}), []);
  assert.deepEqual(associationSegments(ROUTE_PATH, null, {mergedIntervals: [[0, 1]]}), []);
  assert.deepEqual(associationSegments(ROUTE_PATH, ROUTE_LENGTH_M, {mergedIntervals: [[5, 5]]}), []);
});

test('unassociated segments are dashed while associated ones are solid', () => {
  const model = navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  });
  const {ctx, dashes} = stubContext();
  drawNavigationMonitoringLayer({ctx, screenPoint, model});
  const nonEmpty = dashes.filter(dash => Array.isArray(dash) && dash.length);
  assert.ok(nonEmpty.length >= 1, 'at least the gap segment must be dashed');
});

test('clicking a device reports its source, role and evidence state', () => {
  const model = withScreenCoordinates(navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  }), screenPoint);
  const device = model.devices.find(item => item.siteId === 'node:N005');
  const hit = hitNavigationMonitoringFeature(model, device.screen);
  assert.ok(hit, 'clicking on a device must hit it');
  assert.equal(hit.kind, 'device');

  const html = navigationMonitoringFeatureTooltip(hit, navigationSimulationModel(simulationResult()));
  assert.match(html, /设备来源/);
  assert.match(html, /关联身份/);
  assert.match(html, /空间证据状态/);
  assert.match(html, /仿真规划（尚未建成）/);
  assert.match(html, /起终点强制设备/);
});

test('clicking a gap reports length, duration, tolerance and evidence state', () => {
  const model = withScreenCoordinates(navigationMonitoringLayerModel({
    result: simulationResult(), candidates: candidateSnapshot().candidates,
    routePath: ROUTE_PATH, routeLengthM: ROUTE_LENGTH_M, layerOn: true,
  }), screenPoint);
  const segment = model.unassociatedSegments[0];
  const midpoint = [
    (segment.screenFrom[0] + segment.screenTo[0]) / 2,
    (segment.screenFrom[1] + segment.screenTo[1]) / 2,
  ];

  // 设备命中**优先**于航段命中（同一点上两者都可能命中时，先给设备详情）。
  const deviceHit = model.devices.find(item =>
    Math.hypot(item.screen[0] - midpoint[0], item.screen[1] - midpoint[1]) <= 11);
  if (deviceHit) {
    assert.equal(hitNavigationMonitoringFeature(model, midpoint).kind, 'device',
      'a device must win over a segment at the same click point');
  }

  // 缺口命中与详情：用一份只含航段的模型（设备优先级已在上方单独锁定）。
  const gapOnly = {...model, devices: []};
  const hit = hitNavigationMonitoringFeature(gapOnly, midpoint);
  assert.ok(hit, 'clicking on a gap must hit it');
  assert.equal(hit.kind, 'gap');
  assert.equal(hit.segment.startM, 9000);
  assert.equal(hit.segment.endM, 9500);

  const simulation = navigationSimulationModel(simulationResult());
  const html = navigationMonitoringFeatureTooltip(hit, simulation);
  assert.match(html, /缺口长度/);
  assert.match(html, /经过时间/);
  assert.match(html, /容忍判定/);
  assert.match(html, /速度证据状态/);
  assert.match(html, /超出容忍时间/);
  assert.match(html, /500\.00 m/);
  assert.match(html, /33\.33 s/);
  assert.match(html, /不表示该航段不安全/);
});

test('point-to-segment distance clamps to endpoints and never guesses', () => {
  assert.equal(pointToSegmentDistance([0, 0], null, [1, 1]), null);
  assert.equal(pointToSegmentDistance([0, 0], [0, 0], [10, 0]), 0);
  assert.equal(pointToSegmentDistance([5, 3], [0, 0], [10, 0]), 3);
  assert.equal(pointToSegmentDistance([-4, 0], [0, 0], [10, 0]), 4);
  assert.equal(pointToSegmentDistance([14, 0], [0, 0], [10, 0]), 4);
  assert.equal(pointToSegmentDistance([3, 4], [0, 0], [0, 0]), 5);
});

// =============================================================================
// 10. 测试夹具不得冒充权威结果
// =============================================================================

test('a test fixture is labelled and never presented as an authoritative result', () => {
  assert.match(TEST_FIXTURE_LABEL, /非权威/);
  assert.match(TEST_FIXTURE_LABEL, /不得写回或冒充正式结果/);
  const html = renderNavigationMonitoringPanel({}, {
    result: simulationResult(), fixtureLabel: true,
  });
  assert.ok(html.includes(TEST_FIXTURE_LABEL));
});

test('simulation status text keeps blocked and undetermined distinct from passed', () => {
  assert.match(simulationStatusText('blocked'), /已阻断/);
  assert.match(simulationStatusText('undetermined'), /不可判定/);
  assert.match(simulationStatusText('passed'), /通过/);
  assert.equal(simulationStatusText('passed').includes('阻断'), false);
  assert.equal(simulationStatusText(undefined), '未运行');
});

// =============================================================================
// 11. 主操作按钮与候选统计刷新（本次实测到的点击失效缺陷回归）
// =============================================================================

/** 极简 document 桩：只支撑「按 id 取节点」，供局部刷新使用。 */
async function withDocStub(nodes, run) {
  const previous = globalThis.document;
  globalThis.document = {getElementById: id => nodes.get(id) || null};
  try { return await run(); }
  finally { globalThis.document = previous; }
}

function navigationControllerStub({button, container, calls, failure = null}) {
  const values = {
    navigationRouteSource: {value: 'operational_route'},
    navigationRouteId: {value: 'R0005'},
    navigation_monitor_association_radius_m: {value: '2500'},
    navigationCandidateSummary: container,
    runNavigationMonitoringSimulation: button,
  };
  return {
    $: id => values[id] ?? null,
    navigationMonitoring: {
      //: 候选已就绪：bind 不应再触发一次自动读取。
      state: () => ({candidates: {candidates: [], counts: {}, reason_counts: {}}}),
      loadCandidates: async () => { calls.candidates += 1; },
      simulate: async payload => {
        if (failure) throw new Error(failure);
        calls.simulate.push(payload);
      },
    },
    panelError: message => calls.errors.push(message),
  };
}

test('the run button is re-bound on every render and never left unhandled', async () => {
  resetNavigationMonitoringBinding();
  const calls = {candidates: 0, simulate: [], errors: []};
  const button = {disabled: false, dataset: {}, onclick: null};
  const container = {innerHTML: ''};
  const controller = navigationControllerStub({button, container, calls});

  await withDocStub(new Map([['navigationCandidateSummary', container]]), async () => {
    bindNavigationMonitoringPanel(controller);
    assert.equal(typeof button.onclick, 'function', 'bind must attach a handler');

    await button.onclick();
    assert.equal(calls.candidates, 1, 'the facade must be asked for the snapshot');
    assert.deepEqual(calls.simulate, [{
      routeSource: 'operational_route', routeId: 'R0005',
      draft: {monitor_association_radius_m: '2500'},
    }]);
    assert.equal(calls.errors.length, 0);
    assert.equal(button.disabled, false, 'the button must be re-enabled');
    assert.match(container.innerHTML, /候选总数/,
      'the candidate summary must be refreshed in place');
  });

  // 模拟一次 renderWorkflow()：面板整体重建，新节点必须拿到新的处理器。
  const second = {disabled: false, dataset: {}, onclick: null};
  const secondContainer = {innerHTML: ''};
  const secondController = navigationControllerStub(
    {button: second, container: secondContainer, calls});
  await withDocStub(new Map([['navigationCandidateSummary', secondContainer]]), async () => {
    bindNavigationMonitoringPanel(secondController);
    assert.equal(typeof second.onclick, 'function');
    await second.onclick();
    assert.equal(calls.simulate.length, 2,
      'the re-rendered button must still run the read-only simulation');
  });
  resetNavigationMonitoringBinding();
});

test('refreshing the candidate summary never rebuilds the panel (no click race)', async () => {
  resetNavigationMonitoringBinding();
  const calls = {candidates: 0, simulate: [], errors: []};
  const button = {disabled: false, dataset: {}, onclick: null};
  const container = {innerHTML: '旧内容'};
  const controller = navigationControllerStub({button, container, calls});

  const nodes = new Map([['navigationCandidateSummary', container]]);
  await withDocStub(nodes, async () => {
    bindNavigationMonitoringPanel(controller);
    const handler = button.onclick;
    // 局部刷新只改容器内容，**不**触碰按钮节点（否则点击会被替换打断）。
    assert.equal(refreshNavigationCandidateSummary(), true);
    assert.equal(button.onclick, handler, 'the button handler must stay identical');
    assert.match(container.innerHTML, /候选总数/);
    assert.notEqual(container.innerHTML, '旧内容');
  });

  // 容器不在页面上时局部刷新安全返回 false（绝不抛异常）。
  await withDocStub(new Map(), async () => {
    assert.equal(refreshNavigationCandidateSummary(), false);
  });
  resetNavigationMonitoringBinding();
});

test('a failing read-only call is surfaced and never silently swallowed', async () => {
  resetNavigationMonitoringBinding();
  const calls = {candidates: 0, simulate: [], errors: []};
  const button = {disabled: false, dataset: {}, onclick: null};
  const controller = navigationControllerStub({
    button, container: {innerHTML: ''}, calls, failure: '导航服务监测仿真请求失败',
  });
  await withDocStub(new Map(), async () => {
    bindNavigationMonitoringPanel(controller);
    await button.onclick();
    assert.deepEqual(calls.errors, ['导航服务监测仿真请求失败']);
    assert.equal(button.disabled, false, 'the button must be re-enabled after a failure');
  });
  resetNavigationMonitoringBinding();
});
