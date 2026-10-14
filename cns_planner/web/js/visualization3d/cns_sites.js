/**
 * 三维 CNS 站址与设备身份的 Cesium 实体构造（Round38-T3D4）。
 *
 * 与既有的 `radar_volume.js` / `protection_volume.js` 同样的边界：本模块**只做**
 * 「服务端记录 → 实体」的纯构造，依赖由调用方注入（`{ Cesium, undulationAt }`），
 * 因此可以在 Node 里直接单测，不需要 DOM 或真实 Viewer。
 *
 * 硬约定（违反即返工）：
 *
 *  * **站址存在绝不推断设备已安装**：设备只按服务端给出的四类身份渲染；
 *    候选站址只画站址符号 + 灰环，绝不画成已布设设备；
 *  * **铁塔高度按两种来源分别画，绝不混为一谈**：
 *    ① `structure_geometry_authority === 'resolved_tower_top'`（有完整垂直基准的塔顶
 *    事实）→ 按真实高度画塔身；
 *    ② `structure_geometry_authority === 'planning_estimate'`（只有既有工程估算证据：
 *    源表海拔 + 塔身高度）→ 只画**明确标注**的工程示意塔身，虚线描边、弹窗写明
 *    "非已确认 EGM2008 塔顶正高、未计入楼面建筑高度"；
 *    两种来源都没有时**不**画任何竖直结构，只画地面符号并显式标注塔顶未知。
 *    因此"不能精确绘制塔体"与"不能作为工程候选"是两件事，绝不互相推断。
 *  * **地面站绝不继承塔顶高度**：地面站用服务原点（缺证则贴地）绘制；
 *  * `stale` / `planned` / `blocked` / `unknown` 一律如实展示，不伪装为已确认；
 *  * 正高 → 椭球高逐点 `h = H + N`；`N` 未解析时该实体标记为高度不完整，
 *    绝不把高度当 0 静默渲染。
 */

//: 设备类别配色（与服务端 `SERVICE_CATEGORY_COLORS` 同值，也与二维一致）。
export const CNS_SERVICE_STYLE = Object.freeze({
  C: { color: '#37d67a', label: '通信' },
  N: { color: '#ffd166', label: '导航' },
  RID: { color: '#ff9f43', label: 'RID 合作监视' },
  Radar: { color: '#4d9dff', label: 'Radar 非合作监视' },
  other: { color: '#9fb0c0', label: '未归类' },
});

//: 站址状态配色与线型（四类身份必须一眼可分）。
export const CNS_SITE_STATE_STYLE = Object.freeze({
  existing_facility: {
    color: '#5ce08a', label: '既有真实设施',
    outline: '#0f3d24', dashed: false, pixelSize: 12,
  },
  planned_selected: {
    color: '#ffb454', label: '规划选中（待确认）',
    outline: '#7c541b', dashed: true, pixelSize: 10,
  },
  candidate_unselected: {
    color: '#8fa3b5', label: '候选未选中',
    outline: '#3a4a58', dashed: false, pixelSize: 6,
  },
  blocked: {
    color: '#ff6b6b', label: 'blocked / 缺证',
    outline: '#5a1f26', dashed: true, pixelSize: 7,
  },
});

//: 铁塔结构的显示参数（真实塔顶证据 / 工程估算 / 未知塔顶三者必须可分）。
export const CNS_TOWER_STYLE = Object.freeze({
  topKnown: { color: '#4dd2ff', outline: '#0d3b52' },
  //: 仅工程估算证据：与已确认塔顶同色系但更淡 + 虚线描边，一眼可辨"非确认高度"。
  estimated: { color: '#7fa8c4', outline: '#ffb454' },
  topUnknown: { color: '#9fb0c0', outline: '#3a4a58' },
  //: 塔身圆柱的最小可见半径（米）。铁塔是细高结构，过细会在远景消失。
  minimumRadiusM: 1.6,
  //: 塔顶设备标识环的高度偏移（相对塔顶，米）。
  deviceRingOffsetM: 2.2,
});

//: 站名标签字体：需求要求"铁塔图标和站名保持易读，不采用过小字体"。
export const CNS_LABEL_FONT = '13px "Microsoft YaHei", "Segoe UI", sans-serif';
export const CNS_LABEL_FONT_PICKED = 'bold 15px "Microsoft YaHei", "Segoe UI", sans-serif';

const SITE_LABEL_LIMIT = 120;

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * 正高 → 椭球高：逐点 `h = H + N`。
 *
 * 返回 `null` 表示**无法解析**（缺少 geoid N），调用方必须据此把实体标记为高度
 * 不完整，绝不当成 0。
 */
export function ellipsoidalHeight(orthometricM, longitude, latitude, undulationAt) {
  const ortho = numberOrNull(orthometricM);
  if (ortho === null) return null;
  const n = undulationAt(longitude, latitude);
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return null;
  return ortho + Number(n);
}

function stateStyle(siteState) {
  return CNS_SITE_STATE_STYLE[siteState] || CNS_SITE_STATE_STYLE.candidate_unselected;
}

function categoryStyle(category) {
  return CNS_SERVICE_STYLE[category] || CNS_SERVICE_STYLE.other;
}

/** 站址弹窗字段（身份、来源、证据状态——需求要求点击站址可查看这三项）。 */
export function sitePopupRows(site) {
  const rows = [
    ['站址身份', `${site.site_kind_label || site.site_kind || '—'}`],
    ['布设状态', `${site.site_state_label || site.site_state}（${site.site_id}）`],
    ['名称', site.name || '—'],
    ['站点类型', site.site_type || '—'],
    ['经度 / 纬度', `${Number(site.longitude).toFixed(6)} / ${Number(site.latitude).toFixed(6)}`],
    ['安装位置语义', site.installation_position_label || '—'],
    ['塔顶正高', site.tower_top_orthometric_m === null
      || site.tower_top_orthometric_m === undefined
      ? 'unknown（未解析，绝不当 0）' : `${site.tower_top_orthometric_m} m（${site.tower_top_status}）`],
    ['地形正高', site.terrain_elevation_m === null || site.terrain_elevation_m === undefined
      ? '未解析' : `${site.terrain_elevation_m} m（${site.terrain_elevation_source || '—'}）`],
    ['塔身结构高度', site.tower_structure_height_m === null
      || site.tower_structure_height_m === undefined
      ? '未解析' : `${site.tower_structure_height_m} m（源文件字段，非已确认高度）`],
    ['三维结构几何', site.structure_geometry_available
      ? `有（依据：${site.structure_geometry_basis}）`
      : `无（${site.structure_geometry_blocked_reason || '缺证'}）`],
    ['结构几何权威性', site.structure_geometry_authority === 'resolved_tower_top'
      ? '已解析塔顶正高（strict）'
      : site.structure_geometry_authority === 'planning_estimate'
        ? '工程估算（planning estimate，非已确认塔顶）'
        : '无（不可绘制塔体）'],
    ...(site.planning_estimate_origin_egm2008_m === null
      || site.planning_estimate_origin_egm2008_m === undefined ? [] : [[
      '工程估算原点', `${site.planning_estimate_origin_egm2008_m} m`
        + '（源表海拔 + 塔身高度；未计入楼面建筑高度；'
        + '仅供明确标注的工程规划与非权威展示，不得据以认定 LOS 通过）',
    ]]),
    ['垂直状态', site.vertical_status || 'unknown'],
    ['基座类型', site.base_type || 'unknown'],
    ['空间资格', `${site.spatial_use_status || '—'}`
      + (site.spatial_block_reason ? `（${site.spatial_block_reason}）` : '')],
    ['坐标来源', site.coordinate_source || '—'],
  ];
  const devices = site.devices || [];
  if (devices.length) {
    rows.push(['设备（按身份登记）', devices.map((row) => {
      const style = categoryStyle(row.category);
      return `${style.label}·${row.device_id || '—'}（${row.installation_status || '—'}）`;
    }).join('；')]);
  } else {
    rows.push(['设备', '无已登记设备（候选站址不推断设备已安装）']);
  }
  if ((site.existing_device_ids || []).length) {
    rows.push(['既有设备清单', site.existing_device_ids.join('、')]);
  }
  if (site.planned_panel_count) {
    rows.push(['已登记面阵', `${site.planned_panel_count} 个（规划条目，未确认安装）`]);
  }
  rows.push(['节点总量', '站址存在 ≠ 设备已安装；规划安装高度 ≠ 真实已安装高度']);
  return rows;
}

/** 按设备类别分组，供图例与分组显示使用。 */
export function deviceCategoryGroups(sites) {
  const groups = new Map();
  for (const site of sites || []) {
    for (const device of site.devices || []) {
      const category = device.category || 'other';
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push({ site, device });
    }
  }
  return groups;
}

/**
 * 服务端 `cns-sites` 响应 → 实体 + 分组。
 *
 * 返回 `{ entities, towers, groundSites, plans, devices, diagnostics }`：
 *  - `towers`      —— 有塔顶证据、**画了竖直结构**的铁塔实体；
 *  - `groundSites` —— 地面站 / 无塔顶证据的铁塔（地面符号）；
 *  - `devices`     —— 按设备类别着色的标识实体（挂在其宿主站址高度上）；
 *  - `plans`       —— 规划选中但**不在**候选快照里的地面站动作（待确认、虚线）；
 *  - `diagnostics` —— 跳过项与原因（绝不静默丢弃）。
 */
export function addCnsSiteEntities(viewer, sites, options = {}) {
  const { Cesium, undulationAt } = options;
  const entities = [];
  const towers = [];
  const groundSites = [];
  const plans = [];
  const devices = [];
  const skipped = [];
  const labelBudget = {
    used: 0,
    limit: options.labelLimit || SITE_LABEL_LIMIT,
    //: 候选站址（数百个）的标签上限；避免图面被候选站名淹没。
    candidateLimit: options.candidateLabelLimit || 40,
  };

  const requires = (value, label) => {
    if (!value) {
      skipped.push({ part: label, reason: 'cesium_or_undulation_missing' });
      return false;
    }
    return true;
  };
  if (!requires(Cesium, 'cns_sites') || !requires(undulationAt, 'cns_sites')) {
    return { entities, towers, groundSites, plans, devices, skipped,
             diagnostics: { skipped, counts: {} } };
  }

  const wantsLabel = (site) => {
    if (!site.name) return false;
    if (labelBudget.used >= labelBudget.limit) return false;
    // 需求要求"铁塔图标和站名保持易读"：既有设施与规划选中站址**始终**带标签，
    // 候选站址只在其标签预算内带标签——候选有数百个，全部打标签会让图面不可读
    // （那反而让"哪些是已布设"更难判读）。被省略标签的候选站址仍有完整弹窗。
    const priority = site.site_state === 'existing_facility'
      || site.site_state === 'planned_selected'
      || site.site_state === 'blocked';
    if (!priority && labelBudget.used >= labelBudget.candidateLimit) return false;
    labelBudget.used += 1;
    return true;
  };

  for (const site of sites || []) {
    const longitude = numberOrNull(site.longitude);
    const latitude = numberOrNull(site.latitude);
    if (longitude === null || latitude === null) {
      skipped.push({ part: site.site_id || 'unknown', reason: 'coordinate_missing' });
      continue;
    }
    const style = stateStyle(site.site_state);
    const isTower = site.installation_position === 'tower_top';
    //: 两种高度来源严格分开：strict 塔顶正高优先；否则才可能是工程估算。
    const strictTop = numberOrNull(site.tower_top_orthometric_m);
    const estimateTop = site.structure_geometry_authority === 'planning_estimate'
      ? numberOrNull(site.planning_estimate_origin_egm2008_m) : null;
    const hasStructure = Boolean(site.structure_geometry_available) && isTower
      && (strictTop !== null || estimateTop !== null);
    const structureIsEstimate = hasStructure && strictTop === null;
    const structureHeight = strictTop !== null ? strictTop : estimateTop;
    const towerColor = !hasStructure
      ? Cesium.Color.fromCssColorString(CNS_TOWER_STYLE.topUnknown.color)
      : Cesium.Color.fromCssColorString(structureIsEstimate
        ? CNS_TOWER_STYLE.estimated.color : CNS_TOWER_STYLE.topKnown.color);

    // ---- 地面锚点：一律画（站址身份必须可见） ------------------------------
    const groundHeight = ellipsoidalHeight(
      site.terrain_elevation_m, longitude, latitude, undulationAt);
    const groundIncomplete = site.terrain_elevation_m !== null
      && site.terrain_elevation_m !== undefined && groundHeight === null;
    const anchor = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(
        longitude, latitude, groundHeight === null ? 0 : groundHeight),
      point: {
        pixelSize: style.pixelSize,
        color: Cesium.Color.fromCssColorString(style.color),
        outlineColor: Cesium.Color.fromCssColorString(style.outline),
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: site.name || site.site_id,
      label: wantsLabel(site) ? {
        text: site.name,
        font: CNS_LABEL_FONT,
        fillColor: Cesium.Color.fromCssColorString(
          site.site_state === 'candidate_unselected' ? '#cfe0ee' : '#ffffff'),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        // 候选站址标签下移，避免与"已布设"站址混淆。
        pixelOffset: new Cesium.Cartesian2(0, site.site_state === 'candidate_unselected'
          ? 14 : -18),
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      } : undefined,
    });
    anchor.__cns = {
      kind: 'cns-site',
      site,
      title: `${site.site_state_label || site.site_state} · ${site.name || site.site_id}`,
      rows: sitePopupRows(site),
    };
    entities.push(anchor);

    // ---- 铁塔竖直结构：有 strict 塔顶或已标注的工程估算高度时才画 -------------
    if (hasStructure) {
      const topHeight = ellipsoidalHeight(
        structureHeight, longitude, latitude, undulationAt);
      if (topHeight !== null && groundHeight !== null && topHeight > groundHeight) {
        const structure = viewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(longitude, latitude, groundHeight),
          cylinder: {
            length: topHeight - groundHeight,
            topRadius: CNS_TOWER_STYLE.minimumRadiusM,
            bottomRadius: CNS_TOWER_STYLE.minimumRadiusM * 1.6,
            material: towerColor.withAlpha(structureIsEstimate ? 0.38 : 0.55),
            outline: true,
            outlineColor: Cesium.Color.fromCssColorString(structureIsEstimate
              ? CNS_TOWER_STYLE.estimated.outline : CNS_TOWER_STYLE.topKnown.outline),
          },
          name: `${site.name || site.tower_id} 塔身`,
        });
        structure.__cns = {
          kind: 'tower-structure',
          site,
          title: `铁塔结构 ${site.name || site.tower_id}`
            + (structureIsEstimate ? '（工程估算示意）' : ''),
          rows: sitePopupRows(site).concat([
            ['结构绘制依据', structureIsEstimate
              ? 'terrain_elevation_m → 工程估算原点（planning_estimate；'
                + '非已确认 EGM2008 塔顶正高，未计入楼面建筑高度）'
              : 'terrain_elevation_m → tower_top_orthometric_m（既有塔顶正高）'],
            ['结构高度（米）', `${(topHeight - groundHeight).toFixed(3)}`],
            ['高度基准', structureIsEstimate
              ? '工程估算正高 → WGS84 椭球高（逐点 N）；仅工程示意，不作安全净空依据'
              : 'EGM2008 正高 → WGS84 椭球高（逐点 N）'],
          ]),
        };
        entities.push(structure);
        towers.push(structure);
      } else {
        skipped.push({
          part: site.site_id || 'unknown',
          reason: groundIncomplete ? 'geoid_undulation_unresolved'
            : 'tower_top_not_above_terrain',
        });
      }
    } else if (isTower) {
      // 塔但无塔顶证据：**不画**结构，只登记原因（unknown ≠ 0）。
      skipped.push({
        part: site.site_id || 'unknown',
        reason: site.structure_geometry_blocked_reason || 'tower_top_unresolved',
      });
    }

    if (!isTower) groundSites.push(anchor);
    else if (!hasStructure) groundSites.push(anchor);

    // ---- 设备标识：按类别着色，挂在宿主站址高度上 --------------------------
    for (const device of site.devices || []) {
      const deviceStyle = categoryStyle(device.category);
      const deviceId = device.device_id || 'device';
      for (const entity of addDeviceMarker(viewer, {
        Cesium, undulationAt, site, device, deviceStyle, deviceId,
      })) {
        entities.push(entity);
        devices.push(entity);
      }
    }
  }

  // ---- 规划选中但不在候选快照里的地面站动作（待确认，独立分组） ------------
  for (const plan of options.plannedDevices || []) {
    const longitude = numberOrNull(plan.longitude);
    const latitude = numberOrNull(plan.latitude);
    if (longitude === null || latitude === null) {
      skipped.push({ part: plan.device_id || 'planned', reason: 'coordinate_missing' });
      continue;
    }
    const deviceStyle = categoryStyle(plan.category);
    const entity = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(longitude, latitude, 0),
      point: {
        pixelSize: 9,
        color: Cesium.Color.fromCssColorString(deviceStyle.color).withAlpha(0.85),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      name: plan.device_id || plan.action_id,
    });
    entity.__cns = {
      kind: 'cns-planned-device',
      plan,
      title: `规划设备（待确认）· ${plan.device_id || plan.action_id}`,
      rows: [
        ['类别', deviceStyle.label],
        ['服务键', plan.service_key || '—'],
        ['安装状态', plan.installation_status || 'planned_selected_unconfirmed'],
        ['设备选型状态', plan.equipment_selection_status || '—'],
        ['安装位置语义', '地面站安装（规划）'],
        ['宿主站址', plan.linked_site_available
          ? `${plan.linked_site_id}` : '未在候选站址快照中找到（不画虚假宿主关系）'],
        ['经度 / 纬度', `${longitude.toFixed(6)} / ${latitude.toFixed(6)}`],
        ['来源', plan.source || 'cns_corridor_site_plan.selected_actions'],
        ['限制', '规划选中 ≠ 已安装设备；规划安装高度 ≠ 真实已安装高度'],
      ],
    };
    entities.push(entity);
    plans.push(entity);
  }

  return {
    entities, towers, groundSites, plans, devices,
    diagnostics: {
      skipped,
      counts: {
        sites: (sites || []).length,
        towers_with_structure: towers.length,
        ground_or_unknown_tower: groundSites.length,
        device_markers: devices.length,
        planned_devices: plans.length,
        labels: labelBudget.used,
      },
    },
  };
}

/** 单个设备标识（塔顶设备画在塔顶上方并带类别色环；地面设备画在地面站原点）。 */
function addDeviceMarker(viewer, { Cesium, undulationAt, site, device, deviceStyle, deviceId }) {
  const longitude = Number(site.longitude);
  const latitude = Number(site.latitude);
  const isTowerTop = (device.position_semantics === 'tower_top')
    || (site.installation_position === 'tower_top'
        && device.position_semantics !== 'ground_site');
  let base = null;
  let baseAuthority = 'tower_top_orthometric_m';
  if (isTowerTop) {
    //: 塔顶设备：优先 strict 塔顶正高；严格事实缺失且只有已标注的工程估算时才退回
    //: 估算高度（弹窗必须写明估算身份），绝不在无任何高度证据时贴地冒充塔顶。
    const strictTop = numberOrNull(site.tower_top_orthometric_m);
    const estimateTop = site.structure_geometry_authority === 'planning_estimate'
      ? numberOrNull(site.planning_estimate_origin_egm2008_m) : null;
    if (strictTop !== null) {
      base = ellipsoidalHeight(strictTop, longitude, latitude, undulationAt);
    } else if (estimateTop !== null) {
      base = ellipsoidalHeight(estimateTop, longitude, latitude, undulationAt);
      baseAuthority = 'planning_estimate_origin';
    }
  } else {
    base = ellipsoidalHeight(site.terrain_elevation_m, longitude, latitude, undulationAt);
    baseAuthority = 'terrain_elevation_m';
  }
  const heightIncomplete = base === null;
  const z = (base === null ? 0 : base) + (isTowerTop ? CNS_TOWER_STYLE.deviceRingOffsetM : 0);

  const entity = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(longitude, latitude, z),
    point: {
      pixelSize: isTowerTop ? 9 : 8,
      color: Cesium.Color.fromCssColorString(deviceStyle.color),
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    name: `${deviceStyle.label} ${deviceId}`,
  });
  entity.__cns = {
    kind: 'cns-device',
    site,
    device,
    title: `${deviceStyle.label} · ${deviceId}`,
    rows: [
      ['设备类别', deviceStyle.label],
      ['服务键', device.service_key || '—'],
      ['安装状态', device.installation_status || '—'],
      ['安装位置语义', device.position_semantics === 'ground_site' ? '地面站安装' : '塔顶安装'],
      ['物理挂载确认', device.physical_mount_confirmed === true
        ? '已确认' : '未确认（不得当作已安装高度）'],
      ['宿主站址', site.name || site.site_id],
      ['宿主布设状态', site.site_state_label || site.site_state],
      ['塔顶正高', site.tower_top_orthometric_m === null
        || site.tower_top_orthometric_m === undefined
        ? 'unknown' : `${site.tower_top_orthometric_m} m`],
      ['绘制高度基准', baseAuthority === 'planning_estimate_origin'
        ? '工程估算原点（planning_estimate；非已确认塔顶正高，仅工程示意）'
        : baseAuthority],
      ['绘制高度', heightIncomplete
        ? 'geoid N 未解析：高度不完整（未当 0 渲染）' : `${z.toFixed(3)} m（椭球高）`],
      ['来源', device.source || '—'],
    ],
  };
  return [entity];
}
