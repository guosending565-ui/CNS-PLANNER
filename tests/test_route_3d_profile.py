"""Production Route3DProfile V1 tests.

Covers the additive distance-parameterised 3D profile contract, its readiness / geometry /
diagnostics semantics, the effective-vertical priority (profile > constant H > legacy), the
"stale profile never silently falls back to constant H" boundary, the Coverage3D linkage, the
Route Safety Evidence V2 terminal-transition boundary, the persistence/API surface and the
explicit "Theta* core is untouched" invariant.

Nothing here asserts a safe/unsafe verdict: Route3DProfile V1 is a thin derivation layer, not
a 3D planner and not a kinematic validation.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.geometric_3d import (
    GeometricCoverage3DV1, path_length_m, route_profile_height,
)
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.route_3d_profile import normalize_route_3d_profiles
from cns_planner.domain.spatial_3d import effective_route_vertical_context


DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE_ID = "R0001"
LAYER_ID = "L-LOW"
CRUISE_M = 100.0
TERMINAL_DEPARTURE_M = 20.0
TERMINAL_ARRIVAL_M = 40.0

#: The path is the distance basis: ``L`` is the true geometric length (which is also what the
#: existing Coverage3D / RouteVerticalProfile consumers parameterise), so the derivation stays
#: consistent with its consumers by construction.
PATH = [[122.0, 30.0], [122.02, 30.0]]
LENGTH_M = path_length_m(PATH)
JOIN_M = LENGTH_M / 5.0
LEAVE_M = LENGTH_M * 4.0 / 5.0

ROUTE = {
    "route_id": ROUTE_ID, "status": "passed", "distance_m": LENGTH_M,
    "path": deepcopy(PATH),
}


def workflow(tmp_path, *, routes=True):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    if routes:
        service.state["operational_routes"] = [deepcopy(ROUTE)]
        service.state["result_statuses"]["routes"] = "passed"
    return service


def layer(**overrides):
    payload = {
        "altitude_layer_id": LAYER_ID, "name": "低层",
        "nominal_altitude_m": CRUISE_M, "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True,
    }
    payload.update(overrides)
    return payload


def assignment(**overrides):
    payload = {
        "route_id": ROUTE_ID, "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    }
    payload.update(overrides)
    return payload


def procedure_payload(kind, **overrides):
    if kind == "departure":
        payload = {
            "procedure_id": "DP-1", "procedure_type": "departure", "route_id": ROUTE_ID,
            "site_reference": "REF-1", "altitude_layer_id": LAYER_ID,
            "transition_mode": "climb_to_cruise_layer",
            "horizontal_geometry": {"turn_radius_m": 50.0},
            "vertical_profile": {
                "climb_rate_mps": 2.5,
                "terminal_altitude_egm2008_m": TERMINAL_DEPARTURE_M,
                "terminal_altitude_source": "manual_engineering_review",
                "terminal_altitude_evidence": "评审记录 DP-1",
            },
            "join_leave_point": {"kind": "join", "distance_along_route_m": JOIN_M},
            "source": "工程确认-测试", "confirmed": True,
        }
    else:
        payload = {
            "procedure_id": "AR-1", "procedure_type": "arrival", "route_id": ROUTE_ID,
            "site_reference": "REF-2", "altitude_layer_id": LAYER_ID,
            "transition_mode": "descend_from_cruise_layer",
            "horizontal_geometry": {"turn_radius_m": 60.0},
            "vertical_profile": {
                "descent_rate_mps": 2.0,
                "terminal_altitude_egm2008_m": TERMINAL_ARRIVAL_M,
                "terminal_altitude_source": "verified_site_survey",
                "terminal_altitude_evidence": "survey AR-1",
            },
            "join_leave_point": {"kind": "leave", "distance_along_route_m": LEAVE_M},
            "source": "工程确认-测试", "confirmed": True,
        }
    payload.update(overrides)
    return payload


def adoption(**overrides):
    payload = {
        "adoption_id": "LRA-1", "route_id": ROUTE_ID, "status": "published",
        "current_applicability": "current", "validation_id": "LRV-1",
        "validation_fingerprint": "validation-fp", "candidate_id": "LC-1",
        "candidate_fingerprint": "candidate-fp", "projection_fingerprint": "projection-fp",
        "altitude_layer_id": LAYER_ID,
    }
    payload.update(overrides)
    return payload


def ready_service(tmp_path, *, with_adoption=True):
    """A service whose complete explicit input set satisfies every readiness item."""

    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    service.set_departure_arrival_procedure(procedure_payload("departure"))
    service.set_departure_arrival_procedure(procedure_payload("arrival"))
    if with_adoption:
        service.state["layered_operational_adoptions"] = {
            "schema_version": "layered-operational-adoptions",
            "status": "passed", "count": 1, "items": [adoption()],
        }
    return service


def stored_profile(service, route_id=ROUTE_ID):
    profiles = service.state["spatial_3d"]["route_3d_profiles"]
    return next(item for item in profiles.values() if item["route_id"] == route_id)


# --------------------------------------------------------------------------------------
# 1. contract + normal four-point geometry
# --------------------------------------------------------------------------------------


def test_ready_route_derives_the_four_point_climb_cruise_descent_profile(tmp_path):
    service = ready_service(tmp_path)
    result = service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "passed"
    assert profile["current_applicability"] == "current"
    assert profile["waypoints"] == [
        {"distance_along_route_m": 0.0, "altitude_m": TERMINAL_DEPARTURE_M},
        {"distance_along_route_m": JOIN_M, "altitude_m": CRUISE_M},
        {"distance_along_route_m": LEAVE_M, "altitude_m": CRUISE_M},
        {"distance_along_route_m": LENGTH_M, "altitude_m": TERMINAL_ARRIVAL_M},
    ]
    assert [item["phase_id"] for item in profile["phases"]] == [
        "takeoff_event", "climb", "cruise", "descent", "landing_event",
    ]
    lengths = profile["phase_lengths_m"]
    assert lengths == {"climb": JOIN_M, "cruise": LEAVE_M - JOIN_M, "descent": LENGTH_M - LEAVE_M}
    assert result["route_3d_profiles"]["count"] == 1
    assert result["route_3d_profiles"]["items"][0]["status"] == "passed"


def test_profile_carries_the_full_declared_contract(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    for key in (
        "profile_id", "route_id", "adoption_id", "altitude_layer_id",
        "departure_procedure_id", "arrival_procedure_id", "status",
        "current_applicability", "vertical_reference", "route_length_m", "waypoints",
        "phases", "fingerprints", "provenance", "limitations",
    ):
        assert key in profile, key
    assert profile["vertical_reference"] == "egm2008_orthometric"
    assert profile["adoption_id"] == "LRA-1"
    assert profile["altitude_layer_id"] == LAYER_ID
    assert profile["departure_procedure_id"] == "DP-1"
    assert profile["arrival_procedure_id"] == "AR-1"
    assert profile["route_length_m"] == LENGTH_M
    assert profile["fingerprints"]["profile_fingerprint"]
    assert all(
        {"distance_along_route_m", "altitude_m"} <= set(item) for item in profile["waypoints"]
    )


def test_normalize_route_3d_profiles_is_idempotent(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    once = normalize_route_3d_profiles(service.state["spatial_3d"]["route_3d_profiles"])
    twice = normalize_route_3d_profiles(once)
    assert once == twice


# --------------------------------------------------------------------------------------
# 2. join / leave boundaries and overlap
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("join,leave,reason_fragment", [
    (900.0, 100.0, "transition overlap"),
    (-1.0, 800.0, "不得为负"),
    (200.0, LENGTH_M + 1.0, "越界"),
])
def test_invalid_join_leave_geometry_is_unresolved_and_never_repaired(
    tmp_path, join, leave, reason_fragment,
):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", join_leave_point={"kind": "join", "distance_along_route_m": join})
    )
    service.set_departure_arrival_procedure(
        procedure_payload("arrival", join_leave_point={"kind": "leave", "distance_along_route_m": leave})
    )
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "unresolved"
    assert profile["current_applicability"] == "unresolved"
    assert profile["waypoints"] == []
    # The explicit user input is preserved verbatim — never clamped, swapped or repaired.
    assert profile["join_leave"]["join_distance_along_route_m"] == join
    assert profile["join_leave"]["leave_distance_along_route_m"] == leave
    assert any(reason_fragment in reason for reason in profile["reasons"]), profile["reasons"]


def test_degenerate_join_equals_leave_is_a_zero_length_cruise_not_an_error(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", join_leave_point={"kind": "join", "distance_along_route_m": 500.0})
    )
    service.set_departure_arrival_procedure(
        procedure_payload("arrival", join_leave_point={"kind": "leave", "distance_along_route_m": 500.0})
    )
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "passed"
    assert profile["phase_lengths_m"]["cruise"] == 0.0
    assert profile["waypoints"] == [
        {"distance_along_route_m": 0.0, "altitude_m": TERMINAL_DEPARTURE_M},
        {"distance_along_route_m": 500.0, "altitude_m": CRUISE_M},
        {"distance_along_route_m": LENGTH_M, "altitude_m": TERMINAL_ARRIVAL_M},
    ]
    # Sampling is still consistent at the collapsed station.
    assert route_profile_height(profile, 0.0, LENGTH_M) == TERMINAL_DEPARTURE_M
    assert route_profile_height(profile, 500.0, LENGTH_M) == CRUISE_M


def test_shared_station_endpoints_are_kept_and_the_phase_lengths_add_up(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "passed"
    phases = {item["phase_id"]: item for item in profile["phases"]}
    # takeoff_event shares the 0 m station with the start of climb; the shared station is kept
    # (it is the endpoint event, not a fabricated segment) and no anchor is duplicated.
    assert phases["takeoff_event"]["start_distance_along_route_m"] == 0.0
    assert phases["takeoff_event"]["end_distance_along_route_m"] == 0.0
    assert phases["takeoff_event"]["horizontal_length_m"] == 0.0
    assert phases["climb"]["start_distance_along_route_m"] == 0.0
    assert phases["climb"]["start_altitude_m"] == TERMINAL_DEPARTURE_M
    assert phases["landing_event"]["end_distance_along_route_m"] == LENGTH_M
    assert len({(item["distance_along_route_m"], item["altitude_m"]) for item in profile["waypoints"]}) == len(
        profile["waypoints"]
    )
    non_event = sum(
        item["horizontal_length_m"] for item in profile["phases"]
        if not item["phase_id"].endswith("event")
    )
    assert non_event == pytest.approx(LENGTH_M)


def test_fully_identical_anchor_points_are_removed(tmp_path):
    service = ready_service(tmp_path)
    # z0 == H and s_join == 0 makes (0, z0) and (s_join, H) fully identical: exactly one is kept.
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={
            "climb_rate_mps": 2.5,
            "terminal_altitude_egm2008_m": CRUISE_M,
            "terminal_altitude_source": "manual_engineering_review",
            "terminal_altitude_evidence": "评审记录 DP-1",
        }, join_leave_point={"kind": "join", "distance_along_route_m": 0.0})
    )
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "passed"
    assert profile["waypoints"] == [
        {"distance_along_route_m": 0.0, "altitude_m": CRUISE_M},
        {"distance_along_route_m": LEAVE_M, "altitude_m": CRUISE_M},
        {"distance_along_route_m": LENGTH_M, "altitude_m": TERMINAL_ARRIVAL_M},
    ]


# --------------------------------------------------------------------------------------
# 3. fail-closed readiness: terminal altitude, no FABDEM default
# --------------------------------------------------------------------------------------


def test_missing_terminal_altitude_fails_closed_without_touching_the_input(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={"climb_rate_mps": 2.5})
    )
    readiness = service.route_3d_profile_readiness()
    entry = next(item for item in readiness["routes"] if item["route_id"] == ROUTE_ID)
    assert readiness["status"] == "not_ready"
    assert entry["ready"] is False
    assert any("terminal_altitude_egm2008_m" in blocker for blocker in entry["blockers"])
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "not_ready"
    assert profile["waypoints"] == []
    # The procedure itself is untouched: no value was written back.
    procedure = next(
        item for item in service.state["spatial_3d"]["departure_arrival_procedures"]
        if item["procedure_id"] == "DP-1"
    )
    assert "terminal_altitude_egm2008_m" not in procedure["vertical_profile"]


def test_terminal_altitude_without_source_or_evidence_is_not_admissible(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={
            "climb_rate_mps": 2.5, "terminal_altitude_egm2008_m": 20.0,
        })
    )
    readiness = service.route_3d_profile_readiness()
    blockers = "；".join(readiness["blockers"])
    assert "缺少显式 source" in blockers
    assert "缺少 evidence" in blockers


def test_terminal_altitude_with_a_source_but_no_evidence_is_not_admissible(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={
            "climb_rate_mps": 2.5, "terminal_altitude_egm2008_m": 20.0,
            "terminal_altitude_source": "manual_engineering_review",
        })
    )
    readiness = service.route_3d_profile_readiness()
    blockers = "；".join(readiness["blockers"])
    assert "缺少 evidence" in blockers
    assert "缺少显式 source" not in blockers
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert stored_profile(service)["status"] == "not_ready"


@pytest.mark.parametrize("source,evidence", [
    ("fabdem_v12_dtm", "sampled_at_landing_site"),
    ("dtm", "terrain_pixel"),
    ("landing_platform", "surface_elevation"),
])
def test_a_terrain_derived_platform_height_is_never_a_terminal_altitude(
    tmp_path, source, evidence,
):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={
            "climb_rate_mps": 2.5, "terminal_altitude_egm2008_m": 20.0,
            "terminal_altitude_source": source, "terminal_altitude_evidence": evidence,
        })
    )
    readiness = service.route_3d_profile_readiness()
    assert readiness["status"] == "not_ready"
    assert any("地形/起降平台" in blocker for blocker in readiness["blockers"])
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert stored_profile(service)["status"] == "not_ready"


def test_every_missing_input_is_reported_individually(tmp_path):
    service = workflow(tmp_path)
    readiness = service.route_3d_profile_readiness()
    entry = readiness["routes"][0]
    assert entry["ready"] is False
    joined = "；".join(entry["blockers"])
    for fragment in (
        "RouteOperatingLayer", "缺少 AltitudeLayer H", "adoption", "departure procedure",
        "arrival procedure",
    ):
        assert fragment in joined, fragment


def test_non_passed_route_is_not_ready(tmp_path):
    service = ready_service(tmp_path)
    service.state["operational_routes"][0]["status"] = "stale"
    readiness = service.route_3d_profile_readiness()
    assert readiness["status"] == "not_ready"
    assert any("passed" in blocker for blocker in readiness["blockers"])


# --------------------------------------------------------------------------------------
# 4. join/leave distance is explicit and never derived from cruise speed
# --------------------------------------------------------------------------------------


def test_join_distance_is_never_derived_from_cruise_speed(tmp_path):
    service = ready_service(tmp_path)
    # An explicit cruise_speed is present as unrelated performance evidence, and the explicit
    # join_leave_point is deliberately absent.  The derivation must fail closed — it must NOT
    # compute 800 m / 20 m/s = 40 s × 20 m/s and silently accept it.
    service.set_departure_arrival_procedure(procedure_payload("departure", vertical_profile={
        "climb_rate_mps": 2.5, "cruise_speed_mps": 20.0,
        "terminal_altitude_egm2008_m": TERMINAL_DEPARTURE_M,
        "terminal_altitude_source": "manual_engineering_review",
        "terminal_altitude_evidence": "评审记录 DP-1",
    }, join_leave_point=None))
    readiness = service.route_3d_profile_readiness()
    assert readiness["status"] == "not_ready"
    assert any("cruise_speed" in blocker for blocker in readiness["blockers"])
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "not_ready"
    assert profile["join_leave"] == {}
    assert "cruise_speed" not in profile.get("diagnostics", {})


def test_join_leave_kind_must_match_the_procedure_type(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", join_leave_point={"kind": "leave", "distance_along_route_m": JOIN_M})
    )
    assert service.route_3d_profile_readiness()["status"] == "not_ready"


# --------------------------------------------------------------------------------------
# 5. diagnostics, value by value
# --------------------------------------------------------------------------------------


def test_transition_diagnostics_are_exact_and_explicitly_bounded(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    diagnostics = stored_profile(service)["diagnostics"]
    assert diagnostics["policy"] == "diagnostic_not_aircraft_kinematic_validation"
    assert diagnostics["not_aircraft_kinematic_validation"] is True
    assert diagnostics["evaluates_real_flight_feasibility"] is False
    assert diagnostics["join_leave_distance_is_not_derived_from_cruise_speed"] is True
    assert diagnostics["rate_source"] == "procedure_vertical_profile_explicit_performance_evidence"
    climb = diagnostics["climb"]
    assert climb["altitude_delta_m"] == CRUISE_M - TERMINAL_DEPARTURE_M
    assert climb["transition_horizontal_distance_m"] == JOIN_M
    assert climb["rate_mps"] == 2.5
    assert climb["duration_s"] == pytest.approx((CRUISE_M - TERMINAL_DEPARTURE_M) / 2.5)
    assert climb["implied_horizontal_speed_mps"] == pytest.approx(JOIN_M / climb["duration_s"])
    assert climb["boundary"] == "diagnostic_not_aircraft_kinematic_validation"
    descent = diagnostics["descent"]
    assert descent["altitude_delta_m"] == TERMINAL_ARRIVAL_M - CRUISE_M
    assert descent["transition_horizontal_distance_m"] == LENGTH_M - LEAVE_M
    assert descent["rate_mps"] == 2.0
    assert descent["duration_s"] == pytest.approx(abs(TERMINAL_ARRIVAL_M - CRUISE_M) / 2.0)
    assert descent["implied_horizontal_speed_mps"] == pytest.approx(
        (LENGTH_M - LEAVE_M) / descent["duration_s"]
    )


def test_missing_rate_fails_closed_and_emits_no_diagnostic_value(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(procedure_payload("departure", vertical_profile={
        "terminal_altitude_egm2008_m": TERMINAL_DEPARTURE_M,
        "terminal_altitude_source": "manual_engineering_review",
        "terminal_altitude_evidence": "评审记录 DP-1",
    }))
    # ``climb_rate_mps`` is explicit performance evidence: without it the departure procedure
    # stays pending, the profile stays not_ready and **no** diagnostic number is emitted.
    procedure = next(
        item for item in service.state["spatial_3d"]["departure_arrival_procedures"]
        if item["procedure_id"] == "DP-1"
    )
    assert procedure["status"] == "pending_confirmation"
    assert "climb_rate_mps" in procedure["missing_evidence"]
    readiness = service.route_3d_profile_readiness()
    assert readiness["status"] == "not_ready"
    assert any("climb_rate_mps" in blocker for blocker in readiness["blockers"])
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "not_ready"
    assert profile["waypoints"] == []
    assert profile["diagnostics"]["climb"] is None
    assert profile["diagnostics"]["descent"] is None


def test_zero_rate_is_not_admissible_performance_evidence(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", vertical_profile={
            "climb_rate_mps": 0.0,
            "terminal_altitude_egm2008_m": TERMINAL_DEPARTURE_M,
            "terminal_altitude_source": "manual_engineering_review",
            "terminal_altitude_evidence": "评审记录 DP-1",
        })
    )
    assert service.route_3d_profile_readiness()["status"] == "not_ready"


# --------------------------------------------------------------------------------------
# 6. effective vertical priority + Coverage3D linkage
# --------------------------------------------------------------------------------------


def test_current_profile_has_priority_and_is_consumed_by_coverage_3d(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    context = effective_route_vertical_context(service.state["spatial_3d"], ROUTE_ID)
    assert context["mode"] == "waypoint_linear"
    assert context["source_kind"] == "production_route_3d_profile"
    assert context["constant_altitude_m"] is None
    assert [item["altitude_m"] for item in context["waypoints"]] == [
        TERMINAL_DEPARTURE_M, CRUISE_M, CRUISE_M, TERMINAL_ARRIVAL_M,
    ]
    # The existing sampling helper consumes the profile with no change of its own.
    assert route_profile_height(context, 0.0, LENGTH_M) == TERMINAL_DEPARTURE_M
    assert route_profile_height(context, JOIN_M / 2.0, LENGTH_M) == pytest.approx(60.0)
    assert route_profile_height(context, LENGTH_M / 2.0, LENGTH_M) == CRUISE_M
    assert route_profile_height(context, LENGTH_M, LENGTH_M) == TERMINAL_ARRIVAL_M

    # Coverage3D reads the same context: the sampled altitude is the 3D profile, not a fixed H.
    model = GeometricCoverage3DV1({"sample_spacing_m": LENGTH_M / 5.0, "confirmed": True})
    result = model.evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"], {"cells": []},
        {"terrain": {"cells": {}}}, {"items": []}, {"items": []},
    )
    route = result["routes"][0]
    assert route["altitude_profile"]["source_kind"] == "production_route_3d_profile"
    assert route["altitude_profile"]["mode"] == "waypoint_linear"
    altitudes = [item["altitude_egm2008_m"] for item in route["samples"]]
    assert altitudes[0] == TERMINAL_DEPARTURE_M
    assert altitudes[-1] == TERMINAL_ARRIVAL_M
    assert max(altitudes) == CRUISE_M
    assert "altitude_profile" in route


def test_route_vertical_profile_consumes_the_3d_profile_geometry(tmp_path):
    """The existing RouteVerticalProfile derives its flight altitude from the 3D profile."""

    from cns_planner.algorithms.route_vertical_profile import RouteVerticalProfileV1

    class Sampler:
        def sample_wgs84(self, coordinate):
            return {"status": "passed", "ground_egm2008_m": 5.0, "reason": "test"}

        def describe(self):
            return {"dataset": "FABDEM", "vertical_reference": "egm2008_orthometric",
                    "mtime_ns": "a"}

    service = ready_service(tmp_path)
    parameters = {"target_spacing_m": LENGTH_M / 4.0, "max_samples": 6}
    # Baseline: with no Route3DProfile the constant cruise altitude H is used everywhere.
    before = RouteVerticalProfileV1(parameters).evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"],
        {"status": "not_calculated", "routes": []}, Sampler(),
    )
    assert {item["flight_egm2008_m"] for item in before["profiles"][0]["samples"]} == {CRUISE_M}

    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    after = RouteVerticalProfileV1(parameters).evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"],
        {"status": "not_calculated", "routes": []}, Sampler(),
    )
    altitudes = [item["flight_egm2008_m"] for item in after["profiles"][0]["samples"]]
    assert altitudes[0] == TERMINAL_DEPARTURE_M
    assert altitudes[-1] == TERMINAL_ARRIVAL_M
    assert max(altitudes) == CRUISE_M
    assert altitudes != [CRUISE_M] * len(altitudes)

    # Deleting the profile returns to the unchanged constant-H behaviour, which proves the
    # values above came from the profile rather than from the altitude layer.
    service.delete_route_3d_profile({"route_id": ROUTE_ID})
    restored = RouteVerticalProfileV1(parameters).evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"],
        {"status": "not_calculated", "routes": []}, Sampler(),
    )
    assert {item["flight_egm2008_m"] for item in restored["profiles"][0]["samples"]} == {CRUISE_M}


def test_without_a_profile_the_constant_cruise_behaviour_is_unchanged(tmp_path):
    service = ready_service(tmp_path)
    context = effective_route_vertical_context(service.state["spatial_3d"], ROUTE_ID)
    assert context["mode"] == "constant"
    assert context["source_kind"] == "production_fixed_cruise_layer"
    assert context["constant_altitude_m"] == CRUISE_M
    assert context["fallback_profile_used"] is False


def test_legacy_profile_is_only_used_when_no_production_assignment_exists():
    spatial = {
        "route_altitude_profiles": {ROUTE_ID: {
            "route_id": ROUTE_ID, "mode": "waypoint_linear",
            "vertical_reference": "egm2008_orthometric",
            "waypoints": [
                {"distance_along_route_m": 0.0, "altitude_m": 30.0},
                {"distance_along_route_m": 1000.0, "altitude_m": 30.0},
            ],
            "source": "v3c_validated_route", "confirmed": True, "status": "confirmed",
        }},
        "route_operating_layers": [], "altitude_layers": [], "route_3d_profiles": {},
    }
    context = effective_route_vertical_context(spatial, ROUTE_ID)
    assert context["source_kind"] == "legacy_or_v3_route_altitude_profile"
    assert context["fallback_profile_used"] is True


def test_stale_profile_never_silently_falls_back_to_constant_h(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    # An upstream change (the altitude layer source) stales the stored profile.
    service.set_altitude_layer(layer(source="工程确认-测试-v2"))
    profile = stored_profile(service)
    assert profile["status"] == "stale"
    context = effective_route_vertical_context(service.state["spatial_3d"], ROUTE_ID)
    assert context["status"] == "unresolved"
    assert context["silent_constant_h_fallback"] is False
    assert context["fallback_profile_used"] is False
    assert context["constant_altitude_m"] is None
    # The constant cruise layer is still configured — and deliberately not used.
    assert context["reason"] == "stored_production_route_3d_profile_not_current"
    assert context["waypoints"] == []


def test_coverage_3d_reports_missing_data_instead_of_a_fixed_h_track(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    service.set_altitude_layer(layer(source="工程确认-测试-v2"))
    model = GeometricCoverage3DV1({"sample_spacing_m": 500.0, "confirmed": True})
    result = model.evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"], {"cells": []},
        {"terrain": {"cells": {}}}, {"items": []}, {"items": []},
    )
    assert result["routes"][0]["status"] == "missing_data"
    assert result["status"] == "missing_data"


# --------------------------------------------------------------------------------------
# 7. validation boundary + Route Safety Evidence V2
# --------------------------------------------------------------------------------------


def test_profile_records_the_explicit_cruise_only_validation_boundary(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    boundary = stored_profile(service)["validation_boundary"]
    assert boundary["cruise_validation"] == "validated/current"
    assert boundary["cruise_validation_scope"] == "fixed_cruise_altitude_only"
    assert boundary["terminal_transition_validation"] == "not_evaluated"
    assert boundary["full_3d_geometry_validated"] is False
    assert boundary["never_rewrites_layered_route_validation"] is True
    assert boundary["produces_safe_or_unsafe_verdict"] is False
    assert boundary["next_stage"] == "vertical_transition_continuous_validation"
    assert boundary["next_stage_implemented"] is False


class DirectQgis:
    @staticmethod
    def call(callback):
        return callback()


class ApiContext:
    def __init__(self, workflow):
        self.workflow, self.qgis, self.data = workflow, DirectQgis(), object()


#: Minimal but *complete* published lineage, so the Safety Evidence V2 aggregation resolves its
#: references and the assessment is ``current`` rather than degraded to ``stale``.
def safety_lineage_records(geometry_status="validated_candidate"):
    candidate_record = {
        "candidate_id": "LC-1", "route_id": ROUTE_ID, "altitude_layer_id": LAYER_ID,
        "status": "candidate", "current_applicability": "current",
        "candidate_fingerprint": "candidate-fp", "path": deepcopy(PATH),
        "algorithm_id": "layered_risk_aware_theta_star_v2", "algorithm_version": "2.0",
        "planning_objective": {"total_cost": 1.0, "distance_m": LENGTH_M},
        "route_risk_density": {"value": 0.001, "threshold": 1.0, "status": "passed"},
        "search_parameters": {"heading_bin_count": 8},
        "provenance": {"regulatory_compliance": {"status": "not_evaluated"}},
    }
    validation_record = {
        "validation_id": "LRV-1", "status": geometry_status,
        "current_applicability": "current", "status_reason": None,
        "candidate": {"candidate_id": "LC-1", "route_id": ROUTE_ID,
                      "altitude_layer_id": LAYER_ID, "candidate_fingerprint": "candidate-fp"},
        "route": {"path": deepcopy(PATH), "nominal_altitude_m": CRUISE_M,
                  "vertical_reference": "egm2008_orthometric"},
        "domains": {"terrain": {"status": "passed", "minimum_margin": 40.0},
                    "building": {"status": "passed", "minimum_margin": 30.0}},
        "failed_intervals": [], "unresolved_intervals": [],
        "resource_limits": {"limit_reached": False},
        "fingerprints": {"validation_fingerprint": "validation-fp"},
        "source_type": "configured_real_sources",
    }
    risk_profile = {
        "profile_id": "RRP-1", "status": "passed", "current_applicability": "current",
        "candidate": {"candidate_id": "LC-1", "candidate_fingerprint": "candidate-fp"},
        "domains": {"ground": {
            "domain_id": "ground", "status": "passed", "exposure_index_m": 5.0,
            "resolved_length_m": LENGTH_M, "unresolved_length_m": 0.0, "coverage": 1.0,
        }},
        "policy": {"domains": {"ground": {"status": "not_configured"}}},
        "fingerprints": {"profile_fingerprint": "profile-fp"},
    }
    return candidate_record, validation_record, risk_profile


def install_safety_lineage(service, geometry_status="validated_candidate"):
    candidate_record, validation_record, risk_profile = safety_lineage_records(geometry_status)
    service.state["layered_route_candidates"] = {
        "schema_version": "layered-route-candidate-collection", "status": "passed",
        "count": 1, "active_candidate_id": "LC-1", "items": [candidate_record], "masks": {},
    }
    service.state["layered_route_validations"] = {
        "items": [validation_record], "count": 1, "status": "passed",
        "active_validation_id": "LRV-1",
    }
    service.state["route_risk_profiles"] = {
        "items": [risk_profile], "count": 1, "status": "passed",
        "active_profile_id": "RRP-1",
    }
    service.state["spatial_3d"]["route_altitude_profiles"][ROUTE_ID] = {
        "route_id": ROUTE_ID, "mode": "constant", "vertical_reference": "egm2008_orthometric",
        "constant_altitude_m": CRUISE_M, "source": "test", "confirmed": True,
        "status": "confirmed", "waypoints": [],
    }
    return validation_record


def validated_geometry_record():
    _, validation_record, _ = safety_lineage_records()
    return validation_record


def geometry_lineage(service, record):
    """The minimum lineage view ``_geometry_domain`` consumes (validation + route)."""

    return {
        "adoption": {"adoption_id": "LRA-1", "route_id": ROUTE_ID},
        "validation": record,
        "candidate": record.get("candidate") or {},
        "operational_route": {
            "route_id": ROUTE_ID, "status": "passed", "path": deepcopy(PATH),
        },
    }


def test_geometry_domain_reports_both_validations_and_downgrades_to_incomplete(tmp_path):
    from cns_planner.domain.route_safety_evidence_v2 import (
        DOMAIN_IDS, overall_status_for,
    )

    service = ready_service(tmp_path)
    record = validated_geometry_record()
    lineage = geometry_lineage(service, record)

    # Without a Route3DProfile the geometry domain keeps its original verdict untouched.
    baseline = service.route_safety_evidence_service._geometry_domain(lineage)
    assert baseline["status"] == "validated"
    assert baseline["transition"]["applies"] is False
    assert baseline["status_reason"] is None

    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    geometry = service.route_safety_evidence_service._geometry_domain(lineage)
    # Both verdicts are shown side by side: the unchanged cruise validation and the
    # not-evaluated terminal transition.
    assert geometry["status"] == "validation_incomplete"
    assert geometry["status_reason"] == "terminal_transition_validation_not_evaluated"
    assert geometry["evidence"]["validation_status"] == "validated_candidate"
    assert geometry["transition"]["cruise_validation"] == "validated/current"
    assert geometry["transition"]["terminal_transition_validation"] == "not_evaluated"
    assert geometry["transition"]["full_3d_geometry_validated"] is False
    assert geometry["transition"]["geometry_mode"] == "climb_cruise_descent_waypoint_linear"
    assert geometry["hard_constraint_failure"] is False
    assert geometry["semantics"]["cruise_validator_never_endorses_climb_or_descent"] is True
    assert geometry["semantics"]["produces_safe_or_unsafe_verdict"] is False

    # An unvalidated transition means the overall status is at least ``evidence_incomplete`` —
    # the existing vocabulary reports it as ``evidence_unresolved`` (a stronger statement).
    complete = {domain_id: "validated" for domain_id in DOMAIN_IDS}
    complete["ground_exposure"] = "assessed"
    complete["regulatory"] = "evaluated_no_confirmed_intersection"
    complete["cns_operational_support"] = "supported"
    assert overall_status_for(complete) == "evidence_complete"
    downgraded = {**complete, "geometry_obstacle": geometry["status"]}
    overall = overall_status_for(downgraded)
    assert overall in ("evidence_incomplete", "evidence_unresolved")
    assert overall != "evidence_complete"
    assert overall not in ("safe", "unsafe")
    unresolved = {**complete, "geometry_obstacle": "unresolved"}
    assert overall_status_for(unresolved) == "evidence_unresolved"
    not_ready = {**complete, "geometry_obstacle": "not_ready"}
    assert overall_status_for(not_ready) == "evidence_incomplete"


def test_geometry_domain_reports_an_unresolved_transition_for_a_stale_profile(tmp_path):
    service = ready_service(tmp_path)
    record = validated_geometry_record()
    lineage = geometry_lineage(service, record)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    # An upstream change makes the stored profile non-current: the geometry domain must reflect
    # the unresolved transition instead of silently keeping the fixed-H verdict.
    service.set_altitude_layer(layer(source="工程确认-测试-v3"))
    assert stored_profile(service)["status"] == "stale"
    geometry = service.route_safety_evidence_service._geometry_domain(lineage)
    assert geometry["transition"]["terminal_transition_validation"] == (
        "unresolved_profile_not_current"
    )
    assert geometry["transition"]["current_applicability"] == "stale"
    assert geometry["status"] == "unresolved"
    assert geometry["status_reason"] == "terminal_transition_validation_unresolved_profile"
    assert geometry["hard_constraint_failure"] is False


def test_geometry_domain_ignores_a_profile_of_another_route(tmp_path):
    service = ready_service(tmp_path)
    record = validated_geometry_record()
    lineage = geometry_lineage(service, record)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    service.state["spatial_3d"]["route_3d_profiles"][
        next(iter(service.state["spatial_3d"]["route_3d_profiles"]))
    ]["route_id"] = "R-OTHER"
    geometry = service.route_safety_evidence_service._geometry_domain(lineage)
    assert geometry["transition"]["applies"] is False
    assert geometry["status"] == "validated"


def test_safety_evidence_never_claims_current_or_safe_with_a_3d_profile(tmp_path):
    service = ready_service(tmp_path)
    install_safety_lineage(service)
    baseline = service.evaluate_route_safety_evidence_v2({"adoption_id": "LRA-1"})["items"][-1]
    assert baseline["domains"]["geometry_obstacle"]["transition"]["applies"] is False
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    record = service.evaluate_route_safety_evidence_v2({"adoption_id": "LRA-1"})["items"][-1]
    geometry = record["domains"]["geometry_obstacle"]
    profile = stored_profile(service)
    # The assessment fingerprint is bound to the Route3DProfile: introducing a profile changes
    # it, which is exactly what makes a previously stored assessment stale.
    assert record["fingerprints"]["assessment_fingerprint"] != (
        baseline["fingerprints"]["assessment_fingerprint"]
    )
    assert record["fingerprints"]["route_3d_profile_fingerprint"] == (
        profile["fingerprints"]["profile_fingerprint"]
    )
    assert record["fingerprints"]["route_3d_profile_id"] == profile["profile_id"]
    if geometry["transition"]["applies"]:
        assert geometry["transition"]["terminal_transition_validation"] in (
            "not_evaluated", "unresolved_profile_not_current",
        )
        assert geometry["transition"]["full_3d_geometry_validated"] is False
    assert record["status"] not in ("safe", "unsafe")
    assert record["evidence_summary"]["is_safety_certification"] is False
    assert record["evidence_summary"]["produces_safety_score"] is False
    assert record["evidence_summary"][
        "cns_gap_is_operational_support_deficit_not_route_unsafe"
    ] is True
    # The original LayeredRouteValidation record is never rewritten by the profile or by the
    # safety aggregation.
    stored_validation = service.state["layered_route_validations"]["items"][0]
    assert stored_validation["status"] == "validated_candidate"
    assert "terminal_transition_validation" not in stored_validation
    assert "transition" not in stored_validation


# --------------------------------------------------------------------------------------
# 8. invalidation / fingerprint
# --------------------------------------------------------------------------------------


def test_profile_fingerprint_binds_every_declared_dependency(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    first = stored_profile(service)["fingerprints"]["profile_fingerprint"]
    components = stored_profile(service)["fingerprints"]["components"]
    for key in (
        "operational_route", "operational_adoption", "route_operating_layer",
        "altitude_layer", "departure_procedure", "arrival_procedure",
    ):
        assert key in components, key
    departure = components["departure_procedure"]
    assert departure["terminal_altitude_source"] == "manual_engineering_review"
    assert departure["terminal_altitude_evidence"] == "评审记录 DP-1"
    assert departure["join_leave_distance_along_route_m"] == JOIN_M

    # A terminal-altitude evidence change alone changes the fingerprint.
    service.set_departure_arrival_procedure(procedure_payload("departure", vertical_profile={
        "climb_rate_mps": 2.5,
        "terminal_altitude_egm2008_m": TERMINAL_DEPARTURE_M,
        "terminal_altitude_source": "manual_engineering_review",
        "terminal_altitude_evidence": "评审记录 DP-1 修订版",
    }))
    second = service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert stored_profile(service)["fingerprints"]["profile_fingerprint"] != first
    assert second["route_3d_profiles"]["count"] == 1


def test_profile_changes_stale_only_downstream_never_the_theta_chain(tmp_path):
    service = ready_service(tmp_path)
    service.state["coverage_3d"] = {"status": "passed", "input_fingerprint": "coverage-fp"}
    service.state["result_statuses"]["coverage_3d"] = "passed"
    service.state["route_vertical_profiles"] = {"status": "passed", "input_fingerprint": "vp-fp"}
    service.state["result_statuses"]["route_vertical_profiles"] = "passed"
    service.state["layered_route_candidates"] = {
        "status": "passed", "count": 1,
        "items": [{
            "candidate_id": "LC-1", "route_id": ROUTE_ID, "status": "candidate",
            "current_applicability": "current", "candidate_fingerprint": "candidate-fp",
            "planning_objective": {"total_cost": 1.0, "distance_m": LENGTH_M},
        }],
    }
    service.state["layered_route_validations"] = {
        "status": "passed", "count": 1,
        "items": [{"validation_id": "LRV-1", "status": "validated_candidate",
                   "current_applicability": "current",
                   "fingerprints": {"validation_fingerprint": "validation-fp"}}],
    }
    before = deepcopy(service.state["layered_route_candidates"])
    before_objective = deepcopy(
        service.state["layered_route_candidates"]["items"][0]["planning_objective"]
    )
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert service.state["coverage_3d"]["status"] == "stale"
    assert service.state["route_vertical_profiles"]["status"] == "stale"
    # Theta* candidate / validation / adoption are never staled by a profile change: the
    # candidate fingerprint and its planning objective stay byte-identical.
    candidate = service.state["layered_route_candidates"]["items"][0]
    assert candidate["candidate_fingerprint"] == before["items"][0]["candidate_fingerprint"]
    assert candidate["planning_objective"] == before_objective
    assert candidate["status"] not in ("stale",)
    assert candidate["current_applicability"] == "current"
    assert service.state["layered_route_validations"]["items"][0]["status"] == "validated_candidate"
    assert service.state["layered_operational_adoptions"]["items"][0]["status"] == "published"
    assert service.state["result_statuses"]["layered_route_candidate"] != "stale"


def test_upstream_route_layer_and_procedure_changes_stale_the_profile(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    service.set_route_operating_layer(assignment(source="工程确认-测试-v2"))
    assert stored_profile(service)["status"] == "stale"
    assert service.state["result_statuses"]["route_3d_profiles"] == "stale"

    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert stored_profile(service)["status"] == "passed"
    service.set_departure_arrival_procedure(procedure_payload("arrival", source="工程确认-测试-v2"))
    assert stored_profile(service)["status"] == "stale"

    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    # A route geometry change (new geometry → invalidated routes) also stales the profile.
    service.state["operational_routes"][0]["path"] = [[122.0, 30.0], [122.05, 30.0]]
    service.invalidation_service.route_operating_layer("route_geometry_changed")
    assert stored_profile(service)["status"] == "stale"


def test_route_operating_layer_change_stales_the_profile_but_keeps_the_record(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile_id = stored_profile(service)["profile_id"]
    service.set_altitude_layer(layer(nominal_altitude_m=120.0))
    # A changed upstream value keeps the stored record (audit evidence) and its recorded
    # derivation, while the current applicability becomes stale.
    assert stored_profile(service)["profile_id"] == profile_id
    assert stored_profile(service)["cruise_altitude_m"] == CRUISE_M
    assert stored_profile(service)["status"] == "stale"


# --------------------------------------------------------------------------------------
# 9. persistence / API
# --------------------------------------------------------------------------------------


def test_evaluate_api_persists_and_readiness_api_reports_blockers(tmp_path):
    service = ready_service(tmp_path)
    router = ApiRouter(ApiContext(service))
    readiness = router.get("/api/route-3d-profiles/readiness", {}, {}).data
    assert readiness["status"] == "ready"
    response = router.post("/api/route-3d-profiles/evaluate", {"route_id": ROUTE_ID}).data
    assert response["route_3d_profiles"]["count"] == 1
    stored = router.get("/api/route-3d-profiles", {}, {}).data
    assert stored["items"][0]["status"] == "passed"
    assert stored["items"][0]["fingerprints"]["profile_fingerprint"]

    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    persisted = normalize_route_3d_profiles(
        restored.state["spatial_3d"]["route_3d_profiles"]
    )
    assert len(persisted) == 1
    assert next(iter(persisted.values()))["status"] == "passed"
    assert restored.state["result_statuses"]["route_3d_profiles"] == "passed"


def test_api_never_generates_a_profile_in_the_background(tmp_path):
    service = ready_service(tmp_path)
    router = ApiRouter(ApiContext(service))
    assert router.get("/api/route-3d-profiles", {}, {}).data["count"] == 0
    assert service.state["spatial_3d"]["route_3d_profiles"] == {}
    assert service.state["result_statuses"]["route_3d_profiles"] == "not_calculated"


def test_delete_api_removes_only_the_requested_profile(tmp_path):
    service = ready_service(tmp_path)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    router = ApiRouter(ApiContext(service))
    response = router.post(
        "/api/route-3d-profiles/delete", {"profile_id": stored_profile(service)["profile_id"]},
    ).data
    assert response["route_3d_profiles"]["count"] == 0
    assert service.state["spatial_3d"]["route_3d_profiles"] == {}
    with pytest.raises(ValueError, match="不存在"):
        router.post("/api/route-3d-profiles/delete", {"profile_id": "R3DP-NOPE"})


def test_legacy_project_backfills_the_additive_container(tmp_path):
    service = ready_service(tmp_path)
    project = blank_project({})
    modern_spatial = deepcopy(project["spatial_3d"])
    legacy = deepcopy(project)
    legacy["spatial_3d"].pop("route_3d_profiles")
    normalized = normalize_project(legacy, service.grid_service)
    assert normalized["spatial_3d"]["route_3d_profiles"] == {}
    assert normalized["spatial_3d"] == modern_spatial
    assert normalized["result_statuses"]["route_3d_profiles"] == "not_calculated"


# --------------------------------------------------------------------------------------
# 10. legacy / V3 semantics do not regress
# --------------------------------------------------------------------------------------


def test_route_altitude_profiles_are_never_written_by_a_route_3d_profile(tmp_path):
    service = ready_service(tmp_path)
    service.state["spatial_3d"]["route_altitude_profiles"] = {ROUTE_ID: {
        "route_id": ROUTE_ID, "mode": "waypoint_linear",
        "vertical_reference": "egm2008_orthometric",
        "waypoints": [
            {"distance_along_route_m": 0.0, "altitude_m": 30.0},
            {"distance_along_route_m": LENGTH_M, "altitude_m": 30.0},
        ],
        "source": "v3c_validated_route", "confirmed": True, "status": "confirmed",
        "derived": True, "locked": True, "locked_by_adoption": True,
    }}
    before = deepcopy(service.state["spatial_3d"]["route_altitude_profiles"])
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert service.state["spatial_3d"]["route_altitude_profiles"] == before
    # The 3D profile wins, but the V3 container keeps its own semantics.
    context = effective_route_vertical_context(service.state["spatial_3d"], ROUTE_ID)
    assert context["source_kind"] == "production_route_3d_profile"
    assert context["mode"] == "waypoint_linear"


def test_locked_v3_profile_edit_rejection_is_untouched(tmp_path):
    from cns_planner.application.spatial_3d_service import is_locked_v3_profile

    service = ready_service(tmp_path)
    locked = {
        "source": "v3c_validated_route", "derived": True, "locked_by_adoption": True,
    }
    assert is_locked_v3_profile(locked) is True
    assert is_locked_v3_profile({"source": "v3c_validated_route"}) is False
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    assert service.state["spatial_3d"]["route_altitude_profiles"] == {}


# --------------------------------------------------------------------------------------
# 11. UI contract (front-end module shape, no chart engine added)
# --------------------------------------------------------------------------------------


def test_front_end_ui_module_exposes_the_compact_card_contract():
    source = (
        Path("cns_planner/web/js/workflow/route3d_profile.js").read_text(encoding="utf-8")
    )
    for fragment in (
        "renderRoute3DProfilePanel", "bindRoute3DProfile", "diagnostic_not_aircraft_kinematic_validation",
        "terminal_transition_validation", "full_3d_geometry_validated", "profile_fingerprint",
        "climb_rate_mps", "descent_rate_mps", "implied_horizontal_speed_mps",
        "duration_s", "altitude_delta_m", "transition_horizontal_distance_m",
        "/api/route-3d-profiles/evaluate", "/api/route-3d-profiles/delete",
    ):
        assert fragment in source, fragment
    steps = Path("cns_planner/web/js/workflow/step03_routes.js").read_text(encoding="utf-8")
    assert "renderRoute3DProfilePanel" in steps
    assert "bindRoute3DProfile" in steps
