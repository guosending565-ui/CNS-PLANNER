/** Read-only C3-B consumer shared by the 2D view, stage panels and report. */
export const STAGE_LABELS = {
  P14: '服务走廊业务结论',
  P15: '能力缺口业务结论',
  P16: '设施规划提案（仅 proposal）',
  P17: '连续中断局部门禁',
  P18: '方案评审投影',
};

export function researchIdentity(task, manifest) {
  const taskSummary = task?.result_summary || {};
  const taskInput = taskSummary.input_version || {};
  const manifestInput = manifest?.input_version || {};
  const checks = [
    ['run_id', taskSummary.run_id || task?.run_id, manifest?.run_id],
    ['input_version', taskInput.id, manifestInput.id],
    ['input_fingerprint', taskInput.input_fingerprint, manifestInput.input_fingerprint],
    ['contract_version', taskSummary.contract_version, manifest?.contract_version],
    ['result_list_sha256', taskSummary.authoritative_result_list_sha256,
      manifest?.authoritative_result_list_sha256],
  ];
  for (const [key, left, right] of checks) {
    if (left && right && left !== right) {
      throw new Error(`research identity mismatch: ${key}`);
    }
  }
  const identity = {
    runId: manifest?.run_id || taskSummary.run_id || task?.run_id || null,
    inputVersionId: manifestInput.id || taskInput.id || null,
    inputFingerprint: manifestInput.input_fingerprint || taskInput.input_fingerprint || null,
    parameterFingerprint: manifestInput.parameter_fingerprint || null,
    contractVersion: manifest?.contract_version || taskSummary.contract_version || null,
    resultListSha256: manifest?.authoritative_result_list_sha256
      || taskSummary.authoritative_result_list_sha256 || null,
    taskId: task?.task_id || null,
  };
  identity.consistent = Boolean(
    identity.runId && identity.inputVersionId && identity.contractVersion
    && identity.resultListSha256);
  return identity;
}

export function researchStatusTone(status) {
  const value = String(status || 'unknown').toLowerCase();
  if (['satisfied', 'pass', 'passed', 'succeeded', 'review_initialized'].includes(value)) {
    return 'ok';
  }
  if (['failed', 'business_failed', 'blocked', 'blocked_by_p17_gate'].includes(value)) {
    return 'bad';
  }
  if (['proposal_ready', 'proposal_only', 'review_projected'].includes(value)) return 'proposal';
  if (['unknown', 'not_calculated', 'not_evaluated', 'pending'].includes(value)) return 'unknown';
  return 'neutral';
}

export function researchRunView(task, manifest) {
  const identity = researchIdentity(task, manifest);
  const business = manifest?.business_state || {};
  const phase = (name, label) => ({
    status: business[name]?.status || 'unknown',
    label,
    tone: researchStatusTone(business[name]?.status),
  });
  const gateStatus = manifest?.gates?.p17_step6_gate?.status || 'unknown';
  const p18Status = manifest?.gates?.p18_initialize?.status || 'unknown';
  return {
    runId: identity.runId,
    identity,
    taskCompleted: task?.advanced?.status === 'succeeded' && task?.result_ready === true,
    taskStatus: task?.advanced?.status || 'unknown',
    resultReady: task?.result_ready === true,
    P14: phase('P14', STAGE_LABELS.P14),
    P15: phase('P15', STAGE_LABELS.P15),
    P16: phase('P16', STAGE_LABELS.P16),
    P17: {status: gateStatus, label: STAGE_LABELS.P17, tone: researchStatusTone(gateStatus)},
    P18: {status: p18Status, label: STAGE_LABELS.P18, tone: researchStatusTone(p18Status)},
    Radar: {
      status: business.surveillance_Radar?.formal_status
        || business.surveillance_Radar?.verdict || 'not_calculated',
      label: 'Radar 证据',
      tone: 'unknown',
      verdict: business.surveillance_Radar?.verdict || 'unknown',
    },
    S: {
      status: business.surveillance_S?.verdict || 'unknown',
      label: '监视满足性',
      tone: researchStatusTone(business.surveillance_S?.verdict || 'unknown'),
    },
    C: {
      status: business.communication_C?.verdict || 'unknown',
      label: '通信覆盖',
      tone: researchStatusTone(business.communication_C?.verdict || 'unknown'),
    },
    N: {
      status: business.navigation_N?.verdict || 'unknown',
      label: '导航 N',
      tone: researchStatusTone(business.navigation_N?.verdict || 'unknown'),
    },
    // This research contract never grants formal confirmation, including when
    // an older P17 local gate still exposes confirmation_allowed=true.
    confirmationAllowed: false,
    confirmationLabel: '研究结果，不允许正式确认',
    p18: manifest?.gates?.p18_initialize || null,
    stages: manifest?.stages || {},
    limitations: manifest?.research_limitations || [],
    provenance: manifest?.provenance || null,
    parameters: manifest?.research_parameters || null,
    map: manifest?.map_projection || null,
    reportPayloadSha256: manifest?.report_payload_sha256 || null,
  };
}

export async function readResearchRun({baseUrl = '', token, runId, taskId, fetchImpl = fetch}) {
  const read = async path => {
    const response = await fetchImpl(baseUrl + path, {headers: {'X-CNS-Token': token}});
    if (!response.ok) throw new Error(`research read failed: ${response.status}`);
    return response.json();
  };
  const task = taskId ? await read(`/api/tasks/${encodeURIComponent(taskId)}`) : null;
  const manifest = await read(`/api/research/runs/${encodeURIComponent(runId)}/manifest`);
  return {task, manifest, view: researchRunView(task, manifest)};
}
