/**
 * Round32-K：后台任务"活跃 → 成功"跨越只刷新一次 / 恢复后仍能刷新。
 *
 * 锁定的行为（全部来自 `tasks.js` 的既有实现，不新增业务框架）：
 *  1. 状态从活跃跨越到 succeeded 时，`settledHandler` **恰好**被调用一次；
 *  2. 后续轮询（每 1.5 s）不会重复刷新；
 *  3. 页面刷新后（新的 task center，仅凭已记录的 task_id 恢复）仍能在成功时触发刷新；
 *  4. failed / cancelled / stale 绝不伪装成成功（不触发业务刷新）。
 *
 * 用内存 storage + 极小 fake fetch，不构造大型 fixture。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createTaskCenter, setSessionToken, writeStoredTaskIds,
} from '../cns_planner/web/js/tasks.js';

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
  };
}

function task(taskId, status, progress) {
  return {
    task_id: taskId,
    task_type: 'runtime_probe',
    task_name: '运行时探针',
    status_text: status,
    message: status,
    progress,
    started_at: '2026-01-01T00:00:00+00:00',
    finished_at: null,
    heartbeat_age_seconds: 1,
    cancel_requested: false,
    can_cancel: status === 'running',
    result_scope: '运行时探针',
    business_error: null,
    advanced: {status, task_id: taskId},
  };
}

/** 依次返回给定批次的任务列表（`/api/tasks` 每次 refresh 消费一批，最后一批持续复用）。 */
function sequencedFetch(batches) {
  let index = 0;
  return async (path) => {
    // 任务目录不参与任务列表序列：它每次都是空目录。
    if (String(path).includes('/catalog')) return {ok: true, json: async () => ({items: []})};
    const items = batches[Math.min(index, batches.length - 1)];
    index += 1;
    return {ok: true, json: async () => items};
  };
}

test('active → succeeded 只刷新一次，后续轮询不重复刷新', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  writeStoredTaskIds(['task-k1'], storage);
  const calls = [];
  const fetchImpl = sequencedFetch([
    [task('task-k1', 'queued', 0.0)],
    [task('task-k1', 'running', 0.4)],
    [task('task-k1', 'succeeded', 0.9)],
    [task('task-k1', 'succeeded', 1.0)],
    [task('task-k1', 'succeeded', 1.0)],
  ]);
  const center = createTaskCenter({
    fetch: fetchImpl, storage, document: null, interval: 1500,
    onTaskSettled: (payload) => { calls.push(payload); return Promise.resolve(true); },
  });

  await center.refresh();   // queued
  await center.refresh();   // running
  await center.refresh();   // succeeded ← 唯一一次跨越
  await center.refresh();   // succeeded（重复轮询）
  await center.refresh();   // succeeded（重复轮询）

  assert.equal(calls.length, 1, '跨越时必须且只刷新一次');
  assert.equal(calls[0].previous_status, 'running');
  assert.equal(calls[0].task.task_id, 'task-k1');
  assert.equal(String(calls[0].task.advanced.status), 'succeeded');
});

test('刷新页面后按已记录 task_id 恢复，仍能在成功时触发刷新', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  writeStoredTaskIds(['task-k2'], storage);
  // 页面刷新后：第一次轮询就看到 running（恢复），随后成功。
  const fetchImpl = sequencedFetch([
    [task('task-k2', 'running', 0.5)],
    [task('task-k2', 'succeeded', 0.9)],
    [task('task-k2', 'succeeded', 1.0)],
  ]);
  const calls = [];
  const center = createTaskCenter({
    fetch: fetchImpl, storage, document: null, interval: 1500,
    onTaskSettled: (payload) => { calls.push(payload); return null; },
  });

  const first = await center.refresh();
  assert.deepEqual(first.map((item) => item.task_id), ['task-k2'],
    '恢复必须能按 task_id 找回仍在运行的任务');
  await center.refresh();
  await center.refresh();

  assert.equal(calls.length, 1, '恢复后的成功跨越同样只刷新一次');
  assert.equal(calls[0].previous_status, 'running');
});

test('failed / cancelled / stale 绝不伪装成成功，也不触发业务刷新', async () => {
  setSessionToken('session-token');
  for (const status of ['failed', 'cancelled', 'stale']) {
    const storage = memoryStorage();
    writeStoredTaskIds([`task-${status}`], storage);
    const fetchImpl = sequencedFetch([
      [task(`task-${status}`, 'running', 0.9)],
      [task(`task-${status}`, status, 0.9)],
      [task(`task-${status}`, status, 0.9)],
    ]);
    const calls = [];
    const center = createTaskCenter({
      fetch: fetchImpl, storage, document: null, interval: 1500,
      onTaskSettled: (payload) => { calls.push(payload); return null; },
    });
    await center.refresh();
    await center.refresh();
    await center.refresh();
    assert.equal(calls.length, 0, `${status} 不得触发"结果已更新"刷新`);
  }
});
