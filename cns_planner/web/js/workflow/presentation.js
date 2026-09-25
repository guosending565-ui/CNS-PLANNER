/**
 * =========================================================================
 * 集中式前端展示映射（B4X · Six-Step Product UI Convergence）
 * =========================================================================
 *
 * 为什么存在
 * ----------
 * 在 B4X 之前，`ready` / `blocked` / `not_calculated` / `missing_data` / `stale`
 * 这类 raw enum 的中文判断散落在 step02 → step06 各自的渲染代码里，同一状态在不同
 * 步骤里可能被写成不同中文（甚至直接漏出英文 raw enum）。本模块把它收敛成**唯一**
 * 一份展示映射：所有业务面板只允许从这里取词，不得再手写第二套判断。
 *
 * 硬边界（本模块是纯展示层）
 * --------------------------
 *  - 不读取 `flow` / `state`，不发起请求，不写任何业务状态；
 *  - 不发明后端没有的语义：没有映射的 raw 值原样返回（**绝不**把未知状态伪装成"通过"）；
 *  - 不改变判定本身：`statusText()` 只做"raw → 中文"的取词，不参与任何 gate 计算；
 *  - CSS 类名仍然使用 raw 值（见 `statusBadge`），因此配色语义不因中文改名而变化。
 *
 * 词汇分层
 * --------
 *  1. `STATUS_TEXT`        全局默认词表（`not_calculated` → 未计算 等）；
 *  2. `WORKFLOW_STATUS_TEXT` 工作流执行语境（ready → 可继续，blocked → 暂不能继续）；
 *  3. `READINESS_TEXT`     就绪语境（可继续 / 可继续（采用工程假设）/ 暂不能继续）；
 *  4. `MATURITY_TEXT`      结果成熟度（provisional → 候选 / 试算结果；authoritative → 按语境）；
 *  5. `ASSESSMENT_TEXT`    检查结论（passed → 检查通过 …）；
 *  6. `INPUT_REQUIREMENT_TEXT` 输入需求等级（必需 / 可采用工程假设 / 可选 / 增强数据）；
 *  7. `CONSTRAINT_OUTCOME_TEXT` 约束场三态（可通行 / 障碍 / 证据不足）；
 *  8. `CONSTRAINT_BLOCKER_TEXT` / `CONSTRAINT_UNKNOWN_TEXT` 阻挡与证据不足原因；
 *  9. `CANONICAL_NODE_LABELS` canonical workflow 节点中文名；
 * 10. `VERTICAL_REFERENCE_TEXT` 垂向基准中文（绝不直接显示 `egm2008_orthometric`）。
 */

// ---- 1. 全局默认状态词表 ----------------------------------------------------

/**
 * 全局状态词表。**这是唯一的默认来源**；`common.js` 的 `statusText()` 直接转发到这里，
 * 因此旧的 import 路径（`from './common.js'`）继续有效，不存在第二份副本。
 *
 * 词条选择遵循两条既有约定（前端测试已锁定，B4X 不改写）：
 *  - `stale` → 「已失效」（不是「需要重新计算」）。工作流语境下需要「需要重新计算」时
 *    请用 `workflowStatusText()` / `constraintFreshness()`，不要在全局词表里改词。
 *  - `unknown` → 「证据不足/尚无法判断」，业务面板需要更短的「证据不足」时用
 *    `assessmentText()` / `constraintOutcomeText()`。
 */
export const STATUS_TEXT={
  // 计算生命周期
  not_calculated:'未计算',not_initialized:'未初始化',not_evaluated:'未评价',
  not_run:'未运行',not_available:'不可用',not_applicable:'不适用',
  // 数据
  missing_data:'缺少数据',unavailable:'不可用',partial:'部分覆盖',unsupported:'不可用',
  unknown:'证据不足/尚无法判断',unknown_category:'未知类别',
  pending_confirmation:'待确认',evidence_required:'需要补充证据',
  // 校验 / 评估
  passed:'通过',failed:'失败',breach:'净空突破',gap:'缺口',
  confirmed_gap:'确认缺口',confirmed_deficit:'确认缺口',warning:'警告',error:'错误',
  incomplete:'不完整',in_progress:'进行中',running:'正在计算',completed:'已完成',
  completed_with_warnings:'已完成（有提示）',
  // 有效性
  stale:'已失效',stale_current_project:'对应旧项目状态',current:'当前有效',
  confirmed:'已确认',applied:'已应用',not_confirmed:'尚未确认',not_applied:'尚未应用',
  draft:'草稿',blocked:'已阻断',superseded:'已被取代',revoked:'已撤销',
  // 需求 / 方案
  recommendation_ready:'需求建议可采用',adopted:'已采用',not_adopted:'尚未采用',
  no_action_required:'无需规划动作',no_eligible_proposal:'无可行方案',
  proposal_ready:'提案可审查',ready_for_confirmation:'可确认',selected:'已选择',
  // 规划目标
  objectives_met:'规划目标满足',objectives_not_met:'规划目标未满足',
  objectives_unknown:'规划目标证据不足',objectives_not_configured:'未配置规划目标',
  // 覆盖
  ready:'正常',no_coverage:'无覆盖',partial_intersection:'部分相交',full_coverage:'完全覆盖',
  // 约束场证据完整性（B3X 校验域状态）
  resolved:'已解析',resolved_unconfirmed:'已解析（未确认）',unresolved:'证据不足',
  confirmed_none:'已确认无',confirmed_present:'已确认存在',not_provided:'未提供',
  not_declared:'尚未声明',
  // 证据链与评估结论（plan review / route safety evidence / CNS 评估）
  evidence_complete:'证据完整',evidence_incomplete:'证据不完整',
  validated:'已验证',validated_candidate:'已验证候选',
  assessed:'已评估',not_ready:'尚不具备条件',
  meets_under_model:'在模型假设下满足',supported_under_model:'在模型假设下支持',
  operational_support_deficit:'运行支持能力不足',
  satisfied:'满足',not_satisfied:'不满足',under_redundant:'冗余不足',
  uncovered:'未覆盖',covered:'已覆盖',
  // 运行语义
  not_declared_for_planning:'规划模式尚未声明',assumed_empty:'按空既有设施工程基线规划',
  factual:'按事实数据规划',assume_empty_for_planning:'按空既有设施工程基线规划'
};

/**
 * raw 状态 → 中文。未登记的取值**原样返回**（绝不猜、绝不改成"通过"）。
 * @param {*} status raw enum
 * @returns {string}
 */
export function statusText(status){
  if(status===null||status===undefined||status==='')return '—';
  const key=String(status);
  return Object.prototype.hasOwnProperty.call(STATUS_TEXT,key)?STATUS_TEXT[key]:key;
}

/**
 * 状态徽章。CSS 类名始终使用 **raw** 值（`flow-<raw>`），中文只是显示文本。
 * 这是既有行为，B4X 不变更任何配色语义。
 */
export function statusBadge(status,label=''){
  const key=String(status??'');
  return '<span class="flow-badge flow-'+key+'">'+(label||statusText(status))+'</span>';
}

// ---- 2. 工作流状态 ----------------------------------------------------------

/** 工作流执行状态。ready → 可继续。 */
export const WORKFLOW_STATUS_TEXT={
  ready:'可继续',ready_with_assumptions:'可继续（采用工程假设）',blocked:'暂不能继续',
  running:'正在计算',completed:'已完成',completed_with_warnings:'已完成（有提示）',
  stale:'需要重新计算',failed:'执行失败',not_calculated:'未计算',
  pending_confirmation:'待确认',not_configured:'未配置'
};

/** 工作流执行状态 → 中文。 */
export function workflowStatusText(status){
  const key=String(status??'');
  if(Object.prototype.hasOwnProperty.call(WORKFLOW_STATUS_TEXT,key))return WORKFLOW_STATUS_TEXT[key];
  return statusText(status);
}

// ---- 3. 就绪状态 ------------------------------------------------------------

/** 就绪状态：三态 + 工程假设态。 */
export const READINESS_TEXT={
  ready:'可继续',ready_with_assumptions:'可继续（采用工程假设）',
  blocked:'暂不能继续',running:'正在计算',stale:'需要重新计算',
  not_calculated:'未计算',not_configured:'未配置',pending_confirmation:'待确认',
  unknown:'证据不足',completed:'已完成',completed_with_warnings:'已完成（有提示）',
  failed:'执行失败'
};

/** 就绪状态 → 中文。 */
export function readinessText(state){
  const key=String(state??'');
  if(Object.prototype.hasOwnProperty.call(READINESS_TEXT,key))return READINESS_TEXT[key];
  return statusText(state);
}

/** 就绪状态是否允许继续（只读判定，不改变任何后端 gate）。 */
export function readinessAllowsContinue(state){
  return state==='ready'||state==='ready_with_assumptions';
}

// ---- 4. 结果成熟度 ----------------------------------------------------------

/**
 * 结果成熟度。`authoritative` 必须按业务 context 取词，因此这里给出三个别名，
 * 默认落回「已确认」（最保守的通用说法）。
 */
export const MATURITY_TEXT={
  provisional:'候选 / 试算结果',authoritative:'已确认',
  research:'研究对照',experimental:'实验',deprecated:'已弃用',unknown:'证据不足'
};

/** 结果成熟度 → 中文。 */
export function maturityText(maturity){return textOr(MATURITY_TEXT,maturity);}

/**
 * authoritative 的业务语境词：同一份权威结果在不同业务面读作"已发布 / 已采纳 / 已确认"。
 * context 由调用方显式给出（绝不根据文案反推业务语义）。
 */
export const AUTHORITATIVE_CONTEXT_TEXT={
  published:'已发布',adopted:'已采纳',confirmed:'已确认',applied:'已应用'
};

/** 按业务语境显示权威结果状态。 */
export function authoritativeText(context){
  const key=String(context??'');
  if(Object.prototype.hasOwnProperty.call(AUTHORITATIVE_CONTEXT_TEXT,key)){
    return AUTHORITATIVE_CONTEXT_TEXT[key];
  }
  return MATURITY_TEXT.authoritative;
}

// ---- 5. 检查结论 ------------------------------------------------------------

/** 域检查结论（地形 / 建筑 / 铁塔 / 禁飞受限区域 …）。 */
export const ASSESSMENT_TEXT={
  passed:'检查通过',failed:'检查不通过',unknown:'证据不足',
  not_evaluated:'尚未检查',not_applicable:'不适用',stale:'需要重新检查',
  warning:'检查通过（有提示）',partial:'部分通过'
};

/** 检查结论 → 中文。 */
export function assessmentText(outcome){return textOr(ASSESSMENT_TEXT,outcome);}

/** 检查结论是否可视为"通过"（只有 passed 才是通过，unknown 绝不算通过）。 */
export function assessmentPassed(outcome){
  return outcome==='passed'||outcome==='validated';
}

// ---- 6. 输入需求等级 --------------------------------------------------------

/** Step01「必要输入清单」的需求等级。 */
export const INPUT_REQUIREMENT_TEXT={
  required:'必需',assumable:'可采用工程假设',optional:'可选',enhanced:'增强数据',
  // 兼容后端可能出现的等价取值
  mandatory:'必需',recommended:'可采用工程假设',nice_to_have:'增强数据'
};

/** 需求等级 → 中文。 */
export function inputRequirementText(level){return textOr(INPUT_REQUIREMENT_TEXT,level);}

/** 需求等级徽章（tone 只表达等级，不表达"缺失=失败"）。 */
export function inputRequirementBadge(level){
  const key=String(level??'');
  return '<span class="requirement-badge" data-level="'+escapeAttribute(key)+'">'
    +escapeText(inputRequirementText(level))+'</span>';
}

// ---- 7. 约束场三态 ----------------------------------------------------------

/**
 * Planning Constraint Field 三态（B3X `CONSTRAINT_OUTCOMES`）。
 * 语义必须与后端逐字对应：pass 可通行 / blocked 障碍 / unknown 证据不足。
 */
export const CONSTRAINT_OUTCOME_TEXT={
  pass:'可通行',blocked:'障碍',unknown:'证据不足',
  not_calculated:'尚未计算',stale:'需要重新计算'
};

/** 约束格结果 → 中文。 */
export function constraintOutcomeText(outcome){return textOr(CONSTRAINT_OUTCOME_TEXT,outcome);}

/** 约束场三态的地图配色（障碍优先突出；unknown 不允许被隐藏成"可通行"）。 */
export const CONSTRAINT_OUTCOME_COLOR={
  blocked:'#c92a2a',unknown:'#f0a020',pass:'#3f9e6a'
};

// ---- 8. 约束阻挡 / 证据不足原因 ---------------------------------------------

/**
 * `blocked_by` 的域 → 中文。顺序即弹窗显示顺序（主要阻挡原因优先）。
 * 与后端 `domain.planning_constraint_field.BLOCKER_DOMAINS` 一一对应。
 */
export const CONSTRAINT_BLOCKER_TEXT={
  terrain:'地形',building:'建筑',tower:'铁塔',
  airspace:'禁飞/受限区域',critical_site:'保护要地'
};

/** 阻挡域显示顺序（与后端 BLOCKER_DOMAINS 同序）。 */
export const CONSTRAINT_BLOCKER_ORDER=['terrain','building','tower','airspace','critical_site'];

/**
 * 后端 `unknown_reasons[].reason` → 中文（覆盖 B3X 全部 reason code）。
 * 未登记的 reason 原样返回：宁可显示技术码，也不编造业务解释。
 */
export const CONSTRAINT_UNKNOWN_REASON_TEXT={
  terrain_evidence_or_clearance_unresolved:'地形证据或垂直净空未解析',
  building_ground_height_or_status_unresolved:'建筑地面高程或高度状态未解析',
  tower_dataset_unresolved:'铁塔数据尚未解析',
  tower_clearance_unresolved:'铁塔垂直净空未配置',
  tower_top_not_confirmed:'塔顶高程尚未确认',
  restricted_area_dataset_unresolved:'禁飞/受限区域数据尚未解析',
  protected_site_dataset_unresolved:'保护要地数据尚未解析',
  restricted_area_unconfirmed:'受限区域尚未确认',
  restricted_area_vertical_scope_unresolved:'受限区域垂向适用范围未解析',
  conditional_policy_unresolved:'条件性禁飞区尚未确认策略',
  source_point_only_no_protection_geometry:'仅有源点，缺少保护几何',
  unknown:'证据不足'
};

/** 证据不足原因 → 中文。 */
export function constraintUnknownText(reason){
  if(reason&&typeof reason==='object')return constraintUnknownReason(reason);
  return textOr(CONSTRAINT_UNKNOWN_REASON_TEXT,reason);
}

/** 单条 unknown_reason 记录 → 中文短语（含域与可选 feature / tower id）。 */
export function constraintUnknownReason(reason){
  const item=reason&&typeof reason==='object'?reason:{};
  const domain=item.domain?textOr(CONSTRAINT_BLOCKER_TEXT,item.domain):'';
  const detail=textOr(CONSTRAINT_UNKNOWN_REASON_TEXT,item.reason);
  const owner=item.tower_id||item.feature_id||'';
  const head=domain?domain+'：':'';
  return head+detail+(owner?'（'+owner+'）':'');
}

/**
 * unknown_reasons 列表 → 去重后的中文短语数组。
 * 同一域可能出现多条原因（例如多座未确认塔），这里按短语去重并保留出现顺序。
 */
export function constraintUnknownSummary(reasons){
  const values=Array.isArray(reasons)?reasons:[];
  const seen=new Set(),result=[];
  for(const reason of values){
    const text=constraintUnknownReason(reason);
    if(!text||text==='—'||seen.has(text))continue;
    seen.add(text);result.push(text);
  }
  return result;
}

/**
 * 约束场新鲜度：**主界面只说「当前」或「需要重新计算」**（B4X §10）。
 * 完整 fingerprint 只允许出现在高级 / 审计区。
 */
export const CONSTRAINT_FRESHNESS_TEXT={
  current:'当前',stale:'需要重新计算',not_calculated:'尚未计算',
  unavailable:'数据源不可用',pending:'待确认'
};

/** 约束场新鲜度 → 中文。 */
export function constraintFreshness(state){return textOr(CONSTRAINT_FRESHNESS_TEXT,state);}

/** 约束场状态卡的 raw → 中文（与三态同源，不另立词表）。 */
export function constraintFieldStatusText(status){
  const key=String(status??'');
  if(key==='completed'||key==='completed_with_warnings'||key==='passed')return '已生成';
  if(key==='stale')return CONSTRAINT_FRESHNESS_TEXT.stale;
  return constraintOutcomeText(key);
}

/**
 * 阻挡原因列表 → 中文短语数组（支持多原因）。
 * @param {string[]} blockedBy 后端 `blocked_by`
 * @returns {string[]} 例如 ['地形','建筑']
 */
export function constraintBlockerList(blockedBy){
  const values=Array.isArray(blockedBy)?blockedBy:[];
  const known=CONSTRAINT_BLOCKER_ORDER.filter(domain=>values.map(String).includes(domain));
  const extra=values.map(String).filter(domain=>!CONSTRAINT_BLOCKER_ORDER.includes(domain));
  return [...known,...extra].map(domain=>textOr(CONSTRAINT_BLOCKER_TEXT,domain));
}

/** 阻挡原因 → 单行中文（多原因用「、」连接）。 */
export function constraintBlockerText(blockedBy){
  const list=constraintBlockerList(blockedBy);
  return list.length?list.join('、'):'—';
}

/** 一个约束格的中文摘要（地图弹窗与状态卡的唯一取词入口）。 */
export function constraintCellSummary(cell){
  const item=cell&&typeof cell==='object'?cell:{};
  const outcome=String(item.outcome||'unknown');
  const lines=['高度层 '+ (item.altitude_layer_id||'—')];
  lines.push('状态 '+constraintOutcomeText(outcome));
  if(outcome==='blocked')lines.push('主要阻挡原因 '+constraintBlockerText(item.blocked_by));
  if(outcome==='unknown'){
    const reasons=(item.unknown_reasons||[]).slice(0,3).map(constraintUnknownReason);
    lines.push('证据不足原因 '+(reasons.length?reasons.join('；'):'—'));
  }
  return lines;
}

// ---- 9. canonical workflow 节点 ---------------------------------------------

/**
 * canonical workflow 节点 → 中文名。键与后端 `domain/canonical_workflow.py` 的节点 id
 * 一一对应；未登记的节点 id 原样返回（绝不猜）。
 */
export const CANONICAL_NODE_LABELS={
  environment:'环境模型',
  risk_field:'航路风险场',
  route_candidate:'候选航路',
  route_validation:'航路安全验证',
  operational_route:'正式运行航路',
  required_cns:'CNS能力需求',
  coverage:'三维覆盖评估',
  service_capability:'服务能力评估',
  service_corridor:'CNS服务走廊',
  capability_gap:'CNS能力缺口',
  facility_plan:'CNS设施规划',
  plan_review:'方案评审',
  report:'规划报告'
};

/** canonical 节点 id → 中文名。 */
export function canonicalNodeLabel(node){return textOr(CANONICAL_NODE_LABELS,node);}

/** canonical 节点 id 是否是本产品的主链节点。 */
export function isCanonicalNode(node){
  return Object.prototype.hasOwnProperty.call(CANONICAL_NODE_LABELS,String(node??''));
}

// ---- 10. 垂向基准 -----------------------------------------------------------

/**
 * 垂向基准 raw 值 → 用户可理解中文。
 * `egm2008_orthometric` 显示为「EGM2008 正高」，绝不直接把 raw 值丢给用户。
 */
export const VERTICAL_REFERENCE_TEXT={
  egm2008_orthometric:'EGM2008 正高',egm2008:'EGM2008 正高',
  wgs84_ellipsoidal:'WGS84 椭球高',ellipsoidal:'WGS84 椭球高',
  agl:'距地高度（AGL）',msl:'平均海平面高（MSL）',amsl:'平均海平面高（MSL）',
  unknown:'垂向基准未确认'
};

/** 垂向基准 → 中文。 */
export function verticalReferenceText(reference){
  const key=String(reference??'');
  if(!key)return VERTICAL_REFERENCE_TEXT.unknown;
  return textOr(VERTICAL_REFERENCE_TEXT,key);
}

// ---- 11. 高度层显示 ---------------------------------------------------------

/**
 * 高度层的一行中文显示，例如 ``ALT-080 · 80 m · EGM2008 正高``。
 *
 * 规则（B4X §9）：
 *  - 绝不写死 ALT-080 / 80 m：一切取自传入的 AltitudeLayer 目录条目；
 *  - 缺少 nominal 时显示「nominal 高度待工程确认」，**绝不**取上下界中值；
 *  - 缺少垂向基准时显示「垂向基准未确认」，绝不猜 datum。
 */
export function altitudeLayerLabel(layer){
  const item=layer&&typeof layer==='object'?layer:{};
  const id=String(item.altitude_layer_id||'').trim();
  const nominal=item.nominal_altitude_m;
  const altitude=(nominal===null||nominal===undefined||nominal==='')
    ?'nominal 高度待工程确认'
    :formatAltitude(nominal)+' '+verticalReferenceText(item.vertical_reference);
  return [id||'未命名高度层',altitude].join(' · ');
}

/** 高度值的可读文本（保留必要小数，不补零）。 */
export function formatAltitude(value){
  const number=Number(value);
  if(!Number.isFinite(number))return '—';
  const text=Number.isInteger(number)?String(number):String(Number(number.toFixed(2)));
  return text+' m';
}

// ---- 12. 数据源 / 空状态 ----------------------------------------------------

/**
 * 空状态与不可用原因的**分类**文案（B4X §25）。
 *
 * 设计意图：用户不应该看到 `source unavailable · —`，也不应该所有异常都写成"失败"。
 * 因此这里按**真实原因**分类取词；调用方必须显式给出 reason code，禁止用一句"失败"兜底。
 */
export const SOURCE_STATE_TEXT={
  unavailable:'数据源不可用',not_configured:'尚未配置数据源',
  not_calculated:'尚未计算',stale:'需要重新计算',
  evidence_insufficient:'证据不足',no_result:'暂无结果',
  loading:'正在加载',ready:'数据源正常',warning:'数据源有告警',error:'数据源错误',
  checking:'正在检查数据源'
};

/** 数据源 / 空状态原因 → 中文。 */
export function sourceStateText(reason){return textOr(SOURCE_STATE_TEXT,reason);}

/** 空结果块：解释为什么没有内容，而不是假装有数据、也不统一写成"失败"。 */
export function emptyReasonText(reason,detail=''){
  const head=sourceStateText(reason);
  return detail?head+'：'+detail:head;
}

// ---- 13. 高级 / 审计区标识 --------------------------------------------------

/**
 * 开发阶段编号（P1 / P7 / P8 / P14 / P15 / P16）与旧版本代号（V1 / V2 / V3）只允许
 * 出现在「高级 / 审计信息」区。本函数把这些标识统一收进高级区的说明行，
 * 生产主界面因此永远不出现开发术语。
 */
export function advancedAuditNote(text){
  return '<div class="advanced-audit-note"><span class="advanced-audit-tag">高级 / 审计信息</span>'
    +escapeText(text)+'</div>';
}

// ---- 内部工具 ---------------------------------------------------------------

/**
 * 通用 HTML 转义：全部业务面板共用的**唯一**实现（纯字符串，不依赖 DOM）。
 * `common.js` 从本模块转发它，因此旧的 `from './common.js'` 导入继续有效。
 */
export function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}

function textOr(table,key){
  const raw=String(key??'');
  if(!raw)return '—';
  return Object.prototype.hasOwnProperty.call(table,raw)?table[raw]:raw;
}

function escapeText(value){return escapeHtml(value);}

function escapeAttribute(value){return escapeHtml(value);}

/** 供测试与审计读取：本模块导出的全部映射表名。 */
export const PRESENTATION_TABLES=[
  'STATUS_TEXT','WORKFLOW_STATUS_TEXT','READINESS_TEXT','MATURITY_TEXT',
  'AUTHORITATIVE_CONTEXT_TEXT','ASSESSMENT_TEXT','INPUT_REQUIREMENT_TEXT',
  'CONSTRAINT_OUTCOME_TEXT','CONSTRAINT_OUTCOME_COLOR','CONSTRAINT_BLOCKER_TEXT',
  'CONSTRAINT_BLOCKER_ORDER','CONSTRAINT_UNKNOWN_REASON_TEXT','CONSTRAINT_FRESHNESS_TEXT',
  'CANONICAL_NODE_LABELS','VERTICAL_REFERENCE_TEXT','SOURCE_STATE_TEXT'
];

export const PRESENTATION_VERSION='b4x-presentation@1.0';
