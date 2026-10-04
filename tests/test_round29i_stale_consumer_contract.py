"""Round 29-I：stale 消费方契约测试。

三条硬约束：

1. **加载项目绝不写 ``result_statuses``**，也绝不自动 save；
2. 任何消费者都**不得**把 raw ``result_statuses`` 直接当作"当前有效"权威 ——
   P16 对上游 currentness 的门禁必须是 fail-closed；
3. 算法语义版本变化时，只读投影必须如实标 stale（绝不误判 current）。

本文件不运行真实权威 P16，也不改写任何权威项目状态。
"""

from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cns_planner.application.corridor_site_planning_service import (  # noqa: E402
    CorridorSitePlanningService,
)
from cns_planner.application.corridor_service import apply_algorithm_semantics_stale  # noqa: E402
from cns_planner.application.workflow_service import WorkflowService  # noqa: E402


DEFAULTS = Path("cns_planner/config/defaults.json")


def _build(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [{
        "route_id": "R1", "status": "passed", "path": [[-0.001, 0.0], [0.001, 0.0]],
    }]
    workflow.state["grid"] = {"status": "passed", "level": 1, "cells": [
        {"grid_id": "G1", "bbox": [-0.0005, 0.0, 0.0005, 0.001]},
    ]}
    workflow.state["grid_attributes"]["terrain"] = {"status": "passed", "cells": {}}
    workflow.state["spatial_3d"] = {
        "altitude_layers": [{
            "altitude_layer_id": "L1", "lower_altitude_m": 50, "upper_altitude_m": 150,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }],
        "route_altitude_profiles": {"R1": {
            "route_id": "R1", "mode": "constant", "constant_altitude_m": 100,
            "vertical_reference": "egm2008_orthometric", "source": "test",
            "confirmed": True, "status": "confirmed",
        }},
        "site_vertical_profiles": {},
    }
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    return workflow


def test_project_load_does_not_write_result_statuses_or_save(tmp_path):
    """加载项目：``result_statuses`` 与磁盘字节都不得改变（绝不自动 save）。"""

    workflow = _build(tmp_path)
    path = tmp_path / "project.json"
    before_statuses = deepcopy(workflow.state.get("result_statuses") or {})
    before_bytes = path.read_bytes()

    restored = WorkflowService(path, DEFAULTS)

    assert (restored.state.get("result_statuses") or {}) == before_statuses
    assert path.read_bytes() == before_bytes, "加载项目绝不允许自动 save"


def test_p16_refuses_when_upstream_status_is_raw_stale(tmp_path):
    """上游 raw 状态为 stale ⇒ P16 必须 fail-closed，绝不当作 current。"""

    workflow = _build(tmp_path)
    workflow.state["cns_corridor_assessment"]["status"] = "stale"
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "missing_data"
    assert result["selected_actions"] == []


def test_p16_refuses_when_channel_gap_status_is_raw_stale(tmp_path):
    workflow = _build(tmp_path)
    workflow.state["cns_corridor_gap_assessment"]["status"] = "stale"
    result = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert result["status"] == "missing_data"


def test_algorithm_semantics_projection_marks_old_version_stale():
    """算法语义版本变化 ⇒ 只读投影如实标 stale（绝不误判 current）。"""

    result = {
        "status": "proposal_ready",
        "algorithm_id": "corridor_reuse_first_site_planner_v2",
        "algorithm_version": "1.0",
    }
    projected = apply_algorithm_semantics_stale(
        result, "corridor_reuse_first_site_planner_v2", "2.2",
    )
    assert projected["status"] == "stale"
    assert projected["stale_reason"] == "algorithm_semantics_changed"
    assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == "1.0"
    assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "2.2"


def test_p16_snapshot_projects_algorithm_semantics(tmp_path):
    """P16 的只读快照必须消费投影后的 currentness，而不是 raw 状态。"""

    workflow = _build(tmp_path)
    service = workflow.corridor_site_planning_service
    assert isinstance(service, CorridorSitePlanningService)

    service.session.state["cns_corridor_site_plan"] = {
        "status": "proposal_ready",
        "algorithm_id": service.planner.algorithm_id,
        "algorithm_version": "0.0-ancient",
    }
    snapshot = service.result_snapshot()
    assert snapshot["status"] == "stale"
    #: raw 状态本身不得被投影过程改写。
    assert service.session.state["cns_corridor_site_plan"]["status"] == "proposal_ready"
