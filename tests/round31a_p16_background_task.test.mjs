/**
 * Round 31-A：CNS 设施规划（P16）后台化的**前端契约**测试。
 *
 * 只断言两件事（不重复后端契约）：
 *  1. 「后台计算任务」窗口在任务出现时自动展开，且绝不覆盖用户的手动收起；
 *  2. P16 的业务按钮走后台任务入口（立刻返回 task_id），不再同步阻塞页面。
 */

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {createTaskCenter, writeTaskPanelUiState} from '../cns_planner/web/js/tasks.js';

const STEP05_SOURCE = new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url);
const MAIN_SOURCE = new URL('../cns_planner/web/js/main.js', import.meta.url);

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: key => map.delete(key),
  };
}

class FakeElement {
  constructor(id = '', document = null) {
    this.id = id;
    this.ownerDocument = document;
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.className = '';
    this.textContent = '';
  }
  set innerHTML(value) {
    this._innerHTML = value;
    if (this.id !== 'cnsTaskCenter') return;
    const doc = this.ownerDocument;
    for (const id of [
      'cnsTaskPanel', 'cnsTaskToggle', 'cnsTaskPanelContent',
      'cnsTaskErrorArea', 'cnsTaskList', 'cnsTaskPanelCount',
    ]) doc.nodes.set(id, new FakeElement(id, doc));
    const head = new FakeElement('', doc);
    head.className = 'cns-task-panel-head';
    doc.head = head;
  }
  get innerHTML() { return this._innerHTML || ''; }
  querySelector(selector) { return selector === '.cns-task-panel-head' ? this.ownerDocument.head : null; }
  getBoundingClientRect() { return {left: 700, top: 500, width: 300, height: 200}; }
  setPointerCapture(id) { this.captured = id; }
  releasePointerCapture(id) { this.released = id; }
}

function fakeDocument() {
  const nodes = new Map();
  const listeners = {};
  const view = {
    innerWidth: 1000, innerHeight: 700,
    addEventListener: (name, fn) => { listeners[name] = fn; },
    removeEventListener: name => { delete listeners[name]; },
  };
  const doc = {
    nodes, defaultView: view,
    body: {appendChild(node) { nodes.set(node.id, node); }},
    createElement() { return new FakeElement('', doc); },
    getElementById: id => nodes.get(id) || null,
    querySelectorAll() { return []; },
  };
  doc.listeners = listeners;
  return doc;
}

/** 一条"正在计算"的 P16 后台任务（字段与后端 /api/tasks 的业务视图一致）。 */
function runningTask(taskId = 'task-1') {
  return {
    task_id: taskId,
    task_type: 'cns_corridor_site_plan_evaluate',
    task_name: 'CNS 设施规划',
    status_text: '正在计算',
    message: '正在评估已有铁塔共址方案（已选择 1 个方案）',
    progress: 0.42,
    created_at: '2026-10-06T00:00:00Z',
    started_at: '2026-10-06T00:00:01Z',
    heartbeat_at: '2026-10-06T00:00:05Z',
    heartbeat_age_seconds: 1,
    can_cancel: true,
    result_scope: 'CNS 设施规划',
    advanced: {
      status: 'running', task_id: taskId,
      task_type: 'cns_corridor_site_plan_evaluate',
    },
  };
}

function runtime(payloadRef, storage = memoryStorage()) {
  const document = fakeDocument();
  const request = async url => {
    const path = String(url);
    if (path === '/api/state') return {};
    if (path.endsWith('/catalog')) return {items: []};
    return payloadRef.value;
  };
  return {document, storage, center: createTaskCenter({storage, document, fetch: request})};
}

test('任务出现时「后台计算任务」窗口自动展开，并显示业务名与进度', async () => {
  const payloadRef = {value: [runningTask()]};
  const {document, center} = runtime(payloadRef);
  await center.refresh();
  const content = document.getElementById('cnsTaskPanelContent');
  assert.equal(content.hidden, false, '有活跃任务时必须自动展开，用户不该自己去角落找进度');
  assert.equal(document.getElementById('cnsTaskPanelCount').textContent, '1');
  assert.equal(document.getElementById('cnsTaskPanelCount').dataset.taskCount, '1');
});

test('刷新页面（新会话）时若任务仍在运行，同样自动展开', async () => {
  // 用户上一次把面板收起了：偏好被持久化。
  const storage = memoryStorage();
  writeTaskPanelUiState({collapsed: true, x: null, y: null}, storage);
  const payloadRef = {value: [runningTask()]};
  const {document, center} = runtime(payloadRef, storage);
  await center.refresh();
  assert.equal(
    document.getElementById('cnsTaskPanelContent').hidden, false,
    '刷新后必须能看到正在进行的任务（本轮 UX 要求）',
  );
});

test('用户手动收起后，轮询不会重新撑开；只有新任务出现才再次自动展开', async () => {
  const payloadRef = {value: [runningTask('task-1')]};
  const {document, center} = runtime(payloadRef);
  await center.refresh();
  assert.equal(document.getElementById('cnsTaskPanelContent').hidden, false);

  document.getElementById('cnsTaskToggle').onclick();   // 用户手动收起
  assert.equal(document.getElementById('cnsTaskPanelContent').hidden, true);

  await center.refresh();                               // 同一个任务仍在计算
  assert.equal(
    document.getElementById('cnsTaskPanelContent').hidden, true,
    '轮询不得覆盖用户的手动收起',
  );

  payloadRef.value = [runningTask('task-2'), runningTask('task-1')];
  await center.refresh();                               // 新任务出现
  assert.equal(
    document.getElementById('cnsTaskPanelContent').hidden, false,
    '新提交的后台任务必须自动展开',
  );
});

test('没有任务时面板保持自动收起（既有 Round 2.8 契约不变）', async () => {
  const payloadRef = {value: []};
  const {document, center} = runtime(payloadRef);
  await center.refresh();
  const content = document.getElementById('cnsTaskPanelContent');
  assert.equal(content.hidden, true);
  assert.equal(content.dataset.autoCollapsed, 'true');
  assert.equal(document.getElementById('cnsTaskPanelCount').textContent, '0');
});

test('P16 业务按钮改走后台任务入口，不再同步阻塞页面', () => {
  const source = readFileSync(STEP05_SOURCE, 'utf8');
  assert.match(
    source,
    /submitBackgroundTask\('\/api\/cns-corridor-site-plan\/evaluate'/,
    'P16 必须经 submitBackgroundTask 提交（立即返回 task_id）',
  );
  assert.doesNotMatch(
    source,
    /actionButton\('evaluateCorridorSitePlan'/,
    'P16 不得再走通用的同步 actionButton 路径',
  );
  assert.match(source, /后台计算中/, '按钮必须进入「后台计算中」而不是假死');
});

test('壳层把既有任务框架接到业务页面（不新建第二套任务框架）', () => {
  const source = readFileSync(MAIN_SOURCE, 'utf8');
  assert.match(source, /import \{setupTaskCenter, submitHeavyTask\} from '\.\/tasks\.js'/);
  assert.match(
    source,
    /submitBackgroundTask:\(path,payload\)=>submitHeavyTask\(/,
    '业务页面的后台入口必须复用 tasks.js 的 submitHeavyTask',
  );
});
