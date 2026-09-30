// =========================================================
// Step05 CNS service business presentation（Round 3）
//
// 本模块只做**前端业务呈现**：
//  * 陆海分类事实（surface classification policy + facts）；
//  * Communication / RID **工程规划 Profile** 卡片；
//  * service-aware 的 P14 服务走廊证据 / P15 能力缺口 / P16 设施规划；
//  * 前端测试所需的纯函数（词表、计数文本、同址判定）。
//
// 铁律：
//  * 所有数值（半径、要求站址数）都来自 **backend canonical 字段**
//    （``flow.device_catalog`` / ``required_cns`` / P14 / P15 / P16），
//    前端不复制第二份常量作为业务 authority；
//  * 前端**不重算**距离、重数、land/sea；
//  * 只有一个地方比较「实际站址数 vs 要求站址数」，且两侧都是后端给出的数字
//    （见 :func:`distinctSiteCountText` / :func:`requiredSiteCountText` 的注释）。
// =========================================================
import {escapeHtml} from './common.js';
import {
  ALL_REQUIRED_MODE_LABEL, CNS_FOCUS_SERVICES, CNS_GAP_COLORS, CNS_GAP_STATE_LABELS,
  CNS_ROUNDD_SERVICE_KEYS, CNS_SERVICE_AUTHORITY_NOTE, CNS_SERVICE_GEOMETRY_LEGEND_NOTE,
  CNS_SERVICE_REQUIREMENT_ROWS, NAVIGATION_BASELINE_DISCLAIMER,
  NAVIGATION_BASELINE_LEGEND_LABEL, NAVIGATION_BASELINE_MODEL_LABEL,
  NAVIGATION_DELIVERY_DEPENDENCY_LABEL, NAVIGATION_EQUIPMENT_NOT_SELECTED_NOTE,
  NAVIGATION_PLANNING_UNIT_LABEL, RADAR_DIRECTIONAL_GEOMETRY_NOTE, RADAR_TYPE_TEXT,
  REDUNDANCY_STATUS_TEXT,
  SERVICE_KEY_LABELS, SERVICE_KEY_SHORT_LABELS, SERVICE_STATUS_TEXT, SURFACE_CLASS_TEXT,
  SURVEILLANCE_DUAL_CHANNEL_LABELS, SURVEILLANCE_DUAL_CHANNEL_NOTE,
  cnsServiceFormalLabel, combinedStatusText, distinctSiteCountText, gapStateText,
  isRounddServiceKey, isSurfaceAwareServiceKey, navigationBaselineText,
  navigationDeliveryStatusText, navigationGapCauseText, navigationGeometryStatusText,
  navigationPlanningReadinessText, navigationReasonText, navigationSiteSourceText,
  navigationSuitabilityOf as navigationSuitabilityOfValue,
  plannerFamilyLabel, redundancyStatusText, requiredSiteCountText,
  sameDistinctSite, serviceKeyLabel, serviceStatusText, subsystemServiceLabel, surfaceClassText,
  vocabularyText,
} from '../map/service_semantics.js';

/** 端点（与后端 router 登记一致；本模块不新增任何端点）。 */
export const SURFACE_CLASSIFICATION_POLICY_ENDPOINT = '/api/surface-classification-policy';
export const SURFACE_CLASS_FACTS_EVALUATE_ENDPOINT = '/api/surface-class-facts/evaluate';
export const CNS_SERVICE_CORRIDOR_ENDPOINT = '/api/cns-service-corridor';

/** 格心代表点语义：必须逐字显示，绝不暗示连续精确海岸线。 */
export const SURFACE_FACTS_BASIS_NOTE =
  '按 L8 格心代表点分类，不代表连续精确海岸线。';

/** 工程规划基线 ≠ 厂家验证性能 ≠ 实测传播覆盖（逐字声明）。 */
export const ENGINEERING_BASELINE_DISCLAIMER =
  '工程规划基线 ≠ 厂家验证性能 ≠ 实测传播覆盖';

/** 六面未评估项（缺证据），绝不写成已评估。 */
export const NOT_EVALUATED_AREAS = [
  'RF 传播', '地形视距（terrain LOS）', '建筑遮挡（building blocking）',
  '链路预算（link budget）', '接收灵敏度（sensitivity）', '干扰（interference）',
];

export const NOT_EVALUATED_TEXT = '未评估 / 缺证据';

/** 设备事实里的 urban 半径（本轮**只**作设备事实，城区规划未启用）。 */
export const URBAN_FACT_NOTE = '城区 1 km：仅保留设备事实，当前未启用城区规划';

/** 策略变化后 facts 过期时的提示（必须明确要求"重新生成"）。 */
export const SURFACE_FACTS_STALE_NOTE =
  '分类策略已变化：已保存的陆海分类事实不再对应当前策略，需要重新生成陆海分类事实。';

// ---- 1. 陆海分类事实（Surface Facts） ---------------------------------------

/** 分类策略状态：confirmed / pending（raw enum 不直接露到生产 UI）。 */
export function surfacePolicyStatusText(value) {
  const key = String(value ?? '').trim();
  if (key === 'confirmed') return '已确认';
  if (key === 'pending_confirmation' || key === 'pending') return '待确认';
  if (key === 'missing_data') return '缺少数据';
  return key || '待确认';
}

/** Land Mask 是否已配置（只看后端给出的来源事实，不推断文件是否存在）。 */
export function landMaskConfigured(facts) {
  const landMask = facts?.land_mask || {};
  const policy = facts?.policy || {};
  return Boolean(landMask.configured_path)
    || Boolean(String(landMask.layer_name || policy.land_mask_layer_name || '').trim());
}

/** 分类策略 + 事实状态 → 业务摘要模型。 */
export function surfaceFactsModel(flow) {
  const policy = flow?.surface_classification_policy || {};
  const facts = flow?.surface_class_facts || {};
  const policySnapshot = facts.policy || policy;
  const counts = facts.surface_class_counts || {};
  const statusText = String(facts.status || 'not_calculated');
  // 策略变化 → facts stale：由后端 result_statuses 如实转印，前端不自行判断"新旧"。
  const resultStatus = flow?.result_statuses?.surface_class_facts || null;
  return {
    policy,
    facts,
    policyLandMaskLayerName: String(
      policySnapshot.land_mask_layer_name ?? policy.land_mask_layer_name ?? '',
    ) || null,
    coastalUncertaintyBufferM: Number.isFinite(Number(policySnapshot.coastal_uncertainty_buffer_m))
      ? Number(policySnapshot.coastal_uncertainty_buffer_m)
      : (Number.isFinite(Number(policy.coastal_uncertainty_buffer_m))
        ? Number(policy.coastal_uncertainty_buffer_m)
        : null),
    policyConfirmed: policySnapshot.confirmed === true || policy.confirmed === true,
    policyStatus: String(policySnapshot.status || policy.status || 'pending_confirmation'),
    landMaskConfigured: landMaskConfigured(facts),
    factsStatus: statusText,
    factsResultStatus: resultStatus,
    stale: statusText === 'stale' || resultStatus === 'stale',
    sourceIdentity: facts.land_mask?.source_identity || null,
    counts: {
      land: Number(counts.land || 0),
      sea: Number(counts.sea || 0),
      coastal_uncertain: Number(counts.coastal_uncertain || 0),
      unknown: Number(counts.unknown || 0),
    },
    gridCellCount: Number(facts.grid_cell_count || 0),
    classifiedGridCellCount: Number(facts.classified_grid_cell_count || 0),
    inputFingerprint: facts.input_fingerprint || null,
  };
}

/** 来源身份：主界面只说"已验证内容指纹"，完整值放高级 disclosure。 */
export function sourceIdentityLabel(sourceIdentity) {
  if (!sourceIdentity || typeof sourceIdentity !== 'object') return '未记录来源身份';
  const basis = String(sourceIdentity.identity_basis || sourceIdentity.basis || '').trim();
  const sha = String(sourceIdentity.sha256 || sourceIdentity.content_sha256 || '').trim();
  const verified = sourceIdentity.verified === true || Boolean(sha)
    || basis === 'content_sha256';
  if (!verified) return '来源身份未验证';
  return basis === 'declared_source_facts' ? '已验证来源声明（非内容指纹）' : '已验证内容指纹';
}

/** 来源身份的完整值（高级 disclosure 用）。 */
export function sourceIdentityDetail(sourceIdentity) {
  if (!sourceIdentity || typeof sourceIdentity !== 'object') return '未记录';
  const lines = [];
  for (const [key, value] of Object.entries(sourceIdentity)) {
    if (value === null || value === undefined || value === '') continue;
    lines.push(key + '=' + String(value));
  }
  return lines.join('\n') || '未记录';
}

/**
 * 一行"标签 + 值（+ 可选多行说明）"。
 *
 * 说明文字里的换行（``\n``）转成 ``<br>``：后端给出的原因是多行的，
 * 直接塞进 ``<small>`` 会让整段挤在一行。
 */
function row(label, value, note = '') {
  const noteHtml = note
    ? '<br><small>' + escapeHtml(note).replace(/\n/g, '<br>') + '</small>'
    : '';
  return '<div class="list-row"><span><b>' + escapeHtml(label) + '</b>'
    + noteHtml
    + '</span><small>' + escapeHtml(value) + '</small></div>';
}

/**
 * 【陆海分类事实】面板。
 *
 * 主操作：保存分类策略 / 生成·重新生成陆海分类事实。
 * **不要求用户先运行 Radar**（本区与 Radar policy 完全解耦）。
 */
export function renderSurfaceFactsPanel(flow) {
  const model = surfaceFactsModel(flow);
  const counts = model.counts;
  const stale = model.stale
    ? '<div class="wb-blocker" data-kind="blocker"><b>需要重新生成</b><span>'
      + escapeHtml(SURFACE_FACTS_STALE_NOTE) + '</span></div>'
    : '';
  const factsDetail = 'status=' + model.factsStatus
    + '；policy.status=' + model.policyStatus
    + '；input_fingerprint=' + String(model.inputFingerprint || '未计算');
  return '<h3>陆海分类事实（Surface Facts）</h3>'
    + '<div class="parameter-note">' + escapeHtml(SURFACE_FACTS_BASIS_NOTE)
    + ' unknown（证据不足）保持未知，<b>绝不按海面处理</b>；本区与雷达监视规划解耦，'
    + '不需要先运行 Radar。</div>'
    + stale
    + '<div class="form-grid">'
    + '<label>Land Mask<select class="panel-input" id="surfaceLandMaskState" disabled>'
    + '<option selected>' + escapeHtml(model.landMaskConfigured ? '已配置' : '未配置') + '</option>'
    + '</select></label>'
    + '<label>land_mask_layer_name<input class="panel-input" id="surfaceLandMaskLayer" value="'
    + escapeHtml(model.policyLandMaskLayerName || '') + '" placeholder="留空表示按保守关键词自动选层"></label>'
    + '<label>coastal_uncertainty_buffer_m<input class="panel-input" type="number" min="0" step="any" '
    + 'id="surfaceCoastalBuffer" value="'
    + (model.coastalUncertaintyBufferM === null ? '' : model.coastalUncertaintyBufferM) + '"></label>'
    + '<label>分类策略状态<select class="panel-input" id="surfacePolicyStatus" disabled>'
    + '<option selected>' + escapeHtml(surfacePolicyStatusText(model.policyStatus)) + '</option>'
    + '</select></label>'
    + '</div>'
    + '<label class="check-row"><input type="checkbox" id="surfacePolicyConfirmed" '
    + (model.policyConfirmed ? 'checked' : '') + '> 分类策略已确认（未确认时下游仍需人工复核）</label>'
    + '<label>策略来源<input class="panel-input" id="surfacePolicySource" value="'
    + escapeHtml(String(model.policy.source || 'project_engineering_default')) + '"></label>'
    + '<div class="button-row">'
    + '<button class="secondary" id="saveSurfaceClassificationPolicy">保存分类策略</button>'
    + '<button class="secondary" id="evaluateSurfaceClassFacts">生成 / 重新生成陆海分类事实</button>'
    + '</div>'
    + '<h4>Surface Facts 计数</h4>'
    + row('陆地 land', String(counts.land))
    + row('海上 sea', String(counts.sea))
    + row('海岸不确定 coastal_uncertain', String(counts.coastal_uncertain))
    + row('证据不足 unknown', String(counts.unknown),
      'unknown 是证据不足，不是海面')
    + row('已判定格数 / 总格数',
      model.classifiedGridCellCount + ' / ' + model.gridCellCount)
    + '<div class="flow-summary">来源身份：<b>' + escapeHtml(sourceIdentityLabel(model.sourceIdentity))
    + '</b> · Surface Facts 状态：' + escapeHtml(surfacePolicyStatusText(model.policyStatus === 'confirmed' ? 'confirmed' : 'pending'))
    + '</div>'
    + '<details class="grid-info-detail"><summary>高级：来源身份与指纹完整值</summary>'
    + '<div class="grid-info-detail-body">'
    + '<pre>' + escapeHtml(sourceIdentityDetail(model.sourceIdentity) + '\n' + factsDetail) + '</pre>'
    + '</div></details>';
}

// ---- 2. Communication / RID 工程规划 Profile --------------------------------

/** 半径是否由 canonical ``radius_by_surface`` 承载（而不是单一 radius_m）。 */
export function isSurfaceAwareDevice(item) {
  const geometry = item?.coverage_geometry || {};
  const mapping = geometry.radius_by_surface;
  if (!mapping || typeof mapping !== 'object') return false;
  return Object.values(mapping).some(value => Number.isFinite(Number(value)) && Number(value) > 0);
}

/** 设备目录里该 service 的 canonical 条目（不存在即 ``null``，绝不编造参数）。 */
export function catalogItemForService(flow, serviceKey) {
  return (flow?.device_catalog?.items || []).find(
    item => isSurfaceAwareDevice(item) && String(item?.service_key || '') === String(serviceKey),
  ) || null;
}

/** 半径文本：后端米值 → km 文本；缺失 ⇒ 未配置（绝不补默认值）。 */
export function radiusKmText(radiusM) {
  const value = Number(radiusM);
  if (!Number.isFinite(value) || value <= 0) return '未配置';
  const km = value / 1000;
  return (Number.isInteger(km) ? km.toFixed(1) : String(km)) + ' km';
}

/** 要求站址数文本（**后端 canonical 数字**，不是前端常量）。 */
export function requiredSiteText(value) {
  return requiredSiteCountText(value);
}

const SURFACE_ROWS = [
  ['land', '陆地'],
  ['sea', '海上'],
  ['coastal_uncertain', '海岸不确定'],
];

/** 一个 service 的工程规划 Profile 卡片（全部数值来自后端 canonical 字段）。 */
export function serviceProfileCard(flow, serviceKey) {
  const item = catalogItemForService(flow, serviceKey);
  const geometry = item?.coverage_geometry || {};
  const radius = geometry.radius_by_surface || {};
  const required = flow?.required_cns?.project_default || {};
  const requirementName = serviceKey === 'C:communication' ? 'communication' : 'surveillance';
  const requirement = required[requirementName] || {};
  const mapping = requirement.redundancy_by_surface || {};
  const geometryModel = String(geometry.model || '').trim();
  const horizontalDeg = item?.geometry?.horizontal_coverage_deg;
  const title = SERVICE_KEY_SHORT_LABELS[serviceKey] || serviceKey;
  const fullTitle = SERVICE_KEY_LABELS[serviceKey] || serviceKey;
  const rows = SURFACE_ROWS.map(([surface, label]) => {
    const radiusText = radiusKmText(radius[surface]);
    const requiredValue = mapping[surface];
    return row(
      label,
      radiusText + ' · 要求 ' + requiredSiteText(requiredValue),
      requiredValue === undefined ? '要求站址数未在本项目正式需求中声明（不推断）' : '',
    );
  }).join('');
  const ridExtras = serviceKey === 'S:rid_cooperative'
    ? row('城区 1 km', '仅设备事实（未启用城区规划）', URBAN_FACT_NOTE)
      + row('几何形态', '全向（omnidirectional / 360°）',
        'RID 绝不使用 Radar 的 90° 面阵 / sector / 方位角')
    : row('几何形态', '全向（omnidirectional / 360°）', '');
  const provenance = item
    ? '参数来源：<b>' + escapeHtml(String(item?.service_key || '')) + '</b>'
      + ' · 设备 ' + escapeHtml(String(item?.device_id || '—'))
      + '（' + escapeHtml(String(item?.model || item?.name || '未命名')) + '）'
    : '设备目录里没有该 service 的 canonical 条目：半径与要求站址数<b>不得由前端编造</b>，'
      + '请先在设备资料库登记该 service 的设备事实。';
  return '<div class="coverage-card cns-service-profile" data-service-key="'
    + escapeHtml(serviceKey) + '">'
    + '<b>' + escapeHtml(title) + ' 工程规划基线</b>'
    + '<span>' + escapeHtml(fullTitle)
    + ' · 水平：' + escapeHtml(Number.isFinite(Number(horizontalDeg))
      ? Number(horizontalDeg) + '° 全向' : '全向 360°')
    + ' · 几何模型：' + escapeHtml(geometryModel || '未配置')
    + '（' + escapeHtml(String(geometry.model_scope || 'geometric_only')) + '）</span>'
    + rows
    + ridExtras
    + row('冗余口径', '按 <b>不同物理站址</b>计数（distinct_site_id）',
      '绝不把"N 台设备"当成"N 重"')
    + '<small>' + escapeHtml(ENGINEERING_BASELINE_DISCLAIMER) + '</small>'
    + '<small>' + provenance + '</small>'
    + '</div>';
}

/** 两个正式 surface-aware 服务的 Profile 区块。 */
export function renderServiceProfiles(flow) {
  const cards = CNS_FOCUS_SERVICES.map(key => serviceProfileCard(flow, key)).join('');
  const missing = CNS_FOCUS_SERVICES.filter(key => !catalogItemForService(flow, key));
  return '<h3>Communication / RID 工程规划 Profile</h3>'
    + '<div class="parameter-note">' + escapeHtml(ENGINEERING_BASELINE_DISCLAIMER)
    + '；下列半径与要求站址数只转印后端 canonical 字段（'
    + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '）。</div>'
    + cards
    + (missing.length
      ? '<div class="wb-blocker" data-kind="assumption"><b>工程假设</b><span>'
        + escapeHtml(missing.map(key => serviceKeyLabel(key)).join('、'))
        + '：设备目录尚未登记对应的 canonical service 条目，因此 Profile 的半径保持"未配置"；'
        + '系统不会用单一 radius_m 输入框替代分 surface 半径。</span></div>'
      : '')
    + '<div class="parameter-note">' + escapeHtml(NOT_EVALUATED_AREAS.join(' / '))
    + '：' + escapeHtml(NOT_EVALUATED_TEXT) + '。</div>';
}

// ---- 3. service-aware P14 / P15 / P16 ---------------------------------------

/**
 * 按 service_key 汇总 P15 ``service_redundancy``。
 *
 * 只合并后端给出的计数与状态；跨 subsystem 的合并只做求和/最不利状态，
 * **不**重算任何站址数。
 */
export function aggregateServiceStatements(flow) {
  const groups = new Map();
  const assessment = flow?.cns_corridor_gap_assessment || {};
  for (const route of assessment.routes || []) {
    for (const subsystem of route.subsystems || []) {
      for (const entry of subsystem.service_redundancy || []) {
        const key = String(entry?.service_key || '');
        if (!key) continue;
        const bucket = groups.get(key) || {
          service_key: key,
          subsystem: entry?.subsystem || subsystem.subsystem,
          //: service 总体状态：**后端原值**（缺失即 unknown → 证据不足）。
          status: overallServiceStatus(entry?.status),
          voxel_count: 0,
          status_counts: {satisfied: 0, confirmed_deficit: 0, unknown: 0},
          required_distinct_site_count_by_surface: {},
          distinct_site_count_by_surface: {},
          surface_class_counts: {},
          //: 逐 surface 状态：**只**由后端提供（缺失即不显示，绝不前端推导）。
          status_by_surface: undefined,
          surface_dependent: entry?.surface_dependent !== false,
          //: Round D：``N:rtk_augmentation`` 的专属桶字段（geometry / delivery / 缺口原因 /
          //: 依赖结论）必须一并透传，否则 P15 面板就只剩一行模糊状态。
          geometry_status_counts: {},
          delivery_status_counts: {},
          gap_cause_counts: {},
          evidence_required_reasons: [],
          dependency_only: false,
          recommended_dependency_service: null,
          max_reference_baseline_m: null,
          required_distinct_site_count: null,
          distinct_site_count: null,
          delivery_service_key: null,
        };
        bucket.voxel_count += Number(entry?.voxel_count || 0);
        for (const [status, count] of Object.entries(entry?.status_counts || {})) {
          bucket.status_counts[status] = (bucket.status_counts[status] || 0) + Number(count || 0);
        }
        //: Round D：导航增强桶的计数类字段只做**加法透传**（仍然不做任何判定）。
        for (const field of ['geometry_status_counts', 'delivery_status_counts', 'gap_cause_counts']) {
          for (const [name, value] of Object.entries(entry?.[field] || {})) {
            bucket[field][name] = Number(bucket[field][name] || 0) + Number(value || 0);
          }
        }
        for (const reason of entry?.evidence_required_reasons || []) {
          const text = String(reason);
          if (!bucket.evidence_required_reasons.includes(text)) {
            bucket.evidence_required_reasons.push(text);
          }
        }
        if (entry?.dependency_only === true) bucket.dependency_only = true;
        if (entry?.recommended_dependency_service) {
          bucket.recommended_dependency_service = String(entry.recommended_dependency_service);
        }
        if (Number.isFinite(Number(entry?.max_reference_baseline_m))) {
          bucket.max_reference_baseline_m = Number(entry.max_reference_baseline_m);
        }
        if (Number.isFinite(Number(entry?.required_distinct_site_count))) {
          const requiredValue = Number(entry.required_distinct_site_count);
          bucket.required_distinct_site_count = bucket.required_distinct_site_count === null
            ? requiredValue
            : Math.max(bucket.required_distinct_site_count, requiredValue);
        }
        if (Number.isFinite(Number(entry?.distinct_site_count))) {
          const actualValue = Number(entry.distinct_site_count);
          bucket.distinct_site_count = bucket.distinct_site_count === null
            ? actualValue
            : Math.min(bucket.distinct_site_count, actualValue);
        }
        if (entry?.delivery_service_key) {
          bucket.delivery_service_key = String(entry.delivery_service_key);
        }
        // 逐 surface 状态：只有后端**直接给出**时才透传（当前快照不给，因此保持 undefined）。
        // 这里绝不从计数比较推导，也不为缺失的 surface 补默认值。
        for (const [surface, status] of Object.entries(entry?.status_by_surface || {})) {
          if (status === null || status === undefined) continue;
          bucket.status_by_surface = bucket.status_by_surface || {};
          bucket.status_by_surface[surface] = status;
        }
        for (const field of [
          'required_distinct_site_count_by_surface',
          'distinct_site_count_by_surface',
          'surface_class_counts',
        ]) {
          for (const [surface, value] of Object.entries(entry?.[field] || {})) {
            if (value === null || value === undefined) continue;
            // 同一 service 在不同 voxel 的计数：取"最不利"（最小实际 / 最大要求），
            // 这是既有前端呈现的保守读法，**不**生成新的业务数值。
            const current = bucket[field][surface];
            if (current === undefined) bucket[field][surface] = value;
            else if (field === 'distinct_site_count_by_surface') {
              bucket[field][surface] = Math.min(Number(current), Number(value));
            } else if (field === 'required_distinct_site_count_by_surface') {
              bucket[field][surface] = Math.max(Number(current), Number(value));
            } else {
              bucket[field][surface] = Number(current) + Number(value);
            }
          }
        }
        groups.set(key, bucket);
      }
    }
  }
  return [...groups.values()];
}

/**
 * 逐 surface 行：**只展示后端事实**。
 *
 * 后端当前只在快照里给出
 * ``required_distinct_site_count_by_surface``（要求）与
 * ``distinct_site_count_by_surface``（实际），因此这里只显示计数文本，
 * **绝不**比较 ``actual >= required`` 得出"满足 / 冗余不足"。
 *
 * 逐 surface 状态只有在后端**直接给出** ``status_by_surface`` /
 * ``status_counts_by_surface`` 时才显示（见 :func:`surfaceStatusRows`）；
 * 后端没有给就什么都不显示，前端不补算。
 */
function surfaceCountRows(statement) {
  const surfaces = ['land', 'sea', 'coastal_uncertain', 'unknown'];
  return surfaces.map(surface => {
    const required = statement.required_distinct_site_count_by_surface?.[surface];
    const actual = statement.distinct_site_count_by_surface?.[surface];
    const voxelCount = statement.surface_class_counts?.[surface];
    if (required === undefined && actual === undefined && voxelCount === undefined) return '';
    return row(
      surfaceClassText(surface),
      distinctSiteCountText(actual, required),
      voxelCount === undefined ? '' : '体元 ' + voxelCount,
    );
  }).join('');
}

/**
 * 逐 surface **状态**行：只在后端给出逐 surface 状态时渲染。
 *
 * 后端目前不提供该字段，因此正常路径返回空串——这是有意的
 * fail-closed：没有后端结论就不显示状态，绝不前端推导。
 */
function surfaceStatusRows(statement) {
  const mapping = statement.status_by_surface;
  if (!mapping || typeof mapping !== 'object') return '';
  const surfaces = ['land', 'sea', 'coastal_uncertain', 'unknown'];
  const rows = surfaces
    .filter(surface => mapping[surface] !== null && mapping[surface] !== undefined)
    .map(surface => row(
      surfaceClassText(surface),
      redundancyStatusText(mapping[surface]),
      '后端给出的逐 surface 状态',
    )).join('');
  return rows ? '<h5>逐 surface 状态（后端给出）</h5>' + rows : '';
}

/**
 * P15「CNS 能力缺口」的 service-aware 分段。
 *
 * 优先展示 service 结果（服务 / surface_class / 实际站址数 / 要求站址数 / 状态），
 * 再展示既有 subsystem 口径（绝不删除既有信息）。
 */
export function serviceGapStatements(flow) {
  const assessment = flow?.cns_corridor_gap_assessment || {};
  if (!assessment || String(assessment.status || '') === 'not_calculated') {
    return '<div class="empty-note">尚未评估能力缺口</div>';
  }
  const statements = aggregateServiceStatements(flow);
  if (!statements.length) {
    return '<div class="parameter-note">本结果中没有 service 级冗余证据'
      + '（legacy 项目保持子系统口径；Communication / RID 需要先完成服务走廊评估）。</div>';
  }
  return statements.map(statement => {
    const deficitVoxels = Number(statement.status_counts.confirmed_deficit || 0);
    const unknownVoxels = Number(statement.status_counts.unknown || 0);
    const satisfiedVoxels = Number(statement.status_counts.satisfied || 0);
    //: Round D：缺口语义按服务区分（导航增强是站址缺口，Radar 是覆盖/站址缺口，
    //: Communication / RID 才是冗余不足）。措辞变化不改变任何判定。
    const deficitLabel = statement.service_key === 'N:rtk_augmentation' ? '站址缺口'
      : statement.service_key === 'S:radar_noncooperative' ? '覆盖 / 站址缺口'
        : '冗余不足';
    return '<div class="coverage-card cns-service-statement" data-service-key="'
      + escapeHtml(statement.service_key) + '" data-service-status="'
      + escapeHtml(statement.status) + '">'
      + '<b>' + escapeHtml(serviceKeyLabel(statement.service_key))
      + ' · ' + escapeHtml(backendServiceStatusText(statement.status)) + '</b>'
      + '<small>四服务口径：' + escapeHtml(cnsServiceFormalLabel(statement.service_key)) + '</small>'
      + '<span>体元 ' + statement.voxel_count
      + ' · 满足 ' + satisfiedVoxels
      + ' · ' + escapeHtml(deficitLabel) + ' ' + deficitVoxels
      + ' · 证据不足 ' + unknownVoxels + '</span>'
      + surfaceCountRows(statement)
      + surfaceStatusRows(statement)
      + navigationStatementRows(statement)
      + radarStatementNote(statement)
      + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
      + '</div>';
  }).join('');
}

/**
 * P15 ``N:rtk_augmentation`` 桶的专属行：geometry / delivery / 缺口原因 / dependency_only。
 *
 * 全部字段都是后端桶里**已有**的 canonical 事实；这里只做中文转印，
 * **不**比较任何计数、**不**推导状态。
 */
function navigationStatementRows(statement) {
  if (String(statement?.service_key || '') !== 'N:rtk_augmentation') return '';
  const geometryLabel = navigationGeometryStatusText(
    worstServiceStatus(statement.geometry_status_counts));
  const deliveryLabel = navigationDeliveryStatusText(
    worstServiceStatus(statement.delivery_status_counts));
  const baselineText = navigationBaselineText(statement.max_reference_baseline_m);
  const siteText = distinctSiteCountText(
    statement.distinct_site_count, statement.required_distinct_site_count);
  const rows = [
    row('参考站几何（' + NAVIGATION_BASELINE_LEGEND_LABEL + '）', geometryLabel,
      '基线距离 ' + baselineText + ' · ' + siteText),
    row('通信修正数据交付（' + navigationDeliveryDependencyLabel(
      statement.delivery_service_key) + '）', deliveryLabel, ''),
  ];
  for (const [cause, count] of Object.entries(statement.gap_cause_counts || {})) {
    rows.push(row('缺口原因', navigationGapCauseText(cause), count + ' 个体元'));
  }
  for (const reason of statement.evidence_required_reasons || []) {
    rows.push(row('证据不足原因', navigationReasonText(reason), ''));
  }
  const dependencyNote = statement.dependency_only === true
    ? '<div class="coverage-card cns-dependency-only"><b>无需新增导航站，应优先补通信服务</b>'
      + '<span>该航路的导航增强缺口只来自 RTK 修正数据的通信交付'
      + '（应补服务：' + escapeHtml(navigationDeliveryDependencyLabel(
        statement.recommended_dependency_service)) + '），'
      + '导航站址几何本身已满足。</span></div>'
    : '';
  return rows.join('') + dependencyNote;
}

/** delivery 依赖服务 key → 中文（Round D：主文本不出现 raw service key）。 */
function navigationDeliveryDependencyLabel(serviceKey) {
  const key = String(serviceKey || '').trim();
  if (!key) return NAVIGATION_DELIVERY_DEPENDENCY_LABEL;
  return cnsServiceFormalLabel(key);
}

/** P15 ``S:radar_noncooperative`` 桶的方向性几何声明（不复制任何 Radar 数值）。 */
function radarStatementNote(statement) {
  if (String(statement?.service_key || '') !== 'S:radar_noncooperative') return '';
  return '<div class="parameter-note">' + escapeHtml(RADAR_DIRECTIONAL_GEOMETRY_NOTE)
    + ' 本服务的站址重数按 Radar 面阵几何与 surface 要求给出，'
    + '绝不与 Communication / RID 的站址数相加。</div>';
}

/** P14 服务走廊：service 级逐体元证据（需要按需载入明细后才可用）。 */
export function serviceCorridorEvidence(flow) {
  const assessment = flow?.cns_corridor_assessment || {};
  const detail = assessment.detail || null;
  if (!detail) {
    const available = assessment.detail_available === true;
    return '<div class="parameter-note">逐体元 service 证据（含 surface_class / '
      + 'distinct_site_count / required_distinct_site_count）属大型明细，已外置保存；'
      + (available ? '点下方按钮按需读取。' : '当前结果尚未生成。') + '</div>'
      + (available
        ? '<div class="button-row"><button class="secondary" id="loadServiceCorridorDetail">'
          + '载入 service 逐体元证据</button></div>'
        : '');
  }
  const groups = new Map();
  for (const route of detail.routes || []) {
    for (const voxel of route.voxels || []) {
      for (const subsystem of voxel.subsystems || []) {
        for (const entry of subsystem.service_redundancy || []) {
          if (!isSurfaceAwareServiceKey(entry?.service_key)) continue;
          const key = String(entry.service_key);
          const bucket = groups.get(key) || {service_key: key, samples: []};
          bucket.samples.push({
            route_id: route.route_id,
            voxel_id: voxel.voxel_id,
            surface_class: entry.surface_class || voxel.surface_class,
            required: entry.required_distinct_site_count,
            actual: entry.distinct_site_count,
            status: entry.status,
          });
          groups.set(key, bucket);
        }
      }
    }
  }
  if (!groups.size) {
    return '<div class="parameter-note">逐体元明细中没有 service 级证据'
      + '（legacy 项目保持子系统口径）。</div>';
  }
  return [...groups.values()].map(bucket => {
    const rows = bucket.samples.slice(0, 12).map(sample => row(
      surfaceClassText(sample.surface_class),
      distinctSiteCountText(sample.actual, sample.required),
      redundancyStatusText(sample.status) + ' · ' + String(sample.voxel_id || ''),
    )).join('');
    return '<div class="coverage-card cns-service-statement" data-service-key="'
      + escapeHtml(bucket.service_key) + '">'
      + '<b>' + escapeHtml(serviceKeyLabel(bucket.service_key))
      + ' · 逐体元证据 ' + bucket.samples.length + ' 条</b>'
      + rows
      + '</div>';
  }).join('');
}

// ---- P16 设施规划 -------------------------------------------------------------

/** 规划动作的目标服务（**绝不**只显示 C / S）。 */
export function facilityTargetTitle(action) {
  const key = String(action?.service_key || '').trim();
  if (isSurfaceAwareServiceKey(key)) {
    return serviceKeyLabel(key) + '缺口';
  }
  if (key) return serviceKeyLabel(key) + '缺口';
  return subsystemServiceLabel(action?.subsystem) + '（子系统口径）';
}

/**
 * 同一物理站址提示：只有 canonical ``distinct_site_id`` 相同才给。
 *
 * ``others`` 必须**不含当前动作自己**：把自己算作"另一个同址动作"会让每一条
 * 动作都被误标成共址。
 */
export function colocatedNote(action, others) {
  const identity = String(action?.distinct_site_id || '').trim();
  if (!identity) return '';
  const known = (others || [])
    .filter(item => item !== action)
    .map(item => String(item?.distinct_site_id ?? '').trim())
    .filter(Boolean);
  if (!known.some(value => sameDistinctSite(value, identity))) return '';
  return '同一物理站址，不增加独立站址重数';
}

/** 该 action 的物理站址身份是否在本计划里出现过**多次**。 */
function sharedPhysicalSite(selected, action) {
  const identity = String(action?.distinct_site_id || '').trim();
  if (!identity) return false;
  return selected.filter(
    item => String(item?.distinct_site_id || '').trim() === identity,
  ).length > 1;
}

/**
 * P16 selected_actions 的 service-aware 卡片。
 *
 * 站址计数一律取后端 ``current_units``（= distinct_site_count）与
 * ``required_units``，且**只显示计数**，不由前端比较得出状态；
 * 状态行只消费后端自带的 ``action.status``（P16 action 契约字段），
 * 缺失时显示「证据不足」，绝不前端推导。
 *
 * 「新增后」的单位数只在该动作是**该物理站址唯一的规划动作**时才展示——
 * 同一站址上的第二条设备**不增加独立站址重数**，把它的 after_units 当成
 * "新增后重数"会误导用户。
 */
export function facilityPlanCards(result) {
  const selected = result?.selected_actions || [];
  if (!selected.length) {
    return '<div class="empty-note">没有产生确认缺口边际改善为正的可行动作</div>';
  }
  const existingSiteIds = selected;
  return selected.map(action => {
    const impact = action?.impact || {};
    const progress = (impact.target_progress || [])[0] || null;
    const before = progress?.before_units;
    const after = progress?.after_units;
    const required = Number(action?.required_units);
    const current = Number(action?.current_units);
    const shared = sharedPhysicalSite(selected, action);
    const note = colocatedNote(action, existingSiteIds);
    const afterClaim = !shared && Number.isFinite(Number(before)) && Number.isFinite(Number(after))
      ? '<span>新增后：' + escapeHtml(distinctSiteCountText(after, required || after)) + '</span>'
      : '';
    const serviceKey = String(action?.service_key || '');
    //: Round D：导航增强规划的是**工程规划单元**（不是设备型号），
    //: device_id = null 是正常状态，绝不显示成错误。
    const deviceClause = serviceKey === 'N:rtk_augmentation'
      ? ' · ' + escapeHtml(NAVIGATION_PLANNING_UNIT_LABEL)
      : ' · 新增 ' + escapeHtml(String(action?.device_id || '设备型号未选择'));
    return '<div class="coverage-card cns-facility-action" data-service-key="'
      + escapeHtml(serviceKey) + '" data-planner-family="'
      + escapeHtml(String(action?.planner_family || '')) + '">'
      + '<b>' + escapeHtml(facilityTargetTitle(action))
      + (Number.isFinite(required) && Number.isFinite(current)
        ? ' · ' + escapeHtml(distinctSiteCountText(current, required)) : '') + '</b>'
      + '<span>建议：' + escapeHtml(String(action?.host?.host_tower_name || ''))
      + (action?.host?.host_tower_id ? '（' + escapeHtml(String(action.host.host_tower_id)) + '）' : '')
      + deviceClause
      + ' · 服务 ' + escapeHtml(serviceKeyLabel(action?.service_key)) + '</span>'
      + '<span>站址来源：' + escapeHtml(navigationSiteSourceText(action?.reuse_class))
      + ' · distinct_site_id：' + escapeHtml(String(action?.distinct_site_id || '—')) + '</span>'
      + afterClaim
      + (action?.surface_class
        ? '<span>surface_class：' + escapeHtml(surfaceClassText(action.surface_class)) + '</span>' : '')
      + (note ? '<span class="cns-colocation-note">' + escapeHtml(note) + '</span>' : '')
      + (action?.subsystem_mount_status === 'declared_not_compatible'
        ? '<span>分系统安装：站点声明的可用分系统不包含该设备分系统</span>' : '')
      + facilityActionServiceRows(action)
      + '<span>状态：' + escapeHtml(backendServiceStatusText(action?.status)) + '</span>'
      + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
      + '<small>审计标识：' + escapeHtml(String(action?.action_id || '—')) + '</small>'
      + '</div>';
  }).join('');
}

/**
 * P16 动作的 service 专属行（Round D）。
 *
 * 导航增强：规划单元 / 设备选型状态 / 站址适用性；
 * 非合作监视：方向性面阵的型号、panel、方位角、波束宽度与俯仰预设。
 * 全部数值都来自 backend canonical action，前端不新增任何 Radar / RTK 数值。
 */
function facilityActionServiceRows(action) {
  const serviceKey = String(action?.service_key || '');
  if (serviceKey === 'N:rtk_augmentation') {
    const equipment = String(action?.equipment_selection_status || 'not_selected');
    return '<span class="cns-navigation-action">新增 GNSS/RTK 基准站规划单元'
      + ' · 设备选型状态 ' + escapeHtml(equipment === 'not_selected' ? '尚未选择' : equipment) + '</span>'
      + '<span>复用类别：' + escapeHtml(navigationSiteSourceText(action?.reuse_class))
      + ' · 站址来源：' + escapeHtml(navigationSiteSourceText(action?.planning_origin))
      + ' · 坐标系 ' + escapeHtml(String((action?.coordinate || []).join(', ') || '—')) + '</span>'
      + '<small>' + escapeHtml(NAVIGATION_EQUIPMENT_NOT_SELECTED_NOTE) + '</small>'
      + '<details class="grid-info-detail"><summary>高级：规划单元原始字段</summary>'
      + '<div class="grid-info-detail-body"><pre>' + escapeHtml([
        'planning_unit=' + String(action?.planning_unit ?? 'null'),
        'equipment_selection_status=' + String(action?.equipment_selection_status ?? 'null'),
        'maturity=' + String(action?.maturity ?? 'null'),
        'action_type=' + String(action?.action_type ?? 'null'),
      ].join('\n')) + '</pre></div></details>';
  }
  if (serviceKey === 'S:radar_noncooperative') {
    const panel = action?.panel || {};
    const elevation = panel.elevation_center_deg === undefined || panel.elevation_center_deg === null
      ? '未给出俯仰预设' : '俯仰预设 ' + String(panel.elevation_center_deg) + '°';
    return '<span class="cns-radar-action">方向性 Radar panel'
      + ' · ' + escapeHtml(RADAR_TYPE_TEXT[action?.radar_type] || String(action?.radar_type || '未声明型号'))
      + ' · panel ' + escapeHtml(String(panel.panel_id || '—')) + '</span>'
      + '<span>方位角 ' + escapeHtml(String(panel.azimuth_deg ?? '—')) + '°'
      + ' · 波束宽度 ' + escapeHtml(String(panel.beamwidth_deg ?? '—')) + '°'
      + ' · ' + escapeHtml(elevation)
      + ' · 铁塔 ' + escapeHtml(String(action?.tower_id || '—')) + '</span>'
      + '<span>方向性面阵几何：绝不按普通圆形覆盖站渲染'
      + (action?.provenance?.algorithm_id
        ? ' · 来源算法 ' + escapeHtml(String(action.provenance.algorithm_id))
          + '（' + escapeHtml(String(action.provenance.algorithm_version || '—')) + '）'
        : '') + '</span>';
  }
  if (serviceKey === 'C:communication' || serviceKey === 'S:rid_cooperative') {
    return '<span>动作：全向站点规划（' + escapeHtml(plannerFamilyLabel(action?.planner_family))
      + '）· 站址来源 ' + escapeHtml(navigationSiteSourceText(action?.planning_origin)) + '</span>';
  }
  return '';
}

/**
 * 后端 service 总体状态 → 中文。
 *
 * **只**消费后端给定的状态（``satisfied`` / ``confirmed_deficit`` / ``unknown``）：
 * 未登记取值一律显示「证据不足」，绝不从任何计数比较推导状态。
 */
export function backendServiceStatusText(status) {
  const key = String(status ?? '').trim();
  if (!key) return '证据不足';
  // 满足 / 冗余不足 / 证据不足 —— 冗余语境沿用集中词表，未登记取值原样返回。
  return redundancyStatusText(key);
}

/**
 * service 总体状态：**只**取后端 ``service_redundancy[].status``。
 *
 * 后端当前不提供该字段（或给出未登记取值）时返回 ``unknown`` → 「证据不足」，
 * 这是有意的 fail-closed：前端绝不用 ``actual >= required`` 之类逻辑补算状态。
 */
function overallServiceStatus(status) {
  const key = String(status ?? '').trim();
  if (key === 'satisfied' || key === 'confirmed_deficit' || key === 'unknown') return key;
  return 'unknown';
}

/** P16 残余 target 的 service-aware 摘要。 */
export function residualTargetsSummary(result) {
  const residuals = result?.residual_confirmed_targets || [];
  if (!residuals.length) return '<div class="empty-note">没有残余确认目标</div>';
  return residuals.slice(0, 20).map(target => {
    const key = String(target?.service_key || '').trim();
    const title = key ? serviceKeyLabel(key) + '缺口' : subsystemServiceLabel(target?.subsystem);
    // 残余状态**只**用后端给出的 ``final_status``（combined 语义），绝不前端推导。
    return row(
      title + (target?.surface_class ? ' · ' + surfaceClassText(target.surface_class) : ''),
      distinctSiteCountText(target?.current_units, target?.required_units),
      '残余状态 ' + combinedStatusText(target?.final_status),
    );
  }).join('');
}

/** service 状态词表的转发（面板与图例同源）。 */
export const CNS_SERVICE_STATUS_LABELS = SERVICE_STATUS_TEXT;
export const CNS_REDUNDANCY_STATUS_LABELS = REDUNDANCY_STATUS_TEXT;
export const CNS_SURFACE_CLASS_LABELS = SURFACE_CLASS_TEXT;
export const CNS_GAP_STATE_LEGEND = CNS_GAP_STATE_LABELS;
export const CNS_GAP_STATE_COLORS = CNS_GAP_COLORS;
export {
  combinedStatusText, distinctSiteCountText, gapStateText, isSurfaceAwareServiceKey,
  redundancyStatusText, sameDistinctSite, serviceStatusText,
  subsystemServiceLabel, surfaceClassText, vocabularyText,
};

// =========================================================
// Round D：统一 CNS 服务规划工作台（四服务）
//
// Communication(C) / 导航增强(N) / 合作监视 RID(S) / 非合作监视 Radar(S)
//
// 铁律不变：
//  * 所有数值（半径、要求站址数、工程基线距离、panel 几何）都只转印 backend
//    canonical 字段，前端**不提供任何默认值**，也不复制第二份业务 authority；
//  * 前端不做距离、重数、land/sea 判定，也不把 RID 与 Radar 合并成「监视」；
//  * 缺失证据一律 fail-closed 显示「未声明 / 证据不足 / 待确认」。
// =========================================================

/** Round D 唯一的新增写入口（站址适用性）；**不**新增第二套站址容器。 */
export const NAVIGATION_SITE_SUITABILITY_ENDPOINT = '/api/navigation-site-suitability';

/** RequiredCNS 写入端点（复用既有契约，不新增）。 */
export const REQUIRED_CNS_ENDPOINT = '/api/required-cns';

/** service_key → 合法 DOM id（冒号不能出现在 id 里）。 */
export function serviceRequirementDomId(serviceKey) {
  return 'requiredService_' + String(serviceKey || '').replace(/[^A-Za-z0-9]/g, '_');
}

// ---- 4.1 CNS 服务需求（RequiredCNS 多服务编辑区） ----------------------------

/**
 * 四服务需求模型：**只读** backend canonical ``required_cns.project_default``。
 *
 * legacy 项目（``services`` 键不存在）返回的每一行都是 ``services: null`` ⇒ 界面
 * 显示"尚未显式声明"，**绝不**在打开页面时自动写入任何 service。
 */
export function rounddRequiredServicesModel(flow) {
  const required = flow?.required_cns || {};
  const project = required.project_default || {};
  const rows = CNS_SERVICE_REQUIREMENT_ROWS.map(spec => {
    const requirement = project[spec.subsystem] || {};
    const services = requirement.services && typeof requirement.services === 'object'
      ? requirement.services : null;
    const entry = services ? services[spec.serviceKey] : null;
    const declared = Boolean(
      services && Object.prototype.hasOwnProperty.call(services, spec.serviceKey),
    );
    return {
      serviceKey: spec.serviceKey, subsystem: spec.subsystem, label: spec.label,
      checkboxLabel: spec.checkboxLabel, requirement, services, entry, declared,
      //: 只有后端 ``required === true`` 才算"本项目要求该服务"。
      required: entry?.required === true,
      confirmed: entry?.confirmed === true,
      domId: serviceRequirementDomId(spec.serviceKey),
    };
  });
  const surveillance = project.surveillance || {};
  const surveillanceServices = surveillance.services && typeof surveillance.services === 'object'
    ? surveillance.services : null;
  const ridRequired = surveillanceServices?.['S:rid_cooperative']?.required === true;
  const radarRequired = surveillanceServices?.['S:radar_noncooperative']?.required === true;
  const declaredMode = String(surveillance.service_requirement_mode || '').trim();
  return {
    required, project, rows,
    servicesDeclared: rows.some(item => item.services !== null),
    ridRequired, radarRequired, declaredMode,
    //: all_required 由后端写入（显式声明或"两个 required 服务"自动升级）；
    //: 前端只显示，不提供"任选一个即可"的选项。
    allRequired: declaredMode === 'all_required' || (ridRequired && radarRequired),
  };
}

/** 【CNS 服务需求】面板：四个服务复选框 + 只读的 all_required 监视模式。 */
export function renderRequiredCnsServicesPanel(flow) {
  const model = rounddRequiredServicesModel(flow);
  const rows = model.rows.map(item => '<label class="check-row cns-service-requirement" data-service-key="'
    + escapeHtml(item.serviceKey) + '" data-declared="' + String(item.declared) + '">'
    + '<input type="checkbox" id="' + escapeHtml(item.domId) + '"'
    + (item.required ? ' checked' : '') + '> '
    + escapeHtml(item.label) + ' · ' + escapeHtml(item.checkboxLabel)
    + (item.declared ? '' : ' <small>（本服务尚未显式声明）</small>')
    + '</label>').join('');
  const modeRow = model.allRequired
    ? '<div class="flow-summary cns-surveillance-mode">监视要求模式：<b>'
      + escapeHtml(ALL_REQUIRED_MODE_LABEL) + '</b>'
      + '<br>该语义只读：本项目已冻结为两条监视通道都必须满足，'
      + '不提供"任选一个即可"的选项。</div>'
    : '';
  const legacyNote = model.servicesDeclared ? ''
    : '<div class="parameter-note">本项目当前只有旧版子系统需求（没有 services 声明）：'
      + '以上复选框默认全部未启用，打开本页<b>不会</b>自动写入任何服务要求。</div>';
  return '<h3>CNS 服务需求</h3>'
    + '<div class="parameter-note">只有显式启用某个服务，才会在 required_cns 里写入该 service；'
    + '未启用的服务既不被推断为"需要"，也不被推断为"不需要"。</div>'
    + legacyNote
    + '<div class="cns-service-requirements" data-services-declared="'
    + String(model.servicesDeclared) + '">' + rows + '</div>'
    + modeRow
    + '<div class="button-row"><button class="secondary" id="saveRequiredCnsServices">'
    + '保存 CNS 服务需求</button></div>'
    + '<div class="parameter-note">' + escapeHtml(CNS_SERVICE_GEOMETRY_LEGEND_NOTE) + '</div>';
}

// ---- 4.2 导航增强（GNSS/RTK）工程规划参数 -----------------------------------

/**
 * 导航增强工程规划模型：**只**读
 * ``required_cns.project_default.navigation.services["N:rtk_augmentation"].planning``。
 *
 * 前端**没有**任何默认基线距离：缺失即 ``null``，显示「未配置」。
 */
export function navigationPlanningModel(flow) {
  const requirement = ((flow?.required_cns || {}).project_default || {}).navigation || {};
  const services = requirement.services && typeof requirement.services === 'object'
    ? requirement.services : null;
  const entry = services ? services['N:rtk_augmentation'] : null;
  const planning = entry && typeof entry.planning === 'object' ? entry.planning : null;
  const baselineValue = planning ? Number(planning.max_reference_baseline_m) : NaN;
  const siteValue = planning ? Number(planning.required_distinct_site_count) : NaN;
  return {
    requirement, entry, planning,
    declared: Boolean(entry),
    required: entry?.required === true,
    //: 模型名由 registry 冻结为 reference_station_baseline（非法取值后端直接 raise）。
    model: String(planning?.model || ''),
    baselineM: Number.isFinite(baselineValue) && baselineValue > 0 ? baselineValue : null,
    siteCount: Number.isInteger(siteValue) && siteValue > 0 ? siteValue : null,
    deliveryServiceKey: planning?.delivery_service_key ?? null,
    confirmed: planning?.confirmed === true,
    source: planning?.source ?? null,
    maturity: planning?.maturity || null,
    readiness: String(planning?.planning_readiness || 'pending_confirmation'),
    missingEvidence: planning?.missing_evidence === true,
  };
}

/** 【导航增强（GNSS/RTK）工程规划】卡片：数值**只**来自 canonical planning 块。 */
export function renderNavigationPlanningPanel(flow) {
  const model = navigationPlanningModel(flow);
  const baselineText = navigationBaselineText(model.baselineM);
  const readiness = navigationPlanningReadinessText(model.readiness);
  const delivery = model.deliveryServiceKey
    ? escapeHtml(model.deliveryServiceKey) : '未配置';
  return '<h3>导航增强（GNSS/RTK）工程规划</h3>'
    + '<div class="parameter-note">' + escapeHtml(NAVIGATION_BASELINE_DISCLAIMER) + '</div>'
    + '<div class="coverage-card cns-navigation-planning" data-service-key="N:rtk_augmentation"'
    + ' data-planning-readiness="' + escapeHtml(model.readiness) + '">'
    + '<b>' + escapeHtml(NAVIGATION_BASELINE_MODEL_LABEL) + '</b>'
    + '<span>规划模型：' + escapeHtml(NAVIGATION_BASELINE_MODEL_LABEL) + '（只读） · '
    + escapeHtml(NAVIGATION_BASELINE_LEGEND_LABEL) + '：<b>' + escapeHtml(baselineText) + '</b></span>'
    + '<div class="form-grid">'
    + '<label>最大基准距离（m）<input class="panel-input" type="number" min="0" step="any" '
    + 'id="navigationBaselineM" value="' + (model.baselineM === null ? '' : model.baselineM) + '"></label>'
    + '<label>要求的独立站址数<input class="panel-input" type="number" min="1" step="1" '
    + 'id="navigationDistinctSiteCount" value="' + (model.siteCount === null ? '' : model.siteCount) + '"></label>'
    + '<label>交付服务<select class="panel-input" id="navigationDeliveryService" disabled>'
    + '<option selected>' + escapeHtml(NAVIGATION_DELIVERY_DEPENDENCY_LABEL)
    + '（' + delivery + '）</option></select></label>'
    + '</div>'
    + '<label class="check-row"><input type="checkbox" id="navigationPlanningConfirmed"'
    + (model.confirmed ? ' checked' : '') + '> 导航增强工程策略已确认</label>'
    + '<label>工程依据 / 来源<input class="panel-input" id="navigationPlanningSource" value="'
    + escapeHtml(model.source || '') + '" placeholder="工程依据 / 来源"></label>'
    + '<div class="flow-summary">规划就绪状态：<b>' + escapeHtml(readiness) + '</b>'
    + (model.missingEvidence
      ? ' · 仍有工程参数尚未确认（最大基准距离与要求的独立站址数都必须显式给出）' : '')
    + '</div>'
    + '<div class="button-row"><button class="secondary" id="saveNavigationPlanning">'
    + '保存导航增强工程参数</button></div>'
    + '<small>本区不提供任何默认距离：数值只能由用户按工程依据输入，'
    + '并逐字声明「' + escapeHtml(NAVIGATION_BASELINE_DISCLAIMER) + '」。</small>'
    + '<details class="grid-info-detail"><summary>高级：工程参数原始字段</summary>'
    + '<div class="grid-info-detail-body"><pre>' + escapeHtml([
      'model=' + (model.model || '未声明'),
      'max_reference_baseline_m=' + (model.baselineM === null ? 'null' : String(model.baselineM)),
      'required_distinct_site_count=' + (model.siteCount === null ? 'null' : String(model.siteCount)),
      'delivery_service_key=' + String(model.deliveryServiceKey ?? 'null'),
      'confirmed=' + String(model.confirmed),
      'maturity=' + String(model.maturity ?? 'null'),
      'planning_readiness=' + model.readiness,
    ].join('\n')) + '</pre></div></details>'
    + '</div>';
}

// ---- 4.3 导航基准站候选适用性 -----------------------------------------------

/** 适用性字段（与后端 ``SUITABILITY_FIELDS`` 的顺序一致）。 */
export const NAVIGATION_SUITABILITY_FIELDS = [
  ['confirmed', '适用性已确认'],
  ['planning_use_confirmed', '允许作为导航基准站规划候选'],
  ['open_sky_confirmed', '天空开阔条件已确认'],
  ['surveyed_coordinate_confirmed', '精确测量坐标已确认'],
  ['stable_mount_confirmed', '稳定安装条件已确认'],
  ['backhaul_available', '回传条件已确认'],
  ['reference_station_installed', '已有参考站已建成'],
];

/** 适用性字段 → DOM id（前端唯一一套，bind 与测试共用）。 */
export const NAVIGATION_SUITABILITY_DOM_IDS = {
  confirmed: 'navSuitabilityConfirmed',
  planning_use_confirmed: 'navSuitabilityPlanningUse',
  open_sky_confirmed: 'navSuitabilityOpenSky',
  surveyed_coordinate_confirmed: 'navSuitabilitySurveyed',
  stable_mount_confirmed: 'navSuitabilityStableMount',
  backhaul_available: 'navSuitabilityBackhaul',
  reference_station_installed: 'navSuitabilityInstalled',
  source: 'navSuitabilitySource',
  notes: 'navSuitabilityNotes',
};

/** 「适合安装」与「已经安装」必须分开（第 7 节逐字语义）。 */
export const NAVIGATION_SUITABILITY_SEMANTICS_NOTE =
  '「适用性已确认 + 允许作为导航基准站规划候选」只表示该站址可以进入 RTK 基准站规划候选；'
  + '「已有参考站已建成」才表示已有参考站事实。候选铁塔不会因为适用性确认而自动显示成"已有参考站"。';

/** 站址条目上已声明的适用性（顶层优先，其次 ``metadata``）；未声明返回 ``null``。 */
export const navigationSuitabilityOf = navigationSuitabilityOfValue;

/** 三类站址来源（与后端 ``set_navigation_site_suitability`` 的集合一一对应）。 */
export const NAVIGATION_SUITABILITY_SOURCES = [
  ['existing_cns_facility', '已有 CNS 设施', 'existing_cns_facilities'],
  ['tower_colocation_host', '共塔候选（真实铁塔）', 'towers'],
  ['candidate_site', '候选站址', 'candidate_sites'],
];

/** 可编辑适用性的站址清单（**只**取既有站址容器，绝不新建容器）。 */
export function navigationSuitabilitySites(flow) {
  const sites = [];
  for (const [siteSource, sourceLabel, collectionKey] of NAVIGATION_SUITABILITY_SOURCES) {
    const items = flow?.[collectionKey]?.items || [];
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      const host = (item.metadata || {}).host || {};
      const siteId = String(
        siteSource === 'tower_colocation_host'
          ? (item.tower_id || host.host_tower_id || '')
          : (item.site_id || item.facility_id || ''),
      ).trim();
      if (!siteId) continue;
      sites.push({
        siteSource, sourceLabel, siteId,
        facilityId: item.facility_id ?? null,
        towerId: siteSource === 'tower_colocation_host' ? siteId : (host.host_tower_id ?? null),
        name: String(item.name || siteId),
        coordinate: Array.isArray(item.coordinate) ? item.coordinate.slice(0, 2) : null,
        suitability: navigationSuitabilityOf(item),
      });
    }
  }
  return sites;
}

/** 适用性表单的字段行（``null`` 表示未声明：勾选框全部不勾，并逐项标注"未声明"）。 */
export function navigationSuitabilityFieldRows(suitability) {
  const item = suitability && typeof suitability === 'object' ? suitability : null;
  return NAVIGATION_SUITABILITY_FIELDS.map(([name, label]) => {
    const value = item ? item[name] : null;
    const note = value === true || value === false ? '' : ' <small>（未声明）</small>';
    return '<label class="check-row cns-suitability-field" data-field="' + escapeHtml(name) + '">'
      + '<input type="checkbox" id="' + escapeHtml(NAVIGATION_SUITABILITY_DOM_IDS[name]) + '"'
      + (value === true ? ' checked' : '') + '> ' + escapeHtml(label) + note + '</label>';
  }).join('');
}

/**
 * 【导航基准站候选适用性】面板。
 *
 * 不做"一次展开数百个 tower 卡片"：先搜索 / 选择一个站址，再编辑该站址的适用性。
 * 选择与载入完全在前端完成（数据都已在 flow 里），保存才走后端写入口。
 */
export function renderNavigationSuitabilityPanel(flow) {
  const sites = navigationSuitabilitySites(flow);
  const declared = sites.filter(item => item.suitability !== null);
  const eligible = declared.filter(item => item.suitability.confirmed === true
    && item.suitability.planning_use_confirmed === true);
  const installed = declared.filter(item => item.suitability.reference_station_installed === true);
  const options = sites.slice(0, 200).map(item => '<option value="'
    + escapeHtml(item.siteSource + '|' + item.siteId) + '">'
    + escapeHtml(item.sourceLabel) + ' · ' + escapeHtml(item.name) + ' · ' + escapeHtml(item.siteId)
    + '（' + (item.suitability ? '已有适用性声明' : '尚未声明') + '）</option>').join('');
  return '<h3>导航基准站候选适用性</h3>'
    + '<div class="parameter-note">' + escapeHtml(NAVIGATION_SUITABILITY_SEMANTICS_NOTE) + '</div>'
    + '<label>搜索 / 选择站址<select class="panel-input" id="navigationSuitabilitySite">'
    + '<option value="">— 请选择要编辑的站址 —</option>' + options + '</select></label>'
    + (sites.length > 200
      ? '<div class="parameter-note">站址较多，选择列表只显示前 200 个；'
        + '请在搜索框内缩小范围后再选择。</div>'
      : '')
    + '<div class="button-row"><button class="secondary" id="loadNavigationSuitabilitySite">'
    + '载入所选站址的适用性</button></div>'
    + '<div class="coverage-card cns-navigation-suitability" data-selected-site-source="" '
    + 'data-selected-site-id="">'
    + navigationSuitabilityFieldRows(null)
    + '<label>来源 / 工程依据<input class="panel-input" id="'
    + escapeHtml(NAVIGATION_SUITABILITY_DOM_IDS.source) + '" value=""></label>'
    + '<label>备注<input class="panel-input" id="'
    + escapeHtml(NAVIGATION_SUITABILITY_DOM_IDS.notes) + '" value=""></label>'
    + '<div class="button-row">'
    + '<button class="secondary" id="saveNavigationSiteSuitability">保存站址适用性</button>'
    + '<button class="secondary" id="clearNavigationSiteSuitability">'
    + '清除该站址的适用性声明</button></div>'
    + '</div>'
    + '<div class="flow-summary">站址合计 ' + sites.length
    + ' · 已有适用性声明 ' + declared.length
    + ' · 已确认可进入规划候选 ' + eligible.length
    + ' · 已建成参考站 ' + installed.length + '</div>'
    + '<div class="parameter-note">本区不生成站址、不推断坐标、不排序候选：'
    + '它只把用户选择的站址写入既有站址条目的元数据，并继续走既有失效与保存链路。</div>';
}

// ---- 4.4 非合作监视（Radar）卡片 -------------------------------------------
//
// **不复制 Radar 参数形成第二 authority**：本卡只读 canonical Radar state
// （``radar_surveillance_layout`` / result_statuses）与 P15 的
// ``S:radar_noncooperative`` service 证据。Radar 参数与运行仍在既有
// 「雷达监视规划」面板（cns-res-radar）里，本卡不新增任何 Radar 编辑器。

/** Radar layout 状态 → 业务中文（未登记原样返回）。 */
export const RADAR_LAYOUT_STATE_TEXT = {
  not_calculated: '未计算',
  stale: '已过时',
  proposal_ready: '候选划设就绪',
  infeasible: '不可行',
  refinement_incomplete: '精细化未完成',
};

/** Radar 型号短名（显示用；数值仍只来自 backend canonical 结果）。 */
export {RADAR_TYPE_TEXT};

/** current / stale / missing 三态 → 业务中文（Radar 卡片与 tooltip 共用）。 */
export function radarStateText(status, stale) {
  const key = String(status ?? '').trim();
  if (stale === true || key === 'stale') return '当前结果已过时';
  if (!key || key === 'not_calculated') return '尚无当前结果';
  return RADAR_LAYOUT_STATE_TEXT[key] || key;
}

/** P15 里 ``S:radar_noncooperative`` 的 canonical service 桶（不存在即 ``null``）。 */
export function radarServiceBucket(flow) {
  const assessment = flow?.cns_corridor_gap_assessment || {};
  for (const route of assessment.routes || []) {
    for (const subsystem of route.subsystems || []) {
      for (const entry of subsystem.service_redundancy || []) {
        if (String(entry?.service_key || '') === 'S:radar_noncooperative') return entry;
      }
    }
  }
  return null;
}

/** 【非合作监视（Radar）】卡片：只转印 canonical Radar state 与 Radar service 证据。 */
export function renderRadarServicePanel(flow) {
  const layout = flow?.radar_surveillance_layout || {};
  const items = Array.isArray(layout.items) ? layout.items : [];
  const item = items.length ? items[items.length - 1] : null;
  const bucket = radarServiceBucket(flow);
  const resultStatus = String((flow?.result_statuses || {}).radar_surveillance_layout || '');
  const stale = resultStatus === 'stale' || item?.status === 'stale';
  const panels = Array.isArray(item?.selected_panels) ? item.selected_panels : [];
  const towerIds = Array.isArray(item?.selected_tower_ids) ? item.selected_tower_ids : [];
  const panelRows = panels.slice(0, 8).map(panel => row(
    RADAR_TYPE_TEXT[panel?.radar_type] || String(panel?.radar_type || '未声明型号'),
    '铁塔 ' + String(panel?.tower_id ?? '—'),
    '方位角 ' + String(panel?.azimuth_deg ?? '—') + '° · 半宽 '
    + String(panel?.panel_half_width_deg ?? '—') + '°（90° 单面阵的 canonical 几何）',
  )).join('');
  const surfaceRows = ['land', 'sea', 'coastal_uncertain'].map(surface => {
    const required = bucket?.required_distinct_site_count_by_surface?.[surface];
    const actual = bucket?.distinct_site_count_by_surface?.[surface];
    const voxels = bucket?.surface_class_counts?.[surface];
    if (required === undefined && actual === undefined && voxels === undefined) return '';
    return row(
      surfaceClassText(surface) + '（Radar 要求站址）',
      distinctSiteCountText(actual, required),
      voxels === undefined ? '' : '体元 ' + voxels,
    );
  }).join('');
  return '<h3>非合作监视（Radar）</h3>'
    + '<div class="parameter-note">' + escapeHtml(RADAR_DIRECTIONAL_GEOMETRY_NOTE)
    + ' 本卡只转印 canonical Radar 结果，Radar 参数与运行仍在'
    + '「雷达监视规划」面板中（本卡不提供第二套 Radar 编辑器）。</div>'
    + '<div class="coverage-card cns-radar-service" data-service-key="S:radar_noncooperative"'
    + ' data-radar-state="' + escapeHtml(radarStateText(item?.status, stale)) + '">'
    + '<b>' + escapeHtml(RADAR_TYPE_TEXT.radar_i) + ' / ' + escapeHtml(RADAR_TYPE_TEXT.radar_ii)
    + ' · ' + escapeHtml(radarStateText(item?.status, stale)) + '</b>'
    + '<span>Radar 模型状态：' + escapeHtml(radarStateText(item?.status, stale))
    + '（阶段 ' + escapeHtml(String(item?.stage_label || '未给出阶段')) + '）'
    + ' · 结果状态：' + escapeHtml(radarStateText(resultStatus || item?.status, stale))
    + (stale ? ' · <b>当前结果已过时，需重新运行</b>' : '') + '</span>'
    + row('Radar-I 面阵数', String(item?.radar_i_panel_count ?? '—'),
      '来自 backend canonical radar_surveillance_layout')
    + row('Radar-II 面阵数', String(item?.radar_ii_panel_count ?? '—'),
      '来自 backend canonical radar_surveillance_layout')
    + row('当前选中铁塔 / 面阵',
      String(towerIds.length || item?.selected_tower_count || 0) + ' 个站址 · '
      + String(item?.selected_panel_count ?? panels.length) + ' 个面阵',
      towerIds.slice(0, 6).join('、'))
    + row('Radar service 状态',
      bucket ? redundancyStatusText(bucket.status) : '证据不足',
      bucket ? '来自 P15 的 S:radar_noncooperative service 证据' : '尚未生成 Radar service 证据')
    + surfaceRows
    + panelRows
    + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
    + '</div>';
}

// ---- 4.5 双通道监视（RID / Radar 分开，绝不相加） ----------------------------

/** 双通道监视状态 → 业务中文。 */
export const DUAL_CHANNEL_STATUS_TEXT = {
  satisfied: '满足',
  confirmed_deficit: '未满足（存在监视缺口）',
  unknown: '证据不足 / 待确认',
  not_declared: '未要求',
};

/**
 * 双通道监视结论：**只**聚合后端给出的通道状态（最不利），绝不重算 provider。
 *
 * 未声明的通道（``null``）不参与判定；只要求一条通道时不把另一条当成缺口。
 */
export function dualChannelStatus(statuses) {
  const values = (statuses || []).map(value => String(value ?? '')).filter(Boolean);
  if (!values.length) return 'not_declared';
  if (values.includes('confirmed_deficit')) return 'confirmed_deficit';
  if (values.every(value => value === 'satisfied')) return 'satisfied';
  return 'unknown';
}

/** 监视通道模型：RID / Radar 各自的 canonical service 桶 + 双通道结论。 */
export function surveillanceChannelModel(flow) {
  const requirement = rounddRequiredServicesModel(flow);
  const buckets = aggregateServiceStatements(flow);
  const rid = buckets.find(item => item.service_key === 'S:rid_cooperative') || null;
  const radar = buckets.find(item => item.service_key === 'S:radar_noncooperative') || null;
  const statuses = [];
  if (requirement.ridRequired && rid) statuses.push(rid.status);
  if (requirement.radarRequired && radar) statuses.push(radar.status);
  return {
    rid, radar,
    ridRequired: requirement.ridRequired,
    radarRequired: requirement.radarRequired,
    allRequired: requirement.allRequired,
    dualStatus: dualChannelStatus(statuses),
  };
}

/** 【监视总览】三行：合作监视 / 非合作监视 / 双通道监视（**绝不相加**）。 */
export function renderSurveillanceDualChannelPanel(flow) {
  const model = surveillanceChannelModel(flow);
  const channelRow = (label, bucket, required) => {
    const status = required
      ? (bucket ? redundancyStatusText(bucket.status) : '证据不足')
      : '未要求';
    const note = bucket
      ? '体元 ' + String(bucket.voxel_count ?? 0)
        + ' · 满足 ' + String(bucket.status_counts?.satisfied ?? 0)
        + ' · 缺口 ' + String(bucket.status_counts?.confirmed_deficit ?? 0)
        + ' · 证据不足 ' + String(bucket.status_counts?.unknown ?? 0)
      : (required ? '尚未生成该通道的 service 证据' : '本项目未要求该服务');
    return row(label, status, note);
  };
  return '<h3>监视总览（双通道）</h3>'
    + '<div class="parameter-note">' + escapeHtml(SURVEILLANCE_DUAL_CHANNEL_NOTE) + '</div>'
    + '<div class="coverage-card cns-dual-channel" data-dual-status="'
    + escapeHtml(model.dualStatus) + '" data-all-required="' + String(model.allRequired) + '">'
    + channelRow(SURVEILLANCE_DUAL_CHANNEL_LABELS.rid, model.rid, model.ridRequired)
    + channelRow(SURVEILLANCE_DUAL_CHANNEL_LABELS.radar, model.radar, model.radarRequired)
    + row(SURVEILLANCE_DUAL_CHANNEL_LABELS.dual,
      DUAL_CHANNEL_STATUS_TEXT[model.dualStatus] || model.dualStatus,
      model.allRequired
        ? escapeHtml(ALL_REQUIRED_MODE_LABEL) + '：两条通道都必须满足'
        : '只按本项目实际要求的通道判定')
    + '<small>本区不做任何站点数相加：RID 与 Radar 的站址、面阵与覆盖证据一律分服务给出。</small>'
    + '</div>';
}

// ---- 4.6 P14：四服务服务走廊证据 ---------------------------------------------

/** 状态最不利排序（只用于把后端给出的状态排成"最差的那个"，不做判定）。 */
function worstServiceStatus(counts) {
  if (Number(counts?.confirmed_deficit || 0)) return 'confirmed_deficit';
  if (Number(counts?.unknown || 0)) return 'unknown';
  return 'satisfied';
}

/**
 * P14 逐体元明细 → (route × service) 聚合。
 *
 * 只计数与转印后端给出的 status / surface_class / distinct-site 证据 /
 * geometry·delivery 状态 / gap cause；**不**做任何距离或重数判定。
 * 明细缺失（未按需载入）时返回空数组。
 */
export function corridorServiceRows(flow) {
  const detail = flow?.cns_corridor_assessment?.detail || null;
  if (!detail) return [];
  const groups = new Map();
  for (const route of detail.routes || []) {
    for (const voxel of route.voxels || []) {
      for (const subsystem of voxel.subsystems || []) {
        for (const entry of subsystem.service_redundancy || []) {
          const serviceKey = String(entry?.service_key || '');
          if (!serviceKey) continue;
          const key = String(route.route_id) + '|' + serviceKey;
          const bucket = groups.get(key) || {
            routeId: route.route_id, serviceKey,
            voxelCount: 0,
            statusCounts: {satisfied: 0, confirmed_deficit: 0, unknown: 0},
            surfaceCounts: {}, geometryCounts: {}, deliveryCounts: {},
            causeCounts: {}, reasons: new Set(),
            requiredSite: null, actualSite: null,
            providerCount: 0, baselineM: null, dependencyOnly: false,
            recommendedDependencyService: null, panelTypes: {},
          };
          bucket.voxelCount += 1;
          const status = String(entry.status || 'unknown');
          if (Object.prototype.hasOwnProperty.call(bucket.statusCounts, status)) {
            bucket.statusCounts[status] += 1;
          }
          const surface = String(entry.surface_class || voxel.surface_class || 'unknown');
          bucket.surfaceCounts[surface] = Number(bucket.surfaceCounts[surface] || 0) + 1;
          const required = entry.required_distinct_site_count;
          const actual = entry.distinct_site_count;
          if (Number.isFinite(Number(required))) {
            bucket.requiredSite = bucket.requiredSite === null
              ? Number(required) : Math.max(bucket.requiredSite, Number(required));
          }
          if (Number.isFinite(Number(actual))) {
            bucket.actualSite = bucket.actualSite === null
              ? Number(actual) : Math.min(bucket.actualSite, Number(actual));
          }
          for (const cause of entry.gap_causes || []) {
            bucket.causeCounts[cause] = Number(bucket.causeCounts[cause] || 0) + 1;
          }
          for (const reason of entry.reasons || []) bucket.reasons.add(String(reason));
          for (const [name, value] of Object.entries(entry.geometry_status_counts || {})) {
            bucket.geometryCounts[name] = Number(bucket.geometryCounts[name] || 0) + Number(value || 0);
          }
          for (const [name, value] of Object.entries(entry.delivery_status_counts || {})) {
            bucket.deliveryCounts[name] = Number(bucket.deliveryCounts[name] || 0) + Number(value || 0);
          }
          if (!bucket.geometryCounts.satisfied && entry.geometry_status) {
            const name = String(entry.geometry_status);
            bucket.geometryCounts[name] = Number(bucket.geometryCounts[name] || 0) + 1;
          }
          if (entry.delivery_status) {
            const name = String(entry.delivery_status);
            bucket.deliveryCounts[name] = Number(bucket.deliveryCounts[name] || 0) + 1;
          }
          const providers = Array.isArray(entry.providers) ? entry.providers : [];
          bucket.providerCount += providers.length;
          for (const provider of providers) {
            const type = String(provider?.radar_type || '').trim();
            if (type) bucket.panelTypes[type] = Number(bucket.panelTypes[type] || 0) + 1;
          }
          if (Number.isFinite(Number(entry.max_reference_baseline_m))) {
            bucket.baselineM = Number(entry.max_reference_baseline_m);
          }
          if (entry.dependency_only === true) bucket.dependencyOnly = true;
          if (entry.recommended_dependency_service) {
            bucket.recommendedDependencyService = String(entry.recommended_dependency_service);
          }
          groups.set(key, bucket);
        }
      }
    }
  }
  return [...groups.values()].map(bucket => ({
    ...bucket,
    status: worstServiceStatus(bucket.statusCounts),
    reasons: [...bucket.reasons].sort(),
  }));
}

/** P14 service 级证据区块（四服务分列；明细需按需载入后才有内容）。 */
export function renderCorridorServiceEvidence(flow) {
  const rows = corridorServiceRows(flow);
  const head = '<div class="parameter-note">服务级证据按 route × service 分列：'
    + '每个服务各自给出 status、满足 / 缺口 / 证据不足体元数、'
    + 'distinct-site 证据与原因，绝不跨服务相加。</div>';
  if (!rows.length) {
    return head + '<div class="parameter-note">尚无可用的逐体元 service 证据'
      + '（P14 明细需按需载入，或本项目仍是旧版子系统口径）。</div>';
  }
  const cards = rows.slice(0, 24).map(bucket => {
    const statusTextValue = redundancyStatusText(bucket.status);
    const surfaceRows = ['land', 'sea', 'coastal_uncertain', 'unknown'].map(surface => {
      const count = bucket.surfaceCounts[surface];
      if (count === undefined) return '';
      return row(surfaceClassText(surface), String(count) + ' 个体元', '');
    }).join('');
    const navigationRows = bucket.serviceKey === 'N:rtk_augmentation'
      ? row('参考站几何（工程基线）',
        navigationGeometryStatusText(worstServiceStatus(bucket.geometryCounts)),
        '基线距离 ' + navigationBaselineText(bucket.baselineM)
        + ' · ' + distinctSiteCountText(bucket.actualSite, bucket.requiredSite))
        + row('通信修正数据交付',
          navigationDeliveryStatusText(worstServiceStatus(bucket.deliveryCounts)),
          Object.entries(bucket.deliveryCounts).map(([name, count]) =>
            navigationDeliveryStatusText(name) + ' ' + count).join(' · ') || '尚无交付证据')
        + Object.entries(bucket.causeCounts).map(([cause, count]) => row(
          '缺口原因', navigationGapCauseText(cause), count + ' 个体元')).join('')
        + (bucket.dependencyOnly
          ? '<div class="coverage-card cns-dependency-only"><b>无需导航建站，仅需通信补盲</b>'
            + '<span>该路由的导航增强结果只依赖 RTK 修正数据的通信交付：站址几何已满足，'
            + '应优先补通信服务。</span></div>'
          : '')
      : '';
    const radarRows = bucket.serviceKey === 'S:radar_noncooperative'
      ? row('方向性面阵证据',
        bucket.providerCount + ' 个 provider',
        Object.entries(bucket.panelTypes).map(([type, count]) =>
          (RADAR_TYPE_TEXT[type] || type) + ' × ' + count).join(' · ') || '无 panel 证据')
        + row('要求站址（按 surface 的 canonical 数字）',
          distinctSiteCountText(bucket.actualSite, bucket.requiredSite), RADAR_DIRECTIONAL_GEOMETRY_NOTE)
      : '';
    return '<div class="coverage-card cns-corridor-service" data-service-key="'
      + escapeHtml(bucket.serviceKey) + '" data-route-id="' + escapeHtml(String(bucket.routeId)) + '">'
      + '<b>' + escapeHtml(String(bucket.routeId)) + ' · '
      + escapeHtml(cnsServiceFormalLabel(bucket.serviceKey)) + ' · ' + escapeHtml(statusTextValue) + '</b>'
      + '<span>体元 ' + bucket.voxelCount
      + ' · 满足 ' + bucket.statusCounts.satisfied
      + ' · 已确认缺口 ' + bucket.statusCounts.confirmed_deficit
      + ' · 证据不足 ' + bucket.statusCounts.unknown + '</span>'
      + navigationRows + radarRows + surfaceRows
      + (bucket.reasons.length
        ? '<small>原因：' + escapeHtml(bucket.reasons.map(navigationReasonText).join('；')) + '</small>'
        : '')
      + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
      + '</div>';
  }).join('');
  return head + cards
    + (rows.length > 24 ? '<div class="empty-note">另有 ' + (rows.length - 24) + ' 组 route × service 证据</div>' : '');
}
