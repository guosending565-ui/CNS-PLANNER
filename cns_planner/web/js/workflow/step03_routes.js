import {escapeHtml,shell,statusBadge,statusText} from './common.js';
import {bindRouteVerticalProfile,renderRouteVerticalProfilePanel} from './route_vertical_profile.js';

function metric(value,unit=''){const number=Number(value);return value!==null&&value!==undefined&&Number.isFinite(number)?number.toFixed(2)+(unit?' '+unit:''):'—';}

export const PLANNER_V1='route_planner_v1';
export const PLANNER_V2='risk_aware_route_planner_v2';
export const ROUTE_PLANNER_TYPE='route_planner';

function turnCount(path){
  const points=(path||[]).filter(point=>Array.isArray(point)&&point.length>=2);
  let turns=0;
  for(let index=1;index<points.length-1;index++){
    const a=points[index-1],b=points[index],c=points[index+1];
    if(Math.abs((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))>1e-12)turns++;
  }
  return turns;
}

function pathLengthM(path){let total=0;for(let index=1;index<(path||[]).length;index++){const a=path[index-1],b=path[index],lat1=a[1]*Math.PI/180,lat2=b[1]*Math.PI/180,dlat=lat2-lat1,dlon=(b[0]-a[0])*Math.PI/180,h=Math.sin(dlat/2)**2+Math.cos(lat1)*Math.cos(lat2)*Math.sin(dlon/2)**2;total+=6371008.8*2*Math.asin(Math.sqrt(h));}return (path||[]).length>1?total:null;}

export function filterReferenceSites(items,{workspace=null,search='',region='',siteType=''}={}){
  const bbox=workspace?.bbox||null,needle=String(search||'').trim().toLocaleLowerCase();
  return (items||[]).filter(item=>{
    const point=item.coordinate;
    if(!Array.isArray(point)||point.length!==2||item.quality==='invalid')return false;
    if(bbox&&!(bbox[0]<=point[0]&&point[0]<=bbox[2]&&bbox[1]<=point[1]&&point[1]<=bbox[3]))return false;
    if(region&&item.region!==region)return false;
    if(siteType&&item.site_type!==siteType)return false;
    const haystack=[item.name,item.location,item.region,item.site_type,item.reference_site_id].join(' ').toLocaleLowerCase();
    return !needle||haystack.includes(needle);
  });
}

function pointInWorkspace(point,workspace){
  const bbox=workspace?.bbox;
  return Array.isArray(point)&&point.length===2&&(!bbox||(bbox[0]<=point[0]&&point[0]<=bbox[2]&&bbox[1]<=point[1]&&point[1]<=bbox[3]));
}

function routeTouchesWorkspace(route,workspace){
  const path=route.path||[],bbox=workspace?.bbox;
  if(!bbox)return path.length>1;
  if(!path.length)return false;
  const xs=path.map(point=>point?.[0]).filter(Number.isFinite),ys=path.map(point=>point?.[1]).filter(Number.isFinite);
  return xs.length>1&&Math.max(...xs)>=bbox[0]&&Math.min(...xs)<=bbox[2]&&Math.max(...ys)>=bbox[1]&&Math.min(...ys)<=bbox[3];
}

export function referenceOverlayModel(flow,layers={routes:true,points:true,landingSites:true}){
  const catalog=flow?.reference_routes||{};
  return {
    referenceRoutes:layers.routes?(catalog.items||[]).filter(item=>routeTouchesWorkspace(item,flow?.workspace)):[],
    referencePoints:layers.points?(catalog.points||[]).filter(item=>pointInWorkspace(item.coordinate,flow?.workspace)):[],
    referenceLandingSites:layers.landingSites?filterReferenceSites(flow?.reference_landing_sites?.items||[],{workspace:flow?.workspace}):[],
    scenarioRoutes:flow?.scenario_routes||[],
    operationalRoutes:flow?.operational_routes||[],
  };
}

export function findAlgorithmManifest(catalog,algorithmType,algorithmId,version){
  return (catalog||[]).find(item=>item.algorithm_type===algorithmType&&item.algorithm_id===algorithmId&&String(item.version)===String(version))||null;
}

function schemaDefaults(manifest){
  const properties=manifest?.parameter_schema?.properties||{},result={};
  Object.entries(properties).forEach(([name,schema])=>{if(schema&&Object.prototype.hasOwnProperty.call(schema,'default'))result[name]=schema.default;});
  return result;
}

export function effectiveParameters(manifest,selection){
  const supplied=(selection&&typeof selection.parameters==='object'&&selection.parameters)?selection.parameters:{},result=schemaDefaults(manifest);
  Object.entries(supplied).forEach(([name,value])=>{if(value!==null)result[name]=value;});
  const declared=manifest?.parameter_schema?.properties||{};
  return Object.entries(result).filter(([name])=>Object.prototype.hasOwnProperty.call(declared,name)||name in supplied)
    .sort(([a],[b])=>a.localeCompare(b)).map(([name,value])=>{
      const schema=declared[name]||{};
      return {name,value,source:Object.prototype.hasOwnProperty.call(supplied,name)?'selection':'schema_default',declared:Object.keys(declared).includes(name),
        minimum:schema.minimum,maximum:schema.maximum};
    });
}

export function plannerCardModel(flow){
  const selection=flow?.algorithm_selection?.[ROUTE_PLANNER_TYPE]||{};
  if(!selection.algorithm_id)return null;
  const manifest=findAlgorithmManifest(flow?.algorithm_catalog,ROUTE_PLANNER_TYPE,selection.algorithm_id,selection.version);
  if(!manifest){
    return {status:'manifest_missing',selection:{algorithm_id:selection.algorithm_id,version:selection.version},
      message:'当前算法选择在 algorithm_catalog 中没有精确匹配的 Manifest；不显示任何推断的限制说明。'};
  }
  return {status:'passed',manifest,selection:{algorithm_id:manifest.algorithm_id,version:manifest.version},
    effective_parameters:effectiveParameters(manifest,selection),active:true};
}

function listBlock(title,values){
  const items=(values||[]).filter(Boolean);
  return '<div class="parameter-note"><b>'+escapeHtml(title)+'</b>'+(items.length?'<ul>'+items.map(item=>'<li>'+escapeHtml(item)+'</li>').join('')+'</ul>':'<br>—')+'</div>';
}

function plannerCard(model){
  if(!model)return '';
  if(model.status!=='passed')return '<h3>当前规划器</h3><div class="parameter-note">'+escapeHtml(model.message||'Manifest 不可用')+'</div>';
  const m=model.manifest,parameters=model.effective_parameters||[];
  const rows=parameters.length?parameters.map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.name)+'</b><small>'+escapeHtml(String(item.value))+' · '+(item.source==='selection'?'来自当前参数选择':'来自 schema 默认值')+(item.declared?'':' · schema 未声明')+'</small></span></div>').join(''):'<div class="empty-note">Manifest 未声明有效参数</div>';
  return '<h3>当前规划器 '+statusBadge('passed')+'</h3>'
    +'<div class="parameter-note">以下内容直接来自 algorithm_catalog / Manifest，前端不硬编码算法限制。<br>'
    +'<b>'+escapeHtml(m.name||m.algorithm_id)+'</b> · <code>'+escapeHtml(m.algorithm_id)+'@'+escapeHtml(m.version)+'</code> · 成熟度 <code>'+escapeHtml(m.maturity)+'</code> · 提供方 '+escapeHtml(m.provider)+'</div>'
    +'<div class="flow-summary">'+escapeHtml(m.description||'')+'</div>'
    +listBlock('输入 contract',m.inputs)+listBlock('假设 assumptions',m.assumptions)+listBlock('局限 limitations',m.limitations)
    +'<h3>有效参数</h3><div class="scroll-list">'+rows+'</div>';
}

function plannerEntry(route,result,plannerId){
  if(!result||result.algorithm_id!==plannerId)return null;
  return {status:result.status,
    path_length_m:Number.isFinite(result.distance_m)?result.distance_m:pathLengthM(result.path),
    segment_count:Array.isArray(result.path)?result.path.length:null,
    turn_count:turnCount(result.path),
    risk_exposure_index_m:result.risk_exposure_index_m??null,
    max_risk_index:result.max_risk_index??null};
}

export function routePlannerComparisonModel(flow){
  const results=flow?.operational_routes||[];
  const rows=(flow?.scenario_routes||[]).map(route=>{
    const result=results.find(item=>item.route_id===route.route_id);
    return {route_id:route.route_id,direction:route.direction||'',
      [PLANNER_V1]:plannerEntry(route,result,PLANNER_V1),
      [PLANNER_V2]:plannerEntry(route,result,PLANNER_V2)};
  });
  const hasV1=rows.some(row=>row[PLANNER_V1]),hasV2=rows.some(row=>row[PLANNER_V2]);
  const hasReference=(flow?.reference_routes?.items||[]).length>0;
  return {rows,hasV1,hasV2,bothPresent:hasV1&&hasV2,hasReference,
    referenceNote:hasReference?'真实参考航线只与运行航线作长度/几何并列，不作优劣结论。':'当前项目没有参考航线可比。',
    semantics:'factual_side_by_side_no_superiority_conclusion',automatic_ranking:false};
}

function comparisonPanelV2(flow,model){
  if(!model.rows.length)return '';
  if(!model.bothPresent)return '<h3>V1 / V2 结果并列</h3><div class="parameter-note">当前只有 '+(model.hasV2?'V2':'V1')+' 结果；切换到另一个规划器并重新生成运行航路后才会出现并列比较。本面板只做事实并列，不作优劣结论。</div>';
  const body=model.rows.map(row=>'<div class="list-row route-row"><span><b>'+escapeHtml(row.route_id)+'</b> '+escapeHtml(row.direction)+'<small>V1 length '+metric(row[PLANNER_V1]?.path_length_m,'m')+' · vertices '+escapeHtml(String(row[PLANNER_V1]?.segment_count??'—'))+' · turns '+escapeHtml(String(row[PLANNER_V1]?.turn_count??'—'))+'</small><small>V2 length '+metric(row[PLANNER_V2]?.path_length_m,'m')+' · vertices '+escapeHtml(String(row[PLANNER_V2]?.segment_count??'—'))+' · turns '+escapeHtml(String(row[PLANNER_V2]?.turn_count??'—'))+' · risk exposure '+metric(row[PLANNER_V2]?.risk_exposure_index_m,'index·m')+'</small></span></div>').join('');
  return '<h3>V1 / V2 结果并列</h3><div class="parameter-note">只并列展示：长度/几何来自各自 planner 的真实输出与已发布路径；risk exposure 为 V2 自报相对工程指数。'
    +'<b>本面板不判定“更好”</b>，不排名、不评分、不推荐算法。V1 顶点更少是因为它对共线点做了简化，V2 保留完整 grid path 与网格中心（无 smoothing），这只说明输出契约不同。</div><div class="scroll-list route-list">'+body+'</div>';
}

function endpointOptions(nodes,selectedId){
  return (nodes||[]).map(node=>'<option value="'+escapeHtml(node.node_id)+'" '+(node.node_id===selectedId?'selected':'')+'>'+escapeHtml(node.node_id)+' · '+escapeHtml(node.name)+'</option>').join('');
}

function odScenarioPanel(flow){
  const nodes=flow.nodes||[];
  const disabled=nodes.length<2?'disabled':'';
  const first=nodes[0]?.node_id,last=nodes[nodes.length-1]?.node_id;
  return '<h3>起点 → 终点 创建航路</h3><div class="parameter-note">显式指定两个 node，只创建这一条（或这一对）场景航路，不会因为参考点数量自动生成全连接。创建后会替换当前场景航路并清空运行航路，需要重新生成运行航路。</div>'
    +'<div class="form-grid"><label>起点<select id="odStartNode">'+endpointOptions(nodes,first)+'</select></label><label>终点<select id="odEndNode">'+endpointOptions(nodes,last)+'</select></label></div>'
    +'<label>方向<select id="odDirection"><option value="ab">仅 起点→终点</option><option value="ba">仅 终点→起点</option><option value="both" selected>双向（两个 route_id）</option></select></label>'
    +'<button class="primary full" id="createOdRoute" '+disabled+'>创建航路</button>'
    +'<div class="parameter-note">兼容说明：下方“生成场景航路”保留原 all-pairs 行为，供旧项目继续使用；新项目优先使用本面板。</div>';
}

function selectedReferencePanel(flow,selected){
  if(!selected)return '<div class="empty-note">点击地图或下方列表中的真实航线/航路点查看来源事实。</div>';
  const catalog=flow.reference_routes||{};
  const item=selected.kind==='route'
    ?(catalog.items||[]).find(value=>value.reference_route_id===selected.id)
    :(catalog.points||[]).find(value=>value.reference_route_point_id===selected.id);
  if(!item)return '<div class="empty-note">所选参考对象当前不可用</div>';
  const source=item.source||{},coordinate=Array.isArray(item.coordinate)?item.coordinate.map(value=>Number(value).toFixed(6)).join(', '):'—';
  return '<div class="reference-detail"><b>'+escapeHtml(item.name||item.route_number||selected.id)+'</b><small>分类 '+escapeHtml(item.category||item.type||'未注明')+' · 航线编号 '+escapeHtml(item.route_number||'—')+(selected.kind==='point'?' · 点序 '+escapeHtml(item.sequence):'')+'</small><small>坐标 '+escapeHtml(coordinate)+' · CRS '+escapeHtml(item.crs_status||'pending_confirmation')+'</small><small>source '+escapeHtml(source.file||'未记录')+(source.sheet?' / '+escapeHtml(source.sheet):'')+(source.row?' / row '+escapeHtml(source.row):'')+'</small></div>';
}

function referenceRoutesPanel(flow,selected){
  const catalog=flow.reference_routes||{},routes=catalog.items||[],points=catalog.points||[];
  const rows=routes.map(route=>'<button class="list-row reference-route-row" data-select-reference-route="'+escapeHtml(route.reference_route_id)+'"><span><b>'+escapeHtml(route.name||route.reference_route_id)+'</b><small>航线编号 '+escapeHtml(route.route_number)+' · '+escapeHtml(route.category||'分类未注明')+' · '+(route.ordered_points||[]).length+' 点 · '+metric(route.length_m,'m')+'</small></span></button>').join('');
  return '<h3>真实参考航线 '+statusBadge(catalog.status||'not_calculated')+'</h3><div class="parameter-note">reference_routes / route points 为只读参考层，不会写入 flow.nodes、scenario_routes 或 operational_routes。全部中间点均按 sequence 保留；CRS 为 pending_confirmation 时仅按源数值临时显示。</div>'+selectedReferencePanel(flow,selected)+'<div class="flow-summary">航线 '+(catalog.count||0)+' 条 · 航路点 '+(catalog.point_count||points.length)+' 个</div><div class="scroll-list">'+(rows||'<div class="empty-note">尚无已转换 CSV/XLSX/GeoJSON 参考航线；ET 需先转换。</div>')+'</div>';
}

function comparisonPanel(flow,selected){
  const references=flow.reference_routes?.items||[],operational=(flow.operational_routes||[]).filter(item=>item.status==='passed');
  const reference=(selected?.kind==='route'&&references.find(item=>item.reference_route_id===selected.id))||references[0];
  const planned=operational[0];
  return '<div class="route-comparison"><b>真实参考航线 vs 系统规划运行航线</b><br>参考航线：'+(reference?escapeHtml(reference.name)+' · '+metric(reference.length_m,'m'):'不存在')+'<br>运行航线：'+(planned?escapeHtml(planned.route_id)+' · '+metric(planned.distance_m??pathLengthM(planned.path),'m'):'不存在')+'<br>两者是否都存在：'+(reference&&planned?'是':'否')+'（仅并列展示，不作优劣评分）</div>';
}

function referenceLandingPanel(flow){
  const catalog=flow.reference_landing_sites||{},items=filterReferenceSites(catalog.items||[],{workspace:flow.workspace});
  const regions=[...new Set(items.map(item=>item.region).filter(Boolean))].sort(),types=[...new Set(items.map(item=>item.site_type).filter(Boolean))].sort();
  const added=new Set((flow.nodes||[]).map(item=>item.reference_site_id).filter(Boolean));
  const rows=items.map(item=>'<div class="list-row reference-site-row" data-reference-site data-search="'+escapeHtml([item.name,item.location,item.reference_site_id].join(' ').toLocaleLowerCase())+'" data-region="'+escapeHtml(item.region||'')+'" data-site-type="'+escapeHtml(item.site_type||'')+'"><span><b>'+escapeHtml(item.name)+'</b><small>'+escapeHtml(item.region||'未标地区')+' · '+escapeHtml(item.site_type||'类型未标')+' · '+escapeHtml(item.quality)+' · '+item.coordinate.map(value=>Number(value).toFixed(5)).join(', ')+'</small><small>'+escapeHtml(item.reference_site_id)+(item.possible_duplicate?' · 疑似重复':'')+'</small></span><button class="secondary" data-add-reference-site="'+escapeHtml(item.reference_site_id)+'" '+(added.has(item.reference_site_id)?'disabled':'')+'>'+(added.has(item.reference_site_id)?'已加入':'加入项目')+'</button></div>').join('');
  return '<h3>参考起降点 '+statusBadge(catalog.status||'not_calculated')+'</h3><div class="parameter-note">reference_landing_sites 与 flow.nodes 严格分离。源文件未声明 CRS，全部保持 pending_confirmation；地图位置仅按源数值 [lon,lat] 临时展示，点击“加入项目”后才创建 node。</div><label>搜索<input class="panel-input" id="referenceSiteSearch" placeholder="名称、位置或稳定 ID"></label><div class="form-grid"><label>区域<select id="referenceSiteRegion"><option value="">全部区域</option>'+regions.map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('')+'</select></label><label>类型<select id="referenceSiteType"><option value="">全部类型</option>'+types.map(value=>'<option value="'+escapeHtml(value)+'">'+escapeHtml(value)+'</option>').join('')+'</select></label></div><div class="flow-summary" id="referenceSiteCount">工作区内 '+items.length+' / 全部 '+(catalog.count||0)+' 条；疑似重复只标记、不合并。</div><div class="scroll-list reference-site-list">'+(rows||'<div class="empty-note">当前工作区没有可展示的参考起降点</div>')+'</div>';
}

export function riskAwareRoutePanel(flow){
  const selection=flow.algorithm_selection?.route_planner||{};
  if(selection.algorithm_id!=='risk_aware_route_planner_v2'||selection.version!=='2.0')return '';
  const p=selection.parameters||{},lambda=p.risk_weight_lambda??0,component=p.risk_component||'overall',policy=p.unknown_risk_policy||'block';
  return '<h3>Risk-Aware Route Planner V2</h3><div class="parameter-note">直接使用 MH/T grid_id 与 RiskModelV1 相对工程指数；不是事故概率、SORA GRC 或 TLS。P13 仅规划二维战略水平航路，高度剖面仍由 P7 独立配置。</div><label>Risk weight λ<input class="panel-input" type="number" min="0" step="any" id="routeRiskLambda" value="'+escapeHtml(lambda)+'"></label><label>Risk component<select id="routeRiskComponent"><option value="overall" '+(component==='overall'?'selected':'')+'>Overall</option><option value="ground" '+(component==='ground'?'selected':'')+'>Ground</option><option value="air" '+(component==='air'?'selected':'')+'>Air</option></select></label><label>Unknown risk policy<select id="routeUnknownPolicy"><option value="block" '+(policy==='block'?'selected':'')+'>Block（默认）</option><option value="penalize" '+(policy==='penalize'?'selected':'')+'>Penalize</option></select></label><label>Unknown penalty index（penalize 时必须显式填写）<input class="panel-input" type="number" min="0" max="1" step="any" id="routeUnknownPenalty" value="'+escapeHtml(p.unknown_penalty_index??'')+'"></label><label>Max relative risk index（可空；仅工程阈值）<input class="panel-input" type="number" min="0" max="1" step="any" id="routeMaxRisk" value="'+escapeHtml(p.max_relative_risk_index??'')+'"></label><button class="secondary full" id="saveRiskRouteParameters">保存 V2 参数</button>';
}

function buildingClearancePanel(flow){
  const policy=flow.building_clearance_policy||{},result=flow.building_clearance_assessment||{},stats=result.statistics||{};
  const critical=(result.critical_buildings||[]).slice(0,12).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.building_id||'unknown')+'</b><small>水平 '+metric(item.horizontal_minimum_m,'m')+' · 地面/屋顶 '+metric(item.ground_elevation_m,'m')+' / '+metric(item.roof_elevation_m,'m')+' · '+escapeHtml(item.vertical_status||'unknown')+'</small></span></div>').join('');
  const unresolved=(result.routes||[]).map(item=>item.unresolved_building_count||0).reduce((a,b)=>a+b,0);
  return '<h3>三维建筑净空 '+statusBadge(result.status||'not_calculated')+'</h3><div class="parameter-note">FABDEM DTM footprint median + GBA height_m 构成 LoD1 棱柱；GLO-30 DSM 不参与屋顶高程。建筑环境风险与本评估彼此独立。</div><div class="form-grid"><label>水平净空 (m)<input class="panel-input" type="number" min="0" step="any" id="buildingHorizontalClearance" value="'+escapeHtml(policy.horizontal_clearance_m??'')+'"></label><label>垂直净空 (m)<input class="panel-input" type="number" min="0" step="any" id="buildingVerticalClearance" value="'+escapeHtml(policy.vertical_clearance_m??'')+'"></label><label>最小建筑高度 (m，可空)<input class="panel-input" type="number" min="0" step="any" id="buildingMinHeight" value="'+escapeHtml(policy.min_building_height_m??'')+'"></label><label>地形起伏复核阈值 (m，可空)<input class="panel-input" type="number" min="0" step="any" id="buildingReliefReview" value="'+escapeHtml(policy.terrain_relief_review_m??'')+'"></label></div><label>工程参数来源<input class="panel-input" id="buildingClearanceSource" value="'+escapeHtml(policy.source||'')+'"></label><label class="check-row"><input type="checkbox" id="buildingClearanceConfirmed" '+(policy.confirmed?'checked':'')+'>参数已由工程依据确认</label><div class="button-row"><button class="secondary" id="saveBuildingClearancePolicy">保存参数</button><button class="primary" id="evaluateBuildingClearance">执行净空分析</button></div><div class="flow-summary">breach '+(stats.breach_count||0)+' · safe routes '+(stats.safe_route_count||0)+' · unknown routes '+(stats.unknown_route_count||0)+' · unresolved buildings '+unresolved+'<br>unknown/unresolved 永远不视为 safe；本结果不构成认证或法规符合性结论。</div><h3>Closest / critical buildings</h3><div class="scroll-list">'+(critical||'<div class="empty-note">尚无评估证据</div>')+'</div>';
}

export function render({flow,interactionMode,selectedReference=null}){
  const nodes=(flow.nodes||[]).map(node=>'<div class="list-row"><span><b>'+node.node_id+'</b> '+escapeHtml(node.name)+'<small>'+node.coordinate.map(value=>value.toFixed(5)).join(', ')+(node.reference_site_id?' · 来源 '+escapeHtml(node.reference_site_id):' · 手工点')+'</small></span><button data-delete-node="'+node.node_id+'">×</button></div>').join('');
  const routes=(flow.scenario_routes||[]).map(route=>{const result=(flow.operational_routes||[]).find(item=>item.route_id===route.route_id),v2=result?.algorithm_id==='risk_aware_route_planner_v2',details=v2?'<small>Distance '+metric(result.distance_m,'m')+' · Risk exposure '+metric(result.risk_exposure_index_m,'index·m')+' · Mean '+metric(result.mean_risk_index)+' · Max '+metric(result.max_risk_index)+' · Detour '+metric(result.detour_factor)+'</small>':'';return '<div class="list-row route-row"><span><b>'+route.route_id+'</b> '+route.direction+' '+statusBadge(result?.status||'not_calculated')+details+'</span><button data-delete-route="'+route.route_id+'">×</button></div>';}).join('');
  const routeOptions=(flow.operational_routes||[]).map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  const profiles=Object.values(flow.spatial_3d?.route_altitude_profiles||{}).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.route_id)+'</b><small>'+escapeHtml(item.mode)+' · '+item.constant_altitude_m+' m '+escapeHtml(item.vertical_reference)+'</small></span></div>').join('');
  const altitude='<h3>Route 3D Altitude Profile</h3><div class="panel-file-input"><select id="altitudeRoute">'+routeOptions+'</select><select id="routeVerticalReference"><option value="agl">AGL</option><option value="egm2008_orthometric">EGM2008 orthometric</option><option value="wgs84_ellipsoidal">WGS84 ellipsoidal</option></select></div><label>Constant altitude (m)<input class="panel-input" type="number" id="routeAltitude" value="100"></label><button class="secondary full" id="saveRouteAltitude" '+(!routeOptions?'disabled':'')+'>保存航路高度剖面</button><div class="scroll-list">'+(profiles||'<div class="empty-note">尚未配置运行航路高度</div>')+'</div>';
  const motionProfiles=Object.values(flow.operational_timing?.route_motion_profiles||{}).map(item=>'<div class="list-row"><span><b>'+escapeHtml(item.route_id)+'</b><small>'+escapeHtml(item.mode)+' · '+(item.constant_ground_speed_mps??'待确认')+' m/s · '+escapeHtml(item.status)+'</small></span></div>').join('');
  const motion='<h3>Route Motion Profile</h3><div class="demo-note">P9 仅实现 confirmed constant ground speed；不会借用 Aircraft cruise speed。</div><label>运行航路<select id="motionRoute">'+routeOptions+'</select></label><label>Constant ground speed (m/s)<input class="panel-input" type="number" min="0" step="any" id="routeGroundSpeed" placeholder="必须显式输入"></label><button class="secondary full" id="saveRouteMotion" '+(!routeOptions?'disabled':'')+'>保存航路运动剖面</button><div class="scroll-list">'+(motionProfiles||'<div class="empty-note">尚未配置航路运动剖面</div>')+'</div>';
  const body=referenceRoutesPanel(flow,selectedReference)+referenceLandingPanel(flow)+'<h3>项目起降点</h3><button class="'+(interactionMode==='node'?'primary':'secondary')+' full" id="addNodeMode">地图点击增加起降点</button><div class="scroll-list">'+(nodes||'<div class="empty-note">至少添加两个点</div>')+'</div>'+odScenarioPanel(flow)+'<h3>旧：生成方向</h3><label>生成方向</label><select id="routeDirection"><option value="both">双向（独立生成两个 route_id）</option><option value="ab">A→B</option><option value="ba">B→A</option></select>'+plannerCard(plannerCardModel(flow))+riskAwareRoutePanel(flow)+'<div class="button-row"><button class="secondary" id="scenarioRoutes">生成场景航路（all-pairs，兼容）</button><button class="primary" id="operationalRoutes">生成运行航路</button></div><div class="scroll-list route-list">'+(routes||'<div class="empty-note">尚无航路</div>')+'</div>'+comparisonPanelV2(flow,routePlannerComparisonModel(flow))+comparisonPanel(flow,selectedReference)+altitude+renderRouteVerticalProfilePanel(flow.route_vertical_profiles,flow.operational_routes)+motion+buildingClearancePanel(flow)+'<div class="flow-summary">已退役编号：'+((flow.retired_route_ids||[]).join(', ')||'无')+'<br>环境风险：'+statusText(flow.risks?.environment?.status||'not_calculated')+'</div><button class="primary full" id="nextStep" '+(!flow.steps?.['3']?'disabled':'')+'>下一步：运行规则</button>';
  return shell('03','航路设计','地图点击增加起降点；场景与运行航路分别保存。',body);
}
export function bind(c){
  bindRouteVerticalProfile(c);
  c.$('addNodeMode').onclick=c.toggleNodeMode;c.actionButton('scenarioRoutes',()=>c.mutate('scenario',{direction:c.$('routeDirection').value}));c.actionButton('operationalRoutes',()=>c.mutate('operational'));
  if(c.$('createOdRoute'))c.actionButton('createOdRoute',()=>{const start=c.$('odStartNode').value,end=c.$('odEndNode').value;if(start===end)throw new Error('起点与终点不能相同');return c.mutate('scenario-od',{start_node_id:start,end_node_id:end,direction:c.$('odDirection').value});});
  const applyReferenceFilter=()=>{const search=c.$('referenceSiteSearch').value.trim().toLocaleLowerCase(),region=c.$('referenceSiteRegion').value,siteType=c.$('referenceSiteType').value;let visible=0;document.querySelectorAll('[data-reference-site]').forEach(row=>{const show=(!search||row.dataset.search.includes(search))&&(!region||row.dataset.region===region)&&(!siteType||row.dataset.siteType===siteType);row.hidden=!show;if(show)visible++;});const count=c.$('referenceSiteCount');if(count)count.textContent='当前筛选 '+visible+' 条；疑似重复只标记、不合并。';c.paint();};
  c.$('referenceSiteSearch').oninput=applyReferenceFilter;c.$('referenceSiteRegion').onchange=applyReferenceFilter;c.$('referenceSiteType').onchange=applyReferenceFilter;
  document.querySelectorAll('[data-add-reference-site]').forEach(button=>button.onclick=async()=>{try{button.disabled=true;await c.resourceAction('/api/reference-landing-sites/add-to-project',{reference_site_id:button.dataset.addReferenceSite});}catch(error){c.panelError(error.message);button.disabled=false;}});
  document.querySelectorAll('[data-select-reference-route]').forEach(button=>button.onclick=()=>c.selectReference({kind:'route',id:button.dataset.selectReferenceRoute}));
  if(c.$('saveRiskRouteParameters'))c.actionButton('saveRiskRouteParameters',()=>{const current=c.flow().algorithm_selection.route_planner,numberOrNull=id=>{const value=c.$(id).value.trim();return value===''?null:Number(value);};return c.resourceAction('/api/algorithms/select',{algorithm_type:'route_planner',algorithm_id:current.algorithm_id,version:current.version,parameters:{risk_weight_lambda:Number(c.$('routeRiskLambda').value),risk_component:c.$('routeRiskComponent').value,unknown_risk_policy:c.$('routeUnknownPolicy').value,unknown_penalty_index:numberOrNull('routeUnknownPenalty'),max_relative_risk_index:numberOrNull('routeMaxRisk')}});});
  document.querySelectorAll('[data-delete-node]').forEach(button=>button.onclick=()=>c.mutate('node-delete',{node_id:button.dataset.deleteNode}).catch(error=>c.panelError(error.message)));
  document.querySelectorAll('[data-delete-route]').forEach(button=>button.onclick=()=>c.mutate('route-delete',{route_id:button.dataset.deleteRoute}).catch(error=>c.panelError(error.message)));
  c.actionButton('saveRouteAltitude',()=>c.resourceAction('/api/spatial-3d/route-profile',{route_id:c.$('altitudeRoute').value,mode:'constant',vertical_reference:c.$('routeVerticalReference').value,constant_altitude_m:Number(c.$('routeAltitude').value),source:'user_configuration',confirmed:true}));
  c.actionButton('saveRouteMotion',()=>{const timing=structuredClone(c.flow().operational_timing||{route_motion_profiles:{},service_scenarios:{},response_time_budgets:{},encounter_scenarios:{}}),routeId=c.$('motionRoute').value;timing.route_motion_profiles=timing.route_motion_profiles||{};timing.route_motion_profiles[routeId]={route_id:routeId,mode:'constant_ground_speed_mps',constant_ground_speed_mps:Number(c.$('routeGroundSpeed').value),source:'user_configuration',confirmed:true};return c.resourceAction('/api/operational-timing',{operational_timing:timing});});
  const optional=id=>{const value=c.$(id).value.trim();return value===''?null:Number(value);};
  c.actionButton('saveBuildingClearancePolicy',()=>c.resourceAction('/api/building-clearance/policy',{horizontal_clearance_m:optional('buildingHorizontalClearance'),vertical_clearance_m:optional('buildingVerticalClearance'),min_building_height_m:optional('buildingMinHeight'),terrain_relief_review_m:optional('buildingReliefReview'),source:c.$('buildingClearanceSource').value.trim(),confirmed:c.$('buildingClearanceConfirmed').checked}));
  c.actionButton('evaluateBuildingClearance',()=>c.resourceAction('/api/building-clearance/evaluate',{}));
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(4);
}
