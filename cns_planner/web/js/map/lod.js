// =========================================================
// 集中式地图显示分级（LOD）
//
// 目的：缩小看概况、放大看细节。所有与比例尺有关的显示阈值只在
// 本模块定义，禁止散落到 main.js 或其他绘制代码里。
//
// 判定依据：view.res（每屏幕像素对应的 EPSG:3857 米）。40075016.686 米
// 是 Web Mercator 赤道周长，因此 res 可换算为"每像素多少米"。
//
// 三档：
//   overview  全域/大范围 —— 只显示概况
//   medium    中等尺度   —— 开始出现主要点
//   detail    局部尺度   —— 展开单点与名称
// =========================================================

/** 每屏幕像素对应的地面米数分档阈值（数值越大表示视图越"远"）。 */
export const LOD_THRESHOLDS={medium:2200,detail:420};

/** 单个起降点/航路点被合并的屏幕距离（像素）。 */
export const CLUSTER_PIXEL_THRESHOLD={overview:26,medium:15,detail:0};

/**
 * 真实铁塔图标的显示模式（复用同一套 LOD，不新增第二套阈值）。
 *
 * ``cluster``          只显示聚合点；孤立单塔不绘制（也不参与命中）。
 * ``cluster_isolated`` 仍以聚合为主，只有聚合不住的孤立铁塔画出简化符号。
 * ``single``           每一个真实铁塔都画独立铁塔图标（逐塔显示）。
 */
export const TOWER_MARKER_MODES={
  overview:'cluster',
  medium:'cluster_isolated',
  detail:'single'
};

/**
 * 铁塔符号的**真实屏幕像素尺寸**（MAP-TOWER-SYMBOL-V2）。
 *
 * BUG（已修复）：旧实现把 ``towerSymbolScale`` 当成归一化倍数（0.7 / 0.78 / 1）直接乘进
 * canvas 坐标，而 ``TOWER_SYMBOL`` 的 y 跨度只有约 1.75 单位 ⇒ detail 档符号总高度也只有
 * 约 1.75 px，地图放大到 detail LOD 仍然几乎不可见。
 *
 * 现在这里的数值就是**符号的可见高度（px）**，几何本身仍是同一份归一化 ``TOWER_SYMBOL``：
 *  * ``overview`` 只画聚合点（单塔不进入绘制计划），因此单塔高度为 0；被 highlight 的
 *    宿主塔例外显示，并沿用 medium 的尺寸；
 *  * ``medium``   孤立单塔约 16 px 高（肉眼可辨认）；
 *  * ``detail``   逐塔显示，约 22 px 高（桁架结构清晰）。
 *
 * 具体的 px → 归一化坐标换算只发生在 ``display_layers.js``（符号几何的唯一所有者），
 * 本模块只声明"多大、多粗、命中半径多少"。
 */
export const TOWER_SYMBOL_SIZE_PX={
  overview:0,
  medium:16,
  detail:22
};

/** 被高亮的宿主铁塔：任何档位都至少按 detail 尺寸绘制，外加高亮环。 */
export const TOWER_HIGHLIGHT_SIZE_PX=22;

/** 共塔候选（真实铁塔派生的宿主候选）：与宿主塔使用同一尺寸语言。 */
export const TOWER_COLOCATION_CANDIDATE_SIZE_PX=22;

/** 铁塔高亮环半径（px）：必须明显大于符号本身，不能只靠颜色变化。 */
export const TOWER_HIGHLIGHT_RING_RADIUS_PX=15;

/**
 * 铁塔符号的笔画宽度（px，与尺寸一起集中在本模块，禁止散落到绘制代码）。
 *
 * 主体 stroke 不能细到看不见；halo 比主体再宽约 2 px，保证在海图、道路、建筑底图上
 * 都有足够对比度。
 */
export const TOWER_SYMBOL_STROKE_PX={
  overview:1.5,
  medium:1.7,
  detail:2.1
};
export const TOWER_SYMBOL_HALO_EXTRA_PX=2;

/**
 * 铁塔（单塔）的点击命中半径（px）。
 *
 * 必须与视觉尺寸一致：detail 档符号约 22 px 高、约 14 px 宽，半径 13 px 才能避免
 * "看得见却点不中"。它只作用于 towers；nodes / landingSites 的命中半径完全不变。
 */
export const TOWER_HIT_RADIUS_PX={
  overview:13,
  medium:13,
  detail:13
};

/**
 * 参考航线与航路点的显示阈值：航路点只有在足够近时才显示。
 * scenario/operational/CNS gap 线宽按档整体降低，避免抢地图。
 *
 * 铁塔相关的 LOD 参数（``towerMarkerMode`` / ``towerSymbolSizePx`` …）与其它图层共用
 * 同一套 LOD 档位，不新增第二套阈值。
 */
export const ROUTE_STYLES={
  overview:{
    routeWidth:1,routeAlpha:.30,referenceWidth:1,referenceAlpha:.30,
    scenarioWidth:1.5,scenarioAlpha:.55,operationalWidth:2.2,operationalAlpha:.85,
    gapWidth:2.4,gapAlpha:.75,infeasibleWidth:1,pointRadius:2.4,pointAlpha:.55,
    nameMode:'hidden',markerMode:'cluster',showAllNames:false,coverageRing:false,
    towerMarkerMode:TOWER_MARKER_MODES.overview,
    towerSymbolSizePx:TOWER_SYMBOL_SIZE_PX.overview,
    towerSymbolStrokePx:TOWER_SYMBOL_STROKE_PX.overview,
    towerHighlightSizePx:TOWER_HIGHLIGHT_SIZE_PX,
    towerCandidateSizePx:TOWER_COLOCATION_CANDIDATE_SIZE_PX,
    towerHighlightRingRadiusPx:TOWER_HIGHLIGHT_RING_RADIUS_PX,
    towerHitRadiusPx:TOWER_HIT_RADIUS_PX.overview
  },
  medium:{
    routeWidth:1.5,routeAlpha:.45,referenceWidth:1.5,referenceAlpha:.55,
    scenarioWidth:2,scenarioAlpha:.7,operationalWidth:3,operationalAlpha:.92,
    gapWidth:3.2,gapAlpha:.85,infeasibleWidth:1.2,pointRadius:3,pointAlpha:.8,
    nameMode:'avoid',markerMode:'cluster',showAllNames:false,coverageRing:true,
    towerMarkerMode:TOWER_MARKER_MODES.medium,
    towerSymbolSizePx:TOWER_SYMBOL_SIZE_PX.medium,
    towerSymbolStrokePx:TOWER_SYMBOL_STROKE_PX.medium,
    towerHighlightSizePx:TOWER_HIGHLIGHT_SIZE_PX,
    towerCandidateSizePx:TOWER_COLOCATION_CANDIDATE_SIZE_PX,
    towerHighlightRingRadiusPx:TOWER_HIGHLIGHT_RING_RADIUS_PX,
    towerHitRadiusPx:TOWER_HIT_RADIUS_PX.medium
  },
  detail:{
    routeWidth:2,routeAlpha:.6,referenceWidth:1.8,referenceAlpha:.7,
    scenarioWidth:2.4,scenarioAlpha:.75,operationalWidth:3.6,operationalAlpha:1,
    gapWidth:4,gapAlpha:.95,infeasibleWidth:1.4,pointRadius:3.4,pointAlpha:.95,
    nameMode:'avoid',markerMode:'single',showAllNames:false,coverageRing:true,
    towerMarkerMode:TOWER_MARKER_MODES.detail,
    towerSymbolSizePx:TOWER_SYMBOL_SIZE_PX.detail,
    towerSymbolStrokePx:TOWER_SYMBOL_STROKE_PX.detail,
    towerHighlightSizePx:TOWER_HIGHLIGHT_SIZE_PX,
    towerCandidateSizePx:TOWER_COLOCATION_CANDIDATE_SIZE_PX,
    towerHighlightRingRadiusPx:TOWER_HIGHLIGHT_RING_RADIUS_PX,
    towerHitRadiusPx:TOWER_HIT_RADIUS_PX.detail
  }
};

/**
 * 当前 LOD 档位下单个铁塔是否真的被画出来。
 * @param {'overview'|'medium'|'detail'} level
 * @returns {boolean} overview 只画聚合点，因此单塔返回 false
 */
export function towerSingleVisible(level){
  return (TOWER_MARKER_MODES[level]||TOWER_MARKER_MODES.detail)!=='cluster';
}

/** 三档的中文名，用于地图角标与状态栏。 */
export const LOD_LABELS={overview:'概述',medium:'中等',detail:'细节'};

/**
 * 根据视图分辨率（以及可选的屏幕可见要素数量）给出 LOD 档位。
 * @param {number} res view.res（EPSG:3857 米/像素）
 * @param {number} [featureCount] 屏幕内要素数量，用于在临界处提前降级
 */
export function lodLevel(res,featureCount=0){
  const value=Number(res);
  if(!Number.isFinite(value)||value<=0)return 'overview';
  // 要素极多时下调一档，避免过密遮挡（只是显示逻辑，不改变数据）
  const crowded=Number(featureCount)>1400?1:0;
  if(value>LOD_THRESHOLDS.medium)return crowded?'overview':'overview';
  if(value>LOD_THRESHOLDS.detail)return crowded?'overview':'medium';
  return crowded?'medium':'detail';
}

/** 每像素米数的可读文本。 */
export function resolutionLabel(res){
  const value=Number(res);
  if(!Number.isFinite(value)||value<=0)return '—';
  if(value>=1000)return (value/1000).toFixed(1)+' km/px';
  return value.toFixed(0)+' m/px';
}

/**
 * 一次性取出当前视图的完整显示配置。
 * @param {{res:number,featureCount?:number}} view
 */
export function displayStyle(view){
  const level=lodLevel(view&&view.res,view&&view.featureCount);
  return {
    level,
    label:LOD_LABELS[level],
    resolution:resolutionLabel(view&&view.res),
    clusterPixels:CLUSTER_PIXEL_THRESHOLD[level],
    styles:ROUTE_STYLES[level]
  };
}

/** 某个图层在指定档位下是否应该绘制（供调用方做整体开关）。 */
export function visibleAt(level,feature){
  const table={
    referencePoints:{overview:false,medium:'near',detail:true},
    referenceRoutes:{overview:true,medium:true,detail:true},
    scenarioRoutes:{overview:true,medium:true,detail:true},
    operationalRoutes:{overview:true,medium:true,detail:true},
    gapLines:{overview:true,medium:true,detail:true}
  };
  const row=table[feature];
  if(!row)return true;
  const value=row[level];
  return value===undefined?true:value;
}
