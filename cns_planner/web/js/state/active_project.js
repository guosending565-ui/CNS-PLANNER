/**
 * A3：`GET /api/project/active` 轻量投影的**纯逻辑**落地（BUG-PROJECT-OPEN-A3）。
 *
 * 这个 endpoint 只回答"当前 active project 是谁、workflow revision 是多少、写请求需要的
 * 会话 token 是什么"这一组装配事实；它**不是**完整 `/api/state`，也没有 workflow 快照。
 * 因此它的落地必须与 `applyState()`（完整 state）严格分开：
 *
 *   * 这里只更新 `token` / `project_storage` / workflow revision 与 project 摘要；
 *   * **不**触碰 `layers` / `paths` / `bounds` / 逐 cell 明细 / 地图视图；
 *   * `state.revision`（`/api/render?rev=` 的**渲染**缓存版本）与 `flow.revision`
 *     （workflow 乐观锁版本）是两个不同的版本号，绝不互相覆盖。
 *
 * 纯函数：不读 DOM、不写全局、不发请求，因此可以在 node 环境直接验证。
 */

/** 归一化轻量投影（缺失字段一律保持 `null`，绝不编造）。 */
export function normalizeActiveProject(active) {
  if (!active || typeof active !== 'object') return null;
  const raw = Number(active.workflow_revision ?? active.revision);
  return {
    token: typeof active.token === 'string' && active.token ? active.token : '',
    revision: Number.isFinite(raw) ? raw : null,
    project_storage: active.project_storage && typeof active.project_storage === 'object'
      ? active.project_storage : null,
    project: active.project && typeof active.project === 'object' ? active.project : null,
    workspace: active.workspace && typeof active.workspace === 'object' ? active.workspace : null,
    grid: active.grid && typeof active.grid === 'object' ? active.grid : null,
    workflow_fingerprint: typeof active.workflow_fingerprint === 'string'
      ? active.workflow_fingerprint : null,
  };
}

/**
 * 把轻量投影合并进当前 `state` / `flow`（返回新对象，绝不就地改写入参）。
 *
 * @param {{state:object|null, flow:object|null, active:object}} input
 * @returns {{ok:boolean, state:object|null, flow:object|null, revisionChanged:boolean,
 *   revision:number|null, identity:string, project:object|null}}
 */
export function applyActiveProject({ state, flow, active } = {}) {
  const normalized = normalizeActiveProject(active);
  if (!normalized) {
    return { ok: false, state: state || null, flow: flow || null, revisionChanged: false, revision: null, identity: '', project: null };
  }
  const previousRevision = Number.isFinite(Number(flow?.revision)) ? Number(flow.revision) : null;
  // 会话令牌只能来自这里：轻量启动绝不允许把 token 丢掉（否则随后的写请求全部 403）。
  const nextState = {
    ...(state || {}),
    token: normalized.token || state?.token || '',
    workflow_revision: normalized.revision,
    project_storage: normalized.project_storage || state?.project_storage,
  };
  let nextFlow = flow;
  if (flow && typeof flow === 'object') {
    nextFlow = { ...flow };
    if (normalized.revision !== null) nextFlow.revision = normalized.revision;
    if (normalized.project) nextFlow.project = { ...(flow.project || {}), ...normalized.project };
  }
  const storage = normalized.project_storage || {};
  return {
    ok: true,
    state: nextState,
    flow: nextFlow,
    revisionChanged: Boolean(
      normalized.revision !== null && previousRevision !== null
      && normalized.revision !== previousRevision
    ),
    revision: normalized.revision,
    identity: String(storage.file || storage.directory || '').trim(),
    project: normalized.project,
  };
}
