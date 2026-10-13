/**
 * Radar 方向性三维覆盖楔体的 Cesium 实体构造（Round37-T3D-2B）。
 *
 * 为什么单独成模块：Radar 的几何语义与通信（全向半球包络）、RID（陆海非正圆 footprint）
 * **完全不同**，而且必须可被 Node 直接单测（不依赖 DOM/真 Viewer）。因此这里只做
 * 「顶点 → 实体」的纯构造，依赖由调用方**注入**：
 *
 *   addRadarVolumeEntities(viewer, volumes, { Cesium, undulationAt })
 *
 * 硬约定：
 *  * **绝不**把单面阵渲染成全向圆或半球；几何完全来自服务端顶点（方位窗口、
 *    0–45° 仰角、斜距面）；
 *  * 正高 → 椭球高逐点 `h = H + N`；`N` 未解析时**不**把高度当 0 渲染，而是
 *    记录 skipped 并保持该几何不完整；
 *  * 每个真实面阵**单独成组**（可单独关闭），被阻塞的面阵只留"缺证锚点"，
 *    绝不画任何楔体；
 *  * ``device_spec_preview`` 使用**独立**函数与独立配色，绝不与真实面阵混在同一组。
 */

//: 预览模式的常驻免责声明（与服务端 RADAR_PREVIEW_DISCLAIMER 同一句）。
export const RADAR_PREVIEW_DISCLAIMER = '设备参数示意，非已采纳布设，非已确认探测覆盖';

//: Radar 显示透明度（真实规划面阵 vs 设备参数示意预览必须一眼可分）。
export const RADAR_STYLE = {
  project: {
    alpha: 0.26, outlineAlpha: 0.92,
    surface: '#ff9f43', outline: '#ffd9a0', blocked: '#ff6b6b', accent: '#ff9f43',
  },
  preview: {
    alpha: 0.22, outlineAlpha: 0.9,
    surface: '#c792ea', outline: '#e8d3ff', blocked: '#ff6b6b', accent: '#c792ea',
  },
};

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function ringToCartesians(ring, { Cesium, undulationAt }) {
  const rows = [];
  for (const row of ring || []) {
    const lon = Number(row[0]);
    const lat = Number(row[1]);
    const ortho = Number(row[2]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const n = undulationAt(lon, lat);
    const height = Number.isFinite(ortho) ? (n === null ? null : ortho + n) : null;
    rows.push({ lon, lat, ortho, n, height });
  }
  return rows;
}

function bandPositions(lower, upper, Cesium) {
  if (!lower.length || !upper.length) return null;
  if (lower.some((row) => row.height === null) || upper.some((row) => row.height === null)) {
    return null;
  }
  const loop = lower.map((row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height))
    .concat(upper.slice().reverse().map(
      (row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height)));
  return loop;
}

function popupRows(volume, geometry, blocked) {
  const window = volume.azimuth_window || {};
  const rows = [
    ['panel_id', volume.panel_id || '—'],
    ['型号', `${volume.radar_type_label || volume.radar_type || '—'}`],
    ['塔位', `${volume.tower_id || '—'}${volume.tower_name ? `（${volume.tower_name}）` : ''}`],
    ['几何状态', blocked ? `blocked（${geometry.reason || '—'}）` : 'available'],
    ['中心方位角', volume.azimuth_deg === null || volume.azimuth_deg === undefined
      ? '—' : `${Number(volume.azimuth_deg).toFixed(4)}°`],
    ['水平波束', window.span_deg === undefined ? '—'
      : `${window.span_deg}°（${Number(window.start_deg).toFixed(2)}° → ${Number(window.end_deg).toFixed(2)}°${window.wrapped_across_north ? '，跨正北' : ''}）`],
    ['仰角范围', geometry.elevation_min_deg === undefined ? '—'
      : `${geometry.elevation_min_deg}°–${geometry.elevation_max_deg}°（中心 ${geometry.elevation_center_deg}°）`],
    ['近端斜距', geometry.min_slant_range_m === undefined
      ? '—' : `${geometry.min_slant_range_m} m`],
    ['远端斜距', geometry.max_slant_range_m === undefined
      ? '—' : `${geometry.max_slant_range_m} m`],
    ['斜距语义', geometry.slant_range_semantics === 'slant'
      ? '斜距（**不是**所有高度截面的水平半径）' : '—'],
    ['仰角 0° 水平半径', geometry.horizontal_radius_at_elevation_zero_m === undefined
      ? '—' : `${geometry.horizontal_radius_at_elevation_zero_m} m（唯一与二维圆重合的截面）`],
    ['雷达原点正高', volume.service_origin_egm2008_m === null
      || volume.service_origin_egm2008_m === undefined
      ? '未确认/未记录' : `${volume.service_origin_egm2008_m} m（EGM2008）`],
    ['原点确认状态', volume.origin_status === 'confirmed' ? 'confirmed'
      : `${volume.origin_status || 'missing'}（未确认，不得当作已确认安装高度）`],
    ['来源容器', volume.parameter_source || '—'],
    ['布局条目状态', `${volume.layout_item_status || '—'}${volume.layout_stale_reason ? `（${volume.layout_stale_reason}）` : ''}`],
    ['LOS 状态', `${(volume.los || {}).status || 'unknown'}（${(volume.los || {}).reason || 'terrain_los_not_persisted'}）`],
    ['语义', volume.coverage_semantics || 'engineering_geometric_directional_coverage_not_measured'],
    ['限制', '地形遮挡 / 绕射 / 杂波 / 干扰 / 雷达方程一律未评估；几何 ≠ 实测探测能力'],
    ['禁止几何', (volume.forbidden_geometry || []).join('、') || '—'],
  ];
  if (volume.preview) {
    rows.push(['模式', `设备参数示意预览：${RADAR_PREVIEW_DISCLAIMER}`]);
  }
  if (blocked && (geometry.required_evidence || []).length) {
    rows.push(['所需证据', geometry.required_evidence.join('；')]);
  }
  return rows;
}

/** 服务端 ``radar-volumes`` 响应 → 实体 + 分组 + 单独的显示控制。 */
export function addRadarVolumeEntities(viewer, volumes, deps) {
  const { Cesium, undulationAt } = deps;
  const style = RADAR_STYLE.project;
  const entities = [];
  const groups = [];
  const skipped = [];

  for (const volume of volumes || []) {
    const geometry = volume.geometry || {};
    const blocked = geometry.status !== 'available';
    const origin = finite(volume.service_origin_egm2008_m);
    const n = undulationAt(volume.longitude, volume.latitude);
    const originHeight = (origin === null || n === null) ? null : origin + n;
    const group = {
      panel_id: volume.panel_id,
      tower_id: volume.tower_id,
      radar_type: volume.radar_type,
      azimuth_deg: volume.azimuth_deg,
      status: geometry.status || 'blocked',
      reason: geometry.reason || null,
      origin_confirmed: volume.origin_confirmed === true,
      preview: false,
      entities: [],
      visible: true,
    };

    // 1) 站心锚点：即使几何被阻塞也显示，作为"有真实面阵但缺证"的证据。
    const anchor = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(
        volume.longitude, volume.latitude, originHeight === null ? 0 : originHeight),
      point: {
        pixelSize: 9,
        color: Cesium.Color.fromCssColorString(blocked ? style.blocked : style.accent),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: `Radar 面阵 ${volume.panel_id}`,
    });
    anchor.__cns = radarPopup(volume, geometry, blocked);
    entities.push(anchor);
    group.entities.push(anchor);

    if (blocked) {
      skipped.push({ panel_id: volume.panel_id, part: 'wedge',
                     reason: geometry.reason || 'geometry_blocked' });
      groups.push(group);
      continue;
    }
    if (originHeight === null) {
      // geoid 未解析 ⇒ 顶点高度不可信；**不**用 0 冒充。
      skipped.push({ panel_id: volume.panel_id, part: 'wedge',
                     reason: 'egm2008_undulation_unavailable' });
      groups.push(group);
      continue;
    }

    const fill = Cesium.Color.fromCssColorString(style.surface).withAlpha(style.alpha);
    const outlineColor = Cesium.Color.fromCssColorString(style.outline)
      .withAlpha(style.outlineAlpha);

    // 2) 外侧曲面（斜距 = max）与内侧曲面（斜距 = min）：逐相邻仰角层成带。
    let bands = 0;
    for (const [key, rings] of [['outer', geometry.outer_surface_rings || []],
                                ['inner', geometry.inner_surface_rings || []]]) {
      for (let index = 0; index < rings.length - 1; index += 1) {
        const lower = ringToCartesians(rings[index], deps);
        const upper = ringToCartesians(rings[index + 1], deps);
        const loop = bandPositions(lower, upper, Cesium);
        if (!loop) continue;
        const band = viewer.entities.add({
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(loop),
            material: fill,
            outline: false,
            perPositionHeight: true,
            arcType: Cesium.ArcType.NONE,
          },
        });
        band.__cns = radarPopup(volume, geometry, false);
        entities.push(band);
        group.entities.push(band);
        bands += 1;
      }
      if (!rings.length) {
        skipped.push({ panel_id: volume.panel_id, part: `${key}_surface`,
                       reason: 'surface_rings_empty' });
      }
    }
    if (!bands) {
      skipped.push({ panel_id: volume.panel_id, part: 'wedge',
                     reason: 'no_renderable_wedge_band' });
    }

    // 3) 两个端面（方位 ±45°）：这是"方向性"最直观的证据，必须显式画出。
    for (const fan of geometry.side_fans || []) {
      const rows = ringToCartesians(fan.ring || [], deps);
      if (!rows.length || rows.some((row) => row.height === null)) {
        skipped.push({ panel_id: volume.panel_id, part: `side_fan_${fan.edge}`,
                       reason: 'side_fan_unresolved' });
        continue;
      }
      const fanEntity = viewer.entities.add({
        polygon: {
          hierarchy: new Cesium.PolygonHierarchy(rows.map(
            (row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height))),
          material: Cesium.Color.fromCssColorString(style.surface).withAlpha(style.alpha + 0.08),
          outline: true,
          outlineColor,
          outlineWidth: 1.2,
          perPositionHeight: true,
          arcType: Cesium.ArcType.NONE,
        },
      });
      fanEntity.__cns = radarPopup(volume, geometry, false);
      entities.push(fanEntity);
      group.entities.push(fanEntity);
    }

    // 4) 地面外弧 / 近端弧 / 中心方位射线：把"中心方位、水平 90°、近远斜距"
    //    在三维里显式画出来，说明斜距是**斜**距而不是水平半径。
    const arc = ringToCartesians(geometry.ground_arc || [], deps);
    if (arc.length && arc.every((row) => row.height !== null)) {
      const arcEntity = viewer.entities.add({
        polyline: {
          positions: arc.map((row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height)),
          width: 2.2,
          material: outlineColor,
          clampToGround: false,
        },
      });
      arcEntity.__cns = radarPopup(volume, geometry, false);
      entities.push(arcEntity);
      group.entities.push(arcEntity);
    } else {
      skipped.push({ panel_id: volume.panel_id, part: 'ground_arc',
                     reason: 'ground_arc_unresolved' });
    }

    const nearArc = ringToCartesians(geometry.near_arc || [], deps);
    if (nearArc.length && nearArc.every((row) => row.height !== null)) {
      const nearEntity = viewer.entities.add({
        polyline: {
          positions: nearArc.map(
            (row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height)),
          width: 1.4,
          material: Cesium.Color.fromCssColorString(style.outline).withAlpha(0.7),
          clampToGround: false,
        },
      });
      nearEntity.__cns = radarPopup(volume, geometry, false);
      entities.push(nearEntity);
      group.entities.push(nearEntity);
    } else {
      skipped.push({ panel_id: volume.panel_id, part: 'near_arc',
                     reason: 'near_arc_unresolved' });
    }

    const ray = ringToCartesians(geometry.azimuth_center_ray || [], deps);
    if (ray.length >= 2 && ray.every((row) => row.height !== null)) {
      const rayEntity = viewer.entities.add({
        polyline: {
          positions: ray.map(
            (row) => Cesium.Cartesian3.fromDegrees(row.lon, row.lat, row.height)),
          width: 3,
          material: Cesium.Color.fromCssColorString(style.accent),
          clampToGround: false,
        },
      });
      rayEntity.__cns = radarPopup(volume, geometry, false);
      entities.push(rayEntity);
      group.entities.push(rayEntity);
    } else {
      skipped.push({ panel_id: volume.panel_id, part: 'azimuth_center_ray',
                     reason: 'azimuth_center_ray_unresolved' });
    }

    groups.push(group);
  }

  const setVisible = (visible) => {
    for (const group of groups) {
      for (const entity of group.entities) {
        entity.show = Boolean(visible) && group.visible;
      }
    }
  };
  return {
    entities,
    groups,
    setVisible,
    diagnostics: { skipped, entity_count: entities.length, group_count: groups.length },
  };
}

/** 设备参数示意预览：**独立**入口与独立配色，绝不与真实规划面阵共用分组。 */
export function addRadarPreviewEntities(viewer, volumes, deps) {
  const { Cesium } = deps;
  const result = addRadarVolumeEntities(viewer, volumes, deps);
  const style = RADAR_STYLE.preview;
  for (const entity of result.entities) {
    if (entity.polygon) {
      entity.polygon.material = Cesium.Color
        .fromCssColorString(style.surface).withAlpha(style.alpha);
      entity.polygon.outlineColor = Cesium.Color
        .fromCssColorString(style.outline).withAlpha(style.outlineAlpha);
    }
    if (entity.point) {
      entity.point.color = Cesium.Color.fromCssColorString(style.accent);
    }
    if (entity.polyline) {
      entity.polyline.material = Cesium.Color
        .fromCssColorString(style.outline).withAlpha(style.outlineAlpha);
    }
    const info = entity.__cns;
    if (info) {
      entity.__cns = {
        ...info,
        //: ``preview = true`` 让只读验收探针能把预览实体与真实面阵实体分开统计。
        preview: true,
        title: `设备参数示意 ${info.title || ''}`.trim(),
        rows: [...(info.rows || []),
               ['免责声明', RADAR_PREVIEW_DISCLAIMER],
               ['隔离', '不写入 ProjectState、不进入规划结果/覆盖统计/LOS 验收']],
      };
    }
  }
  for (const group of result.groups) group.preview = true;
  return result;
}

export function radarPopup(volume, geometry, blocked) {
  return {
    kind: 'radar-panel',
    title: `${volume.preview ? '[示意] ' : ''}Radar 面阵 ${volume.panel_id || ''}`.trim(),
    rows: popupRows(volume, geometry, blocked),
  };
}
