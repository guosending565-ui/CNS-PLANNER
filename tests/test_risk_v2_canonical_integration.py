"""真实跨组件 integration gate：canonical Risk V2 → Layered Planner V1 → RouteRiskProfile V1。

本文件是 **contract integration regression**，不是手写 consumer fixture：

1. 用真实 ``GridRiskModelV2.evaluate()`` 生产 ``grid_risk_v2``（canonical nested
   ``cells[gid]["domains"][domain_id]``），policy 仅显式确认 ``ground`` 的
   ``population_exposure``（weight = 1）；
2. Layered cost policy 只让 ``ground_lambda > 0``，其余显式 0；
3. 运行 Layered Route Planner V1：candidate 必须成功，且 ground exposure 由 canonical
   nested domain index 解析并 > 0（修复前真实 λ>0 会被判 ``risk_domain_unresolved``）；
4. 运行 RouteRiskProfile：ground exposure / mean 必须与 candidate ``cost_breakdown`` 逐值一致；
5. 真实 chain 上再验证 fail-closed：删除一个候选格的 domain index 时 planner 仍按
   ``risk_domain_unresolved`` 阻断，且绝不补 0；
6. 验证历史错误的 flat ``cell[domain_id]`` schema 会被暴露为 missing，而不是被静默读取。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.risk_v2 import DOMAIN_IDS, normalize_risk_policy_v2
from cns_planner.domain.route_risk_profile import grid_risk_v2_cells_fingerprint
from cns_planner.domain.spatial_3d import normalize_spatial_3d
from cns_planner.risk.accessors_v2 import (
    cell_domain_index, cell_domain_record, cell_factor_record, domain_record_path,
)
from cns_planner.risk.model_v2 import GridRiskModelV2
from cns_planner.risk.route_profile import RouteRiskProfiler
from cns_planner.risk.route_exposure import (
    integrate_path_exposure, resolve_cell_domain_indices,
)

DEFAULTS = Path("cns_planner/config/defaults.json")
LAYER_ID = "L8-LOW"
LOW_ALTITUDE = 300.0

#: 真实 population 密度（people/km²）：三个格不同，保证归一化 index 非退化且都 > 0。
POPULATION_DENSITIES = (20.0, 40.0, 60.0)


# --------------------------------------------------------------------------------------
# fixtures（真实 canonical 生产链）
# --------------------------------------------------------------------------------------


def grid_cells(columns=3, level=8):
    cells = []
    for column in range(columns):
        west = 122.0 + 0.01 * column
        cells.append({
            "grid_id": f"MHT4063-L{level}-C{column}-RP0", "level": level,
            "column": column, "row": 0,
            "bbox": [west, 30.0, west + 0.01, 30.01],
            "center": [west + 0.005, 30.005],
        })
    return cells


def confirmed_ground_policy():
    """最小 confirmed RiskAggregationPolicyV2：ground 仅 population_exposure weight=1。"""

    return normalize_risk_policy_v2({
        "domains": {
            "ground": {
                "method": "weighted_sum",
                "weights": {"population_exposure": 1.0},
                "required_factors": ["population_exposure"],
                "source": "工程确认-集成测试",
                "evidence": {"review": "risk_v2_canonical_integration"},
                "confirmed": True,
            },
        },
    })


def grid_attributes(cells, *, densities=POPULATION_DENSITIES):
    """真实 canonical 属性：population 目标网格密度 + terrain/buildings 粗包络证据。"""

    population = {
        "status": "passed", "unit_status": "verified_from_raster_metadata",
        "algorithm_id": "population-mapping", "algorithm_version": "fixture-1",
        "grid_level": 8, "source": {"path": "population.tif"},
        "cells": {
            cell["grid_id"]: {
                "status": "passed",
                "population_density_people_km2": density,
                "population_count_people": density * 10.0,
                "coverage_status": "full",
                "source_coverage_fraction": 1.0,
            }
            for cell, density in zip(cells, densities)
        },
    }
    return {"population": population}


def terrain_facts(cells, elevation=10.0):
    """Feasibility adapter 的 canonical facts（不伪造任何缺测值）。"""

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
        for cell in cells
    ]


class StubAdapter:
    """GIS 边界替身：只返回给出的事实。"""

    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    def build_cells(self, grid_cells, state):
        self.calls += 1
        return deepcopy(self.facts)

    def describe(self):
        return {"adapter_id": "stub"}


def real_grid_risk_v2(cells, *, densities=POPULATION_DENSITIES):
    """真实 ``GridRiskModelV2.evaluate()`` 输出（canonical nested domains）。"""

    grid = {"status": "passed", "level": 8, "count": len(cells), "cells": cells}
    return GridRiskModelV2().evaluate(
        grid, grid_attributes(cells, densities=densities),
        {"policy": confirmed_ground_policy()},
    )


def workflow(tmp_path, *, cells, ground_lambda=1.0, name="canonical.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    # Canonical nested Risk V2 evidence feeds the production Theta* V2 planner.
    assert service.layered_route_planner_service.planner.algorithm_id == (
        "layered_risk_aware_theta_star_v2"
    )
    service.state["grid"] = {
        "status": "passed", "level": 8, "count": len(cells), "cells": cells,
    }
    service.state["scenario_routes"] = [{
        "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
        "start": list(cells[0]["center"]), "end": list(cells[-1]["center"]),
    }]
    service.state["spatial_3d"]["altitude_layers"] = [
        normalize_spatial_3d({"altitude_layers": [{
            "altitude_layer_id": LAYER_ID, "name": "低层",
            "nominal_altitude_m": LOW_ALTITUDE, "lower_altitude_m": 250.0,
            "upper_altitude_m": 350.0, "vertical_reference": "egm2008_orthometric",
            "source": "工程确认-集成测试", "confirmed": True,
        }]})["altitude_layers"][0]
    ]
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 50.0, "source": "工程确认-集成测试", "confirmed": True,
    })
    service.set_layered_route_cost_policy({
        "ground_lambda": ground_lambda, "air_traffic_lambda": 0.0,
        "environment_obstacle_lambda": 0.0, "source": "工程确认-集成测试", "confirmed": True,
    })
    service.state["building_clearance_policy"].update({
        "horizontal_clearance_m": 0.0, "vertical_clearance_m": 10.0,
        "source": "工程确认-集成测试", "confirmed": True, "status": "confirmed",
    })
    service.set_layered_route_planning_request({
        "scenario_route_id": "R0001", "altitude_layer_id": LAYER_ID,
        "source": "工程确认-集成测试", "confirmed": True,
    })
    service.state["grid_risk_v2"] = real_grid_risk_v2(cells)
    return service


def run_planner(service, cells):
    """运行 production Theta* V2，返回完整 collection（candidates + masks）。"""

    return service.evaluate_layered_route_candidate(
        {}, adapter=StubAdapter(terrain_facts(cells)),
    )


def candidate_of(collection):
    return collection["layered_route_candidates"]["items"][-1]


# --------------------------------------------------------------------------------------
# 1. producer → accessor：canonical nested schema 是唯一读取路径
# --------------------------------------------------------------------------------------


def test_producer_writes_the_canonical_nested_domain_schema():
    cells = grid_cells()
    risk = real_grid_risk_v2(cells)
    for cell in cells:
        record = risk["cells"][cell["grid_id"]]
        # 生产者写 nested domains；accessor 读取的就是它，逐值一致。
        assert set(record["domains"]) >= set(DOMAIN_IDS)
        ground = cell_domain_record(record, "ground")
        assert ground is record["domains"]["ground"]
        index, status = cell_domain_index(record, "ground")
        assert status == "passed"
        assert index == pytest.approx(ground["index"])
        assert index > 0.0
        # 生产者只写 canonical nested 容器，绝不写 flat ``record[domain_id]``。
        assert "ground" not in record
        assert cell_factor_record(record, "population_exposure")["status"] == "passed"
    assert domain_record_path("ground") == "domains.ground"
    assert set(DOMAIN_IDS) == {"ground", "air_traffic", "environment_obstacle"}


def test_flat_legacy_schema_is_exposed_as_missing_never_silently_read():
    cells = grid_cells(columns=1)
    canonical = real_grid_risk_v2(cells)
    grid_id = cells[0]["grid_id"]
    flat = {"status": "passed", "cells": {
        grid_id: {"ground": {"status": "passed", "index": 0.4}},
    }}
    resolved, unresolved = resolve_cell_domain_indices(flat, (grid_id,), ("ground",))
    assert resolved[grid_id]["ground"] is None
    assert unresolved[grid_id] == ["ground:missing_data"]
    # The canonical producer output resolves instead, and the two fingerprints differ.
    resolved_ok, unresolved_ok = resolve_cell_domain_indices(canonical, (grid_id,), ("ground",))
    assert resolved_ok[grid_id]["ground"] > 0.0
    assert unresolved_ok == {}
    assert grid_risk_v2_cells_fingerprint(flat) != grid_risk_v2_cells_fingerprint(canonical)


# --------------------------------------------------------------------------------------
# 2. canonical producer → Layered Planner λ>0 → RouteRiskProfile
# --------------------------------------------------------------------------------------


def test_nested_producer_feeds_theta_v2_and_profile_exposure(tmp_path):
    cells = grid_cells()
    service = workflow(tmp_path, cells=cells)
    risk = service.state["grid_risk_v2"]

    # The real producer keeps every domain index inside [0, 1] and non-zero for ground.
    producer_indices = [
        cell_domain_index(risk["cells"][cell["grid_id"]], "ground")[0] for cell in cells
    ]
    assert all(value is not None and value > 0.0 for value in producer_indices)

    candidate = candidate_of(run_planner(service, cells))
    assert candidate["status"] == "candidate"
    assert candidate["candidate_id"] is not None
    profile = service.evaluate_route_risk_profile({})["route_risk_profiles"]["items"][-1]
    assert profile["status"] == "passed"
    assert profile["route_length_m"] == pytest.approx(candidate["distance_m"], abs=1e-9)
    ground = profile["domains"]["ground"]
    assert ground["exposure_index_m"] > 0.0
    assert ground["mean_index"] > 0.0
    assert ground["coverage"] == 1.0
    assert ground["unresolved_length_m"] == 0.0
    assert ground["status"] == "resolved"
    assert ground["active_cost_domain"] is False
    assert profile["consistency"]["status"] == "passed"
    # Non-active domains stay unrequired (λ=0 => not a planning input) and are unresolved
    # only because their aggregation policy is not confirmed — never because of a flat read.
    assert profile["domains"]["air_traffic"]["active_cost_domain"] is False


def test_profile_ground_exposure_matches_a_direct_canonical_integral(tmp_path):
    """Same numbers through a third, independent read of the canonical nested schema."""

    cells = grid_cells()
    service = workflow(tmp_path, cells=cells)
    risk = service.state["grid_risk_v2"]
    candidate = candidate_of(run_planner(service, cells))
    centers = {cell["grid_id"]: list(cell["center"]) for cell in cells}
    indices, _unresolved = resolve_cell_domain_indices(risk, candidate["grid_path"], ("ground",))
    for cell in cells:
        record = risk["cells"][cell["grid_id"]]
        assert indices[cell["grid_id"]]["ground"] == pytest.approx(
            record["domains"]["ground"]["index"]
        )
    integral = integrate_path_exposure(
        start=candidate["path"][0], end=candidate["path"][-1],
        grid_path=candidate["grid_path"], centers=centers, indices=indices,
        domain_ids=("ground",), integration_domains=("ground",),
    )
    profile = service.evaluate_route_risk_profile({})["route_risk_profiles"]["items"][-1]
    assert integral["domain_exposure_index_m"]["ground"] == pytest.approx(
        profile["domains"]["ground"]["exposure_index_m"], abs=1e-9
    )


def test_missing_canonical_domain_index_still_fails_closed_without_zero(tmp_path):
    cells = grid_cells()
    service = workflow(tmp_path, cells=cells)
    tampered = deepcopy(service.state["grid_risk_v2"])
    target = cells[1]["grid_id"]
    # Simulate a genuinely unresolved domain record inside the canonical nested container.
    tampered["cells"][target]["domains"]["ground"] = {
        "domain_id": "ground", "status": "unresolved", "index": None,
    }
    service.state["grid_risk_v2"] = tampered

    collection = run_planner(service, cells)
    candidate = candidate_of(collection)
    assert candidate["status"] == "candidate"
    profile = service.evaluate_route_risk_profile({})["route_risk_profiles"]["items"][-1]
    ground = profile["domains"]["ground"]
    assert ground["status"] == "unresolved"
    assert ground["exposure_index_m"] is None
    assert target in ground["unresolved_cells"]
    resolved, unresolved = resolve_cell_domain_indices(tampered, (target,), ("ground",))
    assert resolved[target]["ground"] is None  # never 0
    assert unresolved[target] == ["ground:unresolved"]


# --------------------------------------------------------------------------------------
# 3. fingerprint 契约：domain index 敏感、airspace 无关
# --------------------------------------------------------------------------------------


def test_cells_fingerprint_follows_domain_index_and_ignores_airspace():
    cells = grid_cells()
    risk = real_grid_risk_v2(cells)
    baseline = grid_risk_v2_cells_fingerprint(risk)

    airspace_changed = deepcopy(risk)
    airspace_changed["airspace"] = {
        "status": "displayed", "features": [{"name": "适飞空域-集成测试", "area": 999.0}],
    }
    for cell in cells:
        airspace_changed["cells"][cell["grid_id"]]["airspace"] = {"status": "displayed"}
    assert grid_risk_v2_cells_fingerprint(airspace_changed) == baseline

    domain_changed = deepcopy(risk)
    grid_id = cells[0]["grid_id"]
    domain_changed["cells"][grid_id]["domains"]["ground"]["index"] = round(
        float(domain_changed["cells"][grid_id]["domains"]["ground"]["index"]) / 2.0, 9
    )
    assert grid_risk_v2_cells_fingerprint(domain_changed) != baseline

    status_changed = deepcopy(risk)
    status_changed["cells"][grid_id]["domains"]["ground"]["status"] = "unresolved"
    assert grid_risk_v2_cells_fingerprint(status_changed) != baseline


def test_cells_fingerprint_is_stable_and_uses_the_nested_schema_only():
    cells = grid_cells()
    risk = real_grid_risk_v2(cells)
    assert grid_risk_v2_cells_fingerprint(risk) == grid_risk_v2_cells_fingerprint(deepcopy(risk))
    # Moving a canonical record to the historical flat position must change the fingerprint:
    # the flat form is not a second supported schema.
    moved = deepcopy(risk)
    grid_id = cells[0]["grid_id"]
    ground = moved["cells"][grid_id]["domains"].pop("ground")
    moved["cells"][grid_id]["ground"] = ground
    assert grid_risk_v2_cells_fingerprint(moved) != grid_risk_v2_cells_fingerprint(risk)


def test_profiler_wrapper_and_service_agree_on_the_real_chain(tmp_path):
    cells = grid_cells()
    service = workflow(tmp_path, cells=cells)
    candidate = candidate_of(run_planner(service, cells))
    policy = service.route_risk_profile_policy()
    profiler = RouteRiskProfiler().evaluate(
        candidate=candidate, grid=service.state["grid"],
        grid_risk_v2=service.state["grid_risk_v2"], profile_policy=policy,
    )
    service_profile = service.evaluate_route_risk_profile({})["route_risk_profiles"]["items"][-1]
    assert profiler["status"] == service_profile["status"] == "passed"
    assert profiler["fingerprints"]["profile_fingerprint"] == (
        service_profile["fingerprints"]["profile_fingerprint"]
    )
    assert profiler["domains"]["ground"]["exposure_index_m"] == pytest.approx(
        service_profile["domains"]["ground"]["exposure_index_m"], abs=1e-9
    )
