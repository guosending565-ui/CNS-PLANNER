"""Round35-D 定向回归：开阔海域零人口**工程假设**模式与格级陆域缺席证据。

断言对象（任务书 D3-B）：

* 新模式与既有模式并存、互斥确认；未确认时行为与历史完全一致；
* 适用条件逐条可审计：整格在人口栅格有效覆盖范围内、canonical 陆域可用、
  格与陆域多边形**无交集**、不在海岸不确定带内、确实缺少有效观测；
* **格心判为 sea 但格的边缘包含陆地**的格子绝不被当作开阔海域；
* 与陆域相交的缺失人口格永远保持 ``missing_data``（不静默补零）；
* 数据有效 footprint 之外的格子不得被自动当海；
* 零人口来自"工程假设"，与原始观测显式可区分。
"""

from __future__ import annotations

import json

import pytest

from cns_planner.data.mapping.population import OPEN_SEA_ZERO_TRIGGER, PopulationGridService
from cns_planner.domain.quantities import geographic_bbox_area_m2
from cns_planner.domain.population_nodata import (
    CONFIRMED_ZERO_COVERAGE_STATUS, MODE_NODATA_IS_ZERO_POPULATION,
    MODE_OPEN_SEA_NO_SETTLEMENT_ZERO, OPEN_SEA_ELIGIBILITY_CONDITIONS,
    confirmed_zero_allocation, default_population_nodata_policy,
    normalize_population_nodata_policy, open_sea_evidence_required, open_sea_zero_permitted,
    policy_mode,
)
from cns_planner.gis.population_nodata_evidence import (
    build_open_sea_evidence, open_sea_evidence_summary,
)
from cns_planner.gis.radar_layout_adapter import LandMaskSource

LAND_A = (122.0000, 30.0000, 122.0020, 30.0020)
LAND_B = (122.0100, 30.0000, 122.0120, 30.0020)


def _land_geojson(tmp_path, rectangles=(LAND_A, LAND_B), name="land.geojson"):
    features = [
        {
            "type": "Feature", "properties": {"name": "land"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [west, south], [east, south], [east, north], [west, north], [west, south],
            ]]},
        }
        for west, south, east, north in rectangles
    ]
    path = tmp_path / name
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8",
    )
    return path


def _source(tmp_path, *, buffer_m=30.0, **kwargs):
    return LandMaskSource(
        _land_geojson(tmp_path, **kwargs), coastal_uncertainty_buffer_m=buffer_m,
    )


# ------------------------------------------------------------- policy 契约


def _open_sea_policy(source="工程确认-round35d"):
    return normalize_population_nodata_policy({
        "mode": MODE_OPEN_SEA_NO_SETTLEMENT_ZERO,
        "source": source,
        "evidence": {"review": "round35d", "land_mask": "zhoushan_land_osm_v2.gpkg"},
        "confirmed": True,
    })


def test_open_sea_mode_is_confirmed_only_with_explicit_mode_source_and_evidence():
    assert policy_mode(_open_sea_policy()) == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO
    assert open_sea_evidence_required(_open_sea_policy()) is True
    # 缺证据 ⇒ 绝不确认。
    assert policy_mode(normalize_population_nodata_policy({
        "mode": MODE_OPEN_SEA_NO_SETTLEMENT_ZERO, "source": "x", "confirmed": True,
    })) is None
    # 未确认默认值不要求逐格证据，且行为与历史一致。
    assert open_sea_evidence_required(default_population_nodata_policy()) is False
    assert policy_mode(normalize_population_nodata_policy({
        "mode": MODE_NODATA_IS_ZERO_POPULATION, "source": "x",
        "evidence": {"a": 1}, "confirmed": True,
    })) == MODE_NODATA_IS_ZERO_POPULATION
    assert open_sea_evidence_required(normalize_population_nodata_policy({
        "mode": MODE_NODATA_IS_ZERO_POPULATION, "source": "x",
        "evidence": {"a": 1}, "confirmed": True,
    })) is False


def test_open_sea_eligibility_conditions_are_declared_and_enforced():
    policy = _open_sea_policy()
    full = {
        "relation": "open_sea", "land_mask_available": True,
        "inside_population_footprint": True, "does_not_intersect_land": True,
        "outside_coastal_uncertainty_buffer": True, "no_valid_population_observation": True,
    }
    permitted, reason = open_sea_zero_permitted(policy, full)
    assert permitted is True and reason == OPEN_SEA_ZERO_TRIGGER
    for key in (
        "land_mask_available", "inside_population_footprint", "does_not_intersect_land",
        "outside_coastal_uncertainty_buffer", "no_valid_population_observation",
    ):
        broken = dict(full, **{key: False})
        permitted, reason = open_sea_zero_permitted(policy, broken)
        assert permitted is False
        assert reason == f"open_sea_condition_failed:{key}"
    for relation in ("land_intersects", "within_coastal_buffer", "outside_data_footprint",
                     "unknown", None):
        permitted, reason = open_sea_zero_permitted(policy, dict(full, relation=relation))
        assert permitted is False
        assert reason in (
            "land_intersects", "within_coastal_buffer", "outside_data_footprint",
            "unknown", "missing_open_sea_evidence",
        )
    # 模式一绝不走"开阔海域"这条路径（它有自己的 adapter 侧判据）。
    legacy = normalize_population_nodata_policy({
        "mode": MODE_NODATA_IS_ZERO_POPULATION, "source": "x",
        "evidence": {"a": 1}, "confirmed": True,
    })
    assert open_sea_zero_permitted(legacy, full) == (False, "policy_mode_is_not_open_sea")
    assert len(OPEN_SEA_ELIGIBILITY_CONDITIONS) == 6
    assert "evidence_is_an_engineering_assumption_not_an_observation" in (
        OPEN_SEA_ELIGIBILITY_CONDITIONS
    )


def test_confirmed_zero_allocation_marks_open_sea_as_engineering_assumption():
    allocation = confirmed_zero_allocation(1_000_000.0, _open_sea_policy())
    assert allocation["population_count_people"] == 0.0
    assert allocation["interpretation"] == "open_sea_no_settlement_engineering_assumption"
    assert allocation["nodata_semantics"]["is_engineering_assumption_not_observation"] is True
    assert "open_sea_no_settlement_engineering_assumption" in allocation["quality_flags"]
    legacy = confirmed_zero_allocation(1_000_000.0, normalize_population_nodata_policy({
        "mode": MODE_NODATA_IS_ZERO_POPULATION, "source": "x",
        "evidence": {"a": 1}, "confirmed": True,
    }))
    assert legacy["interpretation"] == "confirmed_zero_population_not_an_observation"
    assert legacy["nodata_semantics"]["is_engineering_assumption_not_observation"] is False


# ------------------------------------------------------------- 格级陆域探测


def test_cell_land_relation_distinguishes_open_sea_land_edge_and_buffer(tmp_path):
    source = _source(tmp_path, buffer_m=30.0)
    # 两个陆域之间的空隙：格与二者都不相交，且距离 > 100 m ⇒ 开阔海域。
    open_sea = source.cell_land_relation([122.0040, 30.0008, 122.0060, 30.0012])
    assert open_sea["relation"] == "open_sea"
    assert open_sea["does_not_intersect_land"] is True
    assert open_sea["outside_coastal_uncertainty_buffer"] is True
    assert open_sea["land_mask_available"] is True

    # 格心判 sea、但格的边缘实际包含陆地：**不得**判为开阔海域。
    edge = [122.0015, 30.0008, 122.0035, 30.0012]
    center = ((edge[0] + edge[2]) / 2.0, (edge[1] + edge[3]) / 2.0)
    center_surface, _evidence = source.classify(*center)
    assert center_surface == "sea", "本用例的前提：格心确实是 sea"
    relation = source.cell_land_relation(edge)
    assert relation["relation"] == "land_intersects"
    assert relation["does_not_intersect_land"] is False
    assert relation["land_intersection_count"] >= 1

    # 与陆域不相交但落在海岸不确定带内：仍不是开阔海域。
    buffer_cell = source.cell_land_relation([122.0022, 30.0008, 122.0030, 30.0012])
    assert buffer_cell["relation"] == "within_coastal_buffer"
    assert buffer_cell["does_not_intersect_land"] is True
    assert buffer_cell["outside_coastal_uncertainty_buffer"] is False
    assert 0.0 < buffer_cell["distance_to_land_boundary_m"] <= 30.0

    # 数据有效 footprint 之外：无证据，不得自动当海。
    outside = source.cell_land_relation([122.0200, 30.0000, 122.0210, 30.0010])
    assert outside["relation"] == "outside_data_footprint"
    assert outside["land_mask_available"] is True
    assert outside["reason"] == "cell_not_wholly_inside_land_mask_footprint"


def test_cell_land_relation_fails_closed_without_land_mask(tmp_path):
    source = LandMaskSource(tmp_path / "missing.gpkg", coastal_uncertainty_buffer_m=30.0)
    relation = source.cell_land_relation(LAND_A)
    assert relation["relation"] == "unknown"
    assert relation["land_mask_available"] is False


def test_open_sea_evidence_builder_is_zero_cost_when_mode_not_confirmed(tmp_path):
    grid = {"cells": [{"grid_id": "G1", "bbox": list(LAND_A)}]}
    assert build_open_sea_evidence(
        grid, land_mask_path=_land_geojson(tmp_path),
        surface_policy={"coastal_uncertainty_buffer_m": 100.0},
        nodata_policy=default_population_nodata_policy(),
    ) is None
    evidence = build_open_sea_evidence(
        grid, land_mask_path=_land_geojson(tmp_path),
        surface_policy={"coastal_uncertainty_buffer_m": 100.0},
        nodata_policy=_open_sea_policy(),
    )
    assert set(evidence) == {"G1"}
    summary = open_sea_evidence_summary(evidence)
    assert summary["status"] == "built"
    assert summary["counts"]["land_intersects"] == 1
    assert summary["land_intersects_never_becomes_zero"] is True
    # 没有 canonical 陆域源 ⇒ 空证据（逐格 fail-closed），绝不"没有掩膜就当海"。
    assert build_open_sea_evidence(
        grid, land_mask_path=None,
        surface_policy={"coastal_uncertainty_buffer_m": 100.0},
        nodata_policy=_open_sea_policy(),
    ) == {}


# ------------------------------------------------------------- 映射层接线


class _NoObservationAdapter:
    """无任何有效人口观测的替身（格内无像元中心、全部像元 NoData）。"""

    def __init__(self, path, *, inside_extent=True):
        self.path = path
        self._inside = inside_extent

    def describe(self):
        return {
            "path": str(self.path), "crs": "EPSG:4326", "band": 1,
            "width": 10, "height": 10, "pixel_size": [1 / 1200, 1 / 1200],
            "nodata": -99999.0, "unit_metadata": "people", "scale": 1.0, "offset": 0.0,
        }

    def read_values(self, bbox):
        return []

    def read_population_count(self, bbox, nodata_semantics=None):
        area = geographic_bbox_area_m2(bbox)
        return {
            "status": "missing_data", "value_status": "missing_data",
            "coverage_status": "nodata_only", "population_count_people": None,
            "population_density_people_km2": None, "density_support_area_m2": None,
            "density_semantics": None, "target_area_m2": area,
            "valid_covered_area_m2": 0.0, "source_coverage_fraction": 0.0,
            "source_pixel_count": 0, "quality_flags": ["no_valid_source_pixels"],
        }

    def inside_extent(self, bbox):
        return self._inside


GRID = {
    "status": "passed", "level": 8,
    "cells": [{"grid_id": "SEA", "bbox": [122.0040, 30.0008, 122.0060, 30.0012]},
              {"grid_id": "LAND", "bbox": [122.0015, 30.0008, 122.0035, 30.0012]}],
}


def _map(*, policy, evidence=None, inside_extent=True):
    return PopulationGridService(
        lambda path: _NoObservationAdapter(path, inside_extent=inside_extent)
    ).map(GRID, "population.tif", policy, open_sea_evidence=evidence)


def test_unconfirmed_policy_never_fills_zero(tmp_path):
    result = _map(policy=None, evidence=None)
    for cell in result["cells"].values():
        assert cell["coverage_status"] == "nodata_only"
        assert cell["value_status"] == "missing_data"
        assert cell["population_count_people"] is None
    assert result["open_sea_zero_policy"] is None


def test_open_sea_mode_zeroes_only_cells_with_land_absence_evidence(tmp_path):
    source = _source(tmp_path, buffer_m=100.0)
    evidence = {
        grid_id: source.cell_land_relation(cell["bbox"])
        for cell in GRID["cells"] for grid_id in [cell["grid_id"]]
    }
    assert evidence["SEA"]["relation"] == "open_sea"
    assert evidence["LAND"]["relation"] == "land_intersects"

    result = _map(policy=_open_sea_policy(), evidence=evidence)
    sea = result["cells"]["SEA"]
    land = result["cells"]["LAND"]

    # 开阔海域：按显式工程假设记为已知 0，并带完整来源/证据。
    assert sea["coverage_status"] == CONFIRMED_ZERO_COVERAGE_STATUS
    assert sea["population_count_people"] == 0.0
    assert sea["value_status"] == "passed"
    assert sea["nodata_semantics"]["trigger"] == OPEN_SEA_ZERO_TRIGGER
    assert sea["nodata_semantics"]["mode"] == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO
    assert "open_sea_no_settlement_engineering_assumption" in sea["quality_flags"]
    assert sea["open_sea_evidence"]["relation"] == "open_sea"

    # 与陆域相交：绝不判 0，仍是 missing_data，并记录拒绝原因。
    assert land["coverage_status"] == "nodata_only"
    assert land["value_status"] == "missing_data"
    assert land["population_count_people"] is None
    assert land["confirmed_zero_denied_reason"] == "land_intersects"

    summary = result["open_sea_zero_policy"]
    assert summary["mode"] == MODE_OPEN_SEA_NO_SETTLEMENT_ZERO
    assert summary["is_engineering_assumption_not_observation"] is True
    assert summary["evidence_summary"]["counts"]["open_sea"] == 1
    assert summary["evidence_summary"]["counts"]["land_intersects"] == 1


def test_open_sea_mode_requires_footprint_and_evidence():
    # 整格不在人口栅格有效覆盖范围内 ⇒ 拒绝判 0。
    outside = _map(
        policy=_open_sea_policy(),
        evidence={"SEA": {"relation": "open_sea", "land_mask_available": True,
                          "does_not_intersect_land": True,
                          "outside_coastal_uncertainty_buffer": True}},
        inside_extent=False,
    )
    assert outside["cells"]["SEA"]["coverage_status"] == "nodata_only"
    assert outside["cells"]["SEA"]["confirmed_zero_denied_reason"] == (
        "open_sea_condition_failed:inside_population_footprint"
    )
    # 完全没有证据（例如没有 canonical 陆域源）⇒ 逐格拒绝。
    no_evidence = _map(policy=_open_sea_policy(), evidence={})
    assert no_evidence["cells"]["SEA"]["coverage_status"] == "nodata_only"
    assert no_evidence["cells"]["SEA"]["confirmed_zero_denied_reason"] == (
        "missing_open_sea_evidence"
    )


def test_legacy_mode_keeps_its_own_behaviour_unchanged():
    """模式一的既有语义不受本次新增影响（无观测 ⇒ 已知 0，不看陆域证据）。"""

    legacy = normalize_population_nodata_policy({
        "mode": MODE_NODATA_IS_ZERO_POPULATION, "source": "legacy-confirmation",
        "evidence": {"review": "round35c"}, "confirmed": True,
    })
    result = _map(policy=legacy, evidence=None)
    for cell in result["cells"].values():
        assert cell["coverage_status"] == CONFIRMED_ZERO_COVERAGE_STATUS
        assert cell["population_count_people"] == 0.0
        assert cell["nodata_semantics"]["trigger"] == "no_source_pixel_centre_inside_cell"
    assert result["open_sea_zero_policy"] is None


def test_valid_observation_is_never_overwritten_by_open_sea_assumption():
    """有真实观测（哪怕人口极小）就绝不是"缺少观测"，不得判 0。"""

    trigger, reason = PopulationGridService._confirmed_zero_trigger(
        _open_sea_policy(),
        {"grid_id": "G", "bbox": [122.004, 30.0008, 122.006, 30.0012]},
        [0.5],
        {"coverage_status": "partial", "population_count_people": 0.4},
        _NoObservationAdapter("x"),
        {"G": {"relation": "open_sea", "land_mask_available": True,
               "does_not_intersect_land": True,
               "outside_coastal_uncertainty_buffer": True}},
    )
    assert trigger is None
    assert reason == "valid_population_observation_present"


def test_adapter_never_judges_open_sea_zero_by_itself():
    """adapter 不持有陆域几何：新模式下一律返回 None，由映射层用证据裁定。"""

    from cns_planner.gis.raster_adapter import GdalRasterAdapter

    class _Probe(GdalRasterAdapter):
        def __init__(self, policy):  # noqa: D107 - 只测模式分派，不构造 GDAL 连接
            self._policy = policy

        def _inside_extent(self, bbox):  # noqa: D102
            return True

    adapter = _Probe(_open_sea_policy())
    assert adapter._confirmed_zero_population(_open_sea_policy(), [122.0, 30.0, 122.01, 30.01]) is None


def test_open_sea_evidence_summary_is_auditable():
    summary = open_sea_evidence_summary({
        "a": {"relation": "open_sea"}, "b": {"relation": "land_intersects"},
        "c": {"relation": "weird"}, "d": {},
    })
    assert summary["counts"] == {
        "open_sea": 1, "land_intersects": 1, "within_coastal_buffer": 0,
        "outside_data_footprint": 0, "unknown": 2,
    }
    assert summary["total"] == 4
    assert open_sea_evidence_summary(None)["status"] == "not_built"
    # 未确认策略时绝不产生"已知 0"记录：missing_data != zero。
    assert confirmed_zero_allocation(1.0, default_population_nodata_policy()) is None
