"""R7-D18：Step01–04 状态与 C/N/S 剩余问题（只读快照，不写 ProjectState）。"""

from __future__ import annotations

import json
from pathlib import Path

ACCEPT = Path(r"D:\Projects\_cns_acceptance\r7d18")
SNAPSHOT = ACCEPT / "api_workflow2.json"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "r7d18"
P14_EXPECTED_FP = "b070276bfb6a628564b59b2b112d6e9dedd5be9626e31a21f1f3a76e3134ae4a"
P15_EXPECTED_FP = "952103e3e98bb2d3056606d4aa139d94ae3679fc03f92399e66852c64cd977e2"
D4 = OUT / "existing_route_validation.json"


def _status(container):
    if not isinstance(container, dict):
        return None
    return container.get("status")


def _classification(container, *, current_version=None, historical=False):
    if not isinstance(container, dict) or not container:
        return "unknown"
    status = container.get("status")
    if historical or status in ("superseded",):
        return "historical_only"
    if status == "failed":
        return "business_failed"
    if status in ("stale", "superseded") or container.get("stale_reason"):
        return "stale"
    if status in (None, "", "not_calculated", "not_evaluated", "not_initialized",
                  "not_confirmed", "pending_confirmation", "unknown"):
        return "unknown"
    if current_version and container.get("algorithm_version") != current_version:
        return "stale"
    if status in ("passed", "ready", "confirmed", "recommendation_ready",
                  "completed_with_warnings", "proposal_ready", "configured"):
        return "current"
    return "unknown"


def _subsystem_counts(p15_route):
    result = {}
    for item in (p15_route or {}).get("subsystems") or []:
        service = item.get("service") or {}
        counts = service.get("voxel_counts") or {}
        result[item.get("subsystem")] = {
            "status": item.get("status"),
            "required_voxel_count": item.get("required_voxel_count"),
            "satisfied": counts.get("satisfied"),
            "confirmed_deficit": counts.get("confirmed_deficit"),
            "unknown": counts.get("unknown"),
            "max_continuous_deficit_projection_m": item.get("max_continuous_deficit_projection_m"),
        }
    return result


def main() -> int:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    catalog = {
        entry.get("algorithm_id"): entry.get("version")
        for entry in data.get("algorithm_catalog") or [] if isinstance(entry, dict)
    }
    statuses = data.get("result_statuses") or {}
    p14 = data.get("cns_corridor_assessment") or {}
    p15 = data.get("cns_corridor_gap_assessment") or {}
    p16 = data.get("cns_corridor_site_plan") or {}
    p16_results = data.get("cns_corridor_site_plan_results") or {}
    p17 = data.get("protection_envelope") or {}
    route = next(
        (item for item in data.get("operational_routes") or [] if item.get("route_id") == "R0005"),
        None,
    )
    p15_route = next(
        (item for item in p15.get("routes") or [] if item.get("route_id") == "R0005"), None,
    )
    pcf_collection = data.get("planning_constraint_fields") or {}
    pcf = next(
        (item for item in pcf_collection.get("items") or []
         if item.get("altitude_layer_id") == "ALT-100"), None,
    )
    recommendation = data.get("required_cns_recommendation") or {}
    adoption = data.get("required_cns_adoption") or {}
    source_audits = data.get("source_audits") or {}
    audit_items = {
        role: {
            "status": item.get("status"),
            "reasons": list(item.get("reasons") or []),
            "file_name": item.get("file_name"),
        }
        for role, item in (source_audits.get("items") or {}).items()
    }
    d4 = json.loads(D4.read_text(encoding="utf-8")) if D4.is_file() else {}
    validation_posture = data.get("layered_route_planner_readiness") or {}

    steps = {
        "1": {
            "title": "数据准备",
            "classification": "current" if _status(data.get("workspace")) == "passed" else "stale",
            "containers": {
                "workspace": {"status": _status(data.get("workspace"))},
                "grid": {"status": _status(data.get("grid")), "count": (data.get("grid") or {}).get("count")},
                "source_audits": {
                    "status": source_audits.get("status"),
                    "count": source_audits.get("count"),
                    "invalid_source_path_count": sum(
                        1 for item in audit_items.values()
                        if item.get("status") == "invalid_source_path"
                    ),
                    "equipment_reference_catalog": audit_items.get("equipment_reference_catalog"),
                },
                "data_readiness": {"status": _status(data.get("data_readiness"))},
            },
        },
        "2": {
            "title": "环境与风险",
            "classification": "stale",
            "containers": {
                "grid": {"status": _status(data.get("grid")), "classification": "current"},
                "grid_risk": {
                    "status": _status(data.get("grid_risk")),
                    "classification": _classification(data.get("grid_risk"), current_version="1.1"),
                    "algorithm_version": (data.get("grid_risk") or {}).get("algorithm_version"),
                },
                "grid_risk_v2": {
                    "status": _status(data.get("grid_risk_v2")),
                    "classification": _classification(data.get("grid_risk_v2"), current_version="2.0"),
                    "input_fingerprint": (data.get("grid_risk_v2") or {}).get("input_fingerprint"),
                    "policy_fingerprint": (data.get("grid_risk_v2") or {}).get("policy_fingerprint"),
                },
                "environment_risk": {"status": statuses.get("environment_risk")},
                "planning_constraint_fields": {
                    "status": pcf_collection.get("status"),
                    "classification": "current",
                    "count": pcf_collection.get("count"),
                    "alt100": {
                        "status": (pcf or {}).get("status"),
                        "field_id": (pcf or {}).get("field_id"),
                        "constraint_field_fingerprint": (pcf or {}).get("constraint_field_fingerprint"),
                        "stale_reason": (pcf or {}).get("stale_reason"),
                        "counts": (pcf or {}).get("counts"),
                        "warnings": (pcf or {}).get("warnings"),
                        "cells_detail": (pcf or {}).get("cells_detail"),
                        "artifact_ref": (pcf or {}).get("artifact_ref"),
                    },
                },
                "surface_class_facts": {
                    "status": _status(data.get("surface_class_facts")),
                    "classification": "current",
                    "input_fingerprint": (data.get("surface_class_facts") or {}).get("input_fingerprint"),
                },
                "building_clearance_assessment": {
                    "status": _status(data.get("building_clearance_assessment")),
                    "classification": "unknown",
                },
            },
        },
        "3": {
            "title": "航路规划与发布",
            "classification": "stale",
            "operational_route": {
                "route_id": "R0005",
                "status": (route or {}).get("status"),
                "vertex_count": len((route or {}).get("path") or []),
                "kind": (route or {}).get("kind"),
                "strict_safety_validation_passed": (
                    ((route or {}).get("provenance") or {}).get("strict_safety_validation_passed")
                ),
                "operational_authorization": (
                    ((route or {}).get("provenance") or {}).get("operational_authorization")
                ),
                "engineering_conditional_acceptance": (
                    ((route or {}).get("provenance") or {}).get("engineering_conditional_acceptance")
                ),
            },
            "d4_existing_route_revalidation": {
                "contract": d4.get("contract"),
                "status": d4.get("status"),
                "domains": {
                    name: {"status": value.get("status")}
                    for name, value in (d4.get("domains") or {}).items()
                },
                "risk_status": (d4.get("risk") or {}).get("status"),
                "risk_status_reason": (d4.get("risk") or {}).get("status_reason"),
                "research_only": d4.get("research_only"),
                "theta_star_executed": d4.get("theta_star_executed"),
            },
            "containers": {
                "layered_route_candidates": {
                    "status": (data.get("layered_route_candidates") or {}).get("status"),
                    "count": len((data.get("layered_route_candidates") or {}).get("items") or []),
                    "classification": "stale",
                    "stale_reason": "planning_constraint_field_changed",
                },
                "route_risk_profiles": {
                    "status": (data.get("route_risk_profiles") or {}).get("status"),
                    "count": len((data.get("route_risk_profiles") or {}).get("items") or []),
                    "classification": "stale",
                },
                "layered_route_validations": {
                    "status": (data.get("layered_route_validations") or {}).get("status"),
                    "count": len((data.get("layered_route_validations") or {}).get("items") or []),
                    "classification": "stale",
                },
                "layered_operational_adoptions": {
                    "status": (data.get("layered_operational_adoptions") or {}).get("status"),
                    "count": len((data.get("layered_operational_adoptions") or {}).get("items") or []),
                    "classification": "stale",
                },
            },
            "readiness": {
                "status": validation_posture.get("status"),
                "selected_altitude_layer_id": (
                    (validation_posture.get("altitude_layer_catalog") or {}).get(
                        "selected_altitude_layer_id"
                    )
                ),
                "request": validation_posture.get("request"),
            },
        },
        "4": {
            "title": "CNS 需求",
            "classification": "current" if recommendation.get("status") == "recommendation_ready" else "stale",
            "containers": {
                "required_cns": {"status": _status(data.get("required_cns")), "classification": "current"},
                "required_cns_recommendation": {
                    "status": recommendation.get("status"),
                    "classification": (
                        "current" if recommendation.get("status") == "recommendation_ready" else "stale"
                    ),
                    "result_status": recommendation.get("result_status"),
                    "input_fingerprint": recommendation.get("input_fingerprint"),
                    "proposal_only": recommendation.get("proposal_only"),
                    "requires_user_adoption": recommendation.get("requires_user_adoption"),
                    "adoption_status": recommendation.get("adoption_status"),
                },
                "required_cns_adoption": {
                    "status": adoption.get("status"),
                    "classification": "historical_only",
                    "recommendation_fingerprint": adoption.get("recommendation_fingerprint"),
                    "required_cns_fingerprint": adoption.get("required_cns_fingerprint"),
                },
            },
        },
    }

    p14_confirmed = p14.get("input_fingerprint") == P14_EXPECTED_FP and p14.get("algorithm_version") == "1.4"
    p15_confirmed = p15.get("input_fingerprint") == P15_EXPECTED_FP and p15.get("algorithm_version") == "1.3"
    cns_issues = {
        "p14": {
            "status": p14.get("status"),
            "classification": "business_failed",
            "algorithm_id": p14.get("algorithm_id"),
            "algorithm_version": p14.get("algorithm_version"),
            "current_algorithm_version": catalog.get(p14.get("algorithm_id")),
            "input_fingerprint": p14.get("input_fingerprint"),
            "fingerprint_matches_frozen": p14_confirmed,
            "detail_endpoint": p14.get("detail_endpoint"),
            "deficit_voxel_ids_count": p14.get("deficit_voxel_ids_count"),
            "unknown_voxel_ids_count": p14.get("unknown_voxel_ids_count"),
            "reusable": p14_confirmed,
            "note": "current 算法版本下的真实业务失败；不是计算 stale，不重算。",
        },
        "p15": {
            "status": p15.get("status"),
            "classification": "business_failed",
            "algorithm_id": p15.get("algorithm_id"),
            "algorithm_version": p15.get("algorithm_version"),
            "input_fingerprint": p15.get("input_fingerprint"),
            "fingerprint_matches_frozen": p15_confirmed,
            "detail_endpoint": p15.get("detail_endpoint"),
            "unknown_voxel_ids_count": p15.get("unknown_voxel_ids_count"),
            "route_length_m": (p15_route or {}).get("route_length_m"),
            "subsystems": _subsystem_counts(p15_route),
            "reusable": p15_confirmed,
            "note": "current 算法版本下的真实业务失败；不是计算 stale，不重算。",
        },
        "p16": {
            "status": p16.get("status"),
            "classification": "historical_only",
            "algorithm_version": p16.get("algorithm_version"),
            "input_fingerprint": p16.get("input_fingerprint"),
            "selected_actions_inline": len(p16.get("selected_actions") or []),
            "historical_selected_actions": len(p16_results.get("selected_actions") or []),
            "target_voxel_count": p16.get("target_voxel_count"),
            "p16_actionable_target_ids": p16_results.get("p16_actionable_target_ids"),
            "residual_confirmed_targets": len(p16_results.get("residual_confirmed_targets") or []),
            "unknown_evidence_required": len(p16_results.get("unknown_evidence_required") or []),
            "stop_reason": p16.get("stop_reason"),
            "note": "仅提案；13 项历史动作未确认安装，未应用。",
        },
        "p17": {
            "status": p17.get("status"),
            "algorithm_version": p17.get("algorithm_version"),
            "route_3d_profiles_status": (data.get("route_3d_profiles") or {}).get("status"),
            "note": "未计算；旧结果不得升级为 current。",
        },
        "continuous_service": {
            "status": (data.get("continuous_service_acceptability") or {}).get("status"),
            "algorithm_version": (data.get("continuous_service_acceptability") or {}).get("algorithm_version"),
            "stale_reason": (data.get("continuous_service_acceptability") or {}).get("stale_reason"),
            "note": "2.2 vs 当前代码 2.5：计算 stale，需按 2.5 重算；不是业务失败。",
        },
        "route_safety_evidence_v2": {
            "status": (data.get("route_safety_evidence_v2") or {}).get("status"),
        },
    }
    report = {
        "schema_version": "cns-r7d18-step-status/v1",
        "snapshot": str(SNAPSHOT),
        "revision": data.get("revision"),
        "project": data.get("project"),
        "steps": steps,
        "cns_issues": cns_issues,
        "remaining_blockers": [
            "Step02: grid_risk_v2 仍 pending_confirmation（风险策略未确认），grid_risk stale",
            "Step03: candidate/validation/adoption 链 stale；既有航路 D4 复验为 unknown"
            "（建筑 4tCa7 仅 predicted 高度；air_traffic 风险指数缺失）",
            "Step03: 运行航路 provenance strict_safety_validation_passed=false、"
            "operational_authorization=false（工程演示条件接受，不是正式发布）",
            "Step04: recommendation 已 current（proposal-only）；历史 adoption 仍 superseded，"
            "未自动 adopt",
            "Step05: P14/P15 business_failed（C 2727 缺口 / S 2727 缺口 / N 2727 unknown）",
            "Step05: P16 仅 proposal_ready，13 项历史动作未确认安装；连续服务 2.2→2.5 计算 stale",
            "Step06: 方案审查 stale、方案未确认、报告未计算；P17 未计算",
        ],
    }
    (OUT / "step_status.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(json.dumps({
        "revision": report["revision"],
        "steps": {key: value["classification"] for key, value in steps.items()},
        "p14": {k: cns_issues["p14"][k] for k in ("status", "fingerprint_matches_frozen")},
        "p15": {k: cns_issues["p15"][k] for k in ("status", "fingerprint_matches_frozen")},
        "subsystems": cns_issues["p15"]["subsystems"],
        "d4": steps["3"]["d4_existing_route_revalidation"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
