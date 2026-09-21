/**
 * Phase 3.5 稳定化前端契约测试。
 *
 * 覆盖两件事：
 * 1. 规划终态语义：``search_incomplete``（搜索预算耗尽）绝不能被展示成 ``blocked``
 *    或"空域不可行"，且必须与 ``no_path`` 可区分；
 * 2. 建筑几何质量报告必须随验证证据一起被前端模型原样保留（含 make_valid 统计），
 *    不允许在投影层被丢字段。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {candidateModel} from '../cns_planner/web/js/workflow/layered_theta_v2.js';
import {
  layeredRouteValidationModel, renderLayeredRouteValidation,
} from '../cns_planner/web/js/workflow/layered_route_validation.js';

test('search_incomplete stays distinct from no_path and from blocked', () => {
  const limited = candidateModel({
    candidate_id:'LC-LIMITED', status:'search_incomplete', terminal_status:'search_incomplete',
    terminal_status_semantics:'search_budget_exhausted_reachability_not_proven',
    search_incomplete:true,
    blocking_reasons:[{
      reason_code:'search_budget_exhausted', reason:'达到 max_expanded_labels',
      resource_limit:'max_expanded_labels', reachability_proven:false, optimality_proven:false,
    }],
  }, true);
  assert.equal(limited.status, 'search_incomplete');
  assert.equal(limited.terminalStatus, 'search_incomplete');
  assert.equal(limited.terminalStatusSemantics,
    'search_budget_exhausted_reachability_not_proven');
  assert.equal(limited.resourceLimited, true);
  assert.equal(limited.reachabilityProven, false);
  assert.equal(limited.resourceLimit, 'max_expanded_labels');
  assert.equal(limited.searchIncomplete, true);

  const noPath = candidateModel({
    candidate_id:'LC-NOPATH', status:'no_path', terminal_status:'no_path',
    terminal_status_semantics:'search_completed_without_a_feasible_path',
    search_incomplete:false,
    blocking_reasons:[{
      reason_code:'no_traversable_path', reachability_proven:true, optimality_proven:false,
    }],
  }, true);
  assert.equal(noPath.status, 'no_path');
  assert.equal(noPath.resourceLimited, false);
  assert.equal(noPath.searchIncomplete, false);
  assert.notEqual(limited.terminalStatusSemantics, noPath.terminalStatusSemantics);

  const candidateSuccess = candidateModel({
    candidate_id:'LC-OK', status:'candidate',
    terminal_status:'candidate',
    terminal_status_semantics:'search_completed_with_a_feasible_path',
  }, true);
  assert.equal(candidateSuccess.terminalStatus, 'candidate');
  assert.equal(candidateSuccess.resourceLimited, false);
});

test('building geometry quality report survives the validation projection', () => {
  const validation = {
    validation_id:'LRV-TEST', status:'unresolved',
    status_reason:'unresolved_domain_evidence',
    current_applicability:'current', route_id:'R-1', altitude_layer_id:'L-100',
    validated_at:'2026-09-21T00:00:00Z', source_type:'configured_real_sources',
    candidate:{candidate_id:'LC-1', candidate_fingerprint:'cfp', path_fingerprint:'pfp'},
    fingerprints:{validation_fingerprint:'vfp'},
    minimum_margins:{terrain_vertical_m:40.25, building_vertical_m:null},
    resource_limits:{max_evidence_items:20000, observed_evidence_items:10,
      limit_reached:false, safety_parameter:false},
    policies:{terrain_vertical_clearance_m:50, building_horizontal_clearance_m:0,
      building_vertical_clearance_m:10},
    domains:{
      terrain:{status:'passed', minimum_margin:40.25, violations:[], unresolved:[], evidence:{}},
      building:{
        status:'unresolved', reason:'building_evidence_unresolved', minimum_margin:null,
        violations:[], unresolved:[{reason_id:'building_footprint_quality_unresolved'}],
        evidence:{
          source:{id:'gba', file_name:'zhoushan_buildings.gpkg'},
          source_modified:false,
          make_valid_applied:2,
          building_quality_report:{
            status:'invalid_geometry_present',
            counts:{passed:88, repaired:2, invalid:1},
            repair:{applied_count:2, failed_count:1, method:'shapely_make_valid'},
            semantics:{unrepairable_geometry_stays_unknown:true},
          },
        },
      },
    },
    failed_intervals:[], unresolved_intervals:[],
    provenance:{shared_validators:['domain.building_geometry_quality.prepare_footprint_polygons']},
    validator_versions:{}, semantics:{},
  };
  const model = layeredRouteValidationModel({layered_route_validations:{items:[validation]}});
  const html = renderLayeredRouteValidation({layered_route_validations:{items:[validation]}});
  assert.match(html, /building quality report/);
  assert.match(html, /passed 88/);
  assert.match(html, /repaired 2/);
  assert.match(html, /invalid 1/);
  assert.match(html, /make_valid applied 2/);
  assert.match(html, /repair failed 1/);
  assert.match(html, /source rewritten false/);
  // 前端必须说明"修复失败保持 unknown"，不能让人误读成已通过。
  assert.match(html, /修复失败保持 unknown/);
  assert.ok(model);
});
