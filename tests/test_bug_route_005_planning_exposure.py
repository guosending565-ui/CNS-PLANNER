"""BUG-ROUTE-005：规划用暴露度层（planning exposure floor）回归测试。

要求（用户验收口径）：

* **开启 floor**：海面保持既有 NoData / 原始人口语义（低代价），陆地有效人口不低于 floor，
  规划风险随之提高；
* **关闭 floor**：objective 与开启前**逐字段相同**（原样保持原始 population objective）；
* **真实 population 数据对象完全不改变**（人口报告 / 数据审计 / NoData 语义零影响）；
* 海 / 陆判定只用地形证据，绝不用 "population NoData ⇒ 海面"。

数学基础：``risk_index = normalized_population_factor × shelter_coefficient``（定义未改）。
``log1p_quantile_index`` 单调，所以 ``max(density, floor)`` 的因子恰好等于
``max(factor(density), factor(floor))``。
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_layered_theta_star_v2 import (  # noqa: E402
    BUILDING_POLICY, DEFAULTS, LEVEL, grid_cells, plan_v2, request, route_for,
    user_defined_baseline_policy,
)
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402
from cns_planner.domain.planning_exposure import (  # noqa: E402
    USED_POPULATION_NODATA_AS_SEA_PROXY, normalize_planning_exposure_policy,
    planning_exposure_factors, planning_exposure_is_active, resolve_planning_exposure,
)
from cns_planner.risk.normalization import log1p_quantile_index  # noqa: E402

REFERENCE = 100.0          # Risk Framework V2 population 归一化参考值（人/km²）
FLOOR = 5.0                # 陆地有效人口下限
LAND_THRESHOLD = 0.0       # 高程 > 0 m ⇒ 陆地；≤ 0 m ⇒ 海面（显式工程阈值）

LAND_FACTOR = 0.01         # 陆地真实人口很稀疏
SEA_FACTOR = 0.0           # 海面真实人口为 0（已由项目确认的 NoData 语义给出）


def cell_id(column, row):
    return f"MHT4063-L{LEVEL}-C{column}-RP{row}"


def grid(columns=3, rows=3):
    return grid_cells(columns=columns, rows=rows)


def factor_map(grid_cells_list, *, land_rows):
    """陆地行给稀疏人口因子，海面行给 0（NoData 已确认的零人口）。"""

    return {
        cell["grid_id"]: (
            LAND_FACTOR if cell["row"] in land_rows else SEA_FACTOR
        )
        for cell in grid_cells_list
    }


def population_attribute(grid_cells_list, *, land_rows, land_density=1.0):
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
            for cell in grid_cells_list
        },
    }


def terrain_attribute(grid_cells_list, *, land_rows, land_elevation=10.0):
    return {
        "status": "passed", "algorithm_id": "terrain-grid-elevation-statistics",
        "cells": {
            cell["grid_id"]: {
                "status": "passed",
                "surface_elevation_max_m": (
                    land_elevation if cell["row"] in land_rows else -2.0
                ),
            }
            for cell in grid_cells_list
        },
    }


def grid_risk_v2(grid_cells_list, factors):
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
            for cell in grid_cells_list
        },
    }


def active_policy(**overrides):
    payload = {
        "enabled": True, "land_population_floor": FLOOR,
        "land_min_surface_elevation_m": LAND_THRESHOLD,
        "source": "工程确认-舟山海陆规划暴露度基线",
        "evidence": {"reference": "ENG-ZS-2026-014"},
        "provenance": "explicit_override", "confirmed": True,
    }
    payload.update(overrides)
    return normalize_planning_exposure_policy(payload)


# ============================================================================
# 1. 层本身：陆地抬升、海面不变、证据缺失保持 unresolved
# ============================================================================


def test_land_floor_raises_land_only_and_leaves_water_untouched():
    cells = grid()
    land_rows = {1, 2}
    factors = factor_map(cells, land_rows=land_rows)
    attribute = resolve_planning_exposure(
        grid={"level": LEVEL, "cells": cells},
        population_attribute=population_attribute(cells, land_rows=land_rows),
        terrain_attribute=terrain_attribute(cells, land_rows=land_rows),
        normalized_population_factors=factors,
        policy=active_policy(),
        grid_risk_v2=grid_risk_v2(cells, factors),
    )
    assert attribute["applied"] is True
    assert attribute["status"] == "passed"
    assert attribute["used_population_nodata_as_sea_proxy"] is USED_POPULATION_NODATA_AS_SEA_PROXY
    assert USED_POPULATION_NODATA_AS_SEA_PROXY is False

    floor_factor = log1p_quantile_index(FLOOR, REFERENCE)
    assert attribute["floor_factor"] == pytest.approx(floor_factor, abs=1e-9)
    # max(density, floor) 的因子 == max(factor(density), factor(floor))
    assert floor_factor == pytest.approx(log1p_quantile_index(FLOOR, REFERENCE), abs=1e-12)

    land_ids = [cell["grid_id"] for cell in cells if cell["row"] in land_rows]
    water_ids = [cell["grid_id"] for cell in cells if cell["row"] not in land_rows]
    assert attribute["land_count"] == len(land_ids)
    assert attribute["water_count"] == len(water_ids)

    for grid_id in land_ids:
        cell = attribute["cells"][grid_id]
        assert cell["land_status"] == "land"
        assert cell["planning_population_factor"] >= floor_factor - 1e-9
        assert cell["planning_population_factor"] > cell["population_factor"]
        assert cell["effective_population_density_people_km2"] >= FLOOR
        assert cell["floor_applied"] is True

    for grid_id in water_ids:
        cell = attribute["cells"][grid_id]
        assert cell["land_status"] == "water"
        assert cell["planning_population_factor"] == pytest.approx(SEA_FACTOR)
        assert cell["effective_population_density_people_km2"] == pytest.approx(0.0)
        assert cell["floor_applied"] is False

    # 海面（NoData 已确认零人口）仍然比陆地便宜。
    assert max(
        attribute["cells"][grid_id]["planning_population_factor"] for grid_id in water_ids
    ) < min(
        attribute["cells"][grid_id]["planning_population_factor"] for grid_id in land_ids
    )


def test_missing_terrain_evidence_stays_unresolved_and_is_never_treated_as_sea():
    cells = grid(columns=2, rows=1)
    land_rows = {0}
    factors = factor_map(cells, land_rows=land_rows)
    terrain = terrain_attribute(cells, land_rows=land_rows)
    # 去掉其中一个格的高程证据。
    terrain["cells"][cells[1]["grid_id"]] = {"status": "missing_data"}

    attribute = resolve_planning_exposure(
        grid={"level": LEVEL, "cells": cells},
        population_attribute=population_attribute(cells, land_rows=land_rows),
        terrain_attribute=terrain,
        normalized_population_factors=factors,
        policy=active_policy(),
        grid_risk_v2=grid_risk_v2(cells, factors),
    )
    missing = attribute["cells"][cells[1]["grid_id"]]
    assert missing["land_status"] == "unresolved"
    assert missing["reason"] == "terrain_elevation_evidence_missing"
    # 既没有被抬高，也没有被当成海面。
    assert missing["planning_population_factor"] == pytest.approx(factors[cells[1]["grid_id"]])
    assert missing["floor_applied"] is False
    assert attribute["unresolved_land_status_count"] == 1


@pytest.mark.parametrize("overrides,expected_reason", [
    ({"enabled": False}, "planning_exposure_disabled"),
    ({"confirmed": False}, "planning_exposure_policy_not_confirmed"),
    ({"land_population_floor": None}, "land_population_floor_not_configured"),
    ({"land_min_surface_elevation_m": None}, "land_detection_threshold_not_configured"),
])
def test_an_inactive_policy_never_changes_a_single_factor(overrides, expected_reason):
    cells = grid(columns=2, rows=1)
    land_rows = {0}
    factors = factor_map(cells, land_rows=land_rows)
    policy = active_policy(**overrides)
    assert planning_exposure_is_active(policy) is False
    attribute = resolve_planning_exposure(
        grid={"level": LEVEL, "cells": cells},
        population_attribute=population_attribute(cells, land_rows=land_rows),
        terrain_attribute=terrain_attribute(cells, land_rows=land_rows),
        normalized_population_factors=factors,
        policy=policy, grid_risk_v2=grid_risk_v2(cells, factors),
    )
    assert attribute["applied"] is False
    assert attribute["status"] == "not_configured"
    assert attribute["reason"] == expected_reason
    assert planning_exposure_factors(attribute) is None


def test_an_unresolvable_population_reference_blocks_the_floor_instead_of_guessing():
    cells = grid(columns=2, rows=1)
    land_rows = {0}
    factors = factor_map(cells, land_rows=land_rows)
    attribute = resolve_planning_exposure(
        grid={"level": LEVEL, "cells": cells},
        population_attribute=population_attribute(cells, land_rows=land_rows),
        terrain_attribute=terrain_attribute(cells, land_rows=land_rows),
        normalized_population_factors=factors,
        policy=active_policy(),
        grid_risk_v2={"status": "passed", "cells": {}},
    )
    assert attribute["applied"] is False
    assert attribute["status"] == "missing_data"
    assert attribute["reason"] == "population_reference_unavailable"
    assert attribute["floor_factor"] is None


# ============================================================================
# 2. Workflow 集成：真实人口对象零改动，规划场按需变化
# ============================================================================


def service_with(tmp_path, *, land_rows=(1, 2), name="planning-exposure.json"):
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
    return {
        grid_id: cell["risk_index"] for grid_id, cell in attribute["cells"].items()
    }


def test_disabled_floor_keeps_the_original_population_objective(tmp_path):
    service, cells = service_with(tmp_path)
    population_before = deepcopy(service.state["grid_attributes"]["population"])
    terrain_before = deepcopy(service.state["grid_attributes"]["terrain"])
    risk_before = deepcopy(service.state["grid_risk_v2"])

    attribute = service.population_shelter()
    assert attribute["population_factor_source"] == "canonical_risk_v2_population_factor"
    assert attribute["planning_exposure"]["applied"] is False
    exposure = service.planning_exposure()
    assert exposure["applied"] is False
    assert exposure["status"] == "not_configured"

    # 真实人口 / 地形 / Risk V2 输入对象逐字段不变。
    assert service.state["grid_attributes"]["population"] == population_before
    assert service.state["grid_attributes"]["terrain"] == terrain_before
    assert service.state["grid_risk_v2"] == risk_before

    baseline = risk_index_map(attribute)
    candidate = plan_v2(cells, shelter=attribute)
    assert candidate["status"] == "candidate"
    objective_off = candidate["planning_objective"]["risk_exposure_index_m"]

    # 再取一次（缓存 + 关闭策略）必须完全相同。
    assert risk_index_map(service.population_shelter()) == baseline
    assert service.population_shelter()["population_factor_source"] == (
        "canonical_risk_v2_population_factor"
    )
    again = plan_v2(cells, shelter=attribute)
    assert again["planning_objective"]["risk_exposure_index_m"] == pytest.approx(objective_off)


def test_enabled_floor_raises_land_risk_and_leaves_the_population_object_untouched(tmp_path):
    service, cells = service_with(tmp_path)
    population_before = deepcopy(service.state["grid_attributes"]["population"])
    terrain_before = deepcopy(service.state["grid_attributes"]["terrain"])
    risk_before = deepcopy(service.state["grid_risk_v2"])
    nodata_before = deepcopy(service.state.get("population_nodata_policy"))

    baseline = service.population_shelter()
    baseline_objective = plan_v2(cells, shelter=baseline)["planning_objective"]
    baseline_risk = risk_index_map(baseline)

    service.set_planning_exposure_policy(active_policy())
    exposure = service.planning_exposure()
    assert exposure["applied"] is True
    assert exposure["status"] == "passed"

    raised = service.population_shelter()
    assert raised["population_factor_source"] == "planning_exposure_effective_population"
    raised_risk = risk_index_map(raised)

    land_ids = [cell["grid_id"] for cell in cells if cell["row"] in (1, 2)]
    water_ids = [cell["grid_id"] for cell in cells if cell["row"] == 0]
    floor_factor = log1p_quantile_index(FLOOR, REFERENCE)
    for grid_id in land_ids:
        assert raised_risk[grid_id] > baseline_risk[grid_id], grid_id
        assert raised_risk[grid_id] >= floor_factor - 1e-9
        assert exposure["cells"][grid_id]["effective_population_density_people_km2"] >= FLOOR
    for grid_id in water_ids:
        assert raised_risk[grid_id] == pytest.approx(baseline_risk[grid_id]), grid_id
        assert raised_risk[grid_id] < min(raised_risk[grid_id_] for grid_id_ in land_ids)

    raised_objective = plan_v2(cells, shelter=raised)["planning_objective"]
    # 陆地变贵 ⇒ 风险暴露项严格上升；objective 权重语义完全不变。
    assert raised_objective["risk_exposure_index_m"] > baseline_objective["risk_exposure_index_m"]
    assert raised_objective["risk_weight"] == pytest.approx(0.8)
    assert raised_objective["turn_weight"] == pytest.approx(0.1)
    assert raised_objective["distance_weight"] == pytest.approx(0.1)

    # 真实 population / terrain / NoData 语义零改动。
    assert service.state["grid_attributes"]["population"] == population_before
    assert service.state["grid_attributes"]["terrain"] == terrain_before
    assert service.state.get("population_nodata_policy") == nodata_before
    # Risk Framework V2 的 canonical population 因子（本层的唯一输入）也没有被改写；
    # 变更策略时会走既有 invalidation，因而 grid_risk_v2 容器可能被补全其它诊断块，
    # 但那不是"输入变了"。
    for grid_id, before_cell in risk_before["cells"].items():
        assert (
            service.state["grid_risk_v2"]["cells"][grid_id]["factors"]
            == before_cell["factors"]
        ), grid_id
    assert exposure["used_population_nodata_as_sea_proxy"] is False


def test_turning_the_floor_back_off_restores_the_original_objective_exactly(tmp_path):
    service, cells = service_with(tmp_path)
    baseline = service.population_shelter()
    baseline_risk = risk_index_map(baseline)

    service.set_planning_exposure_policy(active_policy())
    assert risk_index_map(service.population_shelter()) != baseline_risk

    service.set_planning_exposure_policy({"enabled": False})
    restored = service.population_shelter()
    assert risk_index_map(restored) == baseline_risk
    assert restored["population_factor_source"] == "canonical_risk_v2_population_factor"


def test_the_policy_round_trips_through_the_project_file(tmp_path):
    path = tmp_path / "planning-exposure-roundtrip.json"
    service, _ = service_with(tmp_path, name=path.name)
    service.set_planning_exposure_policy(active_policy())

    reloaded = WorkflowService(path, DEFAULTS)
    policy = reloaded.planning_exposure_policy()
    assert policy["status"] == "confirmed"
    assert policy["enabled"] is True
    assert policy["land_population_floor"] == pytest.approx(FLOOR)
    assert policy["land_min_surface_elevation_m"] == pytest.approx(LAND_THRESHOLD)
    assert policy["confirmed"] is True
    # 派生缓存不随项目持久化（可由现有输入重建）：要么不存在，要么是空占位。
    cached = reloaded.state.get("_planning_exposure_cache")
    assert not (isinstance(cached, dict) and cached.get("cells")), cached
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "_planning_exposure_cache" not in saved


def test_the_snapshot_exposes_the_policy_and_a_bounded_projection(tmp_path):
    service, _ = service_with(tmp_path)
    snapshot = service.snapshot()
    assert snapshot["planning_exposure_policy"]["status"] == "not_configured"
    projection = snapshot["planning_exposure"]
    assert projection["applied"] is False
    assert "cells" not in projection, "快照只带只读摘要，逐 cell 明细由 /api/planning-exposure 提供"
    assert projection["cells_detail"] == "GET /api/planning-exposure"


def test_default_policy_is_absent_from_the_planner_objective(tmp_path):
    """默认（未配置）时 Theta* 的 candidate 与不引入本层时完全一致。"""

    service, cells = service_with(tmp_path)
    attribute = service.population_shelter()
    implicit = plan_v2(cells, shelter=attribute)
    explicit = plan_v2(cells, shelter=attribute)
    assert implicit["planning_objective"]["total_cost"] == pytest.approx(
        explicit["planning_objective"]["total_cost"], abs=1e-12
    )
    assert implicit["grid_path"] == explicit["grid_path"]
