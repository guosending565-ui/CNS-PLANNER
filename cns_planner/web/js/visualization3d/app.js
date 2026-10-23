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

import { addFlightProtectionResearch } from './flight_protection_research.js';
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
import {
  RADAR_PREVIEW_DISCLAIMER,
  addRadarPreviewEntities,
  addRadarVolumeEntities,
} from './radar_volume.js';
import {
  PROTECTION_PREVIEW_DISCLAIMER,
  addProtectionPreviewEntities,
  addProtectionVolumeEntities,
  protectionBoundaryPoints,
  removeProtectionEntities,
} from './protection_volume.js';
import {
  CNS_SERVICE_STYLE,
  CNS_SITE_STATE_STYLE,
  addCnsSiteEntities,
} from './cns_sites.js';
import {
  ASSUMPTION_SITES_DISCLAIMER,
  addAssumptionSiteEntities,
} from './assumption_sites.js';
import {
  FIXED_CORRIDOR_DISCLAIMER,
  FIXED_CORRIDOR_PREVIEW_DISCLAIMER,
  addFixedCorridorBandEntities,
  corridorRingsFor,
} from './fixed_corridor_band.js';
import {
  LANDING_PAD_ICON_URL, addLandingPadEntities, landingPadRouteEndpoints,
} from './landing_pads.js';
import {
  INTRUSION_MODE_BANNER,
  PLAYBACK_SPEEDS,
  PlaybackController,
  SCOPE_LABELS,
  addIntrusionEntities,
  applyIntrusionState,
  buildPlaybackModel,
  createCameraFollow,
  describeState,
  removeIntrusionEntities,
  setIntrusionVisible,
} from './intrusion_playback.js';

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
  // 模拟轨迹回放：实例键、控制器、实体句柄、相机跟随与图层意图。
  // 这里**只有**一个实体句柄与一个控制器，清除时整体置空 ⇒ 不可能留下残留实体。
  intrusion: {
    handle: null,
    controller: null,
    follow: null,
    model: null,
    volumeBounds: null,
    deps: null,
    targets: [],
    selectedKey: '',
    revision: 0,
    // 只在**成功**完成一次后端判定后自增；验收脚本据此等待"新结果已就位"，
    // 而不是被上一次仍然 configured 的模型骗过。
    evaluationRevision: 0,
    layerWanted: false,
    tickInstalled: false,
  },
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

/** 地形高度（椭球高）：NoData 仅在渲染时取 0，并在面板披露。
 *
 * A7-BUG-01 真实根因：本函数原先直接解引用 ``S.terrainFrame`` / ``S.win``，而
 * ``terrain-window`` 是**按需**加载的（首屏默认不取）。塔图层 / 演示航路 / 正式航路
 * 之前在未加载地形窗口时就调用本函数，于是抛出
 * ``Cannot read properties of null (reading 'terrain')``，整层实体创建中断，
 * 用户看到的是"图层打开了但铁塔/航线都没出来 + 一条错误日志"。
 *
 * 这里只做**最小空值保护**：没有地形帧时如实返回 ``null``（几何调用方据此标注
 * "高度不完整"），绝不当 0 静默渲染。真正的修复是调用方先按几何范围补齐地形窗口
 * （见 :func:`ensureTerrainForPoints`），因此正常路径仍然拿到真实 FABDEM 高程。
 */
function terrainHeightAt(lon, lat) {
  if (!S.terrainFrame || !S.terrainFrame.terrain || !S.win) return null;
  const H = bilinear(S.terrainFrame.terrain, S.win.width, S.win.height, S.win.bbox, lon, lat);
  if (H === null) return 0;
  const h = orthometricToEllipsoidal(H, lon, lat);
  return h === null ? 0 : h;
}

/**
 * A7-BUG-01 / A7-OBS-03：为一批几何点**按需**补齐地形窗口。
 *
 * 复用既有唯一实现 ``loadTerrainWindow``（与 ``ensureGeoidWindow`` 同一条通道），
 * 不新建第二套地形采样。窗口已覆盖所需范围时不重复请求。
 *
 * 返回 ``true`` 表示调用之后 ``S.terrainFrame`` 可用。
 */
async function ensureTerrainForPoints(points, label) {
  const usable = (points || []).filter(
    (row) => Array.isArray(row) && Number.isFinite(row[0]) && Number.isFinite(row[1]));
  if (!usable.length) return Boolean(S.terrainFrame);
  const lons = usable.map((row) => row[0]);
  const lats = usable.map((row) => row[1]);
  const margin = 0.02;
  const bbox = [
    Math.min(...lons) - margin, Math.min(...lats) - margin,
    Math.max(...lons) + margin, Math.max(...lats) + margin,
  ];
  const current = S.win && S.win.bbox;
  const covered = current && current[0] <= bbox[0] && current[1] <= bbox[1]
    && current[2] >= bbox[2] && current[3] >= bbox[3];
  if (covered && S.terrainFrame) return true;
  await loadTerrainWindow(bbox, 512, 512);
  renderStatus();
  if (!S.terrainFrame || !S.terrainFrame.terrain) {
    log(`${label}：地形窗口不可用，相关几何高度标注为"不完整"（不当作 0）`, 'warn');
    return false;
  }
  return true;
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

/**
 * 铁塔点位实体（A7-BUG-01）。
 *
 * 身份分层**只**用服务端既有字段，绝不新造第二套塔高算法：
 *
 *  * ``tower_top_orthometric_m`` 为有限数值 ⇒ ``tower_top_status =
 *    resolved_unconfirmed``（已严格解析、**未**经实测确认），进 ``known`` 组；
 *  * 否则 ⇒ ``unknown`` 组。这些塔的塔顶正高**未解析**（权威容器
 *    ``tower_obstacle_profiles`` 的 ``unresolved_count``），既有工程估算塔高
 *    （``tower_structure_height_m`` / 共址工程估算原点）只能作明确标注的示意，
 *    本层因此只画地面符号并如实披露缺证原因，绝不把估算当成已确认塔顶。
 *
 * ``options.terrainReady === false`` 表示地形窗口不可用：此时不做任何"高度=0"的
 * 静默渲染，而是在弹窗中明确标注高度不完整（unknown ≠ 0）。
 */
/**
 * R7-D3B：统一铁塔图标（青绿色通信塔 PNG，二维/三维共用同一份资源）。
 *
 * 图标由 ``D:\浏览器下载内容\青绿色通信塔图标.png`` 去透明边、按高度等比缩放生成；
 * ``unknown`` 变体只做低饱和灰蓝化，**不改变**任何塔高/塔顶语义。
 */
const TOWER_ICON_URL = '/assets/symbols/tower_qinglv_128.png?v=r7d3b';
const TOWER_ICON_UNKNOWN_URL = '/assets/symbols/tower_qinglv_unknown_128.png?v=r7d3b';

function addTowerEntities(viewer, items, options = {}) {
  const terrainReady = options.terrainReady !== false;
  const known = []; const unknown = [];
  for (const tower of items) {
    const top = tower.tower_top_orthometric_m;
    const hasTop = typeof top === 'number';
    const hasEstimate = !hasTop
      && (typeof tower.tower_structure_height_m === 'number'
        || typeof tower.planning_estimate_origin_egm2008_m === 'number');
    const n = undulationAt(tower.longitude, tower.latitude);
    const ground = terrainHeightAt(tower.longitude, tower.latitude);
    const topZ = hasTop ? orthometricToEllipsoidal(top, tower.longitude, tower.latitude) : null;
    const heightIncomplete = !terrainReady || ground === null;
    const entity = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(tower.longitude, tower.latitude,
        ground === null ? 0 : ground),
      // 用 billboard 而不是 point：图标底部锚定在塔址地面，距离越近图标越大；
      // unknown 塔仍显示灰蓝图标，绝不因“没有塔顶”而隐藏或伪装成已确认。
      billboard: {
        image: hasTop ? TOWER_ICON_URL : TOWER_ICON_UNKNOWN_URL,
        width: 30,
        height: 34,
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
        scaleByDistance: new Cesium.NearFarScalar(400.0, 0.9, 30000.0, 0.45),
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0.0, 60000.0),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: tower.name || tower.tower_id,
    });
    entity.__cns = {
      kind: 'tower', hasTop,
      title: `铁塔 ${tower.name || ''} (${tower.tower_id})`
        + (hasTop ? '' : '（塔顶未解析）'),
      rows: [
        ['站点类型', tower.site_type || '—'],
        ['经度 / 纬度', `${tower.longitude.toFixed(6)} / ${tower.latitude.toFixed(6)}`],
        ['地形正高', tower.terrain_elevation_m === null || tower.terrain_elevation_m === undefined
          ? '未解析' : `${tower.terrain_elevation_m} m`],
        ['塔身高度', tower.tower_structure_height_m === null || tower.tower_structure_height_m === undefined
          ? '未解析' : `${tower.tower_structure_height_m} m（源文件字段，工程估算；非已确认塔顶）`],
        ['塔顶正高', hasTop ? `${top} m` : 'unknown（未解析，绝不当 0）'],
        ['塔顶状态', tower.tower_top_status || 'unknown'],
        ['塔顶权威性', hasTop
          ? 'resolved_unconfirmed：已严格解析塔顶正高，但**未经实测确认**'
          : (hasEstimate
            ? 'unresolved：仅有工程估算塔高证据，不得据以认定塔顶正高或 LOS 通过'
            : 'unresolved：无塔顶证据')],
        ['geoid N', n === null ? '未解析' : `${n.toFixed(4)} m`],
        ['塔顶椭球高', topZ === null ? 'unknown' : `${topZ.toFixed(3)} m`],
        ['绘制高度完整', heightIncomplete
          ? '不完整：地形窗口/geoid N 未解析（未当 0 渲染）' : '完整'],
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
      + ' 本视图不得作为运行依据。'
      + ' L1：建筑 4tCa7 高度证据为 ML 预测，采用工程条件接受，不构成严格安全验证通过。'
      + ' L2：当前航路使用 EGM2008 正高 100 m 恒定巡航高度假设；真实离场爬升和进场下降'
      + '程序尚未配置及验证，当前建筑净空结果不代表起降过渡阶段安全。';
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
    // 背景影像由上方「底图模式」开关统一驱动（默认离线 ⇒ 初始不勾选）。
    // 这里**不**用 BASE_LAYER_DEFAULTS.imagery 当初始值：那会让勾选框在默认离线
    // 模式下显示成"已开启"，与真实渲染的底图（FABDEM 晕渲）自相矛盾。
    ['imagery', onlineAllowed ? '高清卫星影像' : '高清卫星影像（离线模式禁用）',
      false, 'base'],
    ['terrain', 'FABDEM 多级三维地形', BASE_LAYER_DEFAULTS.terrain, 'base'],
    ['labels', '地理注记', BASE_LAYER_DEFAULTS.labels, 'base'],
    ['routeDemo', 'R0005 工程演示航路', BUSINESS_LAYER_DEFAULTS.routeDemo, 'business'],
    ['routeCanonical', '正式容器航路（stale）', BUSINESS_LAYER_DEFAULTS.routeCanonical, 'business'],
    ['towersKnown', '铁塔（塔顶高度已解析、尚未权威确认，77 座级 / resolved_unconfirmed）', BUSINESS_LAYER_DEFAULTS.towersKnown, 'business'],
    ['towersUnknown', '铁塔（塔顶未解析，296 座，仅工程估算塔高，不得据以认定塔顶/ LOS）', BUSINESS_LAYER_DEFAULTS.towersUnknown, 'business'],
    ['facilities', 'CNS 站址', BUSINESS_LAYER_DEFAULTS.facilities, 'business'],
    ['buildings', '近景建筑', BUSINESS_LAYER_DEFAULTS.buildings, 'business'],
    ['grid', '规划网格', BUSINESS_LAYER_DEFAULTS.grid, 'reserved'],
    ['communication', '通信包络（三维工程几何）', BUSINESS_LAYER_DEFAULTS.communication,
      'business'],
    ['rid', 'RID 陆海非正圆覆盖', BUSINESS_LAYER_DEFAULTS.rid, 'business'],
    ['radar', 'Radar 方向性覆盖（工程几何楔体）', BUSINESS_LAYER_DEFAULTS.radar,
      'business'],
    ['protection', 'P17 航路保护区（历史/缺证分明）', BUSINESS_LAYER_DEFAULTS.protection,
      'business'],
    // Round37-T3D-3B：模拟轨迹回放已实现并接入（不再是 reserved 合同占位）。
    ['intrusion', '模拟轨迹目标（SIMULATED 工程示意）', BUSINESS_LAYER_DEFAULTS.intrusion,
      'business'],
    // Round38-T3D4：已有二维能力的展示接入（默认关闭，按需请求）。
    ['cnsSites', 'CNS 站址与设备（既有/规划选中/候选/blocked）',
      BUSINESS_LAYER_DEFAULTS.cnsSites, 'business'],
    // R7-D16：起降点（项目 + 参考）PNG billboard；与二维 marker 同一张图，默认关闭。
    ['landingPads', '起降点（项目 + 参考；PNG billboard，只读）',
      BUSINESS_LAYER_DEFAULTS.landingPads, 'business'],
    // R7-B：P16-V2 选址的**工程假设**独立图层（默认关闭，洋红菱形，非正式设备）。
    ['assumptionSites', 'P16-V2 假设站址（只读，非正式设备）',
      BUSINESS_LAYER_DEFAULTS.assumptionSites, 'business'],
    ['flightProtectionResearch', '飞行保护区（工程假设）', false, 'business'],
    ['fixedCorridor', '固定 1000 m CNS 保障目标带',
      BUSINESS_LAYER_DEFAULTS.fixedCorridor, 'business'],
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
  popup.style.maxHeight = `${window.innerHeight - 40}px`;
  popup.style.overflowY = 'auto';
  const top = Math.max(12, Math.min(position.y + 14, window.innerHeight - popup.offsetHeight - 20));
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

    // Browser image errors may hide the proxy JSON body. Read only local budget metadata;
    // this endpoint never fetches a tile or consumes upstream request allowance.
    const refineImageryFailure = async (reason) => {
      const needsReason = ['imagery_tile_request_failed', 'imagery_tile_timeout'].includes(reason);
      if (reason && !needsReason) return reason;
      try {
        const config = await getJSON(`${API}/imagery/config`);
        if (config.budget && S.manifest && S.manifest.imagery) {
          S.manifest.imagery.budget = config.budget;
          renderStatus({ quiet: true });
        }
        if (needsReason && config.online_allowed && config.budget && !config.budget.degraded_reason
            && config.budget.budget_remaining === 0) return 'imagery_budget_exhausted';
      } catch { /* Keep the original failure if local diagnostics are unavailable. */ }
      return reason;
    };
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
      onModeFallback: async (reason) => {
        const serial = S.basemap.status().mode_serial;
        reason = await refineImageryFailure(reason);
        if (S.basemap.status().mode_serial !== serial) return;
        const offline = $('bmOffline');
        const online = $('bmOnline');
        if (offline) offline.checked = true;
        if (online) online.checked = false;
        const node = $('basemapModeStatus');
        if (node) {
          node.textContent = reason === 'imagery_budget_exhausted'
            ? '影像瓦片预算已耗尽，已回退离线；请停止在线演示并核对会话及每日剩余额度'
            : '在线影像加载失败，已回退离线开发模式';
          node.className = 'err';
        }
        if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
      },
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
      // 本地离线影像"没有这一块"是**数据边界**的正常状态（后端 404）：只记录一次，
      // 既不刷红字也不回退。它与在线越界（503）是不同的事，必须分开表达。
      onOfflineTileMissing: (count, message) => {
        if (count === 1) {
          log(`本地离线影像数据范围外瓦片（正常，非加载失败）：${message}`);
        }
        if (count === 1 || count % 25 === 0) renderStatus({ quiet: true });
      },
    });
    const imageryStatus = S.manifest.imagery || {};
    const imagerySource = (imageryStatus.sources || []).find(
      source => source.id === imageryStatus.default_source);
    const providerName = imagerySource && (imagerySource.label || imagerySource.id)
      || '在线卫星影像';
    // R7-D3C：在线源既可能是天地图高清，也可能是 ArcGIS 备用源；
    // 文案必须按**实际配置的源**生成。天地图没有官方服务端 Key 时，它的在线模式
    // 明确显示“待配置”，绝不把浏览器端 tk 或伪装请求当成已配置。
    const onlineIsHd = Boolean(imagerySource && imagerySource.id === 'tianditu_img_w');
    const hdPending = imageryStatus.hd_pending_configuration === true;
    const arcgisConfigured = Boolean(imageryStatus.credential_present);
    const hdLabel = hdPending
      ? '天地图高清影像（待配置：缺少官方服务端 Key）'
      : '天地图高清影像（z18，约 0.52 m/px）';
    const onlineLabel = onlineIsHd ? hdLabel
      : (hdPending && !arcgisConfigured ? hdLabel : 'ArcGIS 在线卫星影像');
    const onlineCredit = (imagerySource && imagerySource.attribution)
      || (onlineIsHd || (hdPending && !arcgisConfigured)
        ? '天地图 © 国家基础地理信息中心' : 'Tiles © Esri');
    const onlineCoverageNote = onlineIsHd ? 'R0005 及渲染缓冲' : '舟山展示范围';
    const missingSettings = ((onlineIsHd || (hdPending && !arcgisConfigured)
      ? imageryStatus.hd_missing_configuration
      : imageryStatus.missing_configuration) || []).join('、');
    // "当前底图"必须反映**实际渲染**的图层与**真实到达的瓦片**，而不是"可用性"。
    const updateBasemapLabels = () => {
      const stats = S.basemap && S.basemap.layerStats ? S.basemap.layerStats() : null;
      const imageryShown = Boolean(stats && stats.imagery && stats.imagery.show);
      const reliefShown = Boolean(stats && stats.relief && stats.relief.show);
      const offlineShown = Boolean(stats && stats.offline_imagery
        && stats.offline_imagery.show);
      const imageryResolved = stats && stats.imagery ? (stats.imagery.resolved || 0) : 0;
      const offlineResolved = stats && stats.offline_imagery
        ? (stats.offline_imagery.resolved || 0) : 0;
      // 三种底图的判据必须**分开**：离线影像有真实瓦片才叫已渲染，否则会把
      // "图层 show=true 但一张瓦片都没到"误报成"当前底图：卫星影像"。
      const active = imageryShown && imageryResolved > 0 ? 'online_satellite'
        : (offlineShown && offlineResolved > 0 ? 'offline_satellite'
          : (offlineShown ? 'offline_satellite_pending'
            : (reliefShown ? 'relief' : 'none')));
      const reliefNode = $('reliefStatus');
      if (reliefNode) {
        const offlineSourceRow = (imageryStatus.sources || []).find(
          source => source.id === 'offline_zhoushan_imagery');
        const offlineLabel = (offlineSourceRow && offlineSourceRow.label)
          || '舟山离线卫星影像（Sentinel-2 L2A）';
        const offlineAttribution = (offlineSourceRow && offlineSourceRow.attribution)
          || 'Contains modified Copernicus Sentinel data';
        reliefNode.textContent = active === 'online_satellite'
          ? `当前底图：${onlineLabel}（${onlineCoverageNote}）`
          : active === 'offline_satellite'
            ? `当前底图：${offlineLabel} · ${offlineAttribution}`
            : active === 'offline_satellite_pending'
              ? `当前底图：${offlineLabel}（本地瓦片加载中）`
              : (active === 'relief' ? '当前底图：FABDEM 地形晕渲（非卫星影像）' : '');
        reliefNode.className = (active === 'online_satellite' || active === 'offline_satellite')
          ? 'ok-text' : 'dim';
      }
      const creditsNode = $('credits');
      if (creditsNode) {
        const terrainCredit = '地形：FABDEM 30 m + EGM2008（EGM2008 正高→WGS84 椭球高）';
        const offlineSourceRow = (imageryStatus.sources || []).find(
          source => source.id === 'offline_zhoushan_imagery');
        // 署名必须逐条对应**真实**来源，绝不能把 Sentinel-2 标成 ArcGIS World Imagery。
        const offlineCredit = offlineSourceRow && offlineSourceRow.status === 'available'
          ? `离线影像：${offlineSourceRow.attribution || 'Contains modified Copernicus Sentinel data'}`
            + `（Sentinel-2 L2A，${offlineSourceRow.native_gsd_m || 10} m，本地瓦片，零上游请求）`
          : `离线影像：不可用（${(offlineSourceRow && offlineSourceRow.reason_code) || 'offline_imagery_unavailable'}）`;
        // 三种状态必须区分：离线模式禁用（不是凭证问题）/ 未授权 / 已配置。
        const offline = imageryStatus.online_allowed === false;
        const imageryCredit = offline
          ? `在线影像：未启用（${imageryStatus.reason_code || 'imagery_online_mode_disabled'}）`
          : imageryStatus.status === 'blocked'
            ? `在线影像：未授权，未渲染（${imageryStatus.reason_code || 'credential_unavailable'}）`
            : `在线影像：${onlineLabel} — ${onlineCredit}`;
        // 只有**真的在渲染**该底图时才追加来源标注（与上面同一判据）。
        const activeCredit = active === 'relief'
          ? ' · 底图：FABDEM 地形晕渲（非卫星影像，仅用于判读起伏）'
          : active === 'offline_satellite'
            ? ` · 底图：${offlineCredit}`
            : active === 'online_satellite'
              ? ` · 底图：${onlineLabel}（在线，${onlineCoverageNote}）` : '';
        creditsNode.textContent = `${offlineCredit} · ${imageryCredit} · ${terrainCredit}${activeCredit}`;
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
        const offlineMissing = status.offline_tile_missing || 0;
        const lastError = status.imagery_last_error;
        const offline = imageryStatus.online_allowed === false;
        const offlineSourceRow = (imageryStatus.sources || []).find(
          source => source.id === 'offline_zhoushan_imagery');
        const offlineOk = Boolean(offlineSourceRow && offlineSourceRow.status === 'available');
        const offlineResolvedNow = stats && stats.offline_imagery
          ? (stats.offline_imagery.resolved || 0) : 0;
        // 离线影像状态行**独立**于在线状态：两者是并行的两条底图链路，
        // 把离线结果塞进"在线是否配置"的分支会让面板自相矛盾。
        const offlineNote = offlineOk
          ? `本地离线影像：${offlineSourceRow.attribution || 'Copernicus Sentinel-2'}`
            + `（已完成 ${offlineResolvedNow} 张本地瓦片`
            + (offlineMissing > 0 ? `，数据范围外 ${offlineMissing} 张，正常` : '')
            + '，未消耗在线预算）'
          : `本地离线影像不可用：${(offlineSourceRow && offlineSourceRow.reason_code)
            || 'offline_imagery_unavailable'}`;
        if (hdPending && !arcgisConfigured) {
          // R7-D3C：未取得官方服务端 Key 时，在线高清模式是“待配置”，不是失败；
          // 此时不创建 provider、不触网，离线 Sentinel-2 仍照常工作。
          imageryNode.textContent =
            `天地图高清影像：待配置（缺少 ${(imageryStatus.hd_missing_configuration || [])
              .join('、') || 'CNS_TIANDITU_SERVER_KEY'}）；`
            + `官方接入方式为服务端 Key，浏览器端 tk 不使用，未触网 · ${offlineNote}`;
          imageryNode.className = 'dim';
        } else if (missingSettings && !offlineOk) {
          imageryNode.textContent = `${onlineLabel}未配置：缺少 ${missingSettings}（服务端设置后重启并刷新页面）· ${offlineNote}`;
          imageryNode.className = 'dim';
        } else if (offline && !offlineOk) {
          // 在线未授权 + 本地离线影像也不可用：两种原因都要说清。
          imageryNode.textContent = `${onlineLabel}：未启用在线访问（默认离线）· ${offlineNote}`;
          imageryNode.className = 'dim';
        } else if (imageryStatus.status === 'blocked' && !offlineOk) {
          imageryNode.textContent =
            `${onlineLabel}不可用：${imageryStatus.reason_code || 'credential_unavailable'} · ${offlineNote}`;
          imageryNode.className = 'err';
        } else if (failures > 0 && imageryShown) {
          imageryNode.textContent =
            `${onlineLabel}瓦片加载失败 ${failures} 次：${lastError || '未知原因'} · ${offlineNote}`;
          imageryNode.className = 'err';
        } else if (offlineResolvedNow > 0) {
          imageryNode.textContent = `${offlineNote} · 在线源：${offline
            ? '未启用（默认离线）' : '备用，未加载'}`;
          imageryNode.className = 'ok-text';
        } else if (imageryResolved > 0) {
          // 有真实瓦片完成才敢说"已渲染"；0 张时必须保持"等待验证"，不能空口宣称。
          const notes = [];
          if (discards > 0) notes.push(`Cesium 内部丢弃 ${discards} 次`);
          if (outOfScope > 0) notes.push(`范围外拒绝 ${outOfScope} 次`);
          const suffix = notes.length ? `（另有${notes.join('、')}，均属正常调度）` : '';
          imageryNode.textContent =
            `${providerName} 已渲染真实瓦片（已完成 ${imageryResolved} 张）${suffix}`;
          imageryNode.className = 'ok-text';
        } else if (discards > 0 || outOfScope > 0) {
          // 只有丢弃/范围外拒绝、没有任何真实失败：**不得**报成加载失败。
          imageryNode.textContent =
            `等待真实瓦片（丢弃 ${discards} 次、范围外 ${outOfScope} 次，非失败）· ${offlineNote}`;
          imageryNode.className = 'dim';
        } else {
          imageryNode.textContent = offlineOk
            ? `${offlineNote}（本地瓦片加载中）`
            : `${onlineLabel}已配置，等待真实瓦片验证`;
          imageryNode.className = 'dim';
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

    /**
     * 底图模式开关（离线开发 / 在线演示）—— Round38-E2E-PRECHECK 任务 B。
     *
     * 语义边界（必须与业务证据严格分开）：
     *  * 只切换**背景影像**加载模式，**不**表示任何 CNS 业务证据的 currentness，
     *    也**不**表示正式规划有效性；
     *  * **不**重建 Viewer、**不**动 camera、**不**增删业务实体、**不**改图层勾选、
     *    **不**写 ProjectState、**不**重算 FABDEM/geoid 或任何规划高度；
     *  * 影像图层在 ``createBasemap`` 中一次性建好，这里只切 ``show``
     *    ⇒ 连续切换不可能叠加图层或增长内存；
     *  * 在线不可用（``online_allowed=false`` 或凭证缺失）时，按钮**置灰**并如实
     *    披露真实原因码，**不**假装可用、**不**硬编码或提交任何 token。
     */
    const basemapModeStatus = $('basemapModeStatus');
    const modeReasonText = (reason) => {
      if (reason === 'imagery_online_mode_disabled') {
        return '未启用在线访问（服务端 CNS_IMAGERY_ONLINE_MODE 仍为默认 disabled）';
      }
      if (reason === 'arcgis_token_missing') {
        return '未配置影像凭证（CNS_ARCGIS_TOKEN 缺失）';
      }
      if (reason === 'tianditu_tk_missing') {
        return '未配置影像凭证（CNS_TIANDITU_TK 缺失）';
      }
      if (reason === 'imagery_budget_exhausted') return '影像瓦片预算已耗尽，请核对会话及每日剩余额度';
      if (reason === 'imagery_credential_rejected') return '影像凭证无效、过期或权限不足';
      if (reason === 'imagery_tile_timeout') return '在线影像瓦片超时';
      if (reason === 'imagery_tile_request_failed') return '在线影像瓦片请求失败';
      if (reason === 'superseded_by_newer_switch') return '已被更新的切换取代';
      // 离线影像相关原因：必须与在线失败**分开**表述，否则用户会去查凭证/网络，
      // 而真实问题是"本地离线瓦片数据缺失或未生成"。
      if (reason === 'offline_imagery_unavailable') return '本地离线影像数据不可用，已回退地形晕渲';
      if (reason === 'offline_imagery_not_configured') return '未配置本地离线影像数据源';
      if (reason === 'offline_imagery_metadata_missing') return '本地离线影像元信息缺失，请先生成离线瓦片数据';
      if (reason === 'offline_imagery_tiles_missing') return '本地离线影像瓦片目录缺失，请先生成离线瓦片数据';
      if (reason === 'offline_imagery_tile_missing') return '本地离线影像缺少该瓦片（数据范围边缘，正常）';
      return reason ? `原因码 ${reason}` : '未知原因';
    };
    const setModeStatus = (text, cls) => {
      if (!basemapModeStatus) return;
      basemapModeStatus.textContent = text;
      basemapModeStatus.className = cls || 'dim';
    };
    const offlineRadio = $('bmOffline');
    const offlineImageryRadio = $('bmOfflineImagery');
    const onlineRadio = $('bmOnline');
    const onlineUsable = typeof S.basemap.onlineAvailable === 'function'
      ? S.basemap.onlineAvailable() : false;
    const onlineReason = typeof S.basemap.onlineBlockReason === 'function'
      ? S.basemap.onlineBlockReason() : null;
    // 离线影像能力必须**独立**查询：它不依赖在线授权，因此在线被禁用时它照样可用。
    const offlineImageryUsable = typeof S.basemap.offlineImageryAvailable === 'function'
      ? S.basemap.offlineImageryAvailable() : false;
    const offlineImageryReason = typeof S.basemap.offlineImageryBlockReason === 'function'
      ? S.basemap.offlineImageryBlockReason() : null;
    const offlineImagerySource = (imageryStatus.sources || []).find(
      source => source.id === 'offline_zhoushan_imagery');
    const offlineImageryLabel = (offlineImagerySource && offlineImagerySource.label)
      || '舟山离线卫星影像';
    const offlineImageryCredit = (offlineImagerySource && offlineImagerySource.attribution)
      || 'Contains modified Copernicus Sentinel data';

    // 如实反映能力：离线影像不可用时置灰并给出**真实**原因。
    if (offlineImageryRadio) {
      if (!offlineImageryUsable) {
        offlineImageryRadio.disabled = true;
        offlineImageryRadio.title = `本地离线影像不可用：${modeReasonText(offlineImageryReason)}`;
        const label = offlineImageryRadio.closest('label');
        if (label) label.classList.add('bm-disabled');
      } else {
        offlineImageryRadio.title = '使用本地离线 Sentinel-2 影像（不触网、不消耗在线瓦片预算）';
      }
    }

    // 如实反映能力：在线不可用时置灰，并在标题里给出**真实**原因。
    // R7-D3C：天地图未取得官方服务端 Key 时，标签本身写明“待配置”，
    // 而不是笼统的“未配置/加载失败”，避免误以为是代码缺失。
    const onlineModeLabel = $('bmOnlineLabel');
    if (onlineModeLabel) onlineModeLabel.textContent = onlineLabel;
    if (onlineRadio) {
      if (!onlineUsable) {
        onlineRadio.disabled = true;
        onlineRadio.title = hdPending && !arcgisConfigured
          ? `${onlineLabel}；官方接入方式为服务端 Key，未配置时不触网；默认使用 Sentinel-2 离线底图`
          : `${onlineLabel}未配置：${missingSettings || modeReasonText(onlineReason)}`;
        const label = onlineRadio.closest('label');
        if (label) label.classList.add('bm-disabled');
      } else {
        onlineRadio.title = onlineIsHd
          ? `加载天地图高清影像（z18，约 0.52 m/px；${onlineCoverageNote}；服务端代理，服务端 Key 不下发）`
          : '加载已配置的在线卫星影像（保留 FABDEM 三维地形）';
      }
    }
    // 默认勾选与真实默认底图**一致**：本地影像可用即默认离线卫星影像。
    const syncModeRadios = () => {
      const current = typeof S.basemap.basemapMode === 'function'
        ? S.basemap.basemapMode() : 'offline_relief';
      if (offlineRadio) offlineRadio.checked = current === 'offline_relief';
      if (offlineImageryRadio) offlineImageryRadio.checked = current === 'offline_imagery';
      if (onlineRadio) onlineRadio.checked = current === 'online';
    };
    if (offlineImageryRadio) offlineImageryRadio.checked = false;
    if (offlineRadio) offlineRadio.checked = true;
    syncModeRadios();

    // 连续切换的竞态由 basemap 的 mode_serial 独占裁决（旧结果一律作废）。
    let modeSwitchSerial = 0;
    const applyMode = async (mode) => {
      const serial = ++modeSwitchSerial;
      if (mode === 'online') setModeStatus(`正在加载 ${onlineLabel}…`, 'dim');
      if (mode === 'offline_imagery') setModeStatus(`正在加载${offlineImageryLabel}…`, 'dim');
      const result = await S.basemap.setMode(mode);
      result.reason = await refineImageryFailure(result.reason);
      // 已有更新的切换 ⇒ 本次结果作废（既不写 UI，也不回传为成功）。
      if (serial !== modeSwitchSerial) {
        return { ok: false, mode: S.basemap.mode(), reason: 'superseded_by_newer_switch' };
      }
      // 走**最终**的 updateBasemapLabels（它在图层绑定处被包了一层，负责把
      // 「高清卫星影像」勾选框同步为实际生效的模式）。
      if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
      syncModeRadios();
      if (result.ok && result.mode === 'online') {
        setModeStatus(`底图模式：${onlineLabel}（${onlineCoverageNote}；真实瓦片已加载；${onlineCredit}）`, 'ok-text');
        log(`底图模式切换为${onlineLabel}：在线影像瓦片已真实到达`);
      } else if (result.ok && result.basemap_mode === 'offline_imagery') {
        setModeStatus(`底图模式：离线卫星影像（${offlineImageryLabel}，本地瓦片，未消耗在线预算）`, 'ok-text');
        log('底图模式切换为离线卫星影像：本地瓦片已加载（无上游请求）');
      } else if (result.mode === 'offline' && mode === 'online') {
        // 回退必须**明确披露原因**，且离线场景保持可用（绝不留下空白无提示）。
        setModeStatus(`底图模式：${result.basemap_mode === 'offline_imagery' ? '离线卫星影像' : '离线地形晕渲'}`
          + `（在线影像不可用，已回退；${modeReasonText(result.reason)}）`, 'err');
        log(`在线影像不可用，已回退离线：${result.reason || 'unknown'}`, 'err');
      } else if (result.mode === 'offline' && mode === 'offline_imagery') {
        // 离线影像不可用 ⇒ 回退晕渲，绝不出现蓝屏或空白。
        setModeStatus(`底图模式：离线地形晕渲（本地离线影像不可用，已回退；${modeReasonText(result.reason)}）`, 'err');
        log(`本地离线影像不可用，已回退地形晕渲：${result.reason || 'unknown'}`, 'err');
      } else if (result.mode === 'offline') {
        setModeStatus('底图模式：离线地形晕渲（FABDEM）', 'dim');
      }
      return result;
    };
    if (offlineRadio) offlineRadio.onchange = () => { if (offlineRadio.checked) applyMode('offline'); };
    if (offlineImageryRadio) {
      offlineImageryRadio.onchange = () => { if (offlineImageryRadio.checked) applyMode('offline_imagery'); };
    }
    if (onlineRadio) onlineRadio.onchange = () => { if (onlineRadio.checked) applyMode('online'); };
    // 首屏如实显示默认模式；在线不可用时把真实原因一并说明。
    if (offlineImageryUsable) {
      setModeStatus(`底图模式：离线卫星影像（${offlineImageryLabel}，本地瓦片，未消耗在线预算）`, 'dim');
    } else if (onlineUsable) {
      setModeStatus(`底图模式：离线地形晕渲（FABDEM）· 本地离线影像不可用：${modeReasonText(offlineImageryReason)}`, 'dim');
    } else {
      setModeStatus(`底图模式：离线地形晕渲（FABDEM）· ${onlineLabel}未配置：${missingSettings || modeReasonText(onlineReason)}`
        + ` · 本地离线影像不可用：${modeReasonText(offlineImageryReason)}`, 'dim');
    }
    S.applyBasemapMode = applyMode;
    S.basemapModeState = () => ({
      mode: typeof S.basemap.mode === 'function' ? S.basemap.mode() : null,
      basemap_mode: typeof S.basemap.basemapMode === 'function' ? S.basemap.basemapMode() : null,
      online_available: onlineUsable,
      online_block_reason: onlineReason,
      online_source_id: imagerySource ? imagerySource.id : null,
      online_is_hd: onlineIsHd,
      online_label: onlineLabel,
      online_credit: onlineCredit,
      online_coverage_note: onlineCoverageNote,
      hd_status: imageryStatus.hd_status || null,
      hd_pending_configuration: hdPending,
      hd_reason_code: imageryStatus.hd_reason_code || null,
      offline_imagery_available: offlineImageryUsable,
      offline_imagery_block_reason: offlineImageryReason,
      offline_imagery_attribution: offlineImageryCredit,
      status_text: basemapModeStatus ? basemapModeStatus.textContent : null,
    });
    // 低频兜底轮询：影像瓦片在首屏之后仍会陆续到达（尤其低空视角），
    // 仅靠 onImageryProgress 回调可能被节流/竞态漏掉，这里每 1.5 s 校正一次文案。
    if (!S.basemapLabelTimer) {
      S.basemapLabelTimer = setInterval(() => {
        // 走 S.updateBasemapLabels（最终版），保证轮询路径也会同步模式镜像勾选框。
        try {
          if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
        } catch (_error) { /* 文案刷新失败不影响渲染 */ }
      }, 1500);
    }
    const terrainLoad = $('terrainLoad');
    if (terrainLoad) terrainLoad.textContent = 'Cesium heightmap-1.0 · LOD 0–13 · 自动逐级细化';

    const groups = {
      routeDemo: [], routeCanonical: [], towersKnown: [], towersUnknown: [],
      facilities: [], buildings: [], terrain: [], communication: [], rid: [],
      radar: [], radarPreview: [], protection: [], protectionPreview: [],
      cnsSites: [], landingPads: [], fixedCorridor: [], assumptionSites: [],
    };
    S.groups = groups;
    // 三个懒加载 Promise 缓存挂在 `S` 上（而不是闭包局部变量）：这样
    // `S.clearBusinessLayers()` 才能真的把它们清掉——"切换航路或项目后清理旧图层
    // 和缓存"如果只清实体不清缓存，下一次勾选会直接复用旧几何。
    S.routeContextPromise = null;
    S.pointsPromise = null;
    S.towerGroupsPromise = null;

    const ensureRouteContext = async () => {
      if (!S.routeContextPromise) S.routeContextPromise = (async () => {
        S.routes = await getJSON(`${API}/routes`);
        const rows = S.routes.routes || [];
        const demo = rows.find(
          (row) => row.authority === 'engineering_demo_projection');
        // A7-OBS-03：航路顶点的高度需要逐点 geoid N，因此**先**按航路几何范围补齐地形
        // 窗口，再构造实体。缺这一步时（演示航路以前只在 `!S.terrainFrame` 时才补一次，
        // 判据不完整）terrainHeightAt 会因 terrainFrame 为 null 抛错，整条航线不显示。
        const points = [];
        for (const row of rows) {
          for (const point of row.path || []) points.push(point);
        }
        if (points.length) await ensureTerrainForPoints(points, '航路图层');
        return { demo, routes: rows };
      })();
      return S.routeContextPromise;
    };
    const ensurePoints = async () => {
      await ensureRouteContext();
      if (!S.pointsPromise) S.pointsPromise = getJSON(
        `${API}/manifest?layers=towers,cns_devices,cns_facilities`).then((payload) => {
        S.points = payload;
        const byId = {};
        for (const layer of payload.layers || []) byId[layer.id] = layer;
        return byId;
      });
      return S.pointsPromise;
    };
    const ensureTowerGroups = async () => {
      if (!S.towerGroupsPromise) S.towerGroupsPromise = (async () => {
        const byId = await ensurePoints();
        const items = (byId.towers && byId.towers.items) || [];
        // A7-BUG-01：塔实体需要"地形正高 → 椭球高"的逐点 N，因此**先**按塔点位范围
        // 补齐地形窗口，再构造实体。否则 terrainHeightAt 会在 terrainFrame 为 null 时
        // 抛错，整层塔一个都不出来（这正是人工验收复现的现场）。
        const terrainReady = await ensureTerrainForPoints(
          items.map((tower) => [tower.longitude, tower.latitude]), '铁塔图层');
        const result = addTowerEntities(viewer, items, { terrainReady });
        for (const entity of result.all) entity.show = false;
        groups.towersKnown = result.known;
        groups.towersUnknown = result.unknown;
        return result;
      })();
      return S.towerGroupsPromise;
    };

    // ---- Round38-T3D4：站址图例（颜色与图层身份必须与二维系统一致） ----------
    function renderCnsSiteLegend() {
      const node = $('cnsSiteLegend');
      if (!node) return;
      const payload = S.cnsSites;
      if (!payload) {
        node.innerHTML = '<div class="dim">未开启。勾选上方「CNS 站址与设备身份」后按需请求。</div>';
        return;
      }
      const deviceLegend = Object.entries(CNS_SERVICE_STYLE)
        .filter(([key]) => key !== 'other')
        .map(([, style]) => `<span><i style="background:${style.color}"></i>${style.label}`
          + ` ${style.color}</span>`).join('');
      const stateLegend = Object.entries(CNS_SITE_STATE_STYLE)
        .map(([, style]) => `<span><i style="background:${style.color}"></i>${style.label}`
          + `（${style.pixelSize}px）</span>`).join('');
      const counts = payload.counts || {};
      node.innerHTML =
        `<div class="svc-legend">${deviceLegend}</div>`
        + `<div class="svc-legend">${stateLegend}</div>`
        + `<div class="dim">共 ${payload.count} 个站址：既有设施 ${counts.existing_facilities || 0}、`
        + `规划选中 ${counts.planned_selected || 0}、候选 ${counts.candidate_unselected || 0}、`
        + `blocked ${counts.spatial_blocked || 0}。</div>`
        + `<div class="dim">铁塔 ${counts.tower_sites || 0}（严格解析塔顶 `
        + `${counts.towers_with_resolved_tower_top_geometry || 0}，工程估算 `
        + `${counts.towers_with_planning_estimate_geometry || 0}，缺塔顶 `
        + `${counts.towers_missing_tower_top || 0}；工程估算不等于已确认塔顶）、`
        + `地面站 ${counts.ground_sites || 0}。</div>`
        + '<div class="warn">站址存在 ≠ 设备已安装；候选站址不画成已布设设备。</div>';
    }
    S.renderCnsSiteLegend = renderCnsSiteLegend;

    // ---- Round38-T3D4：固定目标带面板（currentness 与垂直证据如实展示） -------
    function renderFixedCorridorPanel(bands, result) {
      const note = $('fixedCorridorNote');
      const list = $('fixedCorridorList');
      // A7-BAND-02：**几何可用（geometry available）** 与 **正式成果有效（business
      // currentness）** 是两个不同口径。此前的空态文案把"还没请求/已请求但没有航路带"
      // 一律写成"没有正式运行航路，无法给出固定保障目标带"，与用户已经亲眼看到的
      // "已画出 1/1 条固定 1000 m 目标带"直接冲突。这里按真实来源分类，且不再删除
      // 任何已经成功绘制的历史预览带。
      if (note) {
        if (!bands || !bands.length) {
          const loaded = Boolean(S.fixedCorridor);
          note.textContent = loaded
            ? '本图层已请求完成：当前项目没有携带固定保障目标带几何的航路'
              + `（服务端 band_count=${(S.fixedCorridor || {}).band_count || 0}）。`
              + '这不代表服务离线，也不代表本项目永久没有目标带。'
            : '尚未加载固定保障目标带图层。勾选上方「固定 1000 m CNS 保障目标带」后按需请求；'
              + '未加载前不判断是否有目标带。';
          note.className = 'dim';
        } else {
          const current = bands.filter((row) => row.drawable_as_current_band).length;
          const preview = bands.filter((row) => row.preview_allowed).length;
          const geometry = bands.filter((row) => row.route_geometry_fingerprint).length;
          note.innerHTML = `共 ${bands.length} 条航路：几何可用 ${geometry} 条`
            + `（几何可用 ≠ 正式成果有效）；正式 current 目标带 ${current} 条，`
            + `非权威历史预览 ${preview} 条。`
            + (current ? '' : '<div class="warn">当前没有 current 的保障证据，'
              + '因此这些带**不是**正式已实现覆盖，只能作明确标注的历史预览。</div>')
            + `<div class="warn">${FIXED_CORRIDOR_DISCLAIMER}</div>`;
          note.className = 'dim';
        }
      }
      if (!list) return;
      if (!bands || !bands.length) { list.innerHTML = ''; return; }
      list.innerHTML = bands.map((band, index) => {
        const drawable = corridorRingsFor(band).authority;
        const label = drawable === 'current'
          ? '<span class="ok-text">current</span>'
          : (drawable === 'non_authoritative_preview'
            ? `<span class="warn">非权威历史预览（${band.currentness}）</span>`
            : `<span class="err">不可绘制（${band.geometry_reason || band.reason || '—'}）</span>`);
        const vertical = band.vertical_extent_status === 'confirmed'
          ? `标称层切片 ${band.altitude_layer_id} = ${band.nominal_altitude_m} m`
          : '<span class="warn">无垂直上下界（仅水平目标带）</span>';
        return `<label><input type="checkbox" id="fixed_band_${index}" checked> `
          + `<span>${band.route_id} · ${label} · ${vertical}</span></label>`;
      }).join('')
        + `<div class="dim">${FIXED_CORRIDOR_PREVIEW_DISCLAIMER}</div>`;
      (bands || []).forEach((band, index) => {
        const input = $(`fixed_band_${index}`);
        if (!input) return;
        input.onchange = () => {
          const group = (result.groups || []).find((row) => row.route_id === band.route_id);
          if (!group) return;
          group.visible = input.checked;
          const layerOn = Boolean(($('ly_fixedCorridor') || {}).checked);
          for (const entity of group.entities) {
            entity.show = layerOn && group.visible;
          }
          S.stats.entities = viewer.entities.values.length;
          renderStatus();
        };
      });
    }
    S.renderFixedCorridorPanel = renderFixedCorridorPanel;

    S.lazyLayerLoaders = {
      flightProtectionResearch: async () => {
        const payload = await getJSON(`${API}/flight-protection-research`);
        S.flightProtectionResearch = payload;
        const result = addFlightProtectionResearch(viewer, payload, Cesium);
        groups.flightProtectionResearch = result.entities;
        const note = $('flightProtectionResearchNote');
        if (note) note.textContent = payload.status === 'available'
          ? '工程研究假设 · engineering_research_assumption · formal_admission_evaluated=false · overall_status=unknown。R0005：14点/13段，水平±20m，EGM2008正高80–120m；中心100m；第13段最低净空2.760m；建筑4tCa7 unknown（predicted）。点击边界或标记查看证据。'
          : `blocked：${payload.reason}；不猜测高度；overall_status=unknown`;
        return result.entities;
      },
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
      // Radar：**默认关闭**，只有用户勾选才请求。真实规划面阵与"设备参数示意预览"
      // 是两条互不相通的数据面：这里只加载**真实** selected_panels 派生的面阵，
      // 预览另有独立控制区、独立实体组与独立配色。
      radar: async () => {
        const payload = await getJSON(`${API}/radar-volumes`);
        S.coverage = { ...(S.coverage || {}), radar: payload };
        // 楔体外沿的斜距最大可达 5000 m ⇒ geoid 窗口必须按真实半径扩边。
        const radius = Math.max(0, ...(payload.volumes || []).map(
          (row) => (row.geometry || {}).max_slant_range_m || 0));
        const geoid = await ensureGeoidWindow(
          (payload.volumes || []).map((row) => [row.longitude, row.latitude]), radius);
        const result = addRadarVolumeEntities(
          viewer, payload.volumes || [], { Cesium, undulationAt });
        groups.radar = result.entities;
        S.radarGroups = result.groups;
        S.radarDiagnostics = { geoid_window: geoid, ...result.diagnostics };
        renderRadarPanelList();
        log(`Radar 面阵：${payload.geometry_count}/${payload.panels_total} 有确定几何，`
          + `状态 ${payload.status}（geoid 窗口 ${geoid.status}）`);
        if (payload.required_evidence && payload.required_evidence.length) {
          log(`Radar 缺证：${payload.required_evidence.slice(0, 2).join('；')}`, 'warn');
        }
        if (result.diagnostics.skipped.length) {
          log(`Radar 有 ${result.diagnostics.skipped.length} 项几何被跳过：`
            + result.diagnostics.skipped.map(
              (row) => `${row.part}=${row.reason}`).join('、'), 'warn');
        }
        // 交给 LazyLayerRegistry：整体开关必须尊重每个面阵各自的开关状态。
        return { entities: result.entities, setVisible: result.setVisible };
      },
      // P17：真实持久化结果与用户工程示意分属两条数据面。这里只加载 project_result；
      // 当前 demo 若与历史路线身份不符，只生成红色冲突锚点，绝不迁移 990 m 走廊。
      protection: async () => {
        const payload = await getJSON(`${API}/protection-volumes`);
        S.protection = payload;
        const points = protectionBoundaryPoints(payload.volumes || []);
        const geoid = await ensureGeoidWindow(points, 0);
        const result = addProtectionVolumeEntities(
          viewer, payload.volumes || [], { Cesium, undulationAt });
        groups.protection = result.entities;
        S.protectionGroups = result.groups;
        S.protectionDiagnostics = { geoid_window: geoid, ...result.diagnostics };
        renderProtectionPanelList();
        const note = $('protectionResultNote');
        if (note) {
          const available = payload.available_horizontal_count || 0;
          const conflicts = (payload.volumes || []).filter(
            (row) => row.status === 'geometry_version_conflict').length;
          note.textContent = `历史 P17 水平切片 ${available}；当前 demo 几何冲突 ${conflicts}；`
            + '未确认垂直范围不生成正式三维体。';
          note.className = conflicts ? 'warn' : 'dim';
        }
        log(`P17 保护区：${payload.status}，历史水平切片 ${payload.available_horizontal_count || 0}，`
          + `blocked ${payload.blocked_count || 0}（geoid 窗口 ${geoid.status}）`);
        return { entities: result.entities, setVisible: result.setVisible };
      },
      // Round38-T3D4：CNS 站址与设备身份（四类分层）。**默认关闭**，勾选才请求；
      // 只读身份投影，绝不推断安装、绝不重算覆盖。
      cnsSites: async () => {
        const payload = await getJSON(`${API}/cns-sites`);
        S.cnsSites = payload;
        const points = (payload.sites || []).map((row) => [row.longitude, row.latitude]);
        const geoid = await ensureGeoidWindow(points, 0);
        const result = addCnsSiteEntities(viewer, payload.sites || [], {
          Cesium,
          undulationAt,
          terrainHeightAt,
          plannedDevices: payload.planned_devices || [],
        });
        groups.cnsSites = result.entities;
        S.cnsSiteGroups = result;
        S.cnsSiteDiagnostics = { geoid_window: geoid, ...result.diagnostics };
        renderCnsSiteLegend();
        if (result.diagnostics.skipped.length) {
          log(`站址图层有 ${result.diagnostics.skipped.length} 项结构几何未绘制：`
            + result.diagnostics.skipped.slice(0, 3).map(
              (row) => `${row.part}=${row.reason}`).join('、'), 'warn');
        }
        log(`CNS 站址：${payload.status}，共 ${payload.count} 个（`
          + `塔 ${payload.counts.tower_sites} / 地面 ${payload.counts.ground_sites}；`
          + `有塔顶结构几何 ${payload.counts.towers_with_structure_geometry}，`
          + `缺塔顶 ${payload.counts.towers_missing_tower_top}；`
          + `既有设施 ${payload.counts.existing_facilities}，`
          + `规划选中 ${payload.counts.planned_selected}，`
          + `候选 ${payload.counts.candidate_unselected}）`);
        if ((payload.blocked_sites || []).length) {
          log(`Radar 站址输入 blocked ${payload.blocked_sites.length} 个`
            + `（保留身份与原因，不画设备）`, 'warn');
        }
        return groups.cnsSites;
      },
      // R7-D16：起降点（项目 + 参考）。只读共享候选快照；二维与三维共用同一张 PNG，
      // 起点/终点只通过锚点环与标签前缀区分，不改变任何坐标或业务身份。
      landingPads: async () => {
        const payload = await getJSON('/api/cns-shared-site-candidates');
        S.landingPads = payload;
        const endpoints = landingPadRouteEndpoints(
          (S.routes && S.routes.routes) || []);
        const points = ((payload && payload.candidates) || [])
          .filter((row) => row && (row.candidate_kind === 'project_node'
            || row.candidate_kind === 'reference_landing_site'))
          .map((row) => row.coordinate)
          .filter((row) => Array.isArray(row) && row.length >= 2);
        const geoid = points.length
          ? await ensureGeoidWindow(points, 0) : { status: 'not_required' };
        const result = addLandingPadEntities(viewer, payload, {
          Cesium, terrainHeightAt, endpoints,
        });
        groups.landingPads = result.entities;
        S.landingPadGroups = result;
        S.landingPadDiagnostics = { geoid_window: geoid, ...result.diagnostics };
        log(`起降点：${result.diagnostics.drawn}/${result.diagnostics.total} 个 billboard`
          + `（项目 + 参考；PNG ${LANDING_PAD_ICON_URL}；`
          + `起点/终点 ${endpoints ? '已按权威航路首末点标记' : '权威航路不唯一，仅按身份着色'}）`);
        if (result.diagnostics.skipped.length) {
          log(`起降点 ${result.diagnostics.skipped.length} 项未绘制：`
            + result.diagnostics.skipped.slice(0, 3).map(
              (row) => `${row.part}=${row.reason}`).join('、'), 'warn');
        }
        return groups.landingPads;
      },
      // R7-B：P16-V2 工程假设站址。只读任务 staged 结果；不读 ProjectState，
      // 不与正式 CNS 设备共用配色，也绝不把假设站址写成正式设备。
      assumptionSites: async () => {
        const payload = await getJSON(`${API}/assumption-sites`);
        S.assumptionSites = payload;
        if (payload.status !== 'available') {
          groups.assumptionSites = [];
          log(`P16-V2 假设站址暂不可用：${payload.reason || payload.status || 'unknown'}`
            + '（不会回退到 V1 正式方案）', 'warn');
          return [];
        }
        groups.assumptionSites = addAssumptionSiteEntities(viewer, payload, { Cesium });
        log(`P16-V2 假设站址：${payload.count} 个，来源任务 ${payload.source_task_id}`
          + `；${ASSUMPTION_SITES_DISCLAIMER}`);
        return groups.assumptionSites;
      },
      // Round38-T3D4：固定 1000 m CNS 保障目标带。几何与 currentness 全部来自服务端
      // 唯一实现；非 current 时只按**明确标注的非权威历史预览**绘制。
      fixedCorridor: async () => {
        const payload = await getJSON(`${API}/fixed-corridor-bands`);
        S.fixedCorridor = payload;
        const bands = payload.bands || [];
        // 目标带外环的正高来自服务端几何，椭球高需逐点 geoid N；按 bbox 取一次窗口。
        const lons = []; const lats = [];
        for (const band of bands) {
          for (const ring of (band.preview_polygons || []).concat(band.polygons || [])) {
            for (const point of ring) { lons.push(point[0]); lats.push(point[1]); }
          }
        }
        let geoid = { status: 'not_required' };
        if (lons.length) {
          const margin = 0.02;
          const bbox = [
            Math.min(...lons) - margin, Math.min(...lats) - margin,
            Math.max(...lons) + margin, Math.max(...lats) + margin,
          ];
          // 只在当前窗口**不覆盖**该 bbox 时才重新取地形窗口：避免为了画目标带
          // 把已经用于航路/站址判读的地形窗口反复换掉。
          const current = S.win && S.win.bbox;
          const covered = current && current[0] <= bbox[0] && current[1] <= bbox[1]
            && current[2] >= bbox[2] && current[3] >= bbox[3];
          if (!covered) await loadTerrainWindow(bbox, 256, 256);
          const center = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2];
          geoid = await ensureGeoidWindow([center], 0);
        }
        const result = addFixedCorridorBandEntities(viewer, bands, {
          Cesium, undulationAt, terrainHeightAt,
        });
        groups.fixedCorridor = result.entities;
        S.fixedCorridorGroups = result.groups;
        S.fixedCorridorDiagnostics = { geoid_window: geoid, ...result.diagnostics };
        renderFixedCorridorPanel(bands, result);
        log(`固定 1000 m 保障目标带：${payload.status}，可绘制 ${result.groups.length}/`
          + `${payload.band_count} 条（${payload.width_contract.width_policy_version}）`);
        if (result.diagnostics.skipped.length) {
          log(`目标带跳过 ${result.diagnostics.skipped.length} 个环：`
            + result.diagnostics.skipped.map(
              (row) => `${row.part}=${row.reason}`).join('、'), 'warn');
        }
        return { entities: result.entities, setVisible: result.setVisible };
      },
    };
    S.lazyLayers = new LazyLayerRegistry(S.lazyLayerLoaders);
    renderCnsSiteLegend();
    // A7-BAND-02：`null` = 图层尚未请求（不能断言"没有目标带"），`[]` = 已请求且为空。
    renderFixedCorridorPanel(null, { groups: [] });

    // ---- Radar 面阵清单（每个真实面阵单独成组，可单独关闭） --------------------
    function radarLayerEnabled() {
      const input = $('ly_radar');
      return Boolean(input && input.checked);
    }

    function renderRadarPanelList() {
      const node = $('radarPanelList');
      if (!node) return;
      const rows = S.radarGroups || [];
      if (!rows.length) {
        node.innerHTML = '<div class="dim">未开启。勾选上方「Radar 方向性覆盖」后按需请求；'
          + '无真实 selected_panels 时只显示缺证提示，不会补默认方向。</div>';
        return;
      }
      node.innerHTML = rows.map((group, index) => {
        const azimuth = group.azimuth_deg === null || group.azimuth_deg === undefined
          ? '—' : `${Number(group.azimuth_deg).toFixed(2)}°`;
        const status = group.status === 'available'
          ? '<span class="ok-text">已渲染楔体</span>'
          : `<span class="warn">缺证（${group.reason || 'blocked'}）</span>`;
        return `<label><input type="checkbox" id="radar_panel_${index}" `
          + `${group.visible ? 'checked' : ''}> <span>${group.panel_id || '—'} · `
          + `${group.radar_type || '—'} · 方位 ${azimuth} · ${status}</span></label>`;
      }).join('');
      rows.forEach((group, index) => {
        const input = $(`radar_panel_${index}`);
        if (!input) return;
        input.onchange = () => {
          group.visible = input.checked;
          for (const entity of group.entities) {
            entity.show = radarLayerEnabled() && group.visible;
          }
          S.stats.entities = viewer.entities.values.length;
          renderStatus();
        };
      });
    }

    // ---- 设备参数示意预览（与真实规划结果完全隔离） ----------------------------
    function fillRadarPreviewTowers(options) {
      const select = $('radarPreviewTower');
      if (!select) return;
      select.innerHTML = (options || []).map((row) => {
        const status = row.tower_confirmed ? '已确认' : '未确认';
        const height = row.tower_top_orthometric_m === null
          || row.tower_top_orthometric_m === undefined
          ? '—' : `${Number(row.tower_top_orthometric_m).toFixed(2)} m`;
        return `<option value="${row.tower_id}">${row.tower_id}`
          + `${row.name ? ` ${row.name}` : ''} · 塔顶正高 ${height} · ${status}</option>`;
      }).join('');
    }

    function setRadarPreviewNote(text, cls) {
      const node = $('radarPreviewNote');
      if (!node) return;
      node.textContent = text || '';
      node.className = cls || 'dim';
    }

    function clearRadarPreviewEntities() {
      for (const entity of groups.radarPreview || []) viewer.entities.remove(entity);
      groups.radarPreview = [];
      S.radarPreviewGroups = [];
    }

    async function loadRadarPreviewTowers() {
      setRadarPreviewNote('正在读取候选站位…');
      const payload = await getJSON(
        `${API}/radar-volumes?mode=device_spec_preview`);
      S.radarPreviewOptions = payload;
      fillRadarPreviewTowers(payload.preview_tower_options || []);
      setRadarPreviewNote(
        `候选站位 ${(payload.preview_tower_options || []).length} 个：`
        + '只列出塔顶正高**有来源记录**的塔位（含未确认状态，逐条标注）。');
    }

    async function buildRadarPreview() {
      const towerSelect = $('radarPreviewTower');
      const typeSelect = $('radarPreviewType');
      const azimuthInput = $('radarPreviewAzimuth');
      const ackInput = $('radarPreviewAck');
      const query = new URLSearchParams({ mode: 'device_spec_preview' });
      if (towerSelect && towerSelect.value) query.set('tower_id', towerSelect.value);
      if (typeSelect && typeSelect.value) query.set('radar_type', typeSelect.value);
      if (azimuthInput && azimuthInput.value !== '') {
        query.set('azimuth_deg', azimuthInput.value);
      }
      // 未确认塔顶派生正高**必须**由用户显式承认，否则服务端会 fail-closed。
      if (ackInput && ackInput.checked) {
        query.set('preview_origin_mode', 'tower_top_orthometric_preview');
      }
      const payload = await getJSON(`${API}/radar-volumes?${query.toString()}`);
      S.radarPreview = payload;
      if (!S.radarPreviewOptions && (payload.preview_tower_options || []).length) {
        fillRadarPreviewTowers(payload.preview_tower_options);
      }
      clearRadarPreviewEntities();
      if (payload.geometry_count) {
        const radius = Math.max(0, ...(payload.volumes || []).map(
          (row) => (row.geometry || {}).max_slant_range_m || 0));
        await ensureGeoidWindow(
          (payload.volumes || []).map((row) => [row.longitude, row.latitude]), radius);
        const result = addRadarPreviewEntities(
          viewer, payload.volumes || [], { Cesium, undulationAt });
        groups.radarPreview = result.entities;
        S.radarPreviewGroups = result.groups;
        setRadarPreviewNote(
          `已生成示意楔体 1 个（${payload.volumes[0].radar_type}，`
          + `中心方位 ${Number(payload.volumes[0].azimuth_deg).toFixed(2)}°，`
          + `原点状态 ${payload.preview_origin_status}）。不写入项目状态。`, 'ok-text');
      } else {
        const reason = payload.reason || (payload.required_evidence || []).join('；')
          || 'preview_not_rendered';
        setRadarPreviewNote(`未生成几何：${reason}（缺证时不渲染确定几何）`, 'warn');
      }
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
    }

    function clearRadarPreview() {
      clearRadarPreviewEntities();
      S.radarPreview = null;
      setRadarPreviewNote('已清除示意几何（真实规划面阵不受影响）。');
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
    }

    const previewLoadButton = $('radarPreviewLoad');
    if (previewLoadButton) {
      previewLoadButton.onclick = () => loadRadarPreviewTowers().catch((error) => {
        setRadarPreviewNote(`读取候选站位失败：${error.message}`, 'err');
      });
    }
    const previewBuildButton = $('radarPreviewBuild');
    if (previewBuildButton) {
      previewBuildButton.onclick = () => buildRadarPreview().catch((error) => {
        setRadarPreviewNote(`生成示意失败：${error.message}`, 'err');
      });
    }
    const previewClearButton = $('radarPreviewClear');
    if (previewClearButton) previewClearButton.onclick = clearRadarPreview;
    const disclaimer = $('radarPreviewDisclaimer');
    if (disclaimer) disclaimer.textContent = RADAR_PREVIEW_DISCLAIMER;
    renderRadarPanelList();

    // ---- P17 工程示意：必须显式选路线并输入宽度/上下高度 ----------------------
    function setProtectionPreviewNote(text, cls) {
      const node = $('protectionPreviewNote');
      if (!node) return;
      node.textContent = text || '';
      node.className = cls || 'dim';
    }

    function clearProtectionPreviewEntities() {
      removeProtectionEntities(viewer, groups.protectionPreview || []);
      groups.protectionPreview = [];
      S.protectionPreviewGroups = [];
    }

    function fillProtectionRoutes(options) {
      const select = $('protectionPreviewRoute');
      if (!select) return;
      select.innerHTML = '<option value="">请选择航路…</option>' + (options || []).map((row) => {
        const identity = row.route_geometry_identity || {};
        return `<option value="${identity.fingerprint || ''}">${row.route_id || '—'} · `
          + `${row.authority || '—'} · ${identity.point_count || row.point_count || '—'} 点 · `
          + `${row.status || 'unknown'}</option>`;
      }).join('');
    }

    async function loadProtectionPreviewRoutes() {
      setProtectionPreviewNote('正在读取可选航路身份…');
      const payload = await getJSON(
        `${API}/protection-volumes?mode=engineering_scenario_preview`);
      S.protectionPreviewOptions = payload;
      fillProtectionRoutes(payload.route_options || []);
      setProtectionPreviewNote('请选择航路并手工输入半宽及上下正高；系统不预填权威数值。');
    }

    async function buildProtectionPreview() {
      const route = $('protectionPreviewRoute');
      const width = $('protectionPreviewWidth');
      const lower = $('protectionPreviewLower');
      const upper = $('protectionPreviewUpper');
      if (!route || !route.value || !width || width.value === ''
          || !lower || lower.value === '' || !upper || upper.value === '') {
        setProtectionPreviewNote('必须主动选择航路，并填写水平半宽、下界和上界。', 'warn');
        return;
      }
      const query = new URLSearchParams({
        mode: 'engineering_scenario_preview',
        route_geometry_fingerprint: route.value,
        half_width_m: width.value,
        lower_altitude_m: lower.value,
        upper_altitude_m: upper.value,
      });
      const payload = await getJSON(`${API}/protection-volumes?${query.toString()}`);
      clearProtectionPreviewEntities();
      const points = protectionBoundaryPoints(payload.volumes || []);
      await ensureGeoidWindow(points, 0);
      const result = addProtectionPreviewEntities(
        viewer, payload.volumes || [], { Cesium, undulationAt });
      groups.protectionPreview = result.entities;
      S.protectionPreviewGroups = result.groups;
      S.protectionPreview = payload;
      setProtectionPreviewNote(
        `已生成隔离示意：半宽 ${payload.volumes[0].horizontal_half_width_m} m，`
        + `高度 ${payload.volumes[0].lower_altitude_m}–${payload.volumes[0].upper_altitude_m} m。`,
        'ok-text');
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
    }

    function clearProtectionPreview() {
      clearProtectionPreviewEntities();
      S.protectionPreview = null;
      setProtectionPreviewNote('已清除工程示意；历史 P17 图层不受影响。');
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
    }

    const protectionLoad = $('protectionPreviewLoad');
    if (protectionLoad) protectionLoad.onclick = () => loadProtectionPreviewRoutes()
      .catch((error) => setProtectionPreviewNote(`读取航路失败：${error.message}`, 'err'));
    const protectionBuild = $('protectionPreviewBuild');
    if (protectionBuild) protectionBuild.onclick = () => buildProtectionPreview()
      .catch((error) => setProtectionPreviewNote(`生成示意失败：${error.message}`, 'err'));
    const protectionClear = $('protectionPreviewClear');
    if (protectionClear) protectionClear.onclick = clearProtectionPreview;
    const protectionDisclaimer = $('protectionPreviewDisclaimer');
    if (protectionDisclaimer) protectionDisclaimer.textContent = PROTECTION_PREVIEW_DISCLAIMER;

    function protectionLayerEnabled() {
      const input = $('ly_protection');
      return Boolean(input && input.checked);
    }

    /** 复选框 id 用实例键派生（下标不是身份，只在验收辅助里作为兼容参数）。 */
    function protectionGroupDomId(instanceKey) {
      return `protection_group_${String(instanceKey).replace(/[^A-Za-z0-9_-]/g, '')}`;
    }

    function protectionGroupByKey(value) {
      const rows = S.protectionGroups || [];
      if (typeof value === 'string' && value) {
        return rows.find((group) => group.render_instance_id === value) || null;
      }
      // 兼容 Round37-T3D-3A 验收脚本旧的数组下标调用；仅限验收辅助，不作为长期标识。
      return rows[Number(value) || 0] || null;
    }

    function applyProtectionGroupVisibility(group) {
      const enabled = protectionLayerEnabled();
      for (const entity of group.entities) entity.show = Boolean(enabled && group.visible);
    }

    function setProtectionGroupVisible(target, value) {
      const group = protectionGroupByKey(target);
      if (!group) return false;
      group.visible = Boolean(value);
      applyProtectionGroupVisibility(group);
      const input = $(protectionGroupDomId(group.render_instance_id));
      if (input) input.checked = group.visible;
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
      return true;
    }

    function renderProtectionPanelList() {
      const node = $('protectionPanelList');
      if (!node) return;
      const rows = S.protectionGroups || [];
      if (!rows.length) {
        node.innerHTML = '<div class="dim">未开启。开启后逐项列出 historical/stale 与冲突。</div>';
        return;
      }
      node.innerHTML = rows.map((group) => {
        const label = group.status === 'historical_stale'
          ? '历史 P17 水平切片（stale）'
          : `缺证：${group.status}`;
        const authority = group.route_authority || '—';
        const key = group.render_instance_id || '';
        return `<label title="${key}"><input type="checkbox" `
          + `id="${protectionGroupDomId(key)}" data-instance="${key}" `
          + `${group.visible ? 'checked' : ''}> <span>${group.route_id || '—'} · `
          + `${authority} · ${label}</span></label>`;
      }).join('');
      rows.forEach((group) => {
        const input = $(protectionGroupDomId(group.render_instance_id));
        if (!input) return;
        input.onchange = () => setProtectionGroupVisible(
          group.render_instance_id, input.checked);
      });
    }
    renderProtectionPanelList();
    // 实例键驱动的保护体开关（验收脚本经 window.__CNS_3D__ 复用同一实现）。
    S.protectionApi = {
      setGroupVisible: setProtectionGroupVisible,
      groupByKey: protectionGroupByKey,
    };

    // ---- 模拟轨迹回放：用户配置 + 单一时间基准 + 三类语义严格分离 --------------
    // 这里**没有**任何几何判定：进入/离开事件全部来自后端 assess_simulated_track。
    // 前端只负责插值、时间轴、事件标记与实体绑定，因此不可能出现"第二套判定算法"。
    S.intrusion.deps = { Cesium, undulationAt };
    const intrusionDeps = () => ({
      Cesium, undulationAt, volumeBounds: S.intrusion.volumeBounds,
    });
    const finiteOrNull = (value) => {
      if (value === null || value === undefined || value === '') return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    };

    function setIntrusionNote(text, cls) {
      const node = $('intrusionNote');
      if (!node) return;
      node.textContent = text || '';
      node.className = cls || 'dim';
    }

    function renderIntrusionHud(state) {
      const node = $('intrusionHud');
      if (!node) return;
      if (!state) {
        node.className = 'dim';
        node.innerHTML = '尚未配置模拟轨迹。';
        return;
      }
      const text = describeState(state);
      node.className = '';
      node.innerHTML = `<div>当前模拟时间：<b>${text.timeText}</b>（${text.elapsedText}）</div>`
        + `<div>目标位置：${text.positionText}（EGM2008 正高）</div>`
        + `<div>水平：${text.horizontalText}</div>`
        + `<div>三维示意：${text.volumeText}</div>`
        + `<div>${text.surveillanceText}</div>`
        + `<div class="warn">${text.banner}</div>`;
    }

    function renderIntrusionEvents(model) {
      const node = $('intrusionEvents');
      if (!node) return;
      if (!model || !model.events.length) {
        node.className = 'dim';
        node.innerHTML = '未配置或无 entry/exit 事件。';
        return;
      }
      node.className = '';
      node.innerHTML = model.events.map((row, index) => {
        const scope = SCOPE_LABELS[row.scope] || row.scope;
        const kind = row.kind === 'entry' ? '进入' : '离开';
        const crossing = row.crossing ? '' : '（非物理穿越）';
        const height = row.altitudeEgm2008M === null
          ? '高度缺失' : `${row.altitudeEgm2008M.toFixed(1)} m`;
        return `<button data-event-index="${index}">${row.timeS.toFixed(2)} s · `
          + `${scope} · ${kind}${crossing} · ${height}</button>`;
      }).join('');
      node.querySelectorAll('button[data-event-index]').forEach((button) => {
        button.onclick = () => {
          if (!focusIntrusionEvent(Number(button.dataset.eventIndex))) {
            setIntrusionNote('该事件点缺少 EGM2008 geoid 偏移，无法定位（不猜高度）。', 'warn');
          }
        };
      });
    }

    function eventCartesian(row) {
      const undulation = undulationAt(row.longitude, row.latitude);
      if (!Number.isFinite(undulation) || !Number.isFinite(row.altitudeEgm2008M)) return null;
      return Cesium.Cartesian3.fromDegrees(
        row.longitude, row.latitude, row.altitudeEgm2008M + undulation);
    }

    function focusIntrusionEvent(index) {
      const model = S.intrusion.model;
      const row = model && model.events[index];
      if (!row) return false;
      const position = eventCartesian(row);
      if (!position) return false;
      // 定位到事件点上方 900 m：保留当前朝向（通常已切到俯视），不贴地也不跳变。
      const undulation = undulationAt(row.longitude, row.latitude);
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(
          row.longitude, row.latitude, row.altitudeEgm2008M + undulation + 1500),
        duration: 0.8,
      });
      const handle = S.intrusion.handle;
      const marker = ((handle && handle.eventEntities) || []).find(
        (entity) => entity.__cns && entity.__cns.eventIndex === index);
      if (marker) showPopup(marker, { x: 330, y: 92 });
      return true;
    }

    function applyIntrusionFrame(state) {
      const handle = S.intrusion.handle;
      if (!handle) return;
      const applied = applyIntrusionState(handle, state, S.intrusion.deps);
      if (S.intrusion.follow && S.intrusion.follow.following
          && applied && applied.position) {
        S.intrusion.follow.update(applied.position);
      }
      renderIntrusionHud(state);
      const timeline = $('intrusionTimeline');
      if (timeline && !S.intrusion.dragging) timeline.value = String(state.timeS);
    }

    function syncIntrusionTimeline(model) {
      const timeline = $('intrusionTimeline');
      if (!timeline || !model) return;
      timeline.min = String(model.startTimeS);
      timeline.max = String(model.endTimeS);
      timeline.step = String(Math.max(0.01, model.durationS / 500));
      timeline.value = String(model.startTimeS);
      timeline.disabled = false;
    }

    function clearIntrusionEntities() {
      if (S.intrusion.controller) S.intrusion.controller.pause();
      if (S.intrusion.follow) S.intrusion.follow.disable();
      if (S.intrusion.handle) removeIntrusionEntities(viewer, S.intrusion.handle);
      S.intrusion.handle = null;
      S.intrusion.controller = null;
      S.intrusion.model = null;
      S.intrusion.volumeBounds = null;
      groups.intrusion = [];
      S.intrusion.revision += 1;
    }

    function intrusionTargetOptions(projectPayload, previewVolume) {
      const options = [];
      for (const volume of (projectPayload || {}).volumes || []) {
        options.push({
          key: `project:${volume.render_instance_id}`,
          mode: 'project_result',
          renderInstanceId: volume.render_instance_id,
          label: `${volume.route_id || '—'} · ${volume.route_authority || '—'} · `
            + `${volume.status} · ${volume.vertical_extent_status}`,
          volume,
        });
      }
      if (previewVolume) {
        options.push({
          key: `preview:${previewVolume.render_instance_id}`,
          mode: 'engineering_scenario_preview',
          renderInstanceId: previewVolume.render_instance_id,
          label: `${previewVolume.route_id || '—'} · 用户工程示意 · `
            + `${previewVolume.horizontal_half_width_m} m · `
            + `${previewVolume.lower_altitude_m}–${previewVolume.upper_altitude_m} m`,
          volume: previewVolume,
        });
      }
      return options;
    }

    async function loadIntrusionTargets() {
      setIntrusionNote('正在读取保护体实例…');
      const project = await getJSON(`${API}/protection-volumes`);
      S.protection = S.protection || project;
      // 预设需要航路几何身份来构造垂线方向；只取身份端点，不额外拉地形窗口。
      if (!S.routes) S.routes = await getJSON(`${API}/routes`);
      const previewVolume = (((S.protectionPreview || {}).volumes) || [])[0] || null;
      const options = intrusionTargetOptions(project, previewVolume);
      S.intrusion.targets = options;
      const select = $('intrusionTarget');
      if (select) {
        select.innerHTML = '<option value="">请选择保护体实例…</option>'
          + options.map((row) => `<option value="${row.key}">${row.label}</option>`).join('');
        select.value = options.some((row) => row.key === S.intrusion.selectedKey)
          ? S.intrusion.selectedKey : '';
      }
      setIntrusionNote(options.length
        ? `已读取 ${options.length} 个保护体实例（历史 / 缺证冲突 / 用户示意各自独立实例键）。`
          + '历史实例只能给水平相交结论，不能得到三维入侵。'
        : '当前没有可用保护体实例：请先开启 P17 图层，或在「自定义保护区示意」生成一个示意。');
      return options;
    }

    function selectedIntrusionTarget() {
      const select = $('intrusionTarget');
      const key = select ? select.value : '';
      return (S.intrusion.targets || []).find((row) => row.key === key) || null;
    }

    /** 演示预设：全部数值都是**工程假设**，由用户主动点击才写入输入框。 */
    function buildIntrusionPreset(kind, target) {
      const volume = (target || {}).volume || {};
      const routeFingerprint = ((volume.route_geometry_identity || {}).fingerprint) || '';
      const routes = (S.routes && S.routes.routes) || [];
      const route = routes.find(
        (row) => ((row.identity || {}).fingerprint) === routeFingerprint) || null;
      if (!route || !Array.isArray(route.path) || route.path.length < 2) {
        return { error: 'no_matching_route_geometry',
          message: '当前没有与该保护体实例匹配的航路几何，无法生成垂线方向；请先开启航路图层。' };
      }
      const lower = finiteOrNull(volume.lower_altitude_m);
      const upper = finiteOrNull(volume.upper_altitude_m);
      const slice = finiteOrNull(volume.nominal_altitude_slice_egm2008_m);
      const halfWidth = finiteOrNull(volume.horizontal_half_width_m)
        ?? finiteOrNull(volume.outer_half_width_m);
      const path = route.path;
      const anchor = path[Math.floor(path.length / 2)];
      const before = path[Math.max(0, Math.floor(path.length / 2) - 1)];
      const after = path[Math.min(path.length - 1, Math.floor(path.length / 2) + 1)];
      let dx = after[0] - before[0];
      let dy = after[1] - before[1];
      const norm = Math.hypot(dx, dy);
      if (!Number.isFinite(norm) || norm <= 0) {
        return { error: 'degenerate_route_direction',
          message: '航路在锚点处方向退化，无法生成垂线；请手工输入轨迹节点。' };
      }
      dx /= norm; dy /= norm;
      const px = -dy; const py = dx;
      const cosLat = Math.max(0.2, Math.cos((anchor[1] * Math.PI) / 180));
      const halfSpanM = (halfWidth === null ? 250 : halfWidth) * 1.6;
      const lonStep = (halfSpanM * px) / (111320 * cosLat);
      const latStep = (halfSpanM * py) / 111320;
      const nodes = [];
      const midAltitude = (lower !== null && upper !== null) ? (lower + upper) / 2 : slice;
      if (midAltitude === null) {
        return { error: 'altitude_missing',
          message: '该实例既无确认上下界也无名义高度切片：拒绝生成高度为 0 的假轨迹。' };
      }
      if (kind === 'vertical') {
        const lowerAltitude = lower !== null ? lower - 40 : midAltitude - 60;
        const upperAltitude = upper !== null ? upper + 40 : midAltitude + 60;
        for (const [time, altitude] of [
          [0, lowerAltitude], [45, midAltitude], [90, upperAltitude]]) {
          nodes.push({
            longitude: Number(anchor[0].toFixed(7)), latitude: Number(anchor[1].toFixed(7)),
            altitude_egm2008_m: Number(altitude.toFixed(3)), relative_time_s: time,
          });
        }
      } else {
        for (const [time, sign] of [[0, -1], [45, 0], [90, 1]]) {
          nodes.push({
            longitude: Number((anchor[0] + sign * lonStep).toFixed(7)),
            latitude: Number((anchor[1] + sign * latStep).toFixed(7)),
            altitude_egm2008_m: Number(midAltitude.toFixed(3)), relative_time_s: time,
          });
        }
      }
      return {
        kind, nodes, routeId: route.route_id || null,
        halfSpanM,
        assumptions: {
          route_geometry: 'from_existing_route_identity',
          half_width_m: halfWidth,
          lower_altitude_m: lower,
          upper_altitude_m: upper,
          nominal_altitude_slice_egm2008_m: slice,
          altitude_mid_m: midAltitude,
          vertical_reference: 'EGM2008',
          note: '工程假设：数值全部来自已有航路身份或用户输入，不是权威探测或运行事实',
        },
      };
    }

    function loadIntrusionPreset(kind = 'crossing') {
      const target = selectedIntrusionTarget();
      if (!target) {
        setIntrusionNote('必须先在「保护体实例」里选择对象，再载入演示预设。', 'warn');
        return { status: 'selection_required' };
      }
      const preset = buildIntrusionPreset(kind, target);
      if (preset.error) {
        setIntrusionNote(preset.message, 'warn');
        return { status: preset.error, message: preset.message };
      }
      const textarea = $('intrusionTrack');
      if (textarea) textarea.value = JSON.stringify(preset.nodes, null, 0);
      setIntrusionNote(`已载入${kind === 'vertical' ? '垂直穿越' : '水平穿越'}演示预设`
        + `（工程假设，非权威事实）：${preset.nodes.length} 个节点，时长 `
        + `${preset.nodes[preset.nodes.length - 1].relative_time_s} s。可手工修改后再判定。`,
      'warn');
      return { status: 'loaded', kind, nodes: preset.nodes, assumptions: preset.assumptions };
    }

    async function assessIntrusion() {
      const target = selectedIntrusionTarget();
      if (!target) {
        setIntrusionNote('必须显式选择保护体实例；系统不会替你挑一个。', 'warn');
        return { status: 'selection_required' };
      }
      const textarea = $('intrusionTrack');
      const raw = textarea ? textarea.value.trim() : '';
      if (!raw) {
        setIntrusionNote('必须由用户配置至少两个轨迹节点。', 'warn');
        return { status: 'track_required' };
      }
      let nodes;
      try {
        nodes = JSON.parse(raw);
      } catch (error) {
        setIntrusionNote(`轨迹 JSON 解析失败：${error.message}`, 'err');
        return { status: 'track_invalid', message: error.message };
      }
      if (!Array.isArray(nodes) || nodes.length < 2) {
        setIntrusionNote('轨迹必须是至少两个节点的 JSON 数组。', 'warn');
        return { status: 'track_invalid', message: 'at least two nodes' };
      }
      const query = new URLSearchParams({
        mode: target.mode,
        render_instance_id: target.renderInstanceId,
        track_id: 'SIM-R0005-USER',
        track: JSON.stringify(nodes),
      });
      if (target.mode === 'engineering_scenario_preview') {
        const volume = target.volume;
        query.set('route_geometry_fingerprint',
          ((volume.route_geometry_identity || {}).fingerprint) || '');
        query.set('half_width_m', String(volume.horizontal_half_width_m));
        query.set('lower_altitude_m', String(volume.lower_altitude_m));
        query.set('upper_altitude_m', String(volume.upper_altitude_m));
      }
      setIntrusionNote('正在按后端合同判定轨迹 × 保护体…');
      const payload = await getJSON(`${API}/intrusion-assessment?${query.toString()}`);
      const model = buildPlaybackModel(payload);

      clearIntrusionEntities();   // 先清再建：绝不把新轨迹叠加到旧实体上。
      const geoid = await ensureGeoidWindow(
        model.nodes.map((row) => [row.longitude, row.latitude]), 0);
      const lower = finiteOrNull(target.volume.lower_altitude_m);
      const upper = finiteOrNull(target.volume.upper_altitude_m);
      S.intrusion.volumeBounds = (lower !== null && upper !== null)
        ? { lower, upper } : null;
      S.intrusion.handle = addIntrusionEntities(viewer, model, intrusionDeps());
      groups.intrusion = S.intrusion.handle.entities;
      S.intrusion.model = model;
      S.intrusion.payload = payload;
      S.intrusion.geoidWindow = geoid;
      S.intrusion.revision += 1;
      S.intrusion.evaluationRevision += 1;
      S.intrusion.follow = createCameraFollow(viewer, { Cesium });
      S.intrusion.controller = new PlaybackController({
        model,
        onUpdate: (state) => applyIntrusionFrame(state),
      });
      const layerInput = $('ly_intrusion');
      if (layerInput) {
        layerInput.checked = true;
        S.intrusion.layerWanted = true;
      }
      setIntrusionVisible(S.intrusion.handle, true);
      syncIntrusionTimeline(model);
      renderIntrusionEvents(model);
      applyIntrusionFrame(S.intrusion.controller.state);
      const horizontal = payload.horizontal_projection_intersection || {};
      const intrusion = payload.three_dimensional_volume_intrusion || {};
      setIntrusionNote(`判定完成：水平 ${horizontal.status}；三维示意 `
        + `${intrusion.status}${intrusion.reason ? `（${intrusion.reason}）` : ''}；`
        + `监视探测 ${(payload.surveillance_detection || {}).status}。`
        + `轨迹实体 ${S.intrusion.handle.entities.length} 个（geoid 窗口 ${geoid.status}）。`,
      intrusion.status === 'intrudes' ? 'warn' : 'dim');
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
      return {
        status: 'evaluated', model, payload,
        handle_entity_count: S.intrusion.handle.entities.length,
      };
    }

    function clearIntrusion() {
      clearIntrusionEntities();
      for (const id of ['intrusionPlay', 'intrusionReset']) {
        const node = $(id);
        if (node) node.textContent = id === 'intrusionPlay' ? '▶ 播放' : '↺ 重置';
      }
      const timeline = $('intrusionTimeline');
      if (timeline) { timeline.disabled = true; timeline.value = '0'; }
      const follow = $('intrusionFollow');
      if (follow) follow.checked = false;
      renderIntrusionEvents(null);
      renderIntrusionHud(null);
      setIntrusionNote('已清除模拟目标、轨迹与事件标记；无残留实体。');
      S.stats.entities = viewer.entities.values.length;
      renderStatus();
      return { status: 'cleared', entities: viewer.entities.values.length };
    }

    const intrusionPlayButton = $('intrusionPlay');
    if (intrusionPlayButton) {
      intrusionPlayButton.onclick = () => {
        const controller = S.intrusion.controller;
        if (!controller) {
          setIntrusionNote('先选择保护体实例并「开始判定并回放」，才有可播放的轨迹。', 'warn');
          return;
        }
        controller.toggle();
        intrusionPlayButton.textContent = controller.playing ? '⏸ 暂停' : '▶ 播放';
      };
    }
    const intrusionResetButton = $('intrusionReset');
    if (intrusionResetButton) {
      intrusionResetButton.onclick = () => {
        const controller = S.intrusion.controller;
        if (!controller) return;
        controller.reset();
        if (intrusionPlayButton) intrusionPlayButton.textContent = '▶ 播放';
      };
    }
    const intrusionTimeline = $('intrusionTimeline');
    if (intrusionTimeline) {
      intrusionTimeline.onpointerdown = () => { S.intrusion.dragging = true; };
      const endDrag = () => { S.intrusion.dragging = false; };
      intrusionTimeline.onpointerup = endDrag;
      intrusionTimeline.onchange = endDrag;
      intrusionTimeline.oninput = () => {
        const controller = S.intrusion.controller;
        if (!controller) return;
        // 拖动与播放使用同一个时间基准：先暂停，再按拖动值 seek，显示必然一致。
        controller.pause();
        if (intrusionPlayButton) intrusionPlayButton.textContent = '▶ 播放';
        controller.scrub(Number(intrusionTimeline.value));
      };
    }
    const intrusionSpeed = $('intrusionSpeed');
    if (intrusionSpeed) {
      intrusionSpeed.innerHTML = PLAYBACK_SPEEDS.map(
        (value) => `<option value="${value}"${value === 1 ? ' selected' : ''}>${value}×`
          + '</option>').join('');
      intrusionSpeed.onchange = () => {
        if (S.intrusion.controller) S.intrusion.controller.setSpeed(intrusionSpeed.value);
      };
    }
    const intrusionFollowInput = $('intrusionFollow');
    if (intrusionFollowInput) {
      intrusionFollowInput.onchange = () => {
        const handle = S.intrusion.handle;
        if (!handle || !S.intrusion.follow) {
          intrusionFollowInput.checked = false;
          setIntrusionNote('先判定并生成模拟轨迹，再开启相机跟随。', 'warn');
          return;
        }
        if (intrusionFollowInput.checked) {
          const state = S.intrusion.controller.state;
          const applied = applyIntrusionState(handle, state, S.intrusion.deps);
          S.intrusion.follow.enable((applied && applied.position)
            || handle.target.position, {});
        } else {
          S.intrusion.follow.disable();
        }
      };
    }
    const intrusionLoadButton = $('intrusionLoadTargets');
    if (intrusionLoadButton) {
      intrusionLoadButton.onclick = () => loadIntrusionTargets().catch(
        (error) => setIntrusionNote(`读取保护体实例失败：${error.message}`, 'err'));
    }
    const intrusionPresetButton = $('intrusionPreset');
    if (intrusionPresetButton) {
      intrusionPresetButton.onclick = () => loadIntrusionPreset('crossing');
    }
    const intrusionAssessButton = $('intrusionAssess');
    if (intrusionAssessButton) {
      intrusionAssessButton.onclick = () => assessIntrusion().catch(
        (error) => setIntrusionNote(`判定失败：${error.message}`, 'err'));
    }
    const intrusionClearButton = $('intrusionClear');
    if (intrusionClearButton) intrusionClearButton.onclick = clearIntrusion;
    const intrusionTargetSelect = $('intrusionTarget');
    if (intrusionTargetSelect) {
      intrusionTargetSelect.onchange = () => {
        S.intrusion.selectedKey = intrusionTargetSelect.value;
        setIntrusionNote(intrusionTargetSelect.value
          ? '实例已选择；载入预设或手工输入轨迹节点后判定。'
          : '尚未选择保护体实例。');
      };
    }
    const simulatedNotice = $('simulatedNotice');
    if (simulatedNotice) simulatedNotice.textContent = INTRUSION_MODE_BANNER;
    renderIntrusionHud(null);
    renderIntrusionEvents(null);

    // 逐帧推进只调用控制器（时间驱动），**绝不**用回调累加判定事件是否发生。
    if (!S.intrusion.tickInstalled) {
      S.intrusion.tickInstalled = true;
      viewer.clock.onTick.addEventListener(() => {
        const controller = S.intrusion.controller;
        if (controller && controller.playing) controller.tick();
      });
    }

    // 供验收脚本使用的闭包出口（window.__CNS_3D__ 只做转发，不复制逻辑）。
    S.intrusion.api = {
      loadTargets: loadIntrusionTargets,
      preset: loadIntrusionPreset,
      assess: assessIntrusion,
      clear: clearIntrusion,
      focusEvent: focusIntrusionEvent,
      state: () => (S.intrusion.controller ? S.intrusion.controller.state : null),
      seek: (timeS) => (S.intrusion.controller
        ? S.intrusion.controller.scrub(Number(timeS)) : null),
      play: () => (S.intrusion.controller ? S.intrusion.controller.play() : null),
      pause: () => (S.intrusion.controller ? S.intrusion.controller.pause() : null),
      reset: () => (S.intrusion.controller ? S.intrusion.controller.reset() : null),
      setSpeed: (value) => (S.intrusion.controller
        ? S.intrusion.controller.setSpeed(value) : null),
      setFollow: (enabled, options) => {
        const follow = S.intrusion.follow;
        const handle = S.intrusion.handle;
        if (!follow || !handle) return false;
        const yes = Boolean(enabled && (typeof enabled === 'object' ? enabled.enabled : true));
        const row = (enabled && typeof enabled === 'object') ? enabled : (options || {});
        const input = $('intrusionFollow');
        if (input) input.checked = yes;
        if (yes) {
          const applied = applyIntrusionState(handle, S.intrusion.controller.state,
            S.intrusion.deps);
          follow.enable((applied && applied.position) || handle.target.position, {
            heading: Number.isFinite(row.headingDeg)
              ? Cesium.Math.toRadians(row.headingDeg) : undefined,
            pitch: Number.isFinite(row.pitchDeg)
              ? Cesium.Math.toRadians(row.pitchDeg) : undefined,
            range: Number.isFinite(row.rangeM) ? row.rangeM : undefined,
          });
        } else {
          follow.disable();
        }
        return true;
      },
      targets: () => (S.intrusion.targets || []).map((row) => ({
        key: row.key, mode: row.mode, render_instance_id: row.renderInstanceId,
        label: row.label,
      })),
    };

    const bindBase = (id, setter) => {
      const input = $(`ly_${id}`);
      if (input) input.onchange = () => {
        const applied = setter(input.checked);
        if (applied === false) input.checked = false;
        // 底图切换后必须刷新"当前底图"与版权来源标注（它们按**实际渲染**的图层生成）。
        if (typeof S.updateBasemapLabels === 'function') S.updateBasemapLabels();
      };
    };
    // 「高清卫星影像」勾选框**不是**第二个模式开关：它只是底图模式的可视化镜像，
    // 只读展示实际模式，用户只从上方双选开关切换。
    // （地形与注记仍是各自独立的图层开关，语义不变。）
    const imageryCheckbox = $('ly_imagery');
    if (imageryCheckbox) {
      imageryCheckbox.disabled = true;
      imageryCheckbox.title = '当前底图模式的只读镜像；请使用上方「底图模式」切换';
    }
    /** 把勾选框同步为**实际生效**的模式（在线失败回退时也要复位）。
     *
     * 判据取 ``basemapMode()``（三值）而不是 ``mode()``（二值）：后者对"离线晕渲"
     * 与"离线卫星影像"都返回 ``offline``，用它会让离线卫星影像时勾选框显示未勾选，
     * 与画面真实显示的底图不符。
     */
    const syncImageryCheckbox = () => {
      if (!imageryCheckbox) return;
      const mode = typeof S.basemap.basemapMode === 'function'
        ? S.basemap.basemapMode() : 'offline_relief';
      imageryCheckbox.checked = mode === 'online' || mode === 'offline_imagery';
    };
    const previousUpdateLabels = S.updateBasemapLabels;
    S.updateBasemapLabels = () => {
      if (typeof previousUpdateLabels === 'function') previousUpdateLabels();
      syncImageryCheckbox();
    };
    bindBase('terrain', (value) => S.basemap.setTerrainVisible(value));
    bindBase('labels', (value) => S.basemap.setLabelsVisible(value));
    // 影像图层完全不存在（在线未授权 **且** 本地离线影像不可用）：勾选框必须反映
    // 真实能力，不能显示成"已开启"。
    const imageryInput = imageryCheckbox;
    const anyImagery = (typeof S.basemap.imageryAvailable === 'function'
      && S.basemap.imageryAvailable())
      || (typeof S.basemap.offlineImageryAvailable === 'function'
        && S.basemap.offlineImageryAvailable());
    if (imageryInput && !anyImagery) {
      imageryInput.checked = false;
      imageryInput.disabled = true;
      imageryInput.title = '无可用卫星影像图层（在线未授权，且本地离线影像不可用）；'
        + '当前使用 FABDEM 地形晕渲，见上方「底图模式」';
    } else if (imageryInput) {
      imageryInput.checked = false;   // 默认离线：由 syncImageryCheckbox 按真实模式刷新
      imageryInput.title = '底图模式的只读镜像；请使用上方「底图模式」切换';
    }
    // A7-OBS-02：本构建没有创建地理注记 provider，勾选框必须**事先**显示为不可用并给出
    // 原因，而不是让用户勾选后什么也看不到（随后再被复位）。能力判定走 basemap 的唯一实现。
    const labelsInput = $('ly_labels');
    if (labelsInput && typeof S.basemap.labelsAvailable === 'function'
        && !S.basemap.labelsAvailable()) {
      labelsInput.checked = false;
      labelsInput.disabled = true;
      labelsInput.title = '本构建未启用在线地理注记图源（离线开发模式）；'
        + '此项不影响业务图层与地形判读';
    }
    // 切换业务图层**不得**重建 Cesium Viewer，也不得重新初始化卫星底图：
    // 下面的循环只调用 LazyLayerRegistry.setVisible（首次才请求几何）。
    S.viewerIdentity = viewer;

    /**
     * Round38-T3D4：切换航路 / 项目后清理旧图层与缓存。
     *
     * 需求明确要求"切换航路或项目后清理旧图层和缓存"。这里给出**唯一**入口：
     * 它删除专题图层实体、清空懒加载注册表与已缓存的业务数据，并把图层复选框
     * 复位为默认（项目打开默认只展示底图）。**不**重建 Viewer，也不重载底图。
     */
    S.clearBusinessLayers = (options = {}) => {
      const keepBase = options.keepBaseLayers !== false;
      for (const id of Object.keys(BUSINESS_LAYER_DEFAULTS)) {
        for (const entity of S.lazyLayers.entities(id)) {
          try { viewer.entities.remove(entity); } catch (_error) { /* 已移除 */ }
        }
      }
      S.lazyLayers = new LazyLayerRegistry(S.lazyLayerLoaders || {});
      for (const key of Object.keys(groups)) groups[key] = [];
      S.routes = null;
      S.points = null;
      S.coverage = null;
      S.radarGroups = null;
      S.protectionGroups = null;
      S.cnsSiteGroups = null;
      S.cnsSites = null;
      S.assumptionSites = null;
      S.fixedCorridor = null;
      S.fixedCorridorGroups = null;
      S.routeContextPromise = null;
      S.pointsPromise = null;
      S.towerGroupsPromise = null;
      for (const id of Object.keys(BUSINESS_LAYER_DEFAULTS)) {
        const input = $(`ly_${id}`);
        if (input) input.checked = Boolean(BUSINESS_LAYER_DEFAULTS[id]);
      }
      renderCnsSiteLegend();
      // A7-BAND-02：清理后回到"尚未请求"状态（`null`），不得断言"没有目标带"。
      renderFixedCorridorPanel(null, { groups: [] });
      S.stats.entities = viewer.entities.values.length;
      renderStatus({ quiet: true });
      if (!keepBase) return false;
      return true;
    };

    for (const id of Object.keys(BUSINESS_LAYER_DEFAULTS)) {
      // 模拟轨迹不是"服务端几何懒加载"图层：它的实体只由用户配置的轨迹产生，
      // 因此单独接线（见下方 ly_intrusion），不经过 LazyLayerRegistry。
      if (id === 'intrusion') continue;
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

    // 模拟轨迹图层的整体开关：只切换已有实体的 show，绝不请求几何、绝不重建 Viewer。
    const intrusionLayerInput = $('ly_intrusion');
    if (intrusionLayerInput) {
      intrusionLayerInput.checked = S.intrusion.layerWanted;
      intrusionLayerInput.onchange = () => {
        S.intrusion.layerWanted = intrusionLayerInput.checked;
        if (S.intrusion.handle) {
          setIntrusionVisible(S.intrusion.handle, intrusionLayerInput.checked);
          // 整体开关会重置所有实体的 show；随后按当前时间基准恢复剖面条的显隐。
          if (S.intrusion.controller) applyIntrusionFrame(S.intrusion.controller.state);
        } else if (intrusionLayerInput.checked) {
          setIntrusionNote('图层已开启，但还没有模拟轨迹：请选择保护体实例并'
            + '「开始判定并回放」。系统不会自动生成目标。');
        }
        S.stats.entities = viewer.entities.values.length;
        renderStatus();
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
    // 图层必须**按 provider URL 身份**识别，绝不能按固定索引取。
    // 实测缺陷：加入本地离线影像后共有三层（在线 / 离线 / 晕渲），而本探针原先按
    // index 0/1 取值，于是 relief_stats 实际读到的是**离线影像**图层——证据与现实
    // 不符，会让人误判"晕渲在渲染"或"影像计数不对"。
    const layerByUrl = (needle) => {
      if (!layers) return null;
      for (let i = 0; i < layers.length; i += 1) {
        const layer = layers.get(i);
        const url = layer && layer.imageryProvider && layer.imageryProvider.url;
        if (typeof url === 'string' && url.includes(needle)) return layer;
      }
      return null;
    };
    const onlineLayer = layerByUrl('imagery/tianditu_img_w')
      || layerByUrl('arcgis_world_imagery');
    const offlineLayer = layerByUrl('offline_zhoushan_imagery');
    const reliefLayerRef = layerByUrl('/relief/');
    return {
      tiles_to_render: surface && surface._tilesToRender ? surface._tilesToRender.length : null,
      tiles_loading: surface && surface._tilesToRender
        ? surface._tilesToRender.filter((t) => t && t.state === 1).length : null,
      imagery_layers: layers ? layers.length : null,
      //: 各底图图层**各自**的真实瓦片计数（在线 / 离线 / 晕渲，三者不可混用）。
      imagery_stats: imageryStats(onlineLayer),
      imagery_source_id: onlineLayer && onlineLayer.imageryProvider
        ? (String(onlineLayer.imageryProvider.url || '').includes('tianditu_img_w')
          ? 'tianditu_img_w' : 'arcgis_world_imagery') : null,
      imagery_visible: onlineLayer ? Boolean(onlineLayer.show) : null,
      offline_imagery_stats: imageryStats(offlineLayer),
      offline_imagery_visible: offlineLayer ? Boolean(offlineLayer.show) : null,
      relief_stats: imageryStats(reliefLayerRef),
      relief_visible: reliefLayerRef ? Boolean(reliefLayerRef.show) : null,
      terrain_provider: S.viewer.terrainProvider && S.viewer.terrainProvider.constructor
        ? S.viewer.terrainProvider.constructor.name : null,
      vertical_exaggeration: scene.verticalExaggeration,
      maximum_screen_space_error: globe ? globe.maximumScreenSpaceError : null,
      water_effect: globe ? globe.showWaterEffect : null,
      lighting: globe ? globe.enableLighting : null,
      fog: scene.fog ? scene.fog.enabled : null,
      sky_atmosphere: scene.skyAtmosphere ? scene.skyAtmosphere.show : null,
      globe_show: globe ? globe.show : null,
      entity_count: S.viewer.entities ? S.viewer.entities.values.length : null,
    };
  },
  cameraPresets() {
    return Object.fromEntries(Object.entries(CAMERA_PRESETS).map(
      ([id, row]) => [id, { ...row }]));
  },
  /** 验收辅助：把某个 Radar 面阵锚点投影为**屏幕像素**（只读，用于真实点击）。
   *
   * ``preview=true`` 时取设备参数示意预览的分组；否则取真实规划面阵分组。
   */
  radarAnchorScreenPosition(index = 0, preview = false) {
    if (!S.viewer || !S.viewer.scene) return { error: 'viewer_not_ready' };
    const groups = (preview ? S.radarPreviewGroups : S.radarGroups) || [];
    const group = groups[Number(index) || 0];
    if (!group) return { error: 'group_missing', group_count: groups.length };
    const anchor = group.entities && group.entities[0];
    if (!anchor || !anchor.position) return { error: 'anchor_position_missing' };
    const position = anchor.position.getValue(Cesium.JulianDate.now());
    if (!position) return { error: 'cartesian_unresolved' };
    const scene = S.viewer.scene;
    let pixel = null;
    let method = null;
    // ``Scene.prototype.cartesianToCanvasCoordinates`` 是 1.126 的稳定入口；
    // ``SceneTransforms`` 的函数名在版本间变过，故仅作回退。
    if (typeof scene.cartesianToCanvasCoordinates === 'function') {
      pixel = scene.cartesianToCanvasCoordinates(position);
      method = 'scene.cartesianToCanvasCoordinates';
    }
    if (!pixel && Cesium.SceneTransforms) {
      const transforms = Cesium.SceneTransforms;
      const toWindow = transforms.worldToWindowCoordinates
        || transforms.wgs84ToWindowCoordinates;
      if (typeof toWindow === 'function') {
        pixel = toWindow.call(transforms, scene, position);
        method = 'SceneTransforms';
      }
    }
    if (!pixel) return { error: 'projection_unavailable' };
    return {
      x: Math.round(pixel.x), y: Math.round(pixel.y), method,
      viewport: { width: scene.canvas.clientWidth, height: scene.canvas.clientHeight },
      panel_id: group.panel_id, preview: Boolean(group.preview),
    };
  },
  /** 验收辅助：飞到某个 Radar 面阵锚点的**固定俯视位姿**（只读）。 */
  flyToRadarPanel(index = 0, preview = false, heightM = 3000, pitchDeg = -30) {
    if (!S.viewer) return false;
    const groups = (preview ? S.radarPreviewGroups : S.radarGroups) || [];
    const group = groups[Number(index) || 0];
    const anchor = group && group.entities && group.entities[0];
    if (!anchor || !anchor.position) return false;
    const position = anchor.position.getValue(Cesium.JulianDate.now());
    if (!position) return false;
    const cartographic = Cesium.Cartographic.fromCartesian(position);
    if (!cartographic) return false;
    const longitude = Cesium.Math.toDegrees(cartographic.longitude);
    const latitude = Cesium.Math.toDegrees(cartographic.latitude);
    const height = Number(heightM) || 3000;
    const pitch = Number(pitchDeg) || -30;
    // 站位放在锚点**南侧**，距离按"视线中心落点"估算，保证锚点落在视口内：
    // 落点水平距离 ≈ height / tan(|pitch|)；取约 0.8 倍，避免锚点掉出屏幕下沿。
    const offsetDeg = (height / Math.tan(Math.abs(pitch) * Math.PI / 180)) * 0.8 / 111320;
    S.viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(
        longitude, latitude - offsetDeg, height),
      orientation: {
        heading: 0,
        pitch: Cesium.Math.toRadians(pitch),
        roll: 0,
      },
    });
    return true;
  },
  /** 验收辅助：在**屏幕像素**处做只读拾取探测（用于验证"可点击查看几何信息"）。
   *
   * 只回传"是否命中带 ``__cns`` 的实体 + 其标题/类型/行数"，绝不回传 viewer 本身。
   */
  pickProbe(x, y) {
    if (!S.viewer || !S.viewer.scene) return null;
    const picked = S.viewer.scene.pick(
      new Cesium.Cartesian2(Number(x) || 0, Number(y) || 0));
    const info = picked && picked.id ? picked.id.__cns : null;
    return {
      hit: Boolean(info),
      kind: info ? info.kind : null,
      title: info ? info.title : null,
      preview: info ? Boolean(info.preview) : false,
      row_count: info && info.rows ? info.rows.length : 0,
    };
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
  /** 验收辅助：底图模式开关的只读状态（模式 / 在线是否可用 / 真实原因 / 文案）。 */
  basemapModeState() {
    return typeof S.basemapModeState === 'function' ? S.basemapModeState() : null;
  },
  /** 验收辅助：三个底图图层各自的真实瓦片计数与来源（离线影像单列一项）。 */
  basemapLayerStats() {
    return S.basemap && typeof S.basemap.layerStats === 'function'
      ? S.basemap.layerStats() : null;
  },
  /** 验收辅助：切换底图模式（离线地形晕渲 / 离线卫星影像 / ArcGIS 在线卫星影像）。
   *
   * 这是**唯一**用于自动化验收的模式入口；它走与 UI 开关完全相同的
   * ``S.applyBasemapMode``，因此不可能绕过置灰与回退逻辑。
   */
  setBasemapMode(mode) {
    if (typeof S.applyBasemapMode !== 'function') return null;
    const target = ['online', 'offline_imagery'].includes(mode) ? mode : 'offline';
    return S.applyBasemapMode(target);
  },
  /** 验收辅助：Cesium Viewer 是否被**重建**过（切图层绝不允许重建 viewer）。 */
  viewerIdentityStable() {
    return Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer;
  },
  /** 验收辅助：读取**真实生效**的场景渲染效果开关（只读，不修改任何设置）。
   *
   * 用于诊断"卫星影像偏蓝发白"这类色彩异常：只有拿到真实开关值，才能把
   * 嫌疑项逐个消融验证，而不是靠猜。
   */
  sceneEffects() {
    const viewer = S.viewer;
    if (!viewer) return null;
    const globe = viewer.scene.globe;
    return {
      show_water_effect: globe.showWaterEffect,
      enable_lighting: globe.enableLighting,
      show_ground_atmosphere: globe.showGroundAtmosphere,
      fog_enabled: viewer.scene.fog ? viewer.scene.fog.enabled : null,
      base_color: globe.baseColor ? globe.baseColor.toCssColorString() : null,
      maximum_screen_space_error: globe.maximumScreenSpaceError,
      vertical_exaggeration: viewer.scene.verticalExaggeration,
      terrain_provider: viewer.terrainProvider ? viewer.terrainProvider.constructor.name : null,
      imagery_layer_count: viewer.imageryLayers.length,
    };
  },
  /** 验收辅助：**临时**切换场景效果用于色彩消融实验（只影响当前会话显示）。
   *
   * 它**不**改任何默认值、**不**写 ProjectState、**不**持久化；页面刷新即恢复。
   * 存在的唯一理由是让"是否 showWaterEffect 造成海面偏蓝发白"可被实验证伪。
   */
  setSceneEffect(name, value) {
    const viewer = S.viewer;
    if (!viewer) return false;
    const globe = viewer.scene.globe;
    const allowed = {
      show_water_effect: (v) => { globe.showWaterEffect = Boolean(v); },
      enable_lighting: (v) => { globe.enableLighting = Boolean(v); },
      show_ground_atmosphere: (v) => { globe.showGroundAtmosphere = Boolean(v); },
      fog_enabled: (v) => { if (viewer.scene.fog) viewer.scene.fog.enabled = Boolean(v); },
    };
    if (!allowed[name]) return false;
    allowed[name](value);
    return true;
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
                     rid: { total: 0, shown: 0 },
                     radar: { total: 0, shown: 0 },
                     radar_preview: { total: 0, shown: 0 },
                     protection: { total: 0, shown: 0 },
                     protection_preview: { total: 0, shown: 0 },
                     cns_sites: { total: 0, shown: 0 },
                     fixed_corridor: { total: 0, shown: 0 },
                     intrusion: { total: 0, shown: 0 },
                     intrusion_event: { total: 0, shown: 0 } };
    for (const entity of S.viewer.entities.values) {
      const info = entity.__cns;
      if (!info) continue;
      const bucket = info.kind === 'communication-volume' ? result.communication
        : (info.kind === 'rid-footprint' ? result.rid
          : (info.kind === 'radar-panel'
            ? (info.preview ? result.radar_preview : result.radar)
            : (info.kind === 'p17-protection' ? result.protection
              : (info.kind === 'protection-preview' ? result.protection_preview
                : ((info.kind === 'cns-site' || info.kind === 'tower-structure'
                    || info.kind === 'cns-device' || info.kind === 'cns-planned-device')
                  ? result.cns_sites
                  : ((info.kind === 'fixed-corridor-band'
                      || info.kind === 'fixed-corridor-layer-slice')
                    ? result.fixed_corridor
                    : (info.kind === 'simulated-intrusion-event' ? result.intrusion_event
                      : (info.kind && info.kind.startsWith('simulated-intrusion')
                        ? result.intrusion : null))))))));
      if (!bucket) continue;
      bucket.total += 1;
      if (entity.show) bucket.shown += 1;
    }
    return result;
  },
  /** 验收辅助：P16-V2 假设站址图层的只读证据（默认关闭、非正式设备）。 */
  assumptionSiteStats() {
    const payload = S.assumptionSites;
    const entities = (S.lazyLayers && S.lazyLayers.entities('assumptionSites')) || [];
    return {
      payload: payload ? {
        status: payload.status,
        reason: payload.reason,
        source_task_id: payload.source_task_id,
        scenario_id: payload.scenario_id,
        mount_height_agl_m: payload.mount_height_agl_m,
        assumption_only: payload.assumption_only,
        formal_equipment: payload.formal_equipment,
        canonical_write: payload.canonical_write,
        count: payload.count,
      } : null,
      entity_count: entities.length,
      shown_entity_count: entities.filter((entity) => entity.show).length,
      layer_checked: Boolean(($('ly_assumptionSites') || {}).checked),
    };
  },

  /** 验收辅助：CNS 站址 / 设备身份的只读证据（四类分层 + 高度证据完整性）。 */
  cnsSiteStats() {
    const payload = S.cnsSites;
    const groups = S.cnsSiteGroups;
    return {
      payload: payload ? {
        status: payload.status,
        revision: payload.revision,
        read_only: payload.read_only,
        writes_performed: payload.writes_performed,
        algorithms_invoked: payload.algorithms_invoked,
        count: payload.count,
        counts: payload.counts,
        identity_rules: payload.identity_rules,
        shared_site_source: payload.shared_site_source,
        radar_site_input_status: payload.radar_site_input_status,
        service_categories: payload.service_categories,
        blocked_site_reasons: (payload.blocked_sites || []).map(
          (row) => ({ site_id: row.site_id, reason: row.block_reason,
                      required_evidence: row.required_evidence })),
      } : null,
      entity_counts: {
        site_anchors: groups ? groups.entities.length : 0,
        towers_with_structure: groups ? groups.towers.length : 0,
        device_markers: groups ? groups.devices.length : 0,
        planned_devices: groups ? groups.plans.length : 0,
      },
      diagnostics: S.cnsSiteDiagnostics || null,
      viewer_identity_stable: Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer,
    };
  },
  /** 验收辅助：起降点 PNG billboard 的只读证据（同源资源 / 身份 / 绘制数量）。 */
  landingPadStats() {
    const records = (S.landingPadGroups && S.landingPadGroups.records) || [];
    const entities = (S.groups && S.groups.landingPads) || [];
    const kinds = {};
    const roles = {};
    for (const row of records) {
      kinds[row.kind] = (kinds[row.kind] || 0) + 1;
      roles[row.role] = (roles[row.role] || 0) + 1;
    }
    const sampleEntity = entities[0] || null;
    const billboard = sampleEntity && sampleEntity.billboard;
    const image = billboard && billboard.image
      && typeof billboard.image.getValue === 'function'
      ? billboard.image.getValue() : (billboard ? billboard.image : null);
    return {
      icon: LANDING_PAD_ICON_URL,
      payload_status: S.landingPads ? S.landingPads.status : null,
      candidate_count: S.landingPads ? (S.landingPads.candidates || []).length : null,
      count: records.length,
      entities: entities.length,
      kinds,
      roles,
      diagnostics: S.landingPadDiagnostics || null,
      sample: sampleEntity ? {
        id: sampleEntity.id,
        image: typeof image === 'string' ? image : String(image),
        image_complete: Boolean(image && image.complete),
        image_natural_width: image ? image.naturalWidth : null,
        billboard_width: billboard && billboard.width
          ? billboard.width.getValue() : null,
        billboard_height: billboard && billboard.height
          ? billboard.height.getValue() : null,
        position_show: Boolean(sampleEntity.show),
      } : null,
    };
  },
  /** 验收辅助：固定 1000 m 保障目标带的只读证据（currentness + 身份 + 绘制性质）。 */
  fixedCorridorStats() {
    const payload = S.fixedCorridor;
    return {
      payload: payload ? {
        status: payload.status,
        read_only: payload.read_only,
        writes_performed: payload.writes_performed,
        band_count: payload.band_count,
        width_contract: payload.width_contract,
        geometry_source: payload.geometry_source,
        corridor_effective_status: payload.round38_corridor_effective_status,
        identity_rules: payload.identity_rules,
        bands: (payload.bands || []).map((band) => ({
          route_id: band.route_id,
          currentness: band.currentness,
          currentness_reason: (band.currentness_entry || {}).reason || null,
          drawable_as_current_band: band.drawable_as_current_band,
          preview_allowed: band.preview_allowed,
          preview_authority: band.preview_authority,
          geometry_status: band.geometry_status,
          geometry_fingerprint: band.geometry_fingerprint,
          route_geometry_fingerprint: band.route_geometry_fingerprint,
          route_authority: band.route_authority,
          width_policy_version: band.width_policy_version,
          corridor_half_width_m: band.corridor_half_width_m,
          corridor_total_width_m: band.corridor_total_width_m,
          vertical_extent_status: band.vertical_extent_status,
          altitude_layer_id: band.altitude_layer_id,
          nominal_altitude_m: band.nominal_altitude_m,
          may_claim_3d_certified_volume: band.may_claim_3d_certified_volume,
        })),
      } : null,
      groups: (S.fixedCorridorGroups || []).map((group) => ({
        route_id: group.route_id,
        authority: group.authority,
        currentness: group.currentness,
        vertical_extent_status: group.vertical_extent_status,
        entity_count: group.entities.length,
        visible: group.visible,
        shown: group.entities.filter((entity) => entity.show).length,
      })),
      diagnostics: S.fixedCorridorDiagnostics || null,
      viewer_identity_stable: Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer,
    };
  },
  setFixedCorridorGroupVisible(index, value) {
    const group = (S.fixedCorridorGroups || [])[Number(index) || 0];
    if (!group) return false;
    group.visible = Boolean(value);
    const layerInput = $('ly_fixedCorridor');
    for (const entity of group.entities) {
      entity.show = Boolean(layerInput && layerInput.checked && group.visible);
    }
    const input = $(`fixed_band_${Number(index) || 0}`);
    if (input) input.checked = group.visible;
    return true;
  },
  /** 验收辅助：按**唯一入口**清理业务图层与缓存（切换航路/项目后必须调用）。 */
  clearBusinessLayers() {
    return typeof S.clearBusinessLayers === 'function'
      ? S.clearBusinessLayers() : false;
  },
  /** 验收辅助：P17 历史/冲突与工程示意的隔离证据。 */
  protectionStats() {
    const groupsOf = (groups) => (groups || []).map((group) => ({
      render_instance_id: group.render_instance_id || null,
      object_id: group.object_id,
      route_id: group.route_id,
      route_authority: group.route_authority || null,
      status: group.status,
      historical: group.historical,
      preview: group.preview,
      entity_count: group.entities.length,
      visible: group.visible,
      shown: group.entities.filter((entity) => entity.show).length,
    }));
    return {
      project: S.protection || null,
      preview: S.protectionPreview || null,
      project_groups: groupsOf(S.protectionGroups),
      preview_groups: groupsOf(S.protectionPreviewGroups),
      diagnostics: S.protectionDiagnostics || null,
      viewer_identity_stable: Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer,
    };
  },
  /** 验收辅助：只读的实例身份列表（不含几何，避免把大坐标串带进证据）。 */
  protectionInstances() {
    const payload = S.protection || null;
    return {
      schema_version: payload ? payload.schema_version : null,
      mode: payload ? payload.mode : null,
      status: payload ? payload.status : null,
      revision: payload ? payload.revision : null,
      render_instance_ids: payload ? payload.render_instance_ids || [] : [],
      render_instance_id_unique: payload ? payload.render_instance_id_unique : null,
      duplicate_render_instance_ids: payload ? payload.duplicate_render_instance_ids || [] : [],
      instance_identity_rule: payload ? payload.instance_identity_rule || null : null,
      volumes: ((payload || {}).volumes || []).map((row) => ({
        render_instance_id: row.render_instance_id,
        object_id: row.object_id,
        route_id: row.route_id,
        route_authority: row.route_authority,
        status: row.status,
        geometry_status: (row.geometry || {}).status,
        geometry_dimension: row.geometry_dimension,
        vertical_extent_status: row.vertical_extent_status,
        protection_geometry_identity: row.protection_geometry_identity || null,
        route_geometry_fingerprint: ((row.route_geometry_identity || {}).fingerprint) || null,
        p17_route_geometry_fingerprint:
          ((row.p17_route_geometry_identity || {}).fingerprint) || null,
        eligible_for_current_warning: row.eligible_for_current_warning === undefined
          ? null : row.eligible_for_current_warning,
      })),
      intrusion_contract: payload && payload.intrusion_contract
        ? {
          mode: payload.intrusion_contract.mode,
          status: payload.intrusion_contract.status,
          reason: payload.intrusion_contract.reason,
          simulated: payload.intrusion_contract.simulated,
          surveillance_status:
            (payload.intrusion_contract.surveillance_detection || {}).status,
          protection_geometry_statuses:
            payload.intrusion_contract.protection_geometry_statuses || [],
        } : null,
      viewer_identity_stable: Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer,
    };
  },
  /** 开关某个保护体实例；优先按**实例键**，数字下标仅为 3A 验收脚本兼容。 */
  setProtectionGroupVisible(target, value) {
    if (!S.protectionApi) return false;
    return S.protectionApi.setGroupVisible(target, value);
  },
  /** 验收辅助：模拟轨迹回放的只读证据（不含几何）。 */
  intrusionStats() {
    const model = S.intrusion.model;
    const controller = S.intrusion.controller;
    const state = S.intrusion.api && S.intrusion.api.state
      ? S.intrusion.api.state() : null;
    const describeRows = state ? describeState(state) : null;
    return {
      configured: Boolean(model),
      evaluation_revision: S.intrusion.evaluationRevision,
      render_instance_id: model ? model.renderInstanceId : null,
      track_id: model ? model.trackId : null,
      simulated: model ? model.simulated : null,
      eligible_for_current_warning: model ? model.eligibleForCurrentWarning : null,
      duration_s: model ? model.durationS : null,
      node_count: model ? model.nodes.length : 0,
      event_count: model ? model.events.length : 0,
      events: model ? model.events.map((row) => ({
        scope: row.scope, kind: row.kind, time_s: row.timeS,
        longitude: row.longitude, latitude: row.latitude,
        altitude_egm2008_m: row.altitudeEgm2008M,
        crossing: row.crossing, crossing_basis: row.crossingBasis,
      })) : [],
      horizontal_status: model ? model.horizontal.status : null,
      volume_status: model ? model.intrusion.status : null,
      volume_reason: model ? model.intrusion.reason : null,
      surveillance_status: model ? model.detection.status : null,
      playing: controller ? controller.playing : false,
      speed: controller ? controller.speed : null,
      time_s: state ? state.timeS : null,
      position: state ? state.position : null,
      horizontal_inside: state ? state.horizontal.inside : null,
      volume_inside: state ? state.volume.inside : null,
      labels: describeRows,
      banner: INTRUSION_MODE_BANNER,
      entity_count: S.intrusion.handle ? S.intrusion.handle.entities.length : 0,
      event_entity_count: S.intrusion.handle ? S.intrusion.handle.eventEntities.length : 0,
      trail_status: S.intrusion.handle ? S.intrusion.handle.diagnostics.trail_status : null,
      volume_bar_shown: Boolean(S.intrusion.handle && S.intrusion.handle.bar
        && S.intrusion.handle.bar.show),
      camera_following: Boolean(S.intrusion.follow && S.intrusion.follow.following),
      geoid_window: S.intrusion.geoidWindow || null,
      targets: S.intrusion.api ? S.intrusion.api.targets() : [],
      isolation: model ? model.isolation : null,
      viewer_identity_stable: Boolean(S.viewerIdentity) && S.viewerIdentity === S.viewer,
    };
  },
  /** 验收辅助：与界面按钮等价的模拟轨迹操作（不复制任何判定逻辑）。 */
  intrusion(action, argument, extra) {
    const api = S.intrusion.api;
    if (!api) return null;
    if (action === 'loadTargets') return api.loadTargets();
    if (action === 'preset') return api.preset(argument || 'crossing');
    if (action === 'assess') return api.assess();
    if (action === 'clear') return api.clear();
    if (action === 'play') return api.play();
    if (action === 'pause') return api.pause();
    if (action === 'reset') return api.reset();
    if (action === 'seek') return api.seek(argument);
    if (action === 'speed') return api.setSpeed(argument);
    if (action === 'follow') return api.setFollow(argument, extra);
    if (action === 'focusEvent') return api.focusEvent(Number(argument));
    if (action === 'state') return api.state();
    return null;
  },
  /** 验收辅助：直接设置输入框里的轨迹节点（用户配置路径的等价入口）。 */
  setIntrusionTrack(nodes) {
    const textarea = $('intrusionTrack');
    if (!textarea) return { status: 'not_ready' };
    textarea.value = JSON.stringify(nodes || []);
    return { status: 'set', node_count: (nodes || []).length };
  },
  /** 验收辅助：Radar 图层的**只读**证据（真实面阵 + 隔离的设备参数示意）。 */
  radarStats() {
    const radar = S.coverage ? S.coverage.radar : null;
    const preview = S.radarPreview || null;
    const groupsOf = (groups) => (groups || []).map((group) => ({
      panel_id: group.panel_id,
      radar_type: group.radar_type,
      azimuth_deg: group.azimuth_deg,
      status: group.status,
      reason: group.reason,
      origin_confirmed: group.origin_confirmed,
      preview: group.preview,
      entity_count: group.entities.length,
      visible: group.visible,
      shown: group.entities.filter((entity) => entity.show).length,
    }));
    return {
      project: radar ? {
        mode: radar.mode,
        status: radar.status,
        coverage_semantics: radar.coverage_semantics,
        geometry_model: radar.geometry_model,
        panels_total: radar.panels_total,
        geometry_count: radar.geometry_count,
        blocked_count: radar.blocked_count,
        required_evidence: radar.required_evidence,
        forbidden_geometry: radar.forbidden_geometry,
        not_a_rid_footprint: radar.not_a_rid_footprint,
        omnidirectional: radar.omnidirectional,
        los: radar.los,
        geometry_semantics_separation: radar.geometry_semantics_separation,
        project_layout: radar.project_layout,
        panels: (radar.volumes || []).map((row) => ({
          panel_id: row.panel_id,
          tower_id: row.tower_id,
          radar_type: row.radar_type,
          azimuth_deg: row.azimuth_deg,
          longitude: row.longitude,
          latitude: row.latitude,
          origin_status: row.origin_status,
          origin_egm2008_m: row.service_origin_egm2008_m,
          status: (row.geometry || {}).status,
          reason: (row.geometry || {}).reason,
          min_slant_range_m: (row.geometry || {}).min_slant_range_m || null,
          max_slant_range_m: (row.geometry || {}).max_slant_range_m || null,
        })),
        groups: groupsOf(S.radarGroups),
      } : null,
      //: 预览：**独立**证据块（不与真实规划面阵合并、不计入覆盖统计）。
      preview: preview ? {
        mode: preview.mode,
        status: preview.status,
        geometry_count: preview.geometry_count,
        disclaimer: preview.disclaimer,
        isolation: preview.isolation,
        preview_origin_status: preview.preview_origin_status,
        preview_origin_confirmed: preview.preview_origin_confirmed,
        preview_origin_acknowledged_by_user: preview.preview_origin_acknowledged_by_user,
        preview_selection: preview.preview_selection,
        volumes: (preview.volumes || []).map((row) => ({
          panel_id: row.panel_id,
          radar_type: row.radar_type,
          azimuth_deg: row.azimuth_deg,
          longitude: row.longitude,
          latitude: row.latitude,
          azimuth_window: row.azimuth_window,
          status: (row.geometry || {}).status,
          elevation_min_deg: (row.geometry || {}).elevation_min_deg,
          elevation_max_deg: (row.geometry || {}).elevation_max_deg,
          min_slant_range_m: (row.geometry || {}).min_slant_range_m,
          max_slant_range_m: (row.geometry || {}).max_slant_range_m,
          cross_section: (row.geometry || {}).cross_section,
          origin_confirmed: (row.geometry || {}).origin_confirmed,
        })),
        groups: groupsOf(S.radarPreviewGroups),
      } : null,
      diagnostics: S.radarDiagnostics || null,
      entity_counts: {
        radar: S.groups && S.groups.radar ? S.groups.radar.length : 0,
        radar_preview: S.groups && S.groups.radarPreview
          ? S.groups.radarPreview.length : 0,
      },
    };
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
  flightProtectionResearchStats() {
    const rows = S.groups?.flightProtectionResearch || [];
    return { total: rows.length, shown: rows.filter((e) => e.show).length,
      status: S.flightProtectionResearch?.status || 'unloaded' };
  },
  towerIconStats() {
    const known = S.groups?.towersKnown || [];
    const unknown = S.groups?.towersUnknown || [];
    const all = known.concat(unknown);
    const images = new Set();
    let billboards = 0;
    for (const entity of all) {
      const billboard = entity && entity.billboard;
      if (billboard && billboard.image !== undefined) billboards += 1;
      const image = billboard && billboard.image
        && typeof billboard.image.getValue === 'function'
        ? billboard.image.getValue() : billboard && billboard.image;
      if (typeof image === 'string') images.add(image);
    }
    return {
      total: all.length,
      known: known.length,
      unknown: unknown.length,
      billboards,
      images: [...images],
      unified_icon: images.size > 0 && [...images].every((url) => url.includes('tower_qinglv')),
      //: R7-D3C：known 的语义是“塔顶高度已解析、尚未权威确认”，不是“已确认塔顶”。
      known_semantics: 'tower_top_height_resolved_unconfirmed',
      unknown_semantics: 'tower_top_unresolved',
    };
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
        radar_entities: S.groups && S.groups.radar ? S.groups.radar.length : 0,
        radar_groups: (S.radarGroups || []).length,
        radar_preview_entities: S.groups && S.groups.radarPreview
          ? S.groups.radarPreview.length : 0,
      },
      stats: S.stats,
      // 底图**实测**状态（含 imagery_failures / imagery_last_error / discards）：验收
      // 脚本据此断言"影像真的出图了"，而不是只看 manifest 的 configured_unverified。
      imagery: S.basemap ? S.basemap.status() : null,
      // 底图**模式**开关的只读证据（离线开发 / 在线演示）。只反映背景影像加载模式，
      // 与任何 CNS 业务证据的 currentness 或正式规划有效性无关。
      basemap_mode: typeof S.basemapModeState === 'function' ? S.basemapModeState() : null,
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
        radar: S.coverage.radar ? {
          mode: S.coverage.radar.mode,
          status: S.coverage.radar.status,
          coverage_semantics: S.coverage.radar.coverage_semantics,
          panels_total: S.coverage.radar.panels_total,
          geometry_count: S.coverage.radar.geometry_count,
          blocked_count: S.coverage.radar.blocked_count,
          required_evidence: S.coverage.radar.required_evidence,
          los: S.coverage.radar.los,
          forbidden_geometry: S.coverage.radar.forbidden_geometry,
          project_layout: S.coverage.radar.project_layout,
        } : null,
      } : null,
      // 设备参数示意预览：**独立**证据块，绝不与真实规划面阵合并。
      radar_preview: S.radarPreview ? {
        mode: S.radarPreview.mode,
        status: S.radarPreview.status,
        geometry_count: S.radarPreview.geometry_count,
        disclaimer: S.radarPreview.disclaimer,
        isolation: S.radarPreview.isolation,
        preview_origin_confirmed: S.radarPreview.preview_origin_confirmed,
        preview_origin_status: S.radarPreview.preview_origin_status,
      } : null,
      checks: S.checks,
      errors: S.errors,
      log: logLines.slice(-40),
    };
  },
};

boot();
