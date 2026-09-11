from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.gap import CNSGapAnalyzerV1, GapAnalyzer


ROUTE = {"route_id": "R1", "status": "passed", "path": [[0.0, 0.0], [0.02, 0.0]]}
PROFILE = {
    "aircraft_id": "A1",
    "communication": {"status": "confirmed", "capabilities": ["radio"]},
    "navigation": {"status": "confirmed", "capabilities": ["gnss"]},
    "surveillance": {"status": "confirmed", "capabilities": ["ads-b"]},
}


def requirements(c_required=True):
    return {
        "status": "passed",
        "project_default": {
            "communication": {"status": "passed", "required": c_required, "coverage_requirement": 50, "max_gap_m": 700, "latency_ms": 100, "redundancy": 1},
            "navigation": {"status": "passed", "required": False},
            "surveillance": {"status": "passed", "required": False},
        },
        "route_overrides": {},
    }


def facilities(radius=600):
    return {
        "status": "passed", "items": [{
            "facility_id": "F1", "coordinate": [0.01, 0.0], "status": "active",
            "devices": [{"device_id": "DC", "subsystem": "C", "status": "active"}],
        }],
    }, {"status": "passed", "items": [{"device_id": "DC", "subsystem": "C", "radius_m": radius, "latency_ms": 50, "enabled": True}]}


def test_gap_analyzer_is_replaceable_protocol_and_uses_route_length():
    analyzer: GapAnalyzer = CNSGapAnalyzerV1()
    existing, catalog = facilities()
    result = analyzer.analyze([ROUTE], requirements(), PROFILE, existing, catalog)
    communication = result["routes"][0]["subsystems"][0]
    route_length = communication["ground_coverage"]["route_length_m"]
    assert route_length == pytest.approx(2223.9, rel=0.01)
    assert communication["ground_coverage"]["covered_length_m"] == pytest.approx(1200, rel=0.02)
    assert communication["coverage_ratio"] == pytest.approx(1200 / route_length, rel=0.02)
    assert communication["gap_length_m"] == pytest.approx(route_length - 1200, rel=0.02)
    assert sum(item["length_m"] for item in communication["uncovered_segments"]) == pytest.approx(communication["gap_length_m"])
    assert communication["status"] == "passed"


def test_gap_status_and_not_applicable_are_explicit():
    existing, catalog = facilities(radius=200)
    result = CNSGapAnalyzerV1().analyze([ROUTE], requirements(), PROFILE, existing, catalog)
    subsystems = result["routes"][0]["subsystems"]
    assert subsystems[0]["status"] == "gap"
    assert subsystems[0]["gap_length_m"] > 0
    assert subsystems[1]["status"] == "not_applicable"
    assert subsystems[2]["status"] == "not_applicable"


def test_missing_and_pending_inputs_never_pass():
    existing, catalog = facilities(radius=5000)
    no_aircraft = CNSGapAnalyzerV1().analyze([ROUTE], requirements(), None, existing, catalog)
    assert no_aircraft["routes"][0]["subsystems"][0]["status"] == "missing_data"
    pending = requirements(); pending["project_default"]["communication"]["status"] = "pending_confirmation"
    result = CNSGapAnalyzerV1().analyze([ROUTE], pending, PROFILE, existing, catalog)
    assert result["routes"][0]["subsystems"][0]["status"] == "pending_confirmation"
    catalog["items"][0].pop("latency_ms")
    missing_performance = CNSGapAnalyzerV1().analyze([ROUTE], requirements(), PROFILE, existing, catalog)
    assert missing_performance["routes"][0]["subsystems"][0]["status"] == "missing_data"


def test_fingerprint_and_segments_are_stable():
    existing, catalog = facilities()
    analyzer = CNSGapAnalyzerV1()
    first = analyzer.analyze([ROUTE], requirements(), PROFILE, existing, catalog)
    second = analyzer.analyze(deepcopy([ROUTE]), deepcopy(requirements()), deepcopy(PROFILE), deepcopy(existing), deepcopy(catalog))
    assert first == second
    segment = first["routes"][0]["subsystems"][0]["uncovered_segments"][0]
    assert set(segment) >= {"start", "end", "path", "length_m", "route_offset_start_m", "route_offset_end_m"}
    changed = analyzer.analyze([ROUTE], requirements(), PROFILE, *facilities(radius=700))
    assert changed["input_fingerprint"] != first["input_fingerprint"]


def test_route_override_and_redundancy_are_applied_per_route():
    existing, catalog = facilities(radius=5000)
    required = requirements()
    required["route_overrides"]["R1"] = {
        "communication": {"status": "passed", "required": True, "coverage_requirement": 100, "max_gap_m": 0, "latency_ms": 100, "redundancy": 2},
        "navigation": {"status": "passed", "required": False},
        "surveillance": {"status": "passed", "required": False},
    }
    result = CNSGapAnalyzerV1().analyze([ROUTE], required, PROFILE, existing, catalog)
    communication = result["routes"][0]["subsystems"][0]
    assert communication["ground_coverage"]["required_redundancy"] == 2
    assert communication["coverage_ratio"] == 0
    assert communication["status"] == "gap"


def test_workflow_persists_and_invalidates_gap_result(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))
    existing, catalog = facilities(radius=5000)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
    workflow.state["required_cns"] = requirements()
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [deepcopy(PROFILE)]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["existing_cns_facilities"] = existing
    workflow.state["device_catalog"] = catalog
    result = workflow.analyze_cns_gaps()
    assert result["cns_gap_analysis"]["routes"][0]["subsystems"][0]["status"] == "passed"
    assert WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json")).cns_gap_snapshot() == result["cns_gap_analysis"]
    false_requirements = {name: {"required": False} for name in ("communication", "navigation", "surveillance")}
    stale = workflow.set_required_cns({"scope": "project", "requirements": false_requirements})
    assert stale["cns_gap_analysis"]["status"] == "stale"
    assert stale["result_statuses"]["cns_gap"] == "stale"


def test_old_schema_v2_backfills_gap_result(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))
    workflow.state.pop("cns_gap_analysis")
    workflow.state["result_statuses"].pop("cns_gap")
    workflow.repository.save(workflow.state)
    restored = WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))
    assert restored.state["cns_gap_analysis"]["status"] == "not_calculated"
    assert restored.state["result_statuses"]["cns_gap"] == "not_calculated"


def test_gap_api_and_step5_overlay_are_wired():
    router = Path("cns_planner/api/router.py").read_text(encoding="utf-8")
    step5 = Path("cns_planner/web/js/workflow/step05_cns.js").read_text(encoding="utf-8")
    map_main = Path("cns_planner/web/js/main.js").read_text(encoding="utf-8")
    assert '"/api/cns-gaps"' in router and '"/api/cns-gaps/analyze"' in router
    assert "gap-analysis" in step5
    assert "uncovered_segments" in map_main


@pytest.mark.parametrize("changed", ["workspace", "route", "rules", "aircraft_profile", "required_cns", "devices", "existing_cns"])
def test_every_gap_input_invalidates_saved_result(tmp_path, changed):
    workflow = WorkflowService(tmp_path / "project.json", Path("cns_planner/config/defaults.json"))
    workflow.state["cns_gap_analysis"] = {**CNSGapAnalyzerV1.empty(), "status": "passed"}
    workflow.state["result_statuses"]["cns_gap"] = "passed"
    workflow.invalidation_service.workflow(changed)
    assert workflow.state["cns_gap_analysis"]["status"] == "stale"
    assert workflow.state["result_statuses"]["cns_gap"] == "stale"
