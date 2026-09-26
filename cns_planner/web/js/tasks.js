/**
 * Phase4-B6X：通用"重任务"面板（最小实现，独立模块）。
 *
 * 设计约束（与仓库既有约定一致）：
 *  - 主界面只说业务语言：正在排队 / 正在计算 / 进度 % / 最近心跳 / 取消任务 /
 *    已完成 / 已取消 / 执行失败 / 输入已变化，请重新运行。
 *    **绝不**在主界面显示 `queued` / `running` / `stale` / `task_id` 这些技术值；
 *    它们只出现在折叠的「高级 / 审计信息」里。
 *  - 任务状态来自后端持久 task store：刷新页面后用 localStorage 里的 task_id 恢复，
 *    因此状态不依赖任何内存 DOM 状态。
 *  - 关闭浏览器/页面**不会**触发取消；取消必须由用户显式点击。
 *
 * 本模块是纯前端最小实现：不引入框架、不复制后端任务框架，只消费 /api/tasks*。
 */

const TASKS_ENDPOINT = '/api/tasks';
const STATE_ENDPOINT = '/api/state';
const STORAGE_KEY = 'cns.heavyTasks.v1';
const POLL_INTERVAL_MS = 1500;

/**
 * 会话令牌：任务端点与其它受保护端点一样要求有效会话。令牌只能由后端在
 * ``/api/state`` 里下发（前端不生成、不持久化），因此这里在首次需要时取一次。
 */
let sessionToken = null;

export function setSessionToken(token) {
  sessionToken = token || null;
  return sessionToken;
}

export async function ensureSessionToken(fetchImpl = globalThis.fetch) {
  if (sessionToken) return sessionToken;
  const response = await fetchImpl(STATE_ENDPOINT, {headers: {Accept: 'application/json'}});
  const data = await response.json().catch(() => ({}));
  if (data && typeof data.token === 'string' && data.token) sessionToken = data.token;
  return sessionToken;
}

function withToken(headers = {}) {
  return sessionToken ? {'X-CNS-Token': sessionToken, ...headers} : {...headers};
}

/** 后端状态 → 中文业务文案（主界面唯一词表；与 presentation.js 风格一致）。 */
export const TASK_STATUS_TEXT = {
  queued: '正在排队',
  running: '正在计算',
  succeeded: '已完成',
  failed: '执行失败',
  cancelling: '正在取消',
  cancelled: '已取消',
  stale: '输入已变化，请重新运行',
};

/** 技术状态值（只允许出现在高级区）。 */
export const TASK_ADVANCED_STATUSES = [
  'queued', 'running', 'succeeded', 'failed', 'cancelling', 'cancelled', 'stale',
];

export function taskStatusText(status) {
  const key = String(status || '');
  return Object.prototype.hasOwnProperty.call(TASK_STATUS_TEXT, key)
    ? TASK_STATUS_TEXT[key] : '状态未知';
}

export function progressText(progress) {
  if (progress === null || progress === undefined || progress === '') return '—';
  const value = Number(progress);
  if (!Number.isFinite(value)) return '—';
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

/** 心跳时间 → "刚刚 / 12 秒前 / 3 分钟前"。 */
export function heartbeatText(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 5) return '刚刚';
  if (value < 60) return `${Math.round(value)} 秒前`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟前`;
  return `${Math.round(value / 3600)} 小时前`;
}

/** 是否是仍需要用户关注的（未结束的）任务状态。 */
export function isActiveStatus(status) {
  return ['queued', 'running', 'cancelling'].includes(String(status || ''));
}

/** 成功后是否需要提示"业务结果已更新"。 */
export function resultScopeText(task) {
  return (task && task.result_scope) ? String(task.result_scope) : '业务结果';
}

export function readStoredTaskIds(storage) {
  const store = storage || globalThis.localStorage;
  try {
    const raw = store?.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : [];
  } catch (error) {
    return [];
  }
}

export function writeStoredTaskIds(ids, storage) {
  const store = storage || globalThis.localStorage;
  try {
    store?.setItem(STORAGE_KEY, JSON.stringify(Array.from(new Set(ids)).slice(-20)));
  } catch (error) {
    /* 存储不可用时不阻塞 UI：恢复能力降级，但任务本身仍在后端持久化。 */
  }
}

export function rememberTaskId(taskId, storage) {
  if (!taskId) return readStoredTaskIds(storage);
  const ids = readStoredTaskIds(storage);
  ids.push(String(taskId));
  writeStoredTaskIds(ids, storage);
  return readStoredTaskIds(storage);
}

/** 业务 endpoint 的异步提交：POST 带 ``async: true``，期望 202 + task_id。 */
export async function submitHeavyTask(api, path, payload = {}) {
  const response = await api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payload, async: true }),
  });
  const taskId = response?.task_id || response?.task?.task_id;
  if (!taskId) throw new Error('服务端未返回任务标识，无法跟踪本次计算');
  rememberTaskId(taskId);
  return response;
}

export async function cancelHeavyTask(taskId, fetchImpl = globalThis.fetch) {
  await ensureSessionToken(fetchImpl).catch(() => null);
  const response = await fetchImpl(`${TASKS_ENDPOINT}/cancel`, {
    method: 'POST',
    headers: withToken({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ task_id: taskId }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || '取消请求失败');
  return data;
}

export async function loadTasks(fetchImpl = globalThis.fetch) {
  await ensureSessionToken(fetchImpl).catch(() => null);
  const response = await fetchImpl(TASKS_ENDPOINT, {
    headers: withToken({ Accept: 'application/json' }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || '任务列表不可用');
  return Array.isArray(data) ? data : (data.items || []);
}

export async function loadTaskCatalog(fetchImpl = globalThis.fetch) {
  await ensureSessionToken(fetchImpl).catch(() => null);
  const response = await fetchImpl(`${TASKS_ENDPOINT}/catalog`, {
    headers: withToken({ Accept: 'application/json' }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) return [];
  return Array.isArray(data?.items) ? data.items : [];
}

/** 通用提交入口：业务页面只需给出 task_type，任务框架不复制。 */
export async function submitTaskType(taskType, payload = {}, fetchImpl = globalThis.fetch) {
  await ensureSessionToken(fetchImpl).catch(() => null);
  const response = await fetchImpl(`${TASKS_ENDPOINT}`, {
    method: 'POST',
    headers: withToken({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ ...payload, task_type: taskType }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.error || '任务提交失败');
    error.code = data?.detail?.code || data?.code || null;
    throw error;
  }
  const taskId = data?.task_id || data?.task?.task_id;
  if (taskId) rememberTaskId(taskId);
  return data;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** 单条任务的业务行：主界面只出现中文文案 + 进度 + 心跳 + 取消/继续操作。 */
export function taskRowHtml(task) {
  const status = String(task?.advanced?.status || '');
  const lines = [
    `<div class="cns-task-row" data-task-id="${escapeHtml(task.task_id)}" data-status="${escapeHtml(status)}">`,
    `<div class="cns-task-head"><strong>${escapeHtml(task.task_name || '后台计算')}</strong>`,
    `<span class="cns-task-status">${escapeHtml(task.status_text || taskStatusText(status))}</span></div>`,
    `<div class="cns-task-progress"><span>进度 ${escapeHtml(progressText(task.progress))}</span>`,
    `<span>最近心跳 ${escapeHtml(heartbeatText(task.heartbeat_age_seconds))}</span></div>`,
    `<div class="cns-task-message">${escapeHtml(task.message || '')}</div>`,
  ];
  if (task.business_error?.text) {
    lines.push(`<div class="cns-task-error" role="alert">${escapeHtml(task.business_error.text)}</div>`);
  }
  if (task.can_cancel) {
    lines.push(`<button class="secondary compact cns-task-cancel" data-task-id="${escapeHtml(task.task_id)}">取消任务</button>`);
  }
  if (status === 'succeeded') {
    lines.push(`<div class="cns-task-done">${escapeHtml(resultScopeText(task))}已更新；刷新页面即可看到最新正式结果。</div>`);
  }
  if (status === 'stale') {
    lines.push('<div class="cns-task-hint">当前正式结果未被覆盖；请重新运行。</div>');
  }
  lines.push(advancedHtml(task));
  lines.push('</div>');
  return lines.join('');
}

/** 高级 / 审计信息：raw 状态值、task_id、输入指纹与 artifact 引用只出现在这里。 */
export function advancedHtml(task) {
  const advanced = task?.advanced || {};
  const rows = [
    ['任务标识', advanced.task_id],
    ['技术状态', advanced.status],
    ['任务类型', advanced.task_type],
    ['输入 revision', advanced.input_revision],
    ['输入指纹', advanced.input_fingerprint],
    ['输入快照引用', advanced.input_snapshot_ref],
    ['结果明细引用', advanced.result_artifact_ref?.relative_path || advanced.result_artifact_ref?.artifact_id],
    ['任务存储', advanced.store_directory],
    ['技术错误码', advanced.error?.code],
    ['技术错误说明', advanced.error?.message],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!rows.length) return '';
  return [
    '<details class="algorithm-detail cns-task-advanced"><summary>高级 / 审计信息 · 任务契约字段</summary>',
    '<div class="flow-summary">',
    rows.map(([label, value]) => `<div><span class="cns-task-label">${escapeHtml(label)}</span>${escapeHtml(String(value))}</div>`).join(''),
    '</div></details>',
  ].join('');
}

export function panelHtml(tasks, catalog = []) {
  const items = Array.isArray(tasks) ? tasks : [];
  const body = items.length
    ? items.map(taskRowHtml).join('')
    : '<div class="wb-empty cns-task-empty">当前没有后台计算任务</div>';
  const starters = (Array.isArray(catalog) ? catalog : [])
    .filter((item) => item && item.task_type && item.task_type !== 'runtime_probe')
    .map((item) => [
      `<button class="secondary compact cns-task-start" data-task-type="${escapeHtml(item.task_type)}">`,
      `运行${escapeHtml(item.task_name || '后台计算')}</button>`,
    ].join('')).join('');
  return [
    '<div class="cns-task-panel" id="cnsTaskPanel">',
    '<div class="cns-task-panel-head"><h3>后台计算任务</h3>',
    '<button class="secondary compact" id="cnsTaskToggle">收起</button></div>',
    `<div class="cns-task-panel-body" id="cnsTaskList">${body}</div>`,
    starters ? `<div class="cns-task-starters">${starters}</div>` : '',
    '<div class="cns-task-note">关闭页面不会取消任务；刷新页面后仍可按任务标识恢复进度。</div>',
    '</div>',
  ].join('');
}

/**
 * 任务面板控制器：轮询状态、渲染、取消、刷新后恢复。
 */
export function createTaskCenter(options = {}) {
  const fetchImpl = options.fetch || globalThis.fetch;
  const storage = options.storage || globalThis.localStorage;
  const documentRef = options.document || globalThis.document;
  const interval = Number(options.interval || POLL_INTERVAL_MS);
  let timer = null;
  let tasks = [];
  let catalog = [];
  let collapsed = false;

  const known = () => readStoredTaskIds(storage);

  function mount() {
    if (!documentRef) return null;
    let host = documentRef.getElementById('cnsTaskCenter');
    if (!host) {
      host = documentRef.createElement('aside');
      host.id = 'cnsTaskCenter';
      host.className = 'cns-task-center';
      documentRef.body.appendChild(host);
    }
    host.innerHTML = panelHtml(tasks, catalog);
    const toggle = documentRef.getElementById('cnsTaskToggle');
    if (toggle) {
      toggle.textContent = collapsed ? '展开' : '收起';
      toggle.onclick = () => {
        collapsed = !collapsed;
        const panel = documentRef.getElementById('cnsTaskList');
        if (panel) panel.hidden = collapsed;
        toggle.textContent = collapsed ? '展开' : '收起';
      };
    }
    for (const button of documentRef.querySelectorAll('.cns-task-cancel')) {
      button.onclick = async () => {
        try {
          await cancelHeavyTask(button.dataset.taskId, fetchImpl);
          await refresh();
        } catch (error) {
          showPanelError(error.message);
        }
      };
    }
    for (const button of documentRef.querySelectorAll('.cns-task-start')) {
      button.onclick = async () => {
        try {
          await submitTaskType(button.dataset.taskType, {}, fetchImpl);
          await refresh();
        } catch (error) {
          showPanelError(error.message);
        }
      };
    }
    return host;
  }

  function showPanelError(message) {
    const panel = documentRef?.getElementById('cnsTaskList');
    if (!panel) return;
    const note = documentRef.createElement('div');
    note.className = 'cns-task-error';
    note.setAttribute('role', 'alert');
    note.textContent = message;
    panel.prepend(note);
  }

  async function refresh() {
    const ids = known();
    const [all, available] = await Promise.all([
      loadTasks(fetchImpl),
      catalog.length ? Promise.resolve(catalog) : loadTaskCatalog(fetchImpl).catch(() => []),
    ]);
    catalog = available;
    // 恢复语义：刷新页面后按 task_id 找回状态；同时展示当前活跃任务。
    tasks = all.filter((task) => isActiveStatus(task.advanced?.status) || ids.includes(task.task_id));
    tasks.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    mount();
    return tasks;
  }

  function start() {
    if (timer !== null) return;
    refresh().catch(() => {});
    timer = setInterval(() => { refresh().catch(() => {}); }, interval);
  }

  function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  return {
    start, stop, refresh, mount,
    get tasks() { return tasks; },
  };
}

if (typeof document !== 'undefined' && !globalThis.__CNS_TASK_CENTER__) {
  globalThis.__CNS_TASK_CENTER__ = createTaskCenter();
  globalThis.__CNS_TASK_CENTER__.start();
}
