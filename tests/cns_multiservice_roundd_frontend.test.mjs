// =========================================================
// Round D 前端契约测试：CNS 多服务规划工作台
// （通信 / 导航增强（GNSS/RTK）/ 合作监视（RID）/ 非合作监视（Radar））
//
// 覆盖（编号与 Round D 验收清单一致）：
//   A 四个 service label 正确；
//   B legacy 项目无 services：UI 不自动创建 Radar/RTK 需求；
//   C RID + Radar required：显示 all_required 双通道监视（只读）；
//   D Radar / RID provider 数绝不相加；
//   E Navigation policy 未确认：显示待确认；
//   F Navigation baseline 缺失：不画 envelope；
//   G confirmed + baseline：画导航增强工程基线；
//   H 图例文字不得含 "RTK 无线覆盖范围" 一类红线措辞；
//   I Navigation action device_id=null：正常显示规划单元 / 设备型号未选择；
//   J dependency_only：显示"无需新增导航站，应补通信"；
//   K 普通 N tower 无 confirmed suitability：不显示为已建参考站；
//   L reference_station_installed：正确区分已建站 / 可规划候选；
//   M Radar 仍使用 directional layer，没有新增 circle renderer；
//   N 五/六类分析图层 reopen 后关闭；
//   O P15 四 service 分开展示；
//   P P16 四 service action 分开展示；
//   Q unknown surface 不被渲染成 sea；
//   R 前端源中不存在 RTK 默认距离常量；
//   S 前端源中不复制 Communication / RID 半径 authority；
//   T Raw enums 不作为主要业务文本。
//
// 与既有前端测试同一惯例：没有 jsdom，也不伪造浏览器；断言只读纯函数输出与源码结构。
// =========================================================
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {
  ALL_REQUIRED_MODE_LABEL, CNS_ROUNDD_SERVICE_KEYS, CNS_SERVICE_FORMAL_LABELS,
  CNS_SERVICE_GEOMETRY_LEGEND_NOTE, NAVIGATION_BASELINE_LEGEND_LABEL,
  RADAR_DIRECTIONAL_GEOMETRY_NOTE, SERVICE_KEY_LABELS, SURVEILLANCE_DUAL_CHANNEL_LABELS,
  navigationReferenceStationInstalled, navigationSuitabilityOf,
} from '../cns_planner/web/js/map/service_semantics.js';
import {
  NAVIGATION_SUITABILITY_DOM_IDS, dualChannelStatus, facilityPlanCards,
  navigationPlanningModel, navigationSuitabilitySites, renderCorridorServiceEvidence,
  renderNavigationPlanningPanel, renderNavigationSuitabilityPanel, renderRadarServicePanel,
  renderRequiredCnsServicesPanel, renderSurveillanceDualChannelPanel, rounddRequiredServicesModel,
  serviceGapStatements,
} from '../cns_planner/web/js/workflow/cns_service_evidence.js';
import {
  CNS_SERVICE_LAYER_IDS, cnsMapFeatureAt, cnsMapFeatureTooltip, cnsServiceLegendModel,
  cnsServiceOverlayModel, drawCnsNavigationEnvelope, navigationBaselineModel,
} from '../cns_planner/web/js/map/cns_service_overlay.js';
import {render as renderStep5} from '../cns_planner/web/js/workflow/step05_cns.js';

const html = readFileSync(new URL('../cns_planner/web/index.html', import.meta.url), 'utf8');
const main = readFileSync(new URL('../cns_planner/web/js/main.js', import.meta.url), 'utf8');
const displayLayers = readFileSync(
  new URL('../cns_planner/web/js/map/display_layers.js', import.meta.url), 'utf8');
const overlaySource = readFileSync(
  new URL('../cns_planner/web/js/map/cns_service_overlay.js', import.meta.url), 'utf8');
const evidenceSource = readFileSync(
  new URL('../cns_planner/web/js/workflow/cns_service_evidence.js', import.meta.url), 'utf8');
const semanticsSource = readFileSync(
  new URL('../cns_planner/web/js/map/service_semantics.js', import.meta.url), 'utf8');
const step05Source = readFileSync(
  new URL('../cns_planner/web/js/workflow/step05_cns.js', import.meta.url), 'utf8');
const radarOverlay = readFileSync(
  new URL('../cns_planner/web/js/map/radar_layout_overlay.js', import.meta.url), 'utf8');

// ---- helpers ----------------------------------------------------------------

/** 图层抽屉里 id → <input> 标签原文。 */
function checkboxAttributes(source) {
  const found = new Map();
  for (const match of source.matchAll(/<input\b[^>]*type="checkbox"[^>]*>/g)) {
    const id = /\bid="([^"]+)"/.exec(match[0]);
    if (id) found.set(id[1], match[0]);
  }
  return found;
}

const isChecked = attributes => /\bchecked\b/.test(attributes);

/**
 * 面板的**可见业务文本**：去掉高级 `<details>` 内容与全部标签/属性。
 *
 * raw enum 只允许出现在高级详情与 data-* 属性里，不允许出现在主界面文字上。
 */
function visibleText(source) {
  return String(source)
    .replace(/<details[\s\S]*?<\/details>/g, ' ')
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ');
}

/**
 * 去掉注释后的**可执行代码**。
 *
 * 结构护栏只应约束真正会跑的代码：文档注释里允许出现
 * "绝不复制 4000/2000/5000" 这类说明文字。
 */
function stripComments(source) {
  return String(source)
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** 记录型 canvas stub（与既有 radar overlay 测试同一做法）。 */
function recordingCtx() {  const calls = [];
  const ctx = {
    calls, save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {},
    closePath() {}, fill() {}, stroke() {}, arc() {}, rect() {}, fillRect() {},
    setLineDash(dash) { calls.push(['setLineDash', dash]); },
    set fillStyle(value) { calls.push(['fillStyle', value]); },
    set strokeStyle(value) { calls.push(['strokeStyle', value]); },
    set globalAlpha(value) { calls.push(['globalAlpha', value]); },
    set lineWidth(value) { calls.push(['lineWidth', value]); },
    set lineCap(value) { calls.push(['lineCap', value]); },
  };
  return ctx;
}

// ---- fixtures ---------------------------------------------------------------

const RTK_PLANNING_CONFIRMED = {
  model: 'reference_station_baseline',
  max_reference_baseline_m: 15000,
  required_distinct_site_count: 2,
  delivery_service_key: 'C:communication',
  confirmed: true,
  source: 'user_configuration',
  maturity: 'engineering_assumption',
  planning_readiness: 'ready',
  missing_evidence: false,
};

const RTK_PLANNING_UNCONFIRMED = {
  ...RTK_PLANNING_CONFIRMED,
  confirmed: false,
  planning_readiness: 'pending_confirmation',
  missing_evidence: true,
};

const RTK_PLANNING_NO_BASELINE = {
  ...RTK_PLANNING_CONFIRMED,
  max_reference_baseline_m: null,
  planning_readiness: 'pending_confirmation',
  missing_evidence: true,
};

function serviceRequirement(serviceKey, extra = {}) {
  return {
    service_key: serviceKey, required: true, confirmed: true,
    type: {}, performance: {}, status: 'passed', ...extra,
  };
}

/** Round D 完整流程 fixture：四服务全部显式声明（含 RTK planning）。 */
function rounddFlow(over = {}) {
  return {
    project: {name: 'Round D'},
    device_catalog: {status: 'passed', count: 0, items: []},
    required_cns: {
      status: 'passed', source: 'user_configuration',
      project_default: {
        communication: {
          required: true, status: 'passed',
          services: {'C:communication': serviceRequirement('C:communication')},
        },
        navigation: {
          required: true, status: 'passed',
          services: {
            'N:rtk_augmentation': serviceRequirement('N:rtk_augmentation', {
              planning: {...RTK_PLANNING_CONFIRMED},
            }),
          },
        },
        surveillance: {
          required: true, status: 'passed', service_requirement_mode: 'all_required',
          services: {
            'S:rid_cooperative': serviceRequirement('S:rid_cooperative'),
            'S:radar_noncooperative': serviceRequirement('S:radar_noncooperative'),
          },
        },
      },
    },
    existing_cns_facilities: {
      status: 'passed', count: 1,
      items: [{
        facility_id: 'NAV-1', site_id: 'NAV-1', name: '已建成参考站',
        coordinate: [122.2, 30.0], status: 'active',
        metadata: {
          navigation_site_suitability: {
            confirmed: true, planning_use_confirmed: true,
            reference_station_installed: true, source: 'site_survey',
          },
        },
      }],
    },
    tower_colocation_candidates: {
      status: 'passed', count: 1,
      items: [{
        site_id: 'tower-colocation:TT-1', name: '共塔候选', coordinate: [122.21, 30.0],
        metadata: {
          host: {host_tower_id: 'TT-1'},
          navigation_site_suitability: {
            confirmed: true, planning_use_confirmed: true,
            reference_station_installed: false, source: 'site_survey',
          },
        },
      }],
    },
    candidate_sites: {status: 'not_calculated', count: 0, items: []},
    towers: {status: 'passed', count: 0, items: []},
    ...over,
  };
}

/** legacy 项目：只有旧版子系统需求，**没有** services。 */
function legacyFlow() {
  return {
    project: {name: 'Legacy'},
    device_catalog: {status: 'passed', count: 0, items: []},
    required_cns: {
      status: 'passed',
      project_default: {
        communication: {required: true},
        navigation: {required: false},
        surveillance: {required: true, service_key: 'S:rid_cooperative'},
      },
    },
    existing_cns_facilities: {status: 'not_calculated', items: []},
    candidate_sites: {status: 'not_calculated', items: []},
    towers: {status: 'not_calculated', items: []},
  };
}

/** P15 canonical 结果：四服务各自一个桶（Radar + RID 都是 S 子系统）。 */
const CORRIDOR_GAP = {
  status: 'passed',
  routes: [{
    route_id: 'R1', status: 'pending_confirmation',
    subsystems: [
      {
        subsystem: 'C', status: 'pending_confirmation', combined_status: 'satisfied',
        service_redundancy: [{
          service_key: 'C:communication', voxel_count: 3, status: 'satisfied',
          status_counts: {satisfied: 3, confirmed_deficit: 0, unknown: 0},
          surface_class_counts: {land: 3},
        }],
      },
      {
        subsystem: 'N', status: 'pending_confirmation', combined_status: 'confirmed_gap',
        service_redundancy: [{
          service_key: 'N:rtk_augmentation', voxel_count: 2, status: 'confirmed_deficit',
          status_counts: {satisfied: 0, confirmed_deficit: 2, unknown: 0},
          surface_class_counts: {land: 2},
          geometry_status_counts: {satisfied: 2, confirmed_deficit: 0, unknown: 0},
          delivery_status_counts: {confirmed_deficit: 2},
          gap_cause_counts: {correction_delivery_deficit: 2},
          evidence_required_reasons: ['correction_delivery_confirmed_deficit'],
          dependency_only: true, recommended_dependency_service: 'C:communication',
          max_reference_baseline_m: 15000, required_distinct_site_count: 2,
          distinct_site_count: 2, delivery_service_key: 'C:communication',
        }],
      },
      {
        subsystem: 'S', status: 'pending_confirmation', combined_status: 'confirmed_gap',
        service_redundancy: [
          {
            service_key: 'S:rid_cooperative', voxel_count: 2, status: 'satisfied',
            status_counts: {satisfied: 2, confirmed_deficit: 0, unknown: 0},
            surface_class_counts: {land: 2},
          },
          {
            service_key: 'S:radar_noncooperative', voxel_count: 2, status: 'confirmed_deficit',
            status_counts: {satisfied: 0, confirmed_deficit: 2, unknown: 0},
            surface_class_counts: {land: 2},
            required_distinct_site_count_by_surface: {land: 2},
            distinct_site_count_by_surface: {land: 1},
          },
        ],
      },
    ],
  }],
};

/** P14 canonical 明细（按需载入后的 detail）。 */
const CORRIDOR_DETAIL = {
  status: 'passed', detail_available: true,
  detail: {
    routes: [{
      route_id: 'R1',
      voxels: [{
        voxel_id: 'V1', surface_class: 'land',
        subsystems: [
          {
            subsystem: 'C', service_redundancy: [{
              service_key: 'C:communication', status: 'satisfied', surface_class: 'land',
              required_distinct_site_count: 2, distinct_site_count: 2,
            }],
          },
          {
            subsystem: 'N', service_redundancy: [{
              service_key: 'N:rtk_augmentation', status: 'confirmed_deficit',
              surface_class: 'land', required_distinct_site_count: 2, distinct_site_count: 1,
              geometry_status: 'satisfied', delivery_status: 'confirmed_deficit',
              gap_causes: ['correction_delivery_deficit'],
              dependency_only: true, recommended_dependency_service: 'C:communication',
              max_reference_baseline_m: 15000, providers: [],
              reasons: ['correction_delivery_confirmed_deficit'],
            }],
          },
          {
            subsystem: 'S', service_redundancy: [
              {
                service_key: 'S:rid_cooperative', status: 'satisfied', surface_class: 'land',
                required_distinct_site_count: 2, distinct_site_count: 2,
              },
              {
                service_key: 'S:radar_noncooperative', status: 'confirmed_deficit',
                surface_class: 'unknown', required_distinct_site_count: 2, distinct_site_count: 1,
                providers: [{radar_type: 'radar_i', tower_id: 'TT-1'}], reasons: [],
              },
            ],
          },
        ],
      }],
    }],
  },
};

/** P16 canonical 结果：四类 planner action（含 navigation / radar 专属字段）。 */
const SITE_PLAN = {
  status: 'proposal_ready',
  selected_actions: [
    {
      action_id: 'existing_cns_facility:F1:C-1', action_type: 'add_device_to_existing_facility',
      planner_family: 'omnidirectional_site', service_key: 'C:communication', subsystem: 'C',
      device_id: 'C-1', distinct_site_id: 'F1', reuse_class: 'existing_cns_facility',
      planning_origin: 'existing_cns_facility', required_units: 2, current_units: 1,
      status: 'confirmed_deficit',
      impact: {target_progress: [{before_units: 1, after_units: 2}]},
    },
    {
      action_id: 'tower_colocation_host:TT-9:S-RID-1', action_type: 'add_device_to_explicit_site',
      planner_family: 'omnidirectional_site', service_key: 'S:rid_cooperative', subsystem: 'S',
      device_id: 'S-RID-1', distinct_site_id: 'tower:TT-9', reuse_class: 'tower_colocation_host',
      planning_origin: 'tower_colocation_host', required_units: 2, current_units: 1,
      status: 'confirmed_deficit',
      impact: {target_progress: [{before_units: 1, after_units: 2}]},
    },
    {
      action_id: 'navigation-reference-station:tower:TT-9',
      action_type: 'add_navigation_reference_station',
      planner_family: 'navigation_reference_station', service_key: 'N:rtk_augmentation',
      subsystem: 'N', device_id: null, equipment_selection_status: 'not_selected',
      planning_unit: 'reference_station_engineering_planning_unit',
      maturity: 'engineering_planning_proposal', coordinate: [122.2, 30.0],
      distinct_site_id: 'tower:TT-9', reuse_class: 'tower_colocation_host',
      planning_origin: 'tower_colocation_host', required_units: 2, current_units: 1,
      status: 'confirmed_deficit',
      impact: {target_progress: [{before_units: 1, after_units: 2}]},
    },
    {
      action_id: 'radar:R1:panel-2', action_type: 'add_radar_panel',
      planner_family: 'directional_radar', service_key: 'S:radar_noncooperative', subsystem: 'S',
      radar_type: 'radar_i', tower_id: 'TT-9', distinct_site_id: 'tower:TT-9',
      reuse_class: 'tower_colocation_host',
      panel: {
        panel_id: 'panel-2', azimuth_deg: 120, beamwidth_deg: 90, elevation_center_deg: 22.5,
      },
      provenance: {algorithm_id: 'radar_surveillance_layout', algorithm_version: '1.1'},
      required_units: 2, current_units: 1, status: 'confirmed_deficit',
      impact: {target_progress: [{before_units: 1, after_units: 2}]},
    },
  ],
};

const RADAR_LAYOUT = {
  status: 'proposal_ready',
  items: [{
    route_id: 'R1', status: 'proposal_ready', stage_label: '两阶段 MILP 完成',
    radar_i_panel_count: 2, radar_ii_panel_count: 1,
    selected_panel_count: 3, selected_tower_count: 2,
    selected_tower_ids: ['TT-1', 'TT-2'],
    selected_panels: [{radar_type: 'radar_i', tower_id: 'TT-1', azimuth_deg: 120,
      panel_half_width_deg: 45}],
  }],
};

// ---- A. 四服务正式中文名 -----------------------------------------------------

test('A: the four CNS services carry the frozen formal Chinese names', () => {
  assert.deepEqual(CNS_ROUNDD_SERVICE_KEYS, [
    'C:communication', 'N:rtk_augmentation', 'S:rid_cooperative', 'S:radar_noncooperative',
  ]);
  assert.equal(CNS_SERVICE_FORMAL_LABELS['C:communication'], '通信');
  assert.equal(CNS_SERVICE_FORMAL_LABELS['N:rtk_augmentation'], '导航增强（GNSS/RTK）');
  assert.equal(CNS_SERVICE_FORMAL_LABELS['S:rid_cooperative'], '合作监视（RID）');
  assert.equal(CNS_SERVICE_FORMAL_LABELS['S:radar_noncooperative'], '非合作监视（Radar）');
  // RID 与 Radar 绝不合并成模糊的「监视」
  assert.notEqual(
    CNS_SERVICE_FORMAL_LABELS['S:rid_cooperative'],
    CNS_SERVICE_FORMAL_LABELS['S:radar_noncooperative'],
  );
  assert.doesNotMatch(CNS_SERVICE_FORMAL_LABELS['C:communication'], /监视/);
  // 既有 Round 3 词表不回归
  assert.equal(SERVICE_KEY_LABELS['S:rid_cooperative'], 'RID 合作监视');
});

// ---- B. legacy 项目不自动创建服务需求 ---------------------------------------

test('B: a legacy project without services never auto-creates RTK/Radar requirements', () => {
  const model = rounddRequiredServicesModel(legacyFlow());
  assert.equal(model.servicesDeclared, false);
  assert.equal(model.ridRequired, false);
  assert.equal(model.radarRequired, false);
  assert.equal(model.rows.every(row => row.required === false), true);
  assert.equal(model.rows.every(row => row.services === null), true);

  const panel = renderRequiredCnsServicesPanel(legacyFlow());
  assert.doesNotMatch(panel, /\bchecked\b/, 'legacy 项目打开本页不得勾选任何服务');
  assert.match(panel, /打开本页<b>不会<\/b>自动写入任何服务要求/);
  // 写动作只在"用户显式启用 / 显式取消"时写 services（源码护栏）
  const bindBody = step05Source.slice(step05Source.indexOf('const writeServiceRequirement='));
  assert.match(bindBody, /if\(required\|\|existed\)/);
  assert.match(bindBody, /if\(Object\.keys\(services\)\.length\)bucket\.services=services;/);
});

// ---- C. all_required 双通道监视（只读） --------------------------------------

test('C: RID + Radar produce the read-only all_required dual-channel mode', () => {
  const model = rounddRequiredServicesModel(rounddFlow());
  assert.equal(model.ridRequired, true);
  assert.equal(model.radarRequired, true);
  assert.equal(model.allRequired, true);

  const panel = renderRequiredCnsServicesPanel(rounddFlow());
  assert.equal(ALL_REQUIRED_MODE_LABEL, '双通道均需满足（all_required）');
  assert.match(panel, /监视要求模式：<b>双通道均需满足（all_required）<\/b>/);
  assert.match(panel, /该语义只读/);
  // 不提供"任选一个即可"的**选项控件**（all_required 语义只读）
  assert.doesNotMatch(panel, /<select[^>]*id="surveillance/i);
  assert.doesNotMatch(panel, /<option[^>]*>任选/);
  assert.doesNotMatch(panel, /service_requirement_mode"[^>]*>[\s\S]{0,40}<option/);
});

// ---- D. RID / Radar 计数绝不相加 --------------------------------------------

test('D: RID and Radar provider counts are never added together', () => {
  const panel = renderSurveillanceDualChannelPanel(rounddFlow({
    cns_corridor_gap_assessment: CORRIDOR_GAP,
  }));
  assert.match(panel, new RegExp(SURVEILLANCE_DUAL_CHANNEL_LABELS.rid));
  assert.match(panel, new RegExp(SURVEILLANCE_DUAL_CHANNEL_LABELS.radar));
  assert.match(panel, new RegExp(SURVEILLANCE_DUAL_CHANNEL_LABELS.dual));
  assert.doesNotMatch(panel, /站点总数|合计站点|监视站点总数|总共 \d+ 个站址/);
  assert.match(panel, /绝不.*相加|一律分服务/);
  // 双通道结论只聚合后端状态（最不利），绝不重算 provider
  assert.equal(dualChannelStatus(['satisfied', 'satisfied']), 'satisfied');
  assert.equal(dualChannelStatus(['satisfied', 'confirmed_deficit']), 'confirmed_deficit');
  assert.equal(dualChannelStatus(['satisfied', 'unknown']), 'unknown');
  assert.equal(dualChannelStatus([]), 'not_declared');
  assert.match(panel, /未满足（存在监视缺口）/);
});

// ---- E. 导航策略未确认 -------------------------------------------------------

test('E: an unconfirmed navigation policy is shown as pending confirmation', () => {
  const flow = rounddFlow({
    required_cns: {
      project_default: {
        navigation: {
          required: true,
          services: {
            'N:rtk_augmentation': serviceRequirement('N:rtk_augmentation', {
              planning: {...RTK_PLANNING_UNCONFIRMED},
            }),
          },
        },
      },
    },
  });
  const model = navigationPlanningModel(flow);
  assert.equal(model.confirmed, false);
  assert.equal(model.readiness, 'pending_confirmation');
  assert.equal(model.missingEvidence, true);
  const panel = renderNavigationPlanningPanel(flow);
  assert.match(panel, /规划就绪状态：<b>待确认<\/b>/);
  assert.match(panel, /仍有工程参数尚未确认/);
});

// ---- F. baseline 缺失 ⇒ 不画 envelope ---------------------------------------

test('F: a missing canonical baseline never draws a navigation envelope', () => {
  const flow = rounddFlow({
    required_cns: {
      project_default: {
        navigation: {
          required: true,
          services: {
            'N:rtk_augmentation': serviceRequirement('N:rtk_augmentation', {
              planning: {...RTK_PLANNING_NO_BASELINE},
            }),
          },
        },
      },
    },
  });
  const model = navigationBaselineModel(flow);
  assert.equal(model.baselineM, null);
  assert.equal(model.drawable, false);
  assert.equal(model.baselineText, '未配置');
  assert.equal(drawCnsNavigationEnvelope({
    ctx: recordingCtx(), view: {res: 10}, screenPoint: () => [10, 10], model,
  }), 0);
  // 面板也必须显示"未配置"，绝不预填任何距离
  const panel = renderNavigationPlanningPanel(flow);
  assert.match(panel, /导航增强工程基线范围：<b>未配置<\/b>/);
  assert.doesNotMatch(panel, /value="1[05]000"|value="20000"/);
});

// ---- G. confirmed + baseline ⇒ 画工程基线 -----------------------------------

test('G: a confirmed policy with a canonical baseline draws the engineering baseline', () => {
  const model = navigationBaselineModel(rounddFlow());
  assert.equal(model.confirmed, true);
  assert.equal(model.baselineM, 15000);
  assert.equal(model.baselineText, '15.0 km');
  assert.equal(model.drawable, true);
  assert.equal(model.sites.length, 2);

  const ctx = recordingCtx();
  const drawn = drawCnsNavigationEnvelope({
    ctx, view: {res: 10}, screenPoint: coordinate => [100 + coordinate[0], 100], model,
  });
  assert.ok(drawn >= 2, '已建成参考站与规划候选站都必须画出工程基线范围');
  // 规划候选站用虚线；已建成参考站用实线
  const dashes = ctx.calls.filter(call => call[0] === 'setLineDash').map(call => call[1]);
  assert.ok(dashes.some(dash => Array.isArray(dash) && dash.length === 2), '必须有虚线样式');
  assert.equal(NAVIGATION_BASELINE_LEGEND_LABEL, '导航增强工程基线范围');

  const legend = cnsServiceLegendModel();
  const baseline = legend.find(line => line.id === 'cns-navigation-baseline');
  assert.ok(baseline, '导航增强工程基线图例行必须存在');
  assert.match(baseline.label, /导航增强工程基线范围/);
  assert.match(baseline.label, /实线：已建成参考站/);
  assert.match(baseline.label, /虚线：规划候选站/);
});

// ---- H. 图例红线措辞 ---------------------------------------------------------

test('H: the legend never uses forbidden RTK coverage wording', () => {
  const legend = cnsServiceLegendModel();
  const text = legend.map(line => line.label + '|' + line.note).join('\n');
  for (const forbidden of [
    'RTK 无线覆盖范围', 'RTK 覆盖半径', '无线覆盖距离', '保证导航范围', '实测服务范围',
    'RTK 无线覆盖', '全域覆盖半径',
  ]) {
    assert.doesNotMatch(text, new RegExp(forbidden), `图例不得出现红线措辞：${forbidden}`);
  }
  assert.match(text, /导航增强工程基线范围/);
  assert.match(text, /不代表实测 RTK 服务半径/);
  // 全图统一免责声明必须逐字出现
  const disclaimer = legend.find(line => line.id === 'cns-legend-disclaimer');
  assert.ok(disclaimer, '统一几何/基线免责声明行必须存在');
  assert.equal(disclaimer.note, CNS_SERVICE_GEOMETRY_LEGEND_NOTE);
  // Radar 与 RID 的几何语义仍分列
  assert.match(text, new RegExp(RADAR_DIRECTIONAL_GEOMETRY_NOTE));
  assert.match(text, /合作监视：全向几何/);
});

// ---- I. navigation action device_id=null ------------------------------------

test('I: a navigation action with device_id=null renders the planning unit, not an error', () => {
  const cards = facilityPlanCards(SITE_PLAN);
  assert.match(cards, /新增 GNSS\/RTK 基准站规划单元/);
  assert.match(cards, /设备选型状态 尚未选择/);
  assert.match(cards, /当前仅完成站址与服务规划，具体设备型号尚未选择。/);
  assert.doesNotMatch(cards, /新增 设备</);
  assert.doesNotMatch(cards, /设备型号缺失|未找到设备|device_id 缺失/);
  // raw 规划单元字段只在高级详情里
  const text = visibleText(cards);
  assert.doesNotMatch(text, /reference_station_engineering_planning_unit/);
  assert.match(cards, /高级：规划单元原始字段/);
});

// ---- J. dependency_only -----------------------------------------------------

test('J: dependency_only tells the user to add communication, not a navigation station', () => {
  const statements = serviceGapStatements({cns_corridor_gap_assessment: CORRIDOR_GAP});
  assert.match(statements, /无需新增导航站，应优先补通信服务/);
  assert.match(statements, /RTK 修正数据通信交付不足/);
  assert.match(statements, /参考站几何/);
  // 主文本不出现 raw enum
  assert.doesNotMatch(visibleText(statements), /dependency_only|recommended_dependency_service/);

  const corridor = renderCorridorServiceEvidence({
    cns_corridor_assessment: CORRIDOR_DETAIL,
  });
  assert.match(corridor, /无需导航建站，仅需通信补盲/);
  assert.doesNotMatch(visibleText(corridor), /dependency_only/);
});

// ---- K. 普通 N tower 不是已建参考站 ------------------------------------------

test('K: a plain tower without confirmed suitability is never an existing station', () => {
  const flow = rounddFlow({
    tower_colocation_candidates: {status: 'not_calculated', count: 0, items: []},
    towers: {
      status: 'passed', count: 1,
      items: [{
        tower_id: 'TT-2', name: '普通铁塔', coordinate: [122.3, 30.0],
        available_subsystems: ['N'],
      }],
    },
  });
  const sites = navigationSuitabilitySites(flow);
  const tower = sites.find(item => item.siteSource === 'tower_colocation_host');
  assert.ok(tower, '真实铁塔必须作为可编辑站址出现');
  assert.equal(tower.suitability, null, '未声明 suitability 时必须是无证据，不是合格');
  assert.equal(navigationSuitabilityOf({tower_id: 'TT-2'}), null);

  const model = navigationBaselineModel(flow);
  assert.equal(model.sites.some(site => site.label === '普通铁塔'), false);
  assert.equal(model.candidates.length, 0, '连 suitability 声明都没有 ⇒ 不画正式 envelope，也不冒充候选');
  // available_subsystems 含 N 绝不构成资格
  assert.equal(navigationReferenceStationInstalled(null, 'tower_colocation_host'), false);
});

// ---- L. 已建站 vs 可规划候选 -------------------------------------------------

test('L: reference_station_installed separates installed stations from planning candidates', () => {
  assert.equal(navigationReferenceStationInstalled({reference_station_installed: true}, 'candidate_site'), true);
  assert.equal(navigationReferenceStationInstalled({reference_station_installed: false}, 'existing_cns_facility'), false);
  assert.equal(navigationReferenceStationInstalled({}, 'existing_cns_facility'), true);
  assert.equal(navigationReferenceStationInstalled({}, 'tower_colocation_host'), false);

  const model = navigationBaselineModel(rounddFlow());
  const kinds = model.sites.map(site => site.kind).sort();
  assert.deepEqual(kinds, ['existing_reference_station', 'proposed_reference_station']);
  const towerSite = model.sites.find(site => site.label === '共塔候选');
  assert.equal(towerSite.kind, 'proposed_reference_station',
    '共塔候选的 suitability 确认只表示"获准安装"，绝不是已建成参考站');

  const panel = renderNavigationSuitabilityPanel(rounddFlow());
  assert.match(panel, /「已有参考站已建成」才表示已有参考站事实/);
  assert.match(panel, /已有参考站已建成/);
  assert.match(panel, /允许作为导航基准站规划候选/);
});

// ---- M. Radar 仍是方向性图层 -------------------------------------------------

test('M: Radar keeps the directional overlay and adds no circle renderer', () => {
  // CNS service overlay 绝不出现扇区 / 方位角字段名
  assert.doesNotMatch(overlaySource, /sectorPath|half_width_deg|azimuth_deg/);
  // Radar 扇形仍只由既有 radar_layout_overlay 提供，且不被 CNS service 语义污染
  assert.doesNotMatch(radarOverlay, /cns_service|RID|rid_cooperative/);
  assert.doesNotMatch(radarOverlay, /cnsNavigation/);
  // 绘制入口只为 Communication / RID / 导航基线服务，不为 Radar 画圆
  const draw = overlaySource.slice(
    overlaySource.indexOf('export function drawCnsServiceOverlay'),
    overlaySource.indexOf('function radiusForSite'));
  assert.ok(draw.length > 0);
  assert.doesNotMatch(draw, /radar_noncooperative|radar_i|radar_ii/);
  assert.match(draw, /cnsNavigationLayer/);
  // radar 图层仍由既有开关驱动
  assert.match(displayLayers, /layers\.radarSurveillanceLayer===true/);
});

// ---- N. 六类分析图层 reopen 后关闭 ------------------------------------------

test('N: all six CNS analysis layers (including navigation) are off after reopen', () => {
  assert.ok(CNS_SERVICE_LAYER_IDS.includes('cnsNavigationLayer'));
  const boxes = checkboxAttributes(html);
  for (const id of CNS_SERVICE_LAYER_IDS) {
    assert.ok(boxes.has(id), `图层抽屉缺少开关 ${id}`);
    assert.doesNotMatch(boxes.get(id), /\bchecked\b/, `${id} 必须默认关闭`);
  }
  const block = main.slice(
    main.indexOf('const PROJECT_REOPEN_RESET_LAYER_IDS='),
    main.indexOf('async function openProject('));
  assert.ok(block.length > 0, 'main.js 必须保留复位常量与函数');
  for (const id of CNS_SERVICE_LAYER_IDS) {
    assert.match(block, new RegExp("'" + id + "'"), `复位列表缺少 ${id}`);
    assert.match(main, new RegExp("'" + id + "'"), `main.js 的 LAYER_IDS 缺少 ${id}`);
  }
  assert.match(block, /removeAttribute\('checked'\)/);
  const openBody = main.slice(
    main.indexOf('async function openProject('), main.indexOf('function getTiandituKey('));
  const resets = [...openBody.matchAll(/resetAnalysisLayerSelection\(\);\s*\n\s*renderWorkflow\(\);/g)];
  assert.equal(resets.length, 2, '两条打开路径都必须在 renderWorkflow 前复位图层');
  assert.doesNotMatch(block, /'online'/);
  // 新图层也进入显示层合流点
  assert.match(displayLayers, /layers\.cnsNavigationLayer===true/);
});

// ---- O. P15 四服务分开展示 ---------------------------------------------------

test('O: P15 lists the four services separately', () => {
  const statements = serviceGapStatements({cns_corridor_gap_assessment: CORRIDOR_GAP});
  for (const key of CNS_ROUNDD_SERVICE_KEYS) {
    assert.match(statements, new RegExp('data-service-key="' + key + '"'), `P15 缺少 ${key}`);
  }
  assert.match(statements, /合作监视（RID）/);
  assert.match(statements, /非合作监视（Radar）/);
  assert.match(statements, /导航增强（GNSS\/RTK）/);
  // 导航增强必须分开 geometry / delivery
  assert.match(statements, /参考站几何/);
  assert.match(statements, /通信修正数据交付/);
  assert.match(statements, /缺口原因/);
  // Radar 必须声明方向性几何
  assert.match(statements, new RegExp(RADAR_DIRECTIONAL_GEOMETRY_NOTE.slice(0, 12)));
});

// ---- P. P16 四类 action 分开展示 --------------------------------------------

test('P: P16 lists the four service actions separately', () => {
  const cards = facilityPlanCards(SITE_PLAN);
  for (const key of CNS_ROUNDD_SERVICE_KEYS) {
    assert.match(cards, new RegExp('data-service-key="' + key + '"'), `P16 缺少 ${key}`);
  }
  assert.match(cards, /data-planner-family="navigation_reference_station"/);
  assert.match(cards, /data-planner-family="directional_radar"/);
  // Radar action 明确是方向性面阵，且数值来自 canonical action
  assert.match(cards, /方向性 Radar panel/);
  assert.match(cards, /Radar-I（中近程雷达Ⅰ型）/);
  assert.match(cards, /方位角 120°/);
  assert.match(cards, /波束宽度 90°/);
  assert.match(cards, /俯仰预设 22\.5°/);
  assert.match(cards, /绝不按普通圆形覆盖站渲染/);
  // 站址来源 / distinct_site_id / 动作 / 收益都在卡片上
  assert.match(cards, /站址来源：/);
  assert.match(cards, /distinct_site_id：tower:TT-9/);
  assert.match(cards, /同一物理站址，不增加独立站址重数/);
  assert.doesNotMatch(cards, /3 重服务冗余|三重服务|重服务冗余/);
});

// ---- Q. unknown surface 不是 sea --------------------------------------------

test('Q: an unknown surface is never rendered as sea', () => {
  const corridor = renderCorridorServiceEvidence({cns_corridor_assessment: CORRIDOR_DETAIL});
  assert.match(corridor, /证据不足/);
  assert.doesNotMatch(corridor, /surface_class：海上/);
  assert.doesNotMatch(corridor, /海上 1 个体元/);
  const tooltip = cnsMapFeatureTooltip({
    kind: 'gap_segment', service_key: 'S:radar_noncooperative', state: 'unknown',
    route_id: 'R1', from_m: 0, to_m: 120, length_m: 120, causes: [],
    from: [122.2, 30.0], to: [122.21, 30.0],
  }, {cns_corridor_gap_assessment: CORRIDOR_GAP});
  assert.match(tooltip, /非合作监视（Radar）/);
  assert.doesNotMatch(tooltip, /海上/);
});

// ---- R. 前端无 RTK 默认距离 --------------------------------------------------

test('R: no RTK default distance constant exists in the front-end source', () => {
  const sources = [
    ['service_semantics.js', semanticsSource],
    ['cns_service_evidence.js', evidenceSource],
    ['cns_service_overlay.js', overlaySource],
    ['step05_cns.js', step05Source],
  ];
  for (const [name, raw] of sources) {
    const source = stripComments(raw);
    assert.doesNotMatch(source, /max_reference_baseline_m\s*[:=]\s*\d/,
      `RTK 基线距离不得有默认值：${name}`);
    assert.doesNotMatch(source, /\b(?:10000|15000|20000)\b/,
      `不得出现预填的 RTK 基线距离数字：${name}`);
  }
  // 面板在未配置时显示"未配置"，而不是任何一个数字
  const panel = renderNavigationPlanningPanel(legacyFlow());
  assert.match(panel, /导航增强工程基线范围：<b>未配置<\/b>/);
});

// ---- S. 不复制 Communication / RID 半径 authority ---------------------------

test('S: the front-end never copies the Communication / RID planning radius authority', () => {
  const sources = [
    ['service_semantics.js', semanticsSource],
    ['cns_service_evidence.js', evidenceSource],
    ['cns_service_overlay.js', overlaySource],
    ['step05_cns.js', step05Source],
  ];
  for (const [name, raw] of sources) {
    const source = stripComments(raw);
    assert.doesNotMatch(source, /radius_by_surface\s*[:=]\s*\{[^}]*\d/,
      `不得硬编码 radius_by_surface：${name}`);
    assert.doesNotMatch(source, /\b(?:4000|2000|5000)\b/,
      `不得复制 4000 / 2000 / 5000 半径常量：${name}`);
  }
  // 覆盖模型只从后端字段取半径
  const model = cnsServiceOverlayModel({
    existing_cns_facilities: {
      status: 'passed', count: 1,
      items: [{
        facility_id: 'F1', name: '通信站', coordinate: [122.2, 30.0],
        devices: [{
          device_id: 'C-1', subsystem: 'C', status: 'active',
          service_key: 'C:communication',
          coverage_geometry: {radius_by_surface: {land: 4321, sea: 1234}},
        }],
      }],
    },
  });
  const site = model.sites.find(item => item.service_key === 'C:communication');
  assert.equal(site.radius_by_surface.land, 4321);
  assert.equal(site.radius_by_surface.sea, 1234);
});

// ---- T. raw enum 不作主要业务文本 -------------------------------------------

test('T: raw enums are never the primary business text', () => {
  const panels = [
    renderRequiredCnsServicesPanel(rounddFlow()),
    renderNavigationPlanningPanel(rounddFlow()),
    renderNavigationSuitabilityPanel(rounddFlow()),
    renderSurveillanceDualChannelPanel(rounddFlow({cns_corridor_gap_assessment: CORRIDOR_GAP})),
    renderRadarServicePanel(rounddFlow({radar_surveillance_layout: RADAR_LAYOUT})),
    serviceGapStatements({cns_corridor_gap_assessment: CORRIDOR_GAP}),
    facilityPlanCards(SITE_PLAN),
  ].join('\n');
  const text = visibleText(panels);
  for (const raw of [
    'confirmed_deficit', 'dependency_only', 'planning_readiness', 'not_calculated',
    'proposal_ready', 'reference_station_engineering_planning_unit', 'not_selected',
    'pending_confirmation', 'confirmed_gap', 'proposed_reference_station',
  ]) {
    assert.doesNotMatch(text, new RegExp(raw), `raw enum 不得作为主要业务文本：${raw}`);
  }
  // 主界面必须给出中文结论
  assert.match(text, /待确认|未配置|证据不足|满足|缺口/);
  // Radar 卡片的模型状态必须中文化
  assert.match(text, /候选划设就绪/);
});

// ---- 附加：地图 tooltip 与整页接线 ------------------------------------------

test('Round D: map tooltips stay service-separated and read-only', () => {
  const flow = rounddFlow({cns_corridor_gap_assessment: CORRIDOR_GAP});
  const layers = {cnsServiceGapLayer: true, cnsFacilityPlanLayer: true, cnsNavigationLayer: true};
  const screenPoint = () => [50, 50];
  const feature = cnsMapFeatureAt({
    click: [50, 50], flow, layers, screenPoint,
  });
  assert.ok(feature, '导航基线站点必须可命中');
  const tooltip = cnsMapFeatureTooltip(feature, flow);
  assert.match(tooltip, /导航增强工程基线范围|CNS 服务缺口|CNS 设施规划动作/);
  assert.doesNotMatch(tooltip, /站点总数/);
});

test('Round D: Step05 mounts the four-service workbench panels', () => {
  const rendered = renderStep5({flow: rounddFlow()});
  assert.match(rendered, /CNS 服务需求/);
  assert.match(rendered, /导航增强（GNSS\/RTK）工程规划/);
  assert.match(rendered, /导航基准站候选适用性/);
  assert.match(rendered, /非合作监视（Radar）/);
  assert.match(rendered, /监视总览（双通道）/);
  assert.match(rendered, /四服务走廊证据（service-aware）/);
  // 站址适用性表单字段 id 必须与 bind 使用的常量一致
  for (const id of Object.values(NAVIGATION_SUITABILITY_DOM_IDS)) {
    assert.match(rendered, new RegExp('id="' + id + '"'), `缺少站址适用性字段 ${id}`);
  }
});
