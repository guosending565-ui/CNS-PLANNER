from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.v3_operational_adoption_service import V3OperationalAdoptionService
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.gis.fine_environment_adapter import FINE_SOURCE_ROLES, real_data_source_readiness
from cns_planner.gis.v3_environment_adapter import V3RealEnvironmentAdapter
from cns_planner.risk.v1 import RiskModelV1
from cns_planner.route_planner.risk_aware_v2 import RiskAwareRoutePlannerV2
from cns_planner.route_planner_v3.continuous_contracts import validation_fingerprint_components
from cns_planner.route_planner_v3.continuous_validation import _route_status
from cns_planner.route_planner_v3.hard_constraints import HardConstraintEvaluator
from cns_planner.route_planner_v3.readiness import evaluate_v3_readiness

DEFAULTS = Path("cns_planner/config/defaults.json")
WORKSPACE = [122.0, 29.9, 122.02, 29.92]


def _policy():
    return {
        "min_altitude_egm2008_m": 100.0, "max_altitude_egm2008_m": 300.0,
        "vertical_step_m": 50.0, "terrain_clearance_m": 30.0,
        "building_horizontal_clearance_m": 10.0, "building_vertical_clearance_m": 20.0,
        "aircraft_min_turn_radius_m": 10.0, "confirmed": True,
        "cost_model": {"components": {}},
    }


def _confirmed_policy():
    """The full explicit V3 policy the service requires (no value is ever defaulted)."""

    policy = dict(_policy())
    policy.update({
        "max_climb_gradient": 0.5, "max_descent_gradient": 0.5,
        "planning_speed_mps": 25.0, "source": "airspace_decoupling_test",
    })
    return policy


def _cell(status="not_applicable"):
    return {
        "grid_id": "G1", "center": [122.0, 30.0], "bbox": [121.99, 29.99, 122.01, 30.01],
        "level": 8, "column": 0, "row": 0, "cell_size_m": 100.0,
        "terrain": {"data_status": "passed", "surface_clearance_egm2008_m": 80.0},
        "buildings": {"data_status": "passed", "required_clearance_egm2008_m": None},
        "airspace": {"status": status}, "soft_fields": {},
    }


def test_v3_airspace_is_display_only_for_readiness_and_feasibility():
    policy = _policy()
    for status in ("confirmed_allowed", "confirmed_restricted", "unknown", "not_applicable"):
        environment = {"status": "passed", "properties": {
            "terrain_clearance_m": 30.0, "building_horizontal_clearance_m": 10.0,
            "building_vertical_clearance_m": 20.0,
        }, "cells": [_cell(status)]}
        evaluator = HardConstraintEvaluator(environment, policy, climb_gradient=1.0, descent_gradient=1.0)
        assert evaluator.state_feasible("G1", 0, 100.0) == (True, None)
        readiness = evaluate_v3_readiness({"environment": environment, "policy": policy,
                                           "aircraft_motion_limits": {}})
        assert readiness["airspace"]["status"] == "ready"
        assert readiness["airspace"]["applicability"] == "not_applicable"
        assert readiness["airspace"]["layer_role"] == "display_only_reference_layer"


def test_v3c_fingerprint_excludes_airspace_policy_and_source_audit():
    base = {
        "refinement": {"refinement_fingerprint": "R", "frame": {}},
        "policy": {"curve_chord_error_m": 1.0, "airspace_allow_touching_blocked_boundary": False},
        "source_audit": {"terrain": {"sha256": "T"}, "airspace": {"sha256": "A"}},
    }
    changed = deepcopy(base)
    changed["policy"]["airspace_allow_touching_blocked_boundary"] = True
    changed["source_audit"]["airspace"] = {"sha256": "B"}
    assert validation_fingerprint_components(base) == validation_fingerprint_components(changed)


class _Terrain:
    def sample_cells(self, cells, *, transform):
        return {cell["fine_cell_id"]: {
            "data_status": "passed", "surface_elevation_max_egm2008_m": 50.0,
        } for cell in cells}

    def describe(self):
        return {"dataset": "fake verified FABDEM", "vertical_reference": "egm2008_orthometric"}


def test_real_v3a_adapter_keeps_unknown_building_height_unresolved():
    grid = {"level": 8, "cells": [{
        "grid_id": "G1", "center": [122.0, 30.0], "bbox": [121.99, 29.99, 122.01, 30.01],
        "level": 8, "column": 0, "row": 0,
    }]}
    state = {
        "grid_attributes": {"buildings": {"cells": {"G1": {
            "status": "passed", "building_count": 2, "height_max_m": None,
            "valid_height_fraction": 0.5,
        }}}},
        "grid_risk": {"cells": {}},
        "source_audits": {"items": {
            role: {"status": "verified", "source_id": role} for role in
            ("terrain_dtm", "buildings", "building_grid")
        }},
    }
    environment = V3RealEnvironmentAdapter(_Terrain()).build(
        grid=grid, policy=_policy(), state=state,
    )
    cell = environment["cells"][0]
    assert environment["source_type"] == "configured_real_sources"
    assert cell["buildings"]["data_status"] == "unknown"
    assert cell["buildings"]["required_clearance_egm2008_m"] is None
    assert cell["airspace"]["status"] == "not_applicable"


def test_v2_path_and_result_do_not_depend_on_airspace_eligibility():
    grid = {"status": "passed", "level": 8, "cells": [
        {"grid_id": "A", "center": [122.0, 30.0], "bbox": [121.9995, 29.9995, 122.0005, 30.0005], "column": 0, "row": 0, "level": 8},
        {"grid_id": "B", "center": [122.001, 30.0], "bbox": [122.0005, 29.9995, 122.0015, 30.0005], "column": 1, "row": 0, "level": 8},
    ]}
    route = {"route_id": "R", "start": [122.0, 30.0], "end": [122.001, 30.0]}
    risk = {"status": "passed", "cells": {
        key: {"overall": {"status": "passed", "score": 0.1}} for key in ("A", "B")
    }}
    planner = RiskAwareRoutePlannerV2()
    first = planner.plan(route, grid, risk, [], {"status": "blocked", "allowed_grid_ids": []})
    second = planner.plan(route, grid, risk, [], {"status": "passed", "allowed_grid_ids": ["A"]})
    assert first["status"] == second["status"] == "passed"
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["airspace_source"]["status"] == "not_applicable"


def test_risk_value_and_inputs_do_not_depend_on_airspace_mapping():
    grid = {"status": "passed", "level": 8, "cells": [{"grid_id": "G1"}]}
    attributes = {
        "population": {"status": "passed", "cells": {"G1": {
            "status": "passed", "value_status": "passed",
            "population_density_people_km2": 10.0,
        }}},
        "terrain": {"status": "passed", "cells": {"G1": {
            "status": "passed", "min_elevation": 0.0, "max_elevation": 0.0,
        }}},
        "traffic": {"status": "passed", "cells": {"G1": {
            "status": "passed", "traffic_density_norm": 0.5,
        }}},
        "conflict": {"status": "passed", "cells": {"G1": {
            "status": "passed", "conflict_rate_norm": 0.0,
        }}},
        "airspace": {"status": "passed", "cells": {"G1": {
            "status": "partial_intersection", "airspaces": [{"category": "CTR"}],
        }}},
    }
    first = RiskModelV1().evaluate(grid, attributes)
    attributes["airspace"] = {"status": "stale", "cells": {"G1": {
        "status": "confirmed_restricted", "airspaces": [{"category": "changed"}],
    }}}
    second = RiskModelV1().evaluate(grid, attributes)
    assert first == second
    assert first["cells"]["G1"]["airspace_constraint"]["status"] == "not_applicable"


def test_real_source_readiness_uses_vertical_profile_and_building_grid_audit(tmp_path):
    terrain = tmp_path / "fabdem.tif"
    buildings = tmp_path / "buildings.gpkg"
    terrain.write_bytes(b"fixture")
    buildings.write_bytes(b"fixture")
    audits = {"items": {
        role: {"status": "verified", "source_id": role}
        for role in ("terrain_dtm", "buildings", "building_grid")
    }}
    profile = {
        "crs": {"observed_vertical": "EGM2008_orthometric"},
        "verification": {"status": "verified_from_raster_metadata"},
    }
    ready = real_data_source_readiness(
        {"terrain_dtm": terrain, "buildings": buildings},
        policy_confirmed=True, source_audits=audits, terrain_profile=profile,
    )
    assert ready["status"] == "ready"
    assert ready["blocking_reasons"] == []
    assert ready["airspace"]["status"] == "not_applicable"

    blocked = real_data_source_readiness(
        {"terrain_dtm": terrain, "buildings": buildings},
        policy_confirmed=False, source_audits=audits, terrain_profile=profile,
    )
    assert blocked["status"] == "blocked"
    assert blocked["blocking_reasons"] == ["v3_policy_not_confirmed"]


def test_v3c_skipped_airspace_does_not_block_active_domains():
    domains = {
        name: {"status": "passed"}
        for name in ("geometry", "terrain", "building", "altitude", "kinematics")
    }
    domains["airspace"] = {
        "status": "skipped", "applicability": "not_applicable",
        "reason": "display_only_airspace_not_used_for_route_constraints",
    }
    status, _ = _route_status(domains, {})
    assert status == "validated_route"


class _Session:
    def __init__(self):
        self.state = {}


def test_v3d_does_not_track_airspace_or_basemap_sources():
    service = V3OperationalAdoptionService(
        _Session(), None, None, lambda: None,
    )
    assert service.stale_for_sources(["airspace", "basemap"]) == {
        "status": "not_applicable", "stale_adoption_ids": [], "stale_route_ids": [],
    }


# --------------------------------------------------------------------------------------
# V3-B: no airspace source role, no allowed-airspace blocker
# --------------------------------------------------------------------------------------


def test_v3b_fine_source_roles_and_readiness_have_no_airspace_requirement(tmp_path):
    assert "airspace" not in FINE_SOURCE_ROLES
    terrain = tmp_path / "fabdem.tif"
    buildings = tmp_path / "buildings.gpkg"
    terrain.write_bytes(b"fixture")
    buildings.write_bytes(b"fixture")
    readiness = real_data_source_readiness(
        {"terrain_dtm": terrain, "buildings": buildings}, policy_confirmed=True,
        source_audits={"items": {
            role: {"status": "verified"} for role in ("terrain_dtm", "buildings", "building_grid")
        }},
        terrain_profile={
            "crs": {"observed_vertical": "EGM2008_orthometric"},
            "verification": {"status": "verified_from_raster_metadata"},
        },
    )
    assert readiness["status"] == "ready"
    assert readiness["roles"] == list(FINE_SOURCE_ROLES)
    assert not [reason for reason in readiness["blocking_reasons"] if "airspace" in reason]
    assert readiness["airspace"] == {"status": "not_applicable", "applicability": "display_only"}


def test_real_source_adapters_never_construct_confirmed_airspace_sources():
    """The V3 real-data wiring must not read ConfirmedAirspace* anywhere."""

    application = Path("cns_planner/application/app_context.py").read_text(encoding="utf-8")
    assert "ConfirmedAirspacePolicySource" not in application
    assert "ConfirmedAirspacePolygonSource" not in application
    assert "airspace_source" not in application
    adapter = Path("cns_planner/gis/v3_environment_adapter.py").read_text(encoding="utf-8")
    assert "AirspacePolicy" not in adapter
    assert "airspace" in adapter and '"not_applicable"' in adapter


# --------------------------------------------------------------------------------------
# V3-A configured_real_sources: readiness, P1 gate, injected adapter
# --------------------------------------------------------------------------------------


def _real_workflow(tmp_path, *, policy_confirmed=True):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    service.state["workspace"] = {"status": "passed", "bbox": list(WORKSPACE)}
    service.state["nodes"] = [
        {"node_id": "N001", "name": "A", "coordinate": [122.0005, 29.9005]},
        {"node_id": "N002", "name": "B", "coordinate": [122.0195, 29.9195]},
    ]
    service.state["node_seq"] = 2
    service.state["data_source_profiles"] = {"terrain_dtm": {
        "source_id": "fabdem",
        "crs": {"observed_vertical": "EGM2008_orthometric"},
        "verification": {"status": "verified_from_raster_metadata"},
    }}
    service.state["source_audits"] = {"items": {
        role: {"status": "verified", "source_id": role}
        for role in ("terrain_dtm", "buildings", "building_grid")
    }}
    policy = dict(_confirmed_policy())
    if not policy_confirmed:
        policy["confirmed"] = False
    service.set_route_planner_v3_policy(policy)
    return service


def test_v3a_real_source_readiness_resolves_terrain_vertical_and_building_grid(tmp_path):
    service = _real_workflow(tmp_path)
    readiness = service.route_planner_v3_service.real_data_readiness()
    assert readiness["v3a_status"] == "ready"
    assert readiness["v3a_blocking_reasons"] == []
    assert readiness["terrain_vertical_reference"] == "EGM2008_orthometric"
    assert readiness["terrain_source_verified"] is True
    assert readiness["buildings_source_verified"] is True
    assert readiness["building_grid_source"] is True
    # The display-only airspace layer never gates the real-data verdict.
    assert readiness["airspace"] == {"status": "not_applicable", "applicability": "display_only"}
    assert not [item for item in readiness["v3a_blocking_reasons"] if "airspace" in item]


def test_v3a_real_source_readiness_stays_blocked_while_policy_is_unconfirmed(tmp_path):
    service = _real_workflow(tmp_path, policy_confirmed=False)
    readiness = service.route_planner_v3_service.real_data_readiness()
    assert readiness["v3a_status"] == "blocked"
    assert readiness["v3a_blocking_reasons"] == ["v3_policy_not_confirmed"]
    # Source evidence itself is still reported as verified: the blocker is the policy.
    assert readiness["terrain_source_verified"] is True
    assert readiness["building_grid_source"] is True


def test_v3a_configured_real_sources_cannot_bypass_an_unconfirmed_policy(tmp_path):
    service = _real_workflow(tmp_path, policy_confirmed=False)
    with pytest.raises(ValueError, match="V3 policy 未确认"):
        service.evaluate_route_planner_v3({
            "environment_source": "configured_real_sources",
            "allow_unconfirmed_policy": True,
        })


def test_v3a_configured_real_sources_uses_the_injected_gis_adapter(tmp_path):
    service = _real_workflow(tmp_path)
    grid = service.route_planner_v3_service._l8_grid()
    service.state["grid_attributes"] = {"buildings": {"cells": {
        cell["grid_id"]: {"status": "passed", "building_count": 0} for cell in grid["cells"]
    }}}
    adapter = V3RealEnvironmentAdapter(_Terrain())
    service.route_planner_v3_service.evaluate(
        {"environment_source": "configured_real_sources", "policy": _confirmed_policy()},
        adapter=adapter,
    )
    records = (service.state.get("route_planner_v3_experiments") or {}).get("records") or []
    assert len(records) == 1
    record = records[0]
    assert record["environment_source"] == "configured_real_sources"
    assert record["source_type"] == "configured_real_sources"
    assert record["grounding"] == "audited_configured_real_sources"
    assert record["result"]["status"] in ("strategic_candidate", "infeasible", "search_incomplete")
    # No AirspacePolicy is configured anywhere in this project.
    assert not (service.state.get("airspace_policies") or {}).get("items")


# --------------------------------------------------------------------------------------
# Invalidation: the display-only airspace source never stales planning
# --------------------------------------------------------------------------------------


def test_airspace_and_basemap_source_changes_do_not_stale_risk_or_routes(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = service.set_workspace(WORKSPACE, {
        "status": "passed", "population": {"status": "passed"}, "traffic": {"status": "passed"},
        "conflict": {"status": "passed"}, "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"}, "property": {"status": "missing_data"},
        "loaded_layer_count": 1, "covered_layer_count": 1,
    })
    # Phase4-B5X：通用快照只带网格摘要，逐 cell 明细走专用读取接口。
    grid = service.grid_snapshot()
    attributes = service.grid_attributes_snapshot()
    for kind in ("population", "terrain", "traffic", "conflict"):
        attributes[kind].update({
            "status": "passed", "grid_level": grid["level"], "count": grid["count"],
            "cells": {cell["grid_id"]: {} for cell in grid["cells"]},
        })
    service.apply_grid_attributes(attributes)
    service.evaluate_grid_risk({})
    before_risk = deepcopy(service.state["grid_risk"])
    before_attributes = deepcopy(service.state["grid_attributes"])
    service.state["operational_routes"] = [{"route_id": "R0001", "status": "passed"}]

    service.invalidate_grid_attributes({"airspace"})
    service.invalidate_grid_attributes({"basemap"})

    assert service.state["grid_attributes"] == before_attributes
    assert service.state["grid_risk"] == before_risk
    assert service.state["operational_routes"] == [{"route_id": "R0001", "status": "passed"}]
    assert service.state["result_statuses"].get("routes") != "stale"
    assert service.state["result_statuses"].get("environment_risk") != "stale"


