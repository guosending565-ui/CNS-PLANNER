/**
 * BUG-GRID-POPUP-001 前端回归测试。
 *
 * 现象：右侧 Step02 显示「人口映射：通过 / 5510/5510」，但点击任意 L8 格 popup 都显示
 * 「人口：缺少数据」。
 *
 * 根因（已复现）：`WorkflowService.snapshot()` 为性能做 `slim_grid_attributes()`，删掉
 * `grid_attributes.*.cells` 只留摘要 + `detail_available`；而 `main.js` 有**两套** flow
 * 写入方式 —— `applyWorkflow()` 之后会调 `syncGridApis()` 重新 hydrate 逐 cell 明细，
 * `resourceAction()` 却直接把 slim snapshot 赋给 flow。于是任何走 `resourceAction`
 * 的操作之后，`flow.grid_attributes.population.cells` 变成 undefined，
 * `buildGridOverlayCache()` 拿不到人口 cell，popup 就显示 missing_data。
 *
 * 本测试锁定：
 *  1. 统一的 snapshot 应用路径必须 hydrate 逐 cell 明细；
 *  2. 通用 /api/workflow 快照继续是 slim 的（性能优化不得回退）；
 *  3. count=0 / confirmed_zero 必须显示真实 0，而不是 missing；
 *  4. mapping passed 但某个 grid_id 缺失时**不得静默**；
 *  5. 两个交错的异步 grid 明细请求，旧响应不得覆盖新工作区。
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {buildGridOverlayCache} from '../cns_planner/web/js/map/grid_overlay.js';
import {
  gridCellSummary, gridCellSummaryHtml,
} from '../cns_planner/web/js/workflow/grid_details.js';
import {
  createWorkflowSnapshotApplier, populationCellStructureDiagnostics,
} from '../cns_planner/web/js/state/workflow_snapshot.js';

const readMain = () => readFileSync(
  new URL('../cns_planner/web/js/main.js', import.meta.url), 'utf8',
);

const formatNumber = value => (Number.isFinite(value) ? String(value) : '—');
const theme = {
  quantileBreaks: values => values,
  bboxContainsHalfOpen: (bbox, lon, lat) =>
    lon >= bbox[0] && lon < bbox[2] && lat >= bbox[1] && lat < bbox[3],
};

// ---- fixtures -------------------------------------------------------------

/** 通用 workflow 快照的 slim 投影（等价于 workflow_service.slim_grid_attributes）。 */
function slim(attributes) {
  const result = {};
  for (const [name, value] of Object.entries(attributes)) {
    const summary = {...value};
    delete summary.cells;
    summary.detail_available = Boolean(value && value.cells);
    result[name] = summary;
  }
  return result;
}

function grid(count = 3) {
  return {
    status: 'passed', level: 8,
    cells: Array.from({length: count}, (_, index) => ({
      grid_id: `L8-${String(index).padStart(6, '0')}`,
      level: 8,
      bbox: [122.0 + index * 0.001, 29.9, 122.001 + index * 0.001, 29.901],
      center: [122.0005 + index * 0.001, 29.9005],
    })),
  };
}

/** 逐 cell 明细：第 2 格是**已确认的零人口**（必须显示 0，不能是 missing）。 */
function detailedAttributes(count = 3) {
  const cells = {};
  for (let index = 0; index < count; index += 1) {
    cells[`L8-${String(index).padStart(6, '0')}`] = index === 1
      ? {
        population_count_people: 0, population_density_people_km2: 0,
        source_coverage_fraction: 1, coverage_status: 'confirmed_zero',
        value_status: 'passed', valid_sample_count: 4,
      }
      : {
        population_count_people: 100 + index, population_density_people_km2: 900 + index,
        source_coverage_fraction: 1, coverage_status: 'full',
        value_status: 'passed', valid_sample_count: 4,
      };
  }
  return {
    population: {status: 'passed', cells, full_cells: count, missing_cells: 0},
    terrain: {status: 'passed', cells: {}, elevation_unit: 'm', unit_status: 'source_declared'},
    buildings: {status: 'unsupported', cells: {}},
    airspace: {status: 'passed', cells: {}},
  };
}

function summarySnapshot() {
  const attributes = detailedAttributes();
  return {
    revision: 7, grid: grid(), grid_attributes: slim(attributes),
    radar_surveillance_layout: {status: 'passed', count: 1, items: []},
  };
}

function popup(snapshot, index = 0) {
  const cache = buildGridOverlayCache(
    snapshot.grid, snapshot.grid_attributes, {}, theme, null,
  );
  const lon = 122.0005 + index * 0.001;
  const lat = 29.9005;
  const item = cache.cells.find(entry => theme.bboxContainsHalfOpen(entry.cell.bbox, lon, lat));
  return {item, summary: gridCellSummary(item, snapshot, formatNumber, 'none')};
}

// ---- 依赖注入的 applier 装配 ------------------------------------------------

/**
 * 构造一个与 main.js 同构的 applier：`getFlow` / `setFlow` 指向局部变量，
 * `nextSerial` / `currentSerial` 模拟 gridDataSerial，fetch* 是可控的假接口。
 */
function harness({grids = [], attributes = []} = {}) {
  let flow = null;
  let serial = 0;
  const gridCalls = [];
  const attributeCalls = [];
  let gridIndex = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => { gridCalls.push('serial'); return ++serial; },
    currentSerial: () => serial,
    fetchGrid: async () => {
      gridCalls.push('grid');
      const value = grids[gridIndex] ?? grids[grids.length - 1];
      return value;
    },
    fetchAttributes: async () => {
      attributeCalls.push('attributes');
      const value = attributes[gridIndex] ?? attributes[attributes.length - 1];
      gridIndex += 1;
      return value;
    },
    onError: message => { throw new Error(message); },
    afterApply: () => {},
  });
  return {
    applier,
    gridCalls, attributeCalls,
    get flow() { return flow; },
    set flow(value) { flow = value; },
    get serial() { return serial; },
  };
}

// ---- 1. 统一路径必须 hydrate 逐 cell 明细 -----------------------------------

test('applying a slim workflow snapshot hydrates per-cell grid detail', async () => {
  const full = {grid: grid(), grid_attributes: detailedAttributes()};
  const h = harness({grids: [full.grid], attributes: [full.grid_attributes]});
  const snapshot = summarySnapshot();

  // 安装前：slim 快照里没有 cells。
  assert.equal(Boolean(snapshot.grid_attributes.population.cells), false);
  assert.equal(snapshot.grid_attributes.population.detail_available, true);

  await h.applier.applyWorkflowSnapshot(snapshot);

  // 安装后：逐 cell 明细必须回来 —— 这正是修复点。
  assert.equal(Object.keys(h.flow.grid_attributes.population.cells).length, 3);
  const {item, summary} = popup(h.flow);
  assert.equal(item.population.population_density_people_km2, 900);
  assert.match(summary, /人口：count 100 person/);
  assert.doesNotMatch(summary, /缺少数据|missing_data/);
  assert.deepEqual(h.attributeCalls, ['attributes']);
});

test('the slim snapshot never triggers a grid request when there is no grid', async () => {
  const h = harness();
  await h.applier.applyWorkflowSnapshot({revision: 1, grid: {status: 'not_calculated', cells: []}});
  assert.deepEqual(h.attributeCalls, []);
});

test('the general workflow snapshot stays slim (the performance optimisation is not reverted)', () => {
  const snapshot = summarySnapshot();
  assert.equal('cells' in snapshot.grid_attributes.population, false);
  assert.equal(snapshot.grid_attributes.population.detail_available, true);
  // 通用快照不得携带上万 cells
  assert.equal(JSON.stringify(snapshot).includes('population_density_people_km2'), false);
});

// ---- 2. resourceAction 与 applyWorkflow 必须走同一条路径 --------------------

test('main.js routes resourceAction through the same unified snapshot path', () => {
  const main = readMain();
  // resourceAction 不再直接 flow=data
  assert.doesNotMatch(main, /async function resourceAction\([^)]*\)\{const data=await api\([^)]*\);flow=data;/,
    'resourceAction 不得把 slim response 直接赋给 flow');
  assert.match(main, /async function resourceAction\([^)]*\)\{return applyWorkflowSnapshot\(await api\(/,
    'resourceAction 必须走 applyWorkflowSnapshot');
  assert.match(main, /createWorkflowSnapshotApplier\(\{/);
  assert.match(main, /from '\.\/state\/workflow_snapshot\.js'/);
  // 旧的 syncGridApis 函数体已被统一 hydrate 取代（不得留下第二套写入路径）。
  assert.doesNotMatch(main, /async function syncGridApis\(/,
    'flow 写入必须只有 applyWorkflowSnapshot 一条路径');
  assert.match(main, /remapPopulation:async\(\)=>[\s\S]{0,200}await applyWorkflowSnapshot\(data\)/);
  // 打开项目：先 hydrate 完整快照，再刷新 /api/state。
  assert.match(main, /openProject[\s\S]{0,600}await applyWorkflowSnapshot\(data\.workflow\)/);
  assert.match(main, /applyWorkflowSnapshot\(data\.workflow\)\.then/);
  // 入口文件必须保持精简（tests/test_architecture.py）。
  assert.ok(main.split('\n').length <= 450,
    `main.js 必须保持 450 行以内的轻入口（当前 ${main.split('\n').length} 行）`);
});

// ---- 3. 0 人口必须显示 0 ----------------------------------------------------

test('confirmed zero population renders as a real 0 and never as missing data', () => {
  const full = {grid: grid(), grid_attributes: detailedAttributes()};
  const {item} = popup(full, 1);
  assert.equal(item.population.population_count_people, 0);
  assert.equal(item.population.coverage_status, 'confirmed_zero');
  const summary = gridCellSummary(item, full, formatNumber, 'none');
  assert.match(summary, /人口：count 0 person · density 0 person\/km²/);
  assert.doesNotMatch(summary, /缺少数据|missing_data/);
  // HTML 变体也不得把 0 当成"无值"而隐藏人口行。
  const html = gridCellSummaryHtml(item, full, formatNumber, 'none');
  assert.match(html, /count 0 person/);
});

// ---- 4. 结构错误不得被静默成"缺少数据" --------------------------------------

test('a missing population cell while status=passed is reported, not silently degraded', () => {
  const broken = {grid: grid(), grid_attributes: detailedAttributes()};
  delete broken.grid_attributes.population.cells['L8-000002'];
  const diagnostics = populationCellStructureDiagnostics(broken.grid, broken.grid_attributes);
  assert.equal(diagnostics.length, 1);
  assert.equal(diagnostics[0].code, 'population_cell_missing_for_grid_id');
  assert.deepEqual(diagnostics[0].first_missing_grid_ids, ['L8-000002']);
  assert.equal(diagnostics[0].grid_count, 3);

  // population.cells 整体缺失但 status=passed：也要显式报告。
  const absent = {grid: grid(), grid_attributes: detailedAttributes()};
  delete absent.grid_attributes.population.cells;
  const absentDiagnostics = populationCellStructureDiagnostics(
    absent.grid, absent.grid_attributes,
  );
  assert.equal(absentDiagnostics[0].code, 'population_cells_absent_while_status_passed');

  // 完整的一一对应：没有任何诊断。
  const healthy = {grid: grid(), grid_attributes: detailedAttributes()};
  assert.deepEqual(populationCellStructureDiagnostics(healthy.grid, healthy.grid_attributes), []);
});

test('5510-cell synthetic fixture keeps grid ids and population keys one to one', () => {
  const count = 5510;
  const bigGrid = {
    status: 'passed', level: 8,
    cells: Array.from({length: count}, (_, index) => ({
      grid_id: `L8-${String(index).padStart(6, '0')}`, level: 8,
      bbox: [122.0 + index * 1e-5, 29.9, 122.0 + (index + 1) * 1e-5, 29.9001],
      center: [122.0 + index * 1e-5, 29.90005],
    })),
  };
  const attributes = {population: {status: 'passed', cells: {}}};
  for (const cell of bigGrid.cells) {
    attributes.population.cells[cell.grid_id] = {
      population_count_people: 0, population_density_people_km2: 0,
      source_coverage_fraction: 1, coverage_status: 'confirmed_zero', value_status: 'passed',
    };
  }
  const gridIds = new Set(bigGrid.cells.map(cell => cell.grid_id));
  const populationKeys = new Set(Object.keys(attributes.population.cells));
  assert.equal(gridIds.size, count);
  assert.equal(populationKeys.size, count);
  assert.equal([...gridIds].every(id => populationKeys.has(id)), true);
  assert.deepEqual(populationCellStructureDiagnostics(bigGrid, attributes), []);

  // 每个 grid_id 都能在 overlay cache 里找到 population 记录（0 也必须是**存在**的记录）。
  const cache = buildGridOverlayCache(bigGrid, attributes, {}, theme, null);
  assert.equal(cache.cells.length, count);
  assert.equal(cache.cells.every(entry => entry.population !== null), true);
  const sample = cache.cells[count - 1];
  assert.equal(sample.population.population_count_people, 0);
  assert.match(
    gridCellSummary(sample, {grid_attributes: attributes}, formatNumber, 'none'),
    /count 0 person/,
  );
});

// ---- 5. 异步竞态：旧响应不得覆盖新工作区 ------------------------------------

test('a superseded grid detail response never overwrites a newer workspace', async () => {
  const renamed = (prefix) => ({
    grid: {
      ...grid(),
      cells: grid().cells.map(cell => ({...cell, grid_id: `${prefix}-${cell.grid_id}`})),
    },
    grid_attributes: detailedAttributes(),
  });
  const workspaceA = renamed('A');
  const workspaceB = renamed('B');

  let flow = null;
  let serial = 0;
  //: 两个请求各自的 deferred：由测试显式控制 resolve 顺序。
  const pending = [];
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: () => new Promise(resolve => pending.push({kind: 'grid', resolve})),
    fetchAttributes: () => new Promise(resolve => pending.push({kind: 'attributes', resolve})),
    onError: message => { throw new Error(message); },
    afterApply: () => {},
  });

  const first = applier.applyWorkflowSnapshot(structuredClone(summarySnapshot()));
  await Promise.resolve();
  await Promise.resolve();
  const second = applier.applyWorkflowSnapshot(structuredClone(summarySnapshot()));
  await Promise.resolve();
  await Promise.resolve();

  // 4 个待决请求：前两个属于第一次（已过期），后两个属于第二次。
  assert.equal(pending.length, 4, `期望 4 个待决请求，实际 ${pending.length}`);
  const [aGrid, aAttributes, bGrid, bAttributes] = pending;
  // 先让**过期的**第一次返回（工作区 A）——它必须被丢弃。
  aGrid.resolve(workspaceA.grid);
  aAttributes.resolve(workspaceA.grid_attributes);
  await Promise.all([first]);
  assert.equal(flow.grid.cells[0].grid_id.startsWith('A-'), false,
    '过期响应在更新的请求存在时绝不得安装（flow 必须仍是未 hydrate 的 slim 快照）');
  assert.equal(Boolean(flow.grid_attributes.population.cells), false,
    '过期响应不得把工作区 A 的逐 cell 明细装进 flow');

  // 再让第二次返回（工作区 B）——这才是最终状态。
  bGrid.resolve(workspaceB.grid);
  bAttributes.resolve(workspaceB.grid_attributes);
  await Promise.all([second]);

  assert.equal(Object.keys(flow.grid_attributes.population.cells).length, 3);
  assert.equal(flow.grid.cells[0].grid_id.startsWith('B-'), true,
    '最终工作区必须是较新的 B，而不是迟到的 A');
});
