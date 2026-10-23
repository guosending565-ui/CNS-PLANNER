/**
 * 起降点三维 billboard（R7-D16）。
 *
 * 边界：
 *  * 只读消费 ``GET /api/cns-shared-site-candidates`` 的候选站址快照
 *    （``project_node`` / ``reference_landing_site`` 两类），不读 ProjectState、
 *    不发任何写请求、不参与任何规划判定；
 *  * 二维 marker 与三维 billboard **共用同一张** PNG
 *    ``/assets/symbols/landing_pad.png``（原始文件 SHA256
 *    ``37f83a99…``），透明边缘由二维绘制期裁剪，三维由 Cesium 原生 alpha 处理；
 *  * 起点 / 终点只通过锚点环颜色与标签前缀区分；经纬度、``node_id`` /
 *    ``reference_site_id`` 与业务身份一律不改；
 *  * 高度用既有地形窗口 + geoid 逐点正高转椭球高；缺地形时贴地高度 0，
 *    并在弹窗显式标注"高度未解析"，绝不把未知当已知。
 */

//: 与二维 ``display_layers.js`` 的 ``LANDING_PAD_ICON_URL`` 同源。
export const LANDING_PAD_ICON_URL = '/assets/symbols/landing_pad.png?v=r7d16';

export const LANDING_PAD_STYLE = Object.freeze({
  normal: { ring: '#1a8677', label: '起降点' },
  start: { ring: '#1f7a4d', label: '起点' },
  end: { ring: '#c2410c', label: '终点' },
  reference: { ring: '#1a8677', label: '参考起降点' },
  referenceDuplicate: { ring: '#8b6b2f', label: '参考起降点（疑似重复）' },
});

const LANDING_KINDS = new Set(['project_node', 'reference_landing_site']);

function finiteCoordinate(value) {
  if (!Array.isArray(value) || value.length < 2) return null;
  const lon = Number(value[0]);
  const lat = Number(value[1]);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
  if (Math.abs(lon) > 180 || Math.abs(lat) > 90) return null;
  return [lon, lat];
}

function coordinateKey(coordinate) {
  return `${coordinate[0].toFixed(9)},${coordinate[1].toFixed(9)}`;
}

/** 从 ``routes`` 的权威航路取首末点；缺失/多义时返回 null，绝不猜。 */
export function landingPadRouteEndpoints(routeRows) {
  const rows = Array.isArray(routeRows) ? routeRows : [];
  const canonical = rows.filter((row) => row
    && row.authority === 'canonical_operational_route'
    && Array.isArray(row.path) && row.path.length >= 2);
  if (canonical.length !== 1) return null;
  const path = canonical[0].path;
  return {
    routeId: canonical[0].route_id || null,
    start: finiteCoordinate(path[0]),
    end: finiteCoordinate(path[path.length - 1]),
  };
}

/** 候选站点 → 起降点显示模型（纯函数，便于 Node 断言）。 */
export function landingPadSiteModel(payload, endpoints) {
  const candidates = Array.isArray(payload) ? payload
    : ((payload && payload.candidates) || []);
  const startKey = endpoints && endpoints.start ? coordinateKey(endpoints.start) : null;
  const endKey = endpoints && endpoints.end ? coordinateKey(endpoints.end) : null;
  const model = [];
  for (const candidate of candidates) {
    if (!candidate || !LANDING_KINDS.has(String(candidate.candidate_kind))) continue;
    const coordinate = finiteCoordinate(candidate.coordinate);
    if (!coordinate) continue;
    const key = coordinateKey(coordinate);
    const isStart = startKey !== null && key === startKey;
    const isEnd = endKey !== null && key === endKey;
    const reference = String(candidate.candidate_kind) === 'reference_landing_site';
    const role = isStart ? 'start' : (isEnd ? 'end' : (reference ? 'reference' : 'normal'));
    // 空间可用性（例如 CRS 未确认导致的 blocked）只进弹窗与状态，不改变身份配色：
    // 参考起降点始终用青绿色，项目节点用常规色，起点/终点才用绿/橙锚点环。
    const style = isStart ? LANDING_PAD_STYLE.start
      : (isEnd ? LANDING_PAD_STYLE.end
        : (reference ? LANDING_PAD_STYLE.reference : LANDING_PAD_STYLE.normal));
    model.push({
      id: String(candidate.candidate_id || candidate.source_id || ''),
      siteId: String(candidate.source_id || ''),
      name: String(candidate.name || candidate.source_id || '起降点'),
      kind: String(candidate.candidate_kind),
      coordinate,
      role,
      roleLabel: style.label,
      ringColor: style.ring,
      spatialUseStatus: String(candidate.spatial_use_status || 'unknown'),
      coordinateQuality: String(candidate.coordinate_quality || 'unknown'),
    });
  }
  model.sort((left, right) => left.id.localeCompare(right.id));
  return model;
}

function groundHeight(terrainHeightAt, coordinate) {
  if (typeof terrainHeightAt !== 'function') return null;
  try {
    const height = terrainHeightAt(coordinate[0], coordinate[1]);
    return Number.isFinite(height) ? height : null;
  } catch (_error) {
    return null;
  }
}

/**
 * 构造三维起降点实体（billboard + 锚点环 + 起点/终点标签）。
 *
 * @returns {{entities:Array, records:Array, diagnostics:Object}}
 */
export function addLandingPadEntities(viewer, payload, options = {}) {
  const { Cesium, terrainHeightAt, endpoints } = options;
  if (!Cesium || !viewer || !viewer.entities) {
    return {
      entities: [], records: [],
      diagnostics: { skipped: [{ part: 'all', reason: 'cesium_or_viewer_unavailable' }] },
    };
  }
  const records = landingPadSiteModel(payload, endpoints);
  const entities = [];
  const skipped = [];
  for (const record of records) {
    const [lon, lat] = record.coordinate;
    const height = groundHeight(terrainHeightAt, record.coordinate);
    const labelText = `${record.roleLabel} · ${record.name}`;
    try {
      const entity = viewer.entities.add({
        id: `landing-pad:${record.id}`,
        name: labelText,
        position: Cesium.Cartesian3.fromDegrees(lon, lat, height === null ? 0 : height),
        billboard: {
          image: LANDING_PAD_ICON_URL,
          // 底部锚定：图标只向坐标上方生长，不改变起降点位置语义。
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
          horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
          width: 64,
          height: 64,
          sizeInMeters: false,
          scaleByDistance: new Cesium.NearFarScalar(600.0, 1.15, 24000.0, 0.34),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0.0, 40000.0),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        ellipse: {
          semiMajorAxis: 5.5,
          semiMinorAxis: 5.5,
          material: Cesium.Color.fromCssColorString(record.ringColor).withAlpha(0.9),
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 2,
          height: height === null ? 0.3 : height + 0.3,
        },
        label: {
          text: labelText,
          font: '12px "Microsoft YaHei", "Segoe UI", sans-serif',
          fillColor: Cesium.Color.fromCssColorString(record.ringColor),
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
          pixelOffset: new Cesium.Cartesian2(0, 62),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0.0, 12000.0),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      entity.__cns = {
        kind: 'landing-pad',
        title: labelText,
        rows: [
          ['业务身份', record.kind === 'project_node' ? '项目起降点' : '参考起降点'],
          ['角色', record.roleLabel],
          ['经度 / 纬度', `${lon.toFixed(6)} / ${lat.toFixed(6)}`],
          ['标识', record.siteId || record.id],
          ['空间可用性', record.spatialUseStatus],
          ['坐标质量', record.coordinateQuality],
          ['高度', height === null
            ? '未解析（地形窗口/geoid N 不可用，未当 0 冒充）'
            : `${height.toFixed(3)} m（WGS84 椭球高）`],
          ['说明', '只读展示：不写 ProjectState、不改变起降点几何与业务身份。'],
        ],
      };
      entities.push(entity);
    } catch (error) {
      skipped.push({
        part: record.id || 'unknown', reason: String(error && error.message || error),
      });
    }
  }
  return {
    entities,
    records,
    diagnostics: {
      skipped,
      total: records.length,
      drawn: entities.length,
      icon: LANDING_PAD_ICON_URL,
      endpoints: endpoints || null,
    },
  };
}
