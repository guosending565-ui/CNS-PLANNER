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
import {escapeHtml,statusBadge,statusText} from './common.js';

export const RADAR_LAYOUT_ENDPOINT='/api/radar-surveillance-layout';
export const RADAR_LAYOUT_EVALUATE_ENDPOINT='/api/radar-surveillance-layout/evaluate';
export const RADAR_LAYOUT_READINESS_ENDPOINT='/api/radar-surveillance-layout/readiness';
export const RADAR_POLICY_ENDPOINT='/api/radar-surveillance-policy';

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
  route_altitude:'固定巡航高度 ALT-080 / 80 m EGM2008（不是地形高程）',
  vertical_delta:'vertical_delta = 目标 − 雷达原点；目标低于雷达原点 ⇒ 负仰角 ⇒ 不覆盖',
  radar_origin:'雷达原点 = tower_top_orthometric_m（塔顶即相位中心，不再叠加挂高）',
  coastal_buffer:'海岸不确定带：工程假设 / 未确认，不是数据真实精度',
  plane_radii:'斜距 ≠ 80 m 平面水平半径；半径由后端交截计算提供',
};

const pct=value=>Number.isFinite(value)?(value*100).toFixed(1)+'%':'—';
const num=(value,digits=1)=>Number.isFinite(value)?value.toFixed(digits):'—';
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
  const item=detail||(layout.items||[])[0]||null;
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
    const displayInner=Number.isFinite(display.display_inner_radius_m)
      ?display.display_inner_radius_m:inner;
    const displayOuter=Number.isFinite(display.display_outer_radius_m)
      ?display.display_outer_radius_m:outer;
    // 只消费后端水平半径：斜距绝不进入前端几何。
    if(panel.plane_intersection_status==='no_intersection'
      ||!Number.isFinite(inner)||!Number.isFinite(outer)||outer<=0
      ||!Number.isFinite(displayInner)||!Number.isFinite(displayOuter)||displayOuter<=0){
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
      displayRadiusInnerM:displayInner,
      displayRadiusOuterM:displayOuter,
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

function panelList(panels){
  if(!panels.length)return '<div class="empty-note">尚无选中的单面阵</div>';
  return panels.slice(0,40).map(panel=>{
    const plane=panel.altitude_plane_geometry||{};
    const display=panel.display_geometry||{};
    const geometryLine=panel.plane_intersection_status==='no_intersection'
      ?'80 m 平面：无有效交截（'+escapeHtml(plane.plane_intersection_reason||'no_intersection')+'）'
      :'80 m 平面：dz '+num(plane.dz_m)+' m · 真实物理水平范围 '+num(panel.horizontal_inner_radius_m)
        +' – '+num(panel.horizontal_outer_radius_m)+' m（后端交截，非斜距）';
    const displayLine=panel.plane_intersection_status==='no_intersection'
      ?''
      :'<br><small>地图航路聚焦显示范围：'
        +num(Number.isFinite(display.display_inner_radius_m)
          ?display.display_inner_radius_m:panel.horizontal_inner_radius_m)
        +' – '+num(Number.isFinite(display.display_outer_radius_m)
          ?display.display_outer_radius_m:panel.horizontal_outer_radius_m)
        +' m（非设备最大探测边界）</small>';
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

/**
 * 任务卡 HTML（Step05 独立任务卡；不重构其它页面）。
 */
export function renderRadarSurveillanceLayoutPanel(flow){
  const model=radarLayoutModel(flow);
  const item=model.item;
  const readiness=model.readiness||{};
  const origin=model.radarOrigin||{};
  const parameters=model.parameters||{};
  const source=model.deviceSource||{};
  const buffer=(model.landMask?.coastal_uncertainty_buffer||{});
  const demoWarning=model.demoPreviewOnly
    ?'<div class="parameter-note"><b>'+escapeHtml(model.previewWarning||RADAR_DEMO_PREVIEW_WARNING)
      +'</b><br><small>not_for_operational_use=true · not_for_safety_claim=true · not_for_final_confirmed_plan=true</small></div>'
    :'';

  const readinessBlock='<div class="parameter-note">'
    +'<b>'+escapeHtml(RADAR_LAYOUT_PROPOSAL_TITLE)+'</b> · model_scope '+escapeHtml(RADAR_LAYOUT_MODEL_SCOPE)
    +' · algorithm_version '+escapeHtml(String(readiness.algorithm_version||RADAR_LAYOUT_VERSION))+'<br>'
    +'航路：'+(readiness.passed_operational_route_count||0)+' 条已发布（共 '+(readiness.operational_route_count||0)+' 条）'
    +' · 固定高度层 '+escapeHtml(item?.altitude_layer_id||readiness.fixed_altitude?.altitude_layer_id||'ALT-080')
    +'（'+(Number.isFinite(item?.altitude_m)?num(item.altitude_m):num(readiness.fixed_altitude?.altitude_m))+' m，'
    +escapeHtml(item?.vertical_reference||readiness.fixed_altitude?.vertical_reference||'egm2008_orthometric')+'）<br>'
    +'<b>航路采样高度</b>：'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.route_altitude)+'<br>'
    +'铁塔：'+(readiness.tower_count||0)+' 座 · 已解析雷达原点 '+(readiness.tower_with_resolved_radar_base_count||0)+' 座'
    +' · tower_obstacle_profiles '+(readiness.tower_obstacle_profile_count||0)+' 条<br>'
    +'<b>雷达原点</b>：'+escapeHtml(origin.radar_origin_basis||'tower_top_orthometric_m')
    +'（'+escapeHtml(origin.installation_assumption||'radar_phase_center_at_tower_top')
    +' · engineering_confirmed='+String(origin.engineering_confirmed===true)+'）<br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.radar_origin)+'</small><br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.vertical_delta)+'</small><br>'
    +'legacy 挂高：'+statusBadge('legacy_not_used_by_v1_1')
    +(Number.isFinite(model.legacyMountHeight?.radar_mount_height_m)
      ?' 记录值 '+num(model.legacyMountHeight.radar_mount_height_m)+' m · 不参与 V1.1 几何'
      :' 未配置 · 不影响 V1.1 几何')+'<br>'
    +'ALT-080：'+statusBadge(readiness.fixed_altitude?.present?(readiness.fixed_altitude?.confirmed?'passed':'pending_confirmation'):'missing_data')
    +' · EPSG:32651 米制变换 '+statusBadge(readiness.metric_transform?.status||'not_checked')
    +' · solver '+statusBadge(readiness.solver?.available?'passed':'solver_unavailable')
    +(readiness.solver?.available?'（'+escapeHtml(String(readiness.solver.scipy_version||'—'))+'）'
      :'（'+escapeHtml(readiness.solver?.reason||'scipy 不可用；绝不退化为 greedy')+'）')+'<br>'
    +'land mask：'+statusBadge(model.landMaskReady?'passed':'missing_data')
    +(model.landMaskReady
      ?' '+escapeHtml(model.landMask?.layer_name||model.landMaskProvenance?.layer_name||'—')
        +' · '+escapeHtml(model.landMask?.source_crs||model.landMaskProvenance?.source_crs||'CRS 未知')
        +' · '+escapeHtml(model.landMask?.classification_basis||model.landMaskProvenance?.classification_basis||'explicit_polygon')
      :' '+escapeHtml(model.landMaskReason||'未配置显式陆域 Polygon 来源')
        +'（surface_class 保持 unknown，fail-closed，绝不按 sea 处理，也不用 DEM NoData 推断海洋）')+'<br>'
    +'海岸不确定带：'+num(Number.isFinite(buffer.coastal_uncertainty_buffer_m)
      ?buffer.coastal_uncertainty_buffer_m:item?.parameters?.coastal_uncertainty_buffer_m)+' m · '
    +escapeHtml(buffer.parameter_origin||'engineering_assumption')
    +' · confirmed='+String(buffer.confirmed===true)
    +'<br><small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.coastal_buffer)+'</small><br>'
    +'采样参数：optimization '+num(parameters.optimization_sample_spacing_m||25)+' m'
    +' · validation '+num(parameters.validation_sample_spacing_m||5)+' m'
    +' · max refinement rounds '+text(parameters.max_refinement_rounds==null?3:parameters.max_refinement_rounds)+'<br>'
    +'<small>'+escapeHtml(RADAR_LAYOUT_V1_1_SEMANTICS.plane_radii)+'</small><br>'
    +(model.readinessBlockers.length
      ?'<b>readiness 阻断</b>：'+model.readinessBlockers.map(escapeHtml).join(' · ')+'<br>':'')
    +escapeHtml(RADAR_LAYOUT_PROPOSAL_ONLY_NOTE)+'</div>';

  const policyForm='<div class="form-grid">'
    +'<label>海岸不确定带 (m)<input class="panel-input" type="number" min="0" id="radarCoastalBuffer" value="'
    +(Number.isFinite(buffer.coastal_uncertainty_buffer_m)?buffer.coastal_uncertainty_buffer_m:30)+'"></label>'
    +'<label>陆域图层名<input class="panel-input" id="radarLandMaskLayer" value="'
    +escapeHtml(model.landMask?.layer_name||model.landMaskProvenance?.layer_name||'zhejiang_boundary')+'"></label>'
    +'<label>legacy 工程示例挂高 (m)<input class="panel-input" type="number" id="radarMountHeight" value="'
    +(Number.isFinite(model.legacyMountHeight?.radar_mount_height_m)?model.legacyMountHeight.radar_mount_height_m:'')
    +'" placeholder="留空=未配置" disabled></label>'
    +'</div>'
    +'<div class="parameter-note">海岸不确定带是<b>工程保守假设（未确认）</b>，不是边界数据真实精度，'
    +'也不等同于 5 m validation resolution。legacy 挂高字段只作历史兼容读取，'
    +'V1.1 雷达原点恒等于 tower_top_orthometric_m，该字段不参与任何几何。</div>'
    +'<label class="check-row"><input type="checkbox" id="radarAllowMixed" '+(item?.stage==='radar_i_plus_radar_ii'?'checked':'')+'> 允许 I-only 被证明不可行后回退 I型+II型</label>'
    +'<div class="button-row">'
    +'<button class="secondary" id="saveRadarSurveillancePolicy">保存划设参数</button>'
    +'<button class="primary" id="evaluateRadarSurveillanceLayout">运行初步划设</button>'
    +'<button class="secondary" id="loadRadarSurveillanceDetail">载入逐点明细</button></div>';

  if(!item){
    return '<h3>'+escapeHtml(RADAR_LAYOUT_TITLE)+' '+statusBadge(model.status)+'</h3>'
      +demoWarning+readinessBlock+policyForm
      +'<div class="empty-note">尚未运行「监视雷达初步划设」。本任务不会在后台自动生成，也不会自动 Apply。</div>';
  }

  const solverBlock='<div class="flow-summary">'
    +'stage：'+escapeHtml(item.stage_label||item.stage||'—')
    +' · solver '+escapeHtml(model.solver?.name||'—')+'（'+escapeHtml(model.solver?.library||'—')+'）'
    +' · 状态 '+statusBadge(model.solverStatus||'not_run')+'（'+escapeHtml(model.solverLabel||'—')+'）<br>'
    +'optimality_proven='+String(model.optimalityProven)+' · infeasibility_proven='+String(model.infeasibilityProven)
    +' · mip_gap '+(model.solver?.mip_gap==null?'—':Number(model.solver.mip_gap).toExponential(2))
    +'<br>'+escapeHtml(model.solver?.message||'')+'</div>';

  const counts='<div class="coverage-card"><b>面阵与站址</b>'
    +'<span>单面阵总数 '+text(model.panelCount)+' = Ⅰ型 '+text(model.radarICount)+' + Ⅱ型 '+text(model.radarIICount)+'</span>'
    +'<span>选中铁塔 '+text(model.towerCount)+' / 候选铁塔 '+text(model.candidateTowerCount)
    +' · 候选面阵 '+text(model.candidatePanelCount)+'</span></div>';

  const routeFocusedDisplayNote='<div class="parameter-note"><b>地图雷达扇区采用航路聚焦显示</b>：'
    +'扇区外缘仅绘制至该面阵实际覆盖的最远航路点附近，用于提高方案可读性。'
    +'设备真实探测边界及后端规划约束未被截断。</div>';

  const coverageBlock=coverageLine('陆地航路',model.land,REQUIRED_SITE_COUNT_LABELS.land)
    +coverageLine('海上航路',model.sea,REQUIRED_SITE_COUNT_LABELS.sea)
    +coverageLine('海岸不确定带（按陆地处理）',model.coastalUncertain,REQUIRED_SITE_COUNT_LABELS.coastal_uncertain)
    +coverageLine('未知地表（fail-closed）',model.unknown,'未知：不得自动按 sea 处理');

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
    +routeFocusedDisplayNote
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
    +'<div class="scroll-list cns-input-list">'+panelList(model.selectedPanels)+'</div>'
    +noIntersectionNote+infeasibleBlock+unknownBlock;
}

export default renderRadarSurveillanceLayoutPanel;
