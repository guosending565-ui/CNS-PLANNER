/**
 * Phase4-B7X compatibility UI 边界回归：
 *  - Step01 生产算法设置不再提供归档 / compatibility 实现的新选择；
 *  - 旧项目已保存的归档选择仍作为现状可见、不被静默改写；
 *  - Step03 高级区的归档 V2 参数面板只写运行期 compatibility selection。
 *
 * 独立运行：`node tests/b7x_compatibility_ui.test.mjs`
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {render as renderStep1} from '../cns_planner/web/js/workflow/step01_project.js';
import {riskAwareRoutePanel} from '../cns_planner/web/js/workflow/step03_routes.js';

const PRODUCTION_CATALOG = [
  {algorithm_type: 'route_planner', algorithm_id: 'route_planner_v1', version: '1.0', name: 'Route Planner V1'},
  {algorithm_type: 'route_planner', algorithm_id: 'risk_aware_route_planner_v2', version: '2.0', name: 'Risk-Aware Route Planner V2'},
  {algorithm_type: 'layered_route_planner', algorithm_id: 'layered_route_planner_v1', version: '1.0', name: 'Layered Route Planner V1'},
  {algorithm_type: 'layered_route_planner', algorithm_id: 'layered_risk_aware_theta_star_v2', version: '2.0', name: 'Layered Risk-Aware Theta* V2'},
  {algorithm_type: 'coverage_planner', algorithm_id: 'coverage_planner_v1', version: '1.0', name: 'Coverage Planner V1'},
  {algorithm_type: 'cns_gap_analyzer', algorithm_id: 'cns_gap_analysis_v1', version: '1.0', name: 'CNS Gap Analysis V1'},
  {algorithm_type: 'cns_gap_analyzer', algorithm_id: 'cns_gap_analysis_v2', version: '2.0', name: 'CNS Gap Analysis V2'},
  {algorithm_type: 'site_planner', algorithm_id: 'reuse_first_site_planner_v1', version: '1.0', name: 'Reuse-first CNS Site Planner V1'},
  {algorithm_type: 'site_planner', algorithm_id: 'corridor_reuse_first_site_planner_v2', version: '2.0', name: 'Corridor-aware Reuse-first CNS Site Planner V2'},
  {algorithm_type: 'coverage_model', algorithm_id: 'geometric_coverage_3d_v1', version: '1.0', name: 'Geometric Coverage 3D V1'},
];

function renderProject(selection) {
  return renderStep1({
    state: {project_storage: {}, paths: {}, data_health: {}, workflow: {}},
    flow: {algorithm_selection: selection, algorithm_catalog: PRODUCTION_CATALOG, project: {}},
  });
}

const NEW_PROJECT_SELECTION = {
  layered_route_planner: {algorithm_type: 'layered_route_planner', algorithm_id: 'layered_risk_aware_theta_star_v2', version: '2.0', parameters: {}},
  site_planner: {algorithm_type: 'site_planner', algorithm_id: 'corridor_reuse_first_site_planner_v2', version: '2.0', parameters: {}},
  coverage_model: {algorithm_type: 'coverage_model', algorithm_id: 'geometric_coverage_3d_v1', version: '1.0', parameters: {}},
};

test('step 01 never offers a frozen compatibility algorithm to a new project', () => {
  const html = renderProject(NEW_PROJECT_SELECTION);
  for (const algorithmId of ['route_planner_v1', 'risk_aware_route_planner_v2',
    'layered_route_planner_v1', 'coverage_planner_v1', 'cns_gap_analysis_v1',
    'cns_gap_analysis_v2', 'reuse_first_site_planner_v1']) {
    assert.ok(!html.includes(`${algorithmId}@`),
      `新项目不得提供归档算法 ${algorithmId}`);
  }
  // 纯 legacy 类型的下拉必须为空（没有可选项，也没有被静默替换成正式实现）。
  for (const type of ['route_planner', 'coverage_planner', 'cns_gap_analyzer']) {
    assert.match(html, new RegExp(`data-algorithm-select="${type}"></select>`),
      `${type} 不得提供任何 compat/legacy 选项`);
  }
  assert.match(html, /geometric_coverage_3d_v1@1\.0/, '正式三维覆盖模型必须仍可选');
  assert.match(html, /归档 \/ compatibility 实现不再作为新项目可选算法/);
});

test('step 01 keeps an old project saved legacy selection visible and selectable', () => {
  const html = renderProject({
    ...NEW_PROJECT_SELECTION,
    route_planner: {algorithm_type: 'route_planner', algorithm_id: 'risk_aware_route_planner_v2', version: '2.0', parameters: {risk_weight_lambda: 1.5}},
  });
  assert.match(html, /risk_aware_route_planner_v2@2\.0/, '旧项目已保存的归档选择必须原样可见');
  assert.ok(!html.includes('· route_planner_v1@'), '未保存的归档实现仍不得成为新选择');
});

test('step 03 archived V2 parameter panel is read-only history after legacy cleanup', () => {
  const runtime = riskAwareRoutePanel({
    compatibility_selection: {
      route_planner: {algorithm_type: 'route_planner', algorithm_id: 'risk_aware_route_planner_v2', version: '2.0', parameters: {risk_weight_lambda: 2.5}, selection_source: 'runtime_compatibility_selection'},
    },
    algorithm_selection: {},
  });
  // B8X：运行期 compatibility selection 覆盖通道与 V2 参数入口已删除；面板只做只读审计，
  // 但来源标识仍按原值转印（旧会话缓存里可能仍是 runtime_compatibility_selection）。
  assert.match(runtime, /历史只读/);
  assert.match(runtime, /risk_weight_lambda 2\.5/, '面板必须显示已保存的覆盖参数原值');
  assert.doesNotMatch(runtime, /id="saveRiskRouteParameters"/);

  const saved = riskAwareRoutePanel({
    compatibility_selection: {},
    algorithm_selection: {route_planner: {algorithm_type: 'route_planner', algorithm_id: 'risk_aware_route_planner_v2', version: '2.0', parameters: {risk_weight_lambda: 0.5}}},
  });
  assert.match(saved, /saved_legacy_selection/, '旧项目已保存的 selection 是面板默认来源');
  assert.match(saved, /risk_weight_lambda 0\.5/);

  assert.equal(riskAwareRoutePanel({compatibility_selection: {}, algorithm_selection: {layered_route_planner: {algorithm_id: 'layered_risk_aware_theta_star_v2'}}}), '',
    '正式分层规划器下不得显示归档 V2 参数面板');
});
