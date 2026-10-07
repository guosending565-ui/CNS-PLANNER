/**
 * Round32-J：Step06 前端消费契约在**瘦身后的 workflow 投影**上仍然成立。
 *
 * 后端 `workflow.snapshot()` 现在只下发有界投影：`cns_plan_review.variants[*].evaluation`
 * 不再带 `before_p15` / `after_p15` / `authoritative_hypothetical` / `actions`，
 * P17 的 `post_plan_projection.radar_surveillance_layout.items` 也被摘要化。
 *
 * 本文件用**与后端投影同形**的 fixture 锁定 Step06 仍然消费得到的字段：
 *
 *   1. 方案审查摘要（variant 数量 / selected variant / 所选动作数 / 确认门禁）；
 *   2. variant 卡（name / source / 动作数 / 门禁）；
 *   3. 完整 comparison matrix 表；
 *   4. 每个 variant 自己的 P17 投影结论；
 *   5. 强制披露（managed gap / limitations 原文 / FC30 阈值）；
 *   6. 确认门禁与 fail-closed 文案；
 *   7. P17=unacceptable 绝不被投影改写成 unknown/acceptable。
 *
 * 运行：`node tests/round32j_step06_projection_contract.test.mjs`
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  mandatoryDisclosureModel, planReviewSummary, render as renderStep6,
  variantContinuousServiceProjection,
} from '../cns_planner/web/js/workflow/step06_review.js';

const CONTRACT = JSON.parse(readFileSync(
  new URL('./fixtures/round32j_workflow_projection_contract.json', import.meta.url), 'utf8'));

// ---- fixture：与后端 slim_plan_review_for_workflow / slim_continuous_service_for_workflow 同形

function variantEvaluation(overrides = {}) {
  return {
    status: 'evaluated',
    detail_available: true,
    detail_omitted: true,
    omitted_detail_fields: CONTRACT.plan_review.variant_evaluation_omitted_fields,
    detail_endpoint: CONTRACT.plan_review.detail_endpoint,
    comparison_matrix: [{
      route_id: 'R1', subsystem: 'C', objective_status: 'met',
      service: {voxel_counts: {satisfied: 10, confirmed_deficit: 0, unknown: 0}},
      redundancy: {voxel_counts: {satisfied: 9, confirmed_deficit: 1, unknown: 0}},
      total_confirmed_deficit_projection_m: 42.5,
      max_continuous_deficit_projection_m: 18.0,
      unknown_voxel_ids: [],
    }],
    confirmation_gate: {
      status: 'ready_for_confirmation', confirmation_allowed: true,
      requires_confirm_without_objectives_acknowledgement: false,
      regressions: [], unknown_regressions: [], unknown_voxel_ids: [],
      confirmed_objective_count: 1, objective_failures: [], objective_unknown: [],
    },
    action_summary: {
      selected_count: 1, reuse_class_counts: {candidate_site: 1},
      explicit_costs_by_unit: {site: 120.0}, cross_unit_total: null,
      cost_semantics: 'explicit_costs_grouped_by_unit_no_cross_unit_total',
    },
    planned_p14_fingerprint: 'p14-fp',
    planned_p15_fingerprint: 'p15-fp',
    continuous_service_projection: {
      evaluated_for_this_variant: true,
      evaluated_basis: 'variant_projection',
      projected_status: 'acceptable_with_managed_gap',
      status: 'acceptable_with_managed_gap',
      baseline_status: 'unacceptable',
      post_plan_status: 'acceptable_with_managed_gap',
      projected_managed_gap_count: 1,
      projected_unacceptable_count: 0,
      projected_unknown_count: 0,
      primary_threat_status: 'passed',
      supplementary_threat_status: 'limitation',
      limitations: [{limitation_id: 'L1', disclosure: 'Radar 非合作监视限制'}],
      disclosure_lines: ['managed gap：服务中断 6.0 s ≤ 阈值 8.0 s（缺口真实存在）'],
      variant_specific_gate: {
        status: 'acceptable_with_managed_gap', confirmation_allowed: true,
        variant_specific: true, blocking_reason: null,
      },
      comparison: {improved_service_count: 1, remaining_gap_count: 0},
      applied_action_ids: ['candidate_site:S1:C1'],
      projection_semantics: 'post_plan_projection_hypothetical_state_never_written_into_existing_cns',
      persisted_as_upstream: false,
      written_into_existing_cns: false,
      note: '本结论是本 variant 假设实施后的投影态评估（hypothetical），不是现网事实。',
    },
    ...overrides,
  };
}

function variant(overrides = {}) {
  return {
    variant_id: 'PV-1',
    name: 'Baseline',
    source: 'baseline',
    selected_action_ids: [],
    source_p16_fingerprint: 'p16-fp',
    baseline_fingerprint: 'baseline-fp',
    notes: '',
    status: 'evaluated',
    evaluation: variantEvaluation(),
    ...overrides,
  };
}

const UNACCEPTABLE_VARIANT = variant({
  variant_id: 'PV-2',
  name: 'P16 Auto Proposal',
  source: 'p16_auto',
  selected_action_ids: ['candidate_site:S1:C1'],
  evaluation: variantEvaluation({
    continuous_service_projection: {
      evaluated_for_this_variant: true,
      evaluated_basis: 'variant_projection',
      projected_status: 'unacceptable',
      status: 'unacceptable',
      baseline_status: 'unacceptable',
      post_plan_status: 'unacceptable',
      projected_managed_gap_count: 0,
      projected_unacceptable_count: 1,
      projected_unknown_count: 0,
      primary_threat_status: 'failed',
      supplementary_threat_status: 'limitation',
      limitations: [],
      disclosure_lines: [],
      variant_specific_gate: {
        status: 'unacceptable', confirmation_allowed: false, variant_specific: true,
        blocking_reason: '本变体实施后的 P17 投影结论为 unacceptable——不可接受',
      },
      comparison: {improved_service_count: 0, remaining_gap_count: 1},
      applied_action_ids: ['candidate_site:S1:C1'],
      projection_semantics: 'post_plan_projection_hypothetical_state_never_written_into_existing_cns',
      persisted_as_upstream: false,
      written_into_existing_cns: false,
      note: '本结论是本 variant 假设实施后的投影态评估（hypothetical），不是现网事实。',
    },
  }),
});

function planReviewProjection() {
  return {
    workflow_projection: CONTRACT.plan_review.workflow_projection,
    status: 'current',
    model_scope: 'human_reviewed_plan_decision',
    automatic_overall_score: null,
    automatic_rank: null,
    review_id: 'REV-1',
    baseline_fingerprint: 'baseline-fp',
    input_fingerprints: {p15: 'p15-fp'},
    selected_variant_id: 'PV-2',
    initialized_from: {source: 'test'},
    variant_count: 2,
    variants_detail_available: true,
    variants_detail_endpoint: CONTRACT.plan_review.detail_endpoint,
    variant_evaluation_detail_omitted_fields:
      CONTRACT.plan_review.variant_evaluation_omitted_fields,
    variants: [variant(), UNACCEPTABLE_VARIANT],
  };
}

const CONTINUOUS_PARAMETERS = {
  schema_version: 1,
  semantics: 'continuous_service_parameters_with_per_parameter_authority',
  limits_resolution_order: 'cns_continuous_service_policy → planning_evidence → 内置工程基线',
  builtin_limits: {},
  parameters: {
    c_full_outage_max_s: {
      field: 'c_full_outage_max_s', label: 'C 服务中断阈值', value: 8.0, unit: 's',
      participating: true, authority: 'engineering_assumption',
      source_type: 'engineering_assumption', source: 'test fixture',
      statement: '测试工程假设', reason: null, report_disclosure: '测试披露',
    },
    D_separation_m: {
      field: 'D_separation_m', label: '保护走廊间隔', value: 120.0, unit: 'm',
      participating: true, authority: 'builtin_engineering_assumption',
      source_type: 'engineering_assumption', source: null, statement: null,
    },
  },
};

function continuousServiceProjection() {
  return {
    workflow_projection: CONTRACT.continuous_service.workflow_projection,
    detail_available: true,
    detail_endpoint: CONTRACT.continuous_service.detail_endpoint,
    result: {
      status: 'acceptable_with_managed_gap',
      baseline_status: 'unacceptable',
      post_plan_status: 'acceptable_with_managed_gap',
      algorithm_id: 'continuous_service_acceptability_v1',
      algorithm_version: '2.2',
      parameters: CONTINUOUS_PARAMETERS,
      managed_gap_count: 1,
      unacceptable_count: 0,
      unknown_count: 0,
      primary_threat_status: 'passed',
      supplementary_threat_status: 'limitation',
      disclosure_lines: ['managed gap：服务中断 6.0 s ≤ 阈值 8.0 s（缺口真实存在）'],
      limitations: [{
        limitation_id: 'L1', capability: 'radar_non_cooperative_surveillance',
        disclosure: 'Radar 非合作监视限制：该限制不改变主要威胁的判定。',
        status: 'limitation', blocking_primary_threat: false, must_disclose_in_report: true,
      }],
      routes: [{
        route_id: 'R1', subsystem: 'C', status: 'acceptable_with_managed_gap',
        longest_event: null, managed_gaps: [], limitations: [], events_by_kind: {},
      }],
      post_plan_projection: {
        available: true, status: 'acceptable_with_managed_gap', route_count: 1,
        applied_action_ids: ['candidate_site:S1:C1'],
        projection_semantics:
          'post_plan_projection_hypothetical_state_never_written_into_existing_cns',
        persisted_as_upstream: false,
        comparison: {
          improved_service_count: 1, remaining_gap_count: 0,
          improved_services: [], remaining_gaps: [], projection: 'p16_selected_actions',
        },
        radar_surveillance_layout: {
          status: 'feasible', count: 2, collection_id: 'RAD-1', schema_version: 1,
          algorithm_id: 'radar_surveillance_layout_v1', algorithm_version: '1.3',
          proposal_only: true, evaluated_at: '2026-01-01T00:00:00Z',
          model_scope: 'radar_surveillance_layout', boundaries: {min_altitude_m: 50},
          items_count: 2, items_detail: 'artifact', detail_omitted: true,
          omitted_detail_fields: CONTRACT.continuous_service.radar_omitted_fields,
          detail_endpoint: CONTRACT.continuous_service.radar_detail_endpoint,
        },
      },
    },
    parameters: CONTINUOUS_PARAMETERS,
    policy: {},
    operation_scenario: {},
    fc30: {
      selected_aircraft_id: 'A1', is_selected: true, disclosure: 'FC30 事实披露',
      device_failsafe_fact: {value_s: 10.0, is_planning_threshold: false, kind: 'device_fact', statement: '失联 > 10 s 触发 RTL'},
      project_planning_threshold: {value_s: 8.0, evidence_required: false, authority: 'engineering_assumption', statement: '本项目规划阈值'},
      redundancy_degradation_threshold: {value_s: 20.0, separate_from_full_outage: true, statement: '冗余退化独立阈值'},
      thresholds_are_separate: true, threshold_merge_forbidden: true,
    },
    step6_gate: {
      status: 'acceptable_with_managed_gap', confirmation_allowed: true,
      allowed_statuses: ['fully_satisfied', 'acceptable_with_managed_gap'],
      variant_specific: false, evaluates: 'authoritative_post_plan_projection',
      projected_status: 'acceptable_with_managed_gap',
      baseline_status: 'unacceptable', post_plan_status: 'acceptable_with_managed_gap',
      managed_gap_count: 1, unacceptable_count: 0, unknown_count: 0,
      limitations: [{limitation_id: 'L1', disclosure: 'Radar 非合作监视限制：该限制不改变主要威胁的判定。'}],
      disclosure_lines: ['managed gap：服务中断 6.0 s ≤ 阈值 8.0 s（缺口真实存在）'],
      requires_managed_gap_disclosure: true, reasons: [],
    },
  };
}

function flowFixture(overrides = {}) {
  return {
    steps: {'6': true},
    workspace: {area_km2: 12.5},
    operational_routes: [{route_id: 'R1'}],
    aircraft: {manufacturer: 'Test', model: 'T1'},
    rules: {status: 'passed'},
    coverage_3d: {status: 'passed', routes: []},
    result_statuses: {cns_plan_review: 'passed'},
    review: {status: 'passed', risks: []},
    cns_plan_review: planReviewProjection(),
    confirmed_cns_plan: {
      status: 'not_confirmed', plan_id: null, application: {},
      current_applicability: 'not_evaluated',
    },
    cns_planning_reports: {records: [], active_report_id: null, status: 'not_calculated'},
    cns_continuous_service: continuousServiceProjection(),
    cns_corridor_site_plan: {status: 'passed', selected_actions: [], candidate_actions: []},
    radar_surveillance_layout: {status: 'feasible'},
    radar_surveillance_layout_readiness: {status: 'ready'},
    route_safety_evidence_v2: {items: []},
    route_safety_evidence_v2_readiness: {},
    planning_evidence: {field_status: {}, active: [], disclosure_lines: []},
    required_cns_recommendation: {status: 'passed', matched_policies: [], field_provenance: {}},
    ...overrides,
  };
}

function renderHtml(flow) {
  return renderStep6({state: {data_health: {status: 'passed'}}, flow, selection: {}});
}

// ---- 1. 契约清单本身与后端投影一致 -----------------------------------------

test('契约清单与后端投影的字段名一致（machine-readable 锁定）', () => {
  const review = planReviewProjection();
  for (const name of CONTRACT.plan_review.review_required_fields) {
    assert.ok(name in review, `review 投影缺少必需字段 ${name}`);
  }
  const projectedVariant = review.variants[0];
  for (const name of CONTRACT.plan_review.variant_required_fields) {
    assert.ok(name in projectedVariant, `variant 投影缺少必需字段 ${name}`);
  }
  for (const name of CONTRACT.plan_review.variant_evaluation_fields) {
    assert.ok(name in projectedVariant.evaluation, `缺少消费字段 ${name}`);
  }
  for (const name of CONTRACT.plan_review.variant_evaluation_omitted_fields) {
    assert.ok(!(name in projectedVariant.evaluation), `审计证据 ${name} 不得出现在投影里`);
  }

  const p17 = continuousServiceProjection();
  assert.equal(p17.workflow_projection, CONTRACT.continuous_service.workflow_projection);
  for (const name of CONTRACT.continuous_service.result_required_fields) {
    assert.ok(name in p17.result, `P17 结果缺少必需字段 ${name}`);
  }
  const post = p17.result[CONTRACT.continuous_service.result_projection_field];
  assert.ok(post && typeof post === 'object');
  for (const name of CONTRACT.continuous_service.post_plan_required_fields) {
    assert.ok(name in post, `post_plan_projection 缺少必需字段 ${name}`);
  }
  for (const name of CONTRACT.continuous_service.radar_summary_fields) {
    assert.ok(name in post.radar_surveillance_layout, `Radar 摘要缺少业务结论字段 ${name}`);
  }
  for (const name of CONTRACT.continuous_service.radar_omitted_fields) {
    assert.ok(!(name in post.radar_surveillance_layout), `Radar 明细 ${name} 不得进入快照`);
  }
});

// ---- 2. 方案审查摘要（variant 数量 / selected / 动作数 / 门禁） --------------

test('方案审查摘要仍能读出版本数量、已选方案、动作数与确认门禁', () => {
  const summary = planReviewSummary(planReviewProjection(),
    {status: 'not_confirmed', application: {}});
  assert.equal(summary.variantCount, 2);
  assert.equal(summary.selectedVariantId, 'PV-2');
  assert.deepEqual(summary.selectedActionIds, ['candidate_site:S1:C1']);
  assert.equal(summary.gate, 'ready_for_confirmation');
  assert.equal(summary.confirmedStatus, 'not_confirmed');
  assert.equal(summary.applyStatus, 'not_applied');
});

// ---- 3. variant 卡（name / source / 动作数 / 门禁） --------------------------

test('投影后 variant 卡仍显示名称、来源标识、动作数与门禁', () => {
  const html = renderHtml(flowFixture());
  assert.match(html, /Baseline/);
  assert.match(html, /P16 Auto Proposal/);
  assert.match(html, /来源标识：p16_auto/);
  assert.match(html, /1 个动作/);
  assert.match(html, /确认门禁：/);
  assert.match(html, /data-variant-id="PV-2"/);
});

// ---- 4. comparison matrix --------------------------------------------------

test('投影后完整 comparison matrix 表仍然渲染', () => {
  const html = renderHtml(flowFixture());
  assert.match(html, /完整客观指标矩阵/);
  assert.match(html, /缺口总长 m/);
  assert.match(html, /42\.5/);
  assert.match(html, /18\.0/);
  // 显式费用（action_summary）仍然可用，且绝不跨单位合计。
  assert.match(html, /120\.0 site/);
  assert.match(html, /显式费用/);
});

// ---- 5. variant 自己的 P17 投影结论 ----------------------------------------

test('每个 variant 仍读到自己的 P17 投影结论（managed gap / unacceptable / unknown）', () => {
  const model = variantContinuousServiceProjection(planReviewProjection().variants[0]);
  assert.equal(model.evaluated, true);
  assert.equal(model.projectedStatus, 'acceptable_with_managed_gap');
  assert.equal(model.passes, true);
  assert.equal(model.managedGapCount, 1);
  assert.equal(model.unacceptableCount, 0);
  assert.equal(model.blockingReason, null);

  const html = renderHtml(flowFixture());
  assert.match(html, /本 variant 实施后的 P17 结论/);
  assert.match(html, /该结论属于本 variant 的投影态，不是现网事实。/);
  assert.match(html, /managed gap 1 · unacceptable 0 · unknown 0/);
});

// ---- 6. 强制披露 -----------------------------------------------------------

test('强制披露仍读出 managed gap、limitations 原文与 FC30 阈值', () => {
  const model = mandatoryDisclosureModel(flowFixture());
  assert.equal(model.managedGapCount, 1);
  assert.deepEqual(model.managedGapDisclosureLines,
    ['managed gap：服务中断 6.0 s ≤ 阈值 8.0 s（缺口真实存在）']);
  assert.equal(model.limitations.length, 1);
  assert.equal(model.limitations[0].limitationId, 'L1');
  assert.equal(model.fc30.thresholdsAreSeparate, true);
  assert.equal(model.fc30.thresholdMergeForbidden, true);
  assert.equal(model.fc30.projectPlanningThreshold.value_s, 8.0);

  const html = renderHtml(flowFixture());
  assert.match(html, /强制披露（managed gap \/ 工程假设 \/ 能力限制 \/ FC30 与阈值 \/ 保护参数）/);
  assert.match(html, /Radar 非合作监视限制/);
  assert.match(html, /能力限制（黄色 \/ 橙色）/);
  assert.match(html, /data-step6-threshold="device_failsafe_fact"/);
});

// ---- 7. P17=unacceptable 的 fail-closed ------------------------------------

test('P17=unacceptable 绝不被投影改写成 unknown 或 acceptable，且门禁保持 fail-closed', () => {
  const model = variantContinuousServiceProjection(planReviewProjection().variants[1]);
  assert.equal(model.evaluated, true);
  assert.equal(model.projectedStatus, 'unacceptable');
  assert.equal(model.effectiveStatus, 'unacceptable');
  assert.equal(model.passes, false, 'unacceptable 绝不允许确认（fail-closed）');
  assert.equal(model.unacceptableCount, 1);
  assert.match(String(model.blockingReason), /unacceptable/);

  const html = renderHtml(flowFixture());
  assert.match(html, /本 variant 实施后的 P17 结论不允许确认/);
  assert.match(html, /只有 fully_satisfied \/ acceptable_with_managed_gap 才允许进入正式评审/);
  // 投影绝不把不可接受表述成"通过"。
  assert.doesNotMatch(html, /本 variant 实施后的 P17 结论：可接受（完全满足）/);
});

test('尚无 P17 投影的 variant 仍保持不可判定（绝不显示为通过）', () => {
  const missing = variantContinuousServiceProjection({
    evaluation: {continuous_service_projection: {evaluated_for_this_variant: false}},
  });
  assert.equal(missing.present, true);
  assert.equal(missing.evaluated, false);
  assert.equal(missing.effectiveStatus, null);
  assert.equal(missing.passes, false, '未评估绝不通过（fail-closed）');
  assert.match(missing.label, /不可判定/);
});

// ---- 8. 确认与应用分段仍然可用 --------------------------------------------

test('确认与应用分段仍渲染门禁、确认按钮与草稿报告预览入口', () => {
  const html = renderHtml(flowFixture());
  assert.match(html, /id="confirmPlan"/);
  assert.match(html, /data-gate="ready_for_confirmation"/);
  assert.match(html, /id="applyPlan"/);
  assert.match(html, /预览草稿报告|草稿/);
});
