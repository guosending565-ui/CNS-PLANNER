import {escapeHtml,statusBadge} from './common.js';

const finite=value=>Number.isFinite(value);
const fmt=(value,digits=1)=>finite(value)?Number(value).toFixed(digits):'—';

export function profileChart(profile){
  if(!profile)return '<div class="empty-note">尚无剖面。请选择 passed 运行航路并执行生成；缺少 FABDEM、高度确认或净空证据时会明确显示原因。</div>';
  const samples=profile.samples||[],usable=samples.filter(item=>finite(item.ground_egm2008_m)||finite(item.flight_egm2008_m));
  if(!samples.length)return '<div class="empty-note">'+escapeHtml((profile.reasons||['没有可绘制样本']).join('；'))+'</div>';
  const width=720,height=260,pad={l:48,r:16,t:18,b:34},length=Math.max(1,profile.route_length_m||0);
  const intervalValues=(profile.building_intervals||[]).flatMap(item=>[item.roof_elevation_m,finite(item.roof_elevation_m)&&finite(profile.required_vertical_clearance_m)?item.roof_elevation_m+profile.required_vertical_clearance_m:null]).filter(finite);
  const values=[...usable.flatMap(item=>[item.ground_egm2008_m,item.flight_egm2008_m]),...intervalValues].filter(finite);
  if(!values.length)return '<div class="empty-note">样本存在，但 FABDEM ground / flight EGM2008 均不可解析：'+escapeHtml((profile.reasons||[]).join('；'))+'</div>';
  let ymin=Math.min(...values),ymax=Math.max(...values);const margin=Math.max(5,(ymax-ymin)*.08);ymin-=margin;ymax+=margin;
  const x=value=>pad.l+Math.max(0,Math.min(length,value||0))/length*(width-pad.l-pad.r),y=value=>pad.t+(ymax-value)/(ymax-ymin)*(height-pad.t-pad.b);
  const path=key=>samples.filter(item=>finite(item[key])).map((item,index)=>(index?'L':'M')+x(item.distance_m).toFixed(1)+' '+y(item[key]).toFixed(1)).join(' ');
  const intervals=(profile.building_intervals||[]).filter(item=>finite(item.roof_elevation_m)).map(item=>{const breach=item.status==='breach',roof=item.roof_elevation_m,ground=finite(item.ground_elevation_m)?item.ground_elevation_m:ymin,top=finite(profile.required_vertical_clearance_m)?roof+profile.required_vertical_clearance_m:roof,ix=x(item.start_distance_m),iw=Math.max(1,x(item.end_distance_m)-ix),color=breach?'#d43b35':'#8b6b2f',title=escapeHtml(item.building_id||'building')+' · roof '+fmt(roof)+' m · '+escapeHtml(item.status||'unknown');return '<g><rect x="'+ix.toFixed(1)+'" y="'+y(roof).toFixed(1)+'" width="'+iw.toFixed(1)+'" height="'+Math.max(1,y(ground)-y(roof)).toFixed(1)+'" fill="'+color+'" opacity=".35"><title>'+title+'</title></rect><rect x="'+ix.toFixed(1)+'" y="'+y(top).toFixed(1)+'" width="'+iw.toFixed(1)+'" height="'+Math.max(1,y(roof)-y(top)).toFixed(1)+'" fill="'+color+'" opacity=".14" stroke="'+color+'" stroke-dasharray="3 2"><title>'+title+' · required clearance '+fmt(profile.required_vertical_clearance_m)+' m</title></rect></g>';}).join('');
  const ticks=[ymin,(ymin+ymax)/2,ymax].map(value=>'<text x="4" y="'+(y(value)+4).toFixed(1)+'" font-size="11" fill="#566">'+fmt(value,0)+' m</text>').join('');
  return '<svg data-profile-svg viewBox="0 0 '+width+' '+height+'" role="img" aria-label="航路三维安全剖面" style="width:100%;min-height:220px;background:#f7faf9;border:1px solid #ccd8d4">'+ticks+intervals+'<path d="'+path('ground_egm2008_m')+'" fill="none" stroke="#6b5b3e" stroke-width="3"/><path d="'+path('flight_egm2008_m')+'" fill="none" stroke="#0873cb" stroke-width="3"/><line data-profile-cursor x1="0" x2="0" y1="'+pad.t+'" y2="'+(height-pad.b)+'" stroke="#222" stroke-dasharray="3 3" visibility="hidden"/><text x="'+pad.l+'" y="'+(height-8)+'" font-size="11">0</text><text x="'+(width-pad.r-72)+'" y="'+(height-8)+'" font-size="11">'+fmt(length,0)+' m</text></svg>';
}

export function renderRouteVerticalProfilePanel(collection={},routes=[]){
  const profiles=collection.profiles||[],routeIds=[...new Set([...(routes||[]).filter(item=>item.status==='passed').map(item=>item.route_id),...profiles.map(item=>item.route_id)])],first=profiles.find(item=>item.route_id===routeIds[0])||profiles[0],options=routeIds.map(routeId=>{const profile=profiles.find(item=>item.route_id===routeId);return '<option value="'+escapeHtml(routeId)+'">'+escapeHtml(routeId)+' · '+escapeHtml(profile?.status||'未生成')+'</option>';}).join('');
  const summary=first?'<div class="flow-summary" data-profile-summary>'+profileSummary(first)+'</div>':'<div class="flow-summary" data-profile-summary>'+statusBadge(collection.status||'not_calculated')+' · '+escapeHtml((collection.reasons||['尚未生成']).join('；'))+'</div>';
  return '<h3>航路三维安全剖面</h3><div class="parameter-note">只读 visualization profile：FABDEM DTM (EGM2008) + confirmed route altitude；建筑与 breach 仅复用 BuildingClearanceV1 精确证据，采样不参与安全判定。</div><div class="button-row"><select id="verticalProfileRoute">'+options+'</select><button class="secondary" id="evaluateVerticalProfile">生成/刷新剖面</button></div>'+summary+'<div data-profile-chart>'+profileChart(first)+'</div><div class="flow-summary" data-profile-tooltip>悬停曲线查看 distance / lon,lat / ground / flight / AGL / clearance / status；对应位置会在二维地图标记。</div><div class="parameter-note">棕线 FABDEM ground；蓝线 flight EGM2008；建筑区间与 required clearance/breach 仅作既有证据可视化。</div>';
}

export function bindRouteVerticalProfile(c){
  const select=c.$('verticalProfileRoute'),collection=c.flow().route_vertical_profiles||{};
  const show=()=>{const profile=(collection.profiles||[]).find(item=>item.route_id===select?.value),chart=document.querySelector('[data-profile-chart]'),summary=document.querySelector('[data-profile-summary]');if(chart)chart.innerHTML=profileChart(profile);if(summary&&profile)summary.innerHTML=profileSummary(profile);bindHover(profile,c);};
  if(select)select.onchange=show;
  c.actionButton('evaluateVerticalProfile',()=>c.resourceAction('/api/route-vertical-profiles/evaluate',{route_id:select?.value||null}));
  show();
}

function profileSummary(profile){return statusBadge(profile.status)+' · '+fmt(profile.route_length_m)+' m · '+(profile.sampling?.sample_count||0)+' samples · spacing '+fmt(profile.sampling?.actual_spacing_m)+' m<br>building intervals '+(profile.building_intervals||[]).length+' · breach intervals '+(profile.breach_intervals||[]).length+' · critical '+escapeHtml((profile.critical_buildings||[]).map(item=>item.building_id).join(', ')||'无')+' · vertical reference '+escapeHtml(profile.vertical_reference||'unknown');}

function bindHover(profile,c){
  const svg=document.querySelector('[data-profile-svg]'),tooltip=document.querySelector('[data-profile-tooltip]');if(!svg||!profile?.samples?.length)return;
  const cursor=svg.querySelector('[data-profile-cursor]'),samples=profile.samples,width=720,left=48,right=16,usable=width-left-right;
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),svgX=(event.clientX-rect.left)/Math.max(1,rect.width)*width,distance=Math.max(0,Math.min(profile.route_length_m,(svgX-left)/usable*profile.route_length_m));const sample=samples.reduce((best,item)=>Math.abs(item.distance_m-distance)<Math.abs(best.distance_m-distance)?item:best,samples[0]);const x=left+sample.distance_m/Math.max(1,profile.route_length_m)*usable;cursor?.setAttribute('x1',x);cursor?.setAttribute('x2',x);cursor?.setAttribute('visibility','visible');if(tooltip)tooltip.textContent='distance '+fmt(sample.distance_m)+' m · lon,lat '+fmt(sample.coordinate?.[0],6)+', '+fmt(sample.coordinate?.[1],6)+' · ground '+fmt(sample.ground_egm2008_m)+' m · flight '+fmt(sample.flight_egm2008_m)+' m EGM2008 · AGL '+fmt(sample.flight_agl_m)+' m · clearance '+fmt(sample.ground_clearance_m)+' m · '+sample.status;c.setProfileHover?.(sample.coordinate);};
  svg.onpointerleave=()=>{cursor?.setAttribute('visibility','hidden');c.setProfileHover?.(null);};
}
