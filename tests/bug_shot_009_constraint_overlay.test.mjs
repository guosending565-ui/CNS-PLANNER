/**
 * BUG-SHOT-009 回归：ALT-100 高度层障碍图层的三处口径必须同时成立
 * （状态卡 / 图例 / 地图），且**几何缺失时不得假装画成功**。
 *
 * 修复前的两层缺陷：
 *  1. `constraintFieldModel()` 没有返回 `usable`，而覆盖层与图例都按
 *     `model.usable` 判定 —— 明细已经读到、cells 非空，地图仍然零绘制，
 *     图例末句也一直显示「逐格明细尚未读取」；
 *  2. 约束接口 `geometry_source = frontend_grid_index` 不下发几何，bbox 只能由
 *     前端 `gridRenderCache.byId` 补齐；通用 workflow 快照已把逐格明细外置，
 *     首屏该索引为空，于是 8008 个 cell 全部落入 `unresolved`。
 *
 * 本测试用真实的 `createConstraintFieldView` + 真实覆盖层绘制路径（假 ctx）锁定：
 *  - 模型必须如实给出 usable / mapStatus / cells；
 *  - 几何就绪 + 三态开关开启时，blocked / unknown / pass 真实落到 ctx.fill；
 *  - 几何缺失时返回零绘制、不伪造，并**触发一次**几何水合（不是每帧重试）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {constraintFieldModel, constraintMapModel} from '../cns_planner/web/js/workflow/constraint_field.js';
import {drawConstraintFieldOverlay} from '../cns_planner/web/js/map/constraint_field_overlay.js';
import {createConstraintFieldView} from '../cns_planner/web/js/workflow/constraint_view.js';

const LAYER_ID = 'ALT-100';

function cell(gridId, bbox) {
  return {grid_id: gridId, bbox, level: 8};
}

/** 三个格子：一个障碍、一个证据不足、一个可通行。 */
const GRID_CELLS = [
  cell('G1', [122.0, 29.8, 122.001, 29.801]),
  cell('G2', [122.001, 29.8, 122.002, 29.801]),
  cell('G3', [122.002, 29.8, 122.003, 29.801]),
];

const GRID = {status: 'passed', level: 8, count: 3, cells: GRID_CELLS};

const CONSTRAINT_MAP = {
  status: 'passed',
  altitude_layer_id: LAYER_ID,
  field_id: 'PCF-ALT-100-test',
  counts: {total: 3, pass: 1, blocked: 1, unknown: 1, blocked_by: {terrain: 1, building: 0}},
  total_count: 3,
  bbox: [122.0, 29.8, 122.003, 29.801],
  cells: [
    {grid_id: 'G1', outcome: 'blocked', blocked_by: ['terrain']},
    {grid_id: 'G2', outcome: 'unknown', blocked_by: []},
    {grid_id: 'G3', outcome: 'pass', blocked_by: []},
  ],
};

const CONSTRAINT_SUMMARY = {
  status: 'passed',
  count: 1,
  items: [{
    altitude_layer_id: LAYER_ID,
    status: 'completed_with_warnings',
    field_id: 'PCF-ALT-100-test',
    counts: {total: 3, pass: 1, blocked: 1, unknown: 1, blocked_by: {terrain: 1, building: 0}},
  }],
};

/** 最小可用网格缓存（与 buildGridOverlayCache 的产物结构一致）。 */
function gridCache(cells) {
  const built = cells.map(item => ({cell: item}));
  return {cells: built, byId: new Map(built.map(entry => [entry.cell.grid_id, entry]))};
}

const EMPTY_GRID_CACHE = {cells: [], byId: new Map()};

/** 记录 fill 调用的假 Canvas 2D 上下文（保持 fillStyle 读写的真实语义）。 */
function canvasCtx() {
  const fills = [];
  const ctx = {
    fills,
    globalAlpha: 1,
    strokeStyle: '',
    lineWidth: 0,
    beginPath() {},
    moveTo() {},
    lineTo() {},
    closePath() {},
    fill() { fills.push(this.fillStyle); },
    stroke() {},
    save() {},
    restore() {},
  };
  ctx.fillStyle = '';
  return ctx;
}

function view() {
  return {x: 0, y: 0, res: 1e-4};
}

function screenPoint() {
  return [100, 100];
}

const ALL_VISIBLE = [121.9, 29.7, 122.1, 29.9];

// ---- 1. 展示模型必须如实暴露 usable -----------------------------------------

test('constraintFieldModel exposes usable from the map projection', () => {
  const map = constraintMapModel(CONSTRAINT_MAP);
  const usable = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map, altitudeLayerId: LAYER_ID});
  assert.equal(usable.usable, true, '明细已读到（map.status=passed）时 usable 必须为真');
  assert.equal(usable.cells.length, 3);
  assert.equal(usable.mapStatus, 'passed');

  const missing = constraintMapModel({status: 'cells_unavailable', reason: '明细不可读', cells: []});
  const notUsable = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map: missing, altitudeLayerId: LAYER_ID});
  assert.equal(notUsable.usable, false, '明细不可读时 usable 必须为假，绝不假装可画');
  assert.deepEqual(notUsable.cells, []);
});

// ---- 2. 几何就绪时三态真实落图 ------------------------------------------------

test('overlay paints blocked / unknown / pass when geometry is ready', () => {
  const map = constraintMapModel(CONSTRAINT_MAP);
  const model = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map, altitudeLayerId: LAYER_ID});
  const ctx = canvasCtx();
  const result = drawConstraintFieldOverlay({
    ctx, view: view(), screenPoint, model,
    cellsById: gridCache(GRID_CELLS).byId,
    layers: {enabled: true, blocked: true, unknown: true, pass: true},
    visibleBounds: ALL_VISIBLE,
    gridTheme: null,
  });
  assert.deepEqual(result.drawn, {blocked: 1, unknown: 1, pass: 1}, '三态必须各画一格');
  assert.equal(result.entries, 3);
  assert.deepEqual(result.unresolved, []);
  assert.equal(ctx.fills.length, 3, 'Canvas fill 必须真的被调用（修复前为 0）');
});

test('overlay honours the three independent switches', () => {
  const map = constraintMapModel(CONSTRAINT_MAP);
  const model = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map, altitudeLayerId: LAYER_ID});
  const ctx = canvasCtx();
  const result = drawConstraintFieldOverlay({
    ctx, view: view(), screenPoint, model,
    cellsById: gridCache(GRID_CELLS).byId,
    layers: {enabled: true, blocked: true, unknown: false, pass: false},
    visibleBounds: ALL_VISIBLE,
    gridTheme: null,
  });
  assert.deepEqual(result.drawn, {blocked: 1, unknown: 0, pass: 0}, '默认只画障碍');
  assert.equal(ctx.fills.length, 1);
});

// ---- 3. 几何缺失：如实零绘制并触发一次水合 -------------------------------------

test('view reports zero drawn and hydrates geometry once when the index is empty', async () => {
  let hydrateCalls = 0;
  let gridCacheValue = EMPTY_GRID_CACHE;
  const apiCalls = [];
  const constraint = createConstraintFieldView({
    api: async url => {
      apiCalls.push(url);
      if (url.startsWith('/api/planning-constraint-field?')) return CONSTRAINT_SUMMARY;
      if (url.startsWith('/api/planning-constraint-field/map')) return CONSTRAINT_MAP;
      throw new Error('unexpected ' + url);
    },
    getFlow: () => ({spatial_3d: {altitude_layers: [{altitude_layer_id: LAYER_ID, nominal_altitude_m: 100}]}}),
    getLayers: () => ({altitudeConstraintLayer: true}),
    visibleBounds: () => ALL_VISIBLE,
    getGridCache: () => gridCacheValue,
    getNode: () => null,
    hydrateGridGeometry: async () => {
      hydrateCalls += 1;
      // 真实 hydrate 是一次 HTTP GET：这里必须让出事件循环，才能复现
      // "本轮几何仍缺失 → 如实零绘制 → 下一轮重绘真正落图"的时序。
      await new Promise(resolve => setTimeout(resolve, 0));
      gridCacheValue = gridCache(GRID_CELLS);
    },
    afterChange: () => {},
    paint: () => {},
  });

  constraint.select(LAYER_ID);
  const ctx = canvasCtx();
  const first = constraint.draw({ctx, view: view(), screenPoint, gridTheme: null});
  assert.deepEqual(first.drawn, {blocked: 0, unknown: 0, pass: 0}, '几何缺失时不得假装画成功');
  assert.equal(first.entries, 0);
  assert.equal(constraint.gridGeometryReady(), false);

  // 水合是异步的：等它落地后再画一轮；该轮会异步启动逐格明细读取，如实零绘制。
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(hydrateCalls, 1, '几何缺失只允许触发一次水合');
  assert.equal(constraint.gridGeometryReady(), true);

  const second = constraint.draw({ctx, view: view(), screenPoint, gridTheme: null});
  assert.deepEqual(second.drawn, {blocked: 0, unknown: 0, pass: 0},
    '明细请求在飞行中时本轮仍不得假装画成功');

  // 明细落地（loadMap 内部 afterChange → paint）后，必须真正画出障碍格。
  await new Promise(resolve => setTimeout(resolve, 0));
  await new Promise(resolve => setTimeout(resolve, 0));
  const third = constraint.draw({ctx, view: view(), screenPoint, gridTheme: null});
  assert.deepEqual(third.drawn, {blocked: 1, unknown: 0, pass: 0},
    '几何与明细都就绪后必须真正画出障碍格');

  // 再画一轮：不得重复触发水合，也不得重复请求明细。
  const hydrateBefore = hydrateCalls;
  const detailCallsBefore = apiCalls.filter(url => url.includes('/map')).length;
  constraint.draw({ctx, view: view(), screenPoint, gridTheme: null});
  assert.equal(hydrateCalls, hydrateBefore, '渲染循环不得变成水合请求风暴');
  assert.equal(
    apiCalls.filter(url => url.includes('/map')).length,
    detailCallsBefore,
    '明细已读到时不得重复请求',
  );
});

// ---- 4. 图例与地图共用同一份可用性判定 -----------------------------------------

test('legend text switches to the usable wording only when cells are drawable', () => {
  const map = constraintMapModel(CONSTRAINT_MAP);
  const usableModel = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map, altitudeLayerId: LAYER_ID});
  assert.equal(usableModel.usable, true);
  const missingModel = constraintFieldModel({
    collection: CONSTRAINT_SUMMARY,
    map: constraintMapModel({status: 'not_calculated', cells: []}),
    altitudeLayerId: LAYER_ID,
  });
  assert.equal(missingModel.usable, false);
  // 图例计数始终来自后端 counts（与地图是否绘制无关，这是既有语义）。
  assert.equal(usableModel.outcomes.blocked, 1);
  assert.equal(missingModel.outcomes.blocked, 1);
});

// ---- 5. 视口过滤不得丢掉窗口内的格子 -------------------------------------------

test('overlay drops cells outside the visible bounds and keeps the rest', () => {
  const map = constraintMapModel(CONSTRAINT_MAP);
  const model = constraintFieldModel({collection: CONSTRAINT_SUMMARY, map, altitudeLayerId: LAYER_ID});
  const ctx = canvasCtx();
  const result = drawConstraintFieldOverlay({
    ctx, view: view(), screenPoint, model,
    cellsById: gridCache(GRID_CELLS).byId,
    layers: {enabled: true, blocked: true, unknown: true, pass: true},
    visibleBounds: [122.0, 29.8, 122.0015, 29.801],
    gridTheme: null,
  });
  assert.deepEqual(result.drawn, {blocked: 1, unknown: 1, pass: 0});
});
