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
