/**
 * Bootstrap / restore 回归测试（BUG-PROJECT-RESTORE-001 / BUG-BOOTSTRAP-LOAD-001）。
 *
 * 覆盖的验收语义：
 *  1. server explicit project + F5 → 直接采用服务器项目，不重新 open；
 *  2. automatic server + cached explicit directory → 成功恢复一次；
 *  3. 恢复请求携带**当前会话**的合法 token / revision（从最新 /api/state 取得）；
 *  4. 恢复成功后使用 fresh state / workflow 重渲染；
 *  5. 恢复失败只尝试一次（不循环、不猜别的项目）；
 *  6. bootstrap 不因 restore pending / failure 卡死（loading 必须能收尾、页面可用）；
 *  7. 无缓存时保持 automatic（不发任何恢复请求）；
 *  8. server explicit project 永远优先于缓存。
 *
 * 这里只用真实的 createApiClient + 真实 restore 函数，配一个内存 fetch：
 * 因此"token 是否在 POST 前就位"是被真正断言的，而不是靠 mock 假装。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {createApiClient} from '../cns_planner/web/js/api/client.js';
import {bootstrapProjectState, withTimeout, TIMEOUT_ERROR_NAME} from '../cns_planner/web/js/state/bootstrap.js';
import {
  LAST_EXPLICIT_PROJECT_KEY, readLastExplicitProject, restoreLastExplicitProject,
} from '../cns_planner/web/js/state/explicit_project.js';

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: key => map.delete(key),
  };
}

function serverState({directory = '', automatic = true, name = '', token = 'session-token', revision = 7} = {}) {
  return {
    token, revision,
    project_storage: {automatic, directory, file: directory ? directory + '/project_state.json' : ''},
    paths: {}, layers: [], population: {}, terrain: {}, terrain_dtm: {},
    data_health: {}, defaults: {engineering_parameters: {}},
    workflow: {project: {name}, revision, workspace: {bbox: [120, 30, 121, 31]}, grid: {}},
  };
}

/**
 * 内存 fetch：按 URL 返回预置响应，并记录每次请求的 method / headers / body。
 * `respond` 允许测试按需抛错或返回 4xx。
 */
function fakeFetch(respond) {
  const calls = [];
  const impl = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({url, method: options.method || 'GET', headers: {...(options.headers || {})}, body});
    const result = respond(url, options, calls.length);
    if (result instanceof Error) throw result;
    const status = result?.status || 200;
    const payload = result?.data === undefined ? result : result.data;
    return {
      ok: status >= 200 && status < 300,
      status,
      // 与真实 ApiHandler 一致：JSON 端点始终声明 application/json，
      // createApiClient 依据它决定走 response.json()。
      headers: {
        get: name => {
          const key = String(name).toLowerCase();
          if (key === 'content-type') return 'application/json; charset=utf-8';
          if (key === 'x-cns-revision') return String(result?.revision ?? '');
          return null;
        },
      },
      json: async () => payload,
    };
  };
  impl.calls = calls;
  return impl;
}

/** 组装"和 main.js 同构"的启动过程。 */
function harness({serverStates, respond, storage = memoryStorage(), restore, newAbortController} = {}) {
  const queue = [...serverStates];
  const updates = [];
  let live = queue[0];
  const fetchImpl = fakeFetch(respond || (url => {
    if (url === '/api/state') return queue.length > 1 ? queue.shift() : live;
    return {data: {}};
  }));
  const api = createApiClient(() => live?.token, () => live?.workflow?.revision ?? live?.revision);
  const update = (data, meta) => { live = data; updates.push({data, meta}); };
  return {
    api, update, updates, fetchImpl, storage,
    restore: restore || (state => restoreLastExplicitProject(state, {api, storage})),
  };
}

async function run(harnessOptions) {
  const h = harness(harnessOptions);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = h.fetchImpl;
  try {
    const result = await bootstrapProjectState({
      api: h.api,
      restore: h.restore,
      update: h.update,
      newAbortController: h.newAbortController,
    });
    return {...h, result};
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('1/8. server explicit project：直接采用服务器项目，绝不重新 open（缓存被覆盖）', async () => {
  const storage = memoryStorage({
    [LAST_EXPLICIT_PROJECT_KEY]: JSON.stringify({directory: 'D:/stale/cache', project_name: 'stale'}),
  });
  const explicit = serverState({directory: 'D:/server/project', automatic: false, name: '服务器项目'});
  const h = await run({serverStates: [explicit], storage});

  assert.equal(h.result.restored, false);
  assert.equal(h.result.attempted, false);
  assert.equal(h.result.reason, 'server_project');
  assert.equal(h.updates.length, 1, '服务器已有明确项目时只落地一次，不重新 open');
  assert.equal(h.updates[0].data.project_storage.directory, 'D:/server/project');
  assert.equal(h.fetchImpl.calls.filter(call => call.method === 'POST').length, 0, '不得发出任何恢复 POST');
  // 服务器项目回写缓存，下一次即使服务器退回自动项目也只恢复这一个目录
  assert.equal(readLastExplicitProject(storage).directory, 'D:/server/project');
});

test('2/3. automatic server + cached explicit directory：恢复一次，且 POST 带当前会话 token/revision', async () => {
  const storage = memoryStorage({
    [LAST_EXPLICIT_PROJECT_KEY]: JSON.stringify({directory: 'D:/chosen/project', project_name: 'Chosen'}),
  });
  const automatic = serverState({directory: '', automatic: true, name: '未命名项目', token: 'session-token', revision: 11});
  const restored = serverState({directory: 'D:/chosen/project', automatic: false, name: 'Chosen', token: 'session-token', revision: 12});
  let stateCalls = 0;
  const h = await run({
    serverStates: [automatic],
    storage,
    respond: url => {
      if (url === '/api/project/open') return restored;
      stateCalls += 1;
      return stateCalls === 1 ? automatic : restored;
    },
  });

  assert.equal(h.result.restored, true);
  assert.equal(h.result.reason, 'restored');
  const posts = h.fetchImpl.calls.filter(call => call.method === 'POST');
  assert.equal(posts.length, 1, '恰好恢复一次');
  assert.equal(posts[0].url, '/api/project/open');
  assert.deepEqual(posts[0].body, {project_dir: 'D:/chosen/project'});
  assert.equal(posts[0].headers['X-CNS-Token'], 'session-token', '恢复请求必须携带会话令牌');
  assert.equal(posts[0].headers['X-CNS-Revision'], '11', '恢复请求必须携带当前服务器 revision');
  // 恢复请求必须发生在服务器状态落地之后（token 才可能就位）
  assert.ok(h.fetchImpl.calls.indexOf(posts[0]) > 0, '恢复请求在 /api/state 之后');
  assert.equal(h.fetchImpl.calls[0].url, '/api/state');
  assert.equal(h.updates[0].meta.reason, 'server_state');
});

test('4. 恢复成功后使用 fresh state / workflow 重渲染，且顺序为 state → open → state', async () => {
  const storage = memoryStorage({
    [LAST_EXPLICIT_PROJECT_KEY]: JSON.stringify({directory: 'D:/chosen/project', project_name: 'Chosen'}),
  });
  const automatic = serverState({directory: '', automatic: true, name: '未命名项目', revision: 3});
  const opened = serverState({directory: 'D:/chosen/project', automatic: false, name: 'Chosen', revision: 4});
  const fresh = serverState({directory: 'D:/chosen/project', automatic: false, name: 'Chosen', revision: 9});
  fresh.workflow.grid = {detail_available: true, cell_count: 4};

  const stateCalls = [];
  const fetchImpl = fakeFetch(url => {
    if (url !== '/api/state') return {data: opened};
    stateCalls.push('state');
    return stateCalls.length === 1 ? automatic : fresh;
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  let live = automatic;
  const updates = [];
  try {
    const api = createApiClient(() => live?.token, () => live?.workflow?.revision);
    const result = await bootstrapProjectState({
      api,
      restore: state => restoreLastExplicitProject(state, {api, storage}),
      update: (data, meta) => { live = data; updates.push({data, meta}); },
    });
    assert.equal(result.restored, true);
    assert.deepEqual(stateCalls, ['state', 'state'], '恢复成功必须重新取一次 fresh /api/state');
    assert.equal(updates.length, 2, 'server_state → restored(fresh state)');
    assert.equal(updates[0].meta.reason, 'server_state');
    assert.equal(updates[1].meta.reason, 'restored');
    assert.equal(updates[1].data.workflow.grid.cell_count, 4, '最终 flow 来自 fresh state');
    assert.equal(updates[1].data.revision, 9);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('5/6. 恢复失败：只尝试一次、明确提示、页面仍然落地（不卡死）', async () => {
  const storage = memoryStorage({
    [LAST_EXPLICIT_PROJECT_KEY]: JSON.stringify({directory: 'D:/gone/project', project_name: 'Gone'}),
  });
  const automatic = serverState({directory: '', automatic: true, name: '未命名项目'});
  const errors = [];
  const h = harness({
    serverStates: [automatic],
    storage,
    respond: (url) => (url === '/api/state' ? automatic : {status: 403, data: {error: '无效会话或来源'}}),
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = h.fetchImpl;
  let result;
  try {
    result = await bootstrapProjectState({
      api: h.api, restore: h.restore, update: h.update, onError: message => errors.push(message),
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(result.ok, true, '恢复失败不得让 bootstrap 失败');
  assert.equal(result.restored, false);
  assert.equal(result.attempted, true);
  assert.equal(result.reason, 'restore_failed');
  const posts = h.fetchImpl.calls.filter(call => call.method === 'POST');
  assert.equal(posts.length, 1, '失败只尝试一次，不循环');
  assert.equal(posts[0].body.project_dir, 'D:/gone/project', '不扫描、不猜测其它项目');
  assert.equal(h.updates.length, 1, '失败时保留服务器（自动恢复）项目，不重复渲染');
  assert.equal(h.updates[0].data.project_storage.automatic, true);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /恢复上次项目失败/);
  assert.match(errors[0], /当前保留自动恢复项目/);
});

test('6. restore pending/超时也不卡死：bootstrap 必须给出结论', async () => {
  const storage = memoryStorage({
    [LAST_EXPLICIT_PROJECT_KEY]: JSON.stringify({directory: 'D:/slow/project'}),
  });
  const automatic = serverState({directory: '', automatic: true});
  const fetchImpl = fakeFetch(url => (url === '/api/state' ? automatic : undefined));
  // 让 /api/project/open 永不返回（模拟 QGIS 单线程被占用）
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (url, options) => (url === '/api/project/open' ? new Promise(() => {}) : fetchImpl(url, options));
  const errors = [];
  const updates = [];
  let live = automatic;
  try {
    const api = createApiClient(() => live?.token, () => live?.workflow?.revision);
    const result = await bootstrapProjectState({
      api,
      restore: state => restoreLastExplicitProject(state, {api, storage}),
      update: (data, meta) => { live = data; updates.push(meta); },
      timeout_ms: 30,
      onError: message => errors.push(message),
    });
    assert.equal(result.ok, true);
    assert.equal(result.timeout, true, '恢复阶段超时必须被识别');
    assert.equal(updates.length, 1);
    assert.equal(updates[0].reason, 'server_state', 'promise 未 settle 时页面也必须有服务器状态可用');
    assert.equal(errors.length, 1);
    assert.match(errors[0], /恢复上次项目超时/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('7. 无缓存时保持 automatic，且不发任何 POST', async () => {
  const storage = memoryStorage();
  const automatic = serverState({directory: '', automatic: true, name: '未命名项目'});
  const h = await run({serverStates: [automatic], storage});

  assert.equal(h.result.ok, true);
  assert.equal(h.result.attempted, false);
  assert.equal(h.result.restored, false);
  assert.equal(h.result.reason, 'server_project');
  assert.equal(h.fetchImpl.calls.filter(call => call.method === 'POST').length, 0);
  assert.equal(h.updates[0].data.project_storage.automatic, true);
});

test('/api/state 失败：bootstrap 明确失败并给出可读原因', async () => {
  const errors = [];
  const api = createApiClient(() => null, () => null);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fakeFetch(() => new Error('connect ECONNREFUSED'));
  try {
    const result = await bootstrapProjectState({
      api,
      restore: async state => ({state, restored: false, attempted: false, error: null}),
      update: () => { throw new Error('不应被调用'); },
      onError: message => errors.push(message),
    });
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'state_failed');
    assert.equal(errors.length, 1);
    assert.match(errors[0], /无法连接本机地图服务/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('withTimeout：正常返回直通，超时抛出可识别的中文错误', async () => {
  await assert.doesNotReject(withTimeout(Promise.resolve(1), {timeout_ms: 50}));
  await assert.rejects(
    withTimeout(new Promise(() => {}), {timeout_ms: 20, label: '读取服务器状态'}),
    error => error.name === TIMEOUT_ERROR_NAME && /读取服务器状态/.test(error.message),
  );
});
