"""Risk Framework V2 characterization (factor → ground/air_traffic/environment_obstacle).

The suite locks the additive contract: legacy Risk V1 stays untouched, V2 ships
no production risk weight, missing/unknown never becomes zero, UAV traffic
exposure never enters Ground, and airspace never enters a V2 value or
fingerprint.
"""

from copy import deepcopy
import math
from pathlib import Path

import pytest

from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.risk_v2 import (
    DOMAIN_FACTOR_IDS, DOMAIN_IDS, FACTOR_IDS, default_risk_policy_v2,
    empty_grid_risk_v2, normalize_grid_risk_v2, normalize_risk_policy_v2,
)
from cns_planner.persistence.project_repository import ProjectRepository
from cns_planner.risk.model_v2 import GridRiskModelV2
from cns_planner.risk.v1 import RiskModelV1


# --------------------------------------------------------------------------- fixtures


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


def _attribute(name, **metadata):
    return {
        "status": "passed",
        "source": {"path": f"{name}.fixture"},
        "algorithm_id": f"{name}-mapping",
        "algorithm_version": "fixture-1",
        "grid_level": 7,
        "count": 2,
        "cells": {"G1": {"status": "passed"}, "G2": {"status": "passed"}},
        **metadata,
    }


def _attributes():
    """Canonical fixture: every V2 factor has a real canonical source."""

    population = _attribute("population", unit_status="verified_from_raster_metadata")
    population["cells"] = {
        "G1": {
            "status": "passed", "population_density_people_km2": 20.0,
            "population_count_people": 200.0, "coverage_status": "full",
            "source_coverage_fraction": 1.0,
        },
        "G2": {
            "status": "passed", "population_density_people_km2": 10.0,
            "population_count_people": 100.0, "coverage_status": "full",
            "source_coverage_fraction": 1.0,
        },
    }
    terrain = _attribute("terrain")
    terrain["cells"] = {
        "G1": {
            "status": "passed", "mean_elevation": 20.0, "min_elevation": 10.0,
            "max_elevation": 30.0, "surface_elevation_min_m": 10.0,
            "surface_elevation_max_m": 30.0, "surface_elevation_mean_m": 20.0,
        },
        "G2": {
            "status": "passed", "mean_elevation": 3.0, "min_elevation": 1.0,
            "max_elevation": 5.0, "surface_elevation_min_m": 1.0,
            "surface_elevation_max_m": 5.0, "surface_elevation_mean_m": 3.0,
        },
    }
    traffic = _attribute("traffic")
    traffic["cells"] = {
        "G1": {
            "status": "passed", "flight_count": 2, "flight_seconds": 20.0,
            "traffic_density_raw": 0.5, "traffic_density_norm": 0.5,
        },
        "G2": {
            "status": "passed", "flight_count": 1, "flight_seconds": 10.0,
            "traffic_density_raw": 0.25, "traffic_density_norm": 0.25,
        },
    }
    conflict = _attribute("conflict")
    conflict["cells"] = {
        "G1": {"status": "passed", "conflict_count": 1, "conflict_rate": 0.1, "conflict_rate_norm": 0.2},
        "G2": {"status": "passed", "conflict_count": 0, "conflict_rate": 0.0, "conflict_rate_norm": 0.0},
    }
    buildings = _attribute("buildings", metadata={"zero_semantics": "inside_declared_source_extent_without_feature"})
    buildings["cells"] = {
        "G1": {
            "status": "passed", "building_count": 4, "building_coverage_ratio": 0.4,
            "height_mean_m": 12.0, "height_p95_m": 20.0, "height_max_m": 25.0,
            "valid_height_fraction": 1.0,
        },
        "G2": {
            "status": "passed", "building_count": 0, "building_coverage_ratio": 0.0,
            "height_mean_m": None, "height_p95_m": None, "height_max_m": None,
            "valid_height_fraction": None,
        },
    }
    airspace = _attribute("airspace")
    airspace["cells"] = {
        "G1": {"status": "no_coverage", "intersected_layer_count": 0, "coverage_ratio": 0.0, "airspaces": []},
        "G2": {"status": "no_coverage", "intersected_layer_count": 0, "coverage_ratio": 0.0, "airspaces": []},
    }
    return {
        "population": population, "terrain": terrain, "traffic": traffic,
        "conflict": conflict, "buildings": buildings, "airspace": airspace,
    }


def _confirmed_environment_policy(weights=None, required=None):
    return normalize_risk_policy_v2({
        "domains": {
            "environment_obstacle": {
                "method": "weighted_sum",
                "weights": weights or {"terrain_relief": 0.5, "building_coverage": 0.5},
                "required_factors": required or ["terrain_relief", "building_coverage"],
                "source": "工程评审记录 fixture-2026-01",
                "evidence": {"review": "fixture"},
                "confirmed": True,
            },
        },
    })


def _evaluate(attributes=None, policy=None, parameters=None):
    settings = {"policy": policy} if policy is not None else {}
    settings.update(parameters or {})
    return GridRiskModelV2().evaluate(_grid(), attributes or _attributes(), settings)


def _defaults_path(tmp_path):
    target = tmp_path / "config" / "defaults.json"
    target.parent.mkdir(exist_ok=True)
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


def _workflow(tmp_path, name="project.json"):
    return WorkflowService(tmp_path / name, _defaults_path(tmp_path))


# ------------------------------------------------------------------ 1. V1 unchanged


def test_legacy_v1_outputs_and_formula_are_characterized_unchanged():
    """RiskModelV1 keeps its exact relative ground/air/overall formula."""

    attributes = _attributes()
    result = RiskModelV1().evaluate(_grid(), attributes, {
        "population_reference": {"value": 20.0},
        "terrain_relief_reference": {"value": 20.0},
        "ground_formula": {"terrain_multiplier": 0.2},
    })

    first = result["cells"]["G1"]
    assert math.isclose(first["ground"]["score"], 0.6)
    assert math.isclose(first["air"]["score"], 0.35)
    assert math.isclose(first["overall"]["score"], 0.525)
    assert result["algorithm_id"] == "risk-model-v1-relative-index"
    assert result["algorithm_version"] == "1.1"
    assert result["parameters"]["overall_weights"] == {"ground": 0.7, "air": 0.3}
    assert first["airspace_constraint"]["status"] == "not_applicable"


def test_v1_document_is_not_rewritten_by_the_v2_module():
    """V2 is additive: every V1 default parameter and input name is untouched."""

    assert RiskModelV1.default_parameters["overall_weights"] == {"ground": 0.7, "air": 0.3}
    assert RiskModelV1.default_parameters["ground_completeness_weights"]["traffic"] == 0.4
    assert "airspace" not in RiskModelV1.input_names
    assert GridRiskModelV2.algorithm_id != RiskModelV1.algorithm_id
    # The V2 result never contains a legacy ground/air/overall score.
    result = _evaluate()
    assert "ground" not in result
    assert result["overall"]["status"] == "not_configured"
    assert result["overall"]["index"] is None


# -------------------------------------------- 2. UAV traffic is Air/Traffic only


def test_uav_traffic_exposure_belongs_only_to_air_traffic_domain():
    result = _evaluate()

    traffic = result["cells"]["G1"]["factors"]["uav_traffic_exposure"]
    assert traffic["domain"] == "air_traffic"
    assert traffic["raw_value"] == 0.5
    assert traffic["normalized_index"] == 0.5
    assert "flight_count" not in traffic  # raw semantics are declared, not guessed

    ground = result["cells"]["G1"]["domains"]["ground"]
    assert "uav_traffic_exposure" not in ground["contributors"]
    assert set(ground["contributors"]) == set(DOMAIN_FACTOR_IDS["ground"])
    assert set(DOMAIN_FACTOR_IDS["ground"]) == {
        "population_exposure", "property_exposure", "critical_infrastructure_exposure",
    }
    # No ground factor may read the traffic / conflict namespaces.
    for factor_id in DOMAIN_FACTOR_IDS["ground"]:
        assert result["cells"]["G1"]["factors"][factor_id]["source_role"] not in ("traffic", "conflict")
    assert result["cells"]["G1"]["domains"]["air_traffic"]["contributors"]["uav_traffic_exposure"]["status"] == "passed"


# ------------------------------------------------- 3. raw + normalized + provenance


def test_canonical_factors_expose_raw_normalized_normalization_and_provenance():
    result = _evaluate()
    factors = result["cells"]["G1"]["factors"]

    expected_raw = {
        "population_exposure": 20.0,
        "uav_traffic_exposure": 0.5,
        "conflict_exposure": 0.1,
        "terrain_relief": 20.0,
        "building_coverage": 0.4,
        "building_height": 20.0,
    }
    expected_source_role = {
        "population_exposure": "population",
        "uav_traffic_exposure": "traffic",
        "conflict_exposure": "conflict",
        "terrain_relief": "terrain",
        "building_coverage": "buildings",
        "building_height": "buildings",
    }
    expected_method = {
        "population_exposure": "log1p_ratio_to_dataset_quantile",
        "uav_traffic_exposure": "canonical_normalized_field",
        "conflict_exposure": "canonical_normalized_field",
        "terrain_relief": "ratio_to_dataset_quantile",
        "building_coverage": "identity_ratio_0_1",
        "building_height": "ratio_to_dataset_quantile",
    }
    required_keys = {
        "factor_id", "domain", "status", "raw_value", "raw_unit", "normalized_index",
        "normalization", "source_role", "source_id", "source_fingerprint", "coverage",
        "quality_flags", "provenance",
    }
    for factor_id, raw in expected_raw.items():
        factor = factors[factor_id]
        assert required_keys <= set(factor), factor_id
        assert factor["raw_value"] == pytest.approx(raw)
        assert factor["status"] == "passed"
        assert factor["normalized_index"] is not None
        assert 0.0 <= factor["normalized_index"] <= 1.0
        assert factor["source_role"] == expected_source_role[factor_id]
        assert factor["source_id"] == f"{expected_source_role[factor_id]}.fixture"
        assert factor["source_fingerprint"].startswith("risksourcev2-")
        assert factor["normalization"]["method"] == expected_method[factor_id]
        assert factor["normalization"]["reference_fingerprint"].startswith("riskrefv2-")
        assert factor["normalization"]["semantics"] == "relative_scaling_only_not_a_safety_threshold"
        assert factor["normalization"]["output_range"] == [0.0, 1.0]
        assert factor["provenance"]["cell_status"] == "passed"
        assert factor["provenance"]["canonical_field"]

    assert factors["population_exposure"]["raw_unit"] == "people/km²"
    assert factors["terrain_relief"]["raw_unit"] == "m"
    assert factors["building_coverage"]["raw_unit"] == "ratio_0_1"
    assert "building_coverage_is_not_sheltering" in factors["building_coverage"]["quality_flags"]
    # terrain reference is the same dataset-quantile method as V1.
    assert result["references"]["terrain_relief"]["mode"] == "dataset_quantile"
    assert result["references"]["terrain_relief"]["quantile"] == 0.95
    assert result["references"]["terrain_relief"]["value"] == pytest.approx(19.2)


# ------------------------------------------------------------- 4. missing != 0


def test_missing_and_unknown_factors_never_become_zero():
    attributes = _attributes()
    attributes["traffic"]["status"] = "missing_data"
    attributes["traffic"]["cells"] = {grid_id: {"status": "missing_data"} for grid_id in ("G1", "G2")}
    attributes["population"]["cells"]["G2"] = {"status": "missing_data"}
    attributes["buildings"]["cells"]["G1"] = {"status": "outside_coverage", "reason": "outside_coverage"}

    result = _evaluate(attributes)
    factors = result["cells"]["G1"]["factors"]

    assert factors["uav_traffic_exposure"]["status"] == "missing_data"
    assert factors["uav_traffic_exposure"]["normalized_index"] is None
    assert factors["uav_traffic_exposure"]["raw_value"] is None
    assert factors["property_exposure"]["status"] == "unknown"
    assert factors["property_exposure"]["normalized_index"] is None
    assert factors["property_exposure"]["raw_value"] is None
    assert factors["critical_infrastructure_exposure"]["status"] == "unknown"
    assert factors["critical_infrastructure_exposure"]["normalized_index"] is None
    assert factors["building_coverage"]["status"] == "missing_data"
    assert factors["building_coverage"]["normalized_index"] is None

    g2 = result["cells"]["G2"]["factors"]
    assert g2["population_exposure"]["status"] == "missing_data"
    assert g2["population_exposure"]["normalized_index"] is None
    # A confirmed zero is still a real zero (building coverage 0 on G2).
    assert result["cells"]["G2"]["factors"]["building_coverage"]["status"] == "passed"
    assert result["cells"]["G2"]["factors"]["building_coverage"]["normalized_index"] == 0.0
    assert result["cells"]["G2"]["factors"]["conflict_exposure"]["normalized_index"] == 0.0


# ------------------------------------------------------ 5. partial building height


def test_partial_building_heights_stay_partial_and_unresolved():
    attributes = _attributes()
    partial = deepcopy(attributes)
    partial["buildings"]["cells"]["G1"]["valid_height_fraction"] = 0.6
    partial["buildings"]["cells"]["G2"]["valid_height_fraction"] = 0.5

    result = _evaluate(partial)
    height = result["cells"]["G1"]["factors"]["building_height"]

    assert height["status"] == "partial"
    assert height["resolved"] is False
    assert height["normalized_index"] is not None  # measured heights are kept, never zeroed
    assert "partial_building_height_coverage" in height["quality_flags"]
    assert height["coverage"] == pytest.approx(0.6)
    # Coverage stays independently valid: coverage is not a sheltering proxy.
    assert result["cells"]["G1"]["factors"]["building_coverage"]["status"] == "passed"

    policy = normalize_risk_policy_v2({
        "domains": {
            "environment_obstacle": {
                "method": "weighted_sum",
                "weights": {"building_coverage": 0.5, "building_height": 0.5},
                "required_factors": ["building_coverage", "building_height"],
                "source": "工程评审记录 fixture-2026-01",
                "confirmed": True,
            },
        },
    })
    domain = _evaluate(partial, policy=policy)["cells"]["G1"]["domains"]["environment_obstacle"]
    assert domain["status"] == "unresolved"
    assert domain["index"] is None
    assert domain["unresolved"] == ["building_height"]
    assert domain["contributors"]["building_coverage"]["included"] is True
    assert domain["contributors"]["building_height"]["included"] is False

    unknown_fraction = _attributes()
    unknown_fraction["buildings"]["cells"]["G1"]["valid_height_fraction"] = None
    unresolved = _evaluate(unknown_fraction)["cells"]["G1"]["factors"]["building_height"]
    assert unresolved["status"] == "partial"
    assert unresolved["resolved"] is False
    assert "valid_height_fraction_unknown" in unresolved["quality_flags"]


# ------------------------------------------- 6. no confirmed policy => null index


def test_without_confirmed_policy_factors_are_ready_but_domain_index_is_null():
    result = _evaluate()

    assert result["policy"]["status"] == "pending_confirmation"
    assert result["policy"]["parameter_status"] == "no_default_production_risk_weights"
    assert result["policy"]["domains"]["ground"]["weights"] == {}
    assert result["policy"]["domains"]["ground"]["confirmed"] is False

    assert result["factor_status"]["population_exposure"]["status"] == "passed"
    assert result["factor_status"]["population_exposure"]["readiness"] == "ready"

    for domain_id in DOMAIN_IDS:
        domain = result["cells"]["G1"]["domains"][domain_id]
        assert domain["status"] == "pending_confirmation"
        assert domain["index"] is None
        assert domain["data_completeness"] == 0.0
        assert domain["reason"] == "aggregation_policy_pending_confirmation"
        assert domain["aggregation_policy_fingerprint"].startswith("riskaggv2-")
        assert result["domains"][domain_id]["index"] is None
        assert result["domains"][domain_id]["index_scope"] == "per_cell_only"
    assert result["status"] == "pending_confirmation"

    # The shipped default policy has no weight at all.
    assert default_risk_policy_v2()["domains"]["ground"]["weights"] == {}
    assert default_risk_policy_v2()["overall"]["status"] == "not_configured"


# ------------------------------------------------ 7. explicit confirmed weights


def test_explicit_confirmed_weights_produce_a_domain_index():
    policy = _confirmed_environment_policy()
    assert policy["domains"]["environment_obstacle"]["status"] == "confirmed"

    result = _evaluate(policy=policy)
    first = result["cells"]["G1"]["domains"]["environment_obstacle"]
    second = result["cells"]["G2"]["domains"]["environment_obstacle"]

    assert result["policy_fingerprint"].startswith("riskpolicyv2-")
    assert first["status"] == "passed"
    assert first["index"] == pytest.approx(0.7)
    assert first["data_completeness"] == pytest.approx(1.0)
    assert first["unresolved"] == []
    assert first["contributors"]["terrain_relief"]["weight"] == 0.5
    assert first["contributors"]["terrain_relief"]["contribution"] == pytest.approx(0.5)
    assert first["contributors"]["building_coverage"]["contribution"] == pytest.approx(0.2)
    assert first["semantics"]["no_automatic_renormalization"] is True
    assert first["semantics"]["engineering_internal_domain"] is True
    assert second["status"] == "passed"
    assert second["index"] == pytest.approx(0.1042, abs=1e-3)
    # Only environment_obstacle is confirmed; the other two domains stay pending,
    # so the cell-level status is still pending_confirmation (never a fake pass).
    assert result["cells"]["G1"]["status"] == "pending_confirmation"
    assert result["cells"]["G1"]["domains"]["ground"]["index"] is None

    # Levels stay unconfigured unless the caller supplies explicit bands.
    assert first["level"] is None
    banded = _evaluate(policy=policy, parameters={
        "risk_levels_by_domain": {"environment_obstacle": [
            {"min": 0.0, "max": 0.5, "level": "band_low"},
            {"min": 0.5, "max": 1.0, "level": "band_high", "include_max": True},
        ]},
    })["cells"]["G1"]["domains"]["environment_obstacle"]
    assert banded["level"] == "band_high"


# ------------------------------- 8. missing required factor is never re-normalized


def test_missing_required_factor_is_unresolved_and_weights_are_not_renormalized():
    attributes = _attributes()
    attributes["terrain"]["cells"]["G1"] = {"status": "missing_data"}

    result = _evaluate(attributes, policy=_confirmed_environment_policy())
    domain = result["cells"]["G1"]["domains"]["environment_obstacle"]

    assert domain["status"] == "unresolved"
    assert domain["index"] is None
    assert domain["unresolved"] == ["terrain_relief"]
    assert domain["missing_required_factors"] == ["terrain_relief"]
    assert domain["reason"] == "missing_required_factors"
    assert domain["data_completeness"] == pytest.approx(0.5)
    # Re-normalizing to the available 0.5 building-coverage weight would have produced 0.2.
    assert domain["contributors"]["terrain_relief"]["weight"] == 0.5
    assert domain["contributors"]["terrain_relief"]["included"] is False
    assert domain["contributors"]["terrain_relief"]["contribution"] is None
    assert domain["contributors"]["building_coverage"]["included"] is True
    assert domain["semantics"]["missing_is_not_zero"] is True


def test_non_required_missing_factor_also_blocks_instead_of_being_dropped():
    policy = _confirmed_environment_policy(
        weights={"terrain_relief": 0.5, "building_coverage": 0.5},
        required=["building_coverage"],
    )
    attributes = _attributes()
    attributes["terrain"]["cells"]["G1"] = {"status": "missing_data"}

    domain = _evaluate(attributes, policy=policy)["cells"]["G1"]["domains"]["environment_obstacle"]
    assert domain["status"] == "unresolved"
    assert domain["index"] is None
    assert domain["missing_required_factors"] == []
    assert domain["reason"] == "missing_weighted_factor_not_renormalized"
    assert domain["unresolved"] == ["terrain_relief"]


# ------------------------------------------------- 9. invalid weights are rejected


@pytest.mark.parametrize("payload,reason", [
    ({"method": "weighted_sum", "weights": {"population_exposure": 0.5},
      "required_factors": ["population_exposure"], "source": "x", "confirmed": True}, "non_unit_sum"),
    ({"method": "weighted_sum", "weights": {"population_exposure": 0.5, "terrain_relief": 0.7},
      "required_factors": ["population_exposure"], "source": "x", "confirmed": True}, "non_unit_sum_over"),
    ({"method": "weighted_sum", "weights": {"population_exposure": -0.5, "terrain_relief": 1.5},
      "required_factors": ["population_exposure"], "source": "x", "confirmed": True}, "negative_weight"),
    ({"method": "average", "weights": {}, "required_factors": [],
      "source": "x", "confirmed": True}, "unknown_method"),
    ({"method": "weighted_sum", "weights": {"unknown_factor": 1.0},
      "required_factors": [], "source": "x", "confirmed": True}, "unknown_factor"),
    ({"method": "weighted_sum", "weights": {"population_exposure": 1.0},
      "required_factors": [], "source": "x", "confirmed": True}, "missing_required_factor"),
    ({"method": "weighted_sum", "weights": {"population_exposure": 1.0},
      "required_factors": ["terrain_relief"], "source": "x", "confirmed": True}, "required_without_weight"),
    ({"method": "weighted_sum", "weights": {"population_exposure": 1.0},
      "required_factors": ["population_exposure"], "source": "", "confirmed": True}, "confirmed_without_source"),
    ({"method": None, "weights": {"population_exposure": 1.0},
      "required_factors": ["population_exposure"], "source": "x", "confirmed": True}, "weights_without_method"),
])
def test_invalid_or_non_sum_weights_are_rejected(payload, reason):
    with pytest.raises(ValueError):
        normalize_risk_policy_v2({"domains": {"ground": payload}})


def test_weights_are_never_silently_normalized():
    """A 0.5 + 0.2 policy must raise instead of being scaled to 0.71/0.29."""

    with pytest.raises(ValueError, match="权重和"):
        normalize_risk_policy_v2({"domains": {"environment_obstacle": {
            "method": "weighted_sum",
            "weights": {"terrain_relief": 0.5, "building_height": 0.2},
            "required_factors": ["terrain_relief", "building_height"],
            "source": "工程评审记录 fixture-2026-01",
            "confirmed": True,
        }}})


# ------------------------------------------------ 10. airspace stays display-only


def test_airspace_never_enters_a_v2_value_or_fingerprint():
    baseline = _evaluate()

    attributes = _attributes()
    attributes["airspace"]["cells"]["G1"] = {
        "status": "partial_intersection",
        "intersected_layer_count": 2,
        "coverage_ratio": 0.7,
        "airspaces": [
            {"layer_id": "L1", "feature_id": 1, "category": "CTR", "type": None, "intersection_ratio": 0.5},
            {"layer_id": "L2", "feature_id": 2, "category": "限制区", "type": None, "intersection_ratio": 0.9},
        ],
    }
    changed = _evaluate(attributes)

    assert changed == baseline
    assert "airspace" not in changed["input_status"]
    assert "airspace" not in changed["source_versions"]
    assert "airspace" not in baseline["input_fingerprint"]
    assert changed["input_fingerprint"] == baseline["input_fingerprint"]
    assert changed["airspace"]["status"] == "not_applicable"
    assert changed["airspace"]["applicability"] == "display_only"
    assert changed["airspace"]["used_in_value_or_fingerprint"] is False
    assert "airspace" not in {factor["domain"] for factor in changed["cells"]["G1"]["factors"].values()}
    assert "airspace" not in "".join(FACTOR_IDS)


# -------------------------------------------------------- 11. not_computed models


def test_absolute_risk_and_sora_models_are_not_computed():
    result = _evaluate()

    assert result["risk_semantics"] == "relative_engineering_index"
    assert result["absolute_risk"]["status"] == "not_computed"
    assert result["absolute_risk"]["value"] is None
    assert result["sora_grc"]["status"] == "not_computed"
    assert result["sora_grc"]["value"] is None
    assert result["sora_arc"]["status"] == "not_computed"
    assert result["sora_arc"]["value"] is None
    for model in ("absolute_risk", "sora_grc", "sora_arc"):
        assert "verified" in result[model]["reason"]
        assert result[model]["required_models"]
    assert result["semantics"]["not_accident_probability"] is True
    assert result["semantics"]["not_sora_grc"] is True
    assert result["semantics"]["not_sora_arc"] is True
    # No default cross-domain overall weight (0.7 / 0.3) exists anywhere.
    assert result["overall"]["status"] == "not_configured"
    assert result["overall"]["index"] is None
    assert "0.7" not in str(result["overall"])


# ---------------------------------------- 12. V2 never stales routes / CNS results


def test_risk_v2_change_never_stales_current_routes_or_cns(tmp_path):
    service = _workflow(tmp_path)
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    service.state["grid_attributes"] = _attributes()
    service.state["grid"] = _grid()
    service.state["operational_routes"] = [{"route_id": "R0001", "status": "passed", "path": [[120.001, 30.001]]}]
    service.state["result_statuses"].update({
        "routes": "passed", "coverage": "passed", "cns_gap": "passed",
        "cns_gap_v2": "passed", "cns_service_capability": "passed",
        "service_timeline": "passed", "environment_risk": "passed",
        "coverage_3d": "passed", "report": "passed",
    })
    service.state["grid_risk"] = {"status": "passed", "algorithm_id": "risk-model-v1-relative-index", "cells": {}}
    service.state["coverage_3d"] = {"status": "passed"}
    service.evaluate_grid_risk_v2()
    assert service.state["grid_risk_v2"]["status"] == "pending_confirmation"

    service.invalidation_service.risk_v2("fixture_reason")
    assert service.state["grid_risk_v2"]["status"] == "stale"
    assert service.state["result_statuses"]["grid_risk_v2"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"
    assert service.state["result_statuses"]["coverage_3d"] == "passed"
    assert service.state["result_statuses"]["cns_gap_v2"] == "passed"
    assert service.state["result_statuses"]["environment_risk"] == "passed"
    assert service.state["result_statuses"]["report"] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"
    assert service.state["grid_risk"]["status"] == "passed"
    assert service.state["coverage_3d"]["status"] == "passed"

    # A policy change only stales grid_risk_v2.
    service.set_risk_policy_v2({"domains": {}})
    assert service.state["result_statuses"]["routes"] == "passed"
    assert service.state["result_statuses"]["cns_gap_v2"] == "passed"
    assert service.state["grid_risk"]["status"] == "passed"
    assert service.state["risk_policy_v2"]["status"] == "pending_confirmation"


def test_source_change_stales_and_recomputes_v2_without_touching_routes(tmp_path):
    service = _workflow(tmp_path, "source-change.json")
    service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    service.state["grid_attributes"] = _attributes()
    service.state["grid"] = _grid()
    service.state["result_statuses"]["routes"] = "passed"
    service.state["operational_routes"] = [{"route_id": "R0001", "status": "passed"}]
    service.evaluate_grid_risk_v2()
    service.save()

    service.invalidate_grid_attributes({"terrain"})

    assert service.state["grid_attributes"]["terrain"]["status"] == "stale"
    assert service.state["grid_risk_v2"]["status"] == "stale"
    assert service.state["result_statuses"]["grid_risk_v2"] == "stale"
    assert service.state["result_statuses"]["routes"] == "passed"
    assert service.state["operational_routes"][0]["status"] == "passed"


def test_apply_grid_attributes_and_traffic_simulation_recompute_v2(tmp_path):
    service = _workflow(tmp_path, "recompute.json")
    state = service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    grid_ids = [cell["grid_id"] for cell in state["grid"]["cells"]]
    mapped = _attributes()
    for name in ("population", "terrain", "airspace", "traffic", "conflict", "buildings"):
        mapped[name]["cells"] = {}
    for index, grid_id in enumerate(grid_ids):
        mapped["population"]["cells"][grid_id] = {
            "status": "passed", "population_density_people_km2": 10.0 + index,
            "population_count_people": 100.0 + index, "coverage_status": "full",
            "source_coverage_fraction": 1.0,
        }
        mapped["terrain"]["cells"][grid_id] = {
            "status": "passed", "mean_elevation": 5.0, "min_elevation": 0.0,
            "max_elevation": 10.0 + index, "surface_elevation_min_m": 0.0,
            "surface_elevation_max_m": 10.0 + index, "surface_elevation_mean_m": 5.0,
        }
        mapped["traffic"]["cells"][grid_id] = {
            "status": "passed", "flight_count": 1, "flight_seconds": 10.0,
            "traffic_density_raw": 0.1 + index, "traffic_density_norm": 0.1,
        }
        mapped["conflict"]["cells"][grid_id] = {
            "status": "passed", "conflict_count": 0, "conflict_rate": 0.0,
            "conflict_rate_norm": 0.0,
        }
        mapped["buildings"]["cells"][grid_id] = {
            "status": "passed", "building_count": 1, "building_coverage_ratio": 0.2,
            "height_mean_m": 8.0, "height_p95_m": 10.0, "height_max_m": 12.0,
            "valid_height_fraction": 1.0,
        }
        mapped["airspace"]["cells"][grid_id] = {
            "status": "no_coverage", "intersected_layer_count": 0,
            "coverage_ratio": 0.0, "airspaces": [],
        }
    for name in ("population", "terrain", "airspace", "traffic", "conflict", "buildings"):
        mapped[name]["grid_level"] = state["grid"]["level"]
        mapped[name]["count"] = len(grid_ids)

    applied = service.apply_grid_attributes(mapped)

    assert applied["grid_risk_v2"]["count"] == len(grid_ids)
    assert applied["grid_risk_v2"]["status"] == "pending_confirmation"
    assert applied["grid_risk_v2"]["input_fingerprint"].startswith("riskframeworkv2-")
    assert applied["grid_risk_v2"]["factor_status"]["population_exposure"]["cell_count"] == len(grid_ids)
    assert applied["grid_risk_v2"]["factor_status"]["population_exposure"]["status"] == "passed"
    assert applied["grid_risk_v2"]["factor_status"]["building_height"]["status"] == "passed"
    assert applied["result_statuses"]["grid_risk_v2"] == "pending_confirmation"
    # Legacy V1 still runs unchanged on the same inputs.
    assert applied["grid_risk"]["algorithm_id"] == "risk-model-v1-relative-index"


# -------------------------------------------------------- 13. legacy backfill


def test_legacy_project_backfills_risk_v2_keys(tmp_path):
    store = tmp_path / "project" / "project_state.json"
    defaults = _defaults_path(tmp_path)
    service = WorkflowService(store, defaults)
    service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    document = ProjectRepository(store).load()
    document.pop("grid_risk_v2")
    document.pop("risk_policy_v2")
    document["result_statuses"].pop("grid_risk_v2", None)
    ProjectRepository(store).save(document)

    restored = WorkflowService(store, defaults)

    policy = restored.risk_policy_v2_snapshot()
    result = restored.grid_risk_v2_snapshot()
    assert policy["status"] == "pending_confirmation"
    assert policy["domains"]["ground"]["weights"] == {}
    assert policy["parameter_status"] == "no_default_production_risk_weights"
    assert result["status"] == "not_calculated"
    assert result["risk_semantics"] == "relative_engineering_index"
    assert restored.state["result_statuses"]["grid_risk_v2"] == "not_calculated"
    # Legacy Risk V1 keys survive unchanged.
    assert restored.grid_risk_snapshot()["algorithm_id"] == "risk-model-v1-relative-index"

    restored.save()
    reopened = WorkflowService(store, defaults)
    assert reopened.risk_policy_v2_snapshot() == policy
    assert reopened.grid_risk_v2_snapshot() == result


def test_v2_normalizers_are_idempotent():
    policy = _confirmed_environment_policy()
    assert normalize_risk_policy_v2(policy) == policy
    result = _evaluate(policy=policy)
    assert normalize_grid_risk_v2(result) == result
    empty = empty_grid_risk_v2()
    assert normalize_grid_risk_v2(empty) == empty
    assert normalize_risk_policy_v2(default_risk_policy_v2()) == default_risk_policy_v2()


def test_readiness_reports_factors_domains_policy_and_fingerprints(tmp_path):
    service = _workflow(tmp_path, "readiness.json")
    service.set_workspace([120.001, 30.001, 120.02, 30.02], _health())
    service.state["grid_attributes"] = _attributes()
    service.state["grid"] = _grid()
    service.evaluate_grid_risk_v2()

    readiness = service.risk_framework_v2_readiness()

    assert readiness["risk_semantics"] == "relative_engineering_index"
    assert readiness["policy"]["default_production_risk_weights"] is False
    assert readiness["policy"]["status"] == "pending_confirmation"
    assert set(readiness["factors"]) == set(FACTOR_IDS)
    assert readiness["factors"]["population_exposure"]["readiness"] == "ready"
    assert readiness["factors"]["property_exposure"]["canonical_source_available"] is False
    assert readiness["factors"]["building_height"]["status"] in ("passed", "partial")
    assert set(readiness["domains"]) == set(DOMAIN_IDS)
    ground = readiness["domains"]["ground"]
    assert ground["status"] == "pending_confirmation"
    assert ground["index"] is None
    assert ground["index_scope"] == "per_cell_only"
    assert ground["aggregation_policy_fingerprint"].startswith("riskaggv2-")
    assert readiness["not_computed"]["absolute_risk"]["status"] == "not_computed"
    assert readiness["airspace"]["applicability"] == "display_only"


def test_api_surface_uses_the_existing_workflow_naming(tmp_path):
    service = _workflow(tmp_path, "api.json")
    assert hasattr(service, "grid_risk_v2_snapshot")
    assert hasattr(service, "risk_policy_v2_snapshot")
    assert hasattr(service, "risk_framework_v2_readiness")
    assert hasattr(service, "set_risk_policy_v2")
    assert hasattr(service, "evaluate_grid_risk_v2")
    source = Path("cns_planner/api/router.py").read_text(encoding="utf-8")
    for route in (
        "/api/grid-risk-v2", "/api/risk-policy-v2", "/api/risk-framework-v2/readiness",
        "/api/grid-risk-v2/evaluate",
    ):
        assert route in source
    # The V2 API must not reuse or shadow the legacy risk routes.
    assert '"/api/grid-risk"' not in source
