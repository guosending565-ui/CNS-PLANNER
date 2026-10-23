import math

import pytest

from cns_planner.domain.navigation_monitoring_simulation import (
    simulate_navigation_monitoring,
    validate_navigation_monitoring_parameters,
)


def _candidate(candidate_id, x, *, kind="tower_colocation", eligible=True):
    return {
        "candidate_id": candidate_id,
        "candidate_kind": kind,
        "coordinate": [x, 0.0] if x is not None else None,
        "spatial_use_status": "eligible" if eligible else "blocked",
    }


def test_parameter_defaults_and_validation_boundaries():
    assert validate_navigation_monitoring_parameters(None) == {
        "monitor_association_radius_m": 2000.0,
        "gap_tolerance_s": 10.0,
        "route_speed_mps": 15.0,
        "risk_priority_weight": 3.0,
        "max_optional_monitor_sites": 4,
        "force_endpoint_monitors": True,
        "parameter_sources": {
            "monitor_association_radius_m": "default",
            "gap_tolerance_s": "default",
            "route_speed_mps": "default",
            "risk_priority_weight": "default",
            "max_optional_monitor_sites": "default",
            "force_endpoint_monitors": "fixed_constraint",
        },
    }
    assert validate_navigation_monitoring_parameters({"max_optional_monitor_sites": 0})["max_optional_monitor_sites"] == 0
    for key in ("monitor_association_radius_m", "gap_tolerance_s", "route_speed_mps", "risk_priority_weight"):
        for bad in (None, 0, -1, math.inf, math.nan, True):
            with pytest.raises(ValueError, match="参数"):
                validate_navigation_monitoring_parameters({key: bad})
    for bad in (-1, 1.5, math.inf, True):
        with pytest.raises(ValueError, match="参数"):
            validate_navigation_monitoring_parameters({"max_optional_monitor_sites": bad})
    for bad in (False, None, 1, "true"):
        with pytest.raises(ValueError, match="强制"):
            validate_navigation_monitoring_parameters({"force_endpoint_monitors": bad})


def test_missing_mandatory_endpoint_blocks_simulation():
    result = simulate_navigation_monitoring(
        route=[[0, 0], [1000, 0]],
        candidates=[_candidate("node:start", 0), _candidate("node:end", None, eligible=False)],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 100},
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "mandatory_endpoint_spatial_evidence_unavailable"
    assert [item["site_id"] for item in result["mandatory_requirements"]] == ["node:start", "node:end"]
    assert result["mandatory_requirements"][1]["status"] == "blocked"


def test_line_circle_intervals_merge_clip_and_create_multiple_gaps():
    result = simulate_navigation_monitoring(
        route=[[0, 0], [1000, 0]],
        candidates=[
            _candidate("node:start", 0), _candidate("node:end", 1000),
            _candidate("tower-colocation:A", 250), _candidate("tower-colocation:B", 450),
            _candidate("tower-colocation:C", 800),
        ],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 100, "max_optional_monitor_sites": 3},
    )
    assert result["merged_intervals_m"] == [[0.0, 100.0], [150.0, 550.0], [700.0, 1000.0]]
    assert [gap["length_m"] for gap in result["gaps"]] == [50.0, 150.0]
    assert result["associated_length_m"] == 800.0
    assert result["longest_gap_m"] == 150.0
    assert result["longest_gap_duration_s"] == 10.0
    assert result["gaps"][1]["tolerance_status"] == "satisfied"


def test_touching_intervals_merge_and_zero_cap_keeps_only_endpoints():
    merged = simulate_navigation_monitoring(
        route=[[0, 0], [400, 0]],
        candidates=[_candidate("node:start", 0), _candidate("node:end", 400), _candidate("tower-colocation:A", 200)],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 100, "max_optional_monitor_sites": 1},
    )
    assert merged["merged_intervals_m"] == [[0.0, 400.0]]
    assert merged["gaps"] == []

    capped = simulate_navigation_monitoring(
        route=[[0, 0], [400, 0]],
        candidates=[_candidate("node:start", 0), _candidate("node:end", 400), _candidate("tower-colocation:A", 200)],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 100, "max_optional_monitor_sites": 0},
    )
    assert capped["selected_optional_sites"] == []
    assert capped["selection_stop_reason"] == "optional_site_limit_reached"
    assert capped["gaps"][0]["length_m"] == 200.0


def test_reliable_segment_speeds_override_assumption_and_missing_speed_is_unknown():
    base = dict(
        route=[[0, 0], [1000, 0]],
        candidates=[_candidate("node:start", 0), _candidate("node:end", 1000)],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 425, "route_speed_mps": 15, "gap_tolerance_s": 10},
    )
    reliable = simulate_navigation_monitoring(
        **base,
        segment_speeds=[{"start_m": 425, "end_m": 575, "speed_mps": 30, "authority": "reliable"}],
    )
    assert reliable["gaps"][0]["length_m"] == 150.0
    assert reliable["gaps"][0]["duration_s"] == 5.0
    assert reliable["gaps"][0]["speed_source"] == "reliable_segment_speeds"

    for bad in (
        [{"start_m": 425, "end_m": 500, "speed_mps": 30, "authority": "reliable"}],
        [{"start_m": 425, "end_m": 575, "speed_mps": 0, "authority": "reliable"}],
        [{"start_m": 425, "end_m": 575, "speed_mps": 30, "authority": "unreliable"}],
    ):
        unknown = simulate_navigation_monitoring(**base, segment_speeds=bad)
        assert unknown["gaps"][0]["duration_s"] is None
        assert unknown["gaps"][0]["tolerance_status"] == "undetermined"


def test_risk_weighting_ties_and_reusable_device_identity_are_deterministic():
    result = simulate_navigation_monitoring(
        route=[[0, 0], [1000, 0]],
        candidates=[
            _candidate("node:start", 0, kind="project_node"),
            _candidate("node:end", 1000, kind="project_node"),
            _candidate("tower-colocation:T1", 300),
            _candidate("node:L1", 700, kind="project_node"),
            _candidate("tower-colocation:T0", 700),
        ],
        endpoint_site_ids=["node:start", "node:end"],
        parameters={"monitor_association_radius_m": 100, "gap_tolerance_s": 15, "max_optional_monitor_sites": 2, "risk_priority_weight": 3},
        risk_intervals=[[600, 800]],
        reusable_devices=[
            {"device_id": "D-N-START", "service_key": "N", "site_id": "node:start", "status": "installed"},
            {"device_id": "D-N-NEAR", "service_key": "N", "site_id": "node:other", "status": "installed"},
        ],
    )
    assert [item["site_id"] for item in result["selected_optional_sites"]] == ["node:L1", "tower-colocation:T1"]
    assert result["devices"][0] == {"device_id": "D-N-START", "site_id": "node:start", "service_key": "N", "provisioning": "reused_existing"}
    assert len({item["device_id"] for item in result["devices"]}) == len(result["devices"])
    assert all(item["site_id"] != "node:other" for item in result["devices"])


def test_redundant_optional_site_is_pruned_and_fingerprint_tracks_parameters():
    common = dict(
        route=[[0, 0], [600, 0]],
        candidates=[
            _candidate("node:start", 0), _candidate("node:end", 600),
            _candidate("node:A", 300, kind="project_node"),
            _candidate("tower-colocation:B", 300),
        ],
        endpoint_site_ids=["node:start", "node:end"],
    )
    first = simulate_navigation_monitoring(**common, parameters={"monitor_association_radius_m": 200, "max_optional_monitor_sites": 2})
    second = simulate_navigation_monitoring(**common, parameters={"monitor_association_radius_m": 190, "max_optional_monitor_sites": 2})
    assert [item["site_id"] for item in first["selected_optional_sites"]] == ["node:A"]
    assert "tower-colocation:B" in first["pruned_optional_site_ids"] or "tower-colocation:B" in first["unselected_candidate_ids"]
    assert first["result_fingerprint"] != second["result_fingerprint"]
    assert first["semantic_disclaimer"] == "中心线关联仿真，不代表左右各500米全宽CNS保障能力"
    assert first["full_width_service_assurance_status"] == "not_evaluated"
