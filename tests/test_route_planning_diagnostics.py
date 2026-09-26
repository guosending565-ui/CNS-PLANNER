"""Route 质量/诊断指标的中立行为测试。

B8X：原文件同时覆盖 ``tools/route_planning_baseline.py`` 驱动的 legacy 专家诊断与
``tools/route_planning_diagnostics.py`` / ``tools/route_planning_expert_brief.py``。
随 RoutePlannerV1 / RiskAwareRoutePlannerV2 按 delete gate 物理删除，这三个工具（专家
证据包、λ/grid 敏感性、专家简报）已没有可运行的 planner 目标，一并移除；其对应
characterization 测试随之删除，而不是通过改断言继续维持 legacy 执行路径。

保留下来的测试只覆盖中立指标模块 ``cns_planner.benchmark.quality`` 与合成用例目录
``cns_planner.benchmark.fixtures``：它们不实例化任何算法，也不写入任何状态。
"""

import json

import pytest

from cns_planner.benchmark import fixtures
from cns_planner.benchmark.quality import (
    RoutePlanningDiagnostics, grid_path_metrics, polyline_metrics,
)


def test_zigzag_index_has_explicit_normalized_definition():
    straight = polyline_metrics([[0, 0], [0.001, 0], [0.002, 0]])
    zigzag = polyline_metrics([[0, 0], [0.001, 0], [0.001, 0.001], [0.002, 0.001]])
    assert straight["zigzag_index"] == pytest.approx(0)
    assert zigzag["zigzag_index"] > 0
    assert "sum_absolute_heading_change_deg" in zigzag["zigzag_index_definition"]


def test_direction_histogram_counts_all_grid_step_classes():
    path = [
        "MHT4063-L07-C00000010-RP00000010",
        "MHT4063-L07-C00000011-RP00000010",  # E
        "MHT4063-L07-C00000011-RP00000011",  # N
        "MHT4063-L07-C00000010-RP00000010",  # SW
    ]
    result = grid_path_metrics(path)
    assert result["horizontal_step_count"] == 1
    assert result["vertical_step_count"] == 1
    assert result["diagonal_step_count"] == 1
    assert result["direction_histogram"]["E"] == 1
    assert result["direction_histogram"]["N"] == 1
    assert result["direction_histogram"]["SW"] == 1
    assert result["grid_level"] == 7


def test_route_planning_diagnostics_is_read_only_and_descriptive():
    result = {"route_id": "R1", "status": "passed", "path": [[0, 0], [0.01, 0]]}
    before = json.loads(json.dumps(result))
    diagnostics = RoutePlanningDiagnostics.evaluate({"route_id": "R1"}, result, [])
    assert result == before
    assert diagnostics["verdicts"]["preferred_algorithm"] is None
    assert diagnostics["constraint_input_summary"]["hard_constraint_count"] == 0


def test_new_cases_expose_grid_bias_and_bbox_expression_difference():
    zigzag = fixtures.case("zigzag_open_grid_bias")
    bbox = fixtures.case("bbox_overblocking_demo")
    assert "格网" in zigzag["description"]
    assert bbox["hard_constraints"][0]["bbox"]
    assert bbox["demonstration"]["hypothetical_polygon_not_consumed_by_planner"]
    assert bbox["demonstration"]["claim_limit"] == "does_not_assert_polygon_is_the_final_solution"
