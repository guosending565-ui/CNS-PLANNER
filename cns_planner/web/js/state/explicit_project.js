export const LAST_EXPLICIT_PROJECT_KEY = 'cns.lastExplicitProject.v1';

function resolveStorage(storage) {
  if (storage) return storage;
  try { return globalThis.document?.defaultView?.localStorage || globalThis.localStorage; }
  catch (_) { return null; }
}

export function readLastExplicitProject(storage) {
  storage=resolveStorage(storage);
  try {
    const value = JSON.parse(storage?.getItem(LAST_EXPLICIT_PROJECT_KEY) || 'null');
    if (!value || typeof value.directory !== 'string' || !value.directory.trim()) return null;
    return {
      directory: value.directory.trim(),
      project_name: String(value.project_name || ''),
      recorded_at: String(value.recorded_at || ''),
    };
  } catch (_) { return null; }
}

export function recordExplicitProject(state, storage, now = () => new Date().toISOString()) {
  storage=resolveStorage(storage);
  const directory = String(state?.project_storage?.directory || '').trim();
  if (!directory || state?.project_storage?.automatic === true) return null;
  const value = {
    directory,
    project_name: String(state?.workflow?.project?.name || ''),
    recorded_at: now(),
  };
  try { storage?.setItem(LAST_EXPLICIT_PROJECT_KEY, JSON.stringify(value)); } catch (_) { /* recovery cache only */ }
  return value;
}

/**
 * Bootstrap contract: server state wins.  Only an automatic/empty server project may
 * restore the one directory this browser recorded after a successful Save As/Open.
 * The caller invokes this once per page load, so a failure cannot create a retry loop.
 *
 * BUG-PROJECT-RESTORE-001：本函数的调用者**必须**在服务器状态已经落地（`update(state)`
 * 已执行、API 客户端的 token getter 能取到 `/api/state` 下发的会话令牌）之后再调用它。
 * 恢复请求走 `POST /api/project/open`，服务端要求 `X-CNS-Token` / `X-CNS-Revision`
 * 与**当前**会话一致；在 token 就绪之前发起，只会得到 403「无效会话或来源」。
 * 启动编排见 `state/bootstrap.js`。
 */
export async function restoreLastExplicitProject(serverState, {
  api, storage, now,
} = {}) {
  storage=resolveStorage(storage);
  const storageState = serverState?.project_storage || {};
  if (!storageState.automatic && storageState.directory) {
    recordExplicitProject(serverState, storage, now);
    return {state: serverState, restored: false, attempted: false, error: null};
  }
  const cached = readLastExplicitProject(storage);
  if (!cached || typeof api !== 'function') {
    return {state: serverState, restored: false, attempted: false, error: null};
  }
  try {
    const restoredState = await api('/api/project/open', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project_dir: cached.directory}),
    });
    recordExplicitProject(restoredState, storage, now);
    return {state: restoredState, restored: true, attempted: true, error: null};
  } catch (error) {
    return {state: serverState, restored: false, attempted: true, error};
  }
}
