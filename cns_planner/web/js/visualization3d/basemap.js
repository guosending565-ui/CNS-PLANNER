export const BASE_LAYER_DEFAULTS = Object.freeze({
  imagery: true,
  terrain: true,
  labels: false,
});

export const BUSINESS_LAYER_DEFAULTS = Object.freeze({
  routeDemo: false,
  routeCanonical: false,
  towersKnown: false,
  towersUnknown: false,
  facilities: false,
  buildings: false,
  grid: false,
  communication: false,
  rid: false,
  radar: false,
  protection: false,
  intrusion: false,
});

//: 海面法线贴图（8×8，切线空间，多方向轻微扰动）。
//:
//: 为什么不能只用 1×1 中性像素：实测（同相机、同场景 A/B）单个 1×1 法线会让
//: `showWaterEffect` 把整片海面推到近乎纯白——地图主体亮度均值由 126 升到 249、
//: 93.8% 的像素进入 >200 的高亮区间，海陆对比被完全冲掉。原因是 1×1 法线在整个
//: 海面产生**完全一致**的镜面响应，缺少随视角变化的细微扰动。
//: 这里改用小幅多方向法线图，让高光自然衰减。
const WATER_NORMAL_MAP = 'data:image/png;base64,'
  + 'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAZUlEQVR42r2MrQ7AMAgG+6go1Bxq'
  + 'DjWFm6rqYzbpz75Bs9nJiQvkuJBUgaIdWjpKqaDYw1FdLil1kB/eMKLlnjAxN2SHs08nJEnDmTs2'
  + 'm0jGA7JNZBtgGSswuyBy4dj9Q4gvfghudmK/Srttz1MAAAAASUVORK5CYII=';

//: 影像瓦片**真实**请求计数（只读包装，不改变任何请求语义）。
//:
//: 面板里的 ``imagery_failures`` 只统计 Cesium 广播的失败事件，无法回答
//: "本视角到底请求/完成了几张影像瓦片"——而这是本轮计费与成功率的必需证据。
const IMAGERY_TILE_COUNTERS = new WeakMap();

/** 包一层 ``requestImage``，只计数成功/失败；非函数时静默跳过（测试替身容错）。
 *
 * 实测（Cesium 1.126 vendored 包）：取图方法名是原型上的 ``requestImage``，
 * 而**不是** ``_requestImage``；URL 也从 ``_url`` 改成了原型 getter ``url``。
 * 首版按 ``_requestImage`` 包装因此什么都没计到（计数恒为 null），
 * 但底图其实已经在正常加载——这类"证据为空"必须靠真实 API 探测而不是猜。
 *
 * ``resolved``/``accepted`` 只在**真正拿到 HTTP 成功结果**时递增：Cesium 内部的 tile
 * 加载失败重试、请求去重与丢弃策略会让"方法被调用次数"远大于真实网络瓦片数
 * （实测同一会话调用 65568 次而网络层只有 311 张瓦片），因此调用次数
 * （``request_image_calls``）不能当作"已下载瓦片数"，面板与证据只用 ``accepted``。
 *
 * T3D-2A 修正（``07_defects_and_handoff.md`` §1.4）：``requestImage`` 返回**空结果**
 * 是 Cesium 内部**主动丢弃/取消**瓦片的正常调度行为，不是网络失败。上一轮把它
 * 计进 ``rejected`` 并由面板显示成红字"瓦片加载失败"，把用户引向错误方向。
 * 现在三态严格分开：
 *
 * * ``accepted``  —— 真的拿到图像；
 * * ``discarded`` —— promise 正常完成但返回空（Cesium 丢弃，**不是**失败）；
 * * ``rejected``  —— promise 被拒绝（真实失败：网络/代理/上游）。
 */
export function attachImageryTileCounters(provider, onProgress) {
  const existing = provider && provider.requestImage;
  const source = (typeof existing === 'function' ? existing : null)
    || (provider && provider._requestImage);
  if (typeof source !== 'function') return null;
  const original = source.bind(provider);
  const counters = { request_image_calls: 0, accepted: 0, resolved: 0, rejected: 0,
                     discarded: 0, last_error: null, last_discard: null };
  IMAGERY_TILE_COUNTERS.set(provider, counters);
  provider.requestImage = function countedRequestImage(...args) {
    counters.request_image_calls += 1;
    const result = original(...args);
    if (result && typeof result.then === 'function') {
      return result.then(
        (value) => {
          if (value) {
            // 真实的图像结果（非 undefined）才算一次已完成的瓦片。
            counters.accepted += 1;
            counters.resolved = counters.accepted;
            if (typeof onProgress === 'function') onProgress(counters.accepted);
          } else {
            // 空结果 = Cesium 主动丢弃/取消该瓦片，**不是**加载失败。
            counters.discarded += 1;
            counters.last_discard = 'imagery_tile_discarded_by_cesium';
          }
          return value;
        },
        (error) => {
          counters.rejected += 1;
          counters.last_error = String((error && error.message) || error).slice(0, 200);
          throw error;
        });
    }
    return result;
  };
  return counters;
}

/** 读取某个 imagery layer 的瓦片计数与来源信息（供只读场景探针使用）。 */
export function imageryStats(layer) {
  if (!layer) return null;
  const provider = layer._imageryProvider || null;
  const counters = provider ? IMAGERY_TILE_COUNTERS.get(provider) : null;
  const tilingScheme = provider && provider.tilingScheme;
  return {
    request_image_calls: counters ? counters.request_image_calls : null,
    accepted: counters ? counters.accepted : null,
    resolved: counters ? counters.resolved : null,
    //: ``rejected`` 才是**真实失败**；``discarded`` 是 Cesium 的正常丢弃调度。
    rejected: counters ? counters.rejected : null,
    discarded: counters ? counters.discarded : null,
    last_error: counters ? counters.last_error : null,
    last_discard: counters ? counters.last_discard : null,
    ready: Boolean(layer.ready),
    show: Boolean(layer.show),
    // ``_url`` 在 1.126 已不存在；优先读原型 getter ``url``。
    url_template: provider
      ? (typeof provider.url === 'string' ? provider.url
        : (typeof provider._url === 'string' ? provider._url : null))
      : null,
    // vendored/minified 包会把类名压成两字符（实测影像=Cy、地理=H0），
    // 因此同时记录"是否 WebMercator 方案"这个**语义**判据，避免只看类名。
    tiling_scheme: tilingScheme ? tilingScheme.constructor.name : null,
    tiling_scheme_is_webmercator: Boolean(
      tilingScheme && typeof Cesium !== 'undefined'
      && Cesium.WebMercatorTilingScheme
      && tilingScheme instanceof Cesium.WebMercatorTilingScheme),
    tiling_scheme_is_geographic: Boolean(
      tilingScheme && typeof Cesium !== 'undefined'
      && Cesium.GeographicTilingScheme
      && tilingScheme instanceof Cesium.GeographicTilingScheme),
    maximum_level: provider && provider.maximumLevel !== undefined
      ? provider.maximumLevel : null,
    minimum_level: provider && provider.minimumLevel !== undefined
      ? provider.minimumLevel : null,
    provider_credit: provider && provider.credit
      ? String(provider.credit.html || provider.credit) : null,
  };
}

/** 解析 Cesium 的 "Failed to obtain image tile X: .. Y: .. Level: .." 消息。
 *
 * 该消息是 Cesium 1.126 ``ImageryLayer`` 在取图失败时抛出的固定文案，坐标是
 * **WebMercator XYZ**（与影像瓦片同一网格）。解析失败返回 ``null``。
 */
export function parseImageryTileError(message) {
  const match = /X:\s*(-?\d+)\s*Y:\s*(-?\d+)\s*Level:\s*(-?\d+)/i.exec(String(message || ''));
  if (!match) return null;
  return { x: Number(match[1]), y: Number(match[2]), z: Number(match[3]) };
}

/** WebMercator XYZ 瓦片的地理 bbox（与后端 ``_tile_bounds`` 同式）。 */
export function webMercatorTileBounds(z, x, y) {
  const n = 2 ** z;
  const west = (x / n) * 360 - 180;
  const east = ((x + 1) / n) * 360 - 180;
  const north = (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
  const south = (Math.atan(Math.sinh(Math.PI * (1 - (2 * (y + 1)) / n))) * 180) / Math.PI;
  return { west, south, east, north };
}

/** 该瓦片是否落在受控展示范围之外（与后端 ``_intersects_zhoushan`` 同判据）。
 *
 * 后端对范围外瓦片返回 503 ``imagery_tile_outside_scope`` —— 这是**正常**的
 * （Cesium 会请求矩形边界外的瓦片），不是底图故障。前端可以**独立复算**这个几何
 * 事实，因此不需要靠猜测或靠错误文案分类。
 */
export function tileOutsideScope(message, scopeBbox) {
  const tile = parseImageryTileError(message);
  if (!tile || !Array.isArray(scopeBbox) || scopeBbox.length < 4) return null;
  if (!Number.isInteger(tile.z) || tile.z < 0 || tile.z > 30) return null;
  const limit = 2 ** tile.z;
  if (tile.x < 0 || tile.y < 0 || tile.x >= limit || tile.y >= limit) return null;
  const bounds = webMercatorTileBounds(tile.z, tile.x, tile.y);
  const [minLon, minLat, maxLon, maxLat] = scopeBbox;
  return !(bounds.east >= minLon && bounds.west <= maxLon
    && bounds.north >= minLat && bounds.south <= maxLat);
}

export class LazyLayerRegistry {
  constructor(loaders = {}) {
    this.loaders = { ...loaders };
    this.entries = new Map();
  }

  async setVisible(id, visible) {
    let entry = this.entries.get(id);
    if (!entry && visible) {
      const loader = this.loaders[id];
      if (typeof loader !== 'function') return false;
      const entities = await loader();
      entry = { entities: Array.isArray(entities) ? entities : [], loaded: true };
      this.entries.set(id, entry);
    }
    if (!entry) return true;
    for (const entity of entry.entities) entity.show = Boolean(visible);
    return true;
  }

  entities(id) {
    const entry = this.entries.get(id);
    return entry ? entry.entities : [];
  }

  loaded(id) {
    return this.entries.has(id);
  }
}

export function createLocalViewer(container, options = {}) {
  const globe = new Cesium.Globe(Cesium.Ellipsoid.WGS84);
  // The vendored Cesium bundle intentionally excludes optional texture assets.
  // Use an inlined 8×8 normal map instead of fetching a missing file: a 1×1 neutral
  // pixel makes `showWaterEffect` blow the whole sea out to near-white (measured:
  // mean luminance 126 → 249), so the map must carry small multi-directional detail.
  globe.oceanNormalMapUrl = WATER_NORMAL_MAP;
  return new Cesium.Viewer(container, {
    ...options,
    globe,
    skyBox: false,
    moon: false,
  });
}

export async function createBasemap(viewer, config, options = {}) {
  const terrainUrl = options.terrainUrl || '/api/visualization-3d/terrain';
  const waterMaskAvailable = options.waterMaskAvailable === true;
  const terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl(terrainUrl, {
    requestWaterMask: waterMaskAvailable,
    requestVertexNormals: false,
  });
  viewer.terrainProvider = terrainProvider;

  const scene = viewer.scene;
  scene.verticalExaggeration = 1.0;
  scene.globe.enableLighting = true;
  scene.globe.depthTestAgainstTerrain = true;
  scene.globe.showWaterEffect = waterMaskAvailable;
  scene.globe.maximumScreenSpaceError = 3.0;
  scene.skyAtmosphere.show = true;
  scene.fog.enabled = true;
  scene.fog.density = 0.00018;
  scene.globe.showGroundAtmosphere = true;
  scene.globe.shadows = Cesium.ShadowMode.RECEIVE_ONLY;
  viewer.shadows = true;

  let imageryLayer = null;
  let labelsLayer = null;
  let reliefLayer = null;
  const defaultId = config && config.default_source;
  const source = (config && config.sources || []).find((row) => row.id === defaultId);
  // 离线开发模式（``imagery/config.online_allowed === false``）：**绝不**创建影像
  // provider。Cesium 对 show=false 的图层不取瓦片，但创建一个可用的 provider 仍然
  // 意味着"随时可能发请求"；离线模式必须从**根上**不建立这条链路。
  const onlineAllowed = config && config.online_allowed !== false;
  const imageryUsable = Boolean(
    onlineAllowed && config && config.status !== 'blocked'
    && source && source.status !== 'blocked');
  if (imageryUsable) {
    const provider = new Cesium.UrlTemplateImageryProvider({
      url: source.local_url,
      rectangle: Cesium.Rectangle.fromDegrees(...config.scope_bbox_crs84),
      tileWidth: source.tile_size || 256,
      tileHeight: source.tile_size || 256,
      minimumLevel: 4,
      maximumLevel: (source.zoom || [4, 20])[1],
      credit: new Cesium.Credit(source.attribution || 'Tiles © Esri and data providers'),
    });
    // 计费/成功率证据需要**真实**的影像瓦片请求数，而不是仅凭面板推测。
    // Cesium 1.126 的 ``ImageryProvider`` 以 ``_requestImage`` 实现模板取图；这里包一层
    // 只读计数，不改任何请求语义（同一 URL、同一参数，仅计数）。
    attachImageryTileCounters(provider, options.onImageryProgress);
    // 注意：Cesium 的 ``ImageryLayerCollection.prototype.add()`` **返回 undefined**
    // （1.126 实测）。必须显式持有图层对象，不能用返回值当引用。
    imageryLayer = new Cesium.ImageryLayer(provider);
    viewer.imageryLayers.add(imageryLayer);
  }

  // 合法影像不可用时，用**明确标注**的 FABDEM 地形晕渲表达真实起伏。
  // 它绝不是卫星影像：既不冒充影像，也不把影像验收标为通过；仅供人工判读地形。
  //
  // T3D-BMAP-2 变更：影像**已授权**时也创建该图层，但默认隐藏、只作为用户主动切换的
  // 地形判读图层。这样可以做"同相机 satellite on/off"真实对照，而不必改任何 URL。
  const relief = options.relief || null;
  if (relief && relief.status === 'available' && relief.url_template) {
    const provider = new Cesium.UrlTemplateImageryProvider({
      url: relief.url_template,
      // **必须**显式指定地理（EPSG:4326）瓦片方案：UrlTemplateImageryProvider 默认按
      // **WebMercator** 计算行列号，而本服务的 relief 端点与地形 layer.json 一样使用
      // 地理 2×1 TMS 网格。实测证据：z=6 时 Cesium 默认请求 x=53/y=25（WebMercator 坐标），
      // 而目标范围（121–123E, 29–32N）在地理网格上是 x=107/y=42–43，两者完全错位，
      // 服务端一律判为越界，表现为满屏 4xx 且底图空白。
      tilingScheme: new Cesium.GeographicTilingScheme(),
      // 服务端 relief 端点采用**顶原点**（XYZ/slippy）y，与 Cesium 地理瓦片方案一致，
      // 因此这里**不能**再 reverseY（翻转会再次落到错误半球）。
      // 把晕渲图层限制在 FABDEM 覆盖范围内，避免请求无数据的全球低层级瓦片。
      rectangle: relief.extent
        ? Cesium.Rectangle.fromDegrees(...relief.extent)
        : undefined,
      tileWidth: relief.tile_size || 256,
      tileHeight: relief.tile_size || 256,
      minimumLevel: relief.minzoom === undefined ? 6 : relief.minzoom,
      maximumLevel: relief.maxzoom || 13,
      credit: new Cesium.Credit(
        relief.attribution || 'FABDEM 30 m + EGM2008 地形晕渲（非卫星影像）'),
    });
    // 绝不能依赖瓦片请求就显示：影像可用时它必须默认关闭，且**不产生任何请求**
    // （Cesium 对 show=false 的图层不取瓦片，实测请求数为 0）。
    // 同上：``add()`` 返回 undefined。早期实现把返回值赋给 reliefLayer，于是图层
    // **已经渲染**（实测 imagery_layers=1、瓦片请求正常），但 status().relief 报
    // 'blocked'、reliefAvailable() 为 false、setReliefVisible() 直接失败，版权栏也
    // 因此丢掉"FABDEM 地形晕渲（非卫星影像）"这条来源标注——证据与现实不符。
    reliefLayer = new Cesium.ImageryLayer(provider);
    viewer.imageryLayers.add(reliefLayer);
    // 离线开发模式（影像被禁止）下，晕渲就是**默认底图**：否则画面只剩基色，
    // 三维功能测试会被误判成"因为没有卫星影像而失败"。在线模式下仍默认隐藏。
    reliefLayer.show = !imageryUsable;
    if (!imageryLayer) {
      // 只有影像缺失时才调低地球基色，让灰阶晕渲的明暗层次可读。
      scene.globe.baseColor = Cesium.Color.fromCssColorString('#0b1a26');
    }
  }

  const state = {
    imagery: imageryLayer ? 'available' : 'blocked',
    //: 影像瓦片**真实**失败次数与最后一条原因。Cesium 不抛异常，只广播 errorEvent；
    //: 不订阅它就会出现"面板显示已配置、底图全蓝"的静默失败（实测 11/11 瓦片 503）。
    imagery_failures: 0,
    imagery_last_error: null,
    //: Cesium **主动丢弃/取消**瓦片的次数（正常调度，不是失败）。必须与
    //: ``imagery_failures`` 分开计数，否则面板会把丢弃报成红字失败。
    imagery_discards: 0,
    //: 落在受控展示范围之外的瓦片（后端 503 ``imagery_tile_outside_scope``）。
    //: 这是**正常的**：Cesium 会请求图层矩形边界外的瓦片。同样不得报成失败。
    imagery_out_of_scope: 0,
    relief: reliefLayer ? 'available' : 'blocked',
    relief_is_satellite_imagery: false,
    terrain: 'available',
    labels: 'blocked',
    quality: 'normal',
  };
  const imageryProvider = imageryLayer ? imageryLayer.imageryProvider : null;
  const scopeBbox = (config && config.scope_bbox_crs84) || null;
  /** 三类事件的**可独立验证**分类（不靠猜测，也不靠错误文案措辞）。
   *
   * 实测（T3D-BMAP-2 §1.4）：网络层失败 0、代理拒绝 0，但 Cesium 仍会触发
   * ``errorEvent``，面板因此显示红字"瓦片加载失败"，把用户引向错误方向。
   * 现在按以下判据区分：
   *
   * 1. ``out_of_scope``：瓦片坐标可解析且**几何上**落在受控 bbox 之外
   *    （后端范围守卫的正常拒绝，前端可独立复算）；
   * 2. ``discarded``：provider 计数显示"有丢弃、无真实拒绝"；
   * 3. ``failed``：其余（真实网络/代理/上游失败）。
   */
  const classifyImageryError = (message) => {
    if (tileOutsideScope(message, scopeBbox) === true) return 'out_of_scope';
    const counters = imageryProvider
      ? IMAGERY_TILE_COUNTERS.get(imageryProvider) : null;
    const rejected = counters ? counters.rejected : null;
    const discarded = counters ? counters.discarded : null;
    if (rejected !== null && discarded !== null && rejected === 0 && discarded > 0) {
      return 'discarded';
    }
    return 'failed';
  };
  if (imageryProvider && imageryProvider.errorEvent
      && typeof imageryProvider.errorEvent.addEventListener === 'function') {
    // 测试替身没有 errorEvent，必须容错；真实 Cesium 的 provider 一定有。
    imageryProvider.errorEvent.addEventListener((error) => {
      const message = String((error && error.message) || error);
      const kind = classifyImageryError(message);
      if (kind === 'out_of_scope') {
        state.imagery_out_of_scope += 1;
        if (typeof options.onImageryOutOfScope === 'function') {
          options.onImageryOutOfScope(state.imagery_out_of_scope, message);
        }
        return;
      }
      if (kind === 'discarded') {
        state.imagery_discards += 1;
        if (typeof options.onImageryDiscard === 'function') {
          options.onImageryDiscard(state.imagery_discards, message);
        }
        return;
      }
      state.imagery_failures += 1;
      state.imagery_last_error = message;
      if (typeof options.onImageryFailure === 'function') {
        options.onImageryFailure(state.imagery_failures, state.imagery_last_error);
      }
    });
  }
  const controller = {
    /** 影像图层开关。离线模式下**没有**该图层，返回 false（调用方须据此回退勾选）。 */
    setImageryVisible(value) {
      if (!imageryLayer) return false;
      imageryLayer.show = Boolean(value);
      return true;
    },
    imageryAvailable() { return Boolean(imageryLayer); },
    onlineAllowed() { return onlineAllowed; },
    setTerrainVisible(value) {
      scene.globe.show = Boolean(value);
      return true;
    },
    /** 地形晕渲图层开关（独立于卫星影像；影像可用时默认关闭、按需切换）。 */
    setReliefVisible(value) {
      if (!reliefLayer) return false;
      reliefLayer.show = Boolean(value);
      return true;
    },
    reliefAvailable() { return Boolean(reliefLayer); },
    /** 只读图层证据：影像/晕渲各自的真实瓦片计数、方案与版权（供验收采集）。 */
    layerStats() {
      return {
        imagery: imageryStats(imageryLayer),
        relief: reliefLayer ? {
          ...imageryStats(reliefLayer),
          is_satellite_imagery: false,
        } : null,
      };
    },
    setLabelsVisible(value) {
      if (!labelsLayer) return false;
      labelsLayer.show = Boolean(value);
      return true;
    },
    setQuality(value) {
      if (!['normal', 'high'].includes(value)) return false;
      state.quality = value;
      scene.globe.maximumScreenSpaceError = value === 'high' ? 1.5 : 3.0;
      viewer.resolutionScale = value === 'high'
        ? Math.min(globalThis.devicePixelRatio || 1, 1.5) : 1.0;
      return true;
    },
    status() { return { ...state }; },
  };
  controller.setTerrainVisible(BASE_LAYER_DEFAULTS.terrain);
  controller.setLabelsVisible(BASE_LAYER_DEFAULTS.labels);
  // 默认底图：在线模式为卫星影像；离线模式为 FABDEM 晕渲（已在上面打开）。
  controller.setImageryVisible(imageryUsable && BASE_LAYER_DEFAULTS.imagery);
  return controller;
}
