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
  CNS_GAP_COLORS, CNS_GAP_STATE_LABELS, COMBINED_STATUS_MAP_STATE,
  REDUNDANCY_STATUS_MAP_STATE, RID_SEA_PLANNING_RADIUS_LABEL,
  SERVICE_LEGEND_LABELS, SERVICE_STATUS_TEXT, SURFACE_CLASS_TEXT,
  isSurfaceAwareServiceKey, serviceKeyLabel, subsystemServiceLabel,
} from './service_semantics.js';

/** 图层开关 id（与 index.html 中的 checkbox 一一对应，默认未勾选）。 */
export const CNS_SERVICE_LAYER_IDS = [
  'cnsCommunicationLayer',
  'cnsRidLayer',
  'cnsServiceGapLayer',
  'cnsFacilityPlanLayer',
  'surfaceFactsLayer',
];

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
  const drawn = {sites: 0, circles: 0, gapSegments: 0};
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
  return drawn;
}

/** 站点半径：Communication 统一全向，RID 分 land / sea 两档（都来自后端）。 */
function radiusForSite(site, surface) {
  return radiusBySurfaceOf(site?.radius_by_surface, surface);
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
