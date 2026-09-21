"""Layered Risk-Aware Route Planner V1 tests.

Covers the explicit planning request, the "no default clearance / no default λ" policies,
the coarse terrain/building feasibility mask (blocked vs unknown, unknown never 0), the
single-layer MH/T L8 A* with Risk Framework V2 soft cost, the cost formula and breakdown,
the candidate container (never an operational route, never a ``RouteOperatingLayer``), the
layered-only invalidation chain and the GIS-boundary adapter.  Legacy V1/V2/V3 behaviour is
intentionally untouched and stays locked by the existing characterization suites.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.registry import (
    build_default_algorithm_registry, default_algorithm_selection,
)
from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.api.router import ApiRouter
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route import (
    COST_DOMAIN_IDS, active_cost_domains, cost_lambdas,
    default_layer_feasibility_mask, default_layered_route_cost_policy,
    default_layered_route_feasibility_policy, default_layered_route_request,
    empty_layered_route_candidate_collection, feasibility_policy_is_runnable,
    normalize_layered_route_candidate, normalize_layered_route_candidate_collection,
    normalize_layered_route_cost_policy, normalize_layered_route_feasibility_policy,
    normalize_layered_route_request, resolve_cruise_altitude,
)
from cns_planner.domain.spatial_3d import normalize_spatial_3d
from cns_planner.gis.layered_feasibility_adapter import (
    LAYERED_BUILDING_MAPPING_METHOD, LAYERED_TERRAIN_MAPPING_METHOD,
    LayeredFeasibilityAdapter, layered_feasibility_source_status,
)
from cns_planner.layered_route_planner.planner import (
    LayeredRoutePlannerV1, build_layer_feasibility_mask, resolve_lambda_domain_indices,
)

DEFAULTS = Path("cns_planner/config/defaults.json")
LAYER_ID = "L8-LOW"
LOW_ALTITUDE = 300.0


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def grid_cells(columns=4, rows=3, level=8):
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


class StubTerrainSource:
    """A structural stand-in for the verified FABDEM window sampler."""

    def __init__(self, elevations=None, *, available=True, vertical_reference="egm2008_orthometric"):
        self.elevations = elevations or {}
        self._available = available
        self.vertical_reference = vertical_reference
        self.vertical_status = "confirmed" if available else "unresolved"
        self.calls = 0

    def usable(self):
        return self._available, None if self._available else "terrain_vertical_datum_unresolved"

    def describe(self):
        return {"role": "terrain_dtm", "dataset": "FABDEM", "fixture": True}

    def sample_cells(self, cells, *, transform):
        self.calls += 1
        result = {}
        for cell in cells:
            key = str(cell["fine_cell_id"])
            value = self.elevations.get(key, self.elevations.get("*"))
            result[key] = (
                {
                    "data_status": "passed", "surface_elevation_max_egm2008_m": value,
                    "valid_pixel_count": 4, "nodata_pixel_count": 0,
                    "sampling": "intersecting_valid_fabdem_pixels_max_egm2008", "reason": None,
                }
                if value is not None else
                {
                    "data_status": "unknown", "surface_elevation_max_egm2008_m": None,
                    "valid_pixel_count": 0, "nodata_pixel_count": 9,
                    "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
                    "reason": "no_valid_dtm_pixel_intersects_this_fine_cell_nodata_is_not_flat_ground",
                }
            )
        return result


def workflow(
    tmp_path, name="project.json", *, cells=None, route=True, altitude=LOW_ALTITUDE,
    lower=250.0, upper=350.0,
):
    """A project whose ``layered_route_planner`` is **explicitly** Layered Planner V1.

    Theta* V2 is the project's default layered planner, so a V1 behaviour suite must select
    V1 explicitly — exactly what a legacy project with a saved V1 selection does.  The
    default-selection migration itself is covered by the dedicated registry tests.
    """

    service = WorkflowService(tmp_path / name, DEFAULTS)
    service.select_algorithm({
        "algorithm_type": "layered_route_planner",
        "algorithm_id": "layered_route_planner_v1",
        "version": "1.0",
        "parameters": {},
    })
    assert service.layered_route_planner_service.planner.algorithm_id == (
        "layered_route_planner_v1"
    )
    grid = grid_cells() if cells is None else cells
    service.state["grid"] = {
        "status": "passed", "level": 8, "count": len(grid), "cells": grid,
    }
    if route:
        # Endpoints follow the current grid: an OD pair anchored to another grid would make
        # every run fail for the wrong reason.
        service.state["scenario_routes"] = [{
            "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
            "start": list(grid[0]["center"]), "end": list(grid[-1]["center"]),
        }]
    service.state["spatial_3d"]["altitude_layers"] = [
        normalize_spatial_3d({"altitude_layers": [layer(
            nominal_altitude_m=altitude, lower_altitude_m=lower, upper_altitude_m=upper,
        )]})["altitude_layers"][0]
    ]
    return service


def set_grid(service, cells):
    """Point the project (and its scenario route endpoints) at a specific grid."""

    service.state["grid"] = {
        "status": "passed", "level": 8, "count": len(cells), "cells": cells,
    }
    service.state["scenario_routes"] = [{
        "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
        "start": list(cells[0]["center"]), "end": list(cells[-1]["center"]),
    }]
    return cells


def configured_sources(service):
    """Register an available (in-memory) coarse feasibility source for readiness tests.

    In the real application this provider is supplied by ``ApplicationContext`` from the
    configured verified FABDEM/buildings sources; it never opens a dataset.
    """

    service.layered_route_planner_service.source_status = lambda: {
        "terrain": {"available": True, "reason": None},
        "buildings": {"role": "buildings", "audit_status": "configured_unverified"},
        "building_grid": {"role": "building_grid", "audit_status": "configured_unverified"},
        "airspace": {"role": "airspace", "applicability": "display_only"},
    }


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


def risk_v2(cells, index=0.5, *, status="passed"):
    """Canonical ``grid_risk_v2`` fixture: nested ``cell["domains"][domain_id]`` records."""

    return {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains", "algorithm_version": "2.0",
        "input_fingerprint": "riskv2-input-fixture", "policy_fingerprint": "riskv2-policy-fixture",
        "cells": {
            cell["grid_id"]: {
                "grid_id": cell["grid_id"], "status": "passed",
                "domains": {
                    domain_id: {
                        "domain_id": domain_id, "status": status, "index": index,
                    }
                    for domain_id in ("ground", "air_traffic", "environment_obstacle")
                },
                "factors": {},
            }
            for cell in cells
        },
    }


def stub_facts(grid, elevation=10.0, buildings=None):
    buildings = buildings or {}
    return [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": elevation,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": buildings.get(cell["grid_id"], {
                "data_status": "passed", "building_count": 0, "height_max_m": None,
                "valid_height_fraction": None,
            }),
        }
        for cell in grid
    ]


class StubAdapter:
    """GIS-boundary stand-in: canonical facts only, never fabricated."""

    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    def build_cells(self, grid_cells, state):
        self.calls += 1
        return deepcopy(self.facts)

    def describe(self):
        return {
            "adapter_id": "stub", "terrain_mapping_method": LAYERED_TERRAIN_MAPPING_METHOD,
            "building_mapping_method": LAYERED_BUILDING_MAPPING_METHOD,
        }

    def usable(self):
        return True, None

    def source_status(self):
        return {"terrain": {"available": True, "reason": None}}


# --------------------------------------------------------------------------------------
# 1. planning request
# --------------------------------------------------------------------------------------


def test_planning_request_requires_an_explicit_altitude_layer():
    pending = normalize_layered_route_request({"scenario_route_id": "R1"})
    assert pending["status"] == "blocked"
    assert pending["status_reason"] == "altitude_layer_not_explicitly_selected"
    assert pending["altitude_layer_id"] is None
    # The layer is never inferred: an OD pair without a layer is blocked too.
    od = normalize_layered_route_request({"start_node_id": "N1", "end_node_id": "N2"})
    assert od["status"] == "blocked"
    assert od["parameter_status"] == "no_default_altitude_layer"


def test_planning_request_is_pending_until_confirmed_and_requires_a_source():
    candidate = normalize_layered_route_request({
        "scenario_route_id": "R1", "altitude_layer_id": "L1",
    })
    assert candidate["status"] == "pending_confirmation"
    assert candidate["status_reason"] == "planning_request_not_confirmed"
    confirmed = normalize_layered_route_request({
        "scenario_route_id": "R1", "altitude_layer_id": "L1",
        "source": "工程确认", "confirmed": True,
    })
    assert confirmed["status"] == "confirmed"
    with pytest.raises(ValueError, match="显式 source"):
        normalize_layered_route_request({"altitude_layer_id": "L1", "confirmed": True})


def test_request_rejects_a_route_id_together_with_an_od_pair():
    with pytest.raises(ValueError, match="不得同时提供"):
        normalize_layered_route_request({
            "scenario_route_id": "R1", "start_node_id": "N1", "end_node_id": "N2",
            "altitude_layer_id": "L1",
        })


def test_default_request_has_no_scenario_and_no_layer():
    default = default_layered_route_request()
    assert default["scenario_route_id"] is None
    assert default["altitude_layer_id"] is None
    assert default["confirmed"] is False
    assert default["parameter_status"] == "no_default_altitude_layer"


# --------------------------------------------------------------------------------------
# 2. policies: no default clearance, no default lambda, null != 0
# --------------------------------------------------------------------------------------


def test_feasibility_policy_ships_without_a_default_clearance():
    default = default_layered_route_feasibility_policy()
    assert default["terrain_vertical_clearance_m"] is None
    assert default["status"] == "blocked"
    assert default["parameter_status"] == "no_default_clearance"
    runnable, reason = feasibility_policy_is_runnable(default)
    assert runnable is False
    assert reason == "terrain_vertical_clearance_not_configured"


def test_feasibility_policy_distinguishes_null_from_an_explicit_zero():
    zero = normalize_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 0.0, "source": "工程确认", "confirmed": True,
    })
    assert zero["terrain_vertical_clearance_m"] == 0.0
    assert zero["status"] == "confirmed"
    assert feasibility_policy_is_runnable(zero)[0] is True
    missing = normalize_layered_route_feasibility_policy({"confirmed": True, "source": "x"})
    assert missing["terrain_vertical_clearance_m"] is None
    assert missing["status"] == "blocked"
    with pytest.raises(ValueError, match="显式 source"):
        normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 5.0, "confirmed": True,
        })


def test_cost_policy_defaults_are_null_and_null_is_not_zero():
    default = default_layered_route_cost_policy()
    assert all(default[f"{domain}_lambda"] is None for domain in COST_DOMAIN_IDS)
    assert default["status"] == "pending_confirmation"
    assert default["parameter_status"] == "no_default_lambda"
    assert default["null_is_not_zero"] is True
    assert default["explicit_zero_is_legal"] is True


def test_explicit_zero_lambda_disables_a_domain_and_null_stays_pending():
    zero = normalize_layered_route_cost_policy({
        "ground_lambda": 0.0, "air_traffic_lambda": 2.0,
        "environment_obstacle_lambda": 0.0, "source": "工程确认", "confirmed": True,
    })
    assert zero["status"] == "confirmed"
    assert cost_lambdas(zero)["ground"] == 0.0
    assert active_cost_domains(zero) == ("air_traffic",)
    pending = normalize_layered_route_cost_policy({
        "ground_lambda": 0.0, "air_traffic_lambda": None,
        "environment_obstacle_lambda": 0.0, "source": "工程确认", "confirmed": True,
    })
    assert pending["status"] == "pending_confirmation"
    assert pending["status_reason"] == "cost_weights_not_configured"
    assert cost_lambdas(pending)["air_traffic"] is None


def test_negative_lambda_is_rejected():
    with pytest.raises(ValueError, match="有限非负数"):
        normalize_layered_route_cost_policy({"ground_lambda": -1.0})


# --------------------------------------------------------------------------------------
# 3. altitude layer resolution (datum is never guessed)
# --------------------------------------------------------------------------------------


def test_layer_without_a_nominal_or_with_an_unknown_datum_stays_blocked():
    missing = resolve_cruise_altitude({"altitude_layer_id": "L", "status": "confirmed"})
    assert missing["status"] == "blocked"
    assert missing["reason"] == "nominal_altitude_missing"
    unknown = resolve_cruise_altitude({
        "altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 100.0,
        "vertical_reference": "unknown",
    })
    assert unknown["reason"] == "vertical_reference_unknown"
    pending = resolve_cruise_altitude({
        "altitude_layer_id": "L", "status": "pending_confirmation",
        "nominal_altitude_m": 100.0, "vertical_reference": "egm2008_orthometric",
    })
    assert pending["reason"] == "altitude_layer_pending_confirmation"


def test_agl_and_wgs84_layers_block_without_explicit_conversion_evidence():
    agl = resolve_cruise_altitude({
        "altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 100.0,
        "vertical_reference": "agl",
    })
    assert agl["status"] == "blocked"
    assert agl["reason"] == "agl_requires_explicit_dem_surface_elevation"
    converted = resolve_cruise_altitude(
        {"altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 100.0,
         "vertical_reference": "agl"}, surface_elevation_m=12.0,
    )
    assert converted["altitude_egm2008_m"] == 112.0
    wgs84 = resolve_cruise_altitude({
        "altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 100.0,
        "vertical_reference": "wgs84_ellipsoidal",
    })
    assert wgs84["status"] == "blocked"
    assert wgs84["reason"] == "wgs84_ellipsoidal_requires_explicit_geoid_undulation"
    with_undulation = resolve_cruise_altitude(
        {"altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 100.0,
         "vertical_reference": "wgs84_ellipsoidal"}, geoid_undulation_m=2.5,
    )
    assert with_undulation["altitude_egm2008_m"] == 97.5


def test_canonical_layer_resolves_without_any_conversion():
    resolved = resolve_cruise_altitude({
        "altitude_layer_id": "L", "status": "confirmed", "nominal_altitude_m": 120.0,
        "vertical_reference": "egm2008_orthometric",
    })
    assert resolved["status"] == "confirmed"
    assert resolved["altitude_egm2008_m"] == 120.0
    assert resolved["conversion"] == "already_canonical_egm2008_orthometric"


# --------------------------------------------------------------------------------------
# 4. coarse feasibility mask semantics
# --------------------------------------------------------------------------------------


def mask_for(service, facts, *, adapter=None):
    return build_layer_feasibility_mask(
        request=service.state["layered_route_planning_request"],
        cruise_altitude=resolve_cruise_altitude(
            service.state["spatial_3d"]["altitude_layers"][0]
        ),
        cells=facts,
        feasibility_policy=service.state["layered_route_feasibility_policy"],
        building_clearance_policy=service.state["building_clearance_policy"],
        source_audits={}, grid_level=8, adapter=adapter,
    )


def test_unconfirmed_feasibility_policy_makes_every_cell_unknown(tmp_path):
    service = workflow(tmp_path)
    mask = mask_for(service, stub_facts(grid_cells()))
    assert mask["counts"] == {"feasible": 0, "blocked": 0, "unknown": 12}
    assert all(
        cell["reason_code"] == "terrain_clearance_not_confirmed"
        for cell in mask["cells"].values()
    )


def test_missing_terrain_and_no_data_are_unknown_never_zero(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service)
    cells = grid_cells(columns=2, rows=1)
    facts = stub_facts(cells)
    facts[0]["terrain"] = {
        "data_status": "unknown", "surface_elevation_max_egm2008_m": None,
        "reason": "no_valid_dtm_pixel_intersects_this_fine_cell_nodata_is_not_flat_ground",
    }
    mask = mask_for(service, facts)
    assert mask["cells"][cells[0]["grid_id"]]["status"] == "unknown"
    assert mask["cells"][cells[0]["grid_id"]]["reason_code"] == "terrain_data_unavailable"
    assert mask["cells"][cells[0]["grid_id"]]["terrain_floor_egm2008_m"] is None
    assert mask["cells"][cells[1]["grid_id"]]["status"] == "feasible"


def test_altitude_below_the_terrain_floor_is_blocked(tmp_path):
    service = workflow(tmp_path, altitude=300.0)
    confirmed_environment(service, clearance=50.0)
    cells = grid_cells(columns=1, rows=1)
    mask = mask_for(service, stub_facts(cells, elevation=280.0))
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "blocked"
    assert cell["reason_code"] == "altitude_below_terrain_floor"
    assert cell["terrain_floor_egm2008_m"] == 330.0
    assert cell["terrain_margin_m"] == -30.0
    # no clearance default: the floor is terrain max + the explicit clearance only
    assert cell["terrain_vertical_clearance_m"] == 50.0


def test_zero_buildings_means_no_vertical_building_constraint(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service)
    cells = grid_cells(columns=1, rows=1)
    mask = mask_for(service, stub_facts(cells))
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "feasible"
    assert cell["building_count"] == 0
    assert cell["building_required_clearance_egm2008_m"] is None
    assert cell["building_margin_m"] is None


def test_partial_height_coverage_is_unknown_and_never_zero(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service)
    cells = grid_cells(columns=1, rows=1)
    facts = stub_facts(cells, buildings={
        cells[0]["grid_id"]: {
            "data_status": "passed", "building_count": 5, "height_max_m": 40.0,
            "valid_height_fraction": 0.6,
        },
    })
    mask = mask_for(service, facts)
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "unknown"
    assert cell["reason_code"] == "building_height_or_ground_elevation_unresolved"
    assert cell["building_required_clearance_egm2008_m"] is None


def test_missing_building_grid_fact_is_unknown_and_never_zero(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service)
    cells = grid_cells(columns=1, rows=1)
    facts = stub_facts(cells, buildings={
        cells[0]["grid_id"]: {
            "data_status": "unknown", "building_count": None,
            "reason": "building_grid_missing_or_outside_coverage",
        },
    })
    mask = mask_for(service, facts)
    assert mask["cells"][cells[0]["grid_id"]]["status"] == "unknown"
    assert mask["cells"][cells[0]["grid_id"]]["reason_code"] == (
        "building_grid_missing_or_outside_coverage"
    )


def test_building_floor_uses_terrain_max_plus_height_plus_existing_clearance(tmp_path):
    service = workflow(tmp_path, altitude=215.0, lower=0.0, upper=400.0)
    confirmed_environment(service)
    cells = grid_cells(columns=2, rows=1)
    facts = stub_facts(cells, elevation=10.0, buildings={
        cells[0]["grid_id"]: {
            "data_status": "passed", "building_count": 3, "height_max_m": 200.0,
            "valid_height_fraction": 1.0,
        },
        cells[1]["grid_id"]: {
            "data_status": "passed", "building_count": 1, "height_max_m": 50.0,
            "valid_height_fraction": 1.0,
        },
    })
    mask = mask_for(service, facts)
    blocked = mask["cells"][cells[0]["grid_id"]]
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "altitude_below_building_clearance_floor"
    # 10 (terrain max) + 200 (height max) + 10 (existing confirmed vertical clearance)
    assert blocked["building_required_clearance_egm2008_m"] == 220.0
    assert blocked["building_margin_m"] == -5.0
    assert mask["cells"][cells[1]["grid_id"]]["status"] == "feasible"


def test_mask_declares_the_coarse_envelope_semantics_and_keeps_airspace_out():
    mask = default_layer_feasibility_mask("L1")
    assert mask["semantics"]["feasibility_scope"] == "coarse_strategic_vertical_envelope"
    assert mask["semantics"]["not_exact_footprint"] is True
    assert mask["semantics"]["not_horizontal_clearance"] is True
    assert mask["semantics"]["horizontal_clearance_deferred_to_continuous_validation"] is True
    assert mask["semantics"]["airspace"] == "display_only_not_used_for_feasibility"
    assert mask["airspace"]["applicability"] == "display_only"
    assert mask["airspace"]["used_in_mask"] is False


# --------------------------------------------------------------------------------------
# 5. Risk Framework V2 domain indices
# --------------------------------------------------------------------------------------


def test_only_enabled_domains_need_a_resolved_index():
    cells = ["A", "B"]
    risk = {
        "cells": {
            "A": {"domains": {"ground": {"status": "passed", "index": 0.4}}},
            "B": {"domains": {"ground": {"status": "pending_confirmation", "index": None}}},
        },
    }
    resolved, unresolved = resolve_lambda_domain_indices(risk, ("ground",), cells)
    assert resolved["A"] == {"ground": 0.4}
    assert resolved["B"] == {"ground": None}
    assert "B" in unresolved
    # A domain that is not enabled is never required.
    resolved, unresolved = resolve_lambda_domain_indices(risk, (), cells)
    assert resolved == {"A": {}, "B": {}}
    assert unresolved == {}


def test_flat_legacy_cell_schema_is_missing_not_silently_read():
    """The historical flat ``cell[domain_id]`` form is never accepted as canonical."""

    cells = ["A"]
    flat = {"status": "passed", "cells": {
        "A": {"ground": {"status": "passed", "index": 0.4}},
    }}
    resolved, unresolved = resolve_lambda_domain_indices(flat, ("ground",), cells)
    assert resolved["A"]["ground"] is None
    assert unresolved["A"] == ["ground:missing_data"]


def test_overall_risk_v2_is_never_used_as_a_soft_cost():
    cells = ["A"]
    risk = {"cells": {"A": {"overall": {"status": "passed", "index": 0.9}}}}
    resolved, unresolved = resolve_lambda_domain_indices(risk, ("ground",), cells)
    assert resolved["A"]["ground"] is None
    assert unresolved["A"] == ["ground:missing_data"]


# --------------------------------------------------------------------------------------
# 6. A* + cost formula
# --------------------------------------------------------------------------------------


def run_planner(service, facts, *, adapter=None, constraints=None):
    return service.evaluate_layered_route_candidate(
        {"hard_constraints": constraints} if constraints is not None else {},
        adapter=adapter or StubAdapter(facts),
    )["layered_route_candidates"]


def test_lambda_zero_keeps_the_shortest_path_when_every_cell_is_free(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    assert candidate["grid_path"] == [grid[0]["grid_id"], grid[1]["grid_id"]]
    assert candidate["optimization_cost"] == pytest.approx(candidate["distance_m"])
    assert candidate["cost_breakdown"]["active_domains"] == []
    assert candidate["cost_breakdown"]["lambda_weighted_contributions_m"]["ground"] is None


def test_edge_cost_uses_the_mean_of_both_endpoint_domain_indices(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=2.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = {
        "status": "passed", "input_fingerprint": "i", "policy_fingerprint": "p",
        "cells": {
            grid[0]["grid_id"]: {"domains": {"ground": {"status": "passed", "index": 0.2}}},
            grid[1]["grid_id"]: {"domains": {"ground": {"status": "passed", "index": 0.8}}},
        },
    }
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    distance = candidate["distance_m"]
    mean = 0.5
    assert candidate["cost_breakdown"]["mean_domain_index"]["ground"] == pytest.approx(mean, abs=1e-9)
    assert candidate["cost_breakdown"]["domain_exposure_index_m"]["ground"] == pytest.approx(
        distance * mean, rel=1e-9,
    )
    assert candidate["cost_breakdown"]["lambda_weighted_contributions_m"]["ground"] == pytest.approx(
        distance * 2.0 * mean, rel=1e-9,
    )
    assert candidate["optimization_cost"] == pytest.approx(distance * (1 + 2.0 * mean), rel=1e-9)


def test_positive_lambda_reports_no_path_when_a_required_domain_index_is_missing(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=1.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    # no risk_v2 at all: λ_ground > 0 and the index is unresolved
    service.state["grid_risk_v2"] = {"status": "passed", "cells": {}}
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    # A completed search without a traversable path is ``no_path`` — a statement about the
    # current constraint set, never about the airspace being infeasible.
    assert candidate["status"] == "no_path"
    assert candidate["terminal_status_semantics"] == (
        "search_completed_without_a_feasible_path"
    )
    assert candidate["blocking_reasons"][0]["reason_code"] == "no_traversable_path"
    mask = list(collection["masks"].values())[0]
    assert mask["counts"]["blocked"] == 2
    assert all(
        cell["reason_code"] == "risk_domain_unresolved" for cell in mask["cells"].values()
    )


def test_zero_lambda_ignores_the_domain_even_when_the_index_is_missing(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0, air=0.0, environment=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = {"status": "passed", "cells": {}}
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    assert candidate["cost_breakdown"]["active_domains"] == []


def test_blocked_feasible_cells_force_a_detour_and_the_heuristic_stays_pure_distance(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=3, rows=2))
    service.state["grid_risk_v2"] = risk_v2(grid)
    blocked = [
        cell for cell in grid if cell["column"] == 1 and cell["row"] == 0
    ][0]["grid_id"]
    facts = stub_facts(grid, buildings={
        blocked: {
            "data_status": "passed", "building_count": 9, "height_max_m": 900.0,
            "valid_height_fraction": 1.0,
        },
    })
    collection = run_planner(service, facts)
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    assert blocked not in candidate["grid_path"]
    assert candidate["cost_breakdown"]["heuristic"] == (
        "pure_metric_straight_line_distance_admissible_because_all_lambda_nonnegative"
    )
    assert candidate["cost_breakdown"]["risk_v2_overall_used"] is False


def test_search_state_is_a_single_layer_with_no_cross_layer_edge():
    planner = LayeredRoutePlannerV1()
    assert planner.uses_explicit_altitude_layer is True
    assert planner.uses_risk_v2_overall is False
    text = Path("cns_planner/layered_route_planner/planner.py").read_text(encoding="utf-8")
    assert "altitude_index" not in text
    assert "no_cross_layer_edge" in text
    assert "no_free_3d_state" in text


def test_hard_constraints_still_remove_cells_from_the_search(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    # 5x3 grid: the blocked middle column still leaves a detour, so the search (not the
    # feasibility mask) is what has to route around the constraint.
    grid = set_grid(service, grid_cells(columns=5, rows=3))
    service.state["grid_risk_v2"] = risk_v2(grid)
    # An inset box: neighbouring grid edges differ only by floating-point noise, and the
    # planner's bbox intersection is (correctly) a strict positive-area test.
    west, south, east, north = grid[2]["bbox"]
    collection = run_planner(
        service, stub_facts(grid),
        constraints=[{
            "name": "测试硬约束",
            "bbox": [west + 1e-6, south + 1e-6, east - 1e-6, north - 1e-6],
        }],
    )
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    assert grid[2]["grid_id"] not in candidate["grid_path"]
    assert len(candidate["grid_path"]) > 2
    mask = list(collection["masks"].values())[0]
    assert mask["cells"][grid[2]["grid_id"]]["reason_code"] == "hard_constraint"


def test_endpoints_inside_a_hard_constraint_fail_closed(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    collection = run_planner(
        service, stub_facts(grid),
        constraints=[{"name": "覆盖起点", "bbox": list(grid[0]["bbox"])}],
    )
    candidate = collection["items"][-1]
    assert candidate["status"] == "blocked"
    assert candidate["blocking_reasons"][0]["reason_code"] == "endpoint_in_hard_constraint"


def test_expansion_cap_is_reported_as_search_incomplete_not_failed(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=6, rows=6))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.layered_route_planner_service.planner = LayeredRoutePlannerV1(
        {"max_expanded_states": 1}
    )
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    # The expansion cap stopped the search: reachability is unproven, so this is
    # ``search_incomplete`` (resource limited) and NOT ``blocked`` / ``no_path``.
    assert candidate["status"] == "search_incomplete"
    assert candidate["terminal_status_semantics"] == (
        "search_budget_exhausted_reachability_not_proven"
    )
    assert candidate["search_incomplete"] is True
    assert candidate["statistics"]["search_completeness"] == (
        "expansion_cap_reached_optimality_not_proven"
    )
    assert candidate["blocking_reasons"][0]["reason_code"] == "search_budget_exhausted"
    assert candidate["blocking_reasons"][0]["reachability_proven"] is False
    assert candidate["blocking_reasons"][0]["optimality_proven"] is False


def test_candidate_cost_breakdown_lists_every_domain_contribution(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=1.0, air=2.0, environment=3.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid, index=0.25)
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    assert candidate["status"] == "candidate"
    breakdown = candidate["cost_breakdown"]
    assert breakdown["lambdas"] == {"ground": 1.0, "air_traffic": 2.0, "environment_obstacle": 3.0}
    distance = candidate["distance_m"]
    for domain, lam in (("ground", 1.0), ("air_traffic", 2.0), ("environment_obstacle", 3.0)):
        assert breakdown["mean_domain_index"][domain] == pytest.approx(0.25, abs=1e-9)
        assert breakdown["lambda_weighted_contributions_m"][domain] == pytest.approx(
            distance * lam * 0.25, rel=1e-9,
        )
    assert candidate["optimization_cost"] == pytest.approx(
        distance * (1 + 0.25 * 6.0), rel=1e-9,
    )


# --------------------------------------------------------------------------------------
# 7. candidate container contract
# --------------------------------------------------------------------------------------


def test_candidate_is_never_an_operational_route_and_writes_nothing_downstream(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.state["operational_routes"] = [{
        "route_id": "LEGACY", "status": "passed", "path": [[122.0, 30.0], [122.01, 30.01]],
    }]
    before_routes = deepcopy(service.state["operational_routes"])
    before_spatial = deepcopy(service.state["spatial_3d"])
    before_selection = deepcopy(service.state["algorithm_selection"])
    collection = run_planner(service, stub_facts(grid))
    candidate = collection["items"][-1]
    assert candidate["operational_route"] is False
    assert candidate["cns_assessed"] is False
    assert candidate["continuous_validation_required"] is True
    assert candidate["route_operating_layer_created"] is False
    assert candidate["operational_routes_untouched"] is True
    assert service.state["operational_routes"] == before_routes
    assert service.state["spatial_3d"] == before_spatial
    assert service.state["algorithm_selection"] == before_selection
    assert service.state["spatial_3d"]["route_operating_layers"] == []


def test_candidate_normalizer_forces_the_candidate_flags_even_when_payload_lies():
    normalized = normalize_layered_route_candidate({
        "status": "candidate", "route_id": "R1", "altitude_layer_id": "L1",
        "operational_route": True, "cns_assessed": True,
        "continuous_validation_required": False, "route_operating_layer_created": True,
    })
    assert normalized["operational_route"] is False
    assert normalized["cns_assessed"] is False
    assert normalized["continuous_validation_required"] is True
    assert normalized["route_operating_layer_created"] is False


def test_candidate_fingerprint_components_are_closed_and_never_include_airspace():
    planner = LayeredRoutePlannerV1()
    fingerprints = planner.fingerprints(
        request={"altitude_layer_id": "L1", "scenario_route_id": "R1"},
        scenario_route={"route_id": "R1"}, grid={"cells": []}, layer_mask={},
        grid_risk_v2={"input_fingerprint": "i", "policy_fingerprint": "p"},
        cost_policy=normalize_layered_route_cost_policy({
            "ground_lambda": 0.0, "air_traffic_lambda": 0.0,
            "environment_obstacle_lambda": 0.0, "source": "x", "confirmed": True,
        }),
        feasibility_policy=normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 10.0, "source": "x", "confirmed": True,
        }),
        hard_constraints=[], building_clearance_policy={}, source_audits={},
    )
    components = fingerprints["components"]
    assert set(components) == {
        "scenario_route_id", "grid_identity", "altitude_layer_id", "request_fingerprint",
        "hard_constraints", "terrain_source_audit", "building_source_audit",
        "building_grid_source_audit", "building_clearance_policy",
        "risk_framework_v2_input_fingerprint", "risk_framework_v2_policy_fingerprint",
        "cost_policy", "feasibility_policy", "feasibility_mask_fingerprint",
        "planner_version",
    }
    assert "airspace" not in " ".join(components)
    assert "airspace" not in str(components).lower()


def test_candidate_fingerprint_changes_with_every_declared_component():
    planner = LayeredRoutePlannerV1()
    base = dict(
        request={"altitude_layer_id": "L1", "scenario_route_id": "R1"},
        scenario_route={"route_id": "R1"}, grid={"cells": []}, layer_mask={},
        grid_risk_v2={"input_fingerprint": "i", "policy_fingerprint": "p"},
        cost_policy=normalize_layered_route_cost_policy({
            "ground_lambda": 0.0, "air_traffic_lambda": 0.0,
            "environment_obstacle_lambda": 0.0, "source": "x", "confirmed": True,
        }),
        feasibility_policy=normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 10.0, "source": "x", "confirmed": True,
        }),
        hard_constraints=[], building_clearance_policy={}, source_audits={},
    )
    baseline = planner.fingerprints(**base)["candidate_fingerprint"]
    assert planner.fingerprints(**base)["candidate_fingerprint"] == baseline
    changed_risk = planner.fingerprints(
        **{**base, "grid_risk_v2": {"input_fingerprint": "i2", "policy_fingerprint": "p"}}
    )["candidate_fingerprint"]
    assert changed_risk != baseline
    changed_source = planner.fingerprints(
        **{**base, "source_audits": {"items": {"terrain_dtm": {"sha256": "abc"}}}}
    )["candidate_fingerprint"]
    assert changed_source != baseline
    changed_constraints = planner.fingerprints(
        **{**base, "hard_constraints": [{"bbox": [0, 0, 1, 1]}]}
    )["candidate_fingerprint"]
    assert changed_constraints != baseline
    changed_layer = planner.fingerprints(
        **{**base, "request": {"altitude_layer_id": "L2", "scenario_route_id": "R1"}}
    )["candidate_fingerprint"]
    assert changed_layer != baseline


# --------------------------------------------------------------------------------------
# 8. service-level behaviour, readiness and invalidation
# --------------------------------------------------------------------------------------


def test_default_readiness_is_blocked_without_any_default_parameters(tmp_path):
    service = workflow(tmp_path, route=False)
    readiness = service.layered_route_planner_readiness()
    assert readiness["status"] == "blocked"
    codes = {item["reason_code"] for item in readiness["blockers"]}
    assert "route_identity_not_selected" in codes
    assert "terrain_vertical_clearance_not_configured" in codes
    assert "cost_weights_not_configured" in codes
    assert readiness["feasibility_policy"]["terrain_vertical_clearance_m"] is None
    assert all(
        item["lambda"] is None for item in readiness["cost_policy"]["domains"].values()
    )
    assert readiness["airspace"]["used_in_mask_search_or_fingerprint"] is False


def test_readiness_becomes_ready_only_after_every_explicit_confirmation(tmp_path):
    service = workflow(tmp_path)
    configured_sources(service)
    confirmed_environment(service)
    confirmed_request(service)
    readiness = service.layered_route_planner_readiness()
    assert readiness["status"] == "ready"
    assert readiness["altitude_layer_catalog"]["cruise_altitude"]["altitude_egm2008_m"] == LOW_ALTITUDE
    assert readiness["cost_policy"]["active_domains"] == ["ground"]


def test_unconfirmed_policies_block_before_any_source_read(tmp_path):
    service = workflow(tmp_path)
    confirmed_request(service)
    adapter = StubAdapter([])
    collection = service.evaluate_layered_route_candidate({}, adapter=adapter)[
        "layered_route_candidates"
    ]
    assert collection["items"][-1]["status"] == "blocked"
    assert collection["items"][-1]["blocking_reasons"][0]["reason_code"] in (
        "feasibility_policy_not_confirmed", "cost_policy_not_confirmed",
        "terrain_vertical_clearance_not_configured",
    )
    assert adapter.calls == 0


def test_unresolvable_altitude_layer_blocks_before_any_source_read(tmp_path):
    service = workflow(tmp_path, altitude=None)
    confirmed_environment(service)
    confirmed_request(service)
    adapter = StubAdapter([])
    collection = service.evaluate_layered_route_candidate({}, adapter=adapter)[
        "layered_route_candidates"
    ]
    assert collection["items"][-1]["status"] == "blocked"
    assert collection["items"][-1]["blocking_reasons"][0]["reason_code"] == "nominal_altitude_missing"
    assert adapter.calls == 0


def test_missing_adapter_is_reported_instead_of_fabricating_an_environment(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service)
    confirmed_request(service)
    collection = service.evaluate_layered_route_candidate({})["layered_route_candidates"]
    assert collection["items"][-1]["status"] == "missing_data"
    assert collection["items"][-1]["blocking_reasons"][0]["reason_code"] == (
        "feasibility_adapter_not_configured"
    )


def test_risk_v2_change_stales_only_the_layered_candidate(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.state["result_statuses"].update({
        "routes": "passed", "coverage_3d": "passed", "cns_gap_v2": "passed",
        "report": "passed", "grid_risk_v2": "passed",
    })
    service.state["operational_routes"] = [{"route_id": "LEGACY", "status": "passed"}]
    service.state["coverage_3d"] = {"status": "passed"}
    service.state["cns_gap_analysis_v2"] = {"status": "passed"}
    run_planner(service, stub_facts(grid))
    assert service.state["result_statuses"]["layered_route_candidate"] == "passed"
    service.invalidation_service.risk_v2("risk_policy_v2_changed")
    assert service.state["result_statuses"]["layered_route_candidate"] == "stale"
    for untouched in ("routes", "coverage_3d", "cns_gap_v2"):
        assert service.state["result_statuses"][untouched] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"
    assert service.state["coverage_3d"]["status"] == "passed"
    assert service.state["cns_gap_analysis_v2"]["status"] == "passed"


def test_policy_and_request_changes_stale_only_the_layered_candidate(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.state["result_statuses"]["routes"] = "passed"
    run_planner(service, stub_facts(grid))
    service.set_layered_route_cost_policy({
        "ground_lambda": 1.0, "air_traffic_lambda": 0.0,
        "environment_obstacle_lambda": 0.0, "source": "工程确认-测试", "confirmed": True,
    })
    assert service.state["result_statuses"]["layered_route_candidate"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"


def test_altitude_layer_change_stales_the_layered_candidate(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.state["result_statuses"]["routes"] = "passed"
    run_planner(service, stub_facts(grid))
    service.set_altitude_layer(layer(nominal_altitude_m=320.0))
    assert service.state["result_statuses"]["layered_route_candidate"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"


def test_building_source_change_stales_the_layered_candidate_only(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.state["result_statuses"]["routes"] = "passed"
    run_planner(service, stub_facts(grid))
    service.invalidation_service.grid_sources(["building_grid"])
    assert service.state["result_statuses"]["layered_route_candidate"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"


def test_stale_candidate_is_preserved_and_a_new_run_replaces_the_current_one(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    first = run_planner(service, stub_facts(grid))["items"][-1]
    assert first["current_applicability"] == "current"
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 80.0, "source": "工程确认-测试", "confirmed": True,
    })
    collection = run_planner(service, stub_facts(grid))
    statuses = {item["status"] for item in collection["items"]}
    assert "stale" in statuses
    assert collection["items"][-1]["status"] == "candidate"
    assert collection["items"][-1]["current_applicability"] == "current"


def test_rerunning_with_unchanged_inputs_is_idempotent(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    first = run_planner(service, stub_facts(grid))
    second = run_planner(service, stub_facts(grid))
    assert first["count"] == second["count"] == 1
    assert first["items"][0]["candidate_fingerprint"] == second["items"][0]["candidate_fingerprint"]


def test_delete_candidate_removes_only_that_record(tmp_path):
    service = workflow(tmp_path)
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    candidate = run_planner(service, stub_facts(grid))["items"][-1]
    service.delete_layered_route_candidate(candidate["candidate_id"])
    assert service.layered_route_candidates()["count"] == 0
    with pytest.raises(ValueError, match="未找到"):
        service.delete_layered_route_candidate("missing")


# --------------------------------------------------------------------------------------
# 9. project state / persistence / API
# --------------------------------------------------------------------------------------


def test_blank_project_has_pending_layered_containers():
    project = blank_project({})
    assert project["layered_route_planning_request"]["status"] == "pending_confirmation"
    assert project["layered_route_feasibility_policy"]["terrain_vertical_clearance_m"] is None
    assert project["layered_route_cost_policy"]["ground_lambda"] is None
    assert project["layered_route_candidates"] == empty_layered_route_candidate_collection()
    assert project["result_statuses"]["layered_route_candidate"] == "not_calculated"


def test_legacy_project_backfills_the_layered_containers_as_a_fixed_point():
    project = blank_project({})
    for key in (
        "layered_route_planning_request", "layered_route_feasibility_policy",
        "layered_route_cost_policy", "layered_route_candidates",
    ):
        project.pop(key)
    project["result_statuses"].pop("layered_route_candidate")
    normalized = normalize_project(project, WorkspaceGridService())
    assert normalized["layered_route_planning_request"] == default_layered_route_request()
    assert normalized["layered_route_feasibility_policy"] == (
        default_layered_route_feasibility_policy()
    )
    assert normalized["layered_route_cost_policy"] == default_layered_route_cost_policy()
    assert normalized["layered_route_candidates"] == empty_layered_route_candidate_collection()
    assert normalized["result_statuses"]["layered_route_candidate"] == "not_calculated"
    # backfilling is an idempotent fixed point
    assert normalize_project(deepcopy(normalized), WorkspaceGridService())[
        "layered_route_candidates"
    ] == normalized["layered_route_candidates"]


def test_layered_containers_survive_save_and_restore(tmp_path):
    service = workflow(tmp_path, name="layer.json")
    confirmed_environment(service, ground=0.0)
    confirmed_request(service)
    grid = set_grid(service, grid_cells(columns=2, rows=1))
    service.state["grid_risk_v2"] = risk_v2(grid)
    run_planner(service, stub_facts(grid))
    service.save()
    restored = WorkflowService(tmp_path / "layer.json", DEFAULTS)
    assert restored.state["layered_route_planning_request"]["status"] == "confirmed"
    assert restored.state["layered_route_cost_policy"]["ground_lambda"] == 0.0
    collection = restored.layered_route_candidates()
    assert collection["count"] == 1
    assert collection["items"][0]["status"] == "candidate"
    assert collection["items"][0]["current_applicability"] == "current"


def test_candidate_collection_normalizer_is_idempotent():
    collection = normalize_layered_route_candidate_collection(
        empty_layered_route_candidate_collection()
    )
    assert collection == normalize_layered_route_candidate_collection(collection)


def test_layered_planner_has_its_own_algorithm_type_and_never_becomes_the_route_planner():
    selection = default_algorithm_selection()
    assert selection["route_planner"]["algorithm_id"] == "route_planner_v1"
    # The layered default is now the production Theta* V2 baseline; V1 stays registered and
    # explicitly selectable as the legacy/baseline layered planner.
    assert selection["layered_route_planner"]["algorithm_id"] == (
        "layered_risk_aware_theta_star_v2"
    )
    assert selection["layered_route_planner"]["version"] == "2.0"
    registry = build_default_algorithm_registry({})
    manifests = {
        (item.algorithm_type, item.algorithm_id, item.version) for item in registry.manifests()
    }
    assert ("layered_route_planner", "layered_route_planner_v1", "1.0") in manifests
    assert ("layered_route_planner", "layered_risk_aware_theta_star_v2", "2.0") in manifests
    # Neither layered planner can ever be selected as the project's ``route_planner``.
    assert ("route_planner", "layered_route_planner_v1", "1.0") not in manifests
    assert ("route_planner", "layered_risk_aware_theta_star_v2", "2.0") not in manifests


class ApiWorkflow:
    def layered_route_planner_readiness(self): return {"status": "blocked"}
    def layered_route_planning_request(self): return {"status": "pending_confirmation"}
    def layered_route_feasibility_policy(self): return {"terrain_vertical_clearance_m": None}
    def layered_route_cost_policy(self): return {"ground_lambda": None}
    def layered_route_candidates(self): return {"count": 0}
    def set_layered_route_planning_request(self, payload): return {"request": payload}
    def set_layered_route_feasibility_policy(self, payload): return {"feasibility": payload}
    def set_layered_route_cost_policy(self, payload): return {"cost": payload}
    def evaluate_layered_route_candidate(self, payload): return {"payload": payload}
    def delete_layered_route_candidate(self, candidate_id): return {"deleted": candidate_id}

    def __getattr__(self, name):
        # The router builds every action lambda eagerly, so unknown workflow methods must
        # resolve to an inert callable instead of breaking the additive layer.
        return lambda *args, **kwargs: {"unsupported": name}


class ApiContext:
    workflow = ApiWorkflow()
    data = object()


def test_layered_api_routes_are_additive_and_forward_the_payload():
    router = ApiRouter(ApiContext())
    assert router.get("/api/layered-route-planner/readiness", {}, {}).data == {"status": "blocked"}
    assert router.get("/api/layered-route-planning-request", {}, {}).data["status"] == (
        "pending_confirmation"
    )
    assert router.get("/api/layered-route-candidates", {}, {}).data == {"count": 0}
    payload = {"scenario_route_id": "R1"}
    assert router.post("/api/layered-route-planning-request", payload).data == {"request": payload}
    assert router.post("/api/layered-route-candidates/delete", {"candidate_id": "C1"}).data == {
        "deleted": "C1",
    }


# --------------------------------------------------------------------------------------
# 10. GIS boundary adapter
# --------------------------------------------------------------------------------------


def test_adapter_reuses_the_fabdem_window_sampler_and_the_l8_building_facts():
    grid = grid_cells(columns=2, rows=1)
    source = StubTerrainSource({grid[0]["grid_id"]: 12.0, grid[1]["grid_id"]: None})
    adapter = LayeredFeasibilityAdapter(source)
    state = {
        "grid_attributes": {"buildings": {"cells": {
            grid[0]["grid_id"]: {
                "status": "passed", "building_count": 2, "height_max_m": 30.0,
                "valid_height_fraction": 1.0, "building_coverage_ratio": 0.2,
            },
            grid[1]["grid_id"]: {"status": "missing_data", "building_count": None},
        }}},
    }
    facts = adapter.build_cells(grid, state)
    assert source.calls == 1
    assert facts[0]["terrain"]["surface_elevation_max_egm2008_m"] == 12.0
    assert facts[0]["buildings"]["building_count"] == 2
    assert facts[1]["terrain"]["data_status"] == "unknown"
    assert facts[1]["buildings"]["building_count"] is None
    described = adapter.describe()
    assert described["terrain_mapping_method"] == LAYERED_TERRAIN_MAPPING_METHOD
    assert described["building_mapping_method"] == LAYERED_BUILDING_MAPPING_METHOD
    assert described["not_exact_footprint"] is True


def test_adapter_reports_unknown_when_the_dtm_datum_is_unresolved():
    grid = grid_cells(columns=1, rows=1)
    source = StubTerrainSource({}, available=False)
    adapter = LayeredFeasibilityAdapter(source)
    facts = adapter.build_cells(grid, {})
    assert facts[0]["terrain"]["data_status"] == "unknown"
    assert facts[0]["terrain"]["surface_elevation_max_egm2008_m"] is None
    assert source.calls == 0


def test_layered_source_status_marks_airspace_display_only():
    status = layered_feasibility_source_status({"source_audits": {"items": {}}}, "dtm.tif")
    assert status["airspace"]["applicability"] == "display_only"
    assert status["airspace"]["used_in_feasibility"] is False
    assert status["terrain_dtm"]["configured"] is True


def test_algorithm_package_never_reads_files_or_gis_libraries():
    root = Path("cns_planner/layered_route_planner")
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("*.py")}
    assert sources
    for name, text in sources.items():
        assert "osgeo" not in text and "import gdal" not in text, name
        assert "from qgis" not in text, name
        assert "open(" not in text, name
        assert "Path(" not in text, name
        assert "route_operating_layers" not in text, name
