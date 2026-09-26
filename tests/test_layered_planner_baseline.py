"""Theta* V2 production-baseline migration and search-parameter provenance tests.

Covers the two debts closed in this round:

* the default ``layered_route_planner`` was promoted from Layered Planner V1 to
  ``layered_risk_aware_theta_star_v2`` — new projects get V2, legacy projects without a saved
  selection are backfilled to V2, and a project that explicitly saved V1 keeps running V1;
* the previously invisible planner constants ``heading_bin_count = 8`` and
  ``theta_min_deg = 5.0`` are now explicit, persisted, auditable search parameters with a
  ``software_algorithm_baseline_not_engineering_confirmed`` provenance.

The Theta* mathematics itself is deliberately unchanged: the default parameters and the same
parameters written explicitly through an algorithm selection produce the identical candidate.
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.algorithms.registry import (
    build_default_algorithm_registry, default_algorithm_selection,
    normalize_algorithm_selection,
)
from cns_planner.application.project_state import blank_project, normalize_project
from cns_planner.domain.layered_route import (
    normalize_layered_route_feasibility_policy, normalize_layered_route_request,
    resolve_cruise_altitude,
)
from cns_planner.domain.layered_theta_v2 import (
    ALGORITHM_ID, ALGORITHM_VERSION, DEFAULT_HEADING_BIN_COUNT, DEFAULT_THETA_MIN_DEG,
    EXPLICIT_PARAMETER_ORIGIN, SOFTWARE_ALGORITHM_BASELINE_SOURCE,
    SOFTWARE_BASELINE_PARAMETER_ORIGIN, SEARCH_PARAMETER_PURPOSE,
    default_theta_v2_search_parameter_provenance,
    normalize_theta_v2_search_parameters, search_parameter_fingerprint,
    theta_v2_search_parameter_view,
)
from cns_planner.domain.population_shelter import (
    resolve_population_shelter, user_defined_baseline_policy,
)
from cns_planner.layered_route_planner.planner import (
    LayeredRoutePlannerV1, build_layer_feasibility_mask,
)
from cns_planner.layered_route_planner.theta_star_v2 import LayeredRiskAwareThetaStarV2

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"
LAYER_ID = "L8-LOW"
ALTITUDE_H = 300.0
LEVEL = 8


# --------------------------------------------------------------------------------------
# 1-3. default selection, legacy backfill and explicit V1 preservation
# --------------------------------------------------------------------------------------


def test_blank_project_ships_theta_star_v2_as_the_layered_planner_default():
    project = blank_project({})
    selection = project["algorithm_selection"]["layered_route_planner"]
    assert selection["algorithm_type"] == "layered_route_planner"
    assert selection["algorithm_id"] == ALGORITHM_ID
    assert selection["version"] == ALGORITHM_VERSION
    # B7X：新项目不再持久化 legacy ``route_planner`` selection；旧项目保存的旧值
    # 由 CompatibilitySelectionAdapter 原样解析（见 test_phase4_b7x_compatibility）。
    assert "route_planner" not in project["algorithm_selection"]


def test_legacy_project_without_a_layered_selection_is_backfilled_to_theta_star_v2():
    legacy = blank_project({})
    legacy["algorithm_selection"].pop("layered_route_planner", None)
    normalized = normalize_project(deepcopy(legacy), WorkspaceGridService())
    selection = normalized["algorithm_selection"]["layered_route_planner"]
    assert selection["algorithm_id"] == ALGORITHM_ID
    assert selection["version"] == ALGORITHM_VERSION
    assert selection["parameters"]["heading_bin_count"] == DEFAULT_HEADING_BIN_COUNT
    assert selection["parameters"]["theta_min_deg"] == DEFAULT_THETA_MIN_DEG
    assert selection["parameters"]["max_expanded_labels"] is None
    # Idempotent: a second normalization never drifts.
    again = normalize_project(deepcopy(normalized), WorkspaceGridService())
    assert again["algorithm_selection"] == normalized["algorithm_selection"]


def test_existing_project_with_an_explicit_v1_selection_keeps_running_v1():
    project = blank_project({})
    project["algorithm_selection"]["layered_route_planner"] = {
        "algorithm_type": "layered_route_planner",
        "algorithm_id": LayeredRoutePlannerV1.algorithm_id,
        "version": LayeredRoutePlannerV1.algorithm_version,
        "parameters": {},
    }
    normalized = normalize_project(deepcopy(project), WorkspaceGridService())
    selection = normalized["algorithm_selection"]["layered_route_planner"]
    # No silent migration: an explicitly saved V1 stays V1, with its parameters untouched.
    assert selection["algorithm_id"] == "layered_route_planner_v1"
    assert selection["version"] == "1.0"
    assert selection["parameters"] == {}
    registry = build_default_algorithm_registry({})
    instance = registry.create(
        selection["algorithm_type"], selection["algorithm_id"],
        selection["version"], selection["parameters"],
    )
    assert instance.algorithm_id == "layered_route_planner_v1"
    assert getattr(instance, "uses_theta_star", False) is False


def test_v1_remains_registered_and_explicitly_selectable_as_the_legacy_baseline():
    registry = build_default_algorithm_registry({})
    manifests = {item.algorithm_id: item for item in registry.manifests("layered_route_planner")}
    assert set(manifests) == {"layered_route_planner_v1", ALGORITHM_ID}
    v1_manifest = manifests["layered_route_planner_v1"]
    assert v1_manifest.version == "1.0"
    assert "legacy/baseline" in v1_manifest.name
    assert any("legacy/baseline" in item for item in v1_manifest.assumptions)
    assert any("Theta* V2" in item for item in v1_manifest.assumptions)
    # The promoted default documents itself as the production layered baseline and explains
    # that 8 / 5 are a software baseline, not engineering-confirmed aviation parameters.
    v2_manifest = manifests[ALGORITHM_ID]
    assert "production layered planning baseline" in v2_manifest.description
    joined = " ".join(v2_manifest.assumptions)
    assert "software_algorithm_baseline_not_engineering_confirmed" in joined
    assert "不是航空器最小转弯角" in joined
    assert "search_parameter_provenance" in v2_manifest.parameter_schema["properties"]


# --------------------------------------------------------------------------------------
# 4-6. the closed "invisible 8 / 5 constant" debt and its provenance
# --------------------------------------------------------------------------------------


def test_default_selection_carries_explicit_parameters_and_software_baseline_provenance():
    parameters = default_algorithm_selection()["layered_route_planner"]["parameters"]
    assert parameters["heading_bin_count"] == 8
    assert parameters["theta_min_deg"] == 5.0
    assert parameters["max_expanded_labels"] is None
    provenance = parameters["search_parameter_provenance"]
    assert provenance["source"] == SOFTWARE_ALGORITHM_BASELINE_SOURCE
    assert provenance["parameter_origin"] == SOFTWARE_BASELINE_PARAMETER_ORIGIN
    assert provenance["purpose"] == SEARCH_PARAMETER_PURPOSE
    assert provenance["evidence"] is None
    assert provenance["engineering_confirmed"] is False
    assert provenance["engineering_boundary"][
        "software_algorithm_baseline_not_engineering_confirmed"
    ] is True
    assert provenance["engineering_boundary"][
        "theta_min_deg_is_not_aircraft_minimum_turn_angle"
    ] is True
    assert provenance["engineering_boundary"]["d_ref_m_is_not_aircraft_turn_radius"] is True


def test_legacy_theta_v2_selection_without_parameters_is_backfilled_with_provenance():
    normalized = normalize_algorithm_selection({
        "layered_route_planner": {
            "algorithm_type": "layered_route_planner",
            "algorithm_id": ALGORITHM_ID, "version": ALGORITHM_VERSION,
        },
    })["layered_route_planner"]
    parameters = normalized["parameters"]
    assert parameters["heading_bin_count"] == 8
    assert parameters["theta_min_deg"] == 5.0
    # Normalization records the provenance explicitly instead of leaving it implicit.
    assert parameters["search_parameter_provenance"]["parameter_origin"] == (
        SOFTWARE_BASELINE_PARAMETER_ORIGIN
    )
    assert normalize_algorithm_selection({
        "layered_route_planner": normalized,
    })["layered_route_planner"] == normalized


def test_software_baseline_is_never_auto_upgraded_to_engineering_confirmed():
    baseline = normalize_theta_v2_search_parameters({})
    provenance = baseline["search_parameter_provenance"]
    assert provenance["engineering_confirmed"] is False
    assert provenance["evidence"] is None
    # Claiming engineering confirmation without evidence is refused.
    claimed = normalize_theta_v2_search_parameters({
        "engineering_confirmed": True, "confirmed": True,
        "search_parameter_provenance": {"engineering_confirmed": True, "confirmed": True},
    })
    assert claimed["search_parameter_provenance"]["engineering_confirmed"] is False
    # Only an explicit statement + evidence + confirmation may set it.
    confirmed = normalize_theta_v2_search_parameters({
        "search_parameter_provenance": {
            "engineering_confirmed": True, "confirmed": True,
            "evidence": {"statement": "工程评审记录", "reference": "ENG-2026-001"},
            "source": "engineering_review",
        },
    })
    assert confirmed["search_parameter_provenance"]["engineering_confirmed"] is True
    assert confirmed["search_parameter_provenance"]["evidence"]["reference"] == "ENG-2026-001"
    assert default_theta_v2_search_parameter_provenance()["engineering_confirmed"] is False


def test_explicit_override_marks_explicit_origin_and_enters_the_fingerprint():
    baseline = normalize_theta_v2_search_parameters({})
    baseline_view = theta_v2_search_parameter_view(baseline)
    assert baseline_view["parameter_origin"] == SOFTWARE_BASELINE_PARAMETER_ORIGIN
    assert baseline_view["software_algorithm_baseline"] is True

    overridden = normalize_theta_v2_search_parameters({"heading_bin_count": 12})
    view = theta_v2_search_parameter_view(overridden)
    assert view["parameter_origin"] == EXPLICIT_PARAMETER_ORIGIN
    assert view["software_algorithm_baseline"] is False
    # A changed value can never keep claiming to be the untouched software baseline, and the
    # provenance override is visible in the readiness/candidate view and in the fingerprint.
    assert view["provenance"]["parameter_origin"] == EXPLICIT_PARAMETER_ORIGIN
    assert view["engineering_confirmed"] is False
    assert view["fingerprint"] != baseline_view["fingerprint"]
    assert search_parameter_fingerprint({"theta_min_deg": 0.0}) != baseline_view["fingerprint"]

    # Theta* V2 itself exposes the same view: overrides reach the planner, not just the form.
    instance = LayeredRiskAwareThetaStarV2({"heading_bin_count": 12, "theta_min_deg": 0.0})
    assert instance.parameters["heading_bin_count"] == 12
    assert instance.parameters["theta_min_deg"] == 0.0
    assert instance.search_parameter_provenance["parameter_origin"] == (
        EXPLICIT_PARAMETER_ORIGIN
    )
    assert instance.parameters["search_parameter_provenance"]["engineering_confirmed"] is False


# --------------------------------------------------------------------------------------
# 7-8. the Theta* characterization itself is unchanged
# --------------------------------------------------------------------------------------


def grid_cells(columns=6, rows=5):
    cells = []
    for column in range(columns):
        for row in range(rows):
            west, south = 122.0 + 0.01 * column, 30.0 + 0.01 * row
            cells.append({
                "grid_id": f"MHT4063-L{LEVEL}-C{column}-RP{row}", "level": LEVEL,
                "column": column, "row": row,
                "bbox": [west, south, west + 0.01, south + 0.01],
                "center": [west + 0.005, south + 0.005],
            })
    return cells


def fingerprint_inputs():
    return {
        "request": normalize_layered_route_request({
            "scenario_route_id": "R0001", "altitude_layer_id": LAYER_ID,
            "source": "工程确认-测试", "confirmed": True,
        }),
        "scenario_route": {"route_id": "R0001", "start": [122.0, 30.0], "end": [122.05, 30.03]},
        "grid": {"level": LEVEL, "cells": grid_cells()},
        "layer_mask": {"status": "passed", "cells": {}, "mask_fingerprint": "mask"},
        "grid_risk_v2": {"status": "passed", "input_fingerprint": "in", "policy_fingerprint": "pol"},
        "objective_policy": None, "risk_density_constraint": None,
        "population_shelter": {"status": "passed", "cells": {}},
        "shelter_policy": user_defined_baseline_policy(),
        "regulatory_constraints": None, "hard_constraints": [],
        "building_clearance_policy": {}, "feasibility_policy": None, "source_audits": {},
    }


def plan_math(parameters):
    """One real Theta* V2 planning run on a small deterministic fixture."""

    grid = grid_cells()
    cells = [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": 10.0,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": {
                "data_status": "passed", "building_count": 0,
                "height_max_m": None, "valid_height_fraction": None,
            },
        }
        for cell in grid
    ]
    mask = build_layer_feasibility_mask(
        request=fingerprint_inputs()["request"],
        cruise_altitude=resolve_cruise_altitude({
            "altitude_layer_id": LAYER_ID, "name": "低层",
            "nominal_altitude_m": ALTITUDE_H, "lower_altitude_m": 250.0,
            "upper_altitude_m": 350.0, "vertical_reference": "egm2008_orthometric",
            "source": "工程确认-测试", "evidence": {"note": "unit test"},
            "confirmed": True, "status": "confirmed",
        }),
        cells=cells,
        feasibility_policy=normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 50.0, "source": "工程确认-测试", "confirmed": True,
        }),
        building_clearance_policy={
            "status": "confirmed", "horizontal_clearance_m": 0.0,
            "vertical_clearance_m": 10.0, "source": "工程确认-测试", "confirmed": True,
        },
        source_audits={}, grid_level=LEVEL,
    )
    shelter = resolve_population_shelter(
        grid={"level": LEVEL, "cells": grid},
        population_attribute={
            "status": "passed", "cells": {
                cell["grid_id"]: {
                    "status": "passed", "population_density_people_km2": 100.0,
                    "source_coverage_fraction": 1.0, "coverage_status": "covered",
                } for cell in grid
            },
        },
        normalized_population_factors={cell["grid_id"]: 0.5 for cell in grid},
        policy=user_defined_baseline_policy(),
    )
    return LayeredRiskAwareThetaStarV2(parameters).plan(
        request=fingerprint_inputs()["request"],
        scenario_route=fingerprint_inputs()["scenario_route"],
        grid={"level": LEVEL, "cells": grid},
        layer_mask=mask,
        grid_risk_v2=fingerprint_inputs()["grid_risk_v2"],
        feasibility_policy=normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 50.0, "source": "工程确认-测试", "confirmed": True,
        }),
        population_shelter=shelter,
        shelter_policy=user_defined_baseline_policy(),
        building_clearance_policy={
            "status": "confirmed", "horizontal_clearance_m": 0.0,
            "vertical_clearance_m": 10.0, "source": "工程确认-测试", "confirmed": True,
        },
    )


def test_default_parameters_and_explicitly_written_baseline_are_the_same_planner():
    """Characterization: promoting V2 did not change the Theta* mathematics."""

    implicit = plan_math({})
    explicit = plan_math({
        "heading_bin_count": DEFAULT_HEADING_BIN_COUNT,
        "theta_min_deg": DEFAULT_THETA_MIN_DEG,
        "max_expanded_labels": None,
        "search_parameter_provenance": default_theta_v2_search_parameter_provenance(),
    })
    assert implicit["status"] == explicit["status"] == "candidate"
    assert implicit["grid_path"] == explicit["grid_path"]
    assert implicit["path"] == explicit["path"]
    assert implicit["candidate_fingerprint"] == explicit["candidate_fingerprint"]
    assert implicit["planning_objective"] == explicit["planning_objective"]
    assert implicit["statistics"]["heading_bin_count"] == DEFAULT_HEADING_BIN_COUNT
    assert implicit["statistics"]["theta_min_deg"] == DEFAULT_THETA_MIN_DEG
    assert implicit["statistics"]["search_parameter_provenance"]["parameter_origin"] == (
        SOFTWARE_BASELINE_PARAMETER_ORIGIN
    )
    # The objective weights are untouched by this round.
    objective = implicit["planning_objective"]
    assert (objective["risk_weight"], objective["turn_weight"], objective["distance_weight"]) == (
        0.8, 0.1, 0.1,
    )

    overridden = plan_math({"heading_bin_count": 12})
    assert overridden["status"] == "candidate"
    assert overridden["candidate_fingerprint"] != implicit["candidate_fingerprint"]
    assert overridden["statistics"]["heading_bin_count"] == 12
    assert overridden["search_parameters"]["parameter_origin"] == EXPLICIT_PARAMETER_ORIGIN
    assert overridden["search_parameters"]["engineering_confirmed"] is False


def test_readiness_exposes_the_effective_search_parameters_with_provenance(tmp_path):
    from cns_planner.application.workflow_service import WorkflowService

    service = WorkflowService(tmp_path / "readiness.json", DEFAULTS)
    readiness = service.layered_route_planner_readiness()
    assert readiness["algorithm"]["algorithm_id"] == ALGORITHM_ID
    assert readiness["algorithm"]["uses_theta_star"] is True
    parameters = readiness["theta_star_v2"]["search_parameters"]
    assert parameters["applicable"] is True
    assert parameters["heading_bin_count"] == 8
    assert parameters["theta_min_deg"] == 5.0
    assert parameters["max_expanded_labels"] is None
    assert parameters["parameter_origin"] == SOFTWARE_BASELINE_PARAMETER_ORIGIN
    assert parameters["engineering_confirmed"] is False
    assert parameters["software_algorithm_baseline"] is True
    assert parameters["provenance"]["source"] == SOFTWARE_ALGORITHM_BASELINE_SOURCE
    assert parameters["provenance"]["evidence"] is None
    assert parameters["provenance"]["purpose"] == SEARCH_PARAMETER_PURPOSE
    assert parameters["fingerprint"]
    # An explicitly selected override is reported as an explicit, still-unconfirmed origin.
    service.select_algorithm({
        "algorithm_type": "layered_route_planner", "algorithm_id": ALGORITHM_ID,
        "version": ALGORITHM_VERSION, "parameters": {"heading_bin_count": 12},
    })
    overridden = service.layered_route_planner_readiness()["theta_star_v2"]["search_parameters"]
    assert overridden["heading_bin_count"] == 12
    assert overridden["parameter_origin"] == EXPLICIT_PARAMETER_ORIGIN
    assert overridden["engineering_confirmed"] is False


def test_theta_v2_fingerprints_follow_the_declared_parameters():
    baseline = LayeredRiskAwareThetaStarV2()
    explicit = LayeredRiskAwareThetaStarV2({
        "heading_bin_count": DEFAULT_HEADING_BIN_COUNT,
        "theta_min_deg": DEFAULT_THETA_MIN_DEG,
        "max_expanded_labels": None,
        "search_parameter_provenance": default_theta_v2_search_parameter_provenance(),
    })
    overridden = LayeredRiskAwareThetaStarV2({"heading_bin_count": 12})
    first = baseline.fingerprints(**fingerprint_inputs())
    second = explicit.fingerprints(**fingerprint_inputs())
    third = overridden.fingerprints(**fingerprint_inputs())
    assert first["candidate_fingerprint"] == second["candidate_fingerprint"]
    assert first["policy_fingerprint"] == second["policy_fingerprint"]
    assert third["candidate_fingerprint"] != first["candidate_fingerprint"]
    assert third["policy_fingerprint"] != first["policy_fingerprint"]
    assert first["components"]["theta_parameters"] == second["components"]["theta_parameters"]


def test_registry_never_accepts_unknown_theta_parameters_silently():
    registry = build_default_algorithm_registry({})
    with pytest.raises(ValueError, match="不接受参数"):
        registry.create("layered_route_planner", ALGORITHM_ID, ALGORITHM_VERSION, {
            "heading_bins": 8,
        })
    with pytest.raises(ValueError, match="heading_bin_count"):
        registry.create("layered_route_planner", ALGORITHM_ID, ALGORITHM_VERSION, {
            "heading_bin_count": 7,
        })
    with pytest.raises(ValueError, match="theta_min_deg"):
        registry.create("layered_route_planner", ALGORITHM_ID, ALGORITHM_VERSION, {
            "theta_min_deg": 200.0,
        })
