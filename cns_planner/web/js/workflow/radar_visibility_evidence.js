/** Pure read-only renderer for Step05 Terrain + Building combined LOS evidence. */
import {escapeHtml,statusBadge} from './common.js';

export const RADAR_VISIBILITY_DISCLAIMER='当前模型仅表达地形与建筑物的几何直达视线，不包含雷达方程、RCS、Pd/Pfa、衍射、多径或杂波。';

//: Round34-T07-I2：联合 LOS 只读诊断端点（``/status`` 不打开任何数据集）。
export const RADAR_COMBINED_LOS_STATUS_ENDPOINT='/api/radar-terrain-los/combined/status';
export const RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT='/api/radar-terrain-los/combined/diagnose';
export const RADAR_COMBINED_LOS_DETAILS_ENDPOINT='/api/radar-terrain-los/combined/details';

export const RADAR_COMBINED_LOS_TITLE='地形与建筑联合视线诊断';
export const RADAR_COMBINED_LOS_DISCLAIMER='地形与建筑联合视线诊断只表达同一条射线上地形与建筑几何的直达遮挡，不包含雷达方程、RCS、探测概率、衍射、多径或杂波；它不写任何项目状态，也不进入 Radar 划设、MILP、Required CNS 或 P16。';
export const RADAR_COMBINED_LOS_SYNTHETIC_NOTE='隔离诊断示例（synthetic）：端点由真实建筑地面高程与高度构造，不代表真实塔站安装位置或现场探测性能，不冒充正式规划结果。';

export const RADAR_COMBINED_LOS_STATE_TEXT={
  not_requested:'尚未请求联合诊断',computed:'已计算',failed:'计算失败',stale:'旧证据已 stale',
};
export const RADAR_COMBINED_LOS_STATUS_TEXT={
  clear:'clear（几何直达未被遮挡）',
  blocked:'blocked（几何直达被遮挡）',
  unknown:'unknown（证据不足，fail-closed）',
  not_evaluated:'not_evaluated（该域未被评价）',
  geometry_filtered:'geometry_filtered（设备冻结几何已排除，不是物理 unknown）',
};
export const BUILDING_DOMAIN_STATE_TEXT={
  evaluated:'已评价',
  geometry_filtered:'设备几何过滤（不是物理 unknown）',
  not_evaluated:'未评价',
};
export const COMBINED_COMPLETION_TEXT={
  complete:'complete',incomplete:'incomplete',not_evaluated:'not_evaluated',
};

const text=value=>(value==null||value==='')?'—':String(value);
const num=value=>(Number.isFinite(Number(value))?Number(value):'—');

// 设备冻结几何已排除的射线：这不是物理 unknown，而是"根本没有进入设备覆盖"。
const FILTERED_BUILDING_STATUSES=['out_of_equipment_range','geometry_filtered','disabled'];

export function buildingDomainState(building){
  if(!building||typeof building!=='object')return 'not_evaluated';
  const status=String(building.status||'');
  if(FILTERED_BUILDING_STATUSES.includes(status))return 'geometry_filtered';
  if(status==='not_evaluated')return 'not_evaluated';
  return 'evaluated';
}

export function radarVisibilityEvidenceModel(value){
  const evidence=value&&typeof value==='object'?value:null;
  const terrain=evidence?.terrain_los||{};
  const building=evidence?.building_los||{};
  const hit=(building.obstruction_hits||[])[0]||{};
  const domainState=buildingDomainState(building);
  return {
    requested:Boolean(evidence),
    combinedStatus:evidence?.combined_visibility||'not_evaluated',
    terrainStatus:terrain.status||'not_evaluated',
    buildingStatus:building.status||'not_evaluated',
    buildingDomainState:domainState,
    geometryFiltered:domainState==='geometry_filtered',
    withinFrozenDeviceRange:building.within_frozen_device_range??null,
    computationCompletion:evidence?.computation_completion||'not_evaluated',
    evidenceCompletion:evidence?.evidence_completion||'not_evaluated',
    reason:building.reason||terrain.reason||null,
    heightEvidenceLevel:hit.height_evidence_level||null,
    terrainAlgorithm:terrain.algorithm_version||null,
    buildingAlgorithm:building.algorithm_version||null,
    combinedAlgorithm:evidence?.algorithm_version||null,
    sourceFile:building.building_source_identity?.file_name||null,
  };
}

export function renderRadarVisibilityEvidence(value){
  const model=radarVisibilityEvidenceModel(value);
  if(!model.requested){
    return '<section class="evidence-block"><h4>地形 + 建筑联合视线</h4>'
      +'<p>尚未执行建筑 LOS 联合诊断；默认加载不会打开 GPKG/DEM。</p>'
      +`<p>${escapeHtml(RADAR_VISIBILITY_DISCLAIMER)}</p></section>`;
  }
  return '<section class="evidence-block"><h4>地形 + 建筑联合视线</h4>'
    +`<p><b>结论</b> · 联合视线：${escapeHtml(text(model.combinedStatus))}`
    +` · 地形 LOS：${escapeHtml(text(model.terrainStatus))}`
    +` · 建筑 LOS：${escapeHtml(text(model.buildingStatus))}`
    +(model.geometryFiltered
      ?' · <b>设备几何过滤</b>：该射线不在冻结设备覆盖内（不是物理 unknown）'
      :'')
    +'</p>'
    +`<p><b>工程原因</b> · ${escapeHtml(text(model.reason))}`
    +` · 高度证据等级：${escapeHtml(text(model.heightEvidenceLevel))}`
    +` · 计算完整度：${escapeHtml(text(model.computationCompletion))}`
    +` · 证据完整度：${escapeHtml(text(model.evidenceCompletion))}</p>`
    +`<p><b>数据来源</b> · ${escapeHtml(text(model.sourceFile))}`
    +` · ${escapeHtml(text(model.terrainAlgorithm))}`
    +` · ${escapeHtml(text(model.buildingAlgorithm))}`
    +` · ${escapeHtml(text(model.combinedAlgorithm))}</p>`
    +`<p>${escapeHtml(RADAR_VISIBILITY_DISCLAIMER)}</p></section>`;
}

/* ============================================================================
 * Round34-T07-I2：联合 LOS 只读证据块
 *
 * 硬约定：
 *  * 默认只消费轻量状态（**不打开 DEM，也不打开 GPKG**）；
 *  * 唯一会读两个真实来源的入口是用户显式点击「开始联合诊断」；
 *  * 前端**不重算**任何结论：三态、完成度、遮挡分类一律原样转印后端字段；
 *  * 可信遮挡与预测性遮挡**分开**披露，绝不合并；
 *  * ``unknown`` 绝不渲染成 clear，``geometry_filtered`` 绝不渲染成物理 unknown。
 * ========================================================================== */

function _countLine(label,counts,labels){
  const source=counts&&typeof counts==='object'?counts:{};
  const names=labels||{};
  //: 只展示**实际发生**的取值：0 计数不占版面，也不会让"零个 clear"看起来像结论。
  const keys=Object.keys(source).filter(key=>Number(source[key])>0);
  return '<div class="coverage-card"><b>'+escapeHtml(label)+'</b>'
    +(keys.length
      ?keys.map(key=>'<span>'+escapeHtml(names[key]||key)+' <b>'
        +escapeHtml(String(source[key]))+'</b></span>').join('')
      :'<span>（本次无该维度取值）</span>')
    +'</div>';
}

function _combinedRayRow(item){
  const geometry=item?.ray_geometry||{};
  const distance=Number.isFinite(Number(geometry.slant_distance_m))
    ?num(geometry.slant_distance_m):'—';
  return '<div class="gap-result"><b>'+escapeHtml(text(item?.tower_id))
    +'</b> · probe '+escapeHtml(text(item?.probe_id))
    +' · 联合 '+statusBadge(String(item?.combined_los_status||'unknown'),
      String(item?.combined_los_status||'unknown'))
    +' · 地形 '+escapeHtml(text(item?.terrain_los_status))
    +' · 建筑 '+escapeHtml(text(item?.building_los_status))
    +' · 建筑域 '+escapeHtml(BUILDING_DOMAIN_STATE_TEXT[item?.building_domain_state]
      ||text(item?.building_domain_state))
    +'<br><small>可信遮挡 '+(item?.trustworthy_obstruction_count??0)
    +' · 预测性遮挡 '+(item?.predictive_obstruction_count??0)
    +' · 遮挡命中 '+(item?.building_obstruction_hit_count??0)
    +' · 计算 '+(COMBINED_COMPLETION_TEXT[item?.computation_completion]
      ||text(item?.computation_completion))
    +' · 证据 '+(COMBINED_COMPLETION_TEXT[item?.evidence_completion]
      ||text(item?.evidence_completion))
    +' · 斜距 '+escapeHtml(String(distance))+' m'
    +(item?.reason?' · 原因 '+escapeHtml(String(item.reason)):'')
    +'</small></div>';
}

function _optionList(options,{valueKey,label,selected}){
  return (options||[]).map(item=>{
    const value=text(item?.[valueKey]);
    return '<option value="'+escapeHtml(value)+'"'
      +(value===selected?' selected':'')+'>'+escapeHtml(label(item))+'</option>';
  }).join('');
}

/** 联合 LOS 只读证据模型（纯函数，便于 node 环境测试）。 */
export function radarCombinedLosModel(flow){
  const status=(flow||{}).radar_combined_los||{};
  const request=(flow||{}).radar_combined_los_request||null;
  const evidence=(flow||{}).radar_combined_los_diagnosis||null;
  const rawDetailPage=(flow||{}).radar_combined_los_details||null;
  const evidenceStatus=evidence?.evidence_status;
  const hasEvidence=Boolean(evidence&&evidence.input_fingerprint
    &&(evidenceStatus==='computed'||evidenceStatus==='stale'));
  const detailPage=hasEvidence&&rawDetailPage
    &&rawDetailPage.request_id===evidence.request_id
    &&rawDetailPage.input_fingerprint===evidence.input_fingerprint
    &&rawDetailPage.generation===evidence.generation
    ?rawDetailPage:null;
  const view=evidence||status||{};
  const aggregate=view.aggregate||{};
  const availability=status.adapter_availability||{};
  const currentness=view.currentness||status.currentness||{};
  const routeConsumption=view.route_consumption||status.route_consumption||{};
  const total=aggregate.evaluated_ray_count||view.evaluated_ray_count||0;
  return {
    status,evidence,detailPage,requestPending:request?.state==='running',
    routeConsumption,
    engineeringDemoMode:routeConsumption.engineering_demo_mode===true,
    hasEvidence,
    availableToCompute:status.available_to_compute===true
      ||status.available_with_explicit_altitude_layer===true,
    evidenceStatus:view.evidence_status||'not_requested',
    evidenceStatusLabel:RADAR_COMBINED_LOS_STATE_TEXT[view.evidence_status]
      ||view.evidence_status_label||RADAR_COMBINED_LOS_STATE_TEXT.not_requested,
    algorithmVersion:view.algorithm_version||status.algorithm_version||null,
    schemaVersion:view.schema_version||status.schema_version||null,
    evaluationScope:view.evaluation_scope||status.evaluation_scope||null,
    evaluatedRayCount:total,
    evaluatedTowerCount:view.evaluated_tower_count||0,
    candidateTowerCount:view.candidate_tower_count||0,
    raySampleCount:view.ray_sample_count||0,
    raySampleLimit:view.ray_sample_limit||0,
    raySampleTruncated:view.ray_sample_truncated===true,
    terrainCounts:aggregate.terrain_los_status_counts||{},
    buildingCounts:aggregate.building_los_status_counts||{},
    combinedCounts:aggregate.combined_los_status_counts||{},
    computationCounts:aggregate.computation_completion_counts||{},
    evidenceCounts:aggregate.evidence_completion_counts||{},
    buildingDomainStateCounts:aggregate.building_domain_state_counts||{},
    heightEvidenceCounts:aggregate.height_evidence_level_counts||{},
    trustworthyRayCount:aggregate.trustworthy_obstruction_ray_count||0,
    predictiveRayCount:aggregate.predictive_obstruction_ray_count||0,
    geometryFilteredRayCount:aggregate.geometry_filtered_ray_count||0,
    notEvaluatedRayCount:aggregate.building_not_evaluated_ray_count||0,
    unknownCombinationCount:aggregate.unknown_combination_count||0,
    rays:Array.isArray(view.rays)?view.rays:[],
    currentness,stale:currentness.stale===true,
    currentnessState:currentness.state||null,
    currentnessReasons:Array.isArray(currentness.reasons_zh)?currentness.reasons_zh:[],
    failureReason:view.failure_reason||null,
    failureDetail:view.failure_detail||null,
    inputFingerprint:view.input_fingerprint||null,
    bounds:view.bounds||null,
    truncation:view.truncation||null,
    probeSelection:view.probe_selection||null,
    executionReceipt:view.execution_receipt||null,
    lookupSnapshot:view.lookup_snapshot||null,
    sourceProvenance:view.source_provenance||null,
    terrainSourceIdentity:view.terrain_source_identity||availability.terrain_dtm||null,
    buildingSourceIdentity:view.building_source_identity||availability.buildings||null,
    altitudeLayer:view.altitude_layer||availability.altitude_layer||null,
    sampling:view.sampling||null,
    demOpenCount:view.dem_open_count,
    buildingOpenCount:view.building_open_count,
    terrainConfigured:availability.terrain_dtm?.configured===true,
    buildingsConfigured:availability.buildings?.configured===true,
    buildingsSpatialIndexChecked:availability.buildings?.spatial_index_checked===true,
    towerOptions:Array.isArray(status.tower_options)?status.tower_options:[],
    altitudeLayerOptions:Array.isArray(status.altitude_layer_options)
      ?status.altitude_layer_options:[],
    detailRecordCount:detailPage&&Array.isArray(detailPage.records)
      ?detailPage.records.length:0,
    detailMatchedCount:detailPage?detailPage.matched_record_count:null,
    detailLimit:detailPage?detailPage.limit:null,
    detailMaxLimit:detailPage?detailPage.max_limit:null,
    detailRecordTotal:view.detail_record_total||0,
    detailAvailable:hasEvidence&&(view.detail_available===true||status.detail_available===true),
    notEvaluated:Array.isArray(view.not_evaluated)?view.not_evaluated:[],
    disclaimer:view.user_disclaimer_zh||RADAR_COMBINED_LOS_DISCLAIMER,
    productionEnforcedEnabled:view.production_enforced_enabled===true,
    readOnlyDeclared:view.has_project_state_writer===false
      &&view.writes_project_state===false,
    syntheticDisclosure:view.synthetic_endpoint_disclosure||null,
  };
}

/** 联合 LOS 只读证据块 HTML（含站址 / 高度层选择与显式诊断按钮）。 */
export function renderRadarCombinedLosBlock(flow,options={}){
  const model=radarCombinedLosModel(flow);
  const layer=model.altitudeLayer||{};
  const bounds=model.bounds||{};
  const statusLine='<b>'+escapeHtml(RADAR_COMBINED_LOS_TITLE)+'</b> '
    +statusBadge(model.evidenceStatus,
      RADAR_COMBINED_LOS_STATE_TEXT[model.evidenceStatus]||'')
    +' · 只读证据（不参与规划判定）';
  const selectedTower=String(options.selectedTowerId||model.rays[0]?.tower_id
    ||model.towerOptions[0]?.tower_id||'');
  const selectedLayer=String(options.selectedLayerId||layer.altitude_layer_id||'');
  const probeBudget=options.probeBudget||bounds.probe_budget||6;
  const selectors='<div class="form-grid">'
    +'<label>诊断站址<select class="panel-input" id="radarCombinedLosTower">'
    +_optionList(model.towerOptions,{
      valueKey:'tower_id',selected:selectedTower,
      label:item=>text(item?.tower_id)})
    +'</select></label>'
    +'<label>高度层（EGM2008 正高）<select class="panel-input" id="radarCombinedLosLayer">'
    +_optionList(model.altitudeLayerOptions,{
      valueKey:'altitude_layer_id',selected:selectedLayer,
      label:item=>text(item?.altitude_layer_id)+'（'
        +(Number.isFinite(Number(item?.nominal_altitude_m))
          ?num(item.nominal_altitude_m):'—')+' m）'})
    +'</select></label>'
    +'<label>目标采样点预算<input class="panel-input" type="number" min="1" max="48" '
    +'id="radarCombinedLosProbeBudget" value="'+escapeHtml(String(probeBudget))+'"></label>'
    +'</div>';
  const actionRow='<div class="button-row">'
    +'<button class="secondary" id="runRadarCombinedLos"'
    +(model.availableToCompute?'':' disabled')+'>开始联合诊断</button>'
    +'<button class="secondary" id="loadRadarCombinedLosDetails"'
    +(model.hasEvidence?'':' disabled')+'>载入逐射线明细（受限分页）</button></div>';
  const demoScope=model.engineeringDemoMode
    ?'<label class="check-row"><input type="checkbox" id="radarCombinedLosEngineeringDemoMode" checked>'
      +'<b>显式启用工程演示只读航路消费</b></label>'
      +'<div class="parameter-note"><b>工程条件接受，严格验证未通过，仅供演示</b>；'
      +'strict_safety_validation_passed=false · engineering_conditional_acceptance=true · '
      +'use_scope=engineering_demo_only · operational_authorization=false · '
      +'published_to_operational_routes=false<br>limitations '
      +escapeHtml(JSON.stringify(model.routeConsumption.limitations||[]))+'</div>'
    :'';
  const availabilityNote=model.availableToCompute?''
    :'<div class="parameter-note">当前不可计算：需要项目已配置的 terrain_dtm 与 buildings '
      +'数据源、显式米制投影与真实铁塔站址。缺失一律 fail-closed，'
      +'绝不猜默认数据源，也不接受前端指定任意本地路径。</div>';
  const boundaryLine='<div class="parameter-note">'
    +'本轮<b>未评估</b>：'+model.notEvaluated.slice(0,8).map(escapeHtml).join(' · ')
    +'（等）<br>诊断<b>不写</b>任何 ProjectState 容器、<b>不写</b> artifact、'
    +'<b>不注册</b>失效入口，也不参与 MILP / P16 / Required CNS；'
    +'生产 <code>enforced</code> 保持未启用（'
    +(model.productionEnforcedEnabled?'已启用':'未启用')+'）。'
    +'完整地形剖面与完整 LOS 矩阵都不进入 API 响应。</div>';
  const syntheticLine=model.syntheticDisclosure
    ?'<div class="parameter-note"><b>隔离诊断示例（synthetic）</b>：'
      +escapeHtml(model.syntheticDisclosure)+'</div>'
    :(model.hasEvidence
      ?'<div class="parameter-note"><b>隔离诊断示例（synthetic）</b>：'
        +escapeHtml(RADAR_COMBINED_LOS_SYNTHETIC_NOTE)+'</div>'
      :'');
  const disclaimer='<div class="parameter-note"><b>'+escapeHtml(model.disclaimer)
    +'</b></div>';

  if(!model.hasEvidence){
    const planned='<div class="parameter-note">'
      +(model.requestPending?'联合诊断请求处理中，旧结果已退出当前视图。':'尚未执行联合诊断。')
      +'打开本面板或读取项目状态<b>不会</b>读取 DEM / GPKG，也不会触发任何 LOS 计算。'
      +(model.failureReason
        ?'<br><b>最近一次失败：</b>'+escapeHtml(text(model.failureReason))
          +(model.failureDetail?'（'+escapeHtml(text(model.failureDetail))+'）':'')
          +'<br>失败<b>不</b>等于"无遮挡"：不会返回任何 clear 结论。'
        :'')
      +'</div>';
    return '<h4>'+escapeHtml(RADAR_COMBINED_LOS_TITLE)+'</h4>'
      +'<div class="parameter-note">'+statusLine+'</div>'
      +planned+selectors+demoScope+availabilityNote+actionRow
      +syntheticLine+boundaryLine+disclaimer;
  }

  const countsLine='<div class="parameter-note"><b>已评估结论</b>：联合评价 '
    +model.evaluatedRayCount+' 条射线 · 已评估站址 '+model.evaluatedTowerCount
    +' / 候选站址 '+model.candidateTowerCount+' 座</div>'
    +_countLine('地形 LOS',model.terrainCounts,RADAR_COMBINED_LOS_STATUS_TEXT)
    +_countLine('建筑 LOS',model.buildingCounts,RADAR_COMBINED_LOS_STATUS_TEXT)
    +_countLine('联合直达视线',model.combinedCounts,RADAR_COMBINED_LOS_STATUS_TEXT);
  const evidenceLevelLine='<div class="parameter-note">'
    +'<b>建筑证据等级</b>：可信遮挡（verified / source_tagged / derived） '
    +model.trustworthyRayCount+' 条射线 · 预测性遮挡（predicted） '
    +model.predictiveRayCount+' 条射线'
    +'<br>两类遮挡<b>分开</b>披露、绝不合并；预测性高度不得用作严格判定依据。'
    +'<br>建筑域状态：'+Object.keys(model.buildingDomainStateCounts).map(key=>
      escapeHtml(BUILDING_DOMAIN_STATE_TEXT[key]||key)+' <b>'
      +model.buildingDomainStateCounts[key]+'</b>').join(' · ')
    +(model.geometryFilteredRayCount
      ?'<br>其中 '+model.geometryFilteredRayCount
        +' 条射线被设备冻结几何排除：这不是物理 unknown，也不代表"被建筑遮挡"。'
      :'')
    +'<br>高度证据等级计数：'+(Object.keys(model.heightEvidenceCounts).length
      ?Object.keys(model.heightEvidenceCounts).map(key=>escapeHtml(key)+' <b>'
        +model.heightEvidenceCounts[key]+'</b>').join(' · ')
      :'—')+'</div>';
  const completionLine='<div class="parameter-note">'
    +'<b>完成度</b>：计算 '+Object.keys(model.computationCounts).map(key=>
      escapeHtml(key)+' <b>'+model.computationCounts[key]+'</b>').join(' · ')
    +' · 证据 '+Object.keys(model.evidenceCounts).map(key=>
      escapeHtml(key)+' <b>'+model.evidenceCounts[key]+'</b>').join(' · ')
    +'<br>未知组合 <b>'+model.unknownCombinationCount+'</b> 条：'
    +'证据不足一律保持 unknown 并 fail-closed，<b>绝不</b>当作 clear。'
    +(model.truncation
      ?'<br>预算截断：已评价 '+text(model.truncation.evaluated_probe_count)
        +' · 未评价 '+text(model.truncation.unevaluated_probe_count)
        +' · 每塔预算 '+text(model.truncation.probe_budget_per_tower)
        +' · 射线上限 '+text(model.truncation.max_ray_total)
        +(model.truncation.building_budget_exhausted?' · 建筑域预算已耗尽':'')
      :'')+'</div>';
  const failureLine=model.failureReason
    ?'<div class="parameter-note"><b>计算失败：</b>'+escapeHtml(text(model.failureReason))
      +(model.failureDetail?'（'+escapeHtml(text(model.failureDetail))+'）':'')
      +'<br>失败<b>不</b>等于"无遮挡"：不会返回任何 clear 结论。</div>':'';
  const staleWarning=model.stale
    ?'<div class="parameter-note"><b>旧证据已 stale：</b>下列结论来自旧输入，仅作参考，'
      +'不可作为当前联合可见性依据。</div>':'';
  const sourceLine='<div class="parameter-note">'
    +'<b>数据来源</b>：地形 '+escapeHtml(text(model.terrainSourceIdentity?.file_name))
    +' · 建筑 '+escapeHtml(text(model.buildingSourceIdentity?.file_name))
    +'（'+escapeHtml(text(model.buildingSourceIdentity?.source_version))
    +' · RTree '+escapeHtml(String(model.buildingSourceIdentity?.spatial_index_available))
    +'）<br>算法版本：联合 '+escapeHtml(text(model.algorithmVersion))
    +' · 契约 '+escapeHtml(text(model.schemaVersion))
    +' · 评价范围 '+escapeHtml(text(model.evaluationScope))
    +'<br>高度层：'+escapeHtml(text(layer.altitude_layer_id))
    +'（'+(Number.isFinite(Number(layer.nominal_altitude_m))
      ?num(layer.nominal_altitude_m):'—')+' m · '
    +escapeHtml(text(layer.vertical_reference))+'）· 采样 '
    +(Number.isFinite(Number(model.sampling?.sampling_interval_m))
      ?num(model.sampling.sampling_interval_m):'—')+' m · 包络半径 '
    +(Number.isFinite(Number(model.sampling?.envelope_radius_m))
      ?num(model.sampling.envelope_radius_m):'—')+' m'
    +'<br>数据源打开次数：DEM '+text(model.demOpenCount)
    +' · buildings '+text(model.buildingOpenCount)
    +' · 建筑查询 '+text(model.executionReceipt?.lookup_snapshot?.building_ray_count)
    +' · 建筑候选命中 '+text(model.executionReceipt?.lookup_snapshot?.building_exact_intersection_count)
    +'<br>输入指纹 <code>'+escapeHtml(text(model.inputFingerprint))+'</code></div>';
  const currentnessLine='<div class="parameter-note">'
    +'<b>证据时效（独立判断）</b>：'+escapeHtml(text(model.currentnessState))
    +'<br>'+(model.currentnessReasons.length
      ?model.currentnessReasons.map(escapeHtml).join('<br>')
      :'未提供可比较的旧证据，因此不宣称任何旧结果 stale。')
    +'<br>联合 LOS 证据的 stale <b>不</b>传播到上游规划结果、P14/P15/P16 或任何既有容器；'
    +'本轮没有 ProjectState writer，也没有结果持久化。</div>';
  const sampleList=model.rays.length
    ?'<div class="gap-results">'+model.rays.map(_combinedRayRow).join('')+'</div>'
    :'<div class="empty-note">本次诊断没有可展示的射线记录。</div>';
  const sampleNote=model.raySampleTruncated
    ?'<div class="parameter-note">仅展示前 '+model.raySampleCount
      +' 条射线；这不是全部评价明细（默认摘要不隐含全部明细）。</div>':'';
  const detailBlock=model.detailPage
    ?'<div class="parameter-note">逐射线明细：匹配 '+String(model.detailMatchedCount)
      +' 条 · 本页 '+model.detailRecordCount+' 条 · 每页上限 '+String(model.detailMaxLimit)
      +'（分页读取，不重算、不重新打开数据源，也不含完整地形剖面）</div>'
    :'';
  return '<h4>'+escapeHtml(RADAR_COMBINED_LOS_TITLE)+'</h4>'
    +'<div class="parameter-note">'+statusLine+'</div>'
    +staleWarning+failureLine+countsLine+evidenceLevelLine+completionLine
    +selectors+demoScope+actionRow+sourceLine+currentnessLine
    +syntheticLine
    +'<div class="scroll-list cns-input-list">'+sampleNote+sampleList+'</div>'
    +detailBlock+boundaryLine+disclaimer;
}
