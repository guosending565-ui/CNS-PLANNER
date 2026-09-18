import {escapeHtml,statusBadge,statusText} from './common.js';

// Layered Risk-Aware Route Planner V1 workbench (production main line).
//
// Hard rules surfaced in the UI:
//   * the cruise altitude layer is an explicit选择 — never inferred from a RouteAltitudeProfile
//     and never defaulted;
//   * there is no default terrain clearance and no default λ: ``null != 0`` and an explicit
//     ``0`` is legal (that domain is then not a planning input at all);
//   * the result is only a candidate: it is not an operational route, CNS is not assessed and
//     continuous validation is still required.
export const LAYERED_PLANNER_ALGORITHM_TYPE='layered_route_planner';
export const LAYERED_PLANNER_ALGORITHM_ID='layered_route_planner_v1';
export const LAYERED_CANDIDATE_LABEL='分层候选（candidate，非运行航路）';
export const LAYERED_BLOCKED_NOTE='没有 confirmed 参数时一律 blocked：不提供任何默认高度、净空或 λ。';
export const COST_DOMAIN_LABELS={
  ground:'Ground（地面暴露）',
  air_traffic:'Air / Traffic（空中交通暴露）',
  environment_obstacle:'Environment / Obstacle（工程环境-障碍物）',
};

export function layeredRoutePlannerModel(flow){
  const readiness=flow?.layered_route_planner_readiness||{};
  const request=flow?.layered_route_planning_request||readiness.request||{};
  const feasibility=flow?.layered_route_feasibility_policy||{};
  const cost=flow?.layered_route_cost_policy||{};
  const candidates=flow?.layered_route_candidates||{};
  const catalog=readiness.altitude_layer_catalog||{};
  const costPolicy=readiness.cost_policy||{};
  const domains=Object.keys(COST_DOMAIN_LABELS).map(domainId=>{
    const policy=(costPolicy.domains||{})[domainId]||{};
    const lambda=policy.lambda;
    return {
      domainId,label:COST_DOMAIN_LABELS[domainId],
      lambda:Number.isFinite(lambda)?lambda:null,
      configured:policy.configured===true,
      enabled:policy.enabled===true,
      // ``null`` is "not confirmed yet"; ``0`` is an explicit, legal decision.
      state:policy.configured!==true?'待确认（null）':(policy.enabled?'启用（λ>0）':'关闭（显式 0）'),
    };
  });
  const items=candidates.items||[];
  const active=items.find(item=>item.candidate_id&&item.candidate_id===candidates.active_candidate_id)||null;
  return {
    status:readiness.status||'blocked',
    algorithm:readiness.algorithm||{},
    scenarioRoutes:(flow?.scenario_routes||[]).map(route=>({
      routeId:route.route_id,
      direction:route.direction||((route.start_node_id&&route.end_node_id)?(route.start_node_id+'→'+route.end_node_id):''),
    })),
    selectedRouteId:request.scenario_route_id||readiness.scenario_route?.route_id||null,
    scenarioRouteStatus:readiness.scenario_route?.status||'not_resolved',
    layers:(catalog.layers||[]).map(layer=>({
      layerId:layer.altitude_layer_id,name:layer.name,
      nominal:Number.isFinite(layer.nominal_altitude_m)?layer.nominal_altitude_m:null,
      verticalReference:layer.vertical_reference,
      status:layer.status,reasons:layer.reasons||[],
    })),
    selectedLayerId:request.altitude_layer_id||null,
    cruiseAltitude:readiness.altitude_layer_catalog?.cruise_altitude||null,
    requestStatus:request.status||'pending_confirmation',
    requestConfirmed:request.confirmed===true,
    source:request.source||null,
    feasibility:{
      status:feasibility.status||'blocked',
      clearance:Number.isFinite(feasibility.terrain_vertical_clearance_m)?feasibility.terrain_vertical_clearance_m:null,
      parameterStatus:feasibility.parameter_status||null,
      fingerprint:feasibility.fingerprint||null,
      maskingConfirmed:readiness.feasibility_policy?.status==='confirmed',
    },
    cost:{
      status:cost.status||'pending_confirmation',
      domains,
      anyNull:domains.some(item=>!item.configured),
      allZero:domains.every(item=>item.configured&&!item.enabled),
      fingerprint:costPolicy.fingerprint||null,
      parameterStatus:costPolicy.parameter_status||null,
    },
    risk:readiness.risk_framework_v2||{},
    buildingClearance:readiness.building_clearance_policy||{},
    mask:readiness.feasibility_mask||{},
    blockers:readiness.blockers||[],
    candidates:{
      status:candidates.status||'not_calculated',
      count:candidates.count||0,
      activeFingerprint:candidates.current_candidate_fingerprint||null,
      items:items.map(item=>({
        candidateId:item.candidate_id,
        routeId:item.route_id,
        layerId:item.altitude_layer_id,
        status:item.status,
        applicability:item.current_applicability||'unknown',
        reason:item.reason,
        blockingReasons:item.blocking_reasons||[],
        distance:Number.isFinite(item.distance_m)?item.distance_m:null,
        cost:Number.isFinite(item.optimization_cost)?item.optimization_cost:null,
        detour:Number.isFinite(item.detour_factor)?item.detour_factor:null,
        cellCount:(item.grid_path||[]).length,
        gridPath:item.grid_path||[],
        costBreakdown:item.cost_breakdown||{},
        candidateFingerprint:item.candidate_fingerprint||null,
        feasibilityFingerprint:item.feasibility_fingerprint||null,
        riskFingerprint:item.risk_fingerprint||null,
        policyFingerprint:item.policy_fingerprint||null,
        operationalRoute:item.operational_route===true,
        continuousValidationRequired:item.continuous_validation_required!==false,
        routeOperatingLayerCreated:item.route_operating_layer_created===true,
        searchIncomplete:item.search_incomplete===true,
      })),
      active:active?{candidateId:active.candidate_id,status:active.status}:null,
    },
    // The overlay renders the mask of the currently selected (route, layer) lane only.
    overlayMask:selectOverlayMask(candidates,request),
    layerCatalog:readiness.altitude_layer_catalog||{},
    sources:readiness.sources||{},
  };
}

export function selectOverlayMask(candidates,request){
  const masks=candidates?.masks||{};
  const key=(request?.scenario_route_id||'od')+'@'+(request?.altitude_layer_id||'layer');
  return masks[key]||null;
}

export function layeredFeasibilityCellCounts(mask){
  const counts=mask?.counts||{};
  return {
    feasible:Number.isFinite(counts.feasible)?counts.feasible:0,
    blocked:Number.isFinite(counts.blocked)?counts.blocked:0,
    unknown:Number.isFinite(counts.unknown)?counts.unknown:0,
  };
}

export function layeredOverlayModel(flow){
  const model=layeredRoutePlannerModel(flow);
  const mask=model.overlayMask;
  if(!mask)return {available:false,reason:'尚未计算 selected layer 的 feasibility mask',cells:[],counts:{feasible:0,blocked:0,unknown:0}};
  const cells=Object.values(mask.cells||{}).map(cell=>({
    gridId:cell.grid_id,status:cell.status,reason:cell.reason,reasonCode:cell.reason_code,
  }));
  return {
    available:true,
    altitudeLayerId:mask.altitude_layer_id,
    status:mask.status,
    currentApplicability:mask.current_applicability||'stale',
    counts:layeredFeasibilityCellCounts(mask),
    cells,
  };
}

export function layeredCandidatePaths(flow){
  const model=layeredRoutePlannerModel(flow);
  const mask=model.overlayMask;
  return model.candidates.items
    .filter(item=>item.status==='candidate'&&item.applicability==='current'&&item.gridPath.length)
    .map(item=>({
      candidateId:item.candidateId,
      gridPath:item.gridPath,
      record:(mask?.cells||{})[item.candidateId]||null,
    }));
}

function formatNumber(value,digits=2){
  return Number.isFinite(value)?Number(value).toFixed(digits):'—';
}

function blockerList(blockers){
  if(!blockers.length)return '<div class="parameter-note">没有 blocking 项：readiness = ready。</div>';
  return '<div class="scroll-list">'+blockers.map(item=>
    '<div class="list-row"><span><b>'+escapeHtml(item.reason_code||'blocked')+'</b>'+
    '<small>'+escapeHtml(item.reason||'')+'</small></span></div>').join('')+'</div>';
}

function domainRow(domain){
  return '<div class="list-row"><span><b>'+escapeHtml(domain.label)+'</b> '+statusBadge(domain.enabled?'passed':'not_calculated')+
    '<small>λ '+escapeHtml(domain.state)+' · 配置值 '+(domain.lambda===null?'null（待确认）':formatNumber(domain.lambda,4))+
    '</small></span></div>';
}

function candidateRow(item){
  const breakdown=item.costBreakdown||{};
  const contributions=breakdown.lambda_weighted_contributions_m||{};
  const contributionText=Object.keys(COST_DOMAIN_LABELS).map(domainId=>
    COST_DOMAIN_LABELS[domainId]+' '+formatNumber(contributions[domainId])).join(' · ');
  const blockers=(item.blockingReasons||[]).map(reason=>reason.reason_code).join(', ');
  return '<div class="list-row"><span><b>'+escapeHtml(item.candidateId||LAYERED_CANDIDATE_LABEL)+'</b> '+
    statusBadge(item.status)+' <small>'+escapeHtml(statusText(item.applicability))+'</small>'+
    '<small>layer '+escapeHtml(item.layerId||'—')+
    ' · distance '+formatNumber(item.distance)+' m'+
    ' · optimization cost '+formatNumber(item.cost)+
    ' · detour '+formatNumber(item.detour,3)+
    ' · cells '+item.cellCount+'</small>'+
    '<small>cost breakdown：distance '+formatNumber(breakdown.distance_contribution_m)+' m · '+escapeHtml(contributionText)+'</small>'+
    '<small>fingerprints：candidate '+escapeHtml((item.candidateFingerprint||'—').slice(0,20))+
    ' · feasibility '+escapeHtml((item.feasibilityFingerprint||'—').slice(0,20))+
    ' · risk '+escapeHtml((item.riskFingerprint||'—').slice(0,20))+
    ' · policy '+escapeHtml((item.policyFingerprint||'—').slice(0,20))+'</small>'+
    (blockers?'<small>blocking：'+escapeHtml(blockers)+'</small>':'')+
    (item.reason?'<small>'+escapeHtml(item.reason)+'</small>':'')+
    (item.searchIncomplete?'<small>search incomplete：达到 expansion cap，最优性未证明</small>':'')+
    '</span></div>';
}

export function renderLayeredRoutePlannerPanel(flow){
  const model=layeredRoutePlannerModel(flow);
  const routeOptions=model.scenarioRoutes.map(route=>
    '<option value="'+escapeHtml(route.routeId)+'"'+(route.routeId===model.selectedRouteId?' selected':'')+'>'+
    escapeHtml(route.routeId+(route.direction?' · '+route.direction:''))+'</option>').join('');
  const layerOptions=model.layers.map(layer=>
    '<option value="'+escapeHtml(layer.layerId)+'"'+(layer.layerId===model.selectedLayerId?' selected':'')+'>'+
    escapeHtml(layer.layerId+(layer.nominal===null?' · nominal 待确认':' · '+layer.nominal)+' m')+'</option>').join('');
  const counts=model.mask?.counts||{};
  return '<h3>生产候选规划（Layered Risk-Aware Route Planner V1）</h3>'+
    '<div class="parameter-note">pipeline：scenario/OD 航路 → <b>显式 AltitudeLayer</b> → terrain/building '+
    'coarse 可行性 mask → MH/T L8 A* → Risk Framework V2 soft cost → LayeredRouteCandidate。'+
    '结果只是 <b>candidate</b>：不得直接写入运行航路或 CNS；连续验证（精确 footprint 与水平净空）仍需后续执行。'+
    '这是 <b>coarse_strategic_vertical_envelope</b>，不是 exact footprint，也不是水平/精确建筑净空结论。</div>'+
    '<div class="parameter-note">readiness：'+statusBadge(model.status)+' · '+
    escapeHtml(model.algorithm.algorithm_id||LAYERED_PLANNER_ALGORITHM_ID)+'@'+escapeHtml(model.algorithm.algorithm_version||'1.0')+
    ' · '+LAYERED_BLOCKED_NOTE+'</div>'+
    '<label>scenario / OD 航路</label>'+
    '<select id="layeredRouteSelect">'+(routeOptions||'<option value="">当前没有 scenario/OD 航路</option>')+'</select>'+
    '<label>巡航高度层（显式选择，不从 profile 推断）</label>'+
    '<select id="layeredAltitudeLayerSelect">'+(layerOptions||'<option value="">尚未配置 AltitudeLayer</option>')+'</select>'+
    '<div class="parameter-note">selected layer cruise altitude：'+
    escapeHtml(model.cruiseAltitude?.status||'not_resolved')+
    (Number.isFinite(model.cruiseAltitude?.altitude_egm2008_m)?' · '+formatNumber(model.cruiseAltitude.altitude_egm2008_m)+' m EGM2008':'')+
    (model.cruiseAltitude?.reason?' · '+escapeHtml(model.cruiseAltitude.reason):'')+
    '<br>AGL / WGS84 高度层在缺少显式 DEM surface 或 geoid 证据时保持 blocked：V1 不做 datum/geoid 猜测或伪转换。</div>'+
    '<label>request source / evidence</label>'+
    '<input id="layeredRequestSource" placeholder="工程依据（确认时必填）" value="'+escapeHtml(model.source||'')+'">'+
    '<label class="checkbox-row"><input type="checkbox" id="layeredRequestConfirmed"'+(model.requestConfirmed?' checked':'')+'> 显式确认本次规划请求</label>'+
    '<div class="button-row">'+
    '<button class="secondary" id="saveLayeredRequest">保存 planning request</button>'+
    '<button class="secondary" id="saveLayeredFeasibilityPolicy">保存 feasibility policy</button>'+
    '<button class="secondary" id="saveLayeredCostPolicy">保存 cost policy</button>'+
    '<button class="primary" id="evaluateLayeredCandidate">运行 candidate（需真实数据源）</button>'+
    '</div>'+
    '<h3>参数 readiness</h3>'+
    '<div class="list-row"><span><b>terrain_vertical_clearance_m</b> '+statusBadge(model.feasibility.status)+
    '<small>'+(model.feasibility.clearance===null?'null · 无默认值，必须由工程确认':'已确认 '+formatNumber(model.feasibility.clearance)+' m')+
    ' · '+(model.feasibility.parameterStatus||'')+'</small>'+
    '<small>feasibility policy fingerprint '+escapeHtml(model.feasibility.fingerprint||'—')+'</small></span></div>'+
    '<div class="list-row"><span><b>LayeredRouteCostPolicy</b> '+statusBadge(model.cost.status)+
    '<small>null ≠ 0：null 表示待确认，显式 0 表示该 domain 不作为规划输入</small>'+
    '<small>cost policy fingerprint '+escapeHtml(model.cost.fingerprint||'—')+'</small></span></div>'+
    '<div class="scroll-list">'+model.cost.domains.map(domainRow).join('')+'</div>'+
    '<div class="list-row"><span><b>建筑垂直净空（复用既有策略）</b> '+statusBadge(model.buildingClearance.status)+
    '<small>'+(Number.isFinite(model.buildingClearance.vertical_clearance_m)?'vertical '+formatNumber(model.buildingClearance.vertical_clearance_m)+' m':'vertical 待确认')+
    ' · 不新建第二套建筑净空定义</small></span></div>'+
    '<label>feasibility policy：terrain vertical clearance (m)</label>'+
    '<input id="layeredTerrainClearance" type="number" step="0.1" placeholder="必填，无默认值" value="'+
    (model.feasibility.clearance===null?'':model.feasibility.clearance)+'">'+
    '<label>feasibility policy source</label>'+
    '<input id="layeredFeasibilitySource" placeholder="工程依据（确认时必填）">'+
    '<label class="checkbox-row"><input type="checkbox" id="layeredFeasibilityConfirmed"> 显式确认 feasibility policy</label>'+
    '<label>cost policy：ground λ</label>'+
    '<input id="layeredGroundLambda" type="number" step="0.1" placeholder="留空 = null（待确认）" value="'+
    (model.cost.domains[0].lambda===null?'':model.cost.domains[0].lambda)+'">'+
    '<label>cost policy：air traffic λ</label>'+
    '<input id="layeredAirLambda" type="number" step="0.1" placeholder="留空 = null（待确认）" value="'+
    (model.cost.domains[1].lambda===null?'':model.cost.domains[1].lambda)+'">'+
    '<label>cost policy：environment / obstacle λ</label>'+
    '<input id="layeredEnvironmentLambda" type="number" step="0.1" placeholder="留空 = null（待确认）" value="'+
    (model.cost.domains[2].lambda===null?'':model.cost.domains[2].lambda)+'">'+
    '<label>cost policy source</label>'+
    '<input id="layeredCostSource" placeholder="工程依据（确认时必填）">'+
    '<label class="checkbox-row"><input type="checkbox" id="layeredCostConfirmed"> 显式确认 cost policy</label>'+
    '<h3>Blocking 项</h3>'+blockerList(model.blockers)+
    '<h3>Risk Framework V2（soft cost 输入）</h3>'+
    '<div class="parameter-note">只读取每格 domain index；Risk V2 <b>overall 不使用</b>。λ&gt;0 的 domain 在任一候选格 '+
    'missing/unresolved/pending 时该格不可遍历（绝不使用 unknown penalty 或默认 risk）；λ=0 的 domain 不作为规划输入。</div>'+
    '<div class="list-row"><span><b>grid_risk_v2</b> '+statusBadge(model.risk.status||'not_calculated')+
    '<small>input fingerprint '+escapeHtml(model.risk.input_fingerprint||'—')+
    ' · policy fingerprint '+escapeHtml(model.risk.policy_fingerprint||'—')+
    ' · overall_used '+escapeHtml(String(model.risk.overall_used===true))+'</small></span></div>'+
    '<h3>Selected layer feasibility mask</h3>'+
    '<div class="list-row"><span><b>'+escapeHtml(model.mask?.altitude_layer_id||'—')+'</b> '+statusBadge(model.mask?.status||'not_calculated')+
    '<small>feasible '+((counts.feasible)||0)+' · blocked '+((counts.blocked)||0)+' · unknown '+((counts.unknown)||0)+
    ' · applicability '+escapeHtml(model.mask?.current_applicability||'—')+'</small>'+
    '<small>mask fingerprint '+escapeHtml(model.mask?.mask_fingerprint||'—')+'</small></span></div>'+
    '<div class="parameter-note">unknown ≠ feasible ≠ blocked；unknown 永不进入 A*。building_count=0 表示该格没有建筑垂向约束；'+
    'valid_height_fraction&lt;1 或缺失高度/地面高程一律 unknown，绝不当 0。适飞空域仍 display_only，不进入 mask、搜索或 fingerprint。</div>'+
    '<h3>候选结果</h3>'+
    '<div class="scroll-list">'+(model.candidates.items.length?model.candidates.items.map(candidateRow).join(''):
      '<div class="list-row"><span>尚无 candidate：'+escapeHtml(model.blockers.length?'请先补齐 confirmed 参数':'请点击运行 candidate')+'</span></div>')+'</div>'+
    '<div class="parameter-note">candidate 不会写入 operational_routes / CNS，也不会自动创建 RouteOperatingLayer。'+
    '风险权重与高度层都只来自显式确认的工程输入。</div>';
}

export function layeredRequestPayload(flow){
  const model=layeredRoutePlannerModel(flow);
  return {
    scenario_route_id:model.selectedRouteId,
    altitude_layer_id:model.selectedLayerId,
    source:model.source,
    confirmed:model.requestConfirmed,
  };
}

export function layeredEvaluatePayload(close=id=>document.getElementById(id)){
  const value=id=>{const node=close(id);return node?node.value:'';};
  const checked=id=>{const node=close(id);return node?node.checked===true:false;};
  const number=id=>{const raw=value(id);return raw===''||raw===null?null:Number(raw);};
  const request={scenario_route_id:value('layeredRouteSelect')||null,altitude_layer_id:value('layeredAltitudeLayerSelect')||null,
    source:value('layeredRequestSource')||'',confirmed:checked('layeredRequestConfirmed')};
  const feasibility={terrain_vertical_clearance_m:number('layeredTerrainClearance'),source:value('layeredFeasibilitySource')||'',
    confirmed:checked('layeredFeasibilityConfirmed')};
  const cost={ground_lambda:number('layeredGroundLambda'),air_traffic_lambda:number('layeredAirLambda'),
    environment_obstacle_lambda:number('layeredEnvironmentLambda'),source:value('layeredCostSource')||'',
    confirmed:checked('layeredCostConfirmed')};
  return {request,feasibility,cost};
}

export function bindLayeredRoutePlanner(context){
  const {$,panelError,actionButton,resourceAction}=context;
  const current=()=>layeredRoutePlannerModel(context.flow());
  actionButton('saveLayeredRequest',async()=>{
    const {request}=layeredEvaluatePayload(id=>$(id));
    await resourceAction('/api/layered-route-planning-request',request);
  });
  actionButton('saveLayeredFeasibilityPolicy',async()=>{
    const {feasibility}=layeredEvaluatePayload(id=>$(id));
    await resourceAction('/api/layered-route-feasibility-policy',feasibility);
  });
  actionButton('saveLayeredCostPolicy',async()=>{
    const {cost}=layeredEvaluatePayload(id=>$(id));
    await resourceAction('/api/layered-route-cost-policy',cost);
  });
  actionButton('evaluateLayeredCandidate',async()=>{
    const model=current();
    if(model.blockers.length){
      panelError('当前存在 blocking 项：请先补齐 confirmed 的高度层、clearance 与 λ，再运行 candidate。'+LAYERED_BLOCKED_NOTE);
      return;
    }
    await resourceAction('/api/layered-route-candidates/evaluate-real',{});
  });
}
