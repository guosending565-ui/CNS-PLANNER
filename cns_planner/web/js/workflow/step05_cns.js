import {advancedAuditNote,blockerList,escapeHtml,nextStepBar,shell,sourceModeText,statusBadge,statusText,wbBlock,wbPanel,wbSegHint} from './common.js';
import {RADAR_LAYOUT_EVALUATE_ENDPOINT,RADAR_LAYOUT_TITLE,RADAR_POLICY_ENDPOINT,radarLayoutModel,renderRadarSurveillanceLayoutPanel} from './radar_surveillance_layout.js';

// Radar Surveillance Layout V1（proposal-only）在 Step05 是**独立任务卡**：
// 重导出供前端测试与地图 overlay 使用，不改变本文件其余部分的既有结构。
export {RADAR_LAYOUT_EVALUATE_ENDPOINT,RADAR_LAYOUT_TITLE,RADAR_POLICY_ENDPOINT,radarLayoutModel,renderRadarSurveillanceLayoutPanel};

// 二级分段：同一一级标签下同屏只呈现一个任务，id 在整步内唯一。
// 名称是业务语言；工程编号（P7…P18）只允许出现在高级标签的 advancedAuditNote 里。
const OPERATE_SEGMENTS=[['cns-op-devices','设备与参数'],['cns-op-existing','已有设施'],['cns-op-candidates','候选站址']];
const RESULT_SEGMENTS=[['cns-res-coverage','三维覆盖评估'],['cns-res-capability','服务能力评估'],['cns-res-corridor','CNS 服务走廊'],['cns-res-gap','CNS 能力缺口'],['cns-res-site','CNS 设施规划'],['cns-res-radar','雷达监视规划']];
const ADVANCED_SEGMENTS=[['cns-adv-timeline','运行时间线'],['cns-adv-gapv2','保护与缺口分析'],['cns-adv-compat','旧版兼容试算'],['cns-adv-closedloop','高级方案影响试算']];

// ---- 六区结构（B4X §20） ----------------------------------------------------
// 每个二级分段都是"一步完整任务"，因此每一段内部按固定六区组织：
//   目标 → 输入准备 → 阻塞项与工程假设 → 主操作 → 结果 → 下一步。
// 硬约束：**同一分段内最多一个 primary 按钮**；其余动作一律 secondary 或移入高级标签。
// 每个分段的"下一步"只说明它在 canonical 链里的推进方向，不新增任何按钮。

/** canonical 生产链（result 标签的固定顺序，不含兼容与雷达分支）。 */
export const CNS_CANONICAL_CHAIN=[
  ['cns-res-coverage','三维覆盖'],
  ['cns-res-capability','服务能力'],
  ['cns-res-corridor','服务走廊'],
  ['cns-res-gap','能力缺口'],
  ['cns-res-site','设施规划']
];

/** 分段抬头：说明本段在 canonical 链中的位置。 */
function chainNote(current){
  const names=CNS_CANONICAL_CHAIN.map(item=>item[1]).join(' → ');
  return '<div class="parameter-note">链条：'+escapeHtml(names)+'；当前环节：'+escapeHtml(current)+'。</div>';
}

/** 该分段在链条上的下一步（只写文案，不生成按钮）。 */
function chainNext(current){
  const index=CNS_CANONICAL_CHAIN.findIndex(item=>item[0]===current);
  if(index<0)return '';
  const next=CNS_CANONICAL_CHAIN[index+1];
  return next?('下一环节：'+next[1]):'本环节是生产链的最后一步，完成后进入方案评审。';
}

/**
 * 分段抬头行：一句业务语言说明"这一段要达成什么、需要什么输入"。
 * 只做展示，不改变任何 gate。
 */
function segIntro(goal,inputs){
  return '<div class="parameter-note"><b>本段目标：</b>'+escapeHtml(goal)
    +'<br><b>输入准备：</b>'+escapeHtml(inputs)+'</div>';
}

/** 下一步：说明本段的推进方向与它为什么还不能推进。 */
function nextHint(text){
  return text?'<div class="flow-summary">下一步：'+escapeHtml(text)+'</div>':'';
}

/**
 * 状态徽章：**文字一律经集中词表取中文**，CSS 类名仍用 raw 值（配色语义不变）。
 * `statusText` 对未登记取值原样返回，因此这里必须给出业务兜底文案，
 * 绝不让 `not_calculated` / `missing_data` 这类 raw enum 露到界面文字上。
 */
function wbBadge(state,fallback='未计算'){
  const raw=String(state??'');
  const text=raw?statusText(raw):'';
  return statusBadge(raw||fallback,(text&&text!=='—')?text:fallback);
}

/** 兼容结果声明：不解锁下一步，也不得表述成"正式结果 / 已采纳 / 已应用"。 */
const COMPATIBILITY_NOTE='旧版兼容试算结果不解锁下一步，也不得表述为"正式结果 / 已采纳 / 已应用"；它们只保存在当前会话，仅供工程对照。';

// ---- Existing CNS 两组状态（事实掌握情况 / 规划模式） -------------------------
// 两个字段必须**分开**显示，并且都经 presentation.js 的集中词表取词。
// 字段缺失时显式显示"尚未声明 / 未配置"，绝不推断。

/**
 * 既有 CNS 设施**字段级**取词兼容层（`knowledge_status` / `planning_mode`）。
 *
 * B4X 之后这两个取值已经登记进集中词表 `presentation.js`（`not_declared` /
 * `confirmed_none` / `confirmed_present` / `factual` / `assume_empty_for_planning`），
 * 因此本函数的**第一优先**是全局 `statusText()`；下面的本地表只在全局词表尚未
 * 登记该取值时兜底，避免任何 raw enum 直接露到生产界面。
 * 它不参与任何业务判定，也不发明任何语义。
 */
export const EXISTING_CNS_FIELD_TEXT={
  knowledge_status:{
    not_declared:'尚未声明',confirmed_none:'已确认无',confirmed_present:'已确认存在'
  },
  planning_mode:{
    factual:'按事实数据规划',assume_empty_for_planning:'按空既有设施工程基线规划'
  }
};

/** 字段值 → 中文：集中词表优先，其次本步骤兜底表，最后原样返回（不编造）。 */
function existingFieldText(field,value){
  const key=String(value??'').trim();
  if(!key)return '';
  const centralized=statusText(key);
  if(centralized!==key)return centralized;
  const table=EXISTING_CNS_FIELD_TEXT[field]||{};
  if(Object.prototype.hasOwnProperty.call(table,key))return table[key];
  return key;
}

/** `flow.existing_cns_facilities.knowledge_status` → 中文。 */
export function existingKnowledgeLabel(knowledgeStatus){
  return existingFieldText('knowledge_status',knowledgeStatus)||'尚未声明';
}

/** `flow.existing_cns_facilities.planning_mode` → 中文。 */
export function existingPlanningModeLabel(planningMode){
  return existingFieldText('planning_mode',planningMode)||'未配置';
}

/** assume-empty 工程基线的逐字声明；只有该规划模式才持续显示。 */
export const ASSUME_EMPTY_BASELINE_NOTE='空既有设施工程规划基线；不表示现实中不存在既有 CNS 设施。';

/** 需要持续显示 assume-empty 声明时的判定（只读，不改任何业务状态）。 */
export function showsAssumeEmptyBaseline(existing){
  return (existing||{}).planning_mode==='assume_empty_for_planning';
}

/** 事实掌握情况 + 规划模式两行状态（外加 assume-empty 声明）。 */
export function existingCnsStatusRows(existing){
  const source=existing||{};
  return '<div class="flow-summary">事实掌握情况：<b>'+escapeHtml(existingKnowledgeLabel(source.knowledge_status))+'</b>'
    +' · 规划模式：<b>'+escapeHtml(existingPlanningModeLabel(source.planning_mode))+'</b></div>'
    +(showsAssumeEmptyBaseline(source)
      ?'<div class="parameter-note">'+escapeHtml(ASSUME_EMPTY_BASELINE_NOTE)+'</div>':'');
}

function collectionList(collection,kind){
  const items=collection?.items||[];
  if(!items.length)return '<div class="empty-note">尚未导入</div>';
  return items.slice(0,20).map(item=>{
    const id=kind==='facility'?item.facility_id:item.site_id;
    const detail=kind==='facility'?(item.devices||[]).map(device=>device.subsystem+' '+(device.name||device.device_id||'')).join(' · '):(item.available_subsystems||[]).join('/');
    return '<div class="list-row"><span><b>'+escapeHtml(item.name||id)+'</b><br><small>'+escapeHtml(id)+' · '+escapeHtml(detail||'未配置设备')+'</small></span><small>'+escapeHtml((item.coordinate||[]).join(', '))+'</small></div>';
  }).join('')+(items.length>20?'<div class="empty-note">另有 '+(items.length-20)+' 项</div>':'');
}

// ---- 共塔候选（真实铁塔宿主） -------------------------------------------------
// 共塔候选是**宿主候选**，不是已有 CNS 设备：列表只展示宿主事实与确认状态。
export const REUSE_CLASS_LABEL={
  existing_cns_facility:'已有站点',
  existing_shared_site:'共享站址',
  tower_colocation_host:'共塔候选',
  candidate_site:'普通候选',
  new_build_candidate:'新建候选'
};

export function reuseClassLabel(value){
  return REUSE_CLASS_LABEL[value]||value||'未知类型';
}

/** 子系统的安装证据等级：unverified 显示为「未核实」，绝不写成"已确认"。 */
function mountEvidenceLabel(value){
  const key=String(value??'').trim();
  if(!key||key==='unverified')return '未核实';
  if(key==='confirmed')return '已核实';
  return statusText(key);
}

/** 评分口径 raw 值 → 中文（绝不把内部代理名当成业务评分口径展示）。 */
function scoreSemanticsLabel(value){
  const key=String(value??'').trim();
  if(!key||key==='action_count_proxy')return '按动作数量代理';
  if(key==='action_count_proxy_no_currency')return '按动作数量代理（不含货币成本）';
  if(key==='marginal_gap_reduction')return '按边际缺口缩减量';
  return key;
}

/** 能力模型范围 raw 值 → 中文（未登记取值原样显示，绝不编造）。 */
function modelScopeLabel(value){
  const key=String(value??'').trim();
  if(!key)return '静态能力';
  if(key==='static_capability'||key==='static')return '静态能力';
  if(key==='propagation')return '传播模型';
  return key;
}

/** 规划宿主确认状态：eligible / not_confirmed 等 raw 值经集中词表取词。 */
function hostStatusLabel(value){
  const key=String(value??'').trim();
  if(!key)return '未确认';
  if(key==='eligible')return '已允许（工程规划层）';
  if(key==='ineligible')return '未允许（工程规划层）';
  return statusText(key);
}

/** 候选可行性状态：eligible / ineligible 等 raw 值经集中词表取词。 */
function eligibilityLabel(value){
  const key=String(value??'').trim();
  if(!key)return '证据不足';
  if(key==='eligible')return '满足条件';
  if(key==='ineligible')return '不满足条件';
  return statusText(key);
}

export function towerColocationList(collection){
  const items=collection?.items||[];
  if(!items.length)return '<div class="empty-note">尚无共塔候选：先导入真实铁塔，再执行"铁塔派生"</div>';
  const policy=collection.policy||{};
  const rows=items.slice(0,20).map(item=>{
    const host=(item.metadata||{}).host||{},mount=(item.metadata||{}).planning_host||{};
    const profile=(item.metadata||{}).obstacle_profile||{};
    const top=profile.tower_top_orthometric_m;
    return '<div class="list-row"><span><b>共塔候选</b> '+escapeHtml(host.host_tower_id||item.site_id)
      +'<br><small>铁塔 '+escapeHtml(host.host_tower_name||'—')+' · '+escapeHtml(host.host_site_type||'—')
      +' · 规划宿主'+(host.planning_host_use_confirmed?'已允许':'未确认')+'</small>'
      +'<br><small>物理安装 未核实（需现场勘察） · 分系统证据状态 '+escapeHtml(mountEvidenceLabel(mount.subsystem_mount_status))+'</small>'
      +'<br><small>塔顶 '+(typeof top==='number'?top.toFixed(1)+' m EGM2008':'未解析')
      +' · 位置'+(host.site_position_available?'可用':'未知')+'</small></span>'
      +'<small>'+escapeHtml(reuseClassLabel((item.planning_profile||{}).reuse_class))+'</small></div>';
  }).join('');
  return '<div class="parameter-note">共塔候选 = 真实铁塔作为<b>共塔规划宿主</b>的工程候选；它们不是已有 CNS 设备，也不带任何设备性能参数。'
    +'<br>规划层：'+escapeHtml(hostStatusLabel(policy.planning_host_status||'not_confirmed'))
    +' · 物理实施层：未核实，需现场勘察（requires_site_survey='+String(policy.requires_site_survey!==false)+'）'
    +'<br>优先共塔（prefer，不是 force）：塔不能满足缺口时仍会生成普通候选站。</div>'    +rows+(items.length>20?'<div class="empty-note">另有 '+(items.length-20)+' 项</div>':'');
}

/**
 * Tower Colocation Policy 表单（Step05「操作 → 候选站址」）。
 *
 * **两层语义，必须分开**：
 *
 * * 规划层 —— `planning_host_use_confirmed`：允许 P11/P16 在**工程规划方案**里把真实铁塔
 *   作为 Preferred Host Site 参与 what-if 比较；
 * * 物理实施层 —— `physical_mount_confirmed`（恒 false）/ `requires_site_survey`（恒 true）：
 *   本阶段没有逐塔现场调查数据，因此**绝不**把 373 个铁塔写成"该铁塔确实可以安装设备"。
 *
 * 只写既有的 `tower_colocation_policy` 字段，并复用**现有**端点
 * `POST /api/tower-obstacle-profiles/evaluate`（payload 携带 `tower_colocation_policy`）：
 * 不新增第二套 API/contract。
 */
export function towerColocationPolicyForm(flow){
  const colocation=flow.tower_colocation_candidates||{},policy=colocation.policy||{};
  const profiles=flow.tower_obstacle_profiles||{};
  const assumption=policy.service_origin_assumption||'';
  const planningHost=policy.planning_host_use_confirmed===true;
  return '<div class="form-grid">'
    +'<label>服务原点假设<select id="towerColocationOrigin">'
    +'<option value="" '+(assumption===''?'selected':'')+'>未假设（不把塔顶当服务原点）</option>'
    +'<option value="tower_top_agl_0" '+(assumption==='tower_top_agl_0'?'selected':'')+'>塔顶 EGM2008 · 挂高 0（显式假设）</option>'
    +'</select></label></div>'
    +'<label class="check-row"><input type="checkbox" id="towerColocationPlanningHost" '+(planningHost?'checked':'')+'>允许真实铁塔作为共塔规划宿主候选（工程规划假设，不涉及物理安装确认）</label>'
    +'<div class="flow-summary">物理安装条件：<b>未逐塔核实，需现场勘察</b>'
    +'（physical_mount_confirmed=false · requires_site_survey=true，即"尚未确认物理安装、需要现场勘察"）</div>'
    +'<label>策略来源<input class="panel-input" id="towerColocationSource" value="'+escapeHtml(policy.source||'')+'"></label>'
    +'<div class="button-row"><button class="secondary" id="saveTowerColocationPolicy">保存策略并派生候选</button>'
    +'<button class="secondary" id="deriveTowerColocation">重新解析塔顶高程事实</button></div>'
    +'<div class="flow-summary">规划宿主 '+statusBadge(policy.planning_host_status||'not_confirmed')
    +' · 策略 '+(policy.enabled===true?'已启用':'未启用（共塔候选尚不满足规划条件）')
    +' · 分系统安装证据 '+escapeHtml(mountEvidenceLabel(policy.subsystem_mount_status))
    +'<br>共塔候选 '+(colocation.count||0)+' 个 · 塔顶已解析 '+(profiles.resolved_count||0)
    +' · 未解析 '+(profiles.unresolved_count||0)
    +'<br>两个条件（规划宿主允许 + 服务原点假设）满足才会进入规划；'
    +'设备型号与性能参数仍只能来自设备资料库 / 用户确认，绝不从铁塔数据推断；'
    +'分系统是否真的装得上保持未核实，需现场勘察。</div>';
}

function gapList(analysis){
  if(!analysis?.routes?.length)return '<div class="empty-note">尚未运行运行航路缺口分析</div>';
  return analysis.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+wbBadge(route.status)+'</b>'+route.subsystems.map(item=>'<div class="gap-row"><strong>'+item.subsystem+'</strong><span>'+statusText(item.status)+'</span><span>覆盖 '+(item.coverage_ratio==null?'—':(item.coverage_ratio*100).toFixed(1)+'%')+'</span><span>缺口 '+(item.gap_length_m==null?'—':Math.round(item.gap_length_m)+' m')+'</span></div>').join('')+'</div>').join('');
}

function coverage3dList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未运行三维几何覆盖</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+wbBadge(route.status)+'</b>'+route.subsystems.map(item=>'<div class="gap-row"><strong>'+item.subsystem+'</strong><span>'+statusText(item.status)+'</span><span>覆盖 '+(item.covered_fraction==null?'—':(item.covered_fraction*100).toFixed(1)+'%')+'</span><span>未覆盖 '+(item.uncovered_length_m==null?'—':Math.round(item.uncovered_length_m)+' m')+'</span></div>').join('')+'</div>').join('');
}

function capabilityList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未计算 CNS 服务能力</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' '+wbBadge(route.status)+'</b>'+route.subsystems.map(item=>{const sample=(item.samples||[]).find(value=>(value.provider_evaluations||[]).length)||(item.samples||[])[0]||{},provider=(sample.provider_evaluations||[])[0]||{},margin=provider.link_budget?.link_margin_db;return '<div class="coverage-card"><b>'+item.subsystem+' · '+statusText(item.status)+'</b><span>满足 '+pct(item.meets_fraction)+' · 不满足 '+pct(item.fail_fraction)+' · 证据不足 '+pct(item.unknown_fraction)+'</span><span>模型范围 '+escapeHtml(modelScopeLabel(provider.model_scope||item.model_scope))+' · '+escapeHtml(provider.model_family||'模型未确认')+(Number.isFinite(margin)?' · 链路余量 '+margin.toFixed(1)+' dB':'')+'</span><small>'+escapeHtml((provider.reasons||sample.reasons||[]).join('；')||'无额外证据')+'</small></div>';}).join('')+'</div>').join('');
}

function pct(value){return value==null?'—':(value*100).toFixed(1)+'%';}

function timelineList(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未生成服务时间线</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+(route.duration_s==null?'时间未知':route.duration_s.toFixed(1)+' s')+'</b>'+route.subsystems.map(item=>{const duration=item.duration_by_state_s||{},length=item.length_by_state_m||{};return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml((item.states_present||[]).map(state=>statusText(state)).join('/'))+'</b><span>可用 '+formatMetric(duration.available,'s')+' · 降级可用 '+formatMetric(duration.available_degraded,'s')+' · 应急 '+formatMetric(duration.contingency,'s')+'</span><span>丢失 '+formatMetric(duration.lost,'s')+' · 证据不足 '+formatMetric(duration.unknown,'s')+' / '+formatMetric(length.unknown,'m')+'</span></div>';}).join('')+'</div>').join('');
}

function protectionSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未计算保护包络</div>';
  return '<div class="coverage-card"><b>'+statusText(result.status)+' · '+escapeHtml(result.model_scope||'工程战术保护包络')+'</b><span>预置时间 T_pre '+formatMetric(result.t_pre_s,'s')+' · 反应距离 D_reaction '+formatMetric(result.d_reaction_m,'m')+' · 保护距离 D_protect '+formatMetric(result.d_protect_m,'m')+'</span><small>'+escapeHtml((result.reasons||[]).join('；')||'输入已确认')+'</small></div>';
}

function gapV2List(result){
  if(!result?.routes?.length)return '<div class="empty-note">尚未运行能力缺口分析</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+statusText(route.status||'unknown')+'</b>'+route.subsystems.map(item=>{
    const segments=(item.segments||[]).filter(segment=>segment.combined_status!=='satisfied'&&segment.combined_status!=='not_applicable').slice(0,12);
    const rows=segments.map(segment=>{const evidence=(segment.evidence||[]).map(value=>value.kind).filter(Boolean).join(' / ');return '<div class="list-row"><span><b>'+escapeHtml(statusText(segment.combined_status))+'</b> '+Math.round(segment.start_route_offset_m)+'–'+Math.round(segment.end_route_offset_m)+' m<br><small>'+escapeHtml((segment.gap_causes||[]).join(' / ')||'无结构化原因')+' · '+escapeHtml(segment.remediation_scope||'未给出处置范围')+'</small></span><small>'+escapeHtml((segment.reasons||[]).join('；')||'无额外原因')+'<br>证据：'+escapeHtml(evidence||'证据不足')+'</small></div>';}).join('');
    return '<div class="coverage-card"><b>'+item.subsystem+' · '+statusText(item.status)+'</b><span>规划缺口 '+formatMetric(item.planning_assessment?.length_by_status_m?.confirmed_gap,'m')+' · 运行丢失 '+formatMetric(item.runtime_lost_length_m,'m')+' / '+formatMetric(item.runtime_lost_duration_s,'s')+'</span><span>合计缺口 '+formatMetric(item.gap_length_m,'m')+' · 应急暴露 '+formatMetric(item.contingency_exposure_length_m,'m')+' / '+formatMetric(item.contingency_exposure_duration_s,'s')+' · 证据不足 '+formatMetric(item.unknown_length_m,'m')+'</span><span>最大连续缺口 '+formatMetric(item.max_continuous_gap_length_m,'m')+' / '+formatMetric(item.max_continuous_gap_duration_s,'s')+'</span>'+rows+'</div>';
  }).join('')+'</div>').join('');
}

function sitePlanSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未运行复用优先站址试算</div>';
  const impacts=new Map((result.candidate_impacts||[]).map(item=>[item.action_id,item]));
  const candidates=(result.candidate_actions||[]).slice(0,20).map(action=>{const impact=impacts.get(action.action_id)||{};const host=action.host||{};return '<div class="list-row"><span><b>'+escapeHtml(action.action_id)+'</b>'+(action.reuse_class==='tower_colocation_host'?' <b>共塔候选</b>':'')+'<br><small>'+escapeHtml(reuseClassLabel(action.reuse_class))+' · '+escapeHtml(eligibilityLabel(action.eligibility?.status))+(host.host_tower_id?' · 铁塔 '+escapeHtml(host.host_tower_id):'')+' · 分系统安装 '+escapeHtml(action.subsystem_mount_status||'unverified')+(action.requires_site_survey?'（需现场勘察）':'')+'</small></span><small>假设收益 what-if gain '+formatMetric(impact.planning_gap_reduction_m,'m')+'</small></div>';}).join('');
  const selected=(result.selected_actions||[]).map(action=>'<div class="coverage-card"><b>'+escapeHtml(action.action_id)+'</b><span>'+escapeHtml(reuseClassLabel(action.reuse_class))+' · '+escapeHtml(action.subsystem)+' · 边际收益 '+formatMetric(action.marginal_planning_gap_reduction_m,'m')+'</span><span>规划宿主 '+escapeHtml(hostStatusLabel(action.planning_host_status||'not_applicable'))+' · 分系统安装 '+escapeHtml(action.subsystem_mount_status||'unverified')+' · 物理安装'+(action.physical_mount_confirmed===true?'已确认':'未核实')+(action.requires_site_survey?'（需现场勘察）':'')+'</span><small>评分口径：'+escapeHtml(scoreSemanticsLabel(action.score_semantics))+'</small></div>').join('');
  return '<div class="coverage-card"><b>'+statusText(result.status||'unknown')+' · 仅提案（未应用）</b><span>目标 '+formatMetric(result.target_planning_gap_length_m,'m')+' · 预计闭合 '+formatMetric(result.resolved_planning_gap_length_m,'m')+' · 剩余 '+formatMetric(result.remaining_planning_gap_length_m,'m')+'</span><span>已有站点 '+(result.existing_reuse_count||0)+' · 共享站址 '+(result.shared_site_reuse_count||0)+' · 共塔候选 '+((result.reuse_counts||{}).tower_colocation_host||0)+' · 普通候选 '+(result.candidate_site_count||0)+' · 新建候选 '+(result.new_build_count||0)+'</span><small>'+escapeHtml(result.cost_summary?.cost_semantics||'按动作数量代理，不含货币成本')+'；需闭环复核确认</small></div><h4>候选动作假设收益</h4>'+candidates+'<h4>选中的规划方案</h4>'+(selected||'<div class="empty-note">没有产生正规划缺口缩减量的可行动作</div>');
}

/*
 * P12 closed-loop residual semantics
 *
 * 后端当前只提供 legacy residual_gap_segments。
 * 不能把 residual_gap_segments 的数量直接解释成“规划残余”。
 *
 * planning_status === confirmed_gap  -> 规划残余
 * runtime_status  === confirmed_gap  -> 运行场景残余
 */
function asArray(value){
  return Array.isArray(value)?value:[];
}

function sumBy(items,field){
  return asArray(items).reduce((sum,item)=>{
    const value=Number(item?.[field]);
    return sum+(Number.isFinite(value)?value:0);
  },0);
}

function classifyClosedLoopResiduals(result){
  const residuals=asArray(result?.residual_gap_segments);

  const planningResiduals=residuals.filter(
    item=>item?.planning_status==='confirmed_gap'
  );

  const operationalResiduals=residuals.filter(
    item=>item?.runtime_status==='confirmed_gap'
  );

  const operationalKeys=new Set(
    operationalResiduals.map(item=>item?.segment_id).filter(Boolean)
  );

  residuals.forEach(item=>{
    const legacyOperational=
      item?.planning_status==='satisfied' &&
      item?.combined_status==='confirmed_gap' &&
      item?.runtime_status!=='confirmed_gap';

    const key=item?.segment_id;

    if(legacyOperational && (!key || !operationalKeys.has(key))){
      operationalResiduals.push(item);
      if(key)operationalKeys.add(key);
    }
  });

  return {
    residuals,
    planningResiduals,
    operationalResiduals,
    regressions:asArray(result?.regression_segments),
    planningResidualLengthM:sumBy(planningResiduals,'length_m'),
    operationalResidualLengthM:sumBy(operationalResiduals,'length_m'),
    operationalResidualDurationS:sumBy(operationalResiduals,'duration_s')
  };
}

function residualCauses(item){
  const causes=asArray(item?.gap_causes).filter(Boolean);
  return causes.length?causes.join(' / '):'未提供结构化原因';
}

function closedLoopResidualCard(item,kind){
  const title=kind==='planning'?'规划残余缺口':'运行场景残余';
  const startM=Number(item?.start_route_offset_m);
  const endM=Number(item?.end_route_offset_m);
  const lengthM=Number(item?.length_m);
  const startS=Number(item?.start_time_s);
  const endS=Number(item?.end_time_s);
  const durationS=Number(item?.duration_s);

  const rangeText=
    (Number.isFinite(startM)?startM.toFixed(1):'—')+
    '–'+
    (Number.isFinite(endM)?endM.toFixed(1):'—')+
    ' m';

  const timeText=
    (Number.isFinite(startS)?startS.toFixed(1):'—')+
    '–'+
    (Number.isFinite(endS)?endS.toFixed(1):'—')+
    ' s';

  return '<div class="coverage-card">'+
    '<b>'+escapeHtml(item?.route_id||'—')+' · '+escapeHtml(item?.subsystem||'—')+' · '+title+'</b>'+
    '<span>航路位置 '+rangeText+' · 长度 '+formatMetric(lengthM,'m')+'</span>'+
    '<span>时间 '+timeText+' · 持续 '+formatMetric(durationS,'s')+'</span>'+
    '<span>规划判定 '+escapeHtml(statusText(item?.planning_status||'unknown'))+
      ' · 运行判定 '+escapeHtml(statusText(item?.runtime_status||'unknown'))+
      ' · 合计判定 '+escapeHtml(statusText(item?.combined_status||'unknown'))+'</span>'+
    '<small>原因：'+escapeHtml(residualCauses(item))+
      '；处置范围：'+escapeHtml(segmentScopeLabel(item?.remediation_scope))+'</small>'+
    '</div>';
}

/** 处置范围 raw 值 → 中文（未登记取值原样显示，绝不编造）。 */
function segmentScopeLabel(value){
  const key=String(value??'').trim();
  if(!key)return '未给出处置范围';
  if(key==='unknown')return '证据不足';
  return key;
}

/** 写入状态 raw 值 → 中文（"未写入正式项目"绝不写成"已应用"）。 */
function commitStatusLabel(value){
  const key=String(value??'').trim();
  if(!key||key==='not_committed')return '尚未写入正式项目';
  if(key==='preview')return '预览（未写入正式项目）';
  if(key==='committed'||key==='applied')return '已写入正式项目';
  return statusText(key);
}

function closedLoopSummary(result){
  if(!result||result.status==='not_calculated'){
    return '<div class="empty-note">尚未生成方案影响试算</div>';
  }

  const prediction=result.prediction_comparison||{};
  const classified=classifyClosedLoopResiduals(result);

  const planningResiduals=classified.planningResiduals;
  const operationalResiduals=classified.operationalResiduals;
  const regressions=classified.regressions;

  const planningClosed=planningResiduals.length===0;
  const noRegression=regressions.length===0;

  const rows=(result.comparisons||[]).map(item=>
    '<div class="coverage-card">'+
      '<b>'+escapeHtml(item.route_id)+' · '+escapeHtml(item.subsystem)+'</b>'+
      '<span>规划确认缺口 '+formatMetric(item.before?.planning_confirmed_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.planning_confirmed_gap_length_m,'m')+
        ' · 变化 '+formatMetric(item.delta?.planning_confirmed_gap_length_m,'m')+'</span>'+
      '<span>合计缺口 '+formatMetric(item.before?.combined_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.combined_gap_length_m,'m')+
        ' · 证据不足 '+formatMetric(item.before?.unknown_length_m,'m')+
        ' → '+formatMetric(item.after?.unknown_length_m,'m')+'</span>'+
      '<span>运行丢失 '+formatMetric(item.before?.runtime_lost_length_m,'m')+
        ' / '+formatMetric(item.before?.runtime_lost_duration_s,'s')+
        ' → '+formatMetric(item.after?.runtime_lost_length_m,'m')+
        ' / '+formatMetric(item.after?.runtime_lost_duration_s,'s')+'</span>'+
      '<span>应急暴露 '+formatMetric(item.before?.contingency_exposure_length_m,'m')+
        ' → '+formatMetric(item.after?.contingency_exposure_length_m,'m')+
        ' · 最大连续缺口 '+formatMetric(item.before?.max_continuous_gap_length_m,'m')+
        ' → '+formatMetric(item.after?.max_continuous_gap_length_m,'m')+'</span>'+
    '</div>'
  ).join('');

  const planningSummary=planningClosed
    ? '<div class="coverage-card"><b>规划缺口已闭合</b><span>规划残余 0.0 m</span><small>复核后没有"规划判定 = 确认缺口"的残余分段。</small></div>'
    : '<div class="coverage-card"><b>仍存在规划残余缺口</b><span>'+planningResiduals.length+
      ' 段 · '+formatMetric(classified.planningResidualLengthM,'m')+
      '</span><small>仅统计"规划判定 = 确认缺口"的分段。</small></div>';

  const operationalSummary=operationalResiduals.length
    ? '<div class="coverage-card"><b>仍存在运行场景残余</b><span>'+
      operationalResiduals.length+' 段 · '+
      formatMetric(classified.operationalResidualLengthM,'m')+
      ' · '+formatMetric(classified.operationalResidualDurationS,'s')+
      '</span><small>运行场景残余不等于规划建站失败；应结合服务场景事件与运行证据处置。</small></div>'
    : '<div class="coverage-card"><b>无已确认的运行场景残余</b><span>运行残余 0.0 m</span></div>';

  const planningCards=planningResiduals.slice(0,12)
    .map(item=>closedLoopResidualCard(item,'planning'))
    .join('');

  const operationalCards=operationalResiduals.slice(0,12)
    .map(item=>closedLoopResidualCard(item,'operational'))
    .join('');

  const regressionCards=regressions.slice(0,12).map(item=>
    '<div class="list-row"><span><b>'+
      escapeHtml(item.route_id)+' · '+escapeHtml(item.subsystem)+
      '</b> '+Math.round(item.start_route_offset_m||0)+'–'+
      Math.round(item.end_route_offset_m||0)+
      ' m</span><small>'+
      escapeHtml(statusText(item.before_combined))+' → '+
      escapeHtml(statusText(item.after_combined))+'</small></div>'
  ).join('');

  const validationTitle=
    escapeHtml(statusText(result.validation_status||result.status||'unknown'))+
    ' · '+
    escapeHtml(commitStatusLabel(result.commit_status));

  const regressionSummary=noRegression
    ? '<div class="coverage-card"><b>回退项：0</b><span>未发现已确认回退</span></div>'
    : '<div class="coverage-card"><b>回退项：'+regressions.length+'</b><span>发现应用方案后的已确认回退，需要人工复核。</span></div>';

  return '<div class="coverage-card">'+
      '<b>'+validationTitle+'</b>'+
      '<span>试算预测缩减 '+formatMetric(prediction.predicted_planning_gap_reduction_m,'m')+
        ' · 复核实测缩减 '+formatMetric(prediction.actual_planning_gap_reduction_m,'m')+
        ' · 误差 '+formatMetric(prediction.prediction_error_m,'m')+'</span>'+
      '<span>实际达成比例 '+(prediction.realized_fraction==null?'—':(prediction.realized_fraction*100).toFixed(1)+'%')+'</span>'+
      '<small>'+escapeHtml((result.reasons||[]).join('；')||'无额外原因')+'</small>'+
    '</div>'+
    planningSummary+
    operationalSummary+
    regressionSummary+
    rows+
    '<h4>规划残余缺口</h4>'+
      (planningCards||'<div class="empty-note">无已确认的规划残余缺口</div>')+
    '<h4>运行场景残余</h4>'+
      (operationalCards||'<div class="empty-note">无已确认的运行场景残余</div>')+
    '<h4>回退项</h4>'+
      (regressionCards||'<div class="empty-note">未发现已确认回退</div>');
}

function corridorSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未评估 CNS 服务走廊</div>';
  if(!result.routes?.length)return '<div class="empty-note">服务走廊缺少可评估航路或已确认参数</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+escapeHtml(statusText(route.status||'unknown'))+' · '+(route.voxel_count||0)+' 个体元</b>'+((route.subsystems||[]).map(item=>'<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(statusText(item.status||'unknown'))+'</b><span>满足体元 '+(item.satisfied_voxel_count||0)+' · 确认缺口 '+(item.confirmed_deficit_voxel_count||0)+' · 证据不足 '+(item.unknown_voxel_count||0)+'</span><span>体积占比 满足 '+pct(item.satisfied_volume_fraction)+' · 缺口 '+pct(item.confirmed_deficit_volume_fraction)+' · 证据不足 '+pct(item.unknown_volume_fraction)+'</span><small>离散体积代理；代表性体元探测不等于整个体元都有保证</small></div>').join(''))+'</div>').join('');
}

function planningObjectivesPanel(flow){
  const routes=flow.operational_routes||[],firstRoute=routes[0]?.route_id||'',policy=flow.cns_planning_objectives||{routes:{}},entry=policy.routes?.[firstRoute]?.subsystems?.C?.objectives||{};
  const routeOptions=routes.map(item=>'<option value="'+escapeHtml(item.route_id)+'">'+escapeHtml(item.route_id)+'</option>').join('');
  const field=(id,label,name,operator)=>'<label>'+label+' ('+operator+')<input class="panel-input" type="number" min="0" step="any" id="'+id+'" value="'+(entry[name]?.value??'')+'"></label>';
  return '<h3>CNS 空间规划目标</h3><div class="parameter-note">规划目标与 CNS 能力需求分离；没有已确认的显式目标时保持"未配置规划目标"，不提供监管或工程默认阈值。</div><div class="form-grid"><label>航路<select id="planningObjectiveRoute">'+routeOptions+'</select></label><label>分系统<select id="planningObjectiveSubsystem"><option>C</option><option>N</option><option>S</option></select></label>'+field('objectiveMinSatisfied','最小满足体积占比','min_satisfied_volume_fraction','≥')+field('objectiveMaxDeficit','最大确认缺口体积占比','max_confirmed_deficit_volume_fraction','≤')+field('objectiveMaxUnknown','最大证据不足体积占比','max_unknown_volume_fraction','≤')+field('objectiveMinRedundancy','最小冗余满足体积占比','min_redundancy_satisfied_volume_fraction','≥')+field('objectiveMaxContinuous','最大空间连续缺口投影 m','max_continuous_deficit_projection_m','≤')+'<label>来源<input id="planningObjectiveSource" value="user_configuration"></label><label class="check-row"><input type="checkbox" id="planningObjectiveConfirmed">目标已确认</label></div><div class="button-row"><button class="secondary" id="savePlanningObjectives" '+(!routeOptions?'disabled':'')+'>保存目标</button><button class="primary" id="evaluateCorridorGap">评估能力缺口</button></div>';
}

/** 规划目标字段名 → 中文（未登记取值原样显示，绝不编造；字段名仍在高级审计里可查）。 */
export const OBJECTIVE_FIELD_LABEL={
  min_satisfied_volume_fraction:'最小满足体积占比',
  max_confirmed_deficit_volume_fraction:'最大确认缺口体积占比',
  max_unknown_volume_fraction:'最大证据不足体积占比',
  min_redundancy_satisfied_volume_fraction:'最小冗余满足体积占比',
  max_continuous_deficit_projection_m:'最大空间连续缺口投影'
};

function objectiveLabel(value){
  const key=String(value??'').trim();
  if(!key)return '未命名规划目标';
  return OBJECTIVE_FIELD_LABEL[key]||key;
}

function corridorGapSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未评估能力缺口</div>';
  if(!result.routes?.length)return '<div class="empty-note">需要当前已确认的服务走廊评估结果</div>';
  return result.routes.map(route=>'<div class="gap-route"><b>'+escapeHtml(route.route_id)+' · '+escapeHtml(statusText(route.status||'unknown'))+'</b>'+route.subsystems.map(item=>{const service=item.service||{},redundancy=item.redundancy||{},segments=item.continuous_deficit_segments||[],objectiveRows=(item.objective_results||[]).map(value=>'<div class="list-row"><span>'+escapeHtml(objectiveLabel(value.objective))+'</span><small>实际值 '+(value.actual==null?'—':Number(value.actual).toFixed(3))+' '+escapeHtml(value.operator||'')+' '+(value.target??'—')+' · '+escapeHtml(statusText(value.status))+'</small></div>').join('');return '<div class="coverage-card"><b>'+item.subsystem+' · '+escapeHtml(statusText(item.status))+' · '+escapeHtml(statusText(item.objective_status))+'</b><span>服务 满足 '+pct(service.fractions?.satisfied)+' · 确认缺口 '+pct(service.fractions?.confirmed_deficit)+' · 证据不足 '+pct(service.fractions?.unknown)+'</span><span>冗余 满足 '+pct(redundancy.fractions?.satisfied)+' · 确认缺口 '+pct(redundancy.fractions?.confirmed_deficit)+' · 证据不足 '+pct(redundancy.fractions?.unknown)+'</span><span>空间连续缺口合计 '+formatMetric(item.total_confirmed_deficit_projection_m,'m')+' · 最大 '+formatMetric(item.max_continuous_deficit_projection_m,'m')+' · 分段 '+segments.length+'</span>'+objectiveRows+'</div>';}).join('')+'</div>').join('');
}

function corridorSitePlanSummary(result){
  if(!result||result.status==='not_calculated')return '<div class="empty-note">尚未运行走廊感知设施规划</div>';
  const selected=(result.selected_actions||[]).map(action=>'<div class="coverage-card"><b>'+escapeHtml(action.action_id)+'</b><span>'+escapeHtml(reuseClassLabel(action.reuse_class))+' · '+escapeHtml(action.subsystem||'')+' · 单位体积收益 '+formatMetric(action.marginal_confirmed_requirement_unit_volume_gain,'m³·单元')+'</span><small>评分口径：'+escapeHtml(scoreSemanticsLabel(action.score_semantics))+'</small></div>').join('');
  const trace=(result.iteration_trace||[]).map(item=>'<div class="list-row"><span>第 '+item.iteration+' 轮 '+escapeHtml(item.selected_action_id)+'</span><small>'+escapeHtml(reuseClassLabel(item.reuse_class))+' · 边际收益 '+formatMetric(item.marginal_impact?.confirmed_requirement_unit_volume_gain,'m³·单元')+'</small></div>').join('');
  const objectiveRows=[];for(const route of result.after?.routes||[])for(const subsystem of route.subsystems||[])for(const item of subsystem.objective_results||[])objectiveRows.push('<div class="list-row"><span>'+escapeHtml(route.route_id)+' '+escapeHtml(subsystem.subsystem)+' · '+escapeHtml(objectiveLabel(item.objective))+'</span><small>实际值 '+(item.actual==null?'—':Number(item.actual).toFixed(3))+' · '+escapeHtml(statusText(item.status))+'</small></div>');
  return '<div class="coverage-card"><b>'+escapeHtml(statusText(result.status||'unknown'))+' · 仅提案（未应用）</b><span>目标 '+(result.target_voxel_count||0)+' · 已选动作 '+(result.selected_actions||[]).length+' · 已确认单位体积收益 '+formatMetric(result.confirmed_requirement_unit_volume_gain,'m³·单元')+'</span><span>残余 '+(result.residual_confirmed_targets||[]).length+' · 待补证据 '+(result.unknown_evidence_required||[]).length+' · '+escapeHtml(stopReasonLabel(result.stop_reason))+'</span><small>仅提案；既有 CNS 设施、走廊评估与能力缺口结果均未修改，需用户确认后另行应用。</small></div><h4>已选动作与迭代收益</h4>'+(selected||'<div class="empty-note">没有确认缺口边际改善为正的可行动作</div>')+trace+'<h4>规划目标前后对比</h4>'+(objectiveRows.join('')||'<div class="empty-note">未配置已确认的规划目标</div>');
}

/** 停止原因 raw 值 → 中文（未登记取值原样显示，绝不编造）。 */
function stopReasonLabel(value){
  const key=String(value??'').trim();
  if(!key)return '未给出停止原因';
  if(key==='target_resolved')return '目标已闭合';
  if(key==='no_eligible_action')return '没有可用动作';
  if(key==='evidence_insufficient')return '证据不足，需补充证据';
  if(key==='iteration_limit_reached')return '达到迭代上限';
  return key;
}

function formatMetric(value,unit){return Number.isFinite(value)?value.toFixed(1)+' '+unit:'—';}

/** 参数来源 raw 值 → 中文（`user_configuration` 等内部来源标识不直接展示给用户）。 */
function parameterSourceLabel(value){
  const key=String(value??'').trim();
  if(!key)return '来源未标明';
  if(key==='user_configuration')return '用户配置';
  if(key==='system_default')return '系统默认值';
  return sourceModeText(key);
}

/** 设备目录来源 raw 值 → 中文。 */
function deviceCatalogSourceLabel(value){
  const key=String(value??'').trim();
  if(!key)return '未声明来源';
  return sourceModeText(key);
}

function equipmentReferencePanel(flow){
  const catalog=flow.equipment_reference_catalog||{},items=catalog.items||[];
  const rows=items.map(item=>'<div class="coverage-card equipment-reference-card"><b>'+escapeHtml(item.name)+' <span>'+escapeHtml(item.equipment_id)+'</span></b><span>'+escapeHtml(item.manufacturer||'厂商待确认')+(item.model?' · '+escapeHtml(item.model):'')+' · '+escapeHtml((item.subsystems||[]).join('/')||'非CNS规划设备')+'</span><small>技术：'+escapeHtml((item.technology||[]).join('、')||'未标')+'；来源：'+escapeHtml(item.source?.file||'未登记')+' '+escapeHtml(item.source?.locator||'')+'</small><small>规划映射状态：'+escapeHtml(planningMappingLabel(item.planning_mapping?.status))+'</small></div>').join('');
  return '<h3>真实设备资料库 '+wbBadge(catalog.status||'not_calculated','未计算')+'</h3><div class="parameter-note">真实设备资料库是来源事实模型，与当前算法的设备目录分离。下列参数只读，不完整记录不会自动补半径、MTBF、MTTR、成本或容量，也不会自动参与覆盖评估与设施规划。</div><div class="scroll-list equipment-reference-list">'+(rows||'<div class="empty-note">尚无真实设备参考记录</div>')+'</div>';
}

/** 设备资料的规划映射状态 → 中文（未登记取值原样显示，绝不编造）。 */
function planningMappingLabel(value){
  const key=String(value??'').trim();
  if(!key||key==='not_mapped')return '尚未映射到规划参数';
  if(key==='mapped')return '已映射';
  if(key==='partial')return '部分映射';
  return statusText(key);
}

// ---- 阻塞项与工程假设（只读判定，不改变任何 gate） ---------------------------
// 这里只把 flow 里已经存在的事实翻译成"为什么还不能推进"，绝不新增校验、
// 也绝不把缺失数据自动补成默认值。

/** 已有 CNS 设施：事实掌握情况与规划模式是否已声明。 */
function existingBlockerItems(existing){
  const source=existing||{};
  const knowledge=source.knowledge_status,planning=source.planning_mode;
  if(!knowledge&&!planning){
    return [{text:'既有 CNS 设施的"事实掌握情况"与"规划模式"尚未声明',
      detail:'请声明掌握情况（已确认无 / 已确认存在）以及规划模式（按事实数据规划 / 按空既有设施工程基线规划）。'}];
  }
  if(!knowledge){
    return [{text:'"事实掌握情况"尚未声明',detail:'缺少 knowledge_status，无法判断既有设施是否已掌握。'}];
  }
  if(!planning){
    return [{text:'"规划模式"未配置',detail:'缺少 planning_mode；在未配置前不推断按空基线还是按事实数据规划。'}];
  }
  if(planning==='assume_empty_for_planning'){
    return [{kind:'assumption',text:ASSUME_EMPTY_BASELINE_NOTE,
      detail:'规划模式 = 按空既有设施工程基线规划；这是工程假设，不是对现实事实的判断。'}];
  }
  return [];
}

/** 监视能力是否被显式要求成为阻塞项（默认 OPTIONAL）。 */
function radarRequirementDeclared(flow){
  const requirement=(flow.required_cns||{}).project_default||{},surveillance=requirement.surveillance||{};
  if(surveillance.required===true)return true;
  const coverage=surveillance.coverage_requirement;
  if(coverage!==null&&coverage!==undefined&&coverage!=='')return true;
  const performance=surveillance.performance||{};
  for(const key of ['min_detection_range_m','min_detection_probability','max_update_interval_s','max_track_loss_s']){
    const value=performance[key];
    if(value!==null&&value!==undefined&&value!=='')return true;
  }
  return false;
}

/** 监视雷达分段是否是阻塞项（默认不阻塞）。 */
export function radarBlocksNextStep(flow){
  const source=flow||{};
  if(radarRequirementDeclared(source))return true;
  const policy=source.radar_surveillance_policy||{};
  return policy.surveillance_required===true||policy.required===true;
}

/** 监视雷达规划：默认可选，因此没有阻塞项时给出明确说明（不写"通过"）。 */
function radarBlockerItemsFor(flow){
  if(radarBlocksNextStep(flow)){
    return [{text:'监视能力已被需求或监视政策显式要求',
      detail:'因此雷达监视规划的划设结果需要人工复核；未完成前不要把它当作已确认结果。'}];
  }
  return [{kind:'assumption',
    text:'监视雷达规划默认是可选的：没有显式监视需求时不阻塞下一步',
    detail:'只有需求或监视政策明确要求监视能力时，它才成为阻塞项；本分支不写入正式规划结果。'}];
}

/** 下一步栏：说明推进方向，并在不能推进时给出真实原因。 */
function stepNext(flow,segments){
  const enabled=Boolean(flow.steps&&flow.steps['5']);
  return nextStepBar({
    enabled,
    label:'下一步：方案评审',
    reason:enabled?'':(segments||[]).map(item=>item.text).join('；')||'当前步骤尚未完成，暂不能进入方案评审。',
    note:enabled?'生产链结果已生成，可进入方案评审。':''
  });
}

export function render({flow}){
  // 两层卡片式布局：名称 + 弱化 role（第一行）、R/MTBF 两个输入各占半宽（第二行）。
  // data-device-radius / data-device-mtbf 的索引契约保持不变，collect() 无需改动。
  const devices=(flow.devices||[]).map((device,index)=>'<div class="device-row"><b class="device-name">'+escapeHtml(device.subsystem)+' · '+escapeHtml(device.model||device.name||device.device_id)+'</b><span class="device-role">'+escapeHtml(device.role||'')+'</span><label class="device-field device-field-radius">R(m)<input type="number" data-device-radius="'+index+'" value="'+device.radius_m+'"></label><label class="device-field device-field-mtbf">MTBF(h)<input type="number" data-device-mtbf="'+index+'" value="'+(device.mtbf_h||device.mtbf)+'"></label></div>').join('');
  const legacyCoverage=flow.compatibility_coverage||flow.coverage||{};
  let result='<div class="empty-note">尚未运行旧版二维覆盖试算</div>';
  if(legacyCoverage.status&&legacyCoverage.status!=='not_calculated')result=Object.entries(legacyCoverage.layers||{}).map(([key,layer])=>{const stats=layer.statistics;return '<div class="coverage-card"><b>'+key+' '+wbBadge(layer.status)+'</b><span>站点 '+stats.stations+' · 主站 '+stats.primary+' · 补盲 '+stats.gap+' · 共址 '+stats.colocated+'</span><span>平均重数 '+stats.average_multiplicity+' · 未覆盖 '+stats.uncovered_samples+'</span></div>';}).join('');
  const engineeringDefaults=(flow.defaults||{}).engineering_parameters||{};
  const params={...engineeringDefaults,primary_spacing_factor:engineeringDefaults.primary_spacing_factor||{value:'—',source:'未配置来源'},co_location_search_radius_m:engineeringDefaults.co_location_search_radius_m||{value:'—'}};
  const risks=flow.risks||{},riskState=key=>statusText((risks[key]||{}).status);
  const existing=flow.existing_cns_facilities||{},candidates=flow.candidate_sites||{},colocation=flow.tower_colocation_candidates||{},catalog=flow.device_catalog||{},gaps=flow.cns_gap_analysis||{},coverage3d=flow.coverage_3d||{},capability=flow.cns_service_capability||{},corridor=flow.cns_corridor_assessment||{},corridorGap=flow.cns_corridor_gap_assessment||{},corridorSitePolicy=flow.corridor_site_planning_policy||{},corridorSitePlan=flow.cns_corridor_site_plan||{},timeline=flow.service_timeline||{},protection=flow.protection_envelope||{},gapV2=flow.cns_gap_analysis_v2||{},sitePolicy=flow.site_planning_policy||{},sitePlan=flow.compatibility_cns_site_plan||flow.cns_site_plan||{},closedLoop=flow.compatibility_closed_loop_assessment||flow.closed_loop_assessment||{};
  const devicesWithParams=(flow.devices||[]).filter(device=>Number.isFinite(Number(device.radius_m))||Number.isFinite(Number(device.mtbf_h??device.mtbf)));

  // ---- 阻塞项与工程假设（每个分段各自成立，互不代替） -------------------------
  const deviceBlockers=devicesWithParams.length
    ?[{kind:'assumption',text:'设备作用半径与 MTBF 是工程假设输入，不是已确认的设备性能',
      detail:'传播、视距、绕射、干扰与负载均未评估；结果只用于规划口径比较。'}]
    :[{text:'尚未配置任何设备的作用半径 R 与 MTBF',
      detail:'缺少这两个工程参数时，三维覆盖与服务能力没有可用的设备口径。'}];
  const existingBlockers=existingBlockerItems(existing);
  const candidateBlockers=[];
  if(!(candidates.items||[]).length)candidateBlockers.push({text:'候选站址尚未导入',
    detail:'可从已有设施派生，或先导入候选站址文件。'});
  if(!((colocation.policy||{}).planning_host_use_confirmed===true))candidateBlockers.push({kind:'assumption',
    text:'共塔规划宿主尚未允许（规划层）',
    detail:'未允许时共塔候选不参与规划；物理安装层恒为"未核实，需现场勘察"。'});
  const coverageBlockerItems=[];
  if(!(flow.operational_routes||[]).length)coverageBlockerItems.push({text:'尚未确认正式运行航路',
    detail:'三维覆盖按正式运行航路采样；没有航路时无法评估。'});
  if(!((flow.spatial_3d||{}).altitude_layers||[]).length)coverageBlockerItems.push({text:'高度层目录尚不可用',
    detail:'三维覆盖需要已确认的高度层定义。'});
  const corridorBlockerItems=[(coverage3d.status&&coverage3d.status!=='not_calculated')
    ?{kind:'assumption',text:'服务走廊按三维几何覆盖口径构建，不包含传播与干扰评估',
      detail:'体积为离散体积代理，不是法规批准空间。'}
    :{text:'尚未生成三维几何覆盖结果',
      detail:'服务走廊依赖三维几何覆盖口径；请先运行三维覆盖评估。'}];
  const gapBlockerItems=[(corridor.status&&corridor.status!=='not_calculated')
    ?{kind:'assumption',text:'能力缺口按"规划目标与服务走廊对比"判定，不代表运行中断或安全事件',
      detail:'证据不足的体元单独统计，绝不算作满足。'}
    :{text:'尚未生成 CNS 服务走廊结果',
      detail:'能力缺口评估需要当前有效的服务走廊结果。'}];
  const siteBlockerItems=[corridorSitePolicy.confirmed===true
    ?{kind:'assumption',text:'走廊复用优先规划策略已确认，仅用于生成规划方案',
      detail:'方案本身不修改已有 CNS 设施，需用户确认后另行应用。'}
    :{text:'走廊复用优先规划策略尚未确认',
      detail:'未确认时不会生成 CNS 设施规划方案。'}];
  const radarBlockerItems=radarBlockerItemsFor(flow);
  const stepBlockers=siteBlockerItems.concat(coverageBlockerItems);

  // ---- 操作：设备与参数 / 已有设施 / 候选站址 --------------------------------
  const deviceCatalogNote='<div class="demo-note">设备目录：'+escapeHtml(deviceCatalogSourceLabel(catalog.source||flow.device_source))+' · '+(catalog.count||0)+' 型设备</div>';
  const engineeringParameters='<div class="parameter-note">主站间距 '+params.primary_spacing_factor.value+'R · 共址半径 '+params.co_location_search_radius_m.value+'m<br>参数来源：'+escapeHtml(parameterSourceLabel(params.primary_spacing_factor.source))+'</div>';
  const deviceActions='<div class="device-list">'+devices+'</div><div class="button-row"><button class="secondary" id="saveDevices">保存设备参数</button></div>';  const existingPanel='<h3>已有 CNS 设施 '+wbBadge(existing.status||'not_calculated','未计算')+'</h3>'
    +existingCnsStatusRows(existing)
    +'<div class="panel-file-input"><input class="panel-input" id="existing_cnsPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseExisting">选择…</button></div><button class="secondary full" id="importExisting">导入已有设施</button><div class="scroll-list cns-input-list">'+collectionList(existing,'facility')+'</div>';
  const candidatePanel='<h3>候选站址 '+wbBadge(candidates.status||'not_calculated','未计算')+'</h3><div class="panel-file-input"><input class="panel-input" id="candidate_sitesPath" placeholder="JSON / CSV / GeoJSON"><button class="secondary" id="browseCandidates">选择…</button></div><div class="button-row"><button class="secondary" id="importCandidates">导入候选站址</button><button class="secondary" id="deriveCandidates">从已有设施生成</button></div><div class="scroll-list cns-input-list">'+collectionList(candidates,'candidate')+'</div>'
    +'<h3>共塔候选（真实铁塔宿主） '+wbBadge(colocation.status||'not_calculated','未计算')+'</h3>'
    +towerColocationPolicyForm(flow)
    +'<div class="scroll-list cns-input-list">'+towerColocationList(colocation)+'</div>';

  // ---- 结果：canonical 生产链（三维覆盖 → 服务能力 → 服务走廊 → 能力缺口 → 设施规划） ----
  const coverage3dPanel='<h3>三维几何覆盖评估 '+wbBadge(coverage3d.status||'not_calculated','未计算')+'</h3><div class="parameter-note">几何覆盖 ≠ 真实 CNS 性能；传播、视距、绕射、干扰、链路预算和传感器探测概率均未评估。</div><label>采样间距（米）<input class="panel-input" type="number" id="coverage3dSpacing" value="'+(coverage3d.parameters?.sample_spacing_m||flow.algorithm_selection?.coverage_model?.parameters?.sample_spacing_m||500)+'"></label><div class="button-row"><button class="primary" id="evaluateCoverage3d">运行三维几何覆盖</button></div><div class="gap-results">'+coverage3dList(coverage3d)+'</div>';
  const capabilityPanel='<h3>CNS 服务能力评估 '+wbBadge(capability.status||'not_calculated','未计算')+'</h3><div class="parameter-note">静态能力满足不等于当前服务可用；视距、绕射、干扰、负载和切换等尚未评估。</div><button class="secondary full" id="evaluateServiceCapability">评估 CNS 服务能力</button><div class="gap-results">'+capabilityList(capability)+'</div>';
  const corridorPanel='<h3>CNS 服务走廊 '+wbBadge(corridor.status||'not_calculated','未计算')+'</h3><div class="parameter-note">这是工程 CNS 服务需求走廊，不是法规批准空间；水平范围采用保守网格纳入，体积为离散体积代理。</div><button class="secondary full" id="evaluateCorridor">评估 CNS 服务走廊</button><div class="gap-results">'+corridorSummary(corridor)+'</div>';
  const objectivesGapPanel=planningObjectivesPanel(flow)+'<div class="parameter-note">空间连续缺口是服务走廊体元的保守纵向投影，不是运行中断、正式 ICAO 连续性或可用度概率。</div><div class="gap-results">'+corridorGapSummary(corridorGap)+'</div>';
  const corridorSitePlanPanel='<h3>CNS 设施规划 '+wbBadge(corridorSitePlan.status||'not_calculated','未计算')+'</h3><div class="parameter-note">仅针对已确认的走廊缺口目标，通过累计试算验证服务与独立冗余收益；证据不足不会触发建站。</div><label class="check-row"><input type="checkbox" id="corridorSitePolicyConfirmed" '+(corridorSitePolicy.confirmed?'checked':'')+'> 确认走廊复用优先规划策略</label><button class="secondary full" id="evaluateCorridorSitePlan">生成 CNS 设施规划方案</button><div class="gap-results">'+corridorSitePlanSummary(corridorSitePlan)+'</div>';

  // ---- 雷达监视规划：独立 production 分支，默认 OPTIONAL -----------------------
  const radarPanel='<h3>雷达监视规划 '+wbBadge(flow.radar_surveillance_layout?.status||'not_calculated','未计算')+'</h3>'
    +'<div class="parameter-note">本分支<b>默认是可选的</b>：没有显式监视需求时，雷达监视规划不阻塞下一步，其结果也只是候选划设方案，不构成"正式结果已采纳"。</div>'
    +advancedAuditNote('雷达监视规划来自独立的雷达划设任务卡（算法标识 radar_surveillance_layout_v1）；卡片内部的算法版本号、就绪状态与挂高历史字段等术语均为工程内部标识。卡片同时原样回显后端字段取值（例如 unverified / eligible / confirmed），用户可读解释见括号内中文或下方阻塞项说明。')
    +renderRadarSurveillanceLayoutPanel(flow);

  // ---- 高级：兼容分支与闭环（全部不参与正式规划门禁） -------------------------
  // 兼容声明逐字展示（含引号），因此这里不做 HTML 转义：
  // 常量本身是固定的中文短语，不含任何用户输入。
  const compatDeclare=kind=>'<div class="parameter-note">'+kind+'：'+COMPATIBILITY_NOTE+'</div>';
  const coverageResult='<h3>旧版二维覆盖试算（不用于正式规划） '+wbBadge(legacyCoverage.status||'not_calculated','未计算')+'</h3>'+compatDeclare('旧版二维覆盖试算')+'<div class="parameter-note">旧版兼容结果仅保存在当前会话，不写入项目状态，不决定正式规划是否完成。</div><button class="secondary full" id="planCoverage">运行旧版二维覆盖试算</button><div class="coverage-results">'+result+'</div>';
  const gapPanel='<h3>旧版运行航路缺口分析（不用于正式规划） '+wbBadge(gaps.status||'not_calculated','未计算')+'</h3>'+compatDeclare('旧版运行航路缺口分析')+'<button class="secondary full" id="analyzeGaps">分析当前运行航路缺口</button><div class="gap-results">'+gapList(gaps)+'</div>';
  const sitePlanPanel='<h3>旧版站址试算（不用于正式规划） '+wbBadge(sitePlan.status||'not_calculated','未计算')+'</h3>'+compatDeclare('旧版站址试算')+'<div class="parameter-note">旧版兼容试算仅保存在当前会话，不写入项目状态，不解锁方案评审。复用层级顺序：Existing CNS → Existing Shared Site → Tower Colocation Host（真实铁塔共塔宿主）→ Candidate Site → New-build Candidate；该顺序表示 prefer 共塔而不是 force。</div><label class="check-row"><input type="checkbox" id="sitePolicyConfirmed" '+(sitePolicy.confirmed?'checked':'')+'> 确认旧版复用优先试算策略</label><button class="secondary full" id="evaluateSitePlan">运行旧版站址试算</button><div class="gap-results">'+sitePlanSummary(sitePlan)+'</div>';
  const timelinePanel='<h3>C/N/S 服务时间线 '+wbBadge(timeline.status||'not_calculated','未计算')+'</h3><div class="parameter-note">运行状态只来自显式服务场景事件；不从静态能力结论、可靠性规格或 MTBF 推断可用与中断。</div><button class="secondary full" id="evaluateServiceTimeline">生成服务时间线</button><div class="gap-results">'+timelineList(timeline)+'</div>';
  const protectionPanel='<h3>战术保护包络 '+wbBadge(protection.status||'not_calculated','未计算')+'</h3>'+advancedAuditNote('工程战术保护包络属于高级分析面（P15）；它对应开发阶段编号，仅在此高级标签中展示。')+'<div class="parameter-note">工程保护距离 ≠ 法规 Well-Clear / 正式 DAA Detection Volume。</div><button class="secondary full" id="evaluateProtectionEnvelope">计算工程保护距离</button><div class="gap-results">'+protectionSummary(protection)+'</div>';
  const gapV2Panel='<h3>保护与缺口分析 '+wbBadge(gapV2.status||'not_calculated','未计算')+'</h3>'+advancedAuditNote('本分段合并 P7 几何覆盖、P8 静态能力与 P9 运行时间线（开发阶段编号，仅高级区展示）。')+'<div class="parameter-note">Unknown 表示证据不足，不是危险等级，Gap 也不自动触发 Safety Event。</div><label class="check-row"><input type="checkbox" id="gapV2Protection" '+(gapV2.parameters?.evaluate_protection_margin?'checked':'')+'> 可选工程保护余量（非 Well-Clear/认证判断）</label><button class="secondary full" id="evaluateGapV2">运行保护与缺口分析</button><div class="gap-results">'+gapV2List(gapV2)+'</div>';
  const closedLoopPanel='<h3>高级：方案影响试算 '+wbBadge(closedLoop.status||'not_calculated','未计算')+'</h3>'+compatDeclare('方案影响试算')+'<div class="parameter-note">只在工作副本比较方案前后影响；复核操作不会写入正式覆盖、设施、确认方案或运行航路。</div>'+advancedAuditNote('闭环复核对应开发阶段 P11 / P12（预测与实测对比），仅在此高级标签中展示。')+'<div class="button-row"><button class="secondary" id="evaluateClosedLoop">生成影响试算</button><button class="secondary" id="applyClosedLoop" '+(closedLoop.validation_status==='validated_improvement'&&closedLoop.commit_status==='preview'?'':'disabled')+'>复核试算结果（不写入正式项目）</button></div><div class="gap-results">'+closedLoopSummary(closedLoop)+'</div>';

  const body=wbPanel('operate','',{segments:[
    ['cns-op-devices','设备与参数',
      wbBlock('设备与参数',wbSegHint(OPERATE_SEGMENTS,'cns-op-devices')
        +segIntro('准备设备型号、作用半径与 MTBF，形成可复算的布站工程基线。','设备资料库（只读事实）与两台工程参数输入框。')
        +deviceCatalogNote+equipmentReferencePanel(flow)+engineeringParameters)
      +wbBlock('阻塞项与工程假设',blockerList(deviceBlockers,'当前没有阻塞项'))
      +wbBlock('设备参数与布站',deviceActions)
      +nextHint('保存设备参数后进入「已有设施」，声明既有设施的事实掌握情况与规划模式。')],
    ['cns-op-existing','已有设施',
      wbBlock('已有设施',wbSegHint(OPERATE_SEGMENTS,'cns-op-existing')
        +segIntro('掌握既有 CNS 设施的事实情况，并显式声明后续规划采用哪种模式。','已有设施数据文件路径；两条状态都来自后端字段，缺失时如实显示"尚未声明 / 未配置"。')
        +existingPanel)
      +wbBlock('阻塞项与工程假设',blockerList(existingBlockers,'当前没有阻塞项'))
      +nextHint('声明完成后进入「候选站址」，准备候选站址与共塔宿主。')],
    ['cns-op-candidates','候选站址',
      wbBlock('候选站址',wbSegHint(OPERATE_SEGMENTS,'cns-op-candidates')
        +segIntro('准备候选站址与共塔规划宿主，供后续设施规划复用。','候选站址文件路径，或由已有设施派生；共塔候选需要真实铁塔与规划宿主允许。')
        +candidatePanel)
      +wbBlock('阻塞项与工程假设',blockerList(candidateBlockers,'当前没有阻塞项'))
      +nextHint('候选准备完成后进入结果标签的「三维覆盖评估」，开始生产链评估。')]
  ]})
    +wbPanel('result','',{segments:[
      ['cns-res-coverage','三维覆盖评估',
        wbBlock('三维覆盖评估',wbSegHint(RESULT_SEGMENTS,'cns-res-coverage')+chainNote('三维覆盖')
          +segIntro('建立正式运行航路的三维几何覆盖事实基线。','已确认高度层目录与正式运行航路；采样间距为工程假设。')
          +coverage3dPanel)
        +wbBlock('阻塞项与工程假设',blockerList(coverageBlockerItems,'当前没有阻塞项；几何覆盖不作为后续步骤的门禁'))
        +nextHint(chainNext('cns-res-coverage'))],
      ['cns-res-capability','服务能力评估',
        wbBlock('服务能力评估',wbSegHint(RESULT_SEGMENTS,'cns-res-capability')+chainNote('服务能力')
          +segIntro('在几何覆盖基线之上评估 C / N / S 静态服务能力满足情况。','设备作用半径与 MTBF；几何覆盖结果作为口径基线。')
          +capabilityPanel)
        +wbBlock('阻塞项与工程假设',blockerList(deviceBlockers,'当前没有阻塞项'))
        +nextHint(chainNext('cns-res-capability'))],
      ['cns-res-corridor','CNS 服务走廊',
        wbBlock('CNS 服务走廊',wbSegHint(RESULT_SEGMENTS,'cns-res-corridor')+chainNote('服务走廊')
          +segIntro('把服务能力转成沿航路的工程服务走廊与缺口体元。','三维几何覆盖结果与已确认的高度层定义。')
          +corridorPanel)
        +wbBlock('阻塞项与工程假设',blockerList(corridorBlockerItems,'当前没有阻塞项'))
        +nextHint(chainNext('cns-res-corridor'))],
      ['cns-res-gap','CNS 能力缺口',
        wbBlock('CNS 能力缺口',wbSegHint(RESULT_SEGMENTS,'cns-res-gap')+chainNote('能力缺口')
          +segIntro('按已确认的规划目标判定服务走廊中的能力缺口。','当前有效的服务走廊结果；规划目标需要显式确认。')
          +objectivesGapPanel)
        +wbBlock('阻塞项与工程假设',blockerList(gapBlockerItems,'当前没有阻塞项'))
        +nextHint(chainNext('cns-res-gap'))],
      ['cns-res-site','CNS 设施规划',
        wbBlock('CNS 设施规划',wbSegHint(RESULT_SEGMENTS,'cns-res-site')+chainNote('设施规划')
          +segIntro('针对已确认的走廊缺口目标生成复用优先的设施规划方案。','已确认的能力缺口目标与走廊复用优先规划策略。')
          +corridorSitePlanPanel)
        +wbBlock('阻塞项与工程假设',blockerList(siteBlockerItems,'当前没有阻塞项'))
        +nextHint(chainNext('cns-res-site'))],
      ['cns-res-radar','雷达监视规划',
        wbBlock('雷达监视规划',wbSegHint(RESULT_SEGMENTS,'cns-res-radar')
          +segIntro('在监视能力被显式要求或需要工程对照时，给出雷达布站的候选划设方案。','真实铁塔障碍物事实、陆域图层与可用的雷达设备资料。')
          +radarPanel)
        +wbBlock('阻塞项与工程假设',blockerList(radarBlockerItems,'当前没有阻塞项；雷达监视规划默认可选'))
        +nextHint(radarBlocksNextStep(flow)
          ?'监视能力已被显式要求：请人工复核雷达监视规划结果后，再进入下一步。'
          :'本分支可选：无论是否运行雷达监视规划，都不影响进入下一步。')]
    ]})
    +wbPanel('advanced','',{segments:[
      ['cns-adv-timeline','运行时间线',wbBlock('运行时间线',wbSegHint(ADVANCED_SEGMENTS,'cns-adv-timeline')+timelinePanel)],
      ['cns-adv-gapv2','保护与缺口分析',wbBlock('保护与缺口分析',wbSegHint(ADVANCED_SEGMENTS,'cns-adv-gapv2')+protectionPanel+gapV2Panel)],
      ['cns-adv-compat','旧版兼容试算',wbBlock('旧版兼容试算',wbSegHint(ADVANCED_SEGMENTS,'cns-adv-compat')+coverageResult+gapPanel+sitePlanPanel)],
      ['cns-adv-closedloop','高级方案影响试算',wbBlock('高级方案影响试算',wbSegHint(ADVANCED_SEGMENTS,'cns-adv-closedloop')+closedLoopPanel)
        +'<div class="flow-summary">生命风险：'+riskState('life')+' · 财产风险：'+riskState('property')+'<br>高级与旧版兼容结果均不用于正式规划门禁。</div>']
    ]});
  return shell('05','CNS规划','生产链：三维覆盖 → 服务能力 → 服务走廊 → 能力缺口 → 设施规划；雷达监视规划为可选分支。',body+stepNext(flow,stepBlockers));
}

export function bind(c){
  const collect=()=>c.flow().devices.map((device,index)=>({...device,radius_m:Number(document.querySelector('[data-device-radius="'+index+'"]').value),mtbf:Number(document.querySelector('[data-device-mtbf="'+index+'"]').value)}));
  c.actionButton('saveDevices',()=>c.mutate('devices',{devices:collect()}));
  c.actionButton('planCoverage',async()=>{await c.mutate('devices',{devices:collect()});await c.mutate('coverage');});
  c.$('browseExisting').onclick=()=>c.openBrowser('existing_cns',c.$('existing_cnsPath').value);
  c.$('browseCandidates').onclick=()=>c.openBrowser('candidate_sites',c.$('candidate_sitesPath').value);
  c.actionButton('importExisting',()=>c.resourceAction('/api/existing-cns/import',{path:c.$('existing_cnsPath').value.trim()}));
  c.actionButton('importCandidates',()=>c.resourceAction('/api/candidate-sites/import',{path:c.$('candidate_sitesPath').value.trim()}));
  c.actionButton('deriveCandidates',()=>c.resourceAction('/api/candidate-sites/from-existing',{}));
  // 真实铁塔 → 共塔宿主候选 + 塔顶障碍物事实（一次显式动作；不生成任何设备参数）。
  c.actionButton('deriveTowerColocation',()=>c.resourceAction('/api/tower-obstacle-profiles/evaluate',{}));
  // 策略确认：复用同一个端点，payload 携带 tower_colocation_policy（不新增端点/契约）。
  // 这里**只**确认规划层（planning_host_use_confirmed）；物理安装层没有可确认的输入。
  if(c.$('saveTowerColocationPolicy'))c.actionButton('saveTowerColocationPolicy',()=>c.resourceAction('/api/tower-obstacle-profiles/evaluate',{
    tower_colocation_policy:{
      service_origin_assumption:c.$('towerColocationOrigin').value||null,
      planning_host_use_confirmed:c.$('towerColocationPlanningHost').checked,
      source:c.$('towerColocationSource').value.trim()||'user_configuration',
    },
  }));
  c.actionButton('analyzeGaps',()=>c.mutate('gap-analysis'));
  c.actionButton('evaluateCoverage3d',()=>c.resourceAction('/api/coverage-3d/evaluate',{parameters:{sample_spacing_m:Number(c.$('coverage3dSpacing').value),assumption:'user_engineering_sampling_assumption',confirmed:false}}));
  c.actionButton('evaluateServiceCapability',()=>c.resourceAction('/api/cns-service-capability/evaluate',{}));
  c.actionButton('evaluateCorridor',()=>c.resourceAction('/api/cns-service-corridor/evaluate',{}));
  c.actionButton('savePlanningObjectives',()=>{const policy=structuredClone(c.flow().cns_planning_objectives||{routes:{}}),routeId=c.$('planningObjectiveRoute').value,code=c.$('planningObjectiveSubsystem').value,confirmed=c.$('planningObjectiveConfirmed').checked,source=c.$('planningObjectiveSource').value.trim()||'user_configuration',specs={min_satisfied_volume_fraction:['objectiveMinSatisfied','>='],max_confirmed_deficit_volume_fraction:['objectiveMaxDeficit','<='],max_unknown_volume_fraction:['objectiveMaxUnknown','<='],min_redundancy_satisfied_volume_fraction:['objectiveMinRedundancy','>='],max_continuous_deficit_projection_m:['objectiveMaxContinuous','<=']};policy.routes=policy.routes||{};policy.routes[routeId]=policy.routes[routeId]||{route_id:routeId,subsystems:{}};policy.routes[routeId].subsystems=policy.routes[routeId].subsystems||{};policy.routes[routeId].subsystems[code]={objectives:Object.fromEntries(Object.entries(specs).filter(([,value])=>c.$(value[0]).value!=='').map(([name,[id,operator]])=>[name,{value:Number(c.$(id).value),operator,source,confirmed}]))};return c.resourceAction('/api/cns-planning-objectives',{cns_planning_objectives:policy});});
  c.actionButton('evaluateCorridorGap',()=>c.resourceAction('/api/cns-corridor-gap/evaluate',{}));
  c.actionButton('evaluateCorridorSitePlan',()=>{const policy=structuredClone(c.flow().corridor_site_planning_policy||{});policy.confirmed=c.$('corridorSitePolicyConfirmed').checked;policy.source='user_configuration';return c.resourceAction('/api/cns-corridor-site-plan/evaluate',{corridor_site_planning_policy:policy});});
  c.actionButton('evaluateServiceTimeline',()=>c.resourceAction('/api/service-timeline/evaluate',{}));
  c.actionButton('evaluateProtectionEnvelope',()=>c.resourceAction('/api/protection-envelope/evaluate',{}));
  c.actionButton('evaluateGapV2',()=>c.resourceAction('/api/cns-gap-analysis-v2',{parameters:{evaluate_protection_margin:c.$('gapV2Protection').checked}}));
  c.actionButton('evaluateSitePlan',()=>{const policy=structuredClone(c.flow().site_planning_policy||{});policy.confirmed=c.$('sitePolicyConfirmed').checked;policy.source='user_configuration';return c.resourceAction('/api/cns-site-plan',{site_planning_policy:policy});});
  c.actionButton('evaluateClosedLoop',()=>c.resourceAction('/api/cns-closed-loop/evaluate',{}));
  c.actionButton('applyClosedLoop',()=>c.resourceAction('/api/cns-closed-loop/apply',{application_id:c.flow().closed_loop_assessment?.application?.application_id}));
  // ---- Radar Surveillance Layout V1.1（独立任务卡；proposal-only，绝不自动 Apply） ----
  // 保存划设参数：只写 radar_surveillance_policy（25/5/3 + 海岸不确定带 + 陆域图层）。
  // V1.1：挂高不再是必填项；legacy 输入框是 disabled 的只读回显，这里绝不提交它。
  if(c.$('saveRadarSurveillancePolicy'))c.actionButton('saveRadarSurveillancePolicy',()=>{
    const policy=structuredClone(c.flow().radar_surveillance_policy||{});
    const buffer=c.$('radarCoastalBuffer')?.value.trim();
    if(buffer!==''&&buffer!=null)policy.coastal_uncertainty_buffer_m=Number(buffer);
    const layer=c.$('radarLandMaskLayer')?.value.trim();
    if(layer)policy.land_mask_layer_name=layer;
    policy.allow_mixed_radar_types=c.$('radarAllowMixed')?.checked!==false;
    return c.resourceAction(RADAR_POLICY_ENDPOINT,policy);
  });
  // 运行初步划设：一次显式动作 = 一次两阶段 MILP + 5 m 独立连续覆盖复核。
  if(c.$('evaluateRadarSurveillanceLayout'))c.actionButton('evaluateRadarSurveillanceLayout',()=>{
    const payload={route_id:'all'};
    const buffer=c.$('radarCoastalBuffer')?.value.trim();
    if(buffer!==''&&buffer!=null)payload.coastal_uncertainty_buffer_m=Number(buffer);
    const layer=c.$('radarLandMaskLayer')?.value.trim();
    if(layer)payload.land_mask_layer_name=layer;
    return c.resourceAction(RADAR_LAYOUT_EVALUATE_ENDPOINT,payload);
  });
  if(c.$('nextStep'))c.$('nextStep').onclick=()=>c.setStep(6);
}
