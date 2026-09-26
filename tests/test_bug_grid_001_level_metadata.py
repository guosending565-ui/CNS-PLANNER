"""BUG-GRID-001：格网几何只有一个权威来源，且米制尺寸必须与真实 cell 边长自洽。

修复内容（只 additive，不改变 MH/T 4063.1 的层级定义）：

* ``WorkspaceGridService.generate()`` 现在同时返回 ``level_metadata``：
  ``{level, cell_size_m, resolution_x, resolution_y, cell_size_degrees, measurement, source}``；
* 每个 grid cell 也带上同一来源的 ``cell_size_m`` / ``resolution_x`` / ``resolution_y``；
* ``level_metadata`` **始终描述实际生成（coarsen 之后）的层级**，绝不描述被请求的层级；
* metadata 由 ``LEVEL_SIZE_DEGREES``（唯一权威定义）换算而来，前端不得自行按经纬度估算。

注意：MH/T 4063.1 的层级是**度意义上的正方形**，不是米制正方形 —— 经向边长比纬向短
``cos(lat)``。因此 ``resolution_x`` 与 ``resolution_y`` 本来就不相等，``cell_size_m`` 取
面积等效边长 ``sqrt(resolution_x * resolution_y)``。
"""

from __future__ import annotations

import math

import pytest

from cns_planner.domain.geodesy import distance_m
from cns_planner.algorithms.grid.mht4063 import LEVEL_SIZE_DEGREES
from cns_planner.algorithms.grid.service import WorkspaceGridService

#: 舟山工作区附近的真实纬度。
ZHOUSHAN_BBOX = [122.268, 29.835, 122.400, 29.955]
#: 一个足够小、可以停在 L6/L7/L8 的工作区（避免被 max_cells 静默 coarsen）。
SMALL_BBOX = [122.290, 29.900, 122.310, 29.910]


def grid_for(level, bbox=SMALL_BBOX, max_cells=20000):
    return WorkspaceGridService(preferred_level=level, max_cells=max_cells).generate(bbox)


def cell_edge_lengths(cell):
    """从 cell 自己的 bbox 量出真实米制边长（东西 / 南北）。"""

    west, south, east, north = cell["bbox"]
    mid_lat = (south + north) / 2.0
    return (
        distance_m([west, mid_lat], [east, mid_lat]),
        distance_m([(west + east) / 2.0, south], [(west + east) / 2.0, north]),
    )


# ---------------------------------------------------------------------------------------
# 1. metadata 必须存在、完整，并且描述真实层级
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("level", [6, 7, 8])
def test_level_metadata_is_present_complete_and_describes_the_generated_level(level):
    grid = grid_for(level)
    assert grid["status"] == "passed"
    assert grid["level"] == level
    assert grid["coarsened"] is False

    metadata = grid["level_metadata"]
    assert isinstance(metadata, dict), "generate() 必须返回 level_metadata"
    assert set(metadata) >= {
        "level", "cell_size_m", "resolution_x", "resolution_y",
        "cell_size_degrees", "reference_latitude_deg", "measurement", "source",
    }
    assert metadata["level"] == grid["level"]
    assert metadata["unit"] == "m"
    assert metadata["cell_size_m"] > 0
    assert metadata["resolution_x"] > 0
    assert metadata["resolution_y"] > 0
    assert metadata["cell_size_m"] == pytest.approx(
        math.sqrt(metadata["resolution_x"] * metadata["resolution_y"]), rel=1e-9
    )
    assert metadata["cell_size_degrees"] == pytest.approx(
        [float(LEVEL_SIZE_DEGREES[level][0]), float(LEVEL_SIZE_DEGREES[level][1])]
    )
    assert grid["cell_size_degrees"] == metadata["cell_size_degrees"]
    assert metadata["never_estimated_on_the_frontend"] is True


@pytest.mark.parametrize("level", [6, 7, 8])
def test_actual_cell_edges_match_the_metadata_within_ten_percent(level):
    grid = grid_for(level)
    metadata = grid["level_metadata"]
    for cell in grid["cells"]:
        edge_x, edge_y = cell_edge_lengths(cell)
        assert edge_x == pytest.approx(metadata["resolution_x"], rel=0.10), cell["grid_id"]
        assert edge_y == pytest.approx(metadata["resolution_y"], rel=0.10), cell["grid_id"]
        # 面积等效边长也必须落在 10% 以内。
        assert math.sqrt(edge_x * edge_y) == pytest.approx(metadata["cell_size_m"], rel=0.10)
        # 逐格 metadata 与顶层 metadata 同源（同一层级、同一换算）。
        assert cell["resolution_x"] == pytest.approx(metadata["resolution_x"], rel=0.02)
        assert cell["resolution_y"] == pytest.approx(metadata["resolution_y"], rel=0.02)
        assert cell["cell_size_m"] == pytest.approx(
            math.sqrt(cell["resolution_x"] * cell["resolution_y"]), rel=1e-9
        )


def test_level_sizes_are_strictly_ordered_and_match_the_degree_table():
    sizes = {}
    for level in (6, 7, 8):
        metadata = grid_for(level)["level_metadata"]
        sizes[level] = metadata["resolution_y"]
        lon_span, lat_span = LEVEL_SIZE_DEGREES[level]
        assert metadata["resolution_y"] == pytest.approx(
            math.radians(float(lat_span)) * 6371008.8, rel=1e-9
        )
        assert metadata["resolution_x"] == pytest.approx(
            math.radians(float(lon_span)) * 6371008.8
            * math.cos(math.radians(metadata["reference_latitude_deg"])),
            rel=1e-9,
        )
    # L6 一定比 L7 粗，L7 一定比 L8 粗：metadata 不得颠倒层级语义。
    assert sizes[6] > sizes[7] > sizes[8]


def test_a_coarsened_workspace_reports_the_level_it_really_generated():
    # 舟山全域工作区在默认 5000 格上限下会从 L8 被 coarsen 到更粗的层级。
    grid = WorkspaceGridService(preferred_level=8, max_cells=5000).generate(ZHOUSHAN_BBOX)
    assert grid["coarsened"] is True
    assert grid["level"] < 8
    assert grid["level_metadata"]["level"] == grid["level"], (
        "metadata 必须描述实际生成的层级，而不是被请求的层级"
    )
    assert grid["level_metadata"]["cell_size_degrees"] == pytest.approx(
        [float(LEVEL_SIZE_DEGREES[grid["level"]][0]),
         float(LEVEL_SIZE_DEGREES[grid["level"]][1])]
    )
    for cell in grid["cells"]:
        edge_x, edge_y = cell_edge_lengths(cell)
        assert edge_x == pytest.approx(grid["level_metadata"]["resolution_x"], rel=0.10)
        assert edge_y == pytest.approx(grid["level_metadata"]["resolution_y"], rel=0.10)


def test_empty_grid_declares_no_metadata_instead_of_a_guessed_size():
    empty = WorkspaceGridService(preferred_level=7).empty()
    assert empty["level"] is None
    assert empty["level_metadata"] is None
    assert empty["cell_size_degrees"] is None
    capabilities = empty["capabilities"]
    assert capabilities["level_metadata_key"] == "level_metadata"
    assert capabilities["frontend_must_not_estimate_cell_size"] is True


def test_frontend_never_estimates_a_grid_level_or_cell_size():
    """格网渲染/显示模块只能消费后端 geometry + metadata。

    只审计真正负责格网显示与渲染的模块：其他前端模块（建筑足迹、DAA 实验室、V3 corridor）
    有自己的合法米制换算，与格网层级尺寸无关。
    """

    from pathlib import Path

    root = Path(__file__).parents[1] / "cns_planner" / "web" / "js"
    grid_modules = (
        "map/grid_overlay.js",
        "map/renderer.js",
        "workflow/step02_workspace.js",
        "workflow/grid_details.js",
    )
    offenders = []
    for name in grid_modules:
        path = root / name
        assert path.exists(), name
        text = path.read_text(encoding="utf-8")
        for needle in ("111320", "111.32", "Math.cos(lat", "cos(lat"):
            if needle in text:
                offenders.append((name, needle))
        # 不得从度数反推层级（例如 level = Math.round(Math.log2(...))）。
        for token in ("estimateGridLevel", "levelFromDegrees", "levelFromResolution"):
            if token in text:
                offenders.append((name, token))
    assert not offenders, f"格网模块不得自行按经纬度估算层级或尺寸：{offenders}"

    # 正向：Step02 的格网尺寸行确实消费后端 metadata。
    step02 = (root / "workflow" / "step02_workspace.js").read_text(encoding="utf-8")
    assert "grid.level_metadata" in step02
    assert "resolution_x" in step02 and "resolution_y" in step02
    # 格网绘制仍然只使用后端 geometry（cell.bbox），不做前端网格重建。
    overlay = (root / "map" / "grid_overlay.js").read_text(encoding="utf-8")
    assert "item.cell.bbox" in overlay
