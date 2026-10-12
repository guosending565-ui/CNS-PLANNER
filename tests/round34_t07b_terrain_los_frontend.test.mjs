/**
 * Round34-T07-B：Radar 地形 LOS 只读证据的前端契约测试（Step05 任务卡）。
 *
 * 只验证**纯函数**与 HTML 字符串契约：不启动浏览器、不访问网络、不读 DEM。
 *
 * 覆盖的关键约定：
 *  1. 默认只消费轻量状态，**不**触发任何计算入口；
 *  2. 固定中文声明必须出现；
 *  3. 三态计数、原点可用性计数、指纹与时效一律**原样转印**，前端不重算；
 *  4. 「规划候选资格」与「物理 LOS 可评价性」被显式区分；
 *  5. 明细/摘要边界与 stale 语义在 UI 上可见。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  RADAR_TERRAIN_LOS_DETAILS_ENDPOINT, RADAR_TERRAIN_LOS_DIAGNOSE_ENDPOINT,
  RADAR_TERRAIN_LOS_DISCLAIMER, RADAR_TERRAIN_LOS_STATE_TEXT,
  RADAR_TERRAIN_LOS_STATUS_ENDPOINT, RADAR_TERRAIN_LOS_TITLE,
  loadRadarTerrainLosDetails, loadRadarTerrainLosDiagnosis,
  radarTerrainLosModel, renderRadarSurveillanceLayoutPanel,
  renderRadarTerrainLosBlock,
} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';

const STATUS = {
  adapter_id: 'radar_terrain_los_read_only_evidence_v1',
  evidence_status: 'not_requested',
  algorithm_version: 'radar_terrain_los_v1_straight_ray_sampled_profile',
  read_mode: 'single_read_only_native_window_no_resample_per_request',
  available_to_compute: true,
  dem_opened: false,
  los_computation_performed: false,
  los_status_counts: {clear: 0, blocked: 0, unknown: 0},
  origin_availability_counts: {usable: 0, unknown: 0, not_evaluable: 0},
  adapter_availability: {
    terrain_dtm: {configured: true, available: true},
    metric_transform: {available: true},
    altitude_layer: {
      altitude_layer_id: 'ALT-100', nominal_altitude_m: 100,
      vertical_reference: 'egm2008_orthometric', resolution_source: 'operational_route',
    },
  },
};

const EVIDENCE = {
  adapter_id: 'radar_terrain_los_read_only_evidence_v1',
  evidence_status: 'computed',
  evidence_status_label: '已计算',
  algorithm_version: 'radar_terrain_los_v1_straight_ray_sampled_profile',
  read_mode: 'single_read_only_native_window_no_resample_per_request',
  evaluation_scope: 'bounded_tower_probe_sample',
  evaluated_tower_count: 2, evaluated_probe_count: 4,
  candidate_tower_count: 373, candidate_probe_count: 4016,
  los_status_counts: {clear: 2, blocked: 1, unknown: 1},
  origin_availability_counts: {usable: 285, unknown: 0, not_evaluable: 88},
  dem: {
    source_identity: {
      file_name: 'Zhoushan_FABDEM_DTM_30m.tif', absolute_path: 'D:/dem.tif',
      mtime_ns: 1, resampled: false,
    },
    vertical_reference: 'egm2008_orthometric',
    read_receipt: {open_count: 2, one_open_per_path_and_variant: true, reused_count: 0},
  },
  altitude_layer: {
    altitude_layer_id: 'ALT-100', nominal_altitude_m: 100,
    vertical_reference: 'egm2008_orthometric',
    resolution_source: 'operational_route',
    probe_height_source: 'authoritative_altitude_layer_nominal_altitude_m',
    representative_height_used: false,
  },
  sampling: {
    sampling_interval_m: 5, envelope_radius_m: 20,
    curvature_correction_enabled: false, refraction_enabled: false,
  },
  bounds: {
    max_tower_count: 2, probe_budget: 4, record_sample_limit: 24,
    full_terrain_profile_included: false, detail_requires_explicit_request: true,
  },
  input_fingerprint: 'f'.repeat(64),
  currentness: {
    state: 'no_prior_evidence', stale: false, prior_evidence_present: false,
    prior_input_fingerprint: null, current_input_fingerprint: 'f'.repeat(64),
    reasons: [], reasons_zh: ['未提供可比较的旧 LOS 证据，因此不宣称任何旧结果 stale。'],
    upstream_planning_state_untouched: true,
  },
  records: [{
    tower_id: '33090301000160', probe_id: 'R-1:O000000', route_id: 'R-1',
    los_status: 'clear', reason: null, minimum_clearance_m: 3.2,
    horizontal_distance_m: 250, probe_altitude_egm2008_m: 100,
    radar_origin_egm2008_m: 121.4, radar_origin_availability: 'usable',
  }, {
    tower_id: 'T-002', probe_id: 'R-1:O000004', route_id: 'R-1',
    los_status: 'blocked', reason: 'terrain_blocks_ray', minimum_clearance_m: -12.5,
    horizontal_distance_m: 800, probe_altitude_egm2008_m: 100,
    radar_origin_egm2008_m: 90, radar_origin_availability: 'usable',
  }],
  record_sample_count: 2, record_sample_truncated: false,
  low_clearance_threshold_m: 5, low_clearance_samples: [{
    tower_id: '33090301000160', probe_id: 'R-1:O000000', minimum_clearance_m: 3.2,
    los_status: 'clear', low_clearance: true,
  }],
  not_evaluated: ['building_occlusion', 'clutter', 'radar_equation', 'project_state_persistence'],
  dimension_separation_zh: '「规划候选资格」与「物理 LOS 可评价性」是两个不同维度。',
  los_three_state_semantics_zh: {
    clear: '沿射线在本次采样下未见地形遮挡。',
    blocked: '当前地形射线被遮挡；不自动认定雷达绝对无法探测。',
    unknown: '证据不足；不视为 clear。',
  },
  user_disclaimer_zh: RADAR_TERRAIN_LOS_DISCLAIMER,
};

test('terrain LOS endpoints are pinned to the read-only diagnostics routes', () => {
  assert.equal(RADAR_TERRAIN_LOS_STATUS_ENDPOINT, '/api/radar-terrain-los/status');
  assert.equal(RADAR_TERRAIN_LOS_DIAGNOSE_ENDPOINT, '/api/radar-terrain-los/diagnose');
  assert.equal(RADAR_TERRAIN_LOS_DETAILS_ENDPOINT, '/api/radar-terrain-los/details');
});

test('terrain LOS model never computes anything and transcribes backend fields verbatim', () => {
  const model = radarTerrainLosModel({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: EVIDENCE,
  });
  assert.equal(model.hasEvidence, true);
  assert.equal(model.evidenceStatus, 'computed');
  assert.equal(model.clearCount, 2);
  assert.equal(model.blockedCount, 1);
  assert.equal(model.unknownCount, 1);
  assert.equal(model.usableOriginCount, 285);
  assert.equal(model.notEvaluableOriginCount, 88);
  assert.equal(model.evaluatedProbeCount, 4);
  assert.equal(model.candidateProbeCount, 4016);
  assert.equal(model.inputFingerprint, 'f'.repeat(64));
  assert.equal(model.stale, false);
  assert.equal(model.evidenceStatusLabel, RADAR_TERRAIN_LOS_STATE_TEXT.computed);
});

test('model without explicit diagnosis only exposes the lightweight status', () => {
  const model = radarTerrainLosModel({radar_terrain_los: STATUS});
  assert.equal(model.hasEvidence, false);
  assert.equal(model.evidenceStatus, 'not_requested');
  assert.equal(model.evaluatedProbeCount, 0);
  assert.equal(model.records.length, 0);
});

test('no-evidence block never claims any result and offers the explicit entry', () => {
  const html = renderRadarTerrainLosBlock({radar_terrain_los: STATUS});
  assert.match(html, new RegExp(RADAR_TERRAIN_LOS_TITLE));
  assert.match(html, /尚未请求地形 LOS 计算/);
  assert.match(html, /id="runRadarTerrainLos"/);
  assert.match(html, /不会<\/b>自动读取 DEM|不会\*\*自动读取 DEM|不会自动读取 DEM/);
  assert.match(html, new RegExp(RADAR_TERRAIN_LOS_DISCLAIMER));
  assert.match(html, /ALT-100/);
});

test('evidence block shows counts sources fingerprints and the fixed disclaimer', () => {
  const html = renderRadarTerrainLosBlock({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: EVIDENCE,
  });
  assert.match(html, /clear <b>2<\/b>/);
  assert.match(html, /blocked <b>1<\/b>/);
  assert.match(html, /unknown <b>1<\/b>/);
  assert.match(html, /不可评价（not_evaluable）<b>88<\/b>/);
  assert.match(html, /Zhoushan_FABDEM_DTM_30m\.tif/);
  assert.match(html, new RegExp('radar_terrain_los_v1_straight_ray_sampled_profile'));
  assert.match(html, new RegExp('f'.repeat(64)));
  assert.match(html, new RegExp(RADAR_TERRAIN_LOS_DISCLAIMER));
  //: 两个维度必须显式区分。
  assert.match(html, /规划候选资格/);
  assert.match(html, /物理 LOS 可评价性/);
});

test('low clearance hint is shown without changing thresholds', () => {
  const html = renderRadarTerrainLosBlock({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: EVIDENCE,
  });
  assert.match(html, /低净空提示/);
  assert.match(html, /不改变<\/b> LOS 三态阈值|不改变.*LOS 三态阈值/);
});

test('stale evidence is surfaced and never propagates upstream', () => {
  const stale = {
    ...EVIDENCE,
    evidence_status: 'stale',
    currentness: {
      ...EVIDENCE.currentness, state: 'stale', stale: true,
      prior_input_fingerprint: '0'.repeat(64),
      reasons: ['los_inputs_changed'],
      reasons_zh: ['LOS 输入（原点 / 高度层 / DEM / 采样或射线模型）已变化。'],
    },
  };
  const html = renderRadarTerrainLosBlock({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: stale,
  });
  const model = radarTerrainLosModel({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: stale,
  });
  assert.equal(model.stale, true);
  assert.equal(model.evidenceStatus, 'stale');
  assert.match(html, /旧证据已 stale/);
  assert.match(html, /LOS 输入（原点 \/ 高度层 \/ DEM \/ 采样或射线模型）已变化。/);
  assert.match(html, /不<\/b>传播到上游|不<\/b>\*\*传播|不\*\*传播到上游|\*\*不\*\*传播/);
});

test('failed evidence never renders as clear', () => {
  const failed = {
    ...EVIDENCE, evidence_status: 'failed',
    failure_reason: 'terrain_dtm_not_configured',
    failure_detail: '项目未配置 terrain_dtm 数据源路径；不猜默认数据源。',
    los_status_counts: {clear: 0, blocked: 0, unknown: 0},
  };
  const html = renderRadarTerrainLosBlock({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: failed,
  });
  assert.match(html, /计算失败/);
  assert.match(html, /terrain_dtm_not_configured/);
  assert.doesNotMatch(html, /clear <b>[1-9]/);
});

test('full terrain profile is never advertised as included', () => {
  const html = renderRadarTerrainLosBlock({
    radar_terrain_los: STATUS, radar_terrain_los_diagnosis: EVIDENCE,
  });
  assert.match(html, /完整地形剖面不进入 API 响应/);
  assert.doesNotMatch(html, /full_terrain_profile_included":true/);
});

test('explanation panel embeds the LOS block without touching planning sections', () => {
  const html = renderRadarSurveillanceLayoutPanel({
    radar_terrain_los: STATUS,
    radar_surveillance_layout: {status: 'not_calculated', items: []},
  });
  assert.match(html, new RegExp(RADAR_TERRAIN_LOS_TITLE));
  assert.match(html, /proposal_only/);
});

test('diagnosis loader posts to the explicit endpoint and forwards prior fingerprint', async () => {
  const calls = [];
  let flow = {
    radar_terrain_los_diagnosis: {
      input_fingerprint: 'a'.repeat(64),
      algorithm_version: 'radar_terrain_los_v1_straight_ray_sampled_profile',
      evaluation_scope: 'bounded_tower_probe_sample',
    },
  };
  const evidence = await loadRadarTerrainLosDiagnosis({
    api: async (url, options) => {
      calls.push({url, options});
      return {...EVIDENCE, evidence_status: 'stale'};
    },
    getFlow: () => flow,
    setFlow: next => { flow = next; },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, RADAR_TERRAIN_LOS_DIAGNOSE_ENDPOINT);
  assert.equal(calls[0].options.method, 'POST');
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.prior_evidence.input_fingerprint, 'a'.repeat(64));
  assert.equal(evidence.evidence_status, 'stale');
  assert.equal(flow.radar_terrain_los_diagnosis.evidence_status, 'stale');
  //: 新诊断会清掉旧的明细页，避免把旧明细当作本次证据。
  assert.equal(flow.radar_terrain_los_details, null);
});

test('details loader is bounded and never asks the backend to recompute', async () => {
  const calls = [];
  let flow = {};
  await loadRadarTerrainLosDetails({
    api: async url => {
      calls.push(url);
      return {detail_page: {records: [], matched_record_count: 0, limit: 50, max_limit: 200}};
    },
    getFlow: () => flow,
    setFlow: next => { flow = next; },
    filters: {tower_id: 'T-1', limit: 50, ignored: null, empty: ''},
  });
  assert.equal(calls[0].startsWith(RADAR_TERRAIN_LOS_DETAILS_ENDPOINT + '?'), true);
  assert.match(calls[0], /tower_id=T-1/);
  assert.match(calls[0], /limit=50/);
  assert.doesNotMatch(calls[0], /ignored|empty/);
  assert.equal(flow.radar_terrain_los_details.limit, 50);
});
