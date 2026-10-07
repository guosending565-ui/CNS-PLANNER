// =========================================================
// Round 3 前端契约测试：CNS service（Communication + RID）业务呈现与地图可视化。
//
// 覆盖：
//   1. Step05 显示 Communication：4 km / land 2 / sea 1；
//   2. Step05 显示 RID：land 2 km / sea 5 km / land 2 / sea 1；
//   3. RID **不**出现 90° sector / panel；
//   4. 5 km 图例文案必须是「海上最大规划半径」，绝不能叫全域覆盖半径；
//   5. service 结果里 S:rid_cooperative 显示 RID 合作监视，不退化成普通 S；
//   6. P16 target service_key=S:rid_cooperative → UI 显示 RID；
//   7. distinct_site_count=1 / required=2 →「1 / 2 个不同物理站址」+ 冗余不足；
//   8. 两设备同 distinct_site_id 时前端不得自行显示"两重"；
//   9. unknown surface → 证据不足，绝不显示 sea；
//  10. Communication / RID 地图图层默认关闭；
//  11. Radar overlay 原测试条件保持不变（本文件不触碰 radar 模块）；
//  12. 旧 C/N/S fixture 前端显示不回归。
//
// 全部断言只读 front-end 纯函数与静态 DOM 默认值：没有 jsdom，也不伪造浏览器。
// =========================================================
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  cnsServiceLegendModel, cnsServiceOverlayModel, coordinateAtFraction, drawCnsServiceOverlay,
  gapSegmentGeometry, radiusBySurfaceOf, radiusPixel, stateForSegment,
} from '../cns_planner/web/js/map/cns_service_overlay.js';
import {
  CNS_GAP_COLORS, CNS_GAP_STATE_LABELS, COMBINED_STATUS_MAP_STATE,
  RID_SEA_PLANNING_RADIUS_LABEL, SERVICE_KEY_LABELS, SERVICE_LEGEND_LABELS, SURFACE_CLASS_TEXT,
  distinctSiteCountText, sameDistinctSite,
} from '../cns_planner/web/js/map/service_semantics.js';
import {
  SURFACE_FACTS_BASIS_NOTE, facilityPlanCards, isSurfaceAwareDevice, renderServiceProfiles,
  renderSurfaceFactsPanel, serviceCorridorEvidence, serviceGapStatements, surfaceFactsModel,
} from '../cns_planner/web/js/workflow/cns_service_evidence.js';
import {render as renderStep5, bind as bindStep5} from '../cns_planner/web/js/workflow/step05_cns.js';
import {render as renderStep4} from '../cns_planner/web/js/workflow/step04_operation.js';
import {createWorkflowSnapshotApplier} from '../cns_planner/web/js/state/workflow_snapshot.js';

const html = readFileSync(new URL('../cns_planner/web/index.html', import.meta.url), 'utf8');
const main = readFileSync(new URL('../cns_planner/web/js/main.js', import.meta.url), 'utf8');
const displayLayers = readFileSync(
  new URL('../cns_planner/web/js/map/display_layers.js', import.meta.url), 'utf8');

//: Round 3 新增的五个分析图层开关（必须全部默认关闭）。
const ROUND3_LAYER_IDS = [
  'cnsCommunicationLayer', 'cnsRidLayer', 'cnsServiceGapLayer', 'cnsFacilityPlanLayer',
  'surfaceFactsLayer',
];

/** 图层抽屉里 id → <input> 标签原文。 */
function checkboxAttributes(source) {
  const found = new Map();
  for (const match of source.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[0]);
    if (id) found.set(id[1], match[0]);
  }
  return found;
}

/**
 * 去掉注释后的**可执行代码**。
 *
 * 结构护栏只应约束真正会跑的代码：文档注释里允许出现
 * "绝不用 actual >= required" 这类说明文字。
 */
function stripComments(source) {
  return String(source)
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/**
 * 粗粒度函数体切片：从 `function xxx(` / `const xxx=(` 到下一个顶层声明。
 *
 * 只用于结构护栏的"同一函数内不允许同时出现 X 与 Y"判定，不做语法解析。
 */
function functionBodies(code) {
  const starts = [];
  for (const match of code.matchAll(/^(?:export\s+)?(?:async\s+)?function\s+[A-Za-z0-9_$]+|^(?:export\s+)?const\s+[A-Za-z0-9_$]+\s*=\s*(?:async\s*)?\(/gm)) {
    starts.push(match.index);
  }
  const bodies = [];
  for (let index = 0; index < starts.length; index += 1) {
    const end = index + 1 < starts.length ? starts[index + 1] : code.length;
    bodies.push(code.slice(starts[index], end));
  }
  return bodies;
}

// ---- fixtures ---------------------------------------------------------------

/** Communication 设备：radius_by_surface 三档相同（后端 canonical 事实）。 */
const COMMUNICATION_DEVICE = {
  device_id: 'C-COMM-1', subsystem: 'C', name: '通信全向站',
  service_key: 'C:communication', enabled: true,
  coverage_geometry: {
    model: 'hemisphere', model_scope: 'geometric_only', confirmed: false,
    status: 'pending_confirmation', slant_range_m: null,
    radius_by_surface: {land: 4000, coastal_uncertain: 4000, sea: 4000},
  },
};

/** RID 设备：land/coastal 2 km、sea 5 km、urban 1 km（仅设备事实）。 */
const RID_DEVICE = {
  device_id: 'S-RID-1', subsystem: 'S', name: 'RID 接收站',
  service_key: 'S:rid_cooperative', enabled: true,
  type: {service_subtype: 'cooperative_surveillance', target_cooperation: 'cooperative',
    technology: 'network_remote_id'},
  coverage_geometry: {
    model: 'hemisphere', model_scope: 'geometric_only', confirmed: false,
    status: 'pending_confirmation', slant_range_m: null,
    radius_by_surface: {land: 2000, coastal_uncertain: 2000, sea: 5000},
    urban_radius_m: 1000, urban_enabled: false,
  },
};

/** 旧 C/N/S fixture（legacy 项目：没有 service_key，也没有 radius_by_surface）。 */
const LEGACY_DEVICES = [
  {device_id: 'C-LEGACY', subsystem: 'C', name: '通信主站示例', radius_m: 5000, mtbf_h: 20000, role: 'primary'},
  {device_id: 'N-LEGACY', subsystem: 'N', name: '导航主站示例', radius_m: 4000, mtbf_h: 22000, role: 'primary'},
  {device_id: 'S-LEGACY', subsystem: 'S', name: '监视主站示例', radius_m: 4500, mtbf_h: 18000, role: 'primary'},
];

function baseFlow(extra = {}) {
  return {
    project: {name: 'Round3 前端契约'},
    devices: [],
    defaults: {engineering_parameters: {
      primary_spacing_factor: {value: 0.9, source: 'user_configuration'},
      co_location_search_radius_m: {value: 2000, source: 'user_configuration'},
    }},
    steps: {},
    risks: {life: {status: 'not_calculated'}, property: {status: 'not_calculated'}},
    operational_routes: [], scenario_routes: [],
    ...extra,
  };
}

/** 带 canonical service contract 的 flow（含正式需求里的 redundancy_by_surface）。 */
function serviceFlow(extra = {}) {
  return baseFlow({
    device_catalog: {
      status: 'passed', count: 2, source: 'user_configuration',
      items: [COMMUNICATION_DEVICE, RID_DEVICE],
    },
    required_cns: {
      status: 'passed', source: 'user_configuration',
      project_default: {
        communication: {
          required: true, service_key: 'C:communication',
          redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1},
        },
        surveillance: {
          required: true, service_key: 'S:rid_cooperative',
          service_subtype: 'cooperative_surveillance',
          type: {technology: 'network_remote_id', target_cooperation: 'cooperative'},
          redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1},
        },
      },
    },
    ...extra,
  });
}

/** P15 缺口结果：RID 陆地 1/2（冗余不足），Communication 海上 1/1（满足）。 */
const CORRIDOR_GAP = {
  status: 'passed',
  routes: [{
    route_id: 'R-TAOHUA-HANJING', status: 'failed',
    subsystems: [{
      subsystem: 'S', status: 'failed', combined_status: 'confirmed_gap',
      service_redundancy: [{
        service_key: 'S:rid_cooperative', subsystem: 'S', surface_dependent: true,
        voxel_count: 12, status: 'confirmed_deficit',
        status_counts: {satisfied: 5, confirmed_deficit: 6, unknown: 1},
        required_distinct_site_count_by_surface: {land: 2, coastal_uncertain: 2},
        distinct_site_count_by_surface: {land: 1, coastal_uncertain: 1},
        surface_class_counts: {land: 8, coastal_uncertain: 2, unknown: 2},
      }],
      continuous_deficit_segments: [{
        segment_id: 'R1:S:spatial-deficit:1', route_id: 'R-TAOHUA-HANJING', subsystem: 'S',
        start_route_offset_m: 0, end_route_offset_m: 1000, length_m: 1000,
        causes: ['redundancy_deficit'], voxel_ids: ['V1', 'V2'],
      }],
    }, {
      subsystem: 'C', status: 'passed', combined_status: 'satisfied',
      service_redundancy: [{
        service_key: 'C:communication', subsystem: 'C', surface_dependent: true,
        voxel_count: 12, status: 'satisfied',
        status_counts: {satisfied: 12, confirmed_deficit: 0, unknown: 0},
        required_distinct_site_count_by_surface: {sea: 1},
        distinct_site_count_by_surface: {sea: 1},
        surface_class_counts: {sea: 12},
      }],
      continuous_deficit_segments: [],
    }],
  }],
};

// ---- 1 / 2. Profile 基线 -----------------------------------------------------

test('step05 renders the Communication engineering baseline from canonical fields', () => {
  const flow = serviceFlow();
  const panel = renderServiceProfiles(flow);
  assert.match(panel, /Communication 工程规划基线/);
  // 水平 360° 全向
  assert.match(panel, /水平：全向 360°/);
  // 陆地 4.0 km / 海上 4.0 km / 海岸不确定 4.0 km
  assert.match(panel, /<b>陆地<\/b><\/span><small>4\.0 km · 要求 2 个不同物理站址<\/small>/);
  assert.match(panel, /<b>海上<\/b><\/span><small>4\.0 km · 要求 1 个不同物理站址<\/small>/);
  assert.match(panel, /<b>海岸不确定<\/b><\/span><small>4\.0 km · 要求 2 个不同物理站址<\/small>/);
  // geometric_only
  assert.match(panel, /geometric_only/);
  // 工程规划基线 ≠ 厂家验证性能 ≠ 实测传播覆盖
  assert.match(panel, /工程规划基线 ≠ 厂家验证性能 ≠ 实测传播覆盖/);
  // 未评估项完整出现
  for (const area of ['RF 传播', '地形视距', '建筑遮挡', '链路预算', '灵敏度', '干扰']) {
    assert.match(panel, new RegExp(area.replace(/[()（）]/g, '\\$&')), `未评估项缺失：${area}`);
  }
  assert.match(panel, /未评估 \/ 缺证据/);
});

test('step05 renders the RID engineering baseline with both planning radii', () => {
  const flow = serviceFlow();
  const panel = renderServiceProfiles(flow);
  assert.match(panel, /RID 合作监视 工程规划基线/);
  assert.match(panel, /<b>陆地<\/b><\/span><small>2\.0 km · 要求 2 个不同物理站址<\/small>/);
  assert.match(panel, /<b>海上<\/b><\/span><small>5\.0 km · 要求 1 个不同物理站址<\/small>/);
  assert.match(panel, /<b>海岸不确定<\/b><\/span><small>2\.0 km · 要求 2 个不同物理站址<\/small>/);
  // 城区 1 km 只作为设备事实，城区规划未启用
  assert.match(panel, /城区 1 km：仅保留设备事实，当前未启用城区规划/);
  assert.match(panel, /仅设备事实（未启用城区规划）/);
  assert.match(panel, /geometric_only/);
});

test('service profile never invents parameters when the catalog has no canonical entry', () => {
  const panel = renderServiceProfiles(baseFlow({device_catalog: {status: 'passed', items: []}}));
  assert.match(panel, /未配置/);
  assert.doesNotMatch(panel, /4\.0 km/);
  assert.doesNotMatch(panel, /5\.0 km/);
  assert.match(panel, /不得由前端编造/);
});

test('surface aware devices are detected from radius_by_surface, not from a single radius_m', () => {
  assert.equal(isSurfaceAwareDevice(COMMUNICATION_DEVICE), true);
  assert.equal(isSurfaceAwareDevice(RID_DEVICE), true);
  assert.equal(isSurfaceAwareDevice({subsystem: 'C', radius_m: 5000}), false);
  assert.equal(isSurfaceAwareDevice({subsystem: 'C', coverage_geometry: {radius_by_surface: {}}}), false);
});

// ---- 3. RID 不是雷达 ---------------------------------------------------------

test('RID is never rendered as a radar sector or 90 degree panel', () => {
  const flow = serviceFlow();
  const rendered = [
    renderServiceProfiles(flow),
    renderSurfaceFactsPanel(flow),
    cnsServiceLegendModel().map(line => line.label + '|' + line.note).join('\n'),
  ].join('\n');
  // 明确声明 RID 是全向，并与 Radar 方向性面阵区分
  assert.match(rendered, /水平：全向 360°/);
  assert.match(rendered, /RID 绝不使用 Radar 的 90° 面阵 \/ sector \/ 方位角/);
  assert.match(rendered, /RID 合作监视：全向几何/);
  assert.match(rendered, /方向性面阵只属于 Radar Non-cooperative Surveillance/);
  // RID 的几何行只能是"全向"，绝不出方位角 / 半宽 / panel
  assert.doesNotMatch(rendered, /方位角\s*\d/);
  assert.doesNotMatch(rendered, /panel_id|half_width/);
  assert.doesNotMatch(rendered, /雷达扇区|扇区半径/);
  // 图例把 RID 与 Radar 分成两行，绝不都只叫 "S"
  const ridLine = cnsServiceLegendModel().find(line => line.id === 'cns-rid-geometry');
  assert.match(ridLine.label, /RID Cooperative Surveillance/);
  assert.match(ridLine.note, /RID 绝不画 sector \/ 90° panel/);
});

test('RID overlay model never emits azimuth or panel fields', () => {
  const model = cnsServiceOverlayModel(serviceFlow({
    existing_cns_facilities: {
      status: 'passed', count: 1,
      items: [{
        facility_id: 'F1', name: 'RID 站', coordinate: [122.2, 30.0],
        devices: [{
          device_id: 'S-RID-1', subsystem: 'S', status: 'active',
          service_key: 'S:rid_cooperative',
          coverage_geometry: {radius_by_surface: {land: 2000, sea: 5000}},
        }],
      }],
    },
  }));
  const serialized = JSON.stringify(model);
  assert.doesNotMatch(serialized, /azimuth/i);
  assert.doesNotMatch(serialized, /half_width/i);
  assert.doesNotMatch(serialized, /panel/i);
  const site = model.sites.find(item => item.service_key === 'S:rid_cooperative');
  assert.equal(site.radius_by_surface.sea, 5000);
  assert.equal(site.radius_by_surface.land, 2000);
});

// ---- 4. 5 km 文案 -----------------------------------------------------------

test('the 5 km circle is called the offshore maximum planning radius, never a coverage radius', () => {
  assert.equal(RID_SEA_PLANNING_RADIUS_LABEL, 'RID 海上最大规划半径');
  const legend = cnsServiceLegendModel();
  const seaLine = legend.find(line => line.id === 'cns-rid-sea');
  assert.ok(seaLine, 'RID 海上最大规划半径图例行必须存在');
  assert.match(seaLine.label, /RID 海上最大规划半径/);
  assert.doesNotMatch(seaLine.label, /覆盖范围|全域覆盖半径|覆盖半径/);
  // 图例必须写清实际结论由航路采样点 surface_class 决定
  assert.match(seaLine.note, /surface_class/);
  assert.match(seaLine.note, /land\/coastal → land 半径，sea → 本半径/);
  assert.match(seaLine.note, /本虚线不是"RID 覆盖范围"/);
});

test('the service legend keeps Communication, RID and Radar conceptually separate', () => {
  const legend = cnsServiceLegendModel();
  const searchable = legend.map(line => line.label + '|' + line.note).join('\n');
  assert.match(searchable, /Communication/);
  assert.match(searchable, /RID 合作监视：全向几何/);
  assert.match(searchable, /RID Cooperative Surveillance/);
  assert.equal(SERVICE_LEGEND_LABELS['S:rid_cooperative'],
    'RID Cooperative Surveillance（全向，非方向性面阵）');
  assert.equal(SERVICE_LEGEND_LABELS['S:surveillance'],
    'Radar Non-cooperative Surveillance（方向性面阵）');
  // 图例明确区分三种语义：RID 与 Radar 绝不都只显示成 "S"
  assert.notEqual(SERVICE_LEGEND_LABELS['S:rid_cooperative'],
    SERVICE_LEGEND_LABELS['S:surveillance']);
  // 缺口四态图例齐全且颜色沿用既有状态色系
  for (const state of ['satisfied', 'under_redundant', 'uncovered', 'unknown']) {
    assert.ok(CNS_GAP_COLORS[state], `缺少缺口状态色 ${state}`);
    assert.ok(CNS_GAP_STATE_LABELS[state], `缺少缺口状态词 ${state}`);
    assert.ok(legend.some(line => line.label === CNS_GAP_STATE_LABELS[state]),
      `图例缺少状态行 ${state}`);
  }
});

// ---- 5. service 结果不退化成 C/N/S ------------------------------------------

test('service results show RID 合作监视 instead of a bare S', () => {
  assert.equal(SERVICE_KEY_LABELS['S:rid_cooperative'], 'RID 合作监视');
  const statements = serviceGapStatements(serviceFlow({cns_corridor_gap_assessment: CORRIDOR_GAP}));
  assert.match(statements, /RID 合作监视/);
  assert.match(statements, /通信/);
  assert.doesNotMatch(statements, /<b>S · /);
  assert.doesNotMatch(statements, />S</);
});

test('P15 service statements expose service, surface and distinct-site counts only', () => {
  const statements = serviceGapStatements(serviceFlow({cns_corridor_gap_assessment: CORRIDOR_GAP}));
  // 服务
  assert.match(statements, /data-service-key="S:rid_cooperative"/);
  assert.match(statements, /data-service-key="C:communication"/);
  // surface_class
  assert.match(statements, /陆地/);
  assert.match(statements, /海上/);
  assert.match(statements, /海岸不确定/);
  // 实际 / 要求 个不同物理站址（后端事实，原样展示）
  assert.match(statements, /1 \/ 2 个不同物理站址/);
  assert.match(statements, /1 \/ 1 个不同物理站址/);
  // 绝不出现"N 台设备 = N 重"的说法
  assert.doesNotMatch(statements, /2 台设备|两台设备|2 重/);
});

test('P15 per-surface rows carry no front-end derived status', () => {
  const statements = serviceGapStatements(serviceFlow({cns_corridor_gap_assessment: CORRIDOR_GAP}));
  // 逐 surface 行只允许"计数 + 体元"：不得出现前端比较出来的状态词
  const perSurfaceRows = [...statements.matchAll(
    /<b>(陆地|海上|海岸不确定|证据不足)<\/b>[^]*?<\/div>/g)].map(match => match[0]);
  assert.ok(perSurfaceRows.length >= 2, '至少要有逐 surface 行');
  for (const line of perSurfaceRows) {
    // 计数事实：要么是 "N / M 个不同物理站址"，要么是后端没给数时的证据不足
    assert.match(line, /个不同物理站址|不同物理站址数：证据不足/);
    // 不得含前端推导的状态词（"证据不足"只允许作为 surface 名或计数缺失文案出现）
    assert.doesNotMatch(line, /冗余不足|已确认缺口|满足 ·|（满足）/,
      `逐 surface 行不得含前端推导的状态：${line}`);
  }
  // 逐 surface 状态只在后端直接给出 status_by_surface 时才渲染
  assert.doesNotMatch(statements, /逐 surface 状态（后端给出）/);
  const withBackendStatus = serviceGapStatements(serviceFlow({
    cns_corridor_gap_assessment: {status: 'passed', routes: [{
      route_id: 'R1', status: 'failed',
      subsystems: [{subsystem: 'S', status: 'failed', combined_status: 'confirmed_gap',
        service_redundancy: [{
          service_key: 'S:rid_cooperative', surface_dependent: true, voxel_count: 3,
          status: 'confirmed_deficit',
          status_counts: {satisfied: 0, confirmed_deficit: 3, unknown: 0},
          status_by_surface: {land: 'confirmed_deficit', sea: 'satisfied'},
          required_distinct_site_count_by_surface: {land: 2, sea: 1},
          distinct_site_count_by_surface: {land: 1, sea: 1},
          surface_class_counts: {land: 2, sea: 1},
        }]}],
    }]},
  }));
  assert.match(withBackendStatus, /逐 surface 状态（后端给出）/);
  assert.match(withBackendStatus, /陆地[^]*?<small>冗余不足<\/small>/);
  assert.match(withBackendStatus, /海上[^]*?<small>满足<\/small>/);
});

test('service status comes from the backend status field, never from counts', () => {
  // 计数看起来"满足"（2/2）但后端说 confirmed_deficit：必须显示后端结论
  const backendDeficit = serviceGapStatements(serviceFlow({
    cns_corridor_gap_assessment: {status: 'passed', routes: [{
      route_id: 'R1', status: 'failed',
      subsystems: [{subsystem: 'S', status: 'failed', combined_status: 'confirmed_gap',
        service_redundancy: [{
          service_key: 'S:rid_cooperative', surface_dependent: true, voxel_count: 2,
          status: 'confirmed_deficit',
          status_counts: {satisfied: 0, confirmed_deficit: 2, unknown: 0},
          required_distinct_site_count_by_surface: {land: 2},
          distinct_site_count_by_surface: {land: 2},
          surface_class_counts: {land: 2},
        }]}],
    }]},
  }));
  assert.match(backendDeficit, /data-service-status="confirmed_deficit"/);
  assert.match(backendDeficit, /RID 合作监视 · 冗余不足/);
  // 反向：计数看起来不足（1/2）但后端说 satisfied：仍然显示后端结论
  const backendSatisfied = serviceGapStatements(serviceFlow({
    cns_corridor_gap_assessment: {status: 'passed', routes: [{
      route_id: 'R1', status: 'passed',
      subsystems: [{subsystem: 'S', status: 'passed', combined_status: 'satisfied',
        service_redundancy: [{
          service_key: 'S:rid_cooperative', surface_dependent: true, voxel_count: 2,
          status: 'satisfied',
          status_counts: {satisfied: 2, confirmed_deficit: 0, unknown: 0},
          required_distinct_site_count_by_surface: {land: 2},
          distinct_site_count_by_surface: {land: 1},
          surface_class_counts: {land: 2},
        }]}],
    }]},
  }));
  assert.match(backendSatisfied, /data-service-status="satisfied"/);
  assert.match(backendSatisfied, /RID 合作监视 · 满足/);
  // 后端没给状态 → 证据不足，绝不补算（即使计数两边都有）
  const noStatus = serviceGapStatements(serviceFlow({
    cns_corridor_gap_assessment: {status: 'passed', routes: [{
      route_id: 'R1', status: 'passed',
      subsystems: [{subsystem: 'S', status: 'passed', combined_status: 'satisfied',
        service_redundancy: [{
          service_key: 'S:rid_cooperative', surface_dependent: true, voxel_count: 2,
          status_counts: {satisfied: 0, confirmed_deficit: 0, unknown: 0},
          required_distinct_site_count_by_surface: {land: 2},
          distinct_site_count_by_surface: {land: 1},
          surface_class_counts: {land: 2},
        }]}],
    }]},
  }));
  assert.match(noStatus, /data-service-status="unknown"/);
  assert.match(noStatus, /RID 合作监视 · 证据不足/);
});

test('P15 without any service evidence keeps the legacy subsystem wording', () => {
  const legacy = serviceFlow({
    cns_corridor_gap_assessment: {status: 'passed', routes: [{
      route_id: 'R-LEGACY', status: 'failed',
      subsystems: [{subsystem: 'C', status: 'failed', combined_status: 'confirmed_gap'}],
    }]},
  });
  const statements = serviceGapStatements(legacy);
  assert.match(statements, /没有 service 级冗余证据/);
  assert.match(statements, /legacy 项目保持子系统口径/);
});

// ---- 6 / 7 / 8. P16 ---------------------------------------------------------

/** P16 结果：RID target land 1/2；两条动作，第二条与第一条同 physical site。 */
const SITE_PLAN = {
  status: 'proposal_ready',
  selected_actions: [{
    action_id: 'tower_colocation_host:TT-023:S-RID-1',
    service_key: 'S:rid_cooperative', subsystem: 'S', device_id: 'S-RID-1',
    distinct_site_id: 'tower:TT-023', required_units: 2, current_units: 1,
    surface_class: 'land', coordinate: [122.29, 30.06],
    host: {host_tower_id: 'TT-023', host_tower_name: '桃花岛北塔'},
    impact: {target_progress: [{target_id: 'R1|V1|S:rid_cooperative', before_units: 1, after_units: 2, unit_gain: 1}]},
  }, {
    action_id: 'tower_colocation_host:TT-023:S-RID-2',
    service_key: 'S:rid_cooperative', subsystem: 'S', device_id: 'S-RID-2',
    distinct_site_id: 'tower:TT-023', required_units: 2, current_units: 1,
    surface_class: 'land', coordinate: [122.29, 30.06],
    host: {host_tower_id: 'TT-023', host_tower_name: '桃花岛北塔'},
    impact: {target_progress: [{target_id: 'R1|V1|S:rid_cooperative', before_units: 1, after_units: 1, unit_gain: 0}]},
  }],
  residual_confirmed_targets: [{
    target_id: 'R1|V2|S:rid_cooperative', service_key: 'S:rid_cooperative',
    subsystem: 'S', surface_class: 'coastal_uncertain',
    required_units: 2, current_units: 1, final_status: 'confirmed_gap',
  }],
};

test('P16 target service_key=S:rid_cooperative renders RID in the UI', () => {
  const cards = facilityPlanCards(SITE_PLAN);
  assert.match(cards, /RID 合作监视缺口/);
  assert.doesNotMatch(cards, /<b>S 缺口/);
  assert.match(cards, /data-service-key="S:rid_cooperative"/);
  // 目标服务明确，而不是只显示 C / S
  assert.match(cards, /服务 RID 合作监视/);
  // 内部 action_id 只出现在审计行
  assert.match(cards, /审计标识：tower_colocation_host:TT-023:S-RID-1/);
});

test('P16 shows 1 / 2 distinct physical sites as an under-redundancy finding', () => {
  const cards = facilityPlanCards(SITE_PLAN);
  assert.match(cards, /1 \/ 2 个不同物理站址/);
  // 状态行只消费后端给出的 service status；本 fixture 的 action 没带 status ⇒ 证据不足，
  // 前端绝不因为 1 < 2 就自己写"冗余不足"
  assert.match(cards, /状态：证据不足/);
  assert.doesNotMatch(cards, /冗余不足/);
  // 同址两条动作时**不**把 after_units 当成"新增后重数"（见下一个用例）
  assert.doesNotMatch(cards, /新增后：2 \/ 2 个不同物理站址/);
  // 单个独占物理站址的动作仍然显示后端给出的"新增后"单位数
  const single = facilityPlanCards({
    status: 'proposal_ready',
    selected_actions: [SITE_PLAN.selected_actions[0]],
    residual_confirmed_targets: [],
  });
  assert.match(single, /新增后：2 \/ 2 个不同物理站址/);
  assert.doesNotMatch(single, /同一物理站址/);
});

test('P16 service status is transcribed from the backend field only', () => {
  const withStatus = facilityPlanCards({
    status: 'proposal_ready',
    selected_actions: [{...SITE_PLAN.selected_actions[0], status: 'confirmed_deficit'}],
    residual_confirmed_targets: [],
  });
  assert.match(withStatus, /状态：冗余不足/);
  // 反向：后端说 satisfied（即使计数 1 / 2）也照后端显示
  const satisfied = facilityPlanCards({
    status: 'proposal_ready',
    selected_actions: [{...SITE_PLAN.selected_actions[0], status: 'satisfied'}],
    residual_confirmed_targets: [],
  });
  assert.match(satisfied, /状态：满足/);
  assert.doesNotMatch(satisfied, /状态：冗余不足/);
});

test('co-located actions are flagged as the same physical site and never as extra multiplicity', () => {
  const cards = facilityPlanCards(SITE_PLAN);
  assert.match(cards, /同一物理站址，不增加独立站址重数/);
  assert.doesNotMatch(cards, /两重|2 重|双重/);
  assert.equal(sameDistinctSite('tower:TT-023', 'tower:TT-023'), true);
  assert.equal(sameDistinctSite('tower:TT-023', 'tower:TT-024'), false);
  assert.equal(sameDistinctSite('', 'tower:TT-023'), false, '缺少身份时绝不判定为同址');
});

test('two devices on one distinct site keep the backend count instead of self-computed multiplicity', () => {
  const twoDevicesOneSite = {
    status: 'proposal_ready',
    selected_actions: [
      {...SITE_PLAN.selected_actions[0]},
      {...SITE_PLAN.selected_actions[0], action_id: 'tower_colocation_host:TT-023:S-RID-2', device_id: 'S-RID-2'},
    ],
    residual_confirmed_targets: [],
  };
  const cards = facilityPlanCards(twoDevicesOneSite);
  // 后端给出的 current_units 仍然是 1：前端绝不因为"看到两条动作"就写成 2
  assert.match(cards, /1 \/ 2 个不同物理站址/);
  assert.doesNotMatch(cards, /2 \/ 2 个不同物理站址/);
  assert.doesNotMatch(cards, /两重|2 重/);
});

test('distinct site counting semantics are explicit', () => {
  assert.equal(distinctSiteCountText(1, 2), '1 / 2 个不同物理站址');
  assert.equal(distinctSiteCountText(1, 1), '1 / 1 个不同物理站址');
  assert.equal(distinctSiteCountText(1, null), '不同物理站址数：证据不足');
  assert.equal(distinctSiteCountText(null, 2), '不同物理站址数：证据不足');
  // 计数文本层不含任何"满足 / 冗余不足"判定
  const counting = stripComments(readFileSync(
    new URL('../cns_planner/web/js/map/service_semantics.js', import.meta.url), 'utf8'));
  assert.doesNotMatch(counting, /redundancyVerdict/,
    'service_semantics 不得再提供"计数比较 → 状态"的入口');
});

// ---- 8b. 结构护栏：前端不得自行生成正式 service status ----------------------

test('the evidence/presentation module never derives a service status from counts', () => {
  const raw = readFileSync(
    new URL('../cns_planner/web/js/workflow/cns_service_evidence.js', import.meta.url), 'utf8');
  // 只看可执行代码：注释与文档里允许出现"绝不用 actual >= required"这种说明文字
  const code = stripComments(raw);
  assert.doesNotMatch(code, /redundancyVerdict/);
  // 逐 surface 行只拼计数，不含任何状态词或状态分支
  const surfaceRows = code.slice(
    code.indexOf('function surfaceCountRows'),
    code.indexOf('function surfaceStatusRows'));
  assert.ok(surfaceRows.length > 0, 'surfaceCountRows 必须存在');
  assert.doesNotMatch(surfaceRows, /status|Status|满足|冗余|confirmed_deficit/);
  // 状态唯一入口是后端给出的 status / status_by_surface / action.status
  assert.match(code, /entry\?\.status/);
  assert.match(code, /entry\?\.status_by_surface/);
  assert.match(code, /action\?\.status/);
  // 真正的等价逻辑：任何"比较计数 + 生成状态文本"的函数都不允许存在
  for (const body of functionBodies(code)) {
    const comparesCounts = /(?:distinct_site_count|current_units|required_units|required_distinct_site_count)[^;]{0,80}(?:>=|<=|>\s*|<)|(?:>=|<=)[^;]{0,80}(?:distinct_site_count|current_units|required_units)/.test(body);
    if (!comparesCounts) continue;
    assert.doesNotMatch(body, /满足|冗余不足|已确认缺口|confirmed_deficit|satisfied/,
      `含"计数比较"的函数不得同时生成 service 状态文本：${body.slice(0, 90)}`);
  }
});

test('the map overlay module does not derive service status either', () => {
  const code = stripComments(readFileSync(
    new URL('../cns_planner/web/js/map/cns_service_overlay.js', import.meta.url), 'utf8'));
  assert.doesNotMatch(code, /redundancyVerdict/);
  // 缺口着色只映射后端状态枚举
  assert.match(code, /REDUNDANCY_STATUS_MAP_STATE\[/);
  assert.match(code, /COMBINED_STATUS_MAP_STATE\[/);
  // 唯一的 >= 只允许是"最不利状态排序"（key 比较），不得与站址计数比较混用
  assert.doesNotMatch(code, /distinct_site_count[^;]{0,60}>=/);
  assert.doesNotMatch(code, /required_\w*count[^;]{0,60}>=/);
  // 状态词只允许来自集中映射表，不允许在绘制函数里比较计数后拼状态文本
  const draw = code.slice(
    code.indexOf('export function drawCnsServiceOverlay'),
    code.indexOf('function radiusForSite'));
  assert.ok(draw.length > 0, 'drawCnsServiceOverlay 必须存在');
  assert.doesNotMatch(draw, /满足|冗余不足|已确认缺口|confirmed_deficit|satisfied/);
});

// ---- 9. unknown surface -----------------------------------------------------

test('unknown surface is reported as insufficient evidence, never as sea', () => {
  assert.equal(SURFACE_CLASS_TEXT.unknown, '证据不足');
  assert.equal(SURFACE_CLASS_TEXT.sea, '海上');
  const plan = {
    status: 'proposal_ready',
    selected_actions: [{
      ...SITE_PLAN.selected_actions[0], surface_class: 'unknown',
      required_units: 2, current_units: 0,
    }],
    residual_confirmed_targets: [],
  };
  const cards = facilityPlanCards(plan);
  assert.match(cards, /证据不足/);
  assert.doesNotMatch(cards, /surface_class：海上/);

  const gap = serviceFlow({cns_corridor_gap_assessment: {status: 'passed', routes: [{
    route_id: 'R1', status: 'pending_confirmation',
    subsystems: [{subsystem: 'S', status: 'pending_confirmation', combined_status: 'unknown',
      service_redundancy: [{
        service_key: 'S:rid_cooperative', surface_dependent: true, voxel_count: 3,
        status: 'unknown', status_counts: {satisfied: 0, confirmed_deficit: 0, unknown: 3},
        required_distinct_site_count_by_surface: {}, distinct_site_count_by_surface: {},
        surface_class_counts: {unknown: 3},
      }]}],
  }]}});
  const statements = serviceGapStatements(gap);
  assert.match(statements, /证据不足/);
  assert.doesNotMatch(statements, /海上/);
});

test('surface facts panel shows the representative-point basis and keeps unknown explicit', () => {
  const panel = renderSurfaceFactsPanel(baseFlow());
  assert.ok(panel.includes(SURFACE_FACTS_BASIS_NOTE));
  assert.match(panel, /unknown（证据不足）保持未知，<b>绝不按海面处理<\/b>/);
  assert.match(panel, /未配置/);
  assert.match(panel, /不需要先运行 Radar/);
});

test('surface facts panel reports stale facts after a policy change', () => {
  const model = surfaceFactsModel(baseFlow({
    surface_class_facts: {status: 'stale', surface_class_counts: {unknown: 10}, grid_cell_count: 10},
    result_statuses: {surface_class_facts: 'stale'},
  }));
  assert.equal(model.stale, true);
  const panel = renderSurfaceFactsPanel(baseFlow({
    surface_class_facts: {status: 'stale', surface_class_counts: {unknown: 10}, grid_cell_count: 10},
    result_statuses: {surface_class_facts: 'stale'},
  }));
  assert.match(panel, /需要重新生成/);
  assert.match(panel, /需要重新生成陆海分类事实/);
});

test('surface facts counts come straight from the backend container', () => {
  const facts = {
    status: 'passed',
    grid_cell_count: 100, classified_grid_cell_count: 97,
    surface_class_counts: {land: 60, sea: 30, coastal_uncertain: 7, unknown: 3},
    land_mask: {configured_path: 'D:/x/land.gpkg', layer_name: 'zhoushan_land',
      source_identity: {identity_basis: 'content_sha256', sha256: 'a'.repeat(64)}},
    input_fingerprint: 'f'.repeat(64),
  };
  const model = surfaceFactsModel(baseFlow({surface_class_facts: facts}));
  assert.deepEqual(model.counts, {land: 60, sea: 30, coastal_uncertain: 7, unknown: 3});
  assert.equal(model.landMaskConfigured, true);
  const panel = renderSurfaceFactsPanel(baseFlow({surface_class_facts: facts}));
  assert.match(panel, /陆地 land/);
  assert.match(panel, /<small>60<\/small>/);
  assert.match(panel, /<small>30<\/small>/);
  assert.match(panel, /<small>7<\/small>/);
  assert.match(panel, /<small>3<\/small>/);
  assert.match(panel, /97 \/ 100/);
  assert.match(panel, /证据不足 unknown/);
  assert.match(panel, /已验证内容指纹/);
  // 完整 SHA256 不进主界面（只在高级 disclosure 内）
  const mainBody = panel.split('<details')[0];
  assert.doesNotMatch(mainBody, /a{64}/);
  assert.match(panel, /高级：来源身份与指纹完整值/);
});

// ---- 10. 图层默认关闭 -------------------------------------------------------

test('the five new CNS service map layers ship unchecked', () => {
  const boxes = checkboxAttributes(html);
  for (const id of ROUND3_LAYER_IDS) {
    assert.ok(boxes.has(id), `图层抽屉缺少开关 ${id}`);
    assert.doesNotMatch(boxes.get(id), /\bchecked\b/, `${id} 必须默认关闭`);
  }
  // main.js 的 LAYER_IDS 必须逐项包含它们，否则开关读不到、图层永远不画
  for (const id of ROUND3_LAYER_IDS) {
    assert.match(main, new RegExp("'" + id + "'"), `main.js 的 LAYER_IDS 缺少 ${id}`);
  }
});

test('the CNS service legend stays hidden until a service layer is opened', () => {
  assert.match(html, /<div id="cnsServiceLegend" hidden><\/div>/);
  const overlayDraw = displayLayers.slice(displayLayers.indexOf('const cnsServiceOn='));
  assert.match(overlayDraw, /layers\.cnsCommunicationLayer===true\|\|layers\.cnsRidLayer===true/);
  assert.match(overlayDraw, /layers\.cnsServiceGapLayer===true\|\|layers\.cnsFacilityPlanLayer===true/);
});

test('reopening a project resets the five temporary service layers to off', () => {
  // 复位函数必须覆盖全部 5 个图层，并且只取消勾选（不写 .checked = true）
  const block = main.slice(
    main.indexOf('const PROJECT_REOPEN_RESET_LAYER_IDS='),
    main.indexOf('async function openProject('));
  assert.ok(block.length > 0, 'main.js 必须保留复位常量与函数');
  for (const id of ROUND3_LAYER_IDS) {
    assert.match(block, new RegExp("'" + id + "'"), `复位列表缺少 ${id}`);
  }
  assert.match(block, /removeAttribute\('checked'\)/);
  // 两条打开路径（同项目轻量路径 + 常规路径）都必须复位后再渲染
  const openBody = main.slice(main.indexOf('async function openProject('),
    main.indexOf('function getTiandituKey('));
  const resets = [...openBody.matchAll(/resetAnalysisLayerSelection\(\);\s*\n\s*renderWorkflow\(\);/g)];
  assert.equal(resets.length, 2, '同项目路径与常规路径都必须在 renderWorkflow 前复位图层');
  // 不得因此改动在线底图 / 其它图层的默认行为
  assert.doesNotMatch(block, /'online'/);
  assert.doesNotMatch(openBody, /\.checked\s*=/);
  assert.doesNotMatch(main.slice(main.indexOf('function update(data){'),
    main.indexOf('// ---- 启动装配')), /cnsCommunicationLayer|cnsRidLayer|surfaceFactsLayer/);
});

test('the overlay is a separate module and the radar overlay is untouched by it', () => {
  // 独立模块：display_layers 必须从 cns_service_overlay.js 引入，而不是塞进 radar overlay
  assert.match(displayLayers, /from '\.\/cns_service_overlay\.js'/);
  const radarOverlay = readFileSync(
    new URL('../cns_planner/web/js/map/radar_layout_overlay.js', import.meta.url), 'utf8');
  assert.doesNotMatch(radarOverlay, /cns_service|RID|rid_cooperative/);
  // RID 的站点与扇区绘制函数互不共用
  const overlay = readFileSync(
    new URL('../cns_planner/web/js/map/cns_service_overlay.js', import.meta.url), 'utf8');
  assert.doesNotMatch(overlay, /sectorPath|half_width_deg|azimuth_deg/);
});

// ---- 11. 缺口地图语义 -------------------------------------------------------

test('gap segments are coloured from canonical P15 evidence only', () => {
  const flow = serviceFlow({
    operational_routes: [{
      route_id: 'R-TAOHUA-HANJING', status: 'passed', length_m: 2000,
      path: [[122.2, 30.0], [122.21, 30.0]],
    }],
    cns_corridor_gap_assessment: CORRIDOR_GAP,
  });
  const segments = gapSegmentGeometry(flow, null);
  assert.ok(segments.length >= 1);
  const segment = segments.find(item => item.subsystem === 'RID' || item.subsystem === 'S');
  assert.ok(segment, '必须由 P15 缺口段生成至少一条几何');
  assert.equal(segment.service_key, 'S:rid_cooperative');
  assert.equal(segment.state, 'under_redundant');
  assert.deepEqual(segment.from, [122.2, 30.0]);
  // to 端点是后端给的比例（0..1）在正式航路上的等距采样：只校验它落在航路内部且同向
  assert.ok(segment.to[0] > 122.2 && segment.to[0] < 122.21, '缺口终点必须落在航路内部');
  assert.equal(segment.to[1], 30.0);
  assert.equal(COMBINED_STATUS_MAP_STATE.confirmed_gap, 'uncovered');
  // 状态转换表只映射后端枚举，不重算任何距离 / 重数
  assert.equal(stateForSegment(flow, {service_redundancy: [{service_key: 'S:rid_cooperative', status: 'confirmed_deficit'}]}, {status: 'confirmed_deficit'}), 'under_redundant');
  assert.equal(stateForSegment(flow, {service_redundancy: [], combined_status: 'unknown'}, null), 'unknown');
});

test('gap geometry refuses to draw without a passed operational route', () => {
  const flow = serviceFlow({
    operational_routes: [{route_id: 'R1', status: 'pending_confirmation', path: [[122.2, 30.0]]}],
    cns_corridor_gap_assessment: CORRIDOR_GAP,
  });
  assert.deepEqual(gapSegmentGeometry(flow, null), []);
});

test('route offset interpolation only samples the backend path', () => {
  const path = [[122.2, 30.0], [122.22, 30.0]];
  assert.deepEqual(coordinateAtFraction(path, 0), [122.2, 30.0]);
  assert.deepEqual(coordinateAtFraction(path, 1), [122.22, 30.0]);
  const middle = coordinateAtFraction(path, 0.5);
  assert.ok(Math.abs(middle[0] - 122.21) < 1e-9);
  assert.equal(middle[1], 30.0);
});

test('the overlay only draws meaningful sites and never floods candidate circles', () => {
  const flow = serviceFlow({
    cns_corridor_site_plan: SITE_PLAN,
    candidate_sites: {status: 'passed', count: 300,
      items: Array.from({length: 300}, (unused, index) => ({
        site_id: 'CAND-' + index, coordinate: [122.2 + index / 1000, 30.0],
      }))},
    tower_colocation_candidates: {status: 'passed', count: 373,
      items: Array.from({length: 373}, (unused, index) => ({
        site_id: 'TOWER-CAND-' + index, coordinate: [122.3 + index / 1000, 30.1],
      }))},
  });
  const model = cnsServiceOverlayModel(flow);
  // 只画 P16 选中的动作：300 个候选站址与 373 个共塔候选一个都不进模型
  assert.equal(model.sites.length, SITE_PLAN.selected_actions.length,
    '只画 P16 选中动作，绝不铺开候选站址');
  assert.ok(model.sites.every(site => site.kind === 'planned'));
  assert.equal(model.existingCount, 0);
  assert.equal(model.residualSites.length, 0, 'P16 残余目标没有后端坐标，绝不伪造点位');
  const serialized = JSON.stringify(model);
  assert.doesNotMatch(serialized, /CAND-/, '候选站址绝不进入 overlay 模型');
  assert.doesNotMatch(serialized, /TOWER-CAND-/, '共塔候选绝不进入 overlay 模型');
  // P16 完全没有 selected action 时，模型里没有任何"规划覆盖"站点
  const empty = cnsServiceOverlayModel(serviceFlow({
    candidate_sites: {status: 'passed', count: 300, items: [{site_id: 'C1', coordinate: [122.2, 30.0]}]},
  }));
  assert.equal(empty.sites.length, 0);
  assert.equal(empty.plannedCount, 0);
});

test('overlay drawing is bounded and honours the layer switches', () => {
  const calls = [];
  const ctx = {
    save() {}, restore() {}, beginPath() {}, arc() {}, fill() {}, stroke() {},
    setLineDash() {}, moveTo() {}, lineTo() {}, fillRect() {},
    set fillStyle(value) { calls.push('fillStyle'); },
    set strokeStyle(value) { calls.push('strokeStyle'); },
    set globalAlpha(value) {}, set lineWidth(value) {}, set lineCap(value) {},
  };
  const flow = serviceFlow({
    existing_cns_facilities: {status: 'passed', count: 1, items: [{
      facility_id: 'F1', name: 'Comm 站', coordinate: [122.2, 30.0],
      devices: [{device_id: 'C-COMM-1', subsystem: 'C', status: 'active',
        service_key: 'C:communication',
        coverage_geometry: {radius_by_surface: {land: 4000, sea: 4000}}}],
    }]},
  });
  const model = cnsServiceOverlayModel(flow);
  const view = {res: 20};
  const screenPoint = coordinate => [coordinate[0] * 1000, coordinate[1] * 1000];
  // 所有开关关闭：不应绘制任何站点
  const off = drawCnsServiceOverlay({ctx, view, screenPoint, model, layers: {}});
  assert.equal(off.sites, 0);
  assert.equal(off.circles, 0);
  // 打开 Communication：只画 Communication 的圆
  const on = drawCnsServiceOverlay({ctx, view, screenPoint, model,
    layers: {cnsCommunicationLayer: true}});
  assert.equal(on.sites, 1);
  assert.equal(on.circles, 1);
  assert.ok(calls.length > 0);
});

test('radius helpers read the backend mapping verbatim', () => {
  assert.equal(radiusBySurfaceOf({land: 4000, sea: 5000}, 'sea'), 5000);
  assert.equal(radiusBySurfaceOf({land: 4000}, 'sea'), null);
  assert.equal(radiusBySurfaceOf(null, 'land'), null);
  assert.equal(radiusBySurfaceOf({land: 0}, 'land'), null);
  // 米 → 屏幕像素：与 radar overlay 同一投影口径（赤道 1:1，60°N 减半）
  assert.equal(Math.round(radiusPixel(4000, {res: 4}, 0)), 1000);
  assert.equal(Math.round(radiusPixel(4000, {res: 4}, 60)), 2000);
});

// ---- 12. legacy C/N/S 不回归 ------------------------------------------------

test('legacy C/N/S devices keep their editable radius contract', () => {
  const panel = renderStep5({flow: baseFlow({
    devices: LEGACY_DEVICES,
    device_catalog: {status: 'passed', count: 3, items: LEGACY_DEVICES.map(item => ({
      device_id: item.device_id, subsystem: item.subsystem,
      coverage_geometry: {model: 'hemisphere', slant_range_m: item.radius_m},
    }))},
  })});
  // 旧 fixture：既有 data-device-radius 索引契约与可编辑输入框原样保留
  assert.match(panel, /data-device-radius="0"/);
  assert.match(panel, /data-device-radius="2"/);
  assert.match(panel, /data-device-mtbf="0"/);
  assert.match(panel, /<b class="device-name">C · 通信主站示例<\/b>/);
  assert.match(panel, /<label class="device-field device-field-radius">R\(m\)/);
  assert.doesNotMatch(panel, /device-field device-field-radius device-field-wide/,
    'legacy 设备不得被标成 surface-aware 宽字段');
  assert.doesNotMatch(panel, /该设备属于 surface-aware service/);
  // legacy 子系统继续按 C / N / S 显示（RID 只对显式声明 service 的设备出现）
  assert.match(panel, /<b class="device-name">N · 导航主站示例<\/b>/);
  assert.match(panel, /<b class="device-name">S · 监视主站示例<\/b>/);
  assert.doesNotMatch(panel, /RID 合作监视 工程规划基线[^]*?data-service-key="S:rid_cooperative"><b>RID 合作监视 工程规划基线<\/b>[^]*?2\.0 km/,
    'legacy S 设备绝不被当成 RID');
});

test('legacy fixtures never gain a fabricated service profile', () => {
  const panel = renderServiceProfiles(baseFlow({
    device_catalog: {status: 'passed', items: LEGACY_DEVICES.map(item => ({
      device_id: item.device_id, subsystem: item.subsystem,
      coverage_geometry: {model: 'hemisphere', slant_range_m: item.radius_m},
    }))},
  }));
  assert.equal(isSurfaceAwareDevice({coverage_geometry: {model: 'hemisphere', slant_range_m: 5000}}), false);
  assert.match(panel, /未配置/);
  assert.doesNotMatch(panel, /4\.0 km/);
});

// ---- 12b. surface-aware service 的 legacy radius_m 只读 ----------------------

test('surface-aware services keep the legacy radius input read-only', () => {
  const panel = renderStep5({flow: serviceFlow({
    devices: [
      {subsystem: 'C', name: '通信全向站', device_id: 'C-COMM-1', radius_m: 5000, mtbf_h: 20000, role: 'primary'},
      {subsystem: 'S', name: 'RID 接收站', device_id: 'S-RID-1', radius_m: 4500, mtbf_h: 18000, role: 'primary'},
    ],
  })});
  // DOM / 索引契约保留
  assert.match(panel, /data-device-radius="0"/);
  assert.match(panel, /data-device-radius="1"/);
  assert.match(panel, /data-device-mtbf="0"/);
  assert.match(panel, /data-device-mtbf="1"/);
  // 两个 surface-aware 设备的 R(m) 都不可编辑
  const commInput = panel.match(/<input type="number" data-device-radius="0"[^>]*>/)[0];
  const ridInput = panel.match(/<input type="number" data-device-radius="1"[^>]*>/)[0];
  assert.match(commInput, /\bdisabled\b/, 'Communication 的 legacy radius 必须只读/禁用');
  assert.match(ridInput, /\bdisabled\b/, 'RID 的 legacy radius 必须只读/禁用');
  // 原值仍然原样回显（只是不可编辑）
  assert.match(commInput, /value="5000"/);
  assert.match(ridInput, /value="4500"/);
  // 只读说明逐字出现
  assert.match(panel, /兼容字段，只读；正式规划使用按地表类型划分的几何规划半径。/);
  assert.equal((panel.match(/data-surface-aware="true"/g) || []).length, 2);
});

test('legacy devices stay editable while the declared surface-aware subsystem is locked', () => {
  // serviceFlow() 的目录里只有 C:communication 与 S:rid_cooperative
  const panel = renderStep5({flow: serviceFlow({
    devices: [
      {subsystem: 'C', name: '通信主站', device_id: 'C-1', radius_m: 5000, mtbf_h: 20000},
      {subsystem: 'S', name: '监视主站', device_id: 'S-1', radius_m: 4500, mtbf_h: 18000},
      {subsystem: 'N', name: '导航主站', device_id: 'N-1', radius_m: 4000, mtbf_h: 22000},
    ],
  })});
  const input = index => panel.match(
    new RegExp('<input type="number" data-device-radius="' + index + '"[^>]*>'))[0];
  assert.match(input(0), /\bdisabled\b/, 'C:communication 声明后，C 设备行必须只读');
  assert.match(input(1), /\bdisabled\b/, 'S:rid_cooperative 声明后，S 设备行必须只读');
  assert.doesNotMatch(input(2), /\bdisabled\b/, 'N 设备不属 surface-aware service，仍可编辑');
  // 只读说明只出现在 surface-aware 行
  assert.equal((panel.match(/data-surface-aware="true"/g) || []).length, 2);
});

test('without a declared canonical service the device rows stay editable', () => {
  // 默认 demo 目录没有服务条目 ⇒ 没有后端权威半径，保持可编辑（与改动前一致）
  const panel = renderStep5({flow: baseFlow({
    devices: [
      {subsystem: 'C', name: '通信主站示例', device_id: 'C-PRIMARY-DEMO', radius_m: 5000, mtbf_h: 20000},
      {subsystem: 'S', name: '监视主站示例', device_id: 'S-PRIMARY-DEMO', radius_m: 4500, mtbf_h: 18000},
    ],
    device_catalog: {status: 'passed', items: [
      {device_id: 'C-PRIMARY-DEMO', subsystem: 'C', coverage_geometry: {model: 'hemisphere', slant_range_m: 5000}},
      {device_id: 'S-PRIMARY-DEMO', subsystem: 'S', coverage_geometry: {model: 'hemisphere', slant_range_m: 4500}},
    ]},
  })});
  for (const index of [0, 1]) {
    const input = panel.match(
      new RegExp('<input type="number" data-device-radius="' + index + '"[^>]*>'))[0];
    assert.doesNotMatch(input, /\bdisabled\b/, `无 canonical service 时设备 ${index} 必须仍可编辑`);
  }
  assert.doesNotMatch(panel, /data-surface-aware="true"/);
  assert.doesNotMatch(panel, /兼容字段，只读/);
});

test('collect never writes back the read-only legacy radius', async () => {
  // 用真实的 bind()/collect 路径验证：即使只读控件被篡改，也不回写 canonical radius_m。
  const attributes = new Map([
    ['0', {disabled: true, value: '9999', mtbf: '21000'}],
    ['1', {disabled: false, value: '7777', mtbf: '22000'}],
  ]);
  const makeNode = (initial = {}) => ({
    onclick: null, onchange: null, value: '', checked: false, disabled: false, readOnly: false,
    innerHTML: '', textContent: '', title: '', dataset: {},
    ...initial,
    getContext: () => ({clearRect() {}, beginPath() {}, arc() {}, fill() {}, stroke() {}}),
  });
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  // 控件按真实 panel 的 id 提供：bind() 会查询它们，缺一个就会抛错
  for (const id of [
    'saveDevices', 'browseExisting', 'existing_cnsPath', 'browseCandidates', 'candidate_sitesPath',
    'saveSurfaceClassificationPolicy', 'evaluateSurfaceClassFacts', 'saveExistingBaseline',
    'savePlanningObjectives', 'evaluateCorridorGap', 'evaluateCorridorSitePlan',
    'evaluateServiceTimeline', 'evaluateProtectionEnvelope', 'evaluateClosedLoop',
    'applyClosedLoop', 'nextStep',
  ]) node(id);
  for (const id of [
    'existingKnowledgeStatus', 'existingPlanningMode', 'existingBaselineSource',
    'confirmExistingBaseline', 'surfaceLandMaskLayer', 'surfaceCoastalBuffer',
    'surfacePolicySource', 'surfacePolicyConfirmed', 'coverage3dSpacing',
    'planningObjectiveRoute', 'planningObjectiveSubsystem', 'planningObjectiveSource',
    'planningObjectiveConfirmed', 'corridorSitePolicyConfirmed',
  ]) node(id, {value: ''});

  const original = globalThis.document;
  globalThis.document = {
    querySelector: selector => {
      const radius = /\[data-device-radius="(\d+)"\]/.exec(selector);
      if (radius) {
        const current = attributes.get(radius[1]);
        return {get disabled() { return current.disabled; }, get readOnly() { return false; },
          get value() { return current.value; }};
      }
      const mtbf = /\[data-device-mtbf="(\d+)"\]/.exec(selector);
      if (mtbf) {
        const current = attributes.get(mtbf[1]);
        return {get value() { return current.mtbf; }};
      }
      return null;
    },
  };
  let flow = {devices: [
    {subsystem: 'C', device_id: 'C-COMM-1', radius_m: 5000, mtbf_h: 20000},
    {subsystem: 'C', device_id: 'C-LEGACY', radius_m: 5000, mtbf_h: 20000},
  ]};
  let saved = null;
  const handlers = {};
  const bindings = {
    $: id => nodes.get(id) || null,
    flow: () => flow,
    setFlow: value => { flow = value; },
    afterFlowChange: () => {},
    resourceAction: async () => null,
    resourceMutationAndRefresh: async () => null,
    computeAction: async () => null,
    panelError: () => {},
    setStep: () => {},
    openBrowser: () => {},
    searchPlace: () => {},
    paint: () => {},
    actionButton: (id, handler) => { handlers[id] = handler; },
    mutate: async (action, payload) => { saved = {action, payload}; },
  };
  try {
    bindStep5(bindings);
    await handlers.saveDevices();
  } finally {
    globalThis.document = original;
  }
  assert.equal(saved.action, 'devices');
  // 只读控件被篡改成 9999：canonical radius_m 保持 5000（MTBF 仍按输入提交）
  assert.equal(saved.payload.devices[0].radius_m, 5000);
  assert.equal(saved.payload.devices[0].mtbf, 21000);
  // legacy 设备照旧提交用户输入
  assert.equal(saved.payload.devices[1].radius_m, 7777);
  assert.equal(saved.payload.devices[1].mtbf, 22000);
});

test('step05 still mounts the canonical chain and the radar task card', () => {
  const panel = renderStep5({flow: serviceFlow({
    devices: LEGACY_DEVICES,
    cns_corridor_gap_assessment: CORRIDOR_GAP,
    cns_corridor_site_plan: SITE_PLAN,
  })});
  //: Round32-H：Radar 基线位于服务走廊之前（真实依赖 Radar → P14 服务证据）。
  assert.match(panel, /链条：三维覆盖 → 服务能力 → 雷达监视基线 → 服务走廊 → 能力缺口 → 设施规划/);
  assert.match(panel, /陆海分类事实（Surface Facts）/);
  assert.match(panel, /Communication \/ RID 工程规划 Profile/);
  assert.match(panel, /service 级冗余结论（Communication \/ RID）/);
  assert.match(panel, /service-aware 规划方案/);
  assert.match(panel, /监视雷达初步划设/);
  assert.match(panel, /id="saveSurfaceClassificationPolicy"/);
  assert.match(panel, /id="evaluateSurfaceClassFacts"/);
});

test('step05 contract endpoints stay the canonical Round 2 ones', () => {
  const step05 = readFileSync(
    new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url), 'utf8');
  const evidence = readFileSync(
    new URL('../cns_planner/web/js/workflow/cns_service_evidence.js', import.meta.url), 'utf8');
  assert.match(evidence, /SURFACE_CLASSIFICATION_POLICY_ENDPOINT = '\/api\/surface-classification-policy'/);
  assert.match(evidence, /SURFACE_CLASS_FACTS_EVALUATE_ENDPOINT = '\/api\/surface-class-facts\/evaluate'/);
  assert.match(step05, /resourceMutationAndRefresh\(SURFACE_CLASSIFICATION_POLICY_ENDPOINT/);
  assert.match(step05, /resourceAction\(SURFACE_CLASS_FACTS_EVALUATE_ENDPOINT/);
  // 逐体元 service 证据按需读取，绝不塞进通用快照
  assert.match(step05, /refreshCorridorDetail/);
});

// ---- 13. P14 逐体元 service 证据按需读取（不破坏 d34eadb 的懒加载优化） --------

test('P14 service detail is fetched only on demand and lands on a read-only detail field', async () => {
  const requested = [];
  let flow = {
    project: {name: 'P'},
    cns_corridor_assessment: {
      status: 'passed', voxels_count: 12, voxels_detail: 'artifact', detail_available: true,
      detail_endpoint: '/api/cns-service-corridor',
    },
  };
  let serial = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: async () => { throw new Error('不得被调用'); },
    fetchAttributes: async () => { throw new Error('不得被调用'); },
    fetchCorridorDetail: async () => {
      requested.push('cns-service-corridor');
      return {status: 'passed', routes: [{route_id: 'R1', voxels: []}]};
    },
    afterApply: () => {},
  });
  // 安装 slim 快照本身**不**触发走廊明细请求（否则打开项目就会下载逐体元明细）
  await applier.applyWorkflowSnapshot(flow, {hydrate: false});
  assert.deepEqual(requested, []);
  const result = await applier.hydrateCorridorServiceDetail();
  assert.deepEqual(requested, ['cns-service-corridor']);
  assert.equal(result.applied, true);
  assert.ok(flow.cns_corridor_assessment.detail.routes.length === 1);
  // 原有 slim 摘要字段原样保留（只增不改）
  assert.equal(flow.cns_corridor_assessment.voxels_count, 12);
});

test('P14 service detail is skipped when the snapshot does not externalize it', async () => {
  let flow = {project: {name: 'P'}, cns_corridor_assessment: {status: 'not_calculated'}};
  let serial = 0;
  const applier = createWorkflowSnapshotApplier({
    getFlow: () => flow,
    setFlow: value => { flow = value; },
    nextSerial: () => ++serial,
    currentSerial: () => serial,
    fetchGrid: async () => ({cells: []}),
    fetchAttributes: async () => ({}),
    fetchCorridorDetail: async () => { throw new Error('不得被调用'); },
    afterApply: () => {},
  });
  const result = await applier.hydrateCorridorServiceDetail();
  assert.equal(result.applied, false);
  assert.equal(result.reason, 'no_corridor_detail');
});

test('step05 offers the P14 service detail entry only when the backend declares it', () => {
  const without = serviceCorridorEvidence(baseFlow({
    cns_corridor_assessment: {status: 'passed'},
  }));
  assert.doesNotMatch(without, /loadServiceCorridorDetail/);
  const withDetail = serviceCorridorEvidence(baseFlow({
    cns_corridor_assessment: {
      status: 'passed', detail_available: true,
      detail_endpoint: '/api/cns-service-corridor',
    },
  }));
  assert.match(withDetail, /id="loadServiceCorridorDetail"/);
  assert.match(withDetail, /已外置保存/);
});

// ---- 14. Step04 正式需求的 RID 保真（最小调整，不重构 Step04） ----------------

test('step04 renders cooperative-surveillance RID instead of a generic S', () => {
  const rendered = renderStep4({flow: {
    aircraft_profiles: {count: 0, items: []},
    selected_aircraft_profile_id: '',
    scenario_routes: [],
    device_catalog: {items: []},
    steps: {'4': false},
    required_cns: {status: 'passed', source: 'user_configuration', project_default: {
      communication: {required: true, service_key: 'C:communication',
        redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1}},
      surveillance: {
        required: true, service_key: 'S:rid_cooperative',
        service_subtype: 'cooperative_surveillance',
        type: {technology: 'network_remote_id', target_cooperation: 'cooperative'},
        redundancy_by_surface: {land: 2, coastal_uncertain: 2, sea: 1},
      },
    }},
  }});
  assert.match(rendered, /合作监视 RID/);
  assert.match(rendered, /RID 合作监视（S:rid_cooperative）/);
  assert.match(rendered, /technology=network_remote_id/);
  assert.match(rendered, /target_cooperation=cooperative/);
  assert.match(rendered, /要求的不同物理站址数：陆地 2 个不同物理站址/);
  assert.match(rendered, /data-service-identity="S:rid_cooperative"/);
});

test('step04 never infers an RID requirement that was not declared', () => {
  // 没有 service_key / service_subtype，只有旧式 radar technology
  const legacy = renderStep4({flow: {
    aircraft_profiles: {count: 0, items: []}, selected_aircraft_profile_id: '',
    scenario_routes: [], device_catalog: {items: []}, steps: {'4': false},
    required_cns: {status: 'passed', project_default: {
      surveillance: {required: true, type: {technology: 'radar'}},
    }},
  }});
  assert.doesNotMatch(legacy, /合作监视 RID/);
  assert.doesNotMatch(legacy, /RID 合作监视/);
  assert.doesNotMatch(legacy, /data-service-identity="S:rid_cooperative"/);
  // 只转印显式声明的 technology，不做任何推断
  assert.match(legacy, /technology=radar/);
});
