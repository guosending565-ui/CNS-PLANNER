// =========================================================
// 导航监测规划地图图层（Round38-N2）
//
// 职责边界（只做"地图表达"，不碰任何业务状态）：
//  * 只读取**已经拿到**的仿真结果（N1 只读响应）与固定宽带只读几何；
//  * 图层由用户主动开启（``navigationMonitoringLayer``），默认关闭；
//  * 设备图标醒目：起终点强制设备与既有铁塔使用**不同图形**；
//  * 航段区分「已关联」/「未关联」，但**绝不**使用容易被误解成认证安全的措辞；
//  * 2000 m 关联范围**不画**成真实认证导航覆盖区：只画沿航路的已关联/未关联段，
//    不画 2000 m 圆、不画覆盖多边形；
//  * 固定宽带只有在后端 ``currentness === 'current'`` 时才画淡色边界；否则**不画**
//    任何保障宽度（缺证 / stale 由面板文字说明，不在前端自行构造第二套宽度）；
//  * 本模块不写 zoom / layer / LOD，线宽与透明度由调用方按既有 LOD 传入。
// =========================================================
import {drawLine} from './renderer.js';
import {routeOffsetToCoordinate} from './display_layers.js';
import {provisioningText} from '../workflow/navigation_monitoring.js';

/** 图层配色。关联 / 未关联只表达"监测关联"，不表达"安全 / 不安全"。 */
export const NAVIGATION_MONITORING_COLORS = Object.freeze({
  route: '#2b4a63',
  associated: '#1f7a4d',
  unassociated: '#c2410c',
  mandatory: '#123a5c',
  tower: '#b8840f',
  fixedBand: '#7f9bb3',
});

export const NAVIGATION_MONITORING_STYLE = Object.freeze({
  routeWidth: 3.0, routeAlpha: 0.95,
  associatedWidth: 5.0, associatedAlpha: 0.85,
  unassociatedWidth: 5.0, unassociatedAlpha: 0.95,
  deviceRadius: 6.5,
  bandWidth: 1.0, bandAlpha: 0.28,
});

/** 设备图形：强制端点与既有铁塔必须是**不同**图形（不靠颜色区分）。 */
export const NAVIGATION_DEVICE_SHAPES = Object.freeze({
  mandatory: 'triangle',
  tower: 'square',
});

/**
 * 已关联 / 未关联航段几何。
 *
 * 关联区间来自后端 ``merged_intervals_m``（沿航路累计米），未关联区间来自后端
 * ``gaps``；前端**不重算**任何区间，只按里程取点绘制。
 */
export function associationSegments(routePath, routeLengthM, model) {
  if (!Array.isArray(routePath) || routePath.length < 2) return [];
  const length = Number(routeLengthM);
  if (!Number.isFinite(length) || length <= 0) return [];
  const segments = [];
  const intervals = Array.isArray(model?.mergedIntervals) ? model.mergedIntervals : [];
  for (const interval of intervals) {
    const start = Number(interval?.[0]);
    const end = Number(interval?.[1]);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    segments.push({kind: 'associated', startM: start, endM: end});
  }
  const gaps = Array.isArray(model?.gaps) ? model.gaps : [];
  for (const gap of gaps) {
    const start = Number(gap?.start_m);
    const end = Number(gap?.end_m);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
    segments.push({kind: 'unassociated', startM: start, endM: end});
  }
  return segments
    .map(segment => ({
      ...segment,
      from: routeOffsetToCoordinate(routePath, length, segment.startM),
      to: routeOffsetToCoordinate(routePath, length, segment.endM),
    }))
    .filter(segment => segment.from && segment.to);
}

/**
 * 图层模型：把仿真结果 + 候选坐标 + 固定宽带几何转成可绘制要素。
 *
 * @param {{result:object, candidates:Array, routePath:Array, routeLengthM:number,
 *          fixedGeometry:object, layerOn:boolean}} input
 * @returns {{drawable:boolean,reason:string|null,devices:Array,segments:Array,
 *            routePath:Array,fixedBand:object|null}}
 */
export function navigationMonitoringLayerModel({
  result = null, candidates = [], routePath = null, routeLengthM = null,
  fixedGeometry = null, layerOn = false,
} = {}) {
  if (!layerOn) return {drawable: false, reason: 'layer_disabled', devices: [], segments: [], routePath: [], fixedBand: null};
  const model = result && typeof result === 'object' ? result : null;
  if (!model || !Object.keys(model).length) {
    return {drawable: false, reason: 'no_simulation_result', devices: [], segments: [], routePath: [], fixedBand: null};
  }
  const path = Array.isArray(routePath) && routePath.length >= 2 ? routePath : [];
  if (!path.length) {
    return {drawable: false, reason: 'no_route_geometry', devices: [], segments: [], routePath: [], fixedBand: null};
  }

  const byId = new Map();
  for (const item of candidates || []) {
    const id = String(item?.candidate_id ?? '');
    if (id && item?.coordinate) byId.set(id, item);
  }
  //: 站点身份 → 坐标：优先候选快照（含 source_crs 身份），缺失时退回设备条目。
  const devices = [];
  const mandatory = new Set(
    (model.selected_mandatory_site_ids || []).map(item => String(item)),
  );
  for (const device of model.devices || []) {
    const siteId = String(device?.site_id ?? '');
    const candidate = byId.get(siteId);
    const coordinate = candidate?.coordinate
      ?? (Array.isArray(device?.coordinate) ? device.coordinate : null);
    if (!siteId || !coordinate) continue;
    const isMandatory = mandatory.has(siteId);
    devices.push({
      siteId,
      deviceId: device?.device_id ?? null,
      provisioning: device?.provisioning ?? null,
      role: isMandatory ? 'mandatory' : 'optional',
      shape: isMandatory ? NAVIGATION_DEVICE_SHAPES.mandatory : NAVIGATION_DEVICE_SHAPES.tower,
      coordinate,
      candidateKind: candidate?.candidate_kind ?? null,
      sourceCrs: candidate?.source_crs?.value ?? null,
      spatialUseStatus: candidate?.spatial_use_status ?? null,
      spatialBlockReason: candidate?.spatial_block_reason ?? null,
    });
  }

  const segments = associationSegments(path, routeLengthM ?? model.route_length_m, {
    mergedIntervals: model.merged_intervals_m,
    gaps: model.gaps,
  });

  //: 固定宽带：**只有** current 且几何存在时才进入绘制计划。
  const band = fixedGeometry && typeof fixedGeometry === 'object' ? fixedGeometry : {};
  const bandGeometry = band.geometry && band.currentness === 'current'
    && ['Polygon', 'MultiPolygon'].includes(String(band.geometry?.type ?? ''))
    ? band.geometry : null;

  return {
    drawable: true,
    reason: null,
    devices,
    mandatoryDevices: devices.filter(item => item.role === 'mandatory'),
    optionalDevices: devices.filter(item => item.role === 'optional'),
    segments,
    associatedSegments: segments.filter(item => item.kind === 'associated'),
    unassociatedSegments: segments.filter(item => item.kind === 'unassociated'),
    routePath: path,
    routeLengthM: routeLengthM ?? model.route_length_m ?? null,
    fixedBand: bandGeometry
      ? {
        geometry: bandGeometry,
        fingerprint: band.geometry_fingerprint ?? null,
        halfWidthM: band.corridor_half_width_m ?? null,
        currentness: band.currentness,
      }
      : null,
    fixedBandReason: bandGeometry ? null : (
      band.currentness && band.currentness !== 'current'
        ? 'fixed_corridor_geometry_not_current' : 'fixed_corridor_geometry_absent'
    ),
  };
}

/** 环形几何 → 屏幕折线（只为绘制淡色边界；不做任何几何运算）。 */
function polygonRings(geometry) {
  const type = String(geometry?.type ?? '');
  const coordinates = geometry?.coordinates;
  if (!Array.isArray(coordinates)) return [];
  if (type === 'Polygon') return coordinates.filter(Array.isArray);
  if (type === 'MultiPolygon') return coordinates.flat().filter(Array.isArray);
  return [];
}

/** 设备图标：三角形（强制端点）/ 方形（既有铁塔）—— 图形本身必须不同。 */
function drawDeviceShape(ctx, shape, x, y, radius) {
  ctx.beginPath();
  if (shape === 'triangle') {
    ctx.moveTo(x, y - radius);
    ctx.lineTo(x + radius * 0.92, y + radius * 0.78);
    ctx.lineTo(x - radius * 0.92, y + radius * 0.78);
    ctx.closePath();
  } else {
    const half = radius * 0.82;
    ctx.moveTo(x - half, y - half);
    ctx.lineTo(x + half, y - half);
    ctx.lineTo(x + half, y + half);
    ctx.lineTo(x - half, y + half);
    ctx.closePath();
  }
  ctx.fill();
  ctx.stroke();
}

/**
 * 绘制导航监测规划图层。
 *
 * @param {{ctx,screenPoint,model,style?}} input
 * @returns {{drawn:boolean,reason:string|null,devices:number,segments:number,band:boolean}}
 */
export function drawNavigationMonitoringLayer({
  ctx, screenPoint, model, style = NAVIGATION_MONITORING_STYLE,
}) {
  const value = model || {drawable: false, reason: 'no_model'};
  if (!value.drawable) {
    return {
      drawn: false, reason: value.reason ?? 'not_drawable',
      devices: 0, segments: 0, band: false,
    };
  }
  ctx.save();
  //: 固定宽带淡色边界画在**最底层**，避免压住航段与设备。
  if (value.fixedBand) {
    ctx.globalAlpha = style.bandAlpha;
    ctx.strokeStyle = NAVIGATION_MONITORING_COLORS.fixedBand;
    ctx.lineWidth = style.bandWidth;
    for (const ring of polygonRings(value.fixedBand.geometry)) {
      if (ring.length < 3) continue;
      const screen = ring.map(point => screenPoint(point)).filter(Boolean);
      if (screen.length < 3) continue;
      ctx.beginPath();
      screen.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point[0], point[1]);
        else ctx.lineTo(point[0], point[1]);
      });
      ctx.closePath();
      ctx.stroke();
    }
  }
  //: 航路中心线。
  ctx.globalAlpha = style.routeAlpha;
  drawLine(ctx, screenPoint, true, value.routePath, NAVIGATION_MONITORING_COLORS.route, style.routeWidth, []);
  //: 已关联 / 未关联航段：颜色 + 虚线同时区分（不只靠颜色）。
  //: 注意 ``drawLine`` 的第三参是"是否绘制"开关（truthy 才画），必须传 true。
  for (const segment of value.segments) {
    const associated = segment.kind === 'associated';
    ctx.globalAlpha = associated ? style.associatedAlpha : style.unassociatedAlpha;
    drawLine(
      ctx, screenPoint, true, [segment.from, segment.to],
      associated ? NAVIGATION_MONITORING_COLORS.associated
        : NAVIGATION_MONITORING_COLORS.unassociated,
      associated ? style.associatedWidth : style.unassociatedWidth,
      associated ? [] : [7, 5],
    );
  }
  //: 设备图标：强制端点（三角形，实心深蓝）与既有铁塔（方形，琥珀）图形不同。
  ctx.globalAlpha = 1;
  for (const device of value.devices) {
    const screen = screenPoint(device.coordinate);
    if (!screen) continue;
    const mandatory = device.role === 'mandatory';
    ctx.fillStyle = mandatory
      ? NAVIGATION_MONITORING_COLORS.mandatory : NAVIGATION_MONITORING_COLORS.tower;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.8;
    drawDeviceShape(ctx, device.shape, screen[0], screen[1], style.deviceRadius);
  }
  ctx.restore();
  return {
    drawn: true,
    reason: null,
    devices: value.devices.length,
    segments: value.segments.length,
    band: Boolean(value.fixedBand),
  };
}

/** 命中测试：设备优先，其次缺口段。返回可直接用于详情面板的要素。 */
export function hitNavigationMonitoringFeature(model, point, {radius = 11} = {}) {
  if (!model?.drawable || !Array.isArray(point)) return null;
  const [x, y] = point;
  let best = null;
  let bestDistance = Infinity;
  for (const device of model.devices) {
    const screen = device.screen;
    if (!screen) continue;
    const distance = Math.hypot(screen[0] - x, screen[1] - y);
    if (distance <= radius && distance < bestDistance) {
      best = {kind: 'device', device};
      bestDistance = distance;
    }
  }
  if (best) return best;
  for (const segment of model.unassociatedSegments) {
    const distance = pointToSegmentDistance(point, segment.screenFrom, segment.screenTo);
    if (distance !== null && distance <= radius && distance < bestDistance) {
      best = {kind: 'gap', segment};
      bestDistance = distance;
    }
  }
  return best;
}

/** 点到线段距离（屏幕像素）；端点缺失时返回 ``null``（不猜）。 */
export function pointToSegmentDistance(point, from, to) {
  if (!Array.isArray(from) || !Array.isArray(to)) return null;
  const [px, py] = point;
  const [x1, y1] = from;
  const [x2, y2] = to;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared <= 0) return Math.hypot(px - x1, py - y1);
  const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lengthSquared));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/**
 * 点击要素 → 中文详情。
 *
 * 设备详情包含：设备来源、关联身份、证据状态；
 * 缺口详情包含：缺口长度、经过时间、容忍判断、证据状态。
 */
export function navigationMonitoringFeatureTooltip(feature, model, {escapeHtml = value => String(value ?? '')} = {}) {
  if (!feature) return '';
  const row = (label, value) => '<div class="list-row"><span><b>'
    + escapeHtml(label) + '</b></span><small>' + escapeHtml(value ?? '—') + '</small></div>';
  if (feature.kind === 'device') {
    const device = feature.device;
    const rows = [
      row('站址身份', device.siteId),
      row('设备来源', device.provisioning ? provisioningText(device.provisioning) : '—'),
      row('关联身份', device.role === 'mandatory' ? '起终点强制设备' : '可选监测站址'),
      row('设备标识', device.deviceId ?? '—'),
      row('候选种类', device.candidateKind ?? '—'),
      row('坐标来源 CRS', device.sourceCrs ?? '—'),
      row('空间证据状态', device.spatialUseStatus ?? '—'),
    ];
    if (device.spatialBlockReason) rows.push(row('空间阻断原因', device.spatialBlockReason));
    return '<div class="navigation-feature"><h4>监测站址</h4>'
      + '<div class="scroll-list">' + rows.join('') + '</div>'
      + '<small>仿真规划的设备尚未建成；复用既有设备只表示"已有设备可用"，不代表已通过性能验证。</small></div>';
  }
  const segment = feature.segment;
  const gap = (model?.gapRows || []).find(item =>
    item.startM === segment.startM && item.endM === segment.endM) || null;
  const rows = [
    row('航段类型', '未关联航段（缺口）'),
    row('缺口长度', gap?.lengthM === null || gap?.lengthM === undefined
      ? '—' : gap.lengthM.toFixed(2) + ' m'),
    row('经过时间', gap?.durationS === null || gap?.durationS === undefined
      ? '—' : gap.durationS.toFixed(2) + ' s'),
    row('容忍判定', gap?.toleranceStatusText ?? '—'),
    row('容忍阈值', gap?.gapToleranceS === null || gap?.gapToleranceS === undefined
      ? '—' : gap.gapToleranceS.toFixed(1) + ' s'),
    row('速度证据状态', gap?.speedSource ?? '—'),
    row('里程区间', segment.startM.toFixed(2) + ' – ' + segment.endM.toFixed(2) + ' m'),
  ];
  return '<div class="navigation-feature"><h4>未关联航段</h4>'
    + '<div class="scroll-list">' + rows.join('') + '</div>'
    + '<small>「未关联」只表示该航段不在监测关联范围内，不表示该航段不安全或已失去服务。</small></div>';
}

/** 图例分区：导航监测规划（只在图层开启时进入地图图例）。 */
export const NAVIGATION_MONITORING_LEGEND_LAYER = 'navigationMonitoring';
export const NAVIGATION_MONITORING_LEGEND_CHECKBOX = 'navigationMonitoringLayer';
export const NAVIGATION_MONITORING_LEGEND_SYMBOLS = Object.freeze([
  'navigation-route', 'navigation-mandatory-device', 'navigation-tower-device',
  'navigation-associated-segment', 'navigation-unassociated-segment',
  'navigation-fixed-corridor',
]);

/** 为命中测试补齐屏幕坐标（纯函数，便于测试）。 */
export function withScreenCoordinates(model, screenPoint) {
  if (!model?.drawable) return model;
  return {
    ...model,
    devices: model.devices.map(device => ({
      ...device, screen: screenPoint(device.coordinate),
    })),
    unassociatedSegments: model.unassociatedSegments.map(segment => ({
      ...segment, screenFrom: screenPoint(segment.from), screenTo: screenPoint(segment.to),
    })),
  };
}
