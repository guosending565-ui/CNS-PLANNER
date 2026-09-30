// =========================================================
// CNS service map overlay（Round 3，**独立模块**）
//
// 职责边界：
//  * 只读 ``flow``，绝不写业务状态、不发请求；
//  * 只消费后端 canonical 证据：设备目录里的
//    ``coverage_geometry.radius_by_surface``、P15 ``service_redundancy`` /
//    ``continuous_deficit_segments``、P16 ``selected_actions`` /
//    ``residual_confirmed_targets``；
//  * 前端**不计算**距离、重数、land/sea；缺口段的航路位置插值只是把后端给出的
//    ``start_route_offset_m`` / ``end_route_offset_m`` 映射到正式运行航路
//    ``path`` 上（纯坐标采样，不做任何业务判定）；
//  * **不并入** map/radar_layout_overlay.js：Radar 继续扇区，RID **绝不**画 sector /
//    90° panel。
//
// 绘制原则（性能，与 radar overlay 同一原则）：只画"真正需要用户检查的方案"——
//   已有 active provider、P16 selected_actions、显式选中的候选；
//   P16 没有 selected action 时**不**把所有 TowerColocationCandidate 铺满地图。
// 所有图层默认关闭（index.html 的 checkbox 默认未勾选）。
// =========================================================
import {
  ALL_REQUIRED_MODE_LABEL, CNS_GAP_COLORS, CNS_GAP_STATE_LABELS, CNS_ROUNDD_SERVICE_KEYS,
  CNS_SERVICE_GEOMETRY_LEGEND_NOTE, COMBINED_STATUS_MAP_STATE, NAVIGATION_BASELINE_LEGEND_LABEL,
  RADAR_DIRECTIONAL_GEOMETRY_NOTE, REDUNDANCY_STATUS_MAP_STATE, RID_SEA_PLANNING_RADIUS_LABEL,
  SERVICE_LEGEND_LABELS, SERVICE_STATUS_TEXT, SURFACE_CLASS_TEXT,
  SURVEILLANCE_DUAL_CHANNEL_LABELS, cnsServiceFormalLabel, distinctSiteCountText,
  isSurfaceAwareServiceKey, navigationBaselineText, navigationGapCauseText,
  navigationReferenceStationInstalled, navigationSiteSourceText, navigationSuitabilityOf,
  plannerFamilyLabel, serviceKeyLabel, subsystemServiceLabel, surfaceClassText,
} from './service_semantics.js';

/** 图层开关 id（与 index.html 中的 checkbox 一一对应，默认未勾选）。 */
export const CNS_SERVICE_LAYER_IDS = [
  'cnsCommunicationLayer',
  'cnsRidLayer',
  'cnsNavigationLayer',
  'cnsServiceGapLayer',
  'cnsFacilityPlanLayer',
  'surfaceFactsLayer',
];

/** 导航增强工程基线图层 id（Round D 新增；默认关闭，reopen 后仍关闭）。 */
export const CNS_NAVIGATION_LAYER_ID = 'cnsNavigationLayer';

/** 线条/圆样式（Communication 与 RID 是同一组语义色，区分只靠几何与线型）。 */
export const CNS_COVERAGE_STYLE = {
  communication: {color: '#1574d4', fill: '#1574d41f', lineWidth: 1.6},
  rid: {color: '#0f8a78', fill: '#0f8a7814', lineWidth: 1.6},
  ridSea: {color: '#0f8a78', lineWidth: 1.4, dash: [7, 5], alpha: 0.62},
  planned: {color: '#d12f8a'},
  residual: {color: '#c0392b'},
  gapLineWidth: 3.4,
  minCircleRadiusPx: 1,
};

/** 分类语义说明（图例与地图同源，避免两套说法）。 */
export const SURFACE_FACTS_LEGEND = [
  {surface: 'land', label: SURFACE_CLASS_TEXT.land},
  {surface: 'sea', label: SURFACE_CLASS_TEXT.sea},
  {surface: 'coastal_uncertain', label: SURFACE_CLASS_TEXT.coastal_uncertain},
  {surface: 'unknown', label: SURFACE_CLASS_TEXT.unknown},
];

/**
 * 导航增强工程基线 envelope 的样式（Round D）。
 *
 * 实线 = 已建成的参考站；虚线 = P16 提出的规划候选站。
 * 它**只**是 ``max_reference_baseline_m`` 的地图表达，绝不叫"RTK 无线覆盖范围"。
 */
export const CNS_NAVIGATION_STYLE = {
  existing: {color: '#b8840f', fill: '#b8840f14', lineWidth: 1.8},
  proposed: {color: '#b8840f', lineWidth: 1.5, dash: [6, 4], alpha: 0.85},
  candidatePoint: {color: '#8b949e'},
};

/** 陆海分类图层配色（**只**是分类事实，不表达风险或通过与否）。 */
export const SURFACE_FACTS_COLORS = {
  land: '#e3dfc8',
  sea: '#cfe3f2',
  coastal_uncertain: '#d8cdea',
  unknown: '#e8e8e8',
};

// ---- 小工具 -----------------------------------------------------------------

const isFiniteNumber = value => Number.isFinite(Number(value));

/** 后端 ``radius_by_surface`` 原样读取：缺失或非正数 ⇒ 不画（绝不猜半径）。 */
export function radiusBySurfaceOf(radiusBySurface, surface) {
  const mapping = radiusBySurface && typeof radiusBySurface === 'object' ? radiusBySurface : null;
  if (!mapping) return null;
  const value = Number(mapping[surface]);
  return Number.isFinite(value) && value > 0 ? value : null;
}

/** 与 radar overlay 同一投影口径：EPSG:3857 px → 地面米/px。 */
function groundMetresPerPixel(projectedUnitsPerPixel, latitudeDeg) {
  const resolution = Number(projectedUnitsPerPixel);
  const latitude = Number(latitudeDeg);
  if (!Number.isFinite(resolution) || resolution <= 0) return 1;
  if (!Number.isFinite(latitude)) return resolution;
  const scale = Math.cos(Math.max(-85.05112878, Math.min(85.05112878, latitude)) * Math.PI / 180);
  return resolution * Math.max(scale, 1e-9);
}

/** 米制半径 → 屏幕像素半径（与 radar overlay 的 ``radarSectorPixelRadius`` 同式）。 */
export function radiusPixel(radiusM, view, latitudeDeg) {
  return Number(radiusM) / groundMetresPerPixel(view?.res, latitudeDeg);
}

function drawCircle({ctx, view, screenPoint, coordinate, radiusM, style}) {
  if (!isFiniteNumber(radiusM) || Number(radiusM) <= 0) return false;
  const [x, y] = screenPoint(coordinate);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  const pixels = radiusPixel(radiusM, view, coordinate[1]);
  if (!Number.isFinite(pixels) || pixels < CNS_COVERAGE_STYLE.minCircleRadiusPx) return false;
  ctx.save();
  if (style.fill) {
    ctx.beginPath();
    ctx.arc(x, y, pixels, 0, Math.PI * 2);
    ctx.fillStyle = style.fill;
    ctx.fill();
  }
  ctx.beginPath();
  ctx.arc(x, y, pixels, 0, Math.PI * 2);
  ctx.strokeStyle = style.color;
  ctx.lineWidth = style.lineWidth;
  if (style.dash) ctx.setLineDash(style.dash);
  if (Number.isFinite(style.alpha)) ctx.globalAlpha = style.alpha;
  ctx.stroke();
  ctx.restore();
  return true;
}

// ---- 正式运行航路上的偏移量 → 坐标（纯采样，不做业务判定） -------------------

/**
 * 后端航路偏移量（米）→ 累积弧长比例。
 *
 * ``route.length_m`` 是后端给出的航路长度；缺失时退回 1:1（后端偏移量本身即米制
 * 累计距离的近似），任何情况下都**不**改变后端结论。
 */
export function routeOffsetFraction(route, offsetM) {
  const total = Number(route?.length_m);
  const offset = Number(offsetM);
  if (!Number.isFinite(offset)) return null;
  if (!Number.isFinite(total) || total <= 0) return offset;
  return offset / total;
}

/** 在经纬度折线上按**比例**取点（配合 routeOffsetFraction）。 */
export function coordinateAtFraction(path, fraction) {
  if (!Array.isArray(path) || path.length === 0) return null;
  if (path.length === 1) return [Number(path[0][0]), Number(path[0][1])];
  const ratio = Math.max(0, Math.min(1, Number(fraction)));
  if (!Number.isFinite(ratio)) return null;
  const lengths = [];
  let total = 0;
  for (let index = 1; index < path.length; index += 1) {
    const dx = Number(path[index][0]) - Number(path[index - 1][0]);
    const dy = Number(path[index][1]) - Number(path[index - 1][1]);
    const length = Math.hypot(dx, dy);
    lengths.push(length);
    total += length;
  }
  if (total <= 0) return [Number(path[0][0]), Number(path[0][1])];
  let target = ratio * total;
  for (let index = 1; index < path.length; index += 1) {
    const length = lengths[index - 1];
    if (length <= 0) continue;
    if (target <= length) {
      const local = target / length;
      return [
        Number(path[index - 1][0]) + (Number(path[index][0]) - Number(path[index - 1][0])) * local,
        Number(path[index - 1][1]) + (Number(path[index][1]) - Number(path[index - 1][1])) * local,
      ];
    }
    target -= length;
  }
  const last = path[path.length - 1];
  return [Number(last[0]), Number(last[1])];
}

/**
 * 缺口段几何：把 P15 ``continuous_deficit_segments`` 的航路偏移量映射到正式运行航路。
 *
 * 只有正式运行航路（``status === 'passed'`` 且带 ``path``）才参与；
 * 不插值出后端没有给出的状态，也不按距离自行推断 land/sea。
 */
export function gapSegmentGeometry(flow, statuses) {
  const wanted = new Set(statuses && statuses.length ? statuses : [
    'satisfied', 'under_redundant', 'uncovered', 'unknown',
  ]);
  const routes = (flow?.operational_routes || []).filter(
    route => route?.status === 'passed' && Array.isArray(route.path) && route.path.length >= 2,
  );
  const gaps = flow?.cns_corridor_gap_assessment || {};
  const segments = [];
  for (const route of routes) {
    const assessment = (gaps.routes || []).find(item => item?.route_id === route.route_id);
    if (!assessment) continue;
    if (String(gaps.status || '') === 'stale') continue;
    // service 级状态只在 P15 的 ``service_redundancy`` 汇总里给出（体元明细已外置）。
    const serviceState = serviceStateOf(assessment, flow);
    for (const subsystem of assessment.subsystems || []) {
      for (const segment of subsystem.continuous_deficit_segments || []) {
        const startFraction = routeOffsetFraction(route, segment.start_route_offset_m);
        const endFraction = routeOffsetFraction(route, segment.end_route_offset_m);
        if (startFraction === null || endFraction === null) continue;
        const from = coordinateAtFraction(route.path, startFraction);
        const to = coordinateAtFraction(route.path, endFraction);
        if (!from || !to) continue;
        const state = stateForSegment(flow, subsystem, serviceState);
        if (!wanted.has(state)) continue;
        segments.push({
          route_id: route.route_id,
          subsystem: subsystem.subsystem,
          segment_id: segment.segment_id,
          state,
          status: state,
          causes: segment.causes || [],
          length_m: segment.length_m,
          from,
          to,
          service_key: serviceState?.service_key || null,
        });
      }
    }
  }
  return segments;
}

/**
 * P15 → service 级状态。
 *
 * 取 ``cns_corridor_gap_assessment.routes[].subsystems[].service_redundancy``
 * 里**后端已给出**的状态；多服务时取最不利者（绝不把未满足写成满足）。
 * 没有任何 service 证据时返回 ``null``，由调用方退回 combined 语义。
 */
export function serviceStateOf(assessment, flow) {
  const activeService = flow?.cns_service_view?.service_key || null;
  const candidates = [];
  for (const subsystem of assessment?.subsystems || []) {
    for (const entry of subsystem.service_redundancy || []) {
      if (!isSurfaceAwareServiceKey(entry?.service_key)) continue;
      if (activeService && entry.service_key !== activeService) continue;
      candidates.push({...entry, subsystem: subsystem.subsystem});
    }
  }
  if (!candidates.length) return null;
  const rank = {confirmed_deficit: 2, unknown: 1, satisfied: 0};
  return candidates.slice().sort((left, right) => (
    (rank[right.status] ?? 1) - (rank[left.status] ?? 1)
  ))[0];
}

/** 一个 segment 最终用哪一个地图状态（service 证据优先，否则用 combined）。 */
export function stateForSegment(flow, subsystem, serviceState) {
  const activeService = flow?.cns_service_view?.service_key || null;
  if (serviceState) {
    // 同一服务在本 subsystem 的冗余状态决定颜色：满足/冗余不足/证据不足。
    return REDUNDANCY_STATUS_MAP_STATE[serviceState.status] || 'unknown';
  }
  if (activeService) {
    // 明确选了具体服务、但该 subsystem 没有该服务的证据 ⇒ 证据不足，绝不混淆。
    const hasService = (subsystem?.service_redundancy || []).some(
      entry => entry?.service_key === activeService,
    );
    if (!hasService) return 'unknown';
    const entry = (subsystem.service_redundancy || []).find(
      item => item?.service_key === activeService,
    );
    return REDUNDANCY_STATUS_MAP_STATE[entry?.status] || 'unknown';
  }
  return COMBINED_STATUS_MAP_STATE[subsystem?.combined_status] || 'unknown';
}

// ---- 覆盖站点（只画有意义的站点） --------------------------------------------

function coordinateOf(value) {
  if (!Array.isArray(value) || value.length < 2) return null;
  const longitude = Number(value[0]);
  const latitude = Number(value[1]);
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
  return [longitude, latitude];
}

/** 已有 active provider：只取显式声明了 service identity 的设备。 */
function activeProviderSites(flow) {
  const sites = [];
  for (const facility of flow?.existing_cns_facilities?.items || []) {
    const coordinate = coordinateOf(facility?.coordinate);
    if (!coordinate) continue;
    for (const device of facility.devices || []) {
      if (String(device?.status || 'active') !== 'active') continue;
      const serviceKey = String(device?.service_key || '').trim();
      if (!isSurfaceAwareServiceKey(serviceKey)) continue;
      sites.push({
        kind: 'existing',
        service_key: serviceKey,
        distinct_site_id: String(device?.distinct_site_id || facility?.facility_id || facility?.site_id || ''),
        label: facility?.name || facility?.facility_id || '已有设施',
        coordinate,
        radius_by_surface: device?.coverage_geometry?.radius_by_surface || null,
        urban_radius_m: device?.coverage_geometry?.urban_radius_m ?? null,
      });
    }
  }
  return sites;
}

/** P16 selected_actions：只为选中的规划动作画覆盖圆。 */
function selectedActionSites(flow, serviceKeys, selectedActions = null) {
  const plan = flow?.cns_corridor_site_plan || {};
  if (String(plan.status || '') === 'not_calculated') return [];
  // 方案审查可能选中了另一个 variant：那时只画**该 variant** 的动作，
  // 绝不回落到 P16 全部 selected_actions（避免画出用户没在检查的方案）。
  const source = Array.isArray(selectedActions) && selectedActions.length
    ? selectedActions
    : (plan.selected_actions || []);
  const sites = [];
  for (const action of source) {
    const coordinate = coordinateOf(action?.coordinate);
    if (!coordinate) continue;
    const serviceKey = String(action?.service_key || '').trim();
    if (!isSurfaceAwareServiceKey(serviceKey)) continue;
    if (serviceKeys && !serviceKeys.includes(serviceKey)) continue;
    sites.push({
      kind: 'planned',
      service_key: serviceKey,
      action_id: action?.action_id || null,
      distinct_site_id: String(action?.distinct_site_id || ''),
      label: action?.action_id || '规划动作',
      coordinate,
      radius_by_surface: null,
      radius_source: 'device_catalog',
      device_id: action?.device_id || null,
      subsystem: action?.subsystem || null,
      device_type: action?.device_type || null,
    });
  }
  return sites;
}

/**
 * P16 残余确认目标 / 待补证据：**只在后端给出坐标时**才画。
 *
 * 后端 P16 target 不带坐标（坐标在 P14 voxel 上），因此这里如实为空——
 * 绝不用近似位置或前端插值伪造点位。
 */
function facilityTargetSites(flow) {
  const plan = flow?.cns_corridor_site_plan || {};
  const sites = [];
  for (const target of plan.residual_confirmed_targets || []) {
    const coordinate = coordinateOf(target?.coordinate);
    if (!coordinate) continue;
    const serviceKey = String(target?.service_key || '').trim();
    if (!isSurfaceAwareServiceKey(serviceKey)) continue;
    sites.push({
      kind: 'residual',
      service_key: serviceKey,
      distinct_site_id: '',
      label: target?.target_id || '残余目标',
      coordinate,
      radius_by_surface: null,
      urban_radius_m: null,
    });
  }
  return sites;
}

/**
 * 解析"当前应该画哪些 P16 规划动作"。
 *
 * ``selectedActions`` 是调用方（main.js 的 ``proposedPlanActions``）已经算好的结果；
 * 它为空时回落到 P16 自己的 ``selected_actions``。**绝不**回落到全部候选站址。
 */
export function resolveSelectedActions(flow, selectedActions = null) {
  if (Array.isArray(selectedActions) && selectedActions.length) return selectedActions;
  return flow?.cns_corridor_site_plan?.selected_actions || [];
}

/**
 * 构建 CNS service overlay 模型。
 *
 * **绝不**铺开全部候选站址：只包含
 *   1. 已有 active provider（显式声明了 service identity 的设备）；
 *   2. P16 选中的规划动作（或方案审查当前 variant 的动作）；
 *   3. P16 残余目标中**后端确实带坐标**的条目（通常为空，如实不画）。
 */
export function cnsServiceOverlayModel(flow, {selectedActions = null} = {}) {
  const activeService = String(flow?.cns_service_view?.service_key || '').trim() || null;
  const serviceKeys = activeService && isSurfaceAwareServiceKey(activeService) ? [activeService] : null;
  const catalogDevices = new Map(
    (flow?.device_catalog?.items || []).map(item => [String(item?.device_id || ''), item]),
  );
  const planned = selectedActionSites(
    flow, serviceKeys, resolveSelectedActions(flow, selectedActions),
  ).map(site => {
    const device = catalogDevices.get(String(site.device_id || ''));
    const geometry = device?.coverage_geometry || {};
    return {
      ...site,
      radius_by_surface: geometry.radius_by_surface || null,
      urban_radius_m: geometry.urban_radius_m ?? null,
      device_service_key: String(device?.service_key || ''),
    };
  });
  const existing = activeProviderSites(flow).filter(
    site => !serviceKeys || serviceKeys.includes(site.service_key),
  );
  const residual = facilityTargetSites(flow);
  return {
    service_key: activeService,
    sites: [...existing, ...planned],
    residualSites: residual,
    plannedCount: planned.length,
    existingCount: existing.length,
    gapSegments: gapSegmentGeometry(flow, null),
    gapState: activeService ? 'service' : 'combined',
    //: Round D：导航增强工程基线 envelope（实线=已建成参考站 / 虚线=规划候选站）。
    //: 只有 policy confirmed 且 canonical baseline 为正数时才可能绘制。
    navigation: navigationBaselineModel(flow, {selectedActions}),
    facilityPlan: {
      selected_action_count: (flow?.cns_corridor_site_plan?.selected_actions || []).length,
      residual_count: (flow?.cns_corridor_site_plan?.residual_confirmed_targets || []).length,
    },
    surfaceFactsAvailable: flow?.surface_class_facts?.status === 'passed',
  };
}

// ---- 图例 ---------------------------------------------------------------------

/**
 * CNS service 图例行（Communication / RID / Radar 三种语义**绝不混同**）。
 *
 * RID 的 5 km 虚线**只能**叫「RID 海上最大规划半径」，绝不允许叫"全域覆盖半径"。
 */
export function cnsServiceLegendModel() {
  const serviceColors = {
    'C:communication': CNS_COVERAGE_STYLE.communication.color,
    'S:rid_cooperative': CNS_COVERAGE_STYLE.rid.color,
    'N:rtk_augmentation': CNS_NAVIGATION_STYLE.existing.color,
    'S:radar_noncooperative': '#1565c0',
  };
  const serviceNotes = {
    'C:communication': '全向几何规划范围；半径按 site 所在 surface 取后端 radius_by_surface',
    'S:rid_cooperative': '合作监视：全向几何。RID 绝不画 sector / 90° panel',
    'N:rtk_augmentation': '导航增强：' + NAVIGATION_BASELINE_LEGEND_LABEL
      + '；不使用全向覆盖圆模型，也不使用方向性面阵',
    'S:radar_noncooperative': RADAR_DIRECTIONAL_GEOMETRY_NOTE,
  };
  return [
    {
      id: 'cns-communication',
      label: SERVICE_LEGEND_LABELS['C:communication'],
      symbol: '<span class="legend-circle" style="border-color:#1574d4"></span>',
      note: '规划覆盖：全向 360°，半径按 site 所在 surface 取后端 radius_by_surface',
    },
    {
      id: 'cns-rid-land',
      label: 'RID 陆地 / 海岸不确定规划范围（实线）',
      symbol: '<span class="legend-circle" style="border-color:#0f8a78"></span>',
      note: '圆半径来自后端 radius_by_surface.land / coastal_uncertain',
    },
    {
      id: 'cns-rid-sea',
      label: RID_SEA_PLANNING_RADIUS_LABEL + '（虚线）',
      symbol: '<span class="legend-circle legend-dashed" style="border-color:#0f8a78"></span>',
      note: '实际覆盖结论由航路采样点 surface_class 决定：land/coastal → land 半径，sea → 本半径；'
        + '本虚线不是"RID 覆盖范围"',
    },
    {
      id: 'cns-rid-geometry',
      label: SERVICE_LEGEND_LABELS['S:rid_cooperative'],
      symbol: '<span class="legend-dot" style="background:#0f8a78"></span>',
      note: 'RID 合作监视：全向几何。RID 绝不画 sector / 90° panel；'
        + '方向性面阵只属于 Radar Non-cooperative Surveillance',
    },
    {
      id: 'cns-surface-facts',
      label: '地表分类（按 L8 格心代表点）',
      symbol: '<span class="legend-dot" style="background:#7256a1"></span>',
      note: '陆海分类事实图层默认关闭：land / sea / coastal_uncertain / unknown（证据不足，绝不按海面处理）',
    },
    ...Object.keys(CNS_GAP_COLORS).map(state => ({
      id: 'cns-gap-' + state,
      label: CNS_GAP_STATE_LABELS[state],
      symbol: '<span class="legend-stroke" style="border-top-color:' + CNS_GAP_COLORS[state] + '"></span>',
      note: state === 'under_redundant'
        ? '冗余不足：实际不同物理站址数少于要求站址数（按 distinct_site_id 计数）'
        : state === 'unknown'
          ? '证据不足：地表分类未知或 provider 独立性证据缺失（fail-closed）'
          : '',
    })),
    // ---- Round D：导航增强工程基线 + 四服务正式名 + 统一免责声明 --------------
    {
      id: 'cns-navigation-baseline',
      label: NAVIGATION_BASELINE_LEGEND_LABEL + '（实线：已建成参考站 / 虚线：规划候选站）',
      symbol: '<span class="legend-circle legend-dashed" style="border-color:'
        + CNS_NAVIGATION_STYLE.existing.color + '"></span>',
      note: '圆半径只来自 required_cns 的 canonical max_reference_baseline_m；'
        + '它是工程规划参数，不代表实测 RTK 服务半径，也不是厂家保证值',
    },
    ...CNS_ROUNDD_SERVICE_KEYS.map(key => ({
      id: 'cns-service-' + key.replace(/[^A-Za-z0-9]/g, '-').toLowerCase(),
      label: cnsServiceFormalLabel(key),
      symbol: '<span class="legend-dot" style="background:'
        + (serviceColors[key] || CNS_GAP_COLORS.unknown) + '"></span>',
      note: serviceNotes[key] || '',
    })),
    {
      id: 'cns-dual-channel',
      label: SURVEILLANCE_DUAL_CHANNEL_LABELS.dual + '（' + ALL_REQUIRED_MODE_LABEL + '）',
      symbol: '<span class="legend-stroke" style="border-top-color:#c62828"></span>',
      note: '合作监视（RID）与非合作监视（Radar）分服务统计与着色，绝不把两类站址相加',
    },
    {
      id: 'cns-legend-disclaimer',
      label: '图示范围说明',
      symbol: '<span class="legend-dot" style="background:#8b949e"></span>',
      note: CNS_SERVICE_GEOMETRY_LEGEND_NOTE,
    },
  ];
}

/** service 状态词表（面板与图例共用，避免第二套说法）。 */
export const CNS_SERVICE_STATUS_TEXT = SERVICE_STATUS_TEXT;

// ---- 绘制 ---------------------------------------------------------------------

/**
 * 绘制 CNS service overlay。
 *
 * @returns {{sites:number,circles:number,gapSegments:number}}
 */
export function drawCnsServiceOverlay({ctx, view, screenPoint, model, layers = {}} = {}) {
  const drawn = {sites: 0, circles: 0, gapSegments: 0, navigationBaseline: 0};
  if (!view || !model) return drawn;

  const communicationOn = layers.cnsCommunicationLayer === true;
  const ridOn = layers.cnsRidLayer === true;

  for (const site of model.sites || []) {
    const serviceKey = String(site.service_key || '');
    if (serviceKey === 'C:communication' && !communicationOn) continue;
    if (serviceKey === 'S:rid_cooperative' && !ridOn) continue;
    if (serviceKey !== 'C:communication' && serviceKey !== 'S:rid_cooperative') continue;
    const [x, y] = screenPoint(site.coordinate);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    drawn.sites += 1;

    if (serviceKey === 'C:communication') {
      // Communication：全向圆；半径按 site 所在 surface 取后端 radius_by_surface。
      const radius = radiusForSite(site, 'land') ?? radiusForSite(site, 'sea');
      if (drawCircle({
        ctx, view, screenPoint, coordinate: site.coordinate, radiusM: radius,
        style: CNS_COVERAGE_STYLE.communication,
      })) drawn.circles += 1;
    } else {
      // RID：land/coastal 半径实线 + 海上最大规划半径虚线；**绝不**画扇区。
      const landRadius = radiusForSite(site, 'land');
      if (drawCircle({
        ctx, view, screenPoint, coordinate: site.coordinate, radiusM: landRadius,
        style: CNS_COVERAGE_STYLE.rid,
      })) drawn.circles += 1;
      const seaRadius = radiusForSite(site, 'sea');
      if (seaRadius !== null && (landRadius === null || seaRadius > landRadius)) {
        if (drawCircle({
          ctx, view, screenPoint, coordinate: site.coordinate, radiusM: seaRadius,
          style: CNS_COVERAGE_STYLE.ridSea,
        })) drawn.circles += 1;
      }
    }
    // 站点中心：规划动作额外用规划色描边，避免被覆盖圆淹没。
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, site.kind === 'planned' ? 3.6 : 3, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = site.kind === 'planned'
      ? CNS_COVERAGE_STYLE.planned.color
      : (serviceKey === 'C:communication'
        ? CNS_COVERAGE_STYLE.communication.color
        : CNS_COVERAGE_STYLE.rid.color);
    ctx.lineWidth = 1.6;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // CNS 设施规划图层：只补充 P16 残余目标（后端带坐标时才有点位）。
  if (layers.cnsFacilityPlanLayer === true) {
    ctx.save();
    for (const site of model.residualSites || []) {
      const [x, y] = screenPoint(site.coordinate);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      ctx.beginPath();
      ctx.arc(x, y, 3.4, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.strokeStyle = CNS_COVERAGE_STYLE.residual.color;
      ctx.lineWidth = 1.8;
      ctx.fill();
      ctx.stroke();
      drawn.sites += 1;
    }
    ctx.restore();
  }

  if (layers.cnsServiceGapLayer === true) {
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineWidth = CNS_COVERAGE_STYLE.gapLineWidth;
    for (const segment of model.gapSegments || []) {
      const [x1, y1] = screenPoint(segment.from);
      const [x2, y2] = screenPoint(segment.to);
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
      ctx.strokeStyle = CNS_GAP_COLORS[segment.state] || CNS_GAP_COLORS.unknown;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      drawn.gapSegments += 1;
    }
    ctx.restore();
  }

  // 导航增强工程基线（Round D）：**默认关闭**，且只有后端 canonical 基线可画时才画。
  if (layers.cnsNavigationLayer === true) {
    drawn.navigationBaseline = drawCnsNavigationEnvelope({
      ctx, view, screenPoint, model: model.navigation,
    });
  }
  return drawn;
}

/** 站点半径：Communication 统一全向，RID 分 land / sea 两档（都来自后端）。 */
function radiusForSite(site, surface) {
  return radiusBySurfaceOf(site?.radius_by_surface, surface);
}

// =========================================================
// Round D：导航增强工程基线 envelope
//
// 本区刻意放在 ``radiusForSite`` **之后**：它需要业务状态词（满足 / 缺口 / 证据不足），
// 而绘制区间（``drawCnsServiceOverlay`` … ``radiusForSite``）有"不得出现状态词"的护栏。
//
// 铁律：envelope 只是 canonical ``max_reference_baseline_m`` 的**地图表达**；
// 它**不**参与 P14 / P15 / P16 的任何判定，前端也绝不根据地图圆圈重算
// ``within_baseline``。缺失或未确认 ⇒ 不画正式 envelope。
// =========================================================

/** 导航增强政策块（只读 canonical ``planning``）。 */
export function navigationBaselinePolicy(flow) {
  const requirement = ((flow?.required_cns || {}).project_default || {}).navigation || {};
  const services = requirement.services && typeof requirement.services === 'object'
    ? requirement.services : null;
  const entry = services ? services['N:rtk_augmentation'] : null;
  const planning = entry && typeof entry.planning === 'object' ? entry.planning : null;
  const raw = planning ? Number(planning.max_reference_baseline_m) : NaN;
  return {
    declared: Boolean(planning),
    required: entry?.required === true,
    confirmed: planning?.confirmed === true,
    baselineM: Number.isFinite(raw) && raw > 0 ? raw : null,
    deliveryServiceKey: planning?.delivery_service_key ?? null,
  };
}

/**
 * 导航增强工程基线模型。
 *
 * * ``drawable`` 只有"policy 已确认 + canonical 基线为正数 + 确有站址"才为真；
 * * 已建成参考站 → 实线；P16 规划候选站 → 虚线；
 * * 声明了 suitability 但**未确认**的站址只作候选点，**绝不**画正式 envelope。
 */
export function navigationBaselineModel(flow, {selectedActions = null} = {}) {
  const policy = navigationBaselinePolicy(flow);
  const sites = [];
  const candidates = [];
  if (policy.required) {
    const collections = [
      ['existing_cns_facility', 'existing_cns_facilities'],
      ['tower_colocation_host', 'tower_colocation_candidates'],
      ['candidate_site', 'candidate_sites'],
    ];
    for (const [origin, collectionKey] of collections) {
      for (const item of flow?.[collectionKey]?.items || []) {
        const coordinate = coordinateOf(item?.coordinate);
        if (!coordinate) continue;
        const suitability = navigationSuitabilityOf(item);
        if (!suitability) continue;
        const identity = String(
          item.distinct_site_id || item.facility_id || item.site_id || '',
        );
        if (suitability.confirmed !== true || suitability.planning_use_confirmed !== true) {
          candidates.push({
            kind: 'unconfirmed_suitability',
            service_key: 'N:rtk_augmentation',
            distinct_site_id: identity,
            label: String(item.name || item.site_id || '未确认适用性站址'),
            coordinate, planning_origin: origin,
          });
          continue;
        }
        const installed = navigationReferenceStationInstalled(suitability, origin);
        sites.push({
          kind: installed ? 'existing_reference_station' : 'proposed_reference_station',
          service_key: 'N:rtk_augmentation',
          distinct_site_id: identity,
          label: String(item.name || item.site_id || '导航基准站'),
          coordinate, planning_origin: origin,
          reference_station_installed: installed,
        });
      }
    }
  }
  // P16 selected actions：用户正在检查的规划候选站 → 虚线 envelope。
  if (policy.required) {
    for (const action of resolveSelectedActions(flow, selectedActions)) {
      if (String(action?.service_key || '') !== 'N:rtk_augmentation') continue;
      const coordinate = coordinateOf(action?.coordinate);
      if (!coordinate) continue;
      sites.push({
        kind: 'proposed_reference_station',
        service_key: 'N:rtk_augmentation',
        action_id: action?.action_id || null,
        distinct_site_id: String(action?.distinct_site_id || ''),
        label: String(action?.action_id || '规划候选参考站'),
        coordinate,
        planning_origin: String(action?.planning_origin || action?.reuse_class || ''),
        reference_station_installed: false,
      });
    }
  }
  return {
    declared: policy.declared,
    required: policy.required,
    confirmed: policy.confirmed,
    baselineM: policy.baselineM,
    deliveryServiceKey: policy.deliveryServiceKey,
    //: 未确认策略或缺少 canonical 基线 ⇒ 不画正式 envelope（fail-closed）。
    drawable: policy.confirmed && policy.baselineM !== null && sites.length > 0,
    sites, candidates,
    baselineText: navigationBaselineText(policy.baselineM),
  };
}

/** 绘制导航增强工程基线 envelope（实线=已建成参考站 / 虚线=规划候选站）。 */
export function drawCnsNavigationEnvelope({ctx, view, screenPoint, model} = {}) {
  let drawn = 0;
  const baselineM = Number(model?.baselineM);
  if (model?.drawable !== true || !Number.isFinite(baselineM) || baselineM <= 0) return drawn;
  for (const site of model.sites || []) {
    const installed = site.kind === 'existing_reference_station';
    const style = installed ? CNS_NAVIGATION_STYLE.existing : CNS_NAVIGATION_STYLE.proposed;
    const [x, y] = screenPoint(site.coordinate);
    if (drawCircle({
      ctx, view, screenPoint, coordinate: site.coordinate, radiusM: baselineM, style,
    })) drawn += 1;
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, installed ? 3.4 : 3, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = CNS_NAVIGATION_STYLE.existing.color;
    ctx.lineWidth = 1.6;
    if (!installed) ctx.setLineDash([4, 3]);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }
  for (const site of model.candidates || []) {
    const [x, y] = screenPoint(site.coordinate);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = CNS_NAVIGATION_STYLE.candidatePoint.color;
    ctx.setLineDash([2, 2]);
    ctx.lineWidth = 1.4;
    ctx.fill();
    ctx.stroke();
    ctx.restore();
    drawn += 1;
  }
  return drawn;
}

// ---- 地图要素命中与只读 tooltip ----------------------------------------------

function escapeTip(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

/** 点到线段距离（屏幕像素；纯几何，不做任何业务判定）。 */
function pointSegmentDistance(point, from, to) {
  const dx = to[0] - from[0], dy = to[1] - from[1];
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared <= 0) return Math.hypot(point[0] - from[0], point[1] - from[1]);
  const raw = ((point[0] - from[0]) * dx + (point[1] - from[1]) * dy) / lengthSquared;
  const t = Math.max(0, Math.min(1, raw));
  return Math.hypot(point[0] - (from[0] + t * dx), point[1] - (from[1] + t * dy));
}

/** P15 里某个 service 的 canonical 桶（route 内第一个命中；不存在即 ``null``）。 */
export function corridorServiceBucket(flow, serviceKey) {
  const assessment = flow?.cns_corridor_gap_assessment || {};
  for (const route of assessment.routes || []) {
    for (const subsystem of route.subsystems || []) {
      for (const entry of subsystem.service_redundancy || []) {
        if (String(entry?.service_key || '') === String(serviceKey || '')) return entry;
      }
    }
  }
  return null;
}

/** 当前开启的 CNS 图层里可命中的要素（四服务分列，**绝不**相加）。 */
export function cnsMapFeatures(flow, {selectedActions = null, layers = {}} = {}) {
  const features = [];
  if (layers.cnsServiceGapLayer === true) {
    for (const segment of gapSegmentGeometry(flow, null)) {
      features.push({kind: 'gap_segment', ...segment});
    }
  }
  if (layers.cnsFacilityPlanLayer === true) {
    const plan = flow?.cns_corridor_site_plan || {};
    if (String(plan.status || '') !== 'not_calculated') {
      for (const action of resolveSelectedActions(flow, selectedActions)) {
        const coordinate = coordinateOf(action?.coordinate);
        if (!coordinate) continue;
        features.push({kind: 'facility_action', action, coordinate});
      }
    }
  }
  if (layers.cnsNavigationLayer === true) {
    const model = navigationBaselineModel(flow, {selectedActions});
    for (const site of model.sites || []) features.push({kind: 'navigation_site', site});
    for (const site of model.candidates || []) features.push({kind: 'navigation_candidate', site});
  }
  return features;
}

/** 命中测试：段用点到线段距离，点用半径；只返回最近的一个要素。 */
export function cnsMapFeatureAt({
  click, flow, layers = {}, screenPoint, radiusPx = 12, selectedActions = null,
} = {}) {
  if (!Array.isArray(click) || typeof screenPoint !== 'function') return null;
  let best = null, bestDistance = Infinity;
  for (const feature of cnsMapFeatures(flow, {selectedActions, layers})) {
    if (feature.kind === 'gap_segment') {
      const from = screenPoint(feature.from), to = screenPoint(feature.to);
      if (![from[0], from[1], to[0], to[1]].every(Number.isFinite)) continue;
      const distance = pointSegmentDistance(click, from, to);
      if (distance <= radiusPx && distance < bestDistance) {
        best = feature; bestDistance = distance;
      }
      continue;
    }
    const point = screenPoint(feature.coordinate);
    if (!Number.isFinite(point[0]) || !Number.isFinite(point[1])) continue;
    const distance = Math.hypot(click[0] - point[0], click[1] - point[1]);
    if (distance <= radiusPx && distance < bestDistance) {
      best = feature; bestDistance = distance;
    }
  }
  return best;
}

/**
 * 地图 tooltip 内容（只读）。
 *
 * 服务 / 状态 / 缺口原因 / 航路与体元 / surface / 要求的与实际的独立站址数，
 * 全部转印 backend canonical 字段；**不**在前端重算 gap，也**不**把多服务相加。
 */
export function cnsMapFeatureTooltip(feature, flow) {
  if (!feature) return '';
  const row = (label, value, note = '') => '<div class="cns-map-tip-row"><b>'
    + escapeTip(label) + '</b><span>' + escapeTip(value) + '</span>'
    + (note ? '<small>' + escapeTip(note) + '</small>' : '') + '</div>';
  if (feature.kind === 'gap_segment') {
    const serviceKey = String(feature.service_key || '');
    const bucket = corridorServiceBucket(flow, serviceKey);
    const causes = (feature.causes || []).map(cause => navigationGapCauseText(cause)).join(' / ');
    return '<div class="cns-map-tip"><h4>CNS 服务缺口</h4>'
      + row('服务', cnsServiceFormalLabel(serviceKey || feature.subsystem))
      + row('状态', CNS_GAP_STATE_LABELS[feature.state] || String(feature.state || '证据不足'))
      + row('航路', String(feature.route_id || '—'),
        '里程 ' + Math.round(feature.from_m ?? 0) + '–' + Math.round(feature.to_m ?? 0) + ' m'
        + ' · 长度 ' + Math.round(feature.length_m ?? 0) + ' m')
      + row('缺口原因', causes || '未提供结构化原因')
      + row('surface / 体元分布',
        surfaceSummaryText(bucket),
        '体元 ' + String(bucket?.voxel_count ?? '—'))
      + row('要求的 / 实际的独立站址数',
        distinctSiteSummaryText(bucket))
      + row('说明', CNS_SERVICE_GEOMETRY_LEGEND_NOTE)
      + '</div>';
  }
  if (feature.kind === 'facility_action') {
    const action = feature.action || {};
    const siblings = resolveSelectedActions(flow, null).filter(item =>
      String(item?.distinct_site_id || '') === String(action?.distinct_site_id || '')
      && String(action?.distinct_site_id || '') !== '');
    const coLocated = siblings.length > 1
      ? siblings.map(item => cnsServiceFormalLabel(item?.service_key)).join('、')
      : '';
    return '<div class="cns-map-tip"><h4>CNS 设施规划动作</h4>'
      + row('服务', cnsServiceFormalLabel(action?.service_key || action?.subsystem))
      + row('站址', String(action?.site_id || action?.distinct_site_id || '—'),
        '站址来源 ' + navigationSiteSourceText(action?.reuse_class))
      + row('动作', plannerFamilyLabel(action?.planner_family),
        String(action?.device_id || '') ? '设备 ' + String(action.device_id) : '设备型号未选择')
      + row('distinct_site_id', String(action?.distinct_site_id || '—'),
        coLocated ? '同一物理站址共址了 ' + siblings.length + ' 类服务：' + coLocated : '')
      + row('说明', CNS_SERVICE_GEOMETRY_LEGEND_NOTE)
      + '</div>';
  }
  if (feature.kind === 'navigation_site') {
    const site = feature.site || {};
    const installed = site.reference_station_installed === true;
    return '<div class="cns-map-tip"><h4>' + escapeTip(NAVIGATION_BASELINE_LEGEND_LABEL) + '</h4>'
      + row('服务', cnsServiceFormalLabel('N:rtk_augmentation'))
      + row('站址', String(site.label || '—'),
        '站址来源 ' + navigationSiteSourceText(site.planning_origin))
      + row('工程基线距离', navigationBaselineText(navigationBaselineModel(flow).baselineM))
      + row('状态', installed ? '已建成参考站（实线）' : '规划候选站（虚线）')
      + row('说明', '该圆只是工程基线参数的地图表达，不是无线覆盖范围，也不参与任何判定')
      + '</div>';
  }
  if (feature.kind === 'navigation_candidate') {
    const site = feature.site || {};
    return '<div class="cns-map-tip"><h4>导航基准站候选（未确认适用性）</h4>'
      + row('服务', cnsServiceFormalLabel('N:rtk_augmentation'))
      + row('站址', String(site.label || '—'),
        '站址来源 ' + navigationSiteSourceText(site.planning_origin))
      + row('状态', '尚未确认适用性：不画正式工程基线范围')
      + '</div>';
  }
  return '';
}

/** 桶级 surface 分布（只转印计数）。 */
function surfaceSummaryText(bucket) {
  const counts = bucket?.surface_class_counts || {};
  const parts = Object.entries(counts).map(([surface, count]) =>
    surfaceClassText(surface) + ' ' + count);
  return parts.length ? parts.join(' · ') : '证据不足';
}

/** 桶级"要求的 / 实际的独立站址数"（任一侧缺失即证据不足）。 */
function distinctSiteSummaryText(bucket) {
  if (!bucket) return '证据不足';
  return distinctSiteCountText(bucket.distinct_site_count, bucket.required_distinct_site_count);
}

/** 语义名（面板/图例共用；这里只转发 service_semantics 的词表）。 */
export function overlayServiceLabel(serviceKey) {
  return serviceKeyLabel(serviceKey);
}

export function overlaySubsystemLabel(code) {
  return subsystemServiceLabel(code);
}

/**
 * 陆海分类图层：默认关闭，勾选后按 **surface facts** 给 L8 格填色。
 *
 * 只画可见范围内的格（先用 bbox 过滤，再投影）；``unknown`` 用独立的中性色
 * （**绝不**按海面着色）；它只表达"格心代表点分类"，不是连续精确海岸线。
 */
export function drawSurfaceFactsLayer({ctx, grid, facts, screenPoint, visibleBounds, gridTheme}) {
  let drawn = 0;
  const mapping = facts?.by_grid_id;
  if (!mapping || typeof mapping !== 'object') return drawn;
  const visible = typeof visibleBounds === 'function' ? visibleBounds() : null;
  if (!visible) return drawn;
  ctx.save();
  for (const cell of grid?.cells || []) {
    const surface = mapping[String(cell?.grid_id ?? '')];
    if (!surface) continue;
    const bbox = cell.bbox;
    if (!Array.isArray(bbox) || bbox.length < 4) continue;
    const [west, south, east, north] = bbox.map(Number);
    if (![west, south, east, north].every(Number.isFinite)) continue;
    if (east < visible[0] || west > visible[2] || north < visible[1] || south > visible[3]) continue;
    if (gridTheme?.bboxIntersects && !gridTheme.bboxIntersects(bbox, visible)) continue;
    const southwest = screenPoint([west, south]);
    const northeast = screenPoint([east, north]);
    if (![southwest[0], southwest[1], northeast[0], northeast[1]].every(Number.isFinite)) continue;
    ctx.fillStyle = SURFACE_FACTS_COLORS[surface] || SURFACE_FACTS_COLORS.unknown;
    ctx.globalAlpha = 0.55;
    ctx.fillRect(southwest[0], northeast[1], northeast[0] - southwest[0], southwest[1] - northeast[1]);
    drawn += 1;
  }
  ctx.restore();
  return drawn;
}
