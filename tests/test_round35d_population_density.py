"""Round35-D 定向回归：人口密度的**三分量语义**（口径 1.2）与消费端口径守卫。

断言对象（任务书 D2）：

* ``population_count_people``：面积加权后的格内人口数（人）；
* ``population_density_people_km2``：**整格平均**密度，分母**恒为**完整格面积；
* ``population_density_observed_area_people_km2``：有效覆盖区密度，分母是真实有效覆盖面积；
* 低覆盖率**不**丢弃人口数量、**不**把格降级为 missing，只降低"有效覆盖密度"的可信度；
* 风险消费端只接受当前口径；历史（1.1）密度**不得**悄悄参与新风险。
"""

from __future__ import annotations

import pytest

from cns_planner.data.mapping.population import PopulationGridService
from cns_planner.domain.population_density import (
    DENSITY_FIELD, DENSITY_SEMANTICS_CONFIRMED_ZERO, DENSITY_SEMANTICS_OBSERVED_AREA,
    DENSITY_SEMANTICS_VERSION, DENSITY_SEMANTICS_WHOLE_CELL, FIELD_CONTRACTS,
    MIN_USABLE_SOURCE_COVERAGE_FRACTION, OBSERVED_DENSITY_DIAGNOSTIC_FIELD,
    OBSERVED_DENSITY_FIELD, OBSERVED_DENSITY_INSUFFICIENT, OBSERVED_DENSITY_SUFFICIENT,
    density_semantics_disposition, is_current_density_semantics,
    legacy_density_semantics_marker, observed_area_density, observed_density_confidence,
    whole_cell_density,
)
from cns_planner.domain.planning_exposure import POPULATION_DENSITY_SEMANTICS
from cns_planner.domain.population_shelter import (
    empty_population_shelter_attribute, normalize_shelter_coefficient_policy,
    resolve_population_shelter,
)
from cns_planner.domain.risk_v2 import FACTOR_DEFINITIONS
from cns_planner.risk.factors_v2 import POPULATION_FIELD, POPULATION_LEGACY_REASON, FactorExtraction

BBOX = [120.0, 30.0, 120.01, 30.01]


def _allocation(**overrides):
    allocation = {
        "population_count_people": 10.0,
        "target_area_m2": 1_000_000.0,
        "valid_covered_area_m2": 250_000.0,
        "source_coverage_fraction": 0.25,
        "source_pixel_count": 3,
        "value_status": "passed",
        "coverage_status": "partial",
        "quality_flags": ["partial_source_coverage", "not_extrapolated"],
    }
    allocation.update(overrides)
    return allocation


def _quantity_cell(**overrides):
    item = {}
    PopulationGridService._add_quantity_fields(item, BBOX, _allocation(**overrides))
    return item


# ------------------------------------------------------------------ 三个量的定义


def test_whole_cell_density_denominator_is_always_the_full_grid_cell():
    cell = _quantity_cell()
    assert cell["population_count_people"] == pytest.approx(10.0)
    # 整格平均：10 人 / 1 km² = 10 人/km²
    assert cell["population_density_people_km2"] == pytest.approx(10.0)
    assert cell["density_support_area_m2"] == pytest.approx(1_000_000.0)
    assert cell["density_semantics"] == DENSITY_SEMANTICS_WHOLE_CELL
    assert cell["density_semantics_version"] == DENSITY_SEMANTICS_VERSION
    # 有效覆盖密度：10 人 / 0.25 km² = 40 人/km²（独立字段、独立分母）
    assert cell[OBSERVED_DENSITY_FIELD] == pytest.approx(40.0)
    assert cell["observed_density_support_area_m2"] == pytest.approx(250_000.0)
    assert cell["observed_density_semantics"] == DENSITY_SEMANTICS_OBSERVED_AREA
    assert cell["observed_density_confidence"] == OBSERVED_DENSITY_SUFFICIENT
    # 三个量互相自洽：两个密度各自乘自己的分母都等于 count。
    whole = cell[DENSITY_FIELD]
    observed = cell[OBSERVED_DENSITY_FIELD]
    count = cell["population_count_people"]
    assert whole * (cell["density_support_area_m2"] / 1_000_000.0) == pytest.approx(count)
    assert observed * (cell["observed_density_support_area_m2"] / 1_000_000.0) == pytest.approx(count)
    # 声明里写明单位与分母。
    record = cell["density_semantics_record"]
    assert record["whole_cell"]["denominator"] == "full_grid_cell_area"
    assert record["whole_cell"]["unit"] == "person/km2"
    assert record["observed_area"]["denominator"] == "valid_covered_area"
    assert record["risk_consumes"] == DENSITY_FIELD
    assert record["observed_area_is_not_the_whole_cell_density"] is True
    # quantity 契约里两个密度各自声明分母。
    quantities = cell["quantities"]
    assert quantities["population_density"]["conversion"]["denominator"] == "full_grid_cell_area"
    assert quantities["population_density_observed_area"]["conversion"]["denominator"] == (
        "valid_covered_area_km2"
    )
    # 面积加权本身保持不变，且**没有**整像元重复计入。
    assert quantities["population_count"]["conversion"]["whole_pixel_double_counting"] is False
    assert "min(1, overlap_area / pixel_area)" in (
        quantities["population_count"]["conversion"]["area_weighting"]
    )


def test_whole_cell_density_has_no_one_over_coverage_amplification():
    """partial 覆盖下整格平均密度不再被 1/覆盖率放大（旧根因 A 在新口径下不存在）。"""

    for coverage in (0.15, 0.35, 0.6, 0.9):
        cell = _quantity_cell(
            source_coverage_fraction=coverage,
            valid_covered_area_m2=1_000_000.0 * coverage,
        )
        assert cell[DENSITY_FIELD] == pytest.approx(10.0), "整格平均密度与覆盖率无关"
        # 旧的"有效覆盖密度"仍保留在独立字段里（供报告用），但不再冒充整格密度。
        assert cell[OBSERVED_DENSITY_FIELD] == pytest.approx(10.0 / coverage, rel=1e-6)


def test_insufficient_coverage_suppresses_only_the_observed_area_density():
    small = 1_000_000.0 * 0.0005  # 0.05% 覆盖
    cell = _quantity_cell(
        source_coverage_fraction=0.0005, valid_covered_area_m2=small,
    )
    # 人口数量与整格平均密度**保留**（低覆盖率不是丢弃真实面积加权人口的理由）。
    assert cell["population_count_people"] == pytest.approx(10.0)
    assert cell[DENSITY_FIELD] == pytest.approx(10.0)
    assert cell["density_support_area_m2"] == pytest.approx(1_000_000.0)
    # 有效覆盖密度在该覆盖率下没有统计意义：字段为 None，只留诊断值。
    assert cell[OBSERVED_DENSITY_FIELD] is None
    assert cell["observed_density_confidence"] == OBSERVED_DENSITY_INSUFFICIENT
    assert cell[OBSERVED_DENSITY_DIAGNOSTIC_FIELD] == pytest.approx(10.0 / 0.0005)
    # 值状态与覆盖状态都不因此被降级。
    assert cell["value_status"] == "passed"
    assert cell["coverage_status"] == "partial"
    assert MIN_USABLE_SOURCE_COVERAGE_FRACTION == 0.02


def test_density_helpers_are_consistent_and_fail_closed():
    assert whole_cell_density(10.0, 1_000_000.0) == pytest.approx(10.0)
    assert whole_cell_density(None, 1_000_000.0) is None
    assert whole_cell_density(10.0, 0.0) is None
    assert observed_area_density(10.0, 250_000.0, 1_000_000.0) == pytest.approx(40.0)
    assert observed_area_density(10.0, 1_000.0, 1_000_000.0) is None  # 0.1% < 2%
    assert observed_density_confidence(0.01) == OBSERVED_DENSITY_INSUFFICIENT
    assert observed_density_confidence(0.02) == OBSERVED_DENSITY_SUFFICIENT
    assert observed_density_confidence(None, grid_area_m2=1_000_000.0,
                                        covered_area_m2=500_000.0) == OBSERVED_DENSITY_SUFFICIENT
    assert observed_density_confidence(None) is None


# ------------------------------------------------------------------ 语义识别


def test_density_semantics_disposition_separates_current_legacy_and_undeclared():
    assert density_semantics_disposition(
        {"density_semantics": DENSITY_SEMANTICS_WHOLE_CELL,
         "density_semantics_version": DENSITY_SEMANTICS_VERSION}
    ) == "current"
    assert density_semantics_disposition(
        {"density_semantics": DENSITY_SEMANTICS_CONFIRMED_ZERO}
    ) == "confirmed_zero"
    assert density_semantics_disposition(
        {"density_semantics": DENSITY_SEMANTICS_OBSERVED_AREA,
         "density_semantics_version": "1.1"}
    ) == "legacy_not_usable"
    assert density_semantics_disposition({}) == "undeclared"
    # ``density_semantics`` 字段是权威（它声明了分母）：显式声明整格平均密度即当前口径，
    # 版本号只是附加标注；两个函数必须给出完全一致的裁定。
    whole_with_any_version = {
        "density_semantics": DENSITY_SEMANTICS_WHOLE_CELL,
        "density_semantics_version": "1.1",
    }
    assert density_semantics_disposition(whole_with_any_version) == "current"
    assert is_current_density_semantics(whole_with_any_version) is True
    assert is_current_density_semantics(
        {"density_semantics": DENSITY_SEMANTICS_WHOLE_CELL}
    ) is True
    assert is_current_density_semantics(
        {"density_semantics": DENSITY_SEMANTICS_OBSERVED_AREA}
    ) is False
    marker = legacy_density_semantics_marker()
    assert marker["density_semantics_version"] == "1.1"
    assert marker["density_semantics_is_current"] is False


def test_field_contracts_declare_units_and_denominators():
    assert FIELD_CONTRACTS[DENSITY_FIELD]["denominator"] == "full_grid_cell_area"
    assert FIELD_CONTRACTS[OBSERVED_DENSITY_FIELD]["denominator"] == "valid_covered_area"
    assert FIELD_CONTRACTS["population_count_people"]["unit"] == "person"


# ------------------------------------------------------------------ 风险消费端


def _population_attribute(cells):
    return {
        "status": "passed", "value_status": "passed",
        "unit_status": "verified_from_raster_metadata",
        "algorithm_id": "population-grid-raw-statistics",
        "sampling_version": DENSITY_SEMANTICS_VERSION,
        "grid_level": 8, "source": {"path": "population.tif"},
        "cells": cells,
    }


def _population_cell(density, *, semantics=None, version=None, coverage=1.0,
                     status="full"):
    cell = {
        "status": "passed", "value_status": "passed", "coverage_status": status,
        "population_density_people_km2": density,
        "population_count_people": density * 10.0,
        "source_coverage_fraction": coverage,
    }
    if semantics is not None:
        cell["density_semantics"] = semantics
    if version is not None:
        cell["density_semantics_version"] = version
    return cell


def _factors(cells):
    grid = {"status": "passed", "level": 8, "cells": [
        {"grid_id": grid_id, "bbox": [120.0 + index * 0.01, 30.0,
                                      120.01 + index * 0.01, 30.01],
         "center": [120.005 + index * 0.01, 30.005]}
        for index, grid_id in enumerate(sorted(cells))
    ]}
    return FactorExtraction(grid, {"population": _population_attribute(cells)}).extract()


def test_risk_factor_accepts_current_semantics_and_declares_it():
    cells = {
        "G1": _population_cell(20.0, semantics=DENSITY_SEMANTICS_WHOLE_CELL,
                               version=DENSITY_SEMANTICS_VERSION),
        "G2": _population_cell(40.0, semantics=DENSITY_SEMANTICS_WHOLE_CELL,
                               version=DENSITY_SEMANTICS_VERSION),
    }
    result = _factors(cells)
    records = result["factors"]["population_exposure"]
    for grid_id, expected in (("G1", 20.0), ("G2", 40.0)):
        record = records[grid_id]
        assert record["status"] == "passed"
        assert record["raw_value"] == pytest.approx(expected)
        assert record["normalized_index"] is not None
        assert "population_density_semantics_undeclared" not in record["quality_flags"]
    normalization = records["G1"]["normalization"]
    assert normalization["density_semantics"] == DENSITY_SEMANTICS_WHOLE_CELL
    assert normalization["density_semantics_version"] == DENSITY_SEMANTICS_VERSION
    assert normalization["density_denominator"] == "full_grid_cell_area"
    assert normalization["legacy_observed_area_density_is_rejected"] is True


def test_risk_factor_rejects_legacy_observed_area_density():
    """旧口径（1.1）的历史值保持旧语义，**不得**悄悄参与新风险。"""

    legacy_cell = _population_cell(
        40.0, semantics=DENSITY_SEMANTICS_OBSERVED_AREA, version="1.1", coverage=0.25,
        status="partial",
    )
    current_cell = _population_cell(
        10.0, semantics=DENSITY_SEMANTICS_WHOLE_CELL,
        version=DENSITY_SEMANTICS_VERSION, coverage=0.25, status="partial",
    )
    result = _factors({"LEGACY": legacy_cell, "CURRENT": current_cell})
    records = result["factors"]["population_exposure"]
    legacy = records["LEGACY"]
    assert legacy["status"] == "missing_data"
    assert legacy["reason"] == POPULATION_LEGACY_REASON
    assert legacy["normalized_index"] is None
    assert "population_density_semantics_legacy_not_usable" in legacy["quality_flags"]
    assert records["CURRENT"]["status"] == "passed"


def test_risk_factor_flags_undeclared_semantics_without_blocking_legacy_fixtures():
    result = _factors({"G1": _population_cell(30.0)})
    record = result["factors"]["population_exposure"]["G1"]
    assert record["status"] == "passed"
    assert "population_density_semantics_undeclared" in record["quality_flags"]


def test_risk_factor_never_consumes_the_observed_area_density_field():
    assert POPULATION_FIELD == DENSITY_FIELD
    cell = _population_cell(10.0, semantics=DENSITY_SEMANTICS_WHOLE_CELL,
                            version=DENSITY_SEMANTICS_VERSION, coverage=0.25,
                            status="partial")
    cell[OBSERVED_DENSITY_FIELD] = 40.0  # 有效覆盖密度存在，但绝不被消费
    result = _factors({"G1": cell})
    record = result["factors"]["population_exposure"]["G1"]
    assert record["raw_value"] == pytest.approx(10.0)


def test_factor_definition_and_planning_exposure_declare_whole_cell_semantics():
    definition = FACTOR_DEFINITIONS["population_exposure"]
    assert definition["source_field"] == DENSITY_FIELD
    assert definition["density_semantics"] == DENSITY_SEMANTICS_WHOLE_CELL
    assert definition["density_denominator"] == "full_grid_cell_area"
    assert POPULATION_DENSITY_SEMANTICS == DENSITY_SEMANTICS_WHOLE_CELL


def test_population_shelter_carries_semantics_and_count_and_keeps_raw_exposure_definition():
    policy = normalize_shelter_coefficient_policy({
        "status": "confirmed", "default_coefficient": 0.5,
        "source": "round35d-fixture", "confirmed": True,
    })
    grid = {"status": "passed", "level": 8, "cells": [
        {"grid_id": "G1", "bbox": BBOX, "center": [120.005, 30.005]},
    ]}
    population = _population_attribute({
        "G1": _population_cell(20.0, semantics=DENSITY_SEMANTICS_WHOLE_CELL,
                               version=DENSITY_SEMANTICS_VERSION),
    })
    attribute = resolve_population_shelter(
        grid=grid, population_attribute=population,
        normalized_population_factors={"G1": 0.75}, policy=policy,
    )
    cell = attribute["cells"]["G1"]
    assert cell["density_semantics"] == DENSITY_SEMANTICS_WHOLE_CELL
    assert cell["density_semantics_version"] == DENSITY_SEMANTICS_VERSION
    assert cell["population_count_people"] == pytest.approx(200.0)
    assert cell["raw_exposure"] == pytest.approx(20.0 * 0.5)
    assert cell["risk_index"] == pytest.approx(0.75 * 0.5)
    semantics = attribute["semantics"]
    assert semantics["population_density_semantics"] == DENSITY_SEMANTICS_WHOLE_CELL
    assert semantics["population_density_denominator"] == "full_grid_cell_area"
    assert semantics["population_density_is_not_the_observed_covered_area_density"] is True
    assert empty_population_shelter_attribute()["semantics"][
        "population_density_semantics"
    ] == DENSITY_SEMANTICS_WHOLE_CELL
