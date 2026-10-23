/**
 * R6-C：P16-V2（工程假设，只读）前端的契约测试。
 *
 * 只断言"界面如实反映后端"：
 *  1. 版本开关默认 V1，V2 只有显式选择才提交；
 *  2. 阶段 / 已完成 / 总量 / ETA / 取消全部来自任务记录，前端不自造进度；
 *  3. 结果面板展示真实耗时、缓存命中与"工程假设、不写正式方案"；
 *  4. 二维地图只画有坐标的选中设施与未满足体元。
 */

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {
  P16_V2_STAGES, p16V2BlockHtml, p16V2ProvenanceRows, p16V2ResultHtml, p16V2StageRows,
  p16V2TaskCardHtml,
} from '../cns_planner/web/js/workflow/step05_cns.js';
import {drawP16V2Overlay, p16V2OverlayModel} from '../cns_planner/web/js/map/p16_v2_overlay.js';

const STEP05_SOURCE = new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url);
const MAIN_SOURCE = new URL('../cns_planner/web/js/main.js', import.meta.url);

function runningView(overrides = {}) {
  const task = {
    task_id: 'task-abc',
    status_text: '正在计算',
    message: '正在搜索候选组合',
    progress: 0.72,
    heartbeat_age_seconds: 3,
    can_cancel: true,
    advanced: {status: 'running', task_type: 'cns_corridor_site_plan_evaluate_v2', input_fingerprint: 'fp'},
    progress_detail: {
      stage: 'search', stage_label: '候选组合搜索',
      completed: 4, total: 13, unit: '已选动作',
    },
    ...overrides,
  };
  return {task, result: null, etaText: '预计剩余：估算中', elapsedText: '已运行 12 秒'};
}

function sampleResult(overrides = {}) {
  return {
    status: 'computed',
    version: 'p16-v2',
    assumption_only: true,
    formal_write: false,
    feasible: true,
    inputs: {cap: 13},
    selected_actions: [
      {action_id: 'tower_colocation_host:EA-TWR-1:COMM-BASELINE-4KM', subsystem: 'C',
       reuse_class: 'tower_colocation_host', name: 'T1', site_id: 'EA-TWR-1',
       coordinate: [122.1, 30.1]},
    ],
    selected_sites: [{distinct_site_id: 'EA-TWR-1', coordinate: [122.1, 30.1]}],
    coverage: {
      discrete: {
        C: {satisfied: 2727, confirmed_gap: 0, unknown: 0},
        S: {satisfied: 2727, confirmed_gap: 0, unknown: 0},
      },
      continuous: {
        threshold_m: 45, C_max_continuous_deficit_m: 0, S_max_continuous_deficit_m: 0,
        acceptable: true,
      },
    },
    timings: {total_s: 31.611, baseline_s: 3.9, matrix_s: 14.353, search_s: 1.25, exact_s: 3.909},
    matrix: {hit: false, shape: [1296, 5454], bytes: 1000},
    exact: {reruns: 2, conservative_satisfied_voxel_subsystems: 5454},
    unmet_voxels: [],
    unmet_truncated: false,
    ...overrides,
  };
}

test('V2 阶段表与后端执行阶段一致', () => {
  assert.deepEqual(
    P16_V2_STAGES.map(item => item.key),
    ['pool', 'preflight', 'baseline', 'coverage_matrix', 'search', 'exact_eval', 'result'],
  );
});

test('阶段行严格由后端 progress_detail.stage 决定（不自造进度）', () => {
  const html = p16V2StageRows(runningView());
  const done = (html.match(/data-stage-state="done"/g) || []).length;
  const active = (html.match(/data-stage-state="active"/g) || []).length;
  const pending = (html.match(/data-stage-state="pending"/g) || []).length;
  assert.equal(done, 4, 'search 之前的四个阶段已完成');
  assert.equal(active, 1);
  assert.equal(pending, 2);
  assert.match(html, /4 \/ 13 已选动作/, '已完成/总量来自后端 detail');
});

test('任务卡片显示任务标识、阶段、心跳、ETA 与取消入口', () => {
  const html = p16V2TaskCardHtml(runningView());
  assert.match(html, /task-abc/);
  assert.match(html, /预计剩余：估算中/);
  assert.match(html, /已运行 12 秒/);
  assert.match(html, /最近心跳 刚刚/);
  assert.match(html, /data-p16v2-cancel="task-abc"/);
  //: 失败原因必须可见，且不是技术栈。
  const failed = p16V2TaskCardHtml(runningView({
    status_text: '输入已变化，请重新运行',
    advanced: {status: 'stale', task_type: 'cns_corridor_site_plan_evaluate_v2'},
    business_error: {text: '上游规划结论已变化，请先重算上游结果', code: 'task_p16_upstream_not_current'},
  }));
  assert.match(failed, /上游规划结论已变化/);
});

test('没有任务时 V2 区块仍可提交，且说明只读边界', () => {
  const html = p16V2BlockHtml(null);
  assert.match(html, /id="evaluateCorridorSitePlanV2"/);
  assert.match(html, /不写 ProjectState/);
  assert.match(html, /默认版本仍是 V1/);
  assert.match(html, /尚未提交 V2 预算/);
});

test('结果面板展示真实覆盖、真实耗时与缓存状态', () => {
  const html = p16V2ResultHtml(sampleResult());
  assert.match(html, /工程假设：C\/S 全部满足/);
  assert.match(html, /2727/);
  assert.match(html, /31\.61/);
  assert.match(html, /14\.35/);
  assert.match(html, /冷缓存构建/);
  assert.match(html, /不写入 ProjectState/);
  //: 未满足体元被截断时必须如实说明。
  assert.match(
    p16V2ResultHtml(sampleResult({feasible: false, unmet_truncated: true})),
    /地图只显示前 500 个/,
  );
});

test('结果详情展示来源与指纹，且与任务记录的 provenance 一一对应', () => {
  const result = sampleResult({
    near_identities: [{left: 'EA-S-1', right: 'EA-S-2', distance_m: 1.116}],
    near_identity_distance_m: 2,
    sources: {
      terrain_dtm: {path: 'dem.tif', sha256: 'a'.repeat(64)},
      land_mask: {path: 'mask.gpkg', sha256: 'b'.repeat(64)},
    },
    provenance: {
      input_fingerprint: 'f'.repeat(64),
      algorithm_id: 'fast_search_v2',
      algorithm_version: '1.1',
      canonical_write: false,
    },
  });
  const html = p16V2ResultHtml(result);
  assert.match(html, /data-p16v2-near="1"/);
  assert.match(html, /data-p16v2-provenance="1"/);
  assert.match(html, /1 组（阈值 2 m）/);
  assert.match(html, /EA-S-1 ↔ EA-S-2/);
  assert.match(html, /aaaaaaaaaaaa…/);
  assert.match(html, /bbbbbbbbbbbb…/);
  assert.match(html, /ffffffffffff…/);
  assert.match(html, /fast_search_v2 \/ 1\.1/);
  //: 字段缺失时如实显示，不编造来源。
  const empty = p16V2ProvenanceRows({status: 'computed'});
  assert.match(empty, /未发现近邻站址/);
  assert.match(empty, /未绑定/);
});

test('V2 默认不提交，提交时显式声明 v2 版本', () => {
  const source = readFileSync(STEP05_SOURCE, 'utf8');
  //: 默认版本是 V1；开关状态由模块级 p16PlanVersion 决定，工作台重绘不会把 V2 藏掉。
  assert.ok(source.includes("let p16PlanVersion='v1'"));
  assert.ok(source.includes("p16PlanVersion==='v2'?'':' selected'"));
  assert.ok(source.includes("p16PlanVersion==='v2'?' selected':''"));
  assert.ok(source.includes("p16PlanVersion==='v2'?'':' hidden'"));
  assert.ok(source.includes("p16PlanVersion=v2?'v2':'v1'"));
  assert.match(source, /id="p16v1Block"/);
  assert.match(source, /\/api\/cns-corridor-site-plan\/v2\/evaluate/);
  assert.match(source, /version:'v2'/);
  //: V1 的正式入口必须原样保留。
  assert.match(source, /id="evaluateCorridorSitePlan"/);
  assert.match(source, /\/api\/cns-corridor-site-plan\/evaluate/);
});

test('取消复用统一任务端点，不新建第二套取消逻辑', () => {
  const main = readFileSync(MAIN_SOURCE, 'utf8');
  assert.match(main, /cancelBackgroundTask:taskId=>api\('\/api\/tasks\/cancel'/);
  assert.match(readFileSync(STEP05_SOURCE, 'utf8'), /c\.cancelBackgroundTask\(taskId\)/);
});

test('二维叠加只画有坐标的设施与未满足体元', () => {
  const model = p16V2OverlayModel({
    selected_actions: [
      {action_id: 'a', subsystem: 'C', distinct_site_id: 's1', coordinate: [122, 30]},
      {action_id: 'b', subsystem: 'S', distinct_site_id: 's1', coordinate: [122, 30]},
      {action_id: 'c', subsystem: 'C', distinct_site_id: 's2', coordinate: null},
    ],
    unmet_voxels: [{coordinate: [121.5, 31.5], subsystem: 'S', status: 'confirmed_gap'}],
  });
  assert.equal(model.drawable, true);
  assert.equal(model.sites.length, 1, '同一站址的 C/S 动作合并为一个设施');
  assert.deepEqual(model.sites[0].subsystems, ['C', 'S']);
  assert.equal(model.unmet.length, 1);
});

test('绘制入口按屏幕坐标逐点绘制并统计跳过数', () => {
  const calls = [];
  const ctx = {
    save() {}, restore() {}, beginPath() {}, arc() { calls.push('arc'); },
    stroke() {}, fill() {}, setLineDash() {}, moveTo() {}, lineTo() {},
    fillText() {}, set lineWidth(_) {}, set strokeStyle(_) {}, set fillStyle(_) {},
    set font(_) {},
  };
  const outcome = drawP16V2Overlay({
    ctx, view: {}, screenPoint: coordinate => [coordinate[0] * 10, coordinate[1] * 10],
    result: {
      selected_actions: [
        {action_id: 'a', subsystem: 'C', segment: 'x', coordinate: [122, 30]},
        {action_id: 'b', subsystem: 'S', coordinate: [999, 999]},
      ],
      unmet_voxels: [{coordinate: [121, 31], subsystem: 'S', status: 'unknown'}],
    },
  });
  assert.equal(outcome.drawn, 1, '只有有效坐标的设施被绘制');
  //: 越界坐标在模型规范化阶段就被剔除，因此绘制阶段没有额外跳过。
  assert.equal(outcome.skipped, 0);
  assert.ok(calls.length >= 1);
});

test('壳层把任务事件投影为 V2 视图，且只在有结果时叠加地图', () => {
  const main = readFileSync(MAIN_SOURCE, 'utf8');
  assert.match(main, /addEventListener\('cns:tasks'/);
  assert.match(main, /cns_corridor_site_plan_evaluate_v2/);
  assert.match(main, /if\(p16V2View\.result\)drawP16V2Overlay/);
  //: V2 的结果不得被当成正式结果写回 workflow 快照。
  assert.doesNotMatch(main, /setFlow\([^)]*p16V2/);
});
