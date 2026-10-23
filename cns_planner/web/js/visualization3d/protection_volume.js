/** P17 历史保护走廊与用户工程示意的独立 Cesium 实体构造器。 */

export const PROTECTION_PREVIEW_DISCLAIMER = '自定义保护区示意，非已确认 P17 结果';

const STYLES = Object.freeze({
  historical: { fill: '#ffb454', outline: '#ffd59a', alpha: 0.20 },
  preview: { fill: '#b26cff', outline: '#e4bdff', alpha: 0.18 },
  blocked: { fill: '#ff6b6b', outline: '#ff9a9a', alpha: 0.8 },
});

function polygonsOf(geojson) {
  if (!geojson || !Array.isArray(geojson.coordinates)) return [];
  if (geojson.type === 'Polygon') return [geojson.coordinates];
  if (geojson.type === 'MultiPolygon') return geojson.coordinates;
  return [];
}

export function protectionBoundaryPoints(volumes) {
  const points = [];
  for (const volume of volumes || []) {
    const polygons = polygonsOf(((volume.geometry || {}).geometry));
    for (const polygon of polygons) {
      for (const ring of polygon) {
        for (const point of ring) points.push([Number(point[0]), Number(point[1])]);
      }
    }
  }
  return points.filter((point) => Number.isFinite(point[0]) && Number.isFinite(point[1]));
}

function finiteHeight(value) {
  if (value === null || value === undefined || value === '') return null;
  const height = Number(value);
  return Number.isFinite(height) ? height : null;
}

function heightLabel(volume, preview) {
  if (preview) {
    const lower = finiteHeight(volume.lower_altitude_m);
    const upper = finiteHeight(volume.upper_altitude_m);
    if (lower === null || upper === null) return '缺失（垂直范围未确认）';
    return `${lower}–${upper} m EGM2008`;
  }
  const slice = finiteHeight(volume.nominal_altitude_slice_egm2008_m);
  return slice === null ? '缺失' : `${slice} m EGM2008 名义切片`;
}

function atHeight(ring, orthometricHeight, deps) {
  const { Cesium, undulationAt } = deps;
  if (!Number.isFinite(orthometricHeight)) return null;
  const rows = [];
  for (const point of ring || []) {
    const lon = Number(point[0]); const lat = Number(point[1]);
    const n = undulationAt(lon, lat);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || !Number.isFinite(n)) return null;
    rows.push(Cesium.Cartesian3.fromDegrees(lon, lat, orthometricHeight + n));
  }
  return rows.length >= 4 ? rows : null;
}

function popup(volume, preview = false) {
  const halfWidth = preview ? volume.horizontal_half_width_m : volume.outer_half_width_m;
  const height = heightLabel(volume, preview);
  const rows = [
    ['route_id', volume.route_id || '—'],
    ['对象 ID', volume.object_id || '—'],
    ['渲染实例键', volume.render_instance_id || '—'],
    ['模式', preview ? '用户工程示意' : 'P17 持久化结果'],
    ['半宽', halfWidth === undefined || halfWidth === null ? '缺失' : `${halfWidth} m`],
    ['路线里程', volume.route_length_m === undefined ? '—' : `${volume.route_length_m} m`],
    ['几何状态', (volume.geometry || {}).status || 'unknown'],
    ['几何维度', volume.geometry_dimension || 'none'],
    ['垂直范围', volume.vertical_extent_status || 'unknown'],
    ['高度', height],
    ['来源', volume.route_authority || '—'],
    ['当前性', volume.current === true ? 'current'
      : (preview ? 'preview/isolated' : 'historical/stale')],
    ['P17 状态', volume.p17_status || '—'],
    ['版本', `持久化 ${volume.p17_algorithm_version || '—'}；当前代码 ${volume.current_code_algorithm_version || '—'}`],
    ['input fingerprint', volume.protection_input_fingerprint || '—'],
    ['保护几何身份', volume.protection_geometry_identity || '—'],
    ['所需证据', (volume.required_evidence || []).join('；') || '—'],
  ];
  if (preview) {
    rows.push(['免责声明', volume.disclaimer || PROTECTION_PREVIEW_DISCLAIMER]);
    rows.push(['隔离', '不写 ProjectState；不进入正式 P17/CNS/Radar LOS/运行许可']);
  }
  return {
    kind: preview ? 'protection-preview' : 'p17-protection',
    title: preview ? `工程示意保护区 ${volume.route_id || ''}`
      : `历史 P17 保护走廊 ${volume.route_id || ''}`,
    rows,
    preview: preview || undefined,
  };
}

function addBlockedMarker(viewer, volume, deps, style, entities, diagnostics, preview,
  reason = null) {
  const blockedReason = reason
    || (volume.geometry || {}).reason || volume.status || 'geometry_blocked';
  const anchor = volume.route_anchor;
  if (!Array.isArray(anchor) || anchor.length < 2) {
    diagnostics.push({ object_id: volume.object_id, part: 'corridor',
      reason: blockedReason });
    return;
  }
  const marker = viewer.entities.add({
    position: deps.Cesium.Cartesian3.fromDegrees(Number(anchor[0]), Number(anchor[1]), 0),
    point: {
      pixelSize: 10,
      color: deps.Cesium.Color.fromCssColorString(style.fill),
      outlineColor: deps.Cesium.Color.BLACK,
      outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    name: `保护走廊缺证 ${volume.route_id || ''}`,
  });
  marker.__cns = popup(volume, preview);
  entities.push(marker);
  diagnostics.push({ object_id: volume.object_id, part: 'corridor',
    reason: blockedReason });
}

function addSlice(viewer, volume, polygons, height, deps, style, entities, preview) {
  let count = 0;
  for (const polygon of polygons) {
    const rings = polygon.map((ring) => atHeight(ring, height, deps));
    if (!rings.length || rings.some((ring) => ring === null)) return null;
    const hierarchy = new deps.Cesium.PolygonHierarchy(
      rings[0], rings.slice(1).map((ring) => new deps.Cesium.PolygonHierarchy(ring)));
    const area = viewer.entities.add({
      polygon: {
        hierarchy,
        material: deps.Cesium.Color.fromCssColorString(style.fill).withAlpha(style.alpha),
        outline: false,
        perPositionHeight: true,
        arcType: deps.Cesium.ArcType.NONE,
      },
      name: `${preview ? '工程示意' : '历史 P17'}保护区 ${volume.route_id || ''}`,
    });
    area.__cns = popup(volume, preview);
    entities.push(area);
    for (const ring of rings) {
      const boundary = viewer.entities.add({
        polyline: {
          positions: ring,
          width: preview ? 2.0 : 2.4,
          material: deps.Cesium.Color.fromCssColorString(style.outline).withAlpha(0.95),
          clampToGround: false,
        },
      });
      boundary.__cns = popup(volume, preview);
      entities.push(boundary);
    }
    count += 1;
  }
  return count;
}

function addPreviewSides(viewer, volume, polygons, lowerHeight, upperHeight, deps, style,
  entities) {
  let sides = 0;
  for (const polygon of polygons) {
    for (const rawRing of polygon) {
      const lower = atHeight(rawRing, lowerHeight, deps);
      const upper = atHeight(rawRing, upperHeight, deps);
      if (!lower || !upper) return null;
      for (let index = 0; index < lower.length - 1; index += 1) {
        const side = viewer.entities.add({
          polygon: {
            hierarchy: new deps.Cesium.PolygonHierarchy([
              lower[index], lower[index + 1], upper[index + 1], upper[index],
            ]),
            material: deps.Cesium.Color.fromCssColorString(style.fill).withAlpha(style.alpha),
            outline: false,
            perPositionHeight: true,
            arcType: deps.Cesium.ArcType.NONE,
          },
        });
        side.__cns = popup(volume, true);
        entities.push(side);
        sides += 1;
      }
    }
  }
  return sides;
}

function addVolumes(viewer, volumes, deps, preview) {
  const entities = [];
  const groups = [];
  const skipped = [];
  for (const volume of volumes || []) {
    const groupEntities = [];
    const style = preview ? STYLES.preview
      : ((volume.geometry || {}).status === 'available' ? STYLES.historical : STYLES.blocked);
    const geometry = volume.geometry || {};
    const polygons = polygonsOf(geometry.geometry);
    if (geometry.status !== 'available' || !polygons.length) {
      addBlockedMarker(viewer, volume, deps, style, groupEntities, skipped, preview);
    } else if (preview) {
      const lowerHeight = finiteHeight(volume.lower_altitude_m);
      const upperHeight = finiteHeight(volume.upper_altitude_m);
      if (lowerHeight === null || upperHeight === null) {
        addBlockedMarker(viewer, volume, deps, STYLES.blocked, groupEntities, skipped,
          preview, 'vertical_extent_unconfirmed');
      } else {
        const bottom = addSlice(viewer, volume, polygons, lowerHeight,
          deps, style, groupEntities, true);
        const top = addSlice(viewer, volume, polygons, upperHeight,
          deps, style, groupEntities, true);
        const sides = addPreviewSides(viewer, volume, polygons, lowerHeight, upperHeight,
          deps, style, groupEntities);
        if (bottom === null || top === null || sides === null) {
          removeProtectionEntities(viewer, groupEntities);
          groupEntities.length = 0;
          skipped.push({ object_id: volume.object_id, part: 'volume',
            reason: 'egm2008_undulation_unavailable' });
        }
      }
    } else {
      const sliceHeight = finiteHeight(volume.nominal_altitude_slice_egm2008_m);
      if (sliceHeight === null) {
        addBlockedMarker(viewer, volume, deps, STYLES.blocked, groupEntities, skipped,
          preview, 'nominal_altitude_missing');
      } else {
        const slices = addSlice(
          viewer, volume, polygons, sliceHeight, deps, style, groupEntities, false);
        if (slices === null) {
          removeProtectionEntities(viewer, groupEntities);
          groupEntities.length = 0;
          skipped.push({ object_id: volume.object_id, part: 'slice',
            reason: 'egm2008_undulation_unavailable' });
        }
      }
    }
    entities.push(...groupEntities);
    groups.push({
      // 实例键是选择 / 动画 / 事件 / 单独开关 / 清除动作的唯一凭据；
      // object_id 可能被同一航路的多份几何共用，因此不能单独用来分组。
      render_instance_id: volume.render_instance_id || null,
      object_id: volume.object_id,
      route_id: volume.route_id,
      route_authority: volume.route_authority,
      protection_geometry_identity: volume.protection_geometry_identity || null,
      status: volume.status,
      historical: Boolean(volume.historical),
      preview,
      visible: true,
      entities: groupEntities,
    });
  }
  const setVisible = (layerVisible) => {
    for (const group of groups) {
      for (const entity of group.entities) entity.show = Boolean(layerVisible && group.visible);
    }
  };
  return {
    entities, groups, setVisible,
    diagnostics: { skipped, entity_count: entities.length },
  };
}

export function addProtectionVolumeEntities(viewer, volumes, deps) {
  return addVolumes(viewer, volumes, deps, false);
}

export function addProtectionPreviewEntities(viewer, volumes, deps) {
  return addVolumes(viewer, volumes, deps, true);
}

export function removeProtectionEntities(viewer, entities) {
  for (const entity of entities || []) viewer.entities.remove(entity);
}
