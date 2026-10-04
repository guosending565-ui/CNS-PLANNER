// =========================================================
// Step06 方案评审：评审 / 决策 / 交付工作台
//
// 信息架构（B4X §23 主链 / §24 中文优先 / §25 空状态分类）
//  - 操作：评审概览 / 方案比较 / 方案编辑 / 确认与应用
//  - 结果：状态总览 / 报告与交付
//  - 高级：需求依据 / 布站提案证据
//  每个一级标签下同一时刻只显示一个任务（由 workbench.js 的 .wb-seg-active 保证）。
//
// canonical 主链（界面上唯一可见的推进路径）
//   方案比较 → 选择方案 → 确认方案 → 应用确认方案 → 生成规划报告
//
// 语义边界（本次重组只改展示组织与取词，不改任何业务契约）
//  - Select（选择方案）≠ Confirm（冻结快照）≠ Apply（事务提交）：三个动作各自独立，
//    「已选择」绝不自动确认，「已确认」绝不自动应用；后端 gate 一条都不放宽，
//    前端只如实显示 gate 状态与中文原因。
//  - 只消费 canonical 结果：flow.cns_plan_review / flow.confirmed_cns_plan /
//    flow.cns_corridor_site_plan / flow.cns_planning_reports。
//    compatibility 结果（flow.compatibility_*、旧版二维覆盖 / 旧版站址试算 /
//    方案影响试算）只作审计对照，**绝不参与正式确认**（见 compatibilityIsolation）。
//  - 本步就绪判定沿用后端 canonical step 标记 flow.steps['6']：后端
//    workflow_service._steps() 只由 coverage_3d / cns_service_capability /
//    cns_corridor_assessment / cns_corridor_gap_assessment / cns_corridor_site_plan
//    决定，旧版兼容结果既不能解锁也不能阻止确认。
//  - Plan Variant 始终是人工决策候选：不计算隐藏总分、排名或优胜者，
//    也没有任何"自动推荐 / 自动选择"通道；
//  - 所有状态都从现有 flow / state 派生，不新增、不重算任何结论；
//  - 所有既有 DOM id、API path、payload 与 bind 语义保持原样。
// =========================================================
import {escapeHtml,shell,statusBadge,statusText,readinessText,emptyReasonText,
  advancedAuditNote,blockerList,wbPanel,wbBlock,wbSegHint,wbDisclosure} from './common.js';
import {
  ACCEPTABILITY_TEXT,STEP6_ALLOWED_ACCEPTABILITY,continuousServiceModel,limitationRows,
  parameterRows,step6GateAllows,step6GateModel,threatLayerRows,
} from './continuous_service.js';
import {activeMapFigure,applicabilityText,getMapFigureState,refreshMapFigureState,
  resetMapFigureState} from
  './map_figure_state.js';

// ---- 二级任务分段 -----------------------------------------------------------
// id 稳定（review-*），标签是第一视觉层的业务语言：
// 不把 P 编号 / 版本代号 / 算法 id 当作导航语言（它们只出现在高级标签里）。
export const REVIEW_SEGMENTS={
  operate:[['review-op-overview','评审概览'],['review-op-compare','方案比较'],['review-op-edit','方案编辑'],['review-op-confirm','确认与应用']],
  result:[['review-res-status','状态总览'],['review-res-report','报告与交付']],
  advanced:[['review-adv-requirement','需求依据'],['review-adv-proposal','布站提案证据']]
};

/** 本步 canonical 主链：界面上唯一可见的推进路径（三个动作彼此独立）。 */
export const REVIEW_MAIN_CHAIN='方案比较 → 选择方案 → 确认方案 → 应用确认方案 → 生成规划报告';

/**
 * 六区语义的标题（B4X §4）：目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步。
 * 各二级分段按自身语义选用其中的若干区；标题统一从这里取，
 * 既避免同一个区在不同分段换名字，也不让界面出现开发术语。
 */
const SIX_SECTIONS={goal:'目标',input:'输入准备',blockers:'阻塞项与工程假设',action:'主操作',result:'结果',next:'下一步'};

/**
 * 方案审查摘要：Select / Confirm / Apply / 报告状态彼此独立。
 * 契约保持原样（frontend_modules 测试逐字段锁定）。
 */
export function planReviewSummary(review,confirmed){
  const variants=review?.variants||[],selected=variants.find(item=>item.variant_id===review?.selected_variant_id)||null;
  return {variantCount:variants.length,selectedVariantId:selected?.variant_id||null,
    selectedActionIds:selected?.selected_action_ids||[],gate:selected?.evaluation?.confirmation_gate?.status||'not_evaluated',
    confirmedStatus:confirmed?.status||'not_confirmed',applyStatus:confirmed?.application?.status||'not_applied'};
}

// ---- 工具 -------------------------------------------------------------------
function list(value){return String(value||'').split(',').map(item=>item.trim()).filter(Boolean);}
function number(value){return Number.isFinite(value)?value.toFixed(1):'—';}
/** 体素计数三元组：满足 / 确认缺口 / 证据不足。 */
function formatDist(value){const counts=value?.voxel_counts||{};return [counts.satisfied||0,counts.confirmed_deficit??counts.confirmed_gap??0,counts.unknown||0].join('/');}
function formatCosts(summary){const values=summary?.explicit_costs_by_unit||{},text=Object.entries(values).map(([unit,value])=>number(value)+' '+escapeHtml(unit)).join('；');return text||'动作数量代理口径（action_count_proxy，未伪造货币成本）';}
function statusOf(map,key){return map&&map[key]?map[key]:'not_calculated';}
function kvRow(label,value,note){return '<div class="review-row"><span>'+escapeHtml(label)+'</span><span class="review-value">'+(value||'—')+(note?'<small>'+escapeHtml(note)+'</small>':'')+'</span></div>';}
function badgeRow(label,status,note){return kvRow(label,statusBadge(status),note);}
function reviewBlock(title,body,note){return '<div class="review-block"><b>'+escapeHtml(title)+'</b>'+(body||'')+(note?'<small>'+escapeHtml(note)+'</small>':'')+'</div>';}
/** 六区"下一步"区的文字说明：本步没有 nextStep 按钮（推进由二级分段承担），因此只写明下一步做什么、以及为什么还不能做。 */
function nextStepNote(text){return '<p class="parameter-note">'+escapeHtml(text)+'</p>';}

/**
 * 状态总览：按业务组展示既有 status。
 * 只映射现有 result_statuses / risks / review 的取值，不重新计算任何结论。
 */
const STATUS_GROUPS=[
  ['规划输入',[['workspace','工作区'],['grid','标准网格'],['routes','运行航路']]],
  ['CNS 规划',[
    ['coverage_3d','三维几何覆盖'],['cns_service_capability','CNS 服务能力'],
    ['cns_corridor_assessment','CNS 服务走廊'],
    ['cns_corridor_gap_assessment','CNS 能力缺口'],['cns_corridor_site_plan','CNS 设施规划'],
    ['service_timeline','服务时间线'],['protection_envelope','保护包络'],['cns_gap_v2','规划缺口'],['building_clearance','建筑净空'],
    ['route_vertical_profiles','航路垂直剖面'],['encounter_3d_assessment','3D 相遇评估'],
    ['layered_route_candidate','分层航路候选']
  ]],
  ['风险',[
    ['environment_risk','环境风险'],['grid_risk_v2','网格风险'],['technical_risk','技术风险'],
    ['safety_assessment','安全评估']
  ]],
  ['方案与交付',[
    ['cns_plan_review','方案审查'],
    ['required_cns_recommendation','需求建议'],['report','规划报告']
  ]]
];
const EXTRA_RISK_LABELS=[['life','生命风险'],['property','财产风险']];

// ---- 六区公共区块：阻塞项与兼容隔离 -----------------------------------------

/**
 * 兼容结果隔离（B4X §23）。
 *
 * 旧版二维覆盖 / 旧版规划缺口 / 旧版站址试算与方案影响试算只作审计对照，
 * **不参与正式确认**。这里只读取 flow.result_statuses 中既有状态并如实显示；
 * 既不读取 flow.compatibility_*，也**不**从任何兼容结果推导"可确认"——
 * 可确认只来自 canonical 结果与后端 confirmation_gate。
 */
function compatibilityIsolation(flow){
  const statuses=flow.result_statuses||{};
  const rows=[['coverage','旧版二维覆盖试算'],['cns_gap','旧版规划缺口'],['cns_site_plan','旧版站址试算']]
    .map(([key,label])=>badgeRow(label,statusOf(statuses,key)));
  return reviewBlock('旧版兼容结果（不参与正式确认）',rows.join('')
    +'<p class="parameter-note">旧版二维覆盖、旧版规划缺口、旧版站址试算与方案影响试算只用于审计对照：它们的状态既不构成确认依据，也不能放宽或触发「确认方案」「应用已确认方案」「生成正式报告」的任何门禁。</p>');
}

/**
 * 本步的阻塞项与工程假设：只从 canonical 结果与后端 gate 派生。
 * 兼容结果不参与，因此它们的状态不会在这里产生或解除任何阻塞项。
 */
function reviewBlockers(flow){
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{};
  const selected=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id)||null;
  const gate=selected?.evaluation?.confirmation_gate||{},gateStatus=gate.status||'not_evaluated';
  // canonical step 标记只由正式 CNS 链决定（后端 workflow_service._steps()）。
  const canonicalReady=flow.steps?.['6']===true;
  const reports=flow.cns_planning_reports||{};
  const active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id)||null;
  const items=[];
  if(!canonicalReady)items.push({kind:'blocker',text:'正式 CNS 规划链尚未就绪',
    detail:'需要三维几何覆盖、CNS 服务能力、CNS 服务走廊、CNS 能力缺口与 CNS 设施规划都已形成当前结果；旧版二维覆盖与旧版站址试算不能解锁正式确认。'});
  if(review.status!=='current')items.push({kind:'blocker',text:'方案审查尚未初始化或已失效',
    detail:'请到「方案比较」初始化方案审查，系统会按当前规划基线生成候选方案。'});
  else if(!selected)items.push({kind:'blocker',text:'尚未选择候选方案',
    detail:'请到「方案比较」选择一个候选方案：选择只切换候选，不等于确认。'});
  if(selected&&gateStatus==='objectives_not_configured')items.push({kind:'assumption',text:'该方案未配置规划目标',
    detail:'只有在人工勾选知情确认并填写确认理由后，才能确认该方案；系统不会自动勾选，也不会替用户确认。'});
  else if(selected&&gateStatus!=='ready_for_confirmation')items.push({kind:'blocker',text:'确认门禁尚未打开',
    detail:'当前确认门禁状态：'+statusText(gateStatus)+'。'});
  //: Round 2.6：每个 variant 必须有自己的 P17 投影结论；未评估 / 投影结论不可接受
  //: 时一律 fail-closed（与后端 _variant_p17_gate 一致，前端只如实显示）。
  if(selected){
    const p17=variantContinuousServiceProjection(selected);
    if(!p17.evaluated){
      items.push({kind:'blocker',text:'本 variant 尚未做 P17 评估',
        detail:'projected_status='+String(p17.projectedStatus??'缺失')
          +'：该 variant 的 P17 投影结论不可用，因此保持不可判定并阻止确认（fail-closed）；'
          +'绝不把它显示为通过。请重新评价该方案或调整动作后重试。'});
    }else if(!p17.passes){
      items.push({kind:'blocker',text:'本 variant 实施后的 P17 结论不允许确认',
        detail:'本 variant 实施后的 P17 结论：'+p17.label
          +'。只有 fully_satisfied / acceptable_with_managed_gap 才允许进入正式评审。'});
    }
  }
  if(confirmed.status==='confirmed'&&confirmed.current_applicability!=='current')items.push({kind:'blocker',
    text:'已确认方案对应旧项目状态',detail:'需要重新初始化方案审查并重新确认，才能再次应用。'});
  if(active&&active.current_applicability==='stale_current_project')items.push({kind:'blocker',
    text:'当前正式报告对应旧项目状态',detail:'旧报告仍可下载，系统不会自动覆盖或删除；重新确认并应用方案后可生成新报告。'});
  return items;
}

/** 报告分段自己的阻塞项：同样只读后端 gate，不引入任何兼容结果。 */
function reportBlockers(active,hasPlan){
  const items=[];
  if(!hasPlan)items.push({kind:'blocker',text:'尚未确认规划方案',
    detail:'正式报告只接受已确认（或已应用）的方案；当前只能预览草稿，草稿不写入项目。'});
  if(active&&active.current_applicability==='stale_current_project')items.push({kind:'blocker',
    text:'当前报告对应旧项目状态',detail:'旧报告仍可继续下载，系统不会自动覆盖或删除；重新确认并应用方案后可生成新报告。'});
  return items;
}

// ---- 评审概览 ---------------------------------------------------------------
function projectOverview(flow,state){
  const coverage=flow.coverage_3d||{},coverageRoutes=coverage.routes||[];
  return reviewBlock(SIX_SECTIONS.input,[
    kvRow('数据源',statusBadge(state.data_health.status)),
    kvRow('工作区',flow.workspace?flow.workspace.area_km2+' km²':'未定义'),
    kvRow('运行航路',String(flow.operational_routes.length),flow.operational_routes.map(item=>item.route_id).join('、')),
    kvRow('飞行器',flow.aircraft?escapeHtml(flow.aircraft.manufacturer+' '+flow.aircraft.model):'未设置'),
    kvRow('飞行规则',statusBadge(flow.rules?.status||'not_calculated')),
    kvRow('三维覆盖评估',statusBadge(coverage.status||'not_calculated'),coverageRoutes.length+' 条航路')
  ].join(''));
}

/** 评审流程状态：只读现有 canonical 结果，不做任何结论判断。 */
function reviewStatusPanel(flow){
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const application=confirmed.application||{},reports=flow.cns_planning_reports||{};
  const active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id)||null;
  return reviewBlock('方案审查与受控应用',[
    kvRow('方案审查',statusBadge(review.status||'not_initialized'),'初始化后由人工比较与选择'),
    kvRow('候选方案数',String(summary.variantCount)),
    kvRow('已选方案',summary.selectedVariantId?escapeHtml(summary.selectedVariantId):'尚未选择'),
    kvRow('所选动作数',String(summary.selectedActionIds.length)),
    kvRow('确认门禁',statusBadge(summary.gate)),
    kvRow('已确认方案',statusBadge(summary.confirmedStatus)),
    kvRow('应用状态',statusBadge(summary.applyStatus),application.planning_origin?'来源方案编号：'+escapeHtml(String(application.planning_origin.plan_id||'')):''),
    kvRow('规划报告',statusBadge(active?active.current_applicability:(reports.status||'not_calculated')),
      active?'报告编号：'+escapeHtml(active.report_id):emptyReasonText('no_result','尚无正式报告：需要在「报告与交付」中生成'))
  ].join(''),'候选方案无自动总分/排名，也不自动推荐或选择；选择 / 确认 / 应用三步语义彼此独立。');
}

/** 评审概览：六区语义的入口分段（目标 → 输入准备 → 阻塞项 → 主操作 → 结果 → 下一步）。 */
function overviewPanel(flow,state){
  return reviewBlock(SIX_SECTIONS.goal,
      nextStepNote('主链：'+REVIEW_MAIN_CHAIN+'。选择 / 确认 / 应用三者彼此独立：不会因为「已选择」就自动确认，也不会因为「已确认」就自动应用。'))
    +projectOverview(flow,state)
    +reviewBlock(SIX_SECTIONS.blockers,blockerList(reviewBlockers(flow),
      '当前没有阻塞项：主链可以按「选择 → 确认 → 应用 → 生成报告」继续推进。'))
    +reviewBlock(SIX_SECTIONS.action,
      nextStepNote('主操作按主链顺序分布在各分段，每个分段只有一个主操作按钮：方案比较 → 初始化方案审查；方案编辑 → 创建候选方案；确认与应用 → 应用已确认方案；报告与交付 → 生成正式报告。选择候选方案使用候选卡上的「选择此方案」，它只切换当前候选，不修改任何设施。'))
    +reviewBlock(SIX_SECTIONS.result,reviewStatusPanel(flow))
    +reviewBlock(SIX_SECTIONS.next,
      nextStepNote('下一步：到「方案比较」初始化方案审查并选择候选方案；确认门禁打开后进入「确认与应用」。'));
}

// ---- 方案比较：紧凑比较卡 + 完整矩阵 ----------------------------------------
function comparisonRowCard(row){
  const objective=row.objective_status||'not_evaluated',unknown=(row.unknown_voxel_ids||[]).length;
  return '<div class="review-block comparison-card">'
    +'<div class="comparison-head"><b>'+escapeHtml(row.route_id||'—')+' · '+escapeHtml(row.subsystem||'—')+'</b>'+statusBadge(objective)+'</div>'
    +'<div class="comparison-body">'
    +'<div class="comparison-item"><span>服务 满足/缺口/未知</span><b>'+escapeHtml(formatDist(row.service))+'</b><small>连续缺口投影 '+number(row.total_confirmed_deficit_projection_m)+' m</small></div>'
    +'<div class="comparison-item"><span>冗余 满足/缺口/未知</span><b>'+escapeHtml(formatDist(row.redundancy))+'</b><small>最大连续缺口 '+number(row.max_continuous_deficit_projection_m)+' m · 证据不足体素 '+unknown+'</small></div>'
    +'</div></div>';
}
function comparisonCards(selected){
  const matrix=selected?.evaluation?.comparison_matrix||[];
  if(!selected)return '<div class="wb-empty">先初始化方案审查并选择一个方案，再查看逐航路比较卡。</div>';
  if(!matrix.length)return '<div class="wb-empty">所选方案尚无逐航路客观指标；请先重新评价该方案。</div>';
  return matrix.map(comparisonRowCard).join('');
}
/** 完整 Objective Comparison Matrix：原 8 列表格，收进 wbDisclosure。 */
function comparisonMatrixTable(selected){
  const matrix=selected?.evaluation?.comparison_matrix||[];
  const rows=matrix.map(row=>'<tr><td>'+escapeHtml(row.route_id)+'</td><td>'+escapeHtml(row.subsystem)+'</td><td>'+statusText(row.objective_status||'not_evaluated')+'</td><td>'+formatDist(row.service)+'</td><td>'+formatDist(row.redundancy)+'</td><td>'+number(row.total_confirmed_deficit_projection_m)+'</td><td>'+number(row.max_continuous_deficit_projection_m)+'</td><td>'+(row.unknown_voxel_ids||[]).length+'</td></tr>').join('');
  const table='<div class="table-wrap"><table><thead><tr><th>航路</th><th>C/N/S</th><th>规划目标</th><th>服务 满足/缺口/未知</th><th>冗余 满足/缺口/未知</th><th>缺口总长 m</th><th>最大连续缺口 m</th><th>证据不足体素</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  return wbDisclosure('完整客观指标矩阵',matrix.length?table:'<div class="wb-empty">尚未评价所选方案。</div>');
}

// ---- 方案选择 / 方案编辑 / 确认与应用 ----------------------------------------

/**
 * 某个 Plan Variant 的 P17 投影结论（Round 2.6）。
 *
 * 契约：``variant.evaluation.continuous_service_projection``，只转印不推导：
 * ``{evaluated_for_this_variant, projected_status, projected_managed_gap_count,
 *   projected_unacceptable_count, projected_unknown_count, status, comparison,
 *   limitations[], applied_action_ids[], ...}``。
 *
 * fail-closed 规则：``evaluated_for_this_variant !== true`` ⇒ 该 variant **尚未做
 * P17 评估**；``projected_status`` 缺失时**绝不显示为通过**，而是「不可判定 / 阻止」。
 */
export function variantContinuousServiceProjection(variant){
  const evaluation=variant?.evaluation||{};
  const raw=evaluation.continuous_service_projection||null;
  const projection=raw||{};
  const evaluated=projection.evaluated_for_this_variant===true;
  const projectedStatus=projection.projected_status??projection.status??null;
  const gate=projection.variant_specific_gate||{};
  //: 没有完成本 variant 评估时，`projected_status` 即使存在也不得当作通过。
  const effectiveStatus=evaluated?(projectedStatus||'unknown'):null;
  const passes=step6GateAllows(effectiveStatus);
  const text=effectiveStatus
    ?(ACCEPTABILITY_TEXT[effectiveStatus]||{label:String(effectiveStatus),note:''})
    :(ACCEPTABILITY_TEXT.unknown||{label:'不可判定',note:''});
  return {
    present:Boolean(raw),
    evaluated,
    evaluatedBasis:projection.evaluated_basis||null,
    projectedStatus,
    effectiveStatus,
    passes,
    label:evaluated?String(text.label):'不可判定（尚未评估 ⇒ 阻止）',
    statusText:String(text.label),
    note:evaluated
      ?String(text.note||'')
      :'本 variant 尚未做 P17 评估，因此保持不可判定/阻止（fail-closed）。',
    baselineStatus:projection.baseline_status??null,
    postPlanStatus:projection.post_plan_status??null,
    managedGapCount:projection.projected_managed_gap_count??null,
    unacceptableCount:projection.projected_unacceptable_count??null,
    unknownCount:projection.projected_unknown_count??null,
    comparison:projection.comparison||null,
    limitations:projection.limitations||[],
    disclosureLines:projection.disclosure_lines||[],
    appliedActionIds:projection.applied_action_ids||[],
    projectionSemantics:projection.projection_semantics||null,
    persistedAsUpstream:projection.persisted_as_upstream===true,
    writtenIntoExistingCns:projection.written_into_existing_cns===true,
    primaryThreatStatus:projection.primary_threat_status??null,
    supplementaryThreatStatus:projection.supplementary_threat_status??null,
    blockingReason:gate.blocking_reason||null,
    //: 后端自己的 note 原样保留（审计用），但**不覆盖**上面的 fail-closed 说明。
    backendNote:projection.note||null,
    //: 该结论属于**该 variant 的投影态**，不是现网事实 —— 由渲染层逐字写出。
    isProjection:true,
  };
}

//: 「该结论属于本 variant 的投影态，不是现网事实」——必须逐字出现在 variant 卡上。
export const VARIANT_P17_PROJECTION_NOTE='该结论属于本 variant 的投影态，不是现网事实。';

/** 每个 Plan Variant 卡片上的 P17 投影结论区块（本 variant 自己的结论）。 */
function variantP17Line(variant){
  const model=variantContinuousServiceProjection(variant);
  const badge=model.evaluated
    ?statusBadge(model.effectiveStatus||'unknown')
    :statusBadge('unknown');
  return '<div class="review-row" data-variant-p17="'+escapeHtml(String(variant?.variant_id||''))+'">'
    +'<span>本 variant 实施后的 P17 结论</span>'
    +'<span class="review-value">'+badge
    +'<small data-variant-p17-status="'+escapeHtml(String(model.effectiveStatus||'—'))+'">'
    +'本 variant 实施后的 P17 结论：'+escapeHtml(model.label)
    +'（projected_status='+escapeHtml(String(model.projectedStatus??'—'))
    +' · evaluated_for_this_variant='+escapeHtml(String(model.evaluated))+'）</small>'
    +'<small>'+escapeHtml(VARIANT_P17_PROJECTION_NOTE)+'</small>'
    +(model.evaluated
      ?'<small>managed gap '+escapeHtml(String(model.managedGapCount??'—'))
        +' · unacceptable '+escapeHtml(String(model.unacceptableCount??'—'))
        +' · unknown '+escapeHtml(String(model.unknownCount??'—'))+'</small>'
        +'<small>P17 门禁：'+escapeHtml(model.passes?'允许（需逐段披露 managed gap）':'阻止确认（fail-closed）')
        +'</small>'
      :'<small><b>本 variant 尚未做 P17 评估</b>：projected_status '
        +escapeHtml(String(model.projectedStatus??'缺失'))
        +'，因此**不可判定 / 阻止**，绝不显示为通过。</small>')
    +(model.blockingReason?'<small>'+escapeHtml(model.blockingReason)+'</small>':'')
    +'</span></div>';
}

/**
 * 候选方案卡：每个候选一个「选择此方案」按钮（secondary）。
 * 选择按钮不设 primary：候选可能有多个，设成 primary 会让同一分段出现多个主操作，
 * 反而模糊"主链只有一个推进点"的语义。来源标识（variant.source）是后端原值，
 * 只作弱化标识显示，不参与任何判断，也不做中文映射（避免手写第二份词表）。
 */
function variantCards(selectedVariantId,review){
  return (review.variants||[]).map(variant=>{
    const evaluation=variant.evaluation||{},gate=evaluation.confirmation_gate||{},actions=variant.selected_action_ids||[];
    const selected=variant.variant_id===selectedVariantId;
    return '<div class="review-block plan-variant '+(selected?'selected':'')+'"><b>'+escapeHtml(variant.name||variant.variant_id)+'</b>'
      +'<span>'+(selected?'当前已选候选':'候选方案')+' · '+actions.length+' 个动作 · 确认门禁：'+statusText(gate.status||'not_evaluated')+'</span>'
      +'<small>方案编号：'+escapeHtml(variant.variant_id||'')+(variant.source?' · 来源标识：'+escapeHtml(variant.source):'')+'</small>'
      +variantP17Line(variant)
      +'<button class="secondary selectPlanVariant" data-variant-id="'+escapeHtml(variant.variant_id||'')+'">'+(selected?'当前已选（重新选择）':'选择此方案')+'</button></div>';
  }).join('')||'<div class="empty">尚未初始化方案审查。</div>';
}

/**
 * Step6 强制披露中心：managed gap、工程假设、能力限制 limitations 原文、
 * FC30 profile、通信阈值、route protection 参数 —— 全部集中显示。
 *
 * 只转印后端字段；任何一个来源缺失都如实写"（后端未提供）"，绝不冒充。
 */
export function mandatoryDisclosureModel(flow){
  const model=continuousServiceModel(flow);
  const gate=step6GateModel(flow);
  const fc30=model.fc30||{};
  const limits=limitationRows(model);
  const parameters=parameterRows(model);
  const byField=Object.fromEntries(parameters.map(item=>[item.field,item]));
  return {
    gate,
    managedGapCount:gate.managedGapCount,
    managedGapDisclosureLines:model.result.disclosure_lines||[],
    //: 工程假设：内置工程基线 / 尚无依据（evidence_required）的参数逐项列出。
    engineeredAssumptions:parameters
      .filter(item=>item.authority==='builtin_engineering_assumption'
        ||item.authority==='evidence_required'
        ||item.authority==='policy_override')
      .map(item=>({
        field:item.field,label:item.label,value:item.value,unit:item.unit,
        authority:item.authority,authorityText:item.authorityText,
        sourceTypeText:item.sourceTypeText,reason:item.reason,
        evidenceRequired:item.evidenceRequired,
      })),
    //: 能力限制（Radar 非合作监视）原文：**不得**被渲染成 error / 系统错误。
    limitations:limits,
    //: FC30 profile 与通信阈值（设备事实 / 规划阈值 / 冗余退化阈值三件分开）。
    fc30:{
      selectedAircraftId:fc30.selected_aircraft_id||null,
      isSelected:fc30.is_selected===true,
      disclosure:fc30.disclosure||null,
      deviceFailsafeFact:fc30.device_failsafe_fact||null,
      projectPlanningThreshold:fc30.project_planning_threshold||null,
      redundancyDegradationThreshold:fc30.redundancy_degradation_threshold||null,
      thresholdsAreSeparate:fc30.thresholds_are_separate===true,
      thresholdMergeForbidden:fc30.threshold_merge_forbidden===true,
    },
    //: route protection 参数（保护走廊四分量）。
    routeProtection:['D_separation_m','D_maneuver_m','D_uncertainty_m']
      .map(field=>byField[field]).filter(Boolean),
    threatLayers:threatLayerRows(model),
    allowedStatuses:STEP6_ALLOWED_ACCEPTABILITY,
  };
}

/** 强制披露中心的渲染（与 Step5 面板同一份后端字段，绝不出现第二套结论）。 */
function mandatoryDisclosureBlock(flow){
  const model=mandatoryDisclosureModel(flow);
  const fc30=model.fc30||{};
  const fact=fc30.deviceFailsafeFact||{};
  const threshold=fc30.projectPlanningThreshold||{};
  const redundancy=fc30.redundancyDegradationThreshold||{};
  const gapLines=model.managedGapDisclosureLines||[];
  return reviewBlock('强制披露（managed gap / 工程假设 / 能力限制 / FC30 与阈值 / 保护参数）',
    // ---- managed gap -------------------------------------------------------
    '<h3>managed gap（有管理的缺口）</h3>'
    +'<div class="parameter-note">可接受 '+escapeHtml(String(model.managedGapCount))
    +' 段；缺口真实存在，只因连续时长在工程阈值内被接受：<b>绝不是全覆盖</b>。</div>'
    +(gapLines.length
      ?'<div class="scroll-list cns-input-list">'+gapLines.map(line=>
        '<div class="coverage-card"><small>'+escapeHtml(String(line))+'</small></div>').join('')
        +'</div>'
      :'<div class="wb-empty">当前没有 managed gap 披露行。</div>')
    // ---- 工程假设 ----------------------------------------------------------
    +'<h3>工程假设（engineering_assumption / 内置工程基线 / 尚无依据）</h3>'
    +(model.engineeredAssumptions.length
      ?'<div class="scroll-list cns-input-list">'+model.engineeredAssumptions.map(item=>
        '<div class="coverage-card" data-engineered-assumption="'+escapeHtml(item.field)+'">'
        +'<b>'+escapeHtml(item.label||item.field)+'（'+escapeHtml(item.field)+'）</b>'
        +'<span>取值 '+(item.value!==null&&item.value!==undefined&&item.value!==''
          ?escapeHtml(String(item.value)):'—')
        +(item.unit?' '+escapeHtml(item.unit):'')
        +' · 来源：'+escapeHtml(item.authorityText||'—')
        +' · '+escapeHtml(item.sourceTypeText||'—')+'</span>'
        +(item.evidenceRequired?'<span><b>尚无依据（必须显式登记）</b>：判定保持 '
          +'evidence_required / unknown（fail-closed）</span>':'')
        +(item.reason?'<small>'+escapeHtml(String(item.reason))+'</small>':'')
        +'</div>').join('')+'</div>'
      :'<div class="wb-empty">没有使用内置基线或工程假设的参数。</div>')
    // ---- 能力限制（limitations）原文 ---------------------------------------
    +'<h3>能力限制 limitations（Radar 非合作监视，原文披露）</h3>'
    +(model.limitations.length
      ?model.limitations.map(item=>
        '<div class="coverage-card" data-capability-limitation="true" '
        +'style="border-left:3px solid var(--warn);background:var(--warn-bg)">'
        +'<b><span class="flow-badge flow-warning">能力限制（黄色 / 橙色）</span>'
        +escapeHtml(String(item.capability||item.limitationId||'—'))+'</b>'
        +'<span>'+escapeHtml(String(item.disclosure||'（后端未提供 disclosure）'))+'</span>'
        +'<span><b>该限制不改变主要威胁（合作无人机 / RID）的判定；'
        +'不得表述为「监视已完全满足」。</b></span>'
        +'<small>limitation_id='+escapeHtml(String(item.limitationId||'—'))
        +' · status='+escapeHtml(String(item.status||'—'))
        +' · blocking_primary_threat='+escapeHtml(String(item.blockingPrimaryThreat))
        +' · must_disclose_in_report='+escapeHtml(String(item.mustDiscloseInReport))+'</small>'
        +'</div>').join('')
      :'<div class="wb-empty">后端未登记任何能力限制（limitation）。</div>')
    // ---- FC30 profile 与通信阈值 -------------------------------------------
    +'<h3>FC30 profile 与通信阈值（设备事实 ≠ 规划阈值；冗余退化是独立阈值）</h3>'
    +'<div class="coverage-card" data-step6-threshold="device_failsafe_fact">'
    +'<b>设备事实：失联 &gt; '+escapeHtml(String(fact.value_s??'—'))+' s 可触发 Failsafe RTH</b>'
    +'<span>is_planning_threshold='+escapeHtml(String(fact.is_planning_threshold))
    +' · kind='+escapeHtml(String(fact.kind||'—'))+'</span>'
    +'<small>'+escapeHtml(String(fact.statement||'（后端未提供 statement）'))+'</small></div>'
    +'<div class="coverage-card" data-step6-threshold="project_planning_threshold"'
    +(threshold.evidence_required===true?' data-evidence-required="true"':'')+'>'
    +'<b>本项目规划阈值：用户填写 '
    +(threshold.value_s===null||threshold.value_s===undefined||threshold.value_s===''
      ?'—（尚未登记）':escapeHtml(String(threshold.value_s))+' s')+'</b>'
    +'<span>evidence_required='+escapeHtml(String(threshold.evidence_required))
    +' · authority='+escapeHtml(String(threshold.authority||'—'))+'</span>'
    +(threshold.evidence_required===true
      ?'<span><b>尚未登记 ⇒ 判定保持 evidence_required / unknown（fail-closed）。</b>'
        +'设备 failsafe 门限不会被自动采用。</span>':'')
    +'<small>'+escapeHtml(String(threshold.statement||'（后端未提供 statement）'))+'</small></div>'
    +'<div class="coverage-card" data-step6-threshold="redundancy_degradation_threshold">'
    +'<b>冗余退化最大允许时间 '+escapeHtml(String(redundancy.value_s??'—'))+' s（独立阈值）</b>'
    +'<span>separate_from_full_outage='+escapeHtml(String(redundancy.separate_from_full_outage))
    +' · thresholds_are_separate='+escapeHtml(String(fc30.thresholdsAreSeparate))
    +' · threshold_merge_forbidden='+escapeHtml(String(fc30.thresholdMergeForbidden))+'</span>'
    +'<small>'+escapeHtml(String(redundancy.statement||'（后端未提供 statement）'))+'</small></div>'
    // ---- route protection 参数 --------------------------------------------
    +'<h3>route protection 参数（保护走廊四分量）</h3>'
    +(model.routeProtection.length
      ?'<div class="scroll-list cns-input-list">'+model.routeProtection.map(item=>
        '<div class="coverage-card" data-route-protection-parameter="'+escapeHtml(item.field)+'">'
        +'<b>'+escapeHtml(item.label||item.field)+'（'+escapeHtml(item.field)+'）</b>'
        +'<span>取值 '+(item.value!==null&&item.value!==undefined&&item.value!==''
          ?escapeHtml(String(item.value)):'—')
        +(item.unit?' '+escapeHtml(item.unit):'')
        +' · 来源：'+escapeHtml(item.authorityText||'—')+'</span>'
        +'</div>').join('')+'</div>'
      :'<div class="wb-empty">后端未下发 route protection 参数。</div>'),
    '本区块只转印后端字段：门禁允许值 '+model.allowedStatuses.join(' / ')
      +'；能力限制不是系统错误，managed gap 绝不是全覆盖。');
}

function comparisonPanel(flow,review,selected){
  return reviewBlock(SIX_SECTIONS.goal,
      nextStepNote('本分段负责主链的前两步：先初始化方案审查得到候选方案，再由人工选择一个候选。选择只切换当前候选，不等于确认。'))
    +reviewBlock(SIX_SECTIONS.action,
      '<div class="variant-grid">'+variantCards(review.selected_variant_id,review)+'</div>'
      +'<div class="button-row"><button class="primary" id="initializePlanReview">初始化方案审查</button><button class="secondary" id="evaluatePlanVariant" '+(!selected?'disabled':'')+'>重新评价所选方案</button></div>',
      '选择只改变当前候选方案，不修改任何设施；系统不排序、不评分、不自动选择。P17 结论按 variant 各自的投影态显示，绝不把权威结论冒充成本 variant 的结论。')
    +reviewBlock(SIX_SECTIONS.blockers,blockerList(reviewBlockers(flow),
      '当前没有阻塞项：可以继续比较候选方案。'))
    +reviewBlock(SIX_SECTIONS.result,comparisonCards(selected)
      +'<small>满足/缺口/未知均为体素计数；缺口长度为保守纵向投影，不是运行中断时长。</small>'
      +comparisonMatrixTable(selected)
      +reviewBlock('显式费用',escapeHtml(formatCosts(selected?.evaluation?.action_summary)),'按单位分组，不做跨单位合计；未提供显式费用时使用动作数量代理口径。')
      +mandatoryDisclosureBlock(flow))
    +reviewBlock(SIX_SECTIONS.next,
      nextStepNote('下一步：选定候选后到「确认与应用」冻结快照并确认；需要人工增删动作时先到「方案编辑」克隆候选。'));
}
function editPanel(selected){
  return reviewBlock(SIX_SECTIONS.goal,
      nextStepNote('方案编辑只追加人工决策候选：在所选方案的动作集合上增删动作并命名；系统不会自动选中新候选，也不会改动任何设施。'))
    +reviewBlock(SIX_SECTIONS.input,
      kvRow('编辑基线',selected?escapeHtml(selected.name||selected.variant_id)+'（方案编号：'+escapeHtml(selected.variant_id)+'）':'尚未选择方案'))
    +reviewBlock(SIX_SECTIONS.action,
      '<label>纳入动作ID（逗号分隔）<input id="variantInclude" placeholder="candidate_site:S1:C1"></label>'
      +'<label>排除动作ID（逗号分隔）<input id="variantExclude"></label>'
      +'<label>方案名称<input id="variantName" value="用户方案"></label>'
      +'<button class="primary full" id="createPlanVariant" '+(!selected?'disabled':'')+'>克隆并创建方案</button>',
      '创建只追加人工编辑候选；不会自动选中，也不会改动设施。')
    +reviewBlock(SIX_SECTIONS.next,
      nextStepNote('下一步：创建候选后回到「方案比较」选择它，再到「确认与应用」冻结快照。'));
}

/**
 * 确认与应用：三步在视觉与容器上彼此分开，绝不并排成等价主按钮。
 *  - 第 1 步「选择方案」在「方案比较」分段；
 *  - 第 2 步「确认方案」只冻结快照（secondary，独立容器）；
 *  - 第 3 步「应用确认方案」才做事务提交（本分段唯一的 primary）。
 * 门禁完全沿用后端：前端只如实显示 gate 状态与中文原因，一条都不放宽。
 */
function confirmPanel(flow,confirmed,selected,summary){
  const evaluation=selected?.evaluation||{},gate=evaluation.confirmation_gate||{},gateStatus=gate.status||'not_evaluated';
  const acknowledged=gate.requires_confirm_without_objectives_acknowledgement===true;
  // canonical step 标记只由正式 CNS 链决定：旧版兼容结果既不参与判定，也不能解锁确认。
  const canonicalReady=flow.steps?.['6']===true;
  const canConfirm=canonicalReady&&Boolean(selected)&&(gateStatus==='ready_for_confirmation'||acknowledged);
  const canApply=Boolean(summary.confirmedStatus==='confirmed'&&confirmed.current_applicability==='current');
  const applyId=String(confirmed.plan_id||'');
  return reviewBlock(SIX_SECTIONS.goal,
      nextStepNote('主链：'+REVIEW_MAIN_CHAIN+'。本分段承担第 2 步与第 3 步：确认只冻结所选方案的快照，应用才做事务提交，两者绝不互相代替。'))
    +reviewBlock(SIX_SECTIONS.input,[
      kvRow('当前所选方案',selected?escapeHtml(selected.name||selected.variant_id)+'（方案编号：'+escapeHtml(selected.variant_id)+'）':'尚未选择'),
      kvRow('正式 CNS 链',statusBadge(canonicalReady?'ready':'not_available',readinessText(canonicalReady?'ready':'not_available')),
        canonicalReady?'三维几何覆盖、CNS 服务能力、服务走廊、能力缺口与 CNS 设施规划均已形成当前结果':'仅正式 CNS 主链结果可解锁方案确认'),
      kvRow('确认门禁状态',statusBadge(gateStatus),
        acknowledged?'未配置规划目标：勾选知情确认并填写理由后才能确认':'门禁为「'+statusText('ready_for_confirmation')+'」时才可直接确认')
    ].join(''))
    +reviewBlock(SIX_SECTIONS.blockers,blockerList(reviewBlockers(flow),
      '当前没有阻塞项：主链可以按「选择 → 确认 → 应用 → 生成报告」继续推进。'))
    +reviewBlock('主操作 · 第 2 步：确认方案（只冻结快照，不修改设施）',
      kvRow('已确认方案',statusBadge(summary.confirmedStatus),confirmed.plan_id?'方案编号：'+escapeHtml(String(confirmed.plan_id)):'')
      +'<label><input type="checkbox" id="confirmWithoutObjectives" '+(acknowledged?'':'disabled')+'> 未配置规划目标时仍确认（记录明确知情确认）</label>'
      +'<label>确认理由<input id="planDecisionReason" placeholder="请记录人工确认依据"></label>'
      +'<button class="secondary full" id="confirmPlan" data-gate="'+escapeHtml(gateStatus)+'" '+(canConfirm?'':'disabled')+'>确认所选方案</button>',
      acknowledged?'勾选后必须填写确认理由，后端会把这次知情确认记入该方案的确认档案。':'确认只冻结该方案的快照，不写入任何设施，也不等于应用。')
    +reviewBlock('主操作 · 第 3 步：应用确认方案（事务提交）',
      kvRow('可应用条件',statusBadge(canApply?'ready':'not_available',readinessText(canApply?'ready':'not_available')),
        canApply?'已确认且当前有效，可以提交':'需要先确认方案，且 CNS 能力需求、运行航路、设施、候选与设备基线未发生变化')
      +'<button class="primary full" id="applyPlan" data-apply-gate="'+(canApply?'ready':'blocked')+'" '+(canApply?'':'disabled')+'>应用已确认方案</button>',
      '应用会重跑三维覆盖与 CNS 能力缺口评估链，并重算 CNS 服务走廊与能力缺口；验证不一致、出现确认回归或关键证据不足时整笔回滚。'+(applyId?' 当前方案编号：'+escapeHtml(applyId):''))
    +reviewBlock(SIX_SECTIONS.next,
      nextStepNote('下一步：应用成功后到「报告与交付」生成正式规划报告；未应用前也可以先预览草稿，草稿不写入项目。'));
}

// ---- Route Safety Evidence V2 -------------------------------------------------
// 四个 evidence domain 分开显示。全部字段来自后端 route_safety_evidence_v2，
// 前端不做任何重新推导，也不合成 overall safety score / 排名。
export const ROUTE_SAFETY_EVIDENCE_DOMAINS=[
  ['geometry_obstacle','几何与障碍物'],
  ['ground_exposure','地面暴露'],
  ['regulatory','法规约束'],
  ['cns_operational_support','CNS 运行支持']
];
export const ROUTE_SAFETY_EVIDENCE_STATUS_NOTE=
  '该状态表示证据完整性/明确硬约束结果，不是自动安全认证或安全评分。';
export const ROUTE_SAFETY_EVIDENCE_CNS_NOTE=
  'CNS 已确认缺口在证据里记为运行支持缺口：它不代表航路不安全，也不改变几何与障碍物验证结论。';

/** 后端 route_safety_evidence_v2 / readiness 的只读投影。 */
export function routeSafetyEvidenceModel(flow){
  const collection=flow?.route_safety_evidence_v2||{};
  const readiness=flow?.route_safety_evidence_v2_readiness||{};
  const items=Array.isArray(collection.items)?collection.items:[];
  const current=items.find(item=>item&&item.current_applicability==='current')||null;
  const shown=current||items[items.length-1]||null;
  const domains=(shown&&shown.domains)||{};
  const lineage=(shown&&shown.lineage)||{};
  return {
    status:text(shown&&shown.status,'not_ready'),
    statusReason:text(shown&&shown.status_reason),
    applicability:text(shown&&shown.current_applicability,'not_evaluated'),
    assessmentId:text(shown&&shown.assessment_id),
    routeId:text(shown&&shown.route_id),
    adoptionId:text(shown&&shown.adoption_id),
    createdAt:text(shown&&shown.created_at),
    isCurrent:Boolean(current),
    count:items.length,
    readinessStatus:text(readiness.status,'not_ready'),
    target:(readiness.target)||{},
    lineage:{
      complete:Boolean(lineage.complete),
      status:text(lineage.status,'not_resolved'),
      missing:Array.isArray(lineage.missing_links)?lineage.missing_links:[],
      stale:Array.isArray(lineage.stale_links)?lineage.stale_links:[],
    },
    summary:(shown&&shown.evidence_summary)||{},
    limitations:Array.isArray(shown&&shown.limitations)?shown.limitations:[],
    fingerprints:(shown&&shown.fingerprints)||{},
    provenance:(shown&&shown.provenance)||{},
    domains:ROUTE_SAFETY_EVIDENCE_DOMAINS.map(([id,label])=>{
      const domain=domains[id]||{};
      return {
        id,label,
        status:text(domain.status,'not_ready'),
        reason:text(domain.status_reason),
        hardFailure:domain.hard_constraint_failure===true,
        metrics:domain.metrics||{},
        evidence:domain.evidence||{},
        sources:Array.isArray(domain.sources)?domain.sources:[],
        limitations:Array.isArray(domain.limitations)?domain.limitations:[],
      };
    }),
  };
}

function text(value,fallback=''){return value===null||value===undefined||value===''?fallback:String(value);}
function safeNumber(value,digits=3){
  return Number.isFinite(value)?Number(value).toFixed(digits):'—';
}
function metricText(value){return value===null||value===undefined?'—':(Number.isFinite(value)?safeNumber(value):String(value));}

/**
 * 单个 domain 的紧凑指标（只转印后端关键字段）。
 * 状态取值一律经 presentation.js 的词表函数取词；未登记的 raw 值按契约原样返回，
 * 既不猜测、也不伪装成"通过"。
 */
function safetyDomainMetrics(model){
  const metrics=model.metrics||{};
  if(model.id==='geometry_obstacle'){
    return [['验证状态',text(model.evidence&&model.evidence.validation_status)||'—'],
      ['地形检查',statusText(metrics.terrain_status)+' · 最小余量 '+metricText(metrics.terrain_minimum_margin_m)+' m'],
      ['建筑检查',statusText(metrics.building_status)+' · 最小余量 '+metricText(metrics.building_minimum_margin_m)+' m'],
      ['固定巡航高度',metricText(metrics.fixed_cruise_altitude_m)+' m EGM2008'],
      ['未解析区间',String(metrics.unresolved_interval_count??0)
        +' · 不通过区间 '+String(metrics.failed_interval_count??0)],
      ['计算资源受限证据',metrics.resource_limited?'是（计算资源限制，不是安全结论）':'否']];
  }
  if(model.id==='ground_exposure'){
    const theta=(model.evidence&&model.evidence.theta_star_planning_objective)||{};
    const density=(model.evidence&&model.evidence.route_risk_density)||{};
    return [['Theta* 人口×遮蔽暴露指数',metricText(metrics.population_shelter_risk_exposure_index_m)+' 指数·m'],
      ['航路风险密度',metricText(metrics.route_risk_density)+' / 阈值 '+metricText(metrics.route_risk_density_threshold)
        +' · '+statusText(metrics.route_risk_density_status)],
      ['规划目标权重',[['risk','风险'],['turn','转向'],['distance','距离']].map(([key,label])=>label+' '+metricText((theta.weights||{})[key])).join(' · ')],
      ['地面风险剖面 均值/最大值',metricText(metrics.profile_ground_mean_index)+' / '+metricText(metrics.profile_ground_max_index)],
      ['剖面暴露指数 / 未解析长度',metricText(metrics.profile_ground_exposure_index_m)+' 指数·m / '
        +metricText(metrics.profile_ground_unresolved_length_m)+' m'],
      ['剖面阈值 / 状态',metricText(metrics.profile_thresholds&&metrics.profile_thresholds.medium_min)
        +' / '+metricText(metrics.profile_thresholds&&metrics.profile_thresholds.high_min)+' · '
        +readinessText(metrics.profile_thresholds&&metrics.profile_thresholds.status)],
      ['密度角色',density.objective_term?'规划目标项（异常：后端绝不允许）':'评价约束，不是规划目标项']];
  }
  if(model.id==='regulatory'){
    return [['数据集状态',readinessText(model.evidence&&model.evidence.dataset_status)],
      ['是否已配置',metrics.configured?'是':'否（未配置不等于检查通过）'],
      ['是否已评价',metrics.evaluated?'是':'否（未评价不等于检查通过）'],
      ['阻挡约束',String((metrics.blocked_constraints||[]).length)],
      ['未解析约束',String((metrics.unresolved_constraints||[]).length)],
      ['航段数',String(metrics.segment_count??0)]];
  }
  const subsystems=Array.isArray(metrics.subsystems)?metrics.subsystems:[];
  const rows=subsystems.map(item=>[item.subsystem+' 子系统支持结论',
    text(item.operational_support_verdict,'unknown')
    +' · 覆盖 '+statusText(item.coverage&&item.coverage.status)
    +' · 能力 '+statusText(item.capability&&item.capability.status)
    +' · 缺口长度 '+metricText(item.confirmed_gap&&item.confirmed_gap.gap_length_m)+' m']);
  return rows.concat([
    ['运行支持缺口',metrics.operational_support_deficit?'是（CNS 已确认缺口）':'否'],
    ['确认缺口子系统',(metrics.confirmed_gap_subsystems||[]).join('/')||'—'],
    ['证据不足子系统',(metrics.unknown_subsystems||[]).join('/')||'—'],
  ]);
}

function safetyDomainCard(model){
  const metrics=safetyDomainMetrics(model).map(([label,value])=>kvRow(label,escapeHtml(value))).join('');
  const sources=model.sources.length
    ?model.sources.map(source=>kvRow('证据来源',escapeHtml(text(source.role,'—'))
      +(source.algorithm_id?' · '+escapeHtml(String(source.algorithm_id)):'')
      +(source.fingerprint?' · '+escapeHtml(String(source.fingerprint)):''))).join('')
    :'';
  const limitations=model.limitations.length
    ?'<small>'+escapeHtml(model.limitations[0])+'</small>'
    :'<small>—</small>';
  const boundary=model.id==='cns_operational_support'
    ?'<small class="parameter-note">'+escapeHtml(ROUTE_SAFETY_EVIDENCE_CNS_NOTE)+'</small>'
    :'';
  return '<div class="review-block safety-domain-card" data-safety-domain="'+escapeHtml(model.id)+'">'
    +'<div class="comparison-head"><b>'+escapeHtml(model.label)+'</b>'
    +statusBadge(model.status,readinessText(model.status))+(model.hardFailure?statusBadge('failed'):'')+'</div>'
    +(model.reason?'<small>'+escapeHtml(model.reason)+'</small>':'')
    +metrics
    +'<div class="review-sub"><b>证据来源</b></div>'+sources
    +limitations
    +boundary
    +'</div>';
}

/**
 * Route Safety Evidence 紧凑卡片：overall evidence status + 四个 domain card，
 * 审计指纹 / 来源链路折叠在"高级"折叠区内（生产主界面不出现开发术语）。
 */
function routeSafetyEvidencePanel(flow){
  const model=routeSafetyEvidenceModel(flow);
  const ready=model.readinessStatus==='ready';
  const overall=[
    kvRow('证据总体状态',statusBadge(model.status),model.statusReason||''),
    kvRow('当前适用性',statusBadge(model.applicability),
      model.isCurrent?'这是当前有效的评估':'不是当前评估，只作历史证据'),
    kvRow('评估对象',model.routeId?escapeHtml(model.routeId)+' · 采纳记录 '+escapeHtml(model.adoptionId||'—'):'尚未评估'),
    kvRow('来源链路',model.lineage.complete?'完整':'不完整：'
      +escapeHtml([...model.lineage.missing,...model.lineage.stale].join('、')||'—')),
    kvRow('就绪状态',statusBadge(model.readinessStatus,readinessText(model.readinessStatus)),
      ready?'':'需要先存在当前已发布的正式运行航路采纳记录'),
  ].join('');
  const cards=model.domains.map(safetyDomainCard).join('');
  const fingerprintRows=[
    ['评估指纹',model.fingerprints.assessment_fingerprint],
    ['运行航路 / 采纳记录指纹',model.fingerprints.operational_route_adoption_fingerprint],
    ['验证指纹',model.fingerprints.validation_fingerprint],
    ['候选指纹',model.fingerprints.candidate_fingerprint],
    ['航路风险剖面指纹',model.fingerprints.route_risk_profile_fingerprint],
    ['法规数据集指纹',model.fingerprints.regulatory_dataset_fingerprint],
    ['三维覆盖指纹',model.fingerprints.coverage_3d_fingerprint],
    ['CNS 服务能力指纹',model.fingerprints.cns_service_capability_fingerprint],
    ['规划缺口指纹',model.fingerprints.cns_gap_v2_fingerprint],
    ['CNS 服务走廊指纹',model.fingerprints.cns_corridor_fingerprint],
    ['评价器版本',model.fingerprints.evaluator_version],
  ].map(([label,value])=>kvRow(label,escapeHtml(text(value,'—')))).join('');
  const limitationRows=model.limitations.map(item=>'<small>'+escapeHtml(item)+'</small>').join('');
  return reviewBlock('航路安全证据',
    '<p class="parameter-note">'+escapeHtml(ROUTE_SAFETY_EVIDENCE_STATUS_NOTE)+'</p>'
    +'<div class="scroll-list route-list">'+overall+'</div>'
    +'<div class="safety-domain-grid">'+cards+'</div>'
    +wbDisclosure('高级：审计指纹与来源链路',
      '<div class="scroll-list route-list">'+fingerprintRows+'</div>'
      +'<div class="parameter-note">'+escapeHtml(JSON.stringify(model.provenance))+'</div>')
    +wbDisclosure('局限说明',limitationRows||'<small>—</small>')
    +'<div class="button-row"><button class="secondary" id="evaluateRouteSafetyEvidenceV2">评价航路安全证据</button></div>',
    '四个证据域分开报告，前端绝不合成总体安全评分、排名或自动安全等级。');
}

// ---- 状态总览 ---------------------------------------------------------------
function statusOverview(flow){
  const statuses=flow.result_statuses||{},risks=flow.review?.risks||{};
  const groups=STATUS_GROUPS.map(([title,rows])=>{
    // 生命 / 财产风险只存在于 flow.review.risks（不在 result_statuses 里），按需追加。
    const extra=title==='风险'
      ? EXTRA_RISK_LABELS.filter(([key])=>risks[key]&&!rows.some(([known])=>known===key)).map(([key,label])=>[key,label])
      : [];
    const all=[...rows,...extra];
    return wbBlock(title,reviewBlock(title,all.map(([key,label])=>{
      // 技术风险以 risks.technical 为准，缺失时才回退到 result_statuses。
      const status=key==='technical_risk'?(risks.technical?.status||statusOf(statuses,'technical_risk')):statusOf(statuses,key);
      return badgeRow(label,status);
    }).join('')));
  }).join('');
  const overall=reviewBlock('总体状态',
    kvRow('总体状态',statusBadge(flow.review?.overall_status||'not_calculated'))
    +kvRow('总体通过',flow.review?.overall_pass?'是':'否'),
    '只映射后端现有状态，不在此重新计算任何结论。');
  return groups+overall;
}

// ---- 报告与交付 -------------------------------------------------------------
function reportPanel(flow){
  const reports=flow.cns_planning_reports||{},active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id);
  const confirmed=flow.confirmed_cns_plan||{},hasPlan=['confirmed','applied'].includes(confirmed.status),hasReport=Boolean(active);
  const stale=active?.current_applicability==='stale_current_project';
  const planState=confirmed.status||'not_confirmed';
  const planId=confirmed.plan_id?escapeHtml(String(confirmed.plan_id)):'—';
  const variantId=confirmed.variant_id?escapeHtml(String(confirmed.variant_id)):'—';
  // 正式报告只能由已确认（或已应用）的方案生成：这里只如实转印后端门禁，
  // 既不因为存在兼容结果而放宽，也不额外收紧。
  const gateReason=hasPlan
    ?'后端报告门禁已满足（方案状态：'+statusText(planState)+'）'
    :'后端报告门禁要求方案状态为「'+statusText('confirmed')+'」或「'+statusText('applied')+'」；当前方案状态为「'+statusText(planState)+'」';
  const missingReport=hasPlan
    ?emptyReasonText('no_result','方案已确认，但尚未生成正式报告：请执行「生成正式报告」。')
    :emptyReasonText('no_result','请先在方案审查中确认一个规划方案；当前只能预览草稿，草稿不写入项目。');
  return reviewBlock('CNS规划方案报告',[
    kvRow('方案状态',statusBadge(planState),gateReason),
    kvRow('方案来源',hasPlan?'方案编号：'+planId+' · 候选编号：'+variantId:'尚未确认任何方案，因此没有正式报告来源'),
    kvRow('报告状态',statusBadge(active?.current_applicability||reports.status||'not_calculated'),hasReport?'':'尚无正式报告'),
    kvRow('报告编号',active?escapeHtml(active.report_id):'—'),
    kvRow('报告对应方案',active&&active.source_plan_id?escapeHtml(String(active.source_plan_id)):'—'),
    kvRow('生成时间',active?escapeHtml(active.generated_at||'—'):'—')
  ].join(''))
    +reviewBlock(SIX_SECTIONS.blockers,blockerList(reportBlockers(active,hasPlan),
      '当前没有阻塞项：报告可以按当前方案状态生成或下载。'))
    +reviewBlock(SIX_SECTIONS.action,
      (stale?'<p class="inline-error">该报告对应旧项目状态，可继续下载，但不代表当前项目。系统不会自动覆盖或删除这份旧报告；请重新确认并应用方案后再生成新报告。</p>':'')
      +(!hasReport?'<p class="empty">'+escapeHtml(missingReport)+'</p>':'')
      +'<div class="button-row"><button class="secondary" id="previewPlanningReport">预览报告</button><button class="primary" id="generatePlanningReport" data-report-gate="'+(hasPlan?'ready':'blocked')+'" '+(!hasPlan?'disabled':'')+'>生成正式报告</button></div>'
      +'<div class="button-row"><button class="secondary" id="downloadReportHtml" '+(!hasReport?'disabled':'')+'>下载HTML</button><button class="secondary" id="downloadReportPdf" '+(!hasReport?'disabled':'')+'>下载PDF</button></div>'
      +'<button class="secondary full" id="downloadReportPackage" '+(!hasReport?'disabled':'')+'>下载规划数据包</button>',
      '预览报告只生成草稿：草稿不写入项目、不产生报告记录，也不进入导出。PDF 使用与 HTML 完全相同的冻结报告数据与页面；如提示 PDF 能力缺失，请执行 python -m playwright install chromium 后重试。')
    +reviewBlock(SIX_SECTIONS.next,
      nextStepNote('报告对应旧项目状态时系统不会自动覆盖或删除旧报告：请重新确认并应用方案，再生成新报告。'));
}
function deliveryPanel(flow){
  const reports=flow.cns_planning_reports||{},active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id);
  const stale=active?.current_applicability==='stale_current_project';
  return reviewBlock('导出与保存',
    (stale?'<p class="inline-error">当前报告对应旧项目状态：仍可下载，系统不会自动覆盖或删除旧报告。</p>':'')
    +'<div class="button-row export-row"><a class="secondary button-link" download="project.json" href="/api/export/project">项目JSON</a><a class="secondary button-link" download="routes.geojson" href="/api/export/routes">航路GeoJSON</a></div>'
    +'<button class="secondary full" id="saveAll">保存当前项目</button>',
    '导出只读取当前项目状态；报告与项目状态各自独立，不会互相覆盖。预览草稿不写入项目，也不进入导出。');
}

// ---- 专题成果图（Presentation / Cartographic Export，只读消费 canonical 状态） -----
/**
 * 专题成果图入口。
 *
 * 语义边界（与后端 map_figures 模块一致）：
 *  - 只消费**权威运行航路**（flow.operational_routes）与已配置真实 GIS 数据源；
 *  - 不重算任何业务结论，也不回写航路 / CNS 规划状态；
 *  - 缺数据的图层会被省略并在服务端 FigureSpec 中登记原因，这里如实显示；
 *  - **绝不自动生成**：只有用户点击「预览」/「生成 PNG」才会制图。
 *
 * 状态来源（code review 修复项）
 *  - 图件记录来自 `GET /api/map-figures/state` 的独立只读投影
 *    （`workflow/map_figure_state.js`），**不再**依赖 `flow.map_figures`：
 *    图件索引不进入 ProjectState，所以 flow 里永远看不到新图件；
 *  - 首次进入读取一次，整页 refresh 后重新读取，导出成功后重新读取；
 *  - 下载 PNG / 规格 JSON 一律按 `mapFigureState.active_figure_id`。
 */
const MAP_FIGURE_TEMPLATES=[
  ['route_overview_v1','航路周边状况图',true],
  ['route_detail_v1','航路细节放大图',false],
  ['communication_layout_v1','通信设施布设图',true],
  ['navigation_layout_v1','导航完整性监测点布设图',true],
  ['surveillance_layout_v1','监视设施布设图',true],
  ['cns_combined_v1','CNS 综合布设图',true]
];

/** 监视布设图的两个 variant：RID 合作监视与 Radar 非合作监视**绝不混画**。 */
const MAP_FIGURE_SURVEILLANCE_TEMPLATE='surveillance_layout_v1';
const MAP_FIGURE_SURVEILLANCE_PARAMETER='surveillance_service';
const MAP_FIGURE_SURVEILLANCE_VARIANTS=[
  ['rid_cooperative','RID 合作监视（2 km 实线 / 5 km 虚线）'],
  ['radar_noncooperative','Radar 非合作监视（能力限制表达）']
];

/**
 * 模板清单：**以后端 catalog 为准**，catalog 尚未加载时退回内置表。
 *
 * 为什么不能只保留内置表：模板可用性是后端事实（本轮 communication / navigation /
 * surveillance / combined 由 planned 变为 available）。前端硬编码一份就会与后端漂移，
 * 于是用户看到的可选项与实际能生成的图不一致。
 */
function mapFigureTemplateList(model){
  const declared=Array.isArray(model?.catalog?.templates)?model.catalog.templates:[];
  const available=Array.isArray(model?.catalog?.available_template_ids)
    ?model.catalog.available_template_ids:[];
  if(!declared.length)return MAP_FIGURE_TEMPLATES;
  return declared.map(item=>{
    const id=String(item?.template_id||'');
    return [id,String(item?.display_name||id),available.includes(id)];
  }).filter(item=>item[0]);
}

/** variant 取值同样以后端 catalog 为准（非法值由后端明确拒绝，前端不猜）。 */
function mapFigureSurveillanceVariants(model){
  const declared=model?.catalog?.surveillance_service_values;
  if(!Array.isArray(declared)||!declared.length)return MAP_FIGURE_SURVEILLANCE_VARIANTS;
  const labels=new Map(MAP_FIGURE_SURVEILLANCE_VARIANTS.map(item=>[item[0],item[1]]));
  return declared.map(value=>[String(value),labels.get(String(value))||String(value)]);
}

/** 监视布设图的 variant 选择器；其它模板不渲染（因此不会误传参数）。 */
function mapFigureVariantOptions(model,selectedTemplateId,selectedValue){
  if(selectedTemplateId!==MAP_FIGURE_SURVEILLANCE_TEMPLATE)return '';
  const variants=mapFigureSurveillanceVariants(model);
  const fallback=variants[0]?.[0]||'';
  const selected=variants.some(item=>item[0]===selectedValue)?selectedValue:fallback;
  return '<label>监视能力<select id="mapFigureSurveillanceService">'
    +variants.map(([value,label])=>'<option value="'+escapeHtml(value)+'"'
      +(value===selected?' selected':'')+'>'+escapeHtml(label)+'</option>').join('')
    +'</select></label>';
}

/**
 * 模板参数（只构造**登记过的**参数）。
 *
 * 监视布设图必须显式给出 `surveillance_service`：缺省或非法值在后端会被明确拒绝，
 * 前端在这里就把它选出来，绝不静默回退到某一个 variant。
 */
function mapFigureParameters(templateId,variant){
  if(String(templateId)!==MAP_FIGURE_SURVEILLANCE_TEMPLATE)return {};
  const values=MAP_FIGURE_SURVEILLANCE_VARIANTS.map(item=>item[0]);
  const value=values.includes(String(variant))?String(variant):values[0];
  return {[MAP_FIGURE_SURVEILLANCE_PARAMETER]:value};
}

/** 模板下拉：只有 status=available 的模板可选，其余如实标注「尚未实现」。 */
function mapFigureTemplateOptions(model,selectedTemplateId=''){
  return mapFigureTemplateList(model).map(([id,label,available])=>
    '<option value="'+escapeHtml(id)+'"'
    +(available&&id===selectedTemplateId?' selected':'')+(available?'':' disabled')+'>'
    +escapeHtml(label)+(available?'':'（尚未实现）')+'</option>').join('');
}

/** 可选权威运行航路（只认 geometry 顶点 >= 2 的航路）。 */
function mapFigureRouteOptions(flow,selectedRouteId=''){
  const routes=flow.operational_routes||[];
  return routes.map(route=>{
    const points=(route.path||[]).length,plottable=points>=2;
    return '<option value="'+escapeHtml(route.route_id||'')+'"'
      +(plottable&&route.route_id===selectedRouteId?' selected':'')
      +(plottable?'':' disabled')+'>'
      +escapeHtml(route.route_id||'')+(plottable?'':'（几何顶点不足，无法制图）')+'</option>';
  }).join('');
}

export function mapFiguresModel(flow,figureState){
  const routes=flow.operational_routes||[],plottable=routes.filter(route=>(route.path||[]).length>=2);
  const state=figureState||getMapFigureState();
  const collection=state?.records||{},records=Array.isArray(collection.items)?collection.items:[];
  const active=records.find(item=>item.figure_id===collection.active_figure_id)||null;
  return {
    routeCount:routes.length,plottableCount:plottable.length,records,active,
    loaded:Boolean(state?.loaded),loading:Boolean(state?.loading),error:String(state?.error||''),
    catalog:state?.catalog||null,
    canGenerate:plottable.length>0,
    reason:plottable.length>0?'':(routes.length>0
      ?'当前运行航路的几何顶点不足，无法制图。'
      :'当前项目还没有权威运行航路；请先在 Step03 完成验证、采纳与发布。')
  };
}

/** 缺数据图层的简短中文说明（每项一行，不伪造也不隐藏）。 */
function omittedLayerLines(active){
  const items=(active&&active.omitted_layers)||[];
  if(!items.length)return '<small>本图所需图层的数据都可用。</small>';
  return items.slice(0,12).map(item=>'<small>本图未显示 '+escapeHtml(item.display_name||item.layer_key)
    +'：'+escapeHtml(item.reason||'数据不可用')+'</small>').join('');
}

/** 当前图件状态的中文结论：current / stale_revision 各自说清含义。 */
function mapFigureStatusNote(model){
  const active=model.active;
  if(active){
    const applicability=active.current_applicability||'current';
    if(applicability==='stale_revision')return applicabilityText(applicability);
    return applicabilityText(applicability);
  }
  if(model.error)return '读取图件状态失败：'+model.error;
  if(!model.loaded)return '正在读取图件状态…';
  if(model.records.length)return '已有历史图件，但没有指向当前航路的有效图件；请重新生成。';
  return '尚未生成任何图件。';
}

/**
 * 专题图区域正文（**可单独重渲染**）。
 *
 * 之所以拆出来：导出成功后只需局部刷新这一块，绝不整页重渲染（那会丢掉用户在其它
 * 分段里填写的草稿与滚动位置）。
 */
function mapFigureRegionBody(flow,selection={}){
  const model=mapFiguresModel(flow),active=model.active;
  const plottable=(flow.operational_routes||[]).filter(route=>(route.path||[]).length>=2);
  const requestedRoute=String(selection.selected_route_id||'');
  const activeRoute=String(active?.route_id||'');
  const selectedRoute=(plottable.some(route=>route.route_id===requestedRoute)&&requestedRoute)
    ||(plottable.some(route=>route.route_id===activeRoute)&&activeRoute)
    ||String(plottable[0]?.route_id||'');
  const requestedTemplate=String(selection.selected_template_id||'');
  const activeTemplate=String(active?.template_id||'');
  const templateList=mapFigureTemplateList(model);
  const availableTemplates=templateList.filter(item=>item[2]).map(item=>item[0]);
  const selectedTemplate=(availableTemplates.includes(requestedTemplate)&&requestedTemplate)
    ||(availableTemplates.includes(activeTemplate)&&activeTemplate)
    ||availableTemplates[0]||'';
  const requestedVariant=String(selection.selected_surveillance_service||'');
  const activeVariant=String(active?.parameters?.[MAP_FIGURE_SURVEILLANCE_PARAMETER]||'');
  const variantValues=mapFigureSurveillanceVariants(model).map(item=>item[0]);
  const selectedVariant=(variantValues.includes(requestedVariant)&&requestedVariant)
    ||(variantValues.includes(activeVariant)&&activeVariant)
    ||variantValues[0]||'';
  const routeOptions=mapFigureRouteOptions(flow,selectedRoute);
  const generatedAt=active&&active.generated_at?escapeHtml(active.generated_at):'—';
  const bytes=active&&Number.isFinite(active.image_bytes)?(active.image_bytes/1024).toFixed(0)+' KB':'—';
  const applicability=active?(active.current_applicability||'current'):null;
  const stale=applicability==='stale_revision';
  return reviewBlock('专题成果图',
    '<p class="parameter-note">专题图只读消费当前权威运行航路与已配置 GIS 数据源，用 QGIS 版面程序化生成（上方地图、下方图例），不会重算或回写任何业务结论。缺数据的图层会被省略并写明原因。</p>'
    +(stale?'<p class="inline-error">'+escapeHtml(applicabilityText('stale_revision'))+'</p>':'')
    +'<label>模板<select id="mapFigureTemplate">'
      +mapFigureTemplateOptions(model,selectedTemplate)+'</select></label>'
    +mapFigureVariantOptions(model,selectedTemplate,selectedVariant)
    +'<label>航路<select id="mapFigureRoute">'+routeOptions
      +(routeOptions?'':'<option value="">（当前没有可制图的运行航路）</option>')+'</select></label>'
    +(!model.canGenerate?'<p class="empty">'+escapeHtml(model.reason)+'</p>':'')
    +'<div class="button-row"><button class="secondary" id="previewMapFigure" '+(model.canGenerate?'':'disabled')+'>预览</button>'
    +'<button class="primary" id="exportMapFigure" '+(model.canGenerate?'':'disabled')+'>生成 PNG</button></div>'
    +'<div class="button-row"><button class="secondary" id="downloadMapFigure" '+(!active?'disabled':'')+'>打开/下载 PNG</button>'
    +'<button class="secondary" id="downloadMapFigureSpec" '+(!active?'disabled':'')+'>下载图件规格(JSON)</button></div>'
    +reviewBlock('当前图件',
      kvRow('模板',active?escapeHtml((active.title||active.template_id)):'尚未生成')
      +kvRow('航路',active?escapeHtml(active.route_id||'—'):'—')
      +kvRow('状态',active?statusBadge(applicability,applicabilityText(applicability))
        :statusBadge('not_calculated'),mapFigureStatusNote(model))
      +kvRow('图件 revision',active?escapeHtml(String(active.project_revision??'—')):'—','项目当前 revision '+String(flow.revision??'—'))
      +kvRow('格式 / DPI',active?escapeHtml(String(active.format||'png').toUpperCase())+' · '+escapeHtml(String(active.dpi??'—')):'—')
      +kvRow('生成时间',generatedAt)+kvRow('文件大小',bytes))
    +reviewBlock('本图未显示的图层',omittedLayerLines(active)),
    '图件记录保存在当前项目目录的 artifacts/map_figures/routes/<航路>/ 下；图件索引不进入项目状态，因此项目变更后旧图仍可打开，但会被标记为「'+applicabilityText('stale_revision')+'」。');
}

function mapFigurePanel(flow,selection){
  // 稳定容器 id：导出成功后只替换这一块（局部刷新，不整页重渲染）。
  // ``selection`` 只在测试与局部刷新时显式给出；生产首屏渲染走默认值。
  return '<div id="mapFigureRegion" data-map-figure-region="true">'
    +mapFigureRegionBody(flow,selection||{})+'</div>';
}

/**
 * 局部刷新专题图区域（并重新绑定该区域内的按钮）。
 *
 * @param {object} c 步骤上下文（需要 `flow()` 与 `api`）
 * @returns {boolean} 是否真的刷新了已挂载的容器
 */
export function refreshMapFigureRegion(c){
  if(!c||typeof document==='undefined')return false;
  const host=document.getElementById('mapFigureRegion');
  if(!host)return false;
  const selection={
    selected_route_id:c.$?.('mapFigureRoute')?.value||'',
    selected_template_id:c.$?.('mapFigureTemplate')?.value||'',
    selected_surveillance_service:c.$?.('mapFigureSurveillanceService')?.value||'',
  };
  host.innerHTML=mapFigureRegionBody(c.flow?c.flow():{},selection);
  bindMapFigureActions(c);
  return true;
}

/** 首次进入 / 首次渲染读取一次真实图件状态，然后局部刷新。 */
export function ensureMapFigureState(c){
  const state=getMapFigureState();
  const identity=String(c?.projectOpenStep?.()?.identity||'');
  if(state.project_identity!==identity){
    resetMapFigureState();
    // 先同步擦掉旧项目图件，再等待新项目 GET；切换过程中也绝不继续展示 A 的图件。
    refreshMapFigureRegion(c);
  }
  const current=getMapFigureState();
  if(current.loaded||current.loading)return false;
  if(!c||typeof c.api!=='function')return false;
  refreshMapFigureState({api:c.api,projectIdentity:identity})
    .then(()=>refreshMapFigureRegion(c))
    .catch(()=>{/* 失败已记进 mapFigureState.error，界面会如实显示 */});
  return true;
}

/** 导出流程：POST export → GET state → 局部刷新（**绝不**安装成 workflow）。 */
async function exportMapFigure(c){
  const template=c.$('mapFigureTemplate')?.value||'route_overview_v1';
  const routeId=c.$('mapFigureRoute')?.value||c.flow().operational_routes?.[0]?.route_id||null;
  const variant=c.$('mapFigureSurveillanceService')?.value||'';
  // computeAction：原样返回 POST 响应，不把它当 workflow 快照安装（resourceAction 会），
  // 因此 export 的局部响应不可能污染 flow。
  const response=await c.computeAction('/api/map-figures/export',{
    template_id:template,route_id:routeId||null,format:'png',dpi:300,
    parameters:mapFigureParameters(template,variant),
  });
  await refreshMapFigureState({
    api:c.api,projectIdentity:String(c?.projectOpenStep?.()?.identity||''),
  });
  refreshMapFigureRegion(c);
  const figureId=response?.figure_id||activeMapFigure()?.figure_id||'';
  if(c.panelError)c.panelError('专题图已生成'
    +(response?.reused?'（复用已有图件）':'')+'：'+figureId,'success');
  return response;
}

/** 专题图区域内的按钮绑定（初次渲染与局部刷新共用同一份）。 */
function bindMapFigureActions(c){
  // 专题成果图：三个入口都不自动触发——预览 / 生成 / 下载全部由用户显式点击。
  // 模板切换要立刻重渲染区域：监视布设图的 variant 选择器只在该模板下存在，
  // 不重渲染就会出现「选了 RID 却按 Radar 导出」这类错配。
  const templateSelect=c.$('mapFigureTemplate');
  if(templateSelect&&!templateSelect.dataset?.mapFigureBound){
    if(templateSelect.dataset)templateSelect.dataset.mapFigureBound='1';
    // 极简 DOM 替身（部分测试环境）没有 addEventListener；真实浏览器一定有。
    if(typeof templateSelect.addEventListener==='function'){
      templateSelect.addEventListener('change',()=>refreshMapFigureRegion(c));
    }
  }
  c.actionButton('previewMapFigure',async()=>{
    const template=c.$('mapFigureTemplate')?.value||'route_overview_v1';
    const routeId=c.$('mapFigureRoute')?.value||'';
    const variant=c.$('mapFigureSurveillanceService')?.value||'';
    await c.previewMapFigure({
      template_id:template,route_id:routeId,
      parameters:mapFigureParameters(template,variant),
    });
  });
  c.actionButton('exportMapFigure',()=>exportMapFigure(c));
  c.actionButton('downloadMapFigure',()=>c.downloadMapFigure());
  c.actionButton('downloadMapFigureSpec',()=>c.downloadMapFigure('spec'));
}
// ---- 高级：需求依据 / 布站提案证据 ------------------------------------------
function requirementPanel(flow){
  const recommendation=flow.required_cns_recommendation||{},adoption=flow.required_cns_adoption||{};
  return '<div class="review-block"><b>需求依据与来源追溯</b>'
    +'<span>需求模型：'+escapeHtml((recommendation.algorithm_id||flow.algorithm_selection?.requirement_model?.algorithm_id||'manual_required_cns_v1')+'@'+(recommendation.algorithm_version||flow.algorithm_selection?.requirement_model?.version||'1.0'))+'</span>'
    +'<span>需求建议：'+statusText(recommendation.status||'not_calculated')+' · 采用状态：'+statusText(adoption.status||'not_adopted')+'</span>'
    +'<span>匹配规则：'+(recommendation.matched_policies||[]).length+' · 来源字段：'+Object.keys(recommendation.field_provenance||{}).length+'</span>'
    +'<small>需求与能力/服务分离；不代表自动法规合规。</small>'
    +advancedAuditNote('开发阶段编号与版本代号只在本高级区出现：设施规划试算属 P14 → P15 → P16 阶段链，规划缺口与网格风险在生产界面不再带版本代号。')
    +'</div>';
}
function proposalPanel(flow){
  const proposal=flow.cns_corridor_site_plan||{};
  return '<div class="review-block"><b>CNS 设施规划方案</b>'
    +'<span>状态：'+statusText(proposal.status||'not_calculated')+'</span>'
    +'<span>目标体素：'+(proposal.target_voxel_count||0)+' · 已选动作：'+(proposal.selected_actions||[]).length+'</span>'
    +'<span>确认需求单位体积收益：'+(Number.isFinite(proposal.confirmed_requirement_unit_volume_gain)?proposal.confirmed_requirement_unit_volume_gain.toFixed(1)+' m³·unit':'—')+'</span>'
    +'<small>方案本身不修改已有 CNS 设施；方案评审负责人工比较、确认与受控应用。</small></div>'
    +compatibilityIsolation(flow)
    +'<div class="button-row export-row"><a class="secondary button-link" download="sites.geojson" href="/api/export/sites">旧版兼容站址 GeoJSON</a></div>';
}

// ---- 渲染 -------------------------------------------------------------------
export function render({state,flow,selection}){
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const selected=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id)||null;
  const OPERATE=REVIEW_SEGMENTS.operate,RESULT=REVIEW_SEGMENTS.result,ADVANCED=REVIEW_SEGMENTS.advanced;

  const body=wbPanel('operate','',{segments:[
      ['review-op-overview','评审概览',wbBlock('评审概览',wbSegHint(OPERATE,'review-op-overview')+overviewPanel(flow,state))],
      ['review-op-compare','方案比较',wbBlock('方案比较',wbSegHint(OPERATE,'review-op-compare')+comparisonPanel(flow,review,selected))],
      ['review-op-edit','方案编辑',wbBlock('方案编辑',wbSegHint(OPERATE,'review-op-edit')+editPanel(selected))],
      ['review-op-confirm','确认与应用',wbBlock('确认与应用',wbSegHint(OPERATE,'review-op-confirm')+confirmPanel(flow,confirmed,selected,summary))]
    ]})
    +wbPanel('result','',{segments:[
      ['review-res-status','状态总览',wbBlock('状态总览',wbSegHint(RESULT,'review-res-status')+statusOverview(flow)+routeSafetyEvidencePanel(flow))],
      ['review-res-report','报告与交付',wbBlock('报告与交付',wbSegHint(RESULT,'review-res-report')+reportPanel(flow)+deliveryPanel(flow)+mapFigurePanel(flow,selection))]
    ]})
    +wbPanel('advanced','',{segments:[
      ['review-adv-requirement','需求依据',wbBlock('需求依据',wbSegHint(ADVANCED,'review-adv-requirement')+requirementPanel(flow))],
      ['review-adv-proposal','布站提案证据',wbBlock('布站提案证据',wbSegHint(ADVANCED,'review-adv-proposal')+proposalPanel(flow))]
    ]});
  return shell('06','方案评审','比较客观指标，人工选择、确认，再事务式应用。',body);
}

export function bind(c){
  c.actionButton('saveAll',()=>c.mutate('save'));
  c.actionButton('initializePlanReview',()=>c.resourceAction('/api/cns-plan-review/initialize',{}));
  c.actionButton('evaluatePlanVariant',()=>c.resourceAction('/api/cns-plan-review/evaluate',{variant_id:c.flow().cns_plan_review?.selected_variant_id}));
  for(const button of document.querySelectorAll('.selectPlanVariant'))button.onclick=()=>c.resourceAction('/api/cns-plan-review/select',{variant_id:button.dataset.variantId});
  c.actionButton('createPlanVariant',()=>c.resourceAction('/api/cns-plan-review/variant',{
    base_variant_id:c.flow().cns_plan_review?.selected_variant_id,name:c.$('variantName').value,
    include_action_ids:list(c.$('variantInclude').value),exclude_action_ids:list(c.$('variantExclude').value),
  }));
  c.actionButton('confirmPlan',()=>c.resourceAction('/api/cns-plan-review/confirm',{
    variant_id:c.flow().cns_plan_review?.selected_variant_id,
    confirm_without_objectives:c.$('confirmWithoutObjectives').checked,
    source:'step06_user_confirmation',reason:c.$('planDecisionReason').value,
  }));
  c.actionButton('applyPlan',()=>c.resourceAction('/api/cns-plan-review/apply',{plan_id:c.flow().confirmed_cns_plan?.plan_id}));
  c.actionButton('previewPlanningReport',()=>c.previewPlanningReport());
  c.actionButton('generatePlanningReport',()=>c.resourceAction('/api/cns-planning-report/generate',{}));
  c.actionButton('downloadReportHtml',()=>c.downloadPlanningReport('html'));
  c.actionButton('downloadReportPdf',()=>c.downloadPlanningReport('pdf'));
  c.actionButton('downloadReportPackage',()=>c.downloadPlanningReport('package'));
  // 专题成果图：首次进入读取一次真实图件状态（来自外部 index，不读 flow.map_figures）。
  ensureMapFigureState(c);
  bindMapFigureActions(c);
  // 显式评价：不存在任何自动后台评价，前端只提交一次 POST 并转印后端结果。
  c.actionButton('evaluateRouteSafetyEvidenceV2',()=>c.resourceAction(
    '/api/route-safety-evidence-v2/evaluate',{}));
}
