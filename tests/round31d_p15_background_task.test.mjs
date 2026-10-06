/**
 * Round 31-D：CNS 能力缺口评估（P15）后台化的**前端契约**测试。
 *
 * 只断言业务页面把 P15 交给既有后台任务入口（不重复后端契约，也不重复
 * tasks.js 的面板 / 心跳 / 已运行时间测试：那些是 Round 2.8 与 31-A 的既有覆盖）。
 */

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const STEP05_SOURCE = new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url);

test('P15 业务按钮改走后台任务入口，不再同步阻塞页面', () => {
  const source = readFileSync(STEP05_SOURCE, 'utf8');
  assert.match(
    source,
    /submitBackgroundTask\('\/api\/cns-corridor-gap\/evaluate'/,
    'P15 必须经 submitBackgroundTask 提交（立即返回 task_id）',
  );
  assert.doesNotMatch(
    source,
    /actionButton\('evaluateCorridorGap'/,
    'P15 不得再走通用的同步 actionButton 路径',
  );
  assert.match(source, /后台计算中/, '按钮必须进入「后台计算中」而不是假死');
  assert.match(
    source,
    /id="evaluateCorridorGap">评估能力缺口</,
    '「评估能力缺口」按钮本身必须保留（只改提交方式）',
  );
});

test('P15 的进度与结果刷新复用既有「后台计算任务」窗口', () => {
  const source = readFileSync(STEP05_SOURCE, 'utf8');
  assert.match(
    source,
    /「后台计算任务」窗口/,
    '用户被告知去既有任务窗口查看进度（不新建第二套任务框架）',
  );
  // 前端不得自己伪造进度：P15 没有可量化内部循环时只依赖后端阶段与心跳。
  assert.doesNotMatch(source, /setInterval\(\s*\(\)\s*=>\s*\{[^}]*cns-corridor-gap/s);
});
