/** C3-C · R0005 Cesium 三维只读视图。
 *
 * 数据来源只有一份：已发布 run manifest 的三维投影
 * (`/api/research/runs/<id>/visualization-3d`)。本文件不做任何业务计算：
 * 不推导覆盖结论、不改写状态、不重新求解。航路高度由服务端按
 * `h = H_EGM2008 + N` 逐点换算后下发，前端只负责定位与绘制。
 */
import { createLocalViewer } from './visualization3d/basemap.js';

const $ = id => document.getElementById(id);
const state = {
  token: '', boot: null, manifest: null, payload: null, activeRun: null,
  viewer: null, handler: null, meta: new Map(), groups: {},
  terrainProvider: null, basemapLayer: null, ready: false, terrainCalls: 0,
  terrainLevels: [], terrainError: null, labelEntities: [],
};

const COLORS = {
  route: Cesium.Color.fromCssColorString('#3f7fb2'),
  routeCore: Cesium.Color.fromCssColorString('#dcecf6'),
  host: Cesium.Color.fromCssColorString('#1f6f8b'),
  planned: Cesium.Color.fromCssColorString('#e08a1e'),
  deviceC: Cesium.Color.fromCssColorString('#22b58c'),
  deviceN: Cesium.Color.fromCssColorString('#ac8be8'),
  deviceS: Cesium.Color.fromCssColorString('#57c28a'),
  n: Cesium.Color.fromCssColorString('#ac8be8'),
  C: Cesium.Color.fromCssColorString('#1a9c4a'),
  RID: Cesium.Color.fromCssColorString('#e8720c'),
  S: Cesium.Color.fromCssColorString('#8a9aa6'),
  Radar: Cesium.Color.fromCssColorString('#c62828'),
};

const LAYER_LABEL = {
  C: 'C 通信', RID: 'RID 合作', Radar: 'Radar 非合作', S: 'S 监视综合', N: 'N 导航动作',
};

function shortSha(value, length = 12) {
  const text = value == null ? 'unknown' : String(value);
  return text.length <= length ? text : `${text.slice(0, length)}…`;
}

function toast(message, tone = '') {
  const node = $('toast');
  node.textContent = message;
  node.className = `research-toast ${tone}`;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 5200);
}

function pill(node, text, tone = '') {
  if (!node) return;
  node.textContent = text;
  node.className = `research-pill ${tone}`;
}

function connection(text, tone = '') {
  const node = $('connection');
  node.textContent = text;
  node.className = `research-connection ${tone}`;
}

async function api(path) {
  const response = await fetch(path, {headers: {'X-CNS-Token': state.token}});
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    const detail = body?.message || body?.error || `${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return {body, headers: response.headers};
}

// --------------------------------------------------------------------- 地形网格

function decodeFloat32(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Float32Array(bytes.buffer);
}

function sampleTerrain(terrain, longitude, latitude) {
  if (!terrain || terrain.status !== 'available' || !terrain.values) return null;
  const [west, south, east, north] = terrain.extent;
  if (longitude < west || longitude > east || latitude < south || latitude > north) return null;
  const {width, height, values} = terrain;
  const x = (longitude - west) / (east - west) * (width - 1);
  const y = (north - latitude) / (north - south) * (height - 1);
  const x0 = Math.floor(x), y0 = Math.floor(y);
  const x1 = Math.min(x0 + 1, width - 1), y1 = Math.min(y0 + 1, height - 1);
  const fx = x - x0, fy = y - y0;
  const at = (column, row) => values[row * width + column];
  return at(x0, y0) * (1 - fx) * (1 - fy) + at(x1, y0) * fx * (1 - fy)
    + at(x0, y1) * (1 - fx) * fy + at(x1, y1) * fx * fy;
}

function terrainData(terrain) {
  if (!terrain || terrain.status !== 'available') return null;
  terrain.values = decodeFloat32(terrain.heights_ellipsoidal_m_b64);
  return terrain;
}

//: Cesium HeightmapTerrainData 的行序（北极 → 南极）。若运行时探针发现反了会翻一次。
let terrainRowsNorthFirst = true;

function addBasemap(payload) {
  const terrain = payload.terrain || {};
  if (!state.viewer || !terrain.basemap_png_b64) return;
  if (state.basemapLayer) {
    try { state.viewer.imageryLayers.remove(state.basemapLayer, true); } catch { /* noop */ }
    state.basemapLayer = null;
  }
  const provider = new Cesium.SingleTileImageryProvider({
    url: `data:image/png;base64,${terrain.basemap_png_b64}`,
    rectangle: Cesium.Rectangle.fromDegrees(...terrain.extent),
    tileWidth: terrain.basemap_width || 1024,
    tileHeight: terrain.basemap_height || 1024,
  });
  state.basemapLayer = state.viewer.imageryLayers.addImageryProvider(provider);
  state.basemapLayer.show = true;
  state.viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#10344a');
}

function buildTerrainProvider(terrain) {
  if (!terrain || terrain.status !== 'available') return new Cesium.EllipsoidTerrainProvider();
  const tilingScheme = new Cesium.GeographicTilingScheme();
  return new Cesium.CustomHeightmapTerrainProvider({
    width: 33,
    height: 33,
    tilingScheme,
    levelZeroMaximumGeometricError: 750.0,
    callback: (x, y, level) => {
      state.terrainCalls += 1;
      if (state.terrainLevels.length < 40) state.terrainLevels.push(`${level}:${x},${y}`);
      try {
        const rectangle = tilingScheme.tileXYToRectangle(x, y, level);
        const size = 33;
        const buffer = new Float32Array(size * size);
        const west = Cesium.Math.toDegrees(rectangle.west);
        const east = Cesium.Math.toDegrees(rectangle.east);
        const south = Cesium.Math.toDegrees(rectangle.south);
        const north = Cesium.Math.toDegrees(rectangle.north);
        for (let row = 0; row < size; row += 1) {
          const fraction = row / (size - 1);
          const latitude = terrainRowsNorthFirst
            ? north - (north - south) * fraction
            : south + (north - south) * fraction;
          for (let column = 0; column < size; column += 1) {
            const longitude = west + (east - west) * (column / (size - 1));
            const value = sampleTerrain(terrain, longitude, latitude);
            buffer[row * size + column] = value == null ? 0 : value;
          }
        }
        // CustomHeightmapTerrainProvider 期望回调返回**高度数组**；它自己包装成
        // HeightmapTerrainData。返回 HeightmapTerrainData 会被再次包成 buffer 而失效。
        return buffer;
      } catch (error) {
        state.terrainError = String(error);
        return new Float32Array(33 * 33);
      }
    },
  });
}

// --------------------------------------------------------------------- viewer

function buildViewer() {
  const viewer = createLocalViewer($('cesiumContainer'), {
    baseLayer: false,
    animation: false,
    timeline: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    baseLayerPicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    infoBox: false,
    selectionIndicator: false,
    shouldAnimate: false,
    requestRenderMode: false,
  });
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#0b2230');
  viewer.scene.globe.enableLighting = false;
  viewer.scene.globe.showGroundAtmosphere = true;
  viewer.scene.globe.depthTestAgainstTerrain = true;
  viewer.scene.globe.maximumScreenSpaceError = 2.0;
  viewer.scene.skyAtmosphere.show = true;
  viewer.scene.fog.enabled = false;
  viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#071421');
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 120;
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = 80000;
  viewer.scene.screenSpaceCameraController.enableTilt = true;
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(122.23, 29.84, 16000),
    orientation: {heading: Cesium.Math.toRadians(12), pitch: Cesium.Math.toRadians(-68), roll: 0},
  });
  let lastDeclutter = 0;
  viewer.scene.postRender.addEventListener(() => {
    const now = performance.now();
    if (now - lastDeclutter < 320) return;
    lastDeclutter = now;
    declutterLabels();
  });
  return viewer;
}

// --------------------------------------------------------------------- 实体

function clearScene() {
  if (!state.viewer) return;
  state.viewer.entities.removeAll();
  state.meta.clear();
  state.labelEntities = [];
  state.groups = {route: [], sites: [], devices: [], N: [], C: [], RID: [], S: []};
}

function addEntity(entity, group, meta) {
  state.viewer.entities.add(entity);
  const id = entity.id;
  state.meta.set(id, meta);
  if (entity.label) state.labelEntities.push(entity);
  (state.groups[group] = state.groups[group] || []).push(entity);
  return entity;
}

function declutterLabels() {
  if (!state.viewer || !state.labelEntities.length) return;
  const scene = state.viewer.scene;
  const now = state.viewer.clock.currentTime;
  const width = scene.canvas.clientWidth;
  const height = scene.canvas.clientHeight;
  const valueOf = (property, time) => (property && typeof property.getValue === 'function'
    ? property.getValue(time) : property);
  const candidates = [];
  for (const entity of state.labelEntities) {
    if (!entity.show || !entity.position) continue;
    entity.label.show = true;
    const position = valueOf(entity.position, now);
    if (!position) continue;
    const windowPoint = Cesium.SceneTransforms.worldToWindowCoordinates(scene, position);
    if (!windowPoint) continue;
    const text = String(valueOf(entity.label.text, now) ?? '');
    const offset = valueOf(entity.label.pixelOffset, now) || new Cesium.Cartesian2(0, 0);
    const x = windowPoint.x + offset.x;
    const y = windowPoint.y + offset.y;
    if (x < -60 || x > width + 60 || y < -40 || y > height + 40) continue;
    const boxWidth = Math.min(300, text.length * 7.1 + 14);
    const boxHeight = 19;
    const verticalOrigin = valueOf(entity.label.verticalOrigin, now);
    const verticalTop = verticalOrigin === Cesium.VerticalOrigin.TOP;
    const top = verticalTop ? y : y - boxHeight;
    const meta = state.meta.get(entity.id) || {};
    candidates.push({
      entity,
      priority: meta.priority ?? 50,
      box: {left: x - boxWidth / 2, right: x + boxWidth / 2, top, bottom: top + boxHeight},
    });
  }
  candidates.sort((left, right) => right.priority - left.priority);
  const placed = [];
  for (const candidate of candidates) {
    const hit = placed.some(box => !(
      candidate.box.right < box.left || candidate.box.left > box.right
      || candidate.box.bottom < box.top || candidate.box.top > box.bottom));
    if (hit) candidate.entity.label.show = false;
    else placed.push(candidate.box);
  }
}

function shortSiteLabel(site) {
  if (site.kind === 'planned_new_site') {
    const parts = String(site.site_id || '').split('-');
    return parts.length >= 2 ? `规划 ${parts[0]}-${parts[1]}` : `规划 ${site.site_id}`;
  }
  const id = String(site.site_id || '');
  if (id.startsWith('takeoff_landing_site:')) return `起降点 · ${id.split(':').pop()}`;
  if (id.startsWith('tower-colocation:')) return `塔共址 · …${id.slice(-4)}`;
  return id;
}

function siteGround(payload, site) {
  const ground = site.ground || {};
  return {
    ellipsoidal: Number.isFinite(ground.terrain_ellipsoidal_m)
      ? ground.terrain_ellipsoidal_m : 0,
    orthometric: Number.isFinite(ground.terrain_orthometric_m)
      ? ground.terrain_orthometric_m : null,
    undulation: Number.isFinite(ground.undulation_m) ? ground.undulation_m : null,
  };
}

function deviceColor(subsystem) {
  return subsystem === 'N' ? COLORS.deviceN : subsystem === 'S' ? COLORS.deviceS : COLORS.deviceC;
}

function addRoute(payload) {
  const points = payload.route.points || [];
  if (points.length < 2) return;
  const positions = points.map(point => Cesium.Cartesian3.fromDegrees(
    point.longitude, point.latitude, point.altitude_ellipsoidal_m));
  addEntity({
    id: 'route:' + payload.route.route_id,
    polyline: {
      positions,
      width: 7,
      material: new Cesium.PolylineOutlineMaterialProperty({
        color: COLORS.route, outlineColor: Cesium.Color.WHITE.withAlpha(0.65), outlineWidth: 2,
      }),
      arcType: Cesium.ArcType.GEODESIC,
    },
    properties: {kind: 'route'},
  }, 'route', {kind: 'route'});

  const groundHeights = points.map(point => {
    const value = sampleTerrain(state.payload.terrain, point.longitude, point.latitude);
    return value == null ? 0 : value;
  });
  const wallPositions = points.map(point => Cesium.Cartesian3.fromDegrees(
    point.longitude, point.latitude));
  addEntity({
    id: 'route-wall:' + payload.route.route_id,
    wall: {
      positions: wallPositions,
      minimumHeights: groundHeights,
      maximumHeights: points.map(point => point.altitude_ellipsoidal_m),
      material: COLORS.route.withAlpha(0.22),
      outline: false,
    },
  }, 'route', {kind: 'route_wall'});

  const start = points[0];
  const end = points[points.length - 1];
  addEntity({
    id: 'route-label:start',
    position: Cesium.Cartesian3.fromDegrees(
      start.longitude, start.latitude, start.altitude_ellipsoidal_m + 30),
    label: {
      text: 'R0005 起点',
      font: '11px "Segoe UI", "Microsoft YaHei", sans-serif',
      fillColor: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.fromCssColorString('#0b2230'),
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, 20),
      verticalOrigin: Cesium.VerticalOrigin.TOP,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
      distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 8000),
    },
  }, 'route', {kind: 'route_label', point: start, payload, priority: 70});
  addEntity({
    id: 'route-label:end',
    position: Cesium.Cartesian3.fromDegrees(
      end.longitude, end.latitude, end.altitude_ellipsoidal_m + 30),
    label: {
      text: 'R0005 终点',
      font: '11px "Segoe UI", "Microsoft YaHei", sans-serif',
      fillColor: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.fromCssColorString('#0b2230'),
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -20),
      verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
      distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 8000),
    },
  }, 'route', {kind: 'route_label', point: end, payload, priority: 70});
}

function addSites(payload) {
  const sites = payload.sites || [];
  sites.forEach((site, siteIndex) => {
    const [longitude, latitude] = site.coordinate;
    const ground = siteGround(payload, site);
    const planned = site.kind === 'planned_new_site';
    const color = planned ? COLORS.planned : COLORS.host;
    const showSiteLabel = planned || String(site.site_id || '').startsWith('takeoff_landing_site:');
    addEntity({
      id: 'site:' + site.site_id,
      position: Cesium.Cartesian3.fromDegrees(longitude, latitude, ground.ellipsoidal + 6),
      point: {
        pixelSize: planned ? 13 : 11,
        color,
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.NONE,
        disableDepthTestDistance: 0,
      },
      ...(showSiteLabel ? {label: {
        text: shortSiteLabel(site),
        font: '11px "Segoe UI", "Microsoft YaHei", sans-serif',
        fillColor: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.fromCssColorString('#071421'),
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, 12),
        verticalOrigin: Cesium.VerticalOrigin.TOP,
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString('#0b2230').withAlpha(0.72),
        backgroundPadding: new Cesium.Cartesian2(5, 2),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 22000),
      }} : {}),
    }, 'sites', {kind: 'site', site, payload, priority: planned ? 80 : 60});

    (site.devices || []).forEach((device, deviceIndex) => {
      const count = Math.max(1, (site.devices || []).length);
      const angle = (deviceIndex / count) * Math.PI * 2 + siteIndex * 0.4;
      const offsetM = 38;
      const deltaLon = offsetM * Math.cos(angle) / (111320 * Math.cos(latitude * Math.PI / 180));
      const deltaLat = offsetM * Math.sin(angle) / 110540;
      addEntity({
        id: `device:${site.site_id}:${device.device_id}`,
        position: Cesium.Cartesian3.fromDegrees(
          longitude + deltaLon, latitude + deltaLat, ground.ellipsoidal + 3),
        point: {
          pixelSize: 7,
          color: deviceColor(device.subsystem),
          outlineColor: Cesium.Color.fromCssColorString('#071421'),
          outlineWidth: 1,
        },
      }, 'devices', {kind: 'device', site, device, payload});
    });
  });
}

function addActions(payload) {
  (payload.n_actions || []).forEach((action, index) => {
    if (!action.coordinate) return;
    const [longitude, latitude] = action.coordinate;
    const ground = action.ground && Number.isFinite(action.ground.terrain_ellipsoidal_m)
      ? action.ground.terrain_ellipsoidal_m : 0;
    const endpoint = action.kind === 'endpoint'
      || String(action.planner_family || '').includes('endpoint_integrity')
      || (action.segment_index == null
          && !String(action.planner_family || '').includes('middle'));
    const text = endpoint
      ? `N 端点 · ${action.site_id || ''}`
      : `N 中段 · seg${action.segment_index ?? '?'}`;
    addEntity({
      id: 'n:' + action.action_id,
      position: Cesium.Cartesian3.fromDegrees(longitude, latitude, ground + 16),
      point: {
        pixelSize: endpoint ? 15 : 17,
        color: COLORS.n.withAlpha(0.85),
        outlineColor: Cesium.Color.WHITE,
        outlineWidth: 2,
      },
      label: {
        text,
        font: '11px "Segoe UI", "Microsoft YaHei", sans-serif',
        fillColor: Cesium.Color.fromCssColorString('#e9ddff'),
        outlineColor: Cesium.Color.fromCssColorString('#071421'),
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -28 - (index % 3) * 12),
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
        showBackground: true,
        backgroundColor: Cesium.Color.fromCssColorString('#2b2140').withAlpha(0.82),
        backgroundPadding: new Cesium.Cartesian2(5, 2),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 22000),
      },
    }, 'N', {kind: 'action', action, payload, priority: endpoint ? 90 : 85});
  });
}

function addCoverage(payload) {
  for (const layer of payload.layers || []) {
    if (!['C', 'RID', 'S'].includes(layer.id)) continue;
    const color = COLORS[layer.id];
    for (const feature of layer.features || []) {
      if (!feature.center || !(feature.radius_m > 0)) continue;
      const [longitude, latitude] = feature.center;
      if (layer.id === 'S') {
        // S 是 unknown：只画**贴地虚线环**，不画半透明覆盖面，避免被读成有效覆盖。
        const positions = [];
        const steps = 72;
        for (let index = 0; index <= steps; index += 1) {
          const angle = (index / steps) * Math.PI * 2;
          const deltaLon = feature.radius_m * Math.cos(angle)
            / (111320 * Math.cos(latitude * Math.PI / 180));
          const deltaLat = feature.radius_m * Math.sin(angle) / 110540;
          const pointLon = longitude + deltaLon;
          const pointLat = latitude + deltaLat;
          const ground = sampleTerrain(payload.terrain, pointLon, pointLat);
          positions.push(Cesium.Cartesian3.fromDegrees(
            pointLon, pointLat, (ground == null ? 0 : ground) + 4));
        }
        addEntity({
          id: `coverage:${layer.id}:${feature.site_id}`,
          polyline: {
            positions,
            width: 2,
            material: new Cesium.PolylineDashMaterialProperty({
              color: color.withAlpha(0.85), dashLength: 10,
            }),
            arcType: Cesium.ArcType.GEODESIC,
          },
        }, layer.id, {kind: 'coverage', layer, feature, payload});
        continue;
      }
      addEntity({
        id: `coverage:${layer.id}:${feature.site_id}`,
        position: Cesium.Cartesian3.fromDegrees(longitude, latitude),
        ellipse: {
          semiMajorAxis: feature.radius_m,
          semiMinorAxis: feature.radius_m,
          height: 0,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          material: color.withAlpha(layer.status === 'satisfied' ? 0.055 : 0.03),
          outline: true,
          outlineColor: color.withAlpha(layer.status === 'satisfied' ? 0.85 : 0.5),
          outlineWidth: 2,
          classificationType: Cesium.ClassificationType.TERRAIN,
        },
      }, layer.id, {kind: 'coverage', layer, feature, payload});
    }
  }
}

function addScene(payload) {
  clearScene();
  addRoute(payload);
  addSites(payload);
  addActions(payload);
  addCoverage(payload);
  applyVisibility();
}

// --------------------------------------------------------------------- 相机

function routeBounds(payload) {
  const lons = [], lats = [];
  for (const point of payload.route.points || []) { lons.push(point.longitude); lats.push(point.latitude); }
  for (const site of payload.sites || []) { lons.push(site.coordinate[0]); lats.push(site.coordinate[1]); }
  return {
    west: Math.min(...lons), east: Math.max(...lons),
    south: Math.min(...lats), north: Math.max(...lats),
    centerLon: (Math.min(...lons) + Math.max(...lons)) / 2,
    centerLat: (Math.min(...lats) + Math.max(...lats)) / 2,
  };
}

function focusCamera(mode) {
  const payload = state.payload;
  if (!payload || !state.viewer) return;
  const box = routeBounds(payload);
  const ground = sampleTerrain(payload.terrain, box.centerLon, box.centerLat) || 0;
  const target = Cesium.Cartesian3.fromDegrees(box.centerLon, box.centerLat, ground);
  const presets = {
    overview: {heading: 12, pitch: -38, range: 10500},
    top: {heading: 0, pitch: -89.9, range: 9500},
    oblique: {heading: 52, pitch: -22, range: 5200},
    route: {heading: 8, pitch: -26, range: 5600},
  };
  const preset = presets[mode] || presets.overview;
  state.viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(
    Cesium.Math.toRadians(preset.heading), Cesium.Math.toRadians(preset.pitch), preset.range));
  state.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
}

// --------------------------------------------------------------------- 图层

function applyVisibility() {
  const visibility = {};
  document.querySelectorAll('#layerToggles input[data-layer]').forEach(node => {
    visibility[node.dataset.layer] = node.checked;
  });
  for (const [group, entities] of Object.entries(state.groups)) {
    const show = visibility[group] !== false;
    for (const entity of entities) entity.show = show;
  }
  if (state.viewer) {
    if (visibility.terrain === false) {
      state.viewer.terrainProvider = new Cesium.EllipsoidTerrainProvider();
    } else if (state.terrainProvider) {
      state.viewer.terrainProvider = state.terrainProvider;
    }
  }
  const count = Object.values(state.groups).reduce((sum, list) => sum + list.length, 0);
  pill($('layerState'), `${count} 个三维对象`, 'ok');
}

// --------------------------------------------------------------------- 详情

function row(label, value) {
  return `<div class="row"><span>${label}</span><b>${value ?? '—'}</b></div>`;
}

function identityRows(payload) {
  const identity = payload.identity || {};
  return row('run_id', identity.run_id)
    + row('input_version', identity.input_version)
    + row('结果清单 SHA', shortSha(identity.result_list_sha256, 18))
    + row('authority', identity.authority);
}

function inspect(meta) {
  const body = $('inspectBody');
  if (!meta) {
    pill($('inspectState'), '未选择', '');
    body.innerHTML = '<p class="research3d-empty">点击航路、站址、设备或 N 动作，查看身份、假设状态、证据来源与所属 run。</p>';
    return;
  }
  const payload = state.payload;
  pill($('inspectState'), '已选择', 'ok');
  let title = '';
  let kind = '';
  let rows = '';
  let note = '';
  if (meta.kind === 'site') {
    const site = meta.site;
    title = site.site_id;
    kind = site.kind === 'planned_new_site' ? '规划新建站址（研究假设）' : '既有宿主（输入事实）';
    rows = row('设备', `${site.device_count} 台 · ${(site.subsystems || []).join('/')}`)
      + row('复用类型', site.reuse_class || 'unknown')
      + row('proposal_only', String(site.proposal_only === true))
      + row('假设状态', site.assumption_state)
      + row('实际交付', 'unknown')
      + row('地表椭球高', `${site.ground?.terrain_ellipsoidal_m ?? '—'} m`)
      + row('地表正高', `${site.ground?.terrain_orthometric_m ?? '—'} m`)
      + row('EGM2008 N', `${site.ground?.undulation_m ?? '—'} m`);
    note = `证据来源：${site.evidence_source?.container || 'manifest'} · 所属 run ${site.run_id}`;
  } else if (meta.kind === 'device') {
    const {device, site} = meta;
    title = device.device_id;
    kind = `${DEVICE_LABEL[device.subsystem] || device.subsystem} 设备方案`;
    rows = row('所属站址', site.site_id)
      + row('service_key', device.service_key)
      + row('输入半径', `${device.radius_m ?? '—'} m`)
      + row('半径来源', device.radius_source || '—')
      + row('假设状态', device.assumption_state)
      + row('实际交付', 'unknown');
    note = '设备半径是输入投影，不是业务覆盖结论。';
  } else if (meta.kind === 'action') {
    const action = meta.action;
    const endpoint = action.kind === 'endpoint'
      || String(action.planner_family || '').includes('endpoint_integrity')
      || (action.segment_index == null
          && !String(action.planner_family || '').includes('middle'));
    title = action.action_id;
    kind = endpoint ? 'N 端点动作（proposal_only）' : 'N 中段动作（proposal_only）';
    rows = row('站点', action.site_id || '—')
      + row('航段', action.segment_index ?? '端点')
      + row('planner_family', action.planner_family || '—')
      + row('规划状态', action.planning_status)
      + row('实际交付', 'unknown')
      + row('假设状态', action.assumption_state);
    note = 'P16 选中动作仅提案；未安装、未确认、未 Apply。';
  } else if (meta.kind === 'route' || meta.kind === 'route_wall' || meta.kind === 'route_label') {
    title = `${payload.route.route_id} 研究投影航路`;
    kind = '冻结 9 顶点候选（非正式运行航路）';
    rows = row('航路点数', payload.route.point_count)
      + row('高度层', `${payload.vertical.altitude_layer_id} · ${payload.vertical.altitude_layer_name}`)
      + row('EGM2008 正高', `${payload.vertical.nominal_altitude_orthometric_m} m`)
      + row('N 范围', `${payload.vertical.undulation_min_m}–${payload.vertical.undulation_max_m} m`)
      + row('Cesium 椭球高', `${payload.route.points[0].altitude_ellipsoidal_m} m（起点）`)
      + row('运营授权', 'false');
    note = '航路高度逐点 h = H + N；研究投影不是正式运行航路。';
  } else if (meta.kind === 'coverage') {
    const {layer, feature} = meta;
    title = `${LAYER_LABEL[layer.id]} · ${feature.site_id}`;
    kind = '设备输入半径投影（不是业务覆盖结论）';
    rows = row('图层状态', layer.status)
      + row('结论', layer.conclusion)
      + row('输入半径', `${feature.radius_m} m`)
      + row('证据角色', layer.evidence_role)
      + row('状态来源', feature.status_source);
    note = layer.id === 'S'
      ? 'S = RID + Radar；Radar not_calculated ⇒ S unknown，永不显示为满足。'
      : '覆盖圈只投影输入半径，系统不在此重新计算业务覆盖。';
  }
  body.innerHTML = `<h3>${title}</h3><div class="kind">${kind}</div>${rows}`
    + `<div class="note">${note}</div><div class="note">${identityRows(payload)}</div>`;
}

const DEVICE_LABEL = {C: 'C 通信', N: 'N 导航', S: 'S 监视/RID'};

// --------------------------------------------------------------------- 面板

function renderIdentity(payload) {
  const identity = payload.identity || {};
  $('identityRunId').textContent = identity.run_id || '—';
  $('identityInputVersion').textContent = identity.input_version || '—';
  $('identityContract').textContent = identity.contract_version || '—';
  $('identityResultSha').textContent = shortSha(identity.result_list_sha256, 18);
  $('identityResultSha').title = identity.result_list_sha256 || '';
  $('identityPayloadSha').textContent = shortSha(payload.payload_sha256, 18);
  $('identityPayloadSha').title = payload.payload_sha256 || '';
  $('identityManifestSha').textContent = shortSha(identity.manifest_file_sha256, 18);
  $('identityManifestSha').title = identity.manifest_file_sha256 || '';
  pill($('identityState'), '同一 manifest', 'ok');
  $('footerIdentity').textContent = `${identity.run_id} · ${identity.input_version} · ${shortSha(identity.result_list_sha256, 12)}`;
}

function renderVertical(payload) {
  const vertical = payload.vertical || {};
  $('verticalCanonical').textContent = vertical.canonical_reference || '—';
  $('verticalCesium').textContent = vertical.cesium_reference || '—';
  $('verticalLayer').textContent = `${vertical.altitude_layer_id} · ${vertical.nominal_altitude_orthometric_m} m 正高`;
  $('verticalN').textContent = `${vertical.undulation_min_m}–${vertical.undulation_max_m} m`;
  $('verticalGeoid').textContent = shortSha(vertical.geoid?.sha256, 14);
  $('verticalGeoid').title = vertical.geoid?.sha256 || '';
  $('verticalNote').textContent = `${vertical.formula} · ${vertical.geoid?.canonical_backend} · 源状态 SHA 已核对`;
  pill($('verticalState'), '已换算', 'ok');
}

function renderTerrain(payload) {
  const terrain = payload.terrain || {};
  if (terrain.status === 'available') {
    $('terrainSource').textContent = terrain.fabdem?.filename || 'FABDEM';
    $('terrainGrid').textContent = `${terrain.width}×${terrain.height} · ${terrain.grid_resolution_m} m`;
    $('terrainRange').textContent = `${terrain.height_min_m}–${terrain.height_max_m} m 椭球高`;
    $('terrainNodata').textContent = String(terrain.nodata_count ?? '—');
    pill($('terrainState'), '显示网格', 'ok');
  } else {
    $('terrainSource').textContent = '不可用';
    $('terrainGrid').textContent = '—';
    $('terrainRange').textContent = '—';
    $('terrainNodata').textContent = '—';
    pill($('terrainState'), terrain.reason || '不可用', 'bad');
  }
}

function renderCounts(payload) {
  const counts = payload.counts || {};
  $('countSites').textContent = counts.sites ?? '—';
  $('countDevices').textContent = counts.devices ?? '—';
  $('countHosts').textContent = counts.existing_hosts ?? '—';
  $('countPlanned').textContent = counts.planned_new_sites ?? '—';
  $('countN').textContent = counts.n_actions ?? '—';
  pill($('countState'), `${counts.n_actions} 个 N 动作`, 'ok');
}

// --------------------------------------------------------------------- 加载

async function loadRun(runId) {
  if (!runId) return;
  connection('读取三维投影…');
  $('boot').hidden = false;
  $('bootStep').textContent = `读取 ${runId} 的 run manifest 与三维投影`;
  try {
    const {body: payload} = await api(
      `/api/research/runs/${encodeURIComponent(runId)}/visualization-3d`);
    state.payload = payload;
    state.activeRun = payload.run_id;
    terrainData(payload.terrain);
    state.terrainProvider = buildTerrainProvider(payload.terrain);
    if (state.viewer) state.viewer.terrainProvider = state.terrainProvider;
    addBasemap(payload);
    renderIdentity(payload);
    renderVertical(payload);
    renderTerrain(payload);
    renderCounts(payload);
    addScene(payload);
    focusCamera('overview');
    $('runSelect').value = runId;
    history.replaceState(null, '', `?run=${encodeURIComponent(runId)}`);
    $('backTo2d').href = `/?run=${encodeURIComponent(runId)}`;
    connection('研究运行时 · research_isolated 三维只读', 'ok');
    $('boot').hidden = true;
    $('footerState').textContent = '三维只读投影：未重新求解、未重算覆盖、未写正式 ProjectState';
    state.ready = true;
    window.dispatchEvent(new CustomEvent('c3c-3d-loaded', {detail: {runId}}));
  } catch (exc) {
    $('bootStep').textContent = `加载失败：${exc.message}`;
    connection(`三维加载失败：${exc.message}`, 'bad');
    toast(`三维加载失败：${exc.message}`, 'bad');
    throw exc;
  }
}

async function boot() {
  try {
    state.viewer = buildViewer();
    setInitialInteraction();
    const response = await fetch('/api/research/bootstrap');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.boot = data;
    state.token = data.token;
    const select = $('runSelect');
    select.innerHTML = '';
    for (const run of data.runs || []) {
      const option = document.createElement('option');
      option.value = run.run_id;
      option.textContent = `${run.run_id} · ${run.status}`;
      select.appendChild(option);
    }
    const query = new URLSearchParams(location.search);
    const runId = query.get('run') || data.latest_run_id || (data.runs || [])[0]?.run_id;
    if (runId) {
      await loadRun(runId);
    } else {
      $('bootStep').textContent = '尚无已发布研究运行';
      connection('研究运行时就绪；尚无已发布运行', 'ok');
    }
    window.__C3C_3D_READY__ = true;
  } catch (exc) {
    connection(`研究运行时不可用：${exc.message}`, 'bad');
    $('bootStep').textContent = `初始化失败：${exc.message}`;
  }
}

function setInitialInteraction() {
  state.handler = new Cesium.ScreenSpaceEventHandler(state.viewer.scene.canvas);
  state.handler.setInputAction(click => {
    const picked = state.viewer.scene.pick(click.position);
    if (Cesium.defined(picked) && picked.id && picked.id.id) {
      const meta = state.meta.get(picked.id.id);
      if (meta) { inspect(meta); return; }
    }
    inspect(null);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
}

document.querySelectorAll('#layerToggles input[data-layer]').forEach(node => {
  node.addEventListener('change', applyVisibility);
});
document.querySelectorAll('[data-camera]').forEach(node => {
  node.addEventListener('click', () => focusCamera(node.dataset.camera));
});
$('loadRun').addEventListener('click', () => loadRun($('runSelect').value));
$('runSelect').addEventListener('change', event => loadRun(event.target.value));

window.__C3C_3D_DIAG__ = () => ({
  ready: state.ready,
  runId: state.payload?.run_id || null,
  identity: state.payload?.identity || null,
  payloadSha256: state.payload?.payload_sha256 || null,
  counts: state.payload?.counts || null,
  vertical: state.payload ? {
    canonical: state.payload.vertical.canonical_reference,
    cesium: state.payload.vertical.cesium_reference,
    formula: state.payload.vertical.formula,
    altitudeLayer: state.payload.vertical.altitude_layer_id,
    undulationMin: state.payload.vertical.undulation_min_m,
    undulationMax: state.payload.vertical.undulation_max_m,
    routeStartEllipsoidal: state.payload.route.points[0].altitude_ellipsoidal_m,
    routeEndEllipsoidal: state.payload.route.points.at(-1).altitude_ellipsoidal_m,
  } : null,
  layers: (state.payload?.layers || []).map(layer => ({
    id: layer.id, status: layer.status, conclusion: layer.conclusion,
    featureCount: layer.feature_count, evidenceRole: layer.evidence_role,
  })),
  radar: (state.payload?.layers || []).find(layer => layer.id === 'Radar') || null,
  terrain: state.payload ? {
    status: state.payload.terrain.status,
    width: state.payload.terrain.width,
    height: state.payload.terrain.height,
    nodata: state.payload.terrain.nodata_count,
    assetSha256: state.payload.terrain.asset_sha256,
  } : null,
  entities: state.viewer ? Object.fromEntries(
    Object.entries(state.groups).map(([key, list]) => [key, list.length])) : {},
  entityTotal: state.viewer?.entities.values.length || 0,
  camera: state.viewer ? {
    longitude: state.viewer.camera.positionCartographic
      ? Cesium.Math.toDegrees(state.viewer.camera.positionCartographic.longitude) : null,
    latitude: state.viewer.camera.positionCartographic
      ? Cesium.Math.toDegrees(state.viewer.camera.positionCartographic.latitude) : null,
    height: state.viewer.camera.positionCartographic
      ? state.viewer.camera.positionCartographic.height : null,
  } : null,
  terrainRowOrder: terrainRowsNorthFirst ? 'north_to_south' : 'south_to_north',
  imagery: state.viewer ? {
    count: state.viewer.imageryLayers.length,
    layers: state.viewer.imageryLayers._layers.map(layer => ({
      show: layer.show,
      ready: layer.ready,
      providerReady: layer.imageryProvider?.ready,
      error: layer.imageryProvider?.errorEvent ? 'has-event' : null,
      tileWidth: layer.imageryProvider?.tileWidth,
      rectangle: layer.imageryProvider?.rectangle
        ? [Cesium.Math.toDegrees(layer.imageryProvider.rectangle.west),
           Cesium.Math.toDegrees(layer.imageryProvider.rectangle.south),
           Cesium.Math.toDegrees(layer.imageryProvider.rectangle.east),
           Cesium.Math.toDegrees(layer.imageryProvider.rectangle.north)]
        : null,
    })),
  } : null,
  globeTilesLoaded: state.viewer?.scene.globe.tilesLoaded ?? null,
  terrainProvider: state.viewer?.terrainProvider?.constructor?.name || null,
  terrainIsCustom: state.viewer?.terrainProvider === state.terrainProvider,
  terrainCalls: state.terrainCalls,
  terrainLevels: state.terrainLevels,
  terrainError: state.terrainError,
  globeProviderSame: state.viewer?.scene.globe.terrainProvider === state.terrainProvider,
  sceneProviderSame: state.viewer?.scene.terrainProvider === state.terrainProvider,
  terrainReady: state.viewer?.terrainProvider?.ready ?? null,
  terrainErrors: state.viewer?.terrainProvider?.getLevelMaximumGeometricError
    ? [0, 1, 2, 3, 4, 6, 8].map(level =>
      state.viewer.terrainProvider.getLevelMaximumGeometricError(level)) : null,
});

window.__C3C_3D_PROBE__ = async (longitude, latitude) => {
  if (!state.viewer) return null;
  const cartographic = Cesium.Cartographic.fromDegrees(longitude, latitude);
  const deadline = Date.now() + 15000;
  let globeHeight = state.viewer.scene.globe.getHeight(cartographic);
  while ((globeHeight === undefined || globeHeight === null) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 250));
    globeHeight = state.viewer.scene.globe.getHeight(cartographic);
  }
  return {
    globeHeight: globeHeight ?? null,
    expected: sampleTerrain(state.payload.terrain, longitude, latitude),
    longitude, latitude,
  };
};

//: 验收用窄接口：按实体 id 打开详情（不暴露 viewer / 不写任何状态）。
window.__C3C_3D_SELECT__ = id => {
  const meta = state.meta.get(id);
  inspect(meta || null);
  return Boolean(meta);
};

boot();
