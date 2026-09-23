/**
 * Radar Surveillance Layout V1 前端契约测试（Step05 独立任务卡 + 地图 overlay）。
 *
 * 只验证**纯函数**与 HTML 字符串契约；不启动浏览器、不访问网络。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  COVERAGE_STATUS_LABELS, RADAR_LAYOUT_EVALUATE_ENDPOINT, RADAR_LAYOUT_ENDPOINT,
  RADAR_LAYOUT_MODEL_SCOPE, RADAR_LAYOUT_PROPOSAL_ONLY_NOTE, RADAR_LAYOUT_TITLE,
  RADAR_LAYOUT_V1_1_SEMANTICS, RADAR_LAYOUT_VERSION, RADAR_POLICY_ENDPOINT,
  RADAR_TYPE_LABELS, REQUIRED_SITE_COUNT_LABELS, SOLVER_STATUS_LABELS,
  SURFACE_CLASS_LABELS, isCompleteCoverage, radarLayoutModel,
  radarOverlayModel, routeCoverageColours,
} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';
import {renderRadarSurveillanceLayoutPanel} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';
import {render as renderStep5, radarLayoutModel as step5Model} from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  RADAR_COVERAGE_COLORS, RADAR_PANEL_COLORS, drawRadarLayoutOverlay,
  radarCoverageLegend,
} from '../cns_planner/web/js/map/radar_layout_overlay.js';

//: V1.1 一个已选单面阵的完整几何：塔顶 50 m ⇒ dz = +30 m，
//: 水平外半径 = sqrt(3000² − 30²)、内半径 = max(30, sqrt(120² − 30²))。
const PANEL_PLANE = {
  dz_m: 30, horizontal_inner_radius_m: Math.max(30, Math.sqrt(120 * 120 - 30 * 30)),
  horizontal_outer_radius_m: Math.sqrt(3000 * 3000 - 30 * 30),
  plane_intersection_status: 'intersects', plane_intersection_reason: null,
  slant_range_semantics: 'slant',
  horizontal_radius_semantics: 'slant_range_projected_onto_fixed_altitude_plane',
};

const SURVEY = {
  route_id: 'R1', status: 'proposal_ready', stage: 'radar_i_only', stage_label: 'Stage A',
  algorithm_version: '1.1',
  altitude_layer_id: 'ALT-080', altitude_m: 80, vertical_reference: 'egm2008_orthometric',
  selected_panel_count: 4, selected_tower_count: 4, radar_i_panel_count: 4,
  radar_ii_panel_count: 0, candidate_tower_count: 6, candidate_panel_count: 120,
  solver: {
    name: 'scipy.optimize.milp', library: 'HiGHS', stage: 'radar_i_only', status: 'optimal',
    optimality_proven: true, infeasibility_proven: false, mip_gap: 0,
    message: 'Optimization terminated successfully.',
  },
  selected_panels: [{
    panel_id: 'P-T1-radar_i-0.000000', tower_id: 'T1', tower_name: '塔1',
    longitude: 122.2, latitude: 30.0, radar_type: 'radar_i', radar_type_label: '中近程雷达Ⅰ型',
    azimuth_deg: 0, panel_half_width_deg: 45, radar_origin_egm2008_m: 50,
    altitude_plane_egm2008_m: 80, altitude_plane_geometry: PANEL_PLANE,
    horizontal_inner_radius_m: PANEL_PLANE.horizontal_inner_radius_m,
    horizontal_outer_radius_m: PANEL_PLANE.horizontal_outer_radius_m,
    plane_intersection_status: 'intersects',
    slant_range_semantics: 'slant',
    slant_range_preset_m: {min_slant_range_m: 120, max_slant_range_m: 3000},
    coverage: {sample_count: 25, satisfied_sample_count: 25,
      first_distance_along_route_m: 0, last_distance_along_route_m: 600},
  }],
  validation: {
    land: {sample_count: 60, length_m: 1500, satisfied_length_m: 1500, satisfied_fraction: 1,
      minimum_distinct_site_count: 2, required_distinct_site_count: 2,
      under_redundant_sample_count: 0, uncovered_sample_count: 0, unknown_sample_count: 0},
    sea: {sample_count: 60, length_m: 1505, satisfied_length_m: 1505, satisfied_fraction: 1,
      minimum_distinct_site_count: 1, required_distinct_site_count: 1,
      under_redundant_sample_count: 0, uncovered_sample_count: 0, unknown_sample_count: 0},
    coastal_uncertain: {sample_count: 12, length_m: 300, satisfied_length_m: 300,
      satisfied_fraction: 1, minimum_distinct_site_count: 2, required_distinct_site_count: 2,
      under_redundant_sample_count: 0, uncovered_sample_count: 0, unknown_sample_count: 0},
    unknown: {sample_count: 0, length_m: 0, satisfied_length_m: 0, satisfied_fraction: null,
      minimum_distinct_site_count: null, required_distinct_site_count: null,
      under_redundant_sample_count: 0, uncovered_sample_count: 0, unknown_sample_count: 0},
    uncovered_segments: [],
    under_redundant_segments: [],
    unknown_segments: [],
    surface_class_semantics: 'coastal_uncertain_is_treated_as_land_with_required_distinct_site_count_2',
    coverage_profile: {
      entries: [
        {status: 'satisfied', from_m: 0, to_m: 900, sample_count: 37,
          start_coordinate: [122.2, 30.0], end_coordinate: [122.21, 30.0]},
        {status: 'uncovered', from_m: 900, to_m: 1000, sample_count: 5,
          start_coordinate: [122.21, 30.0], end_coordinate: [122.211, 30.0]},
      ],
      count: 2, truncated: false,
    },
  },
  refinement_rounds: [{round_index: 0, violation_count: 0, panel_count: 4, spacing_m: 5}],
  infeasibility_reasons: [], unknown_evidence: [],
  // V1.1：雷达原点 = tower_top_orthometric_m；legacy 挂高只被兼容读取。
  radar_origin: {
    radar_origin_basis: 'tower_top_orthometric_m',
    resolve_status: 'passed', resolved_count: 6, unresolved_count: 0,
    installation_assumption: 'radar_phase_center_at_tower_top',
    engineering_confirmed: false,
    legacy_mount_height_used: false,
    legacy_mount_assumption_status: 'legacy_not_used_by_v1_1',
    backend_hardcoded_mount_height: false,
  },
  sources: {
    terrain_dtm: {available: true, used_for_route_sample_height: false},
    land_mask: {
      ok: true, configured_path: 'D:/x/zhejiang_boundary.gpkg',
      layer_name: 'zhejiang_boundary', source_crs: 'EPSG:4326',
      classification_basis: 'explicit_polygon', crs_status: 'passed',
      coastal_uncertainty_buffer: {
        coastal_uncertainty_buffer_m: 30, parameter_origin: 'engineering_assumption',
        confirmed: false, semantics: 'engineering_conservative_buffer_not_data_accuracy',
        metric_crs: 'EPSG:32651',
      },
    },
    radar_origin: {radar_origin_basis: 'tower_top_orthometric_m', radar_mount_height_required: false},
    radar_mount_height: {
      status: 'not_configured', radar_mount_height_m: null,
      legacy_not_used_by_v1_1: true, required_for_v1_1: false,
    },
  },
  land_mask_source_provenance: {
    source_type: 'real', source_role: 'land_mask', source_crs: 'EPSG:4326',
    layer_name: 'zhejiang_boundary', classification_basis: 'explicit_polygon',
    dem_nodata_used_to_infer_sea: false,
  },
  route_sampling: {
    optimization_sample_spacing_m: 25, validation_sample_spacing_m: 5,
    sample_egm2008_m: 80, sample_egm2008_semantics: 'fixed_alt_080_egm2008_constant_for_every_sample',
    terrain_elevation_used_as_route_height: false,
    nearest_sample_classification_inheritance: false,
  },
  surface_classification: {
    status: 'passed', classification_basis: 'explicit_polygon',
    classification_independence: 'optimization_and_validation_samples_classified_independently',
  },
  semantics_fingerprint: {
    route_altitude_semantics: 'fixed_alt_080_egm2008',
    vertical_delta_semantics: 'target_minus_radar_origin',
    radar_origin_semantics: 'tower_top_orthometric',
    land_mask_semantics: 'explicit_land_polygon_containment_plus_coastal_uncertainty_buffer',
    geometry_version: 'radar_layout_geometry_v1_1',
  },
  parameters: {
    optimization_sample_spacing_m: 25, validation_sample_spacing_m: 5,
    max_refinement_rounds: 3, fixed_altitude_layer_id: 'ALT-080', fixed_altitude_m: 80,
    vertical_reference: 'egm2008_orthometric', metric_crs: 'EPSG:32651',
    route_altitude_semantics: 'fixed_alt_080_egm2008',
    vertical_delta_semantics: 'target_minus_radar_origin',
  },
};

const READINESS = {
  status: 'passed', algorithm_id: 'radar_surveillance_layout', algorithm_version: '1.1',
  model_scope: RADAR_LAYOUT_MODEL_SCOPE,
  proposal_only: true, passed_operational_route_count: 1, operational_route_count: 1,
  tower_count: 6, tower_with_resolved_radar_base_count: 6, tower_obstacle_profile_count: 6,
  blockers: [],
  radar_origin: {
    radar_origin_basis: 'tower_top_orthometric_m',
    installation_assumption: 'radar_phase_center_at_tower_top',
    engineering_confirmed: false, radar_mount_height_required: false,
  },
  radar_mount_height: {
    status: 'not_configured', radar_mount_height_m: null,
    legacy_not_used_by_v1_1: true, required_for_v1_1: false,
  },
  fixed_altitude: {altitude_layer_id: 'ALT-080', altitude_m: 80,
    vertical_reference: 'egm2008_orthometric', present: true, confirmed: true,
    confirmed_and_current: true},
  metric_transform: {status: 'passed', metric_crs: 'EPSG:32651', reason: null},
  solver: {available: true, name: 'scipy.optimize.milp', library: 'HiGHS',
    scipy_version: '1.16.0', reason: null, greedy_fallback_used: false},
  land_mask: {
    status: 'passed', layer_name: 'zhejiang_boundary', source_crs: 'EPSG:4326',
    classification_basis: 'explicit_polygon',
    coastal_uncertainty_buffer: {
      coastal_uncertainty_buffer_m: 30, parameter_origin: 'engineering_assumption',
      confirmed: false, semantics: 'engineering_conservative_buffer_not_data_accuracy',
    },
  },
  semantics_fingerprint: SURVEY.semantics_fingerprint,
  parameters: SURVEY.parameters,
  device_summary: {
    source: {title: '低空智能网联系统相关设备信息-四创(2).docx', sha256: 'e0d9cc20', read_only: true,
      source_modified: false},
    types: [
      {radar_type: 'radar_i', label: '中近程雷达Ⅰ型', min_slant_range_m: 120, max_slant_range_m: 3000,
        source_min_detection_distance: '①最小探测距离：≤120米；',
        source_max_detection_distance: '②最大探测距离：≥3km；',
        azimuth_coverage_raw: '①方位覆盖范围：360°（四面阵）、±45°（单面阵）；',
        elevation_coverage_raw: '②俯仰最大覆盖范围：≥45°（仰角可调）；',
        azimuth_measurement_accuracy_raw: '①方位≤0.5°；', range_measurement_accuracy_raw: '③距离≤10m；',
        search_update_interval_raw: '①搜索≤2s；', track_update_interval_raw: '②跟踪≤1s。',
        rcs_reference_m2: 0.01, pd_reference: 0.8, pfa_reference: 1e-6, used_in_geometry: false},
    ],
  },
  sources: {
    terrain: {available: true, reason: null, used_for_route_sample_height: false},
    land_mask: {ok: true, reason: null, configured_path: 'D:/x/zhejiang_boundary.gpkg'},
    radar_origin: {radar_origin_basis: 'tower_top_orthometric_m'},
    radar_mount_height: {status: 'not_configured', legacy_not_used_by_v1_1: true},
  },
};

function flow() {
  return {
    radar_surveillance_layout: {
      status: 'passed', count: 1,
      items: [{...SURVEY, selected_panels: SURVEY.selected_panels,
        coverage_profile: SURVEY.validation.coverage_profile}],
      by_route: {},
      semantics: {bounded_summary_projection: true, per_sample_detail_omitted: true},
      proposal_only: true,
    },
    radar_surveillance_layout_readiness: READINESS,
    towers: {items: [{tower_id: 'T2', coordinate: [122.22, 30.0]}]},
  };
}

test('radar layout endpoints and V1.1 vocabulary are stable', () => {
  assert.equal(RADAR_LAYOUT_ENDPOINT, '/api/radar-surveillance-layout');
  assert.equal(RADAR_LAYOUT_EVALUATE_ENDPOINT, '/api/radar-surveillance-layout/evaluate');
  assert.equal(RADAR_POLICY_ENDPOINT, '/api/radar-surveillance-policy');
  assert.equal(RADAR_LAYOUT_TITLE, '监视雷达初步划设');
  assert.equal(RADAR_LAYOUT_MODEL_SCOPE, 'geometric_initial_radar_layout');
  assert.match(RADAR_LAYOUT_PROPOSAL_ONLY_NOTE, /proposal_only/);
  assert.equal(RADAR_TYPE_LABELS.radar_i, '中近程雷达Ⅰ型');
  assert.equal(RADAR_TYPE_LABELS.radar_ii, '中近程雷达Ⅱ型');
  assert.equal(SOLVER_STATUS_LABELS.optimal, '已证明最优');
  assert.equal(SOLVER_STATUS_LABELS.infeasible, '已证明不可行');
  assert.match(SOLVER_STATUS_LABELS.time_limit, /未证明/);
  assert.equal(COVERAGE_STATUS_LABELS.satisfied, '满足');
  // V1.1 新增语义词表。
  assert.equal(RADAR_LAYOUT_VERSION, '1.1');
  assert.match(SURFACE_CLASS_LABELS.coastal_uncertain, /海岸不确定带/);
  assert.match(REQUIRED_SITE_COUNT_LABELS.coastal_uncertain, /2 个不同铁塔站址/);
  assert.match(RADAR_LAYOUT_V1_1_SEMANTICS.route_altitude, /80 m EGM2008/);
  assert.match(RADAR_LAYOUT_V1_1_SEMANTICS.vertical_delta, /目标 − 雷达原点/);
  assert.match(RADAR_LAYOUT_V1_1_SEMANTICS.radar_origin, /tower_top_orthometric_m/);
});

test('radar layout model reads counts, solver proof flags and coverage summaries', () => {
  const model = radarLayoutModel(flow());
  assert.equal(model.status, 'passed');
  assert.equal(model.solverStatus, 'optimal');
  assert.equal(model.solverLabel, '已证明最优');
  assert.equal(model.optimalityProven, true);
  assert.equal(model.infeasibilityProven, false);
  assert.equal(model.radarICount, 4);
  assert.equal(model.radarIICount, 0);
  assert.equal(model.panelCount, 4);
  assert.equal(model.land.satisfied_fraction, 1);
  assert.equal(model.sea.required_distinct_site_count, 1);
  assert.equal(model.coastalUncertain.required_distinct_site_count, 2);
  assert.equal(model.landMaskReady, true);
  assert.equal(model.landMask.layer_name, 'zhejiang_boundary');
  assert.equal(model.landMask.source_crs, 'EPSG:4326');
  // V1.1：雷达原点 = 塔顶；legacy 挂高不再是 readiness 门控。
  assert.equal(model.radarOrigin.radar_origin_basis, 'tower_top_orthometric_m');
  assert.equal(model.radarOrigin.legacy_mount_height_used, false);
  assert.equal(model.legacyMountHeight.legacy_not_used_by_v1_1, true);
  assert.equal(model.legacyMountHeight.required_for_v1_1, false);
  assert.equal(model.metricTransform.status, 'passed');
  assert.equal(model.solverReadiness.available, true);
  assert.deepEqual(model.readinessBlockers, []);
  assert.equal(model.routeSampling.sample_egm2008_m, 80);
  assert.equal(model.routeSampling.terrain_elevation_used_as_route_height, false);
  assert.equal(model.routeSampling.nearest_sample_classification_inheritance, false);
  assert.equal(model.deviceTypes.length, 1);
  assert.equal(model.deviceTypes[0].used_in_geometry, false);
  assert.equal(isCompleteCoverage(model.item.status), true);
  assert.equal(isCompleteCoverage('refinement_incomplete'), false);
  assert.equal(isCompleteCoverage('infeasible'), false);
});

test('route coverage colours only emit contiguous segments with a conservative bridge', () => {
  const segments = routeCoverageColours(SURVEY);
  assert.equal(segments.length, 2);
  assert.equal(segments[0].status, 'satisfied');
  assert.equal(segments[1].status, 'uncovered');
  assert.deepEqual(segments[0].from, [122.2, 30.0]);
  assert.deepEqual(segments[1].to, [122.211, 30.0]);
});

test('radar overlay model uses backend 80 m plane radii and never slant range', () => {
  const model = radarOverlayModel(flow(), [{tower_id: 'T2', coordinate: [122.22, 30.0]}]);
  assert.equal(model.proposalOnly, true);
  assert.equal(model.selectedTowers.length, 1);
  assert.equal(model.panels.length, 1);
  const panel = model.panels[0];
  // V1.1：水平半径来自后端交截，绝不是 120 / 3000 斜距本身。
  assert.equal(panel.radius_outer_m, PANEL_PLANE.horizontal_outer_radius_m);
  assert.equal(panel.radius_inner_m, PANEL_PLANE.horizontal_inner_radius_m);
  assert.ok(panel.radius_outer_m < 3000, '外半径必须小于最大斜距');
  assert.notEqual(panel.radius_inner_m, 120);
  assert.equal(panel.dz_m, 30);
  assert.equal(panel.horizontal_radius_semantics, 'backend_plane_intersection_not_slant_range');
  assert.equal(panel.altitude_m, 80);
  assert.deepEqual(model.selectedTowerIds, ['T1']);
  assert.equal(model.panelsWithoutPlaneIntersection, 0);
  // 候选铁塔：未选中的才弱化绘制。
  assert.deepEqual(model.candidateTowers.map((item) => item.tower_id), ['T2']);
  assert.ok(model.routeCoverageColours.length >= 1);
});

test('radar overlay model skips panels whose 80 m plane has no valid intersection', () => {
  const snapshot = flow();
  const item = snapshot.radar_surveillance_layout.items[0];
  item.selected_panels = [
    item.selected_panels[0],
    {
      ...item.selected_panels[0], panel_id: 'P-T1-radar_i-90.000000', azimuth_deg: 90,
      horizontal_inner_radius_m: null, horizontal_outer_radius_m: null,
      plane_intersection_status: 'no_intersection',
      altitude_plane_geometry: {
        dz_m: -30, horizontal_inner_radius_m: null, horizontal_outer_radius_m: null,
        plane_intersection_status: 'no_intersection',
        plane_intersection_reason: 'site_plane_below_radar_origin_no_down_tilt_in_this_model',
      },
    },
  ];
  const model = radarOverlayModel(snapshot, []);
  assert.equal(model.selectedPanels === undefined, true);
  assert.equal(model.panels.length, 1, '无有效交截的 panel 不进入绘制几何');
  assert.equal(model.panelsWithoutPlaneIntersection, 1);
  // 面板仍必须把这个事实说出来（不静默少画）。
  const html = renderRadarSurveillanceLayoutPanel(snapshot);
  assert.match(html, /80 m 平面无有效交截/);
  assert.match(html, /site_plane_below_radar_origin/);
});

test('radar overlay model is empty before an explicit evaluation', () => {
  const model = radarOverlayModel({}, []);
  assert.equal(model.status, 'not_calculated');
  assert.deepEqual(model.panels, []);
  assert.deepEqual(model.selectedTowers, []);
  assert.equal(model.panelsWithoutPlaneIntersection, 0);
});

test('radar layout panel renders the required V1.1 summary fields', () => {
  const html = renderRadarSurveillanceLayoutPanel(flow());
  assert.match(html, /监视雷达初步划设/);
  assert.match(html, /80m固定高度航路方向性雷达几何初步划设方案/);
  assert.match(html, /geometric_initial_radar_layout/);
  assert.match(html, /algorithm_version 1\.1/);
  assert.match(html, /ALT-080/);
  assert.match(html, /25(\.0)? m/);
  assert.match(html, /5(\.0)? m/);
  assert.match(html, /scipy\.optimize\.milp/);
  assert.match(html, /optimality_proven=true/);
  assert.match(html, /中近程雷达Ⅰ型/);
  assert.match(html, /最小斜距 120/);
  // V1.1 关键事实必须出现在面板上。
  assert.match(html, /tower_top_orthometric_m/);
  assert.match(html, /radar_phase_center_at_tower_top/);
  assert.match(html, /legacy_not_used_by_v1_1/);
  assert.match(html, /zhejiang_boundary/);
  assert.match(html, /EPSG:4326/);
  assert.match(html, /海岸不确定带/);
  assert.match(html, /engineering_assumption/);
  assert.match(html, /radarCoastalBuffer/);
  assert.match(html, /radarLandMaskLayer/);
  assert.match(html, /海岸不确定带（按陆地处理）/);
  assert.match(html, /radarSurveillancePolicy|saveRadarSurveillancePolicy/);
  assert.match(html, /evaluateRadarSurveillanceLayout/);
  // legacy 挂高输入框必须存在但被禁用（只读回显），不能作为必填项。
  assert.match(html, /id="radarMountHeight"[^>]*disabled/);
  assert.doesNotMatch(html, /<script/);
});

test('radar layout panel escapes user provided text', () => {
  const evil = flow();
  evil.radar_surveillance_layout.items[0].infeasibility_reasons = [
    '<img src=x onerror=alert(1)>',
  ];
  const html = renderRadarSurveillanceLayoutPanel(evil);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x/);
});

test('step05 exposes an independent radar layout task card without rebuilding the page', () => {
  const html = renderStep5({flow: {
    devices: [], defaults: {engineering_parameters: {
      primary_spacing_factor: {value: 0.9, source: 'x'},
      co_location_search_radius_m: {value: 2000, source: 'x'},
    }},
    steps: {}, risks: {life: {status: 'not_calculated'}, property: {status: 'not_calculated'}},
    ...flow(),
  }});
  assert.match(html, /监视雷达初步划设/);
  assert.equal(typeof step5Model, 'function');
});

test('radar coverage legend and overlay drawing stay bounded', () => {
  assert.deepEqual(radarCoverageLegend().map((item) => item.status),
    ['satisfied', 'under_redundant', 'uncovered', 'unknown']);
  assert.ok(RADAR_COVERAGE_COLORS.uncovered);
  assert.ok(RADAR_PANEL_COLORS.radar_ii);

  const drawn = [];
  const ctx = {
    save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {},
    fill() { drawn.push('fill'); }, stroke() { drawn.push('stroke'); }, arc() { drawn.push('arc'); },
    set fillStyle(value) { drawn.push('fillStyle'); },
    set strokeStyle(value) { drawn.push('strokeStyle'); },
    set globalAlpha(value) {}, set lineWidth(value) {}, set lineCap(value) {},
  };
  const stats = drawRadarLayoutOverlay({
    ctx, view: {res: 100}, screenPoint: (coordinate) => [coordinate[0] * 1000, coordinate[1] * 1000],
    model: radarOverlayModel(flow(), [{tower_id: 'T2', coordinate: [122.22, 30.0]}]),
  });
  assert.equal(stats.selectedTowers, 1);
  assert.equal(stats.panels, 1);
  assert.ok(stats.routeSegments >= 1);
  assert.ok(drawn.includes('arc'));
});

test('radar overlay never draws unselected panel coverage polygons', () => {
  const model = radarOverlayModel(flow(), [{tower_id: 'T2', coordinate: [122.22, 30.0]}]);
  // 只下发已选方案：panel 数等于 summary 里的已选面阵数，且不含任何候选 panel 几何。
  const summaryPanels = flow().radar_surveillance_layout.items[0].selected_panels.length;
  assert.equal(model.panels.length, summaryPanels);
  assert.equal(JSON.stringify(model).includes('covered_sample_indices'), false);
  assert.equal(JSON.stringify(model).includes('candidate_panel_id'), false);
});
