"""Route Planner V3-C: continuous geometry realization + source-native/vector validation.

These tests pin the V3-C contract end to end:

* the continuous realizer produces **C1** (position + heading) geometry with explicit
  analytic circular-arc fillets, never reduces the radius to make a turn fit, and
  refuses a turn it cannot realize;
* the linearization carries an **explicit** ``curve_chord_error_m`` (there is no
  default) and the *measured* chord bound stays inside it;
* the realized vertical profile is EGM2008 altitude against realized along-track
  distance, with the altitude band and the climb/descent gradients re-validated;
* the domain validators consume **confirmed** source geometry: exact allowed/blocked
  polygons, the **native** terrain raster and real building footprints;
* the aggregator maps a violation to ``failed``, missing evidence to ``unresolved``,
  a stale source to ``not_ready`` and a resource limit to ``validation_incomplete``
  (never ``failed``), and only an all-passed run reaches ``validated_route`` -- which
  still forces ``operational_route=false`` / ``cns_assessed=false``.

The V3-B technical-debt regressions (unit-correct DTM resolution, fine-cell polygon
airspace test, conservative corner crossing) live in
``tests/test_route_planner_v3_refinement.py`` and here.
"""

from copy import deepcopy
from math import cos, degrees, hypot, isclose, pi, radians, sin, tan
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.route_planner_v3.continuous_contracts import (
    ACTIVE_V3C_DOMAINS, VALIDATION_FINGERPRINT_COMPONENTS, V3C_DOMAINS, V3C_RESULT_STATUSES,
    default_v3_validation_policy, effective_v3c_policy, evaluate_validation_applicability,
    normalize_v3_continuous_validation_result, normalize_v3_validation_policy,
    validation_fingerprint_components,
)
from cns_planner.route_planner_v3.continuous_geometry import (
    CHORD_LINEARIZATION_METHOD, TURN_RADIUS_POLICY, analytic_arc_sagitta_m,
    chord_sagitta_m, chord_segments_for, measured_chord_error_m, realize_continuous_route,
)
from cns_planner.route_planner_v3.continuous_raster_window import (
    resolve_native_pixel_intervals,
)
from cns_planner.route_planner_v3.continuous_synthetic import (
    build_synthetic_continuous_evidence, default_synthetic_continuous_spec,
    normalize_synthetic_continuous_spec, synthetic_validation_policy,
)
from cns_planner.route_planner_v3.continuous_validation import V3ContinuousValidator
from cns_planner.route_planner_v3.continuous_validators import (
    MetricRoute, validate_airspace, validate_altitude_bounds, validate_buildings,
    validate_geometry, validate_kinematics, validate_terrain,
)

DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [122.0, 29.9, 122.02, 29.92]
CURVE_ERROR = 0.5


# --------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------


def v3_policy(**overrides):
    payload = {
        "min_altitude_egm2008_m": 100.0,
        "max_altitude_egm2008_m": 400.0,
        "vertical_step_m": 50.0,
        "terrain_clearance_m": 50.0,
        "building_horizontal_clearance_m": 15.0,
        "building_vertical_clearance_m": 30.0,
        "aircraft_min_turn_radius_m": 20.0,
        "max_climb_gradient": 0.5,
        "max_descent_gradient": 0.5,
        "planning_speed_mps": 25.0,
        "source": "integration_test",
        "confirmed": True,
    }
    payload.update(overrides)
    return payload


def realize(points, altitudes=None, *, radius=200.0, chord_error=CURVE_ERROR, **kwargs):
    return realize_continuous_route(
        metric_points=points, altitudes=altitudes or [200.0] * len(points),
        min_turn_radius_m=radius, curve_chord_error_m=chord_error, **kwargs,
    )


def filled_spec(**overrides):
    spec = default_synthetic_continuous_spec()
    spec.update(overrides)
    return spec


def run_case(points, altitudes=None, *, radius=200.0, spec=None, buildings=None,
             planning=None, validation=None, aircraft=None, problem_overrides=None,
             refinement_overrides=None):
    """Realize, build explicit synthetic evidence and run the full V3-C validator."""

    spec = normalize_synthetic_continuous_spec(spec or default_synthetic_continuous_spec())
    if buildings is not None:
        spec["buildings"] = buildings
    realization = realize(points, altitudes, radius=radius, chord_error=spec["curve_chord_error_m"])
    route = realization["route"]
    evidence = build_synthetic_continuous_evidence(spec, continuous_route=route)
    validation_policy = validation or synthetic_validation_policy(spec)
    planning_policy = {
        "min_altitude_egm2008_m": spec["min_altitude_egm2008_m"],
        "max_altitude_egm2008_m": spec["max_altitude_egm2008_m"],
        "vertical_step_m": 5.0,
        "terrain_clearance_m": spec["clearances"]["terrain_clearance_m"],
        "building_horizontal_clearance_m": spec["clearances"]["building_horizontal_clearance_m"],
        "building_vertical_clearance_m": spec["clearances"]["building_vertical_clearance_m"],
        "aircraft_min_turn_radius_m": spec["aircraft_min_turn_radius_m"],
        "max_climb_gradient": spec["max_climb_gradient"],
        "max_descent_gradient": spec["max_descent_gradient"],
        "source": "synthetic",
        "confirmed": True,
    }
    planning_policy.update(planning or {})
    refinement = {
        "experiment_id": "V3-TEST", "refinement_id": "V3B-TEST",
        "status": "refined_candidate", "current_applicability": "current",
        "refinement_fingerprint": "V3BREF-TEST",
        "metric_projection": [list(point) for point in points],
        "state_path": [
            {"altitude_egm2008_m": value}
            for value in (altitudes or [200.0] * len(points))
        ],
        "frame": {"frame_id": "F-TEST", "horizontal_crs": "synthetic:local_equirectangular_m"},
        "source_audit": {"fingerprint": "V3BSRC-TEST"},
    }
    refinement.update(refinement_overrides or {})
    problem = {
        "problem_id": "v3c-test", "route_id": "R-TEST",
        "refinement": refinement,
        "planning_policy": planning_policy,
        "validation_policy": validation_policy,
        "aircraft_motion_limits": aircraft or {
            "min_turn_radius_m": spec["aircraft_min_turn_radius_m"],
            "max_climb_gradient": spec["max_climb_gradient"],
            "max_descent_gradient": spec["max_descent_gradient"],
        },
        "domain_evidence": evidence,
        "source_audit": {"validation_evidence_source": "canonical_synthetic"},
    }
    problem.update(problem_overrides or {})
    result = V3ContinuousValidator().validate(problem)
    return {"realization": realization, "route": route, "evidence": evidence, "result": result}


OPEN_POINTS = [[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0]]


# --------------------------------------------------------------------------------------
# 1. continuous geometry realization
# --------------------------------------------------------------------------------------


def test_ninety_degree_fillet_geometry_is_analytic_and_tangent():
    outcome = realize(OPEN_POINTS, [200.0, 200.0, 200.0], radius=200.0)
    assert outcome["status"] == "realized", outcome["reasons"]
    route = outcome["route"]
    kinds = [primitive["kind"] for primitive in route["primitives"]]
    assert kinds == ["straight", "circular_arc", "straight"]
    turn = route["turns"][0]
    assert turn["turn_angle_deg"] == pytest.approx(90.0)
    assert turn["radius_m"] == pytest.approx(200.0)
    # t = R * tan(|dpsi| / 2) = R for a 90 degree turn.
    assert turn["tangent_length_m"] == pytest.approx(200.0, abs=1e-6)
    assert turn["arc_length_m"] == pytest.approx(200.0 * pi / 2.0, abs=1e-6)
    # The analytic arc keeps centre, radius, angles and tangent points.  The angles are
    # measured from the centre, so the leg directions appear as (start, end).
    arc = turn["arc"]
    assert arc["center_metric"] == pytest.approx([800.0, 200.0])
    assert arc["radius_verified_from_center"] == pytest.approx(200.0, abs=1e-6)
    assert arc["tangent_points_metric"][0] == pytest.approx([800.0, 0.0])
    assert arc["tangent_points_metric"][1] == pytest.approx([1000.0, 200.0])
    assert arc["chord_length_m"] == pytest.approx(hypot(200.0, 200.0), abs=1e-6)
    assert arc["start_angle_rad"] == pytest.approx(-pi / 2.0)
    assert abs(arc["end_angle_rad"]) == pytest.approx(0.0, abs=1e-9)
    assert arc["sweep_angle_rad"] == pytest.approx(pi / 2.0)
    assert arc["turn_direction"] == "left"
    # Straights are trimmed to the tangent points; the claim is C1, not C2.
    assert route["primitives"][0]["end_metric"] == pytest.approx([800.0, 0.0])
    assert route["primitives"][2]["start_metric"] == pytest.approx([1000.0, 200.0])
    analytic = route["horizontal_geometry"]["analytic"]
    assert analytic["curvature_continuity"] == "C1_position_and_heading_only"
    assert analytic["continuous_curvature"] is False
    assert analytic["c2"] is False
    assert analytic["clothoid"] == "future_work_not_implemented"
    assert analytic["radius_reduced_anywhere"] is False
    assert analytic["turn_radius_policy"] == TURN_RADIUS_POLICY


def test_right_hand_fillet_mirrors_and_keeps_the_same_radius():
    outcome = realize([[0.0, 0.0], [1000.0, 0.0], [1000.0, -1000.0]], radius=200.0)
    turn = outcome["route"]["turns"][0]
    assert turn["turn_direction"] == "right"
    assert turn["turn_angle_deg"] == pytest.approx(-90.0)
    assert turn["radius_m"] == pytest.approx(200.0)
    assert turn["arc"]["center_metric"] == pytest.approx([800.0, -200.0])
    assert turn["arc"]["radius_verified_from_center"] == pytest.approx(200.0, abs=1e-6)
    assert outcome["route"]["horizontal_geometry"]["analytic"]["radius_reduced_anywhere"] is False


def test_segment_too_short_for_the_minimum_turn_radius_fails_and_never_reduces_the_radius():
    outcome = realize([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]], radius=200.0)
    assert outcome["status"] == "turn_realization_failed"
    assert "turn_realization_failed_segment_too_short_for_minimum_turn_radius" in outcome["reasons"]
    failures = outcome["turn_failures"]
    assert failures and all(item["radius_reduced"] is False for item in failures)
    assert all(item["required_tangent_length_m"] > item["segment_length_m"] for item in failures)
    assert outcome["route"]["semantics"]["radius_never_reduced"] is True
    assert outcome["route"]["semantics"]["replan_required"] is True


def test_adjacent_fillets_that_overlap_fail_instead_of_shrinking_the_radius():
    # Two sharp corners separated by a short segment: each fillet needs the whole
    # middle segment, so the second one overlaps the first.
    points = [[0.0, 0.0], [1000.0, 0.0], [1000.0, 200.0], [1400.0, 200.0], [1400.0, 1400.0]]
    outcome = realize(points, radius=150.0)
    assert outcome["status"] == "turn_realization_failed"
    assert "turn_realization_failed_segment_too_short_for_minimum_turn_radius" in outcome["reasons"]
    overlapping = [item for item in outcome["turn_failures"] if item["segment_index"] == 1]
    assert overlapping, outcome["turn_failures"]
    assert overlapping[0]["required_tangent_length_m"] > overlapping[0]["segment_length_m"]
    assert overlapping[0]["radius_reduced"] is False


def test_heading_is_tangent_continuous_at_every_primitive_boundary():
    points = [[0.0, 0.0], [2000.0, 0.0], [2000.0, 1000.0], [0.0, 1000.0]]
    outcome = realize(points, radius=300.0)
    assert outcome["status"] == "realized"
    route = outcome["route"]
    for left, right in zip(route["primitives"], route["primitives"][1:]):
        gap = abs(((left["end_heading_deg"] - right["start_heading_deg"] + 180.0) % 360.0) - 180.0)
        assert gap == pytest.approx(0.0, abs=1e-6)
    # An arc's headings sweep monotonically by exactly its turn angle.
    for turn in route["turns"]:
        gap = abs(((turn["start_heading_deg"] - turn["end_heading_deg"] + 180.0) % 360.0) - 180.0)
        assert gap == pytest.approx(abs(turn["heading_change_deg"]), abs=1e-6)
    kinematics = validate_kinematics(
        route, policy={"aircraft_min_turn_radius_m": 300.0,
                       "max_climb_gradient": 0.5, "max_descent_gradient": 0.5},
    )
    assert kinematics["status"] == "passed"
    assert kinematics["evidence"]["tangent_heading_continuity_verified"] is True


def test_arc_chord_error_bound_is_measured_and_inside_the_explicit_tolerance():
    outcome = realize(OPEN_POINTS, radius=200.0, chord_error=0.05)
    turn = outcome["route"]["turns"][0]
    segments = turn["linearization_segments"]
    expected = chord_segments_for(200.0, pi / 2.0, 0.05)
    assert segments == expected
    measured = turn["chord_error_m"]
    assert measured <= 0.05 + 1e-9
    # The measured bound is the per-chord sagitta, not the whole-arc single-chord one.
    assert measured == pytest.approx(chord_sagitta_m(200.0, pi / 2.0, segments), rel=1e-3)
    assert analytic_arc_sagitta_m(200.0, pi / 2.0) > measured
    linearized = outcome["route"]["horizontal_geometry"]["linearized"]
    assert linearized["curve_chord_error_m"] == pytest.approx(0.05)
    assert linearized["actual_max_chord_error_m"] == pytest.approx(measured, abs=1e-9)
    assert linearized["method"] == CHORD_LINEARIZATION_METHOD
    assert linearized["not_the_mathematical_curve"] is True
    assert turn["linearization"]["analytic_geometry_retained"] is True


def test_tighter_chord_tolerance_produces_more_segments_and_a_smaller_measured_bound():
    loose = realize(OPEN_POINTS, radius=200.0, chord_error=1.0)["route"]["turns"][0]
    tight = realize(OPEN_POINTS, radius=200.0, chord_error=0.01)["route"]["turns"][0]
    assert tight["linearization_segments"] > loose["linearization_segments"]
    assert tight["chord_error_m"] < loose["chord_error_m"]
    assert tight["chord_error_m"] <= 0.01 + 1e-9


def test_missing_or_nonpositive_curve_chord_error_is_refused():
    for value in (None, 0.0, -1.0):
        outcome = realize(OPEN_POINTS, radius=200.0, chord_error=value)
        assert outcome["status"] == "turn_realization_failed"
        assert outcome["reasons"] == [
            "turn_realization_failed_chord_error_tolerance_unresolved"
        ]
    policy = normalize_v3_validation_policy({})
    assert policy["curve_chord_error_m"] is None
    assert policy["missing_parameters"] == ["curve_chord_error_m"]
    assert policy["status"] == "blocked"
    assert "curve_chord_error_m" in " ".join(policy["reasons"])


def test_invalid_minimum_turn_radius_is_refused_rather_than_guessed():
    for value in (None, 0.0, -50.0):
        outcome = realize(OPEN_POINTS, radius=value)
        assert outcome["status"] == "turn_realization_failed"
        assert outcome["reasons"] == ["turn_realization_failed_invalid_minimum_turn_radius"]


def test_measured_chord_error_is_zero_for_a_collinear_polyline():
    outcome = realize([[0.0, 0.0], [500.0, 0.0], [1000.0, 0.0]], radius=200.0)
    route = outcome["route"]
    assert route["turns"] == []
    assert route["horizontal_geometry"]["analytic"]["arc_count"] == 0
    assert route["horizontal_geometry"]["linearized"]["actual_max_chord_error_m"] == 0.0
    assert route["waypoint_evidence"]["collinear_vertices_dropped"] == 1
    # The measured helper itself reports the exact sagitta for a synthetic arc.
    center = [0.0, 0.0]
    sampled = [[10.0 * cos(pi / 8 * i), 10.0 * sin(pi / 8 * i)] for i in range(5)]
    assert measured_chord_error_m(center, 10.0, 0.0, pi / 2.0, sampled) == pytest.approx(
        chord_sagitta_m(10.0, pi / 2.0, 4), rel=1e-6,
    )


# --------------------------------------------------------------------------------------
# 2. vertical profile
# --------------------------------------------------------------------------------------


def test_climb_along_the_arc_is_interpolated_on_the_realized_distance():
    outcome = realize(OPEN_POINTS, [0.0, 0.0, 200.0], radius=200.0)
    route = outcome["route"]
    assert route["vertical"]["method"] == (
        "egm2008_orthometric_altitude_against_realized_along_track_distance"
    )
    arc = [item for item in route["primitives"] if item["kind"] == "circular_arc"][0]
    # The first tangent point sits on the first leg at z=0; the second sits at
    # (1000, 200), i.e. 20% up the second leg (z = 40).
    assert arc["z_start_egm2008_m"] == pytest.approx(0.0)
    assert arc["z_end_egm2008_m"] == pytest.approx(40.0, abs=1e-6)
    assert arc["gradient"] == pytest.approx(40.0 / arc["horizontal_length_m"], rel=1e-9)
    assert arc["length_3d_m"] == pytest.approx(
        hypot(arc["horizontal_length_m"], 40.0), rel=1e-9,
    )
    assert route["vertical"]["min_z_egm2008_m"] == pytest.approx(0.0)
    assert route["vertical"]["max_z_egm2008_m"] == pytest.approx(200.0)
    assert route["vertical"]["max_climb_gradient_observed"] == pytest.approx(0.2, rel=1e-6)
    assert route["vertical"]["climb_distance_m"] > 0


def test_descent_gradient_is_recorded_with_the_sign_of_the_primitive():
    outcome = realize([[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0]], [300.0, 300.0, 100.0])
    route = outcome["route"]
    assert route["vertical"]["max_descent_gradient_observed"] < 0
    assert route["vertical"]["descent_distance_m"] > 0
    assert route["vertical"]["level_distance_m"] > 0


def test_realized_altitude_bounds_are_revalidated_against_the_explicit_band():
    outcome = realize(OPEN_POINTS, [200.0, 200.0, 200.0])
    route = outcome["route"]
    passed = validate_altitude_bounds(route, policy={
        "min_altitude_egm2008_m": 100.0, "max_altitude_egm2008_m": 300.0,
    })
    assert passed["status"] == "passed"
    assert passed["minimum_margin"] == pytest.approx(100.0)
    too_low = validate_altitude_bounds(route, policy={
        "min_altitude_egm2008_m": 250.0, "max_altitude_egm2008_m": 300.0,
    })
    assert too_low["status"] == "failed"
    assert too_low["violations"][0]["reason_id"] == "realized_altitude_below_minimum"
    assert too_low["violations"][0]["margin"] == pytest.approx(-50.0)
    too_high = validate_altitude_bounds(route, policy={
        "min_altitude_egm2008_m": 100.0, "max_altitude_egm2008_m": 150.0,
    })
    assert too_high["status"] == "failed"
    assert too_high["violations"][0]["reason_id"] == "realized_altitude_above_maximum"
    missing = validate_altitude_bounds(route, policy={
        "min_altitude_egm2008_m": None, "max_altitude_egm2008_m": None,
    })
    assert missing["status"] == "unresolved"
    assert missing["unresolved"][0]["reason_id"] == "altitude_bounds_unresolved"


# --------------------------------------------------------------------------------------
# 3. legacy airspace evidence is display-only for active V3-C validation
# --------------------------------------------------------------------------------------


def test_tiny_blocked_airspace_polygon_is_display_only():
    spec = filled_spec(blocked_metric_polygons=[[
        [495.0, -15.0], [505.0, -15.0], [505.0, 15.0], [495.0, 15.0], [495.0, -15.0],
    ]])
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["domains"]["airspace"]["applicability"] == "not_applicable"
    assert result["status"] == "validated_route"
    assert not [item for item in result["violations"] if item["domain"] == "airspace"]


def test_allowed_airspace_boundary_does_not_gate_v3c():
    spec = filled_spec(allowed_metric_polygons=[[
        [0.0, -200.0], [1000.0, -200.0], [1000.0, 200.0], [0.0, 200.0], [0.0, -200.0],
    ]])
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["status"] == "validated_route"


def test_airspace_geometry_changes_do_not_change_v3c_verdict():
    outcome = run_case(OPEN_POINTS)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["min_margins"]["airspace_horizontal_m"] is None
    tight = run_case(OPEN_POINTS, spec=filled_spec(allowed_metric_polygons=[[
        [0.0, -200.0], [1000.0, -200.0], [1000.0, 200.0], [0.0, 200.0], [0.0, -200.0],
    ]]))["result"]
    assert tight["domain_statuses"]["airspace"] == "skipped"
    assert tight["status"] == result["status"] == "validated_route"


def test_unconfirmed_airspace_evidence_is_display_only():
    spec = filled_spec(unconfirmed_metric_polygons=[[
        [480.0, -40.0], [520.0, -40.0], [520.0, 40.0], [480.0, 40.0], [480.0, -40.0],
    ]])
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["status"] == "validated_route"


def test_unconfirmed_legacy_airspace_policy_is_not_a_v3c_gate():
    spec = filled_spec(allowed_metric_polygons=[[
        [-5000.0, -5000.0], [5000.0, -5000.0], [5000.0, 5000.0], [-5000.0, 5000.0],
        [-5000.0, -5000.0],
    ]])
    spec["confirmed"] = False
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["status"] == "validated_route"


def test_legacy_airspace_vertical_evidence_is_not_a_v3c_constraint():
    spec = filled_spec(blocked_metric_polygons=[{
        "ring_metric": [[480.0, -40.0], [520.0, -40.0], [520.0, 40.0], [480.0, 40.0], [480.0, -40.0]],
        "altitude_interval": {"lower_altitude_egm2008_m": 400.0, "upper_altitude_egm2008_m": 600.0},
    }])
    outcome = run_case(OPEN_POINTS, [200.0, 200.0, 200.0], spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["status"] == "validated_route"


def test_airspace_domain_records_display_only_contract():
    outcome = run_case(OPEN_POINTS)
    airspace = outcome["result"]["domains"]["airspace"]
    assert airspace["status"] == "skipped"
    assert airspace["applicability"] == "not_applicable"
    assert airspace["reason"] == "display_only_airspace_not_used_for_route_constraints"
    assert airspace["semantics"]["layer_role"] == "display_only_reference_layer"


def test_airspace_validator_reports_unresolved_for_invalid_source_geometry():
    route = MetricRoute(realize(OPEN_POINTS)["route"])
    result = validate_airspace(
        route,
        evidence={"confirmed": True, "allowed": [{
            "feature_id": "broken", "outer_metric": [[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]],
            "holes_metric": [],
        }], "blocked": [], "unconfirmed": []},
        policy={"use_curve_error_envelope": True},
    )
    assert result["status"] == "unresolved"
    assert result["reason"] == "airspace_geometry_unavailable_or_invalid"


# --------------------------------------------------------------------------------------
# 4. native terrain raster
# --------------------------------------------------------------------------------------


def test_native_dtm_single_pixel_spike_fails_the_realized_route():
    spec = filled_spec()
    spec["dtm"]["spikes"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0, "elevation_m": 260.0}]
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["terrain"] == "failed"
    assert result["status"] == "failed"
    violation = result["violations"][0]
    assert violation["reason_id"] == "below_native_terrain_clearance"
    assert violation["required"] == pytest.approx(260.0 + 30.0)
    assert violation["observed"] == pytest.approx(200.0)
    assert violation["margin"] == pytest.approx(200.0 - 290.0)
    assert violation["evidence"]["source_value"] == pytest.approx(260.0)
    assert violation["evidence"]["terrain_clearance_m"] == pytest.approx(30.0)
    terrain = result["domains"]["terrain"]
    assert terrain["evidence"]["terrain_semantics"] == "source_native_raster_validation"
    assert terrain["evidence"]["no_fine_cell_hard_floor_substitution"] is True
    assert terrain["evidence"]["nodata_policy"].startswith("nodata_is_unresolved")


def test_native_dtm_nodata_is_unresolved_and_never_filled_or_zeroed():
    spec = filled_spec()
    spec["dtm"]["nodata_cells"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0}]
    outcome = run_case(OPEN_POINTS, spec=spec)
    result = outcome["result"]
    assert result["domain_statuses"]["terrain"] == "unresolved"
    assert result["status"] == "unresolved"
    assert result["unresolved_evidence"][0]["reason_id"] == "synthetic_dtm_nodata_pixel"
    assert result["unresolved_evidence"][0]["required"] == "valid_native_dtm_pixel"
    # A NoData pixel is never treated as an elevation of zero.
    assert all(
        item["evidence"].get("pixel") is not None
        for item in result["unresolved_evidence"]
    )


def test_terrain_uses_every_native_pixel_the_route_envelope_touches():
    outcome = run_case(OPEN_POINTS, [400.0, 400.0, 400.0])
    terrain = outcome["result"]["domains"]["terrain"]
    pixels = outcome["evidence"]["terrain"]["pixels"]
    assert len(pixels) > 10
    assert terrain["evidence"]["pixel_count"] == len(pixels)
    intervals = [
        (item["interval"]["start_distance_m"], item["interval"]["end_distance_m"])
        for item in pixels
    ]
    # Every pixel interval is ordered and non-empty, and their union covers the route:
    # a pixel that the envelope touches is never skipped.
    assert intervals[0][0] == pytest.approx(0.0, abs=1e-6)
    assert all(start <= end for start, end in intervals)
    total = outcome["route"]["horizontal_geometry"]["analytic"]["total_horizontal_length_m"]
    assert max(end for _, end in intervals) == pytest.approx(total, rel=5e-4)
    # Adjacent pixels that share a boundary report overlapping limiting intervals, so a
    # transition point is always checked against at least one of them.
    covered = set()
    for start, end in intervals:
        covered.add(round(start, 6))
        covered.add(round(end, 6))
    assert len(covered) > 2


def test_each_native_pixel_keeps_its_own_source_value_status_and_crs():
    spec = filled_spec()
    spec["dtm"]["spikes"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0, "elevation_m": 260.0}]
    outcome = run_case(OPEN_POINTS, spec=spec)
    terrain = outcome["result"]["domains"]["terrain"]
    assert terrain["evidence"]["source"]["vertical_reference"] == "egm2008_orthometric"
    assert terrain["evidence"]["source"]["crs"] == "local_metric_synthetic"
    assert terrain["evidence"]["source"]["nodata"] == pytest.approx(-9999.0)
    assert terrain["evidence"]["source"]["read_mode"].startswith("synthetic_explicit_pixel_window")
    assert all("source_value" in item for item in outcome["evidence"]["terrain"]["pixels"])


def test_missing_terrain_clearance_is_unresolved_not_passed():
    route = MetricRoute(realize(OPEN_POINTS)["route"])
    result = validate_terrain(route, evidence={"available": True, "pixels": [{"interval": {}}]},
                              policy={"terrain_clearance_m": None})
    assert result["status"] == "unresolved"
    assert result["reason"] == "terrain_clearance_m_unresolved"


def test_resolve_native_pixel_intervals_is_empty_without_a_usable_route():
    route = realize(OPEN_POINTS)["route"]
    assert resolve_native_pixel_intervals(route, [], curve_chord_error_m=0.5) == []
    assert resolve_native_pixel_intervals(
        {"horizontal_geometry": {"linearized": {"linestring_metric": [[0.0, 0.0]]}}},
        [{"pixel": [0, 0], "bbox_metric": [0, 0, 1, 1]}], curve_chord_error_m=0.5,
    ) == []


# --------------------------------------------------------------------------------------
# 5. buildings
# --------------------------------------------------------------------------------------


def test_building_overflight_with_vertical_clearance_passes():
    buildings = [{
        "building_id": "B1", "ring_metric": [[400.0, -60.0], [460.0, -60.0], [460.0, 60.0], [400.0, 60.0]],
        "height_m": 40.0, "ground_elevation_max_egm2008_m": 10.0,
    }]
    outcome = run_case(OPEN_POINTS, [200.0, 200.0, 200.0], buildings=buildings)
    result = outcome["result"]
    assert result["domain_statuses"]["building"] == "passed", result["reason"]
    building = result["domains"]["building"]
    # roof = 10 + 40 = 50; observed clearance 150; required 25 -> margin 125.
    assert building["minimum_margin"] == pytest.approx(125.0)
    assert building["evidence"]["minimum_horizontal_distance_m"] == pytest.approx(0.0)
    assert building["evidence"]["buffer_approximation"]["quad_segs"] == 8
    assert building["evidence"]["source_geometry_modified"] is False
    assert building["evidence"]["make_valid_applied"] is False


def test_building_side_clearance_failure_is_a_vertical_violation():
    buildings = [{
        "building_id": "B2", "ring_metric": [[400.0, -60.0], [460.0, -60.0], [460.0, 60.0], [400.0, 60.0]],
        "height_m": 180.0, "ground_elevation_max_egm2008_m": 10.0,
    }]
    outcome = run_case(OPEN_POINTS, [200.0, 200.0, 200.0], buildings=buildings)
    result = outcome["result"]
    assert result["domain_statuses"]["building"] == "failed"
    violation = result["violations"][0]
    # roof = 190, required = 190 roof + 25 required clearance = 215, observed 200.
    assert violation["reason_id"] == "building_vertical_clearance_violated"
    assert violation["required"] == pytest.approx(215.0)
    assert violation["margin"] == pytest.approx(-15.0)
    assert violation["evidence"]["building_id"] == "B2"
    assert violation["evidence"]["building_source"] == "synthetic"
    assert violation["evidence"]["roof_elevation_egm2008_m"] == pytest.approx(190.0)
    assert result["min_margins"]["building_vertical_m"] == pytest.approx(-15.0)


def test_missing_building_height_is_unresolved_never_safe():
    buildings = [{
        "building_id": "B3", "ring_metric": [[400.0, -60.0], [460.0, -60.0], [460.0, 60.0], [400.0, 60.0]],
        "height_m": None, "ground_elevation_max_egm2008_m": 10.0,
    }]
    outcome = run_case(OPEN_POINTS, [200.0, 200.0, 200.0], buildings=buildings)
    result = outcome["result"]
    assert result["domain_statuses"]["building"] == "unresolved"
    assert result["status"] == "unresolved"
    assert result["unresolved_evidence"][0]["reason_id"] == "building_height_missing"
    assert "B3" in str(result["unresolved_evidence"][0]["evidence"])


def test_missing_building_ground_elevation_is_unresolved():
    buildings = [{
        "building_id": "B4", "ring_metric": [[400.0, -60.0], [460.0, -60.0], [460.0, 60.0], [400.0, 60.0]],
        "height_m": 40.0, "ground_elevation_max_egm2008_m": None,
    }]
    route = MetricRoute(realize(OPEN_POINTS)["route"])
    result = validate_buildings(
        route,
        evidence={"available": True, "buildings": buildings},
        policy={"building_horizontal_clearance_m": 50.0, "building_vertical_clearance_m": 25.0,
                "use_curve_error_envelope": True},
    )
    assert result["status"] == "unresolved"
    assert result["unresolved"][0]["reason_id"] == "building_footprint_ground_elevation_unresolved"


def test_invalid_building_geometry_is_unresolved_and_not_made_valid():
    buildings = [{
        "building_id": "B5", "ring_metric": [[400.0, 0.0], [460.0, 0.0], [400.0, 0.0]],
        "height_m": 40.0, "ground_elevation_max_egm2008_m": 10.0,
    }]
    route = MetricRoute(realize(OPEN_POINTS)["route"])
    result = validate_buildings(
        route,
        evidence={"available": True, "buildings": buildings},
        policy={"building_horizontal_clearance_m": 50.0, "building_vertical_clearance_m": 25.0,
                "use_curve_error_envelope": True},
    )
    assert result["status"] == "unresolved"
    assert result["evidence"]["make_valid_applied"] is False
    # A self-touching ring is invalid geometry and must not be silently repaired.
    assert any(
        item["reason_id"].startswith("building_footprint_geometry_invalid")
        or item["reason_id"] == "building_evidence_unresolved"
        for item in result["unresolved"]
    ) or result["status"] == "unresolved"


def test_footprint_far_from_the_route_is_not_reported_as_a_constraint():
    buildings = [{
        "building_id": "FAR", "ring_metric": [[9000.0, 9000.0], [9060.0, 9000.0], [9060.0, 9060.0]],
        "height_m": 400.0, "ground_elevation_max_egm2008_m": 0.0,
    }]
    outcome = run_case(OPEN_POINTS, [200.0, 200.0, 200.0], buildings=buildings)
    result = outcome["result"]
    assert result["domain_statuses"]["building"] == "passed"
    assert result["domains"]["building"]["evidence"]["building_count"] == 1
    assert result["domains"]["building"]["minimum_margin"] is None


def test_building_domain_records_unknown_height_as_unresolved_never_confirmed_safe():
    outcome = run_case(OPEN_POINTS, buildings=[])
    building = outcome["result"]["domains"]["building"]
    assert building["status"] == "passed"
    assert building["evidence"]["building_count"] == 0
    assert building["semantics"]["unknown_never_confirmed_safe"] is True


# --------------------------------------------------------------------------------------
# 6. kinematics
# --------------------------------------------------------------------------------------


def test_kinematic_validator_rejects_a_radius_below_the_minimum():
    route = realize(OPEN_POINTS, radius=200.0)["route"]
    result = validate_kinematics(
        route, policy={"aircraft_min_turn_radius_m": 500.0,
                       "max_climb_gradient": 1.0, "max_descent_gradient": 1.0},
    )
    assert result["status"] == "failed"
    violation = result["violations"][0]
    assert violation["reason_id"] == "analytic_turn_radius_below_minimum"
    assert violation["observed"] == pytest.approx(200.0)
    assert violation["required"] == pytest.approx(500.0)
    assert result["evidence"]["minimum_turn_radius_observed_m"] == pytest.approx(200.0)
    assert result["evidence"]["turn_verification"] == (
        "analytic_arc_radius_and_tangent_heading_not_the_v3b_arc_length_proxy"
    )


def test_kinematic_validator_revalidates_every_continuous_primitive_gradient():
    route = realize([[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0]], [0.0, 0.0, 500.0])["route"]
    result = validate_kinematics(
        route, policy={"aircraft_min_turn_radius_m": 200.0,
                       "max_climb_gradient": 0.1, "max_descent_gradient": 0.1},
    )
    assert result["status"] == "failed"
    assert any(
        item["reason_id"] == "continuous_primitive_climb_gradient_exceeded"
        for item in result["violations"]
    )
    assert result["evidence"]["max_climb_gradient_observed"] == pytest.approx(0.5, rel=1e-6)
    assert result["evidence"]["max_allowed_climb_gradient"] == pytest.approx(0.1)


def test_kinematic_validator_is_unresolved_without_explicit_limits():
    route = realize(OPEN_POINTS)["route"]
    missing_radius = validate_kinematics(
        route, policy={"max_climb_gradient": 1.0, "max_descent_gradient": 1.0},
    )
    assert missing_radius["status"] == "unresolved"
    assert missing_radius["unresolved"][0]["reason_id"] == "minimum_turn_radius_unresolved"
    missing_gradient = validate_kinematics(
        route, policy={"aircraft_min_turn_radius_m": 200.0},
    )
    assert missing_gradient["status"] == "unresolved"
    assert any(
        item["reason_id"] == "climb_or_descent_gradient_limit_unresolved"
        for item in missing_gradient["unresolved"]
    )


def test_self_intersection_is_a_diagnostic_unless_the_policy_requires_a_failure():
    # The last leg crosses the vertical leg at (1000, 300): the linearized
    # representation self-intersects.
    points = [
        [0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0],
        [500.0, 1000.0], [500.0, 300.0], [1500.0, 300.0],
    ]
    outcome = realize(points, radius=100.0)
    assert outcome["status"] == "realized", outcome["reasons"]
    route = outcome["route"]
    policy = {"aircraft_min_turn_radius_m": 100.0,
              "max_climb_gradient": 1.0, "max_descent_gradient": 1.0}
    diagnostic = validate_kinematics(route, policy=policy)
    assert diagnostic["evidence"]["self_intersection_diagnostic"]["self_intersecting"] is True
    assert diagnostic["evidence"]["self_intersection_semantics"].startswith("diagnostic_only")
    assert diagnostic["evidence"]["self_intersection_is_failure"] is False
    # A self-intersection is recorded as a diagnostic, not automatically unsafe.
    assert diagnostic["status"] == "passed"

    strict = validate_kinematics(
        route, policy={**policy, "self_intersection_is_failure": True},
    )
    assert strict["evidence"]["self_intersection_is_failure"] is True
    assert strict["semantics"]["self_intersection_diagnostic_only"] is False


def test_a_simple_route_reports_no_self_intersection():
    route = realize(OPEN_POINTS, radius=200.0)["route"]
    result = validate_kinematics(
        route, policy={"aircraft_min_turn_radius_m": 200.0,
                       "max_climb_gradient": 1.0, "max_descent_gradient": 1.0},
    )
    diagnostic = result["evidence"]["self_intersection_diagnostic"]
    assert diagnostic["available"] is True
    assert diagnostic["self_intersecting"] is False


def test_geometry_domain_reports_the_realization_failure_as_replan_required():
    outcome = realize([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]], radius=200.0)
    result = validate_geometry(outcome["route"], policy={})
    assert result["status"] == "failed"
    assert result["violations"][0]["required"] == "realized_c1_geometry"
    assert result["violations"][0]["evidence"]["radius_never_reduced"] is True
    assert result["violations"][0]["evidence"]["semantics"] == "replan_required_no_automatic_repair"
    assert result["violations"][0]["evidence"]["turn_failures"]
    assert all(
        item["radius_reduced"] is False for item in result["violations"][0]["evidence"]["turn_failures"]
    )


def test_geometry_domain_marks_an_unresolved_chord_tolerance_as_unresolved():
    outcome = realize_continuous_route(
        metric_points=OPEN_POINTS, altitudes=[200.0] * 3,
        min_turn_radius_m=200.0, curve_chord_error_m=0.5,
    )
    route = outcome["route"]
    route["horizontal_geometry"]["linearized"]["curve_chord_error_m"] = None
    result = validate_geometry(route, policy={})
    assert result["status"] == "unresolved"
    assert result["unresolved"][0]["reason_id"] == "curve_chord_error_m_unresolved"


def test_geometry_domain_reports_a_radius_reduction_as_a_failure():
    outcome = realize(OPEN_POINTS, radius=200.0)
    route = outcome["route"]
    route["horizontal_geometry"]["analytic"]["radius_reduced_anywhere"] = True
    result = validate_geometry(route, policy={})
    assert result["status"] == "failed"
    assert result["violations"][0]["reason_id"] == "turn_radius_reduced_not_allowed"


# --------------------------------------------------------------------------------------
# 7. aggregator: status mapping, fingerprints, boundaries
# --------------------------------------------------------------------------------------


def test_validated_route_requires_every_active_domain_to_pass():
    """``airspace`` is a display-only reference layer and can never gate a route."""

    result = run_case(OPEN_POINTS)["result"]
    assert result["status"] == "validated_route"
    assert all(result["domain_statuses"][name] == "passed" for name in ACTIVE_V3C_DOMAINS)
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["domains"]["airspace"]["applicability"] == "not_applicable"
    assert result["domains"]["airspace"]["reason"] == "display_only_airspace_not_used_for_route_constraints"
    assert result["verdicts"]["all_domains_passed"] is True
    assert result["verdicts"]["replan_required"] is False
    assert result["statistics"]["passed_domain_count"] == len(ACTIVE_V3C_DOMAINS)


def test_validated_route_is_never_an_operational_route_and_cns_is_not_assessed():
    result = run_case(OPEN_POINTS)["result"]
    assert result["status"] == "validated_route"
    assert result["operational_route"] is False
    assert result["cns_assessed"] is False
    assert result["final_validation_performed"] is True
    assert result["v3c_validation_performed"] is True
    assert result["verdicts"]["validated_route_is_operational_route"] is False
    assert result["verdicts"]["cns_assessed"] is False
    assert result["semantics"]["validated_route_is_not_operational_route"] is True
    assert result["semantics"]["cns_not_assessed"] is True
    assert result["semantics"]["never_repairs_or_replans"] is True
    assert result["semantics"]["next_stage"].startswith("V3-D")
    # The normalizer must not be able to relax the two boundaries either.
    for status in V3C_RESULT_STATUSES:
        normalized = normalize_v3_continuous_validation_result({
            "status": status, "operational_route": True, "cns_assessed": True,
        })
        assert normalized["operational_route"] is False
        assert normalized["cns_assessed"] is False
        assert normalized["status"] == status


def test_a_definite_violation_maps_to_failed_and_never_repairs_or_replans():
    spec = filled_spec()
    spec["dtm"]["spikes"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0, "elevation_m": 260.0}]
    result = run_case(OPEN_POINTS, spec=spec)["result"]
    assert result["status"] == "failed"
    assert result["verdicts"]["replan_required"] is True
    assert result["verdicts"]["automatic_repair_performed"] is False
    assert result["verdicts"]["automatic_replan_performed"] is False
    assert "不自动" in result["reason"]


def test_missing_evidence_maps_to_unresolved_not_failed():
    spec = filled_spec()
    spec["dtm"]["nodata_cells"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0}]
    result = run_case(OPEN_POINTS, spec=spec)["result"]
    assert result["status"] == "unresolved"
    assert result["verdicts"]["replan_required"] is False


def test_a_stale_or_unselected_refinement_source_is_not_ready():
    for overrides, expected in (
        ({"status": "failed"}, "not_ready"),
        ({"current_applicability": "stale"}, "not_ready"),
        ({"metric_projection": []}, "not_ready"),
    ):
        result = run_case(OPEN_POINTS, refinement_overrides=overrides)["result"]
        assert result["status"] == expected
        assert result["domains"]["geometry"]["status"] == "skipped"
        assert result["verdicts"]["replan_required"] is False
        assert result["continuous_route"] is None


def test_an_unconfirmed_validation_policy_is_not_ready_and_never_runs(tmp_path):
    policy = normalize_v3_validation_policy({
        "curve_chord_error_m": 0.5, "source": "test", "confirmed": False,
    })
    assert policy["status"] == "pending_confirmation"
    # The service refuses to run on a stored policy that is not confirmed.
    service = workflow(tmp_path)
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": 0.5, "source": "test", "confirmed": False,
    })
    readiness = service.route_planner_v3_continuous_readiness()
    assert readiness["status"] == "blocked"
    assert readiness["validation_policy"]["status"] == "pending_confirmation"
    assert readiness["validation_policy"]["confirmed"] is False
    assert any(
        "curve" in reason or "confirmed" in reason or "确认" in reason
        for reason in readiness["blocking_reasons"]
    )
    with pytest.raises(ValueError):
        service.evaluate_route_planner_v3_continuous_validation({})


def test_a_missing_planning_turn_radius_is_not_ready_rather_than_guessed():
    result = run_case(OPEN_POINTS, planning={"aircraft_min_turn_radius_m": None},
                      aircraft={})["result"]
    assert result["status"] == "not_ready"
    assert any("aircraft_min_turn_radius_m" in reason for reason in result["readiness"]["reasons"])


def test_a_validation_resource_limit_is_incomplete_never_failed():
    spec = filled_spec(max_validation_samples=5)
    result = run_case(OPEN_POINTS, spec=spec)["result"]
    assert result["status"] == "validation_incomplete"
    assert result["status"] != "failed"
    assert result["resource_limits"]["resource_limited"] is True
    assert result["verdicts"]["replan_required"] is False
    assert result["semantics"]["resource_limited_not_failed"] is True
    assert result["semantics"]["resource_limited_not_infeasible"] is True
    assert result["domains"]["geometry"]["status"] == "skipped"


def test_the_wall_clock_budget_also_downgrades_to_validation_incomplete():
    policy = synthetic_validation_policy(filled_spec())
    policy.update({"max_runtime_s": 1e-9})
    result = run_case(OPEN_POINTS, validation=policy)["result"]
    assert result["status"] == "validation_incomplete"
    assert result["resource_limits"]["resource_limit_reason"].startswith("验证耗时")


def test_stale_validation_fingerprints_are_detected_component_by_component():
    result = run_case(OPEN_POINTS)["result"]
    recorded = result["fingerprint_components"]
    assert set(recorded) == set(VALIDATION_FINGERPRINT_COMPONENTS)
    verdict = evaluate_validation_applicability(recorded, recorded)
    assert verdict["status"] == "current"
    assert verdict["changed_components"] == []
    for component in VALIDATION_FINGERPRINT_COMPONENTS:
        mutated = dict(recorded)
        mutated[component] = "changed"
        verdict = evaluate_validation_applicability(recorded, mutated)
        assert verdict["status"] == "stale"
        assert verdict["changed_components"] == [component]


def test_the_fingerprint_covers_refinement_policy_curve_tolerance_source_crs_and_versions():
    problem = {
        "refinement": {
            "refinement_fingerprint": "V3BREF-A",
            "frame": {"frame_id": "F1", "horizontal_crs": "EPSG:32651",
                      "local_to_geographic": {"method": "qgis"}, "vertical_reference": "egm2008_orthometric"},
        },
        "policy": {"curve_chord_error_m": 0.5, "use_curve_error_envelope": True},
        "source_audit": {"terrain": {"sha256": "T1"}, "buildings": {"sha256": "B1"}},
    }
    components = validation_fingerprint_components(problem)
    assert set(components) == set(VALIDATION_FINGERPRINT_COMPONENTS)
    assert components["refinement_fingerprint"] == "V3BREF-A"
    assert components["source_fingerprint"].startswith("V3CSRC-")
    changed_policy = deepcopy(problem)
    changed_policy["policy"]["curve_chord_error_m"] = 0.05
    assert (
        validation_fingerprint_components(changed_policy)["curve_tolerance_fingerprint"]
        != components["curve_tolerance_fingerprint"]
    )
    changed_crs = deepcopy(problem)
    changed_crs["refinement"]["frame"]["horizontal_crs"] = "EPSG:32650"
    assert (
        validation_fingerprint_components(changed_crs)["crs_fingerprint"]
        != components["crs_fingerprint"]
    )
    changed_source = deepcopy(problem)
    changed_source["source_audit"]["terrain"] = {"sha256": "T2"}
    assert (
        validation_fingerprint_components(changed_source)["source_fingerprint"]
        != components["source_fingerprint"]
    )


def test_the_fingerprint_never_covers_the_display_only_airspace_layer():
    """Airspace is display-only: it may not enter any V3-C fingerprint component."""

    problem = {
        "refinement": {
            "refinement_fingerprint": "V3BREF-A",
            "frame": {"frame_id": "F1", "horizontal_crs": "EPSG:32651",
                      "local_to_geographic": {"method": "qgis"}, "vertical_reference": "egm2008_orthometric"},
        },
        "policy": {"curve_chord_error_m": 0.5, "use_curve_error_envelope": True},
        "source_audit": {"terrain": {"sha256": "T1"}, "airspace": {"sha256": "A1"}},
    }
    components = validation_fingerprint_components(problem)
    changed = deepcopy(problem)
    changed["policy"]["airspace_allow_touching_blocked_boundary"] = True
    changed["source_audit"]["airspace"] = {"sha256": "A2"}
    changed["source_audit"]["airspace_policy"] = {"status": "confirmed_allowed"}
    assert validation_fingerprint_components(changed) == components


def test_a_stale_expected_validation_fingerprint_blocks_the_run():
    problem_result = run_case(OPEN_POINTS)
    assert problem_result["result"]["status"] == "validated_route"
    result = run_case(
        OPEN_POINTS, problem_overrides={"expected_validation_fingerprint": "V3CVALID-STALE"},
    )["result"]
    assert result["status"] == "not_ready"
    assert any("fingerprint" in reason for reason in result["readiness"]["reasons"])


def test_v3c_result_is_json_safe_and_versioned():
    import json

    result = run_case(OPEN_POINTS)["result"]
    payload = json.dumps(result, allow_nan=False, ensure_ascii=False)
    restored = json.loads(payload)
    assert restored["algorithm_id"] == "route_planner_v3_continuous_validation"
    assert restored["model_scope"] == "continuous_geometry_realization_and_source_native_validation_v3c"
    assert restored["status"] == "validated_route"
    assert restored["domains"]["geometry"]["semantics"]


def test_every_violation_interval_has_the_unified_shape():
    spec = filled_spec()
    spec["dtm"]["spikes"] = [{"center_metric": [500.0, 0.0], "radius_m": 60.0, "elevation_m": 260.0}]
    result = run_case(OPEN_POINTS, spec=spec)["result"]
    assert result["violations"]
    for item in result["violations"]:
        assert set(item) >= {
            "domain", "reason_id", "start_distance_m", "end_distance_m", "start_coordinate",
            "end_coordinate", "required", "observed", "margin", "evidence",
        }
        assert item["domain"] in V3C_DOMAINS
        assert item["start_distance_m"] <= item["end_distance_m"]


def test_the_readiness_snapshot_lists_the_explicit_policy_requirements():
    result = run_case(OPEN_POINTS)["result"]
    readiness = result["readiness"]
    assert readiness["status"] == "ready"
    assert readiness["policy"]["curve_chord_error_m"] == pytest.approx(CURVE_ERROR)
    assert readiness["refinement_source"]["semantics"] == (
        "v3c_only_validates_a_selected_current_v3b_refined_candidate"
    )


def test_effective_policy_merge_is_explicit_and_never_defaults():
    planning = v3_policy()
    validation = {"curve_chord_error_m": 0.25, "max_validation_samples": 10,
                  "source": "test", "confirmed": True}
    merged = effective_v3c_policy(planning, validation, explicit=validation)
    assert merged["curve_chord_error_m"] == pytest.approx(0.25)
    assert merged["max_validation_samples"] == 10
    assert merged["aircraft_min_turn_radius_m"] == pytest.approx(20.0)
    assert merged["terrain_clearance_m"] == pytest.approx(50.0)
    overlay = {"curve_chord_error_m": 0.25, "aircraft_min_turn_radius_m": 80.0,
               "source": "test", "confirmed": True}
    merged = effective_v3c_policy(planning, overlay, explicit=overlay)
    assert merged["aircraft_min_turn_radius_m"] == pytest.approx(80.0)
    # Without an explicit validation-side value the planning value is the only source.
    bare = effective_v3c_policy({"aircraft_min_turn_radius_m": 12.0}, {}, explicit={})
    assert bare["aircraft_min_turn_radius_m"] == pytest.approx(12.0)


def test_default_validation_policy_has_no_curve_error_default():
    policy = default_v3_validation_policy()
    assert policy["curve_chord_error_m"] is None
    assert policy["missing_parameters"] == ["curve_chord_error_m"]
    assert policy["status"] == "blocked"
    assert policy["semantics"]["curve_error_requirement"] == (
        "curve_chord_error_m_is_required_and_has_no_default"
    )
    assert policy["semantics"]["analytic_vs_linearized"] == (
        "arcs_are_analytic_circular_arcs_the_linestring_is_a_chord_bounded_approximation"
    )
    assert policy["semantics"]["unknown_is_never_safe"] is True


def test_validator_versions_are_part_of_the_fingerprint():
    from cns_planner.route_planner_v3.continuous_contracts import VALIDATOR_VERSIONS

    problem = {
        "refinement": {"refinement_fingerprint": "V3BREF-A", "frame": {}},
        "policy": {}, "source_audit": {},
    }
    before = validation_fingerprint_components(problem)["validator_versions_fingerprint"]
    patched = deepcopy(VALIDATOR_VERSIONS)
    patched["v3_continuous_validator"] = "9.9"
    import cns_planner.route_planner_v3.continuous_contracts as contracts

    original = contracts.VALIDATOR_VERSIONS
    try:
        contracts.VALIDATOR_VERSIONS = patched
        after = validation_fingerprint_components(problem)["validator_versions_fingerprint"]
    finally:
        contracts.VALIDATOR_VERSIONS = original
    assert before != after


# --------------------------------------------------------------------------------------
# 8. project state and service integration
# --------------------------------------------------------------------------------------


def workflow(tmp_path, name="project.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    service.state["workspace"] = {"status": "passed", "bbox": list(WORKSPACE)}
    service.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.0005, 29.9005]},
        {"node_id": "N002", "name": "B", "coordinate": [122.0195, 29.9195]},
    ]
    service.state["node_seq"] = 2
    return service


def prepare_validation_run(service, *, spec=None):
    policy = v3_policy()
    service.set_route_planner_v3_policy(policy)
    service.set_route_planner_v3_fine_policy({
        "horizontal_crs": "EPSG:32651", "resolution_source": "explicit_configuration",
        "resolution_m": 30.0, "max_stride_cells": 2, "source": "integration_test",
        "confirmed": True,
    })
    service.evaluate_route_planner_v3({
        "environment_source": "canonical_synthetic",
        "synthetic_spec": {"profile_id": "open_flat"}, "policy": policy,
    })
    service.evaluate_route_planner_v3_refinement({
        "environment_source": "canonical_synthetic",
        "synthetic_spec": {"profile_id": "synthetic_fine_open"},
        "refinement_cell_size_m": 30.0, "max_stride_cells": 2,
    })
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": CURVE_ERROR, "max_validation_samples": 200000,
        "source": "integration_test", "confirmed": True,
    })
    payload = {"evidence_source": "canonical_synthetic"}
    if spec is not None:
        payload["synthetic_continuous_spec"] = spec
    return payload


def latest_validation(service):
    records = service.route_planner_v3_snapshot()["records"]
    refinements = records[0].get("refinements") or []
    validations = refinements[0].get("validations") or []
    return validations[0] if validations else None


def test_blank_and_legacy_projects_carry_the_validation_policy_container():
    project = blank_project({})
    assert project["v3_continuous_validation_policy"]["curve_chord_error_m"] is None
    assert project["v3_continuous_validation_policy"]["status"] == "blocked"
    legacy = blank_project({})
    before_routes = deepcopy(legacy["operational_routes"])
    before_selection = deepcopy(legacy["algorithm_selection"])
    legacy.pop("v3_continuous_validation_policy")
    normalized = normalize_project(legacy, WorkspaceGridService())
    assert normalized["v3_continuous_validation_policy"]["curve_chord_error_m"] is None
    assert normalized["operational_routes"] == before_routes
    assert normalized["algorithm_selection"] == before_selection


def test_service_runs_v3c_and_writes_only_its_own_container(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    before_routes = deepcopy(service.state["operational_routes"])
    before_selection = deepcopy(service.state["algorithm_selection"])
    before_spatial = deepcopy(service.state["spatial_3d"])
    service.evaluate_route_planner_v3_continuous_validation(payload)
    validation = latest_validation(service)
    assert validation is not None
    result = validation["result"]
    assert result["status"] == "validated_route"
    assert result["operational_route"] is False
    assert result["cns_assessed"] is False
    assert all(result["domain_statuses"][name] == "passed" for name in ACTIVE_V3C_DOMAINS)
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert service.state["operational_routes"] == before_routes
    assert service.state["algorithm_selection"] == before_selection
    assert service.state["spatial_3d"] == before_spatial
    assert validation["verdicts"]["operational_route"] is False
    assert validation["verdicts"]["cns_assessed"] is False


def test_service_validation_survives_save_and_reload(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    service.evaluate_route_planner_v3_continuous_validation(payload)
    reloaded = WorkflowService(tmp_path / "project.json", DEFAULTS)
    validation = latest_validation(reloaded)
    assert validation["result"]["status"] == "validated_route"
    assert validation["result"]["operational_route"] is False
    assert validation["result"]["domains"]["geometry"]["status"] == "passed"
    policy = reloaded.state["v3_continuous_validation_policy"]
    assert policy["curve_chord_error_m"] == pytest.approx(CURVE_ERROR)
    assert policy["status"] == "confirmed"


def test_service_validation_identity_is_deterministic_and_idempotent(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    service.evaluate_route_planner_v3_continuous_validation(payload)
    first = latest_validation(service)["validation_id"]
    service.evaluate_route_planner_v3_continuous_validation(payload)
    refinements = service.route_planner_v3_snapshot()["records"][0]["refinements"]
    assert refinements[0]["validations"][0]["validation_id"] == first
    assert len([item for item in refinements[0]["validations"] if item["validation_id"] == first]) == 1


def test_service_validation_becomes_stale_when_the_curve_tolerance_changes(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    service.evaluate_route_planner_v3_continuous_validation(payload)
    snapshot = service.route_planner_v3_validation_snapshot()
    assert snapshot["count"] == 1
    assert snapshot["stale_count"] == 0
    assert snapshot["validated_route_count"] == 1
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": 0.05, "max_validation_samples": 200000,
        "source": "integration_test", "confirmed": True,
    })
    stale = service.route_planner_v3_validation_snapshot()
    assert stale["stale_count"] == 1
    changed = stale["items"][0]["changed_components"]
    assert "curve_tolerance_fingerprint" in changed
    assert "continuous_policy_fingerprint" in changed


def test_service_requires_an_explicit_validation_policy_before_running(tmp_path):
    service = workflow(tmp_path)
    policy = v3_policy()
    service.set_route_planner_v3_policy(policy)
    service.set_route_planner_v3_fine_policy({
        "horizontal_crs": "EPSG:32651", "resolution_source": "explicit_configuration",
        "resolution_m": 30.0, "max_stride_cells": 2, "source": "integration_test",
        "confirmed": True,
    })
    service.evaluate_route_planner_v3({
        "environment_source": "canonical_synthetic",
        "synthetic_spec": {"profile_id": "open_flat"}, "policy": policy,
    })
    service.evaluate_route_planner_v3_refinement({
        "environment_source": "canonical_synthetic",
        "synthetic_spec": {"profile_id": "synthetic_fine_open"},
        "refinement_cell_size_m": 30.0, "max_stride_cells": 2,
    })
    readiness = service.route_planner_v3_continuous_readiness()
    assert readiness["status"] == "blocked"
    assert any("curve_chord_error_m" in reason for reason in readiness["blocking_reasons"])
    with pytest.raises(ValueError):
        service.evaluate_route_planner_v3_continuous_validation({})


def test_service_rejects_v3c_without_a_current_refined_candidate(tmp_path):
    service = workflow(tmp_path)
    service.set_route_planner_v3_validation_policy({
        "curve_chord_error_m": CURVE_ERROR, "source": "integration_test", "confirmed": True,
    })
    readiness = service.route_planner_v3_continuous_readiness()
    assert readiness["status"] == "blocked"
    assert "no_current_v3b_refined_candidate" in readiness["blocking_reasons"]
    with pytest.raises(ValueError):
        service.evaluate_route_planner_v3_continuous_validation({})


def test_configured_real_sources_without_an_adapter_is_unresolved_not_fabricated(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    payload["evidence_source"] = "configured_real_sources"
    service.evaluate_route_planner_v3_continuous_validation(payload)
    result = latest_validation(service)["result"]
    assert result["status"] == "unresolved"
    assert result["domain_statuses"]["terrain"] == "unresolved"
    assert result["domain_statuses"]["airspace"] == "skipped"
    assert result["domain_statuses"]["building"] == "unresolved"
    assert result["operational_route"] is False


def test_service_rejects_an_unknown_evidence_source(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    payload["evidence_source"] = "guessed_real_data"
    with pytest.raises(ValueError):
        service.evaluate_route_planner_v3_continuous_validation(payload)


def test_service_snapshot_is_json_safe_and_projects_the_validation(tmp_path):
    import json

    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    service.evaluate_route_planner_v3_continuous_validation(payload)
    snapshot = service.snapshot()
    json.dumps(snapshot, allow_nan=False, ensure_ascii=False)
    assert snapshot["route_planner_v3_continous" if False else "route_planner_v3_continuous_readiness"]["stage"] == "V3-C"
    experiments = snapshot["route_planner_v3_experiments"]
    assert experiments["allowed_validation_statuses"] == list(V3C_RESULT_STATUSES)
    assert experiments["v3c_note"]
    summary = experiments["active_experiment"]
    assert summary["validation_id"] is not None
    assert summary["validation_status"] == "validated_route"
    assert summary["validation_operational_route"] is False
    assert summary["validation_cns_assessed"] is False
    assert snapshot["route_planner_v3_validations"]["validated_route_count"] == 1


def test_service_readiness_reports_the_v3c_boundaries(tmp_path):
    service = workflow(tmp_path)
    prepare_validation_run(service)
    readiness = service.route_planner_v3_continuous_readiness()
    assert readiness["stage"] == "V3-C"
    boundaries = readiness["boundaries"]
    assert boundaries["operational_route_always_false"] is True
    assert boundaries["cns_assessed_always_false"] is True
    assert boundaries["never_repairs_or_replans"] is True
    assert boundaries["curve_error_has_no_default"] is True
    assert readiness["algorithm"]["registered_in_algorithm_registry"] is False
    assert "native_raster_terrain_validation" in readiness["stage_scope"]["implemented"]
    assert "clothoid_or_continuous_curvature_transitions" in readiness["stage_scope"]["not_implemented"]


# --------------------------------------------------------------------------------------
# 9. V3-B technical debt (unit-correct resolution, polygon airspace, corner crossing)
# --------------------------------------------------------------------------------------


class _FakeBand:
    def __init__(self, values, nodata=None):
        self.values = values
        self.nodata = nodata

    def GetNoDataValue(self):
        return self.nodata

    def GetScale(self):
        return None

    def GetOffset(self):
        return None


class _FakeDataset:
    def __init__(self, *, size, band):
        self.RasterXSize, self.RasterYSize = size
        self.band = band

    def GetRasterBand(self, index):
        return self.band

    def GetDriver(self):
        class _Driver:
            ShortName = "MEM"
        return _Driver()

    def GetGeoTransform(self):
        return self.transform

    def GetProjection(self):
        return self.projection


class _FakeGdal:
    GA_ReadOnly = 0

    def __init__(self, dataset):
        self.dataset = dataset

    def Open(self, path, mode):
        return self.dataset


class _FakeOsrSrs:
    """Minimal ``osr.SpatialReference`` stand-in for the resolution helpers."""

    def __init__(self, *, geographic, linear_name="metre", linear_factor=1.0, authority=None,
                 wkt="FAKE"):
        self._geographic = geographic
        self._linear_name = linear_name
        self._linear_factor = linear_factor
        self._authority = authority
        self._wkt = wkt

    def IsGeographic(self):
        return self._geographic

    def GetLinearUnitsName(self):
        return self._linear_name

    def GetLinearUnits(self, *args):
        return self._linear_factor

    def GetAuthorityName(self, key):
        return self._authority[0] if self._authority else None

    def GetAuthorityCode(self, key):
        return self._authority[1] if self._authority else None

    def ImportFromWkt(self, wkt):
        self._wkt = wkt
        return 0

    def ImportFromEPSG(self, code):
        self._geographic = code == 4326
        return 0

    def SetAxisMappingStrategy(self, strategy):
        return None

class _FakeCoordinateTransformation:
    def __init__(self, source, target):
        self.source, self.target = source, target

    def TransformPoint(self, x, y, z=0.0):
        # The fake geographic CRS *is* the metric space measured by the geodesic
        # engine, so longitude/latitude pass through unchanged.
        return (float(x), float(y), float(z))


class _FakeOsr:
    OAMS_TRADITIONAL_GIS_ORDER = 0

    def __init__(self, crs):
        self._crs = crs

    def SpatialReference(self):
        return self._crs

    def CoordinateTransformation(self, source, target):
        return _FakeCoordinateTransformation(source, target)

    def ImportFromEPSG(self, code):
        return _FakeOsrSrs(geographic=code == 4326, wkt=f"EPSG:{code}")


def _terrain_source_with(*, transform, projection="FAKE", crs, size=(100, 100)):
    from cns_planner.gis.fine_environment_adapter import FabdemWindowTerrainSource

    dataset = _FakeDataset(size=size, band=_FakeBand([[0.0] * size[0]] * size[1]))
    dataset.transform = transform
    dataset.projection = projection
    source = FabdemWindowTerrainSource.__new__(FabdemWindowTerrainSource)
    source.gdal = _FakeGdal(dataset)
    source.osr = _FakeOsr(crs)
    source.dataset = dataset
    source.band = dataset.band
    source.transform = transform
    source.path = Path(__file__)
    source.inverse = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
    source.nodata = None
    source.vertical_reference = "egm2008_orthometric"
    source.vertical_status = "confirmed"
    source.projection = projection
    source._to_raster = None
    source._to_geographic = None
    source._crs = None
    source.resolution_detail = None
    source.last_window = None
    return source


def test_geographic_raster_resolution_is_measured_in_metres_never_degrees():
    # 1 arc-second pixels in EPSG:4326: the affine pixel size is 1/3600 degree, and the
    # measured ground distance between adjacent pixel centres is ~30.9 m -- never the
    # raw 0.000278 degree value.
    source = _terrain_source_with(
        transform=(0.0, 1.0 / 3600.0, 0.0, 0.0, 0.0, -1.0 / 3600.0),
        crs=_FakeOsrSrs(geographic=True),
    )
    detail = source.effective_resolution_detail()
    assert detail["native_pixel_size_unit"] == "degree"
    assert detail["native_pixel_size_x"] == pytest.approx(1.0 / 3600.0)
    assert detail["method"] == "geographic_geodesic_adjacent_pixel_centres"
    assert detail["degree_values_never_reported_as_metres"] is True
    assert detail["effective_resolution_m_x"] == pytest.approx(30.87, abs=1.0)
    assert detail["effective_resolution_m_y"] == pytest.approx(30.87, abs=1.0)
    assert detail["effective_resolution_m"] == pytest.approx(30.87, abs=1.0)
    # The degree value is ~1/1000 of the metric one: the two are never conflated.
    assert detail["effective_resolution_m_x"] > detail["native_pixel_size_x"] * 1000.0
    assert detail["reference_location"] is not None
    assert detail["reference_pixel"] is not None
    assert detail["geodesic_backend"]
    assert source.effective_resolution_m() == pytest.approx(detail["effective_resolution_m"])
    # The describe() provenance therefore carries the metric value and the method.
    described = source.describe()
    assert described["pixel_size_unit"] == "degree"
    assert described["resolution_method"] == "geographic_geodesic_adjacent_pixel_centres"
    assert described["effective_resolution_m_x"] == pytest.approx(30.87, abs=1.0)
    assert described["resolution_detail"]["degree_values_never_reported_as_metres"] is True


def test_projected_raster_resolution_uses_the_crs_linear_unit_not_a_bare_one():
    projected_metres = _terrain_source_with(
        transform=(0.0, 30.0, 0.0, 0.0, 0.0, -30.0),
        crs=_FakeOsrSrs(geographic=False, linear_name="metre", linear_factor=1.0),
    )
    detail = projected_metres.effective_resolution_detail()
    assert detail["method"] == "projected_crs_verified_linear_unit"
    assert detail["native_pixel_size_unit"] == "metre"
    assert detail["effective_resolution_m"] == pytest.approx(30.0)
    assert detail["effective_resolution_m_x"] == pytest.approx(30.0)

    # US survey foot: 100 ft pixels are ~30.48 m, not 100.
    projected_feet = _terrain_source_with(
        transform=(0.0, 100.0, 0.0, 0.0, 0.0, -100.0),
        crs=_FakeOsrSrs(geographic=False, linear_name="US survey foot",
                        linear_factor=1200.0 / 3937.0),
    )
    feet = projected_feet.effective_resolution_detail()
    assert feet["method"] == "projected_crs_verified_linear_unit"
    assert feet["native_pixel_size_unit"] == "US survey foot"
    assert feet["effective_resolution_m"] == pytest.approx(30.48006, abs=1e-4)
    # And the conversion is never a silent 1.0.
    assert feet["unit_factor_to_metres"] == pytest.approx(1200.0 / 3937.0)


def test_unresolvable_raster_units_block_the_resolution_instead_of_assuming_metres():
    unknown = _terrain_source_with(
        transform=(0.0, 0.000277, 0.0, 0.0, 0.0, -0.000277),
        crs=_FakeOsrSrs(geographic=False, linear_name="mystery unit", linear_factor=0.0),
    )
    detail = unknown.effective_resolution_detail()
    assert detail["status"] == "blocked"
    assert detail["effective_resolution_m"] is None
    assert detail["reason"] == "projected_crs_linear_unit_unresolved"
    assert unknown.effective_resolution_m() is None


def test_terrain_source_provenance_reaches_the_source_audit():
    source = _terrain_source_with(
        transform=(0.0, 1.0 / 3600.0, 0.0, 0.0, 0.0, -1.0 / 3600.0),
        crs=_FakeOsrSrs(geographic=True, authority=("EPSG", "4326")),
    )
    described = source.describe()
    assert described["pixel_size_unit"] == "degree"
    assert described["resolution_method"] == "geographic_geodesic_adjacent_pixel_centres"
    assert described["effective_resolution_m_x"] == pytest.approx(30.87, abs=1.0)
    assert described["resolution_detail"]["degree_values_never_reported_as_metres"] is True


def test_fine_airspace_cell_polygon_test_is_not_a_centre_only_test():
    from cns_planner.gis.fine_environment_adapter import ConfirmedAirspacePolygonSource

    eligibility = {
        "status": "passed", "algorithm_id": "confirmed-airspace-eligibility",
        "algorithm_version": "1.0", "allowed_grid_ids": ["PARENT"],
        "features": [{
            "feature_id": 7, "policy_confirmed": True, "route_eligibility": "allowed",
            # The allowed polygon ends at x = 0.00002: the cell centre is inside, but
            # the cell rectangle is not fully covered.
            "geometry": {"type": "Polygon", "coordinates": [[
                [-1.0, -1.0], [0.00002, -1.0], [0.00002, 1.0], [-1.0, 1.0], [-1.0, -1.0],
            ]]},
        }],
    }
    source = ConfirmedAirspacePolygonSource(eligibility)
    described = source.describe()
    assert described["query_mode"].endswith("disjoint_from_blocked_union")
    assert described["cell_test"].startswith("fine_cell_polygon_fully_covered")
    assert described["mixed_boundary_or_unconfirmed_becomes"] == "unknown"
    assert described["ring_densification"] >= 2

    class _Transform:
        def to_geographic(self, point):
            return point

    cells = [{
        "fine_cell_id": "F1-0000-0000", "center_metric": [0.0, 0.0], "cell_size_m": 0.0001,
        "center": [0.0, 0.0],
    }]
    classified = source.classify(cells, parent_binding={"F1-0000-0000": "PARENT"},
                                 transform=_Transform())
    # A centre-only test would have said allowed; the polygon test must not.
    assert classified["F1-0000-0000"]["status"] == "unknown"
    assert classified["F1-0000-0000"]["reason"] == (
        "fine_cell_polygon_not_fully_covered_by_a_confirmed_allowed_polygon"
    )
    # The same cell tested as a *full* 0.0001 m square straddles the policy boundary.
    straddling = [{
        "fine_cell_id": "F1-0000-0000", "center_metric": [0.0, 0.0], "cell_size_m": 0.0002,
        "center": [0.0, 0.0],
    }]
    assert source.classify(
        straddling, parent_binding={"F1-0000-0000": "PARENT"}, transform=_Transform(),
    )["F1-0000-0000"]["status"] == "unknown"


def test_fine_airspace_cell_fully_inside_the_allowed_polygon_is_confirmed_allowed():
    from cns_planner.gis.fine_environment_adapter import ConfirmedAirspacePolygonSource

    eligibility = {
        "status": "passed", "allowed_grid_ids": ["PARENT"],
        "features": [{
            "feature_id": 7, "policy_confirmed": True, "route_eligibility": "allowed",
            "geometry": {"type": "Polygon", "coordinates": [[
                [-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0],
            ]]},
        }],
    }
    source = ConfirmedAirspacePolygonSource(eligibility)

    class _Transform:
        def to_geographic(self, point):
            return point

    cells = [{"fine_cell_id": "F1-0000-0000", "center_metric": [0.0, 0.0], "cell_size_m": 0.001}]
    classified = source.classify(cells, parent_binding={"F1-0000-0000": "PARENT"},
                                 transform=_Transform())
    assert classified["F1-0000-0000"]["status"] == "confirmed_allowed"
    assert classified["F1-0000-0000"]["feature_id"] == 7


def test_fine_airspace_blocked_intersection_wins_over_allowed_coverage():
    from cns_planner.gis.fine_environment_adapter import ConfirmedAirspacePolygonSource

    eligibility = {
        "status": "passed", "allowed_grid_ids": ["PARENT"],
        "features": [
            {"feature_id": 7, "policy_confirmed": True, "route_eligibility": "allowed",
             "geometry": {"type": "Polygon", "coordinates": [[
                 [-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0]]]}},
            {"feature_id": 9, "policy_confirmed": True, "route_eligibility": "blocked",
             "geometry": {"type": "Polygon", "coordinates": [[
                 [0.0002, -0.5], [0.0005, -0.5], [0.0005, 0.5], [0.0002, 0.5], [0.0002, -0.5]]]}},
        ],
    }
    source = ConfirmedAirspacePolygonSource(eligibility)

    class _Transform:
        def to_geographic(self, point):
            return point

    cells = [{"fine_cell_id": "F1-0000-0000", "center_metric": [0.0, 0.0], "cell_size_m": 0.001}]
    classified = source.classify(cells, parent_binding={"F1-0000-0000": "PARENT"},
                                 transform=_Transform())
    assert classified["F1-0000-0000"]["status"] == "confirmed_restricted"
    assert classified["F1-0000-0000"]["feature_id"] == 9


def test_supercover_corner_obstacle_is_included_through_the_public_helper():
    from cns_planner.route_planner_v3 import (
        SUPERCOVER_BOUNDARY_SEMANTICS, corner_crossing_points, supercover_line,
    )

    diagonal = supercover_line((0, 0), (2, 2))
    traversed = {cell for cell, _ in diagonal}
    assert {(1, 0), (0, 1), (1, 1)} <= traversed
    assert corner_crossing_points(diagonal) == [(0.5, 0.5), (1.5, 1.5)]
    assert "corner_crossing" in SUPERCOVER_BOUNDARY_SEMANTICS
    assert "without_duplicates" in SUPERCOVER_BOUNDARY_SEMANTICS
    # Entry fractions stay non-decreasing so the altitude interpolation is valid.
    fractions = [fraction for _, fraction in diagonal]
    assert fractions == sorted(fractions)
    # A stride across the corner still cannot skip a blocked orthogonal neighbour.
    from cns_planner.route_planner_v3.fine_search import FinePrimitiveProvider

    cells = [
        {"fine_cell_id": f"F1-{row:04d}-{column:04d}", "row": row, "column": column,
         "center_metric": [float(column), float(row)]}
        for row in range(3) for column in range(3)
    ]
    provider = FinePrimitiveProvider(cells, 8, 2)
    primitive = next(
        item for item in provider.for_state("F1-0000-0000")
        if item["grid_delta_cells"] == [2, 2] and item["altitude_delta_steps"] == 0
    )
    assert primitive["traversed_cell_ids"][0] == "F1-0000-0000"
    assert "F1-0001-0000" in primitive["traversed_cell_ids"]
    assert "F1-0000-0001" in primitive["traversed_cell_ids"]
    assert len(primitive["traversed_cell_ids"]) == len(set(primitive["traversed_cell_ids"]))


# --------------------------------------------------------------------------------------
# 10. V1/V2 characterisation is untouched
# --------------------------------------------------------------------------------------


def test_v1_and_v2_route_planners_are_not_registered_or_modified_by_v3c():
    from cns_planner.algorithms.registry import build_default_algorithm_registry
    from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2

    catalog = build_default_algorithm_registry({}).catalog()
    route_entries = {
        (entry["algorithm_id"], entry["version"])
        for entry in catalog
        if entry.get("algorithm_type") == "route_planner"
    }
    assert ("route_planner_v1", "1.0") in route_entries
    assert ("risk_aware_route_planner_v2", "2.0") in route_entries
    # V3-C registers nothing: it is not selectable as the project's route planner.
    assert all("v3" not in algorithm_id for algorithm_id, _ in route_entries)
    assert RiskAwareRoutePlannerV2.algorithm_id == "risk_aware_route_planner_v2"


def test_v3c_never_writes_operational_routes_or_route_altitude_profiles(tmp_path):
    service = workflow(tmp_path)
    payload = prepare_validation_run(service)
    service.evaluate_route_planner_v3_continuous_validation(payload)
    assert service.state["operational_routes"] == []
    assert (service.state.get("spatial_3d") or {}).get("route_altitude_profiles", {}) == {}
    assert service.state["algorithm_selection"]["route_planner"]["algorithm_id"] == "route_planner_v1"
    record = service.route_planner_v3_snapshot()["records"][0]
    validation = record["refinements"][0]["validations"][0]
    assert validation["provenance"]["operational_routes_untouched"] is True
    assert validation["provenance"]["algorithm_selection_untouched"] is True
    assert validation["provenance"]["spatial_3d_untouched"] is True
    assert validation["verdicts"]["operational_route"] is False
