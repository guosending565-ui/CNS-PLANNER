// =========================================================
// 步骤面板公共出口
//
// B4X（Six-Step Product UI Convergence）之后，**全部中文取词**都收敛到
// `presentation.js`：本模块不再自带第二份状态词表，只做转发与面板外壳。
// 因此：
//  - 旧的 `import {statusText} from './common.js'` 继续有效（同一个函数对象）；
//  - 新增业务面板应当优先从 `presentation.js` 取词；
//  - 任何地方都不得再手写 `ready/blocked/not_calculated/missing_data/stale` 的中文判断。
// =========================================================

import {statusText,statusBadge,escapeHtml} from './presentation.js';

export {statusText,statusBadge,escapeHtml};

// 集中映射的再导出：业务面板可以从这里一次取齐全部语境词表。
export {
  STATUS_TEXT,
  WORKFLOW_STATUS_TEXT,workflowStatusText,
  READINESS_TEXT,readinessText,readinessAllowsContinue,
  MATURITY_TEXT,maturityText,AUTHORITATIVE_CONTEXT_TEXT,authoritativeText,
  ASSESSMENT_TEXT,assessmentText,assessmentPassed,
  INPUT_REQUIREMENT_TEXT,inputRequirementText,inputRequirementBadge,
  CONSTRAINT_OUTCOME_TEXT,constraintOutcomeText,CONSTRAINT_OUTCOME_COLOR,
  CONSTRAINT_BLOCKER_TEXT,CONSTRAINT_BLOCKER_ORDER,
  CONSTRAINT_UNKNOWN_REASON_TEXT,constraintUnknownText,constraintUnknownReason,
  constraintBlockerList,constraintBlockerText,constraintCellSummary,
  CANONICAL_NODE_LABELS,canonicalNodeLabel,isCanonicalNode,
  VERTICAL_REFERENCE_TEXT,verticalReferenceText,
  altitudeLayerLabel,formatAltitude,
  SOURCE_STATE_TEXT,sourceStateText,emptyReasonText,
  advancedAuditNote,PRESENTATION_TABLES,PRESENTATION_VERSION
} from './presentation.js';

/** 数据来源模式 → 中文。 */
export const sourceModeText=mode=>({real:'真实数据',synthetic:'模拟数据',manual:'人工录入'}[mode]||mode||'来源未标明');

/**
 * 步骤面板外壳：返回**唯一根容器**。
 *
 * 标题信息写在根容器内部的 [data-workbench-head] 上：右侧工作台（workbench.js）
 * 每次导航都从这里读取步骤编号/标题/说明。该节点必须**常驻 DOM**——一旦被移除，
 * 后续切换一级标签或二级分段就没有标题元数据（真实浏览器会显示 00 与空标题）。
 * 业务内容与 wb-root 本身同样必须保留，否则工作台挂载时会丢失全部面板与控件。
 */
export const shell=(number,title,text,body)=>
  '<div class="wb-root">'
  +'<div data-workbench-head data-workbench-number="'+number+'" data-workbench-title="'+escapeHtml(title)+'" data-workbench-note="'+escapeHtml(text)+'"></div>'
  +body
  +'</div>';

/**
 * 每个 Step 固定的六区结构（B4X §4）：目标 → 输入准备 → 阻塞项/工程假设 →
 * 主操作 → 结果 → 下一步。
 *
 * 这只是**组织约定**：主操作区只允许一个 primary 按钮，次要操作进二级区或高级区。
 * 各步骤自行决定把六区映射到哪些已有的 tab/segment，不强制新造 DOM 结构。
 */
export const STEP_SECTIONS=['目标','输入准备','阻塞项与工程假设','主操作','结果','下一步'];

/**
 * 主操作区：全步只允许一个 primary 按钮（其余一律 secondary 或移入高级区）。
 * @param {string} body 按钮与说明
 * @param {{note?:string}} [options]
 */
export function primaryAction(body,{note=''}=''){
  return '<div class="wb-primary-action">'+(body||'')+(note?'<div class="wb-primary-note">'+escapeHtml(note)+'</div>':'')+'</div>';
}

/**
 * 阻塞项 / 工程假设区。
 * @param {Array<{text:string,kind?:'blocker'|'assumption',detail?:string}>} items
 * @param {string} [emptyNote] 没有阻塞项时的说明（绝不写成"通过"）
 */
export function blockerList(items,emptyNote='当前没有阻塞项'){
  const values=(items||[]).filter(item=>item&&item.text);
  if(!values.length)return '<div class="wb-empty">'+escapeHtml(emptyNote)+'</div>';
  return '<div class="wb-blockers">'+values.map(item=>{
    const kind=item.kind==='assumption'?'assumption':'blocker';
    const label=kind==='assumption'?'工程假设':'阻塞项';
    return '<div class="wb-blocker" data-kind="'+kind+'"><b>'+escapeHtml(label)+'</b>'
      +'<span>'+escapeHtml(item.text)+'</span>'
      +(item.detail?'<small>'+escapeHtml(item.detail)+'</small>':'')+'</div>';
  }).join('')+'</div>';
}

/** 下一步区：说明当前步骤推进到哪一步、以及为什么现在还不能推进。 */
export function nextStepBar({enabled=false,label='下一步',reason='',note=''}={}){
  return '<div class="wb-next-step">'
    +'<button class="primary full" id="nextStep" '+(enabled?'':'disabled')+'>'+escapeHtml(label)+'</button>'
    +(reason?'<div class="wb-next-reason">'+escapeHtml(reason)+'</div>':'')
    +(note?'<div class="wb-primary-note">'+escapeHtml(note)+'</div>':'')
    +'</div>';
}

/** 一级标签面板 / 二级分段面板的快捷构造，转发到 workbench 组件。
 *  wbSection 只接受"标题 + 状态徽章"；标题 + 整块正文请用 wbBlock，
 *  否则正文会落进 .wb-section-head 的 flex 行里被挤压。 */
export {panel as wbPanel,segPanel as wbSegPanel,segmentHint as wbSegHint,section as wbSection,block as wbBlock,metricCard as wbCard,metricGrid as wbCardGrid,emptyState as wbEmpty,snapshotLine as wbLine,engineFacts as wbEngine,disclosure as wbDisclosure,definitionList as wbList,dataTable as wbTable} from './workbench.js';
