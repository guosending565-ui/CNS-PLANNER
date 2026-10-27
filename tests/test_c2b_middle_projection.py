"""C2-B 回归：typed middle N 动作在 P17/P18 投影里**绝不**被当成走廊设施。

背景：C2-NM3 把 NM3 中段 chain 移植进本基线时，P16 侧的 ``apply_winner`` 与
``_apply_cumulative_action`` 都认识 ``NAVIGATION_MIDDLE_REUSE_FAMILY``，但
``plan_projection`` / ``plan_review_service`` 的投影路径不认识，导致：

* ``PlanProjectionBuilder.project`` 把 middle 动作送进
  ``_apply_cumulative_action`` → ``KeyError: 'device_id'``（middle 动作是站点级动作，
  没有 device_id）；
* ``PlanReviewService._evaluate_variant`` 把 middle 动作写进假想 facilities →
  N 监视站被当成普通 radio 设备，错误改变 C/RID/Radar 几何判定。

本回归锁定：middle 动作只进入冻结 NM3 supplement 通道，不被写成假想设施；普通
radio 动作与既有 endpoint 监视动作的行为逐字不变。
"""

from __future__ import annotations

from cns_planner.application.plan_projection import (
    NAVIGATION_MIDDLE_PLANNER_FAMILY,
    project_facilities,
    split_plan_actions,
)

MIDDLE_ACTION = {
    "action_id": "navigation_integrity_endpoint_middle_reuse_v1:R0005:NEW-425000-3301500:3",
    "planner_family": NAVIGATION_MIDDLE_PLANNER_FAMILY,
    "site_id": "NEW-425000-3301500",
    "coordinate": [121.5, 30.5],
    "reuse_class": "new_build_candidate",
    "subsystem": "N",
}

RADIO_ACTION = {
    "action_id": "candidate:RES-1",
    "planner_family": None,
    "site_id": "RES-1",
    "device_id": "DEV-1",
    "subsystem": "C",
    "coordinate": [121.4, 30.4],
    "reuse_class": "existing_shared_site",
}

ENDPOINT_ACTION = {
    "action_id": "add_navigation_integrity_monitor:R0005:origin:N005",
    "planner_family": "endpoint_integrity_monitor",
    "site_id": "N005",
    "device_id": "NAVINT-R0005-origin-N005",
    "subsystem": "N",
    "reuse_class": "existing_shared_site",
}


def _existing():
    return {
        "items": [{
            "facility_id": "F-1", "site_id": "N005", "coordinate": [121.4, 30.4],
            "devices": [], "status": "active",
        }],
        "count": 1,
    }


def test_middle_action_without_device_id_never_raises_and_never_becomes_facility():
    existing = _existing()
    projected = project_facilities(existing, [MIDDLE_ACTION])
    assert projected == existing
    assert all("hypothetical_action_id" != (device.get("metadata") or {}).get(
        "hypothetical_action_id") for item in projected["items"]
        for device in item.get("devices") or [])


def test_split_plan_actions_has_explicit_middle_bucket():
    facilities, radar, navigation, middle = split_plan_actions([MIDDLE_ACTION])
    assert middle == [MIDDLE_ACTION]
    assert facilities == [] and radar == [] and navigation == []


def test_radio_action_still_becomes_hypothetical_facility():
    projected = project_facilities(_existing(), [RADIO_ACTION])
    ids = {item.get("facility_id") for item in projected["items"]}
    assert "p16-proposal:RES-1" in ids
    device_ids = {
        device.get("device_id")
        for item in projected["items"] for device in item.get("devices") or []
    }
    assert "DEV-1" in device_ids


def test_endpoint_monitor_action_still_follows_facilities_bucket():
    #: endpoint 监视动作仍按既有语义进入 facilities 桶（不是 middle 桶），但
    #: ``_apply_cumulative_action`` 本身对它也有显式 guard：它是 caller-owned 的
    #: 假想 endpoint provider，不写成走廊 radio 设备。本测试锁定这两点都不变。
    facilities, _radar, _navigation, middle = split_plan_actions([ENDPOINT_ACTION])
    assert facilities == [ENDPOINT_ACTION]
    assert middle == []
    existing = _existing()
    assert project_facilities(existing, [ENDPOINT_ACTION]) == existing
