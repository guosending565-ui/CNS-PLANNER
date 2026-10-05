/**
 * Round 30-C2B 前端收口：P17 首次探测距离**证据覆盖状态**的中文映射。
 *
 * 背景：P17 2.2 起，Radar 规划探测代理的 ``coverage_status`` 是
 * ``planning_proxy_validated`` —— 它**只**表示"该探测距离的几何代理已通过规划期验证"，
 * 绝不表示"Radar 服务覆盖/冗余已满足"。本测试锁定三件事：
 *
 * 1. 该枚举不再裸显英文，显示为「规划代理已验证」，并**同时**给出语义边界说明；
 * 2. 冻结词表 ``satisfied`` / ``confirmed_deficit`` / ``unknown`` 的中文原样不变；
 * 3. 前端只做展示映射：绝不因该状态推导 Radar 是否满足，也绝不改写后端原值。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DETECTION_COVERAGE_STATUS_TEXT, DETECTION_PLANNING_PROXY_NOTE, SERVICE_STATUS_TEXT,
  detectionCoverageStatusNote, detectionCoverageStatusText, serviceStatusText,
} from '../cns_planner/web/js/map/service_semantics.js';
import {
  continuousServiceModel, renderContinuousServicePanel, threatLayerRows,
} from '../cns_planner/web/js/workflow/continuous_service.js';

const PROXY_STATUS = 'planning_proxy_validated';
const PROXY_TEXT = '规划代理已验证';
const PROXY_NOTE_FRAGMENT = '不代表 Radar 服务覆盖/冗余已满足';

function routeWith(coverageStatus) {
  return {
    route_id: 'R0005', status: 'unacceptable', plan_stage: 'baseline',
    primary_threat_layer: 'cooperative', supplementary_threat_layer: 'noncooperative',
    primary_threat_status: 'unacceptable', supplementary_threat_status: 'unacceptable',
    supplementary_threat_is_limitation: false,
    reason_codes: ['service_outage_exceeds_limit'],
    reasons: ['连续全失联时长超过服务中断阈值'],
    corridor: {
      status: 'evaluated', formula: 'F', D_separation_m: 30, D_maneuver_m: 50,
      D_uncertainty_m: 10, V_relative_mps: 40, T_chain_s: 10, D_protection_m: 0,
      route_length_m: 10000, relative_speed_basis: 'conservative',
      cns_requirement_corridor_half_width_m: 500, outer_half_width_m: 500,
    },
    first_detection_evidence: {
      first_detection_distance_m: 3000, service_key: 'S:radar_noncooperative',
      coverage_status: coverageStatus, usable: true,
      source_type: 'planning_proxy_not_measured_geometry',
      authority: 'radar_layout_planning_proxy',
      detection_distance_usable: true, detection_distance_status: PROXY_STATUS,
      service_coverage_status: 'confirmed_deficit',
      semantics: 'first_detection_distance_from_declared_geometry_only',
    },
    surveillance_acceptance: {
      status: 'acceptable', t_available_s: 74.25, t_margin_s: 64.25,
      reason: '保护链时间余量非负',
    },
    threat_layers: {
      cooperative: {
        layer: 'cooperative', label: '合作无人机 / RID 合作监视（主要威胁）',
        service_keys: ['S:rid_cooperative'], subsystems: ['nominal'], status: 'nominal',
        t_margin_s: 64.25, first_detection_distance_m: 3000, limitations: [], reasons: [],
      },
      noncooperative: {
        layer: 'noncooperative', label: '非合作无人机 / Radar 非合作监视（补充威胁）',
        service_keys: ['S:radar_noncooperative'], subsystems: ['nominal'], status: 'nominal',
        t_margin_s: 64.25, first_detection_distance_m: 3000, limitations: [], reasons: [],
      },
    },
    subsystems: [
      {
        subsystem: 'C', service: 'C:communication', status: 'unacceptable',
        events: [], event_count: 0, longest_event: null, managed_gaps: [],
      },
      {
        subsystem: 'S', service: 'S:surveillance', status: 'nominal',
        events: [], event_count: 0, longest_event: null, managed_gaps: [],
      },
    ],
    events: [], events_by_kind: {}, longest_events: {}, managed_gaps: [], limitations: [],
  };
}

function flowWith(coverageStatus) {
  return {
    cns_continuous_service: {
      //: 投影结构：``result`` 是权威结论本体（与后端 ``/api/workflow`` 同形）。
      result: resultWith(coverageStatus),
      parameters: {semantics: 'continuous_service_parameters', parameters: {}},
      policy: {}, operation_scenario: {}, fc30: {}, step6_gate: {
        status: 'unacceptable', confirmation_allowed: false,
        allowed_statuses: ['fully_satisfied', 'acceptable_with_managed_gap'],
        projected_status: 'unacceptable', limitations: [], reasons: [],
      },
    },
    steps: {5: true},
  };
}

function resultWith(coverageStatus) {
  return {
    status: 'unacceptable', baseline_status: 'unacceptable', post_plan_status: 'unacceptable',
    algorithm_id: 'continuous_service_acceptability_v1', algorithm_version: '2.2',
    schema_version: 'round2.6-post-plan-continuous-service-acceptability',
    reason_codes: ['service_outage_exceeds_limit'], limitations: [],
    route_count: 1, unacceptable_count: 1, unknown_count: 0,
    disclosure_lines: [],
    threat_layer_semantics: (
      'cooperative_rid_primary_and_noncooperative_radar_supplementary_'
      + 'evaluated_separately_never_merged'
    ),
    routes: [routeWith(coverageStatus)],
    baseline: {
      status: 'unacceptable', algorithm_version: '2.2',
      reason_codes: ['service_outage_exceeds_limit'], routes: [routeWith(coverageStatus)],
    },
    post_plan_projection: null,
  };
}

// ---------------------------------------------------------------------------
// 1. 中文映射（planning_proxy_validated 绝不裸显英文）
// ---------------------------------------------------------------------------

test('planning_proxy_validated 显示为中文，且不再裸显英文枚举', () => {
  assert.equal(DETECTION_COVERAGE_STATUS_TEXT[PROXY_STATUS], PROXY_TEXT);
  assert.equal(detectionCoverageStatusText(PROXY_STATUS), PROXY_TEXT);
  assert.ok(!detectionCoverageStatusText(PROXY_STATUS).includes('planning_proxy'));
});

test('satisfied / confirmed_deficit / unknown 的原中文映射不变', () => {
  assert.equal(SERVICE_STATUS_TEXT.satisfied, '满足');
  assert.equal(SERVICE_STATUS_TEXT.confirmed_deficit, '已确认缺口');
  assert.equal(SERVICE_STATUS_TEXT.unknown, '证据不足');
  for (const key of ['satisfied', 'confirmed_deficit', 'unknown']) {
    assert.equal(detectionCoverageStatusText(key), serviceStatusText(key));
  }
});

test('未登记的覆盖状态原样转印（前端绝不编造结论）', () => {
  assert.equal(detectionCoverageStatusText('not_satisfied'), 'not_satisfied');
  assert.equal(detectionCoverageStatusText(null), '证据不足');
});

test('规划代理状态必须同时披露语义边界', () => {
  const note = detectionCoverageStatusNote(PROXY_STATUS);
  assert.equal(note, DETECTION_PLANNING_PROXY_NOTE);
  assert.match(note, new RegExp(PROXY_NOTE_FRAGMENT));
  assert.match(note, /几何验证/);
  for (const key of ['satisfied', 'confirmed_deficit', 'unknown', 'not_satisfied']) {
    assert.equal(detectionCoverageStatusNote(key), '');
  }
  assert.equal(detectionCoverageStatusNote(null), '');
});

// ---------------------------------------------------------------------------
// 2. 渲染：中文 + 语义边界说明同时出现
// ---------------------------------------------------------------------------

test('P17 面板把代理状态渲染成中文并附语义边界说明', () => {
  const html = renderContinuousServicePanel(flowWith(PROXY_STATUS), []);
  assert.match(html, new RegExp(PROXY_TEXT));
  assert.match(html, new RegExp(PROXY_NOTE_FRAGMENT));
  assert.match(html, /证据覆盖状态/);
  assert.ok(
    !html.includes(PROXY_STATUS),
    '正文里不得再出现裸英文枚举 planning_proxy_validated',
  );
});

test('非代理覆盖状态不显示代理文案', () => {
  for (const status of ['satisfied', 'confirmed_deficit', 'unknown']) {
    const html = renderContinuousServicePanel(flowWith(status), []);
    assert.ok(!html.includes(PROXY_TEXT), `${status} 不得显示代理文案`);
    assert.ok(!html.includes(PROXY_STATUS), `${status} 不得显示代理枚举`);
  }
  assert.match(renderContinuousServicePanel(flowWith('confirmed_deficit'), []), /已确认缺口/);
});

// ---------------------------------------------------------------------------
// 3. 不从前端推导 Radar 是否满足
// ---------------------------------------------------------------------------

test('coverage_status 变化不影响威胁分层结论（前端不推导）', () => {
  const proxyRows = threatLayerRows(continuousServiceModel(flowWith(PROXY_STATUS)));
  const satisfiedRows = threatLayerRows(continuousServiceModel(flowWith('satisfied')));
  assert.deepEqual(proxyRows, satisfiedRows);
  //: 补充威胁仍然如实来自后端（``nominal`` 子系统 ≠ satisfied 服务覆盖）。
  const supplementary = proxyRows.find(row => row.layer === 'noncooperative');
  assert.equal(supplementary.isSupplementary, true);
});

test('模型只转印后端原值，绝不改写 coverage_status', () => {
  const model = continuousServiceModel(flowWith(PROXY_STATUS));
  const detection = model.result.routes[0].first_detection_evidence;
  assert.equal(detection.coverage_status, PROXY_STATUS);
  assert.equal(detection.service_coverage_status, 'confirmed_deficit');
  assert.equal(detection.detection_distance_usable, true);
});
