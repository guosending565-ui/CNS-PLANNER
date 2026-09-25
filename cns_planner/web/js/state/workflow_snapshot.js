/**
 * BUG-GRID-POPUP-001 修复：workflow 快照应用 / 网格明细 hydrate 的**唯一**路径。
 *
 * 背景（已复现的真实缺陷）
 * ------------------------
 *
 * `WorkflowService.snapshot()` 出于性能考虑会对 `grid_attributes` 做 `slim_grid_attributes()`
 * —— 删掉 `grid_attributes.*.cells`，只留摘要与 `detail_available`。逐 cell 明细只能通过
 * 专用接口取得：
 *
 *   GET /api/workspace/grid            → grid（cells）
 *   GET /api/workspace/grid/attributes → grid_attributes（含完整 cells）
 *
 * 修复前 `main.js` 有两套互不相同的 flow 写入方式：
 *   * `applyWorkflow()`（`/api/workflow`、`mutate`）→ 写入后调用 `syncGridApis()`，明细被 hydrate；
 *   * `resourceAction()` → 直接把 POST 返回的 snapshot 赋给 flow，**从不** hydrate。
 *
 * 于是任何走 `resourceAction` 的操作（Step02/03/04/05 都大量使用）之后，
 * `flow.grid_attributes.population.cells` 被一个 slim snapshot 覆盖成 `undefined`：
 * 右侧摘要仍然是「人口映射：通过 / 5510/5510」，而 `buildGridOverlayCache()` 再也拿不到
 * population cell，`item.population` 变成 `null`，点击任意 L8 格就显示「人口：缺少数据」。
 *
 * 本模块把「完整 workflow snapshot → 当前 flow → 网格明细 hydrate」固定成**一条**路径，
 * 并保证：
 *
 *  1. 通用 `/api/workflow` 快照**继续保持 slim**（不为了 popup 把上万 cells 塞回通用快照）；
 *  2. 只要当前已有 grid 且 `grid_attributes.*.detail_available`，就必然重新 hydrate；
 *  3. 异步竞态由单调递增的 `gridDataSerial` 独占裁决：旧响应一律丢弃，
 *     既不会覆盖新工作区，也不会把旧工作区的 detail 套到新工作区；
 *  4. hydrate 只改前端展示用的 flow 视图，不写任何业务状态、不调用任何 mutation 端点；
 *  5. 若 mapping 整体 status=passed 但某个 grid_id 在 cells 中缺失，本模块**不**静默掩盖：
 *     显式产出 `population_cell_missing_for_grid_id` 诊断，绝不把结构错误说成"缺少数据"。
 *
 * 本模块是纯逻辑（依赖通过参数注入），因此可以在 node 环境直接测试。
 */

/** grid 明细的稳定身份（诊断与测试用，不参与竞态裁决）。 */
export function gridDetailIdentity(grid, attributes) {
  const cells = grid?.cells || [];
  const keys = Object.keys(attributes?.population?.cells || {});
  return [
    String(cells.length),
    cells.length ? String(cells[0]?.grid_id ?? '') : '',
    cells.length ? String(cells[cells.length - 1]?.grid_id ?? '') : '',
    String(keys.length),
  ].join('|');
}

/** 当前 flow 是否确实带着逐 cell 明细（而不是只有一个 slim 摘要）。 */
export function hasGridDetail(flow) {
  const attributes = flow?.grid_attributes;
  if (!attributes || typeof attributes !== 'object') return false;
  for (const value of Object.values(attributes)) {
    if (value && typeof value === 'object' && value.cells && Object.keys(value.cells).length) {
      return true;
    }
  }
  return false;
}

/** 当前 flow 是否声明「明细可以从专用接口取回」。 */
export function declaresDetailAvailable(flow) {
  // Phase4-B5X：grid 明细统一外置后，通用快照只带 summary + detail_available，
  // 因此"可以取回明细"这一声明既可能挂在 grid 上，也可能挂在网格属性命名空间上。
  if (flow?.grid?.detail_available === true) return true;
  const attributes = flow?.grid_attributes;
  if (!attributes || typeof attributes !== 'object') return false;
  return Object.values(attributes).some(
    value => value && typeof value === 'object' && value.detail_available === true
  );
}

/** 是否有 grid 可以 hydrate（没有 grid 就没有明细可拉）。 */
export function hasGrid(flow) {
  const grid = flow?.grid;
  if (!grid || typeof grid !== 'object') return false;
  const cells = grid.cells;
  if (Array.isArray(cells) && cells.length > 0) return true;
  // B5X：通用快照里的 grid 是摘要（含 cell_count / detail_available），
  // 逐 cell 明细只能从 GET /api/workspace/grid 取回。
  return Number(grid.cell_count) > 0 || grid.detail_available === true;
}

/** 该 snapshot 是否应当触发一次 grid 明细 hydrate。 */
export function needsGridHydration(flow) {
  if (!hasGrid(flow)) return false;
  // slim 快照会声明 detail_available；未声明（未计算 / 旧后端）时不发无意义请求。
  return declaresDetailAvailable(flow);
}

/**
 * 逐 cell 结构自检（**不修数据、不补 0**，只报告结构错误）。
 *
 * 判定语义：mapping 整体 `status==='passed'` 时，每个 grid_id 都必须能在
 * `attributes.population.cells` 里找到一条记录（值为 0 也必须是**存在**的记录）。
 * 缺失即返回 `population_cell_missing_for_grid_id`，绝不静默退化为"缺少数据"。
 */
export function populationCellStructureDiagnostics(grid, attributes) {
  const population = attributes?.population;
  if (!population || typeof population !== 'object') return [];
  if (String(population.status) !== 'passed') return [];
  const cells = population.cells;
  if (!cells || typeof cells !== 'object') {
    return [{
      code: 'population_cells_absent_while_status_passed',
      detail: 'population.status=passed 但 grid_attributes.population.cells 不存在',
      grid_count: (grid?.cells || []).length,
    }];
  }
  const missing = [];
  for (const cell of grid?.cells || []) {
    const gridId = String(cell?.grid_id ?? '');
    if (!gridId) continue;
    if (!Object.prototype.hasOwnProperty.call(cells, gridId)) missing.push(gridId);
  }
  if (!missing.length) return [];
  return [{
    code: 'population_cell_missing_for_grid_id',
    detail: 'population 映射 status=passed，但这些 grid_id 在 population.cells 中不存在',
    missing_count: missing.length,
    grid_count: (grid?.cells || []).length,
    first_missing_grid_ids: missing.slice(0, 10),
  }];
}

/**
 * 建立「applyWorkflowSnapshot / hydrateGridDetail」两件套。
 *
 * @param {object} deps
 * @param {() => object|null} deps.getFlow        读取当前 flow
 * @param {(flow:object) => void} deps.setFlow    写入当前 flow（唯一写入点）
 * @param {() => number} deps.nextSerial          取下一个 grid 请求序号（单调递增）
 * @param {() => number} deps.currentSerial       读当前 grid 请求序号
 * @param {() => Promise<object>} deps.fetchGrid        GET /api/workspace/grid
 * @param {() => Promise<object>} deps.fetchAttributes  GET /api/workspace/grid/attributes
 * @param {(message:string) => void} [deps.onError]
 * @param {(flow:object) => void} [deps.afterApply]    视图重建（render / paint）钩子
 */
export function createWorkflowSnapshotApplier(deps) {
  const {
    getFlow, setFlow, nextSerial, currentSerial, fetchGrid, fetchAttributes,
    // Phase4-B5X：其余外置型结果的按需读取入口（可选依赖，未提供时跳过）。
    fetchRisk = null, fetchRiskV2 = null, fetchLayeredCandidates = null,
    onError = () => {}, afterApply = () => {},
  } = deps;
  for (const [name, fn] of Object.entries({
    getFlow, setFlow, nextSerial, currentSerial, fetchGrid, fetchAttributes,
  })) {
    if (typeof fn !== 'function') throw new Error('createWorkflowSnapshotApplier 缺少依赖：' + name);
  }

  /** 最近一次成功 hydrate 的身份（只作诊断 / 测试可见性，不参与竞态裁决）。 */
  let hydrated = null;

  /**
   * 从专用接口 hydrate 逐 cell 明细。
   *
   * 竞态裁决**只**看 serial：请求发出时取号，响应回来时若序号已过期
   * （期间又发生了新的工作区 / 明细请求），直接丢弃，绝不覆盖更新的 flow。
   * 这与 `syncGridApis()` 既有的 `gridDataSerial` 防竞态机制是同一条语义。
   */
  async function hydrateGridDetail() {
    const serial = nextSerial();
    const [grid, attributes] = await Promise.all([fetchGrid(), fetchAttributes()]);
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    const current = getFlow() || {};
    setFlow({...current, grid, grid_attributes: attributes});
    hydrated = gridDetailIdentity(grid, attributes);
    return {
      applied: true,
      identity: hydrated,
      diagnostics: populationCellStructureDiagnostics(grid, attributes),
    };
  }

  /**
   * B5X：一次性 hydrate 所有**已外置**的大型明细（网格 / 风险 / 候选）。
   *
   * 通用快照只带 summary + `detail_available` + `detail_endpoint`；逐 cell 明细
   * 一律走专用 GET。任何一步失败都不写业务状态，只通过 `onError` 给出中文提示。
   */
  async function hydrateExternalDetail() {
    const snapshot = getFlow() || {};
    const plan = [];
    if (needsGridHydration(snapshot)) {
      plan.push(['grid', () => Promise.all([fetchGrid(), fetchAttributes()])]);
    }
    if (typeof fetchRisk === 'function' && snapshot.grid_risk?.detail_available === true) {
      plan.push(['grid_risk', () => fetchRisk()]);
    }
    if (typeof fetchRiskV2 === 'function' && snapshot.grid_risk_v2?.detail_available === true) {
      plan.push(['grid_risk_v2', () => fetchRiskV2()]);
    }
    if (typeof fetchLayeredCandidates === 'function'
        && snapshot.layered_route_candidates?.detail_available === true) {
      plan.push(['layered_route_candidates', () => fetchLayeredCandidates()]);
    }
    if (!plan.length) return {applied: false, reason: 'no_detail'};
    const serial = nextSerial();
    const results = await Promise.all(plan.map(([, run]) => run()));
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    const current = getFlow() || {};
    const patch = {};
    let diagnostics = [];
    plan.forEach(([name], index) => {
      const value = results[index];
      if (name === 'grid') {
        patch.grid = value[0];
        patch.grid_attributes = value[1];
        diagnostics = populationCellStructureDiagnostics(value[0], value[1]);
      } else {
        patch[name] = value;
      }
    });
    setFlow({...current, ...patch});
    hydrated = gridDetailIdentity(patch.grid || current.grid, patch.grid_attributes || current.grid_attributes);
    return {applied: true, identity: hydrated, diagnostics, hydrated: plan.map(item => item[0])};
  }

  /**
   * 唯一的完整 workflow snapshot 应用路径。
   *
   * 语义：先原样安装 snapshot（它可能是 slim 的），**再**按需 hydrate 逐 cell 明细，
   * 最后才 render / paint —— 因此用户不会看到「摘要 passed 但 popup 缺数据」的中间态。
   */
  async function applyWorkflowSnapshot(snapshot, {hydrate = true} = {}) {
    if (!snapshot || typeof snapshot !== 'object') return getFlow();
    setFlow(snapshot);
    if (hydrate) {
      try {
        await hydrateExternalDetail();
      } catch (exc) {
        onError('专题明细同步失败：' + (exc?.message || exc));
      }
    }
    afterApply(getFlow());
    return getFlow();
  }

  return {
    applyWorkflowSnapshot,
    hydrateGridDetail,
    hydrateExternalDetail,
    state: {
      get hydratedIdentity() { return hydrated; },
      reset() { hydrated = null; },
    },
  };
}
