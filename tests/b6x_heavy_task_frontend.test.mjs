/**
 * Phase4-B6X：持久重任务前端契约测试。
 *
 * 只验证"最小通用任务 UI"的对外契约：
 *  - 主界面只出现中文业务文案（正在排队 / 正在计算 / 进度 / 最近心跳 / 取消任务 /
 *    已完成 / 已取消 / 执行失败 / 输入已变化，请重新运行）；
 *  - raw 枚举（queued / running / stale）与 task_id 只允许出现在「高级 / 审计信息」；
 *  - 刷新页面后按 task_id 恢复状态（localStorage 只存标识，状态来自后端）；
 *  - 关闭页面不触发取消；
 *  - 业务端点异步提交走 `async: true`，提交后记住 task_id。
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  TASK_ADVANCED_STATUSES, TASK_STATUS_TEXT, advancedHtml, cancelHeavyTask,
  createTaskCenter, heartbeatText, isActiveStatus, loadTasks, panelHtml,
  progressText, readStoredTaskIds, rememberTaskId, setSessionToken, submitHeavyTask,
  submitTaskType, taskRowHtml, taskStatusText, writeStoredTaskIds,
} from '../cns_planner/web/js/tasks.js';

const HTML = readFileSync(new URL('../cns_planner/web/index.html', import.meta.url), 'utf8');
const TASKS_JS = readFileSync(new URL('../cns_planner/web/js/tasks.js', import.meta.url), 'utf8');
const TASKS_CSS = readFileSync(new URL('../cns_planner/web/css/tasks.css', import.meta.url), 'utf8');

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
  };
}

function taskFixture(overrides = {}) {
  return {
    task_id: 'task-0123456789abcdef',
    task_type: 'cns_service_corridor_evaluate',
    task_name: '服务走廊评估',
    status_text: '正在计算',
    message: '正在计算服务走廊（体素探测与服务能力判定）',
    progress: 0.42,
    started_at: '2026-01-01T00:00:00+00:00',
    finished_at: null,
    heartbeat_at: '2026-01-01T00:00:05+00:00',
    heartbeat_age_seconds: 3,
    cancel_requested: false,
    can_cancel: true,
    scope_id: 'cns_service_corridor:R1',
    result_scope: '服务走廊评估',
    result_summary: {},
    release: {},
    business_error: null,
    advanced: {
      status: 'running',
      task_id: 'task-0123456789abcdef',
      task_type: 'cns_service_corridor_evaluate',
      input_revision: 7,
      input_fingerprint: 'f'.repeat(64),
      input_snapshot_ref: 'project-state@cns_service_corridor_evaluate#ffffffffffffffff',
      result_artifact_ref: null,
      worker: {pid: 4242, kind: 'heavy-task-worker'},
      error: null,
      store_directory: 'projects/.cns-tasks',
      contract_fields: ['task_id', 'task_type', 'status'],
    },
    ...overrides,
  };
}

test('前端词表覆盖全部 B6X 业务文案且不裸露 raw 枚举', () => {
  assert.deepEqual(TASK_STATUS_TEXT, {
    queued: '正在排队', running: '正在计算', succeeded: '已完成', failed: '执行失败',
    cancelling: '正在取消', cancelled: '已取消', stale: '输入已变化，请重新运行',
  });
  for (const raw of TASK_ADVANCED_STATUSES) {
    assert.notEqual(taskStatusText(raw), raw, `raw 状态 ${raw} 不得作为主界面文案`);
  }
  assert.equal(taskStatusText('queued'), '正在排队');
  assert.equal(taskStatusText('running'), '正在计算');
  assert.equal(taskStatusText('succeeded'), '已完成');
  assert.equal(taskStatusText('cancelled'), '已取消');
  assert.equal(taskStatusText('failed'), '执行失败');
  assert.equal(taskStatusText('stale'), '输入已变化，请重新运行');
});

test('进度与心跳使用业务化展示', () => {
  assert.equal(progressText(0), '0%');
  assert.equal(progressText(0.426), '43%');
  assert.equal(progressText(1.5), '100%');
  assert.equal(progressText(null), '—');
  assert.equal(heartbeatText(1), '刚刚');
  assert.equal(heartbeatText(42), '42 秒前');
  assert.equal(heartbeatText(125), '2 分钟前');
  assert.equal(heartbeatText(-1), '—');
});

test('任务行主界面不出现 raw 枚举与 task_id，取消按钮仅在可取消时出现', () => {
  const visibleText = (html) => html.replace(/<[^>]*>/g, ' ');
  const running = taskRowHtml(taskFixture());
  assert.match(running, /正在计算/);
  assert.match(running, /进度 42%/);
  assert.match(running, /最近心跳 刚刚/);
  assert.match(running, /取消任务/);
  // 主界面**可见文本**不得出现 raw 枚举或 task_id（它们只出现在高级折叠区）。
  const runningMain = running.split('<details')[0];
  assert.doesNotMatch(visibleText(runningMain), /\bqueued\b|\brunning\b|\bstale\b/);
  assert.doesNotMatch(visibleText(runningMain), /task-0123456789abcdef/);

  const finished = taskRowHtml(taskFixture({
    status_text: '已完成', can_cancel: false, advanced: {...taskFixture().advanced, status: 'succeeded'},
  }));
  assert.doesNotMatch(finished, /取消任务/);
  assert.match(finished, /服务走廊评估已更新/);

  const stale = taskRowHtml(taskFixture({
    status_text: '输入已变化，请重新运行', can_cancel: false, progress: 0.9,
    advanced: {...taskFixture().advanced, status: 'stale', error: {code: 'task_input_changed', message: 'x'}},
  }));
  assert.match(stale, /输入已变化，请重新运行/);
  assert.match(stale, /当前正式结果未被覆盖/);
});

test('高级 / 审计信息里才出现 task_id、raw 状态与输入指纹', () => {
  const advanced = advancedHtml(taskFixture());
  assert.match(advanced, /高级 \/ 审计信息/);
  assert.match(advanced, /task-0123456789abcdef/);
  assert.match(advanced, /running/);
  assert.match(advanced, new RegExp('f'.repeat(64)));
  assert.match(advanced, /projects\/\.cns-tasks/);
});

test('面板提供通用任务入口且不暴露技术值', () => {
  const html = panelHtml([taskFixture()], [
    {task_type: 'cns_service_corridor_evaluate', task_name: '服务走廊评估'},
    {task_type: 'planning_constraint_field_generate', task_name: '规划约束场生成'},
    {task_type: 'runtime_probe', task_name: '运行时探针'},
  ]);
  assert.match(html, /后台计算任务/);
  assert.match(html, /运行服务走廊评估/);
  assert.match(html, /运行规划约束场生成/);
  assert.doesNotMatch(html, /运行运行时探针/);
  assert.match(html, /关闭页面不会取消任务/);
  const empty = panelHtml([], []);
  assert.match(empty, /当前没有后台计算任务/);
});

test('localStorage 只保存 task_id，用于刷新后恢复', () => {
  const storage = memoryStorage();
  assert.deepEqual(readStoredTaskIds(storage), []);
  rememberTaskId('task-a', storage);
  rememberTaskId('task-b', storage);
  rememberTaskId('task-a', storage);
  assert.deepEqual(readStoredTaskIds(storage), ['task-a', 'task-b']);
  writeStoredTaskIds([], storage);
  assert.deepEqual(readStoredTaskIds(storage), []);
  const broken = {getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); }};
  assert.deepEqual(readStoredTaskIds(broken), []);
});

test('刷新后按 task_id 恢复：终态历史任务仍显示，无关任务被过滤', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  rememberTaskId('task-keep', storage);
  const fetchImpl = async () => ({
    ok: true,
    json: async () => ([
      taskFixture({task_id: 'task-keep', status_text: '已取消', can_cancel: false,
        advanced: {...taskFixture().advanced, task_id: 'task-keep', status: 'cancelled'}}),
      taskFixture({task_id: 'task-active', status_text: '正在排队',
        advanced: {...taskFixture().advanced, task_id: 'task-active', status: 'queued'}}),
      taskFixture({task_id: 'task-other', status_text: '已完成', can_cancel: false,
        advanced: {...taskFixture().advanced, task_id: 'task-other', status: 'succeeded'}}),
    ]),
  });
  const center = createTaskCenter({fetch: fetchImpl, storage, document: null, interval: 60000});
  const tasks = await center.refresh();
  const ids = tasks.map((item) => item.task_id);
  assert.ok(ids.includes('task-keep'), '刷新后必须能恢复已记录任务的最终状态');
  assert.ok(ids.includes('task-active'), '活跃任务无论是否记录都要显示');
  assert.ok(!ids.includes('task-other'), '无关的已结束任务不进恢复列表');
});

test('业务端点异步提交：带 async 标记并记住 task_id', async () => {
  setSessionToken('session-token');
  const calls = [];
  const api = async (path, options) => {
    calls.push({path, options});
    return {task_id: 'task-xyz', created: true, task: {task_id: 'task-xyz'}};
  };
  const storage = memoryStorage();
  const original = globalThis.localStorage;
  globalThis.localStorage = storage;
  try {
    const response = await submitHeavyTask(api, '/api/cns-service-corridor/evaluate', {});
    assert.equal(response.task_id, 'task-xyz');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/api/cns-service-corridor/evaluate');
    assert.equal(JSON.parse(calls[0].options.body).async, true);
    assert.deepEqual(readStoredTaskIds(storage), ['task-xyz']);
  } finally {
    globalThis.localStorage = original;
  }
});

test('通用提交入口提交 task_type，失败时给出业务错误', async () => {
  setSessionToken('session-token');
  const calls = [];
  const fetchImpl = async (path, options) => {
    calls.push({path, options});
    return {ok: true, json: async () => ({task_id: 'task-generic'})};
  };
  const storage = memoryStorage();
  const original = globalThis.localStorage;
  globalThis.localStorage = storage;
  try {
    await submitTaskType('planning_constraint_field_generate', {}, fetchImpl);
    assert.equal(calls[0].path, '/api/tasks');
    assert.equal(JSON.parse(calls[0].options.body).task_type, 'planning_constraint_field_generate');
    assert.deepEqual(readStoredTaskIds(storage), ['task-generic']);
    const failing = async () => ({
      ok: false, json: async () => ({error: '无法提交该任务，请检查所需输入是否齐备', detail: {code: 'task_submit_failed'}}),
    });
    await assert.rejects(
      () => submitTaskType('planning_constraint_field_generate', {}, failing),
      (error) => error.message === '无法提交该任务，请检查所需输入是否齐备' && error.code === 'task_submit_failed',
    );
  } finally {
    globalThis.localStorage = original;
  }
});

test('取消走显式请求，列表读取使用 /api/tasks', async () => {
  const calls = [];
  const fetchImpl = async (path, options) => {
    calls.push({path, options});
    if (path === '/api/state') return {ok: true, json: async () => ({token: 'session-token'})};
    if (path.endsWith('/cancel')) return {ok: true, json: async () => ({message: '已请求取消，正在停止计算'})};
    return {ok: true, json: async () => ([taskFixture()])};
  };
  const cancelled = await cancelHeavyTask('task-1', fetchImpl);
  assert.equal(cancelled.message, '已请求取消，正在停止计算');
  const cancelCall = calls.find((call) => call.path === '/api/tasks/cancel');
  assert.ok(cancelCall, '取消必须走 POST /api/tasks/cancel');
  assert.equal(JSON.parse(cancelCall.options.body).task_id, 'task-1');
  // 会话令牌由后端在 /api/state 下发，任务端点必须带上它。
  assert.equal(cancelCall.options.headers['X-CNS-Token'], 'session-token');
  const tasks = await loadTasks(fetchImpl);
  assert.equal(tasks.length, 1);
  assert.ok(calls.some((call) => call.path === '/api/tasks' && call.options.headers['X-CNS-Token'] === 'session-token'));
});

test('活跃状态判定与业务语义一致', () => {
  assert.equal(isActiveStatus('queued'), true);
  assert.equal(isActiveStatus('running'), true);
  assert.equal(isActiveStatus('cancelling'), true);
  assert.equal(isActiveStatus('succeeded'), false);
  assert.equal(isActiveStatus('cancelled'), false);
  assert.equal(isActiveStatus('stale'), false);
});

test('页面入口与样式：独立模块加载、无 !important、关闭页面不发取消请求', () => {
  assert.match(HTML, /<link rel="stylesheet" href="\/css\/tasks\.css">/);
  assert.match(HTML, /<script type="module" src="\/js\/tasks\.js"><\/script>/);
  assert.doesNotMatch(TASKS_CSS, /!important/);
  // 页面卸载/隐藏时不触发取消：源码里不得出现 beforeunload/unload 之类的取消钩子。
  assert.doesNotMatch(TASKS_JS, /beforeunload|onunload|visibilitychange/);
});
