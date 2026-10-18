/** Round38-OFFLINE-IMAGERY 前端回归。
 *
 * 覆盖本轮新增/修复的契约：
 *  1. 本地离线影像源被解析并**只**在可用时建立图层；
 *  2. 默认底图为离线卫星影像，且在线 ArcGIS **默认关闭**；
 *  3. 三模式互斥切换只切 ``show``，不新增图层、不动相机与业务实体；
 *  4. 离线瓦片缺失（404）**不**计入失败、**不**触发回退；
 *  5. 离线影像不可用时回退晕渲并给出**真实**原因码；
 *  6. 海面效果在真彩色影像下必须关闭（实测会把海水染成偏蓝发白）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { createBasemap } from '../cns_planner/web/js/visualization3d/basemap.js';

const RELIEF_OPTION = {
  relief: {
    status: 'available', url_template: '/api/visualization-3d/relief/{z}/{x}/{y}.png',
    extent: [121, 29, 123, 32], minzoom: 6, maxzoom: 13, tile_size: 256,
  },
};

/** 离线影像源描述（形状与后端 ``descriptor()`` 一致）。 */
const OFFLINE_SOURCE = {
  id: 'offline_zhoushan_imagery',
  label: '舟山离线卫星影像（Sentinel-2 L2A）',
  status: 'available',
  reason_code: null,
  local_url: '/api/visualization-3d/imagery/offline_zhoushan_imagery/{z}/{x}/{y}',
  tile_size: 256,
  zoom: [9, 14],
  coverage_crs84: [122.087288, 29.747918, 122.43634, 30.036821],
  attribution: 'Contains modified Copernicus Sentinel data 2024',
  is_satellite_imagery: true,
  requires_credential: false,
  consumes_online_budget: false,
};

function cesiumStub({ waterMask = true } = {}) {
  const providers = [];
  const layers = [];
  return {
    providers, layers,
    Cesium: {
      Ellipsoid: { WGS84: 'wgs84' },
      Credit: class { constructor(html) { this.html = html; } },
      Rectangle: { fromDegrees: (...v) => v },
      ShadowMode: { RECEIVE_ONLY: 'receive-only' },
      GeographicTilingScheme: class {},
      WebMercatorTilingScheme: class {},
      Color: { fromCssColorString: (c) => ({ css: c }) },
      CesiumTerrainProvider: {
        fromUrl: async () => ({ kind: 'terrain' }),
      },
      UrlTemplateImageryProvider: class {
        constructor(options) {
          Object.assign(this, options);
          this.errorEvent = { addEventListener: (fn) => { this._listener = fn; } };
          providers.push(this);
        }
        requestImage() { return Promise.resolve({ width: 256 }); }
      },
      ImageryLayer: class {
        constructor(provider) { this._provider = provider; this.show = false; }
        get imageryProvider() { return this._provider; }
      },
    },
    viewer: {
      scene: { globe: {}, fog: {}, skyAtmosphere: {}, verticalExaggeration: 9 },
      imageryLayers: { add: (layer) => { layers.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    },
  };
}

function onlineConfig(extra = {}) {
  return {
    status: 'configured_unverified',
    default_source: 'arcgis_world_imagery',
    scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
    online_allowed: true,
    sources: [
      OFFLINE_SOURCE,
      {
        id: 'arcgis_world_imagery', status: 'configured_unverified',
        local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
        attribution: 'Tiles © Esri', tile_size: 256, zoom: [4, 20],
      },
    ],
    ...extra,
  };
}

async function build(config, options = RELIEF_OPTION, stub = cesiumStub()) {
  const previous = globalThis.Cesium;
  globalThis.Cesium = stub.Cesium;
  const controller = await createBasemap(stub.viewer, config, {
    terrainUrl: '/api/visualization-3d/terrain',
    // 水掩膜可用是海面效果的**前提**；这里显式开启，才能验证"真彩色影像下必须关闭"。
    waterMaskAvailable: true,
    ...options,
  });
  return { controller, stub, restore: () => { globalThis.Cesium = previous; } };
}

test('offline imagery source creates a third layer limited to its real coverage', async () => {
  const { controller, stub, restore } = await build(onlineConfig());
  try {
    assert.equal(stub.layers.length, 3, '在线影像 + 离线影像 + 晕渲，共三层');
    const offline = stub.providers.find(
      (p) => p.url === OFFLINE_SOURCE.local_url);
    assert.ok(offline, '必须建立本地离线影像 provider');
    // 矩形必须取数据**真实**覆盖范围，否则会在数据边缘请求不存在的瓦片。
    assert.deepEqual(offline.rectangle, OFFLINE_SOURCE.coverage_crs84);
    // 层级上限必须是数据真实最细层级：虚报更高层级只会请求到空瓦片。
    assert.equal(offline.maximumLevel, 14);
    assert.equal(offline.minimumLevel, 9);
    assert.equal(controller.offlineImageryAvailable(), true);
    assert.equal(controller.offlineImageryBlockReason(), null);
  } finally { restore(); }
});

test('default basemap is offline imagery and online stays off until asked', async () => {
  const { controller, stub, restore } = await build(onlineConfig());
  try {
    // 任务要求"默认关闭在线 ArcGIS 请求"：即使在线**可用**也必须默认不显示。
    assert.equal(controller.basemapMode(), 'offline_imagery');
    assert.equal(controller.mode(), 'offline');
    const onlineLayer = stub.layers.find(
      (l) => l.imageryProvider.url.includes('arcgis_world_imagery'));
    assert.equal(onlineLayer.show, false, '在线图层默认必须不可见');
    const offlineLayer = stub.layers.find(
      (l) => l.imageryProvider.url.includes('offline_zhoushan_imagery'));
    assert.equal(offlineLayer.show, true, '默认显示本地离线卫星影像');
  } finally { restore(); }
});

test('three modes are mutually exclusive and never add or drop layers', async () => {
  const { controller, stub, restore } = await build(onlineConfig());
  try {
    const count = stub.layers.length;
    const layerOf = (needle) => stub.layers.find(
      (l) => l.imageryProvider.url.includes(needle));
    const online = layerOf('arcgis_world_imagery');
    const offline = layerOf('offline_zhoushan_imagery');
    const relief = stub.layers.find(
      (l) => l.imageryProvider.url.includes('/relief/'));

    await controller.setMode('offline');
    assert.deepEqual([online.show, offline.show, relief.show], [false, false, true]);
    assert.equal(controller.basemapMode(), 'offline_relief');

    await controller.setMode('offline_imagery');
    assert.deepEqual([online.show, offline.show, relief.show], [false, true, false]);
    assert.equal(controller.basemapMode(), 'offline_imagery');

    await controller.setMode('online', {
      waitForImageryTiles: async () => ({ ok: true, resolved: 1 }),
    });
    assert.deepEqual([online.show, offline.show, relief.show], [true, false, false]);
    assert.equal(controller.basemapMode(), 'online');

    await controller.setMode('offline_imagery');
    assert.deepEqual([online.show, offline.show, relief.show], [false, true, false]);
    assert.equal(stub.layers.length, count, '切换只切 show，绝不新增图层');
  } finally { restore(); }
});

test('water effect is off whenever true colour imagery is shown', async () => {
  // 实测缺陷：showWaterEffect 把舟山近岸浑浊黄绿色海水染成偏蓝发白
  // （画面均值 B−R 由 −26.2 变为 +5.6）。真彩色影像下必须关闭。
  const { controller, stub, restore } = await build(onlineConfig());
  try {
    assert.equal(controller.basemapMode(), 'offline_imagery');
    assert.equal(stub.viewer.scene.globe.showWaterEffect, false,
      '真彩色离线影像下海面效果必须关闭');

    await controller.setMode('offline');
    assert.equal(stub.viewer.scene.globe.showWaterEffect, true,
      '灰阶地形晕渲下保留海面效果（提供海陆区分）');

    await controller.setMode('offline_imagery');
    assert.equal(stub.viewer.scene.globe.showWaterEffect, false);

    await controller.setMode('online', {
      waitForImageryTiles: async () => ({ ok: true, resolved: 1 }),
    });
    assert.equal(stub.viewer.scene.globe.showWaterEffect, false,
      '在线真彩色影像同样必须关闭海面效果');

    assert.equal(controller.status().water_effect_policy,
      'relief_only_not_on_true_color_imagery');
  } finally { restore(); }
});

test('missing offline tiles are counted separately and never trigger fallback', async () => {
  const { controller, stub, restore } = await build(onlineConfig());
  try {
    const offline = stub.layers.find(
      (l) => l.imageryProvider.url.includes('offline_zhoushan_imagery'));
    const provider = offline.imageryProvider;
    // 本地数据范围边缘：后端 404，这是**正常**状态。
    provider._listener({ message: 'Failed to obtain image tile X: 1 Y: 1 Level: 14.' });
    assert.equal(controller.status().offline_tile_missing, 1);
    // 绝不能被当成在线失败，也不能把模式打回晕渲。
    assert.equal(controller.status().imagery_failures, 0);
    assert.equal(controller.basemapMode(), 'offline_imagery');
    assert.equal(controller.mode(), 'offline');
  } finally { restore(); }
});

test('unavailable offline imagery falls back to relief with the real reason', async () => {
  const blocked = {
    ...OFFLINE_SOURCE, status: 'blocked',
    reason_code: 'offline_imagery_metadata_missing',
  };
  const config = onlineConfig({ sources: [blocked, onlineConfig().sources[1]] });
  const { controller, stub, restore } = await build(config);
  try {
    assert.equal(stub.layers.length, 2, '离线源不可用 ⇒ 不建立该图层');
    assert.equal(controller.offlineImageryAvailable(), false);
    assert.equal(controller.offlineImageryBlockReason(),
      'offline_imagery_metadata_missing');
    // 默认必须落在晕渲上，并给出**真实**原因（不是"加载失败"）。
    assert.equal(controller.basemapMode(), 'offline_relief');
    assert.equal(controller.status().mode_reason, 'offline_imagery_metadata_missing');
    // 显式切换到离线影像同样回退晕渲，绝不留下空白。
    const result = await controller.setMode('offline_imagery');
    assert.equal(result.ok, false);
    assert.equal(result.basemap_mode, 'offline_relief');
    assert.equal(result.reason, 'offline_imagery_metadata_missing');
  } finally { restore(); }
});

test('offline imagery works even when online access is disabled', async () => {
  // 本轮的核心解耦：离线影像**不**依赖在线授权。
  const config = onlineConfig({ online_allowed: false, status: 'offline_mode' });
  const { controller, stub, restore } = await build(config);
  try {
    assert.equal(controller.onlineAvailable(), false);
    assert.equal(controller.offlineImageryAvailable(), true);
    assert.equal(controller.basemapMode(), 'offline_imagery');
    const offline = stub.layers.find(
      (l) => l.imageryProvider.url.includes('offline_zhoushan_imagery'));
    assert.equal(offline.show, true);
    const online = stub.layers.find(
      (l) => l.imageryProvider.url.includes('arcgis_world_imagery'));
    assert.equal(online, undefined, '在线未授权时结构上不建立该图层');
  } finally { restore(); }
});

test('late online failure falls back to offline imagery, not bare relief', async () => {
  const stub = cesiumStub();
  // 只让**在线** provider 暴露 errorEvent：离线 provider 没有失败回调，
  // 因此这里拿到的 fail 必然属于在线图层（否则会被最后建立的图层覆盖）。
  const listeners = [];
  stub.Cesium.UrlTemplateImageryProvider = class {
    constructor(options) {
      Object.assign(this, options);
      if (String(options.url).includes('arcgis_world_imagery')) {
        this.errorEvent = { addEventListener: (fn) => { listeners.push(fn); } };
      }
    }
    requestImage() { return Promise.resolve({ width: 256 }); }
  };
  const { controller, restore } = await build(onlineConfig(), RELIEF_OPTION, stub);
  try {
    assert.equal(listeners.length, 1, '只有在线 provider 注册失败回调');
    await controller.setMode('online', {
      waitForImageryTiles: async () => ({ ok: true, resolved: 1 }),
    });
    assert.equal(controller.basemapMode(), 'online');
    listeners[0]({ message: 'Failed to obtain image tile X: 3438 Y: 1689 Level: 12.',
      error: { response: '{"code":"imagery_budget_exhausted"}' } });
    // 回退优先回到**仍可用且不触网**的本地离线影像，而不是灰阶晕渲。
    assert.equal(controller.mode(), 'offline');
    assert.equal(controller.basemapMode(), 'offline_imagery');
    assert.equal(controller.status().mode_reason, 'imagery_budget_exhausted');
  } finally { restore(); }
});
