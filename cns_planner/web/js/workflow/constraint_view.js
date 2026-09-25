/**
 * =========================================================================
 * 高度层障碍（Planning Constraint Field）在壳层的唯一装配点（B4X）
 * =========================================================================
 *
 * 为什么单独一个模块
 * ------------------
 * `main.js` 有一条既有的架构约束：**必须保持 450 行以内的轻入口**（由
 * `tests/grid_popup_hydration_frontend.test.mjs` 与
 * `tests/population_nodata_frontend.test.mjs` 共同锁定）。B4X 需要把
 * Planning Constraint Field 接到地图、图层开关、图例与点击弹窗上，因此全部逻辑
 * 收在本模块，`main.js` 只保留装配与依赖注入（约 20 行）。
 *
 * 语义边界（与 B3X / B4X 一致）
 * ----------------------------
 *  - **风险场 = 软成本**；**约束场 = 可行性**。本模块只处理可行性，完全不读风险评分，
 *    两者在地图上、图例上、状态卡上都不合并；
 *  - 摘要来自 workflow 快照（slim、不含 cells），因此**切换高度层不发请求**；
 *    只有用户勾选「高度层障碍」后才按需读取逐格明细（只读 GET）；
 *  - `unknown`（证据不足）**绝不等价于 pass**：地图默认不铺开它，但状态卡与图例
 *    始终给出计数；
 *  - 前端不接触任何文件系统路径：sidecar 由后端读取，HTTP 层只返回
 *    `grid_id / outcome / blocked_by`；
 *  - 不写业务状态、不重算任何结果：生成约束场必须由用户在 Step2 显式点击触发。
 */
import {
  CONSTRAINT_LAYER_ID,loadConstraintField,constraintFieldModel,
  constraintFieldFreshness,constraintLegendModel,constraintCellDetailsHtml
} from './constraint_field.js';
import {drawConstraintFieldOverlay} from '../map/constraint_field_overlay.js';

/**
 * 建立约束场视图控制器。
 *
 * @param {object} deps
 * @param {(url:string)=>Promise<object>} deps.api 只读 GET 客户端
 * @param {() => object|null} deps.getFlow 读取当前 workflow
 * @param {() => object} deps.getLayers 读取图层开关（键=checkbox id）
 * @param {() => [number,number,number,number]} deps.visibleBounds 当前视图经纬度 bbox
 * @param {() => object} deps.getGridCache 网格渲染缓存（byId: grid_id → {cell}）
 * @param {(id:string)=>object|null} deps.getNode 读取 DOM 节点
 * @param {() => void} [deps.afterChange] 状态变化后的重绘回调（renderWorkflow / paint）
 * @param {() => void} [deps.paint] 只重绘地图
 */
export function createConstraintFieldView(deps){
  const {
    api,getFlow,getLayers,visibleBounds,getGridCache,getNode,
    afterChange=()=>{},paint=()=>{}
  } = deps;
  for(const [name,value] of Object.entries({api,getFlow,getLayers,visibleBounds,getGridCache,getNode})){
    if(typeof value!=='function')throw new Error('createConstraintFieldView 缺少依赖：'+name);
  }

  //: 纯展示状态：用户当前选择的高度层 + 已读到的结果。不写任何业务状态。
  let state={altitudeLayerId:'',collection:null,map:null,error:'',loading:false,mapLoaded:false};
  //: 地图数据加载的重入保护：同一高度层只允许一个进行中的请求。
  let mapRequest=null;

  /** 摘要数据来源：本地已读结果优先，否则用 workflow 快照里的 slim 摘要。 */
  function collection(){
    const local=state.collection;
    if(local&&Array.isArray(local.items)&&local.items.length)return local;
    const fromFlow=getFlow()?.planning_constraint_fields;
    return fromFlow&&Array.isArray(fromFlow.items)?fromFlow:local;
  }

  /** 当前选择的高度层目录条目（找不到时返回 null，绝不编造）。 */
  function altitudeLayer(){
    const layers=getFlow()?.spatial_3d?.altitude_layers||[];
    return layers.find(item=>String(item.altitude_layer_id||'')===String(state.altitudeLayerId||''))||null;
  }

  /** 展示模型（Step2 / Step3 面板与地图共用同一份）。 */
  function model(){
    return constraintFieldModel({
      collection:collection(),map:state.map,
      altitudeLayerId:state.altitudeLayerId,workspaceIdentity:getFlow()?.workspace||null
    });
  }

  /** 传给步骤面板的展示上下文。 */
  function presentation(){
    const current=model();
    return {
      model:current,
      freshness:constraintFieldFreshness(current,{workspaceIdentity:getFlow()?.workspace||null}),
      altitudeLayerLabel:altitudeLayerLabelText(),
      mapLoaded:Boolean(state.mapLoaded||(state.map&&state.map.usable)),
      error:state.error,
      loading:state.loading,
      altitudeLayerId:state.altitudeLayerId
    };
  }

  /** 高度层的中文显示文本（`ALT-080 · 80 m · EGM2008 正高`）。 */
  function altitudeLayerLabelText(){
    const layer=altitudeLayer();
    if(!layer)return String(state.altitudeLayerId||'');
    return layerLabel(layer);
  }

  /** 用户选择另一个高度层：只切展示并丢弃上一个层的地图明细（不发请求）。 */
  function select(altitudeLayerId){
    const wanted=String(altitudeLayerId||'').trim();
    state={...state,altitudeLayerId:wanted,collection:null,map:null,mapLoaded:false,error:''};
    afterChange();
    return wanted;
  }

  /** 地图图层是否需要数据（勾选后按需加载，避免默认把几万格发到浏览器）。 */
  function layerNeedsData(){return getLayers()[CONSTRAINT_LAYER_ID]===true;}

  /** 图层开关状态：默认只画障碍；可通行 / 证据不足各自独立。 */
  function layerState(){
    const switches=getLayers();
    return {
      enabled:switches[CONSTRAINT_LAYER_ID]===true,
      blocked:switches[CONSTRAINT_LAYER_ID]===true,
      unknown:switches.altitudeConstraintUnknownLayer===true,
      pass:switches.altitudeConstraintPassLayer===true
    };
  }

  /** 按需读取逐格明细（只读 GET；未选择高度层时**不发起任何请求**）。 */
  async function loadMap(){
    const altitudeLayerId=state.altitudeLayerId;
    if(!altitudeLayerId||!layerNeedsData())return null;
    if(state.mapLoaded)return state.map;
    if(mapRequest)return mapRequest;
    mapRequest=(async()=>{
      try{
        const result=await loadConstraintField(api,altitudeLayerId,{bbox:null});
        state={...state,collection:result.summary,map:result.map,
          mapLoaded:Boolean(result.map&&result.map.usable),error:'',loading:false};
      }catch(exc){
        state={...state,error:exc.message||'数据源不可用',loading:false};
      }finally{
        mapRequest=null;
      }
      afterChange();
      return state.map;
    })();
    return mapRequest;
  }

  /** 绘制覆盖层：勾选但尚未加载时先触发加载，本轮不画（下一轮重绘补上）。 */
  function draw({ctx,view,screenPoint,gridTheme=null}){
    if(layerNeedsData()&&!state.mapLoaded&&state.altitudeLayerId){
      loadMap();
      return {drawn:{blocked:0,unknown:0,pass:0},entries:0,unresolved:[]};
    }
    return drawConstraintFieldOverlay({
      ctx,view,screenPoint,model:model(),cellsById:getGridCache()?.byId,
      layers:layerState(),visibleBounds:visibleBounds(),gridTheme
    });
  }

  /** 单个网格编号对应的约束格（未读到明细时返回 null，调用方如实说明）。 */
  function cellFor(gridId){
    if(!gridId||!state.map||!state.map.usable)return null;
    return state.map.cells.find(cell=>cell.gridId===String(gridId))||null;
  }

  /**
   * 网格点击弹窗的**约束段落**（B4X §12.1）。
   *
   * 用户点一个障碍格，第一眼要看到"为什么不能飞"：优先高度层 / 状态 / 主要阻挡原因；
   * 详细 evidence 进高级折叠。没有读到明细时返回空串（如实表达"尚未读取"），
   * **绝不**显示成"可通行 / 通过"。
   */
  function cellDetailsHtml(gridId){
    const cell=cellFor(gridId);
    if(!cell)return '';
    return '<div class="grid-info-constraint" data-outcome="'+cell.outcome+'">'
      +constraintCellDetailsHtml(cell,{
        altitudeLayerLabelText:altitudeLayerLabelText(),gridId:String(gridId||'')
      })+'</div>';
  }

  /**
   * 图例。只要勾选「高度层障碍」就显示三态（含**证据不足**计数）——
   * 即使"证据不足"地图开关默认关闭，它的计数也始终可见。
   */
  function updateLegend(){
    const legend=getNode('constraintFieldLegend');
    if(!legend)return false;
    const enabled=layerNeedsData();
    legend.hidden=!enabled;
    if(!enabled)return false;
    const current=model();
    const rows=constraintLegendModel(current,current.cells).rows;
    legend.innerHTML='<p><b>高度层障碍</b><span>'+escapeHtml(altitudeLayerLabelText()||'未选择高度层')+'</span></p>'
      +rows.map(row=>'<div class="constraint-legend-row" data-outcome="'+row.outcome+'">'
        +'<i style="background:'+row.color+'"></i>'+escapeHtml(row.label)
        +'<span>'+Number(row.count).toLocaleString()+' 格</span></div>').join('')
      +'<small>'+(current.usable
        ?'约束场 = 可行性（能不能飞）；与风险场（软成本）分开显示。'
          +'穿越证据不足单元的候选永远不能发布为运行航路。'
        :'逐格明细尚未读取：勾选后按需读取；证据不足计数以状态卡为准。')+'</small>';
    return true;
  }

  /** 生成某个高度层的约束场（后端 POST；只由用户显式点击触发）。 */
  async function generate(altitudeLayerId,post){
    const id=String(altitudeLayerId||state.altitudeLayerId||'').trim();
    if(!id)throw Error('请先选择固定巡航高度层');
    if(typeof post!=='function')throw Error('约束场生成入口不可用，请刷新项目状态');
    await post('/api/planning-constraint-fields/evaluate',{altitude_layer_id:id});
    state={...state,altitudeLayerId:id,collection:null,map:null,mapLoaded:false,error:''};
    return id;
  }

  /** 打开另一个项目 / 清工作区时重置展示状态（不自动加载）。 */
  function reset(){
    state={altitudeLayerId:'',collection:null,map:null,error:'',loading:false,mapLoaded:false};
    mapRequest=null;
  }

  /**
   * Step2 / Step3 面板需要的绑定入口（`main.js` 的 stepBindings 直接转发）。
   *
   * @param {(path:string,payload:object)=>Promise<object>} post 显式生成用的 POST（resourceAction）
   * @param {() => Promise<object>} [refresh] 生成成功后重读完整 workflow 快照
   */
  function stepBindings(post,refresh){
    return {
      select:value=>select(value),
      presentation:()=>presentation(),
      // 显式生成某个高度层的约束场（后端 POST，成功后重读 workflow 快照）。
      generate:async altitudeLayerId=>{
        const id=String(altitudeLayerId||state.altitudeLayerId||'').trim();
        if(!id)throw Error('请先选择固定巡航高度层');
        await generate(id,post);
        if(typeof refresh==='function')await refresh();
        return presentation();
      },
      // 读取当前高度层的逐格明细（只读；已读过则直接复用）。
      loadMap:()=>loadMap(),
      refreshLegend:()=>updateLegend(),
    };
  }

  return {
    select,model,presentation,draw,cellFor,loadMap,updateLegend,cellDetailsHtml,
    altitudeLayerLabel:altitudeLayerLabelText,
    layerNeedsData,layerState,generate,reset,paint,stepBindings,
    state(){return {...state};}
  };
}

function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}

//: 高度层中文标签的本地实现（避免与 altitude_layers.js 形成循环 import）。
function layerLabel(layer){
  const id=String(layer.altitude_layer_id||'');
  const nominal=Number(layer.nominal_altitude_m);
  const altitude=Number.isFinite(nominal)
    ?(Number.isInteger(nominal)?String(nominal):String(Number(nominal.toFixed(2))))+' m · '+referenceText(layer.vertical_reference)
    :'nominal 高度待工程确认';
  return id+' · '+altitude;
}

function referenceText(reference){
  const key=String(reference??'');
  const table={
    egm2008_orthometric:'EGM2008 正高',egm2008:'EGM2008 正高',
    wgs84_ellipsoidal:'WGS84 椭球高',ellipsoidal:'WGS84 椭球高',
    agl:'距地高度（AGL）',msl:'平均海平面高（MSL）',amsl:'平均海平面高（MSL）',
    unknown:'垂向基准未确认'
  };
  if(!key)return table.unknown;
  return Object.prototype.hasOwnProperty.call(table,key)?table[key]:key;
}
