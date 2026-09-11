from copy import deepcopy
import math
from pathlib import Path

from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.risk.model import RiskModel
from cns_planner.risk.v1 import RiskModelV1
from cns_planner.services.conflict_grid_service import ConflictGridService
from cns_planner.services.traffic_grid_service import TrafficGridService
from cns_planner.services.workflow import WorkflowService


def _grid():
    return {
        "status": "passed",
        "level": 7,
        "count": 2,
        "cells": [
            {"grid_id": "G1", "geometry": {"type": "Polygon", "coordinates": []}},
            {"grid_id": "G2", "geometry": {"type": "Polygon", "coordinates": []}},
        ],
    }


def _attribute(name, cell_status="passed", **metadata):
    return {
        "status": "passed",
        "source": {"path": f"{name}.fixture"},
        "algorithm_id": f"{name}-mapping",
        "algorithm_version": "fixture-1",
        "grid_level": 7,
        "count": 2,
        "cells": {
            grid_id: {"status": cell_status}
            for grid_id in ("G1", "G2")
        },
        **metadata,
    }


def _attributes():
    empty = {
        name: {
            "status": "not_calculated",
            "source": None,
            "algorithm_id": None,
            "algorithm_version": None,
            "namespace": name,
            "grid_level": None,
            "count": 0,
            "cells": {},
        }
        for name in ("buildings", "property_exposure", "infrastructure", "towers")
    }
    population = _attribute(
        "population", unit_status="verified_from_raster_metadata", value_unit="source-unit"
    )
    population["cells"] = {
        "G1": {"status": "passed", "value_mean": 99.0},
        "G2": {"status": "passed", "value_mean": 3.0},
    }
    terrain = _attribute("terrain", elevation_unit="m")
    terrain["cells"] = {
        "G1": {"status": "passed", "mean_elevation": 100.0, "min_elevation": 90.0, "max_elevation": 110.0},
        "G2": {"status": "passed", "mean_elevation": 50.0, "min_elevation": 45.0, "max_elevation": 55.0},
    }
    airspace = _attribute("airspace", cell_status="no_coverage")
    for cell in airspace["cells"].values():
        cell.update({"intersected_layer_count": 0, "coverage_ratio": 0.0, "airspaces": []})
    traffic = _attribute("traffic")
    traffic["cells"] = {
        "G1": {"status": "passed", "flight_count": 2, "flight_seconds": 20.0, "traffic_density_raw": 0.5, "traffic_density_norm": 0.5},
        "G2": {"status": "passed", "flight_count": 1, "flight_seconds": 10.0, "traffic_density_raw": 0.25, "traffic_density_norm": 0.25},
    }
    conflict = _attribute("conflict")
    conflict["cells"] = {
        "G1": {"status": "passed", "conflict_count": 1, "conflict_rate": 0.1, "conflict_rate_norm": 0.2},
        "G2": {"status": "passed", "conflict_count": 0, "conflict_rate": 0.0, "conflict_rate_norm": 0.0},
    }
    return {
        "population": population, "terrain": terrain, "airspace": airspace,
        "traffic": traffic, "conflict": conflict, **empty,
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


def _workflow_mapped_attributes(grid):
    attributes = _attributes()
    attributes["traffic"] = TrafficGridService.empty()
    attributes["conflict"] = ConflictGridService.empty()
    grid_ids = [cell["grid_id"] for cell in grid["cells"]]
    for name in ("population", "terrain", "airspace"):
        attributes[name]["grid_level"] = grid["level"]
        attributes[name]["count"] = len(grid_ids)
        status = "no_coverage" if name == "airspace" else "passed"
        attributes[name]["cells"] = {
            grid_id: {"status": status} for grid_id in grid_ids
        }
    return attributes


def test_risk_model_protocol_and_v1_contract_are_geometry_free_and_relative():
    model = RiskModelV1()
    attributes = _attributes()

    result = model.evaluate(_grid(), attributes, {
        "scenario": "fixture",
        "population_reference": {"value": 99.0},
        "terrain_relief_reference": {"value": 20.0},
        "ground_formula": {"terrain_multiplier": 0.2},
    })

    assert isinstance(model, RiskModel)
    assert result["status"] == "passed"
    assert result["algorithm_id"] == "risk-model-v1-relative-index"
    assert result["algorithm_version"] == "1.1"
    assert result["parameters"]["scenario"] == "fixture"
    assert result["input_status"]["airspace"] == "passed"
    assert result["source_versions"]["terrain"]["version"] == "fixture-1"
    assert set(result["cells"]) == {"G1", "G2"}
    first = result["cells"]["G1"]
    assert math.isclose(first["ground"]["score"], 0.6)
    assert first["ground"]["level"] == "high"
    assert math.isclose(first["air"]["score"], 0.35)
    assert math.isclose(first["overall"]["score"], 0.525)
    assert first["overall"]["level"] == "medium"
    assert first["ground"]["contributors"]["population"]["contribution"] == 0.5
    assert math.isclose(first["ground"]["contributors"]["terrain"]["contribution"], 0.1)
    assert first["ground"]["contributors"]["buildings"]["status"] == "not_available"
    assert first["ground"]["absolute_risk"]["status"] == "not_calculated"
    assert all("geometry" not in cell for cell in result["cells"].values())
    assert _grid()["cells"][0]["geometry"]


def test_v1_marks_unverified_population_as_relative_only_but_still_normalizes():
    attributes = _attributes()
    attributes["population"]["unit_status"] = "unverified"

    result = RiskModelV1().evaluate(_grid(), attributes)

    ground = result["cells"]["G1"]["ground"]
    contributor = ground["contributors"]["population"]
    assert contributor["status"] == "passed"
    assert contributor["normalized"] == 1.0
    assert contributor["population_unit_status"] == "unverified"
    assert ground["risk_semantics"] == "relative_only"


def test_missing_ground_factor_is_excluded_and_weights_are_renormalized():
    attributes = _attributes()
    attributes["terrain"]["cells"]["G1"] = {"status": "missing_data"}

    result = RiskModelV1().evaluate(_grid(), attributes, {
        "population_reference": {"value": 99.0},
        "terrain_relief_reference": {"value": 20.0},
    })

    ground = result["cells"]["G1"]["ground"]
    population = ground["contributors"]["population"]
    terrain = ground["contributors"]["terrain"]
    assert ground["status"] == "passed"
    assert ground["score"] == 0.5
    assert ground["data_completeness"] == 0.8
    assert population["contribution"] == 0.5
    assert terrain["status"] == "missing_data"
    assert terrain["contribution"] is None


def test_all_missing_inputs_never_become_zero_risk():
    attributes = _attributes()
    for name in ("population", "terrain", "airspace", "traffic", "conflict"):
        attributes[name]["status"] = "missing_data"
        attributes[name]["cells"] = {grid_id: {"status": "missing_data"} for grid_id in ("G1", "G2")}

    result = RiskModelV1().evaluate(_grid(), attributes)

    first = result["cells"]["G1"]
    assert first["ground"]["status"] == "missing_data"
    assert first["ground"]["score"] is None
    assert first["air"]["status"] == "missing_data"
    assert first["overall"]["status"] == "missing_data"
    assert first["overall"]["score"] is None
    assert first["data_completeness"] == 0.0


def test_airspace_uses_max_hit_and_unknown_category_policy_is_explicit():
    attributes = _attributes()
    attributes["airspace"]["cells"]["G1"] = {
        "status": "partial_intersection",
        "airspaces": [
            {"layer_id": "L1", "feature_id": 1, "category": "CTR", "type": None, "intersection_ratio": 0.5},
            {"layer_id": "L2", "feature_id": 2, "category": "限制区", "type": None, "intersection_ratio": 0.9},
        ],
    }
    parameters = {
        "population_reference": {"value": 99.0},
        "terrain_relief_reference": {"value": 20.0},
        "airspace_category_weights": {"CTR": 0.8, "限制区": 0.2},
    }

    result = RiskModelV1().evaluate(_grid(), attributes, parameters)

    air = result["cells"]["G1"]["airspace_constraint"]
    assert air["status"] == "passed"
    assert air["score"] == 0.4
    assert air["score"] != 0.4 + 0.18
    assert air["semantics"] == "airspace_constraint_risk"

    attributes["airspace"]["cells"]["G1"]["airspaces"][0]["category"] = "未知类别"
    missing = RiskModelV1().evaluate(_grid(), attributes, parameters)["cells"]["G1"]["airspace_constraint"]
    assert missing["status"] == "missing_data"
    assert missing["score"] is None
    assert missing["contributors"][0]["status"] == "unknown_category"

    defaulted = RiskModelV1().evaluate(_grid(), attributes, {
        **parameters,
        "unknown_category_policy": "default_weight",
        "unknown_category_default_weight": 0.6,
    })["cells"]["G1"]["airspace_constraint"]
    assert defaulted["status"] == "passed"
    assert defaulted["score"] == 0.3


def test_risk_level_thresholds_are_parameterized():
    attributes = _attributes()
    attributes["traffic"]["cells"]["G1"]["traffic_density_norm"] = 0.4
    attributes["conflict"]["cells"]["G1"]["conflict_rate_norm"] = 0.4
    result = RiskModelV1().evaluate(_grid(), attributes, {
        "population_reference": {"value": 99.0},
        "terrain_relief_reference": {"value": 20.0},
        "overall_weights": {"ground": 0.0, "air": 1.0},
        "risk_levels": [
            {"min": 0.0, "max": 0.4, "level": "below"},
            {"min": 0.4, "max": 1.0, "level": "at_or_above", "include_max": True},
        ],
    })

    assert result["cells"]["G1"]["overall"]["score"] == 0.4
    assert result["cells"]["G1"]["overall"]["level"] == "at_or_above"


class ReplacementRiskModel:
    algorithm_id = "replacement-fixture"
    algorithm_version = "9.1"

    def __init__(self): self.calls = []

    def evaluate(self, grid, grid_attributes, parameters=None):
        self.calls.append((grid, grid_attributes, parameters))
        return {
            "status": "missing_data",
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "parameters": deepcopy(parameters or {}),
            "input_status": {name: value["status"] for name, value in grid_attributes.items()},
            "source_versions": {},
            "grid_level": grid["level"],
            "count": grid["count"],
            "cells": {
                cell["grid_id"]: {
                    "ground": {"status": "missing_data", "value": None, "unit": None},
                    "air": {"status": "not_calculated", "value": None, "unit": None},
                    "overall": {"status": "missing_data", "value": None, "unit": None},
                    "contributors": {},
                    "status": "missing_data",
                }
                for cell in grid["cells"]
            },
        }


def test_workflow_calls_replaceable_model_and_persists_then_stales_risk(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    model = ReplacementRiskModel()
    service = WorkflowService(store, _defaults_path(tmp_path), risk_model=model)
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    mapped = _workflow_mapped_attributes(state["grid"])

    applied = service.apply_grid_attributes(mapped)

    assert len(model.calls) == 1
    assert applied["grid_risk"]["algorithm_id"] == "replacement-fixture"
    assert applied["grid_risk"]["status"] == "missing_data"
    assert applied["risks"]["environment"]["status"] == "missing_data"
    assert WorkflowService(store, service.defaults_path).grid_risk_snapshot() == applied["grid_risk"]

    reevaluated = service.evaluate_grid_risk({"scenario": "fixture"})
    assert len(model.calls) == 2
    assert model.calls[-1][2] == {"scenario": "fixture"}
    assert reevaluated["grid_risk"]["parameters"] == {"scenario": "fixture"}

    service.invalidate_grid_attributes({"terrain"})
    service.save()
    assert service.state["grid_attributes"]["terrain"]["status"] == "stale"
    assert service.state["grid_attributes"]["population"]["status"] == "passed"
    assert service.state["grid_risk"]["status"] == "stale"
    assert service.state["result_statuses"]["environment_risk"] == "stale"


def test_new_future_source_invalidates_existing_risk_even_before_mapping(tmp_path):
    service = WorkflowService(tmp_path / "project.json", _defaults_path(tmp_path))
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    service.apply_grid_attributes(_workflow_mapped_attributes(state["grid"]))
    assert service.state["grid_attributes"]["buildings"]["status"] == "not_calculated"

    service.invalidate_grid_attributes({"buildings"})

    assert service.state["grid_attributes"]["buildings"]["status"] == "not_calculated"
    assert service.state["grid_risk"]["status"] == "stale"


def test_old_project_backfills_risk_and_future_attribute_namespaces(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    defaults = _defaults_path(tmp_path)
    service = WorkflowService(store, defaults)
    service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    document = ProjectRepository(store).load()
    document.pop("grid_risk")
    for name in ("buildings", "property_exposure", "infrastructure", "towers", "traffic", "conflict"):
        document["grid_attributes"].pop(name)
    ProjectRepository(store).save(document)

    restored = WorkflowService(store, defaults)

    assert restored.grid_risk_snapshot()["status"] == "not_calculated"
    assert {
        name: restored.grid_attributes_snapshot()[name]["status"]
        for name in ("buildings", "property_exposure", "infrastructure", "towers", "traffic", "conflict")
    } == {
        "buildings": "not_calculated",
        "property_exposure": "not_calculated",
        "infrastructure": "not_calculated",
        "towers": "not_calculated",
        "traffic": "not_calculated",
        "conflict": "not_calculated",
    }
