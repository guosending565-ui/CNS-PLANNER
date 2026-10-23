/**
 * 固定 1000 m CNS 保障目标带的 Cesium 实体构造（Round38-T3D4）。
 *
 * 语义边界（与 W1A / N2 / FIG 完全一致，绝不改写）：
 *
 *  * 这是**中心线左右各 500 m 的 CNS 服务保障目标带**，只是"服务需要保障哪条带"；
 *  * 它**不是**任何设备已实现的覆盖区域，**不是**实测传播等值线，
 *    **不是**认证导航覆盖区，也**不构成**法规批准空间；
 *  * 它与导航中心线监测关联范围（2000 m）是两件事，互不替代；
 *  * **它绝不是禁飞区或入侵预警边界**——那一类语义只属于 P17 保护区图层。
 *
 * 几何与 currentness 全部来自服务端（`build_fixed_corridor_band` 唯一实现）：
 * 本模块**不重算 buffer、不对经纬度加减近似宽度**，只把服务端给的外环画出来。
 *
 * 垂直维度的表达规则（需求第六条第 5 款）：
 *
 *  * `vertical_extent_status === 'confirmed'`：把水平目标带抬到**已确认固定巡航层**
 *    的标称正高上绘制（一层薄带，便于判读高度层），并显著标注它只是**标称层切片**；
 *  * 否则：只画**水平目标带**（贴地投影），并常驻声明"无垂直上下界、不得标称
 *    三维保障体积"。
 */

//: 与图件同源的样式（浅色半透明填充 + 虚线边界；FIG 用 #9fd4e8 / #2b7f9c / 0.30）。
export const FIXED_CORRIDOR_STYLE = Object.freeze({
  fill: '#9fd4e8',
  fillAlpha: 0.30,
  outline: '#2b7f9c',
  outlineAlpha: 0.95,
  outlineWidth: 2,
  //: 未确认垂直范围时的贴地抬升（米）；纯显示用，不构成任何高度声明。
  groundLiftM: 6.0,
  //: 已确认巡航层切片时的带宽（米）：只是显示厚度，不是保障体积上下界。
  layerSliceThicknessM: 8.0,
});

//: 该图层的常驻语义声明（弹窗与面板都必须原样出现）。
export const FIXED_CORRIDOR_DISCLAIMER = '固定 1000 m CNS 服务保障目标带：'
  + '不是设备已实现的覆盖区域，不是认证导航覆盖区，不构成法规批准空间，'
  + '也不是禁飞区或入侵预警边界。';

//: 非 current 的历史预览常驻标注。
export const FIXED_CORRIDOR_PREVIEW_DISCLAIMER =
  '非权威历史预览：该带宽当前不是 current 结果，不得作为现行保障依据。';

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * 选择本带应绘制的环：current 用 `polygons`，非 current 只允许用 `preview_polygons`。
 *
 * 返回 `{ rings, authority }`；`authority` 为 `null` 时调用方**不得**绘制该带
 * （fail-closed：绝不画近似带、绝不画空几何冒充成功）。
 */
export function corridorRingsFor(band) {
  if (!band) return { rings: [], authority: null };
  if (band.drawable_as_current_band && Array.isArray(band.polygons)
      && band.polygons.length) {
    return { rings: band.polygons, authority: 'current' };
  }
  if (band.preview_allowed && Array.isArray(band.preview_polygons)
      && band.preview_polygons.length) {
    return { rings: band.preview_polygons, authority: 'non_authoritative_preview' };
  }
  return { rings: [], authority: null };
}

/** 该带弹窗字段（身份、宽度契约、currentness、垂直证据逐项如实展示）。 */
export function corridorPopupRows(band, authority) {
  const rows = [
    ['航路', band.route_id || '—'],
    ['band 绘制性质', authority === 'current' ? 'current 正式保障目标带'
      : '非权威历史预览（stale/未评估）'],
    ['currentness', `${band.currentness || 'unknown'}`
      + (band.currentness_entry && band.currentness_entry.reason
        ? `（${band.currentness_entry.reason}）` : '')],
    ['航路运行状态', band.route_operational_status || '—'],
    ['宽度策略', `${band.width_policy_version} · 左右各 ${band.corridor_half_width_m} m · `
      + `总宽 ${band.corridor_total_width_m} m`],
    ['几何构造器', band.geometry_algorithm_id || '—'],
    ['几何指纹', band.geometry_fingerprint || '—'],
    ['航路几何指纹', band.route_geometry_fingerprint || '—'],
    ['几何状态', `${band.geometry_status || 'unknown'}`
      + (band.geometry_reason ? `（${band.geometry_reason}）` : '')],
    ['米制构造 CRS', band.construction_crs || '—'],
    ['投影面积', band.projected_area_m2 === null || band.projected_area_m2 === undefined
      ? '—' : `${Number(band.projected_area_m2).toFixed(1)} m²`],
    ['投影/测地长度比', band.projected_to_geodesic_length_ratio === null
      || band.projected_to_geodesic_length_ratio === undefined
      ? '—' : Number(band.projected_to_geodesic_length_ratio).toFixed(6)],
    ['垂直上下界', band.vertical_extent_status === 'confirmed'
      ? `${band.altitude_layer_id} = 标称 ${band.nominal_altitude_m} m（${band.vertical_reference}）`
      : `未确认（${band.vertical_extent_reason || 'unknown'}）：只画水平目标带`],
    ['三维保障体积', band.may_claim_3d_certified_volume === true
      ? '（不得为 true）' : '**不声明**（水平投影 ≠ 三维认证保障体积）'],
    ['导航关联范围', `${band.navigation_association_radius_m} m（与本带宽是两件事）`],
    ['语义', band.semantics || '—'],
    ['限制', FIXED_CORRIDOR_DISCLAIMER],
  ];
  if (authority !== 'current') rows.push(['预览限制', FIXED_CORRIDOR_PREVIEW_DISCLAIMER]);
  return rows;
}

/**
 * 服务端 `fixed-corridor-bands` 响应 → 实体。
 *
 * 返回 `{ entities, groups, diagnostics }`；`groups` 里每项带 `setVisible`，
 * 便于"整体开关 + 逐带开关"两级控制（与 Radar 面阵同一模式）。
 */
export function addFixedCorridorBandEntities(viewer, bands, options = {}) {
  const { Cesium, undulationAt } = options;
  const entities = [];
  const groups = [];
  const skipped = [];

  for (const band of bands || []) {
    const { rings, authority } = corridorRingsFor(band);
    if (!authority) {
      skipped.push({
        part: band.route_id || 'unknown',
        reason: band.preview_allowed === false
          ? (band.geometry_reason || 'geometry_not_available')
          : 'no_drawable_ring',
      });
      continue;
    }
    const baseColor = Cesium.Color.fromCssColorString(FIXED_CORRIDOR_STYLE.fill);
    const outlineColor = Cesium.Color.fromCssColorString(FIXED_CORRIDOR_STYLE.outline);
    const verticalConfirmed = band.vertical_extent_status === 'confirmed'
      && numberOrNull(band.nominal_altitude_m) !== null;
    const groundAt = typeof options.terrainHeightAt === 'function'
      ? options.terrainHeightAt : null;

    const groupEntities = [];
    rings.forEach((ring, ringIndex) => {
      const heights = [];
      const sliceTop = [];
      let incomplete = false;
      for (const point of ring) {
        const longitude = Number(point[0]);
        const latitude = Number(point[1]);
        if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
          incomplete = true;
          break;
        }
        // 逐点解析该点的椭球高：geoid N **逐点**取，绝不用全场常数 N 顶替。
        const ground = groundAt ? groundAt(longitude, latitude) : null;
        if (ground === null || ground === undefined || !Number.isFinite(Number(ground))) {
          incomplete = true;
          break;
        }
        const z = Number(ground);
        if (verticalConfirmed) {
          // 已确认固定巡航层：在**该点的**椭球地面高之上叠加标称正高，
          // 画一层薄带（只是标称层切片，不是保障体积的上下界）。
          const layerZ = z + numberOrNull(band.nominal_altitude_m);
          heights.push(Cesium.Cartesian3.fromDegrees(
            longitude, latitude, layerZ - FIXED_CORRIDOR_STYLE.layerSliceThicknessM));
          sliceTop.push(Cesium.Cartesian3.fromDegrees(
            longitude, latitude, layerZ + FIXED_CORRIDOR_STYLE.layerSliceThicknessM));
        } else {
          // 无垂直证据：只画水平目标带（贴地抬升一点点避免 z-fighting）。
          heights.push(Cesium.Cartesian3.fromDegrees(
            longitude, latitude, z + FIXED_CORRIDOR_STYLE.groundLiftM));
        }
      }
      if (incomplete || heights.length < 3) {
        skipped.push({
          part: `${band.route_id || 'unknown'}#${ringIndex}`,
          reason: 'terrain_height_or_geoid_unavailable_for_this_band',
        });
        return;
      }
      const polygon = viewer.entities.add({
        name: `${band.route_id} 固定 1000 m CNS 保障目标带`
          + (authority === 'current' ? '' : '（非权威历史预览）'),
        polygon: {
          hierarchy: new Cesium.PolygonHierarchy(heights),
          // 非 current 时用橙色边框 + 更低透明度，与 current 带一眼可分。
          material: baseColor.withAlpha(
            authority === 'current' ? FIXED_CORRIDOR_STYLE.fillAlpha
              : FIXED_CORRIDOR_STYLE.fillAlpha * 0.6),
          outline: true,
          outlineColor: authority === 'current' ? outlineColor
            : Cesium.Color.fromCssColorString('#ff9f43'),
          outlineWidth: FIXED_CORRIDOR_STYLE.outlineWidth,
          perPositionHeight: true,
        },
      });
      polygon.__cns = {
        kind: 'fixed-corridor-band',
        band,
        authority,
        title: `${band.route_id} 固定 1000 m CNS 保障目标带`
          + (authority === 'current' ? '' : '（非权威历史预览）'),
        rows: corridorPopupRows(band, authority),
      };
      entities.push(polygon);
      groupEntities.push(polygon);

      if (verticalConfirmed && sliceTop.length === heights.length) {
        // 顶面：让"这一层是标称巡航层切片"在三维里可见（**不是**保障体积上界）。
        const top = viewer.entities.add({
          name: `${band.route_id} 标称巡航层切片（非保障体积）`,
          polygon: {
            hierarchy: new Cesium.PolygonHierarchy(sliceTop),
            material: baseColor.withAlpha(0.14),
            outline: false,
            perPositionHeight: true,
          },
        });
        top.__cns = {
          kind: 'fixed-corridor-layer-slice',
          band,
          authority,
          title: `${band.route_id} 标称巡航层切片（${band.altitude_layer_id || '—'}）`,
          rows: corridorPopupRows(band, authority).concat([
            ['切片语义', '标称巡航高度层切片：显示用，**不是**认证三维保障体积的上下界'],
          ]),
        };
        entities.push(top);
        groupEntities.push(top);
      }
    });

    if (groupEntities.length) {
      groups.push({
        route_id: band.route_id,
        authority,
        currentness: band.currentness,
        vertical_extent_status: band.vertical_extent_status,
        altitude_layer_id: band.altitude_layer_id,
        visible: true,
        entities: groupEntities,
      });
    }
  }

  const setVisible = (visible) => {
    for (const group of groups) {
      for (const entity of group.entities) entity.show = Boolean(visible) && group.visible;
    }
  };
  return { entities, groups, setVisible, diagnostics: { skipped } };
}
