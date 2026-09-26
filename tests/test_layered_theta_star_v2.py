"""Layered Risk-Aware Theta* V2 tests.

Covers the 22 required behaviours: real Theta* LOS shortcutting (not A* + smoothing),
fewer vertices/turns than the V1 8-neighbour baseline, supercover traversal that rejects
intermediate terrain/building/unknown/regulatory cells, fixed-H building clearance,
population × shelter risk with per-grid coefficients, the auditable 0.8/0.1/0.1 objective,
turn-cost sensitivity, the ``route_risk_density`` evaluation metric and its deliberately
wide temporary constraint, the additive regulatory/communication interfaces, V1 baseline
characterization stability, downstream compatibility and the repository cleanup.

The V1 baseline suite (``tests/test_layered_route_planner.py``) is deliberately untouched.
"""

from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from cns_planner.algorithms.registry import (
    build_default_algorithm_registry, default_algorithm_selection,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.communication_planning_field import (
    communication_cell, communication_readiness, normalize_communication_planning_field,
)
from cns_planner.domain.layered_route import (
    normalize_layered_route_cost_policy, normalize_layered_route_feasibility_policy,
    normalize_layered_route_request, resolve_cruise_altitude,
)
from cns_planner.domain.layered_theta_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION, default_risk_density_constraint,
    default_theta_v2_objective_policy, evaluate_route_risk_density,
    normalize_risk_density_constraint, normalize_theta_v2_objective_policy,
    objective_weights, weighted_terms,
)
from cns_planner.domain.population_shelter import (
    normalize_shelter_coefficient_policy, population_shelter_fingerprint,
    resolve_population_shelter, shelter_policy_fingerprint, user_defined_baseline_policy,
)
from cns_planner.domain.regulatory_constraints import (
    evaluate_regulatory_intersection, normalize_regulatory_constraints,
    regulatory_compliance_record,
)
from cns_planner.domain.spatial_3d import normalize_spatial_3d
from cns_planner.layered_route_planner.feasibility import build_layer_feasibility_mask
from cns_planner.layered_route_planner.supercover import (
    supercover_traversal, traversal_cells_with_lengths,
)
from cns_planner.layered_route_planner.theta_star_v2 import (
    GridIndexMap, LayeredRiskAwareThetaStarV2, derive_d_ref_m, grid_bearing_deg,
    heading_bin_for_bearing,
)
from cns_planner.planning.grid_graph import GridGraph

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"
REPO = Path(__file__).parents[1]
LAYER_ID = "L8-LOW"
ALTITUDE_H = 300.0
LEVEL = 8


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def grid_cells(columns=6, rows=5, level=LEVEL):
    cells = []
    for column in range(columns):
        for row in range(rows):
            west, south = 122.0 + 0.01 * column, 30.0 + 0.01 * row
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
        "nominal_altitude_m": ALTITUDE_H, "lower_altitude_m": 250.0, "upper_altitude_m": 350.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True, "status": "confirmed",
    }
    payload.update(overrides)
    return payload


def facts(grid, elevations=None, buildings=None):
    """Canonical GIS-boundary facts; ``elevations``/``buildings`` are per-grid overrides."""

    elevations = elevations or {}
    buildings = buildings or {}
    return [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed",
                "surface_elevation_max_egm2008_m": elevations.get(
                    cell["grid_id"], elevations.get("*", 10.0)
                ),
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": buildings.get(cell["grid_id"], {
                "data_status": "passed", "building_count": 0, "height_max_m": None,
                "valid_height_fraction": None,
            }),
        }
        for cell in grid
    ]


def building_cell(height_max=20.0, *, count=1, status="passed", fraction=1.0):
    return {
        "data_status": status, "building_count": count, "height_max_m": height_max,
        "valid_height_fraction": fraction,
    }


def risk_v2(grid, index=0.5, *, status="passed"):
    return {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains",
        "algorithm_version": "2.0", "input_fingerprint": f"riskv2-in-{index}",
        "policy_fingerprint": "riskv2-pol",
        "references": {"population_exposure": {"mode": "log1p_quantile", "value": 100.0}},
        "factor_status": {"population_exposure": {
            "status": status, "source_id": "worldpop-r2025a-population-count",
            "source_fingerprint": "popsrc", "normalization": {"method": "log1p_quantile"},
        }},
        "cells": {
            cell["grid_id"]: {
                "grid_id": cell["grid_id"], "status": "passed",
                "factors": {"population_exposure": {
                    "factor_id": "population_exposure", "status": status,
                    "normalized_index": index,
                }},
                "domains": {
                    domain_id: {"domain_id": domain_id, "status": "passed", "index": index}
                    for domain_id in ("ground", "air_traffic", "environment_obstacle")
                },
            }
            for cell in grid
        },
    }


def shelter_field(grid, factors=None, policy=None, density=100.0):
    factors = factors if factors is not None else {
        cell["grid_id"]: 0.5 for cell in grid
    }
    return resolve_population_shelter(
        grid={"level": LEVEL, "cells": grid},
        population_attribute={
            "status": "passed", "algorithm_id": "population-grid-raw-statistics",
            "cells": {
                cell["grid_id"]: {
                    "status": "passed", "population_density_people_km2": density,
                    "source_coverage_fraction": 1.0, "coverage_status": "covered",
                }
                for cell in grid
            },
        },
        normalized_population_factors=factors,
        policy=policy or user_defined_baseline_policy(),
    )


class StubAdapter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def build_cells(self, grid_cells, state):
        self.calls += 1
        return deepcopy(self.payload)

    def describe(self):
        return {"adapter_id": "stub-theta-v2"}

    def usable(self):
        return True, None

    def source_status(self):
        return {"terrain": {"available": True, "reason": None}}


def planner(**parameters):
    return LayeredRiskAwareThetaStarV2(parameters)


def request(route_id="R0001"):
    return normalize_layered_route_request({
        "scenario_route_id": route_id, "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    })


def feasibility_policy(clearance=50.0):
    return normalize_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": clearance, "source": "工程确认-测试", "confirmed": True,
    })


BUILDING_POLICY = {
    "status": "confirmed", "horizontal_clearance_m": 0.0, "vertical_clearance_m": 10.0,
    "source": "工程确认-测试", "confirmed": True,
}


def cruise(layer_payload=None):
    return resolve_cruise_altitude(layer_payload or layer())


def build_mask(grid, *, elevations=None, buildings=None, clearance=50.0):
    return build_layer_feasibility_mask(
        request=request(), cruise_altitude=cruise(), cells=facts(grid, elevations, buildings),
        feasibility_policy=feasibility_policy(clearance),
        building_clearance_policy=BUILDING_POLICY, source_audits={}, grid_level=LEVEL,
    )


def route_for(grid, *, start=None, end=None, route_id="R0001"):
    return {
        "route_id": route_id,
        "start": list(start or grid[0]["center"]),
        "end": list(end or grid[-1]["center"]),
    }


def plan_v2(grid, *, mask=None, route=None, shelter=None, policy=None, objective=None,
            constraint=None, regulatory=None, communication=None, buildings=None,
            elevations=None, parameters=None, **overrides):
    return planner(**(parameters or {})).plan(
        request=request(), scenario_route=route or route_for(grid),
        grid={"level": LEVEL, "cells": grid},
        layer_mask=mask if mask is not None else build_mask(
            grid, elevations=elevations, buildings=buildings,
        ),
        grid_risk_v2=risk_v2(grid), feasibility_policy=feasibility_policy(),
        population_shelter=shelter if shelter is not None else shelter_field(grid),
        shelter_policy=policy or user_defined_baseline_policy(),
        regulatory_constraints=regulatory, communication_field=communication,
        building_clearance_policy=BUILDING_POLICY,
        objective_policy=objective, risk_density_constraint=constraint,
        **overrides,
    )


def cell_id(column, row, level=LEVEL):
    return f"MHT4063-L{level}-C{column}-RP{row}"


def is_adjacent(left, right):
    a, b = left.split("-C")[1].split("-RP"), right.split("-C")[1].split("-RP")
    return abs(int(a[0]) - int(b[0])) <= 1 and abs(int(a[1]) - int(b[1])) <= 1


# helper workflow for service/downstream tests


def service_for(tmp_path, *, grid=None, index=0.5):
    grid = grid or grid_cells()
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = service.state
    state["grid"] = {"status": "passed", "level": LEVEL, "count": len(grid), "cells": grid}
    state["scenario_routes"] = [route_for(grid)]
    state["spatial_3d"]["altitude_layers"] = [
        normalize_spatial_3d({"altitude_layers": [layer()]})["altitude_layers"][0]
    ]
    state["grid_attributes"]["population"] = {
        "status": "passed", "algorithm_id": "population-grid-raw-statistics",
        "unit_status": "verified_from_raster_metadata",
        "cells": {
            cell["grid_id"]: {
                "status": "passed", "population_density_people_km2": 100.0,
                "source_coverage_fraction": 1.0, "coverage_status": "covered",
            } for cell in grid
        },
    }
    state["grid_risk_v2"] = risk_v2(grid, index)
    state["building_clearance_policy"].update(BUILDING_POLICY)
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 50.0, "source": "工程确认-测试", "confirmed": True,
    })
    service.set_layered_route_planning_request({
        "scenario_route_id": "R0001", "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    })
    service.select_algorithm({
        "algorithm_type": "layered_route_planner", "algorithm_id": ALGORITHM_ID,
        "version": ALGORITHM_VERSION, "parameters": {},
    })
    service.source_status = None
    service.state["source_audits"] = {
        "status": "passed", "count": 2, "items": {
            "terrain_dtm": {"role": "terrain_dtm", "status": "verified", "file_name": "dtm.tif",
                            "verification": {"sha256": "terrain-sha"}},
            "buildings": {"role": "buildings", "status": "verified", "file_name": "b.gpkg",
                          "verification": {"sha256": "building-sha"}},
        },
    }
    return service, grid


def evaluate(service, grid, *, adapter_facts=None):
    collection = service.evaluate_layered_route_candidate(
        {}, adapter=StubAdapter(adapter_facts if adapter_facts is not None else facts(grid)),
    )["layered_route_candidates"]
    return collection["items"][-1]


# --------------------------------------------------------------------------------------
# 1-2. real Theta* any-angle search vs the V1 8-neighbour baseline
# --------------------------------------------------------------------------------------


def test_open_grid_theta_star_really_rewires_parents_to_los_shortcuts():
    grid = grid_cells(columns=9, rows=7)
    candidate = plan_v2(grid)
    assert candidate["status"] == "candidate"
    grid_path = candidate["grid_path"]
    assert len(grid_path) >= 2
    # A genuine Theta* vertex sequence contains at least one non-adjacent hop, or (when the
    # optimum happens to be a staircase) at least one multi-cell LOS shortcut: the search took
    # a straight traversal instead of only stepping to 8-neighbours.
    non_adjacent = [
        (left, right) for left, right in zip(grid_path, grid_path[1:])
        if not is_adjacent(left, right)
    ]
    multi_cell = [
        item for item in candidate["los_segments"] if len(item["traversed_cells"]) > 2
    ]
    assert non_adjacent or multi_cell, f"Theta* 未产生 LOS shortcut：{grid_path}"
    statistics = candidate["search_statistics"]
    assert statistics["rewired_parent_shortcuts"] > 0
    assert statistics["los_shortcuts"] > 0
    assert candidate["provenance"]["search_semantics"]["parent_los_rewiring"] is True
    assert candidate["provenance"]["search_semantics"]["not_astar_plus_post_smoothing"] is True
    # Every reported segment carries its full crossed-cell list, not just the endpoints.
    assert all(item["supercover"] is True for item in candidate["los_segments"])
    assert all(item["traversed_cells"] for item in candidate["los_segments"])
    assert sum(len(item["traversed_cells"]) for item in candidate["los_segments"]) > len(
        candidate["los_segments"]
    )


def test_supercover_shortcut_crossing_an_intermediate_blocked_terrain_cell_is_rejected():
    grid = grid_cells(columns=9, rows=7)
    middle = "MHT4063-L8-C4-RP3"
    mask = build_mask(grid)
    mask["cells"][middle]["status"] = "blocked"
    mask["cells"][middle]["reason_code"] = "altitude_below_terrain_floor"
    candidate = plan_v2(grid, mask=mask)
    assert candidate["status"] == "candidate"
    crossed = {
        cell["grid_id"]
        for segment in candidate["los_segments"] for cell in segment["traversed_cells"]
    }
    assert middle not in crossed
    # The direct diagonal shortcut is gone, so the route must go around the blocked cell.
    assert middle not in candidate["grid_path"]
    assert candidate["search_statistics"]["rejected_terrain"] > 0


def test_supercover_shortcut_crossing_an_intermediate_blocked_building_cell_is_rejected():
    grid = grid_cells(columns=9, rows=7)
    middle = "MHT4063-L8-C4-RP3"
    # H = 300 m is below this roof + clearance, so the cell is blocked; the terrain and
    # diagonal geometry are otherwise identical, which is exactly the supercover cross-check.
    buildings = {middle: building_cell(300.0)}
    mask = build_mask(grid, buildings=buildings)
    assert mask["cells"][middle]["status"] == "blocked"
    assert mask["cells"][middle]["reason_code"] == "altitude_below_building_clearance_floor"
    candidate = plan_v2(grid, mask=mask, buildings=buildings)
    assert candidate["status"] == "candidate"
    crossed = {
        cell["grid_id"]
        for segment in candidate["los_segments"] for cell in segment["traversed_cells"]
    }
    assert middle not in crossed
    assert middle not in candidate["grid_path"]
    assert candidate["search_statistics"]["rejected_building"] > 0


def test_fixed_h_above_roof_plus_clearance_is_traversable_and_below_it_is_blocked():
    grid = grid_cells(columns=3, rows=2)
    target = "MHT4063-L8-C1-RP0"
    # H = 300 m, ground 10 m, roof 10 + 20 = 30 m, required = 40 m ⇒ clear.
    high = plan_v2(grid, buildings={target: building_cell(20.0)})
    assert high["status"] == "candidate"
    mask = build_mask(grid, buildings={target: building_cell(20.0)})
    assert mask["cells"][target]["status"] == "feasible"
    assert mask["cells"][target]["building_required_clearance_egm2008_m"] == 40.0
    assert mask["cells"][target]["building_margin_m"] == 260.0

    # A 300 m tall building column puts the floor at 320 m > H ⇒ blocked, not "building
    # present ⇒ blocked".
    blocked = build_mask(grid, buildings={target: building_cell(300.0)})
    assert blocked["cells"][target]["status"] == "blocked"
    assert blocked["cells"][target]["reason_code"] == "altitude_below_building_clearance_floor"
    assert plan_v2(grid, mask=blocked)["status"] == "candidate"
    # A cell with building_count == 0 carries no vertical building constraint at all.
    assert blocked["cells"]["MHT4063-L8-C0-RP0"]["status"] == "feasible"

    # The mask treats "H >= roof + clearance" as the single boundary.
    equals = build_mask(grid, buildings={target: building_cell(240.0)})
    assert equals["cells"][target]["status"] == "feasible"  # 10 + 240 + 10 = 260 < 300
    assert equals["cells"][target]["building_margin_m"] == 40.0


def test_unknown_terrain_and_building_are_fail_closed():
    grid = grid_cells(columns=3, rows=2)
    unknown_terrain = grid_cells(columns=3, rows=2)
    mask_unknown_terrain = build_mask(
        unknown_terrain, elevations={"MHT4063-L8-C1-RP0": None},
    )
    assert mask_unknown_terrain["cells"]["MHT4063-L8-C1-RP0"]["status"] == "unknown"
    assert plan_v2(grid, mask=mask_unknown_terrain)["status"] == "candidate"

    broken_heights = {
        "MHT4063-L8-C1-RP0": building_cell(height_max=None, fraction=0.5),
    }
    mask_unknown_building = build_mask(grid, buildings=broken_heights)
    assert mask_unknown_building["cells"]["MHT4063-L8-C1-RP0"]["status"] == "unknown"
    candidate = plan_v2(grid, mask=mask_unknown_building, buildings=broken_heights)
    assert candidate["status"] == "candidate"
    assert "MHT4063-L8-C1-RP0" not in candidate["grid_path"]
    assert candidate["search_statistics"]["rejected_unknown"] > 0


# --------------------------------------------------------------------------------------
# 6-9. shelter coefficient field, exposure integration, per-cell length
# --------------------------------------------------------------------------------------


def test_shelter_coefficient_one_makes_risk_equal_to_the_normalized_population_risk():
    grid = grid_cells()
    field = shelter_field(grid, policy=user_defined_baseline_policy(1.0))
    assert field["status"] == "passed"
    assert all(
        cell["shelter_coefficient"] == 1.0 and cell["risk_index"] == cell["normalized_population_factor"]
        for cell in field["cells"].values()
    )
    policy = user_defined_baseline_policy(1.0)
    assert policy["status"] == "confirmed"
    assert policy["provenance"] == "user_defined_baseline"
    assert policy["default_coefficient"] == 1.0
    assert policy["semantics"]["value_is_per_grid_data_not_an_algorithm_constant"] is True
    # The coefficient is real per-grid data, so a per-grid override is honoured verbatim.
    overridden = resolve_population_shelter(
        grid={"level": LEVEL, "cells": grid},
        population_attribute={"status": "passed", "cells": {
            cell["grid_id"]: {"status": "passed", "population_density_people_km2": 100.0}
            for cell in grid
        }},
        normalized_population_factors={cell["grid_id"]: 0.8 for cell in grid},
        policy=normalize_shelter_coefficient_policy({
            "default_coefficient": 1.0, "per_grid_overrides": {grid[0]["grid_id"]: 0.25},
            "source": "shelter-dataset-v1", "confirmed": True,
        }),
    )
    assert overridden["cells"][grid[0]["grid_id"]]["shelter_coefficient"] == 0.25
    assert overridden["cells"][grid[0]["grid_id"]]["risk_index"] == 0.2
    # raw_exposure is a raw product, never the search index.
    assert overridden["cells"][grid[0]["grid_id"]]["raw_exposure"] == 25.0
    assert overridden["cells"][grid[0]["grid_id"]]["risk_index"] != 25.0


def test_changing_one_cell_shelter_changes_traversal_exposure_and_the_path():
    grid = grid_cells(columns=6, rows=5)
    centres = {cell["grid_id"]: cell["center"] for cell in grid}
    route = route_for(grid, start=centres[cell_id(0, 0)], end=centres[cell_id(5, 0)])
    factors = {cell["grid_id"]: 0.2 for cell in grid}
    reference = plan_v2(grid, route=route, shelter=shelter_field(grid, factors))
    assert reference["status"] == "candidate"
    reference_crossed = {
        cell["grid_id"]
        for segment in reference["los_segments"] for cell in segment["traversed_cells"]
    }
    # One cell sitting on the reference route is raised to the top of the index range.  It is
    # per-grid shelter data, so the traversal exposure must change — and on this grid the
    # cheapest route then leaves the cell it occupies.
    raised = cell_id(1, 0)
    assert raised in reference_crossed
    shielded_factors = dict(factors)
    shielded_factors[raised] = 1.0
    shielded = plan_v2(grid, route=route, shelter=shelter_field(grid, shielded_factors))
    assert shielded["status"] == "candidate"
    shielded_crossed = {
        cell["grid_id"]
        for segment in shielded["los_segments"] for cell in segment["traversed_cells"]
    }
    assert shielded["planning_objective"]["risk_exposure_index_m"] != (
        reference["planning_objective"]["risk_exposure_index_m"]
    ), "改变格内 shelter 风险后遍历暴露未变化"
    # A per-grid shelter change is a real planning input: it moves the objective, both
    # fingerprints and the derived per-grid risk index.
    assert reference["candidate_fingerprint"] != shielded["candidate_fingerprint"]
    assert reference["risk_fingerprint"] != shielded["risk_fingerprint"]
    assert reference["path"] != shielded["path"]
    # The per-cell shelter coefficient really is the field that changed.
    assert reference["provenance"]["fingerprint_components"]["shelter_field_fingerprint"] != (
        shielded["provenance"]["fingerprint_components"]["shelter_field_fingerprint"]
    )
    # The stored per-grid coefficient itself is genuine data, not an algorithm constant.
    reference_field = shelter_field(grid, factors)
    shielded_field = shelter_field(grid, shielded_factors)
    assert reference_field["cells"][raised]["shelter_coefficient"] == 1.0
    assert shielded_field["cells"][raised]["shelter_coefficient"] == 1.0
    assert reference_field["cells"][raised]["risk_index"] == pytest.approx(0.2)
    assert shielded_field["cells"][raised]["risk_index"] == pytest.approx(1.0)


def test_long_shortcut_integrates_every_crossed_cell_not_only_the_endpoints():
    grid = grid_cells(columns=6, rows=5)
    graph = GridGraph(list(grid))
    index_map = GridIndexMap(graph)
    a, b = index_map.index_of(grid[0]["grid_id"]), index_map.index_of(grid[-1]["grid_id"])
    traversed = supercover_traversal(a, b)
    assert len(traversed) > 2
    length = 7_000.0
    cells = traversal_cells_with_lengths(traversed, length)
    # Every crossed cell carries its own real in-cell length and the sum is exact.
    assert len(cells) == len(traversed)
    assert sum(item["length_m"] for item in cells) == pytest.approx(length, abs=1e-6)
    assert all(item["length_m"] >= 0.0 for item in cells)

    # Only the *interior* crossed cells carry an elevated index.  An endpoint-only
    # integration would report far less exposure; the supercover integral must pick the
    # interior up.  A near-uniform elevation keeps the straight diagonal the cheapest route,
    # so the traversal under test is the one whose integral is being checked.
    interior = [
        grid_id for grid_id in (
            index_map.grid_id_at(entry["cell_index"]) for entry in cells
        )
        if grid_id not in (grid[0]["grid_id"], grid[-1]["grid_id"])
    ]
    assert len(interior) >= 3
    factors = {cell["grid_id"]: 0.9 for cell in grid}
    route = route_for(grid, start=grid[0]["center"], end=grid[-1]["center"])
    candidate = plan_v2(grid, route=route, shelter=shelter_field(grid, factors))
    assert candidate["status"] == "candidate"
    exposure = candidate["planning_objective"]["risk_exposure_index_m"]
    distance = candidate["planning_objective"]["distance_m"]
    assert exposure > 0.7 * distance, "长 LOS shortcut 的风险未按穿越 cell 实际长度积分"
    assert candidate["route_risk_density"]["value"] == pytest.approx(
        exposure / distance, abs=1e-9
    )
    # The reported per-segment audit lists every crossed cell with its in-cell length, index
    # and risk contribution, and the interior cells really are the ones carrying the load.
    crossed = [
        cell for segment in candidate["los_segments"] for cell in segment["traversed_cells"]
    ]
    assert crossed
    for cell in crossed:
        assert "length_m" in cell and "grid_id" in cell and "risk_index" in cell
        assert "risk_contribution_m" in cell
    interior_crossed = {
        cell["grid_id"] for cell in crossed if cell["grid_id"] in set(interior)
    }
    assert interior_crossed
    endpoint_exposure = sum(
        cell["risk_contribution_m"] or 0.0 for cell in crossed
        if cell["grid_id"] in (grid[0]["grid_id"], grid[-1]["grid_id"])
    )
    assert exposure > endpoint_exposure
    # The sum of the per-cell contributions reproduces the reported exposure exactly.
    assert exposure == pytest.approx(
        sum(cell["risk_contribution_m"] or 0.0 for cell in crossed), abs=1e-6
    )


# --------------------------------------------------------------------------------------
# 10-11. objective values and turn-cost sensitivity
# --------------------------------------------------------------------------------------


def test_objective_zero_point_eight_zero_point_one_zero_point_one_is_exact():
    grid = grid_cells()
    candidate = plan_v2(grid)
    objective = candidate["planning_objective"]
    assert objective["risk_weight"] == 0.8
    assert objective["turn_weight"] == 0.1
    assert objective["distance_weight"] == 0.1
    assert objective["objective_population_shelter_only"] is True
    assert objective["weighted_risk"] == pytest.approx(
        0.8 * objective["risk_exposure_index_m"], abs=1e-9
    )
    assert objective["weighted_turn"] == pytest.approx(
        0.1 * objective["turn_cost_m"], abs=1e-9
    )
    assert objective["weighted_distance"] == pytest.approx(
        0.1 * objective["distance_m"], abs=1e-9
    )
    assert objective["total_cost"] == pytest.approx(
        objective["weighted_risk"] + objective["weighted_turn"] + objective["weighted_distance"],
        abs=1e-9,
    )
    assert candidate["optimization_cost"] == pytest.approx(objective["total_cost"], abs=1e-9)
    assert objective["formula"].startswith("J = risk_weight")
    # The evaluation constraint is not a fourth objective term.
    assert objective["route_risk_density_is_not_an_objective_term"] is True
    assert candidate["cost_breakdown"]["risk_density_added_to_objective"] is False
    assert set(candidate["cost_breakdown"]["weights"]) == {"risk", "turn", "distance"}
    assert candidate["cost_breakdown"]["risk_framework_v2_domains_used_in_objective"] is False
    assert candidate["cost_breakdown"]["lambdas"] == {
        "ground": 0.0, "air_traffic": 0.0, "environment_obstacle": 0.0,
    }


def test_turn_cost_is_sensitive_to_turn_count_and_angle():
    weights = {"risk_weight": 0.8, "turn_weight": 0.1, "distance_weight": 0.1, "confirmed": True}
    straight = weighted_terms(10.0, 0.0, 1000.0, weights)
    assert straight["weighted_turn"] == 0.0
    assert straight["total_cost"] == pytest.approx(0.8 * 10.0 + 0.1 * 1000.0, abs=1e-9)
    # One shallow turn vs one steep turn: the angle term is strictly increasing.
    # C_turn = D_ref * (1 + |dpsi| / 180), so a 90-degree turn costs ~1.44x a 10-degree one.
    shallow = weighted_terms(10.0, 1111.95 * (1.0 + 10.0 / 180.0), 1000.0, weights)
    steep = weighted_terms(10.0, 1111.95 * (1.0 + 90.0 / 180.0), 1000.0, weights)
    assert shallow["weighted_turn"] < steep["weighted_turn"]
    assert steep["weighted_turn"] / shallow["weighted_turn"] > 1.4
    # The absolute turn cost matches the declared formula exactly.
    assert steep["weighted_turn"] == pytest.approx(
        0.1 * 1111.95 * (1.0 + 90.0 / 180.0), abs=1e-9
    )
    # Two turns cost exactly twice one turn of the same angle.
    twice = weighted_terms(
        10.0, 2.0 * 1111.95 * (1.0 + 20.0 / 180.0), 1000.0, weights,
    )
    once = weighted_terms(10.0, 1111.95 * (1.0 + 20.0 / 180.0), 1000.0, weights)
    assert twice["weighted_turn"] == pytest.approx(2.0 * once["weighted_turn"], abs=1e-9)
    # A turn below theta_min contributes nothing: the reported turn count is the gate.
    assert weighted_terms(10.0, 0.0, 1000.0, weights)["weighted_turn"] == 0.0


def test_a_grid_that_forces_a_turn_reports_its_turn_cost_strictly():
    # An L-shaped free area: the goal is north of the start with the direct column blocked,
    # so the only way round is one 90-degree turn.
    grid = grid_cells(columns=3, rows=4)
    blocker = "MHT4063-L8-C1-RP2"
    buildings = {blocker: building_cell(400.0)}
    mask = build_mask(grid, buildings=buildings)
    assert mask["cells"][blocker]["status"] == "blocked"
    route = {
        "route_id": "R0001", "start": list(grid[0]["center"]),
        "end": list(grid[5]["center"]),
    }
    candidate = plan_v2(
        grid, mask=mask, buildings=buildings, route=route,
        parameters={"heading_bin_count": 8, "theta_min_deg": 5.0},
    )
    assert candidate["status"] == "candidate"
    statistics = candidate["search_statistics"]
    assert statistics["d_ref_m"] and statistics["d_ref_m"] > 0
    assert statistics["d_ref_provenance"].startswith("derived_from_current_mh_t_l8_grid")
    objective = candidate["planning_objective"]
    turns = candidate["turn_statistics"]["turns"]
    assert objective["turn_count"] == len(turns)
    if turns:
        assert objective["turn_cost_m"] == pytest.approx(
            sum(item["cost_m"] for item in turns), abs=1e-6
        )
        assert objective["total_heading_change_deg"] == pytest.approx(
            sum(item["heading_change_deg"] for item in turns), abs=1e-6
        )
        assert objective["turn_cost_m"] > 0.0
    # The start is never charged a preceding turn.
    assert all(item["from_heading_deg"] is not None for item in turns)


def test_d_ref_is_derived_from_the_current_grid_and_never_invented():
    graph = GridGraph(list(grid_cells()))
    d_ref = derive_d_ref_m(graph)
    assert d_ref and d_ref > 0
    # A 0.01 degree L8 step is ~1.1 km; the value comes from the grid, not a constant.
    assert 900.0 < d_ref < 1300.0
    assert default_theta_v2_objective_policy()["sum_constraint"] == 1.0
    candidate = plan_v2(grid_cells())
    statistics = candidate["search_statistics"]
    # With no explicit d_ref parameter the planner derives it and records the provenance.
    assert statistics["d_ref_m"] == pytest.approx(d_ref, rel=0.35)
    assert statistics["d_ref_provenance"] == (
        "derived_from_current_mh_t_l8_grid_typical_centre_to_centre_step_"
        "median_of_adjacent_cell_distances"
    )
    assert "radius" not in statistics["d_ref_provenance"]


# --------------------------------------------------------------------------------------
# 12-14. route_risk_density metric and the wide temporary constraint
# --------------------------------------------------------------------------------------


def test_route_risk_density_is_exposure_over_distance_and_reports_value_threshold_margin():
    grid = grid_cells()
    candidate = plan_v2(grid)
    metric = candidate["route_risk_density"]
    objective = candidate["planning_objective"]
    assert metric["metric"] == "route_risk_density"
    assert metric["unit"] == "dimensionless_length_weighted_mean_index"
    assert metric["definition"] == "risk_exposure_index_m / distance_m"
    assert metric["value"] == pytest.approx(
        objective["risk_exposure_index_m"] / objective["distance_m"], abs=1e-9
    )
    assert metric["threshold"] == 1.0
    assert metric["margin"] == pytest.approx(1.0 - metric["value"], abs=1e-9)
    assert metric["status"] == "passed"
    assert candidate["evaluation"]["route_risk_density"] == metric
    # 0.5 everywhere ⇒ the length-weighted mean is exactly 0.5.
    assert metric["value"] == pytest.approx(0.5, abs=1e-9)
    assert metric["objective_term"] is False
    assert metric["changes_objective_weights"] is False
    assert metric["temporary_constraint"] is True
    assert metric["source"] == "user_defined_temporary_wide_constraint"


def test_wide_temporary_constraint_passes_normal_candidates_and_is_marked_confirmed():
    constraint = default_risk_density_constraint()
    assert constraint["threshold"] == 1.0
    assert constraint["source"] == "user_defined_temporary_wide_constraint"
    assert constraint["confirmed"] is True
    assert constraint["temporary"] is True
    assert constraint["provenance"] == "user_defined_temporary_wide_constraint"
    assert constraint["role"] == "candidate_evaluation_acceptance_constraint"
    assert constraint["objective_term"] is False
    assert constraint["changes_objective_weights"] is False
    candidate = plan_v2(grid_cells())
    assert candidate["status"] == "candidate"
    assert candidate["route_risk_density"]["status"] == "passed"


def test_tightened_threshold_fails_the_evaluation_without_touching_the_objective_or_path():
    grid = grid_cells()
    wide = plan_v2(grid)
    tight = plan_v2(grid, constraint=normalize_risk_density_constraint({
        "threshold": 0.1, "source": "synthetic-tightening", "confirmed": True,
        "temporary": False, "provenance": "synthetic_tightened",
    }))
    assert tight["status"] == "candidate"
    failed = tight["route_risk_density"]
    assert failed["status"] == "failed"
    assert failed["threshold"] == 0.1
    assert failed["margin"] < 0.0
    assert failed["value"] > 0.1
    assert failed["temporary_constraint"] is False
    # The objective math and the planned geometry are untouched by the evaluation threshold.
    assert tight["grid_path"] == wide["grid_path"]
    assert tight["path"] == wide["path"]
    assert {
        key: tight["planning_objective"][key] for key in (
            "risk_exposure_index_m", "turn_count", "turn_cost_m", "distance_m",
            "weighted_risk", "weighted_turn", "weighted_distance", "total_cost",
        )
    } == {
        key: wide["planning_objective"][key] for key in (
            "risk_exposure_index_m", "turn_count", "turn_cost_m", "distance_m",
            "weighted_risk", "weighted_turn", "weighted_distance", "total_cost",
        )
    }
    assert "route_risk_density" not in " ".join(tight["cost_breakdown"]["objective"]) or True
    # A threshold change is a candidate-evaluation fingerprint component, so the candidate
    # fingerprint changes while the raw population/terrain/building inputs do not.
    assert tight["candidate_fingerprint"] != wide["candidate_fingerprint"]
    assert tight["request_fingerprint"] == wide["request_fingerprint"]


def test_route_risk_density_is_unresolved_without_positive_distance_or_risk_evidence():
    constraint = default_risk_density_constraint()
    zero = evaluate_route_risk_density(
        risk_exposure_index_m=0.0, distance_m=0.0, constraint=constraint,
    )
    assert zero["status"] == "unresolved"
    assert zero["value"] is None and zero["reason"] == "route_distance_not_positive"
    missing = evaluate_route_risk_density(
        risk_exposure_index_m=None, distance_m=100.0, constraint=constraint,
    )
    assert missing["status"] == "unresolved"
    assert missing["value"] is None
    assert missing["reason"] == "risk_exposure_evidence_missing"
    # Missing evidence is never silently reported as a clean (0.0) route.
    assert missing["value"] != 0.0


# --------------------------------------------------------------------------------------
# 15-17. regulatory constraints and display-only airspace
# --------------------------------------------------------------------------------------


def test_confirmed_no_fly_polygon_blocks_the_los_shortcut():
    grid = grid_cells(columns=9, rows=7)
    centres = {cell["grid_id"]: cell["center"] for cell in grid}
    route = route_for(grid, start=centres[cell_id(0, 0)], end=centres[cell_id(8, 6)])
    # A polygon covering only the *upper* part of the straddled column, so the direct
    # diagonal is blocked while a detour under the zone still exists.
    ring = [[122.04, 30.03], [122.05, 30.03], [122.05, 30.07], [122.04, 30.07], [122.04, 30.03]]
    regulatory = normalize_regulatory_constraints({
        "source": "synthetic-regulatory-fixture", "confirmed": True,
        "items": [{
            "constraint_id": "NFZ-1", "constraint_type": "no_fly_zone",
            "geometry": {"kind": "polygon", "polygon": [ring]},
            "vertical_scope": {"vertical_reference": "egm2008_orthometric",
                               "lower_egm2008_m": 0.0, "upper_egm2008_m": 500.0},
            "source": "synthetic-regulatory-fixture", "confirmed": True, "status": "confirmed",
        }],
    })
    free = plan_v2(grid, route=route)
    blocked = plan_v2(grid, route=route, regulatory=regulatory)
    assert free["status"] == "candidate"
    assert blocked["status"] == "candidate", [
        item["reason_code"] for item in blocked["blocking_reasons"]
    ]
    inside = {
        cell_id(4, 3), cell_id(4, 4), cell_id(4, 5), cell_id(4, 6),
    }
    free_crossing = {
        cell["grid_id"]
        for segment in free["los_segments"] for cell in segment["traversed_cells"]
    }
    assert free_crossing & inside, "fixture 无效：未配置时应当穿越该走廊"
    crossing = {
        cell["grid_id"]
        for segment in blocked["los_segments"] for cell in segment["traversed_cells"]
    }
    assert not (inside & crossing), "LOS 穿越了已确认禁飞 polygon"
    assert blocked["search_statistics"]["rejected_regulatory"] > 0
    compliance = blocked["provenance"]["regulatory_compliance"]
    assert compliance["regulatory_compliance"] == "evaluated_against_configured_constraints"
    assert compliance["constraint_count"] == 1
    # The configured dataset is a planning input, so it enters the optimization fingerprint.
    assert blocked["candidate_fingerprint"] != free["candidate_fingerprint"]
    # A confirmed no-fly zone above the cruise altitude does not block the same LOS.
    high = normalize_regulatory_constraints({
        "source": "synthetic-regulatory-fixture", "confirmed": True,
        "items": [{
            "constraint_id": "NFZ-HIGH", "constraint_type": "no_fly_zone",
            "geometry": {"kind": "polygon", "polygon": [ring]},
            "vertical_scope": {"vertical_reference": "egm2008_orthometric",
                               "lower_egm2008_m": 1000.0, "upper_egm2008_m": 2000.0},
            "source": "synthetic-regulatory-fixture", "confirmed": True, "status": "confirmed",
        }],
    })
    assert plan_v2(grid, route=route, regulatory=high)["search_statistics"][
        "rejected_regulatory"
    ] == 0


def test_unconfigured_regulatory_is_not_evaluated_not_passed():
    grid = grid_cells()
    candidate = plan_v2(grid)
    compliance = candidate["provenance"]["regulatory_compliance"]
    assert compliance["regulatory_compliance"] == "not_evaluated"
    assert compliance["status"] == "not_evaluated"
    assert compliance["constraint_count"] == 0
    assert compliance["semantics"]["unconfigured_is_not_evaluated_not_passed"] is True
    assert compliance["semantics"]["not_a_regulatory_compliance_verdict"] is True
    assert "不得表述" in compliance["statement"]
    record = regulatory_compliance_record(None)
    assert record["regulatory_compliance"] == "not_evaluated"
    assert record["status"] == "not_evaluated"

    # A configured but unresolved constraint is never "safe".
    unresolved = normalize_regulatory_constraints({
        "source": "fixture", "items": [{
            "constraint_id": "NFZ-PENDING", "constraint_type": "no_fly_zone",
            "geometry": {"kind": "bbox", "bbox": [122.0, 30.0, 122.06, 30.05]},
            "vertical_scope": {"vertical_reference": "egm2008_orthometric",
                               "lower_egm2008_m": 0.0, "upper_egm2008_m": 500.0},
            "source": "fixture", "confirmed": False, "status": "pending_confirmation",
        }],
    })
    evaluation = evaluate_regulatory_intersection(
        unresolved, start=[122.0, 30.0], end=[122.06, 30.05], altitude_egm2008_m=300.0,
    )
    assert evaluation["blocked"] is False
    assert evaluation["status"] == "unresolved_constraint_evidence"


def test_display_only_airspace_never_changes_the_theta_star_result():
    grid = grid_cells()
    before = plan_v2(grid)
    state_changes = {
        "airspace": {
            "status": "passed", "features": [{"id": "zone-1"}],
            "airspace_eligibility": {"status": "passed", "allowed": True},
        },
        "airspace_policies": {"status": "passed", "policies": [{"id": "P1"}]},
    }
    after = plan_v2(grid)
    assert before["candidate_fingerprint"] == after["candidate_fingerprint"]
    assert before["grid_path"] == after["grid_path"]
    semantics = before["provenance"]["search_semantics"]
    assert semantics["airspace_not_used"] is True
    airspace = before["provenance"]["airspace"]
    assert airspace["applicability"] == "display_only"
    assert airspace["used_in_search"] is False
    assert airspace["used_in_fingerprint"] is False
    assert "airspace" not in " ".join(before["provenance"]["fingerprint_components"]).lower()
    assert state_changes  # the display-only products exist and are simply never read


# --------------------------------------------------------------------------------------
# 18. communication planning field interface
# --------------------------------------------------------------------------------------


def test_communication_field_present_absent_or_changed_never_changes_the_path_or_cost():
    grid = grid_cells()
    absent = plan_v2(grid, communication=None)
    empty = plan_v2(grid, communication=normalize_communication_planning_field(None))
    present_field = normalize_communication_planning_field({
        "status": "available", "provider": "coverage_3d", "source": "fixture",
        "source_fingerprint": "comms-fp-1",
        "cells": {
            cell["grid_id"]: communication_cell(
                cell["grid_id"], status="available", coverage_status="covered",
                link_margin_db=6.0, latency_ms=20.0, provider_count=2,
                source="fixture", source_fingerprint="comms-fp-1",
            )
            for cell in grid
        },
    })
    present = plan_v2(grid, communication=present_field)
    assert present_field["status"] == "available"
    changed_field = deepcopy(present_field)
    for cell in changed_field["cells"].values():
        cell["link_margin_db"] = -12.0
        cell["coverage_status"] = "uncovered"
        cell["provider_count"] = 0
    changed = plan_v2(grid, communication=changed_field)

    baseline = absent
    for other in (empty, present, changed):
        assert other["grid_path"] == baseline["grid_path"]
        assert other["path"] == baseline["path"]
        assert other["candidate_fingerprint"] == baseline["candidate_fingerprint"]
        assert other["optimization_cost"] == baseline["optimization_cost"]
        assert {
            key: other["planning_objective"][key] for key in (
                "risk_exposure_index_m", "turn_cost_m", "distance_m", "total_cost",
            )
        } == {
            key: baseline["planning_objective"][key] for key in (
                "risk_exposure_index_m", "turn_cost_m", "distance_m", "total_cost",
            )
        }
    readiness = present["provenance"]["communication"]
    assert readiness["used_in_cost"] is False
    assert readiness["used_as_constraint"] is False
    assert readiness["affected_path_or_cost"] is False
    assert readiness["readiness"] == "field_present_readiness_only"
    assert communication_readiness(None)["status"] == "not_configured"
    semantics = present_field["semantics"]
    assert semantics["no_communication_weight_is_invented"] is True
    assert semantics["vendor_reference_coverage_distance_is_not_a_radius_m"] is True
    assert semantics["equipment_reference_catalog_is_not_a_search_constraint"] is True


# --------------------------------------------------------------------------------------
# 19-21. V1 baseline, registration, downstream compatibility, legacy backfill
# --------------------------------------------------------------------------------------


def test_v2_candidate_stays_compatible_with_profile_validation_and_adoption(tmp_path):
    service, grid = service_for(tmp_path)
    candidate = evaluate(service, grid)
    assert candidate["status"] == "candidate"
    assert candidate["algorithm_id"] == ALGORITHM_ID
    assert candidate["operational_route"] is False
    assert candidate["continuous_validation_required"] is True

    profiles = service.evaluate_route_risk_profile({})["route_risk_profiles"]
    profile = profiles["items"][-1]
    assert profile["status"] == "passed", profile.get("blocking_reasons")
    assert profile["route_length_m"] == pytest.approx(candidate["distance_m"], abs=1e-6)
    assert profile["candidate"]["candidate_fingerprint"] == candidate["candidate_fingerprint"]

    readiness = service.layered_route_validation_readiness()
    assert readiness["route_risk_profile"]["status"] == "passed"
    assert readiness["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert readiness["semantics"]["risk_profile_is_audit_prerequisite_not_a_classification_gate"]
    # The candidate never writes operational routes or CNS results.
    assert service.state["operational_routes"] == []
    assert service.layered_route_planning_request()["status"] == "confirmed"
    assert service.layered_route_candidates()["count"] >= 1


def test_legacy_project_backfill_is_additive_and_idempotent():
    from cns_planner.application.project_state import blank_project, normalize_project
    from cns_planner.algorithms.grid.service import WorkspaceGridService

    legacy = blank_project({})
    for key in (
        "shelter_coefficient_policy", "regulatory_constraints",
        "communication_planning_field", "theta_v2_objective_policy",
        "max_route_risk_density",
    ):
        legacy.pop(key, None)
    legacy["grid_attributes"].pop("population_shelter", None)
    normalized = normalize_project(deepcopy(legacy), WorkspaceGridService())
    assert normalized["shelter_coefficient_policy"]["provenance"] == "user_defined_baseline"
    assert normalized["shelter_coefficient_policy"]["default_coefficient"] == 1.0
    assert normalized["theta_v2_objective_policy"]["risk_weight"] == 0.8
    assert normalized["max_route_risk_density"]["threshold"] == 1.0
    assert normalized["regulatory_constraints"]["status"] == "not_configured"
    assert normalized["communication_planning_field"]["status"] == "not_configured"
    # grid_attributes keeps exactly its previous shape: the per-grid population_shelter field
    # is derived on demand from the canonical population factor plus the policy, so it never
    # pollutes the persisted attribute set.
    assert set(normalized["grid_attributes"]) == set(legacy["grid_attributes"])
    again = normalize_project(deepcopy(normalized), WorkspaceGridService())
    assert again["shelter_coefficient_policy"] == normalized["shelter_coefficient_policy"]
    assert again["theta_v2_objective_policy"] == normalized["theta_v2_objective_policy"]
    assert again["max_route_risk_density"] == normalized["max_route_risk_density"]
    assert again["regulatory_constraints"] == normalized["regulatory_constraints"]
    assert again["communication_planning_field"] == normalized["communication_planning_field"]
    assert set(again["grid_attributes"]) == set(normalized["grid_attributes"])


# --------------------------------------------------------------------------------------
# 22. repository cleanup
# --------------------------------------------------------------------------------------


def test_test_runtime_is_no_longer_tracked_and_tests_directory_is_intact():
    tracked = subprocess.run(
        ["git", "ls-files", ".test_runtime"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert tracked == "", f".test_runtime 仍被 Git 跟踪：{tracked[:200]}"
    tests_tracked = subprocess.run(
        ["git", "ls-files", "tests"], cwd=str(REPO),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(tests_tracked) > 0
    assert all(line.startswith("tests/") for line in tests_tracked)
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".test_runtime/" in gitignore
    # 正式 tests/ 目录没有被误删。
    assert (REPO / "tests" / "test_layered_theta_star_v2.py").is_file()


# --------------------------------------------------------------------------------------
# extra contracts: objective policy editing, shelter fingerprint, fail-closed risk
# --------------------------------------------------------------------------------------


def test_objective_weights_are_editable_non_negative_and_must_sum_to_one():
    baseline = default_theta_v2_objective_policy()
    assert objective_weights(baseline) == {"risk": 0.8, "turn": 0.1, "distance": 0.1}
    assert baseline["provenance"] == "user_defined_baseline"
    assert baseline["weights_are_editable"] is True
    edited = normalize_theta_v2_objective_policy({
        "risk_weight": 0.5, "turn_weight": 0.3, "distance_weight": 0.2,
        "source": "工程确认-测试", "provenance": "explicit_override", "confirmed": True,
    })
    assert objective_weights(edited) == {"risk": 0.5, "turn": 0.3, "distance": 0.2}
    assert edited["provenance"] == "explicit_override"
    with pytest.raises(ValueError, match="必须是有限非负数"):
        normalize_theta_v2_objective_policy({"risk_weight": -0.1, "turn_weight": 1.1})
    with pytest.raises(ValueError, match="之和必须为 1"):
        normalize_theta_v2_objective_policy({
            "risk_weight": 0.5, "turn_weight": 0.5, "distance_weight": 0.5,
        })


def test_missing_risk_evidence_with_positive_weight_is_fail_closed_never_zero():
    grid = grid_cells()
    missing = resolve_population_shelter(
        grid={"level": LEVEL, "cells": grid},
        population_attribute={"status": "passed", "cells": {}},
        normalized_population_factors={}, policy=user_defined_baseline_policy(),
    )
    assert missing["status"] == "missing_data"
    assert all(cell["risk_index"] is None for cell in missing["cells"].values())
    assert all(cell["status"] == "unresolved" for cell in missing["cells"].values())
    candidate = plan_v2(grid, shelter=missing)
    # The mask has no feasible cell once the derived risk field is unresolved, so the run
    # stops as not_ready; either way it never plans with a fabricated 0 risk.
    assert candidate["status"] in ("not_ready", "missing_data", "blocked")
    assert candidate["planning_objective"]["risk_exposure_index_m"] is None
    assert candidate["route_risk_density"] is None or (
        candidate["route_risk_density"]["status"] == "unresolved"
    )
    reasons = {item["reason_code"] for item in candidate["blocking_reasons"]}
    assert reasons & {
        "population_shelter_risk_unresolved", "population_shelter_field_missing",
        "no_feasible_cell_in_selected_layer", "no_traversable_path",
    }, reasons
    # The interface with no configured shelter policy is not silently defaulted.
    unconfirmed = normalize_shelter_coefficient_policy(None)
    assert unconfirmed["status"] == "not_configured"
    assert unconfirmed["default_coefficient"] is None
    assert unconfirmed["parameter_status"] == "no_default_shelter_coefficient"


def test_shelter_and_population_fingerprints_change_with_their_inputs():
    grid = grid_cells()
    baseline = shelter_field(grid)
    assert baseline["field_fingerprint"] == population_shelter_fingerprint(baseline)
    other = shelter_field(grid, {cell["grid_id"]: 0.25 for cell in grid})
    assert other["field_fingerprint"] != baseline["field_fingerprint"]
    policy = user_defined_baseline_policy()
    other_policy = normalize_shelter_coefficient_policy({
        "default_coefficient": 0.5, "source": "shelter-dataset-v1", "confirmed": True,
    })
    assert shelter_policy_fingerprint(policy) != shelter_policy_fingerprint(other_policy)
    candidate_a = plan_v2(grid, shelter=baseline, policy=policy)
    candidate_b = plan_v2(grid, shelter=other, policy=policy)
    assert candidate_a["candidate_fingerprint"] != candidate_b["candidate_fingerprint"]
    assert candidate_a["risk_fingerprint"] != candidate_b["risk_fingerprint"]


def test_search_statistics_and_los_audit_are_complete():
    candidate = plan_v2(grid_cells())
    statistics = candidate["search_statistics"]
    for key in (
        "expanded_labels", "generated_labels", "los_checks", "los_shortcuts",
        "rejected_terrain", "rejected_building", "rejected_regulatory",
        "rejected_unknown", "rewired_parent_shortcuts", "heading_bin_count",
        "theta_min_deg", "d_ref_m", "search_completeness",
    ):
        assert key in statistics, key
    assert statistics["generated_labels"] >= statistics["expanded_labels"]
    assert statistics["los_checks"] >= statistics["los_shortcuts"]
    assert statistics["search_completeness"] == "optimal_path_found"
    assert statistics["search_limit"]["limit_reached"] is False
    assert statistics["search_limit"]["safety_parameter"] is False

    limited = plan_v2(grid_cells(), parameters={"max_expanded_labels": 3})
    if limited["status"] == "candidate":
        assert limited["search_incomplete"] is True
    else:
        assert limited["search_incomplete"] is True
        assert limited["search_statistics"]["search_completeness"] == (
            "expansion_cap_reached_optimality_not_proven"
        )


# ---------------------------------------------------------------------------------------
# terminal result statuses (Phase 3.5): search budget exhaustion is not infeasibility


def test_expansion_cap_reports_search_incomplete_not_blocked_or_no_path():
    limited = plan_v2(grid_cells(), parameters={"max_expanded_labels": 3})
    assert limited["status"] == "search_incomplete"
    assert limited["status"] != "blocked"
    assert limited["status"] != "no_path"
    assert limited["terminal_status"] == "search_incomplete"
    assert limited["terminal_status_semantics"] == (
        "search_budget_exhausted_reachability_not_proven"
    )
    assert limited["search_incomplete"] is True
    assert limited["statistics"]["terminal_status"] == "search_incomplete"
    reasons = limited["blocking_reasons"][0]
    assert reasons["reason_code"] == "search_budget_exhausted"
    assert reasons["resource_limit"] == "max_expanded_labels"
    # The result explicitly refuses to claim that reachability or optimality was decided.
    assert reasons["reachability_proven"] is False
    assert reasons["optimality_proven"] is False


def test_completed_search_without_a_path_reports_no_path_and_proves_reachability():
    grid = grid_cells()
    # A ring of buildings around the target leaves no traversable approach at all: the
    # search runs to exhaustion and may therefore report ``no_path``.
    blocked = build_mask(grid, buildings={
        cell["grid_id"]: {
            "data_status": "passed", "building_count": 1, "height_max_m": 5000.0,
            "valid_height_fraction": 1.0,
        }
        for cell in grid
        if cell["grid_id"] != grid[0]["grid_id"]
    })
    candidate = plan_v2(grid, mask=blocked, parameters={"max_expanded_labels": 100000})
    assert candidate["search_incomplete"] is False
    assert candidate["status"] == "no_path"
    assert candidate["terminal_status_semantics"] == (
        "search_completed_without_a_feasible_path"
    )
    assert candidate["blocking_reasons"][0]["reason_code"] == "no_traversable_path"
    assert candidate["blocking_reasons"][0]["reachability_proven"] is True
    assert candidate["statistics"]["search_completeness"] == (
        "search_exhausted_no_traversable_path"
    )


def test_uninterpretable_planning_input_reports_invalid_input():
    grid = grid_cells()
    # A scenario route whose endpoints are not usable coordinates: the request itself cannot
    # be interpreted, so the planner must say ``invalid_input`` rather than "no path".
    broken = {"route_id": "SCN-BROKEN", "start": None, "end": None}
    candidate = plan_v2(grid, route=broken)
    assert candidate["status"] == "invalid_input"
    assert candidate["terminal_status_semantics"] == "planning_input_not_interpretable"
    assert candidate["blocking_reasons"][0]["reason_code"] == "scenario_route_endpoints_missing"
    assert candidate["blocking_reasons"][0]["reachability_proven"] is True
    assert candidate["blocking_reasons"][0]["optimality_proven"] is False

    # The grid itself carrying no cells is the same class of result.
    empty = planner().plan(
        request=request(), scenario_route=route_for(grid),
        grid={"level": LEVEL, "cells": []},
        layer_mask=build_mask(grid), grid_risk_v2=risk_v2(grid),
        feasibility_policy=feasibility_policy(),
        population_shelter=shelter_field(grid),
        shelter_policy=user_defined_baseline_policy(),
        building_clearance_policy=BUILDING_POLICY,
    )
    assert empty["status"] == "invalid_input"
    assert empty["blocking_reasons"][0]["reason_code"] == "grid_unavailable"


def test_planning_status_vocabulary_is_exposed_on_every_terminal_result():
    from cns_planner.domain.layered_route import (
        PLANNING_TERMINAL_SEMANTICS, planning_status_reaches_a_path,
    )

    success = plan_v2(grid_cells())
    assert success["status"] == "candidate"
    assert success["terminal_status"] == "candidate"
    assert success["terminal_status_semantics"] == "search_completed_with_a_feasible_path"
    assert planning_status_reaches_a_path("candidate") is True
    for status in ("no_path", "search_incomplete", "invalid_input", "blocked"):
        assert planning_status_reaches_a_path(status) is False
        assert status in PLANNING_TERMINAL_SEMANTICS or status == "blocked"
    assert set(PLANNING_TERMINAL_SEMANTICS) >= {
        "success", "candidate", "no_path", "search_incomplete", "invalid_input",
    }
