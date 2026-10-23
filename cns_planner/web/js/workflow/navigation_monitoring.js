// =========================================================
// 导航服务监测规划（Round38-N2）
//
// 职责：把 N1 的**两个只读接口**转印成中文业务面板 + 地图图层模型。
//
// 铁律（本轮全部断言都围绕它们）：
//  * 前端**不重新实现选址算法**：一切结论来自
//    ``POST /api/navigation-monitoring-simulation`` 的响应字段；
//  * ``simulation_planned`` / ``reused_existing`` / ``blocked`` / ``stale`` /
//    ``unknown`` / ``not_evaluated`` 六种状态**严格区分**，绝不互相冒充；
//  * 2000 m 是**沿航路中心线的监测关联半径**，与固定 1000 m CNS 保障宽带
//    （左右各 500 m）是两件不同的事；关联率**不得**替代全宽覆盖率；
//  * 缺失不等于 0、``unknown`` 不等于 ``pass``、``not_evaluated`` 不等于通过；
//  * 本模块只读：不写 ProjectState、不调用任何写接口、不触发任何评估器。
//
// 本模块导出纯函数（模型 + HTML）以便前端测试直接断言，不依赖浏览器。
// =========================================================
import {escapeHtml,statusBadge} from './common.js';

/** N1 只读接口（与后端 router 登记一致；本模块不新增任何端点）。 */
export const SHARED_SITE_CANDIDATES_ENDPOINT = '/api/cns-shared-site-candidates';
export const NAVIGATION_MONITORING_SIMULATION_ENDPOINT = '/api/navigation-monitoring-simulation';
export const FIXED_CORRIDOR_GEOMETRY_ENDPOINT = '/api/fixed-corridor-geometry';

// ---- 1. 航路权威身份（route authority）与几何指纹 ---------------------------

/** route_source 白名单（与后端一致）：工程演示 vs 正式运行航路。 */
export const ROUTE_SOURCE_LABEL = {
  engineering_demo: '工程演示航路',
  operational_route: '正式运行航路',
};

/** route_authority → 中文。前端**只转印**后端 authority，不自行判定权威性。 */
export const ROUTE_AUTHORITY_LABEL = {
  engineering_demo_projection: '工程演示采纳投影（非正式运行航路）',
  canonical_operational_route: '正式运行航路（canonical）',
};

export function routeAuthorityText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return ROUTE_AUTHORITY_LABEL[key] || key;
}

export function routeSourceText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return ROUTE_SOURCE_LABEL[key] || key;
}

/**
 * 航路身份摘要：**严格区分** authority 与完整几何指纹。
 *
 * 同一 route_id 在不同权威来源 / 不同几何下是**不同的**航路；界面必须同时显示
 * authority 与几何指纹，绝不能只显示 route_id 就让人以为"同一条航路"。
 */
export function routeIdentityModel(identity) {
  const value = identity && typeof identity === 'object' ? identity : {};
  return {
    routeId: value.route_id ?? null,
    routeAuthority: value.route_authority ?? null,
    routeAuthorityText: routeAuthorityText(value.route_authority),
    routeGeometryFingerprint: value.route_geometry_fingerprint ?? null,
    pointCount: value.point_count ?? null,
    currentness: value.currentness ?? null,
    sourceCrs: value.source_crs ?? null,
    constructionCrs: value.construction_crs ?? null,
    adoptionId: value.adoption_id ?? null,
    candidateId: value.candidate_id ?? null,
    candidateFingerprint: value.candidate_fingerprint ?? null,
    validationId: value.validation_id ?? null,
    fingerprintPrefix: fingerprintPrefixOf(value.route_geometry_fingerprint),
  };
}

/** 指纹前缀（``layeredpathv1-`` 等）：用于界面辨认身份家族，不做任何比对。 */
export function fingerprintPrefixOf(value) {
  const text = String(value ?? '');
  const index = text.indexOf('-');
  return index > 0 ? text.slice(0, index + 1) : '';
}

/** 指纹短显：只用于**显示**；比对永远用完整指纹。 */
export function shortFingerprint(value, length = 12) {
  const text = String(value ?? '');
  if (!text) return '—';
  return text.length <= length ? text : text.slice(0, length) + '…';
}

/**
 * 同一航路不同几何不得混用：给定当前权威身份与请求身份，判定是否一致。
 *
 * 返回 ``{consistent, reason}``。任何一项缺失都**不**判一致（fail-closed）。
 */
export function routeIdentityConsistency(authoritative, requested) {
  const left = routeIdentityModel(authoritative);
  const right = routeIdentityModel(requested);
  if (!left.routeGeometryFingerprint) {
    return {consistent: false, reason: 'authoritative_geometry_fingerprint_missing'};
  }
  if (!right.routeGeometryFingerprint) {
    return {consistent: false, reason: 'requested_geometry_fingerprint_missing'};
  }
  if (left.routeGeometryFingerprint !== right.routeGeometryFingerprint) {
    return {consistent: false, reason: 'route_geometry_fingerprint_mismatch'};
  }
  if (left.routeAuthority && right.routeAuthority && left.routeAuthority !== right.routeAuthority) {
    return {consistent: false, reason: 'route_authority_mismatch'};
  }
  return {consistent: true, reason: null};
}

// ---- 2. 导航参数（与后端默认值一致） ---------------------------------------

/**
 * 导航服务监测参数默认值与**可否关闭**语义。
 *
 * ``force_endpoint_monitors`` 是 ``fixed_constraint``：后端对 false/null/非布尔
 * 一律返回 400「强制端点导航服务监测设备不可关闭」。因此前端把它做成**只读**，
 * 不提供关闭入口 —— 而不是让用户改完再被后端拒绝。
 */
export const NAVIGATION_PARAMETER_DEFAULTS = Object.freeze({
  monitor_association_radius_m: 2000,
  gap_tolerance_s: 10,
  route_speed_mps: 15,
  risk_priority_weight: 3,
  max_optional_monitor_sites: 4,
  force_endpoint_monitors: true,
});

/** 参数元数据：单位、中文名、是否用户可编辑、来源语义。 */
export const NAVIGATION_PARAMETER_FIELDS = Object.freeze([
  {
    key: 'monitor_association_radius_m', label: '关联半径', unit: 'm',
    editable: true, min: 1, step: 100,
    note: '沿航路中心线的监测关联半径；**不是**左右各 500 m 的全宽 CNS 保障宽度。',
  },
  {
    key: 'gap_tolerance_s', label: '缺口容忍时间', unit: 's',
    editable: true, min: 1, step: 1,
    note: '每个缺口共用的用户容忍时间；风险画像只改选址优先级，不改这个阈值。',
  },
  {
    key: 'route_speed_mps', label: '航路速度', unit: 'm/s',
    editable: true, min: 1, step: 1,
    note: '未给出可靠分段速度时，缺口经过时间按此假设速度计算。',
  },
  {
    key: 'risk_priority_weight', label: '风险权重', unit: '',
    editable: true, min: 1, step: 1,
    note: '只影响高风险航段的选址优先级；不改变缺口判定阈值。',
  },
  {
    key: 'max_optional_monitor_sites', label: '可选站址上限', unit: '个',
    editable: true, min: 0, step: 1, integer: true,
    note: '在强制端点设备之外，最多再选多少个可选监测站址。',
  },
  {
    key: 'force_endpoint_monitors', label: '起终点监测设备强制规划', unit: '',
    editable: false,
    note: '固定约束：起终点监测设备不可关闭。',
  },
]);

export const FORCE_ENDPOINT_NOTE = '起终点监测设备为强制规划项，不可关闭。';

/** 参数来源 → 中文（后端 ``parameter_sources``：default / request / fixed_constraint）。 */
export const PARAMETER_SOURCE_TEXT = {
  default: '默认值',
  request: '本次请求',
  fixed_constraint: '固定约束',
};

export function parameterSourceText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return PARAMETER_SOURCE_TEXT[key] || key;
}

/** 数值显示：缺失一律「—」，绝不用 0 冒充证据。 */
export function metricText(value, digits = 2, unit = '') {
  const number = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(number)) {
    return '—';
  }
  const text = digits > 0 ? number.toFixed(digits) : String(Math.round(number));
  return unit ? text + ' ' + unit : text;
}

export function percentText(value, digits = 2) {
  const number = Number(value);
  if (value === null || value === undefined || !Number.isFinite(number)) return '—';
  return (number * 100).toFixed(digits) + '%';
}

/** 从界面表单读取参数草稿（只产出后端已登记的键，绝不新增未知键）。 */
export function navigationParameterPayload(draft = {}) {
  const payload = {};
  for (const field of NAVIGATION_PARAMETER_FIELDS) {
    if (!field.editable) continue;
    const raw = draft[field.key];
    if (raw === '' || raw === null || raw === undefined) continue;
    const number = Number(raw);
    if (!Number.isFinite(number)) continue;
    payload[field.key] = field.integer ? Math.trunc(number) : number;
  }
  return payload;
}

/** 参数回填：以后端返回的 ``parameters`` 为准，缺失时用默认值。 */
export function navigationParameterDraft(parameters) {
  const value = parameters && typeof parameters === 'object' ? parameters : {};
  const draft = {};
  for (const field of NAVIGATION_PARAMETER_FIELDS) {
    const current = value[field.key];
    draft[field.key] = current === null || current === undefined
      ? NAVIGATION_PARAMETER_DEFAULTS[field.key]
      : current;
  }
  draft.force_endpoint_monitors = true;
  return draft;
}

// ---- 3. 候选站址（全部已录入起降点 / 既有铁塔 / 不可用候选） ----------------

/** 候选种类 → 中文。 */
export const CANDIDATE_KIND_TEXT = {
  project_node: '工程节点',
  reference_landing_site: '参考起降点',
  tower_colocation: '既有铁塔',
};

/** 空间阻断原因 → 中文（与后端 ``spatial_block_reason`` 取值一致）。 */
export const SPATIAL_BLOCK_REASON_TEXT = {
  reference_source_crs_unresolved: '参考数据源坐标系尚未确认（CRS 未裁定）',
  reference_coordinate_missing: '参考记录缺少坐标',
  reference_coordinate_invalid: '参考记录坐标非法',
  reference_coordinate_quality_unusable: '参考记录坐标质量不可用',
  tower_source_crs_unresolved: '铁塔数据源坐标系尚未确认（CRS 未裁定）',
  tower_coordinate_missing: '铁塔记录缺少坐标',
  tower_coordinate_invalid: '铁塔记录坐标非法',
  tower_coordinate_quality_unusable: '铁塔记录坐标质量不可用',
};

export function spatialBlockReasonText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return SPATIAL_BLOCK_REASON_TEXT[key] || key;
}

export function candidateKindText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return CANDIDATE_KIND_TEXT[key] || key;
}

/**
 * 候选站址模型：全部已录入起降点、既有铁塔，以及**不可用候选的数量与原因**。
 *
 * 只读 ``GET /api/cns-shared-site-candidates`` 的字段，不重新判定空间可用性。
 */
export function sharedSiteCandidateModel(snapshot) {
  const value = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const candidates = Array.isArray(value.candidates) ? value.candidates : [];
  const spatial = Array.isArray(value.spatial_candidates) ? value.spatial_candidates : [];
  const spatialIds = new Set(spatial.map(item => String(item?.candidate_id ?? '')));
  const blocked = candidates.filter(item => !spatialIds.has(String(item?.candidate_id ?? '')));
  const reasons = value.reason_counts && typeof value.reason_counts === 'object'
    ? value.reason_counts : {};
  return {
    status: value.status ?? null,
    available: candidates.length > 0 || Object.keys(value).length > 0,
    counts: value.counts && typeof value.counts === 'object' ? value.counts : {},
    sourceCounts: value.source_counts && typeof value.source_counts === 'object'
      ? value.source_counts : {},
    candidates,
    spatialCandidates: spatial,
    blockedCandidates: blocked,
    reasonCounts: reasons,
    reasonRows: Object.entries(reasons)
      .map(([reason, count]) => ({
        reason, count: Number(count),
        text: spatialBlockReasonText(reason),
      }))
      .sort((left, right) => left.reason.localeCompare(right.reason)),
    corridorWidthPolicyVersion: value.corridor_width_policy_version ?? null,
    corridorHalfWidthM: value.corridor_half_width_m ?? null,
    fullWidthServiceAssuranceStatus: value.full_width_service_assurance_status ?? 'not_evaluated',
    candidateSemantics: value.candidate_semantics ?? null,
  };
}

// ---- 4. 仿真结果（设备来源 / 关联 / 缺口） ---------------------------------

/** 设备供给来源 → 中文：``simulation_planned`` 与 ``reused_existing`` 严格区分。 */
export const PROVISIONING_TEXT = {
  simulation_planned: '仿真规划（尚未建成）',
  reused_existing: '复用既有设备',
};

export function provisioningText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return PROVISIONING_TEXT[key] || key;
}

/** 仿真总状态 → 中文。``blocked`` 不是错误，是如实阻断。 */
export const SIMULATION_STATUS_TEXT = {
  passed: '通过（全部缺口在容忍时间内）',
  failed: '未通过（存在超出容忍时间的缺口）',
  undetermined: '不可判定（缺口经过时间证据不足）',
  blocked: '已阻断（起终点空间证据不足）',
};

export function simulationStatusText(value) {
  const key = String(value ?? '');
  if (!key) return '未运行';
  return SIMULATION_STATUS_TEXT[key] || key;
}

/** 阻断原因 → 中文。 */
export const SIMULATION_BLOCK_REASON_TEXT = {
  mandatory_endpoint_spatial_evidence_unavailable: '起终点监测设备缺少可用的空间证据',
  continuous_gap_tolerance_not_satisfied: '存在超出容忍时间的连续缺口',
};

export function simulationBlockReasonText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return SIMULATION_BLOCK_REASON_TEXT[key] || key;
}

/** 缺口容忍判定三态 → 中文（``undetermined`` 绝不写成通过）。 */
export const TOLERANCE_STATUS_TEXT = {
  satisfied: '在容忍时间内',
  exceeded: '超出容忍时间',
  undetermined: '不可判定（速度证据不足）',
};

export function toleranceStatusText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return TOLERANCE_STATUS_TEXT[key] || key;
}

/** 强制端点要求的状态 → 中文。 */
export const MANDATORY_STATUS_TEXT = {
  satisfied: '已满足',
  blocked: '已阻断（缺少空间证据）',
};

export function mandatoryStatusText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return MANDATORY_STATUS_TEXT[key] || key;
}

/** 端点位置 → 中文。 */
export const ENDPOINT_POSITION_TEXT = {start: '起点', end: '终点'};

export function endpointPositionText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return ENDPOINT_POSITION_TEXT[key] || key;
}

/** 选址停止原因 → 中文。 */
export const SELECTION_STOP_REASON_TEXT = {
  optional_site_limit_reached: '已达可选站址上限',
  no_positive_incremental_association: '没有正增量关联（不再增选）',
  candidate_pool_exhausted: '候选池已用尽',
};

export function selectionStopReasonText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return SELECTION_STOP_REASON_TEXT[key] || key;
}

/** 风险画像身份核查状态 → 中文（``unavailable`` 必须显式标注）。 */
export const RISK_PRIORITY_STATUS_TEXT = {
  available: '已应用风险优先排序',
  unavailable: '未应用风险优先排序',
};

export const RISK_UNAVAILABLE_REASON_TEXT = {
  current_candidate_unavailable: '当前候选航路明细不可用',
  candidate_not_current: '候选航路不是 current',
  candidate_fingerprint_mismatch: '候选航路指纹不一致',
  candidate_geometry_fingerprint_mismatch: '候选航路几何指纹与航路不一致',
  current_risk_profile_unavailable: '没有 current 的风险画像',
  candidate_id_mismatch: '风险画像的候选身份与航路不一致',
  route_id_mismatch: '风险画像的航路身份与航路不一致',
  risk_geometry_fingerprint_mismatch: '风险画像几何指纹与航路不一致',
};

export function riskPriorityStatusText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return RISK_PRIORITY_STATUS_TEXT[key] || key;
}

export function riskUnavailableReasonText(value) {
  const key = String(value ?? '');
  if (!key) return '—';
  return RISK_UNAVAILABLE_REASON_TEXT[key] || key;
}

/** 三维保障状态 → 中文（``not_evaluated`` 不等于通过）。 */
export const THREE_DIMENSIONAL_STATUS_TEXT = {
  available: '可用',
  not_evaluated: '未评估',
};

export function threeDimensionalStatusText(value) {
  const key = String(value ?? '');
  if (!key) return 'not_evaluated';
  return THREE_DIMENSIONAL_STATUS_TEXT[key] || key;
}

/**
 * 仿真结果模型：把响应转印成界面需要的摘要 + 站址明细 + 缺口明细。
 *
 * **不重算任何数值**：关联里程 / 关联比例 / 最长缺口 / 最长缺口时间 / 设备数量
 * 全部直接取后端字段；缺失一律 ``null``（界面显示「—」）。
 */
export function navigationSimulationModel(result) {
  const value = result && typeof result === 'object' ? result : {};
  const devices = Array.isArray(value.devices) ? value.devices : [];
  const gaps = Array.isArray(value.gaps) ? value.gaps : [];
  const mandatory = Array.isArray(value.mandatory_requirements) ? value.mandatory_requirements : [];
  const optional = Array.isArray(value.selected_optional_sites) ? value.selected_optional_sites : [];
  const intervals = Array.isArray(value.merged_intervals_m) ? value.merged_intervals_m : [];
  const byId = new Map();
  for (const device of devices) {
    const siteId = String(device?.site_id ?? '');
    if (siteId && !byId.has(siteId)) byId.set(siteId, device);
  }
  const mandatoryIds = new Set(
    (Array.isArray(value.selected_mandatory_site_ids) ? value.selected_mandatory_site_ids : [])
      .map(item => String(item)),
  );
  const optionalIds = new Set(optional.map(item => String(item?.site_id ?? '')));

  return {
    ran: Object.keys(value).length > 0,
    status: value.status ?? null,
    statusText: simulationStatusText(value.status),
    reason: value.reason ?? null,
    reasonText: value.reason === null || value.reason === undefined
      ? null : simulationBlockReasonText(value.reason),
    routeIdentity: routeIdentityModel(value.route_identity),
    parameters: value.parameters ?? null,
    parameterSources: value.parameters?.parameter_sources ?? {},
    parameterFingerprint: value.parameter_fingerprint ?? null,
    resultFingerprint: value.result_fingerprint ?? null,
    candidateSnapshotFingerprint: value.candidate_snapshot_fingerprint ?? null,
    candidateCounts: value.candidate_counts ?? {},
    //: 固定宽带契约：恒来自后端，前端**不构造**第二套保障宽度。
    corridorWidthPolicyVersion: value.corridor_width_policy_version ?? null,
    corridorHalfWidthM: value.corridor_half_width_m ?? null,
    fullWidthServiceAssuranceStatus: value.full_width_service_assurance_status ?? 'not_evaluated',
    semanticDisclaimer: value.semantic_disclaimer ?? null,
    //: 关联里程 / 关联比例 / 最长缺口 / 最长缺口时间：直接取后端字段。
    associatedLengthM: value.associated_length_m ?? null,
    associatedRatio: value.associated_ratio ?? null,
    routeLengthM: value.route_length_m ?? null,
    riskAssociatedLengthM: value.risk_associated_length_m ?? null,
    longestGapM: value.longest_gap_m ?? null,
    longestGapDurationS: value.longest_gap_duration_s ?? null,
    allGapsWithinTolerance: value.all_gaps_within_tolerance ?? null,
    mergedIntervals: intervals,
    gaps,
    gapCount: gaps.length,
    //: 风险画像身份核查。
    riskPriorityStatus: value.risk_priority_status ?? null,
    riskPriorityStatusText: riskPriorityStatusText(value.risk_priority_status),
    riskPriorityApplied: value.risk_priority_status === 'available',
    riskIdentity: value.risk_identity ?? null,
    riskUnavailableReasons: Array.isArray(value.risk_unavailable_reasons)
      ? value.risk_unavailable_reasons : [],
    riskUnavailableRows: (Array.isArray(value.risk_unavailable_reasons)
      ? value.risk_unavailable_reasons : []).map(reason => ({
        reason, text: riskUnavailableReasonText(reason),
      })),
    //: 三维保障与写入声明。
    threeDimensionalAssuranceStatus: value.three_dimensional_assurance_status ?? 'not_evaluated',
    threeDimensionalAssuranceStatusText: threeDimensionalStatusText(
      value.three_dimensional_assurance_status,
    ),
    threeDimensionalAssuranceReason: value.three_dimensional_assurance_reason ?? null,
    routeElevationUsedForAssociation: value.route_elevation_used_for_association === true,
    writesPerformed: value.writes_performed ?? null,
    //: 设备与站址明细。
    devices,
    deviceCount: devices.length,
    mandatoryRequirements: mandatory,
    mandatoryRows: mandatory.map(item => ({
      position: item?.position ?? null,
      positionText: endpointPositionText(item?.position),
      siteId: item?.site_id ?? null,
      status: item?.status ?? null,
      statusText: mandatoryStatusText(item?.status),
      reason: item?.reason ?? null,
      device: byId.get(String(item?.site_id ?? '')) ?? null,
    })),
    selectedOptionalSites: optional,
    optionalRows: optional.map(item => ({
      siteId: item?.site_id ?? null,
      gainScore: item?.gain_score ?? null,
      device: byId.get(String(item?.site_id ?? '')) ?? null,
    })),
    prunedOptionalSiteIds: Array.isArray(value.pruned_optional_site_ids)
      ? value.pruned_optional_site_ids : [],
    unselectedCandidateIds: Array.isArray(value.unselected_candidate_ids)
      ? value.unselected_candidate_ids : [],
    selectionStopReason: value.selection_stop_reason ?? null,
    selectionStopReasonText: value.selection_stop_reason === null
      || value.selection_stop_reason === undefined
      ? null : selectionStopReasonText(value.selection_stop_reason),
    //: 站址明细：合并设备与身份，供"点击站址看来源"使用。
    siteRows: buildSiteRows({devices, mandatoryIds, optionalIds, byId}),
    //: 缺口明细：供"点击缺口看长度 / 时间 / 容忍判定"使用。
    gapRows: gaps.map((gap, index) => ({
      index,
      startM: gap?.start_m ?? null,
      endM: gap?.end_m ?? null,
      lengthM: gap?.length_m ?? null,
      durationS: gap?.duration_s ?? null,
      speedSource: gap?.speed_source ?? null,
      gapToleranceS: gap?.gap_tolerance_s ?? null,
      toleranceStatus: gap?.tolerance_status ?? null,
      toleranceStatusText: toleranceStatusText(gap?.tolerance_status),
    })),
  };
}

/** 站址明细行：区分强制 / 可选，并保留后端设备来源。 */
export function buildSiteRows({devices = [], mandatoryIds = new Set(), optionalIds = new Set(), byId = new Map()} = {}) {
  const rows = [];
  const seen = new Set();
  const push = (siteId, role) => {
    const key = String(siteId ?? '');
    if (!key || seen.has(key)) return;
    seen.add(key);
    const device = byId.get(key) ?? null;
    rows.push({
      siteId: key,
      role,
      roleText: role === 'mandatory' ? '起终点强制设备' : '可选监测站址',
      provisioning: device?.provisioning ?? null,
      provisioningText: device ? provisioningText(device.provisioning) : '—',
      deviceId: device?.device_id ?? null,
      serviceKey: device?.service_key ?? null,
    });
  };
  for (const siteId of mandatoryIds) push(siteId, 'mandatory');
  for (const siteId of optionalIds) push(siteId, 'optional');
  for (const device of devices) push(device?.site_id, 'optional');
  return rows;
}

// ---- 5. 固定宽带（W1A/W1B 正式业务契约）与全宽状态 --------------------------

/**
 * 固定宽带几何的可用性判定。
 *
 * **只有**后端返回 ``currentness === 'current'`` 且几何存在时，才允许把该几何
 * 当作当前固定保障边界显示。其它情形一律缺证 / stale，绝不在前端自行构造另一套
 * 保障宽度。
 */
export function fixedCorridorGeometryModel(payload) {
  const value = payload && typeof payload === 'object' ? payload : {};
  const current = value.currentness === 'current';
  const hasGeometry = Boolean(value.geometry)
    && ['Polygon', 'MultiPolygon'].includes(String(value.geometry?.type ?? ''));
  return {
    available: Boolean(value.status) && Object.keys(value).length > 0,
    status: value.status ?? null,
    reason: value.reason ?? null,
    currentness: value.currentness ?? null,
    currentnessReason: value.currentness_reason ?? null,
    drawable: current && hasGeometry,
    routeId: value.route_id ?? null,
    widthPolicyVersion: value.width_policy_version ?? null,
    corridorHalfWidthM: value.corridor_half_width_m ?? null,
    corridorTotalWidthM: value.corridor_total_width_m ?? null,
    geometryFingerprint: value.geometry_fingerprint ?? null,
    geometry: hasGeometry ? value.geometry : null,
    disclaimer: value.disclaimer ?? null,
    semantics: value.semantics ?? null,
    identity: value.geometry_identity ?? null,
  };
}

/**
 * 全宽 CNS 服务状态。
 *
 * 铁律：**只有** P14/P15 结果 current **且**身份匹配时，才允许给出有效结论。
 * 否则恒为 ``not_evaluated``，并且必须显式声明"N1 关联率不得替代全宽覆盖率"。
 */
export function fullWidthServiceStatusModel({corridorStatus = null, corridorCurrent = false, identityMatched = false} = {}) {
  const evaluated = Boolean(corridorCurrent && identityMatched);
  return {
    evaluated,
    status: evaluated ? (corridorStatus ?? 'not_evaluated') : 'not_evaluated',
    reason: evaluated ? null : (
      !corridorCurrent ? 'corridor_result_not_current' : 'corridor_identity_not_matched'
    ),
    note: evaluated
      ? '全宽 CNS 服务状态来自当前有效的 P14/P15 结果。'
      : 'P14/P15 结果不是 current 或身份不匹配：全宽 CNS 服务状态保持「未评估」，不得用导航关联率替代。',
  };
}

/** 中心线监测关联 与 固定保障宽带 的分离声明（必须逐字出现在界面上）。 */
export const WIDTH_VS_ASSOCIATION_NOTE =
  '中心线监测关联（关联半径 2000 m）与固定 1000 m CNS 保障宽带（左右各 500 m）是两件不同的事：'
  + '导航关联率**不得**替代全宽覆盖率。2000 m 关联范围不是真实认证导航覆盖区。';

/** 起降点 CRS 未确认时的阻断文案（逐字）。 */
export const REFERENCE_CRS_BLOCKED_TEXT = '起降点坐标系尚未确认，无法完成权威导航服务监测仿真。';

/** 测试夹具必须显式标注，且结果不得冒充权威结果。 */
export const TEST_FIXTURE_LABEL = '测试夹具（非权威结果，不得写回或冒充正式结果）';

// ---- 6. 面板 HTML -----------------------------------------------------------

function badge(status, fallback = '未计算') {
  return statusBadge(status, fallback);
}

/** 摘要卡：关联里程 / 关联比例 / 最长缺口 / 最长缺口时间 / 可选设备数量。 */
export function navigationSimulationSummaryHtml(model) {
  if (!model || !model.ran) {
    return '<div class="wb-empty">尚未运行导航服务监测仿真。</div>';
  }
  const cards = [
    ['关联里程', metricText(model.associatedLengthM, 2, 'm')],
    ['关联比例', percentText(model.associatedRatio)],
    ['航路长度', metricText(model.routeLengthM, 2, 'm')],
    ['最长缺口', metricText(model.longestGapM, 2, 'm')],
    ['最长缺口时间', metricText(model.longestGapDurationS, 2, 's')],
    ['缺口数量', metricText(model.gapCount, 0, '个')],
    ['可选设备数量', metricText(model.optionalRows.length, 0, '个')],
    ['设备总数', metricText(model.deviceCount, 0, '个')],
  ];
  return '<div class="metric-grid">'
    + cards.map(([label, value]) =>
      '<b>' + escapeHtml(value) + '<small>' + escapeHtml(label) + '</small></b>').join('')
    + '</div>';
}

/** 结果摘要（状态 + 原因 + 风险画像 + 三维保障 + 写入声明）。 */
export function navigationSimulationStatusHtml(model) {
  if (!model || !model.ran) return '';
  const rows = [];
  rows.push('<div class="list-row"><span><b>仿真结论</b></span><small>'
    + escapeHtml(model.statusText) + '</small></div>');
  if (model.reasonText) {
    rows.push('<div class="list-row"><span><b>结论原因</b></span><small>'
      + escapeHtml(model.reasonText) + '</small></div>');
  }
  rows.push('<div class="list-row"><span><b>风险优先排序</b><small>'
    + escapeHtml(model.riskPriorityApplied ? '风险画像身份核查通过' : '未应用风险优先排序')
    + '</small></span><small>' + escapeHtml(model.riskPriorityStatusText) + '</small></div>');
  for (const row of model.riskUnavailableRows) {
    rows.push('<div class="list-row"><span><b>未应用原因</b></span><small>'
      + escapeHtml(row.text) + '</small></div>');
  }
  rows.push('<div class="list-row"><span><b>三维保障</b></span><small>'
    + escapeHtml(model.threeDimensionalAssuranceStatusText) + '</small></div>');
  rows.push('<div class="list-row"><span><b>关联使用航路高程</b></span><small>'
    + escapeHtml(model.routeElevationUsedForAssociation ? '是' : '否（中心线二维关联）')
    + '</small></div>');
  rows.push('<div class="list-row"><span><b>选址停止原因</b></span><small>'
    + escapeHtml(model.selectionStopReasonText ?? '—') + '</small></div>');
  rows.push('<div class="list-row"><span><b>写入 ProjectState</b></span><small>'
    + escapeHtml(String(model.writesPerformed ?? '—')) + ' 次</small></div>');
  return '<div class="scroll-list">' + rows.join('') + '</div>';
}

/** 起终点强制设备表（**不可关闭**）。 */
export function navigationMandatoryHtml(model) {
  if (!model || !model.ran) return '';
  if (!model.mandatoryRows.length) {
    return '<div class="wb-empty">响应未给出起终点强制设备要求。</div>';
  }
  const rows = model.mandatoryRows.map(row => '<div class="list-row" data-navigation-mandatory="'
    + escapeHtml(row.siteId) + '"><span><b>' + escapeHtml(row.positionText) + ' · '
    + escapeHtml(row.siteId) + '</b><small>' + escapeHtml(row.device
      ? row.device.provisioning : '—') + '</small></span><small>'
    + escapeHtml(row.statusText) + '</small></div>').join('');
  return '<div class="scroll-list">' + rows + '</div>'
    + '<div class="parameter-note">' + escapeHtml(FORCE_ENDPOINT_NOTE) + '</div>';
}

/** 站址明细表：设备来源 / 关联身份（供点击查看）。 */
export function navigationSiteRowsHtml(model) {
  if (!model || !model.ran || !model.siteRows.length) {
    return '<div class="wb-empty">尚未选出监测站址。</div>';
  }
  const rows = model.siteRows.map(row => '<div class="list-row" data-navigation-site="'
    + escapeHtml(row.siteId) + '"><span><b>' + escapeHtml(row.siteId) + '</b><small>'
    + escapeHtml(row.roleText) + ' · ' + escapeHtml(row.provisioningText) + '</small></span><small>'
    + escapeHtml(row.deviceId ?? '—') + '</small></div>').join('');
  return '<div class="scroll-list">' + rows + '</div>';
}

/** 缺口明细表：长度 / 经过时间 / 容忍判定 / 证据状态。 */
export function navigationGapRowsHtml(model) {
  if (!model || !model.ran) return '';
  if (!model.gapRows.length) {
    return '<div class="wb-empty">当前航路没有未关联缺口。</div>';
  }
  const rows = model.gapRows.map(row => '<div class="list-row" data-navigation-gap="'
    + escapeHtml(row.index) + '"><span><b>缺口 ' + escapeHtml(row.index + 1) + '</b><small>'
    + escapeHtml(metricText(row.lengthM, 2, 'm')) + ' · 容忍 '
    + escapeHtml(metricText(row.gapToleranceS, 1, 's')) + '</small></span><small>'
    + escapeHtml(row.toleranceStatusText) + ' · '
    + escapeHtml(metricText(row.durationS, 2, 's')) + '</small></div>').join('');
  return '<div class="scroll-list">' + rows + '</div>';
}

/** 候选站址概览：全部已录入起降点 / 既有铁塔 / 不可用候选数量与原因。 */
export function sharedSiteCandidateSummaryHtml(model) {
  if (!model || !model.available) {
    return '<div class="wb-empty">候选站址快照尚未读取。</div>';
  }
  const counts = model.counts || {};
  const cards = [
    ['候选总数', metricText(counts.total, 0, '个')],
    ['工程节点', metricText(counts.nodes, 0, '个')],
    ['参考起降点', metricText(counts.reference_landing_sites, 0, '个')],
    ['既有铁塔', metricText(counts.towers, 0, '个')],
    ['可用候选', metricText(counts.spatial_candidates, 0, '个')],
    ['不可用候选', metricText(counts.spatial_blocked, 0, '个')],
  ];
  const reasonRows = model.reasonRows.length
    ? model.reasonRows.map(row => '<div class="list-row" data-navigation-block-reason="'
      + escapeHtml(row.reason) + '"><span><b>' + escapeHtml(row.text) + '</b><small>'
      + escapeHtml(row.reason) + '</small></span><small>'
      + escapeHtml(metricText(row.count, 0, '个')) + '</small></div>').join('')
    : '<div class="wb-empty">当前没有不可用候选。</div>';
  return '<div class="metric-grid">'
    + cards.map(([label, value]) =>
      '<b>' + escapeHtml(value) + '<small>' + escapeHtml(label) + '</small></b>').join('')
    + '</div><h4>不可用候选原因</h4><div class="scroll-list">' + reasonRows + '</div>';
}

/** 固定宽带 / 全宽状态分区。 */
export function fixedWidthSectionHtml({geometryModel, fullWidthModel}) {
  const geometry = geometryModel || fixedCorridorGeometryModel(null);
  const fullWidth = fullWidthModel || fullWidthServiceStatusModel();
  const geometryRows = [
    ['固定宽带状态', geometry.status ?? '—'],
    ['几何 currentness', geometry.currentness ?? '—'],
    ['currentness 原因', geometry.currentnessReason ?? '—'],
    ['半宽 / 总宽', metricText(geometry.corridorHalfWidthM, 0, 'm') + ' / '
      + metricText(geometry.corridorTotalWidthM, 0, 'm')],
    ['宽度策略版本', geometry.widthPolicyVersion ?? '—'],
    ['几何指纹', shortFingerprint(geometry.geometryFingerprint)],
  ];
  const fullWidthRows = [
    ['全宽 CNS 服务状态', fullWidth.status],
    ['是否已评估', fullWidth.evaluated ? '是' : '否'],
    ['原因', fullWidth.reason ?? '—'],
  ];
  return '<div class="scroll-list">'
    + geometryRows.concat(fullWidthRows).map(([label, value]) =>
      '<div class="list-row"><span><b>' + escapeHtml(label) + '</b></span><small>'
      + escapeHtml(value) + '</small></div>').join('')
    + '</div><div class="parameter-note">' + escapeHtml(fullWidth.note) + '</div>'
    + '<div class="parameter-note">' + escapeHtml(WIDTH_VS_ASSOCIATION_NOTE) + '</div>';
}

/** 主面板：导航服务监测规划（操作 + 结果合一，供 Step05 分段使用）。 */
export function renderNavigationMonitoringPanel(flow, options = {}) {
  const candidates = sharedSiteCandidateModel(options.candidates ?? flow?.navigation_monitoring?.candidates);
  const result = navigationSimulationModel(options.result ?? flow?.navigation_monitoring?.result);
  const geometry = fixedCorridorGeometryModel(
    options.fixedCorridorGeometry ?? flow?.navigation_monitoring?.fixed_corridor_geometry,
  );
  const fullWidth = fullWidthServiceStatusModel(options.fullWidth ?? {});
  const draft = navigationParameterDraft(options.draft ?? result.parameters);
  //: 候选快照**尚未读取**时，权威参考起降点的 CRS 状态在界面上仍然是"未确认"
  //: （正式项目 revision 468 的 reference_landing_sites.crs.source_crs.value 为 null）。
  //: 因此这里不依赖快照是否已加载，只要快照缺证或明确报 CRS 未确认，就如实展示阻断句。
  const referenceCrsBlocked = options.referenceCrsBlocked === true
    || candidates.reasonRows.some(row => row.reason === 'reference_source_crs_unresolved');

  const parameterRows = NAVIGATION_PARAMETER_FIELDS.map(field => {
    const value = draft[field.key];
    if (!field.editable) {
      return '<label class="check-row"><input type="checkbox" id="navigationForceEndpointMonitors" '
        + 'checked disabled> ' + escapeHtml(field.label)
        + '<small>' + escapeHtml(field.note) + '</small></label>';
    }
    return '<label>' + escapeHtml(field.label)
      + (field.unit ? '（' + escapeHtml(field.unit) + '）' : '')
      + '<input class="panel-input" type="number" id="navigation_' + escapeHtml(field.key)
      + '" value="' + escapeHtml(value ?? '') + '" min="' + escapeHtml(field.min ?? 0)
      + '" step="' + escapeHtml(field.step ?? 1) + '">'
      + '<small>' + escapeHtml(field.note) + '</small></label>';
  }).join('');

  const routeOptions = [
    ['operational_route', ROUTE_SOURCE_LABEL.operational_route],
    ['engineering_demo', ROUTE_SOURCE_LABEL.engineering_demo],
  ].map(([value, label]) => '<option value="' + escapeHtml(value) + '">'
    + escapeHtml(label) + '</option>').join('');

  const blockers = [];
  if (referenceCrsBlocked) {
    blockers.push({
      text: REFERENCE_CRS_BLOCKED_TEXT,
      detail: '人工 CRS 确认入口保留；本轮不调用确认接口、不修改正式项目、不自动假设 EPSG:4326/4490。',
    });
  }
  if (result.ran && !result.riskPriorityApplied) {
    blockers.push({
      kind: 'assumption',
      text: '未应用风险优先排序',
      detail: '风险画像不是 current 或身份不匹配；如实标注，绝不生成虚假的高风险航段。',
    });
  }
  const blockerHtml = blockers.length
    ? '<div class="wb-blockers">' + blockers.map(item => {
      const kind = item.kind === 'assumption' ? 'assumption' : 'blocker';
      return '<div class="wb-blocker" data-kind="' + kind + '"><b>'
        + escapeHtml(kind === 'assumption' ? '工程假设' : '阻塞项') + '</b><span>'
        + escapeHtml(item.text) + '</span><small>' + escapeHtml(item.detail) + '</small></div>';
    }).join('') + '</div>'
    : '';

  const identity = result.routeIdentity;
  return '<div class="parameter-note">本区只消费两个只读接口：候选站址快照与导航服务监测仿真。'
    + '前端不重新实现选址算法，仿真结果不写入 ProjectState。</div>'
    + blockerHtml
    + '<h4>1. 航路选择与身份</h4>'
    + '<label>航路来源<select class="panel-input" id="navigationRouteSource">'
    + routeOptions + '</select></label>'
    + '<label>航路标识<input class="panel-input" id="navigationRouteId" value="'
    + escapeHtml(options.routeId ?? identity.routeId ?? '') + '" placeholder="留空则使用当前权威航路"></label>'
    + '<div class="scroll-list">'
    + '<div class="list-row"><span><b>航路权威身份</b><small>route authority</small></span><small>'
    + escapeHtml(identity.routeAuthorityText) + '</small></div>'
    + '<div class="list-row"><span><b>完整几何指纹</b><small>'
    + escapeHtml(identity.fingerprintPrefix || '—') + '</small></span><small>'
    + escapeHtml(shortFingerprint(identity.routeGeometryFingerprint)) + '</small></div>'
    + '<div class="list-row"><span><b>航路点数</b></span><small>'
    + escapeHtml(metricText(identity.pointCount, 0, '个')) + '</small></div>'
    + '<div class="list-row"><span><b>权威 currentness</b></span><small>'
    + escapeHtml(identity.currentness ?? '—') + '</small></div>'
    + '</div>'
    + '<h4>2. 候选站址（全部已录入起降点 / 既有铁塔 / 不可用候选）</h4>'
    + '<div id="' + NAVIGATION_CANDIDATE_CONTAINER_ID + '">'
    + sharedSiteCandidateSummaryHtml(candidates)
    + '</div>'
    + '<h4>3. 导航参数</h4>'
    + parameterRows
    + '<div class="button-row"><button class="primary" id="runNavigationMonitoringSimulation">'
    + '运行导航服务监测仿真</button></div>'
    + '<h4>4. 结果摘要</h4>'
    + navigationSimulationSummaryHtml(result)
    + navigationSimulationStatusHtml(result)
    + '<h4>5. 起终点强制设备（不可关闭）</h4>'
    + navigationMandatoryHtml(result)
    + '<h4>6. 站址明细</h4>'
    + navigationSiteRowsHtml(result)
    + '<h4>7. 未关联缺口</h4>'
    + navigationGapRowsHtml(result)
    + '<h4>8. 固定保障宽带与全宽状态</h4>'
    + fixedWidthSectionHtml({geometryModel: geometry, fullWidthModel: fullWidth})
    + (options.fixtureLabel
      ? '<div class="parameter-note">' + escapeHtml(TEST_FIXTURE_LABEL) + '</div>' : '');
}

/** 图例行（地图图例与测试共用同一份定义）。 */
export const NAVIGATION_MONITORING_LEGEND = Object.freeze([
  {id: 'navigation-route', label: '导航服务监测航路（中心线）', symbol: 'route'},
  {id: 'navigation-mandatory-device', label: '起终点强制监测设备（仿真规划）', symbol: 'mandatory'},
  {id: 'navigation-tower-device', label: '推荐铁塔监测设备（复用既有铁塔）', symbol: 'tower'},
  {id: 'navigation-associated-segment', label: '已关联航段（2000 m 关联范围内）', symbol: 'associated'},
  {id: 'navigation-unassociated-segment', label: '未关联航段（缺口）', symbol: 'gap'},
  {id: 'navigation-fixed-corridor', label: '固定 1000 m 保障宽带（淡色边界，仅 current 时显示）', symbol: 'band'},
]);

/** 主操作按钮 id（绑定与测试共用同一份定义）。 */
export const NAVIGATION_RUN_BUTTON_ID = 'runNavigationMonitoringSimulation';
/** 候选站址统计的局部更新容器 id（避免为刷新统计而重建整个面板）。 */
export const NAVIGATION_CANDIDATE_CONTAINER_ID = 'navigationCandidateSummary';

//: 最近一次 bind 注入的控制器：局部刷新与按钮处理器始终使用**最新**的那一份。
let navigationController = null;

/**
 * 从面板表单读取参数草稿（只用后端已登记的键）。
 */
export function readNavigationDraft($) {
  const draft = {};
  for (const key of ['monitor_association_radius_m', 'gap_tolerance_s', 'route_speed_mps',
    'risk_priority_weight', 'max_optional_monitor_sites']) {
    const input = $ ? $('navigation_' + key) : null;
    if (input && input.value !== '') draft[key] = input.value;
  }
  return draft;
}

/**
 * 局部刷新候选站址统计。
 *
 * **为什么是局部刷新而不是整面板重绘**：候选快照是进入分段后按需读取的只读 GET，
 * 若读取完成后触发 ``renderWorkflow()``，整棵面板（含主操作按钮）会被替换；用户正好
 * 在这一瞬间点击「运行导航服务监测仿真」时，点击会落在已被换掉的节点上而静默失效。
 * 只更新候选容器既能让统计如实显示，又完全不动按钮节点。
 *
 * @returns {boolean} 容器存在并已更新时为 true
 */
export function refreshNavigationCandidateSummary(doc = globalThis.document) {
  const container = doc?.getElementById?.(NAVIGATION_CANDIDATE_CONTAINER_ID) || null;
  if (!container) return false;
  const state = navigationController?.navigationMonitoring?.state?.() || {};
  container.innerHTML = sharedSiteCandidateSummaryHtml(sharedSiteCandidateModel(state.candidates));
  return true;
}

/**
 * 绑定导航服务监测规划面板。
 *
 * 主操作按钮用 ``onclick`` 直接绑定：``bind`` 在每次 ``renderWorkflow()`` 之后都会被
 * 调用，因此重建后的新节点一定拿到新处理器。为避免"读取候选快照即重绘"打断点击，
 * 候选统计走 :func:`refreshNavigationCandidateSummary` 的**局部**更新。
 */
export function bindNavigationMonitoringPanel(c) {
  if (!c) return null;
  navigationController = c;
  const runButton = c.$ ? c.$(NAVIGATION_RUN_BUTTON_ID) : null;
  if (runButton && c.navigationMonitoring) {
    runButton.onclick = async () => {
      const facade = c.navigationMonitoring;
      try {
        runButton.disabled = true;
        runButton.dataset.busy = 'true';
        //: 先确保候选快照已读取（面板统计来自它），再运行只读仿真。
        await facade.loadCandidates();
        refreshNavigationCandidateSummary();
        await facade.simulate({
          routeSource: c.$('navigationRouteSource')?.value || null,
          routeId: (c.$('navigationRouteId')?.value || '').trim() || null,
          draft: readNavigationDraft(c.$),
        });
      } catch (error) {
        c.panelError?.(error?.message || String(error));
      } finally {
        //: 恢复的是本次绑定的那个按钮节点；面板若已被重建，新节点由新的 bind 管理
        //: （其初始状态本就是可用），因此这里不需要再去文档里找一次。
        runButton.disabled = false;
        delete runButton.dataset.busy;
      }
    };
  }
  //: 进入本分段时按需读取候选快照（只读 GET），完成后**只**局部刷新统计容器。
  if (!c.$ || !c.navigationMonitoring) return null;
  if (c.$(NAVIGATION_CANDIDATE_CONTAINER_ID) && !c.navigationMonitoring.state?.().candidates) {
    return c.navigationMonitoring.loadCandidates()
      .then(() => { refreshNavigationCandidateSummary(); })
      .catch(error => c.panelError?.(error?.message || String(error)));
  }
  return null;
}

/** 供测试复位模块级引用（生产代码不调用）。 */
export function resetNavigationMonitoringBinding() {
  navigationController = null;
}
