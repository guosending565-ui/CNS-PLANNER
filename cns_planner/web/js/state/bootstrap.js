/**
 * Bootstrap 时序（BUG-PROJECT-RESTORE-001 / BUG-BOOTSTRAP-LOAD-001 的唯一修复点）。
 *
 * 修复前的真实缺陷
 * ----------------
 * `main.js` 的启动链是：
 *
 *     api('/api/state').then(data => restoreLastExplicitProject(data, {api})).then(update)
 *
 * 而 `createApiClient` 的 token getter 是 `() => state?.token`，模块级 `state` 只在
 * `update(data)` 里才被赋值。于是恢复请求 `POST /api/project/open` 必然发生在
 * `state` 仍为 `null` 的时刻：
 *
 *   * 请求头 `X-CNS-Token` 是空串 → 服务端 `api/server.py::do_POST` 的
 *     `valid_token(...)` 判定为假 → **403「无效会话或来源」**；
 *   * 而 revision 门却过得去：GET `/api/state` 的响应体带 `revision`（整数），
 *     `createApiClient` 会把它记进 `knownRevision`，所以请求被真正发出去后才被拒。
 *
 * 也就是说「恢复上次项目」在启动阶段**永不成功**，失败信息还会把用户引向
 * 「无效会话或来源」这种与真实原因无关的说法。
 *
 * 同一时刻还有第二个缺陷：那条 promise 链没有失败落地保证。`#loading`
 * （「正在加载空间数据…」）只在 `renderMap`/`bootstrapFailure` 里被隐藏，而
 * `/api/state` 与恢复 POST 都要在 QGIS 单线程上排队；一旦请求长时间不返回
 * （QGIS 线程占用 / 慢盘上源路径 stat），页面就永远停在
 * 「正在检查数据源 / 正在加载空间数据… / 正在载入业务流程」三行初始占位上，
 * 既没有可读结论，也没有任何超时兜底。
 *
 * 修复后的时序（正确语义）
 * ------------------------
 * 1. `GET /api/state`（带超时）——拿到服务器当前状态，**先**落地；
 * 2. `update(serverState)`——token / revision 就位，自动恢复项目先渲染出来；
 * 3. 服务器已持有明确项目 → 直接采用服务器项目，**不重新 open**（测试 1 / 8）；
 * 4. 服务器是自动恢复项目 + 浏览器缓存有明确目录 → 用**当前会话**的合法
 *    token / revision / origin 恰好恢复一次；失败一次即停止，不循环、不扫描（测试 3 / 5）；
 * 5. 恢复成功 → 重新取一次 fresh `/api/state`，再用 fresh state/workflow 重渲染，
 *    之后才由 flow 驱动项目相关空间数据 hydrate（测试 4）；
 * 6. 每一步失败或超时都必须落到可见状态：隐藏 `#loading` 并给出中文结论，
 *    页面稳定停在服务器项目上（测试 6）。
 *
 * 本模块是纯编排逻辑（依赖全部注入），因此可以在 node 环境直接测试。
 */

/** 单次 bootstrap 请求的默认上限（毫秒）。超时只影响"界面是否可见结论"，不取消后台工作。 */
export const DEFAULT_BOOTSTRAP_TIMEOUT_MS = 90000;

/** 超时错误的可识别名称（不依赖 DOMException，便于测试与降级判断）。 */
export const TIMEOUT_ERROR_NAME = 'CnsBootstrapTimeout';

function timeoutError(label, timeoutMs) {
  const error = new Error(
    `${label}超过 ${Math.round(timeoutMs / 1000)} 秒未返回；`
    + '后台可能仍在处理，请稍后刷新页面确认结果。',
  );
  error.name = TIMEOUT_ERROR_NAME;
  error.timeout_ms = timeoutMs;
  return error;
}

/**
 * 给一个 promise 加显式上限。超时后抛出可读的中文错误，绝不静默悬挂。
 *
 * @param {Promise<any>} promise
 * @param {{timeout_ms?: number, label?: string, signal?: AbortSignal}} [options]
 */
export function withTimeout(promise, {timeout_ms = DEFAULT_BOOTSTRAP_TIMEOUT_MS, label = '请求', signal} = {}) {
  const budget = Number(timeout_ms);
  if (!Number.isFinite(budget) || budget <= 0) return promise;
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const error = timeoutError(label, budget);
      reject(error);
      // 只用于让底层 fetch 尽早放弃；AbortController 不可用时仅拒绝上层。
      try { signal?.abort?.(error); } catch (_) { /* 超时兜底与取消互不依赖 */ }
    }, budget);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * 启动编排：服务器状态优先，需要时才做**一次**明确项目恢复。
 *
 * 依赖全部注入，函数本身不读 DOM、不读 localStorage、不直接 fetch。
 *
 * @param {object} deps
 * @param {(url:string, options?:object)=>Promise<any>} deps.api  既有 API 客户端
 * @param {(serverState:object)=>Promise<object>} deps.restore     恢复动作（explicit_project.js）
 * @param {(state:object, meta:{reason:string, result?:object})=>void|Promise<void>} deps.update 把 state 落地到界面
 * @param {number} [deps.timeout_ms]
 * @param {()=>AbortController|null} [deps.newAbortController]
 * @param {(message:string)=>void} [deps.onError]
 * @param {(message:string)=>void} [deps.onNotice]
 * @param {<(run:()=>Promise<any>)=>Promise<any>>} [deps.serialize]
 *   项目级操作串行化闸门（BUG-WORKSPACE-RESTORE-002）。**恢复请求必须在闸门内发出**，
 *   否则它可能与"上一次 update 的 workflow 明细 hydrate"交错，出现
 *   "右栏已恢复、地图未恢复"或"地图已恢复、Step02 认为工作区不存在"的 split-brain。
 * @param {()=>Promise<object>} [deps.probeActive] A3 轻量探测（`GET /api/project/active`）：
 *   服务器已持有明确项目时，首屏只等这一次毫秒级投影，完整 state 由调用方渐进加载。
 *   不提供时的行为与修复前完全一致（既有调用方与测试不受影响）。
 * @param {(active:object)=>any} [deps.landActive] A3 轻量落地（token / revision / 项目身份）。
 * @returns {Promise<{ok:boolean, state:object|null, restored:boolean, attempted:boolean,
 *   reason:string, error:Error|null, timeout:boolean, light?:boolean}>}
 *   ``light:true`` 表示本次只落地了轻量 active 投影（``probeActive`` / ``landActive`` 路径）：
 *   返回值里的 ``state`` **不是**完整 state，调用方必须随后自行加载完整 ``/api/state``。
 */
export async function bootstrapProjectState({
  api, restore, update,
  timeout_ms = DEFAULT_BOOTSTRAP_TIMEOUT_MS,
  newAbortController,
  onError = () => {},
  onNotice = () => {},
  serialize = null,
  probeActive = null,
  landActive = null,
} = {}) {
  for (const [name, value] of Object.entries({api, restore, update})) {
    if (typeof value !== 'function') throw new Error('bootstrapProjectState 缺少依赖：' + name);
  }
  // 统一 await 落地动作：它可能是同步的（旧调用点）或返回 Promise（新的串行落地）。
  const land = async (data, meta) => {
    await update(data, meta);
  };
  const queue = typeof serialize === 'function' ? serialize : run => run();
  const controller = typeof newAbortController === 'function' ? newAbortController() : null;
  const budget = {timeout_ms, signal: controller?.signal || null};

  // A3 轻量启动：服务器已经持有**明确项目**时，首屏不等待完整 /api/state。
  //
  // 实测根因：same-project / 启动恢复的 fast path 虽然已经命中，但完整 /api/state 每次都会
  // 重新投影整份 workflow snapshot（11.5-15 s、约 3.8 MB），于是"当前项目已激活"仍然要
  // 等十几秒。轻量投影只回答 token / revision / project_storage / project / workspace /
  // grid 摘要这一组装配事实，代价与项目规模无关。
  //
  // 只有"服务器确实持有明确项目"这一个分支走轻量返回：自动恢复项目仍走下面的完整流程，
  // 因为那条流程随后无论如何都要以 fresh /api/state 建立权威 flow；轻量探测本身失败时
  // 同样原样回退到完整流程，绝不因为一次探测失败改变任何既有语义。
  if (typeof probeActive === 'function' && typeof landActive === 'function') {
    let active = null;
    try {
      active = await withTimeout(Promise.resolve(probeActive()), {...budget, label: '读取当前项目'});
    } catch (_) {
      active = null;
    }
    const storage = (active && typeof active === 'object' && active.project_storage) || {};
    if (active && typeof active === 'object' && storage.automatic === false && storage.directory) {
      await landActive(active);
      return {
        ok: true, state: active, restored: false, attempted: false,
        reason: 'active_project', error: null, timeout: false, light: true,
      };
    }
  }

  let serverState;
  try {
    serverState = await withTimeout(Promise.resolve(api('/api/state')), {...budget, label: '读取服务器状态'});
  } catch (error) {
    const timeout = error?.name === TIMEOUT_ERROR_NAME;
    onError('无法连接本机地图服务：' + (error?.message || error));
    return {ok: false, state: null, restored: false, attempted: false, reason: 'state_failed', error, timeout};
  }
  if (!serverState || typeof serverState !== 'object') {
    const error = new Error('服务器状态不可用');
    onError('服务器状态不可用：/api/state 未返回有效对象。');
    return {ok: false, state: null, restored: false, attempted: false, reason: 'state_invalid', error, timeout: false};
  }

  // 先落地服务器状态：token / revision 就位，页面不再停留在初始占位。
  // 之后所有 POST（包括恢复请求）才会带上合法的 X-CNS-Token / X-CNS-Revision。
  await land(serverState, {reason: 'server_state'});

  let result;
  try {
    result = await withTimeout(
      // 恢复请求走项目串行闸门：排队等待"server_state 的 workflow hydrate"真正完成，
      // 这样恢复响应落地时不会与上一次 hydrate 交错。
      Promise.resolve(queue(() => restore(serverState))),
      {...budget, label: '恢复上次项目'},
    );
  } catch (error) {
    const timeout = error?.name === TIMEOUT_ERROR_NAME;
    result = {state: serverState, restored: false, attempted: true, error};
    onError(timeout
      ? '恢复上次项目超时：' + (error?.message || error)
      : '恢复上次项目失败：' + (error?.message || error) + '；当前保留自动恢复项目，请重新选择项目文件夹。');
    return {ok: true, state: serverState, restored: false, attempted: true, reason: 'restore_failed', error, timeout};
  }

  const restoredState = result?.state || serverState;
  if (result?.restored) {
    // 恢复成功必须重新取 fresh state（token / revision / project_storage 都属于新会话上下文），
    // 且必须在闸门内落地：fresh state 的 workflow 才是稳定态的权威 flow。
    try {
      await queue(async () => {
        const fresh = await withTimeout(Promise.resolve(api('/api/state')), {...budget, label: '读取恢复后状态'});
        await land(fresh && typeof fresh === 'object' ? fresh : restoredState, {reason: 'restored', result});
      });
    } catch (error) {
      // 取不到 fresh state 时仍用恢复响应落地：项目本身已经切过去了。
      await land(restoredState, {reason: 'restored', result});
      onNotice('已恢复上次项目；状态摘要刷新失败：' + (error?.message || error));
    }
    onNotice('已恢复上次项目');
    return {ok: true, state: restoredState, restored: true, attempted: true, reason: 'restored', error: null, timeout: false};
  }

  if (result?.attempted && result?.error) {
    onError('恢复上次项目失败：' + (result.error.message || result.error)
      + '；当前保留自动恢复项目，请重新选择项目文件夹。');
    return {ok: true, state: serverState, restored: false, attempted: true, reason: 'restore_failed', error: result.error, timeout: false};
  }

  return {
    ok: true, state: serverState, restored: false, attempted: Boolean(result?.attempted),
    reason: result?.attempted ? 'restore_skipped' : 'server_project', error: null, timeout: false,
  };
}
