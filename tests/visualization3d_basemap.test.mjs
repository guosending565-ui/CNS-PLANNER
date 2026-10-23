import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  applyCameraState,
  readCameraState,
} from '../cns_planner/web/js/visualization3d/camera_state.js';
import {
  BASE_LAYER_DEFAULTS,
  BUSINESS_LAYER_DEFAULTS,
  LazyLayerRegistry,
  attachImageryTileCounters,
  createBasemap,
  createLocalViewer,
  imageryStats,
  parseImageryTileError,
  tileOutsideScope,
  webMercatorTileBounds,
} from '../cns_planner/web/js/visualization3d/basemap.js';
import {
  CAMERA_PRESETS,
  flyToPreset,
} from '../cns_planner/web/js/visualization3d/camera_presets.js';

test('camera state serializes the six public numeric fields without leaking the camera', () => {
  const camera = {
    positionCartographic: { longitude: 1.2, latitude: 0.5, height: 4321 },
    heading: 0.25,
    pitch: -0.8,
    roll: 0.01,
  };

  const state = readCameraState(camera, (radians) => radians * 180 / Math.PI);

  assert.deepEqual(Object.keys(state), [
    'longitude', 'latitude', 'height', 'heading', 'pitch', 'roll',
  ]);
  assert.ok(Object.values(state).every(Number.isFinite));
  assert.equal(Object.values(state).includes(camera), false);
});

test('camera state restores a validated view and rejects non-finite input', () => {
  const calls = [];
  const camera = { setView: (options) => calls.push(options) };
  const fromDegrees = (longitude, latitude, height) => ({ longitude, latitude, height });
  const state = {
    longitude: 122.15,
    latitude: 29.82,
    height: 12000,
    heading: 0.2,
    pitch: -0.9,
    roll: 0,
  };

  assert.equal(applyCameraState(camera, state, fromDegrees), true);
  assert.deepEqual(calls, [{
    destination: { longitude: 122.15, latitude: 29.82, height: 12000 },
    orientation: { heading: 0.2, pitch: -0.9, roll: 0 },
  }]);
  assert.equal(applyCameraState(camera, { ...state, height: Number.NaN }, fromDegrees), false);
  assert.equal(calls.length, 1);
});

test('all business layers are off and remain unloaded until first enable', async () => {
  assert.ok(Object.values(BUSINESS_LAYER_DEFAULTS).every((value) => value === false));
  assert.deepEqual(BASE_LAYER_DEFAULTS, { imagery: true, terrain: true, labels: false });
  let loads = 0;
  const entities = [{ show: true }, { show: true }];
  const registry = new LazyLayerRegistry({ routes: async () => { loads += 1; return entities; } });
  assert.equal(loads, 0);
  await registry.setVisible('routes', false);
  assert.equal(loads, 0);
  await registry.setVisible('routes', true);
  assert.equal(loads, 1);
  await registry.setVisible('routes', false);
  assert.equal(loads, 1);
  assert.ok(entities.every((entity) => entity.show === false));
  await registry.setVisible('routes', true);
  assert.equal(loads, 1);
});

test('basemap uses CesiumTerrainProvider automatic LOD with natural scene settings', async () => {
  const terrainCalls = [];
  const imageryProviders = [];
  globalThis.Cesium = {
    CesiumTerrainProvider: {
      fromUrl: async (url, options) => {
        terrainCalls.push({ url, options });
        return { kind: 'multilevel-terrain' };
      },
    },
    UrlTemplateImageryProvider: class {
      constructor(options) { Object.assign(this, options); imageryProviders.push(options); }
    },
    ImageryLayer: class { constructor(provider) { this.imageryProvider = provider; this.show = true; } },
    Credit: class { constructor(html) { this.html = html; } },
    Rectangle: { fromDegrees: (...values) => values },
    ShadowMode: { RECEIVE_ONLY: 'receive-only' },
  };
  const layers = [];
  const viewer = {
    imageryLayers: { add: (layer) => { layers.push(layer); return layer; } },
    scene: {
      globe: {}, fog: {}, skyAtmosphere: {},
      verticalExaggeration: 9,
    },
    resolutionScale: 1,
    shadows: false,
  };
  const config = {
    status: 'configured_unverified',
    default_source: 'arcgis_world_imagery',
    scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
    sources: [{
      id: 'arcgis_world_imagery', status: 'configured_unverified',
      local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
      attribution: 'Tiles © Esri and data providers', tile_size: 256,
    }],
  };
  const controller = await createBasemap(viewer, config, {
    terrainUrl: '/api/visualization-3d/terrain', waterMaskAvailable: false,
  });
  assert.deepEqual(terrainCalls, [{
    url: '/api/visualization-3d/terrain',
    options: { requestWaterMask: false, requestVertexNormals: false },
  }]);
  assert.equal(viewer.terrainProvider.kind, 'multilevel-terrain');
  assert.equal(viewer.scene.verticalExaggeration, 1.0);
  assert.equal(viewer.scene.globe.enableLighting, true);
  assert.equal(viewer.scene.globe.depthTestAgainstTerrain, true);
  assert.equal(viewer.scene.globe.showWaterEffect, false);
  assert.equal(viewer.scene.skyAtmosphere.show, true);
  assert.equal(viewer.scene.fog.enabled, true);
  assert.equal(viewer.shadows, true);
  assert.equal(imageryProviders[0].url, config.sources[0].local_url);
  controller.setImageryVisible(false);
  assert.equal(layers[0].show, false);
  controller.setTerrainVisible(false);
  assert.equal(viewer.scene.globe.show, false);
  controller.setLabelsVisible(true);
  assert.equal(controller.status().labels, 'blocked');
  controller.setQuality('high');
  assert.equal(viewer.scene.globe.maximumScreenSpaceError, 1.5);
  controller.setQuality('normal');
  assert.equal(viewer.scene.globe.maximumScreenSpaceError, 3.0);
});

test('local viewer uses an inlined multi-pixel water normal map, not a 1x1 pixel', () => {
  const captured = [];
  globalThis.Cesium = {
    Ellipsoid: { WGS84: 'wgs84' },
    Globe: class {
      constructor(ellipsoid) { this.ellipsoid = ellipsoid; this.oceanNormalMapUrl = 'missing.jpg'; }
    },
    Viewer: class {
      constructor(container, options) { captured.push({ container, options }); this.scene = {}; }
    },
  };
  createLocalViewer('cesiumContainer', { geocoder: false });
  assert.equal(captured[0].options.skyBox, false);
  assert.equal(captured[0].options.moon, false);
  const url = captured[0].options.globe.oceanNormalMapUrl;
  assert.match(url, /^data:image\/png;base64,/);
  // A 1x1 neutral pixel makes showWaterEffect blow the sea out to near-white
  // (measured mean luminance 126 -> 249). The embedded map must be larger.
  const bytes = Buffer.from(url.split(',')[1], 'base64');
  assert.deepEqual([...bytes.subarray(0, 8)],
    [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  assert.ok(width > 1 && height > 1, `expected >1x1 normal map, got ${width}x${height}`);
  assert.equal(captured[0].options.skyAtmosphere, undefined);
});

test('six immutable camera presets fly deterministically', () => {
  assert.equal(Object.keys(CAMERA_PRESETS).length, 6);
  assert.ok(Object.isFrozen(CAMERA_PRESETS));
  assert.ok(Object.values(CAMERA_PRESETS).every(Object.isFrozen));
  const calls = [];
  globalThis.Cesium = {
    Cartesian3: { fromDegrees: (...values) => values },
    Math: { toRadians: (degrees) => degrees * Math.PI / 180 },
  };
  const viewer = { camera: { flyTo: (options) => calls.push(options) } };
  assert.equal(flyToPreset(viewer, 'taohua_oblique'), true);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].destination, [122.28, 29.81, 8500]);
  assert.equal(flyToPreset(viewer, 'unknown'), false);
});

test('basemap does not rely on the return value of imageryLayers.add', async () => {
  // 回归（真实缺陷，浏览器实测确认）：Cesium 1.126 的
  // `ImageryLayerCollection.prototype.add()` 返回 **undefined**。早期实现写
  // `reliefLayer = viewer.imageryLayers.add(new Cesium.ImageryLayer(provider))`，
  // 于是 reliefLayer 恒为 undefined —— 图层**确实在渲染**（实测 imagery_layers=1、
  // 晕渲瓦片正常返回、注入钩子记录了这次 add），但 status().relief 报 'blocked'、
  // reliefAvailable() 为 false、setReliefVisible() 直接失败，版权栏也因此丢掉
  // "FABDEM 地形晕渲（非卫星影像）"这条来源标注，交付证据里的 basemap_status.relief
  // 还记成了错误的值。图层引用必须显式持有，不能取自 add() 的返回值。
  const added = [];
  const previous = globalThis.Cesium;
  globalThis.Cesium = {
    CesiumTerrainProvider: { fromUrl: async () => ({ kind: 'terrain' }) },
    UrlTemplateImageryProvider: class {
      constructor(options) { this.options = options; }
    },
    ImageryLayer: class {
      constructor(provider) { this.imageryProvider = provider; this.show = true; }
    },
    Rectangle: { fromDegrees: (...degrees) => ({ degrees }) },
    Credit: class { constructor(text) { this.text = text; } },
    Color: { fromCssColorString: (css) => ({ css }) },
    GeographicTilingScheme: class {},
    ShadowMode: { RECEIVE_ONLY: 2 },
  };
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      // 关键：模拟真实 Cesium —— add() 不返回图层。
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(
      viewer,
      { status: 'blocked', reason_code: 'arcgis_token_missing', sources: [] },
      {
        relief: {
          status: 'available',
          url_template: '/api/visualization-3d/relief/{z}/{x}/{y}.png',
          extent: [121, 29, 123, 32], minzoom: 6, maxzoom: 13, tile_size: 256,
        },
      });

    assert.equal(added.length, 1, '晕渲图层必须被真正加入 imageryLayers');
    assert.equal(controller.status().relief, 'available');
    assert.equal(controller.reliefAvailable(), true);
    assert.equal(controller.setReliefVisible(false), true);
    assert.equal(added[0].show, false, '图层开关必须真的作用到图层对象上');
    // 影像仍然未被授权：不得因为没有影像层就把 relief 当成影像。
    assert.equal(controller.status().imagery, 'blocked');
    assert.equal(controller.status().relief_is_satellite_imagery, false);
  } finally {
    globalThis.Cesium = previous;
  }
});

test('imagery tile failures are observed through provider errorEvent', async () => {
  // 真实缺陷（浏览器实测）：11/11 影像瓦片 503 时，Cesium 只在 provider.errorEvent
  // 上广播，面板却仍显示"已配置，等待真实瓦片验证"，失败只存在于 DevTools。
  const listeners = [];
  const previous = globalThis.Cesium;
  globalThis.Cesium = {
    CesiumTerrainProvider: { fromUrl: async () => ({ kind: 'terrain' }) },
    UrlTemplateImageryProvider: class {
      constructor(options) {
        this.options = options;
        this.errorEvent = { addEventListener: (fn) => listeners.push(fn) };
      }
    },
    ImageryLayer: class {
      constructor(provider) { this.imageryProvider = provider; this.show = true; }
    },
    Rectangle: { fromDegrees: (...degrees) => degrees },
    Credit: class { constructor(text) { this.text = text; } },
    ShadowMode: { RECEIVE_ONLY: 2 },
  };
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: () => undefined },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const observed = [];
    const controller = await createBasemap(viewer, {
      status: 'configured_unverified',
      default_source: 'arcgis_world_imagery',
      scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
      sources: [{
        id: 'arcgis_world_imagery', status: 'configured_unverified',
        local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
        tile_size: 256,
      }],
    }, { onImageryFailure: (count, message) => observed.push([count, message]) });

    assert.equal(listeners.length, 1, '必须订阅 provider.errorEvent');
    assert.equal(controller.status().imagery_failures, 0);
    listeners[0](new Error('Failed to obtain image tile X: 429 Y: 210 Level: 9.'));
    assert.equal(controller.status().imagery_failures, 1);
    assert.equal(controller.status().imagery_last_error,
      'Failed to obtain image tile X: 429 Y: 210 Level: 9.');
    assert.deepEqual(observed, [[1, 'Failed to obtain image tile X: 429 Y: 210 Level: 9.']]);
  } finally {
    globalThis.Cesium = previous;
  }
});

test('imagery tile counter only counts real image results, not every request attempt', async () => {
  // Cesium 会对同一瓦片重复调用 requestImage（去重前重试），实测同一会话调用 6009 次
  // 而网络层只有 282 张瓦片。若把"方法调用次数"当已下载瓦片数，面板与证据都会虚高。
  const provider = {
    url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
    tilingScheme: {},
    maximumLevel: 20,
    minimumLevel: 4,
    credit: { html: 'Tiles © Esri' },
    requestImage: () => Promise.resolve(undefined),      // 被丢弃的请求
  };
  const progress = [];
  attachImageryTileCounters(provider, (count) => progress.push(count));
  const layer = { _imageryProvider: provider, ready: true, show: true };

  await provider.requestImage(1, 2, 3);
  await provider.requestImage(1, 2, 3);
  assert.equal(imageryStats(layer).request_image_calls, 2);
  assert.equal(imageryStats(layer).resolved, 0,
    '没有真实图像结果的调用不得计入已下载瓦片');

  provider.requestImage = (() => {
    const inner = () => Promise.resolve({ width: 256 });
    return inner;
  })();
  attachImageryTileCounters(provider, (count) => progress.push(count));
  await provider.requestImage(1, 2, 3);
  assert.equal(imageryStats(layer).resolved, 1);
  assert.equal(imageryStats(layer).accepted, 1);
  assert.deepEqual(progress, [1]);

  // 测试替身没有 requestImage 时必须安全返回 null，而不是抛异常。
  assert.equal(attachImageryTileCounters({}), null);
});

test('imagery stats reports the tiling scheme identity used for registration', () => {
  const previous = globalThis.Cesium;
  class WebMercatorTilingScheme {}
  class GeographicTilingScheme {}
  globalThis.Cesium = { WebMercatorTilingScheme, GeographicTilingScheme };
  try {
    const imagery = imageryStats({
      _imageryProvider: { url: 'u', tilingScheme: new WebMercatorTilingScheme() },
      ready: true, show: true,
    });
    const relief = imageryStats({
      _imageryProvider: { url: 'r', tilingScheme: new GeographicTilingScheme() },
      ready: true, show: false,
    });
    assert.equal(imagery.tiling_scheme_is_webmercator, true);
    assert.equal(imagery.tiling_scheme_is_geographic, false);
    assert.equal(relief.tiling_scheme_is_geographic, true);
    assert.equal(relief.tiling_scheme_is_webmercator, false);
    assert.equal(relief.show, false);
  } finally {
    globalThis.Cesium = previous;
  }
});

/* ------------------------------------------------ T3D-2A：离线模式与丢弃语义 */

function offlineCesium(layers) {
  return {
    CesiumTerrainProvider: { fromUrl: async () => ({ kind: 'terrain' }) },
    UrlTemplateImageryProvider: class {
      constructor(options) {
        this.options = options;
        this.errorEvent = { addEventListener: () => {} };
      }
    },
    ImageryLayer: class {
      constructor(provider) { this.imageryProvider = provider; this.show = true; }
    },
    Rectangle: { fromDegrees: (...degrees) => degrees },
    Credit: class { constructor(text) { this.text = text; } },
    Color: { fromCssColorString: (css) => ({ css }) },
    GeographicTilingScheme: class {},
    ShadowMode: { RECEIVE_ONLY: 2 },
  };
}

test('offline imagery mode never creates a satellite provider and falls back to relief', async () => {
  // 任务硬要求：默认离线开发模式下**绝不**向上游发出影像请求。Cesium 对 show=false
  // 的图层不取瓦片，但创建一个可用 provider 仍意味着"随时可能发请求"；离线模式必须
  // 从根上不建立这条链路，并把 FABDEM 晕渲作为默认底图。
  const previous = globalThis.Cesium;
  const added = [];
  const providers = [];
  globalThis.Cesium = offlineCesium(added);
  const OriginalProvider = globalThis.Cesium.UrlTemplateImageryProvider;
  globalThis.Cesium.UrlTemplateImageryProvider = class extends OriginalProvider {
    constructor(options) { super(options); providers.push(options.url); }
  };
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, {
      status: 'offline_mode', reason_code: 'imagery_online_mode_disabled',
      online_allowed: false, default_source: 'arcgis_world_imagery',
      scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
      sources: [{
        id: 'arcgis_world_imagery', status: 'offline_mode',
        local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
        tile_size: 256,
      }],
    }, {
      relief: {
        status: 'available', url_template: '/api/visualization-3d/relief/{z}/{x}/{y}.png',
        extent: [121, 29, 123, 32], minzoom: 6, maxzoom: 13, tile_size: 256,
      },
    });
    assert.equal(controller.onlineAllowed(), false);
    assert.equal(controller.imageryAvailable(), false);
    assert.equal(controller.setImageryVisible(true), false,
      '离线模式下不得存在可开启的影像图层');
    assert.equal(providers.length, 1, '只允许创建晕渲 provider，不得创建影像 provider');
    assert.match(providers[0], /\/relief\//);
    assert.equal(added.length, 1);
    assert.equal(added[0].show, true, 'offline relief stays visible');
    assert.equal(controller.status().imagery, 'blocked');
    assert.equal(controller.status().relief, 'available');
  } finally {
    globalThis.Cesium = previous;
  }
});

test('cesium tile errors are classified into out-of-scope, discard and real failure', async () => {
  // 真实缺陷（T3D-BMAP-2 §1.4）：网络层 0 失败、代理 0 拒绝，但 Cesium 仍会触发
  // errorEvent（范围外瓦片被后端 503 拒绝、或内部丢弃），面板因此显示红字
  // "卫星影像瓦片加载失败"，把用户引向错误方向。三类事件必须**可独立验证**地分开：
  // 范围外瓦片用**几何复算**判定（不靠文案措辞），丢弃用 provider 计数判定。
  const previous = globalThis.Cesium;
  const listeners = [];
  const added = [];
  globalThis.Cesium = {
    CesiumTerrainProvider: { fromUrl: async () => ({ kind: 'terrain' }) },
    UrlTemplateImageryProvider: class {
      constructor(options) {
        this.options = options;
        this.errorEvent = { addEventListener: (fn) => listeners.push(fn) };
      }
      // 默认：请求正常完成但返回空 = Cesium 丢弃；测试可把 ``__mode`` 改成 'reject'
      // 模拟真实网络/代理失败。注意**不能**在 attachImageryTileCounters 之后再整体
      // 覆盖 requestImage —— 那会绕开计数包装，让"真实拒绝"永远不可见。
      requestImage() {
        return this.__mode === 'reject'
          ? Promise.reject(new Error('upstream 503'))
          : Promise.resolve(undefined);
      }
    },
    ImageryLayer: class {
      constructor(provider) { this.imageryProvider = provider; this.show = true; }
    },
    Rectangle: { fromDegrees: (...degrees) => degrees },
    Credit: class { constructor(text) { this.text = text; } },
    ShadowMode: { RECEIVE_ONLY: 2 },
  };
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const failures = [];
    const discards = [];
    const outOfScope = [];
    const controller = await createBasemap(viewer, {
      status: 'configured_unverified', online_allowed: true,
      default_source: 'arcgis_world_imagery',
      scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
      sources: [{
        id: 'arcgis_world_imagery', status: 'configured_unverified',
        local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
        tile_size: 256,
      }],
    }, {
      onImageryFailure: (count, message) => failures.push([count, message]),
      onImageryDiscard: (count, message) => discards.push([count, message]),
      onImageryOutOfScope: (count, message) => outOfScope.push([count, message]),
    });
    const provider = added[0].imageryProvider;

    // (1) 范围外瓦片（z=10 x=100 y=500 远在非洲以西）⇒ 不得报成失败。
    //     注意：T3D-BMAP-2 §1.4 记录的那张 `X:861 Y:422 Level:10` **确实在舟山范围内**
    //     （122.7–123.05E / 29.71–30.03N），因此它**不能**用范围守卫解释 —— 这也说明
    //     分类必须靠几何复算而不是靠"看起来像边界"。
    listeners[0](new Error('Failed to obtain image tile X: 100 Y: 500 Level: 10.'));
    assert.equal(controller.status().imagery_failures, 0, '范围外瓦片不得计入失败');
    assert.equal(controller.status().imagery_out_of_scope, 1);
    assert.equal(outOfScope.length, 1);
    assert.equal(failures.length, 0);

    // (2) 范围内瓦片 + 无真实 promise 拒绝 ⇒ 判为 Cesium 内部丢弃。
    //     先真的走一次 requestImage（返回空结果 = 丢弃），使 provider 计数可见。
    await provider.requestImage(861, 422, 10);
    listeners[0](new Error('Failed to obtain image tile X: 861 Y: 422 Level: 10.'));
    assert.equal(controller.status().imagery_failures, 0, '丢弃不得计入失败');
    assert.equal(controller.status().imagery_discards, 1);
    assert.equal(discards.length, 1);
    assert.equal(failures.length, 0);

    // (3) 范围内瓦片 + provider 记录到真实 promise 拒绝 ⇒ 才算真实失败。
    provider.__mode = 'reject';
    await provider.requestImage(859, 421, 10).catch(() => {});
    listeners[0](new Error('Failed to obtain image tile X: 859 Y: 421 Level: 10.'));
    assert.equal(controller.status().imagery_failures, 1);
    assert.equal(failures.length, 1);
    assert.match(failures[0][1], /Failed to obtain image tile/);
  } finally {
    globalThis.Cesium = previous;
  }
});

test('imagery tile geometry helpers reproduce the backend scope guard', () => {
  // 前端**独立复算**后端的范围守卫判据（同一 WebMercator XYZ 网格与同一 bbox），
  // 因此"范围外"这个分类不依赖任何错误文案猜测。
  const bbox = [121.45, 29.3, 123.05, 30.65];
  assert.deepEqual(parseImageryTileError(
    'Failed to obtain image tile X: 861 Y: 422 Level: 10.'),
  { x: 861, y: 422, z: 10 });
  assert.equal(parseImageryTileError('some other error'), null);
  // z=10 x=100 y=500 远在受控 bbox 之外 ⇒ 范围外（与后端单测同一坐标）。
  assert.equal(tileOutsideScope(
    'Failed to obtain image tile X: 100 Y: 500 Level: 10.', bbox), true);
  // z=10 x=861 y=422 实测**在**舟山范围内（122.7–123.05E / 29.71–30.03N）⇒ 不得判成范围外。
  assert.equal(tileOutsideScope(
    'Failed to obtain image tile X: 861 Y: 422 Level: 10.', bbox), false);
  assert.equal(tileOutsideScope(
    'Failed to obtain image tile X: 859 Y: 421 Level: 10.', bbox), false);
  // 不可解析/非法坐标必须返回 null（"未评估"），绝不默认判成范围外。
  assert.equal(tileOutsideScope('nope', bbox), null);
  assert.equal(tileOutsideScope(
    'Failed to obtain image tile X: -1 Y: 5 Level: 10.', bbox), null);
  assert.equal(tileOutsideScope(
    'Failed to obtain image tile X: 861 Y: 422 Level: 10.', null), null);
  const bounds = webMercatorTileBounds(10, 859, 421);
  assert.ok(bounds.west < 122.25 && bounds.east > 122.25);
});

test('coverage layer defaults are off and the registry loads them lazily', async () => {
  // 默认首次进入页面只显示三维基础地理环境：通信/RID 必须默认关闭且**尚未请求**。
  assert.equal(BUSINESS_LAYER_DEFAULTS.communication, false);
  assert.equal(BUSINESS_LAYER_DEFAULTS.rid, false);
  let communicationLoads = 0;
  let ridLoads = 0;
  const registry = new LazyLayerRegistry({
    communication: async () => { communicationLoads += 1; return [{ show: true }]; },
    rid: async () => { ridLoads += 1; return [{ show: true }]; },
  });
  assert.equal(communicationLoads + ridLoads, 0);
  await registry.setVisible('communication', false);
  await registry.setVisible('rid', false);
  assert.equal(communicationLoads + ridLoads, 0, '关闭状态不得触发任何几何请求');
  await registry.setVisible('communication', true);
  assert.equal(communicationLoads, 1);
  assert.equal(ridLoads, 0, '只开启通信不得顺带请求 RID');
  await registry.setVisible('rid', true);
  assert.equal(ridLoads, 1);
});

/* ------------------------- Round38-E2E-PRECHECK 任务 B：底图模式开关（离线/在线） */

/** 造一个**在线可用**的 basemap 场景（影像 provider 真实存在 + 晕渲图层同时存在）。 */
function onlineCapableCesium() {
  return {
    CesiumTerrainProvider: { fromUrl: async () => ({ kind: 'terrain' }) },
    UrlTemplateImageryProvider: class {
      constructor(options) {
        this.options = options;
        this.errorEvent = { addEventListener: () => {} };
      }
      requestImage() { return Promise.resolve({ width: 256 }); }
    },
    ImageryLayer: class {
      constructor(provider) { this.imageryProvider = provider; this.show = true; }
    },
    Rectangle: { fromDegrees: (...degrees) => degrees },
    Credit: class { constructor(text) { this.text = text; } },
    Color: { fromCssColorString: (css) => ({ css }) },
    GeographicTilingScheme: class {},
    ShadowMode: { RECEIVE_ONLY: 2 },
  };
}

const ONLINE_CONFIG = {
  status: 'configured_unverified', online_allowed: true,
  default_source: 'arcgis_world_imagery',
  scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
  sources: [{
    id: 'arcgis_world_imagery', status: 'configured_unverified',
    local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
    tile_size: 256,
  }],
};
const RELIEF_OPTION = {
  relief: {
    status: 'available', url_template: '/api/visualization-3d/relief/{z}/{x}/{y}.png',
    extent: [121, 29, 123, 32], minzoom: 6, maxzoom: 13, tile_size: 256,
  },
};

test('basemap mode defaults to offline and never creates a second imagery layer', async () => {
  // 默认必须是**离线开发**：晕渲可见、在线影像不可见（页面重新打开也回到离线）。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = onlineCapableCesium();
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, RELIEF_OPTION);
    assert.equal(controller.mode(), 'offline', '默认底图模式必须是离线开发');
    assert.equal(controller.onlineAvailable(), true);
    assert.equal(controller.onlineBlockReason(), null);
    const addedAfterInit = added.length;
    assert.equal(addedAfterInit, 2, '影像 + 晕渲各一层，初始化时一次性建好');

    // 连续 离线→在线→离线 切换：绝不允许重复创建 imagery layer。
    const online = await controller.setMode('online', {
      waitForImageryTiles: async () => ({ ok: true, resolved: 1 }),
    });
    assert.equal(online.ok, true);
    assert.equal(online.mode, 'online');
    await controller.setMode('offline');
    await controller.setMode('online', {
      waitForImageryTiles: async () => ({ ok: true, resolved: 2 }),
    });
    await controller.setMode('offline');
    assert.equal(added.length, addedAfterInit,
      '模式切换只切 show，不得新增 imagery layer（否则会叠加/闪烁/涨内存）');
  } finally {
    globalThis.Cesium = previous;
  }
});

test('online mode is refused with the real reason when online access is disabled', async () => {
  // 任务 B2 的"需要尚未配置的访问凭证"分支：**绝不**假装在线可用。
  // 拒绝必须带上服务端给出的**真实**原因码，且场景保持离线可用。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = offlineCesium(added);
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, {
      status: 'offline_mode', reason_code: 'imagery_online_mode_disabled',
      online_allowed: false, default_source: 'arcgis_world_imagery',
      scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
      sources: [{
        id: 'arcgis_world_imagery', status: 'offline_mode',
        local_url: '/api/visualization-3d/imagery/arcgis_world_imagery/{z}/{x}/{y}',
        tile_size: 256,
      }],
    }, RELIEF_OPTION);

    assert.equal(controller.onlineAvailable(), false);
    assert.equal(controller.onlineBlockReason(), 'imagery_online_mode_disabled');
    const result = await controller.setMode('online');
    assert.equal(result.ok, false, '在线不可用时必须拒绝，而不是假装成功');
    assert.equal(result.mode, 'offline');
    assert.equal(result.reason, 'imagery_online_mode_disabled');
    assert.equal(controller.mode(), 'offline');
    // 离线场景必须仍然可用（晕渲可见），不能出现空白且无提示的画面。
    assert.equal(added[0].show, true);
  } finally {
    globalThis.Cesium = previous;
  }
});

test('online mode falls back to offline on real tile failure or timeout', async () => {
  // 影像请求失败/超时 ⇒ 回退离线底图并给出原因（绝不留下空白且无提示的场景）。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = onlineCapableCesium();
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    for (const reason of ['imagery_tile_request_failed', 'imagery_tile_timeout']) {
      const controller = await createBasemap(viewer, ONLINE_CONFIG, RELIEF_OPTION);
      const result = await controller.setMode('online', {
        waitForImageryTiles: async () => ({ ok: false, reason }),
      });
      assert.equal(result.ok, false);
      assert.equal(result.mode, 'offline');
      assert.equal(result.reason, reason);
      assert.equal(controller.mode(), 'offline');
      assert.equal(controller.status().mode_reason, reason);
      const relief = added[added.length - 1];
      assert.equal(relief.show, true, '回退后晕渲必须可见');
      added.length = 0;
    }
  } finally {
    globalThis.Cesium = previous;
  }
});

test('a superseded online switch cannot overwrite the newer mode', async () => {
  // 连续切换的竞态：旧的异步等待完成后**不得**覆盖用户更新的选择。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = onlineCapableCesium();
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, RELIEF_OPTION);
    let release = null;
    const pending = controller.setMode('online', {
      waitForImageryTiles: () => new Promise((resolve) => { release = resolve; }),
    });
    // 在线仍在等待时，用户切回离线。
    await controller.setMode('offline');
    assert.equal(controller.mode(), 'offline');
    // 现在旧的在线等待才完成 —— 其结果必须被作废。
    release({ ok: true, resolved: 5 });
    const stale = await pending;
    assert.equal(stale.ok, false);
    assert.equal(stale.reason, 'superseded_by_newer_switch');
    assert.equal(controller.mode(), 'offline', '旧请求不得覆盖新模式');
  } finally {
    globalThis.Cesium = previous;
  }
});

test('online refusal reports the credential reason, not a hard-coded offline message', async () => {
  // 原因码必须来自**服务端真实配置**，不能是写死的"离线模式"文案：
  // 已授权联网但凭证缺失时，正确的拒绝码是 ``arcgis_token_missing``。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = offlineCesium(added);
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, {
      status: 'blocked', reason_code: 'arcgis_token_missing', online_allowed: true,
      default_source: 'arcgis_world_imagery',
      scope_bbox_crs84: [121.45, 29.3, 123.05, 30.65],
      sources: [],
    }, RELIEF_OPTION);
    assert.equal(controller.onlineAvailable(), false);
    assert.equal(controller.onlineBlockReason(), 'arcgis_token_missing');
    const result = await controller.setMode('online');
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'arcgis_token_missing');
  } finally {
    globalThis.Cesium = previous;
  }
});

test('the 3D toolbar ships the mode switch and app.js routes it through one entry point', () => {
  // 静态接线契约：HTML 必须提供紧凑的双选开关（默认离线），app.js 必须把它接到
  // **唯一**的模式入口，而不是自己再造一套底图管理。
  const here = path.dirname(fileURLToPath(import.meta.url));
  const root = path.resolve(here, '..');
  const html = fs.readFileSync(
    path.join(root, 'cns_planner/web/visualization-3d.html'), 'utf8');
  const app = fs.readFileSync(
    path.join(root, 'cns_planner/web/js/visualization3d/app.js'), 'utf8');

  // HTML：两个模式选项，默认勾选离线。
  assert.match(html, /id="bmOffline"[^>]*value="offline"[^>]*checked/);
  assert.match(html, /id="bmOnline"[^>]*value="online"/);
  assert.match(html, /id="basemapModeStatus"/);
  // 语义边界必须在页面里写明：只是背景影像模式，不代表业务证据 currentness。
  assert.match(html, /不代表任何 CNS 业务证据的 currentness/);

  // app.js：唯一入口 + 只读证据探针，且**不**新建第二套底图管理器。
  assert.match(app, /S\.applyBasemapMode = applyMode/);
  assert.match(app, /S\.basemapModeState = \(\) => \(/);
  assert.match(app, /S\.basemap\.setMode\(mode\)/);
  assert.match(app, /basemap_mode: typeof S\.basemapModeState === 'function'/);
  // 开关只允许走 applyMode，不允许直接调 setImageryVisible 当第二开关。
  assert.match(app, /imageryCheckbox\.disabled = true/);
  assert.doesNotMatch(app, /imageryCheckbox\.onchange/);
});

test('offline mode discloses missing relief without enabling online access', async () => {
  // 任务 B3-10：切换失败后不能出现完全空白且没有提示的场景。
  // 晕渲缺失时仍禁止在线请求，并如实返回缺失原因。
  const previous = globalThis.Cesium;
  const added = [];
  globalThis.Cesium = onlineCapableCesium();
  try {
    const viewer = {
      scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: (layer) => { added.push(layer); return undefined; } },
      shadows: false, terrainProvider: null, resolutionScale: 1,
    };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, {});   // 无 relief
    const result = await controller.setMode('offline');
    assert.equal(result.ok, false);
    assert.equal(result.reason, 'relief_unavailable');
    assert.equal(controller.status().mode_reason, 'relief_unavailable');
    assert.equal(added[0].show, false, '晕渲缺失也必须遵守离线许可，并显式披露缺失');
  } finally {
    globalThis.Cesium = previous;
  }
});


test('online waiting exceptions roll back to offline and preloading is disabled', async () => {
  const previous = globalThis.Cesium;
  globalThis.Cesium = onlineCapableCesium();
  try {
    const layers = [];
    const viewer = { scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: layer => layers.push(layer) } };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, RELIEF_OPTION);
    const result = await controller.setMode('online', {
      waitForImageryTiles: async () => { throw new Error('transport failed'); },
    });
    assert.equal(result.ok, false);
    assert.equal(controller.mode(), 'offline');
    assert.equal(layers[0].show, false);
    assert.equal(layers[1].show, true);
    assert.equal(viewer.scene.globe.preloadAncestors, false);
    assert.equal(viewer.scene.globe.preloadSiblings, false);
  } finally { globalThis.Cesium = previous; }
});

test('default offline never briefly adds a visible online layer even without relief', async () => {
  const previous = globalThis.Cesium;
  globalThis.Cesium = onlineCapableCesium();
  try {
    const shownOnAdd = [];
    const viewer = { scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: layer => shownOnAdd.push(layer.show) } };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, {});
    assert.deepEqual(shownOnAdd, [false]);
    assert.equal(controller.layerStats().imagery.show, false);
    assert.equal(controller.status().mode_reason, 'relief_unavailable');
  } finally { globalThis.Cesium = previous; }
});


test('a late imagery failure rolls back an already online scene without touching entities', async () => {
  const previous = globalThis.Cesium;
  const cesium = onlineCapableCesium();
  let fail;
  cesium.UrlTemplateImageryProvider = class {
    constructor(options) { Object.assign(this, options);
      this.errorEvent = { addEventListener: fn => { fail = fn; } }; }
    requestImage() { return Promise.resolve({ width: 256 }); }
  };
  globalThis.Cesium = cesium;
  try {
    const entities = [{ id: 'tower' }, { id: 'route' }, { id: 'facility' }, { id: 'protection' }];
    const viewer = { entities, camera: { position: 1 },
      scene: { globe: {}, fog: {}, skyAtmosphere: {} }, imageryLayers: { add: () => {} } };
    let fallback;
    const controller = await createBasemap(viewer, ONLINE_CONFIG, {
      ...RELIEF_OPTION, onModeFallback: reason => { fallback = reason; },
    });
    await controller.setMode('online', { waitForImageryTiles: async () => ({ ok: true }) });
    fail({ message: 'Failed to obtain image tile X: 3438 Y: 1689 Level: 12.',
      error: { response: '{"code":"imagery_budget_exhausted"}' } });
    assert.equal(controller.mode(), 'offline');
    assert.equal(fallback, 'imagery_budget_exhausted');
    assert.equal(viewer.entities, entities);
    assert.deepEqual(viewer.camera, { position: 1 });
  } finally { globalThis.Cesium = previous; }
});


test('default waiter requires a fresh image and permits retry after an earlier failure', async () => {
  const previous = globalThis.Cesium;
  const cesium = onlineCapableCesium();
  let reject = true;
  cesium.UrlTemplateImageryProvider = class {
    constructor(options) { Object.assign(this, options); }
    requestImage() { return reject ? Promise.reject(new Error('failed'))
      : Promise.resolve({ width: 256 }); }
  };
  cesium.ImageryLayer = class {
    constructor(provider) { this._imageryProvider = provider; this.show = true; }
    get imageryProvider() { return this._imageryProvider; }
  };
  globalThis.Cesium = cesium;
  try {
    const layers = [];
    const viewer = { scene: { globe: {}, fog: {}, skyAtmosphere: {} },
      imageryLayers: { add: layer => layers.push(layer) } };
    const controller = await createBasemap(viewer, ONLINE_CONFIG, RELIEF_OPTION);
    const provider = layers[0].imageryProvider;
    await assert.rejects(provider.requestImage());
    reject = false;
    const pending = controller.setMode('online', { intervalMs: 1, timeoutMs: 100 });
    await provider.requestImage();
    assert.equal((await pending).ok, true, 'old failures must not poison a new switch');
    await controller.setMode('offline');
    const noFreshImage = await controller.setMode('online', { intervalMs: 1, timeoutMs: 5 });
    assert.equal(noFreshImage.ok, false, 'historical downloads alone cannot prove this view');
    assert.equal(noFreshImage.reason, 'imagery_tile_timeout');
    assert.equal(layers[0].show, false);
    assert.equal(layers[1].show, true);
  } finally { globalThis.Cesium = previous; }
});
