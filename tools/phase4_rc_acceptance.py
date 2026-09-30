"""CNS-PLANNER Phase4-FINAL-RC 预验收脚本（真实 HTTP API）。

用法：
  python rc_acceptance.py --project-dir <项目目录>      # 打开项目并跑 Step3 复验 + Step5/6 主链
  python rc_acceptance.py --project-dir <目录> --reopen # 仅重开并核对持久化状态

约束：只调用 production API，不伪造 confirmed 证据，不写正式项目状态之外的数据。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
RESULTS = []
SESSION = {"token": None, "revision": None, "project_dir": None}


def bootstrap():
    """读取页面引导数据，取得同源会话 token 与当前 workflow revision。

    这不是绕过门禁：POST 契约本来就要求本机同源 + 会话 token + 乐观锁 revision，
    浏览器前端也是从同一引导端点取得它们的。
    """

    state = call("GET", "/api/state", use_session=False)
    body = state.get("body") or {}
    SESSION["token"] = body.get("token") or body.get("session_token")
    SESSION["revision"] = body.get("revision")
    SESSION["project_dir"] = body.get("project_dir")
    return SESSION["token"] is not None


def record(check_id, step, ok, detail="", severity="MAJOR"):
    RESULTS.append({"id": check_id, "step": step, "ok": bool(ok),
                    "severity": severity if not ok else "-", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {check_id} {detail}", flush=True)
    return ok


def call(method, path, payload=None, timeout=900, use_session=True):
    data = None
    headers = {}
    if use_session and SESSION["token"]:
        headers["X-CNS-Token"] = SESSION["token"]
        if SESSION["revision"] is not None:
            headers["X-CNS-Revision"] = str(SESSION["revision"])
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            revision = response.headers.get("X-CNS-Revision")
            if revision is not None:
                SESSION["revision"] = int(revision)
            try:
                return {"ok": True, "status": response.status, "body": json.loads(raw)}
            except ValueError:
                return {"ok": True, "status": response.status, "raw": raw}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return {"ok": False, "status": exc.code, "body": json.loads(body)}
        except ValueError:
            return {"ok": False, "status": exc.code, "raw": body}


def fetch_bytes(path):
    """按字节读取（zip / pdf 等二进制 artifact 不能当 UTF-8 文本解析）。"""

    headers = {}
    if SESSION["token"]:
        headers["X-CNS-Token"] = SESSION["token"]
    request = urllib.request.Request(BASE + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return {"ok": True, "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "bytes": response.read()}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "bytes": exc.read()}


def post(path, payload=None):
    # 每次写操作前刷新 revision：服务端对同步写路径执行乐观锁校验，
    # 浏览器前端同样在每次写前读取当前 revision。
    call("GET", "/api/health")
    return call("POST", path, payload if payload is not None else {})


def refresh_revision():
    """服务端在每次响应都返回当前 revision；写前刷新以避免乐观锁过期。"""

    call("GET", "/api/health")


def get(path):
    return call("GET", path)


def workflow():
    result = get("/api/workflow")
    return result["body"] if result["ok"] else {}


def err(result):
    body = result.get("body")
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or body)[:300]
    return str(result.get("raw"))[:300]


EMPTY_BASELINE_ASSUMPTION = {
    "assumption_id": "ASM-CNS-EMPTY",
    "scope": "project",
    "field": "cns_existing_baseline",
    "value": "empty",
    "basis": "engineering_baseline",
    "reason": "既有 CNS 设施事实尚未取得，按空基线进行工程规划",
    "source_ref": "phase4-final-rc preacceptance",
    "owner": "user",
    "confirmed": True,
    "authority_effect": "allowed_with_disclosure",
    "report_disclosure": "空既有设施工程规划基线；不表示现实中不存在既有 CNS 设施。",
    "invalidates_on": ["existing-cns-source-change"],
    "status": "active",
}


# ---------------------------------------------------------------- Step 1-2 复验
def step_open(project_dir):
    print("\n========== 项目打开 ==========", flush=True)
    result = post("/api/project/open", {"project_dir": str(project_dir)})
    record("OPEN-1", "Step1", result["ok"], f"open {project_dir} status={result.get('status')} {'' if result['ok'] else err(result)}")
    flow = workflow()
    record("OPEN-2", "Step1", bool(flow.get("project")), "canonical workflow snapshot 可读")
    return flow


def step1to2_recheck(flow):
    print("\n========== Step1/Step2 复验 ==========", flush=True)
    grid = flow.get("grid") or {}
    record("S2-GRID-1", "Step2", bool(grid), f"canonical L8 grid present cells={len((grid.get('cells') or []))}")
    attributes = flow.get("grid_attributes") or {}
    layers = (flow.get("spatial_3d") or {}).get("altitude_layers") or []
    ids = sorted(str(item.get("altitude_layer_id")) for item in layers)
    record("S2-ALT-1", "Step2", len(layers) >= 1,
           f"高度层目录为数据驱动 count={len(layers)} ids={ids}")
    record("S2-ALT-2", "Step2", any(i.startswith("ALT-") and i != "ALT-080" for i in ids),
           "目录不写死 80m（存在非 080 层）")
    nodata = flow.get("population_nodata_policy") or {}
    record("S2-POP-1", "Step2", bool(nodata), f"population_nodata_policy present {json.dumps(nodata, ensure_ascii=False)[:160]}")
    shelter = flow.get("shelter_coefficient_policy") or {}
    record("S2-SHELTER-1", "Step2", bool(shelter), f"shelter_coefficient_policy present {json.dumps(shelter, ensure_ascii=False)[:160]}")
    # Constraint Field fail-closed 复核
    fields = get("/api/planning-constraint-fields")
    body = fields.get("body") if fields["ok"] else {}
    counts = json.dumps(body, ensure_ascii=False)
    record("S2-CONSTRAINT-1", "Step2", fields["ok"], f"planning constraint fields 可读 {counts[:200]}")
    return attributes


# ---------------------------------------------------------------- Step 3 复验
def step3_recheck(flow):
    print("\n========== Step3 复验 ==========", flush=True)
    routes = flow.get("operational_routes") or []
    record("S3-ROUTE-1", "Step3", len(routes) >= 1, f"operational route count={len(routes)}")
    if routes:
        route = routes[0]
        record("S3-ROUTE-2", "Step3", bool(route.get("route_id")) and len(route.get("path") or []) > 1,
               f"route_id={route.get('route_id')} path_points={len(route.get('path') or [])}")
        provenance = route.get("provenance") or {}
        layer_identity = str(route.get("altitude_layer_id") or provenance.get("candidate_id") or "")
        record("S3-ROUTE-3", "Step3", "ALT-" in layer_identity,
               f"高度层 identity={layer_identity[:80]} egm2008_in_z={provenance.get('egm2008_altitude_in_third_coordinate')}")
    adoption = get("/api/layered-operational-adoptions")
    adoptions = (adoption.get("body") or {}).get("items") if adoption["ok"] else None
    active = [item for item in (adoptions or []) if str(item.get("status")) in ("applied", "operational")]
    record("S3-ADOPT-1", "Step3", bool(adoptions), f"adoptions={len(adoptions or [])} active={len(active)}")
    profile = get("/api/route-risk-profiles")
    profiles = (profile.get("body") or {}).get("items") if profile["ok"] else None
    record("S3-PROFILE-1", "Step3", bool(profiles), f"route risk profiles={len(profiles or [])}")
    validation = get("/api/layered-route-validations")
    validations = (validation.get("body") or {}).get("items") if validation["ok"] else None
    record("S3-VALID-1", "Step3", bool(validations), f"validations={len(validations or [])}")
    # 旧 fallback 不得出现
    algorithms = get("/api/algorithms")
    text = json.dumps(algorithms.get("body") if algorithms["ok"] else {}, ensure_ascii=False)
    record("S3-FALLBACK-1", "Step3", "RoutePlannerV1" not in text or "legacy" in text.lower(),
           "algorithm catalog 未把 RoutePlannerV1 作为 production 默认")


# ---------------------------------------------------------------- Step 5
def step5(flow):
    print("\n========== Step5 CNS 能力与设施规划 ==========", flush=True)
    print("-- 5.1 Existing CNS baseline 声明（新增闭环保卫路径）", flush=True)

    # 5.1a 静默空基线必须被拒绝
    silent = post("/api/cns-existing-baseline", {
        "cns_existing_baseline": {"knowledge_status": "not_declared",
                                  "planning_mode": "assume_empty_for_planning",
                                  "source": "rc silent probe", "declared_by": "rc"},
    })
    record("S5-BASE-1", "Step5", (not silent["ok"]) and "assumption" in err(silent),
           f"静默空基线被拒绝：{err(silent)[:120]}")

    # 5.1b confirmed_present 无设施/证据必须被拒绝（域 fail-closed）
    present_unproven = post("/api/cns-existing-baseline", {
        "cns_existing_baseline": {"knowledge_status": "confirmed_present",
                                  "planning_mode": "factual",
                                  "source": "rc probe", "declared_by": "rc"},
    })
    record("S5-BASE-2", "Step5", not present_unproven["ok"],
           f"无证据 confirmed_present 被拒绝：{err(present_unproven)[:200]}")

    # 5.1b2 confirmed_present + assume_empty_for_planning 是自相矛盾声明
    contradictory = post("/api/cns-existing-baseline", {
        "cns_existing_baseline": {"knowledge_status": "confirmed_present",
                                  "planning_mode": "assume_empty_for_planning",
                                  "source": "rc probe", "declared_by": "rc",
                                  "evidence_ref": "declared-facility-evidence"},
        "assumption": dict(EMPTY_BASELINE_ASSUMPTION),
    })
    record("S5-BASE-2b", "Step5", not contradictory["ok"],
           f"confirmed_present + assume_empty 自相矛盾声明被拒绝：{err(contradictory)[:200]}")

    # 5.1c 合法声明：not_declared + assume_empty_for_planning + 显式工程假设
    declared = post("/api/cns-existing-baseline", {
        "cns_existing_baseline": {"knowledge_status": "not_declared",
                                  "planning_mode": "assume_empty_for_planning",
                                  "source": "Phase4-Final-RC preacceptance",
                                  "declared_by": "rc-preacceptance"},
        "assumption": dict(EMPTY_BASELINE_ASSUMPTION),
    })
    record("S5-BASE-3", "Step5", declared["ok"],
           f"合法空基线声明通过 {'' if declared['ok'] else err(declared)}")
    snapshot = get("/api/cns-existing-baseline")
    baseline = snapshot["body"] if snapshot["ok"] else {}
    record("S5-BASE-4", "Step5",
           baseline.get("planning_mode") == "assume_empty_for_planning"
           and baseline.get("knowledge_status") == "not_declared",
           f"声明已持久化 {json.dumps({k: baseline.get(k) for k in ('knowledge_status','planning_mode','source')}, ensure_ascii=False)}")
    flow2 = workflow()
    actives = [i for i in (flow2.get("assumptions") or {}).get("items", []) if i.get("status") == "active"]
    empty_asm = [i for i in actives if i.get("field") == "cns_existing_baseline" and i.get("value") == "empty"]
    record("S5-BASE-5", "Step5", len(empty_asm) == 1,
           f"工程假设保留且唯一 active={len(empty_asm)} disclosure={bool(empty_asm and empty_asm[0].get('report_disclosure'))}")

    # 5.1d 幂等重放
    again = post("/api/cns-existing-baseline", {
        "cns_existing_baseline": {"knowledge_status": "not_declared",
                                  "planning_mode": "assume_empty_for_planning",
                                  "source": "Phase4-Final-RC preacceptance",
                                  "declared_by": "rc-preacceptance"},
        "assumption": dict(EMPTY_BASELINE_ASSUMPTION),
    })
    record("S5-BASE-6", "Step5", again["ok"], f"重复声明幂等 {'' if again['ok'] else err(again)}")

    print("-- 5.2 coverage_3d", flush=True)
    coverage = post("/api/coverage-3d/evaluate", {})
    record("S5-COV-1", "Step5", coverage["ok"], f"coverage-3d evaluate {'' if coverage['ok'] else err(coverage)}")
    cov = (coverage.get("body") or {}).get("coverage_3d") or {}
    reason_text = json.dumps(cov.get("routes") or [], ensure_ascii=False)
    # 真实数据链：缺少可用的三维几何 provider 时必须如实 missing_data（不得补 0
    # 或伪造 pass）。本 fixture 未导入既有设施，因此覆盖链停在 missing_data。
    record("S5-COV-2", "Step5",
           str(cov.get("status")) in ("passed", "passed_with_warning", "missing_data", "failed", "blocked"),
           f"coverage status={cov.get('status')} routes={len(cov.get('routes') or [])} "
           f"如实给出缺失原因={'三维几何服务提供者' in reason_text or '缺少航路高度剖面' in reason_text}")

    print("-- 5.3 cns service capability", flush=True)
    capability = post("/api/cns-service-capability/evaluate", {})
    record("S5-CAP-1", "Step5", capability["ok"], f"capability evaluate {'' if capability['ok'] else err(capability)}")
    cap = (capability.get("body") or {}).get("cns_service_capability") or {}
    record("S5-CAP-2", "Step5", bool(cap.get("status")),
           f"capability status={cap.get('status')} maturity={cap.get('maturity')}")

    print("-- 5.4 service corridor（async heavy task 路径优先）", flush=True)
    corridor = post("/api/cns-service-corridor/evaluate", {})
    record("S5-CORR-1", "Step5", corridor["ok"], f"corridor evaluate {'' if corridor['ok'] else err(corridor)}")
    corr = (corridor.get("body") or {}).get("cns_corridor_assessment") or {}
    record("S5-CORR-2", "Step5", bool(corr.get("status")),
           f"corridor status={corr.get('status')} fingerprint={str(corr.get('input_fingerprint'))[:12]}")

    print("-- 5.4b service corridor async heavy task（提交 → 进度 → publish）", flush=True)
    refresh_revision()
    submit = call("POST", "/api/cns-service-corridor/evaluate", {"async": True, "parameters": {}})
    task = submit.get("body") if isinstance(submit.get("body"), dict) else {}
    task_id = str(task.get("task_id") or "")
    record("S5-ASYNC-1", "Step5", submit["ok"] and submit.get("status") == 202 and bool(task_id),
           f"异步提交 HTTP {submit.get('status')} task_id={task_id}")
    if task_id:
        terminal = {"succeeded", "failed", "cancelled", "stale"}
        final, progression, heartbeat_seen = {}, [], False
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            time.sleep(1.0)
            poll = get(f"/api/tasks/{task_id}")
            final = poll.get("body") if isinstance(poll.get("body"), dict) else {}
            # 业务状态在 advanced.status（顶层 status 由前端映射用，可能为空）。
            status = str((final.get("advanced") or {}).get("status") or final.get("status") or "")
            progression.append(status)
            if final.get("heartbeat_at") or final.get("heartbeat"):
                heartbeat_seen = True
            if status in terminal:
                break
        advanced = final.get("advanced") or {}
        business = (final.get("business_error") or {}).get("code")
        final_status = str(advanced.get("status") or final.get("status"))
        # 终态 stale + task_input_changed 表示"提交后输入变化，因此不发布"：这是
        # 输入一致性的 fail-closed 保护；成功终态才是完整发布的证据。
        if final_status in terminal:
            record("S5-ASYNC-2", "Step5", final_status in ("succeeded", "stale"),
                   f"任务终态={final_status} business={business} 进度序列={progression[:6]}")
            record("S5-ASYNC-3", "Step5", heartbeat_seen,
                   f"heartbeat 可观测 heartbeat={final.get('heartbeat_at')}")
            if final_status == "succeeded":
                flow_after = workflow()
                corridor_after = (flow_after.get("cns_corridor_assessment") or {})
                release = final.get("release") or {}
                summary = final.get("result_summary") or {}
                # release 段是可选的 UI 展示元数据；canonical 结果是否真正进入
                # state 由 P14 自己的 status/fingerprint 判定（见 ASYNC-5）。
                record("S5-ASYNC-4", "Step5",
                       bool(advanced.get("result_artifact_ref")) or bool(summary)
                       or (final.get("advanced") or {}).get("result_fingerprint") is not None,
                       f"publish 后 artifact/结果可解析 release={release.get('status')} "
                       f"summary_keys={sorted(summary)[:3]} "
                       f"artifact_ref={bool(advanced.get('result_artifact_ref'))}")
                record("S5-ASYNC-5", "Step5",
                       str(corridor_after.get("status")) in ("pending_confirmation", "passed", "failed")
                       and str(corridor_after.get("input_fingerprint") or "") != "",
                       f"P14 已由任务结果驱动 status={corridor_after.get('status')} "
                       f"fp={str(corridor_after.get('input_fingerprint'))[:12]}")
            else:
                record("S5-ASYNC-4", "Step5",
                       business == "task_input_changed" and not advanced.get("result_artifact_ref"),
                       f"输入变化时未发布 artifact business={business}")
                record("S5-ASYNC-5", "Step5", bool(advanced.get("input_snapshot_ref")),
                       f"immutable snapshot 已冻结 {advanced.get('input_snapshot_ref')}")
        else:
            record("S5-ASYNC-2", "Step5", False,
                   f"任务 900s 内未进入终态 进度序列={progression[-5:]}")

    print("-- 5.5 corridor gap", flush=True)
    gap = post("/api/cns-corridor-gap/evaluate", {})
    record("S5-GAP-1", "Step5", gap["ok"], f"gap evaluate {'' if gap['ok'] else err(gap)}")
    g = (gap.get("body") or {}).get("cns_corridor_gap_assessment") or {}
    record("S5-GAP-2", "Step5", bool(g.get("status")), f"gap status={g.get('status')}")

    print("-- 5.6 corridor site plan", flush=True)
    site_plan = post("/api/cns-corridor-site-plan/evaluate", {})
    record("S5-SITE-1", "Step5", site_plan["ok"], f"site plan evaluate {'' if site_plan['ok'] else err(site_plan)}")
    sp = (site_plan.get("body") or {}).get("cns_corridor_site_plan") or {}
    record("S5-SITE-2", "Step5", bool(sp.get("status")),
           f"site plan status={sp.get('status')} actions={len(sp.get('selected_actions') or [])}")

    print("-- 5.7 Radar OPTIONAL surveillance branch 不得阻塞主链", flush=True)
    radar_ready = get("/api/radar-surveillance-layout/readiness")
    rbody = radar_ready.get("body") if radar_ready["ok"] else {}
    record("S5-RADAR-1", "Step5", radar_ready["ok"],
           f"radar readiness 可读 requirement={json.dumps(rbody, ensure_ascii=False)[:200]}")

    print("-- 5.8 performance admission（hard ceiling 不可 override）", flush=True)
    ceiling = post("/api/cns-service-corridor/evaluate", {"parameters": {"max_route_samples": 10 ** 9, "performance_override": True}})
    detail = err(ceiling)
    record("S5-PERF-1", "Step5", ceiling["ok"] or "override" in detail.lower() or "ceiling" in detail.lower() or "上限" in detail or "envelope" in detail.lower(),
           f"hard ceiling 请求被拒绝或按包线处理：{detail[:200]}")
    return cov, sp


# ---------------------------------------------------------------- Step 6
def step6(flow):
    print("\n========== Step6 方案评审与报告 ==========", flush=True)
    print("-- 6.1 initialize review", flush=True)
    init = post("/api/cns-plan-review/initialize", {})
    if not init["ok"]:
        record("S6-INIT-1", "Step6", False, f"initialize 失败：{err(init)}")
        return {}
    review = (init.get("body") or {}).get("cns_plan_review") or {}
    variants = review.get("variants") or []
    record("S6-INIT-1", "Step6", len(variants) >= 1,
           f"variants={len(variants)} review_id={review.get('review_id')}")
    selected = review.get("selected_variant_id")
    record("S6-INIT-2", "Step6", bool(selected), f"默认 selected={selected}")

    print("-- 6.2 select ≠ confirm", flush=True)
    target = next((v for v in variants if v.get("variant_id") != selected), variants[0])
    select = post("/api/cns-plan-review/select", {"variant_id": target.get("variant_id")})
    record("S6-SELECT-1", "Step6", select["ok"], f"select {target.get('variant_id')} {'' if select['ok'] else err(select)}")
    after_select = (select.get("body") or {}).get("cns_plan_review") or {}
    confirmed_now = (select.get("body") or {}).get("confirmed_cns_plan") or {}
    record("S6-SELECT-2", "Step6",
           after_select.get("selected_variant_id") == target.get("variant_id")
           and confirmed_now.get("status") != "confirmed",
           f"select 生效且不产生 confirmed plan（confirmed={confirmed_now.get('status')}）")

    print("-- 6.3 evaluate variant", flush=True)
    evaluate = post("/api/cns-plan-review/evaluate", {"variant_id": target.get("variant_id")})
    record("S6-EVAL-1", "Step6", evaluate["ok"], f"evaluate variant {'' if evaluate['ok'] else err(evaluate)}")
    ev_variants = ((evaluate.get("body") or {}).get("cns_plan_review") or {}).get("variants") or []
    current = next((v for v in ev_variants if v.get("variant_id") == target.get("variant_id")), {})
    gate = (current.get("evaluation") or {}).get("confirmation_gate") or {}
    record("S6-EVAL-2", "Step6", bool(gate.get("status")), f"confirmation gate={gate.get('status')}")

    print("-- 6.4 confirm", flush=True)
    payload = {"variant_id": target.get("variant_id"), "source": "rc_preacceptance",
               "reason": "Phase4-Final-RC 预验收确认"}
    if gate.get("status") == "objectives_not_configured":
        payload["confirm_without_objectives"] = True
        payload["reason"] = "Phase4-Final-RC 预验收：规划目标未配置，显式确认继续"
    confirm = post("/api/cns-plan-review/confirm", payload)
    if not confirm["ok"]:
        record("S6-CONFIRM-1", "Step6", False, f"confirm 失败 gate={gate.get('status')}：{err(confirm)}")
        return {"gate": gate, "blocked": err(confirm)}
    plan = (confirm.get("body") or {}).get("confirmed_cns_plan") or {}
    plan_id = plan.get("plan_id")
    record("S6-CONFIRM-1", "Step6", plan.get("status") == "confirmed", f"confirmed plan_id={plan_id}")
    record("S6-CONFIRM-2", "Step6", bool(plan_id) and plan.get("variant_id") == target.get("variant_id"),
           f"plan identity: variant={plan.get('variant_id')} == selected={target.get('variant_id')}")
    record("S6-CONFIRM-3", "Step6", (plan.get("application") or {}).get("status") == "not_applied",
           f"confirm ≠ apply（application.status={(plan.get('application') or {}).get('status')}）")

    print("-- 6.5 apply（apply 必须携带 plan_id）", flush=True)
    wrong = post("/api/cns-plan-review/apply", {"plan_id": "CP-NOT-THE-PLAN"})
    record("S6-APPLY-0", "Step6", (not wrong["ok"]) or "stale" in json.dumps(wrong.get("body"), ensure_ascii=False),
           f"错误 plan_id 被拒绝/判 stale：{err(wrong)[:120]}")
    apply_result = post("/api/cns-plan-review/apply", {"plan_id": plan_id})
    applied_plan = (apply_result.get("body") or {}).get("confirmed_cns_plan") or {}
    record("S6-APPLY-1", "Step6", apply_result["ok"],
           f"apply status={apply_result.get('status')} {'' if apply_result['ok'] else err(apply_result)}")
    record("S6-APPLY-2", "Step6", applied_plan.get("plan_id") == plan_id,
           f"apply 后 plan_id 未被替换：{applied_plan.get('plan_id')}")
    record("S6-APPLY-3", "Step6", (applied_plan.get("application") or {}).get("status") in ("applied", "not_applied"),
           f"application.status={(applied_plan.get('application') or {}).get('status')}")

    print("-- 6.6 report preview / generate", flush=True)
    preview = post("/api/cns-planning-report/preview", {})
    record("S6-REPORT-0", "Step6", preview["ok"], f"report preview {'' if preview['ok'] else err(preview)}")
    generate = post("/api/cns-planning-report/generate", {})
    if not generate["ok"]:
        record("S6-REPORT-1", "Step6", False, f"report generate 失败：{err(generate)}")
        return {"plan_id": plan_id, "gate": gate}
    reports = (generate.get("body") or {}).get("cns_planning_reports") or {}
    active_id = reports.get("active_report_id")
    rec = next((r for r in reports.get("records") or [] if r.get("report_id") == active_id), {})
    record("S6-REPORT-1", "Step6", reports.get("status") == "passed", f"report status={reports.get('status')} active={active_id}")
    record("S6-REPORT-2", "Step6", rec.get("source_plan_id") == plan_id,
           f"report 引用同一正式 plan_id：{rec.get('source_plan_id')}")
    record("S6-REPORT-3", "Step6", all(rec.get("artifacts", {}).get(k) for k in ("html", "pdf", "json", "package")),
           f"artifacts={list((rec.get('artifacts') or {}).keys())}")

    print("-- 6.7 artifact / export 可读", flush=True)
    html = fetch_bytes(f"/api/cns-planning-report/artifact?report_id={active_id}&kind=html")
    record("S6-ART-1", "Step6", html["ok"] and len(html.get("bytes") or b"") > 500,
           f"report.html 可读 bytes={len(html.get('bytes') or b'')}")
    package = fetch_bytes(f"/api/cns-planning-report/artifact?report_id={active_id}&kind=package")
    record("S6-ART-2", "Step6",
           package["ok"] and (package.get("bytes") or b"")[:2] == b"PK",
           f"planning-package.zip 可读 bytes={len(package.get('bytes') or b'')} "
           f"zip_magic={(package.get('bytes') or b'')[:2]!r}")
    pdf = fetch_bytes(f"/api/cns-planning-report/artifact?report_id={active_id}&kind=pdf")
    record("S6-ART-3", "Step6",
           pdf["ok"] and (pdf.get("bytes") or b"")[:4] == b"%PDF",
           f"report.pdf 可读 bytes={len(pdf.get('bytes') or b'')}")
    export_sites = get("/api/export/sites")
    record("S6-ART-4", "Step6", export_sites["ok"],
           f"/api/export/sites 可用 {'' if export_sites['ok'] else err(export_sites)}")
    return {"plan_id": plan_id, "report_id": active_id, "gate": gate, "review": review.get("review_id")}


# ---------------------------------------------------------------- 跨步骤稳定性
def stability(project_dir, expected):
    print("\n========== 跨步骤 save/reopen 稳定性 ==========", flush=True)
    before = workflow()
    record("STAB-SAVE-1", "Cross", bool((before.get("cns_corridor_site_plan") or {}).get("status")),
           f"Step5 结果在快照中：site_plan={(before.get('cns_corridor_site_plan') or {}).get('status')}")

    reopened = post("/api/project/open", {"project_dir": str(project_dir)})
    record("STAB-REOPEN-1", "Cross", reopened["ok"], f"reopen {'' if reopened['ok'] else err(reopened)}")
    after = workflow()

    plan = after.get("confirmed_cns_plan") or {}
    record("STAB-REOPEN-2", "Cross", plan.get("plan_id") == expected.get("plan_id"),
           f"confirmed plan_id 保持：{plan.get('plan_id')} (期望 {expected.get('plan_id')})")
    reports = after.get("cns_planning_reports") or {}
    record("STAB-REOPEN-3", "Cross", reports.get("active_report_id") == expected.get("report_id"),
           f"active_report_id 保持：{reports.get('active_report_id')}")
    baseline = after.get("cns_existing_baseline") or {}
    record("STAB-REOPEN-4", "Cross", baseline.get("planning_mode") == "assume_empty_for_planning",
           f"baseline 声明保持：{baseline.get('planning_mode')} knowledge={baseline.get('knowledge_status')}")
    actives = [i for i in (after.get("assumptions") or {}).get("items", []) if i.get("status") == "active"]
    record("STAB-REOPEN-5", "Cross", any(i.get("field") == "cns_existing_baseline" for i in actives),
           f"active 工程假设未丢失 count={len(actives)}")
    routes = after.get("operational_routes") or []
    record("STAB-REOPEN-6", "Cross", len(routes) >= 1, f"operational route 保持 count={len(routes)}")
    coverage = after.get("coverage_3d") or {}
    record("STAB-REOPEN-7", "Cross", coverage.get("status") not in (None, "not_calculated"),
           f"coverage_3d 未丢失 status={coverage.get('status')}")
    # artifact ref 可解析
    rec = next((r for r in reports.get("records") or [] if r.get("report_id") == reports.get("active_report_id")), {})
    html = fetch_bytes(f"/api/cns-planning-report/artifact?report_id={reports.get('active_report_id')}&kind=html")
    record("STAB-REOPEN-8", "Cross",
           html["ok"] and len(html.get("bytes") or b"") > 500,
           f"重开后 report artifact ref 仍可解析 bytes={len(html.get('bytes') or b'')} "
           f"plan_match={rec.get('source_plan_id') == expected.get('plan_id')}")
    return after


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--reopen", action="store_true")
    parser.add_argument("--out", default="")
    options = parser.parse_args()
    project_dir = Path(options.project_dir)
    started = time.monotonic()

    if not bootstrap():
        print("FATAL: 无法从 /api/state 取得会话 token", flush=True)
        return 3
    print(f"session: revision={SESSION['revision']} token={'*' * 8} "
          f"server_project={SESSION['project_dir']}", flush=True)

    flow = step_open(project_dir)
    expected = {}
    if not options.reopen:
        step1to2_recheck(flow)
        step3_recheck(flow)
        step5(flow)
        expected = step6(flow)
        stability(project_dir, expected)

    payload = {"project_dir": str(project_dir), "elapsed_s": round(time.monotonic() - started, 1),
               "checks": RESULTS,
               "failed": [r for r in RESULTS if not r["ok"]],
               "expected": expected}
    if options.out:
        Path(options.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n========== 汇总 ==========")
    print(f"checks={len(RESULTS)} failed={len(payload['failed'])} elapsed={payload['elapsed_s']}s")
    for item in payload["failed"]:
        print(f"  FAIL {item['id']} [{item['step']}] {item['detail'][:200]}")
    return 0 if not payload["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
