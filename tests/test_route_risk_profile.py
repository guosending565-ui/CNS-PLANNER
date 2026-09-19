"""RouteRiskProfile V1 tests.

覆盖：planner 共享积分 helper 后的 candidate distance/cost characterization、segment 长度与
累计距离、domain exposure/mean/max、active domain 与 candidate cost_breakdown 的一致性、
缺失 domain/factor 只产生 unresolved 长度、factor provenance/contributor、未确认阈值仍生成
profile、已确认阈值的 low/medium/high 与连续 high interval 合并、非法阈值拒绝、airspace 无影响、
RiskV2/candidate/policy 变化只 stale profile、profile 不写 operational_routes/CNS、legacy
backfill、stale/不一致 candidate 拒绝，以及 additive API 路由。
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.domain.risk_v2 import DOMAIN_IDS, FACTOR_IDS
from cns_planner.domain.route_risk_profile import (
    ALGORITHM_ID, ARTIFACT_TYPE, classify_domain_index, default_route_risk_profile_collection,
    default_route_risk_profile_policy, empty_route_risk_profile, normalize_route_risk_profile,
    normalize_route_risk_profile_collection, normalize_route_risk_profile_policy,
    route_risk_profile_policy_fingerprint,
)
from cns_planner.domain.spatial_3d import normalize_spatial_3d
from cns_planner.risk.route_exposure import (
    DOMAIN_CELL_CONTAINER_KEY, integrate_path_exposure, resolve_cell_domain_indices,
)
from cns_planner.risk.route_profile import RouteRiskProfiler, build_route_risk_profile

DEFAULTS = Path("cns_planner/config/defaults.json")
LAYER_ID = "L8-LOW"
LOW_ALTITUDE = 300.0

#: Characterization golden values: the planner's own integral for the 3x1 fixture below.
GOLDEN_DISTANCE_M = 1925.858241815
GOLDEN_GROUND_EXPOSURE_INDEX_M = 1011.075576953


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def grid_cells(columns=3, rows=1, level=8):
    cells = []
    for column in range(columns):
        for row in range(rows):
            west = 122.0 + 0.01 * column
            south = 30.0 + 0.01 * row
            cells.append({
                "grid_id": f"MHT4063-L{level}-C{column}-RP{row}", "level": level,
                "column": column, "row": row,
                "bbox": [west, south, west + 0.01, south + 0.01],
                "center": [west + 0.005, south + 0.005],
            })
    return cells


def layer(**overrides):
    payload = {
        "altitude_layer_id": LAYER_ID, "name": "低层",
        "nominal_altitude_m": LOW_ALTITUDE, "lower_altitude_m": 250.0, "upper_altitude_m": 350.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True,
    }
    payload.update(overrides)
    return payload


def workflow(tmp_path, name="project.json", *, cells=None, route=True, endpoint_inset=0.0):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    grid = grid_cells(columns=3, rows=1) if cells is None else cells
    service.state["grid"] = {
        "status": "passed", "level": 8, "count": len(grid), "cells": grid,
    }
    if route:
        # A non-zero inset keeps the endpoint connectors non-degenerate, so the first/last
        # segment exercises the real endpoint→cell connector length.
        start = list(grid[0]["center"])
        end = list(grid[-1]["center"])
        if endpoint_inset:
            start = [start[0] - endpoint_inset, start[1] - endpoint_inset]
            end = [end[0] + endpoint_inset, end[1] + endpoint_inset]
        service.state["scenario_routes"] = [{
            "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
            "start": start, "end": end,
        }]
    service.state["spatial_3d"]["altitude_layers"] = [
        normalize_spatial_3d({"altitude_layers": [layer()]})["altitude_layers"][0]
    ]
    return service


def confirmed_environment(service, *, clearance=50.0, ground=1.0, air=0.0, environment=0.0):
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": clearance, "source": "工程确认-测试", "confirmed": True,
    })
    service.set_layered_route_cost_policy({
        "ground_lambda": ground, "air_traffic_lambda": air,
        "environment_obstacle_lambda": environment, "source": "工程确认-测试", "confirmed": True,
    })
    service.state["building_clearance_policy"].update({
        "horizontal_clearance_m": 0.0, "vertical_clearance_m": 10.0,
        "source": "工程确认-测试", "confirmed": True, "status": "confirmed",
    })


def confirmed_request(service, route_id="R0001"):
    service.set_layered_route_planning_request({
        "scenario_route_id": route_id, "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    })


def confirmed_profile_policy(service, *, medium=0.3, high=0.8, domains=DOMAIN_IDS):
    payload = {"domains": {}}
    for domain_id in domains:
        payload["domains"][domain_id] = {
            "medium_min": medium, "high_min": high,
            "source": "工程确认-测试", "confirmed": True,
        }
    service.set_route_risk_profile_policy(payload)
    return payload


def stub_facts(grid, elevation=10.0):
    return [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": elevation,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": {
                "data_status": "passed", "building_count": 0, "height_max_m": None,
                "valid_height_fraction": None,
            },
        }
        for cell in grid
    ]


class StubAdapter:
    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    def build_cells(self, grid_cells, state):
        self.calls += 1
        return deepcopy(self.facts)

    def describe(self):
        return {"adapter_id": "stub"}


def risk_v2(cells, index=0.5, *, indices=None, missing_domains=None, factor_indices=None,
            policy=None, factor_ids=None):
    """Structural ``grid_risk_v2`` fixture: the **canonical** nested cell schema.

    Canonical (see ``risk/accessors_v2.py``) is
    ``cell = {"status":..., "domains": {domain_id: {"index":..., "status":...}},
    "factors": {factor_id: {...}}}``.  The historical flat ``cell[domain_id]`` form is
    deliberately *not* produced here any more: it masked the production contract and made
    the planner fail closed on real ``λ > 0`` data.
    """

    indices = indices or {}
    missing_domains = missing_domains or {}
    factor_indices = factor_indices or {}
    factor_ids = tuple(FACTOR_IDS if factor_ids is None else factor_ids)
    cell_map = {}
    for cell in cells:
        grid_id = cell["grid_id"]
        value = indices.get(grid_id, index)
        domains = {}
        for domain_id in DOMAIN_IDS:
            if domain_id in (missing_domains.get(grid_id) or set()):
                domains[domain_id] = {
                    "domain_id": domain_id, "status": "missing_data", "index": None,
                }
            else:
                domains[domain_id] = {
                    "domain_id": domain_id, "status": "passed", "index": value,
                }
        factor_value = factor_indices.get(grid_id, value)
        factors = {
            factor_id: {
                "factor_id": factor_id, "domain": (
                    "ground" if factor_id == "population_exposure"
                    else "air_traffic" if factor_id in ("uav_traffic_exposure", "conflict_exposure")
                    else "environment_obstacle"
                ),
                "status": "passed", "resolved": True,
                "normalized_index": factor_value,
                "raw_value": 10.0 * (factor_value + 1.0),
                "source_id": f"src-{factor_id}", "source_fingerprint": f"fp-{factor_id}",
                "source_role": f"role-{factor_id}",
                "normalization": {
                    "method": "ratio_to_dataset_quantile",
                    "reference_fingerprint": f"ref-{factor_id}",
                },
            }
            for factor_id in factor_ids
        }
        cell_map[grid_id] = {
            "grid_id": grid_id, "status": "passed",
            "domains": domains, "factors": factors,
        }
    result = {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains", "algorithm_version": "2.0",
        "input_fingerprint": "riskv2-input-fixture", "policy_fingerprint": "riskv2-policy-fixture",
        "factor_status": {
            factor_id: {
                "source_id": f"src-{factor_id}", "source_fingerprint": f"fp-{factor_id}",
                "normalization": {"reference_fingerprint": f"ref-{factor_id}"},
            }
            for factor_id in FACTOR_IDS
        },
        "cells": cell_map,
    }
    if policy is not None:
        result["policy"] = policy
    return result


def confirmed_risk_policy(domain="ground", weights=None):
    weights = weights or {"population_exposure": 1.0}
    domains = {}
    for domain_id in DOMAIN_IDS:
        confirmed = domain_id == domain
        domains[domain_id] = {
            "domain_id": domain_id,
            "method": "weighted_sum" if confirmed else None,
            "weights": dict(weights) if confirmed else {},
            "required_factors": list(weights) if confirmed else [],
            "source": "工程确认-测试" if confirmed else "未配置",
            "confirmed": confirmed,
            "status": "confirmed" if confirmed else "pending_confirmation",
        }
    return {"status": "confirmed" if domain else "pending_confirmation", "domains": domains}


def prepare(tmp_path, *, cells=None, risk=None, name="project.json", policy=None,
            endpoint_inset=0.0, **environment):
    service = workflow(tmp_path, name, cells=cells, endpoint_inset=endpoint_inset)
    confirmed_environment(service, **environment)
    confirmed_request(service)
    grid = service.state["grid"]["cells"]
    service.state["grid_risk_v2"] = risk if risk is not None else risk_v2(grid)
    service.evaluate_layered_route_candidate({}, adapter=StubAdapter(stub_facts(grid)))
    if policy is not None:
        service.set_route_risk_profile_policy(policy)
    return service, grid


def evaluate_profile(service, payload=None):
    snapshot = service.evaluate_route_risk_profile(payload or {})
    return snapshot["route_risk_profiles"]


# --------------------------------------------------------------------------------------
# 1. shared integral helper: characterization + planner/profile identity
# --------------------------------------------------------------------------------------


def test_shared_integral_keeps_the_candidate_cost_characterization(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(
        tmp_path, cells=grid, risk=risk_v2(grid, indices=values, factor_indices=values),
    )
    candidate = service.layered_route_candidates()["items"][-1]
    assert candidate["status"] == "candidate"
    # Golden values frozen before/after the shared-helper extraction.
    assert candidate["distance_m"] == pytest.approx(GOLDEN_DISTANCE_M, abs=1e-9)
    breakdown = candidate["cost_breakdown"]
    assert breakdown["domain_exposure_index_m"]["ground"] == pytest.approx(
        GOLDEN_GROUND_EXPOSURE_INDEX_M, abs=1e-9
    )
    assert breakdown["mean_domain_index"]["ground"] == pytest.approx(
        GOLDEN_GROUND_EXPOSURE_INDEX_M / GOLDEN_DISTANCE_M, rel=1e-9
    )
    assert candidate["optimization_cost"] == pytest.approx(
        GOLDEN_DISTANCE_M + GOLDEN_GROUND_EXPOSURE_INDEX_M, rel=1e-12
    )

    # The same helper, called directly, must reproduce the planner numbers exactly.
    indices, _unresolved = resolve_cell_domain_indices(
        service.state["grid_risk_v2"], candidate["grid_path"], ("ground",),
    )
    centers = {cell["grid_id"]: list(cell["center"]) for cell in grid}
    integral = integrate_path_exposure(
        start=candidate["path"][0], end=candidate["path"][-1],
        grid_path=candidate["grid_path"], centers=centers, indices=indices,
        domain_ids=("ground",), integration_domains=("ground",),
    )
    assert integral["distance_m"] == pytest.approx(candidate["distance_m"], abs=1e-9)
    assert integral["domain_exposure_index_m"]["ground"] == pytest.approx(
        breakdown["domain_exposure_index_m"]["ground"], abs=1e-9
    )
    assert len(integral["segments"]) == len(candidate["grid_path"]) + 1


def test_profile_integral_equals_the_candidate_cost_breakdown(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(tmp_path, cells=grid, risk=risk_v2(grid, indices=values))
    candidate = service.layered_route_candidates()["items"][-1]
    profile = evaluate_profile(service)["items"][-1]
    assert profile["status"] == "passed"
    assert profile["route_length_m"] == pytest.approx(candidate["distance_m"], abs=1e-9)
    breakdown = candidate["cost_breakdown"]
    for domain_id in breakdown["active_domains"]:
        assert profile["domains"][domain_id]["exposure_index_m"] == pytest.approx(
            breakdown["domain_exposure_index_m"][domain_id], abs=1e-6
        )
        assert profile["domains"][domain_id]["mean_index"] == pytest.approx(
            breakdown["mean_domain_index"][domain_id], abs=1e-9
        )
    assert profile["consistency"]["status"] == "passed"


# --------------------------------------------------------------------------------------
# 2. segments
# --------------------------------------------------------------------------------------


def test_segment_lengths_and_cumulative_distances_are_consistent(tmp_path):
    service, grid = prepare(tmp_path, endpoint_inset=0.002)
    profile = evaluate_profile(service)["items"][-1]
    segments = profile["segments"]
    assert len(segments) == len(grid) + 1
    assert segments[0]["start_cumulative_distance_m"] == 0.0
    total = 0.0
    for index, segment in enumerate(segments):
        assert segment["index"] == index
        assert segment["start_cumulative_distance_m"] == pytest.approx(total, abs=1e-9)
        total += segment["length_m"]
        assert segment["end_cumulative_distance_m"] == pytest.approx(total, abs=1e-9)
        assert segment["length_m"] >= 0
    # With an inset endpoint every connector segment has a real length.
    assert all(segment["length_m"] > 0 for segment in segments)
    assert total == pytest.approx(profile["route_length_m"], abs=1e-6)
    assert profile["route_length_m"] > 0
    # Endpoint connectors reuse the first/last cell index and are labelled as such.
    assert segments[0]["from_grid_id"] is None
    assert segments[0]["to_grid_id"] == grid[0]["grid_id"]
    assert segments[0]["from_grid_role"] == "route_endpoint"
    assert segments[0]["connector"] == "route_start_endpoint_to_first_grid_cell_center"
    assert segments[-1]["to_grid_id"] is None
    assert segments[-1]["from_grid_id"] == grid[-1]["grid_id"]
    assert segments[-1]["connector"] == "last_grid_cell_center_to_route_end_endpoint"
    assert segments[0]["domains"]["ground"]["start_index"] == (
        segments[0]["domains"]["ground"]["end_index"]
    )


# --------------------------------------------------------------------------------------
# 3. domain exposure / mean / max
# --------------------------------------------------------------------------------------


def test_domain_exposure_mean_and_max_follow_the_planner_formula(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(tmp_path, cells=grid, risk=risk_v2(grid, indices=values))
    profile = evaluate_profile(service)["items"][-1]
    segments = profile["segments"]
    expected = 0.0
    for segment in segments:
        record = segment["domains"]["ground"]
        expected += segment["length_m"] * record["mean_index"]
    ground = profile["domains"]["ground"]
    assert ground["exposure_index_m"] == pytest.approx(expected, rel=1e-9)
    assert ground["mean_index"] == pytest.approx(expected / profile["route_length_m"], rel=1e-9)
    assert ground["max_index"] == 0.9
    assert ground["max_location"]["grid_id"] == grid[-1]["grid_id"]
    assert ground["max_location"]["role"] == "grid_cell"
    assert ground["max_location"]["coordinate"] == list(grid[-1]["center"])
    assert ground["coverage"] == 1.0
    assert ground["unresolved_length_m"] == 0.0
    assert ground["status"] == "resolved"
    assert ground["active_cost_domain"] is True
    assert profile["domains"]["air_traffic"]["active_cost_domain"] is False


def test_segment_domain_indices_use_the_mean_of_both_endpoint_cells(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.6, 1.0))}
    service, grid = prepare(tmp_path, cells=grid, risk=risk_v2(grid, indices=values))
    profile = evaluate_profile(service)["items"][-1]
    middle = profile["segments"][1]
    assert middle["domains"]["ground"]["start_index"] == 0.2
    assert middle["domains"]["ground"]["end_index"] == 0.6
    assert middle["domains"]["ground"]["mean_index"] == pytest.approx(0.4, abs=1e-9)
    assert middle["domains"]["ground"]["exposure_index_m"] == pytest.approx(
        middle["length_m"] * 0.4, rel=1e-9
    )


# --------------------------------------------------------------------------------------
# 4. missing domain / factor: unresolved length, never zero
# --------------------------------------------------------------------------------------


def test_missing_domain_index_produces_unresolved_length_instead_of_zero(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(
        tmp_path, cells=grid, endpoint_inset=0.002,
        risk=risk_v2(
            grid, indices=values, missing_domains={grid[1]["grid_id"]: {"air_traffic"}},
        ),
    )
    profile = evaluate_profile(service)["items"][-1]
    air = profile["domains"]["air_traffic"]
    assert air["unresolved_length_m"] > 0
    assert air["exposure_index_m"] is not None  # only resolved segments contribute
    assert air["coverage"] < 1.0
    assert air["resolved_length_m"] + air["unresolved_length_m"] == pytest.approx(
        profile["route_length_m"], abs=1e-6
    )
    assert air["status"] == "partial"
    assert grid[1]["grid_id"] in air["unresolved_cells"]
    # The unresolved segment itself has no exposure value (it is not zero-filled).
    unresolved = [
        segment for segment in profile["segments"]
        if not segment["domains"]["air_traffic"]["resolved"]
    ]
    assert unresolved
    assert all(
        segment["domains"]["air_traffic"]["exposure_index_m"] is None
        and segment["domains"]["air_traffic"]["mean_index"] is None
        for segment in unresolved
    )


def test_missing_factor_data_keeps_unresolved_length_and_marks_it(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    service, grid = prepare(
        tmp_path, cells=grid, risk=risk_v2(grid, factor_ids=("population_exposure",)),
    )
    profile = evaluate_profile(service)["items"][-1]
    # The fixture only carries the population factor: everything else is unresolved.
    assert profile["factors"]["population_exposure"]["resolved_length_m"] == pytest.approx(
        profile["route_length_m"], abs=1e-6
    )
    factor = profile["factors"]["property_exposure"]
    assert factor["unresolved_length_m"] == pytest.approx(profile["route_length_m"], abs=1e-6)
    assert factor["resolved_length_m"] == 0.0
    assert factor["normalized_exposure_index_m"] is None
    assert factor["weighted_contribution_index_m"] is None
    assert factor["canonical_source_available"] is False
    assert factor["contribution_status"] == "not_available"
    assert profile["contributors"]["status"] == "not_available"
    assert profile["contributors"]["ranking"] == []


def test_domain_without_any_grid_risk_cell_is_unresolved_never_zero(tmp_path):
    service, grid = prepare(tmp_path, risk={"status": "passed", "cells": {}}, ground=0.0)
    candidate = service.layered_route_candidates()["items"][-1]
    assert candidate["status"] == "candidate"  # λ=0 means no domain is a planning input
    profile = evaluate_profile(service)["items"][-1]
    assert profile["status"] == "passed"
    for domain_id in DOMAIN_IDS:
        domain = profile["domains"][domain_id]
        assert domain["resolved_length_m"] == 0.0
        assert domain["unresolved_length_m"] == pytest.approx(profile["route_length_m"], abs=1e-6)
        assert domain["exposure_index_m"] is None
        assert domain["mean_index"] is None
        assert domain["max_index"] is None
        assert domain["coverage"] == 0.0
        assert domain["status"] == "unresolved"
    assert profile["segments"]
    assert all(
        segment["domains"]["ground"]["exposure_index_m"] is None
        for segment in profile["segments"]
    )


# --------------------------------------------------------------------------------------
# 5. factor provenance / contributors
# --------------------------------------------------------------------------------------


def test_factor_provenance_and_contributor_ranking(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    risk = risk_v2(
        grid, indices=values, factor_indices=values, policy=confirmed_risk_policy(),
    )
    service, grid = prepare(tmp_path, risk=risk)
    profile = evaluate_profile(service)["items"][-1]
    population = profile["factors"]["population_exposure"]
    expected = 0.0
    for segment in profile["segments"]:
        record = segment["factors"]["population_exposure"]
        expected += segment["length_m"] * record["mean_index"]
    assert population["normalized_exposure_index_m"] == pytest.approx(expected, rel=1e-9)
    assert population["weighted_contribution_index_m"] == pytest.approx(expected, rel=1e-9)
    assert population["weight"] == 1.0
    assert population["contribution_status"] == "available"
    assert population["contributor_rank"] == 1
    assert population["source_ids"] == ["src-population_exposure"]
    assert population["source_fingerprints"] == ["fp-population_exposure"]
    assert population["normalization_reference_fingerprints"] == ["ref-population_exposure"]
    assert population["provenance"]["normalization_method"] == "ratio_to_dataset_quantile"
    assert population["provenance"]["not_accident_cause_probability"] is True
    assert population["provenance"]["canonical_field"] == "population_density_people_km2"
    contributors = profile["contributors"]
    assert contributors["status"] == "available"
    assert contributors["ranking"][0]["factor_id"] == "population_exposure"
    assert contributors["ranking"][0]["rank"] == 1
    assert contributors["semantics"] == (
        "relative_engineering_contribution_not_accident_cause_probability"
    )
    # Segment-level factor evidence keeps both endpoints and their provenance.
    segment_factor = profile["segments"][1]["factors"]["population_exposure"]
    assert segment_factor["from"]["grid_id"] == grid[0]["grid_id"]
    assert segment_factor["to"]["grid_id"] == grid[1]["grid_id"]
    assert segment_factor["from"]["source_fingerprint"] == "fp-population_exposure"
    assert segment_factor["provenance"]["unresolved_never_zero"] is True
    assert profile["domains"]["ground"]["contributors"]["population_exposure"][
        "contributor_rank"
    ] == 1


# --------------------------------------------------------------------------------------
# 6. thresholds: not configured vs confirmed
# --------------------------------------------------------------------------------------


def test_default_policy_has_no_thresholds_and_no_default_values():
    policy = default_route_risk_profile_policy()
    assert policy["status"] == "not_configured"
    assert policy["parameter_status"] == "no_default_thresholds"
    for domain_id in DOMAIN_IDS:
        assert policy["domains"][domain_id]["medium_min"] is None
        assert policy["domains"][domain_id]["high_min"] is None
        assert policy["domains"][domain_id]["confirmed"] is False
        assert policy["domains"][domain_id]["status"] == "not_configured"


def test_profile_is_generated_without_thresholds_but_high_risk_is_not_configured(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(tmp_path, cells=grid, risk=risk_v2(grid, indices=values))
    profile = evaluate_profile(service)["items"][-1]
    assert profile["status"] == "passed"
    ground = profile["domains"]["ground"]
    assert ground["exposure_index_m"] is not None
    assert ground["mean_index"] is not None
    assert ground["max_index"] is not None
    assert ground["classification"]["status"] == "not_configured"
    assert ground["classification"]["level"] is None
    assert ground["classification"]["thresholds"] == {"medium_min": None, "high_min": None}
    assert ground["classification"]["reason"] == "thresholds_not_configured"
    assert ground["high_risk"]["status"] == "not_configured"
    assert ground["high_risk"]["length_m"] is None
    assert ground["high_risk"]["interval_count"] is None
    assert ground["high_risk"]["intervals"] is None
    assert profile["classification"]["status"] == "not_configured"
    assert profile["high_risk"]["status"] == "not_configured"
    assert profile["high_risk"]["cross_domain_high_risk"] == "not_computed"
    assert profile["classification"]["cross_domain_overall"] == "not_computed"


def test_confirmed_thresholds_classify_and_merge_continuous_high_segments(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.9, 0.9, 0.1))}
    service, grid = prepare(
        tmp_path, risk=risk_v2(grid, indices=values),
        policy={"domains": {"ground": {
            "medium_min": 0.3, "high_min": 0.8, "source": "工程确认-测试", "confirmed": True,
        }}},
    )
    profile = evaluate_profile(service)["items"][-1]
    segments = profile["segments"]
    levels = [segment["domains"]["ground"]["classification"]["level"] for segment in segments]
    # idx 0.9,0.9,0.1 with endpoint connectors reusing first/last cell indices.
    assert levels == ["high", "high", "medium", "low"]
    ground = profile["domains"]["ground"]
    high = ground["high_risk"]
    assert high["status"] == "available"
    assert high["interval_count"] == 1
    interval = high["intervals"][0]
    assert interval["start_distance_m"] == 0.0
    assert interval["length_m"] == pytest.approx(
        segments[0]["length_m"] + segments[1]["length_m"], abs=1e-9
    )
    assert interval["end_distance_m"] == pytest.approx(interval["length_m"], abs=1e-9)
    assert interval["max_index"] == 0.9
    assert interval["mean_index"] == pytest.approx(0.9, abs=1e-9)
    assert interval["segment_ids"] == [segments[0]["segment_id"], segments[1]["segment_id"]]
    assert interval["cell_ids"] == sorted({grid[0]["grid_id"], grid[1]["grid_id"]})
    assert ground["length_by_level_m"]["high"] == pytest.approx(interval["length_m"], abs=1e-9)
    assert ground["classification"]["status"] == "passed"
    assert ground["classification"]["level"] == "medium"  # path-length weighted mean index
    assert ground["classification"]["thresholds"] == {"medium_min": 0.3, "high_min": 0.8}
    assert profile["classification"]["status"] == "partial"  # other domains unconfigured
    assert profile["classification"]["domains"]["ground"]["status"] == "passed"
    assert profile["classification"]["domains"]["ground"]["level"] == "medium"
    assert profile["high_risk"]["status"] == "partial"  # other domains unconfigured
    assert profile["high_risk"]["domains"]["ground"]["status"] == "available"
    assert profile["high_risk"]["domains"]["air_traffic"]["status"] == "not_configured"
    # Other domains stay unconfigured: classification is strictly per-domain.
    assert profile["domains"]["air_traffic"]["classification"]["status"] == "not_configured"
    assert profile["domains"]["air_traffic"]["high_risk"]["length_m"] is None


def test_non_adjacent_high_segments_stay_separate_intervals(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.9, 0.1, 0.9))}
    service, grid = prepare(
        tmp_path, risk=risk_v2(grid, indices=values),
        policy={"domains": {"ground": {
            "medium_min": 0.3, "high_min": 0.8, "source": "工程确认-测试", "confirmed": True,
        }}},
    )
    profile = evaluate_profile(service)["items"][-1]
    segments = profile["segments"]
    levels = [segment["domains"]["ground"]["classification"]["level"] for segment in segments]
    assert levels == ["high", "medium", "medium", "high"]
    high = profile["domains"]["ground"]["high_risk"]
    assert high["interval_count"] == 2
    assert high["intervals"][0]["segment_ids"] == [segments[0]["segment_id"]]
    assert high["intervals"][1]["segment_ids"] == [segments[3]["segment_id"]]
    assert high["length_m"] == pytest.approx(
        segments[0]["length_m"] + segments[3]["length_m"], abs=1e-9
    )


@pytest.mark.parametrize("payload, message", [
    ({"domains": {"ground": {"medium_min": 0.9, "high_min": 0.4, "source": "x", "confirmed": True}}},
     "0 <= medium_min <= high_min <= 1"),
    ({"domains": {"ground": {"medium_min": 0.1, "high_min": 1.4, "source": "x", "confirmed": True}}},
     "0 <= medium_min <= high_min <= 1"),
    ({"domains": {"ground": {"medium_min": -0.1, "high_min": 0.5, "source": "x", "confirmed": True}}},
     "0 <= medium_min <= high_min <= 1"),
    ({"domains": {"ground": {"medium_min": 0.4, "source": "x", "confirmed": True}}},
     "必须同时显式提供"),
    ({"domains": {"ground": {"medium_min": 0.4, "high_min": 0.8, "confirmed": True}}},
     "显式 source"),
])
def test_invalid_thresholds_are_rejected(payload, message):
    with pytest.raises(ValueError, match=message):
        normalize_route_risk_profile_policy(payload)


def test_unconfirmed_thresholds_stay_pending_confirmation():
    policy = normalize_route_risk_profile_policy({"domains": {"ground": {
        "medium_min": 0.3, "high_min": 0.8, "source": "工程确认-测试", "confirmed": False,
    }}})
    assert policy["domains"]["ground"]["status"] == "pending_confirmation"
    assert policy["domains"]["ground"]["status_reason"] == "thresholds_not_confirmed"
    assert policy["status"] == "pending_confirmation"
    classification = classify_domain_index(0.95, policy["domains"]["ground"])
    assert classification["status"] == "not_configured"
    assert classification["level"] is None


def test_classify_domain_index_boundaries():
    policy = {
        "domain_id": "ground", "medium_min": 0.3, "high_min": 0.8,
        "status": "confirmed", "confirmed": True, "source": "x",
    }
    assert classify_domain_index(0.29, policy)["level"] == "low"
    assert classify_domain_index(0.3, policy)["level"] == "medium"
    assert classify_domain_index(0.799, policy)["level"] == "medium"
    assert classify_domain_index(0.8, policy)["level"] == "high"
    assert classify_domain_index(1.0, policy)["level"] == "high"


# --------------------------------------------------------------------------------------
# 7. airspace / semantics
# --------------------------------------------------------------------------------------


def test_airspace_is_display_only_and_never_enters_the_profile(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(tmp_path, risk=risk_v2(grid, indices=values))
    first = evaluate_profile(service)["items"][-1]

    service.state["grid_risk_v2"]["airspace"] = {
        "status": "displayed", "features": [{"name": "适飞空域-测试", "area": 12345.0}],
    }
    second = evaluate_profile(service)["items"][-1]
    assert first["fingerprints"]["profile_fingerprint"] == (
        second["fingerprints"]["profile_fingerprint"]
    )
    assert first["domains"] == second["domains"]
    assert second["airspace"]["applicability"] == "display_only"
    assert second["airspace"]["used_in_value_or_fingerprint"] is False
    assert "airspace" not in json.dumps(second["fingerprints"]["components"]).lower()


def test_not_computed_models_and_risk_v2_overall_semantics(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    assert profile["not_computed"]["absolute_risk"]["status"] == "not_computed"
    assert profile["not_computed"]["sora_grc"]["status"] == "not_computed"
    assert profile["not_computed"]["sora_arc"]["status"] == "not_computed"
    assert profile["provenance"]["risk_v2_overall_used"] is False
    assert profile["semantics"]["risk_v2_overall_not_used"] is True
    assert profile["semantics"]["factor_contribution_is_relative_engineering_contribution_not_accident_cause_probability"] is True
    assert profile["semantics"]["classification_is_per_domain_only"] is True
    assert profile["semantics"]["analysis_only_no_replanning"] is True
    assert profile["artifact_type"] == ARTIFACT_TYPE == "layered_route_candidate"


def test_profile_is_json_safe(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    encoded = json.dumps(profile, ensure_ascii=False, allow_nan=False, sort_keys=True)
    assert json.loads(encoded)["profile_id"] == profile["profile_id"]


# --------------------------------------------------------------------------------------
# 8. fingerprints / invalidation
# --------------------------------------------------------------------------------------

def test_profile_fingerprint_is_closed_and_excludes_airspace(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    components = profile["fingerprints"]["components"]
    assert set(components) == {
        "artifact_type", "candidate_id", "candidate_fingerprint", "candidate_input_fingerprint",
        "candidate_risk_fingerprint", "route_id", "altitude_layer_id", "grid_level",
        "path_fingerprint", "grid_path", "grid_risk_v2_input_fingerprint",
        "grid_risk_v2_policy_fingerprint", "grid_risk_v2_cells_fingerprint",
        "profile_policy_fingerprint", "domain_policy_fingerprints",
        "factor_source_fingerprints", "algorithm",
    }
    assert components["algorithm"] == "route_risk_profile_v1@1.0"
    assert components["candidate_fingerprint"] == profile["candidate"]["candidate_fingerprint"]
    assert components["grid_risk_v2_input_fingerprint"] == "riskv2-input-fixture"
    assert components["factor_source_fingerprints"]["population_exposure"][
        "source_fingerprint"
    ] == "fp-population_exposure"


def test_risk_v2_change_stales_only_the_route_risk_profile(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    service.state["result_statuses"].update({
        "routes": "passed", "coverage_3d": "passed", "cns_gap_v2": "passed",
        "grid_risk_v2": "passed", "report": "passed",
    })
    service.state["operational_routes"] = [{"route_id": "LEGACY", "status": "passed"}]
    service.state["coverage_3d"] = {"status": "passed"}
    service.state["cns_gap_analysis_v2"] = {"status": "passed"}
    service.invalidation_service.risk_v2("risk_policy_v2_changed")
    profiles = service.route_risk_profiles()
    assert profiles["items"][-1]["status"] == "stale"
    assert profiles["items"][-1]["profile_id"] == profile["profile_id"]  # audit evidence kept
    assert service.state["result_statuses"]["route_risk_profile"] == "stale"
    for untouched in ("routes", "coverage_3d", "cns_gap_v2", "report"):
        assert service.state["result_statuses"][untouched] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"
    assert service.state["coverage_3d"]["status"] == "passed"
    assert service.state["cns_gap_analysis_v2"]["status"] == "passed"


def test_profile_policy_change_stales_only_the_route_risk_profile(tmp_path):
    service, grid = prepare(tmp_path)
    evaluate_profile(service)
    service.state["result_statuses"]["routes"] = "passed"
    service.state["operational_routes"] = [{"route_id": "LEGACY", "status": "passed"}]
    confirmed_profile_policy(service, medium=0.4, high=0.85)
    profiles = service.route_risk_profiles()
    assert profiles["count"] == 1
    assert profiles["items"][-1]["status"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"


def test_candidate_change_stales_only_the_referencing_profile(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    assert profile["current_applicability"] == "current"
    # A second profile that references a different candidate must stay untouched.
    other = deepcopy(profile)
    other["profile_id"] = "RRP-other-lane"
    other["candidate"] = {**profile["candidate"], "candidate_id": "LRC-other-lane"}
    service.state["route_risk_profiles"]["items"].append(other)
    for item in service.state["layered_route_candidates"]["items"]:
        item["status"] = "stale"
        item["stale_reason"] = "inputs_changed"
    service.route_risk_profile_service.stale_for_candidates(
        [profile["candidate"]["candidate_id"]], "candidate_changed",
    )
    items = {item["profile_id"]: item for item in service.route_risk_profiles()["items"]}
    assert items[profile["profile_id"]]["status"] == "stale"
    assert items[profile["profile_id"]]["stale_reason"] == "candidate_changed"
    assert items["RRP-other-lane"]["status"] == "passed"


def test_candidate_replace_marks_the_old_profile_stale_but_keeps_it(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    # Changing the feasibility policy stales the candidate/product and a new run replaces the
    # candidate record; the old profile is preserved as audit evidence.
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 80.0, "source": "工程确认-测试", "confirmed": True,
    })
    service.evaluate_layered_route_candidate({}, adapter=StubAdapter(stub_facts(grid)))
    profiles = service.route_risk_profiles()
    assert profiles["count"] == 1
    assert profiles["items"][0]["profile_id"] == profile["profile_id"]
    assert profiles["items"][0]["status"] == "stale"
    assert profiles["items"][0]["current_applicability"].startswith("stale")


def test_profile_never_touches_operational_routes_spatial_3d_or_cns(tmp_path):
    service, grid = prepare(tmp_path)
    service.state["operational_routes"] = [{
        "route_id": "LEGACY", "status": "passed", "path": [[122.0, 30.0], [122.01, 30.0]],
    }]
    service.state["cns_gap_analysis"] = {"status": "passed"}
    service.state["cns_gap_analysis_v2"] = {"status": "passed"}
    before = {
        "operational_routes": deepcopy(service.state["operational_routes"]),
        "spatial_3d": deepcopy(service.state["spatial_3d"]),
        "algorithm_selection": deepcopy(service.state["algorithm_selection"]),
        "grid_risk_v2": deepcopy(service.state["grid_risk_v2"]),
        "candidates": deepcopy(service.state["layered_route_candidates"]),
    }
    confirmed_profile_policy(service)
    profile = evaluate_profile(service)["items"][-1]
    assert profile["status"] == "passed"
    assert service.state["operational_routes"] == before["operational_routes"]
    assert service.state["spatial_3d"] == before["spatial_3d"]
    assert service.state["algorithm_selection"] == before["algorithm_selection"]
    assert service.state["grid_risk_v2"] == before["grid_risk_v2"]
    assert service.state["layered_route_candidates"] == before["candidates"]
    assert service.state["spatial_3d"]["route_operating_layers"] == []
    assert service.state["cns_gap_analysis"]["status"] == "passed"
    assert service.state["cns_gap_analysis_v2"]["status"] == "passed"


# --------------------------------------------------------------------------------------
# 9. gates: stale / mismatched candidate
# --------------------------------------------------------------------------------------


def test_stale_candidate_is_rejected_and_nothing_is_saved(tmp_path):
    service, grid = prepare(tmp_path)
    candidate_id = service.layered_route_candidates()["items"][-1]["candidate_id"]
    for item in service.state["layered_route_candidates"]["items"]:
        item["status"] = "stale"
        item["stale_reason"] = "inputs_changed"
    profiles = evaluate_profile(service, {"candidate_id": candidate_id})
    assert profiles["count"] == 0
    assert profiles["last_evaluation"]["status"] == "stale"
    assert profiles["last_evaluation"]["reason_code"] in (
        "candidate_stale", "candidate_stale_inputs_changed",
    )
    assert service.state["result_statuses"]["route_risk_profile"] == "not_calculated"


def test_candidate_distance_mismatch_returns_inconsistent_evidence(tmp_path):
    service, grid = prepare(tmp_path)
    candidate = service.state["layered_route_candidates"]["items"][-1]
    candidate["distance_m"] = round(float(candidate["distance_m"]) + 1.0, 9)
    profiles = evaluate_profile(service)
    assert profiles["count"] == 0
    assert profiles["last_evaluation"]["status"] == "inconsistent_evidence"
    assert profiles["last_evaluation"]["reason_code"] == "profile_candidate_metric_mismatch"
    codes = {item["reason_code"] for item in profiles["last_evaluation"]["blocking_reasons"]}
    assert "route_length_mismatch" in codes
    assert service.state["result_statuses"]["route_risk_profile"] == "not_calculated"


def test_candidate_exposure_mismatch_returns_inconsistent_evidence(tmp_path):
    service, grid = prepare(tmp_path)
    candidate = service.state["layered_route_candidates"]["items"][-1]
    candidate["cost_breakdown"]["domain_exposure_index_m"]["ground"] = 0.0
    profiles = evaluate_profile(service)
    assert profiles["count"] == 0
    assert profiles["last_evaluation"]["status"] == "inconsistent_evidence"
    codes = {item["reason_code"] for item in profiles["last_evaluation"]["blocking_reasons"]}
    assert "active_domain_exposure_mismatch" in codes


def test_candidate_fingerprint_mismatch_is_inconsistent_evidence(tmp_path):
    grid = grid_cells(columns=3, rows=1)
    values = {cell["grid_id"]: value for cell, value in zip(grid, (0.2, 0.5, 0.9))}
    service, grid = prepare(tmp_path, risk=risk_v2(grid, indices=values))
    candidate = service.layered_route_candidates()["items"][-1]
    profiler = RouteRiskProfiler()
    profile = profiler.evaluate(
        candidate={**candidate, "candidate_fingerprint": "tampered"},
        grid=service.state["grid"], grid_risk_v2=service.state["grid_risk_v2"],
        profile_policy=service.route_risk_profile_policy(),
        expected={"candidate_fingerprint": candidate["candidate_fingerprint"]},
    )
    assert profile["status"] == "inconsistent_evidence"
    assert profile["status_reason"] == "candidate_fingerprint_mismatch"


def test_missing_candidate_is_reported_not_ready(tmp_path):
    service = workflow(tmp_path, name="empty.json")
    confirmed_environment(service)
    confirmed_request(service)
    profiles = evaluate_profile(service)
    assert profiles["count"] == 0
    assert profiles["last_evaluation"]["status"] == "not_ready"
    assert profiles["last_evaluation"]["reason_code"] == "candidate_not_available"
    readiness = service.route_risk_profile_readiness()
    assert readiness["status"] == "blocked"
    codes = {item["reason_code"] for item in readiness["blockers"]}
    assert "candidate_not_available" in codes


# --------------------------------------------------------------------------------------
# 10. service lifecycle
# --------------------------------------------------------------------------------------


def test_rerunning_an_unchanged_profile_is_idempotent(tmp_path):
    service, grid = prepare(tmp_path)
    first = evaluate_profile(service)
    second = evaluate_profile(service)
    assert first["count"] == second["count"] == 1
    assert first["items"][0]["profile_id"] == second["items"][0]["profile_id"]
    assert first["items"][0]["fingerprints"]["profile_fingerprint"] == (
        second["items"][0]["fingerprints"]["profile_fingerprint"]
    )


def test_delete_profile_removes_only_that_record(tmp_path):
    service, grid = prepare(tmp_path)
    profile = evaluate_profile(service)["items"][-1]
    service.delete_route_risk_profile(profile["profile_id"])
    assert service.route_risk_profiles()["count"] == 0
    with pytest.raises(ValueError, match="未找到"):
        service.delete_route_risk_profile("missing")


def test_profiles_survive_save_and_restore(tmp_path):
    service, grid = prepare(tmp_path, name="profile.json")
    confirmed_profile_policy(service)
    profile = evaluate_profile(service)["items"][-1]
    service.save()
    restored = WorkflowService(tmp_path / "profile.json", DEFAULTS)
    collection = restored.route_risk_profiles()
    assert collection["count"] == 1
    assert collection["items"][0]["profile_id"] == profile["profile_id"]
    assert collection["items"][0]["current_applicability"] == "current"
    policy = restored.route_risk_profile_policy()
    assert policy["domains"]["ground"]["medium_min"] == 0.3
    assert policy["domains"]["ground"]["high_min"] == 0.8


def test_readiness_reports_configured_and_unconfigured_domains(tmp_path):
    service, grid = prepare(tmp_path)
    readiness = service.route_risk_profile_readiness()
    assert readiness["status"] == "ready"
    assert readiness["artifact_type"] == "layered_route_candidate"
    assert readiness["grid_risk_v2"]["overall_used"] is False
    assert all(
        item["thresholds_status"] == "not_configured"
        for item in readiness["domains"].values()
    )
    confirmed_profile_policy(service, domains=("ground",))
    readiness = service.route_risk_profile_readiness()
    assert readiness["domains"]["ground"]["classification_available"] is True
    assert readiness["domains"]["ground"]["high_risk_metrics"] == "available"
    assert readiness["domains"]["air_traffic"]["high_risk_metrics"] == "not_configured"
    assert readiness["not_computed"]["cross_domain_overall"] == "not_computed"


# --------------------------------------------------------------------------------------
# 11. project state / backfill / API
# --------------------------------------------------------------------------------------


def test_blank_project_ships_unconfirmed_thresholds_and_empty_profiles():
    project = blank_project({})
    policy = project["route_risk_profile_policy"]
    assert policy["status"] == "not_configured"
    assert all(
        policy["domains"][domain_id]["medium_min"] is None for domain_id in DOMAIN_IDS
    )
    assert project["route_risk_profiles"] == default_route_risk_profile_collection()
    assert project["result_statuses"]["route_risk_profile"] == "not_calculated"


def test_legacy_project_backfills_the_profile_containers_as_a_fixed_point():
    project = blank_project({})
    for key in ("route_risk_profile_policy", "route_risk_profiles"):
        project.pop(key)
    project["result_statuses"].pop("route_risk_profile")
    normalized = normalize_project(project, WorkspaceGridService())
    assert normalized["route_risk_profile_policy"] == default_route_risk_profile_policy()
    assert normalized["route_risk_profiles"] == default_route_risk_profile_collection()
    assert normalized["result_statuses"]["route_risk_profile"] == "not_calculated"
    assert normalize_project(deepcopy(normalized), WorkspaceGridService())[
        "route_risk_profiles"
    ] == normalized["route_risk_profiles"]


def test_profile_normalizers_are_idempotent():
    empty = empty_route_risk_profile()
    assert normalize_route_risk_profile(empty) == normalize_route_risk_profile(
        normalize_route_risk_profile(empty)
    )
    collection = default_route_risk_profile_collection()
    assert normalize_route_risk_profile_collection(collection) == (
        normalize_route_risk_profile_collection(
            normalize_route_risk_profile_collection(collection)
        )
    )


def test_full_profile_normalization_is_a_fixed_point(tmp_path):
    service, grid = prepare(tmp_path)
    confirmed_profile_policy(service)
    profile = evaluate_profile(service)["items"][-1]
    once = normalize_route_risk_profile(profile)
    twice = normalize_route_risk_profile(deepcopy(once))
    assert once == twice
    assert once["segments"] == profile["segments"]
    assert once["domains"] == profile["domains"]
    assert once["fingerprints"] == profile["fingerprints"]
    assert once["policy"] == profile["policy"]


def test_policy_fingerprint_changes_with_thresholds_and_confirmation():
    base = default_route_risk_profile_policy()
    confirmed = normalize_route_risk_profile_policy({"domains": {"ground": {
        "medium_min": 0.3, "high_min": 0.8, "source": "工程确认-测试", "confirmed": True,
    }}})
    assert route_risk_profile_policy_fingerprint(base) != (
        route_risk_profile_policy_fingerprint(confirmed)
    )
    other = normalize_route_risk_profile_policy({"domains": {"ground": {
        "medium_min": 0.4, "high_min": 0.8, "source": "工程确认-测试", "confirmed": True,
    }}})
    assert route_risk_profile_policy_fingerprint(confirmed) != (
        route_risk_profile_policy_fingerprint(other)
    )


def test_profiler_reads_domain_indices_with_the_planner_rule(tmp_path):
    """The profiler reads domain indices with the exact rule the planner cost uses."""

    service, grid = prepare(tmp_path)
    # Canonical path per domain: nested ``cell["domains"][domain_id]`` (never flat).
    assert DOMAIN_CELL_CONTAINER_KEY == {
        "ground": "domains.ground", "air_traffic": "domains.air_traffic",
        "environment_obstacle": "domains.environment_obstacle",
    }
    candidate = service.layered_route_candidates()["items"][-1]
    indices, _unresolved = resolve_cell_domain_indices(
        service.state["grid_risk_v2"], candidate["grid_path"], DOMAIN_IDS,
    )
    assert set(indices[candidate["grid_path"][0]]) == set(DOMAIN_IDS)
    assert all(
        value is not None
        for values in indices.values() for value in values.values()
    )


def test_build_route_risk_profile_wrapper_matches_the_profiler(tmp_path):
    service, grid = prepare(tmp_path)
    candidate = service.layered_route_candidates()["items"][-1]
    wrapper = build_route_risk_profile(
        candidate=candidate, grid=service.state["grid"],
        grid_risk_v2=service.state["grid_risk_v2"],
        profile_policy=service.route_risk_profile_policy(),
    )
    profiler = RouteRiskProfiler().evaluate(
        candidate=candidate, grid=service.state["grid"],
        grid_risk_v2=service.state["grid_risk_v2"],
        profile_policy=service.route_risk_profile_policy(),
    )
    assert wrapper["fingerprints"]["profile_fingerprint"] == (
        profiler["fingerprints"]["profile_fingerprint"]
    )
    assert profiler["provenance"]["algorithm"] == f"{ALGORITHM_ID}@1.0"


class ApiWorkflow:
    def route_risk_profile_readiness(self): return {"status": "blocked"}
    def route_risk_profile_policy(self): return {"status": "not_configured"}
    def route_risk_profiles(self): return {"count": 0}
    def set_route_risk_profile_policy(self, payload): return {"policy": payload}
    def evaluate_route_risk_profile(self, payload): return {"payload": payload}
    def delete_route_risk_profile(self, profile_id): return {"deleted": profile_id}

    def __getattr__(self, name):
        return lambda *args, **kwargs: {"unsupported": name}


class ApiContext:
    workflow = ApiWorkflow()
    data = object()


def test_route_risk_profile_api_routes_are_additive_and_forward_the_payload():
    router = ApiRouter(ApiContext())
    assert router.get("/api/route-risk-profile/readiness", {}, {}).data == {"status": "blocked"}
    assert router.get("/api/route-risk-profile-policy", {}, {}).data == {
        "status": "not_configured"
    }
    assert router.get("/api/route-risk-profiles", {}, {}).data == {"count": 0}
    payload = {"domains": {"ground": {"medium_min": 0.3, "high_min": 0.8}}}
    assert router.post("/api/route-risk-profile-policy", payload).data == {"policy": payload}
    assert router.post("/api/route-risk-profiles/evaluate", {"candidate_id": "C1"}).data == {
        "payload": {"candidate_id": "C1"}
    }
    assert router.post("/api/route-risk-profiles/delete", {"profile_id": "P1"}).data == {
        "deleted": "P1"
    }


def test_profile_analysis_never_replans_or_mutates_the_candidate(tmp_path):
    service, grid = prepare(tmp_path)
    before = deepcopy(service.state["layered_route_candidates"])
    confirmed_profile_policy(service)
    evaluate_profile(service)
    assert service.state["layered_route_candidates"] == before
    profile = service.route_risk_profiles()["items"][-1]
    assert profile["provenance"]["replanning"] is False
    assert profile["provenance"]["candidate_mutated"] is False
    assert profile["provenance"]["writes_operational_routes_or_cns"] is False
    assert profile["semantics"]["does_not_modify_candidate"] is True
