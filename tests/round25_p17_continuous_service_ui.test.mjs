/**
 * Round 2.5 前端回归：Step5「连续服务可接受性（P17）」面板 + Step6 门禁投影。
 *
 * 用户裁定：
 *  * 面板必须显示 C / N / S 连续事件、最长连续缺口、持续时间、阈值、
 *    保护走廊参数、T_margin 与最终运行可接受性；
 *  * `coverage gap ≠ 自动 planning failure` 必须在文案里说清；
 *  * managed_gap 必须**强制披露**（service / 位置 / 长度 / 时长 / 阈值 / mitigation /
 *    依据），且**绝不**被表述为"全覆盖"；
 *  * 参数逐项标明 authority（显式证据 / 外部参考 / 内置工程基线），
 *    并把 3 s 的定位写成「FC30 设备 failsafe 触发事实，不是法规阈值」；
 *  * unknown / unacceptable 一律显示为阻止进入正式方案评审（fail-closed）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ACCEPTABILITY_TEXT, CONTINUOUS_SERVICE_EVALUATE_ENDPOINT,
  CONTINUOUS_SERVICE_POLICY_ENDPOINT, OPERATION_SCENARIO_ENDPOINT, STEP6_ALLOWED_ACCEPTABILITY,
  bindContinuousServicePanel, continuousEventRows, continuousServiceModel, longestGapRows,
  managedGapRows, parameterRows, protectionCorridorRows, renderContinuousServicePanel,
  step6GateModel,
} from '../cns_planner/web/js/workflow/continuous_service.js';
import {
  CNS_CANONICAL_CHAIN, render as renderStep05,
} from '../cns_planner/web/js/workflow/step05_cns.js';

// ---------------------------------------------------------------------------
// fixture：与后端 `/api/workflow` 的 `cns_continuous_service` 投影同形
// ---------------------------------------------------------------------------

const PARAMETERS = {
  semantics: 'continuous_service_parameters_with_per_parameter_authority',
  limits_resolution_order: 'cns_continuous_service_policy → planning_evidence 显式记录 → 内置工程基线',
  parameters: {
    c_full_outage_max_s: {
      field: 'c_full_outage_max_s', label: '通信全失联可接受最长时长',
      value: 3, unit: 's', authority: 'builtin_engineering_assumption',
      source_type: 'internal_baseline',
      source: 'Round 2.5 v1 engineering baseline：取 FC30 failsafe 触发门限'
        + '（遥控信号丢失超过 3 s 触发 RTH）作为设备故障保护事实驱动的工程基线，不是法规阈值。',
      statement: '通信全失联可接受最长时长＝3 s（工程基线）。',
      report_disclosure: '本阈值为工程基线，其依据是 FC30 设备 failsafe 触发事实；不代表任何法规要求。',
      reason: '未提供显式工程依据：采用内置工程基线（默认值）',
    },
    navigation_degradation_time_s: {
      field: 'navigation_degradation_time_s', label: '导航降级允许时长（外部参考，不作为硬门）',
      value: 13, unit: 's', authority: 'explicit_evidence', source_type: 'external_reference',
      source: '公开研究资料', external_reference: '公开资料 REF-001：RTK 恢复约 9–13 s',
      statement: '导航降级允许时长为外部研究参考，不作为本评估的硬门。',
      reason: '采用显式登记的外部研究参考值（external_reference）',
    },
    rtk_availability: {
      field: 'rtk_availability', label: 'RTK 可用性', value: 'not_available',
      authority: 'derived_from_route_service_evidence', source_type: 'confirmed_source_fact',
      source: 'P15 航路内 N:rtk_augmentation 服务证据',
      reason: '航路内 RTK 增强服务存在已确认缺口 ⇒ 该段 RTK 视为不可用',
    },
  },
};

const EVENT_OUTAGE = {
  event_id: 'R0005:C:spatial-deficit:1:service_outage', kind: 'service_outage',
  service: 'C:communication', subsystem: 'C', route_id: 'R0005',
  start_offset_m: 120.5, end_offset_m: 180.5, position_basis: 'p15_segment_bounds',
  length_m: 60, duration_s: 3, duration_formula: 'along_route_gap_m / current_route_speed_mps',
  route_speed_mps: 20, limit_s: 3, exceeds_limit: false,
  mitigation: '复用既有站址缩小该段',
  basis: 'threshold=c_full_outage_max_s（authority=builtin_engineering_assumption）',
  semantics: 'conservative_longitudinal_projection_of_p15_confirmed_deficit',
};

const RESULT = {
  status: 'acceptable_with_managed_gap',
  algorithm_id: 'continuous_service_acceptability_v1',
  algorithm_version: '1.0',
  input_fingerprint: 'p17-fp',
  route_speed_mps: 20,
  speed_basis: {
    route_speed_mps: 20, route_speed_source: 'selected_aircraft_profile.cruise_speed_mps',
    fc30_route_speed_mps: 15,
  },
  service_acceptability_limits: {
    C: {service_outage: 3, redundancy_degradation: 10},
    S: {service_outage: 3, redundancy_degradation: 10},
  },
  route_count: 1,
  routes: [{
    route_id: 'R0005', status: 'acceptable_with_managed_gap', route_length_m: 9525.66,
    route_speed_mps: 20,
    corridor: {
      semantics: 'horizontal_route_protection_corridor_not_route_centerline_only',
      route_path: [[122.26, 29.86], [122.18, 29.82]], route_length_m: 9525.66,
      cns_requirement_corridor_half_width_m: 500, D_safety_m: 0, D_uncertainty_m: 0,
      V_relative_mps: 40, relative_speed_basis: 'conservative', T_chain_s: 10,
      T_chain_components_s: {
        detect_track: 3, sensor_to_platform: 1, platform_processing: 2,
        platform_to_aircraft: 1, aircraft_response_manoeuvre: 3,
      },
      D_protection_m: 400, outer_half_width_m: 900,
      formula: 'D_protection = D_safety + V_relative * T_chain + D_uncertainty',
      status: 'evaluated',
    },
    first_detection_evidence: {
      first_detection_distance_m: 5000, service_key: 'S:rid_cooperative',
      coverage_status: 'satisfied', usable: true, source_type: 'declared_geometry',
    },
    surveillance_acceptance: {
      status: 'acceptable', t_available_s: 125, t_margin_s: 115,
      reason: '保护链时间余量非负',
    },
    subsystems: [
      {
        subsystem: 'C', service: 'C:communication', status: 'acceptable_with_managed_gap',
        events: [EVENT_OUTAGE], event_count: 1, limits_s: {service_outage: 3, redundancy_degradation: 10},
        longest_event: EVENT_OUTAGE, managed_gaps: [EVENT_OUTAGE],
      },
      {
        subsystem: 'N', service: 'N:route_containment', status: 'acceptable_degraded',
        events: [{
          event_id: 'R0005:N:navigation_degradation', kind: 'navigation_degradation',
          service: 'N:route_containment', subsystem: 'N', route_id: 'R0005',
          start_offset_m: 0, end_offset_m: 9525.66, length_m: 9525.66,
          duration_s: null, limit_s: null, exceeds_limit: false,
          mitigation: '以 GNSS 回退维持航路包含（精度已逐项核对满足要求）',
          basis: 'rtk=derived_from_route_service_evidence；gnss=engineering_assumption',
          semantics: 'rtk_to_gnss_fallback_state_machine_not_fixed_duration_threshold',
        }],
        event_count: 1, longest_event: {kind: 'navigation_degradation', length_m: 9525.66},
        managed_gaps: [],
        rtk_recovery_external_reference: {
          reference: 'RTK 恢复时长 9–13 s（外部研究参考）',
          role: 'external_reference_only_not_a_gate',
        },
      },
      {
        subsystem: 'S', service: 'S:rid_cooperative', status: 'nominal',
        events: [], event_count: 0, longest_event: null, managed_gaps: [],
      },
      {
        subsystem: 'S', service: 'S:rid_cooperative', status: 'unacceptable',
        events: [{
          event_id: 'R0005:S:surveillance_detection_gap', kind: 'surveillance_detection_gap',
          service: 'S:rid_cooperative', subsystem: 'S', route_id: 'R0005',
          start_offset_m: 0, end_offset_m: 2000, length_m: 2000, duration_s: null,
          limit_s: null, exceeds_limit: true, mitigation: '在保护走廊内补齐监视覆盖',
          basis: '保护走廊内按 surface 的监视服务独立站址覆盖结论',
          semantics: 'protection_corridor_coverage_gap_not_route_centerline_gap',
        }],
        event_count: 1, longest_event: {kind: 'surveillance_detection_gap', length_m: 2000},
        managed_gaps: [], acceptance: {
          status: 'acceptable', t_available_s: 125, t_margin_s: 115, reason: '保护链时间余量非负',
        },
      },
    ],
    events_by_kind: {
      service_outage: [EVENT_OUTAGE],
      redundancy_degradation: [],
      navigation_degradation: [],
      surveillance_detection_gap: [{
        event_id: 'R0005:S:surveillance_detection_gap', kind: 'surveillance_detection_gap',
        service: 'S:rid_cooperative', subsystem: 'S', route_id: 'R0005',
        start_offset_m: 0, end_offset_m: 2000, length_m: 2000, exceeds_limit: true,
      }],
    },
    managed_gaps: [EVENT_OUTAGE],
    reasons: [],
    reason_codes: [],
  }],
  parameters: PARAMETERS.parameters,
  reasons: [],
  reason_codes: [],
  managed_gap_count: 1, unacceptable_count: 0, unknown_count: 0,
  disclosure_lines: [
    '[managed gap] service=C:communication 类型=服务中断（全失联） 位置=航路 R0005 起算 120.5–180.5 m'
      + ' 连续长度=60.0 m 预计持续时间=3.0 s 阈值=3.0 s',
    '  缓解措施（mitigation）：复用既有站址缩小该段',
    '  依据/假设（basis）：threshold=c_full_outage_max_s（authority=builtin_engineering_assumption）',
    '  披露语义：本段并非全覆盖；缺口真实存在，仅因连续时长在工程阈值内被接受为「有管理的缺口」（managed gap）。',
  ],
  not_evaluated: {common_cause: 'not_evaluated', runtime_outage: 'not_evaluated'},
};

const PROJECTION = {
  result: RESULT,
  parameters: PARAMETERS,
  policy: {
    status: 'configured',
    service_acceptability_limits: {C: {service_outage: 3, redundancy_degradation: 10}},
    source: 'pytest fixture', confirmed: true,
  },
  operation_scenario: {
    single_ownship: true, intruder_scope: 'other_uav_only', cruise_altitude: 'ALT-100',
    design_intruder_speed_mps: 20, nominal_closing_speed_mps: 35,
    conservative_closing_speed_mps: 40,
    intruder_speed_source_type: 'engineering_assumption',
    intruder_speed_source: 'Round 2.5 工程假设：设计入侵者速度 20 m/s',
  },
  fc30: {
    aircraft_id: 'FC30', selected_aircraft_id: 'AIRCRAFT-SYN-E2E-01', is_selected: false,
    facts: [{
      parameter: 'route_speed_mps', value: 15, unit: 'm/s', source_type: 'confirmed_source_fact',
      source: 'FC30 机载档案（canonical）',
      statement: 'FC30 航路（巡航）速度为 15 m/s；作为规划基线使用。',
    }, {
      parameter: 'rc_loss_failsafe_trigger_s', value: 3, unit: 's',
      source_type: 'confirmed_source_fact', not_a_regulatory_threshold: true,
      source: 'FC30 机载档案（canonical）：Failsafe RTH 配置下，遥控信号丢失超过 3 s 触发自动返航。',
      statement: '在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失超过 3 s 触发 RTH。',
    }],
    failsafe_trigger_semantics: 'device_failsafe_trigger_fact_not_regulatory_threshold',
    not_a_regulatory_threshold: true,
    disclosure: 'FC30 的 3 s 是设备 failsafe 触发门限，不是法规阈值。',
  },
  step6_gate: {
    status: 'acceptable_with_managed_gap', confirmation_allowed: true,
    allowed_statuses: ['fully_satisfied', 'acceptable_with_managed_gap'],
    managed_gap_count: 1, unacceptable_count: 0, unknown_count: 1,
    requires_managed_gap_disclosure: true, disclosure_lines: RESULT.disclosure_lines,
    input_fingerprint: 'p17-fp', reasons: [],
  },
};

function flowWith(overrides = {}){
  return {
    cns_continuous_service: {...PROJECTION, ...overrides},
    steps: {'5': true},
  };
}

// ---------------------------------------------------------------------------
// 1. 模型转印（不新增业务数值）
// ---------------------------------------------------------------------------

test('continuousServiceModel transcribes the backend projection verbatim', () => {
  const model = continuousServiceModel(flowWith());
  assert.equal(model.status, 'acceptable_with_managed_gap');
  assert.equal(model.result.input_fingerprint, 'p17-fp');
  assert.equal(model.scenario.intruder_scope, 'other_uav_only');
  assert.equal(model.fc30.not_a_regulatory_threshold, true);
  //: 缺失投影时绝不猜：返回 not_calculated。
  assert.equal(continuousServiceModel({}).status, 'not_calculated');
});

test('longest gap rows expose length, duration and threshold per subsystem', () => {
  const rows = longestGapRows(continuousServiceModel(flowWith()));
  const bySubsystem = Object.fromEntries(rows.map(row => [row.subsystem, row]));
  assert.equal(bySubsystem.C.lengthM, 60);
  assert.equal(bySubsystem.C.durationS, 3);
  assert.equal(bySubsystem.C.limitS, 3);
  assert.equal(bySubsystem.C.kindText, '服务中断（全失联）');
  assert.equal(bySubsystem.N.kindText, '导航降级（RTK → GNSS 回退）');
  assert.equal(bySubsystem.S.lengthM, 2000);
});

test('continuous event rows keep the four event kinds separate', () => {
  const rows = continuousEventRows(continuousServiceModel(flowWith()));
  const kinds = rows.map(row => row.kind);
  assert.ok(kinds.includes('service_outage'));
  assert.ok(kinds.includes('surveillance_detection_gap'));
  assert.equal(rows.find(row => row.kind === 'service_outage').exceedsLimit, false);
  assert.equal(rows.find(row => row.kind === 'surveillance_detection_gap').exceedsLimit, true);
});

test('protection corridor rows carry the formula and the actual parameter values', () => {
  const rows = protectionCorridorRows(continuousServiceModel(flowWith()));
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.formula, 'D_protection = D_safety + V_relative * T_chain + D_uncertainty');
  assert.equal(row.dProtectionM, 400);
  assert.equal(row.vRelativeMps, 40);
  assert.equal(row.relativeSpeedBasis, 'conservative');
  assert.equal(row.tChainS, 10);
  assert.equal(row.outerHalfWidthM, 900);
  assert.equal(row.acceptance.t_margin_s, 115);
  assert.equal(row.detection.first_detection_distance_m, 5000);
});

test('parameter rows expose per-parameter authority and external reference', () => {
  const rows = parameterRows(continuousServiceModel(flowWith()));
  const byField = Object.fromEntries(rows.map(row => [row.field, row]));
  assert.equal(byField.c_full_outage_max_s.authorityText, '内置工程基线');
  assert.match(byField.c_full_outage_max_s.source, /FC30 failsafe/);
  assert.equal(byField.navigation_degradation_time_s.sourceTypeText,
    '外部研究参考（非法规、非厂家事实）');
  assert.match(byField.navigation_degradation_time_s.externalReference, /9–13 s/);
  assert.equal(byField.rtk_availability.authorityText, '航路服务证据推导');
});

test('managed gap rows keep service, position, length, duration, threshold, mitigation and basis', () => {
  const rows = managedGapRows(continuousServiceModel(flowWith()));
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.service, 'C:communication');
  assert.equal(row.startOffsetM, 120.5);
  assert.equal(row.endOffsetM, 180.5);
  assert.equal(row.lengthM, 60);
  assert.equal(row.durationS, 3);
  assert.equal(row.limitS, 3);
  assert.match(row.mitigation, /复用既有站址/);
  assert.match(row.basis, /c_full_outage_max_s/);
});

test('step6 gate model transcribes the backend verdict', () => {
  const gate = step6GateModel(flowWith());
  assert.equal(gate.confirmationAllowed, true);
  assert.deepEqual(gate.allowedStatuses, STEP6_ALLOWED_ACCEPTABILITY);
  assert.equal(gate.requiresDisclosure, true);
  assert.equal(gate.managedGapCount, 1);
});

// ---------------------------------------------------------------------------
// 2. 渲染文案
// ---------------------------------------------------------------------------

test('renderContinuousServicePanel shows the verdict, corridor and managed gap disclosure', () => {
  const html = renderContinuousServicePanel(flowWith(), [['cns-res-continuous', '连续服务可接受性']]);
  assert.match(html, /acceptable_with_managed_gap/);
  assert.match(html, /coverage gap ≠ 自动 planning failure/);
  assert.match(html, /D_protection = D_safety \+ V_relative × T_chain \+ D_uncertainty/);
  assert.match(html, /T_margin/);
  assert.match(html, /首次探测距离/);
  assert.match(html, /最长连续缺口/);
  assert.match(html, /预计持续时间/);
  assert.match(html, /阈值/);
  assert.match(html, /managed gap 强制披露/);
  assert.match(html, /绝不是全覆盖/);
  assert.match(html, /复用既有站址/);
  assert.match(html, /不是法规阈值/);
  assert.match(html, /FC30 设备 failsafe 触发事实/);
  assert.match(html, /外部研究参考/);
  assert.match(html, /不可判定/);
  assert.match(html, /data-continuous-parameter="c_full_outage_max_s"/);
  assert.match(html, /data-protection-corridor="R0005"/);
  assert.match(html, /id="evaluateContinuousService"/);
});

test('renderContinuousServicePanel never calls a managed gap full coverage', () => {
  const html = renderContinuousServicePanel(flowWith(), []);
  //: "全覆盖"只允许以否定式出现（"绝不是全覆盖" / "不得表述为…全覆盖"）；
  //: 任何肯定式的"全覆盖"都是把 managed gap 说成没有缺口，必须判失败。
  const matches = [...html.matchAll(/全覆盖/g)];
  assert.ok(matches.length > 0);
  for (const match of matches) {
    const prefix = html.slice(Math.max(0, match.index - 32), match.index)
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
    assert.match(prefix, /(不是|不得表述为|并非)\s*[「"']?$/,
      `"全覆盖"出现了肯定式表述：…${prefix}全覆盖`);
  }
});

test('renderContinuousServicePanel marks unknown and unacceptable as blocking', () => {
  const unknown = renderContinuousServicePanel(
    flowWith({
      result: {...RESULT, status: 'unknown'},
      step6_gate: {...PROJECTION.step6_gate, status: 'unknown', confirmation_allowed: false,
        blocking_reason: 'P17 连续服务可接受性为 unknown（证据缺失）——必须 fail-closed'},
    }), []);
  assert.match(unknown, /不可判定/);
  assert.match(unknown, /阻止进入评审/);
  assert.match(unknown, /fail-closed/);

  const unacceptable = renderContinuousServicePanel(
    flowWith({
      result: {...RESULT, status: 'unacceptable'},
      step6_gate: {...PROJECTION.step6_gate, status: 'unacceptable', confirmation_allowed: false},
    }), []);
  assert.match(unacceptable, /不可接受/);
  assert.match(unacceptable, /阻止进入评审/);
});

test('acceptability text never labels unknown as passed', () => {
  assert.notEqual(ACCEPTABILITY_TEXT.unknown.label, '完全满足');
  assert.match(ACCEPTABILITY_TEXT.unknown.note, /fail-closed/);
  assert.match(ACCEPTABILITY_TEXT.acceptable_with_managed_gap.note, /不是"全覆盖"/);
});

// ---------------------------------------------------------------------------
// 3. Step05 集成 + 绑定契约
// ---------------------------------------------------------------------------

test('step05 result tab carries the continuous service segment in the canonical chain', () => {
  const ids = CNS_CANONICAL_CHAIN.map(item => item[0]);
  assert.ok(ids.includes('cns-res-continuous'));
  assert.equal(ids[ids.length - 1], 'cns-res-continuous');
  const html = renderStep05({flow: flowWith()});
  assert.match(html, /cns-res-continuous/);
  assert.match(html, /连续服务可接受性/);
  assert.match(html, /evaluateContinuousService/);
});

test('bindContinuousServicePanel only uses the contracted endpoints', () => {
  const calls = [];
  const handlers = new Map();
  const elements = new Set([
    'evaluateContinuousService', 'saveContinuousPolicy', 'saveOperationScenario',
    'continuousOutageLimit', 'continuousDegradationLimit', 'continuousPolicySource',
    'continuousPolicyConfirmed', 'scenarioIntruderSpeed', 'scenarioNominalClosing',
    'scenarioConservativeClosing',
  ]);
  const controller = {
    $: id => (elements.has(id) ? {value: '', checked: false} : null),
    actionButton: (id, handler) => { handlers.set(id, handler); },
    resourceMutationAndRefresh: async (path, payload) => { calls.push([path, payload]); return {}; },
    resourceAction: async (path, payload) => { calls.push([path, payload]); return {}; },
  };
  bindContinuousServicePanel(controller);
  assert.deepEqual([...handlers.keys()].sort(),
    ['evaluateContinuousService', 'saveContinuousPolicy', 'saveOperationScenario']);
  return (async () => {
    await handlers.get('evaluateContinuousService')();
    assert.deepEqual(calls[0], [CONTINUOUS_SERVICE_EVALUATE_ENDPOINT, {}]);
    await handlers.get('saveContinuousPolicy')();
    assert.equal(calls[1][0], CONTINUOUS_SERVICE_POLICY_ENDPOINT);
    await handlers.get('saveOperationScenario')();
    assert.equal(calls[2][0], OPERATION_SCENARIO_ENDPOINT);
    assert.equal(calls[2][1].cns_operation_scenario.intruder_scope, 'other_uav_only');
    assert.equal(calls[2][1].cns_operation_scenario.cruise_altitude, 'ALT-100');
  })();
});

test('bindContinuousServicePanel binds nothing when the panel is not rendered', () => {
  const handlers = [];
  bindContinuousServicePanel({
    $: () => null,
    actionButton: id => handlers.push(id),
    resourceAction: async () => ({}),
  });
  assert.deepEqual(handlers, []);
});
