/**
 * =========================================================================
 * 工作区 / 标准网格 / 高度层的**唯一**前端就绪判读（BUG-WORKSPACE-RESTORE-002）
 * =========================================================================
 *
 * 为什么需要单独一个模块
 * ----------------------
 * 修复前，Step01 / Step02 / Step03 / 地图 / 约束场各自手写自己的存在性判断：
 *
 *   * `workspaceMappingSummary()` 只看 `flow.workspace`
 *   * `standardGridPanel()`      只看 `grid.status==='passed'`
 *   * `constraintFieldPanel()`   只看 `altitudeLayerId && grid.status==='passed'`
 *   * `updateGridNotice()`       只看 `grid.status && gridRenderCache.cells.length`
 *
 * 通用 workflow 快照是 **slim** 的（逐 cell 明细外置，只有 `detail_available` +
 * `cell_count`），打开项目 / 首次 bootstrap 时 `flow` 会先后经历：
 *
 *   fresh state → fresh workflow → hydrate grid/details → render
 *
 * 于是"这一刻 flow 里有哪几个字段"会因路径不同而不同，各处判断随之分裂：
 * 右栏恢复了而地图没恢复，或者地图恢复了而 Step02 认为工作区不存在（split-brain）。
 *
 * 本模块把"项目里到底有没有有效工作区 / 有效 L8 网格 / 有效高度层目录"收敛成
 * **一个纯函数**，任何面板与地图都只读它的结论，不再各自 if/else。
 *
 * 语义边界（与后端契约逐字一致）
 * ------------------------------
 *  - **绝不编造**：没有 workspace 就是 `present=false`，没有 grid 就是 `generated=false`；
 *  - **绝不重算**：本模块只读 snapshot 字段，不发请求、不写业务状态、不重建网格；
 *  - `grid.status` 的三态（passed / blocked / 其它）如实转印，`blocked` 不算"已生成"；
 *  - `unknown != pass`：`cells_available` 只陈述"逐 cell 明细是否已在当前 flow 里"，
 *    不参与可行性判定。
 */

/** 正式业务的 canonical 空间索引层级（与后端 OPERATIONAL_GRID_LEVEL 逐字一致）。 */
export const CANONICAL_GRID_LEVEL = 8;

/** 工作区是否存在（只读 `flow.workspace` 的 bbox 与 status，不做任何推断）。 */
export function workspaceReadiness(flow) {
  const workspace = flow && typeof flow === 'object' ? flow.workspace : null;
  const bbox = Array.isArray(workspace?.bbox) ? workspace.bbox.map(Number) : null;
  const validBox = Boolean(bbox) && bbox.length === 4 && bbox.every(Number.isFinite);
  return {
    present: Boolean(workspace) && validBox,
    declared: Boolean(workspace),
    bbox: validBox ? bbox : null,
    areaKm2: Number.isFinite(Number(workspace?.area_km2)) ? Number(workspace.area_km2) : null,
    status: workspace ? String(workspace.status || '') : '',
    revision: workspace && workspace.revision !== undefined ? workspace.revision : null,
    health: workspace?.health || null,
    identity: workspace || null,
  };
}

/**
 * canonical 网格就绪判读。
 *
 * `status==='passed'` 才算"已生成"；`blocked` 如实返回并带上阻断原因（后端已落库）。
 * `cellCount` 优先取 `cell_count`（B5X slim 快照），其次 `count`，最后数 `cells`。
 */
export function gridReadiness(flow) {
  const grid = flow && typeof flow === 'object' ? flow.grid : null;
  const declared = Boolean(grid) && typeof grid === 'object';
  const status = declared ? String(grid.status || 'not_calculated') : 'not_calculated';
  const cells = Array.isArray(grid?.cells) ? grid.cells : [];
  const cellCount = Number.isFinite(Number(grid?.cell_count))
    ? Number(grid.cell_count)
    : (Number.isFinite(Number(grid?.count)) ? Number(grid.count) : cells.length);
  const actualLevel = Number(grid?.level);
  const canonicalLevel = Number.isFinite(Number(grid?.canonical_level))
    ? Number(grid.canonical_level) : CANONICAL_GRID_LEVEL;
  return {
    declared,
    generated: status === 'passed',
    blocked: status === 'blocked',
    status,
    cellCount,
    cellCountKnown: Number.isFinite(cellCount) && cellCount > 0,
    cellsAvailable: cells.length > 0,
    actualLevel: Number.isFinite(actualLevel) ? actualLevel : null,
    canonicalLevel,
    coarsened: grid?.coarsened === true || (Number.isFinite(actualLevel) && actualLevel !== canonicalLevel),
    // 逐 cell 明细是否需要（且可以）按需 hydrate：slim 快照会声明 detail_available。
    detailAvailable: grid?.detail_available === true
      || (Array.isArray(grid?.cells) && grid.cells.length > 0),
    blockedCode: grid?.blocked_code || grid?.error?.code || '',
    blockedMessage: grid?.blocked_message || grid?.error?.message || '',
    requiredCells: Number.isFinite(Number(grid?.required_cells)) ? Number(grid.required_cells) : null,
    maxCells: Number.isFinite(Number(grid?.max_cells)) ? Number(grid.max_cells) : null,
  };
}

/** 高度层目录（只读 `flow.spatial_3d.altitude_layers`；过滤非对象项，不补默认层）。 */
export function altitudeLayerReadiness(flow) {
  const raw = flow?.spatial_3d?.altitude_layers;
  const layers = Array.isArray(raw) ? raw.filter(item => item && typeof item === 'object') : [];
  return {total: layers.length, layers, present: layers.length > 0};
}

/**
 * 统一的项目空间就绪投影。Step02 / Step03 / 地图 / 约束场都只读这一个结论，
 * 因此不会出现"右栏认为有工作区、约束场认为没有"这种 split-brain。
 */
export function workspaceGridReadiness(flow) {
  const workspace = workspaceReadiness(flow);
  const grid = gridReadiness(flow);
  const altitude = altitudeLayerReadiness(flow);
  return {
    workspace,
    grid,
    altitude,
    // 约束场 / 网格相关操作的门禁：**只**看工作区与 canonical 网格，不看明细是否已 hydrate。
    canOperateGrid: workspace.present && grid.generated,
    hasWorkspace: workspace.present,
    hasGrid: grid.generated,
    hasAltitudeLayers: altitude.present,
  };
}

/**
 * 给用户看的中文业务原因（**唯一**取词点）。
 *
 * 只要调用方按 `code` 取词，任何面板都不会再出现"该高度层尚未生成"与
 * "请先框选并保存工作区"同时显示这种自相矛盾的组合。
 */
export const WORKSPACE_GRID_REASON_TEXT = {
  workspace_missing: '尚未保存工作区范围：请在第 02 步「工作区范围」框选并保存分析范围。',
  workspace_bbox_invalid: '已保存的工作区缺少有效 bbox：请重新框选并保存工作区范围。',
  grid_missing: '尚未生成标准规划网格：请先框选并保存工作区范围（保存后会在 MH/T 4063.1 L8 上生成）。',
  grid_blocked: '标准规划网格已阻断：请缩小工作区范围，或显式提高 max_cells 资源上限后重新保存工作区。',
  grid_coarsened: '当前网格实际层级低于 canonical L8（旧项目快照）：请重新保存工作区范围以在 L8 上重建空间索引。',
  altitude_missing: '高度层目录为空：请先在「环境与风险 → 高级 → 高度层」按工程依据补建 AltitudeLayer。',
  altitude_not_selected: '尚未选择固定巡航高度层：请先在上方「固定巡航高度层」中选择一个高度层。',
  altitude_usable: '',
  ready: '',
};

/**
 * 按优先级给出**当前真正缺什么**（第一个不满足的门禁）。
 *
 * @param {object} readiness `workspaceGridReadiness(flow)` 的结果
 * @param {{altitudeLayerId?:string, altitudeUsable?:boolean, requireAltitude?:boolean}} [options]
 *   `requireAltitude=false` 只检查"工作区 + canonical 网格"两级门禁（例如"下一步"推进）。
 * @returns {{code:string, text:string, ready:boolean}}
 */
export function workspaceGridReason(readiness, {
  altitudeLayerId = '', altitudeUsable = false, requireAltitude = true,
} = {}) {
  const text = code => String(WORKSPACE_GRID_REASON_TEXT[code] || '');
  const workspace = readiness?.workspace || {};
  const grid = readiness?.grid || {};
  const altitude = readiness?.altitude || {};
  if (!workspace.present) {
    const code = workspace.declared ? 'workspace_bbox_invalid' : 'workspace_missing';
    return {code, text: text(code), ready: false};
  }
  if (grid.blocked) return {code: 'grid_blocked', text: text('grid_blocked'), ready: false};
  if (!grid.generated) return {code: 'grid_missing', text: text('grid_missing'), ready: false};
  if (grid.coarsened) return {code: 'grid_coarsened', text: text('grid_coarsened'), ready: false};
  if (!requireAltitude) return {code: 'ready', text: '', ready: true};
  if (!altitude.present) return {code: 'altitude_missing', text: text('altitude_missing'), ready: false};
  if (!String(altitudeLayerId || '')) {
    return {code: 'altitude_not_selected', text: text('altitude_not_selected'), ready: false};
  }
  if (!altitudeUsable) {
    return {
      code: 'altitude_not_confirmed',
      text: '已选高度层尚未完成工程确认：请先补齐 nominal 高度与垂向基准并确认。',
      ready: false,
    };
  }
  return {code: 'ready', text: '', ready: true};
}
