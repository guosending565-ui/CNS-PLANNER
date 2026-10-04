"""Round 29-H —— 算法语义版本 / stale 审计回归（D 节）。

本轮改变了持久化语义（P14 endpoint scope、P15 endpoint-only 聚合、P16 endpoint
action/select、Radar surface 长度指标与 canonical gap 投影），因此：

* 对应算法的 ``algorithm_version`` 必须 bump；
* **算法语义版本进入 input fingerprint** —— 上游输入一个字节都没变也不能被误判 current；
* 旧持久化结果在任何只读投影（``/api/cns-service-corridor`` /
  ``/api/cns-corridor-gap`` / ``/api/cns-corridor-site-plan`` /
  ``/api/radar-surveillance-layout``）上必须如实显示 ``stale``。

本文件只验证"语义版本 → stale"这条链，不重算任何真实项目。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1
from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.application.corridor_service import apply_algorithm_semantics_stale
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import empty_cns_corridor_assessment
from cns_planner.domain.cns_planning_objectives import empty_cns_corridor_gap_assessment
from cns_planner.domain.corridor_site_planning import empty_cns_corridor_site_plan
from cns_planner.domain.radar_surveillance_layout import (
    ALGORITHM_ID as RADAR_ALGORITHM_ID, ALGORITHM_VERSION as RADAR_ALGORITHM_VERSION,
)
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2

from test_corridor_site_planner_v2 import DEFAULTS
from test_radar_surveillance_layout import ROUTE_ID, _evaluate, _service


#: 当前冻结的语义版本（Radar 在 Round 29-M 修正动态高度 metadata 后为 V1.3）。
EXPECTED_VERSIONS = {
    "cns_service_corridor_v1": "1.1",
    "cns_corridor_gap_v1": "1.1",
    "corridor_reuse_first_site_planner_v2": "2.1",
    "radar_surveillance_layout": "1.3",
}


def test_algorithm_versions_are_bumped_for_the_changed_semantics():
    assert CNSServiceCorridorV1.algorithm_version == EXPECTED_VERSIONS["cns_service_corridor_v1"]
    assert CNSCorridorGapAnalyzerV1.algorithm_version == EXPECTED_VERSIONS["cns_corridor_gap_v1"]
    assert CorridorReuseFirstSitePlannerV2.algorithm_version == (
        EXPECTED_VERSIONS["corridor_reuse_first_site_planner_v2"]
    )
    assert RADAR_ALGORITHM_ID == "radar_surveillance_layout"
    assert RADAR_ALGORITHM_VERSION == EXPECTED_VERSIONS["radar_surveillance_layout"]

    #: ``empty()`` 与算法类必须逐字一致，否则空结果会被自己的投影误标 stale。
    assert empty_cns_corridor_assessment()["algorithm_version"] == (
        CNSServiceCorridorV1.algorithm_version
    )
    assert empty_cns_corridor_gap_assessment()["algorithm_version"] == (
        CNSCorridorGapAnalyzerV1.algorithm_version
    )
    assert empty_cns_corridor_site_plan()["algorithm_version"] == (
        CorridorReuseFirstSitePlannerV2.algorithm_version
    )


def test_projection_helper_only_marks_a_genuine_semantics_change():
    current = empty_cns_corridor_assessment("passed")
    assert apply_algorithm_semantics_stale(
        current, CNSServiceCorridorV1.algorithm_id, CNSServiceCorridorV1.algorithm_version,
    ) == current

    legacy = {**deepcopy(current), "algorithm_version": "1.0"}
    projected = apply_algorithm_semantics_stale(
        legacy, CNSServiceCorridorV1.algorithm_id, CNSServiceCorridorV1.algorithm_version,
    )
    assert projected["status"] == "stale"
    assert projected["stale_reason"] == "algorithm_semantics_changed"
    assert projected["algorithm_semantics_stale"]["stored_algorithm_version"] == "1.0"
    assert projected["algorithm_semantics_stale"]["current_algorithm_version"] == "1.1"
    #: 旧结果本身（state 里的对象）绝不被就地改写。
    assert legacy["status"] == "passed"
    assert "algorithm_semantics_stale" not in legacy

    #: 完全没有算法身份的历史结果保持不变（手工构造的最小 fixture）。
    anonymous = {"status": "passed", "routes": []}
    assert apply_algorithm_semantics_stale(
        anonymous, CNSServiceCorridorV1.algorithm_id, CNSServiceCorridorV1.algorithm_version,
    ) == anonymous


def test_round29f_p14_result_is_reported_stale_by_the_read_only_projection(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["cns_corridor_assessment"] = {
        **empty_cns_corridor_assessment("failed"),
        #: Round29-F 写下的算法身份。
        "algorithm_version": "1.0",
        "input_fingerprint": "round29f-p14-fingerprint",
    }
    snapshot = workflow.cns_corridor_snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["stale_reason"] == "algorithm_semantics_changed"


def test_round29f_p15_result_is_reported_stale_by_the_read_only_projection(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["cns_corridor_gap_assessment"] = {
        **empty_cns_corridor_gap_assessment("failed"),
        "algorithm_version": "1.0",
        "input_fingerprint": "round29f-p15-fingerprint",
    }
    snapshot = workflow.cns_corridor_gap_snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["stale_reason"] == "algorithm_semantics_changed"


def test_round29f_p16_result_is_reported_stale_by_the_read_only_projection(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["cns_corridor_site_plan"] = {
        **empty_cns_corridor_site_plan("proposal_ready"),
        "algorithm_version": "2.0",
        "input_fingerprint": "round29f-p16-fingerprint",
    }
    snapshot = workflow.cns_corridor_site_plan_snapshot()
    assert snapshot["status"] == "stale"
    assert snapshot["stale_reason"] == "algorithm_semantics_changed"


def test_current_version_results_are_not_falsely_marked_stale(tmp_path):
    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["cns_corridor_assessment"] = empty_cns_corridor_assessment("failed")
    workflow.state["cns_corridor_gap_assessment"] = empty_cns_corridor_gap_assessment("failed")
    workflow.state["cns_corridor_site_plan"] = empty_cns_corridor_site_plan("proposal_ready")
    assert workflow.cns_corridor_snapshot()["status"] == "failed"
    assert workflow.cns_corridor_gap_snapshot()["status"] == "failed"
    assert workflow.cns_corridor_site_plan_snapshot()["status"] == "proposal_ready"


def test_round29f_radar_layout_is_stale_through_the_input_fingerprint(tmp_path):
    """Radar 的 fingerprint 组件已含 ``algorithm_version``：旧版本必然 stale。"""

    service, provider = _service(tmp_path, surface="sea")
    _evaluate(service, provider)
    stored = service.radar_surveillance_layout(ROUTE_ID)["items"][0]
    assert stored["algorithm_version"] == EXPECTED_VERSIONS["radar_surveillance_layout"]
    assert stored["status"] in ("proposal_ready", "infeasible", "search_incomplete")

    #: 模拟 Round29-F（V1.1）写下的 layout：版本与输入指纹都来自旧代码。
    state = service.state
    item = state["radar_surveillance_layout"]["items"][0]
    item["algorithm_version"] = "1.1"
    item["input_fingerprint"] = "round29f-radar-fingerprint"
    item["status"] = "proposal_ready"

    snapshot = service.radar_surveillance_layout(ROUTE_ID)
    assert snapshot["items"][0]["status"] == "stale"
    assert snapshot["items"][0]["stale_reason"] == (
        "radar_surveillance_algorithm_semantics_changed"
    )


def test_semantics_version_is_part_of_the_p14_and_p15_input_fingerprints():
    """语义版本必须是输入指纹的一部分（否则输入没变会被误判 current）。"""

    from cns_planner.algorithms.corridor_gap.v1 import _fingerprint as gap_fingerprint

    corridor = CNSServiceCorridorV1()
    assert corridor.algorithm_version == "1.1"
    #: P14：同一份输入在不同语义版本下必须得到不同指纹。
    import inspect

    source = inspect.getsource(CNSServiceCorridorV1.evaluate)
    assert '"algorithm_semantics"' in source
    assert '"algorithm_version": self.algorithm_version' in source

    source = inspect.getsource(CNSCorridorGapAnalyzerV1.evaluate)
    assert '"algorithm_semantics"' in source

    from cns_planner.site_planner.corridor_reuse_first_v2 import (
        CorridorReuseFirstSitePlannerV2,
    )

    source = inspect.getsource(CorridorReuseFirstSitePlannerV2.assemble)
    assert '"algorithm_semantics"' in source

    #: 指纹函数本身仍然确定性。
    assert gap_fingerprint({"a": 1}) == gap_fingerprint({"a": 1})
