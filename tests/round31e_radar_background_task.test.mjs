/**
 * Round 31-E：Radar 监视规划后台化的**前端契约**测试。
 *
 * 只断言本轮真正改变的两件事（不重复 tasks.js 的窗口 / 心跳 / 取消测试）：
 *  1. 正式路径经后台任务入口提交，演示预览保持既有同步入口；
 *  2. 任务完成后自动刷新正式结果**并**按需取回 Radar 逐点明细（否则地图停在上一版）。
 */

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const STEP05_SOURCE = new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url);
const MAIN_SOURCE = new URL('../cns_planner/web/js/main.js', import.meta.url);
const RADAR_PANEL_SOURCE = new URL(
  '../cns_planner/web/js/workflow/radar_surveillance_layout.js', import.meta.url,
);

test('Radar 正式路径改走后台任务入口，演示预览保持同步', () => {
  const source = readFileSync(STEP05_SOURCE, 'utf8');
  assert.match(
    source,
    /submitBackgroundTask\(RADAR_LAYOUT_EVALUATE_ENDPOINT,payload\)/,
    'Radar 正式路径必须经 submitBackgroundTask 提交（立即返回 task_id）',
  );
  assert.doesNotMatch(
    source,
    /actionButton\('evaluateRadarSurveillanceLayout'/,
    'Radar 不得再走通用的同步 actionButton 路径',
  );
  assert.match(source, /后台计算中/, '按钮必须进入「后台计算中」而不是假死');
  assert.match(
    source,
    /payload\.demo_preview_only/,
    '演示预览必须保留显式分支（后台 worker 无法复现运行时注入）',
  );
  const panelSource = readFileSync(RADAR_PANEL_SOURCE, 'utf8');
  assert.match(
    panelSource,
    /id="evaluateRadarSurveillanceLayout"/,
    '「运行雷达监视规划」按钮本身必须保留（只改提交方式）',
  );
});

test('任务完成后自动刷新 Radar 正式结果与逐点明细', () => {
  const source = readFileSync(MAIN_SOURCE, 'utf8');
  assert.match(source, /refreshWorkflow:async\(\)=>\{/);
  assert.match(
    source,
    /hydrateRadarSurveillanceDetail\(\)/,
    '后台任务发布后必须取回 Radar 明细，否则地图/明细停在上一版',
  );
});
