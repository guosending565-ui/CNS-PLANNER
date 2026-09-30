// =========================================================
// 专题成果图的前端状态（**独立于 workflow flow**）
//
// 为什么必须独立
// --------------
//  图件记录写在 active project 的 `artifacts/map_figures/index.json`，
//  **不进入 ProjectState**（渲染一张图不改变任何业务事实，也不推进业务 revision）。
//  因此前端绝不能依赖 `flow.map_figures`：那个容器只作为旧项目的只读兼容而存在，
//  新生成的图件永远不会出现在里面 —— 这正是"生成后当前图件不出现"的根因。
//
//  本模块是前端专题图状态的**唯一**来源：
//   * 首次进入 / 首次渲染读一次；
//   * 整页 refresh 后重新读；
//   * 导出成功后重新读；
//   * 下载 PNG / 规格 JSON 一律按这里记录的 `active_figure_id`。
//
// 只读契约
// --------
//  唯一的写入口是 `refreshMapFigureState()`，它只发一个 `GET /api/map-figures/state`
//  并**原样保存**服务端投影；本模块不推导适用性、不合成图件记录、不写 workflow。
// =========================================================

/** 空记录骨架（与后端 `records()` 的形状一致）。 */
function emptyRecords(){
  return {items:[],active_figure_id:null,count:0,project_revision:null};
}

function normalizeRecords(value){
  const source=value&&typeof value==='object'?value:{};
  const items=Array.isArray(source.items)?source.items.filter(item=>item&&typeof item==='object'):[];
  return {
    items,
    active_figure_id:source.active_figure_id??null,
    count:Number.isFinite(Number(source.count))?Number(source.count):items.length,
    project_revision:source.project_revision??null,
  };
}

let mapFigureState={
  catalog:null,records:emptyRecords(),loaded:false,loading:false,error:'',project_identity:'',
};
let inflight=null;
let requestSerial=0;

/** 当前只读快照（调用方只读，不要改返回对象）。 */
export function getMapFigureState(){return mapFigureState;}

/** 直接设定状态（供测试与局部刷新预置；生产链只用刷新函数）。 */
export function setMapFigureState(value){
  mapFigureState={
    catalog:value?.catalog??null,
    records:normalizeRecords(value?.records),
    loaded:value?.loaded!==false,
    loading:false,
    error:String(value?.error||''),
    project_identity:String(value?.project_identity||''),
  };
  return mapFigureState;
}

/** 复位（测试用）：清掉快照与进行中的请求。 */
export function resetMapFigureState(){
  inflight=null;
  requestSerial+=1;
  mapFigureState={
    catalog:null,records:emptyRecords(),loaded:false,loading:false,error:'',project_identity:'',
  };
  return mapFigureState;
}

export function mapFigureRecords(){return mapFigureState.records;}

/**
 * 当前图件：**严格**按 `active_figure_id` 匹配。
 *
 * 不做"取最后一条"的兜底：那会让界面显示一张并非当前图件的图，而下载按钮又按
 * active 图号取产物，两者立刻不一致。
 */
export function activeMapFigure(){
  const records=mapFigureState.records||emptyRecords();
  const items=Array.isArray(records.items)?records.items:[];
  const found=items.find(item=>item.figure_id===records.active_figure_id);
  return found||null;
}

/**
 * 读取真实图件状态（`GET /api/map-figures/state`）。
 *
 * 并发去重：同一时刻只发一次请求（首次进入、refresh 与导出后刷新可能同时触发）。
 * 失败时**如实记录** error 并保留上一次快照，绝不伪造空记录冒充"没有图件"。
 *
 * @param {{api:Function,projectIdentity?:string}} deps
 * @returns {Promise<object>} 更新后的状态
 */
export async function refreshMapFigureState({api,projectIdentity=''}={}){
  if(typeof api!=='function')throw new Error('refreshMapFigureState 缺少 api 依赖');
  const identity=String(projectIdentity||'');
  if(inflight&&mapFigureState.project_identity===identity)return inflight;
  const serial=++requestSerial;
  // 项目一旦变化，先清空旧项目快照再发请求；旧请求即使稍后返回，也会被 serial 丢弃。
  if(mapFigureState.project_identity!==identity){
    mapFigureState={
      catalog:null,records:emptyRecords(),loaded:false,loading:true,error:'',
      project_identity:identity,
    };
  }else{
    mapFigureState={...mapFigureState,loading:true};
  }
  inflight=(async()=>{
    try{
      const data=await api('/api/map-figures/state');
      if(serial!==requestSerial)return mapFigureState;
      mapFigureState={
        catalog:data?.catalog??null,
        records:normalizeRecords(data?.records),
        loaded:true,
        loading:false,
        error:'',
        project_identity:identity,
      };
    }catch(exc){
      if(serial!==requestSerial)return mapFigureState;
      mapFigureState={
        ...mapFigureState,
        loaded:true,
        loading:false,
        error:exc?.message||String(exc),
      };
    }finally{
      if(serial===requestSerial)inflight=null;
    }
    return mapFigureState;
  })();
  return inflight;
}

/** 适用性 → 中文（未登记的取值原样返回，绝不猜）。 */
export const MAP_FIGURE_APPLICABILITY_TEXT={
  current:'当前图件',
  stale_revision:'图件来源于旧项目 revision，请重新生成',
  superseded:'已被更新的图件取代',
  inactive:'非当前图件',
};

export function applicabilityText(value){
  const key=String(value??'');
  if(!key)return '—';
  return Object.prototype.hasOwnProperty.call(MAP_FIGURE_APPLICABILITY_TEXT,key)
    ?MAP_FIGURE_APPLICABILITY_TEXT[key]:key;
}
