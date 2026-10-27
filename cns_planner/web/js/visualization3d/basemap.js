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
  // Round38-T3D4：站址 / 设备四类身份与固定 1000 m 保障目标带。
  // 项目打开时**默认只展示底图**，专题图层一律默认关闭（既有约定）。
  cnsSites: false,
  fixedCorridor: false,
  flightProtectionResearch: false,
  // R7-B：P16-V2 假设站址图层，默认关闭；与正式设备图层完全独立。
  assumptionSites: false,
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
      const loaded = await loader();
      // loader 可以返回**实体数组**（既有全部图层），也可以返回
      // ``{ entities, setVisible }``：后者用于需要"整体开关 + 逐组开关"两个层级的图层
      // （Radar 每个真实面阵单独成组，可单独关闭）。数组路径行为与修复前逐字一致。
      if (loaded && !Array.isArray(loaded) && Array.isArray(loaded.entities)) {
        entry = {
          entities: loaded.entities,
          applyVisibility: typeof loaded.setVisible === 'function' ? loaded.setVisible : null,
          loaded: true,
        };
      } else {
        entry = { entities: Array.isArray(loaded) ? loaded : [], loaded: true };
      }
      this.entries.set(id, entry);
    }
    if (!entry) return true;
    if (typeof entry.applyVisibility === 'function') {
      entry.applyVisibility(Boolean(visible));
      return true;
    }
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
  //: 海面效果（``showWaterEffect``）**不能**对所有底图一视同仁地打开。
  //:
  //: 实测（Round38-OFFLINE-IMAGERY 色彩消融，同相机 A/B，画面均值）：
  //:
  //: * 真彩色卫星影像 + 水效**开**：RGB 126/139/132，B−R = **+5.6**，亮度 132；
  //: * 真彩色卫星影像 + 水效**关**：RGB 93/90/67，B−R = **−26.2**，亮度 84；
  //: * 同一景源影像本身：B−R = −27.1，亮度 85。
  //:
  //: 也就是说，水效会把舟山近岸**浑浊黄绿色**海水整体染成偏蓝发白，与真实影像
  //: 明显不符（逐项消融证明它是**唯一**致因：单独关闭光照 / 地面大气 / 雾均无变化）。
  //: 因此水效只在**灰阶地形晕渲**下启用（那里它提供有价值的海陆区分），
  //: 一旦显示真彩色影像就必须关闭；由 ``syncWaterEffect()`` 随模式切换。
  const waterMaskUsable = waterMaskAvailable;
  scene.globe.showWaterEffect = waterMaskAvailable;
  scene.globe.maximumScreenSpaceError = 3.0;
  // Only request the current view; keep the server's request budget unchanged.
  scene.globe.preloadAncestors = false;
  scene.globe.preloadSiblings = false;
  scene.skyAtmosphere.show = true;
  scene.fog.enabled = true;
  scene.fog.density = 0.00018;
  scene.globe.showGroundAtmosphere = true;
  scene.globe.shadows = Cesium.ShadowMode.RECEIVE_ONLY;
  viewer.shadows = true;

  let imageryLayer = null;
  let labelsLayer = null;
  let reliefLayer = null;
  //: Round38-OFFLINE-IMAGERY：本地离线卫星影像图层。
  //:
  //: 与在线影像**并列**的第二个影像图层，但完全独立：本地端点、零上游、零预算、
  //: 无凭证依赖。它必须在 ``createBasemap`` 时一次性建好，模式切换只切 ``show``
  //: ——否则每次切换都会新增图层并增长内存（这是既有实现的硬约束，必须沿用）。
  let offlineImageryLayer = null;
  const defaultId = config && config.default_source;
  const source = (config && config.sources || []).find((row) => row.id === defaultId);
  // 离线影像源：真实 config 的 ``sources`` 里带它；旧测试替身没有 ⇒ 不建图层。
  const offlineSource = (config && config.sources || []).find(
    (row) => row.id === 'offline_zhoushan_imagery');
  const offlineUsable = Boolean(
    offlineSource && offlineSource.status !== 'blocked'
    && offlineSource.local_url
    && Array.isArray(offlineSource.coverage_crs84)
    && offlineSource.coverage_crs84.length === 4);
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
    imageryLayer.show = false;
    viewer.imageryLayers.add(imageryLayer);
  }

  // 本地离线卫星影像图层。**不**受 ``online_allowed`` 约束：它是磁盘上的本地
  // 瓦片，既不触网也不消耗预算，因此在线授权关闭时它照常可用（这正是本轮的目的）。
  // ``rectangle`` 必须取数据真实覆盖范围：数据范围之外的瓦片本地不存在，限制矩形
  // 可以避免 Cesium 去请求它们（否则会在数据边缘产生一批无意义的 404）。
  if (offlineUsable) {
    const provider = new Cesium.UrlTemplateImageryProvider({
      url: offlineSource.local_url,
      rectangle: Cesium.Rectangle.fromDegrees(...offlineSource.coverage_crs84),
      tileWidth: offlineSource.tile_size || 256,
      tileHeight: offlineSource.tile_size || 256,
      minimumLevel: (offlineSource.zoom || [9, 14])[0],
      // 上限必须取**数据真实**的最细层级：这里没有升采样，虚报更高层级只会让
      // Cesium 请求一批不存在的瓦片，把画面拖成空白。
      maximumLevel: (offlineSource.zoom || [9, 14])[1],
      credit: new Cesium.Credit(
        offlineSource.attribution || 'Contains modified Copernicus Sentinel data'),
    });
    attachImageryTileCounters(provider, options.onImageryProgress);
    offlineImageryLayer = new Cesium.ImageryLayer(provider);
    offlineImageryLayer.show = false;
    viewer.imageryLayers.add(offlineImageryLayer);
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
    // 页面打开默认使用离线底图（优先本地影像，其次晕渲）；在线影像只由显式模式切换启用。
    reliefLayer.show = true;
    if (!imageryLayer && !offlineImageryLayer) {
      // 只有**任何**卫星影像都缺失时才调低地球基色，让灰阶晕渲的明暗层次可读。
      // 本地离线影像存在时绝不能调暗：那会把真彩色影像压成暗色（曾把海面推成近黑）。
      scene.globe.baseColor = Cesium.Color.fromCssColorString('#0b1a26');
    }
  }

  // 底图**模式**（离线开发 / 在线演示）的唯一结构性判据。
  //
  // 必须与"影像图层是否存在"分开看：``online_allowed === false`` 是**授权决策**，
  // 不是加载失败。此时按既有约定 Cesium 侧**根本不会**建立 imagery provider
  // （离线模式必须从根上不建立这条链路），因此"切到在线"在结构上不可能成功。
  // 开关必须如实置灰并给出真实原因，而不是让用户点一下、看到空白、再猜发生了什么。
  const onlineBlockReason = (() => {
    if (!onlineAllowed) return 'imagery_online_mode_disabled';
    if (config && config.status === 'blocked') {
      return config.reason_code || 'imagery_credential_unavailable';
    }
    if (!source) return 'imagery_source_unavailable';
    if (source.status === 'blocked') return 'imagery_source_blocked';
    if (!imageryLayer) return 'imagery_layer_unavailable';
    return null;
  })();

  const state = {
    imagery: imageryLayer ? 'available' : 'blocked',
    //: 底图模式：``offline``（默认，FABDEM 晕渲）或 ``online``（卫星影像）。
    //: **只**表示背景影像加载模式；与任何 CNS 业务证据的 currentness、
    //: 正式规划有效性、图层勾选状态均无关。
    //:
    //: Round38-OFFLINE-IMAGERY：对外新增三值 ``basemap_mode``
    //: （``offline_relief`` / ``offline_imagery`` / ``online``）。``mode`` 这个
    //: 二值字段**保持不变**，以免破坏既有调用方与回归：它表达的是"是否在触网"，
    //: 而 ``basemap_mode`` 才表达"当前显示哪一种底图"。
    mode: 'offline',
    //: 三值底图模式（本轮新增的**唯一**模式真相来源）。
    basemap_mode: 'offline_relief',
    provider_name: (source && (source.label || source.id)) || null,
    //: 最近一次模式拒绝/回退的**真实**原因码（正常时为 null）。
    mode_reason: null,
    //: 单调递增的模式序号：连续切换时旧的异步等待一律作废，不允许覆盖新模式。
    mode_serial: 0,
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
    //: 本地离线影像**没有这一块瓦片**（后端 404 ``offline_imagery_tile_missing``）。
    //: 与 ``imagery_out_of_scope`` 语义不同：前者是"本地数据本来就没有"，
    //: 属正常状态，**绝不**触发重试或回退。
    offline_tile_missing: 0,
    //: 本地离线影像真实到达的瓦片数（用于证明"离线模式确实在工作"）。
    offline_imagery_resolved: 0,
    relief: reliefLayer ? 'available' : 'blocked',
    relief_is_satellite_imagery: false,
    terrain: 'available',
    labels: 'blocked',
    quality: 'normal',
    //: 海面效果的真实状态与启用策略（供验收核对"真彩色影像下必须为 false"）。
    water_effect: waterMaskAvailable,
    water_effect_policy: 'relief_only_not_on_true_color_imagery',
  };
  const imageryProvider = imageryLayer ? imageryLayer.imageryProvider : null;
  const offlineProvider = offlineImageryLayer ? offlineImageryLayer.imageryProvider : null;
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
      let reason = 'imagery_tile_request_failed';
      try {
        const response = JSON.parse(error && error.error && error.error.response || '{}');
        if (['imagery_budget_exhausted', 'imagery_credential_rejected',
             'imagery_online_mode_disabled'].includes(response.code)) reason = response.code;
      } catch { /* Cesium may provide only a status code. */ }
      state.mode_reason = reason;
      if (state.mode === 'online') {
        // 在线失败的回退必须与 ``setMode`` 走**同一**条判据：优先回到仍可用且
        // 不触网的本地离线影像，只有它也不可用时才退到灰阶晕渲。两条路径若不一致，
        // 就会出现"异步失败后底图悄悄变成灰阶晕渲"的静默降级。
        applyOfflineImageryView();
        state.mode_serial += 1;
        state.mode = 'offline';
        state.mode_reason = reason;
        if (typeof options.onModeFallback === 'function') options.onModeFallback(reason);
      }
      if (typeof options.onImageryFailure === 'function') {
        options.onImageryFailure(state.imagery_failures, state.imagery_last_error);
      }
    });
  }
  /** 在线影像是否**结构上**可用；不可用时给出真实原因码（不是"加载失败"）。 */
  const onlineUsable = () => onlineBlockReason === null;

  /** 本地离线影像是否结构上可用（图层存在即可用；它不依赖任何授权）。 */
  const offlineImageryUsable = () => Boolean(offlineImageryLayer);

  //: 离线影像的 errorEvent 订阅。
  //:
  //: 关键差异（决定"不产生持续缺失瓦片请求"这条验收）：本地瓦片缺失返回
  //: ``404 offline_imagery_tile_missing``，这是**数据边界**的正常状态，必须
  //: 计入 ``offline_tile_missing`` 并**静默跳过**——绝不累加 ``imagery_failures``、
  //: 绝不触发模式回退。把它当作在线失败处理会让离线底图在数据边缘整片退出。
  if (offlineProvider && offlineProvider.errorEvent
      && typeof offlineProvider.errorEvent.addEventListener === 'function') {
    offlineProvider.errorEvent.addEventListener((error) => {
      const message = String((error && error.message) || error);
      const counters = IMAGERY_TILE_COUNTERS.get(offlineProvider);
      const rejected = counters ? counters.rejected : null;
      const discarded = counters ? counters.discarded : null;
      if (rejected !== null && discarded !== null && rejected === 0 && discarded > 0) {
        state.imagery_discards += 1;
        if (typeof options.onImageryDiscard === 'function') {
          options.onImageryDiscard(state.imagery_discards, message);
        }
        return;
      }
      state.offline_tile_missing += 1;
      if (typeof options.onOfflineTileMissing === 'function') {
        options.onOfflineTileMissing(state.offline_tile_missing, message);
      }
    });
  }

  /** 轮询真实瓦片计数直到"有新瓦片到达 / 真实失败 / 超时"。
   *
   * 判据必须是**真实到达的图像结果**（``resolved``），而不是"图层已 show"——
   * 后者在凭证被拒、上游 503、代理不可达时同样为真，会把空白底图报成"已加载"。
   */
  function defaultWaitForImageryTiles(context) {
    const timeoutMs = context.timeoutMs || 20000;
    const intervalMs = context.intervalMs || 400;
    const startedAt = Date.now();
    return new Promise((resolve) => {
      const tick = () => {
        const stats = imageryStats(context.layer);
        const resolved = stats && stats.resolved ? stats.resolved : 0;
        const rejected = stats && stats.rejected ? stats.rejected : 0;
        if (rejected > context.baselineRejected) {
          resolve({ ok: false, reason: state.mode_reason || 'imagery_tile_request_failed', rejected });
          return;
        }
        if (state.mode_serial !== context.serial) {
          resolve({ ok: false, reason: 'superseded_by_newer_switch' });
          return;
        }
        if (resolved > context.baselineResolved) {
          resolve({ ok: true, resolved });
          return;
        }
        if (Date.now() - startedAt >= timeoutMs) {
          resolve({ ok: false, reason: 'imagery_tile_timeout' });
          return;
        }
        setTimeout(tick, intervalMs);
      };
      setTimeout(tick, intervalMs);
    });
  }

  /** 按当前底图模式同步海面效果。
   *
   * 真彩色影像（在线或离线）下**必须**关闭：实测水效会把舟山近岸浑浊黄绿色海水
   * 染成偏蓝发白（B−R 由 −26.2 变为 +5.6），与真实影像明显不符。
   * 灰阶地形晕渲下保留（它提供有价值的海陆区分）。
   */
  function syncWaterEffect() {
    if (!waterMaskUsable) {
      scene.globe.showWaterEffect = false;
      state.water_effect = false;
      return false;
    }
    const satelliteShown = Boolean(
      (imageryLayer && imageryLayer.show) || (offlineImageryLayer && offlineImageryLayer.show));
    scene.globe.showWaterEffect = !satelliteShown;
    state.water_effect = scene.globe.showWaterEffect;
    return scene.globe.showWaterEffect;
  }

  /** 把画面切到"离线"：晕渲可见、影像不可见。**只**改图层的 show。
   *
   * 晕渲缺失时关闭在线图层并披露 relief_unavailable，绝不以联网补偿离线缺失。
   */
  function applyOfflineView() {
    if (imageryLayer) imageryLayer.show = false;
    if (offlineImageryLayer) offlineImageryLayer.show = false;
    state.basemap_mode = 'offline_relief';
    if (!reliefLayer) return imageryLayer ? 'relief_unavailable' : 'no_basemap_layer';
    reliefLayer.show = true;
    syncWaterEffect();
    return null;
  }

  /** 把画面切到"离线卫星影像"：本地影像可见，其余两个不可见。
   *
   * 这是本轮新增的**默认**底图。它不触网、不占预算，因此与在线授权状态无关。
   * 本地影像缺失时**必须**回退晕渲并披露真实原因——绝不留蓝屏或空白。
   */
  function applyOfflineImageryView() {
    if (imageryLayer) imageryLayer.show = false;
    if (!offlineImageryLayer) {
      // 本地数据没做/不可用 ⇒ 回退晕渲，并如实给出**真实**原因码。
      // 必须是离线源自己的 ``reason_code``（例如元信息缺失、瓦片目录缺失），
      // 而不是笼统的 offline_imagery_unavailable —— 后者会把用户引向"重试"，
      // 而真实原因是"本地数据还没生成"。
      const degraded = applyOfflineView();
      const reason = (offlineSource && offlineSource.reason_code)
        || 'offline_imagery_unavailable';
      return degraded || reason;
    }
    offlineImageryLayer.show = true;
    if (reliefLayer) reliefLayer.show = false;
    state.basemap_mode = 'offline_imagery';
    syncWaterEffect();
    return null;
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

    /** 在线底图**结构上**是否可用（不可用时 ``onlineBlockReason()`` 给出真实原因码）。
     *
     * 这是开关置灰与否的**唯一**判据：``online_allowed=false`` 或凭证缺失时返回 false，
     * UI 必须据此禁用"在线演示"并如实披露原因，不得假装可切换。
     */
    onlineAvailable() { return onlineUsable(); },
    /** 在线不可用的真实原因码；可用时为 null。 */
    onlineBlockReason() { return onlineBlockReason; },
    /** 当前底图模式：``offline`` / ``online``。
     *
     * 语义**保持不变**：只回答"当前是否在触网"，因此离线卫星影像与离线晕渲
     * 都返回 ``offline``。需要区分具体底图请用 ``basemapMode()``。
     */
    mode() { return state.mode; },
    /** 三值底图模式：``offline_relief`` / ``offline_imagery`` / ``online``。 */
    basemapMode() { return state.basemap_mode; },
    /** 本地离线影像图层是否结构上可用（不依赖任何凭证或在线授权）。 */
    offlineImageryAvailable() { return offlineImageryUsable(); },
    /** 离线影像不可用的真实原因码；可用时为 null。 */
    offlineImageryBlockReason() {
      if (offlineImageryLayer) return null;
      return (offlineSource && offlineSource.reason_code)
        || (offlineSource ? 'offline_imagery_unavailable' : 'offline_imagery_not_configured');
    },

    /**
     * 切换底图模式（离线晕渲 / 离线卫星影像 / 在线演示）。
     *
     * 硬约束（与任务 B3 逐条对应）：
     *  * **不**创建/销毁 Viewer，**不**动 camera，**不**碰任何业务实体；
     *  * **不**新增 imagery layer（三个图层在 ``createBasemap`` 时一次性建好，
     *    这里只切 ``show``）⇒ 连续切换不可能叠加图层或增长内存；
     *  * 在线不可用时**立即**以真实原因拒绝，并保持离线场景可用；
     *  * 异步等待期间用户再次切换 ⇒ 旧等待用 ``mode_serial`` 作废，绝不覆盖新模式；
     *  * 在线影像真实瓦片未到达（失败/超时）⇒ 回退离线并给出原因，
     *    **绝不**留下空白且无提示的画面；
     *  * 离线影像缺失/瓦片错误 ⇒ 回退离线晕渲，**绝不**出现蓝屏、空白或持续重试。
     */
    async setMode(mode, options2 = {}) {
      const target = mode === 'online' ? 'online'
        : (mode === 'offline_imagery' ? 'offline_imagery' : 'offline');
      const serial = state.mode_serial + 1;
      state.mode_serial = serial;

      if (target === 'offline' || target === 'offline_imagery') {
        // 离线卫星影像：本地图层就绪即可切；缺失时回退晕渲并披露原因。
        const degraded = target === 'offline_imagery'
          ? applyOfflineImageryView() : applyOfflineView();
        state.mode = 'offline';
        state.mode_reason = degraded;
        // 离线兜底底图是 FABDEM 晕渲；它缺失时必须**显式**披露（degraded 非 null），
        // 否则用户会看到"切回离线后一片空"，却没有任何原因提示。
        return { ok: degraded === null, mode: 'offline',
                 basemap_mode: state.basemap_mode, reason: degraded,
                 relief_available: Boolean(reliefLayer),
                 offline_imagery_available: offlineImageryUsable() };
      }

      // 在线不可用：拒绝并如实披露原因，场景保持离线可用。
      if (!onlineUsable()) {
        const degraded = applyOfflineView();
        state.mode = 'offline';
        state.mode_reason = onlineBlockReason || degraded;
        return { ok: false, mode: 'offline', reason: onlineBlockReason || degraded };
      }

      const baselineResolved = (() => {
        const stats = imageryStats(imageryLayer);
        return stats && stats.resolved ? stats.resolved : 0;
      })();
      if (imageryLayer) imageryLayer.show = true;
      if (offlineImageryLayer) offlineImageryLayer.show = false;
      if (reliefLayer) reliefLayer.show = false;
      // 在线真彩色影像同样必须关闭海面效果（同一色彩致因）。
      syncWaterEffect();

      const wait = typeof options2.waitForImageryTiles === 'function'
        ? options2.waitForImageryTiles : defaultWaitForImageryTiles;
      const baselineRejected = (imageryStats(imageryLayer) || {}).rejected || 0;
      let outcome;
      try {
        outcome = await wait({
          layer: imageryLayer,
          baselineResolved,
          baselineRejected,
          timeoutMs: options2.timeoutMs,
          intervalMs: options2.intervalMs,
          serial,
        });
      } catch {
        outcome = { ok: false, reason: 'imagery_tile_request_failed' };
      }

      // 竞态裁决：等待期间用户已切到别的模式 ⇒ 本次结果一律作废。
      if (state.mode_serial !== serial) {
        return { ok: false, mode: state.mode, reason: 'superseded_by_newer_switch' };
      }

      if (outcome && outcome.ok) {
        state.mode = 'online';
        state.basemap_mode = 'online';
        state.mode_reason = null;
        return { ok: true, mode: 'online', basemap_mode: 'online', reason: null,
                 resolved: outcome.resolved };
      }

      // 真实失败 / 超时：回退离线（绝不留下空白无提示的画面）。
      // 回退**优先回到本地离线影像**（它仍然可用且不触网），只有它也不可用时才
      // 退到灰阶晕渲；两种回退都给出真实原因，绝不出现蓝屏或空白。
      const degraded = offlineImageryUsable()
        ? applyOfflineImageryView() : applyOfflineView();
      state.mode = 'offline';
      state.mode_reason = (outcome && outcome.reason) || degraded
        || 'imagery_tile_unavailable';
      return { ok: false, mode: 'offline', basemap_mode: state.basemap_mode,
               reason: state.mode_reason };
    },
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
    /** 只读图层证据：影像/离线影像/晕渲各自的真实瓦片计数、方案与版权（供验收采集）。 */
    layerStats() {
      return {
        imagery: imageryStats(imageryLayer),
        offline_imagery: offlineImageryLayer ? {
          ...imageryStats(offlineImageryLayer),
          is_satellite_imagery: true,
          source_id: 'offline_zhoushan_imagery',
          attribution: (offlineSource && offlineSource.attribution) || null,
          consumes_online_budget: false,
        } : null,
        relief: reliefLayer ? {
          ...imageryStats(reliefLayer),
          is_satellite_imagery: false,
        } : null,
      };
    },
    /**
     * 地理注记图层开关。
     *
     * A7-OBS-02 结论（人工验收核查）：本构建**没有**创建注记 provider ——
     * `labelsLayer` 全程为 null（vendored Cesium 包不含在线注记图源，离线开发模式也
     * 不会新建在线链路）。因此勾选后没有任何可见反馈，随后开关被如实复位并提示
     * "本轮未启用"：这不是加载失败，而是本图层在本构建中不存在。
     *
     * 这里保持 fail-closed 的返回值语义（没有图层就返回 false，调用方必须复位勾选框），
     * 但**不**为了"看起来有反应"新建底图链路 —— 那属于底图改造，不是本轮缺陷修复。
     */
    setLabelsVisible(value) {
      if (!labelsLayer) return false;
      labelsLayer.show = Boolean(value);
      return true;
    },
    /** 注记图层的真实能力（不存在时前端必须把勾选框显示为不可用，而不是假装已开启）。 */
    labelsAvailable() { return Boolean(labelsLayer); },
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
  // 默认底图（Round38-OFFLINE-IMAGERY 起）：
  //
  //  * 本地离线卫星影像可用 ⇒ 默认显示它（**零**上游、**零**预算、无凭证依赖）；
  //  * 本地影像不可用 ⇒ 退回 FABDEM 晕渲，并如实披露原因；
  //  * 在线 ArcGIS **一律默认关闭**，只在用户主动选择「在线演示」后才显示。
  //
  // 页面重新打开一律回到默认，不做跨会话持久化（避免新增不必要的状态管理）。
  // 注意：这只切图层的 ``show``，不新增/销毁图层，也不动 camera 与业务实体。
  //
  // 统一走 ``applyOfflineImageryView()``：它在本地影像可用时显示影像，不可用时
  // **自动回退晕渲并返回真实原因码**。若这里按"可用/不可用"分成两支，不可用那一支
  // 会返回 null，于是页面停在晕渲却对"为什么不是影像"只字不提——用户只能猜。
  state.mode = 'offline';
  state.mode_reason = applyOfflineImageryView();
  return controller;
}
