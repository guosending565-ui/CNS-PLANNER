"""GRID-L8-UNIFICATION 回归测试：正式业务空间索引统一到 MH/T 4063.1 L8。

**背景（真实缺陷）**：正式业务前端选择 L8，但 ``WorkspaceGridService`` 的默认上限是
5000 格。舟山规模工作区在 L8 下需要 6100 格 > 5000，于是被**静默 coarsen** 到 L7；
而 ``BuildingGridService`` 只能直接映射 L8 事实表 ⇒ 建筑事实整表不可用
（``unsupported_grid_level``），Theta* 在起点 connector 阶段直接
``expanded_labels = 0 / rejected_unknown = 1 / no_path``。

**本轮收口**：

* 定义正式业务 canonical operational grid level = **8**；
* 正式工作流默认 cell ceiling：5000 → **12000**（只是软件资源保护阈值）；
* 正式入口（``generate_operational`` / ``WorkspaceService`` / 正式 API）请求 L8 时
  **禁止 silent coarsening**：超限即 ``blocked``，绝不生成 L7/L6 后继续业务流程，
  也绝不返回伪 passed；
* 底层 ``generate()`` 的通用多层级与 coarsening 能力**保留**（legacy / unit test /
  diagnostic），不粗暴删除 L6/L7 算法。

**没有修改**：MH/T 编码算法、Theta* V2 搜索算法、population×shelter 数学、
Risk Framework V2 数学、TowerObstacle fail-closed、BuildingClearance 数学、
RouteRiskProfile、REUSE_TIERS。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cns_planner.algorithms.grid.mht4063 import LEVEL_SIZE_DEGREES
from cns_planner.algorithms.grid.service import (
    DEFAULT_MAX_CELLS,
    OPERATIONAL_GRID_BLOCKED_CODE,
    OPERATIONAL_GRID_LEVEL,
    OPERATIONAL_GRID_SEMANTICS,
    OperationalGridBlockedError,
    WorkspaceGridService,
    resolve_operational_level,
)
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.data.mapping.buildings import BuildingGridService

ROOT = Path(__file__).parents[1]
DEFAULTS = ROOT / "cns_planner" / "config" / "defaults.json"

#: 舟山走廊规模工作区：L7 = 714 格、L8 = **6100** 格。
#: 它恰好落在"旧默认 5000"与"正式 12000"之间 —— 即缺陷的最小复现区间。
CORRIDOR = [122.268, 29.835, 122.3347, 29.945]
#: 舟山全域工作区：L8 需要 12971 格，超出正式资源上限 ⇒ 必须明确阻断。
OVERSIZED = [122.268, 29.835, 122.400, 29.955]


def _health():
    return {
        "status": "passed", "population": {"status": "passed"},
        "airspace": {"status": "passed"}, "terrain": {"status": "passed"},
        "buildings": {"status": "missing_data"}, "property": {"status": "missing_data"},
        "loaded_layer_count": 3, "covered_layer_count": 3,
    }


def _workflow(tmp_path):
    defaults = tmp_path / "config" / "defaults.json"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(DEFAULTS.read_text(encoding="utf-8"), encoding="utf-8")
    store = tmp_path / "project_state.json"
    return WorkflowService(store, defaults), store


# ------------------------------------------------------------------ 1) canonical 层级与上限


def test_canonical_operational_level_is_l8():
    assert OPERATIONAL_GRID_LEVEL == 8
    assert OPERATIONAL_GRID_SEMANTICS == "strict_canonical_level_no_silent_coarsening"
    assert WorkspaceGridService().preferred_level == OPERATIONAL_GRID_LEVEL

    capability = WorkspaceGridService().capabilities()
    assert capability["canonical_operational_level"] == 8
    assert capability["operational_level"] == 8
    assert capability["operational_semantics"] == OPERATIONAL_GRID_SEMANTICS
    assert capability["operational_silent_coarsening_allowed"] is False
    assert capability["operational_blocked_on_ceiling"] is True
    assert capability["operational_blocked_code"] == OPERATIONAL_GRID_BLOCKED_CODE
    # 12000 只是软件资源保护阈值，不是空间工程参数。
    assert capability["max_cells"] == DEFAULT_MAX_CELLS == 12000
    assert capability["max_cells_is_resource_guard_only"] is True
    assert capability["max_cells_is_spatial_parameter"] is False
    # L6/L7 的底层算法仍然保留（只是不再是正式降级目标）。
    assert capability["available_levels"] == list(range(1, 17))
    assert capability["coarsening_scope"] == "legacy_generate_only"


def test_resolve_operational_level_only_accepts_canonical_l8():
    assert resolve_operational_level(None) == 8
    assert resolve_operational_level(8) == 8
    assert resolve_operational_level("8") == 8
    assert resolve_operational_level("") == 8
    for legacy in (7, 6, "7", "6"):
        with pytest.raises(ValueError) as failure:
            resolve_operational_level(legacy)
        assert "L8" in str(failure.value)
    with pytest.raises(ValueError):
        resolve_operational_level("not-a-level")


# ------------------------------------------------------------------ 2) 正式入口禁止 silent coarsen


def test_legacy_generate_would_silently_coarsen_the_corridor_workspace():
    """缺陷复现：旧默认上限 5000 会把 L8 静默降级到 L7。"""

    grid = WorkspaceGridService(max_cells=5000).generate(CORRIDOR, 8)
    assert grid["level"] == 7
    assert grid["coarsened"] is True
    assert grid["preferred_level"] == 8


def test_operational_entry_generates_the_actual_l8_workspace():
    grid = WorkspaceGridService().generate_operational(CORRIDOR)

    assert grid["status"] == "passed"
    assert grid["level"] == 8
    assert grid["canonical_level"] == 8
    assert grid["preferred_level"] == 8
    assert grid["coarsened"] is False
    assert grid["coarsening_allowed"] is False
    assert grid["max_cells"] == 12000
    assert grid["count"] == 6100
    assert grid["blocked"] is False
    assert len(grid["cells"]) == grid["count"]
    assert all(cell["level"] == 8 for cell in grid["cells"])


def test_level_metadata_describes_the_actual_l8_grid():
    grid = WorkspaceGridService().generate_operational(CORRIDOR)
    metadata = grid["level_metadata"]

    assert metadata["level"] == grid["level"] == 8
    assert metadata["cell_size_degrees"] == pytest.approx(
        [float(LEVEL_SIZE_DEGREES[8][0]), float(LEVEL_SIZE_DEGREES[8][1])]
    )
    assert metadata["resolution_y"] == pytest.approx(
        math.radians(float(LEVEL_SIZE_DEGREES[8][1])) * 6371008.8, rel=1e-9
    )
    assert metadata["resolution_x"] < metadata["resolution_y"], (
        "L8 是度意义上的正方形：经向边长必须比纬向短 cos(lat)"
    )
    for cell in grid["cells"][:50]:
        assert cell["resolution_x"] == pytest.approx(metadata["resolution_x"], rel=0.05)
        assert cell["resolution_y"] == pytest.approx(metadata["resolution_y"], rel=0.05)


def test_operational_entry_blocks_instead_of_coarsening_beyond_the_ceiling():
    result = WorkspaceGridService().generate_operational(OVERSIZED)

    assert result["status"] == "blocked"
    assert result["blocked"] is True
    assert result["blocked_code"] == OPERATIONAL_GRID_BLOCKED_CODE
    # 绝不生成任何 L7/L6 网格，也绝不返回伪 passed。
    assert result["level"] is None
    assert result["coarsened"] is False
    assert result["cells"] == []
    assert result["count"] == 0
    assert result["level_metadata"] is None
    assert result["required_cells"] > result["max_cells"] == 12000
    # canonical 意图仍然如实声明：用户看到的是"L8 装不下"，而不是"系统改用了 L7"。
    assert result["canonical_level"] == 8
    assert result["preferred_level"] == 8

    error = result["error"]
    assert error["code"] == OPERATIONAL_GRID_BLOCKED_CODE
    assert error["level"] == 8
    assert error["coarsened"] is False
    assert error["fallback_level_generated"] is None
    assert error["required_cells"] == result["required_cells"]
    assert error["max_cells"] == 12000
    for keyword in ("L7", "L6", "缩小工作区", "max_cells"):
        assert keyword in error["message"]
    assert len(error["suggestions"]) == 2


def test_operational_entry_respects_an_explicitly_raised_ceiling():
    """显式提高 max_cells 只改软件资源上限，不改变任何空间语义。"""

    result = WorkspaceGridService().generate_operational(OVERSIZED, max_cells=20000)
    assert result["status"] == "passed"
    assert result["level"] == 8
    assert result["coarsened"] is False
    assert result["max_cells"] == 20000
    assert result["count"] == 12971


def test_legacy_generate_still_supports_multi_level_coarsening():
    """底层通用算法（legacy / unit test / diagnostic）不被删除。"""

    service = WorkspaceGridService(preferred_level=7, max_cells=10)
    result = service.generate([120.0, 30.0, 120.1, 30.1])
    assert result["preferred_level"] == 7
    assert result["level"] == 5
    assert result["coarsened"] is True
    assert result["count"] == 4


# ------------------------------------------------------------------ 3) 正式 WorkspaceService


def test_workspace_service_produces_the_l8_snapshot(tmp_path):
    service, _store = _workflow(tmp_path)
    state = service.set_workspace(CORRIDOR, _health())

    grid = state["grid"]
    assert grid["status"] == "passed"
    assert grid["level"] == 8
    assert grid["preferred_level"] == 8
    assert grid["canonical_level"] == 8
    assert grid["coarsened"] is False
    assert grid["max_cells"] == 12000
    assert grid["level_metadata"]["level"] == 8
    assert state["result_statuses"]["grid"] == "passed"
    assert state["result_statuses"]["workspace"] == "passed"
    assert state["workspace"]["status"] == "passed"
    assert state["steps"]["2"] is True


def test_workspace_service_blocks_an_oversized_workspace_without_falling_back(tmp_path):
    service, store = _workflow(tmp_path)
    with pytest.raises(OperationalGridBlockedError) as failure:
        service.set_workspace(OVERSIZED, _health())

    assert "L8" in str(failure.value)
    assert failure.value.details["code"] == OPERATIONAL_GRID_BLOCKED_CODE

    state = service.state
    # 明确 blocked 的持久状态：没有 L7/L6 网格，没有伪 passed。
    assert state["grid"]["status"] == "blocked"
    assert state["grid"]["level"] is None
    assert state["grid"]["cells"] == []
    assert state["grid"]["coarsened"] is False
    assert state["grid"]["required_cells"] > state["grid"]["max_cells"]
    assert state["result_statuses"]["grid"] == "blocked"
    assert state["result_statuses"]["workspace"] == "blocked"
    assert state["workspace"]["status"] == "blocked"
    assert state["workspace"]["blocked_reason"]
    assert state["workspace"]["required_cells"] == state["grid"]["required_cells"]
    # 正式业务流程不得越过这一步继续（第 2 步未通过）。
    assert service.snapshot()["steps"]["2"] is False
    # 阻断状态如实落库：重新打开项目仍然看得到。
    reopened = WorkflowService(store, DEFAULTS)
    assert reopened.state["grid"]["status"] == "blocked"
    assert reopened.state["result_statuses"]["grid"] == "blocked"


def test_workspace_service_rejects_a_legacy_level_selection(tmp_path):
    service, _store = _workflow(tmp_path)
    before = service.state.get("grid")
    with pytest.raises(ValueError) as failure:
        service.set_workspace(CORRIDOR, _health(), 7)
    assert "L8" in str(failure.value)
    # 参数校验发生在写入任何状态之前：既不静默改成 L8，也不留下半成品状态。
    assert service.state.get("grid") == before
    assert service.state.get("workspace") is None


# ------------------------------------------------------------------ 4) 下游以 L8 为 canonical


def test_building_mapping_on_the_operational_grid_never_reports_unsupported():
    grid = WorkspaceGridService().generate_operational(CORRIDOR)

    # (a) 没有配置建筑事实源：如实 missing_data，绝不出现 unsupported_grid_level。
    mapped = BuildingGridService().map(grid, None)
    assert mapped["grid_level"] == 8
    assert mapped["status"] == "missing_data"
    assert all(
        cell["reason"] != "unsupported_grid_level" for cell in mapped["cells"].values()
    )
    assert mapped["environment_mapping"]["grid_level"] == 8
    assert mapped["environment_mapping"]["level_aligned"] is True

    # (b) 正式 L8 + 可用的原始足迹聚合：建筑事实真正可用（不再整表 unsupported）。
    def aggregator(cells, source, grid_level=None, layer_name=None):
        return {
            "status": "passed", "grid_level": grid_level, "count": len(cells),
            "covered_count": len(cells),
            "metadata": {"aggregation_method": "exact_footprint_intersection_and_centroid_allocation"},
            "cells": {
                cell["grid_id"]: {
                    "status": "passed", "building_count": 1, "building_area_m2": 100.0,
                    "building_coverage_ratio": 0.2, "height_mean_m": 10.0,
                    "height_p95_m": 12.0, "height_max_m": 15.0,
                    "valid_height_fraction": 1.0, "building_exposure": 0.2,
                }
                for cell in cells
            },
        }

    mapped = BuildingGridService().map(
        grid, None, footprint_source="zhoushan_buildings.gpkg",
        footprint_aggregator=aggregator,
    )
    environment = mapped["environment_mapping"]
    assert environment["status"] == "passed"
    assert environment["level_aligned"] is True
    assert environment["participates_in_planner"] is True
    assert environment["unresolved_reasons"] == {}


def test_planner_capability_is_bound_to_the_l8_grid(tmp_path):
    service, _store = _workflow(tmp_path)
    service.set_workspace(CORRIDOR, _health())
    capabilities = service.layered_route_planner_readiness()["capabilities"]

    assert capabilities["building_grid"]["declared_level_usable"] is True
    assert capabilities["building_grid"]["unusable_reason"] is None
    assert capabilities["workspace_grid"]["canonical_operational_level"] == 8
    assert capabilities["workspace_grid"]["max_cells"] == 12000
    assert capabilities["planner"]["preferred_level"] == 8
    assert capabilities["planner"]["building_fact_level"] == 8


def test_downstream_mappings_all_consume_the_same_l8_index():
    """人口 / 地形 / 建筑 / 交通 / 冲突映射都以**同一个实际 L8** 网格为空间索引。"""

    from cns_planner.data.mapping.conflict import ConflictGridService
    from cns_planner.data.mapping.population import PopulationGridService
    from cns_planner.data.mapping.terrain import TerrainGridService
    from cns_planner.data.mapping.traffic import TrafficGridService

    grid = WorkspaceGridService().generate_operational(CORRIDOR)
    cell_ids = {cell["grid_id"] for cell in grid["cells"]}

    results = {
        "population": PopulationGridService().map(grid, None),
        "terrain": TerrainGridService().map(grid, None),
        "buildings": BuildingGridService().map(grid, None),
        "traffic": TrafficGridService().map(grid, {"simulation_seconds": 60.0}),
        "conflict": ConflictGridService().map(grid, None, 60.0),
    }
    for role, result in results.items():
        assert result["grid_level"] == 8, f"{role} 必须消费实际 L8 网格"
        # 无源时保持 missing_data（unknown ≠ 0）；关键是**不是** unsupported_grid_level。
        if role == "buildings":
            assert all(
                cell["reason"] != "unsupported_grid_level"
                for cell in result["cells"].values()
            )
        # 网格键集合与实际 L8 网格逐格一致（没有第二套空间索引）。
        assert set(result["cells"]) <= cell_ids or not result["cells"]


# ------------------------------------------------------------------ 5) 旧项目迁移行为


def test_a_legacy_coarsened_project_upgrades_only_when_the_workspace_is_resaved(tmp_path):
    service, store = _workflow(tmp_path)
    service.set_workspace(CORRIDOR, _health())

    # 人为退回一个 legacy L7 快照（模拟本轮之前生成并已保存的旧项目）。
    legacy = WorkspaceGridService(preferred_level=8, max_cells=5000).generate(CORRIDOR, 8)
    service.state["grid"] = {
        "status": "passed", "level": legacy["level"], "count": legacy["count"],
        "workspace_bbox": list(CORRIDOR), "coarsened": True, "cells": legacy["cells"],
    }
    service.state["result_statuses"]["grid"] = "passed"
    service.save()

    reopened = WorkflowService(store, DEFAULTS)
    # 旧快照**不会被静默改写**：加载既有项目不触发网格重新生成。
    assert reopened.state["grid"]["level"] == 7
    assert reopened.state["grid"]["coarsened"] is True

    # 用户重新保存工作区后才升级到 canonical L8；旧 L7 网格被整体替换（不是并存）。
    upgraded = reopened.set_workspace(CORRIDOR, _health())
    assert upgraded["grid"]["level"] == 8
    assert upgraded["grid"]["coarsened"] is False
    assert upgraded["grid"]["count"] == 6100
    assert all(cell["level"] == 8 for cell in reopened.grid_snapshot()["cells"])


# ------------------------------------------------------------------ 6) 前端不再暴露 L6/L7


def test_step02_no_longer_offers_a_grid_level_selector():
    step02 = (ROOT / "cns_planner" / "web" / "js" / "workflow" / "step02_workspace.js").read_text(
        encoding="utf-8"
    )
    main = (ROOT / "cns_planner" / "web" / "js" / "main.js").read_text(encoding="utf-8")

    # 固定展示 canonical L8，而不是下拉选择。
    assert "export const OPERATIONAL_GRID_LEVEL=8" in step02
    assert 'id="workspaceGridLevel"' in step02
    assert "<select id=\"workspaceGridLevel\"" not in step02
    for legacy_label in ("L7（建筑网格 unsupported）", "L6（建筑网格 unsupported）"):
        assert legacy_label not in step02
    # 展示项：canonical level / actual level / 格数 / 资源上限 / 米制边长。
    for token in (
        "canonical level L", "actual level L", "格数 ", "上限 ",
        "resolution_x", "resolution_y", "level_metadata",
    ):
        assert token in step02, token
    # 超限时显示可读错误。
    assert "workspaceGridBlocked" in step02
    assert "已阻断" in step02
    # 提交的层级是常量，不再从任何控件读取。
    assert "grid_level:Step02.OPERATIONAL_GRID_LEVEL" in main
    assert "workspaceGridLevel')?.value" not in main
