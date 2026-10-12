/**
 * Round34-T07-I2：地形 + 建筑**联合** LOS 只读诊断的前端契约测试（Step05）。
 *
 * 只验证**纯函数**、HTML 字符串契约与只读 API 调用形状：不启动浏览器、不访问网络、
 * 不读 DEM / GPKG。
 *
 * 覆盖的关键约定：
 *  1. 默认加载只消费轻量状态，**不**触发任何计算入口；
 *  2. 三态、完成度、遮挡分类、指纹与时效一律**原样转印**，前端不重算；
 *  3. 可信遮挡与预测性遮挡**分开**披露；
 *  4. ``unknown`` 绝不渲染成 clear；``geometry_filtered`` 绝不渲染成物理 unknown；
 *  5. 固定中文声明与「未评估维度」边界必须出现。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BUILDING_DOMAIN_STATE_TEXT,
  RADAR_COMBINED_LOS_DETAILS_ENDPOINT,
  RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT,
  RADAR_COMBINED_LOS_DISCLAIMER,
  RADAR_COMBINED_LOS_STATE_TEXT,
  RADAR_COMBINED_LOS_STATUS_ENDPOINT,
  RADAR_COMBINED_LOS_TITLE,
  radarCombinedLosModel,
  renderRadarCombinedLosBlock,
} from '../cns_planner/web/js/workflow/radar_visibility_evidence.js';
import {
  loadRadarCombinedLosDetails,
  loadRadarCombinedLosDiagnosis,
  loadRadarCombinedLosStatus,
  renderRadarSurveillanceLayoutPanel,
} from '../cns_planner/web/js/workflow/radar_surveillance_layout.js';
import {
  bind as bindStep05,
  resetRadarCombinedLosStatusGuard,
} from '../cns_planner/web/js/workflow/step05_cns.js';

const STATUS = {
  adapter_id: 'radar_combined_los_read_only_evidence_v1',
  evidence_status: 'not_requested',
  algorithm_version: 'radar_combined_visibility_v2',
  schema_version: 'radar_combined_los_evidence_v1',
  evaluation_scope: 'bounded_single_site_probe_combined_los',
  available_to_compute: true,
  detail_available: false,
  adapter_availability: {
    terrain_dtm: {configured: true, file_name: 'Zhoushan_FABDEM_DTM_30m.tif', opened: false},
    buildings: {
      configured: true, role: 'buildings', file_name: 'zhoushan_buildings_v3.gpkg',
      opened: false, spatial_index_checked: false,
    },
    metric_transform: {available: true, metric_crs: 'EPSG:32651'},
    tower_inventory: {tower_count: 3},
    altitude_layer: {altitude_layer_id: 'ALT-080', nominal_altitude_m: 80},
  },
  tower_options: [{tower_id: 'T1'}, {tower_id: 'T2'}],
  altitude_layer_options: [
    {altitude_layer_id: 'ALT-080', nominal_altitude_m: 80},
    {altitude_layer_id: 'ALT-100', nominal_altitude_m: 100},
  ],
};

const EVIDENCE = {
  adapter_id: 'radar_combined_los_read_only_evidence_v1',
  schema_version: 'radar_combined_los_evidence_v1',
  fingerprint_schema_version: 'radar_combined_los_input_fingerprint_v2',
  evidence_status: 'computed',
  algorithm_version: 'radar_combined_visibility_v2',
  evaluation_scope: 'bounded_single_site_probe_combined_los',
  authoritative_project_result: false,
  production_enforced_enabled: false,
  strict_and_engineering_estimate_separate: true,
  has_project_state_writer: false,
  writes_project_state: false,
  writes_artifacts: false,
  participates_in_milp: false,
  input_fingerprint: 'combined-fp-1',
  request_id: 'request-1',
  generation: 1,
  evaluated_tower_count: 1,
  candidate_tower_count: 3,
  ray_sample_count: 2,
  ray_sample_limit: 12,
  ray_sample_truncated: false,
  bounds: {max_tower_count: 1, probe_budget: 6, max_ray_total: 48, detail_page_limit: 20},
  truncation: {
    evaluated_probe_count: 2, unevaluated_probe_count: 0,
    probe_budget_per_tower: 2, max_ray_total: 48,
  },
  sampling: {sampling_interval_m: 5, envelope_radius_m: 20},
  altitude_layer: {altitude_layer_id: 'ALT-080', nominal_altitude_m: 80,
    vertical_reference: 'egm2008_orthometric'},
  source_provenance: {metric_crs: 'EPSG:32651', buildings_role: 'buildings'},
  terrain_source_identity: {file_name: 'Zhoushan_FABDEM_DTM_30m.tif'},
  building_source_identity: {
    file_name: 'zhoushan_buildings_v3.gpkg', source_version: 'round35_buildings_v3',
    spatial_index_available: true,
  },
  dem_open_count: 2,
  building_open_count: 1,
  not_evaluated: ['detection_probability', 'radar_equation', 'clutter'],
  user_disclaimer_zh: RADAR_COMBINED_LOS_DISCLAIMER,
  aggregate: {
    terrain_los_status_counts: {clear: 2, blocked: 0, unknown: 0},
    building_los_status_counts: {clear: 0, blocked: 1, unknown: 1},
    combined_los_status_counts: {clear: 0, blocked: 1, unknown: 1},
    computation_completion_counts: {complete: 2, incomplete: 0, not_evaluated: 0},
    evidence_completion_counts: {complete: 1, incomplete: 1, not_evaluated: 0},
    building_domain_state_counts: {evaluated: 1, geometry_filtered: 1, not_evaluated: 0},
    height_evidence_level_counts: {source_tagged: 1},
    evaluated_ray_count: 2,
    trustworthy_obstruction_ray_count: 1,
    predictive_obstruction_ray_count: 0,
    geometry_filtered_ray_count: 1,
    building_not_evaluated_ray_count: 0,
    unknown_combination_count: 1,
  },
  execution_receipt: {
    elapsed_seconds_is_receipt_only: true,
    lookup_snapshot: {building_ray_count: 1, building_exact_intersection_count: 1},
  },
  rays: [
    {
      ray_id: 'ray-a', tower_id: 'T1', probe_id: 'R:0', route_id: 'R',
      terrain_los_status: 'clear', building_los_status: 'blocked',
      combined_los_status: 'blocked', building_domain_state: 'evaluated',
      computation_completion: 'complete', evidence_completion: 'complete',
      trustworthy_obstruction: true, predictive_obstruction: false,
      trustworthy_obstruction_count: 1, predictive_obstruction_count: 0,
      building_obstruction_hit_count: 1,
      height_evidence_counts: {source_tagged: 1},
      ray_geometry: {slant_distance_m: 812.5, horizontal_crs: 'EPSG:32651'},
      reason: 'confirmed_building_prism_intersection',
    },
    {
      ray_id: 'ray-b', tower_id: 'T1', probe_id: 'R:25', route_id: 'R',
      terrain_los_status: 'clear', building_los_status: 'out_of_equipment_range',
      combined_los_status: 'unknown', building_domain_state: 'geometry_filtered',
      computation_completion: 'incomplete', evidence_completion: 'incomplete',
      trustworthy_obstruction: false, predictive_obstruction: false,
      trustworthy_obstruction_count: 0, predictive_obstruction_count: 0,
      building_obstruction_hit_count: 0,
      ray_geometry: {slant_distance_m: 100.0}, reason: 'outside_inclusive_slant_range',
    },
  ],
  currentness: {
    state: 'no_prior_evidence', stale: false, comparable: false,
    reasons_zh: ['未提供可比较的旧联合 LOS 证据，因此不宣称任何旧结果 stale。'],
    upstream_planning_state_untouched: true,
  },
};

/* --------------------------------------------------------------------- model */

test('model transcribes every domain verdict without recomputing visibility', () => {
  const model = radarCombinedLosModel({radar_combined_los: STATUS, radar_combined_los_diagnosis: EVIDENCE});
  assert.equal(model.evidenceStatus, 'computed');
  assert.equal(model.combinedCounts.blocked, 1);
  assert.equal(model.combinedCounts.unknown, 1);
  assert.equal(model.combinedCounts.clear, 0);
  assert.equal(model.terrainCounts.clear, 2);
  assert.equal(model.buildingCounts.unknown, 1);
  assert.equal(model.evaluatedRayCount, 2);
  assert.equal(model.trustworthyRayCount, 1);
  assert.equal(model.predictiveRayCount, 0);
  assert.equal(model.geometryFilteredRayCount, 1);
  assert.equal(model.unknownCombinationCount, 1);
  assert.equal(model.inputFingerprint, 'combined-fp-1');
  assert.equal(model.productionEnforcedEnabled, false);
  assert.equal(model.readOnlyDeclared, true);
  assert.equal(model.stale, false);
});

test('model exposes selectors but never computes on its own', () => {
  const model = radarCombinedLosModel({radar_combined_los: STATUS});
  assert.equal(model.hasEvidence, false);
  assert.equal(model.availableToCompute, true);
  assert.deepEqual(model.towerOptions.map(item => item.tower_id), ['T1', 'T2']);
  assert.equal(model.altitudeLayerOptions.length, 2);
  assert.equal(model.detailAvailable, false);
});

/* -------------------------------------------------------------------- render */

test('render offers station and altitude-layer selection plus an explicit diagnose button', () => {
  const html = renderRadarCombinedLosBlock({radar_combined_los: STATUS});
  assert.match(html, new RegExp(RADAR_COMBINED_LOS_TITLE));
  assert.match(html, /诊断站址/);
  assert.match(html, /radarCombinedLosTower/);
  assert.match(html, /高度层/);
  assert.match(html, /radarCombinedLosLayer/);
  assert.match(html, /radarCombinedLosProbeBudget/);
  assert.match(html, /开始联合诊断/);
  assert.match(html, /打开本面板或读取项目状态<b>不会<\/b>读取 DEM \/ GPKG/);
  assert.doesNotMatch(html, /联合直达视线/);
  // 未请求时不渲染任何 synthetic 提示：还没有任何诊断证据可声明为隔离示例。
  assert.doesNotMatch(html, /隔离诊断示例/);
  assert.doesNotMatch(html, /radarCombinedLosDetails/);
});

test('explicit altitude-layer selection stays available when no implicit authority exists', () => {
  const selectable = {
    ...STATUS, available_to_compute: false,
    available_with_explicit_altitude_layer: true,
    adapter_availability: {
      ...STATUS.adapter_availability,
      altitude_layer: {configured: false, reason: 'altitude_layer_not_configured'},
    },
  };
  const model = radarCombinedLosModel({radar_combined_los: selectable});
  assert.equal(model.availableToCompute, true);
  assert.doesNotMatch(renderRadarCombinedLosBlock({radar_combined_los: selectable}),
    /id="runRadarCombinedLos" disabled/);
});

test('engineering demo route is explicit, labeled and never shown as operational authorization', () => {
  const demoStatus={...STATUS,route_consumption:{
    mode:'engineering_demo_read_only',engineering_demo_mode:true,
    strict_safety_validation_passed:false,engineering_conditional_acceptance:true,
    use_scope:'engineering_demo_only',operational_authorization:false,
    published_to_operational_routes:false,
    limitations:['工程条件接受，严格验证未通过，仅供演示'],
  }};
  const model=radarCombinedLosModel({radar_combined_los:demoStatus});
  assert.equal(model.engineeringDemoMode,true);
  const html=renderRadarCombinedLosBlock({radar_combined_los:demoStatus});
  assert.match(html,/id="radarCombinedLosEngineeringDemoMode"/);
  assert.match(html,/工程条件接受，严格验证未通过，仅供演示/);
  assert.match(html,/operational_authorization=false/);
});

test('render shows verdict counts, trustworthy vs predictive split and failure disclosure', () => {
  const html = renderRadarCombinedLosBlock({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: EVIDENCE,
  });
  assert.match(html, /联合直达视线/);
  assert.match(html, /地形 LOS/);
  assert.match(html, /建筑 LOS/);
  assert.match(html, /可信遮挡（verified \/ source_tagged \/ derived） 1 条射线/);
  assert.match(html, /预测性遮挡（predicted） 0 条射线/);
  assert.match(html, /两类遮挡<b>分开<\/b>披露、绝不合并/);
  assert.match(html, /设备冻结几何排除：这不是物理 unknown/);
  assert.match(html, /未知组合 <b>1<\/b> 条/);
  assert.match(html, /证据不足一律保持 unknown 并 fail-closed/);
  assert.match(html, /zhoushan_buildings_v3\.gpkg/);
  assert.match(html, /radar_combined_visibility_v2/);
  assert.match(html, /combined-fp-1/);
  assert.match(html, /生产 <code>enforced<\/code> 保持未启用（未启用）/);
  assert.match(html, new RegExp(RADAR_COMBINED_LOS_DISCLAIMER));
  assert.match(html, /detection_probability/);
});

test('a filtered ray is rendered as device geometry filtering, never as a physical verdict', () => {
  const html = renderRadarCombinedLosBlock({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: EVIDENCE,
  });
  assert.match(html, new RegExp(BUILDING_DOMAIN_STATE_TEXT.geometry_filtered));
  assert.match(html, /设备几何过滤（不是物理 unknown）/);
});

test('unknown is explicit and never rendered as clear', () => {
  const unknown = {
    ...EVIDENCE,
    aggregate: {
      ...EVIDENCE.aggregate,
      combined_los_status_counts: {clear: 0, blocked: 0, unknown: 2},
      terrain_los_status_counts: {clear: 0, blocked: 0, unknown: 2},
      building_los_status_counts: {clear: 0, blocked: 0, unknown: 2},
      trustworthy_obstruction_ray_count: 0,
      geometry_filtered_ray_count: 0,
      unknown_combination_count: 2,
    },
    rays: EVIDENCE.rays.map(item => ({
      ...item, combined_los_status: 'unknown', building_domain_state: 'evaluated',
      trustworthy_obstruction: false,
    })),
  };
  const html = renderRadarCombinedLosBlock({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: unknown,
  });
  assert.match(html, /unknown（证据不足，fail-closed）/);
  assert.match(html, /绝不<\/b>当作 clear/);
  assert.doesNotMatch(html, /clear（几何直达未被遮挡）/);
});

test('failed diagnosis renders the reason and never claims a clear ray', () => {
  const failed = {
    ...EVIDENCE,
    evidence_status: 'failed',
    failure_reason: 'buildings_not_configured',
    failure_detail: '项目未配置 buildings 数据源路径',
    rays: [],
    aggregate: {},
    currentness: {state: 'unknown', stale: false, reasons_zh: ['诊断未完成']},
  };
  const html = renderRadarCombinedLosBlock({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: failed,
  });
  assert.match(html, /计算失败/);
  assert.match(html, /buildings_not_configured/);
  assert.match(html, /失败<b>不<\/b>等于"无遮挡"/);
  assert.doesNotMatch(html, /clear（几何直达未被遮挡）/);
  assert.equal(radarCombinedLosModel({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: failed,
  }).hasEvidence, false);
  assert.match(html, /id="loadRadarCombinedLosDetails" disabled/);
});

test('stale evidence is announced instead of silently reused', () => {
  const stale = {
    ...EVIDENCE,
    evidence_status: 'stale',
    stale: true,
    currentness: {
      state: 'stale', stale: true, comparable: true,
      reasons_zh: ['联合 LOS 输入（数据源身份 / 站址原点 / 高度层 / 采样 / 射线模型）已变化。'],
    },
  };
  const html = renderRadarCombinedLosBlock({
    radar_combined_los: STATUS, radar_combined_los_diagnosis: stale,
  });
  assert.match(html, /旧证据已 stale/);
  assert.match(html, /不可作为当前联合可见性依据/);
});

test('the Step05 radar panel embeds the combined block exactly once', () => {
  const html = renderRadarSurveillanceLayoutPanel({radar_combined_los: STATUS});
  const heading = '<h4>' + RADAR_COMBINED_LOS_TITLE + '</h4>';
  assert.equal(html.split(heading).length - 1, 1);
  assert.match(html, /runRadarCombinedLos/);
  assert.match(html, /radarCombinedLosTower/);
});

test('state text vocabulary is complete and self-describing', () => {
  for (const key of ['not_requested', 'computed', 'failed', 'stale']) {
    assert.ok(RADAR_COMBINED_LOS_STATE_TEXT[key]);
  }
  assert.equal(RADAR_COMBINED_LOS_STATUS_ENDPOINT, '/api/radar-terrain-los/combined/status');
  assert.equal(RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT, '/api/radar-terrain-los/combined/diagnose');
  assert.equal(RADAR_COMBINED_LOS_DETAILS_ENDPOINT, '/api/radar-terrain-los/combined/details');
});

/* ------------------------------------------------------------------ loaders */

function flowHarness(initial = {}) {
  let flow = initial;
  const calls = [];
  const api = async (url, options) => {
    calls.push({url, options});
    if (url.startsWith(RADAR_COMBINED_LOS_STATUS_ENDPOINT)) return STATUS;
    if (url.startsWith(RADAR_COMBINED_LOS_DETAILS_ENDPOINT)) {
      return {request_id: EVIDENCE.request_id, generation: EVIDENCE.generation,
        input_fingerprint: EVIDENCE.input_fingerprint,
        detail_page: {records: [{ray_id: 'ray-a'}], matched_record_count: 1,
          limit: 20, max_limit: 50}};
    }
    return {...EVIDENCE, input_fingerprint: 'combined-fp-2'};
  };
  return {
    api, calls,
    getFlow: () => flow,
    setFlow: next => { flow = next; },
    read: () => flow,
  };
}

test('diagnose posts the prior fingerprint and stores evidence without recomputing locally', async () => {
  const harness = flowHarness({radar_combined_los_diagnosis: {
    input_fingerprint: 'combined-fp-1',
    schema_version: 'radar_combined_los_evidence_v1',
    algorithm_version: 'radar_combined_visibility_v2',
    fingerprint_schema_version: 'radar_combined_los_input_fingerprint_v2',
    evaluation_scope: 'bounded_single_site_probe_combined_los',
    rays: [{}],
  }});
  await loadRadarCombinedLosDiagnosis({
    api: harness.api, getFlow: harness.getFlow, setFlow: harness.setFlow,
    payload: {tower_ids: ['T1'], probe_budget: 4, altitude_layer_id: 'ALT-080'},
  });
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.calls[0].url, RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT);
  assert.equal(harness.calls[0].options.method, 'POST');
  const body = JSON.parse(harness.calls[0].options.body);
  assert.deepEqual(body.tower_ids, ['T1']);
  assert.equal(body.probe_budget, 4);
  assert.equal(body.prior_evidence.input_fingerprint, 'combined-fp-1');
  assert.equal(body.prior_evidence.schema_version, 'radar_combined_los_evidence_v1');
  assert.equal(body.prior_evidence.algorithm_version, 'radar_combined_visibility_v2');
  assert.equal(body.prior_evidence.fingerprint_schema_version,
    'radar_combined_los_input_fingerprint_v2');
  // 旧明细在重新诊断后失效：不得拿旧页冒充新证据。
  assert.equal(harness.read().radar_combined_los_details, null);
  assert.equal(harness.read().radar_combined_los_diagnosis.input_fingerprint, 'combined-fp-2');
});

test('diagnose omits prior evidence when nothing comparable exists yet', async () => {
  const harness = flowHarness({});
  await loadRadarCombinedLosDiagnosis({
    api: harness.api, getFlow: harness.getFlow, setFlow: harness.setFlow, payload: {},
  });
  const body = JSON.parse(harness.calls[0].options.body);
  assert.equal(body.prior_evidence, undefined);
});

test('details request is bounded pagination and never recomputes', async () => {
  const harness = flowHarness({radar_combined_los_diagnosis: EVIDENCE});
  await loadRadarCombinedLosDetails({
    api: harness.api, getFlow: harness.getFlow, setFlow: harness.setFlow,
    filters: {tower_id: 'T1', limit: 20, combined_los_status: 'blocked', ignored: 'x'},
  });
  const url = harness.calls[0].url;
  assert.ok(url.startsWith(RADAR_COMBINED_LOS_DETAILS_ENDPOINT));
  const query = new URLSearchParams(url.split('?')[1]);
  assert.equal(query.get('tower_id'), 'T1');
  assert.equal(query.get('limit'), '20');
  assert.equal(query.get('combined_los_status'), 'blocked');
  assert.equal(query.get('request_id'), EVIDENCE.request_id);
  assert.equal(query.get('input_fingerprint'), EVIDENCE.input_fingerprint);
  assert.equal(query.get('generation'), String(EVIDENCE.generation));
  assert.equal(query.get('ignored'), null);
  assert.equal(harness.read().radar_combined_los_details.matched_record_count, 1);
});

test('older diagnosis response cannot overwrite a newer request', async () => {
  let resolveFirst;
  let resolveSecond;
  let callCount = 0;
  let flow = {};
  const api = async () => new Promise(resolve => {
    callCount += 1;
    if (callCount === 1) resolveFirst = resolve;
    else resolveSecond = resolve;
  });
  const dependencies = {
    api,
    getFlow: () => flow,
    setFlow: next => { flow = next; },
  };
  const first = loadRadarCombinedLosDiagnosis(dependencies);
  const second = loadRadarCombinedLosDiagnosis(dependencies);
  resolveSecond({...EVIDENCE, request_id: 'new', generation: 2, input_fingerprint: 'new-fp'});
  await second;
  resolveFirst({...EVIDENCE, request_id: 'old', generation: 1, input_fingerprint: 'old-fp'});
  const old = await first;
  assert.equal(old.ui_superseded, true);
  assert.equal(flow.radar_combined_los_diagnosis.request_id, 'new');
  assert.equal(flow.radar_combined_los_diagnosis.input_fingerprint, 'new-fp');
});

test('starting a diagnosis immediately retires old success and details', async () => {
  let resolveRequest;
  let flow = {
    radar_combined_los_diagnosis: EVIDENCE,
    radar_combined_los_details: {request_id: 'request-1', records: [{ray_id: 'old'}]},
  };
  const pending = loadRadarCombinedLosDiagnosis({
    api: async () => new Promise(resolve => { resolveRequest = resolve; }),
    getFlow: () => flow,
    setFlow: next => { flow = next; },
  });
  assert.equal(flow.radar_combined_los_diagnosis, null);
  assert.equal(flow.radar_combined_los_details, null);
  assert.equal(flow.radar_combined_los_request?.state, 'running');
  assert.equal(radarCombinedLosModel(flow).hasEvidence, false);
  resolveRequest({...EVIDENCE, request_id: flow.radar_combined_los_request.request_id});
  await pending;
  assert.equal(flow.radar_combined_los_request, null);
  assert.equal(flow.radar_combined_los_diagnosis.evidence_status, 'computed');
});

test('late details response cannot attach to a different diagnosis', async () => {
  let resolveDetails;
  let flow = {radar_combined_los_diagnosis: EVIDENCE};
  const api = async () => new Promise(resolve => { resolveDetails = resolve; });
  const pending = loadRadarCombinedLosDetails({
    api,
    getFlow: () => flow,
    setFlow: next => { flow = next; },
  });
  flow = {radar_combined_los_diagnosis: {
    ...EVIDENCE, request_id: 'request-2', generation: 2, input_fingerprint: 'combined-fp-2',
  }};
  resolveDetails({
    request_id: 'request-1', generation: 1, input_fingerprint: 'combined-fp-1',
    detail_page: {records: [{ray_id: 'old'}]},
  });
  await pending;
  assert.equal(flow.radar_combined_los_details, undefined);
});

test('late details cannot attach when only generation changed', async () => {
  let resolveDetails;
  let flow = {radar_combined_los_diagnosis: EVIDENCE};
  const pending = loadRadarCombinedLosDetails({
    api: async () => new Promise(resolve => { resolveDetails = resolve; }),
    getFlow: () => flow, setFlow: value => { flow = value; },
  });
  flow = {radar_combined_los_diagnosis: {...EVIDENCE, generation: 2}};
  resolveDetails({request_id: EVIDENCE.request_id,
    input_fingerprint: EVIDENCE.input_fingerprint, generation: 1,
    detail_page: {records: [{ray_id: 'old-generation'}]}});
  await pending;
  assert.equal(flow.radar_combined_los_details, undefined);
  assert.equal(radarCombinedLosModel({
    radar_combined_los_diagnosis: {...EVIDENCE, generation: 2},
    radar_combined_los_details: {request_id: EVIDENCE.request_id,
      input_fingerprint: EVIDENCE.input_fingerprint, generation: 1, records: [{}]},
  }).detailPage, null);
});

test('status loader only reads the light status endpoint', async () => {
  const harness = flowHarness({});
  const status = await loadRadarCombinedLosStatus({
    api: harness.api, getFlow: harness.getFlow, setFlow: harness.setFlow,
  });
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.calls[0].url, RADAR_COMBINED_LOS_STATUS_ENDPOINT);
  assert.equal(harness.calls[0].options, undefined);
  assert.equal(status.evidence_status, 'not_requested');
  assert.equal(harness.read().radar_combined_los.evidence_status, 'not_requested');
});

test('loaders refuse to run without the three required dependencies', async () => {
  await assert.rejects(() => loadRadarCombinedLosDiagnosis({}), /api \/ getFlow \/ setFlow/);
  await assert.rejects(() => loadRadarCombinedLosDetails({}), /api \/ getFlow \/ setFlow/);
  await assert.rejects(() => loadRadarCombinedLosStatus({}), /api \/ getFlow \/ setFlow/);
});

/* ---------------------------------------------------------------------------
 * H2：Step05 面板**必须**自己装载一次联合 LOS 轻量状态。
 *
 * H1 时 ``loadRadarCombinedLosStatus`` 只有定义、没有任何生产调用点，于是
 * ``flow.radar_combined_los`` 永远是 undefined —— 面板既看不到真实阻塞原因，
 * "开始联合诊断"按钮也永久禁用（用户根本无法发起联合诊断）。
 * 这些用例把"面板首次渲染只请求一次 ``/combined/status``、且绝不触发任何计算"
 * 固定成契约。
 * ------------------------------------------------------------------------- */

function bindStub(flow, elements) {
  let current = flow;
  const calls = [];
  const errors = [];
  const api = async (url, options) => {
    calls.push({url, options});
    if (String(url).startsWith(RADAR_COMBINED_LOS_STATUS_ENDPOINT)) return STATUS;
    throw new Error('本用例只允许请求轻量 status：' + url);
  };
  //: Step05 的 bind() 会对已挂载控件直接赋值（例如 ``.onclick``），因此这里必须
  //: 返回一个"可读写任意属性"的弹性控件替身，而不是裸对象。
  const control = id => new Proxy({id, value: '', checked: false}, {
    get: (target, key) => (key in target ? target[key] : () => {}),
    set: (target, key, value) => { target[key] = value; return true; },
  });
  const mounted = new Set([...elements, 'browseExisting', 'browseCandidates',
    'existing_cnsPath', 'candidate_sitesPath']);
  const c = {
    $: id => (mounted.has(id) ? control(id) : null),
    api,
    flow: () => current,
    setFlow: next => { current = next; },
    actionButton: () => {},
    mutate: () => {},
    mutateAndRefresh: () => {},
    refresh: () => {},
    resourceAction: () => {},
    resourceMutationAndRefresh: () => {},
    openBrowser: () => {},
    afterFlowChange: () => {},
    panelError: message => errors.push(String(message)),
    refreshCorridorDetail: null,
  };
  return {c, calls, errors, read: () => current};
}

test('Step05 首次渲染联合面板时只请求一次轻量 status，且不触发任何计算', async () => {
  resetRadarCombinedLosStatusGuard();
  const stub = bindStub({}, ['radarCombinedLosTower', 'runRadarCombinedLos',
    'loadRadarCombinedLosDetails']);
  bindStep05(stub.c);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stub.calls.length, 1, '面板首次展开只允许一次轻量状态请求');
  assert.equal(stub.calls[0].url, RADAR_COMBINED_LOS_STATUS_ENDPOINT);
  assert.equal(stub.calls[0].options, undefined, '轻量状态必须是只读 GET');
  assert.equal(stub.read().radar_combined_los.evidence_status, 'not_requested');
  // 状态落地触发的重新渲染**不得**再次请求（否则会退化成轮询）。
  bindStep05(stub.c);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stub.calls.length, 1, '重新渲染不得重复请求轻量状态');
});

test('Step05 demo adoption requests only the explicit engineering demo status scope', async () => {
  resetRadarCombinedLosStatusGuard();
  const demo={status:'published_engineering_demo',current_applicability:'current',
    use_scope:'engineering_demo_only',engineering_conditional_acceptance:true,
    strict_safety_validation_passed:false,operational_authorization:false,
    published_to_operational_routes:false};
  const stub=bindStub({layered_operational_adoptions:{items:[demo]}},
    ['radarCombinedLosTower','runRadarCombinedLos','loadRadarCombinedLosDetails']);
  bindStep05(stub.c);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(stub.calls.length,1);
  assert.equal(stub.calls[0].url,
    RADAR_COMBINED_LOS_STATUS_ENDPOINT+'?engineering_demo_mode=true');
});

test('Step05 面板不存在时不请求联合状态', async () => {
  resetRadarCombinedLosStatusGuard();
  const stub = bindStub({}, []);
  bindStep05(stub.c);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stub.calls.length, 0);
});
