import {escapeHtml,statusBadge} from './common.js';

// Production Route3DProfile V1 front-end contract.  A Route3DProfile is a **thin derivation**
// of an already published fixed-H layered route into a distance-parameterised climb → cruise
// → descent profile.  It is never a 3D search, never a Dubins Airplane / minimum-snap
// trajectory and never a kinematic or flight-dynamics validation.
//
// The panel is compact on purpose and reuses the existing RouteVerticalProfile chart: the
// profile's ``mode=waypoint_linear`` waypoints are what that chart already plots, so no second
// chart engine exists here.

export const ROUTE_3D_PROFILE_VERSION='route_3d_profile_v1@1.0';
export const ROUTE_3D_DIAGNOSTIC_LABEL='diagnostic_not_aircraft_kinematic_validation';
export const ROUTE_3D_BOUNDARY_LABEL='cruise_validation=validated/current · terminal_transition_validation=not_evaluated · full_3d_geometry_validated=false';
export const ROUTE_3D_NO_DEFAULT_LABEL='terminal altitude 必须来自显式 manual/verified source + evidence；绝不后台默认 FABDEM 或起降平台高度。';
export const ROUTE_3D_NO_SPEED_DERIVATION_LABEL='join/leave 里程完全以显式 join_leave_point.distance_along_route_m 为准：绝不用 cruise_speed 反推。';
export const ROUTE_3D_ENDPOINT_LABEL='takeoff_event / landing_event 只是端点事件：V1 不虚构零水平距离的垂直悬停段。';
export const ROUTE_3D_PHASE_LABELS={takeoff_event:'起飞事件',climb:'爬升',cruise:'巡航',descent:'下降',landing_event:'着陆事件'};

function finite(value){return Number.isFinite(value);}
function num(value){const number=Number(value);return value===null||value===undefined||value===''||!Number.isFinite(number)?null:number;}
function fmt(value,digits=1){const number=num(value);return number===null?'—':number.toFixed(digits);}

//: Resolve the profile collection for one flow snapshot.  The workflow snapshot injects the
//: read-only projection (with recomputed ``current_applicability``); the stored container
//: inside ``spatial_3d`` is the fallback, so the panel renders either way.
export function route3dProfileItems(flow){
  const projected=flow?.route_3d_profiles;
  if(projected&&Array.isArray(projected.items))return projected.items;
  return Object.values(flow?.spatial_3d?.route_3d_profiles||{});
}

export function route3dProfileModel(flow){
  const items=route3dProfileItems(flow);
  const byRoute={};
  items.forEach(item=>{if(item&&item.route_id)byRoute[String(item.route_id)]=item;});
  const readiness=flow?.route_3d_profile_readiness||null;
  const readinessByRoute={};
  (readiness?.routes||[]).forEach(item=>{readinessByRoute[String(item.route_id)]=item;});
  return {
    profile_version:ROUTE_3D_PROFILE_VERSION,
    items,
    byRoute,
    readiness,
    readinessByRoute,
    boundaries:{
      thin_derivation_layer_not_a_planner:true,
      no_3d_search:true,
      no_dubins_airplane_or_minimum_snap:true,
      no_kinematic_validation:true,
      explicit_user_evaluation_required:true,
      automatic_generation:false,
      produces_safe_or_unsafe_verdict:false,
    },
  };
}

function transitionRows(profile){
  const diagnostics=profile.diagnostics||{};
  const climb=diagnostics.climb||null,descent=diagnostics.descent||null;
  const row=(label,item,rateKey)=>{
    if(!item)return '<div class="list-row"><span><b>'+escapeHtml(label)+'</b><small>unresolved：缺少显式几何或性能证据，不推断。</small></span></div>';
    return '<div class="list-row"><span><b>'+escapeHtml(label)+'</b>'
      +'<small>altitude_delta '+fmt(item.altitude_delta_m)+' m · transition_horizontal_distance '+fmt(item.transition_horizontal_distance_m)+' m</small>'
      +'<small>duration '+fmt(item.duration_s)+' s （'+escapeHtml(item.duration_basis||'—')+'）· implied_horizontal_speed '+fmt(item.implied_horizontal_speed_mps)+' m/s</small>'
      +'<small>rate '+escapeHtml(rateKey)+' '+fmt(item.rate_mps)+' m/s · '+escapeHtml(ROUTE_3D_DIAGNOSTIC_LABEL)+'</small>'
      +((item.unresolved_reasons||[]).length?'<small>未解析：'+escapeHtml(item.unresolved_reasons.join('；'))+'</small>':'')+'</span></div>';
  };
  return row('climb diagnostic',climb,'climb_rate_mps')+row('descent diagnostic',descent,'descent_rate_mps');
}

function phaseRows(profile){
  const phases=profile.phases||[];
  if(!phases.length)return '<div class="empty-note">尚无 phases（profile 未 passed/unresolved）。</div>';
  return phases.map(phase=>'<div class="list-row"><span><b>'+escapeHtml(ROUTE_3D_PHASE_LABELS[phase.phase_id]||phase.phase_id)+'</b>'
    +'<small>'+fmt(phase.start_distance_along_route_m)+' → '+fmt(phase.end_distance_along_route_m)+' m · 水平长度 '+fmt(phase.horizontal_length_m)+' m</small>'
    +'<small>高度 '+fmt(phase.start_altitude_m)+' → '+fmt(phase.end_altitude_m)+' m · Δ '+fmt(phase.altitude_change_m)+' m · '+escapeHtml(phase.altitude_semantics||'')+'</small>'
    +(phase.endpoint_event?'<small>'+escapeHtml(ROUTE_3D_ENDPOINT_LABEL)+'</small>':'')
    +'</span></div>').join('');
}

function validationBlock(profile){
  const boundary=profile.validation_boundary||{};
  return '<div class="list-row"><span><b>validation 边界</b>'
    +'<small>cruise_validation '+escapeHtml(String(boundary.cruise_validation||'—'))+'（scope '+escapeHtml(String(boundary.cruise_validation_scope||'—'))+'）</small>'
    +'<small>terminal_transition_validation '+escapeHtml(String(boundary.terminal_transition_validation||'—'))+' · full_3d_geometry_validated '+escapeHtml(String(boundary.full_3d_geometry_validated))+'</small>'
    +'<small>climb/descent 不由固定高度 validator 背书：本产物不输出 safe/unsafe。</small>'
    +'<small>下一阶段：'+escapeHtml(String(boundary.next_stage||'vertical_transition_continuous_validation'))+'（implemented '+escapeHtml(String(boundary.next_stage_implemented))+'）</small></span></div>';
}

function routeCard(model,routeId){
  const profile=model.byRoute[routeId]||null;
  const readiness=model.readinessByRoute[routeId]||null;
  const readinessReasons=(readiness?.blockers||[]).length?readiness.blockers:(profile?.reasons||[]);
  const header='<div class="list-row route-row"><span><b>'+escapeHtml(routeId)+'</b> '
    +statusBadge(profile?.status||readiness?.status||'not_ready')
    +'<small>readiness '+escapeHtml(readiness?.status||'—')+' · current_applicability '+escapeHtml(profile?.current_applicability||'—')+'</small>'
    +(readinessReasons.length?'<small>blockers/reasons：'+escapeHtml(readinessReasons.join('；'))+'</small>':'')
    +'</span></div>';
  if(!profile){
    return header+'<div class="empty-note">尚未生成 Route3DProfile：本卡片不自动生成，必须由用户显式 evaluate。'+escapeHtml(ROUTE_3D_NO_DEFAULT_LABEL)+'</div>';
  }
  const join=profile.join_leave||{},terminals=profile.terminal_altitude_m||{},lengths=profile.phase_lengths_m||{};
  const fingerprint=(profile.fingerprints||{}).profile_fingerprint;
  return header
    +'<div class="list-row"><span><b>高度与里程</b>'
    +'<small>H '+fmt(profile.cruise_altitude_m)+' m · z0 '+fmt(terminals.departure_egm2008_m)+' m · z1 '+fmt(terminals.arrival_egm2008_m)+' m · 垂向基准 '+escapeHtml(profile.vertical_reference||'—')+'</small>'
    +'<small>L '+fmt(profile.route_length_m)+' m（'+escapeHtml(profile.route_length_basis||'—')+'）· s_join '+fmt(join.join_distance_along_route_m)+' m · s_leave '+fmt(join.leave_distance_along_route_m)+' m</small>'
    +'<small>climb '+fmt(lengths.climb)+' m · cruise '+fmt(lengths.cruise)+' m · descent '+fmt(lengths.descent)+' m</small>'
    +'<small>'+escapeHtml(ROUTE_3D_NO_SPEED_DERIVATION_LABEL)+'</small></span></div>'
    +'<div class="list-row"><span><b>terminal altitude 来源</b>'
    +'<small>departure '+escapeHtml(String(terminals.departure_source||'—'))+' · evidence '+escapeHtml(String(terminals.departure_evidence||'—'))+'</small>'
    +'<small>arrival '+escapeHtml(String(terminals.arrival_source||'—'))+' · evidence '+escapeHtml(String(terminals.arrival_evidence||'—'))+'</small>'
    +'<small>'+escapeHtml(ROUTE_3D_NO_DEFAULT_LABEL)+'</small></span></div>'
    +'<div class="scroll-list">'+phaseRows(profile)+'</div>'
    +'<div class="scroll-list">'+transitionRows(profile)+'</div>'
    +validationBlock(profile)
    +'<div class="list-row"><span><b>profile fingerprint</b><small><code>'+escapeHtml(String(fingerprint||'—'))+'</code></small>'
    +'<small>profile_version '+escapeHtml(String(profile.profile_version||ROUTE_3D_PROFILE_VERSION))+'</small></span></div>';
}

export function renderRoute3DProfilePanel(flow){
  const model=route3dProfileModel(flow);
  const routes=(flow?.operational_routes||[]).map(route=>String(route.route_id));
  const readinessRows=(model.readiness?.routes||[]).map(item=>{
    const blockers=item.blockers||[];
    return '<div class="list-row"><span><b>'+escapeHtml(item.route_id)+'</b> '+statusBadge(item.status)
      +'<small>'+(blockers.length?escapeHtml(blockers.join('；')):'全部显式就绪')+'</small></span></div>';
  }).join('');
  const body=routes.length?routes.map(routeId=>routeCard(model,routeId)).join(''):'<div class="empty-note">尚无运行航路：Route3DProfile 只能由显式 evaluate 生成。</div>';
  return '<h3>完整 3D 航迹（Production Route3DProfile V1）'+statusBadge(model.readiness?.status||'not_ready')+'</h3>'
    +'<div class="parameter-note"><b>起点/起飞事件 → climb → cruise → descent → 终点/着陆事件</b>：'
    +'由已发布固定高度航路按 route distance 分段线性派生，供现有纵剖面 / Coverage3D 直接消费。'
    +'<br>'+escapeHtml(ROUTE_3D_BOUNDARY_LABEL)
    +'<br>本卡片不做 3D 搜索、不做 Dubins Airplane / minimum-snap、不做运动学 Safety，也不自动生成。</div>'
    +(readinessRows?'<div class="parameter-note">readiness（逐航路，缺任一项即 not_ready）</div><div class="scroll-list">'+readinessRows+'</div>':'')
    +'<div class="button-row">'
    +routes.map(routeId=>'<button class="secondary" data-evaluate-route-3d="'+escapeHtml(routeId)+'">evaluate '+escapeHtml(routeId)+'</button>').join('')
    +'<button class="secondary" data-evaluate-route-3d="all">evaluate 全部</button></div>'
    +'<div class="scroll-list route-list">'+body+'</div>'
    +'<div class="button-row">'+model.items.map(item=>'<button data-delete-route-3d="'+escapeHtml(String(item.profile_id))+'">删除 profile '+escapeHtml(String(item.route_id))+'</button>').join('')+'</div>'
    +'<div class="parameter-note">'+escapeHtml(ROUTE_3D_ENDPOINT_LABEL)+' 若 profile 存在但不是 current，垂向解析立即返回 unresolved，'
    +'<b>绝不静默回退到固定巡航高度 H</b>。</div>';
}

export function bindRoute3DProfile(c){
  const actions=[...document.querySelectorAll('[data-evaluate-route-3d]')];
  if(!actions.length)return;
  actions.forEach(button=>button.onclick=async()=>{
    const target=button.dataset.evaluateRoute3d;
    try{button.disabled=true;await c.resourceAction('/api/route-3d-profiles/evaluate',target==='all'?{}:{route_id:target});}
    catch(error){c.panelError(error.message);}
    finally{if(document.body.contains(button))button.disabled=false;}
  });
  document.querySelectorAll('[data-delete-route-3d]').forEach(button=>button.onclick=async()=>{
    try{button.disabled=true;await c.resourceAction('/api/route-3d-profiles/delete',{profile_id:button.dataset.deleteRoute3d});}
    catch(error){c.panelError(error.message);}
    finally{if(document.body.contains(button))button.disabled=false;}
  });
}
