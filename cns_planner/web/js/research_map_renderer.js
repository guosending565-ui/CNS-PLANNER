/** Canvas 2D renderer for the research map. Pure display; no verdict is derived. */
import { lonLatToMercator, mercatorToLonLat, screenPoint } from './map/projection.js';

const COLORS = {
  C: '#0f8a78', RID: '#2f9e6f', Radar: '#c62828', S: '#69737c', N: '#7256a1',
  route: '#123a5c', host: '#1f6f8b', planned: '#d97706', ink: '#1d2a35',
};

function pointInPolyline() { return false; }

/** `map_projection.n_actions[].kind` 是旧投影的显示标签（端点动作为 `other`）；
 * 端点/中段的权威判据是 `planner_family` 与 `segment_index`，展示端按此复算标签，
 * 不改变任何业务结论。 */
export function actionIsEndpoint(action) {
  const family = String(action?.planner_family || '');
  return action?.kind === 'endpoint'
    || family.includes('endpoint_integrity')
    || (action?.segment_index == null && !family.includes('middle'));
}

export function shortSiteLabel(site) {
  const id = String(site?.site_id || '');
  if (site?.kind === 'planned_new_site') {
    const parts = id.split('-');
    return parts.length >= 2 ? `规划 ${parts[0]}-${parts[1]}` : `规划 ${id}`;
  }
  if (id.startsWith('takeoff_landing_site:')) return `起降点 ${id.split(':').pop()}`;
  if (id.startsWith('tower-colocation:')) return `塔共址 …${id.slice(-4)}`;
  return id;
}

function boxesOverlap(left, right) {
  return !(left.right < right.left || left.left > right.right
    || left.bottom < right.top || left.top > right.bottom);
}

export function createResearchMap({ canvas, onSelect, getModel, getVisibility }) {
  let view = null, dragging = null, selected = null, model = null, serial = 0;

  function size() {
    return [Math.max(1, Math.round(canvas.clientWidth)),
      Math.max(1, Math.round(canvas.clientHeight))];
  }

  function fit() {
    model = getModel();
    if (!model) return;
    const points = [];
    for (const point of model.route?.path || []) points.push(point);
    for (const site of model.sites || []) points.push(site.coordinate);
    for (const action of model.actions || model.n_actions || []) {
      if (action.coordinate) points.push(action.coordinate);
    }
    if (!points.length) return;
    const projected = points.map(point => lonLatToMercator(...point));
    const xs = projected.map(point => point[0]), ys = projected.map(point => point[1]);
    const [w, h] = size();
    const dx = Math.max(400, Math.max(...xs) - Math.min(...xs));
    const dy = Math.max(400, Math.max(...ys) - Math.min(...ys));
    view = {
      x: (Math.max(...xs) + Math.min(...xs)) / 2,
      y: (Math.max(...ys) + Math.min(...ys)) / 2,
      res: Math.max(dx / (w * 0.62), dy / (h * 0.56)),
    };
    requestAnimationFrame(draw);
  }

  function project(coordinate) {
    const [w, h] = size();
    return screenPoint(coordinate, view, w, h);
  }

  function drawCircle(ctx, center, radiusM, color, dashed, alpha = 0.08) {
    if (!(radiusM > 0)) return;
    const [x, y] = project(center);
    const radius = radiusM / view.res;
    if (!Number.isFinite(radius) || radius < 1) return;
    ctx.save();
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.globalAlpha = alpha;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = color;
    ctx.lineWidth = dashed ? 1.6 : 1.2;
    if (dashed) ctx.setLineDash([6, 5]);
    ctx.stroke();
    ctx.restore();
  }

  function draw() {
    if (!view || !model) return;
    const dpr = window.devicePixelRatio || 1;
    const [w, h] = size();
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#eef3f5';
    ctx.fillRect(0, 0, w, h);
    const labels = [];

    ctx.save();
    ctx.strokeStyle = 'rgba(70,90,105,.10)';
    ctx.lineWidth = 1;
    const step = 80;
    for (let x = (w / 2 - view.x / view.res) % step; x < w; x += step) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }
    for (let y = (h / 2 + view.y / view.res) % step; y < h; y += step) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.restore();

    const visibility = getVisibility();
    const layerById = new Map((model.layers || []).map(layer => [layer.id, layer]));

    for (const id of ['C', 'RID', 'S']) {
      if (!visibility[id]) continue;
      const layer = layerById.get(id);
      if (!layer) continue;
      const dashed = layer.status !== 'satisfied';
      for (const feature of layer.features || []) {
        const alpha = id === 'S' ? 0
          : layer.status === 'satisfied' ? 0.06 : 0.025;
        drawCircle(ctx, feature.center, feature.radius_m, COLORS[id], dashed, alpha);
      }
    }

    if (visibility.route !== false && model.route?.path?.length) {
      ctx.save();
      ctx.strokeStyle = COLORS.route;
      ctx.lineWidth = 4;
      ctx.lineJoin = 'round';
      ctx.beginPath();
      model.route.path.forEach((point, index) => {
        const [x, y] = project(point);
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.strokeStyle = 'rgba(255,255,255,.8)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
      const [sx, sy] = project(model.route.path[0]);
      const [ex, ey] = project(model.route.path[model.route.path.length - 1]);
      ctx.fillStyle = COLORS.route;
      ctx.beginPath(); ctx.arc(sx, sy, 5, 0, Math.PI * 2); ctx.fill();
      ctx.beginPath(); ctx.arc(ex, ey, 5, 0, Math.PI * 2); ctx.fill();
      labels.push({x: sx, y: sy, text: model.route.route_id || 'R0005', priority: 40,
        color: COLORS.route});
      ctx.restore();
    }

    if (visibility.N) {
      const layer = layerById.get('N');
      for (const feature of layer?.features || []) {
        if (!feature.coordinate) continue;
        const [x, y] = project(feature.coordinate);
        const isEndpoint = actionIsEndpoint(feature);
        ctx.save();
        ctx.strokeStyle = COLORS.N;
        ctx.lineWidth = 2.2;
        ctx.setLineDash(isEndpoint ? [] : [4, 3]);
        ctx.beginPath();
        ctx.arc(x, y, isEndpoint ? 13 : 16, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(114,86,161,.14)';
        ctx.fill();
        const label = isEndpoint ? 'N 端点' : `N 中段 seg${feature.segment_index ?? '?'}`;
        labels.push({x, y: y - 16, text: label, priority: isEndpoint ? 90 : 85,
          color: COLORS.N});
        ctx.restore();
      }
    }

    for (const site of model.sites || []) {
      const [x, y] = project(site.coordinate);
      const planned = site.kind === 'planned_new_site';
      ctx.save();
      ctx.fillStyle = planned ? COLORS.planned : COLORS.host;
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      if (planned) {
        ctx.moveTo(x, y - 7); ctx.lineTo(x + 7, y); ctx.lineTo(x, y + 7); ctx.lineTo(x - 7, y);
        ctx.closePath();
      } else {
        ctx.rect(x - 6, y - 6, 12, 12);
      }
      ctx.fill();
      ctx.stroke();
      if (selected?.type === 'site' && selected.id === site.site_id) {
        ctx.strokeStyle = '#111'; ctx.lineWidth = 2.4;
        ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2); ctx.stroke();
      }
      labels.push({x, y: y + 8, text: shortSiteLabel(site),
        priority: planned ? 80 : 60, color: COLORS.ink});
      ctx.restore();
    }

    labels.sort((left, right) => right.priority - left.priority);
    const placed = [];
    ctx.font = '10px "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.lineJoin = 'round';
    for (const label of labels) {
      const width = ctx.measureText(label.text).width + 8;
      const height = 14;
      const candidates = [
        {x: label.x + 9, align: 'left', top: label.y - 9},
        {x: label.x - 9, align: 'right', top: label.y - 9},
        {x: label.x + 9, align: 'left', top: label.y + 6},
        {x: label.x - 9, align: 'right', top: label.y + 6},
        {x: label.x + 9, align: 'left', top: label.y - 23},
      ];
      for (const candidate of candidates) {
        const left = candidate.align === 'left' ? candidate.x : candidate.x - width;
        const box = {
          left, right: left + width, top: candidate.top, bottom: candidate.top + height,
        };
        if (box.left < 3 || box.right > w - 3 || box.top < 3 || box.bottom > h - 3) continue;
        if (placed.some(other => boxesOverlap(box, other))) continue;
        placed.push(box);
        ctx.textAlign = candidate.align;
        ctx.strokeStyle = 'rgba(255,255,255,.92)';
        ctx.lineWidth = 3;
        ctx.strokeText(label.text, candidate.x, candidate.top + 10);
        ctx.fillStyle = label.color;
        ctx.fillText(label.text, candidate.x, candidate.top + 10);
        break;
      }
    }

    const radarLayer = layerById.get('Radar');
    if (visibility.Radar && radarLayer) {
      ctx.save();
      ctx.fillStyle = 'rgba(198,40,40,.08)';
      ctx.strokeStyle = COLORS.Radar;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.2;
      ctx.fillRect(310, 18, 264, 44);
      ctx.strokeRect(310, 18, 264, 44);
      ctx.setLineDash([]);
      ctx.fillStyle = COLORS.Radar;
      ctx.font = 'bold 12px "Segoe UI", sans-serif';
      ctx.fillText('Radar: not_calculated / unknown', 322, 37);
      ctx.font = '10px "Segoe UI", sans-serif';
      ctx.fillText('缺 radar_surveillance_layout，不是“已满足”', 322, 53);
      ctx.restore();
    }

    ctx.save();
    ctx.fillStyle = 'rgba(255,255,255,.9)';
    ctx.strokeStyle = '#c7d1d8';
    ctx.fillRect(w - 126, h - 38, 110, 22);
    ctx.strokeRect(w - 126, h - 38, 110, 22);
    ctx.fillStyle = COLORS.ink;
    ctx.font = '10px "Segoe UI", sans-serif';
    const metres = Math.round(1200 / view.res / 100) * 100;
    const scale = metres / view.res;
    ctx.fillText(`${metres} m`, w - 119, h - 23);
    ctx.strokeStyle = COLORS.ink;
    ctx.beginPath(); ctx.moveTo(w - 64, h - 20); ctx.lineTo(w - 64 + Math.min(50, scale), h - 20); ctx.stroke();
    ctx.restore();
  }

  function hit(event) {
    if (!view || !model) return null;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    let best = null, bestDistance = 18;
    for (const site of model.sites || []) {
      const [px, py] = project(site.coordinate);
      const distance = Math.hypot(px - x, py - y);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = {type: 'site', id: site.site_id, item: site};
      }
    }
    for (const action of model.n_actions || []) {
      if (!action.coordinate) continue;
      const [px, py] = project(action.coordinate);
      const distance = Math.hypot(px - x, py - y);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = {type: 'action', id: action.action_id, item: action};
      }
    }
    return best;
  }

  canvas.addEventListener('mousedown', event => {
    dragging = {x: event.clientX, y: event.clientY, view: {...view}};
    canvas.style.cursor = 'grabbing';
  });
  window.addEventListener('mousemove', event => {
    if (!dragging) return;
    view.x = dragging.view.x - (event.clientX - dragging.x) * view.res;
    view.y = dragging.view.y + (event.clientY - dragging.y) * view.res;
    draw();
  });
  window.addEventListener('mouseup', () => {
    dragging = null;
    canvas.style.cursor = 'grab';
  });
  canvas.addEventListener('wheel', event => {
    if (!view) return;
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    const wx = view.x + (mx - size()[0] / 2) * view.res;
    const wy = view.y - (my - size()[1] / 2) * view.res;
    const factor = event.deltaY > 0 ? 1.12 : 0.89;
    view.res = Math.max(0.15, Math.min(2000, view.res * factor));
    view.x = wx - (mx - size()[0] / 2) * view.res;
    view.y = wy + (my - size()[1] / 2) * view.res;
    draw();
  }, {passive: false});
  canvas.addEventListener('click', event => {
    const found = hit(event);
    selected = found;
    if (found) {
      const item = found.item;
      if (found.type === 'site') {
        onSelect?.({
          title: item.site_id,
          kind: item.kind === 'planned_new_site' ? '规划新建站址' : '既有宿主',
          rows: [
            ['设备', `${item.device_count} 台 (${item.subsystems.join('/')})`],
            ['复用类型', item.reuse_class || 'unknown'],
            ['实际交付', 'unknown'],
            ['研究身份', 'research_isolated'],
          ],
        });
      } else {
        onSelect?.({
          title: item.action_id,
          kind: actionIsEndpoint(item) ? 'N 端点动作' : 'N 中段动作',
          rows: [
            ['站点', item.site_id || 'unknown'],
            ['航段', item.segment_index ?? '端点'],
            ['状态', 'proposal_only（未安装）'],
            ['实际交付', 'unknown'],
          ],
        });
      }
    } else {
      onSelect?.(null);
    }
    draw();
  });
  canvas.addEventListener('mousemove', event => {
    if (dragging) return;
    canvas.style.cursor = hit(event) ? 'pointer' : 'grab';
  });
  window.addEventListener('resize', () => requestAnimationFrame(draw));

  return {
    load(nextModel) {
      model = nextModel;
      fit();
    },
    draw,
    fit,
    reset() { selected = null; fit(); },
    view: () => ({...view}),
    model: () => model,
  };
}
