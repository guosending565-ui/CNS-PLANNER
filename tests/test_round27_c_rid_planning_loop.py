"""Round 2.7 —— 真实 Communication + RID 规划闭环的定向回归。

本文件只覆盖本轮**行为变更**，逐条对应验收要求：

1. 空 ``ExistingCNS`` 仍是**合法规划基线**：有航路 / grid / surface 时 P14 必须产出
   走廊体元与**已确认缺口**，绝不 ``missing_data`` / ``voxels=[]``；
2. 缺**几何输入**（grid 无单元）必须如实报 ``missing_data``，绝不伪装成"待确认"；
3. 设备侧（``scope=device``）工程假设：登记后提供者类型门禁可由 ``unknown`` 变
   ``meets_under_model``，且**绝不**写入 ``device_catalog``；
4. 运行场景侧（``scope=operation``）工程假设：只作用于所声明的运行航路，**绝不**
   写回机载档案，也**绝不**污染 FC30 的厂家事实；
5. 作用对象 fail-closed：``target_id`` 必须是项目里已载入的设备 / 运行航路；
6. P16：设备侧证据补上后，空 ExistingCNS 下仍能产生 selected actions（真实规划闭环）。
"""

from copy import deepcopy

import pytest

from cns_planner.algorithms.corridor.v1 import CNSServiceCorridorV1
from cns_planner.application.invalidation_service import LAYERED_ADOPTION_SOURCE_TYPE
from cns_planner.domain.fc30_profile import fc30_aircraft_profile
from cns_planner.domain.planning_evidence import (
    PLANNING_EVIDENCE_FIELDS, active_evidence_items, apply_aircraft_evidence,
    apply_device_evidence, normalize_engineering_evidence,
    normalize_planning_evidence_registry,
)
from cns_planner.domain.cns_corridor import normalize_cns_corridor_policy

from test_cns_corridor import (
    GRID, ROUTE, SPATIAL, aircraft, device, facilities, requirement_set,
)
from test_corridor_site_planner_v2 import candidate, configured


EMPTY_FACILITIES = {"status": "passed", "collection_id": "existing-cns-facilities",
                    "count": 0, "items": []}

#: 设施侧**显式未记录型号**（``service_model.status = missing_data``）：这正是
#: ``build_provider_devices`` 的守卫语义 —— 未记录型号时保留设备目录里已确认的
#: service model，绝不把空 dict 当成"型号声明"。
RECORDED_FACILITIES = {"status": "passed", "items": [{
    "facility_id": "F1", "coordinate": [0.0, 0.0], "status": "active",
    "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
    "devices": [{
        "device_id": "D1", "subsystem": "C", "status": "active",
        "service_model": {"status": "missing_data"},
    }],
}]}


def run_p14(*, facilities_payload=None, catalog=None, grid=None, required=None,
            aircraft_profile=None):
    """P14 算法级最小运行（绕开 workflow，直接断言 canonical 结果）。"""

    return CNSServiceCorridorV1().evaluate(
        [ROUTE], SPATIAL, grid if grid is not None else GRID,
        {"terrain": {"status": "passed", "cells": {}}},
        required or requirement_set(), aircraft_profile or aircraft(),
        EMPTY_FACILITIES if facilities_payload is None else facilities_payload,
        catalog if catalog is not None else {"status": "passed", "items": [device()]},
        normalize_cns_corridor_policy({"routes": {"R1": {
            "route_id": "R1", "horizontal_half_width_m": 0.0,
            "vertical_lower_margin_m": 10, "vertical_upper_margin_m": 10,
            "source": "test", "confirmed": True,
        }}}),
    )


def assumption(field, value, *, scope, target_id, source_type="engineering_assumption"):
    payload = {
        "evidence_id": f"PEV-{field}-{scope}".upper(),
        "field": field,
        "scope": scope,
        "target_id": target_id,
        "source_type": source_type,
        "value": value,
        "source": "Round 2.7 targeted regression",
        "declared_by": "test_round27",
        "confirmed": True,
        "confirmed_by_user": True,
    }
    if source_type == "engineering_assumption":
        payload.update({
            "statement": "本测试场景的工程规划假设",
            "reason": "使本轮规划闭环可在无厂家资料时完成",
            "report_disclosure": "本项为工程规划假设，不代表厂家既有设备事实。",
        })
    return payload


def registry_of(*items):
    return normalize_planning_evidence_registry({"items": list(items)})


# ---------------------------------------------------------------- 1. 空基线
def test_empty_existing_cns_is_a_valid_baseline_with_voxels_and_deficits():
    """空 ExistingCNS + 有效 route/grid/surface ⇒ voxels > 0 且**已确认缺口**。"""

    result = run_p14()
    route = result["routes"][0]

    assert result["status"] == "failed", "无任何 provider 必须是已确认缺口，不是缺数据"
    assert route["status"] == "failed"
    assert route["voxel_count"] > 0, "空 ExistingCNS 绝不产生 voxels=[]"
    assert route["horizontal_cell_count"] > 0

    communication = next(item for item in route["subsystems"] if item["subsystem"] == "C")
    assert communication["status"] == "failed"
    assert len(communication["deficit_voxel_ids"]) > 0
    assert communication["required_voxel_count"] > 0


def test_missing_grid_geometry_reports_missing_data_not_pending_confirmation():
    """缺**几何输入**（grid 无单元）必须报 missing_data，绝不伪装成待确认。"""

    result = run_p14(grid={"status": "passed", "level": 1, "cells": []})
    route = result["routes"][0]

    assert route["status"] == "missing_data"
    assert route["voxel_count"] == 0
    assert route["horizontal_cell_count"] == 0
    assert result["status"] == "missing_data"


def test_valid_baseline_with_provider_still_reports_satisfied_service():
    """反向断言：真的存在覆盖 provider 时不得被误判成缺口。"""

    result = run_p14(
        facilities_payload=RECORDED_FACILITIES,
        catalog={"status": "passed", "items": [device(radius=500.0)]},
    )
    communication = next(
        item for item in result["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert communication["status"] == "passed"
    assert communication["deficit_voxel_ids"] == []


# ------------------------------------------------------- 2. 设备侧工程假设
def test_device_scope_evidence_unblocks_provider_type_gate():
    """设备侧 + 机载侧的工程假设共同把类型门禁从 unknown 抬到满足。"""

    catalog = {"status": "passed", "items": [device(radius=500.0)]}
    catalog["items"][0]["type"]["network_scope"] = "unknown"
    required = requirement_set()
    required["project_default"]["communication"]["type"]["network_scope"] = "dedicated"
    recorded = RECORDED_FACILITIES

    #: 机载侧尚未声明 network_scope：需求一旦要求它，判定必须保持 unknown。
    blocked = run_p14(
        facilities_payload=recorded, catalog=catalog, required=required,
    )
    c_blocked = next(
        item for item in blocked["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert c_blocked["status"] == "pending_confirmation", (
        "机载 / 设备任一侧未声明 network_scope 时判定必须保持 unknown（fail-closed）"
    )
    assert c_blocked["deficit_voxel_ids"] == [], "证据不足绝不升级为已确认缺口"

    #: 只补设备侧：提供者类型门禁通过，但机载侧仍缺证据 ⇒ 仍不能判定满足。
    device_only = registry_of(assumption(
        "provider_network_scope", "dedicated", scope="device", target_id="D1",
    ))
    applied_catalog = apply_device_evidence(catalog, device_only)
    assert catalog["items"][0]["type"]["network_scope"] == "unknown", "本体绝不被改写"
    assert applied_catalog["items"][0]["type"]["network_scope"] == "dedicated"
    evidence = applied_catalog["items"][0]["provider_evidence"]
    assert evidence["source_type"] == "engineering_assumption"
    assert evidence["planning_input_only"] is True
    assert evidence["device_catalog_modified"] is False
    assert evidence["semantics"] == (
        "engineering_planning_input_disclosed_not_manufacturer_fact"
    )

    still_blocked = run_p14(
        facilities_payload=recorded, catalog=applied_catalog, required=required,
    )
    c_still = next(
        item for item in still_blocked["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert c_still["status"] == "pending_confirmation", "机载侧证据仍不可缺省"

    #: 再补机载平台侧的 network_scope 声明 ⇒ 两侧齐备，判定转为满足。
    both = registry_of(
        assumption("communication_network_scope", "dedicated",
                   scope="aircraft", target_id="A1"),
        assumption("provider_network_scope", "dedicated", scope="device", target_id="D1"),
    )
    applied_aircraft = apply_aircraft_evidence(aircraft(), both, route_ids=["R1"])
    unblocked = run_p14(
        facilities_payload=recorded, catalog=apply_device_evidence(catalog, both),
        required=required, aircraft_profile=applied_aircraft,
    )
    c_unblocked = next(
        item for item in unblocked["routes"][0]["subsystems"] if item["subsystem"] == "C"
    )
    assert c_unblocked["status"] == "passed"
    assert unblocked["routes"][0]["voxel_count"] > 0


def test_device_evidence_never_modifies_the_device_catalog():
    catalog = {"status": "passed", "items": [device()]}
    snapshot = deepcopy(catalog)
    apply_device_evidence(catalog, registry_of(assumption(
        "provider_network_scope", "dedicated", scope="device", target_id="D1",
    )))
    assert catalog == snapshot, "设备侧工程假设绝不写入设备目录本体"


def test_device_evidence_is_byte_equivalent_without_records():
    catalog = {"status": "passed", "items": [device()]}
    assert apply_device_evidence(catalog, None) == catalog
    assert apply_device_evidence(catalog, {"items": []}) == catalog


def test_provider_network_scope_field_only_accepts_device_scope():
    with pytest.raises(ValueError, match="只允许 scope"):
        normalize_engineering_evidence(assumption(
            "provider_network_scope", "dedicated",
            scope="aircraft", target_id="FC30",
        ))
    with pytest.raises(ValueError, match="只允许 scope"):
        normalize_engineering_evidence(assumption(
            "provider_network_scope", "dedicated", scope="operation", target_id="R0005",
        ))


# ------------------------------------------------------- 3. 运行场景工程假设
def test_operation_evidence_applies_only_to_the_declared_route():
    profile = fc30_aircraft_profile()
    registry = registry_of(
        assumption("communication_airborne_interfaces", ["ip"],
                   scope="operation", target_id="R0005"),
        assumption("remote_id_participation", ["network_remote_id"],
                   scope="operation", target_id="R0005"),
    )

    matching = apply_aircraft_evidence(profile, registry, route_ids=["R0005"])
    other = apply_aircraft_evidence(profile, registry, route_ids=["R9999"])

    assert "ip" in matching["communication"]["type"]["interfaces"]
    assert any(
        item.get("technology") == "network_remote_id"
        for item in matching["surveillance"]["type"].get(
            "cooperative_surveillance_services") or []
    )
    assert matching["airborne_evidence"]["operation_scope_fields"] == [
        "communication_airborne_interfaces", "remote_id_participation",
    ]
    #: 未声明该运行场景时逐字节等价 —— 运行假设绝不外泄到其它航路。
    assert other == profile


def test_operation_evidence_never_pollutes_fc30_manufacturer_facts():
    profile = fc30_aircraft_profile()
    snapshot = deepcopy(profile)
    registry = registry_of(
        assumption("communication_airborne_interfaces", ["ip"],
                   scope="operation", target_id="R0005"),
        assumption("remote_id_participation", ["network_remote_id"],
                   scope="operation", target_id="R0005"),
    )

    applied = apply_aircraft_evidence(profile, registry, route_ids=["R0005"])

    assert profile == snapshot, "运行场景假设绝不修改机载档案本体"
    assert applied is not profile
    #: FC30 canonical 厂家事实逐项不变。
    assert applied["aircraft_id"] == "FC30"
    assert applied["cruise_speed_mps"] == snapshot["cruise_speed_mps"] == 15.0
    assert applied["max_horizontal_speed_mps"] == 20.0
    assert applied["mtow_kg"] == 95.0
    assert applied["communication"]["type"]["technology"] == "dedicated_radio"
    assert applied["navigation"]["type"]["technology"] == "gnss_rtk"
    assert applied["surveillance"]["type"]["technology"] == "adsb"
    assert applied["surveillance"]["type"]["sensor_mode"] == (
        snapshot["surveillance"]["type"]["sensor_mode"]
    )


def test_airborne_latency_evidence_never_touches_fc30_performance():
    """机载链路时延的运行场景假设只叠加到副本，canonical 性能事实逐项不变。"""

    profile = fc30_aircraft_profile()
    snapshot = deepcopy(profile)
    assert snapshot["communication"]["performance"]["max_latency_s"] is None, (
        "FC30 canonical 档案并未声明链路时延 —— 这正是需要工程假设补录的缺口"
    )
    registry = registry_of(assumption(
        "communication_airborne_latency_s", 1.0, scope="operation", target_id="R0005",
    ))

    applied = apply_aircraft_evidence(profile, registry, route_ids=["R0005"])
    assert profile == snapshot, "运行场景假设绝不修改机载档案本体"
    assert applied["communication"]["performance"]["max_latency_s"] == 1.0
    #: FC30 的 failsafe 事实（遥控信号丢失 > 3 s 触发 RTH）绝不被覆盖。
    assert applied["communication"]["performance"]["lost_link_threshold_s"] == 3.0
    assert applied["communication"]["type"]["technology"] == "dedicated_radio"
    assert applied["aircraft_id"] == "FC30"
    assert apply_aircraft_evidence(profile, registry, route_ids=["R9999"]) == profile


def test_airborne_latency_evidence_rejects_negative_and_wrong_scope():
    with pytest.raises(ValueError, match="非负"):
        normalize_engineering_evidence(assumption(
            "communication_airborne_latency_s", -1.0,
            scope="operation", target_id="R0005",
        ))
    with pytest.raises(ValueError, match="只允许 scope"):
        normalize_engineering_evidence(assumption(
            "communication_airborne_latency_s", 1.0, scope="device", target_id="D1",
        ))


def test_aircraft_scope_records_still_require_a_matching_aircraft_id():
    profile = fc30_aircraft_profile()
    registry = registry_of(assumption(
        "communication_airborne_interfaces", ["ip"], scope="aircraft", target_id="OTHER",
    ))
    assert apply_aircraft_evidence(profile, registry, route_ids=["R0005"]) == profile


# --------------------------------------------------- 4. 作用对象 fail-closed
def test_device_scope_target_must_be_a_loaded_device(tmp_path):
    workflow = configured(tmp_path)
    with pytest.raises(ValueError, match="不是已载入的设备"):
        workflow.add_planning_evidence(assumption(
            "provider_network_scope", "dedicated", scope="device", target_id="NO-SUCH-DEVICE",
        ))


def test_operation_scope_target_must_be_a_loaded_route(tmp_path):
    workflow = configured(tmp_path)
    with pytest.raises(ValueError, match="不是已载入的运行航路"):
        workflow.add_planning_evidence(assumption(
            "communication_airborne_interfaces", ["ip"],
            scope="operation", target_id="NO-SUCH-ROUTE",
        ))


def test_field_catalogue_ships_scopes_so_the_ui_never_guesses(tmp_path):
    workflow = configured(tmp_path)
    fields = workflow.planning_evidence_fields()["fields"]
    assert fields["provider_network_scope"]["scopes"] == ("device",)
    assert fields["communication_airborne_interfaces"]["scopes"] == (
        "aircraft", "operation",
    )
    assert fields["remote_id_participation"]["scopes"] == ("aircraft", "operation")
    assert PLANNING_EVIDENCE_FIELDS["provider_network_scope"]["demand_field"] == "network_scope"


# ------------------------------------------------- 5. P16 真实规划闭环
def test_device_evidence_turns_no_eligible_proposal_into_selected_actions(tmp_path):
    """空 ExistingCNS：提供者类型证据不足 ⇒ 无正向增益；补上设备侧假设 ⇒ 真实选站。"""

    workflow = configured(tmp_path, facilities=[], candidates=[candidate("S1")])
    #: 真实项目的运行航路由 layered adoption 拥有，``aircraft_profile`` 失效**不会**
    #: 把它标为 stale（见 ``InvalidationService._should_stale_operational_route``）；
    #: 测试 fixture 必须复现同一身份，否则测的就不是真实链路。
    workflow.state["operational_routes"][0]["provenance"] = {
        "source_type": LAYERED_ADOPTION_SOURCE_TYPE,
        "adoption_id": "LRA-ROUND27-TEST",
        "adoption_fingerprint": "layeredadoptionv1-round27test",
    }
    workflow.state["required_cns"]["project_default"]["communication"]["type"][
        "network_scope"
    ] = "dedicated"
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()

    blocked = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert blocked["target_voxel_count"] > 0, "空 ExistingCNS 仍必须产生已确认目标"
    assert blocked["selected_actions"] == []
    assert blocked["stop_reason"] == "no_positive_confirmed_marginal_gain"

    catalog_before = deepcopy(workflow.state["device_catalog"])
    aircraft_before = deepcopy(workflow.state["aircraft_profiles"])

    workflow.add_planning_evidence(assumption(
        "provider_network_scope", "dedicated", scope="device", target_id="C1",
    ))
    assert workflow.state["device_catalog"] == catalog_before, "证据绝不写入设备目录"
    assert workflow.state["aircraft_profiles"] == aircraft_before

    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    device_only = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]
    assert device_only["selected_actions"] == [], (
        "只补设备侧、机载侧仍缺证据时不得选出站址（逐项核对，绝不代填）"
    )

    workflow.add_planning_evidence(assumption(
        "communication_network_scope", "dedicated", scope="aircraft", target_id="A1",
    ))
    assert workflow.state["device_catalog"] == catalog_before
    workflow.evaluate_cns_corridor()
    workflow.evaluate_cns_corridor_gap()
    unblocked = workflow.evaluate_cns_corridor_site_plan()["cns_corridor_site_plan"]

    assert unblocked["target_voxel_count"] == blocked["target_voxel_count"]
    assert len(unblocked["selected_actions"]) >= 1, (
        "设备侧 + 机载侧工程假设齐备后必须能真实选出站址动作"
    )
    assert unblocked["selected_actions"][0]["device_id"] == "C1"
    assert unblocked["selected_actions"][0]["subsystem"] == "C"
    assert unblocked["confirmed_requirement_unit_volume_gain"] > 0
    #: 证据随项目一起保存，重开后仍是同一份假设（绝不升级为设备事实）。
    active = active_evidence_items(workflow.state["planning_evidence"])
    assert {item["field"] for item in active} >= {
        "provider_network_scope", "communication_network_scope",
    }
    assert all(item["source_type"] == "engineering_assumption" for item in active)
