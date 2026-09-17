"""Route Planner V3-A: contracts, motion kernel, hard constraints and synthetic cases."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.algorithms.registry import default_algorithm_selection
from cns_planner.algorithms.route_planner import RoutePlannerV1
from cns_planner.route_planner_v3 import (
    HardConstraintEvaluator, MotionPrimitiveProvider, SoftCostModel, TransitionValidator,
    V3StrategicPlanner, build_candidate_refinement_corridor, build_v3_state,
    default_v3_cost_model, default_v3_policy, describe_altitude_states,
    empty_v3_cell_environment, heading_bin_for_bearing, normalize_aircraft_motion_limits,
    normalize_candidate_refinement_corridor, normalize_cost_model,
    normalize_v3_cell_environment, normalize_v3_planning_policy,
    normalize_v3_planning_problem, normalize_v3_strategic_result, ring_neighbors,
    v3_state_id,
)
from cns_planner.route_planner_v3.contracts import V3_RESULT_STATUSES
from cns_planner.route_planner_v3.synthetic import build_synthetic_environment

#: L8 MH/T grid over the 0.02 deg x 0.02 deg synthetic workspace (18 x 18 = 324 cells).
WORKSPACE = [122.0, 29.9, 122.02, 29.92]
START = [122.0005, 29.9005]
GOAL = [122.0195, 29.9195]

_GRID_CACHE = {}


def indexed_cells():
    """Canonical L8 cells carrying the derived Chebyshev index used by every helper."""

    if "indexed" not in _GRID_CACHE:
        from cns_planner.route_planner_v3.motion import derive_grid_index

        raw = WorkspaceGridService().generate(list(WORKSPACE), 8)["cells"]
        index = derive_grid_index({str(cell["grid_id"]): cell for cell in raw})
        enriched = []
        for cell in raw:
            level, column, row = index[str(cell["grid_id"])]
            enriched.append({**cell, "level": level, "column": column, "row": row})
        _GRID_CACHE["indexed"] = enriched
    return _GRID_CACHE["indexed"]


def grid_cells():
    return indexed_cells()


def group(**selectors):
    """Grid ids matching a column/row band selector (declarative, no magic numbers)."""

    result = []
    for cell in grid_cells():
        if "column" in selectors and int(cell["column"]) != int(selectors["column"]):
            continue
        if "row" in selectors and int(cell["row"]) != int(selectors["row"]):
            continue
        if "columns" in selectors and int(cell["column"]) not in set(selectors["columns"]):
            continue
        if "rows" in selectors and int(cell["row"]) not in set(selectors["rows"]):
            continue
        result.append(cell["grid_id"])
    return result


def index_map(value, **selectors):
    """A *complete* normalized index map: ``value`` on the selected band, 0 elsewhere.

    A complete map matters: since the planner has no hidden normalizer, an enabled
    channel whose index is missing on any cell blocks the run.  A cell explicitly
    mapped to ``0.0`` with provenance is a measured zero and is therefore allowed.
    """

    band = set(group(**selectors))
    return {
        cell["grid_id"]: (float(value) if cell["grid_id"] in band else 0.0)
        for cell in grid_cells()
    }


def policy(**overrides):
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
        "source": "unit_test_explicit_policy",
    })
    base.update(overrides)
    return base


def environment(spec=None, policy_value=None):
    return build_synthetic_environment(
        grid_cells(), policy_value or policy(), spec or {"profile_id": "open"},
    )


def problem(spec=None, *, start=None, goal=None, policy_value=None, provenance=None,
            environment_policy=None):
    resolved = policy_value or policy()
    # The environment's own clearance channels come from explicit values; a policy
    # under test (for example with a deliberately missing parameter) must not stop
    # the environment from being constructible, otherwise "not_ready" could never
    # be observed.
    return normalize_v3_planning_problem({
        "problem_id": "unit",
        "route_id": "R-TEST",
        "start": start or START,
        "goal": goal or GOAL,
        "policy": normalize_v3_planning_policy(resolved),
        "aircraft_motion_limits": normalize_aircraft_motion_limits({
            "aircraft_id": "SYN", "confirmed": True, "source": "unit_test",
        }),
        "environment": environment(spec, environment_policy or policy()),
        "provenance": provenance or {},
    })


def plan(spec=None, **kwargs):
    return V3StrategicPlanner().plan(problem(spec, **kwargs))


OPEN_FLAT = {"profile_id": "open_flat", "terrain_profile": "flat"}


# --------------------------------------------------------------------------------------
# contracts: JSON-safety, versioning, no defaulted safety parameters
# --------------------------------------------------------------------------------------


def test_all_contracts_are_json_safe_and_versioned():
    normalized_problem = problem()
    result = V3StrategicPlanner().plan(normalized_problem)
    for value in (normalized_problem, result):
        json.dumps(value, allow_nan=False)
    assert normalized_problem["schema_version"] == "3.0-state"
    assert normalized_problem["policy"]["schema_version"] == "3.0-policy"
    assert normalized_problem["environment"]["schema_version"] == "3.0-environment"
    assert result["schema_version"] == "3.0-strategic-result"
    assert result["cost_vector"]["schema_version"] == "3.0-cost-vector"


def test_search_state_carries_grid_altitude_and_heading():
    state = build_v3_state("MHT-X", 3, 250.0, 5, 122.0, 29.9)
    assert state["grid_id"] == "MHT-X"
    assert state["altitude_index"] == 3
    assert state["altitude_egm2008_m"] == 250.0
    assert state["heading_bin"] == 5
    assert state["z"] == state["altitude_egm2008_m"] == 250.0
    assert v3_state_id("MHT-X", 3, 5) == "MHT-X|3|5"


def test_policy_has_no_default_safety_parameters_and_reports_what_is_missing():
    empty = normalize_v3_planning_policy(None)
    for field in (
        "min_altitude_egm2008_m", "max_altitude_egm2008_m", "vertical_step_m",
        "terrain_clearance_m", "building_horizontal_clearance_m",
        "building_vertical_clearance_m", "aircraft_min_turn_radius_m",
        "max_climb_gradient", "max_descent_gradient", "planning_speed_mps",
    ):
        assert empty[field] is None, field
    assert empty["confirmed"] is False
    assert empty["status"] == "pending_confirmation"
    assert set(empty["missing_parameters"]) >= {
        "min_altitude_egm2008_m", "max_altitude_egm2008_m", "vertical_step_m",
        "terrain_clearance_m", "building_horizontal_clearance_m",
        "building_vertical_clearance_m", "aircraft_min_turn_radius_m",
    }
    assert empty["heading_bin_count"] == 8
    assert empty["source"]


@pytest.mark.parametrize("payload", [
    {"max_altitude_egm2008_m": 10.0, "min_altitude_egm2008_m": 100.0},
    {"vertical_step_m": 0.0},
    {"vertical_step_m": -5.0},
    {"heading_bin_count": 7},
    {"max_climb_gradient": -0.1},
    {"planning_speed_mps": 0.0},
    {"building_vertical_clearance_m": -1.0},
])
def test_invalid_policy_values_are_rejected(payload):
    with pytest.raises(ValueError):
        normalize_v3_planning_policy({**policy(), **payload})


def test_missing_or_unconfirmed_policy_never_runs_the_search():
    incomplete = policy()
    incomplete.pop("terrain_clearance_m")
    result = plan(OPEN_FLAT, policy_value=incomplete)
    assert result["status"] == "not_ready"
    assert result["state_path"] == []
    assert result["distance_m"] is None
    assert result["readiness"]["policy"]["status"] == "blocked"
    assert result["readiness"]["policy"]["missing_parameters"] == ["terrain_clearance_m"]
    assert any("terrain_clearance_m" in reason for reason in result["readiness"]["policy"]["reasons"])
    assert result["search_statistics"]["expanded_states"] == 0

    pending = plan(OPEN_FLAT, policy_value=policy(confirmed=False))
    assert pending["status"] == "not_ready"
    assert pending["readiness"]["policy"]["status"] == "pending"
    assert pending["search_statistics"]["expanded_states"] == 0


def test_altitude_discretisation_requires_explicit_bounds_and_step():
    assert describe_altitude_states(normalize_v3_planning_policy(None)) == []
    states = describe_altitude_states(normalize_v3_planning_policy(policy()))
    assert [item["altitude_index"] for item in states] == [0, 1, 2, 3, 4, 5, 6]
    assert states[0]["altitude_egm2008_m"] == 100.0
    assert states[-1]["altitude_egm2008_m"] == 400.0
    assert all(item["vertical_reference"] == "egm2008_orthometric" for item in states)


def test_rate_based_climb_is_only_convertible_with_an_explicit_planning_speed():
    without_speed = policy(
        max_climb_gradient=None, max_descent_gradient=None,
        max_climb_rate_mps=5.0, max_descent_rate_mps=5.0, planning_speed_mps=None,
    )
    blocked = plan(OPEN_FLAT, policy_value=without_speed)
    assert blocked["status"] == "not_ready"
    aircraft = blocked["readiness"]["aircraft"]
    assert aircraft["status"] == "blocked"
    assert aircraft["climb_gradient"] is None
    assert aircraft["descent_gradient"] is None
    assert aircraft["planning_speed_source"] == "unresolved_no_planning_speed_is_ever_guessed"
    assert any("planning_speed" in reason for reason in aircraft["reasons"])

    with_speed = policy(
        max_climb_gradient=None, max_descent_gradient=None,
        max_climb_rate_mps=5.0, max_descent_rate_mps=2.5, planning_speed_mps=25.0,
    )
    converted = plan(OPEN_FLAT, policy_value=with_speed)
    assert converted["status"] == "strategic_candidate"
    aircraft = converted["readiness"]["aircraft"]
    assert aircraft["climb_gradient"] == pytest.approx(0.2)
    assert aircraft["descent_gradient"] == pytest.approx(0.1)
    assert "planning_speed_mps" in aircraft["climb_gradient_source"]
    assert converted["effective_policy"]["max_climb_gradient"] is None


def test_cost_model_requires_an_explicit_non_negative_weight_to_enable_a_component():
    with pytest.raises(ValueError):
        normalize_cost_model({"components": {"population_risk": {"enabled": True}}})
    with pytest.raises(ValueError):
        normalize_cost_model({"components": {"traffic_risk": {"enabled": True, "weight": -1.0}}})
    enabled = normalize_cost_model({"components": {"population_risk": {"enabled": True, "weight": 0.25}}})
    assert enabled["components"]["population_risk"]["enabled"] is True
    assert enabled["components"]["population_risk"]["weight"] == 0.25
    assert enabled["components"]["population_risk"]["normalization"]
    assert enabled["components"]["population_risk"]["source"] == "policy.cost_model"


def test_energy_can_never_be_enabled_and_stays_pending_model():
    model = normalize_cost_model({"components": {"energy": {"enabled": True, "weight": 1.0}}})
    assert model["components"]["energy"]["enabled"] is False
    assert model["components"]["energy"]["weight"] is None
    assert model["components"]["energy"]["status"] == "pending_model"
    default = default_v3_cost_model()
    assert default["components"]["energy"]["semantics"] == "pending_model_disabled"


def test_cns_is_recorded_as_post_route_assessment_and_excluded_from_search_cost():
    result = plan()
    assert result["cost_vector"]["cns_integration"]["integration_mode"] == "post_route_assessment"
    assert result["cost_vector"]["cns_integration"]["excluded_from_search_cost"] is True
    assert result["cns_assessment"]["integration_mode"] == "post_route_assessment"
    assert result["cns_assessment"]["excluded_from_search_cost"] is True
    assert result["cns_assessment"]["evaluated"] is False
    assert result["cns_assessment"]["next_stage"] == (
        "V3-D_validated_route_operational_adapter_and_cns_assessment"
    )


def test_cost_vector_is_a_vector_with_per_component_provenance():
    result = plan()
    components = result["cost_vector"]["components"]
    assert list(components) == [
        "distance", "population_risk", "traffic_risk", "building_exposure", "energy",
    ]
    for name, entry in components.items():
        for field in ("raw", "normalized", "weight", "contribution", "unit", "source", "semantics"):
            assert field in entry, (name, field)
    assert components["distance"]["unit"] == "m"
    assert components["distance"]["weight"] == 1.0
    assert components["distance"]["raw"] == pytest.approx(result["distance_m"])
    assert components["population_risk"]["enabled"] is False
    assert components["population_risk"]["status"] == "disabled_no_explicit_weight"
    assert "energy" in result["cost_vector"]["excluded_components"]
    assert result["cost_vector"]["total_raw_is_not_a_sum_of_units"] is True


def test_environment_contract_rejects_unknown_vertical_reference_and_bad_cells():
    def cell(**overrides):
        return {"grid_id": "A", "center": [1.0, 2.0], **overrides}

    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"canonical_vertical_reference": "agl"})
    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"cells": [cell(terrain={"data_status": "maybe"})]})
    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"cells": [cell(center=[1.0, float("nan")])]})
    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"cells": [cell(airspace={"status": "probably_allowed"})]})
    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"cells": [cell(terrain={"data_status": "maybe"})]})
    with pytest.raises(ValueError):
        normalize_v3_cell_environment({"cells": [{"center": [1.0, 2.0]}]})
    empty = normalize_v3_cell_environment(None)
    assert empty["status"] == "missing_data"
    assert empty_v3_cell_environment()["canonical_vertical_reference"] == "egm2008_orthometric"


def test_environment_carries_the_grid_index_so_topology_is_not_guessed():
    cell = environment()["cells"][0]
    assert cell["column"] is not None and cell["row"] is not None
    assert cell["level"] is not None
    assert len(cell["bbox"]) == 4


def test_result_normalizer_forces_experimental_semantics():
    normalized = normalize_v3_strategic_result({"status": "final_safe", "operational_route": True})
    assert normalized["status"] == "not_ready"
    assert normalized["operational_route"] is False
    assert normalized["final_validation_performed"] is False
    assert "V3-A strategic candidate" in normalized["disclaimer"]


def test_corridor_contract_keeps_its_semantics_under_normalization():
    corridor = normalize_candidate_refinement_corridor({
        "ring_n": 2, "semantics": "safety_corridor", "n_ring_is_not_a_safety_clearance": False,
    })
    assert corridor["semantics"] == "refinement_search_window_not_safety_corridor"
    assert corridor["n_ring_is_not_a_safety_clearance"] is True
    assert corridor["altitude_envelope"]["semantics"] == "planned_altitude_extent_not_a_clearance_volume"


# --------------------------------------------------------------------------------------
# motion primitives and transition validation
# --------------------------------------------------------------------------------------


def test_motion_primitive_provider_covers_horizontal_vertical_and_combined_moves():
    provider = MotionPrimitiveProvider(environment(), 8)
    primitives = provider.for_state(grid_cells()[0]["grid_id"], 0)
    assert {item["kind"] for item in primitives} == {"horizontal_level", "climb", "descend"}
    horizontal = [item for item in primitives if item["kind"] == "horizontal_level"]
    assert len(horizontal) == 8
    assert len({tuple(item["grid_delta"]) for item in horizontal}) == 8
    combined = [item for item in primitives if item["kind"] != "horizontal_level"]
    assert len(combined) == 18
    assert {item["altitude_delta_steps"] for item in combined} == {-1, 1}
    for item in primitives:
        assert item["primitive_id"]
        assert isinstance(item["grid_delta"], list) and len(item["grid_delta"]) == 2


def test_heading_bin_is_derived_from_movement_geometry_never_declared():
    provider = MotionPrimitiveProvider(environment(), 8)
    start = next(cell for cell in grid_cells() if cell["column"] == 1 and cell["row"] == 1)["grid_id"]
    east = next(
        item for item in provider.neighbors(start)
        if provider.bearing_deg(start, item) == pytest.approx(90.0, abs=0.5)
    )
    arrival = provider.arrival_heading_bin(start, east)
    assert arrival == heading_bin_for_bearing(provider.bearing_deg(start, east), 8)
    # A straight continuation from the heading that edge established needs no turn.
    assert provider.required_heading_change_deg(start, east, arrival) == pytest.approx(0.0, abs=1e-3)
    # Starting from heading bin 0 (north) the same edge is a real ~90 deg turn.
    assert provider.required_heading_change_deg(start, east, 0) == pytest.approx(90.0, abs=0.5)
    assert provider.arrival_heading_bin(None, east) is None


def test_transition_validator_enforces_turn_radius_and_gradients():
    validator = TransitionValidator(100.0, 0.5, 0.4)
    assert validator.evaluate(
        horizontal_step_m=100.0, vertical_step_m=0.0, heading_change_deg=0.0,
        kind="horizontal_level",
    ) == (True, None)
    assert validator.evaluate(
        horizontal_step_m=100.0, vertical_step_m=0.0, heading_change_deg=90.0,
        kind="horizontal_level",
    ) == (False, "turn_radius_exceeded")
    assert validator.evaluate(
        horizontal_step_m=100.0, vertical_step_m=60.0, heading_change_deg=0.0, kind="climb",
    ) == (False, "climb_gradient_exceeded")
    assert validator.evaluate(
        horizontal_step_m=100.0, vertical_step_m=50.0, heading_change_deg=0.0, kind="descend",
    ) == (False, "descent_gradient_exceeded")
    assert validator.evaluate(
        horizontal_step_m=0.0, vertical_step_m=50.0, heading_change_deg=0.0, kind="climb",
    ) == (False, "vertical_only_transition_not_modelled")
    assert validator.evaluate(
        horizontal_step_m=0.0, vertical_step_m=0.0, heading_change_deg=10.0, kind="horizontal_level",
    ) == (False, "heading_change_without_horizontal_motion")
    assert TransitionValidator(None, 0.5, 0.5).evaluate(
        horizontal_step_m=100.0, vertical_step_m=0.0, heading_change_deg=10.0,
        kind="horizontal_level",
    ) == (False, "turn_capability_unknown")


def test_gradient_limits_are_source_labelled_in_readiness():
    result = plan(OPEN_FLAT)
    aircraft = result["readiness"]["aircraft"]
    assert aircraft["climb_gradient"] == pytest.approx(0.5)
    assert aircraft["climb_gradient_source"] == "policy_direct_climb_gradient"
    assert aircraft["planning_speed_source"] == "policy"


# --------------------------------------------------------------------------------------
# hard-constraint evaluator: state and transition separation, fail-closed
# --------------------------------------------------------------------------------------


def evaluator(spec=None, policy_value=None):
    resolved = policy_value or policy()
    env = environment(spec, resolved)
    return HardConstraintEvaluator(
        env, normalize_v3_planning_policy(resolved), climb_gradient=0.5, descent_gradient=0.5,
    ), env


def test_state_feasibility_rejects_airspace_altitude_terrain_and_building_separately():
    restricted = group(column=9, rows=[0, 1])
    buildings = group(column=9, rows=[2, 3])
    spec = {
        "profile_id": "mixed",
        "terrain_profile": "flat",
        "base_surface_elevation_m": 120.0,
        "restricted_cells": restricted,
        "building_cells": buildings,
        "building_height_m": 200.0,
    }
    constraints, _ = evaluator(spec)
    assert constraints.state_feasible(restricted[0], 3, 250.0) == (False, "airspace_not_confirmed_allowed")
    assert constraints.state_feasible(buildings[0], 2, 200.0) == (False, "below_building_clearance")
    other = group(column=3, row=0)[0]
    assert constraints.state_feasible(other, 0, 100.0) == (False, "below_terrain_clearance")
    assert constraints.state_feasible(other, 3, 250.0) == (True, None)
    assert constraints.state_feasible(other, 6, 400.0) == (True, None)
    assert constraints.state_feasible(other, 0, 90.0) == (False, "altitude_below_min")
    assert constraints.state_feasible(other, 7, 450.0) == (False, "altitude_above_max")
    assert constraints.state_feasible("MHT-MISSING", 0, 100.0) == (False, "cell_not_in_environment")


def test_transition_feasibility_records_its_reason_and_never_guesses_capability():
    constraints, _ = evaluator()
    assert constraints.transition_feasible(
        source_grid_id="A", target_grid_id="B", source_altitude_index=0, target_altitude_index=1,
        horizontal_step_m=100.0, vertical_step_m=50.0, heading_change_deg=0.0, kind="climb",
    ) == (True, None)
    assert constraints.transition_feasible(
        source_grid_id="A", target_grid_id="B", source_altitude_index=0, target_altitude_index=2,
        horizontal_step_m=100.0, vertical_step_m=100.0, heading_change_deg=0.0, kind="climb",
    ) == (False, "altitude_step_not_single_band")
    assert constraints.transition_feasible(
        source_grid_id="A", target_grid_id="B", source_altitude_index=0, target_altitude_index=0,
        horizontal_step_m=100.0, vertical_step_m=0.0, heading_change_deg=45.0,
        kind="horizontal_level", neighbor=False,
    ) == (False, "neighbor_not_adjacent")
    unknown = HardConstraintEvaluator(
        environment(), normalize_v3_planning_policy(policy()),
        climb_gradient=None, descent_gradient=None,
    )
    assert unknown.transition_feasible(
        source_grid_id="A", target_grid_id="B", source_altitude_index=0, target_altitude_index=1,
        horizontal_step_m=100.0, vertical_step_m=50.0, heading_change_deg=0.0, kind="climb",
    ) == (False, "climb_capability_unknown")


def test_unknown_terrain_or_building_evidence_is_never_feasible():
    constraints, _ = evaluator({"profile_id": "u1", "unknown_terrain_cells": group(column=4)})
    assert constraints.state_feasible(group(column=4, row=0)[0], 3, 250.0) == (
        False, "terrain_clearance_unresolved",
    )
    patched = deepcopy(environment(OPEN_FLAT))
    for cell in patched["cells"]:
        if cell["grid_id"] == group(column=4, row=0)[0]:
            cell["buildings"]["data_status"] = "unknown"
    building = HardConstraintEvaluator(
        patched, normalize_v3_planning_policy(policy()), climb_gradient=0.5, descent_gradient=0.5,
    )
    assert building.state_feasible(group(column=4, row=0)[0], 3, 250.0) == (
        False, "building_clearance_unresolved",
    )


# --------------------------------------------------------------------------------------
# synthetic scenarios
# --------------------------------------------------------------------------------------


def test_case_open_3d_produces_a_strategic_candidate_with_a_full_3d_path():
    result = plan(OPEN_FLAT)
    assert result["status"] == "strategic_candidate"
    assert len(result["state_path"]) >= 10
    assert all(record["vertical_reference"] == "egm2008_orthometric" for record in result["state_path"])
    assert all(record["heading_deg"] is not None for record in result["state_path"])
    assert all(record["primitive_id"] for record in result["state_path"][:-1])
    assert all(record["climb_gradient"] is not None for record in result["state_path"][:-1])
    assert result["distance_m"] > 0
    assert result["search_statistics"]["expanded_states"] > 0
    assert result["horizontal_projection"][0] == START
    assert result["horizontal_projection"][-1] == GOAL
    trajectory = result["trajectory_summary"]
    assert trajectory["state_count"] == len(result["state_path"])
    assert trajectory["edge_count"] == len(result["state_path"]) - 1
    assert trajectory["endpoint_grid_binding"] == "nearest_search_cell_center"


PLATEAU = {
    "profile_id": "plateau", "terrain_profile": "longitude_bands",
    "longitude_bands": [[122.0, 122.0075, 0.0], [122.0075, 122.02, 150.0]],
}


def test_case_required_climb_climbs_above_the_new_terrain_floor():
    result = plan(PLATEAU)
    assert result["status"] == "strategic_candidate"
    altitudes = [record["altitude_egm2008_m"] for record in result["state_path"]]
    assert max(altitudes) >= 200.0, altitudes
    climbs = [
        record for record in result["state_path"][:-1]
        if record["climb_gradient"] and record["climb_gradient"] > 0
    ]
    assert climbs, "required climb 必须真实出现爬升边"
    assert all(record["climb_gradient"] <= 0.5 + 1e-9 for record in climbs)
    assert result["hard_constraint_summary"]["state_rejections"].get("below_terrain_clearance", 0) > 0
    assert result["trajectory_summary"]["climb_edge_count"] >= 1
    # Every state over the terrace stays above that terrace's clearance floor.
    required = {
        cell["grid_id"]: cell["terrain"]["surface_clearance_egm2008_m"]
        for cell in environment(PLATEAU)["cells"]
    }
    for record in result["state_path"]:
        assert record["altitude_egm2008_m"] >= required[record["grid_id"]] - 1e-9


def test_case_terrain_barrier_above_the_ceiling_is_failed():
    result = plan({
        "profile_id": "wall", "terrain_profile": "ridge_longitude",
        "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 600.0,
    })
    assert result["status"] == "failed"
    assert result["state_path"] == []
    assert result["hard_constraint_summary"]["state_rejections"].get("below_terrain_clearance", 0) > 0
    assert result["final_validation_performed"] is False


def test_case_building_obstacle_is_never_flown_through():
    spec = {
        "profile_id": "buildings", "terrain_profile": "flat",
        "buildings_profile": "cluster", "building_height_m": 320.0, "building_count": 40,
    }
    result = plan(spec)
    rejected = result["hard_constraint_summary"]["state_rejections"]
    assert rejected.get("below_building_clearance", 0) > 0
    assert result["status"] in ("strategic_candidate", "failed")
    if result["status"] == "strategic_candidate":
        required = {
            cell["grid_id"]: cell["buildings"]["required_clearance_egm2008_m"]
            for cell in environment(spec)["cells"]
            if cell["buildings"]["required_clearance_egm2008_m"] is not None
        }
        assert required
        for record in result["state_path"]:
            if record["grid_id"] in required:
                assert record["altitude_egm2008_m"] >= required[record["grid_id"]] - 1e-9


def test_case_altitude_ceiling_floor_and_endpoint_band_are_enforced():
    # A ceiling below the terrain clearance floor leaves no usable altitude.
    blocked_by_ceiling = plan(
        {"profile_id": "ceiling", "terrain_profile": "flat", "base_surface_elevation_m": 200.0},
        policy_value=policy(max_altitude_egm2008_m=200.0, vertical_step_m=20.0),
    )
    assert blocked_by_ceiling["status"] == "failed"
    assert blocked_by_ceiling["state_path"] == []
    assert blocked_by_ceiling["hard_constraint_summary"]["state_rejections"].get(
        "below_terrain_clearance", 0
    ) > 0

    # A ceiling below the policy's own band is a policy error, not a plan.
    with pytest.raises(ValueError):
        normalize_v3_planning_policy(policy(min_altitude_egm2008_m=200.0, max_altitude_egm2008_m=150.0))

    high_ground = plan({
        "profile_id": "high", "terrain_profile": "flat", "base_surface_elevation_m": 380.0,
    })
    assert high_ground["status"] == "failed"
    assert high_ground["hard_constraint_summary"]["state_rejections"].get("below_terrain_clearance", 0) > 0

    endpoint = plan(OPEN_FLAT, start=[START[0], START[1], 900.0])
    assert endpoint["status"] == "failed"
    assert "超出 policy 高度范围" in endpoint["reason"]

    with pytest.raises(ValueError):
        normalize_v3_planning_policy(policy(vertical_step_m=350.0))


def test_case_turn_radius_rejection_blocks_every_heading_change():
    # An effectively infinite turn radius forbids any bearing change, so the only
    # edge that can be used is the one already aligned from the start cell.  The OD
    # is unreachable under that restriction, and the rejection is recorded.
    strict = policy(aircraft_min_turn_radius_m=5000.0)
    blocked = plan(OPEN_FLAT, policy_value=strict)
    assert blocked["status"] == "failed"
    assert blocked["state_path"] == []
    rejections = blocked["hard_constraint_summary"]["transition_rejections"]
    assert rejections.get("turn_radius_exceeded", 0) > 0
    aircraft = blocked["readiness"]["aircraft"]
    assert aircraft["status"] == "ready"
    assert aircraft["min_turn_radius_m"] == 5000.0
    assert aircraft["turn_radius_source"] == "policy.aircraft_min_turn_radius_m"

    # A feasible radius lets the same OD succeed, so the rejection is the cause.
    generous = plan(OPEN_FLAT, policy_value=policy(aircraft_min_turn_radius_m=20.0))
    assert generous["status"] == "strategic_candidate"
    assert generous["distance_m"] > 0


def test_case_every_accepted_turn_respects_the_arc_length_limit():
    import math as _math

    for radius in (20.0, 100.0, 300.0):
        result = plan(OPEN_FLAT, policy_value=policy(aircraft_min_turn_radius_m=radius))
        if result["status"] != "strategic_candidate":
            continue
        for record in result["state_path"][:-1]:
            length = record["length_m"] or 0.0
            change = record["heading_change_deg"] or 0.0
            assert radius * _math.radians(change) <= length + 1e-6, (radius, record)


def test_case_climb_rejection_blocks_a_wall_that_needs_more_than_the_gradient():
    result = plan(
        {
            "profile_id": "steep", "terrain_profile": "ridge_longitude",
            "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 250.0,
        },
        policy_value=policy(max_climb_gradient=0.0001, max_descent_gradient=0.5),
    )
    assert result["status"] == "failed"
    assert result["hard_constraint_summary"]["transition_rejections"].get("climb_gradient_exceeded", 0) > 0


def test_case_descent_rejection_blocks_a_descent_that_is_too_steep():
    result = plan(
        {
            "profile_id": "steep_descent", "terrain_profile": "ridge_latitude",
            "ridge_latitude_band": [29.914, 29.917], "ridge_height_m": 250.0,
        },
        policy_value=policy(max_climb_gradient=0.5, max_descent_gradient=0.0001),
        start=[122.0005, 29.918], goal=[122.0195, 29.9005],
    )
    assert result["hard_constraint_summary"]["transition_rejections"].get("descent_gradient_exceeded", 0) > 0
    assert result["status"] in ("strategic_candidate", "failed")


def test_case_airspace_blocked_corridor_is_never_crossed():
    result = plan({
        "profile_id": "airspace", "terrain_profile": "flat",
        "restricted_cells": group(column=9),
    })
    assert result["status"] == "failed"
    assert result["hard_constraint_summary"]["state_rejections"].get("airspace_not_confirmed_allowed", 0) > 0
    airspace = result["readiness"]["airspace"]
    assert airspace["status"] == "ready"
    assert airspace["status_counts"]["confirmed_restricted"] == len(grid_cells()) // 18


def test_case_unknown_terrain_or_building_evidence_blocks_the_run_fail_closed():
    terrain = plan({"profile_id": "unknown_terrain", "unknown_terrain_cells": group(column=9)})
    assert terrain["status"] == "not_ready"
    assert terrain["readiness"]["terrain"]["status"] == "blocked"
    assert terrain["readiness"]["terrain"]["unresolved_cell_count"] == len(grid_cells()) // 18
    assert terrain["readiness"]["airspace"]["status"] == "ready"
    assert "unknown" in " ".join(terrain["readiness"]["terrain"]["reasons"])

    building_spec = {"profile_id": "unknown_building", "unknown_building_cells": group(column=9)}
    resolved = policy()
    patched = normalize_v3_cell_environment(
        build_synthetic_environment(grid_cells(), resolved, building_spec)
    )
    blocked = V3StrategicPlanner().plan(normalize_v3_planning_problem({
        "problem_id": "unknown_building", "start": START, "goal": GOAL,
        "policy": normalize_v3_planning_policy(resolved), "environment": patched,
    }))
    assert blocked["status"] == "not_ready"
    assert blocked["readiness"]["building"]["status"] == "blocked"
    assert blocked["readiness"]["terrain"]["status"] == "ready"


def test_soft_cost_uses_a_provenance_normalized_index_not_a_raw_count():
    spec = {
        "profile_id": "traffic_band", "terrain_profile": "flat",
        "traffic_index_per_cell": index_map(0.4, column=9),
    }
    baseline = plan(spec)
    assert baseline["status"] == "strategic_candidate"
    assert baseline["cost_vector"]["components"]["traffic_risk"]["enabled"] is False
    assert baseline["cost_vector"]["scalar_cost"] == pytest.approx(baseline["distance_m"])

    weighted_policy = policy()
    weighted_policy["cost_model"] = normalize_cost_model({
        "components": {"traffic_risk": {"enabled": True, "weight": 1.0}},
    })
    weighted = plan(spec, policy_value=weighted_policy)
    assert weighted["status"] == "strategic_candidate"
    component = weighted["cost_vector"]["components"]["traffic_risk"]
    assert component["enabled"] is True
    assert component["weight"] == 1.0
    assert component["unit"] == "m_x_normalized_index"
    assert component["semantics"] == "length_integrated_traffic_exposure_index_not_conflict_probability"
    assert weighted["cost_vector"]["scalar_cost"] > weighted["distance_m"]
    # The heuristic is the plain geometric distance: the weight never enters it.
    assert weighted["heuristic_semantics"]["scale"] == pytest.approx(1.0)
    assert weighted["heuristic_semantics"]["soft_penalty_weight_sum_enters_heuristic"] is False
    assert (weighted["hard_constraint_summary"]["state_rejections"]
            == baseline["hard_constraint_summary"]["state_rejections"])

    # A heavier weight must never *increase* the exposure the optimiser accepts.
    strict_policy = policy()
    strict_policy["cost_model"] = normalize_cost_model({
        "components": {"traffic_risk": {"enabled": True, "weight": 40.0}},
    })
    strict = plan(spec, policy_value=strict_policy)
    assert strict["status"] == "strategic_candidate"
    assert (strict["cost_vector"]["components"]["traffic_risk"]["exposure_m"]
            <= component["exposure_m"] + 1e-9)


def test_risk_exposure_integrates_the_normalized_index_over_edge_length():
    spec = {
        "profile_id": "uniform_traffic", "terrain_profile": "flat",
        "traffic_index_per_cell": index_map(0.5, columns=list(range(18))),
    }
    weighted_policy = policy()
    weighted_policy["cost_model"] = normalize_cost_model({
        "components": {"traffic_risk": {"enabled": True, "weight": 2.0}},
    })
    result = plan(spec, policy_value=weighted_policy)
    component = result["cost_vector"]["components"]["traffic_risk"]
    assert component["exposure_definition"] == "length_m_x_mean_index_of_edge_endpoints"
    # Every cell carries index 0.5, so exposure == 0.5 * total length exactly.
    assert component["exposure_m"] == pytest.approx(0.5 * result["distance_m"], rel=1e-9)
    assert component["raw"] == pytest.approx(component["exposure_m"])
    assert component["contribution"] == pytest.approx(2.0 * component["exposure_m"], rel=1e-9)
    assert component["normalized_index_statistics"]["mean"] == pytest.approx(0.5)
    assert component["normalized_index_statistics"]["min"] == pytest.approx(0.5)
    assert component["edge_count"] == len(result["state_path"]) - 1
    # Per-edge penalty is weight x length x index for a single enabled channel.
    for record in result["state_path"][:-1]:
        assert record["soft_penalty"] == pytest.approx(2.0 * 0.5 * record["length_m"], rel=1e-9)
    assert sum(record["soft_penalty"] for record in result["state_path"][:-1]) == pytest.approx(
        2.0 * component["exposure_m"], rel=1e-9,
    )
    # And the scalar is exactly length + weighted exposure per edge.
    assert result["cost_vector"]["scalar_cost"] == pytest.approx(
        result["distance_m"] + 2.0 * component["exposure_m"], rel=1e-9,
    )


def test_soft_cost_provenance_travels_with_the_report():
    spec = {
        "profile_id": "provenance", "terrain_profile": "flat",
        "population_index_per_cell": index_map(0.25, columns=list(range(18))),
        "soft_field_provenance": {
            "population_risk": {
                "source": "unit_test_mapped_source",
                "source_resolution_m": 100.0,
                "mapping_method": "area_weighted_source_pixel_overlap",
                "upsampled_without_new_information": False,
            },
        },
    }
    weighted_policy = policy()
    weighted_policy["cost_model"] = normalize_cost_model({
        "components": {"population_risk": {"enabled": True, "weight": 1.0}},
    })
    component = plan(spec, policy_value=weighted_policy)["cost_vector"]["components"]["population_risk"]
    assert component["source"] == "unit_test_mapped_source"
    assert component["source_resolution_m"] == pytest.approx(100.0)
    assert component["mapping_method"] == "area_weighted_source_pixel_overlap"
    assert component["upsampled_without_new_information"] is False
    assert component["provenance_sources"] == ["unit_test_mapped_source"]


def test_enabled_soft_channel_without_a_provenance_index_blocks_the_run():
    over = policy()
    over["cost_model"] = normalize_cost_model({
        "components": {"population_risk": {"enabled": True, "weight": 1.0}},
    })
    result = plan(OPEN_FLAT, policy_value=over)
    assert result["status"] == "not_ready"
    assert result["readiness"]["cost_model"]["status"] == "blocked"
    detail = result["readiness"]["cost_model"]["unresolved_soft_channels"]
    assert "population_risk" in detail
    assert detail["population_risk"]["unresolved_cell_count"] == len(grid_cells())
    assert any("normalized index" in reason for reason in result["readiness"]["cost_model"]["reasons"])
    assert result["state_path"] == []


def test_raw_counts_and_out_of_range_indices_are_rejected_not_normalized():
    from cns_planner.route_planner_v3.synthetic import normalize_synthetic_spec

    with pytest.raises(ValueError, match="traffic_per_cell"):
        normalize_synthetic_spec({"traffic_per_cell": {"A": 100.0}})
    with pytest.raises(ValueError, match="population_per_cell"):
        normalize_synthetic_spec({"population_per_cell": {"A": 12000.0}})
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        normalize_synthetic_spec({"population_index_per_cell": {"A": 12.0}})
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        normalize_v3_cell_environment({
            "cells": [{
                "grid_id": "A", "center": [1.0, 2.0],
                "soft_fields": {"population_risk": {"normalized_index": 5000.0, "status": "passed"}},
            }],
        })
    with pytest.raises(ValueError, match="normalized_index"):
        normalize_v3_cell_environment({
            "cells": [{
                "grid_id": "A", "center": [1.0, 2.0],
                "soft_fields": {"population_risk": {"status": "passed"}},
            }],
        })


def test_no_hidden_normalizer_constants_and_no_weight_sum_cap():
    from cns_planner import route_planner_v3 as package
    from cns_planner.route_planner_v3 import cost as cost_module

    for absent in (
        "SOFT_WEIGHT_SUM_LIMIT", "POPULATION_NORMALIZATION_PEOPLE",
        "TRAFFIC_NORMALIZATION_AIRCRAFT",
    ):
        assert not hasattr(cost_module, absent), absent
        assert absent not in package.__all__, absent
    # An explicit, finite, non-negative weight of any size is accepted.
    model = normalize_cost_model({
        "components": {
            "population_risk": {"enabled": True, "weight": 7.5},
            "traffic_risk": {"enabled": True, "weight": 2.5},
            "building_exposure": {"enabled": True, "weight": 4.0},
        },
    })
    parsed = SoftCostModel(model)
    assert parsed.penalty_weight_sum == pytest.approx(14.0)
    assert parsed.heuristic_scale() == pytest.approx(1.0)
    assert model["components"]["population_risk"]["normalization"].startswith(
        "external_provenance_normalized_index"
    )


def test_heuristic_is_the_plain_3d_geometric_distance_and_is_admissible():
    result = plan(OPEN_FLAT)
    semantics = result["heuristic_semantics"]
    assert semantics["type"] == "admissible_3d_geometric_distance_lower_bound"
    assert semantics["soft_penalty_non_negative"] is True
    assert semantics["scale"] == pytest.approx(1.0)
    assert semantics["scale_basis"].startswith("exactly 1")
    assert semantics["definition"].startswith("h = 3D geodesic distance")
    assert semantics["altitude_geometric_term"] == "included_vertical_delta"
    assert semantics["active"] is True
    assert semantics["soft_penalty_weight_sum_enters_heuristic"] is False
    assert semantics["soft_penalty_weight_sum_is_bounded"] is False
    assert semantics["no_implicit_normalizer"] is True
    assert "旧版" in semantics["admissibility_argument"]

    blocked = plan(OPEN_FLAT, policy_value=policy(confirmed=False))
    assert blocked["heuristic_semantics"]["active"] is False


#: Cases used for the "heuristic == Dijkstra" optimality comparison.
_DIJKSTRA_CASES = (
    ("open_flat", {"profile_id": "open_flat", "terrain_profile": "flat"}, {}),
    ("plateau", PLATEAU, {}),
    ("buildings", {
        "profile_id": "buildings", "terrain_profile": "flat",
        "buildings_profile": "cluster", "building_height_m": 320.0, "building_count": 40,
    }, {}),
    ("weighted_population", {
        "profile_id": "weighted", "terrain_profile": "flat",
        "population_index_per_cell": None,
    }, {"population_risk": {"enabled": True, "weight": 3.0}}),
    ("weighted_all_channels", {
        "profile_id": "weighted_all", "terrain_profile": "flat",
        "traffic_index_per_cell": None,
    }, {
        "population_risk": {"enabled": True, "weight": 0.5},
        "traffic_risk": {"enabled": True, "weight": 9.0},
        "building_exposure": {"enabled": True, "weight": 2.0},
    }),
    ("zero_penalty_shortest_path", {
        "profile_id": "zero_penalty", "terrain_profile": "flat",
        "traffic_index_per_cell": None,
    }, {"traffic_risk": {"enabled": True, "weight": 5.0}}),
)

#: Complete index maps for the weighted cases (band value, 0.0 elsewhere).
_DIJKSTRA_INDEX_MAPS = {
    "weighted_population": [("population_index_per_cell", 0.9, {"column": 9})],
    "weighted_all_channels": [
        ("population_index_per_cell", 0.3, {"columns": [1, 2, 3]}),
        ("traffic_index_per_cell", 0.7, {"columns": [4, 5, 6]}),
    ],
    "zero_penalty_shortest_path": [("traffic_index_per_cell", 1.0, {"column": 17})],
}


@pytest.mark.parametrize("name,spec,cost_components", _DIJKSTRA_CASES)
def test_heuristic_matches_dijkstra_optimal_cost_case_by_case(name, spec, cost_components, monkeypatch):
    """h = 3D geometric distance must give exactly the Dijkstra (h = 0) optimum.

    A zero-penalty edge is exactly the case that made the old
    ``h = distance x (1 + sum(weight))`` admissible only by accident; comparing
    against a weight-blind search is the direct regression test.
    """

    from cns_planner.route_planner_v3 import planner as planner_module

    resolved_spec = dict(spec)
    for field, value, selectors in _DIJKSTRA_INDEX_MAPS.get(name, []):
        resolved_spec[field] = index_map(value, **selectors)
    resolved = policy()
    if cost_components:
        resolved["cost_model"] = normalize_cost_model({"components": cost_components})
    heuristic_run = plan(resolved_spec, policy_value=resolved)
    assert heuristic_run["search_statistics"]["expansion_cap_reached"] is False

    original = planner_module._Search._heuristic
    monkeypatch.setattr(planner_module._Search, "_heuristic", lambda self, grid_id, altitude_index: 0.0)
    try:
        dijkstra_run = plan(resolved_spec, policy_value=resolved)
    finally:
        monkeypatch.setattr(planner_module._Search, "_heuristic", original)
    # Feasibility is decided by the hard constraints only, so both searches must
    # agree on whether a candidate exists at all.
    assert heuristic_run["status"] == dijkstra_run["status"], name
    assert heuristic_run["status"] in ("strategic_candidate", "failed"), name
    if heuristic_run["status"] != "strategic_candidate":
        return
    assert dijkstra_run["search_statistics"]["search_completeness"] == (
        "queue_exhausted_optimality_proven"
    )
    assert heuristic_run["cost_vector"]["scalar_cost"] == pytest.approx(
        dijkstra_run["cost_vector"]["scalar_cost"], rel=1e-9,
    ), name
    assert heuristic_run["distance_m"] == pytest.approx(dijkstra_run["distance_m"], rel=1e-9), name
    # The plain-geometry heuristic also never expands more states than Dijkstra.
    assert heuristic_run["search_statistics"]["expanded_states"] <= (
        dijkstra_run["search_statistics"]["expanded_states"]
    ), name


def test_scalar_cost_is_never_below_the_geometric_length_for_any_weight():
    over = policy()
    over["cost_model"] = normalize_cost_model({
        "components": {
            "population_risk": {"enabled": True, "weight": 12.0},
            "traffic_risk": {"enabled": True, "weight": 8.0},
            "building_exposure": {"enabled": True, "weight": 20.0},
        },
    })
    spec = {
        "profile_id": "heavy_weights", "terrain_profile": "flat",
        "population_index_per_cell": index_map(0.5, columns=list(range(18))),
        "traffic_index_per_cell": index_map(0.25, columns=list(range(18))),
    }
    result = plan(spec, policy_value=over)
    assert result["status"] == "strategic_candidate"
    assert result["cost_vector"]["scalar_weight_sum_is_not_bounded"] is True
    assert result["cost_vector"]["scalar_weight_sum_not_used_by_the_heuristic"] is True
    assert result["cost_vector"]["scalar_cost"] >= result["distance_m"] - 1e-9


def test_search_records_expanded_states_runtime_and_state_space_shape():
    result = plan()
    statistics = result["search_statistics"]
    assert statistics["expanded_states"] > 0
    assert statistics["generated_states"] > 0
    assert statistics["expanded_transitions"] > 0
    assert statistics["runtime_ms"] >= 0
    assert statistics["expansion_cap"] == 200000
    shape = statistics["state_space_shape"]
    assert shape["grid_cells"] == len(grid_cells())
    assert shape["altitude_levels"] == 7
    assert shape["heading_bins"] == 8
    assert shape["naive_state_count"] == len(grid_cells()) * 7 * 8


def test_expansion_cap_is_resource_limited_never_failed_or_infeasible():
    """Reaching ``max_expanded_states`` is a resource verdict, not a feasibility one."""

    for cap in (1, 500, 5000, 20000):
        result = plan(OPEN_FLAT, policy_value=policy(max_expanded_states=cap))
        assert result["status"] in ("search_incomplete", "strategic_candidate"), cap
        statistics = result["search_statistics"]
        assert statistics["expansion_cap"] == cap
        if result["status"] == "search_incomplete":
            assert statistics["expansion_cap_reached"] is True
            assert statistics["resource_limited"] is True
            assert statistics["resource_limit"] == "max_expanded_states"
            assert statistics["search_completeness"] == (
                "expansion_cap_reached_optimality_not_proven"
            )
            assert statistics["search_complete"] is False
            assert statistics["optimality_proven"] is False
            assert statistics["infeasibility_proven"] is False
            assert result["semantics"]["resource_limited_not_infeasible"] is True
            assert "infeasible" in result["reason"]
            assert "不可行" in result["reason"] or "不构成 infeasible" in result["reason"]
            if result["state_path"]:
                # A candidate found before the cap is kept, but never as proven optimal.
                assert result["distance_m"] is not None
                assert "未证明最优" in result["reason"]
            else:
                assert result["distance_m"] is None
        else:
            assert statistics["search_complete"] is True
            assert statistics["search_completeness"] == "queue_exhausted_optimality_proven"
            assert statistics["optimality_proven"] is True

    # A generous cap completes the same problem and proves optimality.
    complete = plan(OPEN_FLAT, policy_value=policy(max_expanded_states=200000))
    assert complete["status"] == "strategic_candidate"
    assert complete["search_statistics"]["search_complete"] is True
    assert complete["search_statistics"]["resource_limited"] is False
    assert complete["search_statistics"]["resource_limit_reason"] is None


def test_hard_constraint_rejection_statistics_are_reported_and_bounded():
    result = plan(OPEN_FLAT, policy_value=policy(aircraft_min_turn_radius_m=250.0))
    summary = result["hard_constraint_summary"]
    assert summary["total_state_rejections"] == sum(summary["state_rejections"].values())
    assert summary["total_transition_rejections"] == sum(summary["transition_rejections"].values())
    assert summary["unknown_is_never_feasible"] is True
    assert summary["audit_sample_cap_per_reason"] == 200
    evidence = summary["environment_evidence"]
    assert evidence["cell_count"] == len(grid_cells())
    assert evidence["semantics"].startswith("canonical_environment_evidence_gaps")


def test_case_deterministic_fingerprint_and_path_across_runs():
    first = plan(OPEN_FLAT)
    second = plan(OPEN_FLAT)
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["state_path"] == second["state_path"]
    assert first["distance_m"] == second["distance_m"]
    assert first["hard_constraint_summary"]["transition_rejections"] == (
        second["hard_constraint_summary"]["transition_rejections"]
    )
    assert json.dumps(first["state_path"], sort_keys=True) == json.dumps(second["state_path"], sort_keys=True)
    changed = plan(OPEN_FLAT, goal=[122.018, 29.918])
    assert changed["input_fingerprint"] != first["input_fingerprint"]


def test_case_corridor_n_ring_is_a_search_window_not_a_safety_clearance():
    result = plan(OPEN_FLAT, provenance={"corridor_ring_n": 1, "corridor_altitude_margin_m": 25.0})
    corridor = result["candidate_refinement_corridor"]
    assert corridor["semantics"] == "refinement_search_window_not_safety_corridor"
    assert corridor["ring_n"] == 1
    assert corridor["n_ring_is_not_a_safety_clearance"] is True
    assert corridor["ring_semantics"] == "n_ring_support_cells_expand_the_search_window_not_a_clearance"
    assert corridor["next_stage"] == "V3-B_corridor_local_refinement"
    centers = set(corridor["center_grid_ids"])
    support = set(corridor["support_grid_ids"])
    assert centers <= support
    assert corridor["center_state_count"] == len(result["state_path"])
    assert corridor["support_cell_count"] == len(corridor["support_grid_ids"])
    envelope = corridor["altitude_envelope"]
    altitudes = [record["altitude_egm2008_m"] for record in result["state_path"]]
    assert envelope["lower_altitude_egm2008_m"] == pytest.approx(min(altitudes) - 25.0)
    assert envelope["upper_altitude_egm2008_m"] == pytest.approx(max(altitudes) + 25.0)
    assert envelope["explicit_margin_m"] == 25.0
    assert corridor["not_implemented_in_v3a"] == [
        "corridor_local_fine_refinement", "exact_polygon_terrain_continuous_clearance_validation",
    ]

    ring_zero = plan(OPEN_FLAT, provenance={"corridor_ring_n": 0})["candidate_refinement_corridor"]
    assert ring_zero["support_grid_ids"] == ring_zero["center_grid_ids"]
    assert ring_zero["refinement_cell_size_m"] is None


def test_ring_neighbors_are_chebyshev_bounded_by_the_grid_and_never_guessed():
    cells = grid_cells()
    width = max(int(cell["column"]) for cell in cells) + 1
    center = next(cell for cell in cells if cell["column"] == 3 and cell["row"] == 3)
    assert len(ring_neighbors(cells, center["grid_id"], 1)) == 9
    assert len(ring_neighbors(cells, center["grid_id"], 2)) == 25
    corner = next(cell for cell in cells if cell["column"] == 0 and cell["row"] == 0)
    assert len(ring_neighbors(cells, corner["grid_id"], 1)) == 4
    last = next(cell for cell in cells if cell["column"] == width - 1 and cell["row"] == width - 1)
    assert len(ring_neighbors(cells, last["grid_id"], 1)) == 4
    assert ring_neighbors(cells, center["grid_id"], 0) == [center["grid_id"]]
    assert ring_neighbors(cells, "MHT-MISSING", 3) == ["MHT-MISSING"]


def test_corridor_builder_declares_its_optional_parameters_and_next_stage():
    result = plan(OPEN_FLAT)
    corridor = build_candidate_refinement_corridor(
        result["state_path"], environment(), ring_n=1, refinement_cell_size_m=30.0,
    )
    json.dumps(corridor, allow_nan=False)
    assert corridor["refinement_cell_size_m"] == 30.0
    assert corridor["corridor_id"].startswith("V3CORR-")
    empty = build_candidate_refinement_corridor([], environment(), ring_n=2)
    assert empty["center_grid_ids"] == [] and empty["support_grid_ids"] == []
    assert empty["semantics"] == "refinement_search_window_not_safety_corridor"


# --------------------------------------------------------------------------------------
# V3-A must never be presented as a final validated operational route
# --------------------------------------------------------------------------------------


def test_no_v3_result_is_ever_labelled_final_safe_or_validated():
    results = [
        plan(OPEN_FLAT),
        plan({"profile_id": "wall", "terrain_profile": "ridge_longitude",
              "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 600.0}),
        plan({"profile_id": "unknown", "unknown_terrain_cells": group(column=9)}),
        plan(OPEN_FLAT, policy_value=policy(confirmed=False)),
        plan(OPEN_FLAT, policy_value=policy(aircraft_min_turn_radius_m=5000.0)),
    ]
    forbidden_status_prefixes = ("final", "safe", "validated", "operational")
    forbidden_keys = (
        '"validated": true', '"operationally_valid"', '"certified_safe"',
        '"operational_route": true', '"final_validation_performed": true',
        '"status": "final', '"status":"final',
    )
    for result in results:
        assert result["status"] in V3_RESULT_STATUSES
        for status in V3_RESULT_STATUSES:
            assert not status.startswith(forbidden_status_prefixes), status
        assert result["operational_route"] is False
        assert result["final_validation_performed"] is False
        assert result["semantics"]["not_final_safe"] is True
        assert result["semantics"]["not_validated_operational_route"] is True
        serialized = json.dumps(result, ensure_ascii=False).lower()
        for token in forbidden_keys:
            assert token not in serialized, token
        # The disclaimer is present verbatim, so the experimental scope travels with
        # the result wherever it is copied.
        assert "不是 final safe" in result["disclaimer"]


def test_result_declares_the_v3a_scope_limits_explicitly():
    result = plan(OPEN_FLAT)
    limits = result["semantics"]["v3a_scope_limits"]
    assert limits["corridor_local_fine_refinement"] == "implemented_in_V3-B"
    assert limits["exact_polygon_terrain_continuous_clearance_validation"] == "not_implemented_V3-C"
    assert limits["validated_route_operational_adapter"] == "not_implemented_V3-D"
    assert limits["route_cns_joint_optimization"] == "future_backlog_not_V3-D"
    assert limits["energy_model"] == "pending_model_disabled"
    assert result["semantics"]["search_state"] == "grid_id + altitude_index + heading_bin"
    assert result["semantics"]["canonical_vertical_reference"] == "egm2008_orthometric"
    assert result["semantics"]["heuristic_is_plain_geometric_distance"] is True
    assert result["semantics"]["soft_fields_are_provenance_normalized_indices"] is True
    assert result["semantics"]["no_hidden_soft_normalizer"] is True
    assert result["semantics"]["expansion_cap_is_resource_limited_not_infeasible"] is True
    assert "not_a_flight_dynamics_certification_model" in result["semantics"]["motion_model"]
    assert result["semantics"]["motion_model_id"] == "engineering_3d_motion_primitives_v3a"


def test_blocked_and_failed_results_carry_no_candidate_path_or_corridor():
    for result in (
        plan(OPEN_FLAT, policy_value=policy(confirmed=False)),
        plan({"profile_id": "wall", "terrain_profile": "ridge_longitude",
              "ridge_longitude_band": [122.008, 122.012], "ridge_height_m": 600.0}),
    ):
        assert result["state_path"] == []
        assert result["horizontal_projection"] == []
        assert result["distance_m"] is None
        assert result["candidate_refinement_corridor"] is None
        assert result["cost_vector"]["scalar_cost"] is None
        assert result["readiness"] is not None


# --------------------------------------------------------------------------------------
# V1 / V2 characterisation is untouched
# --------------------------------------------------------------------------------------


def test_v1_output_contract_is_unchanged():
    planner = RoutePlannerV1(grid_size=6)
    route = {"route_id": "R-GOLDEN-003", "start": [0.1, 0.1], "end": [0.9, 0.9]}
    result = planner.plan(route, [0.0, 0.0, 1.0, 1.0], [])
    assert list(result) == [
        "route_id", "status", "path", "reason", "algorithm_id", "algorithm_version",
        "input_fingerprint", "environment_risk",
    ]
    assert result["algorithm_id"] == "route_planner_v1"
    assert result["algorithm_version"] == "1.0"
    pinned = RoutePlannerV1(grid_size=6).plan(
        {"route_id": "R-GOLDEN-001", "start": [0.1, 0.1], "end": [0.9, 0.9]},
        [0.0, 0.0, 1.0, 1.0], [{"name": "中心硬约束", "bbox": [0.4, 0.4, 0.6, 0.6]}],
    )
    assert pinned["input_fingerprint"] == (
        "ae10b604508d03f2445f36573156ba109e2ed30904d6e8ff5957f0dce8a936d4"
    )


def test_v3_is_additive_and_never_the_default_planner():
    selection = default_algorithm_selection()
    assert selection["route_planner"]["algorithm_id"] == "route_planner_v1"
    assert selection["route_planner"]["version"] == "1.0"
    assert V3StrategicPlanner.algorithm_id == "route_planner_v3_strategic"
    assert V3StrategicPlanner().uses_v3_native_3d is True


def test_v3_package_is_isolated_from_v2_spatial_3d_and_never_reads_files():
    root = Path("cns_planner/route_planner_v3")
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("*.py")}
    assert sources
    for name, text in sources.items():
        assert "from ..route_planner" not in text and "import risk_aware" not in text, name
        assert "route_altitude_profiles" not in text, name
        assert "import gdal" not in text and "osgeo" not in text, name
        assert "open(" not in text, name
    # The 30 m refinement, the exact final validator and an energy model are absent.
    for absent in ("refinement.py", "exact_validation.py", "final_validator.py", "energy.py"):
        assert not (root / absent).exists(), absent
