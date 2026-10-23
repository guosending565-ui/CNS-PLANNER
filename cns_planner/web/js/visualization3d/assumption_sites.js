/**
 * P16-V2 工程假设站址的三维叠加（R7-B）。
 *
 * 与正式 CNS 设备图层的区别是**硬边界**，不是配色差异：
 *
 *  * 数据只来自已完成 V2 任务的 staged result（`/api/visualization-3d/assumption-sites`），
 *    不读 ProjectState、不读 V1 正式方案；
 *  * 站点一律洋红色菱形 + 虚线标签 + “[假设]”前缀；正式设备使用既有四类身份配色；
 *  * V2 结果没有逐站原点高程，因此实体**贴地**并明确显示"原点高程未绑定/未猜测"，
 *    绝不把 15 m 统一挂高当成真实安装高程；
 *  * 图层默认关闭，由用户显式勾选；关闭后不删除正式图层，也不改任何状态。
 *
 * 纯构造：Cesium 由调用方注入，便于在 Node 中单测。
 */

export const ASSUMPTION_SITE_STYLE = Object.freeze({
  color: '#ff4fd8',
  outline: '#5a003f',
  label_prefix: '[假设]',
  pixel_size: 11,
});

export const ASSUMPTION_SITES_DISCLAIMER = (
  'P16-V2 选址是工程假设预算，不是正式设备、不是 P16-V1 结果，也不写入 ProjectState；'
  + '站点贴地显示，原点高程未在任务结果中绑定，本图层不猜测挂高。'
);

/**
 * @param {object} viewer Cesium Viewer
 * @param {object} payload `/api/visualization-3d/assumption-sites` 响应
 * @param {{Cesium: object}} deps
 * @returns {Array<object>} 创建的实体（调用方负责可见性）
 */
export function addAssumptionSiteEntities(viewer, payload, { Cesium }) {
  const sites = Array.isArray(payload?.sites) ? payload.sites : [];
  const entities = [];
  const color = Cesium.Color.fromCssColorString(ASSUMPTION_SITE_STYLE.color);
  const outline = Cesium.Color.fromCssColorString(ASSUMPTION_SITE_STYLE.outline);
  for (const site of sites) {
    const coordinate = Array.isArray(site.coordinate) ? site.coordinate : [];
    if (coordinate.length < 2 || !Number.isFinite(Number(coordinate[0]))
        || !Number.isFinite(Number(coordinate[1]))) continue;
    const longitude = Number(coordinate[0]);
    const latitude = Number(coordinate[1]);
    const subsystems = (site.subsystems || []).join('+') || '?';
    const title = `${site.name || site.site_id || site.distinct_site_id}`;
    const entity = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(longitude, latitude),
      point: {
        pixelSize: ASSUMPTION_SITE_STYLE.pixel_size,
        color,
        outlineColor: outline,
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: `${ASSUMPTION_SITE_STYLE.label_prefix}V2假设 · ${subsystems}`,
        font: '12px "Microsoft YaHei", sans-serif',
        fillColor: color,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(0, -18),
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: `${ASSUMPTION_SITE_STYLE.label_prefix}${title}`,
    });
    entity.__cns = {
      kind: 'assumption_site',
      assumptionOnly: true,
      formal: false,
      title: `P16-V2 工程假设站址 ${title}`,
      rows: [
        ['语义', '工程假设（只读）；非正式设备，不写入 ProjectState'],
        ['distinct_site_id', site.distinct_site_id || '—'],
        ['子系统', subsystems],
        ['设备', (site.devices || []).join(', ') || '—'],
        ['复用类别', (site.reuse_classes || []).join(', ') || '—'],
        ['动作', (site.action_ids || []).join(', ') || '—'],
        ['场景统一挂高', payload?.mount_height_agl_m == null
          ? '未知（不猜测）' : `${payload.mount_height_agl_m} m（工程假设）`],
        ['三维位置', '贴地显示；逐站原点正高未在任务结果中绑定'],
        ['来源任务', payload?.source_task_id || '—'],
      ],
    };
    entities.push(entity);
  }
  return entities;
}

export default { ASSUMPTION_SITE_STYLE, ASSUMPTION_SITES_DISCLAIMER, addAssumptionSiteEntities };
