from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import build_default_algorithm_registry, default_algorithm_selection
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.gap.v1 import CNSGapAnalyzerV1
from cns_planner.gap.v2 import CNSGapAnalyzerV2


DEFAULTS = Path("cns_planner/config/defaults.json")


def required(code="C"):
    names = {"C": "communication", "N": "navigation", "S": "surveillance"}
    values = {
        name: {"required": False, "status": "passed"}
        for name in names.values()
    }
    values[names[code]] = {"required": True, "status": "passed"}
    return {"project_default": values, "route_overrides": {}}


def p7(code="C", covered=True, length=100.0):
    samples = [
        {"distance_along_route_m": 0.0, "covered": covered, "providers": [], "grid_id": "G0"},
        {"distance_along_route_m": length, "covered": covered, "providers": [], "grid_id": "G1"},
    ]
    return {"status": "passed", "routes": [{
        "route_id": "R1", "route_length_m": length, "samples": deepcopy(samples),
        "subsystems": [{"subsystem": code, "status": "passed", "samples": samples}],
    }]}


def p8(status="meets_under_model", code="C", offsets=(0.0, 100.0), providers=None):
    samples = [{
        "distance_along_route_m": offset, "status": status,
        "reasons": [f"p8:{status}"], "evidence": [],
        "provider_evaluations": deepcopy(providers or []),
    } for offset in offsets]
    return {"status": status, "routes": [{
        "route_id": "R1", "route_length_m": 100.0,
        "subsystems": [{"subsystem": code, "status": status, "samples": samples}],
    }]}


def timeline(states=("available",), code="C", boundaries=None):
    boundaries = boundaries or tuple(index * (100.0 / len(states)) for index in range(len(states) + 1))
    intervals = []
    for index, state in enumerate(states):
        start, end = boundaries[index], boundaries[index + 1]
        intervals.append({
            "start_route_offset_m": start, "end_route_offset_m": end,
            "length_m": end - start, "start_time_s": start / 10,
            "end_time_s": end / 10, "duration_s": (end - start) / 10,
            "service_state": state, "reasons": [f"p9:{state}"],
            "evidence": [], "fallback_used": {"source": "test"} if state == "contingency" else None,
        })
    return {"status": "passed", "routes": [{
        "route_id": "R1", "route_length_m": 100.0, "duration_s": 10.0,
        "subsystems": [{"subsystem": code, "status": "passed", "intervals": intervals}],
    }]}


def subsystem(result, code="C"):
    return next(item for item in result["routes"][0]["subsystems"] if item["subsystem"] == code)


def analyze(p8_value=None, timeline_value=None, *, code="C", parameters=None, p7_value=None, protection=None, devices=None):
    return CNSGapAnalyzerV2(parameters).analyze(
        required(code), p7_value or p7(code), p8_value or p8(code=code),
        timeline_value or timeline(code=code), protection or {}, devices or {},
    )


def test_planning_failure_and_runtime_loss_are_structured_confirmed_gaps():
    static_gap = subsystem(analyze(p8("does_not_meet_under_model"), timeline(("unknown",))))
    assert static_gap["status"] == "confirmed_gap"
    assert static_gap["gap_length_m"] == 100
    assert static_gap["unknown_length_m"] == 0
    assert "static_service_mismatch" in static_gap["segments"][0]["gap_causes"]
    assert "unknown_evidence" in static_gap["segments"][0]["gap_causes"]

    runtime_gap = subsystem(analyze(p8(), timeline(("lost",))))
    assert runtime_gap["planning_assessment"]["status"] == "satisfied"
    assert runtime_gap["operational_assessment"]["status"] == "confirmed_gap"
    assert runtime_gap["runtime_lost_duration_s"] == 10
    assert "runtime_service_loss" in runtime_gap["segments"][0]["gap_causes"]


def test_contingency_and_unknown_are_not_counted_as_gap():
    contingency = subsystem(analyze(p8(), timeline(("contingency",))))
    assert contingency["status"] == "satisfied_by_contingency"
    assert contingency["gap_length_m"] == 0
    assert contingency["contingency_exposure_length_m"] == 100
    assert contingency["contingency_exposure_duration_s"] == 10

    unknown = subsystem(analyze(p8("unknown"), timeline(("unknown",))))
    assert unknown["status"] == "unknown"
    assert unknown["gap_length_m"] == 0
    assert unknown["unknown_length_m"] == 100
    assert unknown["segments"][0]["remediation_scope"] in ("evidence_collection", "mixed")


def test_p8_transition_is_unknown_and_unified_breakpoints_merge_equal_states():
    changing = p8("meets_under_model")
    changing["routes"][0]["subsystems"][0]["samples"][1]["status"] = "does_not_meet_under_model"
    result = subsystem(analyze(changing, timeline(("available", "available"), boundaries=(0, 40, 100))))
    assert result["unknown_length_m"] == 100
    assert result["gap_length_m"] == 0
    assert len(result["segments"]) == 1
    assert result["segments"][0]["evidence"][0]["conservative_transition"] is True

    merged = subsystem(analyze(p8(), timeline(("available", "available"), boundaries=(0, 40, 100))))
    assert len(merged["segments"]) == 1
    assert merged["segments"][0]["combined_status"] == "satisfied"


def test_length_time_conservation_and_max_continuous_gap():
    result = subsystem(analyze(
        p8(), timeline(("lost", "available", "lost"), boundaries=(0, 20, 40, 100)),
    ))
    assert result["length_conservation_m"] == pytest.approx(100)
    assert result["time_conservation_s"] == pytest.approx(10)
    assert result["gap_length_m"] == pytest.approx(80)
    assert result["runtime_lost_duration_s"] == pytest.approx(8)
    assert result["max_continuous_gap_length_m"] == pytest.approx(60)
    assert result["max_continuous_gap_duration_s"] == pytest.approx(6)
    assert result["gap_segment_count"] == 2


def test_optional_protection_margin_requires_confirmed_actual_detection_range():
    providers = [{"device_id": "S1", "status": "meets_under_model"}]
    catalog = {"items": [{
        "device_id": "S1",
        "service_model": {
            "confirmed": True,
            "parameters": {"performance": {"min_detection_range_m": 200.0}},
        },
    }]}
    protection = {"status": "passed", "d_protect_m": 250.0}
    result = subsystem(analyze(
        p8(code="S", providers=providers), timeline(code="S"), code="S",
        parameters={"evaluate_protection_margin": True}, protection=protection,
        devices=catalog, p7_value=p7("S"),
    ), "S")
    assert result["status"] == "confirmed_gap"
    assert result["segments"][0]["protection_margin"]["protection_margin_m"] == -50
    assert "protection_margin_gap" in result["segments"][0]["gap_causes"]

    missing = subsystem(analyze(
        p8(code="S", providers=providers), timeline(code="S"), code="S",
        parameters={"evaluate_protection_margin": True}, protection=protection,
        devices={"items": []}, p7_value=p7("S"),
    ), "S")
    assert missing["status"] == "unknown"
    assert missing["gap_length_m"] == 0

    disabled_a = CNSGapAnalyzerV2().analyze(required(), p7(), p8(), timeline(), protection, catalog)
    disabled_b = CNSGapAnalyzerV2().analyze(required(), p7(), p8(), timeline(), {"status": "unknown"}, {})
    assert disabled_a["input_fingerprint"] == disabled_b["input_fingerprint"]


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_runtime_compatibility_api_and_no_new_persistence(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    registry = build_default_algorithm_registry(workflow.defaults)
    assert registry.manifest("cns_gap_analyzer", "cns_gap_analysis_v2", "2.0")
    assert "cns_gap_analyzer" not in default_algorithm_selection()
    assert workflow.gap_analysis_v2_service.analyzer.algorithm_id == "cns_gap_analysis_v2"
    assert workflow.gap_analysis_service.analyzer.algorithm_id == "cns_gap_analysis_v1"
    assert "cns_gap_analysis_v2" not in workflow.state
    assert "cns_gap_v2" not in workflow.state["result_statuses"]

    workflow.state["required_cns"] = required()
    workflow.state["coverage_3d"] = p7()
    workflow.state["cns_service_capability"] = p8()
    workflow.state["service_timeline"] = timeline()
    router = ApiRouter(ApiContext(workflow))
    response = router.post("/api/cns-gap-analysis-v2", {"parameters": {"evaluate_protection_margin": False}}).data
    assert response["cns_gap_analysis_v2"]["algorithm_version"] == "2.0"
    assert response["persistent_write"] is False
    assert response["deprecated"] is True  # old route is a compatibility alias
    assert "cns_gap_analysis_v2" not in workflow.state
    current = router.get("/api/compatibility/cns-gap-analysis-v2", {}, {}).data
    assert current["routes"][0]["route_id"] == "R1"
    assert current["authoritative"] is False

    reopened = WorkflowService(path, DEFAULTS)
    assert "cns_gap_analysis_v2" not in reopened.state
    assert reopened.cns_gap_v2_snapshot()["status"] == "not_calculated"

    workflow.invalidation_service.service_timeline()
    assert workflow.cns_gap_v2_snapshot()["status"] == "stale"


def test_gap_v1_contract_is_unchanged_by_v2_registration():
    assert CNSGapAnalyzerV1.algorithm_id == "cns_gap_analysis_v1"
    assert CNSGapAnalyzerV1.algorithm_version == "1.0"
    assert set(CNSGapAnalyzerV1.empty()) == {
        "status", "algorithm_id", "algorithm_version", "input_fingerprint",
        "route_count", "routes",
    }
