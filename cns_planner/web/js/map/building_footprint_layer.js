// =========================================================
// 建筑轮廓图层（只读显示）
//
// 数据链路（浏览器绝不直接读 .qgz）：
//   QGIS 工程 / GeoPackage / Shapefile / GeoJSON
//     → 后端解析并定位建筑 Polygon 图层（/api/building-footprints）
//     → 按当前视图 bbox 走空间索引查询，返回简化后的 GeoJSON
//     → 本模块按 zoom LOD 决定"要不要拉、拉多少、怎么画"
//
// 边界：
//  - 纯显示层：不写 flow / ProjectState，不调用任何写接口；
//  - 不参与净空判定、风险或算法（unknown != safe 不变）；
//  - 默认关闭：只有图层抽屉里显式勾选"建筑轮廓"才会请求数据；
//  - 低缩放级别不加载全量建筑：按 ``res``（米/像素）分档限制请求范围与要素数。
// =========================================================

/**
 * 建筑轮廓的 LOD 分档阈值（每屏幕像素对应的地面米数）。
 * ``res`` 越大表示视图越"远"。
 *
 *  - ``hidden``：完全不请求（低级别不加载全部建筑，只提示放大）；
 *  - ``coarse``：只拉少量建筑 + 较大简化容差（看得见分布，不追求细节）；
 *  - ``full``  ：完整拉取（容差按 res 自适应）。
 */
export const BUILDING_FOOTPRINT_LOD={hiddenAboveRes:30,fullBelowRes:8,maxFeatures:{coarse:600,full:3000}};

/** 每个屏幕像素对应多少米时，几何简化容差取多少度（1 度 ≈ 111320 m）。 */
const METERS_PER_DEGREE=111320;

/** 视图移动到多远（像素）之后才重新请求（避免每次 paint 都打后端）。 */
const REFRESH_PIXEL_TOLERANCE=180;

/** 给定 res 决定建筑轮廓的显示档位。 */
export function buildingFootprintLevel(res){
  const value=Number(res);
  if(!Number.isFinite(value)||value<=0)return 'hidden';
  if(value>BUILDING_FOOTPRINT_LOD.hiddenAboveRes)return 'hidden';
  if(value>BUILDING_FOOTPRINT_LOD.fullBelowRes)return 'coarse';
  return 'full';
}

/** 简化容差（度）：越远越粗；几何简化只作用于显示，不改任何业务数据。 */
export function buildingFootprintTolerance(res){
  const value=Number(res);
  if(!Number.isFinite(value)||value<=0)return null;
  return Math.max(1e-7,(value*1.2)/METERS_PER_DEGREE);
}

/** 该档位下最多请求多少要素。 */
export function buildingFootprintLimit(level){
  return BUILDING_FOOTPRINT_LOD.maxFeatures[level]||BUILDING_FOOTPRINT_LOD.maxFeatures.coarse;
}

/** 显示样式：按档位调整填充/描边强度，避免在远景抢地图。 */
export function buildingFootprintStyle(level){
  if(level==='full')return {fill:'rgba(120,86,60,0.42)',stroke:'rgba(72,48,28,0.85)',lineWidth:1};
  return {fill:'rgba(120,86,60,0.22)',stroke:'rgba(72,48,28,0.55)',lineWidth:0.8};
}

function bboxOf(points){
  let west=Infinity,south=Infinity,east=-Infinity,north=-Infinity;
  for(const point of points){
    if(!Array.isArray(point)||point.length<2)continue;
    const lon=Number(point[0]),lat=Number(point[1]);
    if(!Number.isFinite(lon)||!Number.isFinite(lat))continue;
    if(lon<west)west=lon;if(lon>east)east=lon;
    if(lat<south)south=lat;if(lat>north)north=lat;
  }
  return Number.isFinite(west)?[west,south,east,north]:null;
}

function movedEnough(previous,next,res){
  if(!previous||!next)return true;
  const threshold=REFRESH_PIXEL_TOLERANCE*Math.max(1,Number(res)||0);
  const shift=Math.max(
    Math.abs(previous[0]-next[0]),Math.abs(previous[1]-next[1]),
    Math.abs(previous[2]-next[2]),Math.abs(previous[3]-next[3])
  );
  return shift*METERS_PER_DEGREE>=threshold;
}

/**
 * 建筑轮廓图层状态机。
 *
 * @param {{api:Function,onUpdate?:Function}} options
 *  - ``api``：既有 API 客户端（沿用 token / revision 头）；
 *  - ``onUpdate``：状态变化后触发的重绘回调（本模块不直接操作 canvas）。
 */
export function createBuildingFootprintLayer({api,onUpdate}={}){
  let state={
    enabled:false,level:'hidden',data:null,bbox:null,res:null,
    loading:false,error:null,truncated:false,count:0,source:null
  };
  let inFlight=0;

  const notify=()=>{if(typeof onUpdate==='function')onUpdate(state);};

  function statusLine(){
    if(!state.enabled)return '图层已关闭';
    if(state.error)return '加载失败：'+state.error;
    if(state.level==='hidden')return '放大后显示建筑轮廓';
    if(state.loading&&!state.count)return '正在加载建筑轮廓…';
    if(!state.count)return '当前视图没有建筑轮廓';
    return '已显示 '+state.count+' 个建筑轮廓'+(state.truncated?'（已达数量上限）':'');
  }

  /** 图层开关切换（默认关闭，只有用户显式打开才会请求）。 */
  function setEnabled(enabled){
    const next=Boolean(enabled);
    if(next===state.enabled)return state;
    state={...state,enabled:next};
    if(!next){state={...state,data:null,bbox:null,count:0,truncated:false,error:null,loading:false};}
    notify();
    return state;
  }

  function reset(){
    inFlight+=1;
    state={...state,data:null,bbox:null,res:null,count:0,truncated:false,error:null,loading:false};
    notify();
    return state;
  }

  /**
   * 与当前视图同步：只在"开关打开 + 缩放足够近 + 视图明显移动"时请求后端。
   * 低缩放级别直接返回，不加载任何建筑。
   */
  function sync({view,size,bbox}={}){
    if(!state.enabled){
      if(state.count||state.data)state={...state,data:null,count:0,truncated:false,bbox:null};
      return state;
    }
    const res=Number(view?.res);
    const level=buildingFootprintLevel(res);
    if(level==='hidden'){
      if(state.level!=='hidden'||state.count)state={...state,level,data:null,count:0,truncated:false,bbox:null,error:null};
      else state={...state,level};
      notify();
      return state;
    }
    if(!Array.isArray(bbox)||bbox.length!==4)return state;
    if(state.level===level&&!movedEnough(state.bbox,bbox,res)&&state.data)return state;
    const request=++inFlight;
    state={...state,level,bbox,res,loading:true,error:null};
    notify();
    const query=new URLSearchParams({
      bbox:bbox.map(value=>Number(value).toFixed(7)).join(','),
      limit:String(buildingFootprintLimit(level))
    });
    const tolerance=buildingFootprintTolerance(res);
    if(tolerance)query.set('tolerance',String(tolerance));
    return api('/api/building-footprints?'+query).then(data=>{
      if(request!==inFlight)return state;
      const failed=data?.status&&data.status!=='passed';
      state={...state,loading:false,
        data:failed?null:data,count:Number(data?.count)||0,
        truncated:Boolean(data?.truncated),source:data?.source||null,
        error:failed?(data.reason||'建筑数据源不可用'):null};
      notify();
      return state;
    }).catch(error=>{
      if(request!==inFlight)return state;
      state={...state,loading:false,data:null,count:0,truncated:false,error:error?.message||String(error)};
      notify();
      return state;
    });
  }

  /** 绘制当前已缓存的建筑轮廓（不做任何坐标换算之外的加工）。 */
  function draw(ctx,screenPoint){
    const features=state.data?.features||[];
    if(!state.enabled||!features.length)return 0;
    const style=buildingFootprintStyle(state.level);
    ctx.save();
    ctx.lineWidth=style.lineWidth;
    ctx.fillStyle=style.fill;
    ctx.strokeStyle=style.stroke;
    let drawn=0;
    for(const feature of features){
      const geometry=feature?.geometry;
      if(!geometry)continue;
      const polygons=geometry.type==='Polygon'?[geometry.coordinates]
        :geometry.type==='MultiPolygon'?geometry.coordinates:[];
      for(const polygon of polygons){
        if(!Array.isArray(polygon)||!polygon.length)continue;
        ctx.beginPath();
        for(const ring of polygon){
          if(!Array.isArray(ring)||ring.length<3)continue;
          let started=false;
          for(const point of ring){
            const [x,y]=screenPoint(point);
            if(!Number.isFinite(x)||!Number.isFinite(y)){started=false;continue;}
            if(started)ctx.lineTo(x,y);else{ctx.moveTo(x,y);started=true;}
          }
          ctx.closePath();
        }
        ctx.fill('evenodd');
        ctx.stroke();
        drawn+=1;
      }
    }
    ctx.restore();
    return drawn;
  }

  return {
    setEnabled,reset,sync,draw,
    statusLine,
    bboxOf,
    state:()=>state
  };
}

export const BUILDING_FOOTPRINT_LAYER_ID='buildingFootprintLayer';

/** 图层状态文本的元素 id（由图层开关 id 派生，避免再散落一份字符串常量）。 */
export function buildingFootprintStatusId(layerId=BUILDING_FOOTPRINT_LAYER_ID){
  return layerId+'Status';
}

/**
 * 把建筑轮廓图层接到壳层上：创建实例、跟随图层开关、同步状态文本。
 *
 * 全部装配都在这里完成，``main.js`` 只需要一行接线（入口文件必须保持精简，
 * 见 ``tests/test_architecture.py``）。复选框监听用 ``addEventListener``，
 * 与 ``shell.js`` 的 ``onchange`` 重绘绑定共存、互不覆盖。
 *
 * @param {{api,$,paint,getView,size,visibleLonLatBounds,isEnabled?,layerId?}} options
 */
export function attachBuildingFootprintLayer({
  api,$,paint,getView,size,visibleLonLatBounds,isEnabled,layerId=BUILDING_FOOTPRINT_LAYER_ID
}){
  const input=typeof $==='function'?$(layerId):null;
  const statusTarget=typeof $==='function'?$(buildingFootprintStatusId(layerId)):null;
  const layer=createBuildingFootprintLayer({api,onUpdate(){updateStatus();paint();}});

  function enabled(){
    return typeof isEnabled==='function'?Boolean(isEnabled()):input?.checked===true;
  }
  function updateStatus(){
    if(statusTarget)statusTarget.textContent=layer.statusLine();
  }
  function sync(){
    const view=getView?.();
    if(!view)return layer.state();
    layer.setEnabled(enabled());
    const result=layer.sync({view,size:size(),bbox:visibleLonLatBounds()});
    updateStatus();
    return result;
  }
  function reset(){
    layer.reset();
    updateStatus();
  }
  if(input&&typeof input.addEventListener==='function')input.addEventListener('change',()=>sync());
  updateStatus();
  return {
    sync,reset,updateStatus,
    draw:(ctx,screenPoint)=>layer.draw(ctx,screenPoint),
    statusLine:layer.statusLine,
    state:layer.state,
    setEnabled:layer.setEnabled
  };
}
