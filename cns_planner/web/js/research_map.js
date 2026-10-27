/** Pure C3-B research view model shared by tests and the 2D browser app. */

export const LAYER_ORDER = ['C', 'RID', 'Radar', 'S', 'N'];
export const LAYER_LABELS = {
  C: 'C 通信',
  RID: 'RID 合作监视',
  Radar: 'Radar 非合作',
  S: 'S 监视综合',
  N: 'N 导航动作',
};
export const LAYER_COLORS = {
  C: '#0f8a78',
  RID: '#2f9e6f',
  Radar: '#c62828',
  S: '#6c757d',
  N: '#7256a1',
};

export function parameterValue(schema, selection) {
  const ids = schema?.ids || [];
  if (ids.length !== 1) throw new Error('research parameter schema invalid');
  const value = selection?.[ids[0]] || schema.default;
  if (!Array.isArray(value) || value.length !== 2 || new Set(value).size !== 2) {
    throw new Error('必须选择两个不同的研究站址');
  }
  const allowed = new Set((schema.options || []).map(item => item.site_id));
  if (value.some(item => !allowed.has(item))) throw new Error('研究站址不在白名单');
  return {[ids[0]]: [...value]};
}

export function parameterChanged(schema, value) {
  const id = schema?.ids?.[0];
  const current = value?.[id] || [];
  const baseline = schema?.default || [];
  return current.length === baseline.length
    && current.some((item, index) => item !== baseline[index]);
}

export function buildMapModel(manifest) {
  const map = manifest?.map_projection;
  if (!map) throw new Error('run map projection missing');
  if (map.run_id !== manifest.run_id) throw new Error('map/run identity mismatch');
  if (map.authority !== 'research_isolated') throw new Error('map authority invalid');
  const counts = map.counts || {};
  if (counts.sites < 1 || counts.devices < 1) throw new Error('map input counts missing');
  const layers = new Map((map.layers || []).map(layer => [layer.id, layer]));
  for (const id of LAYER_ORDER) {
    const layer = layers.get(id);
    if (!layer) throw new Error(`map layer missing: ${id}`);
    if (id === 'Radar') {
      if (layer.conclusion !== 'unknown' || layer.status !== 'not_calculated') {
        throw new Error('Radar must stay not_calculated/unknown');
      }
    }
    if (id === 'S' && layer.status !== 'unknown') {
      throw new Error('S must stay unknown when Radar is not calculated');
    }
    if (layer.status === 'satisfied' && layer.conclusion === 'unknown') {
      throw new Error(`unknown layer must not be displayed as satisfied: ${id}`);
    }
  }
  return {map, layers, counts};
}

export function stageRows(view) {
  return ['P14', 'P15', 'P16', 'P17', 'P18'].map(stage => ({
    stage,
    label: view?.[stage]?.label || stage,
    status: view?.[stage]?.status || 'unknown',
    tone: view?.[stage]?.tone || 'unknown',
  }));
}

export function shortSha(value, length = 12) {
  const text = value == null ? 'unknown' : String(value);
  return text.length <= length ? text : `${text.slice(0, length)}…`;
}

export function reportUrl(runId, format = 'html') {
  if (!runId) throw new Error('run id required');
  return `/api/research/runs/${encodeURIComponent(runId)}/report?format=${format === 'markdown' ? 'markdown' : 'html'}`;
}

export function manifestIdentity(manifest) {
  if (!manifest?.run_id) throw new Error('run manifest identity missing');
  const identity = {
    runId: manifest.run_id,
    inputVersionId: manifest.input_version?.id || null,
    inputFingerprint: manifest.input_version?.input_fingerprint || null,
    parameterFingerprint: manifest.input_version?.parameter_fingerprint
      || manifest.research_parameters?.fingerprint || null,
    contractVersion: manifest.contract_version || null,
    resultListSha256: manifest.authoritative_result_list_sha256 || null,
    stageShas: manifest.stages || {},
  };
  if (!identity.inputVersionId || !identity.contractVersion || !identity.resultListSha256) {
    throw new Error('run manifest identity incomplete');
  }
  return identity;
}

export function statusText(status) {
  return {
    failed: '业务失败（证据未知）',
    business_failed: '业务失败（证据未知）',
    proposal_ready: '提案就绪（未采用）',
    fully_satisfied: '连续中断局部满足',
    review_initialized: '评审已初始化（未确认）',
    not_calculated: '未计算',
    unknown: '未知（不得读作满足）',
    satisfied: '已满足',
  }[status] || String(status || 'unknown');
}
