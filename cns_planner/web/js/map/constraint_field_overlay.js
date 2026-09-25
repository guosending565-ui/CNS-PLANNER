// =========================================================
// 高度层障碍（Planning Constraint Field）地图覆盖层
//
// 语义边界（B4X 第 10 / 12 节）
//  - 本层画的是**可行性事实**（能不能飞），与航路风险场（软成本）完全不合并；
//  - 三态各自独立的图层开关，默认**只画障碍**，避免可通行单元遮挡地图；
//  - `unknown`（证据不足）**绝不被当成可通行**：它的计数在状态卡里始终可见，
//    地图开关关闭只表示"不在图上铺开"，不表示"没有证据不足单元"；
//  - 几何来自前端既有的标准网格索引（`grid_id → cell.bbox`），约束接口只提供
//    `grid_id / outcome / blocked_by`，因此不会重复下发 GeoJSON；
//  - 未被索引到的 grid_id 会被如实计数并返回，绝不静默丢弃（那会让障碍格"消失"）。
// =========================================================
import {CONSTRAINT_LAYER_COLORS,constraintOutcomeStats,constraintOverlayEntries} from '../workflow/constraint_field.js';

/** 三态绘制样式：alpha 与线宽只在 LOD 之外的本模块内定义（不散落到 main.js）。 */
export const CONSTRAINT_OVERLAY_STYLE={
  //: 障碍单元优先突出：不透明、最高视觉权重。
  blocked:{alpha:0.62,stroke:'#7a1414',lineWidth:1.0},
  //: 证据不足：斜纹感不足时用更低的 alpha + 虚线描边，避免与障碍混淆。
  unknown:{alpha:0.34,stroke:'#8a5a00',lineWidth:0.8},
  //: 可通行：最低视觉权重。
  pass:{alpha:0.20,stroke:'#1d5c3d',lineWidth:0.6}
};

/** 绘制顺序：可通行 → 证据不足 → 障碍（障碍最后画，永远在最上层）。 */
export const CONSTRAINT_DRAW_ORDER=['pass','unknown','blocked'];

function cellPath(ctx,screenPoint,bbox){
  const corners=[[bbox[0],bbox[1]],[bbox[2],bbox[1]],[bbox[2],bbox[3]],[bbox[0],bbox[3]]];
  ctx.beginPath();
  corners.forEach((corner,index)=>{
    const point=screenPoint(corner);
    if(index===0)ctx.moveTo(point[0],point[1]);else ctx.lineTo(point[0],point[1]);
  });
  ctx.closePath();
}

/** 单元是否落入当前视图（bbox 与经纬度视口求交；grid cell bbox 是经纬度）。 */
export function constraintCellVisible(bbox,lonLatBounds){
  if(!Array.isArray(bbox)||bbox.length!==4)return false;
  const west=Number(bbox[0]),south=Number(bbox[1]),east=Number(bbox[2]),north=Number(bbox[3]);
  if(![west,south,east,north].every(Number.isFinite))return false;
  if(!Array.isArray(lonLatBounds)||lonLatBounds.length!==4)return true;
  return !(east<lonLatBounds[0]||west>lonLatBounds[2]||north<lonLatBounds[1]||south>lonLatBounds[3]);
}

/**
 * 绘制 Planning Constraint Field 覆盖层。
 *
 * @param {{ctx,view,screenPoint,model,cellsById,layers,visibleBounds,gridTheme}} input
 *   model 来自 `constraintMapModel()`；cellsById 是 `gridRenderCache.byId`。
 * @returns {{drawn:object,entries:number,unresolved:string[]}}
 */
export function drawConstraintFieldOverlay({
  ctx,view,screenPoint,model,cellsById,layers,visibleBounds=null,gridTheme=null
}){
  const result={drawn:{blocked:0,unknown:0,pass:0},entries:0,unresolved:[]};
  if(!ctx||!view||!model||!model.usable)return result;
  const switches=layers||{};
  const enabled=switches.enabled===true;
  if(!enabled)return result;
  const lonLatBounds=Array.isArray(visibleBounds)?visibleBounds:(view.__lonLatBounds||null);

  const {entries,unresolved}=constraintOverlayEntries({cells:model.cells,cellsById});
  result.entries=entries.length;
  result.unresolved=unresolved;

  // 按绘制顺序分桶：障碍最后画，保证它在最上层且不被未知/可通行盖住。
  const buckets={pass:[],unknown:[],blocked:[]};
  for(const entry of entries){
    const key=Object.prototype.hasOwnProperty.call(buckets,entry.outcome)?entry.outcome:'unknown';
    if(!switchOn(switches,key))continue;
    if(!Array.isArray(entry.bbox)||entry.bbox.length!==4)continue;
    if(lonLatBounds){
      const [west,south,east,north]=entry.bbox;
      if(east<lonLatBounds[0]||west>lonLatBounds[2]||north<lonLatBounds[1]||south>lonLatBounds[3])continue;
    }
    if(gridTheme&&typeof gridTheme.bboxIntersects==='function'&&!gridTheme.bboxIntersects(entry.bbox,view))continue;
    buckets[key].push(entry);
  }
  for(const outcome of CONSTRAINT_DRAW_ORDER){
    const style=CONSTRAINT_OVERLAY_STYLE[outcome];
    for(const entry of buckets[outcome]){
      ctx.save();
      ctx.globalAlpha=style.alpha;
      ctx.fillStyle=CONSTRAINT_LAYER_COLORS[outcome];
      cellPath(ctx,screenPoint,entry.bbox);
      ctx.fill();
      if(style.lineWidth>0){
        ctx.globalAlpha=Math.min(1,style.alpha+0.25);
        ctx.strokeStyle=style.stroke;
        ctx.lineWidth=style.lineWidth;
        ctx.stroke();
      }
      ctx.restore();
      result.drawn[outcome]+=1;
    }
  }
  return result;
}

function switchOn(switches,outcome){
  if(outcome==='blocked')return switches.blocked===true;
  if(outcome==='unknown')return switches.unknown===true;
  return switches.pass===true;
}

/** 覆盖层图例数据（状态卡与地图图例共用同一份计数）。 */
export function constraintOverlayLegend(model,entries){
  const stats=constraintOutcomeStats(entries||[]);
  return CONSTRAINT_DRAW_ORDER.map(outcome=>({
    outcome,
    color:CONSTRAINT_LAYER_COLORS[outcome],
    count:stats[outcome],
    total:model&&model.outcomes?Number(model.outcomes[outcome])||0:0
  }));
}
