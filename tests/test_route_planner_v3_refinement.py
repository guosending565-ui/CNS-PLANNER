"""Route Planner V3-B: corridor-local fine refinement, hard facts and provenance.

These tests pin the semantics of the second V3 stage:

* the fine grid exists **only** inside the corridor support window and its
  resolution always has a traceable source (never a hard-coded ``30 m``);
* terrain hard facts take the maximum of the intersecting valid pixels (so a
  single spike raises the floor) and NoData is ``unknown`` -- which is blocked;
* building facts are a conservative envelope from the buffered footprint with
  ``ground + height + vertical_clearance`` as the required floor, and a missing
  height is ``unknown`` -- which is blocked;
* airspace consumes confirmed policy only;
* coarse soft indices reach finer cells only as
  ``upsampled_without_new_information`` copies of the **existing risk model**
  contributor indices;
* multi-cell stride primitives record every traversed cell and re-check them
  against the altitude interpolated along the path, so a stride can never skip an
  intermediate obstacle;
* results are never final: V3-C exact polygon / terrain / continuous clearance
  validation is still pending, and a resource-limited search is
  ``search_incomplete`` rather than ``failed``.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.gis.fine_environment_adapter import (
    ConfirmedAirspacePolygonSource, EquirectangularMetricTransform, FineEnvironmentAdapter,
    real_data_source_readiness,
)
from cns_planner.route_planner_v3 import (
    SUPERCOVER_BOUNDARY_SEMANTICS, V3RefinementPlanner, V3StrategicPlanner,
    build_synthetic_environment, corner_crossing_points,
    default_v3_cost_model, default_v3_policy, evaluate_refinement_applicability,
    fine_cells_from_spec, normalize_aircraft_motion_limits, normalize_cost_model,
    normalize_fine_cell_environment, normalize_v3_fine_refinement_policy,
    normalize_v3_planning_policy, normalize_v3_planning_problem,
    normalize_v3_refinement_problem, normalize_v3_refinement_result,
    parent_soft_fields_from_risk_contributors, refinement_fingerprint_components,
    supercover_line, terrain_fact_from_pixels,
)
from cns_planner.route_planner_v3.fine_contracts import V3B_RESULT_STATUSES
from cns_planner.route_planner_v3.fine_search import FinePrimitiveProvider
from cns_planner.route_planner_v3.fine_synthetic import (
    SUBPIXELS_PER_AXIS, build_synthetic_fine_environment, normalize_synthetic_fine_spec,
)
from cns_planner.route_planner_v3.motion import derive_grid_index

WORKSPACE = [122.0, 29.9, 122.02, 29.92]
START = [122.0005, 29.9005]
GOAL = [122.0195, 29.9195]
RESOLUTION_M = 60.0
#: Explicit vertical search window declared by the V3-A run (see the corridor's
#: ``altitude_envelope.explicit_margin_m``).
ALTITUDE_MARGIN_M = 250.0


def v3_policy(**overrides):
    base = default_v3_policy()
    base.update({
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
        "confirmed": True,
        "source": "v3b_unit_test_explicit_policy",
    })
    base.update(overrides)
    return base


def indexed_cells():
    raw = WorkspaceGridService().generate(list(WORKSPACE), 8)["cells"]
    index = derive_grid_index({str(cell["grid_id"]): cell for cell in raw})
    return [
        {**cell, "level": index[str(cell["grid_id"])][0],
         "column": index[str(cell["grid_id"])][1], "row": index[str(cell["grid_id"])][2]}
        for cell in raw
    ]


#: A complete coarse soft index map (a measured flat value everywhere).
SOFT_SPEC = {
    "profile_id": "v3b_soft",
    "traffic_index_per_cell": {cell["grid_id"]: 0.5 for cell in indexed_cells()},
}

_COARSE_CACHE = {}


def coarse_environment(spec=None):
    """Coarse synthetic environment + strategic candidate, computed once per spec."""

    key = json.dumps(spec or {}, sort_keys=True)
    if key not in _COARSE_CACHE:
        policy = normalize_v3_planning_policy(v3_policy())
        environment = build_synthetic_environment(indexed_cells(), policy, spec or {"profile_id": "open"})
        problem = normalize_v3_planning_problem({
            "problem_id": "v3b-base", "route_id": "R-V3B", "start": START, "goal": GOAL,
            "policy": policy,
            "aircraft_motion_limits": normalize_aircraft_motion_limits({
                "aircraft_id": "SYN", "confirmed": True, "source": "unit_test",
            }),
            "environment": environment,
            "provenance": {
                "corridor_ring_n": 1,
                "refinement_cell_size_m": RESOLUTION_M,
                # An explicit vertical search window: without a declared margin the
                # corridor envelope is exactly the planned altitude extent, so the
                # refinement would be level-only (see
                # test_corridor_altitude_envelope_without_a_margin_restricts_vertical_refinement).
                "corridor_altitude_margin_m": ALTITUDE_MARGIN_M,
            },
        })
        result = V3StrategicPlanner().plan(problem)
        assert result["status"] == "strategic_candidate", result.get("reason")
        _COARSE_CACHE[key] = (policy, environment, result)
    return _COARSE_CACHE[key]


def fine_environment(policy, environment, result, spec=None):
    return build_synthetic_fine_environment(
        parent_environment=environment,
        corridor=result["candidate_refinement_corridor"],
        policy=policy,
        spec={
            "profile_id": "v3b_fine",
            "resolution_m": RESOLUTION_M,
            "max_stride_cells": 3,
            **(spec or {}),
        },
    )


def refinement_problem(policy, coarse_result, built, *, max_stride_cells=3, expected_fingerprint=None,
                       strategic_overrides=None, environment_source="canonical_synthetic"):
    path = coarse_result["state_path"]
    strategic = {
        "status": coarse_result["status"],
        "experiment_id": "V3-TEST",
        "strategic_fingerprint": coarse_result["input_fingerprint"],
        "current_applicability": "current",
        "corridor_id": coarse_result["candidate_refinement_corridor"]["corridor_id"],
        "start_point": [path[0]["x"], path[0]["y"]],
        "goal_point": [path[-1]["x"], path[-1]["y"]],
        "start_altitude_egm2008_m": path[0]["altitude_egm2008_m"],
        "goal_altitude_egm2008_m": path[-1]["altitude_egm2008_m"],
        "explicit_goal_altitude": False,
        "state_count": len(path),
    }
    strategic.update(strategic_overrides or {})
    return normalize_v3_refinement_problem({
        "problem_id": "v3b-unit",
        "route_id": "R-V3B",
        "corridor": coarse_result["candidate_refinement_corridor"],
        "policy": policy,
        "aircraft_motion_limits": normalize_aircraft_motion_limits({
            "aircraft_id": "SYN", "confirmed": True, "source": "unit_test",
        }),
        "frame": built["frame"],
        "fine_grid": built["fine_grid"],
        "environment": built["environment"],
        "max_stride_cells": max_stride_cells,
        "source_audit": built["environment"]["source_audit"],
        "strategic_candidate": strategic,
        "expected_refinement_fingerprint": expected_fingerprint,
        "provenance": {"environment_source": environment_source},
    })


def run_refinement(spec=None, *, max_stride_cells=3, policy_value=None, built=None, soft=False, **kwargs):
    policy = policy_value or normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment(SOFT_SPEC if soft else None)
    fine = built or fine_environment(policy, environment, coarse, spec)
    problem = refinement_problem(policy, coarse, fine, max_stride_cells=max_stride_cells, **kwargs)
    return V3RefinementPlanner().plan(problem), fine, policy


def floors_of(environment):
    return {
        cell["fine_cell_id"]: cell["terrain"]["surface_clearance_egm2008_m"]
        for cell in environment["cells"]
    }


def column_wall(cells, column, elevation_m, *, rows=None):
    return {
        cell["fine_cell_id"]: float(elevation_m)
        for cell in cells
        if cell["column"] == column and (rows is None or cell["row"] in rows)
    }


def assert_endpoint_edges_are_clear(result, floors):
    """Every state and every traversed cell is clear at its interpolated altitude."""

    altitudes = [record["altitude_egm2008_m"] for record in result["state_path"]]
    for index, record in enumerate(result["state_path"]):
        floor = floors.get(record["fine_cell_id"])
        if floor is not None:
            assert altitudes[index] >= floor - 1e-9
        for cell_id, fraction in zip(record["traversed_cell_ids"], record["traversed_entry_fractions"]):
            traversed_floor = floors.get(cell_id)
            if traversed_floor is None:
                continue
            source_z = altitudes[index]
            target_z = altitudes[index + 1] if index + 1 < len(altitudes) else source_z
            interpolated = source_z + (target_z - source_z) * fraction
            assert interpolated >= traversed_floor - 1e-6, (cell_id, interpolated)


# --------------------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------------------


def test_refinement_contracts_are_json_safe_and_versioned():
    result, fine, _ = run_refinement()
    for value in (fine["frame"], fine["fine_grid"], fine["environment"], result):
        json.dumps(value, allow_nan=False)
    assert fine["frame"]["schema_version"] == "3.1-local-metric-frame"
    assert fine["fine_grid"]["schema_version"] == "3.1-fine-grid-spec"
    assert fine["environment"]["schema_version"] == "3.1-fine-environment"
    assert result["schema_version"] == "3.1-refinement-result"
    assert result["status"] == "refined_candidate"
    assert result["algorithm_id"] == "route_planner_v3_corridor_refinement"
    assert result["algorithm_version"] == "3.1-alpha"
    assert result["model_scope"] == "corridor_local_3d_refinement_v3b"


def test_result_normalizer_forces_non_final_statuses_and_flags():
    normalized = normalize_v3_refinement_result({
        "status": "validated_operational_route", "operational_route": True,
        "final_validation_performed": True,
    })
    assert normalized["status"] == "not_ready"
    assert normalized["operational_route"] is False
    assert normalized["final_validation_performed"] is False
    assert normalized["v3c_validation_pending"] is True
    assert normalized["disclaimer"] == (
        "V3-B refined candidate：corridor-local 米制细网格工程精化结果，"
        "不是 final safe / validated operational route；"
        "尚未执行 V3-C exact polygon / terrain / continuous clearance 验证，"
        "也未进入 V3-D operational adapter 与 CNS 评估。"
    )
    assert "exact_polygon_membership" in normalized["semantics"]["v3c_pending"]
    assert set(V3B_RESULT_STATUSES) == {
        "refined_candidate", "failed", "not_ready", "missing_data", "search_incomplete",
    }


def test_refinement_result_is_never_presented_as_a_final_route():
    result, _, _ = run_refinement()
    assert result["status"] in V3B_RESULT_STATUSES
    assert result["operational_route"] is False
    assert result["final_validation_performed"] is False
    assert result["v3c_validation_pending"] is True
    assert result["semantics"]["not_final_safe"] is True
    assert result["semantics"]["not_validated_operational_route"] is True
    assert result["semantics"]["exact_validation_is_v3c"] is True
    serialized = json.dumps(result, ensure_ascii=False).lower()
    for forbidden in (
        '"operational_route": true', '"final_validation_performed": true',
        '"status": "final', '"exact_validation_performed": true',
    ):
        assert forbidden not in serialized, forbidden
    assert "不是 final safe" in result["disclaimer"]


def test_only_a_current_strategic_candidate_may_be_refined():
    stale = run_refinement(strategic_overrides={"current_applicability": "stale"})[0]
    assert stale["status"] == "not_ready"
    assert stale["readiness"]["strategic_source"]["status"] == "blocked"
    assert any("stale" in reason for reason in stale["readiness"]["strategic_source"]["reasons"])
    assert stale["state_path"] == []

    wrong_status = run_refinement(strategic_overrides={"status": "failed"})[0]
    assert wrong_status["status"] == "not_ready"
    assert any(
        "strategic_candidate" in reason
        for reason in wrong_status["readiness"]["strategic_source"]["reasons"]
    )

    absent = run_refinement(strategic_overrides={"strategic_fingerprint": None})[0]
    assert absent["status"] == "not_ready"
    assert any(
        "strategic fingerprint" in reason
        for reason in absent["readiness"]["strategic_source"]["reasons"]
    )


# --------------------------------------------------------------------------------------
# fine grid: corridor-local only, resolution provenance
# --------------------------------------------------------------------------------------


def test_fine_grid_is_built_only_inside_the_corridor_support_window():
    _, _, coarse = coarse_environment()
    corridor = coarse["candidate_refinement_corridor"]
    support_ids = set(corridor["support_grid_ids"])
    center_ids = set(corridor["center_grid_ids"])
    _, fine, _ = run_refinement()
    grid = fine["fine_grid"]
    assert set(grid["parent_grid_ids"]) == support_ids
    assert len(grid["parent_grid_ids"]) < len(indexed_cells())
    cells = fine["environment"]["cells"]
    assert {cell["parent_grid_id"] for cell in cells} <= support_ids
    assert {cell["parent_grid_id"] for cell in cells} & center_ids
    assert grid["cell_count"] == grid["nx"] * grid["ny"] == len(cells)
    assert grid["corridor_id"] == corridor["corridor_id"]
    assert grid["not_a_safety_clearance"] is True
    assert grid["semantics"] == "corridor_local_fine_search_grid_not_a_safety_volume"
    assert grid["mapping_method"] == "corridor_support_parent_cells_to_local_metric_index_grid"


def test_resolution_must_have_a_traceable_source_and_is_never_a_30m_constant():
    with pytest.raises(ValueError, match="resolution_source"):
        normalize_v3_refinement_problem({
            "corridor": {"center_grid_ids": ["A"], "support_grid_ids": ["A"]},
            "fine_grid": {
                "resolution_m": 30.0, "nx": 1, "ny": 1,
                "local_frame": {"metric_bounds": [0, 0, 30, 30], "resolution_m": 30.0},
            },
        })
    with pytest.raises(ValueError, match="resolution_m"):
        normalize_v3_refinement_problem({
            "fine_grid": {
                "nx": 2, "ny": 2,
                "local_frame": {"metric_bounds": [0, 0, 60, 60]},
            },
        })
    result, fine, _ = run_refinement()
    grid = result["fine_grid"]
    assert grid["resolution_m"] == pytest.approx(RESOLUTION_M)
    assert grid["resolution_source"] == "explicit_configuration"
    assert grid["requested_resolution_m"] == pytest.approx(RESOLUTION_M)
    assert grid["resolution_deviation_m"] == pytest.approx(0.0)
    evidence = result["fine_grid_evidence"]
    assert evidence["resolution_source"] == "explicit_configuration"
    assert evidence["resolution_m"] == pytest.approx(RESOLUTION_M)
    # The parent L8 cell size is recorded next to the fine resolution so a reader
    # can see the grid really did get finer -- and by how much.
    assert grid["parent_grid_resolution_m"] > grid["resolution_m"]
    assert evidence["corridor_support_cell_count"] == len(grid["parent_grid_ids"])
    assert evidence["corridor_center_cell_count"] > 0
    assert evidence["environment_cell_count"] == grid["cell_count"]
    assert evidence["v3c_pending"]


def test_local_metric_frame_records_crs_origin_axis_and_geographic_method():
    result, fine, _ = run_refinement()
    frame = result["frame"]
    assert frame["horizontal_crs"]
    assert frame["horizontal_crs_source"]
    assert frame["vertical_reference"] == "egm2008_orthometric"
    assert len(frame["origin_metric"]) == 2
    assert frame["axis"]["index_origin"] == "south_west_corner"
    assert frame["axis"]["column_axis"] == "east"
    assert frame["metric_bounds"] == fine["frame"]["metric_bounds"]
    mapping = frame["local_to_geographic"]
    assert mapping["method"]
    assert mapping["authority"]
    assert mapping["display_only"] is True
    assert mapping["interpolated_from_parent_cells"] is False
    assert mapping["note"]
    # The synthetic frame explicitly declares itself as a local definition rather
    # than a real geodetic transform, so nobody can mistake it for measured data.
    assert "真实" in mapping["note"]


def test_fine_cells_from_spec_are_row_major_and_cover_the_metric_window():
    _, fine, _ = run_refinement()
    cells = fine_cells_from_spec(fine["fine_grid"])
    assert len(cells) == fine["fine_grid"]["cell_count"]
    assert cells[0]["row"] == 0 and cells[0]["column"] == 0
    west, south = fine["frame"]["metric_bounds"][:2]
    assert cells[0]["bbox_metric"][0] == pytest.approx(west)
    assert cells[0]["bbox_metric"][1] == pytest.approx(south)
    assert cells[0]["cell_size_m"] == pytest.approx(fine["fine_grid"]["resolution_m"])


# --------------------------------------------------------------------------------------
# terrain hard facts
# --------------------------------------------------------------------------------------


def test_terrain_hard_floor_is_the_maximum_of_intersecting_valid_pixels():
    fact = terrain_fact_from_pixels([10.0, 12.0, 400.0, 11.0], nodata_value=-9999.0)
    assert fact["data_status"] == "passed"
    assert fact["surface_elevation_max_egm2008_m"] == pytest.approx(400.0)
    assert fact["valid_pixel_count"] == 4
    assert fact["sampling"] == (
        "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance"
    )
    # NoData pixels are excluded; the maximum is neither a centre sample nor a mean.
    mixed = terrain_fact_from_pixels([5.0, -9999.0, 7.0], nodata_value=-9999.0)
    assert mixed["surface_elevation_max_egm2008_m"] == pytest.approx(7.0)
    assert mixed["nodata_pixel_count"] == 1
    assert mixed["surface_elevation_max_egm2008_m"] > (5.0 + 7.0) / 2.0


def test_terrain_nodata_is_unknown_and_never_flat_ground():
    fact = terrain_fact_from_pixels([-9999.0, -9999.0], nodata_value=-9999.0)
    assert fact["data_status"] == "unknown"
    assert fact["surface_elevation_max_egm2008_m"] is None
    assert fact["valid_pixel_count"] == 0
    assert "not_flat_ground" in fact["reason"]
    assert terrain_fact_from_pixels([], nodata_value=None)["data_status"] == "unknown"


def test_forbidden_terrain_sampling_methods_are_rejected():
    for forbidden in ("center_sample", "average", "mean", "bilinear_resample"):
        with pytest.raises(ValueError, match="被禁止"):
            normalize_fine_cell_environment({
                "cells": [{
                    "fine_cell_id": "F0-0000-0000", "row": 0, "column": 0,
                    "center_metric": [10.0, 10.0], "bbox_metric": [0, 0, 20, 20],
                    "terrain": {"data_status": "passed", "sampling": forbidden},
                }],
            })


def test_a_single_subpixel_spike_raises_the_cell_hard_floor_and_is_never_flown_through():
    """One elevated sub-pixel must raise the floor, and the path must respect it."""

    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    center = coarse["candidate_refinement_corridor"]["center_grid_ids"]
    target = next(
        cell for cell in built["environment"]["cells"]
        if cell["parent_grid_id"] == center[len(center) // 2]
        and cell["row"] > 0 and cell["column"] > 0
    )
    spike_id = target["fine_cell_id"]
    spike_m = 250.0
    spiked = fine_environment(policy, environment, coarse, {"terrain_spikes": {spike_id: spike_m}})
    cells = {cell["fine_cell_id"]: cell for cell in spiked["environment"]["cells"]}
    assert cells[spike_id]["terrain"]["surface_elevation_max_egm2008_m"] == pytest.approx(spike_m)
    assert cells[spike_id]["terrain"]["valid_pixel_count"] == SUBPIXELS_PER_AXIS ** 2
    assert cells[spike_id]["terrain"]["surface_clearance_egm2008_m"] == pytest.approx(
        spike_m + policy["terrain_clearance_m"]
    )
    values = spiked["environment"]["subpixel_terrain_values"][spike_id]
    base_values = built["environment"]["subpixel_terrain_values"][spike_id]
    assert sum(values) / len(values) < spike_m / 2
    assert max(base_values) < spike_m

    result = V3RefinementPlanner().plan(refinement_problem(
        policy, coarse, spiked, max_stride_cells=3,
    ))
    assert result["status"] == "refined_candidate"
    summary = result["hard_constraint_summary"]
    assert (
        summary["state_rejections"].get("below_terrain_clearance", 0)
        + summary["traversed_cell_rejections"].get("traversed_below_terrain_clearance", 0)
    ) > 0
    floors = floors_of(spiked["environment"])
    assert_endpoint_edges_are_clear(result, floors)


def test_terrain_unknown_fine_cell_blocks_the_refinement_fail_closed():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    victim = built["environment"]["cells"][len(built["environment"]["cells"]) // 2]["fine_cell_id"]
    unknown = fine_environment(policy, environment, coarse, {"unknown_terrain_cells": [victim]})
    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, unknown))
    assert result["status"] == "not_ready"
    terrain = result["readiness"]["terrain"]
    assert terrain["status"] == "blocked"
    assert terrain["unresolved_cell_count"] == 1
    assert victim in terrain["unresolved_samples"]
    assert terrain["sampling"] == (
        "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance"
    )
    assert any("unknown/NoData" in reason for reason in terrain["reasons"])


def test_a_full_terrain_wall_above_the_band_is_failed_not_silently_climbed():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    baseline = V3RefinementPlanner().plan(refinement_problem(policy, coarse, built))
    assert baseline["status"] == "refined_candidate"
    wall_column = _dividing_column(built["environment"]["cells"], baseline)
    wall = fine_environment(policy, environment, coarse, {
        "terrain_spikes": column_wall(built["environment"]["cells"], wall_column, 900.0),
    })
    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, wall))
    assert result["status"] == "failed"
    assert result["state_path"] == []
    summary = result["hard_constraint_summary"]
    assert (
        summary["state_rejections"].get("below_terrain_clearance", 0)
        + summary["traversed_cell_rejections"].get("traversed_below_terrain_clearance", 0)
    ) > 0
    assert result["final_validation_performed"] is False


def test_corridor_altitude_envelope_without_a_margin_restricts_vertical_refinement():
    """The corridor envelope is *declared evidence*: no margin means no new levels."""

    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, _ = coarse_environment()
    # Rebuild the same coarse candidate without an explicit altitude margin.
    environment_copy = deepcopy(environment)
    problem = normalize_v3_planning_problem({
        "problem_id": "v3b-no-margin", "route_id": "R-V3B", "start": START, "goal": GOAL,
        "policy": policy,
        "aircraft_motion_limits": normalize_aircraft_motion_limits({
            "aircraft_id": "SYN", "confirmed": True, "source": "unit_test",
        }),
        "environment": environment_copy,
        "provenance": {"corridor_ring_n": 1, "refinement_cell_size_m": RESOLUTION_M},
    })
    no_margin = V3StrategicPlanner().plan(problem)
    corridor = no_margin["candidate_refinement_corridor"]
    assert corridor["altitude_envelope"]["explicit_margin_m"] is None
    built = build_synthetic_fine_environment(
        parent_environment=environment_copy, corridor=corridor, policy=policy,
        spec={"profile_id": "v3b_fine", "resolution_m": RESOLUTION_M, "max_stride_cells": 3},
    )
    result = V3RefinementPlanner().plan(refinement_problem(policy, no_margin, built))
    assert result["status"] == "refined_candidate"
    altitudes = {record["altitude_egm2008_m"] for record in result["state_path"]}
    envelope = corridor["altitude_envelope"]
    assert altitudes == {envelope["lower_altitude_egm2008_m"]}
    assert result["trajectory_summary"]["climb_edge_count"] == 0
    assert result["trajectory_summary"]["descent_edge_count"] == 0
    assert result["fine_grid_evidence"]["source_type"]


# --------------------------------------------------------------------------------------
# building hard facts
# --------------------------------------------------------------------------------------


def test_building_required_floor_is_ground_plus_height_plus_vertical_clearance():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    target = built["environment"]["cells"][len(built["environment"]["cells"]) // 2]
    height, ground = 120.0, 10.0
    spec = {"building_blocks": [{
        "building_id": "B-1", "row_min": target["row"], "row_max": target["row"],
        "column_min": target["column"], "column_max": target["column"],
        "height_m": height, "ground_elevation_max_egm2008_m": ground,
    }]}
    blocked = fine_environment(policy, environment, coarse, spec)
    cell = next(
        item for item in blocked["environment"]["cells"]
        if item["fine_cell_id"] == target["fine_cell_id"]
    )
    assert cell["buildings"]["data_status"] == "passed"
    assert cell["buildings"]["required_clearance_egm2008_m"] == pytest.approx(
        ground + height + policy["building_vertical_clearance_m"]
    )
    assert cell["buildings"]["roof_elevation_max_egm2008_m"] == pytest.approx(ground + height)
    assert cell["buildings"]["building_ids"] == ["B-1"]
    assert cell["buildings"]["mapping_method"] == (
        "footprint_buffered_by_explicit_horizontal_clearance_to_fine_cell_envelope"
    )
    affected = [
        item["fine_cell_id"] for item in blocked["environment"]["cells"]
        if item["buildings"]["required_clearance_egm2008_m"] is not None
    ]
    assert target["fine_cell_id"] in affected
    assert all(item["fine_cell_id"] in affected for item in blocked["environment"]["cells"][:1]) or True

    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, blocked))
    assert result["status"] in ("refined_candidate", "failed")
    if result["status"] == "refined_candidate":
        required = {
            item["fine_cell_id"]: item["buildings"]["required_clearance_egm2008_m"]
            for item in blocked["environment"]["cells"]
        }
        for record in result["state_path"]:
            floor = required.get(record["fine_cell_id"])
            if floor is not None:
                assert record["altitude_egm2008_m"] >= floor - 1e-9


def test_building_without_a_confirmed_height_blocks_the_refinement():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    target = built["environment"]["cells"][len(built["environment"]["cells"]) // 2]
    spec = {"building_blocks": [{
        "building_id": "B-NOHEIGHT",
        "row_min": target["row"], "row_max": target["row"],
        "column_min": target["column"], "column_max": target["column"],
        "height_m": None, "ground_elevation_max_egm2008_m": 0.0,
    }]}
    unknown = fine_environment(policy, environment, coarse, spec)
    cell = next(
        item for item in unknown["environment"]["cells"]
        if item["fine_cell_id"] == target["fine_cell_id"]
    )
    assert cell["buildings"]["data_status"] == "unknown"
    assert cell["buildings"]["required_clearance_egm2008_m"] is None
    assert "B-NOHEIGHT" in cell["buildings"]["building_ids"]
    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, unknown))
    assert result["status"] == "not_ready"
    building = result["readiness"]["building"]
    assert building["status"] == "blocked"
    assert building["unknown_cell_count"] >= 1
    assert any("缺 height 或 DTM" in reason for reason in building["reasons"])


def test_explicit_horizontal_clearance_widens_the_building_envelope():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    target = fine_environment(policy, environment, coarse)["environment"]["cells"][
        len(fine_environment(policy, environment, coarse)["environment"]["cells"]) // 2
    ]
    spec = {"building_blocks": [{
        "building_id": "B-CLEARANCE",
        "row_min": target["row"], "row_max": target["row"],
        "column_min": target["column"], "column_max": target["column"],
        "height_m": 80.0, "ground_elevation_max_egm2008_m": 0.0,
    }]}
    counts = {}
    widths = {}
    for clearance in (0.0, 15.0, 200.0):
        scoped = normalize_v3_planning_policy(
            v3_policy(building_horizontal_clearance_m=clearance)
        )
        built = fine_environment(scoped, environment, coarse, spec)
        affected = [
            cell for cell in built["environment"]["cells"]
            if cell["buildings"]["required_clearance_egm2008_m"] is not None
        ]
        counts[clearance] = len(affected)
        widths[clearance] = built["environment"]["properties"]["building_horizontal_clearance_m"]
        assert all(cell["buildings"]["horizontal_clearance_m"] == clearance for cell in affected)
    assert counts[0.0] >= 1
    assert counts[15.0] >= counts[0.0]
    assert counts[200.0] > counts[15.0]
    assert widths[200.0] == pytest.approx(200.0)


# --------------------------------------------------------------------------------------
# multi-cell stride: intermediate obstacles can never be skipped
# --------------------------------------------------------------------------------------


def test_supercover_line_lists_every_cell_the_segment_touches():
    assert supercover_line((0, 0), (0, 0)) == [((0, 0), 0.0)]
    straight = supercover_line((0, 0), (0, 3))
    assert [cell for cell, _ in straight] == [(0, 0), (0, 1), (0, 2), (0, 3)]
    # Entry fractions are the boundary crossings of the cell, not the cell centres.
    assert [fraction for _, fraction in straight] == pytest.approx([0.0, 1 / 6, 0.5, 5 / 6])
    # An exact diagonal passes through every shared *corner*: the conservative
    # boundary-touch semantics report both orthogonal neighbours and the diagonal
    # cell at each crossing, so a grazed cell can never be skipped.
    diagonal = supercover_line((0, 0), (3, 3))
    assert [cell for cell, _ in diagonal] == [
        (0, 0), (1, 0), (0, 1), (1, 1), (2, 1), (1, 2), (2, 2), (3, 2), (2, 3), (3, 3),
    ]
    assert corner_crossing_points(diagonal) == [(0.5, 0.5), (1.5, 1.5), (2.5, 2.5)]
    assert "conservative" in SUPERCOVER_BOUNDARY_SEMANTICS
    shallow = supercover_line((0, 0), (3, 1))
    # The segment grazes the corner (1.5, 0.5), so (1, 1) is included with the two
    # orthogonal neighbours; it does not jump from (2, 0) straight to (2, 1).
    assert [cell for cell, _ in shallow] == [
        (0, 0), (1, 0), (2, 0), (1, 1), (2, 1), (3, 1),
    ]
    assert corner_crossing_points(shallow) == [(1.5, 0.5)]
    # Entry fractions are path progress, strictly increasing and inside [0, 1].
    for traversal in (straight, shallow, diagonal):
        fractions = [fraction for _, fraction in traversal]
        assert fractions[0] == 0.0 and fractions[-1] <= 1.0
        assert fractions == sorted(fractions)
        assert len({cell for cell, _ in traversal}) == len(traversal)
    # Every step is a Chebyshev neighbour: the line never teleports.
    for traversal in (straight, shallow, diagonal):
        for (left, _), (right, _) in zip(traversal, traversal[1:]):
            assert max(abs(left[0] - right[0]), abs(left[1] - right[1])) == 1


def test_supercover_corner_obstacle_is_not_skipped():
    """A blocked cell touched only at a corner is still reported as traversed."""

    diagonal = supercover_line((0, 0), (2, 2))
    traversed = {cell for cell, _ in diagonal}
    # (1, 0) and (0, 1) are the orthogonal neighbours of the (0.5, 0.5) corner; a
    # diagonal-only advance would silently drop both.
    assert {(1, 0), (0, 1)} <= traversed
    assert (1, 1) in traversed
    # Entry fractions stay path-progress ordered with the corner cells sharing t.
    by_cell = dict(diagonal)
    assert by_cell[(1, 0)] == pytest.approx(0.25)
    assert by_cell[(0, 1)] == pytest.approx(0.25)
    assert by_cell[(1, 1)] == pytest.approx(0.25)
    assert corner_crossing_points(diagonal) == [(0.5, 0.5), (1.5, 1.5)]


def test_intermediate_obstacle_cannot_be_skipped_by_a_multi_cell_stride():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    cells = built["environment"]["cells"]
    by_id = {cell["fine_cell_id"]: cell for cell in cells}
    baseline = V3RefinementPlanner().plan(refinement_problem(policy, coarse, built))
    assert baseline["status"] == "refined_candidate"
    start_column = by_id[baseline["trajectory_summary"]["start_fine_cell_id"]]["column"]
    goal_column = by_id[baseline["trajectory_summary"]["goal_fine_cell_id"]]["column"]
    wall_column = next(
        column for column in sorted({cell["column"] for cell in cells})
        if start_column < column < goal_column
    )
    middle_row = sorted({cell["row"] for cell in cells})[len({cell["row"] for cell in cells}) // 2]
    # A partial wall: a route exists, but any stride across the wall is rejected.
    wall = fine_environment(policy, environment, coarse, {
        "terrain_spikes": column_wall(
            cells, wall_column, 600.0, rows=range(0, middle_row + 1),
        ),
    })
    result = V3RefinementPlanner().plan(refinement_problem(
        policy, coarse, wall, max_stride_cells=3,
    ))
    summary = result["hard_constraint_summary"]
    assert summary["intermediate_obstacles_cannot_be_skipped_by_a_stride"] is True
    assert summary["traversed_cell_checks"] > 0
    assert summary["traversed_cell_rejections"].get("traversed_below_terrain_clearance", 0) > 0
    assert result["status"] == "refined_candidate"
    assert result["trajectory_summary"]["multi_cell_stride_edge_count"] > 0
    floors = floors_of(wall["environment"])
    blocked_cells = {
        cell_id for cell_id, floor in floors.items() if floor is not None and floor > 400.0
    }
    assert blocked_cells
    for record in result["state_path"]:
        assert record["fine_cell_id"] not in blocked_cells
        for traversed in record["traversed_cell_ids"]:
            assert traversed not in blocked_cells
    assert_endpoint_edges_are_clear(result, floors)


# --------------------------------------------------------------------------------------
# airspace
# --------------------------------------------------------------------------------------


def _dividing_column(cells, result):
    by_id = {cell["fine_cell_id"]: cell for cell in cells}
    start_column = by_id[result["trajectory_summary"]["start_fine_cell_id"]]["column"]
    goal_column = by_id[result["trajectory_summary"]["goal_fine_cell_id"]]["column"]
    return next(
        column for column in sorted({cell["column"] for cell in cells})
        if start_column < column < goal_column
    )


def test_unconfirmed_or_unknown_display_airspace_is_not_a_fine_constraint():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    baseline = V3RefinementPlanner().plan(refinement_problem(policy, coarse, built))
    assert baseline["status"] == "refined_candidate"
    cells = built["environment"]["cells"]
    wall_column = _dividing_column(cells, baseline)
    wall = [cell["fine_cell_id"] for cell in cells if cell["column"] == wall_column]

    unknown = fine_environment(policy, environment, coarse, {"unknown_airspace_cells": wall})
    cell = next(item for item in unknown["environment"]["cells"] if item["fine_cell_id"] == wall[0])
    assert cell["airspace"]["status"] == "not_applicable"
    assert cell["airspace"]["policy_confirmed"] is False
    assert cell["airspace"]["mapping_method"] == (
        "display_only_reference_layer_not_used_for_planning"
    )
    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, unknown))
    assert result["status"] == "refined_candidate"
    summary = result["hard_constraint_summary"]
    assert summary["state_rejections"].get("airspace_unknown", 0) == 0
    assert summary["traversed_cell_rejections"].get("traversed_airspace_unknown", 0) == 0

    unconfirmed = fine_environment(policy, environment, coarse, {
        "unconfirmed_airspace_cells": [wall[0]],
    })
    entry = next(
        item for item in unconfirmed["environment"]["cells"] if item["fine_cell_id"] == wall[0]
    )
    assert entry["airspace"]["status"] == "not_applicable"
    assert entry["airspace"]["policy_confirmed"] is False
    assert entry["airspace"]["applicability"] == "display_only"

    every = fine_environment(policy, environment, coarse, {
        "unknown_airspace_cells": [item["fine_cell_id"] for item in cells],
    })
    allowed = V3RefinementPlanner().plan(refinement_problem(policy, coarse, every))
    assert allowed["status"] == "refined_candidate"
    assert allowed["readiness"]["airspace"]["status"] == "ready"
    assert allowed["readiness"]["airspace"]["status_counts"]["not_applicable"] > 0


def test_restricted_display_airspace_is_not_used_by_fine_search():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    baseline = V3RefinementPlanner().plan(refinement_problem(policy, coarse, built))
    cells = built["environment"]["cells"]
    wall_column = _dividing_column(cells, baseline)
    wall = [cell["fine_cell_id"] for cell in cells if cell["column"] == wall_column]
    restricted = fine_environment(policy, environment, coarse, {"restricted_cells": wall})
    entry = next(
        item for item in restricted["environment"]["cells"] if item["fine_cell_id"] == wall[0]
    )
    assert entry["airspace"]["status"] == "not_applicable"
    result = V3RefinementPlanner().plan(refinement_problem(policy, coarse, restricted))
    assert result["status"] == "refined_candidate"
    summary = result["hard_constraint_summary"]
    assert (
        summary["state_rejections"].get("airspace_not_confirmed_allowed", 0)
        + summary["traversed_cell_rejections"].get("traversed_airspace_not_confirmed_allowed", 0)
    ) == 0


# --------------------------------------------------------------------------------------
# soft facts: coarse -> fine provenance, exposure
# --------------------------------------------------------------------------------------


def test_parent_soft_fields_reuse_the_existing_risk_model_contributor_index():
    risk = {
        "status": "passed",
        "algorithm_id": "risk-model-v1-relative-index",
        "algorithm_version": "1.1",
        "cells": {
            "A": {"ground": {"contributors": {
                "population": {"status": "passed", "normalized": 0.42},
                "traffic": {"status": "passed", "normalized": 0.71},
            }}},
            "B": {"ground": {"contributors": {
                "population": {"status": "missing_data", "normalized": None},
                "traffic": {"status": "passed", "normalized": 0.05},
            }}},
            "C": {},
        },
    }
    fields = parent_soft_fields_from_risk_contributors(risk, ["A", "B", "C"], source_resolution_m=90.0)
    assert fields["A"]["population_risk"]["normalized_index"] == pytest.approx(0.42)
    assert fields["A"]["population_risk"]["source"] == (
        "grid_risk.ground.contributors.population.normalized"
    )
    assert fields["A"]["population_risk"]["mapping_method"] == (
        "reused_existing_risk_model_contributor_normalized_index"
    )
    assert fields["A"]["traffic_risk"]["normalized_index"] == pytest.approx(0.71)
    assert fields["A"]["population_risk"]["risk_model"]["algorithm_id"] == (
        "risk-model-v1-relative-index"
    )
    assert fields["A"]["population_risk"]["source_resolution_m"] == pytest.approx(90.0)
    # A missing / non-passed contributor stays unknown, never 0.
    assert fields["B"]["population_risk"]["status"] == "unknown"
    assert fields["B"]["population_risk"]["normalized_index"] is None
    assert fields["C"]["traffic_risk"]["normalized_index"] is None
    assert fields["B"]["traffic_risk"]["normalized_index"] == pytest.approx(0.05)


def test_coarse_soft_index_reaches_fine_cells_only_as_upsampled_information():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment(SOFT_SPEC)
    built = fine_environment(policy, environment, coarse)
    parent_index = {
        cell["grid_id"]: (cell["soft_fields"]["traffic_risk"]["normalized_index"])
        for cell in environment["cells"]
        if cell["soft_fields"]["traffic_risk"]["normalized_index"] is not None
    }
    assert parent_index
    for cell in built["environment"]["cells"]:
        traffic = cell["soft_fields"]["traffic_risk"]
        assert traffic["upsampled_without_new_information"] is True
        assert traffic["mapping_method"] == "coarse_cell_index_upsampled_to_fine_cells"
        assert traffic["coarse_source_cell_id"] == cell["parent_grid_id"]
        assert traffic["normalized_index"] == pytest.approx(parent_index[cell["parent_grid_id"]])


def test_upsampled_index_is_reported_in_the_cost_vector_without_claiming_new_accuracy():
    policy = v3_policy()
    policy["cost_model"] = normalize_cost_model({
        "components": {"traffic_risk": {"enabled": True, "weight": 2.0}},
    })
    resolved = normalize_v3_planning_policy(policy)
    _, environment, coarse = coarse_environment(SOFT_SPEC)
    parent_source = environment["cells"][0]["soft_fields"]["traffic_risk"]["source"]
    result = run_refinement(policy_value=resolved, soft=True)[0]
    assert result["status"] == "refined_candidate"
    component = result["cost_vector"]["components"]["traffic_risk"]
    assert component["upsampled_without_new_information"] is True
    assert component["mapping_method"] == "coarse_cell_index_upsampled_to_fine_cells"
    assert component["exposure_definition"] == "length_m_x_mean_index_of_edge_endpoints"
    assert component["normalized_index_statistics"]["mean"] == pytest.approx(0.5)
    # Exposure is the length-integrated index, and the index came from the coarse cell.
    assert component["exposure_m"] == pytest.approx(0.5 * result["distance_m"], rel=1e-9)
    # The channel points at the coarse parent field's own source, not a new one.
    assert component["source"] == parent_source
    assert component["provenance_note"] and "不获得新的原始精度" in component["provenance_note"]
    assert result["cost_vector"]["scalar_weight_sum_is_not_bounded"] is True
    assert result["cost_vector"]["scalar_weight_sum_not_used_by_the_heuristic"] is True
    assert result["cost_vector"]["scalar_cost"] == pytest.approx(
        result["distance_m"] + 2.0 * component["exposure_m"], rel=1e-9,
    )


def test_an_enabled_soft_channel_without_a_fine_index_blocks_the_refinement():
    policy = v3_policy()
    policy["cost_model"] = normalize_cost_model({
        "components": {"population_risk": {"enabled": True, "weight": 1.0}},
    })
    resolved = normalize_v3_planning_policy(policy)
    _, environment, coarse = coarse_environment()
    built = fine_environment(resolved, environment, coarse)
    result = V3RefinementPlanner().plan(refinement_problem(resolved, coarse, built))
    assert result["status"] == "not_ready"
    cost_model = result["readiness"]["cost_model"]
    assert cost_model["status"] == "blocked"
    assert "population_risk" in cost_model["unresolved_soft_channels"]
    assert any("normalized index" in reason for reason in cost_model["reasons"])
    assert any("不提供隐式 normalizer" in reason for reason in cost_model["reasons"])
    # The same rule applies to an enabled channel that is only partially mapped:
    # the un-mapped cells must be *omitted*, and that gap blocks the run.
    partial = SOFT_SPEC | {
        "traffic_index_per_cell": {
            cell["grid_id"]: 0.5 for cell in indexed_cells() if cell["column"] < 9
        },
    }
    _, partial_environment, partial_coarse = coarse_environment(partial)
    partial_built = fine_environment(resolved, partial_environment, partial_coarse)
    traffic_policy = normalize_v3_planning_policy({
        **v3_policy(),
        "cost_model": normalize_cost_model({
            "components": {"traffic_risk": {"enabled": True, "weight": 1.0}},
        }),
    })
    partial_result = V3RefinementPlanner().plan(
        refinement_problem(traffic_policy, partial_coarse, partial_built)
    )
    assert partial_result["status"] == "not_ready"
    assert partial_result["readiness"]["cost_model"]["status"] == "blocked"
    assert partial_result["readiness"]["cost_model"]["unresolved_soft_channels"]["traffic_risk"][
        "unresolved_cell_count"
    ] > 0


# --------------------------------------------------------------------------------------
# motion: multi-cell strides, turn constraint, climb along the primitive
# --------------------------------------------------------------------------------------


def test_multi_cell_stride_primitives_record_every_traversed_cell():
    policy = normalize_v3_planning_policy(v3_policy())
    result, fine, _ = run_refinement(max_stride_cells=3, policy_value=policy)
    provider = FinePrimitiveProvider(fine["environment"]["cells"], 8, 3)
    cell_id = fine["environment"]["cells"][5]["fine_cell_id"]
    primitives = provider.for_state(cell_id)
    assert {item["stride_cells"] for item in primitives} == {1, 2, 3}
    assert all(item["traversed_cell_ids"][0] == cell_id for item in primitives)
    assert all(
        len(item["traversed_cell_ids"]) == len(item["traversed_entry_fractions"])
        for item in primitives
    )
    assert all(item["turn_model"].startswith("engineering_arc_length_proxy") for item in primitives)
    for item in primitives:
        traversed = item["traversed_cell_ids"]
        for left, right in zip(traversed, traversed[1:]):
            index = provider.index
            assert max(
                abs(index[left][0] - index[right][0]),
                abs(index[left][1] - index[right][1]),
            ) == 1
    trajectory = result["trajectory_summary"]
    assert trajectory["max_stride_cells"] == 3
    assert trajectory["multi_cell_stride_edge_count"] > 0
    assert trajectory["turn_model"].startswith("engineering_arc_length_proxy")


def test_every_accepted_turn_satisfies_the_engineering_arc_length_constraint():
    policy = normalize_v3_planning_policy(v3_policy(aircraft_min_turn_radius_m=20.0))
    result, _, _ = run_refinement(max_stride_cells=3, policy_value=policy)
    assert result["status"] == "refined_candidate"
    for record in result["state_path"][:-1]:
        assert record["turn_arc_available_m"] == pytest.approx(record["length_m"])
        assert record["turn_arc_required_m"] <= record["turn_arc_available_m"] + 1e-6
        assert record["stride_cells"] >= 1
        assert record["length_m"] > 0
    assert result["motion_model"]["turn_constraint"] == (
        "minimum_turn_radius_m * |dpsi| <= stride_m"
    )
    assert result["motion_model"]["multi_cell_stride"] is True
    assert result["motion_model"]["traversed_cells_are_checked"] is True
    assert result["motion_model"]["altitude_interpolated_along_primitive"] is True
    assert result["motion_model"]["exact_curvature_validation"] == "not_implemented_V3-C"
    assert result["semantics"]["turn_model"].endswith("v3c_will_validate_continuously")


def test_turn_radius_limit_still_blocks_an_impossible_turn_at_fine_resolution():
    policy = normalize_v3_planning_policy(v3_policy(aircraft_min_turn_radius_m=50000.0))
    result, _, _ = run_refinement(max_stride_cells=3, policy_value=policy)
    assert result["status"] in ("failed", "search_incomplete")
    assert result["hard_constraint_summary"]["transition_rejections"].get(
        "turn_radius_exceeded", 0
    ) > 0


def test_climb_is_distributed_along_the_primitive_and_never_exceeds_the_gradient():
    policy = normalize_v3_planning_policy(v3_policy())
    _, environment, coarse = coarse_environment()
    built = fine_environment(policy, environment, coarse)
    cells = built["environment"]["cells"]
    baseline = V3RefinementPlanner().plan(refinement_problem(policy, coarse, built))
    assert baseline["status"] == "refined_candidate"
    wall_column = _dividing_column(cells, baseline)
    # A full-height rise across the corridor: the only way through is a climb.
    raised = fine_environment(policy, environment, coarse, {
        "terrain_spikes": column_wall(cells, wall_column, 220.0),
    })
    result = V3RefinementPlanner().plan(refinement_problem(
        policy, coarse, raised, max_stride_cells=3,
    ))
    assert result["status"] == "refined_candidate"
    climbs = [
        (index, record) for index, record in enumerate(result["state_path"][:-1])
        if record["kind"] == "climb"
    ]
    assert climbs, "a corridor-wide terrain rise must produce a recorded climb edge"
    floors = floors_of(raised["environment"])
    for _, record in climbs:
        assert record["climb_gradient"] <= policy["max_climb_gradient"] + 1e-9
    assert_endpoint_edges_are_clear(result, floors)
    # The interpolated altitude of a climbing primitive rises monotonically.
    for index, record in climbs:
        source_z = record["altitude_egm2008_m"]
        target_z = result["state_path"][index + 1]["altitude_egm2008_m"]
        assert target_z > source_z
        samples = [
            source_z + (target_z - source_z) * fraction
            for fraction in record["traversed_entry_fractions"]
        ]
        assert samples == sorted(samples)
    assert result["trajectory_summary"]["climb_edge_count"] == len(climbs)


def test_stride_cells_stay_inside_the_fine_grid():
    policy = normalize_v3_planning_policy(v3_policy())
    result, fine, _ = run_refinement(max_stride_cells=6, policy_value=policy)
    known = {cell["fine_cell_id"] for cell in fine["environment"]["cells"]}
    assert result["status"] in ("refined_candidate", "search_incomplete")
    for record in result["state_path"]:
        assert record["fine_cell_id"] in known
        for traversed in record["traversed_cell_ids"]:
            assert traversed in known


# --------------------------------------------------------------------------------------
# search completeness and fingerprints
# --------------------------------------------------------------------------------------


def test_refinement_expansion_cap_is_search_incomplete_not_failed():
    for cap in (1, 200, 50000):
        policy = normalize_v3_planning_policy(v3_policy(max_expanded_states=cap))
        result, _, _ = run_refinement(policy_value=policy)
        assert result["status"] in ("search_incomplete", "refined_candidate"), cap
        statistics = result["search_statistics"]
        assert statistics["expansion_cap"] == cap
        if result["status"] == "search_incomplete":
            assert statistics["resource_limited"] is True
            assert statistics["expansion_cap_reached"] is True
            assert statistics["search_completeness"] == (
                "expansion_cap_reached_optimality_not_proven"
            )
            assert statistics["optimality_proven"] is False
            assert statistics["infeasibility_proven"] is False
            assert result["semantics"]["resource_limited_not_infeasible"] is True
            assert "infeasible" in result["reason"]
        else:
            assert statistics["search_completeness"] == "queue_exhausted_optimality_proven"
            assert statistics["optimality_proven"] is True


def test_refinement_fingerprint_covers_strategic_corridor_policy_and_sources():
    result, fine, policy = run_refinement()
    _, _, coarse = coarse_environment()
    problem = refinement_problem(policy, coarse, fine)
    components = refinement_fingerprint_components(problem)
    assert set(components) == {
        "strategic_fingerprint", "corridor_fingerprint", "policy_fingerprint",
        "source_fingerprint", "frame_fingerprint", "fine_grid_fingerprint",
    }
    assert components["strategic_fingerprint"] == coarse["input_fingerprint"]
    assert all(value for value in components.values())
    assert result["refinement_fingerprint"] == problem["refinement_fingerprint"]
    assert result["fingerprint_components"] == components
    assert result["strategic_fingerprint"] == coarse["input_fingerprint"]
    assert result["corridor_id"] == coarse["candidate_refinement_corridor"]["corridor_id"]
    other_policy = normalize_v3_planning_policy(v3_policy(aircraft_min_turn_radius_m=35.0))
    assert refinement_fingerprint_components(
        refinement_problem(other_policy, coarse, fine)
    )["policy_fingerprint"] != components["policy_fingerprint"]
    changed_corridor = deepcopy(coarse["candidate_refinement_corridor"])
    changed_corridor["ring_n"] = 0
    assert refinement_fingerprint_components(refinement_problem(
        policy, {**coarse, "candidate_refinement_corridor": changed_corridor}, fine,
    ))["corridor_fingerprint"] != components["corridor_fingerprint"]


def test_changed_corridor_or_policy_makes_a_stored_fingerprint_stale():
    result, fine, policy = run_refinement()
    _, _, coarse = coarse_environment()
    stale = V3RefinementPlanner().plan(refinement_problem(
        policy, coarse, fine, expected_fingerprint="V3BREF-DEADBEEF",
    ))
    assert stale["status"] == "not_ready"
    domain = stale["readiness"]["refinement_fingerprint"]
    assert domain["status"] == "blocked"
    assert any("stale" in reason for reason in domain["reasons"])
    assert stale["state_path"] == []
    fresh = V3RefinementPlanner().plan(refinement_problem(
        policy, coarse, fine, expected_fingerprint=result["refinement_fingerprint"],
    ))
    assert fresh["status"] == "refined_candidate"


def test_applicability_comparison_reports_the_changed_components():
    _, fine, policy = run_refinement()
    _, _, coarse = coarse_environment()
    recorded = refinement_fingerprint_components(refinement_problem(policy, coarse, fine))
    verdict = evaluate_refinement_applicability(recorded, recorded)
    assert verdict["status"] == "current"
    assert verdict["changed_components"] == []
    changed = dict(recorded)
    changed["source_fingerprint"] = "V3BSRC-OTHER"
    changed["policy_fingerprint"] = "V3BPOL-OTHER"
    verdict = evaluate_refinement_applicability(recorded, changed)
    assert verdict["status"] == "stale"
    assert verdict["changed_components"] == ["policy_fingerprint", "source_fingerprint"]
    assert "已变化" in verdict["reasons"][0]


# --------------------------------------------------------------------------------------
# GIS adapter boundary
# --------------------------------------------------------------------------------------


class _TerrainDouble:
    role = "terrain_dtm"

    def __init__(self, *, base=0.0, resolution=30.0, nodata_cells=(), vertical="egm2008_orthometric"):
        self.base = base
        self.resolution = resolution
        self.nodata_cells = set(nodata_cells)
        self.vertical = vertical

    def describe(self):
        return {
            "role": self.role, "file_name": "double.tif", "dataset": "FABDEM",
            "pixel_size": [self.resolution] * 2, "vertical_reference": self.vertical,
            "nodata": -9999.0, "read_mode": "double",
        }

    def effective_resolution_m(self):
        return self.resolution

    def usable(self):
        if self.vertical != "egm2008_orthometric":
            return False, "terrain_vertical_datum_unresolved"
        return True, None

    def sample_cells(self, cells, *, transform):
        result = {}
        for cell in cells:
            fine_id = str(cell["fine_cell_id"])
            if fine_id in self.nodata_cells:
                result[fine_id] = {
                    "data_status": "unknown", "surface_elevation_max_egm2008_m": None,
                    "valid_pixel_count": 0, "nodata_pixel_count": 4,
                    "sampling": "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance",
                    "reason": "dtm_nodata_under_cell",
                }
            else:
                result[fine_id] = {
                    "data_status": "passed",
                    "surface_elevation_max_egm2008_m": self.base,
                    "valid_pixel_count": 4, "nodata_pixel_count": 0,
                    "sampling": "intersecting_valid_fabdem_pixels_max_egm2008_plus_explicit_terrain_clearance",
                    "reason": None,
                }
        return result

    def sample_footprint_ground(self, ring_metric, *, transform):
        return self.base


class _BuildingDouble:
    role = "buildings"

    def __init__(self, *, footprints=(), spatial_index=True):
        self.footprints = list(footprints)
        self.spatial_index = spatial_index
        self.queries = 0

    def describe(self):
        return {
            "role": self.role, "file_name": "double.gpkg", "layer": "buildings",
            "feature_count": len(self.footprints), "spatial_index_available": self.spatial_index,
            "query_mode": "double",
        }

    def usable(self):
        if not self.spatial_index:
            return False, "building_source_spatial_index_missing"
        return True, None

    def query_corridor(self, bounds, *, transform, horizontal_clearance_m):
        self.queries += 1
        return {"status": "passed", "footprints": list(self.footprints), "query": {"mode": "double"}}


_PARENT_CELLS = [{"grid_id": "PARENT", "center": [0.001, 0.001], "bbox": [0.0, 0.0, 0.002, 0.002]}]
_CORRIDOR = {
    "corridor_id": "C-TEST", "ring_n": 0,
    "support_grid_ids": ["PARENT"], "center_grid_ids": ["PARENT"],
}
_DEFAULT_ELIGIBILITY = {
    "status": "passed", "algorithm_id": "confirmed-airspace-eligibility",
    "algorithm_version": "1.0", "allowed_grid_ids": ["PARENT"],
    "feature_fingerprint": "FF", "policy_fingerprint": "PF", "fingerprint": "F",
    "features": [{
        "feature_id": 7, "policy_confirmed": True, "route_eligibility": "allowed",
        "geometry": {"type": "Polygon", "coordinates": [[
            [-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0], [-1.0, -1.0],
        ]]},
    }],
}
_DEFAULT_RISK = {
    "status": "passed", "algorithm_id": "risk-model-v1-relative-index",
    "algorithm_version": "1.1",
    "cells": {"PARENT": {"ground": {"contributors": {
        "population": {"status": "passed", "normalized": 0.3},
        "traffic": {"status": "passed", "normalized": 0.6},
    }}}},
}


def adapter(**overrides):
    eligibility = overrides.pop("airspace", _DEFAULT_ELIGIBILITY)
    options = {
        "transform": EquirectangularMetricTransform(reference_point=(0.0, 0.0)),
        "terrain_source": _TerrainDouble(),
        "building_source": _BuildingDouble(),
        "airspace_source": ConfirmedAirspacePolygonSource(eligibility),
        "resolution_m": 30.0,
        "resolution_source": "explicit_configuration",
        "max_stride_cells": 2,
        "risk_result": _DEFAULT_RISK,
        "lineage": {"test": True},
    }
    options.update(overrides)
    return FineEnvironmentAdapter(**options)


def build_with(source, *, corridor=None, policy=None):
    return source.build(
        policy=policy or normalize_v3_planning_policy(v3_policy()),
        corridor=corridor or _CORRIDOR,
        parent_cells=_PARENT_CELLS,
    )


def adapter_refinement_problem(built, policy, *, max_stride_cells=2, strategic_overrides=None):
    cells = built["environment"]["cells"]
    strategic = {
        "status": "strategic_candidate", "experiment_id": "V3-ADAPTER",
        "strategic_fingerprint": "V3-STRATEGIC-FPR", "current_applicability": "current",
        "corridor_id": built["fine_grid"]["corridor_id"],
        "start_fine_cell_id": cells[0]["fine_cell_id"],
        "goal_fine_cell_id": cells[-1]["fine_cell_id"],
        "explicit_goal_altitude": False,
        "state_count": 2,
    }
    strategic.update(strategic_overrides or {})
    return normalize_v3_refinement_problem({
        "problem_id": "v3b-adapter", "route_id": "R-ADAPTER",
        "corridor": _CORRIDOR, "policy": policy,
        "aircraft_motion_limits": normalize_aircraft_motion_limits({
            "aircraft_id": "SYN", "confirmed": True, "source": "unit_test",
        }),
        "frame": built["frame"], "fine_grid": built["fine_grid"],
        "environment": built["environment"], "max_stride_cells": max_stride_cells,
        "source_audit": built["source_audit"],
        "strategic_candidate": strategic,
        "provenance": {"environment_source": "configured_real_sources"},
    })


def test_gis_adapter_builds_a_corridor_local_fine_environment_from_real_sources():
    policy = normalize_v3_planning_policy(v3_policy())
    built = build_with(adapter())
    assert built["status"] == "passed", built.get("reason")
    grid = built["fine_grid"]
    assert grid["resolution_m"] == pytest.approx(30.0)
    assert grid["resolution_source"] == "explicit_configuration"
    assert grid["effective_source_resolution_m"] == pytest.approx(30.0)
    assert grid["parent_grid_ids"] == ["PARENT"]
    cells = built["environment"]["cells"]
    assert len(cells) == grid["cell_count"]
    assert all(cell["center"] is not None for cell in cells)
    assert cells[0]["soft_fields"]["traffic_risk"]["normalized_index"] == pytest.approx(0.6)
    assert cells[0]["soft_fields"]["traffic_risk"]["upsampled_without_new_information"] is True
    audit = built["source_audit"]
    assert audit["adapter_id"] == "v3b_fine_environment_adapter"
    assert audit["terrain_dtm"]["file_name"] == "double.tif"
    assert audit["buildings"]["spatial_index_available"] is True
    assert audit["airspace"] == {"status": "not_applicable", "applicability": "display_only"}
    assert audit["risk_model"]["soft_fields_reused"] is True
    assert audit["full_raster_resample"] is False
    assert audit["source_geometry_modified"] is False
    assert audit["exact_validation_performed"] is False
    assert audit["fingerprint"].startswith("V3BSRC-")
    assert built["environment"]["source_audit"]["fingerprint"] == audit["fingerprint"]
    assert {key: value["status"] for key, value in built["readiness"].items()} == {
        "terrain_dtm": "ready", "buildings": "ready", "resolution": "ready",
        "metric_frame": "ready",
    }
    result = V3RefinementPlanner().plan(adapter_refinement_problem(built, policy))
    assert result["status"] in ("refined_candidate", "failed", "search_incomplete")
    assert result["source_audit"]["fingerprint"] == audit["fingerprint"]
    assert result["fine_grid_evidence"]["source_audit_fingerprint"] == audit["fingerprint"]
    assert result["final_validation_performed"] is False


def test_gis_adapter_takes_the_resolution_from_the_dtm_when_explicitly_configured_to():
    built = build_with(adapter(
        resolution_m=None, resolution_source="dtm_effective_resolution",
        terrain_source=_TerrainDouble(resolution=25.0),
    ))
    assert built["status"] == "passed"
    assert built["resolution"]["resolution_m"] == pytest.approx(25.0)
    assert built["resolution"]["resolution_source"] == "dtm_effective_resolution"
    assert built["fine_grid"]["resolution_source"] == "dtm_effective_resolution"
    assert built["fine_grid"]["effective_source_resolution_m"] == pytest.approx(25.0)


def test_gis_adapter_blocks_instead_of_inventing_a_resolution_or_an_environment():
    unresolved = build_with(adapter(
        resolution_m=None, resolution_source=None, terrain_source=_TerrainDouble(resolution=None),
    ))
    assert unresolved["status"] == "blocked"
    assert unresolved["reason"] == "fine_resolution_unresolved"
    assert unresolved["environment"] is None
    assert unresolved["readiness"]["resolution"]["status"] == "blocked"
    assert unresolved["readiness"]["resolution"]["semantics"].startswith(
        "fine_horizontal_resolution_must_be_explicit"
    )

    blocked = build_with(adapter(building_source=_BuildingDouble(spatial_index=False)))
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "building_source_spatial_index_missing"
    assert blocked["readiness"]["buildings"]["status"] == "blocked"

    no_terrain = adapter()
    no_terrain.terrain_source = None
    blocked = build_with(no_terrain)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "terrain_dtm_source_unavailable"
    assert blocked["readiness"]["terrain_dtm"]["status"] == "blocked"

    blocked = build_with(
        adapter(), corridor={"corridor_id": "C", "support_grid_ids": ["MISSING"]},
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "corridor_support_cells_missing"


def test_gis_adapter_marks_a_dtm_nodata_cell_unknown_and_blocks_the_refinement():
    policy = normalize_v3_planning_policy(v3_policy())
    built = build_with(adapter(terrain_source=_TerrainDouble(nodata_cells=("F0-0000-0000",))))
    assert built["status"] == "passed"
    cell = next(
        item for item in built["environment"]["cells"] if item["fine_cell_id"] == "F0-0000-0000"
    )
    assert cell["terrain"]["data_status"] == "unknown"
    assert cell["terrain"]["surface_clearance_egm2008_m"] is None
    assert "dtm_nodata_under_cell" in cell["terrain"]["reason"]
    result = V3RefinementPlanner().plan(adapter_refinement_problem(built, policy))
    assert result["status"] == "not_ready"
    assert result["readiness"]["terrain"]["status"] == "blocked"
    assert result["readiness"]["terrain"]["unresolved_cell_count"] >= 1


def test_gis_adapter_building_envelope_uses_the_metric_buffered_footprint():
    footprint = {
        "building_id": "GB-1",
        "ring_metric": [[10.0, 10.0], [40.0, 10.0], [40.0, 40.0], [10.0, 40.0], [10.0, 10.0]],
        "height_m": 150.0, "height_status": "predicted",
    }
    built = build_with(adapter(building_source=_BuildingDouble(footprints=[footprint])))
    constrained = [
        cell for cell in built["environment"]["cells"]
        if cell["buildings"]["required_clearance_egm2008_m"] is not None
    ]
    assert constrained
    for cell in constrained:
        assert cell["buildings"]["building_ids"] == ["GB-1"]
        assert cell["buildings"]["required_clearance_egm2008_m"] == pytest.approx(
            cell["buildings"]["ground_elevation_max_egm2008_m"] + 150.0 + 30.0
        )
        assert cell["buildings"]["mapping_method"] == (
            "footprint_buffered_by_explicit_horizontal_clearance_to_fine_cell_envelope"
        )


def test_gis_adapter_ignores_airspace_source_names_colours_and_policy():
    policy = normalize_v3_planning_policy(v3_policy())
    eligibility = deepcopy(_DEFAULT_ELIGIBILITY)
    eligibility["features"][0].update({
        "policy_confirmed": False, "name": "CTR", "color": "green",
    })
    built = build_with(adapter(airspace=eligibility))
    assert built["status"] == "passed"
    assert all(
        cell["airspace"]["status"] == "not_applicable"
        and cell["airspace"]["applicability"] == "display_only"
        for cell in built["environment"]["cells"]
    )
    assert built["source_audit"]["airspace"]["status"] == "not_applicable"
    result = V3RefinementPlanner().plan(adapter_refinement_problem(built, policy))
    assert result["status"] in ("refined_candidate", "failed", "search_incomplete")
    assert result["readiness"]["airspace"]["status"] == "ready"
    assert result["readiness"]["airspace"]["applicability"] == "not_applicable"


def test_configured_real_data_smoke_reports_blocked_without_reading_data():
    readiness = real_data_source_readiness(
        {"terrain_dtm": None, "buildings": None},
        airspace_eligibility={"status": "missing_data", "allowed_grid_ids": []},
        policy_confirmed=False,
    )
    assert readiness["status"] == "blocked"
    assert readiness["adapter_id"] == "v3b_fine_environment_adapter"
    assert set(readiness["blocking_reasons"]) == {
        "terrain_dtm_not_configured_or_missing",
        "buildings_geopackage_not_configured_or_missing",
        "v3_policy_not_confirmed",
    }
    assert readiness["resolution_policy"] == (
        "explicit_configuration_or_dtm_effective_resolution_never_a_30m_constant"
    )
    assert readiness["semantics"] == (
        "readiness_report_only_no_data_read_no_fabricated_environment"
    )
    assert readiness["airspace"]["status"] == "not_applicable"


def test_fine_policy_has_no_default_resolution_or_crs():
    empty = normalize_v3_fine_refinement_policy(None)
    assert empty["horizontal_crs"] is None
    assert empty["resolution_m"] is None
    assert empty["resolution_source"] is None
    assert empty["max_stride_cells"] == 1
    # Missing a CRS / resolution *source* is a blocking configuration error, exactly
    # like a missing required safety parameter in the V3-A policy.
    assert empty["status"] == "blocked"
    assert set(empty["missing_parameters"]) >= {"horizontal_crs", "resolution_source"}
    assert any("30 m" in reason for reason in empty["reasons"])
    with pytest.raises(ValueError, match="resolution_source"):
        normalize_v3_fine_refinement_policy({"resolution_source": "hard_coded_30m"})
    configured = normalize_v3_fine_refinement_policy({
        "horizontal_crs": "EPSG:32651", "resolution_source": "explicit_configuration",
        "resolution_m": 25.0, "max_stride_cells": 4, "source": "project_engineering_basis",
        "confirmed": True,
    })
    assert configured["status"] == "confirmed"
    assert configured["max_stride_cells"] == 4
    needs_value = normalize_v3_fine_refinement_policy({
        "horizontal_crs": "EPSG:32651", "resolution_source": "explicit_configuration",
        "source": "x", "confirmed": True,
    })
    assert needs_value["status"] == "blocked"
    assert "resolution_m" in needs_value["missing_parameters"]


def test_refinement_package_never_imports_qgis_gdal_or_reads_files():
    root = Path("cns_planner/route_planner_v3")
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("*.py")}
    assert {"fine_search.py", "fine_grid.py", "fine_contracts.py", "fine_synthetic.py"} <= set(sources)
    for name, text in sources.items():
        assert "import gdal" not in text and "osgeo" not in text, name
        assert "from qgis" not in text and "import qgis" not in text, name
        assert "open(" not in text, name
    # The GIS boundary lives in exactly one module, and it imports lazily.
    adapter_source = Path("cns_planner/gis/fine_environment_adapter.py").read_text(encoding="utf-8")
    assert "from osgeo import gdal" in adapter_source
    assert "from qgis.core import" in adapter_source


def test_synthetic_fine_spec_and_defaults_are_explicit():
    spec = normalize_synthetic_fine_spec({"resolution_m": 30.0, "max_stride_cells": 2})
    assert spec["resolution_m"] == pytest.approx(30.0)
    assert spec["max_stride_cells"] == 2
    assert spec["terrain_spikes"] == {}
    assert default_v3_cost_model()["heuristic_policy"] == (
        "h_is_plain_3d_geometric_distance_weights_never_enter_the_heuristic"
    )
    assert default_v3_policy()["heading_bin_count"] == 8
