"""工作区网格「层级上限」显式参数的回归测试。

背景：`WorkspaceGridService` 会从请求层级逐级下降，直到格子数 ≤ 上限。舟山案例走廊
在 L7 是 1080 格、L8 是 9720 格，默认上限 5000 会把 L8 静默 coarsen 掉；而 L8 是
building_grid 事实唯一可映射的层级，coarsen 之后建筑事实整表不可用（feasibility mask
里 feasible=0，规划在语义上不可能成立）。

这些测试锁定：默认行为完全不变；显式 `max_cells` 能真正选到 L8；环境变量只改默认值。
"""

from __future__ import annotations

import pytest

from cns_planner.algorithms.grid.service import (
    DEFAULT_MAX_CELLS, MAX_CELLS_ENV, WorkspaceGridService,
)

#: 舟山「桃花岛 → 函景湾」走廊。
CORRIDOR = [122.26, 29.82, 122.33, 29.97]


def test_default_ceiling_is_unchanged():
    service = WorkspaceGridService()
    assert service.max_cells == DEFAULT_MAX_CELLS == 5000

    grid = service.generate(CORRIDOR, 8)
    # The historic behaviour: L8 (8505 cells) is silently coarsened to L7.
    assert grid["level"] == 7
    assert grid["count"] == 945
    assert grid["coarsened"] is True
    assert grid["max_cells"] == 5000


def test_explicit_max_cells_selects_l8_instead_of_coarsening():
    service = WorkspaceGridService()
    grid = service.generate(CORRIDOR, 8, 12000)
    assert grid["level"] == 8
    assert grid["count"] == 8505
    assert grid["coarsened"] is False
    assert grid["max_cells"] == 12000

    # The caller's ceiling only applies to this request.
    assert service.max_cells == DEFAULT_MAX_CELLS
    assert service.generate(CORRIDOR, 8)["level"] == 7


def test_explicit_max_cells_can_also_lower_the_ceiling():
    grid = WorkspaceGridService().generate(CORRIDOR, 8, 100)
    assert grid["count"] <= 100
    assert grid["level"] < 7
    assert grid["max_cells"] == 100


def test_invalid_max_cells_is_rejected():
    with pytest.raises(ValueError):
        WorkspaceGridService(max_cells=0)
    with pytest.raises(ValueError):
        WorkspaceGridService(max_cells="not-a-number")
    with pytest.raises(ValueError):
        WorkspaceGridService().generate(CORRIDOR, 8, -1)


def test_environment_variable_only_changes_the_default(monkeypatch):
    monkeypatch.setenv(MAX_CELLS_ENV, "12000")
    assert WorkspaceGridService().max_cells == 12000
    assert WorkspaceGridService().generate(CORRIDOR, 8)["level"] == 8
    # A default that cannot honour any level still raises instead of guessing.
    monkeypatch.setenv(MAX_CELLS_ENV, "0")
    with pytest.raises(ValueError):
        WorkspaceGridService()
