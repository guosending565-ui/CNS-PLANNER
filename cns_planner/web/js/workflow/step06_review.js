// =========================================================
// Step06 方案评审：评审 / 决策 / 交付工作台
//
// 信息架构
//  - 操作：评审概览 / 方案比较 / 方案编辑 / 确认与应用
//  - 结果：状态总览 / 报告与交付
//  - 高级：需求依据 / 布站提案证据
//  每个一级标签下同一时刻只显示一个任务（由 workbench.js 的 .wb-seg-active 保证）。
//
// 语义边界（本次重组只改展示组织，不改任何业务契约）
//  - Select（选择方案）≠ Confirm（冻结快照）≠ Apply（事务提交）：三步在视觉上分开，
//    Confirm 与 Apply 不再并排成等价主按钮；
//  - Plan Variant 始终是人工决策候选：不计算隐藏总分、排名或优胜者，
//    也没有任何"自动推荐 / 自动选择"通道；
//  - 所有状态都从现有 flow / state 派生，不新增、不重算任何结论；
//  - 所有既有 DOM id、API path、payload 与 bind 语义保持原样。
// =========================================================
import {escapeHtml,shell,statusBadge,statusText,wbPanel,wbBlock,wbSegHint,wbDisclosure} from './common.js';

// ---- 二级任务分段 -----------------------------------------------------------
// id 稳定（review-*），标签是第一视觉层的业务语言：
// 不把 P 编号 / 算法 id 当作导航语言（它们只出现在说明或高级标签里）。
export const REVIEW_SEGMENTS={
  operate:[['review-op-overview','评审概览'],['review-op-compare','方案比较'],['review-op-edit','方案编辑'],['review-op-confirm','确认与应用']],
  result:[['review-res-status','状态总览'],['review-res-report','报告与交付']],
  advanced:[['review-adv-requirement','需求依据'],['review-adv-proposal','布站提案证据']]
};

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
function formatCosts(summary){const values=summary?.explicit_costs_by_unit||{},text=Object.entries(values).map(([unit,value])=>number(value)+' '+escapeHtml(unit)).join('；');return text||'action_count_proxy（未伪造货币成本）';}
function statusOf(map,key){return map&&map[key]?map[key]:'not_calculated';}
function kvRow(label,value,note){return '<div class="review-row"><span>'+escapeHtml(label)+'</span><span class="review-value">'+(value||'—')+(note?'<small>'+escapeHtml(note)+'</small>':'')+'</span></div>';}
function badgeRow(label,status,note){return kvRow(label,statusBadge(status),note);}
function reviewBlock(title,body,note){return '<div class="review-block"><b>'+escapeHtml(title)+'</b>'+(body||'')+(note?'<small>'+escapeHtml(note)+'</small>':'')+'</div>';}

/**
 * 状态总览：按业务组展示既有 status。
 * 只映射现有 result_statuses / risks / review 的取值，不重新计算任何结论。
 */
const STATUS_GROUPS=[
  ['规划输入',[['workspace','工作区'],['grid','标准网格'],['routes','运行航路']]],
  ['CNS 规划',[
    ['coverage','基础覆盖'],['coverage_3d','3D 几何覆盖'],['cns_service_capability','CNS 服务能力'],
    ['service_timeline','服务时间线'],['protection_envelope','保护包络'],['cns_gap','规划缺口'],
    ['cns_gap_v2','规划缺口 V2'],['cns_site_plan','布站方案'],['building_clearance','建筑净空'],
    ['route_vertical_profiles','航路垂直剖面'],['encounter_3d_assessment','3D 相遇评估'],
    ['layered_route_candidate','分层航路候选']
  ]],
  ['风险',[
    ['environment_risk','环境风险'],['grid_risk_v2','网格风险 V2'],['technical_risk','技术风险'],
    ['safety_assessment','安全评估']
  ]],
  ['方案与交付',[
    ['cns_plan_review','方案审查'],['cns_corridor_assessment','服务走廊评估'],
    ['cns_corridor_gap_assessment','走廊空间缺口评估'],['cns_corridor_site_plan','走廊布站提案'],
    ['required_cns_recommendation','需求建议'],['report','规划报告']
  ]]
];
const EXTRA_RISK_LABELS=[['life','生命风险'],['property','财产风险']];

// ---- 评审概览 ---------------------------------------------------------------
function projectOverview(flow,state){
  const layers=Object.entries(flow.coverage?.layers||{}).map(([key,value])=>key+'：'+value.statistics.stations+' 站 / '+statusText(value.status)).join('<br>');
  return reviewBlock('项目概览',[
    kvRow('数据源',statusBadge(state.data_health.status)),
    kvRow('工作区',flow.workspace?flow.workspace.area_km2+' km²':'未定义'),
    kvRow('运行航路',String(flow.operational_routes.length),flow.operational_routes.map(item=>item.route_id).join('、')),
    kvRow('飞行器',flow.aircraft?escapeHtml(flow.aircraft.manufacturer+' '+flow.aircraft.model):'未设置'),
    kvRow('飞行规则',statusBadge(flow.rules?.status||'not_calculated')),
    // 逐层级布站明细：整行留着说明，不塞进右对齐的取值列
    layers?'<div class="review-row"><span>C/N/S 布站层级</span><span class="review-value">—</span></div><small>'+layers+'</small>':''
  ].join(''));
}

/** 评审流程状态：只读现有 state，不做任何结论判断。 */
function reviewStatusPanel(flow){
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const application=confirmed.application||{},reports=flow.cns_planning_reports||{};
  const active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id)||null;
  return reviewBlock('方案审查（Plan Review）与受控应用',[
    kvRow('方案审查',statusBadge(review.status||'not_initialized'),'初始化后由人工比较与选择'),
    kvRow('候选方案数',String(summary.variantCount)),
    kvRow('已选方案',summary.selectedVariantId?escapeHtml(summary.selectedVariantId):'尚未选择'),
    kvRow('所选动作数',String(summary.selectedActionIds.length)),
    kvRow('确认门禁',statusBadge(summary.gate)),
    kvRow('已确认方案',statusBadge(summary.confirmedStatus)),
    kvRow('应用状态',statusBadge(summary.applyStatus),application.planning_origin?'来源：'+escapeHtml(String(application.planning_origin.plan_id||'')):''),
    kvRow('规划报告',statusBadge(active?active.current_applicability:(reports.status||'not_calculated')),active?'报告ID：'+escapeHtml(active.report_id):'尚未生成')
  ].join(''),'候选方案无自动总分/排名（no automatic overall score/rank），也不自动推荐或选择；三步语义彼此独立（选择 / 确认 / 应用）。');
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
  if(!matrix.length)return '<div class="wb-empty">所选方案尚无 comparison matrix；请先重新评价该方案。</div>';
  return matrix.map(comparisonRowCard).join('');
}
/** 完整 Objective Comparison Matrix：原 8 列表格，收进 wbDisclosure。 */
function comparisonMatrixTable(selected){
  const matrix=selected?.evaluation?.comparison_matrix||[];
  const rows=matrix.map(row=>'<tr><td>'+escapeHtml(row.route_id)+'</td><td>'+escapeHtml(row.subsystem)+'</td><td>'+statusText(row.objective_status||'not_evaluated')+'</td><td>'+formatDist(row.service)+'</td><td>'+formatDist(row.redundancy)+'</td><td>'+number(row.total_confirmed_deficit_projection_m)+'</td><td>'+number(row.max_continuous_deficit_projection_m)+'</td><td>'+(row.unknown_voxel_ids||[]).length+'</td></tr>').join('');
  const table='<div class="table-wrap"><table><thead><tr><th>航路</th><th>C/N/S</th><th>规划目标</th><th>服务 满足/缺口/未知</th><th>冗余 满足/缺口/未知</th><th>缺口总长 m</th><th>最大连续缺口 m</th><th>证据不足体素</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  return wbDisclosure('完整客观指标矩阵',matrix.length?table:'<div class="wb-empty">尚未评价所选方案。</div>');
}

// ---- 方案编辑 / 确认与应用 ---------------------------------------------------
function variantCards(selectedVariantId,review){
  return (review.variants||[]).map(variant=>{
    const evaluation=variant.evaluation||{},gate=evaluation.confirmation_gate||{},actions=variant.selected_action_ids||[];
    const selected=variant.variant_id===selectedVariantId;
    return '<div class="review-block plan-variant '+(selected?'selected':'')+'"><b>'+escapeHtml(variant.name||variant.variant_id)+'</b>'
      +'<span>'+escapeHtml(variant.source||'')+' · '+actions.length+' 个动作 · 确认门禁：'+statusText(gate.status||'not_evaluated')+'</span>'
      +'<small>'+escapeHtml(variant.variant_id||'')+'</small>'
      +'<button class="secondary selectPlanVariant" data-variant-id="'+escapeHtml(variant.variant_id||'')+'">'+(selected?'当前已选（重新选择）':'选择此方案')+'</button></div>';
  }).join('')||'<div class="empty">尚未初始化方案审查。</div>';
}
function comparisonPanel(review,selected){
  return reviewBlock('人工决策候选（无自动推荐）',
    '<div class="variant-grid">'+variantCards(review.selected_variant_id,review)+'</div>'
    +'<div class="button-row"><button class="secondary" id="initializePlanReview">初始化方案审查</button><button class="secondary" id="evaluatePlanVariant" '+(!selected?'disabled':'')+'>重新评价所选方案</button></div>',
    '选择只改变当前候选方案，不修改任何设施；系统不排序、不评分、不自动选择。')
    +reviewBlock('所选方案逐航路比较（紧凑卡）',comparisonCards(selected)
      +'<small>满足/缺口/未知均为体素计数；缺口长度为保守纵向投影，不是运行中断时长。</small>')
    +reviewBlock('完整客观指标矩阵',comparisonMatrixTable(selected))
    +reviewBlock('显式费用',escapeHtml(formatCosts(selected?.evaluation?.action_summary)),'按单位分组，不做跨单位合计；未提供显式费用时使用 action_count_proxy。');
}
function editPanel(selected){
  return reviewBlock('用户编辑方案（人工决策候选）',
    kvRow('编辑基线',selected?escapeHtml(selected.name||selected.variant_id)+'（'+escapeHtml(selected.variant_id)+'）':'尚未选择方案')
    +'<label>纳入动作ID（逗号分隔）<input id="variantInclude" placeholder="candidate_site:S1:C1"></label>'
    +'<label>排除动作ID（逗号分隔）<input id="variantExclude"></label>'
    +'<label>方案名称<input id="variantName" value="用户方案"></label>'
    +'<button class="secondary full" id="createPlanVariant" '+(!selected?'disabled':'')+'>克隆并创建方案</button>',
    '创建只追加人工编辑候选；不会自动选中，也不会改动设施。');
}
function confirmPanel(confirmed,selected,summary){
  const evaluation=selected?.evaluation||{},gate=evaluation.confirmation_gate||{},gateStatus=gate.status||'not_evaluated';
  const acknowledged=gate.requires_confirm_without_objectives_acknowledgement===true;
  const canConfirm=Boolean(selected)&&(gateStatus==='ready_for_confirmation'||acknowledged);
  const canApply=Boolean(summary.confirmedStatus==='confirmed'&&confirmed.current_applicability==='current');
  const applyId=String(confirmed.plan_id||'');
  return reviewBlock('1 选择 → 2 确认 → 3 应用',[
    kvRow('当前所选方案',selected?escapeHtml(selected.name||selected.variant_id)+'（'+escapeHtml(selected.variant_id)+'）':'尚未选择'),
    kvRow('确认门禁状态',statusBadge(gateStatus),acknowledged?'未配置规划目标：勾选知情确认并填写理由后才能确认':'仅 ready_for_confirmation 可直接确认')
  ].join(''),'三步语义不同：选择只切换候选，确认只冻结快照，应用才事务提交。')
    +reviewBlock('第 2 步 · 确认（冻结快照，不修改设施）',
      kvRow('已确认方案',statusBadge(summary.confirmedStatus),confirmed.plan_id?'plan_id：'+escapeHtml(String(confirmed.plan_id)):'')
      +'<label><input type="checkbox" id="confirmWithoutObjectives" '+(acknowledged?'':'disabled')+'> 未配置规划目标时仍确认（记录明确知情确认）</label>'
      +'<label>确认理由<input id="planDecisionReason" placeholder="请记录人工确认依据"></label>'
      +'<button class="secondary full" id="confirmPlan" data-gate="'+escapeHtml(gateStatus)+'" '+(canConfirm?'':'disabled')+'>确认所选方案</button>',
      acknowledged?'勾选后必须填写确认理由，后端会把知情确认写入 acknowledgements。':'确认只冻结该方案的快照，不写入任何设施。')
    +reviewBlock('第 3 步 · 应用（事务提交）',
      kvRow('可应用条件',statusBadge(canApply?'ready':'not_available'),canApply?'已确认且当前有效，可以提交':'需要先确认方案，且 RequiredCNS/routes/设施/候选/设备基线未变化')
      +'<button class="primary full" id="applyPlan" data-apply-gate="'+(canApply?'ready':'blocked')+'" '+(canApply?'':'disabled')+'>应用已确认方案</button>',
      '应用会重跑 P7→P10 与 P14→P15；验证不一致、出现确认回归或关键证据不足时整笔回滚。'+(applyId?' 当前 plan_id：'+escapeHtml(applyId):''));
}

// ---- Route Safety Evidence V2 -------------------------------------------------
// 四个 evidence domain 分开显示。全部字段来自后端 route_safety_evidence_v2，
// 前端不做任何重新推导，也不合成 overall safety score / 排名。
export const ROUTE_SAFETY_EVIDENCE_DOMAINS=[
  ['geometry_obstacle','Geometry / Obstacle'],
  ['ground_exposure','Ground Exposure'],
  ['regulatory','Regulatory'],
  ['cns_operational_support','CNS Operational Support']
];
export const ROUTE_SAFETY_EVIDENCE_STATUS_NOTE=
  '该状态表示证据完整性/明确硬约束结果，不是自动安全认证或安全评分。';
export const ROUTE_SAFETY_EVIDENCE_CNS_NOTE=
  'CNS confirmed gap 记为 operational support deficit：它不代表航路不安全，也不改变 geometry validation。';

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

/** 单个 domain 的紧凑指标（只转印后端关键字段）。 */
function safetyDomainMetrics(model){
  const metrics=model.metrics||{};
  if(model.id==='geometry_obstacle'){
    return [['validation status',text(model.evidence&&model.evidence.validation_status)||'—'],
      ['terrain status',text(metrics.terrain_status,'—')+' · min margin '+metricText(metrics.terrain_minimum_margin_m)+' m'],
      ['building status',text(metrics.building_status,'—')+' · min margin '+metricText(metrics.building_minimum_margin_m)+' m'],
      ['fixed cruise altitude',metricText(metrics.fixed_cruise_altitude_m)+' m EGM2008'],
      ['unresolved intervals',String(metrics.unresolved_interval_count??0)
        +' · failed intervals '+String(metrics.failed_interval_count??0)],
      ['resource-limited evidence',metrics.resource_limited?'是（计算资源限制，不是安全结论）':'否']];
  }
  if(model.id==='ground_exposure'){
    const theta=(model.evidence&&model.evidence.theta_star_planning_objective)||{};
    const density=(model.evidence&&model.evidence.route_risk_density)||{};
    return [['Theta* population×shelter exposure',metricText(metrics.population_shelter_risk_exposure_index_m)+' index·m'],
      ['route_risk_density',metricText(metrics.route_risk_density)+' / threshold '+metricText(metrics.route_risk_density_threshold)
        +' · '+text(metrics.route_risk_density_status,'unresolved')],
      ['objective weights',['risk','turn','distance'].map(key=>key+' '+metricText((theta.weights||{})[key])).join(' · ')],
      ['RouteRiskProfile ground mean/max',metricText(metrics.profile_ground_mean_index)+' / '+metricText(metrics.profile_ground_max_index)],
      ['profile exposure / unresolved length',metricText(metrics.profile_ground_exposure_index_m)+' index·m / '
        +metricText(metrics.profile_ground_unresolved_length_m)+' m'],
      ['profile thresholds / provenance',metricText(metrics.profile_thresholds&&metrics.profile_thresholds.medium_min)
        +' / '+metricText(metrics.profile_thresholds&&metrics.profile_thresholds.high_min)+' · '
        +text(metrics.profile_thresholds&&metrics.profile_thresholds.status,'not_configured')],
      ['density role',density.objective_term?'objective term（异常：后端绝不允许）':'评价约束，不是 objective term']];
  }
  if(model.id==='regulatory'){
    return [['dataset status',text(model.evidence&&model.evidence.dataset_status,'not_configured')],
      ['configured',metrics.configured?'是':'否（未配置 ≠ passed）'],
      ['evaluated',metrics.evaluated?'是':'否（not_evaluated ≠ passed）'],
      ['blocked constraints',String((metrics.blocked_constraints||[]).length)],
      ['unresolved constraints',String((metrics.unresolved_constraints||[]).length)],
      ['segment count',String(metrics.segment_count??0)]];
  }
  const subsystems=Array.isArray(metrics.subsystems)?metrics.subsystems:[];
  const rows=subsystems.map(item=>[item.subsystem+' verdict',
    text(item.operational_support_verdict,'unknown')
    +' · coverage '+text(item.coverage&&item.coverage.status,'—')
    +' · capability '+text(item.capability&&item.capability.status,'—')
    +' · gap length '+metricText(item.confirmed_gap&&item.confirmed_gap.gap_length_m)+' m']);
  return rows.concat([
    ['operational support deficit',metrics.operational_support_deficit?'是（operational support deficit）':'否'],
    ['confirmed gap C/N/S',(metrics.confirmed_gap_subsystems||[]).join('/')||'—'],
    ['unknown C/N/S',(metrics.unknown_subsystems||[]).join('/')||'—'],
  ]);
}

function safetyDomainCard(model){
  const metrics=safetyDomainMetrics(model).map(([label,value])=>kvRow(label,escapeHtml(value))).join('');
  const sources=model.sources.length
    ?model.sources.map(source=>kvRow('source',escapeHtml(text(source.role,'—'))
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
    +statusBadge(model.status)+(model.hardFailure?statusBadge('failed'):'')+'</div>'
    +(model.reason?'<small>'+escapeHtml(model.reason)+'</small>':'')
    +metrics
    +'<div class="review-sub"><b>evidence source</b></div>'+sources
    +limitations
    +boundary
    +'</div>';
}

/**
 * Route Safety Evidence 紧凑卡片：overall evidence status + 四个 domain card，
 * 高级 fingerprint / source lineage 折叠。
 */
function routeSafetyEvidencePanel(flow){
  const model=routeSafetyEvidenceModel(flow);
  const ready=model.readinessStatus==='ready';
  const overall=[
    kvRow('overall evidence status',statusBadge(model.status),model.statusReason||''),
    kvRow('current_applicability',statusBadge(model.applicability),
      model.isCurrent?'这是 current assessment':'非 current，仅作历史证据'),
    kvRow('评估对象',model.routeId?escapeHtml(model.routeId)+' · adoption '+escapeHtml(model.adoptionId||'—'):'尚未评估'),
    kvRow('lineage',model.lineage.complete?'完整（complete）':'不完整：'
      +escapeHtml([...model.lineage.missing,...model.lineage.stale].join('、')||'—')),
    kvRow('readiness',statusBadge(model.readinessStatus),ready?'':'需要先存在 current published layered operational adoption'),
  ].join('');
  const cards=model.domains.map(safetyDomainCard).join('');
  const fingerprintRows=[
    ['assessment fingerprint',model.fingerprints.assessment_fingerprint],
    ['operational route/adoption',model.fingerprints.operational_route_adoption_fingerprint],
    ['validation',model.fingerprints.validation_fingerprint],
    ['candidate',model.fingerprints.candidate_fingerprint],
    ['RouteRiskProfile',model.fingerprints.route_risk_profile_fingerprint],
    ['regulatory dataset',model.fingerprints.regulatory_dataset_fingerprint],
    ['Coverage3D',model.fingerprints.coverage_3d_fingerprint],
    ['CNS capability',model.fingerprints.cns_service_capability_fingerprint],
    ['Gap V2',model.fingerprints.cns_gap_v2_fingerprint],
    ['corridor',model.fingerprints.cns_corridor_fingerprint],
    ['evaluator version',model.fingerprints.evaluator_version],
  ].map(([label,value])=>kvRow(label,escapeHtml(text(value,'—')))).join('');
  const limitationRows=model.limitations.map(item=>'<small>'+escapeHtml(item)+'</small>').join('');
  return reviewBlock('Route Safety Evidence',
    '<p class="parameter-note">'+escapeHtml(ROUTE_SAFETY_EVIDENCE_STATUS_NOTE)+'</p>'
    +'<div class="scroll-list route-list">'+overall+'</div>'
    +'<div class="safety-domain-grid">'+cards+'</div>'
    +wbDisclosure('高级：fingerprint / source lineage',
      '<div class="scroll-list route-list">'+fingerprintRows+'</div>'
      +'<div class="parameter-note">'+escapeHtml(JSON.stringify(model.provenance))+'</div>')
    +wbDisclosure('局限（limitations）',limitationRows||'<small>—</small>')
    +'<div class="button-row"><button class="secondary" id="evaluateRouteSafetyEvidenceV2">评价 Route Safety Evidence</button></div>',
    '四个 domain 分开报告，前端绝不合成 overall safety score、排名或自动安全等级。');
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
    '只映射现有 status，不在此重新计算任何结论。');
  return groups+overall;
}

// ---- 报告与交付 -------------------------------------------------------------
function reportPanel(flow){
  const reports=flow.cns_planning_reports||{},active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id);
  const confirmed=flow.confirmed_cns_plan||{},hasPlan=['confirmed','applied'].includes(confirmed.status),hasReport=Boolean(active);
  const stale=active?.current_applicability==='stale_current_project';
  return reviewBlock('CNS规划方案报告',
    kvRow('方案状态',statusBadge(confirmed.status||'not_confirmed'))
    +kvRow('报告状态',statusBadge(active?.current_applicability||reports.status||'not_calculated'))
    +kvRow('报告ID',active?escapeHtml(active.report_id):'尚未生成')
    +kvRow('生成时间',active?escapeHtml(active.generated_at||'—'):'—')
    +(stale?'<p class="inline-error">该报告对应旧项目状态，可继续下载，但不代表当前项目。请重新确认方案并生成新报告。</p>':'')
    +(!hasPlan?'<p class="empty">请先在方案审查中确认一个规划方案；当前只能预览草稿，不能生成正式报告。</p>':'')
    +'<div class="button-row"><button class="secondary" id="previewPlanningReport">预览报告</button><button class="primary" id="generatePlanningReport" data-report-gate="'+(hasPlan?'ready':'blocked')+'" '+(!hasPlan?'disabled':'')+'>生成正式报告</button></div>'
    +'<div class="button-row"><button class="secondary" id="downloadReportHtml" '+(!hasReport?'disabled':'')+'>下载HTML</button><button class="secondary" id="downloadReportPdf" '+(!hasReport?'disabled':'')+'>下载PDF</button></div>'
    +'<button class="secondary full" id="downloadReportPackage" '+(!hasReport?'disabled':'')+'>下载规划数据包</button>',
    'PDF使用与HTML完全相同的冻结ReportDataModel和页面；如提示PDF能力缺失，请执行 python -m playwright install chromium 后重试。');
}
function deliveryPanel(flow){
  const reports=flow.cns_planning_reports||{},active=(reports.records||[]).find(item=>item.report_id===reports.active_report_id);
  const stale=active?.current_applicability==='stale_current_project';
  return reviewBlock('导出与保存',
    (stale?'<p class="inline-error">当前报告对应旧项目状态：仍可下载，系统不会自动覆盖或删除旧报告。</p>':'')
    +'<div class="button-row export-row"><a class="secondary button-link" download="project.json" href="/api/export/project">项目JSON</a><a class="secondary button-link" download="routes.geojson" href="/api/export/routes">航路GeoJSON</a><a class="secondary button-link" download="sites.geojson" href="/api/export/sites">兼容站点GeoJSON</a></div>'
    +'<button class="primary full" id="saveAll">保存当前项目</button>',
    '导出只读取当前项目状态；报告与项目状态各自独立，不会互相覆盖。');
}
// ---- 高级：需求依据 / 布站提案证据 ------------------------------------------
function requirementPanel(flow){
  const recommendation=flow.required_cns_recommendation||{},adoption=flow.required_cns_adoption||{};
  return '<div class="review-block"><b>需求依据与来源追溯（Requirement basis / provenance）</b>'
    +'<span>模型：'+escapeHtml((recommendation.algorithm_id||flow.algorithm_selection?.requirement_model?.algorithm_id||'manual_required_cns_v1')+'@'+(recommendation.algorithm_version||flow.algorithm_selection?.requirement_model?.version||'1.0'))+'</span>'
    +'<span>需求建议：'+statusText(recommendation.status||'not_calculated')+' · 采用状态：'+statusText(adoption.status||'not_adopted')+'</span>'
    +'<span>匹配规则：'+(recommendation.matched_policies||[]).length+' · 来源字段：'+Object.keys(recommendation.field_provenance||{}).length+'</span>'
    +'<small>需求与能力/服务分离；不代表自动法规合规。</small></div>';
}
function proposalPanel(flow){
  const proposal=flow.cns_corridor_site_plan||{};
  return '<div class="review-block"><b>P16 走廊站址规划提案（Corridor Site Plan Proposal）</b>'
    +'<span>状态：'+statusText(proposal.status||'not_calculated')+'</span>'
    +'<span>目标体素：'+(proposal.target_voxel_count||0)+' · 已选动作：'+(proposal.selected_actions||[]).length+'</span>'
    +'<span>确认需求单位体积收益：'+(Number.isFinite(proposal.confirmed_requirement_unit_volume_gain)?proposal.confirmed_requirement_unit_volume_gain.toFixed(1)+' m³·unit':'—')+'</span>'
    +'<small>Proposal only；本身不修改 Existing CNS，P18 负责人工比较、确认与受控 Apply。</small></div>';
}

// ---- 渲染 -------------------------------------------------------------------
export function render({state,flow}){
  const review=flow.cns_plan_review||{},confirmed=flow.confirmed_cns_plan||{},summary=planReviewSummary(review,confirmed);
  const selected=(review.variants||[]).find(item=>item.variant_id===review.selected_variant_id)||null;
  const OPERATE=REVIEW_SEGMENTS.operate,RESULT=REVIEW_SEGMENTS.result,ADVANCED=REVIEW_SEGMENTS.advanced;

  const body=wbPanel('operate','',{segments:[
      ['review-op-overview','评审概览',wbBlock('评审概览',wbSegHint(OPERATE,'review-op-overview')+projectOverview(flow,state)+reviewStatusPanel(flow))],
      ['review-op-compare','方案比较',wbBlock('方案比较',wbSegHint(OPERATE,'review-op-compare')+comparisonPanel(review,selected))],
      ['review-op-edit','方案编辑',wbBlock('方案编辑',wbSegHint(OPERATE,'review-op-edit')+editPanel(selected))],
      ['review-op-confirm','确认与应用',wbBlock('确认与应用',wbSegHint(OPERATE,'review-op-confirm')+confirmPanel(confirmed,selected,summary))]
    ]})
    +wbPanel('result','',{segments:[
      ['review-res-status','状态总览',wbBlock('状态总览',wbSegHint(RESULT,'review-res-status')+statusOverview(flow)+routeSafetyEvidencePanel(flow))],
      ['review-res-report','报告与交付',wbBlock('报告与交付',wbSegHint(RESULT,'review-res-report')+reportPanel(flow)+deliveryPanel(flow))]
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
  // 显式评价：不存在任何自动后台评价，前端只提交一次 POST 并转印后端结果。
  c.actionButton('evaluateRouteSafetyEvidenceV2',()=>c.resourceAction(
    '/api/route-safety-evidence-v2/evaluate',{}));
}
