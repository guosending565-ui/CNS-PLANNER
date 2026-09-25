/**
 * Layered Route Map Evidence V1 前端回归测试。
 *
 * 目标：锁定"只做前端地图表达与临时联动"这一层契约，而不是重新验证后端算法：
 *  - 图层拆分：layeredFeasibilityLayer 只画 selected-layer coarse feasibility mask，
 *    layeredCandidateLayer 只画 current LayeredRouteCandidate，两者可独立开关；
 *  - 几何权威性：candidate.path（Theta* V2 真实起终点 + any-angle）优先，
 *    **只有**旧记录没有 candidate.path 时才退回 grid_path → grid cell center，
 *    且必须显式返回 geometrySource='legacy_grid_path_fallback'（绝不静默 fallback）；
 *  - stale / 非 current 候选永不绘制；
 *  - RouteRiskProfile → 地图联动只用后端 start_coordinate / end_coordinate，
 *    high-risk interval 严格按 segment_ids 拼路径；
 *  - 整个前端不存在 distance → coordinate 插值逻辑；
 *  - leave / blur 清除 highlight；highlight 不改 state / zoom / layer / LOD；
 *  - Validation 不制造 interval geometry（当前 validation interval 没有权威 geometry）；
 *  - 现有 frontend / map / RRP / validation-adoption / workbench 测试不回归。
 *
 * 本文件自带一个极小的 DOM 桩，因此可以独立运行：
 *   node tests/layered_route_map_evidence.test.mjs
 */
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

import {
  LAYERED_CANDIDATE_COLORS,LAYERED_CANDIDATE_GEOMETRY_SOURCE,LAYERED_CANDIDATE_SEMANTICS,
  LAYERED_CANDIDATE_STYLE,
  activeLayeredCandidates,candidateMapGeometry,candidatePathIsAuthoritative,
  currentLayeredCandidateById,currentLayeredRouteCandidate,drawLayeredCandidateOverlay,
  isDrawableCoordinate,layeredCandidateOverlayModel,legacyGridPathFallback,
} from '../cns_planner/web/js/map/layered_candidate_overlay.js';
import {drawRouteEvidenceHighlight} from '../cns_planner/web/js/map/display_layers.js';
import {LAYERED_FEASIBILITY_COLORS,drawLayeredFeasibilityOverlay} from '../cns_planner/web/js/map/layered_feasibility_overlay.js';
import {
  LAYERED_CANDIDATE_LABEL,layeredCandidateLegendModel,renderLayeredCandidateLegend,
  updateLayeredLegends,
} from '../cns_planner/web/js/workflow/layered_legend.js';
import {ROUTE_STYLES,LOD_THRESHOLDS,displayStyle} from '../cns_planner/web/js/map/lod.js';
import {
  bindRouteRiskProfileMapLinkage,routeRiskDomainSvg,routeRiskIntervalHighlight,
  routeRiskIntervalMapPath,routeRiskSegmentHighlight,routeRiskSegmentMapPath,
} from '../cns_planner/web/js/workflow/route_risk_profile.js';

const read=relative=>readFileSync(new URL('../cns_planner/web/'+relative,import.meta.url),'utf8');

const CANDIDATE_OVERLAY_SOURCE=read('js/map/layered_candidate_overlay.js');
const FEASIBILITY_OVERLAY_SOURCE=read('js/map/layered_feasibility_overlay.js');
const DISPLAY_LAYERS_SOURCE=read('js/map/display_layers.js');
const MAIN_SOURCE=read('js/main.js');
const RRP_SOURCE=read('js/workflow/route_risk_profile.js');
const VALIDATION_SOURCE=read('js/workflow/layered_route_validation.js');
const LOD_SOURCE=read('js/map/lod.js');
const INDEX_SOURCE=read('index.html');

// ---- fixtures -----------------------------------------------------------------

/** Theta* V2 候选：真实起终点 + any-angle 折线（**不是** grid cell center 序列）。 */
const THETA_V2_PATH=[[122.0,30.0],[122.0009,30.0021],[122.0031,30.0014],[122.01,30.01]];

function candidateFixture(overrides={}){
  return {
    candidate_id:'LRC-R0001-L8-LOW-1',route_id:'R0001',altitude_layer_id:'L8-LOW',
    lane_key:'R0001@L8-LOW',status:'candidate',current_applicability:'current',
    algorithm_id:'layered_risk_aware_theta_star_v2',
    path:THETA_V2_PATH.map(point=>[...point]),
    grid_path:['A','B','C'],
    operational_route:false,cns_assessed:false,continuous_validation_required:true,
    ...overrides,
  };
}

function layeredFlow(overrides={}){
  const items=overrides.items||[candidateFixture()];
  return {
    layered_route_planning_request:{status:'confirmed',confirmed:true,
      scenario_route_id:'R0001',altitude_layer_id:'L8-LOW'},
    layered_route_candidates:{
      status:'passed',count:items.length,active_candidate_id:overrides.activeCandidateId===undefined
        ?(items[0]||{}).candidate_id||null:overrides.activeCandidateId,
      items,
      masks:{'R0001@L8-LOW':{status:'passed',altitude_layer_id:'L8-LOW',current_applicability:'current',
        counts:{feasible:2,blocked:1,unknown:0},
        cells:{
          A:{grid_id:'A',status:'feasible'},B:{grid_id:'B',status:'blocked'},C:{grid_id:'C',status:'feasible'},
        }}},
    },
    grid:layeredGrid(),
  };
}

function layeredGrid(){
  return {cells:[
    {grid_id:'A',bbox:[122.0,30.0,122.01,30.01],center:[122.005,30.005]},
    {grid_id:'B',bbox:[122.01,30.0,122.02,30.01],center:[122.015,30.005]},
    {grid_id:'C',bbox:[122.02,30.0,122.03,30.01],center:[122.025,30.005]},
  ]};
}

/** RRP profile：segment 带后端给出的 start/end_coordinate（几何权威来源）。
 *  三段首尾相接：S0000 → S0001 → S0002，因此可以验证"按 segment_ids 顺序拼接"。 */
const SEGMENT_COORDINATES=[
  {segment_id:'RRP-S0000',start:[122.000,30.0],end:[122.001,30.0]},
  {segment_id:'RRP-S0001',start:[122.001,30.0],end:[122.002,30.0]},
  {segment_id:'RRP-S0002',start:[122.002,30.0],end:[122.003,30.0]},
];

function rrpSegment(index,overrides={}){
  const coordinates=SEGMENT_COORDINATES[index];
  return {
    segment_id:coordinates.segment_id,index,
    start_cumulative_distance_m:index*550,end_cumulative_distance_m:(index+1)*550,length_m:550,
    start_coordinate:coordinates.start,end_coordinate:coordinates.end,
    start_index_grid_id:'G'+index,end_index_grid_id:'G'+(index+1),
    domains:{ground:{resolved:true,mean_index:0.9,classification:{status:'passed',level:'high'}}},
    ...overrides,
  };
}

function rrpProfile(overrides={}){
  return {
    profile_id:'RRP-LRC-1-abcdef0123456789',artifact_type:'layered_route_candidate',
    status:'passed',current_applicability:'current',
    candidate:{candidate_id:'LRC-R0001-L8-LOW-1',status:'candidate',route_id:'R0001',
      altitude_layer_id:'L8-LOW',current_applicability:'current'},
    route_length_m:1650,
    segments:[rrpSegment(0),rrpSegment(1),rrpSegment(2)],
    domains:{ground:{
      status:'resolved',
      classification:{status:'passed',level:'high',thresholds:{medium_min:0.3,high_min:0.8}},
      high_risk:{status:'available',interval_count:1,intervals:[{
        interval_id:'ground-HR-0001',domain_id:'ground',
        start_distance_m:0,end_distance_m:1100,length_m:1100,
        // 刻意让 distance 与坐标顺序不一致：距离区间覆盖 segment 0..1，
        // 但 segment_ids 的顺序才是几何权威（1,0 → 反向拼接）。
        segment_ids:['RRP-S0001','RRP-S0000'],
        cell_ids:['G0','G1'],
      }]},
    }},
    ...overrides,
  };
}

function rrpFlow(profile=rrpProfile()){
  return {route_risk_profiles:{status:'passed',count:1,items:[profile]}};
}

// ---- 1. 图层拆分 ---------------------------------------------------------------

test('the candidate route layer is a separate switch from the coarse feasibility mask',()=>{
  // index.html：两个图层各自一个 checkbox，名称与职责都不混用
  assert.match(INDEX_SOURCE,/id="layeredFeasibilityLayer"/);
  assert.match(INDEX_SOURCE,/id="layeredCandidateLayer"/);
  // B4X：图层名称改为纯中文业务语言（"候选航路（当前规划结果）"）；
  // 旧版的研究对照候选已另行标注为「旧版试算航路（研究对照）」。
  assert.match(INDEX_SOURCE,/候选航路（当前规划结果）/);
  assert.match(INDEX_SOURCE,/coarse|粗判/);
  // main.js：两个 id 都进入统一 LAYER_IDS（因此可独立开关、独立持久化到抽屉）
  const layerIds=/const LAYER_IDS=\[([^\]]*)\]/.exec(MAIN_SOURCE);
  assert.ok(layerIds,'LAYER_IDS must stay a single literal list');
  assert.match(layerIds[1],/'layeredFeasibilityLayer'/);
  assert.match(layerIds[1],/'layeredCandidateLayer'/);
  // display_layers：两个图层分别由各自的开关与各自的 overlay 模块负责。
  // 图层 key 必须就是 checkbox id 本身（与 LAYER_IDS / layerSwitches 同一套命名）。
  assert.match(DISPLAY_LAYERS_SOURCE,/if\(layers\.layeredFeasibilityLayer\)\{/);
  assert.match(DISPLAY_LAYERS_SOURCE,/if\(layers\.layeredCandidateLayer!==false\)\{/);
  assert.match(DISPLAY_LAYERS_SOURCE,/drawLayeredFeasibilityOverlay/);
  assert.match(DISPLAY_LAYERS_SOURCE,/drawLayeredCandidateOverlay/);
  // feasibility overlay 源码里不再有任何 candidate path 绘制
  assert.doesNotMatch(FEASIBILITY_OVERLAY_SOURCE,/grid_path|currentLayeredCandidate\(flow\)\s*;[\s\S]{0,400}stroke/,
    'the feasibility overlay must not draw a candidate route');
  assert.doesNotMatch(FEASIBILITY_OVERLAY_SOURCE,/strokeStyle/);
});

// ---- 2. candidate.path 优先 grid_path -----------------------------------------

test('candidate.path is authoritative and grid_path is only a labelled legacy fallback',()=>{
  const candidate=candidateFixture();
  const geometry=candidateMapGeometry(candidate,{cells:layeredGrid().cells});
  assert.equal(geometry.geometrySource,LAYERED_CANDIDATE_GEOMETRY_SOURCE);
  assert.equal(geometry.legacyGridPathFallback,false);
  assert.equal(geometry.path.length,THETA_V2_PATH.length);
  // 权威几何原样透传：不做简化、不做偏移、不做四舍五入
  assert.deepEqual(geometry.path,THETA_V2_PATH);
  assert.equal(candidatePathIsAuthoritative(candidate.path),true);
  assert.equal(isDrawableCoordinate([122,30]),true);
  assert.equal(isDrawableCoordinate([122]),false);
  assert.equal(isDrawableCoordinate(['122',30]),false);

  // 旧记录：没有 candidate.path → 显式 legacy fallback，绝不静默
  const legacy=candidateFixture({path:[]});
  const fallback=candidateMapGeometry(legacy,{cells:layeredGrid().cells});
  assert.equal(fallback.geometrySource,'legacy_grid_path_fallback');
  assert.equal(fallback.legacyGridPathFallback,true);
  assert.equal(fallback.geometrySource,LAYERED_CANDIDATE_SEMANTICS.legacyFallbackSource);
  assert.deepEqual(fallback.path,[[122.005,30.005],[122.015,30.005],[122.025,30.005]]);
  assert.deepEqual(fallback.gridIds,['A','B','C']);
  // 连 grid_path 都不可用时返回 null：宁可什么都不画
  assert.equal(candidateMapGeometry(candidateFixture({path:[],grid_path:[]})),null);
  assert.equal(legacyGridPathFallback(candidateFixture({path:[],grid_path:['A']}),{cells:layeredGrid().cells}),null);
  // 只有 1 个点的 path 也不构成权威几何（至少 2 点）→ 退回带标注的 legacy fallback
  const single=candidateMapGeometry(candidateFixture({path:[[122,30]]}),{cells:layeredGrid().cells});
  assert.equal(single.geometrySource,LAYERED_CANDIDATE_SEMANTICS.legacyFallbackSource);
  assert.equal(single.legacyGridPathFallback,true);
  assert.equal(candidatePathIsAuthoritative([[122,30]]),false);
  assert.equal(candidatePathIsAuthoritative(THETA_V2_PATH),true);
});

// ---- 3. Theta* 真实起终点保持 --------------------------------------------------

test('the theta star candidate keeps its real start and end points and any-angle geometry',()=>{
  const flow=layeredFlow();
  const candidate=currentLayeredRouteCandidate(flow);
  const geometry=candidateMapGeometry(candidate,{cells:flow.grid.cells});
  assert.deepEqual(geometry.path[0],[122.0,30.0]);
  assert.deepEqual(geometry.path[geometry.path.length-1],[122.01,30.01]);
  // 起终点与 grid cell center（122.005/122.025）不同：几何不是网格中心序列
  const centers=(flow.grid.cells||[]).map(cell=>cell.center);
  assert.notDeepEqual(geometry.path[0],centers[0]);
  assert.notDeepEqual(geometry.path[geometry.path.length-1],centers[centers.length-1]);
  assert.equal(candidate.algorithm_id,'layered_risk_aware_theta_star_v2');
});

// ---- 4. BUG-MAP-ROUTE-END-001：端点必须精确，且不得出现"越过端点再折回" ----------

test('a candidate whose exact endpoint is off the cell centre still terminates at the marker',()=>{
  // 真实现象：起点/终点 marker（例如 N004）看起来落在一条连续线中间，线在 marker 之后
  // 还多出一小截。取证结论（见 docs 与 python 侧回归）：candidate.path[0] / [-1] **精确
  // 等于** scenario 起终点（端点误差 0），多出来的一截来自 Theta* V2 的
  // "exact start → source cell centre → chain"，即精确端点先回到本格中心再出发。
  //
  // 这里锁定两点：
  //  1. 地图绘制必须使用 candidate.path 的**原样**几何（权威），不得裁剪、不得删点；
  //  2. 端点坐标**精确等于** scenario 起终点 —— 所以"线越过 marker 继续"不可能来自
  //     端点不精确，只可能来自 source/target cell centre 这一跳（该跳属于搜索账本，
  //     前端不做任何显示期裁剪）。
  const centre=[122.005,30.005];
  const exactStart=[122.0099,30.005];      // 精确起点在 source cell 的**东**边缘
  const exactEnd=[122.015,30.01];
  const path=[exactStart,centre,[122.012,30.008],exactEnd];
  const flow={
    layered_route_planning_request:{scenario_route_id:'R-END',altitude_layer_id:'L8-LOW'},
    scenario_routes:[{route_id:'R-END',path:[exactStart,exactEnd]}],
    grid:{level:8,cells:[
      {grid_id:'S',level:8,bbox:[122.0,30.0,122.01,30.01],center:centre},
      {grid_id:'T',level:8,bbox:[122.01,30.0,122.02,30.01],center:[122.015,30.005]},
    ]},
    layered_route_candidates:{active_candidate_id:'C-END',items:[{
      candidate_id:'C-END',status:'candidate',current_applicability:'current',
      route_id:'R-END',altitude_layer_id:'L8-LOW',lane_key:'R-END@L8-LOW',
      algorithm_id:'layered_risk_aware_theta_star_v2',path,grid_path:['S','T'],
    }]},
  };
  const geometry=candidateMapGeometry(currentLayeredRouteCandidate(flow),{cells:flow.grid.cells});
  // 权威几何原样透传（没有任何显示期裁剪 / 删点 / 跳过 connector）
  assert.deepEqual(geometry.path,path);
  assert.equal(geometry.geometrySource,LAYERED_CANDIDATE_GEOMETRY_SOURCE);
  assert.equal(geometry.legacyGridPathFallback,false);
  // 端点精确性：起终点就是 scenario 起终点，误差为 0
  assert.deepEqual(geometry.path[0],exactStart);
  assert.deepEqual(geometry.path[geometry.path.length-1],exactEnd);
  // 并显式确认第二点确实是 source cell centre（这正是"多出一截"的来源）
  assert.deepEqual(geometry.path[1],centre);
  // 前端不得存在任何"按 marker 裁线"的逻辑
  const source=read('js/map/layered_candidate_overlay.js');
  assert.doesNotMatch(source,/slice\(|pop\(\)|clip|trimEnd|dropLast/,
    'candidate overlay 绝不允许在显示层偷偷裁线');
});

// ---- 5. stale candidate 不绘制 -------------------------------------------------

test('a stale or inactive candidate is never drawn as the current planning result',()=>{
  const stale=layeredFlow({items:[candidateFixture({current_applicability:'stale'})]});
  assert.deepEqual(activeLayeredCandidates(stale),[]);
  assert.equal(currentLayeredRouteCandidate(stale),null);
  assert.equal(layeredCandidateOverlayModel(stale).geometry,null);
  assert.equal(drawLayeredCandidateOverlay({ctx:recordingContext(),screenPoint:p=>p,flow:stale}).drawn,false);

  // status 不是 candidate（blocked / not_calculated）同样不画
  for(const status of ['blocked','not_calculated','failed']){
    const blocked=layeredFlow({items:[candidateFixture({status})]});
    assert.equal(currentLayeredRouteCandidate(blocked),null,`status=${status} must not be drawn`);
  }
  // active_candidate_id 指向别的候选时，不画"当前"候选
  const other=layeredFlow({activeCandidateId:'LRC-OTHER'});
  assert.equal(currentLayeredRouteCandidate(other),null);
  // 切换到别的 lane 时，当前 lane 不再有候选
  const otherLane=layeredFlow();
  otherLane.layered_route_planning_request={...otherLane.layered_route_planning_request,altitude_layer_id:'L8-HIGH'};
  assert.equal(currentLayeredRouteCandidate(otherLane),null);
  assert.equal(currentLayeredRouteCandidate(otherLane,{laneRequired:false})?.candidate_id,
    'LRC-R0001-L8-LOW-1','laneRequired=false only relaxes the lane filter');
});

// ---- 5. candidate 层绘制与 LOD -------------------------------------------------

test('the candidate overlay draws the current path under the existing LOD styles',()=>{
  const flow=layeredFlow();
  const ctx=recordingContext();
  const result=drawLayeredCandidateOverlay({
    ctx,screenPoint:point=>[point[0],point[1]],flow,
    style:{width:ROUTE_STYLES.detail.operationalWidth+0.4,alpha:ROUTE_STYLES.detail.operationalAlpha},
  });
  assert.equal(result.drawn,true);
  assert.equal(result.points,THETA_V2_PATH.length);
  assert.equal(result.geometrySource,LAYERED_CANDIDATE_GEOMETRY_SOURCE);
  assert.equal(result.legacyGridPathFallback,false);
  assert.deepEqual(ctx.strokes,[{color:LAYERED_CANDIDATE_COLORS.current,width:4}]);
  // 三条 LOD 档都允许显示 current candidate，且线宽/alpha 只来自既有 LOD 表
  for(const level of ['overview','medium','detail']){
    const styles=ROUTE_STYLES[level];
    const scaled=recordingContext();
    drawLayeredCandidateOverlay({ctx:scaled,screenPoint:p=>p,flow,
      style:{width:styles.operationalWidth+0.4,alpha:styles.operationalAlpha}});
    assert.equal(scaled.strokes[0].width,styles.operationalWidth+0.4,`${level} follows its own LOD width`);
  }
  // LOD 阈值本身没有被本轮的改动动过
  assert.equal(LOD_THRESHOLDS.medium,2200);
  assert.equal(LOD_THRESHOLDS.detail,420);
  assert.deepEqual(displayStyle({res:2201}).level,'overview');
  assert.deepEqual(displayStyle({res:2200}).level,'medium');
  assert.deepEqual(displayStyle({res:420}).level,'detail');
  assert.equal(LAYERED_CANDIDATE_STYLE.width,3.4);
});

// ---- 6. RRP segment 直接使用后端坐标 -------------------------------------------

test('rrp segments use the backend start and end coordinates verbatim',()=>{
  const profile=rrpProfile();
  const segment=profile.segments[0];
  assert.deepEqual(routeRiskSegmentMapPath(segment),[segment.start_coordinate,segment.end_coordinate]);
  const highlight=routeRiskSegmentHighlight(profile,'RRP-S0001');
  assert.deepEqual(highlight,{
    source:'route_risk_profile',
    profileId:'RRP-LRC-1-abcdef0123456789',
    candidateId:'LRC-R0001-L8-LOW-1',
    segmentIds:['RRP-S0001'],
    path:[[122.001,30.0],[122.002,30.0]],
  });
  // 缺少坐标时返回 null，而不是用 distance 推算
  assert.equal(routeRiskSegmentMapPath({segment_id:'X',start_cumulative_distance_m:0,
    end_cumulative_distance_m:100}),null);
  assert.equal(routeRiskSegmentHighlight(profile,'RRP-MISSING'),null);
  // 渲染层：segment 柱必须带回调需要的稳定标识与可聚焦性
  const svg=routeRiskDomainSvg(profile,'ground');
  assert.match(svg,/data-rrp-segment="RRP-S0000"/);
  assert.match(svg,/data-rrp-segment-highlight="RRP-S0000"/);
  assert.match(svg,/tabindex="0"/);
  assert.doesNotMatch(svg,/start_coordinate|end_coordinate/,'the chart geometry still only uses distance');
});

// ---- 7. high-risk 严格由 segment_ids 拼路径 ------------------------------------

test('high risk intervals are joined strictly from segment_ids in segment order',()=>{
  const profile=rrpProfile();
  const interval=profile.domains.ground.high_risk.intervals[0];
  const path=routeRiskIntervalMapPath(profile,[...interval.segment_ids]);
  // segment_ids = [S0001, S0000]：几何必须按这个顺序（1 在前），而不是按 distance
  assert.deepEqual(path,[
    [122.001,30.0],[122.002,30.0],
    [122.0,30.0],[122.001,30.0],
  ]);
  const highlight=routeRiskIntervalHighlight(profile,interval);
  assert.deepEqual(highlight.segmentIds,['RRP-S0001','RRP-S0000']);
  assert.equal(highlight.source,'route_risk_profile');
  assert.equal(highlight.candidateId,'LRC-R0001-L8-LOW-1');
  assert.deepEqual(highlight.path,path);
  // 按 segment 原顺序（S0000,S0001）拼出来的是另一条路径：证明顺序真的来自 segment_ids
  assert.deepEqual(routeRiskIntervalMapPath(profile,['RRP-S0000','RRP-S0001']),[
    [122.0,30.0],[122.001,30.0],[122.002,30.0],
  ]);
  // 未知 segment_id 被跳过；全部不可用时返回 null
  assert.deepEqual(routeRiskIntervalMapPath(profile,['RRP-S0000','RRP-UNKNOWN','RRP-S0001']),[
    [122.0,30.0],[122.001,30.0],[122.002,30.0],
  ]);
  assert.equal(routeRiskIntervalMapPath(profile,['RRP-UNKNOWN']),null);
  assert.equal(routeRiskIntervalHighlight(profile,{interval_id:'X',segment_ids:[]}),null);
  // interval 的 distance 区间即使与坐标顺序冲突，也不参与几何
  assert.equal(interval.start_distance_m,0);
  assert.equal(interval.end_distance_m,1100);
});

// ---- 8. 不存在 distance → coordinate 插值逻辑 ----------------------------------

test('no distance to coordinate interpolation exists anywhere in the frontend linkage',()=>{
  for(const [name,source] of [['layered_candidate_overlay.js',CANDIDATE_OVERLAY_SOURCE],
    ['display_layers.js',DISPLAY_LAYERS_SOURCE],['route_risk_profile.js',RRP_SOURCE],
    ['main.js',MAIN_SOURCE]]){
    assert.doesNotMatch(source,/interpolat|lerp|along_track|cumulative_distance_m\s*[-+*/][^;]*coordinate/i,
      `${name} must not interpolate coordinates from distance`);
    assert.doesNotMatch(source,/haversine|6371008\.8|pointAtMetricDistance/,
      `${name} must not recompute geometry from distance`);
  }
  // highlight 路径只可能来自坐标字段
  assert.match(RRP_SOURCE,/start_coordinate/);
  assert.match(RRP_SOURCE,/end_coordinate/);
  assert.match(RRP_SOURCE,/segment_ids/);
  // display_layers 的 highlight 只消费已经给出的 path
  assert.match(DISPLAY_LAYERS_SOURCE,/const path=highlight\?\.path/);
});

// ---- 9. Validation 边界：不造 interval geometry --------------------------------

test('validation never converts failed or unresolved intervals into map geometry',()=>{
  // validation 面板不引入任何几何模块，也不派生坐标
  assert.match(VALIDATION_SOURCE,/import \{escapeHtml,statusBadge,wbBlock,wbDisclosure\} from '\.\/common\.js';/);
  assert.doesNotMatch(VALIDATION_SOURCE,/coordinate|geometry|path\s*:|latlng|drawLine|screenPoint/,
    'the validation panel must not create interval geometry');
  assert.doesNotMatch(VALIDATION_SOURCE,/import .*(renderer|display_layers|layered_candidate_overlay)/);
  // 本轮 Validation 只允许高亮"当前被验证 candidate"的整条 candidate.path
  assert.match(MAIN_SOURCE,/routeEvidenceHighlight/);
  assert.doesNotMatch(DISPLAY_LAYERS_SOURCE,/failed_intervals|unresolved_intervals/,
    'failed/unresolved validation intervals have no authoritative geometry and stay off the map');
});

// ---- 10. highlight 不改 state / zoom / layer / LOD -----------------------------

test('the evidence highlight is pure UI: no state, no zoom, no layer, no LOD change',()=>{
  // main.js：只是一个模块作用域变量 + 只负责重绘的两个回调
  assert.match(MAIN_SOURCE,/routeEvidenceHighlight=null[,;]/);
  assert.match(MAIN_SOURCE,/routeEvidenceHighlight,\s*proposedPlanActions/,'the highlight reaches the draw call');
  const bindings=MAIN_SOURCE.slice(MAIN_SOURCE.indexOf('function stepBindings(){'),
    MAIN_SOURCE.indexOf('async function previewPlanningReport(){'));
  assert.match(bindings,/routeEvidence:\{/);
  // 只检查这两个回调自己的函数体：不写状态、不调 API、不改 zoom / layer / LOD
  const setter=/set\(value\)\{([^}]*)\}/.exec(bindings)[1];
  const clearer=/clear\(\)\{([^}]*)\}/.exec(bindings)[1];
  assert.match(setter,/^routeEvidenceHighlight=value\|\|null;paint\(\);$/);
  assert.match(clearer,/^routeEvidenceHighlight=null;paint\(\);$/);
  for(const body of [setter,clearer]){
    assert.doesNotMatch(body,/store\.set|mutate|resourceAction|api\(|zoom|layer|gridDisplay|LOD|renderWorkflow|workbench/,
      'the highlight callbacks must only repaint');
  }
  // 图层开关集合里没有为 highlight 增加任何开关
  assert.doesNotMatch(MAIN_SOURCE,/routeEvidenceHighlight[^\n]*\n[^\n]*LAYER_IDS/);
  // evidence highlight 不得进入 LOD 模块：LOD 只声明"多大、多粗、显示不显示"。
  // （MAP-TOWER-SYMBOL-V2 的 TOWER_HIGHLIGHT_* 是铁塔符号的显示尺寸常量，不是 evidence 状态。）
  assert.doesNotMatch(LOD_SOURCE,/routeEvidence|routeRisk|evidenceHighlight/);
  // highlight 只由 display_layers 在正常路线之上绘制
  const index=DISPLAY_LAYERS_SOURCE.indexOf('drawRouteEvidenceHighlight({ctx,view,screenPoint');
  const nodesIndex=DISPLAY_LAYERS_SOURCE.indexOf('// 9) 当前选择的参考对象');
  assert.ok(nodesIndex>=0&&index>nodesIndex,'the highlight must be drawn above the normal routes');
  assert.doesNotMatch(DISPLAY_LAYERS_SOURCE,/highlight[\s\S]{0,200}?displayStyle/);
});

test('drawing the highlight consumes only the given path',()=>{
  const ctx=recordingContext();
  const drawn=drawRouteEvidenceHighlight({ctx,view:{x:0,y:0,res:1},screenPoint:point=>[point[0],point[1]],
    highlight:{source:'route_risk_profile',path:[[122,30],[122.001,30]]}});
  assert.equal(drawn,2);
  assert.equal(ctx.strokes.length,2,'a white halo plus the evidence line');
  assert.equal(ctx.strokes[1].width,6);
  // 没有几何时什么都不画
  assert.equal(drawRouteEvidenceHighlight({ctx:recordingContext(),view:{},screenPoint:p=>p,highlight:null}),0);
  assert.equal(drawRouteEvidenceHighlight({ctx:recordingContext(),view:{},screenPoint:p=>p,
    highlight:{path:[[122,30]]}}),0);
});

// ---- 11. hover / focus 绑定与 leave / blur 清除 ---------------------------------

class StubNode{
  constructor(tag='div'){
    this.tagName=String(tag).toUpperCase();
    this.dataset={};this.attributes={};this.children=[];this.listeners={};
  }
  /** 与 workflow context 同形的存在性查询：渲染面板里没有这些 id。 */
  $(){return null;}
  append(...nodes){for(const node of nodes)this.children.push(node);}
  addEventListener(name,handler){(this.listeners[name]||(this.listeners[name]=[])).push(handler);}
  dispatch(name){for(const handler of this.listeners[name]||[])handler({type:name});}
  querySelectorAll(selector){return collect(this,selector);}
}

function collect(root,selector,out=[]){
  for(const child of root.children||[]){
    if(matches(child,selector))out.push(child);
    collect(child,selector,out);
  }
  return out;
}

function matches(node,selector){
  return String(selector||'').split(',').some(part=>{
    const token=part.trim();
    if(!token)return false;
    if(token.startsWith('[')&&token.endsWith(']')){
      const name=token.slice(1,-1);
      const key=name.startsWith('data-')?name.slice(5).replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()):name;
      return node.dataset[key]!==undefined||node.attributes[name]!==undefined;
    }
    return node.tagName===token.toUpperCase();
  });
}

/** 与 RRP 渲染一致的桩树：3 个 segment 柱 + 1 个 high-risk interval 行。 */
function stubRiskProfileDom(){
  const root=new StubNode('div');
  for(const segment of SEGMENT_COORDINATES){
    const node=new StubNode('rect');
    node.dataset.rrpSegment=segment.segment_id;
    node.dataset.rrpSegmentHighlight=segment.segment_id;
    root.append(node);
  }
  const interval=new StubNode('div');
  interval.dataset.rrpInterval='ground-HR-0001';
  interval.dataset.rrpIntervalDomain='ground';
  root.append(interval);
  return root;
}

test('segment and interval hover or focus set the highlight and leave or blur clear it',()=>{
  const profile=rrpProfile();
  const flow=rrpFlow(profile);
  const root=stubRiskProfileDom();
  const calls=[];
  const bound=bindRouteRiskProfileMapLinkage({
    $:()=>null,flow:()=>flow,document:root,
    routeEvidence:{set:value=>calls.push(['set',value]),clear:()=>calls.push(['clear'])},
  });
  assert.equal(bound,4,'three segment bars plus one high risk interval');

  const segment=root.children[1];
  segment.dispatch('mouseenter');
  assert.deepEqual(calls.pop(),['set',routeRiskSegmentHighlight(profile,'RRP-S0001')]);
  segment.dispatch('mouseleave');
  assert.deepEqual(calls.pop(),['clear']);
  segment.dispatch('focus');
  assert.deepEqual(calls.pop(),['set',routeRiskSegmentHighlight(profile,'RRP-S0001')]);
  segment.dispatch('blur');
  assert.deepEqual(calls.pop(),['clear']);

  const interval=root.children[3];
  interval.dispatch('mouseenter');
  const highlight=calls.pop()[1];
  assert.deepEqual(highlight.segmentIds,['RRP-S0001','RRP-S0000']);
  assert.deepEqual(highlight.path,routeRiskIntervalMapPath(profile,['RRP-S0001','RRP-S0000']));
  interval.dispatch('mouseleave');
  assert.deepEqual(calls.pop(),['clear']);
  interval.dispatch('focus');
  assert.equal(calls.pop()[0],'set');
  interval.dispatch('blur');
  assert.deepEqual(calls.pop(),['clear']);

  // 没有 current profile 时不绑定任何东西（也绝不猜一个 profile 出来）
  const stale=rrpFlow(rrpProfile({current_applicability:'stale'}));
  assert.equal(bindRouteRiskProfileMapLinkage({$:()=>null,flow:()=>stale,document:stubRiskProfileDom(),
    routeEvidence:{set:()=>{},clear:()=>{}}}),0);
  // 没有地图回调时不绑定（也绝不抛异常）
  assert.equal(bindRouteRiskProfileMapLinkage({$:()=>null,flow:()=>flow,document:stubRiskProfileDom()}),0);
  assert.equal(bindRouteRiskProfileMapLinkage(null),0);
  // 绑定只读后端字段：不重算 risk / classification
  assert.doesNotMatch(RRP_SOURCE,/mean_index\s*[-+*/]=|classification\s*=\s*\{/);
});

// ---- 12. RRP → 地图的临时高亮状态在面板里可见（纯显示） ------------------------

test('the risk profile panel shows the temporary map linkage without touching the flow',()=>{
  const flow=rrpFlow();
  const before=JSON.stringify(flow);
  // 两个图例各自独立跟随自己的开关：一个关闭不影响另一个
  const legendFlow=layeredFlow();
  const nodes={
    layeredFeasibilityLegend:{innerHTML:'',hidden:true},
    layeredCandidateLegend:{innerHTML:'',hidden:true},
    layeredFeasibilityLayer:{checked:false},
    layeredCandidateLayer:{checked:true},
  };
  const $=id=>nodes[id]||null;
  assert.equal(updateLayeredLegends({$,flow:legendFlow,formatNumber:String}),true);
  assert.equal(nodes.layeredFeasibilityLegend.hidden,true,'the mask legend follows its own switch');
  assert.match(nodes.layeredCandidateLegend.innerHTML,new RegExp(LAYERED_CANDIDATE_LABEL));
  nodes.layeredFeasibilityLayer.checked=true;
  assert.equal(updateLayeredLegends({$,flow:legendFlow,formatNumber:String}),true);
  assert.equal(nodes.layeredFeasibilityLegend.hidden,false,'both legends may be on at once');
  nodes.layeredCandidateLayer.checked=false;
  updateLayeredLegends({$,flow:legendFlow,formatNumber:String});
  assert.equal(nodes.layeredCandidateLegend.hidden,true,'turning the candidate layer off hides only its legend');

  const target={innerHTML:'',hidden:true};
  assert.equal(renderLayeredCandidateLegend(target,legendFlow),true);
  assert.match(target.innerHTML,new RegExp(LAYERED_CANDIDATE_LABEL));
  assert.match(target.innerHTML,/candidate\.path（权威几何）/);
  assert.match(target.innerHTML,/legacy_grid_path_fallback|legacy fallback/);
  assert.equal(target.hidden,false);
  const model=layeredCandidateLegendModel(layeredFlow());
  assert.equal(model.candidateId,'LRC-R0001-L8-LOW-1');
  assert.equal(model.geometrySource,'candidate_path');
  assert.equal(model.legacyGridPathFallback,false);
  // 旧记录：图例必须显式标注 legacy fallback
  const legacyModel=layeredCandidateLegendModel(layeredFlow({items:[candidateFixture({path:[]})]}));
  assert.equal(legacyModel.geometrySource,'legacy_grid_path_fallback');
  assert.equal(legacyModel.legacyGridPathFallback,true);
  assert.equal(JSON.stringify(flow),before,'reading the map linkage must not mutate the snapshot');
  assert.equal(currentLayeredCandidateById(flow,'LRC-R0001-L8-LOW-1'),null,
    'a RouteRiskProfile snapshot is not a candidate collection');
});

// ---- 13. 地图视觉层级 -----------------------------------------------------------

test('candidate outranks scenario and reference while the highlight outranks everything',()=>{
  const bindings=MAIN_SOURCE.slice(MAIN_SOURCE.indexOf('function stepBindings(){'),
    MAIN_SOURCE.indexOf('async function previewPlanningReport(){'));
  // candidate 在 scenario / operational 之后绘制（因此压在上面），且线宽更重
  const scenarioIndex=DISPLAY_LAYERS_SOURCE.indexOf('for(const route of flow.scenario_routes||[])');
  const candidateIndex=DISPLAY_LAYERS_SOURCE.indexOf('drawLayeredCandidateOverlay({');
  const highlightIndex=DISPLAY_LAYERS_SOURCE.indexOf('drawRouteEvidenceHighlight({ctx,view,screenPoint');
  assert.ok(scenarioIndex>=0&&candidateIndex>scenarioIndex,'candidate is drawn after the scenario route');
  assert.ok(highlightIndex>candidateIndex,'the evidence highlight is drawn last (highest weight)');
  assert.match(DISPLAY_LAYERS_SOURCE,/const width=styles\.operationalWidth\+0\.4/);
  assert.match(DISPLAY_LAYERS_SOURCE,/ctx\.globalAlpha=styles\.scenarioAlpha\*\.72/);
  assert.match(MAIN_SOURCE,/routeAlpha:styles\.referenceAlpha\*\.7/);
  // 不允许自动 zoom / 自动切 layer / 改 LOD / 新增永久状态
  const setter=/set\(value\)\{([^}]*)\}/.exec(bindings)[1];
  assert.doesNotMatch(setter,/zoom|fit|setGridTheme|setGridOutline|currentStep|workbench/);
  assert.doesNotMatch(CANDIDATE_OVERLAY_SOURCE,/zoom\(|fit\(|checked|LOD_THRESHOLDS/);
});

// ---- helpers ------------------------------------------------------------------

function recordingContext(){
  const ctx={strokes:[],fills:[],globalAlpha:1,lineWidth:1,strokeStyle:null,fillStyle:null,
    save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},closePath(){},setLineDash(){},
    fill(){ctx.fills.push(ctx.fillStyle);},
    stroke(){ctx.strokes.push({color:ctx.strokeStyle,width:ctx.lineWidth});}};
  return ctx;
}

test('the recording context is a faithful canvas stand-in',()=>{
  const ctx=recordingContext();
  ctx.save();ctx.strokeStyle='#fff';ctx.lineWidth=9;ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(1,1);
  ctx.stroke();ctx.restore();
  assert.deepEqual(ctx.strokes,[{color:'#fff',width:9}]);
  assert.equal(LAYERED_FEASIBILITY_COLORS.feasible,'#2f9e6f');
  const cells=drawLayeredFeasibilityOverlay({ctx,view:{x:0,y:0,res:1},screenPoint:p=>p,
    flow:layeredFlow(),grid:layeredGrid(),gridTheme:{bboxIntersects:()=>true}});
  assert.equal(cells.cells,3);
  assert.equal(cells.path,0);
  assert.equal(ctx.fills.length,3);
});
