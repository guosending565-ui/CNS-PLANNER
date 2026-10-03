import test from 'node:test';
import assert from 'node:assert/strict';

import {
  corridorSitePlanSummary,
  towerColocationPolicyForm,
  towerColocationList,
} from '../cns_planner/web/js/workflow/step05_cns.js';
import {
  towerColocationDetailHtml,
} from '../cns_planner/web/js/map/tower_reference_layer.js';
import {
  cnsServiceLegendModel,
} from '../cns_planner/web/js/map/cns_service_overlay.js';

test('Round 2.6.1: frontend requires explicit planning-estimate confirmation', () => {
  const html = towerColocationPolicyForm({
    tower_colocation_policy: {},
    tower_colocation_candidates: {
      tower_count: 373,
      confirmed_origin_count: 77,
      estimated_planning_origin_count: 296,
      unusable_count: 0,
      policy: {
        planning_host_use_confirmed: true,
        service_origin_assumption: 'tower_top_agl_0',
        planning_service_origin_policy: 'terrain_plus_source_tower_height',
      },
    },
    tower_obstacle_profiles: {resolved_count: 77, unresolved_count: 296},
  });
  assert.match(html, /id="towerColocationPlanningHost" checked/);
  assert.match(html, /id="towerColocationPlanningEstimate" checked/);
  assert.match(html, /FABDEM地形正高 \+ 源铁塔高度/);
  assert.match(html, /真实铁塔 373 · confirmed origin 77 · estimated planning origin 296 · unusable 0/);
  assert.match(html, /估计值仅用于规划覆盖计算，不代表真实安装高度；实施前必须现场勘察/);
});

test('Round 2.6.1: confirmed and estimated origins have distinct list, popup and legend text', () => {
  const estimated = {
    site_id: 'tower-colocation:T1', coordinate: [122, 30],
    planning_profile: {reuse_class: 'tower_colocation_host'},
    vertical_profile: {
      planning_origin_status: 'estimated',
      planning_service_origin_egm2008_m: 42,
    },
    metadata: {
      host: {host_tower_id: 'T1', host_tower_name: 'T1', site_position_available: true},
      planning_host: {subsystem_mount_status: 'unverified'},
      obstacle_profile: {tower_top_orthometric_m: null},
    },
  };
  assert.match(towerColocationList({items: [estimated], policy: {}}), /规划估计高程（需现场勘察）/);
  assert.match(towerColocationDetailHtml(estimated, String), /规划估计高程/);
  assert.match(towerColocationDetailHtml(estimated, String), /需现场勘察/);
  const labels = cnsServiceLegendModel().map(item => item.label).join('\n');
  assert.match(labels, /已确认塔顶高程的选中共塔站址/);
  assert.match(labels, /规划估计高程的选中共塔站址/);
});

test('Round 2.6.1: P16 panel states reuse-first and exposes no reuse-ratio KPI', () => {
  const html = corridorSitePlanSummary({
    status: 'proposal_ready',
    selected_actions: [],
    candidate_actions: [],
    candidate_impacts: [],
    target_service_groups: [],
    cost_summary: {
      cost_semantics: 'explicit_costs_grouped_by_unit_with_action_count_proxy',
    },
    reuse_counts: {tower_colocation_host: 0},
  });
  assert.match(html, /规划采用基础设施复用优先，同一复用层级内再综合覆盖收益与成本/);
  assert.match(html, /explicit_costs_grouped_by_unit_with_action_count_proxy/);
  //: 复用率只是报告统计量，绝不是规划器主目标 —— 面板不得把它做成 KPI。
  assert.doesNotMatch(html, /复用率/);
});
