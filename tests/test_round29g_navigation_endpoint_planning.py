"""Round 29-G —— Navigation endpoint 规划（BUG-NAV-P16-ELIGIBILITY / P15-SCOPE）回归。

已确认根因（Round 29-F 真实项目 R0005 复现）：

1. ``add_navigation_integrity_monitor`` 动作**没有** ``eligibility`` 字段，P16 的
   ``_what_if`` 因此在第一步就把它判成 ineligible，该动作永远无法被选中；
2. endpoint 动作也没有确定性 ``device_id``（一旦被选中，通用
   ``_apply_cumulative_action`` 会把它当作普通走廊 radio device 塞进 facilities）；
3. P15 尽管给出了正确的 endpoint 缺口，却仍对每个 corridor voxel 的 ``N`` 生成
   ``unknown_service_evidence``，于是造出 2844 个与要求无关的 navigation corridor
   unknown，``required_voxel_count`` 也被算成 2844。

本模块同时覆盖 endpoint 专用 what-if（只重算 endpoint 证据、绝不重跑走廊链）。
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from cns_planner.algorithms.corridor_gap.v1 import CNSCorridorGapAnalyzerV1
from cns_planner.application.project_state import normalize_project  # noqa: F401
from cns_planner.application.workflow_service import WorkflowService
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy
from cns_planner.domain.cns_inputs import normalize_required_cns
from cns_planner.domain.cns_planning_objectives import normalize_cns_planning_objectives
from cns_planner.domain.cns_service_contract import (
    SERVICE_KEY_COMMUNICATION, SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING,
)
from cns_planner.domain.corridor_site_planning import endpoint_target_id
from cns_planner.domain.navigation_integrity_monitoring import (
    DEVICE_ID_PREFIX, PLANNER_FAMILY, PLANNING_UNIT, REASON_DELIVERY_DEFICIT,
    REASON_EVIDENCE_UNKNOWN, REASON_MISSING,
    build_navigation_integrity_endpoint_evidence, endpoint_gap_evidence,
    hypothetical_endpoint_monitor_evidence, navigation_integrity_monitor_actions,
)

from test_corridor_site_planner_v2 import (
    DEFAULTS, aircraft, candidate, device, vertical,
)


KEY = SERVICE_KEY_NAVIGATION_INTEGRITY_MONITORING
COMMUNICATION_KEY = SERVICE_KEY_COMMUNICATION
ROUTE = {
    "route_id": "R1", "status": "passed", "start_node_id": "A", "end_node_id": "B",
    "path": [[-0.001, 0.0], [0.001, 0.0]],
}
SITE_COORDINATE = {"A": [-0.001, 0.0], "B": [0.001, 0.0]}


# --------------------------------------------------------------------------- domain


def requirement(*, communication=True):
    """与 ``test_corridor_site_planner_v2.required`` **同构**的 RequiredCNS 输入。

    刻意**不**调用 ``normalize_required_cns``：``_requirements_complete`` 要求
    ``coverage_requirement`` / ``max_gap_m`` 等 canonical 字段，而既有 fixture 一贯直接
    提供 ``status``。这里保持同一约定，才能让 C 侧走正常的需求判定路径。
    """

    return {
        "status": "passed",
        "project_default": {
            "communication": {
                "required": communication, "status": "passed",
                "type": {"technology": "dedicated_radio", "interfaces": ["ip"]},
                "performance": {"max_latency_s": 0.5, "min_redundancy": 1},
            },
            "navigation": {
                "required": True, "status": "passed", "type": {}, "performance": {},
                "services": {KEY: {"required": True, "confirmed": True}},
            },
            "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
        },
        "route_overrides": {},
    }


def facility(site, installed=False):
    return {
        "facility_id": f"F-{site}", "site_id": site,
        "distinct_site_id": f"takeoff_landing_site:{site}",
        "coordinate": deepcopy(SITE_COORDINATE[site]), "status": "active",
        "devices": ([{"device_id": f"NAV-{site}", "service_key": KEY, "status": "active"}]
                    if installed else []),
    }


def delivery(origin="satisfied", destination="satisfied"):
    return {"R1": {"origin": origin, "destination": destination}}


def evidence(items, *, route=ROUTE, comm=None, required=None):
    return build_navigation_integrity_endpoint_evidence(
        requirement() if required is None else required, [route],
        existing_facilities={"items": items},
        communication_delivery=delivery() if comm is None else comm,
    )


def actions_for(items, **kwargs):
    return navigation_integrity_monitor_actions(
        endpoint_gap_evidence(evidence(items, **kwargs))
    )


def test_missing_origin_action_is_explicitly_eligible():
    """① + ②：缺 origin monitor 时动作必须带显式 ``eligibility=eligible``。"""

    actions = actions_for([facility("B", installed=True)])
    assert [item["endpoint_role"] for item in actions] == ["origin"]
    action = actions[0]
    assert action["eligibility"] == {"status": "eligible", "reasons": []}
    assert action["eligibility_basis"]["endpoint_reason"] == REASON_MISSING["origin"]
    assert action["eligibility_basis"]["takeoff_landing_site_id"] == "A"
    assert action["eligibility_basis"]["coordinate_available"] is True
    assert action["site_binding"] == "takeoff_landing_site"
    assert action["planner_family"] == PLANNER_FAMILY


def test_missing_destination_action_is_explicitly_eligible():
    actions = actions_for([facility("A", installed=True)])
    assert [item["endpoint_role"] for item in actions] == ["destination"]
    assert actions[0]["eligibility"] == {"status": "eligible", "reasons": []}
    assert actions[0]["eligibility_basis"]["endpoint_reason"] == (
        REASON_MISSING["destination"]
    )
    assert actions[0]["takeoff_landing_site_id"] == "B"


def test_unknown_endpoint_evidence_never_produces_an_eligible_action():
    """③：endpoint 站点 / 坐标证据不足时**绝不**生成 eligible 动作。"""

    broken = {**deepcopy(ROUTE), "start_node_id": None, "end_node_id": None,
              "path": [], "start": None, "end": None}
    gap = endpoint_gap_evidence(evidence([facility("B", installed=True)], route=broken))
    route = gap["routes"][0]
    assert len(route["confirmed_endpoint_gaps"]) == 0
    assert route["status"] == "unknown"
    assert {item["target_id"] if "target_id" in item else item["endpoint_role"]
            for item in route["unknown_endpoint_evidence"]} == {"origin", "destination"}
    assert all(
        REASON_EVIDENCE_UNKNOWN in (state["reasons"] or [])
        for state in route["endpoint_states"]
    )
    assert navigation_integrity_monitor_actions(gap) == []


def test_action_device_identity_is_deterministic_and_never_a_vendor_model():
    """④：确定性、非厂家的 planning device identity（同一输入恒等）。"""

    first = actions_for([facility("B", installed=True)])[0]
    second = actions_for([facility("B", installed=True)])[0]
    assert first["device_id"] == second["device_id"]
    assert first["device_id"] == f"{DEVICE_ID_PREFIX}-R1-origin-A"
    assert first["equipment_selection_status"] == "not_selected"
    assert first["planning_unit"] == PLANNING_UNIT
    assert first["service_key"] == KEY
    assert first["device_service_key"] == KEY
    #: 绝不携带任何厂家 / 型号 / 性能声明。
    assert not any(
        token in str(first).lower()
        for token in ("vendor", "manufacturer", "\"model\"", "performance_claim")
    )
    assert "performance" not in first


def test_hypothetical_apply_resolves_monitor_missing_only():
    """⑤：endpoint 专用 what-if 应用后 ``monitor_missing`` 消失。"""

    baseline = evidence([facility("B", installed=True)])
    action = navigation_integrity_monitor_actions(endpoint_gap_evidence(baseline))[0]
    after = hypothetical_endpoint_monitor_evidence(baseline, [action])
    endpoint = after["routes"][0]["endpoints"]["origin"]
    assert endpoint["status"] == "satisfied"
    assert endpoint["planning_status"] == "satisfied"
    assert endpoint["reasons"] == []
    assert endpoint["distinct_site_ids"] == ["takeoff_landing_site:A"]
    gap = endpoint_gap_evidence(after)
    assert gap["routes"][0]["confirmed_endpoint_gaps"] == []
    assert gap["routes"][0]["status"] == "satisfied"
    #: 假想 provider 是 caller-owned：正式输入逐字段不变。
    assert baseline["routes"][0]["endpoints"]["origin"]["status"] == "confirmed_deficit"


def test_communication_delivery_deficit_survives_the_monitor_installation():
    """⑥：C delivery 仍 deficit 时 endpoint 最终仍是 confirmed_deficit。"""

    comm = {"R1": {"origin": "confirmed_deficit", "destination": "satisfied"}}
    baseline = evidence([facility("B", installed=True)], comm=comm)
    action = navigation_integrity_monitor_actions(endpoint_gap_evidence(baseline))[0]
    after = hypothetical_endpoint_monitor_evidence(baseline, [action])
    endpoint = after["routes"][0]["endpoints"]["origin"]
    assert endpoint["planning_status"] == "satisfied"
    assert endpoint["status"] == "confirmed_deficit"
    assert endpoint["reasons"] == [REASON_DELIVERY_DEFICIT]
    assert REASON_MISSING["origin"] not in endpoint["reasons"]
    gap = endpoint_gap_evidence(after)
    assert [
        item["endpoint_role"] for item in gap["routes"][0]["confirmed_endpoint_gaps"]
    ] == ["origin"]
    assert gap["routes"][0]["status"] == "confirmed_deficit"


def test_one_physical_site_still_cannot_discharge_both_endpoints():
    same = {**deepcopy(ROUTE), "end_node_id": "A"}
    baseline = evidence([facility("B", installed=True)], route=same)
    actions = navigation_integrity_monitor_actions(endpoint_gap_evidence(baseline))
    after = hypothetical_endpoint_monitor_evidence(baseline, actions)
    endpoints = after["routes"][0]["endpoints"]
    assert endpoints["origin"]["distinct_site_ids"] == ["takeoff_landing_site:A"]
    assert endpoints["origin"]["status"] == "confirmed_deficit"
    assert endpoints["destination"]["status"] == "confirmed_deficit"
    assert all(
        REASON_MISSING[role] in endpoints[role]["reasons"] for role in ("origin", "destination")
    )


# ------------------------------------------------------------------ P15 endpoint scope


def _corridor_assessment(voxel_count=4, *, endpoint_evidence):
    voxels = []
    for index in range(voxel_count):
        voxels.append({
            "voxel_id": f"V{index}", "grid_id": f"G{index}", "altitude_layer_id": "L1",
            "nearest_route_offset_m": 10.0 * index, "cell_half_diagonal_m": 2.5,
            "discretized_volume_proxy_m3": 100.0, "surface_class": "land",
            "subsystems": [
                {
                    "subsystem": "C", "planning_status": "satisfied",
                    "provider_evaluations": [{
                        "status": "meets_under_model", "stage": "service_model",
                        "provider_id": "P1", "independence_group": "g1",
                        "distinct_site_id": "site:1",
                    }],
                    "service_redundancy": [{
                        "service_key": "C:communication", "surface_dependent": True,
                        "supports_site_planning": True, "status": "satisfied",
                        "counting_basis": "distinct_site_id",
                        "distinct_site_count": 1, "required_distinct_site_count": 1,
                        "surface_class": "land",
                    }],
                },
                #: P14 对 route-endpoint 服务不产生 corridor service 证据，legacy N 因此
                #: 停在 unknown —— 这正是修复前造出 2844 个 corridor unknown 的入口。
                {"subsystem": "N", "planning_status": "unknown",
                 "provider_evaluations": [], "service_redundancy": []},
                {"subsystem": "S", "planning_status": "not_applicable",
                 "provider_evaluations": [], "service_redundancy": []},
            ],
        })
    return {
        "status": "failed", "algorithm_id": "cns_service_corridor_v1",
        "algorithm_version": "1.0", "input_fingerprint": "corridor-fp",
        "endpoint_service_evidence": endpoint_evidence,
        "routes": [{
            "route_id": "R1", "route_length_m": 40.0, "status": "failed", "voxels": voxels,
        }],
    }


def _endpoint_only_requirement():
    return normalize_required_cns({"project_default": {
        "communication": {"required": True, "status": "passed",
                          "type": {}, "performance": {"min_redundancy": 1}},
        "navigation": {"required": True, "status": "passed", "type": {}, "performance": {},
                       "services": {KEY: {"required": True, "confirmed": True}}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }})


def test_n_endpoint_only_scope_makes_corridor_n_not_applicable_with_zero_voxels():
    """⑧：N 的 required 服务全部是 route_endpoints 时，走廊聚合必须 not_applicable/0。"""

    corridor = _corridor_assessment(endpoint_evidence=evidence([]))
    gap = CNSCorridorGapAnalyzerV1().evaluate(corridor, _endpoint_only_requirement(), {})
    route = gap["routes"][0]
    navigation = next(item for item in route["subsystems"] if item["subsystem"] == "N")

    assert navigation["status"] == "not_applicable"
    assert navigation["required_voxel_count"] == 0
    assert navigation["continuous_deficit_segments"] == []
    assert navigation["total_confirmed_deficit_projection_m"] == 0
    assert navigation["max_continuous_deficit_projection_m"] == 0
    assert navigation["confirmed_target_voxel_ids"] == []
    assert navigation["unknown_voxel_ids"] == []
    assert navigation["causes"] == []
    #: 修复前这里是 4（voxel 数）与 4 个 unknown_voxel_ids。
    assert gap["unknown_voxel_ids"] == []

    #: Navigation 的正式状态只来自 endpoint_service_gaps。
    assert gap["endpoint_service_gaps"]["status"] == "confirmed_deficit"
    assert route["endpoint_services"][0]["status"] == "confirmed_deficit"


def test_endpoint_confirmed_deficit_still_fails_the_route_overall_status():
    """⑨：endpoint confirmed deficit 必须让 route overall status 仍为 failed。"""

    corridor = _corridor_assessment(endpoint_evidence=evidence([]))
    gap = CNSCorridorGapAnalyzerV1().evaluate(corridor, _endpoint_only_requirement(), {})
    route = gap["routes"][0]
    assert route["status"] == "failed"
    assert gap["status"] == "failed"
    #: C / S 全部 satisfied 时走廊本体本身是 passed —— failed 只能来自 endpoint。
    assert {
        item["subsystem"]: item["status"] for item in route["subsystems"]
    } == {"C": "passed", "N": "not_applicable", "S": "not_applicable"}


def test_corridor_scope_requirement_keeps_the_legacy_voxel_aggregation():
    """dispatch 按 service_scope：非 endpoint 作用域时既有行为逐项不变。"""

    corridor = _corridor_assessment(endpoint_evidence=evidence([]))
    legacy = normalize_required_cns({"project_default": {
        "communication": {"required": True, "status": "passed",
                          "type": {}, "performance": {"min_redundancy": 1}},
        "navigation": {"required": True, "status": "passed",
                       "type": {"technology": "gnss"}, "performance": {}},
        "surveillance": {"required": False, "status": "passed", "type": {}, "performance": {}},
    }})
    gap = CNSCorridorGapAnalyzerV1().evaluate(corridor, legacy, {})
    navigation = next(
        item for item in gap["routes"][0]["subsystems"] if item["subsystem"] == "N"
    )
    assert navigation["status"] != "not_applicable"
    assert navigation["required_voxel_count"] == 4
    assert len(navigation["unknown_voxel_ids"]) == 4


# --------------------------------------------------------------- P16 endpoint fast path


def _endpoint_workflow(tmp_path, *, c_facility=True, candidates=None,
                       delivery="satisfied"):
    """P16 fixture：N 只要求 endpoint 服务；C 是否已由**现有设施**满足由参数控制。

    ``c_facility=True`` 时走廊 C 需求已由 F-C 上的 C 设备满足 ⇒ P16 没有 corridor
    target，因此可以把"endpoint fast path 不触发走廊重算"单独隔离出来验证。

    这里自己搭最小 workflow（而不是复用 ``configured``）：RequiredCNS 必须**一次成型**，
    因为对已规范化的需求块二次 normalize 会把 ``confirmation_status`` 降级成
    pending_confirmation（那是既有 normalize 契约，不在本轮修复范围内）。
    """

    workflow = WorkflowService(tmp_path / "project.json", DEFAULTS)
    workflow.state["operational_routes"] = [deepcopy(ROUTE)]
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
        }}, "site_vertical_profiles": {},
    }
    workflow.state["required_cns"] = requirement()
    workflow.state["aircraft_profiles"] = {"status": "passed", "items": [aircraft()]}
    workflow.state["selected_aircraft_profile_id"] = "A1"
    workflow.state["device_catalog"] = {"status": "passed", "items": [device()]}
    workflow.state["existing_cns_facilities"] = {
        "status": "passed",
        "items": [_c_facility()] if c_facility else [],
        "count": 1 if c_facility else 0,
    }
    selected_candidates = [candidate("S1")] if candidates is None else candidates
    workflow.state["candidate_sites"] = {
        "status": "passed", "items": selected_candidates, "count": len(selected_candidates),
    }
    workflow.state["cns_corridor_policy"] = normalize_cns_corridor_policy({"routes": {"R1": {
        "route_id": "R1", "horizontal_half_width_m": 0,
        "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
        "source": "test", "confirmed": True,
    }}})
    workflow.state["cns_planning_objectives"] = normalize_cns_planning_objectives(None)
    workflow.evaluate_cns_corridor()
    #: endpoint 证据是 P14 的**显式可选输入**（``navigation_endpoint_evidence``）。这里按
    #: 该契约注入 caller-owned 证据：Communication delivery 是**已确认事实**（C 覆盖已由
    #: F-C 满足），因此 delivery_status = satisfied ⇒ 装 monitor 后 endpoint 可真正解决。
    workflow.state["cns_corridor_assessment"]["endpoint_service_evidence"] = (
        build_navigation_integrity_endpoint_evidence(
            requirement(), [deepcopy(ROUTE)],
            existing_facilities=workflow.state["existing_cns_facilities"],
            communication_delivery={
                "R1": {"origin": delivery, "destination": delivery},
            },
        )
    )
    workflow.evaluate_cns_corridor_gap()
    return workflow


def _c_facility():
    """一台已安装的 C 设备（覆盖走廊，使 C 需求无需任何 P16 动作即满足）。"""

    return {
        "facility_id": "F-C", "site_id": "F-C", "name": "F-C",
        "coordinate": [0.0, 0.0005], "vertical_profile": vertical(),
        "distinct_site_id": "site:F-C", "devices": [device()], "status": "active",
        "source": "test",
        "planning_profile": {
            "reuse_class": "existing_cns_facility", "add_device_allowed": True,
            "source": "test", "confirmed": True,
        },
    }


def test_endpoint_action_never_triggers_a_corridor_full_rerun(tmp_path):
    """⑦：endpoint 动作走专用 what-if，绝不重跑完整 P14/P15。"""

    workflow = _endpoint_workflow(tmp_path, c_facility=True, candidates=[])
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]

    profile = plan["performance_profile"]
    #: 第 1 轮评估 2 个 endpoint 候选并选中 1 个；第 2 轮只剩 1 个候选 ⇒ 3 次 fast path、
    #: 2 次真正应用，而走廊链**一次都没有重跑**。
    assert profile["endpoint_fast_path_count"] == 3
    assert profile["endpoint_fast_path_apply_count"] == 2
    assert profile["full_corridor_rerun_count"] == 0
    assert profile["selected_count_by_planner_family"] == {PLANNER_FAMILY: 2}
    assert profile["endpoint_fast_path_seconds"] >= 0.0

    selected = plan["selected_actions"]
    assert len(selected) == 2
    assert {item["planner_family"] for item in selected} == {PLANNER_FAMILY}
    assert all(item.get("device_id") for item in selected)
    #: endpoint 动作绝不写进正式 ExistingCNS（仍只有那台既有 C 设施）。
    assert [
        item.get("facility_id") for item in workflow.state["existing_cns_facilities"]["items"]
    ] == ["F-C"]
    assert {
        str(action.get("facility_id")) for action in selected
    } == {"p16-proposal:F-C"} or all(
        action.get("site_id") in ("A", "B") for action in selected
    )

    targets = plan["targets"]
    assert {item["target_id"] for item in targets} == {
        endpoint_target_id("R1", "origin", KEY),
        endpoint_target_id("R1", "destination", KEY),
    }
    assert plan["confirmed_requirement_unit_volume_gain"] == pytest.approx(2.0)
    assert plan["authoritative_combined_what_if_consistent"] is True
    #: 每个中选 endpoint 动作都携带**已确认增益**（而不是被静默丢弃成 0 增益）。
    assert all(
        item["impact"]["status"] == "eligible"
        and item["impact"]["confirmed_requirement_unit_volume_gain"] > 0
        for item in selected
    )


def test_delivery_deficit_still_selects_the_monitor_but_keeps_the_endpoint_gap(tmp_path):
    """⑥ 集成：C delivery 仍 deficit 时，monitor 动作仍被选中（它确实解决了
    monitor_missing），但 endpoint 最终仍是 confirmed_deficit(delivery_deficit)。"""

    workflow = _endpoint_workflow(
        tmp_path, c_facility=True, candidates=[], delivery="confirmed_deficit",
    )
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]

    selected = plan["selected_actions"]
    assert len(selected) == 2
    assert all(item["planner_family"] == PLANNER_FAMILY for item in selected)
    assert all(
        item["impact"]["confirmed_requirement_unit_volume_gain"] > 0 for item in selected
    )

    final_gap = workflow.cns_corridor_site_plan_snapshot()[
        "final_hypothetical_evidence"
    ]["corridor_gap"]
    states = final_gap["endpoint_service_gaps"]["routes"][0]["endpoint_states"]
    assert [state["planning_status"] for state in states] == ["satisfied", "satisfied"]
    assert [state["status"] for state in states] == [
        "confirmed_deficit", "confirmed_deficit",
    ]
    assert [state["reasons"] for state in states] == [
        [REASON_DELIVERY_DEFICIT], [REASON_DELIVERY_DEFICIT],
    ]
    #: 缺口照常如实登记为 residual，绝不因为"装上了 monitor"而消失。
    assert {
        item["endpoint_role"] for item in final_gap["endpoint_service_gaps"]
        ["routes"][0]["confirmed_endpoint_gaps"]
    } == {"origin", "destination"}


def test_endpoint_and_corridor_candidates_share_one_p16_pass(tmp_path):
    """C/RID 候选仍走正式完整 P14/P15 语义，endpoint 候选不参与走廊重算。"""

    workflow = _endpoint_workflow(tmp_path, c_facility=False)
    plan = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    families = {
        item.get("planner_family") for item in plan["candidate_actions"]
    }
    assert PLANNER_FAMILY in families
    profile = plan["performance_profile"]
    #: 走廊候选仍然各自执行一次完整重算：endpoint fast path 绝不改变它们的语义。
    assert profile["full_corridor_rerun_count"] > 0
    assert profile["candidate_count_by_planner_family"][PLANNER_FAMILY] == 2
    assert profile["p14_seconds"] > 0.0 and profile["p15_seconds"] > 0.0
