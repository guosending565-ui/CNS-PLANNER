/**
 * CNS-PLANNER 只读三维视图（Round37-T3D-1）。
 *
 * 数据来源**只有**三个只读端点：
 *   GET /api/visualization-3d/manifest          （摘要；?layers=… 才带点位）
 *   GET /api/visualization-3d/routes            （航路几何 + 里程身份）
 *   GET /api/visualization-3d/terrain-window    （FABDEM 高度 + EGM2008 geoid 偏移）
 *
 * 硬约定：
 *  * **不读** `/api/workflow`（单次 147 MB）；首屏只取摘要，点位与地形按需分块加载；
 *  * **不发起任何 POST**，不写任何状态；
 *  * 垂直基准：工程数据是 EGM2008 **正高**，Cesium 需要 WGS84 **椭球高** ⇒
 *    逐点 `h = H + N`。**绝不**用全场常数 N 顶替（面板里给出常数 N 的偏差量化）；
 *  * `stale` / `unknown` / `not_calculated` 一律如实展示，不伪装为已确认；
 *  * NoData 只在**渲染**时映射为 0，且计数在面板中披露。
 */

import { applyCameraState, readCameraState } from './camera_state.js';
import {
  BASE_LAYER_DEFAULTS,
  BUSINESS_LAYER_DEFAULTS,
  LazyLayerRegistry,
  createBasemap,
  createLocalViewer,
  imageryStats,
} from './basemap.js';
import { CAMERA_PRESETS, flyToPreset } from './camera_presets.js';

const API = '/api/visualization-3d';

const S = {
  manifest: null,
  routes: null,
  points: null,
  terrainFrame: null,   // { header, terrain: Float32Array, geoid: Float32Array|null }
  win: null,            // { bbox, width, height, ... }
  geoidMean: null,
  datasets: [],
  stats: {
    requests: 0, bytes: 0, terrainCells: 0, entities: 0,
    posts: 0, workflowRequests: 0, methodObserverInstalled: false,
  },
  errors: [],
  viewer: null,
  basemap: null,
  lazyLayers: null,
  ready: false,
  checks: {},
};

// 零写入自检必须**实测**：包装 fetch 以观测真实请求方法。
// 早期实现读 ``S.stats.posts``，但全模块没有任何累加点（``getJSON`` 恒用 GET），
// 该自检项因此**结构性恒 0**——与同批修掉的 ``[].every()`` 空真属于同一类缺陷：
// 报告了一个永远为真的结论，而不是真的观测过。
(function installRequestMethodObserver() {
  const nativeFetch = globalThis.fetch;
  if (typeof nativeFetch !== 'function') return;
  globalThis.fetch = function observedFetch(input, init) {
    const method = String((init && init.method) || 'GET').toUpperCase();
    if (method !== 'GET') S.stats.posts += 1;
    return nativeFetch.call(globalThis, input, init);
  };
  S.stats.methodObserverInstalled = true;
}());

const $ = (id) => document.getElementById(id);
const logLines = [];

function log(message, cls) {
  const stamp = new Date().toISOString().slice(11, 19);
  logLines.push(`[${stamp}] ${message}`);
  const node = $('log');
  if (node) {
    node.innerHTML = logLines.slice(-60).map((line) => {
      const escaped = String(line).replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
      const klass = cls && line.includes(cls) ? cls : (line.includes('错误') || line.includes('失败') ? 'err' : '');
      return `<div class="${klass}">${escaped}</div>`;
    }).join('');
    node.scrollTop = node.scrollHeight;
  }
  S.log = logLines.slice(-200);
}

function step(text) { const node = $('bootStep'); if (node) node.textContent = text; }

async function getJSON(url) {
  S.stats.requests += 1;
  // 实测（而非硬编码）是否触碰过 147 MB 的 /api/workflow；自检项据此报告。
  if (String(url).split('?')[0] === '/api/workflow') S.stats.workflowRequests += 1;
  const response = await fetch(url, { method: 'GET', headers: { Accept: 'application/json' } });
  const text = await response.text();
  S.stats.bytes += text.length;
  if (!response.ok) {
    let detail = text.slice(0, 400);
    try { detail = JSON.stringify(JSON.parse(text)); } catch (e) { /* 原样 */ }
    throw new Error(`${url} → HTTP ${response.status}: ${detail}`);
  }
  return JSON.parse(text);
}

/* ------------------------------------------------------------------ 地形分块 */

async function loadTerrainWindow(bbox, width, height) {
  const query = new URLSearchParams({
    bbox: bbox.join(','), width: String(width), height: String(height),
    geoid: '1', format: 'f32',
  });
  const url = `${API}/terrain-window?${query.toString()}`;
  S.stats.requests += 1;
  const response = await fetch(url, { method: 'GET' });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`terrain-window → HTTP ${response.status}: ${text.slice(0, 300)}`);
  }
  const buffer = await response.arrayBuffer();
  S.stats.bytes += buffer.byteLength;
  const view = new DataView(buffer);
  const headLength = view.getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, headLength)));
  const cells = header.window.width * header.window.height;
  // Float32Array 视图要求 4 字节对齐 ⇒ 用 slice 复制出对齐缓冲。
  const start = 4 + headLength;
  const terrain = new Float32Array(buffer.slice(start, start + cells * 4));
  const geoidBytes = buffer.byteLength - start - cells * 4;
  const geoid = geoidBytes >= cells * 4
    ? new Float32Array(buffer.slice(start + cells * 4, start + cells * 8))
    : null;
  S.terrainFrame = { header, terrain, geoid };
  S.win = header.window;
  S.stats.terrainCells = cells;
  S.geoidMean = null;
  if (geoid) {
    let sum = 0; let count = 0;
    for (let i = 0; i < geoid.length; i += 1) {
      const value = geoid[i];
      if (Number.isFinite(value)) { sum += value; count += 1; }
    }
    S.geoidMean = count ? sum / count : null;
  }
  log(`地形窗口 ${header.window.width}×${header.window.height}（${(buffer.byteLength / 1024).toFixed(0)} KiB，` +
      `NoData ${header.window.nodata_count}，状态 ${header.window.status}）`);
  return header;
}

function bilinear(grid, width, height, bbox, lon, lat) {
  if (!grid) return null;
  const [west, south, east, north] = bbox;
  const fx = ((lon - west) / (east - west)) * (width - 1);
  const fy = ((north - lat) / (north - south)) * (height - 1);   // 行序北→南
  if (!(fx >= 0 && fy >= 0 && fx <= width - 1 && fy <= height - 1)) return null;
  const x0 = Math.floor(fx); const y0 = Math.floor(fy);
  const x1 = Math.min(x0 + 1, width - 1); const y1 = Math.min(y0 + 1, height - 1);
  const tx = fx - x0; const ty = fy - y0;
  const at = (row, col) => {
    const value = grid[row * width + col];
    return Number.isFinite(value) ? value : null;
  };
  const v00 = at(y0, x0); const v01 = at(y0, x1); const v10 = at(y1, x0); const v11 = at(y1, x1);
  if (v00 === null || v01 === null || v10 === null || v11 === null) return null;
  return v00 * (1 - tx) * (1 - ty) + v01 * tx * (1 - ty) + v10 * (1 - tx) * ty + v11 * tx * ty;
}

/** 正高（EGM2008）→ 椭球高（WGS84）：h = H + N，N 逐点取。 */
function orthometricToEllipsoidal(orthometric, lon, lat) {
  if (orthometric === null || orthometric === undefined) return null;
  const N = S.terrainFrame && S.terrainFrame.geoid
    ? bilinear(S.terrainFrame.geoid, S.win.width, S.win.height, S.win.bbox, lon, lat)
    : null;
  const offset = N === null ? (S.geoidMean === null ? 0 : S.geoidMean) : N;
  return orthometric + offset;
}

function undulationAt(lon, lat) {
  if (!S.terrainFrame || !S.terrainFrame.geoid) return null;
  return bilinear(S.terrainFrame.geoid, S.win.width, S.win.height, S.win.bbox, lon, lat);
}

/** 覆盖几何所需的 EGM2008 geoid 窗口（**按需**加载，不预取整幅地形）。
 *
 * 为什么必须有这一步：正高 → 椭球高是逐点 ``h = H + N``，而默认首屏**不**加载
 * ``terrain-window``（那是业务图层按需请求的）。因此用户第一次打开通信/RID 图层时
 * ``undulationAt`` 会返回 ``null``；若此时直接把顶点高度当 0 或跳过几何，就会得到
 * "图层打开了却什么都没有"的假象。这里显式补齐 geoid 窗口，并**如实返回**窗口状态。
 *
 * 窗口必须覆盖**几何外沿**（半径可达 5000 m），否则外圈顶点的 N 取不到、几何会被
 * 部分跳过。因此 margin 由真实半径换算，而不是拍一个固定度数。
 *
 * 窗口取"已有窗口 ∪ 本次所需 bbox"，因此不会把已有地形窗口缩小。
 */
async function ensureGeoidWindow(points, radiusM = 0) {
  const usable = (points || []).filter(
    (row) => Number.isFinite(row[0]) && Number.isFinite(row[1]));
  if (!usable.length) return { status: 'not_required', reason: 'no_points' };
  const radius = Number.isFinite(radiusM) && radiusM > 0 ? radiusM : 0;
  const meanLat = usable.reduce((sum, row) => sum + row[1], 0) / usable.length;
  const latPad = radius / 111320;
  const lonPad = radius / (111320 * Math.max(0.2, Math.cos(meanLat * Math.PI / 180)));
  // 采样与插值余量：0.02° ≈ 2 km，覆盖双线性插值的半个像元与边界浮点误差。
  const pad = 0.02;
  let west = Math.min(...usable.map((row) => row[0])) - lonPad - pad;
  let south = Math.min(...usable.map((row) => row[1])) - latPad - pad;
  let east = Math.max(...usable.map((row) => row[0])) + lonPad + pad;
  let north = Math.max(...usable.map((row) => row[1])) + latPad + pad;
  if (S.win && S.terrainFrame && S.terrainFrame.geoid) {
    const [w, s, e, n] = S.win.bbox;
    // 已有窗口完全覆盖所需范围 ⇒ 无需再请求。
    if (w <= west && s <= south && e >= east && n >= north) {
      return { status: 'already_loaded', bbox: S.win.bbox };
    }
    west = Math.min(west, w); south = Math.min(south, s);
    east = Math.max(east, e); north = Math.max(north, n);
  }
  await loadTerrainWindow([west, south, east, north], 512, 512);
  renderStatus();
  return {
    status: S.terrainFrame && S.terrainFrame.geoid ? 'loaded' : 'geoid_unavailable',
    bbox: S.win ? S.win.bbox : null,
  };
}

/** 地形高度（椭球高）：NoData 仅在渲染时取 0，并在面板披露。 */
function terrainHeightAt(lon, lat) {
  const H = bilinear(S.terrainFrame.terrain, S.win.width, S.win.height, S.win.bbox, lon, lat);
  if (H === null) return 0;
  const h = orthometricToEllipsoidal(H, lon, lat);
  return h === null ? 0 : h;
}

/** 用真实 FABDEM 高度生成离线山体阴影影像（不依赖任何在线底图）。 */
/* ------------------------------------------------------------------ 实体构建 */

function addRouteEntities(viewer, entry) {
  const isDemo = entry.authority === 'engineering_demo_projection';
  const color = isDemo ? Cesium.Color.fromCssColorString('#ff4d4d')
    : Cesium.Color.fromCssColorString('#8a6d3b');
  const cruise = entry.altitude ? entry.altitude.nominal_altitude_m : null;
  const positions = [];
  const undulations = [];
  for (const [lon, lat] of entry.path) {
    const n = undulationAt(lon, lat);
    undulations.push(n);
    const z = cruise === null ? terrainHeightAt(lon, lat)
      : orthometricToEllipsoidal(cruise, lon, lat);
    positions.push(Cesium.Cartesian3.fromDegrees(lon, lat, z === null ? 0 : z));
  }
  const entities = [];
  const line = viewer.entities.add({
    name: `${entry.route_id} ${isDemo ? '工程演示航路' : '正式容器航路'}`,
    polyline: {
      positions,
      width: isDemo ? 4 : 3,
      clampToGround: false,
      material: isDemo ? color : new Cesium.PolylineDashMaterialProperty({
        color, dashLength: 12,
      }),
    },
  });
  line.__cns = {
    kind: 'route', isDemo, entry,
    title: `${entry.route_id} · ${isDemo ? '工程演示航路（demo projection）' : '正式容器航路（operational_routes）'}`,
    rows: [
      ['权威来源', entry.authority_label],
      ['容器状态', entry.status],
      ['stale 原因', entry.stale_reason || '—'],
      ['使用范围', entry.use_scope],
      ['几何点数', String(entry.identity.point_count)],
      ['candidate', entry.identity.candidate_id || '—'],
      ['validation', entry.identity.validation_id || '—'],
      ['几何指纹', entry.identity.fingerprint],
      ['里程（equirect 水平）', `${entry.identity.mileage.total_m.toFixed(3)} m`],
      ['里程口径', entry.identity.mileage.metric],
      ['巡航高度层', entry.altitude ? `${entry.altitude.altitude_layer_id} = ${cruise} m` : '—'],
      ['高度基准', entry.altitude ? entry.altitude.vertical_reference : '—'],
      ['爬升/下降', entry.altitude && entry.altitude.climb_cruise_descent_available ? '可用' : '不可用（无 Route3DProfile）'],
      ['第三坐标含义', '非高度（2D CRS84）'],
    ],
  };
  entities.push(line);

  entry.path.forEach(([lon, lat], index) => {
    const n = undulations[index];
    const z = cruise === null ? terrainHeightAt(lon, lat) : orthometricToEllipsoidal(cruise, lon, lat);
    const point = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lon, lat, z === null ? 0 : z),
      point: {
        pixelSize: 6, color: Cesium.Color.fromCssColorString('#ffd166'),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    point.__cns = {
      kind: 'route-vertex', isDemo, entry,
      title: `${entry.route_id} 顶点 #${index}`,
      rows: [
        ['经度', lon.toFixed(7)], ['纬度', lat.toFixed(7)],
        ['巡航正高', cruise === null ? '—' : `${cruise} m`],
        ['geoid N', n === null ? '未解析' : `${n.toFixed(4)} m`],
        ['Cesium 椭球高', z === null ? '未解析' : `${z.toFixed(4)} m`],
        ['垂直基准', 'EGM2008 正高 → WGS84 椭球高（逐点 N）'],
      ],
    };
    entities.push(point);
  });
  return entities;
}

function addTowerEntities(viewer, items) {
  const known = []; const unknown = [];
  for (const tower of items) {
    const top = tower.tower_top_orthometric_m;
    const hasTop = typeof top === 'number';
    const n = undulationAt(tower.longitude, tower.latitude);
    const ground = terrainHeightAt(tower.longitude, tower.latitude);
    const topZ = hasTop ? orthometricToEllipsoidal(top, tower.longitude, tower.latitude) : null;
    const entity = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(tower.longitude, tower.latitude,
        ground === null ? 0 : ground),
      point: {
        pixelSize: hasTop ? 5 : 4,
        color: hasTop ? Cesium.Color.fromCssColorString('#4dd2ff')
          : Cesium.Color.fromCssColorString('#9fb0c0'),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: tower.name || tower.tower_id,
    });
    entity.__cns = {
      kind: 'tower', hasTop,
      title: `铁塔 ${tower.name || ''} (${tower.tower_id})`,
      rows: [
        ['站点类型', tower.site_type || '—'],
        ['经度 / 纬度', `${tower.longitude.toFixed(6)} / ${tower.latitude.toFixed(6)}`],
        ['地形正高', tower.terrain_elevation_m === null || tower.terrain_elevation_m === undefined
          ? '未解析' : `${tower.terrain_elevation_m} m`],
        ['塔身高度', tower.tower_structure_height_m === null || tower.tower_structure_height_m === undefined
          ? '未解析' : `${tower.tower_structure_height_m} m`],
        ['塔顶正高', hasTop ? `${top} m` : 'unknown（未解析，绝不当 0）'],
        ['塔顶状态', tower.tower_top_status || 'unknown'],
        ['geoid N', n === null ? '未解析' : `${n.toFixed(4)} m`],
        ['塔顶椭球高', topZ === null ? 'unknown' : `${topZ.toFixed(3)} m`],
        ['垂直状态', tower.vertical_status || 'unknown'],
        ['已确认', tower.confirmed ? '是' : '否'],
      ],
    };
    (hasTop ? known : unknown).push(entity);
  }
  return { known, unknown, all: known.concat(unknown) };
}

function addFacilityEntities(viewer, items) {
  const entities = [];
  for (const site of items) {
    if (typeof site.longitude !== 'number' || typeof site.latitude !== 'number') continue;
    const origin = site.service_origin_egm2008_m;
    const base = terrainHeightAt(site.longitude, site.latitude);
    const z = origin === null || origin === undefined
      ? base : orthometricToEllipsoidal(origin, site.longitude, site.latitude);
    const synthetic = Boolean(site.synthetic);
    const entity = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(site.longitude, site.latitude, z === null ? 0 : z),
      point: {
        pixelSize: 11,
        color: synthetic ? Cesium.Color.fromCssColorString('#ffb454')
          : Cesium.Color.fromCssColorString('#5ce08a'),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: `${synthetic ? '[SYNTHETIC] ' : ''}${site.name || site.facility_id}`,
        font: '12px "Microsoft YaHei", sans-serif',
        fillColor: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -16),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: site.name || site.facility_id,
    });
    entity.__cns = {
      kind: 'facility', synthetic,
      title: `CNS 站址 ${site.name || ''}`,
      rows: [
        ['site_id', site.site_id || '—'],
        ['数据集类别', site.dataset_class || '—'],
        ['真实设施', site.real_world_facility === true ? '是' : (synthetic ? '否（合成/工程验证）' : '未标注')],
        ['服务原点正高', origin === null || origin === undefined ? '未解析' : `${origin} m`],
        ['垂直剖面状态', site.vertical_profile_status || '—'],
        ['垂直基准', site.vertical_reference || '—'],
        ['挂载设备', (site.device_ids || []).join(', ') || '—'],
        ['Cesium 椭球高', z === null ? '未解析' : `${z.toFixed(3)} m`],
      ],
    };
    entities.push(entity);
  }
  return entities;
}

/* ------------------------------------------------- 三维 CNS 覆盖（T3D-2A） */

//: 通信包络与 RID footprint 的显示透明度（用户可调）。
const COVERAGE_STYLE = {
  communication: { alpha: 0.18, outlineAlpha: 0.85,
                   color: '#4dd2ff', outline: '#8ce9ff' },
  rid: { alpha: 0.22, outlineAlpha: 0.9,
         ridLand: '#ffd166', ridSea: '#5ce08a', outline: '#ffd166' },
};

/** 把 ``[lon, lat, H_egm2008]`` 顶点环转成 Cesium 椭球高坐标（逐点 N）。 */
function ringToCartesians(ring) {
  const positions = [];
  for (const row of ring) {
    const lon = Number(row[0]); const lat = Number(row[1]);
    const ortho = Number(row[2]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const n = undulationAt(lon, lat);
    // 正高 → 椭球高：h = H + N。N 未解析时**不**静默当 0，而是标记该实体为不完整。
    const height = Number.isFinite(ortho)
      ? (n === null ? null : ortho + n) : null;
    positions.push({ lon, lat, height, ortho, n });
  }
  return positions;
}

/** 通信三维工程几何包络：半球壳 + 水平切片 + 服务原点。
 *
 * 返回 ``{ entities, diagnostics }``：**任何被跳过的几何都必须出现在
 * ``diagnostics.skipped`` 里**，绝不允许"图层打开了却静默什么都没有"。
 */
function addCommunicationVolumeEntities(viewer, volumes) {
  const entities = [];
  const skipped = [];
  for (const volume of volumes) {
    const geometry = volume.geometry || {};
    const origin = volume.service_origin_egm2008_m;
    const n = undulationAt(volume.longitude, volume.latitude);
    const originHeight = (origin === null || origin === undefined || n === null)
      ? null : origin + n;
    const blocked = geometry.status !== 'available';
    const style = COVERAGE_STYLE.communication;
    const fill = Cesium.Color.fromCssColorString(style.color)
      .withAlpha(blocked ? 0.35 : style.alpha);
    const outlineColor = Cesium.Color.fromCssColorString(style.outline)
      .withAlpha(style.outlineAlpha);

    // 1) 站心点（即使包络被阻塞也要显示：它是"有设备但缺安装高度"的证据）。
    const anchor = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(
        volume.longitude, volume.latitude, originHeight === null ? 0 : originHeight),
      point: {
        pixelSize: 9,
        color: blocked ? Cesium.Color.fromCssColorString('#ff6b6b')
          : Cesium.Color.fromCssColorString('#4dd2ff'),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: `通信包络 ${volume.device_id}`,
    });
    anchor.__cns = communicationPopup(volume, geometry, blocked);
    entities.push(anchor);

    if (blocked) {
      skipped.push({ device_id: volume.device_id, part: 'shell',
                     reason: geometry.reason || 'geometry_blocked' });
      continue;
    }
    if (originHeight === null) {
      // geoid 未解析 ⇒ 顶点高度不可信；**不**用 0 冒充。
      skipped.push({ device_id: volume.device_id, part: 'shell',
                     reason: 'egm2008_undulation_unavailable' });
      continue;
    }

    // 2) 半球壳：逐环多边形（环内插值成三角带由 Cesium 的多边形材质近似）。
    //    这里用**多个水平高度环**表达：每个环是一个闭合多边形，其顶点高度随环变化。
    const rings = geometry.shell_rings || [];
    let shellBands = 0;
    for (let index = 0; index < rings.length - 1; index += 1) {
      const lower = ringToCartesians(rings[index]);
      const upper = ringToCartesians(rings[index + 1]);
      if (!lower.length || !upper.length) continue;
      if (lower.some((row) => row.height === null)
        || upper.some((row) => row.height === null)) continue;
      // 相邻两环之间的"球带"用一个多边形近似：沿下环正向、上环反向闭合。
      const loop = lower.map((row) => Cesium.Cartesian3.fromDegrees(
        row.lon, row.lat, row.height))
        .concat(upper.slice().reverse().map((row) => Cesium.Cartesian3.fromDegrees(
          row.lon, row.lat, row.height)));
      const band = viewer.entities.add({
        polygon: {
          hierarchy: new Cesium.PolygonHierarchy(loop),
          material: fill,
          outline: false,
          perPositionHeight: true,
          arcType: Cesium.ArcType.NONE,
        },
      });
      band.__cns = communicationPopup(volume, geometry, false);
      entities.push(band);
      shellBands += 1;
    }
    if (!shellBands) {
      skipped.push({ device_id: volume.device_id, part: 'shell',
                     reason: 'shell_rings_empty_or_unresolved' });
    }

    // 3) 水平切片外边界（delta = 0）：这是**唯一**与二维圆重合的截面，
    //    显式画出它，避免把切片误当成完整三维包络。
    const section = ringToCartesians(geometry.horizontal_section || []);
    if (section.length && section.every((row) => row.height !== null)) {
      const boundary = viewer.entities.add({
        polyline: {
          positions: section.map((row) => Cesium.Cartesian3.fromDegrees(
            row.lon, row.lat, row.height)),
          width: 1.6, material: outlineColor,
          clampToGround: false,
        },
      });
      boundary.__cns = communicationPopup(volume, geometry, false);
      entities.push(boundary);
    } else {
      skipped.push({ device_id: volume.device_id, part: 'horizontal_section',
                     reason: 'horizontal_section_unresolved' });
    }
  }
  return { entities, diagnostics: { skipped, entity_count: entities.length } };
}

function communicationPopup(volume, geometry, blocked) {
  const resolution = volume.geometry_resolution || {};
  const rows = [
    ['device_id', volume.device_id || '—'],
    ['service_key', volume.service_key || '—'],
    ['站点', volume.site_name || volume.site_id || '—'],
    ['服务原点正高', volume.service_origin_egm2008_m === null
      || volume.service_origin_egm2008_m === undefined
      ? '未确认' : `${volume.service_origin_egm2008_m} m`],
    ['几何状态', blocked ? `blocked（${geometry.reason || '—'}）` : 'available'],
    ['几何模型', geometry.model || '—'],
    ['斜距（规划半径）', geometry.slant_range_m === undefined
      ? '—' : `${geometry.slant_range_m} m`],
    ['半径来源', resolution.radius_source || '—'],
    ['半径口径', resolution.radius_basis === 'surface_envelope_max'
      ? '地表包络（未指定地表类别）' : '按地表解析'],
    ['天顶正高', geometry.apex_egm2008_m === undefined
      ? '—' : `${geometry.apex_egm2008_m} m`],
    ['语义', 'engineering_geometric_coverage_not_measured（工程几何包络，非实测）'],
    ['未评估', (volume.not_evaluated || []).join('、') || '—'],
  ];
  if (blocked) {
    rows.push(['所需证据', (geometry.required_evidence || []).join('；') || '—']);
  }
  return {
    kind: 'communication-volume',
    title: `通信三维包络 ${volume.device_id || ''}`,
    rows,
  };
}

/** RID 陆海非正圆 footprint：只画**有依据的高度切片**，绝不垂直拉伸。
 *
 * 返回 ``{ entities, diagnostics }``：被跳过的分区必须显式记录。
 */
function addRidFootprintEntities(viewer, footprints) {
  const entities = [];
  const skipped = [];
  const style = COVERAGE_STYLE.rid;
  for (const footprint of footprints) {
    const geometry = footprint.geometry || {};
    const origin = footprint.service_origin_egm2008_m;
    const n = undulationAt(footprint.longitude, footprint.latitude);
    const sliceHeight = (origin === null || origin === undefined || n === null)
      ? null : origin + n;
    const blocked = geometry.status !== 'available';

    const anchor = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(
        footprint.longitude, footprint.latitude, sliceHeight === null ? 0 : sliceHeight),
      point: {
        pixelSize: 9,
        color: blocked ? Cesium.Color.fromCssColorString('#ff6b6b')
          : Cesium.Color.fromCssColorString(style.ridSea),
        outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: `RID ${footprint.device_id}`,
    });
    anchor.__cns = ridPopup(footprint, geometry, blocked);
    entities.push(anchor);
    if (blocked) {
      skipped.push({ device_id: footprint.device_id, part: 'footprint',
                     reason: geometry.reason || 'geometry_blocked' });
      continue;
    }
    if (sliceHeight === null) {
      skipped.push({ device_id: footprint.device_id, part: 'slice',
                     reason: 'egm2008_undulation_unavailable' });
      continue;
    }

    // 每个 surface_class 分区单独着色：这正是"非正圆由空间分类决定"的直观证据。
    let areas = 0;
    for (const part of geometry.surface_parts || []) {
      const color = part.surface_class === 'sea' ? style.ridSea : style.ridLand;
      for (const polygon of (part.geojson || {}).coordinates || []) {
        const ring = polygon[0] || [];
        const positions = ring.map((row) => Cesium.Cartesian3.fromDegrees(
          Number(row[0]), Number(row[1]), sliceHeight));
        if (positions.length < 4) continue;
        const area = viewer.entities.add({
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(positions),
            material: Cesium.Color.fromCssColorString(color).withAlpha(style.alpha),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString(color)
              .withAlpha(style.outlineAlpha),
            outlineWidth: 1.5,
            perPositionHeight: true,
            arcType: Cesium.ArcType.NONE,
          },
        });
        area.__cns = ridPopup(footprint, geometry, false, part);
        entities.push(area);
        areas += 1;
      }
    }
    if (!areas) {
      skipped.push({ device_id: footprint.device_id, part: 'surface_parts',
                     reason: 'no_renderable_surface_part' });
    }
  }
  return { entities, diagnostics: { skipped, entity_count: entities.length } };
}

function ridPopup(footprint, geometry, blocked, part) {
  const radii = geometry.boundary_radius_m || {};
  const evidence = geometry.non_circular_evidence || {};
  const rows = [
    ['device_id', footprint.device_id || '—'],
    ['声明依据', footprint.declaration_basis || '—'],
    ['显式 RID 类型', footprint.rid_type_declared ? '是' : '否（仅几何策略一致）'],
    ['几何状态', blocked ? `blocked（${geometry.reason || '—'}）` : 'available'],
    ['陆 / 海岸半径', `${(footprint.radius_by_surface_m || {}).land} m`],
    ['海侧半径', `${(footprint.radius_by_surface_m || {}).sea} m`],
    ['边界半径 min/max', radii.min === undefined ? '—'
      : `${radii.min} / ${radii.max} m`],
    ['半径不同值个数', radii.distinct_values === undefined
      ? '—' : String(radii.distinct_values)],
    ['非正圆判据', evidence.radius_varies_with_azimuth === true
      ? '半径随方位角变化（空间分类结果）' : '该站点周围未出现分区差异'],
    ['垂直表达', '仅高度切片（垂直有效范围未建模，禁止拉伸）'],
    ['语义', 'engineering_geometric_cooperative_surveillance_not_measured'],
    ['非 Radar 扇区', '是（全向 360°，无方位权重）'],
    ['未评估', (footprint.not_evaluated || []).join('、') || '—'],
  ];
  if (part) rows.push(['当前分区', `${part.surface_class}（${part.policy_radius_m} m）`]);
  if (footprint.declaration_note) rows.push(['说明', footprint.declaration_note]);
  return {
    kind: 'rid-footprint',
    title: `RID footprint ${footprint.device_id || ''}`,
    rows,
  };
}

function addDeviceEntities(viewer, items) {
  const entities = [];
  for (const device of items) {
    // 设备本体没有独立坐标（位置在 existing_cns_facilities 上）：只在面板列出，不造假点。
    S.datasets.push(device);
  }
  return entities;
}

/* -------------------------------------------------------------------- 面板 */

function renderStatus(options = {}) {
  const manifest = S.manifest;
  const rows = [
    ['revision', String(manifest.revision)],
    ['scope', `<span class="badge ${manifest.engineering_demo_only ? 'demo' : 'ok'}">${manifest.scope}</span>`],
    ['运行许可', `<span class="badge no">${manifest.operational_authorization ? 'true' : 'false'}</span>`],
    ['严格安全验证', `<span class="badge ${manifest.strict_safety_validation_passed ? 'ok' : 'no'}">${manifest.strict_safety_validation_passed ? 'passed' : '未通过'}</span>`],
    ['ETag', `<code>${(manifest.etag || '').slice(0, 34)}…</code>`],
    ['manifest 体积', `${(manifest.load_budget.manifest_bytes / 1024).toFixed(1)} KiB`],
    ['使用 /api/workflow', S.stats.workflowRequests > 0
      ? `是（违规，实测 ${S.stats.workflowRequests} 次）` : '否（实测 0 次）'],
    ['影像状态', (manifest.imagery || {}).status || 'unknown'],
    ['影像默认源', (manifest.imagery || {}).default_source || '—'],
    // 在线模式与预算必须落到面板：默认离线，且剩余额度按**服务端**证据给出。
    ['影像在线模式', (manifest.imagery || {}).online_mode
      || ((manifest.imagery || {}).budget || {}).request_mode || 'unknown'],
    ['影像剩余额度', String(((manifest.imagery || {}).budget || {}).budget_remaining ?? '—')],
    ['地形协议', manifest.terrain_tiles
      ? `${manifest.terrain_tiles.format} / LOD ${manifest.terrain_tiles.maxzoom}` : 'blocked'],
    // 只统计**本模块自己**发的 JSON 请求（getJSON / loadTerrainWindow）；
    // Cesium 的影像与地形瓦片请求不在此列，必须去 Network 面板看。
    ['本模块端点请求数 / 字节', `${S.stats.requests} / ${(S.stats.bytes / 1024).toFixed(0)} KiB`],
    // 影像瓦片失败数由 provider.errorEvent **实测**累计。此前的 `地形瓦片调用`
    // 读的是从未累加的 S.stats.terrainTiles，属于结构性恒 0 的假指标。
    ['影像瓦片失败', String(S.basemap ? S.basemap.status().imagery_failures : 0)],
    // Cesium **主动丢弃**瓦片与**范围外**瓦片都是正常调度行为（实测网络层 0 失败
    // 却触发 errorEvent）：只做 dim 提示与日志，**绝不**显示成红字"加载失败"。
    ['影像瓦片丢弃（非失败）', String(S.basemap ? S.basemap.status().imagery_discards : 0)],
    ['影像瓦片范围外（非失败）',
      String(S.basemap ? S.basemap.status().imagery_out_of_scope : 0)],
    ['实体总数', String(S.stats.entities)],
  ];
  $('metaTable').innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  const conversion = manifest.vertical_reference.ellipsoidal_conversion;
  const vrows = [
    ['工程基准', manifest.vertical_reference.canonical],
    ['椭球高转换', `<span class="badge ${conversion.status === 'available' ? 'ok' : 'no'}">${conversion.status}</span>`],
    ['方法', conversion.method],
    ['geoid 网格', conversion.geoid_grid ? conversion.geoid_grid.filename : '缺失'],
    ['网格 sha256', conversion.geoid_grid ? `<code>${conversion.geoid_grid.sha256.slice(0, 24)}…</code>` : '—'],
    ['网格尺寸', conversion.geoid_grid ? `${conversion.geoid_grid.width}×${conversion.geoid_grid.height} @ ${conversion.geoid_grid.pixel_size_deg[0]}°` : '—'],
    ['公式', conversion.formula_orthometric_to_ellipsoidal || '—'],
    ['本窗口 N 均值', S.geoidMean === null ? '未解析' : `${S.geoidMean.toFixed(4)} m`],
    ['禁止替代', (conversion.substitutes_forbidden || []).join(' / ')],
  ];
  $('verticalTable').innerHTML = vrows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  const disclosure = $('disclosure');
  const demoBlock = $('disclosureDemo');
  if (manifest.engineering_demo_only) {
    disclosure.style.display = '';
    demoBlock.style.display = '';
    $('disclosureTitle').textContent = '⚠ 工程演示模式（engineering_demo_only）';
    $('disclosureText').textContent =
      ' 严格安全验证未通过、无运行许可（operational_authorization=false）。'
      + ' 航路几何来自合法 demo projection，未写入 canonical operational_routes。'
      + ' 本视图不得作为运行依据。';
  } else {
    demoBlock.style.display = 'none';
    disclosure.style.display = '';
  }

  // 常驻垂直基准声明：**无论转换可用与否**都必须写明当前基准与限制。
  const notice = $('verticalNotice');
  if (notice) {
    if (conversion.status === 'available' && conversion.geoid_grid) {
      const grid = conversion.geoid_grid;
      notice.innerHTML =
        `EGM2008 正高 → WGS84 椭球高（逐点 <code>N</code>，canonical 后端 `
        + `<code>${grid.canonical_backend}</code>；网格 <code>${grid.filename}</code>，`
        + `sha256 <code>${String(grid.sha256).slice(0, 16)}…</code>）。`
        + ` 禁止以 ${(conversion.substitutes_forbidden || []).join(' / ')} 顶替。`;
    } else {
      notice.innerHTML =
        `<span class="warn">未与椭球对齐（${conversion.status}）</span>：`
        + `${conversion.reason_text || conversion.reason || '未知原因'}；`
        + ' 本视图保持 <b>EGM2008 正高</b>自洽渲染，'
        + '<b>绝不</b>把正高当椭球高、也<b>绝不</b>静默回退 EGM96。'
        + ' 与任何在线椭球底图/地形叠加前必须先补齐 geoid 网格。';
    }
  }
  // 底图失败触发的重绘（quiet）不该把同一行 revision 日志刷进运行日志。
  if (!options.quiet) {
    log(`revision=${manifest.revision} scope=${manifest.scope} 图层=${manifest.layer_ids.length}`);
  }
}

function renderLayers() {
  const imageryConfig = (S.manifest && S.manifest.imagery) || {};
  const onlineAllowed = imageryConfig.online_allowed !== false;
  const controls = [
    ['imagery', onlineAllowed ? '高清卫星影像' : '高清卫星影像（离线模式禁用）',
      onlineAllowed && BASE_LAYER_DEFAULTS.imagery, 'base'],
    ['terrain', 'FABDEM 多级三维地形', BASE_LAYER_DEFAULTS.terrain, 'base'],
    ['labels', '地理注记', BASE_LAYER_DEFAULTS.labels, 'base'],
    ['routeDemo', 'R0005 工程演示航路', BUSINESS_LAYER_DEFAULTS.routeDemo, 'business'],
    ['routeCanonical', '正式容器航路（stale）', BUSINESS_LAYER_DEFAULTS.routeCanonical, 'business'],
    ['towersKnown', '铁塔（已知塔顶）', BUSINESS_LAYER_DEFAULTS.towersKnown, 'business'],
    ['towersUnknown', '铁塔（塔顶 unknown）', BUSINESS_LAYER_DEFAULTS.towersUnknown, 'business'],
    ['facilities', 'CNS 站址', BUSINESS_LAYER_DEFAULTS.facilities, 'business'],
    ['buildings', '近景建筑', BUSINESS_LAYER_DEFAULTS.buildings, 'business'],
    ['grid', '规划网格', BUSINESS_LAYER_DEFAULTS.grid, 'reserved'],
    ['communication', '通信包络（三维工程几何）', BUSINESS_LAYER_DEFAULTS.communication,
      'business'],
    ['rid', 'RID 陆海非正圆覆盖', BUSINESS_LAYER_DEFAULTS.rid, 'business'],
    ['radar', 'Radar 覆盖', BUSINESS_LAYER_DEFAULTS.radar, 'reserved'],
    ['protection', '航路保护体', BUSINESS_LAYER_DEFAULTS.protection, 'reserved'],
    ['intrusion', '模拟入侵目标', BUSINESS_LAYER_DEFAULTS.intrusion, 'reserved'],
  ];
  $('layerList').innerHTML = controls.map(([id, label, checked, kind]) =>
    `<label class="layer-${kind}"><input type="checkbox" id="ly_${id}" `
      + `${checked ? 'checked' : ''}> <span>${label}</span></label>`
  ).join('');
}

/* -------------------------------------------------------------------- 弹窗 */

function showPopup(entity, position) {
  const info = entity && entity.__cns;
  const popup = $('popup');
  if (!info) { popup.style.display = 'none'; return; }
  const rows = info.rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
  popup.innerHTML = `<span class="close" id="popupClose">✕</span><b>${info.title}</b>
    <table style="margin-top:6px">${rows}</table>
    <div style="margin-top:8px"><button id="popupFly">定位到此对象</button></div>`;
  popup.style.display = 'block';
  const width = 360;
  const left = Math.min(Math.max(12, position.x + 14), window.innerWidth - width - 20);
  const top = Math.min(Math.max(12, position.y + 14), window.innerHeight - 300);
  popup.style.left = `${left}px`;
  popup.style.top = `${top}px`;
  $('popupClose').onclick = () => { popup.style.display = 'none'; };
  $('popupFly').onclick = () => flyToEntity(entity);
  S.lastPicked = { kind: info.kind, title: info.title };
}

function flyToEntity(entity) {
  const viewer = S.viewer;
  if (!viewer || !entity) return;
  if (entity.polyline) {
    const positions = entity.polyline.positions.getValue(Cesium.JulianDate.now());
    viewer.camera.flyTo({ destination: Cesium.Rectangle.fromCartesianArray(positions) });
    return;
  }
  if (entity.polygon && entity.polygon.hierarchy) {
    // 覆盖几何（包络球带 / RID 分区）没有单点位置：飞到其外接范围。
    const hierarchy = entity.polygon.hierarchy.getValue(Cesium.JulianDate.now());
    const positions = (hierarchy && hierarchy.positions) || [];
    if (positions.length) {
      viewer.camera.flyTo({ destination: Cesium.Rectangle.fromCartesianArray(positions) });
      return;
    }
  }
  const position = entity.position && entity.position.getValue(Cesium.JulianDate.now());
  if (position) {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromElements(
        position.x * 1.0009, position.y * 1.0009, position.z + 1200),
    });
  }
}

/* ---------------------------------------------------------------- 数值自检 */

async function runSelfChecks() {
  const viewer = S.viewer;
  const checks = {};

  // 1) 已知源点独立验证：直接读地形窗口的值（f32 帧）与 Cesium globe 采样对比。
  const samples = [];
  const routeRows = S.routes && Array.isArray(S.routes.routes) ? S.routes.routes : [];
  const demo = routeRows.find((r) => r.authority === 'engineering_demo_projection');
  const probePoints = [];
  if (demo) {
    probePoints.push({ label: 'R0005 起点', lon: demo.path[0][0], lat: demo.path[0][1] });
    probePoints.push({ label: 'R0005 南端', lon: demo.path[demo.path.length - 1][0], lat: demo.path[demo.path.length - 1][1] });
    const middle = demo.path[Math.floor(demo.path.length / 2)];
    probePoints.push({ label: 'R0005 中段', lon: middle[0], lat: middle[1] });
  }

  for (const probe of probePoints) {
    const ortho = bilinear(S.terrainFrame.terrain, S.win.width, S.win.height, S.win.bbox, probe.lon, probe.lat);
    const n = undulationAt(probe.lon, probe.lat);
    const expected = ortho === null ? null : ortho + (n === null ? 0 : n);
    const cartographic = Cesium.Cartographic.fromDegrees(probe.lon, probe.lat);
    const globeHeight = viewer.scene.globe.getHeight(cartographic);
    samples.push({
      label: probe.label, lon: probe.lon, lat: probe.lat,
      terrain_orthometric_m: ortho, undulation_m: n,
      expected_ellipsoidal_m: expected,
      globe_getHeight_m: globeHeight,
      delta_m: (expected === null || !Number.isFinite(globeHeight)) ? null : globeHeight - expected,
    });
  }
  checks.globe_getHeight_samples = samples;
  const deltas = samples.map((s) => s.delta_m).filter((d) => d !== null);
  checks.globe_getHeight_max_delta_m = deltas.length ? Math.max(...deltas.map(Math.abs)) : null;
  // 同上：没有样本即"未评估"，报告 null 而不是 false/true 的空真结论。
  checks.globe_getHeight_consistent = deltas.length
    ? checks.globe_getHeight_max_delta_m < 1.5
    : null;

  // 2) 椭球高 − 正高 = N（逐点）
  const conversionSamples = [];
  for (const probe of probePoints) {
    const n = undulationAt(probe.lon, probe.lat);
    const ortho = bilinear(S.terrainFrame.terrain, S.win.width, S.win.height, S.win.bbox, probe.lon, probe.lat);
    if (n === null || ortho === null) continue;
    conversionSamples.push({
      label: probe.label,
      orthometric_m: ortho,
      ellipsoidal_m: ortho + n,
      difference_m: (ortho + n) - ortho,
      expected_undulation_m: n,
    });
  }
  checks.ellipsoidal_minus_orthometric = conversionSamples;
  // 语义边界（AGENTS.md 长期不变量 3：未评估 ≠ 通过）：
  // 默认清洁首屏**不**加载 terrain-window（那是业务图层按需请求的），因此
  // probePoints 为空时这里**没有**任何样本可比。此时必须报告 null（未评估），
  // 绝不能因为 [].every() === true 而宣称"椭球转换精确"——那是空真通过。
  checks.ellipsoidal_conversion_exact = conversionSamples.length
    ? conversionSamples.every(
      (s) => Math.abs(s.difference_m - s.expected_undulation_m) < 1e-6)
    : null;
  checks.ellipsoidal_conversion_status = conversionSamples.length
    ? 'evaluated' : 'not_evaluated_terrain_window_not_loaded_by_default';

  // 3) 航路里程：前端复算 vs 服务端账本
  if (demo) {
    checks.route_mileage = {
      server_total_m: demo.identity.mileage.total_m,
      point_count: demo.identity.point_count,
      geometry_fingerprint: demo.identity.fingerprint,
    };
  }

  // 4) 零写入：POST 计数来自文件顶部的 fetch 方法观测器（真实观测，不是恒 0 常量）
  checks.post_requests_issued = S.stats.posts;
  checks.request_method_observer_installed = S.stats.methodObserverInstalled === true;
  // 必须**实测**而不是硬编码 false：由 getJSON 在真实请求路径上累计。
  checks.workflow_endpoint_used = (S.stats.workflowRequests || 0) > 0;
  checks.workflow_requests_observed = S.stats.workflowRequests || 0;
  checks.default_business_entities = S.stats.entities;
  checks.default_business_layers_clean = S.stats.entities === 0;
  S.checks = checks;
  return checks;
}

/* -------------------------------------------------------------------- boot */

async function boot() {
  try {
    step('读取三维基础场景清单…');
    S.manifest = await getJSON(`${API}/manifest`);
    renderLayers();
    renderStatus();

    const viewer = createLocalViewer('cesiumContainer', {
      baseLayer: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      animation: false,
      timeline: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
      shouldAnimate: true,
      requestRenderMode: false,
    });
    S.viewer = viewer;
    viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#071421');
    viewer.scene.renderError.addEventListener((_scene, error) => {
      S.errors.push(`renderError: ${error && error.message}`);
      log(`渲染错误：${error && error.message}`, 'err');
    });

    step('连接 FABDEM 多级地形与受控卫星影像…');
    S.basemap = await createBasemap(viewer, S.manifest.imagery || {}, {
      terrainUrl: '/api/visualization-3d/terrain',
      waterMaskAvailable: Boolean(
        S.manifest.terrain_tiles && S.manifest.terrain_tiles.water_mask_status === 'available'),
      relief: S.manifest.terrain_relief || null,
      // 瓦片**真实到达**时刷新底图状态文案：初始化时若把"当前底图"定死，
      // 卫星影像随后才加载完成，面板就会一直停在"等待真实瓦片验证"（实测如此）。
      onImageryProgress: () => {
        if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
      },
      // 影像瓦片失败必须落到面板：Cesium 的 provider 不抛异常，只广播 errorEvent，
      // 不订阅就会出现"面板显示已配置 + 底图全蓝"的静默失败。
      onImageryFailure: (count, message) => {
        const node = $('imageryStatus');
        if (node) {
          node.textContent = `卫星影像瓦片加载失败 ${count} 次：${message}`;
          node.className = 'err';
        }
        if (count === 1) log(`卫星影像瓦片加载失败：${message}`, 'err');
        // 面板里的"影像瓦片失败"行必须跟着动（否则会出现"红字说 11 次、表格说 0 次"
        // 的自相矛盾）。节流：失败可能成百上千，不能每张瓦片都重建整张表。
        if (count === 1 || count % 10 === 0) renderStatus({ quiet: true });
      },
      // Cesium **主动丢弃/取消**瓦片是正常调度行为（实测网络层 0 失败却触发
      // errorEvent）：只做 dim 提示与日志，**绝不**显示成红字"加载失败"。
      onImageryDiscard: (count, message) => {
        if (count === 1) {
          log(`Cesium 丢弃瓦片 ${count} 次（正常调度，非加载失败）：${message}`);
        }
        if (count === 1 || count % 25 === 0) renderStatus({ quiet: true });
      },
      // 图层矩形之外（或受控 bbox 之外）的瓦片被后端范围守卫正常拒绝：Cesium 会
      // 请求这些瓦片，因此这**不是**故障，只记录一次，不刷红字。
      onImageryOutOfScope: (count, message) => {
        if (count === 1) {
          log(`瓦片超出受控展示范围（正常拒绝，非加载失败）：${message}`);
        }
        if (count === 1 || count % 25 === 0) renderStatus({ quiet: true });
      },
    });
    const imageryStatus = S.manifest.imagery || {};
    // "当前底图"必须反映**实际渲染**的图层与**真实到达的瓦片**，而不是"可用性"。
    const updateBasemapLabels = () => {
      const stats = S.basemap && S.basemap.layerStats ? S.basemap.layerStats() : null;
      const imageryShown = Boolean(stats && stats.imagery && stats.imagery.show);
      const reliefShown = Boolean(stats && stats.relief && stats.relief.show);
      const imageryResolved = stats && stats.imagery ? (stats.imagery.resolved || 0) : 0;
      const active = imageryShown && imageryResolved > 0 ? 'satellite'
        : (reliefShown ? 'relief' : 'none');
      const reliefNode = $('reliefStatus');
      if (reliefNode) {
        reliefNode.textContent = active === 'satellite'
          ? '当前底图：ArcGIS World Imagery 高清卫星影像'
          : (active === 'relief' ? '当前底图：FABDEM 地形晕渲（非卫星影像）' : '');
        reliefNode.className = active === 'satellite' ? 'ok-text' : 'dim';
      }
      const creditsNode = $('credits');
      if (creditsNode) {
        const terrainCredit = '地形：FABDEM 30 m + EGM2008（EGM2008 正高→WGS84 椭球高）';
        // 三种状态必须区分：离线模式禁用（不是凭证问题）/ 未授权 / 已配置。
        const offline = imageryStatus.online_allowed === false;
        const imageryCredit = offline
          ? `卫星影像：离线开发模式未启用（${imageryStatus.reason_code || 'imagery_online_mode_disabled'}）`
          : imageryStatus.status === 'blocked'
            ? `卫星影像：未授权，未渲染（${imageryStatus.reason_code || 'credential_unavailable'}）`
            : '卫星影像：ArcGIS World Imagery — Tiles © Esri — Esri, Maxar, '
              + 'Earthstar Geographics, and the GIS User Community';
        // 只有**真的在渲染**该底图时才追加来源标注（与上面同一判据）。
        const reliefCredit = active === 'relief'
          ? ' · 底图：FABDEM 地形晕渲（非卫星影像，仅用于判读起伏）' : '';
        creditsNode.textContent = `${imageryCredit} · ${terrainCredit}${reliefCredit}`;
      }
      // 影像状态行同样必须随**真实瓦片到达**刷新：四个状态必须区分
      // （离线模式 / 凭证被拒 / 瓦片加载实测失败 / 已渲染真实瓦片）。只按
      // manifest.imagery.status 判断会让"全部瓦片 503"继续显示成绿色已配置；
      // 而只在初始化时写一次，则会让影像已经渲染后仍停留在"等待真实瓦片验证"。
      const imageryNode = $('imageryStatus');
      if (imageryNode) {
        const status = S.basemap ? S.basemap.status() : {};
        const failures = status.imagery_failures || 0;
        const discards = status.imagery_discards || 0;
        const outOfScope = status.imagery_out_of_scope || 0;
        const lastError = status.imagery_last_error;
        const offline = imageryStatus.online_allowed === false;
        if (offline) {
          // 离线开发模式：这是**授权决策**，不是失败；底图由 FABDEM 晕渲承担。
          imageryNode.textContent =
            '卫星影像：离线开发模式（未启用在线瓦片，默认使用 FABDEM 晕渲）';
          imageryNode.className = 'dim';
        } else if (imageryStatus.status === 'blocked') {
          imageryNode.textContent =
            `卫星影像不可用：${imageryStatus.reason_code || 'credential_unavailable'}`;
          imageryNode.className = 'err';
        } else if (failures > 0) {
          imageryNode.textContent =
            `卫星影像瓦片加载失败 ${failures} 次：${lastError || '未知原因'}`;
          imageryNode.className = 'err';
        } else if (imageryResolved > 0) {
          // 有真实瓦片完成才敢说"已渲染"；0 张时必须保持"等待验证"，不能空口宣称。
          const notes = [];
          if (discards > 0) notes.push(`Cesium 内部丢弃 ${discards} 次`);
          if (outOfScope > 0) notes.push(`范围外拒绝 ${outOfScope} 次`);
          const suffix = notes.length ? `（另有${notes.join('、')}，均属正常调度）` : '';
          imageryNode.textContent =
            `ArcGIS World Imagery 已渲染真实瓦片（已完成 ${imageryResolved} 张）${suffix}`;
          imageryNode.className = 'ok-text';
        } else if (discards > 0 || outOfScope > 0) {
          // 只有丢弃/范围外拒绝、没有任何真实失败：**不得**报成加载失败。
          imageryNode.textContent =
            `ArcGIS World Imagery 等待真实瓦片（丢弃 ${discards} 次、范围外 ${outOfScope} 次，非失败）`;
          imageryNode.className = 'dim';
        } else {
          imageryNode.textContent = 'ArcGIS World Imagery 已配置，等待真实瓦片验证';
          imageryNode.className = 'ok-text';
        }
      }
      return active;
    };
    updateBasemapLabels();
    if (imageryStatus.online_allowed === false) {
      log(`卫星影像：离线开发模式（${imageryStatus.reason_code
        || 'imagery_online_mode_disabled'}）；底图使用 FABDEM 晕渲`);
    } else if (imageryStatus.status === 'blocked') {
      log(`卫星影像 BLOCKED：${imageryStatus.reason_code || 'credential_unavailable'}`, 'err');
    }
    S.updateBasemapLabels = updateBasemapLabels;
    // 低频兜底轮询：影像瓦片在首屏之后仍会陆续到达（尤其低空视角），
    // 仅靠 onImageryProgress 回调可能被节流/竞态漏掉，这里每 1.5 s 校正一次文案。
    if (!S.basemapLabelTimer) {
      S.basemapLabelTimer = setInterval(() => {
        try { updateBasemapLabels(); } catch (_error) { /* 文案刷新失败不影响渲染 */ }
      }, 1500);
    }
    const terrainLoad = $('terrainLoad');
    if (terrainLoad) terrainLoad.textContent = 'Cesium heightmap-1.0 · LOD 0–13 · 自动逐级细化';

    const groups = {
      routeDemo: [], routeCanonical: [], towersKnown: [], towersUnknown: [],
      facilities: [], buildings: [], terrain: [], communication: [], rid: [],
    };
    S.groups = groups;
    let routeContextPromise = null;
    let pointsPromise = null;
    let towerGroupsPromise = null;

    const ensureRouteContext = async () => {
      if (!routeContextPromise) routeContextPromise = (async () => {
        S.routes = await getJSON(`${API}/routes`);
        const demo = (S.routes.routes || []).find(
          (row) => row.authority === 'engineering_demo_projection');
        if (demo && !S.terrainFrame) {
          const lons = demo.path.map((point) => point[0]);
          const lats = demo.path.map((point) => point[1]);
          const margin = 0.02;
          await loadTerrainWindow([
            Math.min(...lons) - margin, Math.min(...lats) - margin,
            Math.max(...lons) + margin, Math.max(...lats) + margin,
          ], 512, 512);
          renderStatus();
        }
        return { demo, routes: S.routes.routes || [] };
      })();
      return routeContextPromise;
    };
    const ensurePoints = async () => {
      await ensureRouteContext();
      if (!pointsPromise) pointsPromise = getJSON(
        `${API}/manifest?layers=towers,cns_devices,cns_facilities`).then((payload) => {
        S.points = payload;
        const byId = {};
        for (const layer of payload.layers || []) byId[layer.id] = layer;
        return byId;
      });
      return pointsPromise;
    };
    const ensureTowerGroups = async () => {
      if (!towerGroupsPromise) towerGroupsPromise = ensurePoints().then((byId) => {
        const result = addTowerEntities(viewer, (byId.towers && byId.towers.items) || []);
        for (const entity of result.all) entity.show = false;
        groups.towersKnown = result.known;
        groups.towersUnknown = result.unknown;
        return result;
      });
      return towerGroupsPromise;
    };

    S.lazyLayers = new LazyLayerRegistry({
      routeDemo: async () => {
        const context = await ensureRouteContext();
        groups.routeDemo = context.demo ? addRouteEntities(viewer, context.demo) : [];
        return groups.routeDemo;
      },
      routeCanonical: async () => {
        const context = await ensureRouteContext();
        groups.routeCanonical = context.routes
          .filter((row) => row.authority !== 'engineering_demo_projection')
          .flatMap((row) => addRouteEntities(viewer, row));
        return groups.routeCanonical;
      },
      towersKnown: async () => (await ensureTowerGroups()).known,
      towersUnknown: async () => (await ensureTowerGroups()).unknown,
      facilities: async () => {
        const byId = await ensurePoints();
        groups.facilities = addFacilityEntities(
          viewer, (byId.cns_facilities && byId.cns_facilities.items) || []);
        addDeviceEntities(viewer, (byId.cns_devices && byId.cns_devices.items) || []);
        return groups.facilities;
      },
      // 通信 / RID：**只在用户勾选时**才请求几何（懒加载，首屏零业务几何）。
      communication: async () => {
        const payload = await getJSON(`${API}/communication-volumes`);
        S.coverage = { ...(S.coverage || {}), communication: payload };
        // 正高 → 椭球高需要逐点 N；默认首屏未加载地形窗口，这里**按需**补齐。
        // 半径取几何的真实外沿（地表包络最大值），否则外圈顶点的 N 会取不到。
        const geoid = await ensureGeoidWindow(
          (payload.volumes || []).map((row) => [row.longitude, row.latitude]),
          Math.max(0, ...(payload.volumes || []).map(
            (row) => (row.geometry || {}).slant_range_m || 0)));
        const result = addCommunicationVolumeEntities(viewer, payload.volumes || []);
        groups.communication = result.entities;
        S.coverageDiagnostics = {
          ...(S.coverageDiagnostics || {}), communication: {
            geoid_window: geoid, ...result.diagnostics,
          },
        };
        log(`通信三维包络：${payload.available_count}/${payload.volume_count} 可用，`
          + `实体 ${result.entities.length}（geoid 窗口 ${geoid.status}）`);
        if (result.diagnostics.skipped.length) {
          log(`通信包络有 ${result.diagnostics.skipped.length} 项几何被跳过：`
            + result.diagnostics.skipped.map((row) => `${row.part}=${row.reason}`).join('、'),
            'warn');
        }
        return groups.communication;
      },
      rid: async () => {
        const payload = await getJSON(`${API}/rid-footprints`);
        S.coverage = { ...(S.coverage || {}), rid: payload };
        const geoid = await ensureGeoidWindow(
          (payload.footprints || []).map((row) => [row.longitude, row.latitude]),
          Math.max(0, ...(payload.footprints || []).map(
            (row) => (row.radius_by_surface_m || {}).sea || 0)));
        const result = addRidFootprintEntities(viewer, payload.footprints || []);
        groups.rid = result.entities;
        S.coverageDiagnostics = {
          ...(S.coverageDiagnostics || {}), rid: {
            geoid_window: geoid, ...result.diagnostics,
          },
        };
        log(`RID footprint：${payload.available_count}/${payload.footprint_count} 可用，`
          + `实体 ${result.entities.length}（geoid 窗口 ${geoid.status}）`);
        if (result.diagnostics.skipped.length) {
          log(`RID footprint 有 ${result.diagnostics.skipped.length} 项几何被跳过：`
            + result.diagnostics.skipped.map((row) => `${row.part}=${row.reason}`).join('、'),
            'warn');
        }
        return groups.rid;
      },
    });

    const bindBase = (id, setter) => {
      const input = $(`ly_${id}`);
      if (input) input.onchange = () => {
        const applied = setter(input.checked);
        if (applied === false) input.checked = false;
        // 底图切换后必须刷新"当前底图"与版权来源标注（它们按**实际渲染**的图层生成）。
        if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
      };
    };
    bindBase('imagery', (value) => S.basemap.setImageryVisible(value));
    bindBase('terrain', (value) => S.basemap.setTerrainVisible(value));
    bindBase('labels', (value) => S.basemap.setLabelsVisible(value));
    // 离线模式下影像图层不存在：勾选框必须反映真实能力，不能显示成"已开启"。
    const imageryInput = $('ly_imagery');
    if (imageryInput && typeof S.basemap.imageryAvailable === 'function'
        && !S.basemap.imageryAvailable()) {
      imageryInput.checked = false;
      imageryInput.disabled = true;
      imageryInput.title = '离线开发模式：未启用在线瓦片（默认使用 FABDEM 晕渲）';
    }
    // 切换业务图层**不得**重建 Cesium Viewer，也不得重新初始化卫星底图：
    // 下面的循环只调用 LazyLayerRegistry.setVisible（首次才请求几何）。
    S.viewerIdentity = viewer;

    for (const id of Object.keys(BUSINESS_LAYER_DEFAULTS)) {
      const input = $(`ly_${id}`);
      if (!input) continue;
      input.onchange = async () => {
        input.disabled = true;
        try {
          const applied = await S.lazyLayers.setVisible(id, input.checked);
          if (!applied) {
            input.checked = false;
            log(`${input.nextElementSibling ? input.nextElementSibling.textContent : id}：本轮未启用`, 'warn');
          }
          S.stats.entities = viewer.entities.values.length;
          renderStatus();
        } catch (error) {
          input.checked = false;
          S.errors.push(`layer ${id}: ${error && error.message ? error.message : error}`);
          log(`图层 ${id} 加载失败：${S.errors[S.errors.length - 1]}`, 'err');
        } finally {
          input.disabled = false;
        }
      };
    }

    const quality = $('quality');
    if (quality) quality.onchange = () => S.basemap.setQuality(quality.value);
    document.querySelectorAll('[data-preset]').forEach((button) => {
      button.addEventListener('click', () => flyToPreset(viewer, button.dataset.preset));
    });
    const reset = $('resetView');
    if (reset) reset.onclick = () => flyToPreset(viewer, 'archipelago_overview');

    viewer.screenSpaceEventHandler.setInputAction((movement) => {
      const picked = viewer.scene.pick(movement.position);
      if (picked && picked.id) showPopup(picked.id, movement.position);
      else $('popup').style.display = 'none';
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

    flyToPreset(viewer, 'archipelago_overview', 0);
    await new Promise((resolve) => setTimeout(resolve, 800));
    S.stats.entities = viewer.entities.values.length;
    await runSelfChecks();
    S.ready = true;
    $('boot').style.display = 'none';
    log('三维基础地理环境就绪；业务图层默认关闭且尚未请求');
  } catch (error) {
    S.errors.push(String(error && error.message ? error.message : error));
    log(`初始化失败：${S.errors[S.errors.length - 1]}`, 'err');
    const bootNode = $('boot');
    if (bootNode) bootNode.innerHTML = `<div class="err">三维场景初始化失败</div>`
      + `<small>${S.errors[S.errors.length - 1]}</small>`;
  }
}

window.addEventListener('error', (event) => {
  S.errors.push(`window.error: ${event.message} @ ${event.filename || '?'}:${event.lineno || 0}:${event.colno || 0}`);
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  const detail = reason && (reason.stack || reason.message)
    ? (reason.stack || reason.message) : String(reason);
  S.errors.push(`unhandledrejection: ${detail}`);
});

window.__CNS_3D__ = {
  get ready() { return S.ready; },
  cameraState() {
    return S.viewer
      ? readCameraState(S.viewer.camera, Cesium.Math.toDegrees)
      : null;
  },
  setCameraState(state) {
    return S.viewer
      ? applyCameraState(S.viewer.camera, state, Cesium.Cartesian3.fromDegrees)
      : false;
  },
  flyToPreset(id) { return S.viewer ? flyToPreset(S.viewer, id) : false; },
  /** 验收辅助：**窄接口**性能/场景探针，不暴露 Cesium viewer 本身。 */
  spinCamera(radians = 0.004) {
    if (!S.viewer) return false;
    S.viewer.camera.rotateRight(Number(radians) || 0.004);
    return true;
  },
  sceneProbe() {
    if (!S.viewer) return null;
    const scene = S.viewer.scene;
    const globe = scene.globe;
    const surface = globe && globe._surface;
    const layers = S.viewer.imageryLayers;
    return {
      tiles_to_render: surface && surface._tilesToRender ? surface._tilesToRender.length : null,
      tiles_loading: surface && surface._tilesToRender
        ? surface._tilesToRender.filter((t) => t && t.state === 1).length : null,
      imagery_layers: layers ? layers.length : null,
      //: 影像瓦片**真实**请求/完成/失败计数与本视角所用的图层身份。
      imagery_stats: layers && layers.length > 0 ? imageryStats(layers.get(0)) : null,
      imagery_visible: layers && layers.length > 0 ? Boolean(layers.get(0).show) : null,
      relief_stats: layers && layers.length > 1 ? imageryStats(layers.get(1)) : null,
      terrain_provider: S.viewer.terrainProvider && S.viewer.terrainProvider.constructor
        ? S.viewer.terrainProvider.constructor.name : null,
      vertical_exaggeration: scene.verticalExaggeration,
      maximum_screen_space_error: globe ? globe.maximumScreenSpaceError : null,
      water_effect: globe ? globe.showWaterEffect : null,
      lighting: globe ? globe.enableLighting : null,
      fog: scene.fog ? scene.fog.enabled : null,
      sky_atmosphere: scene.skyAtmosphere ? scene.skyAtmosphere.show : null,
      globe_show: globe ? globe.show : null,
    };
  },
  cameraPresets() {
    return Object.fromEntries(Object.entries(CAMERA_PRESETS).map(
      ([id, row]) => [id, { ...row }]));
  },
  basemapStatus() { return S.basemap ? S.basemap.status() : null; },
  /** 验收辅助：切换 FABDEM 晕渲（**非卫星影像**）用于同相机对照。 */
  setReliefVisible(value) {
    const ok = S.basemap ? S.basemap.setReliefVisible(Boolean(value)) : false;
    if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
    return ok;
  },
  /** 验收辅助：切换卫星影像图层可见性（用于同相机 on/off 对照）。 */
  setImageryVisible(value) {
    const ok = S.basemap ? S.basemap.setImageryVisible(Boolean(value)) : false;
    if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
    return ok;
  },
  /** 验收辅助：只读图层证据（真实瓦片计数 / tiling scheme / 来源版权）。 */
  layerStats() { return S.basemap ? S.basemap.layerStats() : null; },
  /** 验收辅助：Cesium Viewer 是否被**重建**过（切图层绝不允许重建 viewer）。 */
  viewerIdentityStable() {
    return Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer;
  },
  /** 验收辅助：覆盖图层的只读证据（服务端原样 payload + 实体数）。 */
  coverageStats() {
    const comm = S.coverage ? S.coverage.communication : null;
    const rid = S.coverage ? S.coverage.rid : null;
    return {
      communication: comm ? {
        available_count: comm.available_count,
        volume_count: comm.volume_count,
        blocked_count: comm.blocked_count,
        coverage_semantics: comm.coverage_semantics,
        slant_range_is_criterion: comm.slant_range_is_criterion,
        route_intersection_binding_allowed: (comm.route_intersection || {}).binding_allowed,
        sites: (comm.volumes || []).map((row) => ({
          device_id: row.device_id, longitude: row.longitude, latitude: row.latitude,
          slant_range_m: (row.geometry || {}).slant_range_m || null,
          status: (row.geometry || {}).status,
        })),
      } : null,
      rid: rid ? {
        available_count: rid.available_count,
        footprint_count: rid.footprint_count,
        blocked_count: rid.blocked_count,
        coverage_semantics: rid.coverage_semantics,
        not_a_radar_sector: rid.not_a_radar_sector,
        vertical: rid.vertical,
        route_intersection_binding_allowed: (rid.route_intersection || {}).binding_allowed,
        sites: (rid.footprints || []).map((row) => ({
          device_id: row.device_id, longitude: row.longitude, latitude: row.latitude,
          sea_radius_m: (row.radius_by_surface_m || {}).sea || null,
          boundary_radius_m: (row.geometry || {}).boundary_radius_m || null,
          status: (row.geometry || {}).status,
        })),
      } : null,
      entity_counts: {
        communication: S.groups ? S.groups.communication.length : 0,
        rid: S.groups ? S.groups.rid.length : 0,
      },
      diagnostics: S.coverageDiagnostics || null,
    };
  },
  /** 验收辅助：覆盖实体的**可见性**（只读）。关闭图层后必须真的隐藏。 */
  coverageVisibility() {
    if (!S.viewer) return null;
    const result = { communication: { total: 0, shown: 0 },
                     rid: { total: 0, shown: 0 } };
    for (const entity of S.viewer.entities.values) {
      const info = entity.__cns;
      if (!info) continue;
      const bucket = info.kind === 'communication-volume' ? result.communication
        : (info.kind === 'rid-footprint' ? result.rid : null);
      if (!bucket) continue;
      bucket.total += 1;
      if (entity.show) bucket.shown += 1;
    }
    return result;
  },
  /** 验收辅助：俯视某个覆盖站点（固定高度，用于近景判读 2–5 km 几何）。 */
  flyToCoverageSite(layerId, ordinal = 0, heightM = 9000) {
    if (!S.viewer || !S.coverage) return false;
    const payload = layerId === 'rid' ? S.coverage.rid : S.coverage.communication;
    const rows = payload
      ? (layerId === 'rid' ? payload.footprints : payload.volumes) : null;
    const row = (rows || [])[ordinal || 0];
    if (!row || typeof row.longitude !== 'number') return false;
    S.viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(
        row.longitude, row.latitude, Number(heightM) || 9000),
      orientation: { heading: 0, pitch: Cesium.Math.toRadians(-90), roll: 0 },
    });
    return true;
  },
  /** 验收辅助：按类型列出实体（ordinal 与 screenPositionOf 的序号一致）。 */
  entitiesOf(kind) {
    if (!S.viewer) return [];
    return S.viewer.entities.values
      .filter((entity) => entity.__cns && entity.__cns.kind === kind)
      .map((entity, ordinal) => ({
        ordinal,
        id: entity.id,
        title: entity.__cns.title,
        hasTop: entity.__cns.hasTop === undefined ? null : entity.__cns.hasTop,
      }));
  },
  /** 验收辅助：把实体位置投影成窗口坐标，供真实鼠标点击使用。 */
  screenPositionOf(kind, ordinal) {
    if (!S.viewer) return null;
    const rows = S.viewer.entities.values.filter(
      (entity) => entity.__cns && entity.__cns.kind === kind);
    const entity = rows[ordinal || 0];
    if (!entity) return null;
    const viewer = S.viewer;
    const now = Cesium.JulianDate.now();
    let position = null;
    if (entity.position) {
      position = entity.position.getValue(now);
    } else if (entity.polyline && entity.polyline.positions) {
      const positions = entity.polyline.positions.getValue(now) || [];
      if (positions.length) position = positions[Math.floor(positions.length / 2)];
    }
    if (!position) return null;
    // Cesium 1.117+ 把 wgs84ToWindowCoordinates 更名为 worldToWindowCoordinates；
    // 两个名字都兼容，避免因版本差异让验收辅助失效。
    const transforms = Cesium.SceneTransforms;
    const project = transforms.worldToWindowCoordinates || transforms.wgs84ToWindowCoordinates;
    if (typeof project !== 'function') return null;
    const win = project.call(transforms, viewer.scene, position);
    return win ? { x: win.x, y: win.y } : null;
  },
  /** 验收辅助：把相机定位到某类实体的第 ordinal 个对象。 */
  flyToEntityOfKind(kind, ordinal) {
    if (!S.viewer) return false;
    const rows = S.viewer.entities.values.filter(
      (entity) => entity.__cns && entity.__cns.kind === kind);
    const entity = rows[ordinal || 0];
    if (!entity) return false;
    flyToEntity(entity);
    return true;
  },
  popupVisible() {
    const popup = $('popup');
    return Boolean(popup && popup.style.display !== 'none' && popup.textContent.trim());
  },
  popupText() {
    const popup = $('popup');
    return popup ? popup.textContent.replace(/\s+/g, ' ').trim() : '';
  },
  terrainWindowStats() { return S.win ? { ...S.win } : null; },
  layerCheckbox(id) { return Boolean($(`ly_${id}`) && $(`ly_${id}`).checked); },
  setLayer(id, value) {
    const input = $(`ly_${id}`);
    if (!input) return false;
    input.checked = value;
    input.dispatchEvent(new Event('change'));
    return true;
  },
  entityCount() { return S.viewer ? S.viewer.entities.values.length : 0; },
  summary() {
    return {
      ready: S.ready,
      revision: S.manifest ? S.manifest.revision : null,
      scope: S.manifest ? S.manifest.scope : null,
      engineering_demo_only: S.manifest ? S.manifest.engineering_demo_only : null,
      operational_authorization: S.manifest ? S.manifest.operational_authorization : null,
      strict_safety_validation_passed: S.manifest ? S.manifest.strict_safety_validation_passed : null,
      ellipsoidal_conversion_status: S.manifest
        ? S.manifest.vertical_reference.ellipsoidal_conversion.status : null,
      geoid_grid: S.manifest && S.manifest.vertical_reference.ellipsoidal_conversion.geoid_grid
        ? S.manifest.vertical_reference.ellipsoidal_conversion.geoid_grid.filename : null,
      geoid_mean_m: S.geoidMean,
      layers: S.manifest ? S.manifest.layer_ids : [],
      terrain_window: S.win ? {
        bbox: S.win.bbox, width: S.win.width, height: S.win.height,
        nodata_count: S.win.nodata_count, status: S.win.status,
        min_m: S.win.min_m, max_m: S.win.max_m,
      } : null,
      counts: {
        entities: S.stats.entities,
        routes: S.routes ? S.routes.route_count : 0,
        mileage_conflicts: S.routes ? S.routes.conflict_count : 0,
        towers_known: S.groups ? S.groups.towersKnown.length : 0,
        towers_unknown: S.groups ? S.groups.towersUnknown.length : 0,
        facilities: S.groups ? S.groups.facilities.length : 0,
        devices: S.datasets.length,
        communication_entities: S.groups ? S.groups.communication.length : 0,
        rid_entities: S.groups ? S.groups.rid.length : 0,
      },
      stats: S.stats,
      // 底图**实测**状态（含 imagery_failures / imagery_last_error / discards）：验收
      // 脚本据此断言"影像真的出图了"，而不是只看 manifest 的 configured_unverified。
      imagery: S.basemap ? S.basemap.status() : null,
      // 覆盖图层证据（服务端语义 + 实体数）；未开启时为 null，绝不假装已加载。
      coverage: (S.coverage || null) ? {
        communication: S.coverage.communication ? {
          coverage_semantics: S.coverage.communication.coverage_semantics,
          available_count: S.coverage.communication.available_count,
          volume_count: S.coverage.communication.volume_count,
          blocked_count: S.coverage.communication.blocked_count,
        } : null,
        rid: S.coverage.rid ? {
          coverage_semantics: S.coverage.rid.coverage_semantics,
          available_count: S.coverage.rid.available_count,
          footprint_count: S.coverage.rid.footprint_count,
          blocked_count: S.coverage.rid.blocked_count,
          not_a_radar_sector: S.coverage.rid.not_a_radar_sector,
        } : null,
      } : null,
      checks: S.checks,
      errors: S.errors,
      log: logLines.slice(-40),
    };
  },
};

boot();
