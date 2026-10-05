// =========================================================
// CNS service 语义词表（Round 3，**展示层唯一来源**）
//
// 本模块只做**展示语义**：raw enum → 业务中文、service_key → 服务名、
// 图例/颜色常量。它**不含任何业务数值**，因此不构成第二份 backend authority。
//
// 铁律（Round 2 冻结，Round 3 遵循）：
//  * Communication / RID 的规划半径、站址重数要求**只**来自 backend canonical
//    字段（``flow.device_catalog.items[].coverage_geometry.radius_by_surface``、
//    ``flow.required_cns``、P14/P15/P16 的 service evidence）；
//  * 前端**绝不**计算距离、重数、land/sea 判定，也不复制 4000/2000/5000 或 2/1
//    这类数字当作新的业务来源；缺失证据一律 fail-closed 显示「证据不足」；
//  * ``unknown`` 永远不是 ``sea`` —— 绝不把证据不足写成海面。
// =========================================================

/** 本轮正式呈现的 surface-aware 服务（顺序即界面顺序）。 */
export const CNS_FOCUS_SERVICES = ['C:communication', 'S:rid_cooperative'];

/**
 * service_key → 业务名。
 *
 * `S:rid_cooperative` 必须显示成「RID 合作监视」，**绝不**退化成泛化 ``S``；
 * `S:surveillance` 与 legacy `C` / `N` / `S` 继续兼容显示。
 */
export const SERVICE_KEY_LABELS = {
  'C:communication': '通信',
  'N:navigation': '导航',
  //: Round D：显式登记 RTK 地面增强与 Radar 非合作监视，未登记取值仍原样返回。
  'N:rtk_augmentation': '导航增强（GNSS/RTK）',
  'S:surveillance': '监视',
  'S:rid_cooperative': 'RID 合作监视',
  'S:radar_noncooperative': '非合作监视（Radar）',
};

/** 主界面使用的短标签（不含括号说明）。 */
export const SERVICE_KEY_SHORT_LABELS = {
  'C:communication': 'Communication',
  'N:navigation': 'Navigation',
  'S:surveillance': 'Surveillance',
  'S:rid_cooperative': 'RID 合作监视',
};

/** 地图图例使用的全名，明确区分三种监视/通信语义。 */
export const SERVICE_LEGEND_LABELS = {
  'C:communication': 'Communication 通信（全向）',
  'N:navigation': 'Navigation 导航',
  'S:surveillance': 'Radar Non-cooperative Surveillance（方向性面阵）',
  'S:rid_cooperative': 'RID Cooperative Surveillance（全向，非方向性面阵）',
};

/** 子系统代码 → 服务名（legacy C/N/S 显示继续兼容）。 */
export const SUBSYSTEM_SERVICE_LABELS = {C: '通信', N: '导航', S: '监视'};

/** surface_class → 业务中文。``unknown`` 是**证据不足**，绝不是海面。 */
export const SURFACE_CLASS_TEXT = {
  land: '陆地',
  sea: '海上',
  coastal_uncertain: '海岸不确定',
  unknown: '证据不足',
};

/**
 * service/冗余状态 → 业务中文（Round 3 冻结词表）。
 *
 * ``satisfied`` → 满足；``confirmed_deficit`` → 已确认缺口；``unknown`` → 证据不足。
 * 图例语境下的「冗余不足」用 :data:`REDUNDANCY_STATUS_TEXT`。
 */
export const SERVICE_STATUS_TEXT = {
  satisfied: '满足',
  confirmed_deficit: '已确认缺口',
  unknown: '证据不足',
};

/** 冗余（独立物理站址数）状态 → 图例/面板中文。 */
export const REDUNDANCY_STATUS_TEXT = {
  satisfied: '满足',
  confirmed_deficit: '冗余不足',
  unknown: '证据不足',
  not_applicable: '不适用',
};

/** 冗余状态 → 颜色语义（沿用既有 Radar coverage 状态色系，但不复用其算法/数据模型）。 */
export const CNS_GAP_COLORS = {
  satisfied: '#1f8a4c',
  under_redundant: '#d98b0b',
  uncovered: '#c62828',
  unknown: '#8b949e',
};

/** 冗余状态 → 地图四态（满足 / 冗余不足 / 未覆盖 / 证据不足）。 */
export const REDUNDANCY_STATUS_MAP_STATE = {
  satisfied: 'satisfied',
  confirmed_deficit: 'under_redundant',
  unknown: 'unknown',
  not_applicable: 'unknown',
};

/** P15 combined_status → 地图四态（未覆盖只在整体缺口时给出）。 */
export const COMBINED_STATUS_MAP_STATE = {
  satisfied: 'satisfied',
  confirmed_gap: 'uncovered',
  unknown: 'unknown',
  not_applicable: 'unknown',
};

export const CNS_GAP_STATE_LABELS = {
  satisfied: '满足',
  under_redundant: '冗余不足',
  uncovered: '未覆盖',
  unknown: '证据不足',
};

/** service_key 是否属于本轮正式 surface-aware 服务。 */
export function isSurfaceAwareServiceKey(serviceKey) {
  return CNS_FOCUS_SERVICES.includes(String(serviceKey || ''));
}

/**
 * raw 状态 → 词表中文。
 *
 * **优先使用后端给出的 status 字段的 key**；只有在该 key 未登记时才回落到中文
 * 展示词（词表本身以中文为 key，因此不会把已翻译文本再翻译一次）。
 */
export function vocabularyText(value, table, fallback = '证据不足') {
  const key = String(value ?? '').trim();
  if (!key) return fallback;
  if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
  const byText = Object.values(table).find(text => text === key);
  if (byText) return byText;
  return key;
}

/** service_key → 业务名（未登记时原样返回，绝不编造）。 */
export function serviceKeyLabel(serviceKey, {table = SERVICE_KEY_LABELS} = {}) {
  const key = String(serviceKey ?? '').trim();
  if (!key) return '未声明服务';
  return table[key] || key;
}

/** legacy 子系统代码 → 中文（C / N / S 兼容显示）。 */
export function subsystemServiceLabel(code) {
  const key = String(code ?? '').trim();
  if (!key) return '未声明分系统';
  return SUBSYSTEM_SERVICE_LABELS[key] || key;
}

/** surface_class → 中文（unknown = 证据不足）。 */
export function surfaceClassText(value) {
  return vocabularyText(value, SURFACE_CLASS_TEXT);
}

/** service 状态 → 中文。 */
export function serviceStatusText(value) {
  return vocabularyText(value, SERVICE_STATUS_TEXT);
}

/**
 * P17 首次探测距离**证据覆盖状态** → 中文。
 *
 * 与 :data:`SERVICE_STATUS_TEXT` 的 ``satisfied`` 严格区分：Radar 规划代理的
 * ``planning_proxy_validated`` **只**表示"该探测距离的几何代理已通过规划期验证"，
 * 绝不表示"Radar 服务覆盖/冗余已满足"（后者只能来自 P15 逐 service 结论）。
 * 本表在冻结词表之上**只增不改**：``satisfied`` / ``confirmed_deficit`` / ``unknown``
 * 的中文与 :data:`SERVICE_STATUS_TEXT` 完全一致。
 */
export const DETECTION_COVERAGE_STATUS_TEXT = {
  ...SERVICE_STATUS_TEXT,
  planning_proxy_validated: '规划代理已验证',
};

/** 规划代理状态的**固定披露文案**（必须与中文状态同时展示）。 */
export const DETECTION_PLANNING_PROXY_NOTE =
  '仅表示 Radar 规划探测距离代理通过几何验证，不代表 Radar 服务覆盖/冗余已满足。';

/** P17 首次探测距离证据覆盖状态 → 中文（``planning_proxy_validated`` 绝不裸显英文）。 */
export function detectionCoverageStatusText(value) {
  return vocabularyText(value, DETECTION_COVERAGE_STATUS_TEXT);
}

/**
 * 该覆盖状态是否来自 Radar 规划代理。
 *
 * **只**用于决定是否展示上面的固定披露；前端绝不据此推导 Radar 服务是否满足。
 */
export function isDetectionPlanningProxyStatus(value) {
  return String(value ?? '').trim() === 'planning_proxy_validated';
}

/** 覆盖状态附注：只有规划代理状态需要披露语义边界，其余返回空串。 */
export function detectionCoverageStatusNote(value) {
  return isDetectionPlanningProxyStatus(value) ? DETECTION_PLANNING_PROXY_NOTE : '';
}

/** 冗余状态 → 中文（confirmed_deficit = 冗余不足）。 */
export function redundancyStatusText(value) {
  return vocabularyText(value, REDUNDANCY_STATUS_TEXT);
}

/** P15 combined_status → 中文。 */
export function combinedStatusText(value) {
  const key = String(value ?? '').trim();
  if (key === 'confirmed_gap') return '已确认缺口';
  if (key === 'satisfied') return '满足';
  if (key === 'unknown') return '证据不足';
  if (key === 'not_applicable') return '不适用';
  return key || '证据不足';
}

/** 地图四态 → 中文。 */
export function gapStateText(value) {
  return vocabularyText(value, CNS_GAP_STATE_LABELS);
}

/**
 * 计数是否"已知"。
 *
 * ``null`` / ``undefined`` / 空字符串一律视为**证据不足**（绝不当作 0）：
 * 后端快照里的缺失字段会以这几种形式出现，把 ``null`` 读成 0 会把
 * "证据不足"误显示成"0 个站址"。
 */
function isKnownCount(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === 'string' && value.trim() === '') return false;
  return Number.isFinite(Number(value));
}

/**
 * 站址计数文本：**必须**显示「不同物理站址」。
 *
 * 例如 ``1 / 2 个不同物理站址``。缺失任一侧证据时给出「证据不足」，
 * 绝不把"2 台设备"写成"2 重"。
 *
 * 本模块**只**表达计数事实：这里**没有**、也不允许新增
 * "actual >= required → 满足 / 冗余不足"这类判定。service 状态一律消费后端的
 * ``status`` / ``status_by_surface``，前端绝不推导。
 */
export function distinctSiteCountText(actual, required) {
  if (!isKnownCount(actual) || !isKnownCount(required)) {
    return '不同物理站址数：证据不足';
  }
  return Number(actual) + ' / ' + Number(required) + ' 个不同物理站址';
}

/**
 * **要求**站址数文本（只读后端数字，缺失即「证据不足」，绝不补 0）。
 *
 * 与 :func:`distinctSiteCountText` 的区别：这里只表达"要求多少"，
 * 因此不会把"尚无实测计数"误写成"证据不足"。
 */
export function requiredSiteCountText(value) {
  if (!isKnownCount(value)) return '证据不足';
  return Number(value) + ' 个不同物理站址';
}

/**
 * 两个 physical site 身份是否相同。
 *
 * 相同 ⇒ 必须提示「同一物理站址，不增加独立站址重数」。
 * 身份来自 backend canonical ``distinct_site_id``；**绝不**用坐标、设备数或
 * ``independence_group`` 代替。
 */
export function sameDistinctSite(left, right) {
  const a = String(left ?? '').trim();
  const b = String(right ?? '').trim();
  if (!a || !b) return false;
  return a === b;
}

/** 语义声明：CNS service 覆盖结论唯一来自 backend canonical 字段。 */
export const CNS_SERVICE_AUTHORITY_NOTE =
  '本区只转印后端 canonical 字段（service_key / surface_class / '
  + 'distinct_site_count / required_distinct_site_count / status）；'
  + '前端不重算距离、重数与 land/sea 判定。';

/** 5 km 虚线圆的**唯一**允许叫法。 */
export const RID_SEA_PLANNING_RADIUS_LABEL = 'RID 海上最大规划半径';

// =========================================================
// Round D：统一 CNS 四服务（Communication / RTK Navigation / RID / Radar）
//
// 本节只补**展示语义**（正式中文名、planner family 名、导航工程基线与缺口原因的中文），
// 依然不含任何业务数值：
//  * 四服务的半径、要求站址数、工程基线距离、Radar 面板几何一律来自 backend canonical；
//  * 前端不把 RID 与 Radar 合并成模糊的「监视」，也不把导航增强写成「RTK 覆盖半径」。
// =========================================================

/** Round D 统一呈现的四个 CNS 服务（顺序即界面顺序）。 */
export const CNS_ROUNDD_SERVICE_KEYS = [
  'C:communication',
  'N:rtk_augmentation',
  'S:rid_cooperative',
  'S:radar_noncooperative',
];

/**
 * 四服务正式中文名（Round D 冻结）。
 *
 * `S:rid_cooperative` 与 `S:radar_noncooperative` **必须**分别显示为
 * 「合作监视（RID）」与「非合作监视（Radar）」，绝不合并成一个「监视」。
 */
export const CNS_SERVICE_FORMAL_LABELS = {
  'C:communication': '通信',
  'N:rtk_augmentation': '导航增强（GNSS/RTK）',
  'S:rid_cooperative': '合作监视（RID）',
  'S:radar_noncooperative': '非合作监视（Radar）',
};

/** RequiredCNS 编辑区的服务行定义（label / 复选框文案 / 所属子系统名）。 */
export const CNS_SERVICE_REQUIREMENT_ROWS = [
  {
    serviceKey: 'C:communication', subsystem: 'communication', subsystemCode: 'C',
    label: '通信', checkboxLabel: '要求通信服务',
  },
  {
    serviceKey: 'N:rtk_augmentation', subsystem: 'navigation', subsystemCode: 'N',
    label: '导航增强', checkboxLabel: '要求 GNSS/RTK 地面增强',
  },
  {
    serviceKey: 'S:rid_cooperative', subsystem: 'surveillance', subsystemCode: 'S',
    label: '合作监视', checkboxLabel: '要求 RID',
  },
  {
    serviceKey: 'S:radar_noncooperative', subsystem: 'surveillance', subsystemCode: 'S',
    label: '非合作监视', checkboxLabel: '要求 Radar',
  },
];

/** 双通道监视三行（**绝不**把两通道相加成一个"监视站点总数"）。 */
export const SURVEILLANCE_DUAL_CHANNEL_LABELS = {
  rid: '合作监视（RID）',
  radar: '非合作监视（Radar）',
  dual: '双通道监视',
};

/** 双通道监视语义声明（逐字）：两通道独立，任一路径缺口即监视未满足。 */
export const SURVEILLANCE_DUAL_CHANNEL_NOTE =
  '合作监视（RID）与非合作监视（Radar）是两条独立通道，统计与证据一律分服务给出；'
  + '双通道监视只有在两通道都满足时才成立，任一通道存在已确认缺口即存在监视缺口。';

/** 后端 all_required 语义的只读显示文案（用户不得改成"任选一个即可"）。 */
export const ALL_REQUIRED_MODE_LABEL = '双通道均需满足（all_required）';

/** 导航增强工程基线的**唯一**图例名（绝不叫 "RTK 无线覆盖范围"）。 */
export const NAVIGATION_BASELINE_LEGEND_LABEL = '导航增强工程基线范围';

/** 导航增强规划模型的正式中文名。 */
export const NAVIGATION_BASELINE_MODEL_LABEL = 'GNSS/RTK 基准站工程基线模型';

/** 工程规划参数（不是厂家保证性能）的逐字声明。 */
export const NAVIGATION_BASELINE_DISCLAIMER =
  '该值为项目确认的工程规划参数，不是厂家保证的无线覆盖距离或实测 RTK 服务半径。';

/** 全图统一的几何/基线免责声明。 */
export const CNS_SERVICE_GEOMETRY_LEGEND_NOTE =
  '图示范围为工程规划几何/基线表达；不代表实测传播或设备保证性能。';

/** 导航增强的 delivery 依赖声明（RTK 修正数据必须经通信服务交付）。 */
export const NAVIGATION_DELIVERY_DEPENDENCY_LABEL = 'RTK 修正数据依赖通信服务';

/** Planner family → 业务中文（内部标识只在高级详情出现）。 */
export const PLANNER_FAMILY_LABELS = {
  omnidirectional_site: '全向站点（通信 / RID）',
  navigation_reference_station: '导航基准站',
  directional_radar: '方向性雷达面阵',
  legacy_subsystem: '旧版子系统口径',
};

/** Radar 型号显示名（**只是名字**；I/II 的几何与计数一律来自 backend canonical 结果）。 */
export const RADAR_TYPE_TEXT = {
  radar_i: 'Radar-I（中近程雷达Ⅰ型）',
  radar_ii: 'Radar-II（中近程雷达Ⅱ型）',
};

/** 导航增强规划单元说明（不是设备型号）。 */
export const NAVIGATION_PLANNING_UNIT_LABEL = '新增 GNSS/RTK 基准站规划单元';
export const NAVIGATION_EQUIPMENT_NOT_SELECTED_NOTE =
  '当前仅完成站址与服务规划，具体设备型号尚未选择。';

/** Radar 几何声明：与 Communication / RID 的全向圆模型**不同**。 */
export const RADAR_DIRECTIONAL_GEOMETRY_NOTE =
  'Radar 使用方向性面阵几何规划，不使用 Communication/RID 的全向圆形模型。';

/** 导航增强 gap cause → 业务中文（后端只有这两个 cause）。 */
export const NAVIGATION_GAP_CAUSE_TEXT = {
  reference_station_deficit: '基准站工程基线 / 独立站址不足',
  correction_delivery_deficit: 'RTK 修正数据通信交付不足',
};

/** 导航增强 delivery 状态 → 业务中文。 */
export const NAVIGATION_DELIVERY_STATUS_TEXT = {
  satisfied: '满足',
  confirmed_deficit: '缺口',
  unknown: '证据不足',
  not_configured: '未配置交付服务',
  not_evaluated: '未评估',
};

/** 导航增强 geometry 状态 → 业务中文（站址基线几何）。 */
export const NAVIGATION_GEOMETRY_STATUS_TEXT = {
  satisfied: '满足',
  confirmed_deficit: '缺口',
  unknown: '证据不足',
};

/** planning readiness → 业务中文。 */
export const NAVIGATION_PLANNING_READINESS_TEXT = {
  ready: '已就绪',
  pending_confirmation: '待确认',
};

/** 导航增强证据不足原因 → 业务中文。 */
export const NAVIGATION_REASON_TEXT = {
  navigation_augmentation_policy_not_confirmed: '导航增强工程策略尚未确认',
  max_reference_baseline_m_missing: '尚未确认最大工程基线距离',
  required_distinct_site_count_missing: '尚未确认要求的独立站址数',
  correction_delivery_service_not_configured: '未配置 RTK 修正数据交付服务',
  correction_delivery_unknown: '通信交付证据不足',
  correction_delivery_confirmed_deficit: '通信交付存在缺口',
  navigation_augmentation_route_evidence_missing: '缺少该航路的导航增强证据',
  navigation_site_suitability_unconfirmed_candidates: '存在尚未确认适用性的候选站址',
};

/**
 * 站址来源（``planning_origin`` / ``reuse_class``）→ 业务中文。
 *
 * Round D 的站址来源标签：Step05 的共塔语境另有自己的 ``REUSE_CLASS_LABEL``
 * （措辞面向"复用类别"），两者语义相关但不互相复制数值或 authority。
 */
export const NAVIGATION_SITE_SOURCE_TEXT = {
  existing_cns_facility: '已有 CNS 设施',
  existing_shared_site: '共享站址',
  tower_colocation_host: '共塔候选',
  candidate_site: '候选站址',
  new_build_candidate: '新建候选',
};

/**
 * 站址条目上已声明的 ``navigation_site_suitability``（顶层优先，其次 ``metadata``）。
 *
 * **纯读取**：未声明返回 ``null``（= 无证据），绝不推断合格性。
 */
export function navigationSuitabilityOf(site) {
  const item = site && typeof site === 'object' ? site : {};
  const metadata = item.metadata && typeof item.metadata === 'object' ? item.metadata : {};
  const raw = item.navigation_site_suitability ?? metadata.navigation_site_suitability ?? null;
  return raw && typeof raw === 'object' ? raw : null;
}

/**
 * 「已有参考站」还是「可规划候选」——后端 ``_reference_station_installed`` 的**只读转印**。
 *
 * 语义（与 backend 一致，因此地图线型与正式判定不会打架）：
 *  * 显式 ``reference_station_installed`` 优先；
 *  * 未声明时一律不由 ``planningOrigin`` 单独推断已建成；仅已有设施中明确的
 *    canonical ``N:rtk_augmentation`` active/installed device 可作为等价事实。
 *
 * 它只决定地图用实线还是虚线，**不**参与任何业务判定（判定唯一来自 backend）。
 */
export function navigationReferenceStationInstalled(suitability, planningOrigin, site = null) {
  const declared = suitability?.reference_station_installed;
  if (declared === true) return true;
  if (String(planningOrigin ?? '').trim() !== 'existing_cns_facility') return false;
  return (site?.devices || []).some(device =>
    String(device?.service_key || '') === 'N:rtk_augmentation'
    && ['active', 'passed', 'installed'].includes(String(device?.status || 'active'))
  );
}

/** service_key → 四服务正式中文名（未登记时退回既有词表，绝不编造）。 */
export function cnsServiceFormalLabel(serviceKey) {
  const key = String(serviceKey ?? '').trim();
  if (!key) return '未声明服务';
  return CNS_SERVICE_FORMAL_LABELS[key] || serviceKeyLabel(key);
}

/** 该 service_key 是否属于 Round D 四服务。 */
export function isRounddServiceKey(serviceKey) {
  return CNS_ROUNDD_SERVICE_KEYS.includes(String(serviceKey ?? ''));
}

/** planner family → 业务中文（未登记原样返回）。 */
export function plannerFamilyLabel(value) {
  const key = String(value ?? '').trim();
  if (!key) return '未声明规划家族';
  return PLANNER_FAMILY_LABELS[key] || key;
}

/** 导航增强 gap cause → 业务中文（未登记原样返回，绝不编造原因）。 */
export function navigationGapCauseText(value) {
  return vocabularyText(value, NAVIGATION_GAP_CAUSE_TEXT, '未给出结构化原因');
}

/** 导航增强 delivery 状态 → 业务中文。 */
export function navigationDeliveryStatusText(value) {
  return vocabularyText(value, NAVIGATION_DELIVERY_STATUS_TEXT);
}

/** 导航增强 geometry 状态 → 业务中文。 */
export function navigationGeometryStatusText(value) {
  return vocabularyText(value, NAVIGATION_GEOMETRY_STATUS_TEXT);
}

/** planning readiness → 业务中文。 */
export function navigationPlanningReadinessText(value) {
  const key = String(value ?? '').trim();
  if (!key) return NAVIGATION_PLANNING_READINESS_TEXT.pending_confirmation;
  return NAVIGATION_PLANNING_READINESS_TEXT[key] || key;
}

/** 导航增强证据不足原因 → 业务中文（未登记原样返回）。 */
export function navigationReasonText(value) {
  const key = String(value ?? '').trim();
  if (!key) return '未给出原因';
  return NAVIGATION_REASON_TEXT[key] || key;
}

/** 站址来源 → 业务中文。 */
export function navigationSiteSourceText(value) {
  const key = String(value ?? '').trim();
  if (!key) return '未声明来源';
  return NAVIGATION_SITE_SOURCE_TEXT[key] || key;
}

/**
 * 导航增强工程基线的**显示**文本（米 → km）。
 *
 * 只做单位换算：数值缺失返回「未配置」，**绝不**补任何默认距离。
 * 前端不得用该文本参与任何判定——正式判定一律来自 backend。
 */
export function navigationBaselineText(baselineM) {
  const value = Number(baselineM);
  if (!Number.isFinite(value) || value <= 0) return '未配置';
  const km = value / 1000;
  return (Number.isInteger(km) ? km.toFixed(1) : String(km)) + ' km';
}
