/**
 * Round 2.4 —— 【工程依据 / 规划假设】人工录入区（Step4 正式 CNS需求 · 结果标签）。
 *
 * 产品缺口：此前一旦主链缺一条工程证据（例如地面通信设备的网络归属范围、
 * 机载是否具备网络远程识别参与能力），唯一办法是**让开发者改数据 / JSON**。
 * 本模块把这件事变成普通用户可完成、可审计、可保存的正式操作：
 *
 *   * 逐字段显示「需求要求什么 / 机载是否已声明 / 是否已补录工程依据」；
 *   * 录入时必须选择**来源类型**（有正式资料支持的事实 / 工程规划假设 / 尚无依据）；
 *   * 选择「工程规划假设」时必须填写 statement + source + basis(reason) +
 *     报告披露文本，且必须显式确认；
 *   * 提交只走正式接口 `POST /api/planning-evidence`，**不写 device catalog**，
 *     **不冒充厂家设备事实**；
 *   * 已录入条目逐条可撤回，并始终显示来源类型徽标与来源出处。
 *
 * 本模块只渲染与提交，不做任何取值推断；缺证据时如实显示"需要工程依据"。
 */

import { blockerList, escapeHtml, wbBlock } from './common.js';

/** 来源类型 → 中文与语义（与后端 `EVIDENCE_SOURCE_TYPES` 一一对应）。 */
export const EVIDENCE_SOURCE_TYPE_TEXT = {
  confirmed_source_fact: {
    label: '有正式资料支持的事实',
    note: '有可引用的正式资料（规范、厂家文件、正式测绘成果等）。必须给出资料出处并显式确认。',
  },
  engineering_assumption: {
    label: '工程规划假设',
    note: '为规划目的采用的工程假设，不代表厂家既有设备事实。必须填写假设陈述、依据、'
      + '来源与报告披露文本，并由用户显式确认。',
  },
  unknown: {
    label: '尚无依据（保持未知）',
    note: '如实登记"这里确实没有依据"。该记录不参与任何判定，只用于说明缺口。',
  },
};

const STATUS_TEXT = {
  satisfied: { text: '已具备可用依据', badge: 'passed' },
  evidence_required: { text: '需要工程依据（当前无法判定）', badge: 'pending_confirmation' },
  incompatible: { text: '已确认不满足需求（不是缺证据）', badge: 'failed' },
  not_required: { text: '当前需求未要求该字段', badge: 'not_applicable' },
};

const VALUE_TYPE_HINT = {
  enum_scalar: '单选（枚举校验）',
  enum: '可多选（枚举校验）',
  number: '数值（非负有限）',
};

/** 数值型证据字段的输入控件（Round 2.7：机载/运行场景性能声明）。 */
function numberInput(name) {
  return `<input type="number" step="any" min="0" id="${name}" value="">`;
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return '未声明';
  if (Array.isArray(value)) return value.length ? value.join('、') : '未声明';
  return String(value);
}

export function optionList(name, options, { multiple } = {}) {
  const list = (options || []).map((value) => (
    `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
  )).join('');
  return multiple
    ? `<select id="${name}" multiple size="${Math.min(4, (options || []).length || 1)}">${list}</select>`
    : `<select id="${name}">${list}</select>`;
}

/** 一条已录入工程依据的可读行（始终带来源类型徽标 + 出处 + 披露）。 */
export function planningEvidenceItemLine(item) {
  const sourceType = EVIDENCE_SOURCE_TYPE_TEXT[item.source_type]
    || { label: item.source_type, note: '' };
  const badge = item.source_type === 'engineering_assumption'
    ? 'assumption'
    : (item.source_type === 'unknown' ? 'blocker' : 'passed');
  const detail = [
    `来源类型：${sourceType.label}`,
    `来源：${item.source || '未记录'}`,
    item.statement ? `假设陈述：${item.statement}` : '',
    item.reason ? `依据：${item.reason}` : '',
    item.report_disclosure ? `报告披露：${item.report_disclosure}` : '',
    `录入：${item.declared_by || '未记录'} · ${item.created_at || ''}`,
    `状态：${item.status} · 权威效应：${item.authority_effect}`,
  ].filter(Boolean).join('；');
  return '<div class="wb-line" data-evidence-id="' + escapeHtml(item.evidence_id) + '" '
    + 'data-source-type="' + escapeHtml(item.source_type) + '">'
    + `<b>${escapeHtml(item.label || item.field)}</b>`
    + ' <span class="badge">' + escapeHtml(valueText(item.value)) + '</span>'
    + ` <span class="status-badge st-${badge}">${escapeHtml(sourceType.label)}</span>`
    + `<div class="demo-note">${escapeHtml(detail)}</div>`
    + `<button class="link danger" id="withdraw_${escapeHtml(item.evidence_id)}">撤回该工程依据</button>`
    + '</div>';
}

export function planningEvidenceSummary(flow) {
  const evidence = flow.planning_evidence || {};
  const fields = flow.planning_evidence_fields || {};
  const status = evidence.field_status || [];
  const active = evidence.active || [];
  const pending = status.filter((item) => item.status === 'evidence_required');
  const incompatible = status.filter((item) => item.status === 'incompatible');
  const required = status.filter((item) => item.demanded_by_requirement);
  return {
    fields: fields.fields || {},
    sourceTypes: fields.source_types || [],
    status,
    active,
    pending,
    incompatible,
    required,
    disclosureLines: evidence.disclosure_lines || [],
    aircraftEvidence: evidence.aircraft_evidence || null,
  };
}

/** 作用对象（与后端 `PROJECT_EVIDENCE_SCOPES` 一一对应）。 */
export const EVIDENCE_SCOPE_TEXT = {
  project: '项目级参数',
  aircraft: '机载平台能力',
  device: '设备 / 提供者',
  operation: '本次运行场景',
};

/** 某个作用对象下可供选择的 `target_id`（来自活项目，绝不手填自由文本）。 */
export function evidenceScopeTargets(flow, scope) {
  if (scope === 'device') {
    return (((flow || {}).device_catalog || {}).items || [])
      .map((item) => String(item.device_id || '')).filter(Boolean);
  }
  if (scope === 'operation') {
    return ((flow || {}).operational_routes || [])
      .map((item) => String(item.route_id || '')).filter(Boolean);
  }
  if (scope === 'aircraft') {
    return [String((flow || {}).selected_aircraft_profile_id || '')].filter(Boolean);
  }
  return [];
}

/**
 * 需要**显式作用对象**才能登记的字段（Round 2.7）。
 *
 * 机载能力字段仍走既有的逐项核对表单；这里只列出必须以
 * `scope=device`（地面提供者）或 `scope=operation`（本次运行场景）登记的字段，
 * 每行固定一个作用对象，避免用户在界面上选出"不存在的组合"。
 */
export function scopedEvidenceModels(flow) {
  const fields = (((flow || {}).planning_evidence_fields || {}).fields) || {};
  const models = [];
  for (const [field, spec] of Object.entries(fields)) {
    const scopes = spec.scopes || null;
    if (!scopes) continue;
    for (const scope of scopes) {
      if (scope !== 'device' && scope !== 'operation') continue;
      models.push({
        field, spec, scope, targets: evidenceScopeTargets(flow, scope),
      });
    }
  }
  return models;
}

function scopedEvidenceRow(model) {
  const { field, spec, scope, targets } = model;
  const name = `pevs_${field}_${scope}`;
  const multiple = spec.value_type === 'enum';
  const label = spec.label || field;
  const scopeText = EVIDENCE_SCOPE_TEXT[scope] || scope;
  if (!targets.length) {
    return '<fieldset class="cns-requirement" data-evidence-field="' + escapeHtml(field)
      + '" data-evidence-scope="' + escapeHtml(scope) + '">'
      + `<legend>${escapeHtml(label)} · ${escapeHtml(scopeText)}</legend>`
      + '<div class="demo-note">当前项目没有可用的作用对象，无法登记该工程假设'
      + '（设备侧需要至少一条设备目录条目；运行场景侧需要至少一条运行航路）。</div>'
      + '</fieldset>';
  }
  const targetHint = scope === 'device' ? '目标设备（device_id）' : '目标运行航路（route_id）';
  return '<fieldset class="cns-requirement" data-evidence-field="' + escapeHtml(field)
    + '" data-evidence-scope="' + escapeHtml(scope) + '">'
    + `<legend>${escapeHtml(label)} · ${escapeHtml(scopeText)}</legend>`
    + `<div class="demo-note">${escapeHtml(spec.semantics || '')}</div>`
    + '<div class="form-grid">'
    + `<label>${escapeHtml(targetHint)}${optionList(name + '_target', targets, {})}</label>`
    + `<label>取值（${escapeHtml(VALUE_TYPE_HINT[spec.value_type] || spec.value_type)}）`
    + (spec.value_type === 'number' ? numberInput(name) : optionList(name, spec.allowed || [], { multiple }))
    + '</label>'
    + '</div>'
    + '<div class="parameter-note">本记录是**本项目的规划输入**（工程假设），'
    + '不是厂家设备事实，也不会写入设备目录 / 机载档案：它只在规划消费点叠加，'
    + '并在结果与报告中带来源标注披露。</div>'
    + `<label>来源类型<select id="${name}_source_type">`
    + Object.keys(EVIDENCE_SOURCE_TYPE_TEXT)
      .map((key) => `<option value="${escapeHtml(key)}">`
        + `${escapeHtml(EVIDENCE_SOURCE_TYPE_TEXT[key].label)}</option>`).join('')
    + '</select></label>'
    + '<div class="form-grid">'
    + `<label>来源 / 依据出处<input id="${name}_source" value=""></label>`
    + `<label>录入人<input id="${name}_declared_by" value=""></label>`
    + '</div>'
    + `<label>假设陈述（工程规划假设必填）<input id="${name}_statement" value=""></label>`
    + `<label>采用依据 / 理由（工程规划假设必填）<input id="${name}_reason" value=""></label>`
    + `<label>报告披露文本（工程规划假设必填）<input id="${name}_disclosure" value=""></label>`
    + `<label><input type="checkbox" id="${name}_confirmed"> 我确认以上内容（事实需有资料支持；`
    + '假设须明确标注为工程规划假设，不得冒充厂家设备事实）</label>'
    + `<button class="primary" id="save_${escapeHtml(name)}">保存该工程依据</button>`
    + '</fieldset>';
}

/**
 * 渲染「工程依据 / 规划假设」区块。
 *
 * @param {object} flow 当前 workflow 快照
 * @returns {string} HTML
 */
export function renderPlanningEvidence(flow) {
  const summary = planningEvidenceSummary(flow);
  const { status } = summary;

  if (!status.length) {
    return '<div class="demo-note">工程依据字段清单尚未载入（`/api/planning-evidence/fields`）。'
      + '在字段清单可用前，系统不会猜测任何取值。</div>';
  }

  const rows = status.map((item) => {
    const statusInfo = STATUS_TEXT[item.status] || { text: item.status, badge: 'unknown' };
    const typeHint = VALUE_TYPE_HINT[item.value_type] || item.value_type;
    const name = 'pev_' + item.field;
    const multiple = item.value_type === 'enum';
    const options = item.allowed || [];
    const demandLine = item.demanded_by_requirement
      ? `需求要求 <code>${escapeHtml(item.demand_field)}</code> = `
        + `<b>${escapeHtml(valueText(item.required_value))}</b>`
      : `当前需求未声明 <code>${escapeHtml(item.demand_field)}</code>（不是门禁条件）`;
    return '<fieldset class="cns-requirement" data-evidence-field="' + escapeHtml(item.field) + '">'
      + `<legend>${escapeHtml(item.label)} <span class="status-badge st-${statusInfo.badge}">`
      + `${escapeHtml(statusInfo.text)}</span></legend>`
      + `<div class="demo-note">${escapeHtml(item.semantics)}</div>`
      + `<div class="flow-summary">${demandLine}<br>`
      + `机载档案声明：<b>${escapeHtml(valueText(item.declared_by_aircraft_profile))}</b> · `
      + `当前生效值：<b>${escapeHtml(valueText(item.effective_value))}</b><br>`
      + `逐项核对：<b>${escapeHtml(item.status_reason || '—')}</b></div>`
      + `<label>取值（${escapeHtml(typeHint)}）`
      + (item.value_type === 'number' ? numberInput(name) : optionList(name, options, { multiple }))
      + '</label>'
      + `<label>来源类型<select id="${name}_source_type">`
      + (summary.sourceTypes.length ? summary.sourceTypes : Object.keys(EVIDENCE_SOURCE_TYPE_TEXT))
        .map((key) => `<option value="${escapeHtml(key)}">`
          + `${escapeHtml((EVIDENCE_SOURCE_TYPE_TEXT[key] || {}).label || key)}</option>`).join('')
      + '</select></label>'
      + '<div class="form-grid">'
      + `<label>来源 / 依据出处<input id="${name}_source" value=""></label>`
      + `<label>录入人<input id="${name}_declared_by" value=""></label>`
      + '</div>'
      + `<label>假设陈述（工程规划假设必填）<input id="${name}_statement" value=""></label>`
      + `<label>采用依据 / 理由（工程规划假设必填）<input id="${name}_reason" value=""></label>`
      + `<label>报告披露文本（工程规划假设必填）<input id="${name}_disclosure" value=""></label>`
      + `<label><input type="checkbox" id="${name}_confirmed"> 我确认以上内容（事实需有资料支持；`
      + '假设须明确标注为工程规划假设，不得冒充厂家设备事实）</label>'
      + `<button class="primary" id="save_${escapeHtml(item.field)}">保存该工程依据</button>`
      + '</fieldset>';
  }).join('');

  //: Round 2.7：设备侧 / 运行场景假设的录入行（每行固定一个作用对象）。
  const scopedRows = scopedEvidenceModels(flow).map(scopedEvidenceRow).join('');

  const blockers = summary.pending.length
    ? summary.pending.map((item) => ({
      kind: 'assumption',
      text: `${item.label}：缺少工程依据，相关判定保持"无法确认"（不会自动判为不满足）。`,
      detail: `${item.semantics} 取值类型：${VALUE_TYPE_HINT[item.value_type] || item.value_type}；`
        + `可选取值：${(item.allowed || []).join(' / ')}`,
    }))
    : [];

  //: **已确认不满足**不是"缺证据"：必须单独、显式地列成阻塞项，
  //: 否则用户会以为只要补一条工程依据就能通过。
  const incompatibleRows = summary.incompatible.map((item) => ({
    kind: 'blocker',
    text: `${item.label}：已确认不满足需求 —— ${item.status_reason || ''}`,
    detail: `需求要求 ${item.demand_field} = ${valueText(item.required_value)}；`
      + `当前生效值 = ${valueText(item.effective_value)}。`
      + '这属于真实不兼容，只能用**与需求一致的取值**或修改需求来解决，'
      + '系统不会把它降级成"缺证据"。',
  }));

  const activeBlock = summary.active.length
    ? '<div class="wb-block">' + summary.active.map((item) => planningEvidenceItemLine({
      ...item,
      label: (summary.fields[item.field] || {}).label
        || (summary.status.find((entry) => entry.field === item.field) || {}).label
        || item.field,
    })).join('') + '</div>'
    : '<div class="demo-note">尚未录入任何工程依据。缺证据时判定保持"无法确认"，'
      + '不会被当成"已满足"，也不会被自动降级成"不满足"。</div>';

  const disclosureBlock = summary.disclosureLines.length
    ? '<div class="parameter-note">报告披露：<br>' + summary.disclosureLines
      .map((line) => escapeHtml(line)).join('<br>') + '</div>'
    : '<div class="parameter-note">报告披露：当前没有生效的工程假设；'
      + '报告中不会出现"把假设写成事实"的表述。</div>';

  return wbBlock('本段目标', '<div class="demo-note">当主链因缺少工程证据而无法确认时，'
    + '由用户在此正式补录：明确区分「有正式资料支持的事实」「工程规划假设」与「尚无依据」。'
    + '工程假设进入独立的规划输入容器（<code>project_state.planning_evidence</code>），'
    + '不写入设备目录，也不改写机载档案源文件；它参与 P8/P14/P15/P16 时一律带来源标注，'
    + '并在报告中自动披露。</div>')
    + wbBlock('缺失工程证据', blockerList([...incompatibleRows, ...blockers],
      '当前没有因缺工程依据而无法确认的字段。'))
    + wbBlock('录入 / 确认工程依据', '<div class="cns-requirements">' + rows + '</div>')
    + wbBlock('设备侧 / 运行场景工程假设（Round 2.7）',
      '<div class="demo-note">这里登记的是**显式作用对象**的规划假设：'
      + '<b>设备侧</b>（地面提供者的类型事实，例如网络归属范围）与 '
      + '<b>本次运行场景</b>（例如本次运行的机载接口 / RID 参与能力）。'
      + '它们只作用于所选的设备 / 运行航路，绝不写入设备目录，也绝不改写机载档案；'
      + '报告与结果会逐条披露"这条输入来自工程假设"。</div>'
      + '<div class="cns-requirements">'
      + (scopedRows || '<div class="demo-note">当前字段清单没有需要设备侧 / 运行场景作用对象的字段。</div>')
      + '</div>')
    + wbBlock('已录入的工程依据', activeBlock + disclosureBlock
      + '<div class="demo-note">来源类型徽标说明：'
      + Object.entries(EVIDENCE_SOURCE_TYPE_TEXT).map(([key, item]) => (
        `<b>${escapeHtml(item.label)}</b>（${escapeHtml(key)}）—— ${escapeHtml(item.note)}`
      )).join('<br>') + '</div>');
}

/**
 * 绑定「工程依据 / 规划假设」交互。与人工点击走**完全相同**的正式接口。
 *
 * @param {object} c 工作台控制器
 * @param {object} [deps] 可注入依赖（测试用）
 */
export function bindPlanningEvidence(c, deps = {}) {
  //: 与人工点击完全相同的正式接口。命令成功后必须让全局 flow 落地：
  //: 优先用工作台统一的 `resourceMutationAndRefresh`（POST → 重读完整 workflow →
  //: 应用），它同时兼容"后端返回完整快照"与"后端只返回资源局部对象"两种契约；
  //: 没有该依赖时（测试替身）回落到 `resourceAction`。
  const action = deps.resourceAction
    || ((path, payload) => (c.resourceMutationAndRefresh
      ? c.resourceMutationAndRefresh(path, payload)
      : c.resourceAction(path, payload)));
  const reportError = deps.panelError || ((message) => c.panelError && c.panelError(message));
  const flow = () => c.flow();
  const summary = () => planningEvidenceSummary(flow());
  const selectedValues = (id, multiple) => {
    const element = c.$(id);
    if (!element) return null;
    if (!multiple) return element.value || null;
    return [...element.selectedOptions].map((option) => option.value).filter(Boolean);
  };
  const text = (id) => {
    const element = c.$(id);
    if (!element) return null;
    const trimmed = String(element.value || '').trim();
    return trimmed || null;
  };
  const checked = (id) => Boolean(c.$(id) && c.$(id).checked);
  const numberValue = (id) => {
    const element = c.$(id);
    if (!element) return null;
    const raw = String(element.value || '').trim();
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const valueOf = (id, valueType) => (
    valueType === 'number' ? numberValue(id) : selectedValues(id, valueType === 'enum')
  );

  for (const item of summary().status) {
    const button = c.$('save_' + item.field);
    if (!button) continue;
    const prefix = 'pev_' + item.field;
    //: 提交失败必须让用户看到**业务中文原因**（例如"工程规划假设缺少必填字段"），
    //: 绝不静默失败——否则用户以为保存成功，重开却发现什么都没有。
    button.onclick = async () => {
      try {
        const sourceType = c.$(prefix + '_source_type').value;
        const value = valueOf(prefix, item.value_type);
        return await action('/api/planning-evidence', {
          planning_evidence: {
            field: item.field,
            scope: 'aircraft',
            target_id: flow().selected_aircraft_profile_id || null,
            source_type: sourceType,
            value: sourceType === 'unknown' ? null : value,
            source: text(prefix + '_source'),
            statement: text(prefix + '_statement'),
            reason: text(prefix + '_reason'),
            report_disclosure: text(prefix + '_disclosure'),
            declared_by: text(prefix + '_declared_by') || 'user',
            confirmed: checked(prefix + '_confirmed'),
            confirmed_by_user: checked(prefix + '_confirmed'),
          },
        });
      } catch (error) {
        reportError('保存工程依据失败：' + ((error && error.message) || error));
        throw error;
      }
    };
  }

  for (const model of scopedEvidenceModels(flow())) {
    const name = `pevs_${model.field}_${model.scope}`;
    const button = c.$('save_' + name);
    if (!button) continue;
    button.onclick = async () => {
      try {
        const sourceType = c.$(name + '_source_type').value;
        const value = valueOf(name, model.spec.value_type);
        return await action('/api/planning-evidence', {
          planning_evidence: {
            field: model.field,
            //: 作用对象由本行固定（设备侧 / 本次运行场景），用户只能选目标实例。
            scope: model.scope,
            target_id: c.$(name + '_target').value || null,
            source_type: sourceType,
            value: sourceType === 'unknown' ? null : value,
            source: text(name + '_source'),
            statement: text(name + '_statement'),
            reason: text(name + '_reason'),
            report_disclosure: text(name + '_disclosure'),
            declared_by: text(name + '_declared_by') || 'user',
            confirmed: checked(name + '_confirmed'),
            confirmed_by_user: checked(name + '_confirmed'),
          },
        });
      } catch (error) {
        reportError('保存工程依据失败：' + ((error && error.message) || error));
        throw error;
      }
    };
  }

  for (const item of summary().active) {
    const button = c.$('withdraw_' + item.evidence_id);
    if (!button) continue;
    button.onclick = async () => {
      try {
        return await action('/api/planning-evidence/withdraw', {
          evidence_id: item.evidence_id,
        });
      } catch (error) {
        reportError('撤回工程依据失败：' + ((error && error.message) || error));
        throw error;
      }
    };
  }
}

export const planningEvidenceInternals = {
  STATUS_TEXT, VALUE_TYPE_HINT, valueText,
};
