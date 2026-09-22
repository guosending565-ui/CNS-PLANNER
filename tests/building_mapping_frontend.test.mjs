/**
 * 建筑环境映射 / 人口映射 / 建筑轮廓图层的前端契约测试。
 *
 * 覆盖 Phase 3.5 人工验收发现的两类显示问题与一条新能力：
 *
 * 1. **人口映射不再只显示 value_status**：统一载体 ``population_mapping`` 的覆盖统计
 *    （coverage_ratio / full_cells / partial_cells / missing_cells）必须出现在卡片上，
 *    ``missing_data`` 不再吞掉真实覆盖情况；
 * 2. **建筑环境映射统一载体**：``environment_mapping`` 的三态
 *    （passed / partial / unsupported）与依据必须原样展示，层级无关的精确聚合要显式说明；
 * 3. **建筑轮廓图层**：默认关闭、低缩放不加载全部建筑、高缩放才按 bbox 请求
 *    ``/api/building-footprints``，并且只做只读绘制（不写任何业务状态）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {mappingStatusCards,buildingEnvironmentSummary} from '../cns_planner/web/js/workflow/step02_workspace.js';
import {
  BUILDING_FOOTPRINT_LOD,buildingFootprintLevel,buildingFootprintLimit,
  buildingFootprintTolerance,createBuildingFootprintLayer,
} from '../cns_planner/web/js/map/building_footprint_layer.js';

/** 从渲染出的 HTML 里取出全部 .metric-card（与 workbench.metricCard 的结构一致）。 */
function cardsOf(html){
  const cards=[];
  const pattern=/<div class="metric-card"><span class="metric-label">(.*?)<\/span><span class="metric-value">(.*?)<\/span>(?:<span class="metric-note">(.*?)<\/span>)?<\/div>/g;
  let match;
  while((match=pattern.exec(html)))cards.push({label:match[1],value:match[2],note:match[3]??''});
  return cards;
}

// ============================================================================
// 任务 2：PopulationMappingResult 的覆盖统计展示
// ============================================================================

test('population mapping shows coverage statistics instead of a bare missing_data',()=>{
  const cards=cardsOf(mappingStatusCards({grid_attributes:{population:{
    status:'missing_data',value_status:'missing_data',
    population_mapping:{
      status:'partial',total_cells:10,covered_cells:6,unresolved_cells:4,
      full_cells:4,partial_cells:2,missing_cells:3,outside_cells:1,coverage_ratio:0.6,
    },
  }}}));
  const population=cards.find(card=>card.label==='人口映射');
  assert.ok(population,'the population mapping card must exist');
  assert.equal(population.value,'部分覆盖','the unified mapping carrier wins over a bare value_status');
  // BUG-POP-001：统一载体下六类计数与未判定数必须全部给出（数字可闭合）。
  assert.equal(
    population.note,
    '已覆盖 6 / 10 格（60%） · full 4 / partial 2 / nodata_only 0 / confirmed_zero 0 '
    +'/ missing 3 / outside 1 · 未判定 4',
    'the coverage statistics are shown verbatim'
  );
});

test('population mapping falls back to the legacy counts for old snapshots',()=>{
  const cards=cardsOf(mappingStatusCards({grid_attributes:{population:{
    status:'passed',value_status:'passed',full_count:3,partial_count:1,missing_count:0,outside_count:0,
  }}}));
  const population=cards.find(card=>card.label==='人口映射');
  assert.equal(population.value,'通过');
  assert.equal(population.note,'已覆盖 4 / 4 格（100%） · full 3 / partial 1 / missing 0 / outside 0');
});

// ============================================================================
// 任务 1：BuildingEnvironmentMappingResult 的展示
// ============================================================================

test('building mapping card renders the unified environment_mapping result',()=>{
  const cards=cardsOf(mappingStatusCards({grid_attributes:{buildings:{
    status:'passed',count:3528,covered_count:3528,
    environment_mapping:{
      status:'passed',total_cells:3528,covered_cells:3528,unresolved_cells:0,
      coverage_ratio:1,grid_level:7,level_aligned:false,level_independent_facts:true,
      participates_in_planner:true,
      mapping_basis:'exact_footprint_intersection_and_centroid_allocation',
    },
  }}}));
  const buildings=cards.find(card=>card.label==='建筑环境映射');
  assert.equal(buildings.value,'通过');
  assert.equal(
    buildings.note,
    '已覆盖 3528 / 3528 格（100%） · 未判定 0 格 · 原始建筑足迹精确聚合',
    'the aggregation basis is stated explicitly'
  );
});

test('building mapping card keeps partial and unsupported honest',()=>{
  const partial=cardsOf(mappingStatusCards({grid_attributes:{buildings:{
    status:'missing_data',count:10,covered_count:4,
    environment_mapping:{status:'partial',total_cells:10,covered_cells:4,unresolved_cells:6,
      coverage_ratio:0.4,mapping_basis:'l8_building_grid_fact_table'},
  }}})).find(card=>card.label==='建筑环境映射');
  assert.equal(partial.value,'部分覆盖');
  assert.match(partial.note,/未判定 6 格 · L8 建筑环境网格直接映射/);

  const unsupported=cardsOf(mappingStatusCards({grid_attributes:{buildings:{
    status:'unsupported',count:3528,covered_count:0,
    environment_mapping:{status:'unsupported',total_cells:3528,covered_cells:0,
      unresolved_cells:3528,coverage_ratio:0,mapping_basis:null},
  }}})).find(card=>card.label==='建筑环境映射');
  assert.equal(unsupported.value,'不可用');
  assert.match(unsupported.note,/未判定 3528 格 · 未记录映射依据/);
});

test('building environment chain summary explains level-independent facts',()=>{
  const flow={
    workspace:{health:{terrain_dtm:{status:'passed'},buildings:{status:'passed'},building_grid:{status:'passed'}}},
    grid_attributes:{buildings:{
      status:'passed',count:3528,covered_count:3528,
      environment_mapping:{
        status:'passed',total_cells:3528,covered_cells:3528,unresolved_cells:0,
        coverage_ratio:1,grid_level:7,level_independent_facts:true,
        mapping_basis:'exact_footprint_intersection_and_centroid_allocation',
      },
    }},
  };
  const html=buildingEnvironmentSummary(flow);
  assert.match(html,/按当前 L7 层级精确聚合原始建筑足迹/);
  assert.match(html,/未做跨层级平均\/插值/);

  const l8=buildingEnvironmentSummary({
    workspace:{health:{}},
    grid_attributes:{buildings:{
      status:'passed',count:10,covered_count:10,
      environment_mapping:{status:'passed',total_cells:10,covered_cells:10,unresolved_cells:0,
        grid_level:8,level_independent_facts:false,mapping_basis:'l8_building_grid_fact_table'},
    }},
  });
  assert.doesNotMatch(l8,/精确聚合原始建筑足迹/,'an L8 native mapping must not claim aggregation');
});

// ============================================================================
// 任务 4：建筑轮廓图层的 LOD 与按需拉取
// ============================================================================

test('building footprint LOD is graded and never loads everything when far away',()=>{
  assert.equal(buildingFootprintLevel(1000),'hidden');
  assert.equal(buildingFootprintLevel(BUILDING_FOOTPRINT_LOD.hiddenAboveRes+1),'hidden');
  assert.equal(buildingFootprintLevel(20),'coarse');
  assert.equal(buildingFootprintLevel(3),'full');
  assert.equal(buildingFootprintLevel(0),'hidden');
  assert.equal(buildingFootprintLevel(undefined),'hidden');
  assert.equal(buildingFootprintLimit('coarse'),600);
  assert.equal(buildingFootprintLimit('full'),3000);
  assert.ok(buildingFootprintLimit('coarse')<buildingFootprintLimit('full'),'低级别不加载全量建筑');
  assert.ok(buildingFootprintTolerance(20)>buildingFootprintTolerance(3),'越远轮廓越粗');
});

test('building footprint layer stays off by default and never requests data',async()=>{
  const calls=[];
  const layer=createBuildingFootprintLayer({
    api:url=>{calls.push(url);return Promise.resolve({status:'passed',count:0,features:[]});},
  });
  assert.equal(layer.state().enabled,false);
  await layer.sync({view:{res:3},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.equal(calls.length,0,'a closed layer must not call the backend');
  assert.equal(layer.statusLine(),'图层已关闭');
});

test('building footprint layer only loads at detail zoom and reports the reason otherwise',async()=>{
  const calls=[];
  const layer=createBuildingFootprintLayer({
    api:url=>{
      calls.push(url);
      return Promise.resolve({
        status:'passed',count:2,truncated:false,
        source:{path:'x.gpkg',layer:'buildings'},
        features:[{type:'Feature',geometry:{type:'Polygon',coordinates:[[[122,30],[122.01,30],[122.01,30.01],[122,30.01],[122,30]]]},properties:{id:'B1'}}],
      });
    },
  });
  layer.setEnabled(true);

  await layer.sync({view:{res:120},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.equal(calls.length,0,'低缩放级别不加载全部建筑');
  assert.equal(layer.statusLine(),'放大后显示建筑轮廓');

  await layer.sync({view:{res:4},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.equal(calls.length,1,'detail zoom loads the current view bbox');
  const url=calls[0];
  assert.match(url,/^\/api\/building-footprints\?/);
  assert.ok(
    url.includes('bbox='+encodeURIComponent('122.0000000,29.9000000,122.1000000,30.0000000')),
    'the request carries the current view bbox: '+url
  );
  assert.match(url,/limit=3000/);
  assert.match(url,/tolerance=/);
  assert.equal(layer.state().count,2);
  assert.equal(layer.statusLine(),'已显示 2 个建筑轮廓');

  // 视图没有明显移动时不会重复请求。
  await layer.sync({view:{res:4},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.equal(calls.length,1,'an unchanged view must not re-request');
});

test('building footprint layer reports backend unavailability without faking data',async()=>{
  const layer=createBuildingFootprintLayer({
    api:()=>Promise.resolve({status:'unavailable',reason:'建筑数据源不可用',features:[],count:0}),
  });
  layer.setEnabled(true);
  await layer.sync({view:{res:4},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.equal(layer.state().count,0);
  assert.equal(layer.state().error,'建筑数据源不可用');
  assert.match(layer.statusLine(),/加载失败：建筑数据源不可用/);

  const failing=createBuildingFootprintLayer({api:()=>Promise.reject(new Error('boom'))});
  failing.setEnabled(true);
  await failing.sync({view:{res:4},size:[800,600],bbox:[122,29.9,122.1,30.0]});
  assert.match(failing.statusLine(),/加载失败：boom/);
});

test('building footprint layer draws read-only outlines through the map projection',async()=>{
  const layer=createBuildingFootprintLayer({
    api:()=>Promise.resolve({
      status:'passed',count:1,truncated:false,
      features:[{type:'Feature',geometry:{type:'MultiPolygon',coordinates:[[[[122,30],[122.01,30],[122.01,30.01],[122,30.01],[122,30]]]]},properties:{id:'B1'}}],
    }),
  });
  layer.setEnabled(true);
  await layer.sync({view:{res:4},size:[800,600],bbox:[122,29.9,122.1,30.0]});

  const projected=[];
  const ctx={
    save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},closePath(){},
    fill(){},stroke(){},lineWidth:0,fillStyle:'',strokeStyle:'',
  };
  const drawn=layer.draw(ctx,coordinate=>{projected.push(coordinate);return [coordinate[0],coordinate[1]];});
  assert.equal(drawn,1);
  assert.equal(projected.length,5,'each ring vertex is projected exactly once');
  assert.deepEqual(projected[0],[122,30]);

  // 关闭图层后不再绘制，也不保留缓存。
  layer.setEnabled(false);
  assert.equal(layer.draw(ctx,()=>[0,0]),0);
  assert.equal(layer.state().count,0);
});
