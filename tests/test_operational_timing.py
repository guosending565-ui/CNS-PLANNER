from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.protection.v1 import TacticalProtectionEnvelopeV1
from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.algorithms.timeline.v1 import RouteServiceTimelineV1
from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.application.project_state import normalize_project
from cns_planner.domain.operational_timing import (
    RESPONSE_COMPONENTS, distance_to_time_s, empty_operational_timing,
    normalize_encounter_scenario, normalize_operational_timing,
    normalize_response_time_budget, normalize_route_motion_profile,
    normalize_service_scenario,
)


DEFAULTS = Path("cns_planner/config/defaults.json")


def p7_coverage():
    samples = [
        {"distance_along_route_m": 0.0, "longitude": 0.0, "latitude": 0.0},
        {"distance_along_route_m": 100.0, "longitude": 0.001, "latitude": 0.0},
    ]
    return {"status": "passed", "routes": [{
        "route_id": "R1", "route_length_m": 100.0,
        "samples": samples, "subsystems": [],
    }]}


def p8_capability(status="meets_under_model"):
    return {"status": status, "routes": [{
        "route_id": "R1", "subsystems": [
            {"subsystem": code, "status": status, "samples": [
                {"distance_along_route_m": 0.0, "status": status},
                {"distance_along_route_m": 100.0, "status": status},
            ]}
            for code in ("C", "N", "S")
        ],
    }]}


def c_requirement():
    return {
        "required": True, "status": "passed",
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 1.0},
    }


def required_cns():
    return {"project_default": {
        "communication": c_requirement(),
        "navigation": {"required": False, "status": "passed"},
        "surveillance": {"required": False, "status": "passed"},
    }, "route_overrides": {}}


def aircraft_profile(fallback=False):
    communication = {
        "confirmed": True, "status": "confirmed", "capabilities": ["radio"],
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"max_latency_s": 0.5}, "fallbacks": [],
    }
    if fallback:
        communication["fallbacks"] = [{
            "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
            "performance": {"max_latency_s": 0.8},
            "max_bridge_time_s": 5.0, "source": "test",
            "confirmed": True,
        }]
    return {
        "aircraft_id": "A1", "communication": communication,
        "navigation": {}, "surveillance": {},
    }


def motion(speed=10.0, confirmed=True):
    return normalize_route_motion_profile({
        "route_id": "R1", "mode": "constant_ground_speed_mps",
        "constant_ground_speed_mps": speed, "source": "test",
        "confirmed": confirmed,
    })


def event(state="available", start=0.0, end=4.0, event_id="E1", confirmed=True):
    return {
        "event_id": event_id, "subsystem": "C", "start_s": start,
        "end_s": end, "external_state": state,
        "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
        "performance": {"latency_s": 0.2}, "source": "test",
        "confirmed": confirmed,
    }


def timing(events=None, include_motion=True):
    raw = empty_operational_timing()
    if include_motion:
        raw["route_motion_profiles"]["R1"] = motion()
    if events is not None:
        raw["service_scenarios"]["R1"] = {
            "route_id": "R1", "scenario_id": "SC1", "events": events,
            "source": "test", "confirmed": True,
        }
    return normalize_operational_timing(raw)


def budget(value=1.0, confirmed=True):
    return normalize_response_time_budget({
        "budget_id": "B1", "source": "test", "confirmed": confirmed,
        "components": {
            name: {"value_s": value, "source": f"test:{name}", "confirmed": confirmed}
            for name in RESPONSE_COMPONENTS
        },
    })


def encounter(relative=30.0, confirmed=True):
    return normalize_encounter_scenario({
        "encounter_id": "X1", "relative_closing_speed_mps": relative,
        "maneuver_distance_m": 50.0, "uncertainty_distance_m": 20.0,
        "source": "test", "confirmed": confirmed,
    })


def test_distance_to_time_and_motion_validation():
    assert distance_to_time_s(100, 20) == 5
    with pytest.raises(ValueError, match="大于零"):
        distance_to_time_s(1, 0)
    with pytest.raises(ValueError, match="大于零"):
        normalize_route_motion_profile({
            "route_id": "R1", "constant_ground_speed_mps": 0,
            "confirmed": True,
        })
    assert motion(10, confirmed=False)["status"] == "pending_confirmation"
    reserved = normalize_route_motion_profile({
        "route_id": "R1", "mode": "waypoint_linear",
        "waypoints": [{"distance_along_route_m": 0, "ground_speed_mps": 10}],
        "source": "test", "confirmed": True,
    })
    assert reserved["status"] == "pending_confirmation"


def test_p8_meet_without_runtime_event_remains_unknown():
    model = RouteServiceTimelineV1()
    timing_value = timing(events=None)
    result = model.evaluate(
        p7_coverage(), p8_capability("meets_under_model"), required_cns(),
        aircraft_profile(), timing_value,
    )
    subsystem = result["routes"][0]["subsystems"][0]
    assert subsystem["states_present"] == ["unknown"]
    assert subsystem["unknown_duration_s"] == 10
    assert subsystem["unknown_length_m"] == 100
    assert subsystem["intervals"][0]["evidence"][0]["status"] == "meets_under_model"
    timing_value["response_time_budgets"]["B1"] = budget()
    unchanged = model.evaluate(
        p7_coverage(), p8_capability("meets_under_model"), required_cns(),
        aircraft_profile(), timing_value,
    )
    assert unchanged["input_fingerprint"] == result["input_fingerprint"]


def test_explicit_scenario_calls_p4_and_fallback_is_preserved():
    available = RouteServiceTimelineV1().evaluate(
        p7_coverage(), p8_capability(), required_cns(), aircraft_profile(),
        timing([event(end=10.0)]),
    )["routes"][0]["subsystems"][0]
    assert available["states_present"] == ["available"]

    fallback = RouteServiceTimelineV1().evaluate(
        p7_coverage(), p8_capability(), required_cns(), aircraft_profile(True),
        timing([event("unavailable", end=4.0)]),
    )["routes"][0]["subsystems"][0]
    first = fallback["intervals"][0]
    assert first["service_state"] == "contingency"
    assert first["fallback_used"]["source"] == "test"
    assert "unknown" in fallback["states_present"]


def test_overlapping_events_are_rejected_without_priority_guessing():
    with pytest.raises(ValueError, match="时间重叠"):
        normalize_service_scenario({
            "route_id": "R1", "events": [
                event(start=0, end=5, event_id="E1"),
                event(start=4, end=6, event_id="E2"),
            ], "confirmed": True,
        })


def test_timeline_duration_and_length_keep_unknown_separate():
    result = RouteServiceTimelineV1().evaluate(
        p7_coverage(), p8_capability(), required_cns(), aircraft_profile(),
        timing([event(start=0, end=4)]),
    )["routes"][0]["subsystems"][0]
    assert result["duration_by_state_s"]["available"] == 4
    assert result["duration_by_state_s"]["unknown"] == 6
    assert result["length_by_state_m"]["available"] == 40
    assert result["length_by_state_m"]["unknown"] == 60
    assert sum(result["duration_by_state_s"].values()) == 10
    assert sum(result["length_by_state_m"].values()) == 100


def test_unconfirmed_motion_does_not_borrow_aircraft_speed():
    result = RouteServiceTimelineV1().evaluate(
        p7_coverage(), p8_capability(), required_cns(),
        {**aircraft_profile(), "cruise_speed_mps": 99},
        normalize_operational_timing({"route_motion_profiles": {"R1": motion(10, False)}}),
    )["routes"][0]
    assert result["duration_s"] is None
    assert result["samples"][1]["time_from_start_s"] is None


def test_protection_budget_fixture_and_missing_inputs():
    result = TacticalProtectionEnvelopeV1().evaluate(budget(), encounter())
    assert result["t_pre_s"] == 6
    assert result["relative_closing_speed_mps"] == 30
    assert result["d_reaction_m"] == 180
    assert result["d_protect_m"] == 250
    assert result["model_scope"] == "engineering_tactical_protection_envelope"
    assert result["regulatory_well_clear"] == "not_evaluated"
    assert result["formal_detection_volume_compliance"] == "not_evaluated"
    missing = TacticalProtectionEnvelopeV1().evaluate(budget(confirmed=False), encounter())
    assert missing["status"] == "unknown"
    assert missing["d_protect_m"] is None


class ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()


def test_registry_persistence_api_and_directed_invalidation(tmp_path):
    path = tmp_path / "project.json"
    workflow = WorkflowService(path, DEFAULTS)
    registry = build_default_algorithm_registry(workflow.defaults)
    assert registry.manifest("timeline_model", "route_service_timeline_v1", "1.0")
    assert registry.manifest("protection_model", "tactical_protection_envelope_v1", "1.0")
    configured = timing([event(end=10)])
    configured["response_time_budgets"]["B1"] = budget()
    configured["encounter_scenarios"]["X1"] = encounter()
    router = ApiRouter(ApiContext(workflow))
    router.post("/api/operational-timing", {"operational_timing": configured})
    assert router.get("/api/operational-timing", {}, {}).data["route_motion_profiles"]["R1"]["constant_ground_speed_mps"] == 10

    workflow.state["coverage_3d"] = p7_coverage()
    workflow.state["cns_service_capability"] = p8_capability()
    workflow.state["required_cns"] = required_cns()
    workflow.state["aircraft_profiles"] = {"items": [aircraft_profile()]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    timeline_result = router.post("/api/service-timeline/evaluate", {}).data
    assert timeline_result["service_timeline"]["routes"][0]["duration_s"] == 10
    protection_result = router.post("/api/protection-envelope/evaluate", {"budget_id": "B1", "encounter_id": "X1"}).data
    assert protection_result["protection_envelope"]["d_protect_m"] == 250
    assert router.get("/api/service-timeline", {}, {}).data["algorithm_id"] == "route_service_timeline_v1"
    assert router.get("/api/protection-envelope", {}, {}).data["algorithm_id"] == "tactical_protection_envelope_v1"

    restored = WorkflowService(path, DEFAULTS)
    assert restored.state["operational_timing"]["response_time_budgets"]["B1"]["status"] == "confirmed"
    assert restored.service_timeline_snapshot()["input_fingerprint"] == timeline_result["service_timeline"]["input_fingerprint"]

    restored.state["service_timeline"]["status"] = "passed"
    restored.state["protection_envelope"]["status"] = "passed"
    restored.state["result_statuses"].update({
        "service_timeline": "passed", "protection_envelope": "passed",
        "grid": "passed", "routes": "passed", "coverage": "passed", "cns_gap": "passed",
    })
    changed = deepcopy(restored.state["operational_timing"])
    changed["route_motion_profiles"]["R1"]["constant_ground_speed_mps"] = 20
    restored.set_operational_timing(changed)
    assert restored.state["result_statuses"]["service_timeline"] == "stale"
    assert restored.state["result_statuses"]["protection_envelope"] == "passed"
    assert {name: restored.state["result_statuses"][name] for name in ("grid", "routes", "coverage", "cns_gap")} == {name: "passed" for name in ("grid", "routes", "coverage", "cns_gap")}

    restored.state["service_timeline"]["status"] = "passed"
    restored.state["result_statuses"]["service_timeline"] = "passed"
    changed = deepcopy(restored.state["operational_timing"])
    changed["encounter_scenarios"]["X1"]["uncertainty_distance_m"] = 25
    restored.set_operational_timing(changed)
    assert restored.state["result_statuses"]["protection_envelope"] == "stale"
    assert restored.state["result_statuses"]["service_timeline"] == "passed"


def test_old_schema_v2_backfill_and_altitude_change_only_stale_future_timeline(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    legacy = deepcopy(workflow.state)
    for field in ("operational_timing", "service_timeline", "protection_envelope"):
        legacy.pop(field)
    for field in ("service_timeline", "protection_envelope"):
        legacy["result_statuses"].pop(field)
    restored = normalize_project(legacy, workflow.grid_service)
    assert restored["operational_timing"] == empty_operational_timing()
    assert restored["service_timeline"]["status"] == "not_calculated"
    assert restored["protection_envelope"]["status"] == "not_calculated"

    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[0, 0], [0.001, 0]],
    }]
    workflow.state["coverage_3d"] = {"status": "passed"}
    workflow.state["cns_service_capability"] = {"status": "meets_under_model"}
    workflow.state["service_timeline"] = {"status": "passed"}
    workflow.state["result_statuses"].update({
        "coverage_3d": "passed", "cns_service_capability": "passed",
        "service_timeline": "passed", "grid": "passed", "routes": "passed",
        "coverage": "passed", "cns_gap": "passed",
    })
    workflow.set_route_altitude_profile({
        "route_id": "R1", "mode": "constant",
        "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": 100, "source": "test", "confirmed": True,
    })
    assert workflow.state["result_statuses"]["service_timeline"] == "stale"
    assert {name: workflow.state["result_statuses"][name] for name in ("grid", "routes", "coverage", "cns_gap")} == {name: "passed" for name in ("grid", "routes", "coverage", "cns_gap")}
