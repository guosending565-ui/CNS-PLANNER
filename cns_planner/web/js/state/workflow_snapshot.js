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
 * 该对象是否**声明**自己是一份完整 workflow 快照。
 *
 * 契约依据（不做"像不像"的启发式猜测）：
 *   * 完整快照一定带 ``project``（后端 `WorkflowService.snapshot()` 的固定顶层键）；
 *   * 局部资源响应（validation collection / projection / preview / readiness / 删除回执）
 *     只有自己的字段。
 *
 * BUG-CONSTRAINT-UI-001 的真实上游：某个 `resourceAction(path)` 拿到的是**局部对象**，
 * 它被当成 workflow 直接安装后，``flow.project`` 立即变成 undefined，界面随即在
 * `flow.project.name` 抛 "Cannot read properties of undefined (reading 'name')"。
 * 后端数据并没有丢——只是前端把不该当快照的东西当成了快照。
 */
export function looksLikeWorkflowSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return false;
  // `schema_version` 不是 workflow 身份：PCF 配置、候选、雷达 proposal 等局部
  // 资源都有自己的 schema_version。完整 workflow 的稳定契约是顶层 `project`；
  // 放宽到 schema_version 会把一次局部保存响应安装成“未命名空项目”，直接造成
  // 右栏 / 地图 split-brain。
  return snapshot.project !== undefined;
}

//: 局部响应安装时**必须**从旧 flow 继承的顶层键（业务基础事实，局部响应永远不拥有）。
const INHERITED_SNAPSHOT_KEYS = [
  'project', 'schema_version', 'revision', 'steps', 'workspace', 'grid',
  'grid_attributes', 'grid_risk', 'grid_risk_v2', 'nodes', 'node_seq', 'route_seq',
  'scenario_routes', 'operational_routes', 'spatial_3d', 'defaults',
];

/**
 * 把一份"被当成 workflow 安装的对象"规范化。
 *
 * @returns {{flow:object, partial:boolean, inherited:string[]}}
 *   `inherited` 是**这次安装实际保留下来**的业务基础键：局部响应不该带走
 *   `project` / `workspace` / `grid` 这些事实，因此必须显式列出以便诊断。
 */
export function normalizeSnapshotInstall(current, snapshot) {
  const base = current && typeof current === 'object' ? current : {};
  if (looksLikeWorkflowSnapshot(snapshot)) return {flow: snapshot, partial: false, inherited: []};
  const incoming = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const merged = {...base, ...incoming};
  const inherited = [];
  for (const key of INHERITED_SNAPSHOT_KEYS) {
    if (key in incoming) continue;
    if (base[key] === undefined) continue;
    merged[key] = base[key];
    inherited.push(key);
  }
  return {flow: merged, partial: true, inherited};
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
    fetchRadarSurveillance = null, fetchLayeredMasks = null,
    // Round 3：P14 服务走廊逐体元 service 证据（可选依赖，未提供时跳过）。
    fetchCorridorDetail = null,
    // A4：当前项目身份（只读）。提供时，明细响应落地前必须复核身份，
    // 迟到的旧项目明细一律丢弃，绝不污染刚切换过来的新项目。
    currentProjectIdentity = null,
    onError = () => {}, afterApply = () => {},
  } = deps;
  for (const [name, fn] of Object.entries({
    getFlow, setFlow, nextSerial, currentSerial, fetchGrid, fetchAttributes,
  })) {
    if (typeof fn !== 'function') throw new Error('createWorkflowSnapshotApplier 缺少依赖：' + name);
  }

  /** 最近一次成功 hydrate 的身份（只作诊断 / 测试可见性，不参与竞态裁决）。 */
  let hydrated = null;

  /** 结构性诊断（例如"把局部响应当成 workflow 安装"）：只报告事实，不修业务数据。 */
  const diagnostics = [];

  /** A4：当前项目身份（未注入时为 null，等价于"不做身份复核"的既有行为）。 */
  function projectIdentity() {
    if (typeof currentProjectIdentity !== 'function') return null;
    try { return String(currentProjectIdentity() || ''); } catch (_) { return null; }
  }

  /** A4：明细响应是否仍属于**发起该请求时**的项目。 */
  function detailBelongsToProject(expected) {
    if (expected === null) return true;
    return projectIdentity() === expected;
  }

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
    // BUG-SHOT-009：网格逐 cell 明细落地后必须重建下游视图 —— `main.js` 的
    // ``afterApply`` 会重建 ``gridRenderCache``（约束覆盖层唯一的几何来源）并重绘。
    // 漏掉这一步时，``flow.grid.cells`` 已经有 8008 条，但渲染缓存仍是空，
    // 于是"明细读到了、地图上一个障碍格都没有"。
    afterApply(getFlow());
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
    // 与 hydrateGridDetail 同理：明细落地后必须重建下游视图（gridRenderCache / 重绘）。
    afterApply(getFlow());
    return {applied: true, identity: hydrated, diagnostics, hydrated: plan.map(item => item[0])};
  }

  /**
   * Rescue Stable map path: hydrate only the LayeredRouteCandidate sidecar.
   * Candidate geometry is externalized from the generic workflow snapshot, but
   * fetching all external detail would also download grid/risk payloads.
   */
  async function hydrateLayeredCandidateDetail() {
    const snapshot = getFlow() || {};
    if (typeof fetchLayeredCandidates !== 'function'
        || snapshot.layered_route_candidates?.detail_available !== true) {
      return {applied: false, reason: 'no_candidate_detail'};
    }
    const serial = nextSerial();
    const identity = projectIdentity();
    const candidates = await fetchLayeredCandidates();
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    // A4：项目已经切走的迟到响应直接丢弃（不写 flow、不重绘）。
    if (!detailBelongsToProject(identity)) return {applied: false, reason: 'project_changed'};
    setFlow({...getFlow(), layered_route_candidates: candidates});
    afterApply(getFlow());
    return {applied: true, hydrated: ['layered_route_candidates']};
  }

  /**
   * Round32-C：按需 hydrate **逐 cell** coarse feasibility mask 明细。
   *
   * 通用刷新路径（``/api/layered-route-candidates``）只带 mask 摘要（``cells`` 已外置，
   * ``cells_detail`` 声明读取入口）。这里只在用户真的要画该图层时拉取**所选车道**的
   * cells，绝不为了地图把整份 candidate collection（实测约 39 MB）重新拉回来。
   *
   * 与其它 hydrate 同一套竞态/身份裁决：旧响应与切走的项目一律丢弃。
   */
  async function hydrateLayeredMaskDetail(laneKey) {
    if (typeof fetchLayeredMasks !== 'function' || !laneKey) {
      return {applied: false, reason: 'no_layered_mask_detail'};
    }
    const serial = nextSerial();
    const identity = projectIdentity();
    const masks = await fetchLayeredMasks(laneKey);
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    if (!detailBelongsToProject(identity)) return {applied: false, reason: 'project_changed'};
    const current = getFlow() || {};
    const collection = current.layered_route_candidates || {};
    if (!masks || typeof masks !== 'object' || !Object.keys(masks).length) {
      return {applied: false, reason: 'no_layered_mask'};
    }
    setFlow({...current, layered_route_candidates: {
      ...collection,
      masks: {...(collection.masks || {}), ...masks},
    }});
    afterApply(getFlow());
    return {applied: true, hydrated: ['layered_route_candidates.masks']};
  }

  /** Rescue Stable map path: hydrate the current proposal-only radar geometry. */
  async function hydrateRadarSurveillanceDetail() {
    const snapshot = getFlow() || {};
    if (typeof fetchRadarSurveillance !== 'function'
        || snapshot.radar_surveillance_layout?.detail_available !== true) {
      return {applied: false, reason: 'no_radar_detail'};
    }
    const serial = nextSerial();
    const identity = projectIdentity();
    const response = await fetchRadarSurveillance();
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    if (!detailBelongsToProject(identity)) return {applied: false, reason: 'project_changed'};
    const items = Array.isArray(response?.items) ? response.items : [];
    const detail = [...items].reverse().find(item =>
      item?.demo_preview_only === true && item?.status !== 'stale'
    ) || items[items.length - 1] || null;
    const current = getFlow() || {};
    const layout = {...(current.radar_surveillance_layout || {}), detail};
    setFlow({...current, radar_surveillance_layout: layout});
    afterApply(getFlow());
    return {applied: Boolean(detail), hydrated: detail ? ['radar_surveillance_layout'] : []};
  }

  /**
   * Round 3：按需读取 P14 服务走廊的**逐体元 service 证据**（只读 GET）。
   *
   * 通用快照把逐 voxel 明细外置（``voxels`` → 计数 + ``voxels_detail``），因此
   * ``surface_class`` / ``distinct_site_count`` / ``required_distinct_site_count``
   * 这类 service 级证据只能从这里按需取回。它**不**写任何业务状态：
   * 只在 flow 的 ``cns_corridor_assessment.detail`` 上挂一份只读明细。
   */
  async function hydrateCorridorServiceDetail() {
    const snapshot = getFlow() || {};
    if (typeof fetchCorridorDetail !== 'function'
        || snapshot.cns_corridor_assessment?.detail_available !== true) {
      return {applied: false, reason: 'no_corridor_detail'};
    }
    const serial = nextSerial();
    const identity = projectIdentity();
    const response = await fetchCorridorDetail();
    if (serial !== currentSerial()) return {applied: false, reason: 'superseded'};
    if (!detailBelongsToProject(identity)) return {applied: false, reason: 'project_changed'};
    const current = getFlow() || {};
    const assessment = {...(current.cns_corridor_assessment || {}), detail: response || null};
    setFlow({...current, cns_corridor_assessment: assessment});
    afterApply(getFlow());
    return {applied: Boolean(response), hydrated: response ? ['cns_corridor_assessment'] : []};
  }

  /**
   * 唯一的完整 workflow snapshot 应用路径。
   *
   * 语义：先原样安装 snapshot（它可能是 slim 的），**再**按需 hydrate 逐 cell 明细，
   * 最后才 render / paint —— 因此用户不会看到「摘要 passed 但 popup 缺数据」的中间态。
   */
  async function applyWorkflowSnapshot(snapshot, {hydrate = true} = {}) {
    // BUG-WORKSPACE-RESTORE-002：半落地 / 失败响应可能根本没有 workflow 字段。
    // 此时（null / undefined / 非对象）**保持当前 flow 不变**，绝不用空值覆盖它
    // ——那会让右栏与地图同时认为"这个项目什么都没有"，而服务器上其实是有数据的。
    if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return getFlow();
    const install = normalizeSnapshotInstall(getFlow(), snapshot);
    setFlow(install.flow);
    if (install.partial) {
      // 只报告事实，不改写任何业务语义：局部响应不该出现在这条路径上，
      // 因此明确给出中文诊断（含继承的键名），而不是静默让 project 消失。
      diagnostics.push({
        code: 'partial_snapshot_installed',
        detail: '该对象不是完整 workflow 快照（缺少 project / schema_version）；'
          + '已按局部响应处理并继承既有业务基础事实：' + install.inherited.join('、'),
        inherited: install.inherited,
      });
    }
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
    hydrateLayeredCandidateDetail,
    hydrateLayeredMaskDetail,
    hydrateRadarSurveillanceDetail,
    hydrateCorridorServiceDetail,
    looksLikeWorkflowSnapshot,
    normalizeSnapshotInstall,
    state: {
      get hydratedIdentity() { return hydrated; },
      get diagnostics() { return diagnostics.slice(); },
      reset() { hydrated = null; diagnostics.length = 0; },
    },
  };
}
