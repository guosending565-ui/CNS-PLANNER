"""Route-planning experiment isolation, geodesic metrics and reference CRS semantics.

The central guarantee under test: running a planner comparison is an *experiment*.
It must never switch ``algorithm_selection`` and never overwrite ``operational_routes``.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import build_default_algorithm_registry
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.benchmark.geodesy import (
    GEODESIC_BACKEND, geodesic_bearing_deg, geodesic_distance_m, heading_change_deg,
    is_geographic_crs,
)
from cns_planner.benchmark.quality import (
    HEADING_CHANGE_TOLERANCE_DEG, METRIC_SEMANTICS, polyline_metrics,
)
from cns_planner.domain.experiment import (
    experiment_id_for, normalize_experiments, scenario_fingerprint,
)
from cns_planner.domain.reference_crs import (
    CRS84, empty_crs_record, is_resolved, normalize_crs_record, unresolved_reason,
)
from cns_planner.domain.reference_route_link import (
    blocked_endpoint_candidates, endpoint_candidate, normalize_reference_route_links,
)
from cns_planner.reference_data import load_reference_landing_sites, load_reference_routes

DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [122.0, 29.9, 122.2, 30.1]
PLANNER_V1 = "route_planner_v1"
PLANNER_V2 = "risk_aware_route_planner_v2"


def workflow_with_routes(tmp_path, name="project.json"):
    workflow = WorkflowService(tmp_path / name, DEFAULTS)
    workflow.set_workspace(WORKSPACE, {"status": "passed"}, 8)
    workflow.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.02, 29.92]},
        {"node_id": "N002", "name": "B", "coordinate": [122.18, 30.08]},
    ]
    workflow.state["node_seq"] = 2
    workflow.generate_scenario_od("N001", "N002", "ab")
    return workflow


def run_experiment(workflow, planners=None):
    payload = {} if planners is None else {"planners": planners}
    return workflow.evaluate_route_experiment(payload)


# --------------------------------------------------------------------------------------
# 1. experiment isolation
# --------------------------------------------------------------------------------------


def test_v1_and_v2_experiments_are_saved_together_without_touching_operational_routes(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    # Establish a real current operational result first.
    workflow.generate_operational([])
    operational_before = deepcopy(workflow.state["operational_routes"])
    status_before = workflow.state["result_statuses"]["routes"]
    selection_before = deepcopy(workflow.state["algorithm_selection"])

    snapshot = run_experiment(workflow)
    collection = snapshot["route_planning_experiments"]

    assert collection["count"] == 1
    record = collection["active_experiment"]
    run_ids = {run["run_id"] for run in record["runs"]}
    assert any(PLANNER_V1 in run_id for run_id in run_ids)
    assert any(PLANNER_V2 in run_id for run_id in run_ids)
    assert {run["algorithm_id"] for run in record["runs"]} == {PLANNER_V1, PLANNER_V2}

    assert workflow.state["operational_routes"] == operational_before
    assert workflow.state["result_statuses"]["routes"] == status_before
    assert workflow.state["algorithm_selection"] == selection_before


def test_experiment_never_changes_current_planner_even_when_only_v2_requested(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    selection_before = deepcopy(workflow.state["algorithm_selection"]["route_planner"])
    assert selection_before["algorithm_id"] == PLANNER_V1
    run_experiment(workflow, [{"algorithm_id": PLANNER_V2, "version": "2.0"}])
    assert workflow.state["algorithm_selection"]["route_planner"] == selection_before


def test_experiment_records_required_provenance_fields(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    record = run_experiment(workflow)["route_planning_experiments"]["active_experiment"]
    assert record["experiment_id"].startswith("EXP-")
    for field in (
        "experiment_id", "scenario_fingerprint", "input_fingerprints", "planners",
        "runs", "provenance", "created_at", "current_applicability",
    ):
        assert field in record, field
    assert record["provenance"]["operational_routes_untouched"] is True
    assert record["provenance"]["algorithm_selection_untouched"] is True
    for planner in record["planners"]:
        assert planner["algorithm_id"] and planner["algorithm_version"]
        assert "manifest" in planner and "effective_parameters" in planner
    for run in record["runs"]:
        assert run["runtime"]["measurement"]
        assert "result" in run and "quality" in run
        assert run["result_snapshot_semantics"] == "frozen_copy_of_planner_output"
        assert run["planner_output_mutated"] is False
    assert record["scenario_fingerprint"] == scenario_fingerprint(workflow.state["scenario_routes"])
    assert record["verdicts"] == {
        "automatically_ranked": False, "automatically_scored": False, "preferred_algorithm": None,
    }


def test_experiment_identity_is_deterministic_for_same_inputs_and_parameters(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    first = run_experiment(workflow)["route_planning_experiments"]["active_experiment_id"]
    second = run_experiment(workflow)["route_planning_experiments"]["active_experiment_id"]
    assert first == second
    # Same scenario, different parameters -> different identity.
    changed = run_experiment(workflow, [
        {"algorithm_id": PLANNER_V1, "version": "1.0"},
        {"algorithm_id": PLANNER_V2, "version": "2.0", "parameters": {"risk_weight_lambda": 5.0}},
    ])["route_planning_experiments"]
    assert changed["active_experiment_id"] != first
    assert changed["count"] == 2


def test_experiment_does_not_mutate_the_stored_planner_result_snapshot(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    record = run_experiment(workflow)["route_planning_experiments"]["active_experiment"]
    for run in record["runs"]:
        if not run["planner_invoked"]:
            continue
        snapshot = deepcopy(run["result"])
        # Re-snapshotting must be stable: the record is frozen evidence.
        again = workflow.route_experiments_snapshot()["active_experiment"]
        same_run = next(item for item in again["runs"] if item["run_id"] == run["run_id"])
        assert same_run["result"] == snapshot
        assert "quality" not in (same_run["result"] or {})


def test_experiment_marks_itself_stale_when_scenario_inputs_change(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    record = run_experiment(workflow)["route_planning_experiments"]["active_experiment"]
    assert record["current_applicability"] == "current"
    workflow.generate_scenario_od("N002", "N001", "ab")
    refreshed = workflow.route_experiments_snapshot()["records"][0]
    assert refreshed["current_applicability"] == "stale_scenario_inputs"
    # Staleness is reported, never destructive: the frozen record is still present.
    assert refreshed["runs"]


def test_experiment_requires_scenario_routes(tmp_path):
    workflow = WorkflowService(tmp_path / "empty.json", DEFAULTS)
    workflow.set_workspace(WORKSPACE, {"status": "passed"}, 8)
    with pytest.raises(ValueError, match="场景航路"):
        workflow.evaluate_route_experiment({})


def test_experiment_rejects_malformed_hard_constraints_without_invoking_planners(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    with pytest.raises(ValueError):
        workflow.evaluate_route_experiment({
            "hard_constraints": [{"name": "非法", "bbox": [122.2, 30.0, 122.0, 30.2]}],
        })
    assert workflow.state["route_planning_experiments"]["count"] == 0


def test_unknown_planner_manifest_fails_explicitly(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    with pytest.raises(Exception):
        run_experiment(workflow, [{"algorithm_id": "route_planner_v3", "version": "3.0"}])


def test_experiment_delete_removes_only_that_record(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    first = run_experiment(workflow)["route_planning_experiments"]["active_experiment_id"]
    run_experiment(workflow, [
        {"algorithm_id": PLANNER_V1, "version": "1.0"},
        {"algorithm_id": PLANNER_V2, "version": "2.0", "parameters": {"risk_weight_lambda": 3.0}},
    ])
    snapshot = workflow.delete_route_experiment(first)
    assert snapshot["route_planning_experiments"]["count"] == 1
    assert snapshot["route_planning_experiments"]["active_experiment_id"] != first
    with pytest.raises(ValueError, match="不存在"):
        workflow.delete_route_experiment("EXP-000000000000")


def test_experiment_is_persisted_and_old_schema_backfills(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    run_experiment(workflow)
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["route_planning_experiments"]["count"] == 1

    # A legacy project has no experiment/link collections at all.
    from cns_planner.application.project_state import blank_project, normalize_project

    legacy = blank_project(workflow.defaults)
    legacy.pop("route_planning_experiments")
    legacy.pop("reference_route_links")
    normalized = normalize_project(legacy, workflow.grid_service)
    assert normalized["route_planning_experiments"]["status"] == "not_calculated"
    assert normalized["route_planning_experiments"]["records"] == []
    assert normalized["reference_route_links"]["count"] == 0


def test_normalize_experiments_is_additive_and_validates_identity():
    assert normalize_experiments(None)["count"] == 0
    assert normalize_experiments([])["count"] == 0
    record = {
        "experiment_id": "EXP-0123456789AB", "scenario_fingerprint": "x",
        "planners": [], "runs": [],
    }
    normalized = normalize_experiments({"records": [record]})
    assert normalized["count"] == 1
    assert normalized["records"][0]["verdicts"]["automatically_ranked"] is False
    with pytest.raises(ValueError, match="experiment_id"):
        normalize_experiments({"records": [{"experiment_id": "bad"}]})


def test_experiment_id_helper_is_order_independent_for_parameters():
    first = experiment_id_for("fp", {"a": "v1@1.0"}, {"a": {"lambda": 1}})
    second = experiment_id_for("fp", {"a": "v1@1.0"}, {"a": {"lambda": 1}})
    third = experiment_id_for("fp", {"a": "v1@1.0"}, {"a": {"lambda": 2}})
    assert first == second != third


# --------------------------------------------------------------------------------------
# 2. geodesic measurement semantics
# --------------------------------------------------------------------------------------


def test_geodesic_backend_is_ellipsoidal_when_pyproj_is_present():
    assert GEODESIC_BACKEND == "pyproj.Geod(ellps=WGS84)"
    assert METRIC_SEMANTICS["distance"] == "ellipsoidal_geodesic_distance_m"
    assert METRIC_SEMANTICS["bearing"] == "ellipsoidal_geodesic_azimuth_deg"


def test_heading_is_geodesic_not_naive_atan2_of_degree_deltas():
    # atan2(dlon, dlat) in degree space would report exactly 90.000000 for a due-east
    # leg; the true geodesic azimuth on WGS84 is slightly less because a rhumb-like
    # east leg converges.  The evaluator must return the geodesic value.
    import math

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    for left, right in (
        ([122.0, 30.0], [122.01, 30.0]),
        ([122.0, 30.0], [122.0, 30.01]),
        ([122.0, 30.0], [122.01, 30.01]),
        ([122.0, 29.9], [122.05, 30.05]),
    ):
        expected = geod.inv(left[0], left[1], right[0], right[1])[0] % 360
        assert geodesic_bearing_deg(left, right) == pytest.approx(expected, abs=1e-9)

    naive_east = math.degrees(math.atan2(0.01, 0.0)) % 360
    geodesic_east = geodesic_bearing_deg([122.0, 30.0], [122.01, 30.0])
    assert naive_east == pytest.approx(90.0)
    assert geodesic_east == pytest.approx(89.9975, abs=1e-3)
    assert abs(geodesic_east - naive_east) > 1e-6

    # The 45-degree diagonal in degree space is also not the geodesic azimuth.
    naive_diagonal = math.degrees(math.atan2(0.01, 0.01)) % 360
    assert geodesic_bearing_deg([122.0, 30.0], [122.01, 30.01]) != pytest.approx(
        naive_diagonal, abs=1e-3,
    )


def test_geodesic_distance_matches_pyproj_and_differs_from_degree_naivety():
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    left, right = [122.0, 30.0], [122.01, 30.01]
    expected = geod.inv(left[0], left[1], right[0], right[1])[2]
    assert geodesic_distance_m(left, right) == pytest.approx(expected)
    # A one-degree-latitude span is ~111.13 km, not 111.19 km (spherical).
    assert geodesic_distance_m([122.0, 30.0], [122.0, 31.0]) == pytest.approx(111_000, rel=0.01)
    assert geodesic_distance_m([122.0, 30.0], [122.0, 31.0]) < 111_195


def test_heading_change_is_normalized_and_tolerance_is_recorded():
    assert heading_change_deg(0, 90) == pytest.approx(90.0)
    assert heading_change_deg(350, 10) == pytest.approx(20.0)
    assert heading_change_deg(0, 180) == pytest.approx(180.0)
    assert heading_change_deg(0, 0) == 0.0
    assert HEADING_CHANGE_TOLERANCE_DEG > 0


def test_polyline_metrics_records_method_and_tolerance():
    metrics = polyline_metrics([[122.0, 30.0], [122.01, 30.0], [122.01, 30.01]])
    assert metrics["turn_count"] == 1
    assert metrics["heading_method"] == "geodesic_azimuth_difference_normalized_to_180"
    assert metrics["heading_change_tolerance_deg"] == HEADING_CHANGE_TOLERANCE_DEG
    assert metrics["assumed_input_crs"] == "EPSG:4326"
    assert metrics["metric_semantics"]["backend"] == GEODESIC_BACKEND
    assert metrics["vertex_dedup_tolerance_m"] > 0


def test_geographic_crs_recognition():
    for value in (CRS84, "EPSG:4326", "epsg:4326", "urn:ogc:def:crs:OGC:1.3:CRS84"):
        assert is_geographic_crs(value) is True
    for value in (None, "", "EPSG:4547", "CGCS2000 / 3-degree Gauss-Kruger CM 120E"):
        assert is_geographic_crs(value) is False


# --------------------------------------------------------------------------------------
# 3. reference CRS semantics
# --------------------------------------------------------------------------------------


def reference_csv(tmp_path, name="routes.csv"):
    source = tmp_path / name
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.10,30.10\n"
        "7,测试线,2,B,122.15,30.15\n"
        "7,测试线,3,C,122.20,30.20\n",
        encoding="utf-8-sig",
    )
    return source


def test_pending_source_crs_disables_formal_length_and_metric_similarity(tmp_path):
    result = load_reference_routes(reference_csv(tmp_path))
    assert result["crs"]["source_crs"]["status"] == "pending_confirmation"
    assert result["crs"]["source_crs"]["confirmed"] is False
    assert result["crs"]["source_crs"]["value"] is None
    route = result["items"][0]
    assert route["length_m"] is None
    assert route["length_status"] == "blocked_unresolved_source_crs"
    assert route["metric_geometry_similarity_available"] is False
    assert route["length_unresolved_reason"] == "source_crs_pending_confirmation"
    # The source numbers are still available for temporary display.
    assert route["source_numeric_path"] == route["path"]
    assert result["metadata"]["metric_measurement_status"] == "disabled_unresolved_source_crs"
    assert "length_m_and_metric_similarity_disabled" in result["warnings"]


def test_csv_and_xlsx_are_never_guessed_as_wgs84_or_cgcs2000(tmp_path):
    for suffix in (".csv", ".xlsx"):
        result = (
            load_reference_routes(reference_csv(tmp_path)) if suffix == ".csv"
            else None
        )
        if result is None:
            continue
        assert result["crs"]["source_crs"]["value"] is None
        assert result["crs"]["representation_crs"]["value"] is None
        assert result["crs"]["representation_crs"]["declared_by_format"] is False


def test_geojson_records_representation_crs_but_not_source_crs(tmp_path):
    source = tmp_path / "routes.geojson"
    source.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"route_number": "9", "sequence": 1, "point_name": "A"},
            "geometry": {"type": "Point", "coordinates": [122.1, 30.1]},
        }, {
            "type": "Feature",
            "properties": {"route_number": "9", "sequence": 2, "point_name": "B"},
            "geometry": {"type": "Point", "coordinates": [122.2, 30.2]},
        }],
    }), encoding="utf-8")
    result = load_reference_routes(source)
    assert result["crs"]["representation_crs"]["value"] == CRS84
    assert result["crs"]["representation_crs"]["status"] == "declared"
    assert result["crs"]["representation_crs"]["declared_by_format"] is True
    # Declaring the representation CRS does NOT prove the source CRS.
    assert result["crs"]["source_crs"]["value"] is None
    assert result["items"][0]["length_m"] is None
    assert "representation_crs_declared_by_format_not_source_crs" in result["warnings"]


def test_confirmed_source_crs_enables_length(tmp_path):
    result = load_reference_routes(reference_csv(tmp_path), crs={
        "source_crs": {
            "value": "EPSG:4326", "status": "confirmed", "confirmed": True,
            "axis_order": "lon_lat",
            "source": {"type": "user_confirmation", "note": "人工确认源表坐标系"},
            "evidence": [{"type": "user_attestation", "note": "测绘部门确认"}],
        },
    })
    route = result["items"][0]
    assert route["length_m"] is not None and route["length_m"] > 0
    assert route["length_status"] == "passed"
    assert route["metric_geometry_similarity_available"] is True
    assert result["metadata"]["metric_measurement_status"] == "enabled"
    assert result["crs"]["source_crs"]["evidence"][0]["type"] == "user_attestation"


def test_unconfirmed_source_crs_value_is_still_blocked():
    record = normalize_crs_record({"source_crs": {"value": "EPSG:4326", "status": "pending_confirmation"}})
    assert is_resolved(record, role="source_crs") is False
    assert unresolved_reason(record, role="source_crs") == "source_crs_value_not_confirmed"
    record = normalize_crs_record({"source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True}})
    assert is_resolved(record, role="source_crs") is True


def test_legacy_crs_status_becomes_evidence_not_a_crs_value():
    record = normalize_crs_record({"crs_status": "pending_confirmation"})
    assert record["source_crs"]["value"] is None
    assert record["source_crs"]["status"] == "pending_confirmation"
    assert record["source_crs"]["evidence"][0]["type"] == "legacy_crs_status_field"
    assert is_resolved(record, role="source_crs") is False


def test_invalid_crs_status_is_rejected():
    with pytest.raises(ValueError, match="status"):
        normalize_crs_record({"source_crs": {"value": "EPSG:4326", "status": "probably"}})


def test_landing_site_import_also_carries_crs_split(tmp_path):
    source = tmp_path / "landing.csv"
    source.write_text(
        "序号,起降设施分类,所属县区,具体位置,经纬度信息\n"
        "1,起降点,定海区,明确点,122.10,30.10\n",
        encoding="utf-8-sig",
    )
    pending = load_reference_landing_sites(source)
    assert pending["crs"]["source_crs"]["status"] == "pending_confirmation"
    assert pending["items"][0]["metric_measurement_status"] == "disabled_unresolved_source_crs"
    confirmed = load_reference_landing_sites(source, crs={
        "source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True},
    })
    assert confirmed["items"][0]["metric_measurement_status"] == "enabled"
    assert confirmed["crs"]["source_crs"]["value"] == "EPSG:4326"


def test_stale_legacy_length_is_preserved_and_metric_length_removed(tmp_path):
    from cns_planner.reference_data.routes import backfill_reference_routes

    legacy = {
        "status": "passed", "count": 1,
        "items": [{
            "reference_route_id": "RLR-LEGACY", "path": [[122.1, 30.1], [122.2, 30.2]],
            "length_m": 12345.6, "crs_status": "pending_confirmation",
        }],
        "points": [], "warnings": [],
        "metadata": {"crs_status": "pending_confirmation"},
    }
    backfilled = backfill_reference_routes(legacy)
    route = backfilled["items"][0]
    assert route["length_m"] is None
    assert route["legacy_length_m"] == 12345.6
    assert route["source_numeric_path"] == [[122.1, 30.1], [122.2, 30.2]]
    assert backfilled["metadata"]["crs_status_legacy"] == "pending_confirmation"


# --------------------------------------------------------------------------------------
# 4. explicit reference route links
# --------------------------------------------------------------------------------------


def test_link_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="confirmed"):
        normalize_reference_route_links([{
            "reference_route_id": "RLR-1", "scenario_route_id": "R0001",
            "confirmed": False, "source": {"type": "auto"},
        }])
    with pytest.raises(ValueError, match="source"):
        normalize_reference_route_links([{
            "reference_route_id": "RLR-1", "scenario_route_id": "R0001", "confirmed": True,
        }])


def test_link_snapshot_creates_and_deletes_a_confirmed_association(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(reference_csv(tmp_path))
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    scenario_id = workflow.state["scenario_routes"][0]["route_id"]

    snapshot = workflow.create_reference_route_link({
        "reference_route_id": reference_id, "scenario_route_id": scenario_id,
        "confirmed": True, "source": {"type": "user_confirmation"},
    })
    links = snapshot["reference_route_links"]
    assert links["count"] == 1
    link = links["items"][0]
    assert link["link_id"].startswith("RRL-")
    assert link["confirmed"] is True
    assert link["origin"] == "user"

    deleted = workflow.delete_reference_route_link(link["link_id"])
    assert deleted["reference_route_links"]["count"] == 0


def test_link_rejects_unknown_reference_or_scenario_route(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(reference_csv(tmp_path))
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    scenario_id = workflow.state["scenario_routes"][0]["route_id"]
    with pytest.raises(ValueError, match="参考航线"):
        workflow.create_reference_route_link({
            "reference_route_id": "RLR-NOPE", "scenario_route_id": scenario_id,
            "confirmed": True, "source": {"type": "user_confirmation"},
        })
    with pytest.raises(ValueError, match="场景航路"):
        workflow.create_reference_route_link({
            "reference_route_id": reference_id, "scenario_route_id": "R9999",
            "confirmed": True, "source": {"type": "user_confirmation"},
        })


def test_endpoint_candidates_are_blocked_while_crs_is_pending(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(reference_csv(tmp_path))
    candidates = workflow.reference_endpoint_candidates_snapshot()
    assert candidates["status"] == "blocked"
    assert candidates["reason"] == "source_crs_pending_confirmation"
    assert candidates["candidate_count"] == 0
    assert candidates["requires_user_confirmation"] is True


def test_endpoint_candidates_appear_only_after_crs_confirmation_and_stay_advisory(tmp_path):
    source = tmp_path / "routes.csv"
    source.write_text(
        "航线编号,航线名称,点序号,点位名称,经度,纬度\n"
        "7,测试线,1,A,122.02,29.92\n"
        "7,测试线,2,B,122.18,30.08\n",
        encoding="utf-8-sig",
    )
    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(source, crs={
        "source_crs": {"value": "EPSG:4326", "status": "confirmed", "confirmed": True},
    })
    candidates = workflow.reference_endpoint_candidates_snapshot()
    assert candidates["status"] == "passed"
    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["state"] == "suggested"
    assert candidate["requires_user_confirmation"] is True
    assert candidate["start_offset_m"] < 100
    # A candidate alone must NOT create a link.
    assert workflow.state["reference_route_links"]["count"] == 0


def test_candidate_helper_returns_none_for_non_numeric_offsets():
    assert endpoint_candidate(
        "RLR-1", "R0001", start_offset_m=None, end_offset_m=1.0,
        reference_crs=empty_crs_record(), scenario_crs={}, method="m", threshold_m=10.0,
    ) is None
    blocked = blocked_endpoint_candidates([{"reference_route_id": "RLR-1"}], "crs_pending")
    assert blocked["status"] == "blocked" and blocked["candidate_count"] == 0


def test_duplicate_link_pair_is_rejected():
    item = {
        "reference_route_id": "RLR-1", "scenario_route_id": "R0001", "confirmed": True,
        "source": {"type": "user_confirmation"},
    }
    with pytest.raises(ValueError, match="重复"):
        normalize_reference_route_links([item, dict(item)])


def test_legacy_landing_site_collection_gets_crs_backfill(tmp_path):
    from cns_planner.reference_data.landing_sites import backfill_reference_landing_sites

    legacy = {
        "status": "passed", "collection_id": "reference-landing-sites", "count": 1,
        "items": [{"reference_site_id": "RLS-1", "coordinate": [122.1, 30.1], "crs_status": "pending_confirmation"}],
        "warnings": ["source_crs_pending_confirmation"],
        "metadata": {"crs_status": "pending_confirmation"},
    }
    backfilled = backfill_reference_landing_sites(legacy)
    assert backfilled["crs"]["source_crs"]["value"] is None
    assert backfilled["crs"]["source_crs"]["evidence"][0]["type"] == "legacy_crs_status_field"
    assert backfilled["items"][0]["metric_measurement_status"] == "disabled_unresolved_source_crs"
    assert backfilled["metadata"]["crs_status_legacy"] == "pending_confirmation"
    # Idempotent: a second pass changes nothing.
    assert backfill_reference_landing_sites(backfilled) == backfilled
    # Empty untouched collections stay byte-identical to the empty schema.
    from cns_planner.reference_data import empty_reference_landing_sites

    assert backfill_reference_landing_sites(empty_reference_landing_sites()) == empty_reference_landing_sites()
    assert backfill_reference_landing_sites(None) == empty_reference_landing_sites()


def test_reference_route_backfill_is_idempotent(tmp_path):
    from cns_planner.reference_data.routes import backfill_reference_routes
    from cns_planner.reference_data import empty_reference_routes

    assert backfill_reference_routes(empty_reference_routes()) == empty_reference_routes()
    assert backfill_reference_routes(None) == empty_reference_routes()
    loaded = load_reference_routes(reference_csv(tmp_path))
    assert backfill_reference_routes(loaded) == loaded


def test_reference_links_persist_and_backfill(tmp_path):
    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(reference_csv(tmp_path))
    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    workflow.create_reference_route_link({
        "reference_route_id": reference_id,
        "scenario_route_id": workflow.state["scenario_routes"][0]["route_id"],
        "confirmed": True, "source": {"type": "user_confirmation"},
    })
    restored = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert restored.state["reference_route_links"]["count"] == 1
    assert restored.snapshot()["reference_route_links"]["items"][0]["confirmed"] is True


# --------------------------------------------------------------------------------------
# 5. registry / API wiring
# --------------------------------------------------------------------------------------


def test_experiment_and_link_api_endpoints_are_additive(tmp_path):
    from cns_planner.api.router import ApiRouter

    workflow = workflow_with_routes(tmp_path)
    workflow.state["reference_routes"] = load_reference_routes(reference_csv(tmp_path))

    class Context:
        data = object()

        def __init__(self, workflow):
            self.workflow = workflow

        def evaluate_route_vertical_profiles(self, payload):
            raise AssertionError("not used")

    router = ApiRouter(Context(workflow))
    evaluated = router.post("/api/route-experiments/evaluate", {}).data
    assert evaluated["route_planning_experiments"]["count"] == 1
    assert router.get("/api/route-experiments", {}, {}).data["count"] == 1
    assert router.get("/api/reference-route-links", {}, {}).data["count"] == 0
    assert router.get("/api/reference-endpoint-candidates", {}, {}).data["status"] == "blocked"
    assert router.get("/api/data-readiness", {}, {}).data["blocks"]["airspace_policies"]

    reference_id = workflow.state["reference_routes"]["items"][0]["reference_route_id"]
    scenario_id = workflow.state["scenario_routes"][0]["route_id"]
    created = router.post("/api/reference-route-links/create", {
        "reference_route_id": reference_id, "scenario_route_id": scenario_id,
        "confirmed": True, "source": {"type": "user_confirmation"},
    }).data
    assert created["reference_route_links"]["count"] == 1
    link_id = created["reference_route_links"]["items"][0]["link_id"]
    assert router.post("/api/reference-route-links/delete", {"link_id": link_id}).data["reference_route_links"]["count"] == 0


def test_registry_route_planners_are_unchanged():
    registry = build_default_algorithm_registry(json.loads(DEFAULTS.read_text(encoding="utf-8")))
    keys = {(item.algorithm_id, item.version) for item in registry.manifests("route_planner")}
    assert keys == {(PLANNER_V1, "1.0"), (PLANNER_V2, "2.0")}
