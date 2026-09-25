import {escapeHtml,statusBadge,statusText,verticalReferenceText} from './common.js';

// Layered Operational Route Architecture V1 front-end contract.  The production route is
// ``离场程序 → 固定巡航高度层 + 水平航路 → 进场程序``: one route carries exactly one cruise
// altitude layer, and the vertical transition stays inside the terminal procedures.
// This module never offers a default real altitude and never derives a layer from a
// RouteAltitudeProfile value.

export const CRUISE_LAYER_MODE='fixed_cruise_layer';
export const PROCEDURE_TYPES=['departure','arrival'];
export const TRANSITION_MODES=['climb_to_cruise_layer','descend_from_cruise_layer','level_transition','not_specified'];
export const TRANSITION_MODE_LABELS={
  climb_to_cruise_layer:'爬升加入巡航高度层',
  descend_from_cruise_layer:'下降离开巡航高度层',
  level_transition:'同高度过渡',
  not_specified:'未指定（保持待工程确认）',
};
export const PROCEDURE_TYPE_LABELS={departure:'离场程序',arrival:'进场程序'};
export const LAYER_PENDING_LABEL='待工程确认';
export const PRODUCTION_ROUTE_LABEL='生产航路 = 离场程序 → 固定巡航高度层 + 水平航路 → 进场程序';
export const ADVANCED_PROFILE_LABEL='advanced_variable_profile：高级/实验剖面展示，不是生产巡航高度层';
// 工程默认高度层（ALT-060/080/100/150/200）只是**目录条目**，绝不是任何航路的默认高度：
// 系统不为航路自动选择或分配高度，必须由用户显式选择。
export const NO_DEFAULT_ALTITUDE_LABEL='系统不为任何航路自动选择或分配默认高度；目录条目必须由用户显式选择并确认。';
// catalog 为空（从未初始化且恢复未补建，或用户显式删空）时的明确提示。
export const EMPTY_ALTITUDE_CATALOG_LABEL='高度层目录为空（共 0 层）：没有可选高度层，请先在步骤 02 的工作区/垂直配置面板补建 AltitudeLayer；系统不会自动选择或推断任何高度。';
export const READINESS_BUCKETS=[
  ['altitude_layer_catalog','高度层目录'],
  ['route_layer_assignment','航路 → 巡航高度层分配'],
  ['departure_procedure','离场程序'],
  ['arrival_procedure','进场程序'],
];

function numberOrNull(value){
  const number=Number(value);
  return value===null||value===undefined||value===''||!Number.isFinite(number)?null:number;
}

export function cruiseLayerCatalog(flow){
  return ((flow?.spatial_3d||{}).altitude_layers||[]).map(layer=>({
    altitude_layer_id:String(layer.altitude_layer_id||''),
    name:layer.name||layer.altitude_layer_id||'',
    nominal_altitude_m:numberOrNull(layer.nominal_altitude_m),
    lower_altitude_m:numberOrNull(layer.lower_altitude_m),
    upper_altitude_m:numberOrNull(layer.upper_altitude_m),
    vertical_reference:layer.vertical_reference||'unknown',
    source:layer.source||'未记录',
    confirmed:layer.confirmed===true,
    status:layer.status||'pending_confirmation',
  }));
}

//: The single source of truth for the panel: explicit assignments only.  A numeric
//: RouteAltitudeProfile altitude is deliberately never consulted here.
export function routeOperatingModel(flow){
  const spatial=flow?.spatial_3d||{};
  const layers=cruiseLayerCatalog(flow);
  const assignments=(spatial.route_operating_layers||[]).filter(item=>item&&item.active!==false);
  const procedures=spatial.departure_arrival_procedures||[];
  const profiles=spatial.route_altitude_profiles||{};
  const routes=(flow?.operational_routes||[]).map(route=>({
    route_id:String(route.route_id),
    status:route.status||'not_calculated',
    has_advanced_profile:Object.prototype.hasOwnProperty.call(profiles,String(route.route_id)),
  }));
  const routeModels=routes.map(route=>{
    const assignment=assignments.find(item=>String(item.route_id)===route.route_id)||null;
    const layer=assignment?layers.find(item=>item.altitude_layer_id===String(assignment.altitude_layer_id))||null:null;
    const departure=procedures.find(item=>item.procedure_type==='departure'&&String(item.route_id)===route.route_id)||null;
    const arrival=procedures.find(item=>item.procedure_type==='arrival'&&String(item.route_id)===route.route_id)||null;
    const cruiseConfirmed=Boolean(assignment&&layer&&assignment.status==='confirmed'&&layer.status==='confirmed');
    return {
      route_id:route.route_id,route_status:route.status,
      assignment, layer,
      operating_mode:CRUISE_LAYER_MODE,
      cruise_status:cruiseConfirmed?'confirmed':'pending_confirmation',
      nominal_altitude_m:layer?layer.nominal_altitude_m:null,
      vertical_reference:layer?layer.vertical_reference:null,
      departure, arrival,
      departure_status:departure&&departure.status==='confirmed'?'confirmed':'pending_confirmation',
      arrival_status:arrival&&arrival.status==='confirmed'?'confirmed':'pending_confirmation',
      has_advanced_profile:route.has_advanced_profile,
    };
  });
  const readiness=flow?.route_operating_readiness||null;
  return {
    production_route_definition:PRODUCTION_ROUTE_LABEL,
    semantics:{
      assignment_source:'explicit_user_selection_only',
      route_layer_is_never_matched_from_a_route_altitude_profile:true,
      cruise_and_terminal_transition_are_separate:true,
      missing_value_is_pending_not_unsafe_and_not_zero:true,
      default_altitudes_provided:false,
    },
    layers,
    confirmed_layer_count:layers.filter(layer=>layer.status==='confirmed').length,
    routes:routeModels,
    procedures,
    readiness,
    readiness_rows:(readiness?READINESS_BUCKETS:[]).map(([key,label])=>({
      key,label,
      status:(readiness[key]||{}).status||'pending_confirmation',
      reasons:(readiness[key]||{}).reasons||[],
    })),
  };
}

function layerRow(layer){
  // 垂向基准必须转成用户可理解中文（B4X §9）：绝不直接把 `egm2008_orthometric`
  // 丢给用户；raw 值仍保留在高级 / 审计区（由 general 说明与后端快照承载）。
  const nominal=layer.nominal_altitude_m===null?'nominal 未配置（'+LAYER_PENDING_LABEL+'）':escapeHtml(String(layer.nominal_altitude_m))+' m nominal';
  const bounds=(layer.lower_altitude_m===null||layer.upper_altitude_m===null)
    ?'高度范围未配置'
    :escapeHtml(String(layer.lower_altitude_m))+' 至 '+escapeHtml(String(layer.upper_altitude_m))+' m';
  return '<div class="list-row"><span><b>'+escapeHtml(layer.name||layer.altitude_layer_id)+'</b> '+statusBadge(layer.status)
    +'<small>'+escapeHtml(layer.altitude_layer_id)+' · '+nominal+' · '+bounds+' · '+escapeHtml(verticalReferenceText(layer.vertical_reference))+'</small>'
    +'<small>来源 '+escapeHtml(layer.source)+' · 工程确认 '+(layer.confirmed?'是':'否')+'</small></span></div>';
}

function cruiseLayerOptions(model,selected){
  const items=model.layers.map(layer=>'<option value="'+escapeHtml(layer.altitude_layer_id)+'" '+(layer.altitude_layer_id===selected?'selected':'')+'>'
    +escapeHtml(layer.altitude_layer_id)+' · '+(layer.nominal_altitude_m===null?LAYER_PENDING_LABEL:escapeHtml(String(layer.nominal_altitude_m))+' m')
    +' · '+escapeHtml(verticalReferenceText(layer.vertical_reference))+' · '+statusText(layer.status)+'</option>').join('');
  return (selected?'':'<option value="">请显式选择（不自动匹配）</option>')+items;
}

function cruiseRouteRow(model,route){
  const selected=route.assignment?String(route.assignment.altitude_layer_id):'';
  return '<div class="list-row route-row"><span><b>'+escapeHtml(route.route_id)+'</b> '+statusBadge(route.cruise_status)
    +'<small>巡航高度层 '+(route.assignment?escapeHtml(selected)+' · '+escapeHtml(route.operating_mode):'未配置 · '+LAYER_PENDING_LABEL)+'</small>'
    +'<small>nominal '+(route.nominal_altitude_m===null?LAYER_PENDING_LABEL:escapeHtml(String(route.nominal_altitude_m))+' m')+' · 垂向基准 '+escapeHtml(verticalReferenceText(route.vertical_reference))+'</small>'
    +'<small>离场 '+statusText(route.departure_status)+' · 进场 '+statusText(route.arrival_status)+' · 高级剖面 '+(route.has_advanced_profile?'存在（仅供参考）':'无')+'</small></span>'
    +'<span class="button-row"><select data-cruise-layer-for="'+escapeHtml(route.route_id)+'">'+cruiseLayerOptions(model,selected)+'</select>'
    +'<button class="secondary" data-save-cruise-layer="'+escapeHtml(route.route_id)+'">保存分配</button>'
    +(route.assignment?'<button data-delete-cruise-layer="'+escapeHtml(route.route_id)+'">解除</button>':'')+'</span></div>';
}

function readinessBlock(model){
  if(!model.readiness_rows.length){
    return '<div class="empty-note">readiness 由后端 route_operating_readiness 提供；当前快照未包含时只显示目录状态，不推断。</div>';
  }
  return '<div class="scroll-list">'+model.readiness_rows.map(row=>'<div class="list-row"><span><b>'+escapeHtml(row.label)+'</b> '+statusBadge(row.status)
    +'<small>'+escapeHtml(row.reasons.length?row.reasons.join('；'):'全部显式确认')+'</small></span></div>').join('')+'</div>';
}

function procedureRow(procedure){
  const type=PROCEDURE_TYPE_LABELS[procedure.procedure_type]||procedure.procedure_type;
  const missing=(procedure.missing_evidence||[]).join('、')||'无';
  return '<div class="list-row route-row"><span><b>'+escapeHtml(procedure.procedure_id)+'</b> '+statusBadge(procedure.status||'pending_confirmation')
    +'<small>'+escapeHtml(type)+' · 航路 '+escapeHtml(procedure.route_id)+' · 高度层 '+escapeHtml(procedure.altitude_layer_id||'未配置（'+LAYER_PENDING_LABEL+'）')+'</small>'
    +'<small>过渡模式 '+escapeHtml(TRANSITION_MODE_LABELS[procedure.transition_mode]||procedure.transition_mode||'未指定')+' · 参考 '+(procedure.node_id?escapeHtml('node '+procedure.node_id):procedure.site_reference?escapeHtml('site '+procedure.site_reference):'未配置')+'</small>'
    +'<small>缺少显式证据：'+escapeHtml(missing)+'</small></span>'
    +'<button data-delete-procedure="'+escapeHtml(procedure.procedure_id)+'">×</button></div>';
}

function procedureEditor(model){
  if(!model.routes.length)return '<div class="empty-note">先创建运行航路，再配置离场/进场程序。</div>';
  const routeOptions=model.routes.map(route=>'<option value="'+escapeHtml(route.route_id)+'">'+escapeHtml(route.route_id)+'</option>').join('');
  const layerOptions='<option value="">未配置（保持'+LAYER_PENDING_LABEL+'）</option>'
    +model.layers.map(layer=>'<option value="'+escapeHtml(layer.altitude_layer_id)+'">'+escapeHtml(layer.altitude_layer_id)+' · '+(layer.nominal_altitude_m===null?LAYER_PENDING_LABEL:escapeHtml(String(layer.nominal_altitude_m))+' m')+'</option>').join('');
  const transitionOptions=TRANSITION_MODES.map(mode=>'<option value="'+escapeHtml(mode)+'">'+escapeHtml(TRANSITION_MODE_LABELS[mode]||mode)+'</option>').join('');
  return '<div class="form-grid">'
    +'<label>procedure id<input class="panel-input" id="procedureId" placeholder="必填，例如 DP-R0001"></label>'
    +'<label>类型<select id="procedureType"><option value="departure">离场 departure</option><option value="arrival">进场 arrival</option></select></label>'
    +'<label>运行航路<select id="procedureRoute">'+routeOptions+'</select></label>'
    +'<label>巡航高度层<select id="procedureLayer">'+layerOptions+'</select></label>'
    +'<label>过渡模式<select id="procedureTransition">'+transitionOptions+'</select></label>'
    +'<label>项目 node id（可选）<input class="panel-input" id="procedureNode" placeholder="必须是已存在 node"></label>'
    +'<label>外部 site 参考（可选）<input class="panel-input" id="procedureSite" placeholder="来源事实记录，不推断"></label>'
    +'<label>爬升率 m/s（departure）<input class="panel-input" type="number" step="any" id="procedureClimbRate" placeholder="必须显式填写"></label>'
    +'<label>下降率 m/s（arrival）<input class="panel-input" type="number" step="any" id="procedureDescentRate" placeholder="必须显式填写"></label>'
    +'<label>转弯半径 m<input class="panel-input" type="number" step="any" id="procedureTurnRadius" placeholder="必须显式填写"></label>'
    +'<label>join/leave 里程 m<input class="panel-input" type="number" step="any" id="procedureJoinDistance" placeholder="必须显式填写"></label>'
    // Production Route3DProfile V1 需要的显式 terminal altitude 证据：数值 + source + evidence
    // 三者缺一不可，系统绝不后台默认 FABDEM 或起降平台高度。
    +'<label>terminal altitude EGM2008 m<input class="panel-input" type="number" step="any" id="procedureTerminalAltitude" placeholder="显式填写，绝不默认地形高度"></label>'
    +'<label>terminal altitude source<input class="panel-input" id="procedureTerminalSource" placeholder="例如 manual 工程评审 / verified 实测"></label>'
    +'<label>terminal altitude evidence<input class="panel-input" id="procedureTerminalEvidence" placeholder="依据文件、记录或编号"></label>'
    +'<label>来源/依据<input class="panel-input" id="procedureSource" placeholder="工程依据、文件或评审记录"></label>'
    +'</div><label class="check-row"><input type="checkbox" id="procedureConfirmed">离场/进场程序参数已由工程依据确认</label>'
    +'<div class="parameter-note">terminal altitude 必须来自显式 manual/verified 来源并带 evidence：'
    +'Route3DProfile 缺任一项即 not_ready，绝不后台默认 FABDEM / 起降平台高度。</div>'
    +'<button class="secondary full" id="saveProcedure">保存程序（显式工程设定）</button>';
}

export function renderCruiseLayerPanel(flow){
  const model=routeOperatingModel(flow);
  const catalog=model.layers.length?model.layers.map(layerRow).join(''):'<div class="empty-note">'+EMPTY_ALTITUDE_CATALOG_LABEL+' 巡航高度层保持 <b>'+LAYER_PENDING_LABEL+'</b>。</div>';
  const routeRows=model.routes.length?model.routes.map(route=>cruiseRouteRow(model,route)).join(''):'<div class="empty-note">尚无运行航路；生成运行航路后才能显式分配巡航高度层。</div>';
  const procedures=model.procedures.length?model.procedures.map(procedureRow).join(''):'<div class="empty-note">尚无离场/进场程序：readiness 保持 '+LAYER_PENDING_LABEL+'，不填默认爬升率/下降率/转弯半径/join-leave 点。</div>';
  return '<div id="cruiseLayerPanel">'
    +'<h3>巡航高度层（生产主模式）'+statusBadge(model.readiness?.status||'pending_confirmation')+'</h3>'
    +'<div class="parameter-note"><b>'+escapeHtml(PRODUCTION_ROUTE_LABEL)+'</b><br>一条具体方案只对应一个巡航高度层；高度转换不进入水平航路规划（cruise 与 terminal transition 分离）。'
    +'巡航高度层只能由用户<b>显式选择</b>，绝不根据 RouteAltitudeProfile 数值自动匹配，也不提供默认高度。</div>'
    +'<h3>已配置 AltitudeLayer</h3><div class="scroll-list">'+catalog+'</div>'
    +'<h3>航路 → 巡航高度层（显式分配）</h3>'
    +'<label>来源/依据<input class="panel-input" id="cruiseLayerSource" placeholder="工程依据、文件或评审记录"></label>'
    +'<label class="check-row"><input type="checkbox" id="cruiseLayerConfirmed">该航路巡航高度层已由工程依据确认</label>'
    +'<div class="scroll-list">'+routeRows+'</div>'
    +'<h3>readiness（分开报告）</h3><div class="parameter-note">缺值 = <b>'+LAYER_PENDING_LABEL+'</b>，既不等于 unsafe 也不等于 0；四项分别报告，不合并成单一结论。</div>'
    +readinessBlock(model)
    +'<h3>离场 / 进场程序（合同 / readiness / CRUD）</h3>'
    +'<div class="parameter-note">本轮只实现合同、readiness 与 CRUD，不做 procedure path optimizer；缺爬升率、下降率、转弯半径或 join/leave point 时保持 '+LAYER_PENDING_LABEL+'，不补默认值。</div>'
    +'<div class="scroll-list">'+procedures+'</div>'
    +procedureEditor(model)
    +'</div>';
}

export function bindCruiseLayer(c){
  if(!c.$('cruiseLayerPanel'))return;
  const optionalNumber=id=>{const field=c.$(id);if(!field)return null;const value=String(field.value??'').trim();return value===''?null:Number(value);};
  const optionalText=id=>{const field=c.$(id);return field?String(field.value||'').trim():'';};
  const source=()=>{const field=c.$('cruiseLayerSource');return field?String(field.value||'').trim():'';};
  const confirmed=()=>{const field=c.$('cruiseLayerConfirmed');return Boolean(field&&field.checked);};
  document.querySelectorAll('[data-save-cruise-layer]').forEach(button=>button.onclick=async()=>{
    const routeId=button.dataset.saveCruiseLayer;
    const select=document.querySelector('[data-cruise-layer-for="'+routeId+'"]');
    try{button.disabled=true;await c.resourceAction('/api/spatial-3d/route-operating-layer',{route_id:routeId,altitude_layer_id:select?select.value:'',operating_mode:CRUISE_LAYER_MODE,source:source(),confirmed:confirmed()});}
    catch(error){c.panelError(error.message);}
    finally{if(document.body.contains(button))button.disabled=false;}
  });
  document.querySelectorAll('[data-delete-cruise-layer]').forEach(button=>button.onclick=async()=>{
    try{button.disabled=true;await c.resourceAction('/api/spatial-3d/route-operating-layer/delete',{route_id:button.dataset.deleteCruiseLayer});}
    catch(error){c.panelError(error.message);}
    finally{if(document.body.contains(button))button.disabled=false;}
  });
  document.querySelectorAll('[data-delete-procedure]').forEach(button=>button.onclick=async()=>{
    try{button.disabled=true;await c.resourceAction('/api/spatial-3d/departure-arrival-procedure/delete',{procedure_id:button.dataset.deleteProcedure});}
    catch(error){c.panelError(error.message);}
    finally{if(document.body.contains(button))button.disabled=false;}
  });
  c.actionButton('saveProcedure',()=>{
    const type=c.$('procedureType').value;
    const verticalProfile={};
    const climbRate=optionalNumber('procedureClimbRate');
    const descentRate=optionalNumber('procedureDescentRate');
    if(type==='departure'&&climbRate!==null)verticalProfile.climb_rate_mps=climbRate;
    if(type==='arrival'&&descentRate!==null)verticalProfile.descent_rate_mps=descentRate;
    // Production Route3DProfile V1 的 terminal altitude 证据：数值、source、evidence 三者
    // 都只在用户显式填写时才发送；前端绝不补默认值，也绝不从地形/平台推导。
    const terminalAltitude=optionalNumber('procedureTerminalAltitude');
    const terminalSource=optionalText('procedureTerminalSource');
    const terminalEvidence=optionalText('procedureTerminalEvidence');
    if(terminalAltitude!==null)verticalProfile.terminal_altitude_egm2008_m=terminalAltitude;
    if(terminalSource)verticalProfile.terminal_altitude_source=terminalSource;
    if(terminalEvidence)verticalProfile.terminal_altitude_evidence=terminalEvidence;
    const horizontalGeometry={};
    const turnRadius=optionalNumber('procedureTurnRadius');
    if(turnRadius!==null)horizontalGeometry.turn_radius_m=turnRadius;
    const joinDistance=optionalNumber('procedureJoinDistance');
    const joinLeavePoint=joinDistance===null?null:{kind:type==='departure'?'join':'leave',distance_along_route_m:joinDistance};
    return c.resourceAction('/api/spatial-3d/departure-arrival-procedure',{
      procedure_id:c.$('procedureId').value.trim(),
      procedure_type:type,
      route_id:c.$('procedureRoute').value,
      altitude_layer_id:c.$('procedureLayer').value||null,
      transition_mode:c.$('procedureTransition').value,
      node_id:c.$('procedureNode').value.trim()||null,
      site_reference:c.$('procedureSite').value.trim()||null,
      horizontal_geometry:horizontalGeometry,
      vertical_profile:verticalProfile,
      join_leave_point:joinLeavePoint,
      source:c.$('procedureSource').value.trim(),
      confirmed:c.$('procedureConfirmed').checked,
    });
  });
}
