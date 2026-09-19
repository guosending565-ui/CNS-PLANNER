// =========================================================
// RouteRiskProfile V1 工作台面板（只读分析视图 + 显式阈值工程设置）
//
// 硬边界（全部只做"转印"，前端不重算任何风险、距离或分类）：
//  * 只分析 current LayeredRouteCandidate：不 replan、不修改 operational_routes / CNS；
//  * 不计算 absolute risk / SORA GRC / ARC，也不生成 cross-domain overall 或 cross-domain
//    high-risk —— 三个 domain（ground / air_traffic / environment_obstacle）各自独立成卡；
//  * 阈值没有默认值：未配置/未确认时只显示 exposure / mean / max 与 raw index，
//    classification 与 high-risk 指标保持 not_configured / null，前端绝不代为分级；
//  * 一维 SVG 的横轴与每个区段位置全部来自后端已有字段（route_length_m /
//    start_cumulative_distance_m / end_cumulative_distance_m），前端不做任何几何或距离推算；
//  * factor contributor 只是 relative engineering contribution，不是事故原因概率；
//  * stale profile 保留为审计证据并明确标 stale，删除只走 /api/route-risk-profiles/delete；
//  * 工程证据（factor / consistency / fingerprints / 历史 profile）默认折叠。
// =========================================================
import {escapeHtml,statusBadge,wbBlock,wbDisclosure} from './common.js';

export const DOMAIN_IDS=['ground','air_traffic','environment_obstacle'];
export const DOMAIN_LABELS={
  ground:'Ground（地面暴露）',
  air_traffic:'Air / Traffic（空中交通暴露）',
  environment_obstacle:'Environment / Obstacle（工程环境-障碍物）',
};
//: 只分析 LayeredRouteCandidate；artifact 不是 operational route。
export const RRP_ARTIFACT_LABEL='RouteRiskProfile 只分析 LayeredRouteCandidate';
export const RRP_CANDIDATE_LABEL='分层候选（candidate，非运行航路）';
export const RRP_SCOPE_NOTE='不 replan、不修改 operational_routes、不计算 absolute risk、'
  +'不计算 SORA GRC/ARC，也不生成 cross-domain overall / cross-domain high-risk。';
export const RRP_RAW_INDEX_LABEL='raw domain index（未确认阈值，不做 low/medium/high 判定）';
export const RRP_CONTRIBUTOR_LABEL='relative engineering contribution（相对工程贡献指数，不是事故原因概率）';
export const RRP_CONTRIBUTOR_NOTE='factor contributor 只是 relative engineering contribution，'
  +'不是事故原因概率，也不代表因果关系。';
//: 三 domain 各自独立的阈值表单控件前缀。
const POLICY_FIELD_PREFIX='rrpThreshold';

const finite=value=>Number.isFinite(value);
const fmt=(value,digits=2)=>finite(value)?Number(value).toFixed(digits):'—';
const text=value=>String(value??'');
const short=value=>{const text_=text(value);return text_||'—';};

function domainLabel(domainId){
  return DOMAIN_LABELS[domainId]||domainId;
}

/** 一维 SVG 的配色：只有已确认阈值时才使用 low/medium/high 语义。 */
const LEVEL_FILL={low:'#2f8f4e',medium:'#c98a1b',high:'#c0392b'};
const LEVEL_BAND={low:'rgba(47,143,78,.10)',medium:'rgba(201,138,27,.14)',high:'rgba(192,57,43,.14)'};
const NEUTRAL_FILL='#8a97a3';

// ---------------------------------------------------------------- model

/**
 * 只读投影：readiness / policy / profiles 全部来自后端 snapshot，前端不重新判断。
 * @param {object} flow published workflow snapshot
 */
export function routeRiskProfileModel(flow){
  const snapshot=flow||{};
  const readiness=snapshot.route_risk_profile_readiness||{};
  const policy=snapshot.route_risk_profile_policy||{};
  const collection=snapshot.route_risk_profiles||{};
  const items=Array.isArray(collection.items)?collection.items:[];
  const readinessDomains=readiness.domains||{};
  const policyDomains=policy.domains||{};

  const domains=DOMAIN_IDS.map(domainId=>{
    const ready=readinessDomains[domainId]||{};
    const policyDomain=policyDomains[domainId]||{};
    const status=text(policyDomain.status||ready.thresholds_status||'not_configured');
    return {
      domainId,
      label:domainLabel(domainId),
      // 阈值状态与按钮可用性直接取自后端 readiness / policy，不做任何前端推断。
      thresholdsStatus:status,
      thresholdsConfigured:ready.classification_available===true||status==='confirmed',
      highRiskMetrics:text(ready.high_risk_metrics||'not_configured'),
      mediumMin:finite(policyDomain.medium_min)?Number(policyDomain.medium_min):null,
      highMin:finite(policyDomain.high_min)?Number(policyDomain.high_min):null,
      source:text(policyDomain.source),
      confirmed:policyDomain.confirmed===true,
      parameterStatus:text(policyDomain.parameter_status),
      policyStatusReason:text(policyDomain.status_reason),
    };
  });

  const current=items.find(item=>item&&item.current_applicability==='current')
    ||items[items.length-1]||null;

  return {
    readinessStatus:text(readiness.status||'blocked'),
    algorithm:readiness.algorithm||{},
    artifactType:text(readiness.artifact_type),
    candidate:readiness.candidate||null,
    currentIdentity:readiness.current_identity||null,
    gridRisk:readiness.grid_risk_v2||{status:'not_calculated',overall_used:false},
    policyStatus:text(readiness.profile_policy?.status||policy.status||'not_configured'),
    policyParameterStatus:text(readiness.profile_policy?.parameter_status||policy.parameter_status||''),
    policyFingerprint:text(readiness.profile_policy?.fingerprint||''),
    policyNotes:policy.notes||[],
    blockers:Array.isArray(readiness.blockers)?readiness.blockers:[],
    notComputed:readiness.not_computed||{},
    semantics:readiness.semantics||{},
    profileCount:Array.isArray(items)?items.length:0,
    collectionStatus:text(collection.status||'not_calculated'),
    lastEvaluation:collection.last_evaluation||null,
    domains,
    current,
    profiles:items.map(profile=>({
      profileId:text(profile?.profile_id),
      status:text(profile?.status||'not_calculated'),
      currentApplicability:text(profile?.current_applicability),
      staleReason:text(profile?.stale_reason),
      statusReason:text(profile?.status_reason),
      createdAt:text(profile?.created_at),
    })),
    scopeNote:RRP_SCOPE_NOTE,
    automaticClassification:false,
    crossDomainOverall:false,
  };
}

/** 当前 profile 的某个 domain 卡片模型：只转印后端字段，不补任何值。 */
export function routeRiskDomainModel(profile,domainId){
  const domain=(profile?.domains||{})[domainId];
  if(!domain)return null;
  const classification=domain.classification||{};
  const highRisk=domain.high_risk||{};
  const thresholds=classification.thresholds||{};
  // 只有 status=passed 才代表后端真的做过 low/medium/high 判定。
  const classified=classification.status==='passed';
  const intervals=classified&&Array.isArray(highRisk.intervals)?highRisk.intervals:[];
  return {
    domainId,
    label:domainLabel(domainId),
    status:text(domain.status),
    activeCostDomain:domain.active_cost_domain===true,
    exposureIndexM:finite(domain.exposure_index_m)?Number(domain.exposure_index_m):null,
    meanIndex:finite(domain.mean_index)?Number(domain.mean_index):null,
    maxIndex:finite(domain.max_index)?Number(domain.max_index):null,
    maxLocation:domain.max_location||null,
    resolvedLengthM:finite(domain.resolved_length_m)?Number(domain.resolved_length_m):null,
    unresolvedLengthM:finite(domain.unresolved_length_m)?Number(domain.unresolved_length_m):null,
    coverage:finite(domain.coverage)?Number(domain.coverage):null,
    resolvedSegmentCount:finite(domain.resolved_segment_count)?Number(domain.resolved_segment_count):null,
    unresolvedSegmentCount:finite(domain.unresolved_segment_count)?Number(domain.unresolved_segment_count):null,
    classificationStatus:text(classification.status||'not_configured'),
    classificationLevel:classification.level||null,
    classificationReason:text(classification.reason),
    classified,
    // 未确认阈值时这两项必须保持 null，绝不伪造。
    mediumMin:finite(thresholds.medium_min)?Number(thresholds.medium_min):null,
    highMin:finite(thresholds.high_min)?Number(thresholds.high_min):null,
    highRiskStatus:text(highRisk.status||'not_configured'),
    highRiskReason:text(highRisk.reason),
    highRiskLengthM:finite(highRisk.length_m)?Number(highRisk.length_m):null,
    highRiskIntervalCount:finite(highRisk.interval_count)?Number(highRisk.interval_count):null,
    intervals,
    unresolvedCells:domain.unresolved_cells||[],
    contributors:domain.contributors||{},
  };
}

// ---------------------------------------------------------------- SVG geometry

/**
 * 一维路径画像几何：x 坐标**只**来自后端已有累计距离字段与 route_length_m。
 *
 * 这里没有任何距离或风险的重新计算：`start_cumulative_distance_m` /
 * `end_cumulative_distance_m` / `route_length_m` 都是后端 profile 的原值，
 * 本函数只做"米 → 像素"的线性映射。
 *
 * @param {{domains?:object,route_length_m?:number,segments?:Array}} profile
 * @param {string} domainId
 */
export function routeRiskDomainGeometry(profile,domainId){
  const routeLength=finite(profile?.route_length_m)?Number(profile.route_length_m):null;
  const segments=Array.isArray(profile?.segments)?profile.segments:[];
  const domain=(profile?.domains||{})[domainId]||null;
  const thresholds=domain?.classification?.thresholds||{};
  const medium=finite(thresholds.medium_min)?Number(thresholds.medium_min):null;
  const high=finite(thresholds.high_min)?Number(thresholds.high_min):null;
  // 与 DOMAIN 卡片完全一致：只有 classification.status=passed 才是"已确认阈值"。
  const classified=(domain?.classification?.status)==='passed';
  const positions=segments.map(segment=>({
    segmentId:text(segment?.segment_id),
    index:finite(segment?.index)?Number(segment.index):null,
    startDistanceM:finite(segment?.start_cumulative_distance_m)?Number(segment.start_cumulative_distance_m):null,
    endDistanceM:finite(segment?.end_cumulative_distance_m)?Number(segment.end_cumulative_distance_m):null,
    lengthM:finite(segment?.length_m)?Number(segment.length_m):null,
    classificationLevel:(segment?.domains||{})[domainId]?.classification?.level||null,
    classificationStatus:text((segment?.domains||{})[domainId]?.classification?.status||'not_configured'),
    meanIndex:finite((segment?.domains||{})[domainId]?.mean_index)?Number((segment?.domains||{})[domainId].mean_index):null,
    resolved:(segment?.domains||{})[domainId]?.resolved===true,
    sourceIds:[text(segment?.start_index_grid_id),text(segment?.end_index_grid_id)].filter(Boolean),
  }));
  return {
    domainId,
    domainLabel:domainLabel(domainId),
    domainStatus:text(domain?.status),
    domainLevel:domain?.classification?.level||null,
    domainClassificationStatus:text(domain?.classification?.status||'not_configured'),
    routeLengthM:routeLength,
    classified,
    mediumMin:classified?medium:null,
    highMin:classified?high:null,
    rawThresholds:{mediumMin:medium,highMin:high},
    positions,
  };
}

/**
 * 原生 SVG 一维路径画像（无第三方依赖）。
 * @param {{domains?:object,route_length_m?:number,segments?:Array}} profile
 * @param {string} domainId
 */
export function routeRiskDomainSvg(profile,domainId){
  const geometry=routeRiskDomainGeometry(profile,domainId);
  const length=geometry.routeLengthM;
  if(!finite(length)||length<=0)return '';
  const width=760,height=132,padLeft=48,padRight=16,padTop=30,padBottom=30;
  const usable=width-padLeft-padRight;
  // 横轴 0 → route_length_m：唯一来源是后端 route_length_m。
  const x=value=>padLeft+Math.max(0,Math.min(length,finite(value)?Number(value):0))/length*usable;
  const barTop=padTop,barHeight=34,barBottom=barTop+barHeight;

  const classified=geometry.classified;
  const caption=classified
    ?'后端已确认阈值：level 来自 profile segment classification（low / medium / high）'
    :'后端未配置/未确认阈值：'+RRP_RAW_INDEX_LABEL;
  const ticks=[0,length/2,length].map(value=>
    '<text x="'+x(value).toFixed(1)+'" y="'+(height-8)+'" font-size="11" text-anchor="middle" fill="#566">'
    +escapeHtml(fmt(value,1))+' m</text>').join('');
  const gridLines=[0,length/2,length].map(value=>
    '<line x1="'+x(value).toFixed(1)+'" x2="'+x(value).toFixed(1)+'" y1="'+(barTop-6)+'" y2="'+(barBottom+6)
    +'" stroke="#dde4e1" stroke-width="1"></line>').join('');

  const bars=geometry.positions.map(position=>{
    const startX=x(position.startDistanceM),endX=x(position.endDistanceM);
    // 区段位置严格取自后端累计距离；宽度不足 1px 时只做最小可视化，不改变数值。
    const barWidth=Math.max(1,endX-startX);
    const level=classified?position.classificationLevel:null;
    const fill=LEVEL_FILL[level]||NEUTRAL_FILL;
    const label=classified
      ?text(level||'未分类')
      :'raw index '+(position.meanIndex===null?'未解析':fmt(position.meanIndex,4));
    const title=escapeHtml('segment '+short(position.segmentId)
      +' · distance '+fmt(position.startDistanceM,1)+' → '+fmt(position.endDistanceM,1)+' m'
      +' · length '+fmt(position.lengthM,1)+' m · '+label);
    return '<rect data-rrp-segment="'+escapeHtml(text(position.segmentId))+'"'
      +' data-rrp-level="'+escapeHtml(classified?text(level||'unclassified'):'not_configured')+'"'
      +' x="'+startX.toFixed(1)+'" y="'+barTop+'" width="'+barWidth.toFixed(1)+'" height="'+barHeight+'"'
      +' fill="'+fill+'" opacity="'+(classified?'.85':'.35')+'"><title>'+title+'</title></rect>';
  }).join('');

  const markers=gridLines;

  // 阈值区段带：只有后端已确认阈值时才画出 low / medium / high 的索引区段。
  // 区段边界严格由后端的 medium_min / high_min 与 route_length_m 决定，前端不做任何分级。
  const bands=classified
    ?[['low',0,geometry.mediumMin],['medium',geometry.mediumMin,geometry.highMin],
      ['high',geometry.highMin,1]]
      .filter(item=>finite(item[1])&&finite(item[2])&&item[2]>item[1])
      .map(item=>{
        const startX=x(item[1]*length),bandWidth=Math.max(1,x(item[2]*length)-startX);
        return '<rect data-rrp-band="'+item[0]+'" x="'+startX.toFixed(1)+'" y="'+(barTop-6)
          +'" width="'+bandWidth.toFixed(1)+'" height="'+(barHeight+12)+'" fill="'+LEVEL_BAND[item[0]]
          +'"><title>'+escapeHtml(item[0]+' 区段 · index '+fmt(item[1],4)+' – '+fmt(item[2],4)
            +'（后端已确认阈值）')+'</title></rect>';
      }).join('')
    :'';
  const thresholdMarkers=classified
    ?[['medium_min',geometry.mediumMin],['high_min',geometry.highMin]]
      .filter(item=>finite(item[1]))
      .map(item=>{
        const value=x(item[1]*length);
        return '<line data-rrp-threshold="'+item[0]+'" x1="'+value.toFixed(1)+'" x2="'+value.toFixed(1)
          +'" y1="'+(barTop-10)+'" y2="'+(barBottom+10)+'" stroke="#33404c" stroke-dasharray="4 3"></line>'
          +'<text x="'+value.toFixed(1)+'" y="'+(barBottom+22)+'" font-size="10" text-anchor="middle" fill="#33404c">'
          +escapeHtml(item[0]+' '+fmt(item[1],4))+'</text>';
      }).join('')
    :'';

  const rawNote='<text x="'+padLeft+'" y="'+(barTop-10)+'" font-size="11" fill="#566">'
    +'route_length_m '+escapeHtml(fmt(length,1))+' m（后端）</text>'
    +'<text x="'+(width-padRight)+'" y="'+(barTop-10)+'" font-size="11" text-anchor="end" fill="#566">'
    +escapeHtml(classified?('domain level '+short(geometry.domainLevel)):'未确认阈值')+'</text>';

  return '<svg data-rrp-svg="'+escapeHtml(domainId)+'" data-rrp-x-axis="cumulative_distance_m"'
    +' viewBox="0 0 '+width+' '+height+'" role="img"'
    +' aria-label="'+escapeHtml(domainLabel(domainId)+' 一维路径画像')+'"'
    +' style="width:100%;min-height:132px;background:#f7faf9;border:1px solid #ccd8d4">'
    +bands+rawNote+thresholdMarkers+markers+bars+ticks
    +'<text x="'+padLeft+'" y="'+(height-28)+'" font-size="10" fill="#667">'+escapeHtml(caption)+'</text>'
    +'</svg>';
}

// ---------------------------------------------------------------- HTML helpers

function listRow(title,note){
  return '<div class="list-row"><span><b>'+escapeHtml(title)+'</b>'+(note?'<small>'+note+'</small>':'')+'</span></div>';
}

function blockerRows(model){
  if(!model.blockers.length){
    return '<div class="parameter-note">后端 readiness 没有 blocking 项：readiness = <code>'
      +escapeHtml(model.readinessStatus)+'</code>。</div>';
  }
  return '<div class="scroll-list">'+model.blockers.map(item=>
    '<div class="list-row"><span><b>'+escapeHtml(short(item.reason_code))
    +'</b> '+statusBadge('blocked')
    +'<small>'+escapeHtml(text(item.reason))+'</small></span></div>').join('')+'</div>';
}

function readinessRows(model){
  const candidate=model.candidate||{};
  const grid=model.gridRisk||{};
  return [
    ['readiness status',statusBadge(model.readinessStatus)+' · '+escapeHtml(model.readinessStatus)],
    ['artifact_type',escapeHtml(short(model.artifactType||'layered_route_candidate'))
      +' · '+escapeHtml(RRP_ARTIFACT_LABEL)+'（不是 operational route）'],
    ['current candidate',escapeHtml(short(candidate.candidate_id||'—'))
      +' · status '+escapeHtml(short(candidate.status||'—'))
      +' · applicability '+escapeHtml(short(candidate.current_applicability||'—'))],
    ['candidate lane / route / layer',escapeHtml(short(candidate.lane_key||'—'))+' · '
      +escapeHtml(short(candidate.route_id||'—'))+' · '+escapeHtml(short(candidate.altitude_layer_id||'—'))],
    ['candidate fingerprint',escapeHtml(text(candidate.candidate_fingerprint).slice(0,28)||'—')],
    ['grid_risk_v2',statusBadge(grid.status||'not_calculated')+' · cells '
      +escapeHtml(String(grid.cell_count??'—'))+' · overall_used '+escapeHtml(String(grid.overall_used===true))],
    ['profile 状态',statusBadge(model.collectionStatus)+' · 已保存 profile '
      +escapeHtml(String(model.profileCount))+' 条'],
    ['阈值配置状态',statusBadge(model.policyStatus)+' · '+escapeHtml(short(model.policyParameterStatus))],
    ['policy fingerprint',escapeHtml(text(model.policyFingerprint).slice(0,28)||'—')],
  ].map(row=>listRow(row[0],row[1])).join('');
}

function evalGateRow(model){
  const evaluation=model.lastEvaluation;
  if(!evaluation)return '';
  const reasons=(evaluation.blocking_reasons||[]).map(item=>text(item.reason||item.reason_code)).filter(Boolean);
  return listRow('最后一次 evaluate',statusBadge(evaluation.status||'not_calculated')
    +' · reason_code <code>'+escapeHtml(short(evaluation.reason_code))
    +'</code>'+(evaluation.reason?' · '+escapeHtml(text(evaluation.reason)):'')
    +(reasons.length?'<br>blocking_reasons：'+escapeHtml(reasons.join('；')):''));
}

function domainCard(model,profile,domainId){
  const domain=routeRiskDomainModel(profile,domainId);
  const header='<h3>'+escapeHtml(domainLabel(domainId))+'</h3>';
  if(!domain){
    return header+'<div class="empty-note">当前 profile 没有该 domain 的字段：不显示推断值。</div>';
  }
  const thresholds=model.domains.find(item=>item.domainId===domainId)||{};  const levelText=domain.classified
    ?statusBadge(domain.classificationLevel||'passed')+' · level '+escapeHtml(short(domain.classificationLevel))
    :statusBadge(domain.classificationStatus)+' · level —（未确认阈值，不伪造等级）';
  const rows=[
    ['exposure_index_m',fmt(domain.exposureIndexM,3)+' index·m'],
    ['mean_index',fmt(domain.meanIndex,6)],
    ['max_index',fmt(domain.maxIndex,6)],
    ['max_location',domain.maxLocation
      ?escapeHtml('grid '+short(domain.maxLocation.grid_id)+' · '+short(domain.maxLocation.role)
        +' · cumulative '+fmt(domain.maxLocation.cumulative_distance_m,1)+' m')
      :'—'],
    ['resolved_length_m',fmt(domain.resolvedLengthM,3)+' m'],
    ['unresolved_length_m',fmt(domain.unresolvedLengthM,3)+' m'],
    ['coverage',domain.coverage===null?'—':escapeHtml(String(domain.coverage))],
    ['domain status',statusBadge(domain.status)+' · resolved segments '
      +escapeHtml(String(domain.resolvedSegmentCount??'—'))+' · unresolved segments '
      +escapeHtml(String(domain.unresolvedSegmentCount??'—'))],
    ['active_cost_domain',escapeHtml(String(domain.activeCostDomain))],
    ['classification',levelText
      +(domain.classificationReason?' · '+escapeHtml(domain.classificationReason):'')],
    ['thresholds',domain.mediumMin===null&&domain.highMin===null
      ?'<b>未配置</b>（无默认阈值）'
      :'medium_min '+fmt(domain.mediumMin,4)+' · high_min '+fmt(domain.highMin,4)
        +' · source '+escapeHtml(short(thresholds.source))+' · confirmed '+escapeHtml(String(domain.classified))],
  ].map(row=>listRow(row[0],row[1])).join('');

  const svg=routeRiskDomainSvg(profile,domainId);
  const chart=svg||'<div class="empty-note">profile 缺少 route_length_m 或 segments：无法绘制一维路径画像（前端不做任何推算）。</div>';

  const highRiskRows=[
    ['high_risk status',statusBadge(domain.highRiskStatus)+' · reason '+escapeHtml(short(domain.highRiskReason))],
    ['high_risk length_m',domain.highRiskLengthM===null?'—（未确认阈值）':fmt(domain.highRiskLengthM,3)+' m'],
    ['high_risk interval_count',domain.highRiskIntervalCount===null?'—（未确认阈值）':escapeHtml(String(domain.highRiskIntervalCount))],
  ].map(row=>listRow(row[0],row[1])).join('');

  const intervalRows=domain.intervals.length
    ?domain.intervals.map(interval=>{
      const details=wbDisclosure('segment_ids / cell_ids（工程证据）',
        '<div class="parameter-note">segment_ids '+escapeHtml((interval.segment_ids||[]).join(', ')||'—')
        +'<br>cell_ids '+escapeHtml((interval.cell_ids||[]).join(', ')||'—')+'</div>');
      return '<div class="list-row route-row"><span><b>'+escapeHtml(short(interval.interval_id))+'</b>'
        +'<small>start_distance_m '+fmt(interval.start_distance_m,3)+' m'
        +' · end_distance_m '+fmt(interval.end_distance_m,3)+' m'
        +' · length_m '+fmt(interval.length_m,3)+' m</small>'
        +'<small>mean_index '+fmt(interval.mean_index,6)+' · max_index '+fmt(interval.max_index,6)+'</small>'
        +details+'</span></div>';
    }).join('')
    :'<div class="empty-note">'+(domain.classified
      ?'该 domain 没有连续 high 区段（interval_count=0）。'
      :'未配置/未确认阈值：不产生 high-risk interval 指标。')+'</div>';

  const contributorEntries=Object.values(domain.contributors||{});
  const contributorRows=contributorEntries.length
    ?contributorEntries.map(item=>'<div class="list-row route-row"><span><b>'
      +escapeHtml(short(item.factor_id))+'</b>'
      +'<small>normalized_exposure_index_m '+fmt(item.normalized_exposure_index_m,3)
      +' · weighted_contribution_index_m '+fmt(item.weighted_contribution_index_m,3)
      +' · weight '+(item.weight===null||item.weight===undefined?'—':escapeHtml(String(item.weight)))
      +' · rank '+escapeHtml(item.contributor_rank===null||item.contributor_rank===undefined?'—':String(item.contributor_rank))
      +' · contribution_status '+escapeHtml(short(item.contribution_status))+'</small>'
      +'<small>'+escapeHtml(RRP_CONTRIBUTOR_LABEL)+'</small></span></div>').join('')
    :'<div class="empty-note">该 domain 没有可用的 factor contributor（contribution_status=not_available 时不排贡献）。</div>';

  const evidence=wbDisclosure('Factor contributors（relative engineering contribution）',
    '<div class="parameter-note">'+escapeHtml(RRP_CONTRIBUTOR_NOTE)+'</div>'
    +'<div class="scroll-list route-list">'+contributorRows+'</div>');
  const unresolvedEvidence=domain.unresolvedCells.length
    ?wbDisclosure('unresolved cells（missing ≠ 0）',
      '<div class="parameter-note">'+escapeHtml(domain.unresolvedCells.join(', '))+'</div>')
    :'';

  return header
    +'<div class="scroll-list route-list" data-rrp-fields="'+escapeHtml(domainId)+'">'+rows+'</div>'
    +'<div data-rrp-chart="'+escapeHtml(domainId)+'">'+chart+'</div>'
    +'<h3>high-risk intervals</h3>'
    +'<div class="scroll-list route-list" data-rrp-high-risk="'+escapeHtml(domainId)+'">'+highRiskRows+'</div>'
    +'<div class="scroll-list route-list" data-rrp-intervals="'+escapeHtml(domainId)+'">'+intervalRows+'</div>'
    +evidence+unresolvedEvidence;
}

function policyPanel(model){
  const fields=model.domains.map(domain=>{
    const mediumId=POLICY_FIELD_PREFIX+'Medium_'+domain.domainId;
    const highId=POLICY_FIELD_PREFIX+'High_'+domain.domainId;
    const sourceId=POLICY_FIELD_PREFIX+'Source_'+domain.domainId;
    const confirmedId=POLICY_FIELD_PREFIX+'Confirmed_'+domain.domainId;
    return '<h3>'+escapeHtml(domain.label)+' '+statusBadge(domain.thresholdsStatus)+'</h3>'
      +'<div class="form-grid">'
      +'<label>medium_min（0–1，可空）<input class="panel-input" type="number" min="0" max="1" step="any" id="'+mediumId
      +'" placeholder="无默认值" value="'+(domain.mediumMin===null?'':escapeHtml(String(domain.mediumMin)))+'"></label>'
      +'<label>high_min（0–1，可空）<input class="panel-input" type="number" min="0" max="1" step="any" id="'+highId
      +'" placeholder="无默认值" value="'+(domain.highMin===null?'':escapeHtml(String(domain.highMin)))+'"></label>'
      +'</div>'
      +'<label>source（工程依据）<input class="panel-input" id="'+sourceId+'" value="'
      +escapeHtml(domain.source===''||domain.source.startsWith('未配置')?'':domain.source)+'"></label>'
      +'<label class="check-row"><input type="checkbox" id="'+confirmedId+'" '+(domain.confirmed?'checked':'')
      +'>该 domain 阈值已由项目工程依据确认</label>'
      +'<div class="parameter-note">当前参数状态 <code>'+escapeHtml(short(domain.parameterStatus))
      +'</code> · status_reason <code>'+escapeHtml(short(domain.policyStatusReason))
      +'</code> · high_risk_metrics <code>'+escapeHtml(domain.highRiskMetrics)+'</code>。'
      +'缺乏 medium_min/high_min 或未勾选 confirmed 时后端保持 not_configured / pending_confirmation，'
      +'此时不产生 classification 与 high-risk 指标。</div>';
  }).join('');

  return wbBlock('风险分级策略（工程设置）',
    '<div class="parameter-note">RouteRiskProfilePolicy 按 domain 逐个显式确认，'
    +'<b>系统绝不提供任何默认阈值</b>（0.6 / 0.8 之类的猜测值一律不出现）。'
    +'保存时必须满足 <code>0 ≤ medium_min ≤ high_min ≤ 1</code>；声明 confirmed 时还必须提供显式 source，'
    +'否则后端直接拒绝。未确认阈值的 domain 仍然输出 exposure / mean / max，'
    +'但 classification 与 high-risk 指标保持 not_configured / null。</div>'
    +(model.policyNotes.length?'<div class="parameter-note">后端 policy notes：'
      +model.policyNotes.map(note=>escapeHtml(text(note))).join('<br>')+'</div>':'')
    +fields
    +'<div class="button-row"><button class="secondary" id="saveRouteRiskProfilePolicy">保存阈值（按 domain 显式确认）</button></div>'
    +'<div class="parameter-note">保存只写 <code>route_risk_profile_policy</code>，并把既有 profile 标 stale；'
    +'不修改 candidate、operational_routes、CNS 或 grid_risk_v2。</div>');
}

function profileEvidenceBlock(profile){
  if(!profile)return '';
  const consistency=profile.consistency||{};
  const checks=Array.isArray(consistency.checks)?consistency.checks:[];
  const consistencyRows=checks.length
    ?checks.map(check=>listRow(check.check_id||'—',statusBadge(check.status||'not_evaluated')
      +' · reason_code '+escapeHtml(short(check.reason_code))
      +(check.reason?' · '+escapeHtml(text(check.reason)):'')
      +' · profile_value '+escapeHtml(String(check.profile_value??'—'))
      +' · candidate_value '+escapeHtml(String(check.candidate_value??'—')))).join('')
    :'<div class="empty-note">没有 consistency check</div>';

  const factorEntries=Object.values(profile.factors||{});
  const factorRows=factorEntries.map(item=>
    '<div class="list-row route-row"><span><b>'+escapeHtml(short(item.factor_id))+'</b>'
    +'<small>domain '+escapeHtml(short(item.domain))+' · status '+escapeHtml(short(item.status))
    +' · resolved '+escapeHtml(String(item.resolved===true))
    +' · raw_unit '+escapeHtml(short(item.raw_unit))+'</small>'
    +'<small>raw_exposure '+(item.raw_exposure?'value '+fmt(item.raw_exposure.value,3)+' '+escapeHtml(short(item.raw_exposure.unit)):'—')
    +' · normalized_exposure_index_m '+fmt(item.normalized_exposure_index_m,3)
    +' · weighted_contribution_index_m '+fmt(item.weighted_contribution_index_m,3)
    +' · weight '+(item.weight===null||item.weight===undefined?'—':escapeHtml(String(item.weight)))
    +' · rank '+escapeHtml(item.contributor_rank===null||item.contributor_rank===undefined?'—':String(item.contributor_rank))+'</small>'
    +'<small>resolved_length_m '+fmt(item.resolved_length_m,3)+' · unresolved_length_m '+fmt(item.unresolved_length_m,3)
    +' · canonical_source_available '+escapeHtml(String(item.canonical_source_available===true))+'</small>'
    +'<small>'+escapeHtml(RRP_CONTRIBUTOR_LABEL)+'</small></span></div>').join('')
    ||'<div class="empty-note">没有 factor 记录</div>';

  const ranking=(profile.contributors||{}).ranking||[];
  const rankingRows=ranking.length
    ?ranking.map(item=>listRow('rank '+String(item.rank)+' · '+short(item.factor_id),
      'domain '+escapeHtml(short(item.domain))+' · weight '+escapeHtml(String(item.weight??'—'))
      +' · weighted_contribution_index_m '+fmt(item.weighted_contribution_index_m,3)
      +' · normalized_exposure_index_m '+fmt(item.normalized_exposure_index_m,3))).join('')
    :'<div class="empty-note">contributors status='+escapeHtml(short((profile.contributors||{}).status))
      +' · reason '+escapeHtml(short((profile.contributors||{}).reason))+'</div>';

  const fingerprints=profile.fingerprints||{};
  const fingerprintRows=[
    ['profile_fingerprint',escapeHtml(short(fingerprints.profile_fingerprint))],
    ['policy_fingerprint',escapeHtml(short(fingerprints.policy_fingerprint))],
    ['candidate_fingerprint',escapeHtml(short(fingerprints.candidate_fingerprint))],
    ['path_fingerprint',escapeHtml(short(fingerprints.path_fingerprint))],
    ['grid_risk_v2_input_fingerprint',escapeHtml(short(fingerprints.grid_risk_v2_input_fingerprint))],
    ['grid_risk_v2_policy_fingerprint',escapeHtml(short(fingerprints.grid_risk_v2_policy_fingerprint))],
    ['grid_risk_v2_cells_fingerprint',escapeHtml(short(fingerprints.grid_risk_v2_cells_fingerprint))],
  ].map(row=>listRow(row[0],row[1])).join('');

  const provenance=profile.provenance||{};
  const provenanceRows=[
    ['pipeline',escapeHtml(short(provenance.pipeline))],
    ['algorithm',escapeHtml(short(provenance.algorithm))],
    ['integral_helper',escapeHtml(short(provenance.integral_helper))],
    ['planner_cost_helper_shared',escapeHtml(String(provenance.planner_cost_helper_shared===true))],
    ['risk_semantics',escapeHtml(short(provenance.risk_semantics))],
    ['risk_v2_overall_used',escapeHtml(String(provenance.risk_v2_overall_used===true))],
    ['replanning',escapeHtml(String(provenance.replanning===true))],
    ['candidate_mutated',escapeHtml(String(provenance.candidate_mutated===true))],
    ['writes_operational_routes_or_cns',escapeHtml(String(provenance.writes_operational_routes_or_cns===true))],
    ['airspace',escapeHtml(short((provenance.airspace||{}).applicability))+' · display_only'],
  ].map(row=>listRow(row[0],row[1])).join('');

  const notes=(profile.notes||[]).length
    ?'<div class="parameter-note">'+profile.notes.map(note=>escapeHtml(text(note))).join('<br>')+'</div>'
    :'';

  return wbBlock('工程证据（默认折叠）',
    wbDisclosure('factor contributors（relative engineering contribution）',
      '<div class="parameter-note">'+escapeHtml(RRP_CONTRIBUTOR_NOTE)+'</div>'
      +'<div class="scroll-list route-list">'+factorRows+'</div>')
    +wbDisclosure('contributor ranking（relative engineering contribution）',
      '<div class="parameter-note">排名只反映 relative engineering contribution，'
      +'不是事故原因概率，也不是优劣结论。</div><div class="scroll-list route-list">'+rankingRows+'</div>')
    +wbDisclosure('consistency（profile 与 candidate 数值一致性 gate）',
      '<div class="parameter-note">status '+statusBadge(consistency.status||'not_evaluated')
      +' · tolerance '+escapeHtml(String(consistency.tolerance??'—'))
      +' · '+escapeHtml(short(consistency.semantics))+'</div>'
      +'<div class="scroll-list route-list">'+consistencyRows+'</div>')
    +wbDisclosure('fingerprints',
      '<div class="scroll-list route-list">'+fingerprintRows+'</div>')
    +wbDisclosure('provenance',
      '<div class="scroll-list route-list">'+provenanceRows+'</div>'
      +'<div class="parameter-note">airspace 是 display_only，不进入任何数值或 fingerprint；'
      +'absolute risk / SORA GRC / ARC 一律 not_computed。</div>')
    +notes);
}

function currentProfileHeader(model,profile){
  const candidate=profile.candidate||{};
  const applicability=text(profile.current_applicability);
  const stale=profile.status==='stale'||applicability.startsWith('stale');
  const rows=[
    ['profile_id',escapeHtml(short(profile.profile_id))],
    ['status',statusBadge(profile.status||'not_calculated')
      +(stale?' · <b>stale：仅作为审计证据保留，不得当作 current 结论</b>':'')],
    ['current_applicability',escapeHtml(short(applicability))],
    ['stale_reason',escapeHtml(short(profile.stale_reason))],
    ['status_reason',escapeHtml(short(profile.status_reason))],
    ['candidate',escapeHtml(short(candidate.candidate_id||'—'))+' · status '+escapeHtml(short(candidate.status||'—'))
      +' · lane '+escapeHtml(short(candidate.lane_key||'—'))],
    ['route / layer',escapeHtml(short((profile.route||{}).route_id||candidate.route_id||'—'))
      +' · '+escapeHtml(short((profile.route||{}).altitude_layer_id||candidate.altitude_layer_id||'—'))
      +' · grid_level '+escapeHtml(short((profile.route||{}).grid_level||(profile.layer||{}).grid_level||'—'))],
    ['route_length_m',fmt(profile.route_length_m,3)+' m（后端累计距离基准）'],
    ['segments',escapeHtml(String((profile.segments||[]).length))+' 段 · cell '
      +escapeHtml(String((profile.route||{}).cell_count??'—'))],
    ['connector_semantics',escapeHtml(short(profile.connector_semantics))],
    ['artifact_type',escapeHtml(short(profile.artifact_type))+' · 只分析 LayeredRouteCandidate'],
  ].map(row=>listRow(row[0],row[1])).join('');
  const blockerRows_=(profile.blocking_reasons||[]).length
    ?wbDisclosure('blocking_reasons',
      '<div class="parameter-note">'+profile.blocking_reasons
        .map(item=>escapeHtml(short(item.reason_code)+' · '+text(item.reason))).join('<br>')+'</div>')
    :'';
  return wbBlock('当前 profile '+statusBadge(profile.status||'not_calculated'),
    (stale?'<div class="parameter-note"><b>该 profile 已 stale</b>：'
      +'输入（candidate / grid_risk_v2 / policy）在生成后发生变化，'
      +'旧 profile 作为审计证据保留而不删除、不覆盖。继续使用前必须重新生成。</div>':'')
    +'<div class="scroll-list route-list">'+rows+'</div>'+blockerRows_,
    stale?statusBadge('stale'):'');
}

function historyBlock(model){
  if(!model.profiles.length){
    return wbBlock('历史 profiles','<div class="empty-note">尚无 RouteRiskProfile 记录。</div>');
  }
  const rows=model.profiles.map(item=>{
    const stale=item.status==='stale'||item.currentApplicability.startsWith('stale');
    return '<div class="list-row route-row"><span><b>'+escapeHtml(short(item.profileId))+'</b> '
      +statusBadge(item.status||'not_calculated')
      +(stale?'<small><b>stale：保留为审计证据</b> · '+escapeHtml(short(item.staleReason))+'</small>':'')
      +'<small>applicability '+escapeHtml(short(item.currentApplicability))
      +' · status_reason '+escapeHtml(short(item.statusReason))+'</small>'
      +'<small><button class="secondary" data-delete-route-risk-profile="'+escapeHtml(item.profileId)
      +'">删除该 profile</button>（只调用 /api/route-risk-profiles/delete）</small></span></div>';
  }).join('');
  return wbBlock('历史 profiles '+statusBadge(model.collectionStatus),
    '<div class="parameter-note">stale profile 必须保留显示：它记录的是生成当时的候选、'
    +'grid_risk_v2 与阈值证据。删除仅移除该条记录，不影响 candidate、运行航路或 CNS 结果。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>');
}

function boundaryBlock(model){
  const notComputed=model.notComputed||{};
  const rows=[
    ['absolute_risk',escapeHtml(short(notComputed.absolute_risk||'not_computed'))],
    ['sora_grc',escapeHtml(short(notComputed.sora_grc||'not_computed'))],
    ['sora_arc',escapeHtml(short(notComputed.sora_arc||'not_computed'))],
    ['cross_domain_overall',escapeHtml(short(notComputed.cross_domain_overall||'not_computed'))],
    ['cross_domain_high_risk',escapeHtml(short(notComputed.cross_domain_high_risk||'not_computed'))],
  ].map(row=>listRow(row[0],row[1])).join('');
  return '<div class="parameter-note"><b>RouteRiskProfile 只分析 LayeredRouteCandidate</b>：'
    +'不 replan、不修改 operational_routes、不计算 absolute risk、不计算 SORA GRC/ARC，'
    +'也不生成 cross-domain overall 或 cross-domain high-risk。'
    +'classification 只在 per-domain 范围成立。</div>'
    +'<div class="scroll-list route-list">'+rows+'</div>';
}

/**
 * 渲染"路径风险画像"分段正文。
 * @param {object} flow published workflow snapshot
 */
export function renderRouteRiskProfile(flow){
  const model=routeRiskProfileModel(flow);
  const candidate=model.candidate||{};
  const overview=wbBlock('RouteRiskProfile V1 '+statusBadge(model.readinessStatus),
    '<div class="parameter-note">只分析 current LayeredRouteCandidate + current grid_risk_v2；'
    +'本面板不重算路径风险与距离，全部数值直接转印后端 profile。'+escapeHtml(RRP_SCOPE_NOTE)+'</div>'
    +'<div class="scroll-list route-list">'+readinessRows(model)+evalGateRow(model)+'</div>'
    +'<div class="button-row"><button class="primary" id="evaluateRouteRiskProfile" '
    +(model.readinessStatus==='ready'?'':'disabled')+'>生成风险画像</button></div>'
    +'<div class="parameter-note">按钮可用性来自后端 readiness：'
    +escapeHtml(model.readinessStatus==='ready'?'ready（无 blocker）':'blocked（blocker 未清除）')
    +'。前端不重新判断候选、grid_risk_v2 或阈值是否就绪。</div>'
    +'<h3>blockers（后端 readiness 原样转印）</h3>'
    +blockerRows(model)
    +'<div class="parameter-note">candidate_status 原值 <code>'+escapeHtml(short(candidate.status))
    +'</code> · 若它不是 candidate/current，后端会明确拒绝生成而不构造假画像。</div>');

  const domainCards=model.domains.map(domain=>wbBlock(
    domain.label+' '+statusBadge(domain.thresholdsStatus),
    domainCard(model,model.current,domain.domainId),
    domain.thresholdsStatus==='confirmed'?statusBadge('confirmed'):'')).join('');

  const policy=policyPanel(model);
  const evidence=model.current?profileEvidenceBlock(model.current)
    :wbBlock('工程证据（默认折叠）','<div class="empty-note">尚无 profile：工程证据随 profile 一起出现。</div>');

  const profileSection=model.current
    ?currentProfileHeader(model,model.current)
    :wbBlock('当前 profile '+statusBadge('not_calculated'),
      '<div class="empty-note">尚无 RouteRiskProfile：先补齐 blocker 与工程阈值，再点击"生成风险画像"。</div>');

  return overview+domainCards+profileSection+policy+evidence+historyBlock(model)+boundaryBlock(model);
}

// ---------------------------------------------------------------- bind

/** 读取表单控件的原始值；空串与 null 都保持"未配置"。 */
function fieldValue(node){
  if(!node)return '';
  const direct=node.value;
  if(direct!==undefined&&direct!==null)return String(direct);
  const attributes=node.attributes||{};
  const attribute=attributes.value;
  return attribute===undefined||attribute===null?'':String(attribute);
}

function numberFrom(id,c){
  const raw=fieldValue(c.$(id)).trim();
  // 空值一律提交 null：null ≠ 0，前端绝不补默认阈值。
  return raw===''?null:Number(raw);
}

function checkedFrom(id,c){
  const node=c.$(id);
  if(!node)return false;
  // 同时容忍布尔属性（真实 DOM）与特性字符串（服务端/桩渲染）。
  const checked=node.checked;
  if(checked===true||checked==='true')return true;
  const attributes=node.attributes||{};
  return attributes.checked===true||attributes.checked==='true';
}

/** 收集并提交按 domain 显式确认的阈值。 */
export function routeRiskProfilePolicyPayload(c){
  const domains={};
  for(const domainId of DOMAIN_IDS){
    domains[domainId]={
      medium_min:numberFrom(POLICY_FIELD_PREFIX+'Medium_'+domainId,c),
      high_min:numberFrom(POLICY_FIELD_PREFIX+'High_'+domainId,c),
      source:fieldValue(c.$(POLICY_FIELD_PREFIX+'Source_'+domainId)).trim(),
      confirmed:checkedFrom(POLICY_FIELD_PREFIX+'Confirmed_'+domainId,c),
    };
  }
  return {domains};
}

/**
 * 绑定"路径风险画像"分段的控件。
 * 每个查询都有存在性守卫：该分段未渲染时不会抛异常。
 */
export function bindRouteRiskProfile(c){
  if(c.$('evaluateRouteRiskProfile')){
    c.actionButton('evaluateRouteRiskProfile',()=>c.resourceAction('/api/route-risk-profiles/evaluate',{}));
  }
  if(c.$('saveRouteRiskProfilePolicy')){
    c.actionButton('saveRouteRiskProfilePolicy',()=>c.resourceAction(
      '/api/route-risk-profile-policy',routeRiskProfilePolicyPayload(c)));
  }
  for(const button of document.querySelectorAll('[data-delete-route-risk-profile]')){
    button.onclick=()=>c.resourceAction('/api/route-risk-profiles/delete',{
      profile_id:button.dataset.deleteRouteRiskProfile,
    }).catch(error=>c.panelError(error.message));
  }
}

export const ROUTE_RISK_PROFILE_SEGMENT='res-risk-profile';
