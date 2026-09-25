/**
 * Phase4-B5X 前端定向测试：artifact lazy read / bbox 视口 / 快照瘦身 hydrate。
 *
 * 覆盖：
 *  1. 约束场地图按**当前视口 bbox** 请求（不再固定拉全量）；
 *  2. 后端只回 bbox 内的格时，展示模型仍然给出全场规模与三态计数；
 *  3. 通用快照瘦身（grid 只有摘要）时，hydrate 判定仍然成立；
 *  4. hydrate 会同时取回风险场 / 分层候选的逐 cell 明细；
 *  5. radar 逐点明细只在显式调用时读取，且写回 flow.radar_surveillance_layout.detail；
 *  6. 明细不可用时给出**中文业务提示**（技术码不进入用户可见文本）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {constraintMapUrl, constraintMapModel, loadConstraintField} from
  '../cns_planner/web/js/workflow/constraint_field.js';
import {createConstraintFieldView} from
  '../cns_planner/web/js/workflow/constraint_view.js';
import {
  createWorkflowSnapshotApplier, declaresDetailAvailable, hasGrid, needsGridHydration,
} from '../cns_planner/web/js/state/workflow_snapshot.js';
import {loadRadarSurveillanceDetail, RADAR_LAYOUT_ENDPOINT} from
  '../cns_planner/web/js/workflow/radar_surveillance_layout.js';

const BBOX = [120.0, 30.0, 120.5, 30.5];

function slimSnapshot() {
  return {
    revision: 3,
    grid: {
      status: 'passed', level: 8, count: 24300, cell_count: 24300,
      detail_available: true, detail_endpoint: '/api/workspace/grid', cells_count: 24300,
    },
    grid_attributes: {population: {status: 'passed', detail_available: true}},
    grid_risk: {status: 'passed', cells_detail: 'artifact', detail_available: true},
    grid_risk_v2: {status: 'passed', cells_detail: 'artifact', detail_available: true},
    layered_route_candidates: {
      status: 'passed', active_candidate_id: 'C1', items_detail: 'artifact',
      masks_detail: 'artifact', detail_available: true,
    },
    planning_constraint_fields: {
      status: 'passed', count: 1,
      items: [{
        field_id: 'PCF-ALT-080-x', altitude_layer_id: 'ALT-080',
        counts: {total: 24300, pass: 100, blocked: 200, unknown: 24000,
                 blocked_by: {terrain: 200, building: 0, tower: 0, airspace: 0, critical_site: 0}},
        artifact_ref: {artifact_id: 'a'.repeat(64)},
      }],
      detail_available: true, detail_endpoint: '/api/planning-constraint-field',
    },
  };
}

// ---- 1. bbox 请求 ----------------------------------------------------------

test('constraint map url carries the viewport bbox and omits it when unknown', () => {
  const withBox = constraintMapUrl('ALT-080', BBOX);
  assert.match(withBox, /bbox=120%2C30%2C120\.5%2C30\.5/);
  const without = constraintMapUrl('ALT-080', null);
  assert.equal(without.includes('bbox='), false, '没有视口时不得编造一个范围');
});

test('constraint view requests the current viewport instead of the full field', async () => {
  const calls = [];
  const api = async url => {
    calls.push(url);
    if (url.includes('/map')) {
      return {
        status: 'passed', altitude_layer_id: 'ALT-080', total_count: 24300,
        bbox: BBOX, counts: {total: 3, pass: 1, blocked: 1, unknown: 1, blocked_by: {}},
        cells: [{grid_id: 'G0', outcome: 'blocked', blocked_by: ['terrain']}],
      };
    }
    return {status: 'passed', count: 1, items: [{
      altitude_layer_id: 'ALT-080', counts: {total: 24300, pass: 100, blocked: 200,
                                             unknown: 24000, blocked_by: {}},
    }]};
  };
  const view = createConstraintFieldView({
    api,
    getFlow: () => ({
      workspace: {bbox: BBOX},
      spatial_3d: {altitude_layers: [{altitude_layer_id: 'ALT-080', nominal_altitude_m: 80,
                                      vertical_reference: 'egm2008_orthometric'}]},
      planning_constraint_fields: {
        status: 'passed', items: [{
          altitude_layer_id: 'ALT-080',
          counts: {total: 24300, pass: 100, blocked: 200, unknown: 24000, blocked_by: {}},
        }],
      },
      grid: {cells: [{grid_id: 'G0'}]},
    }),
    getLayers: () => ({altitudeConstraintLayer: true}),
    visibleBounds: () => BBOX,
    getGridCache: () => ({byId: new Map()}),
    getNode: () => null,
  });
  view.select('ALT-080');
  await view.loadMap();

  const mapCall = calls.find(url => url.includes('/map'));
  assert.ok(mapCall, '必须发出地图明细请求');
  assert.match(mapCall, /bbox=120%2C30%2C120\.5%2C30\.5/,
    '前端必须把当前视口 bbox 传给后端，而不是固定拉全量');
  const model = view.model();
  assert.equal(model.outcomes.total, 24300, '视口过滤不得改变全场规模');
  assert.equal(model.cells.length, 1, '只绘制视口内读回的格');
});

// ---- 2. 明细不可用的中文提示 ----------------------------------------------

test('map projection keeps counts and reports unavailability without inventing pass cells', () => {
  const model = constraintMapModel({
    status: 'cells_unavailable', reason: '结果明细文件不可用；请重新计算该结果',
    altitude_layer_id: 'ALT-080', counts: {total: 24300, pass: 0, blocked: 9918, unknown: 14382},
  });
  assert.equal(model.usable, false);
  assert.equal(model.cells.length, 0, '读不到明细时绝不允许画成"全部可通行"');
  assert.equal(model.counts.blocked, 9918);
});

test('loadConstraintField surfaces the backend business message', async () => {
  const api = async url => {
    if (url.includes('/map')) throw new Error('结果明细数据已损坏，请重新计算该结果');
    return {status: 'passed', items: []};
  };
  await assert.rejects(
    () => loadConstraintField(api, 'ALT-080', {bbox: BBOX}),
    /请重新计算/,
  );
});

// ---- 3/4. 快照瘦身后的 hydrate --------------------------------------------

test('slim snapshot still declares grid detail and triggers hydration', () => {
  const snapshot = slimSnapshot();
  assert.equal('cells' in snapshot.grid, false);
  assert.equal(hasGrid(snapshot), true, 'grid 摘要有 cell_count 时仍视为"有网格"');
  assert.equal(declaresDetailAvailable(snapshot), true);
  assert.equal(needsGridHydration(snapshot), true);
});

test('hydration pulls grid, risk and layered candidate details on demand', async () => {
  const snapshot = slimSnapshot();
  const calls = [];
  let flow = null;
  let serial = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: async () => { calls.push('grid'); return {cells: [{grid_id: 'G0'}]}; },
    fetchAttributes: async () => { calls.push('attributes'); return {population: {cells: {G0: {}}}}; },
    fetchRisk: async () => { calls.push('risk'); return {cells: {G0: {}}}; },
    fetchRiskV2: async () => { calls.push('risk_v2'); return {cells: {G0: {}}}; },
    fetchLayeredCandidates: async () => { calls.push('candidates'); return {masks: {}}; },
    afterApply: () => {},
  });
  await applier.applyWorkflowSnapshot(snapshot);
  assert.deepEqual(calls.sort(), ['attributes', 'candidates', 'grid', 'risk', 'risk_v2']);
  assert.equal(flow.grid.cells.length, 1);
  assert.ok(flow.grid_risk_v2.cells);
  assert.ok(flow.layered_route_candidates.masks !== undefined);
});

test('hydration tolerates a project without any externalized detail', async () => {
  let flow = null;
  let serial = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: async () => { throw new Error('不得被调用'); },
    fetchAttributes: async () => { throw new Error('不得被调用'); },
    fetchRiskV2: async () => { throw new Error('不得被调用'); },
  });
  await applier.applyWorkflowSnapshot({revision: 1, grid: null});
  assert.equal(flow.revision, 1);
});

// ---- 5. radar 逐点明细按需读取 --------------------------------------------

test('radar detail loads only on demand and lands on the display model field', async () => {
  const requested = [];
  let flow = {radar_surveillance_layout: {
    status: 'proposal_ready', count: 1,
    items: [{route_id: 'R1', selected_panel_count: 5, samples_detail: 'artifact'}],
  }};
  const detail = await loadRadarSurveillanceDetail({
    api: async url => { requested.push(url); return {status: 'proposal_ready', items: [{route_id: 'R1'}]}; },
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    afterChange: () => {},
  });
  assert.deepEqual(requested, [RADAR_LAYOUT_ENDPOINT]);
  assert.equal(flow.radar_surveillance_layout.detail, detail);
  assert.equal(flow.radar_surveillance_layout.status, 'proposal_ready',
    '载入明细不得改写业务状态');
});

test('radar detail failures never look like an empty coverage result', async () => {
  let flow = {radar_surveillance_layout: {status: 'proposal_ready'}};
  await assert.rejects(
    () => loadRadarSurveillanceDetail({
      api: async () => { throw new Error('结果明细文件不可用（文件缺失或被移动）；请重新计算该结果'); },
      getFlow: () => flow,
      setFlow: value => { flow = value; },
    }),
    /明细文件不可用/,
  );
  assert.equal(flow.radar_surveillance_layout.detail, undefined);
});
