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
  RADAR_POLICY_ENDPOINT, RADAR_TYPE_LABELS, SOLVER_STATUS_LABELS,
  isCompleteCoverage, radarLayoutModel,
  radarOverlayModel, routeCoverageColours,
} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';
import {renderRadarSurveillanceLayoutPanel} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';
import {render as renderStep5, radarLayoutModel as step5Model} from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  RADAR_COVERAGE_COLORS, RADAR_PANEL_COLORS, drawRadarLayoutOverlay,
  radarCoverageLegend,
} from '../cns_planner/web/js/map/radar_layout_overlay.js';

const SURVEY = {
  route_id: 'R1', status: 'proposal_ready', stage: 'radar_i_only', stage_label: 'Stage A',
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
    azimuth_deg: 0, panel_half_width_deg: 45, radar_origin_egm2008_m: 90,
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
    unknown: {sample_count: 0, length_m: 0, satisfied_length_m: 0, satisfied_fraction: null,
      minimum_distinct_site_count: null, required_distinct_site_count: null,
      under_redundant_sample_count: 0, uncovered_sample_count: 0, unknown_sample_count: 0},
    uncovered_segments: [],
    under_redundant_segments: [],
    unknown_segments: [],
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
  radar_origin: {
    mount_assumption: {radar_mount_height_m: 30, confirmed: false,
      parameter_origin: 'engineering_assumption', source: 'engineering_example',
      status: 'pending_confirmation'},
  },
  parameters: {
    optimization_sample_spacing_m: 25, validation_sample_spacing_m: 5,
    max_refinement_rounds: 3, fixed_altitude_layer_id: 'ALT-080', fixed_altitude_m: 80,
    vertical_reference: 'egm2008_orthometric', metric_crs: 'EPSG:32651',
  },
};

const READINESS = {
  status: 'passed', algorithm_id: 'radar_surveillance_layout', model_scope: RADAR_LAYOUT_MODEL_SCOPE,
  proposal_only: true, passed_operational_route_count: 1, operational_route_count: 1,
  tower_count: 6, tower_with_resolved_radar_base_count: 6,
  radar_mount_height: {status: 'pending_confirmation', radar_mount_height_m: 30,
    confirmed: false, parameter_origin: 'engineering_assumption', source: 'engineering_example'},
  fixed_altitude: {altitude_layer_id: 'ALT-080', altitude_m: 80,
    vertical_reference: 'egm2008_orthometric'},
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
    terrain: {available: true, reason: null},
    land_mask: {ok: false, reason: 'land_mask_not_configured'},
    radar_mount_height: {status: 'pending_confirmation'},
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

test('radar layout endpoints and vocabulary are stable', () => {
  assert.equal(RADAR_LAYOUT_ENDPOINT, '/api/radar-surveillance-layout');
  assert.equal(RADAR_LAYOUT_EVALUATE_ENDPOINT, '/api/radar-surveillance-layout/evaluate');
  assert.equal(RADAR_POLICY_ENDPOINT, '/api/radar-surveillance-policy');
  assert.equal(RADAR_LAYOUT_TITLE, '监视雷达初步划设');  assert.equal(RADAR_LAYOUT_MODEL_SCOPE, 'geometric_initial_radar_layout');
  assert.match(RADAR_LAYOUT_PROPOSAL_ONLY_NOTE, /proposal_only/);
  assert.equal(RADAR_TYPE_LABELS.radar_i, '中近程雷达Ⅰ型');
  assert.equal(RADAR_TYPE_LABELS.radar_ii, '中近程雷达Ⅱ型');
  assert.equal(SOLVER_STATUS_LABELS.optimal, '已证明最优');
  assert.equal(SOLVER_STATUS_LABELS.infeasible, '已证明不可行');
  assert.match(SOLVER_STATUS_LABELS.time_limit, /未证明/);
  assert.equal(COVERAGE_STATUS_LABELS.satisfied, '满足');
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
  assert.equal(model.landMaskReady, false);
  assert.equal(model.landMaskReason, 'land_mask_not_configured');
  assert.equal(model.mountHeight.confirmed, false);
  assert.equal(model.mountHeight.parameter_origin, 'engineering_assumption');
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

test('radar overlay model emits selected towers, panels and route colouring', () => {
  const model = radarOverlayModel(flow(), [{tower_id: 'T2', coordinate: [122.22, 30.0]}]);
  assert.equal(model.proposalOnly, true);
  assert.equal(model.selectedTowers.length, 1);
  assert.equal(model.panels.length, 1);
  assert.equal(model.panels[0].radius_inner_m, 120);
  assert.equal(model.panels[0].radius_outer_m, 3000);
  assert.equal(model.panels[0].altitude_m, 80);
  assert.deepEqual(model.selectedTowerIds, ['T1']);
  // 候选铁塔：未选中的才弱化绘制。
  assert.deepEqual(model.candidateTowers.map((item) => item.tower_id), ['T2']);
  assert.ok(model.routeCoverageColours.length >= 1);
});

test('radar overlay model is empty before an explicit evaluation', () => {
  const model = radarOverlayModel({}, []);
  assert.equal(model.status, 'not_calculated');
  assert.deepEqual(model.panels, []);
  assert.deepEqual(model.selectedTowers, []);
});

test('radar layout panel renders the required summary fields', () => {
  const html = renderRadarSurveillanceLayoutPanel(flow());
  assert.match(html, /监视雷达初步划设/);
  assert.match(html, /80m固定高度航路方向性雷达几何初步划设方案/);
  assert.match(html, /geometric_initial_radar_layout/);
  assert.match(html, /ALT-080/);
  assert.match(html, /25(\.0)? m/);
  assert.match(html, /5(\.0)? m/);
  assert.match(html, /scipy\.optimize\.milp/);
  assert.match(html, /optimality_proven=true/);
  assert.match(html, /中近程雷达Ⅰ型/);
  assert.match(html, /最小斜距 120/);
  assert.match(html, /land_mask_not_configured/);
  assert.match(html, /radarMountHeight/);
  assert.match(html, /radarSurveillancePolicy|saveRadarSurveillancePolicy/);
  assert.match(html, /evaluateRadarSurveillanceLayout/);
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
