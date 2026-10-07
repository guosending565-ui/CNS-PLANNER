/**
 * Round32-K2（BUG-TASK-REQUEST-DOUBLE-001）：底层任务请求必须 **exactly once**。
 *
 * 现场：`ensureFetchContract()` 曾先 `await fetchImpl(path, options)` 判断返回类型，
 * 发现不是 fetch 响应后再交给 adapter，而 adapter 内部又 `await request(...)` 一次。
 * `main.js` 注入的是 `request:(path,options)=>api(path,options)`（成功直接返回解析后的
 * JSON），于是**每一次任务请求都会真的发出两次**：GET 只是浪费，POST `/api/tasks`
 * 会让一次用户点击提交两个后台任务（Radar / P15 / P16 等长任务会被真实重复提交）。
 *
 * 这里锁定"每条路径恰好一次"，覆盖两种注入形态：
 *  A. API 客户端（parsed JSON）+ `createTaskCenter.refresh()`；
 *  B. API 客户端（parsed JSON）+ `submitTaskType()`（最重要：绝不能一次点击提交两个任务）；
 *  C. fetch 契约响应（原生 fetch / 替身）——K 已修的恢复路径必须同时保持 exactly once。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createTaskCenter, setSessionToken, writeStoredTaskIds,
} from '../cns_planner/web/js/tasks.js';

/**
 * 极简任务面板宿主：只实现 `mount()` 真正用到的那几个 DOM 入口，
 * 并让 `.cns-task-start` 返回由 markup 里 `data-task-type` 解析出的按钮，
 * 以便测试走"真实按钮 → submitTaskType(..., 适配后的 fetchImpl)"这条装配路径。
 */
function documentWithTaskStarters() {
  const nodes = new Map();
  const starters = [];
  const host = {
    id: '', className: '', style: {}, dataset: {}, hidden: false, _markup: '',
    set innerHTML(value) {
      this._markup = String(value);
      starters.length = 0;
      for (const match of this._markup.matchAll(/data-task-type="([^"]+)"/g)) {
        starters.push({dataset: {taskType: match[1]}, textContent: '', disabled: false, onclick: null});
      }
    },
    get innerHTML() { return this._markup; },
    querySelector() { return null; },
    getBoundingClientRect() { return {left: 0, top: 0, width: 300, height: 200}; },
  };
  const documentRef = {
    defaultView: {innerWidth: 1000, innerHeight: 700, addEventListener() {}, removeEventListener() {}},
    body: {appendChild(node) { nodes.set(node.id || 'cnsTaskCenter', node); }, contains() { return true; }},
    createElement() { return host; },
    getElementById(id) { return nodes.get(id) || null; },
    querySelectorAll(selector) { return selector === '.cns-task-start' ? starters : []; },
  };
  return {document: documentRef, starters};
}

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
  };
}

function taskRecord(taskId, status) {
  return {
    task_id: taskId,
    task_type: 'runtime_probe',
    task_name: '运行时探针',
    status_text: status,
    message: status,
    progress: 0.4,
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

/** 计数型 API 客户端：成功返回 parsed JSON（与 `main.js` 注入的 `api()` 同契约）。 */
function countingApiClient(handler) {
  const calls = [];
  const client = async (path, options = {}) => {
    calls.push({path: String(path), method: String(options.method || 'GET').toUpperCase()});
    return handler(String(path), options);
  };
  client.calls = calls;
  client.count = (path) => calls.filter((call) => call.path === path).length;
  return client;
}

test('A. API 客户端 + refresh：每个任务端点只请求一次', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  writeStoredTaskIds(['task-once'], storage);
  const client = countingApiClient((path) => {
    if (path.endsWith('/catalog')) return {items: []};
    return [taskRecord('task-once', 'running')];
  });
  const center = createTaskCenter({fetch: client, storage, document: null, interval: 1500});

  const tasks = await center.refresh();

  assert.equal(client.count('/api/tasks'), 1, '任务列表必须恰好请求一次');
  assert.equal(client.count('/api/tasks/catalog'), 1, '任务目录必须恰好请求一次');
  assert.equal(client.calls.length, 2, 'refresh 只允许这两个端点各一次');
  assert.deepEqual(tasks.map((item) => item.task_id), ['task-once'],
    'API 客户端返回的 parsed JSON 必须被正常解析成任务列表');
});

test('B. API 客户端 + 面板提交按钮：一次点击只提交一次任务（POST exactly once）', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  const posts = [];
  const client = async (path, options = {}) => {
    const method = String(options.method || 'GET').toUpperCase();
    if (method === 'POST') posts.push(String(path));
    if (String(path).endsWith('/catalog')) {
      return {items: [{task_type: 'cns_service_corridor_evaluate', task_name: '服务走廊评估'}]};
    }
    if (method === 'POST') return {task_id: 'task-one'};
    return [];
  };
  const doc = documentWithTaskStarters();
  const center = createTaskCenter({fetch: client, storage, document: doc.document, interval: 1500});

  // 先按真实装配轮询一次（catalog 就绪后面板才会渲染提交按钮）。
  await center.refresh();
  assert.equal(doc.starters.length, 1, '面板必须渲染出真实的任务提交按钮');
  // 真实用户动作：点击「运行服务走廊评估」——内部走 submitTaskType(..., fetchImpl)，
  // fetchImpl 就是 main.js 装配时经 ensureFetchContract 适配后的实现。
  await doc.starters[0].onclick();

  assert.equal(posts.length, 1, '一次用户点击绝不能提交两个后台任务');
  assert.equal(posts[0], '/api/tasks');
});

test('C. fetch 契约响应路径同样 exactly once', async () => {
  setSessionToken('session-token');
  const storage = memoryStorage();
  writeStoredTaskIds(['task-once'], storage);
  const calls = [];
  const fakeFetch = async (path) => {
    calls.push(String(path));
    if (String(path).endsWith('/catalog')) return {ok: true, json: async () => ({items: []})};
    return {ok: true, json: async () => [taskRecord('task-once', 'running')]};
  };
  const center = createTaskCenter({fetch: fakeFetch, storage, document: null, interval: 1500});

  const tasks = await center.refresh();

  assert.equal(calls.filter((path) => path === '/api/tasks').length, 1);
  assert.equal(calls.length, 2, 'fetch 契约响应必须原样透传，不得被再次包装/重放');
  assert.deepEqual(tasks.map((item) => item.task_id), ['task-once']);
});
