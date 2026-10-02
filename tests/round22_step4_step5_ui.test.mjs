/**
 * Round 2.2 前端回归：STEP4 服务要求可见性 + STEP5/P16 工程结论文案。
 *
 * 用户裁定：
 *  * STEP4 Adopt 之后必须能**直接看到** Communication / RID 各自的半径、重数与
 *    工程依据，"不要让用户看内部 service_key 才知道发生了什么"；
 *  * P16 面板必须显示候选站址数、selected actions 数、confirmed 改善、residual
 *    deficit 与 unknown evidence 数量；
 *  * failed / infeasible / no_eligible_proposal 必须解释成**工程结论**，
 *    不得把后端 raw 停止原因直接甩给用户。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  corridorSitePlanSummary, stopReasonLabel,
} from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  facilityPlanCards, renderAdoptedRequiredCnsServices, reusesExistingPhysicalSite,
  surfacePolicyLine,
} from '../cns_planner/web/js/workflow/cns_service_evidence.js';

const COMMUNICATION = {
  service_key: 'C:communication', required: true, confirmed: true,
  source: 'engineering_assumption', maturity: 'engineering_planning_baseline',
  geometry: {model: 'hemisphere', omnidirectional: true, horizontal_coverage_deg: 360},
  radius_by_surface: {land: 4000, coastal_uncertain: 4000, sea: 4000},
  redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1},
  not_evaluated: ['link_budget', 'capacity', 'throughput'],
};
const RID = {
  service_key: 'S:rid_cooperative', required: true, confirmed: true,
  source: 'engineering_assumption', maturity: 'engineering_planning_baseline',
  geometry: {model: 'hemisphere', omnidirectional: true, horizontal_coverage_deg: 360},
  radius_by_surface: {land: 2000, coastal_uncertain: 2000, sea: 5000},
  redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1},
  not_evaluated: ['link_budget'],
};

const flowWithServices = {
  required_cns: {
    status: 'passed',
    project_default: {
      communication: {required: true, services: {'C:communication': COMMUNICATION}},
      navigation: {required: false},
      surveillance: {required: true, services: {'S:rid_cooperative': RID}},
    },
  },
};

// ---------------------------------------------------------------------------
// STEP4：服务要求必须对用户可见
// ---------------------------------------------------------------------------

test('step4 shows communication and rid radius, redundancy and engineering basis', () => {
  const html = renderAdoptedRequiredCnsServices(flowWithServices);
  //: 业务中文名可见（Round D 正式服务名），而不是内部 service_key。
  assert.match(html, /<b>通信<\/b>/);
  assert.match(html, /<b>合作监视（RID）<\/b>/);
  assert.match(html, /陆地 4000 m · 海岸不确定 4000 m · 海上 4000 m/);
  assert.match(html, /陆地 2000 m · 海岸不确定 2000 m · 海上 5000 m/);
  assert.match(html, /要求的独立物理站址数：陆地 2 · 海岸不确定 2 · 海上 1/);
  assert.match(html, /全向（水平 360°/);
  assert.match(html, /工程规划基线（非厂家实测规格）/);
  assert.match(html, /绝不把"N 台设备"当成"N 重"/);
  //: 内部 service_key 只能出现在**审计属性**里，不得成为可见文本。
  const visible = html.replace(/data-service-key="[^"]*"/g, '').replace(/<[^>]*>/g, '');
  assert.doesNotMatch(visible, /C:communication/);
  assert.doesNotMatch(visible, /S:rid_cooperative/);
});

test('step4 states explicitly when only the legacy subsystem semantics exist', () => {
  const html = renderAdoptedRequiredCnsServices({required_cns: {project_default: {
    communication: {required: true}, navigation: {required: false}, surveillance: {required: true},
  }}});
  assert.match(html, /尚未声明任何服务级要求/);
  assert.match(html, /下游会按子系统口径规划/);
});

test('surface policy line never invents a value for an undeclared surface', () => {
  assert.equal(surfacePolicyLine(null), '未声明');
  assert.equal(surfacePolicyLine({}), '未声明');
  assert.equal(surfacePolicyLine({land: 4000}), '陆地 4000');
  assert.equal(surfacePolicyLine({land: 4000, sea: null}), '陆地 4000');
});

test('reused existing physical sites are labelled as reuse, not as new builds', () => {
  assert.equal(reusesExistingPhysicalSite('tower_colocation_host'), true);
  assert.equal(reusesExistingPhysicalSite('existing_cns_facility'), true);
  assert.equal(reusesExistingPhysicalSite('candidate_site'), false);
  assert.equal(reusesExistingPhysicalSite(null), false);

  const html = facilityPlanCards({selected_actions: [{
    action_id: 'A1', reuse_class: 'tower_colocation_host', service_key: 'C:communication',
    distinct_site_id: 'site-1', status: 'eligible', required_units: 2, current_units: 1,
    impact: {target_progress: [{before_units: 1, after_units: 2}]},
  }]});
  assert.match(html, /复用既有站址/);
  assert.match(html, /不新增独立站址/);
});

test('co-located actions declare that they do not add distinct-site redundancy', () => {
  const action = {
    action_id: 'A1', reuse_class: 'tower_colocation_host', service_key: 'C:communication',
    distinct_site_id: 'same-site', status: 'eligible', impact: {},
  };
  const other = {
    action_id: 'A2', reuse_class: 'tower_colocation_host', service_key: 'S:rid_cooperative',
    distinct_site_id: 'same-site', status: 'eligible', impact: {},
  };
  const html = facilityPlanCards({selected_actions: [action, other]});
  assert.match(html, /同一物理站址，不增加独立站址重数/);
});

// ---------------------------------------------------------------------------
// STEP5 / P16：工程结论必须可读，且数字完整
// ---------------------------------------------------------------------------

test('p16 stop reasons are translated instead of leaking backend enums', () => {
  assert.equal(stopReasonLabel('all_evaluable_confirmed_objectives_met'),
    '所有可评估的已确认规划目标已满足');
  assert.equal(stopReasonLabel('confirmed_objectives_already_met'),
    '已确认规划目标在基线即已满足，无需新增站址');
  assert.equal(stopReasonLabel('only_unknown_or_missing_evidence'),
    '只存在证据不足的缺口：不自动建站，需先补齐证据');
  assert.equal(stopReasonLabel('no_positive_confirmed_marginal_gain'),
    '没有任何候选动作产生已确认的正边际改善');
  //: 未登记的取值原样返回（绝不编造）。
  assert.equal(stopReasonLabel('some_future_reason'), 'some_future_reason');
  assert.equal(stopReasonLabel(''), '未给出停止原因');
});

test('p16 summary exposes candidate, selected, confirmed, residual and unknown counts', () => {
  const html = corridorSitePlanSummary({
    status: 'no_eligible_proposal', target_voxel_count: 8235,
    candidate_actions: new Array(750).fill({}), candidate_impacts: [
      {evidence_status: 'prefiltered_no_corridor_interaction'},
      {evidence_status: 'prefiltered_no_corridor_interaction'},
      {evidence_status: 'no_confirmed_progress'},
    ],
    selected_actions: [], residual_confirmed_targets: new Array(8235).fill({}),
    residual_unknown_evidence: {
      final_unknown_target_count: 17, selected_action_unknown_target_count: 0,
    },
    unknown_evidence_required: [], confirmed_requirement_unit_volume_gain: 0,
    stop_reason: 'no_positive_confirmed_marginal_gain', after: {routes: []},
  });

  assert.match(html, /候选动作 750/);
  assert.match(html, /已选动作 0/);
  assert.match(html, /几何上不可能触及走廊而预筛跳过 2/);
  assert.match(html, /残余确认缺口目标 8235 \/ 目标总数 8235/);
  assert.match(html, /仍缺证据的缺口目标 17/);
  assert.match(html, /没有任何候选动作产生已确认的正边际改善/);
  //: no_eligible_proposal 必须被解释成工程结论，而不是程序错误。
  assert.match(html, /工程结论/);
  assert.match(html, /不是程序错误/);
  assert.match(html, /不会为了"有方案"而降低覆盖要求/);
  assert.doesNotMatch(html, /no_positive_confirmed_marginal_gain/);
});

test('p16 summary reports selected actions and confirmed improvement verbatim', () => {
  const html = corridorSitePlanSummary({
    status: 'proposal_ready', target_voxel_count: 10, candidate_actions: [{}, {}],
    candidate_impacts: [], selected_actions: [{
      action_id: 'tower-colocation:1', reuse_class: 'tower_colocation_host',
      marginal_confirmed_requirement_unit_volume_gain: 12345.6, score_semantics: 'action_count_proxy',
    }],
    residual_confirmed_targets: [{}, {}],
    residual_unknown_evidence: {final_unknown_target_count: 0, selected_action_unknown_target_count: 3},
    unknown_evidence_required: [], confirmed_requirement_unit_volume_gain: 12345.6,
    stop_reason: 'all_evaluable_confirmed_objectives_met', after: {routes: []},
    iteration_trace: [],
  });
  assert.match(html, /候选动作 2/);
  assert.match(html, /已选动作 1/);
  assert.match(html, /已确认单位体积改善 12345\.6/);
  assert.match(html, /已选动作登记的未知证据 3/);
  assert.match(html, /所有可评估的已确认规划目标已满足/);
  assert.match(html, /tower-colocation:1/);
});

test('p16 summary keeps unknown evidence separate from satisfied conclusions', () => {
  const html = corridorSitePlanSummary({
    status: 'evidence_required', target_voxel_count: 5, candidate_actions: [],
    candidate_impacts: [], selected_actions: [], residual_confirmed_targets: [],
    residual_unknown_evidence: {final_unknown_target_count: 5},
    unknown_evidence_required: [{kind: 'planning_objective_evidence'}],
    confirmed_requirement_unit_volume_gain: 0, stop_reason: 'only_unknown_or_missing_evidence',
    after: {routes: []},
  });
  assert.match(html, /既不算作满足，也不会被静默丢弃/);
  assert.match(html, /待补证据条目 1/);
});

test('p16 summary explains why no gain could be confirmed', () => {
  const html = corridorSitePlanSummary({
    status: 'no_eligible_proposal', target_voxel_count: 8235,
    candidate_actions: [{}, {}], candidate_impacts: [],
    selected_actions: [], residual_confirmed_targets: [],
    residual_unknown_evidence: {final_unknown_target_count: 0},
    candidate_unknown_reason_summary: {
      candidates_with_unknown_targets: 20,
      reason_counts: {
        '该动作覆盖到该目标后其服务证据仍未确认（provider 类型/合格性证据不足）：缺口无法升级为已确认改善': 25397,
        'provider 判定汇总：C:unknown×1362、S:meets_under_model×1641': 3003,
        'P14 服务证据汇总：C:no_service_entry×1362、S:no_service_entry×1641': 3003,
      },
    },
    unknown_evidence_required: [], confirmed_requirement_unit_volume_gain: 0,
    stop_reason: 'no_positive_confirmed_marginal_gain', after: {routes: []},
  });
  assert.match(html, /为什么没有可确认的改善/);
  assert.match(html, /provider 类型\/合格性证据不足/);
  assert.match(html, /25397 个缺口目标/);
  //: Round 2.3：必须能看到"哪一层拿不到证据"，而不是只有一句笼统结论。
  assert.match(html, /provider 判定汇总/);
  assert.match(html, /P14 服务证据汇总/);
  assert.match(html, /受影响候选动作 20 个/);
  assert.match(html, /证据不足绝不算作满足/);
});

test('p16 reason aggregation renders more than six distinct reasons', () => {
  const reason_counts = {};
  for (let index = 1; index <= 10; index += 1) {
    reason_counts[`第 ${index} 类原因`] = index;
  }
  const html = corridorSitePlanSummary({
    status: 'no_eligible_proposal', target_voxel_count: 10,
    candidate_actions: [{}], candidate_impacts: [], selected_actions: [],
    residual_confirmed_targets: [], residual_unknown_evidence: {},
    candidate_unknown_reason_summary: {candidates_with_unknown_targets: 1, reason_counts},
    unknown_evidence_required: [], confirmed_requirement_unit_volume_gain: 0,
    stop_reason: 'no_positive_confirmed_marginal_gain', after: {routes: []},
  });
  assert.match(html, /第 1 类原因/);
  assert.match(html, /第 10 类原因/);
});
