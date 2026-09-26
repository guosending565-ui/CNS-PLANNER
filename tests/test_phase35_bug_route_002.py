"""Phase 3.5 BUG-ROUTE-002：Step03 feasibility policy 保存链回归测试（后端）。

复现与锁定：
  * 人工填写 feasibility policy source（20 m 垂直净空工程基线）并显式勾选确认后保存，
    source 与 confirmed 必须真正持久化，并在重新加载项目后原样恢复；
  * 保存成功后 readiness 必须报告 ``feasibility_policy.status == confirmed``；
  * 只有 confirmed 之后，selected-layer feasibility mask 才可能计算；
  * 空 source + confirmed 仍然被拒绝（不得绕过 confirmation 解除 blocker）；
  * unknown 仍然 fail-closed，绝不因为"已确认"就降级成 feasible。

禁止事项（本文件不触碰）：Theta* V2 搜索算法、RiskProfile 数学模型、
objective 0.8/0.1/0.1、Validation / Adoption、altitude layer 逻辑。
"""

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.api.router import ApiRouter
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.layered_route import (
    normalize_layered_route_feasibility_policy, resolve_cruise_altitude,
)
from cns_planner.domain.spatial_3d import normalize_spatial_3d
from cns_planner.layered_route_planner.feasibility import build_layer_feasibility_mask

DEFAULTS = Path("cns_planner/config/defaults.json")
LEVEL = 8
LAYER_ID = "L8-LOW"

#: 人工在 Step03 填写的工程依据（与 BUG-ROUTE-002 报告一致）。
HUMAN_SOURCE = "Phase3.5人工验收，沿用项目当前已确认20m垂直净空工程基线"
#: 项目当前已确认的垂直净空工程基线：不得被本次修复改写。
CONFIRMED_CLEARANCE_M = 20.0


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def grid_cells(columns=3, rows=1):
    cells = []
    for column in range(columns):
        for row in range(rows):
            west = 122.0 + 0.01 * column
            south = 30.0 + 0.01 * row
            cells.append({
                "grid_id": f"MHT4063-L{LEVEL}-C{column}-RP{row}", "level": LEVEL,
                "column": column, "row": row,
                "bbox": [west, south, west + 0.01, south + 0.01],
                "center": [west + 0.005, south + 0.005],
            })
    return cells


def layer():
    return {
        "altitude_layer_id": LAYER_ID, "name": "低层",
        "nominal_altitude_m": 300.0, "lower_altitude_m": 250.0, "upper_altitude_m": 350.0,
        "vertical_reference": "egm2008_orthometric", "source": "工程确认-测试",
        "evidence": {"note": "unit test"}, "confirmed": True,
    }


def stub_facts(grid, *, elevation=10.0, buildings=None):
    buildings = buildings or {}
    return [
        {
            "grid_id": cell["grid_id"],
            "terrain": {
                "data_status": "passed", "surface_elevation_max_egm2008_m": elevation,
                "sampling": "intersecting_valid_fabdem_pixels_max_egm2008",
            },
            "buildings": buildings.get(cell["grid_id"], {
                "data_status": "passed", "building_count": 0, "height_max_m": None,
                "valid_height_fraction": None,
            }),
        }
        for cell in grid
    ]


def seed(service):
    """把一个带网格 / 航路 / 已确认建筑净空策略的项目写进 service（不保存）。"""

    grid = grid_cells()
    service.state["grid"] = {
        "status": "passed", "level": LEVEL, "count": len(grid), "cells": grid,
    }
    service.state["scenario_routes"] = [{
        "route_id": "R0001", "start_node_id": "N001", "end_node_id": "N002",
        "start": list(grid[0]["center"]), "end": list(grid[-1]["center"]),
    }]
    service.state["spatial_3d"]["altitude_layers"] = [
        normalize_spatial_3d({"altitude_layers": [layer()]})["altitude_layers"][0]
    ]
    # 既有的、已确认的 building clearance policy：feasibility policy 绝不复用它自己的定义，
    # 也不替它做确认。
    service.state["building_clearance_policy"].update({
        "horizontal_clearance_m": 0.0, "vertical_clearance_m": 10.0,
        "source": "工程确认-测试", "confirmed": True, "status": "confirmed",
    })
    return grid


def project(tmp_path, name="bug-route-002.json"):
    service = WorkflowService(tmp_path / name, DEFAULTS)
    grid = seed(service)
    return service, grid


def human_payload(**overrides):
    """Step03 表单提交的 payload：已有 20 m + 人工 source + 显式确认。"""

    payload = {
        "terrain_vertical_clearance_m": CONFIRMED_CLEARANCE_M,
        "source": HUMAN_SOURCE,
        "confirmed": True,
    }
    payload.update(overrides)
    return payload


def mask_from_saved_policy(service, facts):
    return build_layer_feasibility_mask(
        request=service.state["layered_route_planning_request"],
        cruise_altitude=resolve_cruise_altitude(
            service.state["spatial_3d"]["altitude_layers"][0]
        ),
        cells=facts,
        feasibility_policy=service.state["layered_route_feasibility_policy"],
        building_clearance_policy=service.state["building_clearance_policy"],
        source_audits={}, grid_level=LEVEL,
    )


def risk_v2(grid, index=0.5):
    return {
        "status": "passed", "algorithm_id": "risk-framework-v2-domains",
        "algorithm_version": "2.0", "input_fingerprint": "riskv2-input",
        "policy_fingerprint": "riskv2-policy",
        "cells": {
            cell["grid_id"]: {
                "grid_id": cell["grid_id"], "status": "passed",
                "domains": {
                    domain_id: {"domain_id": domain_id, "status": "passed", "index": index}
                    for domain_id in ("ground", "air_traffic", "environment_obstacle")
                },
                "factors": {},
            }
            for cell in grid
        },
    }


class StubAdapter:
    """GIS 边界的替身：只回放 canonical facts，绝不构造环境。"""

    def __init__(self, facts):
        self.facts = facts

    def build_cells(self, grid_cells, state):
        return deepcopy(self.facts)

    def describe(self):
        return {"adapter_id": "stub"}

    def usable(self):
        return True, None


# --------------------------------------------------------------------------------------
# 1. 保存链：payload → 状态服务 → 快照
# --------------------------------------------------------------------------------------


def test_saving_the_form_payload_keeps_source_and_explicit_confirmation(tmp_path):
    service, _ = project(tmp_path)
    service.set_layered_route_feasibility_policy(human_payload())
    policy = service.layered_route_feasibility_policy()
    assert policy["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M
    assert policy["source"] == HUMAN_SOURCE
    assert policy["confirmed"] is True
    assert policy["status"] == "confirmed"
    assert policy["status_reason"] is None
    # workflow snapshot 是前端的唯一数据源：它必须带上同样的三个字段。
    snapshot = service.snapshot()["layered_route_feasibility_policy"]
    assert snapshot["source"] == HUMAN_SOURCE
    assert snapshot["confirmed"] is True
    assert snapshot["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M


def test_the_saved_policy_survives_a_project_reload(tmp_path):
    path = tmp_path / "bug-route-002-reload.json"
    service = WorkflowService(path, DEFAULTS)
    seed(service)
    service.set_layered_route_feasibility_policy(human_payload())
    reloaded = WorkflowService(path, DEFAULTS)
    policy = reloaded.layered_route_feasibility_policy()
    assert policy["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M
    assert policy["source"] == HUMAN_SOURCE
    assert policy["confirmed"] is True
    assert policy["status"] == "confirmed"


def test_the_api_endpoint_persists_and_returns_source_and_confirmation(tmp_path):
    service, _ = project(tmp_path)
    router = ApiRouter(_ApiContext(service))
    response = router.post("/api/layered-route-feasibility-policy", human_payload())
    policy = response.data["layered_route_feasibility_policy"]
    assert policy["source"] == HUMAN_SOURCE
    assert policy["confirmed"] is True
    assert policy["status"] == "confirmed"
    # 前端拿到的是这份 payload，重新渲染时必须能原样回填。
    assert router.get("/api/layered-route-feasibility-policy", {}, {}).data["source"] == HUMAN_SOURCE


def test_readiness_reports_the_confirmed_feasibility_policy(tmp_path):
    service, _ = project(tmp_path)
    service.set_layered_route_feasibility_policy(human_payload())
    readiness = service.layered_route_planner_readiness()
    assert readiness["feasibility_policy"]["status"] == "confirmed"
    assert readiness["feasibility_policy"]["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M
    assert readiness["feasibility_policy"]["source"] == HUMAN_SOURCE
    reason_codes = {item["reason_code"] for item in readiness["blockers"]}
    assert "feasibility_policy_not_confirmed" not in reason_codes
    assert "terrain_vertical_clearance_not_configured" not in reason_codes


# --------------------------------------------------------------------------------------
# 2. confirmation 语义：不绕过、不降级、不臆造
# --------------------------------------------------------------------------------------


def test_confirmation_without_a_source_is_rejected(tmp_path):
    service, _ = project(tmp_path)
    with pytest.raises(ValueError, match="显式 source"):
        service.set_layered_route_feasibility_policy(
            human_payload(source="")
        )


def test_blank_source_keeps_the_policy_pending_instead_of_confirming_it(tmp_path):
    service, _ = project(tmp_path)
    service.set_layered_route_feasibility_policy(human_payload(confirmed=False))
    policy = service.layered_route_feasibility_policy()
    assert policy["confirmed"] is False
    assert policy["status"] == "pending_confirmation"
    assert policy["status_reason"] == "feasibility_policy_not_confirmed"
    # 20 m 本身不被改写：字段值保留，blocker 只由 confirmation 缺失造成。
    assert policy["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M
    assert policy["source"] == HUMAN_SOURCE


def test_missing_clearance_stays_blocked_even_when_confirmed(tmp_path):
    service, _ = project(tmp_path)
    service.set_layered_route_feasibility_policy(human_payload(
        terrain_vertical_clearance_m=None,
    ))
    policy = service.layered_route_feasibility_policy()
    assert policy["status"] == "blocked"
    assert policy["status_reason"] == "terrain_vertical_clearance_not_configured"


def test_normalized_policy_never_invents_a_source_for_an_unconfirmed_policy():
    policy = normalize_layered_route_feasibility_policy({
        "terrain_vertical_clearance_m": None,
    })
    assert policy["confirmed"] is False
    assert policy["status"] == "blocked"
    assert policy["terrain_vertical_clearance_m"] is None


# --------------------------------------------------------------------------------------
# 3. 保存后 selected-layer feasibility mask 才可计算；unknown 仍 fail-closed
# --------------------------------------------------------------------------------------


def test_the_saved_policy_unblocks_the_selected_layer_mask(tmp_path):
    service, grid = project(tmp_path)
    facts = stub_facts(grid)
    # 未确认时：每个 cell 都是 unknown（绝不当作 feasible）。
    before = mask_from_saved_policy(service, facts)
    assert before["counts"]["unknown"] == len(grid)
    assert before["counts"]["feasible"] == 0
    service.set_layered_route_feasibility_policy(human_payload())
    after = mask_from_saved_policy(service, facts)
    assert after["counts"]["feasible"] == len(grid)
    assert after["counts"]["unknown"] == 0
    assert after["feasibility_policy_fingerprint"]
    # 20 m 是唯一被消费的净空值：不得被替换成别的默认值。
    cell = after["cells"][grid[0]["grid_id"]]
    assert cell["terrain_vertical_clearance_m"] == CONFIRMED_CLEARANCE_M
    assert cell["terrain_floor_egm2008_m"] == 10.0 + CONFIRMED_CLEARANCE_M


def test_unknown_terrain_stays_unknown_after_confirmation(tmp_path):
    service, grid = project(tmp_path)
    facts = stub_facts(grid)
    facts[0]["terrain"] = {
        "data_status": "unknown", "surface_elevation_max_egm2008_m": None,
        "reason": "no_valid_dtm_pixel_intersects_this_fine_cell_nodata_is_not_flat_ground",
    }
    service.set_layered_route_feasibility_policy(human_payload())
    mask = mask_from_saved_policy(service, facts)
    assert mask["cells"][grid[0]["grid_id"]]["status"] == "unknown"
    assert mask["cells"][grid[0]["grid_id"]]["reason_code"] == "terrain_data_unavailable"
    assert mask["counts"]["feasible"] == len(grid) - 1
    assert mask["counts"]["unknown"] == 1


def test_the_policy_fingerprint_changes_when_only_the_source_changes(tmp_path):
    service, _ = project(tmp_path)
    service.set_layered_route_feasibility_policy(human_payload())
    first = deepcopy(service.layered_route_feasibility_policy())
    service.set_layered_route_feasibility_policy(human_payload(source=HUMAN_SOURCE + "（复审）"))
    second = service.layered_route_feasibility_policy()
    assert second["source"] != first["source"]
    assert second["status"] == "confirmed"
    assert second["terrain_vertical_clearance_m"] == first["terrain_vertical_clearance_m"]


# --------------------------------------------------------------------------------------
# 4. building vertical clearance policy：独立状态服务，feasibility policy 绝不代它确认
# --------------------------------------------------------------------------------------


def test_building_vertical_clearance_becomes_confirmed_only_when_its_own_premises_hold(tmp_path):
    service, _ = project(tmp_path)
    router = ApiRouter(_ApiContext(service))
    response = router.post("/api/building-clearance/policy", {
        "horizontal_clearance_m": 0.0, "vertical_clearance_m": 20.0,
        "min_building_height_m": None, "terrain_relief_review_m": None,
        "source": HUMAN_SOURCE, "confirmed": True,
    })
    policy = response.data["building_clearance_policy"]
    assert policy["status"] == "confirmed"
    assert policy["vertical_clearance_m"] == 20.0
    assert policy["source"] == HUMAN_SOURCE
    assert policy["confirmed"] is True
    readiness = service.layered_route_planner_readiness()
    assert readiness["building_clearance_policy"]["status"] == "confirmed"
    assert readiness["building_clearance_policy"]["vertical_clearance_m"] == 20.0


def test_building_vertical_clearance_stays_pending_without_its_own_source(tmp_path):
    service, _ = project(tmp_path)
    service.set_building_clearance_policy({
        "horizontal_clearance_m": 0.0, "vertical_clearance_m": 20.0, "confirmed": True,
    })
    policy = service.building_clearance_policy_snapshot()
    assert policy["status"] == "pending_confirmation"
    assert policy["confirmed"] is False or policy["status"] != "confirmed"
    assert policy["horizontal_clearance_m"] == 0.0
    assert policy["vertical_clearance_m"] == 20.0


def test_saving_the_feasibility_policy_never_touches_the_building_clearance_policy(tmp_path):
    service, _ = project(tmp_path)
    before = deepcopy(service.building_clearance_policy_snapshot())
    service.set_layered_route_feasibility_policy(human_payload())
    after = service.building_clearance_policy_snapshot()
    assert after == before, "feasibility policy 与 building clearance policy 必须彼此独立"


# --------------------------------------------------------------------------------------
# 5. selected-layer mask：保存后必须可计算，并自报所属高度层
# --------------------------------------------------------------------------------------


def test_the_selected_layer_mask_is_computable_and_reports_its_altitude_layer(tmp_path):
    service, grid = project(tmp_path)
    service.set_layered_route_cost_policy({
        "ground_lambda": 0.0, "air_traffic_lambda": 0.0,
        "environment_obstacle_lambda": 0.0, "source": "工程确认-测试", "confirmed": True,
    })
    service.set_layered_route_planning_request({
        "scenario_route_id": "R0001", "altitude_layer_id": LAYER_ID,
        "source": "工程确认-测试", "confirmed": True,
    })
    service.state["grid_risk_v2"] = risk_v2(grid)
    service.set_layered_route_feasibility_policy(human_payload())
    service.evaluate_layered_route_candidate(
        {}, adapter=StubAdapter(stub_facts(grid)),
    )
    # Phase4-B5X：通用快照只带候选摘要，逐 cell 的 mask 走专用读取接口。
    collection = service.layered_route_candidates()
    assert collection["masks"], "confirmed feasibility policy 之后 selected-layer mask 必须可计算"
    readiness = service.layered_route_planner_readiness()
    assert readiness["feasibility_mask"]["status"] != "not_calculated"
    assert readiness["feasibility_mask"]["altitude_layer_id"] == LAYER_ID


def test_the_selected_layer_mask_reports_no_layer_before_any_evaluation(tmp_path):
    service, _ = project(tmp_path)
    readiness = service.layered_route_planner_readiness()
    assert readiness["feasibility_mask"]["status"] == "not_calculated"
    assert readiness["feasibility_mask"]["altitude_layer_id"] is None


# --------------------------------------------------------------------------------------
# API context stub
# --------------------------------------------------------------------------------------


class _ApiContext:
    def __init__(self, workflow):
        self.workflow = workflow
        self.data = object()

    @property
    def qgis(self):
        raise AssertionError("此测试路径不得访问 QGIS 边界")
