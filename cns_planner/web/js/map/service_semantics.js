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
  'S:surveillance': '监视',
  'S:rid_cooperative': 'RID 合作监视',
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
