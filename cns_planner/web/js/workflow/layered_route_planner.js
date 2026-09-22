import {escapeHtml,statusBadge,statusText} from './common.js';

// =========================================================
// 「操作 → 分层候选」的公共部分 + Layered Risk-Aware Route Planner V1 面板。
//
// 算法分流严格依据 ``flow.layered_route_planner_readiness.algorithm.algorithm_id``：
//   * ``layered_route_planner_v1``（A* + legacy λ）→ 本文件的 V1 面板与 V1 λ 语义；
//   * ``layered_risk_aware_theta_star_v2@2.0``     → layered_theta_v2.js 的 Theta* V2 视图。
// 本文件只保留两边共用的 planning request / scenario / 显式 AltitudeLayer 与
// terrain-building feasibility 控件，避免两套面板各自发明字段与 id。
//
// V1 继续在 UI 上明示的硬规则：
//   * cruise altitude layer 必须显式选择：不从 RouteAltitudeProfile 推断、不给默认值；
//   * 没有默认地形净空、没有默认 λ：``null != 0``，显式 ``0`` 是合法的工程决定；
//   * 结果只是 candidate：不是运行航路，CNS 未评估，连续验证仍必须执行。
// =========================================================
export const LAYERED_PLANNER_ALGORITHM_TYPE='layered_route_planner';
export const LAYERED_PLANNER_ALGORITHM_ID='layered_route_planner_v1';
//: Theta* V2 的 exact algorithm id：只有它才把「分层候选」切换成 Theta* V2 视图。
export const THETA_STAR_V2_ALGORITHM_ID='layered_risk_aware_theta_star_v2';
export const THETA_STAR_V2_ALGORITHM_VERSION='2.0';
export const LAYERED_V1_ALGORITHM_ID='layered_route_planner_v1';
export const LAYERED_V1_ALGORITHM_VERSION='1.0';
//: 视图角色：Theta* V2 是 production layered planner，V1 只是 legacy/baseline。
export const LAYERED_PLANNER_PRODUCTION_ROLE='production layered planner';
export const LAYERED_PLANNER_LEGACY_ROLE='legacy/baseline layered planner';
export const LAYERED_CANDIDATE_LABEL='分层候选（candidate，非运行航路）';
export const LAYERED_BLOCKED_NOTE='没有 confirmed 参数时一律 blocked：不提供任何默认高度、净空或 λ。';
export const THETA_STAR_V2_BLOCKED_NOTE='Theta* V2 不读取 legacy LayeredRouteCostPolicy 的 λ：'
  +'population × shelter risk 才是搜索代价，缺 shelter policy / population_shelter 场 / confirmed 巡航高度时才 blocked。';
export const COST_DOMAIN_LABELS={
  ground:'Ground（地面暴露）',
  air_traffic:'Air / Traffic（空中交通暴露）',
  environment_obstacle:'Environment / Obstacle（工程环境-障碍物）',
};

// ---------------------------------------------------------------- algorithm switch

/**
 * readiness.algorithm.algorithm_id 是视图选择的唯一依据（缺失时退回 V1 基线面板）。
 *
 * Theta* V2 现在是项目默认的 layered planner，因此真实项目的 readiness 总是报告 V2；这里
 * 保留"缺失信息时绝不冒充 Theta* V2"的保守回退，未知/缺失一律按 legacy 面板显示。
 */
export function layeredAlgorithmId(flow){
  const id=flow?.layered_route_planner_readiness?.algorithm?.algorithm_id;
  return typeof id==='string'&&id?id:LAYERED_PLANNER_ALGORITHM_ID;
}

export function layeredAlgorithmVersion(flow){
  const version=flow?.layered_route_planner_readiness?.algorithm?.algorithm_version;
  return typeof version==='string'&&version?version:null;
}

/** 只有 exact algorithm id 等于 layered_risk_aware_theta_star_v2 才是 Theta* V2。 */
export function layeredPlannerUsesThetaStarV2(flow){
  return layeredAlgorithmId(flow)===THETA_STAR_V2_ALGORITHM_ID;
}

export function layeredPlannerAlgorithmLabel(flow){
  const version=layeredAlgorithmVersion(flow);
  return (layeredPlannerUsesThetaStarV2(flow)
    ?'Layered Risk-Aware Theta* V2'
    :'Layered Risk-Aware Route Planner V1')+(version?'@'+version:'');
}

/** 当前 layered planner 的角色文案：只有 Theta* V2 是 production layered planner。 */
export function layeredPlannerRoleLabel(flow){
  return layeredPlannerUsesThetaStarV2(flow)
    ?LAYERED_PLANNER_PRODUCTION_ROLE
    :LAYERED_PLANNER_LEGACY_ROLE;
}

// ---------------------------------------------------------------- public model

/**
 * 公共 planning request / scenario / 显式 AltitudeLayer 投影（V1 与 V2 完全同源）。
 * @param {object} flow published workflow snapshot
 */
export function layeredPlanningRequestModel(flow){
  const readiness=flow?.layered_route_planner_readiness||{};
  const request=flow?.layered_route_planning_request||readiness.request||{};
  const catalog=readiness.altitude_layer_catalog||{};
  // AltitudeLayer 必须显式选择：目录优先取 readiness，缺失时退回 flow.spatial_3d 的既有定义
  // （绝不从 RouteAltitudeProfile 推断，也不给默认层）。
  const layerSource=(Array.isArray(catalog.layers)&&catalog.layers.length)
    ?catalog.layers
    :((flow?.spatial_3d||{}).altitude_layers||[]);
  return {
    scenarioRoutes:(flow?.scenario_routes||[]).map(route=>({
      routeId:route.route_id,
      direction:route.direction||((route.start_node_id&&route.end_node_id)?(route.start_node_id+'→'+route.end_node_id):''),
    })),
    selectedRouteId:request.scenario_route_id||readiness.scenario_route?.route_id||null,
    scenarioRouteStatus:readiness.scenario_route?.status||'not_resolved',
    scenarioRouteCount:Number.isFinite(readiness.scenario_route?.count)?readiness.scenario_route.count:null,
    layers:layerSource.map(layer=>({
      layerId:layer.altitude_layer_id,name:layer.name,
      nominal:Number.isFinite(layer.nominal_altitude_m)?layer.nominal_altitude_m:null,
      verticalReference:layer.vertical_reference,
      status:layer.status,reasons:layer.reasons||[],
    })),
    layerCatalogStatus:catalog.status||'not_configured',
    selectedLayerId:request.altitude_layer_id||null,
    cruiseAltitude:catalog.cruise_altitude||null,
    requestStatus:request.status||'pending_confirmation',
    requestConfirmed:request.confirmed===true,
    source:request.source||null,
  };
}

/** 公共 terrain / building feasibility 投影（V1 与 V2 完全同源）。 */
export function layeredFeasibilityModel(flow){
  const readiness=flow?.layered_route_planner_readiness||{};
  const feasibility=flow?.layered_route_feasibility_policy||{};
  const source=typeof feasibility.source==='string'?feasibility.source:'';
  return {
    status:feasibility.status||'blocked',
    clearance:Number.isFinite(feasibility.terrain_vertical_clearance_m)?feasibility.terrain_vertical_clearance_m:null,
    parameterStatus:feasibility.parameter_status||null,
    source:source||null,
    // 未配置时的后端占位说明不是工程依据：不回填成可编辑值，否则一次无修改的保存就会
    // 把占位文本当成 explicit source 提交。
    sourceInput:source.startsWith('未配置')?'':source,
    confirmed:feasibility.confirmed===true,
    fingerprint:feasibility.fingerprint||null,
    policyStatus:readiness.feasibility_policy?.status||feasibility.status||'blocked',
    maskingConfirmed:readiness.feasibility_policy?.status==='confirmed',
    buildingClearance:readiness.building_clearance_policy||{},
    mask:readiness.feasibility_mask||{},
    sources:readiness.sources||{},
  };
}

// ---------------------------------------------------------------- public fields

/** 公共 request 字段（scenario / 显式 AltitudeLayer / source / confirmed）。 */
export function renderLayeredPlanningRequestFields(model,{altitudeNote}={}){
  const routeOptions=model.scenarioRoutes.map(route=>
    '<option value="'+escapeHtml(route.routeId)+'"'+(route.routeId===model.selectedRouteId?' selected':'')+'>'+
    escapeHtml(route.routeId+(route.direction?' · '+route.direction:''))+'</option>').join('');
  const layerOptions=model.layers.map(layer=>
    '<option value="'+escapeHtml(layer.layerId)+'"'+(layer.layerId===model.selectedLayerId?' selected':'')+'>'+
    escapeHtml(layer.layerId+(layer.nominal===null?' · nominal 待确认':' · '+layer.nominal)+' m')+'</option>').join('');
  const note=altitudeNote||('AGL / WGS84 高度层在缺少显式 DEM surface 或 geoid 证据时保持 blocked：'
    +'V1 不做 datum/geoid 猜测或伪转换。');
  return '<label>scenario / OD 航路</label>'+
    '<select id="layeredRouteSelect">'+(routeOptions||'<option value="">当前没有 scenario/OD 航路</option>')+'</select>'+
    '<label>巡航高度层（显式选择，不从 profile 推断）</label>'+
    '<select id="layeredAltitudeLayerSelect">'+
    (layerOptions||'<option value="">高度层目录为空（'+model.layerCatalogStatus+' · 共 0 层）：没有可选高度层</option>')+
    '</select>'+
    '<div class="parameter-note">selected layer cruise altitude：'+
    escapeHtml(model.cruiseAltitude?.status||'not_resolved')+
    (Number.isFinite(model.cruiseAltitude?.altitude_egm2008_m)?' · '+formatNumber(model.cruiseAltitude.altitude_egm2008_m)+' m EGM2008':'')+
    (model.cruiseAltitude?.reason?' · '+escapeHtml(model.cruiseAltitude.reason):'')+
    '<br>'+escapeHtml(note)+'</div>'+
    '<label>request source / evidence</label>'+
    '<input id="layeredRequestSource" placeholder="工程依据（确认时必填）" value="'+escapeHtml(model.source||'')+'">'+
    '<label class="checkbox-row"><input type="checkbox" id="layeredRequestConfirmed"'+(model.requestConfirmed?' checked':'')+'> 显式确认本次规划请求</label>';
}

/**
 * 公共 feasibility policy 字段（terrain clearance 无默认值）。
 *
 * source 与 explicit confirmation 必须从后端 policy 回填：否则每次保存后的重渲染都会
 * 把两者清空，第二次保存就把已 confirmed 的策略降级成 pending_confirmation
 * （Phase 3.5 BUG-ROUTE-002）。
 */
export function renderLayeredFeasibilityFields(model){
  const feasibility=model?.feasibility||{};
  const clearance=Number.isFinite(feasibility.clearance)?feasibility.clearance:null;
  return '<label>feasibility policy：terrain vertical clearance (m)</label>'+
    '<input id="layeredTerrainClearance" type="number" step="0.1" placeholder="必填，无默认值" value="'+
    (clearance===null?'':clearance)+'">'+
    '<label>feasibility policy source</label>'+
    '<input id="layeredFeasibilitySource" placeholder="工程依据（确认时必填）" value="'+
    escapeHtml(feasibility.sourceInput||'')+'">'+
    '<label class="checkbox-row"><input type="checkbox" id="layeredFeasibilityConfirmed"'+
    (feasibility.confirmed===true?' checked':'')+'> 显式确认 feasibility policy</label>';
}

// ---------------------------------------------------------------- V1 model

export function layeredRoutePlannerModel(flow){
  const readiness=flow?.layered_route_planner_readiness||{};
  const cost=flow?.layered_route_cost_policy||{};
  const candidates=flow?.layered_route_candidates||{};
  const costPolicy=readiness.cost_policy||{};
  const requestModel=layeredPlanningRequestModel(flow);
  const feasibilityModel=layeredFeasibilityModel(flow);
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
    // 视图选择的唯一依据（V1 面板自己只在 V1 下渲染）。
    algorithmId:layeredAlgorithmId(flow),
    usesThetaStarV2:layeredPlannerUsesThetaStarV2(flow),
    status:readiness.status||'blocked',
    algorithm:readiness.algorithm||{},
    scenarioRoutes:requestModel.scenarioRoutes,
    selectedRouteId:requestModel.selectedRouteId,
    scenarioRouteStatus:requestModel.scenarioRouteStatus,
    layers:requestModel.layers,
    selectedLayerId:requestModel.selectedLayerId,
    cruiseAltitude:requestModel.cruiseAltitude,
    requestStatus:requestModel.requestStatus,
    requestConfirmed:requestModel.requestConfirmed,
    source:requestModel.source,
    feasibility:{
      status:feasibilityModel.status,
      clearance:feasibilityModel.clearance,
      parameterStatus:feasibilityModel.parameterStatus,
      // source / confirmed 由同一个 renderLayeredFeasibilityFields 渲染：V1 面板也必须
      // 拿到后端保存的值，否则保存后重渲染会清空它们。
      source:feasibilityModel.source,
      sourceInput:feasibilityModel.sourceInput,
      confirmed:feasibilityModel.confirmed,
      fingerprint:feasibilityModel.fingerprint,
      maskingConfirmed:feasibilityModel.maskingConfirmed,
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
    buildingClearance:feasibilityModel.buildingClearance,
    mask:feasibilityModel.mask,
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
    overlayMask:selectOverlayMask(candidates,flow?.layered_route_planning_request||readiness.request||{}),
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

/**
 * V1 面板（A* + legacy λ）。只有 algorithm_id = layered_route_planner_v1 时才渲染。
 * Theta* V2 的视图在 layered_theta_v2.js，绝不复用下面的 legacy λ 语义。
 */
export function renderLayeredRoutePlannerPanel(flow){
  const model=layeredRoutePlannerModel(flow);
  const counts=model.mask?.counts||{};
  return '<h3>分层候选规划（legacy/baseline：Layered Risk-Aware Route Planner V1）</h3>'+
    '<div class="parameter-note">当前 <code>layered_route_planner</code> 是 <b>legacy/baseline</b> 实现（V1 A* + '
    +'Risk Framework V2 soft cost）。项目默认已是 <b>Layered Risk-Aware Theta* V2</b>；只有显式保存了 V1 selection '
    +'的项目才会看到本面板，系统不会静默迁移到 V2。</div>'+
    '<div class="parameter-note">pipeline：scenario/OD 航路 → <b>显式 AltitudeLayer</b> → terrain/building '+
    'coarse 可行性 mask → MH/T L8 A* → Risk Framework V2 soft cost → LayeredRouteCandidate。'+
    '结果只是 <b>candidate</b>：不得直接写入运行航路或 CNS；连续验证（精确 footprint 与水平净空）仍需后续执行。'+
    '这是 <b>coarse_strategic_vertical_envelope</b>，不是 exact footprint，也不是水平/精确建筑净空结论。</div>'+
    '<div class="parameter-note">readiness：'+statusBadge(model.status)+' · '+
    escapeHtml(model.algorithm.algorithm_id||LAYERED_PLANNER_ALGORITHM_ID)+'@'+escapeHtml(model.algorithm.algorithm_version||'1.0')+
    ' · role '+escapeHtml(layeredPlannerRoleLabel(flow))+
    ' · '+LAYERED_BLOCKED_NOTE+'</div>'+
    renderLayeredPlanningRequestFields(model)+
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
    renderLayeredFeasibilityFields(model)+
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

// ---------------------------------------------------------------- public payloads

/**
 * 控件读取契约：优先 DOM 实时属性，回退 HTML 属性。
 *
 * 与 Theta* V2 面板（layered_theta_v2.js 的 fieldValue / checkedFrom）保持一致：把
 * input 的 value / checked 只当作属性携带时也必须能读到，绝不静默返回空值。
 */
function fieldValue(node){
  if(!node)return '';
  const direct=node.value;
  if(direct!==undefined&&direct!==null)return String(direct);
  const attribute=(node.attributes||{}).value;
  return attribute===undefined||attribute===null?'':String(attribute);
}

function checkedField(node){
  if(!node)return false;
  if(node.checked===true||node.checked==='true')return true;
  const attributes=node.attributes||{};
  return attributes.checked===true||attributes.checked==='true';
}

/** 公共 request payload（空串一律提交 null / false，不补默认值）。 */
export function layeredRequestPayloadFrom(close=id=>document.getElementById(id)){
  const value=id=>fieldValue(close(id));
  const checked=id=>checkedField(close(id));
  return {
    scenario_route_id:value('layeredRouteSelect')||null,
    altitude_layer_id:value('layeredAltitudeLayerSelect')||null,
    source:value('layeredRequestSource')||'',
    confirmed:checked('layeredRequestConfirmed'),
  };
}

/** 公共 feasibility policy payload：terrain clearance 空值 = null（无默认值）。 */
export function layeredFeasibilityPayloadFrom(close=id=>document.getElementById(id)){
  const value=id=>fieldValue(close(id));
  const checked=id=>checkedField(close(id));
  const raw=value('layeredTerrainClearance');
  return {
    terrain_vertical_clearance_m:raw===''||raw===null?null:Number(raw),
    source:value('layeredFeasibilitySource')||'',
    confirmed:checked('layeredFeasibilityConfirmed'),
  };
}

/** V1 legacy cost policy payload（λ 空值 = null，显式 0 保留为 0）。 */
export function layeredCostPayloadFrom(close=id=>document.getElementById(id)){
  const value=id=>fieldValue(close(id));
  const checked=id=>checkedField(close(id));
  const number=id=>{const raw=value(id);return raw===''||raw===null?null:Number(raw);};
  return {
    ground_lambda:number('layeredGroundLambda'),
    air_traffic_lambda:number('layeredAirLambda'),
    environment_obstacle_lambda:number('layeredEnvironmentLambda'),
    source:value('layeredCostSource')||'',
    confirmed:checked('layeredCostConfirmed'),
  };
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
  return {
    request:layeredRequestPayloadFrom(close),
    feasibility:layeredFeasibilityPayloadFrom(close),
    cost:layeredCostPayloadFrom(close),
  };
}

// ---------------------------------------------------------------- V1 bind

/**
 * 绑定 V1 面板（A* + legacy λ）。Theta* V2 的绑定在 layered_theta_v2.js：
 * 那里的 evaluate 绝不要求/提示 legacy λ。
 */
export function bindLayeredRoutePlanner(context){
  const {$,panelError,actionButton,resourceAction}=context;
  const current=()=>layeredRoutePlannerModel(context.flow());
  actionButton('saveLayeredRequest',async()=>{
    await resourceAction('/api/layered-route-planning-request',layeredRequestPayloadFrom(id=>$(id)));
  });
  actionButton('saveLayeredFeasibilityPolicy',async()=>{
    await resourceAction('/api/layered-route-feasibility-policy',layeredFeasibilityPayloadFrom(id=>$(id)));
  });
  actionButton('saveLayeredCostPolicy',async()=>{
    await resourceAction('/api/layered-route-cost-policy',layeredCostPayloadFrom(id=>$(id)));
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
