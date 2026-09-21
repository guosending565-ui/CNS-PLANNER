"""网格层级能力声明（Phase 3.5，只声明不改变行为）。

覆盖：
* ``BuildingGridService.capabilities()`` 声明 ``available_levels`` / ``preferred_level``，
  并在工作区层级不可用时给出明确原因；
* **L8 行为完全不变**：level 8 正常映射、其它层级仍 ``unsupported`` + 逐格
  ``missing_data``；
* ``WorkspaceGridService.capabilities()`` 声明可用层级、默认层级与 coarsen 语义；
* layered planner readiness 暴露 planner / building_grid / workspace_grid 三份能力声明。
"""

from __future__ import annotations

import pytest

from cns_planner.algorithms.grid.service import WorkspaceGridService
from cns_planner.data.mapping.buildings import BuildingGridService


def test_building_grid_declares_l8_as_the_only_available_level():
    capability = BuildingGridService.capabilities()
    assert capability["available_levels"] == [8]
    assert capability["preferred_level"] == 8
    assert capability["declared_level_usable"] is True
    assert capability["cross_level_aggregation_allowed"] is False
    assert capability["cross_level_interpolation_allowed"] is False
    assert capability["future_work"] == "level_independent_building_fact_acquisition"


def test_building_grid_declares_the_unusable_reason_for_other_levels():
    capability = BuildingGridService.capabilities(declared_level=6)
    assert capability["declared_level_usable"] is False
    assert capability["unusable_reason"] == "unsupported_grid_level"
    assert "L8" in capability["unusable_message"]
    assert capability["workspace_level_alignment"] == "not_aligned_workspace_default_is_7"


def test_building_grid_l8_mapping_behaviour_is_unchanged(tmp_path):
    grid = {
        "level": 8,
        "workspace_bbox": [122.0, 30.0, 122.1, 30.1],
        "cells": [{
            "grid_id": "MHT4063-L8-C1-RP1", "level": 8,
            "bbox": [122.0, 30.0, 122.01, 30.01],
        }],
    }
    # No configured source is still ``missing_data`` (never a silent zero), exactly as before.
    result = BuildingGridService().map(grid, None)
    assert result["status"] == "missing_data"
    assert result["grid_level"] == 8
    assert result["cells"]["MHT4063-L8-C1-RP1"]["status"] == "missing_data"


def test_building_grid_non_l8_mapping_behaviour_is_unchanged():
    grid = {
        "level": 7,
        "workspace_bbox": [122.0, 30.0, 122.1, 30.1],
        "cells": [{
            "grid_id": "MHT4063-L7-C1-RP1", "level": 7,
            "bbox": [122.0, 30.0, 122.02, 30.02],
        }],
    }
    result = BuildingGridService().map(grid, "unused.gpkg")
    assert result["status"] == "unsupported"
    assert result["grid_level"] == 7
    assert result["cells"]["MHT4063-L7-C1-RP1"]["reason"] == "unsupported_grid_level"
    assert result["cells"]["MHT4063-L7-C1-RP1"]["building_count"] is None
    assert "禁止跨层级平均或插值" in result["message"]


def test_workspace_grid_declares_levels_and_coarsening_semantics():
    capability = WorkspaceGridService().capabilities()
    assert capability["available_levels"] == list(range(1, 17))
    assert capability["preferred_level"] == 7
    assert capability["coarsening"] == "silent_step_down_until_cell_count_within_max_cells"
    assert capability["coarsened_flag_key"] == "coarsened"
    assert capability["explicit_level_request_supported"] is True


def test_workspace_grid_capability_accepts_an_explicit_level_and_ceiling():
    capability = WorkspaceGridService().capabilities(preferred_level=8, max_cells=12000)
    assert capability["preferred_level"] == 8
    assert capability["instance_preferred_level"] == 7
    assert capability["max_cells"] == 12000


def test_workspace_grid_capability_rejects_an_invalid_level():
    with pytest.raises(ValueError):
        WorkspaceGridService().capabilities(preferred_level=0)


def test_empty_workspace_grid_result_carries_the_capability_declaration():
    empty = WorkspaceGridService(preferred_level=8, max_cells=100).empty()
    assert empty["capabilities"]["available_levels"] == list(range(1, 17))
    assert empty["capabilities"]["preferred_level"] == 8
    assert empty["capabilities"]["max_cells"] == 100


def test_planner_module_declares_its_level_binding():
    from cns_planner.layered_route_planner.planner import PLANNER_CAPABILITY as V1
    from cns_planner.layered_route_planner.theta_star_v2 import (
        PLANNER_CAPABILITY as V2,
    )

    for capability in (V1, V2):
        assert capability["available_levels"] == [8]
        assert capability["preferred_level"] == 8
        assert capability["building_fact_level"] == 8
        assert capability["level_binding"] == "current_workspace_grid_level_used_as_is"
        assert capability["cross_level_aggregation_allowed"] is False
