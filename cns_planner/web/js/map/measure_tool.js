// =========================================================
// 测距工具（纯前端 UI，BUG-MAP-001）
//
// 只做三件事：
//  1. 把用户单击的 WGS84 经纬度点连成折线；
//  2. 按球面模型（R = 6371008.8 m）计算**地表距离**并累计；
//  3. 把折线、每个航段的长度与累计总长画在当前地图上。
//
// 边界（不越界）：
//  - 不调用任何 API、不写 ProjectState / flow、不触发保存；
//  - 不参与任何算法、风险、净空或规划判定（测距结果只是屏幕上的读数）；
//  - 退出（Esc / 清除 / 再点工具栏按钮）后不保留任何点。
//
// 距离语义：
//  - 按 WGS84 经纬度用 haversine 公式在地球球面上计算，R = 6371008.8 m；
//  - 显示：< 1 km 用 m，>= 1 km 用 km；
//  - 这不是投影平面距离，也不是椭球（geodesic）距离：R 固定为 WGS84 平均半径。
// =========================================================

/** WGS84 平均地球半径（m）。全部距离都以此常数计算，避免各地算法口径不一致。 */
export const EARTH_RADIUS_M=6371008.8;

/** 完成提示：单击加点 → 双击完成 → Esc 退出。 */
export const MEASURE_HINT='单击加点 · 双击完成 · Esc 退出';

/** 屏幕上必须远离上一个点多少像素才算一次真实移动（避免 pointermove 抖动重绘）。 */
const HOVER_MIN_PIXELS=2;

const toRadians=degrees=>degrees*Math.PI/180;

function normalizePoint(coordinate){
  if(!Array.isArray(coordinate)||coordinate.length<2)return null;
  const lon=Number(coordinate[0]),lat=Number(coordinate[1]);
  if(!Number.isFinite(lon)||!Number.isFinite(lat))return null;
  return [lon,lat];
}

/**
 * 两个 WGS84 经纬度点之间的地表距离（m）。
 *
 * ``null`` 表示输入非法——绝不把非法输入当成 0 距离。
 */
export function haversineDistanceM(from,to){
  const a=normalizePoint(from),b=normalizePoint(to);
  if(!a||!b)return null;
  const [lon1,lat1]=a,[lon2,lat2]=b;
  const halfLat=Math.sin(toRadians(lat2-lat1)/2),halfLon=Math.sin(toRadians(lon2-lon1)/2);
  const h=halfLat*halfLat+Math.cos(toRadians(lat1))*Math.cos(toRadians(lat2))*halfLon*halfLon;
  return 2*EARTH_RADIUS_M*Math.asin(Math.min(1,Math.sqrt(h)));
}

/** 折线累计地表距离（m）；非法点被跳过，不足以构成航段时返回 0。 */
export function pathDistanceM(points){
  return measureModel(points).totalM;
}

/**
 * 距离显示：< 1 km 显示 m（取整），>= 1 km 显示 km（2 位小数）。
 * ``null`` / 非有限值返回 ``'—'``，绝不显示伪 0。
 */
export function formatDistance(meters){
  if(meters===null||meters===undefined||typeof meters==='boolean')return '—';
  if(typeof meters==='string'&&meters.trim()==='')return '—';
  const value=Number(meters);
  if(!Number.isFinite(value)||value<0)return '—';
  if(value<1000)return Math.round(value)+' m';
  return (value/1000).toFixed(2)+' km';
}

/**
 * 由顶点序列算出可绘制/可复制的模型：逐段长度、累计长度与总长。
 *
 * @param {Array<[number,number]>} points WGS84 经纬度顶点
 */
export function measureModel(points){
  const list=[];
  for(const point of Array.isArray(points)?points:[]){
    const normalized=normalizePoint(point);
    if(normalized)list.push(normalized);
  }
  const segments=[];
  let totalM=0;
  for(let index=1;index<list.length;index+=1){
    const lengthM=haversineDistanceM(list[index-1],list[index]);
    if(lengthM===null)continue;
    totalM+=lengthM;
    segments.push({
      index,from:list[index-1],to:list[index],lengthM,
      cumulativeM:totalM,label:formatDistance(lengthM),cumulativeLabel:formatDistance(totalM)
    });
  }
  return {
    points:list,segments,totalM,
    totalLabel:list.length>1?formatDistance(totalM):null,
    complete:list.length>1
  };
}

/**
 * 测距状态机（纯内存）。
 *
 * @param {{onUpdate?:Function}} options ``onUpdate`` 在状态变化后触发重绘。
 */
export function createMeasureTool({onUpdate}={}){
  let state={active:false,points:[],hover:null,finished:false};
  let lastHoverScreen=null;

  const notify=()=>{if(typeof onUpdate==='function')onUpdate(state);};
  const emit=patch=>{state={...state,...patch};notify();return state;};

  /**
   * 进入/退出测距：退出时一次性丢弃全部点，不保留任何中间状态。
   * 幂等：重复设置同一个状态不会丢掉已经点好的顶点。
   */
  function setActive(active){
    const next=Boolean(active);
    if(next===state.active)return state;
    lastHoverScreen=null;
    return next
      ?emit({active:true,points:[],hover:null,finished:false})
      :emit({active:false,points:[],hover:null,finished:false});
  }

  /** 单击加点（未激活或已完成时忽略）。 */
  function addPoint(coordinate){
    if(!state.active)return state;
    const point=normalizePoint(coordinate);
    if(!point)return state;
    lastHoverScreen=null;
    return emit({points:[...state.points,point],hover:null,finished:false});
  }

  /** 鼠标当前位置的预览线；``pixel``（屏幕像素）只用于抑制抖动，不参与距离计算。 */
  function setHover(coordinate,pixel){
    if(!state.active||state.finished)return state;
    const point=normalizePoint(coordinate);
    if(!point)return state;
    const usable=Array.isArray(pixel)&&pixel.length>=2
      &&Number.isFinite(Number(pixel[0]))&&Number.isFinite(Number(pixel[1]));
    if(usable&&lastHoverScreen){
      const moved=Math.abs(Number(pixel[0])-lastHoverScreen[0])
        +Math.abs(Number(pixel[1])-lastHoverScreen[1]);
      if(moved<HOVER_MIN_PIXELS)return state;
    }
    if(usable)lastHoverScreen=[Number(pixel[0]),Number(pixel[1])];
    return emit({hover:point});
  }

  /** 双击完成：冻结当前顶点集合，之后移动鼠标不再改变读数。 */
  function finish(){
    if(!state.active)return state;
    lastHoverScreen=null;
    return emit({finished:true,hover:null});
  }

  /** 清除全部测距点（不退出测距模式）。 */
  function clear(){
    lastHoverScreen=null;
    return emit({points:[],hover:null,finished:false});
  }

  function close(){
    lastHoverScreen=null;
    return emit({active:false,points:[],hover:null,finished:false});
  }

  function model(){return measureModel(state.points);}

  /** 当前总长的显示文本（不足两点时为 null，绝不显示 0 m）。 */
  function totalLabel(){return model().totalLabel;}

  /**
   * 绘制折线 + 逐段读数。只读状态、不修改任何业务数据。
   * @returns {number} 已绘制的顶点数
   */
  function draw(ctx,screenPoint){
    if(!state.active)return 0;
    const current=model();
    const preview=state.hover&&current.points.length&&!state.finished
      ?[current.points[current.points.length-1],state.hover]:null;
    const previewM=preview?haversineDistanceM(preview[0],preview[1]):null;
    ctx.save();
    ctx.strokeStyle='#c2410c';
    ctx.fillStyle='#c2410c';
    ctx.lineWidth=2;
    ctx.setLineDash([]);
    if(current.points.length){
      ctx.beginPath();
      current.points.forEach((point,index)=>{
        const [x,y]=screenPoint(point);
        if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);
      });
      if(preview){const [x,y]=screenPoint(preview[1]);ctx.lineTo(x,y);}
      ctx.stroke();
    }
    if(preview){
      ctx.save();
      ctx.setLineDash([5,4]);
      ctx.globalAlpha=.7;
      ctx.beginPath();
      const from=screenPoint(preview[0]),to=screenPoint(preview[1]);
      ctx.moveTo(from[0],from[1]);ctx.lineTo(to[0],to[1]);ctx.stroke();
      ctx.restore();
      if(previewM!==null)drawLabel(ctx,screenPoint(preview[1]),formatDistance(previewM),'#9a3412');
    }
    for(const segment of current.segments){
      drawLabel(ctx,screenPoint(segment.to),segment.cumulativeLabel,'#7c2d12');
    }
    for(const point of current.points){
      const [x,y]=screenPoint(point);
      ctx.beginPath();ctx.arc(x,y,3.4,0,Math.PI*2);ctx.fill();
    }
    ctx.restore();
    return current.points.length;
  }

  function drawLabel(ctx,position,text,color){
    ctx.save();
    ctx.font='12px system-ui, sans-serif';
    ctx.textAlign='left';ctx.textBaseline='bottom';
    ctx.fillStyle='rgba(255,255,255,.88)';
    const width=ctx.measureText?ctx.measureText(text).width:0;
    ctx.fillRect(position[0]+6,position[1]-16,width+6,15);
    ctx.fillStyle=color;
    ctx.fillText(text,position[0]+9,position[1]-3);
    ctx.restore();
  }

  return {
    state:()=>state,
    isActive:()=>state.active,
    isFinished:()=>state.finished,
    setActive,close,addPoint,setHover,finish,clear,model,totalLabel,draw,
  };
}

/**
 * 把测距接到壳层上：创建实例、接管工具栏按钮 / Esc / 画布单击与双击，并维护状态文本。
 *
 * 全部装配都在这里完成，``main.js`` 只需要一行接线与一次绘制调用（入口文件必须保持精简，
 * 见 ``tests/test_architecture.py``）。壳层只提供读写 ``interactionMode`` 的两个回调，
 * 本模块不持有、也不写任何业务状态：
 *
 *  - 单击加点（双击的第二次 click 由 ``event.detail`` 识别，不重复加点）；
 *  - 双击完成；Esc / 「清除」/ 再点工具栏退出并丢弃全部点；
 *  - 退出时把 ``interactionMode`` 交回 ``pan``（调用方通过 ``setMode`` 落地）。
 *
 * @param {{$,canvas,paint,eventLonLat,getMode,setMode}} options
 */
export function attachMeasureTool({$,canvas,paint,eventLonLat,getMode,setMode}){
  const tool=createMeasureTool({onUpdate:()=>updateUi()});
  const button=typeof $==='function'?$('measureTool'):null;
  const clearButton=typeof $==='function'?$('measureClear'):null;
  const status=typeof $==='function'?$('measureStatus'):null;

  function statusText(){
    const current=tool.model();
    if(tool.isFinished()){
      return '测距完成 · 顶点 '+current.points.length+' · 累计 '+(current.totalLabel||'—')+' · Esc 退出';
    }
    return MEASURE_HINT+(current.totalLabel
      ?' · 顶点 '+current.points.length+' · 累计 '+current.totalLabel:' · 尚未落点');
  }

  function updateUi(){
    const active=getMode()==='measure';
    if(button){
      button.classList.toggle('active',active);
      button.setAttribute('aria-pressed',active?'true':'false');
    }
    if(status){status.hidden=!active;status.textContent=active?statusText():'';}
  }

  /** 进入 / 退出测距：退出时清空状态文本并把模式交还给 pan。 */
  function enter(active){
    const next=Boolean(active);
    setMode(next?'measure':'pan');
    tool.setActive(next);
    updateUi();
    paint();
  }

  /** ``interactionMode`` 被别处改动后同步：不丢已点顶点，也不留下过期的激活态。 */
  function sync(){tool.setActive(getMode()==='measure');updateUi();}

  function hover(coordinate,pixel){
    if(getMode()!=='measure')return;
    const before=tool.state().hover;
    tool.setHover(coordinate,pixel);
    const after=tool.state().hover;
    if(after&&(!before||before[0]!==after[0]||before[1]!==after[1]))paint();
  }

  function addPoint(coordinate){
    if(getMode()!=='measure')return;
    tool.addPoint(coordinate);
    paint();
  }

  function finish(){
    if(getMode()!=='measure')return;
    tool.finish();
    updateUi();
    paint();
  }

  if(button)button.onclick=()=>enter(getMode()!=='measure');
  if(clearButton)clearButton.onclick=()=>{tool.clear();enter(false);};
  if(canvas){
    canvas.addEventListener('click',event=>{
      if(getMode()!=='measure')return;
      // 双击的第二次 click 是"完成"动作的一部分，不是一个新顶点。
      if(event.detail>1)return;
      tool.addPoint(eventLonLat(event));
      paint();
    });
    canvas.addEventListener('dblclick',()=>finish());
  }
  if(typeof document!=='undefined'&&document.addEventListener){
    document.addEventListener('keydown',event=>{
      if(event.key!=='Escape'||getMode()!=='measure')return;
      event.preventDefault();enter(false);
    });
  }
  updateUi();
  return {sync,enter,hover,addPoint,finish,updateUi,state:tool.state,model:tool.model,
    isActive:tool.isActive,isFinished:tool.isFinished,
    draw:(ctx,screenPoint)=>tool.draw(ctx,screenPoint)};
}
