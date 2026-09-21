import {escapeHtml,statusBadge,wbBlock,wbDisclosure} from './common.js';
import {
  LAYERED_CANDIDATE_LABEL,THETA_STAR_V2_ALGORITHM_ID,THETA_STAR_V2_ALGORITHM_VERSION,
  THETA_STAR_V2_BLOCKED_NOTE,layeredAlgorithmId,
  layeredAlgorithmVersion,layeredPlannerAlgorithmLabel,layeredPlannerRoleLabel,
  layeredFeasibilityModel,layeredFeasibilityPayloadFrom,layeredPlanningRequestModel,
  layeredRequestPayloadFrom,renderLayeredFeasibilityFields,renderLayeredPlanningRequestFields,
} from './layered_route_planner.js';

// =========================================================
// Layered Risk-Aware Theta* V2 前端（操作 → 分层候选，仅当
// flow.layered_route_planner_readiness.algorithm.algorithm_id ===
// 'layered_risk_aware_theta_star_v2' 时渲染）。
//
// 第一视觉层（严格按此顺序，全部只做"转印"，前端不重算 objective / 风险 / 距离）：
//   ① planning request / scenario / 显式 AltitudeLayer
//   ② terrain / building feasibility
//   ③ Population × Shelter
//   ④ Objective（J = wr*E_risk + wt*C_turn + wd*L）
//   ⑤ Route Risk Density evaluation（candidate 评价约束，不是第四个 objective term）
//   ⑥ Theta* V2 candidate 结果
//
// 业务边界：
//   * Theta* V2 candidate ≠ operational route：不写入 operational_routes / CNS，不自动 adopt；
//   * RouteRiskProfile 是独立的 post-hoc 多域分析，它的 ground / air_traffic /
//     environment_obstacle 与 Theta* objective 的 population × shelter risk 不是同一件事；
//   * legacy LayeredRouteCostPolicy 的 λ 只属于 V1 A*：在这里绝不作 blocker，也不提示"必须补齐 λ"；
//   * objective 权重不在前端静默归一化：前端原样提交，由后端校验（sum≠1 后端直接拒绝）；
//   * missing 绝不当 0。
// =========================================================
export const THETA_STAR_V2_PANEL_TITLE='Layered Risk-Aware Theta* V2';
//: 视图选择依据的常量从这里也可取到（唯一定义仍在 layered_route_planner.js）。
export {THETA_STAR_V2_ALGORITHM_ID,THETA_STAR_V2_ALGORITHM_VERSION,THETA_STAR_V2_BLOCKED_NOTE};
export const THETA_V2_OBJECTIVE_FORMULA='J = wr*E_risk + wt*C_turn + wd*L';
export const THETA_V2_RISK_INDEX_DEFINITION='risk_index = normalized population factor × shelter coefficient';
export const THETA_V2_RAW_EXPOSURE_DEFINITION='raw_exposure = population_density_people_km2 × shelter_coefficient';
export const THETA_V2_RISK_DENSITY_DEFINITION='route_risk_density = risk_exposure_index_m / distance_m';
export const THETA_V2_RISK_DENSITY_ROLE='candidate 评价约束（acceptance constraint）：不是第四个 objective term，'
  +'不改变 objective weights，也不重复计风险（risk exposure 已经在 objective 里）。';
export const THETA_V2_CANDIDATE_SCOPE='Theta* V2 candidate ≠ operational route：不写入 operational_routes / CNS，'
  +'不自动 adopt，仍需要后续连续验证与显式采用流程。';
export const THETA_V2_PROFILE_SEPARATION='RouteRiskProfile 是独立的 post-hoc 多域分析：它的 ground / air_traffic / '
  +'environment_obstacle 与 Theta* objective 里的 population × shelter risk 不是同一件事，两者数值不可互相代替。';
export const THETA_V2_ALTITUDE_NOTE='Theta* V2 固定 z(x, y) = H：H 必须能解析为 confirmed EGM2008 巡航高度；'
  +'缺失时保持 blocked，不做 datum/geoid 猜测或伪转换。';
export const THETA_V2_EVALUATE_BLOCKED_NOTE='Theta* V2 仍有 blocking 项：请先补齐 shelter policy、'
  +'population_shelter 场与 confirmed 巡航高度。legacy LayeredRouteCostPolicy 的 λ 不是 Theta* V2 的 blocker。';
//: 搜索参数（heading / theta）的语义边界。8 与 5.0 是软件算法 baseline，不是工程确认参数。
export const THETA_V2_SEARCH_PARAMETER_BASELINE_SOURCE='cns_planner_software_algorithm_baseline';
export const THETA_V2_SEARCH_PARAMETER_PURPOSE='search_discretization_and_planning_turn_smoothness_proxy';
export const THETA_V2_SEARCH_PARAMETER_BOUNDARY='heading_bin_count / theta_min_deg 只是本项目的软件算法 baseline：'
  +'它们决定搜索离散与规划转向平滑度代理，不是经工程确认的航空参数。theta_min_deg 不等于航空器最小转弯角，'
  +'D_ref 也不等于航空器转弯半径；未同时提供明确 evidence 与 confirmed 时 engineering_confirmed 一律为 false。';
//: 视图选择依据的常量从这里也可取到（唯一定义仍在 layered_route_planner.js）。
//: legacy V1 A* 的 cost policy blocker：在 Theta* V2 视图里不得作为 blocker。
const LEGACY_V1_BLOCKER_CODES=new Set([
  'cost_policy_not_confirmed','cost_weights_not_configured','layered_route_cost_policy_not_confirmed',
  'layered_route_cost_policy_not_runnable',
]);

const finite=value=>Number.isFinite(value);
const text=value=>String(value??'');
const short=value=>{const value_=text(value);return value_||'—';};
const number=value=>finite(value)?Number(value):null;
const fmtNumber=(value,digits=3)=>finite(value)?Number(value).toFixed(digits):'—';

/** legacy λ blocker 判定：只按明确的 reason_code / 文案识别，不做风险推断。 */
export function isLegacyV1CostBlocker(blocker){
  const code=text(blocker?.reason_code);
  if(LEGACY_V1_BLOCKER_CODES.has(code))return true;
  const reason=text(blocker?.reason);
  return reason.includes('LayeredRouteCostPolicy')||reason.includes('λ');
}

function normalizeBlocker(blocker){
  return {
    reasonCode:text(blocker?.reason_code)||text(blocker?.reason)||'blocked',
    reason:text(blocker?.reason),
  };
}

function mergeBlockers(primary,secondary){
  const seen=new Set(),out=[];
  for(const blocker of [...(primary||[]),...(secondary||[])]){
    if(!blocker)continue;
    const normalized=normalizeBlocker(blocker);
    if(seen.has(normalized.reasonCode))continue;
    seen.add(normalized.reasonCode);
    out.push(normalized);
  }
  return out;
}

/** 表单值读取：空串保持"空"，前端绝不补默认值。 */
function fieldValue(node){
  if(!node)return '';
  const direct=node.value;
  if(direct!==undefined&&direct!==null)return String(direct);
  const attributes=node.attributes||{};
  const attribute=attributes.value;
  return attribute===undefined||attribute===null?'':String(attribute);
}

function checkedFrom(node){
  if(!node)return false;
  if(node.checked===true||node.checked==='true')return true;
  const attributes=node.attributes||{};
  return attributes.checked===true||attributes.checked==='true';
}

function numberFromField(node){
  const raw=fieldValue(node).trim();
  // 空值一律提交 null：null ≠ 0。
  return raw===''?null:Number(raw);
}

/** 输入框展示值：只在后端给了明确数值时预填，绝不由前端造默认。 */
function inputValue(value){
  return finite(value)?escapeHtml(String(value)):'';
}

// ---------------------------------------------------------------- model

/** Theta* V2 的只读投影：全部字段来自后端 readiness / policies / candidate，前端不重算。 */
export function layeredThetaV2Model(flow){
  const readiness=flow?.layered_route_planner_readiness||{};
  const theta=readiness.theta_star_v2||{};
  const request=layeredPlanningRequestModel(flow);
  const feasibility=layeredFeasibilityModel(flow);

  const shelterPolicy=flow?.shelter_coefficient_policy||{};
  const shelterField=flow?.population_shelter||{};
  const objectivePolicy=flow?.theta_v2_objective_policy||{};
  const densityPolicy=flow?.max_route_risk_density||{};
  const regulatoryField=flow?.regulatory_constraints||{};
  const communicationField=flow?.communication_planning_field||{};

  const shelterCells=shelterField.cells||{};
  const shelterCellIds=Object.keys(shelterCells);
  const unresolvedCellIds=shelterCellIds.filter(gridId=>(shelterCells[gridId]||{}).status!=='passed');
  const perGridOverrides=shelterPolicy.per_grid_overrides&&typeof shelterPolicy.per_grid_overrides==='object'
    ?shelterPolicy.per_grid_overrides:{};
  const policySource=text(shelterPolicy.source);
  const shelterPolicyModel={
    status:text(shelterPolicy.status||'not_configured'),
    statusReason:text(shelterPolicy.status_reason),
    parameterStatus:text(shelterPolicy.parameter_status),
    defaultCoefficient:number(shelterPolicy.default_coefficient),
    source:policySource,
    // 未配置时的后端占位说明不作为可编辑值回填。
    sourceInput:policySource.startsWith('未配置')?'':policySource,
    provenance:text(shelterPolicy.provenance),
    confirmed:shelterPolicy.confirmed===true,
    perGridOverrides,
    perGridOverrideCount:Object.keys(perGridOverrides).length,
    unit:text(shelterPolicy.unit),
    range:Array.isArray(shelterPolicy.range)?shelterPolicy.range:[0,1],
  };

  const thetaShelter=theta.population_shelter||{};
  const shelterModel={
    policy:shelterPolicyModel,
    field:{
      status:text(shelterField.status||'not_calculated'),
      attribute:text(shelterField.attribute||'population_shelter'),
      namespace:text(shelterField.namespace),
      gridLevel:finite(shelterField.grid_level)?Number(shelterField.grid_level):null,
      source:text(shelterField.source),
      count:finite(shelterField.count)?Number(shelterField.count):shelterCellIds.length,
      coveredCount:finite(shelterField.covered_count)?Number(shelterField.covered_count):null,
      cellCount:shelterCellIds.length,
      resolvedCellCount:shelterCellIds.length-unresolvedCellIds.length,
      unresolvedCellIds,
      fieldFingerprint:text(shelterField.field_fingerprint||thetaShelter.field_fingerprint),
      policyFingerprint:text(shelterField.shelter_coefficient_policy_fingerprint),
      riskIndexDefinition:text(thetaShelter.risk_index_definition||THETA_V2_RISK_INDEX_DEFINITION),
      rawExposureDefinition:text(thetaShelter.raw_exposure_definition||THETA_V2_RAW_EXPOSURE_DEFINITION),
      riskIndexRange:Array.isArray(thetaShelter.risk_index_range)?thetaShelter.risk_index_range:[0,1],
      semantics:shelterField.semantics||{},
    },
    readiness:{
      status:text(thetaShelter.status||'not_calculated'),
      cellCount:finite(thetaShelter.cell_count)?Number(thetaShelter.cell_count):null,
      resolvedCellCount:finite(thetaShelter.resolved_cell_count)?Number(thetaShelter.resolved_cell_count):null,
    },
  };

  const weights={
    risk:number(objectivePolicy.risk_weight),
    turn:number(objectivePolicy.turn_weight),
    distance:number(objectivePolicy.distance_weight),
  };
  const weightsComplete=Object.values(weights).every(value=>value!==null);
  const weightsSum=weightsComplete?weights.risk+weights.turn+weights.distance:null;
  const sumTolerance=number(objectivePolicy.sum_tolerance);
  const objectiveModel={
    status:text(objectivePolicy.status||'not_configured'),
    statusReason:text(objectivePolicy.status_reason),
    riskWeight:weights.risk,
    turnWeight:weights.turn,
    distanceWeight:weights.distance,
    weightsComplete,
    weightsSum,
    sumTolerance,
    sumOk:weightsSum===null?null:Math.abs(weightsSum-1)<=(sumTolerance===null?1e-9:sumTolerance),
    source:text(objectivePolicy.source),
    provenance:text(objectivePolicy.provenance),
    confirmed:objectivePolicy.confirmed===true,
    baseline:objectivePolicy.algorithm_policy_baseline===true,
    weightsEditable:objectivePolicy.weights_are_editable!==false,
    formula:text(objectivePolicy.formula||THETA_V2_OBJECTIVE_FORMULA),
    readiness:theta.objective||{},
  };

  const densityReadiness=theta.evaluation_constraint||{};
  const densityModel={
    constraintId:text(densityPolicy.constraint_id||'max_route_risk_density'),
    metric:text(densityPolicy.metric||'route_risk_density'),
    threshold:number(densityPolicy.threshold),
    comparison:text(densityPolicy.comparison||'less_than_or_equal'),
    unit:text(densityPolicy.unit),
    source:text(densityPolicy.source),
    provenance:text(densityPolicy.provenance),
    confirmed:densityPolicy.confirmed===true,
    temporary:densityPolicy.temporary===true,
    status:text(densityPolicy.status||'not_configured'),
    role:text(densityPolicy.role||'candidate_evaluation_acceptance_constraint'),
    objectiveTerm:densityPolicy.objective_term===true,
    changesObjectiveWeights:densityPolicy.changes_objective_weights===true,
    definition:THETA_V2_RISK_DENSITY_DEFINITION,
    readiness:{
      metric:text(densityReadiness.metric),
      threshold:number(densityReadiness.threshold),
      source:text(densityReadiness.source),
      temporary:densityReadiness.temporary===true,
      role:text(densityReadiness.role),
      objectiveTerm:densityReadiness.objective_term===true,
    },
  };

  const regulatoryRecord=theta.regulatory_constraints||{};
  const regulatoryModel={
    status:text(regulatoryRecord.status||regulatoryField.status||'not_configured'),
    configured:regulatoryRecord.configured===true,
    compliance:text(regulatoryRecord.regulatory_compliance),
    constraintDatasetStatus:text(regulatoryRecord.constraint_dataset_status||regulatoryField.status),
    constraintCount:finite(regulatoryRecord.constraint_count)?Number(regulatoryRecord.constraint_count):null,
    confirmedConstraintCount:finite(regulatoryRecord.confirmed_constraint_count)
      ?Number(regulatoryRecord.confirmed_constraint_count):null,
    datasetFingerprint:text(regulatoryRecord.dataset_fingerprint||regulatoryField.dataset_fingerprint),
    statement:text(regulatoryRecord.statement),
    semantics:regulatoryRecord.semantics||{},
  };

  const communicationRecord=theta.communication||{};
  const communicationModel={
    interface:text(communicationRecord.interface||'communication_planning_field'),
    status:text(communicationRecord.status||communicationField.status||'not_configured'),
    provider:text(communicationRecord.provider||communicationField.provider),
    source:text(communicationRecord.source||communicationField.source),
    cellCount:finite(communicationRecord.cell_count)?Number(communicationRecord.cell_count):null,
    readiness:text(communicationRecord.readiness),
    informationalFingerprint:text(communicationRecord.informational_fingerprint),
    usedInCost:communicationRecord.used_in_cost===true,
    usedAsConstraint:communicationRecord.used_as_constraint===true,
    affectedPathOrCost:communicationRecord.affected_path_or_cost===true,
    semantics:communicationRecord.semantics||{},
  };

  const searchReadiness=theta.search_parameters||{};
  const searchProvenance=searchReadiness.provenance||{};
  const searchSource=text(searchProvenance.source);
  const searchParameterModel={
    applicable:searchReadiness.applicable!==false,
    reason:text(searchReadiness.reason),
    headingBinCount:number(searchReadiness.heading_bin_count),
    thetaMinDeg:number(searchReadiness.theta_min_deg),
    maxExpandedLabels:finite(searchReadiness.max_expanded_labels)
      ?Number(searchReadiness.max_expanded_labels):null,
    dRefM:number(searchReadiness.d_ref_m),
    dRefProvenance:text(searchReadiness.d_ref_provenance),
    parameterOrigin:text(searchReadiness.parameter_origin||searchProvenance.parameter_origin),
    engineeringConfirmed:searchReadiness.engineering_confirmed===true,
    softwareAlgorithmBaseline:searchReadiness.software_algorithm_baseline===true,
    source:searchSource||THETA_V2_SEARCH_PARAMETER_BASELINE_SOURCE,
    // 软件 baseline 的来源标识不是工程来源：不作为可编辑文本回填。
    sourceInput:searchSource===THETA_V2_SEARCH_PARAMETER_BASELINE_SOURCE?'':searchSource,
    purpose:text(searchProvenance.purpose||THETA_V2_SEARCH_PARAMETER_PURPOSE),
    evidence:searchProvenance.evidence||null,
    evidenceReference:searchProvenance.evidence&&searchProvenance.evidence.reference
      ?text(searchProvenance.evidence.reference):'',
    confirmed:searchProvenance.confirmed===true,
    fingerprint:text(searchReadiness.fingerprint),
    semantics:searchProvenance.semantics||{},
    boundary:searchProvenance.engineering_boundary||{},
  };

  const candidates=flow?.layered_route_candidates||{};
  const items=Array.isArray(candidates.items)?candidates.items:[];
  const current=items.find(item=>item&&item.current_applicability==='current')||null;
  const last=items[items.length-1]||null;
  const shown=current||last;

  const readinessBlockers=Array.isArray(readiness.blockers)?readiness.blockers:[];
  const legacyV1Blockers=readinessBlockers.filter(isLegacyV1CostBlocker);
  const thetaBlockers=mergeBlockers(
    readinessBlockers.filter(item=>!isLegacyV1CostBlocker(item)),
    Array.isArray(theta.blockers)?theta.blockers:[],
  );

  return {
    algorithmId:layeredAlgorithmId(flow),
    algorithmVersion:layeredAlgorithmVersion(flow),
    algorithmLabel:layeredPlannerAlgorithmLabel(flow),
    plannerRole:layeredPlannerRoleLabel(flow),
    expectedAlgorithmId:THETA_STAR_V2_ALGORITHM_ID,
    expectedAlgorithmVersion:THETA_STAR_V2_ALGORITHM_VERSION,
    status:text(readiness.status||'blocked'),
    thetaStatus:text(theta.status||'not_ready'),
    semantics:theta.semantics||{},
    request,
    feasibility,
    searchParameters:searchParameterModel,
    shelter:shelterModel,
    objective:objectiveModel,
    riskDensity:densityModel,
    regulatory:regulatoryModel,
    communication:communicationModel,
    blockers:{
      // Theta* V2 的 blocker 只包含后端 readiness 的非 legacy 项与 theta_star_v2 自己的项。
      thetaV2:thetaBlockers,
      // legacy λ 原样保留显示，但明确"不是 Theta* V2 的 blocker"。
      legacyV1:legacyV1Blockers.map(normalizeBlocker),
      all:readinessBlockers.map(normalizeBlocker),
    },
    candidates:{
      status:text(candidates.status||'not_calculated'),
      count:items.length,
      currentFingerprint:text(candidates.current_candidate_fingerprint),
      currentKey:text(candidates.current_key),
      current:current?candidateModel(current,true):null,
      shown:shown?candidateModel(shown,current===shown):null,
    },
    boundary:{
      candidateScope:THETA_V2_CANDIDATE_SCOPE,
      profileSeparation:THETA_V2_PROFILE_SEPARATION,
      profileSeparationConfirmed:'RouteRiskProfile 是独立 post-hoc 分析，不是 Theta* V2 objective 的一部分。',
    },
  };
}

/** candidate 投影：objective / density / turn / search / LOS 全部原样转印后端字段。 */
export function candidateModel(candidate,isCurrent){
  const objective=candidate?.planning_objective||{};
  const density=candidate?.route_risk_density||{};
  const turns=candidate?.turn_statistics||{};
  const search=candidate?.search_statistics||candidate?.statistics||{};
  const losSegments=Array.isArray(candidate?.los_segments)?candidate.los_segments:[];
  return {
    candidateId:text(candidate?.candidate_id),
    status:text(candidate?.status),
    applicability:text(candidate?.current_applicability),
    isCurrent:isCurrent===true,
    routeId:text(candidate?.route_id),
    layerId:text(candidate?.altitude_layer_id),
    algorithmId:text(candidate?.algorithm_id),
    algorithmVersion:text(candidate?.algorithm_version),
    distanceM:number(candidate?.distance_m),
    optimizationCost:number(candidate?.optimization_cost),
    cellCount:Array.isArray(candidate?.grid_path)?candidate.grid_path.length:0,
    objective:{
      riskExposureIndexM:number(objective.risk_exposure_index_m),
      turnCount:number(objective.turn_count),
      totalHeadingChangeDeg:number(objective.total_heading_change_deg),
      turnCostM:number(objective.turn_cost_m),
      distanceM:number(objective.distance_m),
      riskWeight:number(objective.risk_weight),
      turnWeight:number(objective.turn_weight),
      distanceWeight:number(objective.distance_weight),
      weightedRisk:number(objective.weighted_risk),
      weightedTurn:number(objective.weighted_turn),
      weightedDistance:number(objective.weighted_distance),
      totalCost:number(objective.total_cost),
      formula:text(objective.formula||THETA_V2_OBJECTIVE_FORMULA),
      policyFingerprint:text(objective.policy_fingerprint),
      weightsProvenance:text(objective.weights_provenance),
      objectivePopulationShelterOnly:objective.objective_population_shelter_only===true,
      riskV2OverallUsed:objective.risk_v2_overall_used===true,
      routeRiskDensityIsNotAnObjectiveTerm:objective.route_risk_density_is_not_an_objective_term===true,
      dRefM:number(objective.d_ref_m),
      thetaMinDeg:number(objective.theta_min_deg),
      provenance:objective.provenance||{},
    },
    routeRiskDensity:{
      metric:text(density.metric||'route_risk_density'),
      definition:text(density.definition||THETA_V2_RISK_DENSITY_DEFINITION),
      value:number(density.value),
      threshold:number(density.threshold),
      margin:number(density.margin),
      status:text(density.status||'unresolved'),
      reason:text(density.reason),
      comparison:text(density.comparison),
      unit:text(density.unit),
      source:text(density.source),
      temporaryConstraint:density.temporary_constraint===true,
      confirmed:density.confirmed===true,
      riskExposureIndexM:number(density.risk_exposure_index_m),
      distanceM:number(density.distance_m),
      objectiveTerm:density.objective_term===true,
      changesObjectiveWeights:density.changes_objective_weights===true,
      semantics:density.semantics||{},
    },
    turnStatistics:{
      turnCount:number(turns.turn_count),
      totalHeadingChangeDeg:number(turns.total_heading_change_deg),
      turnCostM:number(turns.turn_cost_m),
      thetaMinDeg:number(turns.theta_min_deg),
      dRefM:number(turns.d_ref_m),
      semantics:text(turns.semantics),
      turns:Array.isArray(turns.turns)?turns.turns:[],
    },
    searchStatistics:search||{},
    los:{
      segmentCount:losSegments.length,
      segments:losSegments,
      crossedCellCount:losSegments.reduce((total,segment)=>
        total+(Array.isArray(segment?.traversed_cells)?segment.traversed_cells.length:0),0),
    },
    blockingReasons:Array.isArray(candidate?.blocking_reasons)?candidate.blocking_reasons:[],
    searchIncomplete:candidate?.search_incomplete===true,
    // Terminal status projection (Phase 3.5): ``no_path`` (搜索完整但无解) and
    // ``search_incomplete`` (搜索预算耗尽，可达性未证明) must never be shown as the same thing.
    terminalStatus:text(candidate?.terminal_status||candidate?.status),
    terminalStatusSemantics:text(candidate?.terminal_status_semantics),
    resourceLimited:(candidate?.terminal_status||candidate?.status)==='search_incomplete',
    reachabilityProven:candidate?.blocking_reasons?.[0]?.reachability_proven===true,
    resourceLimit:text(candidate?.blocking_reasons?.[0]?.resource_limit),
    maskStatus:text(candidate?.mask_status),
    fingerprints:{
      candidate:text(candidate?.candidate_fingerprint),
      feasibility:text(candidate?.feasibility_fingerprint),
      risk:text(candidate?.risk_fingerprint),
      policy:text(candidate?.policy_fingerprint),
      request:text(candidate?.request_fingerprint),
      input:text(candidate?.input_fingerprint),
      feasibilityMask:text(candidate?.feasibility_mask_fingerprint),
      communicationInformational:text(candidate?.communication_informational_fingerprint),
    },
    costBreakdown:candidate?.cost_breakdown||{},
    provenance:candidate?.provenance||{},
  };
}

// ---------------------------------------------------------------- HTML helpers

function row(label,value,note=''){
  return '<div class="list-row"><span><b>'+escapeHtml(label)+'</b><small>'
    +escapeHtml(value)+'</small>'+(note?'<small>'+note+'</small>':'')+'</span></div>';
}

function fieldRow(key,label,value,note=''){
  return '<div class="list-row" data-theta-v2-candidate-field="'+escapeHtml(key)+'"><span><b>'
    +escapeHtml(label)+'</b><small>'+escapeHtml(value)+'</small>'
    +(note?'<small>'+note+'</small>':'')+'</span></div>';
}

function listRows(rows){
  if(!rows.length)return '<div class="empty-note">没有可显示的条目。</div>';
  return '<div class="scroll-list route-list">'+rows.join('')+'</div>';
}

function blockerRows(blockers,emptyNote){
  if(!blockers.length)return '<div class="parameter-note">'+escapeHtml(emptyNote)+'</div>';
  return listRows(blockers.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.reasonCode)
    +'</b> '+statusBadge(item.terminalStatus==='search_incomplete'?'search_incomplete':(item.terminalStatus||'blocked'))
    +'<small>'+escapeHtml(item.reason)+'</small>'
    +(item.resourceLimit?'<small>resource_limit='+escapeHtml(String(item.resourceLimit))
      +' · reachability_proven='+escapeHtml(String(item.reachabilityProven===true))+'</small>':'')
    +'</span></div>'));
}

// ---------------------------------------------------------------- sections

function readinessSection(model){
  const thetaBlockers=blockerRows(model.blockers.thetaV2,
    'Theta* V2 readiness 没有 blocking 项。');
  const legacyRows=model.blockers.legacyV1.length
    ?'<h3>legacy V1 λ blocker（<b>不是</b> Theta* V2 的 blocker）</h3>'
      +'<div class="parameter-note">下面这些 blocker 来自 legacy LayeredRouteCostPolicy（V1 A* 的 λ）；'
      +'Theta* V2 的搜索代价是 population × shelter risk，因此这些项既不禁用运行按钮，也不参与 Theta* V2 判断。'
      +'后端 readiness.blockers 的原值仍然完整保留在这里。</div>'
      +blockerRows(model.blockers.legacyV1,'没有 legacy λ blocker。')
    :'';
  return wbBlock(THETA_STAR_V2_PANEL_TITLE,
    '<div class="parameter-note">algorithm_id <code>'+escapeHtml(short(model.algorithmId))+'</code>'
    +'@'+escapeHtml(short(model.algorithmVersion||model.expectedAlgorithmVersion))
    +' · theta_star_v2 readiness '+statusBadge(model.thetaStatus)+'。'
    +'视图完全由 <code>layered_route_planner_readiness.algorithm.algorithm_id</code> 决定：'
    +'只有 <code>'+escapeHtml(THETA_STAR_V2_ALGORITHM_ID)+'</code> 才显示本面板。</div>'
    +'<div class="parameter-note">'+escapeHtml(THETA_STAR_V2_BLOCKED_NOTE)+'</div>'
    +'<div class="scroll-list route-list">'
    +row('readiness status',model.status+'（后端原值）')
    +row('layered planner',short(model.algorithmLabel)+' · '+short(model.plannerRole))
    +row('artifact_type','layered_route_candidate · 只分析候选，不写运行航路')
    +row('algorithm',short(model.algorithmId)+'@'+short(model.algorithmVersion||model.expectedAlgorithmVersion))
    +'</div>'
    +'<h3>Theta* V2 blockers（后端原样转印）</h3>'+thetaBlockers
    +legacyRows
    +'<h3>candidate 集合</h3>'
    +'<div class="scroll-list route-list">'
    +row('candidates status',model.candidates.status+' · 共 '+model.candidates.count+' 条')
    +row('current_applicability',model.candidates.shown
      ?(model.candidates.shown.applicability
        +(model.candidates.shown.isCurrent?' · 这是 current candidate':' · 非 current，仅作证据'))
      :'—')
    +'</div>',
    model.blockers.thetaV2.length?statusBadge('blocked'):statusBadge(model.thetaStatus));
}

/**
 * 搜索参数（heading_bin_count / theta_min_deg / max_expanded_labels）与 provenance。
 *
 * 两个数值不再是 planner 内部不可见常数：这里显示 effective value、parameter_origin 与
 * engineering_confirmed，并允许通过现有 ``/api/algorithms/select`` 显式改写。前端只转印
 * 后端字段，不推导结论，也不把软件 baseline 说成工程确认参数。
 */
function searchParameterSection(model){
  const p=model.searchParameters;
  if(!p.applicable){
    return wbBlock('①b Theta* 搜索参数（heading / theta）',
      '<div class="parameter-note">当前 layered planner 不是 Theta* V2：'
      +escapeHtml(p.reason||'legacy_layered_planner_has_no_theta_search_parameters')
      +'。后端明确报告 applicable=false，前端不为 legacy planner 发明搜索参数。</div>');
  }
  return wbBlock('①b Theta* 搜索参数（heading / theta）',
    '<div class="parameter-note">'+escapeHtml(THETA_V2_SEARCH_PARAMETER_BOUNDARY)+'</div>'
    +'<div class="scroll-list route-list">'
    +row('heading_bin_count',short(p.headingBinCount)+'（生效值）')
    +row('theta_min_deg',short(p.thetaMinDeg)+'（生效值 · 规划转向平滑度代理阈值）')
    +row('max_expanded_labels',p.maxExpandedLabels===null?'null（不设上限）':short(p.maxExpandedLabels))
    +row('parameter_origin',short(p.parameterOrigin))
    +row('engineering_confirmed',p.engineeringConfirmed?'true':'false')
    +row('search parameter fingerprint',short(p.fingerprint))
    +row('d_ref_m / provenance',short(p.dRefM)+' · '+short(p.dRefProvenance))
    +row('source',short(p.source))
    +row('purpose',short(p.purpose))
    +row('evidence',p.evidence?short(JSON.stringify(p.evidence)):'null（未提供工程证据）')
    +'</div>'
    +'<h3>显式改写搜索参数（/api/algorithms/select）</h3>'
    +'<div class="form-grid">'
    +'<label>heading_bin_count（≥4 且整除 360）'
    +'<input class="panel-input" id="thetaV2HeadingBinCount" type="number" step="1" '
    +'value="'+inputValue(p.headingBinCount)+'"></label>'
    +'<label>theta_min_deg（0..180）'
    +'<input class="panel-input" id="thetaV2ThetaMinDeg" type="number" step="any" '
    +'value="'+inputValue(p.thetaMinDeg)+'"></label>'
    +'<label>max_expanded_labels（空 = null，不设上限）'
    +'<input class="panel-input" id="thetaV2MaxExpandedLabels" type="number" step="1" '
    +'value="'+inputValue(p.maxExpandedLabels)+'"></label>'
    +'<label>source（工程来源，可空）'
    +'<input class="panel-input" id="thetaV2SearchParamSource" value="'
    +escapeHtml(p.sourceInput)+'" placeholder="例如 engineering_review"></label>'
    +'<label>evidence reference（可空）'
    +'<input class="panel-input" id="thetaV2SearchParamEvidence" value="'
    +escapeHtml(p.evidenceReference)+'" placeholder="例如 ENG-2026-001"></label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2SearchParamConfirmed"'
    +(p.confirmed?' checked':'')+'> 显式 confirmed</label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2SearchParamEngineeringConfirmed"'
    +(p.engineeringConfirmed?' checked':'')+'> 声明 engineering confirmed（需同时提供 evidence）</label>'
    +'</div>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveThetaV2SearchParameters">保存搜索参数</button>'
    +'</div>'
    +'<div class="parameter-note">改写 heading_bin_count / theta_min_deg 会把 parameter_origin 变成 '
    +'<code>explicit_algorithm_selection</code>；除非同时提供明确 evidence 并勾选 confirmed 与 '
    +'engineering_confirmed，后端仍保持 engineering_confirmed=false。'
    +'heading_bin_count / theta_min_deg / parameter_origin / engineering_confirmed 全部来自后端 readiness，'
    +'前端不推导结论。</div>');
}

function requestSection(model){
  const request=model.request;
  const scenario=request.scenarioRoutes.find(route=>route.routeId===request.selectedRouteId)||null;
  return wbBlock('① 规划请求：scenario / OD + 显式 AltitudeLayer',
    '<div class="parameter-note">pipeline：scenario/OD 航路 → <b>显式 AltitudeLayer</b> → terrain/building '
    +'coarse 可行性 mask → population × shelter risk → heading-aware multi-label Theta* （parent LOS rewiring）'
    +'→ candidate evaluation → LayeredRouteCandidate。</div>'
    +'<div class="parameter-note">'+escapeHtml(THETA_V2_CANDIDATE_SCOPE)+'</div>'
    +'<div class="scroll-list route-list">'
    +row('request status',request.requestStatus+' · confirmed '+String(request.requestConfirmed))
    +row('scenario route',(request.selectedRouteId||'—')+(scenario&&scenario.direction?' · '+scenario.direction:'')
      +' · resolve '+request.scenarioRouteStatus)
    +row('altitude layer catalog',request.layerCatalogStatus+' · 共 '+request.layers.length+' 层')
    +'</div>'
    +renderLayeredPlanningRequestFields(request,{altitudeNote:THETA_V2_ALTITUDE_NOTE})
    +'<div class="button-row">'
    +'<button class="secondary" id="saveLayeredRequest">保存 planning request</button>'
    +'<button class="primary" id="evaluateLayeredCandidate"'
    +(model.blockers.thetaV2.length?' disabled':'')+'>运行 Theta* V2 candidate（需真实数据源）</button>'
    +'</div>'
    +'<div class="parameter-note">按钮只受 Theta* V2 blocker 影响：legacy λ 不参与。'
    +'前端不重新判断候选、mask 或参数是否就绪。</div>');
}

function feasibilitySection(model){
  const feasibility=model.feasibility;
  const building=feasibility.buildingClearance||{};
  const mask=feasibility.mask||{};
  const counts=mask.counts||{};
  const sources=feasibility.sources||{};
  const terrain=sources.terrain||{};
  const population=sources.population||{};
  return wbBlock('② terrain / building feasibility',
    '<div class="parameter-note">粗粒度策略包线（<code>coarse_strategic_vertical_envelope</code>）：'
    +'不是 exact footprint，也不是精确建筑净空结论。unknown ≠ feasible ≠ blocked，unknown 绝不进入搜索，也绝不当 0。</div>'
    +'<div class="scroll-list route-list">'
    +row('terrain_vertical_clearance_m',feasibility.clearance===null
      ?'null · 无默认值，必须由工程确认':'已确认 '+fmtNumber(feasibility.clearance)+' m')
    +row('feasibility policy',feasibility.policyStatus+' · '+(feasibility.parameterStatus||'—'))
    +row('feasibility fingerprint',short(feasibility.fingerprint))
    +row('建筑垂直净空（复用既有策略）',finite(building.vertical_clearance_m)
      ?('vertical '+fmtNumber(building.vertical_clearance_m)+' m · status '+short(building.status)
        +' · 不新建第二套建筑净空定义')
      :('vertical 待确认 · status '+short(building.status)))
    +row('selected layer mask',short(mask.altitude_layer_id)+' · '+short(mask.status)
      +' · applicability '+short(mask.current_applicability))
    +row('mask counts','feasible '+((counts.feasible)||0)+' · blocked '+((counts.blocked)||0)
      +' · unknown '+((counts.unknown)||0))
    +row('mask fingerprint',short(mask.mask_fingerprint))
    +row('terrain source',(terrain.available===true?'available':'unavailable')+' · '+short(terrain.reason))
    +row('population source',(population.available===true?'available':'unavailable')+' · '+short(population.reason))
    +'</div>'
    +renderLayeredFeasibilityFields(model)
    +'<div class="button-row">'
    +'<button class="secondary" id="saveLayeredFeasibilityPolicy">保存 feasibility policy</button>'
    +'</div>');
}

function complianceSection(model){
  const regulatory=model.regulatory;
  const communication=model.communication;
  return wbBlock('②b Regulatory / Communication（只读 readiness 摘要，本轮不新建规则编辑器）',
    '<h3>Regulatory constraints</h3>'
    +'<div class="scroll-list route-list">'
    +row('regulatory status',regulatory.status+' · configured '+String(regulatory.configured))
    +row('regulatory_compliance',short(regulatory.compliance))
    +row('constraint dataset',short(regulatory.constraintDatasetStatus)+' · count '
      +short(regulatory.constraintCount)+' · confirmed '+short(regulatory.confirmedConstraintCount))
    +row('dataset fingerprint',short(regulatory.datasetFingerprint))
    +'</div>'
    +'<div class="parameter-note">'+escapeHtml(short(regulatory.statement))
    +' 未配置 = not_evaluated（绝不写成"符合禁飞要求"）。</div>'
    +'<h3>Communication planning field</h3>'
    +'<div class="scroll-list route-list">'
    +row('communication status',communication.status+' · provider '+short(communication.provider))
    +row('cell count',short(communication.cellCount)+' · readiness '+short(communication.readiness))
    +row('used_in_cost',String(communication.usedInCost)+' · used_as_constraint '
      +String(communication.usedAsConstraint)+' · affected_path_or_cost '+String(communication.affectedPathOrCost))
    +row('informational fingerprint',short(communication.informationalFingerprint))
    +'</div>'
    +'<div class="parameter-note"><b>当前版本 communication 不影响 path / cost</b>：'
    +'它只作为 informational readiness 记录在 candidate 上（used_in_cost=false、used_as_constraint=false、'
    +'affected_path_or_cost=false），不进入搜索、硬门或 fingerprint 判定。</div>');
}

function shelterSection(model){
  const shelter=model.shelter;
  const policy=shelter.policy;
  const field=shelter.field;
  return wbBlock('③ Population × Shelter（risk index 的唯一来源）',
    '<div class="parameter-note">'+escapeHtml(THETA_V2_RISK_INDEX_DEFINITION)+'：'
    +'<b>不是事故概率</b>，也不是安全结论；它是 0–1 的无量纲规划指标，用于 Theta* V2 的 risk exposure 积分。</div>'
    +'<div class="parameter-note">raw exposure 定义 <code>'+escapeHtml(field.rawExposureDefinition)+'</code>；'
    +'<code>missing ≠ 0</code>：population factor 或 shelter coefficient 缺失时该 cell 一律 unresolved，'
    +'risk weight&gt;0 时 fail-closed，绝不补 0。</div>'
    +'<div class="scroll-list route-list">'
    +row('shelter policy status',policy.status+' · '+short(policy.statusReason))
    +row('default_coefficient',policy.defaultCoefficient===null?'null（未配置）':fmtNumber(policy.defaultCoefficient,4)
      +' · 0–1 · 1.0 = 不做任何遮盖折减')
    +row('policy source',short(policy.source))
    +row('policy provenance',short(policy.provenance)+' · confirmed '+String(policy.confirmed))
    +row('per_grid_overrides',policy.perGridOverrideCount+' 条（保存时原样保留，绝不清空）')
    +row('population_shelter status',field.status+' · cell '+String(field.cellCount)
      +' · resolved '+String(field.resolvedCellCount)+' · unresolved '+String(field.unresolvedCellIds.length))
    +row('covered_count',field.coveredCount===null?'—':String(field.coveredCount))
    +row('field fingerprint',short(field.fieldFingerprint))
    +row('policy fingerprint',short(field.policyFingerprint))
    +row('readiness',shelter.readiness.status+' · cell count '+short(shelter.readiness.cellCount)
      +' · resolved '+short(shelter.readiness.resolvedCellCount))
    +'</div>'
    +(field.unresolvedCellIds.length
      ?wbDisclosure('unresolved cells（missing ≠ 0）',
        '<div class="parameter-note">'+escapeHtml(field.unresolvedCellIds.join(', '))+'</div>')
      :'')
    +'<h3>可编辑：shelter_coefficient_policy</h3>'
    +'<label>default_coefficient（0–1，可空 = 未配置）'
    +'<input class="panel-input" id="thetaV2ShelterCoefficient" type="number" min="0" max="1" step="any" '
    +'placeholder="无默认值" value="'+inputValue(policy.defaultCoefficient)+'"></label>'
    +'<label>source（工程依据）'
    +'<input class="panel-input" id="thetaV2ShelterSource" value="'+escapeHtml(policy.sourceInput)+'"></label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2ShelterConfirmed"'
    +(policy.confirmed?' checked':'')+'> shelter_coefficient_policy 已由项目工程依据确认</label>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveThetaV2ShelterPolicy">保存 shelter coefficient policy</button>'
    +'</div>'
    +'<div class="parameter-note">保存只写 <code>shelter_coefficient_policy</code>，'
    +'并把现有 <code>per_grid_overrides</code>（'+policy.perGridOverrideCount+' 条）原样回传，绝不在保存时清空；'
    +'未确认或无系数时后端保持 not_configured / pending_confirmation，不假设任何遮盖系数。</div>');
}

function objectiveSection(model){
  const objective=model.objective;
  const sumText=objective.weightsSum===null
    ?'权重未全部给出（缺项保持后端现值；后端校验为准）'
    :('权重和 = '+fmtNumber(objective.weightsSum,6)
      +(objective.sumOk?'（后端要求 sum=1）':'：与后端 sum=1 约束不一致，后端会拒绝；前端不做静默归一化'));
  return wbBlock('④ Objective：'+THETA_V2_OBJECTIVE_FORMULA,
    '<div class="parameter-note">objective 只评价 <b>population × shelter risk</b>：'
    +'<code>E_risk</code> 是 risk exposure 积分（index·m），<code>C_turn</code> 是规划平滑代价（m，经 D_ref），'
    +'<code>L</code> 是航路长度（m）。baseline 直接取后端现值，前端不另造默认值。</div>'
    +'<div class="scroll-list route-list" data-theta-v2-objective="policy">'
    +fieldRow('objective_policy_status','objective policy status',objective.status+' · '+short(objective.statusReason))
    +fieldRow('objective_provenance','provenance',short(objective.provenance)
      +' · baseline '+String(objective.baseline)+' · confirmed '+String(objective.confirmed))
    +row('formula',objective.formula)
    +row('weights',(objective.weightsComplete?sumText:'—'))
    +row('readiness objective',short(objective.readiness.formula||objective.formula)
      +' · population_shelter_only '+String(objective.readiness.objective_population_shelter_only===true))
    +'</div>'
    +'<h3>可编辑：theta_v2_objective_policy</h3>'
    +'<div class="form-grid">'
    +'<label>risk_weight<input class="panel-input" id="thetaV2RiskWeight" type="number" min="0" step="any" value="'
    +inputValue(objective.riskWeight)+'"></label>'
    +'<label>turn_weight<input class="panel-input" id="thetaV2TurnWeight" type="number" min="0" step="any" value="'
    +inputValue(objective.turnWeight)+'"></label>'
    +'<label>distance_weight<input class="panel-input" id="thetaV2DistanceWeight" type="number" min="0" step="any" value="'
    +inputValue(objective.distanceWeight)+'"></label>'
    +'</div>'
    +'<label>objective source / evidence'
    +'<input class="panel-input" id="thetaV2ObjectiveSource" value="'+escapeHtml(objective.source)+'"></label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2ObjectiveConfirmed"'
    +(objective.confirmed?' checked':'')+'> objective policy 已确认</label>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveThetaV2ObjectivePolicy">保存 objective policy</button>'
    +'</div>'
    +'<div class="parameter-note"><b>不做静默 normalize</b>：前端原样提交三个权重，'
    +'权重和≠1 时只做提示，最终以后端校验为准（后端直接拒绝，不会改写 baseline）。'
    +'<code>route_risk_density</code> 不是第四个 objective term：不改变这里的权重，也不重复计风险。</div>');
}

function riskDensitySection(model){
  const density=model.riskDensity;
  return wbBlock('⑤ Route Risk Density evaluation constraint',
    '<div class="parameter-note"><code>'+escapeHtml(THETA_V2_RISK_DENSITY_DEFINITION)+'</code>：'
    +escapeHtml(THETA_V2_RISK_DENSITY_ROLE)+'</div>'
    +'<div class="scroll-list route-list">'
    +row('constraint',density.constraintId+' · metric '+density.metric+' · '+density.comparison)
    +row('threshold',density.threshold===null?'—':fmtNumber(density.threshold,6)
      +' · unit '+short(density.unit))
    +row('source',short(density.source)+' · provenance '+short(density.provenance))
    +row('confirmed / temporary',String(density.confirmed)+' / '+String(density.temporary))
    +row('role',density.role+' · objective_term '+String(density.objectiveTerm)
      +' · changes_objective_weights '+String(density.changesObjectiveWeights))
    +row('readiness',short(density.readiness.metric)+' · threshold '+short(density.readiness.threshold)
      +' · temporary '+String(density.readiness.temporary)+' · objective_term '+String(density.readiness.objectiveTerm))
    +'</div>'
    +'<h3>可编辑：max_route_risk_density</h3>'
    +'<label>threshold（有限非负数，可空）'
    +'<input class="panel-input" id="thetaV2RiskDensityThreshold" type="number" min="0" step="any" value="'
    +inputValue(density.threshold)+'"></label>'
    +'<label>source（工程依据）'
    +'<input class="panel-input" id="thetaV2RiskDensitySource" value="'+escapeHtml(density.source)+'"></label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2RiskDensityConfirmed"'
    +(density.confirmed?' checked':'')+'> constraint 已确认</label>'
    +'<label class="checkbox-row"><input type="checkbox" id="thetaV2RiskDensityTemporary"'
    +(density.temporary?' checked':'')+'> 临时（temporary）约束</label>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveThetaV2RiskDensity">保存 max_route_risk_density</button>'
    +'</div>'
    +'<div class="parameter-note">阈值只是 candidate 评价的接受条件（evaluation_only）：'
    +'不进入 objective、不改变搜索几何与权重。清空 threshold 时前端提交 null（不猜值），由后端按其规则处理。</div>');
}

function candidateObjectiveRows(candidate){
  const objective=candidate.objective;
  return [
    fieldRow('risk_exposure_index_m','risk_exposure_index_m',fmtNumber(objective.riskExposureIndexM,6)+' index·m'),
    fieldRow('turn_count','turn_count',short(objective.turnCount)),
    fieldRow('total_heading_change_deg','total_heading_change_deg',fmtNumber(objective.totalHeadingChangeDeg,6)+' deg'),
    fieldRow('turn_cost_m','turn_cost_m',fmtNumber(objective.turnCostM,6)+' m'),
    fieldRow('distance_m','distance_m',fmtNumber(objective.distanceM,6)+' m'),
    fieldRow('risk_weight','risk_weight',short(objective.riskWeight)),
    fieldRow('turn_weight','turn_weight',short(objective.turnWeight)),
    fieldRow('distance_weight','distance_weight',short(objective.distanceWeight)),
    fieldRow('weighted_risk','weighted_risk',fmtNumber(objective.weightedRisk,6)),
    fieldRow('weighted_turn','weighted_turn',fmtNumber(objective.weightedTurn,6)),
    fieldRow('weighted_distance','weighted_distance',fmtNumber(objective.weightedDistance,6)),
    fieldRow('total_cost','total_cost',fmtNumber(objective.totalCost,6)),
  ];
}

function candidateSection(model){
  const shown=model.candidates.shown;
  if(!shown){
    return wbBlock('⑥ Theta* V2 candidate 结果',
      '<div class="empty-note">尚无 Theta* V2 candidate：先补齐 Theta* V2 blocker 与 planning request，'
      +'再点击"运行 Theta* V2 candidate"。</div>');
  }
  const objective=shown.objective;
  const density=shown.routeRiskDensity;
  const turns=shown.turnStatistics;
  const search=shown.searchStatistics||{};
  const rejections=['rejected_terrain','rejected_building','rejected_regulatory','rejected_unknown',
    'rejected_hard_constraint','rejected_outside_grid','rewired_parent_shortcuts'];
  const losRows=shown.los.segments.map((segment,index)=>
    '<div class="list-row route-row"><span><b>LOS-'+(index+1)+'</b>'
    +'<small>'+escapeHtml(short(segment.from_grid_id))+' → '+escapeHtml(short(segment.to_grid_id))
    +' · length '+fmtNumber(segment.length_m,3)+' m'
    +' · risk_exposure_index_m '+fmtNumber(segment.risk_exposure_index_m,6)+'</small>'
    +'<small>crossed cells '+String((segment.traversed_cells||[]).length)
    +' · heading '+(segment.incoming_heading_deg===null||segment.incoming_heading_deg===undefined?'—'
      :fmtNumber(segment.incoming_heading_deg,3))
    +' → '+fmtNumber(segment.outgoing_heading_deg,3)+' deg'
    +' · supercover '+String(segment.supercover===true)+' · shortcut '+String(segment.shortcut===true)+'</small>'
    +'</span></div>').join('');
  const turnRows=turns.turns.map((turn,index)=>
    '<div class="list-row route-row"><span><b>turn '+(index+1)+'</b>'
    +'<small>'+fmtNumber(turn.from_heading_deg,3)+' → '+fmtNumber(turn.to_heading_deg,3)
    +' · Δ '+fmtNumber(turn.heading_change_deg,3)+' deg · cost_m '+fmtNumber(turn.cost_m,6)+'</small></span></div>').join('');

  return wbBlock('⑥ Theta* V2 candidate 结果',
    '<div class="parameter-note">下面所有数值都直接转印后端 candidate 字段，前端不重算 objective、'
    +'不重算 risk、不重算距离。candidate 只是候选：'+escapeHtml(THETA_V2_CANDIDATE_SCOPE)+'</div>'
    +'<div class="scroll-list route-list">'
    +row('candidate',short(shown.candidateId||LAYERED_CANDIDATE_LABEL)+' · status '+short(shown.status)
      +' · applicability '+short(shown.applicability))
    +row('lane / layer',short(shown.routeId)+' · '+short(shown.layerId))
    +row('row/step',(shown.isCurrent?'current':(shown.applicability||'—'))
      +' · cells '+String(shown.cellCount)+' · distance '+fmtNumber(shown.distanceM,3)+' m'
    +' · optimization_cost '+fmtNumber(shown.optimizationCost,6))
    +row('search incomplete',String(shown.searchIncomplete))
    +'</div>'
    +'<h3>planning_objective</h3>'
    +'<div class="scroll-list route-list" data-theta-v2-objective="candidate">'
    +candidateObjectiveRows(shown).join('')
    +row('formula',objective.formula)
    +fieldRow('objective_population_shelter_only','objective_population_shelter_only',
      String(objective.objectivePopulationShelterOnly))
    +fieldRow('risk_v2_overall_used','risk_v2_overall_used',String(objective.riskV2OverallUsed))
    +fieldRow('route_risk_density_is_not_an_objective_term','route_risk_density_is_not_an_objective_term',
      String(objective.routeRiskDensityIsNotAnObjectiveTerm))
    +fieldRow('policy_fingerprint','objective policy fingerprint',short(objective.policyFingerprint))
    +'</div>'
    +'<h3>route_risk_density（candidate 评价约束）</h3>'
    +'<div class="scroll-list route-list" data-theta-v2-density="candidate">'
    +fieldRow('route_risk_density_definition','definition',density.definition)
    +fieldRow('route_risk_density_value','value',fmtNumber(density.value,6))
    +fieldRow('route_risk_density_threshold','threshold',fmtNumber(density.threshold,6))
    +fieldRow('route_risk_density_margin','margin',fmtNumber(density.margin,6))
    +fieldRow('route_risk_density_status','status',density.status+' · reason '+short(density.reason))
    +fieldRow('route_risk_density_objective_term','objective_term',String(density.objectiveTerm)
      +' · changes_objective_weights '+String(density.changesObjectiveWeights))
    +fieldRow('route_risk_density_temporary','temporary_constraint',String(density.temporaryConstraint)
      +' · source '+short(density.source))
    +'</div>'
    +'<h3>turn_statistics</h3>'
    +'<div class="scroll-list route-list" data-theta-v2-turns="candidate">'
    +row('turn_count',short(turns.turnCount))
    +row('total_heading_change_deg',fmtNumber(turns.totalHeadingChangeDeg,6)+' deg')
    +row('turn_cost_m',fmtNumber(turns.turnCostM,6)+' m')
    +row('theta_min_deg / d_ref_m',short(turns.thetaMinDeg)+' / '+short(turns.dRefM))
    +row('semantics',short(turns.semantics)+'（规划平滑代理，不是飞行动力学验证）')
    +'</div>'
    +(turns.turns.length?wbDisclosure('turns 明细（'+turns.turns.length+' 次）',listRows([turnRows])):'')
    +'<h3>search_statistics（Theta* / LOS / rewire）</h3>'
    +'<div class="scroll-list route-list" data-theta-v2-search="candidate">'
    +row('expanded / generated labels',short(search.expanded_labels)+' / '+short(search.generated_labels))
    +row('los_checks / los_shortcuts',short(search.los_checks)+' / '+short(search.los_shortcuts))
    +row('rewired_parent_shortcuts',short(search.rewired_parent_shortcuts)
      +'（parent LOS rewiring，不是 A* + 后处理平滑）')
    +row('heading_bin_count / theta_min_deg',short(search.heading_bin_count)+' / '+short(search.theta_min_deg))
    +row('d_ref_m',short(search.d_ref_m)+' · '+short(search.d_ref_provenance))
    +row('search_completeness',short(search.search_completeness)
      +' · limit reached '+String(search.search_limit?.limit_reached===true)
      +' · max_expanded_labels '+short(search.search_limit?.max_expanded_labels))
    +row('risk_unresolved_cell_count',short(search.risk_unresolved_cell_count))
    +row('rejections',rejections.map(key=>key+' '+short(search[key])).join(' · '))
    +'</div>'
    +'<h3>LOS segments（后端审计）</h3>'
    +'<div class="scroll-list route-list">'
    +row('segment count',String(shown.los.segmentCount)+' · crossed cells '+String(shown.los.crossedCellCount))
    +'</div>'
    +(shown.los.segmentCount?wbDisclosure('LOS segment 明细',listRows([losRows])):'')
    +'<h3>blocking_reasons</h3>'
    +blockerRows(shown.blockingReasons.map(normalizeBlocker),'该 candidate 没有 blocking_reasons。')
    +'<h3>fingerprints / provenance</h3>'
    +wbDisclosure('candidate fingerprints',
      listRows([
        row('candidate_fingerprint',short(shown.fingerprints.candidate)),
        row('feasibility_fingerprint',short(shown.fingerprints.feasibility)),
        row('risk_fingerprint',short(shown.fingerprints.risk)),
        row('policy_fingerprint',short(shown.fingerprints.policy)),
        row('request_fingerprint',short(shown.fingerprints.request)),
        row('input_fingerprint',short(shown.fingerprints.input)),
        row('feasibility_mask_fingerprint',short(shown.fingerprints.feasibilityMask)),
        row('communication_informational_fingerprint',short(shown.fingerprints.communicationInformational)),
      ]))
    +wbDisclosure('provenance',
      listRows([
        row('pipeline',short(shown.provenance.pipeline)),
        row('search_semantics',short(shown.provenance.search_semantics?.algorithm||'')),
        row('feasibility_semantics',short(shown.provenance.feasibility_semantics)),
        row('risk_density_constraint',short(shown.provenance.risk_density_constraint?.threshold)
          +' · objective_term '+String(shown.provenance.risk_density_constraint?.objective_term===true)
          +' · changes_objective_weights '+String(shown.provenance.risk_density_constraint?.changes_objective_weights===true)),
        row('communication',short(shown.provenance.communication?.readiness)
          +' · affected_path_or_cost '+String(shown.provenance.communication?.affected_path_or_cost===true)),
        row('airspace',short(shown.provenance.airspace?.applicability)+' · display_only'),
      ]))
    +'<div class="parameter-note"><b>Theta* V2 candidate ≠ operational route</b>：'
    +'不写入 operational_routes / CNS，也不自动 adopt。'+escapeHtml(THETA_V2_PROFILE_SEPARATION)+'</div>');
}

/**
 * 渲染「操作 → 分层候选」的 Theta* V2 视图。
 * @param {object} flow published workflow snapshot
 */
export function renderLayeredThetaV2Panel(flow){
  const model=layeredThetaV2Model(flow);
  return '<div data-theta-v2-panel="'+escapeHtml(model.algorithmId)+'">'
    +readinessSection(model)
    +requestSection(model)
    +searchParameterSection(model)
    +feasibilitySection(model)
    +complianceSection(model)
    +shelterSection(model)
    +objectiveSection(model)
    +riskDensitySection(model)
    +candidateSection(model)
    +'</div>';
}

// ---------------------------------------------------------------- payloads

/**
 * shelter_coefficient_policy payload。
 * ``per_grid_overrides`` 原样回传：保存绝不清空既有覆盖。
 */
export function thetaV2ShelterPolicyPayload(c,current){
  const policy=current||{};
  const coefficient=numberFromField(c.$('thetaV2ShelterCoefficient'));
  const source=fieldValue(c.$('thetaV2ShelterSource')).trim();
  const confirmed=checkedFrom(c.$('thetaV2ShelterConfirmed'));
  const overrides=policy.perGridOverrides&&typeof policy.perGridOverrides==='object'
    ?{...policy.perGridOverrides}:{};
  const unchanged=coefficient===policy.defaultCoefficient
    &&source===text(policy.sourceInput)
    &&confirmed===policy.confirmed;
  const payload={
    default_coefficient:coefficient,
    per_grid_overrides:overrides,
    source,
    confirmed,
  };
  if(unchanged&&policy.provenance)payload.provenance=policy.provenance;
  if(!unchanged)payload.provenance='explicit_override';
  return payload;
}

/** theta_v2_objective_policy payload：原样提交权重，前端绝不静默归一化。 */
export function thetaV2ObjectivePolicyPayload(c,current){
  const objective=current||{};
  return {
    risk_weight:numberFromField(c.$('thetaV2RiskWeight')),
    turn_weight:numberFromField(c.$('thetaV2TurnWeight')),
    distance_weight:numberFromField(c.$('thetaV2DistanceWeight')),
    source:fieldValue(c.$('thetaV2ObjectiveSource')).trim()||text(objective.source),
    confirmed:checkedFrom(c.$('thetaV2ObjectiveConfirmed')),
  };
}

/** max_route_risk_density payload：threshold 空值提交 null（不猜值），后端校验为准。 */
export function thetaV2RiskDensityPayload(c,current){
  const constraint=current||{};
  return {
    threshold:numberFromField(c.$('thetaV2RiskDensityThreshold')),
    source:fieldValue(c.$('thetaV2RiskDensitySource')).trim()||text(constraint.source),
    confirmed:checkedFrom(c.$('thetaV2RiskDensityConfirmed')),
    temporary:checkedFrom(c.$('thetaV2RiskDensityTemporary')),
  };
}

/**
 * search parameter payload（``/api/algorithms/select``）。
 *
 * 三个字段原样提交：空 heading/theta 提交 null，绝不静默回退成软件 baseline（后端会用
 * baseline 补默认值，因此前端必须在 bind 中先拦截空值）。``max_expanded_labels`` 空即 null
 * （不设上限）。provenance 里 ``engineering_confirmed`` 只有在同时提供 evidence 且勾选
 * confirmed 时才可能为 true；后端仍是最终裁决者。
 */
export function thetaV2SearchParametersPayload(c,current){
  const model=current||{};
  const headingBinCount=numberFromField(c.$('thetaV2HeadingBinCount'));
  const thetaMinDeg=numberFromField(c.$('thetaV2ThetaMinDeg'));
  const maxRaw=fieldValue(c.$('thetaV2MaxExpandedLabels')).trim();
  const maxExpandedLabels=maxRaw===''?null:numberFromField(c.$('thetaV2MaxExpandedLabels'));
  const source=fieldValue(c.$('thetaV2SearchParamSource')).trim();
  const evidenceReference=fieldValue(c.$('thetaV2SearchParamEvidence')).trim();
  const confirmed=checkedFrom(c.$('thetaV2SearchParamConfirmed'));
  const engineeringConfirmed=checkedFrom(c.$('thetaV2SearchParamEngineeringConfirmed'));
  const provenance={
    source:source||text(model.source)||THETA_V2_SEARCH_PARAMETER_BASELINE_SOURCE,
    purpose:text(model.purpose)||THETA_V2_SEARCH_PARAMETER_PURPOSE,
    confirmed,
    engineering_confirmed:engineeringConfirmed,
    evidence:evidenceReference
      ?{reference:evidenceReference,statement:'用户在 Step03 Theta* V2 面板显式记录的工程证据引用'}
      :null,
  };
  return {
    algorithm_type:'layered_route_planner',
    algorithm_id:THETA_STAR_V2_ALGORITHM_ID,
    version:THETA_STAR_V2_ALGORITHM_VERSION,
    parameters:{
      heading_bin_count:headingBinCount,
      theta_min_deg:thetaMinDeg,
      max_expanded_labels:maxExpandedLabels,
      search_parameter_provenance:provenance,
    },
  };
}

// ---------------------------------------------------------------- bind
/**
 * 绑定 Theta* V2 视图。evaluate 只按 Theta* V2 blocker 判断：
 * legacy LayeredRouteCostPolicy 的 λ 既不阻止运行，也不出现在提示里。
 */
export function bindLayeredThetaV2(c){
  const model=()=>layeredThetaV2Model(c.flow());
  if(c.$('saveLayeredRequest')){
    c.actionButton('saveLayeredRequest',()=>c.resourceAction(
      '/api/layered-route-planning-request',layeredRequestPayloadFrom(id=>c.$(id))));
  }
  if(c.$('saveLayeredFeasibilityPolicy')){
    c.actionButton('saveLayeredFeasibilityPolicy',()=>c.resourceAction(
      '/api/layered-route-feasibility-policy',layeredFeasibilityPayloadFrom(id=>c.$(id))));
  }
  if(c.$('saveThetaV2ShelterPolicy')){
    c.actionButton('saveThetaV2ShelterPolicy',()=>c.resourceAction(
      '/api/shelter-coefficient-policy',thetaV2ShelterPolicyPayload(c,model().shelter.policy)));
  }
  if(c.$('saveThetaV2ObjectivePolicy')){
    c.actionButton('saveThetaV2ObjectivePolicy',()=>c.resourceAction(
      '/api/theta-v2-objective-policy',thetaV2ObjectivePolicyPayload(c,model().objective)));
  }
  if(c.$('saveThetaV2RiskDensity')){
    c.actionButton('saveThetaV2RiskDensity',()=>c.resourceAction(
      '/api/max-route-risk-density',thetaV2RiskDensityPayload(c,model().riskDensity)));
  }
  if(c.$('saveThetaV2SearchParameters')){
    c.actionButton('saveThetaV2SearchParameters',()=>{
      const payload=thetaV2SearchParametersPayload(c,model().searchParameters);
      const parameters=payload.parameters;
      // 空值不会被静默替换成软件 baseline：这里直接拦截，交给用户补明确数值。
      if(parameters.heading_bin_count===null||!Number.isFinite(parameters.heading_bin_count)
        ||parameters.theta_min_deg===null||!Number.isFinite(parameters.theta_min_deg)){
        c.panelError('heading_bin_count 与 theta_min_deg 必须是明确数值：前端不会用软件 baseline 静默替换空值。');
        return;
      }
      return c.resourceAction('/api/algorithms/select',payload);
    });
  }
  if(c.$('evaluateLayeredCandidate')){
    c.actionButton('evaluateLayeredCandidate',async()=>{
      const current=model();
      if(current.blockers.thetaV2.length){
        c.panelError(THETA_V2_EVALUATE_BLOCKED_NOTE);
        return;
      }
      await c.resourceAction('/api/layered-route-candidates/evaluate-real',{});
    });
  }
}
