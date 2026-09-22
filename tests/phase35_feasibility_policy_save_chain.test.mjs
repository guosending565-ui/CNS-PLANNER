/**
 * Phase 3.5 BUG-ROUTE-002 regression：Step03 → 分层候选 → terrain/building feasibility
 * 的 feasibility policy 保存链。
 *
 * 现象：人工填写 feasibility policy source 并勾选「显式确认 feasibility policy」后点击
 * 「保存 feasibility policy」，重渲染的表单把 source 清空、confirmation 复位；随后再保存
 * 就会把已 confirmed 的策略静默降级成 pending_confirmation，building vertical clearance
 * 与 selected layer mask 因此永远停在待确认 / not_calculated。
 *
 * 本文件锁定三条不变量：
 *   1. 后端 policy 的 source / confirmed 必须回填到表单（渲染层不丢状态）；
 *   2. 表单 → payload 的往返必须保真（第二次保存不得丢掉 source 与 explicit confirmation）；
 *   3. 未确认的 policy 绝不臆造 source / checked（fail-closed）。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  LAYERED_V1_ALGORITHM_ID,
  layeredFeasibilityPayloadFrom,
  renderLayeredRoutePlannerPanel,
  layeredRoutePlannerModel,
} from '../cns_planner/web/js/workflow/layered_route_planner.js';
import {
  THETA_STAR_V2_ALGORITHM_ID,
  bindLayeredThetaV2,
  layeredThetaV2Model,
  renderLayeredThetaV2Panel,
} from '../cns_planner/web/js/workflow/layered_theta_v2.js';

const HUMAN_SOURCE = 'Phase3.5人工验收，沿用项目当前已确认20m垂直净空工程基线';
const CLEARANCE_M = 20.0;

/** 一条已 confirmed 的 feasibility policy（20 m 基线 + 人工 source + 显式确认）。 */
function feasibilityPolicy(overrides = {}) {
  return {
    schema_version: 'layered-route-planner-v1',
    terrain_vertical_clearance_m: CLEARANCE_M,
    source: HUMAN_SOURCE,
    evidence: null,
    confirmed: true,
    status: 'confirmed',
    status_reason: null,
    parameter_status: 'no_default_clearance',
    ...overrides,
  };
}

function costPolicy() {
  return {
    schema_version: 'layered-route-planner-v1',
    ground_lambda: null, air_traffic_lambda: null, environment_obstacle_lambda: null,
    source: '未配置；Layered Route Planner cost weight 必须由项目工程依据显式确认',
    evidence: null, confirmed: false, status: 'pending_confirmation',
    status_reason: 'cost_weights_not_configured', parameter_status: 'no_default_lambda',
  };
}

function flow(overrides = {}, { algorithmId = THETA_STAR_V2_ALGORITHM_ID, policy } = {}) {
  const activePolicy = policy || feasibilityPolicy();
  const base = {
    scenario_routes: [{
      route_id: 'R0013', direction: 'N001→N002', start_node_id: 'N001', end_node_id: 'N002',
    }],
    layered_route_planning_request: {
      schema_version: 'layered-route-planner-v1', request_id: null, scenario_route_id: 'R0013',
      start_node_id: null, end_node_id: null, altitude_layer_id: 'ALT-ZS-080-EGM2008',
      source: 'phase3_real_zhoushan_validation', evidence: null, confirmed: true,
      status: 'confirmed', status_reason: null,
      parameter_status: 'no_default_altitude_layer', layer_resolution: 'not_resolved',
      explicit_layer_selection_only: true,
    },
    layered_route_feasibility_policy: activePolicy,
    layered_route_cost_policy: costPolicy(),
    layered_route_planner_readiness: {
      status: 'blocked',
      algorithm: { algorithm_id: algorithmId, algorithm_version: '2.0', uses_theta_star: true },
      request: {
        status: 'confirmed', confirmed: true, scenario_route_id: 'R0013',
        altitude_layer_id: 'ALT-ZS-080-EGM2008',
      },
      altitude_layer_catalog: {
        status: 'configured', count: 1, altitude_layer_ids: ['ALT-ZS-080-EGM2008'],
        selected_altitude_layer_id: 'ALT-ZS-080-EGM2008',
        cruise_altitude: {
          status: 'confirmed', altitude_egm2008_m: 800, vertical_reference: 'egm2008_orthometric',
        },
      },
      scenario_route: { status: 'resolved', route_id: 'R0013', count: 1 },
      feasibility_policy: {
        status: activePolicy.status, fingerprint: 'layeredfeasv1-x',
        terrain_vertical_clearance_m: activePolicy.terrain_vertical_clearance_m,
        parameter_status: activePolicy.parameter_status, source: activePolicy.source,
      },
      cost_policy: {
        status: 'pending_confirmation', fingerprint: null, active_domains: [],
        parameter_status: 'no_default_lambda',
        domains: {
          ground: { lambda: null, enabled: false, configured: false },
          air_traffic: { lambda: null, enabled: false, configured: false },
          environment_obstacle: { lambda: null, enabled: false, configured: false },
        },
      },
      // 用户报告的第三个现象：building vertical clearance 仍是 pending_confirmation。
      building_clearance_policy: {
        status: 'pending_confirmation', vertical_clearance_m: null, horizontal_clearance_m: null,
        source: '未配置；必须由项目工程依据确认', reused_not_redefined: true,
      },
      feasibility_mask: {
        status: 'not_calculated', counts: {}, mask_fingerprint: null, current_applicability: null,
      },
      risk_framework_v2: { status: 'passed', input_fingerprint: 'riskv2-input', overall_used: false },
      theta_star_v2: {
        algorithm: {
          algorithm_id: THETA_STAR_V2_ALGORITHM_ID, algorithm_version: '2.0', uses_theta_star: true,
        },
        status: 'ready', blockers: [],
        population_shelter: {
          status: 'passed', cell_count: 4, resolved_cell_count: 4,
          field_fingerprint: 'popshelterv1-abc',
          raw_exposure_definition: 'population_density_people_km2 * shelter_coefficient',
          risk_index_definition: 'normalized_population_factor * shelter_coefficient',
          risk_index_range: [0, 1],
        },
        objective: {
          formula: 'J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + distance_weight * distance_m',
          risk_weight: 0.8, turn_weight: 0.1, distance_weight: 0.1,
          provenance: 'user_defined_baseline', objective_population_shelter_only: true,
        },
        evaluation_constraint: {
          metric: 'route_risk_density', threshold: 1,
          source: 'user_defined_temporary_wide_constraint', temporary: true,
          role: 'candidate_evaluation_acceptance_constraint', objective_term: false,
        },
        regulatory_constraints: {
          status: 'not_evaluated', regulatory_compliance: 'not_evaluated',
          constraint_dataset_status: 'not_configured', constraint_count: 0,
          confirmed_constraint_count: 0, dataset_fingerprint: null, configured: false,
          statement: '未配置任何 regulatory constraint 数据集。',
        },
        communication: {
          interface: 'communication_planning_field', status: 'not_configured', provider: null,
          source: null, cell_count: 0, informational_fingerprint: 'commsfieldv1-empty',
          used_in_cost: false, used_as_constraint: false, affected_path_or_cost: false,
          readiness: 'interface_declared_no_field_configured',
        },
        airspace: {
          status: 'not_applicable', applicability: 'display_only',
          used_in_search: false, used_in_hard_gate: false, used_in_fingerprint: false,
        },
      },
      blockers: [],
      sources: { terrain: { available: true, reason: null }, population: { available: true, reason: null } },
    },
    shelter_coefficient_policy: {
      schema_version: 'population-shelter-v1', status: 'confirmed', status_reason: null,
      default_coefficient: 1, per_grid_overrides: {}, unit: 'dimensionless_shelter_coefficient_0_1',
      range: [0, 1], source: 'user_defined_baseline', confirmed: true,
      provenance: 'user_defined_baseline', parameter_status: 'explicit_confirmed_coefficient',
    },
    population_shelter: {
      schema_version: 'population-shelter-v1', attribute: 'population_shelter', status: 'passed',
      source: 'derived', grid_level: 8, count: 4, covered_count: 4,
      field_fingerprint: 'popshelterv1-abc', shelter_coefficient_policy_fingerprint: 'shelterpolicyv1-abc',
      cells: {
        A: { grid_id: 'A', status: 'passed' }, B: { grid_id: 'B', status: 'passed' },
        C: { grid_id: 'C', status: 'passed' }, D: { grid_id: 'D', status: 'passed' },
      },
    },
    regulatory_constraints: {
      status: 'not_configured', count: 0, items: [], dataset_fingerprint: null,
    },
    communication_planning_field: {
      status: 'not_configured', provider: null, count: 0, cells: {},
    },
    theta_v2_objective_policy: {
      schema_version: 'layered-theta-star-v2', status: 'confirmed',
      risk_weight: 0.8, turn_weight: 0.1, distance_weight: 0.1,
      source: 'user_defined_baseline', provenance: 'user_defined_baseline', confirmed: true,
      algorithm_policy_baseline: true, weights_are_editable: true, sum_constraint: 1,
      sum_tolerance: 1e-9,
      formula: 'J = risk_weight * risk_exposure_index_m + turn_weight * turn_cost_m + distance_weight * distance_m',
      objective_population_shelter_only: true,
    },
    max_route_risk_density: {
      schema_version: 'layered-theta-star-v2', constraint_id: 'max_route_risk_density',
      metric: 'route_risk_density', threshold: 1, comparison: 'less_than_or_equal',
      unit: 'dimensionless_length_weighted_mean_index',
      source: 'user_defined_temporary_wide_constraint', confirmed: true, temporary: true,
      provenance: 'user_defined_temporary_wide_constraint', status: 'confirmed',
      role: 'candidate_evaluation_acceptance_constraint', objective_term: false,
      changes_objective_weights: false,
    },
    layered_route_candidates: {
      status: 'not_calculated', count: 0, current_key: null,
      current_candidate_fingerprint: null, items: [], masks: {},
    },
  };
  return { ...base, ...overrides };
}

/** 从渲染出的 HTML 里解析控件（等价于浏览器把属性落到 DOM 属性上的结果）。 */
function fieldsFromHtml(html) {
  const unescape = (value) => String(value)
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  const fields = {};
  for (const match of html.matchAll(/<input\b[^>]*>/g)) {
    const tag = match[0];
    const id = (tag.match(/\bid="([^"]*)"/) || [])[1];
    if (!id) continue;
    const raw = (tag.match(/\bvalue="([^"]*)"/) || [])[1];
    fields[id] = {
      value: raw === undefined ? '' : unescape(raw),
      checked: /\bchecked\b/.test(tag),
      attributes: {},
    };
  }
  return fields;
}

function bindHarness(renderedFields) {
  const calls = [];
  const ids = new Set([...Object.keys(renderedFields), 'saveLayeredFeasibilityPolicy']);
  const c = {
    flow: () => flow(),
    $: (id) => (ids.has(id) ? renderedFields[id] : null),
    panelError: (message) => calls.push(['__error', message]),
    resourceAction: (path, payload) => { calls.push([path, payload]); return Promise.resolve({}); },
    actionButton: () => {},
  };
  return { c, calls };
}

// --------------------------------------------------------------------------------------
// 1. 渲染层必须回填 source / explicit confirmation
// --------------------------------------------------------------------------------------


test('bug-route-002: the theta v2 feasibility form re-renders the saved source and confirmation', () => {
  const html = renderLayeredThetaV2Panel(flow());
  const fields = fieldsFromHtml(html);
  assert.ok(fields.layeredFeasibilitySource, '#layeredFeasibilitySource 必须存在');
  assert.equal(fields.layeredFeasibilitySource.value, HUMAN_SOURCE,
    '已保存的 feasibility policy source 必须在重渲染后原样回填');
  assert.ok(fields.layeredFeasibilityConfirmed, '#layeredFeasibilityConfirmed 必须存在');
  assert.equal(fields.layeredFeasibilityConfirmed.checked, true,
    '已保存的显式确认必须在重渲染后保持勾选');
  assert.equal(fields.layeredTerrainClearance.value, String(CLEARANCE_M),
    '既有 20 m 净空值不得被重渲染改写');
});

test('bug-route-002: the V1 baseline panel re-renders the same source and confirmation', () => {
  const html = renderLayeredRoutePlannerPanel(flow({}, { algorithmId: LAYERED_V1_ALGORITHM_ID }));
  const fields = fieldsFromHtml(html);
  assert.equal(fields.layeredFeasibilitySource.value, HUMAN_SOURCE);
  assert.equal(fields.layeredFeasibilityConfirmed.checked, true);
});

test('bug-route-002: the theta v2 model exposes the backend confirmed flag', () => {
  const confirmed = layeredThetaV2Model(flow());
  assert.equal(confirmed.feasibility.source, HUMAN_SOURCE);
  assert.equal(confirmed.feasibility.confirmed, true);
  assert.equal(confirmed.feasibility.clearance, CLEARANCE_M);

  const pending = layeredThetaV2Model(flow({}, {
    policy: feasibilityPolicy({ source: '未配置；必须由项目工程依据显式确认 terrain_vertical_clearance_m', confirmed: false, status: 'pending_confirmation' }),
  }));
  assert.equal(pending.feasibility.confirmed, false);
});

test('bug-route-002: the V1 model exposes the same feasibility source and confirmation', () => {
  const model = layeredRoutePlannerModel(flow({}, { algorithmId: LAYERED_V1_ALGORITHM_ID }));
  assert.equal(model.feasibility.source, HUMAN_SOURCE);
  assert.equal(model.feasibility.confirmed, true);
});

// --------------------------------------------------------------------------------------
// 2. 表单 → payload 往返必须保真（重复保存不得静默降级）
// --------------------------------------------------------------------------------------


test('bug-route-002: a re-rendered form round-trips source and confirmation into the payload', () => {
  const fields = fieldsFromHtml(renderLayeredThetaV2Panel(flow()));
  const payload = layeredFeasibilityPayloadFrom((id) => fields[id] || null);
  assert.deepEqual(payload, {
    terrain_vertical_clearance_m: CLEARANCE_M,
    source: HUMAN_SOURCE,
    confirmed: true,
  });
});

test('bug-route-002: saving twice does not silently downgrade a confirmed policy', async () => {
  const fields = fieldsFromHtml(renderLayeredThetaV2Panel(flow()));
  const harness = bindHarness(fields);
  bindLayeredThetaV2(harness.c);
  await harness.c.resourceAction('/api/layered-route-feasibility-policy',
    layeredFeasibilityPayloadFrom((id) => fields[id] || null));
  const [path, payload] = harness.calls.pop();
  assert.equal(path, '/api/layered-route-feasibility-policy');
  assert.equal(payload.source, HUMAN_SOURCE,
    '第二次保存必须仍然提交已确认的 source：空 source 会把 policy 降级成 pending_confirmation');
  assert.equal(payload.confirmed, true, '第二次保存必须仍然提交 explicit confirmation');
  assert.equal(payload.terrain_vertical_clearance_m, CLEARANCE_M);
});

test('bug-route-002: an unconfirmed policy renders no source and an unchecked box (fail-closed)', () => {
  const html = renderLayeredThetaV2Panel(flow({}, {
    policy: feasibilityPolicy({
      terrain_vertical_clearance_m: null, source: '未配置；必须由项目工程依据显式确认 terrain_vertical_clearance_m',
      confirmed: false, status: 'blocked', status_reason: 'terrain_vertical_clearance_not_configured',
    }),
  }));
  const fields = fieldsFromHtml(html);
  assert.equal(fields.layeredFeasibilityConfirmed.checked, false,
    '未确认时绝不预勾选：不得绕过 confirmation 解除 blocker');
  assert.equal(fields.layeredTerrainClearance.value, '',
    '没有确认的净空时输入框必须为空，前端不得臆造默认值');
  // 后端占位说明不是可编辑的工程依据：不得回填成"已确认 source"。
  assert.doesNotMatch(fields.layeredFeasibilitySource.value, /^未配置/,
    '未配置占位文本不得作为 source 回填');
});
