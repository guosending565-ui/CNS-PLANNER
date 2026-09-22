/**
 * BUG-UI-POP-002：Step02 人口覆盖必须把「格网判定」与「映射有效面积覆盖」两个口径分开显示。
 *
 * 修复前的显示是 ``已覆盖 986 / 986 格（90%）``：C/T 是格网口径（covered/total），
 * 而括号里的 R 取自 ``population_mapping.coverage_ratio`` —— 它优先是**面积口径**
 * （``coverage_summary.source_coverage_fraction``）。两个口径被拼进同一句话，于是出现了
 * ``986/986 格（90%）`` 这种自相矛盾的显示。
 *
 * 本测试锁定：
 * 1. 两个口径分别暴露（cellRatio / areaRatio）且分别渲染；
 * 2. 旧快照缺新 ratio 时保守显示 "—"，绝不伪造百分比、绝不用另一个口径顶替；
 * 3. ``1024/1024（100%） · 映射有效面积覆盖 90%`` 这类真实组合不再产生混写。
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  mappingStatusCards,populationCoverage,populationCoverageNote,
} from '../cns_planner/web/js/workflow/step02_workspace.js';

const ROOT=new URL('../',import.meta.url);
const readFile=path=>readFileSync(new URL(path,ROOT),'utf8');

/** 后端真实场景：全部格网都有明确结论（格网判定 100%），但映射有效面积只覆盖 90%。 */
const FULL_CELLS_PARTIAL_AREA={
  status:'passed',total_cells:986,covered_cells:986,unresolved_cells:0,
  full_cells:520,partial_cells:400,nodata_only_cells:0,confirmed_zero_cells:66,
  missing_cells:0,outside_cells:0,
  coverage_ratio:0.9,cell_coverage_ratio:1.0,area_coverage_ratio:0.9,
};

function populationCard(attributes){
  const cards=mappingStatusCards({grid_attributes:{population:attributes}});
  const pattern=/<div class="metric-card"><span class="metric-label">(.*?)<\/span><span class="metric-value">(.*?)<\/span>(?:<span class="metric-note">(.*?)<\/span>)?<\/div>/g;
  let match;
  while((match=pattern.exec(cards))){
    if(match[1]==='人口映射')return {value:match[2],note:match[3]??''};
  }
  throw new Error('人口映射卡必须存在');
}

test('两种覆盖口径分别暴露，绝不共享同一个百分比',()=>{
  const counts=populationCoverage({population_mapping:FULL_CELLS_PARTIAL_AREA});
  assert.equal(counts.cellRatio,1.0,'格网判定口径来自 cell_coverage_ratio');
  assert.equal(counts.areaRatio,0.9,'面积口径来自 area_coverage_ratio');
  assert.equal(counts.ratio,0.9,'后端 coverage_ratio 仍原样保留（面积优先）');
});

test('986/986 格 + 90% 面积：分开写，绝不出现 "986/986 格（90%）"',()=>{
  const note=populationCoverageNote(
    populationCoverage({population_mapping:FULL_CELLS_PARTIAL_AREA})
  );
  assert.match(note,/格网判定 986 \/ 986（100%）/);
  assert.match(note,/映射有效面积覆盖 90%/);
  assert.doesNotMatch(note,/986 \/ 986 格（90%）/);
  assert.doesNotMatch(note,/格（90%）/);
  // 六类计数与未判定必须保留。
  assert.match(note,/full 520/);
  assert.match(note,/partial 400/);
  assert.match(note,/nodata_only 0/);
  assert.match(note,/confirmed_zero 66/);
  assert.match(note,/missing 0/);
  assert.match(note,/outside 0/);
  assert.match(note,/未判定 0/);
});

test('人口映射卡输出的 note 与人口覆盖行完全一致',()=>{
  const card=populationCard({status:'missing_data',population_mapping:FULL_CELLS_PARTIAL_AREA});
  assert.equal(card.value,'通过');
  assert.equal(
    card.note,
    populationCoverageNote(populationCoverage({
      population_mapping:FULL_CELLS_PARTIAL_AREA,
    }))
  );
});

test('缺少面积口径时显示 "—"：不伪造百分比、不用格网口径顶替',()=>{
  const missingArea={
    status:'passed',total_cells:986,covered_cells:986,unresolved_cells:0,
    full_cells:520,partial_cells:400,nodata_only_cells:0,confirmed_zero_cells:66,
    missing_cells:0,outside_cells:0,coverage_ratio:null,cell_coverage_ratio:1.0,
  };
  const counts=populationCoverage({population_mapping:missingArea});
  assert.equal(counts.areaRatio,null);
  const note=populationCoverageNote(counts);
  assert.match(note,/格网判定 986 \/ 986（100%）/);
  assert.match(note,/映射有效面积覆盖 —/);
  assert.doesNotMatch(note,/映射有效面积覆盖 100%/);
  assert.doesNotMatch(note,/映射有效面积覆盖 0%/);
});

test('缺少格网口径时用 counting 回退（covered/total），面积口径仍不伪造',()=>{
  const missingCell={
    status:'partial',total_cells:10,covered_cells:6,unresolved_cells:4,
    full_cells:3,partial_cells:2,nodata_only_cells:1,confirmed_zero_cells:0,
    missing_cells:3,outside_cells:1,coverage_ratio:0.7,
  };
  const counts=populationCoverage({population_mapping:missingCell});
  assert.equal(counts.cellRatio,0.6,'回退到 covered/total，这是格网口径的精确值');
  assert.equal(counts.areaRatio,null);
  const note=populationCoverageNote(counts);
  assert.match(note,/格网判定 6 \/ 10（60%）/);
  assert.match(note,/映射有效面积覆盖 —/);
  assert.doesNotMatch(note,/70%/,'面积口径缺失时绝不使用 coverage_ratio 顶替');
});

test('旧快照（无 population_mapping）不得伪造面积口径百分比',()=>{
  const counts=populationCoverage({
    full_count:3,partial_count:1,missing_count:0,outside_count:0,
  });
  assert.equal(counts.unified,false);
  assert.equal(counts.areaRatio,null);
  const note=populationCoverageNote(counts);
  assert.match(note,/格网判定 4 \/ 4（100%）/);
  assert.match(note,/映射有效面积覆盖 —/);
  assert.doesNotMatch(note,/映射有效面积覆盖 0%/);
  assert.doesNotMatch(note,/映射有效面积覆盖 100%/);
});

test('百分比格式化绝不把 null/undefined 渲染成 0%',()=>{
  const source=readFile('cns_planner/web/js/workflow/step02_workspace.js');
  // percentText 必须先显式处理 null/undefined（Number(null)===0 会伪造出 "0%"）。
  assert.match(source,/function percentText\(ratio\)\{[\s\S]{0,200}ratio===null\|\|ratio===undefined/);
  assert.equal(
    populationCoverageNote({
      unified:true,total:4,covered:4,unresolved:0,full:4,partial:0,
      nodataOnly:0,confirmedZero:0,missing:0,outside:0,
      cellRatio:1.0,areaRatio:null,ratio:null,closure:4,closed:true,
    }),
    '格网判定 4 / 4（100%） · 映射有效面积覆盖 — · full 4 / partial 0 '
    +'/ nodata_only 0 / confirmed_zero 0 / missing 0 / outside 0 · 未判定 0'
  );
});

test('源码不再把 coverage_ratio 写进 "X / Y 格（R%）" 这句话',()=>{
  const source=readFile('cns_planner/web/js/workflow/step02_workspace.js');
  assert.doesNotMatch(source,/已覆盖 '\+counts\.covered/);
  assert.doesNotMatch(source,/percentText\(counts\.ratio\)/);
  assert.match(source,/格网判定 '/);
  assert.match(source,/映射有效面积覆盖 '/);
});
