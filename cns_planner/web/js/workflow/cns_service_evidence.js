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
  CNS_FOCUS_SERVICES, CNS_GAP_COLORS, CNS_GAP_STATE_LABELS, CNS_SERVICE_AUTHORITY_NOTE,
  REDUNDANCY_STATUS_TEXT, SERVICE_KEY_LABELS, SERVICE_KEY_SHORT_LABELS, SERVICE_STATUS_TEXT,
  SURFACE_CLASS_TEXT, combinedStatusText, distinctSiteCountText, gapStateText,
  isSurfaceAwareServiceKey, redundancyStatusText, requiredSiteCountText,
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
        };
        bucket.voxel_count += Number(entry?.voxel_count || 0);
        for (const [status, count] of Object.entries(entry?.status_counts || {})) {
          bucket.status_counts[status] = (bucket.status_counts[status] || 0) + Number(count || 0);
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
    return '<div class="coverage-card cns-service-statement" data-service-key="'
      + escapeHtml(statement.service_key) + '" data-service-status="'
      + escapeHtml(statement.status) + '">'
      + '<b>' + escapeHtml(serviceKeyLabel(statement.service_key))
      + ' · ' + escapeHtml(backendServiceStatusText(statement.status)) + '</b>'
      + '<span>体元 ' + statement.voxel_count
      + ' · 满足 ' + satisfiedVoxels
      + ' · 冗余不足 ' + deficitVoxels
      + ' · 证据不足 ' + unknownVoxels + '</span>'
      + surfaceCountRows(statement)
      + surfaceStatusRows(statement)
      + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
      + '</div>';
  }).join('');
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
    return '<div class="coverage-card cns-facility-action" data-service-key="'
      + escapeHtml(String(action?.service_key || '')) + '">'
      + '<b>' + escapeHtml(facilityTargetTitle(action))
      + (Number.isFinite(required) && Number.isFinite(current)
        ? ' · ' + escapeHtml(distinctSiteCountText(current, required)) : '') + '</b>'
      + '<span>建议：' + escapeHtml(String(action?.host?.host_tower_name || ''))
      + (action?.host?.host_tower_id ? '（' + escapeHtml(String(action.host.host_tower_id)) + '）' : '')
      + ' · 新增 ' + escapeHtml(String(action?.device_id || '设备'))
      + ' · 服务 ' + escapeHtml(serviceKeyLabel(action?.service_key)) + '</span>'
      + afterClaim
      + (action?.surface_class
        ? '<span>surface_class：' + escapeHtml(surfaceClassText(action.surface_class)) + '</span>' : '')
      + (note ? '<span class="cns-colocation-note">' + escapeHtml(note) + '</span>' : '')
      + (action?.subsystem_mount_status === 'declared_not_compatible'
        ? '<span>分系统安装：站点声明的可用分系统不包含该设备分系统</span>' : '')
      + '<span>状态：' + escapeHtml(backendServiceStatusText(action?.status)) + '</span>'
      + '<small>' + escapeHtml(CNS_SERVICE_AUTHORITY_NOTE) + '</small>'
      + '<small>审计标识：' + escapeHtml(String(action?.action_id || '—')) + '</small>'
      + '</div>';
  }).join('');
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
