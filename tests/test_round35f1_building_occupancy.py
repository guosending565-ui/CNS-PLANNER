"""Round35-F1 / F1-A + F1-C regression: building occupancy and height-value semantics.

The defect being pinned down (Round35-E risk point 4): ``building_count`` is a
**centroid allocation count**, but three production consumption paths treated
``building_count == 0`` as proof that the cell contains no building at all.  20,491
real L8 cells (25.82 km2 of real footprint) satisfied
``building_count == 0 AND building_coverage_ratio > 0`` and were therefore cleared as
"no vertical building constraint".

This file asserts the *contract*, not an implementation detail:

* ``building_count == 0`` + confirmed-zero footprint coverage ⇒ unchanged fast path;
* ``building_count == 0`` + coverage > 0 ⇒ ``unknown`` (never ``feasible``/``pass``,
  and never ``blocked`` either, because the unresolved building may be low);
* the same fact must receive the same semantic from the coarse mask, the Planning
  Constraint Field and the V3 environment adapter;
* the official height sentinels (``-999`` NoData, ``-1.0`` unavailable) are never
  treated as usable algebra, and ``height_var`` is never treated as a reliability
  signal;
* "height present" and "height trusted" are separate answers.
"""

from pathlib import Path

import pytest

from cns_planner.application.planning_constraint_field_service import (
    generate_planning_constraint_field,
)
from cns_planner.domain.building_clearance import (
    BUILDING_OCCUPANCY_UNKNOWN, building_footprint_occupancy, building_height_evidence_status,
    building_height_present, building_height_status_is_resolved,
    building_height_value_is_usable, building_height_var_is_usable, building_occupancy_state,
    building_roof_elevation, height_value_is_nodata_sentinel, height_value_is_sentinel,
    height_var_semantics, no_building_vertical_constraint,
)
from cns_planner.domain.layered_route import FEASIBILITY_REASON_CODES
from cns_planner.gis.layered_feasibility_adapter import LayeredFeasibilityAdapter, _building_fact
from cns_planner.gis.v3_environment_adapter import V3RealEnvironmentAdapter
from cns_planner.layered_route_planner.feasibility import build_layer_feasibility_mask

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"
VREF = "egm2008_orthometric"
LAYER_ID = "ALT-100"


# ---------------------------------------------------------------------------- fixtures


def one_cell(grid_id="G0"):
    return [{
        "grid_id": grid_id, "level": 8, "column": 0, "row": 0,
        "bbox": [122.0, 30.0, 122.01, 30.01], "center": [122.005, 30.005],
    }]


def building_fact(*, count=0, area=None, ratio=None, height=None, fraction=None,
                  status="passed"):
    fact = {"data_status": status, "building_count": count}
    if area is not None:
        fact["building_area_m2"] = area
    if ratio is not None:
        fact["building_coverage_ratio"] = ratio
    if height is not None:
        fact["height_max_m"] = height
    if fraction is not None:
        fact["valid_height_fraction"] = fraction
    return fact


def coarse_mask(building, *, elevation=10.0, altitude=100.0):
    return build_layer_feasibility_mask(
        request={"scenario_route_id": "R-1", "altitude_layer_id": LAYER_ID},
        cruise_altitude={
            "status": "confirmed", "altitude_layer_id": LAYER_ID,
            "altitude_egm2008_m": altitude, "vertical_reference": VREF,
        },
        cells=[{
            "grid_id": "G0",
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": elevation,
                "vertical_reference": VREF,
            },
            "buildings": building,
        }],
        feasibility_policy={
            "terrain_vertical_clearance_m": 10.0, "source": "engineering",
            "confirmed": True, "status": "confirmed",
        },
        building_clearance_policy={
            "vertical_clearance_m": 10.0, "horizontal_clearance_m": 0.0,
            "source": "engineering", "confirmed": True, "status": "confirmed",
        },
        grid_level=8,
    )


def confirmed_restricted_collection(items=()):
    present = {
        str(item.get("domain") or "critical_site") for item in items if isinstance(item, dict)
    }
    return {
        "source": "audited-fixture", "evidence": [{"record": "F1"}],
        "domain_states": {
            domain: "confirmed_present" if domain in present else "confirmed_none"
            for domain in ("airspace", "critical_site")
        },
        "items": list(items),
    }


def constraint_field(building, *, terrain=10.0, altitude=100.0):
    return generate_planning_constraint_field(
        altitude_layer={
            "altitude_layer_id": LAYER_ID, "name": "100m", "nominal_altitude_m": altitude,
            "vertical_reference": VREF, "source": "engineering", "confirmed": True,
            "status": "confirmed",
        },
        grid={"level": 8, "crs": "OGC:CRS84", "cells": one_cell()},
        terrain_by_cell={"G0": {
            "data_status": "passed", "surface_elevation_max_egm2008_m": terrain,
            "vertical_reference": VREF,
        }},
        buildings_by_cell={"G0": building},
        tower_obstacle_profiles={"status": "passed", "items": {}},
        restricted_areas=confirmed_restricted_collection(),
        policies={
            "terrain_vertical_clearance_m": 10.0, "building_vertical_clearance_m": 10.0,
            "tower_vertical_clearance_m": 10.0,
        },
        source_fingerprints={"fixture": "f1"},
        workspace_identity={"id": "W"},
    )


class _Terrain:
    vertical_reference = VREF
    vertical_status = "confirmed"

    @staticmethod
    def sample_cells(inputs, transform=None):
        return {
            item["fine_cell_id"]: {
                "data_status": "passed", "surface_elevation_max_egm2008_m": 10.0,
                "vertical_reference": VREF, "vertical_reference_source": "fixture",
            }
            for item in inputs
        }

    @staticmethod
    def describe():
        return {"fixture": "terrain"}

    @staticmethod
    def usable():
        return True, None


# ------------------------------------------------------- 1) occupancy semantics (domain)


def test_zero_count_with_confirmed_zero_coverage_is_absent():
    facts = {"building_count": 0, "building_area_m2": 0.0, "building_coverage_ratio": 0.0}
    assert building_occupancy_state(**facts) == "absent"
    assert no_building_vertical_constraint(**facts) is True
    assert building_footprint_occupancy(**facts) == "absent"
    # Missing coverage evidence stays "unknown": it must never be *reported* as a
    # confirmed absence of buildings.
    assert building_occupancy_state(building_count=0) == BUILDING_OCCUPANCY_UNKNOWN
    assert no_building_vertical_constraint(building_count=0) is False


def test_zero_count_with_footprint_coverage_is_present_not_absent():
    for facts in (
        {"building_count": 0, "building_area_m2": 1234.5},
        {"building_count": 0, "building_coverage_ratio": 0.35},
        {"building_count": 0, "building_area_m2": 1234.5, "building_coverage_ratio": 0.35},
    ):
        assert building_occupancy_state(**facts) == "present"
        assert no_building_vertical_constraint(**facts) is False


def test_centroid_in_neighbour_cell_is_present_in_this_cell():
    neighbour = building_fact(count=1, area=2000.0, ratio=0.5, height=80.0, fraction=1.0)
    this_cell = building_fact(count=0, area=900.0, ratio=0.2)
    assert building_occupancy_state(
        building_count=neighbour["building_count"],
        building_area_m2=neighbour["building_area_m2"],
        building_coverage_ratio=neighbour["building_coverage_ratio"],
    ) == "present"
    assert building_occupancy_state(
        building_count=this_cell["building_count"],
        building_area_m2=this_cell["building_area_m2"],
        building_coverage_ratio=this_cell["building_coverage_ratio"],
    ) == "present"


# ------------------------------------------------- 2) coarse feasibility mask contract


def test_coarse_mask_clears_only_a_confirmed_zero_footprint_cell():
    cleared = coarse_mask(building_fact(count=0, area=0.0, ratio=0.0))["cells"]["G0"]
    assert cleared["status"] == "feasible"
    assert cleared["reason_code"] is None

    # Same cell, but the L8 fact table reports real footprint coverage with no
    # centroid height: this must NOT be feasible.
    hazard = coarse_mask(building_fact(count=0, area=2500.0, ratio=0.4))["cells"]["G0"]
    assert hazard["status"] == "unknown"
    assert hazard["reason_code"] == "building_footprint_present_height_unresolved"
    assert hazard["reason_code"] in FEASIBILITY_REASON_CODES
    assert hazard["building_required_clearance_egm2008_m"] is None

    # Not blocked either: the unresolved building may be far below this layer.
    assert hazard["status"] != "blocked"


def test_coarse_mask_reports_occupancy_provenance():
    cell = coarse_mask(building_fact(count=0, area=2500.0, ratio=0.4))["cells"]["G0"]
    provenance = cell.get("provenance") or {}
    assert provenance.get("building_occupancy") == "present"
    assert provenance.get("building_occupancy_evidence") == "footprint_coverage_measured"
    assert provenance.get("building_area_m2") == pytest.approx(2500.0)
    assert provenance.get("building_coverage_ratio") == pytest.approx(0.4)


def test_coarse_mask_keeps_legacy_behaviour_when_coverage_evidence_is_absent():
    # Synthetic / legacy facts carry no coverage fields; the historical "count == 0 ⇒
    # no vertical building constraint" path must keep working, but the default must be
    # recorded explicitly instead of being presented as measured evidence.
    cell = coarse_mask(building_fact(count=0))["cells"]["G0"]
    assert cell["status"] == "feasible"
    provenance = cell.get("provenance") or {}
    assert provenance.get("building_occupancy") == BUILDING_OCCUPANCY_UNKNOWN
    assert provenance.get("building_occupancy_evidence") == "not_provided"


def test_coarse_mask_still_clears_a_tall_building_at_this_layer():
    # A real 20 m building under a 100 m layer stays feasible: the fix must not turn
    # "a building exists" into "blocked".
    cell = coarse_mask(
        building_fact(count=3, area=3000.0, ratio=0.6, height=20.0, fraction=1.0)
    )["cells"]["G0"]
    assert cell["status"] == "feasible"
    assert cell["building_required_clearance_egm2008_m"] == pytest.approx(40.0)


# ------------------------------------------------------- 3) production PCF (authority)


def test_constraint_field_does_not_pass_a_footprint_only_cell():
    field = constraint_field(building_fact(count=0, area=2500.0, ratio=0.4))
    cell = field["cells"][0]
    assert cell["outcome"] == "unknown"
    reasons = {item.get("reason") for item in cell["unknown_reasons"]}
    assert "building_footprint_present_height_unresolved" in reasons
    assert cell["outcome"] != "pass"


def test_constraint_field_still_passes_a_confirmed_zero_footprint_cell():
    field = constraint_field(building_fact(count=0, area=0.0, ratio=0.0))
    assert field["cells"][0]["outcome"] == "pass"
    assert field["cells"][0]["blocked_by"] == []


# --------------------------------------------------- 4) V3 environment adapter (boundary)


def test_v3_adapter_does_not_claim_confirmed_absence_with_coverage():
    hazard = V3EnvironmentAdapterFact(building_fact(count=0, area=2500.0, ratio=0.4))
    assert hazard["data_status"] == "unknown"
    assert hazard["reason"] == "building_footprint_present_height_unresolved"
    assert hazard.get("semantics") != "confirmed_absence_of_building_constraint"
    assert hazard["building_occupancy"] == "present"

    cleared = V3EnvironmentAdapterFact(building_fact(count=0, area=0.0, ratio=0.0))
    assert cleared["data_status"] == "passed"
    assert cleared["semantics"] == "confirmed_absence_of_building_constraint"


def V3EnvironmentAdapterFact(building):
    adapter = V3RealEnvironmentAdapter(_Terrain())
    # V3-A 读取的是 ``grid_attributes.buildings.cells`` 的**映射产物**，其
    # ``status`` 字段由 ``data/mapping/buildings.py::_cell`` 写入；同时保留
    # ``data_status`` 以匹配粗搜适配器的 canonical fact 形态。
    fact = {"status": building.get("data_status", "passed"), **building}
    built = adapter.build(
        grid={"level": 8, "cells": one_cell()},
        policy={
            "terrain_clearance_m": 10.0, "building_horizontal_clearance_m": 0.0,
            "building_vertical_clearance_m": 10.0,
        },
        state={"grid_attributes": {"buildings": {"cells": {"G0": fact}}}},
    )
    return built["cells"][0]["buildings"]


# ------------------------------------------- 5) one fact, one semantic across consumers


def test_all_three_consumers_agree_on_the_footprint_only_cell():
    building = building_fact(count=0, area=2500.0, ratio=0.4)
    assert coarse_mask(building)["cells"]["G0"]["status"] == "unknown"
    assert constraint_field(building)["cells"][0]["outcome"] == "unknown"
    assert V3EnvironmentAdapterFact(building)["data_status"] == "unknown"
    # ... and the coarse adapter passes the same fact through unchanged.
    passed = _building_fact({"status": "passed", **building})
    assert passed["building_area_m2"] == pytest.approx(2500.0)
    assert passed["building_coverage_ratio"] == pytest.approx(0.4)
    assert building_occupancy_state(
        building_count=passed["building_count"],
        building_area_m2=passed["building_area_m2"],
        building_coverage_ratio=passed["building_coverage_ratio"],
    ) == "present"


# ------------------------------------------- 5b) Theta* search stays fail-closed


def test_theta_star_does_not_traverse_the_unknown_footprint_only_cell():
    """搜索层不得把 F1-A 的 unknown 当成可通行。

    ``theta_star_v2._build_gate`` 只放行 ``feasible``：本测试直接调用该 gate，
    固定"哪一种 mask 状态可穿越"这条语义，避免有人为了让路径跑通而放宽它。
    """
    from cns_planner.layered_route_planner.theta_star_v2 import (
        GridIndexMap, _build_gate,
    )
    from cns_planner.planning.grid_graph import GridGraph

    graph = GridGraph(list(one_cell()))
    index_map = GridIndexMap(graph)
    mask = coarse_mask(building_fact(count=0, area=2500.0, ratio=0.4))
    gate, diagnostics = _build_gate(
        graph=graph, index_map=index_map, mask=mask,
        hard_constraints=None, regulatory=None, altitude=100.0,
    )
    rejection = gate("G0")
    assert rejection is not None, "unknown 格不得通过 gate"
    assert rejection["domain"] == "unknown"
    assert rejection["reason_code"] == "building_footprint_present_height_unresolved"
    assert diagnostics["gate_reasons"]["G0"] == "building_footprint_present_height_unresolved"

    cleared_mask = coarse_mask(building_fact(count=0, area=0.0, ratio=0.0))
    cleared_gate, _ = _build_gate(
        graph=graph, index_map=index_map, mask=cleared_mask,
        hard_constraints=None, regulatory=None, altitude=100.0,
    )
    assert cleared_gate("G0") is None, "确认无覆盖的格仍必须可通行"


# --------------------------------------------- 6) height value vs evidence (F1-C)

def test_official_sentinels_are_never_usable_algebra():
    assert height_value_is_nodata_sentinel(-999) is True
    assert height_value_is_nodata_sentinel(-1) is False
    assert height_value_is_sentinel(-1.0) is True
    assert building_height_value_is_usable(-999) is False
    assert building_height_value_is_usable(-1.0) is False
    assert building_height_value_is_usable(0.0) is False
    assert building_height_value_is_usable(None) is False
    assert building_height_value_is_usable(0.254) is True


def test_height_var_minus_one_is_unavailable_never_zero():
    assert building_height_var_is_usable(-1.0) is False
    assert building_height_var_is_usable(-999.0) is False
    assert building_height_var_is_usable(None) is False
    assert building_height_var_is_usable(0.0) is True
    assert building_height_var_is_usable(2.5) is True
    assert height_var_semantics(-1.0) == "unavailable_sentinel"
    assert height_var_semantics(-999.0) == "nodata_sentinel"
    assert height_var_semantics(None) == "missing"
    assert height_var_semantics(2.5) == "variance_m2_usable"


def test_roof_formula_rejects_sentinels_and_keeps_the_raw_value():
    for value in (-999, -1.0, 0.0):
        roof = building_roof_elevation(10.0, value)
        assert roof["status"] == "unresolved"
        assert roof["roof_elevation_egm2008_m"] is None
        assert roof["height_m"] == value  # 原值保留用于审计
    assert building_roof_elevation(10.0, -999)["reason"] == "building_height_nodata_sentinel"
    assert building_roof_elevation(10.0, 20.0)["roof_elevation_egm2008_m"] == pytest.approx(30.0)


def test_height_present_is_not_height_trusted():
    # 全部真实 GBA 高度都只有 "present" 级证据：本轮**不**声称任何建筑高度可信。
    assert building_height_present(0.0055) is True
    assert building_height_present(-1.0) is False
    assert building_height_evidence_status(0.0055) == "present_unverified"
    assert building_height_evidence_status(159.5) == "present_unverified"
    assert building_height_evidence_status(159.5, "osm_tag") == "trusted"
    assert building_height_evidence_status(-999) == "unusable_sentinel"
    assert building_height_evidence_status(None) == "unknown"


def test_height_status_resolved_keeps_its_historical_meaning():
    # 兼容性：本轮不得因为"证据不足"而改变既有 RESOLVED 白名单的判定，
    # 否则 538,203 栋建筑会一次性全部变成 unknown（全域阻断）。
    assert building_height_status_is_resolved("valid") is True
    assert building_height_status_is_resolved("predicted") is True
    assert building_height_status_is_resolved("unknown") is False
    assert building_height_status_is_resolved(None) is False


# ------------------------------------- 7) continuous validators are fail-closed too


def test_continuous_validator_is_fail_closed_on_unresolved_building_height():
    """连续净空验证不得因为"高度有值"就放行 —— 状态未解析必须 unresolved。

    这条链用真实 footprint（不是 L8 格统计），因此它与粗搜的 ``unknown`` 是**不同层级**
    的证据：粗搜说"这一格不确定"，连续验证说"这栋建筑的楼顶算不出来"。
    既有语义已由 ``tests/test_phase4_b3x_planning_constraints.py`` 的连续验证用例覆盖，
    本轮只在这里补一条最小契约断言，确认 F1-A 没有把它改成 fail-open。
    """
    from cns_planner.domain.building_clearance import building_roof_elevation

    unresolved = building_roof_elevation(10.0, None)
    assert unresolved["status"] == "unresolved"
    assert unresolved["roof_elevation_egm2008_m"] is None
    # 哨兵同样不得生产 roof（F1-C），因此连续验证不可能据此判 pass。
    assert building_roof_elevation(10.0, -999)["status"] == "unresolved"
    assert building_roof_elevation(10.0, -1.0)["status"] == "unresolved"
