"""Round38-W1B：P16 真实站址与 P17 固定宽带/响应解耦回归。"""

from copy import deepcopy

import pytest

from cns_planner.application.corridor_site_planning_service import registered_p16_site_sources
from cns_planner.application.result_currentness import apply_algorithm_semantics_stale
from cns_planner.reporting.builder import _continuous_service_section
from cns_planner.reporting.html_renderer import _continuous_service
from cns_planner.site_planner.corridor_reuse_first_v2 import CorridorReuseFirstSitePlannerV2
from cns_planner.algorithms.continuous_service.v1 import ContinuousServiceAcceptabilityV1

from test_continuous_service_acceptability import (
    _aircraft, _assumption, _evaluate, _p14, _p15, _registry, _route,
    _route_result, _subsystem_entry,
)


def _site(site_id, *, reference_site_id=None):
    return {
        "site_id": site_id, "reference_site_id": reference_site_id,
        "coordinate": [122.1, 30.1], "usable": True, "locked": False,
        "planning_profile": {
            "reuse_class": "candidate_site", "add_device_allowed": True, "confirmed": True,
        },
        "vertical_profile": {"service_origin_egm2008_m": 100.0, "confirmed": True},
    }


def _tower(site_id, tower_id):
    return {
        **_site(site_id), "metadata": {"host": {"host_tower_id": tower_id}},
        "planning_profile": {
            "reuse_class": "tower_colocation", "add_device_allowed": True, "confirmed": True,
        },
    }


def test_p16_sources_allow_only_registered_sites_and_keep_crs_gate():
    shared = {"status": "passed", "candidates": [
        {"candidate_id": "node:N1", "source_id": "N1",
         "candidate_kind": "project_node", "spatial_use_status": "eligible"},
        {"candidate_id": "reference-landing-site:R1", "source_id": "R1",
         "candidate_kind": "reference_landing_site", "spatial_use_status": "blocked",
         "spatial_block_reason": "reference_source_crs_unresolved"},
        {"candidate_id": "tower-colocation:T1", "source_id": "T1",
         "candidate_kind": "tower_colocation", "spatial_use_status": "eligible"},
    ], "source_counts": {"tower_records": 1}}
    sources = registered_p16_site_sources(
        shared,
        existing_facilities={"status": "passed", "items": [
            {"facility_id": "F1", "planning_profile": {"confirmed": True}},
        ]},
        candidate_sites={"status": "passed", "items": [
            _site("N1"), _site("FREE"), _site("R1", reference_site_id="R1"),
        ]},
        tower_colocation={"status": "passed", "items": [
            _tower("tower:T1", "T1"), _tower("tower:SYNTHETIC", "SYNTHETIC"),
        ]},
    )
    assert [item["site_id"] for item in sources["candidate_sites"]["items"]] == ["N1"]
    assert [item["metadata"]["host"]["host_tower_id"]
            for item in sources["tower_colocation"]["items"]] == ["T1"]
    assert sources["existing_facilities"]["items"] == [
        {"facility_id": "F1", "planning_profile": {"confirmed": True}},
    ]
    assert sources["statistics"]["allowed_source_counts"] == {
        "existing_cns_facilities": 1, "registered_landing_sites": 1,
        "real_tower_sites": 1,
    }
    assert sources["statistics"]["excluded_reason_counts"] == {
        "reference_source_crs_unresolved": 1,
        "unregistered_candidate_site": 1,
        "unregistered_tower_site": 1,
    }
    assert sources["statistics"]["synthetic_tower_count"] == 0


def _response_result(*, detection_range, aircraft_detection=None, corridor_assessment=None):
    p15 = _p15([_route(length_m=0.0, gap_causes=[], surplus=[{
        "service_key": "S:rid_cooperative", "subsystem": "S", "status": "satisfied",
        "required_distinct_site_count_by_surface": {"sea": 1},
        "distinct_site_count_by_surface": {"sea": 1},
    }])])
    catalog = {"items": []}
    if detection_range is not None:
        catalog["items"].append({
            "device_id": "RID-1", "subsystem": "S", "service_key": "S:rid_cooperative",
            "coverage_geometry": {"radius_by_surface": {"sea": detection_range}, "source": "test"},
        })
    inputs = {
        "corridor_gap_assessment": p15,
        "aircraft_profile": _aircraft(detection_range=aircraft_detection),
        "device_catalog": catalog,
        "planning_evidence": _registry(
            _assumption("D_separation_m", 50.0), _assumption("D_maneuver_m", 50.0),
            _assumption("D_uncertainty_m", 30.0),
            _assumption("T_chain_detect_track_s", 2.0),
            _assumption("T_chain_sensor_to_platform_s", 1.0),
            _assumption("T_chain_platform_processing_s", 1.0),
            _assumption("T_chain_platform_to_aircraft_s", 1.0),
            _assumption("T_chain_aircraft_response_manoeuvre_s", 2.0),
            _assumption("conservative_closing_speed_mps", 40.0),
            _assumption("relative_speed_basis", "conservative"),
            _assumption("rtk_availability", "available"),
            _assumption("gnss_availability", "available"),
        ),
    }
    if corridor_assessment is not None:
        inputs["corridor_assessment"] = corridor_assessment
    return _evaluate(**inputs)


@pytest.mark.parametrize(("detection_range", "expected"), [
    (5000.0, "passed"), (200.0, "failed"), (None, "unknown"),
])
def test_fixed_band_and_response_timeliness_are_independent(detection_range, expected):
    result = _response_result(detection_range=detection_range)
    corridor = _route_result(result)["corridor"]
    assert result["algorithm_version"] == "2.5"
    assert corridor["service_target_half_width_m"] == 500.0
    assert corridor["corridor_width_policy_version"] == "fixed-1000m-v1"
    assert corridor["response_protection_distance_m"] == pytest.approx(410.0)
    assert corridor["response_timeliness_status"] == expected
    assert corridor["current_service_width_derived_from_response_distance"] is False
    assert corridor["outer_half_width_m"] is None
    assert corridor["outer_half_width_m_semantics"] == "historical_audit_only"


def test_p17_uses_fixed_geometry_identity_and_500m_for_current_service_probe():
    p14 = _p14(half_width_m=900.0, path=[[1.0, 1.0], [2.0, 2.0]])
    p14["routes"][0].update({
        "fixed_corridor_geometry_status": "passed",
        "fixed_corridor_geometry": {
            "status": "passed",
            "route_path": [[122.0, 30.0], [122.1, 30.1]],
            "geometry_fingerprint": "fixed-geometry-fp",
        },
    })

    result = _response_result(detection_range=5000.0, corridor_assessment=p14)
    corridor = _route_result(result)["corridor"]

    assert corridor["route_path"] == [[122.0, 30.0], [122.1, 30.1]]
    assert corridor["fixed_corridor_geometry_fingerprint"] == "fixed-geometry-fp"
    surveillance = next(
        item for item in _route_result(result)["subsystems"]
        if item.get("subsystem") == "S" and "acceptance" in item
    )
    assert surveillance["protection_corridor_half_width_m"] == 500.0


def test_old_p16_and_p17_versions_project_stale_without_mutating_history():
    p16 = {"status": "proposal_ready", "algorithm_id": CorridorReuseFirstSitePlannerV2.algorithm_id,
           "algorithm_version": "2.2", "selected_actions": [{"action_id": "A"}]}
    p17 = {"status": "fully_satisfied", "algorithm_id": ContinuousServiceAcceptabilityV1.algorithm_id,
           "algorithm_version": "2.3", "routes": [{"route_id": "R1"}]}
    before = deepcopy((p16, p17))
    assert apply_algorithm_semantics_stale(
        p16, CorridorReuseFirstSitePlannerV2.algorithm_id,
        CorridorReuseFirstSitePlannerV2.algorithm_version,
    )["status"] == "stale"
    assert apply_algorithm_semantics_stale(
        p17, ContinuousServiceAcceptabilityV1.algorithm_id,
        ContinuousServiceAcceptabilityV1.algorithm_version,
    )["status"] == "stale"
    assert (p16, p17) == before


def test_report_separates_fixed_band_gap_and_response_unknown():
    result = _response_result(detection_range=None)
    section = _continuous_service_section({
        "continuous_service_acceptability": result,
        "cns_corridor_site_plan": {
            "status": "no_eligible_proposal",
            "residual_confirmed_targets": [{"target_id": "gap-1"}],
        },
    })
    row = section["route_protection"]["routes"][0]
    assert row["service_target_half_width_m"] == 500.0
    assert row["fixed_band_service_status"] in {"passed", "failed", "unknown"}
    assert row["response_timeliness_status"] == "unknown"
    assert section["planned_gap_status"]["status"] == "no_eligible_proposal"
    assert section["planned_gap_status"]["residual_confirmed_target_count"] == 1
    assert section["no_response_success_claim"] is True
    html = _continuous_service(section)
    assert "固定宽带达标不代表预警及时或避碰成功" in html
    assert "响应及时性" in html


def test_p16_excludes_synthetic_and_unconfirmed_existing_cns_sources():
    """既有 CNS 设施来源必须"真实的已确认设施"，合成/工程验证设施不得成为候选站址。"""

    shared = {"status": "passed", "candidates": [], "source_counts": {}}
    sources = registered_p16_site_sources(
        shared,
        existing_facilities={"status": "passed", "items": [
            {"facility_id": "F-REAL", "metadata": {
                "dataset_class": "field_survey", "real_world_facility": True},
             "planning_profile": {"confirmed": True}},
            {"facility_id": "F-SYNTHETIC", "metadata": {
                "dataset_class": "synthetic_engineering_validation",
                "real_world_facility": False}},
            {"facility_id": "F-NOT-REAL", "metadata": {"real_world_facility": False}},
            {"facility_id": "F-UNCONFIRMED", "metadata": {"real_world_facility": True},
             "planning_profile": {"confirmed": False}},
        ]},
        candidate_sites={"status": "passed", "items": []},
        tower_colocation={"status": "passed", "items": []},
    )
    assert [item["facility_id"] for item in sources["existing_facilities"]["items"]] == ["F-REAL"]
    assert sources["statistics"]["allowed_source_counts"]["existing_cns_facilities"] == 1
    assert sources["statistics"]["excluded_reason_counts"] == {
        "synthetic_existing_cns_facility": 2,
        "unconfirmed_existing_cns_facility": 1,
    }


@pytest.mark.parametrize(("profile", "label"), [
    ({}, "planning_profile 整体缺失"),
    ({"confirmed": None}, "confirmed 显式为 null"),
    ({"confirmed": False}, "confirmed 显式为 false"),
    (None, "planning_profile 为 null"),
    ({"reuse_class": "existing_cns_facility"}, "confirmed 键缺失"),
])
def test_p16_existing_cns_gate_blocks_missing_null_and_false_confirmation(profile, label):
    """Round38-N2 补充核实：未确认的既有设施不得成为有效规划宿主。

    ``planning_profile.confirmed`` 只有**显式 true** 才算已确认；缺失、``null``、
    ``false`` 三种情形必须同样被阻断 —— 否则"没有确认记录"会被静默当成"已确认"。
    """

    facility = {
        "facility_id": "F-REAL-UNCONFIRMED",
        "metadata": {"dataset_class": "field_survey", "real_world_facility": True},
    }
    if profile is not None:
        facility["planning_profile"] = profile
    sources = registered_p16_site_sources(
        {"status": "passed", "candidates": [], "source_counts": {}},
        existing_facilities={"status": "passed", "items": [facility]},
        candidate_sites={"status": "passed", "items": []},
        tower_colocation={"status": "passed", "items": []},
    )
    assert sources["existing_facilities"]["items"] == [], label
    assert sources["existing_facilities"]["count"] == 0, label
    assert sources["statistics"]["allowed_source_counts"]["existing_cns_facilities"] == 0, label
    assert sources["statistics"]["excluded_reason_counts"] == {
        "unconfirmed_existing_cns_facility": 1,
    }, label
    assert sources["statistics"]["existing_cns_source_gate"] == "real_world_confirmed_only"


def test_p16_existing_cns_gate_keeps_only_explicitly_confirmed_real_facility():
    """反向断言：显式 ``confirmed=true`` 的真实设施仍然放行（修复不得过度收紧）。"""

    sources = registered_p16_site_sources(
        {"status": "passed", "candidates": [], "source_counts": {}},
        existing_facilities={"status": "passed", "items": [{
            "facility_id": "F-REAL-CONFIRMED",
            "metadata": {"dataset_class": "field_survey", "real_world_facility": True},
            "planning_profile": {"confirmed": True},
        }]},
        candidate_sites={"status": "passed", "items": []},
        tower_colocation={"status": "passed", "items": []},
    )
    assert [item["facility_id"] for item in sources["existing_facilities"]["items"]] == [
        "F-REAL-CONFIRMED",
    ]
    assert sources["statistics"]["excluded_reason_counts"] == {}


def test_p16_keeps_registered_node_that_carries_reference_site_id():
    """已登记节点同时带 reference_site_id 时，必须按节点自身身份匹配，不得误判未登记。"""

    shared = {"status": "passed", "candidates": [
        {"candidate_id": "node:N1", "source_id": "N1", "candidate_kind": "project_node",
         "spatial_use_status": "eligible", "explicit_reference_site_id": "R1"},
        {"candidate_id": "node:N2", "source_id": "N2", "candidate_kind": "project_node",
         "spatial_use_status": "blocked",
         "spatial_block_reason": "reference_source_crs_unresolved",
         "explicit_reference_site_id": "R2"},
    ], "source_counts": {"tower_records": 0}}
    sources = registered_p16_site_sources(
        shared,
        existing_facilities={"status": "passed", "items": []},
        candidate_sites={"status": "passed", "items": [
            _site("N1", reference_site_id="R1"), _site("N2", reference_site_id="R2"),
        ]},
        tower_colocation={"status": "passed", "items": []},
    )
    assert [item["site_id"] for item in sources["candidate_sites"]["items"]] == ["N1"]
    assert sources["statistics"]["excluded_reason_counts"] == {
        "reference_source_crs_unresolved": 1,
    }


def test_p17_input_fingerprint_covers_detection_evidence():
    """会改变响应及时性的权威探测证据必须进入 P17 输入指纹。"""

    wide = _response_result(detection_range=5000.0)
    narrow = _response_result(detection_range=200.0)
    assert _route_result(wide)["corridor"]["response_timeliness_status"] == "passed"
    assert _route_result(narrow)["corridor"]["response_timeliness_status"] == "failed"
    assert wide["input_fingerprint"] != narrow["input_fingerprint"]

    airborne_wide = _response_result(detection_range=None, aircraft_detection=5000.0)
    airborne_narrow = _response_result(detection_range=None, aircraft_detection=200.0)
    assert airborne_wide["input_fingerprint"] != airborne_narrow["input_fingerprint"]
