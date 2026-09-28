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
export const TASK_PANEL_UI_STORAGE_KEY = 'cns.taskPanel.ui.v1';
const POLL_INTERVAL_MS = 1500;

function resolveStorage(storage) {
  if (storage) return storage;
  try {
    return globalThis.document?.defaultView?.localStorage || globalThis.localStorage;
  } catch (_) { return null; }
}

export function readTaskPanelUiState(storage) {
  const store = resolveStorage(storage);
  try {
    const parsed = JSON.parse(store?.getItem(TASK_PANEL_UI_STORAGE_KEY) || '{}');
    return {
      collapsed: parsed?.collapsed === true,
      x: parsed?.x !== null && parsed?.x !== undefined && Number.isFinite(Number(parsed.x)) ? Number(parsed.x) : null,
      y: parsed?.y !== null && parsed?.y !== undefined && Number.isFinite(Number(parsed.y)) ? Number(parsed.y) : null,
    };
  } catch (_) {
    return {collapsed: false, x: null, y: null};
  }
}

export function writeTaskPanelUiState(value, storage) {
  const store = resolveStorage(storage);
  const state = {
    collapsed: value?.collapsed === true,
    x: value?.x !== null && value?.x !== undefined && Number.isFinite(Number(value.x)) ? Number(value.x) : null,
    y: value?.y !== null && value?.y !== undefined && Number.isFinite(Number(value.y)) ? Number(value.y) : null,
  };
  try { store?.setItem(TASK_PANEL_UI_STORAGE_KEY, JSON.stringify(state)); } catch (_) { /* UI preference only */ }
  return state;
}

export function clampTaskPanelPosition(position, viewport, panelSize) {
  const width = Math.max(0, Number(panelSize?.width) || 0);
  const height = Math.max(0, Number(panelSize?.height) || 0);
  const viewportWidth = Math.max(0, Number(viewport?.width) || 0);
  const viewportHeight = Math.max(0, Number(viewport?.height) || 0);
  return {
    x: Math.min(Math.max(0, Number(position?.x) || 0), Math.max(0, viewportWidth - width)),
    y: Math.min(Math.max(0, Number(position?.y) || 0), Math.max(0, viewportHeight - height)),
  };
}

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

// ---- BUG-TASK-PROGRESS-001：已运行时长 与「只在可靠时才给出」的 ETA -----------------
//
// 硬约束：**绝不伪造 ETA**。
// 只有在"实际开始执行（有 started_at）"且"进度样本足以支撑线性外推"时才显示数值；
// 其余一切情况都显示「预计剩余时间：暂无法估算」。判定条件全部是显式的：
//   1. 任务已进入 running 且 progress ∈ (0, 1)；
//   2. 至少两个**进度确实增长**的样本，且两次采样间隔 >= MIN_SAMPLE_SECONDS；
//   3. 用整体平均速率（elapsed/progress）与最近一段速率外推，两者偏差 <= RELATIVE_TOLERANCE；
//   4. 外推得到的剩余时间不超过 MAX_HORIZON_SECONDS（更远的外推不再有意义，如实说无法估算）。
export const ETA_MIN_SAMPLE_SECONDS = 3;
export const ETA_RELATIVE_TOLERANCE = 0.4;
export const ETA_MAX_HORIZON_SECONDS = 6 * 3600;

/** 解析后端 ISO 时间戳（无法解析时返回 null，绝不猜一个时间）。 */
export function parseTaskTime(value) {
  if (value === null || value === undefined || value === '') return null;
  const stamp = Date.parse(String(value));
  return Number.isFinite(stamp) ? stamp : null;
}

/**
 * 已运行时长（秒）。
 *
 * @param {object} task
 * @param {number} [nowMs] 当前时刻（测试注入用）
 * @returns {number|null} 尚未开始执行时为 null（界面显示 "—"，不显示 0 秒）
 */
export function elapsedSeconds(task, nowMs = Date.now()) {
  const started = parseTaskTime(task?.started_at);
  if (started === null) return null;
  const finished = parseTaskTime(task?.finished_at);
  const end = finished === null ? Number(nowMs) : finished;
  return Math.max(0, (end - started) / 1000);
}

/** 时长中文：`12 秒` / `2 分 10 秒` / `1 小时 02 分`。 */
export function durationText(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 60) return `${Math.round(value)} 秒`;
  if (value < 3600) {
    const minutes = Math.floor(value / 60), rest = Math.round(value - minutes * 60);
    return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
  }
  const hours = Math.floor(value / 3600), minutes = Math.round((value - hours * 3600) / 60);
  return `${hours} 小时 ${String(minutes).padStart(2, '0')} 分`;
}

/**
 * 进度样本登记：只有"进度实际增长且间隔足够"时才记录，
 * 因此速率估算不会被同一进度的重复心跳稀释。
 *
 * @param {Array<{t:number,p:number}>} samples 可变数组（调用方持有）
 * @param {object} task
 * @param {number} nowMs
 */
export function recordProgressSample(samples, task, nowMs = Date.now()) {
  const list = Array.isArray(samples) ? samples : [];
  const progress = Number(task?.progress);
  if (!Number.isFinite(progress)) return list;
  const previous = list.length ? list[list.length - 1] : null;
  if (previous && progress <= previous.p) return list;
  if (previous && (nowMs - previous.t) / 1000 < ETA_MIN_SAMPLE_SECONDS) return list;
  list.push({t: Number(nowMs), p: progress});
  // 样本上限：只保留最近 24 个，避免长任务累积无界内存。
  while (list.length > 24) list.shift();
  return list;
}

/**
 * 预计剩余时间。
 *
 * @param {object} task
 * @param {Array<{t:number,p:number}>} samples
 * @param {number} [nowMs]
 * @returns {{known:boolean, seconds:number|null, text:string, reason:string}}
 */
export function estimateRemaining(task, samples, nowMs = Date.now()) {
  const unknown = reason => ({known: false, seconds: null, text: '预计剩余时间：暂无法估算', reason});
  const status = String(task?.advanced?.status || '');
  if (status !== 'running') return unknown('任务尚未进入执行阶段');
  const progress = Number(task?.progress);
  if (!Number.isFinite(progress) || progress <= 0 || progress >= 1) {
    return unknown('进度信息不足以估计速率');
  }
  const elapsed = elapsedSeconds(task, nowMs);
  if (elapsed === null || elapsed <= 0) return unknown('缺少实际开始时间');
  const list = (Array.isArray(samples) ? samples : []).filter(item => Number.isFinite(item?.t) && Number.isFinite(item?.p));
  if (list.length < 2) return unknown('进度样本不足（需要至少两次进度增长）');
  const first = list[0], last = list[list.length - 1];
  if (last.p <= first.p) return unknown('进度尚未增长');
  const spanSeconds = (last.t - first.t) / 1000;
  if (spanSeconds < ETA_MIN_SAMPLE_SECONDS) return unknown('采样间隔过短');
  const averageRate = progress / elapsed;                        // 整体平均速率（进度/秒）
  const windowRate = (last.p - first.p) / spanSeconds;            // 最近一段速率
  if (!(averageRate > 0) || !(windowRate > 0)) return unknown('速率不可用');
  if (Math.abs(windowRate - averageRate) / Math.max(averageRate, windowRate) > ETA_RELATIVE_TOLERANCE) {
    return unknown('速率波动过大，暂不可靠');
  }
  const remaining = (1 - progress) / averageRate;
  if (!Number.isFinite(remaining) || remaining <= 0) return unknown('剩余时间无法外推');
  if (remaining > ETA_MAX_HORIZON_SECONDS) return unknown('外推时间过远');
  return {
    known: true,
    seconds: remaining,
    text: `预计剩余：${durationText(remaining)}`,
    reason: '按整体平均速率与最近一段速率一致性外推',
  };
}

/** 进度条百分比（用于头部与任务行，只是展示，不参与任何业务判定）。 */
export function progressPercent(task) {
  const value = Number(task?.progress);
  if (!Number.isFinite(value)) return 0;
  return Math.round(Math.max(0, Math.min(1, value)) * 100);
}

/** 任务业务名称：`航路规划 · Theta* V2` → 业务名 `航路规划`（无分隔符时原样返回）。 */
export function taskBusinessName(task) {
  const name = String(task?.task_name || task?.task_type || '后台计算');
  return name.split(' · ')[0].trim() || name;
}

/** 活跃任务的头部摘要文案：`1 个任务正在计算 · 航路规划`。 */
export function activeTaskSummary(tasks) {
  const active = (Array.isArray(tasks) ? tasks : []).filter(task => isActiveStatus(task?.advanced?.status));
  if (!active.length) return {count: 0, text: '', running: 0, queued: 0};
  const running = active.filter(task => String(task?.advanced?.status) === 'running').length;
  const queued = active.filter(task => String(task?.advanced?.status) === 'queued').length;
  const names = Array.from(new Set(active.map(taskBusinessName))).slice(0, 3).join('、');
  const verb = running ? '正在计算' : '正在排队';
  return {count: active.length, running, queued, text: `${active.length} 个任务${verb} · ${names}`};
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
  const store = resolveStorage(storage);
  try {
    const raw = store?.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : [];
  } catch (error) {
    return [];
  }
}

export function writeStoredTaskIds(ids, storage) {
  const store = resolveStorage(storage);
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

/**
 * 双模式响应适配（BUG-TASK-FEEDBACK-001 装配契约）。
 *
 * 本模块历史上用 `fetch` 风格（`response.ok` + `response.json()`）读取任务端点。
 * 但壳层注入的是既有 API 客户端 `api(path, options)` —— 它**成功时直接返回解析后的
 * JSON 对象，失败时抛异常**（见 `api/client.js`）。两者混用会让 `response.ok` 恒为
 * undefined，于是任务面板永远显示"任务列表不可用"。
 *
 * 这里把 API 客户端统一包成 fetch 契约，让下游解析逻辑只有一条路径。
 *
 * @param {(url:string, options?:object)=>Promise<any>} request
 * @returns {(path:string, options?:object)=>Promise<object>}
 */
export function createTaskRequestAdapter(request){
  if(typeof request!=='function')return null;
  const adapt=async (path,options={})=>{
    const method=String(options.method||'GET').toUpperCase();
    const headers={...((options&&options.headers)||{})};
    const body=options&&options.body!==undefined&&options.body!==null?options.body:undefined;
    try{
      const data=await request(path,{method,headers,...(body!==undefined?{body}:{})});
      // API 客户端成功即 200：包成 fetch 风格。
      return {
        ok:true,
        status:200,
        headers:{get:()=>'application/json'},
        json:async()=>(data===undefined?{}:data),
      };
    }catch(error){
      // 失败时把中文业务错误包成非 2xx 响应：下游仍然只读 `data.error`。
      return {
        ok:false,
        status:Number(error?.status)||400,
        headers:{get:()=>'application/json'},
        json:async()=>({error:error?.message||String(error),code:error?.code||null}),
      };
    }
  };
  adapt.__taskRequestAdapter__=true;
  return adapt;
}

/** 由 `tasks.js` 直接使用：把当前 fetch 实现统一到 fetch 契约。 */
function ensureFetchContract(fetchImpl){
  if(typeof fetchImpl!=='function')return globalThis.fetch;
  if(fetchImpl.__taskRequestAdapter__)return fetchImpl;
  return createTaskRequestAdapter(fetchImpl);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** 单条任务的业务行：主界面只出现中文文案 + 进度 + 已运行 + 心跳 + ETA + 取消/继续操作。 */
export function taskRowHtml(task, options = {}) {
  const status = String(task?.advanced?.status || '');
  const samples = Array.isArray(options.samples) ? options.samples : [];
  const nowMs = Number.isFinite(Number(options.nowMs)) ? Number(options.nowMs) : Date.now();
  const elapsed = elapsedSeconds(task, nowMs);
  const eta = estimateRemaining(task, samples, nowMs);
  const lines = [
    `<div class="cns-task-row" data-task-id="${escapeHtml(task.task_id)}" data-status="${escapeHtml(status)}">`,
    `<div class="cns-task-head"><strong>${escapeHtml(task.task_name || '后台计算')}</strong>`,
    `<span class="cns-task-status">${escapeHtml(task.status_text || taskStatusText(status))}</span></div>`,
    `<div class="cns-task-progress"><span>进度 ${escapeHtml(progressText(task.progress))}</span>`,
    `<span>已运行 ${escapeHtml(durationText(elapsed))}</span>`,
    `<span>最近心跳 ${escapeHtml(heartbeatText(task.heartbeat_age_seconds))}</span></div>`,
    `<div class="cns-task-eta" data-eta-known="${eta.known ? 'true' : 'false'}">${escapeHtml(eta.text)}</div>`,
    `<div class="cns-task-message">${escapeHtml(task.message || '')}</div>`,
  ];
  if (task.business_error?.text) {
    lines.push(`<div class="cns-task-error" role="alert">${escapeHtml(task.business_error.text)}</div>`);
  }
  if (task.can_cancel) {
    lines.push(`<button class="secondary compact cns-task-cancel" data-task-id="${escapeHtml(task.task_id)}">取消任务</button>`);
  }
  if (status === 'succeeded') {
    // 成功后引擎会自动 refresh 正式 workflow 快照；这里如实说明"无需 F5"。
    lines.push(`<div class="cns-task-done">${escapeHtml(resultScopeText(task))}结果已更新；工作台自动刷新，无需手动 F5。</div>`);
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

export function panelHtml(tasks, catalog = [], options = {}) {
  const items = Array.isArray(tasks) ? tasks : [];
  const samplesById = options.samplesById || {};
  const nowMs = Number.isFinite(Number(options.nowMs)) ? Number(options.nowMs) : Date.now();
  const body = items.length
    ? items.map(task => taskRowHtml(task, {samples: samplesById[task.task_id], nowMs})).join('')
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
    '<div class="cns-task-panel-content" id="cnsTaskPanelContent">',
    '<div class="cns-task-error-area" id="cnsTaskErrorArea" role="alert"></div>',
    `<div class="cns-task-panel-body" id="cnsTaskList">${body}</div>`,
    starters ? `<div class="cns-task-starters">${starters}</div>` : '',
    '<div class="cns-task-note">关闭页面不会取消任务；刷新页面后仍可按任务标识恢复进度。</div>',
    '</div>',
    '</div>',
  ].join('');
}

/**
 * 任务面板控制器：轮询状态、渲染、取消、刷新后恢复。
 */
export function createTaskCenter(options = {}) {
  // fetch 实现与"任务完成后刷新正式结果"的回调都必须可以在装配后被替换：
  // main.js 在 API 客户端（带会话令牌）就绪后才注入，不能在这里写死。
  // 注入的可能是 fetch 实现，也可能是 API 客户端（成功返回 JSON / 失败抛异常）：
  // 一律先统一到 fetch 契约，避免 `response.ok` 恒为 undefined。
  let fetchImpl = ensureFetchContract(options.fetch || globalThis.fetch);
  let settledHandler = typeof options.onTaskSettled === 'function' ? options.onTaskSettled : null;
  const documentRef = options.document || globalThis.document;
  const storage = resolveStorage(options.storage);
  const interval = Number(options.interval || POLL_INTERVAL_MS);
  let timer = null;
  let tasks = [];
  let catalog = [];
  let uiState = readTaskPanelUiState(storage);
  let collapsed = uiState.collapsed;
  let resizeBound = false;
  let dragging = false;
  let lastMarkup = null;
  // BUG-TASK-PROGRESS-001：task_id → 进度样本（纯 UI 状态，不持久化、不写任何业务状态）。
  const samplesById = new Map();
  // 上一次看到的任务状态：用于识别"刚刚完成"，从而只刷新一次正式结果。
  let lastStatuses = new Map();

  const known = () => readStoredTaskIds(storage);

  /** 任务完成后的业务收尾：重新读取正式 workflow 快照（由壳层注入，见 main.js）。 */
  function notifyBusinessResult(task, previousStatus) {
    if (typeof settledHandler !== 'function') return;
    try {
      const handled = settledHandler({task, previous_status: previousStatus || null});
      // 刷新失败必须让用户看见：结果已经算完，但界面还没拿到——绝不静默。
      if (handled && typeof handled.catch === 'function') {
        handled.catch(error => showPanelError('任务已完成，但刷新正式结果失败：' + (error?.message || error)));
      }
    } catch (error) {
      showPanelError('任务已完成，但刷新正式结果失败：' + (error?.message || error));
    }
  }

  function viewport() {
    const view = documentRef?.defaultView || globalThis;
    return {width: Number(view?.innerWidth) || 0, height: Number(view?.innerHeight) || 0};
  }

  function applyPosition(host, persist = false) {
    if (!host || uiState.x === null || uiState.y === null) return;
    const rect = host.getBoundingClientRect();
    const clamped = clampTaskPanelPosition(uiState, viewport(), rect);
    uiState = {...uiState, ...clamped};
    host.style.left = `${clamped.x}px`;
    host.style.top = `${clamped.y}px`;
    host.style.right = 'auto';
    host.style.bottom = 'auto';
    if (persist) writeTaskPanelUiState(uiState, storage);
  }

  function applyCollapsedState(host) {
    const content = documentRef?.getElementById('cnsTaskPanelContent');
    const toggle = documentRef?.getElementById('cnsTaskToggle');
    if (content) content.hidden = collapsed;
    if (toggle) toggle.textContent = collapsed ? '展开' : '收起';
    if (host) host.dataset.collapsed = String(collapsed);
  }

  function bindDrag(host) {
    const handle = host?.querySelector?.('.cns-task-panel-head');
    if (!handle) return;
    handle.onpointerdown = event => {
      if (event.button !== undefined && event.button !== 0) return;
      if (event.target?.closest?.('button')) return;
      const rect = host.getBoundingClientRect();
      const start = {pointerX: event.clientX, pointerY: event.clientY, x: rect.left, y: rect.top};
      dragging = true;
      handle.setPointerCapture?.(event.pointerId);
      handle.dataset.dragging = 'true';
      handle.onpointermove = move => {
        if (handle.dataset.dragging !== 'true') return;
        const next = clampTaskPanelPosition(
          {x: start.x + move.clientX - start.pointerX, y: start.y + move.clientY - start.pointerY},
          viewport(), host.getBoundingClientRect(),
        );
        uiState = {...uiState, ...next};
        host.style.left = `${next.x}px`; host.style.top = `${next.y}px`;
        host.style.right = 'auto'; host.style.bottom = 'auto';
      };
      const finish = up => {
        if (handle.dataset.dragging !== 'true') return;
        handle.dataset.dragging = 'false';
        dragging = false;
        handle.releasePointerCapture?.(up.pointerId);
        writeTaskPanelUiState(uiState, storage);
      };
      handle.onpointerup = finish;
      handle.onpointercancel = finish;
      event.preventDefault?.();
    };
  }

  function mount() {
    if (!documentRef) return null;
    let host = documentRef.getElementById('cnsTaskCenter');
    if (!host) {
      host = documentRef.createElement('aside');
      host.id = 'cnsTaskCenter';
      host.className = 'cns-task-center';
      documentRef.body.appendChild(host);
    }
    const nowMs = Date.now();
    const markup = panelHtml(tasks, catalog, {samplesById: Object.fromEntries(samplesById), nowMs});
    // Polling is frequent, but an unchanged task snapshot must not destroy focus,
    // pointer capture or an in-progress drag.  Rebuild only when visible task data changes.
    if (lastMarkup === markup && documentRef.getElementById('cnsTaskPanel')) {
      applyCollapsedState(host);
      applyPosition(host);
      publishHeader();
      return host;
    }
    host.innerHTML = markup;
    lastMarkup = markup;
    const toggle = documentRef.getElementById('cnsTaskToggle');
    if (toggle) {
      toggle.onclick = () => {
        collapsed = !collapsed;
        uiState = {...uiState, collapsed};
        applyCollapsedState(host);
        applyPosition(host, true);
      };
    }
    applyCollapsedState(host);
    applyPosition(host);
    bindDrag(host);
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
        // BUG-TASK-FEEDBACK-001：提交期间按钮必须自述状态，不能点了没反应。
        const original = button.textContent;
        try {
          button.disabled = true;
          button.textContent = '正在提交…';
          await submitTaskType(button.dataset.taskType, {}, fetchImpl);
          if (documentRef.body.contains(button)) button.textContent = '已提交，正在排队…';
          await refresh();
        } catch (error) {
          showPanelError('任务提交失败：' + (error.message || error));
        } finally {
          if (documentRef.body.contains(button)) {
            button.disabled = false;
            button.textContent = original;
          }
        }
      };
    }
    publishHeader();
    return host;
  }

  /**
   * 顶栏常驻徽标（BUG-TASK-FEEDBACK-001 第 3 条）：
   * 只要有任何任务在排队 / 计算，就显示「N 个任务正在计算 · 业务名」，
   * 但**不**强制展开大面板、不遮挡页面。点击徽标即展开 / 收起任务面板。
   */
  function publishHeader() {
    const badge = documentRef?.getElementById?.('taskStatusBadge');
    if (!badge) return;
    const summary = activeTaskSummary(tasks);
    if (!summary.count) {
      badge.hidden = true;
      badge.textContent = '';
      badge.removeAttribute('data-active-count');
      return;
    }
    badge.hidden = false;
    badge.dataset.activeCount = String(summary.count);
    badge.textContent = summary.text;
    badge.title = summary.text + '（点击展开 / 收起后台任务面板）';
    badge.onclick = () => {
      collapsed = !collapsed;
      uiState = {...uiState, collapsed};
      const host = documentRef.getElementById('cnsTaskCenter');
      if (host) {applyCollapsedState(host);applyPosition(host, true);}
    };
  }

  function showPanelError(message) {
    const panel = documentRef?.getElementById('cnsTaskErrorArea');
    if (!panel) return;
    panel.className = 'cns-task-error-area cns-task-error';
    panel.textContent = message;
  }

  async function refresh() {
    const ids = known();
    const [all, available] = await Promise.all([
      loadTasks(fetchImpl),
      catalog.length ? Promise.resolve(catalog) : loadTaskCatalog(fetchImpl).catch(() => []),
    ]);
    catalog = available;
    const allTasks = Array.isArray(all) ? all : [];
    // 恢复语义：刷新页面后按 task_id 找回状态；同时展示当前活跃任务。
    // BUG-TASK-FEEDBACK-001 根因：修复前这里先按 previous 状态过滤，于是**刚刚成功**的任务
    // 在变成 succeeded 的那一次轮询就被从 tasks 里剔除，
    //   1) 用户看不到「已完成，结果已更新」；
    //   2) "活跃 → 成功"的跨越判定永远不成立，正式结果自动刷新永不触发（用户必须自己 F5）。
    // 现在先在后端**全量**任务上做跨越判定，再决定展示列表。
    const nowMs = Date.now();
    const statuses = new Map();
    for (const task of allTasks) {
      const key = String(task?.task_id || '');
      if (!key) continue;
      const status = String(task?.advanced?.status || '');
      statuses.set(key, status);
      // 只在"从活跃 → 成功"这一跨越时刷新正式结果：失败 / 取消不刷新业务状态。
      // 判定基于上一次轮询看到的状态，因此重复轮询不会重复刷新。
      const previous = lastStatuses.get(key);
      if (status === 'succeeded' && previous !== undefined && isActiveStatus(previous)) {
        notifyBusinessResult(task, previous);
      }
    }
    lastStatuses = statuses;
    // 展示列表：活跃任务 ∪ 本浏览器记录过的 task_id（succeeded 也会保留一条"已完成"结论）。
    tasks = allTasks.filter((task) => isActiveStatus(task.advanced?.status) || ids.includes(task.task_id));
    tasks.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    // 进度采样：只有进度实际增长时才记录，ETA 才会可靠（否则显示"暂无法估算"）。
    for (const task of tasks) {
      const key = String(task.task_id || '');
      if (!key || String(task.advanced?.status) !== 'running') continue;
      if (!samplesById.has(key)) samplesById.set(key, []);
      recordProgressSample(samplesById.get(key), task, nowMs);
    }
    // 清理已不在列表里的样本，避免长时间运行后无界增长。
    const alive = new Set(tasks.map(task => String(task.task_id || '')));
    for (const key of Array.from(samplesById.keys())) if (!alive.has(key)) samplesById.delete(key);
    if (!dragging) mount();
    else publishHeader();
    return tasks;
  }

  function start() {
    if (timer !== null) return;
    refresh().catch(() => {});
    timer = setInterval(() => { refresh().catch(() => {}); }, interval);
    const view = documentRef?.defaultView || globalThis;
    if (!resizeBound && view?.addEventListener) {
      view.addEventListener('resize', clampMountedPanel);
      resizeBound = true;
    }
  }

  function clampMountedPanel() {
    const host = documentRef?.getElementById('cnsTaskCenter');
    applyPosition(host, true);
  }

  function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    const view = documentRef?.defaultView || globalThis;
    if (resizeBound && view?.removeEventListener) view.removeEventListener('resize', clampMountedPanel);
    resizeBound = false;
  }

  return {
    start, stop, refresh, mount,
    /** 装配时替换请求实现（main.js 注入带会话令牌的 API 客户端；自动适配 fetch 契约）。 */
    setFetch(next) { if (typeof next === 'function') fetchImpl = ensureFetchContract(next); return fetchImpl; },
    /** 装配时替换"任务成功后刷新正式结果"的回调。 */
    setSettledHandler(next) { settledHandler = typeof next === 'function' ? next : null; return settledHandler; },
    get tasks() { return tasks; },
  };
}

/**
 * 唯一装配点：由 `main.js` 注入壳层能力（**不**新建第二套任务框架）。
 *
 * 任务面板的**唯一启动点**就在这里：必须在 API 客户端（带会话令牌）就绪之后才轮询，
 * 否则首次 `/api/tasks` 会因缺令牌被拒（403「无效会话」），面板会先闪一条假错误。
 *
 * @param {object} options
 * @param {(url:string, options?:object)=>Promise<any>} options.request
 *   既有 API 客户端：任务端点与业务端点共用同一个会话令牌 / revision 语义。
 * @param {() => Promise<any>} options.refreshWorkflow
 *   任务成功后的正式结果刷新（重读 workflow 快照并重渲染，用户**不需要** F5）。
 */
export function setupTaskCenter({request, refreshWorkflow} = {}) {
  const center = globalThis.__CNS_TASK_CENTER__ || createTaskCenter();
  globalThis.__CNS_TASK_CENTER__ = center;
  if (typeof request === 'function') center.setFetch(request);
  if (typeof refreshWorkflow === 'function') center.setSettledHandler(refreshWorkflow);
  center.start();
  return center;
}
