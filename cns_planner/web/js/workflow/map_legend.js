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

export const BASEMAP_LEGEND_LABEL='在线底图';
export const BUSINESS_LEGEND_LABEL='真实业务数据';
export const TOWER_LEGEND_LABEL='通信铁塔';

//: 与 map/display_layers.js 绘制用色一致的图例配色（只为辨认，不表示通过与否）。
const LEGEND_COLORS={
  basemap:'#8fa8bf',
  landing:'#1a8677',
  referenceRoute:'#c83f8c',
  referencePoint:'#783b69',
  tower:'#1f7a8c'
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
 * @param {{flow:object,towerLayerOn?:boolean}} input
 * @returns {{groups:Array<{title:string,lines:Array<object>}>}}
 */
export function mapLegendModel({flow,towerLayerOn=false}={}){
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
      '<div class="legend-line" data-legend-id="'+escapeHtml(line.id)+'">'
      +'<span class="legend-symbol">'+line.symbol+'</span>'
      +'<span class="legend-label">'+escapeHtml(line.label)+'</span>'
      +(line.note?'<small>'+escapeHtml(line.note)+'</small>':'')
      +(line.state?'<small class="legend-state">'+escapeHtml(line.state)+'</small>':'')
      +'</div>'
    ).join('')
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
  const model=mapLegendModel({flow,towerLayerOn:$('towerLayer')?.checked===true});
  target.innerHTML=renderMapLegend(model);
  target.hidden=false;
  return true;
}
