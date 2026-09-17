from copy import deepcopy
from heapq import heappop, heappush
from pathlib import Path

import pytest

from cns_planner.algorithms.coverage.v1 import distance_m
from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.algorithms.registry import build_default_algorithm_registry, default_algorithm_selection
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.route_planner.risk_aware_v2 import GridGraph, RiskAwareRoutePlannerV2


DEFAULTS = Path("cns_planner/config/defaults.json")


def _grid(width=3, height=3, step=0.001):
    cells = []
    for row in range(height):
        for column in range(width):
            west, south = column * step, row * step
            cells.append({
                "grid_id": f"G-{row}-{column}", "level": 7,
                "row": row, "column": column,
                "bbox": [west, south, west + step, south + step],
                "center": [west + step / 2, south + step / 2],
            })
    return {"status": "passed", "level": 7, "cells": cells, "count": len(cells)}


def _risk(grid, scores=None, missing=(), status="passed"):
    scores = scores or {}
    cells = {}
    for cell in grid["cells"]:
        grid_id = cell["grid_id"]
        value = None if grid_id in missing else float(scores.get(grid_id, 0.05))
        cells[grid_id] = {
            name: {
                "status": "missing_data" if value is None else "passed",
                "score": value,
            }
            for name in ("overall", "ground", "air")
        }
    return {
        "status": status, "algorithm_id": "risk-model-v1-relative-index",
        "algorithm_version": "1.1", "source_versions": {"fixture": "1"},
        "cells": cells,
    }


def _route(grid, start_id="G-1-0", goal_id="G-1-2"):
    centers = {item["grid_id"]: item["center"] for item in grid["cells"]}
    return {"route_id": "R1", "start": centers[start_id], "end": centers[goal_id]}


def _eligibility(grid, allowed=None, edges=None, status="passed", fingerprint="allowed-v1"):
    graph = GridGraph(grid["cells"])
    allowed = set(allowed or graph.cells)
    if edges is None:
        edges = [
            [left, right]
            for left in sorted(allowed)
            for right in graph.neighbors(left)
            if left < right and right in allowed
        ]
    return {
        "status": status,
        "algorithm_id": "confirmed-airspace-eligibility",
        "algorithm_version": "1.0",
        "fingerprint": fingerprint,
        "feature_fingerprint": "features-v1",
        "policy_fingerprint": "policy-v1",
        "allowed_grid_ids": sorted(allowed),
        "connector_safe_grid_ids": sorted(allowed),
        "allowed_edges": edges,
    }


def test_lambda_zero_shortest_and_positive_lambda_avoids_high_risk():
    grid = _grid()
    risk = _risk(grid, {"G-1-1": 0.95})
    route = _route(grid)
    shortest = RiskAwareRoutePlannerV2({"risk_weight_lambda": 0}).plan(route, grid, risk, [], _eligibility(grid))
    avoided = RiskAwareRoutePlannerV2({"risk_weight_lambda": 10}).plan(route, grid, risk, [], _eligibility(grid))

    assert shortest["grid_path"] == ["G-1-0", "G-1-1", "G-1-2"]
    assert "G-1-1" not in avoided["grid_path"]
    assert avoided["distance_m"] > shortest["distance_m"]
    assert avoided["risk_exposure_index_m"] < shortest["risk_exposure_index_m"]
    assert avoided["optimization_cost"] == pytest.approx(
        avoided["distance_m"] + 10 * avoided["risk_exposure_index_m"]
    )


def test_risk_exposure_formula_metric_distance_and_v2_contract():
    grid = _grid(2, 1)
    risk = _risk(grid, {"G-0-0": 0.2, "G-0-1": 0.6})
    route = _route(grid, "G-0-0", "G-0-1")
    result = RiskAwareRoutePlannerV2({"risk_weight_lambda": 2, "risk_component": "ground"}).plan(route, grid, risk, [], _eligibility(grid))
    expected_distance = distance_m(route["start"], route["end"])

    assert result["status"] == "passed"
    assert result["path"][0] == route["start"] and result["path"][-1] == route["end"]
    assert result["distance_m"] == pytest.approx(expected_distance)
    assert result["risk_exposure_index_m"] == pytest.approx(expected_distance * 0.4)
    assert result["mean_risk_index"] == pytest.approx(0.4)
    assert result["max_risk_index"] == pytest.approx(0.6)
    assert 110 < result["distance_m"] < 112
    assert result["risk_semantics"] == "relative_engineering_index_not_probability"
    assert result["model_scope"] == "two_dimensional_strategic_horizontal_route"
    assert result["risk_source"]["algorithm_id"] == "risk-model-v1-relative-index"
    assert len(result["risk_fingerprint"]) == 64


def test_unknown_risk_blocks_by_default_or_uses_explicit_penalty():
    grid = _grid(3, 1)
    route = _route(grid, "G-0-0", "G-0-2")
    risk = _risk(grid, missing={"G-0-1"}, status="missing_data")
    blocked = RiskAwareRoutePlannerV2().plan(route, grid, risk, [], _eligibility(grid))
    assert blocked["status"] == "missing_data"
    assert blocked["path"] == []

    with pytest.raises(ValueError, match="显式提供"):
        RiskAwareRoutePlannerV2({"unknown_risk_policy": "penalize"})
    penalized = RiskAwareRoutePlannerV2({
        "unknown_risk_policy": "penalize", "unknown_penalty_index": 0.8,
    }).plan(route, grid, risk, [], _eligibility(grid))
    assert penalized["status"] == "passed"
    assert penalized["grid_path"] == ["G-0-0", "G-0-1", "G-0-2"]
    assert penalized["mean_risk_index"] > 0.4


def test_optional_threshold_and_stale_risk_are_explicit():
    grid = _grid(3, 1)
    route = _route(grid, "G-0-0", "G-0-2")
    threshold = RiskAwareRoutePlannerV2({"max_relative_risk_index": 0.5}).plan(
        route, grid, _risk(grid, {"G-0-1": 0.8}), [], _eligibility(grid),
    )
    assert threshold["status"] == "failed"
    assert threshold["risk_threshold_semantics"] == "engineering_relative_index_threshold_not_regulatory"

    stale = RiskAwareRoutePlannerV2().plan(route, grid, _risk(grid, status="stale"), [], _eligibility(grid))
    assert stale["status"] == "missing_data"
    assert "失效" in stale["reason"]


def test_hard_constraints_are_separate_and_diagonal_cannot_cut_blocked_corner():
    grid = _grid(2, 2)
    route = _route(grid, "G-0-0", "G-1-1")
    constraints = [
        {"bbox": [0.001, 0.0, 0.002, 0.001]},
        {"bbox": [0.0, 0.001, 0.001, 0.002]},
    ]
    result = RiskAwareRoutePlannerV2().plan(route, grid, _risk(grid), constraints, _eligibility(grid))
    assert result["status"] == "failed"
    assert result["grid_path"] == []

    start_blocked = RiskAwareRoutePlannerV2().plan(
        route, grid, _risk(grid), [{"bbox": [0.0, 0.0, 0.001, 0.001]}], _eligibility(grid),
    )
    assert start_blocked["status"] == "failed"
    assert "起点或终点" in start_blocked["reason"]
    hard_beats_stale_risk = RiskAwareRoutePlannerV2().plan(
        route, grid, _risk(grid, status="stale"),
        [{"bbox": [0.0, 0.0, 0.001, 0.001]}], _eligibility(grid),
    )
    assert hard_beats_stale_risk["status"] == "failed"
    assert "硬约束" in hard_beats_stale_risk["reason"]


def test_confirmed_allowed_is_mandatory_endpoint_checked_and_disconnected_fails():
    grid = _grid(3, 1)
    route = _route(grid, "G-0-0", "G-0-2")
    planner = RiskAwareRoutePlannerV2()
    missing = planner.plan(route, grid, _risk(grid), [], None)
    assert missing["status"] == "missing_data"
    assert "confirmed allowed" in missing["reason"]

    outside = planner.plan(
        route, grid, _risk(grid), [], _eligibility(grid, allowed={"G-0-1", "G-0-2"}),
    )
    assert outside["status"] == "failed"
    assert "外" in outside["reason"]

    disconnected = planner.plan(
        route, grid, _risk(grid), [],
        _eligibility(grid, allowed={"G-0-0", "G-0-2"}, edges=[]),
    )
    assert disconnected["status"] == "failed"
    assert disconnected["path"] == []


def test_risk_optimization_cannot_leave_allowed_graph():
    grid = _grid(3, 2)
    route = _route(grid, "G-0-0", "G-0-2")
    allowed = {"G-0-0", "G-0-1", "G-0-2"}
    risk = _risk(grid, {"G-0-1": 0.9, "G-1-0": 0.0, "G-1-1": 0.0, "G-1-2": 0.0})
    result = RiskAwareRoutePlannerV2({"risk_weight_lambda": 100}).plan(
        route, grid, risk, [], _eligibility(grid, allowed=allowed),
    )
    assert result["status"] == "passed"
    assert set(result["grid_path"]) <= allowed


def _dijkstra_cost(graph, risks, source, target, risk_lambda):
    queue = [(0.0, source)]
    costs = {source: 0.0}
    while queue:
        cost, current = heappop(queue)
        if cost != costs[current]:
            continue
        for neighbour in graph.neighbors(current):
            length = distance_m(graph.centers[current], graph.centers[neighbour])
            candidate = cost + length * (1 + risk_lambda * (risks[current] + risks[neighbour]) / 2)
            if candidate < costs.get(neighbour, float("inf")):
                costs[neighbour] = candidate
                heappush(queue, (candidate, neighbour))
    return costs[target]


def test_astar_matches_dijkstra_and_is_deterministic_on_ties():
    grid = _grid()
    risk = _risk(grid, {"G-1-1": 0.9})
    route = _route(grid)
    planner = RiskAwareRoutePlannerV2({"risk_weight_lambda": 4})
    first = planner.plan(route, grid, risk, [], _eligibility(grid))
    assert first == planner.plan(route, grid, risk, [], _eligibility(grid))
    graph = GridGraph(grid["cells"])
    values = {grid_id: value["overall"]["score"] for grid_id, value in risk["cells"].items()}
    assert first["optimization_cost"] == pytest.approx(
        _dijkstra_cost(graph, values, "G-1-0", "G-1-2", 4)
    )


def test_grid_graph_prefers_mht_grid_id_and_has_bbox_fallback():
    standard = WorkspaceGridService(preferred_level=7).generate([120, 30, 120.01, 30.01])
    indexed = GridGraph(standard["cells"])
    assert indexed.indices is not None
    assert sum(len(value) for value in indexed.adjacency.values()) > 0

    fallback_cells = deepcopy(_grid(2, 2)["cells"])
    for index, cell in enumerate(fallback_cells):
        cell["grid_id"] = f"fallback-{index}"
        cell.pop("row")
        cell.pop("column")
    fallback = GridGraph(fallback_cells)
    assert fallback.indices is None
    assert fallback.neighbors("fallback-0") == ("fallback-1", "fallback-2", "fallback-3")


def test_registry_adds_v2_but_default_and_legacy_backfill_remain_v1():
    registry = build_default_algorithm_registry({
        "engineering_parameters": {
            "primary_spacing_factor": {"value": 0.9},
            "co_location_search_radius_m": {"value": 100},
        }
    })
    manifest = registry.manifest("route_planner", "risk_aware_route_planner_v2", "2.0")
    assert manifest.maturity == "engineering_baseline"
    assert default_algorithm_selection()["route_planner"] == {
        "algorithm_type": "route_planner", "algorithm_id": "route_planner_v1",
        "version": "1.0", "parameters": {},
    }


def _mark_route_downstream_passed(workflow):
    workflow.state["result_statuses"].update({name: "passed" for name in (
        "routes", "coverage", "cns_gap", "coverage_3d", "cns_service_capability",
        "service_timeline", "cns_gap_v2", "cns_site_plan", "closed_loop_assessment",
        "report",
    )})
    workflow.state["coverage"] = {"status": "passed"}
    workflow.state["cns_gap_analysis"] = {"status": "passed"}
    workflow.state["coverage_3d"]["status"] = "passed"
    workflow.state["cns_service_capability"]["status"] = "meets_under_model"
    workflow.state["service_timeline"]["status"] = "passed"
    workflow.state["cns_gap_analysis_v2"]["status"] = "satisfied"
    workflow.state["cns_site_plan"]["status"] = "proposal_ready"
    workflow.state["closed_loop_assessment"].update({"status": "passed", "commit_status": "preview"})


def test_grid_risk_change_invalidates_routes_only_when_v2_is_selected(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    grid = _grid(2, 1)
    workflow.state["grid"] = grid
    initial = _risk(grid, {"G-0-0": 0.1})
    workflow.state["grid_risk"] = deepcopy(initial)
    _mark_route_downstream_passed(workflow)
    workflow.risk_service.apply_result(_risk(grid, {"G-0-0": 0.2}))
    assert workflow.state["result_statuses"]["routes"] == "passed"
    assert workflow.state["result_statuses"]["coverage_3d"] == "passed"

    workflow.select_algorithm({
        "algorithm_type": "route_planner", "algorithm_id": "risk_aware_route_planner_v2",
        "version": "2.0", "parameters": {"risk_weight_lambda": 1},
    })
    _mark_route_downstream_passed(workflow)
    workflow.risk_service.apply_result(_risk(grid, {"G-0-0": 0.3}))
    assert workflow.state["result_statuses"]["routes"] == "stale"
    for name in ("coverage_3d", "cns_service_capability", "service_timeline", "cns_gap_v2", "cns_site_plan", "closed_loop_assessment"):
        assert workflow.state["result_statuses"][name] == "stale"


def test_workflow_v2_consumes_grid_risk_and_parameter_change_invalidates(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    grid = _grid(3, 1)
    workflow.state["workspace"] = {"status": "passed", "bbox": [0, 0, 0.003, 0.001]}
    workflow.state["grid"] = grid
    workflow.state["grid_risk"] = _risk(grid)
    workflow.state["grid_attributes"]["airspace"]["airspace_eligibility"] = _eligibility(grid)
    workflow.state["scenario_routes"] = [_route(grid, "G-0-0", "G-0-2")]
    workflow.select_algorithm({
        "algorithm_type": "route_planner", "algorithm_id": "risk_aware_route_planner_v2",
        "version": "2.0", "parameters": {"risk_weight_lambda": 0},
    })
    result = workflow.generate_operational([])["operational_routes"][0]
    assert result["status"] == "passed"
    assert result["grid_path"] == ["G-0-0", "G-0-1", "G-0-2"]

    workflow.select_algorithm({
        "algorithm_type": "route_planner", "algorithm_id": "risk_aware_route_planner_v2",
        "version": "2.0", "parameters": {"risk_weight_lambda": 2},
    })
    assert workflow.state["result_statuses"]["routes"] == "stale"


def test_airspace_policy_change_marks_operational_and_route_dependents_stale(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{"route_id": "R1", "status": "passed", "path": []}]
    _mark_route_downstream_passed(workflow)
    workflow.set_airspace_policies({
        "items": [{
            "feature_id": "ASF-1", "route_eligibility": "allowed",
            "confirmed": True, "source": "user_confirmation",
            "evidence": [{"type": "test"}],
        }]
    })
    assert workflow.state["operational_routes"][0]["status"] == "stale"
    assert workflow.state["result_statuses"]["routes"] == "stale"
    assert workflow.state["result_statuses"]["coverage"] == "stale"
