"""Round37-T3D-3B 模拟轨迹事件判定的边界条件回归。

覆盖任务书 §6 的 1–12 项：完整穿越、全程在外、起点/终点在内部、边界接触、
悬停/纯垂直、多次进出、缺垂直范围、上下界完整、时间戳与字段非法、几何指纹
不匹配、historical P17 不得进入当前预警。

这些用例**只**消费 ``assess_simulated_track``：几何判定只有一套实现。
"""

from __future__ import annotations

import pytest

from cns_planner.application.visualization_3d_intrusion_contract import (
    TRACK_INPUT_LIMITS,
    IntrusionContractInputError,
    assess_simulated_track,
    decode_track_payload,
)

#: 经纬度直角多边形（lon 122.20–122.22，lat 29.90–29.92），便于精确构造
#: "进入 / 相切 / 在外"三种位置关系；真实 P17 走廊是它的缓冲区版本，
#: 判定路径完全相同（同一份 Shapely 判定）。
RECT = {
    "type": "Polygon",
    "coordinates": [[
        [122.20, 29.90], [122.22, 29.90], [122.22, 29.92],
        [122.20, 29.92], [122.20, 29.90],
    ]],
}

ROUTE_FINGERPRINT = "routepathv1-boundary-fixture"


def _volume(*, lower=90.0, upper=125.0, vertical_status="user_supplied_engineering_preview",
            historical=False, stale=False, render_instance_id="protectioninstancev1-rect"):
    return {
        "object_id": "protection-corridor:R0005:ALT-100",
        "render_instance_id": render_instance_id,
        "mode": "engineering_scenario_preview",
        "route_id": "R0005",
        "route_authority": "engineering_demo_projection",
        "route_geometry_identity": {"fingerprint": ROUTE_FINGERPRINT, "point_count": 2},
        "protection_geometry_identity": "protectionpreviewv1-boundary-fixture",
        "geometry_dimension": "three_dimensional_volume",
        "geometry": {"status": "available", "geometry": RECT},
        "vertical_extent_status": vertical_status,
        "lower_altitude_m": lower,
        "upper_altitude_m": upper,
        "historical": historical,
        "stale": stale,
    }


def _historical_volume():
    return _volume(lower=None, upper=None,
                   vertical_status="vertical_extent_unconfirmed",
                   historical=True, stale=True,
                   render_instance_id="protectioninstancev1-rect-historical")


def node(longitude, latitude, relative_time_s, altitude_egm2008_m=100.0):
    return {
        "longitude": longitude, "latitude": latitude,
        "altitude_egm2008_m": altitude_egm2008_m,
        "relative_time_s": relative_time_s,
    }


def assess(nodes, volume=None, **kwargs):
    return assess_simulated_track(
        track_id=kwargs.pop("track_id", "SIM-BOUNDARY"),
        track_nodes=nodes,
        protection_volume=volume if volume is not None else _volume(),
        **kwargs,
    )


def _kinds(events):
    return [row["kind"] for row in events]


# ------------------------------------------------------- 1. 完整进入、穿越、离开


def test_track_entering_crossing_and_leaving_reports_two_true_crossings():
    result = assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)])

    horizontal = result["horizontal_projection_intersection"]
    assert horizontal["status"] == "intersects"
    assert horizontal["penetration"] == "interior"
    assert horizontal["boundary_contact"] is False
    assert _kinds(horizontal["events"]) == ["entry", "exit"]
    assert all(row["crossing"] is True for row in horizontal["events"])
    assert horizontal["event_summary"]["true_crossing_count"] == 2

    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "intrudes"
    assert _kinds(intrusion["events"]) == ["entry", "exit"]
    assert all(row["crossing"] is True for row in intrusion["events"])
    assert 0.0 < intrusion["events"][0]["relative_time_s"] < intrusion["events"][1]["relative_time_s"]


# ------------------------------------------------------------------ 2. 全程在外


def test_track_entirely_outside_stays_clear_in_both_semantics():
    result = assess([node(122.19, 29.93, 0.0), node(122.23, 29.93, 10.0)])

    assert result["horizontal_projection_intersection"]["status"] == "clear"
    assert result["horizontal_projection_intersection"]["events"] == []
    assert result["three_dimensional_volume_intrusion"]["status"] == "clear"
    assert result["three_dimensional_volume_intrusion"]["events"] == []
    assert result["track_geometry"]["initially_inside_horizontal"] is False
    assert result["track_geometry"]["finally_inside_horizontal"] is False


# ------------------------------------------------------------- 3. 起点已在内部


def test_track_starting_inside_never_fabricates_a_from_outside_entry():
    result = assess([node(122.21, 29.91, 0.0), node(122.23, 29.91, 10.0)])

    horizontal = result["horizontal_projection_intersection"]
    entry, exit_event = horizontal["events"]
    assert entry["kind"] == "entry"
    assert entry["crossing"] is False
    assert entry["crossing_basis"] == "initially_inside"
    assert entry["relative_time_s"] == pytest.approx(0.0)
    assert exit_event["crossing"] is True
    assert horizontal["event_summary"]["initially_inside_entries"] == 1
    assert horizontal["event_summary"]["true_crossing_count"] == 1

    geometry = result["track_geometry"]
    assert geometry["initially_inside_horizontal"] is True
    assert geometry["initially_inside_volume"] is True
    assert geometry["finally_inside_horizontal"] is False
    assert geometry["finally_inside_volume"] is False


# ------------------------------------------------------------- 4. 终点仍在内部


def test_track_ending_inside_never_fabricates_an_egress_crossing():
    result = assess([node(122.19, 29.91, 0.0), node(122.21, 29.91, 10.0)])

    horizontal = result["horizontal_projection_intersection"]
    entry, exit_event = horizontal["events"]
    assert entry["crossing"] is True
    assert exit_event["kind"] == "exit"
    assert exit_event["crossing"] is False
    assert exit_event["crossing_basis"] == "still_inside_at_track_end"
    assert exit_event["relative_time_s"] == pytest.approx(10.0)
    assert horizontal["event_summary"]["still_inside_exits"] == 1

    geometry = result["track_geometry"]
    assert geometry["finally_inside_horizontal"] is True
    assert geometry["finally_inside_volume"] is True


# ---------------------------------------------------------------- 5. 边界接触


def test_boundary_contact_is_neither_a_volume_entry_nor_clear():
    """整段轨迹只在保护体边界上相切（零维交集）。"""

    result = assess([node(122.19, 29.89, 0.0), node(122.20, 29.90, 10.0)])

    horizontal = result["horizontal_projection_intersection"]
    assert horizontal["status"] == "boundary_contact"
    assert horizontal["boundary_contact"] is True
    assert horizontal["penetration"] == "boundary_touch"
    # 不得伪造"从外进入"的物理穿越事件。
    assert all(row["crossing"] is False for row in horizontal["events"])
    assert all(row["crossing_basis"] == "boundary_touch" for row in horizontal["events"])

    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "boundary_contact"
    assert intrusion["reason"] == "boundary_contact_only_not_a_volume_entry"
    assert intrusion["events"] == []


def test_track_ending_exactly_on_the_boundary_is_not_reported_clear():
    """投影直边与地理边界的毫米级差异不得让"正好贴边"退化成 clear。"""

    result = assess([node(122.19, 29.91, 0.0), node(122.20, 29.91, 10.0)])

    assert result["horizontal_projection_intersection"]["status"] == "boundary_contact"
    assert result["boundary_contact_tolerance_m"] == pytest.approx(0.25)


def test_track_clearly_outside_the_tolerance_still_reports_clear():
    """容差不得退化成"一律相交"：10 m 外的近失仍是 clear。"""

    result = assess([node(122.19, 29.91, 0.0), node(122.1999, 29.91, 10.0)])

    assert result["horizontal_projection_intersection"]["status"] == "clear"


# ------------------------------------------------------ 6. 悬停 / 纯垂直轨迹


def test_hover_inside_reports_occupancy_without_a_crossing():
    result = assess([node(122.21, 29.91, 0.0), node(122.21, 29.91, 20.0)])

    horizontal = result["horizontal_projection_intersection"]
    assert horizontal["status"] == "intersects"
    assert _kinds(horizontal["events"]) == ["entry", "exit"]
    assert all(row["crossing"] is False for row in horizontal["events"])
    assert result["track_geometry"]["initially_inside_horizontal"] is True
    assert result["track_geometry"]["finally_inside_horizontal"] is True


def test_hover_outside_stays_clear():
    result = assess([node(122.30, 29.91, 0.0), node(122.30, 29.91, 20.0)])

    assert result["horizontal_projection_intersection"]["status"] == "clear"


def test_pure_vertical_track_through_the_volume_has_true_crossings():
    """原地垂直穿越保护体：水平段退化为点，但纵向是真实穿越。"""

    result = assess([
        node(122.21, 29.91, 0.0, altitude_egm2008_m=50.0),
        node(122.21, 29.91, 10.0, altitude_egm2008_m=150.0),
    ])

    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "intrudes"
    assert _kinds(intrusion["events"]) == ["entry", "exit"]
    assert all(row["crossing"] is True for row in intrusion["events"])
    entry, exit_event = intrusion["events"]
    assert entry["altitude_egm2008_m"] == pytest.approx(90.0)
    assert exit_event["altitude_egm2008_m"] == pytest.approx(125.0)
    # 水平上起点/终点都在内部 ⇒ 不是"从外进入"，必须区分开。
    assert result["track_geometry"]["initially_inside_volume"] is False
    assert intrusion["events"][0]["crossing_basis"] == (
        "crossed_protected_volume_boundary_from_outside")


# ------------------------------------------------------------- 7. 多次进出


def test_repeated_entries_and_exits_keep_paired_events_in_time_order():
    result = assess([
        node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0),
        node(122.19, 29.91, 20.0), node(122.23, 29.91, 30.0),
    ])

    events = result["horizontal_projection_intersection"]["events"]
    assert _kinds(events) == ["entry", "exit"] * 3
    times = [row["relative_time_s"] for row in events]
    assert times == sorted(times)
    assert all(row["crossing"] is True for row in events)
    assert result["horizontal_projection_intersection"]["event_summary"] == {
        "entry_count": 3, "exit_count": 3, "true_crossing_count": 6,
        "initially_inside_entries": 0, "still_inside_exits": 0,
    }


# --------------------------------------------------- 8. 切片高度缺失 ⇒ blocked


def test_missing_vertical_extent_blocks_the_three_dimensional_judgement():
    result = assess(
        [node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
        volume=_volume(lower=None, upper=None,
                       vertical_status="vertical_extent_unconfirmed"))

    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "blocked",
        "reason": "vertical_extent_unconfirmed",
        "events": [],
    }
    assert result["track_geometry"]["initially_inside_volume"] is None
    assert result["track_geometry"]["finally_inside_volume"] is None


# --------------------------------------------------- 9. 示意保护体上下界完整


def test_preview_with_complete_vertical_bounds_yields_entry_and_exit_heights():
    result = assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)])

    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "intrudes"
    assert result["protected_volume_geometry_status"] == {
        "horizontal": "available", "vertical": "user_supplied_engineering_preview"}


def test_track_above_the_confirmed_bounds_is_horizontal_only():
    result = assess(
        [node(122.19, 29.91, 0.0, altitude_egm2008_m=500.0),
         node(122.23, 29.91, 10.0, altitude_egm2008_m=500.0)])

    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "clear",
        "reason": "track_does_not_enter_confirmed_vertical_interval",
        "events": [],
    }


# ------------------------------------------- 10. 时间戳与字段非法 / 输入包络


@pytest.mark.parametrize("mutate, match", [
    (lambda rows: rows[1].__setitem__("relative_time_s", 0.0), "strictly increasing"),
    (lambda rows: rows[1].__setitem__("relative_time_s", -5.0), "strictly increasing"),
    (lambda rows: rows[1].__setitem__("relative_time_s", float("nan")), "must be finite"),
    (lambda rows: rows[0].pop("altitude_egm2008_m"), "altitude_egm2008_m"),
    (lambda rows: rows[0].__setitem__("longitude", None), "longitude"),
    (lambda rows: rows[0].__setitem__("latitude", float("inf")), "must be finite"),
])
def test_invalid_track_nodes_are_rejected(mutate, match):
    rows = [node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)]
    mutate(rows)

    with pytest.raises(IntrusionContractInputError, match=match):
        assess(rows)


def test_node_count_duration_span_and_payload_are_bounded():
    too_many = [node(122.20, 29.91, float(index)) for index in range(
        TRACK_INPUT_LIMITS["max_nodes"] + 1)]
    with pytest.raises(IntrusionContractInputError, match="node limit"):
        assess(too_many)

    too_long = [node(122.20, 29.91, 0.0),
                node(122.20, 29.91, TRACK_INPUT_LIMITS["max_duration_s"] + 1.0)]
    with pytest.raises(IntrusionContractInputError, match="duration"):
        assess(too_long)

    too_wide = [node(122.20, 29.91, 0.0), node(130.00, 29.91, 10.0)]
    with pytest.raises(IntrusionContractInputError, match="geographic span"):
        assess(too_wide)

    with pytest.raises(IntrusionContractInputError, match="byte limit"):
        decode_track_payload("[" + "1," * TRACK_INPUT_LIMITS["max_payload_bytes"] + "1]")

    with pytest.raises(IntrusionContractInputError, match="valid JSON"):
        decode_track_payload("not json")

    with pytest.raises(IntrusionContractInputError, match="JSON array"):
        decode_track_payload('{"longitude": 1}')


# -------------------------------------- 11. 轨迹参数与保护体几何指纹不匹配


def test_track_bound_to_a_mismatched_protection_instance_is_rejected():
    with pytest.raises(IntrusionContractInputError, match="render_instance_id"):
        assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
               binding={"render_instance_id": "protectioninstancev1-other"})

    with pytest.raises(IntrusionContractInputError, match="route_geometry_fingerprint"):
        assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
               binding={"route_geometry_fingerprint": "routepathv1-someone-else"})

    with pytest.raises(IntrusionContractInputError, match="protection_geometry_identity"):
        assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
               binding={"protection_geometry_identity": "protectionpreviewv1-other"})


def test_matching_binding_is_echoed_back_with_the_instance_key():
    result = assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
                    binding={"render_instance_id": "protectioninstancev1-rect"})

    assert result["render_instance_id"] == "protectioninstancev1-rect"
    assert result["route_geometry_identity"]["fingerprint"] == ROUTE_FINGERPRINT
    assert result["protection_geometry_identity"] == "protectionpreviewv1-boundary-fixture"


# ------------------------------- 12. historical P17 不得参加当前三维入侵判定


def test_historical_protection_geometry_never_yields_a_current_three_dimensional_intrusion():
    result = assess(
        [node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
        volume=_historical_volume())

    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "blocked",
        "reason": "vertical_extent_unconfirmed",
        "events": [],
    }
    assert result["eligible_for_current_warning"] is False
    assert result["isolation"]["historical_p17_excluded_from_current_warning"] is True


def test_stale_geometry_with_confirmed_bounds_is_still_excluded_from_current_warning():
    """即使历史实例带上了确认上下界，也不得升级成"当前三维入侵"。"""

    result = assess(
        [node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
        volume=_volume(historical=True, stale=True))

    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "blocked",
        "reason": "historical_protection_geometry_excluded_from_current_assessment",
        "events": [],
    }
    assert result["eligible_for_current_warning"] is False


# ------------------------------------------- 探测语义：恒为 not_evaluated


def test_surveillance_detection_is_never_derived_from_geometry():
    for nodes in (
        [node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
        [node(122.21, 29.91, 0.0), node(122.21, 29.91, 10.0)],
    ):
        result = assess(nodes)
        assert result["simulated"] is True
        assert result["surveillance_detection"] == {
            "status": "not_evaluated",
            "reason": "real_surveillance_detection_evidence_missing",
            "detection_evidence": None,
        }
        assert result["isolation"]["enters_radar_or_rid_detection"] is False
        assert result["isolation"]["operational_authorization"] is False


def test_client_supplied_detection_evidence_cannot_upgrade_the_verdict():
    """合同只如实回显调用方给的证据引用，判定状态保持 not_evaluated。"""

    result = assess([node(122.19, 29.91, 0.0), node(122.23, 29.91, 10.0)],
                    detection_evidence={"source": "client_claim"})

    assert result["surveillance_detection"]["status"] == "not_evaluated"
    assert result["surveillance_detection"]["detection_evidence"] == {
        "source": "client_claim"}
