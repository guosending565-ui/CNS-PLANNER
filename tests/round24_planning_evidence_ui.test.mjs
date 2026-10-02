/**
 * Round 2.4 前端回归：工程依据 / 规划假设人工入口 + P16 按服务分组。
 *
 * 用户裁定：
 *  * 缺工程证据时必须能在**前端**由普通用户补录，而不是让开发者改 JSON；
 *  * 录入必须显式选择来源类型，事实与假设严格分开，假设必须写披露文本；
 *  * 提交只走正式接口 `POST /api/planning-evidence`，绝不写设备目录；
 *  * P16 面板必须按 canonical service 分组显示
 *    required / 已确认缺口 / 仍缺证据 / 已选动作。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  corridorSitePlanSummary, sitePlanServiceGroups, sitePlanServiceGroupRows,
} from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  EVIDENCE_SOURCE_TYPE_TEXT, bindPlanningEvidence, planningEvidenceSummary,
  renderPlanningEvidence,
} from '../cns_planner/web/js/workflow/planning_evidence.js';

// ---------------------------------------------------------------------------
// fixture：与后端 `/api/planning-evidence` + `/api/planning-evidence/fields`
// ---------------------------------------------------------------------------

const FIELD_STATUS = [
  {
    field: 'communication_network_scope', label: '通信网络归属范围（机载侧）',
    subsystem: 'C', value_type: 'enum_scalar',
    allowed: ['public', 'private', 'dedicated', 'managed_service', 'other'],
    demand_field: 'network_scope', semantics: '需求侧 type.network_scope 是真实类型门禁。',
    required_value: 'dedicated', demanded_by_requirement: true,
    declared_by_aircraft_profile: 'dedicated', effective_value: 'dedicated',
    recorded_evidence_id: null, recorded_source_type: null, status: 'satisfied',
  },
  {
    field: 'remote_id_participation', label: '机载网络远程识别参与能力',
    subsystem: 'S', value_type: 'enum',
    allowed: ['network_remote_id', 'adsb', 'multilateration'],
    demand_field: 'technology',
    semantics: '机载是 cooperative target，不是被动 sensor。',
    required_value: 'network_remote_id', demanded_by_requirement: true,
    declared_by_aircraft_profile: null, effective_value: null,
    matches_requirement: null, status_reason: '机载未声明参与能力',
    recorded_evidence_id: null, recorded_source_type: null, status: 'evidence_required',
  },
];

const ACTIVE_ASSUMPTION = {
  evidence_id: 'PEV-SYN-RID-001', field: 'remote_id_participation',
  scope: 'aircraft', target_id: 'AIRCRAFT-SYN-E2E-01',
  value: ['network_remote_id'], source_type: 'engineering_assumption',
  source: 'synthetic_acceptance_profile',
  statement: '本测试场景假设该机载平台具备 network Remote ID 合作参与能力。',
  reason: '使 Step4→Step5 主链在本测试场景中可评估',
  report_disclosure: '本项为工程规划假设，不代表厂家既有设备事实。',
  declared_by: 'user', confirmed: true, confirmed_by_user: true,
  confirmation_status: 'confirmed', authority_effect: 'allowed_with_disclosure',
  created_at: '2026-10-02T08:00:00+00:00', status: 'active',
};

/** 真实项目口径：机载接口 validation_radio_v1 与需求 ip **无交集**。 */
const INCOMPATIBLE_INTERFACES = {
  field: 'communication_airborne_interfaces', label: '通信机载接口（机载侧）',
  subsystem: 'C', value_type: 'enum', allowed: ['ip'],
  demand_field: 'interfaces', semantics: '要求机载与地面提供者接口有交集。',
  required_value: ['ip'], demanded_by_requirement: true,
  declared_by_aircraft_profile: ['validation_radio_v1'],
  effective_value: ['validation_radio_v1'], matches_requirement: false,
  status_reason: '机载接口与需求接口无交集（地面提供者要求 ip）',
  recorded_evidence_id: null, recorded_source_type: null, status: 'incompatible',
};

function flowWithEvidence({ active = [], extraStatus = [INCOMPATIBLE_INTERFACES] } = {}) {
  return {
    selected_aircraft_profile_id: 'AIRCRAFT-SYN-E2E-01',
    planning_evidence: {
      registry: {schema_version: 'round2.4-engineering-evidence', items: active},
      active,
      field_status: [...FIELD_STATUS, ...extraStatus],
      disclosure_lines: active.length
        ? ['机载网络远程识别参与能力＝network_remote_id（工程规划假设（不代表厂家既有设备事实）；'
          + '来源：synthetic_acceptance_profile）。本项为工程规划假设，不代表厂家既有设备事实。']
        : [],
      aircraft_evidence: active.length
        ? {source_type: 'engineering_assumption', planning_input_only: true}
        : null,
    },
    planning_evidence_fields: {
      fields: {
        remote_id_participation: {label: '机载网络远程识别参与能力'},
      },
      source_types: ['confirmed_source_fact', 'engineering_assumption', 'unknown'],
      container: 'project_state.planning_evidence',
      never_written_to_device_catalog: true,
    },
  };
}

// ---------------------------------------------------------------------------
// 1. 渲染：缺证据必须被如实指出，且给出可选取值
// ---------------------------------------------------------------------------

test('step4 renders the engineering evidence entry with honest missing-evidence state', () => {
  const html = renderPlanningEvidence(flowWithEvidence());
  assert.match(html, /缺失工程证据/);
  assert.match(html, /需要工程依据（当前无法判定）/);
  //: 逐字段说明"需求要求什么 / 机载声明了什么 / 当前生效值"。
  assert.match(html, /需求要求 <code>technology<\/code> = <b>network_remote_id<\/b>/);
  assert.match(html, /机载档案声明：<b>未声明<\/b>/);
  //: 可选取值来自后端枚举，前端不猜测。
  assert.match(html, /<option value="network_remote_id">network_remote_id<\/option>/);
  //: 来源类型必须可选，且三种语义都可见。
  assert.match(html, /<option value="confirmed_source_fact">有正式资料支持的事实<\/option>/);
  assert.match(html, /<option value="engineering_assumption">工程规划假设<\/option>/);
  assert.match(html, /<option value="unknown">尚无依据（保持未知）<\/option>/);
  //: 假设必填项与确认框必须出现在表单里。
  assert.match(html, /假设陈述（工程规划假设必填）/);
  assert.match(html, /报告披露文本（工程规划假设必填）/);
  assert.match(html, /不得冒充厂家设备事实/);
});

test('already-satisfied fields are shown as satisfied and not as blockers', () => {
  const html = renderPlanningEvidence(flowWithEvidence());
  assert.match(html, /已具备可用依据/);
  //: 满足的字段绝不进"缺失工程证据"阻塞列表（该列表只列不兼容与缺证据）。
  const blockers = html.split('缺失工程证据')[1].split('录入 / 确认工程依据')[0];
  assert.doesNotMatch(blockers, /通信网络归属范围/);
  assert.match(blockers, /机载网络远程识别参与能力/);
  assert.match(blockers, /通信机载接口（机载侧）：已确认不满足需求/);
});

test('recorded assumptions are listed with their source-type badge and disclosure', () => {
  const html = renderPlanningEvidence(flowWithEvidence({ active: [ACTIVE_ASSUMPTION] }));
  assert.match(html, /工程规划假设/);
  assert.match(html, /data-source-type="engineering_assumption"/);
  assert.match(html, /synthetic_acceptance_profile/);
  assert.match(html, /本项为工程规划假设，不代表厂家既有设备事实。/);
  assert.match(html, /报告披露/);
  assert.match(html, /撤回该工程依据/);
});

test('an unloaded field catalogue is reported instead of guessed', () => {
  const html = renderPlanningEvidence({ planning_evidence: {} });
  assert.match(html, /工程依据字段清单尚未载入/);
  assert.match(html, /不会猜测任何取值/);
});

test('a confirmed incompatibility is reported as a blocker, never as missing evidence', () => {
  const html = renderPlanningEvidence(flowWithEvidence());
  assert.match(html, /已确认不满足需求（不是缺证据）/);
  assert.match(html, /逐项核对：<b>机载接口与需求接口无交集/);
  //: 必须显式列为阻塞项，并说明"不会降级成缺证据"（HTML 转义引号，故按语义断言）。
  const blockers = html.split('缺失工程证据')[1].split('录入 / 确认工程依据')[0];
  assert.match(blockers, /通信机载接口（机载侧）：已确认不满足需求/);
  assert.match(blockers, /这属于真实不兼容/);
  assert.match(blockers, /系统不会把它降级成/);
  assert.match(blockers, /缺证据/);
  assert.match(blockers, /地面提供者要求 ip/);
});

test('summary separates confirmed incompatibilities from missing evidence', () => {
  const summary = planningEvidenceSummary(flowWithEvidence());
  assert.equal(summary.incompatible.length, 1);
  assert.equal(summary.incompatible[0].field, 'communication_airborne_interfaces');
  //: 不兼容字段绝不混进"待补证据"。
  assert.ok(!summary.pending.some(
    (item) => item.field === 'communication_airborne_interfaces',
  ));
});

test('summary separates required / pending / incompatible / active', () => {
  const summary = planningEvidenceSummary(flowWithEvidence({ active: [ACTIVE_ASSUMPTION] }));
  assert.equal(summary.required.length, 3);
  //: 缺证据与已确认不兼容必须分开计数。
  assert.equal(summary.pending.length, 1);
  assert.equal(summary.pending[0].field, 'remote_id_participation');
  assert.equal(summary.incompatible.length, 1);
  assert.equal(summary.active.length, 1);
  assert.equal(summary.aircraftEvidence.source_type, 'engineering_assumption');
});

// ---------------------------------------------------------------------------
// 2. 提交：走与人工点击完全相同的正式接口
// ---------------------------------------------------------------------------

function fakeController(flow, values, { mutateAndRefresh = false } = {}) {
  const calls = [];
  const bound = new Map();
  const element = (id) => {
    if (!(id in values)) return undefined;
    if (!bound.has(id)) {
      const value = values[id];
      let node;
      if (value && typeof value === 'object' && value.multiple) {
        node = {value: '', selectedOptions: value.options.map((item) => ({value: item}))};
      } else if (value && typeof value === 'object' && 'checked' in value) {
        node = {checked: value.checked};
      } else {
        node = {value};
      }
      node.onclick = null;
      bound.set(id, node);
    }
    return bound.get(id);
  };
  const record = (path, payload) => {
    calls.push({path, payload});
    return Promise.resolve({ok: true});
  };
  const controller = {
    calls,
    flow: () => flow,
    $: element,
    panelError: (message) => calls.push({panelError: message}),
    resourceAction: record,
  };
  if (mutateAndRefresh) controller.resourceMutationAndRefresh = record;
  return controller;
}

test('saving an engineering assumption posts the full disclosure bundle', async () => {
  const controller = fakeController(flowWithEvidence(), {
    pev_remote_id_participation: {multiple: true, options: ['network_remote_id']},
    pev_remote_id_participation_source_type: 'engineering_assumption',
    pev_remote_id_participation_source: 'synthetic_acceptance_profile',
    pev_remote_id_participation_declared_by: 'user',
    pev_remote_id_participation_statement: '本测试场景假设该机载平台具备 RID 参与能力。',
    pev_remote_id_participation_reason: '使主链在本测试场景可评估',
    pev_remote_id_participation_disclosure: '本项为工程规划假设，不代表厂家既有设备事实。',
    pev_remote_id_participation_confirmed: {checked: true},
    save_remote_id_participation: {},
  }, {mutateAndRefresh: true});
  bindPlanningEvidence(controller);
  await controller.$('save_remote_id_participation').onclick();

  assert.equal(controller.calls.length, 1);
  const [call] = controller.calls;
  assert.equal(call.path, '/api/planning-evidence');
  const item = call.payload.planning_evidence;
  assert.equal(item.field, 'remote_id_participation');
  assert.equal(item.scope, 'aircraft');
  assert.equal(item.target_id, 'AIRCRAFT-SYN-E2E-01');
  assert.equal(item.source_type, 'engineering_assumption');
  assert.deepEqual(item.value, ['network_remote_id']);
  assert.equal(item.confirmed, true);
  assert.equal(item.confirmed_by_user, true);
  assert.match(item.report_disclosure, /不代表厂家既有设备事实/);
  //: 绝不携带任何"设备目录/机载档案写入"意图。
  assert.equal('device_id' in item, false);
  assert.equal('aircraft_id' in item, false);
});

test('a failed save surfaces the business reason instead of failing silently', async () => {
  const controller = fakeController(flowWithEvidence(), {
    pev_remote_id_participation: {multiple: true, options: ['network_remote_id']},
    pev_remote_id_participation_source_type: 'engineering_assumption',
    save_remote_id_participation: {},
  }, {mutateAndRefresh: true});
  controller.resourceMutationAndRefresh = () => Promise.reject(
    new Error('engineering_assumption 缺少必填字段：report_disclosure'),
  );
  bindPlanningEvidence(controller);
  await assert.rejects(() => controller.$('save_remote_id_participation').onclick());
  const reported = controller.calls.filter((call) => call.panelError);
  assert.equal(reported.length, 1);
  assert.match(reported[0].panelError, /保存工程依据失败/);
  assert.match(reported[0].panelError, /report_disclosure/);
});

test('choosing unknown sends no value so the gap is recorded honestly', async () => {
  const controller = fakeController(flowWithEvidence(), {
    pev_remote_id_participation: {multiple: true, options: []},
    pev_remote_id_participation_source_type: 'unknown',
    pev_remote_id_participation_source: '尚无资料',
    save_remote_id_participation: {},
  });
  bindPlanningEvidence(controller);
  await controller.$('save_remote_id_participation').onclick();
  const item = controller.calls[0].payload.planning_evidence;
  assert.equal(item.source_type, 'unknown');
  assert.equal(item.value, null);
});

test('withdrawing a recorded evidence posts the formal withdraw endpoint', async () => {
  const controller = fakeController(flowWithEvidence({active: [ACTIVE_ASSUMPTION]}), {
    'withdraw_PEV-SYN-RID-001': {},
  });
  bindPlanningEvidence(controller);
  await controller.$('withdraw_PEV-SYN-RID-001').onclick();
  assert.equal(controller.calls[0].path, '/api/planning-evidence/withdraw');
  assert.equal(controller.calls[0].payload.evidence_id, 'PEV-SYN-RID-001');
});

test('source-type labels never describe an assumption as a manufacturer fact', () => {
  assert.match(EVIDENCE_SOURCE_TYPE_TEXT.engineering_assumption.note, /不代表厂家既有设备事实/);
  assert.match(EVIDENCE_SOURCE_TYPE_TEXT.unknown.note, /不参与任何判定/);
  assert.match(EVIDENCE_SOURCE_TYPE_TEXT.confirmed_source_fact.note, /必须给出资料出处/);
});

// ---------------------------------------------------------------------------
// 3. P16 按 canonical service 分组
// ---------------------------------------------------------------------------

const PLAN_WITH_GROUPS = {
  status: 'proposal_ready',
  stop_reason: 'all_evaluable_confirmed_objectives_met',
  target_voxel_count: 3,
  target_service_groups: [
    {service_key: 'C:communication', bucket: 'C:communication', legacy_subsystem: null,
     required: 1, confirmed_gap: 1, unknown: 0, selected: 1},
    {service_key: 'S:rid_cooperative', bucket: 'S:rid_cooperative', legacy_subsystem: null,
     required: 1, confirmed_gap: 0, unknown: 1, selected: 0},
    {service_key: null, bucket: 'legacy:N', legacy_subsystem: 'N',
     required: 1, confirmed_gap: 0, unknown: 0, selected: 0},
  ],
  targets: [], residual_confirmed_targets: [], residual_unknown_evidence: {},
  selected_actions: [], candidate_actions: [], candidate_impacts: [],
  unknown_evidence_required: [], iteration_trace: [],
};

test('p16 groups targets by canonical service, keeping legacy separate', () => {
  const groups = sitePlanServiceGroups(PLAN_WITH_GROUPS);
  assert.equal(groups.length, 3);
  const byKey = Object.fromEntries(groups.map((item) => [item.service_key, item]));
  assert.equal(byKey['C:communication'].selected, 1);
  assert.equal(byKey['S:rid_cooperative'].unknown, 1);
  //: legacy 目标绝不混进正式服务口径：它有自己的分组键与中文说明。
  assert.equal(byKey['legacy:N'].required, 1);
  assert.match(byKey['legacy:N'].label, /未声明服务（子系统 N）/);
});

test('p16 group rows show required / confirmed gap / unknown / selected', () => {
  const html = sitePlanServiceGroupRows(PLAN_WITH_GROUPS);
  assert.match(html, /按服务分组的规划目标/);
  assert.match(html, /目标 1 · 已确认缺口 1 · 仍缺证据 0 · 已选动作 1/);
  assert.match(html, /legacy:子系统/);
  assert.match(html, /绝不产生无法被候选动作满足的目标/);
  //: 分组块出现在 P16 summary 里。
  const summary = corridorSitePlanSummary(PLAN_WITH_GROUPS);
  assert.match(summary, /按服务分组的规划目标/);
});

test('p16 falls back to target-derived grouping when the backend omits groups', () => {
  const legacyPayload = {
    status: 'no_eligible_proposal', stop_reason: 'no_positive_confirmed_marginal_gain',
    targets: [
      {target_id: 'T1', service_key: 'C:communication', subsystem: 'C'},
      {target_id: 'T2', subsystem: 'N'},
    ],
    residual_confirmed_targets: [{target_id: 'T2', subsystem: 'N'}],
    residual_unknown_evidence: {}, selected_actions: [],
    candidate_actions: [], candidate_impacts: [], unknown_evidence_required: [],
    iteration_trace: [],
  };
  const groups = sitePlanServiceGroups(legacyPayload);
  const byKey = Object.fromEntries(groups.map((item) => [item.service_key, item]));
  assert.equal(byKey['C:communication'].required, 1);
  assert.equal(byKey['legacy:N'].required, 1);
  assert.equal(byKey['legacy:N'].confirmed_gap, 1);
});
