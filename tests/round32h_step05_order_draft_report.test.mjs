/**
 * Round32-H 前端定向测试（Step05 正式顺序 + Radar 必需性权威 + 诊断草稿预览）。
 *
 * 只覆盖本轮改动的契约，不重跑无关回归：
 *  1. RESULT_SEGMENTS / CNS_CANONICAL_CHAIN：Radar 基线位于 CNS 服务走廊之前；
 *  2. 渲染出的二级分段顺序与链条文案同步；
 *  3. RID-only（后端 readiness 说不需要 Radar）绝不阻塞 CNS 服务走廊；
 *  4. 正式需求要求 Radar 但基线未就绪 ⇒ 服务走廊给出明确前置引导；
 *  5. Radar 基线已变化（服务走廊 stale）⇒ 给出"从服务走廊继续"的重算引导；
 *  6. Radar optional ⇒ 不阻塞；
 *  7. 预览报告只消费 html，响应缺 html 时给可读原因；无法解析的 JSON 响应不再
 *     把 "Unexpected end of JSON input" 直接抛给用户。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {CNS_CANONICAL_CHAIN, render as renderStep05}
  from '../cns_planner/web/js/workflow/step05_cns.js';
import {createShellActions} from '../cns_planner/web/js/workflow/shell_actions.js';
import {createApiClient} from '../cns_planner/web/js/api/client.js';

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

const RID_ONLY_REQUIREMENT = {
  project_default: {
    communication: {required: false}, navigation: {required: false},
    surveillance: {
      // 故意把"看起来要求监视"的字段全部置真：它们只描述 S:rid_cooperative，
      // 不能据此推断 Radar 非合作监视必需。
      required: true, coverage_requirement: 0.95,
      performance: {min_detection_range_m: 2000, max_update_interval_s: 2},
      services: {
        'S:rid_cooperative': {required: true, service_key: 'S:rid_cooperative', confirmed: true},
      },
    },
  },
  route_overrides: {},
};

function step05Flow({
  readinessRequired = false,
  readinessStatus = 'passed',
  radarItems = [],
  radarStatus = null,
  corridorStatus = 'passed',
  requiredCns = null,
} = {}) {
  return {
    project: {name: 'Round32-H 测试项目'},
    operational_routes: [{route_id: 'R0005', status: 'passed', path: [[122, 30], [122.1, 30.1]]}],
    spatial_3d: {altitude_layers: []},
    required_cns: requiredCns || {project_default: {}, route_overrides: {}},
    radar_surveillance_layout: {
      status: radarStatus || (radarItems.length ? 'pending_confirmation' : 'not_calculated'),
      items: radarItems,
    },
    radar_surveillance_layout_readiness: {
      status: readinessStatus,
      required_for_current_routes: readinessRequired,
      required_route_ids: readinessRequired ? ['R0005'] : [],
      required_basis: 'domain.radar_service_evidence.radar_required_for',
    },
    cns_corridor_assessment: {status: corridorStatus},
    coverage_3d: {status: 'passed'},
    cns_continuous_service: {
      result: {status: 'unacceptable'},
      step6_gate: {status: 'unacceptable', confirmation_allowed: false},
    },
  };
}

const RADAR_PREREQUISITE = '正式需求包含 Radar 非合作监视服务。请先在「雷达监视基线」完成规划，再评估 CNS 服务走廊。';
const RADAR_REGRESSION = '雷达基线证据已变化。服务走廊及后续结果需要重新评估，请从「CNS 服务走廊」继续。';

// ---------------------------------------------------------------------------
// 1. 正式顺序
// ---------------------------------------------------------------------------

test('the canonical chain puts the radar baseline before the CNS service corridor', () => {
  const ids = CNS_CANONICAL_CHAIN.map(item => item[0]);
  assert.deepEqual(ids, [
    'cns-res-coverage', 'cns-res-capability', 'cns-res-radar',
    'cns-res-corridor', 'cns-res-gap', 'cns-res-site', 'cns-res-continuous',
  ]);
  assert.ok(ids.indexOf('cns-res-radar') < ids.indexOf('cns-res-corridor'),
    'Radar 基线是 P14 的上游服务证据，必须排在服务走廊之前');
  assert.equal(CNS_CANONICAL_CHAIN.find(item => item[0] === 'cns-res-radar')[1], '雷达监视基线');
  // 链条文案随顺序同步（不再把 Radar 描述成"设施规划之后的附加任务"）
  const html = renderStep05({flow: step05Flow()});
  assert.match(html, /链条：三维覆盖 → 服务能力 → 雷达监视基线 → 服务走廊 → 能力缺口 → 设施规划 → 连续服务可接受性/);
});

test('the rendered result segments keep the radar baseline before the corridor', () => {
  const html = renderStep05({flow: step05Flow()});
  const radar = html.indexOf('data-seg-name="cns-res-radar"');
  const corridor = html.indexOf('data-seg-name="cns-res-corridor"');
  assert.ok(radar >= 0 && corridor >= 0, 'both segments must be mounted');
  assert.ok(radar < corridor, 'the radar baseline segment must be rendered before the corridor segment');
  assert.match(html, /data-seg-label="雷达监视基线"/);
  // 服务能力分段的"本标签下还有"同样按新顺序列出
  assert.match(html, /本标签下还有：服务能力评估 · 雷达监视基线 · CNS 服务走廊 · CNS 能力缺口/);
  // 能力分段的下一步指向雷达基线（普通用户点下一步自然进入 Radar 分段）
  assert.match(html, /下一环节：雷达监视基线/);
});

// ---------------------------------------------------------------------------
// 2. Radar 必需性只消费后端权威字段
// ---------------------------------------------------------------------------

test('a RID-only surveillance requirement never blocks the CNS service corridor', () => {
  const flow = step05Flow({requiredCns: RID_ONLY_REQUIREMENT, readinessRequired: false});
  // 夹具确实"看起来"要求监视能力（旧前端判据会据此阻塞）
  assert.equal(flow.required_cns.project_default.surveillance.required, true);
  const html = renderStep05({flow});
  assert.doesNotMatch(html, /请先在「雷达监视基线」完成规划/);
  assert.doesNotMatch(html, /正式需求包含 Radar 非合作监视服务/);
  assert.match(html, /正式需求未要求 Radar 非合作监视：雷达监视基线不阻塞 CNS 服务走廊/);
  assert.match(html, /RID 合作监视（S:rid_cooperative）是\*\*另一条服务\*\*的需求/);
});

test('a required but not-ready radar baseline blocks the corridor with explicit business wording', () => {
  const html = renderStep05({flow: step05Flow({readinessRequired: true, radarItems: []})});
  assert.ok(html.includes(RADAR_PREREQUISITE), 'the corridor segment must state the radar prerequisite');
  assert.match(html, /当前状态：尚未在「雷达监视基线」形成正式需求所要求航路的规划结果（R0005）/);
  // 绝不把 raw enum / 任务指纹当作普通用户可见文字
  assert.doesNotMatch(html, /当前状态：not_calculated/);
  assert.doesNotMatch(html, /当前状态：missing_data/);
  // 雷达分段自身也说明它为什么是前置信据
  assert.match(html, /正式 CNS 需求包含 Radar 非合作监视服务/);
  assert.match(html, /P16 proposal 不等于直接改写 canonical Radar layout/);
});

test('a required route whose radar result went stale blocks the corridor', () => {
  const html = renderStep05({flow: step05Flow({
    readinessRequired: true, radarItems: [{route_id: 'R0005', status: 'stale'}],
  })});
  assert.ok(html.includes(RADAR_PREREQUISITE));
  assert.match(html, /当前状态：雷达基线证据已过时（上游输入已变化）：R0005/);
});

test('a required radar baseline that cannot be evaluated yet blocks the corridor', () => {
  const html = renderStep05({flow: step05Flow({
    readinessRequired: true, readinessStatus: 'not_ready',
    radarItems: [{route_id: 'R0005', status: 'proposal_ready'}],
  })});
  assert.ok(html.includes(RADAR_PREREQUISITE));
  assert.match(html, /雷达监视基线尚不可评估：缺少前置数据/);
});

test('an unrelated stale radar item never blocks the corridor for the required route', () => {
  // 真实项目形态：R0003 已废弃且其 Radar 条目 stale，唯一必需航路 R0005 是 current；
  // 全项目汇总状态因此是 pending_confirmation —— 逐条判定不得据此阻塞 P14。
  const html = renderStep05({flow: step05Flow({
    readinessRequired: true, radarStatus: 'pending_confirmation',
    radarItems: [
      {route_id: 'R0003', status: 'stale'},
      {route_id: 'R0005', status: 'proposal_ready'},
    ],
  })});
  assert.doesNotMatch(html, /请先在「雷达监视基线」完成规划/);
  assert.doesNotMatch(html, /雷达基线证据已变化/);
  assert.match(html, /基线已就绪，可以评估 CNS 服务走廊/);
});

test('a changed radar baseline tells the user to re-evaluate from the corridor', () => {
  const html = renderStep05({flow: step05Flow({
    readinessRequired: true, readinessStatus: 'passed', corridorStatus: 'stale',
    radarItems: [{route_id: 'R0005', status: 'proposal_ready'}],
  })});
  assert.ok(html.includes(RADAR_REGRESSION), 'the radar segment must point at the corridor after a change');
  assert.ok(html.split(RADAR_REGRESSION).length - 1 >= 2,
    'the corridor segment (blockers + inline note) must repeat the same guidance, not silently look current');
  assert.match(html, /系统不会自动重算服务走廊、能力缺口或设施规划，也不会自动触发设施规划的长任务/);
});

test('an optional radar baseline never blocks the corridor even when it is stale', () => {
  const html = renderStep05({flow: step05Flow({
    readinessRequired: false, readinessStatus: 'not_ready', corridorStatus: 'passed',
    radarItems: [{route_id: 'R0005', status: 'stale'}], radarStatus: 'stale',
  })});
  assert.doesNotMatch(html, /请先在「雷达监视基线」完成规划/);
  assert.doesNotMatch(html, /雷达基线证据已变化/);
  assert.match(html, /本分支可选：无论是否运行雷达监视基线，都不影响进入下一步/);
});

test('a current radar baseline never manufactures a blockage', () => {
  const html = renderStep05({flow: step05Flow({
    readinessRequired: true, readinessStatus: 'passed', corridorStatus: 'passed',
    radarItems: [{route_id: 'R0005', status: 'proposal_ready'}],
  })});
  assert.doesNotMatch(html, /请先在「雷达监视基线」完成规划/);
  assert.doesNotMatch(html, /雷达基线证据已变化/);
  assert.match(html, /基线已就绪，可以评估 CNS 服务走廊/);
});

// ---------------------------------------------------------------------------
// 3. 诊断草稿预览
// ---------------------------------------------------------------------------

async function withStubWindow(run) {
  const originalWindow = globalThis.window;
  const originalTimeout = globalThis.setTimeout;
  const opened = [];
  globalThis.window = {
    open() {
      const handle = {location: {href: ''}, closed: false, close() { this.closed = true; }};
      opened.push(handle);
      return handle;
    },
  };
  // 预览成功后 shell_actions 会挂一个 60 s 的 revokeObjectURL 定时器；测试里把长延时
  // 压到 0 ms，避免测试进程空转一分钟（不改变被测逻辑）。
  globalThis.setTimeout = (fn, delay, ...rest) => originalTimeout(fn, delay >= 60000 ? 0 : delay, ...rest);
  try {
    // 必须 await：否则 finally 会在被测异步动作真正执行前就把 stub 拆掉。
    return await run(opened);
  } finally {
    globalThis.window = originalWindow;
    globalThis.setTimeout = originalTimeout;
  }
}

test('the draft preview consumes html and reports a readable reason when it is missing', async () => {
  const messages = [];
  const actions = createShellActions({getNode: () => null, panelError: (message, tone) => messages.push([message, tone])});

  await withStubWindow(async opened => {
    await actions.previewReport(async () => ({html: '<!doctype html><p>诊断草稿</p>'}));
    assert.equal(opened.length, 1);
    assert.match(opened[0].location.href, /^blob:/);
    assert.match(messages.at(-1)[0], /诊断草稿已在新窗口打开/);
    assert.match(messages.at(-1)[0], /不是已确认规划方案，也不是正式报告/);
    assert.equal(messages.at(-1)[1], 'hint');
  });

  await withStubWindow(async () => {
    await assert.rejects(
      () => actions.previewReport(async () => ({status: 'draft', persisted: false})),
      error => {
        assert.match(error.message, /报告预览失败/);
        assert.match(error.message, /服务端没有返回可显示的诊断草稿内容/);
        assert.doesNotMatch(error.message, /Unexpected end of JSON/);
        return true;
      },
    );
  });
});

test('an unparseable JSON response becomes a readable message instead of a raw syntax error', async () => {
  const originalFetch = globalThis.fetch;
  const api = createApiClient(() => 'token', () => 1);
  try {
    globalThis.fetch = async () => new Response('', {
      status: 200, headers: {'content-type': 'application/json; charset=utf-8'},
    });
    await assert.rejects(
      () => api('/api/cns-planning-report/preview', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      }),
      error => {
        assert.match(error.message, /服务端响应无法解析/);
        assert.doesNotMatch(error.message, /Unexpected end of JSON input/);
        return true;
      },
    );

    // 正常 JSON 响应不受影响
    globalThis.fetch = async () => new Response(JSON.stringify({ok: true}), {
      status: 200, headers: {'content-type': 'application/json; charset=utf-8'},
    });
    const data = await api('/api/cns-planning-report/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    assert.deepEqual(data, {ok: true});
  } finally {
    globalThis.fetch = originalFetch;
  }
});
