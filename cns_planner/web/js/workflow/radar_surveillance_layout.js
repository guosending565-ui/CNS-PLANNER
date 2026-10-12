/**
 * Step05「监视雷达初步划设」独立任务卡（Radar Surveillance Layout V1）。
 *
 * 正式名称：「80m固定高度航路方向性雷达几何初步划设方案」。
 *
 * 语义边界（前端只**展示**，不做任何推算）：
 *  * `model_scope = geometric_initial_radar_layout`，`proposal_only = true`；
 *  * radar_equation / Pd-distance curve / terrain LOS / diffraction / building blocking /
 *    clutter / multipath / interference 全部 `not_evaluated`；
 *  * 未配置陆域掩膜时 surface_class = unknown（fail-closed），绝不按 sea 处理；
 *  * radar mount height 只能来自显式工程假设，前端提交后仍标记
 *    `confirmed=false` / `parameter_origin=engineering_assumption`。
 *
 * 本模块只产出 HTML 字符串与纯数据模型，不直接访问 DOM，便于 node 环境测试。
 */
import {escapeHtml,statusBadge,statusText,verticalReferenceText} from './common.js';
//: Round34-T07-R 集成适配：T07-B 的地形 LOS 证据块复用工作台既有的 disclosure
//: 折叠块（``workbench.js`` 的同一实现），不新增第二套折叠 UI、不改既有布局契约。
import {disclosure} from './workbench.js';
//: Round34-T07-I2：地形 + 建筑**联合**视线诊断（复用同一只读证据 renderer，
//: 不新增第二套可视化逻辑；轻量状态不打开任何数据集）。
import {RADAR_COMBINED_LOS_DETAILS_ENDPOINT,RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT,RADAR_COMBINED_LOS_STATUS_ENDPOINT,renderRadarCombinedLosBlock} from './radar_visibility_evidence.js';

export const RADAR_LAYOUT_ENDPOINT='/api/radar-surveillance-layout';
export const RADAR_LAYOUT_EVALUATE_ENDPOINT='/api/radar-surveillance-layout/evaluate';
export const RADAR_LAYOUT_READINESS_ENDPOINT='/api/radar-surveillance-layout/readiness';
export const RADAR_POLICY_ENDPOINT='/api/radar-surveillance-policy';
//: Round34-T07-B：**只读**地形 LOS 诊断入口。
//: ``/status`` 与通用快照同口径（不打开 DEM）；``/diagnose`` 才是唯一会读 DEM 的入口，
//: 且只在用户显式点击时调用；``/details`` 只读服务端内存证据块（受限分页）。
export const RADAR_TERRAIN_LOS_STATUS_ENDPOINT='/api/radar-terrain-los/status';
export const RADAR_TERRAIN_LOS_DIAGNOSE_ENDPOINT='/api/radar-terrain-los/diagnose';
export const RADAR_TERRAIN_LOS_DETAILS_ENDPOINT='/api/radar-terrain-los/details';
//: 用户可见声明（必须原样展示）。
export const RADAR_TERRAIN_LOS_DISCLAIMER='地形视距分析仅为几何可视性证据，不代表实际雷达探测概率或设备可安装确认。';
export const RADAR_TERRAIN_LOS_TITLE='地形视距（LOS）只读检查';
export const RADAR_TERRAIN_LOS_STATE_TEXT={
  not_requested:'尚未请求地形 LOS 计算',
  computed:'已计算',
  failed:'计算失败',
  stale:'旧证据已 stale',
};

export const RADAR_LAYOUT_TITLE='监视雷达初步划设';
export const RADAR_LAYOUT_PROPOSAL_TITLE='80m固定高度航路方向性雷达几何初步划设方案';
export const RADAR_LAYOUT_MODEL_SCOPE='geometric_initial_radar_layout';
export const RADAR_LAYOUT_PROPOSAL_ONLY_NOTE='proposal_only：本结果不写入既有 CNS 设施、不写入 coverage_3d / 能力 / 走廊与站址提案，也不会自动 Apply。';
export const RADAR_DEMO_PREVIEW_WARNING='演示预览：当前航路尚未完成建筑地面高程证据验证，不代表运行航路已发布。';

export const RADAR_TYPE_LABELS={radar_i:'中近程雷达Ⅰ型',radar_ii:'中近程雷达Ⅱ型'};
export const SURFACE_CLASS_LABELS={
  land:'陆地',sea:'海上',coastal_uncertain:'海岸不确定带（按陆地处理）',unknown:'未知（fail-closed）',
};
export const COVERAGE_STATUS_LABELS={satisfied:'满足',under_redundant:'冗余不足',uncovered:'未覆盖',unknown:'证据不足'};
export const REQUIRED_SITE_COUNT_LABELS={
  land:'2 个不同铁塔站址',sea:'1 个铁塔站址',coastal_uncertain:'2 个不同铁塔站址（按陆地处理）',
};
//: V1.1 显式语义（前端只展示，不做任何推算）。
export const RADAR_LAYOUT_VERSION='1.1';
export const RADAR_LAYOUT_V1_1_SEMANTICS={
  // 高度层不写死：实际值来自 readiness.fixed_altitude（B4X §17）。
  route_altitude:'固定巡航高度层（EGM2008 正高，不是地形高程）',
  vertical_delta:'高度差 = 目标 − 雷达原点；目标低于雷达原点 ⇒ 负仰角 ⇒ 不覆盖',
  radar_origin:'雷达原点 = tower_top_orthometric_m（塔顶即相位中心，不再叠加挂高）',
  coastal_buffer:'海岸不确定带：工程假设 / 未确认，不是数据真实精度',
  plane_radii:'斜距 ≠ 平面水平半径；半径由后端交截计算提供',
};

// ---- 字段级中文取词（本面板只展示，不参与任何推算） --------------------------
//: 雷达原点基准 raw 值 → 中文。
export const RADAR_ORIGIN_BASIS_TEXT={
  tower_top_orthometric_m:'塔顶 EGM2008 正高',
  tower_top:'塔顶',ground:'地面',unknown:'基准未确认'
};
//: 安装假设 raw 值 → 中文。
export const RADAR_INSTALLATION_TEXT={
  radar_phase_center_at_tower_top:'相位中心位于塔顶',
  radar_phase_center_at_mount_height:'相位中心位于挂高',
  unknown:'安装假设未确认'
};
//: 陆域判定依据 raw 值 → 中文。
export const SURFACE_BASIS_TEXT={
  explicit_polygon:'显式多边形',
  land_mask_polygon:'显式陆域多边形',
  not_configured:'未配置'
};
//: 参数来源 raw 值 → 中文。
export const PARAMETER_ORIGIN_TEXT={
  engineering_assumption:'工程假设',user_configuration:'用户显式配置',
  project_default:'项目默认值',algorithm_baseline:'算法基线'
};

const pct=value=>Number.isFinite(value)?(value*100).toFixed(1)+'%':'—';
const num=(value,digits=1)=>Number.isFinite(value)?value.toFixed(digits):'—';
/** 垂向基准 raw 值 → 用户可理解中文（绝不直接显示 `egm2008_orthometric`）。 */
const referenceLabel=value=>verticalReferenceText(value);
const text=value=>(value==null||value==='')?'—':String(value);

/** 求解状态 → 用户可读标签（绝不含糊"未完成"与"不可行"）。 */
export const SOLVER_STATUS_LABELS={
  optimal:'已证明最优',
  infeasible:'已证明不可行',
  time_limit:'搜索超时（未证明）',
  iteration_limit:'迭代上限（未证明）',
  node_limit:'节点上限（未证明）',
  solver_error:'求解器错误',
  solver_unavailable:'求解器不可用',
  not_run:'未求解',
};

/**
 * Radar 缺口的 canonical 原因 → 用户可读中文（未登记取值原样显示，绝不编造）。
 *
 * Round29-K：``independent_site_count_limited`` 是**已证明**的物理限制
 * （``gap_classification=confirmed_gap`` 且 ``managed_physical_gap=true``）；
 * ``search_incomplete`` 等只是搜索未完成，**不是** managed 缺口。
 */
export const RADAR_GAP_REASON_TEXT={
  independent_site_count_limited:'独立站址数量受限：约束下无法达到要求的独立站址重数（已证明）',
  search_incomplete:'搜索未完成（触达资源上限，未证明不可行）',
};

/** 结果状态 → 是否可视为"完整覆盖方案"。 */
export function isCompleteCoverage(status){
  return status==='proposal_ready';
}

/**
 * 任务卡数据模型（纯函数，便于测试与在地图上复用）。
 */
export function radarLayoutModel(flow){
  const layout=flow?.radar_surveillance_layout||{},readiness=flow?.radar_surveillance_layout_readiness||{};
  const detail=layout.detail||null;
  const summaryItems=Array.isArray(layout.items)?layout.items:[];
  const item=detail
    ||[...summaryItems].reverse().find(entry=>entry?.demo_preview_only===true&&entry?.status!=='stale')
    ||summaryItems[summaryItems.length-1]
    ||null;
  const solver=item?.solver||null;
  const validation=item?.validation||null;
  const sources=item?.sources||readiness.sources||{};
  return {
    status:layout.status||'not_calculated',
    hasDetail:Boolean(detail||item?.validation),
    detailEndpoint:RADAR_LAYOUT_ENDPOINT,
    readiness,
    item,
    demoPreviewOnly:item?.demo_preview_only===true,
    previewWarning:item?.preview_warning||null,
    solver,
    solverStatus:solver?.status||null,
    solverLabel:solver?SOLVER_STATUS_LABELS[solver.status]||solver.status:null,
    optimalityProven:solver?.optimality_proven===true,
    infeasibilityProven:solver?.infeasibility_proven===true,
    stage:item?.stage||null,
    stageLabel:item?.stage_label||null,
    radarICount:item?.radar_i_panel_count,
    radarIICount:item?.radar_ii_panel_count,
    panelCount:item?.selected_panel_count,
    towerCount:item?.selected_tower_count,
    candidateTowerCount:item?.candidate_tower_count,
    candidatePanelCount:item?.candidate_panel_count,
    selectedPanels:item?.selected_panels||[],
    land:validation?.land||item?.land_validation||null,
    sea:validation?.sea||item?.sea_validation||null,
    unknown:validation?.unknown||item?.unknown_validation||null,
    coastalUncertain:validation?.coastal_uncertain||null,
    uncoveredSegments:validation?.uncovered_segments||item?.uncovered_segments||[],
    underRedundantSegments:validation?.under_redundant_segments||item?.under_redundant_segments||[],
    unknownSegments:validation?.unknown_segments||item?.unknown_segments||[],
    coverageProfile:validation?.coverage_profile||item?.coverage_profile||null,
    refinementRounds:item?.refinement_rounds||[],
    infeasibilityReasons:item?.infeasibility_reasons||[],
    unknownEvidence:item?.unknown_evidence||[],
    //: Round29-K 的 canonical 缺口三字段：**只读转印**，前端绝不自行推断
    //: "搜索未完成"是否等于 managed 缺口。
    gapClassification:item?.gap_classification||null,
    managedPhysicalGap:item?.managed_physical_gap===true,
    gapReason:item?.gap_reason||null,
    gapReasonLabel:item?.gap_reason
      ?(RADAR_GAP_REASON_TEXT[item.gap_reason]||String(item.gap_reason)):null,
    terminalManagedGap:item?.managed_physical_gap===true
      &&item?.gap_classification==='confirmed_gap',
    searchIncomplete:['search_incomplete','refinement_incomplete','unresolved','stale']
      .includes(String(item?.status||'')),
    radarOrigin:item?.radar_origin||readiness.radar_origin||null,
    // legacy 挂高：只展示、只标注，绝不影响 V1.1 几何。
    legacyMountHeight:item?.sources?.radar_mount_height||readiness.radar_mount_height||null,
    landMask:item?.sources?.land_mask||readiness.land_mask||null,
    landMaskProvenance:item?.land_mask_source_provenance||readiness.land_mask_source_provenance||null,
    landMaskReady:Boolean(
      (item?.sources?.land_mask?.ok)||(readiness.land_mask?.status==='passed')
      ||(readiness.sources?.land_mask?.ok)
    ),
    landMaskReason:
      (item?.sources?.land_mask?.reason)||readiness.land_mask?.reason
      ||readiness.sources?.land_mask?.reason||null,
    metricTransform:readiness.metric_transform||null,
    solverReadiness:readiness.solver||null,
    readinessBlockers:item?.blockers||readiness.blockers||[],
    routeSampling:item?.route_sampling||null,
    surfaceClassification:item?.surface_classification||null,
    semanticsFingerprint:item?.semantics_fingerprint||readiness.semantics_fingerprint||null,
    terrainReady:Boolean(readiness.sources?.terrain?.available),
    parameters:item?.parameters||readiness.parameters||null,
    deviceTypes:readiness?.device_summary?.types||[],
    deviceSource:readiness?.device_summary?.source||null,
  };
}

/**
 * 航路按覆盖结果着色的**线段**（satisfied / under_redundant / uncovered / unknown）。
 *
 * 只消费服务端已经合并好的 `coverage_profile`；前端不重算任何几何，也不逐点下发。
 * 相邻不同状态的段之间会补一条极短的"过渡段"，颜色取**较差**的一方（保守显示）。
 */
export function routeCoverageColours(item){
  const profile=item?.validation?.coverage_profile||item?.coverage_profile;
  const entries=profile?.entries||[];
  const finite=coordinate=>Array.isArray(coordinate)&&coordinate.length>=2
    &&coordinate.every(value=>typeof value==='number'&&Number.isFinite(value));
  const rank={satisfied:0,unknown:1,under_redundant:2,uncovered:3};
  const segments=[];
  for(let index=0;index<entries.length;index+=1){
    const entry=entries[index];
    if(!finite(entry.start_coordinate)||!finite(entry.end_coordinate))continue;
    segments.push({
      status:entry.status,from:entry.start_coordinate,to:entry.end_coordinate,
      from_m:entry.from_m,to_m:entry.to_m,
    });
    const next=entries[index+1];
    if(next&&finite(next.start_coordinate)&&!samePoint(entry.end_coordinate,next.start_coordinate)){
      const worst=(rank[next.status]||0)>(rank[entry.status]||0)?next.status:entry.status;
      segments.push({
        status:worst,from:entry.end_coordinate,to:next.start_coordinate,
        from_m:entry.to_m,to_m:next.from_m,bridge:true,
      });
    }
  }
  return segments;
}

function samePoint(left,right){
  return Array.isArray(left)&&Array.isArray(right)
    &&Math.abs(left[0]-right[0])<1e-12&&Math.abs(left[1]-right[1])<1e-12;
}

/**
 * 地图 overlay 输入模型：selected towers、selected panel 扇区、
 * 以及按覆盖结果着色的航路（来自后端**已合并**的 coverage_profile，前端不重算几何）。
 *
 * V1.1（BUG-RADAR-OVERLAY-005）：Radar-I 的 120/3000 m 与 Radar-II 的 200/5000 m 是
 * **斜距（slant range）**，前端**绝不**再把它们当作 80 m 平面的水平半径。
 * 水平内外半径一律消费后端 ``horizontal_inner_radius_m`` / ``horizontal_outer_radius_m``；
 * 后端给出 ``plane_intersection_status='no_intersection'`` 的 panel 不画扇区。
 *
 * 注意：**不**绘制未选 panel 的 coverage polygon（会一次产生数百个多边形）。
 * 候选铁塔由调用方从 `flow.towers` 传入（弱化显示），因为 candidates 不下发到通用快照。
 */
export function radarOverlayModel(flow,candidateTowers=[]){
  const model=radarLayoutModel(flow);
  const item=model.item;
  if(!item)return {
    status:'not_calculated',candidateTowers:[],selectedTowers:[],panels:[],
    routeCoverageColours:[],panelsWithoutPlaneIntersection:0,
  };
  const selectedTowerIds=[...new Set(model.selectedPanels.map(panel=>panel.tower_id))];
  const selectedTowers=model.selectedPanels
    .filter(panel=>panel.longitude!=null&&panel.latitude!=null)
    .map(panel=>({
      tower_id:panel.tower_id,
      name:panel.tower_name,
      coordinate:[panel.longitude,panel.latitude],
      radar_origin_egm2008_m:panel.radar_origin_egm2008_m,
    }));
  let noIntersection=0;
  const panels=[];
  for(const panel of model.selectedPanels){
    if(panel.longitude==null||panel.latitude==null)continue;
    const inner=panel.horizontal_inner_radius_m,outer=panel.horizontal_outer_radius_m;
    const display=panel.display_geometry||{};
    // 地图直接消费后端固定高度平面交截半径；display_geometry 仅保留作审计。
    // 斜距及 route-focused display 半径都绝不进入前端地图几何。
    if(panel.plane_intersection_status==='no_intersection'
      ||!Number.isFinite(inner)||!Number.isFinite(outer)||outer<=0){
      noIntersection+=1;
      continue;
    }
    panels.push({
      panel_id:panel.panel_id,
      tower_id:panel.tower_id,
      coordinate:[panel.longitude,panel.latitude],
      radar_type:panel.radar_type,
      radar_type_label:panel.radar_type_label,
      azimuth_deg:panel.azimuth_deg,
      half_width_deg:panel.panel_half_width_deg,
      altitude_m:item.altitude_m,
      physicalRadiusInnerM:inner,
      physicalRadiusOuterM:outer,
      displayRadiusInnerM:inner,
      displayRadiusOuterM:outer,
      displayGeometrySemantics:display.semantics
        ||'physical_outer_radius_no_covered_sample_detail',
      displayGeometryFallback:display.display_geometry_fallback||(
        panel.display_geometry?null:'physical_outer_radius_no_covered_sample_detail'
      ),
      plane_intersection_status:panel.plane_intersection_status,
      dz_m:panel.altitude_plane_geometry?.dz_m,
      horizontal_radius_semantics:'backend_plane_intersection_not_slant_range',
    });
  }
  return {
    status:model.status,
    proposalOnly:true,
    // 候选铁塔（弱化显示）：只画尚未被选中的候选，避免与 selected towers 重复。
    candidateTowers:(candidateTowers||[]).filter(
      tower=>!selectedTowerIds.includes(tower?.tower_id)
    ),
    selectedTowerIds,
    selectedTowers,
    panels,
    panelsWithoutPlaneIntersection:noIntersection,
    routeCoverageColours:routeCoverageColours(item),
  };
}

function coverageLine(label,summary,requiredLabel){
  if(!summary)return '<div class="list-row"><span>'+escapeHtml(label)+'</span><small>尚无复核结果</small></div>';
  return '<div class="list-row"><span><b>'+escapeHtml(label)+'</b>（需要 '+escapeHtml(requiredLabel)+'）'
    +'<br><small>航段长度 '+num(summary.length_m)+' m · 满足 '+num(summary.satisfied_length_m)+' m（'+pct(summary.satisfied_fraction)+'）'
    +' · 最小实际站址数 '+text(summary.minimum_distinct_site_count)
    +'</small><br><small>冗余不足 '+text(summary.under_redundant_sample_count)+' 点 · 未覆盖 '+text(summary.uncovered_sample_count)
    +' 点 · 证据不足 '+text(summary.unknown_sample_count)+' 点</small></span></div>';
}

function segmentList(title,segments){
  if(!segments?.length)return '';
  const rows=segments.slice(0,20).map(segment=>'<div class="list-row"><span>'+escapeHtml(title)+'</span><small>'
    +num(segment.route_offset_start_m)+' – '+num(segment.route_offset_end_m)+' m（'+num(segment.length_m)+' m）</small></div>').join('');
  return rows+(segments.length>20?'<div class="empty-note">另有 '+(segments.length-20)+' 段</div>':'');
}

function panelList(panels,altitudeLayerId='ALT-080'){
  if(!panels.length)return '<div class="empty-note">尚无选中的单面阵</div>';
  return panels.slice(0,40).map(panel=>{
    const plane=panel.altitude_plane_geometry||{};
    const geometryLine=panel.plane_intersection_status==='no_intersection'
      ?'80 m 平面：无有效交截（'+escapeHtml(plane.plane_intersection_reason||'no_intersection')+'）'
      :'80 m 平面：dz '+num(plane.dz_m)+' m · 真实物理水平范围 '+num(panel.horizontal_inner_radius_m)
        +' – '+num(panel.horizontal_outer_radius_m)+' m（后端交截，非斜距）';
    const displayLine=panel.plane_intersection_status==='no_intersection'
      ?''
      :'<br><small>地图最大物理覆盖范围：'+num(panel.horizontal_inner_radius_m)
        +' – '+num(panel.horizontal_outer_radius_m)+' m（'
        +escapeHtml(altitudeLayerId)+' 平面交截）</small>';
    return '<div class="list-row"><span><b>'+(RADAR_TYPE_LABELS[panel.radar_type]||panel.radar_type)
      +'</b> · 铁塔 '+escapeHtml(panel.tower_id)
      +'<br><small>方位角 '+num(panel.azimuth_deg)+'°（±'+num(panel.panel_half_width_deg)+'°）'
      +' · 雷达原点 '+(Number.isFinite(panel.radar_origin_egm2008_m)?num(panel.radar_origin_egm2008_m)+' m EGM2008':'未解析')
      +'</small><br><small>'+geometryLine+'</small>'+displayLine
      +'<br><small>覆盖 '+text(panel.coverage?.sample_count)+' 个复核点 · 满足 '+text(panel.coverage?.satisfied_sample_count)
      +' · 里程 '+num(panel.coverage?.first_distance_along_route_m)+'–'+num(panel.coverage?.last_distance_along_route_m)+' m</small></span></div>';
  }).join('')
    +(panels.length>40?'<div class="empty-note">另有 '+(panels.length-40)+' 个面阵</div>':'');
}

function deviceSummary(model){
  const types=model.deviceTypes||[];
  if(!types.length)return '<div class="empty-note">尚无两型雷达真实资料摘要</div>';
  return types.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.label)+'</b>'
    +'<br><small>最小斜距 '+num(item.min_slant_range_m)+' m · 最大斜距 '+num(item.max_slant_range_m)+' m（来源：'+escapeHtml(item.source_min_detection_distance)
    +' / '+escapeHtml(item.source_max_detection_distance)+'）</small>'
    +'<br><small>方位 '+escapeHtml(item.azimuth_coverage_raw)+' 俯仰 '+escapeHtml(item.elevation_coverage_raw)+'</small>'
    +'<br><small>精度：'+escapeHtml(item.azimuth_measurement_accuracy_raw)+' '+escapeHtml(item.range_measurement_accuracy_raw)
    +' · 更新：搜索 '+escapeHtml(item.search_update_interval_raw)+' 跟踪 '+escapeHtml(item.track_update_interval_raw)+'</small>'
    +'<br><small>RCS '+text(item.rcs_reference_m2)+' m² · Pd '+text(item.pd_reference)+' · Pfa '+text(item.pfa_reference)
    +'（已记录，<b>不进入几何布设公式</b>）</small></span></div>').join('');
}

/* ============================================================================
 * Round34-T07-B：地形视距（LOS）**只读证据**展示
 *
 * 硬约定：
 *  * 默认只消费通用快照里的**轻量状态**（不打开 DEM、不做 LOS 计算）；
 *  * 唯一会读 DEM 的入口是用户显式点击「执行地形 LOS 检查」；
 *  * 该证据**不进入**任何规划判定，也**不**改变任何冻结几何参数；
 *  * 前端**不重算**任何结论：三态计数、净空、明细一律原样转印后端字段；
 *  * 本轮**不**新增地图叠加图层，启动仍只显示 QGIS 在线底图。
 * ========================================================================== */

/** 地形 LOS 只读证据模型（纯函数，便于 node 环境测试）。 */
export function radarTerrainLosModel(flow){
  const status=(flow||{}).radar_terrain_los||{};
  //: 客户端最近一次显式检查的结果与按需载入的明细页；它们**只**来自显式请求。
  const evidence=(flow||{}).radar_terrain_los_diagnosis||null;
  const detailPage=(flow||{}).radar_terrain_los_details||null;
  const view=evidence||status||{};
  const counts=view.los_status_counts||{};
  const availability=view.origin_availability_counts||{};
  const currentness=view.currentness||status.currentness||{};
  return {
    status,
    evidence,
    detailPage,
    hasEvidence:Boolean(evidence),
    availableToCompute:status.available_to_compute===true,
    evidenceStatus:view.evidence_status||'not_requested',
    evidenceStatusLabel:RADAR_TERRAIN_LOS_STATE_TEXT[view.evidence_status]
      ||view.evidence_status_label||RADAR_TERRAIN_LOS_STATE_TEXT.not_requested,
    algorithmVersion:view.algorithm_version||status.algorithm_version||null,
    readMode:view.read_mode||status.read_mode||null,
    evaluationScope:view.evaluation_scope||null,
    evaluatedTowerCount:view.evaluated_tower_count||0,
    evaluatedProbeCount:view.evaluated_probe_count||0,
    candidateTowerCount:view.candidate_tower_count||0,
    candidateProbeCount:view.candidate_probe_count||0,
    clearCount:counts.clear||0,
    blockedCount:counts.blocked||0,
    unknownCount:counts.unknown||0,
    usableOriginCount:availability.usable||0,
    notEvaluableOriginCount:availability.not_evaluable||0,
    unknownOriginCount:availability.unknown||0,
    dem:view.dem||null,
    //: 已计算时用证据块里的真实高度层；未请求时用只读可用性里的高度层解析结果。
    altitudeLayer:view.altitude_layer||status.adapter_availability?.altitude_layer||null,
    sampling:view.sampling||null,
    bounds:view.bounds||null,
    inputFingerprint:view.input_fingerprint||null,
    currentness,
    stale:currentness.stale===true,
    currentnessReasons:Array.isArray(currentness.reasons_zh)?currentness.reasons_zh:[],
    failureReason:view.failure_reason||null,
    failureDetail:view.failure_detail||null,
    records:Array.isArray(view.records)?view.records:[],
    recordSampleCount:view.record_sample_count||0,
    recordSampleTruncated:view.record_sample_truncated===true,
    lowClearanceSamples:Array.isArray(view.low_clearance_samples)?view.low_clearance_samples:[],
    lowClearanceThresholdM:view.low_clearance_threshold_m,
    notEvaluated:Array.isArray(view.not_evaluated)?view.not_evaluated:[],
    disclaimer:view.user_disclaimer_zh||RADAR_TERRAIN_LOS_DISCLAIMER,
    dimensionSeparation:view.dimension_separation_zh||null,
    losSemantics:view.los_three_state_semantics_zh||null,
    sourceProvenance:view.source_provenance||null,
    detailRecordCount:detailPage&&Array.isArray(detailPage.records)
      ?detailPage.records.length:0,
    detailMatchedCount:detailPage?detailPage.matched_record_count:null,
    detailLimit:detailPage?detailPage.limit:null,
    detailMaxLimit:detailPage?detailPage.max_limit:null,
  };
}

function _losProbeRow(item){
  const clearance=Number.isFinite(item?.minimum_clearance_m)
    ?num(item.minimum_clearance_m):'—';
  return '<div class="gap-result"><b>'+escapeHtml(String(item?.tower_id||'—'))+'</b>'
    +' · probe '+escapeHtml(String(item?.probe_id||'—'))
    +' · '+statusBadge(String(item?.los_status||'unknown'),String(item?.los_status||'unknown'))
    +' · 最小净空 '+escapeHtml(String(clearance))+' m'
    +' · 水平距离 '+(Number.isFinite(item?.horizontal_distance_m)
      ?num(item.horizontal_distance_m):'—')+' m'
    +(item?.reason?' · 原因 '+escapeHtml(String(item.reason)):'')
    +'</div>';
}

/**
 * 地形 LOS 只读证据块 HTML。
 *
 * 中文文案必须包含固定声明：「地形视距分析仅为几何可视性证据，不代表实际雷达探测概率
 * 或设备可安装确认。」并且必须显式展示「规划候选资格」与「物理 LOS 可评价性」是两个
 * **不同维度**。
 */
export function renderRadarTerrainLosBlock(flow){
  const model=radarTerrainLosModel(flow);
  const dem=model.dem||{};
  const identity=dem.source_identity||{};
  const layer=model.altitudeLayer||{};
  const bounds=model.bounds||{};
  const openCount=Number.isFinite(dem.read_receipt?.open_count)
    ?dem.read_receipt.open_count:'—';
  const statusLine='<b>'+escapeHtml(RADAR_TERRAIN_LOS_TITLE)+'</b> '
    +statusBadge(model.evidenceStatus,
      RADAR_TERRAIN_LOS_STATE_TEXT[model.evidenceStatus]||'')
    +' · 只读证据（不参与规划判定）';
  const actionRow='<div class="button-row">'
    +'<button class="secondary" id="runRadarTerrainLos"'
    +(model.availableToCompute?'':' disabled')+'>执行地形 LOS 检查</button>'
    +'<button class="secondary" id="loadRadarTerrainLosDetails"'
    +(model.hasEvidence?'':' disabled')+'>载入逐 probe 明细（受限分页）</button></div>';
  const availabilityNote=model.availableToCompute?''
    :'<div class="parameter-note">当前不可计算：需要已配置的地形数据源（terrain_dtm）、'
      +'显式米制投影与真实铁塔站址。缺失一律 fail-closed，绝不猜默认数据源。</div>';
  if(!model.hasEvidence){
    const planned='<div class="parameter-note">尚未执行地形 LOS 检查。'
      +'打开本面板或读取项目状态<b>不会</b>自动读取 DEM，也不会触发任何 LOS 全量计算。</div>';
    const layerLine=layer.altitude_layer_id
      ?'<div class="parameter-note">当前权威高度层：<b>'+escapeHtml(String(layer.altitude_layer_id))
        +'</b>（'+(Number.isFinite(layer.nominal_altitude_m)?num(layer.nominal_altitude_m):'—')
        +' m · '+escapeHtml(String(layer.vertical_reference||'基准未知'))
        +' · 解析来源 '+escapeHtml(String(layer.resolution_source||'—'))+'）<br>'
        +'probe 高度将只取自该权威高度层的 nominal_altitude_m，'
        +'绝不使用走廊体素的代表高度。</div>':'';
    return '<h4>'+escapeHtml(RADAR_TERRAIN_LOS_TITLE)+'</h4>'
      +'<div class="parameter-note">'+statusLine+'</div>'
      +planned+layerLine+availabilityNote+actionRow
      +'<div class="parameter-note"><b>'+escapeHtml(model.disclaimer)+'</b></div>';
  }
  const countsLine='<div class="coverage-card">'
    +'<b>已评估结论</b>'
    +'<span>clear <b>'+model.clearCount+'</b> · blocked <b>'+model.blockedCount
    +'</b> · unknown <b>'+model.unknownCount+'</b>（共 '+model.evaluatedProbeCount+' 次评价）</span>'
    +'<span>已评估铁塔 '+model.evaluatedTowerCount+' / 候选站址 '+model.candidateTowerCount
    +' 座 · 已评估 probe '+model.evaluatedProbeCount+' / 候选采样点 '+model.candidateProbeCount+' 个</span>'
    +'<span>原点物理输入：可用（usable）<b>'+model.usableOriginCount
    +'</b> · 不可评价（not_evaluable）<b>'+model.notEvaluableOriginCount
    +'</b> · 证据不足（unknown）<b>'+model.unknownOriginCount+'</b></span></div>';
  const sourceLine='<div class="parameter-note">'
    +'数据来源：'+escapeHtml(String(identity.file_name||'—'))
    +' · 垂向基准 '+escapeHtml(String(dem.vertical_reference||'—'))
    +' · 读取模式 '+escapeHtml(String(model.readMode||'—'))+' · DEM 打开次数 '+openCount
    +'<br>算法版本 '+escapeHtml(String(model.algorithmVersion||'—'))
    +' · 评价范围 '+escapeHtml(String(model.evaluationScope||'—'))
    +'<br>高度基准：'+escapeHtml(String(layer.altitude_layer_id||'—'))
    +'（'+(Number.isFinite(layer.nominal_altitude_m)?num(layer.nominal_altitude_m):'—')
    +' m · '+escapeHtml(String(layer.vertical_reference||'基准未知'))+'）'
    +' · probe 高度来源 '+escapeHtml(String(layer.probe_height_source||'—'))
    +' · 代表高度使用='+(layer.representative_height_used===true?'是':'否')
    +'<br>采样 '+ (Number.isFinite(model.sampling?.sampling_interval_m)
      ?num(model.sampling.sampling_interval_m):'—')+' m'
    +' · 包络半径 '+(Number.isFinite(model.sampling?.envelope_radius_m)
      ?num(model.sampling.envelope_radius_m):'—')+' m'
    +' · 曲率修正='+(model.sampling?.curvature_correction_enabled===true?'启用':'关闭')
    +' · 折射='+(model.sampling?.refraction_enabled===true?'启用':'关闭（纯几何）')
    +'<br>输入指纹 <code>'+escapeHtml(String(model.inputFingerprint||'—'))+'</code></div>';
  const currentnessLine='<div class="parameter-note">'
    +'<b>证据时效（独立判断）</b>：'+escapeHtml(String(model.currentness.state||'—'))
    +' · 旧证据指纹 '+(model.currentness.prior_input_fingerprint
      ?'<code>'+escapeHtml(String(model.currentness.prior_input_fingerprint))+'</code>':'（无）')
    +'<br>'+(model.currentnessReasons.length
      ?model.currentnessReasons.map(escapeHtml).join('<br>')
      :'未提供可比较的旧证据，因此不宣称任何旧结果 stale。')
    +'<br>LOS 证据的 stale <b>不</b>传播到上游规划结果、P14/P15/P16 或任何既有容器；'
    +'本轮没有 ProjectState writer，也没有结果持久化。</div>';
  const staleWarning=model.stale
    ?'<div class="parameter-note"><b>旧证据已 stale：</b>下列结论来自旧输入，仅作参考，'
      +'不可作为当前地形可见性依据。</div>':'';
  const failureLine=model.failureReason
    ?'<div class="parameter-note"><b>计算失败：</b>'+escapeHtml(String(model.failureReason))
      +(model.failureDetail?'（'+escapeHtml(String(model.failureDetail))+'）':'')
      +'<br>失败<b>不</b>等于"无遮挡"：不会返回任何 clear 结论。</div>':'';
  const lowClearance=model.lowClearanceSamples.length
    ?'<div class="parameter-note"><b>低净空提示</b>（阈值 '
      +(Number.isFinite(model.lowClearanceThresholdM)?num(model.lowClearanceThresholdM):'—')
      +' m，仅提示、<b>不改变</b> LOS 三态阈值或算法）：<br>'
      +model.lowClearanceSamples.map(item=>'<b>'+escapeHtml(String(item.tower_id||'—'))+'</b>'
        +' · probe '+escapeHtml(String(item.probe_id||'—'))
        +' · 净空 '+(Number.isFinite(item.minimum_clearance_m)?num(item.minimum_clearance_m):'—')
        +' m').join('<br>')+'</div>':'';
  const sampleList=model.records.length
    ?'<div class="gap-results">'+model.records.map(_losProbeRow).join('')+'</div>'
    :'<div class="empty-note">本次评价没有可展示的样本记录。</div>';
  const sampleNote=model.recordSampleTruncated
    ?'<div class="parameter-note">仅展示前 '+model.recordSampleCount
      +' 条样本；这不是全部评价明细（默认摘要不隐含全部明细）。</div>':'';
  const detailBlock=model.detailPage
    ?'<div class="parameter-note">逐 probe 明细：匹配 '+String(model.detailMatchedCount)
      +' 条 · 本页 '+model.detailRecordCount+' 条 · 每页上限 '+String(model.detailMaxLimit)
      +'（分页读取，不重算、不重新打开 DEM，也不含完整地形剖面）</div>'
    :'';
  const boundaryLine='<div class="parameter-note">'
    +'本轮<b>未评估</b>：'+model.notEvaluated.slice(0,8).map(escapeHtml).join(' · ')
    +'（等）<br>'
    +escapeHtml(String(model.dimensionSeparation||''))+'<br>'
    +'默认摘要只含聚合统计与有限样本；逐 probe 明细需要显式请求，且受分页与总数上限约束。'
    +'完整地形剖面不进入 API 响应。</div>';
  const semanticsLine=model.losSemantics
    ?'<div class="parameter-note">clear：'+escapeHtml(String(model.losSemantics.clear||''))
      +'<br>blocked：'+escapeHtml(String(model.losSemantics.blocked||''))
      +'<br>unknown：'+escapeHtml(String(model.losSemantics.unknown||''))+'</div>':'';
  return '<h4>'+escapeHtml(RADAR_TERRAIN_LOS_TITLE)+'</h4>'
    +'<div class="parameter-note">'+statusLine+'</div>'
    +staleWarning+failureLine+countsLine+sourceLine+currentnessLine
    +availabilityNote+actionRow
    +disclosure('样本记录与低净空提示',sampleNote+lowClearance+sampleList)
    +detailBlock+semanticsLine+boundaryLine
    +'<div class="parameter-note"><b>'+escapeHtml(model.disclaimer)+'</b></div>';
}

/** 显式请求一次地形 LOS 只读诊断（唯一会读 DEM 的前端入口）。 */
export async function loadRadarTerrainLosDiagnosis({api,getFlow,setFlow,afterChange=()=>{},
                                                   payload=null}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarTerrainLosDiagnosis 需要 api / getFlow / setFlow 三个依赖');
  const body=payload&&typeof payload==='object'?payload:{};
  //: 若客户端已保存过旧证据，一并提交其输入指纹：服务端据此**独立**判断 stale。
  const previous=(getFlow()||{}).radar_terrain_los_diagnosis;
  if(previous&&previous.input_fingerprint&&body.prior_evidence==null){
    body.prior_evidence={
      input_fingerprint:previous.input_fingerprint,
      algorithm_version:previous.algorithm_version,
      evaluation_scope:previous.evaluation_scope,
    };
  }
  const evidence=await api(RADAR_TERRAIN_LOS_DIAGNOSE_ENDPOINT,{
    method:'POST',body:JSON.stringify(body),
  });
  const current=getFlow()||{};
  setFlow({...current,radar_terrain_los_diagnosis:evidence,
    radar_terrain_los_details:null});
  afterChange();
  return evidence;
}

/** 受限分页读取逐 probe 明细（只读服务端内存证据块，绝不重算）。 */
export async function loadRadarTerrainLosDetails({api,getFlow,setFlow,afterChange=()=>{},
                                                 filters=null}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarTerrainLosDetails 需要 api / getFlow / setFlow 三个依赖');
  const query={};
  const source=filters&&typeof filters==='object'?filters:{};
  for(const key of ['tower_id','route_id','altitude_layer_id','los_status','offset','limit']){
    const value=source[key];
    if(value!==undefined&&value!==null&&String(value)!=='')query[key]=String(value);
  }
  const suffix=Object.keys(query).length?'?'+new URLSearchParams(query).toString():'';
  const page=await api(RADAR_TERRAIN_LOS_DETAILS_ENDPOINT+suffix);
  const current=getFlow()||{};
  setFlow({...current,radar_terrain_los_details:page?.detail_page||null});
  afterChange();
  return page;
}

/** 只读状态：与通用快照同口径，**不打开 DEM**（用户显式刷新时使用）。 */
export async function loadRadarTerrainLosStatus({api,getFlow,setFlow,afterChange=()=>{}}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarTerrainLosStatus 需要 api / getFlow / setFlow 三个依赖');
  const status=await api(RADAR_TERRAIN_LOS_STATUS_ENDPOINT);
  const current=getFlow()||{};
  setFlow({...current,radar_terrain_los:status});
  afterChange();
  return status;
}

/* ---------------------------------------------------------------------------
 * Round34-T07-I2：地形 + 建筑联合 LOS 只读诊断
 *
 *  * ``/combined/status`` 与通用快照同口径：**不打开 DEM，也不打开 GPKG**；
 *  * ``/combined/diagnose`` 是**唯一**同时读两个真实来源的入口，且只在用户显式点击
 *    「开始联合诊断」时调用；失败只呈现中文错误，绝不渲染成"无遮挡"；
 *  * ``/combined/details`` 只读服务端内存证据块（受限分页，绝不重算）。
 * ------------------------------------------------------------------------- */

/** 联合 LOS 轻量状态（不打开任何数据集）。 */
export async function loadRadarCombinedLosStatus({api,getFlow,setFlow,afterChange=()=>{},
                                                  engineeringDemoMode=false}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarCombinedLosStatus 需要 api / getFlow / setFlow 三个依赖');
  const suffix=engineeringDemoMode?'?engineering_demo_mode=true':'';
  const status=await api(RADAR_COMBINED_LOS_STATUS_ENDPOINT+suffix);
  const current=getFlow()||{};
  setFlow({...current,radar_combined_los:status});
  afterChange();
  return status;
}

/** 显式请求一次联合 LOS 只读诊断（唯一会同时读 FABDEM 与 buildings v3 的前端入口）。 */
let radarCombinedLosRequestSequence=0;

export async function loadRadarCombinedLosDiagnosis({api,getFlow,setFlow,afterChange=()=>{},
                                                     payload=null}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarCombinedLosDiagnosis 需要 api / getFlow / setFlow 三个依赖');
  const body=payload&&typeof payload==='object'?{...payload}:{};
  const requestSequence=++radarCombinedLosRequestSequence;
  body.request_id=String(body.request_id||('radar-combined-ui-'+requestSequence));
  //: 若客户端已保存过旧证据，一并提交其输入指纹：服务端据此**独立**判断 stale。
  const previous=(getFlow()||{}).radar_combined_los_diagnosis;
  if(previous&&previous.input_fingerprint&&body.prior_evidence==null){
    body.prior_evidence={
      input_fingerprint:previous.input_fingerprint,
      schema_version:previous.schema_version,
      algorithm_version:previous.algorithm_version,
      fingerprint_schema_version:previous.fingerprint_schema_version,
      evaluation_scope:previous.evaluation_scope,
    };
  }
  const starting=getFlow()||{};
  setFlow({...starting,radar_combined_los_diagnosis:null,radar_combined_los_details:null,
    radar_combined_los_request:{request_id:body.request_id,state:'running',
      sequence:requestSequence}});
  afterChange();
  try{
    const evidence=await api(RADAR_COMBINED_LOS_DIAGNOSE_ENDPOINT,{
      method:'POST',body:JSON.stringify(body),
    });
    if(requestSequence!==radarCombinedLosRequestSequence)
      return {...evidence,ui_superseded:true};
    const current=getFlow()||{};
    setFlow({...current,radar_combined_los_diagnosis:evidence,
      radar_combined_los_details:null,radar_combined_los_request:null});
    afterChange();
    return evidence;
  }catch(error){
    if(requestSequence!==radarCombinedLosRequestSequence)
      return {evidence_status:'superseded',ui_superseded:true};
    const current=getFlow()||{};
    setFlow({...current,radar_combined_los_diagnosis:null,
      radar_combined_los_details:null,radar_combined_los_request:null});
    afterChange();
    throw error;
  }
}

/** 受限分页读取逐射线联合明细（只读服务端内存证据块，绝不重算）。 */
export async function loadRadarCombinedLosDetails({api,getFlow,setFlow,afterChange=()=>{},
                                                  filters=null}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarCombinedLosDetails 需要 api / getFlow / setFlow 三个依赖');
  const query={};
  const expectedEvidence=(getFlow()||{}).radar_combined_los_diagnosis||null;
  const source=filters&&typeof filters==='object'?filters:{};
  for(const key of ['tower_id','route_id','combined_los_status','building_domain_state',
    'offset','limit']){
    const value=source[key];
    if(value!==undefined&&value!==null&&String(value)!=='')query[key]=String(value);
  }
  if(expectedEvidence?.request_id)query.request_id=String(expectedEvidence.request_id);
  if(expectedEvidence?.input_fingerprint)
    query.input_fingerprint=String(expectedEvidence.input_fingerprint);
  if(expectedEvidence?.generation!=null)query.generation=String(expectedEvidence.generation);
  const suffix=Object.keys(query).length?'?'+new URLSearchParams(query).toString():'';
  const page=await api(RADAR_COMBINED_LOS_DETAILS_ENDPOINT+suffix);
  const current=getFlow()||{};
  const currentEvidence=current.radar_combined_los_diagnosis||null;
  const responseMatches=Boolean(expectedEvidence&&currentEvidence
    &&expectedEvidence.request_id===currentEvidence.request_id
    &&expectedEvidence.input_fingerprint===currentEvidence.input_fingerprint
    &&expectedEvidence.generation===currentEvidence.generation
    &&page?.request_id===currentEvidence.request_id
    &&page?.input_fingerprint===currentEvidence.input_fingerprint
    &&page?.generation===currentEvidence.generation);
  if(!responseMatches)return page;
  setFlow({...current,radar_combined_los_details:{
    ...(page?.detail_page||{}),request_id:page.request_id,
    input_fingerprint:page.input_fingerprint,generation:page.generation,
  }});
  afterChange();
  return page;
}

/**
 * 任务卡 HTML（Step05 独立任务卡；不重构其它页面）。
 */
export function renderRadarSurveillanceLayoutPanel(flow){  const model=radarLayoutModel(flow);
  const item=model.item;
  const readiness=model.readiness||{};
  const origin=model.radarOrigin||{};
  const parameters=model.parameters||{};
  const source=model.deviceSource||{};
  const buffer=(model.landMask?.coastal_uncertainty_buffer||{});
  const demoWarning=model.demoPreviewOnly
    ?'<div class="parameter-note"><b>'+escapeHtml(model.previewWarning||RADAR_DEMO_PREVIEW_WARNING)
      +'</b><br><small>工程标识：not_for_operational_use=true · not_for_safety_claim=true'
      +' · not_for_final_confirmed_plan=true（不用于运行、不用于安全声明、不作为最终确认方案）</small></div>'
    :'';

  const readinessBlock='<div class="parameter-note">'
    +'<b>'+escapeHtml(RADAR_LAYOUT_PROPOSAL_TITLE)+'</b>'
    +' · 模型范围 '+escapeHtml(RADAR_LAYOUT_MODEL_SCOPE)
    +' · 算法版本 '+escapeHtml(String(readiness.algorithm_version||RADAR_LAYOUT_VERSION))+'<br>'
    +'航路：'+(readiness.passed_operational_route_count||0)+' 条已发布（共 '+(readiness.operational_route_count||0)+' 条）'
    +' · 固定高度层 '+escapeHtml(item?.altitude_layer_id||readiness.fixed_altitude?.altitude_layer_id||'尚未选择高度层')
    +'（'+(Number.isFinite(item?.altitude_m)?num(item.altitude_m):num(readiness.fixed_altitude?.altitude_m))+' m，'
    +escapeHtml(referenceLabel(item?.vertical_reference||readiness.fixed_altitude?.vertical_reference))+'）<br>'
    +'<b>航路采样高度</b>：'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.route_altitude)+'<br>'
    +'铁塔：'+(readiness.tower_count||0)+' 座 · 已解析雷达原点 '+(readiness.tower_with_resolved_radar_base_count||0)+' 座'
    +' · 塔顶障碍事实 '+(readiness.tower_obstacle_profile_count||0)+' 条<br>'
    +'<b>雷达原点</b>：'+escapeHtml(RADAR_ORIGIN_BASIS_TEXT[origin.radar_origin_basis]||origin.radar_origin_basis||'塔顶 EGM2008 正高')
    +'（'+escapeHtml(RADAR_INSTALLATION_TEXT[origin.installation_assumption]||origin.installation_assumption||'相位中心位于塔顶')
    +' · 工程确认='+(origin.engineering_confirmed===true?'是':'否')+'）<br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.radar_origin)+'</small><br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.vertical_delta)+'</small><br>'
    +'历史兼容挂高：<b>不参与本版几何</b>'
    +(Number.isFinite(model.legacyMountHeight?.radar_mount_height_m)
      ?' 记录值 '+num(model.legacyMountHeight.radar_mount_height_m)+' m · 仅作历史读取'
      :' 未配置 · 不影响几何')+'<br>'
    +escapeHtml(String(item?.altitude_layer_id||readiness.fixed_altitude?.altitude_layer_id||'高度层'))
    +'：'+statusBadge(readiness.fixed_altitude?.present?(readiness.fixed_altitude?.confirmed?'passed':'pending_confirmation'):'missing_data')
    +' · 米制变换（EPSG:32651） '+statusBadge(readiness.metric_transform?.status||'not_checked')
    +' · 求解器 '+statusBadge(readiness.solver?.available?'passed':'solver_unavailable')
    +(readiness.solver?.available?'（'+escapeHtml(String(readiness.solver.scipy_version||'—'))+'）'
      :'（'+escapeHtml(readiness.solver?.reason||'求解器不可用；绝不退化为贪心近似')+'）')+'<br>'
    +'陆域掩膜：'+statusBadge(model.landMaskReady?'passed':'missing_data')
    +(model.landMaskReady
      ?' '+escapeHtml(model.landMask?.layer_name||model.landMaskProvenance?.layer_name||'—')
        +' · '+escapeHtml(model.landMask?.source_crs||model.landMaskProvenance?.source_crs||'坐标基准未知')
        +' · 判定依据 '+escapeHtml(SURFACE_BASIS_TEXT[model.landMask?.classification_basis||model.landMaskProvenance?.classification_basis]||model.landMask?.classification_basis||'显式多边形')
      :' '+escapeHtml(model.landMaskReason||'未配置显式陆域 Polygon 来源')
        +'（地表类别保持未知并 fail-closed，绝不按海域处理，也不用 DEM 无数据推断海洋）')+'<br>'
    +'海岸不确定带：'+num(Number.isFinite(buffer.coastal_uncertainty_buffer_m)
      ?buffer.coastal_uncertainty_buffer_m:item?.parameters?.coastal_uncertainty_buffer_m)+' m · '
    +escapeHtml(PARAMETER_ORIGIN_TEXT[buffer.parameter_origin]||buffer.parameter_origin||'工程假设')
    +' · 已确认='+(buffer.confirmed===true?'是':'否')
    +'<br><small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.coastal_buffer)+'</small><br>'
    +'采样参数：优化 '+num(parameters.optimization_sample_spacing_m||25)+' m'
    +' · 复核 '+num(parameters.validation_sample_spacing_m||5)+' m'
    +' · 最大细化轮次 '+text(parameters.max_refinement_rounds==null?3:parameters.max_refinement_rounds)+'<br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.plane_radii)+'</small><br>'
    +(model.readinessBlockers.length
      ?'<b>准备情况阻断</b>：'+model.readinessBlockers.map(escapeHtml).join(' · ')+'<br>':'')
    +escapeHtml(RADAR_LAYOUT_PROPOSAL_ONLY_NOTE)+'</div>';

  const policyForm='<div class="form-grid">'
    +'<label>海岸不确定带 (m)<input class="panel-input" type="number" min="0" id="radarCoastalBuffer" value="'
    +(Number.isFinite(buffer.coastal_uncertainty_buffer_m)?buffer.coastal_uncertainty_buffer_m:'')+'"></label>'
    +'<label>陆域图层名<input class="panel-input" id="radarLandMaskLayer" value="'
    +escapeHtml(model.landMask?.layer_name||model.landMaskProvenance?.layer_name||'')+'"></label>'
    +'<label>历史兼容工程示例挂高 (m)<input class="panel-input" type="number" id="radarMountHeight" value="'
    +(Number.isFinite(model.legacyMountHeight?.radar_mount_height_m)?model.legacyMountHeight.radar_mount_height_m:'')
    +'" placeholder="留空=未配置" disabled></label>'
    +'</div>'
    +'<div class="parameter-note">海岸不确定带是<b>工程保守假设（未确认）</b>，不是边界数据真实精度，'
    +'也不等同于 5 m 复核分辨率。历史挂高字段只作兼容读取，'
    +'本版雷达原点恒等于塔顶 EGM2008 正高，该字段不参与任何几何。</div>'
    +'<label class="check-row"><input type="checkbox" id="radarAllowMixed" '+(item?.stage==='radar_i_plus_radar_ii'?'checked':'')+'> 允许仅Ⅰ型被证明不可行后回退 Ⅰ型+Ⅱ型</label>'
    +'<label class="check-row"><input type="checkbox" id="radarDemoPreviewOnly" '
    +(model.demoPreviewOnly?'checked':'')+'> 使用当前 Theta* V2 候选航路进行演示预览</label>'
    +'<div class="parameter-note">基于真实候选航路执行雷达几何初步划设。'
    +'结果为 proposal-only，不代表运行航路已经发布。</div>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveRadarSurveillancePolicy">保存划设参数</button>'
    +'<button class="primary" id="evaluateRadarSurveillanceLayout">运行初步划设</button>'
    +'<button class="secondary" id="loadRadarSurveillanceDetail">载入逐点明细</button></div>';

  if(!item){
    return '<h3>'+escapeHtml(RADAR_LAYOUT_TITLE)+' '+statusBadge(model.status)+'</h3>'
      +demoWarning+readinessBlock
      +renderRadarTerrainLosBlock(flow)
      +renderRadarCombinedLosBlock(flow)
      +policyForm
      +'<div class="empty-note">尚未运行「监视雷达初步划设」。本任务不会在后台自动生成，也不会自动 Apply。</div>';
  }

  const solverBlock='<div class="flow-summary">'
    +'阶段：'+escapeHtml(item.stage_label||item.stage||'—')
    +' · 求解器 '+escapeHtml(model.solver?.name||'—')+'（'+escapeHtml(model.solver?.library||'—')+'）'
    +' · 状态 '+statusBadge(model.solverStatus||'not_run')+'（'+escapeHtml(model.solverLabel||'—')+'）<br>'
    +'<small>工程标识：optimality_proven='+String(model.optimalityProven)
    +' · infeasibility_proven='+String(model.infeasibilityProven)
    +' · mip_gap '+(model.solver?.mip_gap==null?'—':Number(model.solver.mip_gap).toExponential(2))+'</small>'
    +'<br>'+escapeHtml(model.solver?.message||'')+'</div>';

  const counts='<div class="coverage-card"><b>面阵与站址</b>'
    +'<span>单面阵总数 '+text(model.panelCount)+' = Ⅰ型 '+text(model.radarICount)+' + Ⅱ型 '+text(model.radarIICount)+'</span>'
    +'<span>选中铁塔 '+text(model.towerCount)+' / 候选铁塔 '+text(model.candidateTowerCount)
    +' · 候选面阵 '+text(model.candidatePanelCount)+'</span></div>';

  const physicalDisplayNote='<div class="parameter-note"><b>地图雷达扇区采用当前固定高度平面的最大物理覆盖范围</b>：'
    +'直接绘制后端水平交截半径；设备斜距和航路聚焦审计范围都不作为地图半径。</div>';

  const coverageBlock=coverageLine('陆地航路',model.land,REQUIRED_SITE_COUNT_LABELS.land)
    +coverageLine('海上航路',model.sea,REQUIRED_SITE_COUNT_LABELS.sea)
    +coverageLine('海岸不确定带（按陆地处理）',model.coastalUncertain,REQUIRED_SITE_COUNT_LABELS.coastal_uncertain)
    +coverageLine('未知地表（fail-closed）',model.unknown,'未知：不得自动按海域处理');

  const refinementBlock=(model.refinementRounds||[]).map(round=>'<div class="list-row"><span>第 '
    +(round.round_index+1)+' 轮复核（'+num(round.spacing_m)+' m）</span><small>违反点 '+text(round.violation_count)
    +' · 该轮面阵数 '+text(round.panel_count)+'</small></div>').join('')
    +((model.refinementRounds||[]).length?'':'<div class="empty-note">尚无复核轮次记录</div>');

  const infeasibleBlock=model.infeasibilityReasons.length
    ?'<div class="parameter-note"><b>不可行/未完成原因</b><br>'+model.infeasibilityReasons.slice(0,20).map(escapeHtml).join('<br>')+'</div>'
    :'';
  const unknownBlock=model.unknownEvidence.length
    ?'<div class="parameter-note"><b>证据不足项</b><br>'+model.unknownEvidence.slice(0,20).map(entry=>escapeHtml(entry.detail||entry.reason_code||entry.reason||'')).join('<br>')+'</div>'
    :'';
  //: Round29-K：只有**已证明**的物理限制才是 managed 缺口（事实缺口，不满足、不可由 P16
  //: 消除）；搜索未完成 / 细化未完成 / 未解析 / 过时**必须**与它显式区分。
  const managedGapBlock=model.terminalManagedGap
    ?'<div class="parameter-note cns-radar-managed-gap" data-radar-managed-gap="confirmed">'
      +'<b>已证明的物理限制（managed physical gap）</b><br>'
      +'Radar 划设 authority 已证明当前约束下不可行（'
      +escapeHtml(model.solverLabel||'已证明不可行')+'）。'
      +'缺口原因：'+escapeHtml(model.gapReasonLabel||'未给出原因')+'<br>'
      +'该缺口是<b>事实缺口</b>：不计为满足，也不由 CNS 设施规划（P16）再次枚举雷达候选面板'
      +'（p16_actionable=false）。它不是系统错误，也不改变主要威胁（合作无人机 / RID）的判定。'
      +'</div>'
    :(model.searchIncomplete
      ?'<div class="parameter-note cns-radar-managed-gap" data-radar-managed-gap="not_confirmed">'
        +'<b>搜索未完成 / 证据不足：不是已确认的 managed 缺口</b><br>'
        +'当前结果状态为 '+escapeHtml(model.status||'未计算')
        +'：它既不是"已证明不可行"，也不是"已满足"。请先重跑 Radar 划设，'
        +'绝不把搜索未完成当作已确认的物理限制。'
        +(model.gapReasonLabel?'<br>原因：'+escapeHtml(model.gapReasonLabel):'')
        +'</div>'
      :'');

  // V1.1：扇区不画必须说清原因（斜距/平面无交截），绝不静默少画。
  const noIntersectionNote=(()=>{
    const count=model.selectedPanels.filter(
      panel=>panel.plane_intersection_status==='no_intersection'
    ).length;
    if(!count)return '';
    const first=model.selectedPanels.find(
      panel=>panel.plane_intersection_status==='no_intersection'
    );
    return '<div class="parameter-note"><b>'
      +count+' 个已选单面阵在 80 m 平面无有效交截</b>（'
      +escapeHtml(first?.altitude_plane_geometry?.plane_intersection_reason||'no_intersection')
      +'）：这些 panel 的方向扇区不会被绘制 —— 斜距绝不当作水平半径使用。</div>';
  })();

  return '<h3>'+escapeHtml(RADAR_LAYOUT_TITLE)+' '+statusBadge(model.status)+'</h3>'
    +demoWarning+readinessBlock
    +solverBlock
    +counts
    +physicalDisplayNote
    +policyForm
    +'<h4>两型雷达真实资料摘要</h4>'
    +'<div class="parameter-note">来源：'+escapeHtml(source.title||'—')+' · SHA-256 '+escapeHtml((source.sha256||'—').slice(0,16))
    +'… · 只读（source_modified=false）</div>'
    +'<div class="scroll-list cns-input-list">'+deviceSummary(model)+'</div>'
    +'<h4>覆盖复核（'+num(parameters.validation_sample_spacing_m||5)+' m 独立复核）</h4>'
    +'<div class="gap-results">'+coverageBlock+'</div>'
    +'<h4>未覆盖 / 冗余不足 / 证据不足连续段</h4>'
    +'<div class="gap-results">'+segmentList('未覆盖',model.uncoveredSegments)
    +segmentList('冗余不足',model.underRedundantSegments)
    +segmentList('证据不足',model.unknownSegments)
    +((model.uncoveredSegments.length+model.underRedundantSegments.length+model.unknownSegments.length)?'':'<div class="empty-note">无连续违反段</div>')+'</div>'
    +'<h4>连续覆盖复核轮次</h4><div class="gap-results">'+refinementBlock+'</div>'
    +'<h4>选中的铁塔与单面阵</h4>'
    +'<div class="scroll-list cns-input-list">'+panelList(
      model.selectedPanels,item.altitude_layer_id||'ALT-080'
    )+'</div>'
    +noIntersectionNote+managedGapBlock+infeasibleBlock+unknownBlock
    //: Round34-T07-B：地形 LOS 只读证据块（**不进入**上面任何规划结论）。
    +renderRadarTerrainLosBlock(flow)
    //: Round34-T07-I2：地形 + 建筑联合 LOS 只读证据块（同样**不进入**规划结论）。
    +renderRadarCombinedLosBlock(flow);
}

/**
 * B5X（Phase4）：按需载入**逐点明细**（只读 GET ``/api/radar-surveillance-layout``）。
 *
 * 通用 workflow 快照只带**有界摘要**（状态 / 计数 / 求解器 / 覆盖率），逐 sample /
 * panel 明细统一外置为 canonical artifact。因此明细只在用户显式点击「载入逐点明细」
 * 时读取，并挂在 ``flow.radar_surveillance_layout.detail``（展示模型已经消费该字段）。
 *
 * 读取失败**不**改任何业务状态：错误由调用方按中文业务提示呈现（明细不可用 /
 * 需要重新计算），绝不把"读不到"渲染成"未覆盖 0 段"。
 */
export async function loadRadarSurveillanceDetail({api,getFlow,setFlow,afterChange=()=>{},routeId=null}={}){
  if(typeof api!=='function'||typeof getFlow!=='function'||typeof setFlow!=='function')
    throw new Error('loadRadarSurveillanceDetail 需要 api / getFlow / setFlow 三个依赖');
  const id=routeId==null?'':String(routeId).trim();
  const url=id?RADAR_LAYOUT_ENDPOINT+'?'+new URLSearchParams({route_id:id}):RADAR_LAYOUT_ENDPOINT;
  const response=await api(url);
  const items=Array.isArray(response?.items)?response.items:[];
  const detail=[...items].reverse().find(item=>
    item?.demo_preview_only===true&&item?.status!=='stale'
  )||items[items.length-1]||response;
  const current=getFlow()||{};
  const layout={...(current.radar_surveillance_layout||{}),detail};
  setFlow({...current,radar_surveillance_layout:layout});
  afterChange();
  return detail;
}

export default renderRadarSurveillanceLayoutPanel;
