import math
from pathlib import Path

from cns_planner.services.conflict_grid_service import ConflictGridService
from cns_planner.services.grid_spatial_index import bbox_area_km2
from cns_planner.services.traffic_grid_service import TrafficGridService
from cns_planner.simulation.conflict_detector import ConflictDetector
from cns_planner.simulation.traffic_simulator import TrafficSimulator
from cns_planner.services.workflow import WorkflowService


def _grid():
    return {
        "status": "passed",
        "level": 7,
        "count": 2,
        "cells": [
            {"grid_id": "G1", "bbox": [0.0, 0.0, 1.0, 1.0], "geometry": {"type": "Polygon"}},
            {"grid_id": "G2", "bbox": [1.0, 0.0, 2.0, 1.0], "geometry": {"type": "Polygon"}},
        ],
    }


def _trajectory(uav_id, points):
    return {
        "uav_id": uav_id,
        "samples": [
            {"time_s": time_s, "coordinate": coordinate, "altitude_m": 100.0}
            for time_s, coordinate in points
        ],
    }


def _defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir()
    target.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target


def _health():
    return {
        "status": "passed",
        "population": {"status": "passed"},
        "airspace": {"status": "passed"},
        "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"},
        "property": {"status": "missing_data"},
        "loaded_layer_count": 1,
        "covered_layer_count": 1,
    }


def test_traffic_simulator_is_reproducible_for_multiple_uavs():
    parameters = {
        "uav_count": 3,
        "od_pairs": [{"start": [120.0, 30.0], "end": [120.01, 30.01]}],
        "speed_range_mps": [15.0, 25.0],
        "altitude_m": 120.0,
        "simulation_seconds": 60.0,
        "timestep_seconds": 5.0,
        "random_seed": 42,
    }

    first = TrafficSimulator().simulate(parameters)
    second = TrafficSimulator().simulate(parameters)

    assert first == second
    assert first["trajectory_count"] == 3
    assert {item["uav_id"] for item in first["trajectories"]} == {
        "UAV-0001", "UAV-0002", "UAV-0003"
    }
    assert all(item["samples"][0]["coordinate"] == [120.0, 30.0] for item in first["trajectories"])
    assert all(item["speed_mps"] >= 15.0 for item in first["trajectories"])


def test_traffic_residence_time_maps_to_grid_id_and_accumulates_all_uavs():
    simulation = {
        "status": "passed",
        "algorithm_id": "fixture-simulator",
        "algorithm_version": "1",
        "input_fingerprint": "fixture",
        "simulation_seconds": 10.0,
        "trajectories": [
            _trajectory("U1", [(0.0, [0.5, 0.5]), (10.0, [1.5, 0.5])]),
            _trajectory("U2", [(0.0, [0.25, 0.25]), (10.0, [0.75, 0.25])]),
        ],
    }

    result = TrafficGridService().map(_grid(), simulation, {
        "normalization": {"value": 15.0 / (bbox_area_km2([0.0, 0.0, 1.0, 1.0]) * 10.0)}
    })

    first, second = result["cells"]["G1"], result["cells"]["G2"]
    assert first["flight_count"] == 2
    assert second["flight_count"] == 1
    assert math.isclose(first["flight_seconds"], 15.0)
    assert math.isclose(second["flight_seconds"], 5.0)
    assert math.isclose(first["traffic_density_raw"], 15.0 / (first["grid_area_km2"] * 10.0))
    assert first["traffic_density_norm"] == 1.0
    assert 0 < second["traffic_density_norm"] < 1
    assert all("geometry" not in cell for cell in result["cells"].values())


def test_two_dimensional_cpa_detects_once_per_pair_with_cooldown():
    times = range(0, 11)
    trajectories = [
        _trajectory("U1", [(t, [-0.001 + 0.0002 * t, 0.0]) for t in times]),
        _trajectory("U2", [(t, [0.0, -0.001 + 0.0002 * t]) for t in times]),
    ]

    result = ConflictDetector().detect(trajectories, {
        "safe_distance_m": 50.0,
        "lookahead_seconds": 10.0,
        "conflict_cooldown_seconds": 30.0,
    })

    assert result["conflict_count"] == 1
    event = result["events"][0]
    assert event["pair"] == ["U1", "U2"]
    assert math.isclose(event["time_s"], 5.0, abs_tol=1e-9)
    assert event["distance_cpa_m"] < 1e-6
    assert all(abs(value) < 1e-12 for value in event["coordinate"])


def test_conflict_points_map_to_grid_and_rate_is_normalized():
    detection = {
        "status": "passed",
        "algorithm_id": "two-dimensional-cpa-v1",
        "algorithm_version": "1.0",
        "parameters": {"safe_distance_m": 50.0},
        "events": [{
            "pair": ["U1", "U2"], "time_s": 5.0, "time_to_cpa_s": 5.0,
            "distance_cpa_m": 0.0, "coordinate": [0.5, 0.5],
            "status": "potential_conflict",
        }],
    }

    result = ConflictGridService().map(
        _grid(), detection, 10.0, {"normalization": {"value": 0.2}}
    )

    assert result["covered_count"] == 1
    assert result["cells"]["G1"]["conflict_count"] == 1
    assert result["cells"]["G1"]["conflict_rate"] == 0.1
    assert result["cells"]["G1"]["conflict_rate_norm"] == 0.5
    assert result["cells"]["G1"]["conflict_points"][0]["pair"] == ["U1", "U2"]
    assert result["cells"]["G2"]["conflict_count"] == 0
    assert result["cells"]["G2"]["conflict_rate_norm"] == 0.0


def test_workflow_saves_traffic_conflict_and_resets_them_with_workspace(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    service = WorkflowService(store, _defaults_path(tmp_path))
    service.set_workspace([120.0, 30.0, 120.02, 30.02], _health())
    parameters = {
        "simulation": {
            "uav_count": 2,
            "od_pairs": [
                {"start": [120.001, 30.01], "end": [120.019, 30.01]},
                {"start": [120.01, 30.001], "end": [120.01, 30.019]},
            ],
            "speed_range_mps": [100.0, 100.0],
            "altitude_m": 120.0,
            "simulation_seconds": 30.0,
            "timestep_seconds": 1.0,
            "random_seed": 7,
        },
        "conflict": {
            "safe_distance_m": 100.0,
            "lookahead_seconds": 10.0,
            "conflict_cooldown_seconds": 20.0,
        },
    }

    state = service.run_traffic_simulation(parameters)

    assert state["grid_attributes"]["traffic"]["status"] == "passed"
    assert state["grid_attributes"]["traffic"]["count"] == state["grid"]["count"]
    assert state["grid_attributes"]["conflict"]["status"] == "passed"
    assert state["grid_risk"]["input_status"]["traffic"] == "passed"
    assert state["grid_risk"]["input_status"]["conflict"] == "passed"
    restored = WorkflowService(store, service.defaults_path)
    assert restored.state["traffic_simulation"] == state["traffic_simulation"]
    assert restored.grid_attributes_snapshot()["traffic"] == state["grid_attributes"]["traffic"]

    restored.invalidate_grid_attributes({"traffic_simulation"})
    assert restored.state["grid_attributes"]["traffic"]["status"] == "stale"
    assert restored.state["grid_attributes"]["conflict"]["status"] == "stale"
    assert restored.state["grid_risk"]["status"] == "stale"

    changed = restored.set_workspace([120.03, 30.03, 120.04, 30.04], _health())
    assert changed["grid_attributes"]["traffic"]["status"] == "not_calculated"
    assert changed["grid_attributes"]["conflict"]["status"] == "not_calculated"
    assert changed["grid_risk"]["status"] == "not_calculated"
    assert changed["traffic_simulation"] is None
