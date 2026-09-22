"""BUG-ROUTE-005（收口）：规划用暴露度层 = 陆地统一相对风险基线 专项测试。

正式规划语义（本轮替换掉旧的 ``land_population_floor`` / ``max(population_density, floor)``）：

    planning_population_factor = (1 - b) * p + b * L
    p = canonical Risk Framework V2 归一化人口因子 [0,1]
    L = 1 if land else 0 if water
    b = land_relative_risk_baseline（0 ≤ b < 1，显式 confirmed 才生效）

锁定的性质：

1. 同一 ``p`` 下 land − water 差值恒等于 ``b``；
2. 所有人口相对差异统一保留 ``(1-b)``，不只抬低人口区域；
3. 输出天然落在 ``[0,1]``，且**不做 clip**（高风险陆地不饱和）；
4. land 内部排序、water 内部排序保持；
5. 不修改真实人口密度、人口报告、Population NoData、Risk V2 canonical factor。

另外锁定：``b = 0`` 完全恢复 canonical 因子；terrain 缺失保持 unresolved 且 fail-closed；
旧 ``land_population_floor`` 绝不被静默换算/迁移；Theta* 仍使用 0.8/0.1/0.1。
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_layered_theta_star_v2 import (  # noqa: E402
    DEFAULTS, LEVEL, grid_cells, plan_v2, user_defined_baseline_policy,
)
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.planning_exposure import (  # noqa: E402
    ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE, PLANNING_FACTOR_FORMULA,
    PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE, USED_POPULATION_NODATA_AS_SEA_PROXY,
    normalize_planning_exposure_policy, planning_exposure_factors,
    planning_exposure_is_active, planning_exposure_policy_fingerprint,
    resolve_planning_exposure,
)

REFERENCE = 100.0          # Risk Framework V2 population 归一化参考值（人/km²）
BASELINE = 0.08            # 首轮工程基线
LAND_THRESHOLD = 0.0       # 高程 > 0 m ⇒ 陆地；≤ 0 m ⇒ 海面（显式工程阈值）

LAND_FACTOR = 0.01         # 陆地真实人口很稀疏
SEA_FACTOR = 0.0           # 海面真实人口为 0（已由项目确认的 NoData 语义给出）


# ============================================================================
# fixtures / helpers
# ============================================================================


def grid(columns=3, rows=3):
    return grid_cells(columns=columns, rows=rows)


def two_cells():
    """一格海面（RP0）+ 一格陆地（RP1），用于精确数值断言。"""

    return grid_cells(columns=1, rows=2)


def factor_map(cells, *, land_rows):
    return {
        cell["grid_id"]: (LAND_FACTOR if cell["row"] in land_rows else SEA_FACTOR)
        for cell in cells
    }


def uniform_factors(cells, *, land_rows, land_factor, water_factor):
    return {
        cell["grid_id"]: (land_factor if cell["row"] in land_rows else water_factor)
        for cell in cells
    }


def population_attribute(cells, *, land_rows, land_density=1.0):
    return {
        "status": "passed", "algorithm_id": "population-grid-raw-statistics",
        "cells": {
            cell["grid_id"]: {
                "status": "passed",
                "population_density_people_km2": (
                    land_density if cell["row"] in land_rows else 0.0
                ),
                "source_coverage_fraction": 1.0, "coverage_status": "full",
            }
            for cell in cells
        },
    }


def terrain_attribute(cells, *, land_rows, land_elevation=10.0):
    return {
        "status": "passed", "algorithm_id": "terrain-grid-elevation-statistics",
        "cells": {
            cell["grid_id"]: {
                "status": "passed",
                "surface_elevation_max_m": (
                    land_elevation if cell["row"] in land_rows else -2.0
                ),
            }
            for cell in cells
        },
    }


def grid_risk_v2(cells, factors):
    return {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains",
        "algorithm_version": "2.0", "input_fingerprint": f"riskv2-in-{REFERENCE}",
        "policy_fingerprint": "riskv2-pol",
        "references": {"population_exposure": {"mode": "dataset_quantile", "value": REFERENCE}},
        "factor_status": {"population_exposure": {
            "status": "passed", "source_id": "worldpop-r2025a-population-count",
            "source_fingerprint": "popsrc",
            "normalization": {
                "method": "log1p_ratio_to_dataset_quantile",
                "reference": {"value": REFERENCE},
            },
        }},
        "cells": {
            cell["grid_id"]: {
                "grid_id": cell["grid_id"], "status": "passed",
                "factors": {"population_exposure": {
                    "factor_id": "population_exposure", "status": "passed",
                    "normalized_index": factors[cell["grid_id"]],
                }},
                "domains": {},
            }
            for cell in cells
        },
    }


def active_policy(**overrides):
    payload = {
        "enabled": True, "land_relative_risk_baseline": BASELINE,
        "land_min_surface_elevation_m": LAND_THRESHOLD,
        "source": "工程确认-舟山海陆相对风险基线",
        "evidence": {"reference": "ENG-ZS-2026-014"},
        "provenance": "explicit_override", "confirmed": True,
    }
    payload.update(overrides)
    return normalize_planning_exposure_policy(payload)


def resolve(cells, *, land_rows, factors=None, policy=None, terrain=None,
            population=None, risk_v2=None):
    factors = factors if factors is not None else factor_map(cells, land_rows=land_rows)
    return resolve_planning_exposure(
        grid={"level": LEVEL, "cells": cells},
        population_attribute=(
            population if population is not None
            else population_attribute(cells, land_rows=land_rows)
        ),
        terrain_attribute=(
            terrain if terrain is not None
            else terrain_attribute(cells, land_rows=land_rows)
        ),
        normalized_population_factors=factors,
        policy=policy if policy is not None else active_policy(),
        grid_risk_v2=risk_v2 if risk_v2 is not None else grid_risk_v2(cells, factors),
    )


def planning_of(attribute, grid_id):
    return attribute["cells"][grid_id]["planning_population_factor"]


# ============================================================================
# 1. b = 0.08 的精确数值
# ============================================================================


@pytest.mark.parametrize("p,expected_land,expected_water", [
    (0.0, 0.08, 0.0),
    (0.1, 0.172, 0.092),
    (0.5, 0.54, 0.46),
    (1.0, 1.0, 0.92),
])
def test_the_engineering_baseline_values_are_exact(p, expected_land, expected_water):
    cells = two_cells()
    factors = uniform_factors(cells, land_rows={1}, land_factor=p, water_factor=p)
    attribute = resolve(cells, land_rows={1}, factors=factors, risk_v2=grid_risk_v2(cells, factors))

    assert attribute["applied"] is True
    assert attribute["status"] == "passed"
    assert attribute["land_relative_risk_baseline"] == pytest.approx(BASELINE, abs=1e-12)
    assert attribute["land_water_gap"] == pytest.approx(BASELINE, abs=1e-12)
    assert attribute["population_difference_retention"] == pytest.approx(0.92, abs=1e-12)
    assert attribute["formula"] == PLANNING_FACTOR_FORMULA
    assert attribute["land_count"] == 1
    assert attribute["water_count"] == 1
    assert attribute["applied_count"] == 2

    land_cell = attribute["cells"][cells[1]["grid_id"]]
    water_cell = attribute["cells"][cells[0]["grid_id"]]
    assert land_cell["land_status"] == "land"
    assert water_cell["land_status"] == "water"
    assert land_cell["planning_population_factor"] == pytest.approx(expected_land, abs=1e-9)
    assert water_cell["planning_population_factor"] == pytest.approx(expected_water, abs=1e-9)
    # canonical factor 与真实人口密度都原样保留（只读转印）。
    assert land_cell["population_factor"] == pytest.approx(p, abs=1e-12)
    assert water_cell["population_factor"] == pytest.approx(p, abs=1e-12)
    assert land_cell["real_population_density_people_km2"] == pytest.approx(1.0)
    assert water_cell["real_population_density_people_km2"] == pytest.approx(0.0)


def test_the_suggested_engineering_value_matches_the_documented_first_round_value():
    assert ENGINEERING_SUGGESTED_LAND_RELATIVE_RISK_BASELINE == pytest.approx(0.08)
    # 建议值本身绝不是默认 policy：未显式配置时依然不生效。
    from cns_planner.domain.planning_exposure import default_planning_exposure_policy

    default = default_planning_exposure_policy()
    assert default["land_relative_risk_baseline"] is None
    assert default["confirmed"] is False
    assert default["enabled"] is False
    assert planning_exposure_is_active(default) is False


# ============================================================================
# 2. land − water gap 恒等于 b；相对差异统一缩放 (1-b)
# ============================================================================


@pytest.mark.parametrize("p", [0.0, 0.03, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
def test_the_land_water_gap_equals_b_for_every_population_factor(p):
    cells = two_cells()
    factors = uniform_factors(cells, land_rows={1}, land_factor=p, water_factor=p)
    attribute = resolve(cells, land_rows={1}, factors=factors, risk_v2=grid_risk_v2(cells, factors))
    land = attribute["cells"][cells[1]["grid_id"]]["planning_population_factor"]
    water = attribute["cells"][cells[0]["grid_id"]]["planning_population_factor"]
    assert land - water == pytest.approx(BASELINE, abs=1e-9), p
    # 逐格也记录了同一个 gap。
    assert attribute["cells"][cells[1]["grid_id"]]["land_water_gap"] == pytest.approx(BASELINE)
    assert attribute["cells"][cells[0]["grid_id"]]["land_water_gap"] == pytest.approx(BASELINE)


@pytest.mark.parametrize("p1,p2", [(0.2, 0.8), (0.0, 1.0), (0.35, 0.4)])
def test_every_population_difference_is_scaled_by_one_minus_b(p1, p2):
    cells = two_cells()
    land_rows = {1}
    first = resolve(
        cells, land_rows=land_rows,
        factors=uniform_factors(cells, land_rows=land_rows, land_factor=p1, water_factor=p1),
    )
    second = resolve(
        cells, land_rows=land_rows,
        factors=uniform_factors(cells, land_rows=land_rows, land_factor=p2, water_factor=p2),
    )
    # land 内部与 water 内部的差值都 × 0.92（统一保留相对差异，不只抬低人口区域）。
    for cell in cells:
        grid_id = cell["grid_id"]
        assert (
            planning_of(second, grid_id) - planning_of(first, grid_id)
        ) == pytest.approx((p2 - p1) * 0.92, abs=1e-9), (p1, p2, grid_id)


def test_land_and_water_internal_orderings_are_preserved():
    cells = grid_cells(columns=1, rows=3)  # RP0 = 海面, RP1/RP2 = 陆地
    land_rows = {1, 2}
    factors = {
        cells[0]["grid_id"]: 0.30,   # water low
        cells[1]["grid_id"]: 0.20,   # land lower
        cells[2]["grid_id"]: 0.70,   # land higher
    }
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors,
        risk_v2=grid_risk_v2(cells, factors),
    )
    water = attribute["cells"][cells[0]["grid_id"]]["planning_population_factor"]
    land_low = attribute["cells"][cells[1]["grid_id"]]["planning_population_factor"]
    land_high = attribute["cells"][cells[2]["grid_id"]]["planning_population_factor"]
    assert water == pytest.approx(0.30 * 0.92, abs=1e-9)
    assert land_low == pytest.approx(0.20 * 0.92 + 0.08, abs=1e-9)
    assert land_high == pytest.approx(0.70 * 0.92 + 0.08, abs=1e-9)
    # 同一 land 集合内排序保持（canonical p 单调 ⇒ planning 单调）。
    assert land_low < land_high


# ============================================================================
# 3. 值域：天然 [0,1]，不做 clip，也不饱和
# ============================================================================


def test_the_output_never_leaves_the_unit_interval_without_clipping():
    cells = two_cells()
    land_rows = {1}
    for step in range(0, 101):
        p = step / 100.0
        factors = uniform_factors(cells, land_rows=land_rows, land_factor=p, water_factor=p)
        attribute = resolve(
            cells, land_rows=land_rows, factors=factors,
            risk_v2=grid_risk_v2(cells, factors),
        )
        for cell in attribute["cells"].values():
            value = cell["planning_population_factor"]
            assert 0.0 <= value <= 1.0, (p, value)


def test_high_population_land_is_not_saturated_by_a_clip():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.99, water_factor=0.99)
    attribute = resolve(cells, land_rows=land_rows, factors=factors)
    land = attribute["cells"][cells[1]["grid_id"]]["planning_population_factor"]
    # 0.99*0.92 + 0.08 = 0.9908 < 1：没有被 clip 抬到与 p=1 相同的饱和值。
    assert land == pytest.approx(0.9908, abs=1e-9)
    assert land < 1.0
    assert attribute["provenance"]["no_clip_used"] is True


def test_a_baseline_close_to_one_stays_exact_and_inside_the_range():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=1.0, water_factor=1.0)
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors,
        policy=active_policy(land_relative_risk_baseline=0.999),
    )
    land = attribute["cells"][cells[1]["grid_id"]]["planning_population_factor"]
    water = attribute["cells"][cells[0]["grid_id"]]["planning_population_factor"]
    assert land == pytest.approx(1.0, abs=1e-9)
    assert water == pytest.approx(0.001, abs=1e-9)
    assert land - water == pytest.approx(0.999, abs=1e-9)


def test_land_with_the_full_population_factor_is_not_counted_as_raised():
    """p = 1 的陆地正好持平（0.92*1 + 0.08 = 1），不是被抬高；raised 只统计真正抬高的格。"""

    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=1.0, water_factor=0.0)
    attribute = resolve(cells, land_rows=land_rows, factors=factors)
    land = attribute["cells"][cells[1]["grid_id"]]
    assert land["planning_population_factor"] == pytest.approx(1.0, abs=1e-9)
    assert land["land_baseline_raised"] is False
    assert attribute["cells"][cells[0]["grid_id"]]["land_baseline_raised"] is False
    assert attribute["land_baseline_raised_count"] == 0
    assert attribute["applied_count"] == 2


# ============================================================================
# 4. b = 0 完全恢复 canonical population factor
# ============================================================================


def test_a_zero_baseline_restores_the_canonical_population_factor_exactly():
    cells = grid_cells(columns=1, rows=3)
    land_rows = {1, 2}
    factors = {
        cells[0]["grid_id"]: 0.37,
        cells[1]["grid_id"]: 0.05,
        cells[2]["grid_id"]: 0.91,
    }
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors,
        policy=active_policy(land_relative_risk_baseline=0.0),
    )
    assert attribute["applied"] is True
    assert attribute["land_relative_risk_baseline"] == pytest.approx(0.0)
    assert attribute["population_difference_retention"] == pytest.approx(1.0)
    assert attribute["land_baseline_raised_count"] == 0
    for grid_id, expected in factors.items():
        cell = attribute["cells"][grid_id]
        assert cell["planning_population_factor"] == pytest.approx(expected, abs=1e-12)
        assert planning_exposure_factors(attribute)[grid_id] == pytest.approx(expected, abs=1e-12)


def test_zero_is_a_legal_explicit_baseline_and_null_is_not_zero():
    zero = active_policy(land_relative_risk_baseline=0.0)
    assert planning_exposure_is_active(zero) is True
    assert zero["land_relative_risk_baseline"] == 0.0

    unset = active_policy(land_relative_risk_baseline=None)
    assert unset["land_relative_risk_baseline"] is None
    assert unset["status"] == "not_configured"
    assert unset["status_reason"] == "land_relative_risk_baseline_not_configured"
    assert planning_exposure_is_active(unset) is False


@pytest.mark.parametrize("value", [-0.01, 1.0, 1.5, float("nan"), float("inf")])
def test_a_baseline_outside_zero_to_one_is_rejected(value):
    with pytest.raises(ValueError):
        active_policy(land_relative_risk_baseline=value)


# ============================================================================
# 5. water 不获得 land baseline
# ============================================================================


def test_water_cells_never_receive_the_land_baseline():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.0, water_factor=0.0)
    attribute = resolve(cells, land_rows=land_rows, factors=factors)
    water = attribute["cells"][cells[0]["grid_id"]]
    assert water["land_status"] == "water"
    assert water["land_indicator"] == pytest.approx(0.0)
    assert water["planning_population_factor"] == pytest.approx(0.0)
    assert water["land_baseline_raised"] is False
    # 海面 p=0: 仍然是最便宜的 0，而不是 0.08。
    assert water["planning_population_factor"] != pytest.approx(BASELINE)


def test_a_water_cell_keeps_its_relative_position_but_is_scaled_by_one_minus_b():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.5, water_factor=0.5)
    attribute = resolve(cells, land_rows=land_rows, factors=factors)
    water = attribute["cells"][cells[0]["grid_id"]]["planning_population_factor"]
    assert water == pytest.approx(0.46, abs=1e-9)


# ============================================================================
# 6. terrain 缺失：unresolved + fail-closed，绝不猜海面
# ============================================================================


def test_missing_terrain_evidence_stays_unresolved_and_never_guesses_water():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.2, water_factor=0.2)
    terrain = terrain_attribute(cells, land_rows=land_rows)
    terrain["cells"][cells[1]["grid_id"]] = {"status": "missing_data"}

    attribute = resolve(
        cells, land_rows=land_rows, factors=factors, terrain=terrain,
        risk_v2=grid_risk_v2(cells, factors),
    )
    missing = attribute["cells"][cells[1]["grid_id"]]
    assert missing["land_status"] == "unresolved"
    assert missing["reason"] == "terrain_elevation_evidence_missing"
    assert missing["terrain_reason"] == "terrain_elevation_evidence_missing"
    assert missing["land_indicator"] is None
    assert missing["baseline_applied"] is False
    # fail-closed：不给伪 0（=猜海面），也不回落成不带基线的 canonical 因子。
    assert missing["planning_population_factor"] is None
    assert missing["population_factor"] == pytest.approx(0.2)
    assert attribute["unresolved_land_status_count"] == 1
    assert attribute["unresolved_cell_count"] == 1
    assert attribute["applied_count"] == 1
    assert cells[1]["grid_id"] not in planning_exposure_factors(attribute)


def test_missing_terrain_evidence_never_becomes_sea_even_when_population_is_zero():
    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.0, water_factor=0.0)
    terrain = terrain_attribute(cells, land_rows=land_rows)
    terrain["cells"][cells[1]["grid_id"]] = {"status": "missing_data"}
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors, terrain=terrain,
        risk_v2=grid_risk_v2(cells, factors),
    )
    missing = attribute["cells"][cells[1]["grid_id"]]
    assert missing["planning_population_factor"] is None
    assert missing["planning_population_factor"] != pytest.approx(0.0)
    assert attribute["used_population_nodata_as_sea_proxy"] is False
    assert USED_POPULATION_NODATA_AS_SEA_PROXY is False


def test_a_cell_without_a_canonical_population_factor_is_unresolved_not_zero():
    cells = two_cells()
    land_rows = {1}
    factors = {cells[0]["grid_id"]: 0.4}  # 陆地格缺 canonical 因子
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors,
        risk_v2=grid_risk_v2(cells, {cells[0]["grid_id"]: 0.4, cells[1]["grid_id"]: 0}),
    )
    missing = attribute["cells"][cells[1]["grid_id"]]
    assert missing["status"] == "unresolved"
    assert missing["reason"] == "population_factor_unresolved"
    assert missing["planning_population_factor"] is None
    assert missing["baseline_applied"] is False
    assert missing["land_status"] == "land"
    assert attribute["unresolved_population_factor_count"] == 1
    assert attribute["status"] == "partial"


def test_an_out_of_range_canonical_factor_is_failed_closed_instead_of_clipped():
    cells = two_cells()
    land_rows = {1}
    factors = {cells[0]["grid_id"]: 0.4, cells[1]["grid_id"]: 1.4}
    attribute = resolve(cells, land_rows=land_rows, factors=factors)
    bad = attribute["cells"][cells[1]["grid_id"]]
    assert bad["status"] == "unresolved"
    assert bad["reason"] == "population_factor_out_of_range"
    assert bad["planning_population_factor"] is None


# ============================================================================
# 7. 未激活策略：逐格零影响
# ============================================================================


@pytest.mark.parametrize("overrides,expected_reason", [
    ({"enabled": False}, "planning_exposure_disabled"),
    ({"confirmed": False}, "planning_exposure_policy_not_confirmed"),
    ({"land_relative_risk_baseline": None}, "land_relative_risk_baseline_not_configured"),
    ({"land_min_surface_elevation_m": None}, "land_detection_threshold_not_configured"),
])
def test_an_inactive_policy_never_changes_a_single_factor(overrides, expected_reason):
    cells = two_cells()
    land_rows = {1}
    factors = factor_map(cells, land_rows=land_rows)
    policy = active_policy(**overrides)
    assert planning_exposure_is_active(policy) is False
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors, policy=policy,
        risk_v2=grid_risk_v2(cells, factors),
    )
    assert attribute["applied"] is False
    assert attribute["status"] == "not_configured"
    assert attribute["reason"] == expected_reason
    assert attribute["cells"] == {}
    assert planning_exposure_factors(attribute) is None


def test_an_unresolvable_population_reference_no_longer_blocks_the_baseline():
    """新公式只需要 canonical 因子本身：参考值只作信息性记录，不再是硬前置条件。"""

    cells = two_cells()
    land_rows = {1}
    factors = uniform_factors(cells, land_rows=land_rows, land_factor=0.2, water_factor=0.2)
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors,
        risk_v2={"status": "passed", "cells": {}},
    )
    assert attribute["applied"] is True
    assert attribute["status"] == "passed"
    assert attribute["population_reference"] is None
    land = attribute["cells"][cells[1]["grid_id"]]["planning_population_factor"]
    assert land == pytest.approx(0.2 * 0.92 + 0.08, abs=1e-9)


# ============================================================================
# 8. 废弃字段 land_population_floor：读取兼容 + 绝不静默迁移
# ============================================================================


def test_a_legacy_land_population_floor_is_never_silently_migrated_to_a_baseline():
    legacy = {
        "enabled": True, "confirmed": True,
        "land_population_floor": 5.0,
        "land_min_surface_elevation_m": LAND_THRESHOLD,
        "source": "旧项目：舟山陆地人口下限", "provenance": "explicit_override",
    }
    policy = normalize_planning_exposure_policy(legacy)

    # 旧项目**不会**自动生效：缺少显式确认的新基线 ⇒ 依然 not_configured。
    assert policy["status"] == "not_configured"
    assert policy["status_reason"] == "land_relative_risk_baseline_not_configured"
    assert policy["land_relative_risk_baseline"] is None
    assert planning_exposure_is_active(policy) is False

    # 旧字段被原样记录并显式标记 deprecated，绝不换算成 0.08 或任何 b。
    assert policy["land_population_floor"] == pytest.approx(5.0)
    assert policy["land_population_floor_deprecated"] is True
    assert policy["land_population_floor_used_for_planning"] is False
    assert policy["land_relative_risk_baseline"] != pytest.approx(BASELINE)

    cells = two_cells()
    land_rows = {1}
    factors = factor_map(cells, land_rows=land_rows)
    attribute = resolve(
        cells, land_rows=land_rows, factors=factors, policy=policy,
        risk_v2=grid_risk_v2(cells, factors),
    )
    assert attribute["applied"] is False
    assert attribute["land_relative_risk_baseline"] is None
    assert attribute["land_population_floor"] == pytest.approx(5.0)
    assert attribute["land_population_floor_used_for_planning"] is False
    assert planning_exposure_factors(attribute) is None


def test_the_deprecated_floor_does_not_participate_in_the_policy_fingerprint():
    with_floor = active_policy(land_population_floor=5.0)
    without_floor = active_policy()
    assert planning_exposure_policy_fingerprint(with_floor) == planning_exposure_policy_fingerprint(
        without_floor
    )
    # 但新的 baseline 一定进指纹。
    baseline_changed = active_policy(land_relative_risk_baseline=0.05)
    assert planning_exposure_policy_fingerprint(baseline_changed) != (
        planning_exposure_policy_fingerprint(without_floor)
    )


def test_a_non_finite_or_negative_legacy_floor_is_still_rejected():
    with pytest.raises(ValueError):
        active_policy(land_population_floor=-1.0)
    with pytest.raises(ValueError):
        active_policy(land_population_floor=float("nan"))


# ============================================================================
# 9. Workflow 集成：真实人口 / NoData / Risk V2 零改动
# ============================================================================


def service_with(tmp_path, *, land_rows=(1, 2), name="planning-exposure-baseline.json"):
    cells = grid()
    service = WorkflowService(tmp_path / name, DEFAULTS)
    factors = factor_map(cells, land_rows=set(land_rows))
    service.state["grid"] = {
        "status": "passed", "level": LEVEL, "count": len(cells), "cells": cells,
    }
    service.state["grid_attributes"]["population"] = population_attribute(
        cells, land_rows=set(land_rows)
    )
    service.state["grid_attributes"]["terrain"] = terrain_attribute(
        cells, land_rows=set(land_rows)
    )
    service.state["grid_risk_v2"] = grid_risk_v2(cells, factors)
    service.state["shelter_coefficient_policy"] = user_defined_baseline_policy()
    # 触发 ensure_state：之后的 before/after 比较才是"同一起点"的比较。
    service.layered_route_planner_service.ensure_state()
    return service, cells


def risk_index_map(attribute):
    return {grid_id: cell["risk_index"] for grid_id, cell in attribute["cells"].items()}


def test_the_default_policy_is_absent_and_keeps_the_canonical_population_objective(tmp_path):
    service, cells = service_with(tmp_path)
    population_before = deepcopy(service.state["grid_attributes"]["population"])
    terrain_before = deepcopy(service.state["grid_attributes"]["terrain"])
    risk_before = deepcopy(service.state["grid_risk_v2"])
    nodata_before = deepcopy(service.state.get("population_nodata_policy"))

    attribute = service.population_shelter()
    assert attribute["population_factor_source"] == "canonical_risk_v2_population_factor"
    assert attribute["planning_exposure"]["applied"] is False
    exposure = service.planning_exposure()
    assert exposure["applied"] is False
    assert exposure["status"] == "not_configured"

    assert service.state["grid_attributes"]["population"] == population_before
    assert service.state["grid_attributes"]["terrain"] == terrain_before
    assert service.state["grid_risk_v2"] == risk_before
    assert service.state.get("population_nodata_policy") == nodata_before

    baseline = risk_index_map(attribute)
    assert risk_index_map(service.population_shelter()) == baseline


def test_the_confirmed_baseline_changes_only_the_planning_factor(tmp_path):
    service, cells = service_with(tmp_path)
    population_before = deepcopy(service.state["grid_attributes"]["population"])
    terrain_before = deepcopy(service.state["grid_attributes"]["terrain"])
    risk_before = deepcopy(service.state["grid_risk_v2"])
    nodata_before = deepcopy(service.state.get("population_nodata_policy"))

    baseline = service.population_shelter()
    baseline_risk = risk_index_map(baseline)
    baseline_objective = plan_v2(cells, shelter=baseline)["planning_objective"]

    service.set_planning_exposure_policy(active_policy())
    exposure = service.planning_exposure()
    assert exposure["applied"] is True
    assert exposure["status"] == "passed"
    assert exposure["land_relative_risk_baseline"] == pytest.approx(BASELINE)
    assert exposure["land_water_gap"] == pytest.approx(BASELINE)
    assert exposure["land_count"] == 6
    assert exposure["water_count"] == 3
    assert exposure["applied_count"] == 9
    assert exposure["population_factor_source"] == PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE
    assert exposure["field_fingerprint"]

    raised = service.population_shelter()
    assert raised["population_factor_source"] == PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE
    raised_risk = risk_index_map(raised)

    land_ids = [cell["grid_id"] for cell in cells if cell["row"] in (1, 2)]
    water_ids = [cell["grid_id"] for cell in cells if cell["row"] == 0]
    for grid_id in land_ids:
        # 0.92*0.01 + 0.08 = 0.0892 > 0.01：陆地风险严格上升。
        assert raised_risk[grid_id] > baseline_risk[grid_id], grid_id
        assert raised["cells"][grid_id]["normalized_population_factor"] == pytest.approx(
            0.0892, abs=1e-9
        ), grid_id
    for grid_id in water_ids:
        # 海面 p = 0 且 L = 0 ⇒ 0.92*0 = 0：仍然是最便宜的 0。
        assert raised_risk[grid_id] == pytest.approx(baseline_risk[grid_id]), grid_id
        assert raised_risk[grid_id] < min(raised_risk[grid_id_] for grid_id_ in land_ids)

    raised_objective = plan_v2(cells, shelter=raised)["planning_objective"]
    assert raised_objective["risk_exposure_index_m"] > baseline_objective["risk_exposure_index_m"]
    # Theta* 的 0.8/0.1/0.1 权重语义完全不变。
    assert raised_objective["risk_weight"] == pytest.approx(0.8)
    assert raised_objective["turn_weight"] == pytest.approx(0.1)
    assert raised_objective["distance_weight"] == pytest.approx(0.1)

    # 真实 population / terrain / NoData 语义与 canonical population factor 零改动。
    assert service.state["grid_attributes"]["population"] == population_before
    assert service.state["grid_attributes"]["terrain"] == terrain_before
    assert service.state.get("population_nodata_policy") == nodata_before
    for grid_id, before_cell in risk_before["cells"].items():
        assert (
            service.state["grid_risk_v2"]["cells"][grid_id]["factors"]
            == before_cell["factors"]
        ), grid_id
    assert exposure["used_population_nodata_as_sea_proxy"] is False


def test_turning_the_baseline_back_off_restores_the_canonical_objective_exactly(tmp_path):
    service, _ = service_with(tmp_path)
    baseline_risk = risk_index_map(service.population_shelter())

    service.set_planning_exposure_policy(active_policy())
    assert risk_index_map(service.population_shelter()) != baseline_risk

    service.set_planning_exposure_policy({"enabled": False})
    restored = service.population_shelter()
    assert risk_index_map(restored) == baseline_risk
    assert restored["population_factor_source"] == "canonical_risk_v2_population_factor"


def test_an_all_fail_closed_exposure_does_not_fall_back_to_canonical_factors(tmp_path):
    """已生效但没有任何格算出因子时，绝不能用 ``or`` 回落成 canonical 因子。"""

    service, cells = service_with(tmp_path)
    # 抹掉全部地形证据：所有格都 fail-closed。
    service.state["grid_attributes"]["terrain"] = {
        "status": "missing_data", "algorithm_id": "terrain-grid-elevation-statistics",
        "cells": {cell["grid_id"]: {"status": "missing_data"} for cell in cells},
    }
    service.set_planning_exposure_policy(active_policy())
    exposure = service.planning_exposure()
    assert exposure["applied"] is True
    assert exposure["status"] == "missing_data"
    assert exposure["applied_count"] == 0

    shelter = service.population_shelter()
    assert shelter["population_factor_source"] == PLANNING_EXPOSURE_POPULATION_FACTOR_SOURCE
    for grid_id, cell in shelter["cells"].items():
        assert cell["status"] == "unresolved", grid_id
        assert cell["risk_index"] is None, grid_id


def test_the_policy_change_goes_through_the_existing_invalidation_chain(tmp_path):
    service, _ = service_with(tmp_path)
    reasons = []
    service.layered_route_planner_service.invalidation = SimpleNamespace(
        layered_route=lambda reason: reasons.append(reason)
    )
    service.set_planning_exposure_policy(active_policy())
    assert reasons == ["planning_exposure_policy_changed"]
    # 派生缓存按新 policy 重建（旧基线的缓存绝不残留）。
    cached = service.state.get("_planning_exposure_cache")
    assert isinstance(cached, dict)
    assert cached["land_relative_risk_baseline"] == pytest.approx(BASELINE)
    assert cached["applied"] is True

    # 相同 policy 再保存一次：没有任何变化 ⇒ 不再失效、也不再写盘。
    service.set_planning_exposure_policy(active_policy())
    assert reasons == ["planning_exposure_policy_changed"]


# ============================================================================
# 10. 快照 / 持久化 / provenance
# ============================================================================


def test_the_policy_round_trips_through_the_project_file(tmp_path):
    path = tmp_path / "planning-exposure-baseline-roundtrip.json"
    service, _ = service_with(tmp_path, name=path.name)
    service.set_planning_exposure_policy(active_policy())

    reloaded = WorkflowService(path, DEFAULTS)
    policy = reloaded.planning_exposure_policy()
    assert policy["status"] == "confirmed"
    assert policy["enabled"] is True
    assert policy["land_relative_risk_baseline"] == pytest.approx(BASELINE)
    assert policy["land_min_surface_elevation_m"] == pytest.approx(LAND_THRESHOLD)
    assert policy["confirmed"] is True
    assert policy["formula"] == PLANNING_FACTOR_FORMULA
    assert policy["land_population_floor_deprecated"] is True
    # 派生缓存不随项目持久化（可由现有输入重建）：要么不存在，要么是空占位。
    cached = reloaded.state.get("_planning_exposure_cache")
    assert not (isinstance(cached, dict) and cached.get("cells")), cached
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "_planning_exposure_cache" not in saved


def test_the_snapshot_records_the_formula_baseline_counts_and_fingerprint(tmp_path):
    service, _ = service_with(tmp_path)
    snapshot = service.snapshot()
    assert snapshot["planning_exposure_policy"]["status"] == "not_configured"

    service.set_planning_exposure_policy(active_policy())
    snapshot = service.snapshot()
    projection = snapshot["planning_exposure"]
    assert "cells" not in projection, "快照只带只读摘要，逐 cell 明细由 /api/planning-exposure 提供"
    assert projection["cells_detail"] == "GET /api/planning-exposure"
    assert projection["applied"] is True
    assert projection["formula"] == PLANNING_FACTOR_FORMULA
    assert projection["land_relative_risk_baseline"] == pytest.approx(BASELINE)
    assert projection["land_water_gap"] == pytest.approx(BASELINE)
    assert projection["population_difference_retention"] == pytest.approx(0.92)
    assert projection["land_count"] == 6
    assert projection["water_count"] == 3
    assert projection["applied_count"] == 9
    assert projection["field_fingerprint"]
    assert projection["policy_fingerprint"]
    provenance = projection["provenance"]
    assert provenance["formula"] == PLANNING_FACTOR_FORMULA
    assert provenance["counts"]["land_count"] == 6
    assert provenance["counts"]["water_count"] == 3
    assert provenance["counts"]["applied_count"] == 9
    assert provenance["land_population_floor"] == "deprecated_not_used_for_planning"
    assert provenance["no_clip_used"] is True
    assert provenance["risk_index_definition_unchanged"] == (
        "normalized_population_factor × shelter_coefficient"
    )


def test_the_shelter_projection_reports_the_planning_exposure_summary(tmp_path):
    service, _ = service_with(tmp_path)
    service.set_planning_exposure_policy(active_policy())
    shelter = service.population_shelter()
    summary = shelter["planning_exposure"]
    assert summary["applied"] is True
    assert summary["formula"] == PLANNING_FACTOR_FORMULA
    assert summary["land_relative_risk_baseline"] == pytest.approx(BASELINE)
    assert summary["land_count"] == 6
    assert summary["water_count"] == 3
    assert summary["applied_count"] == 9
    assert summary["land_population_floor_used_for_planning"] is False
    assert summary["used_population_nodata_as_sea_proxy"] is False
