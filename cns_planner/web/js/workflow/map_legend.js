// =========================================================
// 统一地图图例（index.html 的 #legend 容器）
//
// 职责边界：
//  - 只读 flow 与图层开关，绝不写业务状态、不发请求、不改几何；
//  - **不新建 Towers 专用 legend 系统**：铁塔只是同一套图例里的一行，
//    与在线底图、起降点、参考航路、航路点同处一个容器、同一套视觉层级；
//  - 符号的唯一来源是 map/display_layers.js 的 TOWER_SYMBOL / markerSymbolSvg，
//    因此图例符号与地图符号永远出自同一份定义，不会出现第二套图标；
//  - 铁塔一行只说明"存在一个真实站址"，绝不表达覆盖、频率、功率或可用性。
// =========================================================
import {markerSymbolSvg,towerSymbolSvg} from '../map/display_layers.js';
import {
  cnsServiceLegendModel,continuousServiceLegendModel,radarCapabilityLimitationSummary,
  radarSurveillanceLayoutState,
} from '../map/cns_service_overlay.js';

export const BASEMAP_LEGEND_LABEL='在线底图';
export const BUSINESS_LEGEND_LABEL='真实业务数据';
export const TOWER_LEGEND_LABEL='通信铁塔';
//: Round 2.6：连续服务可接受性的**默认关闭**图层图例分区（名称不再带（P17）代号）。
export const CONTINUOUS_SERVICE_LEGEND_LABEL='CNS 连续服务可接受性';

// =========================================================
// Round 2.8：地图图例改为 **小型 / 可折叠 / 激活图层驱动**。
//
// 用户实机截图的三个问题与对应修复：
//  1. 图例横跨地图下方大片区域、遮挡 selected site / coverage / residual gap；
//     ⇒ 展开宽度 <= 340px、max-height <= 40vh、超长内部滚动，收起时只剩一个按钮；
//  2. 无论图层有没有开都列出全部条目（含 Radar / RID / 连续服务等未激活项），
//     真正可见的 C/RID 图例被淹没；
//     ⇒ 只有**当前激活图层**拥有的符号进入图例，`legend = 当前地图可见内容`；
//  3. 长业务说明塞进地图图例；⇒ 地图图例只回答"这个符号是什么"，长解释留在右侧面板。
// =========================================================

//: 图例分区（分组）标题 —— 展开时按此顺序与名称成组显示。
export const LEGEND_GROUP_TITLES={
  referenceRoute:'航路',
  communication:'Communication',
  rid:'RID',
  shared:'共址 / 多服务',
  siteEvidence:'站址证据',
  radar:'Radar（非合作监视）',
  navigation:'导航增强',
  surface:'地表分类',
  limitation:'限制 / 提示'
};

//: 地图上**每一个可激活图层** → 它在地图上画了什么（符号 id 列表）。
//: 这是"图例 = 当前地图可见内容"的**唯一**映射表：地图绘制与图例条目同源，
//: 新增图层的图例必须在这里登记，否则它不会被任何图例声明。
export const LEGEND_LAYER_SYMBOLS={
  referenceRoute:['reference-route'],
  referencePoints:['reference-route-point'],
  referenceLanding:['reference-landing'],
  routeProtection:['route-protection-corridor'],
  surveillanceProtection:['surveillance-protection-coverage','cns-dual-channel'],
  cnsCommunication:[
    'cns-communication','cns-gap-satisfied','cns-gap-under_redundant',
    'cns-gap-uncovered','cns-gap-unknown',
    'cns-selected-confirmed-origin','cns-selected-estimated-origin',
    'cns-service-c-communication'
  ],
  cnsRid:[
    'cns-rid-land','cns-rid-sea','cns-rid-geometry',
    'cns-selected-confirmed-origin','cns-selected-estimated-origin',
    'cns-service-s-rid-cooperative','cns-dual-channel'
  ],
  cnsNavigation:['cns-navigation-baseline','cns-service-n-rtk-augmentation'],
  radarSurveillance:['cns-service-s-radar-noncooperative','cns-dual-channel'],
  cnsServiceGap:['cns-communication','cns-gap-satisfied','cns-gap-under_redundant',
    'cns-gap-uncovered','cns-gap-unknown'],
  cnsFacilityPlan:['cns-selected-confirmed-origin','cns-selected-estimated-origin',
    'cns-legend-disclaimer'],
  surfaceFacts:['cns-surface-facts'],
  existingCns:['existing-cns-facility'],
  candidateSites:['candidate-site'],
  towers:['tower-reference'],
  legacyGaps:['cns-legacy-c-gap','cns-legacy-n-gap','cns-legacy-s-gap'],
  basemap:['online-basemap']
};

/** 图例图层键 → index.html 里的图层开关 id（前端只在这里解析一次 DOM）。 */
export const LEGEND_LAYER_CHECKBOXES={
  referenceRoute:['referenceRouteLayer'],
  referencePoints:['referenceRoutePointLayer'],
  referenceLanding:['referenceLandingLayer'],
  towers:['towerLayer'],
  routeProtection:['routeProtectionCorridorLayer'],
  surveillanceProtection:['surveillanceProtectionLayer'],
  radarSurveillance:['radarSurveillanceLayer'],
  cnsCommunication:['cnsCommunicationLayer'],
  cnsRid:['cnsRidLayer'],
  cnsNavigation:['cnsNavigationLayer'],
  cnsServiceGap:['cnsServiceGapLayer'],
  cnsFacilityPlan:['cnsFacilityPlanLayer'],
  surfaceFacts:['surfaceFactsLayer'],
  existingCns:['existingCnsLayer'],
  candidateSites:['candidateSiteLayer'],
  legacyGaps:['cLayer','nLayer','sLayer']
};

//: 没有独立开关、但**始终**画在地图上的图层（QGIS 在线底图）。
export const ALWAYS_ACTIVE_LEGEND_LAYERS=['basemap'];

/** 从 DOM 读取当前激活的图例图层集合（只读 checked 状态，绝不写业务状态）。 */
export function activeLegendLayers($){
  const active=new Set(ALWAYS_ACTIVE_LEGEND_LAYERS);
  if(!$)return active;
  for(const [key,ids] of Object.entries(LEGEND_LAYER_CHECKBOXES)){
    if(ids.some(id=>$(id)?.checked===true))active.add(key);
  }
  return active;
}

//: 与 map/display_layers.js 绘制用色一致的图例配色（只为辨认，不表示通过与否）。
const LEGEND_COLORS={
  basemap:'#8fa8bf',
  landing:'#1a8677',
  referenceRoute:'#c83f8c',
  referencePoint:'#783b69',
  tower:'#1f7a8c',
  routeProtection:'#1f6f8b',
  surveillanceProtection:'#8b1f6f'
};

function count(value){
  const number=Number(value);
  return Number.isFinite(number)?number:0;
}

/** 数据集合的一句话状态：只陈述"有没有载入"，不做任何能力结论。 */
function collectionNote(collection,unit){
  const total=count(collection?.count);
  const status=collection?.status||'not_calculated';
  if(status==='passed'&&total>0)return total+' '+unit;
  if(status==='not_calculated'||!collection)return '未配置';
  if(status==='missing_data')return '没有可用记录';
  if(status==='requires_xlsx_or_csv_conversion')return '需先转换格式';
  return status;
}

/**
 * 图例模型：全部行都属于同一个 legend。
 *
 * @param {{flow:object,towerLayerOn?:boolean,routeProtectionLayerOn?:boolean,
 *          surveillanceProtectionLayerOn?:boolean}} input
 * @returns {{groups:Array<{title:string,lines:Array<object>}>}}
 */
export function mapLegendModel({
  flow,towerLayerOn=false,routeProtectionLayerOn=false,surveillanceProtectionLayerOn=false
}={}){
  const towers=flow?.towers||{},routes=flow?.reference_routes||{},landing=flow?.reference_landing_sites||{};
  const pointCount=routes?.point_count??(routes?.points||[]).length;
  return {
    groups:[
      {
        title:BASEMAP_LEGEND_LABEL,
        lines:[
          {id:'online-basemap',label:'在线底图（QGIS 服务瓦片）',symbol:markerSymbolSvg('square',{size:14,fill:'#ffffff',stroke:LEGEND_COLORS.basemap,strokeWidth:1.6})},
        ]
      },
      {
        title:BUSINESS_LEGEND_LABEL,
        lines:[
          {id:'reference-landing',label:'起降点',symbol:markerSymbolSvg('diamond',{size:14,fill:LEGEND_COLORS.landing,stroke:'#ffffff',strokeWidth:1.2}),note:collectionNote(landing,'个')},
          {id:'reference-route',label:'真实参考航路',symbol:'<span class="legend-stroke" style="border-top-color:'+LEGEND_COLORS.referenceRoute+'"></span>',note:collectionNote(routes,'条')},
          {id:'reference-route-point',label:'真实航路点',symbol:markerSymbolSvg('triangle',{size:14,fill:LEGEND_COLORS.referencePoint,stroke:'#ffffff',strokeWidth:1.2}),note:collectionNote({status:pointCount>0?'passed':routes?.status,count:pointCount},'个')},
          // MAP-TOWER-SYMBOL-V2：图例铁塔与地图符号共用同一份 TOWER_SYMBOL 几何，
          // 尺寸同样按**可见高度 px** 给出（20 px，与地图 detail 档同量级），线条清晰。
          {id:'tower-reference',label:TOWER_LEGEND_LABEL,symbol:towerSymbolSvg({size:20,color:LEGEND_COLORS.tower,strokePx:1.8}),note:collectionNote(towers,'个'),state:towerLayerOn?'图层已打开':'图层默认关闭'}
        ]
      },
      // Round 2.6：连续服务可接受性的**默认关闭**图层。
      // 与铁塔一行同一策略：条目常显，但用 state 字段如实说明当前开关状态
      // （默认关闭 / 已打开），绝不暗示这些几何是默认显示的业务事实。
      // 图例必须**显式区分且不得混同**三类：合作监视（RID）/ 非合作监视（Radar 补充）/
      // 能力限制（黄色 / 橙色，不是系统错误）。
      {
        title:CONTINUOUS_SERVICE_LEGEND_LABEL,
        lines:[
          {id:'route-protection-corridor',label:'水平航路保护走廊（D_separation / D_maneuver / D_uncertainty）',
            symbol:'<span class="legend-stroke" style="border-top-color:'+LEGEND_COLORS.routeProtection+';border-top-style:solid"></span>',
            note:'走廊外半宽来自后端 outer_half_width_m；四分量与公式来自后端 corridor，前端不推导。'
              +'D_maneuver 的身份是 engineering_baseline（不是法规值）',
            state:routeProtectionLayerOn?'图层已打开':'图层默认关闭'},
          {id:'surveillance-protection-coverage',label:'监视保护覆盖（探测缺口段 / T_margin）',
            symbol:'<span class="legend-stroke" style="border-top-color:'+LEGEND_COLORS.surveillanceProtection+'"></span>',
            note:'虚线为后端 surveillance_detection_gap 里程段；两端监视点按 first_detection_evidence.usable 着色。'
              +'该图层只表示**已经存在的真实几何**，不存在的覆盖一律不画',
            state:surveillanceProtectionLayerOn?'图层已打开':'图层默认关闭'},
          ...continuousServiceLegendModel({flow,radarState:radarSurveillanceLayoutState(flow)}),
        ],
        //: Radar 求解不可行时，图例里如实给出真实候选 / 限制摘要（绝不伪造扇区）。
        note:radarCapabilityLimitationSummary(flow)||null,
      }
    ]
  };
}

function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,character=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
}

// =========================================================
// Round 2.8：**分区（分组）+ 激活图层驱动**的图例渲染。
// =========================================================

/** 图例项 → 它的分组。键必须与 :data:`LEGEND_GROUP_TITLES` 对齐。 */
const LEGEND_ITEM_GROUP={
  'online-basemap':'basemap',
  'reference-landing':'referenceRoute','reference-route':'referenceRoute',
  'reference-route-point':'referenceRoute',
  'tower-reference':'siteEvidence',
  'route-protection-corridor':'limitation',
  'surveillance-protection-coverage':'limitation',
  'cns-dual-channel':'limitation',
  'cns-communication':'communication',
  'cns-service-c-communication':'communication',
  'cns-rid-land':'rid','cns-rid-sea':'rid','cns-rid-geometry':'rid',
  'cns-service-s-rid-cooperative':'rid',
  'cns-selected-confirmed-origin':'shared','cns-selected-estimated-origin':'shared',
  'existing-cns-facility':'siteEvidence','candidate-site':'siteEvidence',
  'cns-surface-facts':'surface',
  'cns-navigation-baseline':'navigation','cns-service-n-rtk-augmentation':'navigation',
  'cns-service-s-radar-noncooperative':'radar',
  'cns-legend-disclaimer':'limitation'
};

//: ``cns-gap-*``（P15/P17 缺口状态）按前缀归组，避免新增状态时漏登记。
const LEGEND_ITEM_GROUP_PREFIX=[
  ['cns-gap-','communication'],
  ['cns-legacy-','limitation']
];

function legendItemGroup(id){
  const key=String(id||'');
  if(LEGEND_ITEM_GROUP[key])return LEGEND_ITEM_GROUP[key];
  for(const [prefix,group] of LEGEND_ITEM_GROUP_PREFIX){
    if(key.startsWith(prefix))return group;
  }
  return 'limitation';
}

/** 分组展示顺序：航路 → C → RID → 共址 → 站址证据 → 其它 → 限制/提示。 */
const LEGEND_GROUP_ORDER=[
  'referenceRoute','communication','rid','shared','siteEvidence',
  'radar','navigation','surface','basemap','limitation'
];

/** 从图例图层键反查它携带的符号 id 集合。 */
function symbolsForLayers(activeLayers){
  const symbols=new Set();
  for(const layer of activeLayers||[]){
    for(const id of LEGEND_LAYER_SYMBOLS[layer]||[])symbols.add(id);
  }
  return symbols;
}

/**
 * 把扁平符号行按 :data:`LEGEND_ITEM_GROUP` **分组**，并**只保留激活图层**拥有的行。
 *
 * @param {Array<object>} lines 全部符号行（每个都带 `id`）
 * @param {Set<string>} activeSymbols 当前激活图层携带的符号 id
 * @returns {Array<{key:string,title:string,lines:Array<object>,notes:Array<string>}>}
 */
export function groupLegendLines(lines, activeSymbols){
  const groups=new Map();
  for(const line of lines||[]){
    if(activeSymbols&&!activeSymbols.has(line.id))continue;
    const key=legendItemGroup(line.id);
    if(!groups.has(key))groups.set(key,{
      key,title:LEGEND_GROUP_TITLES[key]||key,lines:[],notes:[]
    });
    const group=groups.get(key);
    group.lines.push(line);
    if(line.note)group.notes.push(line.note);
  }
  return LEGEND_GROUP_ORDER
    .filter(key=>groups.has(key))
    .map(key=>groups.get(key))
    .concat([...groups.keys()]
      .filter(key=>!LEGEND_GROUP_ORDER.includes(key))
      .map(key=>groups.get(key)));
}

/**
 * 渲染**紧凑 / 分组 / 激活图层驱动**的地图图例。
 *
 * 与 :func:`renderMapLegend` 的区别（两个都保留）：
 *
 * * ``renderMapLegend`` —— 旧式扁平渲染（"全部条目常显 + 逐条 state 说明开关"），
 *   仍被既有回归测试与旧调用点使用；
 * * ``renderCompactLegend`` —— Round 2.8 的正式地图图例：只渲染**当前激活图层**拥有
 *   的符号，按业务分组，长解释不进入地图（只回答"这个符号是什么"）。
 */
export function renderCompactLegend(groups,{collapsed=true,activeLayerCount=0}={}){
  const items=(groups||[]).reduce((sum,group)=>sum+group.lines.length,0);
  const head=[
    '<div class="map-legend-head" id="mapLegendToggle" role="button" tabindex="0"',
    ' aria-expanded="'+(collapsed?'false':'true')+'"',
    ' aria-controls="mapLegendBody" data-legend-collapsed="'+(collapsed?'true':'false')+'">',
    '<span class="map-legend-title">图例</span>',
    '<span class="map-legend-count" data-legend-count="'+items+'">',
    String(activeLayerCount),' 个激活图层</span>',
    '<span class="map-legend-caret" aria-hidden="true">'+(collapsed?'▸':'▾')+'</span>',
    '</div>'
  ].join('');
  if(collapsed){
    return '<div class="map-legend map-legend-collapsed" id="mapLegend">'+head+'</div>';
  }
  const body=(groups||[]).map(group=>[
    '<div class="map-legend-group" data-legend-group="'+escapeHtml(group.key)+'">',
    '<div class="map-legend-group-label">'+escapeHtml(group.title)+'</div>',
    group.lines.map(line=>[
      '<div class="map-legend-line" data-legend-id="'+escapeHtml(line.id)+'"',
      line.limitation?' data-capability-limitation="true"':'','>',
      '<span class="map-legend-symbol">'+line.symbol+'</span>',
      '<span class="map-legend-label">'+escapeHtml(line.label)+'</span>',
      '</div>'
    ].join('')).join(''),
    '</div>'
  ].join('')).join('');
  return [
    '<div class="map-legend" id="mapLegend">',head,
    '<div class="map-legend-body" id="mapLegendBody">',
    items?body:'<div class="map-legend-empty">当前没有激活的可绘制图层</div>',
    '</div></div>'
  ].join('');
}

/**
 * 把**扁平符号行**（旧模型 + CNS service 模型）整理成分组模型。
 *
 * ``activeLayers`` 缺省时**不**做过滤（保持既有回归测试与旧调用点逐字段不变）；
 * ``updateMapLegend`` 总是显式传入从 DOM 读取的激活图层集合。
 */
export function compactLegendGroups(lines,activeLayers){
  const symbols=activeLayers===undefined||activeLayers===null
    ? null : symbolsForLayers(activeLayers);
  return groupLegendLines(lines,symbols);
}

function escapeHtmlLegend(value){
  return escapeHtml(value);
}

//: 图例展开 / 收起状态的持久化键（只存一个布尔 UI 偏好，绝不写业务状态）。
export const MAP_LEGEND_UI_STORAGE_KEY='cns.mapLegend.ui.v1';

/** 读取图例 UI 偏好；**默认收起**（Round 2.8 的硬要求）。 */
export function readMapLegendUiState(storage){
  const store=storage||resolveLegendStorage();
  try{
    const parsed=JSON.parse(store?.getItem(MAP_LEGEND_UI_STORAGE_KEY)||'{}');
    return {collapsed:parsed?.collapsed!==false};
  }catch(_){
    return {collapsed:true};
  }
}

export function writeMapLegendUiState(value,storage){
  const store=storage||resolveLegendStorage();
  const state={collapsed:value?.collapsed!==false};
  try{store?.setItem(MAP_LEGEND_UI_STORAGE_KEY,JSON.stringify(state));}catch(_){/* UI 偏好 */}
  return state;
}

function resolveLegendStorage(storage){
  if(storage)return storage;
  try{
    return globalThis.document?.defaultView?.localStorage||globalThis.localStorage;
  }catch(_){return null;}
}

/**
 * Round 2.8 正式入口：把**当前激活图层**驱动的小型图例写入 ``#mapLegend``。
 *
 * 行为契约：
 *
 * * 全部符号行来自同一份来源（:func:`mapLegendModel` + :func:`cnsServiceLegendModel`），
 *   因此地图符号与图例符号不会出现第二套；
 * * 只有**当前激活图层**拥有的行会渲染（``legend = 当前地图可见内容``）；
 * * 默认收起，只留一个 ``图例 · N`` 按钮；展开后按业务分组、限宽限高可滚动；
 * * 用户手动展开 / 收起会被记住（``cns.mapLegend.ui.v1``）。
 *
 * @returns {{rendered:boolean,collapsed:boolean,groupCount:number,lineCount:number}}
 */
export function updateCompactMapLegend({$,flow,storage}={}){
  const target=$?.('mapLegend');
  if(!target)return {rendered:false,collapsed:true,groupCount:0,lineCount:0};
  const active=activeLegendLayers($);
  const business=mapLegendModel({
    flow,
    towerLayerOn:$('towerLayer')?.checked===true,
    routeProtectionLayerOn:$('routeProtectionCorridorLayer')?.checked===true,
    surveillanceProtectionLayerOn:$('surveillanceProtectionLayer')?.checked===true
  });
  const lines=[
    ...business.groups.flatMap(group=>group.lines),
    ...cnsServiceLegendModel()
  ];
  const groups=compactLegendGroups(lines,[...active]);
  const ui=readMapLegendUiState(storage);
  const html=renderCompactLegend(groups,{
    collapsed:ui.collapsed,activeLayerCount:active.size
  });
  target.innerHTML=html;
  target.hidden=false;
  //: 新图例接管后，旧的一体式图例必须整体让位：绝不出现"两套图例同时占地图"。
  const legacy=$?.('legend');
  if(legacy)legacy.hidden=true;
  const toggle=target.querySelector?.('#mapLegendToggle')||null;
  const bind=()=>{
    const next=!readMapLegendUiState(storage).collapsed;
    writeMapLegendUiState({collapsed:next},storage);
    updateCompactMapLegend({$,flow,storage});
  };
  if(toggle){
    toggle.onclick=bind;
    toggle.onkeydown=event=>{
      if(event?.key==='Enter'||event?.key===' '){event.preventDefault?.();bind();}
    };
  }
  return {
    rendered:true,collapsed:ui.collapsed,
    groupCount:groups.length,
    lineCount:groups.reduce((sum,group)=>sum+group.lines.length,0)
  };
}

/** 渲染统一图例的真实业务数据分区（返回 HTML，便于测试直接断言）。 */
export function renderMapLegend(model){
  return (model?.groups||[]).map(group=>
    '<div class="legend-group-label">'+escapeHtml(group.title)+'</div>'
    +group.lines.map(line=>
      '<div class="legend-line" data-legend-id="'+escapeHtml(line.id)+'"'
      +(line.limitation?' data-capability-limitation="true"':'')+'>'
      +'<span class="legend-symbol">'+line.symbol+'</span>'
      +'<span class="legend-label">'+escapeHtml(line.label)+'</span>'
      +(line.note?'<small>'+escapeHtml(line.note)+'</small>':'')
      +(line.state?'<small class="legend-state">'+escapeHtml(line.state)+'</small>':'')
      +'</div>'
    ).join('')
    +(group.note?'<div class="legend-group-note" data-legend-note="true"><small>'
      +escapeHtml(group.note)+'</small></div>':'')
  ).join('');
}

/**
 * 把统一图例写入 #legend 内的分区容器。
 *
 * @param {{$:Function,flow:object}} input
 * @returns {boolean} 是否完成渲染
 */
export function updateMapLegend({$,flow}){
  const target=$('businessLegend');
  if(!target)return false;
  const model=mapLegendModel({
    flow,
    towerLayerOn:$('towerLayer')?.checked===true,
    routeProtectionLayerOn:$('routeProtectionCorridorLayer')?.checked===true,
    surveillanceProtectionLayerOn:$('surveillanceProtectionLayer')?.checked===true
  });
  target.innerHTML=renderMapLegend(model);
  target.hidden=false;
  return true;
}

/**
 * Round 3：CNS service 图例（Communication / RID / 缺口状态）。
 *
 * 独立容器（``#cnsServiceLegend``），只在**至少一个** CNS service 图层打开时显示；
 * 首次打开不会因为新增图例而改变默认地图显示。图例明确区分
 * Communication / RID Cooperative Surveillance / Radar Non-cooperative Surveillance，
 * 并且**只**把 5 km 虚线称为「RID 海上最大规划半径」。
 */
export function updateCnsServiceLegend({$,flow}){
  const target=$('cnsServiceLegend');
  if(!target)return false;
  const lines=cnsServiceLegendModel();
  const open=['cnsCommunicationLayer','cnsRidLayer','cnsNavigationLayer','cnsServiceGapLayer','cnsFacilityPlanLayer','surfaceFactsLayer']
    .some(id=>$(id)?.checked===true);
  target.hidden=!open;
  if(!open)return false;
  target.innerHTML='<div class="legend-group-label">CNS service</div>'
    +lines.map(line=>'<div class="legend-line" data-legend-id="'+escapeHtml(line.id)+'">'
      +'<span class="legend-symbol">'+line.symbol+'</span>'
      +'<span class="legend-label">'+escapeHtml(line.label)+'</span>'
      +(line.note?'<small>'+escapeHtml(line.note)+'</small>':'')
      +'</div>').join('');
  return true;
}
