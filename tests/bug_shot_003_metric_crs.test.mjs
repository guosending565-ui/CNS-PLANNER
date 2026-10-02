/**
 * BUG-SHOT-003 回归：连续验证的米制 CRS 必须由 orchestration 从项目正式成果自动取得。
 *
 * 修复前：面板把 ``EPSG:32651`` 硬编码为预填值，payload 只接受人工输入；
 * 留空即拒绝运行，等于"必须靠人工临时补一个 CRS"。
 *
 * 修复后：``layeredValidationMetricCrs(flow)`` 按
 * 当前候选 ``route.metric_crs`` → 历史 validation → 运行航路投影 → 工程建议值
 * 的顺序解析，并把来源如实写进 payload（``horizontal_crs_source``）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  LAYERED_VALIDATION_SUGGESTED_HORIZONTAL_CRS,
  layeredValidationHorizontalCrsPayload,
  layeredValidationMetricCrs,
} from '../cns_planner/web/js/workflow/layered_route_validation.js';

test('metric CRS comes from the current layered candidate route', () => {
  const flow = {
    layered_route_candidates: {
      items: [
        {candidate_id: 'LRC2-R0003-ALT-150', route: {metric_crs: 'EPSG:32650'}},
        {candidate_id: 'LRC2-R0005-ALT-100', route: {metric_crs: 'EPSG:32651'}},
      ],
    },
  };
  const resolved = layeredValidationMetricCrs(flow);
  assert.equal(resolved.value, 'EPSG:32650');
  assert.equal(resolved.source, 'layered_route_candidate.route.metric_crs');
});

test('metric CRS falls back to historical validations then to the suggested value', () => {
  const fromHistory = layeredValidationMetricCrs({
    layered_route_candidates: {items: []},
    layered_route_validations: {items: [{metric_crs: 'EPSG:32652'}]},
  });
  assert.equal(fromHistory.value, 'EPSG:32652');
  assert.equal(fromHistory.source, 'layered_route_validations.metric_crs');

  const fallback = layeredValidationMetricCrs({});
  assert.equal(fallback.value, LAYERED_VALIDATION_SUGGESTED_HORIZONTAL_CRS);
  assert.equal(fallback.source, 'engineering_suggested_default');
});

test('payload uses the project CRS automatically and never requires manual entry', () => {
  const flow = {layered_route_candidates: {items: [{route: {metric_crs: 'EPSG:32651'}}]}};
  const payload = layeredValidationHorizontalCrsPayload('', flow);
  assert.deepEqual(payload, {
    horizontal_crs: 'EPSG:32651',
    horizontal_crs_source: 'layered_route_candidate.route.metric_crs',
  });
});

test('an explicit value typed by the user always wins', () => {
  const flow = {layered_route_candidates: {items: [{route: {metric_crs: 'EPSG:32651'}}]}};
  assert.deepEqual(
    layeredValidationHorizontalCrsPayload('EPSG:32654', flow),
    {horizontal_crs: 'EPSG:32654'},
  );
});

test('empty CRS resolution is reported instead of silently guessing', () => {
  // 项目里既没有候选也没有历史记录时，解析仍会给出工程建议值并标注来源，
  // 因此正常路径永远不需要人工补值；只有当调用方显式传入一个"无 CRS"的 flow
  // 并禁用了建议值时才可能抛错——这里断言解析结果始终带来源。
  const resolved = layeredValidationMetricCrs({
    layered_route_candidates: {items: [{route: {metric_crs: null}}]},
  });
  assert.ok(resolved.value, '解析必须给出可用值');
  assert.ok(resolved.source, '解析必须给出可审计来源');
});
