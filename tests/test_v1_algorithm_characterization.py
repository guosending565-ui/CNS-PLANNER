"""Characterization tests for the observable Route/Coverage V1 contracts.

These fixtures are intentionally small, deterministic, and independent of QGIS,
the network, the filesystem, and machine-specific configuration.
"""

import pytest

from cns_planner.algorithms.coverage_planner import CoveragePlannerV1
from cns_planner.algorithms.route_planner import RoutePlannerV1


@pytest.fixture
def route_fixture():
    return {
        "planner": RoutePlannerV1(grid_size=6),
        "route": {
            "route_id": "R-GOLDEN-001",
            "start": [0.1, 0.1],
            "end": [0.9, 0.9],
        },
        "workspace": [0.0, 0.0, 1.0, 1.0],
    }


@pytest.fixture
def coverage_fixture():
    defaults = {
        "engineering_parameters": {
            "primary_spacing_factor": {
                "value": 0.9,
                "source": "characterization fixture",
                "confirmed": False,
            },
            "co_location_search_radius_m": {
                "value": 100,
                "source": "characterization fixture",
                "confirmed": False,
            },
        }
    }
    routes = [
        {
            "route_id": "R-COV-001",
            "status": "passed",
            "path": [[0.0, 0.0], [0.01, 0.0]],
        }
    ]
    devices = []
    for subsystem in ("C", "N", "S"):
        devices.extend(
            [
                {
                    "device_id": f"{subsystem}-PRIMARY",
                    "subsystem": subsystem,
                    "role": "primary",
                    "radius_m": 50.0,
                    "mtbf_h": 1000.0,
                    "enabled": True,
                },
                {
                    "device_id": f"{subsystem}-GAP",
                    "subsystem": subsystem,
                    "role": "gap",
                    "radius_m": 60.0,
                    "mtbf_h": 800.0,
                    "enabled": True,
                },
            ]
        )
    return {
        "planner": CoveragePlannerV1(defaults),
        "defaults": defaults,
        "routes": routes,
        "devices": devices,
    }


def test_route_v1_success_output_is_stable(route_fixture):
    constraints = [{"name": "中心硬约束", "bbox": [0.4, 0.4, 0.6, 0.6]}]
    planner = route_fixture["planner"]
    route = route_fixture["route"]
    workspace = route_fixture["workspace"]

    result = planner.plan(route, workspace, constraints)

    assert result == planner.plan(route, workspace, constraints)
    assert result == {
        "route_id": "R-GOLDEN-001",
        "status": "passed",
        "path": [
            [0.1, 0.1],
            [0.2, 0.2],
            [0.2, 0.8],
            [0.6, 0.8],
            [0.9, 0.9],
        ],
        "reason": "A* 规划完成；正式风险代价模型待接入",
        "algorithm_id": "route_planner_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": "ae10b604508d03f2445f36573156ba109e2ed30904d6e8ff5957f0dce8a936d4",
        "environment_risk": {
            "status": "pending_confirmation",
            "value": None,
            "unit": None,
            "threshold": None,
            "source": "GRC 接口待正式模型",
        },
    }


def test_route_v1_hard_constraint_failure_output_is_stable(route_fixture):
    constraints = [{"name": "纵向阻断", "bbox": [0.4, 0.0, 0.6, 1.0]}]
    planner = route_fixture["planner"]
    route = route_fixture["route"]
    workspace = route_fixture["workspace"]

    result = planner.plan(route, workspace, constraints)

    assert result == planner.plan(route, workspace, constraints)
    assert result == {
        "route_id": "R-GOLDEN-001",
        "status": "failed",
        "path": [],
        "reason": "管制/硬约束阻断，未找到可用路径",
        "algorithm_id": "route_planner_v1",
        "algorithm_version": "1.0",
        "input_fingerprint": "b6d48296f0677987f1595eb877843a1b97ff7466c4373248cb3fc1b028d692f9",
        "environment_risk": {
            "status": "pending_confirmation",
            "value": None,
            "unit": None,
            "threshold": None,
            "source": "GRC 接口待正式模型",
        },
    }


def test_coverage_v1_cns_station_and_statistics_output_is_stable(coverage_fixture):
    planner = coverage_fixture["planner"]
    routes = coverage_fixture["routes"]
    devices = coverage_fixture["devices"]

    result = planner.plan(routes, devices)

    assert result == planner.plan(routes, devices)
    assert set(result) == {
        "status",
        "layers",
        "physical_sites",
        "algorithm_id",
        "algorithm_version",
        "input_fingerprint",
        "parameters",
    }
    assert result["status"] == "passed"
    assert result["algorithm_id"] == "coverage_planner_v1"
    assert result["algorithm_version"] == "1.0"
    assert result["input_fingerprint"] == "f08786131309d848ff6f38f9d97ff45eec6ea9703220da5dbba0c3e909aaef68"
    assert result["parameters"] == coverage_fixture["defaults"]["engineering_parameters"]
    assert set(result["layers"]) == {"C", "N", "S"}

    primary_coordinates = [[0.0, 0.0], [0.002, 0.0], [0.004, 0.0], [0.006, 0.0], [0.008, 0.0], [0.01, 0.0]]
    expected_site_ids = [f"SITE-{index:04d}" for index in range(1, 7)]
    assert result["physical_sites"] == [
        {
            "site_id": site_id,
            "coordinate": coordinate,
            "source": "CoveragePlannerV1 拟建点",
        }
        for site_id, coordinate in zip(expected_site_ids, primary_coordinates)
    ]

    expected_statistics = {
        "C": {
            "stations": 8,
            "primary": 6,
            "gap": 2,
            "colocated": 2,
            "average_multiplicity": 1.0,
            "uncovered_samples": 0,
            "uncovered_segments": [],
        },
        "N": {
            "stations": 8,
            "primary": 6,
            "gap": 2,
            "colocated": 8,
            "average_multiplicity": 1.0,
            "uncovered_samples": 0,
            "uncovered_segments": [],
        },
        "S": {
            "stations": 8,
            "primary": 6,
            "gap": 2,
            "colocated": 8,
            "average_multiplicity": 1.0,
            "uncovered_samples": 0,
            "uncovered_segments": [],
        },
    }
    for subsystem, layer in result["layers"].items():
        stations = layer["stations"]
        assert layer["status"] == "passed"
        assert layer["message"] == "初版覆盖计算完成"
        assert layer["statistics"] == expected_statistics[subsystem]
        assert [item["station_id"] for item in stations] == [
            f"{subsystem}-R-COV-001-{index:03d}" for index in range(1, 9)
        ]
        assert [item["site_id"] for item in stations] == expected_site_ids + ["SITE-0003", "SITE-0004"]
        assert [item["coordinate"] for item in stations] == primary_coordinates + [[0.004, 0.0], [0.006, 0.0]]
        assert [item["role"] for item in stations] == ["primary"] * 6 + ["gap"] * 2
        assert [item["device_id"] for item in stations] == [f"{subsystem}-PRIMARY"] * 6 + [f"{subsystem}-GAP"] * 2
        assert [item["colocated"] for item in stations] == ([False] * 6 + [True] * 2 if subsystem == "C" else [True] * 8)


def test_coverage_v1_missing_primary_reports_no_uncovered_geometry(coverage_fixture):
    planner = coverage_fixture["planner"]
    routes = coverage_fixture["routes"]
    devices = [
        item
        for item in coverage_fixture["devices"]
        if not (item["subsystem"] == "S" and item["role"] == "primary")
    ]

    result = planner.plan(routes, devices)

    assert result["status"] == "failed"
    assert result["input_fingerprint"] == "a204cfcf0bc7767824d154b79dc51205a72926ccd0e499bf73efa5d6b82fcfc9"
    assert result["layers"]["S"] == {
        "status": "missing_data",
        "stations": [],
        "statistics": {
            "stations": 0,
            "primary": 0,
            "gap": 0,
            "colocated": 0,
            "average_multiplicity": 0,
            "uncovered_samples": 0,
            "uncovered_segments": [],
        },
        "message": "缺少主站设备",
    }
