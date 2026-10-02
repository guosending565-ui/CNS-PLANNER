/**
 * Round 2.5 → Round 2.6 前端回归：Step5「连续服务可接受性」面板 + Step6 门禁投影。
 *
 * 本文件保留 Round 2.5 的原名，但断言已按 **Round 2.6 冻结契约**更新：
 *  * 面板标题不再带 `（P17）` 代号，内部算法标识仍是 `continuous_service_acceptability_v1`；
 *  * 通信阈值**必须分成两件事**：FC30 设备 failsafe 事实（不是规划阈值）与
 *    本项目规划阈值（用户显式登记；未登记 ⇒ evidence_required / unknown，fail-closed）；
 *  * 冗余退化阈值是**独立阈值**，绝不与完全中断合并；
 *  * 保护走廊逐项显示四个分量（D_separation / D_maneuver / D_uncertainty），
 *    D_maneuver=50 m 的身份是 engineering_baseline（不是法规值）；
 *  * 监视威胁分层：合作（RID 主要威胁）/ 非合作（Radar 补充威胁）分开显示；
 *  * Radar 求解不可行 ⇒ 能力限制（limitation，黄色 / 橙色），**不是 error**；
 *  * baseline vs post_plan 两层结论，post_plan 缺失时如实写「尚未计算」。
 *
 * 细节契约（阈值不合并 / fail-closed / 四分量 / 分层 / 比较）由
 * `round26_p17_post_plan_ui.test.mjs` 逐条覆盖；本文件覆盖模型转印 + 渲染 + 绑定。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ACCEPTABILITY_TEXT, CONTINUOUS_SERVICE_EVALUATE_ENDPOINT,
  CONTINUOUS_SERVICE_POLICY_ENDPOINT, OPERATION_SCENARIO_ENDPOINT, STEP6_ALLOWED_ACCEPTABILITY,
  STEP6_ALLOWED_ACCEPTABILITY as ALLOWED, THREAT_LAYER_STATUS_TEXT,
  bindContinuousServicePanel, continuousEventRows, continuousServiceModel, limitationRows,
  longestGapRows, managedGapRows, parameterRows, postPlanComparisonRows, postPlanProjectionModel,
  protectionCorridorRows, renderContinuousServicePanel, step6GateAllows, step6GateModel,
  threatLayerRows,
} from '../cns_planner/web/js/workflow/continuous_service.js';
import {
  CNS_CANONICAL_CHAIN, render as renderStep05,
} from '../cns_planner/web/js/workflow/step05_cns.js';

// ---------------------------------------------------------------------------
// fixture：与后端 `/api/workflow` 的 `cns_continuous_service` 投影同形（Round 2.6）
// ---------------------------------------------------------------------------

const PARAMETERS = {
  semantics: 'continuous_service_parameters_with_per_parameter_authority',
  limits_resolution_order: 'cns_continuous_service_policy → planning_evidence 显式记录 → 内置工程基线',
  parameters: {
    //: Round 2.6：完全中断阈值**没有内置值**，也没有显式记录 ⇒ evidence_required。
    c_full_outage_max_s: {
      field: 'c_full_outage_max_s', label: '最大允许完全通信中断时间',
      value: null, unit: 's', authority: 'evidence_required',
      source_type: 'unknown', participating: false,
      source: 'Round 2.6（用户裁定）：**不再提供内置数值**。FC30 的遥控信号丢失超过 3 s 触发'
        + ' Failsafe RTH 只是**设备 failsafe 事实**，不得自动成为本项目的规划阈值。',
      statement: '最大允许完全通信中断时间必须由用户显式登记（engineering_assumption）；'
        + '未登记时保持 evidence_required / unknown，绝不采用 3 s。',
      report_disclosure: '本阈值必须由用户确认并登记为工程规划假设；设备 failsafe 门限（3 s）'
        + '不等于本项目的规划阈值。未登记时通信判定 fail-closed。',
      reason: '尚无任何依据（无显式记录、也无内置工程基线）：必须由用户 / 工程依据显式提供，'
        + '系统保持 evidence_required（fail-closed），绝不用 0 或其它默认值代替',
    },
    c_redundancy_degradation_max_s: {
      field: 'c_redundancy_degradation_max_s', label: '冗余退化最大允许时间',
      value: 10, unit: 's', authority: 'builtin_engineering_assumption',
      source_type: 'internal_baseline',
      source: '内置工程基线：冗余退化（仍有链路、独立 provider 不足）与设备 failsafe 无关。',
      statement: '冗余退化最大允许时间＝10 s（工程基线，与完全中断阈值是两个独立阈值）。',
      reason: '未提供显式工程依据：采用内置工程基线（默认值）',
    },
    D_separation_m: {
      field: 'D_separation_m', label: '分隔距离（D_separation）',
      value: 50, unit: 'm', authority: 'explicit_evidence',
      source_type: 'engineering_assumption',
      source: '本项目显式登记的分隔距离工程依据。',
      statement: '分隔距离由本项目显式登记。',
      reason: '采用显式登记的工程依据',
    },
    D_maneuver_m: {
      field: 'D_maneuver_m', label: '机动附加距离（D_maneuver，工程基线 50 m）',
      value: 50, unit: 'm', authority: 'builtin_engineering_assumption',
      source_type: 'internal_baseline',
      source: 'Round 2.6 工程基线（engineering_baseline）：机动附加距离固定取 50 m。'
        + '它不是法规值，也不是 FC30 的普遍制动距离事实。',
      statement: 'D_maneuver＝50 m（engineering_baseline，可被显式工程依据替换）。',
      reason: '未提供显式工程依据：采用内置工程基线（默认值）',
    },
    D_uncertainty_m: {
      field: 'D_uncertainty_m', label: '不确定度距离（D_uncertainty）',
      value: null, unit: 'm', authority: 'evidence_required',
      source_type: 'unknown', participating: false,
      statement: 'D_uncertainty 未提供显式工程依据时保持 evidence_required（不得静默取 0）。',
      reason: '尚无任何依据：必须由用户 / 工程依据显式提供',
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
    //: Round 2.5 旧名：后端只作**兼容读取**（新名没有显式记录时回退），并如实标注来源字段名。
    D_safety_m: {
      field: 'D_safety_m', label: '分隔距离（Round 2.5 旧名，兼容读取）',
      value: 50, unit: 'm', authority: 'explicit_evidence', source_type: 'engineering_assumption',
      legacy_alias: true, statement: 'D_safety 是 Round 2.5 的旧名；Round 2.6 的正式参数是 D_separation。',
      reason: '读自 Round 2.5 旧字段名',
    },
  },
};

const THREAT_LAYERS = {
  cooperative: {
    layer: 'cooperative', label: '合作无人机 / RID 合作监视（主要威胁）',
    service_keys: ['S:rid_cooperative'], subsystems: ['nominal'], status: 'nominal',
    t_margin_s: 115, first_detection_distance_m: 5000, limitations: [], reasons: [],
  },
  noncooperative: {
    layer: 'noncooperative', label: '非合作无人机 / Radar 非合作监视（补充威胁）',
    service_keys: ['S:radar_noncooperative'], subsystems: ['limitation'], status: 'limitation',
    t_margin_s: null, first_detection_distance_m: null, limitations: [], reasons: [],
  },
};

const RADAR_LIMITATION = {
  limitation_id: 'noncooperative_surveillance_limitation',
  layer: 'noncooperative',
  capability: 'Radar 非合作监视（补充威胁分层）',
  status: 'limitation',
  blocking_primary_threat: false,
  semantics: 'supplementary_capability_limitation_does_not_change_primary_threat_verdict',
  source_status: 'infeasible',
  solver_status: 'infeasible',
  disclosure: '当前方案对合作无人机的监视链满足当前规划要求；非合作无人机补充监视能力因 Radar '
    + '布局不可行尚未闭合，属于当前方案能力限制。',
  must_disclose_in_report: true,
  no_relaxation_applied: '本轮**没有**为了得到方案而扩大覆盖半径、改动 90° 面板或使用假塔；'
    + '求解不可行是真实工程结论。',
  route_id: 'R0005',
};

const EVENT_OUTAGE = {
  event_id: 'R0005:C:spatial-deficit:1:service_outage', kind: 'service_outage',
  service: 'C:communication', subsystem: 'C', route_id: 'R0005',
  start_offset_m: 120.5, end_offset_m: 180.5, position_basis: 'p15_segment_bounds',
  length_m: 60, duration_s: 3, duration_formula: 'along_route_gap_m / current_route_speed_mps',
  route_speed_mps: 20, limit_s: 3, exceeds_limit: false,
  mitigation: '复用既有站址缩小该段',
  basis: 'threshold=c_full_outage_max_s（authority=evidence_required）',
  semantics: 'conservative_longitudinal_projection_of_p15_confirmed_deficit',
};

const CORRIDOR = {
  semantics: 'horizontal_route_protection_corridor_not_route_centerline_only',
  route_path: [[122.26, 29.86], [122.18, 29.82]], route_length_m: 9525.66,
  cns_requirement_corridor_half_width_m: 500,
  D_separation_m: 50, D_separation_authority: 'explicit_evidence',
  D_maneuver_m: 50, D_maneuver_authority: 'builtin_engineering_assumption',
  D_maneuver_semantics: 'engineering_baseline_interface_not_regulatory_value',
  D_uncertainty_m: 40, D_uncertainty_authority: 'explicit_evidence',
  D_safety_m: 50,
  V_relative_mps: 40, relative_speed_basis: 'conservative', T_chain_s: 10,
  T_chain_components_s: {
    detect_track: 3, sensor_to_platform: 1, platform_processing: 2,
    platform_to_aircraft: 1, aircraft_response_manoeuvre: 3,
  },
  D_protection_m: 540, outer_half_width_m: 1040,
  formula: 'D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty',
  status: 'evaluated',
};

const RESULT = {
  status: 'acceptable_with_managed_gap',
  plan_stage: 'baseline',
  algorithm_id: 'continuous_service_acceptability_v1',
  algorithm_version: '2.0',
  input_fingerprint: 'p17-fp',
  route_speed_mps: 20,
  speed_basis: {
    route_speed_mps: 20, route_speed_source: 'selected_aircraft_profile.cruise_speed_mps',
    fc30_route_speed_mps: 15,
  },
  service_acceptability_limits: {
    C: {service_outage: null, redundancy_degradation: 10},
    S: {service_outage: 3, redundancy_degradation: 10},
  },
  route_count: 1,
  routes: [{
    route_id: 'R0005', status: 'acceptable_with_managed_gap', route_length_m: 9525.66,
    route_speed_mps: 20, plan_stage: 'baseline',
    corridor: CORRIDOR,
    threat_layers: THREAT_LAYERS,
    primary_threat_layer: 'cooperative',
    primary_threat_status: 'satisfied',
    supplementary_threat_layer: 'noncooperative',
    supplementary_threat_status: 'limitation',
    supplementary_threat_is_limitation: true,
    threat_layer_note: '合作无人机（RID）是主要威胁、非合作无人机（Radar）是补充威胁；'
      + '两者分开判定，补充威胁的能力限制不改变主要威胁的结论。',
    limitations: [RADAR_LIMITATION],
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
        events: [EVENT_OUTAGE], event_count: 1,
        limits_s: {service_outage: null, redundancy_degradation: 10},
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
        subsystem: 'S', service: 'S:rid_cooperative', status: 'satisfied',
        events: [], event_count: 0, longest_event: null, managed_gaps: [],
      },
    ],
    events_by_kind: {
      service_outage: [EVENT_OUTAGE],
      redundancy_degradation: [],
      navigation_degradation: [],
      surveillance_detection_gap: [],
    },
    managed_gaps: [EVENT_OUTAGE],
    reasons: [],
    reason_codes: ['no_outage_threshold_evidence'],
  }],
  parameters: PARAMETERS.parameters,
  limitations: [RADAR_LIMITATION],
  primary_threat_status: 'satisfied',
  supplementary_threat_status: 'limitation',
  threat_layer_semantics: 'cooperative_rid_primary_and_noncooperative_radar_supplementary_'
    + 'evaluated_separately_never_merged',
  baseline_status: 'acceptable_with_managed_gap',
  post_plan_status: 'fully_satisfied',
  baseline: null,
  post_plan_projection: {
    available: true, status: 'fully_satisfied', route_count: 1,
    applied_action_ids: ['candidate_site:S1:C1'], persisted_as_upstream: false,
    projection_semantics: 'hypothetical_post_plan_state_never_written_into_existing_cns',
    comparison: {
      improved_service_count: 1, remaining_gap_count: 0,
      improved_services: [{
        route_id: 'R0005', subsystem: 'C', service: 'C:communication',
        status: 'satisfied', kind: null, length_m: null, duration_s: null, limit_s: null,
        exceeds_limit: false, improvement_kind: 'confirmed_gap_resolved',
        declared_improvement_m: null, baseline_length_m: 60, baseline_duration_s: 3,
        reduction_m: 60, semantics: 'gap_event_absent_in_post_plan_state',
      }],
      remaining_gaps: [],
      projection: {
        baseline_fingerprint: 'base-fp', post_plan_fingerprint: 'post-fp',
        applied_action_ids: ['candidate_site:S1:C1'],
        projection_semantics: 'hypothetical_post_plan_state_never_written_into_existing_cns',
        persisted_as_upstream: false,
      },
    },
  },
  reasons: [],
  reason_codes: ['no_outage_threshold_evidence'],
  managed_gap_count: 1, unacceptable_count: 0, unknown_count: 0,
  disclosure_lines: [
    '[managed gap] service=C:communication 类型=服务中断（全失联） 位置=航路 R0005 起算 120.5–180.5 m'
      + ' 连续长度=60.0 m 预计持续时间=3.0 s 阈值=—',
    '  缓解措施（mitigation）：复用既有站址缩小该段',
    '  依据/假设（basis）：threshold=c_full_outage_max_s（authority=evidence_required）',
    '  披露语义：本段并非全覆盖；缺口真实存在，仅因连续时长在工程阈值内被接受为「有管理的缺口」（managed gap）。',
    '[能力限制] Radar 非合作监视（补充威胁分层）',
    '  当前方案对合作无人机的监视链满足当前规划要求；非合作无人机补充监视能力因 Radar 布局不可行尚未闭合，'
      + '属于当前方案能力限制。',
    '  披露语义：本限制**不改变**主要威胁（合作无人机 / RID）的判定，'
      + '但报告与方案评审必须同时显示；不得表述为「监视已完全满足」。',
  ],
  not_evaluated: {common_cause: 'not_evaluated', runtime_outage: 'not_evaluated'},
};

const PROJECTION = {
  result: RESULT,
  parameters: PARAMETERS,
  policy: {
    status: 'configured',
    service_acceptability_limits: {C: {service_outage: null, redundancy_degradation: 10}},
    source: 'pytest fixture', confirmed: true,
  },
  operation_scenario: {
    single_ownship: true, intruder_scope: 'other_uav_only', cruise_altitude: 'ALT-100',
    design_intruder_speed_mps: 20, nominal_closing_speed_mps: 35,
    conservative_closing_speed_mps: 40,
    intruder_speed_source_type: 'engineering_assumption',
    intruder_speed_source: '工程假设：设计入侵者速度 20 m/s',
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
    device_failsafe_fact: {
      parameter: 'rc_loss_failsafe_trigger_s', value_s: 3, authority: 'confirmed_source_fact',
      source_type: 'confirmed_source_fact',
      semantics: 'device_failsafe_trigger_fact_not_regulatory_threshold',
      kind: 'device_failsafe_fact', is_planning_threshold: false,
      statement: 'FC30 设备事实：在 Failsafe RTH 已配置的前提下，遥控（RC）信号丢失超过 3 s '
        + '触发自动返航。它不是法规阈值，也不是本项目的规划阈值。',
    },
    project_planning_threshold: {
      parameter: 'c_full_outage_max_s', value_s: null, authority: 'evidence_required',
      source_type: 'unknown', kind: 'project_planning_threshold', is_planning_threshold: true,
      must_be_engineering_assumption: true, evidence_required: true,
      statement: '本项目的「最大允许完全通信中断时间」由用户显式登记，身份必须是**工程规划假设**。'
        + '未登记时 P17 的通信判定保持 evidence_required / unknown（fail-closed），'
        + '绝不自动采用设备 failsafe 的 3 s。',
      source: null,
      reason: '尚无任何依据：必须由用户 / 工程依据显式提供',
    },
    redundancy_degradation_threshold: {
      parameter: 'c_redundancy_degradation_max_s', value_s: 10,
      authority: 'builtin_engineering_assumption', source_type: 'internal_baseline',
      kind: 'project_planning_threshold', is_planning_threshold: true,
      separate_from_full_outage: true,
      statement: '冗余退化阈值与完全中断阈值是**两个独立阈值**，绝不合并。',
    },
    thresholds_are_separate: true,
    threshold_merge_forbidden: true,
    disclosure: 'FC30 的 3 s 是**设备 failsafe 触发门限**（遥控信号丢失超过 3 s 触发 RTH），'
      + '不是法规阈值，也**不自动**作为本项目的规划阈值。',
  },
  step6_gate: {
    status: 'acceptable_with_managed_gap', confirmation_allowed: true,
    allowed_statuses: ['fully_satisfied', 'acceptable_with_managed_gap'],
    projected_status: 'fully_satisfied', variant_id: 'PV-0001', variant_specific: true,
    limitations: [RADAR_LIMITATION],
    engineered_assumptions: [{
      field: 'c_full_outage_max_s', authority: 'evidence_required',
      statement: '未登记时通信判定保持 evidence_required / unknown。',
    }],
    managed_gap_count: 1, unacceptable_count: 0, unknown_count: 0,
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
  assert.equal(model.result.algorithm_id, 'continuous_service_acceptability_v1',
    '内部算法标识保持不变（只有显示名不再带 P17）');
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
  assert.equal(bySubsystem.S.kindText, '无连续缺口');
});

test('continuous event rows keep the four event kinds separate', () => {
  const rows = continuousEventRows(continuousServiceModel(flowWith()));
  const kinds = rows.map(row => row.kind);
  assert.ok(kinds.includes('service_outage'));
  assert.equal(rows.find(row => row.kind === 'service_outage').exceedsLimit, false);
  assert.equal(kinds.includes('surveillance_detection_gap'), false,
    '本 fixture 没有监视探测缺口段（分层结论在 threat_layers 里）');
});

test('protection corridor rows carry the four components, formula and acceptance', () => {
  const rows = protectionCorridorRows(continuousServiceModel(flowWith()));
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.formula,
    'D_protection = D_separation + V_relative * T_chain + D_maneuver + D_uncertainty');
  assert.equal(row.dSeparationM, 50);
  assert.equal(row.dManeuverM, 50);
  assert.equal(row.dManeuverAuthority, 'builtin_engineering_assumption');
  assert.equal(row.dManeuverSemantics, 'engineering_baseline_interface_not_regulatory_value');
  assert.equal(row.dUncertaintyM, 40);
  assert.equal(row.dProtectionM, 540);
  assert.equal(row.vRelativeMps, 40);
  assert.equal(row.relativeSpeedBasis, 'conservative');
  assert.equal(row.tChainS, 10);
  assert.equal(row.outerHalfWidthM, 1040);
  assert.equal(row.tMarginS, 115);
  assert.equal(row.firstDetectionDistanceM, 5000);
});

test('protection corridor rows fall back to the legacy D_safety_m name only when needed', () => {
  const legacy = flowWith({
    result: {
      ...RESULT,
      routes: [{
        ...RESULT.routes[0],
        corridor: {...CORRIDOR, D_separation_m: null, D_safety_m: 70},
      }],
    },
  });
  const row = protectionCorridorRows(continuousServiceModel(legacy))[0];
  assert.equal(row.dSeparationM, 70, '旧名只在新名缺失时作为回退');
  assert.equal(row.dSafetyM, 70, 'dSafetyM 字段名保留为兼容镜像');
});

test('parameter rows expose per-parameter authority and external reference', () => {
  const rows = parameterRows(continuousServiceModel(flowWith()));
  const byField = Object.fromEntries(rows.map(row => [row.field, row]));
  assert.equal(byField.c_full_outage_max_s.authorityText, '尚无依据（必须显式登记）');
  assert.equal(byField.c_full_outage_max_s.evidenceRequired, true);
  assert.match(byField.c_full_outage_max_s.source, /设备 failsafe 事实/);
  assert.equal(byField.D_maneuver_m.authorityText, '内置工程基线');
  assert.match(byField.D_maneuver_m.source, /不是法规值/);
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

test('threat layer rows keep the two layers separate and transcribe the backend status', () => {
  const rows = threatLayerRows(continuousServiceModel(flowWith()));
  assert.equal(rows.length, 2, '合作 / 非合作两个分层必须各自成行');
  const byLayer = Object.fromEntries(rows.map(row => [row.layer, row]));
  assert.deepEqual(byLayer.cooperative.serviceKeys, ['S:rid_cooperative']);
  assert.equal(byLayer.cooperative.status, 'satisfied',
    '分层状态词汇：nominal 归一为 satisfied（同一个结论）');
  assert.equal(byLayer.cooperative.rawStatus, 'nominal', '后端原值仍然完整保留');
  assert.equal(byLayer.cooperative.statusText, '满足');
  assert.equal(byLayer.cooperative.tMarginS, 115);
  assert.equal(byLayer.cooperative.firstDetectionDistanceM, 5000);
  assert.equal(byLayer.cooperative.isPrimary, true);
  assert.deepEqual(byLayer.noncooperative.serviceKeys, ['S:radar_noncooperative']);
  assert.equal(byLayer.noncooperative.status, 'limitation');
  assert.equal(byLayer.noncooperative.statusText, '能力限制');
  assert.equal(byLayer.noncooperative.isLimitation, true);
  assert.equal(byLayer.noncooperative.isSupplementary, true);
  assert.match(byLayer.noncooperative.statusNote, /不是系统错误/);
});

test('limitation rows transcribe the disclosure verbatim and never mark it as a system error', () => {
  const rows = limitationRows(continuousServiceModel(flowWith()));
  assert.equal(rows.length, 1, '顶层与逐 route 的同一 limitation 必须去重');
  const row = rows[0];
  assert.equal(row.limitationId, 'noncooperative_surveillance_limitation');
  assert.equal(row.status, 'limitation');
  assert.equal(row.blockingPrimaryThreat, false);
  assert.equal(row.solverStatus, 'infeasible');
  assert.equal(row.mustDiscloseInReport, true);
  assert.match(row.disclosure, /属于当前方案能力限制/);
  assert.equal(row.routeId, 'R0005');
});

test('post plan projection transcribes the comparison and stays unavailable when null', () => {
  const model = continuousServiceModel(flowWith());
  const projection = postPlanProjectionModel(model);
  assert.equal(projection.available, true);
  assert.equal(projection.baselineStatus, 'acceptable_with_managed_gap');
  assert.equal(projection.postPlanStatus, 'fully_satisfied');
  assert.equal(projection.improvedServiceCount, 1);
  assert.equal(projection.remainingGapCount, 0);
  assert.deepEqual(projection.appliedActionIds, ['candidate_site:S1:C1']);
  assert.equal(projection.persistedAsUpstream, false);
  const rows = postPlanComparisonRows(model);
  assert.equal(rows.improved.length, 1);
  assert.equal(rows.improved[0].improvementKind, 'confirmed_gap_resolved');
  assert.equal(rows.improved[0].baselineLengthM, 60);
  assert.equal(rows.improved[0].baselineDurationS, 3);
  assert.equal(rows.remaining.length, 0);

  const missing = postPlanProjectionModel(continuousServiceModel(flowWith({
    result: {...RESULT, post_plan_projection: null, post_plan_status: null},
  })));
  assert.equal(missing.available, false);
  assert.equal(missing.postPlanStatus, null);
  assert.match(missing.disclosure, /尚未计算 post-plan 投影/);
});

test('step6 gate model transcribes the verdict and the round 2.6 fields', () => {
  const gate = step6GateModel(flowWith());
  assert.equal(gate.confirmationAllowed, true);
  assert.deepEqual(gate.allowedStatuses, STEP6_ALLOWED_ACCEPTABILITY);
  assert.equal(gate.requiresDisclosure, true);
  assert.equal(gate.managedGapCount, 1);
  assert.equal(gate.projectedStatus, 'fully_satisfied');
  assert.equal(gate.variantId, 'PV-0001');
  assert.equal(gate.variantSpecific, true);
  assert.equal(gate.limitations.length, 1);
  assert.equal(gate.engineeredAssumptions.length, 1);
  //: 取不到的字段一律留空，绝不伪造。
  const bare = step6GateModel(flowWith({step6_gate: {}}));
  assert.equal(bare.projectedStatus, null);
  assert.equal(bare.variantId, null);
  assert.equal(bare.variantSpecific, null);
  assert.deepEqual(bare.limitations, []);
  assert.deepEqual(bare.engineeredAssumptions, []);
});

test('step6 gate allows exactly the two canonical statuses', () => {
  assert.deepEqual(ALLOWED, ['fully_satisfied', 'acceptable_with_managed_gap']);
  assert.equal(step6GateAllows('fully_satisfied'), true);
  assert.equal(step6GateAllows('acceptable_with_managed_gap'), true);
  assert.equal(step6GateAllows('unacceptable'), false);
  assert.equal(step6GateAllows('unknown'), false);
  assert.equal(step6GateAllows('limitation'), false);
  assert.equal(step6GateAllows(null), false);
});

// ---------------------------------------------------------------------------
// 2. 渲染文案
// ---------------------------------------------------------------------------

test('renderContinuousServicePanel shows the verdict, corridor and managed gap disclosure', () => {
  const html = renderContinuousServicePanel(flowWith(), [['cns-res-continuous', '连续服务可接受性']]);
  assert.match(html, /acceptable_with_managed_gap/);
  assert.match(html, /coverage gap ≠ 自动 planning failure/);
  assert.match(html, /D_protection = D_separation \+ V_relative × T_chain \+ D_maneuver \+ D_uncertainty/);
  assert.match(html, /T_margin/);
  assert.match(html, /首次探测距离/);
  assert.match(html, /最长连续缺口/);
  assert.match(html, /预计持续时间/);
  assert.match(html, /阈值/);
  assert.match(html, /managed gap 强制披露/);
  assert.match(html, /绝不是全覆盖/);
  assert.match(html, /复用既有站址/);
  assert.match(html, /不是法规阈值/);
  assert.match(html, /FC30 设备事实/);
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

test('renderContinuousServicePanel renders limitations as a warning block, never as an error', () => {
  const html = renderContinuousServicePanel(flowWith(), []);
  assert.match(html, /data-capability-limitation="true"/);
  assert.match(html, /能力限制（黄色 \/ 橙色）/);
  assert.match(html, /该限制不改变主要威胁（合作无人机 \/ RID）的判定/);
  assert.match(html, /不得表述为「监视已完全满足」/);
  assert.match(html, /非合作无人机补充监视能力因 Radar 布局不可行尚未闭合/);
  //: 能力限制绝不用 error 样式描述；"系统错误"只允许以**否定式**出现
  //: （"不是系统错误"），任何把 limitation 说成系统错误的肯定式表述都判失败。
  assert.doesNotMatch(html, /class="[^"]*(?:error|danger)[^"]*"[^>]*data-capability-limitation/);
  const errorMentions = [...html.matchAll(/系统错误/g)];
  assert.ok(errorMentions.length > 0, '必须明确写出 limitation 不是系统错误');
  for (const match of errorMentions) {
    const prefix = html.slice(Math.max(0, match.index - 24), match.index)
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
    assert.match(prefix, /(不是|不得表述为|并非)\s*[「"']?$/,
      `"系统错误"出现了肯定式表述：…${prefix}系统错误`);
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

test('threat layer status text keeps limitation yellow/orange and never an error', () => {
  assert.equal(THREAT_LAYER_STATUS_TEXT.limitation.label, '能力限制');
  assert.equal(THREAT_LAYER_STATUS_TEXT.limitation.badge, 'warning');
  assert.match(THREAT_LAYER_STATUS_TEXT.limitation.note, /不是系统错误/);
  assert.equal(THREAT_LAYER_STATUS_TEXT.satisfied.label, '满足');
  assert.equal(THREAT_LAYER_STATUS_TEXT.acceptable_with_managed_gap.label, '有管理的缺口');
  assert.equal(THREAT_LAYER_STATUS_TEXT.unacceptable.label, '不可接受');
  assert.equal(THREAT_LAYER_STATUS_TEXT.unknown.label, '不可判定');
  assert.equal(THREAT_LAYER_STATUS_TEXT.not_applicable.label, '不适用');
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
  //: Round 2.6：标题不再带（P17）代号。
  assert.doesNotMatch(html, /连续服务可接受性（P17）/);
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
    //: 提交 payload 结构不变：service_acceptability_limits.C.{service_outage,redundancy_degradation}
    assert.deepEqual(Object.keys(calls[1][1]).sort(),
      ['confirmed', 'service_acceptability_limits', 'source']);
    await handlers.get('saveOperationScenario')();
    assert.equal(calls[2][0], OPERATION_SCENARIO_ENDPOINT);
    assert.equal(calls[2][1].cns_operation_scenario.intruder_scope, 'other_uav_only');
    assert.equal(calls[2][1].cns_operation_scenario.cruise_altitude, 'ALT-100');
  })();
});

test('bindContinuousServicePanel submits the two thresholds separately', () => {
  const calls = [];
  const handlers = {};
  const values = {
    continuousOutageLimit: '12', continuousDegradationLimit: '8',
    continuousPolicySource: 'round26-user-config',
    //: 按钮节点本身也必须存在，否则 bind 阶段就不会注册 handler。
    saveContinuousPolicy: '', saveOperationScenario: '', evaluateContinuousService: '',
  };
  const controller = {
    $: id => (id in values ? {value: values[id], checked: id === 'continuousPolicyConfirmed'}
      : (id === 'continuousPolicyConfirmed' ? {value: '', checked: true} : null)),
    actionButton: (id, handler) => { handlers[id] = handler; },
    resourceMutationAndRefresh: async (path, payload) => { calls.push([path, payload]); return {}; },
  };
  bindContinuousServicePanel(controller);
  return handlers.saveContinuousPolicy().then(() => {
    const payload = calls[0][1];
    assert.equal(payload.service_acceptability_limits.C.service_outage, 12);
    assert.equal(payload.service_acceptability_limits.C.redundancy_degradation, 8,
      '两个阈值必须分别提交，绝不合并成一个字段');
    assert.equal(payload.confirmed, true);
  });
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
