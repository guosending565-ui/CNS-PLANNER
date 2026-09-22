"""工作区网格「层级上限」与正式入口 strict-L8 语义的回归测试。

背景（历史）：`WorkspaceGridService.generate()` 会从请求层级逐级下降，直到格子数 ≤ 上限。
舟山案例走廊在 L7 是 945 格、L8 是 8505 格，旧默认上限 5000 会把 L8 **静默 coarsen** 掉；
而 L8 是 building_grid 事实唯一可映射的层级，coarsen 之后建筑事实整表不可用
（feasibility mask 里 feasible=0，规划在语义上不可能成立）。

GRID-L8-UNIFICATION 之后的语义分成两层，本文件同时锁定两层：

* **正式入口**（`generate_operational()` / `WorkspaceService` / 正式 API）：
  恰好 canonical L8，超限即 `blocked`，绝不 silent coarsen（详见
  `tests/test_bug_grid_l8_unification.py`）；
* **底层通用算法**（`generate()`）：仍保留逐级下降的 legacy 能力，供 unit test / diagnostic
  使用；`CNS_GRID_MAX_CELLS` 只改默认值，显式 `max_cells` 参数始终优先。
"""

from __future__ import annotations

import pytest

from cns_planner.algorithms.grid.service import (
    DEFAULT_MAX_CELLS, MAX_CELLS_ENV, OPERATIONAL_GRID_LEVEL, WorkspaceGridService,
)

#: 舟山「桃花岛 → 函景湾」走廊。
CORRIDOR = [122.26, 29.82, 122.33, 29.97]


def test_default_ceiling_is_the_resource_guard():
    service = WorkspaceGridService()
    # 12000 只是软件资源保护阈值，不是空间工程参数。
    assert service.max_cells == DEFAULT_MAX_CELLS == 12000
    assert service.capabilities()["max_cells_is_resource_guard_only"] is True
    assert service.capabilities()["max_cells_is_spatial_parameter"] is False

    # 默认上限已经足够让走廊停在 L8（8505 格），不再被静默 coarsen。
    grid = service.generate(CORRIDOR, 8)
    assert grid["level"] == 8
    assert grid["count"] == 8505
    assert grid["coarsened"] is False
    assert grid["max_cells"] == 12000


def test_default_operational_level_is_canonical_l8():
    service = WorkspaceGridService()
    assert OPERATIONAL_GRID_LEVEL == 8
    assert service.preferred_level == OPERATIONAL_GRID_LEVEL
    assert service.capabilities()["canonical_operational_level"] == 8
    assert service.capabilities()["operational_silent_coarsening_allowed"] is False


def test_explicit_max_cells_selects_l8_instead_of_coarsening():
    service = WorkspaceGridService()
    grid = service.generate(CORRIDOR, 8, 12000)
    assert grid["level"] == 8
    assert grid["count"] == 8505
    assert grid["coarsened"] is False
    assert grid["max_cells"] == 12000

    # The caller's ceiling only applies to this request.
    assert service.max_cells == DEFAULT_MAX_CELLS


def test_explicit_max_cells_can_also_lower_the_ceiling():
    # 底层 legacy 语义：给一个很小的上限时仍会逐级下降（正式入口不会这样做）。
    grid = WorkspaceGridService().generate(CORRIDOR, 8, 100)
    assert grid["count"] <= 100
    assert grid["level"] < 7
    assert grid["max_cells"] == 100
    assert grid["coarsened"] is True


def test_invalid_max_cells_is_rejected():
    with pytest.raises(ValueError):
        WorkspaceGridService(max_cells=0)
    with pytest.raises(ValueError):
        WorkspaceGridService(max_cells="not-a-number")
    with pytest.raises(ValueError):
        WorkspaceGridService().generate(CORRIDOR, 8, -1)


def test_environment_variable_only_changes_the_default(monkeypatch):
    monkeypatch.setenv(MAX_CELLS_ENV, "6000")
    assert WorkspaceGridService().max_cells == 6000
    # 6000 不足以容纳 L8（8505 格）⇒ 底层 generate 会按 legacy 语义下降。
    assert WorkspaceGridService().generate(CORRIDOR, 8)["level"] == 7
    monkeypatch.setenv(MAX_CELLS_ENV, "12000")
    assert WorkspaceGridService().max_cells == 12000
    assert WorkspaceGridService().generate(CORRIDOR, 8)["level"] == 8
    # A default that cannot honour any level still raises instead of guessing.
    monkeypatch.setenv(MAX_CELLS_ENV, "0")
    with pytest.raises(ValueError):
        WorkspaceGridService()
