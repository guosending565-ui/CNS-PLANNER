from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.encounter_3d import EncounterAssessment3DV1, DAAEventStateMachineV1
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.api.router import ApiRouter
from cns_planner.domain.encounter_3d import normalize_encounter_track, normalize_encounter_policy


DEFAULTS = Path("cns_planner/config/defaults.json")


def track(track_id, role, samples):
    return normalize_encounter_track({
        "track_id": track_id, "role": role, "samples": samples,
        "source": "test", "confirmed": True,
    })


def crossing_tracks(vertical=0.0, time_shift=0.0):
    own = track("OWN", "ownship", [
        {"time_s": 0, "lon": -0.001, "lat": 0, "altitude_egm2008_m": 100},
        {"time_s": 20, "lon": 0.001, "lat": 0, "altitude_egm2008_m": 100},
    ])
    intruder = track("INT", "intruder", [
        {"time_s": time_shift, "lon": 0, "lat": -0.001, "altitude_egm2008_m": 100 + vertical},
        {"time_s": 20 + time_shift, "lon": 0, "lat": 0.001, "altitude_egm2008_m": 100 + vertical},
    ])
    return own, intruder


def policy(confirmed=True, horizontal=30, vertical=20):
    return normalize_encounter_policy({
        "policy_id": "P", "horizontal_threshold_m": horizontal,
        "vertical_threshold_m": vertical, "lookahead_s": 30,
        "advisory_lead_time_s": 4, "warning_lead_time_s": 2,
        "source": "explicit engineering test", "confirmed": confirmed,
    })


def protection(value=.1, status="passed"):
    names = ("detect", "track", "processing", "decision", "communication", "aircraft_reaction")
    return {
        "status": status, "t_pre_s": value * len(names),
        "input_fingerprint": "protect", "components": {
            name: {"value_s": value, "confirmed": True} for name in names
        },
    }


def timeline(c="available", n="available", s="available", c_intervals=None):
    def subsystem(code, state, intervals=None):
        return {"subsystem": code, "intervals": intervals or [{
            "start_time_s": 0, "end_time_s": 30, "service_state": state,
        }]}
    return {"status": "passed", "input_fingerprint": "timeline", "routes": [{
        "route_id": "R1", "subsystems": [
            subsystem("C", c, c_intervals), subsystem("N", n), subsystem("S", s),
        ],
    }]}


def timing(own=None, intruder=None, encounter_policy=None, capability=None, command=None):
    own, intruder = (own, intruder) if own and intruder else crossing_tracks()
    encounter_policy = encounter_policy or policy()
    capability = capability or {
        "capability_id": "CAP", "max_turn_rate_deg_s": 90,
        "max_horizontal_accel_mps2": 10, "max_vertical_speed_mps": 30,
        "response_delay_s": .1, "position_accuracy_m": 2,
        "control_tracking_error_m": 2, "source": "test", "confirmed": True,
        "status": "confirmed", "wind_limit_mps": None,
    }
    command = command or {
        "command_id": "CMD", "ownship_track_id": "OWN", "issued_time_s": 6.3,
        "duration_s": 3, "turn_rate_deg_s": 0, "horizontal_accel_mps2": 0,
        "vertical_speed_mps": 25, "source": "manual test", "confirmed": True,
        "status": "confirmed",
    }
    return {
        "encounter_tracks": {own["track_id"]: own, intruder["track_id"]: intruder},
        "encounter_policies": {encounter_policy["policy_id"]: encounter_policy},
        "maneuver_capability_profiles": {"CAP": capability} if capability else {},
        "maneuver_commands": {"CMD": command} if command else {},
        "encounter_lab": {
            "ownship_track_id": "OWN", "intruder_track_id": "INT", "policy_id": "P",
            "capability_id": "CAP", "command_id": "CMD", "service_route_id": "R1",
            "budget_id": "B",
        },
    }


def evaluate(**kwargs):
    configured = timing(
        kwargs.pop("own", None), kwargs.pop("intruder", None),
        kwargs.pop("encounter_policy", None), kwargs.pop("capability", None),
        kwargs.pop("command", None),
    )
    return EncounterAssessment3DV1().evaluate(
        configured, kwargs.pop("service", timeline()), kwargs.pop("budget", protection()),
    )


def test_3d_crossing_cpa_threshold_and_normal_state_progression():
    result = evaluate()
    geometry = result["geometry"]
    assert geometry["engineering_predicted_conflict"] is True
    assert geometry["horizontal_cpa_m"] == pytest.approx(0, abs=.01)
    assert geometry["vertical_separation_at_cpa_m"] == pytest.approx(0)
    assert geometry["slant_cpa_m"] == pytest.approx(0, abs=.01)
    assert geometry["time_to_horizontal_cpa_s"] == pytest.approx(10, abs=.05)
    states = [item["to_state"] for item in result["state_machine"]["transitions"]]
    assert states == ["DETECTED", "TRACKED", "PREDICTED_CONFLICT", "WARNING", "ACTION_REQUIRED", "COMMAND_SENT", "EXECUTING", "CLEARED"]
    assert [item["component"] for item in result["budget_timeline"]] == ["detect", "track", "processing", "decision", "communication", "aircraft_reaction"]
    assert all(item["actual_elapsed_s"] is not None for item in result["budget_timeline"])
    assert result["regulatory_well_clear"] == "not_evaluated"
    assert result["semantics"]["hazard_zone"] == "not_created"


def test_horizontal_close_vertical_safe_and_vertical_close_horizontal_safe():
    high, intruder = crossing_tracks(vertical=100)
    vertical_safe = evaluate(own=high, intruder=intruder)
    assert vertical_safe["geometry"]["horizontal_cpa_m"] < 1
    assert vertical_safe["geometry"]["engineering_predicted_conflict"] is False
    own = track("OWN", "ownship", [{"time_s": 0, "lon": 0, "lat": 0, "altitude_egm2008_m": 100}, {"time_s": 20, "lon": .001, "lat": 0, "altitude_egm2008_m": 100}])
    far = track("INT", "intruder", [{"time_s": 0, "lon": 0, "lat": .01, "altitude_egm2008_m": 105}, {"time_s": 20, "lon": .001, "lat": .01, "altitude_egm2008_m": 105}])
    horizontal_safe = evaluate(own=own, intruder=far)
    assert horizontal_safe["geometry"]["vertical_separation_at_cpa_m"] == pytest.approx(5)
    assert horizontal_safe["geometry"]["engineering_predicted_conflict"] is False


def test_no_overlap_missing_altitude_pending_policy_and_static_tracks():
    own, intruder = crossing_tracks(time_shift=30)
    assert evaluate(own=own, intruder=intruder)["status"] == "no_time_overlap"
    unresolved = normalize_encounter_track({"track_id": "X", "role": "ownship", "confirmed": True, "samples": [
        {"time_s": 0, "lon": 0, "lat": 0, "altitude_egm2008_m": 100},
        {"time_s": 1, "lon": 0, "lat": 0, "altitude_m": 100, "vertical_reference": "unknown"},
    ]})
    assert unresolved["status"] == "unresolved"
    own, intruder = crossing_tracks()
    pending = evaluate(own=own, intruder=intruder, encounter_policy=policy(False))
    assert pending["status"] == "pending_confirmation"
    static_own = track("OWN", "ownship", [{"time_s": 0, "lon": 0, "lat": 0, "altitude_egm2008_m": 100}, {"time_s": 10, "lon": 0, "lat": 0, "altitude_egm2008_m": 100}])
    static_int = track("INT", "intruder", [{"time_s": 0, "lon": .01, "lat": 0, "altitude_egm2008_m": 100}, {"time_s": 10, "lon": .01, "lat": 0, "altitude_egm2008_m": 100}])
    assert evaluate(own=static_own, intruder=static_int)["geometry"]["horizontal_cpa_m"] > 1000


def test_cns_gates_are_distinct_from_safety_events():
    lost_s = evaluate(service=timeline(s="lost"))
    assert lost_s["state_machine"]["current_state"] == "LOST_TRACK"
    assert lost_s["state_machine"]["safety_event"] == "not_evaluated"
    lost_c = evaluate(service=timeline(c="lost"))
    assert lost_c["state_machine"]["current_state"] == "ALERT_DELIVERY_FAILED"
    c_intervals = [
        {"start_time_s": 0, "end_time_s": 6.2, "service_state": "available"},
        {"start_time_s": 6.2, "end_time_s": 30, "service_state": "lost"},
    ]
    command_failed = evaluate(service=timeline(c_intervals=c_intervals))
    assert command_failed["state_machine"]["current_state"] == "COMMAND_UNAVAILABLE"
    degraded_n = evaluate(service=timeline(n="available_degraded"))
    assert degraded_n["cns_gating"]["ownship_state_confidence"] == "degraded"
    assert degraded_n["state_machine"]["current_state"] == "CLEARED"


def test_missing_capability_and_protection_timeout_are_unresolved():
    configured = timing()
    configured["maneuver_capability_profiles"] = {}
    missing = EncounterAssessment3DV1().evaluate(configured, timeline(), protection())
    assert missing["state_machine"]["current_state"] == "MANEUVER_UNRESOLVED"
    timed_out = evaluate(budget=protection(value=2))
    assert timed_out["state_machine"]["current_state"] == "MANEUVER_UNRESOLVED"
    assert "deadline" in timed_out["state_machine"]["transitions"][-1]["reason"]


def test_state_machine_rejects_skips_and_nonmonotonic_time():
    machine = DAAEventStateMachineV1()
    with pytest.raises(ValueError, match="非法 DAA transition"):
        machine.transition("WARNING", 0, "skip")
    machine.transition("DETECTED", 2, "ok")
    with pytest.raises(ValueError, match="单调不减"):
        machine.transition("TRACKED", 1, "time reversal")


class DirectQgis:
    @staticmethod
    def call(callback): return callback()


class ApiContext:
    def __init__(self, workflow):
        self.workflow, self.qgis, self.data = workflow, DirectQgis(), object()


def test_persistence_api_stale_and_old_project_backfill(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    configured = timing()
    workflow.set_operational_timing(configured)
    workflow.state["service_timeline"] = timeline()
    workflow.state["protection_envelope"] = protection()
    router = ApiRouter(ApiContext(workflow))
    response = router.post("/api/encounter-3d/evaluate", {}).data
    assert response["encounter_3d_assessment"]["input_fingerprint"]
    assert router.get("/api/encounter-3d", {}, {}).data["regulatory_well_clear"] == "not_evaluated"
    changed = deepcopy(configured)
    changed["encounter_policies"]["P"]["horizontal_threshold_m"] = 31
    workflow.set_operational_timing(changed)
    assert workflow.state["encounter_3d_assessment"]["status"] == "stale"
    workflow.state["encounter_3d_assessment"] = response["encounter_3d_assessment"]
    workflow.invalidation_service.service_timeline()
    assert workflow.state["encounter_3d_assessment"]["stale_reason"] == "service_timeline_changed"
    workflow.state["encounter_3d_assessment"] = response["encounter_3d_assessment"]
    workflow.invalidation_service.protection_envelope()
    assert workflow.state["encounter_3d_assessment"]["stale_reason"] == "protection_envelope_changed"
    workflow.state["encounter_3d_assessment"] = response["encounter_3d_assessment"]
    changed_again = deepcopy(changed)
    changed_again["maneuver_commands"]["CMD"]["vertical_speed_mps"] = 24
    workflow.set_operational_timing(changed_again)
    assert workflow.state["encounter_3d_assessment"]["stale_reason"] == "encounter_input_changed"
    legacy = blank_project(workflow.defaults)
    legacy.pop("encounter_3d_assessment")
    legacy["result_statuses"].pop("encounter_3d_assessment")
    restored = normalize_project(legacy, workflow.grid_service)
    assert restored["encounter_3d_assessment"]["status"] == "not_calculated"
    assert restored["result_statuses"]["encounter_3d_assessment"] == "not_calculated"
