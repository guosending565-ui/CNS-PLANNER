"""Towers Operational Integration V2 测试。

覆盖三件事，且只覆盖这三件事：

1. **CNS 共塔优先**：真实铁塔派生为宿主候选，tier 词典序保证"共塔优先、普通站 fallback"，
   但绝不把铁塔变成 existing facility，也绝不从铁塔推断设备参数；
2. **航路塔净空**：塔高进入 feasibility/clearance（hard constraint），
   地面塔 / 楼面塔高度推导正确，未解析一律 fail-closed，
   并且 **0.8/0.1/0.1 objective 与 Risk Framework V2 数学完全未改动**；
3. **定向失效**：铁塔变化只失效真正消费铁塔的下游，绝不无意义地失效 grid_risk / grid_risk_v2。

已有的 "铁塔只读导入" 行为继续由 ``tests/test_towers_real_data.py`` 锁定。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.application.project_state import normalize_project
from cns_planner.application.site_planning_service import _candidate_actions
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.building_clearance import normalize_building_clearance_policy
from cns_planner.domain.cns_inputs import normalize_candidate_site
from cns_planner.domain.layered_route import (
    FEASIBILITY_REASON_CODES, normalize_layered_route_feasibility_policy,
    normalize_layered_route_request, resolve_cruise_altitude,
)
from cns_planner.domain.site_planning import (
    REUSE_TIERS, TOWER_COLOCATION_REUSE_CLASS,
)
from cns_planner.domain.tower_colocation import (
    build_tower_colocation_candidates, default_tower_colocation_policy,
    normalize_tower_colocation_candidates, normalize_tower_colocation_policy,
)
from cns_planner.domain.tower_obstacle import (
    build_tower_obstacle_profile, build_tower_obstacle_profiles, classify_base_type,
    default_tower_clearance_policy, normalize_tower_clearance_policy,
    normalize_tower_obstacle_profiles,
)
from cns_planner.gis.layered_feasibility_adapter import tower_facts_by_cell
from cns_planner.layered_route_planner.planner import build_layer_feasibility_mask
from cns_planner.domain.layered_theta_v2 import USER_DEFINED_BASELINE_WEIGHTS
from cns_planner.risk.factors_v2 import FACTOR_INPUT_ATTRIBUTES
from cns_planner.site_planner.reuse_first_v1 import ReuseFirstSitePlannerV1

DEFAULTS = Path("cns_planner/config/defaults.json")


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def tower(tower_id="T1", *, longitude=122.005, latitude=30.005, site_type="地面角钢塔",
          height_m=45.0, elevation_m=12.0):
    return {
        "tower_id": tower_id, "name": f"塔{tower_id}", "longitude": longitude,
        "latitude": latitude, "site_type": site_type, "height_m": height_m,
        "elevation_m": elevation_m, "district": "定海区",
        "source": {"type": "file_row", "file_name": "towers.xlsx", "sheet": "Sheet1", "row": 3},
        "evidence": [{"type": "file_row", "sheet": "Sheet1", "row": 3}],
    }


def terrain_fact(elevation=100.0, *, reference="egm2008_orthometric", status="passed"):
    return {
        "status": status, "elevation_m": elevation,
        "vertical_reference": reference, "reason": None,
    }


def building_fact(height=20.0, *, status="passed", source="real_building_footprint_at_tower_coordinate"):
    return {
        "status": status, "building_height_m": height,
        "valid_height_fraction": 1.0, "source": source, "reason": None,
    }


def grid_cells(columns=2, rows=1, level=8, step=0.01):
    return [
        {
            "grid_id": f"MHT4063-L{level}-C{column}-RP{row}", "level": level,
            "bbox": [
                122.0 + step * column, 30.0 + step * row,
                122.0 + step * (column + 1), 30.0 + step * (row + 1),
            ],
        }
        for column in range(columns) for row in range(rows)
    ]


def tower_facts(grid, towers_by_cell):
    """cells facts：terrain=100 m、无建筑、towers 键来自 tower_facts_by_cell 的等价输入。"""

    return [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": 100.0,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": {
                "data_status": "passed", "building_count": 0, "height_max_m": None,
                "valid_height_fraction": None,
            },
            "towers": towers_by_cell.get(cell["grid_id"], {
                "data_status": "no_towers", "tower_count": 0, "resolved_count": 0,
                "unresolved_count": 0, "tower_top_max_egm2008_m": None, "reason": None,
            }),
        }
        for cell in grid
    ]


def mask_for(facts, *, altitude=300.0, tower_clearance=None):
    return build_layer_feasibility_mask(
        request=normalize_layered_route_request({
            "scenario_route_id": "R1", "altitude_layer_id": "L8-LOW",
            "source": "测试", "confirmed": True,
        }),
        cruise_altitude=resolve_cruise_altitude({
            "altitude_layer_id": "L8-LOW", "nominal_altitude_m": altitude,
            "vertical_reference": "egm2008_orthometric", "source": "测试",
            "confirmed": True, "status": "confirmed",
        }),
        cells=facts,
        feasibility_policy=normalize_layered_route_feasibility_policy({
            "terrain_vertical_clearance_m": 50.0, "source": "测试", "confirmed": True,
        }),
        building_clearance_policy=normalize_building_clearance_policy({
            "vertical_clearance_m": 10.0, "horizontal_clearance_m": 0.0,
            "source": "测试", "confirmed": True,
        }),
        source_audits={}, grid_level=8, adapter=None,
        tower_clearance_policy=normalize_tower_clearance_policy(tower_clearance),
    )


def cell_tower_fact(count, resolved, unresolved, top):
    return {
        "data_status": "passed" if not unresolved else "unknown",
        "tower_count": count, "resolved_count": resolved, "unresolved_count": unresolved,
        "tower_top_max_egm2008_m": top, "reason": None if not unresolved else "tower_height_unresolved",
    }


# ======================================================================================
# 1. TowerObstacleProfile：地面 / 楼面 / 垂直基准
# ======================================================================================


def test_ground_tower_top_is_terrain_plus_structure_height():
    profile = build_tower_obstacle_profile(
        tower(site_type="地面角钢塔", height_m=45.0),
        terrain=terrain_fact(100.0), building=None,
    )
    assert profile["base_type"] == "ground"
    assert profile["status"] == "resolved"
    assert profile["tower_top_orthometric_m"] == 145.0
    assert profile["vertical_status"] == "egm2008_orthometric_resolved"
    # 源数据海拔只留档，绝不参与塔顶计算
    assert profile["tower_top_orthometric_m"] != 12.0 + 45.0


def test_rooftop_tower_top_is_terrain_plus_building_plus_structure_height():
    profile = build_tower_obstacle_profile(
        tower(site_type="楼面拉线塔", height_m=15.0),
        terrain=terrain_fact(100.0), building=building_fact(20.0),
    )
    assert profile["base_type"] == "rooftop"
    assert profile["status"] == "resolved"
    assert profile["tower_top_orthometric_m"] == 135.0
    assert profile["building_height_m"] == 20.0
    assert any("楼面塔" in item for item in profile["limitations"])


def test_rooftop_tower_without_building_height_is_unresolved_never_zero():
    profile = build_tower_obstacle_profile(
        tower(site_type="楼面抱杆", height_m=15.0),
        terrain=terrain_fact(100.0), building=None,
    )
    assert profile["base_type"] == "rooftop"
    assert profile["status"] == "unresolved"
    assert profile["vertical_status"] == "building_height_unresolved"
    assert profile["tower_top_orthometric_m"] is None
    assert profile["building_height_m"] is None


def test_partially_covered_building_height_is_unresolved():
    profile = build_tower_obstacle_profile(
        tower(site_type="楼面增高架"),
        terrain=terrain_fact(100.0),
        building={
            "status": "passed", "building_height_m": 20.0, "valid_height_fraction": 0.5,
            "source": "l8_grid", "reason": None,
        },
    )
    assert profile["status"] == "unresolved"
    assert profile["vertical_status"] == "building_height_unresolved"


def test_unresolved_terrain_datum_is_never_treated_as_ground():
    profile = build_tower_obstacle_profile(
        tower(site_type="地面单管塔"),
        terrain=terrain_fact(100.0, reference="unknown", status="passed"),
    )
    assert profile["status"] == "unresolved"
    assert profile["vertical_status"] == "terrain_elevation_unresolved"


def test_missing_structure_height_is_unresolved_never_inferred():
    profile = build_tower_obstacle_profile(
        tower(site_type="地面角钢塔", height_m=None), terrain=terrain_fact(100.0),
    )
    assert profile["status"] == "unresolved"
    assert profile["vertical_status"] == "tower_structure_height_missing"


def test_unknown_site_type_is_fail_closed_and_only_policy_can_override():
    assert classify_base_type("角钢塔") == ("unknown", "site_type_unclassified")
    assert classify_base_type(None) == ("unknown", "site_type_missing")
    assert classify_base_type("楼面拉线塔")[0] == "rooftop"
    assert classify_base_type("地面拉线塔")[0] == "ground"
    profile = build_tower_obstacle_profile(tower(site_type="角钢塔"), terrain=terrain_fact(100.0))
    assert profile["status"] == "unresolved"
    assert profile["vertical_status"] == "base_type_unknown"
    # 显式策略假设是唯一允许的覆盖方式
    overridden = build_tower_obstacle_profile(
        tower(site_type="角钢塔"), terrain=terrain_fact(100.0),
        policy={"default_base_type": "ground", "source": "测试", "confirmed": True},
    )
    assert overridden["base_type"] == "ground"
    assert overridden["status"] == "resolved"


def test_profiles_collection_keeps_per_tower_facts_independent():
    collection = build_tower_obstacle_profiles(
        [tower("T1", longitude=122.001, latitude=30.001, site_type="地面角钢塔", height_m=40.0),
         tower("T2", longitude=122.002, latitude=30.002, site_type="楼面抱杆", height_m=10.0)],
        terrain_by_tower={"T1": terrain_fact(100.0), "T2": terrain_fact(101.0)},
        building_by_tower={},
    )
    assert collection["count"] == 2
    assert collection["resolved_count"] == 1
    assert collection["unresolved_count"] == 1
    assert collection["items"]["T1"]["tower_top_orthometric_m"] == 140.0
    assert collection["items"]["T2"]["status"] == "unresolved"
    assert collection["not_a_population_risk_factor"] is True


def test_tower_clearance_policy_has_no_default_values():
    default = default_tower_clearance_policy()
    assert default["tower_vertical_clearance_m"] is None
    assert default["tower_horizontal_clearance_m"] is None
    assert default["status"] == "pending_confirmation"
    assert normalize_tower_clearance_policy({"tower_vertical_clearance_m": 20.0})["status"] == (
        "not_configured"
    )
    configured = normalize_tower_clearance_policy({
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 60.0,
        "source": "测试", "confirmed": True,
    })
    assert configured["status"] == "confirmed"


def test_obstacle_profiles_persistence_round_trip_keeps_heights():
    collection = build_tower_obstacle_profiles(
        [tower()], terrain_by_tower={"T1": terrain_fact(100.0)},
    )
    reopened = normalize_tower_obstacle_profiles(deepcopy(collection))
    assert reopened["items"]["T1"]["tower_top_orthometric_m"] == 145.0
    assert reopened["resolved_count"] == 1


# ======================================================================================
# 2. TowerColocationCandidate：共塔宿主候选
# ======================================================================================


def confirmed_colocation_policy():
    """规划宿主已确认（**只**确认规划层；物理安装层没有可确认的输入）。"""

    return normalize_tower_colocation_policy({
        "service_origin_assumption": "tower_top_agl_0",
        "planning_host_use_confirmed": True,
        "source": "测试", "confirmed": True,
    })


def test_tower_colocation_candidate_carries_host_provenance():
    collection = build_tower_colocation_candidates(
        [tower()],
        obstacle_profiles=build_tower_obstacle_profiles(
            [tower()], terrain_by_tower={"T1": terrain_fact(100.0)},
        ),
        policy=confirmed_colocation_policy(),
    )
    item = normalize_candidate_site(collection["items"][0])
    assert item["site_id"] == "tower-colocation:T1"
    assert item["coordinate"] == [122.005, 30.005]
    assert item["planning_profile"]["reuse_class"] == TOWER_COLOCATION_REUSE_CLASS
    metadata = item["metadata"]
    assert metadata["candidate_origin"] == "tower_colocation"
    assert metadata["host"]["host_type"] == "tower"
    assert metadata["host"]["host_tower_id"] == "T1"
    assert metadata["host"]["host_site_type"] == "地面角钢塔"
    assert metadata["planning_origin"]["host_tower_id"] == "T1"
    assert metadata["source"]["file_name"] == "towers.xlsx"


def test_tower_colocation_never_invents_device_parameters():
    collection = build_tower_colocation_candidates(
        [tower()],
        obstacle_profiles=build_tower_obstacle_profiles(
            [tower()], terrain_by_tower={"T1": terrain_fact(100.0)},
        ),
        policy=confirmed_colocation_policy(),
    )
    item = normalize_candidate_site(collection["items"][0])
    for field in ("coverage_radius", "transmit_power", "frequency", "antenna_height",
                  "capacity", "mtbf_h", "cost"):
        assert field not in item
    assert item["available_subsystems"] == []
    assert collection["creates_existing_facilities"] is False
    assert collection["invents_device_parameters"] is False


def test_unconfirmed_colocation_policy_keeps_candidates_pending_not_eligible():
    default_items = build_tower_colocation_candidates(
        [tower()],
        obstacle_profiles=build_tower_obstacle_profiles(
            [tower()], terrain_by_tower={"T1": terrain_fact(100.0)},
        ),
        policy=default_tower_colocation_policy(),
    )["items"]
    item = normalize_candidate_site(default_items[0])
    assert item["planning_profile"]["confirmed"] is False
    assert item["planning_profile"]["add_device_allowed"] is None
    assert item["vertical_profile"]["confirmed"] is False
    assert item["vertical_profile"]["service_origin_egm2008_m"] is None


def test_unresolved_tower_top_never_confirms_a_service_origin():
    collection = build_tower_colocation_candidates(
        [tower("T1", site_type="楼面抱杆")],
        obstacle_profiles=build_tower_obstacle_profiles(
            [tower("T1", site_type="楼面抱杆")], terrain_by_tower={"T1": terrain_fact(100.0)},
        ),
        policy=confirmed_colocation_policy(),
    )
    item = normalize_candidate_site(collection["items"][0])
    assert item["vertical_profile"]["confirmed"] is False
    assert item["vertical_profile"]["service_origin_egm2008_m"] is None
    assert item["metadata"]["obstacle_profile"]["status"] == "unresolved"


def test_colocation_candidates_persistence_keeps_host_linkage():
    collection = build_tower_colocation_candidates(
        [tower()],
        obstacle_profiles=build_tower_obstacle_profiles(
            [tower()], terrain_by_tower={"T1": terrain_fact(100.0)},
        ),
        policy=confirmed_colocation_policy(),
    )
    collection["items"] = [normalize_candidate_site(item, index)
                           for index, item in enumerate(collection["items"])]
    reopened = normalize_tower_colocation_candidates(deepcopy(collection))
    assert reopened["items"][0]["metadata"]["host"]["host_tower_id"] == "T1"
    assert reopened["policy"]["planning_host_use_confirmed"] is True
    assert reopened["policy"]["physical_mount_confirmed"] is False
    assert reopened["policy"]["requires_site_survey"] is True


# ======================================================================================
# 3. CNS 共塔优先 / 普通站 fallback
# ======================================================================================


def device(subsystem="C"):
    return {
        "device_id": f"{subsystem}-1", "subsystem": subsystem, "enabled": True,
        "coverage_geometry": {
            "model": "hemisphere", "slant_range_m": 700.0,
            "source": "测试", "confirmed": True,
        },
        "service_model": {"model_family": "declared_performance", "confirmed": True},
    }


def target():
    return [{
        "segment_id": "R1:C:001", "route_id": "R1", "subsystem": "C",
        "start_route_offset_m": 0.0, "end_route_offset_m": 1000.0, "length_m": 1000.0,
        "requires_joint_optimization": False,
    }]


def colocation_site(**overrides):
    payload = {
        "site_id": "tower-colocation:T1", "name": "塔T1", "coordinate": [122.005, 30.005],
        "site_type": "地面角钢塔", "usable": True, "locked": False,
        "source": "真实通信铁塔站址（只读导入）",
        "vertical_profile": {
            "surface_elevation_m": 145.0, "surface_vertical_reference": "egm2008_orthometric",
            "mount_height_agl_m": 0.0, "service_origin_egm2008_m": 145.0,
            "source": "tower_colocation", "confirmed": True, "status": "confirmed",
        },
        "planning_profile": {
            "reuse_class": TOWER_COLOCATION_REUSE_CLASS, "add_device_allowed": True,
            "source": "tower_colocation_policy", "confirmed": True, "status": "confirmed",
        },
        "metadata": {"host": {"host_type": "tower", "host_tower_id": "T1"}},
    }
    payload.update(overrides)
    return normalize_candidate_site(payload)


def plain_site(**overrides):
    payload = {
        "site_id": "S1", "name": "普通候选站", "coordinate": [122.02, 30.02],
        "site_type": "other", "usable": True, "locked": False, "source": "用户导入",
        "available_subsystems": ["C"],
        "vertical_profile": {
            "surface_elevation_m": 100.0, "surface_vertical_reference": "egm2008_orthometric",
            "mount_height_agl_m": 0.0, "service_origin_egm2008_m": 100.0,
            "source": "测试", "confirmed": True, "status": "confirmed",
        },
        "planning_profile": {
            "reuse_class": "candidate_site", "add_device_allowed": True,
            "source": "测试", "confirmed": True, "status": "confirmed",
        },
    }
    payload.update(overrides)
    return normalize_candidate_site(payload)


def test_tower_colocation_tier_precedes_plain_candidate_and_new_build():
    assert REUSE_TIERS.index(TOWER_COLOCATION_REUSE_CLASS) < REUSE_TIERS.index("candidate_site")
    assert REUSE_TIERS.index(TOWER_COLOCATION_REUSE_CLASS) < REUSE_TIERS.index("new_build_candidate")
    # 已有 CNS 设备仍然最高优先：共塔是"真实站点复用"的一种，不取代已有设备复用
    assert REUSE_TIERS.index("existing_cns_facility") < REUSE_TIERS.index(
        TOWER_COLOCATION_REUSE_CLASS
    )


def test_tower_colocation_action_becomes_eligible_and_host_is_preserved():
    catalog = {"items": [device()]}
    actions = _candidate_actions(
        target(), {}, {"items": [plain_site()]}, catalog,
        {"items": [colocation_site()]},
    )
    colocation = next(item for item in actions if item["reuse_class"] == TOWER_COLOCATION_REUSE_CLASS)
    assert colocation["eligibility"]["status"] == "eligible"
    assert colocation["host"]["host_tower_id"] == "T1"
    assert colocation["planning_origin"] is None or isinstance(colocation["planning_origin"], dict)
    # 铁塔候选绝不变成 existing facility
    assert colocation["facility_id"] is None
    assert colocation["action_type"] == "add_device_to_explicit_site"


def test_planner_selects_tower_colocation_before_plain_candidate():
    catalog = {"items": [device()]}
    actions = _candidate_actions(
        target(), {}, {"items": [plain_site()]}, catalog,
        {"items": [colocation_site()]},
    )
    impact = {
        "action_id": actions[0]["action_id"], "status": "eligible",
        "resolved_segment_ids": ["R1:C:001"], "affected_segment_ids": ["R1:C:001"],
        "planning_gap_reduction_m": 1000.0,
        "segment_resolutions": [{
            "segment_id": "R1:C:001", "target_length_m": 1000.0,
            "resolved_intervals": [[0.0, 1000.0]], "planning_gap_reduction_m": 1000.0,
            "status": "resolved",
        }],
    }
    impacts = [dict(impact, action_id=action["action_id"]) for action in actions]
    result = ReuseFirstSitePlannerV1().plan(target(), actions, impacts, {})
    assert result["selected_actions"], "共塔候选必须被选中"
    assert result["selected_actions"][0]["reuse_class"] == TOWER_COLOCATION_REUSE_CLASS
    assert result["reuse_counts"][TOWER_COLOCATION_REUSE_CLASS] == 1
    assert result["reuse_counts"]["candidate_site"] == 0


def test_planner_falls_back_to_plain_candidate_when_tower_is_ineligible():
    catalog = {"items": [device()]}
    unusable = colocation_site(planning_profile={
        "reuse_class": TOWER_COLOCATION_REUSE_CLASS, "add_device_allowed": None,
        "source": "tower_colocation_policy_unconfirmed", "confirmed": False,
        "status": "pending_confirmation",
    })
    actions = _candidate_actions(
        target(), {}, {"items": [plain_site()]}, catalog, {"items": [unusable]},
    )
    impact = {
        "status": "eligible", "resolved_segment_ids": ["R1:C:001"],
        "affected_segment_ids": ["R1:C:001"], "planning_gap_reduction_m": 1000.0,
        "segment_resolutions": [{
            "segment_id": "R1:C:001", "target_length_m": 1000.0,
            "resolved_intervals": [[0.0, 1000.0]], "planning_gap_reduction_m": 1000.0,
            "status": "resolved",
        }],
    }
    impacts = [dict(impact, action_id=action["action_id"]) for action in actions]
    result = ReuseFirstSitePlannerV1().plan(target(), actions, impacts, {})
    assert result["selected_actions"][0]["reuse_class"] == "candidate_site"
    assert result["reuse_counts"][TOWER_COLOCATION_REUSE_CLASS] == 0
    assert result["reuse_counts"]["candidate_site"] == 1


def test_colocation_candidates_never_create_existing_facilities_in_state(tmp_path):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert service.state["existing_cns_facilities"]["items"] == []
    service.evaluate_tower_obstacle_profiles({})
    assert service.state["existing_cns_facilities"]["items"] == []
    assert service.state["tower_colocation_candidates"]["creates_existing_facilities"] is False


def _colocation_site_record(site_id="tower-colocation:T1", coordinate=None, *, confirmed=True):
    return normalize_candidate_site({
        "site_id": site_id, "name": "塔T1", "coordinate": coordinate or [0.0045, 0.0],
        "site_type": "地面角钢塔", "usable": True, "locked": False,
        "source": "真实通信铁塔站址（只读导入）",
        "vertical_profile": {
            "surface_elevation_m": 100.0, "surface_vertical_reference": "egm2008_orthometric",
            "mount_height_agl_m": 0.0, "service_origin_egm2008_m": 100.0,
            "source": "tower_colocation", "confirmed": True, "status": "confirmed",
        },
        "planning_profile": {
            "reuse_class": TOWER_COLOCATION_REUSE_CLASS,
            "add_device_allowed": True if confirmed else None,
            "source": "tower_colocation_policy", "confirmed": confirmed,
        },
        "metadata": {"host": {"host_type": "tower", "host_tower_id": "T1"}},
    })


def test_application_site_plan_prefers_tower_colocation_end_to_end(tmp_path):
    """走真实 P11 evaluate 路径（P7/P8 what-if）：共塔候选先于普通候选站被选中。"""

    from test_cns_site_planner import configure_workflow

    workflow = configure_workflow(tmp_path)
    workflow.state["tower_colocation_candidates"] = {
        "status": "passed", "count": 1, "items": [_colocation_site_record()],
    }
    result = workflow.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["status"] == "proposal_ready"
    assert result["selected_actions"][0]["reuse_class"] == TOWER_COLOCATION_REUSE_CLASS
    assert result["selected_actions"][0]["host"]["host_tower_id"] == "T1"
    assert result["reuse_counts"][TOWER_COLOCATION_REUSE_CLASS] == 1
    # 普通候选站没有被使用（共塔已经补上缺口）
    assert result["reuse_counts"]["candidate_site"] == 0
    # 提案阶段绝不真的写入 existing facility
    assert workflow.state["existing_cns_facilities"]["items"] == []


def test_application_site_plan_falls_back_to_plain_candidate_end_to_end(tmp_path):
    """共塔策略未确认时，真实 P11 路径继续用普通候选站补盲（prefer 不是 force）。"""

    from test_cns_site_planner import configure_workflow

    workflow = configure_workflow(tmp_path)
    workflow.state["tower_colocation_candidates"] = {
        "status": "passed", "count": 1,
        "items": [_colocation_site_record(confirmed=False)],
    }
    result = workflow.evaluate_cns_site_plan()["cns_site_plan"]
    assert result["status"] == "proposal_ready"
    assert result["selected_actions"][0]["reuse_class"] == "candidate_site"
    assert result["reuse_counts"][TOWER_COLOCATION_REUSE_CLASS] == 0
    assert result["reuse_counts"]["candidate_site"] == 1


def test_tower_endpoints_and_snapshots_are_registered(tmp_path):
    """API 与快照投影：前端"共塔候选 / 塔顶事实"依赖这四个入口。"""

    router_source=(Path(__file__).resolve().parent.parent / "cns_planner/api/router.py").read_text(
        encoding="utf-8",
    )
    for path in ("/api/tower-obstacle-profiles", "/api/tower-colocation-candidates",
                 "/api/tower-integration-policies", "/api/tower-obstacle-profiles/evaluate"):
        assert path in router_source, f"缺少端点 {path}"

    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    assert service.tower_obstacle_profiles_snapshot()["collection_id"] == "tower_obstacle_profiles"
    assert service.tower_colocation_candidates_snapshot()["collection_id"] == (
        "tower_colocation_candidates"
    )
    policies=service.tower_integration_policies_snapshot()
    assert set(policies) == {
        "tower_obstacle_policy", "tower_colocation_policy", "tower_clearance_policy",
    }
    assert "/api/tower-clearance-policy" in router_source
    # 快照以只读共享引用携带派生层（373 塔规模下不做整树深拷贝）
    snapshot=service.snapshot()
    assert snapshot["tower_obstacle_profiles"] is service.state["tower_obstacle_profiles"]
    assert snapshot["tower_colocation_candidates"] is service.state["tower_colocation_candidates"]


# ======================================================================================
# 4. 航路塔净空
# ======================================================================================


def test_cell_without_tower_keeps_its_original_verdict():
    cells = grid_cells(columns=2, rows=1)
    mask = mask_for(tower_facts(cells, {}), tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 50.0,
        "source": "测试", "confirmed": True,
    })
    assert mask["counts"] == {"feasible": 2, "blocked": 0, "unknown": 0}
    assert mask["tower_cell_count"] == 0


def test_altitude_below_the_tower_floor_is_blocked():
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(1, 1, 0, 290.0)})
    mask = mask_for(facts, altitude=300.0, tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
        "source": "测试", "confirmed": True,
    })
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "blocked"
    assert cell["reason_code"] == "altitude_below_tower_clearance_floor"
    assert cell["tower_required_clearance_egm2008_m"] == 310.0
    assert cell["tower_margin_m"] == -10.0
    assert mask["tower_cell_count"] == 1


def test_altitude_above_the_tower_floor_stays_feasible():
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(1, 1, 0, 270.0)})
    mask = mask_for(facts, altitude=300.0, tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
        "source": "测试", "confirmed": True,
    })
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "feasible"
    assert cell["tower_top_max_egm2008_m"] == 270.0
    assert cell["tower_margin_m"] == 10.0


def test_unconfirmed_tower_clearance_policy_is_unknown_never_feasible():
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(1, 1, 0, 200.0)})
    mask = mask_for(facts, tower_clearance=None)
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "unknown"
    assert cell["reason_code"] == "tower_clearance_not_configured"
    assert mask["tower_clearance_policy_status"] == "not_configured"


def test_unresolved_tower_height_is_unknown_never_obstacle_free():
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(2, 1, 1, 200.0)})
    mask = mask_for(facts, altitude=300.0, tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
        "source": "测试", "confirmed": True,
    })
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "unknown"
    assert cell["reason_code"] == "tower_height_unresolved"
    assert mask["tower_unresolved_cell_count"] == 1


def test_existing_blocked_cell_is_not_downgraded_by_a_lower_tower_floor():
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(1, 1, 0, 150.0)})
    facts[0]["terrain"]["surface_elevation_max_egm2008_m"] = 400.0
    mask = mask_for(facts, altitude=300.0, tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
        "source": "测试", "confirmed": True,
    })
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "blocked"
    assert cell["reason_code"] == "altitude_below_terrain_floor"


def test_tower_clearance_reason_codes_are_declared():
    for code in ("tower_clearance_not_configured", "tower_height_unresolved",
                 "altitude_below_tower_clearance_floor"):
        assert code in FEASIBILITY_REASON_CODES


def test_tower_facts_use_the_source_point_geometry_and_horizontal_clearance():
    cells = grid_cells(columns=3, rows=1)
    towers = {"T1": tower("T1", longitude=122.005, latitude=30.005)}
    profiles = build_tower_obstacle_profiles(
        [towers["T1"]], terrain_by_tower={"T1": terrain_fact(100.0)},
    )
    state = {
        "towers": {"items": list(towers.values())},
        "tower_obstacle_profiles": profiles,
        "tower_clearance_policy": normalize_tower_clearance_policy({
            "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
            "source": "测试", "confirmed": True,
        }),
    }
    zero_buffer = tower_facts_by_cell(cells, state)
    assert set(zero_buffer) == {cells[0]["grid_id"]}
    # 塔顶数值是**已解析**的诊断事实（地形 100 + 塔身 45），与塔的现场坐标一样照原样带出；
    # 但该塔没有确认权威，因此这一格的 data_status 必须是 unknown、reason 必须是
    # tower_top_not_confirmed —— 未确认塔顶不是 hard obstacle 证据（B3X §6.5）。
    assert zero_buffer[cells[0]["grid_id"]]["tower_top_max_egm2008_m"] == 145.0
    assert zero_buffer[cells[0]["grid_id"]]["data_status"] == "unknown"
    assert zero_buffer[cells[0]["grid_id"]]["reason"] == "tower_top_not_confirmed"
    assert zero_buffer[cells[0]["grid_id"]]["confirmed_count"] == 0

    # 显式水平净空把同一个塔扩展到相邻 cell（网格只是加速索引，塔坐标不变）
    state["tower_clearance_policy"] = normalize_tower_clearance_policy({
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 1500.0,
        "source": "测试", "confirmed": True,
    })
    wide = tower_facts_by_cell(cells, state)
    assert len(wide) == 3
    assert all(fact["tower_top_max_egm2008_m"] == 145.0 for fact in wide.values())


def test_tower_clearance_never_coarsens_the_tower_position():
    cells = grid_cells(columns=1, rows=1)
    # 塔在 cell 之外（但落在水平净空扩展范围内）时仍然被计入，而不是"吸附"到格心
    towers = {"T9": tower("T9", longitude=122.03, latitude=30.03)}
    profiles = build_tower_obstacle_profiles(
        [towers["T9"]], terrain_by_tower={"T9": terrain_fact(100.0)},
    )
    state = {
        "towers": {"items": list(towers.values())},
        "tower_obstacle_profiles": profiles,
        "tower_clearance_policy": normalize_tower_clearance_policy({
            "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
            "source": "测试", "confirmed": True,
        }),
    }
    assert tower_facts_by_cell(cells, state) == {}


def test_theta_v2_objective_weights_are_untouched():
    assert USER_DEFINED_BASELINE_WEIGHTS == {"risk": 0.8, "turn": 0.1, "distance": 0.1}


def test_towers_are_not_a_risk_framework_v2_factor_input():
    assert "towers" not in FACTOR_INPUT_ATTRIBUTES


def test_tower_heights_never_enter_the_risk_or_profile_math():
    """铁塔只进净空；Risk V2 / RouteRiskProfile / population×shelter 数学一行都不引用塔。"""

    root = Path(__file__).resolve().parent.parent
    for relative in (
        "cns_planner/risk/factors_v2.py",
        "cns_planner/risk/domains_v2.py",
        "cns_planner/risk/model_v2.py",
        "cns_planner/risk/route_profile.py",
        "cns_planner/domain/route_risk_profile.py",
        "cns_planner/domain/population_shelter.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "tower" not in source.lower(), f"{relative} 不得引用 towers"


# ======================================================================================
# 5. 定向失效
# ======================================================================================


def loaded_workflow(tmp_path, *, towers=True):
    service = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = service.state
    if towers:
        state["towers"] = {
            "status": "passed", "count": 1, "items": [tower()],
        }
    state["grid_risk"] = {"status": "passed", "cells": {}}
    state["grid_risk_v2"] = {"status": "passed", "cells": {}}
    state["result_statuses"]["environment_risk"] = "passed"
    state["result_statuses"]["grid_risk_v2"] = "passed"
    state["cns_site_plan"] = {"status": "proposal_ready"}
    state["result_statuses"]["cns_site_plan"] = "passed"
    # 一个"真的算过"的 layered candidate（空集合会被既有保护正确地忽略）
    state["layered_route_candidates"] = {
        "status": "passed", "count": 1, "masks": {},
        "items": [{"candidate_id": "C1", "status": "candidate", "lane_key": "R1@L8-LOW"}],
    }
    state["result_statuses"]["layered_route_candidate"] = "passed"
    state["tower_obstacle_profiles"] = {"status": "passed", "items": {}}
    state["result_statuses"]["tower_obstacle_profiles"] = "passed"
    state["tower_colocation_candidates"] = {"status": "passed", "items": []}
    state["result_statuses"]["tower_colocation_candidates"] = "passed"
    return service


def test_tower_source_change_never_stales_grid_risk_or_risk_v2(tmp_path):
    service = loaded_workflow(tmp_path)
    service.invalidation_service.grid_sources({"towers"})
    state = service.state
    assert state["grid_risk"]["status"] == "passed"
    assert state["grid_risk_v2"]["status"] == "passed"
    assert state["result_statuses"]["environment_risk"] == "passed"
    assert state["result_statuses"]["grid_risk_v2"] == "passed"


def test_tower_source_change_stales_tower_derived_facts_and_their_consumers(tmp_path):
    service = loaded_workflow(tmp_path)
    service.invalidation_service.grid_sources({"towers"})
    state = service.state
    assert state["result_statuses"]["tower_obstacle_profiles"] == "stale"
    assert state["result_statuses"]["tower_colocation_candidates"] == "stale"
    assert state["result_statuses"]["layered_route_candidate"] == "stale"
    assert state["result_statuses"]["cns_site_plan"] == "stale"


def test_recomputing_tower_facts_stales_only_the_downstream(tmp_path):
    service = loaded_workflow(tmp_path)
    service.evaluate_tower_obstacle_profiles({})
    state = service.state
    # 刚重算的派生事实保持 current
    assert state["result_statuses"]["tower_obstacle_profiles"] == "passed"
    assert state["result_statuses"]["tower_colocation_candidates"] == "passed"
    # 消费它们的下游过时
    assert state["result_statuses"]["layered_route_candidate"] == "stale"
    assert state["result_statuses"]["cns_site_plan"] == "stale"
    # 风险数学完全不受影响
    assert state["grid_risk"]["status"] == "passed"
    assert state["grid_risk_v2"]["status"] == "passed"


def test_terrain_source_change_still_stales_risk_v2(tmp_path):
    service = loaded_workflow(tmp_path)
    service.invalidation_service.grid_sources({"terrain"})
    assert service.state["grid_risk_v2"]["status"] == "stale"


def test_tower_state_survives_save_and_reopen(tmp_path):
    service = loaded_workflow(tmp_path)
    service.evaluate_tower_obstacle_profiles({})
    service.session.save()
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    state = reopened.state
    assert state["tower_obstacle_policy"]["status"] in ("pending_confirmation", "confirmed")
    assert state["tower_clearance_policy"]["tower_vertical_clearance_m"] is None
    # save/reopen 保留共塔候选与宿主关联（373 塔场景下这一层就是候选全集）
    assert state["tower_colocation_candidates"]["count"] == 1
    host = state["tower_colocation_candidates"]["items"][0]["metadata"]["host"]
    assert host["host_tower_id"] == "T1"
    assert state["tower_obstacle_profiles"]["collection_id"] == "tower_obstacle_profiles"


def test_normalize_project_backfills_the_tower_derived_keys(tmp_path):
    service = loaded_workflow(tmp_path)
    value = deepcopy(service.state)
    for key in ("tower_obstacle_profiles", "tower_colocation_candidates",
                "tower_obstacle_policy", "tower_clearance_policy", "tower_colocation_policy"):
        value.pop(key, None)
    normalized = normalize_project(value, service.grid_service)
    assert normalized["tower_obstacle_profiles"]["count"] == 0
    assert normalized["tower_colocation_candidates"]["items"] == []
    assert normalized["tower_clearance_policy"]["status"] == "not_configured"


def test_unknown_policy_values_are_rejected():
    with pytest.raises(ValueError):
        normalize_tower_clearance_policy({"tower_vertical_clearance_m": -1.0})
    with pytest.raises(ValueError):
        normalize_tower_colocation_policy({"service_origin_assumption": "tower_middle"})


# ======================================================================================
# 6. Policy UI 的后端支撑（Step03 塔净空 / Step05 共塔策略）
# ======================================================================================


def test_clearance_policy_save_enters_state_stale_chain_and_fingerprint(tmp_path):
    """Step03 保存塔净空：进 state → 进 fingerprint → 进既有 stale 链（不动风险数学）。"""

    service = loaded_workflow(tmp_path)
    assert service.state["result_statuses"]["tower_colocation_candidates"] == "passed"

    service.set_tower_clearance_policy({
        "tower_vertical_clearance_m": 25.0, "tower_horizontal_clearance_m": 80.0,
        "source": "user_configuration", "confirmed": True,
    })
    policy = service.state["tower_clearance_policy"]
    assert policy["tower_vertical_clearance_m"] == 25.0
    assert policy["tower_horizontal_clearance_m"] == 80.0
    assert policy["status"] == "confirmed" and policy["confirmed"] is True

    state = service.state
    # 进既有 stale 链：layered candidate（塔净空进入 feasibility）
    assert state["result_statuses"]["layered_route_candidate"] == "stale"
    # 不 stale 共塔宿主候选（净空不改变宿主事实）
    assert state["result_statuses"]["tower_colocation_candidates"] == "passed"
    # 绝不动风险数学
    assert state["grid_risk"]["status"] == "passed"
    assert state["grid_risk_v2"]["status"] == "passed"

    # 进 fingerprint：同一批 cells 在两个 policy 下 mask 指纹必须不同
    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(1, 1, 0, 290.0)})
    unconfigured = mask_for(facts, altitude=300.0)
    configured = mask_for(facts, altitude=300.0, tower_clearance=policy)
    assert unconfigured["input_fingerprint"] != configured["input_fingerprint"]
    assert unconfigured["mask_fingerprint"] != configured["mask_fingerprint"]
    assert configured["tower_clearance_policy_status"] == "confirmed"
    # 290 + 25 = 315 m > 300 m ⇒ blocked（未配置时同一格是 unknown，绝不 feasible）
    assert configured["cells"][cells[0]["grid_id"]]["status"] == "blocked"
    assert unconfigured["cells"][cells[0]["grid_id"]]["status"] == "unknown"


def test_clearance_policy_never_invents_a_default_value(tmp_path):
    """没有工程依据时不写任何数值：只填一个净空仍是 not_configured。"""

    service = loaded_workflow(tmp_path)
    service.set_tower_clearance_policy({
        "tower_vertical_clearance_m": 25.0, "source": "user_configuration", "confirmed": True,
    })
    policy = service.state["tower_clearance_policy"]
    assert policy["tower_vertical_clearance_m"] == 25.0
    assert policy["tower_horizontal_clearance_m"] is None
    assert policy["status"] == "not_configured"

    # 空 payload（例如用户清空两个输入框）回到全空：不配置就是不配置
    service.set_tower_clearance_policy({})
    cleared = service.state["tower_clearance_policy"]
    assert cleared["tower_vertical_clearance_m"] is None
    assert cleared["tower_horizontal_clearance_m"] is None
    assert cleared["status"] == "not_configured"
    with pytest.raises(ValueError):
        service.set_tower_clearance_policy({"tower_vertical_clearance_m": -5.0})


def test_colocation_policy_save_through_the_existing_endpoint(tmp_path):
    """Step05 保存共塔策略复用现有 /api/tower-obstacle-profiles/evaluate 语义。"""

    service = loaded_workflow(tmp_path)
    service.evaluate_tower_obstacle_profiles({"tower_colocation_policy": {
        "service_origin_assumption": "tower_top_agl_0",
        "planning_host_use_confirmed": True,
        "source": "user_configuration", "confirmed": True,
    }})
    policy = service.state["tower_colocation_policy"]
    assert policy["enabled"] is True and policy["status"] == "confirmed"
    assert policy["planning_host_status"] == "eligible"
    assert policy["subsystem_mount_status"] == "unverified"
    item = service.state["tower_colocation_candidates"]["items"][0]
    assert item["planning_profile"]["confirmed"] is True
    assert item["planning_profile"]["add_device_allowed"] is True
    # 规划层确认**不**等于物理安装确认（FIX-TOWER-SEM-001）
    assert item["metadata"]["host"]["planning_host_use_confirmed"] is True
    assert item["metadata"]["host"]["physical_mount_confirmed"] is False
    assert item["metadata"]["host"]["requires_site_survey"] is True
    assert item["metadata"]["host"]["device_mount_confirmed"] is False
    assert item["metadata"]["planning_host"]["subsystem_mount_status"] == "unverified"
    # 没有 tower facts provider ⇒ 塔顶未解析 ⇒ 服务原点仍未确认（fail-closed）
    assert item["vertical_profile"]["confirmed"] is False
    assert item["vertical_profile"]["service_origin_egm2008_m"] is None


def test_colocation_policy_needs_planning_host_and_origin_only(tmp_path):
    """只有两个条件：规划宿主允许 + 服务原点假设；物理安装确认不再是启用条件。"""

    service = loaded_workflow(tmp_path)
    for payload in (
        {"service_origin_assumption": "tower_top_agl_0",
         "planning_host_use_confirmed": False, "source": "user_configuration", "confirmed": True},
        {"service_origin_assumption": None,
         "planning_host_use_confirmed": True, "source": "user_configuration", "confirmed": True},
    ):
        service.evaluate_tower_obstacle_profiles({"tower_colocation_policy": payload})
        policy = service.state["tower_colocation_policy"]
        assert policy["enabled"] is False, payload
        assert policy["status"] == "pending_confirmation"
        assert policy["planning_host_status"] == "not_confirmed"
        item = service.state["tower_colocation_candidates"]["items"][0]
        assert item["planning_profile"]["add_device_allowed"] is None
        assert item["planning_profile"]["confirmed"] is False


# ======================================================================================
# 7. FIX-TOWER-SEM-001 / SEM-002：规划宿主 vs 物理安装 / 分系统兼容性
# ======================================================================================


def test_global_host_confirmation_never_implies_physical_mount_for_all_towers():
    """全局"允许共塔规划"绝不等于 373 个铁塔 physical mount confirmed。"""

    towers = [tower(f"T{index:04d}", longitude=122.0 + 0.001 * index,
                    latitude=30.0 + 0.001 * index) for index in range(20)]
    profiles = build_tower_obstacle_profiles(
        towers, terrain_by_tower={item["tower_id"]: terrain_fact(100.0) for item in towers},
    )
    collection = build_tower_colocation_candidates(
        towers, obstacle_profiles=profiles, policy=confirmed_colocation_policy(),
    )
    assert collection["count"] == 20
    for item in collection["items"]:
        host = item["metadata"]["host"]
        assert host["planning_host_use_confirmed"] is True, "规划层已允许"
        assert host["physical_mount_confirmed"] is False, "物理安装绝不被全局策略确认"
        assert host["requires_site_survey"] is True
        assert host["device_mount_confirmed"] is False
    # 集合级也明确声明
    assert collection["creates_existing_facilities"] is False
    assert collection["invents_device_parameters"] is False


def test_physical_mount_and_site_survey_defaults_are_fail_closed():
    default = default_tower_colocation_policy()
    assert default["physical_mount_confirmed"] is False
    assert default["requires_site_survey"] is True
    assert default["planning_host_use_confirmed"] is False
    assert default["device_mount_confirmed"] is False
    # 任何输入组合都不能把物理层打开（本阶段没有逐塔调查数据）
    for payload in (
        {"planning_host_use_confirmed": True, "physical_mount_confirmed": True,
         "requires_site_survey": False, "device_mount_confirmed": True,
         "service_origin_assumption": "tower_top_agl_0", "confirmed": True},
        {"confirmed": True, "device_mount_confirmed": True,
         "service_origin_assumption": "tower_top_agl_0"},
    ):
        policy = normalize_tower_colocation_policy(payload)
        assert policy["physical_mount_confirmed"] is False
        assert policy["requires_site_survey"] is True
        assert policy["device_mount_confirmed"] is False


def test_legacy_policy_input_still_enables_planning_host_only():
    """旧项目 reopen：legacy confirmed + device_mount_confirmed 只映射到规划层。"""

    legacy = normalize_tower_colocation_policy({
        "service_origin_assumption": "tower_top_agl_0",
        "device_mount_confirmed": True, "confirmed": True, "source": "旧项目",
    })
    assert legacy["planning_host_use_confirmed"] is True
    assert legacy["enabled"] is True
    assert legacy["physical_mount_confirmed"] is False
    # 幂等：再 normalize 一次结果不变
    assert normalize_tower_colocation_policy(legacy) == legacy


def test_empty_available_subsystems_is_not_a_capability_claim(tmp_path):
    """available_subsystems=[] 表示"没有证据"，不是"所有目标设备都能装"。"""

    catalog = {"items": [device("C"), device("N"), device("S")]}
    targets = target() + [
        {
            "segment_id": f"R1:{code}:001", "route_id": "R1", "subsystem": code,
            "start_route_offset_m": 0.0, "end_route_offset_m": 1000.0, "length_m": 1000.0,
            "requires_joint_optimization": False,
        }
        for code in ("N", "S")
    ]
    actions = _candidate_actions(
        targets, {}, {}, catalog, {"items": [colocation_site()]},
    )
    assert {action["subsystem"] for action in actions} == {"C", "N", "S"}
    for action in actions:
        # 无证据 → unverified，且**不**因此被排除（共塔方案仍可参与 what-if 比较）
        assert action["subsystem_mount_status"] == "unverified"
        assert action["eligibility"]["status"] == "eligible"
        assert action["requires_site_survey"] is True
        assert action["physical_mount_confirmed"] is False
    # 塔本身仍然不声明任何可用分系统
    site = colocation_site()
    assert site["available_subsystems"] == []


def test_declared_subsystems_are_still_enforced():
    """站点**显式声明**的可用分系统仍然是硬约束（声明与设备不匹配 ⇒ ineligible）。"""

    catalog = {"items": [device("C")]}
    actions = _candidate_actions(
        target(), {}, {"items": [plain_site(available_subsystems=["S"])]}, catalog,
    )
    action = actions[0]
    assert action["subsystem_mount_status"] == "declared_not_compatible"
    assert action["eligibility"]["status"] == "ineligible"
    assert any("可用分系统不包含" in reason for reason in action["eligibility"]["reasons"])
    compatible = _candidate_actions(
        target(), {}, {"items": [plain_site(available_subsystems=["C"])]}, catalog,
    )[0]
    assert compatible["subsystem_mount_status"] == "declared_compatible"
    assert compatible["eligibility"]["status"] == "eligible"


def test_colocation_action_keeps_two_layer_status_through_save_and_reopen(tmp_path):
    """CandidateAction 的两层状态进入 state、save/reopen 不丢。"""

    service = loaded_workflow(tmp_path)
    service.evaluate_tower_obstacle_profiles({"tower_colocation_policy": {
        "service_origin_assumption": "tower_top_agl_0",
        "planning_host_use_confirmed": True,
        "source": "user_configuration", "confirmed": True,
    }})
    state = service.state
    state["device_catalog"] = {"status": "passed", "items": [device("C")]}
    state["cns_gap_analysis_v2"] = {
        "status": "confirmed_gap", "routes": [{
            "route_id": "R1", "route_length_m": 1000.0,
            "subsystems": [{"subsystem": "C", "segments": []}],
        }],
    }
    actions = _candidate_actions(
        target(), state.get("existing_cns_facilities") or {},
        state.get("candidate_sites") or {}, state.get("device_catalog") or {},
        state.get("tower_colocation_candidates") or {},
    )
    action = next(item for item in actions
                  if item["reuse_class"] == TOWER_COLOCATION_REUSE_CLASS)
    assert action["host"]["host_tower_id"] == "T1"
    assert action["device_id"] == "C-1"
    assert action["subsystem"] == "C"
    assert action["planning_host_status"] == "eligible"
    assert action["subsystem_mount_status"] == "unverified"
    assert action["physical_mount_confirmed"] is False
    assert action["requires_site_survey"] is True
    # 内联持久化：写入 plan 后 save/reopen 仍保留这些字段
    service.session.state["cns_site_plan"] = {
        "status": "proposal_ready", "candidate_actions": [action], "selected_actions": [action],
    }
    service.session.save()
    reopened = WorkflowService(tmp_path / "project.json", DEFAULTS)
    stored = reopened.state["cns_site_plan"]["candidate_actions"][0]
    for field in ("host", "device_id", "subsystem", "planning_host_status",
                  "subsystem_mount_status", "physical_mount_confirmed", "requires_site_survey"):
        assert field in stored, f"save/reopen 丢失 {field}"
    assert stored["physical_mount_confirmed"] is False
    assert stored["requires_site_survey"] is True


def test_unresolved_tower_cell_is_unknown_and_the_gate_fails_closed():
    """塔顶未解析 ⇒ cell 为 unknown；Theta* gate 把它当作不可穿越（fail-closed）。"""

    from cns_planner.layered_route_planner.theta_star_v2 import _build_gate

    cells = grid_cells(columns=1, rows=1)
    facts = tower_facts(cells, {cells[0]["grid_id"]: cell_tower_fact(2, 1, 1, 200.0)})
    mask = mask_for(facts, altitude=300.0, tower_clearance={
        "tower_vertical_clearance_m": 20.0, "tower_horizontal_clearance_m": 0.0,
        "source": "测试", "confirmed": True,
    })
    cell = mask["cells"][cells[0]["grid_id"]]
    assert cell["status"] == "unknown"
    assert cell["tower_required_clearance_egm2008_m"] is None, "不生成具体 clearance floor"

    class _Graph:
        def __init__(self, bbox_by_id):
            self.cells = {grid_id: {"bbox": bbox} for grid_id, bbox in bbox_by_id.items()}

    gate, diagnostics = _build_gate(
        graph=_Graph({cells[0]["grid_id"]: cells[0]["bbox"]}), index_map=None, mask=mask,
        hard_constraints=[], regulatory=None, altitude=300.0,
    )
    verdict = gate(cells[0]["grid_id"])
    assert verdict is not None, "unknown 必须 fail-closed（不可穿越）"
    assert verdict["domain"] == "unknown"
    assert verdict["reason_code"] == "tower_height_unresolved"
    assert diagnostics["gate_reasons"][cells[0]["grid_id"]] == "tower_height_unresolved"


def test_adapter_metadata_separates_building_and_tower_horizontal_clearance():
    """describe() 不能再笼统声明整个 adapter not_horizontal_clearance。"""

    from cns_planner.domain.layered_route import COARSE_ENVELOPE_SEMANTICS
    from cns_planner.gis.layered_feasibility_adapter import LayeredFeasibilityAdapter

    described = LayeredFeasibilityAdapter(None).describe()
    assert described["building_horizontal_clearance"] == (
        "not_modeled_here_deferred_to_continuous_validation"
    )
    assert described["tower_horizontal_clearance"] == "explicit_policy_bbox_envelope"
    assert described["tower_horizontal_clearance_envelope"] == (
        "coarse_bbox_envelope_not_exact_radial_clearance"
    )
    assert described["tower_horizontal_clearance_is_exact_radial"] is False
    # mask 语义同样按域拆分（并保留"整体不是精确水平净空"的既有键）
    assert COARSE_ENVELOPE_SEMANTICS["tower_horizontal_clearance"] == (
        "explicit_policy_bbox_envelope"
    )
    assert COARSE_ENVELOPE_SEMANTICS["tower_horizontal_clearance_is_exact_radial"] is False
    assert COARSE_ENVELOPE_SEMANTICS["building_horizontal_clearance"] == (
        "not_modeled_here_deferred_to_continuous_validation"
    )
    assert COARSE_ENVELOPE_SEMANTICS["not_horizontal_clearance"] is True
    # describe 里不再有会误导的笼统声明
    assert "not_horizontal_clearance" not in {
        key for key, value in described.items() if value is True and key.startswith("tower")
    }


def test_rooftop_markers_include_louding_and_stay_conservative():
    """FIX-TOWER-TYPE-001：'楼顶'明确属于 rooftop；其余未知类型不被自动分类。"""

    from cns_planner.domain.tower_obstacle import GROUND_MARKERS, ROOFTOP_MARKERS

    assert "楼顶" in ROOFTOP_MARKERS
    assert classify_base_type("楼顶景观塔")[0] == "rooftop"
    assert classify_base_type("楼顶景观塔")[1] == "site_type_rooftop_marker"
    # 其余 unknown 类型本轮**不**自动分类，也不被设为 ground
    for site_type in ("角钢塔", "H杆塔", "单管塔", "造型景观塔", "水泥杆塔",
                      "通信灯杆塔", "一体化塔房", None):
        assert classify_base_type(site_type)[0] == "unknown", site_type
    assert "地面" in GROUND_MARKERS and "落地" in GROUND_MARKERS
    # 楼顶塔按 rooftop 公式计算（terrain + building + structure）
    profile = build_tower_obstacle_profile(
        tower(site_type="楼顶景观塔", height_m=12.0),
        terrain=terrain_fact(100.0), building=building_fact(30.0),
    )
    assert profile["base_type"] == "rooftop"
    assert profile["status"] == "resolved"
    assert profile["tower_top_orthometric_m"] == 142.0


REAL_TOWER_XLSX = Path(
    r"D:\aaa2026project\UOM\舟山\基础数据\各单位报送的补充材料\各单位报送的补充材料"
    r"\航路航线规划-铁塔数据.xlsx"
)


@pytest.mark.skipif(not REAL_TOWER_XLSX.is_file(), reason="真实铁塔报送数据不在本机")
def test_real_tower_site_type_classification_is_219_39_115():
    """真实 373 条数据重统计：楼顶景观塔 ×2 变为 rooftop ⇒ 219 / 39 / 115。"""

    from collections import Counter

    from cns_planner.reference_data.towers import load_towers

    record = load_towers(REAL_TOWER_XLSX)
    assert record["count"] == 373
    buckets = Counter(classify_base_type(item.get("site_type"))[0] for item in record["items"])
    assert buckets["rooftop"] == 219
    assert buckets["ground"] == 39
    assert buckets["unknown"] == 115
    types = Counter(item.get("site_type") for item in record["items"])
    assert types["楼顶景观塔"] == 2
    unknown_types = {
        site_type for site_type in types if classify_base_type(site_type)[0] == "unknown"
    }
    assert unknown_types == {
        "角钢塔", "H杆塔", "单管塔", "造型景观塔", "水泥杆塔", "通信灯杆塔",
        "一体化塔房", None,
    }
    assert "楼顶景观塔" not in unknown_types
    assert sum(types[site_type] for site_type in unknown_types) == 115
