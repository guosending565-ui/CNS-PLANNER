/**
 * Round32-C 前端回归：Step03 的两个正式动作改为**后台任务提交**，且普通视图只出现业务名。
 *
 * 独立运行：`node tests/round32c_step03_background_frontend.test.mjs`
 *
 * 锁定四件事（与后端 16 条定向测试配套）：
 *  1. 「运行正式候选航路」/「生成风险画像」通过**既有** `c.submitBackgroundTask` 提交，
 *     不再走同步 `resourceAction`（复用既有任务中心，不新建任务框架）；
 *  2. 任务名 / 提交文案是业务名（「航路候选规划」/「航路风险画像」），
 *     普通视图文案里没有算法名、指纹或原始状态值；
 *  3. 通用快照的 mask 摘要**不含 cells**（slim 投影）：地图图层在按需 hydrate 之前
 *     画不出任何 cell；
 *  4. 按需明细 hydrate 走 `fetchLayeredMasks` → 合并回 `layered_route_candidates.masks`。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { LAYERED_EVALUATE_ENDPOINT, LAYERED_TASK_NAME } from '../cns_planner/web/js/workflow/layered_theta_v2.js';
import { bindLayeredCandidatePanel } from '../cns_planner/web/js/workflow/step03_routes.js';
import { bindRouteRiskProfile } from '../cns_planner/web/js/workflow/route_risk_profile.js';
import { layeredFeasibilityCells } from '../cns_planner/web/js/map/layered_feasibility_overlay.js';
import { createWorkflowSnapshotApplier } from '../cns_planner/web/js/state/workflow_snapshot.js';

const LANE = 'R1@ALT-100';
const RRP_ENDPOINT = '/api/route-risk-profiles/evaluate';


/** Theta* V2 bind 桩：只登记面板真实存在的控件，并分别记录两类提交。 */
function layeredHarness() {
  const background = [];
  const sync = [];
  const handler = {id: null};
  const ids = new Set([
    'evaluateLayeredCandidate', 'saveLayeredRequest', 'saveLayeredFeasibilityPolicy',
    'saveThetaV2ShelterPolicy', 'saveThetaV2ObjectivePolicy', 'saveThetaV2RiskDensity',
  ]);
  const c = {
    flow: () => ({layered_route_planner_readiness: {blockers: []}, layered_route_candidates: {}}),
    $: (id) => ids.has(id)
      ? {value: '', checked: false, attributes: {}, dataset: {}, textContent: ''}
      : null,
    panelError: () => {},
    resourceAction: (path, payload) => { sync.push([path, payload]); return Promise.resolve({}); },
    submitBackgroundTask: (path, payload) => { background.push([path, payload]); return Promise.resolve({task_id: 't1'}); },
    actionButton: (id, fn) => { if (id === 'evaluateLayeredCandidate') handler.id = fn; },
  };
  return {c, background, sync, handler};
}


test('候选规划按钮通过既有 submitBackgroundTask 提交（不再同步 POST）', async () => {
  const {c, background, sync, handler} = layeredHarness();
  bindLayeredCandidatePanel(c);
  assert.equal(typeof handler.id, 'function', 'evaluateLayeredCandidate 必须被注册');
  await handler.id();
  assert.deepEqual(background, [[LAYERED_EVALUATE_ENDPOINT, {}]],
    '必须走 submitBackgroundTask（async:true 由 tasks.js 统一追加）');
  assert.deepEqual(sync, [], '不得再发同步 resourceAction');
  assert.equal(LAYERED_EVALUATE_ENDPOINT, '/api/layered-route-candidates/evaluate-real');
});


test('风险画像按钮通过既有 submitBackgroundTask 提交（不再同步 POST）', async () => {
  //: bindRouteRiskProfile 会枚举删除按钮（依赖 document）；这里给最小桩，
  //: 并让 map linkage 因缺少 routeEvidence 而提前返回（不需要真实 DOM）。
  globalThis.document = globalThis.document || {
    querySelectorAll: () => [], getElementById: () => null,
    addEventListener: () => {}, body: {contains: () => false},
  };
  const background = [];
  const sync = [];
  let registered = null;
  const c = {
    $: (id) => (id === 'evaluateRouteRiskProfile'
      ? {textContent: '生成风险画像', dataset: {}} : null),
    flow: () => ({}),
    panelError: () => {},
    resourceAction: (path, payload) => { sync.push([path, payload]); return Promise.resolve({}); },
    submitBackgroundTask: (path, payload) => { background.push([path, payload]); return Promise.resolve({task_id: 't2'}); },
    actionButton: (id, fn) => { if (id === 'evaluateRouteRiskProfile') registered = fn; },
  };
  bindRouteRiskProfile(c);
  assert.equal(typeof registered, 'function');
  await registered();
  assert.deepEqual(background, [[RRP_ENDPOINT, {}]]);
  assert.deepEqual(sync, [], '不得再发同步 resourceAction');
});


test('普通视图任务文案只用业务名，不含算法名 / 指纹 / 原始状态', () => {
  assert.equal(LAYERED_TASK_NAME, '航路候选规划');
  const layered = readFileSync('cns_planner/web/js/workflow/layered_theta_v2.js', 'utf8');
  const rrp = readFileSync('cns_planner/web/js/workflow/route_risk_profile.js', 'utf8');
  for (const source of [layered, rrp]) {
    assert.match(source, /submitBackgroundTask/);
    assert.match(source, /后台计算中，请在「后台计算任务」窗口查看进度/);
  }
  assert.match(layered, /航路候选规划/);
  assert.match(rrp, /航路风险画像/);
  // 新增的（后台任务）文案常量不得携带算法名或指纹前缀。
  const taskTexts = layered.match(/const LAYERED_[A-Z_]+='[^']*'/g) || [];
  assert.ok(taskTexts.length >= 3, taskTexts.join(' | '));
  for (const line of taskTexts) {
    assert.doesNotMatch(line, /Theta|fingerprint|layeredcandv1/);
  }
});


test('通用快照的 mask 摘要不含 cells，按需 hydrate 才装回明细', async () => {
  let flow = {
    layered_route_planning_request: {scenario_route_id: 'R1', altitude_layer_id: 'ALT-100'},
    layered_route_candidates: {
      masks_count: 1,
      masks_detail: 'artifact',
      detail_available: true,
      masks: {[LANE]: {cells_count: 3, cells_detail: 'artifact', mask_fingerprint: 'layeredmaskv1-x'}},
    },
  };
  let serial = 0;
  let applied = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: (next) => { flow = next; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: async () => ({}),
    fetchAttributes: async () => ({}),
    currentProjectIdentity: () => 'p1',
    fetchLayeredMasks: async (laneKey) => ({[laneKey]: {
      cells: {a: {grid_id: 'a', status: 'feasible'}}, cells_count: 1,
    }}),
    afterApply: () => { applied += 1; },
  });

  // slim 摘要阶段：没有 cells ⇒ 地图图层画不出任何 cell（这正是瘦身的目的）。
  assert.equal(layeredFeasibilityCells(flow).length, 0, 'slim mask 摘要不得含 cells');
  assert.equal(flow.layered_route_candidates.masks[LANE].cells, undefined);

  const outcome = await applier.hydrateLayeredMaskDetail(LANE);
  assert.deepEqual(outcome, {applied: true, hydrated: ['layered_route_candidates.masks']});
  assert.equal(applied, 1, 'hydrate 后必须触发视图重建');
  assert.equal(layeredFeasibilityCells(flow).length, 1, 'hydrate 后 cells 参与绘制');
  assert.ok(flow.layered_route_candidates.masks[LANE].cells.a);
});


test('后台任务完成后的刷新会取回航路候选明细（detail hydrate wiring）', () => {
  const main = readFileSync('cns_planner/web/js/main.js', 'utf8');
  const refresh = main.slice(main.indexOf('setupTaskCenter({'), main.indexOf('// ---- 启动装配'));
  assert.match(refresh, /hydrateRadarSurveillanceDetail/);
  assert.match(refresh, /hydrateLayeredCandidateDetail\(\)/,
    '任务发布后必须把按需的航路候选/mask 明细取回来');
  // 图层勾选触发的按需 hydrate 必须用事件委托并把节点传进去（元素可能后出现）。
  assert.match(main, /addEventListener\('change',event=>\{/);
  assert.match(main, /ensureLayeredFeasibilityMaskDetail\(node\)/);
  assert.match(main, /async function ensureLayeredFeasibilityMaskDetail\(node\)/);
});
