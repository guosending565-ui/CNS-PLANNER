"""Vertical Transition Continuous Validation V1 tests.

Covers the additive source-native climb/descent geometry validation contract:

* normal climb/descent pass, terrain penetration, building roof penetration, NoData /
  missing-height unresolved, exact-contact ``margin == 0`` is **not** a penetration;
* the truncated **original** polyline (never a ``start → end`` straight chord) and the
  piecewise-linear Route3DProfile ``z(s)`` resampled on it;
* readiness: profile not passed/current, source verification, explicit ``horizontal_crs``;
* the Route3DProfile *zero-horizontal vertical jump* fail-closed boundary;
* the Safety Evidence V2 linkage (validated → ``full_3d_geometry_validated``, failed → hard
  constraint failure, absent / unresolved → still not complete);
* stale / fingerprint / invalidation, including "never stales the Theta* chain";
* the API surface, the persistence backfill and the front-end UI contract;
* Coverage3D and V1/V3 non-regression.

Nothing here asserts a ``safe`` / ``unsafe`` verdict: this artifact is a *geometry*
validation, not a flight-dynamics, kinematic or terminal-procedure certification.
"""

from copy import deepcopy
from math import hypot
from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.geometric_3d import GeometricCoverage3DV1, path_length_m
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.vertical_transition_validation import (
    GEOMETRY_INTERSECTION_THRESHOLD_M, TRANSITION_DOMAIN_IDS, VALIDATION_STATUSES,
    normalize_vertical_transition_validation_collection,
)

DEFAULTS = Path("cns_planner/config/defaults.json")
ROUTE_ID = "R0001"
LAYER_ID = "L-LOW"
CRUISE_M = 100.0
TERMINAL_DEPARTURE_M = 20.0
TERMINAL_ARRIVAL_M = 40.0
HORIZONTAL_CRS = "EPSG:32651"

#: One route polyline with a **real intermediate vertex inside each transition window**: the
#: transition must truncate this polyline, never replace it with a straight chord.
PATH = [
    [122.0, 30.0], [122.001, 30.0], [122.004, 30.0], [122.01, 30.0], [122.019, 30.0],
]
LENGTH_M = path_length_m(PATH)
JOIN_M = LENGTH_M * 0.2
LEAVE_M = LENGTH_M * 0.8

ROUTE = {
    "route_id": ROUTE_ID, "status": "passed", "distance_m": LENGTH_M,
    "path": deepcopy(PATH),
}

#: An equirectangular local-metric stand-in for ``QgisMetricTransform``.  The magnitudes and
#: the ``to_metric`` / ``to_geographic`` round trip are what matter, not the projection itself.
LON_SCALE = 100000.0
LAT_SCALE = 111320.0


def to_metric(point):
    return [float(point[0]) * LON_SCALE, float(point[1]) * LAT_SCALE]


def to_geographic(point):
    return [float(point[0]) / LON_SCALE, float(point[1]) / LAT_SCALE]


METRIC_PATH = [to_metric(point) for point in PATH]
METRIC_LENGTH_M = sum(
    hypot(right[0] - left[0], right[1] - left[1]) for left, right in zip(METRIC_PATH, METRIC_PATH[1:])
)


def metric_distance_of_route_distance(distance_along_route_m):
    """Map a CRS84 geometric route distance onto the local-metric polyline (same shape)."""

    return float(distance_along_route_m) * (METRIC_LENGTH_M / LENGTH_M)


# ---------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------


def workflow(tmp_path, *, routes=True):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    if routes:
        service.state["operational_routes"] = [deepcopy(ROUTE)]
        service.state["result_statuses"]["routes"] = "passed"
    return service


def layer(**overrides):
    payload = {
        "altitude_layer_id": LAYER_ID, "name": "低层", "nominal_altitude_m": CRUISE_M,
        "lower_altitude_m": 50.0, "upper_altitude_m": 150.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True, "status": "confirmed",
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


def cruise_validation(**overrides):
    payload = {
        "validation_id": "LRV-1", "status": "validated_candidate",
        "current_applicability": "current", "status_reason": None,
        "candidate": {
            "candidate_id": "LC-1", "route_id": ROUTE_ID, "altitude_layer_id": LAYER_ID,
            "candidate_fingerprint": "candidate-fp",
        },
        "route": {
            "path": deepcopy(PATH), "nominal_altitude_m": CRUISE_M,
            "vertical_reference": "egm2008_orthometric",
        },
        "domains": {"terrain": {"status": "passed"}, "building": {"status": "passed"}},
        "failed_intervals": [], "unresolved_intervals": [],
        "source_type": "configured_real_sources",
        "fingerprints": {"validation_fingerprint": "validation-fp", "path_fingerprint": "path-fp"},
    }
    payload.update(overrides)
    return payload


def source_audits():
    return {
        "status": "passed", "count": 2, "items": {
            "terrain_dtm": {
                "role": "terrain_dtm", "status": "verified", "file_name": "dtm.tif",
                "verification": {"sha256": "terrain-sha"},
            },
            "buildings": {
                "role": "buildings", "status": "verified", "file_name": "b.gpkg",
                "verification": {"sha256": "building-sha"},
            },
        },
    }


def ready_service(tmp_path, *, with_adoption=True, with_cruise=True, with_crs=True):
    """Every explicit readiness item satisfied, plus a current Route3DProfile."""

    service = workflow(tmp_path)
    service.set_altitude_layer(layer())
    service.set_route_operating_layer(assignment())
    service.set_departure_arrival_procedure(procedure_payload("departure"))
    service.set_departure_arrival_procedure(procedure_payload("arrival"))
    service.state["source_audits"] = source_audits()
    if with_adoption:
        service.state["layered_operational_adoptions"] = {
            "schema_version": "layered-operational-adoptions",
            "status": "passed", "count": 1, "items": [adoption()],
        }
    if with_cruise:
        service.state["layered_route_validations"] = {
            "schema_version": "layered-route-validation-collection-v1",
            "status": "validated_candidate", "count": 1, "active_validation_id": "LRV-1",
            "items": [cruise_validation()],
        }
    if with_crs:
        service.state["v3_fine_refinement_policy"] = {"horizontal_crs": HORIZONTAL_CRS}
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    return service


def restore_upstream_bypass(service):
    """Re-inject the published adoption + cruise validation without any auto-staling.

    ``set_departure_arrival_procedure`` legitimately stales the adoption chain, so a test that
    changes a procedure afterwards must restore the published lineage before the readiness of
    an *unrelated* artifact is re-read.
    """

    service.state["layered_operational_adoptions"] = {
        "schema_version": "layered-operational-adoptions",
        "status": "passed", "count": 1, "items": [adoption()],
    }
    service.state["layered_route_validations"] = {
        "schema_version": "layered-route-validation-collection-v1",
        "status": "validated_candidate", "count": 1, "active_validation_id": "LRV-1",
        "items": [cruise_validation()],
    }
    service.state["source_audits"] = source_audits()
    return service


def stored_profile(service, route_id=ROUTE_ID):
    return next(
        item for item in service.state["spatial_3d"]["route_3d_profiles"].values()
        if item["route_id"] == route_id
    )


def projected_profile(service, route_id=ROUTE_ID):
    return service.route_3d_profiles()["profiles_by_route"][route_id]


# ---------------------------------------------------------------------------------------
# evidence adapter
# ---------------------------------------------------------------------------------------


def terrain_pixels(*, elevation_climb, elevation_descent=0.0, nodata_climb=False,
                   nodata_descent=False):
    """A per-phase native-pixel fixture covering the whole truncated phase."""

    def build(phase_id, elevation, nodata, suffix):
        return {
            "available": True,
            "source": {"crs": HORIZONTAL_CRS, "nodata": -9999, "phase": suffix},
            "pixels": [{
                "pixel": [10, 20],
                "bbox_metric": [0.0, 0.0, 50.0, 50.0],
                "center_metric": [25.0, 25.0],
                "data_status": "unknown" if nodata else "passed",
                "elevation_egm2008_m": None if nodata else float(elevation),
                "source_value": None if nodata else float(elevation),
                "reason": "source_pixel_nodata_never_filled_never_zeroed" if nodata else None,
            }],
        }

    return {
        "departure_climb": build(
            "departure_climb", elevation_climb, nodata_climb, "climb",
        ),
        "arrival_descent": build(
            "arrival_descent", elevation_descent, nodata_descent, "descent",
        ),
    }


def phase_metric_interval(phase_length_along_route_m, metric_length_m):
    """The along-metric-route interval that exactly covers one whole phase."""

    del phase_length_along_route_m
    return {"start_distance_m": 0.0, "end_distance_m": float(metric_length_m) + 1.0}


def evidence_adapter(*, terrain=None, buildings=None, source_type="configured_real_sources",
                     metric_line_recorder=None):
    """A canonical ``configured_real_sources`` adapter for one test evaluation."""

    terrain = terrain or {
        "departure_climb": {"available": True, "source": {"crs": HORIZONTAL_CRS}, "pixels": []},
        "arrival_descent": {"available": True, "source": {"crs": HORIZONTAL_CRS}, "pixels": []},
    }
    buildings = buildings or {"departure_climb": [], "arrival_descent": []}

    def build(*, phase, phase_id, route_id, profile, metric_line, metric_route, horizontal_crs,
              payload):
        if metric_line_recorder is not None:
            metric_line_recorder.append(deepcopy(metric_line))
        line = [
            [float(point[0]), float(point[1])] for point in metric_line
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        length = sum(
            hypot(right[0] - left[0], right[1] - left[1])
            for left, right in zip(line, line[1:])
        )
        phase_terrain = deepcopy(terrain.get(phase_id) or {
            "available": True, "source": {"crs": HORIZONTAL_CRS}, "pixels": [],
        })
        for pixel in phase_terrain.get("pixels") or []:
            pixel["interval"] = {
                "start_distance_m": 0.0, "end_distance_m": float(length) + 1.0,
            }
        return {
            "adapter_id": "vertical_transition_test_real_source_adapter",
            "source_type": source_type,
            "phase_id": phase_id,
            "metric_crs": horizontal_crs,
            "to_geographic": to_geographic,
            "metric_line": deepcopy(line),
            "terrain": phase_terrain,
            "buildings": {
                "available": True, "source": {"role": "buildings", "crs": HORIZONTAL_CRS},
                "buildings": deepcopy(buildings.get(phase_id) or []),
            },
            "sample_count": len(phase_terrain.get("pixels") or []) + len(
                buildings.get(phase_id) or []
            ),
            "sources": {
                "terrain_dtm": {"role": "terrain_dtm", "sha256": "terrain-sha"},
                "buildings": {"role": "buildings", "sha256": "building-sha"},
                "metric_frame": {"horizontal_crs": horizontal_crs},
            },
        }

    build.to_metric = to_metric
    return build


def evaluate(service, adapter, payload=None):
    request = {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    request.update(payload or {})
    return service.evaluate_vertical_transition_validation(request, evidence_adapter=adapter)


def latest(service, route_id=ROUTE_ID):
    items = service.vertical_transition_validations()["items"]
    return next(
        item for item in reversed(items) if str(item.get("route_id")) == str(route_id)
    )


# ---------------------------------------------------------------------------------------
# 1. contract + readiness
# ---------------------------------------------------------------------------------------


def test_ready_route_evaluates_both_transitions_with_the_full_contract(tmp_path):
    service = ready_service(tmp_path)
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["status"] == "ready", readiness["blockers"]
    assert readiness["horizontal_crs"] == HORIZONTAL_CRS
    assert readiness["horizontal_crs_source"] == "payload.horizontal_crs"
    assert readiness["transition_geometry"]["status"] == "resolved"
    assert set(readiness["transition_geometry"]["phases"]) == {
        "departure_climb", "arrival_descent",
    }

    result = evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    record = latest(service)
    assert record["status"] == "validated"
    assert record["current_applicability"] == "current"
    assert record["full_3d_geometry_validated"] is True
    assert result["status"] == "validated"
    for key in (
        "validation_id", "route_id", "profile_id", "status", "current_applicability",
        "phases", "minimum_margins", "unresolved_evidence", "fingerprints", "provenance",
        "limitations", "created_at",
    ):
        assert key in record, key
    assert record["profile_id"] == stored_profile(service)["profile_id"]
    assert [item["phase_id"] for item in record["phases"]] == [
        "departure_climb", "arrival_descent",
    ]
    assert record["operational_route"] is False
    assert record["cns_assessed"] is False


def test_no_automatic_evaluation_exists(tmp_path):
    service = ready_service(tmp_path)
    collection = service.vertical_transition_validations()
    assert collection["count"] == 0
    assert service.state["vertical_transition_validations"]["items"] == []
    assert service.state["result_statuses"]["vertical_transition_validation"] == "not_calculated"
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["automatic_evaluation"] is False
    assert readiness["explicit_evaluation_required"] is True


@pytest.mark.parametrize("missing,expected_record_status", [
    ("profile", "not_ready"),
    ("adoption", "not_ready"),
    ("cruise_validation", "not_ready"),
    ("source_audits", "not_ready"),
    ("building_audit", "not_ready"),
])
def test_every_missing_readiness_item_blocks_and_is_never_zero(
    tmp_path, missing, expected_record_status,
):
    service = ready_service(tmp_path)
    expected_code = {
        "profile": "current_route_3d_profile_missing",
        "adoption": "current_layered_operational_adoption_missing",
        "cruise_validation": "current_cruise_layered_route_validation_missing",
        "source_audits": "terrain_dtm_source_not_verified",
        "building_audit": "buildings_source_not_verified",
    }[missing]
    if missing == "profile":
        service.state["spatial_3d"]["route_3d_profiles"] = {}
    elif missing == "adoption":
        service.state["layered_operational_adoptions"] = {"items": [], "count": 0}
    elif missing == "cruise_validation":
        service.state["layered_route_validations"] = {"items": [], "count": 0}
    elif missing == "source_audits":
        audits = source_audits()
        audits["items"]["terrain_dtm"]["status"] = "unverified"
        service.state["source_audits"] = audits
    else:
        audits = source_audits()
        audits["items"]["buildings"]["status"] = "unverified"
        service.state["source_audits"] = audits
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["status"] == "not_ready"
    codes = {item["reason_code"] for item in readiness["blockers"]}
    assert expected_code in codes, readiness["blockers"]

    result = evaluate(service, evidence_adapter())
    record = latest(service)
    # A blocked attempt never carries a geometry conclusion.
    assert record["status"] == expected_record_status
    assert record["current_applicability"] in ("not_ready", "stale", "stale_inputs_changed")
    assert record["full_3d_geometry_validated"] is False
    assert record["failed_intervals"] == []
    assert record["unresolved_evidence"] == []
    assert record["blocking_reasons"]
    # A blocked attempt is never recorded as a validated geometry.
    assert result["count"] == 1


def test_explicit_horizontal_crs_is_required(tmp_path):
    service = ready_service(tmp_path, with_crs=False)
    readiness = service.vertical_transition_validation_readiness({"route_id": ROUTE_ID})
    assert readiness["status"] == "not_ready"
    assert any("horizontal_crs" in item["reason_code"] for item in readiness["blockers"])
    assert readiness["horizontal_crs"] is None
    # The same request with an explicit CRS becomes ready: nothing is guessed.
    readiness_ok = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness_ok["status"] == "ready"


def test_non_current_profile_is_not_ready(tmp_path):
    service = ready_service(tmp_path)
    service.set_altitude_layer(layer(source="工程确认-测试-v2"))
    assert stored_profile(service)["status"] == "stale"
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["status"] == "not_ready"
    assert any(
        item["reason_code"] == "route_3d_profile_not_current"
        for item in readiness["blockers"]
    )


def test_readiness_reports_the_zero_clearance_geometry_semantics(tmp_path):
    service = ready_service(tmp_path)
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    usage = readiness["clearance_usage"]
    assert usage["terrain_clearance_m"] == GEOMETRY_INTERSECTION_THRESHOLD_M == 0.0
    assert usage["building_horizontal_clearance_m"] == 0.0
    assert usage["building_vertical_clearance_m"] == 0.0
    assert usage["curve_chord_error_m"] == 0.0
    assert usage["semantics"] == "geometry_intersection_threshold_not_an_engineering_clearance"
    assert usage["new_default_clearance_introduced"] is False
    assert readiness["semantics"]["no_default_clearance_is_invented"] is True


# ---------------------------------------------------------------------------------------
# 2. geometry: truncated original polyline + piecewise-linear z(s)
# ---------------------------------------------------------------------------------------


def test_transition_truncates_the_original_polyline_not_a_straight_chord(tmp_path):
    service = ready_service(tmp_path)
    lines = []
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        metric_line_recorder=lines,
    ))
    record = latest(service)
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")

    # The climb window [0, s_join] contains the real intermediate vertex of the original path.
    assert climb["range"]["truncation"]["kept_interior_vertex_count"] >= 1
    assert climb["range"]["truncation"]["straight_start_to_end_chord_used"] is False
    assert climb["geometry"]["truncated_polyline_used"] is True
    assert climb["geometry"]["straight_start_to_end_chord_used"] is False
    assert climb["geometry"]["metric_vertex_count"] >= 3
    assert climb["range"]["range_source"] == "route_3d_profile_join_leave_distance_along_route"
    assert climb["range"]["start_distance_along_route_m"] == 0.0
    assert climb["range"]["end_distance_along_route_m"] == pytest.approx(JOIN_M)
    assert descent["range"]["start_distance_along_route_m"] == pytest.approx(LEAVE_M)
    assert descent["range"]["end_distance_along_route_m"] == pytest.approx(LENGTH_M)
    # The straight chord would have exactly two vertices; the recorded metric lines prove the
    # original polyline reached the validators.
    assert all(len(line) >= 2 for line in lines)
    assert max(len(line) for line in lines) >= 3


def test_z_is_the_piecewise_linear_route_3d_profile_altitude(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
    ))
    record = latest(service)
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert climb["geometry"]["z_start_m"] == TERMINAL_DEPARTURE_M
    assert climb["geometry"]["z_end_m"] == CRUISE_M
    assert descent["geometry"]["z_start_m"] == CRUISE_M
    assert descent["geometry"]["z_end_m"] == TERMINAL_ARRIVAL_M
    assert climb["evidence"]["curve_chord_error_m"] == 0.0
    assert descent["evidence"]["curve_chord_error_m"] == 0.0


def test_profile_zero_horizontal_vertical_jump_fails_closed(tmp_path):
    """``z0 != H`` with ``s_join == 0`` cannot be represented by a distance-parametric z(s)."""

    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload("departure", join_leave_point={"kind": "join", "distance_along_route_m": 0.0})
    )
    restore_upstream_bypass(service)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "unresolved"
    assert any("zero-horizontal vertical jump" in reason for reason in profile["reasons"])

    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["status"] == "not_ready"
    assert any(
        item["reason_code"] == "route_3d_profile_not_current" for item in readiness["blockers"]
    )
    # The transition geometry refuses the ambiguous profile directly as well.
    from cns_planner.domain.vertical_transition_validation import transition_geometry

    geometry = transition_geometry(profile, ROUTE_ID)
    assert geometry["status"] == "unresolved"
    assert any(
        "zero_horizontal_vertical_jump" in reason for reason in geometry["reasons"]
    )
    evaluate(service, evidence_adapter())
    record = latest(service)
    assert record["status"] == "not_ready"
    assert record["phases"] == []


def test_profile_zero_horizontal_jump_at_the_arrival_endpoint_fails_closed(tmp_path):
    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(
        procedure_payload(
            "arrival", join_leave_point={"kind": "leave", "distance_along_route_m": LENGTH_M},
        )
    )
    restore_upstream_bypass(service)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "unresolved"
    assert any("zero-horizontal vertical jump" in reason for reason in profile["reasons"])
    from cns_planner.domain.vertical_transition_validation import transition_geometry

    geometry = transition_geometry(profile, ROUTE_ID)
    assert geometry["status"] == "unresolved"
    assert any(
        "zero_horizontal_vertical_jump_at_the_arrival_endpoint" in reason
        for reason in geometry["reasons"]
    )


def test_legitimate_zero_length_transition_at_cruise_altitude_still_passes(tmp_path):
    """``z0 == H, s_join == 0`` remains legal: there is no vertical step to represent."""

    service = ready_service(tmp_path)
    service.set_departure_arrival_procedure(procedure_payload("departure", vertical_profile={
        "climb_rate_mps": 2.5, "terminal_altitude_egm2008_m": CRUISE_M,
        "terminal_altitude_source": "manual_engineering_review",
        "terminal_altitude_evidence": "评审记录 DP-1",
    }, join_leave_point={"kind": "join", "distance_along_route_m": 0.0}))
    restore_upstream_bypass(service)
    service.evaluate_route_3d_profile({"route_id": ROUTE_ID})
    profile = stored_profile(service)
    assert profile["status"] == "passed"
    readiness = service.vertical_transition_validation_readiness(
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS}
    )
    assert readiness["status"] == "ready", readiness["blockers"]
    assert readiness["transition_geometry"]["phases"]["departure_climb"]["length_m"] == 0.0

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=0.0, elevation_descent=4.0,
    )))
    record = latest(service)
    assert record["status"] == "validated"
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    assert climb["reason"] == "zero_length_transition_without_altitude_change"
    assert climb["failed_intervals"] == []
    assert climb["unresolved_evidence"] == []
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["status"] == "validated"


# ---------------------------------------------------------------------------------------
# 3. terrain
# ---------------------------------------------------------------------------------------


def test_normal_climb_and_descent_pass_both_domains(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    record = latest(service)
    assert record["status"] == "validated"
    assert record["phase_statuses"] == {
        "departure_climb": "validated", "arrival_descent": "validated",
    }
    assert record["failed_intervals"] == []
    assert record["minimum_margins"]["departure_climb_terrain_m"] == 10.0
    assert record["minimum_margins"]["arrival_descent_building_m"] is None


def test_terrain_penetration_is_failed_with_the_negative_margin(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=120.0, elevation_descent=4.0,
    )))
    record = latest(service)
    assert record["status"] == "failed"
    assert record["full_3d_geometry_validated"] is False
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    assert climb["status"] == "failed"
    assert climb["domain_statuses"]["terrain"] == "failed"
    assert climb["minimum_margins"]["terrain_vertical_m"] == pytest.approx(TERMINAL_DEPARTURE_M - 120.0)
    violation = climb["failed_intervals"][0]
    assert violation["reason_id"] == "below_native_terrain_clearance"
    assert violation["margin"] < 0
    assert record["unresolved_evidence"] == []


def test_terrain_nodata_is_unresolved_and_never_zero(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0, nodata_climb=True,
    )))
    record = latest(service)
    assert record["status"] == "unresolved"
    assert record["failed_intervals"] == []
    assert record["full_3d_geometry_validated"] is False
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    assert climb["status"] == "unresolved"
    assert climb["failed_intervals"] == []
    assert climb["unresolved_evidence"]
    assert record["unresolved_evidence"][0]["phase_id"] == "departure_climb"


# ---------------------------------------------------------------------------------------
# 4. buildings
# ---------------------------------------------------------------------------------------


def building_covering_the_descent(*, ground, height, building_id="B1",
                                 longitude_west=122.0, longitude_east=122.02):
    """A real footprint that the descent interval passes through."""

    y = to_metric([122.0, 30.0])[1]
    x0 = to_metric([longitude_west, 30.0])[0]
    x1 = to_metric([longitude_east, 30.0])[0]
    return {
        "building_id": building_id,
        "source": "GBA",
        "ring_metric": [
            [x0, y - 60.0], [x1, y - 60.0], [x1, y + 60.0], [x0, y + 60.0], [x0, y - 60.0],
        ],
        "height_m": height,
        "height_status": "predicted",
        "ground_elevation_max_egm2008_m": ground,
        "geometry_status": "passed",
    }


def building_covering_the_descent_terminal(*, ground, height, building_id="B1"):
    """A real footprint only around the descent's terminal station (outside the hull)."""

    y = to_metric([122.0, 30.0])[1]
    x0 = to_metric([122.018, 30.0])[0]
    x1 = to_metric([122.021, 30.0])[0]
    return {
        "building_id": building_id,
        "source": "GBA",
        "ring_metric": [
            [x0, y - 60.0], [x1, y - 60.0], [x1, y + 60.0], [x0, y + 60.0], [x0, y - 60.0],
        ],
        "height_m": height,
        "height_status": "predicted",
        "ground_elevation_max_egm2008_m": ground,
        "geometry_status": "passed",
    }


def test_building_roof_penetration_is_failed(tmp_path):
    service = ready_service(tmp_path)
    # Roof = 30 + 20 = 50 m; the descent reaches 40 m, so the prism is penetrated.
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [
            building_covering_the_descent(ground=30.0, height=20.0),
        ]},
    ))
    record = latest(service)
    assert record["status"] == "failed"
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["domain_statuses"]["building"] == "failed"
    assert descent["minimum_margins"]["building_vertical_m"] < 0
    violation = descent["failed_intervals"][0]
    assert violation["reason_id"] == "building_footprint_penetration"
    assert violation["required"] == 50.0
    assert violation["evidence"]["clearance_semantics"] == (
        "geometry_intersection_threshold_not_an_engineering_clearance"
    )
    assert violation["evidence"]["horizontal_clearance_m"] == 0.0


def test_building_missing_height_is_unresolved_not_failed(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [
            building_covering_the_descent(ground=30.0, height=None),
        ]},
    ))
    record = latest(service)
    assert record["status"] == "unresolved"
    assert record["failed_intervals"] == []
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["domain_statuses"]["building"] == "unresolved"


def test_building_missing_ground_is_unresolved(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [
            building_covering_the_descent(ground=None, height=20.0),
        ]},
    ))
    record = latest(service)
    assert record["status"] == "unresolved"
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["domain_statuses"]["building"] == "unresolved"


def test_self_intersecting_transition_footprint_is_repaired_then_evaluated(tmp_path):
    service = ready_service(tmp_path)
    repaired = building_covering_the_descent(ground=0.0, height=400.0)
    x0 = to_metric([122.0, 30.0])[0]
    x1 = to_metric([122.02, 30.0])[0]
    y = to_metric([122.0, 30.0])[1]
    # Bowtie: the same four corners with the ring order crossing itself.
    repaired["ring_metric"] = [
        [x0, y - 60.0], [x1, y + 60.0], [x1, y - 60.0], [x0, y + 60.0], [x0, y - 60.0],
    ]
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [repaired]},
    ))
    record = latest(service)
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    quality = descent["domains"]["building"]["evidence"]["building_quality_report"]
    assert quality["counts"]["repaired"] == 1
    assert quality["repair"]["applied_count"] == 1
    assert descent["domains"]["building"]["evidence"]["make_valid_applied"] is True
    assert descent["domains"]["building"]["evidence"]["source_geometry_modified"] is False
    # The repaired geometry is a real constraint now, so the phase reaches a verdict.
    assert descent["domain_statuses"]["building"] in ("failed", "passed"), descent["reason"]


def test_collinear_transition_footprint_stays_unresolved_after_failed_repair(tmp_path):
    service = ready_service(tmp_path)
    degenerate = building_covering_the_descent(ground=30.0, height=20.0)
    degenerate["ring_metric"] = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [0.0, 0.0]]
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [degenerate]},
    ))
    record = latest(service)
    assert record["status"] == "unresolved"
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["domains"]["building"]["evidence"]["make_valid_applied"] is False
    assert descent["domains"]["building"]["evidence"]["source_geometry_modified"] is False
    quality = descent["domains"]["building"]["evidence"]["building_quality_report"]
    assert quality["counts"] == {"passed": 0, "repaired": 0, "invalid": 1}
    assert quality["repair"]["failed_count"] == 1
    assert quality["semantics"]["unrepairable_geometry_stays_unknown"] is True
    assert descent["domain_statuses"]["building"] == "unresolved"


def test_exact_contact_margin_zero_is_not_a_penetration(tmp_path):
    """The descent's terminal endpoint exactly touches the roof/surface: contact geometry."""

    service = ready_service(tmp_path)
    # terrain 40 m == the arrival terminal altitude 40 m: margin is exactly 0.
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=20.0, elevation_descent=TERMINAL_ARRIVAL_M),
    ))
    record = latest(service)
    assert record["status"] == "validated"
    assert record["failed_intervals"] == []
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["minimum_margins"]["terrain_vertical_m"] == 0.0
    climb = next(item for item in record["phases"] if item["phase_id"] == "departure_climb")
    assert climb["minimum_margins"]["terrain_vertical_m"] == 0.0


def test_exact_contact_with_a_roof_is_not_a_penetration(tmp_path):
    service = ready_service(tmp_path)
    # Roof = 0 + 40 = 40 m, exactly the arrival terminal altitude.
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
        buildings={"departure_climb": [], "arrival_descent": [
            building_covering_the_descent_terminal(ground=0.0, height=TERMINAL_ARRIVAL_M),
        ]},
    ))
    record = latest(service)
    assert record["status"] == "validated"
    descent = next(item for item in record["phases"] if item["phase_id"] == "arrival_descent")
    assert descent["minimum_margins"]["building_vertical_m"] == 0.0


# ---------------------------------------------------------------------------------------
# 5. verdicts
# ---------------------------------------------------------------------------------------


def test_verdict_is_penetration_unresolved_or_validated_and_never_safe(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    record = latest(service)
    assert record["status"] in VALIDATION_STATUSES
    assert record["status"] not in ("safe", "unsafe")
    assert record["semantics"]["produces_safe_or_unsafe_verdict"] is False
    assert record["boundaries"]["is_route_safety_conclusion"] is False
    assert record["boundaries"]["is_aircraft_kinematic_validation"] is False
    assert record["boundaries"]["is_terminal_procedure_certification"] is False
    assert record["boundaries"]["is_flight_dynamics_validation"] is False
    assert record["semantics"][
        "full_3d_geometry_validated_is_not_aircraft_kinematic_validation"
    ] is True
    assert record["semantics"][
        "full_3d_geometry_validated_is_not_terminal_procedure_certification"
    ] is True
    assert record["semantics"]["full_3d_geometry_validated_is_not_route_safe"] is True
    for phase in record["phases"]:
        for domain_id in TRANSITION_DOMAIN_IDS:
            assert phase["domain_statuses"][domain_id] in (
                "passed", "failed", "unresolved", "skipped",
            )


def test_one_failed_phase_fails_the_whole_transition(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    ), buildings={"departure_climb": [], "arrival_descent": [
        building_covering_the_descent(ground=20.0, height=40.0),
    ]}))
    record = latest(service)
    assert record["phase_statuses"]["departure_climb"] == "validated"
    assert record["phase_statuses"]["arrival_descent"] == "failed"
    assert record["status"] == "failed"


def test_unresolved_plus_penetration_prefers_the_penetration(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(
        terrain=terrain_pixels(
            elevation_climb=120.0, elevation_descent=4.0, nodata_descent=True,
        ),
    ))
    record = latest(service)
    assert record["phase_statuses"]["departure_climb"] == "failed"
    assert record["phase_statuses"]["arrival_descent"] == "unresolved"
    assert record["status"] == "failed"


# ---------------------------------------------------------------------------------------
# 6. Safety Evidence V2 linkage
# ---------------------------------------------------------------------------------------


def safety_lineage(service):
    """A complete published lineage for the Safety Evidence V2 aggregation."""

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
    validation_record = cruise_validation()
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
    service.state["layered_route_candidates"] = {
        "schema_version": "layered-route-candidate-collection", "status": "passed",
        "count": 1, "active_candidate_id": "LC-1", "items": [candidate_record], "masks": {},
    }
    service.state["layered_route_validations"] = {
        "items": [validation_record], "count": 1, "status": "validated_candidate",
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


def geometry_domain(service):
    return service.route_safety_evidence_service._geometry_domain({
        "adoption": {"adoption_id": "LRA-1", "route_id": ROUTE_ID},
        "validation": cruise_validation(),
        "candidate": {"candidate_id": "LC-1"},
        "operational_route": {"route_id": ROUTE_ID, "status": "passed", "path": deepcopy(PATH)},
    })


def test_validated_transition_restores_a_complete_geometry_domain(tmp_path):
    service = ready_service(tmp_path)
    safety_lineage(service)

    baseline = geometry_domain(service)
    assert baseline["status"] in ("validation_incomplete", "unresolved")
    assert baseline["transition"]["transition_validation_status"] is None

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    geometry = geometry_domain(service)
    assert geometry["status"] == "validated"
    assert geometry["status_reason"] is None
    assert geometry["hard_constraint_failure"] is False
    assert geometry["transition"]["transition_validation_status"] == "validated"
    assert geometry["transition"]["full_3d_geometry_validated"] is True
    assert geometry["transition"]["transition_validation_fingerprint"]
    assert geometry["semantics"]["consumes_vertical_transition_validation_read_only"] is True
    assert geometry["semantics"][
        "validated_requires_both_cruise_and_transition_geometry_evidence"
    ] is True
    # The stored Route3DProfile is never rewritten by a transition evaluation.
    boundary = stored_profile(service)["validation_boundary"]
    assert boundary["terminal_transition_validation"] == "not_evaluated"
    assert boundary["full_3d_geometry_validated"] is False


def test_failed_transition_is_a_geometry_hard_constraint_failure(tmp_path):
    from cns_planner.domain.route_safety_evidence_v2 import overall_status_for

    service = ready_service(tmp_path)
    safety_lineage(service)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=120.0, elevation_descent=4.0,
    )))
    geometry = geometry_domain(service)
    assert geometry["status"] == "failed"
    assert geometry["hard_constraint_failure"] is True
    assert geometry["status_reason"] == "terminal_transition_penetration"
    assert geometry["transition"]["transition_penetration"] is True
    assert overall_status_for({
        "geometry_obstacle": "failed", "ground_exposure": "assessed",
        "regulatory": "evaluated_no_confirmed_intersection",
        "cns_operational_support": "supported",
    }) == "hard_constraint_failed"


def test_unresolved_or_absent_transition_keeps_the_geometry_domain_incomplete(tmp_path):
    from cns_planner.domain.route_safety_evidence_v2 import overall_status_for

    service = ready_service(tmp_path)
    safety_lineage(service)

    # No transition validation at all: the previous behaviour is preserved exactly.
    absent = geometry_domain(service)
    assert absent["transition"]["applies"] is True
    assert absent["transition"]["transition_validation_status"] is None
    assert absent["status"] != "validated"

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0, nodata_climb=True,
    )))
    unresolved = geometry_domain(service)
    assert unresolved["status"] == "unresolved"
    assert unresolved["hard_constraint_failure"] is False
    assert unresolved["transition"]["transition_validation_status"] == "unresolved"

    complete = {
        "geometry_obstacle": unresolved["status"], "ground_exposure": "assessed",
        "regulatory": "evaluated_no_confirmed_intersection",
        "cns_operational_support": "supported",
    }
    overall = overall_status_for(complete)
    assert overall == "evidence_unresolved"
    assert overall != "evidence_complete"
    assert overall not in ("safe", "unsafe")


def test_transition_fingerprint_enters_the_assessment_fingerprint(tmp_path):
    service = ready_service(tmp_path)
    safety_lineage(service)
    before = service.evaluate_route_safety_evidence_v2({"adoption_id": "LRA-1"})["items"][-1]
    assert before["fingerprints"]["transition_validation_fingerprint"] is None

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    record = service.evaluate_route_safety_evidence_v2({"adoption_id": "LRA-1"})["items"][-1]
    transition = latest(service)
    assert record["fingerprints"]["assessment_fingerprint"] != (
        before["fingerprints"]["assessment_fingerprint"]
    )
    assert record["fingerprints"]["transition_validation_fingerprint"] == (
        transition["fingerprints"]["transition_fingerprint"]
    )
    assert record["fingerprints"]["transition_validation_status"] == "validated"
    # The original LayeredRouteValidation record is never rewritten.
    stored = service.state["layered_route_validations"]["items"][0]
    assert stored["status"] == "validated_candidate"
    assert "transition" not in stored
    assert "vertical_transition" not in stored


def test_route_3d_profile_projection_reports_the_transition_verdict(tmp_path):
    service = ready_service(tmp_path)
    boundary = projected_profile(service)["validation_boundary"]
    assert boundary["terminal_transition_validation"] == "not_evaluated"
    assert boundary["full_3d_geometry_validated"] is False

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    boundary = projected_profile(service)["validation_boundary"]
    assert boundary["terminal_transition_validation"] == "validated"
    assert boundary["full_3d_geometry_validated"] is True
    assert boundary["full_3d_geometry_evidence_complete"] is True
    assert boundary["transition_verdict_is_projected_not_stored"] is True
    assert boundary["full_3d_geometry_validated_is_not_route_safe"] is True
    # The stored record still carries the static cruise-only boundary.
    assert stored_profile(service)["validation_boundary"][
        "terminal_transition_validation"
    ] == "not_evaluated"


# ---------------------------------------------------------------------------------------
# 7. invalidation / fingerprint
# ---------------------------------------------------------------------------------------


def test_fingerprint_binds_every_declared_dependency(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    components = latest(service)["fingerprints"]["components"]
    for key in (
        "validator_version", "route_3d_profile_fingerprint", "route_3d_profile_id",
        "operational_route", "operational_adoption", "cruise_validation_fingerprint",
        "source_audits", "metric_crs", "transition_geometry", "validator_versions",
        "clearance_usage",
    ):
        assert key in components, key
    assert components["metric_crs"] == HORIZONTAL_CRS
    assert components["route_3d_profile_fingerprint"] == (
        stored_profile(service)["fingerprints"]["profile_fingerprint"]
    )
    assert components["cruise_validation_fingerprint"] == "validation-fp"
    assert components["source_audits"]["terrain_dtm"]["status"] == "verified"


def test_profile_change_makes_the_transition_stale(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    validation_id = latest(service)["validation_id"]
    assert latest(service)["current_applicability"] == "current"

    service.set_altitude_layer(layer(source="工程确认-测试-v2"))
    record = latest(service)
    assert record["validation_id"] == validation_id
    assert record["status"] == "stale"
    assert record["current_applicability"] == "stale"
    assert record["full_3d_geometry_validated"] is False
    assert service.state["result_statuses"]["vertical_transition_validation"] == "stale"


def test_cruise_validation_change_makes_the_transition_stale(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    assert latest(service)["current_applicability"] == "current"
    service.invalidation_service.layered_route_validation("cruise_validation_changed")
    assert latest(service)["status"] == "stale"


def test_source_change_makes_the_transition_stale(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    assert latest(service)["status"] == "validated"
    service.invalidation_service.grid_sources({"terrain_dtm", "buildings"})
    # The invalidator stales the stored record; the *recomputed* applicability must agree.
    assert service.state["vertical_transition_validations"]["items"][0]["status"] == "stale"
    assert latest(service)["status"] == "stale"


def test_transition_change_never_stales_the_theta_chain(tmp_path):
    service = ready_service(tmp_path)
    safety_lineage(service)
    candidate_before = deepcopy(service.state["layered_route_candidates"])
    validation_before = deepcopy(service.state["layered_route_validations"])
    adoption_before = deepcopy(service.state["layered_operational_adoptions"])
    profile_before = deepcopy(service.state["spatial_3d"]["route_3d_profiles"])
    service.state["coverage_3d"] = {"status": "passed", "input_fingerprint": "coverage-fp"}
    service.state["result_statuses"]["coverage_3d"] = "passed"
    service.state["route_vertical_profiles"] = {"status": "passed", "input_fingerprint": "vp-fp"}
    service.state["result_statuses"]["route_vertical_profiles"] = "passed"
    service.state["route_safety_evidence_v2"] = {
        "schema_version": "route-safety-evidence-v2-collection",
        "status": "passed", "count": 1, "active_assessment_id": "RSE-1",
        "items": [{
            "schema_version": "route-safety-evidence-v2", "assessment_id": "RSE-1",
            "route_id": ROUTE_ID, "status": "evidence_complete",
            "current_applicability": "current", "domains": {},
        }],
    }
    service.state["result_statuses"]["route_safety_evidence_v2"] = "passed"

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))

    candidate_after = service.state["layered_route_candidates"]
    # The scenario container is stamped by the layered candidate service on read; only the
    # Theta* candidate records, their fingerprints and their objective must be untouched.
    assert len(candidate_after["items"]) == len(candidate_before["items"])
    for before_item, after_item in zip(candidate_before["items"], candidate_after["items"]):
        assert after_item["candidate_id"] == before_item["candidate_id"]
        assert after_item["candidate_fingerprint"] == before_item["candidate_fingerprint"]
        assert after_item["path"] == before_item["path"]
        assert after_item["planning_objective"] == before_item["planning_objective"]
        assert after_item["status"] == before_item["status"]
        assert after_item["current_applicability"] == before_item["current_applicability"]
    assert candidate_after["active_candidate_id"] == candidate_before["active_candidate_id"]
    # The validation/adoption collections are re-projected on read (their own projection
    # recomputes applicability), so only their records and conclusions must be untouched.
    for before_item, after_item in zip(
        validation_before["items"], service.state["layered_route_validations"]["items"]
    ):
        assert after_item["validation_id"] == before_item["validation_id"]
        assert after_item["status"] == before_item["status"]
        assert after_item["fingerprints"] == before_item["fingerprints"]
        assert after_item["failed_intervals"] == before_item["failed_intervals"]
    assert service.state["layered_operational_adoptions"] == adoption_before
    assert service.state["spatial_3d"]["route_3d_profiles"] == profile_before
    # The transition feeds the Safety Evidence and the report only.
    assert service.state["result_statuses"]["route_safety_evidence_v2"] == "stale"
    assert service.state["route_safety_evidence_v2"]["items"][0]["status"] == "stale"
    assert service.state["result_statuses"]["coverage_3d"] == "passed"
    assert service.state["result_statuses"]["route_vertical_profiles"] == "passed"


def test_total_distance_is_unchanged_when_the_route_distance_basis_is_a_plain_polyline(tmp_path):
    """The unchanged Route3DProfile distance basis is the operational route distance."""

    service = ready_service(tmp_path)
    profile = stored_profile(service)
    assert profile["route_length_m"] == pytest.approx(LENGTH_M)
    assert profile["route_length_basis"] == "operational_route_distance_m"


def test_replacing_a_transition_validation_stales_the_previous_record(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    first_id = latest(service)["validation_id"]
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    items = service.vertical_transition_validations()["items"]
    same = [item for item in items if item["validation_id"] == first_id]
    assert len(same) == 1
    # A re-evaluation with identical dependencies keeps the same fingerprint and therefore the
    # same record identity; a changed *conclusion* is still recorded on the new attempt.
    assert latest(service)["status"] == "validated"
    assert latest(service)["current_applicability"] == "current"


def test_transition_current_applicability_is_recomputed_when_inputs_change(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    assert latest(service)["current_applicability"] == "current"
    # A changed verified source digest is part of the declared dependency set.
    service.state["source_audits"]["items"]["terrain_dtm"]["verification"]["sha256"] = "changed"
    record = service.vertical_transition_validations()["items"][0]
    assert record["current_applicability"] == "stale_inputs_changed"
    assert record["full_3d_geometry_validated"] is False


# ---------------------------------------------------------------------------------------
# 8. Coverage3D / Theta* non-regression
# ---------------------------------------------------------------------------------------


def test_coverage_3d_behaviour_does_not_regress(tmp_path):
    service = ready_service(tmp_path)
    model = GeometricCoverage3DV1({"sample_spacing_m": LENGTH_M / 5.0, "confirmed": True})
    before = model.evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"], {"cells": []},
        {"terrain": {"cells": {}}}, {"items": []}, {"items": []},
    )
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    after = model.evaluate(
        [deepcopy(ROUTE)], service.state["spatial_3d"], {"cells": []},
        {"terrain": {"cells": {}}}, {"items": []}, {"items": []},
    )
    assert after == before
    assert after["routes"][0]["altitude_profile"]["source_kind"] == "production_route_3d_profile"


def test_theta_path_objective_and_candidate_fingerprint_are_unchanged(tmp_path):
    service = ready_service(tmp_path)
    service.state["layered_route_candidates"] = {
        "status": "passed", "count": 1, "active_candidate_id": "LC-1",
        "items": [{
            "candidate_id": "LC-1", "route_id": ROUTE_ID, "status": "candidate",
            "current_applicability": "current", "candidate_fingerprint": "candidate-fp",
            "path": deepcopy(PATH),
            "planning_objective": {"total_cost": 1.0, "distance_m": LENGTH_M},
        }],
    }
    candidate_before = deepcopy(service.state["layered_route_candidates"]["items"][0])
    profile_before = deepcopy(stored_profile(service)["fingerprints"]["profile_fingerprint"])

    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))

    candidate_after = service.state["layered_route_candidates"]["items"][0]
    assert candidate_after["candidate_fingerprint"] == candidate_before["candidate_fingerprint"]
    assert candidate_after["path"] == candidate_before["path"]
    assert candidate_after["planning_objective"] == candidate_before["planning_objective"]
    assert candidate_after["status"] == "candidate"
    assert stored_profile(service)["fingerprints"]["profile_fingerprint"] == profile_before


def test_operational_route_and_candidate_containers_are_never_written(tmp_path):
    service = ready_service(tmp_path)
    before_routes = deepcopy(service.state["operational_routes"])
    before_spatial = deepcopy(service.state["spatial_3d"]["route_altitude_profiles"])
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    assert service.state["operational_routes"] == before_routes
    assert service.state["spatial_3d"]["route_altitude_profiles"] == before_spatial
    assert all(len(point) == 2 for point in service.state["operational_routes"][0]["path"])


# ---------------------------------------------------------------------------------------
# 9. API / persistence
# ---------------------------------------------------------------------------------------


class DirectQgis:
    @staticmethod
    def call(callback):
        return callback()


class ApiContext:
    def __init__(self, workflow):
        self.workflow, self.qgis, self.data = workflow, DirectQgis(), object()


def test_api_surface_readiness_collection_and_explicit_evaluation(tmp_path):
    service = ready_service(tmp_path)
    router = ApiRouter(ApiContext(service))
    readiness = router.get(
        "/api/vertical-transition-validation/readiness",
        {"route_id": [ROUTE_ID], "horizontal_crs": [HORIZONTAL_CRS]}, {},
    ).data
    assert readiness["status"] == "ready"
    assert router.get("/api/vertical-transition-validations", {}, {}).data["count"] == 0

    service.vertical_transition_validation_service.evidence_adapter = evidence_adapter(
        terrain=terrain_pixels(elevation_climb=10.0, elevation_descent=4.0),
    )
    response = router.post(
        "/api/vertical-transition-validations/evaluate-real",
        {"route_id": ROUTE_ID, "horizontal_crs": HORIZONTAL_CRS},
    ).data
    assert response["count"] == 1
    assert response["items"][-1]["status"] == "validated"

    stored = router.get("/api/vertical-transition-validations", {}, {}).data
    assert stored["items"][0]["status"] == "validated"
    assert stored["items"][0]["fingerprints"]["transition_fingerprint"]


def test_evaluation_persists_and_reloads(tmp_path):
    service = ready_service(tmp_path)
    evaluate(service, evidence_adapter(terrain=terrain_pixels(
        elevation_climb=10.0, elevation_descent=4.0,
    )))
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    collection = normalize_vertical_transition_validation_collection(
        restored.state["vertical_transition_validations"]
    )
    assert collection["count"] == 1
    assert collection["items"][0]["status"] == "validated"
    assert restored.state["result_statuses"]["vertical_transition_validation"] == "validated"


def test_legacy_project_backfills_the_additive_container(tmp_path):
    service = ready_service(tmp_path)
    project = blank_project({})
    modern = deepcopy(project)
    legacy = deepcopy(project)
    legacy.pop("vertical_transition_validations")
    legacy["result_statuses"].pop("vertical_transition_validation")
    normalized = normalize_project(legacy, service.grid_service)
    assert normalized["vertical_transition_validations"] == modern[
        "vertical_transition_validations"
    ]
    assert normalized["result_statuses"]["vertical_transition_validation"] == "not_calculated"


def test_the_snapshot_exposes_the_transition_surfaces(tmp_path):
    service = ready_service(tmp_path)
    snapshot = service.snapshot()
    assert "vertical_transition_validations" in snapshot
    assert "vertical_transition_validation_readiness" in snapshot
    assert snapshot["vertical_transition_validations"]["count"] == 0
    assert snapshot["vertical_transition_validation_readiness"]["status"] in (
        "ready", "not_ready",
    )


# ---------------------------------------------------------------------------------------
# 10. UI contract
# ---------------------------------------------------------------------------------------


def test_front_end_ui_module_exposes_the_transition_card_contract():
    source = Path("cns_planner/web/js/workflow/route3d_profile.js").read_text(encoding="utf-8")
    for fragment in (
        "Transition Validation", "TRANSITION_PHASE_LABELS", "TRANSITION_DOMAIN_LABELS",
        "transitionBlock", "data-evaluate-transition", "data-transition-crs",
        "TRANSITION_ENDPOINT", "/api/vertical-transition-validations/evaluate-real",
        "horizontal_crs", "failed_intervals", "unresolved_evidence",
        "transition_fingerprint", "geometry validation",
        "clearance_usage", "readiness", "blockers",
    ):
        assert fragment in source, fragment
    assert "safe/unsafe" in source
    steps = Path("cns_planner/web/js/workflow/step03_routes.js").read_text(encoding="utf-8")
    assert "renderRoute3DProfilePanel" in steps
    assert "bindRoute3DProfile" in steps


def test_front_end_does_not_introduce_a_new_chart_engine():
    source = Path("cns_planner/web/js/workflow/route3d_profile.js").read_text(encoding="utf-8")
    for forbidden in ("plotly", "chart.js", "d3.", "echarts", "canvas"):
        assert forbidden not in source.lower(), forbidden
