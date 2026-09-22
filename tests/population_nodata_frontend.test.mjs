/**
 * BUG-POP-001：Step02 「Population NoData Semantics」用户闭环的前端契约测试。
 *
 * 覆盖：
 * 1. 人口映射卡补齐 full / partial / nodata_only / confirmed_zero / missing / outside /
 *    unresolved，且**数字必须可闭合**；
 * 2. NoData 语义面板显示 status / mode / source / evidence / confirmed；
 * 3. mode 只允许 ``nodata_is_zero_population``；缺 source / evidence / confirmed 一律拒绝保存；
 * 4. 明示三条边界（来源 extent 之内 / outside_extent 永远 unknown / 零人口不是安全或适飞结论）；
 * 5. policy 变化后人口映射 stale 的提示与「仅重算人口映射」入口（只打一个 API）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

import {
  POPULATION_NODATA_MODE,mappingStatusCards,populationCoverage,populationCoverageNote,
  populationNodataModel,populationNodataPanel,populationNodataPayload,render as renderStep2,
} from '../cns_planner/web/js/workflow/step02_workspace.js';

const ROOT=new URL('../',import.meta.url);
const readFile=path=>readFileSync(new URL(path,ROOT),'utf8');

/** 从渲染出的 HTML 里取出全部 .metric-card（与 workbench.metricCard 的结构一致）。 */
function cardsOf(html){
  const cards=[];
  const pattern=/<div class="metric-card"><span class="metric-label">(.*?)<\/span><span class="metric-value">(.*?)<\/span>(?:<span class="metric-note">(.*?)<\/span>)?<\/div>/g;
  let match;
  while((match=pattern.exec(html)))cards.push({label:match[1],value:match[2],note:match[3]??''});
  return cards;
}

const MAPPING={
  status:'partial',total_cells:10,covered_cells:6,unresolved_cells:4,
  full_cells:3,partial_cells:2,nodata_only_cells:1,confirmed_zero_cells:1,
  missing_cells:2,outside_cells:1,
  // BUG-UI-POP-002：格网判定口径与映射有效面积口径是两个不同的量。
  coverage_ratio:0.45,cell_coverage_ratio:0.6,area_coverage_ratio:0.45,
};

const CONFIRMED_POLICY={
  status:'confirmed',status_reason:null,mode:POPULATION_NODATA_MODE,role:'population',
  source:'engineering_review_2026',source_id:'worldpop-r2025a-population-count',
  evidence:{product:'WorldPop Population Counts R2025A',note:'海上/无人区像元为 NoData'},
  confirmed:true,confirmed_at:'2026-02-01T00:00:00Z',
  statement:'该项目显式确认：该人口来源在其自身 extent 之内使用 NoData 像元表示「无居住人口」。',
  never_converts:['outside_extent'],
  semantics:{outside_extent_stays_unknown:true,zero_population_is_not_a_safety_verdict:true},
};

// ============================================================================
// 1. 人口映射卡的覆盖统计必须可闭合
// ============================================================================

test('population coverage closes exactly over the six declared buckets',()=>{
  const counts=populationCoverage({population_mapping:MAPPING});
  assert.deepEqual(
    [counts.full,counts.partial,counts.nodataOnly,counts.confirmedZero,counts.missing,counts.outside],
    [3,2,1,1,2,1]
  );
  assert.equal(counts.total,10);
  assert.equal(counts.closure,10,'full+partial+nodata_only+confirmed_zero+missing+outside 必须等于 total');
  assert.equal(counts.covered,counts.full+counts.partial+counts.confirmedZero);
  assert.equal(counts.unresolved,counts.total-counts.covered);
  assert.equal(counts.closed,true);
  // 两个口径必须分别暴露，绝不混写。
  assert.equal(counts.cellRatio,0.6);
  assert.equal(counts.areaRatio,0.45);
  assert.equal(
    populationCoverageNote(counts),
    '格网判定 6 / 10（60%） · 映射有效面积覆盖 45% · full 3 / partial 2 / nodata_only 1 '
    +'/ confirmed_zero 1 / missing 2 / outside 1 · 未判定 4'
  );
});

test('population card renders nodata_only, confirmed_zero and unresolved',()=>{
  const population=cardsOf(mappingStatusCards({grid_attributes:{population:{
    status:'missing_data',value_status:'missing_data',population_mapping:MAPPING,
  }}})).find(card=>card.label==='人口映射');
  assert.ok(population,'人口映射卡必须存在');
  assert.equal(population.value,'部分覆盖');
  assert.match(population.note,/nodata_only 1/);
  assert.match(population.note,/confirmed_zero 1/);
  assert.match(population.note,/未判定 4/);
});

test('legacy snapshots keep the four buckets they really own without inventing zeros',()=>{
  const counts=populationCoverage({full_count:3,partial_count:1,missing_count:0,outside_count:0});
  assert.equal(counts.unified,false);
  assert.equal(counts.nodataOnly,null);
  assert.equal(counts.confirmedZero,null);
  // 旧快照没有面积口径：保持 null（显示 "—"），绝不用格网口径顶替。
  assert.equal(counts.cellRatio,1);
  assert.equal(counts.areaRatio,null);
  assert.equal(
    populationCoverageNote(counts),
    '格网判定 4 / 4（100%） · 映射有效面积覆盖 — · full 3 / partial 1 / missing 0 / outside 0'
  );
});

// ============================================================================
// 2. 面板显示 status / mode / source / evidence / confirmed
// ============================================================================

test('policy model exposes status, mode, source, evidence and confirmed verbatim',()=>{
  const model=populationNodataModel({
    population_nodata_policy:CONFIRMED_POLICY,grid_attributes:{population:{status:'stale'}},
  });
  assert.equal(model.status,'confirmed');
  assert.equal(model.mode,POPULATION_NODATA_MODE);
  assert.equal(model.source,'engineering_review_2026');
  assert.equal(model.sourceId,'worldpop-r2025a-population-count');
  assert.deepEqual(model.evidenceKeys,['product','note']);
  assert.equal(model.confirmed,true);
  assert.equal(model.mappingStale,true);
  assert.equal(model.mappingStatus,'stale');
  assert.deepEqual(model.neverConverts,['outside_extent']);

  const unconfigured=populationNodataModel({});
  assert.equal(unconfigured.status,'not_configured');
  assert.equal(unconfigured.mode,null);
  assert.equal(unconfigured.source,null);
  assert.equal(unconfigured.evidence,null);
  assert.equal(unconfigured.confirmed,false,'绝不凭空伪造确认');
});

test('the panel shows every policy field and states the three NoData boundaries',()=>{
  const html=populationNodataPanel({
    grid:{status:'passed'},population_nodata_policy:CONFIRMED_POLICY,
    grid_attributes:{population:{status:'passed',population_mapping:MAPPING}},
  });
  for(const id of ['populationNodataMode','populationNodataSource','populationNodataSourceId',
    'populationNodataEvidence','populationNodataConfirmed','savePopulationNodata',
    'revokePopulationNodata','remapPopulation']){
    assert.match(html,new RegExp('id="'+id+'"'),`缺少控件 ${id}`);
  }
  assert.match(html,/Population NoData 语义/);
  assert.match(html,/mode nodata_is_zero_population · confirmed 是/);
  assert.match(html,/source engineering_review_2026 · worldpop-r2025a-population-count/);
  assert.match(html,/evidence product、note/);

  // mode 只允许唯一取值：select 里只有一个真实选项。
  assert.equal((html.match(/value="nodata_is_zero_population"/g)||[]).length,1);
  assert.match(html,/请选择（不猜）/);

  // 三条边界必须逐条写明。
  assert.match(html,/只作用于人口来源 extent <b>之内<\/b>的 NoData/);
  assert.match(html,/<b>outside_extent 永远是 unknown<\/b>/);
  assert.match(html,/<b>零人口不是安全结论、也不是适飞结论<\/b>/);
  assert.match(html,/可以随时撤回/);
});

test('the NoData panel escapes every user supplied policy field',()=>{
  const html=populationNodataPanel({
    grid:{status:'passed'},
    population_nodata_policy:{
      ...CONFIRMED_POLICY,
      source:'"><img src=x onerror=alert(1)>',
      source_id:'<script>bad()</script>',
      evidence:{'<b>k</b>':'v'},
    },
    grid_attributes:{population:{}},
  });
  assert.doesNotMatch(html,/<img/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html,/&lt;b&gt;k&lt;\/b&gt;/);
});

test('a stale population mapping shows the reason and the population-only remap entry',()=>{
  const stale=populationNodataPanel({
    grid:{status:'passed'},population_nodata_policy:CONFIRMED_POLICY,
    grid_attributes:{population:{status:'stale'}},
  });
  assert.match(stale,/人口映射当前为 <b>stale<\/b>/);
  assert.match(stale,/仅重算人口映射/);
  assert.doesNotMatch(stale,/id="remapPopulation" disabled/);

  const fresh=populationNodataPanel({
    grid:{status:'passed'},population_nodata_policy:CONFIRMED_POLICY,
    grid_attributes:{population:{status:'passed'}},
  });
  assert.doesNotMatch(fresh,/人口映射当前为/);

  // 没有网格时不得假装能重算。
  const noGrid=populationNodataPanel({grid:{status:'not_calculated'},grid_attributes:{}});
  assert.match(noGrid,/id="remapPopulation" disabled/);
});

// ============================================================================
// 3. 保存前校验：source + evidence + confirmed 缺一不可
// ============================================================================

test('payload validation requires the single allowed mode plus source and confirmed',()=>{
  const base={mode:POPULATION_NODATA_MODE,source:'review',evidence:'{"product":"WorldPop"}',confirmed:true};
  assert.deepEqual(populationNodataPayload(base).population_nodata_policy,{
    mode:POPULATION_NODATA_MODE,role:'population',source:'review',source_id:null,
    evidence:{product:'WorldPop'},confirmed:true,
  });

  for(const [label,broken] of Object.entries({
    'mode 为空':{...base,mode:''},
    'mode 非法':{...base,mode:'nodata_is_water'},
    '缺 source':{...base,source:'   '},
    '未勾选 confirmed':{...base,confirmed:false},
    'evidence 为空':{...base,evidence:''},
    'evidence 非对象':{...base,evidence:'"text"'},
    'evidence 空对象':{...base,evidence:'{}'},
    'evidence 非法 JSON':{...base,evidence:'not json'},
  })){
    assert.throws(()=>populationNodataPayload(broken),Error,label);
  }
});

test('step 2 binds the policy save, the revoke and the population-only remap',async()=>{
  const elements=new Map();
  const registered=new Map();
  const fake=()=>({onchange:null,onclick:null,value:'',checked:false,disabled:false,dataset:{}});
  const calls=[];
  const c={
    $:id=>{if(!elements.has(id))elements.set(id,fake());return elements.get(id);},
    actionButton:(id,handler)=>registered.set(id,handler),
    resourceAction:async(path,payload)=>{calls.push({path,payload});return {path,payload};},
    remapPopulation:async()=>{calls.push({path:'remap'});return {remapped:true};},
    setGridOutline(){},setGridTheme(){},startWorkspace(){},setStep(){},panelError(){},
    clearWorkspace:async()=>{},saveWorkspace:async()=>{},
  };
  const previousDocument=globalThis.document;
  globalThis.document={querySelectorAll:()=>[],getElementById:()=>null};
  try{
    const {bind}=await import('../cns_planner/web/js/workflow/step02_workspace.js');
    bind(c);
  }finally{globalThis.document=previousDocument;}

  for(const id of ['savePopulationNodata','revokePopulationNodata','remapPopulation']){
    assert.ok(registered.has(id),`Step02 必须绑定 ${id}`);
  }
  // 缺字段时同步拒绝：绝不发出请求。
  assert.throws(()=>registered.get('savePopulationNodata')(),Error);
  assert.equal(calls.length,0);

  elements.get('populationNodataMode').value=POPULATION_NODATA_MODE;
  elements.get('populationNodataSource').value='engineering_review_2026';
  elements.get('populationNodataEvidence').value='{"product":"WorldPop"}';
  elements.get('populationNodataConfirmed').checked=true;
  await registered.get('savePopulationNodata')();
  assert.equal(calls[0].path,'/api/population-nodata-policy');
  assert.deepEqual(calls[0].payload.population_nodata_policy.evidence,{product:'WorldPop'});

  await registered.get('revokePopulationNodata')();
  assert.deepEqual(calls[1].payload,{population_nodata_policy:{confirmed:false}});

  await registered.get('remapPopulation')();
  assert.equal(calls[2].path,'remap','重算按钮只允许走 population-only remap');
});

// ============================================================================
// 4. 页面与壳层接线
// ============================================================================

test('step 2 renders the NoData panel inside the data-mapping segment',()=>{
  const flow={
    project:{name:'P'},steps:{'2':true},workspace:null,grid:{status:'not_calculated'},
    grid_attributes:{population:{status:'stale'},terrain:{},buildings:{},airspace:{}},
    grid_risk:{},spatial_3d:{altitude_layers:[]},population_nodata_policy:CONFIRMED_POLICY,
  };
  const html=renderStep2({
    flow,draftWorkspace:null,gridDisplay:{outline:false,theme:'none'},
    populationDisplayLabel:()=>'',formatNumber:String,
  });
  assert.match(html,/Population NoData 语义/);
  assert.match(html,/id="savePopulationNodata"/);
  assert.match(html,/outside_extent 永远是 unknown/);
  const cardIndex=html.indexOf('人口映射');
  const panelIndex=html.indexOf('id="savePopulationNodata"');
  const themeIndex=html.indexOf('专题模式');
  assert.ok(cardIndex>=0&&panelIndex>cardIndex,'面板必须紧邻人口映射卡之后');
  assert.ok(themeIndex<0||panelIndex<themeIndex,'面板必须挂在数据映射段而不是专题浏览段');
});

test('the shell remaps population through the dedicated endpoint only',()=>{
  const main=readFile('cns_planner/web/js/main.js');
  assert.match(main,/resourceAction\('\/api\/workspace\/grid\/population\/remap',\{\}\)/);
  assert.match(main,/remapPopulation:async\(\)=>\{/);
  // 重算之后必须重新拉取逐 cell 的 grid_attributes（通用快照只带摘要）。
  assert.match(main,/remapPopulation:async\(\)=>[\s\S]{0,240}await syncGridApis\(\)/);
  assert.match(main,/remapPopulation:async\(\)=>[\s\S]{0,400}renderWorkflow\(\);paint\(\);return data;\}/);
  // 入口文件必须保持精简（tests/test_architecture.py）。
  assert.ok(main.split('\n').length<=450,'main.js 必须保持 450 行以内的轻入口');

  const step02=readFile('cns_planner/web/js/workflow/step02_workspace.js');
  assert.match(step02,/c\.remapPopulation\(\)/);
  assert.doesNotMatch(step02,/\/api\/workspace\/grid\/population\/remap/,'Step02 不得自己拼 URL');
  assert.doesNotMatch(step02,/set_workspace|workspace',\{bbox/,'重算人口不得重跑工作区');
});
