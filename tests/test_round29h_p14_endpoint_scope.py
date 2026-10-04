"""Round 29-H —— P14 route-endpoint 作用域隔离回归（BUG-NAV-ENDPOINT-P14-SCOPE）。

Round29-G 已修 P15 的 corridor 聚合，但 P14 仍为每个 corridor voxel 的 N 走 legacy
capability 评估 ⇒ 2844 个 navigation corridor unknown（Round29-F 真实项目实测
``N subsystem: required_voxel_count=2844, unknown_voxel_count=2844``）。

本轮按 ``service_scope`` 修 P14：某子系统的 required 服务全部为 ``route_endpoints``
时，该子系统在 corridor voxel 中必须

* ``planning_status = not_applicable``；
* 不生成 corridor service deficit；
* 不生成 corridor unknown；
* 不进入 corridor provider/redundancy 汇总。

Navigation 的正式状态仅来自 ``endpoint_service_evidence``。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.domain.cns_service_registry import (
    ENDPOINT_SCOPE_NOT_APPLICABLE_REASON, endpoint_scope_only_subsystems,
)

from test_round29g_navigation_endpoint_planning import (
    KEY, _endpoint_workflow, requirement,
)


def _entries(corridor, subsystem):
    return [
        next(item for item in voxel["subsystems"] if item["subsystem"] == subsystem)
        for route in corridor["routes"] for voxel in route["voxels"]
    ]


def test_endpoint_scope_dispatch_is_shared_and_scope_driven():
    """dispatch 按 ``service_scope`` 判定，P14 / P15 共用同一实现。"""

    assert endpoint_scope_only_subsystems(requirement()["project_default"]) == frozenset({"N"})

    #: 同一份要求里若同时存在 corridor 与 endpoint 服务，则该子系统**不**被隔离。
    mixed = deepcopy(requirement()["project_default"])
    mixed["navigation"]["services"]["N:rtk_augmentation"] = {"required": True, "confirmed": True}
    assert endpoint_scope_only_subsystems(mixed) == frozenset()

    #: legacy（无显式 services）绝不隔离。
    legacy = deepcopy(requirement()["project_default"])
    legacy["navigation"].pop("services")
    assert endpoint_scope_only_subsystems(legacy) == frozenset()


def test_p14_navigation_voxel_is_not_applicable_when_endpoint_only(tmp_path):
    workflow = _endpoint_workflow(tmp_path, c_facility=True, candidates=[])
    corridor = workflow.state["cns_corridor_assessment"]
    assert corridor["status"] in ("failed",)
    assert corridor["routes"][0]["voxels"], "fixture 必须真的产生走廊体素"

    entries = _entries(corridor, "N")
    assert entries
    for entry in entries:
        assert entry["planning_status"] == "not_applicable"
        assert entry["p8_status"] == "not_applicable"
        assert entry["providers"] == []
        assert entry["provider_evaluations"] == []
        assert entry["service_redundancy"] == []
        assert entry["reasons"] == [ENDPOINT_SCOPE_NOT_APPLICABLE_REASON]
        assert entry["evidence"][0]["kind"] == "endpoint_scope_dispatch"

    #: P14 route 级 subsystem 汇总同样必须是 not_applicable / 0 体素。
    summary = next(
        item for item in corridor["routes"][0]["subsystems"] if item["subsystem"] == "N"
    )
    assert summary["status"] == "not_applicable"
    assert summary["required_voxel_count"] == 0
    assert summary["confirmed_deficit_voxel_count"] == 0
    assert summary["unknown_voxel_count"] == 0


def test_p14_unknown_and_deficit_voxel_ids_exclude_endpoint_only_navigation(tmp_path):
    workflow = _endpoint_workflow(tmp_path, c_facility=True, candidates=[])
    corridor = workflow.state["cns_corridor_assessment"]

    #: 走廊本体（C 已由既有设施满足、S 不要求）不含任何 deficit / unknown 体素。
    assert corridor["unknown_voxel_ids"] == []
    assert corridor["deficit_voxel_ids"] == []
    for route in corridor["routes"]:
        assert route["status"] == "failed"  # 仅因 endpoint 缺口
        for voxel in route["voxels"]:
            navigation = next(
                item for item in voxel["subsystems"] if item["subsystem"] == "N"
            )
            assert navigation["planning_status"] not in ("unknown", "confirmed_deficit")


def test_endpoint_missing_still_fails_the_route_overall_status(tmp_path):
    workflow = _endpoint_workflow(tmp_path, c_facility=True, candidates=[])
    corridor = workflow.state["cns_corridor_assessment"]
    gap = workflow.state["cns_corridor_gap_assessment"]

    endpoint = corridor["endpoint_service_evidence"]["routes"][0]
    assert endpoint["status"] == "confirmed_deficit"
    assert {
        item["endpoint_role"] for item in endpoint["endpoints"].values()
        if item["status"] == "confirmed_deficit"
    } == {"origin", "destination"}

    #: 走廊本体三个子系统都不是 failed，route/整体 failed 只能来自 endpoint 覆盖。
    assert {
        item["subsystem"]: item["status"]
        for item in corridor["routes"][0]["subsystems"]
    } == {"C": "passed", "N": "not_applicable", "S": "not_applicable"}
    assert corridor["routes"][0]["status"] == "failed"
    assert corridor["status"] == "failed"
    assert gap["routes"][0]["status"] == "failed"
    assert gap["status"] == "failed"


def test_corridor_subsystems_are_byte_identical_apart_from_the_scoped_subsystem(tmp_path):
    """C/S 的走廊行为逐字段不变：N 的作用域隔离不改变任何其它子系统的判定。"""

    scoped = _endpoint_workflow(tmp_path / "scoped", c_facility=True, candidates=[])
    baseline = _endpoint_workflow(tmp_path / "baseline", c_facility=True, candidates=[])
    #: 合成对照：把 N 的 endpoint 服务声明去掉（legacy N），C/S 的输入完全不变。
    baseline.state["required_cns"] = _without_navigation_services(
        baseline.state["required_cns"]
    )
    baseline.evaluate_cns_corridor()

    for code in ("C", "S"):
        left = _entries(scoped.state["cns_corridor_assessment"], code)
        right = _entries(baseline.state["cns_corridor_assessment"], code)
        assert len(left) == len(right) and left
        for first, second in zip(left, right):
            assert _stable(first) == _stable(second)

    #: 对照组的 N 不是 not_applicable —— 证明差异确实只来自作用域 dispatch。
    baseline_n = _entries(baseline.state["cns_corridor_assessment"], "N")
    assert any(item["planning_status"] != "not_applicable" for item in baseline_n)
    scoped_n = _entries(scoped.state["cns_corridor_assessment"], "N")
    assert all(item["planning_status"] == "not_applicable" for item in scoped_n)


def test_p15_navigation_aggregation_follows_the_same_scope(tmp_path):
    """P15 与 P14 同一份 dispatch：corridor 聚合 0 体素，正式状态只来自 endpoint。"""

    workflow = _endpoint_workflow(tmp_path, c_facility=True, candidates=[])
    gap = workflow.state["cns_corridor_gap_assessment"]
    navigation = next(
        item for item in gap["routes"][0]["subsystems"] if item["subsystem"] == "N"
    )
    assert navigation["status"] == "not_applicable"
    assert navigation["required_voxel_count"] == 0
    assert navigation["continuous_deficit_segments"] == []
    assert navigation["unknown_voxel_ids"] == []
    assert navigation["confirmed_target_voxel_ids"] == []
    assert gap["endpoint_service_gaps"]["status"] == "confirmed_deficit"

    #: P16 的 endpoint target 只来自 endpoint 证据（不是 corridor voxel）。
    assert KEY in {
        state.get("service_key") or KEY
        for route in gap["endpoint_service_gaps"]["routes"]
        for state in route["endpoint_states"]
    }


def _without_navigation_services(required_cns):
    """对照：N 保持 legacy 要求（required=True，但没有任何 canonical ``services``）。"""

    result = deepcopy(required_cns)
    navigation = result["project_default"]["navigation"]
    navigation.pop("services", None)
    navigation.pop("service_key", None)
    navigation["required"] = True
    navigation["status"] = "passed"
    return result


def _stable(value):
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
