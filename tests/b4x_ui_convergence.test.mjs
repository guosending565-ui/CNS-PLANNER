// =========================================================================
// Phase4-B4X：六步产品 UI 收敛 + 约束可视化的前端契约测试
// =========================================================================
//
// 本轮是 UI / Presentation / Read-only visualization 批次，因此这里锁定的是
// **展示层契约**（不跑浏览器、不造假 DOM，读源码 / 调纯函数）：
//
//  1. 生产主导航精确等于六步，且不出现 P-number / V1-V3 / Legacy 等开发术语；
//  2. 中文取词只有一份集中实现（presentation.js），各 step 不得再手写第二套；
//  3. B4X 要求的关键中文词汇逐条正确（ready→可继续、blocked→暂不能继续 …）；
//  4. canonical 节点中文名逐条正确；
//  5. 多高度层：Step2/Step3 共用同一个 AltitudeLayer selector，**不得写死 ALT-080**；
//  6. 约束场：只读 HTTP 读取路径 + 地图紧凑投影（不回传 evidence / 几何）；
//  7. 默认地图只启用在线底图，所有分析层（含高度层障碍）默认关闭；
//  8. 建筑层默认关闭、铁塔符号可辨识（尺寸语义是屏幕像素高度）；
//  9. 正式 / 兼容隔离：兼容入口只在高级区，且绝不出现"正式结果 / 已采纳"。

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const read = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');

const html = read('cns_planner/web/index.html');
const main = read('cns_planner/web/js/main.js');
const shell = read('cns_planner/web/js/shell.js');
const script = read; // 别名：便于阅读
const presentation = read('cns_planner/web/js/workflow/presentation.js');
const common = read('cns_planner/web/js/workflow/common.js');
const altitudeLayers = read('cns_planner/web/js/workflow/altitude_layers.js');
const constraintField = read('cns_planner/web/js/workflow/constraint_field.js');
const constraintView = read('cns_planner/web/js/workflow/constraint_view.js');
const constraintOverlay = read('cns_planner/web/js/map/constraint_field_overlay.js');
const step01 = read('cns_planner/web/js/workflow/step01_project.js');
const step02 = read('cns_planner/web/js/workflow/step02_workspace.js');
const step03 = read('cns_planner/web/js/workflow/step03_routes.js');
const step05 = read('cns_planner/web/js/workflow/step05_cns.js');
const router = read('cns_planner/api/router.py');
const workflowService = read('cns_planner/application/workflow_service.py');
const constraintService = read('cns_planner/application/planning_constraint_field_service.py');

//: 开发阶段编号与旧版本代号：只允许出现在「高级 / 审计信息」区。
const DEV_TERMS = ['P1', 'P7', 'P8', 'P14', 'P15', 'P16', 'V1', 'V2', 'V3', 'Legacy', 'legacy'];

/** 六个 canonical 步骤的中文标签（逐字）。 */
const STEP_LABELS = [
  '数据准备', '环境与风险', '航路规划与发布', 'CNS需求', 'CNS能力与设施规划', '方案评审与报告'
];

/** 索引一个步骤模块里 `render()` 返回的主界面 HTML（operate + result，不含 advanced）。 */
function renderPrimaryPanels(module, context) {
  const full = module.render(context);
  // advanced 面板整体剔除：开发术语只允许留在那里。
  return full.replace(/<div class="wb-panel[^"]*" data-panel-group="advanced"[\s\S]*?<\/div>\s*$/u, '');
}

// ---- 1. 六步主导航 ----------------------------------------------------------

test('production navigation exposes exactly the six canonical steps', () => {
  const labels = [...html.matchAll(/data-step="(\d)"[^>]*>[\s\S]*?rail-label">([^<]+)</g)]
    .map(match => [Number(match[1]), match[2]]);
  assert.deepEqual(labels.map(item => item[0]), [1, 2, 3, 4, 5, 6], '主导航必须且只能是六步');
  assert.deepEqual(labels.map(item => item[1]), STEP_LABELS, '六步标签必须与产品定义逐字一致');
});

test('production navigation never shows a development phase number or version code', () => {
  const rail = html.slice(html.indexOf('<nav class="rail"'), html.indexOf('</nav>'));
  for (const term of DEV_TERMS) {
    assert.doesNotMatch(rail, new RegExp(`(^|[^A-Za-z0-9])${term}([^A-Za-z0-9]|$)`),
      `主导航不得出现开发术语 ${term}`);
  }
  // 导航语言必须是中文业务语言：不含“环境建模 / 运行规则 / CNS规划 / 方案评审”这类旧标签
  for (const stale of ['项目准备', '环境建模', '运行规则']) {
    assert.ok(!rail.includes(stale), `主导航不得再使用旧标签「${stale}」`);
  }
});

// ---- 2. 集中式中文映射 ------------------------------------------------------

test('all Chinese status wording has exactly one home: presentation.js', () => {
  // common.js 只转发，不再自带第二份词表
  assert.match(common, /import \{statusText,statusBadge,escapeHtml\} from '\.\/presentation\.js'/);
  assert.match(common, /export \{statusText,statusBadge,escapeHtml\}/);
  assert.doesNotMatch(common, /not_calculated:\s*'/, 'common.js 不得保留第二份状态词表');
  // 生产界面不得再手写 raw enum → 中文的判断
  for (const [name, source] of [['step02', step02], ['step03', step03], ['step05', step05]]) {
    assert.doesNotMatch(source, /not_calculated\s*===\s*['"]?\w+['"]?\s*\?\s*['"][\u4e00-\u9fa5]/,
      `${name} 不得手写 raw enum 的中文判断`);
  }
  // presentation.js 是唯一导出映射表的地方
  assert.match(presentation, /export const STATUS_TEXT=\{/);
  assert.match(presentation, /export const WORKFLOW_STATUS_TEXT=\{/);
  assert.match(presentation, /export const READINESS_TEXT=\{/);
  assert.match(presentation, /export const ASSESSMENT_TEXT=\{/);
  assert.match(presentation, /export const INPUT_REQUIREMENT_TEXT=\{/);
  assert.match(presentation, /export const CONSTRAINT_OUTCOME_TEXT=\{/);
  assert.match(presentation, /export const CONSTRAINT_BLOCKER_TEXT=\{/);
  assert.match(presentation, /export const CONSTRAINT_UNKNOWN_REASON_TEXT=\{/);
  assert.match(presentation, /export const CANONICAL_NODE_LABELS=\{/);
  assert.match(presentation, /export const VERTICAL_REFERENCE_TEXT=\{/);
});

test('presentation.js is a pure display layer that never invents semantics', () => {
  // 未登记取值原样返回：绝不伪装成"通过"
  assert.match(presentation, /Object\.prototype\.hasOwnProperty\.call\(STATUS_TEXT,key\)\?STATUS_TEXT\[key\]:key/);
  // 不读 flow / state，不发请求
  assert.doesNotMatch(presentation, /fetch\(|createApiClient|localStorage/);
  assert.doesNotMatch(presentation, /flow\?\.|state\?\./);
});

// ---- 3. B4X 要求的中文词汇 --------------------------------------------------

test('the required Chinese wording is present verbatim', async () => {
  const m = await import('../cns_planner/web/js/workflow/presentation.js');
  const cases = [
    [m.workflowStatusText, 'ready', '可继续'],
    [m.workflowStatusText, 'ready_with_assumptions', '可继续（采用工程假设）'],
    [m.workflowStatusText, 'blocked', '暂不能继续'],
    [m.workflowStatusText, 'running', '正在计算'],
    [m.workflowStatusText, 'completed', '已完成'],
    [m.workflowStatusText, 'completed_with_warnings', '已完成（有提示）'],
    [m.workflowStatusText, 'stale', '需要重新计算'],
    [m.workflowStatusText, 'failed', '执行失败'],
    [m.assessmentText, 'passed', '检查通过'],
    [m.assessmentText, 'failed', '检查不通过'],
    [m.assessmentText, 'unknown', '证据不足'],
    [m.maturityText, 'provisional', '候选 / 试算结果'],
    [m.authoritativeText, 'published', '已发布'],
    [m.authoritativeText, 'adopted', '已采纳'],
    [m.authoritativeText, 'confirmed', '已确认'],
    [m.constraintOutcomeText, 'pass', '可通行'],
    [m.constraintOutcomeText, 'blocked', '障碍'],
    [m.constraintOutcomeText, 'unknown', '证据不足'],
    [m.inputRequirementText, 'required', '必需'],
    [m.inputRequirementText, 'assumable', '可采用工程假设'],
    [m.inputRequirementText, 'optional', '可选'],
    [m.inputRequirementText, 'enhanced', '增强数据'],
    [m.verticalReferenceText, 'egm2008_orthometric', 'EGM2008 正高'],
    [m.constraintFreshness, 'current', '当前'],
    [m.constraintFreshness, 'stale', '需要重新计算']
  ];
  for (const [fn, raw, expected] of cases) {
    assert.equal(fn(raw), expected, `${raw} 必须显示为「${expected}」`);
  }
  // 未知取值原样返回（不发明语义）
  assert.equal(m.statusText('some_unregistered_state'), 'some_unregistered_state');
  assert.equal(m.assessmentText(''), '—');
});

test('every canonical node has a Chinese business name', async () => {
  const m = await import('../cns_planner/web/js/workflow/presentation.js');
  const expected = {
    environment: '环境模型', risk_field: '航路风险场', route_candidate: '候选航路',
    route_validation: '航路安全验证', operational_route: '正式运行航路', required_cns: 'CNS能力需求',
    coverage: '三维覆盖评估', service_capability: '服务能力评估', service_corridor: 'CNS服务走廊',
    capability_gap: 'CNS能力缺口', facility_plan: 'CNS设施规划', plan_review: '方案评审',
    report: '规划报告'
  };
  for (const [node, label] of Object.entries(expected)) {
    assert.equal(m.canonicalNodeLabel(node), label, `canonical 节点 ${node} 必须有中文名`);
    assert.ok(m.isCanonicalNode(node));
  }
  assert.equal(m.canonicalNodeLabel('unknown_node'), 'unknown_node', '未登记节点原样返回');
});

test('constraint blockers are shown in Chinese, never as raw JSON', async () => {
  const m = await import('../cns_planner/web/js/workflow/presentation.js');
  assert.equal(m.constraintBlockerText(['terrain', 'building']), '地形、建筑');
  assert.equal(m.constraintBlockerText(['tower']), '铁塔');
  assert.equal(m.constraintBlockerText(['airspace']), '禁飞/受限区域');
  assert.equal(m.constraintBlockerText(['critical_site']), '保护要地');
  assert.deepEqual(m.constraintBlockerList(['terrain', 'building']), ['地形', '建筑']);
  // 顺序按后端 BLOCKER_DOMAINS：地形 → 建筑 → 铁塔 → 禁飞 → 要地
  assert.deepEqual(m.CONSTRAINT_BLOCKER_ORDER,
    ['terrain', 'building', 'tower', 'airspace', 'critical_site']);
  assert.equal(m.constraintUnknownText('tower_dataset_unresolved'), '铁塔数据尚未解析');
  assert.equal(m.constraintUnknownText('terrain_evidence_or_clearance_unresolved'), '地形证据或垂直净空未解析');
  // unknown 绝不等价于 pass
  assert.notEqual(m.constraintOutcomeText('unknown'), m.constraintOutcomeText('pass'));
});

// ---- 4. 多高度层：统一 selector，绝不写死 ALT-080 ---------------------------

test('step 2 and step 3 share one altitude layer selector implementation', () => {
  assert.match(step02, /from '\.\/altitude_layers\.js'/, 'step02 必须使用统一 selector');
  assert.match(step02, /altitudeLayerSelector\(/);
  assert.match(step02, /bindAltitudeLayerSelector\(/);
  // Step3 通过巡航高度层面板使用同一份目录（route_operating_layer 读 spatial_3d.altitude_layers）
  const routeOperating = read('cns_planner/web/js/workflow/route_operating_layer.js');
  assert.match(routeOperating, /altitude_layers/);
  assert.match(step03, /renderCruiseLayerPanel/);
});

test('the altitude layer selector is catalog-driven and never invents a layer', async () => {
  const m = await import('../cns_planner/web/js/workflow/altitude_layers.js');
  const empty = {spatial_3d: {altitude_layers: []}};
  assert.equal(m.altitudeLayerCatalogSummary(empty).total, 0);
  const options = m.altitudeLayerOptions(empty);
  assert.ok(options.includes('请选择固定巡航高度层'), '空目录只能显示"请选择"，绝不伪造默认层');
  assert.doesNotMatch(options, /ALT-0\d\d/, '空目录下不得出现任何具体高度层');
  assert.equal(m.altitudeLayerSelection({flow: empty}), '', '空目录不得回退到某个默认高度层');

  const flow = {spatial_3d: {altitude_layers: [
    {altitude_layer_id: 'ALT-060', nominal_altitude_m: 60, vertical_reference: 'egm2008_orthometric', status: 'confirmed'},
    {altitude_layer_id: 'ALT-100', nominal_altitude_m: 100, vertical_reference: 'egm2008_orthometric', status: 'confirmed'},
    {altitude_layer_id: 'ALT-150', nominal_altitude_m: 150, vertical_reference: 'egm2008_orthometric', status: 'confirmed'},
    {altitude_layer_id: 'ALT-200', nominal_altitude_m: 200, vertical_reference: 'egm2008_orthometric', status: 'confirmed'},
    {altitude_layer_id: 'CUSTOM-42', nominal_altitude_m: 42, vertical_reference: 'egm2008_orthometric', status: 'confirmed'}
  ]}};
  const list = m.altitudeLayerOptions(flow);
  for (const id of ['ALT-060', 'ALT-100', 'ALT-150', 'ALT-200', 'CUSTOM-42']) {
    assert.ok(list.includes(id), `${id} 必须出现在可选高度层里`);
    assert.ok(list.includes(`value="${id}"`), `${id} 必须是可选的 option 值`);
  }
});

test('altitude layer display uses Chinese vertical reference and never raw enum', () => {
  assert.match(altitudeLayers, /verticalReferenceText/);
  assert.doesNotMatch(altitudeLayers, /'· '\+escapeHtml\(String\(item\.vertical_reference\)\)/);
  // 未确认 / 缺 nominal 的层必须被明确标注，不得静默混入可用层
  assert.match(altitudeLayers, /nominal 高度待工程确认/);
  assert.match(altitudeLayers, /垂向基准待工程确认/);
});

test('no source file hardcodes ALT-080 as a production altitude layer', () => {
  for (const [name, source] of [
    ['step02', step02], ['step03', step03], ['main.js', main],
    ['altitude_layers.js', altitudeLayers], ['constraint_view.js', constraintView],
    ['constraint_field.js', constraintField]
  ]) {
    assert.doesNotMatch(source, /['"]ALT-080['"]/,
      `${name} 不得把 ALT-080 写死为正式高度层（adapter/测试夹具除外）`);
  }
  assert.doesNotMatch(constraintService, /ALT-080/);
});

// ---- 5. 约束场只读读取路径 --------------------------------------------------

test('constraint field read path exposes only the two documented GET endpoints', () => {
  assert.match(router, /if path == "\/api\/planning-constraint-field":/);
  assert.match(router, /if path == "\/api\/planning-constraint-field\/map":/);
  assert.match(router, /workflow\.planning_constraint_field_map\(/);
  assert.match(workflowService, /def planning_constraint_field_map\(/);
  // 只读：不得在这两个分支里出现写操作
  const block = router.slice(router.indexOf('/api/planning-constraint-field"'),
    router.indexOf('/api/shelter-coefficient-policy'));
  assert.doesNotMatch(block, /generate_planning_constraint_field|session\.save|set_/);
});

test('the map endpoint ships grid_id/outcome/blocked_by only, never evidence or geometry', () => {
  assert.match(constraintService, /"geometry_source": "frontend_grid_index"/);
  assert.match(constraintService, /"blocked_by": list\(item\.get\("blocked_by"\) or \[\]\)/);
  // 逐格记录只允许三个字段（几何/证据一律留在后端与前端网格索引里）。
  const projection = constraintService.slice(
    constraintService.indexOf('"cells": [', constraintService.indexOf('def field_map')),
    constraintService.indexOf('def _field(')
  );
  for (const forbidden of ['unknown_reasons', 'evidence_refs', 'polygon', 'center']) {
    assert.ok(!projection.includes(forbidden), `地图投影不得包含 ${forbidden}`);
  }
  assert.match(projection, /"grid_id": str\(item\.get\("grid_id"\) or ""\)/);
  assert.match(projection, /"outcome": str\(item\.get\("outcome"\) or "unknown"\)/);
  // cells 不可读时绝不当成"全部可通行"
  assert.match(constraintService, /"status": "cells_unavailable"/);
  assert.match(constraintService, /约束场明细尚未水合/);
});

test('the frontend reads the constraint field through HTTP only, never through a file path', () => {
  for (const [name, source] of [['constraint_field.js', constraintField], ['constraint_view.js', constraintView]]) {
    assert.doesNotMatch(source, /\.cns-results|\breadFile\b|require\(/, `${name} 不得接触文件系统`);
  }
  // 展示层不得自造生成端点：生成只能在用户显式点击时通过注入的 POST 触发
  assert.match(constraintField, /CONSTRAINT_FIELD_MAP_ENDPOINT='\/api\/planning-constraint-field\/map'/);
  assert.match(constraintField, /CONSTRAINT_FIELD_SUMMARY_ENDPOINT='\/api\/planning-constraint-field'/);
  assert.doesNotMatch(constraintField, /planning-constraint-fields\/evaluate/);
  assert.match(constraintView, /await post\('\/api\/planning-constraint-fields\/evaluate'/,
    '生成端点只能通过注入的 post 调用（唯一触发点是用户显式点击）');
  assert.match(constraintView, /if\(typeof post!=='function'\)throw Error/);
});

test('the constraint field is loaded on demand, never during bootstrap', () => {
  // 启动路径（api('/api/state')）不得触发约束场读取
  const bootstrap = main.slice(main.indexOf("api('/api/state')"));
  assert.doesNotMatch(bootstrap, /loadMap\(\)|loadConstraintField/,
    '约束场绝不能在启动时加载');
  // 只有勾选图层或显式生成才读取
  assert.match(main, /onConstraintLayer:\(\)=>\{constraintView\.loadMap\(\);paint\(\);\}/);
  assert.match(constraintView, /if\(!altitudeLayerId\|\|!layerNeedsData\(\)\)return null;/);
});

// ---- 6. 约束地图表达：三态、障碍优先、unknown 计数不被隐藏 ------------------

test('the constraint overlay separates the three outcomes and draws obstacles last', async () => {
  const m = await import('../cns_planner/web/js/map/constraint_field_overlay.js');
  assert.deepEqual(m.CONSTRAINT_DRAW_ORDER, ['pass', 'unknown', 'blocked'],
    '障碍必须最后绘制（视觉权重最高）');
  assert.ok(m.CONSTRAINT_OVERLAY_STYLE.blocked.alpha > m.CONSTRAINT_OVERLAY_STYLE.unknown.alpha);
  assert.ok(m.CONSTRAINT_OVERLAY_STYLE.unknown.alpha > m.CONSTRAINT_OVERLAY_STYLE.pass.alpha);
  // 三个独立开关，默认只画障碍
  const f = await import('../cns_planner/web/js/workflow/constraint_field.js');
  assert.deepEqual(f.CONSTRAINT_LAYER_DEFAULTS, {blocked: true, unknown: false, pass: false});
});

test('the map carries only grid_id/outcome/blocked_by and reuses the grid index for geometry', async () => {
  const f = await import('../cns_planner/web/js/workflow/constraint_field.js');
  const model = f.constraintMapModel({
    status: 'passed',
    cells: [
      {grid_id: 'G1', outcome: 'blocked', blocked_by: ['terrain']},
      {grid_id: 'G2', outcome: 'unknown', blocked_by: [], unknown_reasons: [{reason: 'x'}]}
    ]
  });
  assert.deepEqual(model.cells.map(item => Object.keys(item).sort()),
    [['blockedBy', 'gridId', 'outcome'], ['blockedBy', 'gridId', 'outcome']],
    '前端模型也只保留三个字段：几何必须复用标准网格索引');
  const entries = f.constraintOverlayEntries({cells: model.cells, cellsById: new Map([
    ['G1', {cell: {bbox: [0, 0, 1, 1]}}]
  ])});
  assert.equal(entries.entries.length, 1);
  assert.deepEqual(entries.unresolved, ['G2'], '缺少几何的 grid_id 必须被显式报告，绝不静默丢弃');
});

test('unknown cells are never hidden: the summary always reports their count', async () => {
  const f = await import('../cns_planner/web/js/workflow/constraint_field.js');
  const model = f.constraintFieldModel({
    altitudeLayerId: 'ALT-080',
    collection: {items: [{
      altitude_layer_id: 'ALT-080', status: 'completed_with_warnings', field_id: 'PCF-1',
      counts: {total: 100, pass: 10, blocked: 20, unknown: 70,
        blocked_by: {terrain: 20, building: 0, tower: 0, airspace: 0, critical_site: 0}},
      warnings: [{reason: 'unknown_constraints_remain', unknown_constraint_count: 70,
        provisional_traversal_allowed: false}]
    }]},
    map: null
  });
  assert.equal(model.outcomes.unknown, 70);
  const rendered = f.constraintSummaryHtml(model, {formatNumber: value => String(value)});
  assert.match(rendered, /证据不足/);
  assert.match(rendered, /70/, '证据不足计数必须出现在状态卡里');
  assert.match(rendered, /证据不足不等于可通行/, '必须明确说明证据不足不等于可通行');
  assert.match(rendered, /不允许穿越证据不足单元/);
  // 即使地图明细没读到，状态卡的计数也来自 slim 摘要
  assert.doesNotMatch(rendered, />通过</);
});

test('the obstacle popup shows Chinese reasons with raw evidence folded away', async () => {
  const f = await import('../cns_planner/web/js/workflow/constraint_field.js');
  const blockUrl = f.constraintMapUrl('ALT-100');
  assert.match(blockUrl, /altitude_layer_id=ALT-100/);
  const htmlBlock = f.constraintCellDetailsHtml(
    {grid_id: 'G1', outcome: 'blocked', blocked_by: ['terrain', 'building']},
    {altitudeLayerLabelText: 'ALT-080 · 80 m · EGM2008 正高', gridId: 'G1'}
  );
  assert.match(htmlBlock, /地形、建筑/);
  assert.doesNotMatch(htmlBlock, /\["terrain","building"\]/, '绝不显示 raw JSON 数组');
  assert.doesNotMatch(htmlBlock, /\[&#39;terrain&#39;/, '绝不显示转义后的 raw JSON');
  assert.match(htmlBlock, /高度层：ALT-080 · 80 m · EGM2008 正高/);
  assert.match(htmlBlock, /状态：<b data-outcome="blocked">障碍<\/b>/);
  // 详细证据进高级折叠
  const unknown = f.constraintCellDetailsHtml(
    {grid_id: 'G2', outcome: 'unknown', unknown_reasons: [{domain: 'tower', reason: 'tower_dataset_unresolved'}]},
    {gridId: 'G2'}
  );
  assert.match(unknown, /铁塔：铁塔数据尚未解析/);
  assert.match(unknown, /证据不足不等于安全/);
  assert.match(unknown, /高级 \/ 审计信息/);
});

// ---- 7. 步骤面板：六区结构 / 主操作唯一 -------------------------------------

test('every step keeps the six-zone vocabulary available', () => {
  assert.match(common, /export const STEP_SECTIONS=\['目标','输入准备','阻塞项与工程假设','主操作','结果','下一步'\]/);
  assert.match(common, /export function primaryAction\(/);
  assert.match(common, /export function blockerList\(/);
  assert.match(common, /export function nextStepBar\(/);
  // 阻塞项空态绝不写成"通过"
  assert.match(common, /当前没有阻塞项/);
});

test('step 2 separates risk (soft cost) from constraints (feasibility)', () => {
  assert.match(step02, /风险场 = 软成本/);
  assert.match(step02, /约束场 = 可行性/);
  assert.match(step02, /两者是两份独立证据/);
  assert.match(step02, /绝不把它们合成/);
  // 高度层选择与约束场是两个独立分段
  assert.match(step02, /\['env-op-altitude','固定巡航高度层'\]/);
  assert.match(step02, /\['env-res-constraint','规划约束场'\]/);
});

test('step 3 keeps exactly one formal route chain and isolates compatibility', () => {
  assert.match(step03, /const PRODUCTION_ROUTE_CHAIN=\[/);
  for (const step of ['选择 OD', '选择固定巡航高度层', '生成正式候选航路', '航路风险画像',
    '航路安全验证', '发布运行航路']) {
    assert.ok(step03.includes(step), `正式链条必须包含「${step}」`);
  }
  // 算法 id 只在高级 / 审计区出现，不作为主标题
  assert.match(step03, /英文|advancedAuditNote\(/);
  assert.match(step03, /advancedAuditNote\(\s*'当前正式规划器实现：'/);
  // 正式链条区不得出现算法实现标识（它只在高级 / 审计区转印）
  const productionCandidates = step03.slice(step03.indexOf('function productionRouteChainBlock('),
    step03.indexOf('/** 约束场状态'));
  assert.doesNotMatch(productionCandidates, /algorithm\.algorithm_id|algorithm\.version/,
    '正式链条区块不得内联显示算法实现标识');
  assert.doesNotMatch(step03, /wbBlock\('Layered Risk-Aware Theta\* V2'/,
    '算法名不得作为业务主标题');
  // 兼容分段只在高级区，且名称明确为旧版 / 研究对照
  assert.match(step03, /\['adv-legacy','旧版兼容 \/ 研究对照'\]/);
  assert.match(step03, /\['adv-experiment','研究对照实验'\]/);
  // 旧版试算航路绝不叫"运行航路 / 正式航路"
  assert.match(step03, /旧版试算航路（研究对照）/);
});

test('terminal procedures are declared as a separate module, not faked', () => {
  assert.match(step03, /正式规划针对<b>固定巡航高度层<\/b>/);
  assert.match(step03, /起飞、爬升、下降和进离场程序将在独立模块中评估/);
  assert.doesNotMatch(step03, /terminal.*geometry.*=\s*polygon/i);
});

test('step 1 shows a requirement-level input checklist without algorithm internals', () => {
  assert.match(step01, /export const INPUT_REQUIREMENTS=\[/);
  for (const label of ['人口', '地形', '建筑', '铁塔', '空域 / 要地', '设备 / 设施', '起降点 / 参考航线']) {
    assert.ok(step01.includes(`label:'${label}'`), `必要输入清单必须包含「${label}」`);
  }
  assert.match(step01, /inputRequirementBadge\(/);
  assert.match(step01, /inputRequirementPanel\(/);
  // 算法 manifest / schema / 版本只在高级区
  const operate = step01.slice(step01.indexOf("const operate=wbPanel('operate'"), step01.indexOf("const result=wbPanel('result'"));
  assert.doesNotMatch(operate, /manifest|algorithm_id|parameter_schema/);
});

// ---- 8. 默认地图状态 / 图层可辨识性 -----------------------------------------

test('the layer drawer ships only the online basemap by default, including the constraint layer', () => {
  const boxes = new Map();
  for (const match of html.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[0]);
    if (id) boxes.set(id[1], match[0]);
  }
  const defaults = [...boxes].filter(([, attributes]) => /\bchecked\b/.test(attributes)).map(([id]) => id);
  assert.deepEqual(defaults, ['online'], `首次打开只允许勾选在线底图，实际：${defaults.join(', ')}`);
  for (const id of ['altitudeConstraintLayer', 'altitudeConstraintUnknownLayer', 'altitudeConstraintPassLayer']) {
    assert.ok(boxes.has(id), `图层抽屉必须有 ${id}`);
    assert.doesNotMatch(boxes.get(id), /\bchecked\b/, `${id} 必须默认关闭`);
  }
  // 约束子开关默认关闭 → 默认只画障碍
  assert.doesNotMatch(boxes.get('altitudeConstraintUnknownLayer'), /\bchecked\b/);
  assert.doesNotMatch(boxes.get('altitudeConstraintPassLayer'), /\bchecked\b/);
});

test('the constraint layer switches reuse the unified layer-switch semantics', () => {
  assert.match(main, /'altitudeConstraintLayer','altitudeConstraintUnknownLayer','altitudeConstraintPassLayer'/);
  assert.match(main, /constraintView\.draw\(\{ctx,view,screenPoint,gridTheme:GridTheme\}\)/);
  // 约束层画在网格之上、航路之下
  const drawLayers = read('cns_planner/web/js/map/display_layers.js');
  const callOrder = [
    'drawGridBoundaries();',
    "if(typeof drawConstraintLayer==='function')drawConstraintLayer();",
    "if(typeof drawBuildingFootprints==='function')drawBuildingFootprints();"
  ];
  const positions = callOrder.map(token => drawLayers.indexOf(token));
  assert.ok(positions.every(index => index >= 0), 'drawWorkflowLayers 必须保留这三个绘制钩子');
  assert.deepEqual([...positions].sort((a, b) => a - b), positions,
    '绘制顺序必须是：网格边界 → 高度层障碍 → 建筑轮廓');
});

test('the building footprint layer stays independently switchable and off by default', () => {
  assert.match(html, /id="buildingFootprintLayer"/);
  const box = /<input\b[^>]*id="buildingFootprintLayer"[^>]*>/.exec(html);
  assert.ok(box && !/\bchecked\b/.test(box[0]), '建筑轮廓层必须默认关闭且可单独打开');
  assert.match(main, /buildingFootprintLayer/);
});

test('tower symbols remain legible: sizes are real on-screen pixel heights', async () => {
  const lod = await import('../cns_planner/web/js/map/lod.js');
  assert.ok(lod.TOWER_SYMBOL_SIZE_PX.medium >= 14, 'medium 档铁塔符号必须可辨识（≥14 px）');
  assert.ok(lod.TOWER_SYMBOL_SIZE_PX.detail >= 20, 'detail 档铁塔符号必须清晰（≥20 px）');
  assert.ok(lod.TOWER_HIGHLIGHT_SIZE_PX >= lod.TOWER_SYMBOL_SIZE_PX.detail);
  const display = read('cns_planner/web/js/map/display_layers.js');
  assert.match(display, /drawTowerSymbol\(/, '必须使用矢量塔形符号，而不是一个过小的点');
  assert.match(display, /sizePx/);
});

// ---- 9. 状态机与 authority 不回退 -------------------------------------------

test('B4X never re-grants production write authority to compatibility results', () => {
  // 旧 planner / coverage / site-plan 仍只能在兼容 / 高级路径
  assert.match(step05, /旧版兼容|旧版二维覆盖试算|旧版站址试算/);
  assert.match(step05, /cns-adv-compat/);
  assert.match(step05, /cns-adv-closedloop/);
  // 生产界面不得给兼容结果"正式结果 / 已采纳 / 已应用"
  const closedLoop = step05.slice(step05.indexOf('closedLoopPanel='), step05.indexOf('const body=wbPanel'));
  assert.doesNotMatch(closedLoop, /正式结果|已采纳/);
  // 约束场不给 operational adoption 开后门（B3X 的 gate 语义保持不变）：
  // 穿越证据不足单元的候选只能"试算"，永不能发布为运行航路。
  assert.match(constraintField, /穿越证据不足单元/);
  assert.match(constraintField, /此类候选永远不能发布为运行航路/);
  assert.match(constraintField, /证据不足不等于可通行/);
});

test('the read-only B4X API adds no new write authority', () => {
  // 本轮不新增任何 POST 端点
  assert.doesNotMatch(router, /planning-constraint-field[^\n]*POST/);
  assert.match(router, /POST \u4e0e GET \u5206\u5f00|def post\(self, path, payload\)/);
  // 生成仍走既有 evaluate 端点
  assert.match(router, /\/api\/planning-constraint-fields\/evaluate/);
});
