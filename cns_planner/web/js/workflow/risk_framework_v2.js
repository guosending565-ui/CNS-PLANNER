import {escapeHtml,statusBadge,statusText} from './common.js';
import {RISK_V2_DOMAIN_IDS,parseRiskV2Theme,riskV2Breaks} from '../map/grid_overlay.js';

// Risk Framework V2 workbench.  Everything shown here is a relative engineering
// index: no accident probability, no SORA GRC/ARC and no absolute safety risk.
//
// B4X：地图专题清单是**生产主界面**的一部分，因此标签一律使用中文业务语言，
// 不再把版本代号（V1 / V2）当作用户可见的前缀。工程标识（risk_v2:factor:… 等
// 专题取值本身、以及版本号）只允许出现在「高级 / 审计信息」区。
export const LEGACY_RISK_V1_LABEL='Legacy Risk V1';
//: 旧版风险模型的业务语言名称（生产界面使用；内部标识仍是 legacy Risk V1）。
export const LEGACY_RISK_BUSINESS_LABEL='旧版相对风险指数';
export const RISK_V2_PENDING_LABEL='聚合策略待确认';
export const RISK_V2_PENDING_NOTE='当前没有 confirmed aggregation policy，也没有任何默认生产 risk weight；domain index 保持 null，不显示伪 0。';
export const RISK_V2_FACTOR_LABELS={
  population_exposure:'人口暴露',
  property_exposure:'财产暴露（无数据源）',
  critical_infrastructure_exposure:'关键基础设施暴露（无数据源）',
  uav_traffic_exposure:'无人机交通暴露',
  conflict_exposure:'冲突暴露',
  terrain_relief:'地形起伏',
  building_coverage:'建筑覆盖率',
  building_height:'建筑高度',
};
export const RISK_V2_DOMAIN_LABELS={
  ground:'地面暴露',
  air_traffic:'空中交通暴露',
  environment_obstacle:'工程环境与障碍物',
};
export const RISK_V2_FACTOR_DOMAIN={
  population_exposure:'ground',
  property_exposure:'ground',
  critical_infrastructure_exposure:'ground',
  uav_traffic_exposure:'air_traffic',
  conflict_exposure:'air_traffic',
  terrain_relief:'environment_obstacle',
  building_coverage:'environment_obstacle',
  building_height:'environment_obstacle',
};
const RISK_V2_MAP_FACTORS=['population_exposure','uav_traffic_exposure','conflict_exposure','terrain_relief','building_coverage','building_height'];

export function riskV2ThemeOptions(){
  return [
    ...RISK_V2_MAP_FACTORS.map(factorId=>['risk_v2:factor:'+factorId,'风险因子 · '+RISK_V2_FACTOR_LABELS[factorId]]),
    ...RISK_V2_DOMAIN_IDS.map(domainId=>['risk_v2:domain:'+domainId,'风险域 · '+RISK_V2_DOMAIN_LABELS[domainId]]),
    ['ground_risk',LEGACY_RISK_BUSINESS_LABEL+' · 地面风险'],
    ['overall_risk',LEGACY_RISK_BUSINESS_LABEL+' · 综合风险'],
  ];
}

export function riskFrameworkV2Model(flow){
  const v2=flow?.grid_risk_v2||{},readiness=flow?.risk_framework_v2_readiness||{};
  const policy=flow?.risk_policy_v2||v2.policy||{};
  const factorReadiness=readiness.factors||{},domainReadiness=readiness.domains||{},policyDomains=policy.domains||{};
  const factorIds=Object.keys(RISK_V2_FACTOR_LABELS);
  const factors=factorIds.map(factorId=>{
    const summary=factorReadiness[factorId]||{},normalization=summary.normalization||{};
    const reference=normalization.reference||{};
    const coverage=Number.isFinite(summary.coverage)?summary.coverage:null;
    return {
      factorId,label:RISK_V2_FACTOR_LABELS[factorId],domain:RISK_V2_FACTOR_DOMAIN[factorId],
      status:summary.status||'not_calculated',readiness:summary.readiness||'blocked',
      rawUnit:summary.raw_unit??null,cellCount:summary.cell_count||0,
      coverage,
      normalizationMethod:normalization.method||null,
      referenceValue:Number.isFinite(reference.value)?reference.value:null,
      referenceFingerprint:summary.reference?.fingerprint||normalization.reference_fingerprint||null,
      sourceRole:summary.source_role||null,sourceId:summary.source_id||null,
      sourceFingerprint:summary.source_fingerprint||null,
      qualityFlags:summary.quality_flags||[],
      canonicalSourceAvailable:summary.canonical_source_available!==false,
    };
  });
  const domains=RISK_V2_DOMAIN_IDS.map(domainId=>{
    const result=domainReadiness[domainId]||{},policyDomain=policyDomains[domainId]||{};
    const weights=policyDomain.weights||{};
    return {
      domainId,label:RISK_V2_DOMAIN_LABELS[domainId],
      status:result.status||'not_calculated',
      index:Number.isFinite(result.index)?result.index:null,
      indexAvailable:Number.isFinite(result.index),
      policyStatus:policyDomain.status||'pending_confirmation',
      method:policyDomain.method||null,
      weights,
      requiredFactors:policyDomain.required_factors||[],
      aggregationPolicyFingerprint:result.aggregation_policy_fingerprint||null,
      dataCompleteness:Number.isFinite(result.data_completeness)?result.data_completeness:0,
      unresolved:result.unresolved||[],
      reason:result.reason||null,
      hasConfirmedWeights:policyDomain.status==='confirmed'&&Object.keys(weights).length>0,
    };
  });
  return {
    status:v2.status||'not_calculated',
    algorithmId:v2.algorithm_id||'risk-framework-v2-domains',
    algorithmVersion:v2.algorithm_version||'2.0',
    riskSemantics:v2.risk_semantics||'relative_engineering_index',
    absoluteRisk:v2.absolute_risk?.status||'not_computed',
    soraGrc:v2.sora_grc?.status||'not_computed',
    soraArc:v2.sora_arc?.status||'not_computed',
    airspaceStatus:v2.airspace?.status||'not_applicable',
    airspaceApplicability:v2.airspace?.applicability||'display_only',
    policyStatus:policy.status||'pending_confirmation',
    policyParameterStatus:policy.parameter_status||'no_default_production_risk_weights',
    policyFingerprint:readiness.policy?.fingerprint||v2.policy_fingerprint||null,
    inputFingerprint:v2.input_fingerprint||null,
    dataCompleteness:Number.isFinite(v2.data_completeness)?v2.data_completeness:0,
    factors,domains,
    pendingDomains:domains.filter(item=>!item.indexAvailable).map(item=>item.domainId),
    defaultProductionRiskWeights:false,
    factorCount:factors.length,
    layerCount:riskV2ThemeOptions().length,
  };
}

// Map legend + hover summary.  They are model-only helpers so ``main.js`` stays a
// thin assembly module; a pending/unresolved domain renders as "no data", not 0.
export function riskV2LegendModel(theme,cache,gridTheme){
  const selection=parseRiskV2Theme(theme);
  if(!selection)return null;
  const breaks=riskV2Breaks(cache,selection);
  const label=selection.kind==='domain'
    ? (RISK_V2_DOMAIN_LABELS[selection.id]||selection.id)
    : (RISK_V2_FACTOR_LABELS[selection.id]||selection.id);
  const shown=breaks.length?[breaks[0],breaks[Math.floor((breaks.length-1)/2)],breaks[breaks.length-1]]:[];
  return {
    selection,
    title:'Risk Framework V2 · '+(selection.kind==='domain'?'域 ':'因子 ')+label,
    unit:selection.kind==='domain'?'domain index（policy 未确认时为 null）':'factor index 0–1',
    ticks:shown.map(value=>gridTheme.formatNumber(value)),
    note:'Risk Framework V2 相对工程指数 · 非事故概率 / 非 SORA GRC / 非 SORA ARC · missing/unknown 与 pending domain 不补 0',
  };
}

export function riskV2CellSummary(riskV2Item,formatNumber){
  const domains=riskV2Item?.domains||{};
  const parts=RISK_V2_DOMAIN_IDS.map(domainId=>domainId+' '+
    (Number.isFinite(domains[domainId]?.index)
      ? formatNumber(domains[domainId].index)
      : '—（'+statusText(domains[domainId]?.status||'not_calculated')+'）'));
  return 'Risk Framework V2（relative engineering index）：'+parts.join(' · ')+
    '\n  factor maps '+(riskV2Item?.factors?Object.keys(riskV2Item.factors).length:0)+
    ' · absolute risk / SORA GRC / SORA ARC = not_computed';
}

function formatIndex(value){
  return value===null||value===undefined?'—':Number(value).toFixed(4);
}

function formatPercent(value){
  return Number.isFinite(value)?(value*100).toFixed(1)+'%':'—';
}

function factorRow(factor){
  const reference=factor.referenceValue===null?'—':String(factor.referenceValue);
  return '<div class="list-row"><span><b>'+escapeHtml(factor.label)+'</b> '+statusBadge(factor.status)+
    '<small>'+escapeHtml(factor.factorId)+' · domain '+escapeHtml(factor.domain)+
    ' · raw '+(factor.rawUnit?escapeHtml(factor.rawUnit):'无单位/无数据')+
    ' · cells '+factor.cellCount+
    ' · coverage '+formatPercent(factor.coverage)+
    '</small><small>normalization '+escapeHtml(factor.normalizationMethod||'—')+
    ' · reference '+escapeHtml(reference)+
    ' · ref fingerprint '+escapeHtml(factor.referenceFingerprint||'—')+
    '</small><small>source '+escapeHtml(factor.sourceRole||'—')+' · '+escapeHtml(factor.sourceId||'未记录')+
    ' · source fingerprint '+escapeHtml(factor.sourceFingerprint||'—')+
    (factor.qualityFlags.length?' · flags '+escapeHtml(factor.qualityFlags.join(', ')):'')+
    '</small></span></div>';
}

function domainCard(domain){
  const weights=Object.keys(domain.weights);
  const weightText=weights.length
    ? weights.map(key=>key+'='+formatIndex(domain.weights[key])).join(' · ')
    : '未配置（无默认权重）';
  const indexText=domain.indexAvailable?formatIndex(domain.index):RISK_V2_PENDING_LABEL;
  const unresolved=domain.unresolved.length?domain.unresolved.join(', '):'无';
  return '<div class="list-row"><span><b>'+escapeHtml(domain.label)+'</b> '+statusBadge(domain.status)+
    '<small>index '+escapeHtml(indexText)+
    ' · policy '+escapeHtml(domain.policyStatus)+
    ' · method '+escapeHtml(domain.method||'—')+
    ' · completeness '+formatPercent(domain.dataCompleteness)+
    '</small><small>weights '+escapeHtml(weightText)+
    ' · required '+escapeHtml(domain.requiredFactors.join(', ')||'未配置')+
    ' · unresolved '+escapeHtml(unresolved)+
    '</small><small>aggregation policy fingerprint '+escapeHtml(domain.aggregationPolicyFingerprint||'—')+
    '</small></span></div>';
}

export function renderRiskFrameworkV2Panel(flow){
  const model=riskFrameworkV2Model(flow);
  const legacyRisk=flow?.grid_risk||{};
  const domainCards=model.domains.map(domainCard).join('');
  const factorRows=model.factors.map(factorRow).join('');
  const pendingNote=model.pendingDomains.length
    ? '<div class="parameter-note">'+RISK_V2_PENDING_LABEL+'：'+escapeHtml(model.pendingDomains.join(', '))+'。'+RISK_V2_PENDING_NOTE+'</div>'
    : '<div class="parameter-note">三个 domain 均已有 confirmed policy 与 index。</div>';
  return '<h3>Risk Framework V2（相对工程指数）</h3>'+
    '<div class="flow-summary"><b>'+escapeHtml(model.riskSemantics)+'</b> · status '+statusText(model.status)+
    ' · '+escapeHtml(model.algorithmId)+'@'+escapeHtml(model.algorithmVersion)+
    '<br>factor → domain：Risk Factor / Exposure → Ground · Air / Traffic · Environment / Obstacle。'+
    '结果只是可解释的 relative engineering index，<b>不是</b>事故概率、SORA GRC/ARC 或绝对安全风险。'+
    '<br>absolute risk '+escapeHtml(model.absoluteRisk)+' · SORA GRC '+escapeHtml(model.soraGrc)+
    ' · SORA ARC '+escapeHtml(model.soraArc)+
    '（缺 verified failure / impact / exposure / consequence / encounter 模型）。'+
    '<br>airspace '+escapeHtml(model.airspaceStatus)+' / '+escapeHtml(model.airspaceApplicability)+
    '：适飞空域仍是 display_only，不进入任何 factor、value 或 fingerprint。'+
    '<br>policy '+escapeHtml(model.policyStatus)+' · '+escapeHtml(model.policyParameterStatus)+
    ' · policy fingerprint '+escapeHtml(model.policyFingerprint||'—')+
    '<br>input fingerprint '+escapeHtml(model.inputFingerprint||'—')+
    ' · completeness '+formatPercent(model.dataCompleteness)+'</div>'+
    '<div class="scroll-list">'+domainCards+'</div>'+pendingNote+
    '<h3>Canonical factors</h3>'+
    '<div class="parameter-note">UAV traffic exposure 来自既有 flight_count/flight_seconds，只属于 Air / Traffic domain，<b>不是</b>地面交通。'+
    '建筑 coverage 不是 sheltering；terrain/building hard clearance 是 feasibility，不转换成 risk。'+
    'missing/unknown 不补 0；valid_height_fraction &lt; 1 的 building height 保持 partial/unresolved。</div>'+
    '<div class="scroll-list">'+factorRows+'</div>'+
    '<div class="button-row"><button class="secondary" id="evaluateRiskV2">重新评估 V2（factor → domain）</button></div>'+
    '<h3>'+LEGACY_RISK_V1_LABEL+'（保持权威）</h3>'+
    '<div class="parameter-note">'+LEGACY_RISK_V1_LABEL+' = 现有 RiskModelV1 / grid_risk（ground / operational air / overall）。'+
    '它继续被当前 Risk-Aware Route Planner V2 消费；本轮 V2 是 additive，不改写 V1 输出、公式或默认模型。'+
    '地图专题中的 “'+LEGACY_RISK_V1_LABEL+' · 地面风险 / 综合风险” 即 V1 图层。</div>'+
    '<div class="flow-summary">'+LEGACY_RISK_V1_LABEL+'：'+statusText(legacyRisk.status||'not_calculated')+
    ' · '+escapeHtml(legacyRisk.algorithm_id||'未计算')+'@'+escapeHtml(legacyRisk.algorithm_version||'-')+
    ' · 完整度 '+formatPercent(Number.isFinite(legacyRisk.data_completeness)?legacyRisk.data_completeness:null)+'</div>';
}
