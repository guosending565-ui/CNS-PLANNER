import { researchIdentity, researchRunView } from './research_runs.js';
import {
  buildMapModel, LAYER_LABELS, parameterChanged, parameterValue, reportUrl,
  shortSha, stageRows, statusText,
} from './research_map.js';
import { createResearchMap } from './research_map_renderer.js';

const $ = id => document.getElementById(id);
const state = {
  boot: null, token: '', manifest: null, view: null, report: null,
  activeRun: null, task: null, timer: null, busy: false, previousFingerprint: null,
};
let map = null;

function toast(message, tone = '') {
  const node = $('toast');
  node.textContent = message;
  node.className = `research-toast ${tone}`;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 5200);
}

function setConnection(text, tone = '') {
  const node = $('connection');
  node.textContent = text;
  node.className = `research-connection ${tone}`;
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (state.token) headers['X-CNS-Token'] = state.token;
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers});
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!response.ok) {
    const detail = body?.detail || body?.error || `${response.status}`;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    error.body = body;
    error.headers = response.headers;
    throw error;
  }
  return {body, headers: response.headers, status: response.status};
}

function pill(node, text, tone = '') {
  if (!node) return;
  node.textContent = text;
  node.className = `research-pill ${tone}`;
}

function optionText(option) {
  const kind = option.host_kind === 'planned_new_site' ? '规划新建' : '既有宿主';
  const subsystems = (option.subsystems || []).join('/') || '—';
  return `${option.label || option.site_id} · ${kind} · ${subsystems}`;
}

function renderInput(input) {
  const summary = input?.input_summary || {};
  $('inputRoute').textContent = input?.route_id || 'R0005';
  $('sourceRunId').textContent = input?.source_run_id || '—';
  $('sourceStateSha').textContent = shortSha(input?.source_state_sha256, 18);
  $('sourceStateSha').title = input?.source_state_sha256 || '';
  const twoKm = summary.two_km_contract === true;
  $('twoKmStatus').textContent = twoKm ? '2 km 合同已冻结' : '运行内逐 provider 复核';
  $('twoKmStatus').style.color = twoKm ? '#237a55' : '#9a6700';
  const counts = $('inputCounts').children;
  const values = [
    summary.sites, summary.devices, summary.existing_hosts,
    summary.planned_new_sites, summary.n_actions,
  ];
  values.forEach((value, index) => {
    counts[index].querySelector('b').textContent = value ?? '—';
  });
  pill($('inputReady'), input?.ready ? '研究输入可用' : '输入不可用',
    input?.ready ? 'ok' : 'bad');
  const schema = input?.parameter_schema;
  const selectA = $('siteSelectA');
  const selectB = $('siteSelectB');
  selectA.innerHTML = '';
  selectB.innerHTML = '';
  if (!schema) {
    selectA.innerHTML = '<option value="">无可用参数</option>';
    selectB.innerHTML = '<option value="">无可用参数</option>';
    return;
  }
  for (const option of schema.options || []) {
    for (const select of [selectA, selectB]) {
      const node = document.createElement('option');
      node.value = option.site_id;
      node.textContent = optionText(option);
      select.appendChild(node);
    }
  }
  selectA.value = schema.default[0];
  selectB.value = schema.default[1];
  updateParameterDraft();
}

function currentParameterDraft() {
  const schema = state.boot?.input?.parameter_schema;
  if (!schema) throw new Error('参数合同未加载');
  return parameterValue(schema, {[schema.ids[0]]: [$('siteSelectA').value, $('siteSelectB').value]});
}

function applyManifestParameters(manifest) {
  const schema = state.boot?.input?.parameter_schema;
  const values = manifest?.research_parameters?.values;
  if (!schema || !values) return;
  const id = schema.ids[0];
  const selected = values[id];
  if (!Array.isArray(selected) || selected.length !== 2) return;
  $('siteSelectA').value = selected[0];
  $('siteSelectB').value = selected[1];
}

function updateParameterDraft() {
  const schema = state.boot?.input?.parameter_schema;
  if (!schema) { $('submitResearch').disabled = true; return; }
  let changed = false, error = null;
  try {
    changed = parameterChanged(schema, currentParameterDraft());
  } catch (exc) {
    error = exc.message;
  }
  const node = $('parameterDraft');
  node.textContent = error || (changed ? '已修改：将生成新输入指纹' : '默认输入（未修改）');
  node.className = error || changed ? 'changed' : '';
  pill($('parameterChanged'), error ? '参数无效' : changed ? '已修改' : '默认',
    error ? 'bad' : changed ? 'warning' : '');
  $('submitResearch').disabled = Boolean(error) || state.busy || !state.boot?.input?.ready;
  $('submitHint').textContent = error || (state.busy ? '任务运行中…'
    : changed ? '提交后真实执行 P14→P18，旧结果不会被冒充' : '使用默认研究输入，真实执行 P14→P18');
}

function renderRuns(runs = []) {
  const select = $('runSelect');
  select.innerHTML = '';
  for (const run of runs) {
    const option = document.createElement('option');
    option.value = run.run_id;
    option.textContent = `${run.run_id} · ${run.status}${run.result_ready ? '' : ' (未发布)'}`;
    select.appendChild(option);
  }
  if (state.activeRun) select.value = state.activeRun;
  const list = $('runList');
  list.innerHTML = '';
  for (const run of runs) {
    const row = document.createElement('div');
    row.className = 'research-run-row';
    const label = document.createElement('code');
    label.textContent = run.run_id;
    const status = document.createElement('span');
    status.className = `research-pill ${run.result_ready ? 'ok' : run.status === 'failed' ? 'bad' : 'unknown'}`;
    status.textContent = run.status;
    row.append(label, status);
    row.addEventListener('click', () => loadRun(run.run_id));
    list.appendChild(row);
  }
  pill($('runCount'), String(runs.length), runs.length ? 'ok' : 'unknown');
}

function setTaskPanel(task) {
  $('taskId').textContent = task?.task_id || '—';
  $('taskId').title = task?.task_id || '';
  $('runningRunId').textContent = task?.run_id
    || (task?.scoped_run_id) || state.activeRun || '—';
  $('taskProgress').textContent = Number.isFinite(task?.progress)
    ? `${Math.round(task.progress * 100)}%` : '—';
  $('taskProgressBar').style.width = Number.isFinite(task?.progress)
    ? `${Math.round(task.progress * 100)}%` : '0%';
  $('taskMessage').textContent = task?.message || '—';
  const status = task?.status || task?.advanced?.status || '未提交';
  pill($('taskState'), status, ['succeeded'].includes(status) ? 'ok'
    : ['failed', 'stale', 'cancelled'].includes(status) ? 'bad' : 'unknown');
  const mode = task?.result_summary?.execution?.mode || task?.worker_payload?.mode;
  $('solverExecuted').textContent = mode === 'fresh_compute'
    ? '真实 fresh_compute' : mode === 'resume_checkpoint' ? '仅重载，未求解' : '未运行';
  $('solverExecuted').style.color = mode === 'fresh_compute' ? '#237a55' : '#43515c';
}

function renderIdentity(view) {
  const identity = view.identity;
  $('identityRunId').textContent = identity.runId || '—';
  $('identityInputVersion').textContent = identity.inputVersionId || '—';
  $('identityContract').textContent = identity.contractVersion || '—';
  $('identityResultSha').textContent = shortSha(identity.resultListSha256, 18);
  $('identityResultSha').title = identity.resultListSha256 || '';
  $('identityParameterSha').textContent = shortSha(identity.parameterFingerprint, 18);
  $('identityParameterSha').title = identity.parameterFingerprint || '';
  const note = $('identityNote');
  note.classList.remove('bad');
  note.textContent = '地图、P14–P18、报告均消费同一 run manifest；已核对 run_id / input_version / contract_version / 结果清单 SHA。';
  pill($('identityState'), '一致', 'ok');
  $('footerIdentity').textContent = `${identity.runId} · ${identity.inputVersionId} · ${shortSha(identity.resultListSha256, 12)}`;
}

function renderStages(view) {
  const container = $('stageCards');
  container.innerHTML = '';
  const tabs = $('stageTabs');
  tabs.innerHTML = '';
  for (const row of stageRows(view)) {
    const card = document.createElement('div');
    card.className = 'research-stage';
    card.dataset.stage = row.stage;
    card.innerHTML = `<span class="stage-id">${row.stage}</span>
      <span class="stage-label">${row.label}</span>
      <span class="stage-status ${row.tone}">${statusText(row.status)}</span>`;
    card.addEventListener('click', () => selectStage(row.stage));
    container.appendChild(card);
    const tab = document.createElement('button');
    tab.dataset.stage = row.stage;
    tab.textContent = row.stage;
    tab.addEventListener('click', () => selectStage(row.stage));
    tabs.appendChild(tab);
  }
}

async function selectStage(stage) {
  document.querySelectorAll('.research-stage').forEach(node => {
    node.classList.toggle('active', node.dataset.stage === stage);
  });
  document.querySelectorAll('#stageTabs button').forEach(node => {
    node.classList.toggle('active', node.dataset.stage === stage);
  });
  pill($('stageDetailLabel'), stage, 'unknown');
  if (!state.activeRun) return;
  $('stageDetail').textContent = '读取权威结果…';
  try {
    const {body} = await api(`/api/research/runs/${encodeURIComponent(state.activeRun)}/results/${stage}`);
    const payload = body?.payload || body;
    const result = payload?.result || payload;
    const summary = {};
    for (const key of ['status', 'algorithm_id', 'algorithm_version', 'input_fingerprint',
      'route_count', 'selected_actions', 'candidate_actions', 'cost_summary', 'parameters',
      'step6_gate', 'plan_stage']) {
      if (result?.[key] !== undefined) summary[key] = result[key];
    }
    if (stage === 'P18') {
      summary.status = payload.status || 'unknown';
      summary.stage_status = payload.status || 'unknown';
      summary.review_status = result?.status || 'unknown';
      summary.confirmed = payload.confirmed === true;
      summary.applied = payload.applied === true;
      summary.confirmation_allowed = payload.confirmation_allowed === true;
      summary.variants = (payload.variants || []).map(item => ({
        variant_id: item.variant_id, gate: item.confirmation_gate,
        confirmation_allowed: item.confirmation_allowed,
        unknown_voxel_count: item.unknown_voxel_count,
      }));
    }
    if (!Object.keys(summary).length) summary.result_status = payload?.status || 'unknown';
    if (Array.isArray(summary.selected_actions)) {
      summary.selected_actions = summary.selected_actions.map(item => item.action_id || item);
    }
    if (Array.isArray(summary.candidate_actions)) {
      summary.candidate_actions_count = summary.candidate_actions.length;
      delete summary.candidate_actions;
    }
    $('stageDetail').textContent = JSON.stringify(summary, null, 2);
    pill($('stageDetailLabel'), `${stage} · 权威结果`, 'ok');
  } catch (exc) {
    $('stageDetail').textContent = `读取失败：${exc.message}`;
    pill($('stageDetailLabel'), `${stage} · 读取失败`, 'bad');
  }
}

function renderMap(view) {
  const model = buildMapModel({run_id: view.runId, map_projection: view.map});
  if (!map) {
    map = createResearchMap({
      canvas: $('researchCanvas'),
      getModel: () => state.mapModel,
      getVisibility: () => {
        const visibility = {};
        document.querySelectorAll('#layerToggles input[data-layer]').forEach(node => {
          visibility[node.dataset.layer] = node.checked;
        });
        return visibility;
      },
      onSelect: item => {
        const node = $('mapInspect');
        if (!item) { node.hidden = true; return; }
        node.hidden = false;
        node.innerHTML = `<h3>${item.title}</h3><div class="kind">${item.kind}</div>` +
          item.rows.map(([key, value]) => `<div class="row"><span>${key}</span><b>${value}</b></div>`).join('');
      },
    });
    document.querySelectorAll('#layerToggles input[data-layer]').forEach(node => {
      node.addEventListener('change', () => map.draw());
    });
    $('fitMap').addEventListener('click', () => map.fit());
  }
  state.mapModel = model.map;
  map.load(model.map);
  const counts = model.counts;
  $('mapSubtitle').textContent = `${view.runId} · ${counts.sites} 站 / ${counts.devices} 设备 · ${counts.existing_hosts} 既有宿主 + ${counts.planned_new_sites} 规划新建 · ${counts.n_actions} 个 N 动作 · 结果清单 ${shortSha(view.identity.resultListSha256, 12)}`;
  const kpis = $('mapKpis').children;
  kpis[0].querySelector('b').textContent = counts.sites;
  kpis[1].querySelector('b').textContent = counts.devices;
  kpis[2].querySelector('b').textContent = counts.n_actions;
  const legend = $('nActionLegend');
  if (legend) legend.textContent = `${counts.n_actions} 个 N 动作`;
}

function updateCountsFromManifest(manifest) {
  const projection = manifest.map_projection || {};
  const counts = projection.counts || {};
  const nodes = $('inputCounts').children;
  [counts.sites, counts.devices, counts.existing_hosts, counts.planned_new_sites, counts.n_actions]
    .forEach((value, index) => {
      if (value !== undefined) nodes[index].querySelector('b').textContent = value;
    });
  const radius = manifest.input_version?.communication_radius || {};
  $('twoKmStatus').textContent = radius.communication_is_2km_by_surface
    ? '2 km 合同已复核' : '2 km 合同未通过';
  $('twoKmStatus').style.color = radius.communication_is_2km_by_surface ? '#237a55' : '#b42318';
}

async function loadReport(runId) {
  $('reportPreview').textContent = '生成报告预览…';
  try {
    const response = await fetch(reportUrl(runId, 'markdown'), {
      headers: {'X-CNS-Token': state.token},
    });
    if (!response.ok) throw new Error(`报告生成失败：${response.status}`);
    const text = await response.text();
    state.report = {
      markdown: text,
      sha: response.headers.get('X-CNS-Report-SHA256'),
      payloadSha: response.headers.get('X-CNS-Report-Payload-SHA256'),
    };
    $('reportPreview').textContent = text.slice(0, 2600);
    $('reportSha').textContent = shortSha(state.report.sha, 20);
    $('reportSha').title = state.report.sha || '';
    pill($('reportState'), '同一 manifest', 'ok');
    $('downloadReport').disabled = false;
    $('downloadMarkdown').disabled = false;
  } catch (exc) {
    $('reportPreview').textContent = exc.message;
    pill($('reportState'), '生成失败', 'bad');
  }
}

async function loadRun(runId) {
  if (!runId) return;
  setConnection('读取 run manifest…');
  try {
    const {body: manifest} = await api(`/api/research/runs/${encodeURIComponent(runId)}/manifest`);
    const view = researchRunView(null, manifest);
    state.manifest = manifest;
    state.view = view;
    state.activeRun = manifest.run_id;
    state.previousFingerprint = view.identity.inputFingerprint;
    applyManifestParameters(manifest);
    updateParameterDraft();
    renderIdentity(view);
    renderStages(view);
    renderMap(view);
    updateCountsFromManifest(manifest);
    $('runSelect').value = runId;
    history.replaceState(null, '', `?run=${encodeURIComponent(runId)}`);
    const threeD = document.getElementById('open3d');
    if (threeD) threeD.href = `/research_3d.html?run=${encodeURIComponent(runId)}`;
    const run = (state.boot?.runs || []).find(item => item.run_id === runId);
    if (run?.task_id) {
      try {
        const {body: task} = await api(`/api/tasks/${encodeURIComponent(run.task_id)}`);
        state.task = task;
        setTaskPanel(task);
        researchIdentity(task, manifest);
      } catch (exc) {
        setTaskPanel({...run, status: run.status, progress: null});
      }
    } else {
      setTaskPanel({run_id: runId, status: 'published', progress: 1, message: '已发布'});
    }
    await loadReport(runId);
    await selectStage('P16');
    setConnection('研究运行时 · research_isolated', 'ok');
    $('footerState').textContent = '地图 / 阶段 / 报告共享同一 run manifest；未重新求解，未写正式 ProjectState';
    window.dispatchEvent(new CustomEvent('c3b-run-loaded', {detail: {runId}}));
  } catch (exc) {
    setConnection(`读取失败：${exc.message}`, 'bad');
    toast(`读取 run 失败：${exc.message}`, 'bad');
  }
}

async function refreshRuns() {
  const {body} = await api('/api/research/bootstrap');
  // bootstrap is intentionally token-free and returns a fresh token; keep the new session token.
  state.token = body.token;
  state.boot = {...state.boot, ...body};
  renderRuns(body.runs || []);
}

async function pollTask(taskId, runId) {
  clearTimeout(state.timer);
  try {
    const {body: task} = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    state.task = task;
    setTaskPanel({...task, run_id: runId});
    const status = task.status || task.advanced?.status;
    if (['succeeded'].includes(status)) {
      state.busy = false;
      $('submitResearch').disabled = false;
      await refreshRuns();
      await loadRun(runId);
      toast('fresh_compute 成功；已加载新 run manifest', 'ok');
      return;
    }
    if (['failed', 'cancelled', 'stale'].includes(status)) {
      state.busy = false;
      $('submitResearch').disabled = false;
      toast(`任务结束：${status} · ${task.message || ''}`, 'bad');
      return;
    }
    state.timer = setTimeout(() => pollTask(taskId, runId), 1200);
  } catch (exc) {
    state.timer = setTimeout(() => pollTask(taskId, runId), 2200);
    setConnection(`任务轮询重试：${exc.message}`, 'bad');
  }
}

async function submitResearch() {
  if (state.busy) return;
  let parameters;
  try {
    parameters = currentParameterDraft();
  } catch (exc) {
    toast(exc.message, 'bad');
    return;
  }
  const schema = state.boot.input.parameter_schema;
  const changed = parameterChanged(schema, parameters);
  state.busy = true;
  updateParameterDraft();
  $('submitResearch').disabled = true;
  const runId = `c3b-ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  const previous = state.view?.identity.inputFingerprint || null;
  setConnection('提交真实 fresh_compute…');
  try {
    const {body, status} = await api('/api/research/runs', {
      method: 'POST',
      body: JSON.stringify({
        task_type: 'r0005_research_fresh_compute',
        mode: 'fresh_compute',
        run_id: runId,
        source_run_id: state.boot.input.source_run_id,
        parameters,
      }),
    });
    setTaskPanel({
      task_id: body.task_id, run_id: body.run_id, status: body.task_status || 'queued',
      progress: 0, message: body.message, result_summary: {execution: {mode: body.mode}},
    });
    toast(`${changed ? '参数已修改，' : ''}已提交 fresh_compute（HTTP ${status}）`, 'ok');
    pill($('parameterChanged'), changed ? '新输入指纹生成中' : '默认输入', changed ? 'warning' : '');
    state.previousFingerprint = previous;
    await pollTask(body.task_id, body.run_id);
  } catch (exc) {
    state.busy = false;
    updateParameterDraft();
    setConnection(`提交失败：${exc.message}`, 'bad');
    toast(`提交失败：${exc.message}`, 'bad');
  }
}

async function download(kind) {
  if (!state.activeRun) return;
  const format = kind === 'markdown' ? 'markdown' : 'html';
  try {
    const response = await fetch(reportUrl(state.activeRun, format), {
      headers: {'X-CNS-Token': state.token},
    });
    if (!response.ok) throw new Error(`导出失败：${response.status}`);
    const blob = await response.blob();
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    const extension = format === 'markdown' ? 'md' : 'html';
    anchor.download = `R0005-research-report-${state.activeRun}.${extension}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 3000);
    toast(`已导出 ${extension.toUpperCase()} 报告`, 'ok');
  } catch (exc) {
    toast(exc.message, 'bad');
  }
}

async function boot() {
  try {
    const response = await fetch('/api/research/bootstrap');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.boot = data;
    state.token = data.token;
    renderInput(data.input);
    renderRuns(data.runs || []);
    const query = new URLSearchParams(location.search);
    const runId = query.get('run') || data.latest_run_id;
    if (runId) {
      await loadRun(runId);
    } else {
      setConnection('研究运行时就绪；尚无已发布运行', 'ok');
      pill($('inputReady'), '待运行', 'warning');
    }
    window.__C3B_READY__ = true;
    window.__C3B_DIAG__ = () => ({
      boot: state.boot,
      manifest: state.manifest,
      view: state.view,
      task: state.task,
      activeRun: state.activeRun,
      reportSha: state.report?.sha || null,
      identity: state.view?.identity || null,
      counts: state.view?.map?.counts || state.boot?.input?.input_summary || null,
      mapLayers: (state.view?.map?.layers || []).map(layer => ({
        id: layer.id, status: layer.status, conclusion: layer.conclusion,
        featureCount: layer.feature_count,
      })),
      stages: state.view ? stageRows(state.view) : [],
      layerLabels: LAYER_LABELS,
    });
  } catch (exc) {
    setConnection(`研究运行时不可用：${exc.message}`, 'bad');
    pill($('inputReady'), '不可用', 'bad');
    toast(`初始化失败：${exc.message}`, 'bad');
  }
}

$('siteSelectA').addEventListener('change', updateParameterDraft);
$('siteSelectB').addEventListener('change', updateParameterDraft);
$('submitResearch').addEventListener('click', submitResearch);
$('downloadReport').addEventListener('click', () => download('html'));
$('downloadMarkdown').addEventListener('click', () => download('markdown'));
$('loadRun').addEventListener('click', () => loadRun($('runSelect').value));
$('refreshRun').addEventListener('click', () => loadRun(state.activeRun));
$('runSelect').addEventListener('change', event => loadRun(event.target.value));

boot();
