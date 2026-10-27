"""R0005 fresh research compute (P14 -> P18) inside the real worker process.

Research-only: results carry ``authority=research_isolated`` and are written to
an isolated research root (results / artifacts / task store / state).  No formal
``ProjectState`` writer, formal project directory or Git is touched.

The pipeline calls the **product** services and algorithms exactly as main
defines them; it adds no algorithm, formula, threshold or business rule.  P17
and P18 are the existing entries; P18 is never confirmed or applied.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
import traceback
from collections import Counter
from copy import deepcopy
from pathlib import Path

from .project_state import NAVIGATION_INTEGRITY_PLANNING_KEY
from .research_input_service import prepare_research_input
from .research_map_service import build_map_projection
from .research_parameters import apply_values, describe, normalize_schema
from .research_run_service import file_sha, identifier, read_json

STAGES = ("P14", "P15", "P16", "P17", "P18")
STATE_KEYS = {
    "P14": "cns_corridor_assessment",
    "P15": "cns_corridor_gap_assessment",
    "P16": "cns_corridor_site_plan",
    "P17": "continuous_service_acceptability",
    "P18": "cns_plan_review",
}
RESULT_KINDS = {
    "P14": "P14_real_result", "P15": "P15_real_result", "P16": "P16_real_result",
    "P17": "P17_real_result", "P18": "P18_review_projection",
}
#: Computed containers that must be dropped when starting from an R0005 *input*.
COMPUTED_KEYS = (
    "cns_corridor_assessment", "cns_corridor_gap_assessment", "cns_corridor_site_plan",
    "continuous_service_acceptability", "cns_plan_review", "research_result_refs",
)
MEMORY_LIMIT_BYTES = 4 * 1024 ** 3
TIME_LIMIT_SECONDS = 900.0


class NoInvalidation:
    """Research chain must never call the product invalidation service."""

    def __getattr__(self, name):
        raise PermissionError("product invalidation forbidden: " + name)


class NotWiredModel:
    """P18 executes gates and review projection only; other models fail closed."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attribute):
        raise RuntimeError("P18 model %s not wired in the research assembly" % self.name)


class ReadOnlySession:
    """Minimal session surface for P14-P16; rejects writes."""

    def __init__(self, state):
        self.state = state

    def save(self):
        raise PermissionError("research P14-P16 session is read-only")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )


def _rss_bytes():
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:  # noqa: BLE001 - Windows / unavailable
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if get_info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters),
                    counters.cb):
            return int(counters.PeakWorkingSetSize)
    except Exception:  # noqa: BLE001
        return 0
    return 0


def input_only_state(state):
    """Strip every previously computed result: the run starts from R0005 input."""

    clean = deepcopy(state)
    for key in COMPUTED_KEYS:
        clean.pop(key, None)
    statuses = clean.get("result_statuses")
    if isinstance(statuses, dict):
        clean["result_statuses"] = {
            key: value for key, value in statuses.items()
            if key == "surface_class_facts"
        }
    else:
        clean["result_statuses"] = {}
    clean.setdefault("research_authority", {})
    clean["research_authority"].setdefault("authority", "research_isolated")
    clean["revision"] = int(clean.get("revision") or 0)
    return clean


# ------------------------------------------------------------------ manifest views
def _subsystem_counts(result):
    counts = Counter()
    for route in result.get("routes") or []:
        for item in route.get("subsystems") or []:
            code = item.get("subsystem") or item.get("code")
            counts[(str(code), str(item.get("planning_status") or item.get("status")))] += 1
    return {"%s:%s" % key: value for key, value in sorted(counts.items())}


def _subsystem_voxels(result, code):
    for route in result.get("routes") or []:
        for item in route.get("subsystems") or []:
            if str(item.get("subsystem")) == code:
                return dict((item.get("service") or {}).get("voxel_counts") or {})
    return {}


def _provider_facts(corridor):
    counts, sites = Counter(), {}
    for route in corridor.get("routes") or []:
        for voxel in route.get("voxels") or []:
            for item in voxel.get("subsystems") or []:
                for evaluation in item.get("provider_evaluations") or []:
                    key = str(evaluation.get("service_key"))
                    counts[(key, str(evaluation.get("status")))] += 1
                    site_id = evaluation.get("site_id") or evaluation.get("facility_id")
                    if site_id:
                        sites.setdefault(key, set()).add(str(site_id))
    return counts, {key: sorted(value) for key, value in sites.items()}


def _radius_facts(state):
    by_service = {}
    for item in (state.get("device_catalog") or {}).get("items") or []:
        geometry = item.get("coverage_geometry") or {}
        radius = geometry.get("radius_by_surface")
        if not isinstance(radius, dict):
            continue
        resolved = radius.get("resolved") if isinstance(radius.get("resolved"), dict) else radius
        key = str(item.get("service_key") or item.get("subsystem") or item.get("device_id"))
        by_service[key] = {
            str(surface): round(float(value), 3)
            for surface, value in resolved.items() if isinstance(value, (int, float))
        }
    communication = next(
        (value for key, value in by_service.items() if key.startswith("C")), None)
    return {
        "by_service": by_service,
        "communication": communication,
        "communication_is_2km_by_surface": (
            communication is not None and all(
                abs(float(value) - 2000.0) < 1e-9 for value in communication.values())),
        "semantics": "per_service_effective_radius_by_surface_from_device_catalog",
    }


def _nav_endpoint_gaps(gap):
    rows = []
    collection = (gap.get("endpoint_service_gaps") or {}) if isinstance(gap, dict) else {}
    for route in collection.get("routes") or []:
        for item in route.get("confirmed_endpoint_gaps") or []:
            rows.append({
                "route_id": route.get("route_id"),
                "service_key": collection.get("service_key") or route.get("service_key"),
                "service_scope": collection.get("service_scope") or route.get("service_scope"),
                "endpoint_role": item.get("endpoint_role"),
                "takeoff_landing_site_id": item.get("takeoff_landing_site_id"),
                "status": item.get("status"),
                "reasons": [str(value)[:200] for value in item.get("reasons") or []],
            })
        for item in route.get("endpoint_states") or []:
            rows.append({
                "route_id": route.get("route_id"),
                "service_key": collection.get("service_key") or route.get("service_key"),
                "endpoint_role": item.get("endpoint_role"),
                "takeoff_landing_site_id": item.get("takeoff_landing_site_id"),
                "planning_status": item.get("planning_status"),
                "integrity_measurement_status": item.get("integrity_measurement_status"),
                "kind": "endpoint_state",
            })
        break
    return {
        "service_key": collection.get("service_key"),
        "service_scope": collection.get("service_scope"),
        "aggregate_status": collection.get("status"),
        "items": rows,
    }


def _compact_limitations(items):
    return [{
        "limitation_id": item.get("limitation_id"),
        "route_id": item.get("route_id"),
        "subsystem": item.get("subsystem"),
        "status": item.get("status") or item.get("severity"),
        "statement": item.get("statement") or item.get("disclosure") or item.get("reason"),
    } for item in items or []]


def _threat_layers(p17_result):
    layers = {}
    for route in p17_result.get("routes") or []:
        for name, entry in (route.get("threat_layers") or {}).items():
            layers[name] = {
                "route_id": route.get("route_id"), "label": entry.get("label"),
                "status": entry.get("status"), "conclusion": entry.get("status"),
                "service_keys": list(entry.get("service_keys") or []),
                "subsystems": list(entry.get("subsystems") or []),
                "t_margin_s": entry.get("t_margin_s"),
                "first_detection_distance_m": entry.get("first_detection_distance_m"),
                "limitations": list(entry.get("limitations") or []),
                "reasons": list(entry.get("reasons") or []),
            }
        break
    return {
        "primary_threat_layer": p17_result.get("primary_threat_layer"),
        "primary_threat_status": p17_result.get("primary_threat_status"),
        "supplementary_threat_layer": p17_result.get("supplementary_threat_layer"),
        "supplementary_threat_status": p17_result.get("supplementary_threat_status"),
        "semantics": ("cooperative_rid_primary_and_noncooperative_radar_supplementary_"
                      "evaluated_separately_never_merged"),
        "layers": layers,
    }


def _admission(state, p17_result, p15_result, gates, p18_record):
    p17_gate = gates.get("p17_step6_gate") or {}
    p18_status = (gates.get("p18_initialize") or {}).get("status")
    p18_review_initialized = p18_status == "review_initialized"
    radar_missing = not bool(state.get("radar_surveillance_layout"))
    s_unknown = _subsystem_voxels(p15_result, "S").get("unknown", 0)
    return {
        "p17_executed": True,
        "p17_status": p17_result.get("status"),
        "p17_is_continuous_outage_verdict_only": True,
        "p17_admissible_as_surveillance_acceptance": False,
        "p17_admissibility_reason": (
            "P17 判定的是「是否还存在来不及介入的连续服务中断」，不是 S 覆盖/满足性结论；"
            "本研究输入下 P15 的 S 为 unknown（%d 体元），P17 仍给出 %s，"
            "其非合作 Radar 层为 %s 且 reason_codes 含 no_detection_evidence。"
            "按「unknown 不等于 pass、禁止默认 PASS / 排除 Radar」，该结论不得读作 S/Radar 通过。"
            % (s_unknown, p17_result.get("status"),
               p17_result.get("supplementary_threat_status"))),
        "p17_step6_gate_allows": p17_gate.get("confirmation_allowed"),
        "p18_status": p18_status,
        "p18_review_initialized": p18_review_initialized,
        "p18_confirmation_allowed": bool(p18_record.get("confirmation_allowed")),
        "p18_confirm_gate": {
            item.get("variant_id"): item.get("confirmation_gate")
            for item in (p18_record.get("variants") or [])
        },
        "confirmed_cns_plan_written": False,
        "existing_cns_facilities_written": False,
        "formal_release_allowed": False,
        "formal_release_reasons": [
            "P14/P15 status=failed（S 的 radar layout 缺失，business_failed，非 identity）",
            "S 体元 unknown（RID satisfied，Radar unknown）",
            "P16 仅为 proposal_ready，未 Adopt/Confirm/Apply",
            "P18 变体 Confirm 门禁不是 ready_for_confirmation（存在 unknown 体元）",
            "actual_delivery/完好性/最优性均未证明",
        ],
        "unique_blocker": {
            "id": "radar_surveillance_layout_missing",
            "statement": (
                "研究输入没有 radar_surveillance_layout（%s），S:radar_noncooperative 无法评价 ⇒ "
                "S unknown ⇒ P14/P15 business_failed ⇒ P18 Confirm 门禁 evidence_required。"
                % ("layout 为空" if radar_missing else "layout 存在但无可用 item")),
            "blocks": ["P14", "P15", "S 判定", "P18 Confirm/Apply", "正式发布"],
            "does_not_block": ["P17 连续服务计算", "P18 评审投影", "run manifest 消费"],
        },
        "c3_admission": ("PASS_WITH_BLOCKER" if p18_review_initialized
                         else "BLOCKED_AT_P17_GATE"),
    }


def _research_limitations(state, p17_result, p15_result, p16_result, p18_record):
    rows = [
        {"limitation_id": "radar_surveillance_layout_missing",
         "statement": "Radar 正式状态 not_calculated；缺 radar layout，S:radar 全部 unknown。",
         "affects": ["S", "P14", "P15", "P18"]},
        {"limitation_id": "p14_p15_business_failed",
         "statement": "P14/P15 status=failed 属业务失败（证据未知），不是 identity/stale 失败。",
         "affects": ["P14", "P15"]},
        {"limitation_id": "p17_verdict_scope",
         "statement": "P17 结论只覆盖连续服务中断事件，不覆盖覆盖满足性、共因/共享电源/塔失效等未评估项。",
         "affects": ["P17"]},
        {"limitation_id": "p18_not_confirmed_or_applied",
         "statement": "P18 只初始化评审与变体投影；未 Confirm、未 Apply。",
         "affects": ["P18"]},
        {"limitation_id": "actual_delivery_unknown",
         "statement": "actual_delivery=unknown、installed=false、canonical_published=false。",
         "affects": ["P14", "P16", "P17"]},
        {"limitation_id": "cost_unknown",
         "statement": "P16 成本 unknown（explicit_costs_by_unit 为空、cross_unit_total=null）。",
         "affects": ["P16"]},
        {"limitation_id": "navigation_endpoint_gaps_open",
         "statement": "端点 N 完好性监视仍为 confirmed_deficit，仅有 P16 提案。",
         "affects": ["N", "P16"]},
        {"limitation_id": "research_authority_not_formal",
         "statement": "全部结果 authority=research_isolated；未写正式 ProjectState / Git / 原始数据。",
         "affects": ["all"]},
    ]
    if (p16_result.get("status") or "") == "proposal_ready":
        rows.append({"limitation_id": "p16_proposal_only",
                     "statement": "P16 为 proposal_ready，动作集不具权威性。",
                     "affects": ["P16", "P18"]})
    if p18_record.get("result_status") == "blocked_by_p17_gate":
        rows.append({"limitation_id": "p18_blocked_by_p17_gate",
                     "statement": "P18 被 P17 门禁拒绝，未初始化评审。", "affects": ["P18"]})
    return rows


def _stage_record(name, results, *, result_status, extra=None):
    entry = (results._index().get("entries") or {}).get(name)
    if entry is None:
        raise KeyError("缺少结果: %s" % name)
    record = {
        "result_name": name, "result_sha256": entry["sha256"],
        "payload_sha256": entry["payload_sha256"], "result_status": result_status,
        "input_fingerprint": entry.get("input_fingerprint"),
        "authority": entry.get("authority"), "path": entry.get("path"),
        "bytes": entry.get("bytes"), "inherited_from_previous_run": False,
        "recomputed_in_this_run": True, "source_run": None, "source_result_sha256": None,
    }
    if extra:
        record.update(extra)
    return record


# ------------------------------------------------------------------ services
def _build_p17(session, state):
    from ..algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1
    from ..algorithms.corridor.v1 import CNSServiceCorridorV1
    from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
    from .continuous_service_service import ContinuousServiceService
    from .plan_projection import PlanProjectionBuilder

    selection = state.get("algorithm_selection") or {}
    corridor_model = CNSServiceCorridorV1(
        (selection.get("corridor_model") or {}).get("parameters") or {})
    gap_analyzer = CNSCorridorGapAnalyzerV1(
        (selection.get("corridor_gap_analyzer") or {}).get("parameters") or {})
    projection = PlanProjectionBuilder(session, corridor_model, gap_analyzer)
    service = ContinuousServiceService(
        session, ContinuousServiceAcceptabilityV1(), NoInvalidation(),
        lambda: session.state, plan_projection=projection,
    )
    return service, corridor_model, gap_analyzer


def _build_p18(session, corridor_model, gap_analyzer, continuous_service):
    from .plan_projection import PlanProjectionBuilder
    from .plan_review_service import PlanReviewService

    projection = PlanProjectionBuilder(session, corridor_model, gap_analyzer)
    coordinator = PlanReviewService(
        session, NotWiredModel("coverage_model_3d"), NotWiredModel("cns_service_model"),
        NotWiredModel("timeline_model"), NotWiredModel("gap_analyzer_v2"),
        corridor_model, gap_analyzer, lambda: session.state, NoInvalidation(),
        continuous_service=continuous_service, plan_projection=projection,
    )
    return coordinator


def _p18_variants(review):
    variants = []
    for variant in review.get("variants") or []:
        evaluation = variant.get("evaluation") or {}
        gate = (evaluation.get("continuous_service_projection") or {}).get(
            "variant_specific_gate") or {}
        confirmation = evaluation.get("confirmation_gate") or {}
        variants.append({
            "variant_id": variant.get("variant_id"), "source": variant.get("source"),
            "selected_action_ids": list(variant.get("selected_action_ids") or []),
            "confirmation_gate": confirmation.get("status"),
            "confirmation_allowed": confirmation.get("confirmation_allowed"),
            "unknown_voxel_count": len(confirmation.get("unknown_voxel_ids") or []),
            "p17_variant_gate_status": gate.get("status"),
            "p17_variant_gate_allows": gate.get("confirmation_allowed"),
            "p17_evaluated_for_this_variant": (
                (evaluation.get("continuous_service_projection") or {}).get(
                    "evaluated_for_this_variant")),
            "not_applied_action_ids": list(evaluation.get("not_applied_action_ids") or []),
        })
    return variants


# ------------------------------------------------------------------ runner
def run_fresh_compute(context):
    """Worker entry: prepare the isolated input, then run P14 -> P18 for real."""

    started = time.monotonic()
    inputs = context.inputs["snapshot"]["inputs"]
    descriptor = inputs["fresh_input"]
    run_id = identifier(inputs["run_id"])
    root = Path(context.workdir).resolve().parent
    source_root = Path(descriptor["root"]).resolve()
    if (root == source_root or root in source_root.parents
            or source_root in root.parents):
        raise ValueError("fresh run root must be separate from the source input")
    if descriptor.get("authority") != "research_isolated":
        raise ValueError("research authority required")
    source_state_path = source_root / "runtime" / "project_state.json"
    source_sha = file_sha(source_state_path)
    if descriptor.get("source_state_sha256") and descriptor["source_state_sha256"] != source_sha:
        raise ValueError("source_state_sha256_mismatch")

    (root / "src").mkdir(parents=True, exist_ok=True)
    shutil_source = source_root / "src" / "research_session.py"
    target_module = root / "src" / "research_session.py"
    if not target_module.is_file():
        if not shutil_source.is_file():
            raise ValueError("research_session_scaffold_missing")
        target_module.write_bytes(shutil_source.read_bytes())
    sys.path.insert(0, str(root / "src"))
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    importlib.invalidate_caches()
    from research_session import (  # noqa: E402
        ArtifactStore, ResearchSession, ResultStore, TaskStore,
    )

    context.progress(0.02, "载入 R0005 研究输入并生成 2 km 设备输入")
    state = json.loads(source_state_path.read_text(encoding="utf-8"))
    prepared, input_report = prepare_research_input(
        input_only_state(state), source_identity={
            "root": str(source_root), "state_sha256": source_sha,
            "source_run_id": descriptor.get("source_run_id"),
        })
    if prepared is None:
        raise ValueError("research input preparation failed: %s"
                         % (input_report.get("reason") or input_report))
    parameter_values = inputs.get("research_parameters")
    parameter_schema = normalize_schema(descriptor.get("parameter_schema"))
    parameter_record = None
    if parameter_values:
        applied = apply_values(prepared, parameter_values.get("values") or {})
        parameter_record = {
            **describe(parameter_values.get("values") or {}, parameter_schema,
                       source_default=(parameter_schema or {}).get("default")),
            "applied": applied,
        }
    session = ResearchSession(root)
    session.state = prepared
    saved = session.save()
    input_fingerprint = session.state_fingerprint()
    results, artifacts, tasks = ResultStore(root), ArtifactStore(root), TaskStore(root)
    checkpoints = root / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    _write_json(root / "evidence" / "research_input_preparation.json", input_report)

    from ..algorithms.corridor.v1 import CNSServiceCorridorV1
    from ..algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
    from ..site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2
    from .corridor_gap_service import CNSCorridorGapService
    from .corridor_service import CNSCorridorService
    from .corridor_site_planning_service import CorridorSitePlanningService

    selection = prepared.get("algorithm_selection") or {}
    read_only = ReadOnlySession(prepared)
    p14 = CNSCorridorService(
        read_only, CNSServiceCorridorV1(
            (selection.get("corridor_model") or {}).get("parameters") or {}),
        NoInvalidation(), lambda: None)
    p15 = CNSCorridorGapService(
        read_only, CNSCorridorGapAnalyzerV1(
            (selection.get("corridor_gap_analyzer") or {}).get("parameters") or {}),
        NoInvalidation(), lambda: None)
    p16 = CorridorSitePlanningService(
        read_only, CorridorReuseFirstSitePlannerV2(
            (selection.get("site_planner") or {}).get("parameters") or {}),
        p14.model, p15.analyzer, NoInvalidation(), lambda: None)

    records, payloads, timings = {}, {}, {}
    peak = _rss_bytes()

    def commit(stage, payload, result_status, extra=None):
        nonlocal peak
        metadata = results.put(stage, payload, input_fingerprint=input_fingerprint)
        artifact = artifacts.publish(payload, kind=RESULT_KINDS[stage],
                                     input_fingerprint=input_fingerprint)
        # The research task store is shared by every run in this root.  A stage
        # id must therefore be run-scoped: otherwise a second run collides with
        # P14 and the historical result identity is silently overwritten.
        stage_task_id = "%s-%s" % (run_id, stage)
        tasks.create(stage_task_id, input_fingerprint)
        tasks.claim(stage_task_id, input_fingerprint)
        tasks.complete(stage_task_id, metadata["sha256"])
        prepared[STATE_KEYS[stage]] = payload["result"]
        prepared.setdefault("research_result_refs", {})[stage] = {
            "result_name": stage, "result_sha256": metadata["sha256"],
            "payload_sha256": metadata["payload_sha256"],
            "artifact_id": artifact["artifact_id"], "task_id": stage,
            "authority": "research_isolated",
        }
        saved = session.save()
        peak = max(peak, _rss_bytes())
        record = _stage_record(stage, results, result_status=result_status, extra={
            **(extra or {}), "artifact_id": artifact["artifact_id"],
            "elapsed_s": round(timings.get(stage, 0.0), 3),
            "state_file_sha256_after": saved["file_sha256"],
        })
        records[stage] = record
        payloads[stage] = payload
        _write_json(checkpoints / (stage + ".json"), {
            "stage": stage, "status": "PASS", "result_status": result_status,
            "result_sha256": record["result_sha256"],
            "payload_sha256": record["payload_sha256"],
            "artifact_id": artifact["artifact_id"],
            "input_fingerprint": result_status and record["input_fingerprint"],
            "elapsed_s": record["elapsed_s"], "finished_at": now(),
            "state_file_sha256_after": saved["file_sha256"],
        })
        context.progress({"P14": 0.25, "P15": 0.4, "P16": 0.65, "P17": 0.85,
                          "P18": 0.95}[stage], "%s 完成；未确认/未发布" % stage)

    # ---- P14
    t0 = time.monotonic()
    p14_inputs = p14.compute_input()
    p14_result = p14.compute(p14_inputs)
    timings["P14"] = time.monotonic() - t0
    commit("P14", {"result": p14_result}, p14_result.get("status"))
    context.check_cancel()

    # ---- P15 (same analyzer expression as the product service)
    t0 = time.monotonic()
    p15_result = p15.analyzer.evaluate(
        p14_result, prepared.get("required_cns") or {},
        prepared.get("cns_planning_objectives") or {})
    timings["P15"] = time.monotonic() - t0
    commit("P15", {"result": p15_result}, p15_result.get("status"))
    context.check_cancel()

    # ---- P16
    t0 = time.monotonic()
    outcome = p16.plan(on_progress=lambda value, message=None: None)
    timings["P16"] = time.monotonic() - t0
    p16_result = outcome.get("result")
    if not isinstance(p16_result, dict) or outcome.get("missing_reason"):
        raise RuntimeError("P16 critical calculation failed: %s"
                           % (outcome.get("missing_reason"),))
    commit("P16", outcome, p16_result.get("status"))
    context.check_cancel()

    # ---- P17 (existing entry; continuous-outage verdict only)
    t0 = time.monotonic()
    p17_service, corridor_model, gap_analyzer = _build_p17(session, prepared)
    p17_service.evaluate()
    timings["P17"] = time.monotonic() - t0
    p17_result = p17_service.result_snapshot()
    p17_gate = p17_service.step6_gate()
    p17_payload = {
        "result": p17_result, "status": p17_result.get("status"),
        "step6_gate": p17_gate, "parameters": p17_service.parameters_snapshot(),
        "fc30": p17_service.fc30_facts_snapshot(),
        "plan_stage": p17_result.get("plan_stage"), "authority": "research_isolated",
        "proposal_only": True,
    }
    commit("P17", p17_payload, p17_result.get("status"), extra={
        "algorithm_id": p17_result.get("algorithm_id"),
        "algorithm_version": p17_result.get("algorithm_version"),
        "baseline_status": p17_result.get("baseline_status"),
        "post_plan_status": p17_result.get("post_plan_status"),
        "gate_status": p17_gate.get("status"),
        "gate_allows_step6": p17_gate.get("confirmation_allowed"),
        "subsystems": _subsystem_counts(p17_result),
    })
    context.check_cancel()

    # ---- P18 (initialize only; Confirm / Apply are never executed)
    t0 = time.monotonic()
    p18_service = _build_p18(session, corridor_model, gap_analyzer, p17_service)
    p18_refusal = None
    try:
        p18_service.initialize()
        review = prepared.get("cns_plan_review") or {}
        variants = _p18_variants(review)
        confirmation_allowed = bool(variants) and all(
            item.get("confirmation_gate") == "ready_for_confirmation" for item in variants)
        p18_payload = {
            "result": review, "status": "review_initialized", "confirmed": False,
            "applied": False, "variants": variants,
            "confirmation_allowed": confirmation_allowed,
            "note": "P18 只完成评审初始化与变体评估；Confirm / Apply 属人工显式动作，本轮未执行。",
        }
        p18_status = "review_initialized"
        p18_extra = {
            "review_id": review.get("review_id"), "review_initialized": True,
            "variant_count": len(variants), "variants": variants,
            "confirmation_allowed": confirmation_allowed,
            "initialized_from": review.get("initialized_from"),
            "confirmed": False, "applied": False,
        }
    except ValueError as error:
        p18_refusal = str(error)
        p18_payload = {
            "result": None, "status": "blocked_by_p17_gate",
            "review_initialized": False, "confirmed": False, "applied": False,
            "refusal": {"blocking_reason": p18_refusal, "gate": p17_gate},
            "review_projection": p18_service.snapshot_result(),
            "note": "P18 按原有 fail-closed 门禁拒绝进入；未绕过门禁、未伪造评审结论。",
        }
        p18_status = "blocked_by_p17_gate"
        p18_extra = {
            "review_initialized": False, "blocking_reason": p18_refusal,
            "gate": p17_gate, "confirmation_allowed": False,
        }
    timings["P18"] = time.monotonic() - t0
    commit("P18", p18_payload, p18_status, extra=p18_extra)
    tasks.close()

    # Freeze this run's authoritative results before another run can overwrite
    # the shared working result directory.  The content-addressed artifacts are
    # already immutable; this snapshot makes the per-stage result SHA readable
    # for every historical run without re-solving.
    run_root = root / "runs" / run_id
    run_results_root = run_root / "results"
    if run_results_root.exists():
        shutil.rmtree(run_results_root)
    run_results_root.mkdir(parents=True)
    for name in sorted((root / "results").glob("*.json")):
        if name.name == "index.json":
            continue
        shutil.copy2(name, run_results_root / name.name)
    shutil.copy2(root / "results" / "index.json", run_results_root / "index.json")
    authoritative_index_sha = file_sha(run_results_root / "index.json")

    # ---- run manifest
    from ..domain.cns_continuous_service import CONTINUOUS_SERVICE_SCHEMA_VERSION
    from ..domain.plan_review import empty_plan_review

    supplement = (prepared.get("cns_corridor_assessment") or {}).get(
        "navigation_integrity_supplement") or {}
    contract_versions = {
        "p17_schema": CONTINUOUS_SERVICE_SCHEMA_VERSION,
        "p17_algorithm": "%s@%s" % (p17_result.get("algorithm_id"),
                                    p17_result.get("algorithm_version")),
        "p18_review": empty_plan_review().get("model_scope"),
        "nm3_supplement": supplement.get("contract_version"),
        "corridor": "%s@%s" % ((selection.get("corridor_model") or {}).get("algorithm_id"),
                               (selection.get("corridor_model") or {}).get("version")),
        "corridor_gap": "%s@%s" % (
            (selection.get("corridor_gap_analyzer") or {}).get("algorithm_id"),
            (selection.get("corridor_gap_analyzer") or {}).get("version")),
        "site_planner": "%s@%s" % ((selection.get("site_planner") or {}).get("algorithm_id"),
                                   (selection.get("site_planner") or {}).get("version")),
        "research_parameters": (parameter_record or {}).get("contract_version"),
    }
    gates = {
        "p17_step6_gate": p17_gate,
        "p18_initialize": {
            "status": p18_status, "allowed": p18_status == "review_initialized",
            "blocking_reason": p18_refusal,
            "allowed_p17_statuses": ["fully_satisfied", "acceptable_with_managed_gap"],
            "p17_status": p17_result.get("status"),
        },
    }
    p14_final = prepared.get("cns_corridor_assessment") or {}
    p15_final = prepared.get("cns_corridor_gap_assessment") or {}
    p16_final = prepared.get("cns_corridor_site_plan") or {}
    nm3 = p14_final.get("navigation_integrity_supplement") or {}
    counts, sites = _provider_facts(p14_final)
    manifest = {
        "schema_version": "c3b0-run-manifest-1", "task": "C3-B0",
        "run_id": run_id, "created_at": now(), "route_id": "R0005",
        "authority": "research_isolated",
        "formal_project_state_written": False, "raw_data_written": False,
        "git_written": False, "research_root": str(root),
        "confirmation_allowed": False, "formal_release_allowed": False,
        "state": {
            "path": "runtime/project_state.json",
            "file_sha256": session.load_manifest().get("file_sha256"),
            "input_fingerprint": input_fingerprint,
            "revision": prepared.get("revision"),
            "schema_version": prepared.get("schema_version"),
        },
        "input_version": {
            "id": "in-c3b-%s-%s" % (
                ((parameter_record or {}).get("fingerprint") or "default")[-12:],
                str(input_fingerprint)[:12]),
            "input_fingerprint": input_fingerprint,
            "revision": prepared.get("revision"), "route_id": "R0005",
            "surface_facts_fingerprint": (prepared.get("surface_class_facts") or {}).get(
                "input_fingerprint"),
            "navigation_integrity_config_fingerprint": (nm3.get("input_fingerprint")),
            "parameter_fingerprint": (parameter_record or {}).get("fingerprint"),
            "communication_radius": _radius_facts(prepared),
            "semantics": "fresh_research_input_from_R0005_never_formal_project_state",
        },
        "research_parameters": parameter_record,
        "research_parameter_schema": (
            {key: parameter_schema[key] for key in (
                "contract_version", "ids", "middle_segments", "options", "default",
                "label", "description", "authority")}
            if parameter_schema else None),
        "nm3_supplement": {
            "contract_version": nm3.get("contract_version"),
            "policy_id": nm3.get("policy_id"),
            "policy_version": nm3.get("policy_version"),
            "input_fingerprint": nm3.get("input_fingerprint"),
            "responsibilities": nm3.get("responsibilities") or [],
            "residual_gaps": nm3.get("residual_gaps") or [],
            "actual_delivery": "unknown",
            "proposal_only": True,
            "canonical_published": False,
        },
        "contract_version": contract_versions["p17_schema"],
        "contract_versions": contract_versions,
        "stages": records,
        "business_state": {
            "P14": {"status": p14_final.get("status"),
                    "subsystems": _subsystem_counts(p14_final),
                    "endpoint_status": (p14_final.get("endpoint_service_evidence") or {}).get(
                        "status")},
            "P15": {"status": p15_final.get("status"),
                    "subsystems": _subsystem_counts(p15_final)},
            "P16": {"status": p16_final.get("status"),
                    "stop_reason": p16_final.get("stop_reason"),
                    "selected_action_count": len(p16_final.get("selected_actions") or []),
                    "candidate_action_count": len(p16_final.get("candidate_actions") or []),
                    "reuse_counts": p16_final.get("reuse_counts"),
                    "cost_summary": p16_final.get("cost_summary")},
            "communication_C": {
                "verdict": "satisfied", "voxel_counts": _subsystem_voxels(p15_final, "C"),
                "radius_basis": "2 km 合同（device radius_by_surface，本轮生成并逐 provider 验证）",
            },
            "navigation_RID": {
                "verdict": "satisfied",
                "provider_evaluations": {"%s:%s" % key: value for key, value in counts.items()
                                         if key[0].startswith("S:rid")},
                "provider_site_count": len(sites.get("S:rid_cooperative") or []),
                "provider_site_ids": sites.get("S:rid_cooperative"),
            },
            "surveillance_Radar": {
                "verdict": "unknown", "formal_status": "not_calculated",
                "reason": "radar_layout_missing",
                "radar_surveillance_layout_present": bool(
                    prepared.get("radar_surveillance_layout")),
                "provider_evaluations": {"%s:%s" % key: value for key, value in counts.items()
                                         if key[0].startswith("S:radar")},
                "p17_supplementary_threat_status": p17_result.get(
                    "supplementary_threat_status"),
                "p17_limitations": _compact_limitations(p17_result.get("limitations")),
                "semantics": "缺 radar layout ⇒ Radar 不计算；不得默认 PASS、不得排除",
            },
            "surveillance_S": {
                "verdict": "unknown", "voxel_counts": _subsystem_voxels(p15_final, "S"),
                "semantics": "RID satisfied + Radar unknown ⇒ S unknown",
            },
            "navigation_N": {
                "verdict": "unknown",
                "note": "端点 N 完好性监视未安装（NM3 supplement 无 installed 证据）",
            },
            "navigation_endpoint_gaps": _nav_endpoint_gaps(p15_final),
        },
        "p17_threat_layers": _threat_layers(p17_result),
        "gates": gates,
        "admission": _admission(prepared, p17_result, p15_final, gates, records["P18"]),
        "research_limitations": _research_limitations(
            prepared, p17_result, p15_final, p16_final, records["P18"]),
        "limitations": _compact_limitations(p17_result.get("limitations")),
        "resource": {
            "elapsed_s": round(time.monotonic() - started, 3),
            "peak_rss_bytes": peak, "limit_bytes": MEMORY_LIMIT_BYTES,
            "limit_seconds": TIME_LIMIT_SECONDS,
        },
        "execution": {
            "worker_pid": os.getpid(), "task_id": context.task_id,
            "task_input_snapshot_sha256": context.record["input_fingerprint"],
            "worker": "cns_planner.tasks.worker", "mode": "fresh_compute",
        },
        "provenance": {
            "mode": "fresh_compute", "solver_executed": True,
            "result_root": str(run_results_root.parent),
            "source_root": str(source_root), "source_state_sha256": source_sha,
            "source_run_id": descriptor.get("source_run_id"),
            "research_parameters": parameter_record,
            "input_preparation": {
                "communication_radius_status": input_report["communication_radius"]["status"],
                "communication_provider_sample_count":
                    input_report["communication_radius"]["provider_sample_count"],
                "all_effective_radii_2000m":
                    input_report["communication_radius"]["all_effective_radii_2000m"],
                "surface_class_facts_status":
                    input_report["surface_class_facts"].get("status"),
                "surface_classification_equal_to_stored":
                    input_report["surface_class_facts"].get("classification_equal_to_stored"),
            },
            "recomputed_stages": list(STAGES),
        },
        "authoritative_result_list_sha256": authoritative_index_sha,
        "authoritative_results": results._index(),
    }
    manifest["map_projection"] = build_map_projection(prepared, p16_final, manifest)
    if manifest["input_version"]["communication_radius"]["communication_is_2km_by_surface"] is not True:
        raise ValueError("2km_contract_required")
    staged = context.stage(manifest, artifact_type="r0005_research_run_manifest")
    from ..tasks.task_spec import TaskRunResult, WorkerResultRef
    return TaskRunResult(summary={
        "run_id": run_id, "input_version": manifest["input_version"],
        "contract_version": manifest["contract_version"],
        "authoritative_result_list_sha256": manifest["authoritative_result_list_sha256"],
        "status_dimensions": {
            "P14": p14_final.get("status"), "P15": p15_final.get("status"),
            "P16": p16_final.get("status"), "P17": p17_result.get("status"),
            "P18": p18_status,
        },
        "execution": manifest["execution"],
    }, staged=WorkerResultRef(
        artifact_id=staged["artifact_id"], relative_path=staged["relative_path"],
        sha256=staged["sha256"], size_bytes=staged["size_bytes"],
        artifact_type="r0005_research_run_manifest"),
        message="fresh compute P14→P18 完成；未确认/未发布")


# ------------------------------------------------------------------ independent reload
def verify_reload(root, run_id):
    """Read the persisted run in a fresh process; recompute nothing."""

    root = Path(root).resolve()
    run_id = identifier(run_id)
    manifest_path = root / "runs" / run_id / "run_manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("run_id") != run_id:
        raise ValueError("run_identity_mismatch")
    if manifest.get("authority") != "research_isolated":
        raise ValueError("run_authority_mismatch")
    sys.path.insert(0, str(root / "src"))
    importlib.invalidate_caches()
    from research_session import ArtifactStore, ResearchSession, ResultStore, TaskStore

    session = ResearchSession(root)
    state = session.load()
    state_manifest = session.load_manifest()
    if state_manifest.get("file_sha256") != manifest["state"]["file_sha256"]:
        raise ValueError("state_file_sha_mismatch")
    if session.state_fingerprint() != manifest["input_version"]["input_fingerprint"]:
        raise ValueError("input_fingerprint_mismatch")
    result_root = Path((manifest.get("provenance") or {}).get("result_root") or root)
    results, artifacts = ResultStore(result_root), ArtifactStore(root)
    index_sha = file_sha(result_root / "results" / "index.json")
    if index_sha != manifest["authoritative_result_list_sha256"]:
        raise ValueError("authoritative_result_list_sha_mismatch")
    checked = {}
    for stage in STAGES:
        entry = manifest["stages"][stage]
        envelope = results.get(stage)
        index_entry = results._index()["entries"][stage]
        if index_entry["sha256"] != entry["result_sha256"]:
            raise ValueError("result_sha_mismatch: " + stage)
        if envelope["payload_sha256"] != entry["payload_sha256"]:
            raise ValueError("payload_sha_mismatch: " + stage)
        if envelope["input_fingerprint"] != manifest["input_version"]["input_fingerprint"]:
            raise ValueError("stage_input_mismatch: " + stage)
        artifacts.read(entry.get("artifact_id") or entry["payload_sha256"])
        checked[stage] = {
            "result_sha256": entry["result_sha256"],
            "payload_sha256": entry["payload_sha256"],
            "result_status": entry["result_status"],
        }
    task_rows = _task_rows(root)
    done = {row["task_id"]: row for row in task_rows if row.get("status") == "done"}
    for stage in STAGES:
        # New runs use run-scoped stage ids; C3-B0 manifests used the bare stage
        # name and remain readable.
        row = done.get("%s-%s" % (run_id, stage)) or done.get(stage)
        if row is None or row.get("result_sha") != manifest["stages"][stage]["result_sha256"]:
            raise ValueError("task_result_sha_mismatch: " + stage)
    return {
        "ok": True, "run_id": run_id, "root": str(root),
        "solver_executed": False, "recomputed_anything": False,
        "state_file_sha256": state_manifest["file_sha256"],
        "input_fingerprint": session.state_fingerprint(),
        "authoritative_result_list_sha256": index_sha,
        "stages": checked,
        "tasks": {stage: (done.get("%s-%s" % (run_id, stage)) or done.get(stage))["result_sha"]
                  for stage in STAGES},
        "business_state": manifest["business_state"],
        "gates": manifest["gates"],
        "admission": manifest["admission"],
        "verified_at": now(), "pid": os.getpid(),
    }


def _task_rows(root):
    """Read the research task store read-only (no solver, no writes)."""

    import sqlite3

    path = Path(root) / "tasks" / "tasks.sqlite"
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM tasks")]
    finally:
        connection.close()


__all__ = [
    "STAGES", "input_only_state", "run_fresh_compute", "verify_reload",
]
