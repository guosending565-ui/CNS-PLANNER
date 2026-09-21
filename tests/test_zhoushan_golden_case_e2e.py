"""真实舟山案例的 golden case 端到端测试（Phase 3.5 稳定化）。

**案例（真实 O.D. 与真实来源数值）**

* 起点：桃花岛无人机起降点 ``RLS-FA13218F1A7F`` ``[122.2841666667, 29.8430555556]``
* 终点：函景湾无人机起降场 ``RLS-30E29238CD3D`` ``[122.2763888889, 29.9452777778]``
* 高度层：``ALT-ZS-300-EGM2008``（300 m EGM2008 orthometric，工程确认）
* 建筑：真实 GBA footprint 的**实测高度**（舟山走廊实测 max 27.912586212158203 m，来自
  ``zhoushan_buildings.gpkg``）

**闭环**：workspace → fixed altitude layer → Layered Risk-Aware Theta* V2 → RouteRiskProfile
→ LayeredRouteValidation → LayeredOperationalAdoption → save/reload。

**证据边界（必须诚实）**：本测试不打开 QGIS/GDAL，因此 FABDEM 与建筑 footprint 证据是
**注入的合成证据**（与真实数据同 schema、同 CRS 语义、同量级数值）。它验证的是**工程管道、
状态语义、失效链与持久化**，不是真实栅格读取——真实数据路径由
``tools/building_geometry_quality_report.py`` 与 QGIS 集成测试负责。
"""

from __future__ import annotations

import math
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.spatial_3d import normalize_spatial_3d

DEFAULTS = Path(__file__).parents[1] / "cns_planner" / "config" / "defaults.json"

#: 真实建筑 GPKG 路径（可选，通过环境变量提供；缺失时相关测试自动 skip）。
REAL_BUILDINGS_ENV = "CNS_ZHOUSHAN_BUILDINGS_GPKG"
REAL_METRIC_CRS = "EPSG:32651"

# ---------------------------------------------------------------- real case facts
START = [122.2841666667, 29.8430555556]          # RLS-FA13218F1A7F 桃花岛
END = [122.2763888889, 29.9452777778]            # RLS-30E29238CD3D 函景湾
START_SITE_ID = "RLS-FA13218F1A7F"
END_SITE_ID = "RLS-30E29238CD3D"
WORKSPACE = [122.268, 29.835, 122.300, 29.955]   # 真实舟山走廊工作区
CRUISE_ALTITUDE_M = 300.0
ALTITUDE_LAYER_ID = "ALT-ZS-300-EGM2008"
#: 舟山走廊真实 GBA footprint 的实测最大高度（只读探测 `zhoushan_buildings.gpkg` 得到）。
REAL_BUILDING_HEIGHT_MAX_M = 27.912586212158203
#: 真实建筑 footprint 在米制 CRS 下的外环（10 个顶点，来自真实 GPKG 的实测形状）。
REAL_FOOTPRINT_VERTEX_COUNT = 10


def _metric_origin():
    """等距圆柱投影的参考点：真实工作区中心（30.05°N 处的地面尺度）。"""

    return (WORKSPACE[0] + WORKSPACE[2]) / 2.0, (WORKSPACE[1] + WORKSPACE[3]) / 2.0


def to_metric(point):
    """lon/lat → 局部米制（合成 metric frame；1° lon ≈ 96.5 km，1° lat ≈ 110.9 km）。"""

    lon0, lat0 = _metric_origin()
    x = (float(point[0]) - lon0) * 111320.0 * math.cos(math.radians(lat0))
    y = (float(point[1]) - lat0) * 110540.0
    return [x, y]


def to_geographic(point):
    """局部米制 → lon/lat（``to_metric`` 的逆）。"""

    lon0, lat0 = _metric_origin()
    lon = lon0 + float(point[0]) / (111320.0 * math.cos(math.radians(lat0)))
    lat = lat0 + float(point[1]) / 110540.0
    return [lon, lat]


# ---------------------------------------------------------------- workspace


def real_workspace_grid(max_cells=5000):
    """真实工作区 bbox 上的 MH/T 标准网格（显式请求 L8，与真实案例一致）。"""

    return WorkspaceGridService(preferred_level=8, max_cells=max_cells).generate(
        WORKSPACE, preferred_level=8,
    )


def golden_workflow(tmp_path):
    """workspace → grid → scenario route → altitude layer（真实 O.D. 与真实高度层）。"""

    grid_result = real_workspace_grid()
    assert grid_result["status"] == "passed"
    assert grid_result["level"] == 8 and grid_result["coarsened"] is False
    cells = grid_result["cells"]

    service = WorkflowService(tmp_path / "golden_project.json", DEFAULTS)
    state = service.state
    state["grid"] = {
        "status": "passed", "level": 8, "count": len(cells),
        "workspace_bbox": list(WORKSPACE), "coarsened": False, "cells": cells,
    }
    service.state["scenario_routes"] = [{
        "route_id": "SCN-ZS-TAOHUADAO-HANJINGWAN",
        "start": list(START), "end": list(END),
        "start_node_id": START_SITE_ID, "end_node_id": END_SITE_ID,
        "reference_site_ids": [START_SITE_ID, END_SITE_ID],
        "provenance": {
            "source": "real_zhoushan_landing_sites",
            "start_reference_site_id": START_SITE_ID, "end_reference_site_id": END_SITE_ID,
        },
    }]
    state["spatial_3d"] = normalize_spatial_3d({"altitude_layers": [{
        "altitude_layer_id": ALTITUDE_LAYER_ID, "name": "舟山 300 m 低层",
        "nominal_altitude_m": CRUISE_ALTITUDE_M,
        "lower_altitude_m": 250.0, "upper_altitude_m": 350.0,
        "vertical_reference": "egm2008_orthometric",
        "source": "工程确认-真实案例", "evidence": {"case": "zhoushan-golden"},
        "confirmed": True, "status": "confirmed",
    }]})
    state["grid_attributes"]["population"] = {
        "status": "passed", "algorithm_id": "population-grid-raw-statistics",
        "unit_status": "verified_from_raster_metadata",
        "cells": {
            cell["grid_id"]: {
                "status": "passed", "population_density_people_km2": 120.0,
                "source_coverage_fraction": 1.0, "coverage_status": "covered",
            } for cell in cells
        },
    }
    state["grid_risk_v2"] = _risk_v2(cells)
    state["building_clearance_policy"].update({
        "status": "confirmed", "horizontal_clearance_m": 0.0,
        "vertical_clearance_m": 10.0, "source": "工程确认-真实案例", "confirmed": True,
    })
    state["source_audits"] = {
        "status": "passed", "count": 3, "items": {
            "terrain_dtm": {"role": "terrain_dtm", "status": "verified",
                            "file_name": "FABDEM_zhoushan.tif",
                            "verification": {"sha256": "golden-terrain-sha"}},
            "buildings": {"role": "buildings", "status": "verified",
                          "file_name": "zhoushan_buildings.gpkg",
                          "verification": {"sha256": "golden-buildings-sha"}},
            "building_grid": {"role": "building_grid", "status": "verified",
                              "file_name": "zhoushan_building_grid_L8.gpkg",
                              "verification": {"sha256": "golden-building-grid-sha"}},
        },
    }
    service.set_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": 50.0, "source": "工程确认-真实案例", "confirmed": True,
    })
    service.set_layered_route_planning_request({
        "scenario_route_id": "SCN-ZS-TAOHUADAO-HANJINGWAN",
        "altitude_layer_id": ALTITUDE_LAYER_ID,
        "source": "工程确认-真实案例", "confirmed": True,
    })
    return service, cells


def _risk_v2(cells, index=0.5):
    return {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains",
        "algorithm_version": "2.0", "input_fingerprint": f"riskv2-golden-{index}",
        "policy_fingerprint": "riskv2-golden-policy",
        "references": {"population_exposure": {"mode": "log1p_quantile", "value": 120.0}},
        "factor_status": {"population_exposure": {
            "status": "passed", "source_id": "worldpop-r2025a-population-count",
            "source_fingerprint": "popsrc", "normalization": {"method": "log1p_quantile"},
        }},
        "cells": {
            cell["grid_id"]: {
                "grid_id": cell["grid_id"], "status": "passed",
                "factors": {"population_exposure": {
                    "factor_id": "population_exposure", "status": "passed",
                    "normalized_index": index,
                }},
                "domains": {
                    domain_id: {"domain_id": domain_id, "status": "passed", "index": index}
                    for domain_id in ("ground", "air_traffic", "environment_obstacle")
                },
            }
            for cell in cells
        },
    }


# ---------------------------------------------------------------- coarse facts (L8)


def coarse_facts(cells, *, building_cells=()):
    """L8 战略垂向包线的事实（真实地形量级 + 真实建筑高度）。"""

    buildings = set(building_cells)
    facts = []
    for cell in cells:
        bbox = cell.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        # 真实走廊的地形量级：南向北在 20~60 m 之间线性抬升（EGM2008 正高）。
        elevation = 20.0 + (float(bbox[1]) - WORKSPACE[1]) / (
            WORKSPACE[3] - WORKSPACE[1]
        ) * 40.0
        facts.append({
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed",
                "surface_elevation_max_egm2008_m": round(elevation, 6),
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": (
                {
                    "data_status": "passed", "building_count": 1,
                    "height_max_m": REAL_BUILDING_HEIGHT_MAX_M,
                    "valid_height_fraction": 1.0,
                } if cell["grid_id"] in buildings else {
                    "data_status": "passed", "building_count": 0,
                    "height_max_m": None, "valid_height_fraction": None,
                }
            ),
        })
    return facts


class GoldenFeasibilityAdapter:
    """``active_adapter.build_cells(grid_cells, state)`` 的真实案例替身。"""

    def __init__(self, facts):
        self._facts = facts
        self.calls = 0

    def build_cells(self, grid_cells, state):
        self.calls += 1
        wanted = {cell["grid_id"] for cell in grid_cells}
        return [deepcopy(item) for item in self._facts if item["grid_id"] in wanted]

    def describe(self):
        return {
            "adapter_id": "golden_zhoushan_coarse_facts",
            "source_type": "configured_real_sources",
            "terrain_dtm": {"role": "terrain_dtm", "file_name": "FABDEM_zhoushan.tif"},
            "buildings": {"role": "buildings", "file_name": "zhoushan_buildings.gpkg"},
        }

    def usable(self):
        return True, None

    def source_status(self):
        return {"terrain": {"available": True, "reason": None}}


# ---------------------------------------------------------------- validation evidence


def real_shaped_footprint(candidate, *, height_m=REAL_BUILDING_HEIGHT_MAX_M, ground_m=20.0,
                          index=0):
    """在候选航路的中点附近放一个**真实形状**的 footprint（10 顶点，非矩形）。"""

    path = candidate["path"]
    middle = path[len(path) // 2]
    center = to_metric(middle)
    half = 60.0
    ring = [
        [center[0] - half, center[1] - half * 0.4],
        [center[0] - half * 0.2, center[1] - half],
        [center[0] + half * 0.6, center[1] - half * 0.8],
        [center[0] + half, center[1] - half * 0.2],
        [center[0] + half * 0.8, center[1] + half * 0.7],
        [center[0] + half * 0.1, center[1] + half],
        [center[0] - half * 0.7, center[1] + half * 0.6],
        [center[0] - half, center[1] + half * 0.2],
        [center[0] - half * 0.6, center[1] - half * 0.1],
        [center[0] - half, center[1] - half * 0.4],
    ]
    assert len(ring) == REAL_FOOTPRINT_VERTEX_COUNT
    return {
        "building_id": f"golden-footprint-{index}",
        "source": "GBA",
        "ring_metric": ring,
        "height_m": height_m,
        "height_status": "predicted",
        "ground_elevation_max_egm2008_m": ground_m,
        # 与生产 GIS 边界返回的 footprint 同形状：真实形状的 ring + 质量标注。
        "geometry_status": "passed",
        "geometry_quality": {
            "status": "passed", "repair_applied": False, "source_modified": False,
        },
    }


def evidence_adapter(candidate, *, terrain_m=40.0, buildings=None, nodata=False):
    """（注入的）源生证据：native 地形窗口 + 真实建筑 footprint。"""

    metric_path = [to_metric(point) for point in candidate["path"]]
    total = 0.0
    distances = [0.0]
    for start, end in zip(metric_path, metric_path[1:]):
        total += math.dist(start, end)
        distances.append(total)

    def build(**_kwargs):
        pixels = []
        for index in range(len(metric_path) - 1):
            pixels.append({
                "pixel": [index, 0],
                "interval": {
                    "start_distance_m": distances[index],
                    "end_distance_m": distances[index + 1],
                },
                "data_status": "unknown" if nodata else "passed",
                "elevation_egm2008_m": None if nodata else terrain_m,
                "source_value": None if nodata else terrain_m,
                "reason": "native_terrain_pixel_nodata" if nodata else None,
            })
        items = deepcopy(buildings or [])
        return {
            "adapter_id": "golden_zhoushan_validation_evidence",
            "source_type": "configured_real_sources",
            "metric_path": metric_path, "metric_crs": "EPSG:32651",
            "to_geographic": to_geographic,
            "terrain": {
                "available": True,
                "source": {"crs": "EPSG:32651", "id": "fabdem-golden"},
                "pixels": pixels,
            },
            "buildings": {
                "available": True, "source": {"id": "gba-golden"},
                "buildings": items,
            },
            "sources": {
                "terrain_dtm": {"id": "terrain-golden"},
                "buildings": {"id": "buildings-golden"},
                "metric_frame": {"horizontal_crs": "EPSG:32651"},
            },
            "sample_count": len(pixels) + len(items),
        }
    return build


# ---------------------------------------------------------------- pipeline steps


def plan_candidate(service, cells, *, building_cells=()):
    adapter = GoldenFeasibilityAdapter(coarse_facts(cells, building_cells=building_cells))
    service.layered_route_planner_service.adapter = adapter
    service.evaluate_layered_route_candidate({}, adapter=adapter)
    candidates = service.layered_route_planner_service.result_snapshot()
    current = next(
        (item for item in candidates["items"] if item.get("current_applicability") == "current"),
        candidates["items"][-1] if candidates["items"] else None,
    )
    return candidates, current, adapter


def latest_validation(service):
    items = service.layered_route_validations()["items"]
    return items[-1] if items else None


# ---------------------------------------------------------------- the golden case


def test_golden_case_closes_the_loop_and_publishes_a_real_od_route(tmp_path):
    """桃花岛 → 函景湾：完整闭环，建筑几何验证必须给出明确 verdict。"""

    service, cells = golden_workflow(tmp_path)
    assert len(cells) >= 24, "真实工作区在 L8 下应当有足够的格子"

    # ---- 1. Layered Risk-Aware Theta* V2 规划 -------------------------------------
    midpoint_cell = cells[len(cells) // 2]["grid_id"]
    candidates, candidate, adapter = plan_candidate(
        service, cells, building_cells=(midpoint_cell,),
    )
    assert adapter.calls == 1
    assert candidate["status"] == "candidate", candidate.get("blocking_reasons")
    assert candidate["algorithm_id"] == "layered_risk_aware_theta_star_v2"
    assert candidate["operational_route"] is False
    assert candidate["continuous_validation_required"] is True
    assert candidate["terminal_status"] == "candidate"
    assert candidate["distance_m"] == pytest.approx(11391.3, rel=0.3)
    assert candidate["search_statistics"]["search_completeness"] in (
        "optimal_path_found", "expansion_cap_reached_optimality_not_proven",
    )

    # ---- 2. RouteRiskProfile -------------------------------------------------------
    profiles = service.evaluate_route_risk_profile({})["route_risk_profiles"]
    profile = profiles["items"][-1]
    assert profile["status"] == "passed", profile.get("blocking_reasons")
    assert profile["candidate"]["candidate_fingerprint"] == candidate["candidate_fingerprint"]
    assert profile["route_length_m"] == pytest.approx(candidate["distance_m"], abs=1e-6)

    # ---- 3. LayeredRouteValidation（源生几何连续验证） ------------------------------
    footprint = real_shaped_footprint(candidate)
    service.evaluate_layered_route_validation(
        {}, evidence_adapter=evidence_adapter(candidate, buildings=[footprint]),
    )
    validation = latest_validation(service)
    assert validation["status"] == "validated_candidate", (
        validation["status_reason"], validation["domains"]["building"].get("reason"),
    )
    assert validation["domains"]["terrain"]["status"] == "passed"
    # **本地稳定化的关键断言**：真实形状的 footprint 必须被真正评估（不再因为几何提取
    # 失败而 building=unresolved）。
    building = validation["domains"]["building"]
    assert building["status"] == "passed"
    assert building["evidence"]["building_count"] == 1
    assert building["evidence"]["building_quality_report"]["counts"] == {
        "passed": 1, "repaired": 0, "invalid": 0,
    }
    assert building["evidence"]["source_geometry_modified"] is False
    assert building["minimum_margin"] > 0.0

    # ---- 4. Operational Adoption（显式人工发布） ----------------------------------
    readiness = service.layered_operational_adoption_readiness()
    assert readiness["status"] in ("ready", "blocked")
    preview = service.preview_layered_operational_adoption({
        "validation_id": validation["validation_id"],
    })
    assert preview["status"] == "ready", preview.get("blocking_reasons")
    assert preview["side_effects"] is False

    applied = service.apply_layered_operational_adoption({
        "validation_id": validation["validation_id"], "confirmed": True,
        "expected_validation_fingerprint": validation["fingerprints"]["validation_fingerprint"],
    })
    assert applied["status"] == "passed"
    adoption = next(
        item for item in service.state["layered_operational_adoptions"]["items"]
        if item["adoption_id"] == applied["adoption_id"]
    )
    assert adoption["status"] == "published"
    assert adoption["current_applicability"] == "current"
    published = next(
        item for item in service.state["operational_routes"]
        if item["route_id"] == "SCN-ZS-TAOHUADAO-HANJINGWAN"
    )
    assert all(len(point) == 2 for point in published["path"])
    assert published["provenance"]["source_type"] == "layered_candidate_operational_adoption_v1"
    assignment = service.state["spatial_3d"]["route_operating_layers"][0]
    assert assignment["altitude_layer_id"] == ALTITUDE_LAYER_ID
    assert assignment["operating_mode"] == "fixed_cruise_layer"

    # ---- 5. save / reload ----------------------------------------------------------
    project_path = service.session.store_path
    session_before = deepcopy(service.state)
    reloaded = WorkflowService(project_path, DEFAULTS)
    assert reloaded.state["operational_routes"] == session_before["operational_routes"]
    assert reloaded.state["layered_route_candidates"]["items"] == (
        session_before["layered_route_candidates"]["items"]
    )
    assert reloaded.state["spatial_3d"]["route_operating_layers"] == (
        session_before["spatial_3d"]["route_operating_layers"]
    )
    reloaded_grid = reloaded.state["grid"]
    assert reloaded_grid["level"] == 8 and reloaded_grid["count"] == len(cells)
    reloaded_candidate = reloaded.layered_route_planner_service.result_snapshot()["items"][-1]
    assert reloaded_candidate["candidate_fingerprint"] == candidate["candidate_fingerprint"]
    assert reloaded_candidate["status"] == "candidate"
    reloaded_validation = reloaded.layered_route_validations()["items"][-1]
    assert reloaded_validation["validation_id"] == validation["validation_id"]
    assert reloaded_validation["status"] == "validated_candidate"
    reloaded_adoption = reloaded.layered_operational_adoptions()["items"][-1]
    assert reloaded_adoption["status"] == "published"
    assert reloaded_adoption["adoption_id"] == applied["adoption_id"]


def test_golden_case_building_geometry_failure_blocks_publication(tmp_path):
    """建筑几何无法修复时：validation=unresolved，adoption 必须拒绝发布。"""

    service, cells = golden_workflow(tmp_path)
    _, candidate, _ = plan_candidate(service, cells)
    assert candidate["status"] == "candidate"
    service.evaluate_route_risk_profile({})

    broken = real_shaped_footprint(candidate)
    # 共线（零面积）环：没有任何可修复的几何，必须保持 unknown。
    broken["ring_metric"] = [[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [0.0, 0.0]]
    service.evaluate_layered_route_validation(
        {}, evidence_adapter=evidence_adapter(candidate, buildings=[broken]),
    )
    validation = latest_validation(service)
    assert validation["status"] == "unresolved"
    assert validation["domains"]["building"]["status"] == "unresolved"
    quality = validation["domains"]["building"]["evidence"]["building_quality_report"]
    assert quality["counts"] == {"passed": 0, "repaired": 0, "invalid": 1}
    assert quality["repair"]["failed_count"] == 1

    preview = service.preview_layered_operational_adoption({
        "validation_id": validation["validation_id"],
    })
    assert preview["status"] != "ready"
    assert preview["publication_allowed"] is False
    assert service.state["operational_routes"] == []


def test_golden_case_terrain_nodata_stays_unresolved_and_never_publishes(tmp_path):
    service, cells = golden_workflow(tmp_path)
    _, candidate, _ = plan_candidate(service, cells)
    service.evaluate_route_risk_profile({})
    service.evaluate_layered_route_validation(
        {}, evidence_adapter=evidence_adapter(candidate, nodata=True),
    )
    validation = latest_validation(service)
    assert validation["status"] == "unresolved"
    assert validation["domains"]["terrain"]["status"] == "unresolved"
    assert service.preview_layered_operational_adoption({
        "validation_id": validation["validation_id"],
    })["publication_allowed"] is False


def test_golden_case_search_budget_exhaustion_is_not_infeasibility(tmp_path):
    """真实走廊上搜索预算不足 ⇒ ``search_incomplete``，绝不是"空域不可行"。"""

    service, cells = golden_workflow(tmp_path)
    service.select_algorithm({
        "algorithm_type": "layered_route_planner",
        "algorithm_id": "layered_risk_aware_theta_star_v2", "version": "2.0",
        "parameters": {"max_expanded_labels": 5, "heading_bin_count": 8, "theta_min_deg": 5.0},
    })
    adapter = GoldenFeasibilityAdapter(coarse_facts(cells))
    service.layered_route_planner_service.adapter = adapter
    service.evaluate_layered_route_candidate({}, adapter=adapter)
    candidate = next(
        item for item in service.layered_route_planner_service.result_snapshot()["items"]
        if item.get("current_applicability") == "current"
        for _ in [0]
    )
    assert candidate["status"] == "search_incomplete"
    assert candidate["search_incomplete"] is True
    assert candidate["blocking_reasons"][0]["reason_code"] == "search_budget_exhausted"
    assert candidate["blocking_reasons"][0]["reachability_proven"] is False
    # 没有候选 ⇒ 后续 profile / validation / adoption 全部无法越过前置门。
    readiness = service.layered_route_validation_readiness()
    assert readiness["status"] != "ready"


def test_golden_case_grid_is_l8_and_building_facts_are_level_bound(tmp_path):
    """真实工作区显式请求 L8；能力声明同时说明层级绑定与已知落差。"""

    service, cells = golden_workflow(tmp_path)
    readiness = service.layered_route_planner_readiness()
    capabilities = readiness["capabilities"]
    planner_capability = capabilities["planner"]
    building_capability = capabilities["building_grid"]
    assert planner_capability["available_levels"] == [8]
    assert planner_capability["preferred_level"] == 8
    assert planner_capability["level_binding"] == "current_workspace_grid_level_used_as_is"
    assert building_capability["available_levels"] == [8]
    assert building_capability["preferred_level"] == 8
    assert building_capability["source_declared_level"] == 8
    assert building_capability["declared_level_usable"] is True
    assert capabilities["workspace_grid"]["available_levels"] == list(range(1, 17))
    # 真实工作区确实停在 L8（未 coarsen）。
    assert service.state["grid"]["level"] == 8
    assert service.state["grid"]["coarsened"] is False


def test_golden_case_coarse_grid_at_l6_cannot_use_l8_building_facts(tmp_path):
    """同一真实工作区若被 coarsen 到 L6：建筑事实整表不可用（unknown != 0）。"""

    service, cells = golden_workflow(tmp_path)
    coarse = WorkspaceGridService(preferred_level=7, max_cells=64).generate(
        WORKSPACE, preferred_level=8,
    )
    assert coarse["coarsened"] is True and coarse["level"] < 8
    service.state["grid"] = {
        "status": "passed", "level": coarse["level"], "count": len(coarse["cells"]),
        "workspace_bbox": list(WORKSPACE), "coarsened": True, "cells": coarse["cells"],
    }
    capability = service.layered_route_planner_readiness()["capabilities"]["building_grid"]
    assert capability["declared_level_usable"] is False
    assert capability["unusable_reason"] == "unsupported_grid_level"
    # L8 建筑事实表在 L6 网格上仍然是整表 unsupported，绝不当 0。
    from cns_planner.data.mapping.buildings import BuildingGridService

    mapped = BuildingGridService().map(
        {"level": coarse["level"], "cells": coarse["cells"],
         "workspace_bbox": list(WORKSPACE)},
        None,
    )
    assert mapped["status"] == "unsupported"
    assert all(
        cell["status"] == "missing_data" for cell in mapped["cells"].values()
    )


# ---------------------------------------------------------------- optional real data


def test_layered_planner_readiness_exposes_level_capability_declarations(tmp_path):
    """readiness 必须显式暴露 planner / building_grid / workspace_grid 三份能力声明。"""

    service, _cells = golden_workflow(tmp_path)
    readiness = service.layered_route_planner_readiness()
    capabilities = readiness["capabilities"]
    assert set(capabilities) == {"planner", "building_grid", "workspace_grid"}
    assert capabilities["planner"]["algorithm_id"] in (
        "layered_risk_aware_theta_star_v2", "layered_route_planner_v1",
    )
    assert capabilities["planner"]["future_work"] == (
        "level_independent_building_fact_acquisition"
    )
    assert capabilities["building_grid"]["cross_level_interpolation_allowed"] is False
    assert capabilities["workspace_grid"]["capability_scope"] == (
        "software_baseline_defaults_not_project_grid"
    )
    # 能力声明是只读的：它不改变阻塞状态，也不写入任何结果。
    assert readiness["status"] in ("ready", "blocked")
    assert service.state["layered_route_candidates"]["count"] == 0


def real_buildings_source():
    """真实建筑 GPKG：来自环境变量或已知本地路径；不存在时返回 ``None``。"""

    candidate = os.environ.get(REAL_BUILDINGS_ENV)
    if not candidate:
        candidate = (
            r"D:/aaa2026project/UOM/舟山/规划系统/building/processed/zhoushan_buildings.gpkg"
        )
    path = Path(candidate)
    return path if path.is_file() else None


def test_real_building_gpkg_geometry_quality_report_for_the_golden_corridor():
    """真实 GPKG 的几何质量报告（只读）：真实案例走廊上的 footprint 必须可被解释。

    该测试在真实数据不存在时自动 skip；它不依赖 QGIS（用 pyproj 做与生产同序的
    投影 → 质量检查 → make_valid）。
    """

    source = real_buildings_source()
    if source is None:
        pytest.skip("真实 buildings GeoPackage 未配置（参见 tests/../tools/building_geometry_quality_report.py）")
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from tools.building_geometry_quality_report import build_report

    report = build_report(source, metric_crs=REAL_METRIC_CRS, bbox=WORKSPACE)
    assert report["evaluated_footprint_count"] > 0
    assert report["source"]["read_only"] is True
    assert report["source"]["source_geometry_modified"] is False
    assert report["method"]["order"] == "project_to_metric_crs_then_check_then_make_valid"
    # 真实数据在质量门之后必须完全可解释：修复的会被标记，无法解释的保持 unknown。
    assert report["counts"]["invalid"] == 0, report["invalid"][:3]
    assert (
        report["counts"]["passed"] + report["counts"]["repaired"]
        == report["evaluated_footprint_count"]
    )
    assert report["semantics"]["unrepairable_geometry_stays_unknown"] is True


def test_real_building_footprints_survive_the_metric_quality_gate():
    """真实 footprint 的环经质量门后仍是有效多边形（根因回归：投影后的 MultiPolygon）。"""

    source = real_buildings_source()
    if source is None:
        pytest.skip("真实 buildings GeoPackage 未配置")

    from cns_planner.domain.building_geometry_quality import prepare_footprint_polygons
    from tools.building_geometry_quality_report import iter_geometries

    from pyproj import CRS, Transformer
    from shapely import wkb as shapely_wkb

    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(REAL_METRIC_CRS), always_xy=True,
    )
    checked = 0
    for _fid, identifier, blob in iter_geometries(source, bbox=WORKSPACE, limit=25):
        geometry = shapely_wkb.loads(blob)
        parts = (
            list(geometry.geoms) if geometry.geom_type == "MultiPolygon"
            else [geometry] if geometry.geom_type == "Polygon" else []
        )
        rings = []
        for part in parts:
            rings.append([
                list(transformer.transform(x, y)) for x, y in part.exterior.coords
            ])
        assert rings, identifier
        prepared = prepare_footprint_polygons({
            "building_id": str(identifier), "ring_parts_metric": rings,
        })
        assert prepared["quality"] in ("passed", "repaired")
        assert prepared["polygons"], identifier
        for polygon in prepared["polygons"]:
            assert polygon.is_valid and polygon.area > 0
        checked += 1
    assert checked > 0
