"""R7-D17 真实 R0005 Step01–06 只读状态核查。

读取验收实例的 /api/workflow 快照（不写 ProjectState、不触发任何重算），
按 current / stale / failed / historical_only / unknown 分类并记录输入指纹。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "step_status.json"

STEPS = {
    "1": ("数据准备", ["workspace", "source_audits", "data_readiness"]),
    "2": ("环境与风险", ["grid", "environment_risk", "grid_risk", "grid_risk_v2",
                          "planning_constraint_fields", "surface_class_facts",
                          "building_clearance_assessment"]),
    "3": ("航路规划与发布", ["layered_route_candidates", "route_risk_profiles",
                              "layered_route_validations", "layered_operational_adoptions"]),
    "4": ("CNS 需求", ["required_cns", "required_cns_recommendation",
                        "required_cns_adoption"]),
    "5": ("CNS 能力与设施规划", ["coverage_3d", "cns_service_capability",
                                  "cns_corridor_assessment", "cns_corridor_gap_assessment",
                                  "cns_corridor_site_plan", "continuous_service_acceptability",
                                  "radar_surveillance_layout"]),
    "6": ("方案评审与报告", ["cns_plan_review", "confirmed_cns_plan",
                              "cns_planning_reports", "protection_envelope",
                              "route_3d_profiles"]),
}
HISTORICAL = {"required_cns_adoption", "cns_corridor_site_plan"}
UNKNOWN_STATUS = {"not_calculated", "not_evaluated", "not_initialized", "not_confirmed",
                  "pending_confirmation", "pending", None, ""}
CURRENT_STATUS = {"passed", "ready", "proposal_ready", "confirmed", "configured", "completed"}
RANK = {"current": 0, "historical_only": 1, "unknown": 2, "stale": 3, "failed": 4}


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (TypeError, ValueError):
        return ()


def _fingerprint(container):
    for key in ("input_fingerprint", "fingerprint", "policy_fingerprint",
                "constraint_field_fingerprint", "baseline_fingerprint"):
        value = container.get(key)
        if isinstance(value, str) and value:
            return {"field": key, "value": value}
    return None


def _classify(name, container, current_version):
    status = container.get("status")
    if name in HISTORICAL:
        return "historical_only"
    if status == "failed":
        return "failed"
    if status in {"stale", "superseded"} or container.get("stale_reason"):
        return "stale"
    if status == "proposal_ready" and container.get("proposal_only"):
        return "historical_only"
    if status in UNKNOWN_STATUS:
        return "unknown"
    if container.get("detail_status") == "artifact_unavailable":
        return "unknown"
    if current_version and container.get("algorithm_version"):
        if _version_tuple(container.get("algorithm_version")) != _version_tuple(current_version):
            return "stale"
    if status in CURRENT_STATUS:
        return "current"
    return "unknown"


def main() -> int:
    snapshot = Path(sys.argv[1])
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    catalog = {
        entry.get("algorithm_id"): entry.get("version")
        for entry in data.get("algorithm_catalog") or []
        if isinstance(entry, dict)
    }

    report = {
        "schema_version": "cns-r7d17-step-status/v1",
        "snapshot": str(snapshot),
        "revision": data.get("revision"),
        "project": data.get("project"),
        "steps": {},
    }
    for step, (title, names) in STEPS.items():
        containers = {}
        worst = "current"
        for name in names:
            container = data.get(name)
            if not isinstance(container, dict):
                fallback = (data.get("result_statuses") or {}).get(name)
                classification = fallback if fallback in ("stale", "failed") else "unknown"
                containers[name] = {"present": False, "status": fallback,
                                    "classification": classification}
                worst = max(worst, classification, key=RANK.get)
                continue
            current_version = catalog.get(container.get("algorithm_id"))
            classification = _classify(name, container, current_version)
            item = {
                "status": container.get("status"),
                "classification": classification,
                "algorithm_id": container.get("algorithm_id"),
                "algorithm_version": container.get("algorithm_version"),
                "current_algorithm_version": current_version,
                "stale_reason": container.get("stale_reason"),
                "detail_status": container.get("detail_status"),
                "proposal_only": container.get("proposal_only"),
                "count": container.get("count") if isinstance(container.get("count"), int) else None,
                "route_count": container.get("route_count"),
                "input_fingerprint": _fingerprint(container),
            }
            containers[name] = {key: value for key, value in item.items() if value is not None}
            if RANK[classification] > RANK[worst]:
                worst = classification
        report["steps"][step] = {
            "title": title,
            "classification": worst,
            "containers": containers,
        }

    routes = data.get("operational_routes") or []
    report["routes"] = {
        "operational": [
            {"route_id": route.get("route_id"), "status": route.get("status"),
             "vertex_count": len(route.get("path") or []),
             "start": route.get("start"), "end": route.get("end")}
            for route in routes
        ],
        "result_status": (data.get("result_statuses") or {}).get("routes"),
    }
    profiles = data.get("tower_obstacle_profiles") or {}
    report["assets"] = {
        "towers": {"status": (data.get("towers") or {}).get("status"),
                    "count": (data.get("towers") or {}).get("count"),
                    "resolved": profiles.get("resolved_count"),
                    "resolved_unconfirmed": profiles.get("resolved_unconfirmed_count"),
                    "unresolved": profiles.get("unresolved_count")},
        "reference_landing_sites": (data.get("reference_landing_sites") or {}).get("count"),
        "operational_timing_routes": len((data.get("operational_timing") or {}).get("route_motion_profiles") or []),
    }
    p16 = data.get("cns_corridor_site_plan_results") or {}
    actions = p16.get("selected_actions") or []
    report["p16_historical_actions"] = {
        "count": len(actions),
        "physical_mount_confirmed": sum(1 for item in actions if item.get("physical_mount_confirmed") is True),
        "requires_site_survey": sum(1 for item in actions if item.get("requires_site_survey") is True),
        "subsystem_mount_status": sorted({item.get("subsystem_mount_status") for item in actions}),
        "selected_actions_inline_in_plan": len(
            (data.get("cns_corridor_site_plan") or {}).get("selected_actions") or []),
        "iteration_trace_count": len(
            (data.get("cns_corridor_site_plan") or {}).get("iteration_trace") or []),
        "stop_reason": (data.get("cns_corridor_site_plan") or {}).get("stop_reason"),
        "target_voxel_count": (data.get("cns_corridor_site_plan") or {}).get("target_voxel_count"),
        "residual_confirmed_targets": len(p16.get("residual_confirmed_targets") or []),
        "unknown_evidence_required": len(p16.get("unknown_evidence_required") or []),
    }
    report["p17"] = {
        "container": "protection_envelope",
        "status": (data.get("protection_envelope") or {}).get("status"),
        "input_fingerprint": (data.get("protection_envelope") or {}).get("input_fingerprint"),
        "route_3d_profiles_status": (data.get("route_3d_profiles") or {}).get("status"),
        "note": "本轮未计算 P17；历史 P17 结果不得升级为 current",
    }
    report["artifact_diagnostics"] = data.get("artifact_diagnostics") or []
    report["result_statuses"] = data.get("result_statuses")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(json.dumps({
        "revision": report["revision"],
        "steps": {step: value["classification"] for step, value in report["steps"].items()},
        "p16_actions": report["p16_historical_actions"]["count"],
        "route": report["routes"]["operational"],
        "diagnostics": len(report["artifact_diagnostics"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
