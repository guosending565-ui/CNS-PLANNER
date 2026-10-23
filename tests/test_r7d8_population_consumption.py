from copy import deepcopy

import pytest

from cns_planner.risk.factors_v2 import FactorExtraction


def cell(count=2):
    return dict(status="missing_data", value_status="passed", quantity_status="passed",
                coverage_status="partial", population_count_people=count,
                population_density_people_km2=count * 100,
                grid_area_m2=10000, density_support_area_m2=10000,
                valid_covered_area_m2=1000, source_coverage_fraction=0.1,
                source_pixel_count=1, density_semantics="whole_cell_mean_density",
                density_semantics_version="1.2")


@pytest.mark.parametrize("count", [2, 0])
def test_resolved_allocation_overrides_missing_centre_statistics(count):
    f = FactorExtraction({}, {"population": {"status": "passed"}})
    assert f._unavailable("population", cell(count)) == (None, None)


@pytest.mark.parametrize("changes", [
    {"value_status": "unknown"}, {"value_status": "missing_data"},
    {"value_status": "stale"}, {"status": "stale"},
    {"population_count_people": None}, {"population_count_people": -1},
    {"population_density_people_km2": 999}, {"source_coverage_fraction": 0},
    {"coverage_status": "nodata_only"}, {"coverage_status": "outside_extent"},
    {"density_support_area_m2": 1000}, {"source_pixel_count": 0},
    {"valid_covered_area_m2": 0}, {"density_semantics_version": "1.1"},
])
def test_incomplete_or_unknown_allocation_stays_unresolved(changes):
    value = deepcopy(cell()); value.update(changes)
    assert FactorExtraction({}, {"population": {"status": "passed"}})._unavailable(
        "population", value)[0] is not None


def test_stale_attribute_wins_over_resolved_allocation():
    assert FactorExtraction({}, {"population": {"status": "stale"}})._unavailable(
        "population", cell())[0] == "stale"


def test_confirmed_policy_zero_remains_distinct_from_real_observation():
    value = dict(status="passed", value_status="passed",
                 coverage_status="nodata_confirmed_zero_population",
                 population_density_people_km2=0, source_coverage_fraction=1,
                 density_semantics="confirmed_zero_population_exposure")
    f = FactorExtraction({}, {"population": {"status": "passed"}})
    assert f._unavailable("population", value) == (None, None)
    assert f._population_coverage_usable(value)


def test_dataset_summary_cannot_resolve_missing_cell():
    f = FactorExtraction({}, {"population": {"status": "passed",
        "population_mapping": {"status": "passed", "covered_cells": 8008}}})
    assert f._unavailable("population", {"status": "missing_data"})[0] == "missing_data"


def test_population_override_does_not_apply_to_other_sources():
    assert FactorExtraction({}, {"terrain": {"status": "passed"}})._unavailable(
        "terrain", cell())[0] == "missing_data"
