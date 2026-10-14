"""Round38-W1B 证据生成：P16 真实站址门禁、P16 性能/停止探针、P17 独立判定样例。

只读、确定性：全部输入都是本文件内显式构造的工程夹具或**既有测试夹具**；
不读也不写任何正式 ProjectState，不调用任何失效入口，**不对正式项目执行全量
P16 搜索**（探针只跑一个最小 workflow 夹具）。

用法（仓库根目录）：
    D:\\tools\\anaconda\\python.exe tools\\round38_w1b_evidence.py <输出目录>
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (str(REPO_ROOT), str(REPO_ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    registered_p16_site_sources,
)
from test_round29g_navigation_endpoint_planning import (  # noqa: E402
    PLANNER_FAMILY, _endpoint_workflow,
)
from test_round38_w1b_fixed_corridor import (  # noqa: E402
    _response_result, _route_result, _site, _tower,
)


def _source_gate_statistics():
    """确定性门禁夹具：只允许已登记起降点 / 真实塔址 / 真实的已确认既有 CNS 设施。"""

    shared = {
        "status": "passed",
        "candidates": [
            {"candidate_id": "node:N1", "source_id": "N1",
             "candidate_kind": "project_node", "spatial_use_status": "eligible"},
            #: 已登记节点额外关联一条参考记录：不得因此被误判成"未登记候选"。
            {"candidate_id": "node:N2", "source_id": "N2", "candidate_kind": "project_node",
             "spatial_use_status": "eligible", "explicit_reference_site_id": "R2"},
            {"candidate_id": "reference-landing-site:R1", "source_id": "R1",
             "candidate_kind": "reference_landing_site", "spatial_use_status": "blocked",
             "spatial_block_reason": "reference_source_crs_unresolved"},
            {"candidate_id": "tower-colocation:T1", "source_id": "T1",
             "candidate_kind": "tower_colocation", "spatial_use_status": "eligible"},
        ],
        "source_counts": {"tower_records": 2},
    }
    sources = registered_p16_site_sources(
        shared,
        existing_facilities={"status": "passed", "items": [
            {"facility_id": "F-REAL",
             "metadata": {"dataset_class": "field_survey", "real_world_facility": True}},
            {"facility_id": "F-SYNTHETIC",
             "metadata": {"dataset_class": "synthetic_engineering_validation",
                          "real_world_facility": False}},
            {"facility_id": "F-UNCONFIRMED", "metadata": {"real_world_facility": True},
             "planning_profile": {"confirmed": False}},
        ]},
        candidate_sites={"status": "passed", "items": [
            _site("N1"), _site("N2", reference_site_id="R2"),
            _site("FREE"), _site("R1", reference_site_id="R1"),
        ]},
        tower_colocation={"status": "passed", "items": [
            _tower("tower:T1", "T1"), _tower("tower:SYNTHETIC", "SYNTHETIC"),
        ]},
    )
    statistics = sources["statistics"]
    return {
        "source_policy": statistics["source_policy"],
        "existing_cns_source_gate": statistics["existing_cns_source_gate"],
        "allowed_source_counts": statistics["allowed_source_counts"],
        "excluded_reason_counts": statistics["excluded_reason_counts"],
        "synthetic_tower_count": statistics["synthetic_tower_count"],
        "allowed_ids": {
            "candidate_sites": [
                item["site_id"] for item in sources["candidate_sites"]["items"]],
            "tower_colocation": [
                item["metadata"]["host"]["host_tower_id"]
                for item in sources["tower_colocation"]["items"]],
            "existing_cns_facilities": [
                item["facility_id"] for item in sources["existing_facilities"]["items"]],
        },
    }


def _performance_probe(workdir, *, c_facility):
    workflow = _endpoint_workflow(workdir, c_facility=c_facility)
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    profile = plan.get("performance_profile") or {}
    return {
        "c_facility": c_facility,
        "planner_family": PLANNER_FAMILY,
        "status": plan.get("status"),
        "candidate_action_count": len(plan.get("candidate_actions") or []),
        "selected_action_count": len(plan.get("selected_actions") or []),
        "candidate_count_by_planner_family": profile.get("candidate_count_by_planner_family"),
        "full_corridor_rerun_count": profile.get("full_corridor_rerun_count"),
        "p14_seconds": profile.get("p14_seconds"),
        "p15_seconds": profile.get("p15_seconds"),
        "stop_diagnostics": plan.get("stop_diagnostics"),
    }


def _p17_examples():
    rows = []
    for detection in (5000.0, 200.0, None):
        result = _response_result(detection_range=detection)
        corridor = _route_result(result)["corridor"]
        rows.append({
            "detection_range_m": detection,
            "algorithm_version": result.get("algorithm_version"),
            "service_target_half_width_m": corridor.get("service_target_half_width_m"),
            "corridor_width_policy_version": corridor.get("corridor_width_policy_version"),
            "response_protection_distance_m": corridor.get("response_protection_distance_m"),
            "response_timeliness_status": corridor.get("response_timeliness_status"),
            "outer_half_width_m": corridor.get("outer_half_width_m"),
        })
    wide = _response_result(detection_range=5000.0)
    narrow = _response_result(detection_range=200.0)
    return rows, {
        "detection_5000m": wide.get("input_fingerprint"),
        "detection_200m": narrow.get("input_fingerprint"),
        "fingerprint_differs": wide.get("input_fingerprint") != narrow.get("input_fingerprint"),
    }


def main():
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (
        REPO_ROOT / "outputs" / "round38_w1b")
    out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="r38w1b_probe_"))
    rows, fingerprint = _p17_examples()
    payload = {
        "p16_source_gate": _source_gate_statistics(),
        "p16_performance_probe": [
            _performance_probe(workdir, c_facility=False),
            _performance_probe(workdir, c_facility=True),
        ],
        "p17_independent_verdicts": rows,
        "p17_input_fingerprint_covers_detection": fingerprint,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    (out / "02_w1b_evidence.json").write_text(text + "\n", encoding="utf-8")
    gate = payload["p16_source_gate"]
    print(json.dumps({
        "output": str(out / "02_w1b_evidence.json"),
        "allowed_source_counts": gate["allowed_source_counts"],
        "excluded_reason_counts": gate["excluded_reason_counts"],
        "p16_probes": [
            {key: probe[key] for key in (
                "c_facility", "status", "candidate_action_count", "selected_action_count",
                "full_corridor_rerun_count", "p14_seconds", "p15_seconds")}
            for probe in payload["p16_performance_probe"]
        ],
        "p17": [
            {key: row[key] for key in (
                "detection_range_m", "response_timeliness_status", "outer_half_width_m")}
            for row in rows
        ],
        "fingerprint_differs": fingerprint["fingerprint_differs"],
    }, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
