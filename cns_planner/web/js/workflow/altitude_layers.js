/**
 * =========================================================================
 * 统一 AltitudeLayer 选择器（B4X 第 9 节）
 * =========================================================================
 *
 * 唯一事实来源是**项目实际的 AltitudeLayer Catalog**
 * （`flow.spatial_3d.altitude_layers`，由 `spatial_3d_service` /
 * `route_operating_layer_service` 维护）。Step2 与 Step3 必须使用**同一个**
 * selector，因此选项构造、排序、禁用判定与中文显示全部收敛在本模块，
 * 任何步骤都不得再手写第二个下拉。
 *
 * 硬规则
 * ------
 *  1. **绝不写死 ALT-080 / 80 m**：选项完全来自目录；目录为空时显示明确的空状态，
 *     而不是伪造一条默认高度层。
 *  2. `ALT-080 · 80 m · EGM2008 正高`：垂向基准必须转成用户可理解中文，
 *     绝不直接显示 `egm2008_orthometric`。
 *  3. 缺 `nominal_altitude_m` 的层仍然**出现在可选列表中**（用户需要看到它的存在），
 *     但标注为「nominal 高度待工程确认」；它不能被当作有效规划高度层使用——
 *     后端 `_altitude_layer()` 会拒绝，前端只如实预告，绝不替用户填值。
 *  4. 已确认与否只看后端 `status` / `confirmed`，前端**不推断确认**。
 *
 * 本模块是纯逻辑 + 字符串构造：不读 DOM、不发请求、不写业务状态。
 */
import {altitudeLayerLabel,formatAltitude,verticalReferenceText,statusText} from './presentation.js';

function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,function(character){
    if(character==='&')return '&amp;';
    if(character==='<')return '&lt;';
    if(character==='>')return '&gt;';
    if(character==='"')return '&quot;';
    return '&#39;';
  });
}

/** 目录条目是否具备被规划使用的完整参数（id + nominal + 垂向基准 + 已确认）。 */
export function altitudeLayerUsable(layer){
  const item=layer&&typeof layer==='object'?layer:{};
  const id=String(item.altitude_layer_id||'').trim();
  const nominal=Number(item.nominal_altitude_m);
  const reference=String(item.vertical_reference||'').trim();
  return Boolean(id)
    && Number.isFinite(nominal)
    && Boolean(reference) && reference!=='unknown'
    && altitudeLayerConfirmed(item);
}

/** 层是否已被工程确认。只读后端字段，不做任何推断。 */
export function altitudeLayerConfirmed(layer){
  const item=layer&&typeof layer==='object'?layer:{};
  if(item.confirmed===false)return false;
  if(item.confirmed===true)return true;
  const status=String(item.status||'');
  // 后端 `_altitude_layer` 接受 confirmed / passed；其余一律视为未确认（fail-closed）。
  return status==='confirmed'||status==='passed';
}

/**
 * 目录读取（唯一入口）。
 * @param {object} flow
 * @returns {Array<object>} 目录条目（原顺序保留，不排序、不补默认层）
 */
export function altitudeLayerCatalog(flow){
  const layers=flow&&flow.spatial_3d?flow.spatial_3d.altitude_layers:null;
  return Array.isArray(layers)?layers.filter(function(item){return item&&typeof item==='object';}):[];
}

/** 按 nominal 高度排序的目录（仅用于展示顺序；不改变目录本身）。 */
export function sortedAltitudeLayers(flow){
  return altitudeLayerCatalog(flow).slice().sort(function(left,right){
    const a=Number(left.nominal_altitude_m),b=Number(right.nominal_altitude_m);
    if(Number.isFinite(a)&&Number.isFinite(b)&&a!==b)return a-b;
    return String(left.altitude_layer_id||'').localeCompare(String(right.altitude_layer_id||''));
  });
}

/** 目录摘要：总数 / 可用数 / 待确认数（只读计数，不推断）。 */
export function altitudeLayerCatalogSummary(flow){
  const layers=altitudeLayerCatalog(flow);
  const usable=layers.filter(altitudeLayerUsable);
  return {
    total:layers.length,
    usable:usable.length,
    pending:layers.length-usable.length,
    hasUsable:usable.length>0,
    defaultId:usable.length?String(usable[0].altitude_layer_id||''):''
  };
}

/**
 * 高度层的**一行**中文下拉文本。例如 `ALT-080 · 80 m · EGM2008 正高`。
 * 未确认 / 缺 nominal 时追加明确标注，绝不静默混入可用层。
 */
export function altitudeLayerOptionLabel(layer){
  const base=altitudeLayerLabel(layer);
  const notes=[];
  if(!Number.isFinite(Number(layer&&layer.nominal_altitude_m)))notes.push('nominal 高度待工程确认');
  else if(!altitudeLayerConfirmed(layer))notes.push('垂向基准待工程确认');
  return notes.length?base+'（'+notes.join('；')+'）':base;
}

/** 用户可理解的高度层详情行（供状态卡使用）。 */
export function altitudeLayerDetailRows(layer){
  const item=layer&&typeof layer==='object'?layer:{};
  const lower=item.lower_altitude_m,upper=item.upper_altitude_m;
  const range=(Number.isFinite(Number(lower))&&Number.isFinite(Number(upper)))
    ?formatAltitude(lower)+' 至 '+formatAltitude(upper)
    :'未配置';
  const nominal=Number.isFinite(Number(item.nominal_altitude_m))
    ?formatAltitude(item.nominal_altitude_m)
    :'待工程确认';
  return [
    {label:'高度层',value:String(item.altitude_layer_id||'—')},
    {label:'名称',value:String(item.name||item.altitude_layer_id||'—')},
    {label:'nominal 高度',value:nominal},
    {label:'垂向基准',value:verticalReferenceText(item.vertical_reference)},
    {label:'高度范围',value:range},
    {label:'状态',value:statusText(item.status||'pending_confirmation')}
  ];
}

/**
 * 选项 HTML。第一个 option 是显式的"请选择"，**不预选任何高度层**——
 * 系统不为任何航路自动选择或分配默认高度。
 */
export function altitudeLayerOptions(flow,options){
  const settings=options||{};
  const selected=String(settings.selected||'');
  const includeUnusable=settings.includeUnusable!==false;
  const all=sortedAltitudeLayers(flow);
  const layers=includeUnusable?all:all.filter(altitudeLayerUsable);
  const rows=layers.map(function(layer){
    const id=String(layer.altitude_layer_id||'');
    const usable=altitudeLayerUsable(layer);
    return '<option value="'+escapeHtml(id)+'"'+(id===selected?' selected':'')
      +(usable?'':' data-usable="false"')
      +'>'+escapeHtml(altitudeLayerOptionLabel(layer))+'</option>';
  });
  return '<option value="">请选择固定巡航高度层（不猜、不自动分配）</option>'+rows.join('');
}

/** 目录为空时的明确说明（不是"失败"，也不是"请稍后重试"）。 */
export const ALTITUDE_LAYER_EMPTY_NOTE=
  '高度层目录为空（共 0 层）：没有可选高度层。请先在「环境与风险」的高度层区'
  +'按工程依据补建 AltitudeLayer；系统不会自动选择或推断任何高度。';

/**
 * 统一的 AltitudeLayer selector（Step2 与 Step3 共用同一个实现）。
 *
 * 跨重渲染保持选择：选中的 raw id 缓存在本模块内（纯 UI 状态），
 * `main.js` 的 renderWorkflow 重新挂载面板时会回填，用户不会因为一次 mutation
 * 就丢掉自己选的高度层。
 */
export function altitudeLayerSelector(options){
  const settings=options||{};
  const flow=settings.flow;
  const id=String(settings.id||'altitudeLayerSelector');
  const label=String(settings.label||'固定巡航高度层');
  const note=String(settings.note||'');
  const includeUnusable=settings.includeUnusable!==false;
  const attribute=String(settings.attribute||'data-altitude-layer-selector');
  const summary=altitudeLayerCatalogSummary(flow);
  const chosen=resolveAltitudeLayerSelection({flow:flow,selected:settings.selected,id:id});
  if(!summary.total){
    return '<div class="altitude-layer-selector">'
      +'<div class="wb-empty">'+escapeHtml(ALTITUDE_LAYER_EMPTY_NOTE)+'</div></div>';
  }
  const current=altitudeLayerCatalog(flow).find(function(item){
    return String(item.altitude_layer_id||'')===chosen;
  })||null;
  const confirmNote=(current&&!altitudeLayerUsable(current))
    ?'<div class="parameter-note">已选高度层尚未完成工程确认（'+escapeHtml(altitudeLayerOptionLabel(current))
      +'）：它可以在界面中被选择与查看，但<b>不能</b>用于生成正式约束场或正式航路；'
      +'请先在「环境与风险」的高度层区补齐 nominal 高度与垂向基准并确认。</div>'
    :'';
  return '<div class="altitude-layer-selector">'
    +'<label for="'+escapeHtml(id)+'">'+escapeHtml(label)+'</label>'
    +'<select class="panel-input" id="'+escapeHtml(id)+'" '+escapeHtml(attribute)
      +' data-altitude-layer-count="'+summary.total+'">'
    +altitudeLayerOptions(flow,{selected:chosen,includeUnusable:includeUnusable})+'</select>'
    +'<div class="altitude-layer-catalog-note">可选高度层 '+summary.total+' 层（可用 '
      +summary.usable+' / 待工程确认 '+summary.pending+'）</div>'
    +(note?'<div class="parameter-note">'+escapeHtml(note)+'</div>':'')
    +confirmNote
    +'</div>';
}

// ---- 选择状态的唯一缓存（纯 UI，不写业务状态） ------------------------------

//: selector id 到用户最后一次显式选择的 altitude_layer_id。
const selectionCache=new Map();

/** 记录一次显式选择（由 bind 的 onchange 调用）。 */
export function rememberAltitudeLayerSelection(id,value){
  const key=String(id||'');
  if(!key)return '';
  const next=String(value||'');
  selectionCache.set(key,next);
  return next;
}

/** 读取缓存的显式选择。 */
export function cachedAltitudeLayerSelection(id){
  return selectionCache.get(String(id||''))||'';
}

/** 清空缓存（打开另一个项目 / 重开工作区时调用）。 */
export function clearAltitudeLayerSelection(){selectionCache.clear();}

/**
 * 解析应当显示为选中的高度层：
 * 显式 selected，其次模块缓存，最后目录里第一个可用层。
 * **绝不**编造一个不在目录中的 id。
 */
export function resolveAltitudeLayerSelection(options){
  const settings=options||{};
  const layers=altitudeLayerCatalog(settings.flow);
  const ids=new Set(layers.map(function(item){return String(item.altitude_layer_id||'');}));
  const candidates=[settings.selected,cachedAltitudeLayerSelection(settings.id)];
  for(const value of candidates){
    const key=String(value||'');
    if(key&&ids.has(key))return key;
  }
  const first=layers.find(altitudeLayerUsable);
  return first?String(first.altitude_layer_id||''):'';
}

/** 读取 selector 当前值（DOM 到 raw id），没有该元素时返回空串。 */
export function selectedAltitudeLayerId(document,id){
  const key=String(id||'altitudeLayerSelector');
  const node=document&&document.getElementById?document.getElementById(key):null;
  return node?String(node.value||''):'';
}

/** 绑定 selector：记录选择并回调（`onchange` 赋值，不叠加监听器）。 */
export function bindAltitudeLayerSelector(document,options){
  const settings=options||{};
  const id=String(settings.id||'altitudeLayerSelector');
  const node=document&&document.getElementById?document.getElementById(id):null;
  if(!node)return null;
  node.onchange=function(){
    const value=rememberAltitudeLayerSelection(id,node.value);
    if(typeof settings.onChange==='function')settings.onChange(value);
  };
  return node;
}

/** 供测试使用：selector 在给定 flow 下应显示的选中 id。 */
export function altitudeLayerSelection(options){
  return resolveAltitudeLayerSelection(options||{});
}
