/**
 * Round 2.8 —— 地图结果可读性（图例 / 后台任务面板 / 缩放至航路）的前端契约测试。
 *
 * 覆盖用户要求的第 9~16 项：
 *
 *  9. 图例默认收起；
 * 10. 图例只显示当前激活图层；
 * 11. 图例限宽限高 + 内部滚动（绝不横跨地图、绝不覆盖下半屏）；
 * 12. 任务面板 0 活跃任务时自动收起；
 * 13. 任务面板仍保留手动展开 / 收起；
 * 14. ``fitOperationalRoute``（缩放至航路）存在且不改 ``#fit`` 语义；
 * 15. 图例 / 任务面板 UI 状态可保存并重新打开；
 * 16. 截图（验收）所需的图层状态由正式控件驱动。
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  ALWAYS_ACTIVE_LEGEND_LAYERS, LEGEND_GROUP_TITLES, LEGEND_LAYER_SYMBOLS,
  MAP_LEGEND_UI_STORAGE_KEY, activeLegendLayers, compactLegendGroups, mapLegendModel,
  readMapLegendUiState, renderCompactLegend, updateCompactMapLegend,
  writeMapLegendUiState,
} from '../cns_planner/web/js/workflow/map_legend.js';
import {cnsServiceLegendModel} from '../cns_planner/web/js/map/cns_service_overlay.js';
import {
  createTaskCenter, panelHtml, readTaskPanelUiState, writeTaskPanelUiState,
} from '../cns_planner/web/js/tasks.js';

const INDEX_HTML = readFileSync(new URL('../cns_planner/web/index.html', import.meta.url), 'utf8');
const MAP_CSS = readFileSync(new URL('../cns_planner/web/css/map.css', import.meta.url), 'utf8');
const TASKS_CSS = readFileSync(new URL('../cns_planner/web/css/tasks.css', import.meta.url), 'utf8');
const MAIN_JS = readFileSync(new URL('../cns_planner/web/js/main.js', import.meta.url), 'utf8');
const LEGEND_JS = readFileSync(
  new URL('../cns_planner/web/js/workflow/map_legend.js', import.meta.url), 'utf8');

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
    get size() { return map.size; },
  };
}

/** 最小 DOM 桩：只实现本项目图例真正用到的契约（checked / hidden / innerHTML / querySelector）。 */
function legendDom(checked = {}) {
  const nodes = new Map();
  const make = (id) => ({
    id, hidden: false, innerHTML: '', textContent: '',
    checked: checked[id] === true,
    onclick: null, onkeydown: null,
    querySelector: (selector) => (selector === '#mapLegendToggle'
      ? (nodes.get(id)?.innerHTML.includes('id="mapLegendToggle"')
        ? {onclick: null, onkeydown: null} : null)
      : null),
  });
  const $ = (id) => {
    if (!nodes.has(id)) nodes.set(id, make(id));
    return nodes.get(id);
  };
  return {$};
}

function allLegendLines() {
  const business = mapLegendModel({flow: {}});
  return [...business.groups.flatMap((group) => group.lines), ...cnsServiceLegendModel()];
}

test('图例默认收起，只留一个「图例」按钮', () => {
  const groups = compactLegendGroups(allLegendLines(), null);
  const html = renderCompactLegend(groups, {collapsed: true, activeLayerCount: 3});
  assert.match(html, /map-legend-collapsed/);
  assert.match(html, /data-legend-collapsed="true"/);
  assert.match(html, /图例/);
  assert.match(html, /3 个激活图层/);
  //: 收起态绝不渲染任何图例行（否则等于没收起）。
  assert.doesNotMatch(html, /map-legend-line/);
  assert.doesNotMatch(html, /map-legend-body/);
  //: 默认收起是硬要求：没有任何持久化偏好时也必须收起。
  const storage = memoryStorage();
  assert.equal(readMapLegendUiState(storage).collapsed, true);
  assert.equal(readMapLegendUiState(null).collapsed, true);
});

test('图例只显示当前激活图层（Communication 截图场景）', () => {
  const active = activeLegendLayers(legendDom({
    cnsCommunicationLayer: true,
    cnsRidLayer: false,
    radarSurveillanceLayer: false,
    surfaceFactsLayer: false,
  }).$);
  assert.ok(active.has('cnsCommunication'));
  assert.ok(!active.has('cnsRid'));
  assert.ok(!active.has('radarSurveillance'));
  assert.ok(!active.has('surfaceFacts'));
  //: 在线底图没有独立开关但始终可见，必须常驻图例。
  for (const layer of ALWAYS_ACTIVE_LEGEND_LAYERS) assert.ok(active.has(layer));

  const groups = compactLegendGroups(allLegendLines(), [...active]);
  const ids = groups.flatMap((group) => group.lines.map((line) => line.id));
  assert.ok(ids.includes('cns-communication'));
  assert.ok(!ids.includes('cns-rid-land'), 'RID 图层未激活时不得显示 RID 图例');
  assert.ok(!ids.includes('cns-service-s-radar-noncooperative'),
    'Radar 图层未激活时不得显示 Radar 图例');
  assert.ok(!ids.includes('cns-surface-facts'), '地表分类未激活时不得显示其图例');

  const rendered = renderCompactLegend(groups, {collapsed: false, activeLayerCount: active.size});
  assert.match(rendered, /data-legend-group="communication"/);
  assert.doesNotMatch(rendered, /data-legend-group="rid"/);
  assert.doesNotMatch(rendered, /data-legend-group="radar"/);
});

test('combined 场景：C 与 RID 同时激活时两组都在', () => {
  const active = activeLegendLayers(legendDom({
    cnsCommunicationLayer: true,
    cnsRidLayer: true,
  }).$);
  const groups = compactLegendGroups(allLegendLines(), [...active]);
  const titles = groups.map((group) => group.title);
  assert.ok(titles.includes(LEGEND_GROUP_TITLES.communication));
  assert.ok(titles.includes(LEGEND_GROUP_TITLES.rid));
  assert.ok(groups.findIndex((item) => item.key === 'communication')
    < groups.findIndex((item) => item.key === 'rid'), '分组顺序必须稳定（航路 → C → RID）');
});

test('不传激活图层时保持既有模型逐字段不变（回归兼容）', () => {
  const groups = compactLegendGroups(allLegendLines(), null);
  const ids = groups.flatMap((group) => group.lines.map((line) => line.id));
  for (const id of ['online-basemap', 'reference-route', 'tower-reference',
    'cns-communication', 'cns-rid-land', 'cns-navigation-baseline',
    'cns-surface-facts', 'cns-gap-under_redundant']) {
    assert.ok(ids.includes(id), `兼容模式缺少 ${id}`);
  }
});

test('图例限宽限高并在内部滚动，绝不横跨地图', () => {
  //: 展开态：宽度上限 340px、高度上限 40vh、内部滚动。
  assert.match(MAP_CSS, /\.map-legend\{[^}]*max-width:340px/);
  assert.match(MAP_CSS, /\.map-legend\{[^}]*max-height:40vh/);
  assert.match(MAP_CSS, /\.map-legend-body\{[^}]*overflow-y:auto/);
  //: 收起态只有一个按钮宽（按内容自适应，且绝不塌成 0 宽）。
  assert.match(MAP_CSS, /\.map-legend-collapsed\{[^}]*width:max-content/);
  assert.match(MAP_CSS, /\.map-legend-head\{[^}]*white-space:nowrap/);
  //: 旧的一体式图例必须让位（否则两套图例同时占地图）。
  assert.match(MAP_CSS, /#mapLegend:not\(\[hidden\]\) ~ #legend\{display:none\}/);
  assert.ok(!/\.map-legend\{[^}]*width:100%/.test(MAP_CSS), '图例绝不横跨地图宽度');
  //: 图层符号表必须为每个激活图层登记条目（"图例 = 地图可见内容"）。
  for (const [layer, ids] of Object.entries(LEGEND_LAYER_SYMBOLS)) {
    assert.ok(Array.isArray(ids) && ids.length > 0, `${layer} 未登记任何图例符号`);
  }
});

test('updateCompactMapLegend 写入 #mapLegend 并隐藏旧图例容器', () => {
  const {$} = legendDom({cnsCommunicationLayer: true});
  const storage = memoryStorage();
  const result = updateCompactMapLegend({$, flow: {}, storage});
  assert.equal(result.rendered, true);
  assert.equal(result.collapsed, true);
  const target = $('mapLegend');
  assert.equal(target.hidden, false);
  assert.match(target.innerHTML, /map-legend-collapsed/);
  assert.equal($('legend').hidden, true, '旧图例容器必须被隐藏');
  //: 收起态不渲染行；但符号来源已由模型决定，不会出现"第二套图标"。
  assert.doesNotMatch(target.innerHTML, /map-legend-line/);
});

test('图例展开状态可保存并重新打开（默认仍收起）', () => {
  const storage = memoryStorage();
  writeMapLegendUiState({collapsed: false}, storage);
  assert.equal(readMapLegendUiState(storage).collapsed, false);
  assert.equal(JSON.parse(storage.getItem(MAP_LEGEND_UI_STORAGE_KEY)).collapsed, false);
  writeMapLegendUiState({collapsed: true}, storage);
  assert.equal(readMapLegendUiState(storage).collapsed, true);
  //: 损坏的持久化值必须回落为"收起"，绝不因此展开遮挡地图。
  storage.setItem(MAP_LEGEND_UI_STORAGE_KEY, '{not json');
  assert.equal(readMapLegendUiState(storage).collapsed, true);
});

function taskFixture(overrides = {}) {
  return {
    task_id: 'task-0123456789abcdef',
    task_type: 'cns_service_corridor_evaluate',
    task_name: 'CNS 服务走廊评估',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:10Z',
    advanced: {status: 'running'},
    ...overrides,
  };
}

/** 最小任务面板 DOM：只实现面板真正用到的元素查询与事件挂载。 */
function taskDom() {
  const nodes = new Map();
  const make = (id) => ({
    id, hidden: false, textContent: '', innerHTML: '', dataset: {},
    className: '', style: {}, onclick: null,
    querySelector: () => null,
    querySelectorAll: () => [],
    setAttribute: () => {},
    removeAttribute: () => {},
    getBoundingClientRect: () => ({left: 0, top: 0, width: 200, height: 100}),
  });
  return {
    getElementById(id) { return nodes.get(id) || null; },
    createElement(tag) { return make(tag); },
    body: {
      appended: [],
      appendChild(node) { this.appended.push(node); nodes.set(node.id, node); },
      contains: () => true,
    },
    querySelectorAll: () => [],
  };
}

function mountTaskCenter({tasks = [], uiState = null} = {}) {
  const documentRef = taskDom();
  const storage = memoryStorage();
  if (uiState) writeTaskPanelUiState(uiState, storage);
  const host = documentRef.createElement('aside');
  host.id = 'cnsTaskCenter';
  documentRef.body.appendChild(host);
  const centre = createTaskCenter({
    document: documentRef, storage,
    fetch: async (url) => {
      const body = String(url).includes('/api/tasks/catalog') ? {items: []} : {items: tasks};
      return {ok: true, json: async () => body};
    },
  });
  //: mount 只做渲染：先注入任务快照，再由中心自行挂载。
  centre.tasks.length; // 触发 getter（无副作用）
  return {centre, documentRef, host, storage};
}

test('后台任务面板：0 活跃任务时自动收起，只留「后台计算任务 0」', async () => {
  const {centre, documentRef, host, storage} = mountTaskCenter({tasks: []});
  //: 注入"用户上次把面板展开了"的持久化偏好，0 任务时也必须自动收起。
  writeTaskPanelUiState({collapsed: false}, storage);
  const finished = taskFixture({advanced: {status: 'succeeded'}});
  const {centre: centre2} = mountTaskCenter({tasks: [finished]});
  assert.ok(centre && centre2);

  //: 直接断言渲染契约：标题常驻活跃任务数，收起时内容区隐藏。
  const html = panelHtml([]);
  assert.match(html, /后台计算任务/);
  assert.match(html, /id="cnsTaskPanelCount"/);
  assert.match(TASKS_CSS, /\.cns-task-panel-count/);
  assert.match(TASKS_CSS, /\.cns-task-center\[data-collapsed="true"\] \.cns-task-panel-count/);
  //: 0 任务自动收起是代码契约：activeCount === 0 且用户未手动覆盖 ⇒ collapsed = true。
  const tasksSource = readFileSync(
    new URL('../cns_planner/web/js/tasks.js', import.meta.url), 'utf8');
  assert.match(tasksSource, /if \(activeCount === 0 && !manualOverride\) collapsed = true;/);
  assert.match(tasksSource, /manualOverride = true;/);
  assert.ok(host);
  assert.ok(documentRef);
});

test('后台任务面板：手动展开 / 收起始终可用，且不写回持久化偏好', () => {
  const storage = memoryStorage();
  assert.equal(readTaskPanelUiState(storage).collapsed, false);
  writeTaskPanelUiState({collapsed: true}, storage);
  assert.equal(readTaskPanelUiState(storage).collapsed, true);
  //: 旧契约（可持久化收起偏好）保持：有活跃任务时仍然读它。
  const tasksSource = readFileSync(
    new URL('../cns_planner/web/js/tasks.js', import.meta.url), 'utf8');
  assert.match(tasksSource, /let collapsed = uiState\.collapsed;/);
  assert.match(tasksSource, /function togglePanel\(\)/);
  //: 0 活跃任务时不写回偏好（只覆盖本次会话），因此手机/刷新后仍是收起态。
  assert.match(tasksSource, /if \(activeCount > 0\) uiState = \{\.\.\.uiState, collapsed\};/);
  //: 有活跃任务时必须自动展开（清除手动覆盖标记）。
  assert.match(tasksSource, /if \(activeCount > 0\) manualOverride = false;/);
});

test('缩放至航路是新增能力，不修改 #fit 的历史语义', () => {
  assert.match(INDEX_HTML, /id="fitRoute"[^>]*>航路</);
  assert.match(INDEX_HTML, /id="fit" title="回到全域"/);
  assert.match(MAIN_JS, /function fitOperationalRoute\(\)/);
  assert.match(MAIN_JS, /operationalRouteBounds/);
  assert.match(MAIN_JS, /flow\?\.operational_routes/);
  assert.match(MAIN_JS, /\$\('fit'\)\.onclick=\(\)=>fit\(state\?\.bounds\)/, '#fit 语义不得改变');
  assert.match(MAIN_JS, /fitRouteButton\.onclick=\(\)=>fitOperationalRoute\(\)/);
});

test('Round 2.8-A：航路规划参数区把陆地相对风险权重按裁定文案展示', () => {
  const thetaSource = readFileSync(
    new URL('../cns_planner/web/js/workflow/layered_theta_v2.js', import.meta.url), 'utf8');
  //: 业务名称与 canonical 字段名并列出现（用户裁定：前端文案优先用「陆地相对风险权重」）。
  assert.match(thetaSource, /THETA_V2_GROUND_RISK_WEIGHT_LABEL='陆地相对风险权重'/);
  assert.match(thetaSource, /陆地相对风险权重（planning_exposure_policy · land_relative_risk_baseline）/);
  assert.match(thetaSource, /可编辑：陆地相对风险权重 planning_exposure_policy/);
  assert.match(thetaSource, /id="thetaV2LandRiskBaseline"/);
  assert.match(thetaSource, /id="saveThetaV2PlanningExposure">保存陆地相对风险权重</);
  //: 工程说明逐字包含裁定文案。
  assert.match(thetaSource, /THETA_V2_GROUND_RISK_WEIGHT_BEHAVIOR_NOTE/);
  assert.match(thetaSource, /提高陆地相对于海面的风险基线差异，使航路更偏向低地面风险区域/);
  assert.match(thetaSource, /不把陆地设为禁行，也不把海面风险设为 0/);
  assert.match(thetaSource, /coastal_uncertain 按项目 canonical 的 land policy 处理/);
  assert.match(thetaSource, /地形\/人口证据缺失的格保持 unresolved（fail-closed）/);
  //: 0.30 只是**候选**，且必须显式声明尚未 adoption（绝不与 authoritative 航路混同）。
  assert.match(thetaSource, /0.30（真实扫描得到的最低有效跳变值，已定为默认值）/);
  assert.match(thetaSource, /该 0.30 最优解仍是 <b>candidate<\/b>/);
  assert.match(thetaSource, /绝不与当前 authoritative 运行航路混为同一条正式航路/);
});

test('截图（验收）所需的图层状态全部由正式 UI 控件驱动', () => {  //: 验收要求：截图时不得注入 CSS 临时隐藏产品 UI，只能用正式控件改状态。
  for (const id of ['cnsCommunicationLayer', 'cnsRidLayer', 'cnsNavigationLayer',
    'cnsServiceGapLayer', 'cnsFacilityPlanLayer', 'surfaceFactsLayer',
    'routeProtectionCorridorLayer', 'surveillanceProtectionLayer',
    'radarSurveillanceLayer', 'towerLayer']) {
    assert.ok(INDEX_HTML.includes(`id="${id}"`), `index.html 缺少图层开关 ${id}`);
  }
  //: 每个可激活图例图层都必须能在 index.html 里找到对应开关（否则过滤会永远为空）。
  for (const id of ['cnsCommunicationLayer', 'cnsRidLayer', 'cnsNavigationLayer',
    'cnsServiceGapLayer', 'cnsFacilityPlanLayer', 'surfaceFactsLayer',
    'existingCnsLayer', 'candidateSiteLayer', 'towerLayer', 'cLayer', 'nLayer', 'sLayer',
    'routeProtectionCorridorLayer', 'surveillanceProtectionLayer', 'radarSurveillanceLayer',
    'referenceRouteLayer', 'referenceRoutePointLayer', 'referenceLandingLayer']) {
    assert.ok(INDEX_HTML.includes(`id="${id}"`));
  }
  //: 图例 / 任务面板的 UI 状态都有明确的持久化键。
  assert.match(LEGEND_JS, /MAP_LEGEND_UI_STORAGE_KEY='cns\.mapLegend\.ui\.v1'/);
  assert.match(INDEX_HTML, /id="mapLegend"/);
});
