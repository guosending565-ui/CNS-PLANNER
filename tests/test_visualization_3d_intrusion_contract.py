from __future__ import annotations

import pytest

from cns_planner.application.visualization_3d_intrusion_contract import (
    IntrusionContractInputError,
    assess_simulated_track,
    formal_intrusion_contract,
)
from cns_planner.application.visualization_3d_protection_volume import (
    build_engineering_preview,
    build_horizontal_corridor,
    route_path_fingerprint,
)


ROUTE = [[122.2000, 29.9000], [122.2200, 29.9000]]


def _historical_volume():
    return {
        "object_id": "protection-corridor:R0005:ALT-100",
        "status": "historical_stale",
        "route_id": "R0005",
        "route_geometry_identity": {
            "fingerprint": route_path_fingerprint(ROUTE),
        },
        "protection_geometry_identity": "protectiongeometryv1-history",
        "geometry_dimension": "horizontal_boundary_with_altitude_slice",
        "vertical_extent_status": "vertical_extent_unconfirmed",
        "lower_altitude_m": None,
        "upper_altitude_m": None,
        "geometry": build_horizontal_corridor(ROUTE, 250.0),
        "historical": True,
        "stale": True,
    }


def _preview_volume():
    route = {
        "route_id": "R0005",
        "authority": "engineering_demo_projection",
        "path": ROUTE,
        "identity": {"fingerprint": route_path_fingerprint(ROUTE)},
    }
    return build_engineering_preview(
        route, half_width_m=250.0, lower_altitude_m=90.0, upper_altitude_m=125.0)


def _crossing_track(altitude=100.0):
    return [
        {"longitude": 122.2100, "latitude": 29.8950,
         "altitude_egm2008_m": altitude, "relative_time_s": 0.0},
        {"longitude": 122.2100, "latitude": 29.9050,
         "altitude_egm2008_m": altitude, "relative_time_s": 10.0},
    ]


def test_formal_mode_is_blocked_when_real_encounter_tracks_are_empty():
    result = formal_intrusion_contract(
        {"encounter_3d_assessment": {"status": "not_calculated", "tracks": []}},
        [_historical_volume()],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "real_intrusion_tracks_empty"
    assert result["simulated"] is False
    assert result["tracks"] == []
    assert result["events"] == []
    assert result["surveillance_detection"]["status"] == "not_evaluated"


def test_unconfirmed_vertical_extent_produces_horizontal_events_only():
    result = assess_simulated_track(
        track_id="SIM-1",
        track_nodes=_crossing_track(altitude=500.0),
        protection_volume=_historical_volume(),
    )

    horizontal = result["horizontal_projection_intersection"]
    assert horizontal["status"] == "intersects"
    assert [event["kind"] for event in horizontal["events"]] == ["entry", "exit"]
    assert horizontal["events"][0]["relative_time_s"] < horizontal["events"][1]["relative_time_s"]
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "blocked",
        "reason": "vertical_extent_unconfirmed",
        "events": [],
    }
    assert result["eligible_for_current_warning"] is False
    assert result["isolation"]["historical_p17_excluded_from_current_warning"] is True


def test_preview_volume_produces_3d_entry_and_exit_but_not_detection():
    result = assess_simulated_track(
        track_id="SIM-2",
        track_nodes=_crossing_track(altitude=100.0),
        protection_volume=_preview_volume(),
    )

    assert result["simulated"] is True
    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "intrudes"
    assert [event["kind"] for event in intrusion["events"]] == ["entry", "exit"]
    assert all(event["altitude_egm2008_m"] == pytest.approx(100.0)
               for event in intrusion["events"])
    assert result["surveillance_detection"] == {
        "status": "not_evaluated",
        "reason": "real_surveillance_detection_evidence_missing",
        "detection_evidence": None,
    }
    assert result["eligible_for_current_warning"] is False
    assert result["isolation"]["enters_radar_or_rid_detection"] is False


def test_altitude_outside_preview_volume_has_no_3d_intrusion():
    result = assess_simulated_track(
        track_id="SIM-3",
        track_nodes=_crossing_track(altitude=150.0),
        protection_volume=_preview_volume(),
    )
    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "clear",
        "reason": "track_does_not_enter_confirmed_vertical_interval",
        "events": [],
    }


def _hover_track(altitude=100.0, longitude=122.2100, latitude=29.9000):
    """水平位置不变：轨迹在原地点纯垂直升降。"""

    return [
        {"longitude": longitude, "latitude": latitude,
         "altitude_egm2008_m": altitude, "relative_time_s": 0.0},
        {"longitude": longitude, "latitude": latitude,
         "altitude_egm2008_m": altitude + 10.0, "relative_time_s": 20.0},
    ]


def test_hovering_track_inside_corridor_is_not_reported_clear():
    """零长度水平段必须按点包含判定，不得因线段长度为零而默认 clear。"""

    result = assess_simulated_track(
        track_id="SIM-HOVER",
        track_nodes=_hover_track(altitude=100.0),
        protection_volume=_preview_volume(),
    )

    horizontal = result["horizontal_projection_intersection"]
    assert horizontal["status"] == "intersects"
    assert [event["kind"] for event in horizontal["events"]] == ["entry", "exit"]
    assert horizontal["events"][0]["relative_time_s"] == pytest.approx(0.0)
    assert horizontal["events"][1]["relative_time_s"] == pytest.approx(20.0)
    intrusion = result["three_dimensional_volume_intrusion"]
    assert intrusion["status"] == "intrudes"
    assert [event["kind"] for event in intrusion["events"]] == ["entry", "exit"]


def test_hovering_track_outside_corridor_stays_clear():
    """退化处理不得把走廊外的悬停点也判成相交。"""

    result = assess_simulated_track(
        track_id="SIM-HOVER-OUT",
        track_nodes=_hover_track(altitude=100.0, longitude=122.3000),
        protection_volume=_preview_volume(),
    )

    assert result["horizontal_projection_intersection"]["status"] == "clear"
    assert result["three_dimensional_volume_intrusion"]["status"] == "clear"


def test_hovering_track_keeps_vertical_block_when_extent_unconfirmed():
    """悬停叠加缺垂直范围：水平可相交，但三维结论必须仍为 blocked。"""

    result = assess_simulated_track(
        track_id="SIM-HOVER-STALE",
        track_nodes=_hover_track(altitude=500.0),
        protection_volume=_historical_volume(),
    )

    assert result["horizontal_projection_intersection"]["status"] == "intersects"
    assert result["three_dimensional_volume_intrusion"] == {
        "status": "blocked",
        "reason": "vertical_extent_unconfirmed",
        "events": [],
    }


def test_track_nodes_require_monotonic_time_and_complete_coordinates():
    bad = _crossing_track()
    bad[1]["relative_time_s"] = 0.0
    with pytest.raises(IntrusionContractInputError, match="strictly increasing"):
        assess_simulated_track(
            track_id="SIM-BAD", track_nodes=bad, protection_volume=_preview_volume())

    missing = _crossing_track()
    del missing[0]["altitude_egm2008_m"]
    with pytest.raises(IntrusionContractInputError, match="altitude_egm2008_m"):
        assess_simulated_track(
            track_id="SIM-BAD2", track_nodes=missing, protection_volume=_preview_volume())
