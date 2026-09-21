"""真实人口来源 NoData 语义确认（additive、需显式确认）的回归测试。

锁定三件事：
1. 未确认时 population 映射与历史行为**完全一致**（NoData 保持 missing_data，绝不补 0）；
2. 只有带 mode + source + evidence 的显式确认才生效，且只转换**来源 extent 之内**、
   全部像元为 NoData 的格子；outside_extent 永远保持 unknown；
3. 确认后该格子在 Risk Framework V2 的 population factor 上是 ``passed``（因此
   population × shelter 风险可解析），但它是"已知的 0"而不是观测值。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from cns_planner.data.mapping.population import PopulationGridService
from cns_planner.domain.population_nodata import (
    CONFIRMED_ZERO_COVERAGE_STATUS, MODE_NODATA_IS_ZERO_POPULATION,
    default_population_nodata_policy, nodata_semantics_record,
    normalize_population_nodata_policy, policy_fingerprint, policy_is_confirmed,
)
from cns_planner.gis.layered_feasibility_adapter import layered_feasibility_source_status
from cns_planner.risk.factors_v2 import FactorExtraction
from cns_planner.services.grid_service import WorkspaceGridService

CONFIRMATION = {
    "mode": MODE_NODATA_IS_ZERO_POPULATION,
    "source": "user_confirmation",
    "source_id": "worldpop-r2025a-population-count",
    "evidence": {
        "product": "WorldPop Population Counts R2025A",
        "note": "海上/无人区像元为 NoData，表示无居住人口。",
    },
    "confirmed": True,
}


class StubPopulationAdapter:
    """Deterministic adapter: in-footprint NoData, out-of-extent, and a pathological edge cell.

    The ``edge`` cell models the real failure mode seen on the Zhoushan corridor: a grid cell
    that intersects a single source pixel by a vanishing area fraction, so ``read_values``
    (pixel-centre test) is empty while ``read_population_count`` extrapolates 2e-7 people into
    a ~79 person/km² "observed covered-area density".
    """

    def __init__(self, inside_bbox, outside_bbox, edge_bbox=None):
        self.inside = tuple(round(value, 8) for value in inside_bbox)
        self.outside = tuple(round(value, 8) for value in outside_bbox)
        self.edge = None if edge_bbox is None else tuple(round(value, 8) for value in edge_bbox)

    def describe(self):
        return {
            "path": "worldpop_fixture.tif", "crs": "EPSG:4326", "band": 1,
            "width": 100, "height": 100, "pixel_size": [0.00083333333, 0.00083333333],
            "nodata": -99999.0, "unit_metadata": None, "scale": 1.0, "offset": 0.0,
        }

    def read_values(self, bbox):
        return []

    def read_population_count(self, bbox, nodata_semantics=None):
        key = tuple(round(value, 8) for value in bbox)
        outside = {
            "status": "missing_data", "value_status": "missing_data",
            "coverage_status": "outside_extent", "population_count_people": None,
            "population_density_people_km2": None, "density_support_area_m2": None,
            "density_semantics": None, "target_area_m2": 1.0, "valid_covered_area_m2": 0.0,
            "source_coverage_fraction": 0.0, "source_pixel_count": 0,
            "quality_flags": ["outside_source_extent"],
        }
        if self.edge is not None and key == self.edge:
            return {
                **outside, "status": "missing_data", "value_status": "passed",
                "coverage_status": "partial", "population_count_people": 2.226748362514787e-07,
                "population_density_people_km2": 79.1369679522076,
                "density_support_area_m2": 0.002814, "valid_covered_area_m2": 0.002814,
                "target_area_m2": 119000.0,
                "source_coverage_fraction": 2.3598325478613725e-08, "source_pixel_count": 1,
                "quality_flags": ["partial_source_coverage", "not_extrapolated"],
            }
        if key == self.outside or key != self.inside:
            return outside
        if not policy_is_confirmed(nodata_semantics):
            return {
                **outside, "coverage_status": "nodata_only",
                "quality_flags": ["no_valid_source_pixels"], "nodata_pixel_count": 4,
            }
        record = nodata_semantics_record(nodata_semantics)
        return {
            "status": "passed", "value_status": "passed",
            "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
            "population_count_people": 0.0, "population_density_people_km2": 0.0,
            "density_support_area_m2": 1.0,
            "density_semantics": "confirmed_zero_population_exposure",
            "target_area_m2": 1.0, "valid_covered_area_m2": 1.0,
            "source_coverage_fraction": 1.0, "source_pixel_count": 0,
            "nodata_pixel_count": 4,
            "quality_flags": list(record["quality_flags"]),
            "nodata_semantics": record,
        }


def _grid():
    return WorkspaceGridService(preferred_level=6).generate([120.001, 30.001, 120.02, 30.02])


# --------------------------------------------------------------- policy contract


def test_default_policy_is_never_confirmed():
    policy = default_population_nodata_policy()
    assert policy["status"] == "not_configured"
    assert policy["mode"] is None
    assert policy["confirmed"] is False
    assert policy_is_confirmed(policy) is False
    assert nodata_semantics_record(policy) is None


def test_confirmation_requires_mode_source_and_evidence():
    assert policy_is_confirmed(normalize_population_nodata_policy(CONFIRMATION)) is True

    for broken in (
        {**CONFIRMATION, "evidence": None},
        {**CONFIRMATION, "evidence": {}},
        {**CONFIRMATION, "source": ""},
        {**CONFIRMATION, "mode": "something_else"},
        {**CONFIRMATION, "confirmed": False},
    ):
        normalized = normalize_population_nodata_policy(broken)
        assert normalized["status"] == "not_configured", broken
        assert policy_is_confirmed(normalized) is False


def test_policy_fingerprint_is_stable_and_evidence_sensitive():
    first = normalize_population_nodata_policy(CONFIRMATION)
    second = normalize_population_nodata_policy(deepcopy(CONFIRMATION))
    assert policy_fingerprint(first) == policy_fingerprint(second)

    changed = normalize_population_nodata_policy({
        **CONFIRMATION, "evidence": {**CONFIRMATION["evidence"], "note": "另一份证据"},
    })
    assert policy_fingerprint(changed) != policy_fingerprint(first)


# ------------------------------------------------------------------ mapping layer


def _map(grid, adapter, policy):
    service = PopulationGridService(adapter_factory=lambda _path: adapter)
    return service.map(grid, "worldpop_fixture.tif", policy)


def test_unconfirmed_policy_keeps_historical_missing_data():
    grid = _grid()
    inside = grid["cells"][0]["bbox"]
    outside = grid["cells"][1]["bbox"]
    adapter = StubPopulationAdapter(inside, outside)

    for policy in (None, default_population_nodata_policy()):
        attribute = _map(grid, adapter, policy)
        cell = attribute["cells"][grid["cells"][0]["grid_id"]]
        assert cell["coverage_status"] == "nodata_only"
        assert cell["value_status"] == "missing_data"
        assert cell["population_count_people"] is None
        assert attribute["confirmed_zero_population_count"] == 0
        assert attribute["status"] == "missing_data"


def test_confirmed_policy_records_in_footprint_nodata_as_known_zero():
    grid = _grid()
    inside = grid["cells"][0]["bbox"]
    outside = grid["cells"][1]["bbox"]
    adapter = StubPopulationAdapter(inside, outside)
    policy = normalize_population_nodata_policy(CONFIRMATION)

    attribute = _map(grid, adapter, policy)
    cell = attribute["cells"][grid["cells"][0]["grid_id"]]

    assert cell["status"] == "passed"
    assert cell["coverage_status"] == CONFIRMED_ZERO_COVERAGE_STATUS
    assert cell["value_status"] == "passed"
    assert cell["population_count_people"] == 0.0
    assert cell["population_density_people_km2"] == 0.0
    assert cell["nodata_semantics"]["mode"] == MODE_NODATA_IS_ZERO_POPULATION
    assert "nodata_interpreted_as_zero_population" in cell["quality_flags"]
    assert attribute["confirmed_zero_population_count"] == 1
    assert attribute["nodata_semantics_policy_fingerprint"] == policy_fingerprint(policy)

    # The out-of-extent neighbour must stay unknown: the confirmation never widens the raster.
    neighbour = attribute["cells"][grid["cells"][1]["grid_id"]]
    assert neighbour["coverage_status"] == "outside_extent"
    assert neighbour["population_count_people"] is None
    assert "nodata_semantics" not in neighbour


def test_confirmed_policy_treats_cell_without_observation_coverage_as_known_zero():
    grid = _grid()
    cells = grid["cells"]
    inside, outside, edge = cells[0]["bbox"], cells[1]["bbox"], cells[2]["bbox"]
    adapter = StubPopulationAdapter(inside, outside, edge_bbox=edge)
    policy = normalize_population_nodata_policy(CONFIRMATION)

    attribute = _map(grid, adapter, policy)
    edge_cell = attribute["cells"][cells[2]["grid_id"]]

    # The pathological extrapolation (2e-7 people -> 79 person/km²) is replaced by a known 0,
    # and the original quality flags are retained as audit evidence.
    assert edge_cell["status"] == "passed"
    assert edge_cell["coverage_status"] == CONFIRMED_ZERO_COVERAGE_STATUS
    assert edge_cell["population_density_people_km2"] == 0.0
    assert edge_cell["nodata_semantics"]["trigger"] == "no_source_pixel_centre_inside_cell"
    assert "partial_source_coverage" in edge_cell["quality_flags"]

    # Without the confirmation the historical (pathological) behaviour is unchanged.
    unconfirmed = _map(grid, adapter, None)
    raw_edge = unconfirmed["cells"][cells[2]["grid_id"]]
    assert raw_edge["coverage_status"] == "partial"
    assert raw_edge["status"] == "missing_data"
    assert abs(raw_edge["population_density_people_km2"] - 79.13) < 0.1


# ------------------------------------------------------------- downstream factors


def _population_attribute(cells):
    return {
        "status": "passed",
        "value_status": "passed",
        "unit_status": "unverified",
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "grid_level": 6,
        "source": {"path": "worldpop_fixture.tif"},
        "algorithm_id": "population-grid-raw-statistics",
        "sampling_version": "1.1",
        "cells": dict(cells),
    }


def _confirmed_zero_cell():
    return {
        "status": "passed", "value_status": "passed",
        "coverage_status": CONFIRMED_ZERO_COVERAGE_STATUS,
        "population_count_people": 0.0, "population_density_people_km2": 0.0,
        "source_coverage_fraction": 1.0, "valid_covered_area_m2": 1.0,
        "density_support_area_m2": 1.0, "grid_area_m2": 1.0,
        "density_semantics": "confirmed_zero_population_exposure",
    }


def test_confirmed_zero_cell_is_a_passed_population_factor():
    grid = _grid()
    grid_ids = [cell["grid_id"] for cell in grid["cells"]]
    zero_id, inhabited_id = grid_ids[0], grid_ids[1]

    # The dataset quantile reference is built from the *resolved* cells, so a real project
    # always has positive densities alongside the confirmed-zero marine cells.
    inhabited = {
        **_confirmed_zero_cell(),
        "population_count_people": 500.0, "population_density_people_km2": 500.0,
    }
    result = FactorExtraction(grid, {
        "population": _population_attribute({zero_id: _confirmed_zero_cell(), inhabited_id: inhabited}),
    }).extract()
    record = result["factors"]["population_exposure"][zero_id]

    assert record["status"] == "passed"
    assert record["raw_value"] == 0.0
    # A confirmed zero is a real, usable index — not a missing value that blocks the search.
    assert record["normalized_index"] == 0.0

    # Without the confirmation the very same cell stays fail-closed on unknown.
    unknown_cell = {
        **_confirmed_zero_cell(), "status": "missing_data", "value_status": "missing_data",
        "coverage_status": "nodata_only", "population_density_people_km2": None,
    }
    unknown = FactorExtraction(grid, {
        "population": _population_attribute({zero_id: unknown_cell, inhabited_id: inhabited}),
    }).extract()
    assert unknown["factors"]["population_exposure"][zero_id]["status"] != "passed"


# ------------------------------------------------------------------- readiness keys


def test_feasibility_source_status_exposes_terrain_and_population_contracts():
    status = layered_feasibility_source_status({
        "source_audits": {"items": {
            "terrain_dtm": {"status": "verified"},
            "buildings": {"status": "verified"},
            "building_grid": {"status": "verified"},
        }},
        "grid_attributes": {"population": {
            "status": "passed", "value_status": "passed", "coverage_status": "partial",
            "nodata_only_count": 1395, "confirmed_zero_population_count": 1395,
            "nodata_semantics": {"mode": MODE_NODATA_IS_ZERO_POPULATION},
        }},
    }, terrain_path="D:/data/fabdem.tif")

    # The readiness projection and the UI both read ``sources.terrain``: reporting the status
    # only under ``terrain_dtm`` made every project look like the FABDEM source was missing.
    assert status["terrain"]["available"] is True
    assert status["terrain"]["reason"] is None
    assert status["terrain_dtm"]["available"] is True
    assert status["population"]["available"] is True
    assert status["population"]["confirmed_zero_population_count"] == 1395

    missing = layered_feasibility_source_status({
        "source_audits": {"items": {"terrain_dtm": {"status": "needs_revalidation"}}},
        "grid_attributes": {},
    }, terrain_path="D:/data/fabdem.tif")
    assert missing["terrain"]["available"] is False
    assert missing["terrain"]["reason"] == "terrain_source_audit_needs_revalidation"
    assert missing["population"]["available"] is False

    unconfigured = layered_feasibility_source_status({}, terrain_path=None)
    assert unconfigured["terrain"]["available"] is False
    assert unconfigured["terrain"]["reason"] == "terrain_source_not_configured"


# ------------------------------------------------------------------ deferred commit


def _session(tmp_path: Path):
    from cns_planner.application.session import WorkflowSession

    defaults = tmp_path / "config" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(
        Path("cns_planner/config/defaults.json").read_text(encoding="utf-8"), encoding="utf-8",
    )
    return WorkflowSession(tmp_path / "project.json", defaults, WorkspaceGridService())


def test_deferred_save_collapses_a_multi_step_action_into_one_commit(tmp_path):
    session = _session(tmp_path)
    session.save()
    baseline = session.state["revision"]

    with session.lock:
        session.defer_save = True
        session.state["grid_attributes"] = {"marker": 1}
        session.save()
        # Nothing reached the disk yet: a concurrent reader cannot see the intermediate step.
        assert session.pending_save is True
        import json

        assert json.loads((tmp_path / "project.json").read_text(encoding="utf-8"))["revision"] == baseline
        session.state["grid_attributes"] = {"marker": 2}
        session.save()
        session.defer_save = False
    assert session.commit_deferred() is True
    assert session.state["revision"] == baseline + 1

    document = __import__("json").loads((tmp_path / "project.json").read_text(encoding="utf-8"))
    assert document["revision"] == baseline + 1
    assert document["grid_attributes"]["marker"] == 2
    # A second commit with nothing pending must be a no-op.
    assert session.commit_deferred() is False
    assert session.state["revision"] == baseline + 1
