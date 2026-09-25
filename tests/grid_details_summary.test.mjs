/**
 * BUG-MAP-001：网格点击弹窗的"默认精简 + 详细信息"契约测试。
 *
 * 改革前：点击网格一次性输出人口 / DEM / 建筑 / 空域 / Traffic / Conflict /
 * Legacy Risk V1 / Risk V2 全部诊断，文本过长。
 * 改革后：
 *  - 默认只显示 grid_id/L、人口（count + density + coverage）、地形关键高程、
 *    建筑 count/max、当前专题关键值，**只有真实存在的值才出现**；
 *  - 原有完整诊断一字不改地保留在「详细信息」展开区；
 *  - 全部文本先转义再拼 HTML，不允许未转义 innerHTML。
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  gridCellDetails,gridCellDetailsHtml,gridCellSummary,gridCellSummaryHtml,themeKeyValue,
} from '../cns_planner/web/js/workflow/grid_details.js';

const ROOT=new URL('../',import.meta.url);
const readFile=path=>readFileSync(new URL(path,ROOT),'utf8');

const FULL_ITEM={
  cell:{grid_id:'MHT4063-L08-C00019199-RP00001785',level:8},
  population:{
    status:'passed',value_status:'passed',coverage_status:'full',valid_sample_count:9,
    population_count_people:120,population_density_people_km2:480,source_coverage_fraction:1,
    value_mean:3,value_sum:27,
  },
  terrain:{status:'passed',valid_sample_count:9,mean_elevation:23.5,min_elevation:1.2,max_elevation:88.4},
  buildings:{status:'passed',building_count:7,height_max_m:31.5,height_p95_m:24,building_coverage_ratio:.18},
  airspace:{status:'passed',intersected_layer_count:1,airspaces:[{name:'L1',feature_id:3}]},
  traffic:{status:'passed',flight_count:2,flight_seconds:60,traffic_density_raw:1,traffic_density_norm:.4},
  conflict:{status:'passed',conflict_count:0,conflict_rate:0,conflict_rate_norm:0},
  risk:{ground:{status:'passed',score:.4,level:'medium'},air:{status:'not_calculated'},
    overall:{status:'passed',score:.4,data_completeness:1,semantics:'relative_index'}},
  risk_v2:{factors:{population_exposure:{status:'passed',normalized_index:.25}},domains:{}},
};

const FULL_FLOW={
  grid_attributes:{
    population:{source_profile:{quantity:'population_count_per_source_pixel'}},
    terrain:{elevation_unit:'m',unit_status:'verified_from_raster_metadata',source:{path:'D:/x/glo30.tif'}},
  },
  grid_risk:{algorithm_id:'risk-model-v1-relative-index',algorithm_version:'1.1'},
};

// ============================================================================
// 1. 默认摘要只包含要求的那几项
// ============================================================================

test('default summary keeps only grid id, population, terrain, buildings and the current theme',()=>{
  const summary=gridCellSummary(FULL_ITEM,FULL_FLOW,String,'none');

  const lines=summary.split('\n');
  assert.equal(lines[0],'MHT4063-L08-C00019199-RP00001785 · L8');
  assert.equal(lines[1],'人口：count 120 person · density 480 person/km² · coverage 100%');
  assert.equal(lines[2],'地形：mean 23.5 m · min 1.2 m · max 88.4 m');
  assert.equal(lines[3],'建筑：count 7 · max 31.5 m');
  assert.equal(lines.length,4,'未选专题时默认摘要就是这四行');

  // 诊断类内容绝不出现在默认视图里。
  for(const noisy of ['空域：','Traffic Exposure','Conflict Exposure','Legacy Risk V1','Risk V2',
    '风险语义','DEM：样本','兼容字段']){
    assert.ok(!summary.includes(noisy),`默认摘要不得包含 ${noisy}`);
  }
});

test('default summary omits every field that has no value instead of printing a placeholder',()=>{
  const sparse={cell:{grid_id:'G2',level:7},population:{value_status:'missing_data'}};
  assert.equal(gridCellSummary(sparse,{},String,'none'),'G2 · L7\n人口：缺少数据');

  // 只有人口 count、没有 density / coverage 时，只显示真的有的那个字段。
  const countOnly={cell:{grid_id:'G3',level:8},population:{population_count_people:5}};
  assert.equal(gridCellSummary(countOnly,{},String,'none'),'G3 · L8\n人口：count 5 person');

  // 旧快照没有人数语义时仍退回源像元统计，并明确标注"非人数"。
  const legacy={cell:{grid_id:'G4',level:8},population:{valid_sample_count:4,value_mean:2.5}};
  assert.match(gridCellSummary(legacy,{},String,'none'),/人口：源像元 mean 2\.5（非人数）/);
});

test('default summary prints the current theme key value and drops it when unavailable',()=>{
  assert.match(gridCellSummary(FULL_ITEM,FULL_FLOW,String,'population'),/当前专题（人口密度）：480 person\/km²/);
  assert.match(gridCellSummary(FULL_ITEM,FULL_FLOW,String,'terrain'),/当前专题（地形高程）：23\.5 m/);
  assert.match(gridCellSummary(FULL_ITEM,FULL_FLOW,String,'building_p95'),/当前专题（P95 建筑高度）：24 m/);
  assert.match(gridCellSummary(FULL_ITEM,FULL_FLOW,String,'ground_risk'),/当前专题（Ground Risk（Legacy Risk V1））：0\.4 \/ medium/);

  // 专题无可用值 / 未选专题：整行省略。
  assert.ok(!gridCellSummary(FULL_ITEM,FULL_FLOW,String,'building_max').includes('当前专题='));
  assert.match(gridCellSummary(FULL_ITEM,FULL_FLOW,String,'building_max'),/当前专题（最大建筑高度）：31\.5 m/);
  const withoutBuildings={cell:{grid_id:'G5',level:8},population:{population_count_people:1}};
  assert.ok(!gridCellSummary(withoutBuildings,{},String,'building_max').includes('当前专题'));
  assert.ok(!gridCellSummary(FULL_ITEM,FULL_FLOW,String,'none').includes('当前专题'));
});

test('theme key value follows the Risk V2 factor and domain layers',()=>{
  assert.deepEqual(themeKeyValue(FULL_ITEM,'risk_v2:factor:population_exposure',String),
    {theme:'risk_v2:factor:population_exposure',label:'Risk V2 因子 population_exposure',value:'0.25'});
  // 未解析的因子：绝不显示伪 0。
  assert.equal(themeKeyValue(FULL_ITEM,'risk_v2:factor:terrain_relief',String).value,null);
  assert.equal(themeKeyValue(FULL_ITEM,'risk_v2:domain:ground',String).value,null);
  assert.equal(themeKeyValue(FULL_ITEM,null,String),null);
  assert.equal(themeKeyValue(FULL_ITEM,'unknown_theme',String),null);
});

// ============================================================================
// 2. 完整诊断被保留（只是折叠）
// ============================================================================

test('the full diagnostic text is preserved verbatim inside the details block',()=>{
  const details=gridCellDetails(FULL_ITEM,FULL_FLOW,String);
  for(const kept of ['目标网格人口数（person）/人口密度（person/km²）','DEM：样本 9',
    '建筑环境：','空域：','Traffic Exposure','Conflict Exposure','Legacy Risk V1',
    'Overall Risk','风险语义','Risk Framework V2（relative engineering index）']){
    assert.ok(details.includes(kept),`完整诊断必须保留 ${kept}`);
  }

  const html=gridCellDetailsHtml(FULL_ITEM,FULL_FLOW,String,'terrain');
  assert.match(html,/<details class="grid-info-detail"><summary>详细信息<\/summary>/);
  assert.ok(html.includes('Traffic Exposure'),'展开区里必须有完整诊断');
  const summaryPart=html.slice(0,html.indexOf('<details'));
  assert.ok(!summaryPart.includes('Traffic Exposure'),'完整诊断只能出现在展开区');
  assert.match(summaryPart,/^<div class="grid-info-summary">/);
});

// ============================================================================
// 3. 转义安全
// ============================================================================

test('grid detail HTML escapes every value taken from flow and item',()=>{
  const evil={
    cell:{grid_id:'<img src=x onerror=alert(1)>',level:8},
    population:{status:'passed',value_status:'passed',coverage_status:'"><script>bad()</script>',
      population_count_people:1},
  };
  const html=gridCellDetailsHtml(evil,FULL_FLOW,String,'none');
  assert.doesNotMatch(html,/<img/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html,/&lt;script&gt;/);

  // 摘要片段本身也必须已转义（调用方可以安全地整段赋给 innerHTML）。
  const summaryHtml=gridCellSummaryHtml(evil,FULL_FLOW,String,'none');
  assert.doesNotMatch(summaryHtml,/<img/);
  assert.match(summaryHtml,/^<div class="grid-info-summary">[\s\S]*<\/div>$/);
});

test('main.js renders the grid popup through the escaped summary and details builder',()=>{
  const main=readFile('cns_planner/web/js/main.js');
  assert.match(main,/import \{gridCellDetailsHtml,populationDisplayLabel\} from '\.\/workflow\/grid_details\.js'/);
  assert.match(main,/info\.innerHTML=formatGridDetails\(item\)/);
  // B4X：障碍格 / 证据不足格的约束结论先于摘要显示（网格弹窗的第一行就是"为什么不能飞"）；
  // 约束区块由 constraint_view 提供，没有读到逐格明细时返回空串（绝不显示成"可通行"）。
  assert.match(main,/constraintView\.cellDetailsHtml\(gridId\)/);
  assert.match(main,/gridCellDetailsHtml\(item,flow,GridTheme\.formatNumber,gridDisplay\.theme,constraintView\.cellFor\(gridId\),constraintView\.altitudeLayerLabel\(\)\)/);
  assert.ok(!/info\.textContent=formatGridDetails/.test(main));
  // 转义只允许在 grid_details 内部完成：壳层不得自己拼 diagnostics 文本。
  assert.ok(!main.includes('Traffic Exposure'));
  const source=readFile('cns_planner/web/js/workflow/grid_details.js');
  assert.match(source,/escapeHtml\(gridCellSummary\(/);
  assert.match(source,/escapeHtml\(gridCellDetails\(/);
});
