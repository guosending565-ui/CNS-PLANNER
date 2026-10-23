// =========================================================
// P16-V2 工程假设方案 · 二维地图叠加（**独立、只读**）
//
// 职责边界：
//  * 只消费任务结果里的 ``selected_sites`` / ``selected_actions`` / ``unmet_voxels``；
//  * 绝不发请求、绝不写业务状态、绝不修改正式 P16 结果；
//  * 用**虚线 / 空心**样式明确区分「工程假设、只读」与正式方案，避免被误认为
//    已经 Apply 的正式布设；
//  * 与三维系统无关：本模块只画二维 canvas，不 import 任何 visualization3d 代码。
// =========================================================

export const P16_V2_STYLE = {
  communication: {color: '#1574d4', fill: '#ffffff'},
  surveillance: {color: '#0f8a78', fill: '#ffffff'},
  ring: {lineWidth: 1.8, dash: [5, 4], radius: 8},
  core: {radius: 3.2, lineWidth: 1.4},
  label: {font: '11px sans-serif', color: '#1f2d3d', offsetY: -12},
  unmet: {color: '#c0392b', size: 4},
  disclaimer: 'P16-V2 · 工程假设（只读，不写入正式方案）',
};

export const P16_V2_SUBSYSTEM_COLORS = {
  C: P16_V2_STYLE.communication.color,
  S: P16_V2_STYLE.surveillance.color,
};

function numberOrNull(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function coordinateOf(value) {
  if (!Array.isArray(value) || value.length < 2) return null;
  const lon = numberOrNull(value[0]), lat = numberOrNull(value[1]);
  if (lon === null || lat === null) return null;
  if (lon < -180 || lon > 180 || lat < -90 || lat > 90) return null;
  return [lon, lat];
}

/**
 * 把 V2 任务结果规范化为可绘制模型（纯函数，不读 DOM）。
 *
 * @param {object|null} result V2 任务 `result_summary`
 * @returns {{drawable:boolean, sites:Array, unmet:Array, disclaimer:string, reason:string}}
 */
export function p16V2OverlayModel(result) {
  const summary = result && typeof result === 'object' ? result : null;
  if (!summary) {
    return {drawable: false, sites: [], unmet: [], disclaimer: P16_V2_STYLE.disclaimer, reason: 'no_result'};
  }
  const sites = new Map();
  for (const action of summary.selected_actions || []) {
    const coordinate = coordinateOf(action?.coordinate);
    if (!coordinate) continue;
    const key = String(action?.distinct_site_id || action?.site_id || coordinate.join(','));
    if (!sites.has(key)) {
      sites.set(key, {
        key,
        coordinate,
        name: String(action?.name || action?.site_id || ''),
        subsystems: new Set(),
      });
    }
    const code = String(action?.subsystem || '').trim();
    if (code) sites.get(key).subsystems.add(code);
  }
  const unmet = [];
  for (const item of summary.unmet_voxels || []) {
    const coordinate = coordinateOf(item?.coordinate);
    if (!coordinate) continue;
    unmet.push({
      coordinate,
      subsystem: String(item?.subsystem || ''),
      status: String(item?.status || ''),
      voxel_id: item?.voxel_id || null,
    });
  }
  return {
    drawable: sites.size > 0 || unmet.length > 0,
    sites: [...sites.values()].map(site => ({
      ...site,
      subsystems: [...site.subsystems].sort(),
    })),
    unmet,
    disclaimer: P16_V2_STYLE.disclaimer,
    reason: sites.size ? 'ok' : (unmet.length ? 'unmet_only' : 'no_geometry'),
  };
}

/** 画一个站址：按子系统画虚线外环 + 实心内点（工程假设样式）。 */
function drawSite(ctx, point, site) {
  const [x, y] = point;
  const codes = site.subsystems.length ? site.subsystems : [''];
  ctx.save();
  codes.forEach((code, index) => {
    const color = P16_V2_SUBSYSTEM_COLORS[code] || '#6b7280';
    const radius = P16_V2_STYLE.ring.radius + index * 3.2;
    ctx.beginPath();
    ctx.setLineDash(P16_V2_STYLE.ring.dash);
    ctx.lineWidth = P16_V2_STYLE.ring.lineWidth;
    ctx.strokeStyle = color;
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.stroke();
  });
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.arc(x, y, P16_V2_STYLE.core.radius, 0, Math.PI * 2);
  ctx.fillStyle = '#ffffff';
  ctx.fill();
  ctx.lineWidth = P16_V2_STYLE.core.lineWidth;
  ctx.strokeStyle = P16_V2_SUBSYSTEM_COLORS[codes[0]] || '#6b7280';
  ctx.stroke();
  if (site.name) {
    ctx.font = P16_V2_STYLE.label.font;
    ctx.fillStyle = P16_V2_STYLE.label.color;
    ctx.fillText(site.name, x + 11, y + P16_V2_STYLE.label.offsetY / 2);
  }
  ctx.restore();
}

/** 画未满足体元（只在方案非 feasible 时才会出现）。 */
function drawUnmet(ctx, points) {
  ctx.save();
  ctx.strokeStyle = P16_V2_STYLE.unmet.color;
  ctx.lineWidth = 1.6;
  for (const point of points) {
    const [x, y] = point;
    const size = P16_V2_STYLE.unmet.size;
    ctx.beginPath();
    ctx.moveTo(x - size, y - size);
    ctx.lineTo(x + size, y + size);
    ctx.moveTo(x + size, y - size);
    ctx.lineTo(x - size, y + size);
    ctx.stroke();
  }
  ctx.restore();
}

/**
 * 唯一绘制入口。
 *
 * @param {{ctx:CanvasRenderingContext2D, view:object, screenPoint:Function, result:object}} options
 * @returns {{drawn:number, skipped:number}}
 */
export function drawP16V2Overlay({ctx, view, screenPoint, result}) {
  const model = p16V2OverlayModel(result);
  if (!view || !model.drawable || typeof screenPoint !== 'function') {
    return {drawn: 0, skipped: 0};
  }
  let drawn = 0, skipped = 0;
  for (const site of model.sites) {
    const point = screenPoint(site.coordinate);
    if (!Array.isArray(point) || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) {
      skipped += 1;
      continue;
    }
    drawSite(ctx, point, site);
    drawn += 1;
  }
  const unmetPoints = [];
  for (const item of model.unmet) {
    const point = screenPoint(item.coordinate);
    if (Array.isArray(point) && Number.isFinite(point[0]) && Number.isFinite(point[1])) {
      unmetPoints.push(point);
    } else {
      skipped += 1;
    }
  }
  if (unmetPoints.length) drawUnmet(ctx, unmetPoints);
  return {drawn, skipped, unmet: unmetPoints.length};
}
